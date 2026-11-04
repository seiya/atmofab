"""The reading operations of the harness specs (issue #589, PR-2), held to the host's writer.

`data/harness_read_case/` is a hand-written realization of `harness_fortran_cpu@0.10.0` §2 / §3.2,
not a certified source. These rows pin that the contract the three specs state is implementable
against the bytes `primary_evidence.write_case_inputs` really writes (`1e-30`, an integer token, a
bool as `1`, `[]`), and the fail-closed half of that contract. They do not certify a harness: a
generated reader is judged by its own self-test and by `generate.verify`.

What is pinned on the specs themselves: the operations on each §5 list, in each §5.1 and in each
renderer's pin; the reading text one across the three harnesses; the sentinels and predicates read
from each `tests.md` and evaluated on what the realization returns; every sentinel judged by a term
of the predicate, and none of the array sentinels holding an extent-1 axis.
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


def _tests_md(spec_id: str) -> str:
    return (_HARNESS_DIR / spec_id / "tests.md").read_text(encoding="utf-8")


def _sentinels(spec_id: str, case_id: str) -> dict:
    """`tests.md` §2's sentinels of `case_id`, as a Compile leaf transcribes them."""
    bullet = re.search(rf"^- `{case_id}` declares [^\n]*$", _tests_md(spec_id), re.M).group(0)
    out = {name: yaml.safe_load(value) for name, value
           in re.findall(r"`(?:inputs\.initial\.)?(rc_\w+) = ([^`]+)`", bullet)}
    for name, extents, last in re.findall(
            r"`(rc_\w+)` of extents `\[([0-9,]+)\]` holding `1\.\.(\d+)`", bullet):
        shape = tuple(int(e) for e in extents.split(","))
        out[name] = np.arange(1, int(last) + 1).reshape(shape).tolist()
    return out


