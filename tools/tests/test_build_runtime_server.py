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
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import typing
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


class EnvOverrideDenylistTests(unittest.TestCase):
    """Caller-supplied `env` may not redirect what runs.

    argv is constrained to fixed presets and build-tool invocations (except
    `run_program`, whose `command` is caller-chosen by design), but before this
    `_run_command` merged the caller's `env` into `os.environ` unfiltered, so
    `LD_PRELOAD` / `PATH` / `BASH_ENV` walked around that constraint.

    ONE mode since issue #171. There was a second — an allowlist of the six make variables
    `Validate.execute` declares, applied when the call carried an `orchestration_id` — which
    existed because the caller might be a LEAF, and which no longer bounds anybody: no leaf
    reaches this server. So this denylist is now the whole rule, and it is deliberately
    INCOMPLETE (see `test_the_denylist_is_not_claimed_to_be_complete`): every program these
    tools run reads its own configuration from the environment, and the list catches a
    mistake rather than confining a caller."""

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

    def test_an_env_value_that_reaches_the_recipe_shell_is_refused(self) -> None:
        # make imports an environment name as a make VARIABLE, so a value arriving this
        # way is interpolated into the host-authored recipe exactly as a command-line
        # assignment would be. `extra_args` has had this rule all along; the env half
        # had no value check at all, so `CASES='; touch /tmp/x'` was accepted there and
        # refused one argument over.
        for value in ("a; touch /tmp/x", "a && id", "$(shell id)", "`id`", "a|b",
                      "a>b", "a\nb", "'x'"):
            with self.subTest(value=value):
                with self._spy_run_command() as run_command:
                    with self.assertRaises(ValueError) as ctx:
                        self.mod.tool_run_quality_checks(
                            self._args("run_quality_checks", {"CASES": value}))
                self.assertIn("reach the make recipe's shell", str(ctx.exception))
                run_command.assert_not_called()

    def test_the_conductor_env_payload_is_accepted(self) -> None:
        # The six make variables `Validate.execute` declares. If this payload ever
        # grows a value the rule refuses, it fails here rather than mid-phase.
        payload = {
            "OBJDIR": "/repo/workspace/tmp/a/build",
            "BINDIR": "/repo/workspace/binary/bin_1/bin",
            "RUNDIR": "/repo/workspace/tmp/a/run",
            "BIN": "sw2d_runner",
            "SPEC": "/repo/spec/x/spec.ir.yaml",
            "CASES": "c1 l0_v1.2-alpha",
        }
        with self._spy_run_command() as run_command:
            self.mod.tool_run_quality_checks(
                self._args("run_quality_checks", payload))
        run_command.assert_called_once()

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

    def test_the_denylist_is_not_claimed_to_be_complete(self) -> None:
        # Names that redirect execution just as effectively and are NOT refused: make
        # imports any environment name as a make variable, so `FC` replaces the compiler a
        # certified Makefile invokes. The orchestrated allowlist used to close this and was
        # retired with the gate (issue #171); pinned here so the rule that remains is not
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


class BuildArgvOverrideTests(unittest.TestCase):
    """`extra_args` is a list of make variable assignments, and nothing else.

    It replaced an ALLOWLIST that ran only under an orchestration — six variable names,
    plus a containment rule on the four whose value is a path, plus an outright refusal of
    `target`. That arm bounded a LEAF's grant, and since Z4 (issue #171) no leaf reaches
    this server at all, so PR-2 of that issue retired it. What is left applies to every
    call, which the old standalone arm did not: an element must ASSIGN (so make switches
    such as `--eval=$(shell ...)` are refused by construction rather than by name), and its
    value may not carry a character the make recipe's shell acts on.

    These two rules are kept for the SECOND defended class — a defect in a caller this
    repository writes (`AGENTS.md` §Development premises) — not against a leaf, and the
    docstring says which because the rule for deleting them later is different."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_server_module()

    def _check(self, extra_args: list[str]) -> None:
        self.mod._validate_build_argv_overrides(None, extra_args, "compile_project")

    def test_the_conductor_payload_is_accepted(self) -> None:
        # Exactly what `_build_inproc` passes. If that payload grows, this fails here
        # rather than the run failing mid-phase.
        self._check(["OBJDIR=/repo/workspace/tmp/a/build",
                     "BINDIR=/repo/workspace/binary/bin_1/bin",
                     "BIN=sw2d_runner"])

    def test_a_make_switch_is_refused_however_it_is_spelled(self) -> None:
        for arg in ("--eval=$(shell id)", "-f/tmp/Makefile", "--load-average=8",
                    "-j", "clean", "--warn-undefined-variables"):
            with self.subTest(arg=arg):
                with self.assertRaises(ValueError) as ctx:
                    self._check([arg])
                self.assertIn("make variable assignments", str(ctx.exception))

    def test_an_execution_redirecting_name_is_refused_as_an_assignment_too(self) -> None:
        """The NAME rule, which must not be weaker than the env twin's.

        A make COMMAND-LINE assignment overrides even a hard assignment in the Makefile, so
        this surface carries more authority than the environment — the canonical document says
        so. PR-2 deleted the argv-side name allowlist with the leaf's grant and its round-1
        correction made only the VALUE rule symmetric, so `SHELL=./evil` — the interpreter of
        every recipe line — was accepted here while `MAKESHELL` was refused one argument over.
        """
        for name in sorted(self.mod._UNSAFE_ASSIGNMENT_NAMES) + ["LD_PRELOAD", "DYLD_LIBRARY_PATH"]:
            with self.subTest(name=name):
                with self.assertRaises(ValueError) as ctx:
                    self._check([f"{name}=/tmp/x"])
                self.assertIn("redirect execution", str(ctx.exception))

    def test_the_assignment_name_rule_is_not_weaker_than_the_env_rule(self) -> None:
        # Derived, not restated: every name the env half refuses must also be refused as an
        # assignment. The reverse does not hold — SHELL and MAKE matter only on the argv side.
        for name in sorted(self.mod._UNSAFE_ENV_OVERRIDE_KEYS):
            with self.subTest(name=name):
                self.assertIn(name, self.mod._UNSAFE_ASSIGNMENT_NAMES)

    def test_a_non_make_build_system_may_pass_its_own_switches(self) -> None:
        """The assignment SHAPE rule belongs to make, and only to make.

        `_build_command` serves eleven build systems and the rule was applied to all of
        them, so `cargo build --release` and `mvn -DskipTests` were refused as "not a make
        variable assignment". Nothing runs through a shell here — this server never passes
        `shell=True` — so a switch is not dangerous for a build tool that does not read one
        as an extra makefile. `origin/main`'s standalone arm had no `extra_args` check at
        all, so this was a capability PR-2 narrowed without saying so."""
        for build_system, args in (
            ("cargo", ["--release"]),
            ("maven", ["-DskipTests"]),
            ("gradle", ["-Pflag=1"]),
            ("ninja", ["-v"]),
        ):
            with self.subTest(build_system=build_system):
                self.mod._validate_build_argv_overrides(
                    None, args, "compile_project", build_system=build_system)

    def test_the_other_two_rules_still_apply_to_every_build_system(self) -> None:
        # What does NOT depend on the build system: a metacharacter anywhere in the element,
        # and an assignment to a name that redirects what is executed.
        for build_system in ("make", "cargo", "maven", "gradle", "ninja", "cmake"):
            with self.subTest(build_system=build_system, rule="metacharacter"):
                with self.assertRaises(ValueError) as ctx:
                    self.mod._validate_build_argv_overrides(
                        None, ["X=a; id"], "compile_project", build_system=build_system)
                self.assertIn("carry a character a shell acts on", str(ctx.exception))
            with self.subTest(build_system=build_system, rule="redirect"):
                with self.assertRaises(ValueError) as ctx:
                    self.mod._validate_build_argv_overrides(
                        None, ["SHELL=/tmp/x"], "compile_project",
                        build_system=build_system)
                self.assertIn("redirect execution", str(ctx.exception))

    def test_the_handler_resolves_the_build_system_before_it_checks_the_argv(self) -> None:
        # The order is load-bearing: the rules differ by build system, so validating first
        # applies make's rule to every caller — which is the defect this row is about.
        project_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, project_dir, ignore_errors=True)
        with mock.patch.object(
            self.mod, "_run_command",
            return_value={"ok": True, "return_code": 0, "stdout": "", "stderr": ""},
        ) as run_command:
            self.mod.tool_compile_project({
                "project_dir": str(project_dir), "build_system": "cargo",
                "extra_args": ["--release"],
            })
        run_command.assert_called_once()

    def test_the_names_that_are_deliberately_still_accepted(self) -> None:
        # The recorded residue, pinned so "what is open" cannot drift into prose alone: a name
        # a Makefile merely READS is not refused, because bounding those means an allowlist and
        # an allowlist bounds a grant no caller holds any more.
        self._check(["FC=/usr/bin/gfortran", "CFLAGS=-O2", "OBJDIR=/repo/obj"])

    def test_a_value_that_reaches_the_recipe_shell_is_refused(self) -> None:
        # The host-authored Makefile interpolates an assignment unquoted into a recipe
        # line, so a metacharacter in a value is a command rather than a value.
        for value in ("a; touch /tmp/x", "a && id", "$(shell id)", "`id`", "a|b",
                      "a\nb", "a>b", "'x'"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as ctx:
                    self._check([f"CASES={value}"])
                self.assertIn("carry a character a shell acts on", str(ctx.exception))

    def test_every_assignment_is_checked_not_just_the_last(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self._check(["OBJDIR=/tmp/obj", "CASES=c1;id", "BIN=runner"])
        self.assertIn("CASES=c1;id", str(ctx.exception))

    def test_a_space_is_a_value_not_a_metacharacter(self) -> None:
        # `CASES` is a word LIST by contract, and a checkout path may hold a space, so
        # the space is deliberately outside the refused set.
        self._check(["CASES=case_a case_b"])

    def test_the_case_id_grammar_fits_the_value_rule(self) -> None:
        # Validate.execute joins the IR's case ids into CASES, so every id the Compile
        # gate accepts must survive this rule. Otherwise a run passes Compile and Build
        # and fails several phases later on an id no gate objected to.
        from tools.spec_input_gates import CASE_ID_TOKEN_RE
        for case_id in ("c1", "l0_v1.2-alpha", "A.b_c-d", "9x"):
            with self.subTest(case_id=case_id):
                self.assertTrue(CASE_ID_TOKEN_RE.match(case_id))
                self._check([f"CASES={case_id}"])

    def test_the_types_are_still_checked(self) -> None:
        with self.assertRaises(ValueError):
            self.mod._validate_build_argv_overrides(None, "OBJDIR=x", "compile_project")
        with self.assertRaises(ValueError):
            self.mod._validate_build_argv_overrides(7, [], "compile_project")

    def test_a_switch_spelled_as_the_target_is_refused_too(self) -> None:
        # `_build_command` places the target POSITIONALLY on the same line the
        # `extra_args` rule guards (`make -jN <target>`), so refusing a switch in one
        # half and accepting it in the other guards nothing. Until Z4 the orchestrated
        # arm refused every target and the standalone arm checked neither half.
        for target in ("--eval=$(shell touch /tmp/x)", "-f/tmp/Makefile",
                       "--load-average=8", "-j24", "all; id", "$(shell id)",
                       "a b", "-"):
            with self.subTest(target=target):
                with self.assertRaises(ValueError) as ctx:
                    self.mod._validate_build_argv_overrides(
                        target, [], "compile_project")
                self.assertIn("must be a build goal, not a switch", str(ctx.exception))

    def test_a_real_build_goal_is_still_accepted(self) -> None:
        # Across the build systems `_build_command` serves, not just make: a gradle task
        # path, an npm script name and a meson typed target all carry `:`, and a make
        # pattern goal carries `%`. A first version of this rule spelled an allowlist of
        # name characters and refused all four — an allowlist over a grammar this server
        # does not own answers a question it cannot know.
        for target in ("all", "clean", "sw2d_runner", "build/libcore.a", "lib.so.1",
                       "x86_64-target", "c++filt", ":app:assembleDebug", "build:prod",
                       "lib.so:shared_library", "%.o", "install-strip"):
            with self.subTest(target=target):
                self.assertEqual(
                    self.mod._validate_build_argv_overrides(
                        target, [], "compile_project"),
                    target,
                )


class BuildArgvOverrideWiringTests(unittest.TestCase):
    """The argv rules are REACHED by the handler, not merely defined beside it.

    `_validate_env_overrides` has such a witness (`test_denylisted_keys_are_refused_by_
    every_env_accepting_tool`); its argv twin had only direct-call tests, so deleting the
    single call site at `tool_compile_project` left the suite green."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_server_module()

    def setUp(self) -> None:
        self.project_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.project_dir, ignore_errors=True)

    def _args(self, **extra) -> dict:
        args: dict = {"project_dir": str(self.project_dir), "build_system": "make"}
        args.update(extra)
        return args

    def test_the_handler_refuses_a_switch_in_either_half_before_running_anything(
        self,
    ) -> None:
        cases = (
            ("target", {"target": "--eval=$(shell id)"}, "must be a build goal, not a switch"),
            ("extra_args", {"extra_args": ["--eval=$(shell id)"]},
             "make variable assignments"),
            ("value", {"extra_args": ["CASES=a; id"]},
             "carry a character a shell acts on"),
        )
        for label, payload, message in cases:
            with self.subTest(half=label):
                with mock.patch.object(
                    self.mod, "_run_command",
                    return_value={"ok": True, "return_code": 0,
                                  "stdout": "", "stderr": ""},
                ) as run_command:
                    with self.assertRaises(ValueError) as ctx:
                        self.mod.tool_compile_project(self._args(**payload))
                self.assertIn(message, str(ctx.exception))
                run_command.assert_not_called()

    def test_the_validated_target_is_the_string_that_runs(self) -> None:
        with mock.patch.object(
            self.mod, "_run_command",
            return_value={"ok": True, "return_code": 0, "stdout": "", "stderr": ""},
        ) as run_command:
            self.mod.tool_compile_project(self._args(target="  sw2d_runner  "))
        argv = run_command.call_args.kwargs["command"]
        self.assertIn("sw2d_runner", argv)
        self.assertNotIn("  sw2d_runner  ", argv)


