#!/usr/bin/env python3
"""`Validate.execute` at an execution site (issue #293, PR-3): the conductor's wiring of
`tools/remote_execution.py`.

The rows drive the production entry point — `Conductor._execute_inproc`, and
`_run_deterministic_substep` where the routing of a failure is the subject — end to end through
the same `ssh` / `scp` shims `tools/tests/test_remote_execution.py` uses: the "site" is a
directory under the test's temporary tree, reached by running the command string locally. What is
mocked is the post-execute gate's process (every other `subprocess.run` is the real one, the
shims' included) and, in two rows, the device probe the `hardware` package names — the local-site
row also mocks `_control_file_module`, to observe that no build control file is shipped.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from pathlib import Path
from unittest import mock

import tools.workflow_conductor as wc
from tools.backends import registry
from tools.execution_sites import Site
from tools.host_execution import LOCAL_SITE, launch_shape
from tools.tests.llm_samples import sample_config_with as _cfg
from tools.tests.target_fixtures import TARGET_ID, profile_with
from tools.tests.test_remote_execution import _SCP_SHIM, _SSH_SHIM
from tools.tests.test_workflow_conductor import _TargetedConductor

# The conductor imports the build-runtime server from `<repo_root>/mcp_servers`, which a scratch
# repository does not carry; the real one is put on the path, as the conductor's own tests do.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mcp_servers"))
import build_runtime_server  # noqa: E402

#: The shipped runner: writes the evidence the execute body reads into its working directory,
#: records its argv and cwd, and exits with `$RUNNER_RC`.
_RUNNER = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, sys
    cases = sys.argv[sys.argv.index("--cases") + 2:]
    json.dump({"verdict": {c: "pass" for c in cases}}, open("diagnostics.json", "w"))
    json.dump({}, open("perf.json", "w"))
    json.dump({"argv": sys.argv, "cwd": os.getcwd()}, open("argv.json", "w"))
    print("runner out")
    print("runner err", file=sys.stderr)
    sys.exit(int(os.environ.get("RUNNER_RC") or 0))
''')

#: The build control file the quality check runs: its test target runs the shipped runner the
#: way the conductor's variables say to, in `RUNDIR`.
_MAKEFILE = textwrap.dedent('''\
    BIN ?= spec_x_runner
    test:
    \tcd $(RUNDIR) && $(BINDIR)/$(BIN) --cases $(SPEC) $(CASES)
''')


def _real_gate_free_run(real):
    """`subprocess.run` with the post-execute gate answered 0 and every other call real."""

    def run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and "tools/validate_pipeline_semantics.py" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real(cmd, *args, **kwargs)

    return run


class _Node:
    """A scratch repository with one built node, a shim-reached site and a conductor for it."""

    def __init__(self, tmp: str, *, target=None, site: Site | None = None) -> None:
        self.root = Path(tmp)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.shims = self.root / "shims"
        self.shims.mkdir()
        for name, body in (("ssh", _SSH_SHIM), ("scp", _SCP_SHIM)):
            p = self.shims / name
            p.write_text(body)
            p.chmod(p.stat().st_mode | stat.S_IXUSR)
        self.log = self.root / "shim.log"
        self.log.touch()
        self.workdir = self.root / "remote" / "jobs"
        self.workdir.mkdir(parents=True)
        self.site = site if site is not None else Site(
            site_id="box", executes=("cpu",), host="box", workdir=str(self.workdir))
        self.target = target if target is not None else profile_with()
        self.conductor = _TargetedConductor(
            repo_root=self.repo, orchestration_id="orch_1", orchestration_agent_run_id="x",
            llm_config=_cfg("claude"), env={}, target_profile=self.target, site=self.site)
        self.refs = wc.NodeRefs(
            target_id=TARGET_ID, node_key="component/spec_x@0.1.0",
            spec_path="spec/component/spec_x", ir_id="x_1", pipeline_id="x_1",
            source_id="src_1", binary_id="bin_1", run_id="run_1", source_binary_id="bin_1")
        ir = self.repo / self.refs.ir_ref
        ir.mkdir(parents=True)
        (ir / "spec.ir.yaml").write_text(
            "case:\n  test_case_set:\n    - case_id: c_alpha\n    - case_id: c_beta\n",
            encoding="utf-8")
        self.src = self.repo / self.refs.source_dir() / "src"
        self.src.mkdir(parents=True)
        (self.src / "Makefile").write_text(_MAKEFILE, encoding="utf-8")
        # A source file the quality check does not need, which must not be shipped.
        (self.src / "kernel.f90").write_text("! not shipped\n", encoding="utf-8")
        bin_dir = self.repo / self.refs.binary_dir() / "bin"
        bin_dir.mkdir(parents=True)
        self.binary = bin_dir / "spec_x_runner"
        self.binary.write_text(_RUNNER, encoding="utf-8")
        self.binary.chmod(0o755)
        self.built_at(self.site.site_id)
        self.node_dir = self.repo / self.refs.run_node_dir()

    def built_at(self, site_id: str | None, host: str | None = "same") -> None:
        """Record where the binary was built (`binary_meta.json#environment.build_site`, issue
        #333) — by default the site it runs at and that site's host, which `Validate.execute`
        requires; None writes a record with no `environment`, as a build-1 record has none."""
        if host == "same":
            host = self.site.host if site_id == self.site.site_id else None
        meta: dict = {"binary_id": self.refs.binary_id}
        if site_id is not None:
            meta["environment"] = {"platform": {"site": site_id},
                                   "build_site": {"site": site_id, "host": host},
                                   "compiler_version": None}
        (self.repo / self.refs.binary_dir() / "binary_meta.json").write_text(
            json.dumps(meta), encoding="utf-8")

    def env(self, **knobs: str):
        env = {"PATH": f"{self.shims}{os.pathsep}{os.environ['PATH']}",
               "SHIM_LOG": str(self.log)}
        env.update(knobs)
        return mock.patch.dict(os.environ, env)

    def execute(self, **knobs: str) -> dict:
        with self.env(**knobs), mock.patch.object(
                subprocess, "run", side_effect=_real_gate_free_run(subprocess.run)):
            return self.conductor._execute_inproc(self.refs, "arid-1")

    def substep(self, **knobs: str):
        with self.env(**knobs), mock.patch.object(
                subprocess, "run", side_effect=_real_gate_free_run(subprocess.run)):
            return self.conductor._run_deterministic_substep(
                self.refs, "validate", "execute", "arid-1", {})

    def log_entries(self, path: Path) -> list[dict]:
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.log.read_text().splitlines()]


class ExecuteAtARemoteSiteTests(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.n = _Node(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_the_evidence_and_the_records_are_the_local_paths_own(self) -> None:
        n = self.n
        # The "site" answers `hostname` with a name of its own, so the platform record can only
        # be the site's answer: the shim runs on this machine, and without this the host's
        # own record would satisfy every assertion below.
        fake = n.root / "site_bin"
        fake.mkdir()
        (fake / "hostname").write_text("#!/bin/sh\necho site-node-zz\n")
        (fake / "hostname").chmod(0o755)
        out = n.execute(SHIM_SSH_PATH=f"{fake}{os.pathsep}{os.environ['PATH']}")
        self.assertEqual(out["returncode"], 0, out)
        job = f"{n.workdir}/orch_1/arid-1"

        # What ran at the site: the shipped runner, by its remote path, in the job's run
        # directory, with both case ids.
        run_tmp = n.repo / "workspace" / "tmp" / "arid-1" / "run"
        argv = json.loads((run_tmp / "argv.json").read_text())
        self.assertEqual(argv["argv"][1:], ["--cases", f"{job}/ir/spec.ir.yaml",
                                            "c_alpha", "c_beta"])
        self.assertEqual(argv["argv"][0], f"{job}/bin/spec_x_runner")
        self.assertEqual(argv["cwd"], f"{job}/run")
        # The quality check ran the same runner in its own directory, and its output came back.
        qc_tmp = n.repo / "workspace" / "tmp" / "arid-1" / "qc_run"
        self.assertEqual(json.loads((qc_tmp / "argv.json").read_text())["cwd"], f"{job}/qc_run")
        self.assertTrue((run_tmp / "raw" / "state_snapshots" / "initial").is_dir())
        # Promoted from the collected run directory, as the local path promotes its own.
        self.assertTrue((n.node_dir / "diagnostics.json").is_file())
        self.assertEqual(json.loads((n.node_dir / "quality_check.json").read_text())["status"],
                         "pass")
        self.assertEqual((n.node_dir / "stdout.log").read_text(), "runner out\n")
        self.assertEqual((n.node_dir / "stderr.log").read_text(), "runner err\n")

        # Shipped: the binary, the IR and the build control file — nothing else of src/.
        shipped = {str(p.relative_to(n.root / "repo" / "workspace" / "tmp" / "arid-1" / "site"
                                     / "stage")) for p in
                   (n.repo / "workspace" / "tmp" / "arid-1" / "site" / "stage").rglob("*")
                   if p.is_file()}
        self.assertEqual(shipped, {"bin/spec_x_runner", "ir/spec.ir.yaml", "src/Makefile"})
        # And the job directory is gone from the site.
        self.assertFalse(Path(job).exists())

        # One log entry per command, at the canonical placements, naming LOCAL artifacts.
        run_log = n.log_entries(n.node_dir / "command_log.jsonl")
        qc_log = n.log_entries(n.src / "command_log.jsonl")
        self.assertEqual([e["tool_name"] for e in run_log], ["run_program"])
        self.assertEqual([e["tool_name"] for e in qc_log], ["run_quality_checks"])
        self.assertEqual(run_log[0]["command"][0], str(n.binary.resolve()))
        self.assertEqual(run_log[0]["cwd"], str(run_tmp))
        self.assertEqual(qc_log[0]["command"], ["make", "test"])
        self.assertEqual(qc_log[0]["cwd"], str(n.src))
        self.assertEqual(run_log[0]["site"]["remote_cwd"], f"{job}/run")
        self.assertEqual(qc_log[0]["site"]["remote_cwd"], f"{job}/src")
        # The server's own bounds, passed because no server applies them at a site.
        server = build_runtime_server
        self.assertEqual(run_log[0]["timeout_sec"], server.RUN_PROGRAM_TIMEOUT_SEC)
        self.assertEqual(qc_log[0]["timeout_sec"], server.QUALITY_CHECKS_TIMEOUT_SEC)
        self.assertEqual(run_log[0]["capture_limit"], wc._FULL_CAPTURE_LIMIT)
        self.assertEqual(sorted(qc_log[0]["env_override_keys"]),
                         ["BIN", "BINDIR", "CASES", "OBJDIR", "RUNDIR", "SPEC"])
        launch_env = launch_shape(n.target, n.site).env
        self.assertTrue(launch_env, "the fixture target's model sets no env; this row would not "
                                    "tell a passed env from a dropped one")
        self.assertEqual(run_log[0]["env_override_keys"], sorted(launch_env))
        for entry in (*run_log, *qc_log):
            self.assertEqual((entry["orchestration_id"], entry["agent_run_id"]),
                             ("orch_1", "arid-1"))

        trial = json.loads((n.node_dir / "trial_meta.json").read_text())
        refs = trial["source_command_ref"]
        self.assertEqual(refs["run_program"]["command_id"], run_log[0]["command_id"])
        self.assertEqual(refs["run_quality_checks"]["command_id"], qc_log[0]["command_id"])
        env = trial["environment"]
        self.assertEqual(env["platform"]["site"], "box")
        # The site's own answers.
        import platform
        self.assertEqual((env["platform"]["machine"], env["platform"]["node"]),
                         (platform.machine(), "site-node-zz"))
        self.assertNotEqual(platform.node(), "site-node-zz")
        self.assertIsNone(env["platform"]["gpu"])
        self.assertEqual(env["execution_site"], {
            "site": "box", "host": "box", "scheduler": "none", "job_id": None,
            "remote_dir": job, "queue_wait_ms": 0})

    def test_a_failing_runner_is_the_kernels_failure_and_the_check_does_not_run(self) -> None:
        n = self.n
        out = n.execute(RUNNER_RC="3")
        self.assertEqual(out["returncode"], 0)
        self.assertIn("[run_program failed: runtime_error]", out["stderr"])
        self.assertIn("runner err", out["stderr"])
        self.assertFalse((n.node_dir / "trial_meta.json").exists())
        run_log = n.log_entries(n.node_dir / "command_log.jsonl")
        self.assertEqual([(e["tool_name"], e["return_code"]) for e in run_log],
                         [("run_program", 3)])
        self.assertEqual(n.log_entries(n.src / "command_log.jsonl"), [])

    def test_a_transport_failure_is_a_deterministic_validate_error_with_no_evidence(
            self) -> None:
        for knobs in ({"SHIM_SSH_FAIL": "atmofab-status"}, {"SHIM_SCP_FAIL": "down"},
                      {"SHIM_SCP_FAIL": "up"}):
            with self.subTest(**knobs), tempfile.TemporaryDirectory() as tmp:
                n = _Node(tmp)
                result = n.substep(**knobs)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("deterministic_validate_error", result.stderr)
                self.assertFalse((n.node_dir / "trial_meta.json").exists())
                self.assertEqual(n.log_entries(n.node_dir / "command_log.jsonl"), [])
                self.assertEqual(n.log_entries(n.src / "command_log.jsonl"), [])

    def test_a_stale_job_directory_is_refused_and_left(self) -> None:
        n = self.n
        stale = n.workdir / "orch_1" / "arid-1"
        stale.mkdir(parents=True)
        (stale / "old").write_text("x")
        result = n.substep()
        self.assertIn("deterministic_validate_error", result.stderr)
        self.assertIn("stale job directory", result.stderr)
        self.assertTrue((stale / "old").exists())

    def test_a_binary_built_at_another_site_is_not_run_here(self) -> None:
        """Issue #333: a binary runs at the site that built it. A target mapped elsewhere since
        its Build reuses that Build (the site is in no key), and its binary is refused before
        anything is shipped — as is a record that names no build site (a build-1 record)."""
        n = self.n
        # The site id and its ssh destination: a site whose `host` changed in `sites.yaml` is
        # another machine under the same id (round 2, Codex).
        for (built_at, host), named in (
                (("elsewhere", "elsewhere"), "elsewhere (host elsewhere)"),
                ((LOCAL_SITE, None), LOCAL_SITE),
                (("box", "another-host"), "box (host another-host)"),
                ((None, None), "(none recorded)")):
            with self.subTest(built_at=built_at, host=host):
                n.built_at(built_at, host)
                result = n.substep()
                self.assertEqual(result.returncode, 1, result)
                self.assertIn("deterministic_validate_error", result.stderr)
                self.assertIn(f"was built at site {named} and target {TARGET_ID} now runs at "
                              f"site box (host box)", result.stderr)
                # Both phases: a rebuild that reproduces the bytes leaves the validate key
                # where it was, and `build` alone would adopt the old site's Validate.
                self.assertIn("--rederive build,validate", result.stderr)
                self.assertEqual(n.calls(), [])
                self.assertFalse((n.node_dir / "trial_meta.json").exists())
        # The site it was built at runs it.
        n.built_at("box")
        self.assertEqual(n.execute()["returncode"], 0)
        self.assertTrue(n.calls())

    def test_the_site_must_execute_the_class_before_anything_is_contacted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gpu = profile_with(hardware={"class": "gpu", "architecture": "sm_90"})
            n = _Node(tmp, target=gpu)  # the default site executes cpu only
            result = n.substep()
            self.assertIn("deterministic_validate_error", result.stderr)
            self.assertIn("gpu is not executed at site box", result.stderr)
            self.assertEqual(n.calls(), [])

    def test_the_device_probe_the_class_names_runs_at_the_site(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gpu = profile_with(hardware={"class": "gpu", "architecture": "sm_90"})
            site = Site(site_id="gpu_box", executes=("gpu",), host="box",
                        workdir=str(Path(tmp) / "remote" / "jobs"))
            n = _Node(tmp, target=gpu, site=site)
            fake = types.SimpleNamespace(PLATFORM_PROBE=(
                sys.executable, "-c", "print('Device Z, 9.9'); print('second')"))
            real = registry.capability_module
            with mock.patch.object(registry, "capability_module", side_effect=lambda a, b, c: (
                    fake if (a, c) == ("hardware", "execution") else real(a, b, c))):
                out = n.execute()
            self.assertEqual(out["returncode"], 0, out)
            env = json.loads((n.node_dir / "trial_meta.json").read_text())["environment"]
            self.assertEqual(env["platform"]["gpu"], "Device Z, 9.9")
            self.assertEqual(env["platform"]["site"], "gpu_box")


#: A stand-in for the device trace's program at the site (issue #307), installed under the name
#: the backend's `EXECUTABLES` gives it. Its profiling form runs the rest of its argv after the
#: first `FAKE_TRACE_PREFIX_LEN` arguments (the prefix minus the program) and returns its status;
#: its summary form writes `$FAKE_TRACE_CSV` to `$FAKE_TRACE_SUMMARY` in its cwd, and the export
#: database the stats run writes for a readable report (the last argument's stem + `.sqlite`),
#: and exits `$FAKE_TRACE_RC`.
_FAKE_TRACER = textwrap.dedent('''\
    #!/usr/bin/env python3
    import os, subprocess, sys
    args = sys.argv[1:]
    n = int(os.environ["FAKE_TRACE_PREFIX_LEN"])
    if args[:1] == [os.environ["FAKE_TRACE_PROFILE_WORD"]]:
        sys.exit(subprocess.run(args[n:]).returncode)
    with open(os.environ["FAKE_TRACE_SUMMARY"], "w") as f:
        f.write(os.environ.get("FAKE_TRACE_CSV", ""))
    with open(os.path.splitext(args[-1])[0] + ".sqlite", "w") as f:
        f.write("db")
    sys.exit(int(os.environ.get("FAKE_TRACE_RC") or 0))
''')


class ExecuteTracedAtARemoteSiteTests(unittest.TestCase):
    """The device trace in the remote job (issue #307): the binary runs under the trace's prefix,
    the summary is the job's second command, in the run directory, and its file comes home with
    the collected run directory to be promoted."""

    CSV = "Instances,Name\n3,k(double *, long)\n"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.n = _Node(self._tmp.name, target=profile_with(parallel={"backend": "cuda"}))
        self.shape = launch_shape(self.n.target, self.n.site)
        self.assertIsNotNone(self.shape.trace)
        trace = registry.capability_module("parallel", "cuda", "device_trace")
        self.site_bin = self.n.root / "site_bin"
        self.site_bin.mkdir()
        tracer = self.site_bin / trace.EXECUTABLES[0]
        tracer.write_text(_FAKE_TRACER)
        tracer.chmod(0o755)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def knobs(self, **extra: str) -> dict[str, str]:
        return {"SHIM_SSH_PATH": f"{self.site_bin}{os.pathsep}{os.environ['PATH']}",
                "FAKE_TRACE_PREFIX_LEN": str(len(self.shape.argv_prefix) - 1),
                "FAKE_TRACE_PROFILE_WORD": self.shape.argv_prefix[1],
                "FAKE_TRACE_SUMMARY": self.shape.trace.summary_file,
                "FAKE_TRACE_CSV": self.CSV, **extra}

    def test_the_trace_is_the_second_command_of_the_job_and_its_summary_comes_home(
            self) -> None:
        n = self.n
        out = n.execute(**self.knobs())
        self.assertEqual(out["returncode"], 0, out)
        job = f"{n.workdir}/orch_1/arid-1"
        run_tmp = n.repo / "workspace" / "tmp" / "arid-1" / "run"
        # The runner ran, under the prefix, in the job's run directory.
        self.assertEqual(json.loads((run_tmp / "argv.json").read_text())["cwd"], f"{job}/run")
        run_log = n.log_entries(n.node_dir / "command_log.jsonl")
        self.assertEqual([e["tool_name"] for e in run_log], ["run_program", "run_program"])
        binary_run, summary = run_log
        prefix = list(self.shape.argv_prefix)
        self.assertEqual(binary_run["command"][:len(prefix)], prefix)
        self.assertEqual(binary_run["command"][len(prefix)], str(n.binary.resolve()))
        self.assertEqual(summary["command"], list(self.shape.trace.summary_argv))
        self.assertEqual((summary["cwd"], summary["site"]["remote_cwd"]),
                         (str(run_tmp), f"{job}/run"))
        self.assertEqual(summary["env_override_keys"], [])
        self.assertEqual([e["tool_name"] for e in n.log_entries(n.src / "command_log.jsonl")],
                         ["run_quality_checks"])
        # Promoted under its neutral name, and recorded apart from the binary's run.
        self.assertEqual((n.node_dir / "kernel_trace.csv").read_text(), self.CSV)
        trial = json.loads((n.node_dir / "trial_meta.json").read_text())
        self.assertEqual(trial["kernel_trace"]["command_id"], summary["command_id"])
        self.assertEqual(trial["source_command_ref"]["run_program"]["command_id"],
                         binary_run["command_id"])
        self.assertEqual(trial["environment"]["launch"]["argv_prefix"], prefix)

    def test_a_failing_summary_is_a_deterministic_validate_error_and_the_check_does_not_run(
            self) -> None:
        n = self.n
        result = n.substep(**self.knobs(FAKE_TRACE_RC="2"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("deterministic_validate_error", result.stderr)
        self.assertIn("kernel_trace_summary_failed: rc=2", result.stderr)
        self.assertEqual(n.log_entries(n.src / "command_log.jsonl"), [])
        self.assertFalse((n.node_dir / "trial_meta.json").exists())
        self.assertFalse((n.node_dir / "kernel_trace.csv").exists())

    def test_a_failing_runner_under_the_trace_runs_no_summary(self) -> None:
        n = self.n
        out = n.execute(**self.knobs(RUNNER_RC="3"))
        self.assertEqual(out["returncode"], 0)
        self.assertIn("[run_program failed: runtime_error]", out["stderr"])
        run_log = n.log_entries(n.node_dir / "command_log.jsonl")
        self.assertEqual([(e["tool_name"], e["return_code"]) for e in run_log],
                         [("run_program", 3)])


class ExecuteAtTheLocalSiteTests(unittest.TestCase):
    """A local site — the default with no `sites.yaml`, or one that lists more classes — runs
    in-process through the server and contacts nothing."""

    def test_a_local_site_that_executes_the_class_runs_here_and_probes_the_device(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gpu = profile_with(hardware={"class": "gpu", "architecture": "sm_90"})
            n = _Node(tmp, target=gpu, site=Site(LOCAL_SITE, ("cpu", "gpu")))
            fake = types.SimpleNamespace(PLATFORM_PROBE=(sys.executable, "-c", "print('Dev L')"))
            real = registry.capability_module
            with mock.patch.object(registry, "capability_module", side_effect=lambda a, b, c: (
                    fake if (a, c) == ("hardware", "execution") else real(a, b, c))), \
                    mock.patch.object(wc.Conductor, "_control_file_module",
                                      side_effect=AssertionError("no control file is shipped")):
                out = n.execute()
            self.assertEqual(out["returncode"], 0, out)
            self.assertEqual(n.calls(), [], "a local site is reached by no transport")
            env = json.loads((n.node_dir / "trial_meta.json").read_text())["environment"]
            self.assertEqual(env["platform"]["gpu"], "Dev L")
            self.assertEqual(env["platform"]["site"], LOCAL_SITE)
            self.assertEqual(env["execution_site"]["host"], None)
            run_log = n.log_entries(n.node_dir / "command_log.jsonl")
            self.assertNotIn("site", run_log[0])
            self.assertEqual(run_log[0]["cwd"],
                             str(n.repo / "workspace" / "tmp" / "arid-1" / "run"))

    def test_a_binary_a_remote_site_built_is_not_run_here(self) -> None:
        """The where-built check (issue #333) at the local site: a target mapped back to
        `local` after a Build at a remote site does not run that binary on this host."""
        with tempfile.TemporaryDirectory() as tmp:
            n = _Node(tmp, site=Site(LOCAL_SITE, ("cpu",)))
            n.built_at("box")
            with mock.patch.object(build_runtime_server, "tool_run_program",
                                   side_effect=AssertionError("nothing runs")):
                result = n.substep()
            self.assertEqual(result.returncode, 1, result)
            self.assertIn(f"was built at site box and target {TARGET_ID} now runs at site "
                          f"{LOCAL_SITE}", result.stderr)


if __name__ == "__main__":
    unittest.main()
