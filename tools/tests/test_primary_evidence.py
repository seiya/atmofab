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
* The Compile gate's static rank check (issue #440): `_RANK_RULES` is pinned by set identity
  against `FUNCTIONS`, and its agreement with evaluation by parity — over one row per function
  and per name form, the inferred rank is `np.ndim` of the evaluated value, and each refusal
  is the evaluator's own message (`StaticRankTest`). Extents are not inferred.
* `evaluate_verdict` with `primary=` is pinned in both directions: the two fixtures the plan
  names — (a) a diagnostics that passes every secondary condition while the captured state
  fails its predicate, and (c) one decoy case among two — fail the test, and an IR with no
  predicate cannot certify, which is the document
  with `primary=None` as with `primary=[]` (issue #324 added `own_verdict` to both and issue
  #383 removed it again).

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
import re
import tempfile
import unittest
import unittest.mock
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
        cases: list[dict] | None = None, variables: list[dict] | None = None) -> dict:
    schema = {"variables": variables if variables is not None else [
                  {"name": "h", "shape_expr": "[nx, ny]"}, {"name": "s", "shape_expr": "scalar"}],
              "time_variable": "t", "time_shape_expr": "scalar"}
    if coordinates is not None:
        schema["coordinates"] = coordinates
    io_contract = {
        "raw_requirements": {"required_evidence": [
            {"artifact": "state_snapshots", "required": True,
             "schema": schema}]},
        "tests": [
            {"test_id": "t_mass", "expected_outcome": "pass", "target_cases": ["a", "b"],
             "quantities": ["mass_drift_rel", "h_min"]},
            {"test_id": "t_sym", "expected_outcome": "pass", "target_cases": ["a", "b"],
             "quantities": ["sym"]},
        ],
    }
    if primary is not None:
        io_contract["predicates"] = primary
    return {"case": {"test_case_set": cases if cases is not None else [_case("a"), _case("b")]},
            "io_contract": io_contract}


#: The issue #440 schema: a stacked rank-3 state beside rank-2 component captures.
MIXED_VARIABLES = (
    [{"name": n, "shape_expr": "[ncomp, nx, ny]"} for n in ("U", "S")]
    + [{"name": n, "shape_expr": "[nx, ny]"} for n in ("hu", "hv", "S_h", "S_hu", "S_hv")])


def _mixed_ir(primary: list[dict] | None, coordinates: list[dict] | None,
              variables: list[dict] | None = None) -> dict:
    cases = [_case("a"), _case("b")]
    for c in cases:
        c["inputs"]["ncomp"] = 3
        c["inputs"]["constants"] = {"f0": 1.0e-4, "df": 1.0e-5}
    return _ir(primary, coordinates=coordinates, cases=cases,
               variables=list(MIXED_VARIABLES) if variables is None else variables)


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
# The snapshot schema variables of `_ir` (`h`, `s`): the state names `cases_read` counts
# (the time variable `t` is not among them).
STATE = {"h", "s"}


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
            "floor": (1, 1), "atan2": (2, 2), "transpose": (1, 3), "flip": (2, 2),
            "coarsen": (2, 2), "smax": (1, 1), "smin": (1, 1), "ssum": (1, 1)})
        self.assertEqual(pe.CONSTANTS, {"pi": np.pi, "e": np.e})
        self.assertEqual(pe.GRAMMAR_VERSION, 6)
        self.assertEqual(pe.MAX_INPUT_RANK, 4)
        for src in ("abs(1, 2)", "sum()", "roll(final.h)", "transpose(final.h, 1, 0, 1)",
                    "flip(final.h)", "flip(final.h, 0, 1)", "coarsen(final.h)",
                    "coarsen(final.h, 2, 2)"):
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

    def test_a_case_scoped_input_only_its_case_declares_is_evaluated(self) -> None:
        """Issue #532: the input the gate now resolves in X alone is the one evaluation reads
        in X; the other target case, which lacks it, is never asked for it."""
        cases = [_case("a", shift=0.25, u_jet=2.0), _case("b")]
        pred = {**SYM, "expr": "maxabs(final.h) * inputs.initial.u_jet", "op": "le",
                "value": 10.0}
        ir = _ir([pred], cases=cases)
        self.assertEqual(pe.validate_primary_predicate_schema(
            [pred], case_ids={"a", "b"}, test_ids=["t_mass", "t_sym"],
            schema=pe.snapshot_schema(ir), cases={c["case_id"]: c for c in cases}), [])
        [rec] = pe.evaluate_primary_predicates(ir, self.run.root)
        self.assertTrue(rec["satisfied"], rec)
        self.assertEqual([e["case"] for e in rec["evaluated"]], ["a"])
        self.assertAlmostEqual(rec["evaluated"][0]["value"], 2.0 * float(self.h.max()))

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
        ir["io_contract"]["predicates"] = [self._one("norm2(x + 0 * final.h)")]
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

    def test_a_non_finite_token_in_a_capture_is_structural(self) -> None:
        """A runner writes a non-finite real as `NaN` / `Infinity` / `-Infinity`
        (RUNNER_OUTPUT_CONTRACT.md §4). The capture reader restores each and refuses it,
        naming the variable (issue #315)."""
        path = self.run.sdir / "b.json"
        clean = path.read_text()
        for token in ("NaN", "Infinity", "-Infinity"):
            doc = json.loads(clean)
            doc["s"] = "@TOKEN@"
            path.write_text(json.dumps(doc).replace('"@TOKEN@"', token))
            rec = self._structural(self._one("final.s"), "holds a non-finite value")
            self.assertIn("'s'", rec["evaluated"][-1]["error"], token)

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
        # the evaluator applies the rule to the raw references, so this evaluates (to 0); the
        # schema gate refuses it upstream since issue #483 (a read only inside `X * 0`)
        [rec] = self._eval([{**HMIN, "bind": {"a": "sum(final.h)", "b": "a * 0"}, "expr": "b"}])
        self.assertEqual(rec["kind"], "physics")
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

    def test_transpose_flip_coarsen_values(self) -> None:
        """Grammar 5 (issue #513): `transpose` reverses the axis order, `flip(a, k)` reverses
        axis `k`, `coarsen(a, f)` is the block mean over `f` cells along every axis."""
        h = np.arange(24.0).reshape(4, 6)
        u = np.array([1.0, 2.0, 3.0, 5.0])
        c3 = np.arange(64.0).reshape(4, 4, 4)
        env = pe.CaseEnv(case_id="c", initial=None, final={"h": h, "u": u, "c": c3},
                         inputs={}, coordinates={})

        def ev(src: str) -> np.ndarray:
            return np.asarray(pe.evaluate(pe.parse_expr(src), env))
        np.testing.assert_array_equal(ev("transpose(final.h)"), h.T)
        self.assertEqual(ev("transpose(final.h)").shape, (6, 4))
        np.testing.assert_array_equal(ev("transpose(final.u)"), u)
        np.testing.assert_array_equal(ev("transpose(final.c)"), np.transpose(c3, (2, 1, 0)))
        np.testing.assert_array_equal(ev("transpose(final.c, 1, 2)"), np.transpose(c3, (0, 2, 1)))
        np.testing.assert_array_equal(ev("transpose(final.c, 2, 0)"), np.transpose(c3, (2, 1, 0)))
        np.testing.assert_array_equal(ev("transpose(final.h, 0, 1)"), h.T)
        np.testing.assert_array_equal(ev("transpose(final.h, 1, 1)"), h)
        c4 = np.arange(120.0).reshape(2, 3, 4, 5)
        env.final["d"] = c4
        np.testing.assert_array_equal(ev("transpose(final.d)"), np.transpose(c4, (3, 2, 1, 0)))
        np.testing.assert_array_equal(ev("transpose(final.d, 1, 3)"), np.swapaxes(c4, 1, 3))
        np.testing.assert_array_equal(ev("flip(final.h, 0)"), h[::-1, :])
        np.testing.assert_array_equal(ev("flip(final.h, 1)"), h[:, ::-1])
        np.testing.assert_array_equal(ev("flip(final.h, 2 - 1.0)"), h[:, ::-1])
        np.testing.assert_array_equal(ev("flip(final.u, 0)"), u[::-1])
        want = np.array([[h[0:2, 0:2].mean(), h[0:2, 2:4].mean(), h[0:2, 4:6].mean()],
                         [h[2:4, 0:2].mean(), h[2:4, 2:4].mean(), h[2:4, 4:6].mean()]])
        np.testing.assert_array_equal(ev("coarsen(final.h, 2)"), want)
        np.testing.assert_array_equal(ev("coarsen(final.u, 2)"), [1.5, 4.0])
        np.testing.assert_array_equal(ev("coarsen(final.u, 4)"), [2.75])
        np.testing.assert_array_equal(ev("coarsen(final.h, 1)"), h)
        self.assertEqual(float(ev("coarsen(final.c, 2)")[1, 0, 1]),
                         float(c3[2:4, 0:2, 2:4].mean()))
        # the conservative restriction: the mean of the coarse field is the fine field's
        self.assertAlmostEqual(float(ev("mean(coarsen(final.h, 2)) - mean(final.h)")), 0.0)
        for src, msg in (("flip(final.h, 2)", "axis 2 is outside [0, 2)"),
                         ("flip(final.h, -1)", "axis -1 is outside"),
                         ("flip(final.h, 0.5)", "the axis 0.5 is not an integer"),
                         ("flip(final.h, final.u)", "must be a scalar"),
                         ("coarsen(final.h, 4)", "does not divide every extent of shape [4, 6]"),
                         ("coarsen(final.u, 8)", "does not divide every extent of shape [4]"),
                         ("transpose(final.h, 1)", "takes the array alone, or the array and the two"),
                         ("transpose(final.c, 1, 3)", "transpose(): axis 3 is outside [0, 3)"),
                         ("transpose(final.c, 0.5, 1)", "the axis 0.5 is not an integer"),
                         ("transpose(final.c, final.u, 1)", "must be a scalar"),
                         ("coarsen(final.h, 0)", "the factor 0 is below 1"),
                         ("coarsen(final.h, 1.5)", "the factor 1.5 is not an integer"),
                         ("coarsen(final.h, final.u)", "must be a scalar"),
                         ("transpose(1.0)", "the first argument must be an array"),
                         ("flip(2.0, 0)", "the first argument must be an array"),
                         ("coarsen(2.0, 1)", "the first argument must be an array")):
            with self.subTest(src=src), self.assertRaisesRegex(pe.PrimaryEvidenceError,
                                                               re.escape(msg)):
                pe.evaluate(pe.parse_expr(src), env)

    def test_a_transformed_pair_is_judged_through_at(self) -> None:
        """The use grammar 5 exists for: a transformed case reads its base through `at` and
        the transformation; the transposed / mirrored state agrees, an untransformed one does
        not."""
        base = np.arange(1.0, 1.0 + NX * NY).reshape(NX, NY)
        envs = {"b": pe.CaseEnv(case_id="b", initial=None, final={"h": base}, inputs={},
                                coordinates={}),
                "t": pe.CaseEnv(case_id="t", initial=None, final={"h": base.T.copy()},
                                inputs={}, coordinates={}),
                "m": pe.CaseEnv(case_id="m", initial=None, final={"h": base[::-1, :].copy()},
                                inputs={}, coordinates={})}
        for case, src, want_zero in (
                ("t", "norm2(final.h - transpose(at('b').final.h))", True),
                ("m", "norm2(final.h - flip(at('b').final.h, 0))", True),
                ("m", "norm2(final.h - flip(at('b').final.h, 1))", False),
                ("m", "norm2(final.h - at('b').final.h)", False)):
            with self.subTest(src=src):
                got = float(pe.evaluate(pe.parse_expr(src), envs[case], at_envs=envs))
                self.assertEqual(got == 0.0, want_zero, got)
        # an unequal-extent pair (the transposed state of a non-square grid) is not paired
        # with the untransposed base: the shape rule refuses it
        with self.assertRaisesRegex(pe.PrimaryEvidenceError, "do not pair"):
            pe.evaluate(pe.parse_expr("norm2(final.h - at('b').final.h)"), envs["t"],
                        at_envs=envs)

    def test_a_stacked_state_axis_exchange_is_writable(self) -> None:
        """Issue #513 round 1: a component suite captures `[ncomp, nx, ny]`; its axis exchange
        exchanges the two spatial axes alone and the two momentum components on axis 0
        (`S'(1) = S(1)^T, S'(2) = S(3)^T, S'(3) = S(2)^T`) — `transpose(a, 1, 2)` with
        `roll(flip(., 0), 1, 0, 0)`. The full reversal `transpose(a)` does not pair."""
        base = np.arange(1.0, 1.0 + 3 * 6 * 4).reshape(3, 6, 4)
        moved = np.stack([base[0].T, base[2].T, base[1].T])
        envs = {"b": pe.CaseEnv(case_id="b", initial=None, final={"S": base}, inputs={},
                                coordinates={}),
                "t": pe.CaseEnv(case_id="t", initial=None, final={"S": moved}, inputs={},
                                coordinates={})}
        src = "maxabs(final.S - roll(flip(transpose(at('b').final.S, 1, 2), 0), 1, 0, 0))"
        self.assertEqual(float(pe.evaluate(pe.parse_expr(src), envs["t"], at_envs=envs)), 0.0)
        # the component exchange is load-bearing: without it the pair does not hold
        src = "maxabs(final.S - transpose(at('b').final.S, 1, 2))"
        self.assertGreater(float(pe.evaluate(pe.parse_expr(src), envs["t"], at_envs=envs)), 0.0)
        with self.assertRaisesRegex(pe.PrimaryEvidenceError, "do not pair"):
            pe.evaluate(pe.parse_expr("maxabs(final.S - transpose(at('b').final.S))"),
                        envs["t"], at_envs=envs)

    def test_atan2_is_the_quadrant_correct_elementwise_arctangent(self) -> None:
        """Grammar 4 (issue #327): `atan2(y, x)` keeps the quadrant `atan(y / x)` loses, pairs
        its operands under the ordinary shape rule, and a mismatched pair is structural."""
        env = pe.CaseEnv(case_id="c", initial=None, final={"u": np.array([-1.0, 1.0])},
                         inputs={}, coordinates={})
        for src, want in (("atan2(1, -1)", 3 * np.pi / 4), ("atan2(-1, -1)", -3 * np.pi / 4),
                          ("atan2(0, -1)", np.pi), ("atan2(1, 0)", np.pi / 2),
                          ("atan2(-0.0, -1)", -np.pi)):   # the sign of zero picks the branch
            with self.subTest(src=src):
                self.assertAlmostEqual(float(pe.evaluate(pe.parse_expr(src), env)), want)
        got = pe.evaluate(pe.parse_expr("atan2(final.u, -1)"), env)
        np.testing.assert_allclose(got, [-3 * np.pi / 4, 3 * np.pi / 4])
        env.final["v"] = np.array([1.0, 2.0, 3.0])
        with self.assertRaisesRegex(pe.PrimaryEvidenceError, r"atan2\(\).*do not pair"):
            pe.evaluate(pe.parse_expr("sum(atan2(final.u, final.v))"), env)

    def test_atan2_corroborates_a_discrete_mode_against_g_to_the_n(self) -> None:
        """The use grammar 4 exists for (issue #327): the discrete Fourier mode of a linear
        periodic update evolves by exactly G**n, whose phase needs `atan2`. The expression
        holds at round-off on the specified update and fails on a 1e-4 coefficient error."""
        nx, n, c, d = 64, 40, 0.39, 0.25
        x = (np.arange(nx) + 0.5) / nx
        u0 = np.sin(2 * np.pi * x)

        def run(dfac: float) -> np.ndarray:
            u = u0.copy()
            for _ in range(n):
                um, up = np.roll(u, 1), np.roll(u, -1)
                u = u - c * (u - um) + d * dfac * (up - 2 * u + um)
            return u

        bind = {
            "th": "2 * pi / inputs.nx", "re": "1 - (inputs.c + 2 * inputs.d) * (1 - cos(th))",
            "im": "-inputs.c * sin(th)", "gm": "sqrt(re**2 + im**2) ** inputs.n",
            "ph": "inputs.n * atan2(im, re)",
            "a0": "sum(initial.u * cos(2 * pi * x))", "b0": "-sum(initial.u * sin(2 * pi * x))",
            "a1": "sum(final.u * cos(2 * pi * x))", "b1": "-sum(final.u * sin(2 * pi * x))",
            "pa": "gm * (cos(ph) * a0 - sin(ph) * b0)", "pb": "gm * (sin(ph) * a0 + cos(ph) * b0)",
        }
        expr = "sqrt((a1 - pa)**2 + (b1 - pb)**2) / sqrt(a0**2 + b0**2)"
        inputs = {"nx": float(nx), "n": float(n), "c": c, "d": d}

        def value(u1: np.ndarray) -> float:
            env = pe.CaseEnv(case_id="c", initial={"u": u0}, final={"u": u1}, inputs=inputs,
                             coordinates={"x": x})
            binds = pe.evaluate_binds(bind, env, {})
            return float(pe.evaluate(pe.parse_expr(expr), env, binds=binds))

        self.assertLess(value(run(1.0)), 1e-12)
        self.assertGreater(value(run(1.0001)), 1e-6)

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
        # a function name too (round 1 of #327: neither side's FUNCTIONS clause was pinned)
        self._structural(self._one("final.s", bind={"atan2": "1"}), "shadows a grammar name")
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
    """`evaluate_verdict` over records this module evaluated from real captures: the plan's
    fixtures (a) and (c) of issue #255, on the host alone."""

    def setUp(self) -> None:
        self.run = _RunDir()
        self.addCleanup(self.run.cleanup)
        self.h = _field()
        self.run.write_state("a", self.h, self.h)
        self.run.write_state("b", self.h, self.h)
        self.ir = _ir([MASS, HMIN, SYM])
        self.tests = self.ir["io_contract"]["tests"]

    def _records(self) -> list[dict]:
        return pe.evaluate_primary_predicates(self.ir, self.run.root)

    def _verdict(self) -> dict:
        return evaluate_verdict(self.tests, self._records(), run_id="r", node_key="n")

    def test_holding_state_passes_with_basis(self) -> None:
        doc = self._verdict()
        self.assertEqual((doc["self_verdict"], doc["failure_class"]), ("pass", "pass"))
        self.assertEqual([r["quantity"] for r in doc["per_test"][0]["basis"]["predicates"]],
                         ["mass_drift_rel", "h_min"])

    def test_fixture_a_tampered_state_fails(self) -> None:
        # the captured final state lost mass in both cases (the translation pair stays a pair)
        self.run.write_state("a", self.h, self.h * 0.9)
        self.run.write_state("b", self.h, self.h * 0.9)
        doc = self._verdict()
        self.assertEqual((doc["self_verdict"], doc["failure_class"]), ("fail", "physics_fail"))
        mass = doc["per_test"][0]
        self.assertEqual(mass["status"], "fail")
        self.assertFalse(mass["basis"]["predicates"][0]["satisfied"])
        self.assertEqual(mass["basis"]["predicates"][0]["evaluated"][0]["case"], "a")
        self.assertEqual(doc["per_test"][1]["status"], "pass")

    def test_fixture_c_one_decoy_case_among_two(self) -> None:
        # case b keeps initial == final (mass drift 0) while its depth breaks positivity
        low = self.h.copy()
        low[3, 1] = 0.1
        self.run.write_state("b", low, low)
        doc = self._verdict()
        self.assertEqual(doc["self_verdict"], "fail")
        drift, hmin = doc["per_test"][0]["basis"]["predicates"]
        self.assertTrue(drift["satisfied"])
        self.assertFalse(hmin["satisfied"])
        self.assertEqual([e["case"] for e in hmin["evaluated"]], ["a", "b"])
        self.assertFalse(hmin["evaluated"][1]["satisfied"])

    def test_structural_record_folds_to_structural_violation(self) -> None:
        (self.run.sdir / "initial" / "b.json").unlink()
        doc = self._verdict()
        self.assertEqual(doc["failure_class"], "structural_violation")
        self.assertEqual(doc["per_test"][0]["status"], "fail")

    def test_xfail_test_certifies_on_its_predicates(self) -> None:
        tests = copy.deepcopy(self.tests)
        tests[0]["expected_outcome"] = "xfail"
        self.assertEqual(evaluate_verdict(tests, self._records())["per_test"][0]["status"], "xfail")
        self.run.write_state("a", self.h, self.h * 0.9)
        self.assertEqual(evaluate_verdict(tests, self._records())["per_test"][0]["status"], "fail")

    def test_a_record_over_a_subset_of_the_tests_cases_raises(self) -> None:
        records = self._records()
        records[0]["target_cases"] = ["a"]
        with self.assertRaisesRegex(PredicateError, "not the test's target_cases"):
            evaluate_verdict(self.tests, records)
        del records[0]["target_cases"]   # a record without the key is accepted as before
        evaluate_verdict(self.tests, records)

    def test_a_test_whose_records_read_fewer_cases_than_it_targets_raises(self) -> None:
        """The surviving half of Z6 PR-3 rounds 1 and 2 (security axis): `case: a` predicates over
        [a, b] pass the target_cases pin (set-equal) and the test would certify on case a alone
        while case b's state failed. The re-check is over the cases a record READS, per test."""
        self.run.write_state("b", self.h, self.h * 0.5)   # b loses mass; a does not
        narrow = {**MASS, "case": "a"}
        del narrow["per_case"]
        narrow_h = {**HMIN, "case": "a"}
        del narrow_h["per_case"]
        self.ir["io_contract"]["predicates"] = [narrow, narrow_h, SYM]
        records = self._records()
        self.assertEqual(records[0]["scope"], "case")
        self.assertEqual(records[0]["cases_read"], ["a"])
        self.assertEqual(records[2]["cases_read"], ["a", "b"])   # `case: a` + at('b')
        self.assertTrue(records[0]["satisfied"])   # a alone holds
        with self.assertRaisesRegex(PredicateError, r"target case\(s\) \['b'\]"):
            evaluate_verdict(self.tests, records)
        # one of a test's records reading b is enough, and that record then judges it
        self.ir["io_contract"]["predicates"] = [narrow, HMIN, SYM]
        doc = evaluate_verdict(self.tests, self._records())
        self.assertEqual(doc["self_verdict"], "fail")
        # a record with no `cases_read` (a forged or older record) values nothing: fail closed
        records = self._records()
        for rec in records[:2]:
            del rec["cases_read"]
        with self.assertRaisesRegex(PredicateError, "values the captured state"):
            evaluate_verdict(self.tests, records)

    def test_a_dead_bind_values_no_case_and_the_recheck_raises(self) -> None:
        """Issue #567: the record's `cases_read` comes from the same function as the gate's, so a
        `case: a` predicate whose only at('b') is in a bind `expr` never reaches is recorded as
        valuing a alone. The record is not forged by hand here."""
        dead = {**MASS, "case": "a",
                "bind": {**MASS["bind"], "dead": "sum(at('b').final.h)"}}
        del dead["per_case"]
        hmin_a = {**HMIN, "case": "a"}
        del hmin_a["per_case"]
        self.ir["io_contract"]["predicates"] = [dead, hmin_a, SYM]
        records = self._records()
        self.assertEqual(records[0]["cases_read"], ["a"])
        self.assertTrue(records[0]["satisfied"])
        with self.assertRaisesRegex(PredicateError, "values the captured state"):
            evaluate_verdict(self.tests, records)
        # reached, the same bind values b as well
        reached = {**dead, "expr": dead["expr"] + " + 1e-300 * dead"}
        self.ir["io_contract"]["predicates"] = [reached, hmin_a, SYM]
        self.assertEqual(self._records()[0]["cases_read"], ["a", "b"])
        # an at('b').final.t (the time variable) that enters the value values nothing of b
        timed = {**dead, "expr": dead["expr"] + " + 1e-300 * at('b').final.t"}
        del timed["bind"]["dead"]
        self.ir["io_contract"]["predicates"] = [timed, hmin_a, SYM]
        records = self._records()
        self.assertEqual(records[0]["cases_read"], ["a"])
        with self.assertRaisesRegex(PredicateError, "values the captured state"):
            evaluate_verdict(self.tests, records)

    def test_unknown_test_id_raises(self) -> None:
        with self.assertRaisesRegex(PredicateError, "no io_contract.tests entry"):
            evaluate_verdict(self.tests, self._records() + [{"test_id": "nope"}])
        with self.assertRaises(PredicateError):
            evaluate_verdict(self.tests, ["x"])

    def test_records_carry_cases_read_and_no_sources_read(self) -> None:
        """Grammar 6: a record names the cases it values and what D7 asks of it, and no longer
        a per-`source` map (the vocabulary is retired). An IR with no `drive` has no one-call
        scalar, so every record's `scalar_pairing` is empty and the verdict folds them."""
        records = self._records()
        for rec in records:
            self.assertNotIn("sources_read", rec)
            self.assertEqual(rec["scalar_pairing"], {})
            self.assertEqual(rec["grammar_version"], 6)
        self.assertEqual(records[1]["cases_read"], ["a", "b"])
        self.assertEqual(evaluate_verdict(self.tests, records)["per_test"][0]["status"], "pass")


# ---------------------------------------------------------------------------- schema gate

