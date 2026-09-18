"""Unit tests for the host-evaluated primary predicates (Z6, issue #255, PR-2).

What is PINNED here and what is SAMPLED (`atmofab-enforcement-change` §4):

* The grammar allowlist is pinned by ELEMENT: every `ast` expression node type the Python
  grammar can produce is driven through `parse_expr`, and the set that parses must equal the
  set the module admits — a node type added to `ast` in a later interpreter fails the row
  rather than passing silently.
* Every evaluation error class the module docstring names (rank, ragged, non-numeric,
  non-finite, absent file, absent variable, non-integer `roll`, array result, unequal-rank
  operands, non-finite intermediate, forward bind, `at` outside targets, non-numeric input)
  has its own probe, and each is a STRUCTURAL record rather than a raise.
* `evaluate_verdict` with `primary=` is pinned in both directions: the two fixtures the plan
  names — (a) a diagnostics that passes every secondary condition while the captured state
  fails the corroborant, and (c) one decoy case among two — fail the test with
  `corroboration=disagree`, and an IR with no primary predicate produces the byte-identical
  document `evaluate_verdict` produced before this module existed.

The captures are SYNTHETIC (a seeded numpy array written in the runner's JSON shape). The
recorded-run fixture the plan names (a `shallow_water2d` n032 case with its `initial/`
capture) does not exist until the Z6 PR-1 billed run has been made; the CLI
(`python3 -m tools.primary_evidence --ir ... --run ...`) is the offline measurement against
it, and the value agreement with `diagnostics.json` is measured there, not here.
"""

from __future__ import annotations

import ast
import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

import numpy as np

from tools import primary_evidence as pe
from tools.verdict_evaluator import PredicateError, evaluate_verdict

NX, NY = 8, 4


def _case(cid: str, **initial: float) -> dict:
    return {"case_id": cid, "inputs": {
        "grid": {"nx": NX, "ny": NY, "dx": 1.0 / NX, "dy": 1.0 / NY, "L_x": 1.0, "L_y": 1.0,
                 "arrangement": "uniform"},
        "initial": {"shift": 0.0, "profile": "wave", **initial},
        "flag": True,
    }}


def _ir(primary: list[dict] | None, *, coordinates: list[dict] | None = None,
        cases: list[dict] | None = None) -> dict:
    schema = {"variables": [{"name": "h", "shape_expr": "[nx, ny]"},
                            {"name": "s", "shape_expr": "scalar"}],
              "time_variable": "t", "time_shape_expr": "scalar"}
    if coordinates is not None:
        schema["coordinates"] = coordinates
    io_contract = {
        "raw_requirements": {"required_evidence": [
            {"artifact": "state_snapshots", "required": True, "min_samples": 1,
             "schema": schema}]},
        "test_predicates": [
            {"test_id": "t_mass", "expected_outcome": "pass", "target_cases": ["a", "b"],
             "pass_when": {"all": [
                 {"ref": "checks.mass.status", "op": "eq", "value": "pass", "per_case": True,
                  "quantity": "mass_drift_rel"}]}},
            {"test_id": "t_sym", "expected_outcome": "pass", "target_cases": ["a", "b"],
             "pass_when": {"all": [
                 {"ref": "errors.sym", "op": "le", "value": 1e-12, "case": "a"}]}},
        ],
    }
    if primary is not None:
        io_contract["primary_predicates"] = primary
    return {"case": {"test_case_set": cases if cases is not None else [_case("a"), _case("b")]},
            "io_contract": io_contract}


def _diag_all_pass() -> dict:
    return {"per_case": {
        "a": {"checks": {"mass": {"status": "pass"}}, "metrics": {"errors.sym": 0.0}},
        "b": {"checks": {"mass": {"status": "pass"}}, "metrics": {"errors.sym": 0.0}},
    }}


