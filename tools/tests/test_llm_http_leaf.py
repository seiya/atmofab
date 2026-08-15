"""Unit tests for the HTTP pure-leaf transport (`tools/llm_http_leaf.py`, issue #28).

The module has no filesystem side effects and spawns nothing, so a fake `opener` is the whole
test seam: it captures the request that would have gone out and returns the body the provider
would have sent back. That makes the two things worth pinning testable exactly —

1. **the request shape**, per provider, because a wrong header or a renamed field is a
   run-time-only failure against a real endpoint; and
2. **the error taxonomy**, because every one of these has to come back as `transport_error`
   rather than as an exception or, worse, as an empty-but-successful answer that the validators
   would then blame the model for.

Plus the one property that is a security claim rather than a behavior: the API key's VALUE
never appears in anything this module returns.
"""

from __future__ import annotations

import io
import json
import pathlib
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import pytest

from tools import llm_config as lc
from tools import llm_http_leaf as hl
from tools import workflow_conductor as wc
from tools.pure_leaf import PURE_SYSTEM_PROMPT

KEY_ENV = "METDSL_TEST_HTTP_KEY"
KEY_VALUE = "sk-test-do-not-log-me"


def _entry(provider: str = "openai_compatible", **kw) -> lc.ResolvedLeafEntry:
    """A leaf entry for a test, NON-streaming unless the test asks otherwise.

    The product default is `stream=True` (see `ResolvedLeafEntry.stream`); this helper inverts it
    so the buffered path keeps its own coverage instead of every buffered test quietly becoming a
    streaming one. `test_streaming_is_on_by_default_for_a_configured_entry` builds an entry
    through the CONFIG LOADER rather than this helper, so the inversion here cannot become the
    product default by accident."""
    base = dict(
        provider=provider,
        model="test-model",
        base_url=("http://localhost:8000/v1" if provider == "openai_compatible"
                  else lc.ANTHROPIC_DEFAULT_BASE_URL),
        api_key_env=KEY_ENV,
        capabilities=lc.PROVIDER_CAPABILITIES[provider],
        stream=False,
    )
    base.update(kw)
    return lc.ResolvedLeafEntry(**base)


def _sse(*frames: "tuple[str, str]", terminator: str = "") -> str:
    """Render `(event, data)` pairs as event-stream wire text.

    `event` empty renders a bare `data:` frame — the OpenAI dialect. `terminator` appends a
    final raw data line (`[DONE]`) without an event name."""
    out = []
    for event, data in frames:
        block = f"event: {event}\n" if event else ""
        out.append(f"{block}data: {data}\n\n")
    if terminator:
        out.append(f"data: {terminator}\n\n")
    return "".join(out)


def _openai_chunk(content: str | None = None, *, finish_reason: str | None = None,
                  usage: dict | None = None, model: str = "test-model-resolved") -> str:
    choice: dict = {"index": 0, "delta": {}}
    if content is not None:
        choice["delta"]["content"] = content
    choice["finish_reason"] = finish_reason
    doc: dict = {"model": model, "choices": [choice]}
    if usage is not None:
        # The `include_usage` chunk carries NO choices at all.
        doc = {"model": model, "choices": [], "usage": usage}
    return json.dumps(doc)


