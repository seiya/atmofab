"""The reading operations of the harness specs (issue #589, PR-2), held to the host's writer.

`data/harness_read_case/` is a hand-written realization of `harness_fortran_cpu@0.10.0` §2 / §3.2,
not a certified source. These rows pin that the contract the three specs state is implementable
against the bytes `primary_evidence.write_case_inputs` really writes (`1e-30`, an integer token, a
bool as `1`, `[]`), and the fail-closed half of that contract. They do not certify a harness: a
generated reader is judged by its own self-test.

What is pinned on the specs themselves: the eight operations are on each §5 list, in each §5.1 and
in each renderer's pin; the two tests are in each `tests.md`; the sentinels and the predicates of
`tests.md` evaluate true on what the realization returns, with the predicates read from the
document, so an edit to them moves this row.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np
import yaml

from tools import primary_evidence as pe
from tools.backends.language.cuda_cpp import runner as cuda_runner
from tools.backends.language.cuda_cpp import signatures as cuda_signatures
from tools.backends.language.fortran import runner as fortran_runner

ROOT = Path(__file__).resolve().parents[2]
DATA = Path(__file__).parent / "data" / "harness_read_case"
HARNESSES = ("harness_fortran_cpu", "harness_cpp_gpu", "harness_fortran_cpu_mpi")
NEW_OPERATIONS = ("read_case", "get_real", "get_int", "get_bool",
                  "get_array_r1", "get_array_r2", "get_array_r3", "get_array_r4")
_HARNESS_DIR = ROOT / "spec" / "infrastructure" / "infra" / "harness"

#: `tests.md` §2's sentinels of `l0_read_case_roundtrip_pass`, as a Compile leaf transcribes them.
SENTINELS = {
    "rc_real": 0.1, "rc_whole": 2, "rc_int": -12, "rc_true": True, "rc_false": False,
    "rc_empty": [], "rc_r1": [-1.5, 1.0e-30, 1.0e+30], "rc_r2": [[1, 2, 3], [4, 5, 6]],
    "rc_r3": np.arange(1, 13).reshape(2, 2, 3).tolist(),
    "rc_r4": np.arange(1, 25).reshape(2, 2, 2, 3).tolist(),
}


def _section51(spec_id: str) -> dict:
    text = (_HARNESS_DIR / spec_id / "controlled_spec.md").read_text(encoding="utf-8")
    block = text[text.index("### 5.1 "):]
    return yaml.safe_load(re.search(r"```yaml\n(.*?)```", block, re.S).group(1))


class SpecSurfaceTests(unittest.TestCase):
    def test_every_harness_publishes_the_reading_operations(self) -> None:
        for spec_id in HARNESSES:
            with self.subTest(spec_id=spec_id):
                text = (_HARNESS_DIR / spec_id / "controlled_spec.md").read_text(encoding="utf-8")
                listed = re.search(r"The published `operation_id`s are exactly:[^\n]*", text).group(0)
                procedures = {p["name"]: p for p in _section51(spec_id)["procedures"]}
                for op in NEW_OPERATIONS:
                    self.assertIn(f"`{spec_id}__{op}`", listed)
                    self.assertIn(f"{spec_id}__{op}", procedures)
                self.assertEqual([a["name"] for a in procedures[f"{spec_id}__read_case"]["args"]],
                                 ["case_id", "ok"])
                for r in range(1, 5):
                    out = procedures[f"{spec_id}__get_array_r{r}"]["args"][1]
                    self.assertEqual((out["intent"], out["rank"], out["spec"].get("alloc")),
                                     ("out", r, True))

    def test_every_tests_document_has_both_tests_and_counts_them(self) -> None:
        words = {"harness_fortran_cpu": ("nine", 9), "harness_cpp_gpu": ("ten", 10),
                 "harness_fortran_cpu_mpi": ("fifteen", 15)}
        for spec_id, (word, count) in words.items():
            with self.subTest(spec_id=spec_id):
                text = (_HARNESS_DIR / spec_id / "tests.md").read_text(encoding="utf-8")
                ids = re.findall(r"^- `test_id`: `([^`]+)`", text, re.M)
                self.assertEqual(len(ids), count)
                self.assertIn(f"The suite defines {word} `case`s and {word} `test_id`s", text)
                self.assertIn(f"`final.case_index <= {count}.0`", text)
                self.assertIn("l0_read_case_roundtrip_pass", ids)
                self.assertIn("l0_missing_case_file_xfail", ids)

    def test_the_fortran_pins_carry_the_operations_under_each_harness_name(self) -> None:
        for spec_id, pin in fortran_runner._HARNESS_PINS.items():
            for op in NEW_OPERATIONS:
                with self.subTest(spec_id=spec_id, op=op):
                    self.assertIn(f"{spec_id}__{op}(", pin.interface)

    def test_the_cuda_pin_is_the_harness_section51(self) -> None:
        pinned = yaml.safe_load(cuda_runner._HARNESS_V1_INTERFACE)
        self.assertEqual(pinned, _section51("harness_cpp_gpu"))

    def test_the_cuda_lowering_of_an_allocatable_out_is_an_owning_reference(self) -> None:
        procedures = {p["name"]: p for p in _section51("harness_cpp_gpu")["procedures"]}
        rendered = cuda_signatures.render_symbol(procedures["harness_cpp_gpu__get_array_r1"])
        self.assertIn("std::vector<dp>& a", rendered)
        for r in (2, 3, 4):
            rendered = cuda_signatures.render_symbol(procedures[f"harness_cpp_gpu__get_array_r{r}"])
            self.assertIn(f"atmofab::Array<dp, {r}>& a", rendered)


class ReaderAgainstTheHostWriterTests(unittest.TestCase):
    """The realization, built with the compiler the serial target uses, over `write_case_inputs`."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        for name in ("harness_fortran_cpu_model.f90", "probe.f90"):
            shutil.copy(DATA / name, cls.dir / name)
        subprocess.run(["gfortran", "-O0", "-fcheck=bounds", "-o", "probe",
                        "harness_fortran_cpu_model.f90", "probe.f90"],
                       cwd=cls.dir, check=True, capture_output=True, text=True)
        cls.ir_dir = cls.dir / "ir"
        cls.ir_dir.mkdir()
        cls.spec = cls.ir_dir / "spec.ir.yaml"
        cls.spec.write_text("# the reader does not open this file\n", encoding="utf-8")
        ir = {"case": {"test_case_set": [
            {"case_id": "c1", "inputs": {"initial": SENTINELS, "note": "skipped string"}},
            {"case_id": "c2", "inputs": {"initial": {"only_here": 5.0}}},
            {"case_id": "c3", "inputs": {}},
        ]}}
        pe.write_case_inputs(ir, cls.ir_dir)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def _run(self, *commands: str, spec: str | None = None
             ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.dir / "probe"), "--cases", spec or str(self.spec), "c1", "--", *commands],
            cwd=self.dir, capture_output=True, text=True, check=False)

    def _write_case(self, case_id: str, text: str) -> None:
        (self.ir_dir / "case_inputs" / f"{case_id}.json").write_text(text, encoding="utf-8")

    def test_the_tests_md_sentinels_read_back_and_satisfy_the_documents_predicates(self) -> None:
        K = "inputs.initial."
        res = self._run("read:c1", f"real:{K}rc_real", f"real:{K}rc_whole", f"int:{K}rc_int",
                        f"bool:{K}rc_true", f"bool:{K}rc_false", f"r1:{K}rc_empty", f"r1:{K}rc_r1",
                        f"r2:{K}rc_r2", f"r3:{K}rc_r3", f"r4:{K}rc_r4")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        lines = res.stdout.splitlines()
        self.assertEqual(lines[0].split(), ["read", "T"])
        value = lambda line: line.split()[-1]  # noqa: E731
        scalar = {
            "rc_real": float(value(lines[1])), "rc_whole": float(value(lines[2])),
            "rc_int": float(value(lines[3])),
            "rc_true": 1.0 if value(lines[4]) == "T" else 0.0,
            "rc_false": 1.0 if value(lines[5]) == "F" else 0.0,
        }
        self.assertEqual(lines[6].split()[1:], ["0"])  # the empty array; its value line is blank
        at = 8
        arrays: dict[str, np.ndarray] = {}
        for name in ("rc_r1", "rc_r2", "rc_r3", "rc_r4"):
            extents = tuple(int(e) for e in lines[at].split()[1:])
            flat = np.array([float(t) for t in lines[at + 1].split()])
            arrays[name] = flat.reshape(extents, order="F")   # printed in array element order
            at += 2
        final = {**scalar, **arrays, "rc_empty_len": 0.0}
        tests = (_HARNESS_DIR / "harness_fortran_cpu" / "tests.md").read_text(encoding="utf-8")
        row = tests[tests.index("- `test_id`: `l0_read_case_roundtrip_pass`"):]
        row = row[:row.index("- `test_id`: `l0_missing_case_file_xfail`")]
        predicate = re.search(r"`predicate`[^\n]*", row).group(0)
        expressions = re.findall(r"`((?:max\(|final\.)[^`]+)`", predicate)
        self.assertEqual(len(expressions), 4)
        env = pe.CaseEnv(case_id="c1", initial=None, final=final,
                         inputs={"initial": SENTINELS}, coordinates={})
        for expression in expressions:
            with self.subTest(expression=expression[:40]):
                left, right = expression.rsplit(" == ", 1)
                self.assertEqual(float(pe.evaluate(pe.parse_expr(left), env)), float(right))

    def test_the_arrays_keep_the_leading_index_outermost(self) -> None:
        K = "inputs.initial."
        res = self._run("read:c1", f"r2:{K}rc_r2")
        lines = res.stdout.splitlines()
        self.assertEqual(lines[1].split()[1:], ["2", "3"])
        # a(1,1), a(2,1), a(1,2), ... in array element order for [[1,2,3],[4,5,6]]
        self.assertEqual([float(t) for t in lines[2].split()], [1, 4, 2, 5, 3, 6])

    def test_an_absent_case_file_is_refused_without_stopping(self) -> None:
        res = self._run("read:l0_no_such_case_file")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.split(), ["read", "F"])

    def test_a_failed_parse_keeps_the_spec_path(self) -> None:
        res = self._run("empty_parse:", "read:c3")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.split(), ["empty_parse", "F", "read", "T"])

    def test_a_case_with_no_numeric_input_is_an_empty_object(self) -> None:
        self.assertEqual((self.ir_dir / "case_inputs" / "c3.json").read_text(), "{}\n")

    def test_a_refused_read_leaves_no_value_of_the_previous_case(self) -> None:
        res = self._run("read:c2", "real:inputs.initial.only_here", "read:l0_no_such_case_file",
                        "real:inputs.initial.only_here")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("no case is loaded", res.stdout)

    def test_a_new_read_replaces_the_case(self) -> None:
        res = self._run("read:c2", "read:c3", "real:inputs.initial.only_here")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("does not hold this name", res.stdout)

    def test_a_getter_stops_the_program_on_what_the_case_does_not_hold(self) -> None:
        K = "inputs.initial."
        for label, command, why in (
                ("no case", f"real:{K}rc_real", "no case is loaded"),
                ("missing", "read:c1|real:absent", "does not hold this name"),
                ("array as a scalar", f"read:c1|real:{K}rc_r1", "array asked as a scalar"),
                ("array as an int", f"read:c1|int:{K}rc_r1", "array asked as a scalar"),
                ("array as a bool", f"read:c1|bool:{K}rc_r1", "array asked as a scalar"),
                ("scalar as an array", f"read:c1|r1:{K}rc_real", "scalar asked as an array"),
                ("depth below the rank", f"read:c1|r2:{K}rc_r1", "nesting depth"),
                ("depth above the rank", f"read:c1|r1:{K}rc_r2", "nesting depth"),
                ("fraction as an int", f"read:c1|int:{K}rc_real", "integer token"),
                ("a real as a bool", f"read:c1|bool:{K}rc_real", "tokens 1 and 0"),
                ("two as a bool", f"read:c1|bool:{K}rc_whole", "tokens 1 and 0")):
            with self.subTest(label):
                res = self._run(*command.split("|"))
                self.assertNotEqual(res.returncode, 0, res.stdout)
                self.assertIn(why, res.stdout)

    def test_an_integer_that_overflows_the_default_integer_stops_the_getter(self) -> None:
        self._write_case("big", '{"n":99999999999}\n')
        res = self._run("read:big", "int:n")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("integer token", res.stdout)

    def test_a_document_outside_the_grammar_is_refused_by_read_case(self) -> None:
        for label, text in (
                ("not an object", "[1]"), ("truncated", '{"a":1'), ("empty file", ""),
                ("nested object", '{"a":{"b":1}}'), ("string", '{"a":"x"}'),
                ("true", '{"a":true}'), ("null", '{"a":null}'),
                ("NaN", '{"a":NaN}'), ("Infinity", '{"a":[1,Infinity]}'), ("-Infinity", '{"a":-Infinity}'),
                ("ragged", '{"a":[[1,2],[3]]}'), ("ragged empty", '{"a":[[1],[]]}'),
                ("mixed depth", '{"a":[1,[2]]}'), ("mixed depth reversed", '{"a":[[1],2]}'),
                ("depth five", '{"a":[[[[[1]]]]]}'), ("escape", '{"a\\"b":1}'),
                ("overflow", '{"a":1e999}'), ("leading zero", '{"a":01}'), ("bare dot", '{"a":.5}'),
                ("trailing dot", '{"a":1.}'), ("plus sign", '{"a":+1}'), ("trailing text", '{"a":1} x'),
                ("trailing comma", '{"a":1,}')):
            with self.subTest(label):
                self._write_case("bad", text)
                res = self._run("read:bad")
                self.assertEqual(res.stdout.split(), ["read", "F"], res.stdout + res.stderr)
                self.assertEqual(res.returncode, 0)

    def test_the_number_spellings_the_host_writes_read_exactly(self) -> None:
        values = [0.1, 1e-30, 1e+30, -1.5, 2.5e-308, 1.7976931348623157e308, 5e-324, 1 / 3]
        ir = {"case": {"test_case_set": [{"case_id": "spellings", "inputs": {"v": values}}]}}
        pe.write_case_inputs(ir, self.ir_dir)
        res = self._run("read:spellings", "r1:inputs.v")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual([float(t) for t in res.stdout.splitlines()[2].split()], values)

    def test_a_line_longer_than_any_record_buffer_reads_whole(self) -> None:
        values = [float(i) + 0.25 for i in range(40000)]
        ir = {"case": {"test_case_set": [{"case_id": "long", "inputs": {"v": values}}]}}
        pe.write_case_inputs(ir, self.ir_dir)
        res = self._run("read:long", "r1:inputs.v")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(res.stdout.splitlines()[1].split()[1:], ["40000"])

    def test_zero_extents_read_at_every_rank(self) -> None:
        self._write_case("zero", '{"e":[],"f":[[],[]]}\n')
        res = self._run("read:zero", "r1:e", "r2:e", "r3:e", "r4:e", "r2:f", "r3:f")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        shapes = [line.split()[1:] for line in res.stdout.splitlines()
                  if line.startswith(("e ", "f "))]
        self.assertEqual(shapes, [["0"], ["0", "0"], ["0", "0", "0"], ["0", "0", "0", "0"],
                                  ["2", "0"], ["2", "0", "0"]])
        res = self._run("read:zero", "r1:f")
        self.assertNotEqual(res.returncode, 0)

    def test_blanks_between_tokens_are_admitted(self) -> None:
        self._write_case("spaced", ' {\n "a" : [ 1 ,\t2 ] , "b" : 3 }\r\n')
        res = self._run("read:spaced", "r1:a", "int:b")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("b 3", res.stdout)

    def test_a_spec_path_with_no_directory_part_reads_from_the_current_directory(self) -> None:
        res = subprocess.run(
            [str(self.dir / "probe"), "--cases", "spec.ir.yaml", "c1", "--", "read:c3"],
            cwd=self.ir_dir, capture_output=True, text=True, check=False)
        self.assertEqual(res.stdout.split(), ["read", "T"], res.stdout + res.stderr)


if __name__ == "__main__":
    unittest.main()
