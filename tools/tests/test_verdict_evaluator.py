"""Unit tests for the deterministic per-test verdict evaluator (R2).

Two concerns:
  1. The evaluator/schema primitives (ops, ref/case resolution, per-case maps, na_allowed,
     failure_class reduction).
  2. Expressibility proof: every pass-rule shape found across the 12 existing tests.md files
     reduces to the DSL (the M1 acceptance criterion) — in particular shallow_water2d's
     nx-dependent thresholds, convergence order, and N/A rules.
"""

import unittest
from pathlib import Path

from tools import verdict_evaluator
from tools.verdict_evaluator import (
    CHECK_REF_LEAF,
    CHECK_STATUS_OPS,
    CHECK_STATUS_VALUES,
    PredicateError,
    evaluate_predicate,
    evaluate_verdict,
    validate_predicate_schema,
)


class OpsAndResolutionTest(unittest.TestCase):
    def test_ops(self) -> None:
        diag = {"metrics": {"metrics.m": 0.5, "metrics.s": "pass", "metrics.flag": True},
                "checks": {"b": {"status": "pass"}},
                "verdict": {"overall": "pass", "failed_checks": ["cfl", "input_guard"]}}

        def one(ref, op, value):
            pred = {"test_id": "t", "expected_outcome": "pass", "target_cases": [],
                    "pass_when": {"all": [{"ref": ref, "op": op, "value": value}]}}
            return evaluate_predicate(pred, diag)[0]

        self.assertEqual(one("metrics.m", "le", 1.0), "pass")
        self.assertEqual(one("metrics.m", "ge", 1.0), "fail")
        self.assertEqual(one("metrics.m", "lt", 0.5), "fail")
        self.assertEqual(one("metrics.m", "gt", 0.4), "pass")
        self.assertEqual(one("metrics.s", "eq", "pass"), "pass")
        self.assertEqual(one("metrics.s", "ne", "fail"), "pass")
        self.assertEqual(one("checks.b.status", "eq", "pass"), "pass")
        # the bool branch of `_values_equal`, observed through a metric address (the runner
        # writes a check's leaf as a `status` enum, never as a bool — issue #269)
        self.assertEqual(one("metrics.flag", "eq", True), "pass")
        self.assertEqual(one("verdict.failed_checks", "includes", "cfl"), "pass")
        self.assertEqual(one("verdict.failed_checks", "includes", "nope"), "fail")

    def test_bool_and_number_do_not_collide(self) -> None:
        # True must not equal 1; a boolean metric compared to a numeric literal fails cleanly.
        diag = {"metrics": {"metrics.flag": True}}
        pred = {"test_id": "t", "expected_outcome": "pass", "target_cases": [],
                "pass_when": {"all": [{"ref": "metrics.flag", "op": "eq", "value": 1}]}}
        self.assertEqual(evaluate_predicate(pred, diag)[0], "fail")

    def test_ordered_op_on_non_number_is_false(self) -> None:
        diag = {"metrics": {"metrics.s": "pass"}}
        pred = {"test_id": "t", "expected_outcome": "pass", "target_cases": [],
                "pass_when": {"all": [{"ref": "metrics.s", "op": "le", "value": 1.0}]}}
        status, kind, _ = evaluate_predicate(pred, diag)
        self.assertEqual(status, "fail")
        self.assertEqual(kind, "physics")

    def test_absent_ref_is_structural(self) -> None:
        pred = {"test_id": "t", "expected_outcome": "pass", "target_cases": [],
                "pass_when": {"all": [{"ref": "checks.gone.status", "op": "eq", "value": "pass"}]}}
        status, kind, _ = evaluate_predicate(pred, {"verdict": {"overall": "pass"}})
        self.assertEqual((status, kind), ("fail", "structural"))

    def test_includes_requires_list_not_string(self) -> None:
        # F4: `includes` must not substring-match a string lhs.
        diag = {"verdict": {"overall": "passed"}}
        pred = {"test_id": "t", "expected_outcome": "pass", "target_cases": [],
                "pass_when": {"all": [{"ref": "verdict.overall", "op": "includes",
                                       "value": "pass"}]}}
        self.assertEqual(evaluate_predicate(pred, diag)[0], "fail")

    def test_per_case_empty_target_cases_is_non_satisfying(self) -> None:
        # F3: a per_case condition with no target cases must not vacuously pass.
        pred = {"test_id": "t", "expected_outcome": "pass", "target_cases": [],
                "pass_when": {"all": [{"ref": "metrics.x", "op": "le", "value": 1.0,
                                       "per_case": True}]}}
        diag = {"cases": [{"case_id": "c", "metrics": {"metrics.x": 999}}]}
        status, kind, _ = evaluate_predicate(pred, diag)
        self.assertEqual((status, kind), ("fail", "structural"))

    def test_includes_bool_number_disjoint(self) -> None:
        # membership keeps bool/number separate: numeric 1 does not match a list-of-True.
        diag = {"verdict": {"failed_checks": [True]}}
        pred = {"test_id": "t", "expected_outcome": "pass", "target_cases": [],
                "pass_when": {"all": [{"ref": "verdict.failed_checks", "op": "includes",
                                       "value": 1}]}}
        self.assertEqual(evaluate_predicate(pred, diag)[0], "fail")

    def test_na_allowed_absent_ref_passes(self) -> None:
        pred = {"test_id": "t", "expected_outcome": "pass", "target_cases": ["c"],
                "pass_when": {"all": [{"ref": "errors.sym.l2", "op": "le", "value": 1e-9,
                                       "per_case": True, "na_allowed": True}]}}
        # case c present but the metric is absent (not applied) -> satisfied
        status, _, _ = evaluate_predicate(pred, {"cases": {"c": {"other": 1}}})
        self.assertEqual(status, "pass")


