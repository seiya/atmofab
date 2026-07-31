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


class _EnvProxyHandler(urllib.request.ProxyHandler):
    """A `ProxyHandler` whose BYPASS decision also comes from the supplied environment.

    `ProxyHandler` takes its proxy URLs from the mapping it is constructed with, but decides
    whether to bypass by calling `proxy_bypass`, which reads the process-global `os.environ`.
    So a run whose environment sets `NO_PROXY=localhost` alongside a proxy would send a
    LOOPBACK request — and its Authorization header — to that proxy, because the global
    environment never mentioned the exemption. The bypass is evaluated against the same map the
    proxies came from."""

    def proxy_open(self, req, proxy, type):      # noqa: A002 - urllib's signature
        if urllib.request.proxy_bypass_environment(req.host, self.proxies):
            return None                          # handled without a proxy
        return super().proxy_open(req, proxy, type)


def _env_proxies(env: "Mapping[str, str]") -> "dict[str, str]":
    """`env`'s proxy settings in `getproxies_environment` form — `{"http": ..., "no": ...}`.

    `no_proxy` is kept under the `"no"` key that `proxy_bypass_environment` reads, which is
    what makes the exemption travel with the proxies rather than being read from the global
    environment."""
    proxies: dict[str, str] = {}
    for key, value in env.items():
        name = key.lower()
        if not value or not name.endswith("_proxy"):
            continue
        proxies[name[: -len("_proxy")]] = value
    return proxies


def _default_opener(env: "Mapping[str, str] | None" = None) -> "Callable[..., Any]":
    """An opener that refuses redirects and honours `env`'s proxy settings.

    `env` is the CONDUCTOR's environment, which is what every other leaf runs under: urllib
    would otherwise read the process-global `os.environ`, so a run whose proxy (or endpoint
    routing) was set for the workflow would silently bypass it."""
    handlers: list[Any] = [_NoRedirects]
    if env is not None:
        handlers.append(_EnvProxyHandler(_env_proxies(env)))
    return urllib.request.build_opener(*handlers).open


# Ceiling on a response body. A pure leaf answers with one JSON document; anything past this is
# not an answer, and reading it unbounded is how a trickling endpoint holds the run open.
_MAX_RESPONSE_BYTES = 32 * 1024 * 1024
_READ_CHUNK_BYTES = 65536
# An error body is only ever a diagnostic excerpt (`_ERROR_DETAIL_CHARS` of it survives), so it
# gets its own, far smaller ceiling: reading 32 MiB to keep 400 characters is not a bound worth
# having, and a gateway emitting an enormous error page is exactly when it matters.
# How far to unwrap looking for a socket. The deepest real chain is four
# (`HTTPError -> HTTPResponse -> BufferedReader -> SocketIO -> socket`); the bound exists so a
# self-referential or unexpectedly deep object cannot spin here.
_SOCKET_UNWRAP_MAX_DEPTH = 8
_MAX_ERROR_BODY_BYTES = 64 * 1024
_ERROR_DETAIL_CHARS = 400


def _set_socket_timeout(response: Any, seconds: float) -> None:
    """Best-effort: set the underlying socket's timeout, so a blocking receive cannot outlive
    the caller's deadline. Silent on any object that does not expose one — a test double, or a
    future response type — because this narrows a bound rather than establishing it.

    The wrapper chain is walked GENERICALLY rather than by a fixed path, because the two
    response types nest differently: a success is `HTTPResponse -> fp(BufferedReader) ->
    raw(SocketIO) -> _sock`, while an `HTTPError` adds a layer (`HTTPError -> fp(HTTPResponse)
    -> ...`). A fixed `fp, raw, _sock` walk reached the socket for the first and not the
    second, so a 503 whose body went silent ran for the deadline PLUS a full socket timeout —
    measured 3.5 s against a 2 s bound."""
    seen: set[int] = set()
    for _ in range(_SOCKET_UNWRAP_MAX_DEPTH):
        if response is None or id(response) in seen:
            return
        seen.add(id(response))
        settimeout = getattr(response, "settimeout", None)
        if callable(settimeout):
            try:
                settimeout(max(seconds, 0.001))
            except (OSError, ValueError):        # pragma: no cover - defensive
                pass
            return
        for attribute in ("fp", "raw", "_sock"):
            nested = getattr(response, attribute, None)
            if nested is not None:
                response = nested
                break
        else:
            return


