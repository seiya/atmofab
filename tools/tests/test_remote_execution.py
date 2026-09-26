#!/usr/bin/env python3
"""Tests for `tools/remote_execution.py` — the remote executor (issue #293, PR-2).

No network: `ssh` and `scp` are shims placed first on `PATH`. The `ssh` shim drops the options
and the destination and runs the command string with `sh -c` LOCALLY, which is what a real ssh
does with it at the far end; the `scp` shim drops the options, strips `host:` and copies with
`cp -rp`. The site's `workdir` is a directory under the test's own temporary tree, so a job runs
end to end on this machine. Each shim appends its argv to a log, and reads three knobs from the
environment: `SHIM_SSH_FAIL` / `SHIM_SCP_FAIL` (a substring of the command, or `up` / `down` for
scp's direction) makes the call exit 255 or 1 without doing anything, `SHIM_SSH_POST` is a shell
snippet run after a command that runs the job script (to lose or plant a control file), and
`SHIM_SSH_DELAY` makes the job script's call wait that many seconds first.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from tools import execution_sites as es
from tools import remote_execution as rx

_SSH_SHIM = r'''#!/usr/bin/env python3
import os, subprocess, sys, time
with open(os.environ["SHIM_LOG"], "a") as f:
    f.write("ssh\t" + "\t".join(sys.argv[1:]) + "\n")
args = sys.argv[1:]
while args:
    if args[0] == "-o":
        args = args[2:]
    elif args[0] == "--":
        args = args[1:]
        break
    elif args[0].startswith("-"):
        args = args[1:]
    else:
        break
host, command = args[0], " ".join(args[1:])
if "job.sh" in command:
    time.sleep(float(os.environ.get("SHIM_SSH_DELAY") or 0))
fail = os.environ.get("SHIM_SSH_FAIL")
if fail and fail in command:
    sys.stderr.write("shim: connection closed by remote host\n")
    sys.exit(255)
rc = subprocess.run(["sh", "-c", command]).returncode
post = os.environ.get("SHIM_SSH_POST")
if post and "job.sh" in command:
    subprocess.run(["sh", "-c", post], check=True)
sys.exit(rc)
'''

_SCP_SHIM = r'''#!/usr/bin/env python3
import os, re, subprocess, sys
with open(os.environ["SHIM_LOG"], "a") as f:
    f.write("scp\t" + "\t".join(sys.argv[1:]) + "\n")
args = sys.argv[1:]
paths = []
while args:
    a = args.pop(0)
    if a == "-o":
        args.pop(0)
    elif a == "--":
        paths += args
        break
    elif a.startswith("-"):
        pass
    else:
        paths.append(a)
direction = "down" if re.match(r"^[^/:]+:", paths[0]) else "up"
fail = os.environ.get("SHIM_SCP_FAIL")
if fail == direction:
    sys.stderr.write("shim: lost connection\n")
    sys.exit(1)
paths = [re.sub(r"^[^/:]+:", "", p) for p in paths]
sys.exit(subprocess.run(["cp", "-rp", *paths]).returncode)
'''

#: A shipped runner: records its argv and cwd, writes an output file, prints to both streams, and
#: exits with `$RUNNER_RC` (0 when unset).
_RUNNER = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, sys
    json.dump({"argv": sys.argv[1:], "cwd": os.getcwd(), "env": os.environ.get("KNOB")},
              open("argv.json", "w"))
    print("runner stdout")
    print("runner stderr", file=sys.stderr)
    sys.exit(int(os.environ.get("RUNNER_RC") or 0))
''')


class _Harness:
    """A temporary tree with the shims on `PATH`, a remote `workdir`, and a runner to ship."""

    def __init__(self, tmp: str) -> None:
        self.root = Path(tmp)
        self.shims = self.root / "shims"
        self.shims.mkdir()
        for name, body in (("ssh", _SSH_SHIM), ("scp", _SCP_SHIM)):
            p = self.shims / name
            p.write_text(body)
            p.chmod(p.stat().st_mode | stat.S_IXUSR)
        self.workdir = self.root / "remote" / "jobs"
        self.workdir.mkdir(parents=True)
        self.local = self.root / "local"
        self.local.mkdir()
        self.runner = self.local / "runner"
        self.runner.write_text(_RUNNER)
        self.runner.chmod(0o755)
        self.log = self.root / "shim.log"
        self.log.touch()
        self.site = es.Site(site_id="box", executes=("cpu",), host="box",
                            workdir=str(self.workdir), scheduler="none")
        self.job = rx.job_dir(self.site, "orch_1", "arid-1")

    def env(self, **knobs: str) -> mock._patch:
        env = {"PATH": f"{self.shims}{os.pathsep}{os.environ['PATH']}",
               "SHIM_LOG": str(self.log)}
        env.update(knobs)
        return mock.patch.dict(os.environ, env)

    def command(self, tag: str, argv: tuple[str, ...], *, cwd: str = "run",
                env: dict[str, str] | None = None, timeout: int = 60,
                tool: str = "run_program") -> rx.CommandSpec:
        return rx.CommandSpec(
            tag=tag, tool_name=tool, argv=argv, cwd=f"{self.job}/{cwd}", env=env or {},
            timeout_sec=timeout, command_log_path=self.local / "logs" / f"{tag}.jsonl",
            capture_limit=120000)

    def request(self, *commands: rx.CommandSpec, **kw) -> rx.JobRequest:
        if not commands:
            commands = (
                self.command("run", (f"{self.job}/bin/runner", "--cases", "c1"),
                             env={"KNOB": "a b"}),
                self.command("qc", ("sh", "-c", "echo qc-ran; ls ../run"), cwd="src",
                             tool="run_quality_checks"),
            )
        kw.setdefault("ship", {"bin/runner": self.runner})
        kw.setdefault("dirs", ("run/raw",))
        kw.setdefault("attribution", {"orchestration_id": "orch_1", "agent_run_id": "arid-1"})
        return rx.JobRequest(site=self.site, job_dir=self.job, commands=commands, **kw)

    def run(self, request: rx.JobRequest, **knobs: str) -> rx.JobResult:
        with self.env(**knobs):
            return rx.execute_job(request, local_tmp=self.local / "tmp")

    def log_entries(self, tag: str) -> list[dict]:
        p = self.local / "logs" / f"{tag}.jsonl"
        if not p.exists():
            return []
        return [json.loads(line) for line in p.read_text().splitlines()]

    def calls(self) -> list[list[str]]:
        return [line.split("\t") for line in self.log.read_text().splitlines()]


class EndToEndTests(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.h = _Harness(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_two_commands_run_in_order_and_each_is_logged_where_the_caller_said(self) -> None:
        result = self.h.run(self.h.request())
        run, qc = result.results
        self.assertTrue(run["ok"])
        self.assertEqual(run["return_code"], 0)
        self.assertEqual(run["stdout"], "runner stdout\n")
        self.assertEqual(run["stderr"], "runner stderr\n")
        self.assertEqual(run["cwd"], f"{self.h.job}/run")
        self.assertTrue(qc["ok"])
        self.assertIn("qc-ran", qc["stdout"])
        self.assertIn("argv.json", qc["stdout"], "qc ran after run, beside its output")
        # The collected tree holds what the runner wrote, at the path it wrote it.
        recorded = json.loads((result.collected / "run" / "argv.json").read_text())
        self.assertEqual(recorded["argv"], ["--cases", "c1"])
        self.assertEqual(recorded["cwd"], f"{self.h.job}/run")
        self.assertEqual(recorded["env"], "a b")
        self.assertTrue((result.collected / "run" / "raw").is_dir())
        # The remote job directory is gone; its parent stays for the next job.
        self.assertFalse(Path(self.h.job).exists())
        self.assertTrue(Path(self.h.job).parent.is_dir())
        for tag, res, tool in (("run", run, "run_program"), ("qc", qc, "run_quality_checks")):
            (entry,) = self.h.log_entries(tag)
            self.assertEqual(entry["command_id"], res["command_id"])
            self.assertEqual(res["command_log_path"], str(self.h.local / "logs" / f"{tag}.jsonl"))
            self.assertEqual(entry["tool_name"], tool)
            self.assertEqual(entry["version"], 1)
            self.assertTrue(entry["ok"])
            self.assertEqual(entry["return_code"], 0)
            self.assertEqual(entry["cwd"], res["cwd"])
            self.assertEqual(entry["command"], res["command"])
            self.assertEqual(entry["orchestration_id"], "orch_1")
            self.assertEqual(entry["agent_run_id"], "arid-1")
            self.assertEqual(entry["site"], {"site": "box", "host": "box", "scheduler": "none",
                                             "job_id": None, "remote_cwd": res["cwd"]})
            self.assertTrue(entry["started_at_utc"].endswith("Z"))
            self.assertGreaterEqual(entry["elapsed_ms"], 0)
            self.assertEqual(entry["elapsed_ms"] % 1000, 0)
        self.assertEqual(self.h.log_entries("run")[0]["env_override_keys"], ["KNOB"])
        self.assertEqual(result.site_record, {
            "site": "box", "host": "box", "scheduler": "none", "job_id": None,
            "remote_dir": self.h.job, "queue_wait_ms": 0})

    def test_the_entry_has_the_local_servers_shape_plus_site(self) -> None:
        """The local server's entry keys, read by running the server once, plus `site`."""
        server = rx._server()
        with tempfile.TemporaryDirectory() as tmp:
            local = server.tool_run_program({
                "project_dir": tmp, "command": ["true"], "env": {"KNOB": "1"},
                "command_log_path": str(Path(tmp) / "log.jsonl"),
                "orchestration_id": "o", "agent_run_id": "a"})
            local_entry = json.loads((Path(tmp) / "log.jsonl").read_text())
        result = self.h.run(self.h.request())
        (entry,) = self.h.log_entries("run")
        self.assertEqual(set(entry), set(local_entry) | {"site"})
        self.assertEqual(set(result.results[0]) - {"command_log_ref"},
                         set(local) - {"command_log_ref"})

    def test_every_entry_goes_through_the_servers_writer(self) -> None:
        server = rx._server()
        with mock.patch.object(server, "_append_command_log", autospec=True,
                               side_effect=server._append_command_log) as spy:
            self.h.run(self.h.request())
        self.assertEqual([c.args[0] for c in spy.call_args_list],
                         [self.h.local / "logs" / "run.jsonl", self.h.local / "logs" / "qc.jsonl"])

    def test_a_log_under_the_working_directory_gets_a_ref(self) -> None:
        cwd = os.getcwd()
        os.chdir(self.h.local)
        try:
            result = self.h.run(self.h.request())
        finally:
            os.chdir(cwd)
        self.assertEqual(result.results[0]["command_log_ref"], "logs/run.jsonl")

    def test_a_failing_command_stops_the_job_and_is_its_result_not_a_refusal(self) -> None:
        result = self.h.run(self.h.request(), RUNNER_RC="3")
        run, qc = result.results
        self.assertFalse(run["ok"])
        self.assertEqual(run["return_code"], 3)
        self.assertNotIn("error", run)
        self.assertIsNone(qc)
        self.assertEqual(len(self.h.log_entries("run")), 1)
        self.assertEqual(self.h.log_entries("qc"), [])
        self.assertFalse(self.h.log_entries("run")[0]["ok"])

    def test_a_command_the_remote_timeout_ended_is_recorded_as_the_server_records_one(
            self) -> None:
        slow = self.h.command("run", ("python3", "-c", "import time; time.sleep(30)"), timeout=1)
        with mock.patch.object(rx, "KILL_AFTER_SEC", 1):
            result = self.h.run(self.h.request(slow))
        (run,) = result.results
        self.assertFalse(run["ok"])
        self.assertIsNone(run["return_code"])
        self.assertEqual(run["error"], "timeout: exceeded 1 sec")
        (entry,) = self.h.log_entries("run")
        self.assertIsNone(entry["return_code"])
        self.assertEqual(entry["error"], "timeout: exceeded 1 sec")

    def test_a_command_that_exits_124_on_its_own_is_not_a_timeout(self) -> None:
        quick = self.h.command("run", ("sh", "-c", "exit 124"), timeout=60)
        (run,) = self.h.run(self.h.request(quick)).results
        self.assertEqual(run["return_code"], 124)
        self.assertNotIn("error", run)

    def test_the_entry_times_are_the_sites_own(self) -> None:
        post = (f"echo 1700000000 > {self.h.job}/ctl/run.t0; "
                f"echo 1700000007 > {self.h.job}/ctl/run.t1")
        result = self.h.run(self.h.request(), SHIM_SSH_POST=post)
        entry = self.h.log_entries("run")[0]
        self.assertEqual(entry["started_at_utc"], "2023-11-14T22:13:20Z")
        self.assertEqual(entry["ended_at_utc"], "2023-11-14T22:13:27Z")
        self.assertEqual(entry["elapsed_ms"], 7000)
        self.assertTrue(result.results[0]["ok"])

    def test_a_long_command_that_succeeded_is_not_a_timeout(self) -> None:
        """Only a 124 or 137 is read as a timeout, however long the command took."""
        cmd = self.h.command("run", ("true",), timeout=5)
        post = (f"echo 1700000000 > {self.h.job}/ctl/run.t0; "
                f"echo 1700000100 > {self.h.job}/ctl/run.t1")
        (run,) = self.h.run(self.h.request(cmd), SHIM_SSH_POST=post).results
        self.assertEqual(run["return_code"], 0)
        self.assertNotIn("error", run)

    def test_output_is_trimmed_to_the_capture_limit_as_the_server_trims_it(self) -> None:
        cmd = rx.CommandSpec(
            tag="run", tool_name="run_program",
            argv=("python3", "-c", "print('x' * 5000)"), cwd=f"{self.h.job}/run", env={},
            timeout_sec=60, command_log_path=self.h.local / "logs" / "run.jsonl",
            capture_limit=1000)
        (run,) = self.h.run(self.h.request(cmd)).results
        self.assertEqual(run["stdout"], rx._server()._trim("x" * 5000 + "\n", 1000))
        self.assertLess(len(run["stdout"]), 1100)

    def test_argv_and_env_survive_quoting(self) -> None:
        odd = ("c 1", "it's", "$HOME", "a;b", "`x`", "*")
        cmd = self.h.command("run", (f"{self.h.job}/bin/runner", *odd), env={"KNOB": "x=y z"})
        result = self.h.run(self.h.request(cmd))
        recorded = json.loads((result.collected / "run" / "argv.json").read_text())
        self.assertEqual(recorded["argv"], list(odd))
        self.assertEqual(recorded["env"], "x=y z")

    def test_the_shipped_file_keeps_its_mode(self) -> None:
        result = self.h.run(self.h.request())
        self.assertTrue(os.access(result.collected / "bin" / "runner", os.X_OK))

    def test_the_platform_record_comes_from_the_site(self) -> None:
        result = self.h.run(self.h.request())
        self.assertEqual(result.platform["machine"], os.uname().machine)
        self.assertEqual(result.platform["node"], os.uname().nodename)
        self.assertIsNone(result.platform["gpu"])
        self.assertEqual(set(result.platform), {"machine", "node", "cpu_model", "gpu"})

    def test_a_probe_answers_the_device_and_a_failing_probe_answers_none(self) -> None:
        probe = ("sh", "-c", "echo 'Device X, 1.0'; echo second")
        result = self.h.run(self.h.request(platform_probe=probe))
        self.assertEqual(result.platform["gpu"], "Device X, 1.0")
        h2 = _Harness(tempfile.mkdtemp(dir=self._tmp.name))
        result = h2.run(h2.request(platform_probe=("sh", "-c", "echo partial; exit 9")))
        self.assertIsNone(result.platform["gpu"])


