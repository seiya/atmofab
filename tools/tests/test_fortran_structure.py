"""Tests for the Fortran structure front end (`tools/backends/language/fortran/structure.py`).

WHAT IS PINNED HERE and what is not, stated because "pin" has been claimed for a sample in this
repository three times and broken three times:

* the SHAPE of what `parse_view` reports — kind, name, dummy text, result name, body offsets,
  interface spans — is pinned by construction, one row per alternative the module enumerates;
* the CORRECTNESS of that report against a whole corpus is NOT pinned here and cannot be: it is
  measured by `tools/backends/language/fortran/structure_differential.py` against the 365 in-tree models and
  against flang, which is a development harness rather than a suite test because it needs
  binaries a run must not depend on;
* the three `problem` gates' own behaviour is pinned in `test_validate_pipeline_semantics.py`;
  this file stops at the front end.

These tests deliberately DO NOT skip when `tree_sitter` is absent. A suite that goes green on a
machine without the parser is a suite that has stopped asking the question — the calibration test
this repository silently skipped for weeks is the precedent. On such a machine this file is red,
which is the intended contract.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.backends.language.fortran import structure as fs  # noqa: E402
from tools.backends.language.fortran.source import (  # noqa: E402
    joined_masked_view as _joined_masked_fortran_view,
)


def view_of(source: str) -> str:
    return _joined_masked_fortran_view(source.lower())


class ParseViewShapeTests(unittest.TestCase):
    def test_each_procedure_kind_is_reported_with_its_own_kind(self) -> None:
        # One row per member of `_PROCEDURE_KINDS`. Dropping an entry drops a whole family of
        # procedures from the report silently — which is the failure mode the regex walk kept
        # having — so each has a row that fails on its own.
        tree = fs.parse_view(view_of(textwrap.dedent("""
            submodule (parent) child
            contains
            subroutine s(u, v)
            end subroutine s
            function f(x) result(y)
              real :: x, y
              y = x
            end function f
            module procedure mp
            end procedure mp
            end submodule child
        """).strip() + "\n"))
        self.assertEqual([], list(tree.errors))
        self.assertEqual(
            [(p.kind, p.name) for p in tree.procedures],
            [("subroutine", "s"), ("function", "f"), ("module_procedure", "mp")],
        )

    def test_a_functions_result_name_comes_from_result_or_from_its_own_name(self) -> None:
        # The definable output of a function is the RESULT variable, and `result(...)` renames it.
        # `PR B` keys the three gates' out-set on this field, so an empty answer here is a silent
        # gate there.
        tree = fs.parse_view(view_of(
            "function f(x) result(y)\nreal :: x, y\ny = x\nend function f\n"))
        self.assertEqual([("function", "f", "y")],
                         [(p.kind, p.name, p.result_name) for p in tree.procedures])
        tree = fs.parse_view(view_of("function g(x)\nreal :: x, g\ng = x\nend function g\n"))
        self.assertEqual([("function", "g", "g")],
                         [(p.kind, p.name, p.result_name) for p in tree.procedures])
        # A subroutine has no result variable, and reporting one would put a phantom name into
        # every gate's out-set.
        tree = fs.parse_view(view_of("subroutine s(u)\nreal :: u\nend subroutine s\n"))
        self.assertIsNone(tree.procedures[0].result_name)

    def test_an_empty_dummy_list_reads_the_same_as_no_dummy_list(self) -> None:
        # `subroutine s()` puts a bare `(` token where `subroutine s(a, b)` puts a `parameters`
        # node, so a reader that trusts the FIELD rather than the node type answers `"("` — which
        # `_split_fortran_names` then turns into no names at all, silently, in both cases. The
        # bug is invisible in the answer and visible here.
        for header in ("subroutine s()", "subroutine s( )", "subroutine s"):
            tree = fs.parse_view(view_of(f"{header}\nend subroutine s\n"))
            self.assertEqual("", tree.procedures[0].dummy_args_text, header)
        tree = fs.parse_view(view_of("subroutine s(a, b)\nend subroutine s\n"))
        self.assertEqual("a, b", tree.procedures[0].dummy_args_text)

    def test_the_body_offsets_slice_the_view_and_nothing_else(self) -> None:
        # THE invariant: a body is one contiguous slice of the view, so a position inside it is a
        # position inside the view (the dependency-dataflow gate compares an assignment position
        # with a call position taken from that slice). An off-by-one line at either end is the
        # difference between reading the header/terminator as body and losing the first or last
        # statement — the latter silences the gate.
        view = view_of(
            "module m\ncontains\nsubroutine s(u, v)\n"
            "  real, intent(out) :: v\n  v = u\nend subroutine s\nend module m\n")
        procedure = fs.parse_view(view).procedures[0]
        self.assertEqual("real, intent(out) :: v\nv = u\n",
                         view[procedure.body_start:procedure.body_end])

    def test_contains_at_marks_this_procedures_own_contains_only(self) -> None:
        # A contained procedure's dummies are its own, and `contains_at` is what keeps them out of
        # the host's out-scope. A derived type's `contains` introduces TYPE-BOUND procedures and
        # must NOT be reported here: mistaking it cuts the host's out-scope at the type
        # definition, which is a fail-open the regex walk shipped once and had to revert.
        view = view_of(
            "subroutine host(u, v)\n  real, intent(out) :: v\n  v = u\ncontains\n"
            "  subroutine inner(w)\n    real, intent(out) :: w\n  end subroutine inner\n"
            "end subroutine host\n")
        host = fs.parse_view(view).procedures[0]
        self.assertIsNotNone(host.contains_at)
        self.assertNotIn("intent(out) :: w", view[host.body_start:host.contains_at])
        view = view_of(
            "subroutine host(u, v)\n  type :: holder\n  contains\n"
            "    procedure, nopass :: p\n  end type holder\n"
            "  real, intent(out) :: v\n  v = u\nend subroutine host\n")
        self.assertIsNone(fs.parse_view(view).procedures[0].contains_at)


class ProcedureDeclarationsTests(unittest.TestCase):
    """`Procedure.declarations` and `module_level_definitions` (issue #430): a definition's OWN
    declaration statements, which the §5.1 signature pin compares."""

    _SOURCE = textwrap.dedent("""
        module m
          implicit none
        contains
          subroutine s(a, n)
            type Local_T
              integer :: k
            end type Local_T
            integer, intent(in) :: a(:)
            integer n
            optional :: n
            value n
            interface
              subroutine cb(k)
                integer :: k
              end subroutine cb
            end interface
            block
              real :: b_local
            end block
          contains
            subroutine inner(q)
              integer :: q
            end subroutine inner
          end subroutine s
          function f(x) result(r)
            type Local_F
              integer :: k
            end type Local_F
            integer, intent(in) :: x
            integer :: r
            r = x
          end function f
        end module m
        submodule (m) impl
        contains
          module procedure mp
          end procedure mp
        end submodule impl
    """)

    def _definitions(self):
        from tools.backends.language.fortran import source as fortran_source
        return fortran_source.module_level_definitions(self._SOURCE.lower(), "m")

    def test_only_the_direct_declaration_children_are_recorded(self) -> None:
        tree = fs.parse_view(view_of(self._SOURCE))
        s = next(p for p in tree.procedures if p.name == "s")
        texts = [(d.kind, tree.view[d.start:d.end].strip()) for d in s.declarations]
        self.assertEqual(texts, [
            ("variable_declaration", "integer, intent(in) :: a(:)"),
            ("variable_declaration", "integer n"),
            ("variable_modification", "optional :: n"),
            ("variable_modification", "value n"),
        ])
        inner = next(p for p in tree.procedures if p.name == "inner")
        self.assertEqual([tree.view[d.start:d.end].strip() for d in inner.declarations],
                         ["integer :: q"])

    def test_module_level_definitions_reads_header_and_own_declarations(self) -> None:
        definitions = self._definitions()
        self.assertEqual(set(definitions), {"s", "f", "mp"})
        # A function's own local types are read as a subroutine's are (round 2).
        self.assertEqual(definitions["f"].local_types, ("local_f",))
        self.assertEqual(definitions["s"].header, "subroutine s(a, n)")
        self.assertEqual(definitions["s"].declarations[0],
                         ("variable_declaration", "integer, intent(in) :: a(:)"))
        self.assertEqual(len(definitions["s"].declarations), 4)
        # The local type's component is a child of the type, not a declaration of `s`.
        self.assertEqual(definitions["s"].local_types, ("local_t",))

    def test_the_abbreviated_module_procedure_answers_none(self) -> None:
        self.assertIsNone(self._definitions()["mp"])

    def test_the_grammar_check_covers_the_declaration_node_types(self) -> None:
        # `_load_parser` refuses a grammar that does not define a `_REQUIRED_NODE_TYPES` member.
        # A renamed declaration node would leave every pinned dummy "not declared" — total
        # over-refusal with no operator-facing cause — so the two types `declarations` collects
        # must be members, and each must be one the installed grammar defines.
        self.assertLessEqual(set(fs._DECLARATION_TYPES), set(fs._REQUIRED_NODE_TYPES))
        self.assertLessEqual({"derived_type_definition", "derived_type_statement", "type_name"},
                             set(fs._REQUIRED_NODE_TYPES))
        self.assertEqual(set(fs._DECLARATION_TYPES),
                         {"variable_declaration", "variable_modification"})


class DerivedTypeTests(unittest.TestCase):
    """`StructureTree.types` and `module_level_type_definitions` (issue #430 PR-2): every derived
    type definition in the file with the scope the walk found it in, and the module-level ones of
    the publishing module, which the §5.1 type comparison reads."""

    _SOURCE = textwrap.dedent("""
        module m
          implicit none
          type T_Mod
            integer :: a; real :: b
          end type
          type, public, extends(t_mod) :: t_ext
            private
            sequence
          contains
            procedure :: tb
          end type t_ext
          interface
            subroutine ext(k)
              integer, intent(in) :: k
              type :: t_iface
                integer :: z
              end type t_iface
            end subroutine ext
          end interface
        contains
          subroutine s()
            type :: t_proc
              integer :: k
            end type t_proc
            block
              type :: t_block
                integer :: k
              end type t_block
            end block
          end subroutine s
        end module m
        submodule (m) impl
          type :: t_sub
            integer :: k
          end type t_sub
        end submodule impl
        module other
          type :: t_mod
            integer :: k
          end type t_mod
        end module other
        program main
          type :: t_prog
            integer :: k
          end type t_prog
        end program main
    """)

    def test_every_definition_is_recorded_with_its_scope(self) -> None:
        tree = fs.parse_view(view_of(self._SOURCE))
        got = [(t.name, t.unit_kind, t.unit, t.in_procedure, t.in_interface) for t in tree.types]
        self.assertEqual(got, [
            ("t_mod", "module", "m", False, False),
            ("t_ext", "module", "m", False, False),
            ("t_iface", "module", "m", True, True),
            ("t_proc", "module", "m", True, False),
            ("t_block", "module", "m", True, False),
            ("t_sub", "submodule", "impl", False, False),
            ("t_mod", "module", "other", False, False),
            ("t_prog", None, None, False, False),
        ])
        ext = next(t for t in tree.types if t.name == "t_ext")
        self.assertEqual(ext.header_extras, ("public", "extends(t_mod)"))
        self.assertEqual(ext.other_children,
                         ("private_statement", "sequence_statement", "derived_type_procedures"))
        self.assertEqual(ext.components, ())

    def test_module_level_type_definitions_reads_the_publishing_module_alone(self) -> None:
        from tools.backends.language.fortran import source as fortran_source
        reading = fortran_source.module_level_type_definitions(self._SOURCE.lower(), "m")
        self.assertEqual(set(reading.definitions), {"t_mod", "t_ext"})
        self.assertEqual(reading.definitions["t_mod"].components,
                         ("integer :: a", "real :: b"))
        self.assertEqual(reading.definitions["t_mod"].header_extras, ())
        self.assertEqual(reading.counts, {
            "t_mod": 2, "t_ext": 1, "t_iface": 1, "t_proc": 1, "t_block": 1, "t_sub": 1,
            "t_prog": 1})
        # A submodule is not the module a consumer `use`s, even when its name is asked for.
        self.assertEqual(
            set(fortran_source.module_level_type_definitions(self._SOURCE.lower(),
                                                             "impl").definitions), set())
        self.assertEqual(
            set(fortran_source.module_level_type_definitions(self._SOURCE.lower(),
                                                             "other").definitions), {"t_mod"})

    def test_the_grammar_check_covers_the_type_node_types(self) -> None:
        self.assertLessEqual(
            {"derived_type_definition", "derived_type_statement", "type_name",
             "access_specifier", "end_type_statement"}, set(fs._REQUIRED_NODE_TYPES))


class DeepNestingTests(unittest.TestCase):
    def test_a_deeply_nested_body_is_walked_without_recursion(self) -> None:
        # The walk was recursive, one Python frame per tree node, so a source with deeply nested
        # constructs raised `RecursionError`. That is a `RuntimeError`, so the validator's own
        # handler caught it, printed `schema_load_failed`, and discarded every other violation of
        # that invocation — a legal source (gfortran accepts it) taking down the whole gate run
        # and blaming schema loading. The regex walk this module replaced had no recursion, so it
        # is a surface the swap introduced. Found by review.
        #
        # The depth is deliberately far past the ~1000 where it used to break, and the assertion
        # is on the PROCEDURE, not merely on "no exception": the first iterative version pushed
        # children onto a different list than it popped, so it stopped raising AND stopped
        # reporting anything — no procedures, no errors, which is the silent gate this whole
        # module exists to prevent, and a green "no crash" test would have accepted it.
        depth = 2000
        opens = "".join(f"if (x > {index}.0) then\n" for index in range(depth))
        closes = "end if\n" * depth
        view = view_of(
            f"module m\ncontains\nsubroutine solve(x, y)\n"
            f"  real, intent(in) :: x\n  real, intent(out) :: y\n"
            f"{opens}  y = x\n{closes}end subroutine solve\nend module m\n")
        tree = fs.parse_view(view)
        self.assertEqual([], list(tree.errors))
        self.assertEqual([("subroutine", "solve")],
                         [(p.kind, p.name) for p in tree.procedures])
        self.assertIn("y = x", view[tree.procedures[0].body_start:tree.procedures[0].body_end])


class InterfaceSpanTests(unittest.TestCase):
    def test_blanking_is_in_place_and_length_preserving(self) -> None:
        view = view_of(
            "subroutine s(u, v)\n  interface\n    subroutine other(a)\n"
            "    end subroutine other\n  end interface\n  v = u\nend subroutine s\n")
        tree = fs.parse_view(view)
        blanked = fs.blank_interface_spans(view, tree.interface_spans)
        self.assertEqual(len(view), len(blanked))
        self.assertEqual(view.count("\n"), blanked.count("\n"))
        self.assertNotIn("subroutine other", blanked)

    def test_the_statement_after_end_interface_survives_the_blanking(self) -> None:
        # A REGRESSION PIN with a name: the first version of this module blanked one line too far
        # (a node span may include its terminating newline), which deleted the statement right
        # after `end interface` from the body. When that statement is the only assignment to the
        # `intent(out)` dummy — the shape of the acceptance matrix — all three gates go silent.
        # Fail-OPEN, and invisible to the 365-file differential: none of those models declares an
        # interface inside a body.
        view = view_of(
            "subroutine s(u, v)\n  real, intent(out) :: v\n  interface\n"
            "    subroutine other(a)\n    end subroutine other\n  end interface\n"
            "  v = u\nend subroutine s\n")
        tree = fs.parse_view(view)
        blanked = fs.blank_interface_spans(view, tree.interface_spans)
        procedure = tree.procedures[0]
        self.assertIn("v = u", blanked[procedure.body_start:procedure.body_end])

    def test_a_procedure_declared_in_an_interface_is_not_a_definition(self) -> None:
        # An interface body DECLARES procedures; it defines none. Reporting them mints phantom
        # envelopes whose "bodies" are declarations, and (worse) their `end subroutine` used to
        # close the enclosing envelope early.
        tree = fs.parse_view(view_of(
            "module m\ninterface\n  subroutine declared(a)\n  end subroutine declared\n"
            "end interface\ncontains\nsubroutine defined(u)\nend subroutine defined\n"
            "end module m\n"))
        self.assertEqual(["defined"], [p.name for p in tree.procedures])
        tree = fs.parse_view(view_of(
            "module m\nabstract interface\n  subroutine declared(a)\n"
            "  end subroutine declared\nend interface\ncontains\n"
            "subroutine defined(u)\nend subroutine defined\nend module m\n"))
        self.assertEqual(["defined"], [p.name for p in tree.procedures])


class ErrorReportingTests(unittest.TestCase):
    def test_an_unresolvable_structure_is_reported_with_a_locatable_line(self) -> None:
        # The message a leaf reads names the statement, so the line has to be the VIEW's line —
        # the view joins continuations, so a source line number would point at the wrong text.
        tree = fs.parse_view(view_of(
            "module m\ncontains\nsubroutine s(u, v)\n  real :: endsubroutine\n"
            "  endsubroutine = 1.0\n  v = u\nend subroutine s\nend module m\n"))
        self.assertTrue(tree.errors)
        for error in tree.errors:
            self.assertGreaterEqual(error.line, 1)
            self.assertLessEqual(error.line, len(tree.view.splitlines()))

    def test_a_clean_source_reports_no_errors(self) -> None:
        tree = fs.parse_view(view_of(
            "module m\ncontains\nsubroutine s(u, v)\n  real, intent(in) :: u\n"
            "  real, intent(out) :: v\n  v = u\nend subroutine s\nend module m\n"))
        self.assertEqual((), tree.errors)


class FrontEndUnavailableTests(unittest.TestCase):
    """The absent-package path, exercised in a REAL interpreter with the import really broken.

    Not `mock.patch`: what is being asserted is what a machine without the packages does, and a
    mock asserts what a machine with them does while a mock is installed. The stub shadows
    `tree_sitter` on `PYTHONPATH`, which is the same mechanism a broken install produces.
    """

    STUB = "raise ImportError('tree_sitter is not available in this test environment')\n"

    def _run(self, script: str) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as td:
            stub_dir = Path(td) / "stub"
            stub_dir.mkdir()
            (stub_dir / "tree_sitter.py").write_text(self.STUB)
            (stub_dir / "tree_sitter_fortran.py").write_text(self.STUB)
            env = dict(os.environ)
            env["PYTHONPATH"] = os.pathsep.join([str(stub_dir), str(REPO_ROOT)])
            return subprocess.run([sys.executable, "-c", script], cwd=str(REPO_ROOT),
                                  env=env, capture_output=True, text=True, check=False)

    def test_parse_view_raises_the_unavailable_error_carrying_the_marker(self) -> None:
        result = self._run(textwrap.dedent("""
            from tools.backends.language.fortran import structure as fs
            try:
                fs.parse_view("subroutine s\\nend subroutine s\\n")
            except fs.FortranStructureUnavailableError as exc:
                print("RAISED", fs.FORTRAN_STRUCTURE_UNAVAILABLE_MARKER in str(exc))
                print("INSTALL", "pip install -r requirements.txt" in str(exc))
            else:
                print("NO-RAISE")
        """))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("RAISED True", result.stdout)
        # The message has to tell the operator what to do: this failure is theirs, not the leaf's.
        self.assertIn("INSTALL True", result.stdout)

    def test_the_generate_gate_raises_instead_of_going_quiet(self) -> None:
        # The whole point: without the front end the three `problem` gates can read NOTHING, and
        # the dangerous outcome is not an exception — it is a clean pass. This drives the real
        # `_validate_generate_outputs` over a real src dir, in a real interpreter with the import
        # really broken, and asserts the error PROPAGATES (so `main` can answer with the dedicated
        # exit code) and that the literal-outputs violation this source would otherwise have
        # earned is not reported in its place.
        result = self._run(textwrap.dedent("""
            import tempfile
            from pathlib import Path
            from tools.backends.language.fortran import structure as fs
            import tools.validate_pipeline_semantics as vps

            from tools.tests.target_fixtures import install_target_profile, pipe_ref
            td = Path(tempfile.mkdtemp())
            install_target_profile(td)
            pipe = td / pipe_ref("problem__probe2d__0.1.0", "p1")
            src = pipe / "src"
            src.mkdir(parents=True)
            (src / "probe2d_model.f90").write_text(
                "module probe2d_model\\ncontains\\nsubroutine solve(x, y)\\n"
                "  real, intent(in) :: x\\n  real, intent(out) :: y\\n  y = 1.0\\n"
                "end subroutine solve\\nend module probe2d_model\\n")
            execution = vps.NodeExecution(node_key="problem/probe2d@0.1.0", node_dir=td,
                                          exec_dir=td, pipeline_dir=pipe)
            violations = []
            try:
                vps._validate_generate_outputs(td, execution, src, violations)
            except fs.FortranStructureUnavailableError as exc:
                print("RAISED", fs.FORTRAN_STRUCTURE_UNAVAILABLE_MARKER in str(exc))
            else:
                print("NO-RAISE")
            print("SILENT", any("literal-only assignments" in v for v in violations))
        """))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("RAISED True", result.stdout)
        self.assertIn("SILENT False", result.stdout)

    def test_the_cli_process_exits_with_the_dedicated_code(self) -> None:
        """THE CHANNEL, observed as a PROCESS EXIT CODE. A leaf can write any text it likes into
        a filename, and did defeat three successive text-based scans; it cannot write into an
        exit code — but that only holds if `main` actually maps the error to the code.

        This drives the real CLI as a subprocess with the import really broken, and asserts
        `returncode`. The row below it does not: it observes the exception at the gate and then
        prints the module CONSTANT, so it says nothing about `main`. With no row here, mutating
        `_main_dispatch`'s `return SOURCE_FRONTEND_UNAVAILABLE_EXIT_CODE` to `return 1` left
        1555 rows green — every conductor reader keyed on rc 3, the `--help` line and the RUNBOOK
        entry all rested on a mapping nothing observed. The twin (rc 4) had this witness from the
        start; this is the missing occurrence of the same rule.

        The fixture is a `problem` node, because the front end is reached through the three
        `problem` model gates.

        It also witnesses the ORDER of `_main_dispatch`'s two exception clauses, which the
        comment there calls load-bearing (`FortranStructureUnavailableError` IS a `RuntimeError`,
        so putting the `RuntimeError` clause first reports `schema_load_failed` at rc 1 — a
        leaf-repairable content failure for a machine problem). Measured: swapping them fails
        this row. A structural pin over the source was written first, on the premise that the
        order could not be observed without editing the module, and removed once that premise
        was measured false.
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            stub_dir = root / "stub"
            stub_dir.mkdir()
            (stub_dir / "tree_sitter.py").write_text(self.STUB)
            (stub_dir / "tree_sitter_fortran.py").write_text(self.STUB)
            repo = root / "repo"
            from tools.tests.target_fixtures import TARGET_ID, install_target_profile
            install_target_profile(repo)
            pipeline_dir = (repo / "workspace" / "pipelines" / "problem__probe2d__0.1.0"
                            / TARGET_ID / "probe2d_20260415_001")
            src = pipeline_dir / "source" / "src_20260415_001" / "src"
            src.mkdir(parents=True)
            (src / "probe2d_model.f90").write_text(
                "module probe2d_model\nend module probe2d_model\n", encoding="utf-8")
            (pipeline_dir / "lineage.json").write_text(json.dumps(
                {"node_key": "problem/probe2d@0.1.0",
                 "pipeline_id": "probe2d_20260415_001"}), encoding="utf-8")
            env = dict(os.environ)
            env["PYTHONPATH"] = os.pathsep.join([str(stub_dir), str(REPO_ROOT)])
            result = subprocess.run(
                [sys.executable, "tools/validate_pipeline_semantics.py",
                 "--repo-root", str(repo), "--workspace-root", "workspace",
                 "--stage", "post_generate", "--pipeline-root", str(pipeline_dir),
                 "--source-id", "src_20260415_001"],
                cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, check=False)
        import tools.validate_pipeline_semantics as vps
        self.assertEqual(vps.SOURCE_FRONTEND_UNAVAILABLE_EXIT_CODE, result.returncode,
                         (result.stdout, result.stderr))
        # The marker stays in the message for a human reader and carries no decision.
        self.assertIn("[fortran-structure-unavailable]", result.stdout)

    def test_the_gate_raises_and_the_module_constant_names_the_code(self) -> None:
        # NOT a witness of the CLI: this observes the exception at the gate and prints the module
        # constant. The row above is the one that observes `main`. Kept because it pins the
        # OTHER half — that the gate raises rather than passing quietly — with the import really
        # broken rather than mocked.
        result = self._run(textwrap.dedent("""
            import tempfile
            from pathlib import Path
            import tools.validate_pipeline_semantics as vps

            from tools.tests.target_fixtures import install_target_profile, pipe_ref
            td = Path(tempfile.mkdtemp())
            install_target_profile(td)
            pipe = td / pipe_ref("problem__probe2d__0.1.0", "p1")
            src = pipe / "src"
            src.mkdir(parents=True)
            (src / "probe2d_model.f90").write_text("module probe2d_model\\nend module probe2d_model\\n")
            execution = vps.NodeExecution(node_key="problem/probe2d@0.1.0", node_dir=td,
                                          exec_dir=td, pipeline_dir=pipe)
            try:
                vps._validate_generate_outputs(td, execution, src, [])
            except Exception as exc:
                print("EXIT", vps.SOURCE_FRONTEND_UNAVAILABLE_EXIT_CODE, type(exc).__name__)
        """))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("EXIT 3 FortranStructureUnavailableError", result.stdout)

    def test_with_the_packages_present_the_same_source_is_gated_normally(self) -> None:
        # The control for `test_the_generate_gate_raises_instead_of_going_quiet`: with the
        # packages present the same literal-only source earns its ordinary violation, so the stub
        # is what makes that row fail rather than the fixture. Named explicitly because "the row
        # above" stopped being that one when rows were added and renamed between them — and the
        # CLI row has its own control, measured when it was written: without the stub the same
        # fixture exits 0/PASS.
        import tools.validate_pipeline_semantics as vps
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            from tools.tests.target_fixtures import install_target_profile, pipe_ref
            install_target_profile(root)
            pipe = root / pipe_ref("problem__probe2d__0.1.0", "p1")
            src = pipe / "src"
            src.mkdir(parents=True)
            (src / "probe2d_model.f90").write_text(
                "module probe2d_model\ncontains\nsubroutine solve(x, y)\n"
                "  real, intent(in) :: x\n  real, intent(out) :: y\n  y = 1.0\n"
                "end subroutine solve\nend module probe2d_model\n")
            execution = vps.NodeExecution(node_key="problem/probe2d@0.1.0", node_dir=root,
                                          exec_dir=root, pipeline_dir=pipe)
            violations: list[str] = []
            vps._validate_generate_outputs(root, execution, src, violations)
        self.assertTrue(any("literal-only assignments" in v for v in violations), violations)
        self.assertFalse(
            any(fs.FORTRAN_STRUCTURE_UNAVAILABLE_MARKER in v for v in violations), violations)


class GrammarContractTests(unittest.TestCase):
    def test_every_node_type_this_module_matches_on_exists_in_the_grammar(self) -> None:
        # The whole module keys on node TYPE STRINGS. A grammar that renames one reports no
        # procedures AND no errors, so every gate returns at its empty-envelope loop with nothing
        # to say — silent, which is the one outcome this module exists to prevent. `_load_parser`
        # asks the grammar directly and converts that into the unavailable error; this pins that
        # the list it asks about is the list the code actually reads, which no version declaration
        # can do — `requirements.txt` bounds the release number an install resolves, and the node
        # type names are not a function of the release number this file could check.
        import tree_sitter_fortran
        from tree_sitter import Language

        language = Language(tree_sitter_fortran.language())
        for node_type in (*fs._PROCEDURE_KINDS, *fs._REQUIRED_NODE_TYPES):
            self.assertIsNotNone(language.id_for_node_kind(node_type, True), node_type)
        # The list is not decorative: every name in it is matched somewhere in the module.
        source = Path(fs.__file__).read_text()
        for node_type in fs._REQUIRED_NODE_TYPES:
            self.assertIn(f'"{node_type}"', source, node_type)

    def test_an_unknown_node_type_makes_the_front_end_unavailable_not_silent(self) -> None:
        # The failure direction, exercised by asking about a name no grammar defines. Without the
        # check, this configuration returns an empty procedure list and a clean parse.
        import tree_sitter_fortran
        from tree_sitter import Language

        language = Language(tree_sitter_fortran.language())
        self.assertIsNone(language.id_for_node_kind("subroutine_renamed_by_a_grammar_bump", True))
        original = fs._REQUIRED_NODE_TYPES
        try:
            fs._REQUIRED_NODE_TYPES = original + ("subroutine_renamed_by_a_grammar_bump",)
            with self.assertRaises(fs.FortranStructureUnavailableError) as caught:
                fs.parse_view("subroutine s\nend subroutine s\n")
        finally:
            fs._REQUIRED_NODE_TYPES = original
        self.assertIn(fs.FORTRAN_STRUCTURE_UNAVAILABLE_MARKER, str(caught.exception))
        self.assertIn("subroutine_renamed_by_a_grammar_bump", str(caught.exception))
        # ... and the module still works once it is restored, so the test cannot pass by leaving
        # the module broken.
        self.assertEqual(1, len(fs.parse_view("subroutine s\nend subroutine s\n").procedures))


class ImportBootstrapTests(unittest.TestCase):
    def test_the_cli_import_fallback_carries_the_same_names_as_the_package_import(self) -> None:
        # `validate_pipeline_semantics` imports its siblings twice: once as `from tools import …`
        # and once, under `except ModuleNotFoundError`, after putting the repo root on `sys.path`.
        # The SECOND branch is the one the conductor actually takes: `_gate_static_check` runs
        # `python3 tools/validate_pipeline_semantics.py`, which puts `tools/` on `sys.path[0]` and
        # NOT the repo root, so `from tools import …` raises and the fallback runs (executed).
        # A name added to the first branch and forgotten in the second therefore fails in
        # production and nowhere else — no test imports the module that way, which is why this
        # pins the two branches against each other rather than trying to reproduce the failure.
        import ast

        source = (REPO_ROOT / "tools" / "validate_pipeline_semantics.py").read_text()
        tree = ast.parse(source)
        branches = [node for node in ast.walk(tree)
                    if isinstance(node, ast.Try) and node.handlers
                    and isinstance(node.handlers[0].type, ast.Name)
                    and node.handlers[0].type.id == "ModuleNotFoundError"]
        self.assertEqual(1, len(branches), "the import bootstrap is no longer one try/except")
        bootstrap = branches[0]

        def imported(body) -> set[str]:
            names: set[str] = set()
            for node in ast.walk(ast.Module(body=list(body), type_ignores=[])):
                if isinstance(node, ast.ImportFrom):
                    names.update(f"{node.module}.{alias.name}" for alias in node.names)
            return names

        self.assertEqual(imported(bootstrap.body), imported(bootstrap.handlers[0].body))
        # Since issue #289 (R4-b PR-3) the structure front end is reached through the registry,
        # which loads the target language's backend lazily; the bootstrap must carry the
        # registry, and no language backend by name.
        self.assertIn("tools.backends.registry", {
            f"{node.module}" if node.module == "tools.backends" else ""
            for node in ast.walk(ast.Module(body=list(bootstrap.body), type_ignores=[]))
            if isinstance(node, ast.ImportFrom)} | {
            f"{node.module}.{alias.name}"
            for node in ast.walk(ast.Module(body=list(bootstrap.body), type_ignores=[]))
            if isinstance(node, ast.ImportFrom) for alias in node.names})
        self.assertFalse({n for n in imported(bootstrap.body)
                          if n.startswith("tools.backends.language")})


if __name__ == "__main__":
    unittest.main()
