"""External libraries (issue #644, the system tier's Part 3, P1).

Rows run over two fixture libraries the test BUILDS with the real compiler, installed under a
non-system directory, as the operator's own installation would be: an archive with a module file
(the vendor shape), and a shared object `libfoo.so.1` that depends on a second shared object in
the same directory (`libbar.so.1`), with a second build of `libfoo.so.1` elsewhere that a
loader path naming it first would load instead (the shape measured in the planning session).

Layers: the declaration and the deps.yaml key (`tools/external_library.py`,
`orchestration_runtime._parse_library_entries`), the site's `libraries:`
(`tools/execution_sites.py`), the compiler capabilities (`library_flags`, `library_binary`) and
the files the identity hashes, a real `make` over the rendered control file with the library
values on its command line, the run's loader path (inherited value first, and an empty
environment), the same build and run under the wrapper's real sandbox, the post-build symbol
checks, the key members, the Build's routing of a symbol violation, and the driver's
`library unavailable` verdict.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tools import external_library as xl
from tools.backends import registry
from tools.backends.build_system.make import control_file as make_control_file
from tools.backends.build_system.make import execute as make_execute
from tools.execution_sites import SitesConfigError, load_sites
from tools.tests.target_fixtures import FORTRAN_CPU

GFORTRAN = shutil.which("gfortran")

_VEND = """\
module vend
  implicit none
contains
  subroutine vend_scale(x, f)
    real(8), intent(inout) :: x(:)
    real(8), intent(in) :: f
    x = x * f
  end subroutine vend_scale
end module vend
"""
_BAR = """\
function bar_twice(x) result(y)
  implicit none
  real(8), intent(in) :: x
  real(8) :: y
  y = 2d0 * x
end function bar_twice
"""
_FOO = """\
function foo_add(x) result(y)
  implicit none
  real(8), intent(in) :: x
  real(8) :: y
  real(8), external :: bar_twice
  y = bar_twice(x) + {offset}
end function foo_add
"""
#: A node model that reaches both libraries.
_MODEL_BOTH = """\
module m_model
  use vend, only: vend_scale
  implicit none
contains
  subroutine m_run()
    real(8) :: a(2)
    real(8), external :: foo_add
    a = [1d0, 2d0]
    call vend_scale(a, 3d0)
    print '(3f8.2)', a, foo_add(1d0)
  end subroutine m_run
end module m_model
"""
_RUNNER = """\
program r_runner
  use m_model, only: m_run
  implicit none
  call m_run()
end program r_runner
"""
#: What the correct libraries print; the shadow build of `libfoo` prints 102.00 for the last.
_EXPECTED = "3.00 6.00 3.00"


def _require_toolchain(case: unittest.TestCase) -> None:
    # Fails rather than skips: GitHub-hosted CI installs every program these rows run
    # (docs/DEVELOPMENT.md, issue #498).
    for program in ("gfortran", "make", "nm", "objdump", "ar"):
        case.assertIsNotNone(shutil.which(program), f"{program} is not installed")


def _run(argv: list[str], cwd: Path, env: dict[str, str] | None = None) -> None:
    proc = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise AssertionError(f"{argv} failed: {proc.stdout}{proc.stderr}")


def build_fixture_libraries(root: Path) -> dict[str, Path]:
    """Build the fixture libraries under `root` and return their directories."""
    src, inc, lib, shadow = root / "src", root / "inst/include", root / "inst/lib", root / "shadow"
    for d in (src, inc, lib, shadow):
        d.mkdir(parents=True, exist_ok=True)
    (src / "vend.f90").write_text(_VEND)
    (src / "bar.f90").write_text(_BAR)
    (src / "foo.f90").write_text(_FOO.format(offset="1d0"))
    (src / "foo_shadow.f90").write_text(_FOO.format(offset="100d0"))
    _run(["gfortran", "-c", "-fPIC", "vend.f90", f"-J{inc}", "-o", "vend.o"], src)
    _run(["ar", "rcs", str(lib / "libvend.a"), "vend.o"], src)
    _run(["gfortran", "-shared", "-fPIC", "bar.f90", "-Wl,-soname,libbar.so.1",
          "-o", str(lib / "libbar.so.1")], src)
    os.symlink("libbar.so.1", lib / "libbar.so")
    _run(["gfortran", "-shared", "-fPIC", "foo.f90", "-Wl,-soname,libfoo.so.1",
          f"-L{lib}", "-lbar", "-o", str(lib / "libfoo.so.1")], src)
    os.symlink("libfoo.so.1", lib / "libfoo.so")
    _run(["gfortran", "-shared", "-fPIC", "foo_shadow.f90", "-Wl,-soname,libfoo.so.1",
          "-o", str(shadow / "libfoo.so.1")], src)
    return {"include": inc, "lib": lib, "shadow": shadow}


def write_declarations(repo: Path) -> None:
    d = repo / xl.LIBRARIES_DIR
    d.mkdir(parents=True, exist_ok=True)
    (d / "vend.yaml").write_text(textwrap.dedent("""\
        library_version: 1
        library_id: vend
        language: fortran
        modules: [vend]
        procedures: []
        link: [vend]
        """))
    (d / "foo.yaml").write_text(textwrap.dedent("""\
        library_version: 1
        library_id: foo
        language: fortran
        modules: []
        procedures: [foo_add]
        link: [foo]
        """))


def write_sites(repo: Path, dirs: dict[str, Path], *, libraries: str | None = None) -> None:
    body = libraries if libraries is not None else (
        f"      vend: {{include: {dirs['include']}, lib: {dirs['lib']}}}\n"
        f"      foo: {{lib: {dirs['lib']}}}\n")
    (repo / "sites.yaml").write_text(
        "sites_version: 1\nsites:\n  local:\n    libraries:\n" + body)


class _Fixture(unittest.TestCase):
    """A temporary repository with the two declarations, the built libraries and a
    `sites.yaml` naming them at the local site."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._td = tempfile.TemporaryDirectory()
        cls.root = Path(cls._td.name)
        cls.repo = cls.root / "repo"
        cls.repo.mkdir()
        cls.dirs = build_fixture_libraries(cls.root / "libs")
        write_declarations(cls.repo)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._td.cleanup()

    def setUp(self) -> None:
        _require_toolchain(self)
        write_sites(self.repo, self.dirs)

    def resolved(self, ids=("vend", "foo")) -> list[xl.ResolvedLibrary]:
        return xl.resolve_closure(self.repo, FORTRAN_CPU,
                                  [xl.load_declaration(self.repo, i) for i in ids])


