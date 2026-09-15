"""The operator-private root (`~/.atmofab`) and the backend homes under it.

ONE place resolves `~/.atmofab` and each relocatable subtree beneath it, because the side
that CREATES a tree there and the side that names it elsewhere must agree: issue #132 is what
happens without that — the root was spelled four times and only one spelling was the guard's.

This file is what is left of `tools/hooks/common.py`, which was the shared body of the
in-sandbox leaf hook (`tools/hooks/cli.py`): the Bash read-target extractor, the write policy,
the auto-read invariant, the protected-root guard. Z4 (issue #171) retired the leaf that held
tools, so the hook and its body went; these resolvers stayed because their consumers were
never the hook. PR-2 of that issue moved them here and deleted the old module, so the name no
longer says "hook" about code no hook runs.

Its readers are `tools/orchestration_runtime.py` (the bwrap profile's credential binds and the
isolated backend homes), `tools/run_workflow.py` (the exclusive start claim) and
`tools/orchestration_diagnostics.py` (locating a leaf's own transcript).

Stdlib only, and imported at module level by all three.
"""

from __future__ import annotations

import os
from pathlib import Path


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

    Under the operator-secret root by default. That placement was chosen so
    `protected_host_read_roots` could cover EVERY orchestration's home through one entry
    rather than only the one it could resolve from metadata; that guard went with the leaf
    hook layer in Z4 ([issue #171](https://github.com/seiya/atmofab/issues/171)), and the
    placement is kept because `tools/prune_workflow_homes.py` and the resolvers below still
    want one root, not a scatter.

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
        # This resolver is meant to stay total and is NOT: `expanduser()` raises
        # `RuntimeError` for a `~account` naming no account. The totality requirement came
        # from the Bash read guard, which called this while DECIDING a read and turned a
        # raising hook into a BLOCK — fail-closed, so a leaf gained nothing, at the cost of an
        # operator whose typo refused every Bash call without naming the variable. That guard
        # is deleted (Z4, issue #171) and the remaining callers are host-side, where a raise
        # is a visible failure rather than a silent refusal — so the DEFECT is unchanged and
        # its consequence is now ordinary. `TODO.md` carries the item.
        return Path(override).expanduser().absolute()
    return operator_secret_root() / "homes"

BACKEND_CREDENTIAL_BACKEND_TYPES = ("claude", "codex")

def backend_credential_home_paths(backend_type: str) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """The backend CLI's config/credential home, as `(dirs, files)`.

    ONE consumer now: `tools/orchestration_runtime.py::_backend_runtime_bind_paths`, which
    rw-binds these into a leaf's bwrap sandbox (the CLI refreshes auth and writes its session
    transcript there). It was canonical for TWO that must not diverge — the second was the
    Bash read guard, which had to forbid reading exactly what that profile makes reachable,
    and which went with the leaf hook layer in Z4 (issue #171). With the pair gone the
    divergence risk is gone with it; what remains is one caller reading one list.
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

    Shared by everything that has to COMPARE these roots. The comparison was load-bearing
    while `protected_host_read_roots` handed out RESOLVED paths (deleted in Z4, issue #171)
    against a `workflow_homes_root` that returns a merely `.absolute()` one, so a caller
    matching an override-relocated root against the list had to resolve it the same lenient
    way or a
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
      * `orchestration_diagnostics._locate_leaf_transcript` (post-mortem of a dangling
        leaf).

    `workflow_conductor._claude_session_resumable` is deliberately NOT one of them and must not
    be added: a resume is served from the ONE home the launch uses, so the version that searched
    both called a pre-move session resumable and threw `--resume` at a home that never held it.
    `dual-read-pairs.md:22` is canonical for that split. (This list named it as a consumer until
    issue #137; it was already wrong, and the docstring is where a maintainer looks before
    changing the resolver.)
    """
    return (_home_dir() / ".claude" / "projects",)