def _read_bounded(response: Any, deadline: float,
                  max_bytes: int = _MAX_RESPONSE_BYTES) -> "tuple[bytes | None, str | None]":
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
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, "response_deadline_exceeded"
        # Shrink the SOCKET timeout to what is left. `urlopen(timeout=)` applies to each
        # receive independently, so a server that sends a byte just before the deadline and
        # then stalls would otherwise buy itself another full timeout — up to twice the
        # configured bound (nearly 1800 s at the 900 s default). Best-effort: on any object
        # that does not expose a socket, the loop still terminates at the deadline, one
        # socket timeout late, which is where this started.
        _set_socket_timeout(response, remaining)
        try:
            chunk = read_once(_READ_CHUNK_BYTES)
        except TimeoutError:
            # The socket timeout we just narrowed is what fired, so report the DEADLINE rather
            # than a bare `timed out`: the two are the same event here, and only one of them
            # names the setting the operator can change.
            if time.monotonic() >= deadline:
                return None, "response_deadline_exceeded"
            raise
        if not chunk:
            return b"".join(chunks), None
        total += len(chunk)
        if total > max_bytes:
            return None, f"response_too_large: over {max_bytes} bytes"
        chunks.append(chunk)


# What replaces a credential wherever one is found in provider-supplied text.
_REDACTED = "[redacted-api-key]"


def _redact(text: str, secret: str) -> str:
    """`text` with `secret` removed.

    The key-secrecy contract has to survive text this module did not write. A provider — a
    debug gateway, a verbose proxy, a misconfigured server — may echo the request headers in a
    4xx/5xx body, and that body is BOTH persisted under `launches/` and emitted in an event.
    Redacting is cheap; reasoning about which endpoints echo is not.

    Applied ONLY to the diagnostic copies — the persisted raw body and the transport-error
    string — never to the document the run reads. A key that is a common substring would
    otherwise corrupt a valid reply. RESIDUAL: a provider that echoed the key inside a
    SUCCESSFUL completion's content would put it in the model's answer channel, which is
    persisted as the leaf's stdout; scrubbing that channel is not possible without corrupting
    answers, and it is not a shape any provider produces."""
    if not secret:
        return text
    return text.replace(secret, _REDACTED)


def redact_secret(text: str, entry: Any,
                  env: "Mapping[str, str] | None" = None) -> str:
    """`text` with `entry`'s API key removed, for a caller that persists provider-supplied
    text outside this module.

    The conductor needs this for the model's ANSWER channel: that value cannot be redacted in
    place (the validators parse it, and a key that is a common substring would corrupt a
    legitimate document), so the redaction happens on the copy written to disk."""
    secret, _ = _api_key(entry, env)
    return _redact(text, secret)