class DeclarationTests(unittest.TestCase):
    """The declaration's closed shape and the deps.yaml key."""

    def _decl(self, repo: Path, library_id: str, text: str) -> None:
        d = repo / xl.LIBRARIES_DIR
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{library_id}.yaml").write_text(textwrap.dedent(text))

    def test_a_declaration_loads_and_its_hash_is_the_file_s(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            write_declarations(repo)
            decl = xl.load_declaration(repo, "vend")
            self.assertEqual((decl.language, decl.modules, decl.procedures, decl.link),
                             ("fortran", ("vend",), (), ("vend",)))
            import hashlib
            self.assertEqual(decl.sha256, hashlib.sha256(
                xl.declaration_path(repo, "vend").read_bytes()).hexdigest())
            self.assertIsNone(xl.declaration_violation(repo, ["vend", "foo"]))

    def test_each_shape_defect_is_refused(self) -> None:
        good = ("library_version: 1\nlibrary_id: x\nlanguage: fortran\nmodules: []\n"
                "procedures: []\nlink: [x]\n")
        cases = {
            "unknown key": good + "extra: 1\n",
            "missing key": good.replace("procedures: []\n", ""),
            "version": good.replace("library_version: 1", "library_version: 2"),
            "id is not the file's": good.replace("library_id: x", "library_id: y"),
            "undeclared language": good.replace("language: fortran", "language: cobol"),
            "empty link": good.replace("link: [x]", "link: []"),
            "duplicate module": good.replace("modules: []", "modules: [a, a]"),
            "not a mapping": "- 1\n",
        }
        for name, text in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as td:
                self._decl(Path(td), "x", text)
                with self.assertRaises(xl.LibraryDeclarationError):
                    xl.load_declaration(Path(td), "x")
                self.assertIn("library_declaration_invalid",
                              xl.declaration_violation(Path(td), ["x"]) or "")
        with tempfile.TemporaryDirectory() as td:
            self.assertIn("cannot be read", xl.declaration_violation(Path(td), ["absent"]))

    def test_the_deps_key_is_read_and_skipped_by_the_node_parse(self) -> None:
        from tools.orchestration_runtime import _parse_dep_entries, _parse_library_entries
        base = {"components": [], "profiles": []}
        doc = {"dependencies": {**base, "libraries": [{"library_id": "vend"},
                                                     {"library_id": "foo"}]}}
        self.assertEqual(_parse_library_entries(doc), (["vend", "foo"], True))
        self.assertEqual(_parse_dep_entries(doc), ([], True))
        self.assertEqual(_parse_library_entries({"dependencies": base}), ([], True))
        for bad in ([{"library_id": "a"}, {"library_id": "a"}], ["a"],
                    [{"library_id": "a", "x": 1}], [{"library_id": "../a"}], {"a": 1}):
            with self.subTest(bad=bad):
                self.assertFalse(_parse_library_entries(
                    {"dependencies": {**base, "libraries": bad}})[1])

    def test_a_missing_deps_yaml_declares_none_and_a_malformed_list_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            self.assertEqual(xl.node_library_ids(repo, "spec/component/x"), [])
            spec = repo / "spec/component/x"
            spec.mkdir(parents=True)
            (spec / "deps.yaml").write_text(
                "dependencies:\n  components: []\n  profiles: []\n  libraries: [vend]\n")
            with self.assertRaises(xl.LibraryDeclarationError):
                xl.node_library_ids(repo, "spec/component/x")


class SitesTests(unittest.TestCase):

    def _load(self, text: str):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "sites.yaml").write_text(text)
            return load_sites(Path(td))

    def test_a_site_lists_its_libraries(self) -> None:
        config = self._load(textwrap.dedent("""\
            sites_version: 1
            sites:
              local:
                libraries:
                  vend: {include: /opt/v/include, lib: /opt/v/lib}
                  lapack:
              box:
                host: box
                workdir: /w
                executes: [cpu]
                scheduler: none
                libraries:
                  foo: {lib: /x}
            """))
        local = config.sites["local"]
        self.assertEqual(local.library("vend"), xl_site("/opt/v/include", "/opt/v/lib"))
        self.assertEqual(local.library("lapack"), xl_site(None, None))
        self.assertIsNone(local.library("foo"))
        self.assertEqual(local.executes, ("cpu",))
        self.assertEqual(config.sites["box"].library("foo"), xl_site(None, "/x"))

    def test_an_unknown_library_key_is_refused(self) -> None:
        with self.assertRaises(SitesConfigError) as cm:
            self._load("sites_version: 1\nsites:\n  local:\n    libraries:\n"
                       "      vend: {libdir: /x}\n")
        self.assertEqual(cm.exception.rule, "sites_config_unknown_key")


def xl_site(include: str | None, lib: str | None):
    from tools.execution_sites import SiteLibrary
    return SiteLibrary(include=include, lib=lib)