class _ChunkedResponse:
    """A response that hands back one slice per `read1`, like a real socket.

    Slice boundaries are the caller's choice precisely so a test can cut a frame in half."""

    def __init__(self, slices: "list[bytes]") -> None:
        self._slices = list(slices)

    def read1(self, _size: int = -1) -> bytes:
        return self._slices.pop(0) if self._slices else b""

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _stream_opener(body: str, captured: list | None = None, *, slices: int = 1,
                   chunk_bytes: int | None = None):
    """An opener answering with `body` as an event stream, cut into `slices` receives.

    `chunk_bytes` sets the receive size directly instead, for a test that needs the receives
    smaller than some bound — `_iter_bounded` discards the whole receive that crosses the size
    ceiling, so a coarse split can refuse a stream before any of it is parsed."""
    def _open(request, timeout=None):        # noqa: ANN001 - test double
        if captured is not None:
            captured.append({
                "url": request.full_url,
                "headers": {k.lower(): v for k, v in request.header_items()},
                "body": json.loads(request.data.decode("utf-8")),
                "timeout": timeout,
            })
        raw = body.encode("utf-8")
        if chunk_bytes is not None:
            step = max(1, chunk_bytes)
        elif slices <= 1:
            return _ChunkedResponse([raw])
        else:
            step = max(1, len(raw) // slices + 1)
        return _ChunkedResponse([raw[i:i + step] for i in range(0, len(raw), step)])
    return _open


class _FakeResponse(io.BytesIO):
    """A minimal stand-in for what `urlopen` yields: a context manager that `.read()`s bytes."""

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False


def _opener(body: dict | str, captured: list | None = None):
    def _open(request, timeout=None):        # noqa: ANN001 - test double
        if captured is not None:
            captured.append({
                "url": request.full_url,
                "method": request.get_method(),
                "headers": {k.lower(): v for k, v in request.header_items()},
                "body": json.loads(request.data.decode("utf-8")),
                "timeout": timeout,
            })
        raw = body if isinstance(body, str) else json.dumps(body)
        return _FakeResponse(raw.encode("utf-8"))
    return _open


_OPENAI_OK = {
    "model": "test-model-resolved",
    "choices": [{"message": {"role": "assistant", "content": '{"ok": true}'},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 11, "completion_tokens": 22},
}

_ANTHROPIC_OK = {
    "model": "claude-opus-5-resolved",
    "content": [{"type": "text", "text": '{"ok": true}'}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 33, "output_tokens": 44},
}


class RequestShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.dict("os.environ", {KEY_ENV: KEY_VALUE}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_openai_request_shape(self) -> None:
        seen: list = []
        hl.run_pure_http_leaf(
            _entry(), [{"role": "user", "content": "PROMPT"}],
            max_output_tokens=4096, opener=_opener(_OPENAI_OK, seen))
        req = seen[0]
        self.assertEqual(req["url"], "http://localhost:8000/v1/chat/completions")
        self.assertEqual(req["method"], "POST")
        self.assertEqual(req["headers"]["authorization"], f"Bearer {KEY_VALUE}")
        self.assertEqual(req["headers"]["content-type"], "application/json")
        self.assertEqual(req["body"]["model"], "test-model")
        self.assertEqual(req["body"]["max_tokens"], 4096)
        self.assertEqual(req["body"]["messages"][0],
                         {"role": "system", "content": PURE_SYSTEM_PROMPT})
        self.assertEqual(req["body"]["messages"][1], {"role": "user", "content": "PROMPT"})

    def test_anthropic_request_shape(self) -> None:
        seen: list = []
        hl.run_pure_http_leaf(
            _entry("anthropic_api"), [{"role": "user", "content": "PROMPT"}],
            max_output_tokens=8192, opener=_opener(_ANTHROPIC_OK, seen))
        req = seen[0]
        self.assertEqual(req["url"], f"{lc.ANTHROPIC_DEFAULT_BASE_URL}/v1/messages")
        self.assertEqual(req["headers"]["x-api-key"], KEY_VALUE)
        self.assertEqual(req["headers"]["anthropic-version"], hl.ANTHROPIC_VERSION)
        # The system channel is pinned by the transport, not by the caller — the same fixed
        # prompt the CLI pure leaf gets via `--system-prompt`.
        self.assertEqual(req["body"]["system"], PURE_SYSTEM_PROMPT)
        self.assertEqual(req["body"]["messages"], [{"role": "user", "content": "PROMPT"}])
        self.assertEqual(req["body"]["max_tokens"], 8192)

    def test_a_configured_effort_reaches_the_request_body(self) -> None:
        seen: list = []
        hl.run_pure_http_leaf(
            _entry(effort="high"), [{"role": "user", "content": "P"}],
            opener=_opener(_OPENAI_OK, seen))
        self.assertEqual(seen[0]["body"]["reasoning_effort"], "high")

    def test_an_absent_effort_is_not_put_on_the_wire(self) -> None:
        """Some servers reject a field they do not implement, so "no level" must mean the key
        is absent rather than empty."""
        seen: list = []
        hl.run_pure_http_leaf(
            _entry(), [{"role": "user", "content": "P"}], opener=_opener(_OPENAI_OK, seen))
        self.assertNotIn("reasoning_effort", seen[0]["body"])

    def test_a_repair_turn_sends_the_prior_conversation(self) -> None:
        seen: list = []
        history = [
            {"role": "user", "content": "FIRST"},
            {"role": "assistant", "content": "BAD DOCUMENT"},
            {"role": "user", "content": "REPAIR: it was malformed"},
        ]
        hl.run_pure_http_leaf(_entry(), history, opener=_opener(_OPENAI_OK, seen))
        self.assertEqual(seen[0]["body"]["messages"][1:], history)

    def test_the_entrys_own_limits_are_used_when_the_caller_gives_none(self) -> None:
        seen: list = []
        hl.run_pure_http_leaf(
            _entry(max_output_tokens=777, timeout_s=12.5),
            [{"role": "user", "content": "P"}], opener=_opener(_OPENAI_OK, seen))
        self.assertEqual(seen[0]["body"]["max_tokens"], 777)
        self.assertEqual(seen[0]["timeout"], 12.5)

    def test_base_url_trailing_slash_does_not_double(self) -> None:
        seen: list = []
        hl.run_pure_http_leaf(
            _entry(base_url="http://localhost:8000/v1/"),
            [{"role": "user", "content": "P"}], opener=_opener(_OPENAI_OK, seen))
        self.assertEqual(seen[0]["url"], "http://localhost:8000/v1/chat/completions")


class ResponseReadingTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.dict("os.environ", {KEY_ENV: KEY_VALUE}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_openai_response_is_read_and_usage_normalized(self) -> None:
        out = hl.run_pure_http_leaf(
            _entry(), [{"role": "user", "content": "P"}], opener=_opener(_OPENAI_OK))
        self.assertIsNone(out.transport_error)
        self.assertEqual(out.text, '{"ok": true}')
        self.assertEqual(out.model, "test-model-resolved")
        self.assertEqual(out.usage, {"input_tokens": 11, "output_tokens": 22,
                                     "total_tokens": 33, "usage_source": "http_provider"})
        self.assertFalse(out.truncated)

    def test_anthropic_response_is_read_and_usage_normalized(self) -> None:
        out = hl.run_pure_http_leaf(
            _entry("anthropic_api"), [{"role": "user", "content": "P"}],
            opener=_opener(_ANTHROPIC_OK))
        self.assertIsNone(out.transport_error)
        self.assertEqual(out.text, '{"ok": true}')
        self.assertEqual(out.model, "claude-opus-5-resolved")
        self.assertEqual(out.usage, {"input_tokens": 33, "output_tokens": 44,
                                     "total_tokens": 77, "usage_source": "http_provider"})

    def test_the_openai_reasoning_and_cache_splits_survive(self) -> None:
        """The reality this transport was blind to. On `orch_20260807T002410Z_acf2b996` the
        `verify` leaf reported 23,538 completion tokens of which 23,438 (99.6%) were reasoning —
        the answer was ~100 tokens — and two otherwise identical `generate` calls reported 64 vs
        32,832 cached prompt tokens. Reading `output_tokens` alone is reading a number that is
        mostly reasoning without knowing it, which is how `max_output_tokens` gets sized wrong."""
        body = dict(_OPENAI_OK, usage={
            "prompt_tokens": 33_000, "completion_tokens": 23_538,
            "completion_tokens_details": {"reasoning_tokens": 23_438},
            "prompt_tokens_details": {"cached_tokens": 32_832}})
        out = hl.run_pure_http_leaf(
            _entry(), [{"role": "user", "content": "P"}], opener=_opener(body))
        self.assertEqual(out.usage["reasoning_tokens"], 23_438)
        self.assertEqual(out.usage["cached_tokens"], 32_832)
        # SUBSETS of completion / prompt — counting them again would double the bill.
        self.assertEqual(out.usage["total_tokens"], 33_000 + 23_538)
        # ...and the provider's own detail objects travel too, so a count not modelled here is
        # still on disk instead of recoverable only from the multi-MB raw SSE capture.
        self.assertEqual(out.usage["provider_details"], {
            "completion_tokens_details": {"reasoning_tokens": 23_438},
            "prompt_tokens_details": {"cached_tokens": 32_832}})

    def test_a_string_valued_detail_field_is_dropped_before_it_can_be_persisted(self) -> None:
        """`provider_details` is persisted (the agent_runs row, the per-attempt metadata)
        WITHOUT passing through `_redact` — unlike every other provider-supplied string this
        module returns. So only COUNTS may travel: a provider that echoes the API key into a
        detail object it controls must not put it on disk."""
        body = dict(_OPENAI_OK, usage={
            "prompt_tokens": 11, "completion_tokens": 22,
            "completion_tokens_details": {"reasoning_tokens": 4, "note": "key=" + KEY_VALUE},
            "prompt_tokens_details": {"cached_tokens": 2}})
        out = hl.run_pure_http_leaf(
            _entry(), [{"role": "user", "content": "P"}], opener=_opener(body))
        self.assertEqual(out.usage["provider_details"], {
            "completion_tokens_details": {"reasoning_tokens": 4},
            "prompt_tokens_details": {"cached_tokens": 2}})
        self.assertNotIn(KEY_VALUE, json.dumps(out.usage))

    def test_a_provider_that_sends_no_detail_objects_stays_clean(self) -> None:
        """Most `openai_compatible` endpoints (vLLM, llama.cpp, Ollama) send neither object.
        Their rows must carry the plain pair, not keys with `None` in them."""
        out = hl.run_pure_http_leaf(
            _entry(), [{"role": "user", "content": "P"}], opener=_opener(_OPENAI_OK))
        self.assertEqual(out.usage, {"input_tokens": 11, "output_tokens": 22,
                                     "total_tokens": 33, "usage_source": "http_provider"})

    def test_the_anthropic_cache_classes_survive(self) -> None:
        """`cache_read_input_tokens` / `cache_creation_input_tokens` are ADDITIONAL prompt
        classes, not subsets of `input_tokens` — dropping them lost real billed input, and with
        it any view of whether the prompt cache was hitting. They count toward the total."""
        body = dict(_ANTHROPIC_OK, usage={
            "input_tokens": 33, "output_tokens": 44,
            "cache_read_input_tokens": 14_278, "cache_creation_input_tokens": 5_849})
        out = hl.run_pure_http_leaf(
            _entry("anthropic_api"), [{"role": "user", "content": "P"}], opener=_opener(body))
        self.assertEqual(out.usage["cache_read_input_tokens"], 14_278)
        self.assertEqual(out.usage["cache_creation_input_tokens"], 5_849)
        self.assertEqual(out.usage["total_tokens"], 33 + 44 + 14_278 + 5_849)

    def test_anthropic_text_blocks_are_concatenated_not_sampled(self) -> None:
        body = dict(_ANTHROPIC_OK, content=[
            {"type": "text", "text": '{"a": 1,'},
            {"type": "thinking", "thinking": "ignored"},
            {"type": "text", "text": ' "b": 2}'},
        ])
        out = hl.run_pure_http_leaf(
            _entry("anthropic_api"), [{"role": "user", "content": "P"}], opener=_opener(body))
        self.assertEqual(out.text, '{"a": 1, "b": 2}')

    def test_openai_truncation_is_reported_by_the_provider(self) -> None:
        body = dict(_OPENAI_OK, choices=[
            {"message": {"content": '{"partial": '}, "finish_reason": "length"}])
        out = hl.run_pure_http_leaf(
            _entry(), [{"role": "user", "content": "P"}], opener=_opener(body))
        self.assertTrue(out.truncated)
        self.assertIsNone(out.transport_error)   # truncation is a CONTENT outcome, repairable

    def test_anthropic_truncation_is_reported_by_the_provider(self) -> None:
        body = dict(_ANTHROPIC_OK, stop_reason="max_tokens")
        out = hl.run_pure_http_leaf(
            _entry("anthropic_api"), [{"role": "user", "content": "P"}], opener=_opener(body))
        self.assertTrue(out.truncated)

    def test_the_configured_model_is_the_fallback_when_the_response_names_none(self) -> None:
        body = {k: v for k, v in _OPENAI_OK.items() if k != "model"}
        out = hl.run_pure_http_leaf(
            _entry(), [{"role": "user", "content": "P"}], opener=_opener(body))
        self.assertEqual(out.model, "test-model")

    def test_the_raw_body_is_returned_for_persistence(self) -> None:
        out = hl.run_pure_http_leaf(
            _entry(), [{"role": "user", "content": "P"}], opener=_opener(_OPENAI_OK))
        self.assertEqual(json.loads(out.raw_response), _OPENAI_OK)


class ErrorTaxonomyTests(unittest.TestCase):
    """Every failure is a `transport_error`, never an exception: the caller's job is to turn it
    into a substep outcome, and an exception escaping here would take the run down instead."""

    def setUp(self) -> None:
        patcher = patch.dict("os.environ", {KEY_ENV: KEY_VALUE}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _error(self, opener, provider: str = "openai_compatible") -> str:
        out = hl.run_pure_http_leaf(
            _entry(provider), [{"role": "user", "content": "P"}], opener=opener)
        self.assertIsNotNone(out.transport_error)
        self.assertEqual(out.text, "")
        self.assertFalse(out.truncated)
        return str(out.transport_error)

    def test_connection_failure(self) -> None:
        def _refuse(*_a, **_k):
            raise OSError("Connection refused")
        self.assertIn("OSError", self._error(_refuse))

    def test_timeout(self) -> None:
        def _slow(*_a, **_k):
            raise TimeoutError("timed out")
        self.assertIn("TimeoutError", self._error(_slow))

    def test_http_error_status_is_a_transport_error_here(self) -> None:
        """Unlike preflight's reachability probe: there the question is "is anything there",
        here it is "did the model answer" — and a 429 means it did not."""
        def _rate_limited(*_a, **_k):
            raise urllib.error.HTTPError(
                "http://x", 429, "Too Many Requests", {},
                io.BytesIO(b'{"error": "slow down"}'))
        message = self._error(_rate_limited)
        self.assertIn("HTTP 429", message)
        # SPACED, because the conductor classifies a leaf's terminal line with patterns
        # anchored on `\bhttp\b`: `http_status_429` is a single word to a regex, so a terse
        # rate-limit body would match nothing and fail the run closed instead of retrying.
        self.assertEqual(wc._leaf_infra_error(wc.ProcResult(1, "", message))[0],
                         "llm_rate_limit")

    def test_server_error_status(self) -> None:
        def _boom(*_a, **_k):
            raise urllib.error.HTTPError("http://x", 503, "Unavailable", {}, io.BytesIO(b""))
        message = self._error(_boom)
        self.assertIn("HTTP 503", message)
        self.assertEqual(wc._leaf_infra_error(wc.ProcResult(1, "", message))[0],
                         "llm_transport_flake")

    def test_non_json_body(self) -> None:
        self.assertIn("response_not_json", self._error(_opener("<html>nope</html>")))

    def test_non_object_body(self) -> None:
        self.assertIn("response_not_an_object", self._error(_opener("[1, 2, 3]")))

    def test_openai_body_missing_choices(self) -> None:
        self.assertIn("response_missing_choices", self._error(_opener({"model": "m"})))

    def test_openai_body_missing_message_content(self) -> None:
        self.assertIn("response_missing_message_content",
                      self._error(_opener({"choices": [{"message": {}}]})))

    def test_anthropic_body_missing_content(self) -> None:
        self.assertIn("response_missing_content",
                      self._error(_opener({"model": "m"}), "anthropic_api"))

    def test_anthropic_body_with_no_text_block(self) -> None:
        self.assertIn("response_has_no_text_block",
                      self._error(_opener({"content": [{"type": "thinking"}]}), "anthropic_api"))

    def test_missing_api_key_is_caught_before_the_request_is_sent(self) -> None:
        def _must_not_be_called(*_a, **_k):
            raise AssertionError("a request was sent without a key")
        env = {k: v for k, v in __import__("os").environ.items() if k != KEY_ENV}
        with patch.dict("os.environ", env, clear=True):
            out = hl.run_pure_http_leaf(
                _entry(), [{"role": "user", "content": "P"}], opener=_must_not_be_called)
        self.assertIn("missing_api_key", str(out.transport_error))
        self.assertIn(KEY_ENV, str(out.transport_error))

    def test_the_supplied_environment_is_where_the_key_is_read(self) -> None:
        """A run's own credential lives in the conductor's environment, which every spawned
        leaf receives. Reading the process-global one takes a key the run did not choose."""
        seen: list = []
        env = {KEY_ENV: "sk-from-the-run"}
        with patch.dict("os.environ", {KEY_ENV: "sk-from-the-process"}, clear=False):
            hl.run_pure_http_leaf(
                _entry(), [{"role": "user", "content": "P"}], env=env,
                opener=_opener(_OPENAI_OK, seen))
        self.assertEqual(seen[0]["headers"]["authorization"], "Bearer sk-from-the-run")

    def test_a_key_absent_from_the_supplied_environment_is_missing(self) -> None:
        with patch.dict("os.environ", {KEY_ENV: "sk-from-the-process"}, clear=False):
            out = hl.run_pure_http_leaf(
                _entry(), [{"role": "user", "content": "P"}], env={},
                opener=_opener(_OPENAI_OK))
        self.assertIn("missing_api_key", str(out.transport_error))

    def test_the_supplied_environments_proxy_is_the_one_installed(self) -> None:
        opener = hl._default_opener({"https_proxy": "http://proxy.example:3128"})
        owner = getattr(opener, "__self__", None)
        proxies = [h for h in getattr(owner, "handlers", [])
                   if isinstance(h, urllib.request.ProxyHandler)]
        self.assertTrue(proxies)
        self.assertIn("https", proxies[0].proxies)

    def _routed_host(self, handler, url: str) -> str:
        """The host the request would actually be sent to: `set_proxy` rewrites `req.host` to
        the proxy, so this distinguishes "proxied" from "direct"."""
        request = urllib.request.Request(url)

        class _Parent:
            def open(self, *_a, **_k):
                return None

        handler.parent = _Parent()
        handler.proxy_open(request, "http://proxy.example:3128", "http")
        return request.host

    def test_the_supplied_environments_no_proxy_is_honoured(self) -> None:
        """`ProxyHandler` takes its proxy URLs from the mapping it is given but decides
        BYPASS by reading the process-global environment. A run whose environment sets
        `NO_PROXY=localhost` alongside a proxy would send a loopback request — and its
        Authorization header — to that proxy, because the global environment never mentioned
        the exemption."""
        env = {"HTTP_PROXY": "http://proxy.example:3128", "NO_PROXY": "localhost,127.0.0.1"}
        with patch.dict("os.environ", {}, clear=True):
            handler = hl._EnvProxyHandler(hl._env_proxies(env))
            self.assertEqual(self._routed_host(handler, "http://127.0.0.1:8000/v1"),
                             "127.0.0.1:8000")
            # ...and a host the exemption does not name still goes through the proxy.
            self.assertEqual(self._routed_host(handler, "http://api.example.com/v1"),
                             "proxy.example:3128")
            # The stock handler is what this replaces: it proxies the loopback request.
            self.assertEqual(
                self._routed_host(urllib.request.ProxyHandler(hl._env_proxies(env)),
                                  "http://127.0.0.1:8000/v1"),
                "proxy.example:3128")

    def test_the_proxy_map_keeps_no_proxy_under_the_key_urllib_reads(self) -> None:
        self.assertEqual(
            hl._env_proxies({"HTTPS_PROXY": "http://p:1", "no_proxy": "localhost"}),
            {"https": "http://p:1", "no": "localhost"})

    def test_an_unsupported_provider_is_a_transport_error_not_a_crash(self) -> None:
        entry = lc.ResolvedLeafEntry(provider="claude_cli", model="opus")
        out = hl.run_pure_http_leaf(entry, [{"role": "user", "content": "P"}])
        self.assertIn("unsupported_http_provider", str(out.transport_error))


class KeySecrecyTests(unittest.TestCase):
    """The config names the environment VARIABLE; the value must not travel any further than
    the request header. The raw response body IS persisted by the caller, so this is the layer
    where that has to hold."""

    def test_the_key_value_appears_in_no_returned_field(self) -> None:
        with patch.dict("os.environ", {KEY_ENV: KEY_VALUE}, clear=False):
            for provider, body in (("openai_compatible", _OPENAI_OK),
                                   ("anthropic_api", _ANTHROPIC_OK)):
                out = hl.run_pure_http_leaf(
                    _entry(provider), [{"role": "user", "content": "P"}], opener=_opener(body))
                self.assertNotIn(KEY_VALUE, json.dumps(list(out)), msg=provider)

    def test_a_provider_that_echoes_the_key_back_does_not_get_it_persisted(self) -> None:
        """The contract has to survive text this module did not write. A debug gateway or a
        verbose proxy can echo the request headers in an error body — which is BOTH persisted
        verbatim under `launches/` and emitted in an event."""
        def _echoing_401(*_a, **_k):
            body = ('{"error":"invalid key","request_headers":'
                    '{"Authorization":"Bearer %s"}}' % KEY_VALUE).encode("utf-8")
            raise urllib.error.HTTPError("http://x", 401, "Unauthorized", {}, io.BytesIO(body))

        with patch.dict("os.environ", {KEY_ENV: KEY_VALUE}, clear=False):
            out = hl.run_pure_http_leaf(
                _entry(), [{"role": "user", "content": "P"}], opener=_echoing_401)
        self.assertNotIn(KEY_VALUE, json.dumps(list(out)))
        self.assertIn("[redacted-api-key]", out.raw_response)
        self.assertIn("HTTP 401", str(out.transport_error))

    def test_the_persisted_copy_of_a_successful_body_is_redacted_too(self) -> None:
        """The success path persists the raw body as well, so the redaction cannot be scoped to
        errors. It is scoped to the DIAGNOSTIC copy: `text` is the parsed document the run
        acts on, and rewriting it would corrupt a valid answer whenever the key is a common
        substring — the documented residual is that a provider echoing the key into a
        successful completion's CONTENT puts it in the model's answer channel."""
        body = dict(_OPENAI_OK, choices=[
            {"message": {"content": '{"leaked": "%s"}' % KEY_VALUE}, "finish_reason": "stop"}])
        with patch.dict("os.environ", {KEY_ENV: KEY_VALUE}, clear=False):
            out = hl.run_pure_http_leaf(
                _entry(), [{"role": "user", "content": "P"}], opener=_opener(body))
        self.assertNotIn(KEY_VALUE, out.raw_response)
        self.assertIn("[redacted-api-key]", out.raw_response)

    def test_redaction_never_touches_the_document_the_run_reads(self) -> None:
        """Local endpoints are configured with placeholder keys — `EMPTY`, `test`, `local`.
        Redacting the body BEFORE parsing rewrites the provider's own document, and a valid
        reply becomes unparseable. The ANSWER is therefore left exactly as sent.

        `model` is redacted even so, and the difference is the point: nothing parses it, it is
        only ever persisted, so a mangled model name under a substring-y key is a smaller cost
        than a credential in `agent_runs.jsonl`. The answer channel cannot make that trade —
        hence the split, and hence its own redaction happens on the copy written to disk."""
        body = {"model": "test-model",
                "choices": [{"message": {"content": '{"ok": 1}'}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2}}
        with patch.dict("os.environ", {KEY_ENV: "test"}, clear=False):
            out = hl.run_pure_http_leaf(
                _entry(), [{"role": "user", "content": "P"}], opener=_opener(body))
        self.assertIsNone(out.transport_error)
        self.assertEqual(out.text, '{"ok": 1}')            # the document is untouched
        self.assertEqual(out.model, "[redacted-api-key]-model")

    def test_a_key_straddling_the_error_truncation_leaves_no_prefix(self) -> None:
        """Slicing to the diagnostic length first cuts through the middle of the key, and the
        exact-string replace then matches nothing while a prefix of the secret survives."""
        secret = "sk-" + "S" * 40
        env = {KEY_ENV: secret}

        def _straddle(*_a, **_k):
            raise urllib.error.HTTPError(
                "http://x", 401, "Unauthorized", {},
                io.BytesIO(("x" * 380 + secret).encode("utf-8")))

        with patch.dict("os.environ", env, clear=False):
            out = hl.run_pure_http_leaf(
                _entry(), [{"role": "user", "content": "P"}], opener=_straddle)
        for length in range(8, len(secret) + 1):
            self.assertNotIn(secret[:length], out.raw_response, msg=f"prefix of {length}")
            self.assertNotIn(secret[:length], str(out.transport_error))

    def test_a_key_echoed_in_the_model_field_is_redacted(self) -> None:
        """`model` is a provider-controlled METADATA channel that is persisted (the
        `agent_runs` row, the per-attempt metadata) and parsed by nothing — so unlike the
        answer, redacting it costs only a mangled model name in the case where the key is a
        substring of one."""
        body = dict(_OPENAI_OK, model=f"gpt-{KEY_VALUE}")
        with patch.dict("os.environ", {KEY_ENV: KEY_VALUE}, clear=False):
            out = hl.run_pure_http_leaf(
                _entry(), [{"role": "user", "content": "P"}], opener=_opener(body))
        self.assertNotIn(KEY_VALUE, out.model)
        self.assertIn("[redacted-api-key]", out.model)
        self.assertEqual(out.text, '{"ok": true}')      # the answer is untouched

    def test_a_key_in_the_http_reason_phrase_is_redacted(self) -> None:
        """The reason phrase is provider-controlled too, and it is what the message falls back
        to when the body was empty or unreadable."""
        def _reason_only(*_a, **_k):
            raise urllib.error.HTTPError(
                "http://x", 401, f"bad key {KEY_VALUE}", {}, io.BytesIO(b""))

        with patch.dict("os.environ", {KEY_ENV: KEY_VALUE}, clear=False):
            out = hl.run_pure_http_leaf(
                _entry(), [{"role": "user", "content": "P"}], opener=_reason_only)
        self.assertNotIn(KEY_VALUE, str(out.transport_error))
        self.assertIn("HTTP 401", str(out.transport_error))

    def test_an_exception_string_carrying_the_key_is_redacted(self) -> None:
        """An operator can embed a credential in `base_url`; a socket error's text repeats it."""
        def _boom(*_a, **_k):
            raise OSError(f"cannot connect to http://user:{KEY_VALUE}@host/v1")

        with patch.dict("os.environ", {KEY_ENV: KEY_VALUE}, clear=False):
            out = hl.run_pure_http_leaf(
                _entry(), [{"role": "user", "content": "P"}], opener=_boom)
        self.assertNotIn(KEY_VALUE, str(out.transport_error))

    def test_an_error_naming_the_variable_does_not_carry_its_value(self) -> None:
        with patch.dict("os.environ", {KEY_ENV: KEY_VALUE}, clear=False):
            def _refuse(*_a, **_k):
                raise OSError("Connection refused")
            out = hl.run_pure_http_leaf(
                _entry(), [{"role": "user", "content": "P"}], opener=_refuse)
        self.assertNotIn(KEY_VALUE, str(out.transport_error))


class TransportBoundsTests(unittest.TestCase):
    """The two bounds the CLI path gets for free and this one has to supply itself: a
    wall-clock deadline (there is no process to kill and no `leaf_timeout` event), and a
    refusal to follow a redirect (which would hand the operator's key to the new host)."""

    def setUp(self) -> None:
        patcher = patch.dict("os.environ", {KEY_ENV: KEY_VALUE}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_trickling_response_is_cut_off_at_the_deadline(self) -> None:
        """`urlopen(timeout=)` resets on every byte, so an endpoint dribbling below the
        interval never trips it and the conductor waits forever."""

        class _Trickle:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def read1(self, _n=None):
                time.sleep(0.02)
                return b" "          # never EOF

        out = hl.run_pure_http_leaf(
            _entry(), [{"role": "user", "content": "P"}],
            timeout_s=0.1, opener=lambda *_a, **_k: _Trickle())
        self.assertEqual(out.transport_error, "response_deadline_exceeded")
        self.assertEqual(out.text, "")

    def test_the_body_is_read_one_socket_operation_at_a_time(self) -> None:
        """`HTTPResponse.read(n)` loops INTERNALLY until it has n bytes, and every inner
        receive resets the socket timeout — so a deadline checked between `read` calls bounds
        nothing. Measured against a real trickling socket before this: `timeout_s=2` was still
        blocked past 40 s. `read1` returns what one receive produced, which is what makes the
        check between iterations effective.

        The fake below has the two methods behave the way the real class does: `read` does not
        return until it has the whole chunk, `read1` returns immediately with what it has."""

        class _LikeHTTPResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def read(self, n=None):          # the trap: blocks for the FULL chunk
                time.sleep(30)
                return b" " * (n or 0)

            def read1(self, _n=None):        # one receive's worth
                time.sleep(0.02)
                return b" "

        started = time.monotonic()
        out = hl.run_pure_http_leaf(
            _entry(), [{"role": "user", "content": "P"}],
            timeout_s=0.1, opener=lambda *_a, **_k: _LikeHTTPResponse())
        self.assertEqual(out.transport_error, "response_deadline_exceeded")
        self.assertLess(time.monotonic() - started, 5.0,
                        msg="the read was not bounded by the deadline")

    def test_a_response_without_read1_still_works(self) -> None:
        """The fallback: a test double, or any object that only implements `read`."""

        class _PlainRead(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            read1 = None                     # explicitly absent

        body = json.dumps(_OPENAI_OK).encode("utf-8")
        out = hl.run_pure_http_leaf(
            _entry(), [{"role": "user", "content": "P"}],
            opener=lambda *_a, **_k: _PlainRead(body))
        self.assertIsNone(out.transport_error)
        self.assertEqual(out.text, '{"ok": true}')

    def _silent_after_one_byte(self, status_line: bytes) -> str:
        """A real server that sends headers, one byte, and then goes silent. Returns its
        base_url. The socket's own timeout is what must be narrowed for this to end at the
        deadline rather than a full timeout later."""
        import socket
        import threading

        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        self.addCleanup(srv.close)
        stop = threading.Event()
        self.addCleanup(stop.set)

        def _serve():
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with conn:
                conn.recv(65536)
                conn.sendall(status_line + b"\r\nContent-Length: 100000\r\n"
                             b"Content-Type: application/json\r\n\r\n")
                stop.wait(0.4)               # just before a 0.5s deadline
                try:
                    conn.sendall(b" ")
                except OSError:
                    return
                stop.wait(30)
        threading.Thread(target=_serve, daemon=True).start()
        return f"http://127.0.0.1:{srv.getsockname()[1]}/v1"

    @pytest.mark.slow
    def test_an_error_body_that_goes_silent_still_ends_at_the_deadline(self) -> None:
        """The wrapper chain nests differently for an `HTTPError` (it adds a layer), so a
        fixed-path unwrap reached the socket for a success and not for an error — a 503 whose
        body went silent ran for the deadline PLUS a full socket timeout (measured 3.5 s
        against a 2 s bound)."""
        entry = _entry(base_url=self._silent_after_one_byte(b"HTTP/1.1 503 Unavailable"))
        started = time.monotonic()
        out = hl.run_pure_http_leaf(entry, [{"role": "user", "content": "P"}], timeout_s=0.5)
        elapsed = time.monotonic() - started
        self.assertIn("HTTP 503", str(out.transport_error))
        # An un-narrowed socket would wait a full extra timeout from t=0.4 -> ~0.9s. The bound
        # sits midway: correct behaviour has 0.2s of headroom, the defect 0.2s of margin.
        self.assertLess(elapsed, 0.7, msg=f"took {elapsed:.2f}s for a 0.5s deadline")

    @pytest.mark.slow
    def test_a_success_body_that_goes_silent_ends_at_the_deadline(self) -> None:
        entry = _entry(base_url=self._silent_after_one_byte(b"HTTP/1.1 200 OK"))
        started = time.monotonic()
        out = hl.run_pure_http_leaf(entry, [{"role": "user", "content": "P"}], timeout_s=0.5)
        elapsed = time.monotonic() - started
        self.assertEqual(out.transport_error, "response_deadline_exceeded")
        self.assertLess(elapsed, 0.7, msg=f"took {elapsed:.2f}s for a 0.5s deadline")

    @pytest.mark.slow
    def test_the_deadline_bounds_the_TOTAL_not_each_receive(self) -> None:
        """`urlopen(timeout=)` applies to each receive independently, so a server that sends a
        byte just before the deadline and then stalls would buy itself another full timeout —
        up to twice the configured bound. Driven over a real socket, because the defect is in
        how the socket's own timeout is set."""
        import socket
        import threading

        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        self.addCleanup(srv.close)
        stop = threading.Event()
        self.addCleanup(stop.set)

        def _serve():
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with conn:
                conn.recv(65536)
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000000\r\n"
                             b"Content-Type: application/json\r\n\r\n")
                time.sleep(0.4)              # a byte just before a 0.5 s deadline...
                try:
                    conn.sendall(b" ")
                except OSError:
                    return
                stop.wait(30)                # ...then stall
        threading.Thread(target=_serve, daemon=True).start()

        entry = _entry(base_url=f"http://127.0.0.1:{srv.getsockname()[1]}/v1")
        started = time.monotonic()
        out = hl.run_pure_http_leaf(
            entry, [{"role": "user", "content": "P"}], timeout_s=0.5)
        elapsed = time.monotonic() - started
        self.assertEqual(out.transport_error, "response_deadline_exceeded")
        # Comfortably under 2x (=1.0s), which is what an un-narrowed per-receive timeout would
        # give: the byte at t=0.4 buys the socket another full 0.5s, landing at ~0.9s.
        self.assertLess(elapsed, 0.7, msg=f"took {elapsed:.2f}s for a 0.5s deadline")

    def test_an_oversized_response_is_refused(self) -> None:
        class _Flood:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def read(self, n=65536):
                return b"x" * n

        out = hl.run_pure_http_leaf(
            _entry(), [{"role": "user", "content": "P"}],
            opener=lambda *_a, **_k: _Flood())
        self.assertIn("response_too_large", str(out.transport_error))

    def test_an_error_body_is_bounded_by_the_same_deadline(self) -> None:
        """`exc.read()` is an unbounded blocking read, so a gateway that trickles its error
        page held the run past `timeout_s` — the one bound this transport has."""

        class _TricklingError(urllib.error.HTTPError):
            def __init__(self):
                super().__init__("http://x", 503, "Unavailable", {}, None)

            def read1(self, _n=None):
                time.sleep(0.02)
                return b" "          # never EOF

        def _raise(*_a, **_k):
            raise _TricklingError()

        started = time.monotonic()
        out = hl.run_pure_http_leaf(
            _entry(), [{"role": "user", "content": "P"}], timeout_s=0.1, opener=_raise)
        self.assertLess(time.monotonic() - started, 5.0)
        # The STATUS still reaches the classifier — that is what makes a 503 retryable.
        self.assertIn("HTTP 503", str(out.transport_error))
        self.assertIn("response_deadline_exceeded", str(out.transport_error))

    def test_an_enormous_error_body_is_refused_at_its_own_ceiling(self) -> None:
        """A diagnostic excerpt of 400 characters does not justify reading 32 MiB."""

        class _FloodError(urllib.error.HTTPError):
            def __init__(self):
                super().__init__("http://x", 500, "Boom", {}, None)

            def read1(self, n=65536):
                return b"x" * n

        def _raise(*_a, **_k):
            raise _FloodError()

        out = hl.run_pure_http_leaf(
            _entry(), [{"role": "user", "content": "P"}], opener=_raise)
        self.assertIn("HTTP 500", str(out.transport_error))
        self.assertIn("response_too_large", str(out.transport_error))
        self.assertLess(len(out.raw_response), hl._MAX_ERROR_BODY_BYTES)

    def test_a_normal_error_body_still_reaches_the_diagnostic(self) -> None:
        """The control: bounding must not stop a short error body being reported."""
        def _raise(*_a, **_k):
            raise urllib.error.HTTPError(
                "http://x", 429, "Too Many Requests", {},
                io.BytesIO(b'{"error": "slow down"}'))

        out = hl.run_pure_http_leaf(_entry(), [{"role": "user", "content": "P"}], opener=_raise)
        self.assertIn("slow down", str(out.transport_error))
        self.assertIn("slow down", out.raw_response)

    def test_the_default_opener_refuses_redirects(self) -> None:
        """A real redirect through the real opener: `urlopen` would follow it and copy the
        Authorization header onto the new host."""
        handler = hl._NoRedirects()
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            handler.redirect_request(
                urllib.request.Request("https://configured.example/v1/chat/completions"),
                io.BytesIO(b""), 302, "Found", {}, "https://elsewhere.example/v1")
        self.assertIn("redirect refused", str(ctx.exception))
        self.assertIn("elsewhere.example", str(ctx.exception))

    def test_the_transport_actually_installs_that_handler(self) -> None:
        opener = hl._default_opener()
        owner = getattr(opener, "__self__", None)
        self.assertTrue(
            any(isinstance(h, hl._NoRedirects) for h in getattr(owner, "handlers", [])),
            msg="the default opener must carry the no-redirect handler")

    def test_a_redirect_reaches_the_caller_as_a_transport_error(self) -> None:
        """End to end over a real socket, so the claim does not rest on the handler alone."""
        import http.server
        import threading

        seen: list[dict] = []

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):                              # noqa: N802
                seen.append({"path": self.path,
                            "auth": self.headers.get("Authorization")})
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:1/v1/chat/completions")
                self.end_headers()

            def log_message(self, *_a):                     # noqa: D102 - silence the test log
                return

        server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), thread.join(timeout=5)))
        base = f"http://127.0.0.1:{server.server_address[1]}/v1"
        out = hl.run_pure_http_leaf(
            _entry(base_url=base), [{"role": "user", "content": "P"}], timeout_s=10)
        self.assertIsNotNone(out.transport_error)
        self.assertIn("redirect refused", str(out.transport_error))
        # The key reached the CONFIGURED endpoint (correct) and exactly once.
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["auth"], f"Bearer {KEY_VALUE}")

    def test_the_default_max_tokens_is_sized_for_the_endpoints_this_targets(self) -> None:
        """Not the CLI leaf's 128000: that exceeds the whole context length of the local
        servers `openai_compatible` exists for, which reject the request outright — as a
        client error, so on the FIRST attempt and without a retry."""
        seen: list = []
        hl.run_pure_http_leaf(
            _entry(), [{"role": "user", "content": "P"}], opener=_opener(_OPENAI_OK, seen))
        self.assertEqual(seen[0]["body"]["max_tokens"], hl.DEFAULT_MAX_OUTPUT_TOKENS)
        self.assertLessEqual(hl.DEFAULT_MAX_OUTPUT_TOKENS, 32768)
        # And large enough for the artifact: the biggest CodegenBundle in this repository is
        # ~45 kB of JSON, so a ceiling much below this turns every run into a truncation loop.
        self.assertGreaterEqual(hl.DEFAULT_MAX_OUTPUT_TOKENS, 32768)


class StreamingRequestTests(unittest.TestCase):
    """What goes ON THE WIRE when streaming is on, and what stays off it when it is not."""

    def setUp(self) -> None:
        patcher = patch.dict("os.environ", {KEY_ENV: KEY_VALUE}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_streaming_is_on_by_default_for_a_configured_entry(self) -> None:
        """The default is the whole fix, so it is pinned through the CONFIG LOADER rather than
        through `_entry`, which deliberately inverts it. A non-streaming completion writes
        nothing until the answer exists; an intermediary bounding the interval between upstream
        reads then cuts it — measured, three attempts dead after 613.6 s / 612.3 s / 611.8 s
        with `HTTP 504` while the entry's own 2400 s `timeout_s` never fired."""
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "llm.yaml"
            path.write_text(
                "defaults:\n  provider: claude_cli\n"
                "phases:\n  generate:\n    substeps:\n      generate:\n"
                "        provider: openai_compatible\n"
                "        base_url: http://localhost:8000/v1\n"
                f"        api_key_env: {KEY_ENV}\n        model: test-model\n",
                encoding="utf-8")
            entry = lc.load_llm_config(path).entry_for("generate", "generate")
        self.assertIs(entry.stream, True)
        seen: list = []
        hl.run_pure_http_leaf(
            entry, [{"role": "user", "content": "P"}],
            opener=_stream_opener(
                _sse(("", _openai_chunk('{"ok": true}', finish_reason="stop"))),
                seen))
        self.assertIs(seen[0]["body"]["stream"], True)

    def test_the_openai_streaming_request_asks_for_usage_in_the_stream(self) -> None:
        """An OpenAI-shaped stream carries no usage unless asked, and usage is what reaches the
        `agent_runs` row the operator's cost comparison is read out of."""
        seen: list = []
        hl.run_pure_http_leaf(
            _entry(stream=True), [{"role": "user", "content": "P"}],
            opener=_stream_opener(
                _sse(("", _openai_chunk("x", finish_reason="stop"))), seen))
        self.assertIs(seen[0]["body"]["stream"], True)
        self.assertEqual(seen[0]["body"]["stream_options"], {"include_usage": True})
        self.assertEqual(seen[0]["headers"]["accept"], "text/event-stream")

    def test_the_anthropic_streaming_request_sets_stream_and_accept(self) -> None:
        """No `stream_options` counterpart: this API reports usage in the stream natively."""
        seen: list = []
        hl.run_pure_http_leaf(
            _entry("anthropic_api", stream=True), [{"role": "user", "content": "P"}],
            opener=_stream_opener(_ANTHROPIC_STREAM, seen))
        self.assertIs(seen[0]["body"]["stream"], True)
        self.assertNotIn("stream_options", seen[0]["body"])
        self.assertEqual(seen[0]["headers"]["accept"], "text/event-stream")

    def test_an_opted_out_entry_sends_exactly_the_non_streaming_request(self) -> None:
        """`stream: false` must send byte-for-byte the request that worked before streaming
        existed — not `"stream": false`. The escape hatch exists for an endpoint that rejects
        what it does not implement, so the hatch must not itself be a new key to reject."""
        seen: list = []
        hl.run_pure_http_leaf(
            _entry(stream=False), [{"role": "user", "content": "P"}],
            opener=_opener(_OPENAI_OK, seen))
        self.assertNotIn("stream", seen[0]["body"])
        self.assertNotIn("stream_options", seen[0]["body"])
        self.assertNotIn("accept", seen[0]["headers"])


class SseFramingTests(unittest.TestCase):
    """The wire format itself, independent of which provider's dialect rides on it."""

    def setUp(self) -> None:
        patcher = patch.dict("os.environ", {KEY_ENV: KEY_VALUE}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, body: str, *, slices: int = 1, provider: str = "openai_compatible"):
        return hl.run_pure_http_leaf(
            _entry(provider, stream=True), [{"role": "user", "content": "P"}],
            opener=_stream_opener(body, slices=slices))

    def test_a_frame_split_across_receives_is_reassembled(self) -> None:
        """A receive boundary is the network's business, not the message's. Parsing per receive
        would cut JSON documents in half at whatever offset the socket happened to return."""
        body = _sse(("", _openai_chunk("he")), ("", _openai_chunk("llo")),
                    ("", _openai_chunk(None, finish_reason="stop")), terminator="[DONE]")
        out = self._run(body, slices=9)
        self.assertIsNone(out.transport_error)
        self.assertEqual(out.text, "hello")

    def test_a_separator_split_across_two_receives_is_still_found(self) -> None:
        """The 3-byte rewind in `_SseBuffer`, pinned directly because nothing else pins it: with
        `_OVERLAP = 0` the whole suite stayed green while two frames silently MERGED into one
        unparseable blob, losing both deltas and the finish_reason — reported as a severed
        stream. Every interior cut of `\\r\\n\\r\\n` is exercised, because which byte the socket
        stops at is not something a test may assume."""
        body = _sse(("", _openai_chunk("A")),
                    ("", _openai_chunk("B", finish_reason="stop")),
                    terminator="[DONE]").replace("\n", "\r\n")
        raw = body.encode("utf-8")
        first = raw.index(b"\r\n\r\n")
        for cut in range(first + 1, first + 4):     # inside the separator, all three positions
            with self.subTest(cut=cut - first):
                out = hl.run_pure_http_leaf(
                    _entry(stream=True), [{"role": "user", "content": "P"}],
                    opener=lambda *_a, **_k: _ChunkedResponse([raw[:cut], raw[cut:]]))
                self.assertIsNone(out.transport_error)
                self.assertEqual(out.text, "AB")

    def test_the_frame_parser_joins_data_lines_with_a_newline(self) -> None:
        """Driven at `_parse_sse_frame`, not through a reader: every reader here parses the data
        as JSON, where `"\\n"` and `""` are equally valid whitespace, so a test that went through
        one could not tell the two joins apart — and did not."""
        self.assertEqual(
            hl._parse_sse_frame(b"data: one\ndata: two"), ("", "one\ntwo"))
        self.assertEqual(
            hl._parse_sse_frame(b"event: e\r\ndata: one\r\ndata: two"), ("e", "one\ntwo"))

    def test_crlf_framed_events_parse(self) -> None:
        """An intermediary that rewrites line endings must not merge the whole stream into one
        unparseable frame."""
        body = _sse(("", _openai_chunk("ok", finish_reason="stop"))).replace("\n", "\r\n")
        out = self._run(body)
        self.assertIsNone(out.transport_error)
        self.assertEqual(out.text, "ok")

    def test_multiple_data_lines_in_one_frame_join_with_a_newline(self) -> None:
        """Dropping the later `data:` lines of a frame truncates the document silently, which
        then reads as a model that emitted invalid JSON."""
        payload = json.dumps({"choices": [{"delta": {"content": "z"},
                                           "finish_reason": "stop"}]})
        half = len(payload) // 2
        # One frame, the JSON split over two `data:` lines, exactly as the format permits.
        body = f"data: {payload[:half]}\ndata: {payload[half:]}\n\n"
        out = hl.run_pure_http_leaf(
            _entry(stream=True), [{"role": "user", "content": "P"}],
            opener=_stream_opener(body))
        # The join introduces a newline INSIDE the JSON, which is legal whitespace there.
        self.assertIsNone(out.transport_error)
        self.assertEqual(out.text, "z")

    def test_a_keepalive_comment_holds_the_connection_without_entering_the_answer(self) -> None:
        """A `:` line is what an endpoint sends to keep an idle intermediary from timing the
        connection out — the same timer this whole change exists to stop tripping. It counts as
        bytes that arrived and contributes nothing to the document."""
        body = (": keepalive\n\n"
                + _sse(("", _openai_chunk("a", finish_reason="stop")))
                + ": keepalive\n\n")
        out = self._run(body)
        self.assertIsNone(out.transport_error)
        self.assertEqual(out.text, "a")
        self.assertIn(": keepalive", out.raw_response)

    def test_a_trailing_partial_frame_is_never_parsed_as_a_whole_one(self) -> None:
        """Half a JSON object read as a whole one is the failure this module refuses to
        produce: it would hand the validators a plausible-looking truncated document and let
        the loops blame the model for a severed connection."""
        body = (_sse(("", _openai_chunk("good", finish_reason="stop")))
                + 'data: {"choices": [{"delta": {"content": "trunc')
        out = self._run(body)
        self.assertIsNone(out.transport_error)
        self.assertEqual(out.text, "good")


_ANTHROPIC_STREAM = _sse(
    ("message_start", json.dumps({"message": {"model": "claude-opus-5-resolved",
                                              "usage": {"input_tokens": 33}}})),
    ("content_block_delta", json.dumps({"delta": {"type": "text_delta",
                                                  "text": '{"ok": true}'}})),
    ("message_delta", json.dumps({"delta": {"stop_reason": "end_turn"},
                                  "usage": {"output_tokens": 44}})),
    ("message_stop", json.dumps({"type": "message_stop"})),
)


class OpenAiStreamReadingTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.dict("os.environ", {KEY_ENV: KEY_VALUE}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, body: str):
        return hl.run_pure_http_leaf(
            _entry(stream=True), [{"role": "user", "content": "P"}],
            opener=_stream_opener(body))

    def test_an_openai_stream_concatenates_its_content_deltas(self) -> None:
        out = self._run(_sse(("", _openai_chunk('{"o')), ("", _openai_chunk('k": true}')),
                             ("", _openai_chunk(None, finish_reason="stop")),
                             terminator="[DONE]"))
        self.assertIsNone(out.transport_error)
        self.assertEqual(out.text, '{"ok": true}')
        self.assertEqual(out.model, "test-model-resolved")

    def test_the_usage_chunk_with_no_choices_is_not_an_error(self) -> None:
        """`stream_options.include_usage` makes the FINAL chunk carry `choices: []`. Indexing
        `[0]` blindly would turn the very option this transport asks for into a crash."""
        out = self._run(_sse(("", _openai_chunk("x", finish_reason="stop")),
                             ("", _openai_chunk(usage={"prompt_tokens": 11,
                                                       "completion_tokens": 22})),
                             terminator="[DONE]"))
        self.assertIsNone(out.transport_error)
        self.assertEqual(out.text, "x")
        self.assertEqual(out.usage, {"input_tokens": 11, "output_tokens": 22,
                                     "total_tokens": 33, "usage_source": "http_provider"})

    def test_the_streamed_usage_chunk_carries_the_reasoning_and_cache_splits(self) -> None:
        """The streaming reader must not be the poorer twin: production runs stream, so a split
        that only the buffered path extracted would never be recorded at all."""
        out = self._run(_sse(("", _openai_chunk("x", finish_reason="stop")),
                             ("", _openai_chunk(usage={
                                 "prompt_tokens": 33_000, "completion_tokens": 23_538,
                                 "completion_tokens_details": {"reasoning_tokens": 23_438},
                                 "prompt_tokens_details": {"cached_tokens": 32_832}})),
                             terminator="[DONE]"))
        self.assertEqual(out.usage["reasoning_tokens"], 23_438)
        self.assertEqual(out.usage["cached_tokens"], 32_832)
        self.assertEqual(out.usage["total_tokens"], 33_000 + 23_538)

    def test_an_openai_stream_finishing_on_length_is_reported_truncated(self) -> None:
        """The provider's own verdict, which the caller routes as `pure_response_truncated`
        without consulting the extractor — same authority as the buffered path's."""
        out = self._run(_sse(("", _openai_chunk("cut", finish_reason="length")),
                             terminator="[DONE]"))
        self.assertIsNone(out.transport_error)
        self.assertTrue(out.truncated)

    def test_reasoning_deltas_are_not_part_of_the_answer_document(self) -> None:
        """Thinking is not the document. The buffered reader takes only `message.content`, and
        a streaming reader that also swallowed `reasoning_content` would prepend the model's
        scratchpad to a JSON artifact and blame the model when it failed to parse."""
        chunk = json.dumps({"choices": [{"delta": {"reasoning_content": "hmm..."},
                                         "finish_reason": None}]})
        out = self._run(_sse(("", chunk), ("", _openai_chunk("ok", finish_reason="stop")),
                             terminator="[DONE]"))
        self.assertEqual(out.text, "ok")

    def test_a_stream_that_ends_on_a_finish_reason_is_complete_without_done(self) -> None:
        """Servers disagree about which terminator they send — llama.cpp has shipped without
        `[DONE]` — so completion is the UNION of the two, not either one alone."""
        out = self._run(_sse(("", _openai_chunk("ok", finish_reason="stop"))))
        self.assertIsNone(out.transport_error)
        self.assertEqual(out.text, "ok")

    def test_a_terminated_stream_that_carried_no_choice_is_not_a_success(self) -> None:
        """Mirrors the buffered reader's `response_missing_choices` exactly. A well-terminated
        stream of nothing but the `include_usage` chunk (`choices: []`) and `[DONE]` would
        otherwise be a SUCCESS with empty text, and the empty answer would be spent as a
        bundle-repair turn instead of being named."""
        out = self._run(_sse(("", _openai_chunk(usage={"prompt_tokens": 1,
                                                       "completion_tokens": 0})),
                             terminator="[DONE]"))
        self.assertEqual(out.transport_error, "response_missing_choices")

    def test_an_empty_content_inside_a_real_choice_stays_legal(self) -> None:
        """The other side of the mirror: the buffered reader accepts `content: ""`, so the
        streaming one must not turn an empty-but-delivered answer into a transport failure."""
        out = self._run(_sse(("", _openai_chunk("", finish_reason="stop")),
                             terminator="[DONE]"))
        self.assertIsNone(out.transport_error)
        self.assertEqual(out.text, "")

    def test_a_body_that_is_not_an_event_stream_is_named_rather_than_retried(self) -> None:
        """An endpoint that ignores `stream: true` and answers with an ordinary JSON body is a
        deterministic misconfiguration that reproduces on every launch. Worded so it does NOT
        classify as a transient flake — three billed re-launches of a config error is the exact
        waste this whole change was written to stop — and naming the remedy."""
        out = hl.run_pure_http_leaf(
            _entry(stream=True), [{"role": "user", "content": "P"}],
            opener=_stream_opener(json.dumps(_OPENAI_OK)))
        self.assertIn("response_not_an_event_stream", str(out.transport_error))
        self.assertIn("stream: false", str(out.transport_error))
        self.assertIsNone(wc._leaf_infra_error(wc.ProcResult(1, "", out.transport_error)))

    def test_a_non_stream_body_that_merely_mentions_data_is_still_caught(self) -> None:
        """The check asks whether a FRAME arrived, not whether the bytes contain `data:`. A model
        answer quoting that token would otherwise escape the check and buy three re-launches of a
        misconfiguration."""
        body = json.dumps({"model": "m", "choices": [
            {"message": {"content": "see data: x"}, "finish_reason": "stop"}]})
        out = hl.run_pure_http_leaf(
            _entry(stream=True), [{"role": "user", "content": "P"}],
            opener=_stream_opener(body))
        self.assertIn("response_not_an_event_stream", str(out.transport_error))

    def test_a_stream_severed_inside_its_very_first_line_is_a_retryable_severance(self) -> None:
        """The narrowest severance there is, and the one a frame-arrival test got backwards: the
        connection dies part-way through the first `data:` line, so no frame ever completes. It
        is plainly an event stream and plainly a transport fault; called a wrong content type it
        would fail closed non-retryably, while the SAME severance one frame later was retried."""
        opening = _sse(("", _openai_chunk("half an ans")))[:40]
        self.assertTrue(opening.startswith("data: "))
        out = hl.run_pure_http_leaf(
            _entry(stream=True), [{"role": "user", "content": "P"}],
            opener=_stream_opener(opening))
        self.assertIn("stream interrupted", str(out.transport_error))
        self.assertEqual(wc._leaf_infra_error(wc.ProcResult(1, "", out.transport_error))[0],
                         "llm_transport_flake")

    def test_a_conforming_stream_that_flush_primes_with_a_newline_is_accepted(self) -> None:
        """Blank lines are legal separators, and a gateway that primes the response by flushing
        one before the first frame is emitting a conforming stream. Requiring the field at byte 0
        rejected it — and rejected it NON-retryably, discarding a complete and fully billed
        answer the framer had already parsed, under a message telling the operator to disable
        streaming on an endpoint that speaks it correctly."""
        answer = _sse(("", _openai_chunk('{"ok": true}', finish_reason="stop")),
                      terminator="[DONE]")
        for prime in ("\n", "\r\n", "\n\n\n"):
            with self.subTest(prime=repr(prime)):
                out = hl.run_pure_http_leaf(
                    _entry(stream=True), [{"role": "user", "content": "P"}],
                    opener=_stream_opener(prime + answer))
                self.assertIsNone(out.transport_error)
                self.assertEqual(out.text, '{"ok": true}')

    def test_a_leading_byte_order_mark_is_ignored_rather_than_eating_a_frame(self) -> None:
        """The wire format says a BOM must be ignored. Left in place it became part of the first
        field NAME (`\\xef\\xbb\\xbfdata`), which matches nothing — so the first frame of a
        conforming stream was dropped silently and the answer came back short. Exercised with the
        marker delivered whole AND split across receives, since it is three bytes and a receive
        boundary can fall inside it."""
        answer = _sse(("", _openai_chunk("first ")),
                      ("", _openai_chunk("second", finish_reason="stop")),
                      terminator="[DONE]")
        for chunk_bytes in (4096, 2, 1):
            with self.subTest(chunk_bytes=chunk_bytes):
                out = hl.run_pure_http_leaf(
                    _entry(stream=True), [{"role": "user", "content": "P"}],
                    opener=_stream_opener("﻿" + answer, chunk_bytes=chunk_bytes))
                self.assertIsNone(out.transport_error)
                self.assertEqual(out.text, "first second")

    def test_a_non_stream_body_containing_a_blank_line_is_still_caught(self) -> None:
        """The other direction of the same predicate. An HTML error page has blank lines in it,
        so a frame-arrival test declared it a severed stream and bought three re-launches of a
        deterministic misconfiguration. What it does NOT have is an event-stream opening line."""
        page = "<html>\n\n<body>502 Bad Gateway</body>\n\n</html>\n"
        out = hl.run_pure_http_leaf(
            _entry(stream=True), [{"role": "user", "content": "P"}],
            opener=_stream_opener(page))
        self.assertIn("response_not_an_event_stream", str(out.transport_error))
        self.assertIsNone(wc._leaf_infra_error(wc.ProcResult(1, "", out.transport_error)))

    def test_a_falsy_error_key_on_a_content_chunk_is_not_an_error(self) -> None:
        """A proxy that stamps `"error": null` — or `""`, `{}`, `false` — on every ordinary chunk
        is a real shape. Treating any of them as an error both dropped the chunk's content and
        reported nonsense; with `""` the result was the worst outcome available, a SUCCESSFUL
        turn carrying an empty document for the validators to blame the model for."""
        for falsy in ("null", '""', "{}", "false", "0", "[]"):
            with self.subTest(error=falsy):
                chunk = ('{"model": "m", "choices": [{"delta": {"content": "ok"}, '
                         f'"finish_reason": "stop"}}], "error": {falsy}}}')
                out = self._run(_sse(("", chunk), terminator="[DONE]"))
                self.assertIsNone(out.transport_error)
                self.assertEqual(out.text, "ok")

    def test_a_stream_of_only_keepalives_that_dies_is_a_retryable_severance(self) -> None:
        """The other side of that check, and the one that matters most. A gateway holding a long
        time-to-first-token open sends nothing but `: keepalive` comment frames — precisely the
        case streaming was introduced for. It contains no `data:` at all, so a body-substring
        test called it a wrong content type and failed the run closed NON-retryably, while the
        identical severance over a chunked connection was retried. It is a transport fault."""
        out = hl.run_pure_http_leaf(
            _entry(stream=True), [{"role": "user", "content": "P"}],
            opener=_stream_opener(": keepalive\n\n: keepalive\n\n"))
        self.assertIn("stream interrupted", str(out.transport_error))
        self.assertEqual(wc._leaf_infra_error(wc.ProcResult(1, "", out.transport_error))[0],
                         "llm_transport_flake")

    def test_an_answer_that_completed_before_the_size_ceiling_is_salvaged(self) -> None:
        """The salvage covers the BOUNDS errors too, not just a severed socket: a gateway that
        keeps talking after `[DONE]` until the 32 MiB ceiling trips has still delivered the
        answer, and the ceiling is about the socket.

        The ceiling is lowered by passing `max_bytes` down rather than by patching
        `hl._MAX_RESPONSE_BYTES`: it is read as a DEFAULT ARGUMENT, bound at def time, so
        patching the module attribute changes nothing the code reads — a first version of this
        test did exactly that, ran a 4 kB body against the real 32 MiB ceiling, and passed while
        the salvage it named was deleted. The receives are also kept small enough that the
        answer lands before the ceiling: `_iter_bounded` DISCARDS the receive that crosses it,
        so a coarse split would refuse the stream before any frame arrived."""
        answer = _sse(("", _openai_chunk('{"ok": true}', finish_reason="stop")),
                      terminator="[DONE]")
        ceiling = len(answer) + 16
        real_iter = hl._iter_bounded
        with patch.object(hl, "_iter_bounded",
                          lambda r, d, max_bytes=ceiling: real_iter(r, d, ceiling)):
            out = hl.run_pure_http_leaf(
                _entry(stream=True), [{"role": "user", "content": "P"}], timeout_s=30,
                opener=_stream_opener(answer + "x" * 4096, chunk_bytes=16))
        self.assertIsNone(out.transport_error)
        self.assertEqual(out.text, '{"ok": true}')

    def test_the_same_stream_without_its_terminator_still_trips_the_ceiling(self) -> None:
        """The control for the test above: the salvage must be what makes the difference, not a
        ceiling that never fired. Same bytes, same ceiling, no `[DONE]`."""
        answer = _sse(("", _openai_chunk('{"ok": true}')))
        ceiling = len(answer) + 16
        real_iter = hl._iter_bounded
        with patch.object(hl, "_iter_bounded",
                          lambda r, d, max_bytes=ceiling: real_iter(r, d, ceiling)):
            out = hl.run_pure_http_leaf(
                _entry(stream=True), [{"role": "user", "content": "P"}], timeout_s=30,
                opener=_stream_opener(answer + "x" * 4096, chunk_bytes=16))
        self.assertIn("response_too_large", str(out.transport_error))

    def test_a_provider_error_quoted_by_a_reader_is_redacted(self) -> None:
        """A reader's error used to be a constant, so returning it unredacted was safe by
        construction. It stopped being one when the stream readers began quoting the provider's
        own `error` frame — and "Incorrect API key provided: sk-..." is exactly the message a
        provider puts there. The caller emits this string as an event and stores it in the
        leaf's stderr, both persisted."""
        body = _sse(("", json.dumps({"error": {
            "type": "invalid_request_error",
            "message": f"Incorrect API key provided: {KEY_VALUE}"}})),
            terminator="[DONE]")
        out = hl.run_pure_http_leaf(
            _entry(stream=True), [{"role": "user", "content": "P"}],
            opener=_stream_opener(body))
        self.assertNotIn(KEY_VALUE, str(out.transport_error))
        self.assertIn("[redacted-api-key]", str(out.transport_error))
        self.assertNotIn(KEY_VALUE, out.raw_response)

    def test_an_openai_error_frame_beats_a_terminator_that_follows_it(self) -> None:
        """The mirror of the Messages API's `error` event, which this dialect sends as an
        ordinary chunk carrying an `error` key. Without it the frame has no `choices`, is skipped
        as unmodelled, and the `[DONE]` that follows reports an upstream failure as a COMPLETE
        answer — handing the validators a truncated document to blame the model for."""
        out = self._run(_sse(("", _openai_chunk('{"partial')),
                             ("", json.dumps({"error": {"type": "server_error",
                                                        "message": "upstream disconnected"}})),
                             terminator="[DONE]"))
        self.assertEqual(out.text, "")
        self.assertIn("server_error", str(out.transport_error))
        self.assertEqual(wc._leaf_infra_error(wc.ProcResult(1, "", out.transport_error))[0],
                         "llm_transport_flake")

    def test_an_openai_stream_with_neither_marker_is_a_transport_error(self) -> None:
        """The case that must not be papered over: a connection severed at 90% leaves a
        syntactically plausible partial document. Reported as a transport failure, it is
        retried; passed through, it is blamed on the model and spends repair turns."""
        out = self._run(_sse(("", _openai_chunk("half an ans"))))
        self.assertEqual(out.text, "")
        self.assertIn("stream interrupted", str(out.transport_error))


class AnthropicStreamReadingTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.dict("os.environ", {KEY_ENV: KEY_VALUE}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, body: str):
        return hl.run_pure_http_leaf(
            _entry("anthropic_api", stream=True), [{"role": "user", "content": "P"}],
            opener=_stream_opener(body))

    def test_an_anthropic_stream_concatenates_text_and_both_usage_halves(self) -> None:
        """Input tokens arrive in `message_start`, output tokens in `message_delta`; a reader
        that took only one of them would report half a bill."""
        out = self._run(_ANTHROPIC_STREAM)
        self.assertIsNone(out.transport_error)
        self.assertEqual(out.text, '{"ok": true}')
        self.assertEqual(out.model, "claude-opus-5-resolved")
        self.assertEqual(out.usage, {"input_tokens": 33, "output_tokens": 44,
                                     "total_tokens": 77, "usage_source": "http_provider"})

    def test_an_anthropic_stream_takes_the_whole_input_side_from_message_start(self) -> None:
        """`message_start` carries the uncached input AND both cache classes; only
        `output_tokens` arrives later. Reading just `input_tokens` there dropped the cache
        split on every streamed turn."""
        body = _sse(
            ("message_start", json.dumps({"message": {
                "model": "claude-opus-5-resolved",
                "usage": {"input_tokens": 33, "cache_read_input_tokens": 14_278,
                          "cache_creation_input_tokens": 5_849}}})),
            ("content_block_delta", json.dumps({"delta": {"type": "text_delta", "text": "x"}})),
            ("message_delta", json.dumps({"delta": {"stop_reason": "end_turn"},
                                          "usage": {"output_tokens": 44}})),
            ("message_stop", json.dumps({"type": "message_stop"})),
        )
        out = self._run(body)
        self.assertEqual(out.usage["cache_read_input_tokens"], 14_278)
        self.assertEqual(out.usage["cache_creation_input_tokens"], 5_849)
        self.assertEqual(out.usage["output_tokens"], 44)
        self.assertEqual(out.usage["total_tokens"], 33 + 44 + 14_278 + 5_849)

    def test_a_thinking_delta_is_not_part_of_the_answer_document(self) -> None:
        """Mirrors the buffered reader, which concatenates only `type == "text"` blocks."""
        body = _sse(
            ("message_start", json.dumps({"message": {"model": "m"}})),
            ("content_block_delta", json.dumps({"delta": {"type": "thinking_delta",
                                                          "thinking": "hmm"}})),
            ("content_block_delta", json.dumps({"delta": {"type": "text_delta", "text": "ok"}})),
            ("message_stop", "{}"))
        self.assertEqual(self._run(body).text, "ok")

    def test_an_anthropic_stream_stopped_at_max_tokens_is_reported_truncated(self) -> None:
        body = _sse(
            ("content_block_delta", json.dumps({"delta": {"type": "text_delta", "text": "cut"}})),
            ("message_delta", json.dumps({"delta": {"stop_reason": "max_tokens"}})),
            ("message_stop", "{}"))
        out = self._run(body)
        self.assertIsNone(out.transport_error)
        self.assertTrue(out.truncated)

    def test_an_anthropic_stream_without_message_stop_is_a_transport_error(self) -> None:
        body = _sse(
            ("content_block_delta", json.dumps({"delta": {"type": "text_delta",
                                                          "text": "half"}})))
        out = self._run(body)
        self.assertEqual(out.text, "")
        self.assertIn("stream interrupted", str(out.transport_error))

    def test_a_completed_stream_carrying_no_text_block_is_not_a_success(self) -> None:
        """The mirror of the buffered reader's `response_has_no_text_block`, and of the OpenAI
        stream reader's `response_missing_choices`. A Messages-API stream that reaches
        `message_stop` having sent only thinking deltas gave no answer; reported as a success it
        would spend a bundle-repair turn on an empty document."""
        body = _sse(
            ("message_start", json.dumps({"message": {"model": "m"}})),
            ("content_block_delta", json.dumps({"delta": {"type": "thinking_delta",
                                                          "thinking": "hmm"}})),
            ("message_stop", "{}"))
        self.assertEqual(self._run(body).transport_error, "response_has_no_text_block")

    def test_an_error_event_after_a_200_beats_a_message_stop(self) -> None:
        """This API really does answer 200 and then fail mid-stream. A `message_stop` that
        follows must not launder that into a successful empty answer."""
        body = _sse(
            ("content_block_delta", json.dumps({"delta": {"type": "text_delta", "text": "x"}})),
            ("error", json.dumps({"error": {"type": "overloaded_error",
                                            "message": "Overloaded"}})),
            ("message_stop", "{}"))
        out = self._run(body)
        self.assertEqual(out.text, "")
        self.assertIn("overloaded_error", str(out.transport_error))


class _StallingStream:
    """A stream that dribbles below any per-receive interval and never reaches a frame end."""

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read1(self, _n=None):
        time.sleep(0.02)
        return b" "                          # never EOF, never a complete frame


class _FloodingStream:
    """A stream that never stops producing bytes."""

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read1(self, n=65536):
        return b"x" * n


class StreamBoundsAndEvidenceTests(unittest.TestCase):
    """The bounds the refactor had to carry across, and what survives a death."""

    def setUp(self) -> None:
        patcher = patch.dict("os.environ", {KEY_ENV: KEY_VALUE}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_stalled_stream_is_still_cut_off_at_the_deadline(self) -> None:
        """`timeout_s` bounds the TOTAL, and a stream is exactly the shape that could have
        reset a per-receive timeout forever. There is deliberately no separate idle bound: a
        reasoning model can legitimately think for minutes before its first token, and an idle
        bound tight enough to be useful would kill the request this change exists to save."""
        started = time.monotonic()
        out = hl.run_pure_http_leaf(
            _entry(stream=True), [{"role": "user", "content": "P"}], timeout_s=0.3,
            opener=lambda *_a, **_k: _StallingStream())
        elapsed = time.monotonic() - started
        self.assertEqual(out.transport_error, "response_deadline_exceeded")
        self.assertLess(elapsed, 2.0)

    def test_an_oversized_stream_is_refused_at_the_same_ceiling(self) -> None:
        """One ceiling for both post paths — a trickling endpoint must not be able to hold the
        run open by never completing a frame.

        The elapsed bound is the load-bearing half. This flood never emits a frame boundary, so
        a splitter that re-scanned the whole pending buffer on every receive spent its time in
        `memcpy` instead: measured, this exact case took over 30 s to reach a ceiling it now
        reaches in well under one. The refusal was never in doubt; WHEN it arrived was."""
        started = time.monotonic()
        out = hl.run_pure_http_leaf(
            _entry(stream=True), [{"role": "user", "content": "P"}], timeout_s=30,
            opener=lambda *_a, **_k: _FloodingStream())
        elapsed = time.monotonic() - started
        self.assertIn("response_too_large", str(out.transport_error))
        self.assertLess(elapsed, 5.0, msg=f"took {elapsed:.1f}s to refuse an oversized stream")

    def test_an_http_error_before_the_first_frame_reports_like_a_buffered_one(self) -> None:
        """The incident itself: a gateway timeout arrives before any frame, so both paths must
        produce the same `HTTP <code>` report the conductor classifies as a transport flake."""
        page = (b"<html>\r\n<head><title>504 Gateway Time-out</title></head>\r\n"
                b"<body><center><h1>504 Gateway Time-out</h1></center></body>\r\n</html>\r\n")

        def _open(request, timeout=None):    # noqa: ANN001 - test double
            raise urllib.error.HTTPError(
                request.full_url, 504, "Gateway Time-out", {}, io.BytesIO(page))

        out = hl.run_pure_http_leaf(
            _entry(stream=True), [{"role": "user", "content": "P"}], opener=_open)
        self.assertIn("HTTP 504", str(out.transport_error))
        self.assertEqual(wc._leaf_infra_error(wc.ProcResult(1, "", out.transport_error))[0],
                         "llm_transport_flake")

    def test_a_stream_that_dies_keeps_what_arrived_as_the_raw_response(self) -> None:
        """Unlike the buffered path, which returns an empty body on a failed read. A stream that
        died at 90% has no other record of where it died, and that record is what gets persisted
        under `launches/`."""
        out = hl.run_pure_http_leaf(
            _entry(stream=True), [{"role": "user", "content": "P"}],
            opener=_stream_opener(_sse(("", _openai_chunk("partial")))))
        self.assertIn("stream interrupted", str(out.transport_error))
        self.assertIn("partial", out.raw_response)

    def test_a_key_echoed_in_a_stream_frame_is_redacted_from_the_raw_response(self) -> None:
        """The raw stream is persisted verbatim, so the redaction contract has to cover text
        this module did not write — a debug gateway echoing the request headers back."""
        body = _sse(("", _openai_chunk(f"seen {KEY_VALUE}", finish_reason="stop")))
        out = hl.run_pure_http_leaf(
            _entry(stream=True), [{"role": "user", "content": "P"}],
            opener=_stream_opener(body))
        self.assertNotIn(KEY_VALUE, out.raw_response)
        self.assertIn("[redacted-api-key]", out.raw_response)

    def _serve_raw(self, response: bytes, *, cut: bool = False, stall: bool = False) -> str:
        """A real server that writes `response` verbatim, then either closes abruptly (`cut`) or
        holds the connection open (`stall`). Returns its base_url.

        Raw, rather than a response double, because what these tests are about is the TRANSPORT
        ENCODING — whether a severance surfaces as a clean EOF or as `IncompleteRead` is decided
        by `http.client` reading a real chunked framing, and no double reproduces that."""
        import socket
        import threading

        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        self.addCleanup(srv.close)
        stop = threading.Event()
        self.addCleanup(stop.set)

        def _serve():
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with conn:
                conn.recv(65536)
                try:
                    conn.sendall(response)
                except OSError:
                    return
                if stall:
                    stop.wait(30)            # the gateway holding an idle connection open
                # `cut` (and the default) simply fall out of the `with`, closing mid-body.
        threading.Thread(target=_serve, daemon=True).start()
        return f"http://127.0.0.1:{srv.getsockname()[1]}/v1"

    def test_a_chunked_stream_cut_mid_body_is_classified_as_a_transport_flake(self) -> None:
        """`Transfer-Encoding: chunked` is the dominant encoding for streaming, and a connection
        cut mid-chunk does NOT reach a clean EOF — `http.client` raises `IncompleteRead`, whose
        text (`IncompleteRead(0 bytes read)`) matches no classifier pattern. Unclassified is
        non-retryable, so without the `stream interrupted` prefix on that path the exact
        mid-stream severance this transport exists to survive failed the run closed, while the
        close-delimited form of the SAME event was retried. Driven over a real socket, because
        the encoding is the whole point and no double reproduces it."""
        base = self._serve_raw(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n"
            + b"20\r\ndata: {\"choices\":[{\"delta\":{}}]}\r\n\r\n",   # a chunk, then FIN
            cut=True)
        out = hl.run_pure_http_leaf(
            _entry(base_url=base, stream=True), [{"role": "user", "content": "P"}],
            timeout_s=10)
        self.assertIn("stream interrupted", str(out.transport_error))
        self.assertEqual(wc._leaf_infra_error(wc.ProcResult(1, "", out.transport_error))[0],
                         "llm_transport_flake")

    def test_an_answer_completed_before_an_untidy_teardown_is_not_thrown_away(self) -> None:
        """The provider's terminator is authoritative. If `[DONE]` reached us, the model is done
        and billed — a severed teardown after it (here: a FIN before the terminating zero-length
        chunk) says nothing about the answer. Discarding it would spend a full generation, ten
        minutes of it on this workload, and re-launch for something already in hand."""
        frames = (_sse(("", _openai_chunk('{"ok": true}', finish_reason="stop")),
                       terminator="[DONE]")).encode("utf-8")
        base = self._serve_raw(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n"
            + f"{len(frames):x}\r\n".encode() + frames + b"\r\n",     # no 0-chunk, then FIN
            cut=True)
        out = hl.run_pure_http_leaf(
            _entry(base_url=base, stream=True), [{"role": "user", "content": "P"}],
            timeout_s=10)
        self.assertIsNone(out.transport_error)
        self.assertEqual(out.text, '{"ok": true}')

    @pytest.mark.slow
    def test_a_keepalive_trailer_after_the_terminator_does_not_lose_the_answer(self) -> None:
        """The sibling case, and the one an SSE gateway produces by design: it holds the
        connection open with `: keepalive` comments after the answer is complete, so the read
        ends at OUR deadline. The answer arrived; the deadline is about the socket."""
        answer = _sse(("", _openai_chunk('{"ok": true}', finish_reason="stop")),
                      terminator="[DONE]").encode("utf-8")
        base = self._serve_raw(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
            b"Connection: close\r\n\r\n" + answer,
            stall=True)
        out = hl.run_pure_http_leaf(
            _entry(base_url=base, stream=True), [{"role": "user", "content": "P"}],
            timeout_s=0.5)
        self.assertIsNone(out.transport_error)
        self.assertEqual(out.text, '{"ok": true}')

    def test_every_stream_failure_string_is_classified_by_the_conductor(self) -> None:
        """The load-bearing one. The conductor decides whether to re-launch by matching the
        leaf's terminal text; a string it recognises nowhere is an UNCLASSIFIED nonzero exit,
        which is non-retryable — so a wording slip here fails a genuinely transient network
        fault closed instead of retrying it."""
        cases = [
            (f"{hl._STREAM_INTERRUPTED}: ended after 3 frames with no [DONE] and no "
             f"finish_reason", "llm_transport_flake"),
            (f"{hl._STREAM_INTERRUPTED}: ended after 0 frames with no message_stop",
             "llm_transport_flake"),
            (f"{hl._STREAM_INTERRUPTED}: provider error event api_error: upstream died",
             "llm_transport_flake"),
            # A more severe tag on the same line must win, so the backoff matches the fault.
            (f"{hl._STREAM_INTERRUPTED}: provider error event overloaded_error: Overloaded",
             "llm_overloaded"),
            (f"{hl._STREAM_INTERRUPTED}: provider error event rate_limit_error: slow down",
             "llm_rate_limit"),
        ]
        for message, expected in cases:
            with self.subTest(message=message):
                tag = wc._leaf_infra_error(wc.ProcResult(1, "", message))
                self.assertIsNotNone(tag, msg="matched no pattern at all")
                self.assertEqual(tag[0], expected)
                self.assertIn(tag[0], wc._RETRYABLE_LEAF_INFRA_TAGS)


if __name__ == "__main__":
    unittest.main()
