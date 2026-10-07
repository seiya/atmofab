"""`Build` at an execution site (issue #333, PR-2): the conductor's wiring of
`tools/remote_execution.py` for the build.

The rows drive the production entry point — `Conductor._build_inproc`, and
`_run_deterministic_substep` where the routing of a failure is the subject — end to end through
the `ssh` / `scp` shims `tools/tests/test_remote_execution.py` uses: the "site" is a directory under
the test's temporary tree, reached by running the command string locally, and its PATH starts with
a directory holding a fake compiler that answers `--version` with a version no real host has, so
what the record says was answered at the site can only have come from there. Real `make` runs the
node's control file there. What is mocked is the post-build gate's process (every other
`subprocess.run` is the real one) and, in the local-site row, `tool_compile_project`.
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

import tools.workflow_conductor as wc
from tools.backends import registry
from tools.execution_sites import Site
from tools.host_execution import LOCAL_SITE
from tools.orchestration_runtime import (
    AUDIT_LOG_BASENAMES,
    _target_toolchain_identity,
    toolchain_version_argv,
)
from tools.tests.llm_samples import sample_config_with as _cfg
from tools.tests.target_fixtures import TARGET_ID, profile_with
from tools.tests.test_execute_at_site import _real_gate_free_run
from tools.tests.test_remote_execution import _SCP_SHIM, _SSH_SHIM
from tools.tests.test_workflow_conductor import _build_runtime, _TargetedConductor

#: The version the site's fake compiler answers: no real compiler prints it.
_SITE_VERSION = "ZZ Fortran (site build) 99.1.0"

#: The site's compiler: answers `--version`, fails naming the source by its absolute path when the
#: source says FAIL (as a real compiler does when handed one), and otherwise writes an executable
#: to its `-o` operand.
_FAKE_COMPILER = textwrap.dedent(f'''\
    #!/bin/sh
    if [ "$1" = --version ]; then echo "{_SITE_VERSION}"; exit 0; fi
    src=$1; out=
    while [ $# -gt 0 ]; do [ "$1" = -o ] && {{ out=$2; shift; }}; shift; done
    echo "compiling $PWD/$src"
    if grep -q FAIL "$src"; then echo "$PWD/$src:3:1: Error: no such thing" >&2; exit 1; fi
    printf '#!/bin/sh\\necho built at the site\\n' > "$out" && chmod +x "$out"
''')

#: The node's build control file: builds `$(BINDIR)/$(BIN)` from the node's source with `$(FC)`,
#: and copies the staged dependency source into the object directory it was staged to.
_MAKEFILE = textwrap.dedent('''\
    FC := {compiler}
    $(BINDIR)/$(BIN): kernel.f90
    \tmkdir -p $(BINDIR) $(OBJDIR)
    \tcat $(OBJDIR)/depy_model.f90 > $(OBJDIR)/linked.txt
    \t$(FC) kernel.f90 -o $(BINDIR)/$(BIN)
''')


class _Node:
    """A scratch repository with one generated node, a shim-reached site, a fake compiler on the
    site's PATH, and a conductor for it."""

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
        self.compiler = wc.build_compiler({
            "language": self.target.toolchain["language"], "standard": "",
            "build_system": self.target.toolchain["build_system"],
            "compiler": str(self.target.toolchain.get("compiler") or ""),
            "backend": self.target.parallel_backend})
        self.site_bin = self.root / "site_bin"
        self.site_bin.mkdir()
        fake = self.site_bin / self.compiler
        fake.write_text(_FAKE_COMPILER)
        fake.chmod(0o755)
        (self.site_bin / "hostname").write_text("#!/bin/sh\necho site-node-zz\n")
        (self.site_bin / "hostname").chmod(0o755)
        self.conductor = _TargetedConductor(
            repo_root=self.repo, orchestration_id="orch_1", orchestration_agent_run_id="x",
            llm_config=_cfg("claude"), env={}, target_profile=self.target, site=self.site)
        self.refs = wc.NodeRefs(
            target_id=TARGET_ID, node_key="component/spec_x@0.1.0",
            spec_path="spec/component/spec_x", ir_id="x_1", pipeline_id="x_1",
            source_id="src_1", binary_id="bin_1")
        (self.repo / self.refs.ir_ref).mkdir(parents=True)
        self.src = self.repo / self.refs.source_dir() / "src"
        self.src.mkdir(parents=True)
        (self.src / "Makefile").write_text(_MAKEFILE.format(compiler=self.compiler),
                                           encoding="utf-8")
        (self.src / "kernel.f90").write_text("! the node's source\n", encoding="utf-8")
        # An audit log an earlier phase appended to: not shipped.
        (self.src / "command_log.jsonl").write_text('{"earlier": true}\n', encoding="utf-8")
        # A dependency source as staging leaves it in the object directory.
        self.obj = self.repo / "workspace" / "tmp" / "arid-1" / "build"
        self.obj.mkdir(parents=True)
        (self.obj / "depy_model.f90").write_text("! staged dependency\n", encoding="utf-8")
        self.bin = self.repo / self.refs.binary_dir() / "bin"
        self.exe = self.bin / "spec_x_runner"
        self.job = f"{self.workdir}/orch_1/arid-1"

    def env(self, *, site_path: str | None = None, **knobs: str):
        path = site_path if site_path is not None else \
            f"{self.site_bin}{os.pathsep}{os.environ['PATH']}"
        env = {"PATH": f"{self.shims}{os.pathsep}{os.environ['PATH']}",
               "SHIM_LOG": str(self.log), "SHIM_SSH_PATH": path}
        env.update(knobs)
        return mock.patch.dict(os.environ, env)

    def build(self, **kw: str) -> dict:
        with self.env(**kw), mock.patch.object(
                subprocess, "run", side_effect=_real_gate_free_run(subprocess.run)):
            return self.conductor._build_inproc(self.refs, "arid-1")

    def substep(self, **kw: str):
        with self.env(**kw), mock.patch.object(
                subprocess, "run", side_effect=_real_gate_free_run(subprocess.run)):
            return self.conductor._run_deterministic_substep(
                self.refs, "build", None, "arid-1", {})

    def meta(self) -> dict:
        return json.loads((self.repo / self.refs.binary_dir() / "binary_meta.json").read_text())

    def log_entries(self) -> list[dict]:
        path = self.src / "command_log.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()]

    def calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.log.read_text().splitlines()]


