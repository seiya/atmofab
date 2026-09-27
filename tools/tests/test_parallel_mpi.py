"""The MPI parallel backend package (`tools/backends/parallel/mpi/`, issue #316 R4-c PR-1).

What the neutral core reads from it through the registry: the launcher's prefix and programs,
the wrapper and the argv it replaces, the binding canary the launch probe compiles, and the
(empty) launch environment. The seams that compose these are tested beside them
(`test_host_execution.py`, `test_host_prerequisites.py`, `test_build_runtime_server.py`).
"""

from __future__ import annotations

import unittest

from tools.backends import registry
from tools.backends.parallel.mpi import execution, launcher, wrapper


class RecordTests(unittest.TestCase):
    def test_the_record_declares_what_pr_1_implements_and_no_presence_floor_yet(self) -> None:
        record = registry.get("parallel", "mpi")
        self.assertEqual(record.backend_provides,
                         frozenset({"execution_env", "launcher", "compiler_wrapper"}))
        self.assertEqual(record.core_provides, frozenset())
        # Each capability resolves to the submodule its CAPABILITY_MODULE_ATTR row names.
        for capability, module in (("execution_env", execution), ("launcher", launcher),
                                   ("compiler_wrapper", wrapper)):
            with self.subTest(capability=capability):
                self.assertIs(registry.capability_module("parallel", "mpi", capability), module)


class LauncherTests(unittest.TestCase):
    def test_the_prefix_carries_the_rank_count_it_is_given(self) -> None:
        for ranks in (1, 2, 4, 48):
            with self.subTest(ranks=ranks):
                prefix = launcher.argv_prefix(ranks)
                self.assertEqual(prefix, (launcher.EXECUTABLE, "-n", str(ranks)))

    def test_a_count_that_is_not_a_positive_int_is_refused(self) -> None:
        for bad in (0, -1, True, False, 2.0, "4", None):
            with self.subTest(ranks=bad), self.assertRaises(ValueError):
                launcher.argv_prefix(bad)  # type: ignore[arg-type]

    def test_the_programs_and_the_probe_are_the_launcher(self) -> None:
        self.assertEqual(launcher.EXECUTABLES, (launcher.EXECUTABLE,))
        self.assertEqual(launcher.RUNTIME_PROBE[0], launcher.EXECUTABLE)


class WrapperTests(unittest.TestCase):
    def test_wrap_replaces_argv0_and_keeps_the_rest(self) -> None:
        argv = ["gfortran", "-fsyntax-only", "-std=f2008", "a.f90"]
        self.assertEqual(wrapper.wrap(argv),
                         (wrapper.COMPILER_WRAPPER, "-fsyntax-only", "-std=f2008", "a.f90"))
        self.assertEqual(argv[0], "gfortran", "wrap must not mutate its input")

    def test_an_empty_argv_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            wrapper.wrap([])

    def test_the_wrapped_compiler_is_a_registered_syntax_adapter(self) -> None:
        # The build-runtime server compares a stage's compiler id against this value; one that
        # names no adapter would silently never wrap.
        self.assertTrue(registry.provides("compiler", wrapper.WRAPPED_COMPILER, "syntax_check"))

    def test_the_canary_uses_the_binding_and_its_argv_takes_the_two_placeholders(self) -> None:
        self.assertIn("use mpi_f08", wrapper.BINDING_CANARY_SOURCE)
        filled = [a.format(scratch="S", source="F") for a in wrapper.BINDING_CANARY_ARGV]
        self.assertIn("S", filled)
        self.assertEqual(filled[-1], "F")
        self.assertTrue(wrapper.BINDING_CANARY_FILENAME.endswith(".f90"))
        self.assertEqual(wrapper.SHOW_ARGV[0], wrapper.COMPILER_WRAPPER)


class ExecutionTests(unittest.TestCase):
    def test_no_environment_and_the_thread_count_is_validated(self) -> None:
        self.assertEqual(execution.environment(1), {})
        for bad in (0, True, 1.0):
            with self.subTest(threads=bad), self.assertRaises(ValueError):
                execution.environment(bad)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
