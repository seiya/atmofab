"""Test-session hygiene for the OPERATOR-PRIVATE ROOT (`~/.atmofab`).

`_prepare_claude_workflow_home` (and its codex twin) create a private per-orchestration
home and nothing removes it. Since issue #64 that home is DURABLE — `<homes-root>/<oid>/
<backend>` under `~/.atmofab/homes` — because it holds the leaf's only conversation
record, and losing it makes a billed run unauditable after the fact. Retention is manual:
`tools/prune_workflow_homes.py`, never anything automatic.

A TEST RUN must not participate in that. Every fixture that drives `record_launch` for a
claude-shaped leaf prepares a home, so a suite would otherwise write a few hundred
directories into the operator's real `~/.atmofab/homes` and leave them there — mixed in
with the homes of real runs, where the prune tool would find them unverifiable (their
"owner" checkouts are temporary directories that no longer exist).

THREE SUBTREES, and the homes were only ever one of them (issue #133). This repository
writes `homes/`, `operator_tokens/` and `start_claims/` under `~/.atmofab`, and until
this file covered all three the other two went into the operator's real root on every
run: measured at `e0bae3d`, `tools/tests/test_orchestration_runtime.py` alone left 249
files in `~/.atmofab/operator_tokens/`, and `start_claims/` held 40. The guard built to
stop exactly that covered the homes and nothing else — which is why the resolvers are
named below rather than the trees.

TWO LAYERS, and the second is the one that actually holds:

  1. REDIRECT. A function-scoped autouse fixture points `ATMOFAB_WORKFLOW_HOMES_ROOT`,
     `ATMOFAB_OPERATOR_TOKENS_ROOT` and `ATMOFAB_START_CLAIM_ROOT` at a per-test
     `tmp_path`, so all three land where pytest already cleans up. Per-TEST rather
     than per-session on purpose: the home path is deterministic now, so two tests using
     the same fixed orchestration id would collide on the exclusive `os.mkdir` under a
     shared root.
  2. ENFORCE, BEFORE THE FACT. A session-scoped guard wraps the three functions that
     decide WHERE each tree goes — `orchestration_runtime._workflow_homes_root`,
     `orchestration_runtime._operator_tokens_root` and
     `run_workflow._start_claims_root` — and raises if one is about to return something
     inside the operator's REAL `~/.atmofab`. The redirect is a default a test can undo
     (`patch.dict(os.environ, ..., clear=True)` without re-setting the name is one line
     away), and this turns "the suite wrote into the operator's tree" from something
     noticed weeks later into a failure at the call that did it.

     Guarding at the RESOLVER is also what makes this survive a fourth subtree: the
     resolvers are the only spelling of the location (issue #132), so a writer that
     appears later either goes through one of them or is caught by
     `test_the_dot_atmofab_constant_is_spelled_once` in
     `tools/tests/test_operator_private_root.py`.

     PREVENT, NOT DETECT, and the distinction was paid for: the first version of this
     guard wrapped the two PREPARERS and raised on the path they RETURNED, so by the time
     it fired the directory was already on disk and nothing removed it. A reviewer
     running one mutant that made `_workflow_homes_root` ignore the redirect left four
     real directories in the operator's `~/.atmofab/homes` — permanent, unverifiable
     residue in the one tree whose retention is manual. Wrapping the resolver means the
     mutant that reaches past the redirect cannot create anything at all.

     The rejected alternative for the two subtrees added later was to WATCH the real
     root — a per-test mtime scan, or a before/after set difference. That is the same
     answer the paragraph below rejects for the homes, for the same reason: it sweeps up
     a run started WHILE the suite is running, and it notices after the file exists.

     "REAL" is load-bearing: the root is resolved ONCE, from the environment as the
     session starts, and NOT re-derived inside the wrapper. Several tests patch `$HOME`
     to a temporary directory precisely in order to exercise the default
     `operator_secret_root()/<subtree>` resolution; re-deriving would make the guard follow
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

Measured on `165c26f`, whole suite, one variable at a time unless noted. **That commit
predates the `met-dsl` -> `atmofab` rename (issue #127), so to RE-TAKE any figure below
spell the names `METDSL_*` there — `ATMOFAB_*` matches nothing at `165c26f` and every
row comes back 0.** The names are written in the current spelling because the record
is about which names the tree reads, not about that commit's text:

  clean                                              5280 passed / 114s
  ATMOFAB_ORCHESTRATION_ID + ATMOFAB_CHILD_AGENT_RUN_ID    9 failed   (the pair issue #84 named)
  every `ATMOFAB_*` name found in the tree, plus
    CODEX_HOME and CLAUDE_CONFIG_DIR, together         181 failed / 356s

Attributed on `tools/tests/test_orchestration_runtime.py` alone (1242 tests, 20s clean):
`ATMOFAB_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT=1` 84 failed **and 482s**, because the tests
then run the real probes; `CODEX_HOME` 10; `ATMOFAB_HOME` 3; `ATMOFAB_ENFORCE_REPLY_BUDGET`
1; every other name measured that way 0. So the pair in the issue was a small part of the
surface, and the expensive member was not in it.

NO COUNT of those names appears here or anywhere else in the code, deliberately — and the
sentence that replaced the first count was itself wrong, which is the argument. This
paragraph said "the 17 `ATMOFAB_*` names the tree reads" for four commits; its replacement
credited the constant-resolving reader with 21, the figure that reader returns with its
constant resolution REMOVED (it returns 27). Reviewers counting literals got 23 and 25.
Every one of those is a right answer to a different question about which files and which
spellings count, which is why the code states none of them:
`test_every_environment_name_the_tree_reads_is_stripped_or_declared` asks the rule about
whatever the tree currently reads.

`pytest_configure` removes those names from `os.environ` before collection — before
collection, because a module body that reads the environment at import runs earlier than
any fixture. A test that wants one of them sets it itself (`patch.dict`), which is what
every test already does.

BY PREFIX, not by list: the names are taken from
`orchestration_runtime.LEAF_ENV_ALLOWED_PREFIXES` — the same constant that decides which
host names reach a leaf — so a `ATMOFAB_*` knob added later is neutralized without anyone
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

from tools.tests import suite_env_guard

# The rule, the declared table and the record all live in `suite_env_guard`, a PLAIN
# module. Not here: `tools/tests` has no `__init__.py`, so pytest imports THIS file as
# module `conftest` while a test's `from tools.tests.conftest import ...` re-executes it as
# a second module object — the hook would populate one copy's record and the witness read
# the other's, empty forever. Measured, and it had already made the witness's primary
# assertion vacuous. A module reached only by its dotted name is imported once.


def pytest_addoption(parser) -> None:
    parser.addoption(
        "--keep-operator-env", action="store_true", default=False,
        help="do not strip the operator's ATMOFAB_* / CODEX_HOME / CLAUDE_CONFIG_DIR "
             "from the environment (issue #84). For deliberately running the suite "
             "against a knob you have set; expect failures that belong to the knob.")


def pytest_configure(config) -> None:
    """Remove the operator's per-run knobs before anything is collected.

    Before COLLECTION, not in a fixture: a module body that reads the environment at import
    runs earlier than any fixture.

    The removal is REPORTED. A knob discarded in silence is a check recorded as run and not
    run — `ATMOFAB_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT=1` is the sharp case, worth 84
    failures and 482s of real probing on `165c26f`, and an operator who sets it now gets
    1242 passed in 49s with nothing probed. `--keep-operator-env` is the way to mean it.
    """
    if config.getoption("--keep-operator-env"):
        suite_env_guard.decline_strip()
        return
    stripped = suite_env_guard.strip_operator_env(os.environ)
    if stripped:
        config._atmofab_stripped_operator_env = stripped


def _operator_env_disclosure(config) -> str | None:
    """What this run did to the operator's environment, or None if it did nothing."""
    if suite_env_guard.DECLINED:
        return ("atmofab: --keep-operator-env -- the operator's environment was NOT "
                "stripped for this run (issue #84); failures may belong to a knob you set")
    stripped = getattr(config, "_atmofab_stripped_operator_env", None)
    if not stripped:
        return None
    return ("atmofab: stripped the operator's environment for this run (issue #84): "
            + ", ".join(stripped)
            + " -- pass --keep-operator-env to run against them instead")


