"""Loopback stand-ins for the two HTTP dialects a leaf speaks (issue #534 PR-J3-b).

`ScriptedMessagesApi` (the Anthropic Messages API) moved here from `test_bwrap_simulation.py`,
where the real claude CLI talks to it, and was extended for the host loop of an HTTP workshop
leaf (`tools/llm_workshop_loop.py`): it honours the body's `stream` (server-sent events when
true, one JSON body when false), reports cache usage, lets a script item name any stop reason,
and can split a tool call's arguments across many deltas and the response body across many
writes. `ScriptedChatApi` is its Chat-Completions twin (`tool_calls` per reply, streamed as
`delta.tool_calls[i]` fragments, `[DONE]`, a usage chunk with `cached_tokens`, any
`finish_reason` — including `"stop"` with `tool_calls`).

A script item is answered to the i-th request whose body offers tools; every other request
(the claude CLI's own side calls) gets a one-word text reply. An item is one of:

* `(blocks, stop)` (Messages) / `(message, finish_reason)` (Chat) — a reply;
* a dict `{"status": <code>, "body": <text>}` — an HTTP error;
* a dict `{"sever": (blocks, stop)}` — that reply streamed with no terminator (no
  `message_stop` / no `[DONE]` and no `finish_reason`);
* a dict `{"sleep": <seconds>, "then": <item>}` — the item, answered after a delay;
* a callable `(body: dict) -> item` — decided from the request.

Request bodies are kept for the assertions. Unbilled: everything is loopback.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar


def _chunks(text: str, size: int) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


class _ScriptedServer:
    """The shared loopback server: subclasses supply `answer(body, item) -> (status,
    content_type, bytes)` and `fallback(body)`."""

    #: Split a tool call's argument JSON into deltas of this many characters.
    delta_chars = 997
    #: Write the response body in pieces of this many bytes (flushing each), so a client read
    #: can end mid-frame and mid-character.
    write_bytes = 0
    #: Declare a `Content-Length` this many bytes LONGER than the body and hold the connection
    #: open `hold_s` seconds before closing: the body arrives whole and the read then outlives
    #: the client's deadline (a trailer that never comes).
    short_by = 0
    hold_s = 0.0

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.bodies: list[bytes] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.bodies.append(body)
                self_main = [b for b in outer.bodies if b'"tools"' in b]
                try:
                    doc = json.loads(body)
                except json.JSONDecodeError:
                    doc = {}
                item: Any = None
                if b'"tools"' in body and len(self_main) <= len(outer.script):
                    item = outer.script[len(self_main) - 1]
                while callable(item):
                    item = item(doc)
                while isinstance(item, dict) and "sleep" in item:
                    time.sleep(float(item["sleep"]))
                    item = item.get("then")
                if isinstance(item, dict) and "status" in item:
                    data = str(item.get("body", "error")).encode()
                    self.send_response(int(item["status"]))
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if item is None:
                    status, ctype, data = outer.fallback(doc)
                else:
                    status, ctype, data = outer.answer(doc, item)
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data) + outer.short_by))
                self.end_headers()
                if outer.write_bytes:
                    for i in range(0, len(data), outer.write_bytes):
                        self.wfile.write(data[i:i + outer.write_bytes])
                        self.wfile.flush()
                else:
                    self.wfile.write(data)
                if outer.hold_s:
                    self.wfile.flush()
                    time.sleep(outer.hold_s)

            do_GET = do_POST

            def log_message(self, *args) -> None:  # BaseHTTPRequestHandler's spelling
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}"

    def answer(self, body: dict, item: Any) -> tuple[int, str, bytes]:  # pragma: no cover
        raise NotImplementedError

    def fallback(self, body: dict) -> tuple[int, str, bytes]:  # pragma: no cover
        raise NotImplementedError

    def main_requests(self) -> list[dict]:
        return [json.loads(b) for b in self.bodies if b'"tools"' in b]

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class ScriptedMessagesApi(_ScriptedServer):
    """An Anthropic Messages API stand-in answering with a SCRIPT (see the module docstring).
    `blocks` are `{"type": "text", "text"}`, `{"type": "tool_use", "id", "name", "input"}` or
    `{"type": "thinking", "thinking", "signature"}` dicts."""

    model = "claude-opus-5-5"
    usage: ClassVar[dict[str, int]] = {
        "input_tokens": 1, "output_tokens": 1,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}

    def _events(self, blocks: list[dict], stop: str, *, terminated: bool = True) -> bytes:
        evs: list[dict] = [{"type": "message_start", "message": {
            "id": "msg_1", "type": "message", "role": "assistant",
            "model": self.model, "content": [], "stop_reason": None,
            "stop_sequence": None, "usage": dict(self.usage)}}]
        for i, block in enumerate(blocks):
            if block["type"] == "text":
                evs += [{"type": "content_block_start", "index": i,
                         "content_block": {"type": "text", "text": ""}},
                        {"type": "content_block_delta", "index": i,
                         "delta": {"type": "text_delta", "text": block["text"]}}]
            elif block["type"] == "thinking":
                evs += [{"type": "content_block_start", "index": i,
                         "content_block": {"type": "thinking", "thinking": "",
                                           "signature": ""}},
                        {"type": "content_block_delta", "index": i,
                         "delta": {"type": "thinking_delta", "thinking": block["thinking"]}},
                        {"type": "content_block_delta", "index": i,
                         "delta": {"type": "signature_delta",
                                   "signature": block.get("signature", "sig")}}]
            else:
                raw = block.get("raw_input")
                if raw is None:
                    raw = json.dumps(block["input"])
                evs.append({"type": "content_block_start", "index": i, "content_block": {
                    "type": "tool_use", "id": block["id"], "name": block["name"],
                    "input": {}}})
                for piece in _chunks(raw, self.delta_chars):
                    evs.append({"type": "content_block_delta", "index": i, "delta": {
                        "type": "input_json_delta", "partial_json": piece}})
            evs.append({"type": "content_block_stop", "index": i})
        evs.append({"type": "message_delta", "delta": {"stop_reason": stop,
                                                       "stop_sequence": None},
                    "usage": {"output_tokens": self.usage.get("output_tokens", 1)}})
        if terminated:
            evs.append({"type": "message_stop"})
        return "".join(f"event: {e['type']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n"
                       for e in evs).encode()

    def _body(self, blocks: list[dict], stop: str) -> bytes:
        content = []
        for block in blocks:
            raw = block.get("raw_input")
            block = {k: v for k, v in block.items() if k != "raw_input"}
            if raw is not None:
                block["input"] = json.loads(raw)
            content.append(block)
        return json.dumps({"id": "msg_1", "type": "message", "role": "assistant",
                           "model": self.model, "content": content, "stop_reason": stop,
                           "usage": dict(self.usage)}, ensure_ascii=False).encode()

    def answer(self, body: dict, item: Any) -> tuple[int, str, bytes]:
        if isinstance(item, dict) and "sever" in item:
            blocks, stop = item["sever"]
            return 200, "text/event-stream", self._events(blocks, stop, terminated=False)
        blocks, stop = item
        if body.get("stream") is True:
            return 200, "text/event-stream", self._events(blocks, stop)
        return 200, "application/json", self._body(blocks, stop)

    def fallback(self, body: dict) -> tuple[int, str, bytes]:
        return self.answer(body, ([{"type": "text", "text": "ok"}], "end_turn"))

    def tool_results(self) -> list[dict]:
        out = []
        for doc in self.main_requests():
            for message in doc.get("messages", []):
                if isinstance(message.get("content"), list):
                    out += [c for c in message["content"] if c.get("type") == "tool_result"]
        return out


class ScriptedChatApi(_ScriptedServer):
    """An OpenAI Chat Completions stand-in answering with a SCRIPT. A reply's `message` is
    `{"content": str | None, "tool_calls": [{"id", "name", "arguments": str}],
    "reasoning_content": str}` (each key optional)."""

    model = "local-model"
    usage: ClassVar[dict[str, Any]] = {"prompt_tokens": 3, "completion_tokens": 2,
                                      "prompt_tokens_details": {"cached_tokens": 1}}

    def _chunks_for(self, message: dict, finish: str | None, *,
                    terminated: bool = True) -> bytes:
        frames: list[dict] = []

        def chunk(delta: dict, finish_reason: str | None = None) -> dict:
            return {"id": "c1", "object": "chat.completion.chunk", "model": self.model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}

        frames.append(chunk({"role": "assistant"}))
        if message.get("reasoning_content"):
            frames.append(chunk({"reasoning_content": message["reasoning_content"]}))
        if message.get("content"):
            for piece in _chunks(message["content"], self.delta_chars):
                frames.append(chunk({"content": piece}))
        for index, call in enumerate(message.get("tool_calls") or ()):
            pieces = _chunks(call.get("arguments", ""), self.delta_chars)
            frames.append(chunk({"tool_calls": [{
                "index": index, "id": call["id"], "type": "function",
                "function": {"name": call["name"], "arguments": pieces[0]}}]}))
            for piece in pieces[1:]:
                frames.append(chunk({"tool_calls": [{
                    "index": index, "function": {"arguments": piece}}]}))
        lines = [f"data: {json.dumps(f, ensure_ascii=False)}\n\n" for f in frames]
        if terminated:
            lines.append(f"data: {json.dumps(chunk({}, finish), ensure_ascii=False)}\n\n")
            lines.append("data: " + json.dumps({"id": "c1", "object": "chat.completion.chunk",
                                                "model": self.model, "choices": [],
                                                "usage": self.usage}) + "\n\n")
            lines.append("data: [DONE]\n\n")
        return "".join(lines).encode()

    def _body(self, message: dict, finish: str | None) -> bytes:
        wire: dict[str, Any] = {"role": "assistant", "content": message.get("content")}
        if message.get("reasoning_content"):
            wire["reasoning_content"] = message["reasoning_content"]
        if message.get("tool_calls"):
            wire["tool_calls"] = [{"id": c["id"], "type": "function",
                                   "function": {"name": c["name"],
                                                "arguments": c.get("arguments", "")}}
                                  for c in message["tool_calls"]]
        return json.dumps({"id": "c1", "object": "chat.completion", "model": self.model,
                           "choices": [{"index": 0, "message": wire,
                                        "finish_reason": finish}],
                           "usage": self.usage}, ensure_ascii=False).encode()

    def answer(self, body: dict, item: Any) -> tuple[int, str, bytes]:
        if isinstance(item, dict) and "sever" in item:
            message, finish = item["sever"]
            return 200, "text/event-stream", self._chunks_for(message, finish,
                                                              terminated=False)
        message, finish = item
        if body.get("stream") is True:
            return 200, "text/event-stream", self._chunks_for(message, finish)
        return 200, "application/json", self._body(message, finish)

    def fallback(self, body: dict) -> tuple[int, str, bytes]:
        return self.answer(body, ({"content": "ok"}, "stop"))


def tool_use(call_id: str, name: str, arguments: dict, **extra: Any) -> dict:
    """A Messages-API `tool_use` block for a script."""
    return {"type": "tool_use", "id": call_id, "name": name, "input": arguments, **extra}


def chat_call(call_id: str, name: str, arguments: dict | str) -> dict:
    """A Chat-Completions tool call for a script (`arguments` a dict is JSON-encoded)."""
    return {"id": call_id, "name": name,
            "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments)}


Script = list[Any]
Responder = Callable[[dict], Any]