class SchemaGateTest(unittest.TestCase):
    def _v(self, preds, *, ir: dict | None = None, pin_targets: bool = True) -> list[str]:
        ir = ir or _ir(preds)
        cases = {c["case_id"]: c for c in ir["case"]["test_case_set"]}
        targets = {p["test_id"]: p["target_cases"]
                   for p in ir["io_contract"]["tests"]} if pin_targets else None
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
            ([{**HMIN, "na_allowed": True}], "na_allowed is not a predicate key"),
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
            ([{**HMIN, "bind": {"atan2": "1"}}], "shadows a grammar name"),
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
            ([{**HMIN, "expr": "final.h"}], "evaluates to an array of rank 2, not a scalar"),
            ([{**HMIN, "expr": "norm2(roll(final.h, 1))"}],
             "roll(): 1 shift(s) for an array of rank 2"),
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

    def test_a_case_scoped_input_path_resolves_in_its_case_alone(self) -> None:
        """Issue #532: under `case: X` the predicate is evaluated in X alone, so a bare
        `inputs.<path>` resolves in X and not in the other target cases; it is still refused
        where X lacks it, and `per_case: true` still resolves it in every target case."""
        ir = _ir([SYM])
        del ir["case"]["test_case_set"][1]["inputs"]["grid"]["dx"]
        scoped = {**SYM, "expr": "maxabs(final.h) * inputs.grid.dx"}
        self.assertEqual(self._v([scoped], ir=ir), [])
        out = self._v([{**scoped, "case": "b"}], ir=ir)
        self.assertEqual(len(out), 1, out)
        self.assertIn("in case 'b': inputs.grid.dx is not a key", out[0])
        out = self._v([{**scoped, "case": " a "}], ir=ir)
        self.assertEqual(out, [])
        # the narrowing is the bare reference's: an at('b') input is still read in 'b'
        out = self._v([{**scoped, "expr": "maxabs(final.h) * at('b').inputs.grid.dx"}], ir=ir)
        self.assertEqual(len(out), 1, out)
        self.assertIn("in case 'b': inputs.grid.dx is not a key", out[0])
        out = self._v([{k: v for k, v in scoped.items() if k != "case"} | {"per_case": True}],
                      ir=ir)
        self.assertEqual(len(out), 1, out)
        self.assertIn("in case 'b': inputs.grid.dx is not a key", out[0])
        # a malformed scope is reported, and the path is then resolved in every target case
        out = self._v([{**scoped, "per_case": True}], ir=ir)
        self.assertEqual(out[0], "predicates[0]: exactly one of `per_case: true` or "
                                 "`case: <case_id>` names the scope")
        self.assertEqual(len(out), 2, out)
        self.assertIn("in case 'b': inputs.grid.dx is not a key", out[1])

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
        # a state read inside a bind counts only when `expr` reaches that bind (transitively)
        self.assertEqual(self._v([{**HMIN, "bind": {"a": "sum(final.h)", "b": "a * 2"},
                                   "expr": "b"}]), [])
        out = self._v([{**HMIN, "bind": {"unused": "final.h"}, "expr": "1.0"}])
        self.assertTrue(any("reads no captured state" in m for m in out), out)
        out = self._v([{**HMIN, "bind": {"z": "at('b').final.h"}, "expr": "1"}])
        self.assertTrue(any("reads no captured state" in m for m in out), out)
        # the reach walk is shared with `cases_read` (issue #567); over the effective
        # references its frontier is the PRUNED expr, so a bind named only inside `0 * a` is
        # not reached and the predicate reads no state outside a term constant by form
        out = self._v([{**HMIN, "bind": {"a": "sum(final.h)"}, "expr": "1.0 + 0 * a"}])
        self.assertEqual(len(out), 1, out)
        self.assertIn("reads no captured state", out[0])
        self.assertIn("outside a term multiplied by a literal zero", out[0])
        # and the keys the secondary side has are refused here
        for key in ("na_allowed", "expected_outcome"):
            out = self._v([{**HMIN, key: "pass"}])
            self.assertTrue(any(f"{key} is not a predicate key" in m for m in out), out)

    def test_a_state_read_only_inside_a_term_constant_by_form_is_refused(self) -> None:
        """Issue #483: a read inside `X * 0`, `X ** 0`, `X - X` or `X + -X` (after `-(-X)` is
        collapsed) does not enter the value, so it is no read — the message keeps the "reads no captured state" prefix and names the
        forms. Pruning is children first, and a bind is pruned before `expr` reaches it."""
        form = "outside a term multiplied by a literal zero"
        refused = [
            {"expr": "sum(final.h * 0 + 1)"},
            {"expr": "0.0 * sum(final.h) + 1"},
            {"expr": "sum(final.h) * -0"},
            {"expr": "sum(final.h) ** 0"},
            {"expr": "sum(final.h - final.h)"},
            {"expr": "sum(final.h) + -sum(final.h) + 1"},
            {"expr": "-sum(final.h) + sum(final.h) + 1"},
            {"expr": "sum(final.h) - -(-sum(final.h)) + 1"},
            {"expr": "-(sum(final.h) * 0) + 1"},
            {"expr": "sum(-(final.h * 0)) + 1"},
            {"expr": "-(sum(final.h) - sum(final.h)) + 1"},
            {"expr": "sum(final.h) + -(-(-sum(final.h))) + 1"},
            {"expr": "sum(final.h) * (final.s * 0)"},
            {"bind": {"m": "sum(final.h)"}, "expr": "m * 0"},
            {"bind": {"m": "sum(final.h)"}, "expr": "(m - m) * 2"},
            {"bind": {"a": "final.h"}, "expr": "sum((a - a) * final.h)"},
            {"bind": {"a": "sum(final.h)", "b": "a * 0"}, "expr": "b + 1"},
        ]
        for extra in refused:
            with self.subTest(**{k: str(v) for k, v in extra.items()}):
                out = self._v([{**HMIN, **extra}])
                self.assertEqual(len(out), 1, out)
                self.assertIn("reads no captured state", out[0])
                self.assertIn(form, out[0])
        accepted = [
            {"expr": "sum(final.h) * 0.5"},
            {"expr": "sum(final.h) - sum(initial.h)"},
            {"expr": "min(final.h) + 0"},
            {"expr": "min(final.h) + 0 * sum(final.h)"},
            {"expr": "min(final.h) + 0.0 * inputs.grid.nx"},
            {"expr": "sum(final.h ** 0 * final.h)"},
            {"expr": "sum(0 / (final.h + 1))"},
            {"expr": "sum(final.h / final.h)"},
            {"expr": "sum(final.h - initial.h)"},
            {"expr": "sum(final.h + -initial.h)"},
            {"expr": "sum(final.h - -final.h)"},
            {"expr": "sum(final.h + -(-final.h))"},
            {"expr": "sum(-(-final.h))"},
            {"bind": {"a": "sum(final.h)", "b": "a - a"}, "expr": "b + a"},
        ]
        for extra in accepted:
            with self.subTest(**{k: str(v) for k, v in extra.items()}):
                self.assertEqual(self._v([{**HMIN, **extra}]), [])
        # an expression that reads no state at all keeps the original message
        out = self._v([{**HMIN, "expr": "inputs.grid.nx * 0"}])
        self.assertEqual(len(out), 1, out)
        self.assertNotIn(form, out[0])

    def test_effective_names_prunes_terms_constant_by_form(self) -> None:
        def names(src: str) -> list[str]:
            return [f"{r.point}.{r.name}" if r.point else r.name
                    for r in pe.effective_names(pe.parse_expr(src))]
        self.assertEqual(names("final.h * 0 + initial.u"), ["initial.u"])
        self.assertEqual(names("0 * final.h"), [])
        self.assertEqual(names("-0.0 * final.h"), [])
        self.assertEqual(names("final.h ** 0 * final.u"), ["final.u"])   # X ** 0 is 1, not 0
        self.assertEqual(names("(final.h - final.h) * final.u"), [])     # children first
        self.assertEqual(names("final.h - initial.h"), ["final.h", "initial.h"])
        self.assertEqual(names("final.h + -final.h + initial.u"), ["initial.u"])
        self.assertEqual(names("-final.h + final.h + initial.u"), ["initial.u"])
        self.assertEqual(names("final.h + -initial.h"), ["final.h", "initial.h"])
        self.assertEqual(names("final.h - -(-final.h) + initial.u"), ["initial.u"])
        self.assertEqual(names("-(-final.h)"), ["final.h"])
        self.assertEqual(names("0 ** final.h + final.h / final.h"),
                         ["final.h", "final.h", "final.h"])
        self.assertEqual(names("final.h * 0.0001"), ["final.h"])
        tree = pe.parse_expr("final.h * 0")
        pe.effective_names(tree)
        self.assertEqual(names("final.h * 0"), [])
        self.assertEqual([r.name for r in pe.expr_names(tree)], ["h"])   # input untouched

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

    # ---- the static rank check (issue #440)

    _PAIR = ("(an operand must be a scalar or of equal rank; right-aligned broadcasting is not "
             "admitted)")
    _COORD = "; a coordinate has the state's rank 3, the highest rank among the snapshot variables"
    _SCALAR = "not a scalar (reduce it with sum/min/max/mean/norm2/maxabs)"
    _YC: ClassVar[list[dict]] = [{"name": "yc", "axis": 2, "count": "inputs.grid.ny",
                                  "length": "inputs.grid.L_y", "placement": "cell_center"}]

    def test_a_coordinate_pairs_only_with_variables_of_the_state_rank(self) -> None:
        ok = {**HMIN, "expr": "maxabs(final.U - yc)"}
        self.assertEqual(self._v([ok], ir=_mixed_ir([ok], self._YC)), [])
        bad = {**HMIN, "expr": "maxabs(final.hv - yc)"}
        self.assertEqual(self._v([bad], ir=_mixed_ir([bad], self._YC)), [
            (f"predicates[0].expr: operator -: operands of rank 2 and 3 {self._PAIR}"
             f"{self._COORD}")])
        # under at('<case>') a coordinate has the same rank
        bad_at = {**HMIN, "expr": "maxabs(final.hv - at('b').yc)"}
        self.assertEqual(self._v([bad_at], ir=_mixed_ir([bad_at], self._YC)), [
            (f"predicates[0].expr: operator -: operands of rank 2 and 3 {self._PAIR}"
             f"{self._COORD}")])
        # a refusal that names no coordinate carries no coordinate suffix
        plain = {**HMIN, "expr": "maxabs(final.hv - final.U)"}
        self.assertEqual(self._v([plain], ir=_mixed_ir([plain], self._YC)), [
            f"predicates[0].expr: operator -: operands of rank 2 and 3 {self._PAIR}"])

    def test_the_issue_440_predicate_is_refused_at_compile(self) -> None:
        """The coriolis 0.1.1 IR's coordinate, bind and expr verbatim: the bind is rank 3 (the
        coordinate's), and `expr` pairs it with rank-2 `initial.hv`. Without the stacked
        rank-3 captures the state's rank is 2 and the same predicate is accepted."""
        coords = [{"name": "row_center", "axis": 1, "count": "inputs.grid.ny",
                   "length": "inputs.grid.ny", "placement": "cell_center"}]
        pred = {**HMIN, "bind": {
            "f_rows": "inputs.constants.f0 + inputs.constants.df * (row_center - 0.5)"},
            "expr": "max(maxabs(final.S_h), maxabs(final.S_hu - f_rows * initial.hv), "
                    "maxabs(final.S_hv + f_rows * initial.hu))", "value": 1e-12}
        out = self._v([pred], ir=_mixed_ir([pred], coords))
        self.assertEqual(out, [(f"predicates[0].expr: operator *: operands of rank 3 and 2 "
                                f"{self._PAIR}{self._COORD}")])
        rank2 = [e for e in MIXED_VARIABLES if e["name"] not in ("U", "S")]
        self.assertEqual(self._v([pred], ir=_mixed_ir([pred], coords, rank2)), [])

    def test_an_array_valued_expr_is_refused(self) -> None:
        for expr, rank in (("final.h", 2), ("final.h - initial.h", 2),
                           ("min(initial.h, final.h)", 2), ("abs(final.h)", 2),
                           ("-final.h", 2), ("roll(final.h, 1, 0)", 2)):
            with self.subTest(expr=expr):
                self.assertEqual(self._v([{**HMIN, "expr": expr}]), [
                    (f"predicates[0].expr: evaluates to an array of rank {rank}, "
                     f"{self._SCALAR}")])

    def test_a_bind_of_array_rank_reduced_in_expr_is_accepted(self) -> None:
        self.assertEqual(self._v([{**HMIN, "bind": {"d": "final.h - initial.h"},
                                   "expr": "maxabs(d)"}]), [])
        self.assertEqual(self._v([{**HMIN, "bind": {"d": "final.h - initial.h", "q": "d * d"},
                                   "expr": "sum(q) + final.s"}]), [])
        # a bind's rank flows into what reads it
        out = self._v([{**HMIN, "bind": {"d": "final.h - initial.h"}, "expr": "d"}])
        self.assertEqual(out, [(f"predicates[0].expr: evaluates to an array of rank 2, "
                                f"{self._SCALAR}")])
        # an unused bind is still ranked: every bind is evaluated at run time
        out = self._v([{**HMIN, "bind": {"bad": "final.h + inputs.initial.a1"},
                        "expr": "min(final.h)"}], ir=self._with_inputs())
        self.assertEqual(out, [(f"predicates[0].bind.bad: operator +: operands of rank 2 "
                                f"and 1 {self._PAIR}")])

    def test_roll_shift_count_is_checked_at_compile(self) -> None:
        self.assertEqual(self._v([{**HMIN, "expr": "maxabs(roll(final.h, 1))"}]), [
            "predicates[0].expr: roll(): 1 shift(s) for an array of rank 2"])
        self.assertEqual(self._v([{**HMIN, "expr": "maxabs(roll(final.h, final.h, 0))"}]), [
            "predicates[0].expr: roll(): a shift must be a scalar"])
        self.assertEqual(self._v([{**HMIN, "expr": "roll(final.s, 1)"}]), [
            "predicates[0].expr: roll(): 1 shift(s) for an array of rank 0"])
        self.assertEqual(self._v([SYM]), [])

    def test_array_first_ranks_are_checked_at_compile(self) -> None:
        """Grammar 5: `transpose` / `flip` / `coarsen` of a scalar, or with an array where the
        axis or the factor goes, is refused before any run; a `flip` axis outside the rank or a
        non-dividing factor needs values and stays an evaluation error."""
        for expr, msg in (("maxabs(flip(final.h, final.h))",
                           "flip(): every argument after the first must be a scalar"),
                          ("maxabs(coarsen(final.h, final.h))",
                           "coarsen(): every argument after the first must be a scalar"),
                          ("transpose(final.s)", "transpose(): the first argument must be an array"),
                          ("flip(final.s, 0)", "flip(): the first argument must be an array"),
                          ("maxabs(transpose(final.h, 1))",
                           ("transpose(): takes the array alone, or the array and the two axes "
                            "it exchanges")),
                          ("maxabs(transpose(final.h, 0, final.h))",
                           "transpose(): every argument after the first must be a scalar")):
            with self.subTest(expr=expr):
                self.assertEqual(self._v([{**HMIN, "expr": expr}]),
                                 [f"predicates[0].expr: {msg}"])
        self.assertEqual(self._v([{**HMIN, "expr": "maxabs(flip(final.h, 7))"}]), [])
        self.assertEqual(self._v([{**HMIN, "expr": "maxabs(coarsen(final.h, 3))"}]), [])

    @staticmethod
    def _with_inputs(b_a2: object = None) -> dict:
        ir = _ir([HMIN])
        for case in ir["case"]["test_case_set"]:
            case["inputs"]["initial"]["a2"] = [[1.0] * NY for _ in range(NX)]
            case["inputs"]["initial"]["a1"] = [1.0] * NX
        if b_a2 is not None:
            ir["case"]["test_case_set"][1]["inputs"]["initial"]["a2"] = b_a2
        return ir

    def test_an_input_list_pairs_by_its_rank_in_each_case(self) -> None:
        ok = {**HMIN, "expr": "maxabs(final.h - inputs.initial.a2)"}
        self.assertEqual(self._v([ok], ir=self._with_inputs()), [])
        bad = {**HMIN, "expr": "maxabs(final.h - inputs.initial.a1)"}
        self.assertEqual(self._v([bad], ir=self._with_inputs()), [
            f"predicates[0].expr: operator -: operands of rank 2 and 1 {self._PAIR}"])
        # the rank differs between cases: the refusing case is named
        ir = self._with_inputs(b_a2=[1.0] * NX)
        self.assertEqual(self._v([ok], ir=ir), [
            (f"predicates[0].expr: in case 'b': operator -: operands of rank 2 and 1 "
             f"{self._PAIR}")])
        # under at('<case>') the input is read in THAT case alone, in every case's evaluation
        self.assertEqual(self._v([{**HMIN, "expr": "maxabs(final.h - at('a').inputs.initial.a2)"}],
                                 ir=ir), [])
        # beside an own-case input (ranked per case), an at('a') input is still read in 'a'
        both = {**HMIN, "expr": "maxabs(final.h - at('a').inputs.initial.a2) + inputs.grid.nx"}
        self.assertEqual(self._v([both], ir=ir), [])
        # every case refuses, each differently: the first refusing case is named
        ir3 = self._with_inputs(b_a2=[1.0] * NX)
        ir3["case"]["test_case_set"][0]["inputs"]["initial"]["a2"] = [[[1.0]] * NY] * NX
        self.assertEqual(self._v([ok], ir=ir3), [
            (f"predicates[0].expr: in case 'a': operator -: operands of rank 2 and 3 "
             f"{self._PAIR}")])
        # a `case:` scope is evaluated in its one case: the other target's rank is not read
        self.assertEqual(self._v([{**SYM, "expr": "maxabs(final.h - inputs.initial.a2)"}],
                                 ir=ir), [])

    def test_elementwise_min_max_and_atan2_pair_by_rank(self) -> None:
        ir = self._with_inputs()
        self.assertEqual(self._v([{**HMIN, "expr": "maxabs(max(final.h, 0))"}], ir=ir), [])
        self.assertEqual(self._v([{**HMIN, "expr": "maxabs(min(final.h, inputs.initial.a2, 1))"}],
                                 ir=ir), [])
        for name in ("max", "min", "atan2"):
            with self.subTest(name=name):
                out = self._v([{**HMIN, "expr": f"maxabs({name}(final.h, inputs.initial.a1))"}],
                              ir=ir)
                self.assertEqual(out, [(f"predicates[0].expr: {name}(): operands of rank "
                                        f"2 and 1 {self._PAIR}")])

    def test_a_malformed_scope_still_ranks_an_own_case_input(self) -> None:
        """With both scopes given, the scope violation is reported and the rank check falls
        back to every target case rather than to no case (where an own-case `inputs.` ref has
        no case to be read in)."""
        ir = self._with_inputs(b_a2=[1.0] * NX)
        out = self._v([{**HMIN, "case": "a", "expr": "maxabs(final.h - inputs.initial.a2)"}],
                      ir=ir)
        self.assertEqual(out[0], "predicates[0]: exactly one of `per_case: true` or "
                                 "`case: <case_id>` names the scope")
        self.assertEqual(out[1:], [
            (f"predicates[0].expr: in case 'b': operator -: operands of rank 2 and 1 "
             f"{self._PAIR}")])

    def test_a_time_variable_listed_among_the_variables_has_rank_0(self) -> None:
        """`_load_capture` reads the time variable as a scalar whatever `variables` says about
        it, so the rank check does too."""
        ir = _ir([HMIN], variables=[{"name": "h", "shape_expr": "[nx, ny]"},
                                    {"name": "t", "shape_expr": "[nx]"}])
        # grammar 6 refuses `t` as a variable name; the rank check still reads it as rank 0
        reserved = ["state_snapshots.schema.variables: 't' is the driver's or the harness's "
                    "own key (['case_id', 'step', 't']), not a variable name"]
        self.assertEqual(self._v([{**HMIN, "expr": "sum(final.h) * final.t"}], ir=ir), reserved)
        self.assertEqual(self._v([{**HMIN, "expr": "final.t"}], ir=ir), reserved)

    def test_a_rank_1_expr_is_refused(self) -> None:
        ir = _ir([HMIN], variables=[{"name": "h", "shape_expr": "[nx, ny]"},
                                    {"name": "v", "shape_expr": "[nx]"}])
        self.assertEqual(self._v([{**HMIN, "expr": "final.v"}], ir=ir), [
            f"predicates[0].expr: evaluates to an array of rank 1, {self._SCALAR}"])

    def test_a_rank_check_does_not_cascade(self) -> None:
        out = self._v([{**HMIN, "expr": "final.h + zz"}])
        self.assertEqual(len(out), 1, out)
        self.assertIn("name 'zz' is not a coordinate", out[0])
        coords = [{"name": "x", "axis": 0, "count": NX, "length": 1.0, "placement": "cell_center"}]
        ir = _ir([HMIN], coordinates=coords)
        out = self._v([{**HMIN, "bind": {"x": "1"}, "expr": "final.h"}], ir=ir)
        self.assertEqual(len(out), 1, out)
        self.assertIn("shadows a grammar name", out[0])
        out = self._v([{**HMIN, "bind": {"A": "final.h["}, "expr": "final.h"}])
        self.assertEqual(len(out), 1, out)
        self.assertIn("does not parse", out[0])
        # a violation outside the names (here the value) does not suppress the rank check
        out = self._v([{**HMIN, "value": None, "expr": "final.h"}])
        self.assertEqual(len(out), 2, out)
        self.assertIn("not a scalar", out[1])


class StaticRankTest(unittest.TestCase):
    """`expr_rank` is the Compile-time mirror of `_eval_node` / `_call` / `_shape_compatible`
    over RANKS: its table is pinned by set identity against `FUNCTIONS`, and its answers by
    parity with evaluation — the rank it infers is `np.ndim` of the value, and a refusal it
    raises is the evaluator's own message."""

    def _envs(self) -> tuple[pe.RankEnv, pe.CaseEnv]:
        h = np.arange(1.0, 1.0 + NX * NY).reshape(NX, NY) / (NX * NY)
        v = np.linspace(0.1, 0.9, NX)
        cap = {"h": h, "v": v, "s": 0.5, "t": 0.2, "step": 3.0}
        inputs = {"k": 2.0, "a1": v.tolist(), "a2": h.tolist()}
        history = {"h": np.stack([h, 2 * h, 3 * h]), "v": np.stack([v, v]),
                   "s": np.array([0.5, 0.7]), "t": np.array([0.1, 0.2]),
                   "step": np.array([1.0, 2.0])}
        series = {"t": np.array([0.1, 0.2, 0.3]), "h_max": np.array([1.0, 1.1, 1.2])}
        derived = {"dt": 0.01, "f": h * 0 + 1.0, "w": v.copy()}
        env = pe.CaseEnv(case_id="a", initial=dict(cap), final=dict(cap), inputs=inputs,
                         coordinates={"x": h * 0 + 0.25}, history=history, series=series,
                         derived=derived, state_rank=2)
        ranks = {"h": 2, "v": 1, "s": 0, "t": 0, "step": 0}
        renv = pe.RankEnv(captures=ranks, coordinates=frozenset({"x"}), coordinate_rank=2,
                          input_rank=lambda cid, p: int(np.ndim(
                              pe.resolve_input_value(env.inputs, p))), case="a",
                          derived_rank=lambda cid, n: int(np.ndim(derived[n])))
        return renv, env

    def test_every_function_has_a_rank_rule(self) -> None:
        self.assertEqual(set(pe._RANK_RULES), set(pe.FUNCTIONS))
        self.assertEqual(set(pe._RANK_RULES.values()),
                         {"reduce", "reduce_or_pairwise", "elementwise", "pairwise", "roll",
                          "array_first", "sample_reduce"})

    ROWS = (
        "sum(final.h)", "mean(final.h)", "min(final.h)", "max(final.h)", "abs(final.h)",
        "sqrt(final.h)", "exp(final.h)", "log(final.h)", "log2(final.h)", "sin(final.h)",
        "cos(final.h)", "norm2(final.h)", "maxabs(final.h)", "roll(final.h, 1, 0)",
        "roll(final.v, 1)", "ceil(final.h)", "floor(final.h)", "atan2(final.h, final.h)",
        "min(final.h, 0.5)", "max(0.5, final.h, initial.h)", "atan2(final.s, final.v)",
        "abs(final.s)", "sum(final.s)", "min(final.s, final.t)",
        "1.5", "pi", "-final.h", "final.h + 1", "2 * final.v", "final.s ** 2", "final.h / final.h",
        "x", "x * final.h", "at('a').x", "final.t", "initial.h - final.h",
        "inputs.k", "inputs.a1", "inputs.a2 - final.h", "at('a').inputs.a1",
        "at('a').final.v", "comparand.final.h", "comparand.initial.s",
        "transpose(final.h)", "transpose(final.v)", "flip(final.h, 1)", "flip(final.v, 0)",
        "transpose(final.h, 0, 1)",
        "coarsen(final.h, 2)", "coarsen(final.v, 2)", "norm2(final.h - flip(at('a').final.h, 0))",
        "sum(coarsen(final.h, inputs.k))",
        # grammar 6
        "final.step", "initial.step", "history.h", "history.v", "history.s", "history.t",
        "history.step", "series.h_max", "derived.dt", "derived.f", "derived.w",
        "smax(history.h)", "smin(history.v)", "ssum(history.h)", "smax(series.t)",
        "smax(history.s)", "max(smax(history.h * derived.dt))", "x * history.h",
        "history.h - x", "at('a').x + history.h", "sum(history.h)", "final.h - derived.f",
    )

    def test_static_rank_is_the_evaluated_rank(self) -> None:
        renv, env = self._envs()
        called: set[str] = set()
        for expr in self.ROWS:
            with self.subTest(expr=expr):
                tree = pe.parse_expr(expr)
                called |= {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call)}
                value = pe.evaluate(tree, env, at_envs={"a": env}, comparand_env=env)
                self.assertEqual(pe.expr_rank(tree, renv, {}), np.ndim(value))
        self.assertEqual(set(pe.FUNCTIONS) - called, set())
        # a bind's rank is what reads it
        self.assertEqual(pe.expr_rank(pe.parse_expr("d + 1"), renv, {"d": 1}), 1)

    def test_static_refusals_are_the_evaluator_refusals(self) -> None:
        renv, env = self._envs()
        for expr in ("final.h + final.v", "final.v - x", "inputs.a1 * final.h",
                     "roll(final.h, 1)", "roll(final.h, final.h, 0)", "roll(final.s, 1)",
                     "max(final.h, final.v)", "min(final.h, 0, final.v)",
                     "atan2(final.h, final.v)", "sum(final.h + final.v)",
                     "transpose(final.s)", "flip(final.s, 0)", "coarsen(final.t, 1)",
                     "transpose(final.h, 1)", "transpose(final.h, final.h, 1)",
                     "transpose(final.h, 0, final.h)",
                     "flip(final.h, final.v)", "coarsen(final.h, final.h)",
                     "transpose(final.h) + final.v", "flip(final.h, 0) - final.v",
                     # grammar 6
                     "smax(final.s)", "smax(1.0)", "history.h - final.v", "history.h + final.h",
                     "derived.w * final.h", "sqrt(x) * history.h"):
            with self.subTest(expr=expr):
                tree = pe.parse_expr(expr)
                with self.assertRaises(pe.PrimaryEvidenceError) as runtime:
                    pe.evaluate(tree, env, at_envs={"a": env})
                with self.assertRaises(pe.PrimaryEvidenceError) as static:
                    pe.expr_rank(tree, renv, {})
                self.assertEqual(str(static.exception), str(runtime.exception))


# ------------------------------------------------------------------------- doc coupling

