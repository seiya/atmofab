#!/usr/bin/env python3
"""Spec-input preconditions: the node-IDENTITY bounds checked before any phase runs.

These gates are neutral policy. They read a spec's identity (`spec_id`, `spec_kind`, the
declared dependency counts) and the tokens that reach a process argv, and they say whether the
node may enter the workflow at all. None of them holds implementation-language knowledge: the
one bound that *derives* from a language — how long a spec_id may be before the names generated
from it breach that language's identifier limit — is asked of EVERY declared language backend
and the strictest answer taken (`max_spec_id_len`): the gate judges a node's identity, which
every target the node may later be built for shares, so the bound is the one no declared
language would refuse.

What makes these SPEC-INPUT rather than compile gates: each one is a defect of the node's
identity, which a Compile re-author cannot repair. Routing an unrepairable defect into a
warm-resume retry only spins, so they are captured here — before any phase runs — and the later
gates keep their own copies only as defense-in-depth backstops.

One gate reads a file rather than an identity: `problem_dependency_surface_violation` reads §5.1
of a member `problem`'s own `controlled_spec.md`, a file no leaf writes, through the section
helpers `orchestration_runtime._published_surface` uses. Those helpers are imported inside the
function, so importing this module imports neither them nor anything they load.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from tools.backends import registry


def max_spec_id_len() -> int:
    """The longest a `spec_id` may be: the minimum of every declared language's `SPEC_ID_MAX`.

    The generated per-node symbols derive from a spec_id by appending a short role suffix, and
    each language bounds how long an identifier may be; its `bundle_facts` module states the
    spec_id bound that leaves room for its suffixes. A spec_id names one node for every target,
    so the gate takes the strictest language's answer. Read per call: the registry loads a
    language package only when `capability_module` is asked, so importing this module loads
    none."""
    bounds = [
        registry.capability_module("language", lang, "bundle_facts").SPEC_ID_MAX
        for lang in registry.backend_ids("language")
        if registry.provides("language", lang, "bundle_facts")
    ]
    if not bounds:
        raise RuntimeError(
            "no declared language backend provides bundle_facts, so no spec_id bound exists; "
            "see tools/backends/registry.py")
    return min(bounds)

# The character grammar a case_id must obey. The runner harness builds each per-case snapshot
# path by concatenating the runtime case_id — `raw/state_snapshots/<case_id>.json` — so a
# `case_id` such as `../../evil` traverses OUT of the run directory and a program that compiles
# and runs cleanly writes an arbitrary file. The compile gates only require a case_id to be a
# non-empty string, so `..`/`/` would otherwise slip through. Restrict the id to the same safe
# token grammar the dependency layer uses for path segments
# (`orchestration_runtime._is_safe_path_token`): `[A-Za-z0-9._-]`, no `..`, narrowed further
# because a case id also reaches the runner's argv.
# The first character additionally may not be `-`: a case id reaches the runner's argv
# (`--cases <spec> <case_id>...`), where a word opening with `-` is an option's spelling, and
# nothing between here and that argv refuses one (the build-runtime library passes
# `run_program`'s `command` through unconstrained, by design). Refusing it here is the one
# place it is stated. (Until issue #444 this comment said the MCP server refused it; it did
# not — measured: `tool_run_program` runs `["echo", "-base"]`, and `CASES=-base` is an
# accepted make assignment.)
#
# Public because three modules ask this one question — the conductor's argv builder, the
# pipeline validator's case-id gate, and the runner emitter — and a grammar with three private
# importers is a grammar with three chances to drift.
CASE_ID_TOKEN_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._-]*$")


#: The spec kinds whose runner the host renders over their target's harness (the "M3c" node:
#: the leaf authors model + checks). An `infrastructure` node authors its own self-test runner,
#: and a `profile` is not a node at all (issue #175). The conductor's `_conductor_authors_runner`
#: and the validator's `_m3c_language` read this one tuple (issue #284: the harness is the
#: target's, so the kind — not a dependency count — is what decides).
M3C_SPEC_KINDS: tuple[str, ...] = ("component", "problem")


def spec_id_length_violation(spec_id: Any) -> str | None:
    """Spec-input bound on spec_id length — the M3d mass-opt-in prerequisite gate.

    Returns an actionable violation message when ``spec_id`` exceeds ``max_spec_id_len()``, else
    ``None``. The per-node symbols generated from a spec_id append a role suffix to it, so an
    over-long spec_id breaches the implementation language's identifier limit; on a
    harness-backed M3c node the host-rendered runner additionally fail-closes at render time (a
    workflow-kill a compile.generate re-author cannot repair — the spec_id is node IDENTITY, not
    authored IR content). This helper is the canonical *spec-input* capture point for exactly
    that identity precondition, which the compile.static hoist deliberately excludes: bounding
    here — before any phase runs — turns an unrepairable late render-kill into an early, clear
    rejection. The renderer keeps the same bound as a defense-in-depth backstop."""
    sid = spec_id.strip() if isinstance(spec_id, str) else ""
    bound = max_spec_id_len()
    if len(sid) > bound:
        return (
            f"spec_id {sid!r} is {len(sid)} chars (>{bound}); the per-node symbols "
            f"derived from it would breach the implementation language's identifier limit "
            f"(and fail-close a harness-backed node's host-render). Rename the spec to "
            f"≤{bound} chars.")
    return None


def infra_dep_declared_violation(infra_dep_count: int) -> str | None:
    """Spec-input bound: a ``deps.yaml`` declares NO ``infrastructure`` dependency, whatever the
    spec's kind (issue #284, R4-a).

    The runner harness is an attribute of the TARGET a node is built for, not of the spec: the
    target profile names it (``spec/targets/<target_id>.yaml`` ``harness``), and the host adds it
    to every non-``infrastructure`` node's closure for that target
    (``target_profile.target_harness_entries``). A harness declared in ``deps.yaml`` would
    pin one target's harness into a spec that is meant to be built for any target, and would put
    it into the target-free Compile closure — so a declaration is refused rather than read.

    Until R4-a PR-3 this was the opposite rule — exactly one ``infrastructure`` entry on every
    spec that builds, none on an ``infrastructure`` or ``profile`` spec — which is the decision
    this reverses (``docs/SPEC.md`` req. 9). Being kind-agnostic, it needs no ``spec_kind``: the
    exemption the old rule read off the catalog has no subject any more. Like the ``spec_id``
    bound it is a node-IDENTITY defect a Compile re-author cannot repair, so it is captured at
    spec-input, before any phase runs."""
    if infra_dep_count == 0:
        return None
    return (
        f"infrastructure_dependency_declared_in_deps: deps.yaml declares {infra_dep_count} "
        f"`infrastructure` (runner-harness) dependenc{'y' if infra_dep_count == 1 else 'ies'}; "
        f"a harness is a target attribute (spec/targets/<target_id>.yaml `harness`), never a "
        f"spec dependency (issue #284). Remove the `infrastructure:` section."
    )


def problem_dep_on_non_problem_violation(declaring_kind: Any, problem_dep_count: int) -> str | None:
    """Spec-input bound: a ``deps.yaml`` ``problems:`` entry is admitted on a ``problem`` only
    (the system tier, Part 1: a ``problem`` composes ``problem``s).

    ``declaring_kind`` is the declaring spec's ``spec_kind`` AS THE CATALOG FILES IT, never the
    ``spec_kind:`` line of its own ``deps.yaml``, which carries no schema. A caller that could not
    obtain the kind from the catalog passes ``None``, and a declaration is then refused rather
    than admitted on a self-declared value. A ``component`` that reached the composition gates
    would meet gates that assume its dependencies are components. Like the other spec-input
    bounds this is a node-IDENTITY defect a Compile re-author cannot repair."""
    if problem_dep_count == 0:
        return None
    if isinstance(declaring_kind, str) and declaring_kind.strip() == "problem":
        return None
    kind_text = (f"a `{declaring_kind.strip()}`" if isinstance(declaring_kind, str)
                 and declaring_kind.strip() else "a spec whose kind the catalog does not confirm")
    return (
        f"problem_dependency_on_non_problem: deps.yaml declares {problem_dep_count} "
        f"`problems:` entr{'y' if problem_dep_count == 1 else 'ies'} on {kind_text}; only a "
        f"`problem` composes `problem`s, and its kind is read from "
        f"spec/registry/spec_catalog.yaml. Remove the `problems:` entries, or register the spec "
        f"as a `problem`."
    )


def problem_dependency_surface_violation(repo_root: Path, member_spec_ref: Any) -> str | None:
    """Spec-input bound: a ``problem`` named under ``problems:`` is admitted as a member only
    when the PROCEDURE names of §5.1 of its own ``controlled_spec.md`` are exactly
    ``{<member>__step}`` (decision D11 of the system tier's Part 1).

    The rule is an ALLOWLIST. The dependency-call gate accepts any one ``<member>__*`` call, and
    the driver calls only the composite's own operations, so a member that publishes an
    ``__init`` (or any further procedure) would let a composite establish the member's state
    once and run every later case on the first case's fields. The rule for a member's private
    state belongs to the system tier's Part 2, which lifts this refusal.

    It reads the member's spec file, which no leaf writes and which
    ``orchestration_runtime._published_surface`` hashes for the compile key, with the same
    section helpers; it does not need a certified compile output, so a member is refused before
    its closure is certified. ``module_parameters``, ``types`` and ``interfaces`` are not counted:
    a value-pinned constant, a declaration and a prototype hold no state.

    Returns ``None`` only when the set of names taken from EVERY ``procedures`` entry is exactly
    ``{<member>__step}``, compared as exact strings; ``<member>`` is the member's spec
    directory name, which is its ``spec_id``. Each of the following is a violation: an
    unresolved member (``member_spec_ref`` empty or not a string), a missing or unreadable file,
    a missing §5 or §5.1, a §5.1 block that does not load, an empty ``procedures`` list, an
    entry that is not a mapping, a missing or non-string ``name``, any other name, and an
    exception raised inside the read."""
    reason = "problem_dependency_surface_not_step_only"
    if not isinstance(member_spec_ref, str) or not member_spec_ref.strip():
        return (f"{reason}: a `problems:` member does not resolve to one spec directory, so its "
                f"§5.1 cannot be read")
    ref = member_spec_ref.strip().rstrip("/")
    member_id = Path(ref).name
    expected = f"{member_id}__step"

    def _violation(detail: str) -> str:
        return (
            f"{reason}: the `problems:` member {ref} {detail}; a `problem` is admitted as a "
            f"member only when the procedure names of §5.1 of its controlled_spec.md are "
            f"exactly [{expected!r}] (the rule for a member's private state and `__init` is not "
            f"yet defined)."
        )

    try:
        from tools.structured_signatures import load_structured_signatures
        from tools.validate_pipeline_semantics import (
            _extract_controlled_spec_section, _extract_subsection_51, _section51_fence_body)
        path = Path(repo_root) / ref / "controlled_spec.md"
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            return _violation(f"has no readable controlled_spec.md ({exc})")
        section = _extract_controlled_spec_section(text, "5")
        if section is None or _extract_subsection_51(section) is None:
            return _violation("has no §5.1")
        body, err = _section51_fence_body(path)
        if err or body is None:
            return _violation(f"has a §5.1 that cannot be read ({err})")
        struct, err = load_structured_signatures(body)
        if err:
            return _violation(f"has a §5.1 that does not load ({err})")
        procedures = struct.get("procedures")
        if not isinstance(procedures, list) or not procedures:
            return _violation("publishes no procedure in §5.1")
        names: set[str] = set()
        for entry in procedures:
            if not isinstance(entry, dict):
                return _violation(f"has a §5.1 procedure entry that is not a mapping ({entry!r})")
            if "name" not in entry or not isinstance(entry["name"], str):
                return _violation(
                    f"has a §5.1 procedure entry whose `name` is missing or not a string "
                    f"({entry.get('name')!r})")
            names.add(entry["name"])
    except Exception as exc:  # noqa: BLE001 - an exception inside the read is a violation
        return _violation(f"could not be read ({type(exc).__name__}: {exc})")
    if names != {expected}:
        return _violation(f"publishes the procedures {sorted(names)}")
    return None
