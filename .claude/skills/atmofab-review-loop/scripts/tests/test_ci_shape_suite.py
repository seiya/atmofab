#!/usr/bin/env python3
"""Tests for `ci_shape_suite`: the PATH, the environment, the hidden set and the exit verdict.

The script's verdict is "green without the skip-probe dependencies and the operator's home".
Its dangerous direction is exit 0 where CI is red: a PATH that still reaches a hidden
executable, the operator's HOME or a leftover `PYTEST_ADDOPTS` / `PYTEST_PLUGINS` reaching the
run, a subset run counted as the whole, or a failed suite or skip check folded into 0 — at the
function and at the call site (`RunWiringTests` drives `_run` with a stub runner). The false-alarm direction is a PATH that loses a tool CI
does have (the linters installed beside a CLI).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parents[3]
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO))

import ci_shape_suite


def _exe(path: Path) -> None:
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)


class CiPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.mixed = self.root / "mixed"
        self.plain = self.root / "plain"
        self.shim = self.root / "shim"
        for d in (self.mixed, self.plain, self.shim):
            d.mkdir()
        for name in ("claude", "ruff", "fortitude"):
            _exe(self.mixed / name)
        (self.mixed / "notes.txt").write_text("not executable\n")
        _exe(self.plain / "gfortran")
        _exe(self.plain / "ruff")

    def _which(self, path: str, name: str) -> str | None:
        return shutil.which(name, path=path)

    def test_hidden_name_is_unreachable_and_siblings_stay(self) -> None:
        path = ci_shape_suite.ci_path(f"{self.mixed}{os.pathsep}{self.plain}", {"claude"}, self.shim)
        self.assertIsNone(self._which(path, "claude"))
        self.assertEqual(Path(self._which(path, "fortitude")).resolve(), self.mixed / "fortitude")
        self.assertEqual(Path(self._which(path, "gfortran")).parent, self.plain)
        self.assertFalse((self.shim / "notes.txt").exists())

    def test_first_directory_still_wins(self) -> None:
        # `mixed` precedes `plain` on the original PATH, so its ruff must still be found first.
        path = ci_shape_suite.ci_path(f"{self.mixed}{os.pathsep}{self.plain}", {"claude"}, self.shim)
        self.assertEqual(Path(self._which(path, "ruff")).resolve(), self.mixed / "ruff")

    def test_directory_without_a_hidden_name_is_kept_verbatim(self) -> None:
        path = ci_shape_suite.ci_path(str(self.plain), {"claude"}, self.shim)
        self.assertEqual(path.split(os.pathsep), [str(self.plain)])

    def test_a_kept_directory_before_a_shimmed_one_still_wins(self) -> None:
        # `plain` precedes `mixed`: plain's ruff must stay first, so the shim takes mixed's
        # position rather than the head of PATH.
        path = ci_shape_suite.ci_path(f"{self.plain}{os.pathsep}{self.mixed}", {"claude"}, self.shim)
        self.assertEqual(Path(self._which(path, "ruff")).parent, self.plain)
        self.assertIsNone(self._which(path, "claude"))
        self.assertEqual(Path(self._which(path, "fortitude")).resolve(), self.mixed / "fortitude")

    def test_hidden_set_is_the_skip_probes_definition(self) -> None:
        from tools.tests import skip_probes

        expected = {thunk() for thunk, _ in skip_probes.DEPENDENCIES.values()}
        self.assertEqual(ci_shape_suite.hidden_executables(), expected)
        self.assertIn("claude", expected)
        self.assertIn("codex", expected)


class CiEnvTests(unittest.TestCase):
    """The environment the suite runs in, built from a synthetic operator environment."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.bin = self.root / "bin"
        self.shim = self.root / "shim"
        self.home = self.root / "home"
        for d in (self.bin, self.shim, self.home):
            d.mkdir()
        _exe(self.bin / "claude")
        _exe(self.bin / "ruff")
        self.operator = {
            "HOME": "/home/operator",
            "PATH": str(self.bin),
            "PYTHONPATH": "/operator/extra",
            "PYTEST_ADDOPTS": "-k only_this",
            "CLAUDECODE": "1",
            "CODEX_COMPANION_SESSION_ID": "s",
            "PYTEST_PLUGINS": "zzsubset",
            "CLAUDE_CODE_ENTRYPOINT": "cli",
            "LANG": "C.UTF-8",
        }
        self.env = ci_shape_suite.ci_env(self.operator, {"claude"}, self.shim, self.home,
                                         "/operator/site")

    def test_home_is_the_empty_directory_not_the_operators(self) -> None:
        self.assertEqual(self.env["HOME"], str(self.home))
        self.assertNotEqual(self.env["HOME"], self.operator["HOME"])

    def test_user_site_goes_first_and_an_existing_pythonpath_is_kept(self) -> None:
        self.assertEqual(self.env["PYTHONPATH"].split(os.pathsep), ["/operator/site", "/operator/extra"])

    def test_session_and_pytest_option_variables_are_dropped(self) -> None:
        for key in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT",
                    "CODEX_COMPANION_SESSION_ID"):
            self.assertNotIn(key, self.env)
        self.assertEqual(self.env["LANG"], "C.UTF-8")
        self.assertEqual(self.env["OMPI_MCA_rmaps_base_oversubscribe"], "1")

    def test_path_hides_and_keeps(self) -> None:
        self.assertIsNone(shutil.which("claude", path=self.env["PATH"]))
        self.assertIsNotNone(shutil.which("ruff", path=self.env["PATH"]))


