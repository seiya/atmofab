"""The host tool-use loop of an HTTP `workshop leaf` (issue #534 PR-J3-b).

A CLI workshop leaf runs its own agent loop: the CLI asks the model, runs the file tools against
the work tree and calls the host's operations over the wrapper's MCP endpoint. An HTTP provider
(`anthropic_api`, `openai_compatible`) has no process and no loop, so the HOST runs it here: it
sends the conversation, executes every tool call the reply contains — the five file tools
(`leaf_contract.HTTP_FILE_TOOLS`) on the lineage's tree, and on a producing lineage the wrapper's
operations through `workshop_wrapper.run_wrapper_op` — appends the results, and asks again, until
the model answers with no tool call or a bound ends the launch. What the leaf leaves in its tree
is then judged exactly as a CLI leaf's tree is (`docs/ORCHESTRATION.md` §workshop leaf).

The rules this module holds, each decided in the PR-J3-b plan (posted on #534):

* A reply's tool calls are executed because the reply CONTAINS them, never because of its stop
  reason (an OpenAI-compatible server can say `finish_reason: "stop"` with `tool_calls`); the
  stop reason decides truncation alone. A reply cut at the output limit (`max_tokens` /
  `length`, or anthropic's `model_context_window_exceeded`) is neither executed nor appended
  to the history.
* Every call of one reply is answered, in order, in ONE message (anthropic) or one `tool`
  message per call (openai), so the history stays a valid replay; a wrapper result carrying
  `ended: True` answers the rest of the reply with `lineage_ended` and ends the loop after.
* A malformed call (an undeclared tool, arguments that are not a JSON object, a schema
  violation) is answered with `tool_arguments_invalid`; nothing runs; the loop continues.
* The deadline is checked before every request, and a request in flight is bounded by
  `min(timeout_s, remaining)`: a `response_deadline_exceeded` when the remaining time was the
  binding term is the lineage's TIMEOUT, not a transport error.
* Anthropic: two `cache_control` breakpoints per request (the system block, and the last block of
  the last user message), set on a COPY at serialization — the stored history carries none; every
  user message is a content-block list, so the first request caches the launch prompt too. The
  assistant turn is replayed exactly as received. The entry's `effort`, when configured, rides
  as `output_config.effort` (absent: no `output_config` key). OpenAI: the entry's `effort` rides
  as `reasoning_effort`; no cache field (the server's automatic
  prefix caching); `reasoning_content`, when a server returns it, is recorded and replayed on
  the assistant message (a thinking server that returns it with `tools` requires it back).

Stdlib only at import (`urllib` through `tools.llm_http_leaf`); the file tools import
`tools.workshop_tree` when called.
"""

from __future__ import annotations

import copy
import json
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

from tools.llm_http_leaf import (
    _STREAM_INTERRUPTED,
    ANTHROPIC_VERSION,
    DEFAULT_HTTP_TIMEOUT_SECONDS,
    DEFAULT_MAX_OUTPUT_TOKENS,
    _anthropic_usage,
    _api_key,
    _normalized_usage,
    _openai_usage,
    _post_json,
    _post_stream,
    _redact,
)
from tools.leaf_contract import (
    HTTP_FILE_TOOLS,
    HTTP_GREP_MAX_MATCHES,
    HTTP_READ_PAGE_BYTES,
    HTTP_READ_PAGE_LINES,
)

#: The category of a tool call the loop answers without running it (decision 8).
TOOL_ARGUMENTS_INVALID = "tool_arguments_invalid"
#: The category of a call answered without running because an earlier call of the same reply
#: ended the lineage (decision 9).
LINEAGE_ENDED = "lineage_ended"
#: The category of a file-tool call the tree refused (a reserved name, a link, a bad path).
TOOL_REFUSED = "tool_refused"

#: The stop reasons that mean "the reply was cut at the output limit".
_TRUNCATION_STOP_REASONS = frozenset({"max_tokens", "length", "model_context_window_exceeded"})

#: The `ended` value of a launch a wrapper result ended (`ended: True`).
ENDED_BY_WRAPPER = "wrapper_ended"
#: The `ended` value of a launch whose reply was cut at the output limit.
ENDED_TRUNCATED = "truncated"
#: The `ended` value of a launch a transport error ended.
ENDED_TRANSPORT = "transport_error"

_CACHE_MARK = {"type": "ephemeral"}


@dataclass
class LoopHistory:
    """One lineage's conversation: the stored `messages` (dialect-shaped, carrying no
    `cache_control`) and how many of them the record already holds, so a `request` row of
    `loop_turns.jsonl` carries the messages from `recorded` on: every message appended since the
    previous request, and the last `user` message again when a launch prompt was merged into it
    (`_append_user`). Rebuilding the history from the rows is replacing it from each row's
    `messages_from` on with the row's messages."""

    messages: list[dict[str, Any]] = field(default_factory=list)
    recorded: int = 0


