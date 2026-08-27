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

import ast
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from tools.tests.suite_env_guard import (
    BACKEND_CONFIG_HOME_ENV,
    STRIPPED_OPERATOR_ENV,
    SUITE_OWNED_ENV,
    operator_env_names_to_strip,
    undeclared_operator_env_names,
)
import tools.tests.suite_env_guard as suite_env_guard

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The regression issue #84 opens with: under the workflow the MCP server refuses a
# caller-named `repo_root`, and this test asserts the OUTSIDE-a-run branch. An operator
# with `METDSL_ORCHESTRATION_ID` exported got a failure belonging to no change.
_SUBJECT_MODULE = "tools.tests.test_build_runtime_server"
_SUBJECT_CLASS = "OrchestratedEnvAllowlistTests"
_SUBJECT_TEST = "test_only_an_absent_repo_root_falls_back_to_project_dir"

def _run(argv: list[str], env_extra: dict[str, str]) -> subprocess.CompletedProcess:
    """A subprocess whose environment this test states in full, on the axis under test.

    The base is the parent's environment with every strippable name REMOVED before
    `env_extra` is applied. Inheriting them instead would make the clean control depend on
    the parent's own environment — which is exactly the coupling this file exists to close,
    and it showed up as a failing control the first time the suite was run with
    `--keep-operator-env`.
    """
    env = {k: v for k, v in os.environ.items()
           if k not in set(operator_env_names_to_strip(os.environ))}
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
           declared in conftest's `SUITE_OWNED_ENV`. That is a ratchet: a new process-global environment dependence
           cannot appear without someone naming it here and saying who sets it.

        Subset, not equality: `SUITE_OWNED_ENV` holds names set at another module's
        import, so which of them are present depends on what has been collected. Running
        this file alone shows one; running the whole suite shows both.
        """
        if "pytest" not in sys.modules:
            # The subject is a pytest hook. Under plain `unittest` there is no conftest
            # and nothing to observe; asserting anyway would fail for the absence of the
            # harness rather than for a defect.
            self.skipTest("the suite's environment guard is not installed")
        # Under pytest the flag is an ASSERTION, not a skip condition. Skipping on it
        # instead let "the hook never set CONFIGURED" pass as "not applicable" — measured,
        # that mutant survived a sweep by turning this test into a skip. Read from the
        # module the hook WRITES TO, so a reader that has ended up with a second copy of
        # the rule fails here rather than asserting over an empty record.
        self.assertTrue(suite_env_guard.CONFIGURED,
                        "the conftest hook did not run, or wrote to a different module")

        for name, operator_value in STRIPPED_OPERATOR_ENV.items():
            with self.subTest(name=name):
                self.assertNotEqual(
                    os.environ.get(name), operator_value,
                    f"{name} reached a test with the value the operator exported")

        undeclared = undeclared_operator_env_names(os.environ)
        self.assertEqual(
            undeclared, set(),
            "an environment name is set during a test and nobody says who sets it. If "
            "the suite sets it, declare it in SUITE_OWNED_ENV with the site that does; "
            "if it leaked from the operator, the guard in tools/tests/conftest.py is "
            f"broken. Values: {({n: os.environ[n] for n in undeclared})}")

    def test_the_ratchet_reports_a_name_nobody_has_claimed(self) -> None:
        """The ratchet's own witness — it fires on nothing this suite produces.

        Without this, `undeclared_operator_env_names` returning a constant empty set is
        invisible: no test sets an undeclared name, so the check above passes either way.
        Driven on a synthetic mapping, since the real one is the thing being ratcheted.
        """
        declared = dict.fromkeys(SUITE_OWNED_ENV, "x")
        self.assertEqual(undeclared_operator_env_names(declared), set())
        self.assertEqual(
            undeclared_operator_env_names({**declared, "METDSL_A_NEW_KNOB": "1"}),
            {"METDSL_A_NEW_KNOB"})
        self.assertEqual(
            undeclared_operator_env_names({**declared, "CODEX_HOME": "/tmp/x"}),
            {"CODEX_HOME"})

    def test_the_strip_removes_the_name_and_records_what_it_removed(self) -> None:
        """Both halves, because a mutant that does one and not the other survives.

        Dropping the record while still popping leaves the environment correct and the
        WITNESS above vacuous — the same shape as the module split this file was written
        after, reached a second way. Driven on a synthetic mapping: on a clean host the
        real strip removes nothing, so there is nothing for an in-process assertion to see.
        """
        record = dict(suite_env_guard.STRIPPED_OPERATOR_ENV)
        self.addCleanup(
            lambda: (suite_env_guard.STRIPPED_OPERATOR_ENV.clear(),
                     suite_env_guard.STRIPPED_OPERATOR_ENV.update(record)))
        suite_env_guard.STRIPPED_OPERATOR_ENV.clear()

        environ = {"METDSL_A_KNOB": "1", "CODEX_HOME": "/tmp/x", "PATH": "/b"}
        removed = suite_env_guard.strip_operator_env(environ)
        self.assertEqual(removed, ["CODEX_HOME", "METDSL_A_KNOB"])
        self.assertEqual(environ, {"PATH": "/b"})
        self.assertEqual(suite_env_guard.STRIPPED_OPERATOR_ENV,
                         {"METDSL_A_KNOB": "1", "CODEX_HOME": "/tmp/x"})
        suite_env_guard.restore_operator_env(environ)
        self.assertEqual(environ, {"METDSL_A_KNOB": "1", "CODEX_HOME": "/tmp/x",
                                   "PATH": "/b"})
        self.assertEqual(suite_env_guard.STRIPPED_OPERATOR_ENV, {})

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
        self.assertEqual(BACKEND_CONFIG_HOME_ENV, ("CODEX_HOME", "CLAUDE_CONFIG_DIR"))
        sample = {name: "/tmp/x" for name in BACKEND_CONFIG_HOME_ENV}
        self.assertEqual(operator_env_names_to_strip(sample),
                         sorted(BACKEND_CONFIG_HOME_ENV))

    def test_the_prefix_rule_covers_every_name_the_tree_uses(self) -> None:
        """A property, in place of the count three documents used to state.

        Enumerated with `os.walk` and not with `grep`, which in an agent session may be a
        shell function running `ugrep --ignore-files` and therefore respects `.gitignore`.
        Every `METDSL_*` literal in non-test `tools/` and `mcp_servers/` must be a name the
        strip removes when it is present. A count would have to be re-taken every time a
        knob is added and was wrong in three places; this cannot go stale, because it asks
        the rule about whatever the tree currently names.
        """
        found = set()
        for root, dirs, files in os.walk(_REPO_ROOT):
            dirs[:] = [d for d in dirs
                       if d not in (".git", "__pycache__", "tests")
                       and not d.startswith("workspace")]
            rel = Path(root).relative_to(_REPO_ROOT)
            if rel.parts and rel.parts[0] not in ("tools", "mcp_servers"):
                continue
            for name in files:
                if not name.endswith(".py"):
                    continue
                text = (Path(root) / name).read_text(encoding="utf-8", errors="replace")
                found.update(re.findall(r"[\"']([A-Z]*METDSL_[A-Z0-9_]+)[\"']", text))
        self.assertTrue(found, "the enumeration found nothing — the walk is broken")
        sample = {name: "x" for name in found}
        self.assertEqual(set(operator_env_names_to_strip(sample)), found)

    def test_no_module_level_environment_read_defeats_the_guard(self) -> None:
        """The hook's import must not itself cache an operator value.

        `strip_operator_env` snapshots the candidate names, then imports
        `orchestration_runtime` to learn the prefix, then pops. A module-scope
        `os.environ` read anywhere in that import chain would read the operator's value
        BEFORE the pop and keep it for the whole session, with every test still green —
        the docstring asserted this could not happen and nothing checked it.

        Read by AST, over the modules the hook's own import reaches directly.
        """
        def module_scope_env_reads(source: str) -> list[int]:
            offenders = []
            for node in ast.parse(source).body:      # module scope only
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Attribute) and sub.attr in ("environ", "getenv"):
                        offenders.append(getattr(sub, "lineno", -1))
            return offenders

        # SELF-TEST FIRST. A negative assertion is green when its detector is broken, and
        # this one reports the empty list for any reader that fails to walk.
        self.assertEqual(module_scope_env_reads("X = os.environ.get('A')\n"), [1])
        self.assertEqual(module_scope_env_reads("X = os.getenv('A')\n"), [1])
        self.assertEqual(
            module_scope_env_reads("def f():\n    return os.environ['A']\n"), [],
            "a read inside a function is not a module-scope read")

        for rel in ("tools/orchestration_runtime.py", "tools/hooks/common.py"):
            found = module_scope_env_reads(
                (_REPO_ROOT / rel).read_text(encoding="utf-8"))
            self.assertEqual(found, [],
                             f"{rel} reads the environment at module scope, lines {found}")

    def test_the_strip_is_reported_and_can_be_declined(self) -> None:
        """Silence here is a check recorded as run and not run — so both are witnessed.

        `METDSL_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT=1` was 84 failures and 482s of real
        probing on `165c26f`; with the strip it is 1242 passed in 49s and nothing probed.
        An operator who set it deliberately has to be able to see that, and to say they
        meant it. Neither the header nor the flag had a witness; both mutants survived.
        """
        poison = {"METDSL_ORCHESTRATION_ID": "orch_header_witness"}
        node = f"tools/tests/{_SUBJECT_MODULE.rsplit('.', 1)[1]}.py" \
               f"::{_SUBJECT_CLASS}::{_SUBJECT_TEST}"

        reported = _run([sys.executable, "-m", "pytest", node], poison)
        self.assertIn("METDSL_ORCHESTRATION_ID", reported.stdout)
        self.assertIn("--keep-operator-env", reported.stdout)
        # The control: with nothing to strip there is no header to print.
        quiet = _run([sys.executable, "-m", "pytest", node], {})
        self.assertNotIn("stripped the operator's environment", quiet.stdout)

        # And the flag really declines the strip: the subject test asserts the OUTSIDE-a-run
        # branch, so keeping the name is what makes it fail.
        declined = _run(
            [sys.executable, "-m", "pytest", node, "--keep-operator-env"], poison)
        self.assertNotEqual(
            declined.returncode, 0,
            "--keep-operator-env did not leave the operator's value in place:\n"
            + declined.stdout)

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
