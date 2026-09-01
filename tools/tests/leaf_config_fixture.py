"""Seed a synthetic repo root with the repository's committed leaf configuration.

Every test that drives `record_launch` (or the diagnostician profile) for the
`claude` backend needs `leaf_config/claude/settings.json` present, because
`_prepare_claude_workflow_home` validates and SHA-pins it before any launch and
fails closed when it is absent.

Driven by the REAL committed file rather than a hand-written copy: a fixture that
invented its own settings would keep passing after the committed file drifted,
which is the failure mode the leaf-config probe exists to catch in the first place.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LEAF_CONFIG_REL = Path("leaf_config") / "claude" / "settings.json"
# The LEAF-owned codex hook source since issue #102. `.codex/hooks.json` is the DEV
# layer now and is not what a leaf launch validates.
CODEX_HOOKS_REL = Path("leaf_config") / "codex" / "hooks.json"


def seed_claude_leaf_config(repo_root: Path) -> Path:
    """Copy this repository's committed leaf settings into `repo_root`."""
    destination = Path(repo_root) / LEAF_CONFIG_REL
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes((REPO_ROOT / LEAF_CONFIG_REL).read_bytes())
    return destination


def seed_codex_hooks(repo_root: Path) -> Path:
    """Copy this repository's committed Codex hook source into `repo_root`.

    The codex twin of the above, for the same reason: `_prepare_codex_workflow_home`
    validates and SHA-pins `leaf_config/codex/hooks.json` before a codex launch and
    fails closed
    when it is absent. Fixtures needed it only once the isolation branch started
    keying on the family the PROFILE resolves — before that, a launch whose response
    omitted `backend` silently skipped isolation on both backends.
    """
    destination = Path(repo_root) / CODEX_HOOKS_REL
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes((REPO_ROOT / CODEX_HOOKS_REL).read_bytes())
    return destination


# --------------------------------------------------------------------------------------
# Isolated-homes redirect for test MODULES, not only for pytest.
#
# `tools/tests/conftest.py` points `METFORGE_WORKFLOW_HOMES_ROOT` at each test's `tmp_path`
# and raises if a prepared home lands in the operator's real `~/.met-forge`. Neither half
# is loaded by plain `unittest`, so any module that prepares a backend home writes into
# the operator's durable tree when run that way — and this branch's own commit messages
# prescribe `env -u METFORGE_WORKFLOW_HOMES_ROOT python3 -m unittest …` as the way to check
# the production resolution. Two reviewers had to prune entries out of the real tree
# afterwards.
#
# A per-CLASS `setUp` was the first fix and covered one class of the several that prepare
# homes. This is per MODULE, so a class added later is covered without anyone remembering
# to. It OVERWRITES whatever is in the environment and restores it in the teardown — an
# earlier version of this comment said it "defers to a value already in the environment",
# which it does not. Under pytest that costs nothing: conftest's autouse fixture sets its
# own per-test root for each test AFTER this has run, and the finer-grained value is the
# one every test actually sees.
#
# WITNESSED FROM OUTSIDE THE PROCESS, because it cannot be witnessed from inside: under
# pytest conftest redirects anyway, so a mutant deleting this changes nothing that the
# suite can see. `test_a_module_run_outside_pytest_writes_nothing_into_the_home` runs a
# dependent class under plain `unittest` in a subprocess with a fake `$HOME` and asserts
# that `.met-forge/homes` never appears there.
_MODULE_HOMES_REDIRECTS: dict[str, tuple] = {}


def isolated_homes_per_test_suite(tests):
    """`load_tests` wrapper giving each test its own isolated-homes root.

    The module-level redirect below makes the operator's tree SAFE outside pytest; it
    does not make the module PASS there, because one root shared by a whole module
    collides on the exclusive `os.mkdir` wherever tests reuse a fixed orchestration id —
    and this repository's fixtures do, heavily (`orch_001`, `orch_to_001`, `o`). Measured:
    `python3 -m unittest tools.tests.test_orchestration_runtime` went from 3 errors to 93
    when the module redirect was introduced, all of them that collision. Under pytest the
    conftest fixture already gives each test its own root, which is why none of this is
    visible there.

    `load_tests` is unittest's own hook for exactly this: the suite is wrapped so the
    redirect is applied around EACH test, matching what conftest does. Pair it with the
    module-level redirect, which still covers anything that runs outside a test (module
    import, class-level setup).
    """
    import os
    import tempfile
    import unittest
    from tools.orchestration_runtime import WORKFLOW_HOMES_ROOT_ENV

    class _PerTestHomesRoot(unittest.TestSuite):
        def run(self, result, debug=False):  # noqa: D102 - unittest protocol
            for test in self:
                if result.shouldStop:
                    break
                with tempfile.TemporaryDirectory(prefix="metdsl-test-homes-") as td:
                    root = Path(td) / "homes"
                    root.mkdir(mode=0o700)
                    previous = os.environ.get(WORKFLOW_HOMES_ROOT_ENV)
                    os.environ[WORKFLOW_HOMES_ROOT_ENV] = str(root)
                    try:
                        test(result)
                    finally:
                        if previous is None:
                            os.environ.pop(WORKFLOW_HOMES_ROOT_ENV, None)
                        else:
                            os.environ[WORKFLOW_HOMES_ROOT_ENV] = previous
            return result

    flat = unittest.TestSuite()
    def _flatten(suite):
        for item in suite:
            if isinstance(item, unittest.TestSuite):
                _flatten(item)
            else:
                flat.addTest(item)
    _flatten(tests)
    return _PerTestHomesRoot(flat)


def redirect_isolated_homes_root_for_module(module_name: str) -> None:
    """Call from a module's `setUpModule`; pair with the restore below."""
    import os
    import tempfile
    from tools.orchestration_runtime import WORKFLOW_HOMES_ROOT_ENV

    tmp = tempfile.TemporaryDirectory(prefix="metdsl-test-homes-")
    root = Path(tmp.name) / "homes"
    root.mkdir(mode=0o700)
    _MODULE_HOMES_REDIRECTS[module_name] = (
        tmp, os.environ.get(WORKFLOW_HOMES_ROOT_ENV))
    os.environ[WORKFLOW_HOMES_ROOT_ENV] = str(root)


def restore_isolated_homes_root_for_module(module_name: str) -> None:
    """Call from a module's `tearDownModule`."""
    import os
    from tools.orchestration_runtime import WORKFLOW_HOMES_ROOT_ENV

    entry = _MODULE_HOMES_REDIRECTS.pop(module_name, None)
    if entry is None:
        return
    tmp, previous = entry
    if previous is None:
        os.environ.pop(WORKFLOW_HOMES_ROOT_ENV, None)
    else:
        os.environ[WORKFLOW_HOMES_ROOT_ENV] = previous
    tmp.cleanup()
