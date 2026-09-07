"""Unit tests for `tools/raw_evidence_excerpt.py` (issue #169, Z3).

Two subjects. The CONTRACT readers are the expected-evidence matrix, which this module now
defines for two callers — the `--stage post_execute` gate and the judge's excerpt — so the
tests here pin the derivation and one test pins that the gate really reads THIS function
rather than a copy. The EXCERPT is the window a pure `validate.judge` leaf gets in place of
`raw/`; what it must do is summarize, and what it must never do is inline an array, so the
size relation is asserted on real-shaped input rather than described in a comment.

The excerpt is also total by contract: every defect in the evidence is a recorded fact, not
an exception, because an exception during context assembly means no judge runs at all on
exactly the runs that most need one. Each malformed-input test below therefore asserts a
RESULT, and the absence of a raise is the point of it.
"""

from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path

from tools import raw_evidence_excerpt as rex
from tools import validate_pipeline_semantics as vps


def _contract(**overrides) -> dict:
    base = {
        "test_evidence_requirements": [
            {"test_id": "t_a", "required_raw_variables": ["h", "dx"]},
        ],
        "test_predicates": [
            {"test_id": "t_a", "target_cases": ["case_a"]},
        ],
        "raw_requirements": {
            "required_evidence": [
                {"artifact": "metrics_basis.json", "required": True},
                {"artifact": "state_snapshots", "required": True, "min_samples": 1},
            ],
        },
    }
    base.update(overrides)
    return base


def _entry(case_id: str = "case_a", **overrides) -> dict:
    base = {"test_id": "t_a", "case_id": case_id, "h": [[1.0, 2.0], [3.0, 4.0]], "dx": 0.5}
    base.update(overrides)
    return base


class _RawDir:
    """A `raw/` directory built from documents, torn down with the test."""

    def __init__(self, case: unittest.TestCase) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        case.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "raw"
        self.path.mkdir()

    def write(self, rel: str, doc) -> Path:
        target = self.path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(doc if isinstance(doc, str) else json.dumps(doc),
                          encoding="utf-8")
        return target


class ExpectedMetricsBasisKeysTest(unittest.TestCase):
    def test_matrix_is_the_test_by_target_case_product(self):
        contract = _contract(test_predicates=[
            {"test_id": "t_a", "target_cases": ["case_a", "case_b"]}])
        keys = rex.expected_metrics_basis_keys(contract)
        self.assertEqual(keys.expected, {("t_a", "case_a"), ("t_a", "case_b")})
        self.assertEqual(keys.multi_target_tests, ["t_a"])
        self.assertEqual(keys.untargeted_tests, [])

    def test_a_test_with_no_target_cases_yields_no_rows_and_is_named(self):
        keys = rex.expected_metrics_basis_keys(_contract(test_predicates=[]))
        self.assertEqual(keys.expected, set())
        self.assertEqual(keys.untargeted_tests, ["t_a"])

    def test_a_test_declaring_no_variables_is_dropped(self):
        # The filter the gate's comment warns about: an empty `required_raw_variables` is not
        # a row here, and `_validate_test_evidence_requirements` refuses such an IR outright.
        contract = _contract(test_evidence_requirements=[
            {"test_id": "t_a", "required_raw_variables": []}])
        self.assertEqual(rex.expected_metrics_basis_keys(contract).test_requirements, {})

    def test_predicates_are_found_when_the_contract_is_still_nested(self):
        nested = {"test_evidence_requirements": _contract()["test_evidence_requirements"],
                  "io_contract": {"test_predicates": _contract()["test_predicates"]}}
        self.assertEqual(rex.expected_metrics_basis_keys(nested).expected,
                         {("t_a", "case_a")})

    def test_malformed_contract_yields_an_empty_matrix(self):
        for contract in ({}, {"test_evidence_requirements": "oops"},
                         {"test_evidence_requirements": [{"test_id": 7}]}):
            keys = rex.expected_metrics_basis_keys(contract)
            self.assertEqual((keys.expected, keys.test_requirements), (set(), {}))


