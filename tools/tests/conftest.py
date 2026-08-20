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
  2. ENFORCE. A session-scoped guard wraps both preparers and RAISES if the home that
     came back is under the operator's REAL secret root. The redirect is a default a test
     can undo — `patch.dict(os.environ, ..., clear=True)` without re-setting the name is
     one line away — and this turns "the suite wrote into the operator's tree" from
     something noticed weeks later into a failure at the call that did it.

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
    """Fail any test whose prepared home landed in the real `~/.met-dsl`."""
    import tools.orchestration_runtime as runtime
    from tools.hooks.common import operator_secret_root

    # Resolved ONCE, before any test can patch `$HOME` — see the module docstring.
    secret_root = operator_secret_root()
    originals = {}
    for name in ("_prepare_claude_workflow_home", "_prepare_codex_workflow_home"):
        original = getattr(runtime, name)
        originals[name] = original

        def _wrapper(*args, _original=original, _name=name, **kwargs):
            isolation = _original(*args, **kwargs)
            home = isolation.get("home") if isinstance(isolation, dict) else None
            if isinstance(home, str) and home.strip():
                # Resolved on BOTH sides: `operator_secret_root` resolves, and a home
                # reached through a symlinked $HOME would otherwise compare unequal.
                try:
                    resolved = Path(home).resolve()
                except (OSError, RuntimeError, ValueError):
                    resolved = Path(home)
                if resolved == secret_root or secret_root in resolved.parents:
                    raise AssertionError(
                        f"{_name} created a home inside the operator's secret root: "
                        f"{resolved}. The suite must keep "
                        f"{runtime.WORKFLOW_HOMES_ROOT_ENV} pointed at a temporary "
                        "directory (see tools/tests/conftest.py)."
                    )
            return isolation

        setattr(runtime, name, _wrapper)
    try:
        yield
    finally:
        for name, original in originals.items():
            setattr(runtime, name, original)

