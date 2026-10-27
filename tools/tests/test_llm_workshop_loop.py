"""The host tool-use loop of an HTTP workshop leaf (`tools/llm_workshop_loop.py`, issue #534
PR-J3-b), driven against the loopback stand-ins of both dialects (`http_stand_ins`).

What each class pins: the request shape per dialect (one declaration table, two wire shapes;
the cache breakpoints on a copy; the first user message a block list); the reply readers,
streamed and buffered, including a 60 KB argument split across many deltas and many writes
(MH1); the execution rule (calls run because the reply holds them, not because of its stop
reason) and the truncation rule (a cut reply is neither run nor appended); every end of the
loop, the deadline before a request and in flight included; the answering of parallel and
malformed calls; the record and its redaction; and the five file tools' arithmetic and refusals
on a real tree."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from tools import llm_workshop_loop as loop
from tools.pure_leaf import (
    HTTP_FILE_TOOL_DECLARATIONS,
    HTTP_GREP_MAX_MATCHES,
    HTTP_READ_PAGE_BYTES,
    HTTP_READ_PAGE_LINES,
)
from tools.tests.http_stand_ins import (
    ScriptedChatApi,
    ScriptedMessagesApi,
    chat_call,
    tool_use,
)

SECRET = "sk-test-secret-0123456789"
ENV = {"LOOP_KEY": SECRET}


def _entry(provider: str, base_url: str, **overrides: Any) -> SimpleNamespace:
    fields = {"provider": provider, "model": "configured-model", "base_url": base_url,
              "api_key_env": "LOOP_KEY", "timeout_s": None, "max_output_tokens": None,
              "stream": True, "effort": ""}
    fields.update(overrides)
    return SimpleNamespace(**fields)


TOOLS = [dict(d) for d in HTTP_FILE_TOOL_DECLARATIONS]


class _Base(unittest.TestCase):
    dialect = "anthropic_api"

    def setUp(self) -> None:
        self.servers: list[Any] = []
        self.rows: list[dict] = []
        self.calls: list[tuple[str, dict]] = []

    def tearDown(self) -> None:
        for server in self.servers:
            server.close()

    def api(self, script: list, **attrs: Any) -> Any:
        cls = ScriptedMessagesApi if self.dialect == "anthropic_api" else ScriptedChatApi
        server = cls(script)
        for k, v in attrs.items():
            setattr(server, k, v)
        self.servers.append(server)
        return server

    def run_loop(self, server: Any, *, history: loop.LoopHistory | None = None,
                 call_tool: Any = None, deadline_s: float = 60.0, tools: Any = None,
                 **entry_overrides: Any) -> loop.LoopResult:
        entry = _entry(self.dialect, server.base_url, **entry_overrides)

        def default_tool(name: str, args: dict) -> dict:
            self.calls.append((name, args))
            return {"ok": True, "echo": name}

        return loop.run_workshop_loop(
            entry, system="SYSTEM", history=history if history is not None
            else loop.LoopHistory(), prompt="PROMPT", tools=tools or TOOLS,
            call_tool=call_tool or default_tool, deadline=time.monotonic() + deadline_s,
            deadline_name="lineage_timeout_s", env=ENV, record=self.rows.append)


def _reply_anthropic(calls: list[dict], stop: str = "tool_use", text: str = "") -> tuple:
    blocks = ([{"type": "text", "text": text}] if text else []) + calls
    return blocks, stop


class RequestShapeTests(_Base):
    def test_one_declaration_table_two_wire_shapes(self) -> None:
        a = loop.anthropic_tools(TOOLS)
        o = loop.openai_tools(TOOLS)
        self.assertEqual([t["name"] for t in a], [t["function"]["name"] for t in o])
        for x, y in zip(a, o):
            self.assertEqual(x["input_schema"], y["function"]["parameters"])
            self.assertEqual(x["description"], y["function"]["description"])
            self.assertEqual(y["type"], "function")

    def test_anthropic_cache_breakpoints_on_a_copy(self) -> None:
        server = self.api([_reply_anthropic([tool_use("t1", "list_files", {})]),
                           _reply_anthropic([], "end_turn", "done")])
        history = loop.LoopHistory()
        result = self.run_loop(server, history=history)
        self.assertEqual(result.ended, "end_turn")
        first, second = server.main_requests()
        self.assertEqual(first["system"], [{"type": "text", "text": "SYSTEM",
                                            "cache_control": {"type": "ephemeral"}}])
        # The first user message is a block list, its last block marked.
        self.assertEqual(first["messages"][0]["content"],
                         [{"type": "text", "text": "PROMPT",
                           "cache_control": {"type": "ephemeral"}}])
        # The second request marks the LAST user message (the tool result), and only it.
        marked = [(i, b) for i, m in enumerate(second["messages"])
                  for b in (m["content"] if isinstance(m["content"], list) else [])
                  if "cache_control" in b]
        self.assertEqual(len(marked), 1)
        self.assertEqual(marked[0][0], len(second["messages"]) - 1)
        self.assertEqual(marked[0][1]["type"], "tool_result")
        # The stored history carries no marker.
        self.assertNotIn("cache_control", json.dumps(history.messages))

    def test_openai_request_has_no_cache_field_and_the_function_shape(self) -> None:
        self.dialect = "openai_compatible"
        server = self.api([({"content": "done"}, "stop")])
        self.run_loop(server, effort="high")
        [body] = server.main_requests()
        self.assertNotIn("cache_control", json.dumps(body))
        self.assertEqual(body["messages"][0], {"role": "system", "content": "SYSTEM"})
        self.assertEqual(body["messages"][1], {"role": "user", "content": "PROMPT"})
        self.assertEqual(body["tools"][0]["type"], "function")
        self.assertEqual(body["stream_options"], {"include_usage": True})
        self.assertEqual(body["reasoning_effort"], "high")
        self.assertEqual(body["max_tokens"], 32768)


class _Mh1Mixin:
    """MH1: a 60 KB `write_file` content split across many deltas assembles byte-identically,
    streamed and buffered, with the body also written in small pieces; a 4-byte UTF-8
    character is split across two deltas (as its two JSON surrogate escapes) and across two
    writes."""

    def _big(self) -> str:
        return ("x" * 997 + "\U0001F600" + "é\n") * 61

    def _script(self, content: str) -> list:
        raise NotImplementedError

    def check(self, stream: bool) -> None:
        content = self._big()
        self.assertGreater(len(content.encode()), 60_000)
        server = self.api(self._script(content), delta_chars=7, write_bytes=513)
        result = self.run_loop(server, stream=stream)
        self.assertIsNone(result.transport_error, result)
        [(name, args)] = self.calls
        self.assertEqual(name, "write_file")
        self.assertEqual(args["content"].encode("utf-8"), content.encode("utf-8"))

    def test_streamed(self) -> None:
        self.check(True)

    def test_buffered(self) -> None:
        self.check(False)


class Mh1AnthropicTests(_Mh1Mixin, _Base):
    def _script(self, content: str) -> list:
        # ensure_ascii: the emoji travels as two `\\ud83d\\ude00` escapes, and 7-char deltas
        # split them.
        raw = json.dumps({"path": "big.txt", "content": content}, ensure_ascii=True)
        return [_reply_anthropic([tool_use("t1", "write_file", {}, raw_input=raw)]),
                _reply_anthropic([], "end_turn", "done")]


class Mh1OpenAiTests(_Mh1Mixin, _Base):
    dialect = "openai_compatible"

    def _script(self, content: str) -> list:
        raw = json.dumps({"path": "big.txt", "content": content}, ensure_ascii=True)
        return [({"tool_calls": [chat_call("c1", "write_file", raw)]}, "tool_calls"),
                ({"content": "done"}, "stop")]


class _EndsMixin:
    def call(self, cid: str, name: str, args: dict) -> Any:
        raise NotImplementedError

    def reply(self, calls: list, stop: str, text: str = "") -> tuple:
        raise NotImplementedError

    @property
    def tool_stop(self) -> str:
        raise NotImplementedError

    @property
    def cut_stop(self) -> str:
        raise NotImplementedError

    def test_no_tool_call_ends_the_turn(self) -> None:
        server = self.api([self.reply([], self.end_stop, "all done")])
        result = self.run_loop(server)
        self.assertEqual(result.ended, self.end_stop)
        self.assertEqual(result.text, "all done")
        self.assertFalse(result.tool_call_made)
        self.assertEqual(result.turns, 1)

    def test_tool_calls_are_executed_by_presence_not_stop_reason(self) -> None:
        server = self.api([self.reply([self.call("a", "list_files", {})], self.end_stop),
                           self.reply([], self.end_stop, "done")])
        result = self.run_loop(server)
        self.assertEqual(self.calls, [("list_files", {})])
        self.assertEqual(result.turns, 2)
        self.assertTrue(result.tool_call_made)

    def test_truncated_reply_is_neither_executed_nor_appended(self) -> None:
        server = self.api([self.reply([self.call("a", "write_file",
                                                 {"path": "f", "content": "x"})],
                                      self.cut_stop)])
        history = loop.LoopHistory()
        result = self.run_loop(server, history=history)
        self.assertEqual(self.calls, [])
        self.assertEqual(result.ended, loop.ENDED_TRUNCATED)
        self.assertEqual(result.truncated, "max_output_tokens=32768 inside a `write_file` call")
        self.assertEqual([m["role"] for m in history.messages], ["user"])

    def test_wrapper_ended_answers_the_rest_without_running(self) -> None:
        def tool(name: str, args: dict) -> dict:
            self.calls.append((name, args))
            if name == "derive":
                return {"ok": False, "ended": True, "category": "max_wrapper_calls"}
            return {"ok": True}

        tools = TOOLS + [{"name": "derive", "description": "d",
                          "input_schema": {"type": "object", "properties": {},
                                           "additionalProperties": False}}]
        server = self.api([self.reply([self.call("a", "list_files", {}),
                                       self.call("b", "derive", {}),
                                       self.call("c", "list_files", {})], self.tool_stop)])
        history = loop.LoopHistory()
        result = self.run_loop(server, call_tool=tool, tools=tools, history=history)
        self.assertEqual([c[0] for c in self.calls], ["list_files", "derive"])
        self.assertEqual(result.ended, loop.ENDED_BY_WRAPPER)
        results = self.results_in(history)
        self.assertEqual(len(results), 3)
        self.assertEqual(results[2]["category"], loop.LINEAGE_ENDED)

    def test_malformed_calls_are_answered_and_the_loop_continues(self) -> None:
        server = self.api([
            self.reply([self.call("a", "no_such_tool", {}),
                        self.call("b", "read_file", {"start_line": 2}),
                        self.call("c", "list_files", {"extra": 1}),
                        self.call("d", "read_file", {"path": 3})], self.tool_stop),
            self.reply([], self.end_stop, "done")])
        history = loop.LoopHistory()
        result = self.run_loop(server, history=history)
        self.assertEqual(self.calls, [])
        self.assertEqual(result.ended, self.end_stop)
        results = self.results_in(history)
        self.assertEqual([r["category"] for r in results], [loop.TOOL_ARGUMENTS_INVALID] * 4)
        self.assertIn("no tool named", results[0]["findings"])
        self.assertIn("required key 'path'", results[1]["findings"])
        self.assertIn("does not take", results[2]["findings"])
        self.assertIn("must be a JSON string", results[3]["findings"])

    def test_parallel_calls_answered_in_order(self) -> None:
        server = self.api([self.reply([self.call("a", "list_files", {}),
                                       self.call("b", "grep_file",
                                                 {"path": "x", "pattern": "y"})],
                                      self.tool_stop),
                           self.reply([], self.end_stop, "done")])
        history = loop.LoopHistory()
        self.run_loop(server, history=history)
        self.assertEqual([c[0] for c in self.calls], ["list_files", "grep_file"])
        self.check_parallel_shape(history)

    def test_transport_error_ends_with_usage_of_earlier_requests(self) -> None:
        server = self.api([self.reply([self.call("a", "list_files", {})], self.tool_stop),
                           {"status": 529, "body": f"overloaded {SECRET}"}])
        result = self.run_loop(server)
        self.assertEqual(result.ended, loop.ENDED_TRANSPORT)
        self.assertIn("HTTP 529", result.transport_error)
        self.assertNotIn(SECRET, result.transport_error)
        self.assertIsNotNone(result.usage)
        self.assertEqual(result.usage["provider_details"]["requests"], 1)
        self.assertFalse(result.timed_out)

    def test_a_severed_stream_is_a_transport_error_and_runs_nothing(self) -> None:
        server = self.api([{"sever": self.reply([self.call("a", "list_files", {})],
                                                self.tool_stop)}])
        result = self.run_loop(server)
        self.assertEqual(self.calls, [])
        self.assertEqual(result.ended, loop.ENDED_TRANSPORT)
        self.assertIn("stream interrupted", result.transport_error)

    def test_deadline_before_a_request_sends_nothing(self) -> None:
        server = self.api([self.reply([], self.end_stop, "x")])
        result = self.run_loop(server, deadline_s=-1)
        self.assertTrue(result.timed_out)
        self.assertEqual(result.ended, "lineage_timeout_s")
        self.assertEqual(server.main_requests(), [])

    def test_in_flight_deadline_is_a_timeout_when_the_deadline_binds(self) -> None:
        server = self.api([{"sleep": 3, "then": self.reply([], self.end_stop, "late")}])
        result = self.run_loop(server, deadline_s=1, timeout_s=600)
        self.assertTrue(result.timed_out)
        self.assertIsNone(result.transport_error)
        self.assertEqual(result.ended, "lineage_timeout_s")

    def test_in_flight_deadline_is_transport_when_timeout_s_binds(self) -> None:
        server = self.api([{"sleep": 3, "then": self.reply([], self.end_stop, "late")}])
        result = self.run_loop(server, deadline_s=600, timeout_s=1)
        self.assertFalse(result.timed_out)
        self.assertEqual(result.ended, loop.ENDED_TRANSPORT)
        self.assertIn("response_deadline_exceeded", result.transport_error)

    def test_unknown_stop_reason_ends_with_it_recorded(self) -> None:
        server = self.api([self.reply([], "refusal", "no")])
        result = self.run_loop(server)
        self.assertEqual(result.ended, "refusal")
        self.assertIsNone(result.transport_error)

    def test_record_rows_and_redaction(self) -> None:
        server = self.api([self.reply([self.call("a", "write_file",
                                                 {"path": "k", "content": SECRET})],
                                      self.tool_stop),
                           self.reply([], self.end_stop, "done")])
        history = loop.LoopHistory()
        self.run_loop(server, history=history)
        events = [r["event"] for r in self.rows]
        self.assertEqual(events, ["request", "response", "tool_call", "tool_result",
                                  "request", "response", "ended"])
        self.assertNotIn(SECRET, json.dumps(self.rows))
        first, second = [r for r in self.rows if r["event"] == "request"]
        self.assertEqual(first["messages_from"], 0)
        self.assertEqual(len(first["messages"]), 1)
        # The second request row holds only what was appended since the first.
        self.assertEqual(second["messages_from"], 1)
        self.assertEqual(len(second["messages"]), len(history.messages) - 2)

    def test_missing_key_is_a_transport_error_with_no_request(self) -> None:
        server = self.api([self.reply([], self.end_stop, "x")])
        entry = _entry(self.dialect, server.base_url)
        result = loop.run_workshop_loop(
            entry, system="S", history=loop.LoopHistory(), prompt="P", tools=TOOLS,
            call_tool=lambda n, a: {}, deadline=time.monotonic() + 30,
            deadline_name="d", env={})
        self.assertIn("missing_api_key", result.transport_error)
        self.assertEqual(server.main_requests(), [])


class StreamReaderTests(unittest.TestCase):
    def test_a_block_left_unstopped_is_a_transport_error(self) -> None:
        """A `message_stop` with a `tool_use` block that never got its `content_block_stop`:
        the partial arguments are never a call."""
        frames = [
            ("message_start", json.dumps({"message": {"model": "m", "usage": {}}})),
            ("content_block_start", json.dumps({"index": 0, "content_block": {
                "type": "tool_use", "id": "t", "name": "write_file", "input": {}}})),
            ("content_block_delta", json.dumps({"index": 0, "delta": {
                "type": "input_json_delta", "partial_json": '{"path": "a", "content": "b"}'}})),
            ("message_delta", json.dumps({"delta": {"stop_reason": "tool_use"}})),
            ("message_stop", "{}")]
        reply, error = loop._anthropic_stream_reply(frames)
        self.assertIsNone(reply)
        self.assertIn("never stopped", error)
        stopped = [*frames[:3], ("content_block_stop", json.dumps({"index": 0})), *frames[3:]]
        reply, error = loop._anthropic_stream_reply(stopped)
        self.assertIsNone(error)
        self.assertEqual(reply.calls[0].arguments, {"path": "a", "content": "b"})


class StreamEndTests(unittest.TestCase):
    def test_an_error_event_wins_over_message_stop(self) -> None:
        frames = [("message_start", json.dumps({"message": {"model": "m", "usage": {}}})),
                  ("error", json.dumps({"error": {"type": "overloaded_error",
                                                  "message": "Overloaded"}})),
                  ("message_stop", "{}")]
        reply, error = loop._anthropic_stream_reply(frames)
        self.assertIsNone(reply)
        self.assertIn("overloaded_error", error)


class SeveredTeardownTests(_Base):
    def test_a_complete_stream_whose_teardown_is_severed_is_not_a_transport_death(self) -> None:
        """The provider's terminator is on the wire, then the rest of the declared length never
        comes and the request's own `timeout_s` runs out: the reply is complete and is used
        (the pure path's rule), not a transport death."""
        for dialect in ("anthropic_api", "openai_compatible"):
            with self.subTest(dialect=dialect):
                self.dialect = dialect
                self.calls = []
                done = (_reply_anthropic([], "end_turn", "all done") if dialect == "anthropic_api"
                        else ({"content": "all done"}, "stop"))
                server = self.api([done], short_by=64, hold_s=4)
                result = self.run_loop(server, timeout_s=2)
                self.assertIsNone(result.transport_error, result)
                self.assertEqual(result.text, "all done")


class UsageSumTests(_Base):
    def test_usage_is_summed_over_the_launch_s_requests_up_to_a_transport_error(self) -> None:
        server = self.api([_reply_anthropic([tool_use("a", "list_files", {})]),
                           _reply_anthropic([tool_use("b", "list_files", {})]),
                           {"status": 500, "body": "boom"}])
        server.usage = {"input_tokens": 7, "output_tokens": 3,
                        "cache_read_input_tokens": 5, "cache_creation_input_tokens": 2}
        result = self.run_loop(server)
        self.assertIsNotNone(result.transport_error)
        self.assertEqual((result.usage["input_tokens"], result.usage["output_tokens"],
                          result.usage["cache_read_input_tokens"],
                          result.usage["cache_creation_input_tokens"]), (14, 6, 10, 4))
        self.assertEqual(result.usage["provider_details"]["requests"], 2)


class AnthropicEndsTests(_EndsMixin, _Base):
    end_stop = "end_turn"
    tool_stop = "tool_use"
    cut_stop = "max_tokens"

    def call(self, cid: str, name: str, args: dict) -> dict:
        return tool_use(cid, name, args)

    def reply(self, calls: list, stop: str, text: str = "") -> tuple:
        return _reply_anthropic(calls, stop, text)

    def results_in(self, history: loop.LoopHistory) -> list[dict]:
        return [json.loads(b["content"]) for m in history.messages if m["role"] == "user"
                for b in m["content"] if b.get("type") == "tool_result"]

    def check_parallel_shape(self, history: loop.LoopHistory) -> None:
        user = [m for m in history.messages if m["role"] == "user"
                and any(b.get("type") == "tool_result" for b in m["content"])]
        self.assertEqual(len(user), 1)
        self.assertEqual([b["tool_use_id"] for b in user[0]["content"]], ["a", "b"])

    def test_thinking_blocks_are_replayed_verbatim(self) -> None:
        server = self.api([([{"type": "thinking", "thinking": "hmm", "signature": "S1"},
                             tool_use("a", "list_files", {})], "tool_use"),
                           _reply_anthropic([], "end_turn", "done")])
        history = loop.LoopHistory()
        self.run_loop(server, history=history)
        second = server.main_requests()[1]
        assistant = second["messages"][1]
        self.assertEqual(assistant["content"][0],
                         {"type": "thinking", "thinking": "hmm", "signature": "S1"})

    def test_warm_prompt_after_a_truncation_merges_into_the_last_user_message(self) -> None:
        server = self.api([_reply_anthropic([tool_use("a", "list_files", {})], "max_tokens"),
                           _reply_anthropic([], "end_turn", "ok")])
        history = loop.LoopHistory()
        self.run_loop(server, history=history)
        self.rows = []
        entry = _entry(self.dialect, server.base_url)
        loop.run_workshop_loop(
            entry, system="S", history=history, prompt="WARM REPAIR FINDINGS", tools=TOOLS,
            call_tool=lambda n, a: {"ok": True}, deadline=time.monotonic() + 60,
            deadline_name="d", env=ENV, record=self.rows.append)
        roles = [m["role"] for m in server.main_requests()[1]["messages"]]
        self.assertEqual(roles, ["user"])
        self.assertEqual(len(server.main_requests()[1]["messages"][0]["content"]), 2)
        # The record re-carries the merged message whole, from its index.
        [request] = [r for r in self.rows if r["event"] == "request"]
        self.assertEqual(request["messages_from"], 0)
        self.assertIn("WARM REPAIR FINDINGS", json.dumps(request["messages"]))
        self.assertIn("PROMPT", json.dumps(request["messages"]))

    def test_rebuilding_from_the_rows_gives_the_history(self) -> None:
        """Replacing the rebuilt history from each request row's `messages_from` on (plus the
        last appended reply, which only the next row or none carries) gives the history."""
        server = self.api([_reply_anthropic([tool_use("a", "list_files", {})]),
                           _reply_anthropic([tool_use("b", "list_files", {})], "max_tokens"),
                           _reply_anthropic([tool_use("c", "list_files", {})]),
                           _reply_anthropic([], "end_turn", "ok")])
        history = loop.LoopHistory()
        self.run_loop(server, history=history)
        self.run_loop(server, history=history)
        rebuilt: list = []
        for row in self.rows:
            if row["event"] == "request":
                rebuilt[row["messages_from"]:] = row["messages"]
        self.assertEqual(rebuilt, history.messages[:len(rebuilt)])
        self.assertEqual(len(rebuilt), len(history.messages) - 1)

    def test_a_context_window_stop_is_a_truncation(self) -> None:
        server = self.api([_reply_anthropic([tool_use("a", "list_files", {})],
                                            "model_context_window_exceeded")])
        result = self.run_loop(server)
        self.assertEqual(self.calls, [])
        self.assertEqual(result.ended, loop.ENDED_TRUNCATED)
        self.assertIn("context window", result.truncated)

    def test_a_failed_tool_result_is_marked_is_error(self) -> None:
        server = self.api([_reply_anthropic([tool_use("a", "no_such", {}),
                                             tool_use("b", "list_files", {})]),
                           _reply_anthropic([], "end_turn", "ok")])
        history = loop.LoopHistory()
        self.run_loop(server, history=history)
        bad, good = [b for m in history.messages if m["role"] == "user"
                     for b in m["content"] if b.get("type") == "tool_result"]
        self.assertIs(bad.get("is_error"), True)
        self.assertNotIn("is_error", good)


class OpenAiEndsTests(_EndsMixin, _Base):
    dialect = "openai_compatible"
    end_stop = "stop"
    tool_stop = "tool_calls"
    cut_stop = "length"

    def call(self, cid: str, name: str, args: dict) -> dict:
        return chat_call(cid, name, args)

    def reply(self, calls: list, stop: str, text: str = "") -> tuple:
        message: dict = {"content": text or None}
        if calls:
            message["tool_calls"] = calls
        return message, stop

    def results_in(self, history: loop.LoopHistory) -> list[dict]:
        return [json.loads(m["content"]) for m in history.messages if m["role"] == "tool"]

    def check_parallel_shape(self, history: loop.LoopHistory) -> None:
        tool_msgs = [m for m in history.messages if m["role"] == "tool"]
        self.assertEqual([m["tool_call_id"] for m in tool_msgs], ["a", "b"])

    def test_reasoning_content_is_recorded_and_replayed_streamed_and_buffered(self) -> None:
        """A thinking server that returns `reasoning_content` with `tools` requires it back on
        every later request (DeepSeek: HTTP 400 otherwise); a reply with none sends none."""
        for stream in (True, False):
            with self.subTest(stream=stream):
                self.rows = []
                server = self.api([({"reasoning_content": "THOUGHT",
                                     "tool_calls": [chat_call("a", "list_files", {})]},
                                    "tool_calls"),
                                   ({"tool_calls": [chat_call("b", "list_files", {})]},
                                    "tool_calls"),
                                   ({"content": "done"}, "stop")])
                self.run_loop(server, stream=stream)
                second, third = server.main_requests()[1:]
                first_reply = second["messages"][2]
                self.assertEqual(first_reply["reasoning_content"], "THOUGHT")
                self.assertNotIn("reasoning_content", third["messages"][4])
                self.assertIn("THOUGHT", json.dumps([r for r in self.rows
                                                     if r["event"] == "response"]))

    def test_assistant_replay_shape(self) -> None:
        server = self.api([({"tool_calls": [chat_call("a", "list_files", {})]}, "tool_calls"),
                           ({"content": "done"}, "stop")])
        self.run_loop(server)
        assistant = server.main_requests()[1]["messages"][2]
        self.assertEqual(assistant, {"role": "assistant", "content": None, "tool_calls": [
            {"id": "a", "type": "function",
             "function": {"name": "list_files", "arguments": "{}"}}]})

    def test_unparseable_arguments_are_answered(self) -> None:
        server = self.api([({"tool_calls": [chat_call("a", "list_files", "{not json")]},
                            "tool_calls"), ({"content": "done"}, "stop")])
        history = loop.LoopHistory()
        self.run_loop(server, history=history)
        [result] = self.results_in(history)
        self.assertEqual(result["category"], loop.TOOL_ARGUMENTS_INVALID)
        self.assertIn("not JSON", result["findings"])

    def test_usage_carries_cached_tokens(self) -> None:
        server = self.api([({"content": "done"}, "stop")])
        result = self.run_loop(server)
        self.assertEqual(result.usage["cached_tokens"], 1)
        self.assertEqual(result.usage["input_tokens"], 3)


class FileToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "tree"
        self.root.mkdir()
        self.outside = Path(self.tmp.name) / "outside"
        self.outside.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def put(self, name: str, text: str) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_the_declarations_state_the_constants(self) -> None:
        read = next(d for d in HTTP_FILE_TOOL_DECLARATIONS if d["name"] == "read_file")
        self.assertIn(f"{HTTP_READ_PAGE_LINES:,}", read["description"])
        self.assertIn(f"{HTTP_READ_PAGE_BYTES:,}", read["description"])
        grep = next(d for d in HTTP_FILE_TOOL_DECLARATIONS if d["name"] == "grep_file")
        self.assertIn(str(HTTP_GREP_MAX_MATCHES), grep["description"])

    def test_read_pages_at_the_line_ceiling(self) -> None:
        self.put("f", "".join(f"l{i}\n" for i in range(1, 5001)))
        page = loop.read_file(self.root, "f")
        self.assertEqual((page["start_line"], page["end_line"], page["total_lines"],
                          page["next_start_line"]), (1, 2000, 5000, 2001))
        last = loop.read_file(self.root, "f", 4001)
        self.assertEqual((last["end_line"], last["next_start_line"]), (5000, None))
        self.assertEqual(last["text"].splitlines()[-1], "l5000")

    def test_read_stops_at_the_last_whole_line_under_the_byte_cap(self) -> None:
        line = "é" * 1000  # 2,000 bytes
        self.put("f", (line + "\n") * 100)
        page = loop.read_file(self.root, "f")
        self.assertLessEqual(len(page["text"].encode()), HTTP_READ_PAGE_BYTES)
        expected = (HTTP_READ_PAGE_BYTES + 1) // 2001
        self.assertEqual(page["end_line"], expected)
        self.assertEqual(page["next_start_line"], expected + 1)

    def test_an_over_cap_line_is_skipped_and_named(self) -> None:
        self.put("f", "a\n" + "b" * (HTTP_READ_PAGE_BYTES + 1) + "\nc\n")
        page = loop.read_file(self.root, "f")
        self.assertEqual(page["text"], "a")
        self.assertEqual(page["skipped_line"], {"line": 2, "bytes": HTTP_READ_PAGE_BYTES + 1})
        self.assertEqual(page["next_start_line"], 3)
        self.assertEqual(loop.read_file(self.root, "f", 3)["text"], "c")

    def test_max_lines_is_clamped_and_reported(self) -> None:
        self.put("f", "x\n" * 3000)
        page = loop.read_file(self.root, "f", 1, 5000)
        self.assertEqual(page["end_line"], HTTP_READ_PAGE_LINES)
        self.assertIn("max_lines_clamped", page)

    def test_start_past_the_end_is_a_finding(self) -> None:
        self.put("f", "x\n")
        self.assertFalse(loop.read_file(self.root, "f", 3)["ok"])

    def test_grep_numbers_caps_and_refuses(self) -> None:
        self.put("f", "".join(("hit\n" if i % 2 else "miss\n") for i in range(1000)))
        found = loop.grep_file(self.root, "f", "^hit$", 10)
        self.assertEqual(found["total_matches"], 500)
        self.assertEqual([m["line"] for m in found["matches"]][:3], [2, 4, 6])
        self.assertEqual(len(found["matches"]), 10)
        self.assertTrue(found["truncated"])
        self.assertEqual(len(loop.grep_file(self.root, "f", "hit", 999)["matches"]),
                         HTTP_GREP_MAX_MATCHES)
        self.assertFalse(loop.grep_file(self.root, "f", "(")["ok"])
        self.put("long", "z" * (HTTP_READ_PAGE_BYTES * 2) + "\n")
        cut = loop.grep_file(self.root, "long", "z")
        self.assertTrue(cut["matches"][0]["cut"])
        self.assertLessEqual(len(cut["matches"][0]["text"].encode()), HTTP_READ_PAGE_BYTES)
        self.assertFalse(loop.grep_file(self.root, "", "z")["ok"])

    def test_write_refusals(self) -> None:
        reserved = {"CONTRACT.md", "tests.md"}
        call = lambda path, content="x": loop.file_tool(
            self.root, reserved, "write_file", {"path": path, "content": content})
        for bad in ("../x", "/etc/x", "a\\b", "a//b", "./a", "", "a\x00b", "d/a\x00b"):
            self.assertFalse(call(bad)["ok"], bad)
        self.assertFalse(call("CONTRACT.md")["ok"])
        self.assertFalse(call("tests.md")["ok"])
        os.symlink(self.outside, self.root / "link")
        self.assertFalse(call("link/x")["ok"])
        self.assertEqual(list(self.outside.iterdir()), [])
        os.symlink(self.outside / "target", self.root / "flink")
        self.assertFalse(call("flink")["ok"])
        self.assertFalse((self.outside / "target").exists())
        self.assertTrue(call("deep/er/f.txt", "hello")["ok"])
        self.assertEqual((self.root / "deep/er/f.txt").read_text(), "hello")
        # A parent replaced by a link between two calls.
        (self.root / "deep/er/f.txt").unlink()
        (self.root / "deep/er").rmdir()
        os.symlink(self.outside, self.root / "deep/er")
        self.assertFalse(call("deep/er/g.txt")["ok"])
        self.assertEqual(list(self.outside.iterdir()), [])

    def test_edit_counts_and_list_sizes(self) -> None:
        self.put("f", "one two two\n")
        edit = lambda old, new: loop.file_tool(
            self.root, set(), "edit_file", {"path": "f", "old_text": old, "new_text": new})
        self.assertIn("0 times", edit("three", "x")["findings"])
        self.assertIn("2 times", edit("two", "x")["findings"])
        self.assertTrue(edit("one", "1")["ok"])
        self.assertEqual((self.root / "f").read_text(), "1 two two\n")
        os.symlink(self.outside, self.root / "l")
        listed = loop.list_files(self.root)
        self.assertEqual(listed["files"], [{"path": "f", "bytes": 10}])
        self.assertEqual(listed["links_refused"], ["l"])


if __name__ == "__main__":
    unittest.main()
