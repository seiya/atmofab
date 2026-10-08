"""Tests for tools/build_runtime.py, the build-runtime library the conductor calls in-process.

run_syntax_check: the Generate.syntax compiler front-end gate — adapter argv shape,
module/use topological source ordering, missing-compiler skip, custom-command
rejection, and (when gfortran is installed) a real -fsyntax-only smoke covering the
error classes the retired post_generate text heuristics used to mimic.
"""

import importlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import typing
import unittest
from pathlib import Path
from unittest import mock

_MODULE_PATH = Path(__file__).resolve().parent.parent / "build_runtime.py"


def _load_module():
    """`tools.build_runtime`, the object the conductor's `from tools.build_runtime import`
    statements read, so a `mock.patch.object` on it is what a conductor call sees."""
    return importlib.import_module("tools.build_runtime")


def _fresh_module():
    """A FRESH execution of `tools/build_runtime.py`, for a row that asks what the module's
    import-time tables are derived from. Registered under a private name, never as
    `tools.build_runtime`, so the shared module every other row reads is left untouched."""
    spec = importlib.util.spec_from_file_location("_build_runtime_fresh", _MODULE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _gfortran_syntax():
    """The gfortran `syntax_check` module, reached the way the library reaches it."""
    from tools.backends import registry
    return registry.capability_module("compiler", "gfortran", "syntax_check")


def _fortran_syntax():
    """The Fortran `syntax_promotions` module, reached the way the library reaches it."""
    from tools.backends import registry
    return registry.capability_module("language", "fortran", "syntax_promotions")


_HAVE_GFORTRAN = shutil.which("gfortran") is not None


class _StandaloneServerEnvMixin:
    """Pin the library's own environment to standalone for tests that call a gated
    handler without `orchestration_id`.

    Those calls are refused when the library runs under the workflow
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
        cls.mod = _load_module()

    def _src_dir(self, files: dict[str, str]) -> Path:
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        for name, text in files.items():
            (d / name).write_text(text, encoding="utf-8")
        return d

    def test_gfortran_adapter_argv_shape(self) -> None:
        # The argv moved to the compiler backend and the promoted classes to the language
        # backend (issue #289, R4-b PR-2); `run_syntax_check` composes the two. The composed
        # line is the one the gate has always run.
        adapter = _gfortran_syntax()
        promotions = tuple(_fortran_syntax().PROMOTED_WARNINGS)
        argv = adapter.argv(standard="f2008", scratch_dir=".mods", openmp=False,
                            promotions=promotions, architecture=None,
                            sources=["a.f90", "b.f90"])
        self.assertEqual(
            argv,
            ["gfortran", "-fsyntax-only", "-std=f2008",
             "-Werror=unused-dummy-argument", "-Werror=unused-variable", "-Werror=ampersand",
             "-J", ".mods", "-I", ".mods",
             "a.f90", "b.f90"])
        argv = adapter.argv(standard="f2018", scratch_dir=".mods", openmp=True,
                            promotions=promotions, architecture="x86_64", sources=["x.f90"])
        self.assertIn("-fopenmp", argv)
        self.assertIn("-std=f2018", argv)
        self.assertIn("-Werror=unused-dummy-argument", argv)
        self.assertIn("-Werror=unused-variable", argv)
        self.assertIn("-Werror=ampersand", argv)
        # an architecture is accepted and not read by a CPU front end
        self.assertNotIn("x86_64", " ".join(argv))
        # sources stay last so the compiler reads them after the mod-dir flags
        self.assertEqual(argv[-1], "x.f90")

    def test_compiler_and_std_are_required(self) -> None:
        # Both used to default to one language's spelling (`gfortran`, `f2008`) in a tool
        # that serves every language; the caller names its target's values.
        d = self._src_dir({"a.f90": "program p\nend program p\n"})
        for args in ({"project_dir": str(d)},
                     {"project_dir": str(d), "compiler": "gfortran"},
                     {"project_dir": str(d), "std": "f2008"},
                     {"project_dir": str(d), "compiler": " ", "std": "f2008"}):
            with self.subTest(args=args):
                with self.assertRaises(ValueError) as ctx:
                    self.mod.tool_run_syntax_check(args)
                self.assertIn("requires a non-empty string", str(ctx.exception))

    def test_a_compiler_that_declares_no_syntax_check_is_refused(self) -> None:
        # A registered compiler record is not an adapter: only `syntax_check` makes one.
        from tools.backends import registry
        d = self._src_dir({})
        with mock.patch.dict(registry._BACKENDS, {
                ("compiler", "frt"): registry.Backend("compiler", "frt", None)}):
            with self.assertRaises(ValueError) as ctx:
                self.mod.tool_run_syntax_check(
                    {"project_dir": str(d), "compiler": "frt", "std": "f2008"})
        self.assertIn("supported=gfortran", str(ctx.exception))

    def test_source_order_topological_by_module_use(self) -> None:
        d = self._src_dir({
            # alphabetically first but uses the module defined last
            "a_runner.f90": "program p\n  use z_model, only: x\nend program p\n",
            "m_checks.f90": "module m_checks\n  use z_model\nend module m_checks\n",
            "z_model.f90": "module z_model\n  integer :: x\nend module z_model\n",
        })
        self.assertEqual(
            _fortran_syntax().compile_order(d),
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
            _fortran_syntax().compile_order(d), ["a.f90", "user.f90"])

    def test_source_order_ignores_unknown_and_intrinsic_modules(self) -> None:
        d = self._src_dir({
            "a.f90": "program p\n  use, intrinsic :: iso_fortran_env, only: int64\n"
                     "  use some_external_lib\nend program p\n",
        })
        self.assertEqual(_fortran_syntax().compile_order(d), ["a.f90"])

    def test_source_order_module_procedure_not_a_definition(self) -> None:
        d = self._src_dir({
            "a.f90": "submodule (m) impl\ncontains\nmodule procedure f\nend procedure f\n"
                     "end submodule impl\n",
            "b.f90": "module b_mod\nend module b_mod\n",
        })
        # `module procedure` must not register a module named "procedure"/f.
        self.assertEqual(_fortran_syntax().compile_order(d), ["a.f90", "b.f90"])

    def test_source_order_returns_a_nested_source_as_a_relative_path(self) -> None:
        # Issue #420: the walk reaches every depth so that the name rule refuses a nested
        # source instead of the stage skipping it while the build links it. The top-level
        # answer is the one the rows above pin, unchanged.
        d = self._src_dir({
            "a_runner.f90": "program p\n  use z_model, only: x\nend program p\n",
            "z_model.f90": "module z_model\n  integer :: x\nend module z_model\n",
        })
        (d / "sub").mkdir()
        (d / "sub" / "x.f90").write_text("module x_mod\nend module x_mod\n", encoding="utf-8")
        order = _fortran_syntax().compile_order(d)
        self.assertIn("sub/x.f90", order)
        self.assertEqual([n for n in order if "/" not in n], ["z_model.f90", "a_runner.f90"])

    def test_every_syntax_language_walks_at_any_depth(self) -> None:
        """Held for every `language` that provides `syntax_promotions`, so a later language
        backend meets the contract without a test of its own (issue #420)."""
        from tools.backends import registry
        languages = [lang for lang in registry.backend_ids("language")
                     if registry.provides("language", lang, "syntax_promotions")]
        self.assertTrue(languages)
        for lang in languages:
            with self.subTest(language=lang):
                module = registry.capability_module("language", lang, "syntax_promotions")
                suffix = tuple(module.SOURCE_SUFFIXES)[0]
                d = self._src_dir({})
                (d / "deep" / "er").mkdir(parents=True)
                (d / "deep" / "er" / f"n{suffix}").write_text("\n", encoding="utf-8")
                self.assertEqual(module.compile_order(d), [f"deep/er/n{suffix}"])

    def test_rejects_custom_command(self) -> None:
        d = self._src_dir({})
        with self.assertRaises(ValueError):
            self.mod.tool_run_syntax_check(
                {"project_dir": str(d), "command": ["gfortran", "x.f90"]})

    def test_rejects_unknown_compiler(self) -> None:
        d = self._src_dir({})
        with self.assertRaises(ValueError) as ctx:
            self.mod.tool_run_syntax_check({"project_dir": str(d), "compiler": "frt", "std": "f2008"})
        self.assertIn("supported=gfortran", str(ctx.exception))

    def test_missing_compiler_returns_skipped(self) -> None:
        d = self._src_dir({"a.f90": "program p\nend program p\n"})
        with mock.patch.object(self.mod.shutil, "which", return_value=None):
            result = self.mod.tool_run_syntax_check({"project_dir": str(d), "compiler": "gfortran", "std": "f2008"})
        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertIn("compiler not available", result["reason"])

    def test_no_sources_returns_skipped(self) -> None:
        d = self._src_dir({"notes.txt": "not fortran"})
        with mock.patch.object(self.mod.shutil, "which", return_value="/usr/bin/gfortran"):
            result = self.mod.tool_run_syntax_check({"project_dir": str(d), "compiler": "gfortran", "std": "f2008"})
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
                {"project_dir": str(d), "compiler": "gfortran", "std": "f2008", "openmp": True})
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

    def _argv0_for(self, **extra: object) -> tuple[str, list[str], list[str]]:
        """The syntax stage's argv and the programs it asked `which` for, for a gfortran stage
        with `extra` arguments."""
        d = self._src_dir({"m.f90": "module m\nend module m\n"})
        fake = subprocess.CompletedProcess(args=["x"], returncode=0, stdout="", stderr="")
        with mock.patch.object(self.mod.shutil, "which", return_value="/opt/x/bin/tool") as which, \
                mock.patch.object(self.mod.subprocess, "run", return_value=fake) as run_mock:
            self.mod.tool_run_syntax_check(
                {"project_dir": str(d), "compiler": "gfortran", "std": "f2008", **extra})
        argv = run_mock.call_args_list[0].args[0]
        self._last_version_argv = [c.args[0] for c in run_mock.call_args_list[1:]]
        return argv[0], argv, [c.args[0] for c in which.call_args_list]

    def test_a_compiler_wrapper_backend_runs_the_stage_through_its_wrapper(self) -> None:
        """Issue #316: the stage of the compiler the wrapper runs is the same argv with the
        wrapper at argv[0], and the availability check asks for the wrapper."""
        wrapper = self.mod._backend_registry().capability_module(
            "parallel", "mpi", "compiler_wrapper")
        self.assertEqual(wrapper.WRAPPED_COMPILER, "gfortran")
        plain0, plain, plain_which = self._argv0_for()
        wrapped0, wrapped, wrapped_which = self._argv0_for(parallel_backend="mpi")
        self.assertEqual(plain0, "gfortran")
        self.assertEqual(wrapped0, wrapper.COMPILER_WRAPPER)
        self.assertEqual(wrapped[1:], plain[1:])
        self.assertIn(wrapper.COMPILER_WRAPPER, wrapped_which)
        self.assertNotIn(wrapper.COMPILER_WRAPPER, plain_which)
        # The recorded version is asked through the wrapper too (round 2): it runs the compiler
        # it is configured with. The probe is cached per argv, so clear it for this row.
        self.mod._syntax_compiler_version.cache_clear()
        self._argv0_for(parallel_backend="mpi")
        self.assertIn(list(wrapper.wrap(_gfortran_syntax().VERSION_ARGV)),
                      self._last_version_argv)
        # A backend that declares no wrapper, and a token with no record, run the adapter's.
        for backend in ("openmp", "none", "zz_no_such_model"):
            with self.subTest(parallel_backend=backend):
                self.assertEqual(self._argv0_for(parallel_backend=backend)[0], "gfortran")

    def test_a_wrapper_does_not_replace_another_compilers_stage(self) -> None:
        wrapper = self.mod._backend_registry().capability_module(
            "parallel", "mpi", "compiler_wrapper")
        self.assertIsNone(self.mod.syntax_compiler_wrapper("mpi", "nvcc"))
        self.assertIs(self.mod.syntax_compiler_wrapper("mpi", wrapper.WRAPPED_COMPILER), wrapper)
        self.assertIsNone(self.mod.syntax_compiler_wrapper(None, wrapper.WRAPPED_COMPILER))

    def test_a_malformed_parallel_backend_is_refused(self) -> None:
        d = self._src_dir({"m.f90": "module m\nend module m\n"})
        for bad in ("", "  ", 3, ["mpi"]):
            with self.subTest(parallel_backend=bad), self.assertRaises(ValueError):
                self.mod.tool_run_syntax_check({"project_dir": str(d), "compiler": "gfortran",
                                                "std": "f2008", "parallel_backend": bad})

    def test_compile_error_returns_ok_false(self) -> None:
        d = self._src_dir({"bad.f90": "program p\n  implicit none (external)\nend program p\n"})
        fake = subprocess.CompletedProcess(
            args=["gfortran"], returncode=1, stdout="",
            stderr="Error: Fortran 2018: IMPLICIT NONE with spec list")
        with mock.patch.object(self.mod.shutil, "which", return_value="/usr/bin/gfortran"), \
                mock.patch.object(self.mod.subprocess, "run", return_value=fake):
            result = self.mod.tool_run_syntax_check({"project_dir": str(d), "compiler": "gfortran", "std": "f2008"})
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
        cls.mod = _load_module()

    def _check(self, files: dict[str, str]) -> dict:
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        for name, text in files.items():
            (d / name).write_text(text, encoding="utf-8")
        return self.mod.tool_run_syntax_check({"project_dir": str(d), "compiler": "gfortran", "std": "f2008"})

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
        # The conductor compiles the adapter's CANARY_SOURCE with the failing stage's own argv to
        # tell a broken INVOCATION (an `-std=` value the driver rejects, so no source is ever
        # parsed) apart from broken sources. Both halves of that must hold against the real
        # compiler: the canary passes under each standard a node may target — were it invalid
        # Fortran, EVERY failing stage would be misattributed to an unviable invocation and
        # nothing would ever reach the leaf — and it fails when the std is not one the driver
        # knows, which is the signal the attribution keys on.
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        (d / _gfortran_syntax().CANARY_FILENAME).write_text(
            _gfortran_syntax().CANARY_SOURCE, encoding="utf-8")
        # every standard a node may declare — a canary that failed any one of these would
        # fail_closed every ordinary syntax finding on a node targeting it
        for std in ("f95", "f2003", "f2008", "f2018", "gnu", "legacy"):
            result = self.mod.tool_run_syntax_check({"project_dir": str(d), "compiler": "gfortran", "std": std})
            self.assertTrue(result["ok"], msg=f"{std}: {result.get('stderr')}")
        bad = self.mod.tool_run_syntax_check({"project_dir": str(d), "compiler": "gfortran", "std": "2008"})
        self.assertFalse(bad["ok"])
        self.assertFalse(bad["skipped"])

    def test_missing_ampersand_continuation_fails(self) -> None:
        # gfortran EXTENDS the standard by accepting a continued character literal whose
        # resume line carries no leading `&`. Left a warning, that shape put a counted-`do`
        # spelling written inside a string at a PHYSICAL line start, where the fail_closed
        # OpenMP presence floor (`_validate_parallel_presence_floor`, anchored and stateless)
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


class CallerEnvTests(unittest.TestCase):
    """A caller-supplied `env` reaches the command as given.

    The library refused execution-redirecting names and shell-active values until issue
    #457, which deleted that layer: the conductor is the only caller under a run, and every
    `env` it passes is composed from host paths, the target profile and the backend
    packages. What is pinned here is that each entry point hands the caller's `env` through
    unmodified, and that the library's own addition (the pytest `PYTHONPATH`) still happens."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_module()

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
        if tool == "run_linter":
            args["preset"] = "fortitude"
        if tool == "run_syntax_check":
            args.update(compiler="gfortran", std="f2008")
        return args

    def test_compile_project_defaults_are_the_module_values_a_remote_build_is_handed(self) -> None:
        """Issue #333: a build at a remote site is handed `COMPILE_PROJECT_TIMEOUT_SEC` and
        `default_build_jobs()` because `tool_compile_project` does not run there, so the library must build with
        those same values. Driven under non-default values, so a literal copy of today's
        defaults in `tool_compile_project` is red."""
        with mock.patch.object(self.mod, "COMPILE_PROJECT_TIMEOUT_SEC", 123), \
                mock.patch.object(self.mod, "default_build_jobs", return_value=7), \
                self._spy_run_command() as run_command:
            self.mod.tool_compile_project(self._args("compile_project", {}))
        kwargs = run_command.call_args.kwargs
        self.assertEqual(kwargs["timeout_sec"], 123)
        self.assertEqual(kwargs["command"], self.mod.build_command("make", None, 7, []))
        with mock.patch.object(self.mod.os, "cpu_count", return_value=9):
            self.assertEqual(self.mod.default_build_jobs(), 4)
        with mock.patch.object(self.mod.os, "cpu_count", return_value=None):
            self.assertEqual(self.mod.default_build_jobs(), 1)

    def test_every_entry_point_hands_the_caller_env_through_unmodified(self) -> None:
        # No name dropped, no value rewritten, nothing added — on each of the five entry
        # points that take `env`.
        # `PATH` and `LD_PRELOAD` among them: names the deleted denylist refused (issue #457).
        payload = {"LDFLAGS": "-lm", "ENVIRONMENT": "ci", "FC": "/usr/bin/gfortran",
                   "PATH": "/opt/x/bin", "LD_PRELOAD": "/opt/x/lib.so"}
        for tool in ("compile_project", "run_program", "run_quality_checks", "run_linter",
                     "run_syntax_check"):
            with self.subTest(tool=tool), self._spy_run_command() as run_command, \
                    mock.patch.object(self.mod.shutil, "which", return_value="/bin/true"):
                (self.project_dir / "a.f90").write_text("program p\nend program p\n",
                                                        encoding="utf-8")
                getattr(self.mod, f"tool_{tool}")(self._args(tool, dict(payload)))
            self.assertEqual(run_command.call_args.kwargs["env"], payload)

    def test_the_conductor_quality_check_env_reaches_the_quality_check_unmodified(
        self,
    ) -> None:
        # Validate.execute's make_test re-run: `execute.quality_check_env`'s six variables.
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
                {"project_dir": str(self.project_dir), "preset": "make_test",
                 "env": dict(payload)})
        self.assertEqual(run_command.call_args.kwargs["env"], payload)

    def test_the_library_adds_its_own_pythonpath_for_the_pytest_preset(self) -> None:
        with self._spy_run_command() as run_command:
            self.mod.tool_run_quality_checks(
                {"project_dir": str(self.project_dir), "preset": "pytest"})
        # project_dir goes first; anything after it is this library's own inherited
        # PYTHONPATH, which varies with how the suite was started.
        self.assertEqual(
            run_command.call_args.kwargs["env"]["PYTHONPATH"].split(os.pathsep)[0],
            str(self.project_dir.resolve()))

    def test_the_conductor_launch_env_reaches_run_program_unmodified(self) -> None:
        # Since issue #289 a parallel runtime's thread variables are the CALLER's, composed by
        # `tools/host_execution.py` and passed as `env` — so they must
        # arrive exactly as sent: no name dropped, no value rewritten, nothing added.
        from tools.host_execution import launch_shape
        from tools.tests.target_fixtures import FORTRAN_CPU

        payload = dict(launch_shape(FORTRAN_CPU).env)
        self.assertTrue(payload, "the fixture profile launches with no environment, so this "
                                 "row would observe nothing")
        with self._spy_run_command() as run_command:
            self.mod.tool_run_program({
                "project_dir": str(self.project_dir), "command": ["true"],
                "env": dict(payload)})
        self.assertEqual(run_command.call_args.kwargs["env"], payload)


class CompileProjectArgumentTests(unittest.TestCase):
    """`compile_project`'s `target` and `extra_args` are checked for TYPE and nothing else.

    Until issue #457 the library also refused a switch, an execution-redirecting assignment
    and a shell-active character in either half. Every in-tree caller composes these values
    from host constants (`_build_inproc` passes the make backend's `build_overrides` and no
    `target`), so that layer was deleted; these rows pin what remains and that the argv the
    caller composes is the argv that runs."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_module()

    def setUp(self) -> None:
        self.project_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.project_dir, ignore_errors=True)

    def _run(self, **extra) -> mock.MagicMock:
        args: dict = {"project_dir": str(self.project_dir), "build_system": "make"}
        args.update(extra)
        with mock.patch.object(
            self.mod, "_run_command",
            return_value={"ok": True, "return_code": 0, "stdout": "", "stderr": ""},
        ) as run_command:
            self.mod.tool_compile_project(args)
        return run_command

    def test_the_types_are_checked_before_anything_runs(self) -> None:
        for payload, message in (({"extra_args": "OBJDIR=x"}, "extra_args must be an array"),
                                 ({"extra_args": ["A=1", 2]}, "extra_args must be an array"),
                                 ({"target": 7}, "target must be a string"),
                                 ({"env": ["A=1"]}, "env must be an object")):
            with self.subTest(payload=payload), mock.patch.object(
                    self.mod, "_run_command") as run_command:
                with self.assertRaises(ValueError) as ctx:
                    self.mod.tool_compile_project(
                        {"project_dir": str(self.project_dir), "build_system": "make",
                         **payload})
                self.assertIn(message, str(ctx.exception))
                run_command.assert_not_called()

    def test_the_conductor_payload_runs_as_composed(self) -> None:
        # Exactly the shape `_build_inproc` passes: three assignments, no target.
        extra = ["OBJDIR=/repo/workspace/tmp/a/build",
                 "BINDIR=/repo/workspace/binary/bin_1/bin", "BIN=sw2d_runner"]
        run_command = self._run(extra_args=list(extra), jobs=3)
        self.assertEqual(run_command.call_args.kwargs["command"],
                         self.mod.build_command("make", None, 3, extra))

    def test_the_target_is_the_string_that_runs(self) -> None:
        run_command = self._run(target="sw2d_runner", jobs=2)
        self.assertEqual(run_command.call_args.kwargs["command"],
                         self.mod.build_command("make", "sw2d_runner", 2, []))

    def test_a_non_make_build_system_may_pass_its_own_switches(self) -> None:
        run_command = self._run(build_system="cargo", extra_args=["--release"], jobs=2)
        self.assertEqual(run_command.call_args.kwargs["command"],
                         self.mod.build_command("cargo", None, 2, ["--release"]))


