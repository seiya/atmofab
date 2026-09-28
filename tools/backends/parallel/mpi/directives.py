"""The MPI presence floor of `Generate.static` (issue #316, R4-c PR-4).

The `parallel_directives` capability, stated for a model whose parallelism is not a directive: a
program built for this model runs as several processes, and the message-passing runtime is
reached through the target's harness alone (`docs/backends/parallel/mpi/GENERATE_RULES.md`). So
the floor a `component` / `problem` node's sources are held to is a whole-node judgment rather
than a per-file directive count, and this module states it as one (`PresenceFloor.
node_violations`, which the validator asks instead of the per-file directive loop when a floor
carries it):

* the model and checks sources never reach the library directly — no `use` of its modules, no
  `include` of its header, no call of a procedure of its namespace — whatever the plan says;
* unless the bundle's `target_lowering_plan.parallelization` explicitly declines MPI, the plan
  declares `state_residency: distributed`, at least one source calls the harness's partition or
  halo exchange, and the checks module sets at least one bound array's partition axis
  (`sb_<var>_axis`) to something other than a literal zero.

It is a PRESENCE floor: whether the decomposition is right, whether each rank computes only what
it owns, and whether the global quantities go through the reductions is `Generate.verify` G6's
judgment. A kernel that computes the whole range on every rank and reports a partition passes it
— the accepted residual of issue #316, stated in the reviewer's fragment. The loops a directive
would parallelize are not counted: whether a kernel needs distributing does not depend on whether
its source spells a counted loop.

The sources are read as the Fortran backend reads them — its `statements` (comments stripped,
continuations joined, `;` split) with string contents masked — asked of the registry, not
imported from the language backend's package.

Stdlib only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

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

# A call of the harness's partition or halo exchange, by its harness name: a `call` statement,
# or the action of a one-line logical `if` (`if (n > 0) call h__partition(...)`).
_DISTRIBUTING_CALL_RE = re.compile(
    r"^(?:if\s*\(.*\)\s*)?call\s+\w+__(?:partition|exchange_halo_r[1-4])\b", re.IGNORECASE)

# An assignment (or a declaration's initializer) of a bound array's partition axis; `(?!=)`
# keeps a comparison `sb_u_axis == 0` out. The value is read up to the next top-level comma.
_AXIS_ASSIGNMENT_RE = re.compile(r"(?<![\w%])sb_(\w+)_axis\s*=(?!=)\s*([^,;]*)", re.IGNORECASE)
# A literal zero, with an optional kind suffix and any enclosing parentheses.
_ZERO_LITERAL_RE = re.compile(r"^\(*\s*[+]?0+(?:_\w+)?\s*\)*$")


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

    #: `node_violations(model_texts=, checks_texts=, plan=, bound_arrays=)` → the findings,
    #: each naming a file. `bound_arrays` is the node's bound array variables (the IR's
    #: snapshot variables of rank >= 1).
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


def _fortran_cpu_violations(*, model_texts: dict[Path, str], checks_texts: dict[Path, str],
                            plan: Any, bound_arrays: Iterable[str]) -> list[str]:
    statements = {path: _code_statements(text)
                  for path, text in [*model_texts.items(), *checks_texts.items()]}
    out: list[str] = []
    for path, stmts in statements.items():
        direct = [raw for raw, masked in stmts
                  if _FORBIDDEN_STATEMENT_RE.match(raw) or _FORBIDDEN_REFERENCE_RE.search(masked)]
        if direct:
            out.append(
                f"{path}: the target profile resolves to MPI (parallel.backend=mpi), and a "
                "physics source reaches the message-passing runtime only through the target's "
                "harness — its partition, halo exchange, rank queries and reductions — never "
                f"directly; this source does: {direct[0][:120]!r}. Remove the library's "
                "`use` / `include` and every call or reference to a name that starts with "
                "`mpi_` (a variable of your own too — rename it), and call the harness "
                "operations the host-rendered runner lists instead")
    if lowering_plan_declines(plan):
        return out
    first = next(iter(model_texts), None) or next(iter(checks_texts), None)
    residency = plan.get("state_residency") if isinstance(plan, dict) else None
    if residency != "distributed":
        out.append(
            f"{first}: the target profile resolves to MPI (parallel.backend=mpi) and the "
            "bundle's target_lowering_plan.parallelization does not decline MPI, but the plan "
            f"declares state_residency {residency!r}: a kernel distributed over the ranks "
            "declares \"state_residency\": \"distributed\" (with distributed_state@1 in "
            "capability_requirements). Only when the kernel genuinely cannot be distributed is "
            "\"model\": \"none\" in the plan's parallelization the answer, with the reason "
            "stated; the independent reviewer holds that declaration to the kernel")
        return out
    if not any(_DISTRIBUTING_CALL_RE.match(masked)
               for stmts in statements.values() for _raw, masked in stmts):
        out.append(
            f"{first}: the target profile resolves to MPI and the plan declares distributed "
            "state, but neither the model nor the checks source calls the harness's "
            "`<harness>__partition` or `<harness>__exchange_halo_r<k>` — the owned range of "
            "each rank comes from the partition, and a stencil reads its neighbours' cells "
            "through the halo exchange. Call them by their harness names (a statement "
            "`call <harness>__partition(...)`; a renamed alias is not recognized)")
    # Only the axis of a BOUND array counts, and only where the checks module sets it: a
    # variable named like one that no capture reads (`sb_tmp_axis = 1`) distributes nothing
    # (round 1 of the PR's review built that shape and it passed).
    arrays = {str(name).casefold() for name in bound_arrays}
    axes = [m.group(2).strip() for stmts in (statements[p] for p in checks_texts)
            for _raw, masked in stmts for m in _AXIS_ASSIGNMENT_RE.finditer(masked)
            if m.group(1).casefold() in arrays]
    if not any(value and not _ZERO_LITERAL_RE.match(value) for value in axes):
        where = next(iter(checks_texts), first)
        out.append(
            f"{where}: the plan declares distributed state, but the checks module never sets "
            "the partition axis (`sb_<var>_axis`) of a bound array "
            f"({', '.join(sorted(arrays)) or 'none declared'}) to anything but 0, so every "
            "bound array is captured as replicated and nothing the run records is "
            "distributed. Set `sb_<var>_axis` to the axis a partitioned array is split along, "
            "with `sb_<var>_lo` / `sb_<var>_hi` / `sb_<var>_glo` describing this rank's owned "
            "cells (the host-rendered runner's comment states the binding)")
    return out


#: The (language, hardware class) pairs the floor is stated for.
_FLOORS: dict[tuple[str, str], PresenceFloor] = {
    ("fortran", "cpu"): PresenceFloor(node_violations=_fortran_cpu_violations),
}


def presence_floor(*, language: str, hardware_class: str) -> PresenceFloor | None:
    """The floor for a node built for MPI in `language` on `hardware_class`, or None when this
    model states none for the pair."""
    return _FLOORS.get((language, hardware_class))