class _RunDir:
    """A run node directory with the two captures of every case written in the runner's
    JSON shape (leading index outermost)."""

    def __init__(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.sdir = self.root / "raw" / "state_snapshots"
        (self.sdir / "initial").mkdir(parents=True)

    def write(self, cid: str, *, initial: dict, final: dict) -> None:
        (self.sdir / "initial" / f"{cid}.json").write_text(json.dumps(initial))
        (self.sdir / f"{cid}.json").write_text(json.dumps(final))

    def write_state(self, cid: str, h0: np.ndarray, h1: np.ndarray, *, s: float = 1.0) -> None:
        self.write(cid, initial={"h": h0.tolist(), "s": s, "t": 0.0},
                   final={"h": h1.tolist(), "s": s, "t": 0.2})

    def cleanup(self) -> None:
        self._td.cleanup()


def _field(seed: int = 0) -> np.ndarray:
    return np.random.default_rng(seed).uniform(0.6, 1.0, (NX, NY))


MASS = {"test_id": "t_mass", "quantity": "mass_drift_rel", "target_cases": ["a", "b"],
        "bind": {"M0": "sum(initial.h) * inputs.grid.dx * inputs.grid.dy",
                 "M1": "sum(final.h) * inputs.grid.dx * inputs.grid.dy"},
        "expr": "abs(M1 - M0) / max(abs(M0), 1e-14)", "op": "le", "value": 1.0e-10,
        "per_case": True}
HMIN = {"test_id": "t_mass", "quantity": "h_min", "target_cases": ["a", "b"],
        "expr": "min(final.h)", "op": "ge", "value": 0.5, "per_case": True}
SYM = {"test_id": "t_sym", "quantity": "sym", "target_cases": ["a", "b"],
       "expr": "norm2(final.h - roll(at('b').final.h, inputs.initial.shift * inputs.grid.nx, 0))"
               " / norm2(final.h)", "op": "le", "value": 1e-12, "case": "a"}


# ------------------------------------------------------------------------------ grammar

class GrammarAllowlistTest(unittest.TestCase):
    """The allowlist is pinned by ELEMENT over every expression node type `ast` defines."""

    # One source per expression node type. A node type this table does not name is caught by
    # `test_every_ast_expression_node_type_is_classified`.
    _SOURCES: ClassVar[dict[str, str]] = {
        "BinOp": "1 + 2", "UnaryOp": "-1", "Constant": "1.5", "Name": "pi",
        "Attribute": "final.h", "Call": "abs(1)",
        "BoolOp": "1 and 2", "Compare": "1 < 2", "IfExp": "1 if 2 else 3",
        "Lambda": "lambda: 1", "Dict": "{1: 2}", "Set": "{1}", "List": "[1]",
        "Tuple": "(1, 2)", "Subscript": "final.h[0]", "Starred": "abs(*final.h)",
        "ListComp": "[x for x in final.h]", "SetComp": "{x for x in final.h}",
        "DictComp": "{x: 1 for x in final.h}", "GeneratorExp": "(x for x in final.h)",
        "Await": "await abs(1)", "Yield": "(yield 1)", "YieldFrom": "(yield from x)",
        "JoinedStr": "f'{1}'", "FormattedValue": "f'{1}'", "NamedExpr": "(x := 1)",
        "Slice": "final.h[0:1]",
    }
    _ADMITTED: ClassVar[set[str]] = {"BinOp", "UnaryOp", "Constant", "Name", "Attribute", "Call"}

    def test_every_ast_expression_node_type_is_classified(self) -> None:
        expr_types = {
            name for name, obj in vars(ast).items()
            if isinstance(obj, type) and issubclass(obj, ast.expr) and obj is not ast.expr
            and not name.startswith("_")
        }
        # `ast.expr` subclasses that are not writable expressions in `mode="eval"` source
        # (deprecated aliases and the interactive-only ones) are excluded by name; the rest
        # must have a row.
        legacy = {"Num", "Str", "Bytes", "NameConstant", "Ellipsis", "Interpolation",
                  "TemplateStr"}
        missing = expr_types - legacy - set(self._SOURCES)
        self.assertEqual(missing, set(), f"add a source for {sorted(missing)}")

    def test_only_the_admitted_node_types_parse(self) -> None:
        parsed = set()
        for name, src in self._SOURCES.items():
            try:
                pe.parse_expr(src)
            except pe.PrimaryEvidenceError:
                continue
            parsed.add(name)
        self.assertEqual(parsed, self._ADMITTED)

    def test_operator_allowlist(self) -> None:
        for src in ("1 % 2", "1 // 2", "1 @ 2", "1 << 2", "1 >> 2", "1 | 2", "1 & 2", "1 ^ 2",
                    "not 1", "~1"):
            with self.subTest(src=src), self.assertRaises(pe.PrimaryEvidenceError):
                pe.parse_expr(src)
        for src in ("1 + 2", "1 - 2", "1 * 2", "1 / 2", "1 ** 2", "-1", "+1"):
            with self.subTest(src=src):
                pe.parse_expr(src)

    def test_constants_are_numbers_only(self) -> None:
        for src in ("'x'", "True", "None", "b'x'", "..."):
            with self.subTest(src=src), self.assertRaises(pe.PrimaryEvidenceError):
                pe.parse_expr(src)

    def test_function_table_is_closed(self) -> None:
        for src in ("__import__('os')", "open('x')", "eval('1')", "getattr(final, 'h')",
                    "np.sum(final.h)", "final.h.sum()", "where(1, 2, 3)"):
            with self.subTest(src=src), self.assertRaises(pe.PrimaryEvidenceError):
                pe.parse_expr(src)
        for name, (lo, hi) in pe.FUNCTIONS.items():
            with self.subTest(name=name):
                pe.parse_expr(f"{name}({', '.join(['1'] * lo)})")
                with self.assertRaises(pe.PrimaryEvidenceError):
                    pe.parse_expr(f"{name}({', '.join(['1'] * (hi + 1))})")
                if lo > 0:
                    with self.assertRaises(pe.PrimaryEvidenceError):
                        pe.parse_expr(f"{name}({', '.join(['1'] * (lo - 1))})")
        with self.assertRaises(pe.PrimaryEvidenceError):
            pe.parse_expr("abs(x=1)")

    def test_attribute_roots(self) -> None:
        for src in ("final.h", "initial.t", "inputs.grid.nx", "at('a').final.h",
                    "at('a').initial.h"):
            with self.subTest(src=src):
                pe.parse_expr(src)
        for src in ("final.h.T", "final", "at('a')", "at('a').h", "at('a').final",
                    "at('a').final.h.T", "at(1).final.h", "at('').final.h", "at().final.h",
                    "at('a', 'b').final.h", "pi.real", "abs(1).real", "x.y", "final.h.shape",
                    "at(case='a').final.h", "np.pi", "__builtins__.open"):
            with self.subTest(src=src), self.assertRaises(pe.PrimaryEvidenceError):
                pe.parse_expr(src)

    def test_non_string_and_unparsable(self) -> None:
        for text in (None, 3, "", "  ", "1 +", "import os", "1; 2"):
            with self.subTest(text=text), self.assertRaises(pe.PrimaryEvidenceError):
                pe.parse_expr(text)

    def test_expr_names_lists_every_reference(self) -> None:
        refs = pe.expr_names(pe.parse_expr(
            "final.h + at('b').initial.s * inputs.grid.dx + x + M0 + pi"))
        self.assertEqual(refs, [
            pe.NameRef("capture", "h", point="final"),
            pe.NameRef("capture", "s", point="initial", case="b"),
            pe.NameRef("inputs", "grid.dx"),
            pe.NameRef("name", "x"), pe.NameRef("name", "M0"), pe.NameRef("name", "pi"),
        ])


# --------------------------------------------------------------------------- evaluation

class EvaluationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.run = _RunDir()
        self.addCleanup(self.run.cleanup)
        self.h = _field()
        self.run.write_state("b", self.h, self.h)
        self.run.write_state("a", np.roll(self.h, 2, axis=0), np.roll(self.h, 2, axis=0))
        self.cases = [_case("a", shift=0.25), _case("b")]

    def _eval(self, preds: list[dict], **kw) -> list[dict]:
        return pe.evaluate_primary_predicates(_ir(preds, cases=self.cases, **kw), self.run.root)

    def test_reference_predicates_pass_and_carry_values(self) -> None:
        out = self._eval([MASS, HMIN, SYM])
        self.assertEqual([r["satisfied"] for r in out], [True, True, True])
        self.assertEqual([r["kind"] for r in out], ["pass"] * 3)
        self.assertEqual(out[0]["evaluated"][0]["value"], 0.0)
        self.assertAlmostEqual(out[1]["evaluated"][0]["value"], float(self.h.min()))
        self.assertEqual([e["case"] for e in out[2]["evaluated"]], ["a"])
        self.assertEqual(out[0]["grammar_version"], pe.GRAMMAR_VERSION)
        self.assertEqual(out[0]["scope"], "per_case")
        self.assertEqual(out[2]["scope"], "case")

    def test_deterministic(self) -> None:
        self.assertEqual(self._eval([MASS, HMIN, SYM]), self._eval([MASS, HMIN, SYM]))

    def test_no_primary_predicates_is_empty(self) -> None:
        self.assertEqual(pe.evaluate_primary_predicates(_ir(None), self.run.root), [])
        self.assertEqual(pe.evaluate_primary_predicates(_ir([]), self.run.root), [])

    def test_physics_fail_names_the_case_and_stops(self) -> None:
        strict = {**HMIN, "value": {"per_case": {"a": 0.5, "b": 2.0}}}
        [rec] = self._eval([strict])
        self.assertFalse(rec["satisfied"])
        self.assertEqual(rec["kind"], "physics")
        self.assertEqual([e["case"] for e in rec["evaluated"]], ["a", "b"])
        self.assertEqual(rec["evaluated"][1]["rhs"], 2.0)
        self.assertFalse(rec["evaluated"][1]["satisfied"])

    def test_coordinates_broadcast_along_their_axis(self) -> None:
        coords = [{"name": "x", "axis": 0, "count": "inputs.grid.nx", "length": "inputs.grid.L_x",
                   "placement": "cell_center"},
                  {"name": "y", "axis": 1, "count": NY, "length": 2.0, "placement": "cell_center"}]
        pred = {"test_id": "t_mass", "quantity": "q", "target_cases": ["b"],
                "expr": "sum(final.h * (x + y)) - sum(final.h * x) - sum(final.h * y)",
                "op": "eq", "value": 0.0, "case": "b"}
        [rec] = self._eval([pred], coordinates=coords)
        self.assertTrue(rec["satisfied"], rec)
        env = pe.load_case_env(self.run.root, self.cases[1],
                               pe.snapshot_schema(_ir([], coordinates=coords)))
        self.assertEqual(env.coordinates["x"].shape, (NX, 1))
        self.assertEqual(env.coordinates["y"].shape, (1, NY))
        np.testing.assert_allclose(env.coordinates["x"].ravel(), (np.arange(NX) + 0.5) / NX)
        np.testing.assert_allclose(env.coordinates["y"].ravel(), (np.arange(NY) + 0.5) * 2 / NY)

    def _structural(self, pred: dict, fragment: str, **kw) -> dict:
        [rec] = self._eval([pred], **kw)
        self.assertFalse(rec["satisfied"])
        self.assertEqual(rec["kind"], "structural", rec)
        ev = rec["evaluated"][-1]
        self.assertEqual(ev["reason"], "evaluation_error")
        self.assertIn(fragment, ev["error"])
        return rec

    def _one(self, expr: str, **extra) -> dict:
        return {"test_id": "t_mass", "quantity": "q", "target_cases": ["a", "b"],
                "expr": expr, "op": "le", "value": 1e9, "case": "b", **extra}

    def test_array_result_is_structural(self) -> None:
        self._structural(self._one("final.h - initial.h"), "not a scalar")

    def test_unequal_rank_operands_are_structural(self) -> None:
        # a rank-1 array against a rank-2 one: numpy would right-align; refused instead
        ir = _ir([self._one("sum(final.h + final.v)")], cases=self.cases)
        ir["io_contract"]["raw_requirements"]["required_evidence"][0]["schema"]["variables"] \
            .append({"name": "v", "shape_expr": "[ny]"})
        for path in (self.run.sdir / "initial" / "a.json", self.run.sdir / "a.json",
                     self.run.sdir / "initial" / "b.json", self.run.sdir / "b.json"):
            doc = json.loads(path.read_text())
            doc["v"] = [1.0] * NY
            path.write_text(json.dumps(doc))
        [rec] = pe.evaluate_primary_predicates(ir, self.run.root)
        self.assertEqual(rec["kind"], "structural")
        self.assertIn("rank 2 and 1", rec["evaluated"][-1]["error"])

    def test_non_finite_intermediate_is_structural(self) -> None:
        self._structural(self._one("sum(final.h) / (final.s - final.s)"), "non-finite")
        self._structural(self._one("log(final.s - final.s)"), "non-finite")
        self._structural(self._one("sqrt(-final.s)"), "non-finite")

    def test_roll_shift_must_be_integer_and_per_axis(self) -> None:
        self._structural(self._one("sum(roll(final.h, 1.5, 0))"), "not an integer")
        self._structural(self._one("sum(roll(final.h, 1))"), "1 shift(s) for an array of rank 2")
        self._structural(self._one("sum(roll(final.h, final.h, 0))"), "must be a scalar")
        [rec] = self._eval([self._one("sum(abs(roll(final.h, 2.0, 0) - at('a').final.h))",
                                      op="le", value=0.0)])
        self.assertTrue(rec["satisfied"], rec)

    def test_at_outside_target_cases_is_structural(self) -> None:
        pred = {**self._one("sum(at('a').final.h)"), "target_cases": ["b"]}
        self._structural(pred, "outside this predicate's target_cases")

    def test_input_path_must_be_a_number(self) -> None:
        self._structural(self._one("inputs.grid.arrangement"), "not a number")
        self._structural(self._one("inputs.flag"), "not a number")
        self._structural(self._one("inputs.grid"), "not a number")
        self._structural(self._one("inputs.grid.missing"), "not a key")

    def test_unknown_name_and_capture_variable(self) -> None:
        self._structural(self._one("final.zeta"), "not a snapshot schema variable")
        self._structural(self._one("nope"), "not a bind, a coordinate or a constant")

    def test_binds_evaluate_in_order_and_cannot_look_forward(self) -> None:
        pred = self._one("B", bind={"A": "sum(final.h)", "B": "A * 2"})
        [rec] = self._eval([pred])
        self.assertAlmostEqual(rec["evaluated"][0]["value"], 2 * float(self.h.sum()))
        self._structural(self._one("B", bind={"B": "A * 2", "A": "sum(final.h)"}),
                         "'A' is not a bind")
        self._structural(self._one("pi", bind={"pi": "1"}), "shadows a grammar name")
        self._structural(self._one("1", bind=[1]), "bind must be a mapping")

    def test_capture_file_defects_are_structural(self) -> None:
        (self.run.sdir / "initial" / "b.json").unlink()
        self._structural(self._one("final.s"), "is absent")
        self.run.write("b", initial={"h": self.h.tolist(), "t": 0.0},
                       final={"h": self.h.tolist(), "s": 1.0, "t": 0.2})
        self._structural(self._one("final.s"), "'s' is not captured")
        bad = self.h.tolist()
        bad[0] = bad[0][:-1]
        self.run.write("b", initial={"h": bad, "s": 1.0, "t": 0.0},
                       final={"h": bad, "s": 1.0, "t": 0.2})
        self._structural(self._one("final.s"), "not a rectangular numeric array")
        self.run.write("b", initial={"h": [[1.0] * NY] * NX, "s": 1.0, "t": 0.0},
                       final={"h": [[None] * NY] * NX, "s": 1.0, "t": 0.2})
        self._structural(self._one("final.s"), "non-numeric")
        self.run.write("b", initial={"h": [1.0] * NX, "s": 1.0, "t": 0.0},
                       final={"h": [1.0] * NX, "s": 1.0, "t": 0.2})
        self._structural(self._one("final.s"), "has rank 1, declared shape_expr has rank 2")
        nan = self.h.tolist()
        nan[0][0] = float("nan")
        self.run.write("b", initial={"h": nan, "s": 1.0, "t": 0.0},
                       final={"h": nan, "s": 1.0, "t": 0.2})
        self._structural(self._one("final.s"), "non-finite")
        self.run.write("b", initial={"h": self.h.tolist(), "s": True, "t": 0.0},
                       final={"h": self.h.tolist(), "s": 1.0, "t": 0.2})
        self._structural(self._one("final.s"), "non-numeric")
        self.run.write_state("b", self.h, self.h)
        (self.run.sdir / "b.json").write_text("[]")
        self._structural(self._one("final.s"), "not a JSON object")
        (self.run.sdir / "b.json").write_text("{")
        self._structural(self._one("final.s"), "unreadable")

    def test_repeated_dimension_token_requires_equal_extents(self) -> None:
        ir = _ir([self._one("final.s")], cases=self.cases)
        ir["io_contract"]["raw_requirements"]["required_evidence"][0]["schema"]["variables"][0] \
            ["shape_expr"] = "[n, n]"
        [rec] = pe.evaluate_primary_predicates(ir, self.run.root)
        self.assertEqual(rec["kind"], "structural")
        self.assertIn("unequal extents", rec["evaluated"][-1]["error"])
        ir["io_contract"]["raw_requirements"]["required_evidence"][0]["schema"]["variables"][0] \
            ["shape_expr"] = f"[{NX}, 3]"
        [rec] = pe.evaluate_primary_predicates(ir, self.run.root)
        self.assertIn("!= declared 3", rec["evaluated"][-1]["error"])

    def test_coordinate_defects_are_structural(self) -> None:
        base = {"name": "x", "count": NX, "length": 1.0, "placement": "cell_center"}
        for coords, fragment in (
            ([{**base, "axis": 2}], "axis must be an integer in [0, 2)"),
            ([{**base, "axis": True}], "axis must be an integer"),
            ([{**base, "axis": 0, "placement": "node"}], "placement must be one of"),
            ([{**base, "axis": 0, "count": 2.5}], "not a positive integer"),
            ([{**base, "axis": 0, "length": 0}], "not a positive number"),
            ([{**base, "axis": 0, "count": "inputs.grid.arrangement"}], "not a number"),
            ([{**base, "axis": 0, "count": "nx"}], "must be a number or inputs.<path>"),
            (["x"], "must be mappings"),
        ):
            with self.subTest(coords=coords):
                self._structural(self._one("x"), fragment, coordinates=coords)

    def test_malformed_predicate_shape_raises(self) -> None:
        for pred, fragment in (
            ({**HMIN, "test_id": ""}, "test_id"),
            ({**HMIN, "quantity": "Bad"}, "quantity"),
            ({**HMIN, "op": "includes"}, "op must be one of"),
            ({**HMIN, "value": None}, "non-null value"),
            ({**HMIN, "target_cases": []}, "target_cases"),
            ({k: v for k, v in HMIN.items() if k != "per_case"}, "exactly one of"),
            ({**HMIN, "case": "a"}, "exactly one of"),
            ({**SYM, "case": "zz"}, "not one of its target_cases"),
            ({**HMIN, "expr": "final.h["}, "does not parse"),
            ("x", "must be a mapping"),
        ):
            with self.subTest(pred=pred), self.assertRaisesRegex(pe.PrimaryEvidenceError, fragment):
                self._eval([pred])
        ir = _ir([HMIN])
        del ir["io_contract"]["raw_requirements"]
        with self.assertRaisesRegex(pe.PrimaryEvidenceError, "no state_snapshots"):
            pe.evaluate_primary_predicates(ir, self.run.root)
        # a target case outside test_case_set is met per case, so it is a structural RECORD
        self._structural({**HMIN, "target_cases": ["a", "zz"]}, "not in case.test_case_set")

    def test_value_map_needs_every_case_and_a_number(self) -> None:
        self._structural({**HMIN, "value": {"per_case": {"a": 0.5}}}, "no entry for 'b'")
        self._structural({**HMIN, "value": "0.5"}, "is not a number")

    def test_function_semantics(self) -> None:
        h = self.h
        for expr, expected in (
            ("mean(final.h)", h.mean()), ("maxabs(-final.h)", np.abs(h).max()),
            ("norm2(final.h)", np.sqrt((h * h).sum())), ("max(final.h)", h.max()),
            ("min(sum(final.h), 1, 2)", min(h.sum(), 1, 2)),
            ("max(min(final.h), 5)", 5.0), ("ceil(2.1) + floor(2.9)", 5.0),
            ("exp(0) + log(e) + log2(8) + sin(0) + cos(0) + 2 ** 3", 1 + 1 + 3 + 0 + 1 + 8),
            ("final.t - initial.t", 0.2),
        ):
            with self.subTest(expr=expr):
                [rec] = self._eval([self._one(expr)])
                self.assertAlmostEqual(rec["evaluated"][0]["value"], float(expected), places=12)


# ------------------------------------------------------------------------ verdict fold

class VerdictIntegrationTest(unittest.TestCase):
    """`evaluate_verdict(primary=...)`: the plan's fixtures (a) and (c), and the no-primary
    identity."""

    def setUp(self) -> None:
        self.run = _RunDir()
        self.addCleanup(self.run.cleanup)
        self.h = _field()
        self.run.write_state("a", self.h, self.h)
        self.run.write_state("b", self.h, self.h)
        self.ir = _ir([MASS, HMIN, SYM])
        self.predicates = self.ir["io_contract"]["test_predicates"]

    def _verdict(self) -> dict:
        primary = pe.evaluate_primary_predicates(self.ir, self.run.root)
        return evaluate_verdict(self.predicates, _diag_all_pass(), run_id="r", node_key="n",
                                primary=primary)

    def test_agreeing_evidence_passes_with_basis(self) -> None:
        doc = self._verdict()
        self.assertEqual(doc["self_verdict"], "pass")
        self.assertEqual(doc["failure_class"], "pass")
        for item in doc["per_test"]:
            self.assertEqual(item["basis"]["corroboration"], "agree")
            self.assertTrue(item["basis"]["primary"])
        self.assertEqual([r["quantity"] for r in doc["per_test"][0]["basis"]["primary"]],
                         ["mass_drift_rel", "h_min"])

    def test_fixture_a_tampered_state_fails_a_passing_diagnostics(self) -> None:
        # (a): every secondary condition passes; the captured final state lost mass in both
        # cases (the translation pair stays a pair, so t_sym's corroborant still holds).
        self.run.write_state("a", self.h, self.h * 0.9)
        self.run.write_state("b", self.h, self.h * 0.9)
        doc = self._verdict()
        self.assertEqual(doc["self_verdict"], "fail")
        self.assertEqual(doc["failure_class"], "physics_fail")
        mass = doc["per_test"][0]
        self.assertEqual(mass["status"], "fail")
        self.assertEqual(mass["basis"]["corroboration"], "disagree")
        self.assertTrue(mass["basis"]["satisfied"])       # the secondary half still held
        self.assertFalse(mass["basis"]["primary"][0]["satisfied"])
        self.assertEqual(mass["basis"]["primary"][0]["evaluated"][0]["case"], "a")
        self.assertEqual(doc["per_test"][1]["status"], "pass")
        self.assertEqual(doc["per_test"][1]["basis"]["corroboration"], "agree")

    def test_fixture_c_one_decoy_case_among_two(self) -> None:
        # (c): case b keeps initial == final (mass drift 0) while its depth breaks positivity.
        low = self.h.copy()
        low[3, 1] = 0.1
        self.run.write_state("b", low, low)
        doc = self._verdict()
        self.assertEqual(doc["self_verdict"], "fail")
        mass = doc["per_test"][0]
        self.assertEqual(mass["basis"]["corroboration"], "disagree")
        drift, hmin = mass["basis"]["primary"]
        self.assertTrue(drift["satisfied"])
        self.assertFalse(hmin["satisfied"])
        self.assertEqual([e["case"] for e in hmin["evaluated"]], ["a", "b"])
        self.assertFalse(hmin["evaluated"][1]["satisfied"])

    def test_structural_record_folds_to_structural_violation(self) -> None:
        (self.run.sdir / "initial" / "b.json").unlink()
        doc = self._verdict()
        self.assertEqual(doc["failure_class"], "structural_violation")
        self.assertEqual(doc["per_test"][0]["status"], "fail")

    def test_both_sides_failing_agree(self) -> None:
        self.run.write_state("a", self.h, self.h * 0.9)
        diag = _diag_all_pass()
        diag["per_case"]["a"]["checks"]["mass"]["status"] = "fail"
        primary = pe.evaluate_primary_predicates(self.ir, self.run.root)
        doc = evaluate_verdict(self.predicates, diag, primary=primary)
        self.assertEqual(doc["per_test"][0]["basis"]["corroboration"], "agree")
        self.assertEqual(doc["self_verdict"], "fail")

    def test_xfail_test_needs_both_halves(self) -> None:
        preds = copy.deepcopy(self.predicates)
        preds[0]["expected_outcome"] = "xfail"
        primary = pe.evaluate_primary_predicates(self.ir, self.run.root)
        self.assertEqual(evaluate_verdict(preds, _diag_all_pass(), primary=primary)
                         ["per_test"][0]["status"], "xfail")
        self.run.write_state("a", self.h, self.h * 0.9)
        primary = pe.evaluate_primary_predicates(self.ir, self.run.root)
        self.assertEqual(evaluate_verdict(preds, _diag_all_pass(), primary=primary)
                         ["per_test"][0]["status"], "fail")

    def test_no_primary_is_byte_identical(self) -> None:
        base = evaluate_verdict(self.predicates, _diag_all_pass(), run_id="r", node_key="n")
        for primary in (None, []):
            self.assertEqual(
                json.dumps(evaluate_verdict(self.predicates, _diag_all_pass(), run_id="r",
                                            node_key="n", primary=primary), sort_keys=True),
                json.dumps(base, sort_keys=True))
        self.assertNotIn("primary", base["per_test"][0]["basis"])
        self.assertNotIn("corroboration", base["per_test"][0]["basis"])

    def test_unknown_test_id_raises(self) -> None:
        primary = [{"test_id": "nope", "satisfied": True, "kind": "pass"}]
        with self.assertRaisesRegex(PredicateError, "no test_predicates entry"):
            evaluate_verdict(self.predicates, _diag_all_pass(), primary=primary)
        with self.assertRaises(PredicateError):
            evaluate_verdict(self.predicates, _diag_all_pass(), primary=["x"])


# ---------------------------------------------------------------------------- schema gate

class SchemaGateTest(unittest.TestCase):
    def _v(self, preds, *, ir: dict | None = None) -> list[str]:
        ir = ir or _ir(preds)
        cases = {c["case_id"]: c for c in ir["case"]["test_case_set"]}
        return pe.validate_primary_predicate_schema(
            preds, case_ids=set(cases), test_ids=["t_mass", "t_sym"],
            schema=pe.snapshot_schema(ir), cases=cases)

    def test_reference_set_is_valid(self) -> None:
        coords = [{"name": "x", "axis": 0, "count": "inputs.grid.nx", "length": "inputs.grid.L_x",
                   "placement": "cell_center"}]
        ir = _ir([MASS, HMIN, SYM], coordinates=coords)
        self.assertEqual(self._v([MASS, HMIN, SYM], ir=ir), [])
        self.assertEqual(self._v(None), [])
        self.assertEqual(self._v([]), [])

    def test_each_violation(self) -> None:
        rows = [
            ("x", "must be a list"),
            (["x"], "must be a mapping"),
            ([{**HMIN, "test_id": "zz"}], "not a tests.md test_id"),
            ([{**HMIN, "test_id": 1}], "test_id must be"),
            ([{**HMIN, "quantity": "Q"}], "quantity must match"),
            ([{**HMIN, "op": "includes"}], "op must be one of"),
            ([{**HMIN, "target_cases": ["a", "zz"]}], "unknown case_id ('zz')"),
            ([{**HMIN, "target_cases": []}], "non-empty list"),
            ([{**HMIN, "target_cases": [1]}], "non-empty list of case_id strings"),
            ([{k: v for k, v in HMIN.items() if k != "per_case"}], "exactly one of"),
            ([{**HMIN, "case": "a"}], "exactly one of"),
            ([{**SYM, "case": "b", "target_cases": ["a"]}], "not one of its target_cases"),
            ([{**HMIN, "na_allowed": True}], "na_allowed has no meaning"),
            ([{**HMIN, "value": None}], "non-null"),
            ([{**HMIN, "value": "0.5"}], "value must be a number"),
            ([{**HMIN, "value": {"x": 1}}], "must be a number or a"),
            ([{**SYM, "value": {"per_case": {"a": 1.0}}}], "per_case is not true"),
            ([{**HMIN, "value": {"per_case": {}}}], "non-empty map"),
            ([{**HMIN, "value": {"per_case": {"a": 1.0, "zz": 1.0}}}], "unknown case_id ('zz')"),
            ([{**HMIN, "value": {"per_case": {"a": "1"}}}], "must be a number"),
            ([{**HMIN, "value": {"per_case": {"a": 1.0}}}], "missing a threshold"),
            ([{**HMIN, "bind": [1]}], "bind must be a mapping"),
            ([{**HMIN, "bind": {"1x": "1"}}], "not an identifier"),
            ([{**HMIN, "bind": {"pi": "1"}}], "shadows a grammar name"),
            ([{**HMIN, "bind": {"A": "final.h["}}], "bind.A: expr does not parse"),
            ([{**HMIN, "expr": "final.h["}], ".expr: expr does not parse"),
            ([{**HMIN, "expr": "final.zeta"}], "not a snapshot schema variable"),
            ([{**HMIN, "expr": "sum(at('zz').final.h)"}], "at('zz') is not one of"),
            ([{**HMIN, "expr": "inputs.grid.arrangement"}], "in case 'a': inputs.grid.arrangement"),
            ([{**HMIN, "expr": "inputs.nope"}], "not a key"),
            ([{**HMIN, "expr": "zz"}], "name 'zz' is not a coordinate"),
            ([{**HMIN, "expr": "B", "bind": {"B": "A", "A": "1"}}],
             "bind 'A' is referenced before it is defined"),
        ]
        for preds, fragment in rows:
            with self.subTest(fragment=fragment):
                out = self._v(preds)
                self.assertTrue(any(fragment in m for m in out), (fragment, out))

    def test_a_bind_that_fails_to_parse_still_counts_as_defined(self) -> None:
        out = self._v([{**HMIN, "expr": "A", "bind": {"A": "final.h["}}])
        self.assertEqual(len(out), 1, out)
        self.assertIn("does not parse", out[0])

    def test_requires_a_snapshot_entry(self) -> None:
        ir = _ir([HMIN])
        del ir["io_contract"]["raw_requirements"]
        out = self._v([HMIN], ir=ir)
        self.assertEqual(len(out), 1)
        self.assertIn("requires a state_snapshots", out[0])

    def test_coordinates_resolve_in_every_case(self) -> None:
        coords = [{"name": "x", "axis": 0, "count": "inputs.grid.nx", "length": "inputs.grid.L_x",
                   "placement": "cell_center"}]
        ir = _ir([HMIN], coordinates=coords)
        del ir["case"]["test_case_set"][1]["inputs"]["grid"]["L_x"]
        out = self._v([HMIN], ir=ir)
        self.assertEqual(len(out), 1, out)
        self.assertIn("does not resolve in case 'b'", out[0])
        self.assertIn("inputs.grid.L_x", out[0])

    def test_time_variable_is_a_capture_name(self) -> None:
        self.assertEqual(self._v([{**HMIN, "expr": "final.t - initial.t"}]), [])


# ----------------------------------------------------------------------------------- CLI

class CliTest(unittest.TestCase):
    def test_main_prints_records_and_exit_code(self) -> None:
        run = _RunDir()
        self.addCleanup(run.cleanup)
        h = _field()
        run.write_state("a", h, h)
        run.write_state("b", h, h)
        ir_dir = run.root / "ir"
        ir_dir.mkdir()
        import yaml
        (ir_dir / "spec.ir.yaml").write_text(yaml.safe_dump(_ir([MASS, HMIN])))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = pe.main(["--ir", str(ir_dir), "--run", str(run.root)])
        self.assertEqual(rc, 0)
        records = json.loads(out.getvalue())
        self.assertEqual([r["quantity"] for r in records], ["mass_drift_rel", "h_min"])
        run.write_state("a", h, h * 0.5)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pe.main(["--ir", str(ir_dir / "spec.ir.yaml"),
                                      "--run", str(run.root)]), 1)
        (ir_dir / "spec.ir.yaml").write_text(yaml.safe_dump(_ir([{**HMIN, "op": "includes"}])))
        with contextlib.redirect_stdout(out):
            self.assertEqual(pe.main(["--ir", str(ir_dir), "--run", str(run.root)]), 2)


if __name__ == "__main__":
    unittest.main()