def _predicates(spec_id: str, test_id: str) -> list[str]:
    """Every predicate expression of `test_id` in `tests.md`, each `<expr> == <value>`. Everything
    in backticks after the line's head is an expression, so a term spelled some other way fails
    here rather than going unread."""
    text = _tests_md(spec_id)
    row = text[text.index(f"- `test_id`: `{test_id}`") + 1:]
    row = row[:row.index("- `test_id`:")] if "- `test_id`:" in row else row
    line = re.search(r"`predicate`[^\n]*", row).group(0)
    body = line.split("): ", 1)[1].split("(op `eq`)", 1)[0]
    expressions = re.findall(r"`([^`]+)`", body)
    assert expressions and all(" == " in e for e in expressions), line
    return expressions


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

    def test_every_tests_document_has_the_tests_and_counts_them(self) -> None:
        words = {"harness_fortran_cpu": ("ten", 10), "harness_cpp_gpu": ("eleven", 11),
                 "harness_fortran_cpu_mpi": ("sixteen", 16)}
        for spec_id, (word, count) in words.items():
            with self.subTest(spec_id=spec_id):
                text = _tests_md(spec_id)
                ids = re.findall(r"^- `test_id`: `([^`]+)`", text, re.M)
                self.assertEqual(len(ids), count)
                self.assertIn(f"The suite defines {word} `case`s and {word} `test_id`s", text)
                self.assertIn(f"`final.case_index <= {count}.0`", text)
                for test_id in ("l0_read_case_roundtrip_pass", "l0_read_case_second_pass",
                                "l0_missing_case_file_xfail"):
                    self.assertIn(test_id, ids)

    def test_the_reading_contract_is_one_text_across_the_three_harnesses(self) -> None:
        """What each harness states of the reading operations and their tests must not drift
        apart; only the harness's name, its stop spelling and the distributed harness's one
        sentence on who reads differ."""
        def spec_slices(spec_id: str) -> list[str]:
            text = (_HARNESS_DIR / spec_id / "controlled_spec.md").read_text(encoding="utf-8")
            paragraph = re.search(r"^Input \(to `__read_case`\):[^\n]*$", text, re.M).group(0)
            paragraph = paragraph.replace(
                " Every rank reads the file itself: the reading is no collective operation and"
                " it communicates nothing.", "")
            return [
                paragraph,
                re.search(r"^  - `l0_read_case_roundtrip_pass`: .*?^  - `l0_missing_case_file_xfail`[^\n]*$",
                          text, re.M | re.S).group(0),
                re.search(r"keep the following token as the spec path \([^)]*\)", text).group(0),
                re.search(rf"^- `{spec_id}__read_case\(.*?^- Each getter[^\n]*$", text,
                          re.M | re.S).group(0),
                re.search(r"^The `l0_read_case_roundtrip_pass` case calls[^\n]*$", text, re.M).group(0),
                re.search(r"^A case file that is absent or unreadable[^\n]*$", text, re.M).group(0),
                re.search(r"^- A getter returns the value[^\n]*$", text, re.M).group(0),
            ]

        def tests_slices(spec_id: str) -> list[str]:
            text = _tests_md(spec_id)
            start = text.index("- `l0_read_case_roundtrip_pass` declares")
            tests = text[text.index("- `test_id`: `l0_read_case_roundtrip_pass`"):]
            return [text[start:text.index("- The abnormal case", start)],
                    tests[:tests.index("## 7.")]]

        def normal(spec_id: str) -> tuple[str, ...]:
            out = []
            for piece in spec_slices(spec_id) + tests_slices(spec_id):
                piece = piece.replace(spec_id, "H")
                out.append(re.sub(r"\(`(error stop|std::exit\(2\))`\)", "(STOP)", piece))
            return tuple(out)

        reference = normal(HARNESSES[0])
        for spec_id in HARNESSES[1:]:
            for mine, theirs in zip(normal(spec_id), reference):
                with self.subTest(spec_id=spec_id, slice=theirs[:40]):
                    self.assertEqual(mine, theirs)

    def test_section_2_declares_each_sentinel_array_at_its_tests_md_shape(self) -> None:
        for spec_id in HARNESSES:
            text = (_HARNESS_DIR / spec_id / "controlled_spec.md").read_text(encoding="utf-8")
            sentinels = _sentinels(spec_id, "l0_read_case_roundtrip_pass")
            for name in ("rc_r1", "rc_r2", "rc_r3", "rc_r4"):
                with self.subTest(spec_id=spec_id, name=name):
                    declared = re.search(rf"`{name}` \(`\[([0-9,]+)\]`\)", text).group(1)
                    shape = np.array(sentinels[name]).shape
                    self.assertEqual(tuple(int(e) for e in declared.split(",")), shape)
                    # `maxabs` refuses an extent-1 axis, so a sentinel with one fails a correct
                    # harness (measured on `[2,1,2,3]`).
                    self.assertNotIn(1, shape)
                    if len(shape) > 1:
                        self.assertGreater(len(set(shape)), 1, "extents that differ")

    def test_every_sentinel_is_judged_by_a_term_of_the_predicate(self) -> None:
        for spec_id in HARNESSES:
            with self.subTest(spec_id=spec_id):
                sentinels = _sentinels(spec_id, "l0_read_case_roundtrip_pass")
                self.assertEqual(set(sentinels), {
                    "rc_real", "rc_whole", "rc_int", "rc_true", "rc_false", "rc_empty",
                    "rc_r1", "rc_r2", "rc_r3", "rc_r4"})
                predicates = " ; ".join(_predicates(spec_id, "l0_read_case_roundtrip_pass"))
                for name in sentinels:
                    if name == "rc_empty":
                        self.assertIn("final.rc_empty_read == 1.0", predicates)
                    elif name in ("rc_true", "rc_false"):
                        self.assertIn(f"final.{name} == 1.0", predicates)
                    elif name in ("rc_real", "rc_whole", "rc_int"):
                        self.assertIn(f"abs(final.{name} - inputs.initial.{name})", predicates)
                    else:
                        self.assertIn(f"maxabs(final.{name} - inputs.initial.{name})", predicates)
                for name in ("rc_e2", "rc_e3", "rc_e4"):
                    self.assertIn(f"abs(final.{name} - 2.0)", predicates)
                second = " ; ".join(_predicates(spec_id, "l0_read_case_second_pass"))
                declared = _sentinels(spec_id, "l0_read_case_second_pass")
                self.assertEqual(set(declared), {"rc_real", "rc_r1"})
                self.assertIn("abs(final.rc_real - inputs.initial.rc_real)", second)
                self.assertIn("maxabs(final.rc_r1 - inputs.initial.rc_r1)", second)
                # the second case differs from the first in every value it declares
                self.assertNotEqual(declared["rc_real"], sentinels["rc_real"])
                self.assertTrue(all(a != b for a, b in zip(declared["rc_r1"], sentinels["rc_r1"])))

    def test_each_sentinel_keeps_the_token_it_exists_for(self) -> None:
        for spec_id in HARNESSES:
            with self.subTest(spec_id=spec_id):
                s = _sentinels(spec_id, "l0_read_case_roundtrip_pass")
                self.assertGreaterEqual(len(repr(s["rc_real"])), 17, "a token a narrow buffer cuts")
                self.assertIs(type(s["rc_whole"]), int, "an integer token read as a real")
                self.assertLess(s["rc_int"], 0, "the sign of an integer token")
                self.assertIs(s["rc_true"], True)
                self.assertIs(s["rc_false"], False)
                self.assertEqual(s["rc_empty"], [])
                self.assertEqual(s["rc_r1"], [-1.5, 1.0e-30, 1.0e+30], "both exponent signs")
                self.assertTrue(all(isinstance(v, int) for v in s["rc_r2"][0]), "integer tokens")

    def test_the_tests_keep_their_outcome_and_the_absent_id_names_no_case(self) -> None:
        for spec_id in HARNESSES:
            with self.subTest(spec_id=spec_id):
                text = _tests_md(spec_id)
                outcomes = dict(re.findall(
                    r"^- `test_id`: `([^`]+)`\n  - `level`: `L0`\n  - `operation_id`: `[^`]+`[^\n]*\n"
                    r"  - `expected_outcome`: `([a-z]+)`", text, re.M))
                self.assertEqual(outcomes["l0_read_case_roundtrip_pass"], "pass")
                self.assertEqual(outcomes["l0_read_case_second_pass"], "pass")
                self.assertEqual(outcomes["l0_missing_case_file_xfail"], "xfail")
                self.assertIn("l0_no_such_case_file", text)
                self.assertNotIn("l0_no_such_case_file", outcomes)
                row = text[text.index("- `test_id`: `l0_read_case_second_pass`"):]
                self.assertIn(f"`{spec_id}__read_case`", row[:row.index("`expected_outcome`")])

    def test_the_seeded_contract_states_what_the_specs_state(self) -> None:
        """A producer reads RUNNER_OUTPUT_CONTRACT §6 and not `controlled_spec.md`; Compile and the
        reviewer read the spec. Each pair below is one rule in its two statements. A sample of the
        rules, not their set: a rule stated in neither is out of its reach."""
        contract = (ROOT / "docs/workflow/RUNNER_OUTPUT_CONTRACT.md").read_text(encoding="utf-8")
        section = " ".join(contract[contract.index("## 6. The case file"):].split())
        rules = (
            ("the token `1` (true) or `0` (false)",) * 2,
            ("holds no default and takes no `ok`",) * 2,
            ("no value of an earlier case remains readable",) * 2,
            ("so `[]` reads at every rank and `[[],[]]` at rank 2 and above",) * 2,
            ("the leading array index", "the leading array index"),
            ("(no fraction", "(no fraction"),
            ("stops the program with a non-zero exit status",) * 2,
            ("a scalar asked as an array or an array as a scalar",) * 2,
            ("does not convert to a finite binary64 value",) * 2,
            ("so its width is not `case_id_len`",) * 2,
        )
        for spec_id in HARNESSES:
            spec = " ".join((_HARNESS_DIR / spec_id / "controlled_spec.md")
                            .read_text(encoding="utf-8").split())
            for in_contract, in_spec in rules:
                with self.subTest(spec_id=spec_id, rule=in_contract):
                    self.assertIn(in_contract, section)
                    self.assertIn(in_spec, spec)
        # the reversals of what §6 forbids are absent
        for reversal in ("returns a default", "keeps the previous case"):
            self.assertNotIn(reversal, section)

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


