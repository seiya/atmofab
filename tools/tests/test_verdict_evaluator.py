#!/usr/bin/env python3
"""The host verdict (issue #582): a test's status is its `expected_outcome` when every predicate
record of the test holds, else `fail`, and `evaluate_verdict` re-checks on the records what the
Compile gate states. Records here are hand-built, and `test_primary_evidence` drives the same
function over records the evaluator produced from real captures."""
from __future__ import annotations

import unittest

from tools.verdict_evaluator import (
    PredicateError,
    _apply_op,
    _reduce_statuses,
    _resolve_value,
    evaluate_verdict,
    is_cross_target_record,
    validate_tests_schema,
)


def _test(tid: str = "t", outcome: str = "pass", cases: tuple[str, ...] = ("a",),
          quantities: tuple[str, ...] = ("q",)) -> dict:
    return {"test_id": tid, "expected_outcome": outcome, "target_cases": list(cases),
            "quantities": list(quantities)}


def _rec(tid: str = "t", q: str = "q", *, ok: bool = True, kind: str | None = None,
         cases: tuple[str, ...] = ("a",), pairing: dict | None = None) -> dict:
    return {"test_id": tid, "quantity": q, "satisfied": ok,
            "kind": kind or ("pass" if ok else "physics"), "target_cases": list(cases),
            "cases_read": list(cases), "comparands": [], "evaluated": [],
            "grammar_version": 6, "scalar_pairing": {} if pairing is None else pairing}


class OpsTest(unittest.TestCase):
    def test_ops(self) -> None:
        for lhs, op, rhs, want in [(1.0, "le", 1, True), (2, "le", 1, False), (1, "ge", 1.0, True),
                                   (0, "ge", 1, False), (1, "lt", 2, True), (2, "lt", 2, False),
                                   (2, "gt", 1, True), (1, "gt", 1, False), (1, "eq", 1.0, True),
                                   (1, "eq", 2, False), (1, "ne", 2, True), (1, "ne", 1, False)]:
            with self.subTest(lhs=lhs, op=op, rhs=rhs):
                self.assertIs(_apply_op(lhs, op, rhs), want)

    def test_bool_and_number_do_not_collide(self) -> None:
        self.assertFalse(_apply_op(True, "eq", 1))
        self.assertTrue(_apply_op(True, "ne", 1))
        self.assertTrue(_apply_op(True, "eq", True))

    def test_ordered_op_on_non_number_is_false(self) -> None:
        for lhs, rhs in [("a", 1), (1, "a"), (True, 1), (None, 1)]:
            self.assertFalse(_apply_op(lhs, "le", rhs))

    def test_unknown_op_raises(self) -> None:
        with self.assertRaises(PredicateError):
            _apply_op(1, "includes", 1)

    def test_per_case_value_map(self) -> None:
        self.assertEqual(_resolve_value({"per_case": {"a": 1}}, "a"), (True, 1))
        self.assertEqual(_resolve_value({"per_case": {"a": 1}}, "b"), (False, None))
        self.assertEqual(_resolve_value({"per_case": {"a": 1}}, None), (False, None))
        self.assertEqual(_resolve_value(3, "a"), (True, 3))
        with self.assertRaises(PredicateError):
            _resolve_value({"per_case": 3}, "a")