class ResolveTests(_Fixture):
    """Which files a library resolves to at the site, and what its identity hashes."""

    def test_the_archive_with_its_module_and_the_shared_object_with_its_load_name(self) -> None:
        vend, foo = self.resolved()
        self.assertEqual(vend.link_files, (("vend", str(self.dirs["lib"] / "libvend.a")),))
        self.assertEqual(vend.soname_files, ())
        self.assertEqual(vend.module_files, (("vend", str(self.dirs["include"] / "vend.mod")),))
        self.assertEqual(foo.link_files, (("foo", str(self.dirs["lib"] / "libfoo.so")),))
        self.assertEqual(foo.soname_files,
                         (("libfoo.so.1", str(self.dirs["lib"] / "libfoo.so.1")),))
        self.assertEqual(xl.library_dirs([vend, foo]), (str(self.dirs["lib"]),))
        self.assertEqual(xl.include_dirs([vend, foo]), (str(self.dirs["include"]),))

    def test_a_library_found_by_the_compiler_s_own_search(self) -> None:
        write_sites(self.repo, self.dirs, libraries="      foo:\n")
        with mock.patch.dict(os.environ, {"LIBRARY_PATH": str(self.dirs["lib"])}):
            (foo,) = self.resolved(("foo",))
        self.assertEqual(Path(foo.link_files[0][1]), self.dirs["lib"] / "libfoo.so")
        self.assertEqual(foo.lib_dir, str(self.dirs["lib"]))
        with mock.patch.dict(os.environ, {"LIBRARY_PATH": ""}):
            with self.assertRaises(xl.LibraryUnresolved) as cm:
                self.resolved(("foo",))
        self.assertEqual(cm.exception.reason, xl.SITE_UNRESOLVED)

    def test_each_missing_file_is_site_library_unresolved(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td) / "lib"
            shutil.copytree(self.dirs["lib"], lib, symlinks=True)
            # The link file stays (a copy, not a link to it), its load name goes.
            (lib / "libfoo.so").unlink()
            shutil.copy2(lib / "libfoo.so.1", lib / "libfoo.so")
            (lib / "libfoo.so.1").unlink()
            write_sites(self.repo, self.dirs, libraries=f"      foo: {{lib: {lib}}}\n")
            with self.assertRaisesRegex(xl.LibraryUnresolved, "loaded as 'libfoo.so.1'"):
                self.resolved(("foo",))
        write_sites(self.repo, self.dirs, libraries=f"      vend: {{lib: {self.dirs['lib']}}}\n")
        with self.assertRaisesRegex(xl.LibraryUnresolved, "module 'vend'"):
            self.resolved(("vend",))

    def test_the_identity_moves_with_each_declared_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lib, inc = Path(td) / "lib", Path(td) / "include"
            shutil.copytree(self.dirs["lib"], lib, symlinks=True)
            shutil.copytree(self.dirs["include"], inc)
            write_sites(self.repo, self.dirs, libraries=(
                f"      vend: {{include: {inc}, lib: {lib}}}\n      foo: {{lib: {lib}}}\n"))
            before = xl.identity(self.resolved())
            self.assertEqual(before, xl.identity(self.resolved()))
            for name, path in (("archive", lib / "libvend.a"),
                               ("load-name file", lib / "libfoo.so.1"),
                               ("module file", inc / "vend.mod")):
                with self.subTest(name):
                    original = path.read_bytes()
                    path.write_bytes(original + b"\0")
                    self.assertNotEqual(before, xl.identity(self.resolved()))
                    path.write_bytes(original)
                    self.assertEqual(before, xl.identity(self.resolved()))
        decls = [xl.load_declaration(self.repo, i) for i in ("vend", "foo")]
        self.assertEqual(xl.declared_identity(decls), [
            {k: v for k, v in entry.items() if k != "files"} for entry in before])

    def test_unavailable_and_refused_closures(self) -> None:
        decls = [xl.load_declaration(self.repo, "foo")]
        write_sites(self.repo, self.dirs, libraries="      vend:\n")
        with self.assertRaises(xl.LibraryUnusable) as cm:
            xl.resolve_closure(self.repo, FORTRAN_CPU, decls)
        self.assertEqual(cm.exception.reason, "library_not_at_site")
        from tools.target_profile import load_target_profile
        gpu = load_target_profile(Path(__file__).resolve().parents[2], "cpp_gpu")
        with self.assertRaises(xl.LibraryUnusable) as cm:
            xl.resolve_closure(self.repo, gpu, decls)
        self.assertEqual(cm.exception.reason, "library_language_mismatch")
        self.assertEqual(xl.resolve_closure(self.repo, FORTRAN_CPU, []), [])

    def test_the_words_a_build_is_handed_and_the_run_s_loader_path(self) -> None:
        resolved = self.resolved()
        lib, inc = self.dirs["lib"], self.dirs["include"]
        self.assertEqual(xl.make_values("gfortran", resolved), (
            f"-I{inc}", f"{lib}/libvend.a {lib}/libfoo.so -Wl,-rpath-link,{lib}"))
        self.assertEqual(xl.make_values("gfortran", []), ("", ""))
        self.assertEqual(make_execute.library_overrides("a b", "c"), ("LIBINC=a b", "LIBS=c"))
        self.assertEqual(xl.loader_env(FORTRAN_CPU, [str(lib)], {"LD_LIBRARY_PATH": "/x:/y"}),
                         {"LD_LIBRARY_PATH": f"{lib}:/x:/y"})
        # No separator before an empty inherited value: the loader reads it as the cwd.
        self.assertEqual(xl.loader_env(FORTRAN_CPU, [str(lib)], {"LD_LIBRARY_PATH": ""}),
                         {"LD_LIBRARY_PATH": str(lib)})
        self.assertEqual(xl.loader_env(FORTRAN_CPU, [str(lib)], {}), {"LD_LIBRARY_PATH": str(lib)})
        self.assertEqual(xl.loader_env(FORTRAN_CPU, [], {"LD_LIBRARY_PATH": "/x"}), {})
        self.assertIn(os.path.realpath(lib), xl.bind_roots(resolved))
        self.assertEqual(xl.built_library_dirs({"library_dirs": [str(lib)]}), (str(lib),))
        self.assertEqual(xl.built_library_dirs({}), ())


