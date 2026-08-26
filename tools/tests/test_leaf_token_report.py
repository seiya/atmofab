"""Witness `skills/workflow-timing-audit/scripts/leaf_token_report.py` on a synthetic run.

The script is the instrument `docs/ORCHESTRATION.md` §"Leaf LLM configuration" names as the way
to re-take its figures, so its parse is evidence and is pinned here rather than trusted. The
fixture is built to carry the three properties that decide the parse and that a real run only
sometimes exhibits: a request cut at the ceiling that wrote no answer while its token accounting
does not close, a leaf that died in transport with no `usage` frame, and an elapsed time that can
only be computed by joining `launches/*.response.json` to `agent_runs.jsonl`.
"""
import json
import pathlib
import sys
import unittest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "skills" / "workflow-timing-audit" / "scripts"))
import leaf_token_report  # noqa: E402


def _stream(frames):
    return "".join(f"data: {json.dumps(f)}\n\n" for f in frames) + "data: [DONE]\n\n"


class LeafTokenReportTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.orch = pathlib.Path(self.tmp.name)
        (self.orch / "launches").mkdir()
        self.addCleanup(self.tmp.cleanup)

    def _leaf(self, arid, *, substep, started, finished, frames, response=True):
        (self.orch / "launches" / f"{arid}.http_response.txt").write_text(
            _stream(frames), encoding="utf-8")
        if response:
            (self.orch / "launches" / f"{arid}.response.json").write_text(
                json.dumps({"started_at": started}), encoding="utf-8")
        with open(self.orch / "agent_runs.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"agent_run_id": arid, "step": "generate",
                                 "substep": substep, "finished_at": finished}) + "\n")

    def _rows(self):
        return {r["agent_run_id"]: r for r in leaf_token_report.rows(str(self.orch))}

    def test_ceiling_row_counts_the_answer_in_characters_not_by_subtraction(self):
        # reasoning EXCEEDS completion, as the real endpoint reports at the ceiling: a report
        # deriving the answer as completion - reasoning would print a negative "answer" for a
        # request that wrote nothing.
        self._leaf("aaaa1111", substep="verify",
                   started="2026-08-06T13:10:04.176757Z", finished="2026-08-06T13:16:09.704271Z",
                   frames=[{"choices": [{"delta": {"reasoning_content": "x" * 40}}]},
                           {"choices": [{"delta": {}, "finish_reason": "length"}],
                            "usage": {"prompt_tokens": 37031, "completion_tokens": 16384,
                                      "completion_tokens_details": {"reasoning_tokens": 16426}}}])
        row = self._rows()["aaaa1111"]
        self.assertEqual(row["answer_chars"], 0)
        self.assertEqual(row["finish_reason"], ["length"])
        self.assertEqual(row["reasoning_tokens"], 16426)
        self.assertEqual(row["elapsed_seconds"], 365.5)
        self.assertEqual(row["tokens_per_second"], 44.8)

    def test_an_answer_is_counted_from_content_deltas(self):
        self._leaf("bbbb2222", substep="generate",
                   started="2026-08-06T12:00:00.000000Z", finished="2026-08-06T12:00:10.000000Z",
                   frames=[{"choices": [{"delta": {"content": "abc"}}]},
                           {"choices": [{"delta": {"content": "de"}, "finish_reason": "stop"}],
                            "usage": {"prompt_tokens": 10, "completion_tokens": 100,
                                      "completion_tokens_details": {"reasoning_tokens": 40}}}])
        row = self._rows()["bbbb2222"]
        self.assertEqual(row["answer_chars"], 5)
        self.assertEqual(row["tokens_per_second"], 10.0)

    def test_a_leaf_with_no_usage_frame_is_a_transport_death_not_a_zero(self):
        self._leaf("cccc3333", substep="verify",
                   started="2026-08-06T14:46:33.983639Z", finished="2026-08-06T14:47:09.278775Z",
                   frames=[{"choices": [{"delta": {"reasoning_content": "y"}}]}])
        row = self._rows()["cccc3333"]
        self.assertTrue(row["transport_death"])
        self.assertNotIn("output_tokens", row)

    def test_elapsed_needs_the_response_file_and_is_not_guessed_without_it(self):
        # `started_at` lives only in launches/<arid>.response.json. Without it there is no
        # elapsed to compute, and the script must fail loudly rather than invent one.
        self._leaf("dddd4444", substep="verify", started="unused",
                   finished="2026-08-06T13:16:09.704271Z", response=False,
                   frames=[{"choices": [{"delta": {}, "finish_reason": "length"}],
                            "usage": {"prompt_tokens": 1, "completion_tokens": 2,
                                      "completion_tokens_details": {"reasoning_tokens": 2}}}])
        with self.assertRaises(FileNotFoundError):
            self._rows()


if __name__ == "__main__":
    unittest.main()