class RefusalTests(unittest.TestCase):
    """Every refusal raises before a log entry is written."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.h = _Harness(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _refused(self, pattern: str, request: rx.JobRequest | None = None,
                 **knobs: str):
        with self.assertRaisesRegex(rx.RemoteExecutionError, pattern) as ctx:
            self.h.run(request or self.h.request(), **knobs)
        self.assertEqual(self.h.log_entries("run"), [])
        self.assertEqual(self.h.log_entries("qc"), [])
        return ctx

    def test_an_existing_job_directory_is_a_stale_job_and_is_refused(self) -> None:
        Path(self.h.job).mkdir(parents=True)
        (Path(self.h.job) / "stale").write_text("old")
        self._refused("stale job")
        self.assertEqual((Path(self.h.job) / "stale").read_text(), "old")

    def test_a_lost_status_is_refused_not_read_as_zero(self) -> None:
        for tag in ("run", "qc"):
            with self.subTest(tag=tag):
                h = _Harness(tempfile.mkdtemp(dir=self._tmp.name))
                with self.assertRaisesRegex(rx.RemoteExecutionError,
                                            f"{tag}.rc was not written"):
                    h.run(h.request(), SHIM_SSH_POST=f"rm {h.job}/ctl/{tag}.rc")
                self.assertEqual(h.log_entries("run"), [])

    def test_a_lost_time_is_refused(self) -> None:
        self._refused("run.t1 was not written", SHIM_SSH_POST=f"rm {self.h.job}/ctl/run.t1")

    def test_a_command_that_ended_before_it_started_is_refused(self) -> None:
        self._refused("ended before it started",
                      SHIM_SSH_POST=f"echo 1 > {self.h.job}/ctl/run.t1")

    def test_a_status_that_is_not_an_integer_is_refused(self) -> None:
        for text in ("", "0x", "zero"):
            with self.subTest(text=text):
                h = _Harness(tempfile.mkdtemp(dir=self._tmp.name))
                with self.assertRaisesRegex(rx.RemoteExecutionError, "not an integer"):
                    h.run(h.request(),
                          SHIM_SSH_POST=f"printf '{text}' > {h.job}/ctl/run.rc")

    def test_a_status_for_a_command_that_should_not_have_run_is_refused(self) -> None:
        self._refused("ran although an earlier command failed",
                      RUNNER_RC="1", SHIM_SSH_POST=f"echo 0 > {self.h.job}/ctl/qc.rc")

    def test_a_program_the_site_cannot_find_is_the_hosts_failure(self) -> None:
        missing = self.h.command("run", ("no-such-program-zz",))
        self._refused("run the job script: ssh exited 4", self.h.request(missing))

    def test_a_connection_failure_is_refused_with_the_stage(self) -> None:
        self._refused("create the job directory.*ssh exited 255", SHIM_SSH_FAIL="mkdir")
        self.assertFalse(Path(self.h.job).exists())

    def test_a_failure_to_ship_is_refused(self) -> None:
        self._refused("ship the job's files: scp exited 1", SHIM_SCP_FAIL="up")

    def test_a_failure_to_collect_leaves_the_remote_directory_and_names_it(self) -> None:
        ctx = self._refused("collect the job directory", SHIM_SCP_FAIL="down")
        self.assertIn(self.h.job, str(ctx.exception))
        self.assertTrue((Path(self.h.job) / "run" / "argv.json").is_file())

    def test_a_directory_that_cannot_be_removed_is_refused(self) -> None:
        self._refused("remove the collected job directory", SHIM_SSH_FAIL="rm -rf")

    def test_a_job_that_outlives_its_local_bound_is_refused(self) -> None:
        cmd = self.h.command("run", ("true",), timeout=1)
        with mock.patch.object(rx, "TRANSPORT_GRACE_SEC", 2), \
                mock.patch.object(rx, "KILL_AFTER_SEC", 0):
            ctx = self._refused("run the job script: ssh did not finish within 3 sec",
                                self.h.request(cmd), SHIM_SSH_DELAY="6")
        self.assertIn(self.h.job, str(ctx.exception))


class RequestValidationTests(unittest.TestCase):
    """A malformed request is refused before any transport call."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.h = _Harness(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _invalid(self, pattern: str, request: rx.JobRequest) -> None:
        with self.assertRaisesRegex(ValueError, pattern):
            self.h.run(request)
        self.assertEqual(self.h.calls(), [])

    def test_an_env_the_server_refuses_is_refused_here(self) -> None:
        for env, pattern in (({"LD_PRELOAD": "/x.so"}, "redirect execution"),
                             ({"KNOB": "a;b"}, "reach the make recipe"),
                             ({"A-B": "1"}, "not a variable name")):
            with self.subTest(env=env):
                self._invalid(pattern, self.h.request(self.h.command("run", ("true",), env=env)))

    def test_a_site_this_executor_does_not_run(self) -> None:
        batch = es.Site(site_id="c", executes=("cpu",), host="c", workdir=str(self.h.workdir),
                        scheduler="zz_batch")
        self._invalid("does not implement", rx.JobRequest(
            site=batch, job_dir=self.h.job, ship={}, commands=(self.h.command("r", ("true",)),)))
        local = es.Site(site_id="local", executes=("cpu",))
        self._invalid("not a remote site", rx.JobRequest(
            site=local, job_dir=self.h.job, ship={}, commands=(self.h.command("r", ("true",)),)))

    def test_paths_stay_inside_the_job_directory(self) -> None:
        ok = self.h.command("run", ("true",))
        for kw, pattern in (
                ({"ship": {"ctl/job.sh": self.h.runner}}, "control directory"),
                ({"ship": {"../x": self.h.runner}}, "plain elements"),
                ({"ship": {"/abs": self.h.runner}}, "plain elements"),
                ({"dirs": ("a/../../b",)}, "plain elements"),
                ({"dirs": ("-rf",)}, "plain elements")):
            with self.subTest(kw=kw):
                self._invalid(pattern, self.h.request(ok, **kw))
        for cwd in ("/tmp", f"{self.h.job}/../x", f"{self.h.job}x", f"{self.h.job}//a"):
            with self.subTest(cwd=cwd):
                bad = rx.CommandSpec(tag="run", tool_name="run_program", argv=("true",),
                                     cwd=cwd, env={}, timeout_sec=1,
                                     command_log_path=self.h.local / "l", capture_limit=1000)
                self._invalid("is not under", self.h.request(bad))
        self._invalid("is not under", rx.JobRequest(
            site=self.h.site, job_dir="/elsewhere/x", ship={}, commands=(ok,)))
        self._invalid("workdir itself", rx.JobRequest(
            site=self.h.site, job_dir=str(self.h.workdir), ship={}, commands=(ok,)))

    def test_commands_are_well_formed(self) -> None:
        for kw, pattern in (({"tag": "Run"}, "lowercase token"),
                            ({"argv": ()}, "empty argv"),
                            ({"timeout_sec": 0}, "timeout_sec"),
                            ({"timeout_sec": True}, "timeout_sec")):
            with self.subTest(kw=kw):
                base = {"tag": "run", "tool_name": "run_program", "argv": ("true",),
                        "cwd": f"{self.h.job}/run", "env": {}, "timeout_sec": 1,
                        "command_log_path": self.h.local / "l", "capture_limit": 1000}
                base.update(kw)
                self._invalid(pattern, self.h.request(rx.CommandSpec(**base)))
        dup = self.h.command("run", ("true",))
        self._invalid("repeat", self.h.request(dup, dup))
        self._invalid("at least one", rx.JobRequest(site=self.h.site, job_dir=self.h.job,
                                                    ship={}, commands=()))

    def test_a_used_local_directory_is_refused(self) -> None:
        (self.h.local / "tmp" / "collected").mkdir(parents=True)
        self._invalid("already exists", self.h.request())

    def test_job_dir_takes_single_elements(self) -> None:
        self.assertEqual(rx.job_dir(self.h.site, "o", "a"), f"{self.h.workdir}/o/a")
        for orch, arid in (("o/x", "a"), ("o", ".."), ("o", "-a"), ("", "a")):
            with self.subTest(orch=orch, arid=arid):
                self.assertRaises(ValueError, rx.job_dir, self.h.site, orch, arid)
        with self.assertRaisesRegex(ValueError, "not a remote site"):
            rx.job_dir(es.Site(site_id="local", executes=("cpu",)), "o", "a")


class ScriptTests(unittest.TestCase):

    def test_the_script_is_posix_sh_that_parses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = _Harness(tmp)
            script = rx.render_job_script(h.request(platform_probe=("probe", "--x")))
            path = Path(tmp) / "job.sh"
            path.write_text(script)
            self.assertEqual(
                subprocess.run(["sh", "-n", str(path)], check=False).returncode, 0)
        self.assertTrue(script.startswith("#!/bin/sh\n"))
        for prog in rx.REMOTE_EXECUTABLES:
            self.assertIn(f"command -v {prog} ", script)


if __name__ == "__main__":
    unittest.main()
