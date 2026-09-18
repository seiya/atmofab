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
                 {"ref": "errors.sym", "op": "le", "value": 1e-12, "case": "a",
                  "quantity": "sym"}]}},
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
        for src in ("1 + 2", "1 - 2", "1 * 2", "1 / 2", "1 ** 2", "-1"):
            with self.subTest(src=src):
                pe.parse_expr(src)
        with self.assertRaises(pe.PrimaryEvidenceError):
            pe.parse_expr("+1")   # unary + is not in the documented grammar

    def test_constants_are_numbers_only(self) -> None:
        for src in ("'x'", "True", "None", "b'x'", "..."):
            with self.subTest(src=src), self.assertRaises(pe.PrimaryEvidenceError):
                pe.parse_expr(src)

    def test_function_table_is_pinned_by_literal(self) -> None:
        """The arities are values a leaf-read document states; iterating the table pins only
        its shape (round 2 census)."""
        self.assertEqual(pe.FUNCTIONS, {
            "sum": (1, 1), "mean": (1, 1), "min": (1, 8), "max": (1, 8), "abs": (1, 1),
            "sqrt": (1, 1), "exp": (1, 1), "log": (1, 1), "log2": (1, 1), "sin": (1, 1),
            "cos": (1, 1), "norm2": (1, 1), "maxabs": (1, 1), "roll": (2, 5), "ceil": (1, 1),
            "floor": (1, 1)})
        self.assertEqual(pe.CONSTANTS, {"pi": np.pi, "e": np.e})
        self.assertEqual(pe.GRAMMAR_VERSION, 2)
        self.assertEqual(pe.MAX_INPUT_RANK, 4)
        for src in ("abs(1, 2)", "sum()", "roll(final.h)"):
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
        with self.assertRaises(pe.PrimaryEvidenceError):
            pe.parse_expr("max(final.h, axis=0)")   # arity satisfied; the keyword alone refuses

    def test_attribute_roots(self) -> None:
        for src in ("final.h", "initial.t", "inputs.grid.nx", "at('a').final.h",
                    "at('a').initial.h", "at('a').x", "at('a').inputs.grid.nx"):
            with self.subTest(src=src):
                pe.parse_expr(src)
        for src in ("final.h.T", "final", "at('a')", "at('a').final", "at('a').inputs",
                    "at(' a').final.h", "at(x).final.h", "at(abs(1)).final.h",
                    "at('a').initial", "at('a').x.y",
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
        self.assertEqual(pe.expr_names(pe.parse_expr("at('c').x + at('c').inputs.grid.nx")), [
            pe.NameRef("name", "x", case="c"), pe.NameRef("inputs", "grid.nx", case="c")])


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
        # an IR with no primary predicates AND no snapshot entry (metrics_basis evidence only)
        # is evaluated to nothing, not refused
        ir = _ir(None)
        del ir["io_contract"]["raw_requirements"]
        self.assertEqual(pe.evaluate_primary_predicates(ir, self.run.root), [])

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
        # expanded to the STATE's shape, so a reference built from a coordinate reduces over
        # the state's cells (round 2: `norm2(h_ref)` over an (nx, 1) field was sqrt(ny) short)
        self.assertEqual(env.coordinates["x"].shape, (NX, NY))
        self.assertEqual(env.coordinates["y"].shape, (NX, NY))
        np.testing.assert_allclose(env.coordinates["x"][:, 0], (np.arange(NX) + 0.5) / NX)
        np.testing.assert_allclose(env.coordinates["x"][:, 1], env.coordinates["x"][:, 0])
        np.testing.assert_allclose(env.coordinates["y"][0], (np.arange(NY) + 0.5) * 2 / NY)

    def test_a_reference_built_from_a_coordinate_values_the_full_shape_norm(self) -> None:
        """Round 2 (blank-slate, HIGH): the doc's analytic-agreement row, valued by the grammar,
        must equal the same formula over the full (nx, ny) grid in numpy."""
        coords = [{"name": "x", "axis": 0, "count": "inputs.grid.nx", "length": "inputs.grid.L_x",
                   "placement": "cell_center"}]
        pred = {"test_id": "t_mass", "quantity": "err", "target_cases": ["b"],
                "bind": {"h_ref": "1 + 0.001 * sin(2 * pi * x / inputs.grid.L_x)"},
                "expr": "norm2(final.h - h_ref) / norm2(h_ref)", "op": "le", "value": 1.0,
                "case": "b"}
        [rec] = self._eval([pred], coordinates=coords)
        x = ((np.arange(NX) + 0.5) / NX)[:, None] * np.ones((NX, NY))
        h_ref = 1 + 0.001 * np.sin(2 * np.pi * x)
        expected = np.linalg.norm(self.h - h_ref) / np.linalg.norm(h_ref)
        self.assertAlmostEqual(rec["evaluated"][0]["value"], float(expected), places=12)
        # sanity: an (nx, 1) reference would have given a different number
        self.assertNotAlmostEqual(
            rec["evaluated"][0]["value"],
            float(np.linalg.norm(self.h - h_ref) / np.linalg.norm(h_ref[:, :1])), places=6)

    def test_a_reduction_over_an_unexpanded_coordinate_field_is_refused(self) -> None:
        """When the captured arrays of the state's rank disagree on shape, a coordinate keeps
        extent 1 on the other axes and reducing over a field built from it is refused."""
        coords = [{"name": "x", "axis": 0, "count": NX, "length": 1.0, "placement": "cell_center"}]
        ir = _ir([self._one("norm2(x) + final.s")], coordinates=coords, cases=self.cases)
        ir["io_contract"]["raw_requirements"]["required_evidence"][0]["schema"]["variables"] \
            .append({"name": "w", "shape_expr": "[nx, nw]"})
        for path in (self.run.sdir / "initial" / "b.json", self.run.sdir / "b.json",
                     self.run.sdir / "initial" / "a.json", self.run.sdir / "a.json"):
            doc = json.loads(path.read_text())
            doc["w"] = [[1.0] * 2] * NX
            path.write_text(json.dumps(doc))
        [rec] = pe.evaluate_primary_predicates(ir, self.run.root)
        self.assertEqual(rec["kind"], "structural")
        self.assertIn("extent-1 axis", rec["evaluated"][-1]["error"])
        env = pe.load_case_env(self.run.root, self.cases[1], pe.snapshot_schema(ir))
        self.assertEqual(env.coordinates["x"].shape, (NX, 1))
        # paired with a state array first, it evaluates
        ir["io_contract"]["primary_predicates"] = [self._one("norm2(x + 0 * final.h)")]
        [rec] = pe.evaluate_primary_predicates(ir, self.run.root)
        self.assertTrue(rec["satisfied"], rec)

    def test_a_coordinate_count_must_match_the_captured_extent(self) -> None:
        coords = [{"name": "x", "axis": 0, "count": NX + 1, "length": 1.0,
                   "placement": "cell_center"}]
        self._structural(self._one("final.s + sum(x)"), "does not match the captured state shape",
                         coordinates=coords)

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
        # an intermediate that RECOVERS to a finite result (inf clipped by an elementwise min)
        # is still refused: the intermediate check is what these probes observe
        self._structural(self._one("min(sum(final.h) / (final.s - final.s), 5)"), "non-finite")
        self._structural(self._one("min(1e400, final.s)"), "non-finite")
        self._structural(self._one("min(exp(1000), final.s)"), "non-finite")

    def test_interpreter_exceptions_become_structural_records(self) -> None:
        """Round 1 (both axes): a Python-float division by zero, an integer literal beyond a
        float, and a numpy refusal on unequal extents used to escape as bare exceptions and
        collapse the whole verdict to `per_test: []`."""
        self._structural(self._one("(final.s - initial.s) / (initial.s - initial.s)"),
                         "ZeroDivisionError")
        self._structural(self._one("final.s + 1 / 0"), "ZeroDivisionError")
        self._structural(self._one("final.s + " + "1" * 400), "OverflowError")
        # equal rank, unequal extents between the two captures: named at load
        self.run.write("b", initial={"h": self.h.tolist(), "s": 1.0, "t": 0.0},
                       final={"h": self.h[:, :2].tolist(), "s": 1.0, "t": 0.2})
        self._structural(self._one("sum(final.h - initial.h)"), "disagree on the state shape")
        # ... and between two cases: the shape rule names the shapes instead of numpy raising
        self.run.write_state("b", self.h, self.h)
        small = self.h[:, :2]
        self.run.write_state("a", small, small)
        self._structural(self._one("sum(final.h - at('a').final.h)"), "do not pair")
        self._structural(self._one("sum(min(final.h, at('a').final.h))"), "do not pair")
        self._structural(self._one("sum(final.h ** at('a').final.h)"), "do not pair")
        self._structural(self._one("sum(min(final.h, at('a').final.h, 1))"), "do not pair")

    def test_a_scalar_pairs_with_an_array(self) -> None:
        for expr in ("sum(final.h * 2) - 2 * sum(final.h)", "sum(max(final.h, 0.0)) - sum(final.h)",
                     "sum(final.s * final.h - final.h)"):
            with self.subTest(expr=expr):
                [rec] = self._eval([self._one(expr, op="eq", value=0.0)])
                self.assertTrue(rec["satisfied"], rec)

    def test_errors_this_module_names_are_kept_verbatim(self) -> None:
        [rec] = self._eval([self._one("nope + final.s")])
        self.assertEqual(rec["evaluated"][0]["error"],
                         "name 'nope' is not a bind, a coordinate or a constant")
        [rec] = self._eval([self._one("final.s + 1 / 0")])
        self.assertEqual(rec["evaluated"][0]["error"],
                         "evaluation failed: ZeroDivisionError: float division by zero")

    def test_deep_nesting_is_a_parse_refusal(self) -> None:
        """Round 3: a 990-deep unary chain passed parse and recursed out of `evaluate`; the
        tree is bounded at `MAX_EXPR_DEPTH` so no admitted expression recurses at evaluation."""
        deep = "nested deeper than|nested too deeply|does not parse"
        for src in ("-" * 5000 + "1", " + ".join(["1"] * 5000), "-" * 990 + "1",
                    "-" * (pe.MAX_EXPR_DEPTH + 1) + "1", "abs(" * 70 + "1" + ")" * 70):
            with self.subTest(src=src[:30]), self.assertRaisesRegex(pe.PrimaryEvidenceError, deep):
                pe.parse_expr(src)
        pe.parse_expr("-" * pe.MAX_EXPR_DEPTH + "1")
        pe.parse_expr("abs(" * 30 + "final.h" + ")" * 30)

    def test_a_predicate_that_reads_no_state_raises_at_evaluation_too(self) -> None:
        """Round 4: the evaluator halves of the round-3 rule were unpinned."""
        for pred in ({**HMIN, "expr": "1.0"}, {**HMIN, "expr": "final.t"},
                     {**HMIN, "bind": {"unused": "final.h"}, "expr": "1.0"}):
            with self.subTest(pred=pred), \
                    self.assertRaisesRegex(pe.PrimaryEvidenceError, "reads no captured state"):
                self._eval([pred])
        [rec] = self._eval([{**HMIN, "bind": {"a": "sum(final.h)", "b": "a * 0"}, "expr": "b"}])
        self.assertEqual(rec["kind"], "physics")   # evaluated (to 0): the rule is syntactic
        for key in ("na_allowed", "expected_outcome"):
            with self.subTest(key=key), \
                    self.assertRaisesRegex(pe.PrimaryEvidenceError, f"{key} is not a primary"):
                self._eval([{**HMIN, key: True}])

    def test_a_grammar_refusal_is_not_prefixed_as_a_parse_error(self) -> None:
        with self.assertRaises(pe.PrimaryEvidenceError) as cm:
            pe.parse_expr("+1")
        self.assertEqual(str(cm.exception), "unary UAdd is not admitted")
        with self.assertRaisesRegex(pe.PrimaryEvidenceError, "nested too deeply to parse"):
            pe.parse_expr("-" * 7000 + "1")   # ast.parse's own limit, not MAX_EXPR_DEPTH

    def test_physics_fail_stops_at_the_first_failing_case(self) -> None:
        strict = {**HMIN, "value": {"per_case": {"a": 2.0, "b": 2.0}}}
        [rec] = self._eval([strict])
        self.assertEqual([e["case"] for e in rec["evaluated"]], ["a"])
        self.assertEqual(rec["kind"], "physics")

    def test_cross_case_coordinates_and_inputs(self) -> None:
        """A convergence order needs the COARSE case's coordinates and inputs under `at()`."""
        coords = [{"name": "x", "axis": 0, "count": "inputs.grid.nx", "length": "inputs.grid.L_x",
                   "placement": "cell_center"}]
        fine = np.random.default_rng(1).uniform(0.6, 1.0, (2 * NX, NY))
        self.run.write_state("a", fine, fine)
        cases = [_case("a"), _case("b")]
        cases[0]["inputs"]["grid"]["nx"] = 2 * NX
        pred = {"test_id": "t_mass", "quantity": "q", "target_cases": ["a", "b"],
                "bind": {"xc": "at('b').x", "nc": "at('b').inputs.grid.nx"},
                "expr": "sum(at('b').final.h * xc) / nc - sum(final.h * x) / inputs.grid.nx",
                "op": "le", "value": 10.0, "case": "a"}
        [rec] = pe.evaluate_primary_predicates(_ir([pred], coordinates=coords, cases=cases),
                                               self.run.root)
        self.assertTrue(rec["satisfied"], rec)
        self.assertEqual(rec["target_cases"], ["a", "b"])
        bad = {**pred, "expr": "at('b').zeta + final.s"}
        [rec] = pe.evaluate_primary_predicates(_ir([bad], coordinates=coords, cases=cases),
                                               self.run.root)
        self.assertIn("not a coordinate of that case", rec["evaluated"][-1]["error"])
        # the coarse state against the fine state: named as a shape mismatch, not a numpy raise
        bad = {**pred, "expr": "sum(at('b').final.h - final.h)"}
        [rec] = pe.evaluate_primary_predicates(_ir([bad], coordinates=coords, cases=cases),
                                               self.run.root)
        self.assertIn("do not pair", rec["evaluated"][-1]["error"])
        # the OTHER case's inputs are what `at('b').inputs` reads (round 2 census: the earlier
        # threshold straddled neither value)
        exact = {**pred, "bind": {}, "expr": "inputs.grid.nx - at('b').inputs.grid.nx + 0 * final.s",
                 "op": "eq", "value": float(NX)}
        [rec] = pe.evaluate_primary_predicates(_ir([exact], coordinates=coords, cases=cases),
                                               self.run.root)
        self.assertTrue(rec["satisfied"], rec)
        self.assertEqual(rec["evaluated"][0]["value"], float(NX))
        # a bind may not shadow a coordinate (the lookup order is binds first)
        shadow = {**pred, "bind": {"x": "1"}, "expr": "sum(x) + final.s"}
        [rec] = pe.evaluate_primary_predicates(_ir([shadow], coordinates=coords, cases=cases),
                                               self.run.root)
        self.assertIn("shadows a grammar name", rec["evaluated"][-1]["error"])

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
        self._structural(self._one("final.s + inputs.grid.arrangement"), "not a number")
        self._structural(self._one("final.s + inputs.flag"), "not a number")
        self._structural(self._one("final.s + inputs.grid"), "not a number")
        self._structural(self._one("final.s + inputs.grid.missing"), "not a key")

    def test_an_input_may_be_a_rectangular_numeric_list(self) -> None:
        """Grammar 2 (Z6 PR-3, the harness sentinels): `inputs.<path>` may be a nested list of
        numbers of rank 1..MAX_INPUT_RANK, read as a float64 array of that shape and paired
        with a capture under the ordinary shape rule; every other list is refused, and a
        coordinate parameter still takes one number."""
        h = self.h
        self.cases[1]["inputs"]["initial"]["h_ref"] = h.tolist()
        self.cases[1]["inputs"]["initial"]["row"] = h[0].tolist()
        self.cases[1]["inputs"]["initial"]["r4"] = np.ones((2, 2, 2, 2)).tolist()
        [rec] = self._eval([self._one("maxabs(final.h - inputs.initial.h_ref)")])
        self.assertTrue(rec["satisfied"], rec)
        self.assertEqual(rec["evaluated"][0]["value"], 0.0)
        [rec] = self._eval([self._one("sum(inputs.initial.r4) + final.s")])
        self.assertEqual(rec["evaluated"][0]["value"], 17.0)
        # the same under at('<case>'): the OTHER case's list input, as an array of its shape
        self.cases[0]["inputs"]["initial"]["h_ref"] = np.roll(h, 2, axis=0).tolist()
        [rec] = self._eval([self._one("maxabs(at('a').final.h - at('a').inputs.initial.h_ref)")])
        self.assertTrue(rec["satisfied"], rec)
        self.assertEqual(rec["evaluated"][0]["value"], 0.0)
        # a [ny] row against the [nx, ny] state: refused by the shape rule like any array
        self._structural(self._one("maxabs(final.h - inputs.initial.row)"), "rank 2 and 1")
        for bad, fragment in ((h[0][:3].tolist() + [[1.0]], "rectangular"),
                              (10 ** 400, "too large for a float"), ([10 ** 400], "rectangular"),
                              ([], "empty"), ([[1.0], [1.0, 2.0]], "rectangular"),
                              ([1.0, "2"], "other than numbers"),
                              ([True, 1.0], "other than numbers"),
                              ([float("nan")], "non-finite"),
                              (np.ones((2,) * 5).tolist(), "rank 1..4")):
            with self.subTest(bad=str(bad)[:40]):
                self.cases[1]["inputs"]["initial"]["bad"] = bad
                self._structural(self._one("final.s + sum(inputs.initial.bad)"), fragment)
        self.assertEqual(pe.resolve_input_number({"a": 2}, "a"), 2.0)
        with self.assertRaisesRegex(pe.PrimaryEvidenceError, "not one number"):
            pe.resolve_input_number({"a": [2.0]}, "a")

    def test_a_harness_shaped_run_evaluates_final_only_predicates(self) -> None:
        """A node whose own runner writes the snapshots (the harness self-test) writes no
        `initial/` capture and, per case, only that case's required variables. A predicate
        over `final.<var>` of a variable that case holds evaluates; one naming the initial
        capture, or a variable another case holds, fails structurally on its own."""
        for cid in ("a", "b"):
            (self.run.sdir / "initial" / f"{cid}.json").unlink()
        (self.run.sdir / "initial").rmdir()
        (self.run.sdir / "a.json").write_text(json.dumps({"h": self.h.tolist(), "t": 0.0}))
        (self.run.sdir / "b.json").write_text(json.dumps({"s": 1.0, "t": 0.0}))
        self.cases[0]["inputs"]["initial"]["h_ref"] = self.h.tolist()
        ok = {**self._one("maxabs(final.h - inputs.initial.h_ref)"), "case": "a"}
        [rec] = self._eval([ok])
        self.assertTrue(rec["satisfied"], rec)
        [rec] = self._eval([self._one("final.s")])
        self.assertTrue(rec["satisfied"], rec)
        self._structural({**self._one("sum(final.h)")}, "final.h: not captured in case 'b'")
        self._structural(self._one("final.s + initial.s"), "has no initial capture")

    def test_unknown_name_and_capture_variable(self) -> None:
        self._structural(self._one("final.zeta + final.s"), "not captured in case 'b'")
        self._structural(self._one("nope + final.s"), "not a bind, a coordinate or a constant")

    def test_binds_evaluate_in_order_and_cannot_look_forward(self) -> None:
        pred = self._one("B", bind={"A": "sum(final.h)", "B": "A * 2"})
        [rec] = self._eval([pred])
        self.assertAlmostEqual(rec["evaluated"][0]["value"], 2 * float(self.h.sum()))
        self._structural(self._one("B", bind={"B": "A * 2", "A": "sum(final.h)"}),
                         "'A' is not a bind")
        self._structural(self._one("pi + final.s", bind={"pi": "1"}), "shadows a grammar name")
        self._structural(self._one("final.s", bind=[1]), "bind must be a mapping")

    def test_capture_file_defects_are_structural(self) -> None:
        # Grammar 2 (Z6 PR-3): a capture carries the variables its writer holds for the case
        # — a harness self-test's own runner writes no `initial/` and a per-case subset — so an
        # ABSENT initial capture or an absent variable fails the predicate that NAMES it, and
        # only that one. The final capture itself absent fails every predicate of the case.
        (self.run.sdir / "initial" / "b.json").unlink()
        [rec] = self._eval([self._one("final.s")])
        self.assertTrue(rec["satisfied"], rec)
        self._structural(self._one("initial.s"), "has no initial capture")
        self._structural(self._one("final.s + initial.t"), "has no initial capture")
        (self.run.sdir / "b.json").unlink()
        self._structural(self._one("final.s"), "b.json is absent")
        self.run.write("b", initial={"h": self.h.tolist(), "t": 0.0},
                       final={"h": self.h.tolist(), "s": 1.0, "t": 0.2})
        [rec] = self._eval([self._one("final.s")])
        self.assertTrue(rec["satisfied"], rec)
        self._structural(self._one("initial.s"), "initial.s: not captured in case 'b'")
        self.run.write("b", initial={"h": self.h.tolist(), "s": 1.0, "t": 0.0},
                       final={"h": self.h.tolist(), "t": 0.2})
        self._structural(self._one("final.s"), "final.s: not captured in case 'b'")
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
        self.run.write("b", initial={"h": [[]] * NX, "s": 1.0, "t": 0.0},
                       final={"h": [[]] * NX, "s": 1.0, "t": 0.2})
        self._structural(self._one("final.s"), "has no elements")
        nan = self.h.tolist()
        nan[0][0] = float("nan")
        self.run.write("b", initial={"h": nan, "s": 1.0, "t": 0.0},
                       final={"h": nan, "s": 1.0, "t": 0.2})
        self._structural(self._one("final.s"), "non-finite")
        self.run.write("b", initial={"h": self.h.tolist(), "s": True, "t": 0.0},
                       final={"h": self.h.tolist(), "s": 1.0, "t": 0.2})
        self._structural(self._one("final.s"), "non-numeric")
        self.run.write("b", initial={"h": self.h.tolist(), "s": "2.5", "t": 0.0},
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
            ([{**base, "axis": 0, "count": 0}], "not a positive integer"),
            ([{**base, "axis": 0, "name": "pi"}], "not a grammar name"),
            ([{**base, "axis": 0, "name": "sum"}], "not a grammar name"),
            ([{**base, "axis": 0, "name": "initial"}], "not a grammar name"),
            ([{**base, "axis": 0, "name": "1x"}], "not a grammar name"),
            (["x"], "must be mappings"),
        ):
            with self.subTest(coords=coords):
                self._structural(self._one("final.s + sum(x)"), fragment, coordinates=coords)

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
        self._structural({**HMIN, "value": "0.5"}, "is not a finite number")
        self._structural({**HMIN, "value": float("inf")}, "is not a finite number")

    def test_function_semantics(self) -> None:
        h = self.h
        for expr, expected in (
            ("mean(final.h)", h.mean()), ("maxabs(-final.h)", np.abs(h).max()),
            ("norm2(final.h)", np.sqrt((h * h).sum())), ("max(final.h)", h.max()),
            ("min(sum(final.h), 1, 2)", min(h.sum(), 1, 2)),
            ("max(min(final.h), 5)", 5.0), ("ceil(2.1) + floor(2.9) + 0 * final.s", 5.0),
            ("exp(0) + log(e) + log2(8) + sin(0) + cos(0) + 2 ** 3 + 0 * final.s", 1 + 1 + 3 + 0 + 1 + 8),
            ("final.t - initial.t + 0 * final.s", 0.2),
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
        # an evidence gap is neither agreement nor disagreement (round 3): whichever way the
        # secondary half went, the label is `unevaluated`
        self.assertEqual(doc["per_test"][0]["basis"]["corroboration"], "unevaluated")
        diag = _diag_all_pass()
        diag["per_case"]["a"]["checks"]["mass"]["status"] = "fail"
        primary = pe.evaluate_primary_predicates(self.ir, self.run.root)
        doc = evaluate_verdict(self.predicates, diag, primary=primary)
        self.assertEqual(doc["per_test"][0]["basis"]["corroboration"], "unevaluated")
        # ... and a SECONDARY gap (a ref the runner never emitted) is `unevaluated` too
        self.run.write_state("b", self.h, self.h)
        primary = pe.evaluate_primary_predicates(self.ir, self.run.root)
        doc = evaluate_verdict(self.predicates, {}, primary=primary)
        self.assertEqual(doc["failure_class"], "structural_violation")
        self.assertEqual(doc["per_test"][0]["basis"]["corroboration"], "unevaluated")

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

    def test_a_record_over_a_subset_of_the_tests_cases_raises(self) -> None:
        primary = pe.evaluate_primary_predicates(self.ir, self.run.root)
        primary[0]["target_cases"] = ["a"]
        with self.assertRaisesRegex(PredicateError, "not the test's target_cases"):
            evaluate_verdict(self.predicates, _diag_all_pass(), primary=primary)
        del primary[0]["target_cases"]   # a record without the key is accepted as before
        evaluate_verdict(self.predicates, _diag_all_pass(), primary=primary)

    def test_a_corroborant_reading_fewer_cases_than_the_condition_raises(self) -> None:
        """Rounds 1 and 2 (security axis): a `case: a` corroborant of a `per_case` condition
        (round 1), then of a suite-level condition over [a, b] (round 2), passed the
        target_cases pin (set-equal) and the coverage gate (same name), and the test certified
        on case a alone with `corroboration: agree` while case b's state failed. The gate now
        refuses both (CoverageGateTest) and the evaluator re-checks the rule here over the
        cases a record READS."""
        self.run.write_state("b", self.h, self.h * 0.5)   # b loses mass; a does not
        narrow = {**MASS, "case": "a"}
        del narrow["per_case"]
        self.ir["io_contract"]["primary_predicates"] = [narrow, HMIN, SYM]
        primary = pe.evaluate_primary_predicates(self.ir, self.run.root)
        self.assertEqual(primary[0]["scope"], "case")
        self.assertEqual(primary[0]["cases_read"], ["a"])
        self.assertEqual(primary[1]["cases_read"], ["a", "b"])
        self.assertEqual(primary[2]["cases_read"], ["a", "b"])   # `case: a` + at('b')
        self.assertTrue(primary[0]["satisfied"])   # a alone holds
        with self.assertRaisesRegex(PredicateError, "reads every case"):
            evaluate_verdict(self.predicates, _diag_all_pass(), primary=primary)
        # round 2: the same corroborant against a SUITE-LEVEL condition over [a, b]
        preds = copy.deepcopy(self.predicates)
        del preds[0]["pass_when"]["all"][0]["per_case"]
        with self.assertRaisesRegex(PredicateError, "reads every case"):
            evaluate_verdict(preds, _diag_all_pass(), primary=primary)
        # a `case:` condition takes a corroborant that reads that case: t_sym's condition is
        # `case: a`; SYM reads a and b, and a record reading b alone is refused
        primary[2]["cases_read"] = ["b"]
        with self.assertRaisesRegex(PredicateError, "reads every case"):
            evaluate_verdict(self.predicates, _diag_all_pass(), primary=primary[1:])
        primary[2]["cases_read"] = ["a"]
        doc = evaluate_verdict(self.predicates, _diag_all_pass(), primary=primary[1:])
        # accepted; both tests then fail on the state itself (b lost mass, so the depth floor
        # and the translation pair both break) with a corroboration recorded
        self.assertEqual([t["basis"]["corroboration"] for t in doc["per_test"]],
                         ["disagree", "disagree"])
        # two records of one quantity, one reading a alone and one reading both: enough
        wide = copy.deepcopy(primary[0])
        wide["scope"], wide["cases_read"] = "per_case", ["a", "b"]
        evaluate_verdict(self.predicates, _diag_all_pass(), primary=[primary[0], wide] + primary[1:])
        # a record with no `cases_read` (a forged or older record) fails closed
        del primary[2]["cases_read"]
        with self.assertRaisesRegex(PredicateError, "reads every case"):
            evaluate_verdict(self.predicates, _diag_all_pass(), primary=primary[1:])

    def test_unknown_test_id_raises(self) -> None:
        primary = [{"test_id": "nope", "satisfied": True, "kind": "pass"}]
        with self.assertRaisesRegex(PredicateError, "no test_predicates entry"):
            evaluate_verdict(self.predicates, _diag_all_pass(), primary=primary)
        with self.assertRaises(PredicateError):
            evaluate_verdict(self.predicates, _diag_all_pass(), primary=["x"])


# ---------------------------------------------------------------------------- schema gate

class SchemaGateTest(unittest.TestCase):
    def _v(self, preds, *, ir: dict | None = None, pin_targets: bool = True) -> list[str]:
        ir = ir or _ir(preds)
        cases = {c["case_id"]: c for c in ir["case"]["test_case_set"]}
        targets = {p["test_id"]: p["target_cases"]
                   for p in ir["io_contract"]["test_predicates"]} if pin_targets else None
        return pe.validate_primary_predicate_schema(
            preds, case_ids=set(cases), test_ids=["t_mass", "t_sym"],
            schema=pe.snapshot_schema(ir), cases=cases, test_target_cases=targets)

    def test_reference_set_is_valid(self) -> None:
        coords = [{"name": "x", "axis": 0, "count": "inputs.grid.nx", "length": "inputs.grid.L_x",
                   "placement": "cell_center"}]
        with_coord = {**HMIN, "expr": "sum(final.h * x) * pi + e", "op": "le", "value": 1e9}
        ir = _ir([MASS, HMIN, SYM, with_coord], coordinates=coords)
        self.assertEqual(self._v([MASS, HMIN, SYM, with_coord], ir=ir), [])
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
            ([{**HMIN, "na_allowed": True}], "na_allowed is not a primary predicate key"),
            ([{**HMIN, "value": None}], "non-null"),
            ([{**HMIN, "value": "0.5"}], "value must be a finite number"),
            ([{**HMIN, "value": float("nan")}], "value must be a finite number"),
            ([{**HMIN, "value": {"x": 1}}], "must be a number or a"),
            ([{**SYM, "value": {"per_case": {"a": 1.0}}}], "per_case is not true"),
            ([{**HMIN, "value": {"per_case": {}}}], "non-empty map"),
            ([{**HMIN, "value": {"per_case": {"a": 1.0, "zz": 1.0}}}], "unknown case_id ('zz')"),
            ([{**HMIN, "value": {"per_case": {"a": "1"}}}], "must be a finite number"),
            ([{**HMIN, "value": {"per_case": {"a": float("inf"), "b": 1.0}}}],
             "must be a finite number"),
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
            ([{**HMIN, "expr": "at('a').zz"}], "at('a').zz is not a coordinate"),
            ([{**HMIN, "expr": "at('a').inputs.flag"}], "in case 'a': inputs.flag"),
            ([{**HMIN, "expr": "sum(at('zz').x)"}], "at('zz') is not one of"),
            ([{**HMIN, "target_cases": ["a"]}], "must equal the target_cases of test 't_mass'"),
        ]
        for preds, fragment in rows:
            with self.subTest(fragment=fragment):
                out = self._v(preds)
                self.assertTrue(any(fragment in m for m in out), (fragment, out))

    def test_target_cases_equality_is_pinned_only_when_the_test_set_is_given(self) -> None:
        """Round 1 (security axis): a corroborant over a SUBSET of its test's cases certified
        the test on its easiest case alone; the gate pins set equality, in either order."""
        subset = {**HMIN, "target_cases": ["a"]}
        self.assertTrue(any("must equal" in m for m in self._v([subset])))
        self.assertEqual(self._v([subset], pin_targets=False), [])
        reordered = {**HMIN, "target_cases": ["b", "a"]}
        self.assertEqual(self._v([reordered]), [])

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

    def test_a_bind_may_not_shadow_a_coordinate(self) -> None:
        coords = [{"name": "x", "axis": 0, "count": NX, "length": 1.0, "placement": "cell_center"}]
        ir = _ir([HMIN], coordinates=coords)
        out = self._v([{**HMIN, "bind": {"x": "1"}, "expr": "sum(x)"}], ir=ir)
        self.assertTrue(any("shadows a grammar name" in m for m in out), out)

    def test_an_input_path_must_resolve_in_every_target_case(self) -> None:
        ir = _ir([HMIN])
        del ir["case"]["test_case_set"][1]["inputs"]["grid"]["dx"]
        out = self._v([{**HMIN, "expr": "inputs.grid.dx + final.s"}], ir=ir)
        self.assertEqual(len(out), 1, out)
        self.assertIn("in case 'b'", out[0])
        # under at('<case>') the path resolves in THAT case alone
        self.assertEqual(self._v([{**HMIN, "expr": "at('a').inputs.grid.dx + final.s"}],
                                 ir=ir), [])

    def test_a_numeric_list_input_resolves_at_the_gate(self) -> None:
        """Grammar 2 (the harness sentinels): a rectangular numeric list resolves like a number
        at the gate, and the same list ragged or holding a string is refused in the case that
        holds it, through the gate's own resolver."""
        ir = _ir([HMIN])
        for case in ir["case"]["test_case_set"]:
            case["inputs"]["initial"]["a2"] = [[1.0, 2.0], [3.0, 4.0]]
        pred = {**HMIN, "expr": "maxabs(final.h) + sum(inputs.initial.a2)"}
        self.assertEqual(self._v([pred], ir=ir), [])
        ir["case"]["test_case_set"][1]["inputs"]["initial"]["a2"] = [[1.0, 2.0], [3.0]]
        out = self._v([pred], ir=ir)
        self.assertEqual(len(out), 1, out)
        self.assertIn("in case 'b': inputs.initial.a2 is not a rectangular", out[0])
        ir["case"]["test_case_set"][1]["inputs"]["initial"]["a2"] = [[1.0, "2"], [3.0, 4.0]]
        out = self._v([pred], ir=ir)
        self.assertIn("other than numbers", out[0])

    def test_a_predicate_that_reads_no_state_is_refused(self) -> None:
        """Round 3 (leaf shortcut): `expr: "1.0"`, an input alone, or the time alone values
        nothing the kernel produced; refused at the gate and at evaluation."""
        for expr in ("1.0", "inputs.grid.nx * 0", "final.t", "sin(pi)", "at('a').inputs.grid.nx"):
            with self.subTest(expr=expr):
                out = self._v([{**HMIN, "expr": expr}])
                self.assertTrue(any("reads no captured state" in m for m in out), out)
        # a state read inside a bind counts only when `expr` reaches that bind (transitively);
        # the rule is SYNTACTIC — `m * 0` passes it and is V3's to judge
        self.assertEqual(self._v([{**HMIN, "bind": {"m": "sum(final.h)"}, "expr": "m * 0"}]), [])
        self.assertEqual(self._v([{**HMIN, "bind": {"a": "sum(final.h)", "b": "a * 2"},
                                   "expr": "b"}]), [])
        out = self._v([{**HMIN, "bind": {"unused": "final.h"}, "expr": "1.0"}])
        self.assertTrue(any("reads no captured state" in m for m in out), out)
        out = self._v([{**HMIN, "bind": {"z": "at('b').final.h"}, "expr": "1"}])
        self.assertTrue(any("reads no captured state" in m for m in out), out)
        # and the keys the secondary side has are refused here
        for key in ("na_allowed", "expected_outcome"):
            out = self._v([{**HMIN, key: "pass"}])
            self.assertTrue(any(f"{key} is not a primary predicate key" in m for m in out), out)

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
        self.assertEqual(self._v([{**HMIN, "expr": "final.t - initial.t + final.s"}]), [])


# ------------------------------------------------------------------------- doc coupling

class CoverageGateTest(unittest.TestCase):
    """`coverage_violations` (Z6 PR-3): every condition's `quantity` has a primary predicate
    of the same test and quantity. Pinned per branch: the absent list, a covered set, an
    uncovered quantity, a `verdict.*` condition needing one like any other, a corroborant on
    the wrong test, and the malformed shapes that are the schema gates' to refuse."""

    _T: ClassVar[list[dict]] = [
        {"test_id": "t_mass", "expected_outcome": "pass", "target_cases": ["a"],
         "pass_when": {"all": [
             {"ref": "checks.mass.status", "op": "eq", "value": "pass", "quantity": "mass"},
             {"ref": "verdict.overall", "op": "eq", "value": "pass", "quantity": "overall"}]}},
        {"test_id": "t_sym", "expected_outcome": "xfail", "target_cases": ["a"],
         "pass_when": {"all": [
             {"ref": "verdict.overall", "op": "eq", "value": "fail", "quantity": "guard"},
             {"ref": "verdict.failed_checks", "op": "includes", "value": "g",
              "quantity": "guard"}]}},
    ]

    @staticmethod
    def _p(test_id: str, quantity: str) -> dict:
        return {"test_id": test_id, "quantity": quantity, "expr": "final.s", "op": "le",
                "value": 1.0, "target_cases": ["a"], "per_case": True}

    def test_covered(self) -> None:
        full = [self._p("t_mass", "mass"), self._p("t_mass", "overall"), self._p("t_sym", "guard")]
        self.assertEqual(pe.coverage_violations(self._T, full), [])
        # two conditions sharing one quantity name share one corroborant; a second corroborant
        # of the same quantity and a corroborant of an unnamed quantity change nothing
        self.assertEqual(pe.coverage_violations(
            self._T, full + [self._p("t_sym", "guard"), self._p("t_mass", "extra")]), [])

    def test_a_corroborant_must_read_every_case_the_condition_holds_in(self) -> None:
        """Rounds 1 and 2 (security axis): per (test, quantity), some corroborant must READ
        every case the condition holds in — every target case for a per_case or a suite-level
        condition, the one case for a `case: X` condition; a corroborant reads every target
        case under per_case, and under `case: X` reads X plus its at('<case>') cases. A
        primary with a malformed scope or expression covers nothing (the primary schema gate
        refuses it)."""
        t = copy.deepcopy(self._T)
        t[0]["target_cases"] = ["a", "b"]
        t[0]["pass_when"]["all"] = [
            {"ref": "m.a", "op": "le", "value": 1, "per_case": True, "quantity": "pc"},
            {"ref": "m.b", "op": "le", "value": 1, "case": "a", "quantity": "ca"},
            {"ref": "m.c", "op": "le", "value": 1, "quantity": "suite"}]
        t[1]["pass_when"]["all"] = [
            {"ref": "m.d", "op": "le", "value": 1, "per_case": True, "quantity": "pc2"}]

        def case_p(test_id: str, quantity: str, case: str, expr: str = "final.s") -> dict:
            p = {**self._p(test_id, quantity), "case": case, "expr": expr,
                 "target_cases": ["a", "b"] if test_id == "t_mass" else ["a"]}
            del p["per_case"]
            return p

        def per_case_p(test_id: str, quantity: str) -> dict:
            return {**self._p(test_id, quantity),
                    "target_cases": ["a", "b"] if test_id == "t_mass" else ["a"]}

        # per_case condition, `case: a` corroborant -> refused (round 1); `case:` condition
        # read in another case -> refused; suite-level with a `case: a` corroborant over a
        # two-case test -> refused (round 2)
        v = pe.coverage_violations(t, [case_p("t_mass", "pc", "a"), case_p("t_mass", "ca", "b"),
                                       case_p("t_mass", "suite", "a"),
                                       per_case_p("t_sym", "pc2")])
        self.assertEqual(len(v), 3, v)
        self.assertIn("t_mass: condition on 'm.a' (quantity 'pc') holds in every target case, "
                      "but no corroborant of that quantity reads every such case (each reads "
                      "[['a']])", v[0])
        self.assertIn("condition on 'm.b' (quantity 'ca') is read in case 'a', but no "
                      "corroborant of that quantity reads every such case (each reads [['b']])",
                      v[1])
        self.assertIn("condition on 'm.c' (quantity 'suite') holds in every target case", v[2])
        self.assertIn("give one primary_predicates entry `per_case: true`, or a `case:` whose "
                      "at('<case>') references reach the rest", v[2])
        # per_case corroborants, or a `case:` one whose at() reaches the rest, satisfy each;
        # a `case: a` corroborant of a `case: a` condition reads exactly the case it needs
        self.assertEqual(pe.coverage_violations(t, [
            per_case_p("t_mass", "pc"), case_p("t_mass", "ca", "a"),
            case_p("t_mass", "suite", "b", "final.s - at('a').final.s"),
            per_case_p("t_sym", "pc2")]), [])
        self.assertEqual(pe.coverage_violations(t, [
            case_p("t_mass", "pc", "a", "min(final.s, at('b').final.s)"),
            per_case_p("t_mass", "ca"), per_case_p("t_mass", "suite"),
            per_case_p("t_sym", "pc2")]), [])
        # an at() inside a BIND reaches the rest too
        bound = case_p("t_mass", "suite", "a", "final.s - other")
        bound["bind"] = {"other": "at('b').final.s"}
        self.assertEqual(pe.coverage_violations(t, [
            per_case_p("t_mass", "pc"), per_case_p("t_mass", "ca"), bound,
            per_case_p("t_sym", "pc2")]), [])
        # one of several corroborants of a quantity reading enough is enough
        self.assertEqual(pe.coverage_violations(t, [
            case_p("t_mass", "pc", "a"), per_case_p("t_mass", "pc"), per_case_p("t_mass", "ca"),
            per_case_p("t_mass", "suite"), per_case_p("t_sym", "pc2")]), [])
        # a single-case test: a `case: a` corroborant reads every case a per_case condition
        # holds in (round 2's over-refusal probe)
        self.assertEqual(pe.coverage_violations(t[1:], [case_p("t_sym", "pc2", "a")]), [])
        # a malformed scope (both keys, neither, per_case: false alone, an empty case), a
        # non-list target_cases, or an unparsable expression is not a corroborant at all
        for bad in ({"per_case": True, "case": "a"}, {}, {"per_case": False}, {"case": ""},
                    {"case": 1}, {"per_case": True, "target_cases": "a"},
                    {"case": "a", "expr": "final.s +"}, {"case": "a", "bind": {"x": "("}}):
            with self.subTest(bad=bad):
                p = {k: v_ for k, v_ in per_case_p("t_sym", "pc2").items() if k != "per_case"}
                p.update(bad)
                v = pe.coverage_violations(t[1:], [p])
                self.assertEqual(len(v), 1, v)
                self.assertIn("has no host-evaluated corroborant", v[0])
        self.assertIsNone(pe.cases_read({"case": "a", "target_cases": ["a"], "expr": "("}))
        self.assertEqual(pe.cases_read({"case": "a", "target_cases": ["a", "b"],
                                        "expr": "at('b').final.s + at('c').final.s"}),
                         {"a", "b", "c"})

    def test_absent_list_is_refused(self) -> None:
        for absent in (None, "x", {}):
            with self.subTest(absent=absent):
                v = pe.coverage_violations(self._T, absent)
                self.assertEqual(len(v), 1, v)
                self.assertIn("primary_predicates missing", v[0])

    def test_each_uncovered_quantity_is_named(self) -> None:
        v = pe.coverage_violations(self._T, [self._p("t_mass", "mass")])
        self.assertEqual(len(v), 3, v)
        self.assertIn("t_mass: condition on 'verdict.overall' (quantity 'overall') has no "
                      "host-evaluated corroborant", v[0])
        self.assertIn("t_sym: condition on 'verdict.overall' (quantity 'guard')", v[1])
        self.assertIn("t_sym: condition on 'verdict.failed_checks' (quantity 'guard')", v[2])
        self.assertIn("quantity 'guard' that values it from the captured state", v[2])

    def test_a_corroborant_counts_for_its_own_test_only(self) -> None:
        v = pe.coverage_violations(self._T, [
            self._p("t_sym", "mass"), self._p("t_sym", "overall"), self._p("t_mass", "guard")])
        self.assertEqual(len(v), 4, v)
        v = pe.coverage_violations(self._T, [
            self._p(" t_mass ", "mass"), self._p("t_mass", "overall"), self._p("t_sym", "guard")])
        self.assertEqual(v, [], "a test_id is compared stripped, as the schema gates do")

    def test_an_empty_list_leaves_every_test_uncovered(self) -> None:
        v = pe.coverage_violations(self._T, [])
        self.assertEqual(len(v), 4, v)

    def test_malformed_shapes_are_left_to_the_schema_gates(self) -> None:
        self.assertEqual(pe.coverage_violations("x", []), [])
        self.assertEqual(pe.coverage_violations([1, {"test_id": 2}], []), [])
        no_q = [{"test_id": "t", "pass_when": {"all": [{"ref": "verdict.overall"}, 3]}}]
        self.assertEqual(pe.coverage_violations(no_q, []), [])
        self.assertEqual(pe.coverage_violations(
            self._T, [1, {"quantity": "mass"}, {"test_id": "t_mass"},
                      {"test_id": "t_mass", "quantity": 1}]), pe.coverage_violations(self._T, []))


class CompileContractCouplingTest(unittest.TestCase):
    """The grammar is stated twice — in `FUNCTIONS` and in the compile contract's grammar block
    the leaf reads — so the block is coupled to the code by POINTER and by MEMBERS: it names
    `FUNCTIONS`, every key appears on its function lines, and the elementwise arity cap and the
    `roll` rank cap it states are the constants' values."""

    _DOC = Path(__file__).resolve().parents[2] / "docs/workflow/phases/phase_01_compile.md"

    def _function_block(self) -> str:
        text = self._DOC.read_text(encoding="utf-8")
        start = text.index("  #   functions   ")
        end = text.index("  #   refused     ", start)
        return text[start:end]

    def test_every_function_is_named_on_the_functions_lines_and_the_pointer_is_present(self):
        block = self._function_block()
        self.assertIn("keys of `FUNCTIONS`", block)
        import re
        words = set(re.findall(r"\b[a-z][a-z0-9]*\b", block))
        self.assertEqual(set(pe.FUNCTIONS) - words, set())

    def test_the_stated_arity_caps_are_the_constants(self):
        block = self._function_block()
        cap = pe.FUNCTIONS["max"][1]
        names = {2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight"}
        self.assertIn(f"two to {names[cap]} are elementwise", block)
        self.assertEqual(pe.FUNCTIONS["min"], pe.FUNCTIONS["max"])
        self.assertIn(f"rank ≤ {pe.FUNCTIONS['roll'][1] - 1}", block)


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
        # round 4: an unreadable IR (a YAML list, malformed YAML, a missing file) and a run
        # directory without captures are exit 2 with a JSON error, never the exit code of
        # "a predicate is unsatisfied" and never a traceback
        for text in ("- a\n- b\n", "io_contract: [unclosed\n"):
            (ir_dir / "spec.ir.yaml").write_text(text)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(pe.main(["--ir", str(ir_dir), "--run", str(run.root)]), 2)
            self.assertIn("error", json.loads(out.getvalue()))
        (ir_dir / "spec.ir.yaml").write_text(yaml.safe_dump(_ir([MASS, HMIN])))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pe.main(["--ir", str(ir_dir / "missing.yaml"),
                                      "--run", str(run.root)]), 2)
            self.assertEqual(pe.main(["--ir", str(ir_dir), "--run", str(run.root / "nope")]), 2)


if __name__ == "__main__":
    unittest.main()
