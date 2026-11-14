"""Issue #644 P2: the LAPACK sample `component/dense_solve_lu`, without a leaf.

The subject is the SPEC and its suite, not the library machinery (P1's
`test_external_library.py`): a hand-written model of the Controlled Spec, linked against the
`lapack` the compiler's own search finds on this host, is run through the host-rendered driver
on the hand transcription `data/drive_ir/pr3_evidence/ir/dense_solve_lu.yaml`, and every
predicate is evaluated by the host evaluator. The rows pin that

* the unmutated model has every test at its expected outcome;
* each seeded defect of `dense_solve_lu_mutants.yaml` moves exactly the tests it names, so an
  edit of the hand IR that stops judging one of them is red here. `tests.md` is held to the
  hand IR by `test_transcription_compile_static_accepts_dense_solve_lu`
  (`test_spec_corpus_versions.py`), which compares the test ids, outcomes and quantities and
  neither a case nor a bound: a case changed or a bound widened in `tests.md` alone is red
  nowhere;
* a model that never reaches the library passes the suite and is refused by the post-build
  symbol check, the one thing that tells it from a conforming model;
* a source that calls the library the way the leaf is told to
  (`tools/prompt_templates/backends/language/fortran/generate_generate.txt`
  `external_library_use`) is clean under the declared lint rules, and the two shapes that
  fragment warns against are findings.

These rows FAIL, never skip, on a host without the library (issue #498): CI installs it
(`.github/workflows/tests.yml`; `docs/DEVELOPMENT.md` §Fresh-machine setup step 6).
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from tools import external_library as xl
from tools import primary_evidence as pe
from tools import verdict_evaluator
from tools.backends import registry
from tools.backends.linter.fortitude import lint
from tools.tests.target_fixtures import FORTRAN_CPU

REPO = Path(__file__).resolve().parents[2]
EVIDENCE = Path(__file__).parent / "data" / "drive_ir" / "pr3_evidence"
SPEC_ID = "dense_solve_lu"
SPEC_REF = f"spec/component/numerics/linear_algebra/{SPEC_ID}"
MODEL = EVIDENCE / "models" / f"{SPEC_ID}_model.f90"
MUTANTS = yaml.safe_load((EVIDENCE / f"{SPEC_ID}_mutants.yaml").read_text(encoding="utf-8"))


def _load_bench():
    spec = importlib.util.spec_from_file_location("pr3_verify_spec", EVIDENCE / "verify_spec.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _mutations(mutant_id: str) -> list[str]:
    """A mutant's patches in the bench's `old=>new` form."""
    return [f"{p['old']}=>{p['new']}" for p in MUTANTS[mutant_id]["patches"]]


def _mutated(mutant_id: str | None) -> str:
    text = MODEL.read_text(encoding="utf-8")
    for patch in (MUTANTS[mutant_id]["patches"] if mutant_id else []):
        assert text.count(patch["old"]) == 1, (mutant_id, patch["old"])
        text = text.replace(patch["old"], patch["new"])
    return text


class DenseSolveLuSampleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        for program in ("gfortran", "nm", "objdump", lint.EXECUTABLE):
            assert shutil.which(program) is not None, f"{program} is not installed"
        cls.bench = _load_bench()
        cls.ir = yaml.safe_load((EVIDENCE / "ir" / f"{SPEC_ID}.yaml").read_text(encoding="utf-8"))
        try:
            cls.resolved = cls.bench.libraries(["lapack"])
        except xl.LibraryUnresolved as exc:
            raise AssertionError(
                f"the `lapack` library is not where the compiler's own search finds it ({exc}); "
                "install it (docs/DEVELOPMENT.md §Fresh-machine setup step 6)") from exc
        cls.expected = {t["test_id"]: t["expected_outcome"]
                        for t in cls.ir["io_contract"]["tests"]}

    def _moved(self, mutant_id: str | None) -> set[str]:
        """The tests whose status is not their expected outcome when the model, with
        `mutant_id`'s patches, is built against the library and run on every case."""
        with tempfile.TemporaryDirectory() as td:
            try:
                self.bench.run(self.ir, MODEL, [], Path(td),
                               _mutations(mutant_id) if mutant_id else [], self.resolved)
            except SystemExit as exc:   # the bench's way of reporting a build or run failure
                self.fail(f"{mutant_id or 'the unmutated model'}: {exc}")
            records = pe.evaluate_primary_predicates(self.ir, Path(td) / "run")
        verdict = verdict_evaluator.evaluate_verdict(self.ir["io_contract"]["tests"], records)
        got = {t["test_id"]: t["status"] for t in verdict["per_test"]}
        return {tid for tid, outcome in self.expected.items() if got.get(tid) != outcome}

    def _symbol_violation(self, mutant_id: str | None) -> str | None:
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / MODEL.name).write_text(_mutated(mutant_id), encoding="utf-8")
            subprocess.run(["gfortran", *self.bench.FLAGS, "-c", MODEL.name, "-o", "model.o"],
                           cwd=td, check=True, capture_output=True)
            _record, violation = xl.symbol_check(FORTRAN_CPU, [Path(td) / "model.o"],
                                                 self.resolved, xl.node_library_ids(REPO, SPEC_REF))
        return violation

    def _lint_codes(self, text: str) -> set[str]:
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / MODEL.name).write_text(text, encoding="utf-8")
            proc = subprocess.run(list(lint.check_argv()), cwd=td, capture_output=True,
                                  text=True, check=False)
        codes = {line.split()[1] for line in proc.stdout.splitlines()
                 if line.startswith(f"{MODEL.name}:")}
        self.assertEqual(proc.returncode == 0, not codes, proc.stdout + proc.stderr)
        return codes

    def test_the_node_declares_the_library_and_the_library_exports_the_declared_procedure(
            self) -> None:
        """The spec's `deps.yaml` names `lapack`; the declaration's one procedure is an export
        of the file its one link name resolves to on this host."""
        self.assertEqual(xl.node_library_ids(REPO, SPEC_REF), ["lapack"])
        declaration = xl.load_declaration(REPO, "lapack")
        self.assertEqual((declaration.language, declaration.modules, declaration.procedures,
                          declaration.link), ("fortran", (), ("dgesv",), ("lapack",)))
        binary = registry.capability_module("compiler", "gfortran", "library_binary")
        (_name, link_file), = self.resolved[0].link_files
        self.assertTrue(any(symbol.rstrip("_") == "dgesv" for symbol in binary.exports(link_file)))

    def test_the_hand_model_has_every_test_at_its_expected_outcome(self) -> None:
        self.assertEqual(sorted(self.expected.values()),
                         ["pass", "pass", "xfail", "xfail"])
        self.assertEqual(self._moved(None), set())

    def test_the_hand_model_reaches_the_library_it_declares(self) -> None:
        self.assertIsNone(self._symbol_violation(None))

    def test_a_model_that_never_reaches_the_library_is_refused_at_build_alone(self) -> None:
        """The suite cannot tell a correct elimination in the model's own source from the
        library's (`tests.md` judges the solution): `library_declared_unused` does."""
        row = MUTANTS["library-never-reached"]
        self.assertEqual((row["by"], row["build"]), ([], "library_declared_unused"))
        self.assertEqual(self._moved("library-never-reached"), set())
        violation = self._symbol_violation("library-never-reached") or ""
        self.assertIn("library_declared_unused", violation)
        self.assertIn("'lapack'", violation)

    def test_the_hand_model_is_clean_under_the_declared_lint_rules(self) -> None:
        """The interface-block form the leaf is told to write passes the rule selection, and the
        two shapes it is warned against are findings: an assumed-size dummy and an interface
        body without its own `implicit none`."""
        text = MODEL.read_text(encoding="utf-8")
        self.assertEqual(self._lint_codes(text), set())
        self.assertEqual(text.count("a(lda, n)"), 1)
        self.assertEqual(self._lint_codes(text.replace("a(lda, n)", "a(lda, *)")), {"C071"})
        self.assertEqual(text.count("      implicit none\n"), 1)
        self.assertEqual(self._lint_codes(text.replace("      implicit none\n", "")), {"C002"})


