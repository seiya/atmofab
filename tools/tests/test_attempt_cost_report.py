"""Witness `skills/workflow-timing-audit/scripts/attempt_cost_report.py` on synthetic runs.

The script is the instrument the timing-audit SKILL's §"Comparing cost across runs" names for
the retry share issue #94 records, so the three decisions a figure depends on are pinned here:
which row is a first attempt and which a retry, that a usage marker is not counted as a zero,
and which failure a retry is attributed to.
"""
import json
import pathlib
import sys
import tempfile
import unittest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "skills" / "workflow-timing-audit" / "scripts" / "attempt_cost_report.py"
sys.path.insert(0, str(_SCRIPT.parent))
import attempt_cost_report  # noqa: E402

_NODE = "problem/x@0.1.0"


def _row(step, substep, started, *, status="pass", out=None, total=None, cost=None,
         node=_NODE, usage=None):
    row = {"agent_role": "substep", "node_key": node, "step": step, "substep": substep,
           "status": status, "started_at": started}
    if usage is not None:
        row["usage"] = usage
    elif out is not None:
        row["usage"] = {"output_tokens": out, "total_tokens": total or out * 2,
                        "cost_usd": cost if cost is not None else out / 1000}
    else:
        row["usage"] = {"status": "not_measured", "reason": "deterministic"}
    return row


class AttemptCostReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _orch(self, orch_id, rows, workspace="workspace"):
        d = self.root / workspace / "orchestrations" / orch_id
        d.mkdir(parents=True)
        # The orchestration's own row: not a substep, so never counted.
        head = {"agent_role": "orchestration", "started_at": "2026-09-01T00:00:00Z"}
        (d / "agent_runs.jsonl").write_text(
            "\n".join(json.dumps(r) for r in [head, *rows]) + "\n", encoding="utf-8")

    def _report(self, **kw):
        return attempt_cost_report.analyze(
            attempt_cost_report.load_rows(str(self.root), kw.get("since"), kw.get("until")))

    def test_later_row_for_same_substep_is_a_retry_attributed_to_the_failure_before_it(self):
        # Written out of order: attempts are ranked by started_at, not by file position.
        self._orch("orch_a", [
            _row("compile", "generate", "2026-09-01T00:04:00Z", out=300),
            _row("compile", "verify", "2026-09-01T00:05:00Z", out=70),
            _row("compile", "verify", "2026-09-01T00:03:00Z", status="fail", out=50),
            _row("compile", "generate", "2026-09-01T00:01:00Z", out=100),
            _row("compile", "static", "2026-09-01T00:02:00Z"),
        ])
        report = self._report()
        totals = report["totals"]
        self.assertEqual((totals["first"]["n"], totals["first"]["output_tokens"]), (2, 150))
        self.assertEqual((totals["retry"]["n"], totals["retry"]["output_tokens"]), (2, 370))
        self.assertEqual(set(report["causes"]), {"compile.verify fail -> compile.generate",
                                                 "compile.verify fail -> compile.verify"})
        self.assertEqual(report["causes"]["compile.verify fail -> compile.generate"]
                         ["output_tokens"], 300)
        self.assertEqual(report["per_substep"]["compile.generate"]
                         ["attempt1_median_output_tokens"], 100)
        self.assertEqual(report["orchestrations"], 1)

    def test_a_deterministic_substep_failure_is_a_cause_though_it_has_no_usage(self):
        self._orch("orch_a", [
            _row("generate", "generate", "2026-09-01T00:01:00Z", out=100),
            _row("generate", "gate", "2026-09-01T00:02:00Z", status="fail"),
            _row("generate", "generate", "2026-09-01T00:03:00Z", out=40),
        ])
        causes = self._report()["causes"]
        self.assertEqual(list(causes), ["generate.gate fail -> generate.generate"])
        self.assertNotIn("generate.gate", self._report()["per_substep"])

    def test_a_marker_is_not_a_zero_and_still_ranks_the_attempt(self):
        # The first launch died with no usage frame: it is not counted, but the one after it is
        # still the second attempt, not a first attempt with a small cost.
        self._orch("orch_a", [
            _row("generate", "generate", "2026-09-01T00:01:00Z", status="fail",
                 usage={"status": "unavailable", "reason": "no usage frame"}),
            _row("generate", "generate", "2026-09-01T00:02:00Z", out=80),
        ])
        totals = self._report()["totals"]
        self.assertEqual(totals["first"]["n"], 0)
        self.assertEqual((totals["retry"]["n"], totals["retry"]["output_tokens"]), (1, 80))

    def test_a_rerun_in_a_new_orchestration_is_a_first_attempt(self):
        self._orch("orch_a", [_row("compile", "generate", "2026-09-01T00:01:00Z", out=100)])
        self._orch("orch_b", [_row("compile", "generate", "2026-09-02T00:01:00Z", out=100)],
                   workspace="workspace_20260902")
        report = self._report()
        self.assertEqual(report["totals"]["retry"]["n"], 0)
        self.assertEqual(report["totals"]["first"]["n"], 2)
        self.assertEqual(report["orchestrations"], 2)

    def test_two_nodes_in_one_run_rank_their_attempts_separately(self):
        self._orch("orch_a", [
            _row("compile", "generate", "2026-09-01T00:01:00Z", out=100, node="a@1"),
            _row("compile", "generate", "2026-09-01T00:02:00Z", out=100, node="b@1"),
        ])
        self.assertEqual(self._report()["totals"]["retry"]["n"], 0)

    def test_window_is_half_open(self):
        self._orch("orch_a", [
            _row("compile", "generate", "2026-08-31T23:59:59Z", out=1),
            _row("validate", "judge", "2026-09-01T00:00:00Z", out=2),
            _row("generate", "verify", "2026-09-02T00:00:00Z", out=4),
        ])
        report = self._report(since="2026-09-01T00:00:00Z", until="2026-09-02T00:00:00Z")
        self.assertEqual(list(report["per_substep"]), ["validate.judge"])

    def test_share_and_render(self):
        self._orch("orch_a", [
            _row("compile", "generate", "2026-09-01T00:01:00Z", out=300, total=1000, cost=3.0),
            _row("compile", "verify", "2026-09-01T00:02:00Z", status="fail", out=10,
                 total=10, cost=0.0),
            _row("compile", "generate", "2026-09-01T00:03:00Z", out=100, total=100, cost=1.0),
        ])
        report = self._report()
        first, retry = report["totals"]["first"], report["totals"]["retry"]
        self.assertAlmostEqual(attempt_cost_report.share(first, retry, "output_tokens"),
                               100 / 410)
        self.assertAlmostEqual(attempt_cost_report.share(first, retry, "total_tokens"),
                               100 / 1110)
        self.assertAlmostEqual(attempt_cost_report.share(first, retry, "cost_usd"), 0.25)
        text = attempt_cost_report.render(report)
        self.assertIn("retry share: output_tokens 24.4%, total_tokens 9.0%, cost_usd 25.0%",
                      text)
        self.assertIn("compile.verify fail -> compile.generate", text)

    def test_no_measured_rows_reports_na_not_zero(self):
        self._orch("orch_a", [_row("generate", "gate", "2026-09-01T00:01:00Z")])
        report = self._report()
        self.assertIsNone(attempt_cost_report.share(
            report["totals"]["first"], report["totals"]["retry"], "output_tokens"))
        self.assertIn("output_tokens n/a", attempt_cost_report.render(report))


if __name__ == "__main__":
    unittest.main()
