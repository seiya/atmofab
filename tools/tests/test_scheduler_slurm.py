#!/usr/bin/env python3
"""Tests for the `slurm` scheduler backend's `job_submit` (`tools/backends/scheduler/slurm/`,
issue #293 PR-4). The executor's path through it, end to end over shims, is
`tools/tests/test_remote_execution.py` `SchedulerTests`."""

from __future__ import annotations

import re
import unittest

from tools.backends import registry
from tools.backends.scheduler.slurm import submit


class ForegroundArgvTests(unittest.TestCase):

    def _argv(self, **kw) -> tuple[str, ...]:
        args = {"directives": (), "job_name": "atmofab-j", "wall_clock_sec": 60,
                "queue_timeout_sec": 5}
        args.update(kw)
        return submit.foreground_argv(**args)

    def test_the_executors_options_come_after_the_directives(self) -> None:
        self.assertEqual(
            self._argv(directives=("--partition=gpu", "-p  debug", "--gres=gpu:1")),
            ("srun", "--partition=gpu", "-p", "debug", "--gres=gpu:1", "--ntasks=1",
             "--job-name=atmofab-j", "--time=1", "--immediate=5"))

    def test_the_time_limit_is_rounded_up_to_whole_minutes(self) -> None:
        for sec, minutes in ((1, 1), (60, 1), (61, 2), (3600, 60), (3601, 61)):
            with self.subTest(sec=sec):
                self.assertIn(f"--time={minutes}", self._argv(wall_clock_sec=sec))

    def test_a_bound_below_one_second_is_refused(self) -> None:
        for kw in ({"wall_clock_sec": 0}, {"queue_timeout_sec": 0}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                self._argv(**kw)


class DeclarationTests(unittest.TestCase):

    def test_the_registry_reaches_this_module_for_job_submit(self) -> None:
        self.assertIs(registry.capability_module("scheduler", "slurm", "job_submit"), submit)
        self.assertIsNone(registry.unavailable_reason("scheduler", "slurm"))

    def test_the_job_id_variable_is_a_variable_name(self) -> None:
        self.assertTrue(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", submit.JOB_ID_VARIABLE))
        self.assertEqual(submit.REMOTE_EXECUTABLES, ("srun",))


if __name__ == "__main__":
    unittest.main()
