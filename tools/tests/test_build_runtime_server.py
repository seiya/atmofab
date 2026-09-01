"""Tests for mcp_servers/build_runtime_server.py.

Bytecode-cache handling: the build-runtime MCP server runs inside a read-only bwrap
sandbox. It must never attempt to write Python bytecode (the previous code
unconditionally created `workspace/.pycache`, which EROFSed before any build ran on a
clean workspace).

run_syntax_check: the Generate.syntax compiler front-end gate — adapter argv shape,
module/use topological source ordering, missing-compiler skip, custom-command
rejection, and (when gfortran is installed) a real -fsyntax-only smoke covering the
error classes the retired post_generate text heuristics used to mimic.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_SERVER_PATH = (
    Path(__file__).resolve().parent.parent.parent / "mcp_servers" / "build_runtime_server.py"
)


def _load_server_module():
    spec = importlib.util.spec_from_file_location("build_runtime_server", _SERVER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so module-level @dataclass can resolve its __module__.
    sys.modules["build_runtime_server"] = mod
    spec.loader.exec_module(mod)
    return mod


class DisableBytecodeWritesTests(unittest.TestCase):
    def test_disable_sets_interpreter_flag_and_env(self) -> None:
        mod = _load_server_module()
        orig_flag = sys.dont_write_bytecode
        orig_env = os.environ.get("PYTHONDONTWRITEBYTECODE")
        try:
            sys.dont_write_bytecode = False
            os.environ.pop("PYTHONDONTWRITEBYTECODE", None)
            mod._disable_bytecode_writes()
            # The interpreter flag must flip (a runtime env var alone is too late) so
            # importlib does not write .pyc; the env var is exported for subprocesses.
            self.assertTrue(sys.dont_write_bytecode)
            self.assertEqual(os.environ.get("PYTHONDONTWRITEBYTECODE"), "1")
        finally:
            sys.dont_write_bytecode = orig_flag
            if orig_env is None:
                os.environ.pop("PYTHONDONTWRITEBYTECODE", None)
            else:
                os.environ["PYTHONDONTWRITEBYTECODE"] = orig_env

    def test_runtime_loader_does_not_mkdir_pycache(self) -> None:
        # Regression: the server must not create workspace/.pycache (read-only under the
        # bwrap sandbox -> EROFS before any build runs).
        src = _SERVER_PATH.read_text(encoding="utf-8")
        self.assertNotIn("pycache_root.mkdir", src)


_HAVE_GFORTRAN = shutil.which("gfortran") is not None


class _StandaloneServerEnvMixin:
    """Pin the server's own environment to standalone for tests that call a gated
    handler without `orchestration_id`.

    Those calls are refused when the server runs under the workflow
    (`ATMOFAB_WORKFLOW_MODE` / `ATMOFAB_ORCHESTRATION_ID` in its environment), so without
    this the verdict would depend on the shell that started the suite — and commands in
    this repository are routinely prefixed with those variables."""

    def setUp(self) -> None:
        super().setUp()  # type: ignore[misc]
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)  # type: ignore[attr-defined]
        os.environ.pop("ATMOFAB_WORKFLOW_MODE", None)
        os.environ.pop("ATMOFAB_ORCHESTRATION_ID", None)


class RunSyntaxCheckTests(_StandaloneServerEnvMixin, unittest.TestCase):
    """Unit tests for tool_run_syntax_check (no compiler required — subprocess mocked
    or skipped paths)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_server_module()

    def _src_dir(self, files: dict[str, str]) -> Path:
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        for name, text in files.items():
            (d / name).write_text(text, encoding="utf-8")
        return d

    def test_gfortran_adapter_argv_shape(self) -> None:
        argv = self.mod._gfortran_syntax_argv("f2008", ".mods", False, ["a.f90", "b.f90"])
        self.assertEqual(
            argv,
            ["gfortran", "-fsyntax-only", "-std=f2008",
             "-Werror=unused-dummy-argument", "-Werror=unused-variable", "-Werror=ampersand",
             "-J", ".mods", "-I", ".mods",
             "a.f90", "b.f90"])
        argv = self.mod._gfortran_syntax_argv("f2018", ".mods", True, ["x.f90"])
        self.assertIn("-fopenmp", argv)
        self.assertIn("-std=f2018", argv)
        self.assertIn("-Werror=unused-dummy-argument", argv)
        self.assertIn("-Werror=unused-variable", argv)
        self.assertIn("-Werror=ampersand", argv)
        # sources stay last so the compiler reads them after the mod-dir flags
        self.assertEqual(argv[-1], "x.f90")

    def test_source_order_topological_by_module_use(self) -> None:
        d = self._src_dir({
            # alphabetically first but uses the module defined last
            "a_runner.f90": "program p\n  use z_model, only: x\nend program p\n",
            "m_checks.f90": "module m_checks\n  use z_model\nend module m_checks\n",
            "z_model.f90": "module z_model\n  integer :: x\nend module z_model\n",
        })
        self.assertEqual(
            self.mod._fortran_syntax_source_order(d),
            ["z_model.f90", "a_runner.f90", "m_checks.f90"])

    def test_source_order_ignores_identifier_starting_with_use(self) -> None:
        # `use\b` guards against an ordinary identifier that merely starts with "use"
        # (user_flag / usedcount) being parsed as a USE statement and minting a bogus edge.
        d = self._src_dir({
            "a.f90": "program p\n  logical :: user_flag\n  integer :: usedcount\n"
                     "  user_flag = .true.\n  usedcount = 2\nend program p\n",
            "user.f90": "module user\nend module user\n",  # would be a false provider
        })
        # user.f90 defines module `user`; if `user_flag` were mis-parsed as `use r_flag`/
        # `use user`, ordering could shuffle. With the fix a.f90 has no real `use`, so the
        # order is a plain name-sort and no spurious dependency is introduced.
        self.assertEqual(
            self.mod._fortran_syntax_source_order(d), ["a.f90", "user.f90"])

    def test_source_order_ignores_unknown_and_intrinsic_modules(self) -> None:
        d = self._src_dir({
            "a.f90": "program p\n  use, intrinsic :: iso_fortran_env, only: int64\n"
                     "  use some_external_lib\nend program p\n",
        })
        self.assertEqual(self.mod._fortran_syntax_source_order(d), ["a.f90"])

    def test_source_order_module_procedure_not_a_definition(self) -> None:
        d = self._src_dir({
            "a.f90": "submodule (m) impl\ncontains\nmodule procedure f\nend procedure f\n"
                     "end submodule impl\n",
            "b.f90": "module b_mod\nend module b_mod\n",
        })
        # `module procedure` must not register a module named "procedure"/f.
        self.assertEqual(self.mod._fortran_syntax_source_order(d), ["a.f90", "b.f90"])

    def test_rejects_custom_command(self) -> None:
        d = self._src_dir({})
        with self.assertRaises(ValueError):
            self.mod.tool_run_syntax_check(
                {"project_dir": str(d), "command": ["gfortran", "x.f90"]})

    def test_rejects_unknown_compiler(self) -> None:
        d = self._src_dir({})
        with self.assertRaises(ValueError) as ctx:
            self.mod.tool_run_syntax_check({"project_dir": str(d), "compiler": "frt"})
        self.assertIn("supported=gfortran", str(ctx.exception))

    def test_missing_compiler_returns_skipped(self) -> None:
        d = self._src_dir({"a.f90": "program p\nend program p\n"})
        with mock.patch.object(self.mod.shutil, "which", return_value=None):
            result = self.mod.tool_run_syntax_check({"project_dir": str(d)})
        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertIn("compiler not available", result["reason"])

    def test_no_sources_returns_skipped(self) -> None:
        d = self._src_dir({"notes.txt": "not fortran"})
        with mock.patch.object(self.mod.shutil, "which", return_value="/usr/bin/gfortran"):
            result = self.mod.tool_run_syntax_check({"project_dir": str(d)})
        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertIn("no fortran sources", result["reason"])

    def test_run_invokes_adapter_argv_and_logs(self) -> None:
        d = self._src_dir({
            "m.f90": "module m\nend module m\n",
            "p.f90": "program p\n  use m\nend program p\n",
        })
        fake = subprocess.CompletedProcess(
            args=["gfortran"], returncode=0, stdout="", stderr="")
        with mock.patch.object(self.mod.shutil, "which", return_value="/usr/bin/gfortran"), \
                mock.patch.object(self.mod.subprocess, "run", return_value=fake) as run_mock:
            result = self.mod.tool_run_syntax_check(
                {"project_dir": str(d), "std": "f2008", "openmp": True})
        # first call = the syntax check itself; a later call probes --version
        argv = run_mock.call_args_list[0].args[0]
        self.assertEqual(argv[:3], ["gfortran", "-fsyntax-only", "-std=f2008"])
        self.assertIn("-fopenmp", argv)
        self.assertIn("-Werror=unused-dummy-argument", argv)
        self.assertIn("-Werror=unused-variable", argv)
        self.assertIn("-Werror=ampersand", argv)
        self.assertEqual(argv[-2:], ["m.f90", "p.f90"])  # topological order
        self.assertTrue(result["ok"])
        self.assertFalse(result["skipped"])
        self.assertEqual(result["compiler"], "gfortran")
        # command_log.jsonl record with the run_syntax_check tool_name
        log_path = d / "command_log.jsonl"
        self.assertTrue(log_path.exists())
        entry = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(entry["tool_name"], "run_syntax_check")
        self.assertEqual(entry["ok"], True)
        # scratch mod dir is created inside project_dir, isolated per call
        self.assertTrue((d / ".mods").is_dir())

    def test_compile_error_returns_ok_false(self) -> None:
        d = self._src_dir({"bad.f90": "program p\n  implicit none (external)\nend program p\n"})
        fake = subprocess.CompletedProcess(
            args=["gfortran"], returncode=1, stdout="",
            stderr="Error: Fortran 2018: IMPLICIT NONE with spec list")
        with mock.patch.object(self.mod.shutil, "which", return_value="/usr/bin/gfortran"), \
                mock.patch.object(self.mod.subprocess, "run", return_value=fake):
            result = self.mod.tool_run_syntax_check({"project_dir": str(d)})
        self.assertFalse(result["ok"])
        self.assertFalse(result["skipped"])
        self.assertIn("IMPLICIT NONE with spec list", result["stderr"])


