#!/usr/bin/env python3
"""HTTP transport for a Z2 pure leaf (issue #28).

A *pure leaf* is one typed document in, one JSON document out: the host assembles a closed
context, the model answers once, and the host validates and writes the result. Nothing in that
contract needs a process. This module speaks it over HTTPS instead, so `generate.generate` /
`generate.verify` can run on an operator-hosted endpoint (vLLM, llama.cpp, Ollama, a hosted
OpenAI-compatible API) or on the Anthropic Messages API, while the expensive agentic substeps
stay where they are.

**Why there is no sandbox here.** bwrap confines a leaf because an agentic leaf runs
model-directed tools against the filesystem. An HTTP pure leaf runs none: the response is DATA,
handed to the same `extract_json_document` / bundle / verdict validators the CLI pure leaf's
response goes through, and the leaf's write authority is the empty set either way (the host
still runs the FS-diff over empty write_roots). What IS new is egress: the conductor now
originates outbound HTTPS to an operator-configured `base_url`. `urllib` honours the standard
proxy environment variables, so an operator who routes egress through a proxy keeps doing so.

**The key is never in the config.** A config names the ENVIRONMENT VARIABLE
(`api_key_env`); this module reads it at call time and puts it in a header. It is never logged,
never returned, never persisted — the raw response body IS persisted by the caller, so the
request side is where that discipline has to live.

Stdlib only (`urllib`), and importing only `tools.pure_leaf` for the shared prompt/category
constants — the same transport-substrate role `pure_leaf.py` plays for the CLI path.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping, NamedTuple, Sequence

from tools.pure_leaf import PURE_SYSTEM_PROMPT

# Wire version required by the Anthropic Messages API. Pinned, not "latest": the response shape
# this module parses is the one this version promises.
ANTHROPIC_VERSION = "2023-06-01"

# A pure leaf's answer is one document; a request that has not answered in this long is not
# going to. The caller may override per entry (`timeout_s`).
DEFAULT_HTTP_TIMEOUT_SECONDS = 900


class HttpLeafResponse(NamedTuple):
    """One HTTP pure-leaf turn.

    `transport_error` is set when the turn did not produce an answer at all — DNS, TLS, a
    timeout, a 4xx/5xx, a non-JSON body, a body missing the fields the shape promises, or a
    missing API key. The caller routes it exactly as it routes a nonzero leaf exit
    (`pure_transport`), because it is the same thing: no document to repair.

    `truncated` means the provider itself said the answer was cut off at the output-token
    ceiling. It is authoritative — more so than inspecting the partial text — so the caller
    classifies it as `pure_response_truncated` without consulting the extractor.
    """

    text: str
    model: str
    usage: "dict[str, Any] | None"
    truncated: bool
    transport_error: "str | None"
    raw_response: str


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect.

    `urlopen` follows them by default and copies the request headers onto the new request with
    no same-origin check, so a redirecting endpoint — misconfigured or hostile — harvests the
    operator's API key and gets to supply the leaf's "answer". A single-shot JSON POST has no
    legitimate use for a redirect, so the safe rule is the simple one."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, D102
        raise urllib.error.HTTPError(
            req.full_url, code,
            f"redirect refused (would forward credentials to {newurl})", headers, fp)


def _default_opener() -> "Callable[..., Any]":
    return urllib.request.build_opener(_NoRedirects).open


# Ceiling on a response body. A pure leaf answers with one JSON document; anything past this is
# not an answer, and reading it unbounded is how a trickling endpoint holds the run open.
_MAX_RESPONSE_BYTES = 32 * 1024 * 1024
_READ_CHUNK_BYTES = 65536


def _read_bounded(response: Any, deadline: float) -> "tuple[bytes | None, str | None]":
    """Read the body under a WALL-CLOCK deadline and a size ceiling.

    `urlopen(timeout=)` is a per-socket-OPERATION timeout: it resets on every recv, so an
    endpoint dribbling one byte below the interval never trips it. The CLI leaf has a process
    to kill and a `leaf_timeout` event; this path has neither, so the bound has to be here.

    Read via `read1`, not `read`. `HTTPResponse.read(n)` loops INTERNALLY until it has n bytes
    or EOF, so a deadline checked between calls bounds nothing: a trickling server keeps that
    one call alive indefinitely while every inner recv resets the socket timeout (measured:
    `timeout_s=2` still blocked past 40s). `read1` returns what one recv produced, which makes
    each iteration socket-timeout-bounded and the check between them effective. The worst case
    is therefore the deadline plus one socket timeout, not unbounded. `read` is the fallback
    only for a test double that does not implement `read1`."""
    read_once = getattr(response, "read1", None) or response.read
    chunks: list[bytes] = []
    total = 0
    while True:
        if time.monotonic() > deadline:
            return None, "response_deadline_exceeded"
        chunk = read_once(_READ_CHUNK_BYTES)
        if not chunk:
            return b"".join(chunks), None
        total += len(chunk)
        if total > _MAX_RESPONSE_BYTES:
            return None, f"response_too_large: over {_MAX_RESPONSE_BYTES} bytes"
        chunks.append(chunk)


def _post_json(
    url: str,
    payload: Mapping[str, Any],
    headers: Mapping[str, str],
    *,
    timeout_s: float,
    opener: "Callable[..., Any] | None",
) -> "tuple[dict[str, Any] | None, str, str | None]":
    """`(document, raw_body, transport_error)` for one JSON POST.

    An HTTP error status is a TRANSPORT error here, unlike in preflight's reachability probe:
    preflight asks "is anything there", this asks "did the model answer". A 429 or a 503 means
    it did not, and the caller's retry/fail-closed handling is what should see that."""
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", **dict(headers)})
    open_url = opener if opener is not None else _default_opener()
    deadline = time.monotonic() + timeout_s
    try:
        with open_url(request, timeout=timeout_s) as response:
            raw, read_error = _read_bounded(response, deadline)
        if read_error is not None:
            return None, "", read_error
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:400]
        except Exception:                       # noqa: BLE001 - diagnostics only
            detail = ""
        # `HTTP <code>`, spaced: the conductor classifies a leaf's terminal line with patterns
        # anchored on `\bhttp\b`, and `http_status_429` is one word to a regex — a terse
        # rate-limit body would then match nothing, and a transient outage would fail the run
        # closed instead of being retried.
        return None, detail, f"HTTP {exc.code} from provider: {detail or exc.reason}"
    except Exception as exc:                    # noqa: BLE001 - DNS/TLS/timeout/socket
        return None, "", f"{type(exc).__name__}: {exc}"
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, text, f"response_not_json: {exc}"
    if not isinstance(doc, dict):
        return None, text, f"response_not_an_object: {type(doc).__name__}"
    return doc, text, None


