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
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from tools import llm_config as lc
from tools import llm_http_leaf as hl
from tools.pure_leaf import PURE_SYSTEM_PROMPT

KEY_ENV = "METDSL_TEST_HTTP_KEY"
KEY_VALUE = "sk-test-do-not-log-me"


def _entry(provider: str = "openai_compatible", **kw) -> lc.ResolvedLeafEntry:
    base = dict(
        provider=provider,
        model="test-model",
        base_url=("http://localhost:8000/v1" if provider == "openai_compatible"
                  else lc.ANTHROPIC_DEFAULT_BASE_URL),
        api_key_env=KEY_ENV,
        capabilities=lc.PROVIDER_CAPABILITIES[provider],
    )
    base.update(kw)
    return lc.ResolvedLeafEntry(**base)


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
        self.assertEqual(out.usage, {"input_tokens": 11, "output_tokens": 22})
        self.assertFalse(out.truncated)

    def test_anthropic_response_is_read_and_usage_normalized(self) -> None:
        out = hl.run_pure_http_leaf(
            _entry("anthropic_api"), [{"role": "user", "content": "P"}],
            opener=_opener(_ANTHROPIC_OK))
        self.assertIsNone(out.transport_error)
        self.assertEqual(out.text, '{"ok": true}')
        self.assertEqual(out.model, "claude-opus-5-resolved")
        self.assertEqual(out.usage, {"input_tokens": 33, "output_tokens": 44})

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
        self.assertIn("http_status_429", self._error(_rate_limited))

    def test_server_error_status(self) -> None:
        def _boom(*_a, **_k):
            raise urllib.error.HTTPError("http://x", 503, "Unavailable", {}, io.BytesIO(b""))
        self.assertIn("http_status_503", self._error(_boom))

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

            def read(self, _n=None):
                time.sleep(0.02)
                return b" "          # never EOF

        out = hl.run_pure_http_leaf(
            _entry(), [{"role": "user", "content": "P"}],
            timeout_s=0.1, opener=lambda *_a, **_k: _Trickle())
        self.assertEqual(out.transport_error, "response_deadline_exceeded")
        self.assertEqual(out.text, "")

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
        servers `openai_compatible` exists for, which reject the request outright."""
        seen: list = []
        hl.run_pure_http_leaf(
            _entry(), [{"role": "user", "content": "P"}], opener=_opener(_OPENAI_OK, seen))
        self.assertEqual(seen[0]["body"]["max_tokens"], hl.DEFAULT_MAX_OUTPUT_TOKENS)
        self.assertLessEqual(hl.DEFAULT_MAX_OUTPUT_TOKENS, 32768)


if __name__ == "__main__":
    unittest.main()