class VerdictTest(unittest.TestCase):
    def test_every_record_holds_gives_the_expected_outcome(self) -> None:
        doc = evaluate_verdict([_test("p"), _test("x", "xfail")],
                               [_rec("p"), _rec("x")], run_id="r", node_key="n")
        self.assertEqual([t["status"] for t in doc["per_test"]], ["pass", "xfail"])
        self.assertEqual((doc["self_verdict"], doc["failure_class"]), ("pass", "pass"))
        self.assertEqual((doc["run_id"], doc["node_key"]), ("r", "n"))

    def test_all_xfail_is_xfail(self) -> None:
        self.assertEqual(evaluate_verdict([_test("x", "xfail")], [_rec("x")])["self_verdict"],
                         "xfail")

    def test_an_unsatisfied_record_fails_the_test_whatever_it_expected(self) -> None:
        for outcome in ("pass", "xfail"):
            with self.subTest(outcome=outcome):
                doc = evaluate_verdict([_test(outcome=outcome)],
                                       [_rec(), _rec(q="q", ok=False)])
                self.assertEqual(doc["per_test"][0]["status"], "fail")
                self.assertEqual((doc["self_verdict"], doc["failure_class"]),
                                 ("fail", "physics_fail"))

    def test_structural_dominates_physics(self) -> None:
        doc = evaluate_verdict([_test("a"), _test("b")],
                               [_rec("a", ok=False), _rec("b", ok=False, kind="structural")])
        self.assertEqual(doc["failure_class"], "structural_violation")

    def test_structural_inside_one_test_dominates(self) -> None:
        doc = evaluate_verdict([_test()], [_rec(ok=False), _rec(ok=False, kind="structural")])
        self.assertEqual(doc["failure_class"], "structural_violation")

    def test_basis_is_the_records_and_nothing_else(self) -> None:
        recs = [_rec()]
        doc = evaluate_verdict([_test()], recs)
        self.assertEqual(doc["per_test"][0]["basis"], {"predicates": recs})

    def test_no_tests_fail_structurally_not_pass(self) -> None:
        for tests in ([], None, "x"):
            doc = evaluate_verdict(tests, [])
            self.assertEqual((doc["self_verdict"], doc["failure_class"], doc["per_test"]),
                             ("fail", "structural_violation", []))

    def test_malformed_entries_raise(self) -> None:
        for tests, recs in [([3], []), ([{"expected_outcome": "pass"}], []),
                            ([{"test_id": " ", "expected_outcome": "pass"}], []),
                            ([_test(outcome="fail")], [_rec()]), ([_test()], [3]),
                            ([_test()], [{"test_id": 3}])]:
            with self.subTest(tests=tests), self.assertRaises(PredicateError):
                evaluate_verdict(tests, recs)

    def test_a_test_with_no_record_raises(self) -> None:
        with self.assertRaisesRegex(PredicateError, "nothing judges it"):
            evaluate_verdict([_test(), _test("other")], [_rec()])

    def test_a_record_of_no_test_raises(self) -> None:
        with self.assertRaisesRegex(PredicateError, "no io_contract.tests entry"):
            evaluate_verdict([_test()], [_rec(), _rec("ghost")])

    def test_a_record_over_other_cases_raises(self) -> None:
        with self.assertRaisesRegex(PredicateError, "not the test's target_cases"):
            evaluate_verdict([_test(cases=("a", "b"))], [_rec(cases=("a",))])

    def test_a_target_case_no_record_values_raises(self) -> None:
        """Review round 1 F3: over [a, b], records whose `cases_read` holds a alone. Set-equal
        `target_cases` say nothing of what a record read, and a cross-target record reads no
        case for this rule; one own record reading b is enough."""
        tests = [_test(cases=("a", "b"))]

        def rec(*read: str) -> dict:
            return {**_rec(cases=("a", "b")), "cases_read": list(read)}
        with self.assertRaisesRegex(PredicateError, r"target case\(s\) \['b'\]"):
            evaluate_verdict(tests, [rec("a")])
        placeholder = {**CrossTargetTest.PLACEHOLDER, "target_cases": ["a", "b"],
                       "cases_read": ["a", "b"]}
        with self.assertRaisesRegex(PredicateError, r"target case\(s\) \['b'\]"):
            evaluate_verdict(tests, [rec("a"), placeholder])
        self.assertEqual(evaluate_verdict(tests, [rec("a"), rec("b")])["self_verdict"], "pass")


class CrossTargetTest(unittest.TestCase):
    PLACEHOLDER = {"test_id": "t", "quantity": "agree", "kind": "no_comparand", "satisfied": True,
                   "target_cases": ["a"], "comparands": [], "evaluated": [],
                   "scalar_pairing": {}}

    def test_classification(self) -> None:
        self.assertTrue(is_cross_target_record(self.PLACEHOLDER))
        self.assertTrue(is_cross_target_record({"comparands": [{"target_id": "x"}]}))
        self.assertFalse(is_cross_target_record(_rec()))
        self.assertFalse(is_cross_target_record({}))

    def test_a_placeholder_takes_no_part_in_the_status(self) -> None:
        doc = evaluate_verdict([_test()], [_rec(), self.PLACEHOLDER])
        self.assertEqual(doc["per_test"][0]["status"], "pass")
        doc = evaluate_verdict([_test()], [_rec(ok=False), self.PLACEHOLDER])
        self.assertEqual(doc["per_test"][0]["status"], "fail")

    def test_a_placeholder_alone_judges_nothing(self) -> None:
        with self.assertRaisesRegex(PredicateError, "nothing judges it"):
            evaluate_verdict([_test()], [self.PLACEHOLDER])

    def test_an_evaluated_comparison_is_refused_never_dropped(self) -> None:
        for bad in ({**self.PLACEHOLDER, "kind": "physics", "satisfied": False,
                     "comparands": [{"target_id": "x"}]},
                    {**self.PLACEHOLDER, "kind": "pass", "comparands": [{"target_id": "x"}],
                     "evaluated": [{"case": "a"}]},
                    {**self.PLACEHOLDER, "evaluated": [{"case": "a"}]},
                    {**self.PLACEHOLDER, "satisfied": False}):
            with self.subTest(bad=bad), self.assertRaisesRegex(PredicateError, "cross-target"):
                evaluate_verdict([_test()], [_rec(), bad])


