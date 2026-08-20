"""Test-session hygiene for the isolated backend homes.

`_prepare_claude_workflow_home` (and its codex twin) create a private per-orchestration
home and nothing removes it. Since issue #64 that home is DURABLE — `<homes-root>/<oid>/
<backend>` under `~/.met-dsl/homes` — because it holds the leaf's only conversation
record, and losing it makes a billed run unauditable after the fact. Retention is manual:
`tools/prune_workflow_homes.py`, never anything automatic.

A TEST RUN must not participate in that. Every fixture that drives `record_launch` for a
claude-shaped leaf prepares a home, so a suite would otherwise write a few hundred
directories into the operator's real `~/.met-dsl/homes` and leave them there — mixed in
with the homes of real runs, where the prune tool would find them unverifiable (their
"owner" checkouts are temporary directories that no longer exist).

TWO LAYERS, and the second is the one that actually holds:

  1. REDIRECT. A function-scoped autouse fixture points `METDSL_WORKFLOW_HOMES_ROOT` at
     a per-test `tmp_path`, so homes land where pytest already cleans up. Per-TEST rather
     than per-session on purpose: the home path is deterministic now, so two tests using
     the same fixed orchestration id would collide on the exclusive `os.mkdir` under a
     shared root.
  2. ENFORCE, BEFORE THE FACT. A session-scoped guard wraps `_workflow_homes_root` — the
     one function that decides WHERE a home goes — and raises if it is about to return
     the operator's REAL homes root. The redirect is a default a test can undo
     (`patch.dict(os.environ, ..., clear=True)` without re-setting the name is one line
     away), and this turns "the suite wrote into the operator's tree" from something
     noticed weeks later into a failure at the call that did it.

     PREVENT, NOT DETECT, and the distinction was paid for: the first version of this
     guard wrapped the two PREPARERS and raised on the path they RETURNED, so by the time
     it fired the directory was already on disk and nothing removed it. A reviewer
     running one mutant that made `_workflow_homes_root` ignore the redirect left four
     real directories in the operator's `~/.met-dsl/homes` — permanent, unverifiable
     residue in the one tree whose retention is manual. Wrapping the resolver means the
     mutant that reaches past the redirect cannot create anything at all.

     "REAL" is load-bearing: the root is resolved ONCE, from the environment as the
     session starts, and NOT re-derived inside the wrapper. Several tests patch `$HOME`
     to a temporary directory precisely in order to exercise the default
     `operator_secret_root()/homes` resolution; re-deriving would make the guard follow
     them there and fail the very tests that prove the production path. What the guard
     is for is the operator's own home, and that one does not move while pytest runs.

The previous version of this file did neither: it TRACKED the homes the session created
under `/tmp` and rmtree'd them at the end. That bookkeeping is gone with the /tmp
location, and so is the reason it was written so carefully. It is recorded here because
the two weaker rules it rejected are still the tempting ones, and both delete a home that
is not the suite's: a plain before/after set difference over the homes root removes a home
belonging to a workflow started WHILE the suite runs, and excluding the homes named by
this checkout's `orchestration_meta.json` still removes one belonging to a run in a
DIFFERENT checkout, whose metadata is not visible from here. Redirecting is what makes the
question not arise — the suite never names the real root, so it never has to decide what
inside it is safe to remove.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _redirect_workflow_homes_root(tmp_path, monkeypatch):
    """Point every isolated backend home this test creates into `tmp_path`."""
    from tools.orchestration_runtime import WORKFLOW_HOMES_ROOT_ENV

    root = tmp_path / "metdsl-homes"
    root.mkdir(mode=0o700, exist_ok=True)
    monkeypatch.setenv(WORKFLOW_HOMES_ROOT_ENV, str(root))
    yield root


@pytest.fixture(scope="session", autouse=True)
def _forbid_isolated_homes_in_operator_secret_root():
    """Fail any test about to resolve the isolated-homes root to the real `~/.met-dsl`."""
    import tools.orchestration_runtime as runtime
    from tools.hooks.common import operator_secret_root

    # Resolved ONCE, before any test can patch `$HOME` — see the module docstring.
    secret_root = operator_secret_root()
    original = runtime._workflow_homes_root

    def _guarded():
        root = original()
        try:
            resolved = Path(root).resolve()
        except (OSError, RuntimeError, ValueError):
            resolved = Path(root)
        if resolved == secret_root or secret_root in resolved.parents:
            raise AssertionError(
                f"a test resolved the isolated-homes root to {resolved}, inside the "
                "operator's real secret root. Nothing has been created — the guard runs "
                "before the directory would be. Keep "
                f"{runtime.WORKFLOW_HOMES_ROOT_ENV} pointed at a temporary directory "
                "(see tools/tests/conftest.py)."
            )
        return root

    # Marked so a test can ask whether the guard is installed rather than inferring it
    # from a function name. The witness for this guard must SKIP when run outside pytest,
    # where conftest is not loaded and the thing it tests does not exist.
    _guarded._metdsl_homes_guard_installed = True
    runtime._workflow_homes_root = _guarded
    try:
        yield
    finally:
        runtime._workflow_homes_root = original