class CasesReadTest(unittest.TestCase):
    """`cases_read`, what a record says it valued: the reader the reported-read gate and the
    per-test reads-every-case rule stand on."""

    # Channel IR `problem__shallow_water2d_channel__0.2.5/shallow-water2d-channel_20261029_001`,
    # predicate [300] (`restricted_order_n032_to_n064_hu_l2`, `case: n256`): the Compile leaf
    # copied one 18-bind block into every restricted predicate; `expr` reaches 4 binds (a32u,
    # a64u, r32u, r64u), and three of the 14 unreached ones read at('…n128…'), which the value
    # never touches (measured with `reached_binds` over `effective_names` at 93be8321; the
    # issue body's "6 of them … 12 unreached" was miscounted).
    _CHANNEL_300_BIND: ClassVar[dict[str, str]] = {
        "a32h": "at('chan_tc2_xpert_n032_dts100').final.h",
        "a32u": "at('chan_tc2_xpert_n032_dts100').final.hu",
        "a32v": "at('chan_tc2_xpert_n032_dts100').final.hv",
        "r32h": "coarsen(final.h, 8)",
        "r32u": "coarsen(final.hu, 8)",
        "r32v": "coarsen(final.hv, 8)",
        "a64h": "at('chan_tc2_xpert_n064_dts100').final.h",
        "a64u": "at('chan_tc2_xpert_n064_dts100').final.hu",
        "a64v": "at('chan_tc2_xpert_n064_dts100').final.hv",
        "r64h": "coarsen(final.h, 4)",
        "r64u": "coarsen(final.hu, 4)",
        "r64v": "coarsen(final.hv, 4)",
        "a128h": "at('chan_tc2_xpert_n128_dts100').final.h",
        "a128u": "at('chan_tc2_xpert_n128_dts100').final.hu",
        "a128v": "at('chan_tc2_xpert_n128_dts100').final.hv",
        "r128h": "coarsen(final.h, 2)",
        "r128u": "coarsen(final.hu, 2)",
        "r128v": "coarsen(final.hv, 2)",
    }

    def test_cases_read_counts_only_the_references_that_enter_the_value(self) -> None:
        """Issue #567: under `case: X` a corroborant values X plus the at('<case>') cases that
        enter its value — in `expr` or a bind `expr` reaches (directly or transitively),
        outside a term constant by form. A dead bind, a `0 *` term, a bind reached only
        through a pruned term, and an at() inside `X - X` add no case. The last row is the
        corpus witness: channel predicate [300]'s bind block (`_CHANNEL_300_BIND`) values
        three cases, not the four its raw references name."""
        def p(expr: str, bind: dict | None = None) -> dict:
            out = {"test_id": "t", "quantity": "q", "expr": expr, "op": "le", "value": 1.0,
                   "target_cases": ["a", "b"], "case": "a"}
            if bind is not None:
                out["bind"] = bind
            return out
        rows = [
            (p("final.s"), {"a"}),
            (p("final.s", {"dead": "at('b').final.s"}), {"a"}),
            (p("final.s + 0 * sum(at('b').final.s)"), {"a"}),
            (p("final.s + 0 * other", {"other": "at('b').final.s"}), {"a"}),
            (p("final.s + w", {"w": "(at('b').final.s - at('b').final.s) * 2"}), {"a"}),
            (p("final.s - other", {"other": "at('b').final.s"}), {"a", "b"}),
            (p("final.s - c", {"other": "at('b').final.s", "c": "other * 2"}), {"a", "b"}),
            # a transitive chain with no own-case read values b alone (review round 1)
            (p("c", {"other": "at('b').final.s", "c": "other * 2"}), {"b"}),
            (p("final.s - at('b').final.s"), {"a", "b"}),
            # a bind reached only through ANOTHER dead bind is dead too
            (p("final.s", {"x": "at('b').final.s", "y": "x * 2"}), {"a"}),
        ]
        for pred, want in rows:
            with self.subTest(expr=pred["expr"], bind=str(pred.get("bind"))):
                self.assertEqual(pe.cases_read(pred, state_names=STATE), want)
        n = "chan_tc2_xpert_n{:03d}_dts100".format
        channel = {"test_id": "l1_tc2_perturbed_refinement_restricted",
                   "quantity": "restricted_order_n032_to_n064_hu_l2",
                   "target_cases": [n(32), n(64), n(128), n(256)], "case": n(256),
                   "bind": dict(self._CHANNEL_300_BIND),
                   "expr": "log2((norm2(a32u - r32u) / norm2(r32u)) / "
                           "(norm2(a64u - r64u) / norm2(r64u)))",
                   "op": "ge", "value": 1.1}
        channel_state = {"h", "hu", "hv"}
        self.assertEqual(pe.cases_read(channel, state_names=channel_state),
                         {n(32), n(64), n(256)})
        # the same block with `expr` reaching an n128 bind values all four
        self.assertEqual(pe.cases_read({**channel, "expr": channel["expr"] + " + "
                                        "norm2(a128u - r128u)"}, state_names=channel_state),
                         {n(32), n(64), n(128), n(256)})
        # an unparsable bind, reached or not, still makes the predicate cover nothing
        self.assertIsNone(pe.cases_read(p("final.s", {"bad": "final.s +"}), state_names=STATE))

    def test_only_a_state_read_values_a_case(self) -> None:
        """Issue #567 review round 1: a case is valued only by a captured STATE read that
        enters the value. `at('b').inputs.<path>` (a normaliser), `at('b').final.t` (the time
        variable is not state) and `at('b').x` (a coordinate) value none of b's state, so a
        `case: a` predicate carrying one of them beside its own state reads a alone; one
        reading ONLY `at('b').final.s` values b alone (its own case is never read), under
        `case: a` and under `per_case: true` alike; a `comparand.` read is another target's
        state and values no case of this run. The schema gate passes every one of these
        (each reads captured state)."""
        t = [{"test_id": "t_mass", "target_cases": ["a", "b"], "quantities": ["pc"]}]

        def case_p(expr: str, **extra: object) -> dict:
            return {"test_id": "t_mass", "quantity": "pc", "op": "le", "value": 1.0,
                    "target_cases": ["a", "b"], "case": "a", "expr": expr, **extra}

        for expr, read in (("final.s / at('b').inputs.grid.nx", {"a"}),
                           ("final.s * at('b').final.t", {"a"}),
                           ("final.s + sum(at('b').x)", {"a"}),
                           ("sum(at('b').final.s)", {"b"}),
                           ("final.t + sum(at('b').final.s)", {"b"}),
                           ("final.s + comparand.final.s", {"a"}),
                           ("final.s + at('b').final.s", {"a", "b"}),
                           ("final.s + sum(at('b').final.h)", {"a", "b"})):
            with self.subTest(expr=expr):
                p = case_p(expr)
                self.assertEqual(pe.cases_read(p, state_names=STATE), read)
                v = pe.quantity_violations(t, [p], STATE)
                self.assertEqual(len(v), 0 if read == {"a", "b"} else 1, v)
        # the same read through a reached bind, and through a bind the frontier would reach
        # only if it matched a capture NAME against a bind name (the walk follows `name`
        # references alone: a bind called `h` is not reached by `final.h`)
        p = case_p("final.s - other", bind={"other": "at('b').inputs.grid.nx"})
        self.assertEqual(pe.cases_read(p, state_names=STATE), {"a"})
        p = case_p("final.s - other", bind={"other": "at('b').final.s"})
        self.assertEqual(pe.cases_read(p, state_names=STATE), {"a", "b"})
        p = case_p("min(final.h)", bind={"h": "sum(at('b').final.s)"})
        self.assertEqual(pe.cases_read(p, state_names=STATE), {"a"})
        # `per_case: true` values every target case only through an own-case state read
        pc = {k: v_ for k, v_ in case_p("sum(at('b').final.s)").items() if k != "case"}
        pc["per_case"] = True
        self.assertEqual(pe.cases_read(pc, state_names=STATE), {"b"})
        pc["expr"] = "final.s"
        self.assertEqual(pe.cases_read(pc, state_names=STATE), {"a", "b"})
        # an `initial.` state read values its case as a `final.` one does (round 3)
        p = case_p("final.s - sum(at('b').initial.h)")
        self.assertEqual(pe.cases_read(p, state_names=STATE), {"a", "b"})
        # with no state names (no snapshot schema) nothing is valued: fail-closed
        self.assertEqual(pe.cases_read(case_p("final.s"), state_names=set()), set())
        self.assertEqual(len(pe.quantity_violations(t, [case_p("final.s + at('b').final.s")],
                                                    set())), 1)

    def test_a_malformed_scope_or_expression_values_nothing(self) -> None:
        for bad in ({"per_case": True, "case": "a"}, {}, {"per_case": False}, {"case": ""},
                    {"case": 1}, {"per_case": True, "target_cases": "a"},
                    {"case": "a", "expr": "final.s +"}, {"case": "a", "bind": {"x": "("}}):
            with self.subTest(bad=bad):
                p = {"target_cases": ["a"], "expr": "final.s", **bad}
                self.assertIsNone(pe.cases_read(p, state_names=STATE))
        # an expr reading other cases' state alone values those cases and not its own
        self.assertEqual(pe.cases_read({"case": "a", "target_cases": ["a", "b"],
                                        "expr": "at('b').final.s + at('c').final.s"},
                                       state_names=STATE),
                         {"b", "c"})


class QuantityGateTest(unittest.TestCase):
    """`quantity_violations` (D10): the replacement of the coverage gate's omission property."""

    _T: ClassVar[list[dict]] = [
        {"test_id": "t_mass", "target_cases": ["a"], "quantities": ["mass", "overall"]},
        {"test_id": "t_sym", "target_cases": ["a"], "quantities": ["guard"]},
    ]

    @staticmethod
    def _p(test_id: str, quantity: str, **kw: object) -> dict:
        return {"test_id": test_id, "quantity": quantity, "expr": "final.s", "op": "le",
                "value": 1.0, "target_cases": ["a"], "per_case": True, **kw}

    def test_equal_sets_pass_and_a_second_predicate_of_a_quantity_changes_nothing(self) -> None:
        full = [self._p("t_mass", "mass"), self._p("t_mass", "overall"), self._p("t_sym", "guard")]
        self.assertEqual(pe.quantity_violations(self._T, full, STATE), [])
        self.assertEqual(pe.quantity_violations(self._T, [*full, self._p("t_sym", "guard")], STATE),
                         [])

    def test_a_listed_quantity_with_no_predicate_is_named(self) -> None:
        v = pe.quantity_violations(self._T, [self._p("t_mass", "mass"), self._p("t_sym", "guard")],
                                   STATE)
        self.assertEqual(len(v), 1, v)
        self.assertIn("t_mass", v[0])
        self.assertIn("['overall']", v[0])
        self.assertIn("judges", v[0])

    def test_an_unlisted_quantity_is_named(self) -> None:
        v = pe.quantity_violations(self._T, [self._p("t_mass", "mass"), self._p("t_mass", "overall"),
                                             self._p("t_sym", "guard"), self._p("t_sym", "extra")],
                                   STATE)
        self.assertEqual(len(v), 1, v)
        self.assertIn("['extra']", v[0])

    def test_a_predicate_counts_for_its_own_test_only(self) -> None:
        v = pe.quantity_violations(self._T, [self._p("t_sym", "mass"), self._p("t_sym", "overall"),
                                             self._p("t_mass", "guard")], STATE)
        self.assertEqual(len(v), 4, v)
        v = pe.quantity_violations(self._T, [self._p(" t_mass ", "mass"), self._p("t_mass", "overall"),
                                             self._p("t_sym", "guard")], STATE)
        self.assertEqual(v, [], "a test_id is compared stripped, as the schema gates do")

    def test_a_cross_target_predicate_stands_for_no_listed_quantity(self) -> None:
        cross = self._p("t_sym", "guard", expr="maxabs(final.s - comparand.final.s)")
        v = pe.quantity_violations(self._T, [self._p("t_mass", "mass"), self._p("t_mass", "overall"),
                                             cross], STATE)
        self.assertEqual(len(v), 1, v)
        self.assertIn("['guard']", v[0])
        # and beside an own predicate, or under a name of its own, it adds nothing to compare
        for extra in (cross, self._p("t_sym", "agree", expr=cross["expr"])):
            with self.subTest(quantity=extra["quantity"]):
                self.assertEqual(pe.quantity_violations(self._T, [
                    self._p("t_mass", "mass"), self._p("t_mass", "overall"),
                    self._p("t_sym", "guard"), extra], STATE), [])

    def test_every_target_case_is_read_by_some_predicate_of_the_test(self) -> None:
        t = [{"test_id": "t", "target_cases": ["a", "b"], "quantities": ["q", "r"]}]

        def one(quantity: str, case: str, expr: str = "final.s") -> dict:
            p = self._p("t", quantity, case=case, expr=expr, target_cases=["a", "b"])
            del p["per_case"]
            return p
        v = pe.quantity_violations(t, [one("q", "a"), one("r", "a")], STATE)
        self.assertEqual(len(v), 1, v)
        self.assertIn("target case(s) ['b']", v[0])
        # by `per_case`, by the other case's own scope, by at(), by a reached bind
        reach = self._p("t", "r", target_cases=["a", "b"])
        self.assertEqual(pe.quantity_violations(t, [one("q", "a"), reach], STATE), [])
        self.assertEqual(pe.quantity_violations(t, [one("q", "a"), one("r", "b")], STATE), [])
        self.assertEqual(pe.quantity_violations(
            t, [one("q", "a", "final.s - at('b').final.s"), one("r", "a")], STATE), [])
        bound = {**one("q", "a", "final.s - other"), "bind": {"other": "at('b').final.s"}}
        self.assertEqual(pe.quantity_violations(t, [bound, one("r", "a")], STATE), [])
        # not by a dead bind, a pruned term, an input or the time of the other case
        for expr, bind in (("final.s", {"dead": "at('b').final.s"}),
                           ("final.s + 0 * sum(at('b').final.s)", None),
                           ("final.s / at('b').inputs.grid.nx", None),
                           ("final.s * at('b').final.t", None)):
            with self.subTest(expr=expr):
                p = one("q", "a", expr)
                if bind:
                    p["bind"] = bind
                self.assertEqual(len(pe.quantity_violations(t, [p, one("r", "a")], STATE)), 1)
        # a cross-target predicate reads no case for this rule
        self.assertEqual(len(pe.quantity_violations(
            t, [one("q", "a"), one("r", "a"),
                self._p("t", "z", expr="final.s - comparand.final.s", target_cases=["a", "b"])],
            STATE)), 1)
        # without the state names the rule is not asked (the names-only comparison)
        self.assertEqual(pe.quantity_violations(t, [one("q", "a"), one("r", "a")]), [])

    def test_malformed_shapes_are_left_to_the_schema_gates(self) -> None:
        self.assertEqual(pe.quantity_violations("x", []), [])
        self.assertEqual(pe.quantity_violations([1, {"test_id": 2}, {"test_id": "t"}], []), [])
        base = pe.quantity_violations(self._T, [])
        self.assertEqual(pe.quantity_violations(
            self._T, [1, {"quantity": "mass"}, {"test_id": "t_mass"},
                      {"test_id": "t_mass", "quantity": 1}]), base)


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

    def test_the_grammar_version_and_the_comparand_root_are_the_code(self):
        """Issue #324 (R4-d PR-2): the block states the module's GRAMMAR_VERSION, and it names
        the cross-target root the parser accepts, in both capture points, on the names lines."""
        text = self._DOC.read_text(encoding="utf-8")
        self.assertIn(f"GRAMMAR_VERSION {pe.GRAMMAR_VERSION} — gated", text)
        names = text[text.index("  #   names       "):text.index("  #   functions   ")]
        spelled = " / ".join(f"{pe._COMPARAND}.{p}.<var>" for p in pe.CAPTURE_POINTS)
        self.assertIn(spelled, names)

    def test_the_state_rank_rule_is_stated(self):
        """Issue #440: the schema block's `coordinates:` line defines the state's shape the
        coordinate takes (`state_rank`: the highest rank), and the V3 gate list names the
        static rank check (`expr_rank`); each read on its own line."""
        lines = self._DOC.read_text(encoding="utf-8").splitlines()
        coord = [ln for ln in lines if ln.lstrip().startswith("coordinates:")]
        self.assertEqual(len(coord), 1, coord)
        self.assertIn("the shape the highest-rank snapshot variables share", coord[0])
        self.assertIn("it pairs only with variables of that rank, never with a lower-rank "
                      "capture beside them", coord[0])
        import re
        # the worked axis example is `state_rank`'s indexing: the named token sits at that axis
        dims, token, axis = re.search(
            r"beside `\[([^\]]+)\]` the `(\w+)` axis is (\d)", coord[0]).groups()
        tokens = [d.strip() for d in dims.split(",")]
        self.assertGreater(len(tokens), 2, "the worked example is a stacked (mixed-rank) state")
        self.assertEqual(tokens.index(token), int(axis))
        self.assertEqual(pe.state_rank({"variables": [
            {"name": "U", "shape_expr": f"[{dims}]"}, {"name": "h", "shape_expr": "[nx, ny]"}]}),
            len(tokens))
        # the lower-rank remedy: the component coordinate's `count` and `length` are both the
        # case's component count, read from the inputs; the mask it states is then 1 on the
        # component it names and 0 on the two others, evaluated by the grammar over the host's
        # own cell-centre positions; `roll` moves along the component axis only
        self.assertIn("with `count` and `length` both the case's component count (an "
                      "`inputs.<path>`, so `count` is every case's extent and `c` is i + 1/2)",
                      coord[0])
        self.assertIn("write the comparison over the stacked variables", coord[0])
        self.assertIn("`count` must equal the captured extent on that axis", coord[0])
        self.assertNotIn("separate variables", coord[0])   # round 1: unfollowable for a stacked output
        poly, on, off1, off2 = re.search(
            r"on three components `(\(c - [^`]+)` is 1 on component (\d), 0 on components "
            r"(\d) and (\d)\)", coord[0]).groups()
        self.assertEqual({int(on), int(off1), int(off2)}, {0, 1, 2})
        schema = {"variables": [{"name": "U", "shape_expr": "[ncomp, nx]"}], "coordinates": [
            {"name": "c", "axis": 0, "count": "inputs.ncomp", "length": "inputs.ncomp",
             "placement": "cell_center"}]}
        c = pe.coordinate_arrays(schema, {"ncomp": 3}, (3, 2))["c"]
        env = pe.CaseEnv(case_id="a", initial=None, final={}, inputs={}, coordinates={"c": c})
        mask = pe.evaluate(pe.parse_expr(poly), env)
        self.assertEqual(mask[:, 0].tolist(), [1.0 if i == int(on) else 0.0 for i in range(3)])
        shifts = [a.strip() for a in re.search(r"`roll\(<var>, ([^)]*)\)`", coord[0])
                  .group(1).split(",")]
        self.assertEqual(len(shifts), len(tokens))   # one shift per axis of the stacked state
        self.assertEqual((shifts[0], set(shifts[1:])), ("±1", {"0"}))
        gate = [ln for ln in lines if ln.startswith("- **`io_contract.tests` and `io_contract.predicates`")]
        self.assertEqual(len(gate), 1)
        self.assertIn("every operand pair is at equal rank (a coordinate at the state's rank) "
                      "and `expr` is a scalar, inferred statically", gate[0])
        self.assertIn("(vi) Each `coordinates[]` entry's `count` names the extent", gate[0])
        self.assertIn("whatever key holds it", gate[0])
        # (vi)'s worked `fail` is one: the token at that axis is not the one `count` names
        dims, axis, token = re.search(r"beside `\[([^\]]+)\]`, `axis: (\d)` with "
                                      r"`count: inputs\.grid\.(\w+)` is a `fail`",
                                      gate[0]).groups()
        self.assertNotEqual([d.strip() for d in dims.split(",")][int(axis)], token)
        header = [ln for ln in lines if "GRAMMAR_VERSION" in ln and "gated" in ln]
        self.assertEqual(len(header), 1)
        nxt = lines[lines.index(header[0]) + 1]
        self.assertIn("by parse, name resolution and operand ranks;", nxt)

    def test_the_worked_cross_target_translation_is_a_cross_target_predicate(self):
        """The worked translation the leaf copies parses, reads the comparand AND this run's
        own state (so the gate accepts it), and is symmetric: swapping the two variants'
        roles leaves its value unchanged on any pair of states."""
        import re
        text = self._DOC.read_text(encoding="utf-8")
        at = text.index("  #   cross-target agreement")
        expr = re.search(r'expr: "([^"]+)"', text[at:at + 600]).group(1)
        refs = pe.expr_names(pe.parse_expr(expr))
        self.assertTrue(pe.predicate_reads_comparand(refs, {}))
        self.assertTrue(pe.predicate_reads_state(refs, {}, {"u"}))
        swapped = (expr.replace("comparand.final.u", "\0").replace("final.u", "comparand.final.u")
                   .replace("\0", "final.u"))
        a, b = np.array([1.0, -2.0, 3.0]), np.array([1.5, -2.0, 2.0])
        env = pe.CaseEnv(case_id="c", initial=None, final={"u": a}, inputs={}, coordinates={})
        cenv = pe.CaseEnv(case_id="c", initial=None, final={"u": b}, inputs={}, coordinates={})
        one = pe.evaluate(pe.parse_expr(expr), env, comparand_env=cenv)
        two = pe.evaluate(pe.parse_expr(swapped), env, comparand_env=cenv)
        self.assertNotEqual(swapped, expr)
        self.assertAlmostEqual(float(one), float(two))
        self.assertAlmostEqual(float(one), 1.0 / 3.0)


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


# ------------------------------------------------------------------ cross-target (issue #324)

CROSS = {"test_id": "t_mass", "quantity": "cross_target_state_agreement",
         "target_cases": ["a", "b"],
         "expr": "maxabs(final.h - comparand.final.h) / max(maxabs(comparand.final.h), 1e-14)",
         "op": "le", "value": 1.0e-12, "per_case": True}


def _comparand(run: _RunDir, target_id: str) -> pe.ComparandEvidence:
    return pe.ComparandEvidence(target_id=target_id, pipeline_ref=f"p/{target_id}",
                                run_id="run_20260928_001", run_dir=run.root,
                                evidence=pe.comparand_evidence_sha256(run.root))


class CrossTargetGrammarTest(unittest.TestCase):
    """Grammar 3's `comparand.` root: the two admitted forms, and every neighbouring spelling
    refused at parse — one row per form, so a dropped branch names itself."""

    def test_admitted_forms_are_comparand_refs(self) -> None:
        for point in ("initial", "final"):
            refs = pe.expr_names(pe.parse_expr(f"sum(comparand.{point}.h) + final.s"))
            self.assertIn(pe.NameRef("comparand", "h", point=point), refs)
            self.assertIn(pe.NameRef("capture", "s", point="final"), refs)

    def test_refused_forms(self) -> None:
        for text in ("comparand", "comparand.h", "comparand.final", "comparand.final.h.x",
                     "comparand.inputs.grid.nx", "comparand.x", "comparand.at('a').final.h",
                     "at('a').comparand.final.h", "at('a').comparand", "comparand()",
                     "sum(comparand)"):
            with self.subTest(text=text), self.assertRaises(pe.PrimaryEvidenceError):
                pe.parse_expr(text)

    def test_comparand_is_reserved_for_binds_and_coordinates(self) -> None:
        run = _RunDir()
        self.addCleanup(run.cleanup)
        run.write_state("a", _field(), _field())
        env = pe.load_case_env(run.root, _case("a"), pe.snapshot_schema(_ir(None)))
        with self.assertRaisesRegex(pe.PrimaryEvidenceError, "shadows a grammar name"):
            pe.evaluate_binds({"comparand": "final.s"}, env, {})
        coords = [{"name": "comparand", "axis": 0, "count": 8, "length": 1.0,
                   "placement": "cell_center"}]
        with self.assertRaisesRegex(pe.PrimaryEvidenceError, "not a grammar name"):
            pe.coordinate_arrays(pe.snapshot_schema(_ir(None, coordinates=coords)), {})

    def test_predicate_reads_comparand_sees_expr_and_every_bind(self) -> None:
        self.assertTrue(pe.is_cross_target_predicate(CROSS))
        self.assertFalse(pe.is_cross_target_predicate(HMIN))
        in_bind = {**HMIN, "bind": {"R": "sum(comparand.final.h)"}, "expr": "min(final.h) + R"}
        self.assertTrue(pe.is_cross_target_predicate(in_bind))
        # a bind the expression never reaches is still evaluated, so it still needs a comparand
        unreached = {**HMIN, "bind": {"R": "sum(comparand.final.h)"}}
        self.assertTrue(pe.is_cross_target_predicate(unreached))
        self.assertFalse(pe.is_cross_target_predicate({**HMIN, "expr": "comparand.nope"}))
        self.assertFalse(pe.is_cross_target_predicate("x"))
        ir = _ir([MASS, CROSS, HMIN, in_bind])
        self.assertEqual(pe.cross_target_predicates(ir), [CROSS, in_bind])
        self.assertEqual(pe.cross_target_predicates(_ir([MASS, HMIN])), [])


class ComparandEvidenceHashTest(unittest.TestCase):
    def setUp(self) -> None:
        self.run = _RunDir()
        self.addCleanup(self.run.cleanup)
        self.run.write_state("a", _field(1), _field(2))

    def test_hash_is_over_the_captures_only(self) -> None:
        h0 = pe.comparand_evidence_sha256(self.run.root)
        self.assertTrue(h0.startswith("sha256:"))
        self.assertEqual(pe.comparand_evidence_sha256(self.run.root), h0)
        # the host-authored schema file is not a capture
        (self.run.sdir / pe.SNAPSHOT_SCHEMA_FILENAME).write_text('{"x": 1}')
        self.assertEqual(pe.comparand_evidence_sha256(self.run.root), h0)
        # a byte of a final capture, of an initial capture, and a file's NAME each move it
        final = self.run.sdir / "a.json"
        final.write_text(final.read_text() + " ")
        h1 = pe.comparand_evidence_sha256(self.run.root)
        self.assertNotEqual(h1, h0)
        init = self.run.sdir / "initial" / "a.json"
        init.write_text(init.read_text() + " ")
        h2 = pe.comparand_evidence_sha256(self.run.root)
        self.assertNotEqual(h2, h1)
        final.rename(self.run.sdir / "b.json")
        self.assertNotEqual(pe.comparand_evidence_sha256(self.run.root), h2)

    def test_file_boundaries_are_framed(self) -> None:
        """Two captures `a.json` = `1` and `b.json` = `2` concatenate, unframed, to the same
        bytes as ONE capture `a.json` = `1b.json2`; the hash keeps the file sets apart."""
        other = _RunDir()
        self.addCleanup(other.cleanup)
        for run in (self.run, other):
            for path in run.sdir.rglob("*.json"):
                path.unlink()
        (self.run.sdir / "a.json").write_text("1")
        (self.run.sdir / "b.json").write_text("2")
        (other.sdir / "a.json").write_text("1b.json2")
        self.assertNotEqual(pe.comparand_evidence_sha256(self.run.root),
                            pe.comparand_evidence_sha256(other.root))

    def test_no_capture_raises(self) -> None:
        for path in (self.run.sdir / "a.json", self.run.sdir / "initial" / "a.json"):
            path.unlink()
        (self.run.sdir / pe.SNAPSHOT_SCHEMA_FILENAME).write_text("{}")
        with self.assertRaisesRegex(pe.PrimaryEvidenceError, "no state snapshot capture"):
            pe.comparand_evidence_sha256(self.run.root)
        with self.assertRaisesRegex(pe.PrimaryEvidenceError, "no state snapshot capture"):
            pe.comparand_evidence_sha256(self.run.root / "missing")


class CrossTargetEvaluationTest(unittest.TestCase):
    """`evaluate_primary_predicates(comparands=...)`: 2 comparands × 2 cases, a disagreement
    with one of them, a comparand capture that is absent, no comparand at all."""

    def setUp(self) -> None:
        self.h = {c: _field(i) for i, c in enumerate("ab")}
        self.runs: dict[str, _RunDir] = {}
        for name in ("own", "gpu", "mpi"):
            run = _RunDir()
            self.addCleanup(run.cleanup)
            for cid in "ab":
                run.write_state(cid, self.h[cid], self.h[cid])
            self.runs[name] = run

    def _eval(self, preds: list[dict], comparands: list[str]) -> list[dict]:
        return pe.evaluate_primary_predicates(
            _ir(preds), self.runs["own"].root,
            comparands=[_comparand(self.runs[t], t) for t in comparands])

    def test_two_comparands_by_two_cases(self) -> None:
        [mass, cross] = self._eval([MASS, CROSS], ["gpu", "mpi"])
        self.assertEqual(cross["kind"], "pass")
        self.assertTrue(cross["satisfied"])
        self.assertEqual([(e["comparand"], e["case"]) for e in cross["evaluated"]],
                         [("gpu", "a"), ("gpu", "b"), ("mpi", "a"), ("mpi", "b")])
        self.assertEqual([c["target_id"] for c in cross["comparands"]], ["gpu", "mpi"])
        self.assertEqual(set(cross["comparands"][0]),
                         {"target_id", "pipeline_ref", "run_id", "evidence"})
        self.assertEqual(cross["grammar_version"], pe.GRAMMAR_VERSION)
        # an ordinary predicate is evaluated once, untagged, and lists no comparand
        self.assertEqual(mass["comparands"], [])
        self.assertEqual([e["case"] for e in mass["evaluated"]], ["a", "b"])
        self.assertNotIn("comparand", mass["evaluated"][0])

    def test_one_comparand_disagreeing_fails_physics_and_the_other_is_still_evaluated(self):
        self.runs["gpu"].write_state("a", self.h["a"], self.h["a"] * (1 + 1e-9))
        [cross] = self._eval([CROSS], ["gpu", "mpi"])
        self.assertEqual(cross["kind"], "physics")
        self.assertFalse(cross["satisfied"])
        self.assertEqual([(e["comparand"], e["case"], e["satisfied"]) for e in cross["evaluated"]],
                         [("gpu", "a", False), ("mpi", "a", True), ("mpi", "b", True)])
        self.assertGreater(cross["evaluated"][0]["value"], 1e-12)

    def test_absent_comparand_capture_is_structural_and_dominates(self) -> None:
        (self.runs["mpi"].sdir / "b.json").unlink()
        self.runs["gpu"].write_state("a", self.h["a"], self.h["a"] * 2)
        for order in (["gpu", "mpi"], ["mpi", "gpu"]):
            with self.subTest(order=order):
                [cross] = self._eval([CROSS], order)
                self.assertEqual(cross["kind"], "structural")
                bad = [e for e in cross["evaluated"] if e.get("reason")]
                self.assertEqual([(e["comparand"], e["case"]) for e in bad], [("mpi", "b")])
                self.assertIn("b.json", bad[0]["error"])

    def test_a_comparand_of_another_shape_is_a_disagreement_not_a_gap(self) -> None:
        """Round 2: a variant that sized its state differently passes its own gates (a
        symbolic extent is bound to nothing), and pairing it with this run's state used to be
        a STRUCTURAL record — this run's evidence gap, repaired at this run's Generate."""
        wide = np.vstack([self.h["a"], self.h["a"][:2]])
        self.runs["gpu"].write_state("a", wide, wide)
        [cross] = self._eval([CROSS], ["gpu", "mpi"])
        self.assertEqual(cross["kind"], "physics")
        bad = cross["evaluated"][0]
        self.assertEqual((bad["comparand"], bad["reason"]), ("gpu", "comparand_shape_mismatch"))
        self.assertIn("[10, 4]", bad["error"])
        self.assertEqual([e["comparand"] for e in cross["evaluated"][1:]], ["mpi", "mpi"])
        # a structural error on another comparand still dominates, in either order
        (self.runs["mpi"].sdir / "b.json").unlink()
        for order in (["gpu", "mpi"], ["mpi", "gpu"]):
            with self.subTest(order=order):
                self.assertEqual(self._eval([CROSS], order)[0]["kind"], "structural")

    def test_absent_comparand_variable_names_the_comparand(self) -> None:
        self.runs["gpu"].write("a", initial={"h": self.h["a"].tolist(), "s": 1.0, "t": 0.0},
                               final={"s": 1.0, "t": 0.2})
        [cross] = self._eval([CROSS], ["gpu"])
        self.assertEqual(cross["kind"], "structural")
        self.assertIn("of the comparand", cross["evaluated"][0]["error"])

    def test_no_comparand(self) -> None:
        [mass, cross] = self._eval([MASS, CROSS], [])
        self.assertEqual(cross["kind"], "no_comparand")
        self.assertTrue(cross["satisfied"])
        self.assertEqual((cross["evaluated"], cross["comparands"]), ([], []))
        self.assertEqual(mass["kind"], "pass")

    def test_a_comparand_read_needs_a_bound_comparand(self) -> None:
        env = pe.load_case_env(self.runs["own"].root, _case("a"), pe.snapshot_schema(_ir(None)))
        with self.assertRaisesRegex(pe.PrimaryEvidenceError, "no comparand is bound"):
            pe.evaluate(pe.parse_expr("sum(comparand.final.h)"), env)

    def test_reading_only_the_comparand_is_refused(self) -> None:
        only = {**CROSS, "expr": "maxabs(comparand.final.h)"}
        with self.assertRaisesRegex(pe.PrimaryEvidenceError, "reads no captured state"):
            self._eval([only], ["gpu"])


