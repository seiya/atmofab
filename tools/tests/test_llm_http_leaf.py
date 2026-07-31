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
from tools import workflow_conductor as wc
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
                stop.wait(0.9)               # just before a 1.0s deadline
                try:
                    conn.sendall(b" ")
                except OSError:
                    return
                stop.wait(30)
        threading.Thread(target=_serve, daemon=True).start()
        return f"http://127.0.0.1:{srv.getsockname()[1]}/v1"

    def test_an_error_body_that_goes_silent_still_ends_at_the_deadline(self) -> None:
        """The wrapper chain nests differently for an `HTTPError` (it adds a layer), so a
        fixed-path unwrap reached the socket for a success and not for an error — a 503 whose
        body went silent ran for the deadline PLUS a full socket timeout (measured 3.5 s
        against a 2 s bound)."""
        entry = _entry(base_url=self._silent_after_one_byte(b"HTTP/1.1 503 Unavailable"))
        started = time.monotonic()
        out = hl.run_pure_http_leaf(entry, [{"role": "user", "content": "P"}], timeout_s=1.0)
        elapsed = time.monotonic() - started
        self.assertIn("HTTP 503", str(out.transport_error))
        # An un-narrowed socket would wait a full extra timeout from t=0.9 -> ~1.9s.
        self.assertLess(elapsed, 1.4, msg=f"took {elapsed:.1f}s for a 1.0s deadline")

    def test_a_success_body_that_goes_silent_ends_at_the_deadline(self) -> None:
        entry = _entry(base_url=self._silent_after_one_byte(b"HTTP/1.1 200 OK"))
        started = time.monotonic()
        out = hl.run_pure_http_leaf(entry, [{"role": "user", "content": "P"}], timeout_s=1.0)
        elapsed = time.monotonic() - started
        self.assertEqual(out.transport_error, "response_deadline_exceeded")
        self.assertLess(elapsed, 1.4, msg=f"took {elapsed:.1f}s for a 1.0s deadline")

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
                time.sleep(0.9)              # a byte just before a 1 s deadline...
                try:
                    conn.sendall(b" ")
                except OSError:
                    return
                stop.wait(30)                # ...then stall
        threading.Thread(target=_serve, daemon=True).start()

        entry = _entry(base_url=f"http://127.0.0.1:{srv.getsockname()[1]}/v1")
        started = time.monotonic()
        out = hl.run_pure_http_leaf(
            entry, [{"role": "user", "content": "P"}], timeout_s=1.0)
        elapsed = time.monotonic() - started
        self.assertEqual(out.transport_error, "response_deadline_exceeded")
        # Comfortably under 2x, which is what an un-narrowed per-receive timeout would give.
        self.assertLess(elapsed, 1.6, msg=f"took {elapsed:.1f}s for a 1.0s deadline")

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


if __name__ == "__main__":
    unittest.main()
