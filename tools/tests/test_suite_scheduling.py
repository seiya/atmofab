"""The suite's default xdist scheduler (`tools/tests/conftest.py::pytest_cmdline_main`).

Driven on a stand-in `config.option`, so the rows need neither xdist nor a worker: under `-n N`
with `--dist` left at its default `no`, the hook picks `worksteal`; every other combination is
left as given. A live run under `-n 2 -v` prints `scheduling tests via WorkStealingScheduling`
(nothing under `-q`; the measurement is in the hook's docstring)."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from tools.tests import conftest


def _decide(**option) -> SimpleNamespace:
    config = SimpleNamespace(option=SimpleNamespace(**option))
    # `firstresult`: a value would end pytest's session
    assert conftest.pytest_cmdline_main(config) is None
    return config.option


class DefaultSchedulerTests(unittest.TestCase):
    def test_workers_with_no_dist_steal(self) -> None:
        self.assertEqual(_decide(numprocesses=24, dist="no").dist, "worksteal")
        self.assertEqual(_decide(numprocesses="auto", dist="no").dist, "worksteal")

    def test_an_explicit_dist_is_kept(self) -> None:
        for dist in ("load", "loadfile", "loadscope", "each"):
            with self.subTest(dist=dist):
                self.assertEqual(_decide(numprocesses=24, dist=dist).dist, dist)

    def test_no_workers_is_left_alone(self) -> None:
        for n in (None, 0):
            with self.subTest(numprocesses=n):
                self.assertEqual(_decide(numprocesses=n, dist="no").dist, "no")

    def test_the_hook_runs_before_xdist_s_own(self) -> None:
        """xdist's `pytest_cmdline_main` is `tryfirst` too and turns `dist == "no"` into `load`
        under `-n`, after which this hook sees nothing to do. Measured 2026-11-11: without the
        marker, `-n 2 -v` prints `scheduling tests via LoadScheduling` again while every row
        above stays green, so the marker is pinned here as declared."""
        self.assertIs(conftest.pytest_cmdline_main.pytest_impl.get("tryfirst"), True)

    def test_without_xdist_the_options_are_absent_and_nothing_is_set(self) -> None:
        option = _decide()
        self.assertFalse(hasattr(option, "dist"))


if __name__ == "__main__":
    unittest.main()