class MetricAddressResolutionTest(unittest.TestCase):
    """A `ref` whose head is neither `checks` nor `verdict` is an opaque metric ADDRESS: a
    whole-string key of the slice's flat `metrics` map, never a nested path. This is the shape
    the harness-rendered runner writes (`{"metrics": {"metrics.zero_rhs_max_abs_dev": 0.0}}`)
    and the vocabulary the Compile gate pins against `diagnostics_contract.metrics`."""

    def _pred(self, ref, **cond):
        return {"test_id": "t", "expected_outcome": "pass", "target_cases": ["c1"],
                "pass_when": {"all": [{"ref": ref, "op": "le", "value": 1.0e-10,
                                       "per_case": True, **cond}]}}

    def test_flat_address_key_resolves(self) -> None:
        # Reproduction pin for the E2E #4 ssprk2 failure: a nested-path resolution reported
        # `ref_absent` (structural_violation) on a run whose metric was present and passing.
        diag = {"per_case": {"c1": {"checks": {"zero_rhs": {"status": "pass"}},
                                    "metrics": {"metrics.zero_rhs_max_abs_dev": 0.0}}}}
        status, kind, _ = evaluate_predicate(
            self._pred("metrics.zero_rhs_max_abs_dev"), diag)
        self.assertEqual((status, kind), ("pass", "pass"))

    def test_non_metrics_heads_resolve_from_the_same_map(self) -> None:
        # `errors.*` / `cfl.*` / `convergence.*` are addresses too, not sub-objects.
        diag = {"per_case": {"c1": {"metrics": {"errors.l2": 1.0e-12, "cfl.max": 0.45}}}}
        self.assertEqual(evaluate_predicate(self._pred("errors.l2"), diag)[0], "pass")
        status, kind, _ = evaluate_predicate(
            {"test_id": "t", "expected_outcome": "pass", "target_cases": ["c1"],
             "pass_when": {"all": [{"ref": "cfl.max", "op": "le", "value": 1.0,
                                    "per_case": True}]}}, diag)
        self.assertEqual((status, kind), ("pass", "pass"))

    def test_absent_address_is_structural(self) -> None:
        for diag in (
            {"per_case": {"c1": {"metrics": {"metrics.other": 0.0}}}},   # key absent
            {"per_case": {"c1": {"checks": {}}}},                        # no metrics map
            {"per_case": {"c1": {"metrics": []}}},                       # metrics not a map
        ):
            status, kind, basis = evaluate_predicate(self._pred("metrics.x"), diag)
            self.assertEqual((status, kind), ("fail", "structural"), diag)
            self.assertEqual(basis["conditions"][0]["evaluated"][-1]["reason"], "ref_absent")

    def test_a_nested_sub_object_is_not_decomposed(self) -> None:
        # The pre-fix (path) semantics would have resolved this; the address semantics must not.
        diag = {"per_case": {"c1": {"metrics": {"errors": {"l2": 1.0e-12}}}}}
        self.assertEqual(evaluate_predicate(self._pred("errors.l2"), diag)[1], "structural")

    def test_checks_and_verdict_heads_stay_nested(self) -> None:
        # A literal `checks.x.status` KEY in the metrics map must not hijack the checks ref.
        diag = {"per_case": {"c1": {
            "checks": {"x": {"status": "pass"}},
            "verdict": {"overall": "pass"},
            "metrics": {"checks.x.status": "fail", "verdict.overall": "fail"}}}}
        for ref in ("checks.x.status", "verdict.overall"):
            pred = {"test_id": "t", "expected_outcome": "pass", "target_cases": ["c1"],
                    "pass_when": {"all": [{"ref": ref, "op": "eq", "value": "pass",
                                           "per_case": True}]}}
            self.assertEqual(evaluate_predicate(pred, diag)[0], "pass", ref)

    def test_null_address_is_present_and_gated_by_na_allowed(self) -> None:
        # The harness writes an honest N/A as `"<address>": null` + `"<address>_reason_na"`.
        diag = {"per_case": {"c1": {"metrics": {"metrics.x": None,
                                                "metrics.x_reason_na": "not applied"}}}}
        status, _, _ = evaluate_predicate(self._pred("metrics.x", na_allowed=True), diag)
        self.assertEqual(status, "pass")
        status, kind, _ = evaluate_predicate(self._pred("metrics.x"), diag)
        self.assertEqual((status, kind), ("fail", "structural"))

    def test_suite_level_address_resolves_without_per_case(self) -> None:
        # A non-per_case metric ref (e.g. a convergence order) reads the top-level metrics map.
        diag = {"metrics": {"convergence.n32_to_n64.analytic_h_order": 0.86}}
        pred = {"test_id": "t", "expected_outcome": "pass", "target_cases": ["c1"],
                "pass_when": {"all": [{"ref": "convergence.n32_to_n64.analytic_h_order",
                                       "op": "ge", "value": 0.80}]}}
        self.assertEqual(evaluate_predicate(pred, diag)[0], "pass")


class CaseResolutionTest(unittest.TestCase):
    def test_map_cases(self) -> None:
        diag = {"cases": {"c1": {"checks": {"profile_selected": {"status": "pass"}}}}}
        pred = {"test_id": "t", "expected_outcome": "pass", "target_cases": ["c1"],
                "pass_when": {"all": [{"ref": "checks.profile_selected.status", "op": "eq",
                                       "value": "pass", "per_case": True}]}}
        self.assertEqual(evaluate_predicate(pred, diag)[0], "pass")

    def test_array_cases(self) -> None:
        diag = {"cases": [{"case_id": "n032", "metrics": {"metrics.mass_drift_rel": 1e-13}}]}
        pred = {"test_id": "t", "expected_outcome": "pass", "target_cases": ["n032"],
                "pass_when": {"all": [{"ref": "metrics.mass_drift_rel", "op": "le",
                                       "value": 1e-10, "per_case": True}]}}
        self.assertEqual(evaluate_predicate(pred, diag)[0], "pass")

    def test_per_case_container_map(self) -> None:
        # Real runners emit a top-level `per_case: {case_id: {...}}` container (e.g. the
        # ssprk2 / demo_dep_top diagnostics). A per_case predicate must resolve THAT slice,
        # not the suite-level object.
        diag = {"checks": {}, "verdict": {"overall": "pass"},
                "per_case": {"l0_x": {"verdict": {"overall": "fail", "failed_checks": ["cfl"]}}}}
        pred = {"test_id": "t", "expected_outcome": "xfail", "target_cases": ["l0_x"],
                "pass_when": {"all": [{"ref": "verdict.overall", "op": "eq", "value": "fail",
                                       "per_case": True},
                                      {"ref": "verdict.failed_checks", "op": "includes",
                                       "value": "cfl", "per_case": True}]}}
        # resolves the per_case slice (overall=fail) not the top-level (overall=pass)
        self.assertEqual(evaluate_predicate(pred, diag)[0], "xfail")

    def test_per_case_container_metric_entries(self) -> None:
        # A per_case entry carrying only a metrics map (no checks/verdict) resolves a metric
        # address against that map.
        diag = {"per_case": {"c": {"metrics": {"metrics.max_abs_dev": 0.0}}}}
        pred = {"test_id": "t", "expected_outcome": "pass", "target_cases": ["c"],
                "pass_when": {"all": [{"ref": "metrics.max_abs_dev", "op": "le", "value": 1.0e-12,
                                       "per_case": True}]}}
        self.assertEqual(evaluate_predicate(pred, diag)[0], "pass")

    def test_case_slice_falls_through_to_other_container(self) -> None:
        # If the preferred `cases` container lacks the case, fall through to `per_case`.
        diag = {"cases": {"other": {"m": 1}},
                "per_case": {"c": {"verdict": {"overall": "pass"}}}}
        pred = {"test_id": "t", "expected_outcome": "pass", "target_cases": ["c"],
                "pass_when": {"all": [{"ref": "verdict.overall", "op": "eq", "value": "pass",
                                       "per_case": True}]}}
        self.assertEqual(evaluate_predicate(pred, diag)[0], "pass")

    def test_per_case_against_flat_diagnostics_is_structural(self) -> None:
        # A per_case predicate against a container-less (component-flat) diagnostics must NOT
        # silently broadcast the top-level object to every case — it is a shape mismatch.
        diag = {"checks": {"g": {"pass": True}}, "verdict": {"overall": "pass"}}
        pred = {"test_id": "t", "expected_outcome": "pass", "target_cases": ["c1"],
                "pass_when": {"all": [{"ref": "verdict.overall", "op": "eq", "value": "pass",
                                       "per_case": True}]}}
        self.assertEqual(evaluate_predicate(pred, diag)[1], "structural")

    def test_missing_case_is_structural(self) -> None:
        diag = {"cases": [{"case_id": "n032", "metrics": {"metrics.mass_drift_rel": 1e-13}}]}
        pred = {"test_id": "t", "expected_outcome": "pass", "target_cases": ["n999"],
                "pass_when": {"all": [{"ref": "metrics.mass_drift_rel", "op": "le",
                                       "value": 1e-10, "per_case": True}]}}
        self.assertEqual(evaluate_predicate(pred, diag)[1], "structural")

    def test_per_case_threshold_map(self) -> None:
        addr = "errors.analytic_h.l2_rel_tend"
        diag = {"cases": [{"case_id": "n032", "metrics": {addr: 0.21}},
                          {"case_id": "n064", "metrics": {addr: 0.11}}]}
        pred = {"test_id": "t", "expected_outcome": "pass", "target_cases": ["n032", "n064"],
                "pass_when": {"all": [{"ref": addr, "op": "le", "per_case": True,
                                       "value": {"per_case": {"n032": 2.2e-1, "n064": 1.2e-1}}}]}}
        self.assertEqual(evaluate_predicate(pred, diag)[0], "pass")
        # tighten n064 beyond tolerance -> physics fail
        diag["cases"][1]["metrics"][addr] = 0.13
        self.assertEqual(evaluate_predicate(pred, diag)[0], "fail")


