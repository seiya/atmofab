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
        c = _HttpConductor(
            repo_root=self.repo, orchestration_id="o", orchestration_agent_run_id="orch",
            env={}, llm_config=self.config)
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
            reply = pending.pop(0) if pending else pending
            if isinstance(reply, str):
                reply = {"text": reply}
            if isinstance(reply, dict) and reply.get("raise"):
                raise OSError(reply["raise"])
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
        patcher = patch("tools.llm_http_leaf._default_opener", lambda: _open)
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
        bodies = sorted(launches.glob("*.http_response.json"))
        self.assertTrue(bodies)
        self.assertIn("choices", json.loads(bodies[0].read_text(encoding="utf-8")))

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

    def test_a_dead_endpoint_is_a_transport_failure_not_a_content_one(self) -> None:
        self._serve([{"raise": "Connection refused"}])
        c = self._conductor()
        outcome = c._run_pure_generate_substep(self.refs, "generate", "generate", None, ())
        self.assertEqual(outcome.status, "fail")
        self.assertIsNotNone(outcome.infra_error)
        meta = json.loads((self.repo / self.refs.source_dir() / "bundle_meta.json")
                          .read_text(encoding="utf-8"))
        self.assertEqual(meta["failure_category"], "pure_transport")

    # --- the pure-only rule, at run time ---------------------------------------------

    def test_a_pure_only_provider_on_a_non_m3c_node_fails_closed(self) -> None:
        """Config validation cannot see node shape. A node with no pure path would otherwise
        take the shared agentic loop with a provider that cannot run it."""
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
            env={}, backend="claude", agent_model="opus")
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

    def test_the_http_leaf_cannot_be_warm_resumed_so_repairs_stay_cold(self) -> None:
        c = self._conductor()
        entry = c.entry_for("generate", "generate")
        self.assertFalse(c._pure_session_resumable("some-session", entry))
        self.assertFalse(entry.supports(lc.CAP_WARM_RESUME))

    def test_the_launch_argv_builder_refuses_an_http_entry(self) -> None:
        """Defense in depth: nothing should reach `leaf_command` with an HTTP entry, and if
        anything did, building a CLI argv out of it is the wrong recovery."""
        c = self._conductor()
        with self.assertRaises(ValueError) as ctx:
            c.leaf_command("P", c.entry_for("generate", "generate"))
        self.assertIn("launches no CLI leaf", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