def _api_key(entry: Any) -> "tuple[str, str | None]":
    name = (entry.api_key_env or "").strip()
    if not name:
        return "", "missing_api_key_env: the entry declares no api_key_env"
    value = os.environ.get(name, "").strip()
    if not value:
        # Names the VARIABLE, never a value — this string reaches logs and artifacts.
        return "", f"missing_api_key: environment variable {name} is unset or empty"
    return value, None


def _openai_request(
    entry: Any, messages: "Sequence[Mapping[str, str]]", max_output_tokens: int
) -> "tuple[str, dict[str, Any], dict[str, str], str | None]":
    key, error = _api_key(entry)
    if error is not None:
        return "", {}, {}, error
    url = entry.base_url.rstrip("/") + "/chat/completions"
    payload: dict[str, Any] = {
        "model": entry.model,
        "messages": [{"role": "system", "content": PURE_SYSTEM_PROMPT}, *messages],
        # `max_tokens`, not `max_completion_tokens`: every local server (vLLM, llama.cpp,
        # Ollama) speaks the former, and it is the compatibility surface this provider is named
        # for. Hosted OpenAI has since renamed it; switching is a follow-up for when a run
        # actually targets it.
        "max_tokens": max_output_tokens,
    }
    return url, payload, {"Authorization": f"Bearer {key}"}, None


def _anthropic_request(
    entry: Any, messages: "Sequence[Mapping[str, str]]", max_output_tokens: int
) -> "tuple[str, dict[str, Any], dict[str, str], str | None]":
    key, error = _api_key(entry)
    if error is not None:
        return "", {}, {}, error
    url = entry.base_url.rstrip("/") + "/v1/messages"
    payload: dict[str, Any] = {
        "model": entry.model,
        # The system channel is pinned to the same fixed prompt the CLI pure leaf gets via
        # `--system-prompt`, so the model's total input stays a function of the host-assembled
        # body alone (A2) on either transport.
        "system": PURE_SYSTEM_PROMPT,
        "messages": list(messages),
        # Required by this API, unlike OpenAI's, where it is optional.
        "max_tokens": max_output_tokens,
    }
    return url, payload, {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}, None


