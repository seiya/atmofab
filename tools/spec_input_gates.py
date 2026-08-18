#!/usr/bin/env python3
"""Spec-input preconditions: the node-IDENTITY bounds checked before any phase runs.

These gates are neutral policy. They read a spec's identity (`spec_id`, `spec_kind`, the
declared dependency counts) and the tokens that reach a process argv, and they say whether the
node may enter the workflow at all. None of them holds implementation-language knowledge: the
one bound that *derives* from a language — how long a spec_id may be before the names generated
from it breach that language's identifier limit — is carried here as a neutral number and
pinned against the language backend's own limit by a test, because this gate runs BEFORE an IR
exists (`run_workflow._resolve_closure` applies it to the `spec_ref`s in `deps.yaml` /
`spec_catalog.yaml`), so there is no language to ask the registry about.

What makes these SPEC-INPUT rather than compile gates: each one is a defect of the node's
identity, which a Compile re-author cannot repair. Routing an unrepairable defect into a
warm-resume retry only spins, so they are captured here — before any phase runs — and the later
gates keep their own copies only as defense-in-depth backstops.
"""

from __future__ import annotations

import re
from typing import Any

# The longest a `spec_id` may be. The generated per-node symbols derive from it by appending a
# short role suffix, and the implementation language bounds how long an identifier may be — so
# this is that bound minus the longest suffix. It is spelled here as a NEUTRAL number rather
# than asked of the language backend: this gate runs pre-IR, where no toolchain has been
# resolved, so there is no axis value to ask about. `test_fortran_runner` pins the number
# against the language backend's identifier limit so the two cannot drift.
MAX_SPEC_ID_LEN = 55

# The character grammar a case_id must obey. The runner harness builds each per-case snapshot
# path by concatenating the runtime case_id — `raw/state_snapshots/<case_id>.json` — so a
# `case_id` such as `../../evil` traverses OUT of the run directory and a program that compiles
# and runs cleanly writes an arbitrary file. The compile gates only require a case_id to be a
# non-empty string, so `..`/`/` would otherwise slip through. Restrict the id to the same safe
# token grammar the dependency layer uses for path segments
# (`orchestration_runtime._is_safe_path_token`): `[A-Za-z0-9._-]`, no `..`, narrowed further
# because a case id also reaches the runner's argv.
# The first character additionally may not be `-`: a case id reaches the runner's argv through
# the build-runtime MCP server, which refuses a leading `-` there, so accepting one here would
# pass Compile and Build and then fail Validate.execute on an id no gate had objected to.
#
# Public because three modules ask this one question — the conductor's argv builder, the
# pipeline validator's case-id gate, and the runner emitter — and a grammar with three private
# importers is a grammar with three chances to drift.
CASE_ID_TOKEN_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._-]*$")


def spec_id_length_violation(spec_id: Any) -> str | None:
    """Spec-input bound on spec_id length — the M3d mass-opt-in prerequisite gate.

    Returns an actionable violation message when ``spec_id`` exceeds ``MAX_SPEC_ID_LEN``, else
    ``None``. The per-node symbols generated from a spec_id append a role suffix to it, so an
    over-long spec_id breaches the implementation language's identifier limit; on a
    harness-backed M3c node the host-rendered runner additionally fail-closes at render time (a
    workflow-kill a compile.generate re-author cannot repair — the spec_id is node IDENTITY, not
    authored IR content). This helper is the canonical *spec-input* capture point for exactly
    that identity precondition, which the compile.static hoist deliberately excludes: bounding
    here — before any phase runs — turns an unrepairable late render-kill into an early, clear
    rejection. The renderer keeps the same bound as a defense-in-depth backstop."""
    sid = spec_id.strip() if isinstance(spec_id, str) else ""
    if len(sid) > MAX_SPEC_ID_LEN:
        return (
            f"spec_id {sid!r} is {len(sid)} chars (>{MAX_SPEC_ID_LEN}); the per-node symbols "
            f"derived from it would breach the implementation language's identifier limit "
            f"(and fail-close a harness-backed node's host-render). Rename the spec to "
            f"≤{MAX_SPEC_ID_LEN} chars.")
    return None


def infra_dep_count_violation(spec_kind: Any, infra_dep_count: int) -> str | None:
    """Spec-input bound on the number of ``infrastructure`` direct dependencies.

    Returns an actionable violation message unless the node declares EXACTLY ONE
    ``infrastructure`` (runner-harness) direct dependency, or is itself an ``infrastructure``
    spec (the harness authors its own self-test runner, so it declares none). Sibling of
    ``spec_id_length_violation``: both are node-IDENTITY preconditions a Compile re-author
    cannot repair, so both are captured at spec-input rather than hoisted into the compile.static
    gate (routing an unrepairable defect to a warm-resume retry would only spin).

    Before this gate, a physics node with zero or >1 infrastructure deps silently degraded to the
    leaf-authored-runner path: ``_conductor_authors_runner`` requires exactly one, so the runner
    was simply never host-rendered and the failure was a quiet loss of the harness path rather
    than an error. That non-M3c physical path has been removed — the only live leaf-authored
    runner is an ``infrastructure`` node's own self-test — so the degradation is now a hard
    rejection."""
    # `.strip()` and NOTHING else — the exemption must be spelled exactly as every
    # downstream reader spells it. `_conductor_authors_runner`, `_pure_leaf_substep` and
    # `_validate_toolchain_backend_supported` all compare `str(...).strip() ==
    # "infrastructure"` with no case folding, so a `spec_kind: Infrastructure` that this
    # gate lower-cased into an exemption would be treated as a PHYSICS node by all three —
    # exempted here and then silently landed on the removed leaf-authored-runner path, with
    # no gate firing anywhere. Being case-sensitive here makes that shape a spec-input
    # rejection instead, which is the direction that fails closed.
    kind = spec_kind.strip() if isinstance(spec_kind, str) else ""
    if kind == "infrastructure":
        return None
    if infra_dep_count == 1:
        return None
    remedy = (
        "Add the single `infrastructure_id` entry" if infra_dep_count < 1
        else f"Remove {infra_dep_count - 1} of them, keeping the one harness this node "
             "builds against")
    return (
        f"a non-infrastructure spec must declare exactly one `infrastructure` "
        f"(runner-harness) dependency in deps.yaml; found {infra_dep_count}. The runner "
        f"glue is host-rendered against exactly that harness, and the former "
        f"leaf-authored-runner path for a node without it has been removed "
        f"(docs/workflow/phases/phase_01_compile.md). {remedy} "
        f"(see spec/problem/dynamics/advection_diffusion/advdiff1d_linear/deps.yaml)."
    )
