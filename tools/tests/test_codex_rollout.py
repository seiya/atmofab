"""`tools/codex_rollout.py` and the conductor's per-command capture (issue #547): the two
records the host's refusal of a cut read on a codex reading lineage reads. The rollout shapes
are the ones measured on codex-cli 0.164.0 (`test_bwrap_simulation.CodexReadingLeafCliTests`
MR5-e); the real CLI's rows are the witness there, these drive every branch of the reader."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import tools.workflow_conductor as wc
from tools import codex_rollout, leaf_contract

_HEADER = ("Warning: truncated output (original token count: 150000)\nTotal output lines: "
           "20000\n\nL00001 x\n…9000 tokens truncated…L20000 x\n")
_THREAD = "019a-thread_1"


def _start() -> dict:
    return {"type": "event_msg", "payload": {"type": "task_started"}}


def _out(output, kind: str = "custom_tool_call_output") -> dict:
    return {"type": "response_item", "payload": {"type": kind, "call_id": "c", "output": output}}


def _items(*texts: str) -> list[dict]:
    return [{"type": "input_text", "text": t} for t in texts]


class ScanTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = Path(tmp.name)

    def write(self, rows, thread: str = _THREAD, day: str = "2026/10/25") -> Path:
        path = (self.config / "sessions" / day
                / f"rollout-2026-10-25T05-38-31-{thread}.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join((r if isinstance(r, str) else json.dumps(r)) + "\n"
                                for r in rows), encoding="utf-8")
        return path

    def scan(self, thread: str | None = _THREAD) -> codex_rollout.RolloutScan:
        return codex_rollout.scan_launch(self.config, thread)

    def counts(self, thread: str | None = _THREAD) -> tuple:
        """`(reason, output_rows, cut_texts)` of the scan."""
        got = self.scan(thread)
        return got.reason, got.output_rows, got.cut_texts

    def test_every_measured_shape_of_a_cut_is_counted_once_per_text(self) -> None:
        escaped = json.dumps({"output": _HEADER})
        self.assertIn("\\n", escaped)
        doubly = json.dumps(json.dumps({"output": _HEADER}))
        ascii_escaped = json.dumps({"output": _HEADER}, ensure_ascii=True)
        self.assertIn("\\u2026", ascii_escaped)
        for name, output in (
                ("header at the item start", _items("Script completed\nOutput:\n", _HEADER)),
                ("inside a stringified result", _items("Output:\n", escaped)),
                ("escaped twice", _items(doubly)),
                ("ellipsis escaped by the script", _items(
                    ascii_escaped.replace("Warning: truncated output", "W"))),
                ("after a prefix", _items("A\n" + _HEADER)),
                ("marker alone", _items("L1\n…138012 tokens truncated…L2\n")),
                ("header alone", _items(_HEADER.split("\n\n")[0])),
                ("header alone after a prefix", _items("A\n" + _HEADER.split("\n\n")[0])),
                ("header alone, escaped", _items(json.dumps({"o": _HEADER.split("\n\n")[0]}))),
                ("a plain string", "Script running with cell ID 3\n" + _HEADER),
                ("two marks in one text", _items(_HEADER + _HEADER)),
                ("the fourth item", _items("a", "b", "c", _HEADER))):
            with self.subTest(name):
                self.write([_start(), _out(output)])
                self.assertEqual(self.counts(), (None, 1, 1))

    def test_each_cut_text_counts(self) -> None:
        self.write([_start(), _out(_items(_HEADER, "ok", _HEADER)),
                    _out("Script completed\n" + _HEADER, "function_call_output")])
        self.assertEqual(self.counts(), (None, 2, 3))

    def test_a_text_over_the_default_budget_is_cut_by_its_size(self) -> None:
        """A raised `exec` budget leaves its text whole in the rollout (MR5-e): counted by its
        UTF-8 size, strictly over the budget."""
        rule = leaf_contract.CODEX_EXEC_OUTPUT_BUDGET_BYTES
        self.write([_start(), _out(_items("y" * rule)), _out(_items("y" * (rule + 1))),
                    _out(_items("\u00e9" * (rule // 2) + "y"))])
        self.assertEqual(self.counts(), (None, 3, 2))

    def test_a_whole_output_and_the_quoted_row_are_not_cut(self) -> None:
        row = leaf_contract.WORKSHOP_READER_TOOL_SURFACE_TEXT["codex_cli"]
        self.assertIn("tokens truncated", row)
        self.write([_start(), _out(_items("Output:\n", row)), _out(_items("L1\nL2\n"))])
        self.assertEqual(self.counts(), (None, 2, 0))

    def test_only_rows_after_the_last_task_started_are_the_launch_s(self) -> None:
        self.write([_start(), _out(_items(_HEADER)), _start(), _out(_items("whole"))])
        self.assertEqual(self.counts(), (None, 1, 0))
        self.write([_start(), _out(_items("whole")), _start(), _out(_items(_HEADER))])
        self.assertEqual(self.counts(), (None, 1, 1))

    def test_a_call_input_and_other_rows_are_not_outputs(self) -> None:
        self.write([_start(),
                    {"type": "response_item", "payload": {"type": "custom_tool_call",
                                                          "input": f"text({_HEADER!r})"}},
                    {"type": "event_msg", "payload": {"type": "agent_message",
                                                      "message": _HEADER}},
                    _out(_items("ok"))])
        self.assertEqual(self.counts(), (None, 1, 0))

    def test_what_the_reader_cannot_read_is_a_reason_never_a_zero(self) -> None:
        self.assertIsNotNone(self.scan(None).reason)
        self.assertIsNotNone(self.scan("").reason)
        self.assertIn("0 rollout files", self.scan().reason)
        self.write([_start(), _out(_items("ok"))])
        self.write([_start(), _out(_items("ok"))], day="2026/10/26")
        self.assertIn("2 rollout files", self.scan().reason)
        for name, rows in (("no task_started", [_out(_items("ok"))]),
                           ("a row that is not JSON", [_start(), "{not json"]),
                           ("a row that is not an object", [_start(), "[1]"])):
            with self.subTest(name):
                other = self.config / "x"
                other.mkdir(exist_ok=True)
                self.config = other
                self.write(rows)
                self.assertIsNotNone(self.scan().reason)

    def test_a_cut_text_is_named_by_the_head_of_its_call(self) -> None:
        """For the findings only: each cut text carries the head of the call that returned it,
        paired by `call_id`; a call the rollout does not hold names nothing."""
        call = {"type": "response_item", "payload": {
            "type": "custom_tool_call", "call_id": "c", "name": "exec",
            "input": "const r = await tools.exec_command({cmd: 'cat host_verdict.json'});\n"
                     + "x" * 400}}
        self.write([_start(), call, _out(_items(_HEADER, _HEADER)),
                    {"type": "response_item", "payload": {
                        "type": "function_call_output", "call_id": "gone", "output": _HEADER}}])
        got = self.scan()
        self.assertEqual(got.cut_texts, 3)
        head = got.cut_calls[0]
        self.assertTrue(head.startswith("const r = await tools.exec_command({cmd: 'cat host_verdict"))
        self.assertEqual(len(head), codex_rollout.CALL_HEAD_CHARS)
        self.assertEqual(got.cut_calls, (head, head, ""))

    def test_a_collaboration_call_is_a_delegation(self) -> None:
        """MR5-f: a read delegated to a sub-agent is in neither record; its call, in the
        launch's own rows, is counted — by name, and by namespace for a tool not named."""
        def call(name, **extra):
            return {"type": "response_item", "payload": {
                "type": "function_call", "call_id": name, "name": name, "arguments": "{}",
                **extra}}
        rows = [_start(), *[call(n) for n in sorted(codex_rollout.COLLABORATION_TOOLS)],
                call("future_tool", namespace="collaboration"), call("wait"),
                _out(_items("ok"))]
        self.write(rows)
        got = self.scan()
        self.assertEqual((got.delegations, got.cut_texts),
                         (len(codex_rollout.COLLABORATION_TOOLS) + 1, 0))
        # Only the launch's own rows: an earlier launch's spawn is not this one's.
        self.write([_start(), call("spawn_agent"), _start(), _out(_items("ok"))])
        self.assertEqual(self.scan().delegations, 0)

    def test_the_glob_names_the_whole_thread_id(self) -> None:
        self.write([_start(), _out(_items(_HEADER))], thread="prefix-" + _THREAD)
        self.assertIn("0 rollout files", self.scan().reason)
        self.assertIn("0 rollout files", self.scan("../x").reason)