class BuildAndRunTests(_Fixture):
    """The rendered control file built by a real `make` with the library values on its command
    line, and the binary run with the loader path the host composes."""

    def _build(self, work: Path) -> Path:
        from tools.backends.language.fortran import control_file as fortran_rules
        src = work / "src"
        src.mkdir(parents=True)
        (src / "m_model.f90").write_text(_MODEL_BOTH)
        (src / "r_runner.f90").write_text(_RUNNER)
        (src / "Makefile").write_text(make_control_file.render_node(
            rules=fortran_rules.rules(standard="f2008", parallel_backend="openmp"),
            compiler="gfortran", bin_name="r_runner", cases_default="c", model_stem="m_model",
            runner_stem="r_runner", closure=[]))
        from tools.build_runtime import tool_compile_project
        result = tool_compile_project({
            "project_dir": str(src), "language": "fortran", "build_system": "make",
            "extra_args": [*make_execute.library_overrides(*xl.make_values(
                "gfortran", self.resolved())),
                *make_execute.build_overrides(str(work / "obj"), str(work / "bin"), "r_runner")],
            "env": {"PATH": os.environ["PATH"]},
            "command_log_path": str(work / "log.jsonl")})
        self.assertTrue(result["ok"], result.get("stderr"))
        return work / "bin" / "r_runner"

    def _output(self, binary: Path, env: dict[str, str]) -> str:
        proc = subprocess.run([str(binary)], env=env, capture_output=True, text=True,
                              check=False)
        return " ".join((proc.stdout + proc.stderr).split()) + f" rc={proc.returncode}"

    def test_the_binary_loads_the_libraries_the_site_names_ahead_of_another_build(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            binary = self._build(Path(td))
            lib = str(self.dirs["lib"])
            shadow_first = {"LD_LIBRARY_PATH": str(self.dirs["shadow"])}
            # Without the prefix the inherited path loads the other build (it lacks bar).
            self.assertNotIn(_EXPECTED, self._output(binary, shadow_first))
            self.assertEqual(self._output(binary, {
                **shadow_first, **xl.loader_env(FORTRAN_CPU, [lib], shadow_first)}),
                f"{_EXPECTED} rc=0")
            self.assertEqual(self._output(binary, xl.loader_env(FORTRAN_CPU, [lib], {})),
                             f"{_EXPECTED} rc=0")

    def test_the_build_and_run_under_the_wrapper_s_sandbox_with_a_cleared_environment(
            self) -> None:
        from tools.orchestration_runtime import render_wrapper_sandbox_prefix
        from tools.tests.test_bwrap_simulation import _bwrap_usable
        self.assertTrue(_bwrap_usable(), "bwrap is absent or unprivileged user namespaces "
                        "are disabled; see docs/BWRAP_ENABLEMENT.md")
        resolved = self.resolved()
        repo_root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            src = work / "src"
            src.mkdir()
            (src / "m_model.f90").write_text(_MODEL_BOTH)
            (src / "r_runner.f90").write_text(_RUNNER)
            inc_value, link_value = xl.make_values("gfortran", resolved)
            script = (f"gfortran -I{work}/obj {inc_value} -J{work}/obj -c m_model.f90 "
                      f"-o {work}/obj/m.o && gfortran -I{work}/obj -c r_runner.f90 "
                      f"-o {work}/obj/r.o && gfortran {work}/obj/m.o {work}/obj/r.o "
                      f"{link_value} -o {work}/obj/x && {work}/obj/x")
            (work / "obj").mkdir()
            env = {"PATH": os.environ["PATH"], "HOME": "/tmp", "TMPDIR": "/tmp",
                   **xl.loader_env(FORTRAN_CPU, xl.library_dirs(resolved), {})}
            prefix = render_wrapper_sandbox_prefix(
                repo_root=repo_root, project_dir=src, rw_binds=[work],
                toolchain_ro_roots=xl.bind_roots(resolved), env=env,
                declared_env_names=("LD_LIBRARY_PATH",))
            proc = subprocess.run([*prefix, "sh", "-c", script], cwd=src, capture_output=True,
                                  text=True, check=False)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual(" ".join(proc.stdout.split()), _EXPECTED)
            # Without the bound directories the library is not reachable in the sandbox.
            unbound = render_wrapper_sandbox_prefix(
                repo_root=repo_root, project_dir=src, rw_binds=[work], env=env,
                declared_env_names=("LD_LIBRARY_PATH",))
            proc = subprocess.run([*unbound, "sh", "-c", f"{work}/obj/x"], cwd=src,
                                  capture_output=True, text=True, check=False)
            self.assertNotEqual(proc.returncode, 0)


class ControlFileAndSyntaxTests(_Fixture):
    """The graph-rendered control file (the one a bundle's Build reads) and the syntax stage
    reach the libraries through the same values."""

    def test_the_graph_rendered_control_file_carries_both_variables(self) -> None:
        from tools.backends.language.fortran import control_file as fortran_rules
        text = make_control_file.render_from_graph(
            rules=fortran_rules.rules(standard="f2008", parallel_backend="openmp"),
            compiler="gfortran", bin_name="x", cases_default="c",
            graph={"compile_units": [{"source": "bundle:m.f90", "object": "m.o"}],
                   "link": {"objects": ["m.o"]}})
        lines = text.splitlines()
        self.assertIn("LIBINC  ?=", lines)
        self.assertIn("LIBS    ?=", lines)
        self.assertIn("\t$(FC) $(FFLAGS) $(OBJDIR)/m.o -o $(BINDIR)/$(BIN) $(LIBS)", lines)
        self.assertIn("$(LIBINC)", next(l for l in lines if l.startswith("FFLAGS")))
        node = make_control_file.render_node(
            rules=fortran_rules.rules(standard="f2008", parallel_backend="openmp"),
            compiler="gfortran", bin_name="x", cases_default="c", model_stem="m",
            runner_stem="r", closure=[]).splitlines()
        self.assertIn("LIBINC  ?=", node)
        self.assertIn("LIBS    ?=", node)

    def test_the_syntax_stage_finds_a_library_module_through_include_dirs(self) -> None:
        from tools.build_runtime import tool_run_syntax_check
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "m_model.f90").write_text(_MODEL_BOTH)

            def check(include_dirs: list[str] | None) -> dict:
                args = {"compiler": "gfortran", "std": "f2008", "project_dir": td,
                        "command_log_path": str(Path(td) / "log.jsonl")}
                if include_dirs is not None:
                    args["include_dirs"] = include_dirs
                return tool_run_syntax_check(args)

            self.assertFalse(check(None)["ok"])
            ok = check([str(self.dirs["include"])])
            self.assertTrue(ok["ok"], ok.get("stderr"))
            for bad in ("a", [1]):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    check(bad)  # type: ignore[arg-type]
        from tools.backends.compiler.nvcc import syntax as nvcc_syntax
        with self.assertRaises(ValueError):
            nvcc_syntax.argv(standard="c++17", scratch_dir=".s", parallel_backend=None,
                             promotions=(), architecture=None, sources=["a.cu"],
                             include_dirs=("/x",))