class ScalarPairingTest(unittest.TestCase):
    """D7 (issue #589) re-checked on the records: each branch of `_check_scalar_pairing` has its
    own row, so a record the gate would have refused cannot reach a pass through a hand-edited
    IR. `scalar` is the cases a record reads the one-call scalar in, `paired` those it reads it
    in together with an array state its operation writes."""

    ALONE = {"a_x": {"scalar": ["a"], "paired": []}}
    BOTH = {"a_x": {"scalar": ["a"], "paired": ["a"]}}

    def _verdict(self, *recs: dict, cases: tuple[str, ...] = ("a",)) -> dict:
        return evaluate_verdict([_test(cases=cases)], list(recs))

    def test_paired_in_itself_or_beside_passes_and_each_half_decides(self) -> None:
        self.assertEqual(self._verdict(_rec(pairing=self.BOTH))["self_verdict"], "pass")
        self.assertEqual(self._verdict(_rec(pairing=self.ALONE), _rec(pairing=self.BOTH))
                         ["self_verdict"], "pass")
        for alone_ok, pair_ok in ((False, True), (True, False)):
            doc = self._verdict(_rec(pairing=self.ALONE, ok=alone_ok),
                                _rec(pairing=self.BOTH, ok=pair_ok))
            self.assertEqual(doc["per_test"][0]["status"], "fail")

    def test_a_scalar_read_alone_raises(self) -> None:
        with self.assertRaisesRegex(PredicateError, r"one-call scalar final\.a_x in \['a'\]"):
            self._verdict(_rec(pairing=self.ALONE))

    def test_a_record_reading_only_arrays_does_not_pair(self) -> None:
        """A record that reads only the arrays carries no entry for the scalar: no pair."""
        with self.assertRaisesRegex(PredicateError, "one-call scalar final.a_x"):
            self._verdict(_rec(pairing=self.ALONE), _rec(pairing={}))

    def test_another_quantity_does_not_pair(self) -> None:
        with self.assertRaisesRegex(PredicateError, "one-call scalar final.a_x"):
            self._verdict(_rec(pairing=self.ALONE), _rec(q="other", pairing=self.BOTH))

    def test_a_cross_target_record_does_not_pair(self) -> None:
        cross = {**CrossTargetTest.PLACEHOLDER, "quantity": "q", "scalar_pairing": self.BOTH}
        with self.assertRaisesRegex(PredicateError, "one-call scalar final.a_x"):
            self._verdict(_rec(pairing=self.ALONE), cross)

    def test_a_pair_over_fewer_cases_does_not_pair(self) -> None:
        with self.assertRaisesRegex(PredicateError, "one-call scalar final.a_x"):
            self._verdict(_rec(pairing={"a_x": {"scalar": ["a", "b"], "paired": []}},
                               cases=("a", "b")),
                          _rec(pairing={"a_x": {"scalar": ["a", "b"], "paired": ["a"]}},
                               cases=("a", "b")), cases=("a", "b"))

    def test_another_scalar_does_not_pair(self) -> None:
        with self.assertRaisesRegex(PredicateError, "one-call scalar final.a_x"):
            self._verdict(_rec(pairing=self.ALONE),
                          _rec(pairing={"a_y": {"scalar": ["a"], "paired": ["a"]}}))

    def test_a_record_without_the_map_is_refused(self) -> None:
        rec = _rec()
        del rec["scalar_pairing"]
        with self.assertRaisesRegex(PredicateError, "carries no scalar_pairing map"):
            self._verdict(rec)
        with self.assertRaisesRegex(PredicateError, "is not a mapping"):
            self._verdict(_rec(pairing={"a_x": ["a"]}))

    def test_a_record_needs_no_sources_read_any_more(self) -> None:
        """Grammar 6: the record lost `sources_read`; one without it is a current record."""
        self.assertNotIn("sources_read", _rec())
        self.assertEqual(self._verdict(_rec())["self_verdict"], "pass")


