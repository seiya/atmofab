"""The launch-shape seam (`tools/host_execution.py`, issue #289 R4-b PR-1)."""

from __future__ import annotations

import types
import unittest
from unittest import mock

from tools import host_execution as he
from tools.backends import registry
from tools.tests.target_fixtures import FORTRAN_CPU, profile_with


class LaunchShapeTests(unittest.TestCase):
    def test_the_checked_in_target_launches_locally_with_its_parallel_models_env(self) -> None:
        shape = he.launch_shape(FORTRAN_CPU)
        self.assertEqual(shape.site, he.LOCAL_SITE)
        self.assertEqual(shape.argv_prefix, ())
        # The env is the parallel backend's own answer, asked through the registry — not a
        # constant restated here.
        expected = registry.capability_module(
            "parallel", FORTRAN_CPU.parallel_backend, "execution_env").environment(
                FORTRAN_CPU.threads_per_rank)
        self.assertTrue(expected, "the checked-in target's model has no env; this row would "
                                  "not tell a composed env from an empty one")
        self.assertEqual(shape.env, expected)

    def test_the_thread_count_is_the_profiles_not_a_default(self) -> None:
        # Driven off the only value every profile carries today (1): a hardcoded 1 must fail.
        # The loader refuses > 1 for now, so the document is varied past it on purpose.
        three = profile_with(execution={"threads_per_rank": 3})
        self.assertEqual(set(he.launch_shape(three).env.values()), {"3"})

    def test_a_serial_model_launches_with_no_environment_of_its_own(self) -> None:
        self.assertEqual(he.launch_shape(profile_with(parallel={"backend": "none"})).env, {})

    def test_a_class_this_host_cannot_run_on_is_refused(self) -> None:
        with self.assertRaises(he.LaunchUnavailable) as ctx:
            he.launch_shape(profile_with(hardware={"class": "gpu", "architecture": "sm_90"}))
        self.assertIn("hardware.class", str(ctx.exception))
        self.assertIn("'execution'", str(ctx.exception))

    def test_a_parallel_value_with_no_launch_answer_is_refused(self) -> None:
        # An open-vocabulary token the registry has no record for: running it with whatever the
        # host carries is the silent answer this seam replaced.
        for backend in ("openmp_tasks", "no_such_model"):
            with self.subTest(backend=backend):
                with self.assertRaises(he.LaunchUnavailable) as ctx:
                    he.launch_shape(profile_with(parallel={"backend": backend}))
                self.assertIn("parallel.backend", str(ctx.exception))
                self.assertIn("'execution_env'", str(ctx.exception))

    def test_a_package_declaration_is_reached_through_capability_module(self) -> None:
        """The env comes from `capability_module`, not from a same-named import."""
        fake = types.SimpleNamespace(environment=lambda threads: {"ZZ_THREADS": str(threads)})
        with mock.patch.object(registry, "capability_module", return_value=fake) as cap:
            env = he.launch_shape(FORTRAN_CPU).env
        cap.assert_called_once_with("parallel", FORTRAN_CPU.parallel_backend, "execution_env")
        self.assertEqual(env, {"ZZ_THREADS": str(FORTRAN_CPU.threads_per_rank)})

    def test_a_core_declaration_is_the_empty_environment_and_loads_nothing(self) -> None:
        record = registry.get("parallel", "none")
        self.assertIn("execution_env", record.core_provides)
        with mock.patch.object(registry, "capability_module") as cap:
            env = he.launch_shape(profile_with(parallel={"backend": "none"})).env
        cap.assert_not_called()
        self.assertEqual(env, {})

    def test_command_and_record(self) -> None:
        shape = he.LaunchShape(argv_prefix=("wrap", "-n1"), env={"B": "2", "A": "1"})
        self.assertEqual(shape.command(["bin", "--cases"]), ["wrap", "-n1", "bin", "--cases"])
        self.assertEqual(shape.record(), {"argv_prefix": ["wrap", "-n1"],
                                          "env": {"A": "1", "B": "2"}})
        self.assertEqual(list(shape.record()["env"]), ["A", "B"])


class OpenmpExecutionEnvTests(unittest.TestCase):
    def test_both_variables_carry_the_count_and_a_bad_count_is_refused(self) -> None:
        module = registry.capability_module("parallel", "openmp", "execution_env")
        self.assertEqual(module.environment(4),
                         {"OMP_NUM_THREADS": "4", "OMP_THREAD_LIMIT": "4"})
        for bad in (0, -1, True, "2", 1.0):
            with self.subTest(threads=bad), self.assertRaises(ValueError):
                module.environment(bad)


if __name__ == "__main__":
    unittest.main()
