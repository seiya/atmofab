#!/usr/bin/env python3
"""Tests for `ci_shape_suite.ci_path` and the executable set it hides.

The script's verdict is "this branch is green as CI sees it". Its dangerous direction is a
PATH that still reaches a hidden executable (a skip CI would refuse reads as a run here), and
the false-alarm direction is a PATH that loses a tool CI does have (the linters beside a CLI,
129 and 220 false failures measured on two branches that dropped whole directories).
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


if __name__ == "__main__":
    unittest.main()
