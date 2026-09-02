#!/usr/bin/env python3
"""The suite's own operator-private-root layers, observed from OUTSIDE them.

`tools/tests/conftest.py` has two layers over `~/.atmofab` (issues #132 / #133): a
per-test REDIRECT of the three environment names, and a session GUARD wrapping the three
resolvers. Nothing under pytest observed either until this file existed, and the first
attempt to fix that observed only half:

  * the two guard WITNESSES elsewhere SKIP when the marker is absent. They have to — under
    plain `unittest` there is no fixture to observe — so deleting the guard makes them
    silently skip rather than fail, which is the shape a safety net fails in;
  * the REDIRECT's own witness was first written into
    `tools/tests/test_operator_private_root.py`, which installs the MODULE-level redirect
    from `leaf_config_fixture` in its `setUpModule`. That layer satisfied the assertion, so
    narrowing conftest's fixture back to homes-only left it green. A round-1 reviewer
    measured it: with `_private_root_redirects()[:1]` in conftest, the file still reported
    14 passed. The blind spot had moved, not closed.

So this file exists to carry NO redirect of its own — no `setUpModule`, no `load_tests`, no
override set anywhere in it. That absence IS the fixture; adding either would make these
assertions observe the wrong layer again. Its own tests write nothing under `~/.atmofab`,
so it needs no protection of its own.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path

import tools.hooks.common as hooks_common
import tools.orchestration_runtime as ort
from tools import run_workflow


class SuiteHarnessCoversAllThreeRootsTests(unittest.TestCase):
    """Both conftest layers, asserted directly and only where they exist."""

    def setUp(self) -> None:
        if not os.environ.get("PYTEST_CURRENT_TEST"):
            self.skipTest("the suite's operator-private-root guard is not installed")

    def _resolvers(self):
        return (
            ("isolated-homes root", ort._workflow_homes_root),
            ("operator token store", ort._operator_tokens_root),
            ("start-claim root", run_workflow._start_claims_root),
        )

    def test_every_operator_private_resolver_is_guarded(self) -> None:
        for label, resolver in self._resolvers():
            self.assertTrue(
                getattr(resolver, "_atmofab_private_root_guard_installed", False),
                f"the session guard does not wrap the {label} resolver — a test can "
                "resolve it into the operator's real ~/.atmofab and nothing will say so "
                "(tools/tests/conftest.py)")

    def test_every_operator_private_root_is_redirected_away_from_the_real_one(self) -> None:
        """The redirect, asserted where it acts rather than by reading the fixture.

        This is the assertion the module docstring is about: it is only worth anything in
        a file that installs no redirect of its own.
        """
        secret_root = hooks_common.operator_secret_root()
        for label, resolver in self._resolvers():
            resolved = Path(resolver()).resolve()
            self.assertFalse(
                resolved == secret_root or secret_root in resolved.parents,
                f"this test's {label} resolves to {resolved}, inside the operator's real "
                "secret root — the per-test redirect in tools/tests/conftest.py does not "
                "cover it")

    def test_each_redirected_name_is_actually_set_for_this_test(self) -> None:
        """The environment, not only where the resolvers land.

        The assertion above is satisfied by anything that keeps the resolvers out of the
        real root — including a resolver that has stopped reading its override at all. This
        one names the three environment variables, so narrowing conftest's fixture to a
        subset fails HERE with the missing name rather than somewhere downstream.
        """
        from tools.tests.leaf_config_fixture import _private_root_redirects

        for env_name, subdir in _private_root_redirects():
            value = os.environ.get(env_name, "")
            self.assertTrue(
                value.strip(),
                f"{env_name} (the {subdir} tree) is not set for this test — "
                "tools/tests/conftest.py's per-test redirect does not cover it")


if __name__ == "__main__":  # pragma: no cover - manual runs
    unittest.main()
