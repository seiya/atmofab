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

    GPU = profile_with(hardware={"class": "gpu", "architecture": "sm_90"})

    @staticmethod
    def _site(site_id: str, *executes: str):
        from tools.execution_sites import Site

        if site_id == he.LOCAL_SITE:
            return Site(site_id, tuple(executes))
        return Site(site_id, tuple(executes), host="box", workdir="/w")

    def test_a_class_the_site_does_not_execute_is_refused(self) -> None:
        """Issue #293: the site half. With no site (no `sites.yaml`) the local site executes its
        default classes, which do not include `gpu`; a site that lists only `cpu` refuses it
        too, and names itself."""
        for site in (None, self._site("gpu_less", "cpu"), self._site(he.LOCAL_SITE, "cpu")):
            with self.subTest(site=site):
                with self.assertRaises(he.LaunchUnavailable) as ctx:
                    he.launch_shape(self.GPU, site)
                self.assertIn("hardware.class: gpu is not executed at site", str(ctx.exception))
                self.assertIn(site.site_id if site else he.LOCAL_SITE, str(ctx.exception))

    def test_a_site_that_executes_the_class_names_itself_and_the_device_probe(self) -> None:
        box = self._site("gpu_box", "gpu")
        shape = he.launch_shape(self.GPU, box)
        self.assertEqual(shape.site, "gpu_box")
        self.assertEqual(shape.platform_probe, tuple(registry.capability_module(
            "hardware", "gpu", "execution").PLATFORM_PROBE))
        self.assertTrue(shape.platform_probe)
        # A local site that lists the class runs it here, with the same probe.
        local = he.launch_shape(self.GPU, self._site(he.LOCAL_SITE, "cpu", "gpu"))
        self.assertEqual((local.site, local.platform_probe), (he.LOCAL_SITE, shape.platform_probe))
        # A class the neutral core runs has no probe, at any site.
        cpu = he.launch_shape(FORTRAN_CPU, self._site("cluster", "cpu", "gpu"))
        self.assertEqual((cpu.site, cpu.platform_probe), ("cluster", None))

    def test_the_probe_is_reached_through_capability_module(self) -> None:
        fake = types.SimpleNamespace(PLATFORM_PROBE=["zz-probe", "--one"])
        real = registry.capability_module

        def capability_module(axis, backend_id, capability):
            if axis == "hardware":
                return fake
            return real(axis, backend_id, capability)

        with mock.patch.object(registry, "capability_module", side_effect=capability_module):
            shape = he.launch_shape(self.GPU, self._site("gpu_box", "gpu"))
        self.assertEqual(shape.platform_probe, ("zz-probe", "--one"))

    def test_a_class_whose_record_does_not_declare_execution_is_refused_at_any_site(
            self) -> None:
        """The registry half, driven by withdrawal: every implemented class declares
        `execution` since issue #293. A site listing the class does not open it."""
        record = registry.get("hardware", "gpu")
        withdrawn = record._replace(backend_provides=record.backend_provides - {"execution"})
        with mock.patch.dict(registry._BACKENDS, {("hardware", "gpu"): withdrawn}):
            with self.assertRaises(he.LaunchUnavailable) as ctx:
                he.launch_shape(self.GPU, self._site("gpu_box", "gpu"))
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