def _post_json(
    url: str,
    payload: Mapping[str, Any],
    headers: Mapping[str, str],
    *,
    timeout_s: float,
    secret: str = "",
    env: "Mapping[str, str] | None" = None,
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
    open_url = opener if opener is not None else _default_opener(env)
    deadline = time.monotonic() + timeout_s
    try:
        with open_url(request, timeout=timeout_s) as response:
            raw, read_error = _read_bounded(response, deadline)
        if read_error is not None:
            return None, "", read_error
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            # Through the SAME bounded reader as a success body. `exc.read()` is an unbounded
            # blocking read: a gateway that trickles, or that sends an enormous error page, held
            # the run past `timeout_s` and grew memory without limit — measured, `timeout_s=2`
            # was still blocked at 25 s on a 503 whose body dribbled.
            body, body_error = _read_bounded(exc, deadline, _MAX_ERROR_BODY_BYTES)
            # Redact BEFORE the length limit: slicing first can cut through the middle of the
            # key, and the exact-string replace then matches nothing while a prefix of the
            # secret survives into `raw_response` and the emitted event.
            detail = ("" if body is None
                      else _redact(body.decode("utf-8", "replace"), secret)[:_ERROR_DETAIL_CHARS])
            if body_error is not None:
                detail = (detail + f" [error body {body_error}]").strip()
        except Exception:                       # noqa: BLE001 - diagnostics only
            detail = ""
        # `HTTP <code>`, spaced: the conductor classifies a leaf's terminal line with patterns
        # anchored on `\bhttp\b`, and `http_status_429` is one word to a regex — a terse
        # rate-limit body would then match nothing, and a transient outage would fail the run
        # closed instead of being retried.
        # `exc.reason` is provider-controlled as much as the body is, and it is what the
        # message falls back to when the body was empty or unreadable.
        return None, detail, (f"HTTP {exc.code} from provider: "
                              f"{detail or _redact(str(exc.reason), secret)}")
    except Exception as exc:                    # noqa: BLE001 - DNS/TLS/timeout/socket
        # The exception's own text can carry the URL, which an operator may have embedded a
        # credential in; redact for the same reason as the body.
        return None, "", _redact(f"{type(exc).__name__}: {exc}", secret)
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    # Parse the ORIGINAL, return the REDACTED copy. Redacting first would mutate the provider's
    # document before it is read: a local endpoint whose key is a short word (`test`, `local`,
    # `EMPTY` — the placeholders vLLM and Ollama are configured with) turns a valid reply into
    # an unparseable one, or silently rewrites the model name it reports. The redacted string is
    # what gets persisted and emitted; the parsed values are what the run uses.
    redacted = _redact(text, secret)
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, redacted, _redact(f"response_not_json: {exc}", secret)
    if not isinstance(doc, dict):
        return None, redacted, f"response_not_an_object: {type(doc).__name__}"
    return doc, redacted, None


def _api_key(entry: Any,
             env: "Mapping[str, str] | None" = None) -> "tuple[str, str | None]":
    name = (entry.api_key_env or "").strip()
    if not name:
        return "", "missing_api_key_env: the entry declares no api_key_env"
    # The CONDUCTOR's environment when it supplies one — the same environment every spawned
    # leaf receives. Reading the process-global one would take a credential the run did not
    # choose, or miss one the run did.
    source = env if env is not None else os.environ
    value = (source.get(name) or "").strip()
    if not value:
        # Names the VARIABLE, never a value — this string reaches logs and artifacts.
        return "", f"missing_api_key: environment variable {name} is unset or empty"
    return value, None


def _openai_request(
    entry: Any, messages: "Sequence[Mapping[str, str]]", max_output_tokens: int,
    env: "Mapping[str, str] | None" = None,
) -> "tuple[str, dict[str, Any], dict[str, str], str | None]":
    key, error = _api_key(entry, env)
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
    # Only when configured: the field is meaningful to reasoning models and rejected by some
    # servers that do not implement it, so an absent level must not put it on the wire.
    if getattr(entry, "effort", ""):
        payload["reasoning_effort"] = entry.effort
    return url, payload, {"Authorization": f"Bearer {key}"}, None


def _anthropic_request(
    entry: Any, messages: "Sequence[Mapping[str, str]]", max_output_tokens: int,
    env: "Mapping[str, str] | None" = None,
) -> "tuple[str, dict[str, Any], dict[str, str], str | None]":
    key, error = _api_key(entry, env)
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
    env: "Mapping[str, str] | None" = None,
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

    url, payload, headers, error = build_request(entry, messages, limit, env)
    if error is not None:
        return HttpLeafResponse("", "", None, False, error, "")
    # `secret` is the key just resolved for this request: everything this call returns —
    # the raw body (persisted under `launches/`) and the transport error (emitted as an event)
    # — is provider-supplied text that may echo it back.
    secret, _ = _api_key(entry, env)
    doc, raw, error = _post_json(url, payload, headers, timeout_s=timeout,
                                 secret=secret, env=env, opener=opener)
    if error is not None or doc is None:
        return HttpLeafResponse("", "", None, False, error or "empty_response", raw)
    text, model, usage, truncated, read_error = read_response(doc)
    if read_error is not None:
        return HttpLeafResponse("", "", None, False, read_error, raw)
    # `model or entry.model`: the response's own value is the provenance ground truth (a
    # gateway may resolve an alias), and the configured one is the honest fallback.
    #
    # Redacted, unlike `text`: this value is only ever PERSISTED (the `agent_runs` row, the
    # per-attempt metadata) — nothing parses it — so removing a credential from it costs
    # nothing but a mangled model name in the case where the key is a substring of one, which
    # is the trade the answer channel could not make.
    return HttpLeafResponse(text, _redact(model, secret) or entry.model,
                            usage or None, truncated, None, raw)


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