class GateReadsThisMatrixTest(unittest.TestCase):
    """The gate and the excerpt must derive the matrix from ONE definition — that is why it
    moved here. Pinned by identity of the function object, not by comparing two outputs: two
    copies agree on every input until the day one of them is edited."""

    def test_post_execute_gate_imports_the_same_functions(self):
        self.assertIs(vps.expected_metrics_basis_keys, rex.expected_metrics_basis_keys)
        self.assertIs(vps._metrics_basis_variable_keys, rex.metrics_basis_variable_keys)
        self.assertIs(vps._METRICS_BASIS_NESTED_VARIABLE_FIELDS,
                      rex.METRICS_BASIS_NESTED_VARIABLE_FIELDS)
        self.assertIs(vps._METRICS_BASIS_BOOKKEEPING_KEYS,
                      rex.METRICS_BASIS_BOOKKEEPING_KEYS)
        self.assertIs(vps._contract_test_evidence_requirements,
                      rex.contract_test_evidence_requirements)
        self.assertIs(vps._normalize_raw_evidence_artifact,
                      rex.normalize_raw_evidence_artifact)
        # `test_id_to_case_ids` and `RAW_EVIDENCE_ALIASES` are NOT asserted here: the gate
        # no longer names either directly — it reaches them through the two functions above
        # — so importing them back would be an unused import, and pinning an import the
        # gate does not make is pinning nothing.


class CoverageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = _RawDir(self)

    def _coverage(self, metrics_basis, contract=None) -> dict:
        self.raw.write("metrics_basis.json", metrics_basis)
        return rex.raw_evidence_excerpt(self.raw.path, contract or _contract())["coverage"]

    def test_complete_coverage(self):
        cov = self._coverage({"per_test": [_entry()]})
        self.assertEqual(cov["expected"], [["t_a", "case_a"]])
        self.assertEqual(cov["present"], [["t_a", "case_a"]])
        self.assertEqual((cov["missing"], cov["unexpected"]), ([], []))
        self.assertEqual(cov["missing_required_variables"], [])

    def test_missing_row(self):
        contract = _contract(test_predicates=[
            {"test_id": "t_a", "target_cases": ["case_a", "case_b"]}])
        cov = self._coverage({"per_test": [_entry("case_a")]}, contract)
        self.assertEqual(cov["missing"], [["t_a", "case_b"]])
        self.assertEqual(cov["unexpected"], [])

    def test_unexpected_row(self):
        cov = self._coverage({"per_test": [_entry("case_a"), _entry("case_zzz")]})
        self.assertEqual(cov["unexpected"], [["t_a", "case_zzz"]])
        self.assertEqual(cov["missing"], [])

    def test_row_present_but_a_declared_variable_is_absent(self):
        # The finer question the gate does not ask of `metrics_basis.json`: the row exists and
        # the matrix is complete, and the evidence still cannot support the test.
        cov = self._coverage({"per_test": [{"test_id": "t_a", "case_id": "case_a",
                                            "h": [[1.0]]}]})
        self.assertEqual((cov["missing"], cov["unexpected"]), ([], []))
        self.assertEqual(cov["missing_required_variables"],
                         [{"test_id": "t_a", "case_id": "case_a",
                           "missing_variables": ["dx"]}])

    def test_a_nested_entry_is_read_the_way_the_gate_reads_it(self):
        # The gate honours the first present nesting field, so an entry that nests its
        # variables satisfies it — and the excerpt must agree, or the judge reports a
        # shortage the gate passed. Same definition, asserted through the excerpt.
        for field in rex.METRICS_BASIS_NESTED_VARIABLE_FIELDS:
            with self.subTest(field=field):
                cov = self._coverage({"per_test": [{
                    "test_id": "t_a", "case_id": "case_a",
                    field: {"h": [[1.0]], "dx": 0.5}}]})
                self.assertEqual(cov["missing_required_variables"], [])

    def test_bookkeeping_keys_are_not_counted_as_variables(self):
        cov = self._coverage({"per_test": [dict(_entry(), status="pass", notes="x")]})
        self.assertEqual(cov["missing_required_variables"], [])
        rows = rex.raw_evidence_excerpt(self.raw.path, _contract())["metrics_basis_arrays"]
        self.assertEqual(sorted(r["variable"] for r in rows), ["dx", "h"])

    def test_a_bookkeeping_key_cannot_satisfy_a_required_variable(self):
        # Both readers of the key rule are exercised: `metrics_basis_variable_keys` decides
        # the shortage, `_entry_variables` decides the rows, and each is a separate
        # spelling of the exclusion. A contract naming `status` as evidence is not a real
        # IR, but it is the only input that tells the two apart.
        contract = _contract(test_evidence_requirements=[
            {"test_id": "t_a", "required_raw_variables": ["status"]}])
        cov = self._coverage({"per_test": [{"test_id": "t_a", "case_id": "case_a",
                                            "status": "pass"}]}, contract)
        self.assertEqual(cov["missing_required_variables"],
                         [{"test_id": "t_a", "case_id": "case_a",
                           "missing_variables": ["status"]}])
        rows = rex.raw_evidence_excerpt(self.raw.path, contract)["metrics_basis_arrays"]
        self.assertEqual(rows, [])

    def test_an_unrecognized_wrapper_still_reads_as_a_shortage(self):
        # `values` is not a nesting field — the gate refuses it by name, and the excerpt
        # must show the judge the same shortage rather than silently accepting the wrapper.
        cov = self._coverage({"per_test": [{"test_id": "t_a", "case_id": "case_a",
                                            "values": {"h": [[1.0]], "dx": 0.5}}]})
        self.assertEqual(cov["missing_required_variables"],
                         [{"test_id": "t_a", "case_id": "case_a",
                           "missing_variables": ["dx", "h"]}])

    def test_deprecated_tests_object_form_is_read(self):
        cov = self._coverage({"tests": {"t_a": {"case_id": "case_a", "h": [[1.0]],
                                                "dx": 0.5}}})
        self.assertEqual(cov["present"], [["t_a", "case_a"]])

    def test_untargeted_and_multi_target_tests_are_carried_through(self):
        cov = self._coverage({"per_test": [_entry()]}, _contract(test_predicates=[]))
        self.assertEqual(cov["tests_without_target_cases"], ["t_a"])
        self.assertEqual(cov["unexpected"], [["t_a", "case_a"]])


