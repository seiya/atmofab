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
originates outbound HTTPS to an operator-configured `base_url`. This module still ASKS urllib
to honour the standard proxy environment variables, and it is handed the leaf's environment to
read them from — but since the leaf's environment became a declared allowlist (issue #63), the
proxy families are a NAMED EXCLUSION (`orchestration_runtime.LEAF_ENV_NAMED_EXCLUSIONS`), so
`_env_proxies` finds nothing and an operator who routes egress through a proxy NO LONGER keeps
doing so on this path. Measured, not inferred. The mechanism below is kept rather than deleted
because the exclusion is a decision that can be revisited by adding the names back, and the
handler is what makes that a one-line change; the exclusion's own comment carries the reasons.
An asymmetry worth knowing: the HTTP PREFLIGHT (`orchestration_runtime`'s reachability probe)
calls `urllib.request.urlopen` bare, so it still reads the process-global environment and still
goes through the operator's proxy. On a proxied host preflight therefore passes while the leaf
call it is vouching for cannot reach the same endpoint.

**The key is never in the config.** A config names the ENVIRONMENT VARIABLE
(`api_key_env`); this module reads it at call time and puts it in a header. It is never logged,
never returned, never persisted — the raw response body IS persisted by the caller, so the
request side is where that discipline has to live.

Stdlib only (`urllib`), and importing only `tools.pure_leaf` for the shared prompt/category
constants — the same transport-substrate role `pure_leaf.py` plays for the CLI path — plus
`tools.leaf_usage` for the ONE usage shape every backend's numbers are recorded in.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Iterator, Mapping, NamedTuple, Sequence

from tools.leaf_usage import LEAF_USAGE_SOURCE_HTTP, normalize_leaf_usage
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

    `env` is the LEAF's environment — the dict `workflow_conductor._child_env` reconstructed
    for this launch, which is what every other leaf of the run gets. (It was the conductor's
    own environment when this was written, and the two stopped being the same thing when the
    leaf environment became a declared allowlist.) Reading it rather than the process-global
    `os.environ` is what keeps this call on the same footing as every other leaf: today that
    means NO proxy, because the proxy families are a named exclusion — see the module
    docstring."""
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


def _iter_bounded(response: Any, deadline: float,
                  max_bytes: int = _MAX_RESPONSE_BYTES
                  ) -> "Iterator[tuple[bytes, str | None]]":
    """Yield the body one receive at a time under a WALL-CLOCK deadline and a size ceiling.

    `urlopen(timeout=)` is a per-socket-OPERATION timeout: it resets on every recv, so an
    endpoint dribbling one byte below the interval never trips it. The CLI leaf has a process
    to kill and a `leaf_timeout` event; this path has neither, so the bound has to be here.

    Read via `read1`, not `read`. `HTTPResponse.read(n)` loops INTERNALLY until it has n bytes
    or EOF, so a deadline checked between calls bounds nothing: a trickling server keeps that
    one call alive indefinitely while every inner recv resets the socket timeout (measured:
    `timeout_s=2` still blocked past 40s). `read1` returns what one recv produced, which makes
    each iteration socket-timeout-bounded and the check between them effective. The worst case
    is therefore the deadline plus one socket timeout, not unbounded. `read` is the fallback
    only for a test double that does not implement `read1`.

    `error` is non-None on exactly the LAST item yielded, and its chunk is then empty; a clean
    EOF simply ends the iteration. A caller collecting bytes stops at the error, and a caller
    that has already handed earlier chunks to a parser still learns the body never finished —
    which is what lets the streaming reader tell a complete answer from a severed one."""
    read_once = getattr(response, "read1", None) or response.read
    total = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            yield b"", "response_deadline_exceeded"
            return
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
                yield b"", "response_deadline_exceeded"
                return
            raise
        if not chunk:
            return
        total += len(chunk)
        if total > max_bytes:
            yield b"", f"response_too_large: over {max_bytes} bytes"
            return
        yield chunk, None


def _read_bounded(response: Any, deadline: float,
                  max_bytes: int = _MAX_RESPONSE_BYTES) -> "tuple[bytes | None, str | None]":
    """The whole body, or `(None, error)`. A thin fold over `_iter_bounded`, so the deadline and
    ceiling discipline has exactly one implementation for both the JSON and the SSE path."""
    chunks: list[bytes] = []
    for chunk, error in _iter_bounded(response, deadline, max_bytes):
        if error is not None:
            return None, error
        chunks.append(chunk)
    return b"".join(chunks), None


# A server-sent-events frame ends at a BLANK line, and both `\n` and `\r\n` are line
# terminators. Both spellings, because an intermediary that rewrites line endings would
# otherwise merge every frame in the stream into one unparseable blob. Split on BYTES rather
# than on decoded text: the separator is pure ASCII, so a frame boundary can never fall inside
# a multi-byte character, which is what makes per-frame decoding safe when a receive splits one.
_SSE_FRAME_SEPARATOR = re.compile(rb"\r?\n\r?\n")
_SSE_LINE_SEPARATOR = re.compile(rb"\r?\n")
# What an event stream's FIRST line looks like: one of the four defined fields, or a comment
# (the keepalive an idle intermediary is fed). Anchored at the start of the body, matched
# whether or not that line is terminated yet.
_SSE_OPENING_LINE = re.compile(rb"^(?:data|event|id|retry)\s*:|^:")
# The wire format says a leading BOM "must be ignored". Stripped once, at the buffer, so both
# the framer and the opening-line test below see the same bytes: leaving it in made the first
# field name `\xef\xbb\xbfdata`, which matches nothing, so the FIRST FRAME of a conforming
# stream was silently dropped.
_UTF8_BOM = b"\xef\xbb\xbf"


def _looks_like_an_event_stream(received: bytes) -> bool:
    """Whether what arrived was an event stream at all, decided on how it OPENS.

    The question this answers is "did the endpoint honour `stream: true`". Three weaker tests
    were tried and each was wrong in a way that was reproduced:

      * "does the body contain `data:`" called a keepalive-only stream (`: ping` frames, which
        is what a gateway sends during a long time-to-first-token) a wrong content type and
        failed it closed non-retryably, while letting an ordinary JSON answer whose text merely
        quoted `data:` through;
      * "did any complete FRAME arrive" inverted both of those: a real stream severed before its
        first blank line failed closed, and any body with a blank line in it — an HTML error
        page — was reported as a retryable severance;
      * requiring the field at byte 0 rejected a CONFORMING stream that flush-primes with a
        newline, or carries a BOM. Blank lines are legal separators and a leading BOM is
        specified to be ignored, so the framer parsed those bodies correctly and only this test
        refused them — discarding a complete, fully billed answer under a message that told the
        operator to set `stream: false` on an endpoint that speaks SSE properly.

    So: skip a BOM and any leading blank lines, then look at the first line with content. An
    event stream opens with a field or a comment; an HTML page opens with `<` and a JSON body
    with `{`.

    RESIDUAL, stated rather than papered over: a body severed inside the first four or five
    bytes (`dat`, `even`) has not yet produced a token to recognise and is reported as "not an
    event stream" — a non-retryable answer to what was really a severance. The window is those
    bytes only, one byte later the colon arrives and it classifies correctly, and prefix-matching
    partial field names would add a fresh way to be wrong for a case worth this little."""
    body = received[len(_UTF8_BOM):] if received.startswith(_UTF8_BOM) else received
    return _SSE_OPENING_LINE.search(body.lstrip(b"\r\n")) is not None


class _SseBuffer:
    """Incremental frame splitter: bytes in, complete `(event, data)` frames out.

    Searches only the NEWLY ARRIVED region of the buffer, rewound by three bytes so a separator
    split across two receives is still found. Re-scanning the whole buffer on every receive is
    quadratic, and a stream that never completes a frame turns that into a hang rather than the
    intended size refusal — measured, a body with no frame boundary at all spent over 30 s in
    `memcpy` before reaching the 32 MiB ceiling that was supposed to stop it in milliseconds.

    Whatever is still pending at end of stream is DISCARDED by the caller, never emitted: it is
    a truncated frame, and reading half a JSON object as a whole one is the exact failure this
    module refuses to produce."""

    # `len(b"\r\n\r\n") - 1` — the most of a separator that can sit in the previous receive.
    _OVERLAP = 3

    def __init__(self) -> None:
        self._pending = bytearray()
        self._scanned = 0
        self._bom_settled = False

    def feed(self, chunk: bytes) -> "list[tuple[str, str]]":
        """The frames completed by `chunk`. A frame carrying no `data:` line at all (one made
        only of keepalive comments) is dropped here rather than handed on as an empty answer."""
        self._pending += chunk
        if not self._bom_settled:
            # A leading BOM is specified to be ignored, and left in place it becomes part of the
            # first field NAME (`\xef\xbb\xbfdata`), silently dropping the first frame of a
            # conforming stream. Decided on the buffer rather than on the first chunk, because a
            # three-byte marker can arrive split across receives; while the buffer is still a
            # strict prefix of the marker the decision waits, which costs nothing — no frame
            # separator fits in those bytes.
            if len(self._pending) < len(_UTF8_BOM) and _UTF8_BOM.startswith(self._pending):
                return []
            self._bom_settled = True
            if self._pending.startswith(_UTF8_BOM):
                del self._pending[:len(_UTF8_BOM)]
        frames: list[tuple[str, str]] = []
        start = max(0, self._scanned - self._OVERLAP)
        while True:
            match = _SSE_FRAME_SEPARATOR.search(self._pending, start)
            if match is None:
                break
            event, data = _parse_sse_frame(bytes(self._pending[:match.start()]))
            if data:
                frames.append((event, data))
            del self._pending[:match.end()]
            start = 0
        self._scanned = len(self._pending)
        return frames


def _parse_sse_frame(frame: bytes) -> "tuple[str, str]":
    """`(event name, data)` for one frame, per the EventSource wire format.

    Every clause is against a real endpoint rather than a spec reading:
      * a line opening with `:` is a COMMENT — the keepalive that holds an idle intermediary
        open — so it counts as bytes that arrived and contributes nothing to the answer;
      * repeated `data:` lines in one frame join with a newline (the Messages API does not use
        this, some gateways do, and dropping the later ones truncates the document silently);
      * exactly one space after the colon is stripped, and only one;
      * `id:` / `retry:` / any unknown field is ignored rather than rejected, because a stream
        this module cannot fully model is still one it must read the answer out of."""
    event = ""
    data: list[str] = []
    for line in _SSE_LINE_SEPARATOR.split(frame):
        text = line.decode("utf-8", "replace")
        if not text or text.startswith(":"):
            continue
        field, _, value = text.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event = value
        elif field == "data":
            data.append(value)
    return event, "\n".join(data)


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


def _build_post(url: str, payload: Mapping[str, Any], headers: Mapping[str, str],
                *, env: "Mapping[str, str] | None",
                opener: "Callable[..., Any] | None") -> "tuple[Any, Callable[..., Any]]":
    """`(request, open_url)` for one JSON POST. Shared by the buffered and streaming paths so
    the redirect refusal and the environment's proxy settings cannot apply to only one."""
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", **dict(headers)})
    return request, (opener if opener is not None else _default_opener(env))


def _http_error_report(exc: urllib.error.HTTPError, deadline: float,
                       secret: str) -> "tuple[str, str]":
    """`(detail, message)` for an HTTP error status, identically on both post paths.

    A 504 arrives BEFORE the first byte of any answer, streaming or not, so this is the one
    report both paths share — and the one the incident this module was hardened against
    produced three times."""
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
    except Exception:                           # noqa: BLE001 - diagnostics only
        detail = ""
    # `HTTP <code>`, spaced: the conductor classifies a leaf's terminal line with patterns
    # anchored on `\bhttp\b`, and `http_status_429` is one word to a regex — a terse
    # rate-limit body would then match nothing, and a transient outage would fail the run
    # closed instead of being retried.
    # `exc.reason` is provider-controlled as much as the body is, and it is what the
    # message falls back to when the body was empty or unreadable.
    return detail, (f"HTTP {exc.code} from provider: "
                    f"{detail or _redact(str(exc.reason), secret)}")


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
    request, open_url = _build_post(url, payload, headers, env=env, opener=opener)
    deadline = time.monotonic() + timeout_s
    try:
        with open_url(request, timeout=timeout_s) as response:
            raw, read_error = _read_bounded(response, deadline)
        if read_error is not None:
            return None, "", read_error
    except urllib.error.HTTPError as exc:
        detail, message = _http_error_report(exc, deadline, secret)
        return None, detail, message
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


def _post_stream(
    url: str,
    payload: Mapping[str, Any],
    headers: Mapping[str, str],
    *,
    timeout_s: float,
    secret: str = "",
    env: "Mapping[str, str] | None" = None,
    opener: "Callable[..., Any] | None" = None,
) -> "tuple[list[tuple[str, str]] | None, str, str | None]":
    """`(frames, raw_body, transport_error)` for one server-sent-events POST.

    `frames` is the ordered `(event name, data)` list; the name is `""` for the OpenAI dialect,
    which sends bare `data:` lines. They are MATERIALIZED rather than handed to the reader as
    they arrive: the whole stream is already bounded by `_MAX_RESPONSE_BYTES`, and a list keeps
    each shape's reader a pure function over data — testable exactly the way the buffered
    readers are, with no socket in sight.

    `frames` is `None` ONLY when no stream could be read at all (an HTTP error status, which
    arrives before any of it). Every other failure returns the frames that DID arrive alongside
    the error, because the answer may already be complete: the provider's terminator can be on
    the wire before a severed teardown or an over-long keepalive trailer kills the read, and
    discarding a finished answer costs the whole billed generation.

    `raw_body` is returned even on failure, unlike `_post_json`'s empty string. A stream that
    died at 90% is the only evidence of WHERE it died, and `launches/<agent_run_id>
    .http_response.txt` exists to keep it."""
    request, open_url = _build_post(url, payload, headers, env=env, opener=opener)
    deadline = time.monotonic() + timeout_s
    frames: list[tuple[str, str]] = []
    received: list[bytes] = []
    buffer = _SseBuffer()
    try:
        with open_url(request, timeout=timeout_s) as response:
            for chunk, read_error in _iter_bounded(response, deadline):
                if read_error is not None:
                    return frames, _redact(_decode(received), secret), read_error
                received.append(chunk)
                frames.extend(buffer.feed(chunk))
    except urllib.error.HTTPError as exc:
        # A gateway timeout arrives before the first frame, so this is byte-for-byte the report
        # the buffered path gives — the conductor classifies both with the same patterns.
        detail, message = _http_error_report(exc, deadline, secret)
        return None, detail, message
    except Exception as exc:                    # noqa: BLE001 - DNS/TLS/timeout/socket
        # PREFIXED, unlike the buffered path's bare `TypeName: text`. This is the ordinary way a
        # severed stream surfaces: on `Transfer-Encoding: chunked` — the dominant encoding for
        # streaming — a connection cut mid-body raises `IncompleteRead` rather than reaching a
        # clean EOF, and `IncompleteRead(0 bytes read)` matches no classifier pattern at all.
        # Unclassified means non-retryable, so without the prefix the exact mid-stream severance
        # this transport exists to survive would fail the run closed while the close-delimited
        # form of the SAME event was retried.
        return frames, _redact(_decode(received), secret), _redact(
            f"{_STREAM_INTERRUPTED}: {type(exc).__name__}: {exc}", secret)
    raw = _decode(received)
    if received and not _looks_like_an_event_stream(b"".join(received)):
        # The endpoint ignored `stream: true` and answered with an ordinary body. Deliberately
        # NOT worded as an interruption: that reading is retryable, and this is a deterministic
        # misconfiguration that reproduces on every launch. Unclassified, so it fails closed on
        # the first attempt with the body itself as the evidence — which is the whole lesson of
        # the incident this transport was rewritten for.
        #
        # The test is how the body OPENS — see `_looks_like_an_event_stream`, which records the
        # two weaker tests that were tried and exactly what each of them got wrong.
        return None, _redact(raw, secret), (
            f"response_not_an_event_stream: {len(received)} reads opened with no event-stream "
            f"line; set `stream: false` on this entry if the endpoint cannot speak "
            f"server-sent events")
    return frames, _redact(raw, secret), None


def _decode(chunks: "Sequence[bytes]") -> str:
    """The received bytes as text. `replace`, never `strict`: a stream cut mid-character must
    still yield the evidence of where it was cut, not raise on the way to reporting it."""
    return b"".join(chunks).decode("utf-8", "replace")


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
    env: "Mapping[str, str] | None" = None, *, stream: bool,
) -> "tuple[str, dict[str, Any], dict[str, str], str | None]":
    key, error = _api_key(entry, env)
    if error is not None:
        return "", {}, {}, error
    url = entry.base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {key}"}
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
    if stream:
        payload["stream"] = True
        # An OpenAI-shaped stream carries NO usage unless this is asked for, and `usage` is what
        # reaches `ProcResult.usage` and the `agent_runs` row the operator's cost comparison is
        # read out of. The residual risk is a server that validates unknown body keys and answers
        # 400 — which is cheap and self-naming (milliseconds, no tokens, and the provider's own
        # message names `stream_options` in the persisted body), tagged `llm_client_error` so it
        # fails closed on the FIRST attempt instead of being retried, and recovered from with one
        # `stream: false` in the config. Losing token accounting silently is the worse trade.
        payload["stream_options"] = {"include_usage": True}
        headers["Accept"] = "text/event-stream"
    # No `"stream": false` when opted out: the escape hatch must send byte-for-byte the request
    # that worked before streaming existed, or the hatch is itself a new thing to be rejected by
    # the endpoint it exists for.
    return url, payload, headers, None