class VerdictTests(unittest.TestCase):
    def test_only_a_passing_full_run_with_a_clean_check_is_zero(self) -> None:
        self.assertEqual(ci_shape_suite.verdict(0, (10, 0), 10, 0), 0)
        for suite_rc, counted, check_rc in ((1, (10, 0), 0), (5, (10, 0), 0), (0, None, 0),
                                            (0, (9, 0), 0), (0, (11, 0), 0), (0, (10, 1), 0),
                                            (0, (10, 0), None), (0, (10, 0), 1)):
            with self.subTest(suite_rc=suite_rc, counted=counted, check_rc=check_rc):
                self.assertEqual(ci_shape_suite.verdict(suite_rc, counted, 10, check_rc), 1)


class ReportTestcasesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)

    def test_counts_elements_not_the_tests_attribute(self) -> None:
        report = self.root / "r.xml"
        report.write_text('<testsuites><testsuite tests="9"><testcase name="a"/>'
                          '<testcase name="b"><failure/></testcase>'
                          '<testcase name="c"><error/></testcase>'
                          '<testcase name="d"><skipped/></testcase></testsuite></testsuites>')
        self.assertEqual(ci_shape_suite.report_testcases(report), (4, 2))

    def test_a_bare_testsuite_root_is_read(self) -> None:
        report = self.root / "r.xml"
        report.write_text('<testsuite><testcase name="a"/></testsuite>')
        self.assertEqual(ci_shape_suite.report_testcases(report), (1, 0))

    def test_missing_or_truncated_report_is_none(self) -> None:
        self.assertIsNone(ci_shape_suite.report_testcases(self.root / "absent.xml"))
        truncated = self.root / "t.xml"
        truncated.write_text("<testsuites><testsuite><testcase")
        self.assertIsNone(ci_shape_suite.report_testcases(truncated))


class RunWiringTests(unittest.TestCase):
    """`_run` with a stub runner: which environment each subprocess gets, and the exit code."""

    PRISTINE = 3

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        _exe(self.bin / "claude")
        self.operator = {"HOME": "/home/operator", "PATH": str(self.bin)}
        self.work = self.root / "work"
        self.work.mkdir()
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def _runner(self, ran: int, suite_rc: int = 0, check_rc: int = 0, pristine_rc: int = 0,
                failed: int = 0):
        def run(cmd, env, **_kwargs):
            self.calls.append((list(cmd), dict(env)))
            if "--collect-only" in cmd:
                # The checkout's addopts would select a subset; only `-o addopts=` gives the
                # pristine count. The ids come first, the count line last, as pytest prints.
                pristine = "-o" in cmd and cmd[cmd.index("-o") + 1] == "addopts="
                count = self.PRISTINE if pristine else ran
                out = "tools/tests/test_a.py::T::t\n\n" + f"{count} tests collected in 0.1s\n"
                return subprocess.CompletedProcess(cmd, pristine_rc, out, "")
            if "tools.tests.skip_probes" in cmd:
                return subprocess.CompletedProcess(cmd, check_rc, "skipped=0 accepted=0 refused=0\n", "")
            report = next(a.split("=", 1)[1] for a in cmd if a.startswith("--junitxml="))
            Path(report).write_text("<testsuites><testsuite>"
                                    + '<testcase name="f"><failure/></testcase>' * failed
                                    + '<testcase name="x"/>' * (ran - failed)
                                    + "</testsuite></testsuites>")
            return subprocess.CompletedProcess(cmd, suite_rc, "3 passed\n", "")
        return run

    def _go(self, **kw) -> int:
        args = argparse.Namespace(jobs=0, keep=False)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return ci_shape_suite._run(args, {"claude"}, self.work, environ=self.operator,
                                       runner=self._runner(**kw))

    def test_green_run_and_every_subprocess_gets_the_reduced_environment(self) -> None:
        self.assertEqual(self._go(ran=self.PRISTINE), 0)
        self.assertEqual(len(self.calls), 3)
        for cmd, env in self.calls:
            with self.subTest(cmd=cmd[:4]):
                self.assertEqual(env["HOME"], str(self.work / "home"))
                self.assertIsNone(shutil.which("claude", path=env["PATH"]))

    def test_a_failed_suite_is_one(self) -> None:
        self.assertEqual(self._go(ran=self.PRISTINE, suite_rc=1), 1)

    def test_a_refusing_skip_check_is_one(self) -> None:
        self.assertEqual(self._go(ran=self.PRISTINE, check_rc=1), 1)

    def test_a_subset_is_one(self) -> None:
        self.assertEqual(self._go(ran=self.PRISTINE - 1), 1)

    def test_a_failure_the_exit_status_hid_is_one(self) -> None:
        self.assertEqual(self._go(ran=self.PRISTINE, failed=1, suite_rc=0), 1)

    def test_a_failed_pristine_collection_is_two(self) -> None:
        self.assertEqual(self._go(ran=self.PRISTINE, pristine_rc=2), 2)

    def test_a_hidden_executable_left_on_path_stops_the_run(self) -> None:
        # The self-check behind ci_path: if the PATH it builds still reaches a hidden name,
        # nothing runs and the exit is 2.
        with mock.patch.object(ci_shape_suite, "ci_path", lambda path, hide, shim: path):
            self.assertEqual(self._go(ran=self.PRISTINE), 2)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