class SymbolCheckTests(_Fixture):
    """The post-build checks over the objects a node's leaf authored."""

    def _object(self, work: Path, name: str, text: str) -> Path:
        (work / f"{name}.f90").write_text(text)
        _run(["gfortran", f"-I{self.dirs['include']}", "-c", f"{name}.f90", "-o", f"{name}.o"],
             work)
        return work / f"{name}.o"

    def _check(self, objects: list[Path], declared: list[str]) -> tuple[dict, str | None]:
        return xl.symbol_check(FORTRAN_CPU, objects, self.resolved(), declared)

    def test_a_node_that_declares_and_calls_both_passes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            obj = self._object(Path(td), "m_model", _MODEL_BOTH)
            record, violation = self._check([obj], ["vend", "foo"])
            self.assertIsNone(violation)
            self.assertEqual(record["violations"], [])
            self.assertEqual(record["objects"], ["m_model.o"])

    def test_an_import_of_an_undeclared_library_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            obj = self._object(Path(td), "m_model", _MODEL_BOTH)
            _record, violation = self._check([obj], ["vend"])
            self.assertIn("library_symbol_not_declared", violation or "")
            self.assertIn("foo_add_", violation or "")
            self.assertNotIn("library_declared_unused", violation or "")

    def test_a_definition_of_a_library_s_symbol_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            obj = self._object(Path(td), "own", _FOO.format(offset="5d0").replace(
                "real(8), external :: bar_twice\n", "").replace("bar_twice(x)", "x"))
            _record, violation = self._check([obj], [])
            self.assertIn("library_symbol_interposed", violation or "")
            self.assertNotIn("library_symbol_not_declared", violation or "")

    def test_a_declared_library_no_object_references_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            obj = self._object(Path(td), "plain", "subroutine plain()\nend subroutine plain\n")
            _record, violation = self._check([obj], ["vend", "foo"])
            self.assertEqual((violation or "").count("library_declared_unused"), 2)


class BuildRoutingTests(_Fixture):
    """`Conductor._build_inproc` hands the library values to make, records the directories,
    and routes a symbol violation of the node's own objects to Generate."""

    def _build(self, declared: tuple[str, ...]) -> tuple[dict, list[dict]]:
        import tools.workflow_conductor as wc
        from tools.tests.llm_samples import sample_config_with
        from tools.tests.test_workflow_conductor import _build_runtime, _TargetedConductor
        build_runtime = _build_runtime()
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            write_declarations(repo)
            write_sites(repo, self.dirs)
            spec = repo / "spec/component/spec_x"
            spec.mkdir(parents=True)
            (spec / "deps.yaml").write_text(
                "dependencies:\n  components: []\n  profiles: []\n  libraries:\n"
                + "".join(f"    - {{library_id: {i}}}\n" for i in declared))
            c = _TargetedConductor(repo_root=repo, orchestration_id="t",
                                   orchestration_agent_run_id="x",
                                   llm_config=sample_config_with("claude"), env={},
                                   target_profile=FORTRAN_CPU)
            refs = wc.NodeRefs(target_id=FORTRAN_CPU.target_id,
                               node_key="component/spec_x@0.1.0",
                               spec_path="spec/component/spec_x", ir_id="x_1",
                               pipeline_id="x_1", source_id="src_1", binary_id="bin_1")
            (repo / refs.ir_ref).mkdir(parents=True, exist_ok=True)
            (repo / refs.source_dir() / "src").mkdir(parents=True, exist_ok=True)
            calls: list[dict] = []

            def fake_compile(args):
                calls.append(args)
                overrides = dict(a.split("=", 1) for a in args["extra_args"])
                obj, bin_dir = Path(overrides["OBJDIR"]), Path(overrides["BINDIR"])
                obj.mkdir(parents=True, exist_ok=True)
                (obj / "spec_x_model.f90").write_text(_MODEL_BOTH)
                _run(["gfortran", *overrides["LIBINC"].split(), "-c", "spec_x_model.f90",
                      "-o", "spec_x_model.o"], obj)
                bin_dir.mkdir(parents=True, exist_ok=True)
                (bin_dir / overrides["BIN"]).write_text("bin\n")
                return {"ok": True, "return_code": 0, "command_id": f"cid{len(calls)}",
                        "command": ["make", *args["extra_args"]], "stderr": ""}

            real_run = subprocess.run

            def run_but_the_post_build_validator(argv, *a, **k):
                # The post_build validator subprocess is not this row's subject; every other
                # program (the compiler, the symbol-table readers) runs for real.
                if any("validate_pipeline_semantics" in str(w) for w in argv):
                    return subprocess.CompletedProcess(argv, 0, "", "")
                return real_run(argv, *a, **k)

            with mock.patch.object(build_runtime, "tool_compile_project", fake_compile), \
                    mock.patch.object(wc.Conductor, "_dependency_closure_nodes",
                                      lambda self_, refs_: []), \
                    mock.patch.object(wc.Conductor, "_stage_dependency_sources",
                                      lambda self_, refs_, obj_dir, *, phase: []), \
                    mock.patch.object(wc.Conductor, "_closure_libraries",
                                      lambda self_, refs_: self.resolved()), \
                    mock.patch.object(subprocess, "run", run_but_the_post_build_validator):
                out = c._build_inproc(refs, "child-1")
            self.assertEqual(out["returncode"], 0)
            meta = json.loads((repo / refs.binary_dir() / "binary_meta.json").read_text())
        return meta, calls

    def test_a_node_that_declares_what_it_calls_passes_and_records_the_directories(
            self) -> None:
        meta, calls = self._build(("vend", "foo"))
        self.assertEqual(meta["verification_status"], "pass", meta.get("failure_excerpt"))
        inc_value, link_value = xl.make_values("gfortran", self.resolved())
        for call in calls:   # the build and the checked build alike, before the overrides
            self.assertEqual(call["extra_args"][:2], [f"LIBINC={inc_value}", f"LIBS={link_value}"])
        self.assertEqual(meta["library_dirs"], [str(self.dirs["lib"])])
        self.assertEqual(meta["library_check"]["violations"], [])
        self.assertEqual(meta["library_check"]["objects"], ["spec_x_model.o"])

    def test_an_undeclared_import_fails_the_build_toward_generate(self) -> None:
        # `foo` is on the link line (a dependency's declaration, `_closure_libraries` above)
        # and the node itself declares only `vend`.
        import tools.workflow_conductor as wc
        meta, _calls = self._build(("vend",))
        self.assertEqual(meta["verification_status"], "fail")
        self.assertEqual(meta["failure_category"], "validate_post_build_violation")
        self.assertIn("library_symbol_not_declared", meta["failure_excerpt"])
        self.assertEqual(wc.BUILD_FAILURE_ROUTING["validate_post_build_violation"][0],
                         "generate")