def _anthropic_request(
    entry: Any, messages: "Sequence[Mapping[str, str]]", max_output_tokens: int,
    env: "Mapping[str, str] | None" = None, *, stream: bool,
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
    headers = {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
    if stream:
        # No `stream_options` counterpart: this API puts usage in the stream natively, split
        # across `message_start` (input) and `message_delta` (output).
        payload["stream"] = True
        headers["Accept"] = "text/event-stream"
    return url, payload, headers, None


def _normalized_usage(usage: "dict[str, Any] | None") -> "dict[str, Any] | None":
    """A reader's canonical-name usage in the one shape every leaf backend records.

    The readers speak their provider's dialect; this is where the result joins the CLI path's
    shape — `total_tokens` derived, `usage_source` stamped, the detail objects moved
    under `provider_details`. `None` when the provider reported nothing usable, which the
    caller records as an explicit marker rather than as zero cost.
    """
    if not isinstance(usage, dict) or not usage:
        return None
    raw = dict(usage)
    return normalize_leaf_usage(raw, source=LEAF_USAGE_SOURCE_HTTP,
                                details=raw.pop("provider_details", None))


def _openai_usage(raw: Any) -> "dict[str, Any]":
    """An OpenAI-dialect `usage` object in the canonical leaf-usage names.

    Maps `prompt_tokens`/`completion_tokens` onto `input_tokens`/`output_tokens`, and lifts
    the two counts that dominate the bill out of their nested detail objects:
    `completion_tokens_details.reasoning_tokens` and `prompt_tokens_details.cached_tokens`.
    Both were dropped before, and on `orch_20260807T002410Z_acf2b996` both were the story:
    reasoning was 84% of `completion_tokens` on two `generate` calls and 99.6% on a `verify`
    call whose answer was ~100 tokens, and two otherwise identical `generate` calls reported
    64 vs 32,832 cached prompt tokens. Sizing `max_output_tokens` from `output_tokens` alone
    reads a number that is mostly reasoning without knowing it.

    Both detail objects also travel under `provider_details`, reduced to their INT-VALUED
    entries (see the comment below), so a count this map does not model is still on disk.
    `isinstance(v, int)` drops anything malformed rather than coercing it (a `bool` is an
    `int`, and is filtered downstream by `leaf_usage.normalize_leaf_usage`).
    """
    usage = raw if isinstance(raw, dict) else {}
    completion = usage.get("completion_tokens_details")
    prompt = usage.get("prompt_tokens_details")
    mapped = {
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
        "reasoning_tokens": (completion.get("reasoning_tokens")
                             if isinstance(completion, dict) else None),
        "cached_tokens": (prompt.get("cached_tokens")
                          if isinstance(prompt, dict) else None),
    }
    out: dict[str, Any] = {k: v for k, v in mapped.items() if isinstance(v, int)}
    # INT-VALUED entries only, not the objects verbatim. Everything else this module returns is
    # passed through `_redact` before it can be persisted, because a provider may echo the API
    # key back in any string it controls — and these dicts are persisted (the agent_runs row,
    # the per-attempt metadata) without going through the answer channel's redaction. A token
    # count cannot carry a key; a string-valued field this map does not model would.
    details: dict[str, Any] = {}
    for name, reported in (("completion_tokens_details", completion),
                           ("prompt_tokens_details", prompt)):
        counts = {k: v for k, v in (reported or {}).items()
                  if isinstance(v, int) and not isinstance(v, bool)} \
            if isinstance(reported, dict) else {}
        if counts:
            details[name] = counts
    if out and details:
        out["provider_details"] = details
    return out


def _anthropic_usage(raw: Any) -> "dict[str, Any]":
    """A Messages-API `usage` object in the canonical leaf-usage names.

    Its four token classes already carry those names, so this is a filter, not a map. The
    two cache classes were dropped before; they are separate prompt classes (not a subset of
    `input_tokens`), so losing them lost real billed input — and with it any view of whether
    the prompt cache was hitting.
    """
    usage = raw if isinstance(raw, dict) else {}
    keys = ("input_tokens", "output_tokens",
            "cache_read_input_tokens", "cache_creation_input_tokens")
    return {k: usage[k] for k in keys if isinstance(usage.get(k), int)}


def _read_openai_response(doc: Mapping[str, Any]) -> "tuple[str, str, dict, bool, str | None]":
    choices = doc.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return "", "", {}, False, "response_missing_choices"
    choice = choices[0]
    message = choice.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        return "", "", {}, False, "response_missing_message_content"
    model = doc.get("model")
    return (content, model if isinstance(model, str) else "",
            _openai_usage(doc.get("usage")),
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
    model = doc.get("model")
    return (text, model if isinstance(model, str) else "",
            _anthropic_usage(doc.get("usage")),
            doc.get("stop_reason") == "max_tokens", None)


# What an incomplete stream is reported as. The WORDING is load-bearing, not decoration: the
# conductor classifies a leaf's terminal text with `_LEAF_INFRA_ERROR_PATTERNS`, and a string
# that matches none of them is an unclassified nonzero exit — non-retryable, so a genuinely
# transient network fault would fail the run closed instead of being re-launched. `stream
# interrupted` matches the transport-flake alternative `\bstream (?:disconnected|interrupted|
# aborted)\b`. `stream error: ...` would NOT: that alternative requires the phrase to end the
# line, so any detail after it matches nothing.
_STREAM_INTERRUPTED = "stream interrupted"


def _read_openai_stream(
        frames: "Sequence[tuple[str, str]]") -> "tuple[str, str, dict, bool, str | None]":
    """Fold an OpenAI-dialect event stream into the same 5-tuple the buffered reader returns.

    Mirrors `_read_openai_response` decision for decision — only `choices[0].delta.content` is
    the answer, exactly as only `choices[0].message.content` is there. A `reasoning_content`
    delta is thinking, not the document, and is dropped for the same reason the buffered reader
    never looks for one."""
    text: list[str] = []
    model = ""
    usage: dict[str, Any] = {}
    finish_reason: "str | None" = None
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
            # A frame this dialect does not model (a gateway's own annotation). Skipping it is
            # safe; a stream made ONLY of them ends with neither terminator and is reported
            # below, so nothing is silently accepted.
            continue
        if not isinstance(doc, dict):
            continue
        detail = doc.get("error")
        # TRUTHY, not `is not None`. A proxy that stamps `"error": null` — or `""`, `{}`,
        # `false`, `0` — on every ordinary content chunk is a real shape, and treating any of
        # those as an error both dropped the chunk's content and reported nonsense
        # (`provider error event False`). With `""` the run got the worst outcome available: a
        # SUCCESSFUL turn carrying an empty document. A falsy error is not an error.
        if detail:
            # The mirror of the Messages API's `error` EVENT, which this dialect expresses as an
            # ordinary chunk carrying an `error` key. Without this the frame has no `choices`, is
            # skipped as unmodelled, and a `[DONE]` (or a `finish_reason` on an earlier chunk)
            # then reports an upstream failure as a COMPLETE answer — handing the validators a
            # truncated document to blame the model for, which is the outcome this module
            # refuses everywhere else.
            error_detail = (f"{detail.get('type', 'error')}: {detail.get('message', '')}"
                            if isinstance(detail, dict) else str(detail))
            continue
        if isinstance(doc.get("model"), str):
            model = doc["model"]
        chunk_usage = doc.get("usage")
        if isinstance(chunk_usage, dict):
            usage = _openai_usage(chunk_usage) or usage
        choices = doc.get("choices")
        # `choices` is legitimately EMPTY on the final chunk that `stream_options.include_usage`
        # asks for, so this must never index blindly.
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            continue
        saw_choice = True
        choice = choices[0]
        delta = choice.get("delta")
        if isinstance(delta, dict) and isinstance(delta.get("content"), str):
            text.append(delta["content"])
        if isinstance(choice.get("finish_reason"), str):
            finish_reason = choice["finish_reason"]
    # The UNION of the two completion markers, because servers disagree about which they send:
    # llama.cpp has shipped without `[DONE]`, and some gateways swallow the finish_reason on the
    # last chunk. A connection severed mid-answer produces NEITHER, which is the case that has to
    # fail — otherwise a network fault arrives at the validators as a truncated document and is
    # blamed on the model, spending repair turns on it.
    # An error frame WINS over a terminator that follows it, exactly as the Messages API reader
    # lets its `error` event beat a `message_stop`.
    if error_detail:
        return "", "", {}, False, (
            f"{_STREAM_INTERRUPTED}: provider error event {error_detail}".strip())
    if not (saw_done or finish_reason is not None):
        return "", "", {}, False, (
            f"{_STREAM_INTERRUPTED}: ended after {len(frames)} frames with no [DONE] and no "
            f"finish_reason")
    if not saw_choice:
        # The buffered reader's `response_missing_choices`, and the mirror has to be exact: a
        # well-terminated stream of nothing but the `include_usage` chunk (`choices: []`) and
        # `[DONE]` would otherwise be a SUCCESS with empty text, spending a bundle-repair turn on
        # an answer the provider never gave. An empty `content` inside a real choice stays legal,
        # exactly as it is in the buffered path.
        return "", "", {}, False, "response_missing_choices"
    return "".join(text), model, usage, finish_reason == "length", None


def _read_anthropic_stream(
        frames: "Sequence[tuple[str, str]]") -> "tuple[str, str, dict, bool, str | None]":
    """Fold a Messages-API event stream into the same 5-tuple the buffered reader returns.

    Takes only `text_delta`, mirroring the buffered reader's `type == "text"` block filter:
    `thinking_delta`, `signature_delta` and `input_json_delta` are not the answer document."""
    text: list[str] = []
    model = ""
    usage: dict[str, Any] = {}
    truncated = False
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
                # `message_start` carries the whole input side — the uncached input AND the
                # two cache classes — so take all of it, not just `input_tokens`. Only
                # `output_tokens` arrives later, on `message_delta`.
                usage.update(_anthropic_usage(message.get("usage")))
        elif event == "content_block_delta":
            delta = doc.get("delta")
            if (isinstance(delta, dict) and delta.get("type") == "text_delta"
                    and isinstance(delta.get("text"), str)):
                text.append(delta["text"])
        elif event == "message_delta":
            delta = doc.get("delta")
            if isinstance(delta, dict) and delta.get("stop_reason") == "max_tokens":
                truncated = True
            reported = doc.get("usage")
            if isinstance(reported, dict) and isinstance(reported.get("output_tokens"), int):
                # Cumulative, so the last one wins.
                usage["output_tokens"] = reported["output_tokens"]
        elif event == "message_stop":
            complete = True
        elif event == "error":
            detail = doc.get("error")
            if isinstance(detail, dict):
                error_detail = f"{detail.get('type', 'error')}: {detail.get('message', '')}"
            else:
                error_detail = str(detail)
    # An `error` event WINS over a `message_stop`, because this API really does answer 200 and
    # then fail mid-stream (`overloaded_error` is the common one), and the detail is what lets
    # the conductor rank it above a bare transport flake.
    if error_detail:
        return "", "", {}, False, (
            f"{_STREAM_INTERRUPTED}: provider error event {error_detail}".strip())
    if not complete:
        return "", "", {}, False, (
            f"{_STREAM_INTERRUPTED}: ended after {len(frames)} frames with no message_stop")
    if not text:
        return "", "", {}, False, "response_has_no_text_block"
    return "".join(text), model, usage, truncated, None


class _Shape(NamedTuple):
    """One provider's wire dialect: how to ask, and how to read either kind of answer.

    Named rather than a bare tuple because three callables is where positional unpacking stops
    being readable. `build_request` takes a `stream` keyword instead of there being a fourth,
    streaming-only builder: the two requests differ by two keys, and a separate builder would
    duplicate the URL, the headers and the `_api_key` failure path to express that."""

    build_request: Callable[..., Any]
    read_response: Callable[..., Any]
    read_stream: Callable[..., Any]


_SHAPES: Mapping[str, _Shape] = {
    "openai_compatible": _Shape(
        _openai_request, _read_openai_response, _read_openai_stream),
    "anthropic_api": _Shape(
        _anthropic_request, _read_anthropic_response, _read_anthropic_stream),
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
    limit = int(max_output_tokens or entry.max_output_tokens or DEFAULT_MAX_OUTPUT_TOKENS)
    timeout = float(timeout_s or entry.timeout_s or DEFAULT_HTTP_TIMEOUT_SECONDS)

    # `getattr`, not `entry.stream`: `entry` is typed `Any` and callers hand this hand-built
    # objects, the same defensive read `effort` already gets. Defaulting to True keeps the
    # DEFAULT with the fix — an object that has never heard of the field still streams.
    stream = bool(getattr(entry, "stream", True))
    url, payload, headers, error = shape.build_request(
        entry, messages, limit, env, stream=stream)
    if error is not None:
        return HttpLeafResponse("", "", None, False, error, "")
    # `secret` is the key just resolved for this request: everything this call returns —
    # the raw body (persisted under `launches/`) and the transport error (emitted as an event)
    # — is provider-supplied text that may echo it back.
    secret, _ = _api_key(entry, env)
    if stream:
        frames, raw, error = _post_stream(url, payload, headers, timeout_s=timeout,
                                          secret=secret, env=env, opener=opener)
        if frames is None:
            return HttpLeafResponse("", "", None, False, error or "empty_response", raw)
        text, model, usage, truncated, read_error = shape.read_stream(frames)
        if read_error is None:
            # The provider's own terminator is on the wire, so the ANSWER is complete and
            # whatever went wrong went wrong after it: a teardown severed before the final
            # zero-length chunk, or a keepalive trailer that outlived the deadline. Both were
            # reproduced. Reporting those as a transport failure would throw away a finished
            # generation — for this workload, ten billed minutes of it — and re-launch for an
            # answer already in hand.
            error = None
        if error is not None:
            # Died mid-answer. Report the TRANSPORT message rather than the reader's "no
            # terminator" summary of it: the former names what actually happened to the socket.
            return HttpLeafResponse("", "", None, False, error, raw)
    else:
        doc, raw, error = _post_json(url, payload, headers, timeout_s=timeout,
                                     secret=secret, env=env, opener=opener)
        if error is not None or doc is None:
            return HttpLeafResponse("", "", None, False, error or "empty_response", raw)
        text, model, usage, truncated, read_error = shape.read_response(doc)
    if read_error is not None:
        # REDACTED, like every other provider-supplied string this module returns. A reader's
        # error used to be a constant, so this was safe by construction; it stopped being one
        # when the stream readers began quoting the provider's own `error` frame, and
        # "Incorrect API key provided: sk-..." is exactly the message class a provider puts
        # there. The caller emits this string as an event and stores it in the leaf's stderr,
        # both persisted — which is the whole reason the key discipline lives on this side.
        return HttpLeafResponse("", "", None, False, _redact(read_error, secret), raw)
    # `model or entry.model`: the response's own value is the provenance ground truth (a
    # gateway may resolve an alias), and the configured one is the honest fallback.
    #
    # Redacted, unlike `text`: this value is only ever PERSISTED (the `agent_runs` row, the
    # per-attempt metadata) — nothing parses it — so removing a credential from it costs
    # nothing but a mangled model name in the case where the key is a substring of one, which
    # is the trade the answer channel could not make.
    return HttpLeafResponse(text, _redact(model, secret) or entry.model,
                            _normalized_usage(usage), truncated, None, raw)


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