class VerdictReduceTest(unittest.TestCase):
    def _mk(self, statuses):
        # build predicates that trivially yield the requested statuses via verdict.overall
        preds, diag = [], {"cases": {}}
        for i, st in enumerate(statuses):
            cid = f"c{i}"
            expected = "xfail" if st == "xfail" else "pass"
            want_pass = st in ("pass", "xfail")
            diag["cases"][cid] = {"verdict": {"overall": "pass" if want_pass else "fail"}}
            preds.append({"test_id": f"t{i}", "expected_outcome": expected,
                          "target_cases": [cid],
                          "pass_when": {"all": [{"ref": "verdict.overall", "op": "eq",
                                                 "value": "pass", "per_case": True}]}})
        return preds, diag

    def test_all_pass(self) -> None:
        preds, diag = self._mk(["pass", "pass"])
        doc = evaluate_verdict(preds, diag, run_id="r", node_key="n")
        self.assertEqual(doc["self_verdict"], "pass")
        self.assertEqual(doc["failure_class"], "pass")

    def test_all_xfail_is_xfail(self) -> None:
        preds, diag = self._mk(["xfail", "xfail"])
        self.assertEqual(evaluate_verdict(preds, diag)["self_verdict"], "xfail")

    def test_mixed_pass_and_xfail_is_pass(self) -> None:
        preds, diag = self._mk(["pass", "xfail"])
        self.assertEqual(evaluate_verdict(preds, diag)["self_verdict"], "pass")

    def test_any_fail_is_fail_physics(self) -> None:
        preds, diag = self._mk(["pass", "fail"])
        doc = evaluate_verdict(preds, diag)
        self.assertEqual(doc["self_verdict"], "fail")
        self.assertEqual(doc["failure_class"], "physics_fail")

    def test_structural_dominates_physics(self) -> None:
        # one physics fail + one structural (absent case) -> structural_violation
        preds, diag = self._mk(["fail"])
        preds.append({"test_id": "tx", "expected_outcome": "pass", "target_cases": ["absent"],
                      "pass_when": {"all": [{"ref": "verdict.overall", "op": "eq",
                                             "value": "pass", "per_case": True}]}})
        self.assertEqual(evaluate_verdict(preds, diag)["failure_class"], "structural_violation")

    def test_empty_predicates_fail_structural_not_pass(self) -> None:
        # A node with no evaluable per-test rule must not certify as pass.
        for preds in ([], None):
            doc = evaluate_verdict(preds, {"verdict": {"overall": "pass"}},
                                   run_id="r", node_key="n")
            self.assertEqual(doc["self_verdict"], "fail")
            self.assertEqual(doc["failure_class"], "structural_violation")
            self.assertEqual(doc["per_test"], [])

    def test_missing_test_id_raises(self) -> None:
        with self.assertRaises(PredicateError):
            evaluate_verdict([{"expected_outcome": "pass", "target_cases": [],
                               "pass_when": {"all": [{"ref": "a", "op": "eq", "value": 1}]}}], {})


class CaseScopedConditionTest(unittest.TestCase):
    """R3-core: `case: <case_id>` resolves ONE target case's slice.

    This is the scope a cross-case reduction is read at — the checks module emits a
    convergence order as a per-case metric of the case that completes it, so the predicate
    must compare it there rather than in every case (`per_case`) or suite-wide (neither).
    """

    # Two resolutions; only the finer one carries the derived order metric (a sparse
    # per-case metric, exactly what the accumulator pattern produces).
    _DIAG = {
        "per_case": {
            "n032": {"metrics": {"errors.h.l2": 4.0e-3}},
            "n064": {"metrics": {"errors.h.l2": 1.0e-3,
                                 "convergence.order_n032_to_n064": 2.0}},
        }
    }

    def _pred(self, **cond):
        base = {"ref": "convergence.order_n032_to_n064", "op": "ge", "value": 1.9,
                "case": "n064"}
        base.update(cond)
        # `case=None` from a caller means "drop the key". An absent `case` and an explicit
        # `case: null` are NOT the same thing to the evaluator — see `test_explicit_null_case_raises`.
        if "case" in cond and cond["case"] is None:
            base.pop("case")
        return {"test_id": "t", "expected_outcome": "pass",
                "target_cases": ["n032", "n064"], "pass_when": {"all": [base]}}

    def test_resolves_the_named_case(self) -> None:
        status, kind, basis = evaluate_predicate(self._pred(), self._DIAG)
        self.assertEqual((status, kind), ("pass", "pass"))
        evaluated = basis["conditions"][0]["evaluated"]
        self.assertEqual([e["case"] for e in evaluated], ["n064"])
        self.assertEqual(evaluated[0]["lhs"], 2.0)

    def test_physics_fail_when_the_named_case_misses_the_threshold(self) -> None:
        status, kind, _ = evaluate_predicate(self._pred(value=2.5), self._DIAG)
        self.assertEqual((status, kind), ("fail", "physics"))

    def test_a_per_case_condition_would_be_structural_on_the_sparse_metric(self) -> None:
        # The contrast that motivates `case:`: `per_case` ranges over BOTH cases, and n032
        # legitimately has no order metric — reporting a contract gap for correct evidence.
        status, kind, _ = evaluate_predicate(
            self._pred(case=None, per_case=True), self._DIAG)
        self.assertEqual((status, kind), ("fail", "structural"))

    def test_absent_slice_is_structural(self) -> None:
        status, kind, basis = evaluate_predicate(self._pred(), {"per_case": {"n032": {}}})
        self.assertEqual((status, kind), ("fail", "structural"))
        self.assertEqual(basis["conditions"][0]["evaluated"][0]["reason"], "case_absent")

    def test_absent_ref_in_the_named_case_is_structural(self) -> None:
        status, kind, basis = evaluate_predicate(
            self._pred(), {"per_case": {"n064": {"metrics": {}}}})
        self.assertEqual((status, kind), ("fail", "structural"))
        self.assertEqual(basis["conditions"][0]["evaluated"][0]["reason"], "ref_absent")

    def test_case_and_per_case_together_raise(self) -> None:
        with self.assertRaises(PredicateError):
            evaluate_predicate(self._pred(per_case=True), self._DIAG)

    def test_non_string_case_raises(self) -> None:
        for bad in (7, "", "   ", ["n064"]):
            with self.subTest(bad=bad):
                with self.assertRaises(PredicateError):
                    evaluate_predicate(self._pred(case=bad), self._DIAG)

    def test_explicit_null_case_raises(self) -> None:
        # `case: null` is rejected by the schema gate. The evaluator must not read it as
        # "absent" and silently widen the condition to suite-level scope — that would
        # evaluate different data than the author wrote.
        pred = self._pred()
        pred["pass_when"]["all"][0]["case"] = None
        with self.assertRaises(PredicateError):
            evaluate_predicate(pred, self._DIAG)

    def test_absent_case_key_resolves_suite_scope(self) -> None:
        # ...whereas an ABSENT `case` key legitimately means the suite-level object.
        pred = self._pred()
        pred["pass_when"]["all"][0].pop("case")
        pred["pass_when"]["all"][0]["ref"] = "verdict.overall"
        pred["pass_when"]["all"][0].update(op="eq", value="pass")
        self.assertEqual(
            evaluate_predicate(pred, {"verdict": {"overall": "pass"}, **self._DIAG})[0], "pass")


