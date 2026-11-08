#!/usr/bin/env python3
"""Tests for `ci_shape_suite`: the PATH, the environment, the hidden set and the exit verdict.

The script's verdict is "green without the skip-probe dependencies and the operator's home".
Its dangerous direction is exit 0 where CI is red: a PATH that still reaches a hidden
executable, the operator's HOME or a leftover `PYTEST_ADDOPTS` reaching the run, or a failed
suite or skip check folded into 0. The false-alarm direction is a PATH that loses a tool CI
does have (the linters installed beside a CLI).
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

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
        self.assertEqual(path.split(os.pathsep), [str(self.shim), str(self.plain)])

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
        for key in ("PYTEST_ADDOPTS", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
            self.assertNotIn(key, self.env)
        self.assertEqual(self.env["LANG"], "C.UTF-8")

    def test_path_hides_and_keeps(self) -> None:
        self.assertIsNone(shutil.which("claude", path=self.env["PATH"]))
        self.assertIsNotNone(shutil.which("ruff", path=self.env["PATH"]))


class VerdictTests(unittest.TestCase):
    def test_only_a_passing_suite_with_a_clean_check_is_zero(self) -> None:
        self.assertEqual(ci_shape_suite.verdict(0, True, 0), 0)
        for suite_rc, written, check_rc in ((1, True, 0), (0, False, None), (0, True, None),
                                            (0, True, 1), (5, True, 0)):
            with self.subTest(suite_rc=suite_rc, written=written, check_rc=check_rc):
                self.assertEqual(ci_shape_suite.verdict(suite_rc, written, check_rc), 1)


if __name__ == "__main__":
    unittest.main()
