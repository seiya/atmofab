"""Unit tests for `tools/raw_evidence_excerpt.py` (issue #169, Z3; issue #582).

The EXCERPT is the window a `validate.judge` leaf gets in place of `raw/`; what it must do is
summarize, and what it must never do is inline an array, so the size relation is asserted on
real-shaped input rather than described in a comment. One row pins that the gate reads THIS
module's artifact spelling rather than a copy.

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
        "tests": [
            {"test_id": "t_a", "expected_outcome": "pass", "target_cases": ["case_a"],
             "quantities": ["q"]},
        ],
        "raw_requirements": {
            "required_evidence": [
                {"artifact": "state_snapshots", "required": True,
                 "schema": {"variables": [
                     {"name": "h", "shape_expr": "[nx, ny]"},
                     {"name": "cfl_run_max", "shape_expr": "scalar"},
                 ]}},
            ],
        },
    }
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


class GateReadsThisSpellingTest(unittest.TestCase):
    """The gate and the excerpt read one definition of an artifact's spelling. Pinned by identity
    of the function object: two copies agree on every input until the day one is edited."""

    def test_post_execute_gate_imports_the_same_function(self):
        self.assertIs(vps._normalize_raw_evidence_artifact,
                      rex.normalize_raw_evidence_artifact)


class _SnapshotRow:
    """Read one variable's summary off a snapshot written for the row."""

    raw: _RawDir

    def _row(self, value, variable="h", contract=None) -> dict:
        self.raw.write("state_snapshots/case_a.json", {variable: value})
        excerpt = rex.raw_evidence_excerpt(self.raw.path, contract or _contract())
        return next(r for r in excerpt["state_snapshots"]["cases"][0]["variables"]
                    if r["name"] == variable)


class MalformedEvidenceIsAFactNotAnExceptionTest(_SnapshotRow, unittest.TestCase):
    def setUp(self) -> None:
        self.raw = _RawDir(self)

    def _excerpt(self, contract=None) -> dict:
        return rex.raw_evidence_excerpt(self.raw.path, contract or _contract())

    def test_absent_raw_directory(self):
        missing = self.raw.path.parent / "no_such_raw"
        result = rex.raw_evidence_excerpt(missing, _contract())
        self.assertFalse(result["raw_dir_present"])
        self.assertEqual(result["state_snapshots"],
                         {"present": False, "schema": None, "cases": []})
        self.assertEqual([r["present"] for r in result["required_evidence"]], [False])

    def test_unparseable_snapshot(self):
        self.raw.write("state_snapshots/case_a.json", "{not json")
        problems = self._excerpt()["problems"]
        self.assertTrue(any("not parseable as JSON" in p for p in problems))

    def test_snapshot_that_is_not_an_object(self):
        self.raw.write("state_snapshots/case_a.json", [1, 2])
        self.assertIn("raw/state_snapshots/case_a.json: not a JSON object",
                      self._excerpt()["problems"])

    def test_a_malformed_snapshot_does_not_hide_the_sound_ones(self):
        self.raw.write("state_snapshots/case_a.json", "{not json")
        self.raw.write("state_snapshots/case_b.json", {"h": [[1.0]]})
        result = self._excerpt()
        cases = {c["case_id"]: c for c in result["state_snapshots"]["cases"]}
        self.assertFalse(cases["case_a"]["read"])
        self.assertEqual([v["name"] for v in cases["case_b"]["variables"]], ["h"])
        self.assertTrue(result["problems"])

    def test_raggedness_under_a_later_branch_is_detected(self):
        """Codex, round 2: `_shape` descended through `children[0]` only, so raggedness under
        any later sibling was invisible — `[[[1,2],[3,4]], [[5],[6,7,8]]]` reported shape
        `[2,2,2]` with `ragged=False`, and its element count matched the rectangular shape too,
        so nothing in the summary betrayed it. Malformed evidence reached the judge looking
        structurally sound, which is the one thing these summaries exist to prevent."""
        row = self._row([[[1.0, 2.0], [3.0, 4.0]], [[5.0], [6.0, 7.0, 8.0]]])
        self.assertTrue(row["ragged"])

    def test_a_rank_change_partway_across_a_level_is_ragged(self):
        """The other direction of the same walk: a level whose members are some lists and some
        scalars changes rank partway, which no length comparison alone would catch."""
        row = self._row([[1.0, [2.0]]])
        self.assertTrue(row["ragged"])

    def test_a_rectangular_array_is_not_called_ragged(self):
        """The over-refusal direction of the fix above: widening raggedness detection must not
        start flagging the ordinary shape, which is every real array in the corpus."""
        row = self._row([[[1.0, 2.0], [3.0, 4.0]], [[5.0, 6.0], [7.0, 8.0]]])
        self.assertFalse(row["ragged"])
        self.assertEqual(row["shape"], [2, 2, 2])

    def test_ragged_array_is_summarized_not_refused(self):
        row = self._row([[1.0, 2.0], [3.0]])
        self.assertTrue(row["ragged"])
        self.assertEqual(row["count"], 3)

    def test_the_deepest_document_json_can_parse_is_summarized(self):
        # The walk is iterative, so the excerpt survives whatever nesting reaches it. The
        # bound is `json.loads` itself, which raises `RecursionError` around 1000 levels on
        # CPython 3.10 (measured), so 900 is the deepest input this module can ever be given
        # — and a deeper one arrives as a `problems` entry, never as a crash.
        depth = 900
        self.raw.write("state_snapshots/case_a.json",
                       '{"h": ' + "[" * depth + "1.0" + "]" * depth + ', "dx": 0.5}')
        row = next(r for r in self._excerpt()["state_snapshots"]["cases"][0]["variables"]
                   if r["name"] == "h")
        self.assertEqual((row["count"], row["numeric_count"]), (1, 1))
        self.assertEqual(len(row["shape"]), depth)