class CrossTargetSchemaTest(unittest.TestCase):
    def _v(self, preds) -> list[str]:
        ir = _ir(preds)
        cases = {c["case_id"]: c for c in ir["case"]["test_case_set"]}
        return pe.validate_primary_predicate_schema(
            preds, case_ids=set(cases), test_ids=["t_mass", "t_sym"],
            schema=pe.snapshot_schema(ir), cases=cases,
            test_target_cases={p["test_id"]: p["target_cases"]
                               for p in ir["io_contract"]["tests"]})

    def test_gate(self) -> None:
        self.assertEqual(self._v([MASS, CROSS]), [])
        self.assertEqual(self._v([{**CROSS, "expr": "maxabs(final.h - comparand.initial.t)"}]),
                         [])
        rows = [
            ({**CROSS, "expr": "maxabs(comparand.final.h)"}, "reads no captured state"),
            ({**CROSS, "expr": "maxabs(final.h - comparand.final.zz)"},
             "comparand.final.zz is not a snapshot schema variable"),
            ({**CROSS, "expr": "maxabs(final.h - comparand.inputs.grid.nx)"},
             "cross-target reference is exactly"),
            ({**CROSS, "expr": "maxabs(final.h - at('b').comparand.final.h)"},
             "does not combine with at"),
            ({**CROSS, "bind": {"comparand": "final.s"}}, "shadows a grammar name"),
        ]
        for pred, fragment in rows:
            with self.subTest(fragment=fragment):
                v = self._v([pred])
                self.assertTrue(any(fragment in x for x in v), v)


class CrossTargetVerdictTest(unittest.TestCase):
    """`evaluate_verdict` over cross-target records (issue #383): the verdict evaluates no
    comparison, so every cross-target record it accepts is the `no_comparand` placeholder, and an
    evaluated one is refused. `own_verdict` is gone: the verdict IS the run's own."""

    def setUp(self) -> None:
        self.h = _field()
        self.runs: dict[str, _RunDir] = {}
        for name in ("own", "gpu", "mpi"):
            run = _RunDir()
            self.addCleanup(run.cleanup)
            for cid in "ab":
                run.write_state(cid, self.h, self.h)
            self.runs[name] = run

    def _primary(self, preds: list[dict], comparands: list[str], **kw):
        ir = _ir(preds)
        return ir, pe.evaluate_primary_predicates(
            ir, self.runs["own"].root,
            comparands=[_comparand(self.runs[t], t) for t in comparands], **kw)

    def _verdict(self, preds: list[dict], comparands: list[str]):
        ir, primary = self._primary(preds, comparands)
        return evaluate_verdict(ir["io_contract"]["tests"], primary, run_id="r", node_key="n")

    def test_the_verdict_takes_the_placeholder_and_writes_no_own_verdict(self) -> None:
        doc = self._verdict([MASS, SYM, CROSS], [])
        self.assertEqual((doc["self_verdict"], doc["failure_class"]), ("pass", "pass"))
        self.assertEqual(list(doc), ["node_key", "run_id", "self_verdict", "failure_class",
                                     "per_test"])
        t_mass = doc["per_test"][0]
        self.assertEqual([(r["quantity"], r["kind"]) for r in t_mass["basis"]["predicates"]],
                         [("mass_drift_rel", "pass"),
                          ("cross_target_state_agreement", "no_comparand")])

    def test_an_evaluated_cross_target_record_is_refused_by_the_verdict(self) -> None:
        """Every evaluated shape — agreeing, disagreeing, structural — is a comparison the
        verdict would otherwise ignore, so it raises rather than being dropped."""
        self.runs["mpi"].write_state("b", self.h, self.h * (1 + 1e-6))
        (self.runs["gpu"].sdir / "a.json").unlink()
        ir = _ir([MASS, SYM, CROSS])
        for name, kind in (("own", "pass"), ("mpi", "physics"), ("gpu", "structural")):
            with self.subTest(kind=kind):
                primary = pe.evaluate_primary_predicates(
                    ir, self.runs["own"].root, comparands=[_comparand(self.runs[name], name)])
                cross = [r for r in primary if r["quantity"] == "cross_target_state_agreement"]
                self.assertEqual(cross[0]["kind"], kind)
                with self.assertRaisesRegex(PredicateError, "evaluated cross-target"):
                    evaluate_verdict(ir["io_contract"]["tests"], primary)
        # a placeholder carrying anything evaluated is refused too, one field at a time
        ir, primary = self._primary([MASS, SYM, CROSS], [])
        placeholder = next(r for r in primary if r["kind"] == "no_comparand")
        for field, value in (("evaluated", [{"case": "a", "satisfied": True}]),
                             ("comparands", [{"target_id": "gpu"}]),
                             ("satisfied", False)):
            with self.subTest(field=field):
                bad = [dict(r) if r is not placeholder else {**r, field: value}
                       for r in primary]
                with self.assertRaisesRegex(PredicateError, "evaluated cross-target"):
                    evaluate_verdict(ir["io_contract"]["tests"], bad)

    def test_own_failure_fails_the_verdict(self) -> None:
        self.runs["own"].write_state("a", self.h, self.h * 0.5)   # loses mass in case a
        doc = self._verdict([MASS, SYM, CROSS], [])
        self.assertEqual(doc["self_verdict"], "fail")

    def test_the_reads_every_case_recheck_does_not_count_a_cross_record(self) -> None:
        # t_mass's only OWN record reads case a, and a cross placeholder reads both
        own_a = {**MASS, "per_case": None, "case": "a"}
        own_a.pop("per_case")
        cross = {**CROSS, "quantity": "mass_drift_rel"}
        _ir_doc, primary = self._primary([own_a, cross, SYM], [])
        self.assertEqual(
            [sorted(r["cases_read"]) for r in primary if r["kind"] == "no_comparand"],
            [["a", "b"]])
        with self.assertRaisesRegex(PredicateError, "values the captured state"):
            self._verdict([own_a, cross, SYM], [])

    def test_no_verdict_carries_own_verdict(self) -> None:
        doc = self._verdict([MASS, HMIN, SYM], [])
        self.assertNotIn("own_verdict", doc)
        self.assertNotIn("own_verdict", evaluate_verdict([], []))

    def test_cross_target_only_evaluates_the_cross_target_predicates_alone(self) -> None:
        """The agreement's evaluation: the cross-target predicates only, each against every
        comparand, and nothing else evaluated — a broken own predicate is not even read."""
        self.runs["mpi"].write_state("b", self.h, self.h * (1 + 1e-6))
        broken = {**MASS, "expr": "final.nosuch / initial.h"}
        _ir_doc, records = self._primary([broken, SYM, CROSS], ["gpu", "mpi"],
                                         cross_target_only=True)
        self.assertEqual([r["quantity"] for r in records], ["cross_target_state_agreement"])
        [rec] = records
        self.assertEqual(rec["kind"], "physics")
        self.assertEqual([c["target_id"] for c in rec["comparands"]], ["gpu", "mpi"])
        self.assertEqual(sorted({ev["comparand"] for ev in rec["evaluated"]}), ["gpu", "mpi"])
        self.assertEqual({ev["comparand"] for ev in rec["evaluated"] if not ev["satisfied"]},
                         {"mpi"})
        # without the keyword the broken own predicate is evaluated (and recorded structural)
        _ir_doc, everything = self._primary([broken, SYM, CROSS], ["gpu", "mpi"])
        self.assertEqual(everything[0]["kind"], "structural")


class CrossTargetCliTest(unittest.TestCase):
    def test_comparand_flag(self) -> None:
        import yaml
        runs = {}
        for name in ("own", "gpu"):
            runs[name] = _RunDir()
            self.addCleanup(runs[name].cleanup)
            for cid in "ab":
                runs[name].write_state(cid, _field(), _field())
        ir_path = runs["own"].root / "spec.ir.yaml"
        ir_path.write_text(yaml.safe_dump(_ir([MASS, CROSS])))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = pe.main(["--ir", str(ir_path), "--run", str(runs["own"].root),
                          "--comparand", f"gpu={runs['gpu'].root}"])
        self.assertEqual(rc, 0)
        cross = json.loads(out.getvalue())[1]
        self.assertEqual([c["target_id"] for c in cross["comparands"]], ["gpu"])
        self.assertEqual(cross["comparands"][0]["evidence"],
                         pe.comparand_evidence_sha256(runs["gpu"].root))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pe.main(["--ir", str(ir_path), "--run", str(runs["own"].root)]), 0)
            self.assertEqual(pe.main(["--ir", str(ir_path), "--run", str(runs["own"].root),
                                      "--comparand", "gpu"]), 2)
        runs["gpu"].write_state("a", _field(), _field() * 2)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pe.main(["--ir", str(ir_path), "--run", str(runs["own"].root),
                                      "--comparand", f"gpu={runs['gpu'].root}"]), 1)


# ------------------------------------------------------------------------- grammar 6 / drive
#
# Issue #589 PR-1a. The fixtures: the hand-converted coriolis component (one call, no loop)
# under `tools/tests/data/drive_ir/`, and three IRs built here — `_toy` (a `loop` problem
# whose loop scalars are host data), `_toyi` (a `loop` problem whose `init` writes the state
# and the loop scalars) and `_rus` (a one-call component with a rank-0 real `out`: D7).

_DRIVE_DIR = Path(__file__).resolve().parent / "data" / "drive_ir"


def _coriolis() -> dict:
    import yaml
    return yaml.safe_load((_DRIVE_DIR / "coriolis_0.2.0.spec.ir.yaml").read_text())


def _arg(name: str, intent: str | None, typ: str, rank: int = 0,
         dims: list[str] | None = None) -> dict:
    out = {"name": name, "rank": rank, "spec": {"type": typ}}
    if intent is not None:
        out["intent"] = intent
    if dims is not None:
        out["dims"] = dims
    return out


def _api(ops: dict[str, list[dict]]) -> dict:
    return {"published_operations": [{"operation_id": op} for op in ops],
            "signatures": [{"symbol": op, "signature": {"kind": "subroutine", "name": op,
                                                          "args": args}}
                           for op, args in ops.items()]}


def _evidence(variables: list[dict], *, loop: bool, coords: bool = True) -> list[dict]:
    schema = {"variables": variables, "time_variable": "t", "time_shape_expr": "scalar"}
    if coords:
        schema["coordinates"] = [{"name": "x", "axis": 0, "count": "inputs.grid.nx",
                                  "length": 1.0, "placement": "cell_center"}]
    out = [{"artifact": "state_snapshots", "required": True, "schema": schema}]
    if loop:
        arrays = [v["name"] for v in variables if v["shape_expr"] != "scalar"]
        out += [{"artifact": "state_history"},
                {"artifact": "step_series",
                 "names": ["t", "step"] + [f"{a}_{m}" for a in arrays for m in ("min", "max")]}]
    return out


def _toy_case(cid: str, nx: int) -> dict:
    return {"case_id": cid,
            "inputs": {"grid": {"nx": nx}, "time": {"t_start": 0.0, "t_end": 1.0},
                       "history": {"every": 2}},
            "initial_state": {"h": "1 + x"},
            "derived": {"n_step": 10,
                        "dt": "(inputs.time.t_end - inputs.time.t_start) / derived.n_step"}}


def _toy() -> dict:
    """A `loop` problem, host-data loop scalars: `toy__step(nx, dt, t, h inout, ok out)`."""
    step = [_arg("nx", "in", "integer"), _arg("dt", "in", "real"), _arg("t", "in", "real"),
            _arg("h", "inout", "real", 1, ["nx"]), _arg("ok", "out", "logical")]
    return {
        "meta": {"spec_id": "toy", "spec_kind": "problem"},
        "public_api": _api({"toy__step": step}),
        "drive": {"step": {"operation": "toy__step", "result": "ok",
                           "arguments": {"nx": "inputs.grid.nx", "dt": "derived.dt",
                                         "t": "time", "h": "state.h", "ok": "state.ok"}},
                  "loop": {"n_step": "derived.n_step", "dt": "derived.dt",
                           "t_start": "inputs.time.t_start", "t_end": "inputs.time.t_end"},
                  "history": {"every": "inputs.history.every"}},
        "case": {"test_case_set": [_toy_case("a", 4), _toy_case("b", 8)]},
        "io_contract": {
            "raw_requirements": {"required_evidence": _evidence(
                [{"name": "h", "shape_expr": "[nx]"}, {"name": "ok", "shape_expr": "scalar"}],
                loop=True)},
            "tests": [{"test_id": "t_run", "expected_outcome": "pass",
                       "target_cases": ["a", "b"], "quantities": ["mass", "run.n_step"]}],
            "predicates": [
                {"test_id": "t_run", "quantity": "mass", "target_cases": ["a", "b"],
                 "per_case": True, "expr": "abs(sum(final.h) - sum(initial.h))", "op": "le",
                 "value": 1e-12},
                {"test_id": "t_run", "quantity": "run.n_step", "target_cases": ["a", "b"],
                 "per_case": True, "expr": "final.step - derived.n_step", "op": "eq",
                 "value": 0}]}}


def _toyi() -> dict:
    """A `loop` problem whose `init` writes `h`, `dt`, `n_plan` and `init_ok`."""
    init = [_arg("nx", "in", "integer"), _arg("h", "out", "real", 1, ["nx"]),
            _arg("dt", "out", "real"), _arg("n_plan", "out", "integer"),
            _arg("init_ok", "out", "logical")]
    step = [_arg("nx", "in", "integer"), _arg("dt", "in", "real"), _arg("t", "in", "real"),
            _arg("h", "inout", "real", 1, ["nx"]), _arg("ok", "out", "logical")]
    ir = _toy()
    ir["meta"]["spec_id"] = "toyi"
    ir["public_api"] = _api({"toyi__init": init, "toyi__step": step})
    ir["drive"] = {
        "init": {"operation": "toyi__init", "result": "init_ok",
                 "arguments": {"nx": "inputs.grid.nx", "h": "state.h", "dt": "state.dt",
                               "n_plan": "state.n_plan", "init_ok": "state.init_ok"}},
        "step": {"operation": "toyi__step", "result": "ok",
                 "arguments": {"nx": "inputs.grid.nx", "dt": "state.dt", "t": "time",
                               "h": "state.h", "ok": "state.ok"}},
        "loop": {"n_step": "state.n_plan", "dt": "state.dt", "t_start": "inputs.time.t_start",
                 "t_end": "inputs.time.t_end"},
        "history": {"every": "inputs.history.every"}}
    for case in ir["case"]["test_case_set"]:
        del case["initial_state"], case["derived"]
    io = ir["io_contract"]
    io["raw_requirements"]["required_evidence"] = _evidence(
        [{"name": n, "shape_expr": s} for n, s in (("h", "[nx]"), ("dt", "scalar"),
                                                   ("n_plan", "scalar"), ("init_ok", "scalar"),
                                                   ("ok", "scalar"))], loop=True)
    io["tests"] = [{"test_id": "t_run", "expected_outcome": "pass", "target_cases": ["a", "b"],
                    "quantities": ["init_ok", "plan", "run.n_step"]}]
    common = {"test_id": "t_run", "target_cases": ["a", "b"], "per_case": True}
    io["predicates"] = [
        {**common, "quantity": "init_ok", "expr": "initial.init_ok", "op": "eq", "value": 1},
        {**common, "quantity": "plan", "op": "le", "value": 1e-12,
         "expr": "abs(initial.dt - 0.5 / max(initial.h)) + abs(initial.n_plan * initial.dt - 1)"},
        {**common, "quantity": "run.n_step", "expr": "final.step - initial.n_plan", "op": "eq",
         "value": 0}]
    return ir


def _rus() -> dict:
    """A one-call component with a rank-0 real `out` (`a_x`): D7's class."""
    op = [_arg("nx", "in", "integer"), _arg("q", "in", "real", 1, ["nx"]),
          _arg("F", "out", "real", 1, ["nx"]), _arg("a_x", "out", "real"),
          _arg("ok", "out", "logical")]
    common = {"test_id": "t_flux", "target_cases": ["a"], "per_case": True}
    return {
        "meta": {"spec_id": "rus", "spec_kind": "component"},
        "public_api": _api({"rus__flux": op}),
        "drive": {"step": {"operation": "rus__flux", "result": "ok",
                           "arguments": {"nx": "inputs.grid.nx", "q": "state.q",
                                         "F": "state.F", "a_x": "state.a_x",
                                         "ok": "state.ok"}}},
        "case": {"test_case_set": [{"case_id": "a", "inputs": {"grid": {"nx": 4}},
                                    "initial_state": {"q": "x", "F": -999.0, "a_x": -999.0}}]},
        "io_contract": {
            "raw_requirements": {"required_evidence": _evidence(
                [{"name": "q", "shape_expr": "[nx]"}, {"name": "F", "shape_expr": "[nx]"},
                 {"name": "a_x", "shape_expr": "scalar"}, {"name": "ok", "shape_expr": "scalar"}],
                loop=False)},
            "tests": [{"test_id": "t_flux", "expected_outcome": "pass", "target_cases": ["a"],
                       "quantities": ["ok", "flux", "speed"]}],
            "predicates": [
                {**common, "quantity": "ok", "expr": "final.ok", "op": "eq", "value": 1},
                {**common, "quantity": "flux", "expr": "maxabs(final.F - initial.q)", "op": "le",
                 "value": 1.0},
                {**common, "quantity": "speed", "op": "le", "value": 1e-12,
                 "expr": "abs(final.a_x - maxabs(final.F))"},
                # D7: the scalar read beside its pair, a DIFFERENT predicate (review R1-4)
                {**common, "quantity": "speed", "expr": "final.a_x", "op": "ge", "value": 0}]}}


class Grammar6Test(unittest.TestCase):
    """The grammar 6 roots and functions, both polarities."""

    def test_the_new_roots_parse_and_name_what_they_read(self) -> None:
        self.assertEqual(
            pe.expr_names(pe.parse_expr("history.h + history.t + series.h_min + derived.dt "
                                        "+ final.step")),
            [pe.NameRef("history", "h"), pe.NameRef("history", "t"),
             pe.NameRef("series", "h_min"), pe.NameRef("derived", "dt"),
             pe.NameRef("capture", "step", point="final")])
        for src in ("history", "series", "derived", "history.h.x", "series.a.b",
                    "at('a').history.h", "at('a').series.t", "at('a').derived.dt",
                    "comparand.history.h", "comparand.series.t", "history.h(1)"):
            with self.subTest(src=src), self.assertRaises(pe.PrimaryEvidenceError):
                pe.parse_expr(src)

    def test_the_refusals_name_the_grammar_6_roots(self) -> None:
        """The refusal text a repairing leaf reads names the grammar 6 roots: a cross-case
        `history` read says the sampled roots are read in the case being evaluated only, and an
        unadmitted root lists them among the admitted ones."""
        with self.assertRaisesRegex(
                pe.PrimaryEvidenceError,
                r"history\.\*, series\.\* and derived\.\* are read in the case being evaluated "
                r"only"):
            pe.parse_expr("at('a').history.h")
        with self.assertRaisesRegex(pe.PrimaryEvidenceError,
                                    r"roots: initial, final, inputs, history, series, derived, "
                                    r"at\('<case_id>'\), comparand\)"):
            pe.parse_expr("foo.bar")

    def test_the_sample_reductions(self) -> None:
        a = np.arange(24.0).reshape(2, 3, 4)
        env = pe.CaseEnv("c", None, {}, {}, {}, history={"h": a, "t": np.array([1.0, 2.0])},
                         state_rank=2)
        ev = lambda e: pe.evaluate(pe.parse_expr(e), env)  # noqa: E731
        np.testing.assert_array_equal(ev("smax(history.h)"), [11.0, 23.0])
        np.testing.assert_array_equal(ev("smin(history.h)"), [0.0, 12.0])
        np.testing.assert_array_equal(ev("ssum(history.h)"), [66.0, 210.0])
        np.testing.assert_array_equal(ev("smax(history.t)"), [1.0, 2.0])   # identity on rank 1
        self.assertEqual(ev("max(smax(history.h))"), 23.0)
        self.assertEqual(ev("max(history.h)"), 23.0)                       # unchanged
        with self.assertRaisesRegex(pe.PrimaryEvidenceError, r"smax\(\): the argument must be"):
            ev("smax(1.0)")

    def test_one_sample_of_a_field_reduces(self) -> None:
        """A history of one sample has a leading extent 1, which is no state axis."""
        env = pe.CaseEnv("c", None, {}, {}, {}, history={"h": np.ones((1, 3, 4))}, state_rank=2)
        self.assertEqual(pe.evaluate(pe.parse_expr("sum(history.h)"), env), 12.0)
        # Every whole-array reduction takes the sample axis as no state axis, not `sum` alone.
        self.assertEqual(pe.evaluate(pe.parse_expr("mean(history.h)"), env), 1.0)
        self.assertAlmostEqual(pe.evaluate(pe.parse_expr("norm2(history.h)"), env),
                               np.sqrt(12.0))
        self.assertEqual(pe.evaluate(pe.parse_expr("maxabs(history.h)"), env), 1.0)
        env2 = env._replace(history={"h": np.ones((2, 3, 1))})
        for fn in ("sum", "mean", "norm2", "maxabs"):
            with self.subTest(fn=fn), \
                    self.assertRaisesRegex(pe.PrimaryEvidenceError, "extent-1 axis"):
                pe.evaluate(pe.parse_expr(f"{fn}(history.h)"), env2)

    def test_a_coordinate_broadcasts_against_history_and_a_field_of_it_does_not(self) -> None:
        x = np.array([[0.5], [1.5]]) * np.ones((2, 3))
        hist = np.stack([np.ones((2, 3)), 2 * np.ones((2, 3))])
        env = pe.CaseEnv("c", None, {}, {}, {"x": x}, history={"h": hist}, state_rank=2)
        np.testing.assert_array_equal(pe.evaluate(pe.parse_expr("x * history.h"), env),
                                      x[None] * hist)
        with self.assertRaisesRegex(pe.PrimaryEvidenceError, "operands of rank 2 and 3"):
            pe.evaluate(pe.parse_expr("(x + 0) * history.h"), env)
        with self.assertRaisesRegex(pe.PrimaryEvidenceError, "operands of rank 2 and 3"):
            pe.evaluate(pe.parse_expr("c * history.h"), env, binds={"c": x})

    def test_step_is_a_capture_name_and_a_reserved_variable_name(self) -> None:
        ir = _ir([HMIN])
        schema = pe.snapshot_schema(ir)
        cases = {c["case_id"]: c for c in ir["case"]["test_case_set"]}
        gate = lambda preds, **kw: pe.validate_primary_predicate_schema(  # noqa: E731
            preds, case_ids={"a", "b"}, test_ids=["t_mass"], schema=schema, cases=cases, **kw)
        self.assertEqual(gate([{**HMIN, "expr": "min(final.h) + final.step"}]), [])
        for name in ("step", "case_id"):
            with self.subTest(name=name):
                bad = {**schema, "variables": schema["variables"] + [
                    {"name": name, "shape_expr": "scalar"}]}
                v = pe.validate_primary_predicate_schema(
                    [HMIN], case_ids={"a", "b"}, test_ids=["t_mass"], schema=bad, cases=cases)
                self.assertIn(f"{name!r} is the driver's or the harness's own key", v[0])

    def test_the_new_roots_resolve_only_where_the_ir_declares_them(self) -> None:
        ir = _toy()
        io, cases = ir["io_contract"], pe._cases_by_id(ir)
        ctx = pe.predicate_schema_context(ir)

        def gate(expr: str, **over) -> list[str]:
            pred = {**io["predicates"][0], "expr": expr}
            return pe.validate_primary_predicate_schema(
                [pred], case_ids=set(cases), test_ids=["t_run"],
                schema=pe.snapshot_schema(ir), cases=cases, **{**ctx, **over})
        for ok in ("max(history.h)", "min(series.h_min)", "max(smax(history.h)) * derived.dt",
                   "max(history.t) + sum(final.h)"):
            with self.subTest(ok=ok):
                self.assertEqual(gate(ok), [])
        # ...and a declared one is not refused a second time (the cascade 9c275119 removed)
        self.assertEqual(gate("sum(final.h) * derived.dt", case_inputs=None), [])
        refused = [
            ("max(history.q)", {}, "history.q is not a snapshot schema variable"),
            ("max(history.h)", {"history_declared": False}, "declares no state_history"),
            ("min(series.q_min)", {}, "series.q_min is not a name of the step_series entry"),
            ("min(series.h_min)", {"series_names": None}, "declares no step_series"),
            ("sum(final.h) * derived.nope", {}, "derived.nope is not a derived value"),
            # with the case evaluation failed (`case_inputs` None, refused by (c)), a read is
            # judged against the names the case declares: an undeclared one is still refused
            ("sum(final.h) * derived.nope", {"case_inputs": None}, "derived.nope is not a derived"),
            ("smax(history.h)", {}, "evaluates to an array of rank 1"),
        ]
        for expr, over, msg in refused:
            with self.subTest(expr=expr, over=over):
                v = gate(expr, **over)
                self.assertTrue(any(msg in m for m in v), v)


