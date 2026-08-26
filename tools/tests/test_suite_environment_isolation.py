#!/usr/bin/env python3
"""The suite must give the same verdict under any operator environment (issue #84).

`tools/tests/conftest.py` removes the operator's per-run knobs from `os.environ` before
collection. That guard has the shape the conftest docstring warns about: nothing normally
trips it, so it can be deleted, inverted or quietly narrowed and every other test stays
green. Its sibling — the isolated-homes guard — was the only survivor of a reviewer's
18-mutant sweep for exactly that reason, and the answer there was a witness. This is that
witness for the environment half.

The end-to-end case carries a CONTROL that must fail. A subprocess that passes proves
nothing on its own: it cannot be told apart from a poisoning that was never poisonous. So
the same test is driven three ways — poisoned under pytest (passes, because the guard ran),
poisoned under plain `unittest` where no conftest is loaded (fails, which is what makes the
first result mean something), and clean under `unittest` (passes, which places the blame on
the environment rather than on the runner).
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from tools.tests.conftest import (
    _BACKEND_CONFIG_HOME_ENV,
    STRIPPED_OPERATOR_ENV,
    operator_env_names_to_strip,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The regression issue #84 opens with: under the workflow the MCP server refuses a
# caller-named `repo_root`, and this test asserts the OUTSIDE-a-run branch. An operator
# with `METDSL_ORCHESTRATION_ID` exported got a failure belonging to no change.
_SUBJECT_MODULE = "tools.tests.test_build_runtime_server"
_SUBJECT_CLASS = "OrchestratedEnvAllowlistTests"
_SUBJECT_TEST = "test_only_an_absent_repo_root_falls_back_to_project_dir"

# Names the SUITE sets on purpose, and who sets them. Not exemptions from the guard —
# the guard is about the OPERATOR's environment, and these are set after it has run.
# Both are process-global, so both are visible to every test collected afterwards; that
# is recorded as a rough edge in TODO.md rather than fixed here.
_SUITE_OWNED_ENV = {
    "METDSL_WORKFLOW_HOMES_ROOT":
        "the `_redirect_workflow_homes_root` fixture in tools/tests/conftest.py, per test",
    "METDSL_DEP_READINESS_ALLOW_PERSISTED_FALLBACK":
        "a module-level `os.environ.setdefault` in test_orchestration_runtime.py and the "
        "three test_pure_leaf_* modules, so it appears once any of them is imported",
}


def _run(argv: list[str], env_extra: dict[str, str]) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(env_extra)
    return subprocess.run(
        argv, cwd=_REPO_ROOT, env=env, capture_output=True, text=True, timeout=300)


class OperatorEnvironmentIsolationTests(unittest.TestCase):

    def test_no_ambient_operator_name_survives_into_a_running_test(self) -> None:
        """The guard's effect, observed from inside a test rather than inferred.

        TWO claims, because "no `METDSL_*` name is set during a test" is false and would
        be the wrong thing to pin:

        1. Nothing a test sees carries the OPERATOR's value. Vacuous on a clean host —
           there was nothing to strip — which is why the end-to-end case below drives a
           real poisoned process rather than relying on this one.
        2. Every `METDSL_*` name that IS set during a test is one the SUITE set, and is
           declared below. That is a ratchet: a new process-global environment dependence
           cannot appear without someone naming it here and saying who sets it.

        Subset, not equality: `_SUITE_OWNED_ENV` holds names set at another module's
        import, so which of them are present depends on what has been collected. Running
        this file alone shows one; running the whole suite shows both.
        """
        if "pytest" not in sys.modules:
            # The subject is a pytest hook. Under plain `unittest` there is no conftest
            # and nothing to observe; asserting anyway would fail for the absence of the
            # harness rather than for a defect.
            self.skipTest("the suite's environment guard is not installed")

        for name, operator_value in STRIPPED_OPERATOR_ENV.items():
            with self.subTest(name=name):
                self.assertNotEqual(
                    os.environ.get(name), operator_value,
                    f"{name} reached a test with the value the operator exported")

        survivors = set(operator_env_names_to_strip(os.environ))
        undeclared = survivors - set(_SUITE_OWNED_ENV)
        self.assertEqual(
            undeclared, set(),
            "an environment name is set during a test and nobody says who sets it. If "
            "the suite sets it, declare it in _SUITE_OWNED_ENV with the site that does; "
            "if it leaked from the operator, the guard in tools/tests/conftest.py is "
            f"broken. Values: {({n: os.environ[n] for n in undeclared})}")

    def test_the_rule_is_read_from_the_leaf_env_prefix_constant_not_copied(self) -> None:
        """Coupled by POINTER: the prefix is defined once, in the production constant.

        `orchestration_runtime.LEAF_ENV_ALLOWED_PREFIXES` is what decides which host names
        reach a leaf; a `METDSL_*` knob added later must be neutralized here without
        anyone remembering this file exists. Pinning the string `"METDSL_"` in a second
        place would state the rule twice and let the two drift, so what is pinned is that
        the function FOLLOWS the constant — moving the constant moves the answer.
        """
        import tools.orchestration_runtime as runtime

        sample = {"METDSL_ORCHESTRATION_ID": "x", "ZZTOP_KNOB": "y", "PATH": "/b"}
        self.assertEqual(operator_env_names_to_strip(sample), ["METDSL_ORCHESTRATION_ID"])
        with mock.patch.object(runtime, "LEAF_ENV_ALLOWED_PREFIXES", ("ZZTOP_",)):
            self.assertEqual(operator_env_names_to_strip(sample), ["ZZTOP_KNOB"])

    def test_the_backend_configuration_homes_are_covered_too(self) -> None:
        """`CODEX_HOME` carries no `METDSL_` prefix and cost 10 failures when exported.

        Measured on `165c26f` over `tools/tests/test_orchestration_runtime.py` alone.
        `CLAUDE_CONFIG_DIR` is its twin; it cost 0 in the same measurement and is covered
        by symmetry, so that a future test reading it cannot inherit the operator's.
        """
        self.assertEqual(_BACKEND_CONFIG_HOME_ENV, ("CODEX_HOME", "CLAUDE_CONFIG_DIR"))
        sample = {name: "/tmp/x" for name in _BACKEND_CONFIG_HOME_ENV}
        self.assertEqual(operator_env_names_to_strip(sample),
                         sorted(_BACKEND_CONFIG_HOME_ENV))

    def test_a_poisoned_environment_is_neutralized_end_to_end(self) -> None:
        """Through real processes, with the control that must fail.

        Nothing here is mocked: the guard is a pytest hook, so the only way to observe it
        is to start pytest. `unittest` loads no conftest, which makes it the control
        runner — the same code, the same poison, no guard.
        """
        poison = {"METDSL_ORCHESTRATION_ID": "orch_witness"}
        node = f"tools/tests/{_SUBJECT_MODULE.rsplit('.', 1)[1]}.py" \
               f"::{_SUBJECT_CLASS}::{_SUBJECT_TEST}"
        unittest_target = f"{_SUBJECT_MODULE}.{_SUBJECT_CLASS}.{_SUBJECT_TEST}"

        guarded = _run([sys.executable, "-m", "pytest", node, "-q"], poison)
        self.assertEqual(guarded.returncode, 0,
                         f"the guard did not neutralize the poison:\n{guarded.stdout}")

        # CONTROL 1 — the poison is real. Without the conftest the same test fails.
        control = _run([sys.executable, "-m", "unittest", unittest_target], {})
        self.assertEqual(control.returncode, 0,
                         f"the control runner cannot run the subject at all:\n"
                         f"{control.stderr}")
        poisoned = _run([sys.executable, "-m", "unittest", unittest_target], poison)
        self.assertNotEqual(
            poisoned.returncode, 0,
            "the control did not fail, so the guarded run proves nothing — the subject "
            "test no longer reads the ambient environment and this witness needs a new "
            f"subject:\n{poisoned.stderr}")


if __name__ == "__main__":
    unittest.main()