class LeafFacingTests(_Fixture):
    """What a leaf of a declaring node is told: the `libraries` list of
    `dependency_surface.json` and the `## External libraries` block of its `DEPENDENCIES.md`
    seed."""

    def _conductor(self, declared: tuple[str, ...]):
        import tools.workflow_conductor as wc
        from types import SimpleNamespace
        from tools.tests.llm_samples import sample_config_with
        from tools.tests.test_workflow_conductor import _TargetedConductor
        spec = self.repo / "spec/component/spec_x"
        spec.mkdir(parents=True, exist_ok=True)
        (spec / "deps.yaml").write_text(
            "dependencies:\n  components: []\n  profiles: []\n  libraries:"
            + ("".join(f"\n    - {{library_id: {i}}}" for i in declared) or " []") + "\n")
        c = _TargetedConductor(repo_root=self.repo, orchestration_id="t",
                               orchestration_agent_run_id="x",
                               llm_config=sample_config_with("claude"), env={},
                               target_profile=FORTRAN_CPU)
        refs = wc.NodeRefs(target_id=FORTRAN_CPU.target_id, node_key="component/spec_x@0.1.0",
                           spec_path="spec/component/spec_x", ir_id="x_1", pipeline_id="x_1")
        spec_ns = SimpleNamespace(workshop_seeds_host_sources=False, workshop_seed_expanders={},
                                  workshop_document_seeds={}, workshop_outputs=())
        return c, refs, spec_ns

    def test_the_dependency_surface_lists_the_node_s_libraries(self) -> None:
        import tools.orchestration_runtime as ort
        for declared in (("vend", "foo"), ()):
            with self.subTest(declared=declared):
                c, refs, _spec = self._conductor(declared)
                (self.repo / refs.ir_ref).mkdir(parents=True, exist_ok=True)
                (self.repo / refs.ir_ref / "dependency_graph.json").write_text("{}")
                with mock.patch.object(ort, "_resolve_component_dep_surface", return_value=[]):
                    c._write_dependency_surface(refs)
                doc = json.loads((self.repo / refs.ir_ref / "dependency_surface.json").read_text())
                if declared:
                    self.assertEqual(doc["libraries"], [
                        {"library_id": "vend", "modules": ["vend"], "procedures": []},
                        {"library_id": "foo", "modules": [], "procedures": ["foo_add"]}])
                else:
                    self.assertNotIn("libraries", doc)

    def test_the_dependencies_seed_carries_the_block(self) -> None:
        c, refs, spec = self._conductor(("vend", "foo"))
        with mock.patch.object(type(c), "_workshop_spec_reads", lambda self_, s: False):
            seeds = c._workshop_seeds(refs, {}, {"step": "validate", "substep": "judge"}, spec)
        text = seeds["DEPENDENCIES.md"]
        self.assertTrue(text.startswith("## External libraries"), text)
        self.assertIn("- vend: modules [vend]; procedures []", text)
        self.assertIn("- foo: modules []; procedures [foo_add]", text)
        c, refs, spec = self._conductor(())
        with mock.patch.object(type(c), "_workshop_spec_reads", lambda self_, s: False):
            self.assertNotIn("DEPENDENCIES.md", c._workshop_seeds(
                refs, {}, {"step": "validate", "substep": "judge"}, spec))