class StateReadsTest(unittest.TestCase):
    """What counts as a state read under grammar 6 (`state_reads`, `predicate_reads_state`)."""

    def _reads(self, expr: str, ir: dict) -> bool:
        refs, binds = pe._predicate_refs({"expr": expr})
        return pe.predicate_reads_state(refs, binds, pe.state_reads(ir))

    def test_the_classes_decide_both_capture_points(self) -> None:
        toy, toyi = _toy(), _toyi()
        # the result state `ok` is no state read (R4-3): `fixed_form` admits its flag
        self.assertEqual(pe.state_reads(toy)[:5],
                         (frozenset({"h"}), frozenset(), True,
                          frozenset({"derived.n_step"}), frozenset({"t_run"})))
        self.assertEqual(pe.state_reads(toyi)[:2],
                         (frozenset({"h", "dt", "n_plan"}), frozenset({"h", "dt", "n_plan"})))
        self.assertEqual(pe.state_reads(toyi)[3], frozenset({"initial.n_plan", "final.n_plan"}))
        # initial.<host var> is the host's own initial_state read back: no read
        self.assertFalse(self._reads("sum(initial.h)", toy))
        self.assertTrue(self._reads("sum(final.h)", toy))
        # initial.<v> init writes is the kernel's
        self.assertTrue(self._reads("sum(initial.h)", toyi))
        self.assertTrue(self._reads("initial.dt", toyi))
        # history / series of a written variable; derived, inputs, the driver's step and time
        # (and their history / series, R2-1) never
        for expr, want in (("max(history.t)", False), ("max(smax(history.h))", True),
                           ("min(series.h_min)", True),
                           ("derived.dt", False), ("inputs.grid.nx", False),
                           ("final.step", False), ("final.t", False), ("initial.step", False)):
            with self.subTest(expr=expr):
                self.assertIs(self._reads(expr, toy), want)
        # a variable nothing writes (coriolis U, an `in` state) is not read through final.
        cor = _coriolis()
        self.assertFalse(self._reads("sum(final.U)", cor))
        self.assertTrue(self._reads("sum(final.S)", cor))

    def test_passing_the_classes_changes_the_gate_s_answer(self) -> None:
        """The pre-drive default (`reads=None`) counts `initial.<host var>`; the classes do
        not — the one default this keyword keeps, pinned both ways."""
        ir = _toy()
        io, cases = ir["io_contract"], pe._cases_by_id(ir)
        pred = {**io["predicates"][0], "expr": "sum(initial.h)"}
        kw = dict(case_ids=set(cases), test_ids=["t_run"], schema=pe.snapshot_schema(ir),
                  cases=cases)
        self.assertEqual(pe.validate_primary_predicate_schema([pred], **kw), [])
        v = pe.validate_primary_predicate_schema([pred], reads=pe.state_reads(ir), **kw)
        self.assertEqual(len(v), 1)
        self.assertIn("reads no captured state the kernel produced", v[0])

    def test_the_zero_step_closure_is_exempt_only_in_its_fixed_form_on_a_loop_node(self):
        ir = _toy()
        reads = pe.state_reads(ir)
        closure = ir["io_contract"]["predicates"][1]
        self.assertTrue(pe.is_zero_step_closure(closure, reads))
        self.assertTrue(pe.is_zero_step_closure({**closure, "expr": " final.step-derived.n_step"},
                                                reads))
        for bad in ({"op": "le"}, {"value": 1}, {"value": True},
                    {"expr": "final.step - 2 * derived.n_step"},
                    {"expr": "final.step + derived.n_step"}, {"bind": {"k": "1"}},
                    # R3-1: the closure carries the FIXED quantity run.n_step, nothing else
                    {"quantity": "mass"}, {"quantity": " run.n_step"}, {"quantity": None}):
            with self.subTest(bad=bad):
                self.assertFalse(pe.is_zero_step_closure({**closure, **bad}, reads))
        self.assertFalse(pe.is_zero_step_closure(closure, reads._replace(loop=False)))
        self.assertFalse(pe.is_zero_step_closure(closure, {"h"}))
        io, cases = ir["io_contract"], pe._cases_by_id(ir)
        kw = dict(case_ids=set(cases), test_ids=["t_run"], schema=pe.snapshot_schema(ir),
                  cases=cases, **pe.predicate_schema_context(ir))
        self.assertEqual(pe.validate_primary_predicate_schema([closure], **kw), [])
        v = pe.validate_primary_predicate_schema(
            [{**closure, "expr": "final.step - derived.n_step + 0.0"}], **kw)
        self.assertTrue(any("reads no captured state" in m for m in v), v)
        # ONE closure per test is exempt: a second, in the same form and quantity, is not
        self.assertEqual(closure["quantity"], pe.ZERO_STEP_QUANTITY)
        v = pe.validate_primary_predicate_schema([closure, dict(closure)], **kw)
        self.assertEqual(len(v), 1)
        self.assertTrue(v[0].startswith("predicates[1]: reads no captured state"), v)
        # R3-1: nor is the closure's text under another quantity, which it would stand for
        v = pe.validate_primary_predicate_schema([closure, {**closure, "quantity": "mass"}], **kw)
        self.assertEqual(len(v), 1)
        self.assertTrue(v[0].startswith("predicates[1]: reads no captured state"), v)
        v = pe.validate_primary_predicate_schema([{**closure, "quantity": "mass"}], **kw)
        self.assertEqual(len(v), 1)
        self.assertTrue(v[0].startswith("predicates[0]: reads no captured state"), v)
        # R1-9: the closure of a test that is not one of the IR's `pass` tests is not exempt
        other_test = {**closure, "test_id": "t_other"}
        v = pe.validate_primary_predicate_schema(
            [closure, other_test], **{**kw, "test_ids": ["t_run", "t_other"]})
        self.assertEqual(len(v), 1, v)
        self.assertTrue(v[0].startswith("predicates[1]: reads no captured state"), v)
        reads2 = reads._replace(closure_tests=reads.closure_tests | {"t_other"})
        self.assertTrue(pe.is_zero_step_closure(other_test, reads2))

    def test_the_evaluator_exempts_one_closure_per_test_too(self) -> None:
        run = _RunDir()
        try:
            for cid, n in (("a", 4), ("b", 8)):
                h = [1.0] * n
                run.write(cid, initial={"h": h, "ok": 0.0, "t": 0.0, "step": 0},
                          final={"h": h, "ok": 1.0, "t": 1.0, "step": 10})
            ir = _toy()
            self.assertEqual([r["kind"] for r in pe.evaluate_primary_predicates(ir, run.root)],
                             ["pass", "pass"])
            ir["io_contract"]["predicates"].append(dict(ir["io_contract"]["predicates"][1]))
            with self.assertRaisesRegex(pe.PrimaryEvidenceError,
                                        r"predicates\[2\]: reads no captured state"):
                pe.evaluate_primary_predicates(ir, run.root)
            # R3-1: the closure's text under another quantity is not exempt either
            ir = _toy()
            ir["io_contract"]["predicates"][1]["quantity"] = "mass"
            with self.assertRaisesRegex(pe.PrimaryEvidenceError,
                                        r"predicates\[1\]: reads no captured state"):
                pe.evaluate_primary_predicates(ir, run.root)
        finally:
            run.cleanup()


def _toy_with_input_state() -> dict:
    """`_toy` with an array state `b` the step operation only reads (an `in` dummy)."""
    ir = _toy()
    sig = ir["public_api"]["signatures"][0]["signature"]["args"]
    sig.insert(3, _arg("b", "in", "real", 1, ["nx"]))
    ir["drive"]["step"]["arguments"]["b"] = "state.b"
    for case in ir["case"]["test_case_set"]:
        case["initial_state"]["b"] = "2 + x"
    ev = ir["io_contract"]["raw_requirements"]["required_evidence"]
    ev[0]["schema"]["variables"].append({"name": "b", "shape_expr": "[nx]"})
    ev[2]["names"] += ["b_min", "b_max"]
    return ir


class DriverCounterReadsTest(unittest.TestCase):
    """R2-1: the driver's counters `history.t`, `history.step`, `series.t` and `series.step`
    are not kernel state — neither for `is_state_read`, the schema gate, the evaluator nor
    `cases_read` — and neither are the history and the series folds of a variable nothing
    writes; those of a written variable are."""

    _COUNTERS = ("max(history.t)", "max(history.step)", "max(series.t)", "max(series.step)")
    _MSG = "predicates[0]: reads no captured state the kernel produced"

    def _pred(self, expr: str) -> dict:
        return {"test_id": "t_run", "quantity": "mass", "target_cases": ["a", "b"],
                "per_case": True, "expr": expr, "op": "ge", "value": 0}

    def test_is_state_read(self) -> None:
        ir = _toy_with_input_state()
        self.assertEqual(pe.drive_violations(ir, kind="problem"), [])
        reads = pe.state_reads(ir)
        for kind, name, want in (("history", "t", False), ("history", "step", False),
                                 ("series", "t", False), ("series", "step", False),
                                 ("history", "h", True), ("series", "h_min", True),
                                 ("series", "h_max", True), ("history", "b", False),
                                 ("series", "b_min", False), ("series", "b_max", False),
                                 ("history", "ok", False),   # a result (R4-3)
                                 ("series", "h_mean", False)):
            with self.subTest(ref=f"{kind}.{name}"):
                self.assertIs(pe.is_state_read(pe.NameRef(kind, name), reads), want)
        # a plain set (the pre-drive rule) follows the same naming
        self.assertFalse(pe.is_state_read(pe.NameRef("series", "step"), {"h"}))
        self.assertTrue(pe.is_state_read(pe.NameRef("series", "h_min"), {"h"}))

    def test_the_schema_gate_refuses_each_counter_alone(self) -> None:
        ir = _toy_with_input_state()
        io, cases = ir["io_contract"], pe._cases_by_id(ir)
        kw = dict(case_ids=set(cases), test_ids=["t_run"], schema=pe.snapshot_schema(ir),
                  cases=cases, **pe.predicate_schema_context(ir))
        for expr in self._COUNTERS + ("max(smax(history.b))", "min(series.b_min)"):
            with self.subTest(expr=expr):
                v = pe.validate_primary_predicate_schema([self._pred(expr)], **kw)
                self.assertEqual(len(v), 1, v)
                self.assertTrue(v[0].startswith(self._MSG), v)
                self.assertIn("history.t / history.step / series.t / series.step", v[0])
                self.assertEqual(pe.cases_read(self._pred(expr), kw["reads"]), set())
        for expr in ("max(smax(history.h))", "min(series.h_min)"):
            with self.subTest(expr=expr):
                self.assertEqual(pe.validate_primary_predicate_schema(
                    [self._pred(expr)], **kw), [])
                self.assertEqual(pe.cases_read(self._pred(expr), kw["reads"]), {"a", "b"})

    def test_the_evaluator_refuses_each_counter_alone(self) -> None:
        run = _RunDir()
        try:
            for expr in self._COUNTERS:
                with self.subTest(expr=expr):
                    ir = _toy_with_input_state()
                    ir["io_contract"]["predicates"] = [self._pred(expr)]
                    with self.assertRaisesRegex(
                            pe.PrimaryEvidenceError,
                            r"predicates\[0\]: reads no captured state the kernel produced"):
                        pe.evaluate_primary_predicates(ir, run.root)
        finally:
            run.cleanup()

    def test_the_swe_repro_is_refused(self) -> None:
        """The finding's repro: a test's `extrema.h.min` predicates replaced by
        `max(series.step) ge 0` passed every gate; the schema gate now refuses each, and the
        test's cases are valued by its other predicates only."""
        import yaml
        ir = yaml.safe_load((_DRIVE_DIR / "shallow_water2d_0.4.7/spec.ir.yaml").read_text())
        io, cases = ir["io_contract"], pe._cases_by_id(ir)
        hits = [i for i, p in enumerate(io["predicates"]) if p["quantity"] == "extrema.h.min"]
        self.assertTrue(hits)
        for i in hits:
            io["predicates"][i] = {**io["predicates"][i], "expr": "max(series.step)",
                                   "op": "ge", "value": 0}
        self.assertEqual(pe.drive_violations(ir, kind="problem"), [])
        v = pe.validate_primary_predicate_schema(
            io["predicates"], case_ids=set(cases), test_ids=[t["test_id"] for t in io["tests"]],
            schema=pe.snapshot_schema(ir), cases=cases,
            test_target_cases={t["test_id"]: t["target_cases"] for t in io["tests"]},
            **pe.predicate_schema_context(ir))
        self.assertEqual(sorted(int(m.split("]")[0].split("[")[1]) for m in v), hits, v)
        self.assertTrue(all("reads no captured state the kernel produced" in m for m in v), v)


#: The tail of gate (l)'s loop-node refusal: the fixed quantity (review round 3, R3-1).
_CLOSURE_QUANTITY_TAIL = (", under `quantity: run.n_step`, the name each `pass` test of a "
                          "`loop` node lists in its tests.md `quantities` line")


class ClosureExemptionTest(unittest.TestCase):
    """R1-9: the reads-a-capture exemption belongs to the predicate that IS gate (l)'s
    accepted closure — a loop node, a `pass` test, the ref the counterpart of `loop.n_step` —
    and both exemption sites (the schema gate, the evaluator) ask the same `state_reads`."""

    _MSG = "predicates[1]: reads no captured state"

    def _ir(self, *, outcome: str, ref: str) -> dict:
        ir = _toy()
        for case in ir["case"]["test_case_set"]:
            case["derived"]["z"] = 10
        ir["io_contract"]["tests"][0]["expected_outcome"] = outcome
        ir["io_contract"]["predicates"][1]["expr"] = f"final.step - {ref}"
        return ir

    def _schema(self, ir: dict) -> list[str]:
        io, cases = ir["io_contract"], pe._cases_by_id(ir)
        return pe.validate_primary_predicate_schema(
            io["predicates"], case_ids=set(cases), test_ids=["t_run"],
            schema=pe.snapshot_schema(ir), cases=cases, **pe.predicate_schema_context(ir))

    def _evaluate(self, ir: dict) -> list[dict]:
        run = _RunDir()
        try:
            for cid, n in (("a", 4), ("b", 8)):
                h = [1.0] * n
                run.write(cid, initial={"h": h, "ok": 0.0, "t": 0.0, "step": 0},
                          final={"h": h, "ok": 1.0, "t": 1.0, "step": 10})
            return pe.evaluate_primary_predicates(ir, run.root)
        finally:
            run.cleanup()

    def test_an_xfail_test_s_step_count_form_is_not_exempt(self) -> None:
        for ref in ("derived.z", "derived.n_step"):
            with self.subTest(ref=ref):
                ir = self._ir(outcome="xfail", ref=ref)
                self.assertEqual(pe.drive_violations(ir, kind="problem"), [])  # (l): pass only
                v = self._schema(ir)
                self.assertEqual(len(v), 1, v)
                self.assertTrue(v[0].startswith(self._MSG), v)
                with self.assertRaisesRegex(pe.PrimaryEvidenceError,
                                            r"predicates\[1\]: reads no captured state"):
                    self._evaluate(ir)

    def test_a_pass_test_s_accepted_closure_is_exempt(self) -> None:
        ir = self._ir(outcome="pass", ref="derived.n_step")
        self.assertEqual(self._schema(ir), [])
        self.assertEqual([r["kind"] for r in self._evaluate(ir)], ["pass", "pass"])

    def test_the_closure_quantity_is_run_n_step(self) -> None:
        """R3-1: the closure carries ONE fixed quantity, the name the swe fixture uses."""
        self.assertEqual(pe.ZERO_STEP_QUANTITY, "run.n_step")
        self.assertIn(f"`quantity: {pe.ZERO_STEP_QUANTITY}`", _CLOSURE_QUANTITY_TAIL)

    def test_a_pass_test_s_closure_under_another_quantity_is_not_exempt(self) -> None:
        """R3-1: the accepted closure's text under a PHYSICS quantity would value that
        quantity by the step count alone; gate (l) does not accept it, and neither the schema
        gate nor the evaluator exempts it."""
        ir = self._ir(outcome="pass", ref="derived.n_step")
        ir["io_contract"]["predicates"][1]["quantity"] = "mass"
        ir["io_contract"]["tests"][0]["quantities"] = ["mass"]
        self.assertEqual([m for m in pe.drive_violations(ir, kind="problem")
                          if m.startswith("(l)")], [
            "(l) test 't_run' (pass) carries no predicate `expr: \"final.step - <ref>\"`, "
            "`op: eq`, `value: 0`, `per_case: true` over all its target cases whose <ref> is "
            "the closure counterpart of drive.loop.n_step: for this IR 'derived.n_step' (the "
            "zero-step closure)" + _CLOSURE_QUANTITY_TAIL])
        v = self._schema(ir)
        self.assertEqual(len(v), 1, v)
        self.assertTrue(v[0].startswith(self._MSG), v)
        with self.assertRaisesRegex(pe.PrimaryEvidenceError,
                                    r"predicates\[1\]: reads no captured state"):
            self._evaluate(ir)

    def test_a_non_closure_predicate_under_run_n_step_is_not_exempt(self) -> None:
        """R3-1: the quantity alone exempts nothing — a predicate under `run.n_step` that is
        not the closure's fixed form is held to the reads-a-capture rule."""
        for expr in ("derived.n_step", "final.step", "max(series.step)",
                     "final.step - derived.n_step + 0.0"):
            with self.subTest(expr=expr):
                ir = self._ir(outcome="pass", ref="derived.n_step")
                ir["io_contract"]["predicates"].append(
                    {**ir["io_contract"]["predicates"][1], "expr": expr, "op": "ge",
                     "value": 0})
                self.assertEqual(ir["io_contract"]["predicates"][2]["quantity"],
                                 pe.ZERO_STEP_QUANTITY)
                v = self._schema(ir)
                self.assertEqual(len(v), 1, v)
                self.assertTrue(v[0].startswith("predicates[2]: reads no captured state"), v)
                with self.assertRaisesRegex(pe.PrimaryEvidenceError,
                                            r"predicates\[2\]: reads no captured state"):
                    self._evaluate(ir)

    #: (surface, the text that opens the statement, the text that closes it): every statement
    #: of gate (l)'s loop-node closure an author or a leaf reads.
    _CLOSURE_SITES = (
        ("docs/workflow/phases/phase_01_compile.md", "(l) THE ZERO-STEP CLOSURE", "; (m)"),
        ("docs/TESTS.md", "A `tests.md` whose node runs a time loop", "\n"),
        ("tools/prompt_templates/workshop_compile_generate.txt",
         "every `pass` test carries the zero-step closure", "or the deterministic gate"),
    )

    def test_each_statement_of_the_closure_names_its_quantity(self) -> None:
        """R3-1: each statement of gate (l)'s closure names the quantity the code fixes, inside
        that statement (a bounded window, its bound self-tested)."""
        root = Path(__file__).resolve().parents[2]
        token = f"`{pe.ZERO_STEP_QUANTITY}`"
        for rel, start, stop in self._CLOSURE_SITES:
            with self.subTest(surface=rel):
                text = (root / rel).read_text(encoding="utf-8")
                self.assertEqual(text.count(start), 1, start)
                head = text.index(start)
                window = text[head:text.index(stop, head)]
                self.assertLess(len(window), 2000, "the window runs past the statement")
                self.assertIn(token, window)

    #: (surface, opens, closes, the clauses that must stand inside): each statement of the
    #: no-state-term rule names the closure's quantity among what it does NOT send to
    #: `Compile fail`, and requirement 9's read rule carries the exemption TESTS.md's `history`
    #: paragraph cites.
    _NOT_SUCH_A_QUANTITY_SITES = (
        ("docs/workflow/phases/phase_01_compile.md", "Four things are NOT such a quantity",
         "An xfail test that judges such a",
         ("Nor is `run.n_step`: its entry is gate (l)'s closure",)),
        ("docs/TESTS.md", "9. A judged quantity is defined in §5", "\n",
         ("except in gate (l)'s zero-step closure, which that rule exempts under the quantity "
          "`run.n_step` only", "Nor does `run.n_step`")),
    )
    #: (surface, opens, closes, the clauses that must stand inside) — review round 4, R4-3: each
    #: statement says a result is no state read, its flag is exempt only under the result's own
    #: name, and what that asks of an author.
    _RESULT_FLAG_SITES = (
        ("docs/workflow/phases/phase_01_compile.md", "Every entry READS STATE",
         "whether the value depends on the state",
         ("a RESULT `<r>` (the state a `result:` binds; another\n    # logical `out` counts)",
          "a result's FLAG under `quantity: <r>`",
          "either eq 0 on an `xfail` test (whose cases it values); under another quantity, value "
          "or\n    # outcome it reads nothing")),
        ("docs/workflow/phases/phase_01_compile.md", "Four things are NOT such a quantity",
         "An xfail test that judges such a",
         ("Nor is a result `<r>`: its entry is the flag, exempt above.",)),
        ("docs/workflow/phases/phase_01_compile.md", "(l) THE ZERO-STEP CLOSURE", "; (m)",
         ("result's FLAG eq 1", "under its fixed quantity (`run.n_step`, the result's name), "
          "which\n  #   the test's tests.md `quantities` line lists")),
        ("docs/TESTS.md", "9. A judged quantity is defined in §5", "\n",
         ("except in its fixed flag form, which that rule exempts under the result's own name "
          "only", "So a `pass` test of a node without a time loop lists the result's name in its "
          "`quantities` line", "an `xfail` guard test judges the flag under that name, and a "
          "flag predicate under any other quantity reads nothing",
          "Nor does a result's name: its entry is that flag form.")),
        ("tools/prompt_templates/workshop_compile_generate.txt",
         "every `pass` test carries the zero-step closure", "Four things the gate cannot see",
         ("the result's own name for a result flag",
          "(the result flag eq 0 under the result's own name; a flag under any other quantity "
          "reads nothing)")),
    )

    def test_each_statement_says_a_result_flag_stands_only_under_its_name(self) -> None:
        """R4-3: the reads-state list, the not-such-a-quantity list, gate (l), TESTS.md
        requirement 9 and the compile generate template each carry the result-flag clauses, in
        a bounded window (its bound self-tested)."""
        root = Path(__file__).resolve().parents[2]
        for rel, start, stop, clauses in self._RESULT_FLAG_SITES:
            with self.subTest(surface=rel, start=start):
                text = (root / rel).read_text(encoding="utf-8")
                self.assertEqual(text.count(start), 1, start)
                head = text.index(start)
                window = text[head:text.index(stop, head)]
                self.assertLess(len(window), 6000, "the window runs past the statement")
                for clause in clauses:
                    self.assertIn(clause, window)

    def test_the_closure_is_not_a_quantity_the_statement_rule_refuses(self) -> None:
        """R4-1: phase_01's no-state-term rule and TESTS.md requirement 9 except gate (l)'s
        closure under `run.n_step`, so a leaf does not declare `Compile fail` over it."""
        root = Path(__file__).resolve().parents[2]
        for rel, start, stop, clauses in self._NOT_SUCH_A_QUANTITY_SITES:
            with self.subTest(surface=rel):
                text = (root / rel).read_text(encoding="utf-8")
                self.assertEqual(text.count(start), 1, start)
                head = text.index(start)
                window = text[head:text.index(stop, head)]
                self.assertLess(len(window), 4000, "the window runs past the statement")
                for clause in clauses:
                    self.assertIn(clause, window)
                    self.assertIn(f"`{pe.ZERO_STEP_QUANTITY}`", clause)

    def test_the_evaluator_refuses_an_entry_that_reads_no_state_past_the_gate(self) -> None:
        """R4-2: `evaluate_primary_predicates` refuses, by its OWN whole-entry message, an
        entry the schema gate would refuse and that reaches it anyway (an IR the gate never
        saw) — not only by the per-case refusal that follows it, whose message shares the
        `reads no captured state` head."""
        ir = self._ir(outcome="pass", ref="derived.n_step")
        ir["io_contract"]["predicates"].append(
            {**ir["io_contract"]["predicates"][0], "expr": "derived.n_step + inputs.grid.nx",
             "op": "ge", "value": 0})
        v = self._schema(ir)
        self.assertEqual(len(v), 1, v)
        self.assertTrue(v[0].startswith("predicates[2]: reads no captured state"), v)
        with self.assertRaisesRegex(pe.PrimaryEvidenceError, re.escape(
                "predicates[2]: reads no captured state the kernel produced (final.<written "
                "var>, initial.<var init writes>, history.<written var>, series.<written "
                "var>_min|_max) in expr or a bind expr reaches") + r"\Z"):
            self._evaluate(ir)

    def test_a_pass_test_s_closure_with_a_wrong_ref_is_not_exempt(self) -> None:
        ir = self._ir(outcome="pass", ref="derived.z")
        self.assertTrue([m for m in pe.drive_violations(ir, kind="problem")
                         if m.startswith("(l) test 't_run'")])
        v = self._schema(ir)
        self.assertEqual(len(v), 1, v)
        self.assertTrue(v[0].startswith(self._MSG), v)
        with self.assertRaisesRegex(pe.PrimaryEvidenceError,
                                    r"predicates\[1\]: reads no captured state"):
            self._evaluate(ir)