SENTINELS = _sentinels("harness_fortran_cpu", "l0_read_case_roundtrip_pass")
SECOND = _sentinels("harness_fortran_cpu", "l0_read_case_second_pass")


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
            {"case_id": "second", "inputs": {"initial": SECOND}},
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
        final = {**scalar, **arrays, "rc_empty_read": 1.0, "rc_e2": float(arrays["rc_r2"][0, 1]),
                 "rc_e3": float(arrays["rc_r3"][0, 0, 1]),
                 "rc_e4": float(arrays["rc_r4"][0, 0, 0, 1])}
        expressions = _predicates("harness_fortran_cpu", "l0_read_case_roundtrip_pass")
        self.assertEqual(len(expressions), 5)
        env = pe.CaseEnv(case_id="c1", initial=None, final=final,
                         inputs={"initial": SENTINELS}, coordinates={})
        for expression in expressions:
            with self.subTest(expression=expression[:40]):
                left, right = expression.rsplit(" == ", 1)
                self.assertEqual(float(pe.evaluate(pe.parse_expr(left), env)), float(right))

    def test_a_second_case_read_after_the_first_holds_its_own_values(self) -> None:
        K = "inputs.initial."
        res = self._run("read:c1", f"real:{K}rc_real", "read:second", f"real:{K}rc_real",
                        f"r1:{K}rc_r1")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        lines = res.stdout.splitlines()
        env = pe.CaseEnv(case_id="second", initial=None, inputs={"initial": SECOND}, coordinates={},
                         final={"rc_real": float(lines[3].split()[-1]),
                                "rc_r1": np.array([float(v) for v in lines[5].split()])})
        expressions = _predicates("harness_fortran_cpu", "l0_read_case_second_pass")
        self.assertEqual(len(expressions), 1)
        left, right = expressions[0].rsplit(" == ", 1)
        self.assertEqual(float(pe.evaluate(pe.parse_expr(left), env)), float(right))
        self.assertNotEqual(float(lines[1].split()[-1]), float(lines[3].split()[-1]))

    def test_a_placeholder_does_not_satisfy_the_tests(self) -> None:
        """`tests.md` §4: a case that does not fill its variables fails its test."""
        for test_id in ("l0_read_case_roundtrip_pass", "l0_read_case_second_pass",
                        "l0_missing_case_file_xfail"):
            sentinels = {"l0_read_case_roundtrip_pass": SENTINELS,
                         "l0_read_case_second_pass": SECOND}.get(test_id, {})
            final = {name: np.zeros(np.array(value).shape) for name, value in SENTINELS.items()
                     if isinstance(value, list) and value}
            final.update({name: 0.0 for name in (
                "rc_real", "rc_whole", "rc_int", "rc_true", "rc_false", "rc_empty_read",
                "rc_e2", "rc_e3", "rc_e4", "read_refused")})
            env = pe.CaseEnv(case_id="x", initial=None, final=final,
                             inputs={"initial": sentinels}, coordinates={})
            text = _tests_md("harness_fortran_cpu")
            row = text[text.index(f"- `test_id`: `{test_id}`") + 1:]
            found = re.search(r"`final\.read_refused == 1\.0`", row[:row.index("## 7.")])
            expressions = (["final.read_refused == 1.0"] if test_id.endswith("xfail") and found
                           else _predicates("harness_fortran_cpu", test_id))
            self.assertTrue(expressions)
            verdicts = []
            for expression in expressions:
                left, right = expression.rsplit(" == ", 1)
                verdicts.append(float(pe.evaluate(pe.parse_expr(left), env)) == float(right))
            with self.subTest(test_id=test_id):
                self.assertFalse(all(verdicts))

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

    def test_a_parse_with_a_path_and_no_case_keeps_the_earlier_path(self) -> None:
        res = self._run("short_parse:/nowhere/spec.ir.yaml", "read:c3")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.split(), ["short_parse", "F", "read", "T"])

    def test_a_document_refused_by_grammar_leaves_no_part_of_itself_readable(self) -> None:
        self._write_case("partial", '{"early":1,"late":"x"}\n')
        res = self._run("read:partial", "real:early")
        self.assertNotEqual(res.returncode, 0, res.stdout)
        self.assertIn("no case is loaded", res.stdout)

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
                ("two as a bool", f"read:c1|bool:{K}rc_whole", "tokens 1 and 0"),
                ("a negative as a bool", f"read:c1|bool:{K}rc_int", "tokens 1 and 0")):
            with self.subTest(label):
                res = self._run(*command.split("|"))
                self.assertNotEqual(res.returncode, 0, res.stdout)
                self.assertIn(why, res.stdout)

    def test_an_integer_that_overflows_the_default_integer_stops_the_getter(self) -> None:
        self._write_case("big", '{"n":99999999999}\n')
        res = self._run("read:big", "int:n")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("integer token", res.stdout)

    def test_only_the_exact_tokens_read_as_a_bool(self) -> None:
        self._write_case("bools", '{"neg":-0,"whole":1.0,"exp":1e0,"big":99999999999}\n')
        for name in ("neg", "whole", "exp", "big"):
            with self.subTest(name):
                res = self._run("read:bools", f"bool:{name}")
                self.assertNotEqual(res.returncode, 0, res.stdout)
                self.assertIn("tokens 1 and 0", res.stdout)

    def test_a_document_outside_the_grammar_is_refused_by_read_case(self) -> None:
        for label, text in (
                ("not an object", "[1]"), ("truncated", '{"a":1'), ("empty file", ""),
                ("nested object", '{"a":{"b":1}}'), ("string", '{"a":"x"}'),
                ("true", '{"a":true}'), ("null", '{"a":null}'),
                ("NaN", '{"a":NaN}'), ("Infinity", '{"a":[1,Infinity]}'), ("-Infinity", '{"a":-Infinity}'),
                ("ragged", '{"a":[[1,2],[3]]}'), ("ragged empty", '{"a":[[1],[]]}'),
                ("mixed depth", '{"a":[1,[2]]}'), ("mixed depth reversed", '{"a":[[1],2]}'),
                ("depth five", '{"a":[[[[[1]]]]]}'), ("escape", '{"a\\"b":1}'),
                ("escape that closes no string", '{"a\\nb":1}'), ("number beside empty", '{"a":[[],2]}'),
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
