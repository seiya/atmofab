"""The conductor running a pure leaf over HTTP (issue #28 Phase 5).

`test_llm_http_leaf.py` covers the transport in isolation. This file covers the WIRING: that
`spawn_leaf` dispatches an HTTP entry into `_run_http_leaf` instead of spawning anything, that
the reply comes back in the shape the existing pure loop reads (so the bundle validators, the
repair loop, the artifact writes and the finalize-before-write ordering are all untouched), and
that the two failure modes peculiar to this transport — a dead endpoint and a
provider-reported truncation — land in the categories the loop already knows.

The real `_run_pure_generate_substep` runs here, against the same M3c fixture the CLI producer
tests use (imported from `test_pure_leaf_producer`, so the two cannot drift): only
`urllib.request.urlopen` is replaced. The point is that almost nothing else needed replacing.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

import tools.llm_config as lc
import tools.workflow_conductor as wc

from tools.tests.test_pure_leaf_producer import (
    _SPEC_ID,
    _PureFakeConductor,
    _RenderingFakeConductor,
    _valid_bundle,
    _write_node,
)
from tools.tests.llm_samples import sample_config_with as _cfg

KEY_ENV = "ATMOFAB_TEST_HTTP_KEY"

_MIXED_CONFIG = (
    "defaults:\n  provider: claude_cli\n  model: opus\n"
    "phases:\n  generate:\n    substeps:\n      generate:\n"
    "        provider: openai_compatible\n"
    "        base_url: http://localhost:8000/v1\n"
    f"        api_key_env: {KEY_ENV}\n"
    "        model: local-coder\n"
)


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False


def _sse_completion(text: str, *, finish_reason: str = "stop",
                    terminated: bool = True) -> str:
    """One OpenAI-dialect event stream delivering `text`, split over two content deltas.

    Two deltas rather than one on purpose: a wiring test that only ever saw a single-frame
    stream would not notice a reader that dropped everything after the first."""
    head, tail = text[: len(text) // 2], text[len(text) // 2:]
    frames = [
        {"model": "local-coder-resolved",
         "choices": [{"delta": {"content": head}, "finish_reason": None}]},
        {"model": "local-coder-resolved",
         "choices": [{"delta": {"content": tail}, "finish_reason": None}]},
    ]
    if terminated:
        frames.append({"model": "local-coder-resolved",
                       "choices": [{"delta": {}, "finish_reason": finish_reason}]})
        frames.append({"model": "local-coder-resolved", "choices": [],
                       "usage": {"prompt_tokens": 5, "completion_tokens": 6,
                                 "completion_tokens_details": {"reasoning_tokens": 4},
                                 "prompt_tokens_details": {"cached_tokens": 2}}})
    out = "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames)
    return out + ("data: [DONE]\n\n" if terminated else "")


class _HttpConductor(_PureFakeConductor):
    """The CLI fake, with its `spawn_leaf` override REMOVED so the real one runs.

    That is the whole point: the real `spawn_leaf` is what decides, from the entry, whether to
    launch a process or call the HTTP transport."""

    def spawn_leaf(self, *args, **kwargs):  # type: ignore[override]
        return wc.Conductor.spawn_leaf(self, *args, **kwargs)


class _HttpServeMixin:
    """The fake endpoint. A mixin because two test classes drive it (issue #209)."""

    def _serve(self, replies: list[dict | str]) -> list[dict]:
        """Install a fake `urlopen` answering `replies` in order; return the captured requests.

        A reply may be a mapping (sent as a completion of its `text`, honouring an optional
        `finish_reason`), or a string (the completion text). Answers as an EVENT STREAM by
        default, because that is what the product default asks for; `{"nonstream": True}` gets
        the buffered JSON body, `{"raw": ...}` an arbitrary one, `{"raise": ...}` an exception.
        `nonstream` only makes sense against an entry carrying `stream: false` — a buffered body
        answering a streaming request is a severed stream, not a buffered exchange.
        A streaming reply may also carry `{"unterminated": True}` — frames with no `[DONE]` and
        no `finish_reason`, i.e. a connection severed mid-answer."""
        captured: list[dict] = []
        pending = list(replies)

        def _open(request, timeout=None):       # noqa: ANN001 - test double
            captured.append(json.loads(request.data.decode("utf-8")))
            # The LAST reply repeats once the script is exhausted, so a test can drive a
            # bounded retry loop without listing every attempt.
            reply = pending.pop(0) if len(pending) > 1 else (pending[0] if pending else "")
            if isinstance(reply, str):
                reply = {"text": reply}
            if isinstance(reply, dict) and reply.get("raise"):
                raise OSError(reply["raise"])
            if isinstance(reply, dict) and "raw" in reply:
                return _FakeResponse(reply["raw"].encode("utf-8"))
            if reply.get("nonstream"):
                body = {
                    "model": "local-coder-resolved",
                    "choices": [{"message": {"content": reply.get("text", "")},
                                 "finish_reason": reply.get("finish_reason", "stop")}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 6},
                }
                return _FakeResponse(json.dumps(body).encode("utf-8"))
            return _FakeResponse(_sse_completion(
                reply.get("text", ""), finish_reason=reply.get("finish_reason", "stop"),
                terminated=not reply.get("unterminated")).encode("utf-8"))

        # `_default_opener`, not `urlopen`: the transport builds a no-redirect opener rather
        # than calling `urlopen` directly (a redirect would forward the API key), so patching
        # `urlopen` would silently stop intercepting anything.
        patcher = patch("tools.llm_http_leaf._default_opener", lambda env=None: _open)
        patcher.start()
        self.addCleanup(patcher.stop)
        return captured


class HttpPureLeafWiringTests(_HttpServeMixin, unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        self.refs = _write_node(self.repo)
        (self.repo / "workspace" / "orchestrations" / "o").mkdir(parents=True, exist_ok=True)
        cfg_path = self.repo / "llm.yaml"
        cfg_path.write_text(_MIXED_CONFIG, encoding="utf-8")
        self.config = lc.load_llm_config(cfg_path)
        key = patch.dict("os.environ", {KEY_ENV: "sk-test"}, clear=False)
        key.start()
        self.addCleanup(key.stop)

    def _conductor(self) -> _HttpConductor:
        # `env` carries the key, as it does in production (`run_workflow` builds the base env
        # from `os.environ`): the transport reads the CONDUCTOR's environment, not the
        # process-global one, so that a run's own credential and proxy routing are what apply.
        c = _HttpConductor(
            repo_root=self.repo, orchestration_id="o", orchestration_agent_run_id="orch",
            env={KEY_ENV: "sk-test"}, llm_config=self.config)
        self._events: list[dict] = []
        c.emit = lambda event, **f: self._events.append({"event": event, **f})  # type: ignore
        return c


    # --- the happy path --------------------------------------------------------------

    def test_an_http_leaf_produces_the_same_artifacts_as_a_cli_one(self) -> None:
        self._serve([json.dumps(_valid_bundle())])
        c = self._conductor()
        outcome = c._run_pure_generate_substep(self.refs, "generate", "generate", None, ())
        self.assertEqual(outcome.status, "pass")
        self.assertEqual(outcome.output_refs, [])       # pure: the HOST writes, after finalize
        base = self.repo / self.refs.source_dir()
        for name in ("codegen_bundle.json", "bundle_meta.json"):
            self.assertTrue((base / name).exists(), msg=name)
        self.assertTrue((base / "src" / f"{_SPEC_ID}_model.f90").exists())
        self.assertTrue((base / "src" / "Makefile").exists())

    def test_the_pure_loop_resolves_its_resume_as_a_pure_launch(self) -> None:
        """The pure loop must tell the resume resolver which home its launch uses.

        A pure leaf is given no private `CLAUDE_CONFIG_DIR`, so its transcript is
        written to — and served from — the operator's `~/.claude`. If this call site
        says `pure=False`, the resolver looks in the orchestration's private home,
        finds nothing, and every pure reuse repair silently goes cold, re-inlining
        `pure_context` on the branch's most expensive substep. Measured: that
        mutation survived a pin placed on the resolver alone, so it is asserted here
        at the CALL SITE, on the value that actually arrives.
        """
        self._serve([json.dumps(_valid_bundle())])
        c = self._conductor()
        seen: list[bool] = []
        c._resolve_reuse_resume = (  # type: ignore[assignment]
            lambda repair, phase, substep, pure=False: seen.append(pure))
        c._run_pure_generate_substep(
            self.refs, "generate", "generate",
            {"repair_strategy": "reuse", "repair_target_agent_run_id": "child-1"}, ())
        self.assertEqual(seen, [True])

    def test_the_provenance_names_the_http_provider_and_its_resolved_model(self) -> None:
        self._serve([json.dumps(_valid_bundle())])
        c = self._conductor()
        c._run_pure_generate_substep(self.refs, "generate", "generate", None, ())
        agent_run = [payload for sub, payload in c.calls
                     if sub == "finalize-child" and "--agent-run-json" in payload]
        row = agent_run[-1]["--agent-run-json"]
        self.assertEqual(row["agent_backend"], "openai_compatible")
        self.assertEqual(row["agent_model"], "local-coder-resolved")

    def test_the_leafs_token_usage_reaches_the_agent_run_row(self) -> None:
        """The wiring nobody had pinned: the transport parsed usage correctly and nothing
        asserted it ever ARRIVED anywhere. It has to reach the durable `agent_runs.jsonl` /
        `agent.result.json` row, because that row is what a cost audit reads — the alternative
        source is a multi-MB raw SSE capture (issue #47)."""
        self._serve([json.dumps(_valid_bundle())])
        c = self._conductor()
        c._run_pure_generate_substep(self.refs, "generate", "generate", None, ())
        row = [payload["--agent-run-json"] for sub, payload in c.calls
               if sub == "finalize-child" and "--agent-run-json" in payload][-1]
        self.assertEqual(row["usage"], {
            "input_tokens": 5, "output_tokens": 6, "reasoning_tokens": 4, "cached_tokens": 2,
            "total_tokens": 11, "usage_source": "http_provider",
            "provider_details": {"completion_tokens_details": {"reasoning_tokens": 4},
                                 "prompt_tokens_details": {"cached_tokens": 2}}})

    def test_the_raw_response_body_is_persisted(self) -> None:
        """What arrives is now an EVENT STREAM, and it is kept as it arrived — framing, the
        `[DONE]` terminator and all. That is also why the file is named `.txt`: a stream is
        never a JSON document, so a `.json` name would make every one of these a workspace
        violation (see the sibling test below)."""
        self._serve([json.dumps(_valid_bundle())])
        c = self._conductor()
        c._run_pure_generate_substep(self.refs, "generate", "generate", None, ())
        launches = self.repo / "workspace" / "orchestrations" / "o" / "launches"
        bodies = sorted(launches.glob("*.http_response.txt"))
        self.assertTrue(bodies)
        persisted = bodies[0].read_text(encoding="utf-8")
        self.assertIn("data: ", persisted)
        self.assertIn("[DONE]", persisted)
        self.assertIn("bundle_schema_version", persisted)

    def test_a_streaming_leaf_produces_the_same_artifacts_as_a_buffered_one(self) -> None:
        """The whole point of the change: the transport differs, the run does not. The bundle a
        streamed answer produces must be byte-identical to the one the buffered path produced,
        or streaming would be a behaviour change dressed as a transport fix."""
        bundle = json.dumps(_valid_bundle())
        written: list[bytes] = []
        for reply in ({"text": bundle, "nonstream": True}, {"text": bundle}):
            self.setUp()                        # a fresh repo, config and key for each transport
            if reply.get("nonstream"):
                # The SERVER's dialect has to match what the ENTRY asked for; a buffered body
                # answering a streaming request is a severed stream, which is a different test.
                self.config = self._opted_out_config()
            self._serve([reply])
            c = self._conductor()
            outcome = c._run_pure_generate_substep(self.refs, "generate", "generate", None, ())
            self.assertEqual(outcome.status, "pass", msg=str(reply))
            written.append(
                (self.repo / self.refs.source_dir() / "codegen_bundle.json").read_bytes())
        self.assertEqual(written[0], written[1])

    def test_a_stream_severed_before_its_terminator_is_a_transport_failure(self) -> None:
        """The correctness risk streaming introduces. A connection cut at 90% leaves a
        plausible-looking partial document; passed through, the validators reject it as
        unparseable and the loops spend repair turns blaming the model for a network fault.
        It has to arrive as `pure_transport` — the category that is RETRIED, not repaired."""
        self._serve([{"text": json.dumps(_valid_bundle()), "unterminated": True}])
        c = self._conductor()
        c._sleep_backoff = lambda _s: None                 # type: ignore[assignment]
        outcome = c._run_pure_generate_substep(self.refs, "generate", "generate", None, ())
        self.assertEqual(outcome.status, "fail")
        errors = [e for e in self._events if e["event"] == "http_leaf_transport_error"]
        self.assertTrue(errors)
        self.assertIn("stream interrupted", errors[0]["error"])

    def _opted_out_config(self) -> lc.LlmConfig:
        """`_MIXED_CONFIG` with `stream: false` on the HTTP generate leaf."""
        cfg_path = self.repo / "opted-out.yaml"
        cfg_path.write_text(_MIXED_CONFIG.rstrip("\n") + "\n        stream: false\n",
                            encoding="utf-8")
        return lc.load_llm_config(cfg_path)

    def test_an_entry_with_stream_false_sends_the_buffered_request(self) -> None:
        """The config key reaching the wire, end to end: the operator's escape hatch for an
        endpoint that cannot speak SSE has to actually change what is posted."""
        self.config = self._opted_out_config()
        self.assertIs(self.config.entry_for("generate", "generate").stream, False)
        sent = self._serve([{"text": json.dumps(_valid_bundle()), "nonstream": True}])
        c = self._conductor()
        outcome = c._run_pure_generate_substep(self.refs, "generate", "generate", None, ())
        self.assertEqual(outcome.status, "pass")
        self.assertNotIn("stream", sent[0])
        self.assertNotIn("stream_options", sent[0])

    def test_a_non_json_body_is_persisted_without_becoming_a_workspace_violation(self) -> None:
        """The body this file most needs to keep is the one that is NOT JSON — an HTML error
        page from a proxy. Under a `.json` name, `validate_workspace_root`, which parses every
        `workspace/**/*.json`, turns that evidence into an `invalid json` violation that
        outlives the transport failure and can block a later resume."""
        from tools.validate_workspace_root import _scan_json_for_violations
        self._serve([{"raw": "<html><title>502 Bad Gateway</title></html>"}])
        c = self._conductor()
        outcome = c._run_pure_generate_substep(self.refs, "generate", "generate", None, ())
        self.assertEqual(outcome.status, "fail")          # transport, as it should be
        launches = self.repo / "workspace" / "orchestrations" / "o" / "launches"
        bodies = sorted(launches.glob("*.http_response.*"))
        self.assertTrue(bodies)
        self.assertIn("502 Bad Gateway", bodies[0].read_text(encoding="utf-8"))
        for path in launches.rglob("*.json"):
            self.assertEqual(_scan_json_for_violations(path), [], msg=str(path))

    def test_a_key_echoed_into_the_answer_is_not_persisted(self) -> None:
        """The transport redacts every provider string it returns, but the model's ANSWER is
        also written to disk (`_persist_leaf_output`) — and it cannot be redacted in the value,
        because the validators parse it and a key that is a common substring would corrupt a
        legitimate document. The split is at persistence."""
        bundle = _valid_bundle()
        bundle["files"][0]["content"] = (
            bundle["files"][0]["content"] + "\n! leaked sk-test\n")
        self._serve([json.dumps(bundle)])
        c = self._conductor()
        c._run_pure_generate_substep(self.refs, "generate", "generate", None, ())
        dialogs = self.repo / "workspace" / "orchestrations" / "o" / "agents"
        logs = list(dialogs.rglob("*.stdout.log"))
        self.assertTrue(logs)
        for log in logs:
            body = log.read_text(encoding="utf-8")
            self.assertNotIn("sk-test", body, msg=str(log))
            self.assertIn("[redacted-api-key]", body)
        # ...and the validators saw the TRUE document: the bundle was accepted and written.
        written = (self.repo / self.refs.source_dir() / "src" / f"{_SPEC_ID}_model.f90"
                   ).read_text(encoding="utf-8")
        self.assertIn("sk-test", written)

    def test_the_key_comes_from_the_conductors_environment(self) -> None:
        """Every spawned leaf receives the conductor's environment; an HTTP leaf must read the
        same one. Reading the process-global environment takes a credential the run did not
        choose, or misses one it did."""
        self._serve([json.dumps(_valid_bundle())])
        c = self._conductor()
        c.env = {}                                    # the run supplies no key
        outcome = c._run_pure_generate_substep(self.refs, "generate", "generate", None, ())
        self.assertEqual(outcome.status, "fail")
        self.assertIn("missing_api_key",
                      " ".join(e.get("error", "") for e in self._events))

    def test_no_process_is_spawned(self) -> None:
        self._serve([json.dumps(_valid_bundle())])
        c = self._conductor()
        with patch("subprocess.Popen", side_effect=AssertionError("a process was spawned")):
            outcome = c._run_pure_generate_substep(self.refs, "generate", "generate", None, ())
        self.assertEqual(outcome.status, "pass")

    # --- the repair loop -------------------------------------------------------------

    def test_a_repair_turn_replays_the_conversation_in_memory(self) -> None:
        """No session to reopen, so the prior turns ARE the resume: the second request must
        carry the first answer and the critique of it."""
        sent = self._serve(["not a json document at all", json.dumps(_valid_bundle())])
        c = self._conductor()
        outcome = c._run_pure_generate_substep(self.refs, "generate", "generate", None, ())
        self.assertEqual(outcome.status, "pass")
        self.assertEqual(len(sent), 2)
        roles = [m["role"] for m in sent[1]["messages"]]
        self.assertEqual(roles[:4], ["system", "user", "assistant", "user"])
        self.assertEqual(sent[1]["messages"][2]["content"], "not a json document at all")

    def test_each_substep_run_starts_a_fresh_conversation(self) -> None:
        sent = self._serve([json.dumps(_valid_bundle()), json.dumps(_valid_bundle())])
        c = self._conductor()
        c._run_pure_generate_substep(self.refs, "generate", "generate", None, ())
        c._run_pure_generate_substep(self.refs, "generate", "generate", None, ())
        self.assertEqual([len(r["messages"]) for r in sent], [2, 2])

    def _attempt_categories(self) -> list[str]:
        """The per-attempt failure categories the loop emitted, read from the events it
        published — `bundle_meta.json` records only the TERMINAL one."""
        return [e["failure_category"] for e in self._events
                if e.get("event") == "pure_bundle_attempt_failed"]

    def test_a_provider_reported_truncation_is_classified_as_truncated(self) -> None:
        """The provider's own signal must decide, not the extractor's inference: this reply is
        also unparseable, so a test that only counted attempts stayed green with the whole
        `response_truncated` plumbing severed."""
        sent = self._serve([
            {"text": '{"partial": ', "finish_reason": "length"},
            {"text": json.dumps(_valid_bundle())},
        ])
        c = self._conductor()
        outcome = c._run_pure_generate_substep(self.refs, "generate", "generate", None, ())
        self.assertEqual(outcome.status, "pass")
        self.assertEqual(len(sent), 2)
        self.assertEqual(self._attempt_categories(), ["pure_response_truncated"])

    def test_a_truncated_reply_that_PARSES_is_still_rejected(self) -> None:
        """The case only the provider's signal can catch: a cut-off answer that happens to be
        valid JSON. Without the signal the host would accept a partial bundle as complete."""
        bundle = _valid_bundle()
        truncated = dict(bundle)
        truncated["files"] = truncated["files"][:1]
        sent = self._serve([
            {"text": json.dumps(truncated), "finish_reason": "length"},
            {"text": json.dumps(bundle)},
        ])
        c = self._conductor()
        outcome = c._run_pure_generate_substep(self.refs, "generate", "generate", None, ())
        self.assertEqual(outcome.status, "pass")
        self.assertEqual(len(sent), 2)
        self.assertEqual(self._attempt_categories(), ["pure_response_truncated"])

    def test_a_repair_turn_does_not_re_send_the_whole_context(self) -> None:
        """The replay already carries the prior prompt and answer, so the repair renders the
        WARM (slim) turn. Rendering the cold fallback on top of it shipped the node's whole
        closed context once per attempt and each prior bundle twice."""
        self._serve(["not a json document at all", json.dumps(_valid_bundle())])
        c = self._conductor()
        c._run_pure_generate_substep(self.refs, "generate", "generate", None, ())
        requests = [payload["--request-json"] for sub, payload in c.calls
                    if sub == "record-launch" and "--request-json" in payload]
        self.assertEqual(len(requests), 2)
        self.assertIsNotNone(requests[0].get("pure_context"))
        self.assertIsNone(requests[1].get("pure_context"))
        self.assertNotIn("prior_document", requests[1])
        self.assertTrue(requests[1].get("warm_resume"))

    def test_a_dead_endpoint_is_retried_and_then_is_a_transport_failure(self) -> None:
        """A refused connection is a TRANSIENT tag, so the loop re-launches it within the
        bounded budget before failing closed — one dropped connection must not lose a run that
        has already paid for every earlier phase."""
        sent = self._serve([{"raise": "Connection refused"}])
        c = self._conductor()
        slept: list = []
        c._sleep_backoff = slept.append                    # type: ignore[assignment]
        outcome = c._run_pure_generate_substep(self.refs, "generate", "generate", None, ())
        self.assertEqual(outcome.status, "fail")
        self.assertIsNotNone(outcome.infra_error)
        self.assertEqual(len(sent), wc.MAX_LEAF_TRANSIENT_RETRIES + 1)
        self.assertEqual(len(slept), wc.MAX_LEAF_TRANSIENT_RETRIES)
        self.assertEqual([e["event"] for e in self._events].count("leaf_transient_retry"),
                         wc.MAX_LEAF_TRANSIENT_RETRIES)
        meta = json.loads((self.repo / self.refs.source_dir() / "bundle_meta.json")
                          .read_text(encoding="utf-8"))
        self.assertEqual(meta["failure_category"], "pure_transport")

    def test_a_transient_failure_that_clears_lets_the_substep_pass(self) -> None:
        """The point of the retry: a 429 that clears must not cost the run."""
        sent = self._serve([{"raise": "Connection refused"}, json.dumps(_valid_bundle())])
        c = self._conductor()
        c._sleep_backoff = lambda _s: None                 # type: ignore[assignment]
        outcome = c._run_pure_generate_substep(self.refs, "generate", "generate", None, ())
        self.assertEqual(outcome.status, "pass")
        self.assertEqual(len(sent), 2)
        # A retry is not a repair turn: the second attempt is a fresh cold launch, not a
        # slim repair, so it carries the full context and no findings.
        requests = [payload["--request-json"] for sub, payload in c.calls
                    if sub == "record-launch" and "--request-json" in payload]
        self.assertIsNotNone(requests[-1].get("pure_context"))
        self.assertNotIn("repair", requests[-1])

    def test_a_client_error_is_not_retried(self) -> None:
        """A 4xx is a deterministic misconfiguration; retrying it three times would report it
        as a provider outage the operator should wait out."""
        import urllib.error

        def _open(request, timeout=None):                  # noqa: ANN001 - test double
            raise urllib.error.HTTPError(
                "http://x", 400, "Bad Request", {}, io.BytesIO(b'{"error":"max_tokens"}'))

        patcher = patch("tools.llm_http_leaf._default_opener", lambda env=None: _open)
        patcher.start()
        self.addCleanup(patcher.stop)
        c = self._conductor()
        c._sleep_backoff = lambda _s: None                 # type: ignore[assignment]
        outcome = c._run_pure_generate_substep(self.refs, "generate", "generate", None, ())
        self.assertEqual(outcome.status, "fail")
        self.assertEqual([e["event"] for e in self._events].count("leaf_transient_retry"), 0)

    # --- the pure-only rule, at run time ---------------------------------------------

    def test_a_pure_only_provider_on_a_shapeless_node_fails_closed(self) -> None:
        """Config validation cannot see node shape. A node with no pure path would otherwise
        take the shared agentic loop with a provider that cannot run it.

        Since issue #169 NO in-tree node is such a node — the `infrastructure` harness self-test
        was the last one and is now the `harness` bundle shape — so the subject is built by
        stubbing the predicate the shape reader consults first. The refusal text is checked
        because an operator acts on it: it used to say "it is not an M3c node", which stopped
        being the deciding property when the second shape landed."""
        c = self._conductor()
        c._conductor_authors_makefile = lambda refs: False   # type: ignore[assignment]
        outcome = c.run_substep(self.refs, "generate", "generate")
        self.assertEqual(outcome.status, "fail")
        assert outcome.infra_error is not None
        self.assertEqual(outcome.infra_error[0], "pure_only_provider_on_agentic_path")
        self.assertIn("no CodegenBundle shape", outcome.infra_error[1])
        self.assertNotIn("M3c", outcome.infra_error[1])
        # ...and it names BOTH inputs the predicate reads. A round-3 reviewer caught the first
        # replacement sending an operator to the IR alone, when the node_key can decide.
        self.assertIn("node_key", outcome.infra_error[1])

    def test_an_agentic_provider_on_a_shapeless_node_is_untouched(self) -> None:
        c = _HttpConductor(
            repo_root=self.repo, orchestration_id="o", orchestration_agent_run_id="orch",
            env={KEY_ENV: "sk-test"}, llm_config=_cfg("claude", agent_model="opus"))
        c._conductor_authors_makefile = lambda refs: False   # type: ignore[assignment]
        self.assertFalse(c._pure_leaf_substep(self.refs, "generate", "generate"))
        entry = c.entry_for("generate", "generate")
        self.assertTrue(entry.supports(lc.CAP_AGENTIC))      # so the guard does not fire

    # --- the mixed configuration itself ----------------------------------------------

    def test_the_same_run_carries_two_providers(self) -> None:
        """Acceptance 2, at the level this file can assert it without a billed run: one
        conductor, one config, two providers, each leaf resolving to its own."""
        c = self._conductor()
        self.assertEqual(c.entry_for("generate", "generate").backend_token, "openai_compatible")
        self.assertEqual(c.entry_for("generate", "verify").backend_token, "claude")
        self.assertEqual(c.entry_for("validate", "judge").backend_token, "claude")
        self.assertEqual(c.entry_for(None, None).backend_token, "claude")
        self.assertEqual(
            {row["backend"] for row in self.config.provenance_map().values()},
            {"claude", "openai_compatible"})

    def test_an_http_repair_is_warm_only_while_the_in_memory_history_exists(self) -> None:
        """The provider has no session, so the replay is the reopen — and it lives exactly as
        long as one substep run. Before the first turn, and after the reset a fresh run
        performs, there is nothing to resume."""
        c = self._conductor()
        entry = c.entry_for("generate", "generate")
        self.assertFalse(entry.supports(lc.CAP_WARM_RESUME))     # no session, ever
        self.assertFalse(c._pure_session_resumable("s", entry, "generate", "generate"))
        self._serve([json.dumps(_valid_bundle())])
        c._run_pure_generate_substep(self.refs, "generate", "generate", None, ())
        self.assertTrue(c._pure_session_resumable("s", entry, "generate", "generate"))
        c.reset_http_history("generate", "generate")
        self.assertFalse(c._pure_session_resumable("s", entry, "generate", "generate"))

    def test_the_transport_owns_its_own_ceilings(self) -> None:
        """The conductor passed the CLI leaf's 128000 and the process cap, which made the
        transport's own defaults unreachable and asked every endpoint for a ceiling it rejects
        as a client error — non-retryable, on the first attempt."""
        import tools.llm_http_leaf as hl
        sent = self._serve([json.dumps(_valid_bundle())])
        c = self._conductor()
        c._run_pure_generate_substep(self.refs, "generate", "generate", None, ())
        self.assertEqual(sent[0]["max_tokens"], hl.DEFAULT_MAX_OUTPUT_TOKENS)
        self.assertNotEqual(sent[0]["max_tokens"], wc.LEAF_MAX_OUTPUT_TOKENS)

    def test_an_entrys_own_ceilings_reach_the_request(self) -> None:
        cfg = self.repo / "sized.yaml"
        cfg.write_text(_MIXED_CONFIG + "        max_output_tokens: 4096\n"
                                       "        timeout_s: 30\n", encoding="utf-8")
        self.config = lc.load_llm_config(cfg)
        sent = self._serve([json.dumps(_valid_bundle())])
        c = self._conductor()
        c._run_pure_generate_substep(self.refs, "generate", "generate", None, ())
        self.assertEqual(sent[0]["max_tokens"], 4096)

    def test_the_launch_argv_builder_refuses_an_http_entry(self) -> None:
        """Defense in depth: nothing should reach `leaf_command` with an HTTP entry, and if
        anything did, building a CLI argv out of it is the wrong recovery."""
        c = self._conductor()
        with self.assertRaises(ValueError) as ctx:
            c.leaf_command(c.entry_for("generate", "generate"))
        self.assertIn("launches no CLI leaf", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()


# ======================================================================================
# The cold outer reopen (issue #209)
# ======================================================================================
class _RenderingHttpConductor(_RenderingFakeConductor, _HttpConductor):
    """The rendering fake AND the real `spawn_leaf`, in one conductor.

    Neither alone can see this: `_HttpConductor`'s record-launch returns the literal `"PROMPT"`,
    so nothing that reaches the HTTP body is asserted; `_RenderingFakeConductor`'s `spawn_leaf`
    never dispatches to the transport (and drops `entry`, which is what the dispatch reads). The
    MRO puts the real prompt pipeline on record-launch and the real transport on the spawn.
    """

    def spawn_leaf(self, prompt_text, child_env, entry=None, **kwargs):  # type: ignore[override]
        self.prompts = getattr(self, "prompts", [])
        self.prompts.append(prompt_text)
        return _HttpConductor.spawn_leaf(
            self, prompt_text, child_env, entry=entry, **kwargs)


_COMPILE_HTTP_CONFIG = (
    "defaults:\n  provider: claude_cli\n  model: opus\n"
    "phases:\n  compile:\n    substeps:\n      generate:\n"
    "        provider: openai_compatible\n"
    "        base_url: http://localhost:8000/v1\n"
    f"        api_key_env: {KEY_ENV}\n"
    "        model: local-coder\n"
)

_PRIOR_ARID = "prior-arid"
_PRIOR_IR_MARKER = "prior_ir_marker_209"
_EXCERPT = "compile_static_violation: step 3 lowers no local operation"


class ColdOuterReopenTests(_HttpServeMixin, unittest.TestCase):
    """An outer `reuse` reopen on a provider that holds no session (issue #209).

    The reopening gate's findings and the failed attempt's own document must reach the leaf as a
    COLD REPAIR turn — the same repair template a warm reuse renders, with the context re-inlined
    — rather than as a full launch prompt that re-derives the substep from scratch and drops the
    diagnosis. These run over HTTP because `openai_compatible` is the provider that declares no
    `warm_resume` at all; the sibling case (a CLI provider whose session is gone) is pinned in
    `test_pure_leaf_producer.py`.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        (self.repo / "workspace" / "orchestrations" / "o" / "launches").mkdir(
            parents=True, exist_ok=True)
        key = patch.dict("os.environ", {KEY_ENV: "sk-test"}, clear=False)
        key.start()
        self.addCleanup(key.stop)

    def _conductor(self, config_text: str) -> _RenderingHttpConductor:
        cfg = self.repo / "llm.yaml"
        cfg.write_text(config_text, encoding="utf-8")
        c = _RenderingHttpConductor(
            repo_root=self.repo, orchestration_id="o", orchestration_agent_run_id="orch",
            env={KEY_ENV: "sk-test"}, llm_config=lc.load_llm_config(cfg))
        c.exemplar_value = None
        self._events: list[dict] = []
        c.emit = lambda event, **f: self._events.append({"event": event, **f})  # type: ignore
        return c

    def _write_launch_record(self, payload: dict) -> None:
        """The repair target's OWN launch record — the only non-heuristic name of the directory
        that attempt wrote into, since `_ensure_fresh_producer_id` has rotated `refs` past it."""
        (self.repo / "workspace" / "orchestrations" / "o" / "launches"
         / f"{_PRIOR_ARID}.request.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8")

    @staticmethod
    def _repair(findings: str | None = _EXCERPT,
                target: str = _PRIOR_ARID) -> dict[str, str]:
        rep = {"issue_severity": "major", "repair_strategy": "reuse",
               "repair_target_agent_run_id": target,
               "repair_reason": "compile_static_compile_static_violation"}
        if findings is not None:
            rep["repair_findings"] = findings
        return rep

    def _event(self, name: str) -> dict | None:
        rows = [row for row in self._events if row["event"] == name]
        return rows[-1] if rows else None

    # --- compile: the IR half ---------------------------------------------------------

    def _compile_fixture(self, *, stage_prior: bool = True):
        from tools.tests.test_pure_leaf_compile import _valid_ir, _write_compile_node
        refs = _write_compile_node(self.repo)
        if stage_prior:
            # DELIBERATELY not `refs.ir_ref`: production reaches this directory only through the
            # launch record, because the phase entry has already rotated the id. A fixture that
            # staged the prior IR where `refs` points would pass against a restore that ignored
            # the record entirely.
            prior_ref = f"{refs.ir_ref.rsplit('/', 1)[0]}/advdiff-uc2_20260101_000"
            prior_ir = _valid_ir()
            prior_ir["meta"]["notes"] = _PRIOR_IR_MARKER
            prior_dir = self.repo / prior_ref
            prior_dir.mkdir(parents=True, exist_ok=True)
            (prior_dir / "spec.ir.yaml").write_text(
                yaml.safe_dump(prior_ir, sort_keys=False), encoding="utf-8")
            self._write_launch_record({"ir_ref": prior_ref,
                                       "pipeline_ref": refs.pipeline_ref,
                                       "agent_run_id": _PRIOR_ARID})
        return refs

    def test_an_outer_reuse_reopen_on_an_http_compile_entry_carries_the_gate_excerpt_cold(
            self) -> None:
        from tools.tests.test_pure_leaf_compile import _doc
        refs = self._compile_fixture()
        sent = self._serve([json.dumps(_doc())])
        c = self._conductor(_COMPILE_HTTP_CONFIG)
        outcome = c.run_substep(refs, "compile", "generate",
                                repair=self._repair())
        self.assertEqual(outcome.status, "pass")
        self.assertEqual(outcome.attempts, 1)
        # What the provider actually received on the FIRST turn.
        body = sent[0]["messages"][-1]["content"]
        self.assertIn(_EXCERPT, body)
        self.assertIn("Your prior document under repair", body)
        self.assertIn(_PRIOR_IR_MARKER, body)
        self.assertIn("Authoring rules", body)        # lifted from the compile launch template
        # ... and the request that produced it is a cold reuse repair, not a launch.
        req = c.requests[0]
        self.assertEqual(req["repair_strategy"], "reuse")
        self.assertEqual(req["repair_findings"], _EXCERPT)
        self.assertIn(_PRIOR_IR_MARKER, req["prior_document"])
        self.assertFalse(req.get("warm_resume"))
        self.assertTrue(req.get("pure_context"))
        self.assertNotIn("exemplar", req)             # the repair template has no slot
        self.assertIsNotNone(self._event("resume_session_unavailable"))
        self.assertEqual(
            {k: v for k, v in (self._event("pure_reopen_cold") or {}).items()
             if k in ("findings_carried", "prior_document_carried", "target")},
            {"findings_carried": True, "prior_document_carried": True,
             "target": _PRIOR_ARID})

    def test_the_prior_document_is_the_producers_two_key_ir_document(self) -> None:
        """The artifact holds only the `ir` half; the document the leaf is asked to correct is
        the one it returned, so the host reconstructs the other half rather than handing back a
        shape the output contract rejects."""
        from tools.tests.test_pure_leaf_compile import _doc
        refs = self._compile_fixture()
        self._serve([json.dumps(_doc())])
        c = self._conductor(_COMPILE_HTTP_CONFIG)
        c.run_substep(refs, "compile", "generate", repair=self._repair())
        prior = json.loads(c.requests[0]["prior_document"])
        self.assertEqual(set(prior), {"ir", "last_fail_reason"})
        self.assertIsNone(prior["last_fail_reason"])
        self.assertEqual(prior["ir"]["meta"]["notes"], _PRIOR_IR_MARKER)

    def test_a_cold_outer_reopen_without_a_prior_artifact_still_carries_the_findings(
            self) -> None:
        """The bundle- / IR-document repair-exhaustion routes reopen with NO accepted artifact.
        The findings are the whole carry-forward there, and must not be dropped with it."""
        from tools.tests.test_pure_leaf_compile import _doc
        refs = self._compile_fixture(stage_prior=False)
        sent = self._serve([json.dumps(_doc())])
        c = self._conductor(_COMPILE_HTTP_CONFIG)
        outcome = c.run_substep(refs, "compile", "generate", repair=self._repair())
        self.assertEqual(outcome.status, "pass")
        self.assertIn(_EXCERPT, sent[0]["messages"][-1]["content"])
        self.assertNotIn("Your prior document under repair",
                         sent[0]["messages"][-1]["content"])
        self.assertNotIn("prior_document", c.requests[0])
        self.assertEqual(self._event("pure_reopen_cold")["prior_document_carried"], False)
        self.assertEqual(self._event("pure_reopen_cold")["findings_carried"], True)

    def test_a_cold_outer_reopen_with_an_unusable_target_renders_the_launch_prompt(self) -> None:
        """`_validate_launch_request_payload` refuses a reuse repair whose target is `"none"`, so
        that reopen keeps today's cold LAUNCH. The one route the findings still cannot ride.

        The prior IR is staged and its launch record written, but under the arid `prior-arid`
        while the reopen names `"none"` — which is the production shape: a target spelled `"none"`
        is the payload-absent spelling and no launch is ever recorded under it. So the seed's
        `usable` guard is what stops the carry, and NOTHING is resolved to be dropped later. An
        earlier version of this docstring claimed the opposite ("the prior artifact IS on disk
        here"), and the dead branch it justified is gone.
        """
        from tools.tests.test_pure_leaf_compile import _doc
        refs = self._compile_fixture()
        sent = self._serve([json.dumps(_doc())])
        c = self._conductor(_COMPILE_HTTP_CONFIG)
        outcome = c.run_substep(refs, "compile", "generate",
                                repair=self._repair(target="none"))
        self.assertEqual(outcome.status, "pass")
        self.assertNotIn(_EXCERPT, sent[0]["messages"][-1]["content"])
        # The request carries the payload-absent spelling (`"none"`), not a reuse repair.
        self.assertEqual(c.requests[0]["repair_strategy"], "none")
        self.assertNotIn("prior_document", c.requests[0])
        # Both carries are false, and the event must say so rather than reporting the artifact
        # that happens to sit on disk under a different arid.
        self.assertEqual(self._event("pure_reopen_cold"),
                         {"event": "pure_reopen_cold", "node_key": refs.node_key,
                          "substep": "generate", "target": "none",
                          "findings_carried": False, "prior_document_carried": False})

    def test_an_outer_reopen_without_findings_still_renders_the_launch_prompt(self) -> None:
        """The seed's other guard: no excerpt, nothing to repair, so the reopen is a launch and
        no `pure_reopen_cold` is emitted at all."""
        from tools.tests.test_pure_leaf_compile import _doc
        refs = self._compile_fixture()
        self._serve([json.dumps(_doc())])
        c = self._conductor(_COMPILE_HTTP_CONFIG)
        c.run_substep(refs, "compile", "generate", repair=self._repair(findings=None))
        self.assertEqual(c.requests[0]["repair_strategy"], "none")
        self.assertNotIn("prior_document", c.requests[0])
        self.assertIsNone(self._event("pure_reopen_cold"))

    def test_an_unparseable_prior_ir_degrades_instead_of_raising(self) -> None:
        """`_pure_ir_prior_document` promises it NEVER raises, and the seed that calls it runs
        OUTSIDE the loop's context-assembly guard — so a raise there does not fail the substep
        closed, it takes the conductor down mid-run with no `step_result.json` and nothing for a
        `--resume` to pick up. The docstring names the reachable cause itself: an IR written by an
        AGENTIC leaf (`llm.yaml` changed across a `--resume`) carries no round-trip guarantee.

        The probe is invalid YAML rather than a missing file, because a missing file is already
        covered above and exercises only the `record is None` arm.
        """
        from tools.tests.test_pure_leaf_compile import _doc
        refs = self._compile_fixture()
        prior_ref = f"{refs.ir_ref.rsplit('/', 1)[0]}/advdiff-uc2_20260101_000"
        (self.repo / prior_ref / "spec.ir.yaml").write_text(
            "meta:\n\tspec_id: tab-indented, which YAML refuses\n", encoding="utf-8")
        sent = self._serve([json.dumps(_doc())])
        c = self._conductor(_COMPILE_HTTP_CONFIG)
        outcome = c.run_substep(refs, "compile", "generate", repair=self._repair())
        self.assertEqual(outcome.status, "pass")
        # The findings still ride; only the document is lost.
        self.assertIn(_EXCERPT, sent[0]["messages"][-1]["content"])
        self.assertNotIn("prior_document", c.requests[0])
        self.assertEqual(self._event("pure_reopen_cold")["prior_document_carried"], False)

    def test_an_undecodable_prior_bundle_degrades_instead_of_raising(self) -> None:
        """The bundle half of the row above. `codegen_bundle.json` is host-written as UTF-8, so
        undecodable bytes mean a truncated or externally-damaged file — and `UnicodeError` is not
        an `OSError`, which is exactly the narrowing that would turn this into a crash."""
        refs = _write_node(self.repo)
        prior_dir = self.repo / refs.source_dir("s_20260101_000")
        prior_dir.mkdir(parents=True, exist_ok=True)
        (prior_dir / "codegen_bundle.json").write_bytes(b'{"files": "\xff\xfe not utf-8"}')
        self._write_launch_record({"ir_ref": refs.ir_ref, "pipeline_ref": refs.pipeline_ref,
                                   "source_id": "s_20260101_000",
                                   "agent_run_id": _PRIOR_ARID})
        sent = self._serve([json.dumps(_valid_bundle())])
        c = self._conductor(_MIXED_CONFIG)
        outcome = c.run_substep(refs, "generate", "generate", repair=self._repair())
        self.assertEqual(outcome.status, "pass")
        self.assertIn(_EXCERPT, sent[0]["messages"][-1]["content"])
        self.assertNotIn("prior_document", c.requests[0])
        self.assertEqual(self._event("pure_reopen_cold")["prior_document_carried"], False)

    # --- generate: the bundle half ----------------------------------------------------

    def test_an_outer_reuse_reopen_on_an_http_generate_entry_carries_the_prior_bundle(
            self) -> None:
        refs = _write_node(self.repo)
        prior_bundle = _valid_bundle()
        prior_bundle["files"][0]["content"] = (
            prior_bundle["files"][0]["content"] + "\n! " + _PRIOR_IR_MARKER + "\n")
        prior_dir = self.repo / refs.source_dir("s_20260101_000")
        prior_dir.mkdir(parents=True, exist_ok=True)
        (prior_dir / "codegen_bundle.json").write_text(
            json.dumps(prior_bundle, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        self._write_launch_record({"ir_ref": refs.ir_ref, "pipeline_ref": refs.pipeline_ref,
                                   "source_id": "s_20260101_000",
                                   "agent_run_id": _PRIOR_ARID})
        sent = self._serve([json.dumps(_valid_bundle())])
        c = self._conductor(_MIXED_CONFIG)
        outcome = c.run_substep(refs, "generate", "generate", repair=self._repair())
        self.assertEqual(outcome.status, "pass")
        body = sent[0]["messages"][-1]["content"]
        self.assertIn(_EXCERPT, body)
        self.assertIn(_PRIOR_IR_MARKER, body)
        self.assertIn(_PRIOR_IR_MARKER, c.requests[0]["prior_document"])
        self.assertFalse(c.requests[0].get("warm_resume"))
        self.assertEqual(self._event("pure_reopen_cold")["prior_document_carried"], True)

    def test_the_first_cold_turn_is_followed_by_a_warm_http_repair(self) -> None:
        """The cold seed must not make the WHOLE run cold: the in-memory history the first turn
        left behind is the reopen for the second, exactly as an unseeded run's is."""
        bad = _valid_bundle()
        del bad["capability_requirements"]
        refs = _write_node(self.repo)
        self._write_launch_record({"ir_ref": refs.ir_ref, "pipeline_ref": refs.pipeline_ref,
                                   "source_id": "s_20260101_000",
                                   "agent_run_id": _PRIOR_ARID})
        sent = self._serve([json.dumps(bad), json.dumps(_valid_bundle())])
        c = self._conductor(_MIXED_CONFIG)
        outcome = c.run_substep(refs, "generate", "generate", repair=self._repair())
        self.assertEqual(outcome.status, "pass")
        self.assertEqual(outcome.attempts, 2)
        # Turn 0 is the cold repair; turn 1 replays it, its reply, and a SLIM repair on top.
        self.assertEqual(len(sent[1]["messages"]), len(sent[0]["messages"]) + 2)
        self.assertTrue(c.requests[1].get("warm_resume"))
        self.assertNotIn("pure_context", c.requests[1])