class ExecuteLoaderPathTests(unittest.TestCase):
    """`Validate.execute` gives every run of the binary — the run, the quality check, each sweep
    shape and the checked row — the Build's recorded library directories ahead of the inherited
    loader path, and records the directories."""

    def test_every_run_is_given_the_build_s_library_directories(self) -> None:
        import tools.workflow_conductor as wc
        from tools.tests.llm_samples import sample_config_with
        from tools.tests.test_workflow_conductor import (
            _CLEAN_C_ALPHA_IR, _build_runtime, _record_build_site, _TargetedConductor,
            _write_c_alpha_captures)
        build_runtime = _build_runtime()
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            c = _TargetedConductor(repo_root=repo, orchestration_id="t",
                                   orchestration_agent_run_id="x",
                                   llm_config=sample_config_with("claude"), env={},
                                   target_profile=FORTRAN_CPU)
            refs = wc.NodeRefs(target_id=FORTRAN_CPU.target_id,
                               node_key="component/spec_x@0.1.0",
                               spec_path="spec/component/spec_x", ir_id="x_1",
                               pipeline_id="x_1", source_id="src_1", binary_id="bin_1",
                               run_id="run_1", source_binary_id="bin_1")
            (repo / refs.ir_ref).mkdir(parents=True, exist_ok=True)
            (repo / refs.ir_ref / "spec.ir.yaml").write_text(_CLEAN_C_ALPHA_IR)
            (repo / refs.source_dir() / "src").mkdir(parents=True, exist_ok=True)
            envs: list[dict] = []

            def fake_run_program(args):
                envs.append(dict(args["env"]))
                _write_c_alpha_captures(Path(args["project_dir"]))
                (Path(args["project_dir"]) / "perf.json").write_text("{}")
                return {"ok": True, "command_id": "R"}

            def fake_qc(args):
                envs.append(dict(args["env"]))
                Path(args["env"]["RUNDIR"]).mkdir(parents=True, exist_ok=True)
                _write_c_alpha_captures(Path(args["env"]["RUNDIR"]))
                return {"ok": True, "command_id": "Q"}

            _record_build_site(repo, refs)
            meta = repo / refs.binary_dir("bin_1") / "binary_meta.json"
            meta.write_text(json.dumps({**json.loads(meta.read_text()),
                                        "library_dirs": ["/opt/zz/lib"]}))
            with mock.patch.object(build_runtime, "tool_run_program", fake_run_program), \
                    mock.patch.object(build_runtime, "tool_run_quality_checks", fake_qc), \
                    mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": "/inherited"}), \
                    mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
                        [], 0, "", "")):
                self.assertEqual(c._execute_inproc(refs, "child-1")["returncode"], 0)
            trial = json.loads((repo / refs.run_node_dir() / "trial_meta.json").read_text())
        # run, quality check, two sweep shapes, the checked row.
        self.assertEqual(len(envs), 5)
        for env in envs:
            self.assertEqual(env.get("LD_LIBRARY_PATH"), "/opt/zz/lib:/inherited", env)
        self.assertEqual(trial["environment"]["library_dirs"], ["/opt/zz/lib"])
        self.assertNotIn("LD_LIBRARY_PATH", json.dumps(trial["environment"]["launch"]))


class KeyMemberTests(_Fixture):
    """The build key's `libraries` member, its comparand form, and the validate key's copy."""

    def _member(self, *, probe: bool, comparand: bool = False, declared=("vend", "foo")):
        from tools.orchestration_runtime import DerivationResolver, _closure_libraries_member
        spec = self.repo / "spec/component/n"
        spec.mkdir(parents=True, exist_ok=True)
        (spec / "deps.yaml").write_text(
            "dependencies:\n  components: []\n  profiles: []\n  libraries:"
            + ("".join(f"\n    - {{library_id: {i}}}" for i in declared) or " []") + "\n")
        resolver = DerivationResolver(self.repo, target=FORTRAN_CPU, comparand=comparand,
                                      spec_refs={"component/n@0.1.0": "spec/component/n"})
        return _closure_libraries_member(self.repo, "component/n@0.1.0", [], resolver,
                                         probe=probe)

    def test_present_only_when_declared_and_probed_or_declared_form(self) -> None:
        self.assertEqual(self._member(probe=True), xl.identity(self.resolved()))
        declared = self._member(probe=False)
        self.assertEqual(declared, xl.declared_identity(
            [xl.load_declaration(self.repo, i) for i in ("vend", "foo")]))
        self.assertEqual(self._member(probe=True, comparand=True), declared)
        self.assertIsNone(self._member(probe=True, declared=()))

    def test_the_compile_and_generate_keys_carry_the_node_s_own_declarations(self) -> None:
        import tools.orchestration_runtime as ort
        from tools.orchestration_runtime import DerivationResolver

        def inputs(step: str, declared: tuple[str, ...]) -> dict:
            self._member(probe=False, declared=declared)   # writes the deps.yaml
            resolver = DerivationResolver(self.repo, target=FORTRAN_CPU,
                                          spec_refs={"component/n@0.1.0": "spec/component/n"})
            with mock.patch.object(ort, "_derived_closure_graph", return_value={}), \
                    mock.patch.object(ort, "_closure_signature", return_value=[]), \
                    mock.patch.object(ort, "_spec_file_hash", return_value="t"), \
                    mock.patch.object(ort, "_component_direct_deps", return_value=[]), \
                    mock.patch.object(ort, "_certified_output_hash", return_value="h"), \
                    mock.patch.object(ort, "_pipeline_closure_for_key", return_value=[]), \
                    mock.patch.object(ort, "_harness_for_key", return_value="h@1"), \
                    mock.patch("tools.codegen_bundle.harness_capability_manifest_document_for",
                               return_value={}):
                return ort.phase_derivation_inputs(
                    self.repo, node_key="component/n@0.1.0", step=step,
                    spec_ref="spec/component/n", ir_ref="workspace/i", resolver=resolver)

        want = xl.declared_identity([xl.load_declaration(self.repo, "foo")])
        for step in ("compile", "generate"):
            with self.subTest(step=step):
                self.assertEqual(inputs(step, ("foo",))["libraries"], want)
                self.assertNotIn("libraries", inputs(step, ()))

    def test_a_site_that_cannot_serve_it_is_unresolvable(self) -> None:
        from tools.orchestration_runtime import DerivationInputsUnresolvable
        write_sites(self.repo, self.dirs, libraries="      vend:\n")
        with self.assertRaisesRegex(DerivationInputsUnresolvable, "library_not_at_site"):
            self._member(probe=True)

    def test_a_comparand_reads_the_files_off_the_candidate_s_stamp(self) -> None:
        from tools.orchestration_runtime import (
            _comparand_substituted_inputs, _host_probed_leaves, _target_toolchain_identity)
        probed = self._member(probe=True)
        declared = self._member(probe=False)
        toolchain = _target_toolchain_identity(FORTRAN_CPU, probe=False)
        leaves = {leaf: "v" for leaf in _host_probed_leaves(FORTRAN_CPU)}
        inputs = {"source": "s", "toolchain": toolchain, "libraries": declared}
        stamp = {"derivation_inputs": {"toolchain": {**toolchain, **leaves},
                                       "libraries": probed}}
        out = _comparand_substituted_inputs(stamp, inputs, FORTRAN_CPU)
        self.assertEqual(out["libraries"], probed)
        for broken in ([], probed[:1], [{**probed[0], "library_id": "zz"}, probed[1]],
                       [{**probed[0], "declaration_sha256": "0"}, probed[1]]):
            with self.subTest(broken=broken):
                self.assertIsNone(_comparand_substituted_inputs(
                    {"derivation_inputs": {"toolchain": {**toolchain, **leaves},
                                           "libraries": broken}}, inputs, FORTRAN_CPU))
        no_libs = {"derivation_inputs": {"toolchain": {**toolchain, **leaves}}}
        self.assertIsNone(_comparand_substituted_inputs(no_libs, inputs, FORTRAN_CPU))
        self.assertNotIn("libraries", _comparand_substituted_inputs(
            no_libs, {k: v for k, v in inputs.items() if k != "libraries"}, FORTRAN_CPU))

    def test_the_validate_key_copies_the_build_stamp_s_member(self) -> None:
        import tools.orchestration_runtime as ort
        self._member(probe=False)   # writes the node's deps.yaml
        binary = self.repo / "workspace/b"
        binary.mkdir(parents=True, exist_ok=True)
        probed = self._member(probe=True)

        def inputs(stamped: Any) -> dict:
            (binary / "binary_meta.json").write_text(json.dumps(
                {"derivation_inputs": {} if stamped is None else {"libraries": stamped}}))
            with mock.patch.object(ort, "_certified_output_hash", return_value="h"), \
                    mock.patch.object(ort, "_spec_file_hash", return_value="t"), \
                    mock.patch.object(ort, "_pipeline_closure_for_key", return_value=[]):
                return ort.phase_derivation_inputs(
                    self.repo, node_key="component/n@0.1.0", step="validate",
                    spec_ref="spec/component/n", ir_ref="workspace/i",
                    binary_ref="workspace/b",
                    resolver=ort.DerivationResolver(
                        self.repo, target=FORTRAN_CPU,
                        spec_refs={"component/n@0.1.0": "spec/component/n"}))

        self.assertEqual(inputs(probed)["libraries"], probed)
        # A library swapped under an unchanged binary moves the validate key.
        swapped = json.loads(json.dumps(probed))
        swapped[1]["files"]["soname"]["libfoo.so.1"] = "0" * 64
        self.assertNotEqual(ort._derivation_key("validate", inputs(probed)),
                            ort._derivation_key("validate", inputs(swapped)))
        with self.assertRaises(ort.DerivationInputsUnresolvable):
            inputs(None)