class RetiredArgumentTests(unittest.TestCase):
    """`capability_token` is refused, not ignored.

    It named a secret in `capabilities/<agent_run_id>.json` that the orchestration gate
    compared against the launch record. A caller still sending one is written against a
    contract this server no longer implements, so serving the call would be serving it as
    if the check had passed."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_server_module()

    def setUp(self) -> None:
        self.project_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.project_dir, ignore_errors=True)

    def _args(self, tool: str) -> dict:
        args: dict = {"project_dir": str(self.project_dir),
                      "capability_token": "cap_secret"}
        if tool == "run_program":
            args["command"] = ["true"]
        if tool == "compile_project":
            args["build_system"] = "make"
        return args

    def test_every_tool_refuses_it(self) -> None:
        # `detect_build_system` included: it is the one tool the retirement made MORE
        # available (it used to be refused outright under the workflow), so a row that
        # skipped it would leave the only re-opened surface unchecked.
        for tool in ("compile_project", "run_program", "run_quality_checks",
                     "run_linter", "run_syntax_check", "detect_build_system"):
            with self.subTest(tool=tool):
                with mock.patch.object(self.mod, "_run_command") as run_command:
                    with self.assertRaises(ValueError) as ctx:
                        getattr(self.mod, f"tool_{tool}")(self._args(tool))
                self.assertIn("capability_token", str(ctx.exception))
                self.assertIn("#171", str(ctx.exception))
                run_command.assert_not_called()

    def test_the_attribution_arguments_are_not_refused(self) -> None:
        # The negative control: the two ids that REPLACED the token must be served.
        with mock.patch.object(
                self.mod, "_run_command",
                return_value={"ok": True, "return_code": 0}) as run_command:
            self.mod.tool_run_linter({
                "project_dir": str(self.project_dir), "preset": "ruff",
                "orchestration_id": "orch_x", "agent_run_id": "arid_x"})
        self.assertEqual(run_command.call_args.kwargs["attribution"],
                         {"orchestration_id": "orch_x", "agent_run_id": "arid_x"})


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


class _ReadBudgetExceeded(RuntimeError):
    """Raised by `_BoundedReads` when a reader will not stop reading an ended stream."""


class _BoundedReads:
    """A `BytesIO` that refuses to be read indefinitely.

    An exhausted `BytesIO` returns `b""` for ever, so a loop that does not treat that as the end
    never terminates. This makes the loop's own termination OBSERVABLE: exceeding the budget is a
    fast exception rather than a hung suite.
    """

    def __init__(self, data: bytes, budget: int = 200) -> None:
        self._buf = io.BytesIO(data)
        self._left = budget

    def _spend(self) -> None:
        if self._left <= 0:
            raise _ReadBudgetExceeded(
                "the reader did not stop at the end of the stream")
        self._left -= 1

    def readline(self) -> bytes:
        self._spend()
        return self._buf.readline()

    def read(self, size: int = -1) -> bytes:
        self._spend()
        return self._buf.read(size)


class RpcFramingTests(unittest.TestCase):
    """`_read_message` / `_write_message` / `main` — the stdio transport, driven end to end.

    Nothing read these three before this class (measured at 9f2e16d: `grep -c` over
    `tools/tests/*.py` finds 0 references to each of `_handle_request`, `_read_message` and
    `_write_message`), although every `compile` and `run` this repository performs traverses them.

    The tests below split into two kinds and the docstrings say which:

      * CONTRACT — a property the transport is required to have. Both framings are implemented, so
        both are contract: a client may send `Content-Length` headers (which `mcp_call.py` does) or
        one JSON object per line (which the MCP stdio transport does).
      * RECORD — what the code does today at a boundary nobody has decided about. A record test
        is not an argument that the behaviour is right. Where the current answer looks wrong to me
        I have said so in the docstring and left the behaviour alone; changing it is a decision for
        the operator, and the entry it would be recorded under is `TODO.md`.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_server_module()

    def _drive(self, stdin_bytes: bytes) -> tuple[int | None, list[dict], Exception | None]:
        """Run `main()` against `stdin_bytes`, returning (exit code, responses, escaped error).

        Drives the REAL entry point rather than poking `_read_message`: the framing, the dispatch
        and the loop's own termination are one behaviour, and two of the three recorded boundaries
        below are properties of `main`, not of the reader.

        The stdin is BOUNDED, and that is not a convenience. `main` is a `while True` whose only
        exit is the `return 0` it takes when the reader answers None; a change that made it
        `continue` there spins forever on a stream that has ended. Measured the hard way — that
        exact mutant hung this file's own sweep until an outer `timeout` killed it, and the kill
        skipped the `finally` that restores the mutated source, so the next run would have
        measured a mutated baseline. An exhausted `BytesIO` returns `b""` for ever, so the bound
        has to come from the fixture: after `_READ_BUDGET` reads the stream raises, `main` cannot
        catch it (nothing there catches anything), and the spin becomes a fast, legible failure.
        """
        out = io.StringIO()
        stream = _BoundedReads(stdin_bytes)
        with mock.patch.object(sys, "stdin", mock.Mock(buffer=stream)), \
                mock.patch.object(sys, "stdout", out):
            try:
                code: int | None = self.mod.main()
                escaped: Exception | None = None
            except Exception as exc:  # noqa: BLE001 - the escape is the subject of three rows
                code, escaped = None, exc
        responses = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
        return code, responses, escaped

    def test_main_returns_at_end_of_input_rather_than_spinning(self) -> None:
        """CONTRACT, and the one the bound above exists for.

        `main`'s only exit is the `return 0` it takes when `_read_message` answers None. Nothing
        else in the loop can end it, so this is the whole of the server's termination behaviour,
        and it was observed by nothing. The witness is the read budget: a `main` that kept looping
        on an ended stream would exhaust it and surface as `_ReadBudgetExceeded` instead of
        hanging the suite.
        """
        code, responses, escaped = self._drive(
            self._line({"jsonrpc": "2.0", "id": 1, "method": "ping"}))
        self.assertIsNone(escaped)
        self.assertEqual(code, 0)
        self.assertEqual(len(responses), 1)

    def test_the_read_budget_itself_fires(self) -> None:
        """The self-test for the bound. Without it, a budget set too high (or a fixture that
        silently stopped counting) would turn the row above into a test that can only pass — the
        spin it is about would come back as a hang, which reads as an unrelated infrastructure
        problem rather than as this defect."""
        stream = _BoundedReads(b"", budget=3)
        for _ in range(3):
            self.assertEqual(stream.readline(), b"")
        with self.assertRaises(_ReadBudgetExceeded):
            stream.readline()

    @staticmethod
    def _framed(payload: dict) -> bytes:
        body = json.dumps(payload).encode("utf-8")
        return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body

    @staticmethod
    def _line(payload: dict) -> bytes:
        return json.dumps(payload).encode("utf-8") + b"\n"

    # ---- CONTRACT ----------------------------------------------------------------

    def test_both_framings_are_read(self) -> None:
        """CONTRACT. Both are implemented, so both are the contract — and they have separate
        callers: `mcp_servers/mcp_call.py` writes `Content-Length` headers, while the MCP stdio
        transport is newline-delimited JSON. A change that kept only one would break a real
        client, and nothing observed either."""
        for label, encode in (("content-length", self._framed), ("newline", self._line)):
            with self.subTest(framing=label):
                code, responses, escaped = self._drive(encode({"jsonrpc": "2.0", "id": 1,
                                                               "method": "ping"}))
                self.assertIsNone(escaped)
                self.assertEqual(code, 0)
                self.assertEqual([r["id"] for r in responses], [1])
                self.assertEqual(responses[0]["result"], {})

    def test_a_header_block_may_carry_more_than_content_length(self) -> None:
        """CONTRACT. The reader skips to the blank line, so a client sending `Content-Type` (which
        the MCP specification permits) is served rather than mis-framed."""
        body = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"}).encode("utf-8")
        stdin = (f"Content-Length: {len(body)}\r\n".encode("ascii")
                 + b"Content-Type: application/vscode-jsonrpc; charset=utf-8\r\n\r\n" + body)
        code, responses, escaped = self._drive(stdin)
        self.assertIsNone(escaped)
        self.assertEqual(code, 0)
        self.assertEqual([r["id"] for r in responses], [7])

    def test_blank_lines_between_messages_are_skipped(self) -> None:
        """CONTRACT. A writer that terminates a framed body with a newline leaves one behind, and
        the loop must not read it as a message."""
        code, responses, escaped = self._drive(
            b"\r\n" + self._framed({"jsonrpc": "2.0", "id": 1, "method": "ping"})
            + b"\n\n" + self._line({"jsonrpc": "2.0", "id": 2, "method": "ping"}))
        self.assertIsNone(escaped)
        self.assertEqual(code, 0)
        self.assertEqual([r["id"] for r in responses], [1, 2])

    def test_a_notification_produces_no_response_and_does_not_end_the_loop(self) -> None:
        """CONTRACT. `_handle_request` returns None for a notification and `main` must write
        nothing for it — a JSON-RPC notification has no reply — while still serving what follows.
        Writing `null` would be a protocol violation a client sees; stopping would be worse."""
        code, responses, escaped = self._drive(
            self._line({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
            + self._line({"jsonrpc": "2.0", "id": 5, "method": "ping"}))
        self.assertIsNone(escaped)
        self.assertEqual(code, 0)
        self.assertEqual([r["id"] for r in responses], [5])

    def test_write_message_emits_one_line_of_json_without_escaping_non_ascii(self) -> None:
        """CONTRACT. One line per message is the framing; `ensure_ascii=False` is what keeps a
        diagnostic containing non-ASCII readable instead of `\\uXXXX`-escaped. A compiler on a
        non-English locale, and any path with a non-ASCII component, reach this."""
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            self.mod._write_message({"id": 1, "result": {"text": "コンパイル失敗 — naïve"}})
        raw = out.getvalue()
        self.assertTrue(raw.endswith("\n"))
        self.assertEqual(raw.count("\n"), 1, "a message must be exactly one line")
        self.assertIn("コンパイル失敗 — naïve", raw)
        self.assertNotIn("\\u", raw)
        self.assertEqual(json.loads(raw)["result"]["text"], "コンパイル失敗 — naïve")

    # ---- RECORD ------------------------------------------------------------------

    def test_record_eof_while_reading_a_header_ends_the_loop_with_zero(self) -> None:
        """RECORD, and this one is also the contract: a closed stdin is how a client disconnects,
        and exiting 0 is what stops the server being reported as crashed."""
        for label, stdin in (("empty", b""), ("mid-header", b"Content-Length: 41\r\n")):
            with self.subTest(case=label):
                code, responses, escaped = self._drive(stdin)
                self.assertIsNone(escaped)
                self.assertEqual(code, 0)
                self.assertEqual(responses, [])

    def test_record_an_empty_body_after_a_header_ends_the_loop_with_zero(self) -> None:
        """RECORD. `Content-Length: N` followed by nothing reads as EOF, not as a framing error.

        A truncated stream and a clean disconnect become the same event, so a client killed
        mid-write is indistinguishable from one that closed politely. Recorded, not defended: I
        have not changed it, and whether a partial frame should be reported is the operator's
        decision.
        """
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode("utf-8")
        code, responses, escaped = self._drive(
            f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
        self.assertIsNone(escaped)
        self.assertEqual(code, 0)
        self.assertEqual(responses, [])

    def test_record_a_partially_truncated_body_escapes_as_a_json_error(self) -> None:
        """RECORD. A body that is short but not empty reaches `json.loads` and its
        `JSONDecodeError` is not caught by `main`, so the process dies with a traceback.

        The contrast with the row above is the point: zero bytes is a clean exit, one byte is an
        uncaught exception. Recorded rather than fixed.
        """
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode("utf-8")
        code, responses, escaped = self._drive(
            f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body[:5])
        self.assertIsNone(code)
        self.assertIsInstance(escaped, json.JSONDecodeError)
        self.assertEqual(responses, [])

    def test_record_a_non_numeric_content_length_escapes_as_a_value_error(self) -> None:
        """RECORD. `int(...)` on the header value is unguarded. Same class as the row above."""
        code, responses, escaped = self._drive(b"Content-Length: abc\r\n\r\n{}")
        self.assertIsNone(code)
        self.assertIsInstance(escaped, ValueError)
        self.assertNotIsInstance(escaped, json.JSONDecodeError)
        self.assertEqual(responses, [], "nothing may be answered before the escape")

    def test_record_a_json_value_that_is_not_an_object_is_skipped_silently(self) -> None:
        """RECORD. `main` drops a non-dict message and reads the next one, with no reply and no
        diagnostic. A client that sends a JSON-RPC batch (an ARRAY, which the specification
        allows) therefore gets silence rather than an error — and silence is the answer a caller
        cannot distinguish from a slow server. Recorded; not changed here.
        """
        code, responses, escaped = self._drive(
            self._line([{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
            + self._line({"jsonrpc": "2.0", "id": 2, "method": "ping"}))
        self.assertIsNone(escaped)
        self.assertEqual(code, 0)
        self.assertEqual([r["id"] for r in responses], [2])


class RequestDispatchTests(unittest.TestCase):
    """`_handle_request` — and the one thing it does that decides whether a gate MEANS anything.

    `tools/call` catches every exception a handler raises and returns it as a SUCCESSFUL JSON-RPC
    response carrying `isError: True`. That is the only channel by which a capability-gate refusal
    reaches a client: the gate raises `ValueError`, and these twenty lines decide whether the
    client is told "refused" or "fine". A change here that dropped `isError` would make every
    refusal in this server look like a pass, with no other test in the repository noticing.

    Written under `.claude/skills/atmofab-enforcement-change`: this is the exit of the enforcement
    machinery, not an ordinary handler.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_server_module()

    def test_initialize_echoes_the_client_protocol_version(self) -> None:
        response = self.mod._handle_request({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2999-01-01", "capabilities": {}}})
        self.assertEqual(response["result"]["protocolVersion"], "2999-01-01")
        self.assertEqual(response["result"]["serverInfo"]["name"], self.mod.SERVER_NAME)
        self.assertEqual(response["result"]["serverInfo"]["version"], self.mod.SERVER_VERSION)

    def test_initialize_without_a_protocol_version_answers_the_default(self) -> None:
        """The default is a CONSTANT of the module, read here rather than transcribed — a
        transcribed value would turn a deliberate protocol bump into a test failure that says
        nothing about what broke."""
        response = self.mod._handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(response["result"]["protocolVersion"],
                         self.mod.DEFAULT_PROTOCOL_VERSION)

    def test_tools_list_serves_every_registered_tool(self) -> None:
        """Set identity against `TOOLS`, not a transcribed list: a tool added to the module and
        not served is a tool no client can call, and a name written here would have to be edited
        for every legitimate addition."""
        response = self.mod._handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        served = {tool["name"] for tool in response["result"]["tools"]}
        self.assertEqual(served, set(self.mod.TOOLS))
        for tool in response["result"]["tools"]:
            self.assertIn("inputSchema", tool)
            self.assertTrue(tool.get("description"))

    def test_a_notification_gets_no_response(self) -> None:
        self.assertIsNone(self.mod._handle_request(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_an_unknown_method_with_an_id_is_a_method_not_found_error(self) -> None:
        response = self.mod._handle_request(
            {"jsonrpc": "2.0", "id": 3, "method": "no/such/method"})
        self.assertEqual(response["error"]["code"], -32601)
        self.assertNotIn("result", response)

    def test_an_unknown_method_without_an_id_gets_no_response(self) -> None:
        """A message with no id is a notification whatever its method, and answering one is a
        protocol violation. The branch is separate from the one above and is reached only by an
        unknown method, so it needs its own probe."""
        self.assertIsNone(self.mod._handle_request({"jsonrpc": "2.0", "method": "no/such/method"}))

    def test_an_unknown_TOOL_is_a_jsonrpc_error_not_an_isError_result(self) -> None:
        """The two error channels are different, and a client distinguishes them. An unknown tool
        is a JSON-RPC `error` (-32602); a handler that RAN and failed is a successful response
        carrying `isError`. Collapsing them would make "this server cannot do that" and "that
        call was refused" the same answer."""
        response = self.mod._handle_request({
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "no_such_tool", "arguments": {}}})
        self.assertEqual(response["error"]["code"], -32602)
        self.assertIn("no_such_tool", response["error"]["message"])
        self.assertNotIn("result", response)

    def test_a_successful_call_carries_isError_false_and_the_handler_return_value(self) -> None:
        """Driven through a REAL registered tool with a stub handler, so the wrapping is what is
        observed rather than a reimplementation of it."""
        marker = {"ok": True, "value": "コンパイル済み"}
        with mock.patch.dict(self.mod.TOOLS, {}, clear=False):
            tool = self.mod.TOOLS["detect_build_system"]
            patched = type(tool)(name=tool.name, description=tool.description,
                                 input_schema=tool.input_schema, handler=lambda args: marker)
            self.mod.TOOLS["detect_build_system"] = patched
            response = self.mod._handle_request({
                "jsonrpc": "2.0", "id": 5, "method": "tools/call",
                "params": {"name": "detect_build_system", "arguments": {}}})
        self.assertIs(response["result"]["isError"], False)
        self.assertEqual(response["result"]["structuredContent"], marker)
        self.assertEqual(json.loads(response["result"]["content"][0]["text"]), marker)

    def test_a_handler_exception_becomes_isError_true_with_its_message(self) -> None:
        """The row this class exists for, in its general form."""
        def explode(_args):
            raise ValueError("the gate said no")

        with mock.patch.dict(self.mod.TOOLS, {}, clear=False):
            tool = self.mod.TOOLS["detect_build_system"]
            self.mod.TOOLS["detect_build_system"] = type(tool)(
                name=tool.name, description=tool.description,
                input_schema=tool.input_schema, handler=explode)
            response = self.mod._handle_request({
                "jsonrpc": "2.0", "id": 6, "method": "tools/call",
                "params": {"name": "detect_build_system", "arguments": {}}})
        self.assertIs(response["result"]["isError"], True)
        self.assertEqual(response["result"]["structuredContent"]["error"], "the gate said no")
        self.assertIn("the gate said no", response["result"]["content"][0]["text"])

    def test_a_REAL_argument_refusal_reaches_the_client_as_isError(self) -> None:
        """The same row driven by a real refusal rather than a stub, because a stub cannot show
        that the refusal actually TRAVELS this path.

        The refusal used to be the capability gate's (a workflow-mode server called without
        `orchestration_id`); that gate was retired in issue #171, and the argument refusal that
        replaced it — `capability_token`, which this server no longer implements — is raised from
        the same place in the same way. What this asserts is unchanged: the client can tell a
        refusal from a pass. `isError` is True, the reason is carried, and the response is NOT a
        successful result with data in it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            response = self.mod._handle_request({
                "jsonrpc": "2.0", "id": 7, "method": "tools/call",
                "params": {"name": "run_syntax_check",
                           "arguments": {"project_dir": tmp,
                                         "capability_token": "cap_secret"}}})
        result = response["result"]
        self.assertIs(result["isError"], True, "an argument refusal must not look like a pass")
        detail = result["structuredContent"]["error"]
        self.assertIn("capability_token", detail)
        self.assertNotIn("ok", result["structuredContent"],
                         "a refusal must not carry a handler's success keys")

    def test_the_refusal_and_the_success_differ_in_the_field_a_client_reads(self) -> None:
        """The negative control for the row above. Two calls, one refused and one served, compared
        on `isError` — because an assertion that a refusal has `isError: True` says nothing unless
        a success has `isError: False` on the same path. Mutating the constant makes both rows
        red, which is the property; without this one, `isError: True -> True` everywhere would
        pass."""
        with tempfile.TemporaryDirectory() as tmp:
            refused = self.mod._handle_request({
                "jsonrpc": "2.0", "id": 8, "method": "tools/call",
                "params": {"name": "run_syntax_check",
                           "arguments": {"project_dir": tmp,
                                         "capability_token": "cap_secret"}}})
            served = self.mod._handle_request({
                "jsonrpc": "2.0", "id": 9, "method": "tools/call",
                "params": {"name": "detect_build_system", "arguments": {"project_dir": tmp}}})
        self.assertIs(refused["result"]["isError"], True)
        self.assertIs(served["result"]["isError"], False)


class BuildCommandTests(unittest.TestCase):
    """`_recommended_build_system` and `_build_command` — the SUCCESS paths.

    The refusal side of this server is covered thickly; these two were referenced once each from
    the whole test corpus (measured at 9f2e16d). An earlier version of this sentence added "and
    never on a path that produces an argv", which round 1 falsified:
    `test_host_prerequisites.py:102` does take `_build_command(build_system, None, 1, [])[0]`.
    What is true is narrower and is what these rows are for — each of those two references
    observes ONE thing (an executable name; one marker detection succeeding), so the argv SHAPES,
    the marker table as an enumeration, the two defaults and the knot between the two functions
    were unheld. Every `compile` this repository performs is one of these argv.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_server_module()

    def _dir_with(self, *names: str) -> str:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        for name in names:
            (Path(holder.name) / name).write_text("", encoding="utf-8")
        return holder.name

    @classmethod
    def _accepted_build_systems(cls) -> set[str]:
        """Every build system `_build_command` ACCEPTS, found by calling it.

        THE QUESTION CHANGED HERE, and the reason is three rounds of the same failure. Asking
        "what does the dispatch look like" needs a reader, and every reader was defeated by the
        next spelling: a regex missed an arm reformatted across lines and one written with a
        double space; an `ast` walk over `==` and `in <literal>` missed `bs = build_system` bound
        to an alias, and missed `if build_system in _EXTRA:` against a module-level dict — which
        is THIS MODULE'S OWN IDIOM (`_LINT_PRESET_COMMANDS` is exactly that). Each fix produced
        the next spelling, which `.claude/skills/atmofab-review-loop` names as the sign that the
        pin is in the wrong place rather than the wrong shape.

        So the question is now one a LOOKUP can answer: `_build_command` raises `ValueError` for
        anything it does not implement, so "is this build system implemented" is a call, not a
        parse. The candidate set is every string constant in the module (an adapter's name has to
        be written down somewhere for the dispatch to match it, wherever that is — inside the
        function, in a module-level table, or as a dict key) plus the marker table's own values.
        A name that is accepted and has no `_BASE_ARGV` row is the defect this is for.

        What this does NOT reach, said rather than left implied: a build system whose name is
        never a literal anywhere in the module — computed, imported, or read from a file. There is
        no such thing today and it would be a different design.
        """
        import ast
        import inspect
        module_source = Path(inspect.getsourcefile(cls.mod)).read_text(encoding="utf-8")
        candidates = {
            node.value for node in ast.walk(ast.parse(module_source))
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value}
        candidates.update(b for _m, b in cls._marker_table())
        def implemented(candidate: str) -> bool:
            """A refusal (`ValueError`) is the answer this probe asks for; anything else is this
            reader's problem rather than an acceptance. Either way the candidate is not an
            implemented build system."""
            try:
                cls.mod._build_command(candidate, None, 1, [])
            except Exception:  # noqa: BLE001
                return False
            return True

        return {candidate for candidate in candidates if implemented(candidate)}

    @classmethod
    def _marker_table(cls) -> list[tuple[str, str]]:
        """The `(marker, build system)` table `_recommended_build_system` walks, read with `ast`.

        A REGEX over the source found the entries only while they were formatted one per line.
        Measured in round 1: splitting a single entry across two lines — `("CMakeLists.txt",\n
        "cmake"),`, which any reformatting produces — made `cmake` vanish from both sweeps that
        read this table, with the whole class green and the subtest count silently dropping. A
        sweep that shrinks without saying so is worse than no sweep, because the count is what a
        reader takes as evidence.

        `ast` reads the literal the interpreter reads, so formatting cannot move it. The exact
        count is asserted by the callers against `_MARKER_COUNT` below rather than by a floor: the
        first version's floor was `>= 10` for an eleven-entry table, so losing exactly one entry —
        the failure that actually happened — was invisible.
        """
        import inspect
        return cls._read_marker_table(inspect.getsource(cls.mod._recommended_build_system))

    @staticmethod
    def _read_marker_table(source: str) -> list[tuple[str, str]]:
        """The pure half, so the probe below can drive THIS function on a synthetic input."""
        import ast
        for node in ast.walk(ast.parse(textwrap.dedent(source))):
            if not isinstance(node, ast.Assign):
                continue
            if not any(getattr(target, "id", None) == "checks" for target in node.targets):
                continue
            table = node.value
            if not isinstance(table, ast.List):
                # AssertionError, not TypeError: this is a check on the SHAPE of the code being
                # read, reported to whoever changed it, not a type contract of this function.
                raise AssertionError(  # noqa: TRY004
                    "the marker table is no longer a list literal")
            return [(entry.elts[0].value, entry.elts[1].value) for entry in table.elts]
        raise AssertionError(
            "`checks` is no longer assigned a list literal in _recommended_build_system; this "
            "reader cannot find the marker table it sweeps")

    #: How many markers the table must have, so a sweep that reads fewer is a failure rather than
    #: a quieter run. Derived once here and checked by every row that walks the table.
    _MARKER_COUNT = 11

    def test_the_marker_table_reader_finds_every_entry(self) -> None:
        """The self-test for the reader, and the whole reason it is `ast` and not a regex.

        Both directions: the count is exact, and the reader survives a reformatting that the
        regex did not. The second half is driven on a synthetic function rather than by editing
        the real one.
        """
        self.assertEqual(len(self._marker_table()), self._MARKER_COUNT)
        reformatted = textwrap.dedent('''
            def f(project_dir, language):
                checks = [
                    ("Makefile", "make"),
                    ("CMakeLists.txt",
                     "cmake"),  # split across two lines, with a comment
                ]
            ''')
        # DRIVES the real reader. The first version of this row re-implemented the `ast` walk
        # inline, so replacing `_marker_table`'s body with the old regex left the whole file
        # green — measured in round 2, and it is the same sin this branch had already fixed in
        # the sibling boundary check one PR earlier.
        self.assertEqual(
            self._read_marker_table(reformatted),
            [("Makefile", "make"), ("CMakeLists.txt", "cmake")])
        self.assertEqual(
            len(re.findall(r'\("([^"]+)", "([^"]+)"\),', reformatted)), 1,
            "the regex this reader replaced would still find only one of the two entries; if "
            "that stops being true the reason for using ast has changed")
        accepted = self._accepted_build_systems()
        self.assertEqual(accepted, set(self._BASE_ARGV))
        self.assertNotIn(
            "definitely-not-a-build-system", accepted,
            "the acceptance probe calls something that is not a build system a build system")

    #: Which marker file means which build system. THE THIRD self-comparison on this branch, and
    #: the one two rounds of fixes did not reach: every row that walked the marker table took its
    #: expectation FROM that table, so `("CMakeLists.txt", "cmake")` -> `("CMakeLists.txt",
    #: "make")` was green everywhere, and a `cmake` project would have been built with `make` with
    #: nothing red. Measured at HEAD and at the branch's first commit alike, so this is
    #: pre-existing rather than introduced — the fix belongs here because this is the PR that
    #: claims to cover `_recommended_build_system`.
    #:
    #: Written out, because the association IS the knowledge and there is nowhere else to derive
    #: it from. Adding a marker means adding a row, which is the point.
    _MARKER_MEANS: typing.ClassVar[dict[str, str]] = {
        "Makefile": "make",
        "makefile": "make",
        "CMakeLists.txt": "cmake",
        "meson.build": "meson",
        "build.ninja": "ninja",
        "Cargo.toml": "cargo",
        "go.mod": "go",
        "pom.xml": "maven",
        "build.gradle": "gradle",
        "package.json": "npm",
        "pyproject.toml": "poetry",
    }

    def test_each_marker_means_the_build_system_this_repository_expects(self) -> None:
        """Set identity against an INDEPENDENT statement of the mapping.

        Not against `_marker_table()`, which is the table under test — comparing it with itself is
        what let the association drift unobserved. Both directions, so a marker added to the
        function without a decision here is a failure, and a row here for a marker the function
        dropped is too.
        """
        self.assertEqual(
            dict(self._marker_table()), self._MARKER_MEANS,
            "the marker table and the mapping this repository expects disagree; a project would "
            "be built with a different build system than its marker names")

    def test_a_marker_file_selects_its_build_system_and_the_reason_names_it(self) -> None:
        """One case per marker, driven through the real function — the enumeration is killed
        element by element rather than as a set, because a missing element shows up in no other
        test. The expectation comes from `_MARKER_MEANS`, not from the table being swept."""
        markers = [(marker, self._MARKER_MEANS[marker]) for marker, _ in self._marker_table()]
        self.assertEqual(len(markers), self._MARKER_COUNT)
        for marker, expected in markers:
            with self.subTest(marker=marker):
                answer = self.mod._recommended_build_system(self._dir_with(marker), "fortran")
                self.assertEqual(answer["build_system"], expected)
                self.assertIn(marker, answer["reason"])

    def test_the_first_matching_marker_wins(self) -> None:
        """A directory carrying two markers has one answer, and which one is a property of the
        table's ORDER. Recorded because a reorder is silent otherwise."""
        answer = self.mod._recommended_build_system(
            self._dir_with("Makefile", "CMakeLists.txt"), "fortran")
        self.assertEqual(answer["build_system"], "make")

    def test_an_unmarked_directory_defaults_to_make_and_says_which_default(self) -> None:
        """Two different defaults with two different reasons, and the reason is the only thing
        that tells them apart — an assertion on `build_system` alone cannot, since both are
        `make`."""
        empty = self._dir_with()
        family = self.mod._recommended_build_system(empty, "fortran")
        self.assertEqual(family["build_system"], "make")
        self.assertIn("Fortran/C family", family["reason"])
        other = self.mod._recommended_build_system(empty, "haskell")
        self.assertEqual(other["build_system"], "make")
        self.assertEqual(other["reason"], "fallback default")

    #: Build systems whose EXECUTABLE is not their id. The one piece of knowledge this row holds,
    #: and it is held here because `_build_command` is the only other place it exists — comparing
    #: argv[0] against `build_system_executable` instead proves nothing, since that function IS
    #: `_build_command(bs, None, 1, [])[0]`. Round 1 measured the consequence: renaming `mvn`,
    #: `cargo`, `poetry` and `go build`'s argv all survived, because the assertion compared the
    #: implementation with itself.
    _EXECUTABLE_RENAMES: typing.ClassVar[dict[str, str]] = {"maven": "mvn"}

    def test_every_build_system_the_recommender_can_return_builds_an_argv(self) -> None:
        """The knot between the two functions, which nothing tied: a marker table entry naming a
        build system `_build_command` does not implement is a `compile` that raises after the
        recommendation succeeded. The argv[0] check is against the EXPECTED executable, so a
        renamed program is caught rather than compared with itself."""
        markers = self._marker_table()
        self.assertEqual(len(markers), self._MARKER_COUNT,
                         "this sweep is reading fewer entries than the table has")
        for _marker, build_system in markers:
            with self.subTest(build_system=build_system):
                argv = self.mod._build_command(build_system, None, 4, [])
                self.assertTrue(argv, f"{build_system} produced an empty argv")
                self.assertEqual(
                    argv[0], self._EXECUTABLE_RENAMES.get(build_system, build_system),
                    "argv[0] is neither the build system's own name nor its recorded rename; if "
                    "this is a deliberate rename, add it to _EXECUTABLE_RENAMES")
                self.assertEqual(argv[0], self.mod.build_system_executable(build_system),
                                 "the host probe would look for a different program than a build "
                                 "actually runs")

    #: The full argv each build system produces with no target and no extra arguments. argv[0]
    #: alone is not the contract: round 1 measured `go build` -> `go test` surviving, because the
    #: program is `go` either way and the SUBCOMMAND is where the meaning is. Adding a build
    #: system means adding a row here, which is the point — someone has to decide what it runs.
    _BASE_ARGV: typing.ClassVar[dict[str, list[str]]] = {
        "make": ["make", "-j4"],
        "cmake": ["cmake", "--build", ".", "-j", "4"],
        "meson": ["meson", "compile", "-j", "4"],
        "ninja": ["ninja", "-j4"],
        "cargo": ["cargo", "build"],
        "go": ["go", "build"],
        "maven": ["mvn", "package"],
        "gradle": ["gradle", "build"],
        "npm": ["npm", "run", "build"],
        "pnpm": ["pnpm", "run", "build"],
        "poetry": ["poetry", "build"],
    }

    def test_every_build_system_runs_the_argv_this_repository_expects(self) -> None:
        """The whole argv, not just its first word.

        Set identity against the dispatch, in both directions: every build system `_build_command`
        implements has a row here, and every row is a build system it implements. The first
        version compared `argv[0]` with `build_system_executable`, which IS
        `_build_command(bs, None, 1, [])[0]` — the implementation compared with itself, and four
        renames survived it.
        """
        implemented = set(self._BASE_ARGV)
        for build_system, expected in sorted(self._BASE_ARGV.items()):
            with self.subTest(build_system=build_system):
                self.assertEqual(self.mod._build_command(build_system, None, 4, []), expected)
        # The other direction: a build system the dispatch gained and this table did not.
        self.assertEqual(
            self._accepted_build_systems(), implemented,
            "the build systems _build_command ACCEPTS and the argv this table records are "
            "different sets; a new adapter needs a row saying what it runs")

    def test_the_make_argv_is_the_documented_shape(self) -> None:
        """`make` is the default this repository actually runs, so its argv is pinned exactly —
        the jobs flag glued to `-j`, the target after it, extra arguments last."""
        self.assertEqual(self.mod._build_command("make", None, 4, []), ["make", "-j4"])
        self.assertEqual(self.mod._build_command("make", "all", 2, []), ["make", "-j2", "all"])
        self.assertEqual(self.mod._build_command("make", "all", 2, ["V=1"]),
                         ["make", "-j2", "all", "V=1"])

    def test_extra_arguments_reach_every_build_system_and_stay_last(self) -> None:
        """A family over the whole dispatch, and the property is one an operator relies on:
        arguments they passed are handed to the tool, unreordered. `cmake` is the one that puts
        them behind a `--` separator, so it is asserted separately rather than excluded."""
        for build_system in ("make", "meson", "ninja", "cargo", "go", "maven", "gradle",
                             "npm", "pnpm", "poetry"):
            with self.subTest(build_system=build_system):
                argv = self.mod._build_command(build_system, None, 1, ["--flag", "x"])
                self.assertEqual(argv[-2:], ["--flag", "x"])
        cmake = self.mod._build_command("cmake", "tgt", 3, ["--flag"])
        self.assertEqual(cmake, ["cmake", "--build", ".", "-j", "3", "--target", "tgt",
                                 "--", "--flag"])

    def test_a_build_system_with_no_adapter_is_refused_by_name(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self.mod._build_command("scons", None, 1, [])
        self.assertIn("scons", str(caught.exception))

    def test_the_default_target_is_the_tools_own_and_not_a_missing_argument(self) -> None:
        """`gradle` and `npm` substitute `build` for an absent target rather than omitting it, so
        the argv is well-formed either way. Recorded because the two shapes are indistinguishable
        from the caller's side."""
        self.assertEqual(self.mod._build_command("gradle", None, 1, []), ["gradle", "build"])
        self.assertEqual(self.mod._build_command("npm", None, 1, []), ["npm", "run", "build"])


class McpCallClientTests(unittest.TestCase):
    """`mcp_servers/mcp_call.py` — the client this repository's own procedures drive.

    Referenced by `docs/RUNBOOK.md`, by `.claude/skills/atmofab-enforcement-change`'s verification
    reference, by the review-loop skill and by `TODO.md` (on three lines at 9f2e16d, four
    occurrences at HEAD — this branch's own edit added one; two earlier versions of this sentence
    said "twice" and "three times", and a bare count here is ambiguous between lines and
    occurrences, which is why it now says which), all of which use it as the vehicle for END-TO-END verification of the
    capability gate — and it had no test at all (measured at
    9f2e16d: zero references in `tools/tests/`). The skills treat it as the instrument that proves
    a refusal is real, so an instrument that silently stopped reporting refusals would take the
    evidence with it.

    Driven as a real SUBPROCESS, not by importing `_mcp_call`, because the exit code is half the
    contract — the skills read it.

    NOT PINNED, measured rather than assumed: deleting `_mcp_call`'s `finally: proc.kill();
    proc.wait()` survives every row here. What that costs is a leaked server process per call, not
    a wrong answer — the client still returns the right verdict — and observing it from a
    subprocess test means listing processes, which is racy. Said here rather than left for a
    reader to count as covered.

    This paragraph used to say the module spawns the server by a RELATIVE path and "only works
    with the repository root as the working directory", asserted below. Round 2 caught that: the
    commit that removed that behaviour left the sentence describing it, in the very change written
    to stop this instrument from lying, and the row 70 lines down now asserts the OPPOSITE. The
    client resolves the server from its own `__file__`; `_call` still passes `cwd=REPO_ROOT`
    because that is what the documented invocations use, not because it is required.
    """

    REPO_ROOT = _SERVER_PATH.parent.parent

    def _call(self, tool: str, args: dict, *, workflow: bool) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if not k.startswith("ATMOFAB_")}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        if workflow:
            env["ATMOFAB_WORKFLOW_MODE"] = "1"
        return subprocess.run(
            [sys.executable, "mcp_servers/mcp_call.py", "--tool", tool,
             "--args-json", json.dumps(args)],
            cwd=self.REPO_ROOT, env=env, capture_output=True, text=True,
            # Bounded well under the time a served call takes (~1s measured), because the client
            # BLOCKS rather than fails when the server's framing breaks: `_read_message` waits on
            # `readline()`, and a `_write_message` that stops emitting a newline leaves it waiting
            # for a line that never ends. Measured — that mutation turned this class into a hang
            # long enough to time the whole mutation sweep out. A generous timeout here buys
            # nothing and hides that.
            timeout=30,
            check=False)  # the return code IS the subject of these rows

    def test_an_argument_refusal_comes_back_as_a_nonzero_exit_and_a_message(self) -> None:
        """The end-to-end the skills actually run, and the property they rely on.

        A call carrying the retired `capability_token` is refused by
        `_refuse_retired_arguments`. That refusal travels as a ValueError -> an `isError`
        result -> a `RuntimeError` in the client -> a non-zero exit. Every link is somebody's
        twenty lines, and until this row nothing drove the chain. (The refusal it used to drive
        was the capability gate's, retired in issue #171; the chain under test is the same.)
        """
        with tempfile.TemporaryDirectory() as tmp:
            done = self._call("run_syntax_check",
                              {"project_dir": tmp, "capability_token": "cap_secret"},
                              workflow=False)
        self.assertNotEqual(done.returncode, 0,
                            "a refused call exited 0; a script reading the exit code would "
                            "record the refusal as satisfied")
        self.assertIn("capability_token", done.stderr)
        self.assertEqual(done.stdout.strip(), "",
                         "a refused call printed a result document on stdout")

    def test_a_served_call_comes_back_as_exit_zero_and_json_on_stdout(self) -> None:
        """The negative control. Without it the row above is satisfied by a client that fails on
        every call — which is exactly what a broken client looks like, and is indistinguishable
        from a working gate."""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "Makefile").write_text("all:\n\t@true\n", encoding="utf-8")
            done = self._call("detect_build_system",
                              {"project_dir": tmp, "language": "fortran"}, workflow=False)
            self.assertEqual(done.returncode, 0, done.stderr)
            payload = json.loads(done.stdout)
            # DERIVED, not transcribed. The first version of this row asserted a key
            # (`build_system`) the tool does not emit — it emits `recommended_build_system`, and
            # the difference is invisible until you run it. Comparing against
            # `_recommended_build_system`'s own answer for the same directory ties the client's
            # output to the function this file also tests directly, so a rename breaks one place.
            expected = _load_server_module()._recommended_build_system(tmp, "fortran")
            self.assertEqual(payload["recommended_build_system"], expected["build_system"])
            self.assertEqual(payload["reason"], expected["reason"])
            self.assertEqual(payload["project_dir"], tmp)

    def test_an_unknown_tool_is_reported_rather_than_returning_an_empty_result(self) -> None:
        """The other error channel — a JSON-RPC `error`, not an `isError` result — reaches the
        caller too. A client that dropped it would return `{}` with exit 0, which reads as a tool
        that ran and found nothing."""
        done = self._call("no_such_tool", {}, workflow=False)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("no_such_tool", done.stderr)

    def test_the_client_works_from_any_working_directory(self) -> None:
        """It did not, and the first version of this row PINNED that as the specification.

        `mcp_call.py` spawned the server by the relative path
        `mcp_servers/build_runtime_server.py`, so every documented use silently required the
        checkout root as `cwd` — and the failure named the wrong layer: the spawn failed, the
        server's `stderr` was captured and never read, and the caller saw `unexpected EOF while
        reading MCP message header`. (My first version of this row asserted only a non-zero exit
        and its docstring said the failure "names a missing file". Driven for real, no
        missing-file message appears anywhere. Round 1 found both halves.)

        Why that was worth fixing rather than recording: this client is the instrument
        `docs/RUNBOOK.md` and `.claude/skills/atmofab-enforcement-change` hand an operator to PROVE
        a capability-gate refusal. With the old behaviour, "the gate refused the call" and "the
        client never started" were the same non-zero exit — so a verification could be recorded as
        passed by someone standing in the wrong directory, which is a false record in the audit
        trail. The test that pinned it would then have turned the fix into a regression.
        """
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "Makefile").write_text("all:\n\t@true\n", encoding="utf-8")
            env = {k: v for k, v in os.environ.items() if not k.startswith("ATMOFAB_")}
            done = subprocess.run(
                [sys.executable, str(self.REPO_ROOT / "mcp_servers" / "mcp_call.py"),
                 "--tool", "detect_build_system",
                 "--args-json", json.dumps({"project_dir": tmp})],
                cwd=tmp, env=env, capture_output=True, text=True, timeout=30, check=False)
            self.assertEqual(done.returncode, 0, done.stderr)
            expected = _load_server_module()._recommended_build_system(tmp, "")
            self.assertEqual(json.loads(done.stdout)["recommended_build_system"],
                             expected["build_system"])

    def test_a_failure_to_START_the_server_is_not_reported_as_a_framing_error(self) -> None:
        """The other half, and the one with the route.

        A caller cannot act on `unexpected EOF while reading MCP message header` when the real
        cause is that the server never ran. Worse, for the operator running a gate verification it
        is indistinguishable from the refusal they are trying to observe. The client now reads what
        the server said before it stopped talking and puts it in the error.

        Driven by pointing the client at a server that cannot start, rather than by reading the
        code: the property is what reaches the caller's stderr.
        """
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "mcp_call.py"
            source = (self.REPO_ROOT / "mcp_servers" / "mcp_call.py").read_text(encoding="utf-8")
            server = Path(tmp) / "build_runtime_server.py"
            server.write_text("import sys\nsys.stderr.write('BOOM: cannot start\\n')\n"
                              "raise SystemExit(3)\n", encoding="utf-8")
            broken.write_text(source, encoding="utf-8")
            env = {k: v for k, v in os.environ.items() if not k.startswith("ATMOFAB_")}
            done = subprocess.run(
                [sys.executable, str(broken), "--tool", "detect_build_system",
                 "--args-json", json.dumps({"project_dir": tmp})],
                cwd=self.REPO_ROOT, env=env, capture_output=True, text=True, timeout=30,
                check=False)
        self.assertNotEqual(done.returncode, 0)
        # On the DIAGNOSTIC, not on the stream. `done.stderr` also carries anything the CHILD
        # inherited a terminal for, so `assertIn("BOOM", done.stderr)` is satisfied by a client
        # that captured nothing and reported "(nothing on stderr)" — measured in round 2:
        # removing `stderr=subprocess.PIPE` from the spawn left this row green while the client's
        # own message said, verbatim, that the server produced nothing.
        self.assertIn("produced: BOOM: cannot start", done.stderr,
                      "the server's own diagnostic never reached the caller's ERROR, so a server "
                      "that cannot start is still reported as a framing error")
        self.assertNotIn("(nothing on stderr)", done.stderr)
        self.assertIn("build_runtime_server.py", done.stderr,
                      "the error does not say which server it failed to talk to")

    def test_a_server_that_stays_alive_and_talks_nonsense_does_not_hang_the_client(self) -> None:
        """The ordering inside the failure path, which nothing observed.

        `_server_stderr` reads to EOF. On a failure where the server is STILL RUNNING — a
        non-JSON line on its stdout reaches `json.loads` while the process keeps waiting on a
        stdin pipe this client still holds open — reading before killing it blocks for ever, and
        the client has no timeout of its own. Round 2 measured both the reordering and the
        deletion of the cleanup surviving the whole file.

        WHAT THE WITNESS ACTUALLY IS, corrected after round 3 measured it: the `timeout` on the
        subprocess call, not the `assertLess` that used to sit below. Under the reordering, the
        client never returns and the row died as a `subprocess.TimeoutExpired` ERROR — the elapsed
        assertion was never reached, and replacing it with `pass` left the row green. So the
        timeout is caught here and turned into a `fail()` that says what happened, which is what
        the previous docstring claimed and did not have.

        WHAT IT DOES NOT COVER, also measured: a server that reads stdin and writes NOTHING hangs
        this client for ever — `_read_message` blocks in `readline()`, no exception is raised, and
        `mcp_call.py` has no timeout of its own. That is unchanged from `origin/main` and is
        recorded in `TODO.md`; this row is about the failure path, not about every hang.
        """
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "build_runtime_server.py").write_text(
                "import sys\n"
                "sys.stderr.write('BOOM: alive but wrong\\n')\n"
                "sys.stderr.flush()\n"
                "sys.stdout.write('not json at all\\n')\n"
                "sys.stdout.flush()\n"
                "sys.stdin.read()\n",  # stays alive on the stdin pipe the client holds open
                encoding="utf-8")
            client = Path(tmp) / "mcp_call.py"
            client.write_text(
                (self.REPO_ROOT / "mcp_servers" / "mcp_call.py").read_text(encoding="utf-8"),
                encoding="utf-8")
            env = {k: v for k, v in os.environ.items() if not k.startswith("ATMOFAB_")}
            try:
                done = subprocess.run(
                    [sys.executable, str(client), "--tool", "detect_build_system",
                     "--args-json", json.dumps({"project_dir": tmp})],
                    cwd=self.REPO_ROOT, env=env, capture_output=True, text=True, timeout=20,
                    check=False)
            except subprocess.TimeoutExpired:
                self.fail(
                    "the client did not return for a server that is still alive: reading the "
                    "server's stderr before killing it blocks until EOF, and this client has no "
                    "timeout of its own, so the documented gate-verification command never "
                    "returns")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("produced: BOOM: alive but wrong", done.stderr,
                      "the diagnostic must still be reported when the server is alive")

    def test_a_malformed_frame_from_the_server_still_carries_the_diagnostic(self) -> None:
        """The framing failure that is NOT a JSON error, and it was uncovered.

        `_read_message` does `int(header_value)`, which raises a BARE `ValueError` on a
        non-numeric `Content-Length` — `json.JSONDecodeError` is a `ValueError`, but not the other
        way round, so a clause catching only the JSON one lets this escape without the server's
        stderr attached. Measured in round 2: narrowing the clause back survived the whole file,
        i.e. nothing drove this branch through the client. `mcp_servers/build_runtime_server.py`'s
        own tests record the same header as reachable, so this is not an invented shape.
        """
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "build_runtime_server.py").write_text(
                "import sys\n"
                "sys.stderr.write('BOOM: bad framing\\n')\n"
                "sys.stderr.flush()\n"
                "sys.stdout.write('Content-Length: not-a-number\\r\\n\\r\\n{}')\n"
                "sys.stdout.flush()\n",
                encoding="utf-8")
            client = Path(tmp) / "mcp_call.py"
            client.write_text(
                (self.REPO_ROOT / "mcp_servers" / "mcp_call.py").read_text(encoding="utf-8"),
                encoding="utf-8")
            env = {k: v for k, v in os.environ.items() if not k.startswith("ATMOFAB_")}
            done = subprocess.run(
                [sys.executable, str(client), "--tool", "detect_build_system",
                 "--args-json", json.dumps({"project_dir": tmp})],
                cwd=self.REPO_ROOT, env=env, capture_output=True, text=True, timeout=20,
                check=False)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("produced: BOOM: bad framing", done.stderr,
                      "a non-numeric Content-Length escaped without the server's diagnostic")

    def test_the_documents_that_teach_this_client_still_name_it(self) -> None:
        """The reason this class is worth its runtime: the client is an INSTRUMENT of the review
        procedure, not a product feature. If the documents stop pointing at it the tests here go
        on passing while nothing uses it — so the coupling is asserted in the direction that
        matters, from the documents to the file."""
        sources = {
            "docs/RUNBOOK.md": "mcp_call.py",
            ".claude/skills/atmofab-enforcement-change/references/verification.md": "mcp_call.py",
            "TODO.md": "mcp_call.py",
            # The review-loop skill names the module without its extension, which is why the
            # needle differs — asserting `mcp_call.py` there would have been a row that could
            # only fail. Round 1 pointed out that the class docstring names four documents and
            # this loop checked two of them.
            ".claude/skills/atmofab-review-loop/SKILL.md": "mcp_call",
        }
        for rel, needle in sorted(sources.items()):
            path = self.REPO_ROOT / rel
            with self.subTest(document=rel):
                self.assertTrue(path.is_file(), f"{rel} is gone; this coupling has no subject")
                self.assertIn(needle, path.read_text(encoding="utf-8"))


class SchemaMinimumsAreEnforcedTests(unittest.TestCase):
    """An MCP argument schema is advisory; `_bounded_int` is what makes it a rule.

    The served schema declares `"minimum": 1` on `jobs` and `timeout_sec` and 1000 on
    `capture_limit`, and a client is free to ignore it — `make -j-5` then waits forever
    and `timeout_sec=0` kills the build instantly, neither with an error naming the
    cause. `_bounded_int` is the only thing refusing them.

    It lost its witness in PR-2: the check lived in `OrchestratedEnvAllowlistTests`,
    which was deleted whole with the capability gate, and it has nothing to do with that
    gate. Deleting the `if value < minimum` raise left the full suite green.

    The minimums are read OUT OF THE SERVED SCHEMA rather than restated here, so the two
    cannot drift apart in the direction this test exists to catch: a schema that declares
    a bound the code does not hold."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_server_module()

    def setUp(self) -> None:
        self.project_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.project_dir, ignore_errors=True)

    def _args(self, tool: str) -> dict:
        args: dict = {"project_dir": str(self.project_dir)}
        if tool == "run_program":
            args["command"] = ["true"]
        if tool == "compile_project":
            args["build_system"] = "make"
        if tool == "run_linter":
            args["preset"] = "fortitude"
        return args

    def _bounded_properties(self) -> list[tuple[str, str, int]]:
        out = []
        for name, tool in self.mod.TOOLS.items():
            for prop, schema in tool.input_schema.get("properties", {}).items():
                if schema.get("type") == "integer" and "minimum" in schema:
                    out.append((name, prop, int(schema["minimum"])))
        return out

    def test_every_declared_minimum_is_refused_below_its_bound(self) -> None:
        bounded = self._bounded_properties()
        self.assertTrue(bounded, "no integer minimum in any served schema; "
                                 "this coupling has lost its subject")
        for tool, prop, minimum in bounded:
            with self.subTest(tool=tool, argument=prop):
                args = self._args(tool)
                args[prop] = minimum - 1
                with mock.patch.object(
                    self.mod, "_run_command",
                    return_value={"ok": True, "return_code": 0,
                                  "stdout": "", "stderr": ""},
                ) as run_command:
                    with self.assertRaises(ValueError) as ctx:
                        getattr(self.mod, f"tool_{tool}")(args)
                self.assertIn(prop, str(ctx.exception))
                self.assertIn(str(minimum), str(ctx.exception))
                run_command.assert_not_called()

    def test_the_bound_itself_is_accepted(self) -> None:
        # Off-by-one in the other direction: a rule that refused the declared minimum
        # would make the schema a lie the same way.
        for tool, prop, minimum in self._bounded_properties():
            with self.subTest(tool=tool, argument=prop):
                args = self._args(tool)
                args[prop] = minimum
                with mock.patch.object(
                    self.mod, "_run_command",
                    return_value={"ok": True, "return_code": 0,
                                  "stdout": "", "stderr": ""},
                ):
                    getattr(self.mod, f"tool_{tool}")(args)


class ServedSchemaDescribesWhatIsEnforcedTests(unittest.TestCase):
    """`compile_project`'s served descriptions, driven against the rules they describe.

    `mcp_servers/tools/*.json` exists for only two tools, so `ToolSchemaDocumentParityTests`
    compares a document to the served schema for those two and `compile_project`'s
    descriptions are coupled to NOTHING. That is how they went on advertising the retired
    allowlist to every client through PR-2 — "Refused under an orchestration", "only
    assignments to OBJDIR, BINDIR, RUNDIR, BIN, SPEC, CASES are accepted" — and it is how the
    `extra_args` text drifted again one commit after it was corrected, still saying every
    element must be a make assignment after that rule was scoped to make.

    Coupled by DRIVING: each row runs the real validator, asserts it refuses (so the row
    cannot pass by describing a rule that is gone), and then asserts the served description
    names the rule. The code is the authority; the description is checked against it."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_server_module()

    def _description(self, tool: str, argument: str) -> str:
        return self.mod.TOOLS[tool].input_schema["properties"][argument]["description"]

    def test_every_argv_refusal_is_described_where_the_caller_reads_it(self) -> None:
        rows = (
            ("target", {"target": "--eval=$(shell id)"}, "not open with -"),
            ("target", {"target": "a b"}, "whitespace"),
            ("extra_args", {"extra_args": ["--release"]}, "ASSIGN a make variable"),
            ("extra_args", {"extra_args": ["SHELL=/tmp/x"]}, "redirection of what is executed"),
            ("extra_args", {"extra_args": ["X=a; id"]}, "character a shell acts on"),
        )
        for argument, payload, phrase in rows:
            with self.subTest(argument=argument, payload=payload):
                # (1) the rule is REALLY enforced — otherwise the description below would be
                # describing something that no longer happens, which is the whole defect.
                with self.assertRaises(ValueError):
                    self.mod._validate_build_argv_overrides(
                        payload.get("target"), payload.get("extra_args", []),
                        "compile_project", build_system="make")
                # (2) and the client is told.
                self.assertIn(phrase, self._description("compile_project", argument))

    def test_no_served_description_names_a_retired_mechanism(self) -> None:
        # Derived from what the server REFUSES as retired, plus the phrase the two deleted
        # modes were spelled with. A description naming one of these is describing a contract
        # this server no longer implements.
        retired = tuple(self.mod._RETIRED_ARGUMENTS) + (
            "under an orchestration", "Under an orchestration", "allowlist")
        for name, tool in self.mod.TOOLS.items():
            for argument, spec in tool.input_schema.get("properties", {}).items():
                description = spec.get("description", "")
                for token in retired:
                    with self.subTest(tool=name, argument=argument, token=token):
                        self.assertNotIn(token, description)

    def test_every_declared_minimum_is_covered_by_the_row_that_drives_them(self) -> None:
        # The other half of "the schema is advisory": a declared minimum the code does not
        # hold is the same defect in the other direction. `SchemaMinimumsAreEnforcedTests`
        # derives its subject from the schema and drives each; this row pins that the two
        # derivations see the SAME set, so a new bounded argument cannot be added to the
        # schema and skipped there by a derivation that quietly narrowed.
        declared = {
            (name, argument)
            for name, tool in self.mod.TOOLS.items()
            for argument, spec in tool.input_schema.get("properties", {}).items()
            if spec.get("type") == "integer" and "minimum" in spec
        }
        self.assertTrue(declared, "no integer minimum in any served schema")
        driven = {
            (tool, argument)
            for tool, argument, _minimum
            in SchemaMinimumsAreEnforcedTests._bounded_properties(self)
        }
        self.assertEqual(declared, driven)


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

