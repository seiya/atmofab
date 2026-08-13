"""Tests for the Fortran structure front end (`tools/fortran_structure.py`).

WHAT IS PINNED HERE and what is not, stated because "pin" has been claimed for a sample in this
repository three times and broken three times:

* the SHAPE of what `parse_view` reports — kind, name, dummy text, result name, body offsets,
  interface spans — is pinned by construction, one row per alternative the module enumerates;
* the CORRECTNESS of that report against a whole corpus is NOT pinned here and cannot be: it is
  measured by `tools/fortran_structure_differential.py` against the 365 in-tree models and
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

from tools import fortran_structure as fs  # noqa: E402
from tools.validate_pipeline_semantics import _joined_masked_fortran_view  # noqa: E402


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
            from tools import fortran_structure as fs
            try:
                fs.parse_view("subroutine s\\nend subroutine s\\n")
            except fs.FortranStructureUnavailableError as exc:
                print("RAISED", fs.FORTRAN_STRUCTURE_UNAVAILABLE_MARKER in str(exc))
                print("INSTALL", "pip install tree-sitter tree-sitter-fortran" in str(exc))
            else:
                print("NO-RAISE")
        """))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("RAISED True", result.stdout)
        # The message has to tell the operator what to do: this failure is theirs, not the leaf's.
        self.assertIn("INSTALL True", result.stdout)

    def test_the_generate_gate_reports_the_marker_instead_of_going_quiet(self) -> None:
        # The whole point: without the front end the three `problem` gates can read NOTHING, and
        # the dangerous outcome is not an exception — it is a clean pass. This drives the real
        # `_validate_generate_outputs` over a real src dir, in a real interpreter with the import
        # really broken, and asserts a VIOLATION carrying the marker comes back.
        result = self._run(textwrap.dedent("""
            import tempfile
            from pathlib import Path
            from tools.fortran_structure import FORTRAN_STRUCTURE_UNAVAILABLE_MARKER
            import tools.validate_pipeline_semantics as vps

            td = Path(tempfile.mkdtemp())
            src = td / "src"
            src.mkdir()
            (src / "probe2d_model.f90").write_text(
                "module probe2d_model\\ncontains\\nsubroutine solve(x, y)\\n"
                "  real, intent(in) :: x\\n  real, intent(out) :: y\\n  y = 1.0\\n"
                "end subroutine solve\\nend module probe2d_model\\n")
            execution = vps.NodeExecution(node_key="problem/probe2d@0.1.0", node_dir=td,
                                          exec_dir=td, pipeline_dir=td)
            violations = []
            vps._validate_generate_outputs(td, execution, src, violations)
            print("MARKED", any(FORTRAN_STRUCTURE_UNAVAILABLE_MARKER in v for v in violations))
            # ... and the literal-outputs violation this source WOULD have earned is not reported
            # in its place, because the gate did not run at all.
            print("SILENT", any("literal-only assignments" in v for v in violations))
        """))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("MARKED True", result.stdout)
        self.assertIn("SILENT False", result.stdout)

    def test_with_the_packages_present_the_same_source_is_gated_normally(self) -> None:
        # The control for the row above: the stub is what makes it fail, not the fixture.
        import tools.validate_pipeline_semantics as vps
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "src"
            src.mkdir()
            (src / "probe2d_model.f90").write_text(
                "module probe2d_model\ncontains\nsubroutine solve(x, y)\n"
                "  real, intent(in) :: x\n  real, intent(out) :: y\n  y = 1.0\n"
                "end subroutine solve\nend module probe2d_model\n")
            execution = vps.NodeExecution(node_key="problem/probe2d@0.1.0", node_dir=root,
                                          exec_dir=root, pipeline_dir=root)
            violations: list[str] = []
            vps._validate_generate_outputs(root, execution, src, violations)
        self.assertTrue(any("literal-only assignments" in v for v in violations), violations)
        self.assertFalse(
            any(fs.FORTRAN_STRUCTURE_UNAVAILABLE_MARKER in v for v in violations), violations)


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
        self.assertIn("tools.fortran_structure", imported(bootstrap.body))


if __name__ == "__main__":
    unittest.main()