class VerdictRecordReadersTest(unittest.TestCase):
    """The audit readers of `verdict.json` `basis.predicates[]` read a record of either shape:
    one written before issue #589 (with `sources_read`, grammar 5) and one written after
    (without it, `grammar_version: 6`). The conductor's failure report is the reader here;
    `tools/judge_replay.py` reads no basis (`test_judge_replay`)."""

    def _doc(self, rec: dict) -> dict:
        return {"self_verdict": "fail", "failure_class": "physics_fail", "per_test": [
            {"test_id": "t", "status": "fail", "basis": {"predicates": [rec]}}]}

    def test_the_failure_report_reads_both_shapes(self) -> None:
        from tools.workflow_conductor import _verdict_failure_report
        failing = {"quantity": "q", "expr": "max(final.h)", "op": "le", "satisfied": False,
                   "evaluated": [{"case": "a", "value": 2.0, "rhs": 1.0, "satisfied": False}]}
        old = {**failing, "grammar_version": 5, "sources_read": {"operation": ["a"]}}
        new = {**failing, "grammar_version": 6, "scalar_pairing": {}}
        for rec in (old, new):
            with self.subTest(grammar=rec["grammar_version"]):
                report = _verdict_failure_report(self._doc(rec))
                self.assertIn("- test t: fail", report)
                self.assertIn("quantity='q' expr='max(final.h)' op='le' case='a' value=2.0 "
                              "rhs=1.0", report)


class ReduceTest(unittest.TestCase):
    def test_rule(self) -> None:
        for statuses, want in [(["pass"], "pass"), (["xfail"], "xfail"), (["pass", "xfail"], "pass"),
                               (["pass", "fail"], "fail"), (["xfail", "blocked"], "fail"),
                               (["skipped", "xfail"], "xfail"), (["skipped"], "pass")]:
            self.assertEqual(_reduce_statuses(statuses), want, statuses)


class SchemaTest(unittest.TestCase):
    def _v(self, tests: object, ids: tuple[str, ...] = ("t",)) -> list[str]:
        return validate_tests_schema(tests, case_ids={"a", "b"}, test_ids=list(ids))

    def test_valid(self) -> None:
        self.assertEqual(self._v([_test()]), [])
        self.assertEqual(self._v([_test(quantities=("q.r_1", "s"))]), [])

    def test_empty_or_not_a_list(self) -> None:
        for tests in ([], None, "x", {}):
            self.assertEqual(self._v(tests), ["io_contract.tests must be a non-empty list"])

    def test_each_refusal_names_its_field(self) -> None:
        for mutate, token in [
                ({"test_id": ""}, ".test_id"), ({"test_id": 3}, ".test_id"),
                ({"expected_outcome": "fail"}, ".expected_outcome"),
                ({"expected_outcome": True}, ".expected_outcome"),
                ({"target_cases": []}, ".target_cases"), ({"target_cases": "a"}, ".target_cases"),
                ({"target_cases": ["ghost"]}, "unknown case_id"),
                ({"target_cases": [3]}, "unknown case_id"),
                ({"quantities": []}, ".quantities"), ({"quantities": "q"}, ".quantities"),
                ({"quantities": ["Q"]}, "must match"), ({"quantities": [3]}, "must match"),
                ({"quantities": ["q", "q"]}, "duplicated")]:
            with self.subTest(mutate=mutate):
                v = self._v([{**_test(), **mutate}])
                self.assertTrue(any(token in m for m in v), v)

    def test_entry_not_a_mapping(self) -> None:
        self.assertTrue(any("must be a mapping" in m for m in self._v([3])))

    def test_id_set_equals_tests_md_both_ways(self) -> None:
        self.assertIn("missing tests from tests.md (['u'])", " ".join(self._v([_test()], ("t", "u"))))
        self.assertIn("unknown test_id not in tests.md (['t'])", " ".join(self._v([_test()], ("u",))))

    def test_duplicate_id(self) -> None:
        self.assertIn("duplicated test_id", " ".join(self._v([_test(), _test()])))


if __name__ == "__main__":
    unittest.main()
