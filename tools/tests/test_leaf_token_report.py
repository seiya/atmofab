"""Witness `skills/workflow-timing-audit/scripts/leaf_token_report.py` on synthetic runs.

The script is the instrument `docs/ORCHESTRATION.md` §"Leaf LLM configuration" names as the way
to take its figures, so its parse is evidence and is pinned here rather than trusted. An earlier
version of this file asserted four of the six reported columns and left `prompt_tokens` and
`output_tokens` unwitnessed, which a mutation sweep caught by swapping them for the
`input_tokens` / `output_tokens` spelling `agent_runs.jsonl` uses -- printing `None` into a
canonical document's figures with the suite green. Every column is asserted here, and every
branch that decides WHETHER a figure is printed has a constructed input, including the ones a
real run only sometimes produces.
"""
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "skills" / "workflow-timing-audit" / "scripts" / "leaf_token_report.py"
sys.path.insert(0, str(_SCRIPT.parent))
import leaf_token_report  # noqa: E402


def _stream(frames):
    return "".join(f"data: {json.dumps(f)}\n\n" for f in frames) + "data: [DONE]\n\n"


def _usage_frame(*, prompt=10, completion=100, reasoning=40, finish="stop", content=None):
    choice = {"delta": {} if content is None else {"content": content}, "finish_reason": finish}
    return {"choices": [choice],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion,
                      "completion_tokens_details": {"reasoning_tokens": reasoning}}}


class LeafTokenReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orch = pathlib.Path(self.tmp.name)
        (self.orch / "launches").mkdir()
        # Created empty: a launch with no run row is one of the cases under test, and without
        # this the fixture would fail on the missing file instead of exercising it.
        (self.orch / "agent_runs.jsonl").write_text("", encoding="utf-8")
        self.addCleanup(self.tmp.cleanup)

    def _leaf(self, arid, *, substep="verify", started="2026-08-06T12:00:00.000000Z",
              finished="2026-08-06T12:00:10.000000Z", frames=None, body=None,
              response=True, run_row=True):
        text = body if body is not None else _stream(frames or [])
        (self.orch / "launches" / f"{arid}.http_response.txt").write_text(text, encoding="utf-8")
        if response:
            (self.orch / "launches" / f"{arid}.response.json").write_text(
                json.dumps({"started_at": started}), encoding="utf-8")
        if run_row:
            with open(self.orch / "agent_runs.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"agent_run_id": arid, "step": "generate",
                                     "substep": substep, "started_at": started,
                                     "finished_at": finished}) + "\n")

    def _rows(self):
        return {r["agent_run_id"]: r for r in leaf_token_report.rows(str(self.orch))}

    def _run_cli(self, *args):
        return subprocess.run([sys.executable, str(_SCRIPT), str(self.orch), *args],
                              capture_output=True, text=True, cwd=str(_ROOT))

    # --- the six reported columns -------------------------------------------------------
    def test_every_reported_column_is_asserted(self):
        self._leaf("aaaa1111", substep="generate",
                   finished="2026-08-06T12:00:20.000000Z",
                   frames=[_usage_frame(prompt=37512, completion=65536, reasoning=62460,
                                        finish="length", content="abcde")])
        row = self._rows()["aaaa1111"]
        self.assertEqual(row["prompt_tokens"], 37512)      # the two columns a sweep found
        self.assertEqual(row["output_tokens"], 65536)      # unwitnessed
        self.assertEqual(row["reasoning_tokens"], 62460)
        self.assertEqual(row["answer_chars"], 5)
        self.assertEqual(row["elapsed_seconds"], 20.0)
        self.assertEqual(row["tokens_per_second"], 3276.8)
        self.assertEqual(row["finish_reason"], ["length"])
        # and the CLI prints them, which nothing else here would notice
        out = self._run_cli().stdout
        self.assertIn("in= 37512", out)
        self.assertIn("out= 65536", out)

    def test_the_answer_is_counted_in_characters_not_by_subtraction(self):
        # reasoning EXCEEDS completion, as the real endpoint reports at the ceiling: a report
        # deriving the answer as completion - reasoning prints a negative "answer" for a
        # request that wrote nothing.
        self._leaf("aaaa2222", frames=[{"choices": [{"delta": {"reasoning_content": "x" * 40}}]},
                                       _usage_frame(completion=16384, reasoning=16426,
                                                    finish="length")])
        self.assertEqual(self._rows()["aaaa2222"]["answer_chars"], 0)

    def test_a_non_string_content_delta_contributes_no_characters(self):
        # Some dialects put a LIST in `delta.content`. A truthiness test would count its
        # element count as characters, printing a small non-zero answer for a request that
        # wrote none -- which is the one number this report exists to get right.
        self._leaf("aaaa3333", frames=[{"choices": [{"delta": {"content": ["a", "b", "c"]}}]},
                                       _usage_frame(completion=16384, reasoning=16400,
                                                    finish="length")])
        self.assertEqual(self._rows()["aaaa3333"]["answer_chars"], 0)

    # --- ordering ------------------------------------------------------------------------
    def test_rows_are_ordered_by_finished_at_not_by_agent_run_id(self):
        # The ids are chosen so lexical order is the REVERSE of chronological order: a report
        # sorted by filename reads as a timeline it is not.
        self._leaf("zzzz0001", finished="2026-08-06T12:00:05.000000Z", frames=[_usage_frame()])
        self._leaf("aaaa0002", finished="2026-08-06T12:00:30.000000Z", frames=[_usage_frame()])
        self.assertEqual([r["agent_run_id"] for r in leaf_token_report.rows(str(self.orch))],
                         ["zzzz0001", "aaaa0002"])

    # --- the three ways a leaf produces no usage frame, told apart ------------------------
    def test_a_severed_stream_is_reported_as_severed_with_its_frame_count(self):
        self._leaf("bbbb1111", frames=[{"choices": [{"delta": {"reasoning_content": "y"}}]}])
        row = self._rows()["bbbb1111"]
        self.assertIn("severed after 1 frames", row["note"])
        self.assertNotIn("tokens_per_second", row)

    def test_an_error_body_is_reported_with_its_head_so_a_504_is_readable(self):
        self._leaf("bbbb2222", body="<html>\n<head><title>504 Gateway Time-out</title></head>\n")
        note = self._rows()["bbbb2222"]["note"]
        self.assertIn("not an event stream", note)
        self.assertIn("504 Gateway Time-out", note)

    def test_a_launch_with_no_agent_runs_row_is_not_called_a_transport_death(self):
        self._leaf("bbbb3333", run_row=False, frames=[_usage_frame()])
        row = self._rows()["bbbb3333"]
        self.assertIn("no agent_runs.jsonl row", row["note"])
        self.assertNotIn("tokens_per_second", row)

    def test_a_run_without_finished_at_reports_that_rather_than_raising(self):
        self._leaf("bbbb4444", finished=None, frames=[_usage_frame()])
        self.assertIn("no finished_at", self._rows()["bbbb4444"]["note"])

    # --- numbers that must never be printed as if measured -------------------------------
    def test_a_non_positive_elapsed_yields_no_rate(self):
        self._leaf("cccc1111", started="2026-08-06T12:00:20.000000Z",
                   finished="2026-08-06T12:00:10.000000Z", frames=[_usage_frame()])
        row = self._rows()["cccc1111"]
        self.assertIsNone(row.get("tokens_per_second"))
        self.assertIn("not positive", row["note"])

    def test_a_messages_api_stream_is_a_dialect_mismatch_not_a_zero(self):
        self._leaf("cccc2222", frames=[{"type": "message_delta",
                                        "usage": {"output_tokens": 512}}])
        row = self._rows()["cccc2222"]
        self.assertIn("dialect mismatch", row["note"])
        self.assertIsNone(row.get("tokens_per_second"))

    def test_the_last_usage_frame_wins(self):
        self._leaf("cccc3333", finished="2026-08-06T12:00:10.000000Z",
                   frames=[_usage_frame(completion=1), _usage_frame(completion=100)])
        self.assertEqual(self._rows()["cccc3333"]["output_tokens"], 100)

    def test_a_duplicate_agent_run_id_is_refused_rather_than_last_wins(self):
        self._leaf("dddd1111", frames=[_usage_frame()])
        with open(self.orch / "agent_runs.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"agent_run_id": "dddd1111", "step": "generate",
                                 "substep": "verify",
                                 "finished_at": "2026-08-06T13:00:00.000000Z"}) + "\n")
        with self.assertRaises(ValueError):
            self._rows()

    def test_elapsed_needs_the_response_file_and_is_not_guessed_without_it(self):
        self._leaf("dddd2222", response=False, frames=[_usage_frame()])
        with self.assertRaises(FileNotFoundError):
            self._rows()

    def test_a_blank_line_in_agent_runs_is_not_a_record(self):
        self._leaf("dddd3333", frames=[_usage_frame()])
        with open(self.orch / "agent_runs.jsonl", "a", encoding="utf-8") as fh:
            fh.write("\n")
        self.assertEqual(self._rows()["dddd3333"]["output_tokens"], 100)

    # --- the CLI ------------------------------------------------------------------------
    def test_the_substep_filter_selects_rather_than_excludes(self):
        self._leaf("eeee1111", substep="generate", frames=[_usage_frame()])
        self._leaf("eeee2222", substep="verify", frames=[_usage_frame()])
        out = self._run_cli("generate").stdout
        self.assertIn("eeee1111", out)
        self.assertNotIn("eeee2222", out)

    def test_a_run_with_no_leaf_streams_says_so_and_exits_nonzero(self):
        (self.orch / "agent_runs.jsonl").write_text("", encoding="utf-8")
        result = self._run_cli()
        self.assertEqual(result.returncode, 1)
        self.assertIn("no persisted leaf streams", result.stderr)

    def test_an_orchestration_id_resolves_under_the_workspace_root(self):
        # The sibling audit script takes an id; a path-only tool would answer a plausible
        # invocation with a FileNotFoundError naming a relative path.
        self.assertEqual(leaf_token_report.resolve(str(self.orch)), str(self.orch))
        with self.assertRaises(FileNotFoundError):
            leaf_token_report.resolve("orch_does_not_exist")


if __name__ == "__main__":
    unittest.main()