class MalformedEvidenceIsAFactNotAnExceptionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = _RawDir(self)

    def _excerpt(self, contract=None) -> dict:
        return rex.raw_evidence_excerpt(self.raw.path, contract or _contract())

    def test_absent_raw_directory(self):
        missing = self.raw.path.parent / "no_such_raw"
        result = rex.raw_evidence_excerpt(missing, _contract())
        self.assertFalse(result["raw_dir_present"])
        self.assertIn("raw/metrics_basis.json: absent", result["problems"])
        self.assertEqual(result["coverage"]["missing"], [["t_a", "case_a"]])

    def test_unparseable_metrics_basis(self):
        self.raw.write("metrics_basis.json", "{not json")
        problems = self._excerpt()["problems"]
        self.assertTrue(any("not parseable as JSON" in p for p in problems))

    def test_metrics_basis_that_is_not_an_object(self):
        self.raw.write("metrics_basis.json", [1, 2])
        self.assertIn("raw/metrics_basis.json: not a JSON object", self._excerpt()["problems"])

    def test_metrics_basis_with_neither_container(self):
        self.raw.write("metrics_basis.json", {"rows": []})
        self.assertTrue(any("neither a `per_test` list nor a `tests` object" in p
                            for p in self._excerpt()["problems"]))

    def test_entry_without_a_case_id(self):
        self.raw.write("metrics_basis.json", {"per_test": [{"test_id": "t_a", "h": []}]})
        self.assertTrue(any("carries no case_id" in p for p in self._excerpt()["problems"]))

    def test_duplicated_entry(self):
        self.raw.write("metrics_basis.json", {"per_test": [_entry(), _entry()]})
        self.assertTrue(any("duplicated entry" in p for p in self._excerpt()["problems"]))

    def test_a_malformed_entry_does_not_hide_the_sound_ones(self):
        self.raw.write("metrics_basis.json",
                       {"per_test": ["a string", _entry()]})
        result = self._excerpt()
        self.assertEqual(result["coverage"]["present"], [["t_a", "case_a"]])
        self.assertTrue(result["problems"])

    def test_ragged_array_is_summarized_not_refused(self):
        self.raw.write("metrics_basis.json",
                       {"per_test": [_entry(h=[[1.0, 2.0], [3.0]])]})
        row = next(r for r in self._excerpt()["metrics_basis_arrays"]
                   if r["variable"] == "h")
        self.assertTrue(row["ragged"])
        self.assertEqual(row["count"], 3)

    def test_the_deepest_document_json_can_parse_is_summarized(self):
        # The walk is iterative, so the excerpt survives whatever nesting reaches it. The
        # bound is `json.loads` itself, which raises `RecursionError` around 1000 levels on
        # CPython 3.10 (measured), so 900 is the deepest input this module can ever be given
        # — and a deeper one arrives as a `problems` entry, never as a crash.
        depth = 900
        self.raw.write("metrics_basis.json",
                       '{"per_test": [{"test_id": "t_a", "case_id": "case_a", "h": '
                       + "[" * depth + "1.0" + "]" * depth + ', "dx": 0.5}]}')
        row = next(r for r in self._excerpt()["metrics_basis_arrays"]
                   if r["variable"] == "h")
        self.assertEqual((row["count"], row["numeric_count"]), (1, 1))
        self.assertEqual(len(row["shape"]), depth)