def _defect_row(mutant_id: str):
    def row(self: DenseSolveLuSampleTest) -> None:
        self.assertEqual(self._moved(mutant_id), set(MUTANTS[mutant_id]["by"]))
    row.__doc__ = f"{mutant_id}: {' '.join(str(MUTANTS[mutant_id]['title']).split())}"
    return row


#: One row per defect a test must catch (a row per mutant, so the runs spread over workers).
_DEFECT_ROW = "test_the_seeded_defect_moves_the_tests_it_names_"
for _mutant_id, _mutant in MUTANTS.items():
    if _mutant["by"]:
        setattr(DenseSolveLuSampleTest, _DEFECT_ROW + _mutant_id.replace("-", "_"),
                _defect_row(_mutant_id))


class DefectRowsTest(unittest.TestCase):
    def test_every_mutant_is_measured_by_a_row(self) -> None:
        """A mutant with tests to move has its generated row; the one without is the build
        row's. So a row added to the file is run, never only listed."""
        rows = {name.removeprefix(_DEFECT_ROW) for name in dir(DenseSolveLuSampleTest)
                if name.startswith(_DEFECT_ROW)}
        self.assertEqual(rows, {"transposed_matrix", "ignored_status", "status_compared_with_order",
                                "wrong_leading_dimension", "inputs_overwritten"})
        self.assertEqual({m for m, row in MUTANTS.items() if not row["by"]},
                         {"library-never-reached"})


if __name__ == "__main__":
    unittest.main()