class CommandCaptureTests(unittest.TestCase):
    """`_codex_command_output`: what the host's capture says each command printed."""

    def take(self, line: str) -> list[tuple[str, int]]:
        commands: list[tuple[str, int]] = []
        wc._codex_command_output(line, wc._parse_codex_line(line), commands)
        return commands

    def test_a_command_execution_records_its_whole_output_in_utf8_bytes(self) -> None:
        line = json.dumps({"type": "item.completed", "item": {
            "type": "command_execution", "command": "/usr/bin/zsh -c 'cat a'",
            "aggregated_output": "é" * 10 + "x"}})
        self.assertEqual(self.take(line), [("/usr/bin/zsh -c 'cat a'", 21)])

    def test_other_events_and_items_record_nothing(self) -> None:
        for event in ({"type": "item.started", "item": {"type": "command_execution",
                                                        "aggregated_output": "x" * 50000}},
                      {"type": "item.completed", "item": {"type": "agent_message",
                                                          "text": "x" * 50000}},
                      {"type": "turn.completed"}):
            with self.subTest(event=event["type"]):
                self.assertEqual(self.take(json.dumps(event)), [])
        self.assertEqual(self.take("not json"), [])

    def test_a_record_the_host_clipped_counts_over_every_budget(self) -> None:
        reassembler = wc._LineReassembler(limit=100)
        line = json.dumps({"type": "item.completed", "item": {
            "type": "command_execution", "command": "cat big",
            "aggregated_output": "x" * 500}}) + "\n"
        (clipped,) = reassembler.feed(line)
        self.assertIsNone(wc._parse_codex_line(clipped))
        ((_, size),) = self.take(clipped)
        self.assertGreater(size, leaf_contract.CODEX_EXEC_OUTPUT_BUDGET_BYTES)

    def test_the_spawn_collects_the_list_it_hands_the_reader(self) -> None:
        """`_spawn_codex_json_leaf` sets `command_outputs` from the list its reader filled."""
        seen: dict = {}

        def read(self, *args, commands, **kwargs):
            commands.append(("cat x", 7))
            seen["commands"] = commands
            return wc.ProcResult(0, "done", "")

        conductor = wc.Conductor.__new__(wc.Conductor)
        original = wc.Conductor._read_codex_json_leaf
        wc.Conductor._read_codex_json_leaf = read  # type: ignore[assignment]
        try:
            result = conductor._spawn_codex_json_leaf([], {}, "a", prompt_text="p")
        finally:
            wc.Conductor._read_codex_json_leaf = original  # type: ignore[assignment]
        self.assertEqual(result.command_outputs, [("cat x", 7)])
        self.assertIs(result.command_outputs, seen["commands"])


if __name__ == "__main__":
    unittest.main()
