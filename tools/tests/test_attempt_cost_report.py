"""Witness `skills/workflow-timing-audit/scripts/attempt_cost_report.py` on synthetic runs.

The script is the instrument the timing-audit SKILL's §"Comparing cost across runs" names for
the retry share issue #94 records, so every decision a published figure depends on is pinned
here: which row is a first attempt and which a retry (including the cut at an operator
`--resume`), that a usage marker is not counted as a zero, which failure a retry is attributed
to, the correction of a warm-resumed row recorded as a session running total, and every column
of the rendered table, since that table is quoted verbatim into the record.
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

    def _orch(self, orch_id, rows, workspace="workspace", resumes=()):
        d = self.root / workspace / "orchestrations" / orch_id
        d.mkdir(parents=True)
        # The orchestration's own row: not a substep, so never counted.
        head = {"agent_role": "orchestration", "started_at": "2026-09-01T00:00:00Z"}
        (d / "agent_runs.jsonl").write_text(
            "\n".join(json.dumps(r) for r in [head, *rows]) + "\n", encoding="utf-8")
        # A decoy event with a resume-like name precedes the real one: only
        # `resume_status_reset` cuts a segment.
        events = [{"ts": "2026-09-01T00:00:30Z", "event": "resume_phase_state_preserved"}]
        events += [{"ts": ts, "event": "resume_status_reset"} for ts in resumes]
        (d / "phase_state_log.jsonl").write_text(
            "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
        return d

    def _warm(self, orch_dir, arid, target, turn_usage, *, warm=True):
        """Write a launch request naming the resumed turn, and the turn's own CLI envelope."""
        (orch_dir / "launches").mkdir(exist_ok=True)
        (orch_dir / "launches" / f"{arid}.request.json").write_text(json.dumps(
            {"agent_run_id": arid, "warm_resume": warm,
             "repair_target_agent_run_id": target}), encoding="utf-8")
        dialogs = orch_dir / "agents" / arid / "dialogs"
        dialogs.mkdir(parents=True)
        (dialogs / "leaf.stdout.log").write_text(
            json.dumps({"type": "result", "session_id": arid, "usage": turn_usage}),
            encoding="utf-8")

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

    def test_a_retry_is_attributed_to_the_MOST_RECENT_failure_even_in_another_step(self):
        self._orch("orch_a", [
            _row("generate", "generate", "2026-09-01T00:01:00Z", out=100),
            _row("generate", "verify", "2026-09-01T00:02:00Z", status="fail", out=5),
            _row("generate", "generate", "2026-09-01T00:03:00Z", out=40),
            _row("generate", "gate", "2026-09-01T00:04:00Z", status="fail"),
            _row("validate", "judge", "2026-09-01T00:05:00Z", status="fail", out=7),
            _row("generate", "generate", "2026-09-01T00:06:00Z", out=30),
        ])
        causes = self._report()["causes"]
        self.assertEqual({k: v["output_tokens"] for k, v in causes.items()},
                         {"generate.verify fail -> generate.generate": 40,
                          "validate.judge fail -> generate.generate": 30})

    def test_a_retry_with_no_failure_before_it_says_so(self):
        self._orch("orch_a", [
            _row("compile", "generate", "2026-09-01T00:01:00Z", out=10),
            _row("compile", "generate", "2026-09-01T00:02:00Z", out=20),
        ])
        self.assertEqual(list(self._report()["causes"]),
                         ["no preceding failure -> compile.generate"])

    def test_attempt1_figure_is_the_median_not_the_mean(self):
        for i, out in enumerate((10, 20, 90)):
            self._orch(f"orch_{i}", [_row("compile", "generate", f"2026-09-0{i + 1}T00:00:00Z",
                                          out=out)])
        self.assertEqual(self._report()["per_substep"]["compile.generate"]
                         ["attempt1_median_output_tokens"], 20)

    def test_an_operator_resume_starts_a_new_segment(self):
        self._orch("orch_a", [
            _row("compile", "generate", "2026-09-01T00:00:10Z", out=100),
            _row("compile", "verify", "2026-09-01T00:00:20Z", status="fail", out=5),
            _row("compile", "generate", "2026-09-01T00:00:40Z", out=60),
            # The operator resumed at 00:01:00: the next compile.generate is a first attempt.
            _row("compile", "generate", "2026-09-01T00:02:00Z", out=70),
            _row("compile", "verify", "2026-09-01T00:03:00Z", status="fail", out=5),
            _row("compile", "generate", "2026-09-01T00:04:00Z", out=80),
        ], resumes=["2026-09-01T00:01:00Z"])
        entry = self._report()["per_substep"]["compile.generate"]
        self.assertEqual((entry["first"]["n"], entry["first"]["output_tokens"]), (2, 170))
        self.assertEqual((entry["retry"]["n"], entry["retry"]["output_tokens"]), (2, 140))
        # The decoy event alone (no resume) would leave one segment: 1 first, 3 retries.
        self.assertEqual(self._report()["causes"]["compile.verify fail -> compile.generate"]["n"], 2)

    def _cumulative_session(self, *, turn2_usage=None, target="t1", warm=True):
        t1 = {"input_tokens": 2, "output_tokens": 100, "cache_read_input_tokens": 0,
              "cache_creation_input_tokens": 50, "total_tokens": 152, "cost_usd": 1.0}
        t2 = {"input_tokens": 4, "output_tokens": 130, "cache_read_input_tokens": 50,
              "cache_creation_input_tokens": 80, "total_tokens": 264, "cost_usd": 1.5}
        r1 = _row("compile", "generate", "2026-09-01T00:01:00Z", usage=t1)
        r1["agent_run_id"] = "t1"
        fail = _row("compile", "verify", "2026-09-01T00:02:00Z", status="fail", out=5)
        fail["agent_run_id"] = "v1"
        r2 = _row("compile", "generate", "2026-09-01T00:03:00Z", usage=t2)
        r2["agent_run_id"] = "t2"
        d = self._orch("orch_a", [r1, fail, r2])
        per_turn = {"input_tokens": 2, "output_tokens": 30, "cache_read_input_tokens": 50,
                    "cache_creation_input_tokens": 30}
        self._warm(d, "t2", target, turn2_usage or per_turn, warm=warm)
        return self._report()

    def test_a_cumulative_warm_resumed_row_is_replaced_by_its_turn(self):
        report = self._cumulative_session()
        retry = report["per_substep"]["compile.generate"]["retry"]
        self.assertEqual((retry["output_tokens"], retry["total_tokens"]), (30, 112))
        self.assertAlmostEqual(retry["cost_usd"], 0.5)
        self.assertEqual((report["decumulated_rows"], report["uncorrected_warm_resumes"]), (1, 0))

    def test_a_per_turn_warm_resumed_row_is_left_alone(self):
        # Recorded usage already equals the turn's envelope usage: an older CLI.
        same = {"input_tokens": 4, "output_tokens": 130, "cache_read_input_tokens": 50,
                "cache_creation_input_tokens": 80}
        report = self._cumulative_session(turn2_usage=same)
        self.assertEqual(report["per_substep"]["compile.generate"]["retry"]["output_tokens"], 130)
        self.assertEqual((report["decumulated_rows"], report["uncorrected_warm_resumes"]), (0, 0))

    def test_a_row_the_equality_cannot_decide_is_counted_not_guessed(self):
        # The difference is 30 output tokens but the envelope says 29 (one class off).
        off = {"input_tokens": 2, "output_tokens": 29, "cache_read_input_tokens": 50,
               "cache_creation_input_tokens": 30}
        report = self._cumulative_session(turn2_usage=off)
        self.assertEqual(report["per_substep"]["compile.generate"]["retry"]["output_tokens"], 130)
        self.assertEqual((report["decumulated_rows"], report["uncorrected_warm_resumes"]), (0, 1))
        self.assertIn("warm-resume rows left as recorded: 1",
                      attempt_cost_report.render(report))

    def test_a_warm_resume_whose_target_has_no_row_is_counted_not_guessed(self):
        report = self._cumulative_session(target="missing")
        self.assertEqual((report["decumulated_rows"], report["uncorrected_warm_resumes"]), (0, 1))

    def test_a_cold_launch_is_never_decumulated(self):
        report = self._cumulative_session(warm=False)
        self.assertEqual(report["per_substep"]["compile.generate"]["retry"]["output_tokens"], 130)
        self.assertEqual((report["decumulated_rows"], report["uncorrected_warm_resumes"]), (0, 0))

    def test_rendered_table_columns(self):
        # compile.generate: 3 firsts (10, 20, 90), one retry with output 100 and total 900, so
        # retry%out (100/220) and retry%total (900/1140) differ and first/retry differ.
        for i, out in enumerate((10, 20, 90)):
            rows = [_row("compile", "generate", f"2026-09-0{i + 1}T00:00:00Z", out=out,
                         total=80)]
            if i == 0:
                rows += [_row("compile", "verify", "2026-09-01T00:01:00Z", status="fail",
                              out=1, total=1),
                         _row("compile", "generate", "2026-09-01T00:02:00Z", out=100,
                              total=900)]
            self._orch(f"orch_{i}", rows)
        text = attempt_cost_report.render(self._report())
        self.assertIn("3 orchestrations, 5 measured leaf launches (1 retries)", text)
        self.assertIn(f"{'compile.generate':<22} {3:>5} {1:>5} {'45.5%':>9} {'20':>21}", text)
        self.assertIn(f"{'compile.verify':<22} {1:>5} {0:>5} {'0.0%':>9} {'1':>21}", text)
        self.assertIn(f"{100:>12,} {1:>4}  compile.verify fail -> compile.generate", text)

    def test_only_substep_rows_are_counted(self):
        # A non-substep row carrying a usage number (no such row exists today) is not a leaf
        # attempt and must not rank or count as one.
        step_row = _row("compile", "generate", "2026-09-01T00:00:30Z", out=999)
        step_row["agent_role"] = "step"
        self._orch("orch_a", [step_row, _row("compile", "generate", "2026-09-01T00:01:00Z",
                                             out=10)])
        totals = self._report()["totals"]
        self.assertEqual((totals["first"]["n"], totals["first"]["output_tokens"]), (1, 10))
        self.assertEqual(totals["retry"]["n"], 0)

    def test_no_measured_rows_reports_na_not_zero(self):
        self._orch("orch_a", [_row("generate", "gate", "2026-09-01T00:01:00Z")])
        report = self._report()
        self.assertIsNone(attempt_cost_report.share(
            report["totals"]["first"], report["totals"]["retry"], "output_tokens"))
        self.assertIn("output_tokens n/a", attempt_cost_report.render(report))


if __name__ == "__main__":
    unittest.main()
