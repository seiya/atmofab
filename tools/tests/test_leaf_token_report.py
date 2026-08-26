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
              response=True, run_row=True, run_started=None):
        """Write one leaf's three artifacts.

        `run_started` defaults to a stamp MICROSECONDS before `finished`, as a real
        `agent_runs.jsonl` row carries -- the record is written at completion. The request's
        own start lives only in `response.json`, and keeping the two APART here is what makes
        "which file supplies `started_at`" observable: with the same value in both, an
        implementation reading the wrong one passes.
        """
        text = body if body is not None else _stream(frames or [])
        (self.orch / "launches" / f"{arid}.http_response.txt").write_text(text, encoding="utf-8")
        if response:
            (self.orch / "launches" / f"{arid}.response.json").write_text(
                json.dumps({"started_at": started}), encoding="utf-8")
        if run_row:
            if run_started is None and finished is not None:
                run_started = finished.replace(".000000Z", ".000025Z")
            with open(self.orch / "agent_runs.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"agent_run_id": arid, "step": "generate",
                                     "substep": substep, "started_at": run_started,
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
        self.assertIn("no event-stream frames parsed", note)
        self.assertIn("504 Gateway Time-out", note)
        # NOT the repository's own `response_not_an_event_stream`, which names a 200 body that
        # did not open as a stream and fails closed on the first attempt. An HTTP error body
        # reaches this branch too and IS classified and retried, so borrowing that spelling
        # would tell a reader the opposite of what the conductor did.
        self.assertNotIn("response_not_an_event_stream", note)

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

    def test_the_cli_prints_every_column_under_its_own_label(self):
        # `in=` and `out=` were witnessed here before `reasoning=` and `answer=` were, and a
        # sweep that swapped the latter two in the f-string passed: the report would have
        # printed a reviewer's reasoning floor as its answer length.
        self._leaf("ffff1111", substep="generate", finished="2026-08-06T12:00:20.000000Z",
                   frames=[_usage_frame(prompt=37512, completion=65536, reasoning=62460,
                                        finish="length", content="abcde")])
        out = self._run_cli().stdout
        for label, value in (("in=", 37512), ("reasoning=", 62460), ("answer=", 5),
                             ("out=", 65536)):
            self.assertRegex(out, rf"{label}\s*{value}(ch)?(\s|$)")

    def test_the_cli_prints_a_note_row_instead_of_crashing_on_a_leaf_with_no_figures(self):
        # The note branch of `main()` had no test: dropping its `continue` left the suite green
        # and killed the real CLI with KeyError on any run containing a 504 row.
        self._leaf("ffff2222", body="<html><head><title>504 Gateway Time-out</title></head>")
        self._leaf("ffff3333", substep="generate", frames=[_usage_frame()])
        result = self._run_cli()
        self.assertEqual(result.returncode, 0)
        self.assertIn("504 Gateway Time-out", result.stdout)
        self.assertIn("out=", result.stdout)          # the healthy row still prints

    def test_no_arguments_is_a_usage_error(self):
        result = subprocess.run([sys.executable, str(_SCRIPT)], capture_output=True, text=True,
                                cwd=str(_ROOT))
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr)

    def test_only_the_http_response_streams_are_read(self):
        # A real `launches/` also holds `<arid>.prompt.txt` and `<arid>.reply.txt`; a looser
        # glob would fabricate a row per sibling file.
        self._leaf("ffff4444", substep="generate", frames=[_usage_frame()])
        (self.orch / "launches" / "ffff4444.prompt.txt").write_text("x", encoding="utf-8")
        (self.orch / "launches" / "ffff4444.reply.txt").write_text("y", encoding="utf-8")
        self.assertEqual([r["agent_run_id"] for r in leaf_token_report.rows(str(self.orch))],
                         ["ffff4444"])

    def test_a_zero_elapsed_is_refused_like_a_negative_one(self):
        # `<= 0`, not `< 0`: at exactly zero the rate is a division by zero, and zero is what a
        # record written in the same microsecond produces.
        self._leaf("ffff5555", started="2026-08-06T12:00:10.000000Z",
                   finished="2026-08-06T12:00:10.000000Z", frames=[_usage_frame()])
        row = self._rows()["ffff5555"]
        self.assertIsNone(row.get("tokens_per_second"))
        self.assertIn("not positive", row["note"])

    def test_elapsed_comes_from_the_request_start_not_the_agent_run_row(self):
        # The agent run's own `started_at` is written at completion, microseconds before
        # `finished_at`. Reading it would report ~0 s and an absurd rate for every leaf.
        self._leaf("ffff6666", started="2026-08-06T12:00:00.000000Z",
                   finished="2026-08-06T12:00:10.000000Z", frames=[_usage_frame()])
        self.assertEqual(self._rows()["ffff6666"]["elapsed_seconds"], 10.0)

    def test_a_row_without_finished_at_sorts_last_rather_than_breaking_the_sort(self):
        self._leaf("ffff7777", finished=None, frames=[_usage_frame()])
        self._leaf("ffff8888", finished="2026-08-06T12:00:30.000000Z", frames=[_usage_frame()])
        self.assertEqual([r["agent_run_id"] for r in leaf_token_report.rows(str(self.orch))],
                         ["ffff8888", "ffff7777"])

    def test_a_run_with_no_leaf_streams_says_so_and_exits_nonzero(self):
        (self.orch / "agent_runs.jsonl").write_text("", encoding="utf-8")
        result = self._run_cli()
        self.assertEqual(result.returncode, 1)
        self.assertIn("no persisted leaf streams", result.stderr)

    def test_an_orchestration_id_resolves_under_the_workspace_root(self):
        # POSITIVE witness: an implementation with the id branch deleted passes a
        # path-in/path-out assertion and a raises-on-nonsense assertion alike, so build the
        # workspace layout and resolve a bare id through it.
        # The layout is spelled LITERALLY, not built from `WORKSPACE_ROOT`: deriving the
        # fixture from the constant under test measures set identity with itself, and a mutant
        # pointing the constant somewhere else passes.
        root = self.orch / "fake_repo"
        (root / "workspace" / "orchestrations" / "orch_xyz").mkdir(parents=True)
        self.assertEqual(leaf_token_report.resolve("orch_xyz", start=str(root)),
                         str(root / "workspace" / "orchestrations" / "orch_xyz"))
        self.assertEqual(leaf_token_report.resolve(str(self.orch)), str(self.orch))
        with self.assertRaises(FileNotFoundError):
            leaf_token_report.resolve("orch_does_not_exist", start=str(root))

    def test_an_id_resolves_from_a_subdirectory_by_walking_to_the_checkout_root(self):
        # The sibling `analyze_timing.py` walks up to the repo root, so an id works from
        # anywhere. A bare relative constant would resolve only from the root itself.
        root = self.orch / "fake_repo"
        (root / ".git").mkdir(parents=True)
        (root / "workspace" / "orchestrations" / "orch_xyz").mkdir(parents=True)
        deep = root / "tools" / "backends"
        deep.mkdir(parents=True)
        self.assertEqual(leaf_token_report.resolve("orch_xyz", start=str(deep)),
                         str(root / "workspace" / "orchestrations" / "orch_xyz"))


if __name__ == "__main__":
    unittest.main()
