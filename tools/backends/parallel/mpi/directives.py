"""The MPI presence floor of `Generate.static` (issue #316, R4-c PR-4; issue #610, plan B §3.7).

The `parallel_directives` capability, stated for a model whose parallelism is not a directive: a
program built for this model runs as several processes, and the message-passing runtime is
reached through the target's harness alone (`docs/backends/parallel/mpi/GENERATE_RULES.md`). So
the floor a `component` / `problem` node's sources are held to is a whole-node judgment rather
than a per-file directive count, and this module states it as one (`PresenceFloor.
node_violations`, which the validator asks instead of the per-file directive loop when a floor
carries it). The host-rendered driver distributes each case (issue #610): it partitions the
spatial axes the IR declares, hands each call its rank's block, gathers the captures and makes
the stop uniform. What the floor asks of the leaf's MODEL sources is what stays theirs:

* they never reach the library directly — no `use` of its modules, no `include` of its header,
  no call of a procedure of its namespace;
* on a `component` node they name no harness operation at all (issue #512): every caller — the
  driver and a consumer alike — calls a component's published operations on its own rank's
  block, so an operation that partitions, exchanges, asks the grid or reduces inside itself
  distributes that block a second time;
* the plan declares `state_residency: distributed` (one rule for both kinds on this target;
  the bundle contract couples `distributed_state@2` to it);
* on a `problem` node at least one model source calls the harness's halo exchange by its harness
  name: a problem's step reads its neighbours' cells across a block edge only through it.

A plan that declines MPI exempts nothing: the driver distributes from the IR whatever the plan's
`parallelization` says, so a `problem` that declines and exchanges nothing fails the shape
comparison, and a `component` is held to the same rules either way. `lowering_plan_declines`
stays the `parallel_directives` capability's interface, which the OpenMP and CUDA floors' callers
reach through the validator.

It is a PRESENCE floor: whether a problem's exchange precedes every stencil read, with the spec's
periodicity, and whether each global quantity goes through a reduction is `Generate.verify` G6's
judgment, and the shape comparison of the sweep is what judges the result.

The sources are read as the Fortran backend reads them — its `statements` (comments stripped,
continuations joined, `;` split) with string contents masked — asked of the registry, not
imported from the language backend's package.

Stdlib only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_NO_PARALLELISM_VALUES = frozenset({"none", "off", "serial", "sequential", "false", "disabled"})
_LOWERING_PARALLELIZATION_MODEL_KEYS = frozenset({"model", "method", "scheme", "kind"})

# A statement that reaches the library itself: a `use` of either of its Fortran modules (with
# the optional attribute and `::` forms), an `include` of its header, or a call of anything in
# its namespace. `mpi\b` does not match inside `mpi_utils` (`_` is a word character), and a
# harness name (`…_mpi__partition`) never STARTS a statement with `mpi_`.
_FORBIDDEN_STATEMENT_RE = re.compile(
    r"^(?:use\b\s*(?:,\s*\w+\s*)?(?:::\s*)?mpi(?:_f08)?\b"
    r"|include\b\s*['\"]\s*mpif\.h"
    r"|call\s+mpi_\w*)",
    re.IGNORECASE)
# A function reference or call anywhere in a statement: a name of the library's namespace
# followed by an argument list, not preceded by a word character or a component selector
# (`x%mpi_count(1)` is the leaf's own component).
_FORBIDDEN_REFERENCE_RE = re.compile(r"(?<![\w%])mpi_\w*\s*\(", re.IGNORECASE)



def _exchange_call_re(harness_spec_id: str) -> re.Pattern[str]:
    """A call of the target harness's halo exchange, by its harness name — the harness's own
    prefix, and only the exchanges it publishes (§3.3: `exchange_halo_r1` / `_r2`) — as a `call`
    statement or the action of a one-line logical `if` (`if (n > 0) call h__exchange_halo_r1(…)`).
    Round 3 of issue #316 PR-4's review found the prefix read as any identifier, so a leaf's own
    `<spec_id>__exchange_halo_r1` counted."""
    return re.compile(
        rf"^(?:if\s*\(.*\)\s*)?call\s+{re.escape(harness_spec_id)}__exchange_halo_r[12]\b",
        re.IGNORECASE)


def _code_statements(text: str) -> list[tuple[str, str]]:
    """`text` as the Fortran backend's statements, each as `(raw, masked)`: stripped, and the
    second with string contents masked. A statement-start pattern reads the raw one (an
    `include` names its file in a literal); a pattern that may match anywhere reads the masked
    one, so a message string naming a procedure is not a reference to it."""
    from tools.backends import registry
    reader = registry.capability_module("language", "fortran", "source_reading")
    return [(s.strip(), reader.mask_string_contents(s).strip()) for s in reader.statements(text)]



@dataclass(frozen=True)
class PresenceFloor:
    """What the floor asks of one (language, hardware class) pair: a whole-node judgment."""

    #: `node_violations(model_texts=, plan=, harness_spec_id=, node_kind=)` → the findings, each
    #: naming a file. `harness_spec_id` is the target's harness; `node_kind` is the first segment
    #: of the node_key (`component` / `problem`).
    node_violations: Any


def lowering_plan_declines(plan: Any) -> bool:
    """True when a bundle's ``target_lowering_plan`` EXPLICITLY declines MPI: a model-bearing
    member of its ``parallelization`` object names no parallelism or a model other than MPI, and
    none names MPI. An absent plan, object or model declines nothing (the target's backend is the
    default), as for OpenMP (`tools/backends/parallel/openmp/directives.py`)."""
    if not isinstance(plan, dict):
        return False
    par = plan.get("parallelization")
    if not isinstance(par, dict):
        return False
    declined = False
    for key, value in par.items():
        if str(key).strip().lower() not in _LOWERING_PARALLELIZATION_MODEL_KEYS:
            continue
        if not isinstance(value, str) or not value.strip():
            continue
        token = value.strip().lower()
        if "mpi" in token and token not in _NO_PARALLELISM_VALUES:
            return False
        declined = True
    return declined


def _harness_reference_re(harness_spec_id: str) -> re.Pattern[str]:
    """Any name of the target harness's namespace (`<harness>__<op>`), not preceded by a word
    character or a component selector — a reference, a call, or a name in a `use` list."""
    return re.compile(rf"(?<![\w%]){re.escape(harness_spec_id)}__\w+", re.IGNORECASE)


def _fortran_cpu_violations(*, model_texts: dict[Path, str], plan: Any,
                            harness_spec_id: str, node_kind: str) -> list[str]:
    statements = {path: _code_statements(text) for path, text in model_texts.items()}
    out: list[str] = []
    if node_kind == "component" and harness_spec_id:
        named = _harness_reference_re(harness_spec_id)
        for path in model_texts:
            hits = [m.group(0) for _raw, masked in statements[path] for m in named.finditer(masked)]
            if hits:
                out.append(
                    f"{path}: a component's published operations are called on a rank's own "
                    "block — by the host-rendered driver, which partitions each case, and by "
                    "every consumer — so a component MODEL source names no operation of the "
                    f"harness; this one names {hits[0]!r}: an operation that partitions, "
                    "exchanges, asks the grid or reduces inside itself distributes an already "
                    "distributed block a second time. Keep each operation computing exactly "
                    "what the spec says over the arrays it is handed")
    for path, stmts in statements.items():
        direct = [raw for raw, masked in stmts
                  if _FORBIDDEN_STATEMENT_RE.match(raw) or _FORBIDDEN_REFERENCE_RE.search(masked)]
        if direct:
            out.append(
                f"{path}: the target profile resolves to MPI (parallel.backend=mpi), and a "
                "physics source reaches the message-passing runtime only through the target's "
                "harness — its halo exchange, rank and grid queries and reductions — never "
                f"directly; this source does: {direct[0][:120]!r}. Remove the library's "
                "`use` / `include` and every call or reference to a name that starts with "
                "`mpi_` (a variable of your own too — rename it), and call the harness "
                "operations instead")
    first = next(iter(model_texts), None)
    residency = plan.get("state_residency") if isinstance(plan, dict) else None
    if residency != "distributed":
        out.append(
            f"{first}: the target profile resolves to MPI (parallel.backend=mpi), and the "
            f"bundle's target_lowering_plan declares state_residency {residency!r}: the "
            "host-rendered driver decides per case and process grid, from the IR's "
            "`drive.distribute` axes, whether to distribute the case over the ranks, and on a "
            "distributed case each rank's state is its block, so the plan declares "
            "\"state_residency\": \"distributed\" (with distributed_state@2 in "
            "capability_requirements)")
    if node_kind == "problem" and harness_spec_id:
        exchange = _exchange_call_re(harness_spec_id)
        if not any(exchange.match(masked)
                   for stmts in statements.values() for _raw, masked in stmts):
            out.append(
                f"{first}: the target profile resolves to MPI, and no model source of this "
                f"problem calls the harness's `{harness_spec_id}__exchange_halo_r1` / `_r2` by "
                "that name: the host-rendered driver hands the step its rank's block, and a "
                "stencil reads its neighbours' cells across the block's edge only through the "
                "halo exchange (a statement "
                f"`call {harness_spec_id}__exchange_halo_r2(...)`; a renamed alias is not "
                "recognized)")
    return out


#: The (language, hardware class) pairs the floor is stated for.
_FLOORS: dict[tuple[str, str], PresenceFloor] = {
    ("fortran", "cpu"): PresenceFloor(node_violations=_fortran_cpu_violations),
}


def presence_floor(*, language: str, hardware_class: str) -> PresenceFloor | None:
    """The floor for a node built for MPI in `language` on `hardware_class`, or None when this
    model states none for the pair."""
    return _FLOORS.get((language, hardware_class))