class SchemaTest(unittest.TestCase):
    def _kwargs(self, **over):
        base = dict(case_ids={"c1"}, test_ids=["t1"], check_ids={"g"},
                    verdict_fields={"overall", "failed_checks"}, metric_addrs=set())
        base.update(over)
        return base

    def _pred(self, **over):
        p = {"test_id": "t1", "expected_outcome": "pass", "target_cases": ["c1"],
             "pass_when": {"all": [{"ref": "verdict.overall", "op": "eq", "value": "pass",
                                    "quantity": "overall"}]}}
        p.update(over)
        return p

    def test_valid(self) -> None:
        self.assertEqual(validate_predicate_schema([self._pred()], **self._kwargs()), [])

    def test_scope_and_na_flags_must_be_booleans(self) -> None:
        """Round 2 (census): every reader of `per_case` / `na_allowed` uses `bool()`, so a
        truthy string or `1` is admitted only if no gate refuses it; this one does, so the
        coverage gate and the evaluator never read a scope the schema did not."""
        for flag, bad in (("per_case", 1), ("per_case", "true"), ("na_allowed", "yes"),
                          ("per_case", None)):
            with self.subTest(flag=flag, bad=bad):
                v = validate_predicate_schema([self._pred(pass_when={"all": [
                    {"ref": "verdict.overall", "op": "eq", "value": "pass",
                     "quantity": "overall", flag: bad}]})], **self._kwargs())
                self.assertTrue(any(f"{flag} must be a boolean" in x for x in v), v)
        v = validate_predicate_schema([self._pred(pass_when={"all": [
            {"ref": "verdict.overall", "op": "eq", "value": "pass", "quantity": "overall",
             "per_case": True, "na_allowed": False}]})], **self._kwargs())
        self.assertEqual(v, [])

    def test_quantity_is_required_on_every_condition(self) -> None:
        """Z6 PR-3 (issue #255): a condition with no `quantity` — a `verdict.*` one included —
        is refused, since the coverage gate keys a condition's corroborant by that name; a
        malformed name is refused by the same rule."""
        for cond in ({"ref": "verdict.overall", "op": "eq", "value": "pass"},
                     {"ref": "verdict.overall", "op": "eq", "value": "pass", "quantity": "Bad Q"},
                     {"ref": "verdict.overall", "op": "eq", "value": "pass", "quantity": None}):
            with self.subTest(cond=cond):
                v = validate_predicate_schema([self._pred(pass_when={"all": [cond]})],
                                              **self._kwargs())
                self.assertTrue(any("quantity must be present and match" in x for x in v), v)

    def test_empty_required(self) -> None:
        self.assertTrue(validate_predicate_schema([], **self._kwargs()))
        self.assertTrue(validate_predicate_schema(None, **self._kwargs()))

    def test_unhashable_op_is_a_violation_not_a_crash(self) -> None:
        # A malformed op authored as a YAML list/map must yield a violation, not TypeError.
        for bad_op in ([{"eq": 1}], {"op": "eq"}):
            v = validate_predicate_schema(
                [self._pred(pass_when={"all": [{"ref": "verdict.overall", "op": bad_op,
                                                "value": "pass"}]})], **self._kwargs())
            self.assertTrue(any(".op must be one of" in x for x in v), (bad_op, v))

    def test_bad_op_and_outcome(self) -> None:
        v = validate_predicate_schema(
            [self._pred(expected_outcome="maybe",
                        pass_when={"all": [{"ref": "verdict.overall", "op": "between",
                                            "value": 1}]})], **self._kwargs())
        self.assertTrue(any("expected_outcome" in x for x in v))
        self.assertTrue(any(".op must be one of" in x for x in v))

    def test_unknown_target_case(self) -> None:
        v = validate_predicate_schema([self._pred(target_cases=["zzz"])], **self._kwargs())
        self.assertTrue(any("unknown case_id" in x for x in v))

    def _case_scoped(self, **cond):
        base = {"ref": "verdict.overall", "op": "eq", "value": "pass", "case": "c1",
                "quantity": "overall"}
        base.update(cond)
        return self._pred(target_cases=["c1", "c2"], pass_when={"all": [base]})

    def test_case_scoped_condition_is_valid(self) -> None:
        self.assertEqual(
            validate_predicate_schema([self._case_scoped()],
                                      **self._kwargs(case_ids={"c1", "c2"})), [])

    def test_case_must_be_one_of_this_predicates_target_cases(self) -> None:
        # `c3` is a DECLARED case, so `target_cases` validation would pass it — but this
        # predicate does not range over it, and no metrics-basis row exists for the pair.
        v = validate_predicate_schema([self._case_scoped(case="c3")],
                                      **self._kwargs(case_ids={"c1", "c2", "c3"}))
        self.assertTrue(any("is not one of this predicate's target_cases" in x for x in v), v)

    def test_case_must_be_a_non_empty_string(self) -> None:
        for bad in ("", "   ", 7, ["c1"], None):
            with self.subTest(bad=bad):
                v = validate_predicate_schema([self._case_scoped(case=bad)],
                                              **self._kwargs(case_ids={"c1", "c2"}))
                self.assertTrue(any(".case must be a non-empty string" in x for x in v), v)

    def test_case_membership_message_does_not_stringify_a_non_str_target(self) -> None:
        # `target_cases: [123, "c2"]` is separately reported as an unknown case_id. The `case`
        # membership message must not print `123` as `'123'`, which would read as though the
        # rejected case WERE present in the list it is being compared against.
        pred = self._pred(target_cases=[123, "c2"],
                          pass_when={"all": [{"ref": "verdict.overall", "op": "eq",
                                              "value": "pass", "case": "123"}]})
        v = validate_predicate_schema([pred], **self._kwargs(case_ids={"c1", "c2"}))
        matched = [x for x in v if ".case '123' is not one of" in x]
        self.assertEqual(len(matched), 1, v)
        self.assertIn("target_cases (['c2'])", matched[0])

    def test_case_and_per_case_are_mutually_exclusive(self) -> None:
        v = validate_predicate_schema([self._case_scoped(per_case=True)],
                                      **self._kwargs(case_ids={"c1", "c2"}))
        self.assertTrue(any("mutually exclusive condition scopes" in x for x in v), v)

    def test_test_id_set_mismatch(self) -> None:
        v = validate_predicate_schema([self._pred()], **self._kwargs(test_ids=["t1", "t2"]))
        self.assertTrue(any("missing tests from tests.md" in x for x in v))
        v = validate_predicate_schema([self._pred(test_id="tX")], **self._kwargs())
        self.assertTrue(any("unknown test_id" in x for x in v))

    def test_unknown_check_ref(self) -> None:
        v = validate_predicate_schema(
            [self._pred(pass_when={"all": [{"ref": "checks.nope.status", "op": "eq",
                                            "value": "pass"}]})], **self._kwargs())
        self.assertTrue(any("diagnostics_contract.checks" in x for x in v))
        # The id is judged FIRST and short-circuits: an unknown id WITH a refused tail and a
        # refused value earns the id message ALONE — never the tail's or the value's (issue
        # #269; round 1 found the row observing this on a clean tail, where neither ordering
        # nor short-circuit is reachable).
        v = validate_predicate_schema(
            [self._pred(pass_when={"all": [{"ref": "checks.nope.pass", "op": "eq",
                                            "value": True}]})], **self._kwargs())
        about_nope = [x for x in v if "checks.nope" in x or ".value" in x]
        self.assertEqual(len(about_nope), 1, v)
        self.assertIn("checks.nope not in diagnostics_contract.checks", about_nope[0])

    def _check_ref_violations(self, ref: str, value: object = "pass") -> list[str]:
        return validate_predicate_schema(
            [self._pred(pass_when={"all": [{"ref": ref, "op": "eq", "value": value,
                                            "quantity": "g"}]})], **self._kwargs())

    def test_check_ref_pass_leaf_is_refused(self) -> None:
        # The spelling `phase_01_compile.md` offered before issue #269; the runner never
        # emits a `pass` leaf, so at execute it was `ref_absent` -> structural_violation ->
        # Generate, which cannot repair the IR.
        v = self._check_ref_violations("checks.g.pass", True)
        self.assertEqual(len(v), 1, v)
        self.assertIn("reads a `pass` leaf", v[0])
        self.assertIn("— write checks.g.status", v[0])

    def test_check_ref_bare_id_is_refused(self) -> None:
        # Worse than `.pass`: the dict IS present, so every op compares false and the test
        # fails as `physics_fail` with no structural record at all.
        v = self._check_ref_violations("checks.g", True)
        self.assertEqual(len(v), 1, v)
        self.assertIn("names the check object", v[0])
        self.assertIn("— write checks.g.status", v[0])

    def test_check_ref_extra_tail_is_refused(self) -> None:
        # Two different spellings of "some other tail" (rule 1-b: not one counterexample).
        # `checks.g.Status` is round 1's surviving mutant: a case-folding comparison passed
        # every row while `_resolve_ref` is case-exact, so the ref was `ref_absent` at execute.
        for ref in ("checks.g.status.x", "checks.g.result", "checks.g.Status"):
            with self.subTest(ref=ref):
                v = self._check_ref_violations(ref)
                self.assertEqual(len(v), 1, v)
                self.assertIn("has the tail", v[0])
                self.assertIn("— write checks.g.status", v[0])

    def test_check_ref_status_leaf_resolves(self) -> None:
        self.assertEqual(self._check_ref_violations("checks.g.status"), [])

    def test_check_status_condition_pins_op_and_value(self) -> None:
        """Round 1 (issue #269): the ref remedy alone was followable by half — every corpus
        `.pass` predicate carried `value: true`, and `checks.<id>.status eq true` passed the
        gate while `_values_equal` never equates a bool to a str, so a correct kernel was
        reported `physics_fail`. PINNED: a non-member value (bool / number / misspelt member)
        and a non-status op (including `ne`) are each refused with one violation naming the
        repair; both members under `eq` pass. SAMPLED: the spellings below, not the whole
        value space."""
        def one(op, value):
            return validate_predicate_schema(
                [self._pred(pass_when={"all": [{"ref": "checks.g.status", "op": op,
                                                "value": value, "quantity": "g"}]})],
                **self._kwargs())
        for value in (True, 1, "failed", "PASS", "na", ["pass"]):
            with self.subTest(value=value):
                v = one("eq", value)
                self.assertEqual(len(v), 1, v)
                self.assertIn("is not a check status", v[0])
                self.assertIn('— write value "pass" or "fail"', v[0])
        # a null value is the schema's own rule (`must have a non-null value`), stated once
        v = one("eq", None)
        self.assertEqual(len(v), 1, v)
        self.assertIn("non-null", v[0])
        # `ne` is refused as an op (round 1, second pass): `ne "fail"` is satisfied by the
        # per-case `na` of a check that does not apply — a pass on an unevaluated check.
        with self.subTest(op="ne", value="fail"):
            v = one("ne", "fail")
            self.assertEqual(len(v), 1, v)
            self.assertIn("is not a status comparison", v[0])
        for op in sorted(set(verdict_evaluator._OPS) - set(CHECK_STATUS_OPS)):
            with self.subTest(op=op):
                # an ordered op also earns the schema's own "must be a number" rule; this row
                # pins the status refusal, stated exactly once
                v = [x for x in one(op, "pass") if "is not a status comparison" in x]
                self.assertEqual(len(v), 1, v)
        for op in sorted(CHECK_STATUS_OPS):
            for value in CHECK_STATUS_VALUES:
                with self.subTest(op=op, value=value):
                    self.assertEqual(one(op, value), [])
        # the op/value half is judged only once the ref half is clean: a refused ref with a
        # bad value earns the ref message ALONE (one repair at a time, ordered by reachability)
        v = self._check_ref_violations("checks.g.pass", True)
        self.assertEqual(len(v), 1, v)
        self.assertIn("reads a `pass` leaf", v[0])
        # a metric ref is never judged as a status
        self.assertEqual(validate_predicate_schema(
            [self._pred(pass_when={"all": [{"ref": "metrics.m", "op": "le", "value": 1.0,
                                            "quantity": "m"}]})],
            **self._kwargs(metric_addrs={"metrics.m"})), [])

    def test_padded_ref_is_refused_because_the_evaluator_does_not_strip(self) -> None:
        """Round 1 (issue #269): the gate used to validate `ref.strip()` while `_eval_condition`
        resolves `ref` verbatim, so a padded `checks.<id>.status` passed --stage compile and was
        `ref_absent` on every run — satisfied on every run under `na_allowed`. Pinned at the
        gate, and the asymmetry is pinned by driving both sides on the same input."""
        diag = {"cases": {"c1": {"checks": {"g": {"status": "fail"}}}}}
        for ref in (" checks.g.status", "checks.g.status ", "\tchecks.g.status\n"):
            with self.subTest(ref=ref):
                v = self._check_ref_violations(ref)
                self.assertEqual(len(v), 1, v)
                self.assertIn("whitespace", v[0])
                self.assertIn("— write 'checks.g.status'", v[0])
                pred = self._pred(pass_when={"all": [{"ref": ref, "op": "eq", "value": "pass",
                                                      "per_case": True, "na_allowed": True}]})
                # what the evaluator would have done with it: satisfied although the check FAILED
                self.assertEqual(evaluate_predicate(pred, diag)[:2], ("pass", "pass"))

    def test_unknown_verdict_field(self) -> None:
        v = validate_predicate_schema(
            [self._pred(pass_when={"all": [{"ref": "verdict.mystery", "op": "eq",
                                            "value": 1}]})], **self._kwargs())
        self.assertTrue(any("verdict.mystery" in x for x in v))

    def test_metric_addr_must_be_pinned(self) -> None:
        v = validate_predicate_schema(
            [self._pred(pass_when={"all": [{"ref": "metrics.mass_drift_rel", "op": "le",
                                            "value": 1e-10, "per_case": True}]})],
            **self._kwargs())
        self.assertTrue(any("diagnostics_contract.metrics" in x for x in v))
        # once pinned it resolves
        self.assertEqual(validate_predicate_schema(
            [self._pred(pass_when={"all": [{"ref": "metrics.mass_drift_rel", "op": "le",
                                            "value": 1e-10, "per_case": True,
                                            "quantity": "mass_drift_rel"}]})],
            **self._kwargs(metric_addrs={"metrics.mass_drift_rel"})), [])

    def test_metric_addr_must_be_pinned_verbatim_not_by_head(self) -> None:
        # A bare-head pin (`metrics: ["cfl"]`) behind a deeper ref (`cfl.max`) is unresolvable at
        # execute: the renderer emits one key per DECLARED address (`cfl`), while the evaluator
        # looks the whole ref up as a key -> ref_absent -> a permanent structural_violation on an
        # otherwise-correct run. Reject it at Compile, where it is repairable.
        pred = self._pred(pass_when={"all": [{"ref": "cfl.max", "op": "le", "value": 1.0,
                                              "per_case": True, "quantity": "cfl"}]})
        v = validate_predicate_schema([pred], **self._kwargs(metric_addrs={"cfl"}))
        self.assertTrue(any("diagnostics_contract.metrics" in x for x in v), v)
        # the same ref pinned verbatim is accepted, and it is the key the runner emits
        self.assertEqual(
            validate_predicate_schema([pred], **self._kwargs(metric_addrs={"cfl.max"})), [])
        diag = {"cases": {"c1": {"metrics": {"cfl.max": 0.45}}}}
        self.assertEqual(evaluate_predicate(pred, diag)[0], "pass")

    def test_missing_value_rejected(self) -> None:
        # F1: a condition without `value` would compare against None at execute (permanent
        # physics fail); it must be caught at Compile.
        v = validate_predicate_schema(
            [self._pred(pass_when={"all": [{"ref": "verdict.overall", "op": "eq"}]})],
            **self._kwargs())
        self.assertTrue(any("non-null `value`" in x for x in v))

    def test_non_per_case_dict_value_rejected(self) -> None:
        # A dict `value` that is not the {per_case: ...} form is malformed and would slip
        # through as a literal at execute (e.g. `ne` against a dict = vacuous true = false pass).
        for bad in ({"threshold": 0}, {"per_case": {"c1": 1}, "extra": 1}):
            v = validate_predicate_schema(
                [self._pred(pass_when={"all": [{"ref": "verdict.overall", "op": "ne",
                                                "value": bad}]})], **self._kwargs())
            self.assertTrue(any("value must be a scalar" in x for x in v), (bad, v))

    def test_explicit_null_value_rejected(self) -> None:
        v = validate_predicate_schema(
            [self._pred(pass_when={"all": [{"ref": "verdict.overall", "op": "eq",
                                            "value": None}]})], **self._kwargs())
        self.assertTrue(any("non-null `value`" in x for x in v))

    def test_bare_verdict_ref_rejected(self) -> None:
        v = validate_predicate_schema(
            [self._pred(pass_when={"all": [{"ref": "verdict", "op": "eq", "value": "pass"}]})],
            **self._kwargs())
        self.assertTrue(any("needs a field" in x for x in v))

    def test_per_case_value_map_must_cover_all_target_cases(self) -> None:
        # A per-case threshold map missing an entry for a target case would fail that case at
        # execute (value_absent_for_case) — catch it at Compile.
        v = validate_predicate_schema(
            [self._pred(target_cases=["c1", "c2"],
                        pass_when={"all": [{"ref": "metrics.m", "op": "le", "per_case": True,
                                            "value": {"per_case": {"c1": 1.0}}}]})],
            **self._kwargs(case_ids={"c1", "c2"}, metric_addrs={"metrics.m"}))
        self.assertTrue(any("missing a threshold for target case" in x for x in v), v)
        # complete map is accepted
        self.assertEqual(validate_predicate_schema(
            [self._pred(target_cases=["c1", "c2"],
                        pass_when={"all": [{"ref": "metrics.m", "op": "le", "per_case": True,
                                            "value": {"per_case": {"c1": 1.0, "c2": 2.0}},
                                            "quantity": "m"}]})],
            **self._kwargs(case_ids={"c1", "c2"}, metric_addrs={"metrics.m"})), [])

    def test_ordered_op_requires_numeric_threshold(self) -> None:
        # An ordered op with a string threshold (e.g. YAML "1e-10") deterministically fails at
        # execute; reject it at Compile.
        v = validate_predicate_schema(
            [self._pred(pass_when={"all": [{"ref": "metrics.m", "op": "le", "value": "1e-10"}]})],
            **self._kwargs(metric_addrs={"metrics.m"}))
        self.assertTrue(any("must be a number for the ordered op" in x for x in v), v)
        # per-case string thresholds are also caught
        v2 = validate_predicate_schema(
            [self._pred(target_cases=["c1"],
                        pass_when={"all": [{"ref": "metrics.m", "op": "ge", "per_case": True,
                                            "value": {"per_case": {"c1": "0.8"}}}]})],
            **self._kwargs(metric_addrs={"metrics.m"}))
        self.assertTrue(any("must be a number for the ordered op" in x for x in v2), v2)
        # a numeric threshold is accepted
        self.assertEqual(validate_predicate_schema(
            [self._pred(pass_when={"all": [{"ref": "metrics.m", "op": "le", "value": 1e-10,
                                            "quantity": "m"}]})],
            **self._kwargs(metric_addrs={"metrics.m"})), [])

    def test_verdict_ref_requires_declared_field_no_default(self) -> None:
        # With no declared verdict fields, a verdict.* ref is rejected (no seeded default).
        v = validate_predicate_schema(
            [self._pred(pass_when={"all": [{"ref": "verdict.overall", "op": "eq",
                                            "value": "pass"}]})],
            **self._kwargs(verdict_fields=set()))
        self.assertTrue(any("verdict.overall" in x for x in v), v)

    def test_per_case_value_map_requires_per_case_flag(self) -> None:
        v = validate_predicate_schema(
            [self._pred(pass_when={"all": [{"ref": "verdict.overall", "op": "eq",
                                            "value": {"per_case": {"c1": "pass"}}}]})],
            **self._kwargs())
        self.assertTrue(any("per_case value map but per_case is not true" in x for x in v))