class LoopResult(NamedTuple):
    """One launch of an HTTP workshop lineage.

    `text` is the last reply's text; `turns` the number of requests answered; `tool_calls`
    the count of executed-or-answered calls by name; `transport_error` set when a request
    failed (the redacted message); `timed_out` when the deadline ended the launch (before a
    request, or in flight with the remaining time binding); `truncated` names the cut (the
    limit and the first tool cut) when a reply was cut at the output limit; `ended` is the
    reason the launch ended (the last reply's stop reason when it made no tool call, or one of
    `ENDED_BY_WRAPPER`, `ENDED_TRUNCATED`, `ENDED_TRANSPORT`, the deadline's name);
    `tool_call_made` whether any reply of the launch made a tool call."""

    text: str
    usage: dict[str, Any] | None
    turns: int
    tool_calls: dict[str, int]
    transport_error: str | None
    timed_out: bool
    truncated: str | None
    ended: str | None
    model: str
    model_reported: bool
    raw_response: str
    tool_call_made: bool


class _ToolCall(NamedTuple):
    id: str
    name: str
    #: The arguments as a JSON object, or None when they did not parse into one.
    arguments: dict[str, Any] | None
    #: Why the arguments are unusable, when they are.
    malformed: str | None


class _Reply(NamedTuple):
    #: The assistant message to append to the history (dialect-shaped), or None when the reply
    #: holds nothing a replay may carry (an empty content).
    message: dict[str, Any] | None
    text: str
    calls: list[_ToolCall]
    stop_reason: str
    usage: dict[str, Any]
    model: str
    #: The openai `reasoning_content` (also on `message`, replayed when non-empty).
    reasoning: str


# --- tool declarations and argument validation -------------------------------------------


def anthropic_tools(tools: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The Messages-API declaration of each tool of `tools` (`{name, description,
    input_schema}`)."""
    return [{"name": t["name"], "description": t["description"],
             "input_schema": copy.deepcopy(t["input_schema"])} for t in tools]


def openai_tools(tools: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The Chat-Completions declaration of each tool of `tools`: the same name, description
    and schema in the `function` wrapper."""
    return [{"type": "function",
             "function": {"name": t["name"], "description": t["description"],
                          "parameters": copy.deepcopy(t["input_schema"])}} for t in tools]


_JSON_TYPES: Mapping[str, Callable[[Any], bool]] = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}


def schema_violation(value: Any, schema: Mapping[str, Any], where: str = "arguments"
                     ) -> str | None:
    """The first way `value` fails `schema`, or None. Covers what the declared tools use:
    `type`, an object's `properties` / `required` / `additionalProperties: false`, and an
    array's `items`."""
    kind = schema.get("type")
    check = _JSON_TYPES.get(kind) if isinstance(kind, str) else None
    if check is not None and not check(value):
        return f"{where} must be a JSON {kind}"
    if kind == "object":
        props = schema.get("properties") or {}
        for name in schema.get("required") or ():
            if name not in value:
                return f"{where} is missing the required key {name!r}"
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(props))
            if extra:
                return (f"{where} has keys the tool does not take: {extra} (it takes "
                        f"{sorted(props)})")
        for name, sub in props.items():
            if name in value and isinstance(sub, Mapping):
                found = schema_violation(value[name], sub, f"{where}.{name}")
                if found is not None:
                    return found
    if kind == "array" and isinstance(schema.get("items"), Mapping):
        for index, item in enumerate(value):
            found = schema_violation(item, schema["items"], f"{where}[{index}]")
            if found is not None:
                return found
    return None


# --- request building --------------------------------------------------------------------


def _anthropic_payload(entry: Any, system: str, messages: Sequence[Mapping[str, Any]],
                       tools: list[dict[str, Any]], max_tokens: int,
                       stream: bool) -> dict[str, Any]:
    """The Messages-API body. The two cache breakpoints are placed on a COPY: the system
    block, and the last content block of the last `user` message. A configured `effort` is
    sent as `output_config: {"effort": <level>}` (issue #560); an absent one sends no
    `output_config` key at all, so the model's own default applies and a strict gateway sees
    no key it might reject. The level is the entry's, so every request of a launch carries the
    same one (a change between requests would invalidate the prompt cache)."""
    sent = copy.deepcopy(list(messages))
    for message in reversed(sent):
        if message.get("role") == "user" and isinstance(message.get("content"), list) \
                and message["content"]:
            last = message["content"][-1]
            if isinstance(last, dict):
                last["cache_control"] = dict(_CACHE_MARK)
            break
    payload: dict[str, Any] = {
        "model": entry.model,
        "system": [{"type": "text", "text": system, "cache_control": dict(_CACHE_MARK)}],
        "messages": sent,
        "tools": tools,
        "max_tokens": max_tokens,
    }
    if getattr(entry, "effort", ""):
        payload["output_config"] = {"effort": entry.effort}
    if stream:
        payload["stream"] = True
    return payload


