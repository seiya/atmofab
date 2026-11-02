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
         cases: tuple[str, ...] = ("a",), read: dict | None = None) -> dict:
    return {"test_id": tid, "quantity": q, "satisfied": ok,
            "kind": kind or ("pass" if ok else "physics"), "target_cases": list(cases),
            "cases_read": list(cases), "comparands": [], "evaluated": [],
            "sources_read": {"operation": list(cases)} if read is None else read}


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
                   "sources_read": {"operation": ["a"]}}

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


class ReportedPairingTest(unittest.TestCase):
    """D13 re-checked on the records: each branch of `_check_reported_pairing` has its own row,
    so a record the gate would have refused cannot reach a pass through a hand-edited IR."""

    REPORTED = {"reported": ["a"]}
    OP = {"operation": ["a"]}

    def _verdict(self, *recs: dict, cases: tuple[str, ...] = ("a",)) -> dict:
        return evaluate_verdict([_test(cases=cases)], list(recs))

    def test_paired_passes_and_each_half_decides(self) -> None:
        self.assertEqual(self._verdict(_rec(read=self.REPORTED), _rec(read=self.OP))
                         ["per_test"][0]["status"], "pass")
        for reported_ok, state_ok in ((False, True), (True, False)):
            doc = self._verdict(_rec(read=self.REPORTED, ok=reported_ok),
                                _rec(read=self.OP, ok=state_ok))
            self.assertEqual(doc["per_test"][0]["status"], "fail")

    def test_alone_raises(self) -> None:
        with self.assertRaisesRegex(PredicateError, "source: reported"):
            self._verdict(_rec(read=self.REPORTED))

    def test_a_record_does_not_pair_with_itself(self) -> None:
        with self.assertRaisesRegex(PredicateError, "source: reported"):
            self._verdict(_rec(read={"reported": ["a"], "operation": ["a"]}))

    def test_two_records_reading_both_do_not_pair(self) -> None:
        both = {"reported": ["a"], "operation": ["a"]}
        with self.assertRaisesRegex(PredicateError, "source: reported"):
            self._verdict(_rec(read=both), _rec(read=both))

    def test_another_quantity_does_not_pair(self) -> None:
        with self.assertRaisesRegex(PredicateError, "source: reported"):
            self._verdict(_rec(read=self.REPORTED), _rec(q="other", read=self.OP))

    def test_a_cross_target_record_does_not_pair(self) -> None:
        cross = {**CrossTargetTest.PLACEHOLDER, "quantity": "q"}
        with self.assertRaisesRegex(PredicateError, "source: reported"):
            self._verdict(_rec(read=self.REPORTED), cross, _rec(q="z"))

    def test_a_record_or_reference_read_does_not_pair(self) -> None:
        for read in ({"record": ["a"]}, {"reference": ["a"]}, {}):
            with self.subTest(read=read), self.assertRaisesRegex(PredicateError, "reported"):
                self._verdict(_rec(read=self.REPORTED), _rec(read=read))

    def test_a_pair_over_fewer_cases_does_not_pair(self) -> None:
        with self.assertRaisesRegex(PredicateError, "source: reported"):
            self._verdict(_rec(read={"reported": ["a", "b"]}, cases=("a", "b")),
                          _rec(read={"operation": ["a"]}, cases=("a", "b")), cases=("a", "b"))

    def test_record_and_reference_reads_need_no_pair(self) -> None:
        for read in ({"record": ["a"]}, {"reference": ["a"], "operation": ["a"]}):
            with self.subTest(read=read):
                self.assertEqual(self._verdict(_rec(read=read))["self_verdict"], "pass")

    def test_a_record_without_the_map_is_refused(self) -> None:
        rec = _rec()
        del rec["sources_read"]
        with self.assertRaisesRegex(PredicateError, "sources_read"):
            self._verdict(rec)


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