class ResultFlagFormTest(unittest.TestCase):
    """Review round 4, R4-3: a RESULT state (bound to a `result:` dummy of `step` or `init`) is
    no kernel-state read, so a flag predicate stands for no physics quantity; the four fixed
    flag forms (`fixed_form`) are exempt from the reads-a-capture rule, ONE per form per test,
    each only under the result's own name. The schema gate, the evaluator and gate (l) ask the
    one function."""

    _NO_STATE = "reads no captured state the kernel produced"

    @staticmethod
    def _schema(ir: dict) -> list[str]:
        io, cases = ir["io_contract"], pe._cases_by_id(ir)
        return pe.validate_primary_predicate_schema(
            io["predicates"], case_ids=set(cases),
            test_ids=[t["test_id"] for t in io["tests"]], schema=pe.snapshot_schema(ir),
            cases=cases,
            test_target_cases={t["test_id"]: t["target_cases"] for t in io["tests"]},
            **pe.predicate_schema_context(ir))

    def _refused_at(self, ir: dict, idx: int) -> None:
        """The schema gate refuses ``predicates[idx]`` (and only it) as reading no state, and so
        does the evaluator (the IR never reaches captures: it raises first or at that entry)."""
        v = [m for m in self._schema(ir) if self._NO_STATE in m]
        self.assertEqual([m.split(":")[0] for m in v], [f"predicates[{idx}]"], v)
        with self.assertRaisesRegex(pe.PrimaryEvidenceError,
                                    re.escape(f"predicates[{idx}]: {self._NO_STATE}")):
            pe.evaluate_primary_predicates(ir, Path("/nonexistent-run-dir"))

    @staticmethod
    def _rus_xfail(expr: str = "final.ok", value: float = 0, quantity: str = "ok") -> dict:
        rus = _rus()
        io = rus["io_contract"]
        io["tests"] = [{"test_id": "t_flux", "expected_outcome": "xfail", "target_cases": ["a"],
                        "quantities": [quantity]}]
        io["predicates"] = [{"test_id": "t_flux", "quantity": quantity, "target_cases": ["a"],
                             "per_case": True, "expr": expr, "op": "eq", "value": value}]
        return rus

    @staticmethod
    def _toyi_xfail(quantity: str = "init_ok", value: float = 0) -> dict:
        ir = _toyi()
        io = ir["io_contract"]
        io["tests"].append({"test_id": "t_bad", "expected_outcome": "xfail",
                            "target_cases": ["a", "b"], "quantities": [quantity]})
        io["predicates"].append({"test_id": "t_bad", "quantity": quantity,
                                 "target_cases": ["a", "b"], "per_case": True,
                                 "expr": "initial.init_ok", "op": "eq", "value": value})
        return ir

    # --- the reviewer's repro --------------------------------------------------------------------
    def test_the_coriolis_repro_is_refused_by_the_gate_and_the_evaluator(self) -> None:
        """`l0_zero_f_gives_zero_source_pass`'s `max_abs_s` predicate rewritten to
        `final.guard_pass eq 1.0` cleared every gate (0 findings), and with captures S == 7.5,
        guard_pass = 1 the verdict passed while the test claims max|S| == 0."""
        tid = "l0_zero_f_gives_zero_source_pass"
        ir = _coriolis()
        preds = ir["io_contract"]["predicates"]
        i = next(i for i, p in enumerate(preds)
                 if p["test_id"] == tid and p["quantity"] == "max_abs_s")
        preds[i].update(expr="final.guard_pass", op="eq", value=1.0)
        v = _all_gates(ir)
        self.assertEqual(len(v), 2, v)
        self.assertTrue(v[0].startswith(f"predicates[{i}]: {self._NO_STATE} (final.<v>"), v)
        self.assertTrue(v[1].startswith(f"test {tid!r}: no predicate values the captured state "
                                        "of target case(s) ['l0_zero_f']"), v)
        # the evaluator over the reviewer's captures: refused, so no verdict passes on them
        ir["io_contract"]["predicates"] = [p for p in preds if p["test_id"] == tid]
        ir["io_contract"]["tests"] = [t for t in ir["io_contract"]["tests"]
                                      if t["test_id"] == tid]
        j = next(k for k, p in enumerate(ir["io_contract"]["predicates"])
                 if p["quantity"] == "max_abs_s")
        run = _RunDir()
        self.addCleanup(run.cleanup)
        u = np.zeros((3, 4, 3)).tolist()
        s75 = np.full((3, 4, 3), 7.5).tolist()
        run.write("l0_zero_f", initial={"U": u, "S": s75, "guard_pass": 0.0, "t": 0.0,
                                        "step": 0},
                  final={"U": u, "S": s75, "guard_pass": 1.0, "t": 0.0, "step": 1})
        with self.assertRaisesRegex(pe.PrimaryEvidenceError,
                                    re.escape(f"predicates[{j}]: {self._NO_STATE}")):
            pe.evaluate_primary_predicates(ir, run.root)
        # the honest predicate on the same captures fails the test
        honest = _coriolis()
        honest["io_contract"]["predicates"] = [p for p in honest["io_contract"]["predicates"]
                                               if p["test_id"] == tid]
        honest["io_contract"]["tests"] = ir["io_contract"]["tests"]
        records = pe.evaluate_primary_predicates(honest, run.root)
        doc = evaluate_verdict(honest["io_contract"]["tests"], records)
        self.assertEqual(doc["per_test"][0]["status"], "fail")

    def test_a_loop_node_s_result_under_a_physics_quantity_reads_nothing(self) -> None:
        ir = _toy()
        ir["io_contract"]["predicates"][0].update(expr="final.ok", op="eq", value=1)
        self.assertIsNone(pe.fixed_form(ir["io_contract"]["predicates"][0], pe.state_reads(ir)))
        self._refused_at(ir, 0)

    # --- each form, under its name and under another ---------------------------------------------
    def test_a_one_call_pass_flag_is_exempt_under_the_result_s_name_only(self) -> None:   # (a)
        rus = _rus()
        self.assertEqual(_all_gates(rus), [])
        self.assertEqual(pe.fixed_form(rus["io_contract"]["predicates"][0], pe.state_reads(rus)),
                         pe.FORM_RESULT_PASS)
        rus["io_contract"]["predicates"][0]["quantity"] = "speed"
        rus["io_contract"]["tests"][0]["quantities"] = ["flux", "speed"]
        self.assertIn("(l) test 't_flux' (pass) carries no predicate `expr: \"final.ok\"`, "
                      "`op: eq`, `value: 1`, `per_case: true` over all its target cases under "
                      "`quantity: ok`, the result's own name its tests.md `quantities` line "
                      "lists (the call's result, the zero-step closure of a one-call node)",
                      pe.drive_violations(rus, kind="component"))
        self._refused_at(rus, 0)

    def test_an_xfail_flag_is_exempt_under_the_result_s_name_only(self) -> None:   # (b)
        rus = self._rus_xfail()
        self.assertEqual(_all_gates(rus), [])
        self.assertEqual(pe.fixed_form(rus["io_contract"]["predicates"][0], pe.state_reads(rus)),
                         pe.FORM_RESULT_XFAIL)
        toy = _toy()   # any node: a loop node's xfail test too
        toy["io_contract"]["tests"].append({"test_id": "t_x", "expected_outcome": "xfail",
                                            "target_cases": ["b"], "quantities": ["ok"]})
        toy["io_contract"]["predicates"].append(
            {"test_id": "t_x", "quantity": "ok", "target_cases": ["b"], "per_case": True,
             "expr": " final.ok ", "op": "eq", "value": 0.0})
        self.assertEqual(_all_gates(toy), [])
        self._refused_at(self._rus_xfail(quantity="guard"), 0)

    def test_an_init_pass_flag_is_exempt_under_the_init_result_s_name_only(self) -> None:  # (c)
        toyi = _toyi()
        self.assertEqual(_all_gates(toyi), [])
        self.assertEqual(pe.fixed_form(toyi["io_contract"]["predicates"][0],
                                       pe.state_reads(toyi)), pe.FORM_INIT_PASS)
        toyi["io_contract"]["predicates"][0]["quantity"] = "plan"
        toyi["io_contract"]["tests"][0]["quantities"] = ["plan", "run.n_step"]
        self.assertTrue([m for m in pe.drive_violations(toyi, kind="problem")
                         if "under `quantity: init_ok`, the init result's own name" in m])
        self._refused_at(toyi, 0)

    def test_an_init_xfail_flag_is_exempt_under_the_init_result_s_name_only(self) -> None:  # (d)
        ir = self._toyi_xfail()
        self.assertEqual(_all_gates(ir), [])
        self.assertEqual(pe.fixed_form(ir["io_contract"]["predicates"][-1], pe.state_reads(ir)),
                         pe.FORM_INIT_XFAIL)
        bad = self._toyi_xfail(quantity="plan_refused")
        self._refused_at(bad, len(bad["io_contract"]["predicates"]) - 1)

    # --- what is not a fixed form ----------------------------------------------------------------
    def test_a_second_copy_of_a_form_in_one_test_is_not_exempt(self) -> None:
        rus = _rus()
        rus["io_contract"]["predicates"].append(copy.deepcopy(rus["io_contract"]["predicates"][0]))
        self._refused_at(rus, 4)
        rus = self._rus_xfail()
        rus["io_contract"]["predicates"].append(copy.deepcopy(rus["io_contract"]["predicates"][0]))
        self._refused_at(rus, 1)

    def test_the_xfail_form_on_a_pass_test_is_not_exempt(self) -> None:
        rus = _rus()
        rus["io_contract"]["predicates"][0]["value"] = 0
        self.assertIsNone(pe.fixed_form(rus["io_contract"]["predicates"][0],
                                        pe.state_reads(rus)))
        self._refused_at(rus, 0)
        # ... nor the pass form on an xfail test
        self._refused_at(self._rus_xfail(value=1), 0)
        # ... nor the init xfail form on a pass test
        toyi = _toyi()
        toyi["io_contract"]["predicates"][0]["value"] = 0
        self._refused_at(toyi, 0)

    def test_the_form_is_matched_by_its_whole_shape(self) -> None:
        """`per_case: true` over all the test's target cases, `op: eq`, no bind, the text."""
        for bad in ({"target_cases": []}, {"per_case": None, "case": "a"}, {"op": "ge"},
                    {"bind": {"z": "final.F"}}, {"expr": "final.ok + 0"},
                    {"expr": "initial.ok"}, {"quantity": " ok"}):
            with self.subTest(bad=bad):
                rus = _rus()
                rus["io_contract"]["predicates"][0].update(bad)
                self.assertIsNone(pe.fixed_form(rus["io_contract"]["predicates"][0],
                                                pe.state_reads(rus)))
        toy = _toy()
        toy["io_contract"]["tests"][0]["target_cases"] = ["a", "b"]
        flag = {"test_id": "t_run", "quantity": "ok", "target_cases": ["a"], "per_case": True,
                "expr": "final.ok", "op": "eq", "value": 0}
        toy["io_contract"]["tests"][0]["expected_outcome"] = "xfail"
        self.assertIsNone(pe.fixed_form(flag, pe.state_reads(toy)))   # not all its targets

    def test_a_list_form_flag_names_the_result_of_the_entry_running_its_cases(self) -> None:
        """On a list-form `drive.step` the form's `<r>` is the result of the entry that runs
        the test's cases: Kessler's `__terminal_velocity` entry re-bound so its result is
        `tv_ok`, a `final.guard_pass eq 1` on its test (guard_pass now host data there) is no
        fixed form and reads nothing; gate (l) asks for `final.tv_ok` under `tv_ok`."""
        ir = _kessler()
        entry = ir["drive"]["step"][1]
        ir["io_contract"]["raw_requirements"]["required_evidence"][0]["schema"][
            "variables"].append({"name": "tv_ok", "shape_expr": "scalar"})
        entry["arguments"]["guard_pass"] = "state.tv_ok"
        for case in ir["case"]["test_case_set"]:
            case["initial_state"]["guard_pass" if case["case_id"] == "tv_a" else "tv_ok"] = 0.0
        i = next(i for i, p in enumerate(ir["io_contract"]["predicates"])
                 if p["test_id"] == "t_tv" and p["expr"] == "final.guard_pass")
        reads = pe.state_reads(ir)
        self.assertEqual(reads.results_for("tv_a"), frozenset({"tv_ok"}))
        self.assertIsNone(pe.fixed_form(ir["io_contract"]["predicates"][i], reads))
        v = _all_gates(ir)
        self.assertEqual(len(v), 2, v)
        self.assertTrue(v[0].startswith("(l) test 't_tv' (pass) carries no predicate `expr: "
                                        "\"final.tv_ok\"`"), v)
        self.assertTrue(v[1].startswith(f"predicates[{i}]: {self._NO_STATE} (final.<v>"), v)

    def test_a_logical_out_that_is_no_result_still_counts(self) -> None:
        reads = pe.state_reads(_saturation())
        for name, want in (("converged", True), ("guard_pass", False)):
            with self.subTest(name=name):
                refs, binds = pe._predicate_refs({"expr": f"final.{name}"})
                self.assertIs(pe.predicate_reads_state(refs, binds, reads, ["s_a"]), want)

    def test_only_an_xfail_form_values_its_cases(self) -> None:
        """A design decision of R4-3: an `xfail` test's judgment IS the guard flag, so its form
        values the test's cases (`cases_read`, which the coverage rule and the verdict re-check
        read); a `pass` test's flag values none — it would stand in for an unread case."""
        rus = self._rus_xfail()
        reads = pe.state_reads(rus)
        self.assertEqual(pe.cases_read(rus["io_contract"]["predicates"][0], reads), {"a"})
        self.assertEqual(pe.cases_read(_rus()["io_contract"]["predicates"][0],
                                       pe.state_reads(_rus())), set())
        self.assertEqual(pe.cases_read(_toyi()["io_contract"]["predicates"][0],
                                       pe.state_reads(_toyi())), set())
        ir = self._toyi_xfail()
        self.assertEqual(pe.cases_read(ir["io_contract"]["predicates"][-1], pe.state_reads(ir)),
                         {"a", "b"})
        self.assertEqual(pe.VALUING_FORMS, {pe.FORM_RESULT_XFAIL, pe.FORM_INIT_XFAIL})
        # the coriolis guard tests are judged by the flag alone, and certify on it
        cor = _coriolis()
        self.assertEqual(_all_gates(cor), [])


class LazyLoaderTest(unittest.TestCase):
    """`history/` and `series/` are read only when a predicate names them."""

    def setUp(self) -> None:
        self.run = _RunDir()
        self.ir = _toy()
        for cid, n in (("a", 4), ("b", 8)):
            h = (1 + (np.arange(n) + 0.5) / n).tolist()
            self.run.write(cid, initial={"h": h, "ok": 0.0, "t": 0.0, "step": 0},
                           final={"h": h, "ok": 1.0, "t": 1.0, "step": 10})

    def tearDown(self) -> None:
        self.run.cleanup()

    def _eval(self, expr: str, quantity: str = "mass") -> dict:
        ir = copy.deepcopy(self.ir)
        ir["io_contract"]["predicates"] = [
            {"test_id": "t_run", "quantity": quantity, "target_cases": ["a", "b"],
             "per_case": True, "expr": expr, "op": "le", "value": 1e9},
            ir["io_contract"]["predicates"][1]]
        return pe.evaluate_primary_predicates(ir, self.run.root)

    def _series(self, cid: str, doc: dict) -> None:
        (self.run.sdir / "series").mkdir(exist_ok=True)
        (self.run.sdir / "series" / f"{cid}.json").write_text(json.dumps(doc))

    def test_nothing_is_read_when_no_predicate_names_them(self) -> None:
        recs = self._eval("sum(final.h)")
        self.assertEqual([r["kind"] for r in recs], ["pass", "pass"])
        self.assertFalse((self.run.sdir / "series").exists())

    def test_an_empty_series_loads_and_a_reduction_over_it_is_structural_alone(self) -> None:
        for cid in "ab":
            self._series(cid, {"t": [], "step": [], "h_min": [], "h_max": []})
        recs = self._eval("min(series.h_min)")
        self.assertEqual(recs[0]["kind"], "structural")
        self.assertIn("reduction over an empty array", recs[0]["evaluated"][0]["error"])
        self.assertEqual(recs[1]["kind"], "pass")        # the other predicate is unaffected
        for fn in ("sum", "mean", "max", "norm2", "maxabs"):
            with self.subTest(fn=fn):
                rec = self._eval(f"{fn}(series.h_min)")[0]
                self.assertEqual(rec["kind"], "structural")

    def test_a_non_finite_series_entry_is_refused(self) -> None:
        self._series("a", {"t": [0.1], "step": [1], "h_min": [float("nan")], "h_max": [1.0]})
        rec = self._eval("min(series.h_max)")[0]
        self.assertEqual(rec["kind"], "structural")
        self.assertIn("'h_min' holds a non-finite value", rec["evaluated"][0]["error"])

    def test_series_entries_of_unequal_length_are_refused(self) -> None:
        self._series("a", {"t": [0.1, 0.2], "h_min": [1.0]})
        self.assertIn("not of one length", self._eval("min(series.h_min) + min(series.t)")[0]["evaluated"][0]["error"])

    def test_history_stacks_samples_by_numeric_step(self) -> None:
        hdir = self.run.sdir / "history"
        hdir.mkdir()
        for cid, n in (("a", 4), ("b", 8)):
            for k in (2, 10):
                (hdir / f"{cid}__{k}.json").write_text(json.dumps(
                    {"h": [float(k)] * n, "ok": 1.0, "t": 0.1 * k, "step": k}))
        (hdir / "a__x__3.json").write_text("{}")   # not a sample of case a
        env = pe.load_case_env(self.run.root, self.ir["case"]["test_case_set"][0],
                               pe.snapshot_schema(self.ir))
        hist = env.history.get()
        np.testing.assert_array_equal(hist["step"], [2.0, 10.0])   # 10 after 2, not before
        self.assertEqual(hist["h"].shape, (2, 4))
        # the step count alone is no state read (R2-1): read beside the samples of h
        self.assertEqual(self._eval("max(history.step) + 0 * max(smax(history.h))")[0]
                         ["evaluated"][0]["value"], 10.0)

    def test_a_sample_whose_step_is_not_its_name_is_refused(self) -> None:
        hdir = self.run.sdir / "history"
        hdir.mkdir()
        (hdir / "a__2.json").write_text(json.dumps({"h": [1.0] * 4, "ok": 1.0, "t": 0.2,
                                                    "step": 3}))
        rec = self._eval("max(history.h)")[0]
        self.assertIn("carries step 3.0, not the committed step 2", rec["evaluated"][0]["error"])

    def test_no_sample_is_an_empty_history(self) -> None:
        env = pe.load_case_env(self.run.root, self.ir["case"]["test_case_set"][0],
                               pe.snapshot_schema(self.ir))
        hist = env.history.get()
        self.assertEqual(hist["h"].shape, (0, 4))
        self.assertEqual(hist["t"].shape, (0,))
        rec = self._eval("max(smax(history.h))")[0]
        self.assertEqual(rec["kind"], "structural")

    def test_derived_reads_the_host_evaluation(self) -> None:
        rec = self._eval("sum(final.h) * 0 + derived.dt + sum(final.h)")[0]
        self.assertEqual(rec["kind"], "pass")
        self.assertAlmostEqual(rec["evaluated"][0]["value"], 0.1 + sum(1 + (i + 0.5) / 4
                                                                       for i in range(4)))


class CaseInputsTest(unittest.TestCase):
    """`evaluate_case_inputs` / `write_case_inputs` against values computed by hand."""

    def test_a_1d_case_against_literal_arrays(self) -> None:
        out = pe.evaluate_case_inputs(_toy())
        self.assertEqual(out["a"], {
            "inputs.grid.nx": 4, "inputs.time.t_start": 0.0, "inputs.time.t_end": 1.0,
            "inputs.history.every": 2, "derived.n_step": 10, "derived.dt": 0.1,
            "h": [1.125, 1.375, 1.625, 1.875]})
        self.assertEqual(len(out["b"]["h"]), 8)
        self.assertIsInstance(out["a"]["inputs.grid.nx"], int)
        self.assertIsInstance(out["a"]["derived.n_step"], int)

    def test_the_coriolis_cases_against_values_computed_by_hand(self) -> None:
        out = pe.evaluate_case_inputs(_coriolis())
        zero = out["l0_zero_f"]
        self.assertEqual(set(zero), {"inputs.grid.ncomp", "inputs.grid.nx", "inputs.grid.ny",
                                     "derived.f", "U", "S"})
        nx, ny = 4, 3
        # cell centres of a unit-length axis: x = i + 1/2, so (x + 1/2) = i + 1
        u = [[[1 + 0.1 * (i + 1) + 0.05 * (j + 1) for j in range(ny)] for i in range(nx)],
             [[0.1 * (i + 1) - 0.2 * (j + 1) for j in range(ny)] for i in range(nx)],
             [[0.3 * (j + 1) - 0.05 * (i + 1) for j in range(ny)] for i in range(nx)]]
        self.assertEqual(zero["U"], u)
        self.assertEqual(zero["S"], [[[-999.0] * ny] * nx] * 3)
        self.assertEqual(zero["derived.f"], [[0.0] * ny] * nx)
        # a 2-D derived field: f(i, j) = f_0 + f_j * j + f_i * i, shape [nx, ny]
        gen = out["l0_general_f"]["derived.f"]
        self.assertEqual(gen, [[1e-4 + 2e-5 * j + 3e-6 * i for j in range(ny)]
                               for i in range(nx)])
        self.assertEqual(out["l0_ncomp2"]["inputs.grid.ncomp"], 2)
        self.assertEqual(np.shape(out["l0_ncomp4"]["U"]), (4, 4, 3))

    def test_an_infrastructure_payload_is_every_numeric_leaf(self) -> None:
        ir = {"case": {"test_case_set": [{"case_id": "s", "inputs": {
            "a": 1, "b": 2.5, "flag": True, "name": "x", "v": [1, 2], "w": [],
            "m": [[1.0, 2.0], [3.0, 4.0]], "r": [[1], [2, 3]], "mixed": [1, "x"],
            "g": {"k": 3}}}]}}
        self.assertEqual(pe.evaluate_case_inputs(ir)["s"], {
            "inputs.a": 1, "inputs.b": 2.5, "inputs.flag": 1, "inputs.v": [1, 2],
            "inputs.w": [], "inputs.m": [[1.0, 2.0], [3.0, 4.0]], "inputs.g.k": 3})
        ir["case"]["test_case_set"][0]["inputs"]["bad"] = float("inf")
        with self.assertRaisesRegex(pe.PrimaryEvidenceError, "non-finite"):
            pe.evaluate_case_inputs(ir)

    def test_the_t_end_check_both_ways(self) -> None:
        ir = _toy()
        pe.evaluate_case_inputs(ir)   # 0 + 10 * 0.1 == 1.0
        ir["case"]["test_case_set"][1]["inputs"]["time"]["t_end"] = 1.0 + 1e-9
        ir["case"]["test_case_set"][1]["derived"]["dt"] = 0.1
        with self.assertRaisesRegex(pe.PrimaryEvidenceError,
                                    r"case 'b': drive\.loop: t_start \+ n_step \* dt"):
            pe.evaluate_case_inputs(ir)
        # within 4 ulp of t_end passes
        ir["case"]["test_case_set"][1]["inputs"]["time"]["t_end"] = float(
            np.nextafter(1.0, 2.0))
        pe.evaluate_case_inputs(ir)
        # a loop scalar init writes skips the check (never fails it)
        toyi = _toyi()
        toyi["case"]["test_case_set"][0]["inputs"]["time"]["t_end"] = 123.0
        pe.evaluate_case_inputs(toyi)

    def test_each_host_refusal_names_the_case_and_the_entry(self) -> None:
        def mutate(fn) -> dict:
            ir = _toy()
            fn(ir["case"]["test_case_set"][0])
            return ir
        rows = [
            (lambda c: c["derived"].update(n_step=10.5), "drive.loop.n_step"),
            (lambda c: c["derived"].update(n_step=0, dt=0.1), "not an integer >= 1"),
            (lambda c: c["derived"].update(n_step=0), "derived.dt: evaluation failed"),
            (lambda c: c["derived"].update(dt="0 - 0.1"), "not > 0"),
            (lambda c: c["inputs"]["history"].update(every=-1), "not an integer >= 0"),
            (lambda c: c["inputs"]["history"].update(every=1.5), "not an integer >= 0"),
            (lambda c: c["initial_state"].update(h="1 / (x - x)"), "initial_state.h"),
            (lambda c: c["initial_state"].update(h=[1.0, 2.0]), "a literal of shape [2]"),
            (lambda c: c["initial_state"].update(h="final.h"), "final.h is not admitted"),
            (lambda c: c["initial_state"].pop("h"), "initial_state has no entry for ['h']"),
            (lambda c: c["initial_state"].update(ok=0), "takes no initial_state entry"),
            (lambda c: c["derived"].update(n_step="final.step"), "final.step is not admitted"),
            (lambda c: c["inputs"]["grid"].update(nx=4.5), "resolved to 4.5"),
        ]
        for fn, msg in rows:
            with self.subTest(msg=msg), self.assertRaises(pe.PrimaryEvidenceError) as cm:
                pe.evaluate_case_inputs(mutate(fn))
            self.assertIn("case 'a'", str(cm.exception))
            self.assertIn(msg, str(cm.exception))

    def test_an_integral_float_loop_count_is_admitted(self) -> None:
        ir = _toy()
        for c in ir["case"]["test_case_set"]:
            c["derived"]["n_step"] = "ceil(9.5)"
        out = pe.evaluate_case_inputs(ir)
        self.assertEqual(out["a"]["derived.n_step"], 10)
        self.assertIsInstance(out["a"]["derived.n_step"], int)

    def test_a_derived_field_fills_its_dummy_by_the_broadcast_rule(self) -> None:
        self.assertEqual(pe._fit(np.ones((1, 4, 3)), (4, 3), "w").shape, (4, 3))
        np.testing.assert_array_equal(pe._fit(np.array([[1.0], [2.0]]), (2, 3), "w"),
                                      [[1.0] * 3, [2.0] * 3])
        self.assertEqual(pe._fit(2.0, (2,), "w").tolist(), [2.0, 2.0])
        for bad in (np.ones(4), np.ones((3, 3)), np.ones((2, 2, 3))):
            with self.subTest(shape=bad.shape), self.assertRaises(pe.PrimaryEvidenceError):
                pe._fit(bad, (4, 3), "w")

    def test_write_replaces_through_a_temporary_name_and_touches_nothing_else(self) -> None:
        ir = _toy()
        with tempfile.TemporaryDirectory() as td:
            ir_dir = Path(td)
            out_dir = ir_dir / "case_inputs"
            out_dir.mkdir()
            (out_dir / "a.json").write_text("stale")
            (out_dir / "other.json").write_text("keep me")
            inode = out_dir.stat().st_ino
            replaced: list[tuple[str, str]] = []
            real_replace = pe.os.replace

            def spy(src, dst):
                replaced.append((str(src), str(dst)))
                return real_replace(src, dst)

            with unittest.mock.patch.object(pe.os, "replace", spy):
                paths = pe.write_case_inputs(ir, ir_dir)
            self.assertEqual(paths, [out_dir / "a.json", out_dir / "b.json"])
            self.assertEqual([Path(d).name for _s, d in replaced], ["a.json", "b.json"])
            for src, _d in replaced:
                self.assertEqual(Path(src).parent, out_dir)
                self.assertNotIn(Path(src).name, ("a.json", "b.json"))
                self.assertFalse(Path(src).exists())
            self.assertEqual(out_dir.stat().st_ino, inode)
            self.assertEqual((out_dir / "other.json").read_text(), "keep me")
            self.assertEqual(sorted(p.name for p in out_dir.iterdir()),
                             ["a.json", "b.json", "other.json"])
            text = (out_dir / "a.json").read_text()
            self.assertEqual(json.loads(text), pe.evaluate_case_inputs(ir)["a"])
            self.assertEqual(text, json.dumps(json.loads(text), sort_keys=True,
                                              separators=(",", ":")) + "\n")
            first = (out_dir / "b.json").read_bytes()
            pe.write_case_inputs(ir, ir_dir)
            self.assertEqual((out_dir / "b.json").read_bytes(), first)   # deterministic

    def test_write_creates_the_directory_and_leaves_nothing_on_failure(self) -> None:
        ir = _toy()
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(len(pe.write_case_inputs(ir, Path(td))), 2)
            self.assertTrue((Path(td) / "case_inputs").is_dir())
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td) / "case_inputs"
            with unittest.mock.patch.object(pe.os, "replace", side_effect=OSError("boom")), \
                    self.assertRaises(OSError):
                pe.write_case_inputs(ir, Path(td))
            self.assertEqual(list(out_dir.iterdir()), [])
        bad = _toy()
        bad["case"]["test_case_set"][0]["initial_state"]["h"] = "1 / (x - x)"
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(pe.PrimaryEvidenceError):
                pe.write_case_inputs(bad, Path(td))
            self.assertFalse((Path(td) / "case_inputs").exists())   # evaluated before writing