class ArraySummaryTest(_SnapshotRow, unittest.TestCase):
    """What the judge is actually given about an array. Every branch of `_summarize`."""

    def setUp(self) -> None:
        self.raw = _RawDir(self)

    def test_shape_and_extent(self):
        row = self._row([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]])
        self.assertEqual(row["shape"], [2, 3])
        self.assertFalse(row["ragged"])
        self.assertEqual((row["count"], row["min"], row["max"]), (6, 1.0, 6.0))
        self.assertFalse(row["all_zero"])

    def test_all_zero(self):
        self.assertTrue(self._row([[0.0, 0.0], [0.0, -0.0]])["all_zero"])

    def test_a_uniformly_negative_array_is_not_all_zero(self):
        """The sign direction, which the rest of this family could not distinguish.

        MEASURED: a mutant clearing `all_zero` only for `number > 0.0` survived the whole
        suite, because every other probe here uses zeros or a POSITIVE array — inputs on which
        `!= 0.0` and `> 0.0` agree. The corpus has the distinguishing input: `flux_dif` on
        `l0_linear_state_diff_flux_pass` is uniformly negative in three recorded certified-pass
        pipelines, so the mutant would have asserted `all_zero: true` about real passing
        evidence, and the template spends its longest carve-out on what that flag means."""
        row = self._row([[-0.025, -0.0249], [-0.0251, -0.025]])
        self.assertFalse(row["all_zero"])
        self.assertLess(row["max"], 0.0)

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

    def _snapshots(self) -> dict:
        return rex.raw_evidence_excerpt(self.raw.path, _contract())["state_snapshots"]

    def test_absent_directory(self):
        self.assertEqual(self._snapshots(),
                         {"present": False, "schema": None, "cases": []})

    def test_a_recorded_min_samples_is_not_echoed(self):
        """Issue #396: `snapshot_schema.json` written before the key left the contract still
        carries `min_samples`; the excerpt's `schema` does not copy it."""
        self.raw.write("state_snapshots/snapshot_schema.json", {
            "variables": [{"name": "h", "shape_expr": "[nx, ny]"}],
            "time_variable": "t", "min_samples": 24})
        self.raw.write("state_snapshots/case_a.json", {"h": [[1.0, 2.0]], "t": 0.5})
        self.assertEqual(set(self._snapshots()["schema"]),
                         {"time_variable", "declared_variables"})

    def test_case_is_summarized_against_the_declared_shape(self):
        self.raw.write("state_snapshots/snapshot_schema.json", {
            "variables": [{"name": "h", "shape_expr": "[nx, ny]"},
                          {"name": "z_b", "shape_expr": "[nx, ny]"}],
            "time_variable": "t"})
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
        self.assertIsNone(case["initial"])

    def test_the_initial_capture_is_summarized_beside_the_final_one(self):
        # Z6 (issue #255): a host-rendered runner writes `initial/<case_id>.json` right after
        # `case_setup`; the excerpt carries it under the case's `initial` key, summarized by
        # the same rules, so the judge sees both capture points. A case with no initial
        # capture (a harness self-test) carries `initial: None`.
        self.raw.write("state_snapshots/snapshot_schema.json", {
            "variables": [{"name": "h", "shape_expr": "[nx, ny]"}],
            "time_variable": "t"})
        self.raw.write("state_snapshots/case_a.json", {"h": [[3.0, 4.0]], "t": 1.0})
        self.raw.write("state_snapshots/initial/case_a.json", {"h": [[1.0, 2.0]], "t": 0.0})
        self.raw.write("state_snapshots/case_b.json", {"h": [[5.0, 6.0]], "t": 1.0})
        cases = {c["case_id"]: c for c in self._snapshots()["cases"]}
        # the `initial/` files are not cases of their own
        self.assertEqual(sorted(cases), ["case_a", "case_b"])
        a = cases["case_a"]
        self.assertEqual(a["time_value"]["value"], 1.0)
        self.assertEqual(a["variables"][0]["max"], 4.0)
        self.assertTrue(a["initial"]["read"])
        self.assertEqual(a["initial"]["case_id"], "case_a")
        self.assertEqual(a["initial"]["time_value"]["value"], 0.0)
        self.assertEqual(a["initial"]["variables"][0]["max"], 2.0)
        self.assertEqual(a["initial"]["variables"][0]["declared_shape_expr"], "[nx, ny]")
        self.assertIsNone(cases["case_b"]["initial"])
        # an unreadable initial capture is a problem the excerpt names
        self.raw.write("state_snapshots/initial/case_b.json", "{broken")
        excerpt = rex.raw_evidence_excerpt(self.raw.path, _contract())
        b = next(c for c in excerpt["state_snapshots"]["cases"] if c["case_id"] == "case_b")
        self.assertFalse(b["initial"]["read"])
        self.assertTrue(any("initial/case_b.json" in p for p in excerpt["problems"]),
                        excerpt["problems"])

    def test_no_row_names_a_source_and_step_is_an_ordinary_row(self):
        """Policy 4 (issue #589 PR-1b): the `source` vocabulary is gone, wherever a stale IR still
        spells it, and `step` — the driver's own column, declared by no schema — is summarized
        like any other name, with no declared shape."""
        self.raw.write("state_snapshots/snapshot_schema.json", {
            "variables": [{"name": "h", "shape_expr": "[nx, ny]"},
                          {"name": "cfl_run_max", "shape_expr": "scalar"}],
            "time_variable": "t"})
        self.raw.write("state_snapshots/case_a.json",
                       {"h": [[1.0]], "cfl_run_max": 0.5, "step": 4, "t": 1.0})
        self.raw.write("state_snapshots/initial/case_a.json",
                       {"h": [[1.0]], "cfl_run_max": 0.0, "step": 0, "t": 0.0})
        contract = _contract(raw_requirements={"required_evidence": [
            {"artifact": "state_snapshots", "required": True, "schema": {"variables": [
                {"name": "h", "shape_expr": "[nx, ny]", "source": "reported"}]}}]})
        snapshots = rex.raw_evidence_excerpt(self.raw.path, contract)["state_snapshots"]
        self.assertEqual(snapshots["schema"]["declared_variables"], [
            {"name": "cfl_run_max", "shape_expr": "scalar"},
            {"name": "h", "shape_expr": "[nx, ny]"}])
        case = snapshots["cases"][0]
        for rows in (case["variables"], case["initial"]["variables"]):
            self.assertEqual(sorted(v["name"] for v in rows), ["cfl_run_max", "h", "step"])
            for variable in rows:
                self.assertNotIn("source", variable)
            step = next(v for v in rows if v["name"] == "step")
            self.assertIsNone(step["declared_shape_expr"])
        self.assertEqual(next(v for v in case["variables"] if v["name"] == "step")["value"], 4)
        # `step` is no declared variable that went missing.
        self.assertEqual(case["declared_variables_absent"], [])

    def _case(self, case_id: str = "case_a") -> dict:
        self.raw.write("state_snapshots/snapshot_schema.json", {
            "variables": [{"name": "h", "shape_expr": "[nx, ny]"}], "time_variable": "t"})
        self.raw.write(f"state_snapshots/{case_id}.json", {"h": [[1.0]], "t": 1.0})
        return next(c for c in self._snapshots()["cases"] if c["case_id"] == case_id)

    def test_history_samples_counts_this_case_s_files_alone(self):
        self.assertEqual(self._case()["history_samples"], 0)    # no `history/` at all
        for rel in ("case_a__0.json", "case_a__2.json", "case_a__10.json"):
            self.raw.write(f"state_snapshots/history/{rel}", {"h": [[1.0]], "t": 0.0})
        self.assertEqual(self._case()["history_samples"], 3)
        # Neither another case's sample, nor a name of another shape, nor a directory.
        for rel in ("case_b__1.json", "case_a__x.json", "case_a__1.txt", "case_a.json",
                    "case_a__.json", "xcase_a__1.json"):
            self.raw.write(f"state_snapshots/history/{rel}", {"h": [[1.0]], "t": 0.0})
        (self.raw.path / "state_snapshots" / "history" / "case_a__7.json").mkdir()
        self.assertEqual(self._case()["history_samples"], 3)
        # A case whose id extends another's reads its own (`case_a__b` holds `case_a__`'s prefix).
        self.raw.write("state_snapshots/case_a__b.json", {"h": [[1.0]], "t": 1.0})
        self.raw.write("state_snapshots/history/case_a__b__1.json", {"h": [[1.0]], "t": 0.0})
        self.assertEqual(self._case("case_a__b")["history_samples"], 1)
        self.assertEqual(self._case()["history_samples"], 3)

    def test_series_summarizes_each_name_and_is_none_without_a_file(self):
        self.assertIsNone(self._case()["series"])
        self.raw.write("state_snapshots/series/case_a.json",
                       '{"t": 2.0, "step": [1, 2, 3], "h_max": [1.0, 5.0, NaN]}')
        series = self._case()["series"]
        self.assertEqual(sorted(series), ["h_max", "step", "t"])
        self.assertEqual(series["step"]["shape"], [3])
        self.assertEqual((series["step"]["min"], series["step"]["max"]), (1, 3))
        self.assertEqual(series["t"]["value"], 2.0)
        # A non-finite entry is counted, and none is inlined.
        self.assertEqual((series["h_max"]["nan_count"], series["h_max"]["max"]), (1, 5.0))
        # Another case's file is not this one's.
        self.raw.write("state_snapshots/case_b.json", {"h": [[1.0]], "t": 1.0})
        self.assertIsNone(self._case("case_b")["series"])

    def test_a_series_that_is_no_object_or_no_json_is_a_problem_and_no_summary(self):
        for text, needle in (("[1]", "raw/state_snapshots/series/case_a.json"),
                             ("{", "raw/state_snapshots/series/case_a.json")):
            with self.subTest(text=text):
                self.raw.write("state_snapshots/series/case_a.json", text)
                self.assertIsNone(self._case()["series"])
        self.raw.write("state_snapshots/series/case_a.json", "{")
        problems = rex.raw_evidence_excerpt(self.raw.path, _contract())["problems"]
        self.assertTrue(any("raw/state_snapshots/series/case_a.json" in p for p in problems),
                        problems)

    def test_a_series_or_a_sample_is_not_read_as_a_final_capture(self):
        self._case()
        self.raw.write("state_snapshots/history/case_a__1.json", {"h": [[1.0]], "t": 0.0})
        self.raw.write("state_snapshots/series/case_a.json", {"t": 1.0, "step": [1]})
        self.assertEqual([c["case_id"] for c in self._snapshots()["cases"]], ["case_a"])


class RequiredEvidenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = _RawDir(self)

    def _rows(self, contract=None) -> list[dict]:
        return rex.raw_evidence_excerpt(
            self.raw.path, contract or _contract())["required_evidence"]

    def test_presence_of_each_declared_artifact(self):
        rows = {row["artifact"]: row for row in self._rows()}
        self.assertFalse(rows["state_snapshots"]["present"])
        self.assertTrue(rows["state_snapshots"]["required"])

    def test_a_directory_artifact_counts_as_present(self):
        self.raw.write("state_snapshots/case_a.json", {"h": [[1.0]]})
        rows = {row["artifact"]: row for row in self._rows()}
        self.assertTrue(rows["state_snapshots"]["present"])

    def test_an_alias_spelling_resolves_and_keeps_what_the_ir_wrote(self):
        contract = _contract(raw_requirements={"required_evidence": [
            {"artifact": "raw/state_snapshots/", "required": True}]})
        row = self._rows(contract)[0]
        self.assertEqual((row["declared_as"], row["artifact"]),
                         ("raw/state_snapshots/", "state_snapshots"))

    def test_an_unrecognized_artifact_is_reported_rather_than_dropped(self):
        contract = _contract(raw_requirements={"required_evidence": [
            {"artifact": "telemetry.json", "required": True}]})
        row = self._rows(contract)[0]
        self.assertEqual((row["declared_as"], row["artifact"], row["present"]),
                         ("telemetry.json", None, False))

    def _drive_rows(self) -> dict:
        contract = _contract(raw_requirements={"required_evidence": [
            {"artifact": "state_snapshots", "required": True},
            {"artifact": "state_history", "required": True},
            {"artifact": "step_series", "required": True}]})
        return {row["artifact"]: row["present"] for row in self._rows(contract)}

    def test_the_driver_s_artifacts_are_looked_for_where_it_writes_them(self):
        """Issue #589: `state_history` and `step_series` sit under `state_snapshots/`
        (`_EVIDENCE_DIRS`), so a row reading `raw/<artifact>` would answer false for every run."""
        self.assertEqual(self._drive_rows(), {"state_snapshots": False, "state_history": False,
                                              "step_series": False})
        self.raw.write("state_snapshots/case_a.json", {"h": [[1.0]]})
        self.assertEqual(self._drive_rows(), {"state_snapshots": True, "state_history": False,
                                              "step_series": False})
        self.raw.write("state_snapshots/history/case_a__1.json", {"h": [[1.0]]})
        self.assertEqual(self._drive_rows(), {"state_snapshots": True, "state_history": True,
                                              "step_series": False})
        self.raw.write("state_snapshots/series/case_a.json", {"step": [1], "t": 1.0})
        self.assertEqual(self._drive_rows(), {"state_snapshots": True, "state_history": True,
                                              "step_series": True})

    def test_a_directory_at_the_artifact_s_bare_name_does_not_answer(self):
        self.raw.write("state_history/x.json", {})
        self.raw.write("step_series/x.json", {})
        self.assertEqual(self._drive_rows(), {"state_snapshots": False, "state_history": False,
                                              "step_series": False})

    def test_required_false_is_carried(self):
        contract = _contract(raw_requirements={"required_evidence": [
            {"artifact": "state_snapshots", "required": False}]})
        self.assertFalse(self._rows(contract)[0]["required"])