class ArraySummaryTest(unittest.TestCase):
    """What the judge is actually given about an array. Every branch of `_summarize`."""

    def setUp(self) -> None:
        self.raw = _RawDir(self)

    def _row(self, value, variable="h") -> dict:
        self.raw.write("metrics_basis.json", {"per_test": [_entry(**{variable: value})]})
        excerpt = rex.raw_evidence_excerpt(self.raw.path, _contract())
        return next(r for r in excerpt["metrics_basis_arrays"] if r["variable"] == variable)

    def test_shape_and_extent(self):
        row = self._row([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]])
        self.assertEqual(row["shape"], [2, 3])
        self.assertFalse(row["ragged"])
        self.assertEqual((row["count"], row["min"], row["max"]), (6, 1.0, 6.0))
        self.assertFalse(row["all_zero"])

    def test_all_zero(self):
        self.assertTrue(self._row([[0.0, 0.0], [0.0, -0.0]])["all_zero"])

    def test_all_zero_is_false_when_a_zero_array_also_carries_a_non_finite(self):
        # Zeros and a `nan` is not "all zero" — and it is the pair that names the defect,
        # so neither half may swallow the other.
        row = self._row([[0.0, float("nan")], [0.0, 0.0]])
        self.assertFalse(row["all_zero"])
        self.assertEqual(row["nan_count"], 1)

    def test_all_zero_is_false_when_a_zero_array_also_carries_a_non_number(self):
        self.assertFalse(self._row([[0.0, None]])["all_zero"])

    def test_nan_is_counted_and_never_emitted_as_a_bare_token(self):
        row = self._row([[1.0, float("nan")]])
        self.assertEqual(row["nan_count"], 1)
        self.assertFalse(row["all_zero"])
        # `json.dumps` would happily write a bare `NaN`, which no strict parser accepts and
        # which the leaf receives as a broken document.
        self.assertNotIn("NaN", json.dumps(row))

    def test_inf_is_counted(self):
        row = self._row([[float("inf"), float("-inf"), 1.0]])
        self.assertEqual(row["inf_count"], 2)
        self.assertEqual((row["min"], row["max"]), (1.0, 1.0))

    def test_an_array_of_only_non_finite_values_reports_no_extent(self):
        row = self._row([[float("nan"), float("inf")]])
        self.assertEqual((row["min"], row["max"], row["all_zero"]), (None, None, False))

    def test_non_numeric_leaves_are_counted_separately(self):
        row = self._row([["a", None, True, 1.0]])
        self.assertEqual((row["numeric_count"], row["non_numeric_count"]), (1, 3))

    def test_empty_array(self):
        row = self._row([])
        self.assertEqual((row["shape"], row["count"], row["min"]), ([0], 0, None))

    def test_scalar_keeps_its_value(self):
        row = self._row(0.25, variable="dx")
        self.assertEqual((row["kind"], row["value"]), ("scalar", 0.25))

    def test_non_finite_scalar_becomes_a_name(self):
        self.assertEqual(self._row(float("nan"), variable="dx")["value"], "nan")
        self.assertEqual(self._row(float("inf"), variable="dx")["value"], "inf")
        self.assertEqual(self._row(float("-inf"), variable="dx")["value"], "-inf")

    def test_long_text_is_truncated_and_its_length_kept(self):
        row = self._row("x" * 5000, variable="dx")
        self.assertEqual(row["length"], 5000)
        self.assertLess(len(row["value"]), 300)


class StateSnapshotsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = _RawDir(self)
        self.raw.write("metrics_basis.json", {"per_test": [_entry()]})

    def _snapshots(self) -> dict:
        return rex.raw_evidence_excerpt(self.raw.path, _contract())["state_snapshots"]

    def test_absent_directory(self):
        self.assertEqual(self._snapshots(),
                         {"present": False, "schema": None, "cases": []})

    def test_case_is_summarized_against_the_declared_shape(self):
        self.raw.write("state_snapshots/snapshot_schema.json", {
            "variables": [{"name": "h", "shape_expr": "[nx, ny]"},
                          {"name": "z_b", "shape_expr": "[nx, ny]"}],
            "time_variable": "t", "min_samples": 1})
        self.raw.write("state_snapshots/case_a.json",
                       {"h": [[1.0, 2.0]], "t": 0.5})
        snapshots = self._snapshots()
        self.assertTrue(snapshots["present"])
        self.assertEqual(snapshots["schema"]["time_variable"], "t")
        case = snapshots["cases"][0]
        self.assertEqual(case["case_id"], "case_a")
        self.assertEqual(case["time_value"]["value"], 0.5)
        variable = case["variables"][0]
        self.assertEqual((variable["name"], variable["declared_shape_expr"],
                          variable["shape"]), ("h", "[nx, ny]", [1, 2]))
        # A variable the schema declares and the snapshot omits is the run's fact, not a
        # missing key in the excerpt.
        self.assertEqual(case["declared_variables_absent"], ["z_b"])

    def test_an_unreadable_snapshot_is_recorded_as_unread(self):
        self.raw.write("state_snapshots/case_a.json", "{broken")
        case = self._snapshots()["cases"][0]
        self.assertFalse(case["read"])
        self.assertEqual(case["variables"], [])

    def test_cases_are_ordered(self):
        for name in ("case_c", "case_a", "case_b"):
            self.raw.write(f"state_snapshots/{name}.json", {"h": [[1.0]]})
        self.assertEqual([c["case_id"] for c in self._snapshots()["cases"]],
                         ["case_a", "case_b", "case_c"])


class RequiredEvidenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = _RawDir(self)
        self.raw.write("metrics_basis.json", {"per_test": [_entry()]})

    def _rows(self, contract=None) -> list[dict]:
        return rex.raw_evidence_excerpt(
            self.raw.path, contract or _contract())["required_evidence"]

    def test_presence_of_each_declared_artifact(self):
        rows = {row["artifact"]: row for row in self._rows()}
        self.assertTrue(rows["metrics_basis.json"]["present"])
        self.assertFalse(rows["state_snapshots"]["present"])
        self.assertTrue(rows["state_snapshots"]["required"])

    def test_a_directory_artifact_counts_as_present(self):
        self.raw.write("state_snapshots/case_a.json", {"h": [[1.0]]})
        rows = {row["artifact"]: row for row in self._rows()}
        self.assertTrue(rows["state_snapshots"]["present"])

    def test_an_alias_spelling_resolves_and_keeps_what_the_ir_wrote(self):
        contract = _contract(raw_requirements={"required_evidence": [
            {"artifact": "raw/metrics_basis.json", "required": True}]})
        row = self._rows(contract)[0]
        self.assertEqual((row["declared_as"], row["artifact"]),
                         ("raw/metrics_basis.json", "metrics_basis.json"))

    def test_an_unrecognized_artifact_is_reported_rather_than_dropped(self):
        contract = _contract(raw_requirements={"required_evidence": [
            {"artifact": "telemetry.json", "required": True}]})
        row = self._rows(contract)[0]
        self.assertEqual((row["declared_as"], row["artifact"], row["present"]),
                         ("telemetry.json", None, False))

    def test_required_false_is_carried(self):
        contract = _contract(raw_requirements={"required_evidence": [
            {"artifact": "execution_trace.json", "required": False}]})
        self.assertFalse(self._rows(contract)[0]["required"])


class ExcerptIsBoundedTest(unittest.TestCase):
    """The whole reason this module exists: `raw/` is 13,023,908 B on `shallow_water2d` and
    cannot be inlined. The relation is asserted on input of the real shape, not described."""

    def test_no_numeric_array_survives_into_the_excerpt(self):
        raw = _RawDir(self)
        # 4 variables x 128 x 128 floats, twice — the shape of one real metrics-basis entry.
        array = [[float(i * j) for j in range(128)] for i in range(128)]
        entries = [{"test_id": "t_a", "case_id": case, "h": array, "hu": array,
                    "hv": array, "dx": 0.5}
                   for case in ("case_a", "case_b")]
        metrics_basis = raw.write("metrics_basis.json", {"per_test": entries})
        raw.write("state_snapshots/snapshot_schema.json",
                  {"variables": [{"name": "h", "shape_expr": "[nx, ny]"}],
                   "time_variable": "t"})
        snapshot = raw.write("state_snapshots/case_a.json", {"h": array, "t": 1.0})

        contract = _contract(test_evidence_requirements=[
            {"test_id": "t_a", "required_raw_variables": ["h", "hu", "hv", "dx"]}],
            test_predicates=[{"test_id": "t_a",
                              "target_cases": ["case_a", "case_b"]}])
        rendered = json.dumps(rex.raw_evidence_excerpt(raw.path, contract), indent=2)

        source_bytes = metrics_basis.stat().st_size + snapshot.stat().st_size
        self.assertGreater(source_bytes, 500_000)
        self.assertLess(len(rendered), source_bytes / 100)
        # The row count is the bound's shape: (entry x variable) + (snapshot x variable).
        excerpt = json.loads(rendered)
        self.assertEqual(len(excerpt["metrics_basis_arrays"]), 8)
        # And the arrays themselves are gone: a value from inside one appears nowhere.
        self.assertNotIn("8128.0", rendered)

    def test_output_is_json_serializable_and_deterministic(self):
        raw = _RawDir(self)
        raw.write("metrics_basis.json", {"per_test": [_entry(h=[[float("nan"), 1.0]])]})
        raw.write("state_snapshots/case_a.json", {"h": [[math.inf]], "t": 0.0})
        first = json.dumps(rex.raw_evidence_excerpt(raw.path, _contract()),
                           allow_nan=False, sort_keys=False)
        second = json.dumps(rex.raw_evidence_excerpt(raw.path, _contract()),
                            allow_nan=False, sort_keys=False)
        self.assertEqual(first, second)

    def test_policy_version_is_stamped(self):
        raw = _RawDir(self)
        raw.write("metrics_basis.json", {"per_test": [_entry()]})
        self.assertEqual(
            rex.raw_evidence_excerpt(raw.path, _contract())["policy_version"],
            rex.RAW_EXCERPT_POLICY_VERSION)
        self.assertIsInstance(rex.RAW_EXCERPT_POLICY_VERSION, int)


if __name__ == "__main__":
    unittest.main()
