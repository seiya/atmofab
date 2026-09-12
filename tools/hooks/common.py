#!/usr/bin/env python3
"""Host-side helpers that survived the leaf hook layer.

Until Z4 (issue #171) this module was the shared body of `tools/hooks/cli.py`, the in-sandbox
hook a leaf ran on every tool call: the Bash read-target extractor, the write policy, the
auto-read invariant, the protected-root guard. No leaf holds a tool any more — every LLM
substep is a pure function with no shell — so the hook and its body went, and what is left is
the handful of helpers whose consumers were never the hook at all.

Its callers are `tools/orchestration_runtime.py`, `tools/run_workflow.py` and
`tools/orchestration_diagnostics.py`. The file keeps its name and location for this change
only; PR-2 of issue #171 moves the operator-private-root half to `tools/operator_private_root.py`
and deletes what is left.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def validate_pipeline_semantics_stage(*, step_key: str, args_json: dict[str, Any]) -> str:
    """Validate `validate_pipeline_semantics` stage input for a step capability."""
    allowed_by_step: dict[str, frozenset[str]] = {
        "compile": frozenset({"compile", "full"}),
        "generate": frozenset({"post_generate", "post_build", "full"}),
        "build": frozenset({"post_build", "full"}),
        "validate": frozenset({"post_execute", "pre_judge", "full"}),
    }
    stage = args_json.get("stage") or args_json.get("--stage")
    if not isinstance(stage, str) or not stage.strip():
        raise ValueError(
            "pre_command_execute hook: validate_pipeline_semantics requires args_json.stage "
            "(or --stage) as non-empty string"
        )
    stage_l = stage.strip().lower()
    allowed = allowed_by_step.get(step_key)
    if allowed is not None and stage_l not in allowed:
        raise ValueError(
            "pre_command_execute hook: validate_pipeline_semantics "
            f"--stage {stage_l!r} not permitted for capability step={step_key!r} "
            f"(allowed={sorted(allowed)})"
        )

    if stage_l == "pre_judge":
        for key, val in args_json.items():
            key_s = str(key).lower().replace("_", "-")
            if "allow-missing-orchestration" in key_s or "allow-missing-llm-review" in key_s:
                if val is True or val == 1:
                    raise ValueError(
                        "pre_command_execute hook: pre_judge forbids allow-missing-orchestration "
                        "and allow-missing-llm-review"
                    )
                if isinstance(val, str) and val.strip().lower() in {"true", "1", "yes"}:
                    raise ValueError(
                        "pre_command_execute hook: pre_judge forbids allow-missing-orchestration "
                        "and allow-missing-llm-review"
                    )
    return stage_l

def _normalize_rel_posix(path_token: str) -> str:
    """Normalize repo-relative path into stable POSIX token."""
    token = path_token.strip().replace("\\", "/").lstrip("/")
    while "//" in token:
        token = token.replace("//", "/")
    return token.rstrip("/")

# Extensionless filenames permitted under a directory allowlist entry.
# Build-control names (makefile, gnumakefile) are intentionally excluded — they must be
# declared as explicit file pins to prevent undeclared command-execution injection.
_ALLOWED_EXTENSIONLESS_BYPRODUCT_NAMES: frozenset[str] = frozenset({
    "readme", "license", "changelog", "authors", "install", "notice", "copying",
})

# True compiler byproducts — created directly by the compiler as subprocess output.
# Terminal validation accepts these under a directory allowlist as confined build output.
# (NOTE: the legacy "gate provenance / gate_changed_paths" terminal model is gone —
# Phase-2 authorizes step/substep writes by write_roots-containment of the FS-diff.
# See docs/ORCHESTRATION.md.)
_COMPILER_BYPRODUCT_EXTENSIONS: frozenset[str] = frozenset({".mod", ".o", ".a"})

# Allowlist of extensions permitted under a directory allowlist entry via the
# Edit/Write file tools. Restricted to source code only.
#
# Excluded (must use explicit file pins):
#   - Build control files (.mk, .cmake, .toml, .cfg, .ini, .nml) — can alter downstream
#     build behaviour or inject arbitrary commands via CMakeLists.txt / Makefile fragments.
#   - Structured data/documents (.json, .yaml, .xml, .csv, .md, .txt, etc.) — undeclared
#     data injection is unauditable and can poison downstream steps.
#   - Compiler byproducts (.mod, .o, .a) — created directly by the compiler as subprocess
#     output, never via Edit/Write. File-tool writes of these extensions are blocked here;
#     terminal validation also rejects them unless they land under the step's write_roots —
#     agents must clean up build artefacts before record-agent-run.
#
# Extensionless files are gated by _ALLOWED_EXTENSIONLESS_BYPRODUCT_NAMES.
# Everything else is rejected (fail-closed).
_ALLOWED_BYPRODUCT_EXTENSIONS: frozenset[str] = frozenset({
    # Fortran source — primary intended output of the generate step
    ".f90", ".f", ".f95", ".f03", ".f08", ".fpp",
    # C/C++ source — primary intended output of the generate step
    ".c", ".h", ".cpp", ".hpp", ".cc", ".hh", ".cxx", ".inc",
})

def _home_dir() -> Path:
    """The host home directory, read the same way the bwrap profile reads it."""
    raw = (os.environ.get("HOME") or "").strip()
    return Path(raw) if raw else Path.home()

def operator_secret_root() -> Path:
    """`~/.atmofab/` — the root of the operator-private trees (`homes/`, `start_claims/`)."""
    return (_home_dir() / ".atmofab").resolve()

# The environment name that relocates a subtree of the operator-private root, and its
# resolver. Defined HERE, in the module that also owns
# `operator_secret_root` and the protected-read-root list, and imported by
# `tools/orchestration_runtime.py` — the same arrangement as
# `backend_credential_home_paths`, and for the same reason: the side that CREATES a tree
# under `~/.atmofab` and the side that FORBIDS reading it must resolve the location
# identically. Issue #132 is what happens without that: the root was spelled four times
# and only one of the spellings was the guard's.
#
# The SUBTREES are what is relocatable, never `operator_secret_root()` itself. That
# function is the guard's anchor, and it is what several hook tests patch `$HOME` to
# reason about; an override there would make them reason about a layout production never
# creates.
WORKFLOW_HOMES_ROOT_ENV = "ATMOFAB_WORKFLOW_HOMES_ROOT"

def workflow_homes_root() -> Path:
    """`~/.atmofab/homes` — the durable root of every isolated backend home.

    Under the operator-secret root by default, which is what lets
    `protected_host_read_roots` cover EVERY orchestration's home through one entry rather
    than only the current one it can resolve from metadata.

    `ATMOFAB_WORKFLOW_HOMES_ROOT` relocates it, and that used to silently withdraw the
    coverage: with the override pointing outside `~/.atmofab`, a leaf's Bash read of a
    SIBLING orchestration's transcript was allowed (measured), while
    `docs/HOOKS.md` and this module's own docstrings asserted that closure without
    naming the condition. The root is returned here so the guard protects wherever the
    homes actually are.
    """
    override = os.environ.get(WORKFLOW_HOMES_ROOT_ENV, "").strip()
    if override:
        # Made absolute so callers get a usable path, and that is ALL this does — it does
        # NOT make a relative override safe. `.absolute()` resolves against THIS process's
        # cwd, and the guard runs in a hook process while the writer runs in the
        # conductor: a relative override simply becomes two different absolute paths
        # instead of staying relative (measured: `relhomes` resolves under `/tmp`, under
        # the repo root, and under `$HOME` depending on who asks). An earlier version of
        # this comment claimed the call closed that axis; it does not.
        #
        # What closes it is a REFUSAL on the creation side
        # (`_create_workflow_backend_home`), which is where failing closed is available.
        # This resolver is meant to stay total — it feeds `protected_host_read_roots`, and
        # a hook that raises while deciding a read is worse than one that guards a path
        # nobody writes to — and it is NOT: `expanduser()` raises `RuntimeError` for a
        # `~account` naming no account. What follows is fail-CLOSED (`tools/hooks/cli.py`
        # turns a raising hook into a BLOCK), so a leaf gains nothing; the cost is an
        # operator whose typo refuses every Bash call without naming the variable.
        # `TODO.md` carries the item for the private-root resolvers.
        return Path(override).expanduser().absolute()
    return operator_secret_root() / "homes"

BACKEND_CREDENTIAL_BACKEND_TYPES = ("claude", "codex")

def backend_credential_home_paths(backend_type: str) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """The backend CLI's config/credential home, as `(dirs, files)`.

    CANONICAL for two consumers that must not diverge:
      * `tools/orchestration_runtime.py::_backend_runtime_bind_paths`, which
        rw-binds these into a leaf's bwrap sandbox (the CLI refreshes auth and
        writes its session transcript there);
      * the Bash read guard below, which must forbid reading exactly what that
        profile makes reachable.
    Split dirs/files because the bind side materializes a missing config *dir*
    but existence-gates the auth *file* (it cannot be fabricated).
    """
    home = _home_dir()
    btype = (backend_type or "").strip().lower()
    if btype == "claude":
        return (home / ".claude",), (home / ".claude.json",)
    if btype == "codex":
        # Mirror preflight's codex-home resolution so the guarded path is the
        # bound one even when CODEX_HOME relocates it.
        raw = os.environ.get("CODEX_HOME", "").strip() or os.environ.get("ATMOFAB_HOME", "").strip()
        codex_home = Path(raw).expanduser() if raw else home / ".codex"
        if not codex_home.is_absolute():
            codex_home = codex_home.resolve()
        return (codex_home,), ()
    return (), ()

def _resolve_lenient(path: Path) -> Path:
    """`path.resolve()`, falling back to `path` itself when the OS refuses.

    Shared by everything that has to COMPARE these roots. The comparison is what makes it
    load-bearing: `protected_host_read_roots` hands out RESOLVED paths, while
    `workflow_homes_root` returns a merely `.absolute()` one, so a caller matching an
    override-relocated root against the list must resolve it the same lenient way or a
    tree under a symlinked `/tmp` silently fails to match.
    """
    try:
        return path.resolve()
    except (OSError, ValueError, RuntimeError):
        return path

def claude_leaf_projects_roots(repo_root: Path,
                               orchestration_id: str | None = None) -> tuple[Path, ...]:
    """Every directory a Claude leaf's per-project state may live under.

    ONE root since Z4 (issue #171): the operator's `~/.claude/projects`. Every claude leaf is a
    PURE leaf, which takes no settings layer (`--safe-mode`) and is prepared no private
    `CLAUDE_CONFIG_DIR`, so it writes its `--session-id` transcript there. Issue #63's
    per-orchestration private home existed for the AGENTIC leaf and went with it; a transcript
    written under one before Z4 is no longer reachable from here — accepted, and the reason the
    signature keeps `orchestration_id` is that its callers pass one and the key it would select
    no longer exists.

    CANONICAL for the consumers that must not drift apart, all of which locate a leaf's own
    session by `<projects-root>/<slug>/<session-id>.jsonl`:
      * `orchestration_diagnostics._locate_leaf_transcript` / `_claude_projects_dir`
        (post-mortem of a dangling leaf).

    `workflow_conductor._claude_session_resumable` is deliberately NOT one of them and must not
    be added: a resume is served from the ONE home the launch uses, so the version that searched
    both called a pre-move session resumable and threw `--resume` at a home that never held it.
    `dual-read-pairs.md:22` is canonical for that split. (This list named it as a consumer until
    issue #137; it was already wrong, and the docstring is where a maintainer looks before
    changing the resolver.)
    """
    return (_home_dir() / ".claude" / "projects",)