class ExcerptIsBoundedTest(unittest.TestCase):
    """The whole reason this module exists: `raw/` is 13,023,908 B on `shallow_water2d` and
    cannot be inlined. The relation is asserted on input of the real shape, not described."""

    def test_the_retired_sections_are_gone(self):
        # Issue #582: `raw/metrics_basis.json` left the contract, so its coverage matrix and its
        # array rows left the window, also when a recorded run still holds the file.
        raw = _RawDir(self)
        raw.write("metrics_basis.json", {"per_test": []})
        self.assertEqual(set(rex.raw_evidence_excerpt(raw.path, _contract())),
                         {"policy_version", "raw_dir_present", "required_evidence",
                          "state_snapshots", "problems"})

    def test_no_numeric_array_survives_into_the_excerpt(self):
        raw = _RawDir(self)
        # 3 variables x 128 x 128 floats, twice — the shape of one real pair of snapshots.
        # Every value is DISTINCT and 8 characters wide, so no element's repr is a substring of
        # another's or of a summary statistic. The first version of this fixture used `i * j`,
        # whose reprs nest (`129.0` inside `16129.0`), which made the leak search below report
        # four false positives — a probe that cannot tell a leak from an overlap answers
        # nothing.
        array = [[100000.0 + (i * 128 + j) for j in range(128)] for i in range(128)]
        raw.write("state_snapshots/snapshot_schema.json",
                  {"variables": [{"name": "h", "shape_expr": "[nx, ny]"}],
                   "time_variable": "t"})
        snapshots = [raw.write(f"state_snapshots/{case}.json",
                               {"h": array, "hu": array, "hv": array, "dx": 0.5, "t": 1.0})
                     for case in ("case_a", "case_b")]

        rendered = json.dumps(rex.raw_evidence_excerpt(raw.path, _contract()), indent=2)

        source_bytes = sum(p.stat().st_size for p in snapshots)
        self.assertGreater(source_bytes, 500_000)
        self.assertLess(len(rendered), source_bytes / 100)
        # The row count is the bound's shape: snapshot x variable.
        excerpt = json.loads(rendered)
        self.assertEqual([len(c["variables"]) for c in excerpt["state_snapshots"]["cases"]],
                         [4, 4])
        # And the arrays themselves are gone. NOT by spot-checking one magic value: an earlier
        # version of this row asserted only that `8128.0` (a value from deep inside the array)
        # was absent, and a review round walked straight through it by making `_summarize`
        # inline the first five elements of every array — the leak was at the FRONT, and the
        # probe was at the back. The property is checked over the whole population instead:
        # every distinct source value must be absent from the render EXCEPT the two the summary
        # legitimately reports.
        source_values = {v for row in array for v in row}
        legitimate = {min(source_values), max(source_values)}
        self.assertEqual({len(repr(v)) for v in source_values}, {8},
                         "the fixture's values must be one width, or a substring search "
                         "cannot tell a leak from an overlap")
        leaked = sorted(v for v in source_values - legitimate if repr(v) in rendered)
        self.assertEqual(leaked, [], f"{len(leaked)} array element(s) reached the excerpt")
        # Self-test the probe: the two values it EXEMPTS must really be in the render, or the
        # assertion above is green because the search itself finds nothing.
        for value in legitimate:
            self.assertIn(repr(value), rendered,
                          f"{value!r} should appear as a summary statistic")

    def test_output_is_json_serializable_and_deterministic(self):
        raw = _RawDir(self)
        raw.write("state_snapshots/case_a.json", {"h": [[math.inf]], "t": 0.0})
        raw.write("state_snapshots/case_b.json", '{"h": [[NaN, 1.0]], "t": 0.0}')
        first = json.dumps(rex.raw_evidence_excerpt(raw.path, _contract()),
                           allow_nan=False, sort_keys=False)
        second = json.dumps(rex.raw_evidence_excerpt(raw.path, _contract()),
                            allow_nan=False, sort_keys=False)
        self.assertEqual(first, second)

    def test_policy_version_is_stamped(self):
        raw = _RawDir(self)
        self.assertEqual(
            rex.raw_evidence_excerpt(raw.path, _contract())["policy_version"],
            rex.RAW_EXCERPT_POLICY_VERSION)
        self.assertIsInstance(rex.RAW_EXCERPT_POLICY_VERSION, int)
        # 2 since Z6 (issue #255): the window gained each case's `initial` capture. 3 since
        # issue #582: the metrics-basis sections left it and each variable names its `source`. 4
        # since issue #589: `source` went, and each case carries `history_samples` and `series`.
        self.assertEqual(rex.RAW_EXCERPT_POLICY_VERSION, 4)


if __name__ == "__main__":
    unittest.main()