def _read_openai_response(doc: Mapping[str, Any]) -> "tuple[str, str, dict, bool, str | None]":
    choices = doc.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return "", "", {}, False, "response_missing_choices"
    choice = choices[0]
    message = choice.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        return "", "", {}, False, "response_missing_message_content"
    usage = doc.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    normalized = {
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
    }
    model = doc.get("model")
    return (content, model if isinstance(model, str) else "",
            {k: v for k, v in normalized.items() if isinstance(v, int)},
            choice.get("finish_reason") == "length", None)


def _read_anthropic_response(doc: Mapping[str, Any]) -> "tuple[str, str, dict, bool, str | None]":
    blocks = doc.get("content")
    if not isinstance(blocks, list):
        return "", "", {}, False, "response_missing_content"
    # Concatenate the TEXT blocks: a pure leaf is told to answer with one document, but a
    # provider may still split it across blocks, and dropping any of them would corrupt the
    # document into an unparseable one and blame the model for it.
    text = "".join(
        block.get("text", "") for block in blocks
        if isinstance(block, dict) and block.get("type") == "text"
        and isinstance(block.get("text"), str))
    if not text:
        return "", "", {}, False, "response_has_no_text_block"
    usage = doc.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    normalized = {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
    }
    model = doc.get("model")
    return (text, model if isinstance(model, str) else "",
            {k: v for k, v in normalized.items() if isinstance(v, int)},
            doc.get("stop_reason") == "max_tokens", None)


_SHAPES: Mapping[str, tuple[Callable[..., Any], Callable[..., Any]]] = {
    "openai_compatible": (_openai_request, _read_openai_response),
    "anthropic_api": (_anthropic_request, _read_anthropic_response),
}


def run_pure_http_leaf(
    entry: Any,
    messages: "Sequence[Mapping[str, str]]",
    *,
    timeout_s: "float | None" = None,
    max_output_tokens: "int | None" = None,
    opener: "Callable[..., Any] | None" = None,
) -> HttpLeafResponse:
    """One pure-leaf turn against `entry`'s HTTP provider.

    `messages` is the conversation so far in OpenAI/Anthropic role form — for a first attempt
    a single user turn, for a repair the prior turns plus the critique (the in-process analog
    of the CLI path's `--resume --fork-session`). The system channel is supplied here, not by
    the caller, so both shapes get the same fixed `PURE_SYSTEM_PROMPT`.

    Never raises: every failure comes back as `transport_error`, because the caller's job is to
    turn it into a substep outcome, not to unwind."""
    shape = _SHAPES.get(entry.provider)
    if shape is None:
        return HttpLeafResponse(
            "", "", None, False,
            f"unsupported_http_provider: {entry.provider!r}", "")
    build_request, read_response = shape
    limit = int(max_output_tokens or entry.max_output_tokens or DEFAULT_MAX_OUTPUT_TOKENS)
    timeout = float(timeout_s or entry.timeout_s or DEFAULT_HTTP_TIMEOUT_SECONDS)

    url, payload, headers, error = build_request(entry, messages, limit)
    if error is not None:
        return HttpLeafResponse("", "", None, False, error, "")
    doc, raw, error = _post_json(url, payload, headers, timeout_s=timeout, opener=opener)
    if error is not None or doc is None:
        return HttpLeafResponse("", "", None, False, error or "empty_response", raw)
    text, model, usage, truncated, read_error = read_response(doc)
    if read_error is not None:
        return HttpLeafResponse("", "", None, False, read_error, raw)
    # `model or entry.model`: the response's own value is the provenance ground truth (a
    # gateway may resolve an alias), and the configured one is the honest fallback.
    return HttpLeafResponse(text, model or entry.model, usage or None, truncated, None, raw)


# The default `max_tokens` when neither the entry nor the caller names one. Deliberately NOT
# the CLI leaf's 128000 ceiling: that is a Claude-CLI budget, and sending it verbatim to a local
# server (vLLM, llama.cpp, Ollama — the endpoints `openai_compatible` is named for) is rejected
# outright as a client error, because it exceeds the model's whole context length.
#
# 32768 is sized on the artifact: the largest `CodegenBundle` in this repository is ~45 kB of
# JSON, which is roughly 15-20k tokens, and a ceiling below that turns every run into a
# truncation-repair loop — a worse failure than a rejected request, because it is slow and
# looks like a model problem. An operator whose endpoint cannot take 32768 sets
# `max_output_tokens:` on the entry; so does one whose model can take more.
DEFAULT_MAX_OUTPUT_TOKENS = 32768