@unittest.skipUnless(_HAVE_GFORTRAN, "gfortran not available")
class RunSyntaxCheckGfortranSmokeTests(_StandaloneServerEnvMixin, unittest.TestCase):
    """Real-compiler smoke: the gate must catch, with the actual gfortran front-end,
    the error classes the retired post_generate text heuristics used to mimic
    (identifier > 63 chars / implicit none spec-list / non-constant STOP code) plus the
    three promoted warning classes (unused dummy argument / unused variable / a character
    literal resumed without `&`), and must pass a valid two-file module dependency
    (define-before-use via .mod written by -fsyntax-only) as well as the associate binding
    that sanctions an inert dummy."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_server_module()

    def _check(self, files: dict[str, str]) -> dict:
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        for name, text in files.items():
            (d / name).write_text(text, encoding="utf-8")
        return self.mod.tool_run_syntax_check({"project_dir": str(d), "std": "f2008"})

    def test_valid_module_dependency_passes(self) -> None:
        result = self._check({
            "dep_model.f90": "module dep_model\n  implicit none\n  integer :: n = 1\n"
                             "end module dep_model\n",
            "top_runner.f90": "program top_runner\n  use dep_model, only: n\n"
                              "  implicit none\n  print *, n\nend program top_runner\n",
        })
        self.assertTrue(result["ok"], msg=result.get("stderr"))

    def test_implicit_none_spec_list_fails_under_f2008(self) -> None:
        result = self._check({
            "bad.f90": "program bad\n  implicit none (external)\nend program bad\n",
        })
        self.assertFalse(result["ok"])

    def test_over_63_char_identifier_fails(self) -> None:
        long_name = "x" * 64
        result = self._check({
            "bad.f90": f"program bad\n  implicit none\n  integer :: {long_name}\n"
                       f"  {long_name} = 1\nend program bad\n",
        })
        self.assertFalse(result["ok"])

    def test_nonconstant_stop_code_fails_under_f2008(self) -> None:
        result = self._check({
            "bad.f90": "program bad\n  implicit none\n"
                       "  character(len=8) :: cid\n  cid = 'c1'\n"
                       "  error stop 'unknown case_id: '//cid\nend program bad\n",
        })
        self.assertFalse(result["ok"])

    def test_unused_dummy_argument_fails(self) -> None:
        # A dummy the interface fixes but the body never reads is a dead dummy: the gate
        # must reject it so the leaf binds it with the associate idiom instead.
        result = self._check({
            "m.f90": "module m\n  implicit none\ncontains\n"
                     "  subroutine step(z_b, y)\n"
                     "    real, intent(in) :: z_b\n    real, intent(out) :: y\n"
                     "    y = 1.0\n  end subroutine step\n"
                     "end module m\n",
        })
        self.assertFalse(result["ok"])
        self.assertIn("unused-dummy-argument", result["stderr"])

    def test_unused_variable_fails(self) -> None:
        result = self._check({
            "bad.f90": "program bad\n  implicit none\n  integer :: leftover\n"
                       "  print *, 1\nend program bad\n",
        })
        self.assertFalse(result["ok"])
        self.assertIn("unused-variable", result["stderr"])

    def test_canary_source_is_valid_under_every_standard_and_detects_a_bad_std(self) -> None:
        # The conductor compiles SYNTAX_CANARY_SOURCE with the failing stage's own argv to
        # tell a broken INVOCATION (an `-std=` value the driver rejects, so no source is ever
        # parsed) apart from broken sources. Both halves of that must hold against the real
        # compiler: the canary passes under each standard a node may target — were it invalid
        # Fortran, EVERY failing stage would be misattributed to an unviable invocation and
        # nothing would ever reach the leaf — and it fails when the std is not one the driver
        # knows, which is the signal the attribution keys on.
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        (d / "atmofab_syntax_canary.f90").write_text(
            self.mod.SYNTAX_CANARY_SOURCE, encoding="utf-8")
        # every standard a node may declare — a canary that failed any one of these would
        # fail_closed every ordinary syntax finding on a node targeting it
        for std in ("f95", "f2003", "f2008", "f2018", "gnu", "legacy"):
            result = self.mod.tool_run_syntax_check({"project_dir": str(d), "std": std})
            self.assertTrue(result["ok"], msg=f"{std}: {result.get('stderr')}")
        bad = self.mod.tool_run_syntax_check({"project_dir": str(d), "std": "2008"})
        self.assertFalse(bad["ok"])
        self.assertFalse(bad["skipped"])

    def test_missing_ampersand_continuation_fails(self) -> None:
        # gfortran EXTENDS the standard by accepting a continued character literal whose
        # resume line carries no leading `&`. Left a warning, that shape put a counted-`do`
        # spelling written inside a string at a PHYSICAL line start, where the fail_closed
        # OpenMP presence floor (`_validate_openmp_presence_floor`, anchored and stateless)
        # counted it — a false REJECT on a source this gate had passed. Issue #25 promotes
        # the class so the shape never reaches the floor; the conforming `&`-led resume
        # below (`test_conforming_continued_literal_passes`) is unaffected.
        result = self._check({
            "amp.f90": "module amp\n  implicit none\ncontains\n"
                       "  subroutine msg(u)\n    integer, intent(in) :: u\n"
                       "    write (u, '(a)') 'start&\n"
                       "do i = 1, n suffix'\n"
                       "  end subroutine msg\nend module amp\n",
        })
        self.assertFalse(result["ok"])
        self.assertIn("ampersand", result["stderr"])

    def test_conforming_continued_literal_passes(self) -> None:
        # The promotion must reject only the missing-`&` extension: a literal resumed WITH
        # the leading `&` is standard f2008 and stays silent.
        result = self._check({
            "cont.f90": "module cont\n  implicit none\ncontains\n"
                        "  subroutine msg(u)\n    integer, intent(in) :: u\n"
                        "    write (u, '(a)') 'a message that is &\n"
                        "      &continued'\n"
                        "  end subroutine msg\nend module cont\n",
        })
        self.assertTrue(result["ok"], msg=result.get("stderr"))

    def test_lone_ampersand_line_is_not_promoted_by_werror_ampersand(self) -> None:
        # A lone-`&` continuation line draws a diagnostic with NO `-W<class>` tag (bare
        # `f951: Warning: '&' not allowed by itself`), so `-Werror=ampersand` does not
        # promote it and such a source still reaches a gate. `tools/backends/language/fortran/lines` states
        # that as the reason its scanner must keep handling the shape; pinned here because
        # it is the compiler's answer, not an inference from the flag name.
        result = self._check({
            "lone.f90": "module lone\n  implicit none\ncontains\n"
                        "  subroutine msg(u)\n    integer, intent(in) :: u\n"
                        "    write (u, '(a)') 'hi'\n"
                        "&\n"
                        "  end subroutine msg\nend module lone\n",
        })
        self.assertTrue(result["ok"], msg=result.get("stderr"))
        self.assertIn("not allowed by itself", result["stderr"])

    def test_default_on_warning_names_its_file_without_failing_the_gate(self) -> None:
        # Only the three promoted classes are errors. Other default-on warnings (-Wtabs
        # here) still print, anchored to their file, on a source the gate PASSES. The
        # conductor's dependency attribution (`_gate_syntax_check`) relies on exactly this: a
        # staged dependency's filename appearing in a failing stage's output proves nothing
        # about whose defect it is, so attribution asks the compiler (does the dependency
        # closure pass on its own?) instead of reading the diagnostics.
        result = self._check({
            "noisy.f90": "module noisy\n  implicit none\ncontains\n"
                         "  subroutine msg(u)\n    integer, intent(in) :: u\n"
                         "\twrite (u, '(a)') 'a message'\n"
                         "  end subroutine msg\nend module noisy\n",
        })
        self.assertTrue(result["ok"], msg=result.get("stderr"))
        self.assertIn("noisy.f90", result["stderr"])
        self.assertIn("Wtabs", result["stderr"])

    def test_associate_binding_suppresses_unused_dummy(self) -> None:
        # Pins the sanctioned escape hatch: the very idiom CHECKS_MODULE_CONTRACT §5
        # mandates must pass this gate, so gate and doc cannot drift apart.
        result = self._check({
            "m.f90": "module m\n  implicit none\ncontains\n"
                     "  subroutine step(z_b, y)\n"
                     "    real, intent(in) :: z_b\n    real, intent(out) :: y\n"
                     "    associate (unused_z_b => z_b)\n    end associate\n"
                     "    y = 1.0\n  end subroutine step\n"
                     "end module m\n",
        })
        self.assertTrue(result["ok"], msg=result.get("stderr"))


class OrchestrationGateFailClosedTests(unittest.TestCase):
    """The capability gate under the workflow.

    A leaf holds the committed `mcp__build-runtime` permission, so before this the only
    thing standing between it and an unattributed build/run was one optional argument:
    dropping `orchestration_id` skipped the capability, the role/phase check, and the
    audit record. Under the workflow the omission is refused; standalone use (no
    workflow environment) keeps working, because there is no orchestration to attribute
    a call to."""

    GATED_TOOLS = (
        "compile_project",
        "run_program",
        "run_quality_checks",
        "run_linter",
        "run_syntax_check",
    )

    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_server_module()

    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("ATMOFAB_WORKFLOW_MODE", "ATMOFAB_ORCHESTRATION_ID"):
            os.environ.pop(name, None)
        self.project_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.project_dir, ignore_errors=True)

    def _args(self, tool: str) -> dict:
        args: dict = {"project_dir": str(self.project_dir)}
        if tool == "run_program":
            args["command"] = ["true"]
        return args

    def _call(self, tool: str, args: dict) -> dict:
        return getattr(self.mod, f"tool_{tool}")(args)

    def test_workflow_mode_refuses_gated_tool_without_orchestration_id(self) -> None:
        for tool in self.GATED_TOOLS:
            with self.subTest(tool=tool):
                os.environ["ATMOFAB_WORKFLOW_MODE"] = "1"
                with self.assertRaises(ValueError) as ctx:
                    self._call(tool, self._args(tool))
                # Both halves are diagnosable: what is missing, and why it is required.
                self.assertIn("orchestration_id", str(ctx.exception))
                self.assertIn("ATMOFAB_WORKFLOW_MODE", str(ctx.exception))

    def test_orchestration_id_env_alone_also_refuses(self) -> None:
        # Either signal suffices: the conductor sets ATMOFAB_ORCHESTRATION_ID per child
        # on top of the run-wide ATMOFAB_WORKFLOW_MODE.
        os.environ["ATMOFAB_ORCHESTRATION_ID"] = "orch_x"
        with self.assertRaises(ValueError) as ctx:
            self._call("run_linter", self._args("run_linter"))
        self.assertIn("ATMOFAB_ORCHESTRATION_ID", str(ctx.exception))

    def test_workflow_mode_off_is_not_workflow_mode(self) -> None:
        # `0` is the explicit non-workflow spelling (`tools/hooks/cli.py` uses it too)
        # and must not be read as merely "set". Every OTHER value counts as workflow
        # mode: the hook layer's allowlist (`{"1","true","yes"}`) is the fail-open
        # direction for a check whose job is to refuse.
        os.environ["ATMOFAB_WORKFLOW_MODE"] = "0"
        result = self._call("run_syntax_check", self._args("run_syntax_check"))
        self.assertTrue(result["skipped"])

    def test_standalone_mode_allows_gated_tool_without_orchestration_id(self) -> None:
        result = self._call("run_syntax_check", self._args("run_syntax_check"))
        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])

    def test_detect_build_system_is_refused_under_the_workflow(self) -> None:
        # It holds no capability by design (no substep is granted it), but it reports
        # which marker files exist in any directory it is pointed at and the resolved
        # path of that directory — a read outside the manifest boundary, with no command
        # log to attribute it. Refused under the workflow, usable standalone.
        os.environ["ATMOFAB_WORKFLOW_MODE"] = "1"
        with self.assertRaises(ValueError) as ctx:
            self.mod.tool_detect_build_system({"project_dir": str(self.project_dir)})
        self.assertIn("not available under the workflow", str(ctx.exception))
        os.environ.pop("ATMOFAB_WORKFLOW_MODE")
        result = self.mod.tool_detect_build_system({"project_dir": str(self.project_dir)})
        self.assertEqual(result["recommended_build_system"], "make")

    def test_refusal_precedes_loading_the_orchestration_runtime(self) -> None:
        # The refusal cannot be defeated by pointing the call at a directory with no
        # orchestration workspace: it never reaches a filesystem lookup.
        os.environ["ATMOFAB_WORKFLOW_MODE"] = "1"
        with mock.patch.object(self.mod, "_load_orchestration_runtime") as loader:
            with self.assertRaises(ValueError):
                self._call("compile_project", self._args("compile_project"))
        loader.assert_not_called()


class EnvOverrideDenylistTests(unittest.TestCase):
    """Caller-supplied `env` may not redirect what runs.

    argv is constrained to fixed presets and build-tool invocations (except
    `run_program`, whose `command` is caller-chosen by design), but before this
    `_run_command` merged the caller's `env` into `os.environ` unfiltered, so
    `LD_PRELOAD` / `PATH` / `BASH_ENV` walked around that constraint.

    These cases cover the standalone guardrail. The workflow rule is an allowlist and
    is covered by OrchestratedEnvAllowlistTests below — a denylist cannot be complete,
    because every program these tools run reads its own configuration from the
    environment."""

    UNSAFE = (
        "LD_PRELOAD",
        "LD_AUDIT",
        "LD_LIBRARY_PATH",
        "DYLD_INSERT_LIBRARIES",
        "BASH_ENV",
        "ENV",
        "IFS",
        "PATH",
        "PYTHONPATH",
        # The gcc driver finds and execs its own front end through these: with
        # COMPILER_PATH pointing at a directory holding an executable `f951`,
        # run_syntax_check returned ok=True on Fortran no compiler had parsed.
        "COMPILER_PATH",
        "GCC_EXEC_PREFIX",
        "LIBRARY_PATH",
        # GNU make reads these as switches / extra makefiles, so
        # MAKEFLAGS='--eval=$(shell ...)' runs before the certified Makefile is read.
        "MAKEFLAGS",
        "GNUMAKEFLAGS",
        "MAKEFILES",
        "MAKESHELL",
    )

    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_server_module()

    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("ATMOFAB_WORKFLOW_MODE", "ATMOFAB_ORCHESTRATION_ID"):
            os.environ.pop(name, None)
        self.project_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.project_dir, ignore_errors=True)

    def _spy_run_command(self):
        return mock.patch.object(
            self.mod,
            "_run_command",
            return_value={"ok": True, "return_code": 0, "stdout": "", "stderr": ""},
        )

    def _args(self, tool: str, env: dict) -> dict:
        args: dict = {"project_dir": str(self.project_dir), "env": env}
        if tool == "run_program":
            args["command"] = ["true"]
        if tool == "compile_project":
            args["build_system"] = "make"
        return args

    def test_denylisted_keys_are_refused_by_every_env_accepting_tool(self) -> None:
        tools = (
            "compile_project",
            "run_program",
            "run_quality_checks",
            "run_linter",
            "run_syntax_check",
        )
        for tool in tools:
            for key in self.UNSAFE:
                with self.subTest(tool=tool, key=key):
                    with self._spy_run_command() as run_command:
                        with self.assertRaises(ValueError) as ctx:
                            getattr(self.mod, f"tool_{tool}")(
                                self._args(tool, {key: "/tmp/evil"}))
                    self.assertIn(key, str(ctx.exception))
                    # Refused, not stripped after the merge: nothing ran.
                    run_command.assert_not_called()

    def test_denylist_is_case_insensitive_and_prefix_exact(self) -> None:
        with self._spy_run_command():
            with self.assertRaises(ValueError):
                self.mod.tool_run_linter(self._args("run_linter", {"ld_preload": "x"}))
        # Neighbours that merely look similar stay usable.
        with self._spy_run_command() as run_command:
            self.mod.tool_run_linter(
                self._args("run_linter", {"LDFLAGS": "-lm", "ENVIRONMENT": "ci"}))
        self.assertEqual(
            run_command.call_args.kwargs["env"], {"LDFLAGS": "-lm", "ENVIRONMENT": "ci"})

    def test_standalone_denylist_is_not_claimed_to_be_complete(self) -> None:
        # Names that redirect execution just as effectively and are NOT refused
        # standalone: make imports any environment name as a make variable, so `FC`
        # replaces the compiler a certified Makefile invokes. This is what the
        # orchestrated allowlist exists for; pinned here so the standalone rule is not
        # mistaken for a boundary.
        with self._spy_run_command() as run_command:
            self.mod.tool_run_quality_checks(
                self._args("run_quality_checks", {"FC": "/tmp/evil-gfortran"}))
        self.assertEqual(run_command.call_args.kwargs["env"], {"FC": "/tmp/evil-gfortran"})

    def test_conductor_quality_check_env_payload_is_accepted(self) -> None:
        # The only caller-supplied env in the repository (Validate.execute's make_test
        # re-run) must survive the denylist unmodified.
        payload = {
            "OBJDIR": "/tmp/obj", "BINDIR": "/tmp/bin", "RUNDIR": "/tmp/run",
            "BIN": "sw2d_runner", "SPEC": "/tmp/spec.ir.yaml", "CASES": "c1 c2",
        }
        with self._spy_run_command() as run_command:
            self.mod.tool_run_quality_checks(
                {"project_dir": str(self.project_dir), "preset": "make_test",
                 "env": dict(payload)})
        self.assertEqual(run_command.call_args.kwargs["env"], payload)

    def test_server_injected_env_is_not_subject_to_the_denylist(self) -> None:
        # The check sits where the caller's argument is read, so the server's own
        # additions still happen. PYTHONPATH for the pytest preset...
        with self._spy_run_command() as run_command:
            self.mod.tool_run_quality_checks(
                {"project_dir": str(self.project_dir), "preset": "pytest"})
        # project_dir goes first; anything after it is this server's own inherited
        # PYTHONPATH, which varies with how the suite was started.
        self.assertEqual(
            run_command.call_args.kwargs["env"]["PYTHONPATH"].split(os.pathsep)[0],
            str(self.project_dir.resolve()))
        # ...and OMP_* for a CPU run_program.
        with self._spy_run_command() as run_command:
            self.mod.tool_run_program({
                "project_dir": str(self.project_dir), "command": ["true"],
                "target": {"class": "cpu"}, "threads_per_rank": 4})
        self.assertEqual(run_command.call_args.kwargs["env"]["OMP_NUM_THREADS"], "4")


class OrchestratedEnvAllowlistTests(unittest.TestCase):
    """Under an orchestration the caller's `env` is an allowlist, not a denylist.

    A denylist over environment names cannot be finished: the loader reads `LD_*`, the
    gcc driver reads `COMPILER_PATH` to find the front end it execs, and make reads
    `MAKEFLAGS` as switches and imports every other name as a make variable. Only the
    keys `Validate.execute` declares are accepted, so a name nobody has thought of is
    refused by construction."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_server_module()

    def setUp(self) -> None:
        self.repo_root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo_root, ignore_errors=True)

    def _check(self, env: dict, tool: str = "run_quality_checks") -> None:
        self.mod._validate_env_overrides(
            env, tool, orchestrated=True, repo_root=self.repo_root)

    def _paths(self, **extra: str) -> dict:
        payload = {
            "OBJDIR": f"{self.repo_root}/workspace/tmp/a/obj",
            "BINDIR": f"{self.repo_root}/workspace/binary/bin_1/bin",
            "RUNDIR": f"{self.repo_root}/workspace/tmp/a/qc_run",
            "BIN": "sw2d_runner",
            "SPEC": f"{self.repo_root}/workspace/ir/x/spec.ir.yaml",
            "CASES": "case_a case_b",
        }
        payload.update(extra)
        return payload

    def test_declared_make_variables_are_accepted(self) -> None:
        self._check(self._paths())

    def test_everything_else_is_refused(self) -> None:
        for key in ("FC", "CC", "CFLAGS", "MAKEFLAGS", "LD_PRELOAD", "COMPILER_PATH",
                    "SOMETHING_NOBODY_LISTED"):
            with self.subTest(key=key):
                with self.assertRaises(ValueError) as ctx:
                    self._check({key: "x"})
                self.assertIn(key, str(ctx.exception))
                # The message tells the caller what IS accepted.
                self.assertIn("OBJDIR", str(ctx.exception))

    def test_allowlist_is_case_exact(self) -> None:
        # `objdir` is a different environment variable: accepting it would leave the
        # make_test re-run on the Makefile's own OBJDIR default instead of failing. The
        # value is one a folded key would ACCEPT, so only the key rule can refuse it.
        with self.assertRaises(ValueError) as ctx:
            self._check({"objdir": "runner"})
        self.assertIn("accepts only these env overrides", str(ctx.exception))

    def test_the_case_id_grammar_fits_the_CASES_value_rule(self) -> None:
        # Validate.execute joins the IR's case ids into CASES, so every id the Compile
        # gate accepts must be a word this rule accepts. Otherwise a run passes Compile
        # and Build and fails several phases later on an id no gate objected to.
        from tools.spec_input_gates import CASE_ID_TOKEN_RE
        for case_id in ("c1", "l0_v1.2-alpha", "A.b_c-d", "9x"):
            with self.subTest(case_id=case_id):
                self.assertTrue(CASE_ID_TOKEN_RE.match(case_id))
                self.assertTrue(self.mod._MAKE_NAME_VALUE_RE.match(case_id))
        # The Compile grammar is the regex plus a separate `..` exclusion; the ids it
        # refuses outright are refused here too.
        for rejected in ("-c1", "a/b", "x y"):
            with self.subTest(rejected=rejected):
                self.assertIsNone(CASE_ID_TOKEN_RE.match(rejected))
                self.assertIsNone(self.mod._MAKE_NAME_VALUE_RE.match(rejected))

    def test_tools_the_workflow_passes_no_env_to_accept_none(self) -> None:
        # `run_program` / `run_linter` / `run_syntax_check` are never given a caller env
        # by the workflow, and a make variable means nothing to a linter, so their
        # allowlist is empty rather than the six.
        for tool in ("compile_project", "run_program", "run_linter", "run_syntax_check"):
            with self.subTest(tool=tool):
                with self.assertRaises(ValueError) as ctx:
                    self._check({"OBJDIR": f"{self.repo_root}/obj"}, tool=tool)
                self.assertIn("(none)", str(ctx.exception))

    def test_allowlist_matches_the_conductor_payload(self) -> None:
        # The set is exactly what Validate.execute passes (workflow_conductor.py's
        # make_test re-run, canonical in docs/workflow/phases/phase_04_validate.md).
        # If that payload grows, this fails rather than the run failing mid-phase.
        self.assertEqual(
            self.mod._MAKE_VARIABLE_ALLOWLIST,
            frozenset({"OBJDIR", "BINDIR", "RUNDIR", "BIN", "SPEC", "CASES"}))

    def test_values_that_reach_the_recipe_shell_are_refused(self) -> None:
        # The host-authored Makefile interpolates these unquoted into a recipe line
        # (`cd $(RUNDIR) && $(BINDIR)/$(BIN) --cases $(SPEC) $(CASES)`), so an accepted
        # key with a metacharacter in its value is a command. Names alone are not the
        # rule.
        for value in ("c1; touch /tmp/x", "c1 && id", "$(shell id)", "`id`", "a|b",
                      "a\nb", "a>b", "'x'", "c1 --evil"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as ctx:
                    self._check({"CASES": value})
                self.assertIn("reach the make recipe's shell", str(ctx.exception))

    def test_a_path_value_must_land_inside_the_repository(self) -> None:
        # BINDIR alone points the recipe at any executable on the machine, so a path
        # value is judged by where it resolves, not by its spelling.
        for value in ("/usr/bin", "../../usr/bin", f"{self.repo_root}/../etc"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as ctx:
                    self._check({"BINDIR": value})
                self.assertIn("inside the repository", str(ctx.exception))
    def test_a_path_value_must_be_absolute(self) -> None:
        # Refused even when the relative value WOULD land inside the repository: make
        # reads it from its own working directory (or from wherever `cd $(RUNDIR)` left
        # it), never from the root this check measures against, so a relative value can
        # only ever be checked as a different path from the one that runs.
        (self.repo_root / "workspace/binary/bin_1/bin").mkdir(parents=True)
        cwd = os.getcwd()
        os.chdir(self.repo_root)
        self.addCleanup(os.chdir, cwd)
        self.assertTrue(
            Path("workspace/binary/bin_1/bin").resolve().is_relative_to(self.repo_root))
        with self.assertRaises(ValueError) as ctx:
            self._check({"BINDIR": "workspace/binary/bin_1/bin"})
        self.assertIn("absolute", str(ctx.exception))

    def test_a_path_value_may_hold_any_character_the_filesystem_does(self) -> None:
        # The rule for a path is containment, not a character class: a checkout whose
        # path is non-ASCII must not fail every Build with a message about shell
        # metacharacters.
        nested = self.repo_root / "作業" / "build"
        self._check({"OBJDIR": str(nested)})

    def test_bin_is_one_name_not_a_list(self) -> None:
        # `BIN` is a command name in `$(BINDIR)/$(BIN) --cases ...`, so a space in it
        # appends an argument to the runner. Only CASES is a list.
        for value in ("runner extra", "/bin/sh", "../../bin/sh", "sub/runner"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as ctx:
                    self._check({"BIN": value})
                self.assertIn(f"BIN={value}", str(ctx.exception))
        self._check({"CASES": "case_a case_b"})

    def test_a_path_value_may_not_hold_a_shell_metacharacter(self) -> None:
        # Absolute, inside the repository, no space — and still a command, because the
        # recipe interpolates it unquoted. Containment cannot see this; this rule is
        # the only thing between it and `cd $(RUNDIR) && $(BINDIR)/$(BIN) ...`.
        for value in (f"{self.repo_root}/x`id`", f"{self.repo_root}/a$FOO",
                      f"{self.repo_root}/a;id", f"{self.repo_root}/a|id"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as ctx:
                    self._check({"OBJDIR": value})
                self.assertIn("reach the make recipe's shell", str(ctx.exception))

    def test_a_refusal_caused_by_the_checkout_path_names_it(self) -> None:
        # Otherwise the message blames a value the caller composed correctly from a
        # repo_root it does not choose.
        root = Path(tempfile.mkdtemp()) / "my repo"
        root.mkdir()
        self.addCleanup(shutil.rmtree, root.parent, ignore_errors=True)
        with self.assertRaises(ValueError) as ctx:
            self.mod._validate_env_overrides(
                {"OBJDIR": f"{root}/obj"}, "run_quality_checks",
                orchestrated=True, repo_root=root)
        self.assertIn("repository path itself", str(ctx.exception))
        # Not appended to a refusal it did not cause: BIN is never composed from the
        # checkout path, so the clause would explain the wrong thing.
        with self.assertRaises(ValueError) as ctx_name:
            self.mod._validate_env_overrides(
                {"BIN": "my runner"}, "run_quality_checks",
                orchestrated=True, repo_root=root)
        self.assertNotIn("repository path itself", str(ctx_name.exception))

    def test_a_path_value_may_not_hold_a_space(self) -> None:
        # The other half of the path rule: `<repo>/a b` resolves inside the repository
        # and still word-splits in `cd $(RUNDIR) && $(BINDIR)/$(BIN) …`.
        with self.assertRaises(ValueError) as ctx:
            self._check({"OBJDIR": f"{self.repo_root}/a b"})
        self.assertIn("reach the make recipe's shell", str(ctx.exception))

    def test_an_empty_path_or_command_value_is_refused(self) -> None:
        # `RUNDIR=` is not "unset": make imports it as set, so `?=` keeps the default
        # away and `cd $(RUNDIR)` becomes a bare `cd`, which lands in the home
        # directory. `$(OBJDIR)/x` likewise becomes an absolute path at the root.
        for key in ("OBJDIR", "BINDIR", "RUNDIR", "SPEC", "BIN"):
            with self.subTest(key=key):
                with self.assertRaises(ValueError) as ctx:
                    self._check({key: ""})
                self.assertIn(key, str(ctx.exception))
        # An empty case list is a list.
        self._check({"CASES": ""})

    def test_only_an_absent_repo_root_falls_back_to_project_dir(self) -> None:
        # A present but empty `repo_root` is a value, not an omission — the capability
        # gate reads it that way, so every containment check must too, or the caller
        # picks the root its paths are measured from.
        self.assertNotEqual(
            self.mod._repo_root_for_call({"repo_root": ""}, "/some/caller/dir"),
            Path("/some/caller/dir"))
        self.assertEqual(
            self.mod._repo_root_for_call({}, str(self.repo_root)), self.repo_root)

    def test_under_the_workflow_the_root_must_be_the_servers_own_checkout(self) -> None:
        """Everything the capability gate trusts is read from under `repo_root`, so a
        caller that names its own root brings its own evidence. A leaf can write a whole
        orchestration tree in the scratch directory the agent contract grants it and hold
        a capability it wrote itself; the root has to be the server's checkout."""
        checkout = self.mod._server_checkout_root()
        with mock.patch.dict(os.environ, {"ATMOFAB_WORKFLOW_MODE": "1"}):
            with self.assertRaises(ValueError) as ctx:
                self.mod._repo_root_for_call(
                    {"repo_root": str(self.repo_root)}, str(self.repo_root))
            self.assertIn("must be this server's own checkout", str(ctx.exception))
            # project_dir is the fallback, so it cannot name a root either.
            with self.assertRaises(ValueError):
                self.mod._repo_root_for_call({}, str(self.repo_root))
            # The real checkout is accepted.
            self.assertEqual(
                self.mod._repo_root_for_call({"repo_root": str(checkout)}, "/x"), checkout)
        # Outside a run there is no orchestration to anchor.
        self.assertEqual(
            self.mod._repo_root_for_call(
                {"repo_root": str(self.repo_root)}, "/x"), self.repo_root)

    def test_a_forged_orchestration_tree_is_refused_through_the_handler(self) -> None:
        # End to end: a complete, self-consistent orchestration tree the caller wrote —
        # preflight, phase_state, launch record, capability with its own token — buys
        # nothing, because the gate never reads it.
        forged = self.repo_root / "fake"
        orch = forged / "workspace/orchestrations/x"
        (orch / "launches").mkdir(parents=True)
        (orch / "capabilities").mkdir(parents=True)
        (orch / "preflight.json").write_text(json.dumps({"status": "pass"}), encoding="utf-8")
        (orch / "phase_state.json").write_text(
            json.dumps({"current_state": "preflight_passed"}), encoding="utf-8")
        (orch / "launches/evil.response.json").write_text("{}", encoding="utf-8")
        (orch / "capabilities/evil.json").write_text(
            json.dumps({"capability_token": "attacker-chosen",
                        "mcp_permissions": ["run_program"]}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"ATMOFAB_WORKFLOW_MODE": "1"}):
            with self.assertRaises(ValueError) as ctx:
                self.mod.tool_run_program({
                    "project_dir": str(forged), "repo_root": str(forged),
                    "orchestration_id": "x", "agent_run_id": "evil",
                    "capability_token": "attacker-chosen", "command": ["true"]})
        self.assertIn("must be this server's own checkout", str(ctx.exception))

    def test_the_numeric_arguments_are_held_to_their_declared_minimums(self) -> None:
        # The served schema declares these minimums and an MCP argument schema is
        # advisory, so the server enforces them. `make -j-5` waits forever.
        for raw, minimum, name in ((0, 1, "jobs"), (-5, 1, "jobs"), (0, 1, "timeout_sec"),
                                   (999, 1000, "capture_limit")):
            with self.subTest(name=name, raw=raw):
                with self.assertRaises(ValueError) as ctx:
                    self.mod._bounded_int(raw, 10, minimum, name)
                self.assertIn(f"{name} must be >= {minimum}", str(ctx.exception))
        # An absent value and an explicit null both take the default.
        self.assertEqual(self.mod._bounded_int(None, 7, 1, "jobs"), 7)
        self.assertEqual(self.mod._bounded_int(3, 7, 1, "jobs"), 3)

    def test_argv_values_are_refused_the_same_way(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self.mod._validate_build_argv_overrides(
                None, ["OBJDIR=obj; touch /tmp/x;"], "compile_project",
                orchestrated=True, repo_root=self.repo_root)
        self.assertIn("reach the make recipe's shell", str(ctx.exception))

    def test_every_assignment_is_checked_not_just_the_last(self) -> None:
        # Both elements reach the command line, so a duplicate key must not let the
        # first one through unchecked.
        # The LAST assignment is the one make keeps, so it is valid here: only the
        # earlier element is dangerous, and collapsing the pairs into a mapping would
        # drop exactly that one.
        good = f"OBJDIR={self.repo_root}/obj"
        with self.assertRaises(ValueError) as ctx:
            self.mod._validate_build_argv_overrides(
                None, [f"OBJDIR={self.repo_root}/obj; touch /tmp/x;", good],
                "compile_project", orchestrated=True, repo_root=self.repo_root)
        self.assertIn("touch /tmp/x", str(ctx.exception))

    def test_a_target_is_refused_under_an_orchestration(self) -> None:
        # Build names no target, and a target runs whatever else the Makefile defines
        # under a grant that covers compiling.
        with self.assertRaises(ValueError) as ctx:
            self.mod._validate_build_argv_overrides(
                "test", [], "compile_project", orchestrated=True,
                repo_root=self.repo_root)
        self.assertIn("does not accept a target", str(ctx.exception))

    def test_standalone_argv_is_the_operators_own(self) -> None:
        # The deliberate counterpart to the standalone env guardrail: outside an
        # orchestration the caller already chose the command, so `target` and
        # `extra_args` are not restricted. Pinned so the asymmetry is a decision.
        self.assertEqual(
            self.mod._validate_build_argv_overrides(
                "all", ["-j2", "FC=gfortran-13"], "compile_project", orchestrated=False,
                repo_root=self.repo_root),
            "all")

    def test_non_string_argv_arguments_are_refused_not_crashed(self) -> None:
        for target, extra in ((True, []), (3, []), (None, 7), (None, [1])):
            with self.subTest(target=target, extra_args=extra):
                with self.assertRaises(ValueError):
                    self.mod._validate_build_argv_overrides(
                        target, extra, "compile_project", orchestrated=True,
                        repo_root=self.repo_root)

    def test_blank_orchestration_id_is_not_orchestrated(self) -> None:
        # The gate reads a blank orchestration_id as absent; this predicate must agree,
        # or a whitespace value would pick a different rule than the gate applied.
        self.assertFalse(self.mod._is_orchestrated_call({"orchestration_id": "   "}))
        self.assertFalse(self.mod._is_orchestrated_call({}))
        self.assertTrue(self.mod._is_orchestrated_call({"orchestration_id": "orch_x"}))


class SyntaxCheckSourcesTests(_StandaloneServerEnvMixin, unittest.TestCase):
    """`sources` is appended to the compiler front-end argv, so it is argv, not data.

    The gcc driver reads its own options anywhere in that list: `-B<dir>/` execs a
    planted `f951` and `@file` reads further options out of a file — whose own name may
    end in `.f90` — and either way the check returns ok=True having compiled something
    other than what was staged. The rule is what a source name IS. Refused in every
    mode; the workflow never passes this argument at all."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_server_module()

    def setUp(self) -> None:
        super().setUp()
        self.project_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.project_dir, ignore_errors=True)
        (self.project_dir / "a.f90").write_text("program p\nend program p\n", encoding="utf-8")

    def _call(self, sources: list) -> dict:
        return self.mod.tool_run_syntax_check(
            {"project_dir": str(self.project_dir), "sources": sources})

    def test_an_explicit_source_is_refused_without_a_compiler_too(self) -> None:
        # The skip for an uninstalled compiler used to return first, so `/etc/passwd`
        # came back `{ok: True, skipped: True}` on a machine without gfortran.
        with mock.patch.object(self.mod.shutil, "which", return_value=None):
            with self.assertRaises(ValueError) as ctx:
                self._call(["/etc/passwd"])
        self.assertIn("Fortran source files in project_dir", str(ctx.exception))

    def test_a_clean_tree_still_skips_when_the_compiler_is_absent(self) -> None:
        with mock.patch.object(self.mod.shutil, "which", return_value=None):
            result = self.mod.tool_run_syntax_check({"project_dir": str(self.project_dir)})
        self.assertTrue(result["skipped"])
        self.assertIn("compiler not available", result["reason"])

    def test_anything_that_is_not_a_staged_source_file_is_refused(self) -> None:
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        (outside / "b.f90").write_text("program q\nend program q\n", encoding="utf-8")
        (self.project_dir / "resp.f90").write_text("-Bfake/\na.f90\n", encoding="utf-8")
        (self.project_dir / "link.f90").symlink_to(outside / "b.f90")
        for bad in (
            ["-B/tmp/fake/", "a.f90"],          # option: exec a planted front end
            ["--param=x", "a.f90"],
            ["@resp.f90"],                      # response file: options out of a file
            ["../outside/a.f90"],
            ["/etc/passwd"],
            ["notes.txt"],                      # not a source
            ["missing.f90"],                    # not staged
            ["link.f90"],                       # symlink out of project_dir
        ):
            with self.subTest(sources=bad):
                with self.assertRaises(ValueError) as ctx:
                    self._call(bad)
                self.assertIn("Fortran source files in project_dir", str(ctx.exception))

    def test_a_staged_file_whose_NAME_is_an_option_is_still_refused(self) -> None:
        # The leaf writes its own src/, so it can stage a file called `@resp.f90` — a
        # real regular file in project_dir, which the containment half accepts. The gcc
        # driver reads `@file` as a list of further options regardless of suffix, so the
        # name shape is the half that refuses it.
        (self.project_dir / "@resp.f90").write_text("-Bfake/\na.f90\n", encoding="utf-8")
        (self.project_dir / "-Bx.f90").write_text("program r\nend program r\n", encoding="utf-8")
        for name in ("@resp.f90", "-Bx.f90"):
            with self.subTest(name=name):
                self.assertTrue((self.project_dir / name).is_file())
                with self.assertRaises(ValueError) as ctx:
                    self._call([name])
                self.assertIn("Fortran source files in project_dir", str(ctx.exception))

    def test_the_source_suffixes_are_the_tool_s_own(self) -> None:
        # The name rule is built from the same tuple auto-discovery uses, so an added
        # suffix cannot make an explicit `sources` list refuse a file the tool would
        # otherwise have found itself.
        for suffix in self.mod._FORTRAN_SYNTAX_SOURCE_SUFFIXES:
            with self.subTest(suffix=suffix):
                self.assertTrue(self.mod._build_syntax_source_re().match(f"a{suffix}"))

    def test_auto_discovery_is_held_to_the_same_rule(self) -> None:
        # The workflow never passes `sources`, so this is the reading that actually
        # runs. Auto-discovery filters on suffix alone, so a staged `-o.f90` walked
        # into the compiler argv as an option and the file was never parsed.
        (self.project_dir / "-o.f90").write_text("program r\nend program r\n",
                                                 encoding="utf-8")
        # Refused whether or not a compiler is installed: the rule is about the names,
        # not about what a compiler would do with them, and an optional stage skipping on
        # a machine without that compiler must not be why a bad name goes unnoticed.
        for which in ("/usr/bin/gfortran", None):
            with self.subTest(compiler_installed=which is not None):
                with mock.patch.object(self.mod.shutil, "which", return_value=which), \
                        self.assertRaises(ValueError) as ctx:
                    self.mod.tool_run_syntax_check({"project_dir": str(self.project_dir)})
                self.assertIn("Fortran source files in project_dir", str(ctx.exception))

    def test_staged_source_names_are_accepted(self) -> None:
        with mock.patch.object(self.mod.shutil, "which", return_value=None):
            result = self._call(["a.f90"])
        # Reaches the ordinary missing-compiler skip, i.e. it was not refused.
        self.assertTrue(result["skipped"])


class GatedHandlerWiringTests(unittest.TestCase):
    """Every gated handler must apply the orchestrated rules, and must choose them per
    call.

    The restriction is selected from `orchestration_id` at each call site, so a site
    that asked for the standalone rule instead would leave the workflow on the weaker
    one — with every helper-level test still green. Asserting the call literal is how
    `test_mcp_grant_table_matches_conductor_call_sites` pins the capability gate for the
    same reason."""

    GATED_TOOLS = (
        "compile_project", "run_program", "run_quality_checks", "run_linter",
        "run_syntax_check",
    )

    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_server_module()

    def test_each_gated_handler_applies_the_orchestrated_rules(self) -> None:
        import inspect
        for tool in self.GATED_TOOLS:
            source = inspect.getsource(getattr(self.mod, f"tool_{tool}"))
            with self.subTest(tool=tool):
                self.assertIn("_maybe_enforce_orchestration_mcp_gate", source)
                self.assertIn(
                    f'_validate_env_overrides(\n        env, "{tool}", '
                    "orchestrated=_is_orchestrated_call(args),",
                    source)
                self.assertIn(
                    f'_validate_orchestrated_paths(command_log_path, args, project_dir, "{tool}")',
                    source)


class ToolSchemaDocumentParityTests(unittest.TestCase):
    """`mcp_servers/tools/*.json` must say what the served schema says.

    The server does not load them — `TOOLS` in the module is what a client is served —
    but the harness reads them at startup and so do people, and until this test they
    were free to go on describing a call shape the server refuses (they carried no
    orchestration properties at all and an unrestricted `env`). Only the two tools that
    have a document are covered; the other four are served-schema-only by choice."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_server_module()

    def test_documents_match_the_served_schema(self) -> None:
        doc_dir = _SERVER_PATH.parent / "tools"
        served = {
            name: tool.input_schema["properties"] for name, tool in self.mod.TOOLS.items()
        }
        documents = sorted(doc_dir.glob("*.json"))
        self.assertTrue(documents, "no tool schema documents found")
        for path in documents:
            with self.subTest(document=path.name):
                doc = json.loads(path.read_text(encoding="utf-8"))
                name = doc["name"]
                self.assertIn(name, served, f"{path.name} documents an unserved tool")
                for key, spec in doc["arguments"]["properties"].items():
                    self.assertIn(key, served[name],
                                  f"{path.name} documents an argument the tool does not take")
                    if "description" in spec or "description" in served[name][key]:
                        self.assertEqual(
                            spec.get("description"), served[name][key].get("description"),
                            f"{path.name}:{key} description differs from the served schema")
                # Both directions: a property added to the served schema must appear in
                # the document too, or the document quietly describes a smaller tool.
                self.assertEqual(
                    set(doc["arguments"]["properties"]), set(served[name]),
                    f"{path.name} and the served schema declare different arguments")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class RunLinterPresetDispatchTests(unittest.TestCase):
    """The preset -> argv table `tool_run_linter` runs and the launch-time host probe reads.

    These were an if-chain with `mixed` restating `fortitude`'s and `cppcheck`'s command lines a
    second time, and `mixed` had no test at all. The refactor that made the argv readable for the
    probe (issue #109) rewrote that path, so the shapes are pinned here: a simple preset returns
    the command's own keys plus `preset`, a composite returns `runs`, and both spellings are what
    the conductor's `_gate_lint_check` normalizes and what the lint evidence records.
    """

    def setUp(self) -> None:
        self.mod = _load_server_module()
        self.calls: list[list[str]] = []

        def fake_run_command(*, command, **kwargs):
            self.calls.append(list(command))
            return {"ok": True, "command_id": f"cid{len(self.calls)}", "return_code": 0,
                    "stdout": "", "stderr": "", "command": list(command)}

        self.patch = mock.patch.object(self.mod, "_run_command", fake_run_command)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_a_simple_preset_returns_the_flat_shape_and_runs_one_command(self) -> None:
        result = self.mod.tool_run_linter({"preset": "fortitude", "project_dir": "."})
        self.assertEqual(result["preset"], "fortitude")
        self.assertTrue(result["ok"])
        self.assertNotIn("runs", result)
        self.assertEqual(self.calls, [list(self.mod._LINT_PRESET_COMMANDS["fortitude"])])

    def test_a_composite_preset_runs_each_sub_preset_in_order(self) -> None:
        result = self.mod.tool_run_linter({"preset": "mixed", "project_dir": "."})
        subs = self.mod._LINT_PRESET_COMPOSITES["mixed"]
        self.assertEqual(result["preset"], "mixed")
        self.assertEqual([entry["sub_preset"] for entry in result["runs"]], list(subs))
        self.assertEqual(
            self.calls, [list(self.mod._LINT_PRESET_COMMANDS[sub]) for sub in subs])
        # each sub-run keeps its own command_id, which is what the lint evidence records
        self.assertEqual(
            len({entry["command_id"] for entry in result["runs"]}), len(subs))

    def test_a_composite_is_not_ok_when_any_sub_run_is_not(self) -> None:
        original = self.mod._run_command

        def failing_second(*, command, **kwargs):
            out = original(command=command, **kwargs)
            if len(self.calls) == 2:
                out["ok"] = False
            return out

        with mock.patch.object(self.mod, "_run_command", failing_second):
            result = self.mod.tool_run_linter({"preset": "mixed", "project_dir": "."})
        self.assertFalse(result["ok"])

    def test_an_unsupported_preset_names_the_supported_set_before_running_anything(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self.mod.tool_run_linter({"preset": "no_such_linter", "project_dir": "."})
        message = str(caught.exception)
        self.assertIn("unsupported preset: no_such_linter", message)
        for preset in list(self.mod._LINT_PRESET_COMMANDS) + list(self.mod._LINT_PRESET_COMPOSITES):
            self.assertIn(preset, message)
        self.assertEqual(self.calls, [])

    def test_a_custom_command_is_still_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.mod.tool_run_linter(
                {"preset": "fortitude", "project_dir": ".", "command": ["rm", "-rf", "/"]})
        self.assertEqual(self.calls, [])

    def test_a_composite_names_only_presets_the_command_table_defines(self) -> None:
        """A composite that named an unknown sub-preset would `KeyError` mid-run, after its
        earlier sub-runs had already executed."""
        for preset, subs in self.mod._LINT_PRESET_COMPOSITES.items():
            for sub in subs:
                self.assertIn(sub, self.mod._LINT_PRESET_COMMANDS, f"{preset} -> {sub}")

    def test_no_simple_preset_argv_is_spelled_in_the_neutral_core(self) -> None:
        """Every simple preset's argv comes from its backend package, not from this module.

        The property issue #120 closed. It is pinned by IDENTITY against the package rather than
        by the absence of a table: a reinstated table would satisfy "there is no
        `_INLINE_LINT_PRESET_COMMANDS`" the moment it were given another name, and would not
        satisfy this.
        """
        from tools.backends import registry

        self.assertFalse(hasattr(self.mod, "_INLINE_LINT_PRESET_COMMANDS"))
        for preset in self.mod._SIMPLE_LINT_PRESETS:
            record = registry.get("linter", preset)
            self.assertIn("lint", record.backend_provides, preset)
            module = registry.capability_module("linter", preset, "lint")
            self.assertEqual(self.mod._LINT_PRESET_COMMANDS[preset], tuple(module.check_argv()),
                             preset)

    def test_each_arm_of_the_import_time_declaration_check_refuses(self) -> None:
        """All three raises, driven one at a time. Before this the count driven was ZERO.

        The guard had four arms and the orphan one was the only one with a test; issue #120
        deleted that arm together with the table it watched, and took the file's only witness for
        this function with it. What the three survivors protect is not decorative: a name in both
        tables makes `lint_preset_sub_presets` and `tool_run_linter`'s result-shape branch
        disagree about whether a preset is simple, so one preset returns two shapes depending on
        which reader asked; a composite naming a preset with no command row `KeyError`s mid-run,
        AFTER its earlier sub-runs have already executed; and a default with no row breaks every
        caller that names no preset. Each is reached from `Generate.gate` through `run_linter`.

        Driven with synthetic tables because today's declarations are consistent — which is the
        point of an import-time check — and each arm is asserted to name the offending preset, so
        a guard that raised the wrong message would fail rather than pass on the raise alone.
        """
        cases = (
            ("both simple and composite",
             {"_LINT_PRESET_COMPOSITES": {**self.mod._LINT_PRESET_COMPOSITES,
                                          "fortitude": ("fortitude",)}},
             "fortitude"),
            ("a composite naming an unregistered sub-preset",
             {"_LINT_PRESET_COMPOSITES": {**self.mod._LINT_PRESET_COMPOSITES,
                                          "zz_composite": ("zz_absent",)}},
             "zz_absent"),
            ("a default preset with no command row",
             {"DEFAULT_LINT_PRESET": "zz_absent"},
             "zz_absent"),
        )
        for label, attrs, expected in cases:
            with self.subTest(arm=label):
                with mock.patch.multiple(self.mod, **attrs):
                    with self.assertRaises(ValueError) as caught:
                        self.mod._check_lint_preset_declarations()
                self.assertIn(expected, str(caught.exception))

    def test_the_declaration_check_accepts_the_real_tables(self) -> None:
        """The other direction, so the three arms above cannot pass by refusing everything."""
        self.mod._check_lint_preset_declarations()

    def test_a_preset_whose_record_declares_no_package_is_refused_by_name(self) -> None:
        """`mixed` is the live instance: a composite carries `lint` in `core_provides`, so
        `_lint_preset_command` must refuse it rather than compose an argv for it.

        Before issue #120 the refusal was a `KeyError` out of the inlined table, which named
        nothing. It is now the registry's, which names the record and the capability. Driven
        through the real function on the real declaration, not a synthetic one.
        """
        with self.assertRaises(Exception) as caught:
            self.mod._lint_preset_command("mixed")
        message = str(caught.exception)
        self.assertIn("mixed", message)
        self.assertIn("lint", message)

