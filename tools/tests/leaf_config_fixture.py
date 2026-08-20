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
CODEX_HOOKS_REL = Path(".codex") / "hooks.json"


def seed_claude_leaf_config(repo_root: Path) -> Path:
    """Copy this repository's committed leaf settings into `repo_root`."""
    destination = Path(repo_root) / LEAF_CONFIG_REL
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes((REPO_ROOT / LEAF_CONFIG_REL).read_bytes())
    return destination


def seed_codex_hooks(repo_root: Path) -> Path:
    """Copy this repository's committed Codex hook source into `repo_root`.

    The codex twin of the above, for the same reason: `_prepare_codex_workflow_home`
    validates and SHA-pins `.codex/hooks.json` before a codex launch and fails closed
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
# `tools/tests/conftest.py` points `METDSL_WORKFLOW_HOMES_ROOT` at each test's `tmp_path`
# and raises if a prepared home lands in the operator's real `~/.met-dsl`. Neither half
# is loaded by plain `unittest`, so any module that prepares a backend home writes into
# the operator's durable tree when run that way — and this branch's own commit messages
# prescribe `env -u METDSL_WORKFLOW_HOMES_ROOT python3 -m unittest …` as the way to check
# the production resolution. Two reviewers had to prune entries out of the real tree
# afterwards.
#
# A per-CLASS `setUp` was the first fix and covered one class of the several that prepare
# homes. This is per MODULE, so a class added later is covered without anyone remembering
# to. It defers to a value already in the environment — under pytest the conftest fixture
# sets its own per-test root afterwards, which is finer-grained and wins.
_MODULE_HOMES_REDIRECTS: dict[str, tuple] = {}


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