def pytest_report_header(config) -> str | None:
    return _operator_env_disclosure(config)


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    """The same line again at the end, because the header does not survive `-q`.

    `pytest_report_header` prints in the session preamble, which `-q` suppresses — and
    `-q` is the ONLY form this repository documents (`README.md`, and two skills under
    `.claude/`). So the disclosure added in round 1, on the ground that a silent strip is
    a check recorded as run and not run, was invisible in every invocation anyone is told
    to use, with its own witness green because that witness ran pytest without `-q`.

    A terminal-summary line survives `-q` (measured). Printed in ADDITION to the header
    rather than instead of it: under a full run both appear, which costs one duplicated
    line and means no invocation loses it.
    """
    message = _operator_env_disclosure(config)
    if message:
        terminalreporter.write_line(message)


def pytest_unconfigure(config) -> None:
    suite_env_guard.restore_operator_env(os.environ)


@pytest.fixture(autouse=True)
def _redirect_operator_private_roots(tmp_path, monkeypatch):
    """Point every tree this test writes under `~/.atmofab` into `tmp_path`.

    All THREE subtrees, not just the homes: the isolated backend homes, the operator
    token store, and the start-claim locks. Only the first was redirected until issue
    #133, and the other two were writing into the operator's real root the whole time
    (measured at `e0bae3d`: `test_orchestration_runtime.py` alone left 249 files in
    `~/.atmofab/operator_tokens/`, and `~/.atmofab/start_claims/` held 40).

    Per-TEST rather than per-session on purpose: the home path is deterministic now, so
    two tests using the same fixed orchestration id would collide on the exclusive
    `os.mkdir` under a shared root.
    """
    from tools.tests.leaf_config_fixture import _private_root_redirects

    roots = {}
    for env_name, subdir in _private_root_redirects():
        root = tmp_path / f"atmofab-{subdir}"
        root.mkdir(mode=0o700, exist_ok=True)
        monkeypatch.setenv(env_name, str(root))
        roots[env_name] = root
    yield roots