class DeviceTraceLaunchTests(unittest.TestCase):
    """The trace half of the launch shape (issue #307): a parallel backend that declares
    `device_trace` puts its profiling argv in front of the binary and names the summary command
    and file; one that declares none launches bare."""

    CUDA_GPU = profile_with(hardware={"class": "gpu", "architecture": "sm_90"},
                            parallel={"backend": "cuda"})

    def _gpu_site(self):
        from tools.execution_sites import Site

        return Site("gpu_box", ("gpu",), host="box", workdir="/w")

    def test_a_cuda_launch_runs_under_the_device_trace_and_names_its_summary(self) -> None:
        trace = registry.capability_module("parallel", "cuda", "device_trace")
        shape = he.launch_shape(self.CUDA_GPU, self._gpu_site())
        self.assertEqual(shape.argv_prefix, tuple(trace.profile_argv_prefix(he.KERNEL_TRACE_STEM)))
        self.assertEqual(shape.argv_prefix[0], trace.EXECUTABLES[0])
        self.assertIsNotNone(shape.trace)
        self.assertEqual(shape.trace.summary_argv, tuple(trace.summary_argv(he.KERNEL_TRACE_STEM)))
        self.assertEqual(shape.trace.summary_file, trace.summary_file(he.KERNEL_TRACE_STEM))
        # The binary follows the prefix, and the record carries it.
        self.assertEqual(shape.command(["bin", "--cases"])[len(shape.argv_prefix):],
                         ["bin", "--cases"])
        self.assertEqual(shape.record()["argv_prefix"], list(shape.argv_prefix))

    def test_the_stem_is_the_seams_constant(self) -> None:
        """The prefix and the summary are asked with ONE stem, so the summary reads the report
        the prefix wrote; a backend handed a different stem must show it."""
        calls: list[tuple[str, str]] = []

        def spy(name):
            return lambda stem: (calls.append((name, stem)), (f"{name}:{stem}",))[1]

        fake = types.SimpleNamespace(profile_argv_prefix=spy("p"), summary_argv=spy("s"),
                                     summary_file=lambda stem: f"f:{stem}", EXECUTABLES=("zz",))
        real = registry.capability_module

        def capability_module(axis, backend_id, capability):
            return fake if capability == "device_trace" else real(axis, backend_id, capability)

        with mock.patch.object(registry, "capability_module", side_effect=capability_module):
            shape = he.launch_shape(self.CUDA_GPU, self._gpu_site())
            executables = he.execution_executables("cuda")
        self.assertEqual(calls, [("p", he.KERNEL_TRACE_STEM), ("s", he.KERNEL_TRACE_STEM)])
        self.assertEqual(shape.argv_prefix, (f"p:{he.KERNEL_TRACE_STEM}",))
        self.assertEqual(shape.trace, he.TraceShape((f"s:{he.KERNEL_TRACE_STEM}",),
                                                    f"f:{he.KERNEL_TRACE_STEM}"))
        self.assertEqual(executables, ("zz",))

    def test_openmp_and_none_have_no_trace(self) -> None:
        for backend in ("openmp", "none"):
            with self.subTest(backend=backend):
                shape = he.launch_shape(profile_with(parallel={"backend": backend}))
                self.assertEqual((shape.argv_prefix, shape.trace), ((), None))
                self.assertEqual(he.execution_executables(backend), ())

    def test_execution_executables_are_the_backends(self) -> None:
        self.assertEqual(he.execution_executables("cuda"), tuple(registry.capability_module(
            "parallel", "cuda", "device_trace").EXECUTABLES))
        self.assertTrue(he.execution_executables("cuda"))

    def test_a_withdrawn_declaration_launches_bare(self) -> None:
        """The dispatch reads the record: withdrawing `device_trace` from cuda's removes the
        prefix, the summary and the executables together."""
        record = registry.get("parallel", "cuda")
        withdrawn = record._replace(
            backend_provides=record.backend_provides - {"device_trace"})
        with mock.patch.dict(registry._BACKENDS, {("parallel", "cuda"): withdrawn}):
            shape = he.launch_shape(self.CUDA_GPU, self._gpu_site())
            self.assertEqual(he.execution_executables("cuda"), ())
        self.assertEqual((shape.argv_prefix, shape.trace), ((), None))