class BuildAtARemoteSiteTests(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.n = _Node(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_the_binary_comes_home_and_the_record_says_where_it_was_built(self) -> None:
        n = self.n
        out = n.build()
        self.assertEqual(out["returncode"], 0, out)
        # The binary the SITE built is this node's binary.
        self.assertEqual(subprocess.run([str(n.exe)], capture_output=True, text=True,
                                        check=True).stdout, "built at the site\n")
        meta = n.meta()
        self.assertEqual(meta["verification_status"], "pass", meta)
        env = meta["environment"]
        self.assertEqual(env["build_site"]["site"], "box")
        self.assertEqual(env["build_site"]["remote_dir"], n.job)
        self.assertEqual(env["platform"]["site"], "box")
        self.assertEqual(env["platform"]["node"], "site-node-zz")
        # The compiler as the site answered it; the key's stays this host's.
        self.assertEqual(env["compiler_version"], _SITE_VERSION)
        self.assertEqual(meta["compiler_version"],
                         _target_toolchain_identity(n.target)["compiler_version"])
        self.assertNotEqual(meta["compiler_version"], _SITE_VERSION)

    def test_the_log_entry_is_the_local_one_and_names_the_site_argv(self) -> None:
        n = self.n
        n.build()
        earlier, entry = n.log_entries()
        self.assertEqual(earlier, {"earlier": True})
        self.assertEqual(entry["tool_name"], "compile_project")
        self.assertEqual(entry["cwd"], str(n.src))
        # `command` is what the local server would have run: the local paths.
        jobs = _build_runtime().default_build_jobs()
        self.assertEqual(entry["command"], _build_runtime().build_command(
            "make", None, jobs,
            [f"OBJDIR={n.obj}", f"BINDIR={n.bin}", "BIN=spec_x_runner"]))
        self.assertEqual(entry["site"]["remote_command"], _build_runtime().build_command(
            "make", None, jobs,
            [f"OBJDIR={n.job}/build", f"BINDIR={n.job}/bin", "BIN=spec_x_runner"]))
        self.assertEqual(entry["site"]["remote_cwd"], f"{n.job}/src")
        self.assertEqual(entry["timeout_sec"], _build_runtime().COMPILE_PROJECT_TIMEOUT_SEC)
        # The site's diagnostics are kept whole, as the local build keeps them.
        self.assertEqual(entry["capture_limit"], wc._FULL_CAPTURE_LIMIT)
        self.assertEqual(n.meta()["environment"]["build_site"]["host"], "box")
        self.assertEqual(n.meta()["command_id"], entry["command_id"])

    def test_the_whole_source_and_the_staged_sources_are_shipped_and_not_the_audit_logs(
            self) -> None:
        n = self.n
        n.build()
        stage = n.repo / "workspace" / "tmp" / "arid-1" / "site" / "stage"
        shipped = sorted(p.relative_to(stage).as_posix() for p in stage.rglob("*")
                         if p.is_file())
        # Everything in the object directory — the fixture's `depy` and whatever staging put
        # there (the target's harness) — and of `src/` everything but the audit log.
        staged = sorted(f"build/{p.name}" for p in n.obj.iterdir() if p.is_file())
        self.assertIn("build/depy_model.f90", staged)
        self.assertEqual(shipped, sorted([*staged, "src/Makefile", "src/kernel.f90"]))
        self.assertIn("command_log.jsonl", AUDIT_LOG_BASENAMES)
        # The staged source reached the build where the control file reads it.
        collected = n.repo / "workspace" / "tmp" / "arid-1" / "site" / "collected"
        self.assertEqual((collected / "build" / "linked.txt").read_text(),
                         "! staged dependency\n")

    def test_a_stale_file_in_the_nodes_bin_is_not_kept(self) -> None:
        n = self.n
        n.bin.mkdir(parents=True)
        (n.bin / "leftover").write_text("old")
        n.build()
        self.assertEqual(sorted(p.name for p in n.bin.iterdir()), ["spec_x_runner"])

    def test_a_site_without_the_build_compiler_is_the_hosts_failure_not_the_sources(
            self) -> None:
        """make, missing its compiler, would exit 2 and read as a failure of the source, which
        routes the node back to Generate; the job refuses before the command instead."""
        n = self.n
        # The site's PATH: what the job needs, and no compiler of that name.
        bare = n.root / "bare"
        bare.mkdir()
        for tool in ("sh", "uname", "hostname", "grep", "sed", "mkdir", "rm", "date", "env",
                     "timeout", "make", "cat", "chmod"):
            found = wc.shutil.which(tool)
            if found:
                (bare / tool).symlink_to(found)
        self.assertIsNone(wc.shutil.which(n.compiler, path=str(bare)))
        result = n.substep(site_path=str(bare))
        self.assertEqual(result.returncode, 1, result)
        self.assertIn("deterministic_build_error", result.stderr)
        self.assertIn(f"{n.compiler} is missing or not executable", result.stderr)
        self.assertFalse((n.repo / n.refs.binary_dir() / "binary_meta.json").exists())
        self.assertFalse((Path(n.job) / "ctl" / "build.stdout").exists(), "make did not run")

    def test_a_source_the_site_compiler_refuses_is_the_sources_failure_named_locally(
            self) -> None:
        n = self.n
        (n.src / "kernel.f90").write_text("! FAIL\n", encoding="utf-8")
        out = n.build()
        self.assertEqual(out["returncode"], 0, "a content failure, not a transport one")
        meta = n.meta()
        self.assertEqual(meta["verification_status"], "fail")
        self.assertIsNotNone(meta["failure_category"])
        self.assertEqual(meta["failure_source_refs"],
                         [f"{n.conductor._rel(n.src)}/kernel.f90"])
        # The diagnostics name the node's local source, not the job directory's copy — on both
        # streams.
        log = (n.repo / n.refs.binary_dir() / "compile.stderr.log").read_text()
        self.assertIn(f"{n.src}/kernel.f90:3:1: Error", log)
        self.assertNotIn(n.job, log)
        out = (n.repo / n.refs.binary_dir() / "compile.stdout.log").read_text()
        self.assertIn(f"compiling {n.src}/kernel.f90", out)
        self.assertNotIn(n.job, out)
        self.assertIn(f"{n.src}/kernel.f90", meta["failure_excerpt"])
        self.assertNotIn(n.job, meta["failure_excerpt"])
        # Still recorded as built (and failed) at the site.
        self.assertEqual(meta["environment"]["build_site"]["site"], "box")

    def test_a_launcher_target_is_refused_before_anything_is_shipped(self) -> None:
        n = _Node(tempfile.mkdtemp(dir=self._tmp.name),
                  target=profile_with(parallel={"backend": "mpi"}))
        # The fixture conductor certifies a harness closure for the target first, which this
        # profile's harness is not: the refusal under test precedes any staging.
        n.conductor._bind_a_harness_only_closure = lambda *a: None
        result = n.substep()
        self.assertEqual(result.returncode, 1, result)
        self.assertIn("deterministic_build_error", result.stderr)
        self.assertIn("launcher", result.stderr)
        self.assertEqual(n.calls(), [])

    def test_the_toolchain_probe_is_the_keys_version_argv(self) -> None:
        """The job asks the site the same question `_target_toolchain_identity` asks this host
        (`toolchain_version_argv`), so the two recorded versions are one question's answers."""
        import tools.remote_execution as rx

        n = self.n
        seen = {}
        real_execute = rx.execute_job

        def spy(request, **kw):
            seen["probe"] = request.toolchain_probe
            seen["required"] = request.required_programs
            return real_execute(request, **kw)

        with mock.patch.object(rx, "execute_job", spy):
            n.build()
        self.assertEqual(seen["probe"], toolchain_version_argv(n.target))
        self.assertEqual(seen["required"], (n.compiler,))


class ToolchainVersionArgvTests(unittest.TestCase):

    def test_the_key_probes_the_argv_the_build_job_is_handed(self) -> None:
        """The build key's `compiler_version` is asked with `toolchain_version_argv` — the argv
        the remote build job's toolchain probe runs — for every checked-in profile, the one
        whose parallel backend puts a compiler wrapper in the compiler's place included."""
        from tools.target_profile import list_target_ids, load_target_profile
        repo = Path(__file__).resolve().parents[2]
        server = _build_runtime()
        ids = sorted(list_target_ids(repo))
        self.assertIn("fortran_cpu_mpi", ids)
        for target_id in ids:
            with self.subTest(target=target_id):
                target = load_target_profile(repo, target_id)
                asked: list[tuple[str, ...]] = []
                with mock.patch.object(server, "_syntax_compiler_version",
                                       side_effect=lambda argv, asked=asked:
                                       asked.append(argv) or "v"):
                    identity = _target_toolchain_identity(target)
                self.assertEqual(asked, [toolchain_version_argv(target)])
                self.assertEqual(identity["compiler_version"], "v")
                # And that argv asks the program the build runs: through the parallel backend's
                # wrapper when it declares one, which runs the configured compiler.
                compiler = str(target.toolchain.get("compiler") or "") or str(
                    registry.capability_module("language", target.toolchain["language"],
                                               "bundle_facts").DEFAULT_COMPILER)
                expected: tuple[str, ...] = (compiler, "--version")
                if registry.provides("parallel", target.parallel_backend, "compiler_wrapper"):
                    wrapper = registry.capability_module("parallel", target.parallel_backend,
                                                         "compiler_wrapper")
                    expected = tuple(str(a) for a in wrapper.wrap(expected))
                    self.assertNotEqual(expected, (compiler, "--version"))
                self.assertEqual(toolchain_version_argv(target), expected)


class BuildAtTheLocalSiteTests(unittest.TestCase):

    def test_the_local_build_records_the_local_site_and_the_keys_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for site in (None, Site(site_id=LOCAL_SITE, executes=("cpu",))):
                with self.subTest(site=site):
                    n = _Node(tempfile.mkdtemp(dir=tmp), site=site)
                    if site is None:
                        n.conductor.site = None

                    def fake_compile(args, n=n):
                        n.bin.mkdir(parents=True, exist_ok=True)
                        n.exe.write_text("x")
                        fake_compile.args = args
                        return {"ok": True, "return_code": 0, "command_id": "cid"}

                    with mock.patch.object(_build_runtime(), "tool_compile_project",
                                           fake_compile):
                        n.build()
                    self.assertEqual(fake_compile.args["project_dir"], str(n.src))
                    meta = n.meta()
                    env = meta["environment"]
                    self.assertEqual(env["build_site"], wc._local_site_record())
                    self.assertEqual(env["platform"]["site"], LOCAL_SITE)
                    self.assertIsNone(env["platform"]["gpu"])
                    self.assertEqual(env["compiler_version"], meta["compiler_version"])
                    self.assertEqual(n.calls(), [], "nothing reached for a site")


if __name__ == "__main__":
    unittest.main()