class TwelveSpecExpressibilityTest(unittest.TestCase):
    """Each real tests.md pass-rule shape reduces to the DSL and evaluates correctly."""

    def test_component_status_check_and_guard(self) -> None:
        # demo_dep_base: a passing check + a standard "inverted" xfail guard (guard fires,
        # verdict stays pass). Each check is read at its `status` leaf, the one leaf the
        # runner writes (issue #269).
        diag = {"checks": {"scale_identity": {"status": "pass"}, "input_guard": {"status": "pass"}},
                "verdict": {"overall": "pass", "failed_checks": []}}
        preds = [
            {"test_id": "l0_scale_identity_pass", "expected_outcome": "pass",
             "target_cases": ["l0_scale_identity_pass"],
             "pass_when": {"all": [{"ref": "checks.scale_identity.status", "op": "eq", "value": "pass"},
                                   {"ref": "verdict.overall", "op": "eq", "value": "pass"}]}},
            {"test_id": "l0_invalid_length_xfail", "expected_outcome": "xfail",
             "target_cases": ["l0_invalid_length_xfail"],
             "pass_when": {"all": [{"ref": "checks.input_guard.status", "op": "eq", "value": "pass"},
                                   {"ref": "verdict.overall", "op": "eq", "value": "pass"}]}},
        ]
        doc = evaluate_verdict(preds, diag)
        self.assertEqual([p["status"] for p in doc["per_test"]], ["pass", "xfail"])
        self.assertEqual(doc["self_verdict"], "pass")

    def test_component_status_style_guard_membership_xfail(self) -> None:
        # dynamics component: checks.<name>.status == "pass"; guard xfail = overall fail AND
        # failed_checks includes 'input_guard'.
        diag = {"checks": {"input_guard": {"status": "fail", "invalid_state_detected": True}},
                "verdict": {"overall": "fail", "failed_checks": ["input_guard"]}}
        pred = {"test_id": "l0_invalid_dry_state_xfail", "expected_outcome": "xfail",
                "target_cases": ["l0_invalid_dry_state_xfail"],
                "pass_when": {"all": [{"ref": "verdict.overall", "op": "eq", "value": "fail"},
                                      {"ref": "verdict.failed_checks", "op": "includes",
                                       "value": "input_guard"}]}}
        self.assertEqual(evaluate_predicate(pred, diag)[0], "xfail")

    def test_profile_per_case_membership(self) -> None:
        # The two `test_id`s below were copied from a `profile` spec's `tests.md`, which issue
        # #175 deleted along with the idea that a `profile` is executed at all. They are kept
        # verbatim rather than renamed because what this row is ABOUT is the evaluator's
        # per-case membership rule, which is kind-blind — but read them as arbitrary ids, not
        # as a `spec` this tree still carries.
        # profile: per-case checks + guard membership on component_compatibility.
        diag = {"cases": {
            "profile_select_default": {"checks": {"profile_selected": {"status": "pass"}},
                                       "verdict": {"overall": "pass", "failed_checks": []}},
            "profile_guard_incompatible_version": {
                "checks": {"component_compatibility": {"status": "fail"}},
                "verdict": {"overall": "fail", "failed_checks": ["component_compatibility"]}}}}
        preds = [
            {"test_id": "l0_select_default_profile_pass", "expected_outcome": "pass",
             "target_cases": ["profile_select_default"],
             "pass_when": {"all": [{"ref": "checks.profile_selected.status", "op": "eq",
                                    "value": "pass", "per_case": True},
                                   {"ref": "verdict.overall", "op": "eq", "value": "pass",
                                    "per_case": True}]}},
            {"test_id": "l0_guard_incompatible_component_version_xfail", "expected_outcome": "xfail",
             "target_cases": ["profile_guard_incompatible_version"],
             "pass_when": {"all": [{"ref": "verdict.overall", "op": "eq", "value": "fail",
                                    "per_case": True},
                                   {"ref": "verdict.failed_checks", "op": "includes",
                                    "value": "component_compatibility", "per_case": True}]}},
        ]
        doc = evaluate_verdict(preds, diag)
        self.assertEqual([p["status"] for p in doc["per_test"]], ["pass", "xfail"])

    def test_problem_nx_threshold_convergence_and_na(self) -> None:
        # shallow_water2d L1: per-case nx-dependent theoretical error thresholds, a
        # convergence-order check over a runner-emitted metric, per-case mass drift, and an
        # N/A (not-applied) momentum check.
        # Every metric — per-case and suite-level alike — is a flat dotted-address key of a
        # `metrics` map, exactly as the runner writes it.
        def case(cid: str, l2: float) -> dict:
            return {"case_id": cid, "metrics": {"cfl.max": 0.45, "metrics.mass_drift_rel": 1e-13,
                                                "errors.analytic_h.l2_rel_tend": l2}}

        diag = {"cases": [case("swe2d_ref_n032", 0.20), case("swe2d_ref_n064", 0.11),
                          case("swe2d_ref_n128", 0.06)],
                # convergence order derived by the runner (harness emits it under R1); referenced
                # as a top-level metric, pinned in diagnostics_contract.metrics.
                "metrics": {"convergence.n32_to_n64.analytic_h_order": 0.86,
                            "convergence.n64_to_n128.analytic_h_order": 0.87}}
        cases = ["swe2d_ref_n032", "swe2d_ref_n064", "swe2d_ref_n128"]
        pred = {"test_id": "l1_refinement_linear_wave", "expected_outcome": "pass",
                "target_cases": cases,
                "pass_when": {"all": [
                    {"ref": "cfl.max", "op": "le", "value": 1.0, "per_case": True},
                    {"ref": "metrics.mass_drift_rel", "op": "le", "value": 1e-10, "per_case": True},
                    {"ref": "errors.analytic_h.l2_rel_tend", "op": "le", "per_case": True,
                     "value": {"per_case": {"swe2d_ref_n032": 2.2e-1, "swe2d_ref_n064": 1.2e-1,
                                            "swe2d_ref_n128": 6.5e-2}}},
                    {"ref": "convergence.n32_to_n64.analytic_h_order", "op": "ge", "value": 0.80},
                    {"ref": "convergence.n64_to_n128.analytic_h_order", "op": "ge", "value": 0.80},
                    # momentum not applied for this profile -> na_allowed absorbs the null
                    {"ref": "metrics.momx_drift_rel", "op": "le", "value": 1e-10,
                     "per_case": True, "na_allowed": True}]}}
        status, kind, _ = evaluate_predicate(pred, diag)
        self.assertEqual((status, kind), ("pass", "pass"))

    def test_problem_cfl_guard_xfail(self) -> None:
        diag = {"cases": [{"case_id": "swe2d_guard", "cfl": {"max": 1.4},
                           "verdict": {"overall": "fail", "failed_checks": ["cfl"]}}]}
        pred = {"test_id": "l0_cfl_guard_xfail", "expected_outcome": "xfail",
                "target_cases": ["swe2d_guard"],
                "pass_when": {"all": [{"ref": "verdict.overall", "op": "eq", "value": "fail",
                                       "per_case": True},
                                      {"ref": "verdict.failed_checks", "op": "includes",
                                       "value": "cfl", "per_case": True}]}}
        self.assertEqual(evaluate_predicate(pred, diag)[0], "xfail")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class CheckRefLeafStatementSitesTest(unittest.TestCase):
    """Issue #269 (`atmofab-enforcement-change` rule 3-a): the leaf a `checks.<id>` predicate
    ref reads is defined ONCE, as `verdict_evaluator.CHECK_REF_LEAF`, and every document that
    states it is checked against the constant. Before #269 the compile-inlined phase contract
    offered `checks.<id>.pass|status`, a spelling half of which the runner never emits, and
    nothing compared the two: four certified IRs carried `.pass`.

    Each surface is read inside a window opened by an ANCHOR that precedes the statement and
    is byte-identical in the wording being refused (so restoring the old wording fails on the
    missing token, not on the anchor) and closed by the head of the next block; both are
    self-tested to occur exactly once, in order. The window must hold exactly one statement
    line, that line must carry the token derived from the constant, and the refused
    spellings must be absent FROM THE WINDOW — never tree-wide, because `checks.<x>.pass` is
    a legitimate preflight.json vocabulary in another namespace.

    Two of the three surfaces are READERS of the leaf (the phase contract, the generate
    template) and go red when their prose drifts from the code; the third is the PRODUCER
    (the certified harness spec, which the rendered runner carries verbatim) and goes red
    when the CONSTANT drifts from what the runner writes — the witness in the other
    direction."""

    _REPO = Path(verdict_evaluator.__file__).resolve().parents[1]

    # (surface, anchor, bound, statement-line marker, required token) — the two `{leaf}`
    # holes are filled from the constant at run time, never spelled here.
    _SURFACES: tuple[tuple[str, str, str, str, str], ...] = (
        ("docs/workflow/phases/phase_01_compile.md",
         "# test_predicates ref vocabulary (all resolvable at --stage compile):",
         "# condition scope — the three ways",
         "#   checks.<id>",
         "checks.<id>.{leaf}"),
        ("tools/prompt_templates/pure_generate_generate.txt",
         "(A) Author an honest `status` for each id.",
         "(B) `metric_compute(",
         "checks.<id>",
         "reads `checks.<id>.{leaf}`"),
        ("spec/infrastructure/infra/harness/harness_fortran_cpu/controlled_spec.md",
         "- **`diagnostics.json`** — a JSON object with a top-level `checks` object",
         "- **`perf.json`**",
         "diagnostics_contract.checks[].id",
         '{{ "{leaf}": "pass"|"fail" }}'),
    )
    _REFUSED: tuple[str, ...] = ("pass|status", "checks.<id>.pass", "checks.<id>...",
                                 "`checks.<id>`", "checks.<id> ")

    def _window(self, rel: str, anchor: str, bound: str) -> str:
        text = (self._REPO / rel).read_text(encoding="utf-8")
        self.assertEqual(text.count(anchor), 1,
                         f"{rel}: the anchor {anchor!r} occurs {text.count(anchor)} times, "
                         f"not once — this check would read a window it did not mean to")
        self.assertEqual(text.count(bound), 1,
                         f"{rel}: the bound {bound!r} occurs {text.count(bound)} times, not once")
        start, end = text.index(anchor), text.index(bound)
        self.assertLess(start, end, f"{rel}: the bound precedes the anchor")
        window = text[start:end]
        self.assertTrue(window.strip(), f"{rel}: the window is empty")
        self.assertLess(len(window), len(text), f"{rel}: the window is the whole file")
        return window

    @staticmethod
    def _statement(window: str, first_line: str) -> str:
        """The marker line and its hard-wrapped continuation: every following line whose
        indentation is deeper than the marker's (a Markdown / comment wrap), stopping at the
        first line that is not."""
        def indent(ln: str) -> int:  # past a comment marker, which the wrapped line repeats
            return len(ln) - len(ln.lstrip(" #"))
        lines = window.splitlines()
        i = lines.index(first_line)
        out = [first_line]
        for ln in lines[i + 1:]:
            if not ln.strip(" #") or indent(ln) <= indent(first_line):
                break
            out.append(ln)
        return "\n".join(out)

    def test_statement_reader_takes_the_wrap_and_stops_at_the_next_item(self) -> None:
        # Self-test of the continuation rule (rule 3-a: "read the STATEMENT, not the line —
        # prose WRAPS"; and not across an item boundary).
        window = ("  #   checks.<id>.status -> first\n"
                  "  #                        continued\n"
                  "  #   <metric address>  -> next item\n")
        stmt = self._statement(window, "  #   checks.<id>.status -> first")
        self.assertIn("continued", stmt)
        self.assertNotIn("next item", stmt)

    def test_surface_list_is_the_literal(self) -> None:
        # A loop over an emptied tuple asserts nothing and stays green.
        self.assertEqual({rel for rel, *_ in self._SURFACES},
                         {"docs/workflow/phases/phase_01_compile.md",
                          "tools/prompt_templates/pure_generate_generate.txt",
                          "spec/infrastructure/infra/harness/harness_fortran_cpu/controlled_spec.md"})
        for rel, *_ in self._SURFACES:
            self.assertTrue((self._REPO / rel).is_file(), f"{rel}: a surface the code names "
                            f"is not in the tree — a rename or move shrank this scan")

    def test_every_statement_site_names_the_status_leaf(self) -> None:
        for rel, anchor, bound, marker, token_tpl in self._SURFACES:
            with self.subTest(surface=rel):
                window = self._window(rel, anchor, bound)
                statements = [ln for ln in window.splitlines() if marker in ln]
                self.assertEqual(len(statements), 1,
                                 f"{rel}: expected exactly one statement line carrying "
                                 f"{marker!r} between the anchor and the bound, found "
                                 f"{len(statements)}: {statements}")
                token = token_tpl.format(leaf=CHECK_REF_LEAF)
                # On the STATEMENT, not anywhere in the window: round 1 planted a bare
                # `checks.<id>` statement with the token appended to an unrelated line of the
                # same window, and the window-wide read passed it. The statement is the marker
                # line plus its continuation lines (those up to the next line that opens an
                # item of the same block).
                self.assertIn(token, self._statement(window, statements[0]),
                              f"{rel}: the statement does not name the one leaf a `checks.<id>` "
                              f"ref may read — `checks.<id>.{CHECK_REF_LEAF}` "
                              f"(verdict_evaluator.CHECK_REF_LEAF); the code and the document "
                              f"must agree, and the code is canonical")
                for refused in self._REFUSED:
                    self.assertNotIn(refused, window,
                                     f"{rel}: the window still offers {refused!r}, a shape "
                                     f"`_check_ref` refuses at --stage compile")

    # The surfaces that state the VALUE vocabulary in the `"pass"|"fail"` spelling (the generate
    # template says it in Fortran-literal form inside clause (A) and is not coupled for it).
    _VALUE_SURFACES: tuple[str, ...] = (
        "docs/workflow/phases/phase_01_compile.md",
        "spec/infrastructure/infra/harness/harness_fortran_cpu/controlled_spec.md",
    )

    def test_value_vocabulary_sites_name_the_members_in_order(self) -> None:
        # Round 1 (issue #269): the gate now pins a status condition's value to
        # `CHECK_STATUS_VALUES`; the documents that spell the vocabulary are checked against
        # the constant, in the constant's order, inside the same anchored windows.
        token = "|".join(f'"{v}"' for v in CHECK_STATUS_VALUES)
        by_rel = {rel: (anchor, bound) for rel, anchor, bound, *_ in self._SURFACES}
        self.assertEqual(set(self._VALUE_SURFACES), set(by_rel) - {
            "tools/prompt_templates/pure_generate_generate.txt"})
        for rel in self._VALUE_SURFACES:
            with self.subTest(surface=rel):
                window = self._window(rel, *by_rel[rel])
                self.assertIn(token, window,
                              f"{rel}: the window does not spell the status values as {token} "
                              f"(verdict_evaluator.CHECK_STATUS_VALUES, in order)")

    def test_constant_is_what_the_gate_pins(self) -> None:
        # The constant the documents are coupled to is the one the gate reads: a `checks`
        # ref with exactly that tail passes, the same id with any other tail is refused with
        # the repair spelled from the constant.
        from tools.verdict_evaluator import _check_ref
        self.assertEqual(_check_ref("L", f"checks.g.{CHECK_REF_LEAF}", {"g"}, set(), set()), [])
        bad = _check_ref("L", "checks.g.other", {"g"}, set(), set())
        self.assertEqual(len(bad), 1, bad)
        self.assertIn(f"— write checks.g.{CHECK_REF_LEAF} compared by", bad[0])