class LibraryVerdictTests(_Fixture):
    """The driver's decision before any phase (`run_workflow._library_verdict`)."""

    def _verdict(self, until: str = "Validate", **closure: Any) -> dict | None:
        from tools import run_workflow
        spec = self.repo / "spec/component/n"
        spec.mkdir(parents=True, exist_ok=True)
        (spec / "deps.yaml").write_text(
            "dependencies:\n  components: []\n  profiles: []\n  libraries:\n"
            "    - {library_id: foo}\n")
        with mock.patch.object(run_workflow, "_resolve_dependency_closure",
                               return_value=([], None)):
            return run_workflow._library_verdict(self.repo, "spec/component/n", FORTRAN_CPU,
                                                 until)

    def test_a_bad_declaration_is_refused_on_every_run_a_compile_only_one_included(
            self) -> None:
        from tools import run_workflow
        with mock.patch.object(run_workflow, "_resolve_dependency_closure", return_value=(
                [], {"reason": "library_declaration_invalid", "detail": "d"})):
            for until in ("Compile", "Validate"):
                with self.subTest(until=until):
                    verdict = run_workflow._library_verdict(
                        self.repo, "spec/component/n", FORTRAN_CPU, until)
                    self.assertEqual((verdict["status"], verdict["reason"]),
                                     ("fail", "library_declaration_invalid"))
        with mock.patch.object(run_workflow, "_resolve_dependency_closure", return_value=(
                [], {"reason": "dependency_cycle", "detail": "d"})):
            self.assertIsNone(run_workflow._library_verdict(
                self.repo, "spec/component/n", FORTRAN_CPU, "Validate"))

    def test_a_usable_library_and_a_compile_only_run_say_nothing(self) -> None:
        self.assertIsNone(self._verdict())
        write_sites(self.repo, self.dirs, libraries="      vend:\n")
        self.assertIsNone(self._verdict("Compile"))

    def test_a_library_the_site_does_not_list_is_unavailable(self) -> None:
        write_sites(self.repo, self.dirs, libraries="      vend:\n")
        verdict = self._verdict()
        self.assertEqual((verdict["status"], verdict["reason"], verdict["library_id"],
                          verdict["site"]),
                         ("library_unavailable", "library_not_at_site", "foo", "local"))

    def test_an_unresolved_site_entry_is_a_refusal(self) -> None:
        write_sites(self.repo, self.dirs, libraries="      foo: {lib: /nonexistent}\n")
        verdict = self._verdict()
        self.assertEqual((verdict["status"], verdict["reason"]),
                         ("fail", xl.SITE_UNRESOLVED))

    def test_the_post_build_programs_are_required(self) -> None:
        from tools import host_prerequisites
        real_which = shutil.which
        with mock.patch.object(host_prerequisites.shutil, "which",
                               lambda name: None if name == "objdump" else real_which(name)):
            verdict = self._verdict()
        self.assertEqual((verdict["status"], verdict["reason"], verdict["missing"]),
                         ("fail", "missing_required_host_tools", ["objdump"]))


if __name__ == "__main__":
    unittest.main()