class LauncherLaunchTests(unittest.TestCase):
    """The launcher half of the launch shape (issue #316): a parallel backend that declares
    `launcher` starts the binary's ranks with the profile's `execution.ranks`, outermost in the
    prefix; its programs join the executing machine's; a launcher target runs at `local` only;
    more ranks than one need a launcher."""

    MPI4 = profile_with(parallel={"backend": "mpi"},
                        execution={"threads_per_rank": 1, "ranks": 4})

    @staticmethod
    def _site(site_id: str, *executes: str):
        from tools.execution_sites import Site

        if site_id == he.LOCAL_SITE:
            return Site(site_id, tuple(executes))
        return Site(site_id, tuple(executes), host="box", workdir="/w")

    def test_the_prefix_is_the_launchers_with_the_profiles_rank_count(self) -> None:
        launcher = registry.capability_module("parallel", "mpi", "launcher")
        for ranks in (1, 3, 4):
            with self.subTest(ranks=ranks):
                profile = profile_with(parallel={"backend": "mpi"},
                                       execution={"threads_per_rank": 1, "ranks": ranks})
                shape = he.launch_shape(profile)
                self.assertEqual(shape.argv_prefix, tuple(launcher.argv_prefix(ranks)))
                self.assertEqual(he.launch_argv_prefix("mpi", ranks), shape.argv_prefix)
                self.assertEqual(shape.runtime_probe, tuple(launcher.RUNTIME_PROBE))
                self.assertIsNone(shape.trace)
                self.assertEqual(shape.env, {})
        # A profile stating no count is one rank — under the launcher all the same.
        one = profile_with(parallel={"backend": "mpi"})
        self.assertNotIn("ranks", one.doc["execution"])
        self.assertEqual(he.launch_shape(one).argv_prefix, tuple(launcher.argv_prefix(1)))

    def test_a_backend_without_a_launcher_has_no_prefix_and_no_runtime_probe(self) -> None:
        for backend in ("openmp", "none"):
            with self.subTest(backend=backend):
                shape = he.launch_shape(profile_with(parallel={"backend": backend}))
                self.assertEqual((shape.argv_prefix, shape.runtime_probe), ((), None))
                self.assertEqual(he.launch_argv_prefix(backend, 1), ())
                self.assertFalse(he.declares_launcher(backend))
        self.assertTrue(he.declares_launcher("mpi"))
        self.assertFalse(he.declares_launcher("zz_no_such_model"))

    def test_the_launcher_goes_outside_a_device_trace(self) -> None:
        """No backend declares both today; the order is fixed so the day one does, the trace
        runs per rank under the launcher rather than tracing the launcher itself."""
        record = registry.get("parallel", "mpi")
        both = record._replace(backend_provides=record.backend_provides | {"device_trace"})
        fake_trace = types.SimpleNamespace(
            profile_argv_prefix=lambda stem: ("zz-trace", stem), summary_argv=lambda stem: (),
            summary_file=lambda stem: stem, EXECUTABLES=("zz-trace",))
        real = registry.capability_module

        def capability_module(axis, backend_id, capability):
            if capability == "device_trace":
                return fake_trace
            return real(axis, backend_id, capability)

        with mock.patch.dict(registry._BACKENDS, {("parallel", "mpi"): both}), \
                mock.patch.object(registry, "capability_module", side_effect=capability_module):
            prefix = he.launch_argv_prefix("mpi", 2)
            shape = he.launch_shape(profile_with(parallel={"backend": "mpi"},
                                                 execution={"threads_per_rank": 1, "ranks": 2}))
            executables = he.execution_executables("mpi")
        launcher = registry.capability_module("parallel", "mpi", "launcher")
        self.assertEqual(prefix, (*launcher.argv_prefix(2), "zz-trace", he.KERNEL_TRACE_STEM))
        self.assertEqual(shape.argv_prefix, prefix)
        self.assertEqual(executables, (*launcher.EXECUTABLES, "zz-trace"))

    def test_the_launchers_programs_are_the_executing_machines(self) -> None:
        launcher = registry.capability_module("parallel", "mpi", "launcher")
        self.assertEqual(he.execution_executables("mpi"), tuple(launcher.EXECUTABLES))
        self.assertTrue(he.execution_executables("mpi"))

    def test_a_launcher_target_runs_at_local_only(self) -> None:
        """The backstop of the site gate (`execution_sites.site_violations`): a remote site is
        refused whatever it executes and however many ranks the profile runs."""
        for profile in (self.MPI4, profile_with(parallel={"backend": "mpi"})):
            with self.subTest(ranks=profile.ranks):
                with self.assertRaises(he.LaunchUnavailable) as ctx:
                    he.launch_shape(profile, self._site("cluster", "cpu"))
                self.assertIn("runs its binary under a launcher", str(ctx.exception))
                self.assertIn("site cluster", str(ctx.exception))
        # The local site, named or not, runs it.
        self.assertEqual(he.launch_shape(self.MPI4, self._site(he.LOCAL_SITE, "cpu")).site,
                         he.LOCAL_SITE)
        self.assertEqual(he.launch_shape(self.MPI4).site, he.LOCAL_SITE)

    def test_more_ranks_than_one_need_a_launcher(self) -> None:
        for backend in ("openmp", "none"):
            with self.subTest(backend=backend):
                profile = profile_with(parallel={"backend": backend},
                                       execution={"threads_per_rank": 1, "ranks": 2})
                with self.assertRaises(he.LaunchUnavailable) as ctx:
                    he.launch_shape(profile)
                self.assertIn("execution.ranks: 2 ranks need a launcher", str(ctx.exception))

    def test_perf_parallelism_states_the_profiles_rank_count(self) -> None:
        cpu = profile_with(parallel={"backend": "mpi"},
                           execution={"threads_per_rank": 1, "ranks": 4})
        self.assertEqual(he.perf_parallelism(cpu.doc), (4, 1, 0))
        # A class with `perf_facts` answers the per-rank half; the count is still the profile's.
        gpu = profile_with(hardware={"class": "gpu", "architecture": "sm_90"},
                           parallel={"backend": "cuda"},
                           execution={"threads_per_rank": 1, "ranks": 3})
        facts = registry.capability_module("hardware", "gpu", "perf_facts")
        _ranks, per_rank, devices = facts.parallelism(1)
        self.assertEqual(he.perf_parallelism(gpu.doc), (3, per_rank, devices))
        # And a profile stating none is one rank, as every profile before issue #316.
        self.assertEqual(he.perf_parallelism(FORTRAN_CPU.doc)[0], 1)


