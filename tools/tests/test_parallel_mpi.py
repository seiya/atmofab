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


class LaunchCanaryTests(unittest.TestCase):
    """`launch_canary_problem`: one run of `LAUNCH_CANARY_RANKS` processes, and nothing else,
    reads as one run."""

    def _out(self, *sizes: object, noise: str = "") -> str:
        return noise + "".join(f"atmofab-mpi-size {n}\n" for n in sizes)

    def test_one_run_of_the_canary_count_passes_with_runtime_noise_around_it(self) -> None:
        n = launcher.LAUNCH_CANARY_RANKS
        self.assertGreater(n, 1, "a canary of one process cannot tell one run from several")
        self.assertIsNone(launcher.launch_canary_problem(0, self._out(*[n] * n)))
        self.assertIsNone(launcher.launch_canary_problem(
            0, self._out(*[n] * n, noise="Authorization required, but no protocol\n")))

    def test_every_other_shape_is_a_problem(self) -> None:
        n = launcher.LAUNCH_CANARY_RANKS
        for rc, out, needle in (
                (0, self._out(*[1] * n), "reported run sizes ['1', '1']"),
                (0, self._out(n), "reported run sizes ['2']"),
                (0, self._out(*[n] * (n + 1)), "reported run sizes"),
                (0, "", "reported run sizes none"),
                (0, "atmofab-mpi-size 2 extra\natmofab-mpi-size 2\n", "reported run sizes"),
                (1, self._out(*[n] * n), "the launch exited 1")):
            with self.subTest(rc=rc, out=out):
                problem = launcher.launch_canary_problem(rc, out)
                self.assertIsNotNone(problem)
                self.assertIn(needle, problem)


def _working_pair() -> str | None:
    """A directory holding a wrapper and a launcher of one installation that compile the
    binding canary, or None. Searched on PATH, then in /usr/bin (where a distribution installs
    them); a development host may have several installations."""
    import os
    import shutil
    import subprocess
    import tempfile
    for directory in [*os.environ.get("PATH", "").split(os.pathsep), "/usr/bin"]:
        exe = shutil.which(wrapper.COMPILER_WRAPPER, path=directory)
        if exe is None or shutil.which(launcher.EXECUTABLE, path=directory) is None:
            continue
        with tempfile.TemporaryDirectory() as tmp:
            src = f"{tmp}/{wrapper.BINDING_CANARY_FILENAME}"
            with open(src, "w", encoding="utf-8") as fh:
                fh.write(wrapper.BINDING_CANARY_SOURCE)
            argv = [exe, *(a.format(scratch=tmp, source=src)
                           for a in wrapper.BINDING_CANARY_ARGV)]
            if subprocess.run(argv, capture_output=True, check=False, timeout=120).returncode == 0:
                return directory
    return None


_PAIR = _working_pair()


@unittest.skipUnless(_PAIR, "no MPI installation whose wrapper compiles the binding canary")
class RealToolchainCanaryTests(unittest.TestCase):
    """The canary sources are this package's Fortran, and nothing else compiles them: run them
    with a real installation, so a source that stops compiling (or stops printing what
    `launch_canary_problem` reads) fails here rather than refusing every launch."""

    def test_the_launch_canary_builds_and_reads_as_one_run(self) -> None:
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            src = f"{tmp}/{launcher.LAUNCH_CANARY_FILENAME}"
            exe = f"{tmp}/canary"
            with open(src, "w", encoding="utf-8") as fh:
                fh.write(launcher.LAUNCH_CANARY_SOURCE)
            build = subprocess.run(
                [f"{_PAIR}/{wrapper.COMPILER_WRAPPER}",
                 *(a.format(scratch=tmp, source=src, exe=exe)
                   for a in launcher.LAUNCH_CANARY_BUILD_ARGV)],
                capture_output=True, text=True, check=False, timeout=120, cwd=tmp)
            self.assertEqual(build.returncode, 0, build.stderr)
            prefix = list(launcher.argv_prefix(launcher.LAUNCH_CANARY_RANKS))
            prefix[0] = f"{_PAIR}/{prefix[0]}"
            run = subprocess.run([*prefix, exe], capture_output=True, text=True, check=False,
                                 timeout=120, cwd=tmp, stdin=subprocess.DEVNULL)
            self.assertIsNone(launcher.launch_canary_problem(run.returncode, run.stdout),
                              run.stdout + run.stderr)
            # Started without the launcher it is one process: the quality check's premise.
            alone = subprocess.run([exe], capture_output=True, text=True, check=False,
                                   timeout=120, cwd=tmp, stdin=subprocess.DEVNULL)
            self.assertEqual(alone.returncode, 0, alone.stderr)
            self.assertIn("atmofab-mpi-size 1", alone.stdout)


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