class SyntaxCheckSourcesTests(_StandaloneServerEnvMixin, unittest.TestCase):
    """`sources` is appended to the compiler front-end argv, so it is argv, not data.

    The gcc driver reads its own options anywhere in that list: `-B<dir>/` execs a
    planted `f951` and `@file` reads further options out of a file — whose own name may
    end in `.f90` — and either way the check returns ok=True having compiled something
    other than what was staged. The rule is what a source name IS. Refused in every
    mode; the workflow never passes this argument at all."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_module()

    def setUp(self) -> None:
        super().setUp()
        self.project_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.project_dir, ignore_errors=True)
        (self.project_dir / "a.f90").write_text("program p\nend program p\n", encoding="utf-8")

    def _call(self, sources: list) -> dict:
        return self.mod.tool_run_syntax_check(
            {"project_dir": str(self.project_dir), "sources": sources,
             "compiler": "gfortran", "std": "f2008"})

    def test_an_explicit_source_is_refused_without_a_compiler_too(self) -> None:
        # The skip for an uninstalled compiler used to return first, so `/etc/passwd`
        # came back `{ok: True, skipped: True}` on a machine without gfortran.
        with mock.patch.object(self.mod.shutil, "which", return_value=None):
            with self.assertRaises(ValueError) as ctx:
                self._call(["/etc/passwd"])
        self.assertIn("fortran source files in project_dir", str(ctx.exception))

    def test_a_clean_tree_still_skips_when_the_compiler_is_absent(self) -> None:
        with mock.patch.object(self.mod.shutil, "which", return_value=None):
            result = self.mod.tool_run_syntax_check({"project_dir": str(self.project_dir), "compiler": "gfortran", "std": "f2008"})
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
                self.assertIn("fortran source files in project_dir", str(ctx.exception))

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
                self.assertIn("fortran source files in project_dir", str(ctx.exception))

    def test_the_source_suffixes_are_the_tool_s_own(self) -> None:
        # The name rule is built from the same tuple auto-discovery uses, so an added
        # suffix cannot make an explicit `sources` list refuse a file the tool would
        # otherwise have found itself.
        suffixes = tuple(_fortran_syntax().SOURCE_SUFFIXES)
        for suffix in suffixes:
            with self.subTest(suffix=suffix):
                self.assertTrue(self.mod._build_syntax_source_re(suffixes).match(f"a{suffix}"))

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
                    self.mod.tool_run_syntax_check({"project_dir": str(self.project_dir), "compiler": "gfortran", "std": "f2008"})
                self.assertIn("fortran source files in project_dir", str(ctx.exception))

    def test_a_nested_source_is_refused_with_the_move_instruction(self) -> None:
        """Issue #420: a source below the top level is refused, not compiled and not skipped.

        The refusal is the author's only statement of the rule — the conductor hands this text
        to the leaf as its failure excerpt — so it must name the file and say where it goes.
        Raised before the compiler-availability and no-source skips, so a directory whose ONLY
        source is nested fails closed rather than reporting `skipped`."""
        only_nested = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, only_nested, ignore_errors=True)
        (self.project_dir / "build").mkdir()
        (self.project_dir / "build" / "x.f90").write_text("module x\nend module x\n",
                                                          encoding="utf-8")
        (only_nested / "build").mkdir()
        (only_nested / "build" / "x.f90").write_text("module x\nend module x\n",
                                                     encoding="utf-8")
        for project_dir in (self.project_dir, only_nested):
            for which in ("/usr/bin/gfortran", None):
                with self.subTest(project_dir=project_dir.name, compiler_installed=bool(which)):
                    with mock.patch.object(self.mod.shutil, "which", return_value=which), \
                            self.assertRaises(self.mod.SyntaxSourceNameError) as ctx:
                        self.mod.tool_run_syntax_check(
                            {"project_dir": str(project_dir), "compiler": "gfortran",
                             "std": "f2008"})
                    message = str(ctx.exception)
                    self.assertIn("refused: build/x.f90", message)
                    self.assertIn("move every source file to the top level", message)

    def test_a_flat_refusal_carries_no_move_instruction(self) -> None:
        # The sentence is conditional on a `/`: an option-shaped top-level name is not cured by
        # moving it, and telling the author to move it would be an instruction that cannot
        # converge.
        (self.project_dir / "-o.f90").write_text("program r\nend program r\n", encoding="utf-8")
        with mock.patch.object(self.mod.shutil, "which", return_value=None), \
                self.assertRaises(self.mod.SyntaxSourceNameError) as ctx:
            self.mod.tool_run_syntax_check(
                {"project_dir": str(self.project_dir), "compiler": "gfortran", "std": "f2008"})
        self.assertNotIn("top level", str(ctx.exception))

    def test_staged_source_names_are_accepted(self) -> None:
        with mock.patch.object(self.mod.shutil, "which", return_value=None):
            result = self._call(["a.f90"])
        # Reaches the ordinary missing-compiler skip, i.e. it was not refused.
        self.assertTrue(result["skipped"])


class BuildCommandTests(unittest.TestCase):
    """`_recommended_build_system` and `build_command` — the SUCCESS paths.

    The refusal side of this library is covered thickly; these two were referenced once each from
    the whole test corpus (measured at 9f2e16d). An earlier version of this sentence added "and
    never on a path that produces an argv", which round 1 falsified:
    `test_host_prerequisites.py:102` does take `build_command(build_system, None, 1, [])[0]`.
    What is true is narrower and is what these rows are for — each of those two references
    observes ONE thing (an executable name; one marker detection succeeding), so the argv SHAPES,
    the marker table as an enumeration, the two defaults and the knot between the two functions
    were unheld. Every `compile` this repository performs is one of these argv.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_module()

    def _dir_with(self, *names: str) -> str:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        for name in names:
            (Path(holder.name) / name).write_text("", encoding="utf-8")
        return holder.name

    @classmethod
    def _accepted_build_systems(cls) -> set[str]:
        """Every build system `build_command` ACCEPTS, found by calling it.

        THE QUESTION CHANGED HERE, and the reason is three rounds of the same failure. Asking
        "what does the dispatch look like" needs a reader, and every reader was defeated by the
        next spelling: a regex missed an arm reformatted across lines and one written with a
        double space; an `ast` walk over `==` and `in <literal>` missed `bs = build_system` bound
        to an alias, and missed `if build_system in _EXTRA:` against a module-level dict — which
        is THIS MODULE'S OWN IDIOM (`_LINT_PRESET_COMMANDS` is exactly that). Each fix produced
        the next spelling, which `.claude/skills/atmofab-review-loop` names as the sign that the
        pin is in the wrong place rather than the wrong shape.

        So the question is now one a LOOKUP can answer: `build_command` raises `ValueError` for
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
                cls.mod.build_command(candidate, None, 1, [])
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
        self.assertIn("compiled language", family["reason"])
        other = self.mod._recommended_build_system(empty, "haskell")
        self.assertEqual(other["build_system"], "make")
        self.assertEqual(other["reason"], "fallback default")

    #: Build systems whose EXECUTABLE is not their id. The one piece of knowledge this row holds,
    #: and it is held here because `build_command` is the only other place it exists — comparing
    #: argv[0] against `build_system_executable` instead proves nothing, since that function IS
    #: `build_command(bs, None, 1, [])[0]`. Round 1 measured the consequence: renaming `mvn`,
    #: `cargo`, `poetry` and `go build`'s argv all survived, because the assertion compared the
    #: implementation with itself.
    _EXECUTABLE_RENAMES: typing.ClassVar[dict[str, str]] = {"maven": "mvn"}

    def test_every_build_system_the_recommender_can_return_builds_an_argv(self) -> None:
        """The knot between the two functions, which nothing tied: a marker table entry naming a
        build system `build_command` does not implement is a `compile` that raises after the
        recommendation succeeded. The argv[0] check is against the EXPECTED executable, so a
        renamed program is caught rather than compared with itself."""
        markers = self._marker_table()
        self.assertEqual(len(markers), self._MARKER_COUNT,
                         "this sweep is reading fewer entries than the table has")
        for _marker, build_system in markers:
            with self.subTest(build_system=build_system):
                argv = self.mod.build_command(build_system, None, 4, [])
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

        Set identity against the dispatch, in both directions: every build system `build_command`
        implements has a row here, and every row is a build system it implements. The first
        version compared `argv[0]` with `build_system_executable`, which IS
        `build_command(bs, None, 1, [])[0]` — the implementation compared with itself, and four
        renames survived it.
        """
        implemented = set(self._BASE_ARGV)
        for build_system, expected in sorted(self._BASE_ARGV.items()):
            with self.subTest(build_system=build_system):
                self.assertEqual(self.mod.build_command(build_system, None, 4, []), expected)
        # The other direction: a build system the dispatch gained and this table did not.
        self.assertEqual(
            self._accepted_build_systems(), implemented,
            "the build systems build_command ACCEPTS and the argv this table records are "
            "different sets; a new adapter needs a row saying what it runs")

    def test_the_make_argv_is_the_documented_shape(self) -> None:
        """`make` is the default this repository actually runs, so its argv is pinned exactly —
        the jobs flag glued to `-j`, the target after it, extra arguments last."""
        self.assertEqual(self.mod.build_command("make", None, 4, []), ["make", "-j4"])
        self.assertEqual(self.mod.build_command("make", "all", 2, []), ["make", "-j2", "all"])
        self.assertEqual(self.mod.build_command("make", "all", 2, ["V=1"]),
                         ["make", "-j2", "all", "V=1"])

    def test_extra_arguments_reach_every_build_system_and_stay_last(self) -> None:
        """A family over the whole dispatch, and the property is one an operator relies on:
        arguments they passed are handed to the tool, unreordered. `cmake` is the one that puts
        them behind a `--` separator, so it is asserted separately rather than excluded."""
        for build_system in ("make", "meson", "ninja", "cargo", "go", "maven", "gradle",
                             "npm", "pnpm", "poetry"):
            with self.subTest(build_system=build_system):
                argv = self.mod.build_command(build_system, None, 1, ["--flag", "x"])
                self.assertEqual(argv[-2:], ["--flag", "x"])
        cmake = self.mod.build_command("cmake", "tgt", 3, ["--flag"])
        self.assertEqual(cmake, ["cmake", "--build", ".", "-j", "3", "--target", "tgt",
                                 "--", "--flag"])

    def test_a_build_system_with_no_adapter_is_refused_by_name(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self.mod.build_command("scons", None, 1, [])
        self.assertIn("scons", str(caught.exception))

    def test_the_default_target_is_the_tools_own_and_not_a_missing_argument(self) -> None:
        """`gradle` and `npm` substitute `build` for an absent target rather than omitting it, so
        the argv is well-formed either way. Recorded because the two shapes are indistinguishable
        from the caller's side."""
        self.assertEqual(self.mod.build_command("gradle", None, 1, []), ["gradle", "build"])
        self.assertEqual(self.mod.build_command("npm", None, 1, []), ["npm", "run", "build"])


class ArgumentContractTests(unittest.TestCase):
    """What each entry point accepts and refuses at its argument boundary.

    The rows here were spread over classes about the MCP layer deleted in issue #444 —
    `RetiredArgumentTests`, `SchemaMinimumsAreEnforcedTests`,
    `ServedSchemaDescribesWhatIsEnforcedTests`, `ToolSchemaDocumentParityTests` — and each
    observes the library rather than the protocol, so each was kept (classified per row by
    what its body calls). The served schema they used to derive their tables from is gone,
    so the tables are spelled here."""

    #: The integer bounds `_bounded_int` holds each entry point to: (tool, argument, minimum).
    #: Spelled from the call sites (`_bounded_int(..., <minimum>, "<argument>")`); the MCP
    #: schema the rows used to read them from declared the same eleven.
    BOUNDED = (
        ("compile_project", "jobs", 1),
        ("compile_project", "timeout_sec", 1),
        ("compile_project", "capture_limit", 1000),
        ("run_program", "timeout_sec", 1),
        ("run_program", "capture_limit", 1000),
        ("run_quality_checks", "timeout_sec", 1),
        ("run_quality_checks", "capture_limit", 1000),
        ("run_linter", "timeout_sec", 1),
        ("run_linter", "capture_limit", 1000),
        ("run_syntax_check", "timeout_sec", 1),
        ("run_syntax_check", "capture_limit", 1000),
    )

    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_module()

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
        if tool == "run_syntax_check":
            args.update(compiler="gfortran", std="f2008")
        return args

    def test_every_minimum_is_refused_below_its_bound(self) -> None:
        """`_bounded_int` is the only thing refusing these: `make -j-5` waits forever and
        `timeout_sec=0` kills the build instantly, neither with an error naming the cause.
        Deleting the `if value < minimum` raise once left the full suite green."""
        for tool, prop, minimum in self.BOUNDED:
            with self.subTest(tool=tool, argument=prop):
                args = self._args(tool)
                args[prop] = minimum - 1
                with mock.patch.object(
                    self.mod, "_run_command",
                    return_value={"ok": True, "return_code": 0,
                                  "stdout": "", "stderr": ""},
                ) as run_command, self.assertRaises(ValueError) as ctx:
                    getattr(self.mod, f"tool_{tool}")(args)
                self.assertIn(prop, str(ctx.exception))
                self.assertIn(str(minimum), str(ctx.exception))
                run_command.assert_not_called()

    def test_the_bound_itself_is_accepted(self) -> None:
        # Off-by-one in the other direction.
        for tool, prop, minimum in self.BOUNDED:
            with self.subTest(tool=tool, argument=prop):
                args = self._args(tool)
                args[prop] = minimum
                with mock.patch.object(
                    self.mod, "_run_command",
                    return_value={"ok": True, "return_code": 0,
                                  "stdout": "", "stderr": ""},
                ):
                    getattr(self.mod, f"tool_{tool}")(args)

    def test_the_required_set_is_what_the_entry_point_refuses_without(self) -> None:
        """Driven, for the two entry points whose required set issue #289 widened: dropping
        any required argument is a refusal naming it, and a call with all of them reaches the
        tool. The only row observing `run_linter`'s refusal of a missing `preset`."""
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        (d / "a.f90").write_text("program p\nend program p\n", encoding="utf-8")
        full = {
            "run_syntax_check": {"project_dir": str(d), "compiler": "gfortran", "std": "f2008"},
            "run_linter": {"project_dir": str(d), "preset": "fortitude"},
        }
        for tool, args in full.items():
            with self.subTest(tool=tool):
                handler = getattr(self.mod, f"tool_{tool}")
                with mock.patch.object(self.mod, "_run_command",
                                       return_value={"ok": True, "return_code": 0,
                                                     "stdout": "", "stderr": ""}), \
                        mock.patch.object(self.mod.shutil, "which", return_value="/bin/true"):
                    handler(dict(args))
                    for name in sorted(set(args) - {"project_dir"}):
                        partial = {k: v for k, v in args.items() if k != name}
                        with self.assertRaises(ValueError) as ctx:
                            handler(partial)
                        self.assertIn(name, str(ctx.exception))

    def test_compile_project_passes_its_target_to_the_build_tool(self) -> None:
        # `target` is `compile_project`'s build goal; `run_program` once read an argument of
        # the same name with another meaning (retired in issue #289).
        with mock.patch.object(
                self.mod, "_run_command",
                return_value={"ok": True, "return_code": 0}) as run_command:
            self.mod.tool_compile_project({
                "project_dir": str(self.project_dir), "build_system": "make",
                "target": "all"})
        run_command.assert_called_once()
        self.assertIn("all", run_command.call_args.kwargs["command"])

    def test_the_attribution_arguments_are_recorded(self) -> None:
        with mock.patch.object(
                self.mod, "_run_command",
                return_value={"ok": True, "return_code": 0}) as run_command:
            self.mod.tool_run_linter({
                "project_dir": str(self.project_dir), "preset": "ruff",
                "orchestration_id": "orch_x", "agent_run_id": "arid_x"})
        self.assertEqual(run_command.call_args.kwargs["attribution"],
                         {"orchestration_id": "orch_x", "agent_run_id": "arid_x"})


class EntryPointTableTests(unittest.TestCase):
    """`docs/BUILD_RUNTIME.md`'s entry-point table names exactly the module's `tool_*` functions.

    The README's MCP tool table was pinned to the served `TOOLS` registry until issue #444
    deleted both; this is that observation re-homed on what replaced them. Set identity in both
    directions, against the module's own names rather than a second literal."""

    DOC = Path(__file__).resolve().parents[2] / "docs" / "BUILD_RUNTIME.md"

    def _table_entry_points(self) -> set[str]:
        lines = self.DOC.read_text(encoding="utf-8").splitlines()
        start = lines.index("## Entry points")
        names: set[str] = set()
        for line in lines[start + 1:]:
            if line.startswith("## "):
                break
            m = re.match(r"^\| `(tool_[a-z_]+)` \|", line)
            if m:
                names.add(m.group(1))
        return names

    def test_the_table_is_the_modules_entry_point_set(self) -> None:
        mod = _load_module()
        defined = {name for name in vars(mod)
                   if name.startswith("tool_") and callable(getattr(mod, name))}
        self.assertEqual(len(defined), 5, defined)
        self.assertEqual(self._table_entry_points(), defined)


class RunLinterPresetDispatchTests(unittest.TestCase):
    """The preset -> argv table `tool_run_linter` runs and the launch-time host probe reads.

    These were an if-chain with `mixed` restating `fortitude`'s and `cppcheck`'s command lines a
    second time, and `mixed` had no test at all. The refactor that made the argv readable for the
    probe (issue #109) rewrote that path, so the shapes are pinned here: a simple preset returns
    the command's own keys plus `preset`, a composite returns `runs`, and both spellings are what
    the conductor's `_gate_lint_check` normalizes and what the lint evidence records.
    """

    def setUp(self) -> None:
        self.mod = _load_module()
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

    def test_the_served_presets_are_the_registry_s_linters(self) -> None:
        """The inverse of the row above: every linter whose record carries `lint` in
        `backend_provides` IS a served simple preset, and the served composites are the
        registry's `COMPOSITE_LINTERS` (issue #424). The row above holds for any SUBSET — a
        literal tuple missing a linter passes it — which is how the served schema's text came to
        omit `nvcc` while the table carried it; this row makes the set the registry's."""
        from tools.backends import registry

        extracted = {bid for bid in registry.backend_ids("linter")
                     if "lint" in registry.get("linter", bid).backend_provides}
        self.assertTrue(extracted)
        self.assertEqual(set(self.mod._SIMPLE_LINT_PRESETS), extracted)
        self.assertEqual(set(self.mod._LINT_PRESET_COMMANDS), extracted)
        self.assertEqual(self.mod._LINT_PRESET_COMPOSITES, dict(registry.COMPOSITE_LINTERS))
        for composite, members in registry.COMPOSITE_LINTERS.items():
            with self.subTest(composite=composite):
                self.assertEqual(self.mod.lint_preset_sub_presets(composite), members)
        # ... and it is DERIVED, not merely equal today: a fifth linter record and a second
        # composite declared in the registry are served by a freshly loaded module with no
        # library edit. A literal tuple or dict equal to today's registry passes the rows above
        # and fails here.
        fifth = registry.Backend("linter", "zz_lint", "tools.backends.linter.ruff",
                                 backend_provides=frozenset({"lint"}))
        with mock.patch.dict(registry._BACKENDS, {("linter", "zz_lint"): fifth}), \
                mock.patch.dict(registry.COMPOSITE_LINTERS, {"zz_comp": ("zz_lint", "ruff")}):
            fresh = _fresh_module()
            self.assertIn("zz_lint", fresh._SIMPLE_LINT_PRESETS)
            self.assertEqual(fresh._LINT_PRESET_COMMANDS["zz_lint"],
                             fresh._LINT_PRESET_COMMANDS["ruff"])
            self.assertEqual(fresh.lint_preset_sub_presets("zz_comp"), ("zz_lint", "ruff"))
        restored = _fresh_module()
        self.assertNotIn("zz_lint", restored._SIMPLE_LINT_PRESETS)

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


class FileTakingLinterTests(unittest.TestCase):
    """A linter that is handed its files by name rather than walking a directory (issue #289,
    R4-b PR-4: the CUDA compiler driver). `SOURCE_SUFFIXES` on the linter's `lint` module is the
    switch (`_lint_command_over`)."""

    def setUp(self) -> None:
        self.mod = _load_module()
        self.calls: list[list[str]] = []

        def fake_run_command(*, command, **kwargs):
            self.calls.append(list(command))
            return {"ok": True, "command_id": "cid", "return_code": 0, "stdout": "",
                    "stderr": "", "command": list(command)}

        patch = mock.patch.object(self.mod, "_run_command", fake_run_command)
        patch.start()
        self.addCleanup(patch.stop)

    def test_every_lint_backend_states_which_kind_it_is(self) -> None:
        from tools.backends import registry
        for preset in self.mod._SIMPLE_LINT_PRESETS:
            with self.subTest(preset=preset):
                module = registry.capability_module("linter", preset, "lint")
                self.assertTrue(module.SOURCE_SUFFIXES is None
                                or isinstance(module.SOURCE_SUFFIXES, tuple))
        self.assertIsNone(registry.capability_module("linter", "fortitude", "lint").SOURCE_SUFFIXES)
        self.assertEqual((".cu",), registry.capability_module("linter", "nvcc", "lint").SOURCE_SUFFIXES)

    def test_sources_are_found_at_depth_prefixed_and_sorted_and_links_are_not_followed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sub").mkdir()
            for name in ("b.cu", "-o.cu", "@r.cu", "sub/a.CU", "x.f90"):
                (root / name).write_text("")
            (root / "link.cu").symlink_to(root / "b.cu")
            self.assertEqual(["./-o.cu", "./@r.cu", "./b.cu", "./sub/a.CU"],
                             self.mod._lint_source_files(tmp, (".cu",)))
            result = self.mod.tool_run_linter({"preset": "nvcc", "project_dir": tmp})
        from tools.backends import registry
        lint = registry.capability_module("linter", "nvcc", "lint")
        self.assertEqual([list(lint.source_argv(["./-o.cu", "./@r.cu", "./b.cu", "./sub/a.CU"]))],
                         self.calls)
        self.assertEqual("nvcc", result["preset"])

    def test_no_source_is_clean_and_runs_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "Makefile").write_text("")
            result = self.mod.tool_run_linter({"preset": "nvcc", "project_dir": tmp,
                                               "command_log_path": str(Path(tmp) / "log.jsonl")})
            self.assertFalse((Path(tmp) / "log.jsonl").exists())
        self.assertEqual([], self.calls)
        self.assertTrue(result["ok"])
        self.assertEqual(0, result["return_code"])
        self.assertTrue(result["skipped"])

    def test_a_directory_linter_is_handed_the_directory_as_before(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a.cu").write_text("")
            self.mod.tool_run_linter({"preset": "fortitude", "project_dir": tmp})
        self.assertEqual([list(self.mod._LINT_PRESET_COMMANDS["fortitude"])], self.calls)


class BuildExecuteBackendTests(unittest.TestCase):
    """The `make` rows are the make backend's `build_execute` since issue #424 PR-2: the library
    asks the package, so a value there is what a build and a quality check run, and a second
    extracted build system needs no row here."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_module()

    def _execute(self):
        return self.mod._backend_registry().capability_module(
            "build_system", "make", "build_execute")

    def test_the_make_argv_is_the_backend_s(self) -> None:
        execute = self._execute()
        with mock.patch.object(execute, "build_argv",
                               lambda target, jobs, extra: ["zz-make", str(jobs), *extra]):
            self.assertEqual(self.mod.build_command("make", None, 3, ["A=1"]),
                             ["zz-make", "3", "A=1"])
            self.assertEqual(self.mod.build_system_executable("make"), "zz-make")
        # The table answers a build system no backend carries `build_execute` for.
        self.assertEqual(self.mod.build_command("ninja", None, 2, []), ["ninja", "-j2"])

    def test_a_build_system_extracted_later_needs_no_library_row(self) -> None:
        registry = self.mod._backend_registry()
        alias = registry.Backend("build_system", "zz_make_alias",
                                 "tools.backends.build_system.make",
                                 backend_provides=frozenset({"control_file", "build_execute"}))
        with mock.patch.dict(registry._BACKENDS, {("build_system", "zz_make_alias"): alias}):
            self.assertEqual(self.mod.build_command("zz_make_alias", "all", 2, []),
                             ["make", "-j2", "all"])
        # Its presets join the table too — and here they are make's own names, so the
        # composition refuses the second declaration rather than resolving it by order.
        with mock.patch.dict(registry._BACKENDS, {("build_system", "zz_make_alias"): alias}), \
                self.assertRaises(ValueError) as caught:
            self.mod._quality_check_preset_commands()
        self.assertIn("declared by both build_system backend 'make' and build_system backend "
                      "'zz_make_alias'", str(caught.exception))

    def test_the_quality_check_presets_are_composed_from_the_backends(self) -> None:
        execute = self._execute()
        self.assertEqual(
            {k: v for k, v in self.mod._QUALITY_CHECK_PRESET_COMMANDS.items()
             if k in execute.QUALITY_CHECK_COMMANDS},
            {k: tuple(v) for k, v in execute.QUALITY_CHECK_COMMANDS.items()})
        with mock.patch.object(execute, "QUALITY_CHECK_COMMANDS",
                               {"zz_preset": ("make", "zz")}):
            commands = self.mod._quality_check_preset_commands()
        self.assertEqual(commands["zz_preset"], ("make", "zz"))
        self.assertNotIn("make_test", commands)
        self.assertEqual(commands["ctest"], ("ctest", "--output-on-failure"))

    def test_a_preset_declared_twice_refuses_the_table(self) -> None:
        execute = self._execute()
        with mock.patch.object(execute, "QUALITY_CHECK_COMMANDS",
                               {"pytest": ("make", "pytest")}), \
                self.assertRaises(ValueError) as caught:
            self.mod._quality_check_preset_commands()
        self.assertIn("'pytest' is declared by both", str(caught.exception))

if __name__ == "__main__":  # pragma: no cover
    unittest.main()