class LocalPlatformRecordTests(unittest.TestCase):
    """`local_platform_record` (issue #293; `workflow_conductor._host_platform_record` until
    then): every fact that cannot be read is `None`, never a refusal."""

    def test_the_host_facts_and_no_probe(self) -> None:
        import platform

        record = he.local_platform_record()
        self.assertEqual(set(record), {"machine", "node", "cpu_model", "gpu"})
        self.assertEqual((record["machine"], record["node"]),
                         (platform.machine(), platform.node()))
        self.assertIsNone(record["gpu"])

    def test_an_unreadable_cpuinfo_records_none(self) -> None:
        from pathlib import Path

        with mock.patch.object(Path, "read_text", side_effect=OSError("no proc")):
            record = he.local_platform_record()
        self.assertIsNone(record["cpu_model"])
        self.assertTrue(record["machine"])

    def test_the_probe_records_its_first_line_when_it_exits_zero(self) -> None:
        import sys

        ok = (sys.executable, "-c", "print(' Device A, 1.0 '); print('second')")
        self.assertEqual(he.local_platform_record(ok)["gpu"], "Device A, 1.0")
        for probe in ((sys.executable, "-c", "print('x'); raise SystemExit(3)"),
                      (sys.executable, "-c", "print('')"),
                      ("zz-no-such-program-anywhere",)):
            with self.subTest(probe=probe):
                self.assertIsNone(he.local_platform_record(probe)["gpu"])

    def test_a_runtime_probe_adds_the_parallel_runtime_line(self) -> None:
        import sys

        ok = (sys.executable, "-c", "print('Runtime X 4.1'); print('more')")
        record = he.local_platform_record(runtime_probe=ok)
        self.assertEqual(record["parallel_runtime"], "Runtime X 4.1")
        self.assertIsNone(record["gpu"])
        failing = (sys.executable, "-c", "raise SystemExit(1)")
        self.assertIsNone(he.local_platform_record(runtime_probe=failing)["parallel_runtime"])
        # No probe, no key: a launch without a launcher records what it always recorded.
        self.assertNotIn("parallel_runtime", he.local_platform_record())

    def test_a_probe_that_hangs_records_none(self) -> None:
        import subprocess

        with mock.patch.object(subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("p", 1)) as run:
            self.assertIsNone(he.local_platform_record(("p",))["gpu"])
        self.assertEqual(run.call_args.kwargs["timeout"], he.PROBE_TIMEOUT_SEC)


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