@pytest.fixture(scope="session", autouse=True)
def _forbid_anything_in_operator_secret_root():
    """Fail any test about to resolve one of the three roots to the real `~/.atmofab`."""
    import tools.orchestration_runtime as runtime
    from tools import run_workflow
    from tools.hooks.common import operator_secret_root

    # Resolved ONCE, before any test can patch `$HOME` — see the module docstring.
    secret_root = operator_secret_root()

    def _guarded(original, label, env_name):
        def _wrapper():
            root = original()
            try:
                resolved = Path(root).resolve()
            except (OSError, RuntimeError, ValueError):
                resolved = Path(root)
            if resolved == secret_root or secret_root in resolved.parents:
                raise AssertionError(
                    f"a test resolved the {label} to {resolved}, inside the "
                    "operator's real secret root. Nothing has been created — the guard "
                    "runs before the directory would be. Keep "
                    f"{env_name} pointed at a temporary directory "
                    "(see tools/tests/conftest.py)."
                )
            return root

        # Marked so a test can ask whether the guard is installed rather than inferring
        # it from a function name. ONE spelling for all three, so a witness cannot ask
        # about a marker that exists only on the resolver it happens to name. The
        # witnesses must SKIP when run outside pytest, where conftest is not loaded and
        # the thing they test does not exist.
        _wrapper._atmofab_private_root_guard_installed = True
        return _wrapper

    installed = [
        (runtime, "_workflow_homes_root", "isolated-homes root",
         runtime.WORKFLOW_HOMES_ROOT_ENV),
        (runtime, "_operator_tokens_root", "operator token store",
         runtime.OPERATOR_TOKENS_ROOT_ENV),
        (run_workflow, "_start_claims_root", "start-claim root",
         run_workflow.START_CLAIMS_ROOT_ENV),
    ]
    originals = [(module, attr, getattr(module, attr))
                 for module, attr, _label, _env in installed]
    for (module, attr, label, env_name), (_m, _a, original) in zip(installed, originals):
        setattr(module, attr, _guarded(original, label, env_name))
    try:
        yield
    finally:
        for module, attr, original in originals:
            setattr(module, attr, original)