class DriveViolationsTest(unittest.TestCase):
    """`drive_violations` (SHAPE §6 (a)-(l)): admitted on the coriolis fixture and on the two
    loop IRs built here, and one refusal row per clause, each asserting its message."""

    def _v(self, ir: dict, kind: str | None = None) -> list[str]:
        return pe.drive_violations(ir, kind=kind or ir["meta"]["spec_kind"])

    def _refused(self, ir: dict, msg: str, kind: str | None = None) -> None:
        v = self._v(ir, kind)
        self.assertTrue(any(msg in m for m in v), v)

    def test_admitted(self) -> None:
        for name, ir in (("coriolis", _coriolis()), ("toy", _toy()), ("toyi", _toyi()),
                         ("rus", _rus())):
            with self.subTest(ir=name):
                self.assertEqual(self._v(ir), [])

    def test_the_kind(self) -> None:
        self.assertEqual(self._v({"case": {}}, "infrastructure"), [])
        self.assertEqual(self._v(_toy(), "infrastructure"),
                         ["drive: an infrastructure node carries no drive block (its own runner "
                          "runs its cases)"])
        ir = _toy()
        del ir["drive"]
        self._refused(ir, "drive is required on a problem node")
        self._refused(_coriolis(), "spec kind 'physics' is not one of", "physics")

    def test_a_problem_publishes_and_calls_its_step(self) -> None:   # (a)
        ir = _toy()
        ir["meta"]["spec_id"] = "other"
        self._refused(ir, "(a) a problem publishes other__step")
        self._refused(ir, "(a) drive.step.operation is 'toy__step'; a problem's drive.step "
                          "calls other__step")
        self.assertEqual(self._v(ir, "component"), [])   # a component's operation is any
        ir = _toyi()
        ir["public_api"]["published_operations"].append({"operation_id": "toyi__setup"})
        ir["public_api"]["signatures"][0]["symbol"] = "toyi__setup"
        ir["drive"]["init"]["operation"] = "toyi__setup"
        self._refused(ir, "(a) drive.init.operation is 'toyi__setup'; a problem's init is "
                          "toyi__init")

    def test_the_structure(self) -> None:   # (b) structural half
        rows = [
            (lambda d: d["step"]["arguments"].pop("t"), "binds no value to dummy(ies) ['t']"),
            (lambda d: d["step"]["arguments"].update(zz="time"), "names ['zz'], which are not"),
            (lambda d: d["step"]["arguments"].update(t="clock"), "binding 'clock' is not one"),
            (lambda d: d["step"].update(operation="toy__nope"), "'toy__nope' is not a published"),
            (lambda d: d.update(init=[d["step"]]), "drive.init is one mapping entry"),
            (lambda d: d["loop"].pop("t_end"), "drive.loop.t_end is required whenever loop"),
            (lambda d: d["loop"].update(dt="time"), "drive.loop.dt must be inputs.<path>"),
            (lambda d: d.update(history={"every": "derived.n_step"}),
             "drive.history must be {every: inputs.<path>}"),
            (lambda d: d.update(extra=1), "drive.extra is not a key of drive"),
        ]
        for fn, msg in rows:
            with self.subTest(msg=msg):
                ir = _toy()
                fn(ir["drive"])
                self._refused(ir, msg)

    def test_the_bindings(self) -> None:   # (b)
        def toy_with(fn):
            ir = _toy()
            fn(ir)
            return ir
        sig = lambda ir, i: ir["public_api"]["signatures"][0]["signature"]["args"][i]  # noqa
        rows = [
            (toy_with(lambda ir: ir["drive"]["step"]["arguments"].update(h="inputs.grid.nx")),
             "an inout dummy binds state.<var> only"),
            (toy_with(lambda ir: ir["drive"]["step"]["arguments"].update(ok="inputs.grid.nx")),
             "an out dummy binds state.<var> only"),
            (toy_with(lambda ir: ir["drive"]["step"]["arguments"].update(h="state.zz")),
             "state.zz is not a snapshot schema variable"),
            (toy_with(lambda ir: ir["drive"]["step"]["arguments"].update(t="step")),
             "step binds a rank-0 integer dummy"),
            (toy_with(lambda ir: ir["drive"]["step"]["arguments"].update(nx=4.5)),
             "the literal 4.5 does not bind a integer dummy"),
            (toy_with(lambda ir: ir["drive"]["step"]["arguments"].update(dt={"procedure": "x"})),
             "{procedure: ...} binds a procedure-typed dummy"),
            (toy_with(lambda ir: sig(ir, 3).update(rank=0, dims=None)),
             "a rank-0 dummy bound to state.h, whose shape_expr has rank 1"),
            (toy_with(lambda ir: sig(ir, 0).update(spec={"type": "string"})),
             "a dummy of type 'string' cannot be driven"),
            (toy_with(lambda ir: (sig(ir, 1).update(intent="out"),
                                  ir["drive"]["step"]["arguments"].update(dt="state.ok"))),
             "a rank-0 real out of the step operation on a loop node is refused"),
            (toy_with(lambda ir: ir["io_contract"]["raw_requirements"]["required_evidence"][0]
                      ["schema"]["variables"].append({"name": "spare", "shape_expr": "[nx]"})),
             "'spare' is bound by no dummy of drive"),
            (toy_with(lambda ir: ir["io_contract"]["raw_requirements"]["required_evidence"][0]
                      ["schema"]["variables"][0].update(source="operation")),
             "variables[0].source: the source vocabulary is retired"),
        ]
        for ir, msg in rows:
            with self.subTest(msg=msg):
                self._refused(ir, msg)
        # one-call node: an in/out pair binds two states; a rank-0 out binds its own name
        rus = _rus()
        rus["public_api"]["signatures"][0]["signature"]["args"][2]["intent"] = "out"
        rus["drive"]["step"]["arguments"]["F"] = "state.q"
        self._refused(rus, "the in/out pair ['q', 'F'] binds one state, state.q")
        rus = _rus()
        rus["drive"]["step"]["arguments"]["a_x"] = "state.ok"
        self._refused(rus, "a rank-0 real out binds the state named after the dummy, state.a_x")

    def test_the_shapes_per_case(self) -> None:   # (c)
        ir = _toy()
        ir["public_api"]["signatures"][0]["signature"]["args"][3]["dims"] = ["nx + 1"]
        ir["io_contract"]["raw_requirements"]["required_evidence"][0]["schema"]["coordinates"] = []
        ir["case"]["test_case_set"][0]["initial_state"]["h"] = 1.0
        ir["case"]["test_case_set"][1]["initial_state"]["h"] = 1.0
        self.assertEqual(self._v(ir), [])
        self.assertEqual(len(pe.evaluate_case_inputs(ir)["a"]["h"]), 5)
        ir["io_contract"]["raw_requirements"]["required_evidence"][0]["schema"]["variables"][0][
            "shape_expr"] = "[4]"
        self._refused(ir, "(c) case 'a': state.h: shape_expr extent 4 on axis 0 disagrees")
        ir = _toy()
        ir["public_api"]["signatures"][0]["signature"]["args"][3]["dims"] = ["dt"]
        self._refused(ir, "(c) case 'a': drive.step.arguments.h (state.h): dt is 0.1, not an "
                          "integer")
        ir = _toy()
        ir["public_api"]["signatures"][0]["signature"]["args"][3]["dims"] = ["t"]
        self._refused(ir, "dims token 't' names dummy 't', bound to time")
        ir = _toy()
        ir["io_contract"]["raw_requirements"]["required_evidence"][0]["schema"]["coordinates"][0][
            "count"] = 3
        self._refused(ir, "coordinates['x'].count 3 is not the extent of axis 0")

    def test_the_result(self) -> None:   # (d)
        ir = _toy()
        ir["drive"]["step"]["result"] = "h"
        self._refused(ir, "(d) drive.step.result 'h' is not a logical rank-0 out dummy")

    def test_the_loop_references(self) -> None:   # (e)
        ir = _toyi()
        ir["drive"]["loop"]["dt"] = "state.h"
        self._refused(ir, "(e) drive.loop.dt (state.h): a state.<var> loop reference names a "
                          "scalar init writes")
        ir = _toy()
        ir["drive"]["loop"]["n_step"] = "derived.nope"
        self._refused(ir, "(e) drive.loop.n_step (derived.nope): case 'a' has no derived.nope")
        ir = _toy()
        ir["drive"]["loop"]["t_start"] = "inputs.time.nope"
        self._refused(ir, "(e) drive.loop.t_start (inputs.time.nope): in case 'a'")
        ir = _toy()
        del ir["drive"]["history"]
        self._refused(ir, "(e) drive.history is required with loop")
        rus = _rus()
        rus["drive"]["history"] = {"every": "inputs.grid.nx"}
        self._refused(rus, "(e) drive.history is refused without loop")

    def test_the_initial_state_entries(self) -> None:   # (f)
        ir = _toy()
        del ir["case"]["test_case_set"][1]["initial_state"]["h"]
        self._refused(ir, "(f) case 'b': initial_state has no entry for ['h']")
        ir = _toy()
        ir["case"]["test_case_set"][0]["initial_state"]["zz"] = 1.0
        self._refused(ir, "(f) case 'a': initial_state.zz is not a snapshot schema variable")
        ir = _toy()
        ir["case"]["test_case_set"][0]["initial_state"]["ok"] = 0
        self._refused(ir, "(f) case 'a': initial_state.ok — a variable init writes, or a logical")
        ir = _toyi()
        ir["case"]["test_case_set"][0]["initial_state"] = {"dt": 0.1}
        self._refused(ir, "(f) case 'a': initial_state.dt — a variable init writes")
        rus = _rus()
        rus["case"]["test_case_set"][0]["initial_state"]["F"] = "0 * x"
        self._refused(rus, "(f) case 'a': initial_state.F — an output the operation only writes "
                           "takes a number literal")

    def test_the_list_form(self) -> None:   # (f')
        rus = _rus()
        rus["case"]["test_case_set"].append(
            {**copy.deepcopy(rus["case"]["test_case_set"][0]), "case_id": "b"})
        rus["io_contract"]["tests"][0]["target_cases"] = ["a", "b"]
        for p in rus["io_contract"]["predicates"]:
            p["target_cases"] = ["a", "b"]
        entry = rus["drive"]["step"]
        rus["drive"]["step"] = [{**entry, "cases": ["a"]}, {**entry, "cases": ["b"]}]
        self.assertEqual(self._v(rus), [])
        rus["drive"]["step"][1]["cases"] = ["a"]
        self._refused(rus, "(f') drive.step[].cases lists ['a'] more than once")
        self._refused(rus, "(f') drive.step[].cases leaves ['b'] to no entry")
        rus["drive"]["step"][1]["cases"] = ["b", "zz"]
        self._refused(rus, "(f') drive.step[].cases names unknown case_id(s) ['zz']")
        toy = _toy()
        toy["drive"]["step"] = [{**toy["drive"]["step"], "cases": ["a", "b"]}]
        self._refused(toy, "(f') a loop node's drive.step is one mapping entry, not a list")

    def test_the_host_expression_names(self) -> None:   # (g)
        rows = [
            ("initial_state", "h", "final.h + x", "(g) case 'a': initial_state.h: final.h is not"),
            ("initial_state", "h", "history.h", "history.h is not admitted"),
            ("initial_state", "h", "derived.dt + x", "derived.dt is not admitted"),
            ("initial_state", "h", "inputs.grid.nx + y", "name 'y' is not a coordinate"),
            ("derived", "dt", "derived.later", "derived.later is not a derived entry written "
                                               "before"),
            ("derived", "dt", "series.t_min", "series.t_min is not admitted"),
            ("derived", "dt", "at('b').x", "at('<case>') is not admitted"),
            ("derived", "dt", "comparand.initial.h", "comparand.initial.h is not admitted"),
        ]
        for block, key, text, msg in rows:
            with self.subTest(text=text):
                ir = _toy()
                ir["case"]["test_case_set"][0][block][key] = text
                self._refused(ir, msg)
        # a derived expression may read initial.<v> of a host-materialised variable ...
        ir = _toy()
        for c in ir["case"]["test_case_set"]:
            c["derived"]["h0"] = "max(initial.h)"
        self.assertEqual(self._v(ir), [])
        # ... never one init writes
        ir = _toyi()
        ir["case"]["test_case_set"][0]["derived"] = {"q": "max(initial.h)"}
        self._refused(ir, "(g) case 'a': derived.q: initial.h is not admitted")

    def test_the_history_artifacts(self) -> None:   # (h)
        ir = _toy()
        ev = ir["io_contract"]["raw_requirements"]["required_evidence"]
        ev[2]["names"] = ["t", "step", "h_min"]
        self._refused(ir, "(h) step_series.names must be exactly ['h_max', 'h_min', 'step', 't']")
        ev[2]["names"] = ["t", "step", "h_min", "h_max", "h_max"]
        self._refused(ir, "(h) step_series.names must be exactly")
        del ev[2]
        self._refused(ir, "(h) the step_series evidence entry is declared iff drive.loop is")
        del ev[1]
        self._refused(ir, "(h) the state_history evidence entry is declared iff drive.history")
        rus = _rus()
        rus["io_contract"]["raw_requirements"]["required_evidence"].append(
            {"artifact": "step_series", "names": ["t", "step"]})
        self._refused(rus, "(h) the step_series evidence entry is declared iff drive.loop is")

    def test_d7_is_clause_i(self) -> None:   # (i)
        rus = _rus()
        rus["io_contract"]["predicates"][2]["expr"] = "final.a_x"
        self._refused(rus, "(i) predicates[2] (test 't_flux', quantity 'speed') reads final.a_x")

    def test_every_init_written_variable_is_read(self) -> None:   # (k)
        ir = _toyi()
        ir["io_contract"]["predicates"][0]["expr"] = "final.ok"
        self._refused(ir, "(k) 'init_ok', which init writes, is read by no predicate over "
                          "initial.init_ok")
        ir = _toyi()
        ir["io_contract"]["predicates"][1]["expr"] = (
            "abs(initial.dt - 0.5) + abs(initial.n_plan * initial.dt - 1) + 0 * max(initial.h)")
        self._refused(ir, "(k) 'dt' sets the loop (drive.loop) and init writes it: a predicate "
                          "over initial.dt also reads initial.<a> of an array init writes (['h'])")
        self._refused(ir, "(k) 'n_plan' sets the loop")

    def test_the_zero_step_closure(self) -> None:   # (l)
        ir = _toy()
        closure = ir["io_contract"]["predicates"][1]
        for bad in ({"expr": "final.step - derived.n_step + 0"}, {"value": 1},
                    {"op": "le"}, {"target_cases": ["a"]},
                    {"per_case": None, "case": "a"},
                    {"expr": "final.step - derived.nope"},
                    {"expr": "final.step - inputs.grid.zz"},
                    {"expr": "final.step - final.h"},
                    {"expr": "final.step - inputs.grid.nx"}):
            with self.subTest(bad=bad):
                ir["io_contract"]["predicates"][1] = {**closure, **bad}
                self._refused(ir, "(l) test 't_run' (pass) carries no predicate `expr: "
                                  "\"final.step - <ref>\"`")
        for good in ("final.step - derived.n_step", "final.step-derived.n_step"):
            with self.subTest(good=good):
                ir["io_contract"]["predicates"][1] = {**closure, "expr": good}
                self.assertFalse(any(m.startswith("(l)") for m in self._v(ir)), self._v(ir))
        cor = _coriolis()
        cor["io_contract"]["predicates"][0]["value"] = 0.0
        self._refused(cor, "(l) test 'l0_zero_f_gives_zero_source_pass' (pass) carries no "
                           "predicate `expr: \"final.guard_pass\"`, `op: eq`, `value: 1`")
        cor = _coriolis()   # an xfail test needs none: its predicate reads guard_pass == 0
        self.assertEqual(cor["io_contract"]["tests"][-1]["expected_outcome"], "xfail")
        self.assertEqual(self._v(cor), [])


    def test_the_closure_ref_is_the_counterpart_of_loop_n_step(self) -> None:   # (l)
        """R1-1: the `<ref>` of `final.step - <ref>` is the one value the loop runs by —
        `drive.loop.n_step`'s binding verbatim for `derived.*` / `inputs.*`, `initial.<n>` /
        `final.<n>` for an init-written `state.<n>` — and the refusal names it for this IR.
        Another derived value, another input (one that resolves in every case), or another
        init-written scalar is refused."""
        def closure_expr(ir: dict, expr: str) -> dict:
            for pred in ir["io_contract"]["predicates"]:
                if pred["quantity"] == "run.n_step":
                    pred["expr"] = expr
            return ir

        def l_findings(ir: dict) -> list[str]:
            return [m for m in self._v(ir) if m.startswith("(l)")]

        toy = _toy()
        for case in toy["case"]["test_case_set"]:
            case["derived"]["z"] = 10
        for bad in ("final.step - derived.z", "final.step - inputs.history.every"):
            with self.subTest(bad=bad):
                self.assertEqual(l_findings(closure_expr(copy.deepcopy(toy), bad)), [
                    "(l) test 't_run' (pass) carries no predicate `expr: \"final.step - "
                    "<ref>\"`, `op: eq`, `value: 0`, `per_case: true` over all its target cases "
                    "whose <ref> is the closure counterpart of drive.loop.n_step: for this IR "
                    "'derived.n_step' (the zero-step closure)"
                    + _CLOSURE_QUANTITY_TAIL])
        self.assertEqual(l_findings(closure_expr(copy.deepcopy(toy),
                                                 "final.step - derived.n_step")), [])
        for bad in ("final.step - final.init_ok", "final.step - initial.init_ok",
                    "final.step - initial.dt", "final.step - inputs.grid.nx"):
            with self.subTest(bad=bad):
                self.assertEqual(l_findings(closure_expr(_toyi(), bad)), [
                    "(l) test 't_run' (pass) carries no predicate `expr: \"final.step - "
                    "<ref>\"`, `op: eq`, `value: 0`, `per_case: true` over all its target cases "
                    "whose <ref> is the closure counterpart of drive.loop.n_step: for this IR "
                    "'initial.n_plan' or 'final.n_plan' (the zero-step closure)"
                    + _CLOSURE_QUANTITY_TAIL])
        for good in ("final.step - initial.n_plan", "final.step - final.n_plan"):
            with self.subTest(good=good):
                self.assertEqual(l_findings(closure_expr(_toyi(), good)), [])
        # An rk4-like loop whose count is a case input.
        rk = _toy()
        rk["drive"]["loop"]["n_step"] = "inputs.time.n_step"
        for case in rk["case"]["test_case_set"]:
            case["inputs"]["time"]["n_step"] = 10
            case["derived"] = {"dt": "(inputs.time.t_end - inputs.time.t_start) "
                                     "/ inputs.time.n_step"}
        self.assertEqual(self._v(closure_expr(copy.deepcopy(rk), "final.step - inputs.time.n_step")),
                         [])
        self.assertTrue(l_findings(closure_expr(rk, "final.step - inputs.grid.nx")))

    def test_an_init_planned_loop_also_closes_the_init_result(self) -> None:   # (l)
        """R1-3: with `loop.n_step: state.n_plan` (init-written) a refused `init` planning 0
        steps satisfies `final.step - initial.n_plan` with 0 - 0, so every `pass` test also
        carries `initial.init_ok eq 1` per case over all its targets, under `quantity: init_ok`
        (review round 4, R4-3: the init result is no state read, and this fixed form is
        exempt — `fixed_form`'s `FORM_INIT_PASS`)."""
        msg = ("(l) test 't_run' (pass) carries no predicate `expr: \"initial.init_ok\"`, "
               "`op: eq`, `value: 1`, `per_case: true` over all its target cases under "
               "`quantity: init_ok`, the init result's own name its tests.md `quantities` line "
               "lists: drive.loop.n_step is a scalar init writes, so the closure counts the "
               "steps against init's own plan, which a refused init (result 0) planning 0 "
               "steps satisfies with 0 - 0; the init result closes it")
        self.assertNotIn("init_ok", pe.state_reads(_toyi()).initial)
        self.assertEqual(self._v(_toyi()), [])
        for bad in ({"value": 0}, {"op": "ge"}, {"expr": "initial.init_ok + 0"},
                    {"target_cases": ["a"]}, {"per_case": None, "case": "a"},
                    {"quantity": "init"}):
            with self.subTest(bad=bad):
                ir = _toyi()
                ir["io_contract"]["predicates"][0].update(bad)
                self.assertIn(msg, self._v(ir))
        ir = _toyi()   # init_ok still read (gate (k)) but not in the fixed form
        ir["io_contract"]["predicates"][0]["expr"] = "initial.init_ok * 1"
        self.assertIn(msg, self._v(ir))
        # A host-data count needs no init result.
        self.assertFalse([m for m in self._v(_toy()) if "initial.init_ok" in m])

    def test_the_swe_fixture_closure_is_its_loop_count(self) -> None:   # (l)
        import yaml
        swe = yaml.safe_load((_DRIVE_DIR / "shallow_water2d_0.4.7" / "spec.ir.yaml").read_text())
        self.assertEqual(swe["drive"]["loop"]["n_step"], "derived.n_step")
        self.assertFalse([m for m in self._v(swe, "problem") if m.startswith("(l)")])
        for pred in swe["io_contract"]["predicates"]:
            if pred.get("expr") == "final.step - derived.n_step":
                pred["expr"] = "final.step - inputs.history.every"
        self.assertTrue([m for m in self._v(swe, "problem") if m.startswith("(l)")])


    # --- R1-8: one row per clause a mutant survived; each is red with its clause deleted ---
    @staticmethod
    def _sig(ir: dict, op: str) -> list:
        return next(s["signature"]["args"] for s in ir["public_api"]["signatures"]
                    if s["symbol"] == op)

    @staticmethod
    def _schema_var(ir: dict, name: str, shape: str) -> None:
        ir["io_contract"]["raw_requirements"]["required_evidence"][0]["schema"][
            "variables"].append({"name": name, "shape_expr": shape})

    def test_a_host_materialised_scalar_cannot_set_the_loop(self) -> None:   # (e)
        """A `state.<var>` loop reference names a scalar `init` writes. A scalar the host
        fills (`initial_state`) as `loop.n_step` would dodge the host checks (n_step an
        integer >= 1, the t_end check), which skip a `state.*` operand, and gate (l), which
        restates nothing (e) refuses — so the IR would pass with no finding at all."""
        ir = _toy()
        self._sig(ir, "toy__step").append(_arg("nn", "in", "integer"))
        ir["drive"]["step"]["arguments"]["nn"] = "state.nn"
        self._schema_var(ir, "nn", "scalar")
        for case in ir["case"]["test_case_set"]:
            case["initial_state"]["nn"] = 0          # not >= 1, and never checked as host data
        ir["drive"]["loop"]["n_step"] = "state.nn"
        self.assertEqual(self._v(ir), ["(e) drive.loop.n_step (state.nn): a state.<var> loop "
                                       "reference names a scalar init writes"])

    def test_an_input_array_has_the_dummy_s_extents(self) -> None:   # (c)
        ir = _toy()
        self._sig(ir, "toy__step").append(_arg("w", "in", "real", 1, ["nx"]))
        ir["drive"]["step"]["arguments"]["w"] = "inputs.w"
        ir["case"]["test_case_set"][0]["inputs"]["w"] = [1.0, 2.0, 3.0]
        ir["case"]["test_case_set"][1]["inputs"]["w"] = [1.0] * 8
        self.assertEqual(self._v(ir), [
            "(c) case 'a': drive.step.arguments.w (inputs.w): an input of shape [3], the "
            "dummy's dims evaluate to [4]"])

    def test_a_derived_array_cannot_bind_a_rank_0_dummy(self) -> None:   # (c)
        ir = _toy()
        for case in ir["case"]["test_case_set"]:
            case["derived"]["arr"] = "x"
        ir["drive"]["step"]["arguments"]["dt"] = "derived.arr"
        self.assertEqual(self._v(ir), [
            f"(c) case {c!r}: drive.step.arguments.dt (derived.arr): an array bound to a rank-0 "
            "dummy" for c in ("a", "b")])

    def test_two_dummies_of_one_state_give_one_extent(self) -> None:   # (c)
        """`init`'s `h` (dims `nx + 1`) and `step`'s `h` (dims `nx`) bind one state."""
        ir = _toyi()
        self._sig(ir, "toyi__init")[1]["dims"] = ["nx + 1"]
        self.assertEqual(self._v(ir), [
            "(c) case 'a': drive.step.arguments.h (state.h): extents [4], while another dummy "
            "bound to state.h gives [5]",
            "(c) case 'b': drive.step.arguments.h (state.h): extents [8], while another dummy "
            "bound to state.h gives [9]"])

    def test_an_integer_state_takes_an_integral_initial_state(self) -> None:   # (c)
        """Without the refusal the case file would carry `int(1.5)` = 1 in every cell: the
        payload writer's `as_int` truncates, and reaches only values a guard found integral
        (an int literal, an integer-bound derived value, the host n_step, an integer
        state's initial content)."""
        ir = _toy()
        self._sig(ir, "toy__step").append(_arg("k", "in", "integer", 1, ["nx"]))
        ir["drive"]["step"]["arguments"]["k"] = "state.k"
        self._schema_var(ir, "k", "[nx]")
        ir["io_contract"]["raw_requirements"]["required_evidence"][2]["names"] += [
            "k_min", "k_max"]
        for case in ir["case"]["test_case_set"]:
            case["initial_state"]["k"] = 1.5
        self.assertEqual(self._v(ir), [
            f"(c) case {c!r}: initial_state.k: an integer is bound, and the value is not "
            "integral" for c in ("a", "b")])
        with self.assertRaisesRegex(pe.PrimaryEvidenceError, "is not integral"):
            pe.evaluate_case_inputs(ir)
        for case in ir["case"]["test_case_set"]:
            case["initial_state"]["k"] = 2.0
        self.assertEqual(pe.evaluate_case_inputs(ir)["a"]["k"], [2, 2, 2, 2])

    def test_a_state_array_has_one_reading_dummy_on_a_loop_node(self) -> None:   # (b)
        ir = _toy()
        self._sig(ir, "toy__step").append(_arg("h0", "in", "real", 1, ["nx"]))
        ir["drive"]["step"]["arguments"]["h0"] = "state.h"
        self.assertEqual(self._v(ir), [
            "drive.step: state.h is read by dummies ['h', 'h0']: a state array has at most one "
            "reading dummy per entry"])


class ScalarPairingTest(unittest.TestCase):
    """D7 (`scalar_pairing_violations`, re-checked by `evaluate_verdict`): the class is derived
    from `drive` + signatures; a scalar read alone, or beside a pair that reads only arrays,
    is refused."""

    def test_the_class(self) -> None:
        self.assertEqual(pe.drive_model(_rus()).one_call_scalars, {"a_x": frozenset({"F"})})
        self.assertEqual(pe.drive_model(_coriolis()).one_call_scalars, {})   # logical only
        self.assertEqual(pe.drive_model(_toy()).one_call_scalars, {})        # a loop node
        mutated = _rus()   # the class follows the intent: an `in` scalar is no output
        mutated["public_api"]["signatures"][0]["signature"]["args"][3]["intent"] = "in"
        self.assertEqual(pe.drive_model(mutated).one_call_scalars, {})

    def test_both_ways(self) -> None:
        rus = _rus()
        self.assertEqual(pe.scalar_pairing_violations(rus), [])   # [3] beside its pair [2]
        preds = rus["io_contract"]["predicates"]
        preds[2]["expr"] = "final.a_x"
        v = pe.scalar_pairing_violations(rus)
        self.assertEqual(len(v), 2)
        self.assertIn("predicates[2] (test 't_flux', quantity 'speed') reads final.a_x — the "
                      "scalar a rank-0 out of a one-call operation writes — in ['a']", v[0])
        self.assertIn("(['F'])", v[0])
        self.assertTrue(v[1].startswith("predicates[3] "), v)
        # a pair of the same quantity that reads only the arrays does not pair
        preds.append({**preds[2], "expr": "maxabs(final.F)"})
        self.assertEqual(len(pe.scalar_pairing_violations(rus)), 2)
        # a pair of the same quantity that reads both does
        preds.append({**preds[2], "expr": "final.a_x - maxabs(final.F)", "op": "le",
                      "value": 1.0})
        self.assertEqual(pe.scalar_pairing_violations(rus), [])
        alone = lambda: [m for m in pe.scalar_pairing_violations(rus)  # noqa: E731
                         if m.startswith("predicates[2] ")]
        # another quantity's pair does not
        preds[-1]["quantity"] = "flux"
        self.assertEqual(len(alone()), 1)
        # a pair reading the array only inside a term constant by form does not
        preds[-1].update(quantity="speed", expr="final.a_x + 0 * maxabs(final.F)")
        self.assertEqual(len(alone()), 1)
        # a pair reading an array the operation does not write does not
        preds[-1].update(expr="final.a_x - maxabs(final.q)")
        self.assertEqual(len(alone()), 1)

    def test_a_predicate_never_pairs_itself(self) -> None:
        """R1-4: the pair is a DIFFERENT predicate. A sole predicate reading the scalar and an
        array — `final.a_x + 1e-300 * sum(final.F)`, judged by `a_x` alone, the spelling the
        retired `reported_read_violations` refused, or the honest `abs(final.a_x -
        maxabs(final.F))` — is refused; a second predicate of the test and quantity reading
        `final.a_x` (alone, or itself paired) admits it, and the verdict agrees."""
        msg = ("predicates[2] (test 't_flux', quantity 'speed') reads final.a_x — the scalar a "
               "rank-0 out of a one-call operation writes — in ['a'], and no OTHER predicate of "
               "that test and quantity reads final.a_x in those cases with final.<a> of an "
               "array state the same operation writes (['F']) read beside it by one of the two "
               "(D7: a kernel-reported scalar is judged beside the state its call wrote, by a "
               "predicate other than the one reading it)")
        for expr in ("final.a_x + 1e-300 * sum(final.F)", "abs(final.a_x - maxabs(final.F))"):
            with self.subTest(expr=expr):
                rus = _rus()
                preds = rus["io_contract"]["predicates"]
                preds[2]["expr"] = expr
                del preds[3]
                self.assertEqual(pe.scalar_pairing_violations(rus), [msg])
                preds.append({**preds[2], "expr": "final.a_x", "op": "ge", "value": 0})
                self.assertEqual(pe.scalar_pairing_violations(rus), [])
                preds[3]["expr"] = "final.a_x - max(final.F)"     # two pairs pair each other
                self.assertEqual(pe.scalar_pairing_violations(rus), [])
                preds[3]["quantity"] = "flux"                     # another quantity's does not
                self.assertIn(msg, pe.scalar_pairing_violations(rus))
        run = _RunDir()
        try:
            run.write("a", initial={"q": [0.125, 0.375, 0.625, 0.875], "F": [-999.0] * 4,
                                    "a_x": -999.0, "ok": 0.0, "t": 0.0, "step": 0},
                      final={"q": [0.125, 0.375, 0.625, 0.875], "F": [0.5] * 4, "a_x": 0.5,
                             "ok": 1.0, "t": 0.0, "step": 1})
            solo = _rus()
            del solo["io_contract"]["predicates"][3]
            tests = solo["io_contract"]["tests"]
            with self.assertRaisesRegex(PredicateError, r"reads the one-call scalar final\.a_x "
                                                        r"in \['a'\] and no OTHER record"):
                evaluate_verdict(tests, pe.evaluate_primary_predicates(solo, run.root))
            self.assertEqual(evaluate_verdict(
                tests, pe.evaluate_primary_predicates(_rus(), run.root))["self_verdict"], "pass")
        finally:
            run.cleanup()

    def test_the_mutated_class_drops_the_finding(self) -> None:
        """With `a_x` an `in` (the derivation's `out` -> `in`) the alone read is no D7 case:
        this row and `test_both_ways` together go red if the derivation reads the wrong intent."""
        rus = _rus()
        rus["io_contract"]["predicates"][2]["expr"] = "final.a_x"
        rus["public_api"]["signatures"][0]["signature"]["args"][3]["intent"] = "in"
        self.assertEqual(pe.scalar_pairing_violations(rus), [])
        self.assertNotEqual(pe.scalar_pairing_violations(_rus_alone()), [])

    def test_through_real_records_and_the_verdict(self) -> None:
        run = _RunDir()
        try:
            run.write("a", initial={"q": [0.125, 0.375, 0.625, 0.875], "F": [-999.0] * 4,
                                    "a_x": -999.0, "ok": 0.0, "t": 0.0, "step": 0},
                      final={"q": [0.125, 0.375, 0.625, 0.875], "F": [0.5] * 4, "a_x": 0.5,
                             "ok": 1.0, "t": 0.0, "step": 1})
            rus = _rus()
            recs = pe.evaluate_primary_predicates(rus, run.root)
            self.assertEqual(recs[2]["scalar_pairing"],
                             {"a_x": {"scalar": ["a"], "paired": ["a"]}})
            self.assertEqual(recs[0]["scalar_pairing"], {})
            tests = rus["io_contract"]["tests"]
            self.assertEqual(evaluate_verdict(tests, recs)["self_verdict"], "pass")
            alone = pe.evaluate_primary_predicates(_rus_alone(), run.root)
            self.assertEqual(alone[2]["scalar_pairing"],
                             {"a_x": {"scalar": ["a"], "paired": []}})
            with self.assertRaisesRegex(PredicateError, "one-call scalar final.a_x"):
                evaluate_verdict(tests, alone)
            # R1-2: a cross-target predicate reading the one-call scalar alone is no D7 case at
            # the gate (`_own_predicates`) and none in the verdict: its `no_comparand`
            # placeholder takes no part in the status.
            cross = _rus()
            cross["io_contract"]["predicates"].append(
                {"test_id": "t_flux", "quantity": "agree", "target_cases": ["a"],
                 "per_case": True, "expr": "abs(final.a_x - comparand.final.a_x)", "op": "le",
                 "value": 1e-12})
            self.assertEqual(pe.scalar_pairing_violations(cross), [])
            recs = pe.evaluate_primary_predicates(cross, run.root)
            self.assertEqual((recs[4]["kind"], recs[4]["scalar_pairing"]),
                             ("no_comparand", {"a_x": {"scalar": ["a"], "paired": []}}))
            self.assertEqual(evaluate_verdict(tests, recs)["self_verdict"], "pass")
        finally:
            run.cleanup()


