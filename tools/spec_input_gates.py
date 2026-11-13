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

Two gates read a file rather than an identity: `problem_dependency_surface_violation` and
`problem_dependency_init_not_published_violation` read §5.1 of a member `problem`'s own
`controlled_spec.md` (and of the composing `problem`'s), a file no leaf writes, through
`problem_procedure_names` and the section helpers `orchestration_runtime._published_surface`
uses. Those helpers are imported inside the
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


def problem_procedure_names(repo_root: Path, spec_ref: Any) -> tuple[set[str] | None, str]:
    """The PROCEDURE names of §5.1 of the ``problem`` at ``spec_ref``'s own
    ``controlled_spec.md``, as ``(names, "")``, or ``(None, detail)`` when they cannot be read.

    The ONE reader of a `problem`'s published procedure set for the member rules: the spec-input
    admission and `__init` rules below, the IR gate that ties a `problem/` dependency's
    `operations` to that set, and the source gate that decides which members publish `__init`
    (`validate_pipeline_semantics`). Each of them REFUSES what this cannot read, so they share it
    (one polarity).

    It reads the spec file, which no leaf writes and which
    ``orchestration_runtime._published_surface`` hashes for the compile key, with the same
    section helpers; it does not need a certified compile output. ``module_parameters``,
    ``types`` and ``interfaces`` are not counted: a value-pinned constant, a declaration and a
    prototype hold no state.

    ``None`` is answered for: an unresolved ``spec_ref`` (empty or not a string), a missing or
    unreadable file, a missing §5 or §5.1, a §5.1 block that does not load, an empty
    ``procedures`` list, an entry that is not a mapping, a missing or non-string ``name``, and an
    exception raised inside the read. ``detail`` then completes "the spec <ref> ..."."""
    if not isinstance(spec_ref, str) or not spec_ref.strip():
        return None, "does not resolve to one spec directory, so its §5.1 cannot be read"
    ref = spec_ref.strip().rstrip("/")
    try:
        from tools.structured_signatures import load_structured_signatures
        from tools.validate_pipeline_semantics import (
            _extract_controlled_spec_section, _extract_subsection_51, _section51_fence_body)
        path = Path(repo_root) / ref / "controlled_spec.md"
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            return None, f"has no readable controlled_spec.md ({exc})"
        section = _extract_controlled_spec_section(text, "5")
        if section is None or _extract_subsection_51(section) is None:
            return None, "has no §5.1"
        body, err = _section51_fence_body(path)
        if err or body is None:
            return None, f"has a §5.1 that cannot be read ({err})"
        struct, err = load_structured_signatures(body)
        if err:
            return None, f"has a §5.1 that does not load ({err})"
        procedures = struct.get("procedures")
        if not isinstance(procedures, list) or not procedures:
            return None, "publishes no procedure in §5.1"
        names: set[str] = set()
        for entry in procedures:
            if not isinstance(entry, dict):
                return None, f"has a §5.1 procedure entry that is not a mapping ({entry!r})"
            if "name" not in entry or not isinstance(entry["name"], str):
                return None, (f"has a §5.1 procedure entry whose `name` is missing or not a "
                              f"string ({entry.get('name')!r})")
            names.add(entry["name"])
    except Exception as exc:  # noqa: BLE001 - an exception inside the read is a refusal
        return None, f"could not be read ({type(exc).__name__}: {exc})"
    return names, ""


def problem_member_admitted_sets(member_id: str) -> tuple[frozenset[str], ...]:
    """The procedure-name sets of §5.1 a member ``problem`` may publish (the system tier,
    Part 2): ``{<member>__step}`` or ``{<member>__step, <member>__init}``. A third procedure is
    refused: a `problem`'s surface is these two (`docs/CONTROLLED_SPEC.md`)."""
    step, init = f"{member_id}__step", f"{member_id}__init"
    return frozenset({step}), frozenset({step, init})


def problem_dependency_surface_violation(repo_root: Path, member_spec_ref: Any) -> str | None:
    """Spec-input bound: a ``problem`` named under ``problems:`` is admitted as a member only
    when the PROCEDURE names of §5.1 of its own ``controlled_spec.md`` are one of
    `problem_member_admitted_sets` — ``{<member>__step}`` or ``{<member>__step, <member>__init}``
    (the system tier, Part 2, which lifted Part 1's ``__step``-only allowlist, D11).

    An ALLOWLIST. What makes a member's ``__init`` admissible is the rule that comes with it:
    the composite publishes ``__init`` itself (`problem_dependency_init_not_published_violation`),
    its ``__init`` calls the member's and nothing else names it (the source placement gate of
    each language), and a member's private state is a function of the current case's inputs
    (`docs/CONTROLLED_SPEC.md`), which the continuation case and the repeat case judge.

    Returns ``None`` only when the names read by `problem_procedure_names` equal one admitted
    set, compared as exact strings; ``<member>`` is the member's spec directory name, which is
    its ``spec_id``. Everything `problem_procedure_names` cannot read is a violation, and so is
    any other set."""
    reason = "problem_dependency_surface_not_step_or_init"
    if not isinstance(member_spec_ref, str) or not member_spec_ref.strip():
        return (f"{reason}: a `problems:` member does not resolve to one spec directory, so its "
                f"§5.1 cannot be read")
    ref = member_spec_ref.strip().rstrip("/")
    member_id = Path(ref).name
    admitted = problem_member_admitted_sets(member_id)

    def _violation(detail: str) -> str:
        return (
            f"{reason}: the `problems:` member {ref} {detail}; a `problem` is admitted as a "
            f"member only when the procedure names of §5.1 of its controlled_spec.md are "
            f"exactly {sorted(admitted[0])!r} or {sorted(admitted[1])!r}."
        )

    names, detail = problem_procedure_names(repo_root, ref)
    if names is None:
        return _violation(detail)
    if frozenset(names) not in admitted:
        return _violation(f"publishes the procedures {sorted(names)}")
    return None


def problem_dependency_init_not_published_violation(
    repo_root: Path, composite_spec_ref: Any, member_spec_ref: Any,
) -> str | None:
    """Spec-input bound (the system tier, Part 2): a ``problem`` whose ``problems:`` member
    publishes ``<member>__init`` publishes ``<composite>__init`` in its own §5.1, because that is
    the one place the member's ``__init`` may be called from (`docs/CONTROLLED_SPEC.md`), and the
    driver calls only the composite's own operations.

    Asked only of a member `problem_dependency_surface_violation` admitted. Reads both §5.1
    sets with `problem_procedure_names`; a set that cannot be read is a violation (fail-closed),
    the composite's own included. ``None`` when the member publishes no ``__init``, or when the
    composite's §5.1 names ``<composite>__init``."""
    reason = "problem_dependency_init_not_published"
    member_ref = str(member_spec_ref or "").strip().rstrip("/")
    composite_ref = str(composite_spec_ref or "").strip().rstrip("/")
    member_names, detail = problem_procedure_names(repo_root, member_ref)
    if member_names is None:
        return f"{reason}: the `problems:` member {member_ref or '(unresolved)'} {detail}"
    member_id = Path(member_ref).name
    if f"{member_id}__init" not in member_names:
        return None
    composite_names, detail = problem_procedure_names(repo_root, composite_ref)
    composite_id = Path(composite_ref).name if composite_ref else "(unresolved)"
    if composite_names is None:
        return (f"{reason}: the composing `problem` {composite_ref or '(unresolved)'} {detail}, "
                f"so whether it publishes `{composite_id}__init` for the `problems:` member "
                f"{member_ref}, which publishes `{member_id}__init`, cannot be read")
    if f"{composite_id}__init" in composite_names:
        return None
    return (f"{reason}: the `problems:` member {member_ref} publishes `{member_id}__init`, and "
            f"the composing `problem` {composite_ref} does not publish `{composite_id}__init` "
            f"in its §5.1 (it publishes {sorted(composite_names)}). A composite whose member "
            f"publishes `__init` publishes `__init` itself, and calls the member's `__init` "
            f"there and nowhere else (docs/CONTROLLED_SPEC.md).")


def problem_member_violation(
    repo_root: Path, composite_spec_ref: Any, member_spec_ref: Any,
) -> str | None:
    """Both member rules, in order: the admitted surface, then (for an admitted member) the
    composite's `__init`. The violation starts with its reason token, the text before the first
    `:`, which the closure walk records as its error reason."""
    return (problem_dependency_surface_violation(repo_root, member_spec_ref)
            or problem_dependency_init_not_published_violation(
                repo_root, composite_spec_ref, member_spec_ref))
