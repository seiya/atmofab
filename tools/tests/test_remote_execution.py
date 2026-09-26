#!/usr/bin/env python3
"""Tests for `tools/remote_execution.py` — the remote executor (issue #293, PR-2).

No network: `ssh` and `scp` are shims placed first on `PATH`. The `ssh` shim refuses a call
without `BatchMode=yes` (a real one could prompt and hang), drops the options and the
destination, and runs the command string with `sh -c` LOCALLY in the test's working directory —
a real ssh hands it to the user's login shell in the home directory. The executor needs that
shell to be POSIX-family (its commands are `sh` command lines) and does not depend on the
directory (argv[0] is absolute or a PATH name, every other path absolute). The `scp` shim drops
the options, strips `host:`, and copies with `cp`, refusing a directory without `-r` as scp
does; like scp it keeps a file's execute bit. The site's `workdir` is a directory under the
test's own temporary tree, so a job runs end to end on this machine. Each shim appends its argv to a log, and reads these knobs from the
environment: `SHIM_SSH_FAIL` / `SHIM_SCP_FAIL` (a substring of the command, or `up` / `down` for
scp's direction) makes the call exit 255 or 1 without doing anything, `SHIM_SSH_POST` is a shell
snippet run after the call that runs the job script (the one whose command carries a status
marker) (to plant or lose a file),
`SHIM_SSH_SUB` is a JSON `[pattern, replacement]` applied with `re.sub` to the job script's stdout
(to lose, forge or alter a status line), `SHIM_SSH_PATH` is the PATH every call runs under
(to take a tool away from the "site"), and `SHIM_SSH_DELAY` makes the job script's call wait
that many seconds first. A job script killed mid-run makes the shim exit non-zero (137 for a
SIGKILL), where a real ssh exits 255: the rows match the stage, not the number.

A `slurm` site's job runs under `srun`, which is a shim too: it logs its argv, skips its options,
and runs the rest with `SLURM_JOB_ID=4242` added to the environment, relaying the task's stdout
and exit status as a real one does (measured on Slurm 20.02). Its knobs: `SHIM_SRUN_QUEUE` waits
that many seconds first (a queued job), `SHIM_SRUN_DENY` exits 1 with srun's words for an
allocation `--immediate` gave up on, `SHIM_SRUN_KILLED` runs the task and then exits 143 as a job
the scheduler killed does, and `SHIM_SRUN_NO_ID` leaves the job id unset.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
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
import json, os, re, subprocess, sys, time
with open(os.environ["SHIM_LOG"], "a") as f:
    f.write(json.dumps(["ssh", *sys.argv[1:]]) + "\n")
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
if "BatchMode=yes" not in sys.argv:
    sys.stderr.write("shim: no BatchMode=yes, a real ssh could prompt\n")
    sys.exit(97)
host, command = args[0], " ".join(args[1:])
if "atmofab-status" in command:
    time.sleep(float(os.environ.get("SHIM_SSH_DELAY") or 0))
fail = os.environ.get("SHIM_SSH_FAIL")
if fail and fail in command:
    sys.stderr.write("shim: connection closed by remote host\n")
    sys.exit(255)
env = dict(os.environ)
if os.environ.get("SHIM_SSH_PATH"):
    env["PATH"] = os.environ["SHIM_SSH_PATH"]
proc = subprocess.run(["sh", "-c", command], stdout=subprocess.PIPE, text=True, env=env)
out, rc = proc.stdout, proc.returncode
sub = os.environ.get("SHIM_SSH_SUB")
if sub and "atmofab-status" in command:
    pattern, repl = json.loads(sub)
    out = re.sub(pattern, repl, out, flags=re.M)
sys.stdout.write(out)
post = os.environ.get("SHIM_SSH_POST")
if post and "atmofab-status" in command:
    subprocess.run(["sh", "-c", post], check=True)
sys.exit(rc)
'''

_SCP_SHIM = r'''#!/usr/bin/env python3
import json, os, re, subprocess, sys
with open(os.environ["SHIM_LOG"], "a") as f:
    f.write(json.dumps(["scp", *sys.argv[1:]]) + "\n")
args = sys.argv[1:]
paths = []
recursive = False
while args:
    a = args.pop(0)
    if a == "-o":
        args.pop(0)
    elif a == "--":
        paths += args
        break
    elif a.startswith("-"):
        recursive = recursive or "r" in a
    else:
        paths.append(a)
direction = "down" if re.match(r"^[^/:]+:", paths[0]) else "up"
fail = os.environ.get("SHIM_SCP_FAIL")
if fail == direction:
    sys.stderr.write("shim: lost connection\n")
    sys.exit(1)
paths = [re.sub(r"^[^/:]+:", "", p) for p in paths]
if not recursive and any(os.path.isdir(p) for p in paths[:-1]):
    sys.stderr.write("shim: not a regular file\n")
    sys.exit(1)
sys.exit(subprocess.run(["cp", *(["-r"] if recursive else []), *paths]).returncode)
'''

_SRUN_SHIM = r'''#!/usr/bin/env python3
import json, os, subprocess, sys, time
with open(os.environ["SHIM_LOG"], "a") as f:
    f.write(json.dumps(["srun", *sys.argv[1:]]) + "\n")
args = sys.argv[1:]
while args and args[0].startswith("-"):
    args = args[2:] if args[0] in ("-p", "-t", "-J", "-N", "-n") else args[1:]
if os.environ.get("SHIM_SRUN_DENY"):
    sys.stderr.write("srun: error: Unable to allocate resources: Requested nodes are busy\n")
    sys.exit(1)
time.sleep(float(os.environ.get("SHIM_SRUN_QUEUE") or 0))
env = dict(os.environ)
if not os.environ.get("SHIM_SRUN_NO_ID"):
    env["SLURM_JOB_ID"] = "4242"
rc = subprocess.run(args, env=env).returncode
if os.environ.get("SHIM_SRUN_KILLED"):
    sys.stderr.write("slurmstepd: error: *** JOB 4242 CANCELLED DUE TO TIME LIMIT ***\n")
    sys.exit(143)
sys.exit(rc)
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
        for name, body in (("ssh", _SSH_SHIM), ("scp", _SCP_SHIM), ("srun", _SRUN_SHIM)):
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
            tag=tag, tool_name=tool, argv=argv, cwd=f"{self.job}/{cwd}",
            record_cwd=str(self.local / "cwd" / cwd), env=env or {},
            timeout_sec=timeout, command_log_path=self.local / "logs" / f"{tag}.jsonl",
            capture_limit=120000)

    def request(self, *commands: rx.CommandSpec, **kw) -> rx.JobRequest:
        if not commands:
            commands = (
                self.command("run", (f"{self.job}/bin/runner", "--cases", "c1"),
                             env={"ZED": "1", "KNOB": "a b"}),
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
        return [json.loads(line) for line in self.log.read_text().splitlines()]


#: What the job script and the shipped runner execute at the site.
_SITE_TOOLS = ("sh", "uname", "hostname", "grep", "sed", "mkdir", "rm", "date", "env", "timeout", "ls",
               "python3")


def _bare_path(root: Path, *, without: str, sh: str = "sh") -> Path:
    """A directory to use as the site's PATH: the tools the job needs, less `without`, with
    `sh` being the program named `sh` (a site whose `/bin/sh` is bash: `sh="bash"`)."""
    bare = root / f"path_without_{without}_{sh}"
    bare.mkdir()
    for tool in _SITE_TOOLS:
        found = shutil.which(sh if tool == "sh" else tool)
        if tool != without and found:
            (bare / tool).symlink_to(found)
    if without:
        assert shutil.which(without, path=str(bare)) is None
    return bare


def _local_cpu_model() -> str | None:
    for line in Path("/proc/cpuinfo").read_text(errors="replace").splitlines():
        if line.startswith("model name"):
            return line.split(":", 1)[1].strip() or None
    return None


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
        # The result and the entry record the LOCAL directory the command stands for.
        self.assertEqual(run["cwd"], str(self.h.local / "cwd" / "run"))
        self.assertTrue(qc["ok"])
        self.assertIn("qc-ran", qc["stdout"])
        self.assertIn("argv.json", qc["stdout"], "qc ran after run, beside its output")
        # The collected tree holds what the runner wrote, at the path it wrote it.
        recorded = json.loads((result.collected / "run" / "argv.json").read_text())
        self.assertEqual(recorded["argv"], ["--cases", "c1"])
        self.assertEqual(recorded["cwd"], f"{self.h.job}/run")
        self.assertEqual(recorded["env"], "a b")
        self.assertTrue((result.collected / "run" / "raw").is_dir())
        # The remote job directory is gone, and so is the orchestration's directory above it,
        # which held no other job (the next job makes it again).
        self.assertFalse(Path(self.h.job).exists())
        self.assertFalse(Path(self.h.job).parent.exists())
        self.assertTrue(self.h.workdir.is_dir())
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
            self.assertEqual(entry["site"], {
                "site": "box", "host": "box", "scheduler": "none", "job_id": None,
                "remote_cwd": f"{self.h.job}/{'run' if tag == 'run' else 'src'}",
                "remote_command": list(self.h.request().commands[
                    ["run", "qc"].index(tag)].argv)})
            self.assertTrue(entry["started_at_utc"].endswith("Z"))
            self.assertGreaterEqual(entry["elapsed_ms"], 0)
            self.assertEqual(entry["elapsed_ms"] % 1000, 0)
        self.assertEqual(self.h.log_entries("run")[0]["env_override_keys"], ["KNOB", "ZED"])
        self.assertEqual(result.site_record, {
            "site": "box", "host": "box", "scheduler": "none", "job_id": None,
            "remote_dir": self.h.job, "queue_wait_ms": 0})


    def test_the_orchestrations_directory_stays_while_another_job_is_in_it(self) -> None:
        sibling = Path(self.h.job).parent / "arid-other"
        sibling.mkdir(parents=True)
        self.h.run(self.h.request())
        self.assertFalse(Path(self.h.job).exists())
        self.assertTrue(sibling.is_dir())
    def test_every_transport_call_carries_the_options_and_ends_them(self) -> None:
        """No prompt (a prompt hangs a run), a bounded connect, and `--` before the destination
        and the paths, on every ssh and scp call; scp copies directories recursively."""
        self.assertIn("BatchMode=yes", rx.SSH_OPTIONS)
        self.assertIn("ConnectTimeout=30", rx.SSH_OPTIONS)
        self.h.run(self.h.request())
        calls = self.h.calls()
        self.assertEqual([c[0] for c in calls], ["ssh", "scp", "ssh", "scp", "ssh"])
        n = len(rx.SSH_OPTIONS)
        for call in calls:
            args = call[1:]
            with self.subTest(call=call[:3]):
                if call[0] == "ssh":
                    self.assertEqual(args[:n + 1], [*rx.SSH_OPTIONS, "--"])
                    self.assertEqual(args[n + 1], "box")
                else:
                    self.assertEqual(args[:2], ["-q", "-r"])
                    self.assertEqual(args[2:n + 3], [*rx.SSH_OPTIONS, "--"])

    def test_the_entry_names_each_shipped_file_by_its_local_source(self) -> None:
        """What the post-execute gate binds: `command[0]` is the node's own built binary, not
        its copy at the site, which the site record names instead."""
        result = self.h.run(self.h.request())
        (entry,) = self.h.log_entries("run")
        self.assertEqual(entry["command"], [str(self.h.runner), "--cases", "c1"])
        self.assertEqual(entry["executed_command"], f"{self.h.runner} --cases c1")
        self.assertEqual(result.results[0]["command"], entry["command"])
        self.assertEqual(result.results[0]["executed_command"], entry["executed_command"])
        self.assertEqual(entry["site"]["remote_command"],
                         [f"{self.h.job}/bin/runner", "--cases", "c1"])
        # A word that is no shipped file is left as it is.
        (qc_entry,) = self.h.log_entries("qc")
        self.assertEqual(qc_entry["command"], ["sh", "-c", "echo qc-ran; ls ../run"])

    def test_the_post_execute_gate_accepts_the_entries_it_binds(self) -> None:
        """The real post-execute gate functions over the two entries this executor wrote, in a
        synthetic tree where the shipped runner IS the node's build `bin/` file and the shipped
        control file IS its source `src/` one: no violation. Each entry with its site value put
        back — `command` from `site.remote_command`, `cwd` from `site.remote_cwd` — is refused."""
        import tools.validate_pipeline_semantics as vps

        repo = self.h.root / "repo"
        pipeline = repo / "workspace" / "pipelines" / "p"
        bin_dir = pipeline / "binary" / "b1" / "bin"
        bin_dir.mkdir(parents=True)
        runner = bin_dir / "runner"
        runner.write_text(_RUNNER)
        runner.chmod(0o755)
        src_dir = pipeline / "source" / "s1" / "src"
        src_dir.mkdir(parents=True)
        (src_dir / "Makefile").write_text("test:\n\t@echo quality check ran\n")
        (src_dir.parent / "source_meta.json").write_text('{"verification_status": "pass"}')
        ir = repo / "workspace" / "ir" / "n" / "spec.ir.yaml"
        ir.parent.mkdir(parents=True)
        ir.write_text("{}\n")
        node_dir = pipeline / "runs" / "r1" / "n"
        run_log, qc_log = node_dir / "command_log.jsonl", src_dir / "command_log.jsonl"
        run = rx.CommandSpec(
            tag="run", tool_name="run_program",
            argv=(f"{self.h.job}/bin/runner", "--cases", f"{self.h.job}/ir/spec.ir.yaml", "c1"),
            cwd=f"{self.h.job}/run", record_cwd=str(repo / "workspace" / "tmp" / "run"), env={},
            timeout_sec=60, command_log_path=run_log, capture_limit=120000)
        qc = rx.CommandSpec(
            tag="qc", tool_name="run_quality_checks", argv=("make", "test"),
            cwd=f"{self.h.job}/src", record_cwd=str(src_dir), env={}, timeout_sec=60,
            command_log_path=qc_log, capture_limit=120000)
        result = self.h.run(self.h.request(run, qc, ship={
            "bin/runner": runner, "ir/spec.ir.yaml": ir, "src/Makefile": src_dir / "Makefile"}))
        self.assertTrue(result.results[1]["ok"], result.results[1])
        (node_dir / "trial_meta.json").write_text(json.dumps({
            "source_binary_id": "b1", "source_source_id": "s1",
            "source_command_ref": [
                {"command_id": r["command_id"], "command_log_ref": log.relative_to(repo).as_posix()}
                for r, log in zip(result.results, (run_log, qc_log))]}))
        execution = vps.NodeExecution(node_key="n", node_dir=node_dir, exec_dir=node_dir,
                                      pipeline_dir=pipeline)

        def violations() -> list[str]:
            found: list[str] = []
            vps._validate_run_program_inputs(repo, execution, found)
            vps._validate_quality_check_commands(repo, execution, found)
            return found

        with mock.patch.object(vps, "_target_toolchain_from_pipeline_dir", autospec=True,
                               return_value=("make", "fortran")):
            self.assertEqual(violations(), [])
            for log, key, site_key, refusal in (
                    (run_log, "command", "remote_command", "Mixed-build attribution"),
                    (qc_log, "cwd", "remote_cwd", "must run inside source/<source_id>/src")):
                with self.subTest(key=key):
                    kept = log.read_text()
                    entry = json.loads(kept)
                    entry[key] = entry["site"][site_key]
                    log.write_text(json.dumps(entry) + "\n")
                    (found,) = violations()
                    self.assertIn(refusal, found)
                    log.write_text(kept)

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

    def test_a_command_that_ignores_term_is_killed_and_recorded_as_a_timeout(self) -> None:
        stubborn = self.h.command("run", ("python3", "-c", (
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "time.sleep(30)")), timeout=1)
        # The job's local bound (1 + 1 + 2 sec) is far below the 30 sec the command would take
        # if nothing KILLed it.
        with mock.patch.object(rx, "KILL_AFTER_SEC", 1), \
                mock.patch.object(rx, "TRANSPORT_GRACE_SEC", 2):
            (run,) = self.h.run(self.h.request(stubborn)).results
        self.assertIsNone(run["return_code"])
        self.assertEqual(run["error"], "timeout: exceeded 1 sec")

    def test_a_command_that_exits_124_on_its_own_is_not_a_timeout(self) -> None:
        quick = self.h.command("run", ("sh", "-c", "exit 124"), timeout=60)
        (run,) = self.h.run(self.h.request(quick)).results
        self.assertEqual(run["return_code"], 124)
        self.assertNotIn("error", run)

    def test_the_entry_times_are_the_sites_own(self) -> None:
        sub = json.dumps([r"^(atmofab-status run 0) \d+ \d+$", r"\1 1700000000 1700000007"])
        result = self.h.run(self.h.request(), SHIM_SSH_SUB=sub)
        entry = self.h.log_entries("run")[0]
        self.assertEqual(entry["started_at_utc"], "2023-11-14T22:13:20Z")
        self.assertEqual(entry["ended_at_utc"], "2023-11-14T22:13:27Z")
        self.assertEqual(entry["elapsed_ms"], 7000)
        self.assertTrue(result.results[0]["ok"])

    def test_a_long_command_that_succeeded_is_not_a_timeout(self) -> None:
        """Only a 124 or 137 is read as a timeout, however long the command took."""
        cmd = self.h.command("run", ("true",), timeout=5)
        sub = json.dumps([r"^(atmofab-status run 0) \d+ \d+$", r"\1 1700000000 1700000100"])
        (run,) = self.h.run(self.h.request(cmd), SHIM_SSH_SUB=sub).results
        self.assertEqual(run["return_code"], 0)
        self.assertNotIn("error", run)

    def test_output_is_trimmed_to_the_capture_limit_as_the_server_trims_it(self) -> None:
        cmd = rx.CommandSpec(
            tag="run", tool_name="run_program",
            argv=("python3", "-c", "print('x' * 5000)"), cwd=f"{self.h.job}/run", record_cwd="/local/run", env={},
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

    def test_a_site_without_one_platform_tool_loses_that_fact_only(self) -> None:
        bare = _bare_path(self.h.root, without="hostname")
        result = self.h.run(self.h.request(), SHIM_SSH_PATH=str(bare))
        self.assertEqual(result.platform["machine"], os.uname().machine)
        self.assertIsNone(result.platform["node"])
        self.assertEqual(result.platform["cpu_model"], _local_cpu_model())

    def test_a_login_banner_without_a_newline_is_not_read(self) -> None:
        result = self.h.run(self.h.request(), SHIM_SSH_SUB=json.dumps([r"\A", "Last login: x"]))
        self.assertTrue(result.results[0]["ok"])
        self.assertEqual(result.platform["machine"], os.uname().machine)

    def test_a_platform_value_is_recorded_without_surrounding_space(self) -> None:
        result = self.h.run(self.h.request(platform_probe=("echo", "  Dev X  ")))
        self.assertEqual(result.platform["gpu"], "Dev X")

    def test_a_probe_answer_is_recorded_as_printed(self) -> None:
        """A backslash sequence in a device name is not interpreted (`echo` under dash would)."""
        result = self.h.run(self.h.request(platform_probe=("printf", "%s\n", "Dev\\c X")))
        self.assertEqual(result.platform["gpu"], "Dev\\c X")

    def test_a_probe_that_hangs_answers_none_within_its_bound(self) -> None:
        hang = ("python3", "-c", "import time; time.sleep(60)")
        with mock.patch.object(rx, "PROBE_TIMEOUT_SEC", 1), \
                mock.patch.object(rx, "TRANSPORT_GRACE_SEC", 20):
            cmd = self.h.command("run", ("true",), timeout=1)
            result = self.h.run(self.h.request(cmd, platform_probe=hang))
        self.assertIsNone(result.platform["gpu"])

    def test_attribution_records_the_two_ids_and_nothing_else(self) -> None:
        """As the server's `_attribution` does: an attribution key cannot overwrite a field."""
        request = self.h.request(attribution={"orchestration_id": "o", "agent_run_id": "a",
                                              "ok": True, "return_code": 0})
        self.h.run(request, RUNNER_RC="3")
        (entry,) = self.h.log_entries("run")
        self.assertEqual((entry["ok"], entry["return_code"]), (False, 3))
        self.assertEqual((entry["orchestration_id"], entry["agent_run_id"]), ("o", "a"))

    def test_a_probe_answers_the_device_and_a_failing_probe_answers_none(self) -> None:
        # Only the first line is the device: a later one is never read as a platform line.
        probe = ("sh", "-c", "echo 'Device X, 1.0'; echo 'atmofab-platform gpu second'")
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
        self._refused(f"create the job directory: ssh exited 1 .*a stale job directory exists: "
                      f"{self.h.job}")
        self.assertEqual((Path(self.h.job) / "stale").read_text(), "old")

    def test_a_lost_status_is_refused_not_read_as_zero(self) -> None:
        for tag in ("run", "qc"):
            with self.subTest(tag=tag):
                h = _Harness(tempfile.mkdtemp(dir=self._tmp.name))
                with self.assertRaisesRegex(rx.RemoteExecutionError,
                                            f"'{tag}': no status line"):
                    h.run(h.request(),
                          SHIM_SSH_SUB=json.dumps([rf"^atmofab-status {tag} .*$", ""]))
                self.assertEqual(h.log_entries("run"), [])
                # Refused in step 5, before the removal: the message's "left" is true.
                self.assertTrue(Path(h.job).is_dir())

    def test_a_command_that_ended_before_it_started_is_refused(self) -> None:
        self._refused("ended before it started", SHIM_SSH_SUB=json.dumps(
            [r"^(atmofab-status run 0) \d+ \d+$", r"\1 1700000009 1700000000"]))

    def test_a_status_line_that_does_not_parse_is_refused(self) -> None:
        for bad in ("x", "0x", "", "0 1"):
            with self.subTest(bad=bad):
                h = _Harness(tempfile.mkdtemp(dir=self._tmp.name))
                with self.assertRaisesRegex(rx.RemoteExecutionError, "does not parse"):
                    h.run(h.request(), SHIM_SSH_SUB=json.dumps(
                        [r"^(atmofab-status run) 0 (\d+) (\d+)$", rf"\1 {bad} \2 \3"]))

    def test_a_second_status_line_for_a_command_is_refused(self) -> None:
        self._refused("2 status lines", SHIM_SSH_SUB=json.dumps(
            [r"^(atmofab-status run .*)$", r"\1\n\1"]))

    def test_a_status_line_for_no_command_of_the_job_is_refused(self) -> None:
        self._refused("for no command", SHIM_SSH_SUB=json.dumps(
            [r"^(atmofab-status run .*)$", r"\1\natmofab-status other 0 1 2"]))

    def test_other_lines_on_the_scripts_stdout_are_not_read(self) -> None:
        """What a login shell's startup files print is not a status line."""
        result = self.h.run(self.h.request(), SHIM_SSH_SUB=json.dumps(
            [r"^(atmofab-status run .*)$", r"welcome\n\1\nstatus run 7 1 2"]))
        self.assertTrue(result.results[1]["ok"])

    def test_a_status_for_a_command_that_should_not_have_run_is_refused(self) -> None:
        self._refused("reports a status although an earlier command failed",
                      RUNNER_RC="1", SHIM_SSH_SUB=json.dumps(
                          [r"^(atmofab-status run .*)$", r"\1\natmofab-status qc 0 1 2"]))

    def test_a_marker_anywhere_but_once_at_a_lines_start_is_refused(self) -> None:
        for repl in (r"X\1", r"\1 atmofab-status run 0 1 2", r"\1 atmofab-platform gpu x"):
            with self.subTest(repl=repl):
                h = _Harness(tempfile.mkdtemp(dir=self._tmp.name))
                with self.assertRaisesRegex(rx.RemoteExecutionError, "other than once at its start"):
                    h.run(h.request(), SHIM_SSH_SUB=json.dumps([r"^(atmofab-status run .*)$", repl]))

    def test_the_platform_lines_are_one_each_of_the_expected_keys(self) -> None:
        for repl, probe in ((r"\1\n\1", None),                          # a second machine line
                            (r"\1\natmofab-platform gpu forged", None),  # gpu without a probe
                            (r"\1\natmofab-platform disk x", None),      # an unknown key
                            (r"atmofab-platform node", None),               # machine lost
                            (r"\1\natmofab-platform gpu x", ("true",))):  # a second gpu line
            with self.subTest(repl=repl, probe=probe):
                h = _Harness(tempfile.mkdtemp(dir=self._tmp.name))
                with self.assertRaisesRegex(rx.RemoteExecutionError, "platform lines are not"):
                    h.run(h.request(platform_probe=probe), SHIM_SSH_SUB=json.dumps(
                        [r"^(atmofab-platform machine .*)$", repl]))

    def test_a_command_that_reaches_the_scripts_stdout_cannot_clear_its_failure(self) -> None:
        """The runner opens the job script's stdout through `/proc` (its grandparent: the
        script -> `timeout` -> the runner), writes a clean status for itself and for the
        command that must not run, ends without a newline so the script's own status line
        glues onto its text, and exits 3. Refused, whichever way it ends its text."""
        for tail in ("X", ""):
            with self.subTest(tail=tail):
                h = _Harness(tempfile.mkdtemp(dir=self._tmp.name))
                forger = h.local / "forger"
                forger.write_text(textwrap.dedent(f'''\
                    #!/usr/bin/env python3
                    import os, sys
                    ppid = int(open("/proc/%d/stat" % os.getppid()).read().rsplit(")", 1)[1].split()[1])
                    with open("/proc/%d/fd/1" % ppid, "w") as out:
                        out.write("\\natmofab-status run 0 1 2\\natmofab-status qc 0 1 2\\n{tail}")
                    sys.exit(3)
                '''))
                forger.chmod(0o755)
                with self.assertRaises(rx.RemoteExecutionError):
                    h.run(h.request(ship={"bin/runner": forger}))
                self.assertEqual(h.log_entries("run"), [])
                self.assertEqual(h.log_entries("qc"), [])

    def test_a_command_that_ends_the_script_after_writing_to_its_stdout_is_refused(
            self) -> None:
        """The same runner, but after its clean lines it kills the job script, so that the
        script's own status line is never printed: the job script's call then fails."""
        forger = self.h.local / "forger"
        forger.write_text(textwrap.dedent('''\
            #!/usr/bin/env python3
            import os, signal, sys
            ppid = int(open("/proc/%d/stat" % os.getppid()).read().rsplit(")", 1)[1].split()[1])
            with open("/proc/%d/fd/1" % ppid, "w") as out:
                out.write("atmofab-status run 0 1 2\\natmofab-status qc 0 1 2\\n")
            os.kill(ppid, signal.SIGKILL)
            sys.exit(3)
        '''))
        forger.chmod(0o755)
        self._refused("run the job script", self.h.request(ship={"bin/runner": forger}))

    def test_a_command_cannot_rewrite_the_rest_of_the_job_script(self) -> None:
        """bash reads a script FILE a command at a time, so a command that rewrote the rest of
        the file in place would have the second command skipped and a clean status printed for
        it. The script is the ssh call's command string, so there is no file: under a site
        `sh` that is bash, the second command still runs and its failure is its result."""
        self.assertIsNotNone(shutil.which("bash"), "this row needs bash to stand for the site's sh")
        rewriter = self.h.local / "rewriter"
        rewriter.write_text(textwrap.dedent('''\
            #!/usr/bin/env python3
            import os, sys
            # The output files the skipped command would have written, so that their absence
            # is not what refuses the job.
            for name in ("qc.stdout", "qc.stderr"):
                open(os.path.join("..", "ctl", name), "w").close()
            # Wherever the script might sit as a file under the job directory (not this file).
            me = os.path.realpath(sys.argv[0])
            paths = [os.path.join(d, f) for d, _, fs in os.walk("..") for f in fs
                     if os.path.realpath(os.path.join(d, f)) != me]
            for path in paths:
                try:
                    text = open(path).read()
                except (OSError, UnicodeDecodeError):
                    continue
                if text.count('if [ "$rc" = 0 ]') >= 2:
                    first = text.index('if [ "$rc" = 0 ]')
                    i = text.index('if [ "$rc" = 0 ]', first + 1)
                    new = 'echo "atmofab-status qc 0 1 1"; exit 0\\n'
                    with open(path, "r+") as f:
                        f.seek(i)
                        f.write(new.ljust(len(text) - i))
        '''))
        rewriter.chmod(0o755)
        qc = self.h.command("qc", ("sh", "-c", "exit 1"), cwd="src", tool="run_quality_checks")
        run = self.h.command("run", (f"{self.h.job}/bin/runner",))
        bash_site = _bare_path(self.h.root, without="", sh="bash")
        result = self.h.run(self.h.request(run, qc, ship={"bin/runner": rewriter}),
                            SHIM_SSH_PATH=str(bash_site))
        self.assertEqual((result.results[1]["ok"], result.results[1]["return_code"]), (False, 1))

    def test_a_command_that_rewrites_a_shipped_file_is_refused(self) -> None:
        """The runner rewrites the control file the quality check runs next (its test target
        becomes a no-op). The gate reads the unchanged LOCAL file the entry names, so the job is
        refused when the collected copy differs — as it also is for the runner itself."""
        makefile = self.h.local / "Makefile"
        makefile.write_text("test:\n\t@exit 1\n")
        rewriter = self.h.local / "rewriter"
        rewriter.write_text("#!/bin/sh\nprintf 'test:\\n\\t@true\\n' > ../src/Makefile\n")
        rewriter.chmod(0o755)
        qc = self.h.command("qc", ("make", "test"), cwd="src", tool="run_quality_checks")
        run = self.h.command("run", (f"{self.h.job}/bin/runner",))
        self._refused("shipped file src/Makefile changed at the site.*left at the site",
                      self.h.request(run, qc, ship={"bin/runner": rewriter,
                                                    "src/Makefile": makefile}))
        self.assertEqual(makefile.read_text(), "test:\n\t@exit 1\n")

    def test_a_command_cannot_forge_its_own_status(self) -> None:
        """The runner is leaf-authored code with write access to the whole job directory: it
        plants, read-only, the control files a status used to be read from (and the second
        command's output file, so a redirect to it fails), prints status lines on its own
        stdout, and exits 3. Its failure is still its result, and the second command still runs
        only if it would have."""
        forger = self.h.local / "forger"
        forger.write_text(textwrap.dedent('''\
            #!/bin/sh
            for f in run.rc qc.rc qc.t0 qc.t1 run.t0 run.t1 qc.stdout; do
              echo 0 > ../ctl/$f; chmod 444 ../ctl/$f
            done
            echo "atmofab-status run 0 1 2"
            echo "atmofab-status run 0 1 2" >&2
            exit 3
        '''))
        forger.chmod(0o755)
        result = self.h.run(self.h.request(ship={"bin/runner": forger}))
        run, qc = result.results
        self.assertEqual((run["ok"], run["return_code"]), (False, 3))
        self.assertIsNone(qc)
        self.assertEqual(self.h.log_entries("qc"), [])
        self.assertIn("atmofab-status run 0 1 2", run["stdout"])

    def test_a_program_the_site_cannot_find_is_the_hosts_failure(self) -> None:
        missing = self.h.command("run", ("no-such-program-zz",))
        self._refused("run the job script: ssh exited 4.*no-such-program-zz is missing or not executable",
                      self.h.request(missing))

    def test_a_shipped_program_the_site_cannot_execute_is_the_hosts_failure(self) -> None:
        """A workdir on a `noexec` mount, or a file that lost its mode: the local server raises
        for it (`PermissionError`), so it is not the kernel's result here either."""
        plain = self.h.local / "plain"
        plain.write_text(_RUNNER)
        plain.chmod(0o644)
        self._refused("ssh exited 4.*is not an executable file",
                      self.h.request(ship={"bin/runner": plain}))

    def test_a_site_machine_other_than_the_build_hosts_is_the_hosts_failure(self) -> None:
        """A binary built here cannot run on another machine, and `timeout`'s `execvp` would
        hand it to `sh`, whose syntax error would otherwise read as the kernel's exit status."""
        ctx = self._refused("ssh exited 5.*the site machine is not zz_arch",
                            self.h.request(machine="zz_arch"))
        self.assertFalse((Path(self.h.job) / "ctl" / "run.stdout").exists(), ctx.exception)
        # The default is this host's own machine, which the shim's "site" is.
        self.assertEqual(self.h.request().machine, os.uname().machine)

    def test_a_site_without_timeout_is_the_hosts_failure(self) -> None:
        bare = _bare_path(self.h.root, without="timeout")
        self._refused("(?s)ssh exited 3.*timeout is missing", SHIM_SSH_PATH=str(bare))

    def test_a_timeout_that_does_not_take_kill_after_is_the_hosts_failure(self) -> None:
        bare = _bare_path(self.h.root, without="timeout")
        (bare / "timeout").write_text(
            '#!/bin/sh\n[ "$1" = -k ] && { echo "timeout: invalid option -- k" >&2; exit 1; }\n'
            'exec "$@"\n')
        (bare / "timeout").chmod(0o755)
        self._refused("(?s)ssh exited 3.*timeout does not take -k", SHIM_SSH_PATH=str(bare))

    def test_a_program_that_did_not_start_is_the_hosts_failure(self) -> None:
        """126 / 127: a missing interpreter, a missing shared library at the site, a program
        `timeout` could not execute. The stderr tail is in the message."""
        loader = "#!/bin/sh\necho 'error while loading shared libraries' >&2\nexit 127\n"
        for body, rc, tail in (("#!/nonexistent/interpreter\n", "12[67]", "bin/runner"),
                               (loader, "127", "error while loading shared libraries"),
                               ("#!/bin/sh\nexit 126\n", "126", r"\(empty\)")):
            with self.subTest(body=body):
                h = _Harness(tempfile.mkdtemp(dir=self._tmp.name))
                prog = h.local / "prog"
                prog.write_text(body)
                prog.chmod(0o755)
                with self.assertRaisesRegex(
                        rx.RemoteExecutionError,
                        rf"(?s)'run' exited {rc}: at a site that is the code of a program.*"
                        rf"its stderr ends: [^(]*{tail}"):
                    h.run(h.request(ship={"bin/runner": prog}))
                self.assertEqual(h.log_entries("run"), [])

    def test_a_directory_the_script_cannot_make_is_the_hosts_failure(self) -> None:
        """A shipped FILE where the job needs a directory: one of `dirs`, or a command's cwd."""
        for dirs in (("run/raw",), ()):
            with self.subTest(dirs=dirs):
                h = _Harness(tempfile.mkdtemp(dir=self._tmp.name))
                what = "run/raw" if dirs else f"{h.job}/run"
                with self.assertRaisesRegex(rx.RemoteExecutionError,
                                            rf"(?s)ssh exited 3.*cannot make {re.escape(what)}"):
                    h.run(h.request(ship={"bin/runner": h.runner, "run": h.runner}, dirs=dirs))
                self.assertEqual(h.log_entries("run"), [])

    def test_a_connection_failure_is_refused_with_the_stage(self) -> None:
        ctx = self._refused("create the job directory: ssh exited 255", SHIM_SSH_FAIL="mkdir")
        self.assertFalse(Path(self.h.job).exists())
        # Neither a stale directory nor one left behind: none was made.
        self.assertNotIn("stale", str(ctx.exception))
        self.assertNotIn("left at the site", str(ctx.exception))

    def test_a_failure_to_ship_is_refused(self) -> None:
        self._refused("ship the job's files: scp exited 1", SHIM_SCP_FAIL="up")

    def test_a_failure_to_collect_leaves_the_remote_directory_and_names_it(self) -> None:
        ctx = self._refused("collect the job directory", SHIM_SCP_FAIL="down")
        self.assertIn(f"the job directory is left at the site for inspection — remove "
                      f"{self.h.job} when done", str(ctx.exception))
        self.assertTrue((Path(self.h.job) / "run" / "argv.json").is_file())

    def test_a_lost_output_file_is_refused(self) -> None:
        for name in ("run.stdout", "qc.stderr"):
            with self.subTest(name=name):
                h = _Harness(tempfile.mkdtemp(dir=self._tmp.name))
                with self.assertRaisesRegex(rx.RemoteExecutionError,
                                            f"{name} was not collected.*left at the site"):
                    h.run(h.request(), SHIM_SSH_POST=f"rm {h.job}/ctl/{name}")
                self.assertEqual(h.log_entries("run"), [])

    def test_a_file_that_cannot_be_staged_is_refused(self) -> None:
        self._refused("stage the job's files: bin/runner.*left at the site",
                      self.h.request(ship={"bin/runner": self.h.local / "no-such-file"}))

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
        self._invalid("implements 'job_submit' for scheduler", rx.JobRequest(
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
        ctl_cwd = rx.CommandSpec(tag="run", tool_name="run_program", argv=("true",),
                                 cwd=f"{self.h.job}/ctl/x", record_cwd="/local/run", env={}, timeout_sec=1,
                                 command_log_path=self.h.local / "l", capture_limit=1000)
        self._invalid("control directory", self.h.request(ctl_cwd))
        for cwd in ("/tmp", f"{self.h.job}/../x", f"{self.h.job}x", f"{self.h.job}//a"):
            with self.subTest(cwd=cwd):
                bad = rx.CommandSpec(tag="run", tool_name="run_program", argv=("true",),
                                     cwd=cwd, record_cwd="/local/run", env={}, timeout_sec=1,
                                     command_log_path=self.h.local / "l", capture_limit=1000)
                self._invalid("is not under", self.h.request(bad))
        elsewhere = rx.CommandSpec(tag="run", tool_name="run_program", argv=("true",),
                                   cwd="/elsewhere/x/run", record_cwd="/local/run", env={}, timeout_sec=1,
                                   command_log_path=self.h.local / "l", capture_limit=1000)
        self._invalid("job_dir '/elsewhere/x' is not under", rx.JobRequest(
            site=self.h.site, job_dir="/elsewhere/x", ship={}, commands=(elsewhere,)))
        self._invalid("workdir itself", rx.JobRequest(
            site=self.h.site, job_dir=str(self.h.workdir), ship={}, commands=(ok,)))

    def test_commands_are_well_formed(self) -> None:
        for kw, pattern in (({"tag": "Run"}, "lowercase token"),
                            ({"argv": ()}, "empty argv"),
                            ({"argv": ("bin/runner",)}, "relative path"),
                            ({"argv": ("./runner",)}, "relative path"),
                            ({"record_cwd": "local/run"}, "not an absolute local path"),
                            ({"timeout_sec": 0}, "timeout_sec"),
                            ({"timeout_sec": True}, "timeout_sec")):
            with self.subTest(kw=kw):
                base = {"tag": "run", "tool_name": "run_program", "argv": ("true",),
                        "cwd": f"{self.h.job}/run", "record_cwd": "/local/run", "env": {},
                        "timeout_sec": 1,
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


class SchedulerTests(unittest.TestCase):
    """A `slurm` site: the same job, run under the backend's `srun` prefix."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.h = self._slurm(_Harness(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @staticmethod
    def _slurm(h: _Harness, **kw) -> _Harness:
        kw.setdefault("scheduler_directives", ("--partition=debug", "-p other", "--time=1"))
        kw.setdefault("queue_timeout_sec", 77)
        h.site = dataclasses.replace(h.site, scheduler="slurm", **kw)
        return h

    def _refused(self, pattern: str, **knobs: str) -> None:
        with self.assertRaisesRegex(rx.RemoteExecutionError, pattern):
            self.h.run(self.h.request(), **knobs)
        self.assertEqual(self.h.log_entries("run"), [])
        self.assertEqual(self.h.log_entries("qc"), [])

    def test_the_job_runs_under_srun_and_records_its_id(self) -> None:
        result = self.h.run(self.h.request())
        self.assertTrue(all(r["ok"] for r in result.results))
        self.assertEqual(result.site_record["scheduler"], "slurm")
        self.assertEqual(result.site_record["job_id"], "4242")
        for tag in ("run", "qc"):
            (entry,) = self.h.log_entries(tag)
            self.assertEqual(entry["site"]["job_id"], "4242")
            self.assertEqual(entry["site"]["scheduler"], "slurm")
        # One srun call, inside the job's ssh call: the time limit, the directives split into
        # words (the last `--time` wins), and the options no directive may change.
        (srun,) = [c for c in self.h.calls() if c[0] == "srun"]
        wall = sum(c.timeout_sec + rx.KILL_AFTER_SEC for c in self.h.request().commands) \
            + rx.TRANSPORT_GRACE_SEC
        self.assertEqual(srun[1:9], [f"--time={-(-wall // 60)}", "--partition=debug", "-p",
                                     "other", "--time=1", "--ntasks=1",
                                     "--job-name=atmofab-arid-1", "--immediate=77"])
        self.assertEqual(srun[9:11], ["sh", "-c"])
        self.assertIn(rx.STATUS_MARKER, srun[11])
        self.assertEqual([c[0] for c in self.h.calls()], ["ssh", "scp", "ssh", "srun", "scp", "ssh"])
        self.assertFalse(Path(self.h.job).exists())

    def test_the_queue_timeout_defaults_and_bounds_the_call(self) -> None:
        h = self._slurm(_Harness(tempfile.mkdtemp(dir=self._tmp.name)), queue_timeout_sec=None)
        request = h.request(platform_probe=("true",))
        with mock.patch.object(rx, "_ssh", wraps=rx._ssh) as ssh:
            h.run(request)
        (srun,) = [c for c in h.calls() if c[0] == "srun"]
        self.assertIn(f"--immediate={rx.QUEUE_TIMEOUT_DEFAULT_SEC}", srun)
        # The time limit covers the device probe; the local bound adds the queue wait on top.
        wall = sum(c.timeout_sec + rx.KILL_AFTER_SEC for c in request.commands) \
            + rx.TRANSPORT_GRACE_SEC + rx.PROBE_TIMEOUT_SEC
        self.assertIn(f"--time={-(-wall // 60)}", srun)
        (job_call,) = [c for c in ssh.call_args_list if c.kwargs["stage"] == "run the job script"]
        self.assertEqual(job_call.kwargs["timeout"],
                         rx.QUEUE_TIMEOUT_DEFAULT_SEC + wall + rx.TRANSPORT_GRACE_SEC)

    def test_the_queue_wait_is_the_time_between_asking_and_starting(self) -> None:
        result = self.h.run(self.h.request(), SHIM_SRUN_QUEUE="1.5")
        # Whole seconds (both ends are the sites' epoch seconds), and the wait, not an epoch.
        self.assertIn(result.site_record["queue_wait_ms"], (1000, 2000, 3000))

    def test_the_job_line_is_printed_before_the_first_command(self) -> None:
        """Printed after a command, the "start" would count that command's run as queue wait."""
        script = rx.render_job_script(self.h.request())
        job_line = script.index(f"{rx.JOB_MARKER} ")
        self.assertLess(job_line, script.index("t0=$(date +%s)"))
        self.assertLess(job_line, script.index(f"{rx.STATUS_MARKER} "))

    def test_a_start_before_the_ask_is_recorded_as_no_wait(self) -> None:
        """The two times are read on two machines; a job machine whose clock is behind the
        login's gives a negative difference, recorded as 0 rather than refused."""
        result = self.h.run(self.h.request(), SHIM_SSH_SUB=json.dumps(
            [r"^(atmofab-job \S+) \d+$", r"\1 1"]))
        self.assertEqual(result.site_record["queue_wait_ms"], 0)
        self.assertTrue(all(r["ok"] for r in result.results))

    def test_a_job_not_granted_an_allocation_is_refused(self) -> None:
        self._refused("run the job script: ssh exited 1 .*Unable to allocate resources",
                      SHIM_SRUN_DENY="1")
        self.assertTrue(Path(self.h.job).is_dir())

    def test_a_job_the_scheduler_killed_is_refused_although_every_status_arrived(self) -> None:
        self._refused("run the job script: ssh exited 143 .*TIME LIMIT", SHIM_SRUN_KILLED="1")

    def test_a_job_without_its_id_is_refused(self) -> None:
        self._refused("empty job id", SHIM_SRUN_NO_ID="1")

    def test_the_scheduler_lines_are_one_each_and_only_under_a_scheduler(self) -> None:
        one_each = "1 atmofab-submitted and 2 atmofab-job lines|, not one each"
        for pattern, repl, refusal in (
                (r"^(atmofab-job .*)$", r"\1\n\1", one_each),              # a second job line
                (r"^(atmofab-job .*)$", "", one_each),                     # the job line lost
                (r"^(atmofab-submitted .*)$", "", one_each),               # the ask lost
                (r"^(atmofab-job) \S+ (.*)$", r"\1 9 9 \2", "does not parse"),  # not its shape
                (r"^(atmofab-submitted) .*$", r"\1 soon", "does not parse"),
                (r"^(atmofab-submitted .*)$", r"\1 2", "does not parse"),  # a third word
                (r"^(atmofab-job) \S+ (.*)$", r"\1 a/b \2", "does not parse"),  # not an element
                (r"^(atmofab-job \S+) .*$", r"\1 later", "does not parse"),  # not an epoch
                # A marker glued to more letters is no scheduler line, and no status line either.
                (r"^(atmofab-job .*)$", r"\1\natmofab-jobx 8 999", "does not parse")):
            with self.subTest(repl=repl, pattern=pattern):
                h = self._slurm(_Harness(tempfile.mkdtemp(dir=self._tmp.name)))
                with self.assertRaisesRegex(rx.RemoteExecutionError, refusal):
                    h.run(h.request(), SHIM_SSH_SUB=json.dumps([pattern, repl]))
                self.assertEqual(h.log_entries("run"), [])
        h = _Harness(tempfile.mkdtemp(dir=self._tmp.name))
        with self.assertRaisesRegex(rx.RemoteExecutionError, "run under no scheduler"):
            h.run(h.request(), SHIM_SSH_SUB=json.dumps(
                [r"^(atmofab-platform machine .*)$", r"atmofab-job 1 2\n\1"]))

    def test_the_launch_probe_asks_for_the_schedulers_program(self) -> None:
        from tools.host_prerequisites import required_site_executables
        self.assertEqual(rx.scheduler_executables("slurm"), ("srun",))
        self.assertEqual(rx.scheduler_executables("none"), ())
        selection = {"build_system": "make"}
        self.assertEqual(required_site_executables(selection, scheduler="slurm"),
                         ("timeout", "make", "srun"))
        self.assertEqual(required_site_executables(selection, scheduler="none"),
                         ("timeout", "make"))


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


class ProbeSiteTests(unittest.TestCase):
    """`probe_site` (issue #293, PR-3): the driver's launch-time question to a remote site."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.h = _Harness(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def probe(self, *exes: str, **knobs: str) -> rx.SiteProbe:
        with self.h.env(**knobs):
            return rx.probe_site(self.h.site, exes)

    def test_one_call_answers_the_missing_programs_and_the_machine(self) -> None:
        import platform

        got = self.probe("sh", "zz-no-such-tool", "timeout", "zz-other")
        self.assertEqual(got, rx.SiteProbe(missing=("zz-no-such-tool", "zz-other"),
                                           machine=platform.machine()))
        calls = self.h.calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:5], ["ssh", *rx.SSH_OPTIONS])
        self.assertEqual(self.probe().missing, ())

    def test_a_site_whose_path_lacks_a_program_reports_it(self) -> None:
        bare = _bare_path(self.h.root, without="timeout")
        self.assertEqual(self.probe("timeout", "sh", SHIM_SSH_PATH=str(bare)).missing,
                         ("timeout",))

    def test_a_site_that_does_not_answer_is_a_remote_execution_error(self) -> None:
        with self.assertRaises(rx.RemoteExecutionError) as ctx:
            self.probe("sh", SHIM_SSH_FAIL=rx.PROBE_MARKER)
        self.assertIn("probe the site", str(ctx.exception))
        self.assertIn("box", str(ctx.exception))

    def test_an_answer_not_in_the_probes_shape_is_refused(self) -> None:
        cases = {
            "no machine line": "printf 'hello\\n'",
            "two machine lines": (f"echo '{rx.PROBE_MARKER} machine a'; "
                                  f"echo '{rx.PROBE_MARKER} machine b'"),
            "an unknown kind": f"echo '{rx.PROBE_MARKER} weather sunny'",
            "a program not asked about": (f"echo '{rx.PROBE_MARKER} missing zz'; "
                                          f"echo '{rx.PROBE_MARKER} machine x'"),
        }
        for what, script in cases.items():
            with self.subTest(what), mock.patch.object(
                    rx, "_ssh", side_effect=lambda *a, _s=script, **k: subprocess.run(
                        ["sh", "-c", _s], capture_output=True, text=True).stdout):
                with self.assertRaises(rx.RemoteExecutionError):
                    rx.probe_site(self.h.site, ("sh",))

    def test_an_unusable_workdir_and_a_timeout_without_kill_are_named(self) -> None:
        import platform

        blocker = self.h.root / "remote" / "blocker"
        blocker.write_text("a file, so nothing can be made beneath it")
        site = es.Site(site_id="box", executes=("cpu",), host="box",
                       workdir=str(blocker / "jobs"))
        fake = self.h.root / "fake_timeout"
        fake.mkdir()
        (fake / "timeout").write_text('#!/bin/sh\n[ "$1" = -k ] && exit 1\nexec true\n')
        (fake / "timeout").chmod(0o755)
        with self.h.env(SHIM_SSH_PATH=f"{fake}{os.pathsep}{os.environ['PATH']}"):
            got = rx.probe_site(site, ("sh",))
        self.assertEqual(got, rx.SiteProbe(missing=(), machine=platform.machine(), problems=(
            "the workdir cannot be made or is not writable",
            "its timeout does not take -k")))
        # A workdir that does not exist yet is made, as the first job would make it.
        fresh = self.h.root / "remote" / "fresh" / "jobs"
        with self.h.env():
            ok = rx.probe_site(es.Site(site_id="box", executes=("cpu",), host="box",
                                       workdir=str(fresh)), ("sh",))
        self.assertEqual(ok.problems, ())
        self.assertTrue(fresh.is_dir())

    def test_an_existing_workdir_that_cannot_be_written_is_named(self) -> None:
        locked = self.h.root / "remote" / "locked"
        locked.mkdir()
        locked.chmod(0o555)
        try:
            # The fixture's premise, asserted rather than assumed: a process that may write
            # anywhere (root) would see this directory as writable.
            self.assertFalse(os.access(locked, os.W_OK), "the fixture needs an unwritable dir")
            with self.h.env():
                got = rx.probe_site(es.Site(site_id="box", executes=("cpu",), host="box",
                                            workdir=str(locked)), ("sh",))
        finally:
            locked.chmod(0o755)
        self.assertEqual(got.problems, ("the workdir cannot be made or is not writable",))

    def test_a_workdir_where_nothing_runs_is_named(self) -> None:
        """A noexec mount, stood in for by a `chmod` that sets no mode: the probe's program is
        then not executable, as it is not on a noexec mount; nothing is left behind."""
        fake = self.h.root / "no_chmod"
        fake.mkdir()
        (fake / "chmod").write_text("#!/bin/sh\nexit 0\n")
        (fake / "chmod").chmod(0o755)
        with self.h.env(SHIM_SSH_PATH=f"{fake}{os.pathsep}{os.environ['PATH']}"):
            got = rx.probe_site(self.h.site, ("sh",))
        self.assertEqual(got.problems,
                         ("a program in the workdir cannot be executed (a noexec mount)",))
        self.assertEqual(list(self.h.workdir.iterdir()), [])
        with self.h.env():
            self.assertEqual(rx.probe_site(self.h.site, ("sh",)).problems, ())
        self.assertEqual(list(self.h.workdir.iterdir()), [])

    def test_a_login_banner_without_a_newline_does_not_hide_a_line(self) -> None:
        """The probe's first line is empty, as the job script's is: a banner printed without a
        newline glues onto it, not onto the first probe line."""
        real = rx._ssh

        def banner(*a, **k):
            return "Last login: somewhere" + real(*a, **k)

        with self.h.env(), mock.patch.object(rx, "_ssh", side_effect=banner):
            got = rx.probe_site(self.h.site, ("zz-no-such-tool", "sh"))
        self.assertEqual(got.missing, ("zz-no-such-tool",))
        # And the banner itself is named: scp fails on a login that prints.
        self.assertEqual(got.problems, (rx.STARTUP_OUTPUT_PROBLEM,))

    def test_startup_output_is_not_read_as_a_probe_line_and_is_named(self) -> None:
        with mock.patch.object(rx, "_ssh", return_value=(
                f"Welcome\n{rx.PROBE_MARKER} machine x86_64\nbye\n")):
            self.assertEqual(rx.probe_site(self.h.site, ("sh",)),
                             rx.SiteProbe(missing=(), machine="x86_64",
                                          problems=(rx.STARTUP_OUTPUT_PROBLEM,)))
        # Empty lines are not output: the probe prints one itself.
        with mock.patch.object(rx, "_ssh", return_value=(
                f"\n\n{rx.PROBE_MARKER} machine x86_64\n")):
            self.assertEqual(rx.probe_site(self.h.site, ("sh",)).problems, ())

    def test_a_login_that_prints_is_named_through_the_transport(self) -> None:
        fake = self.h.root / "chatty"
        fake.mkdir()
        # A startup file's echo, stood in for by an `sh` that prints before it runs the script.
        real_sh = shutil.which("sh")
        (fake / "sh").write_text(f"#!{real_sh}\necho 'Welcome to the site'\nexec {real_sh} \"$@\"\n")
        (fake / "sh").chmod(0o755)
        with self.h.env(SHIM_SSH_PATH=f"{fake}{os.pathsep}{os.environ['PATH']}"):
            got = rx.probe_site(self.h.site, ("sh",))
        self.assertEqual(got.problems, (rx.STARTUP_OUTPUT_PROBLEM,))

    def test_a_malformed_request_is_refused_before_any_call(self) -> None:
        local = es.Site(site_id="local", executes=("cpu",))
        with self.h.env():
            with self.assertRaises(ValueError):
                rx.probe_site(local, ("sh",))
            for bad in ("a b", "$(x)", "-v", "../sh", ""):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    rx.probe_site(self.h.site, (bad,))
        self.assertEqual(self.h.calls(), [])


if __name__ == "__main__":
    unittest.main()
