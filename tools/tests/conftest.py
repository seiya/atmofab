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

THE SECOND SUBJECT: the operator's ambient environment (issue #84).

Several dozen tests decide what they are asserting by READING `os.environ` — whether the
server is "under a workflow", which configuration home a backend resolves, whether
preflight probes for real. Every one of those names is a PER-RUN knob that
`tools/run_workflow.py` sets in the node environment, so the shell most likely to have
them exported is a shell used for this repository's own work. A suite that inherits them
answers a question about the machine instead of about the code, and the cost is a
reviewer's round: on PR #81 a reviewer reported 152 failures as a branch regression when
143 were the branch's and the rest were this.

Measured on `165c26f`, whole suite, one variable at a time unless noted:

  clean                                              5280 passed / 114s
  METDSL_ORCHESTRATION_ID + METDSL_CHILD_AGENT_RUN_ID    9 failed   (the pair issue #84 named)
  the 17 `METDSL_*` names the tree reads + CODEX_HOME
    + CLAUDE_CONFIG_DIR, together                      181 failed / 356s

Attributed on `tools/tests/test_orchestration_runtime.py` alone (1242 tests, 20s clean):
`METDSL_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT=1` 84 failed **and 482s**, because the tests
then run the real probes; `CODEX_HOME` 10; `METDSL_HOME` 3; `METDSL_ENFORCE_REPLY_BUDGET`
1; the other ten names 0. So the pair in the issue was 5% of the surface, and the
expensive member was not in it.

`pytest_configure` removes those names from `os.environ` before collection — before
collection, because a module body that reads the environment at import runs earlier than
any fixture. A test that wants one of them sets it itself (`patch.dict`), which is what
every test already does.

BY PREFIX, not by list: the names are taken from
`orchestration_runtime.LEAF_ENV_ALLOWED_PREFIXES` — the same constant that decides which
host names reach a leaf — so a `METDSL_*` knob added later is neutralized without anyone
remembering this file. The two exact names beside it (`CODEX_HOME`, `CLAUDE_CONFIG_DIR`)
are the backend configuration homes, which carry no such prefix; `CODEX_HOME` is the one
measured above, and `CLAUDE_CONFIG_DIR` is its twin, included by symmetry rather than by
measurement (it cost 0 failures on `165c26f`).

What this does NOT do, stated so it is not read as more: it neutralizes the names above
and nothing else. `PATH`, `HOME` and the locale family are still inherited, and a suite
run under `env -i` is not covered — that is a different and much larger claim than the one
measured here.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

# The backend configuration homes, which carry no `METDSL_` prefix. See the docstring.
_BACKEND_CONFIG_HOME_ENV = ("CODEX_HOME", "CLAUDE_CONFIG_DIR")

# Populated by `pytest_configure`; a witness reads it rather than inferring the guard from
# a side effect. Name -> the value the operator had exported.
STRIPPED_OPERATOR_ENV: dict[str, str] = {}


def operator_env_names_to_strip(environ) -> list[str]:
    """The ambient names a test must not be able to inherit.

    Defined here ONCE and read by both `pytest_configure` and its witness, so the witness
    cannot drift into pinning a copy of the rule.

    The candidate names are snapshotted BEFORE the import, so that the import cannot read
    a name this function is about to declare strippable. Nothing in
    `orchestration_runtime` reads the environment at module level today; taking the
    snapshot first is what keeps that from becoming load-bearing.
    """
    present = list(environ)
    from tools.orchestration_runtime import LEAF_ENV_ALLOWED_PREFIXES

    names = {n for n in present if n.startswith(tuple(LEAF_ENV_ALLOWED_PREFIXES))}
    names.update(n for n in _BACKEND_CONFIG_HOME_ENV if n in present)
    return sorted(names)


# Names the SUITE sets on purpose, and who sets them. NOT exemptions from the guard — the
# guard is about the OPERATOR's environment, and these are set after it has run. Both are
# process-global, so both are visible to every test collected afterwards; that is recorded
# as a rough edge in TODO.md rather than fixed.
SUITE_OWNED_ENV = {
    "METDSL_WORKFLOW_HOMES_ROOT":
        "the `_redirect_workflow_homes_root` fixture below, per test",
    "METDSL_DEP_READINESS_ALLOW_PERSISTED_FALLBACK":
        "a module-level `os.environ.setdefault` in test_orchestration_runtime.py and the "
        "three test_pure_leaf_* modules, so it appears once any of them is imported",
}


def undeclared_operator_env_names(environ) -> set[str]:
    """Strippable names present that nobody has claimed — the ratchet.

    Either the guard stopped stripping, or the suite grew a new process-global environment
    dependence without saying who sets it. Lives HERE, beside the rule and the table it
    compares against, so that a mutation to it is a mutation to a mechanism rather than to
    an assertion inside a test — the version that lived in the witness survived a sweep.
    """
    return set(operator_env_names_to_strip(environ)) - set(SUITE_OWNED_ENV)


def pytest_configure(config) -> None:
    """Remove the operator's per-run knobs before anything is collected."""
    for name in operator_env_names_to_strip(os.environ):
        STRIPPED_OPERATOR_ENV[name] = os.environ.pop(name)


def pytest_unconfigure(config) -> None:
    """Put them back, so an in-process pytest invocation leaves the caller's env alone."""
    os.environ.update(STRIPPED_OPERATOR_ENV)
    STRIPPED_OPERATOR_ENV.clear()


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
