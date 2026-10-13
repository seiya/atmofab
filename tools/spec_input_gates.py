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
"""

from __future__ import annotations

import re
from typing import Any

def max_spec_id_len() -> int:
    """The longest a `spec_id` may be: the minimum of every declared language's `SPEC_ID_MAX`.

    The generated per-node symbols derive from a spec_id by appending a short role suffix, and
    each language bounds how long an identifier may be; its `bundle_facts` module states the
    spec_id bound that leaves room for its suffixes. A spec_id names one node for every target,
    so the gate takes the strictest language's answer. The registry is imported HERE, not at
    module level: a language package imports this module (through `tools.runner_ir`), so a
    module-level import is a cycle, and it would also load every language package at startup."""
    from tools.backends import registry

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