def _rus_alone() -> dict:
    rus = _rus()
    rus["io_contract"]["predicates"][2]["expr"] = "final.a_x"
    return rus


# Review round 2, R2-2 / R2-3: two-operation LIST-form nodes, one schema, partitioned cases —
# the shapes PR-3 converts. The dummies are the real §5.1 fences, read from the spec.

_REPO = Path(__file__).resolve().parents[2]
_MICRO = _REPO / "spec" / "component" / "microphysics"


def _fence_procedures(rel: str) -> dict[str, list[dict]]:
    """symbol -> args of the `### 5.1` yaml fence of a controlled_spec.md under microphysics."""
    import yaml
    text = (_MICRO / rel / "controlled_spec.md").read_text()
    block = text.split("### 5.1", 1)[1].split("```yaml", 1)[1].split("```", 1)[0]
    return {p["name"]: p["args"] for p in yaml.safe_load(block)["procedures"]}


_CONSTS = {"rvap": 461.46, "cpvap": 1845.6, "cl": 4218.0, "lhv0": 2.501e6, "psat0": 610.7,
           "tem00": 273.15, "eps": 1e-30, "dwatr": 1000.0}


def _list_form_ir(spec_id: str, rel: str, partition: dict[str, list[str]],
                  initial: dict[str, dict], tests: list[tuple[str, list[str], list[tuple]]],
                  arrays: list[str], scalars: list[str]) -> dict:
    """A component IR whose `drive.step` is the list form: one entry per published operation
    (``partition``: operation suffix -> its cases), every dummy bound to `state.<its name>`
    (rank >= 1 or logical) or to `inputs.grid.<name>` / `inputs.dt` / `inputs.constants.<name>`."""
    procs = _fence_procedures(rel)
    step = []
    for suffix, cids in partition.items():
        op = f"{spec_id}__{suffix}"
        arguments = {}
        for a in procs[op]:
            name = a["name"]
            if a["rank"] >= 1 or a["spec"]["type"] == "logical":
                arguments[name] = f"state.{name}"
            elif name in ("ka", "ks", "ke"):
                arguments[name] = f"inputs.grid.{name}"
            elif name == "dt":
                arguments[name] = "inputs.dt"
            else:
                arguments[name] = f"inputs.constants.{name}"
        step.append({"operation": op, "result": "guard_pass", "arguments": arguments,
                     "cases": cids})
    cases = [{"case_id": cid, "inputs": {"grid": {"ka": 8, "ks": 2, "ke": 7}, "dt": 10.0,
                                         "constants": dict(_CONSTS)},
              "initial_state": dict(initial[cid])}
             for cids in partition.values() for cid in cids]
    variables = [{"name": n, "shape_expr": "[ka]"} for n in arrays] \
        + [{"name": n, "shape_expr": "scalar"} for n in scalars]
    schema = {"variables": variables, "time_variable": "t", "time_shape_expr": "scalar",
              "coordinates": [{"name": "x", "axis": 0, "count": "inputs.grid.ka",
                               "length": "inputs.grid.ka", "placement": "cell_center"}]}
    io_tests, preds = [], []
    for tid, targets, rows in tests:
        io_tests.append({"test_id": tid, "expected_outcome": "pass", "target_cases": targets,
                         "quantities": sorted({q for q, *_ in rows})})
        preds += [{"test_id": tid, "quantity": q, "target_cases": targets, "per_case": True,
                   "expr": e, "op": o, "value": val} for q, e, o, val in rows]
    return {
        "meta": {"spec_id": spec_id, "spec_kind": "component"},
        "public_api": _api({op: procs[op] for op in (e["operation"] for e in step)}),
        "drive": {"step": step},
        "case": {"test_case_set": cases},
        "io_contract": {"raw_requirements": {"required_evidence": [
            {"artifact": "state_snapshots", "required": True, "schema": schema}]},
            "tests": io_tests, "predicates": preds}}


_KES = "microphysics_kessler_warm_rain_1d_column"
_SAT = "microphysics_saturation_liquid_1d_column"
_GUARD = ("guard_pass", "final.guard_pass", "eq", 1)


def _kessler() -> dict:
    """`__adjustment` over c_adj (adj_a, adj_b) writes temp qv qc qr cptot cvtot (inout) and
    rhoe_t evaporate pcsat (out); `__terminal_velocity` over c_tv (tv_a) writes vterm_c vterm_r.
    In c_adj the terminal-velocity arrays are bound by no entry, and in c_tv the adjustment's."""
    adj_host = {"temp": "280 + x", "dens": "1.1 - 0.05 * x", "pres": "95000 - 3000 * x",
                "qv": 0.002, "qc": 0.0, "qr": 0.0, "cptot": 1004.0, "cvtot": 717.0,
                "rhoe_t": -999.0, "evaporate": -999.0, "pcsat": -999.0}
    tv_host = {"dens": "1.1 - 0.05 * x", "rhoq_c": 1e-4, "rhoq_r": "1e-3 + 2e-4 * x",
               "refstate_dens": "1.2 - 0.08 * x", "vterm_c": -999.0, "vterm_r": -999.0}
    # each partition's host data for the arrays its entry does not bind: any form
    adj = {**adj_host, "rhoq_c": 0.0, "rhoq_r": 0.0, "refstate_dens": 1.0,
           "vterm_c": "0 * x", "vterm_r": -1.0}
    tv = {**tv_host, "temp": 280.0, "pres": 9e4, "qv": 0.0, "qc": 0.0, "qr": 0.0,
          "cptot": 1.0, "cvtot": 1.0, "rhoe_t": "0 * x", "evaporate": 0.0, "pcsat": 0.0}
    arrays = ["temp", "dens", "pres", "qv", "qc", "qr", "cptot", "cvtot", "rhoe_t",
              "evaporate", "pcsat", "rhoq_c", "rhoq_r", "refstate_dens", "vterm_c", "vterm_r"]
    return _list_form_ir(
        _KES, "kessler/microphysics_kessler_warm_rain_1d_column",
        {"adjustment": ["adj_a", "adj_b"], "terminal_velocity": ["tv_a"]},
        {"adj_a": adj, "adj_b": adj, "tv_a": tv},
        [("t_adj", ["adj_a", "adj_b"], [_GUARD, ("qv_nonneg", "min(final.qv)", "ge", 0)]),
         ("t_tv", ["tv_a"], [_GUARD, ("vterm", "min(final.vterm_r)", "ge", 0)])],
        arrays, ["guard_pass"])


def _saturation() -> dict:
    """`__dens2qsat_liq` over q_a, q_b reads temp and dens and writes qsat; `__saturation_
    adjustment` over s_a updates temp qv qc cptot cvtot, writes rhoe_d and `converged` — a
    logical result of that operation alone, so host data (-1, tests.md) on the q cases."""
    q = {"temp": "273.15 + 0 * x", "dens": "1.2 - 0.1 * x", "qsat": -999.0, "qv": 0.0,
         "qc": 0.0, "cptot": 1.0, "cvtot": 1.0, "rhoe_d": -999.0, "converged": -1.0}
    sa = {"temp": "280 + 4 * x", "dens": "1.1 - 0.05 * x", "qv": 0.002, "qc": 0.0,
          "cptot": 1004.0, "cvtot": 717.0, "rhoe_d": -999.0, "qsat": -999.0}
    return _list_form_ir(
        _SAT, "saturation/microphysics_saturation_liquid_1d_column",
        {"dens2qsat_liq": ["q_a", "q_b"], "saturation_adjustment": ["s_a"]},
        {"q_a": q, "q_b": q, "s_a": sa},
        [("t_q", ["q_a", "q_b"], [_GUARD, ("qsat_formula", "maxabs(final.qsat)", "le", 1)]),
         ("t_s", ["s_a"], [_GUARD, ("converged", "final.converged", "eq", 1),
                           ("satadj", "maxabs(final.temp - initial.temp)", "le", 1)])],
        ["temp", "dens", "qv", "qc", "cptot", "cvtot", "qsat", "rhoe_d"],
        ["guard_pass", "converged"])


def _all_gates(ir: dict) -> list[str]:
    """Every IR-side gate `ShippedFixturesTest` runs, concatenated."""
    io, cases = ir["io_contract"], pe._cases_by_id(ir)
    ctx = pe.predicate_schema_context(ir)
    return (pe.drive_violations(ir, kind=ir["meta"]["spec_kind"])
            + pe.validate_primary_predicate_schema(
                io["predicates"], case_ids=set(cases),
                test_ids=[t["test_id"] for t in io["tests"]],
                schema=pe.snapshot_schema(ir), cases=cases,
                test_target_cases={t["test_id"]: t["target_cases"] for t in io["tests"]},
                **ctx)
            + pe.quantity_violations(io["tests"], io["predicates"], ctx["reads"])
            + ([] if ctx["case_inputs"] is not None else ["case inputs did not evaluate"]))


class ListFormCasesTest(unittest.TestCase):
    """R2-2 / R2-3: on a list-form `drive.step` the variable classes, the shapes, gate (f)
    and the reads-a-capture rule are PER CASE — from the entries that run the case."""

    def test_both_microphysics_shapes_pass_every_gate(self) -> None:
        for build in (_kessler, _saturation):
            with self.subTest(fixture=build.__name__):
                self.assertEqual(_all_gates(build()), [])

    def test_the_classes_are_per_case(self) -> None:
        reads = pe.state_reads(_saturation())
        temp = pe.NameRef("capture", "temp", "final")
        self.assertFalse(pe.is_state_read(temp, reads, "q_a"))
        self.assertTrue(pe.is_state_read(temp, reads, "s_a"))
        self.assertTrue(pe.is_state_read(temp, reads))           # the node's: some entry writes
        self.assertFalse(pe.is_state_read(temp, reads, "no_such_case"))
        self.assertTrue(pe.is_state_read(pe.NameRef("capture", "temp", "final", "s_a"),
                                         reads, "q_a"))         # at('s_a') reads s_a's state
        # the result `guard_pass` is no state read (R4-3); the s_a entry's `converged`, a
        # logical out that is no `result:`, is
        self.assertEqual(reads.by_case["q_a"][0], frozenset({"qsat"}))
        self.assertIn("converged", reads.by_case["s_a"][0])
        self.assertNotIn("guard_pass", reads.by_case["s_a"][0])
        # the mapping form keeps no per-case table
        self.assertIsNone(pe.state_reads(_rus()).by_case)

    def test_final_temp_on_a_dens2qsat_case_is_no_state_read(self) -> None:
        ir = _saturation()
        preds = ir["io_contract"]["predicates"]
        i = next(i for i, p in enumerate(preds) if p["quantity"] == "qsat_formula")
        preds[i]["expr"] = "maxabs(final.temp)"        # temp is `in` on __dens2qsat_liq
        v = _all_gates(ir)
        # the pass test's guard flag values no case (R4-3), so the coverage rule names them too
        self.assertEqual(len(v), 2, v)
        self.assertTrue(v[0].startswith(
            f"predicates[{i}]: reads no captured state the kernel produced in case(s) "
            "['q_a', 'q_b'], which its scope evaluates"), v)
        self.assertTrue(v[1].startswith("test 't_q': no predicate values the captured state "
                                        "of target case(s) ['q_a', 'q_b']"), v)
        # the same read on the saturation_adjustment case is kernel state
        ir = _saturation()
        j = next(i for i, p in enumerate(ir["io_contract"]["predicates"])
                 if p["quantity"] == "satadj")
        ir["io_contract"]["predicates"][j]["expr"] = "maxabs(final.temp)"
        self.assertEqual(_all_gates(ir), [])

    def test_a_scope_over_both_partitions_reads_state_in_each(self) -> None:
        """A predicate whose read is kernel state in one partition and host data in the other
        is refused naming the case — by the schema gate and by the evaluator — and
        `cases_read` / the per-test rule count only the case it reads state in."""
        ir = _saturation()
        io = ir["io_contract"]
        io["tests"].append({"test_id": "t_mix", "expected_outcome": "xfail",
                            "target_cases": ["q_a", "s_a"], "quantities": ["temp"]})
        pred = {"test_id": "t_mix", "quantity": "temp", "target_cases": ["q_a", "s_a"],
                "per_case": True, "expr": "maxabs(final.temp)", "op": "le", "value": 1e9}
        io["predicates"].append(pred)
        idx = len(io["predicates"]) - 1
        reads = pe.state_reads(ir)
        self.assertEqual(pe.cases_read(pred, reads), {"s_a"})
        v = _all_gates(ir)
        self.assertEqual(len(v), 2, v)
        self.assertEqual(v[0], f"predicates[{idx}]: reads no captured state the kernel produced "
                         "in case(s) ['q_a'], which its scope evaluates: on a list-form "
                         "drive.step a variable is kernel state only in the cases of the "
                         "entries that write it, and elsewhere its capture is the host's "
                         "initial_state (judge those cases by a variable their own entry "
                         "writes)")
        self.assertTrue(v[1].startswith("test 't_mix': no predicate values the captured state "
                                        "of target case(s) ['q_a']"), v)
        # q_a valued by a variable its own entry writes: admitted
        pred["expr"] = "maxabs(final.temp) + maxabs(final.qsat)"
        self.assertEqual(_all_gates(ir), [])
        pred["expr"] = "maxabs(final.temp)"
        run = _RunDir()
        try:
            with self.assertRaisesRegex(
                    pe.PrimaryEvidenceError,
                    rf"predicates\[{idx}\]: reads no captured state the kernel produced in "
                    r"case\(s\) \['q_a'\], which its scope evaluates"):
                pe.evaluate_primary_predicates(ir, run.root)
        finally:
            run.cleanup()

    def test_gate_f_is_per_case(self) -> None:
        # `converged` is logical on s_a (no entry) and host data on q_a (an entry required)
        ir = _saturation()
        del ir["case"]["test_case_set"][0]["initial_state"]["converged"]
        self.assertEqual(pe.drive_violations(ir, kind="component"),
                         ["(f) case 'q_a': initial_state has no entry for ['converged']"])
        ir = _saturation()
        ir["case"]["test_case_set"][2]["initial_state"]["converged"] = -1.0
        self.assertEqual(pe.drive_violations(ir, kind="component"),
                         ["(f) case 's_a': initial_state.converged — a variable init writes, "
                          "or a logical result the driver writes, takes no initial_state "
                          "entry"])
        # vterm_r: host data on adj_a (an expression admitted, adj_a's `0 * x` vterm_c is one),
        # an output the operation only writes on tv_a (a number literal)
        ir = _kessler()
        ir["case"]["test_case_set"][2]["initial_state"]["vterm_r"] = "0 * x"
        self.assertEqual(pe.drive_violations(ir, kind="component"),
                         ["(f) case 'tv_a': initial_state.vterm_r — an output the operation "
                          "only writes takes a number literal (its pre-call content)"])
        # an unbound array on one partition still needs its entry there
        ir = _kessler()
        del ir["case"]["test_case_set"][0]["initial_state"]["vterm_r"]
        self.assertEqual(pe.drive_violations(ir, kind="component"),
                         ["(f) case 'adj_a': initial_state has no entry for ['vterm_r']"])

    def test_an_unbound_array_takes_its_extents_from_another_entry(self) -> None:
        ir = _kessler()
        payload = pe.evaluate_case_inputs(ir)
        self.assertEqual(payload["adj_a"]["vterm_r"], [-1.0] * 8)    # no entry of adj_a binds
        self.assertEqual(payload["adj_a"]["vterm_c"], [0.0] * 8)
        self.assertEqual(payload["tv_a"]["pres"], [9e4] * 8)        # no entry of tv_a binds
        self.assertEqual(len(payload["tv_a"]["rhoe_t"]), 8)
        # the extent follows the binding evaluated against THIS case's inputs
        ir["case"]["test_case_set"][0]["inputs"]["grid"]["ka"] = 5
        self.assertEqual(len(pe.evaluate_case_inputs(ir)["adj_a"]["vterm_r"]), 5)

    def test_disagreeing_or_missing_tokens_are_refused(self) -> None:
        ir = _kessler()
        tv = ir["drive"]["step"][1]["arguments"]
        tv["ka"] = "inputs.grid.ka_tv"
        for case in ir["case"]["test_case_set"]:
            case["inputs"]["grid"]["ka_tv"] = 9 if case["case_id"] != "tv_a" else 8
        self.assertEqual(pe.drive_violations(ir, kind="component"), [
            f"(c) case {cid!r}: state.{var}: shape_expr token 'ka' names 'ka', which the "
            "drive entries bind to different values in this case ({'drive.step[0]': 8, "
            "'drive.step[1]': 9})" for cid, var in (("adj_a", "rhoq_c"), ("adj_b", "rhoq_c"))])
        ir = _kessler()
        var = next(v for v in ir["io_contract"]["raw_requirements"]["required_evidence"][0][
            "schema"]["variables"] if v["name"] == "vterm_r")
        var["shape_expr"] = "[nz]"
        self.assertEqual(pe.drive_violations(ir, kind="component"), [
            f"(c) case {cid!r}: state.vterm_r: shape_expr token 'nz' names 'nz', which no "
            "drive entry binds as a rank-0 dummy, so the extent of an array no entry running "
            "this case binds is unknown" for cid in ("adj_a", "adj_b")])

    def test_d7_counts_a_scalar_only_where_its_entry_runs(self) -> None:
        """`record_scalar_pairing` with `one_call_by_case`: a one-call scalar is a kernel
        scalar only in the cases of the entry that writes it."""
        ir = _rus()
        other = [_arg("nx", "in", "integer"), _arg("q", "in", "real", 1, ["nx"]),
                 _arg("G", "out", "real", 1, ["nx"]), _arg("ok", "out", "logical")]
        ir["public_api"] = _api({"rus__flux": ir["public_api"]["signatures"][0]["signature"][
            "args"], "rus__other": other})
        ir["drive"]["step"] = [
            {**ir["drive"]["step"], "cases": ["a"]},
            {"operation": "rus__other", "result": "ok", "cases": ["b"],
             "arguments": {"nx": "inputs.grid.nx", "q": "state.q", "G": "state.G",
                           "ok": "state.ok"}}]
        ir["case"]["test_case_set"].append({**copy.deepcopy(ir["case"]["test_case_set"][0]),
                                            "case_id": "b"})
        model = pe.drive_model(ir)
        by_case = pe.one_call_by_case(ir, model)
        self.assertEqual(by_case, {"a": {"a_x": frozenset({"F"})}, "b": {}})
        pred = {"target_cases": ["a", "b"], "per_case": True,
                "expr": "final.a_x + maxabs(final.F) + maxabs(final.G)"}
        self.assertEqual(pe.record_scalar_pairing(pred, model.one_call_scalars, by_case),
                         {"a_x": {"scalar": ["a"], "paired": ["a"]}})
        self.assertEqual(pe.record_scalar_pairing(pred, model.one_call_scalars),
                         {"a_x": {"scalar": ["a", "b"], "paired": ["a", "b"]}})

    def test_d7_pairs_a_case_only_with_an_array_its_own_entry_writes(self) -> None:
        """Review round 3, R3-2: when BOTH entries write the one-call scalar `a_x` (case a's
        beside its array `F`, case b's beside `G`), `final.a_x + maxabs(final.F)` is paired in
        case a only — in case b `F` is host data, and b's entry writes `G`. Red when the per-case
        array filter of `record_scalar_pairing` admits every array of the scalar."""
        ir = _rus()
        other = [_arg("nx", "in", "integer"), _arg("q", "in", "real", 1, ["nx"]),
                 _arg("G", "out", "real", 1, ["nx"]), _arg("a_x", "out", "real"),
                 _arg("ok", "out", "logical")]
        ir["public_api"] = _api({"rus__flux": ir["public_api"]["signatures"][0]["signature"][
            "args"], "rus__other": other})
        ir["drive"]["step"] = [
            {**ir["drive"]["step"], "cases": ["a"]},
            {"operation": "rus__other", "result": "ok", "cases": ["b"],
             "arguments": {"nx": "inputs.grid.nx", "q": "state.q", "G": "state.G",
                           "a_x": "state.a_x", "ok": "state.ok"}}]
        ir["case"]["test_case_set"].append({**copy.deepcopy(ir["case"]["test_case_set"][0]),
                                            "case_id": "b"})
        model = pe.drive_model(ir)
        by_case = pe.one_call_by_case(ir, model)
        self.assertEqual(by_case, {"a": {"a_x": frozenset({"F"})},
                                   "b": {"a_x": frozenset({"G"})}})
        pred = {"target_cases": ["a", "b"], "per_case": True,
                "expr": "final.a_x + maxabs(final.F)"}
        self.assertEqual(pe.record_scalar_pairing(pred, model.one_call_scalars, by_case),
                         {"a_x": {"scalar": ["a", "b"], "paired": ["a"]}})


class ShippedFixturesTest(unittest.TestCase):
    """The two hand-converted fixtures pass every IR-side gate and evaluate."""

    def test_both_fixtures(self) -> None:
        import yaml
        for rel in ("coriolis_0.2.0.spec.ir.yaml", "shallow_water2d_0.4.7/spec.ir.yaml"):
            with self.subTest(rel=rel):
                ir = yaml.safe_load((_DRIVE_DIR / rel).read_text())
                self.assertEqual(pe.drive_violations(ir, kind=ir["meta"]["spec_kind"]), [])
                io, cases = ir["io_contract"], pe._cases_by_id(ir)
                ctx = pe.predicate_schema_context(ir)
                self.assertIsNotNone(ctx["case_inputs"])
                self.assertEqual(pe.validate_primary_predicate_schema(
                    io["predicates"], case_ids=set(cases),
                    test_ids=[t["test_id"] for t in io["tests"]],
                    schema=pe.snapshot_schema(ir), cases=cases,
                    test_target_cases={t["test_id"]: t["target_cases"] for t in io["tests"]},
                    **ctx), [])
                self.assertEqual(pe.quantity_violations(io["tests"], io["predicates"],
                                                        ctx["reads"]), [])

    def test_the_loop_fixture_lowers_one_call(self) -> None:
        """R1-6: on a `loop` node `algorithm` lowers ONE call of `__step` (phase_01
        §`algorithm.execution_mode`): the converted swe fixture is `sequence` with an empty
        `iteration_contract` and orders no host-evaluated step. No gate pins this (an inner
        iterative solve is legitimate; V2 judges it); this row keeps the reference fixture in
        the documented form."""
        import yaml
        ir = yaml.safe_load((_DRIVE_DIR / "shallow_water2d_0.4.7/spec.ir.yaml").read_text())
        algo = ir["algorithm"]
        self.assertIn("loop", ir["drive"])
        self.assertEqual((algo["execution_mode"], algo["iteration_contract"]), ("sequence", {}))
        self.assertEqual(algo["ordering"], [st["step_id"] for st in algo["steps"]])
        self.assertFalse({"step_02_initial_condition", "step_04_dt_rule"}
                         & {st["step_id"] for st in algo["steps"]})

    def test_a_failed_case_evaluation_is_reported_once(self) -> None:
        """One case whose evaluation fails is (c)'s finding; the predicate gate judges each
        `derived.*` read against the names the case declares instead of refusing every read
        of every case (the cascade found while wiring PR-1a), and an undeclared name is still
        refused."""
        import yaml
        ir = yaml.safe_load((_DRIVE_DIR / "shallow_water2d_0.4.7/spec.ir.yaml").read_text())
        first = ir["case"]["test_case_set"][0]
        first["derived"]["dx"] = "inputs.grid.L_x / 0"
        drive_v = pe.drive_violations(ir, kind="problem")
        self.assertTrue(drive_v)
        self.assertTrue(all(m.startswith("(c)") for m in drive_v), drive_v)
        io, cases = ir["io_contract"], pe._cases_by_id(ir)
        ctx = pe.predicate_schema_context(ir)
        self.assertIsNone(ctx["case_inputs"])

        def schema_v(predicates: list) -> list[str]:
            return pe.validate_primary_predicate_schema(
                predicates, case_ids=set(cases),
                test_ids=[t["test_id"] for t in io["tests"]],
                schema=pe.snapshot_schema(ir), cases=cases,
                test_target_cases={t["test_id"]: t["target_cases"] for t in io["tests"]},
                **ctx)

        self.assertEqual(schema_v(io["predicates"]), [])
        reader = next(p for p in io["predicates"] if "derived." in str(p.get("expr")))
        bad = dict(reader, expr=str(reader["expr"]).replace("derived.", "derived.no_such_", 1))
        self.assertTrue(any("derived.no_such_" in m for m in schema_v([bad])))


if __name__ == "__main__":
    unittest.main()