def _openai_payload(entry: Any, system: str, messages: Sequence[Mapping[str, Any]],
                    tools: list[dict[str, Any]], max_tokens: int,
                    stream: bool) -> dict[str, Any]:
    """The Chat-Completions body, with the workshop system prompt and the tools."""
    payload: dict[str, Any] = {
        "model": entry.model,
        "messages": [{"role": "system", "content": system}, *copy.deepcopy(list(messages))],
        "tools": tools,
        "max_tokens": max_tokens,
    }
    if getattr(entry, "effort", ""):
        payload["reasoning_effort"] = entry.effort
    if stream:
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
    return payload


def _endpoint(entry: Any, key: str, stream: bool) -> tuple[str, dict[str, str]]:
    if entry.provider == "anthropic_api":
        headers = {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
        url = entry.base_url.rstrip("/") + "/v1/messages"
    else:
        headers = {"Authorization": f"Bearer {key}"}
        url = entry.base_url.rstrip("/") + "/chat/completions"
    if stream:
        headers["Accept"] = "text/event-stream"
    return url, headers


def user_message(entry: Any, text: str) -> dict[str, Any]:
    """The launch prompt as a `user` message: a content-block list on anthropic (decision 5:
    the cache breakpoint then caches the launch prompt on the first request), a string on
    openai."""
    if entry.provider == "anthropic_api":
        return {"role": "user", "content": [{"type": "text", "text": text}]}
    return {"role": "user", "content": text}


def _append_user(entry: Any, history: LoopHistory, text: str) -> None:
    """Append the launch prompt. On anthropic a history ending in a `user` message (the tool
    results of a launch a bound or a truncation ended) takes the prompt as one more block of
    that message, so the replay never holds two consecutive user turns; that message is then
    marked unrecorded again, so the next `request` row carries it whole (a row's messages
    replace the reconstructed history from its `messages_from` on)."""
    messages = history.messages
    message = user_message(entry, text)
    if (entry.provider == "anthropic_api" and messages
            and messages[-1].get("role") == "user"
            and isinstance(messages[-1].get("content"), list)):
        messages[-1]["content"].extend(message["content"])
        history.recorded = min(history.recorded, len(messages) - 1)
        return
    messages.append(message)


# --- reply readers -----------------------------------------------------------------------


def _parse_arguments(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    if isinstance(raw, dict):
        return raw, None
    if isinstance(raw, str):
        if not raw.strip():
            return {}, None
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError as exc:
            return None, f"the arguments are not JSON ({exc.msg} at char {exc.pos})"
        if not isinstance(doc, dict):
            return None, f"the arguments are a JSON {type(doc).__name__}, not an object"
        return doc, None
    return None, f"the arguments are a {type(raw).__name__}, not a JSON object"


def _anthropic_reply(doc: Mapping[str, Any]) -> tuple[_Reply | None, str | None]:
    """A buffered Messages-API body as a `_Reply`: the content list kept exactly as received."""
    blocks = doc.get("content")
    if not isinstance(blocks, list):
        return None, "response_missing_content"
    calls: list[_ToolCall] = []
    text: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            text.append(block["text"])
        elif block.get("type") == "tool_use":
            args, why = _parse_arguments(block.get("input"))
            calls.append(_ToolCall(str(block.get("id") or ""), str(block.get("name") or ""),
                                   args, why))
    model = doc.get("model")
    stop = doc.get("stop_reason")
    return _Reply({"role": "assistant", "content": blocks} if blocks else None,
                  "".join(text), calls, stop if isinstance(stop, str) else "",
                  _anthropic_usage(doc.get("usage")),
                  model if isinstance(model, str) else "", ""), None


def _anthropic_stream_reply(frames: Sequence[tuple[str, str]]
                            ) -> tuple[_Reply | None, str | None]:
    """A Messages-API event stream folded into the content list a buffered body would hold.

    Each block accumulates per index from `content_block_start` through its deltas to
    `content_block_stop`; a `tool_use` block's `input_json_delta` fragments are parsed ONLY at
    its stop, so a partial argument is never a call. A stream with no `message_stop`, or with a
    block left unstopped, is a transport error (`stream interrupted`), and an `error` event wins
    over a `message_stop` (the rule the deleted pure HTTP reader also applied)."""
    blocks: dict[int, dict[str, Any]] = {}
    partial: dict[int, list[str]] = {}
    stopped: set[int] = set()
    model = ""
    usage: dict[str, Any] = {}
    stop_reason = ""
    complete = False
    error_detail = ""
    for event, data in frames:
        try:
            doc = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(doc, dict):
            continue
        if event == "message_start":
            message = doc.get("message")
            if isinstance(message, dict):
                if isinstance(message.get("model"), str):
                    model = message["model"]
                usage.update(_anthropic_usage(message.get("usage")))
        elif event == "content_block_start":
            index, block = doc.get("index"), doc.get("content_block")
            if isinstance(index, int) and isinstance(block, dict):
                blocks[index] = copy.deepcopy(block)
                if block.get("type") == "tool_use":
                    partial[index] = []
        elif event == "content_block_delta":
            index, delta = doc.get("index"), doc.get("delta")
            if not (isinstance(index, int) and index in blocks and isinstance(delta, dict)):
                continue
            block = blocks[index]
            kind = delta.get("type")
            if kind == "text_delta" and isinstance(delta.get("text"), str):
                block["text"] = str(block.get("text") or "") + delta["text"]
            elif kind == "input_json_delta" and isinstance(delta.get("partial_json"), str):
                partial.setdefault(index, []).append(delta["partial_json"])
            elif kind == "thinking_delta" and isinstance(delta.get("thinking"), str):
                block["thinking"] = str(block.get("thinking") or "") + delta["thinking"]
            elif kind == "signature_delta" and isinstance(delta.get("signature"), str):
                block["signature"] = str(block.get("signature") or "") + delta["signature"]
        elif event == "content_block_stop":
            index = doc.get("index")
            if isinstance(index, int) and index in blocks:
                stopped.add(index)
        elif event == "message_delta":
            delta = doc.get("delta")
            if isinstance(delta, dict) and isinstance(delta.get("stop_reason"), str):
                stop_reason = delta["stop_reason"]
            # The final usage, through the same reader as `message_start`'s: it carries the
            # authoritative `output_tokens` and, only here when streaming, the
            # `output_tokens_details` breakdown (`thinking_tokens` -> `reasoning_tokens`,
            # issue #560). Every key the delta carries wins over `message_start`'s. On the
            # real wire the final delta repeats all four token classes, equal to
            # `message_start`'s (103 of 103 recorded replies of
            # `orch_20261028T235119Z_492e5092`), so the start-side read matters only to a
            # gateway whose delta omits them; until #560 only `output_tokens` was read here.
            usage.update(_anthropic_usage(doc.get("usage")))
        elif event == "message_stop":
            complete = True
        elif event == "error":
            detail = doc.get("error")
            error_detail = (f"{detail.get('type', 'error')}: {detail.get('message', '')}"
                            if isinstance(detail, dict) else str(detail))
    if error_detail:
        return None, f"{_STREAM_INTERRUPTED}: provider error event {error_detail}".strip()
    if not complete:
        return None, (f"{_STREAM_INTERRUPTED}: ended after {len(frames)} frames with no "
                      "message_stop")
    unstopped = sorted(set(blocks) - stopped)
    if unstopped:
        return None, (f"{_STREAM_INTERRUPTED}: content block(s) {unstopped} never stopped "
                      "before message_stop")
    content: list[dict[str, Any]] = []
    calls: list[_ToolCall] = []
    text: list[str] = []
    for index in sorted(blocks):
        block = blocks[index]
        if block.get("type") == "tool_use":
            args, why = _parse_arguments("".join(partial.get(index, [])))
            # The replayed block holds the parsed object; an unparseable one (never seen from
            # a complete stream) is replayed with `{}` and answered as malformed.
            block["input"] = args if args is not None else {}
            calls.append(_ToolCall(str(block.get("id") or ""), str(block.get("name") or ""),
                                   args, why))
        elif block.get("type") == "text" and isinstance(block.get("text"), str):
            text.append(block["text"])
        content.append(block)
    return _Reply({"role": "assistant", "content": content} if content else None,
                  "".join(text), calls, stop_reason, usage, model, ""), None


def _openai_message(content: str, calls: list[dict[str, Any]], reasoning: str
                    ) -> dict[str, Any] | None:
    """The stored assistant message: `content` (null when empty),
    `tool_calls` only when there are some, and `reasoning_content` only when the server
    returned one — replayed as received, because a server that returns it in a request with
    `tools` requires it back on every later request (DeepSeek's thinking mode answers 400
    otherwise; without `tools` it is ignored), and a server that returns none gets none."""
    if not content and not calls and not reasoning:
        return None
    message: dict[str, Any] = {"role": "assistant",
                               "content": content if content else None}
    if reasoning:
        message["reasoning_content"] = reasoning
    if calls:
        message["tool_calls"] = calls
    return message


def _openai_calls(raw_calls: Iterable[Any]) -> tuple[list[_ToolCall], list[dict[str, Any]]]:
    calls: list[_ToolCall] = []
    wire: list[dict[str, Any]] = []
    for raw in raw_calls:
        if not isinstance(raw, dict):
            continue
        function = raw.get("function") if isinstance(raw.get("function"), dict) else {}
        call_id = str(raw.get("id") or "")
        name = str(function.get("name") or "")
        arguments = function.get("arguments")
        text_args = arguments if isinstance(arguments, str) else json.dumps(arguments)
        args, why = _parse_arguments(arguments if arguments is not None else "")
        calls.append(_ToolCall(call_id, name, args, why))
        wire.append({"id": call_id, "type": "function",
                     "function": {"name": name, "arguments": text_args}})
    return calls, wire


def _openai_reply(doc: Mapping[str, Any]) -> tuple[_Reply | None, str | None]:
    choices = doc.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None, "response_missing_choices"
    choice = choices[0]
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    content = message.get("content") if isinstance(message.get("content"), str) else ""
    reasoning = (message.get("reasoning_content")
                 if isinstance(message.get("reasoning_content"), str) else "")
    calls, wire = _openai_calls(message.get("tool_calls") or [])
    model = doc.get("model")
    finish = choice.get("finish_reason")
    return _Reply(_openai_message(content, wire, reasoning), content, calls,
                  finish if isinstance(finish, str) else "",
                  _openai_usage(doc.get("usage")),
                  model if isinstance(model, str) else "", reasoning), None


def _openai_stream_reply(frames: Sequence[tuple[str, str]]
                         ) -> tuple[_Reply | None, str | None]:
    """A Chat-Completions event stream folded into the buffered message: `delta.content`,
    `delta.reasoning_content` and each `delta.tool_calls[i]` accumulated per index. Complete on
    a `finish_reason` or `[DONE]` (the union: servers disagree about which they send);
    an `error` chunk wins."""
    text: list[str] = []
    reasoning: list[str] = []
    calls: dict[int, dict[str, Any]] = {}
    model = ""
    usage: dict[str, Any] = {}
    finish: str | None = None
    saw_done = False
    saw_choice = False
    error_detail = ""
    for _event, data in frames:
        if data == "[DONE]":
            saw_done = True
            continue
        try:
            doc = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(doc, dict):
            continue
        detail = doc.get("error")
        if detail:
            error_detail = (f"{detail.get('type', 'error')}: {detail.get('message', '')}"
                            if isinstance(detail, dict) else str(detail))
            continue
        if isinstance(doc.get("model"), str):
            model = doc["model"]
        if isinstance(doc.get("usage"), dict):
            usage = _openai_usage(doc["usage"]) or usage
        choices = doc.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            continue
        saw_choice = True
        choice = choices[0]
        delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
        if isinstance(delta.get("content"), str):
            text.append(delta["content"])
        if isinstance(delta.get("reasoning_content"), str):
            reasoning.append(delta["reasoning_content"])
        for fragment in delta.get("tool_calls") or ():
            if not isinstance(fragment, dict):
                continue
            index = fragment.get("index")
            if not isinstance(index, int):
                index = len(calls)
            slot = calls.setdefault(index, {"id": "", "name": "", "arguments": []})
            if isinstance(fragment.get("id"), str) and fragment["id"]:
                slot["id"] = fragment["id"]
            function = fragment.get("function")
            if isinstance(function, dict):
                if isinstance(function.get("name"), str):
                    slot["name"] += function["name"]
                if isinstance(function.get("arguments"), str):
                    slot["arguments"].append(function["arguments"])
        if isinstance(choice.get("finish_reason"), str):
            finish = choice["finish_reason"]
    if error_detail:
        return None, f"{_STREAM_INTERRUPTED}: provider error event {error_detail}".strip()
    if not (saw_done or finish is not None):
        return None, (f"{_STREAM_INTERRUPTED}: ended after {len(frames)} frames with no "
                      "[DONE] and no finish_reason")
    if not saw_choice:
        return None, "response_missing_choices"
    parsed, wire = _openai_calls(
        {"id": slot["id"], "type": "function",
         "function": {"name": slot["name"], "arguments": "".join(slot["arguments"])}}
        for _index, slot in sorted(calls.items()))
    content = "".join(text)
    return _Reply(_openai_message(content, wire, "".join(reasoning)), content, parsed,
                  finish or "", usage, model, "".join(reasoning)), None


# --- tool results ------------------------------------------------------------------------


def _append_results(entry: Any, history: list[dict[str, Any]],
                    answered: Sequence[tuple[_ToolCall, dict[str, Any]]]) -> None:
    """Every call's result, in the reply's order: one `user` message of `tool_result` blocks on
    anthropic, one `tool` message per call on openai."""
    if entry.provider == "anthropic_api":
        blocks = []
        for call, result in answered:
            block: dict[str, Any] = {"type": "tool_result", "tool_use_id": call.id,
                                     "content": json.dumps(result, ensure_ascii=False)}
            if result.get("ok") is False:
                block["is_error"] = True
            blocks.append(block)
        history.append({"role": "user", "content": blocks})
        return
    for call, result in answered:
        history.append({"role": "tool", "tool_call_id": call.id,
                        "content": json.dumps(result, ensure_ascii=False)})


def _invalid(findings: str) -> dict[str, Any]:
    return {"ok": False, "category": TOOL_ARGUMENTS_INVALID, "findings": findings}


def _answer(call: _ToolCall, schemas: Mapping[str, Mapping[str, Any]],
            call_tool: Callable[[str, dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    """Run one call, or answer it without running (decision 8)."""
    schema = schemas.get(call.name)
    if schema is None:
        return _invalid(f"there is no tool named {call.name!r}; your tools are "
                        f"{sorted(schemas)}")
    if call.arguments is None:
        return _invalid(call.malformed or "the arguments are not a JSON object")
    violation = schema_violation(call.arguments, schema)
    if violation is not None:
        return _invalid(violation)
    try:
        result = call_tool(call.name, dict(call.arguments))
    except Exception as exc:  # noqa: BLE001 — a tool's failure is the leaf's feedback
        return {"ok": False, "category": "tool_error",
                "findings": f"{type(exc).__name__}: {exc}"}
    return result if isinstance(result, dict) else {"ok": True, "result": result}


# --- the loop ----------------------------------------------------------------------------


def _sum_usage(total: dict[str, int], usage: Mapping[str, Any]) -> None:
    for key, value in usage.items():
        if isinstance(value, int) and not isinstance(value, bool):
            total[key] = total.get(key, 0) + value


def run_workshop_loop(
    entry: Any,
    *,
    system: str,
    history: LoopHistory,
    prompt: str,
    tools: Sequence[Mapping[str, Any]],
    call_tool: Callable[[str, dict[str, Any]], dict[str, Any]],
    deadline: float,
    deadline_name: str,
    env: Mapping[str, str] | None = None,
    opener: Callable[..., Any] | None = None,
    record: Callable[[dict[str, Any]], None] | None = None,
) -> LoopResult:
    """One launch of an HTTP workshop lineage: append `prompt` to `history` and run the tool-use
    loop until the model answers with no tool call, a wrapper result ends the lineage, a reply
    is cut at the output limit, a request fails, or `deadline` (a `time.monotonic()` instant;
    its bound named `deadline_name`) passes. Never raises on what the provider or the model
    sends: every failure is a field of the result.

    `history.messages` is mutated in place: the prompt, every complete assistant reply and every
    tool-result message are appended (a cut reply is not). `record(event)` receives one dict per
    `loop_turns.jsonl` row (`request`, `response`, `tool_call`, `tool_result`, `ended`), every
    provider-supplied string redacted of the entry's API key."""
    emit = record if record is not None else (lambda _event: None)
    key, key_error = _api_key(entry, env)
    secret = key

    def redacted(value: Any) -> Any:
        if isinstance(value, str):
            return _redact(value, secret)
        if isinstance(value, list):
            return [redacted(v) for v in value]
        if isinstance(value, dict):
            return {k: redacted(v) for k, v in value.items()}
        return value

    anthropic = entry.provider == "anthropic_api"
    declared = anthropic_tools(tools) if anthropic else openai_tools(tools)
    schemas = {str(t["name"]): t["input_schema"] for t in tools}
    max_tokens = int(getattr(entry, "max_output_tokens", None) or DEFAULT_MAX_OUTPUT_TOKENS)
    base_timeout = float(getattr(entry, "timeout_s", None) or DEFAULT_HTTP_TIMEOUT_SECONDS)
    stream = bool(getattr(entry, "stream", True))
    messages = history.messages
    _append_user(entry, history, prompt)

    usage_total: dict[str, int] = {}
    requests = 0
    tool_counts: dict[str, int] = {}
    text = ""
    model = ""
    raw_last = ""
    transport_error: str | None = None
    timed_out = False
    truncated: str | None = None
    ended: str | None = None
    tool_call_made = False

    def finish() -> LoopResult:
        # Summed over every answered request of the launch, a transport-ended one included
        # (the earlier requests' tokens were spent); `requests` says how many.
        usage = (_normalized_usage({**usage_total, "provider_details": {"requests": requests}})
                 if usage_total else None)
        reported = _redact(model, secret)
        emit({"event": "ended", "ended": ended, "requests": requests,
              "timed_out": timed_out, "truncated": truncated,
              "transport_error": transport_error, "usage": usage,
              "tool_calls": dict(tool_counts)})
        return LoopResult(text, usage, requests, dict(tool_counts), transport_error,
                          timed_out, truncated, ended, reported or entry.model,
                          bool(reported), raw_last, tool_call_made)

    if key_error is not None:
        transport_error, ended = key_error, ENDED_TRANSPORT
        return finish()

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # Decision 7: no request is sent past the deadline.
            timed_out, ended = True, deadline_name
            return finish()
        timeout = min(base_timeout, remaining)
        deadline_binds = remaining < base_timeout
        url, headers = _endpoint(entry, key, stream)
        build = _anthropic_payload if anthropic else _openai_payload
        payload = build(entry, system, messages, declared, max_tokens, stream)
        emit({"event": "request", "model": entry.model, "max_tokens": max_tokens,
              "stream": stream, "tools": [t["name"] for t in tools],
              "messages_from": history.recorded,
              "messages": redacted(messages[history.recorded:])})
        history.recorded = len(messages)
        if stream:
            frames, raw, error = _post_stream(url, payload, headers, timeout_s=timeout,
                                              secret=secret, env=env, opener=opener)
            reply, read_error = (None, None)
            if frames is not None:
                reply, read_error = (_anthropic_stream_reply(frames) if anthropic
                                     else _openai_stream_reply(frames))
                if read_error is None:
                    # The provider's terminator is on the wire: the reply is complete whatever
                    # happened to the socket after it (the pure path's rule).
                    error = None
        else:
            doc, raw, error = _post_json(url, payload, headers, timeout_s=timeout,
                                         secret=secret, env=env, opener=opener)
            reply, read_error = (None, None)
            if error is None and doc is not None:
                reply, read_error = _anthropic_reply(doc) if anthropic else _openai_reply(doc)
        raw_last = raw or ""
        emit({"event": "response", "raw": raw_last,
              "transport_error": _redact(error, secret) if error else None,
              "read_error": _redact(read_error, secret) if read_error else None})
        if error is not None or reply is None:
            message = _redact(error or read_error or "empty_response", secret)
            if (error is not None and error.startswith("response_deadline_exceeded")
                    and deadline_binds):
                # Decision 7: the lineage's remaining time bounded this request, so the
                # deadline ended the launch inside it — the tree is judged as it stands.
                timed_out, ended = True, deadline_name
            else:
                transport_error, ended = message, ENDED_TRANSPORT
            return finish()
        requests += 1
        _sum_usage(usage_total, reply.usage)
        if reply.model:
            model = reply.model
        if reply.stop_reason in _TRUNCATION_STOP_REASONS:
            # Decision 4: a cut reply's calls are never executed and the reply is not
            # appended (its last call has no result a replay could carry).
            limit = (f"max_output_tokens={max_tokens}"
                     if reply.stop_reason != "model_context_window_exceeded"
                     else "the model's context window")
            cut = reply.calls[-1].name if reply.calls else None
            truncated = limit + (f" inside a `{cut}` call" if cut else "")
            ended = ENDED_TRUNCATED
            text = reply.text
            return finish()
        if reply.message is not None:
            messages.append(reply.message)
        if not reply.calls:
            ended = reply.stop_reason or "unknown"
            text = reply.text
            return finish()
        tool_call_made = True
        answered: list[tuple[_ToolCall, dict[str, Any]]] = []
        lineage_ended = False
        for call in reply.calls:
            if lineage_ended:
                result: dict[str, Any] = {
                    "ok": False, "category": LINEAGE_ENDED, "ended": True,
                    "findings": "an earlier call of this reply ended the lineage; this call "
                                "was not run"}
            else:
                result = _answer(call, schemas, call_tool)
                if result.get("ended") is True:
                    lineage_ended = True
            tool_counts[call.name] = tool_counts.get(call.name, 0) + 1
            emit({"event": "tool_call", "id": call.id, "name": call.name,
                  "arguments": redacted(call.arguments)
                  if call.arguments is not None else None,
                  "malformed": call.malformed})
            emit({"event": "tool_result", "id": call.id, "name": call.name,
                  "result": redacted(result)})
            answered.append((call, result))
        _append_results(entry, messages, answered)
        text = reply.text
        if lineage_ended:
            ended = ENDED_BY_WRAPPER
            return finish()


# --- the five file tools -----------------------------------------------------------------


def _split_lines(data: bytes) -> list[bytes]:
    """`data`'s lines without their `\\n`, as `read.py` splits them: an unterminated last line
    counts, a trailing newline does not add an empty one."""
    parts = data.split(b"\n")
    return parts[:-1] + ([parts[-1]] if parts[-1] else [])


def _refused(findings: str) -> dict[str, Any]:
    return {"ok": False, "category": TOOL_REFUSED, "findings": findings}


def _tree_text(root: Path, path: str) -> tuple[list[str] | None, dict[str, Any] | None]:
    from tools.workshop_tree import plain_relative_path, read_regular_file_under
    rel = plain_relative_path(path)
    if rel is None:
        return None, _refused(f"{path!r} is not a plain relative path of your work tree")
    data, why = read_regular_file_under(Path(root), rel)
    if data is None:
        return None, _refused(f"{rel.as_posix()}: {why}")
    return [line.decode("utf-8", "replace") for line in _split_lines(data)], None


def read_file(root: Path, path: str, start_line: int = 1,
              max_lines: int = HTTP_READ_PAGE_LINES) -> dict[str, Any]:
    """One page of `<root>/<path>` (decision 3): from `start_line`, at most `max_lines` lines
    (clamped to `HTTP_READ_PAGE_LINES`) and at most `HTTP_READ_PAGE_BYTES` UTF-8 bytes of text,
    stopping at the last whole line under both. A line longer than the byte page is not
    returned: the page ends before it and `next_start_line` skips it, `skipped_line` naming it."""
    lines, refused = _tree_text(root, path)
    if lines is None:
        assert refused is not None
        return refused
    total = len(lines)
    if start_line < 1:
        return _refused(f"start_line must be 1 or more (got {start_line})")
    if max_lines < 1:
        return _refused(f"max_lines must be 1 or more (got {max_lines})")
    result: dict[str, Any] = {"ok": True, "path": path, "start_line": start_line,
                              "total_lines": total}
    if max_lines > HTTP_READ_PAGE_LINES:
        result["max_lines_clamped"] = (f"max_lines {max_lines} is over the page of "
                                       f"{HTTP_READ_PAGE_LINES:,} lines; {HTTP_READ_PAGE_LINES:,} "
                                       "were read")
        max_lines = HTTP_READ_PAGE_LINES
    if total == 0:
        result.update(end_line=0, next_start_line=None, text="")
        return result
    if start_line > total:
        return _refused(f"{path}: start_line {start_line} is past the end ({total} lines)")
    page: list[str] = []
    size = 0
    k = start_line - 1
    next_start: int | None = None
    while k < total and len(page) < max_lines:
        line = lines[k]
        cost = len(line.encode("utf-8", "replace")) + (1 if page else 0)
        if len(line.encode("utf-8", "replace")) > HTTP_READ_PAGE_BYTES:
            result["skipped_line"] = {"line": k + 1,
                                      "bytes": len(line.encode("utf-8", "replace"))}
            result["findings"] = (
                f"line {k + 1} is {result['skipped_line']['bytes']:,} bytes, over the "
                f"{HTTP_READ_PAGE_BYTES:,}-byte page; read it with `grep_file`")
            next_start = k + 2 if k + 1 < total else None
            break
        if size + cost > HTTP_READ_PAGE_BYTES:
            next_start = k + 1
            break
        page.append(line)
        size += cost
        k += 1
    else:
        next_start = k + 1 if k < total else None
    result.update(end_line=start_line + len(page) - 1, next_start_line=next_start,
                  text="\n".join(page))
    return result


def grep_file(root: Path, path: str, pattern: str,
              max_matches: int = HTTP_GREP_MAX_MATCHES) -> dict[str, Any]:
    """The lines of ONE file matching `pattern` (a Python `re`), each with its line number: at
    most `max_matches` (clamped to `HTTP_GREP_MAX_MATCHES`) and at most `HTTP_READ_PAGE_BYTES`
    bytes of text, a line cut to what remains (`cut: true`). `total_matches` counts every
    matching line; `truncated` says the result does not hold them all whole."""
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return _refused(f"pattern {pattern!r} is not a valid Python regular expression ({exc})")
    lines, refused = _tree_text(root, path)
    if lines is None:
        assert refused is not None
        return refused
    if max_matches < 1:
        return _refused(f"max_matches must be 1 or more (got {max_matches})")
    max_matches = min(max_matches, HTTP_GREP_MAX_MATCHES)
    matches: list[dict[str, Any]] = []
    total = 0
    size = 0
    cut_any = False
    for number, line in enumerate(lines, start=1):
        if not regex.search(line):
            continue
        total += 1
        if len(matches) >= max_matches or size >= HTTP_READ_PAGE_BYTES:
            continue
        encoded = line.encode("utf-8", "replace")
        room = HTTP_READ_PAGE_BYTES - size
        entry: dict[str, Any] = {"line": number}
        if len(encoded) > room:
            entry["text"] = encoded[:room].decode("utf-8", "ignore")
            entry["cut"] = True
            cut_any = True
        else:
            entry["text"] = line
        size += len(entry["text"].encode("utf-8"))
        matches.append(entry)
    return {"ok": True, "path": path, "matches": matches, "total_matches": total,
            "truncated": cut_any or total > len(matches)}


def list_files(root: Path) -> dict[str, Any]:
    """Every file of the tree with its size, and every link (refused: never followed)."""
    from tools.workshop_tree import list_tree_files
    files, links = list_tree_files(Path(root))
    return {"ok": True, "files": [{"path": p, "bytes": n} for p, n in files],
            "links_refused": links}


def file_tool(root: Path, reserved: Iterable[str], name: str,
              args: Mapping[str, Any]) -> dict[str, Any]:
    """Run one of `HTTP_FILE_TOOLS` on the tree at `root` (arguments already schema-checked).
    `reserved` is the lineage's host-placed names, which `write_file` / `edit_file` refuse."""
    from tools.workshop_tree import edit_tree_file, write_tree_file
    if name == "read_file":
        return read_file(root, str(args["path"]), int(args.get("start_line", 1)),
                         int(args.get("max_lines", HTTP_READ_PAGE_LINES)))
    if name == "grep_file":
        return grep_file(root, str(args["path"]), str(args["pattern"]),
                         int(args.get("max_matches", HTTP_GREP_MAX_MATCHES)))
    if name == "list_files":
        return list_files(root)
    if name == "write_file":
        why = write_tree_file(root, str(args["path"]), str(args["content"]),
                              reserved=reserved)
        if why is not None:
            return _refused(why)
        return {"ok": True, "path": args["path"],
                "bytes": len(str(args["content"]).encode("utf-8"))}
    if name == "edit_file":
        why = edit_tree_file(root, str(args["path"]), str(args["old_text"]),
                             str(args["new_text"]), reserved=reserved)
        if why is not None:
            return _refused(why)
        return {"ok": True, "path": args["path"]}
    return _invalid(f"{name!r} is not a file tool ({list(HTTP_FILE_TOOLS)})")


__all__ = [
    "ENDED_BY_WRAPPER",
    "ENDED_TRANSPORT",
    "ENDED_TRUNCATED",
    "LINEAGE_ENDED",
    "TOOL_ARGUMENTS_INVALID",
    "TOOL_REFUSED",
    "LoopHistory",
    "LoopResult",
    "anthropic_tools",
    "file_tool",
    "grep_file",
    "list_files",
    "openai_tools",
    "read_file",
    "run_workshop_loop",
    "schema_violation",
    "user_message",
]
