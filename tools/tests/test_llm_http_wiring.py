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

import tools.llm_config as lc
import tools.workflow_conductor as wc

from tools.tests.test_pure_leaf_producer import (
    _SPEC_ID,
    _PureFakeConductor,
    _valid_bundle,
    _write_node,
)

KEY_ENV = "METDSL_TEST_HTTP_KEY"

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


class _HttpConductor(_PureFakeConductor):
    """The CLI fake, with its `spawn_leaf` override REMOVED so the real one runs.

    That is the whole point: the real `spawn_leaf` is what decides, from the entry, whether to
    launch a process or call the HTTP transport."""

    def spawn_leaf(self, *args, **kwargs):  # type: ignore[override]
        return wc.Conductor.spawn_leaf(self, *args, **kwargs)


class HttpPureLeafWiringTests(unittest.TestCase):
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

    def _serve(self, replies: list[dict | str]) -> list[dict]:
        """Install a fake `urlopen` answering `replies` in order; return the captured requests.

        A reply may be a mapping (sent as an OpenAI-shaped completion of its `text`, honouring
        an optional `finish_reason`), or a string (the completion text)."""
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
            body = {
                "model": "local-coder-resolved",
                "choices": [{"message": {"content": reply.get("text", "")},
                             "finish_reason": reply.get("finish_reason", "stop")}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 6},
            }
            return _FakeResponse(json.dumps(body).encode("utf-8"))

        # `_default_opener`, not `urlopen`: the transport builds a no-redirect opener rather
        # than calling `urlopen` directly (a redirect would forward the API key), so patching
        # `urlopen` would silently stop intercepting anything.
        patcher = patch("tools.llm_http_leaf._default_opener", lambda env=None: _open)
        patcher.start()
        self.addCleanup(patcher.stop)
        return captured

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

    def test_the_provenance_names_the_http_provider_and_its_resolved_model(self) -> None:
        self._serve([json.dumps(_valid_bundle())])
        c = self._conductor()
        c._run_pure_generate_substep(self.refs, "generate", "generate", None, ())
        agent_run = [payload for sub, payload in c.calls
                     if sub == "finalize-child" and "--agent-run-json" in payload]
        row = agent_run[-1]["--agent-run-json"]
        self.assertEqual(row["agent_backend"], "openai_compatible")
        self.assertEqual(row["agent_model"], "local-coder-resolved")

    def test_the_raw_response_body_is_persisted(self) -> None:
        self._serve([json.dumps(_valid_bundle())])
        c = self._conductor()
        c._run_pure_generate_substep(self.refs, "generate", "generate", None, ())
        launches = self.repo / "workspace" / "orchestrations" / "o" / "launches"
        bodies = sorted(launches.glob("*.http_response.txt"))
        self.assertTrue(bodies)
        self.assertIn("choices", json.loads(bodies[0].read_text(encoding="utf-8")))

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

    def test_a_pure_only_provider_on_a_non_m3c_node_fails_closed(self) -> None:
        """Config validation cannot see node shape. A node with no pure path would otherwise
        take the shared agentic loop with a provider that cannot run it. The live non-M3c node
        is the `infrastructure` harness self-test, which authors its own runner."""
        c = self._conductor()
        c._conductor_authors_makefile = lambda refs: False   # type: ignore[assignment]
        outcome = c.run_substep(self.refs, "generate", "generate")
        self.assertEqual(outcome.status, "fail")
        assert outcome.infra_error is not None
        self.assertEqual(outcome.infra_error[0], "pure_only_provider_on_agentic_path")
        self.assertIn("not an M3c node", outcome.infra_error[1])

    def test_an_agentic_provider_on_a_non_m3c_node_is_untouched(self) -> None:
        c = _HttpConductor(
            repo_root=self.repo, orchestration_id="o", orchestration_agent_run_id="orch",
            env={KEY_ENV: "sk-test"}, backend="claude", agent_model="opus")
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
