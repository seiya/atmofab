"""The conductor running an HTTP leaf (issue #28 Phase 5; re-driven through the HTTP `workshop
leaf` in issue #534 PR-J4-b).

`test_llm_http_leaf.py` covers the shared HTTP layer in isolation, and `test_workshop_http.py`
the host tool-use loop's behaviour per dialect and per substep. This file covers the WIRING
between an HTTP entry and the conductor's records and retry decisions: the agent-run row's
provider, model and usage; the per-attempt model provenance; the raw response persisted under
`launches/` (and never as a workspace violation); the key read from the conductor's own
environment; no process spawned; a severed stream, a dead endpoint and a client error landing in
the transport categories the retry budget reads; `stream: false` and an entry's ceilings
reaching the wire; a mixed-provider configuration; and a damaged launch record on an outer
reopen degrading instead of raising.

Until PR-J4-b these rows drove the HTTP PURE leaf (`_run_http_leaf`, the in-memory pure
conversation and the cold-reopen `prior_document` carriers), which went with the pure path. The
rows here are the ones whose subject survives on the workshop path; each runs the REAL
`_run_workshop_producer_substep` and host loop against the loopback Chat-Completions stand-in
(`http_stand_ins.ScriptedChatApi`), the fixture `test_workshop_http.py` uses, with only the
wrapper operations faked.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import tools.llm_config as lc
import tools.llm_http_leaf as hl
import tools.workflow_conductor as wc
from tools.tests.llm_samples import sample_config_with as _cfg
from tools.tests.test_workshop_http import (
    _ORCH,
    SECRET,
    _bundle_writes,
    _ChatDialect,
    _GenerateHarness,
    _HttpProducerFake,
)

_PRIOR_ARID = "prior-arid"
_EXCERPT = "generate_gate_lint: unused variable x"


class HttpWorkshopWiringTests(_ChatDialect, _GenerateHarness, unittest.TestCase):
    """`generate.generate` on an `openai_compatible` entry, every other substep on claude."""

    def wired(self, script: list, **kwargs) -> _HttpProducerFake:
        c = self.conductor(script, **kwargs)
        self.events: list[dict] = []
        c.emit = lambda event, **f: self.events.append({"event": event, **f})  # type: ignore
        self.slept: list = []
        c._sleep_backoff = self.slept.append                     # type: ignore[assignment]
        return c

    def passing(self) -> list:
        return [self.calls(*_bundle_writes()), self.done()]

    def agent_run_row(self) -> dict:
        return [payload["--agent-run-json"] for sub, payload in self.c.calls
                if sub == "finalize-child" and "--agent-run-json" in payload][-1]

    def launches(self):
        return self.repo / "workspace" / "orchestrations" / _ORCH / "launches"

    # --- what the run records -----------------------------------------------------------

    def test_the_provenance_names_the_http_provider_and_its_resolved_model(self) -> None:
        self.wired(self.passing())
        self.assertEqual(self.run_substep().status, "pass")
        row = self.agent_run_row()
        self.assertEqual(row["agent_backend"], "openai_compatible")
        self.assertEqual(row["agent_model"], self.api.model)
        # Issue #348: the per-attempt row records the same model, and says it is the one the
        # provider's response reported.
        attempt = self.meta()["per_attempt"][0]
        self.assertEqual(attempt["model"], self.api.model)
        self.assertEqual(attempt["model_provenance"], "http_response")

    def test_a_response_naming_no_model_records_the_configured_one_as_such(self) -> None:
        """Issue #348: when the provider's response names no model, the loop falls back to the
        entry's configured name; the per-attempt row must say so rather than presenting a
        configured alias as a resolved model."""
        self.wired(self.passing())
        self.api.model = ""
        self.assertEqual(self.run_substep().status, "pass")
        attempt = self.meta()["per_attempt"][0]
        self.assertEqual(attempt["model"], "m")
        self.assertEqual(attempt["model_provenance"], "configured_entry")

    def test_the_leafs_token_usage_reaches_the_agent_run_row(self) -> None:
        """The wiring nobody had pinned: the reader parsed usage correctly and nothing asserted
        it ever ARRIVED anywhere. It has to reach the durable `agent_runs.jsonl` /
        `agent.result.json` row, because that row is what a cost audit reads — the alternative
        source is a multi-MB raw capture (issue #47). Summed over the launch's two requests."""
        self.wired(self.passing())
        self.api.usage = {"prompt_tokens": 5, "completion_tokens": 6,
                          "completion_tokens_details": {"reasoning_tokens": 4},
                          "prompt_tokens_details": {"cached_tokens": 2}}
        self.assertEqual(self.run_substep().status, "pass")
        usage = self.agent_run_row()["usage"]
        self.assertEqual({k: v for k, v in usage.items() if k != "provider_details"}, {
            "input_tokens": 10, "output_tokens": 12, "reasoning_tokens": 8, "cached_tokens": 4,
            "total_tokens": 22, "usage_source": "http_provider"})
        self.assertEqual(usage["provider_details"]["requests"], 2)

    def test_the_raw_response_body_is_persisted(self) -> None:
        """What arrives is an EVENT STREAM, and it is kept as it arrived — framing, the
        `[DONE]` terminator and all. That is also why the file is named `.txt`: a stream is
        never a JSON document, so a `.json` name would make every one of these a workspace
        violation (see the sibling test below)."""
        self.wired(self.passing())
        self.assertEqual(self.run_substep().status, "pass")
        persisted = (self.launches() / "child-1.http_response.txt").read_text(encoding="utf-8")
        self.assertIn("data: ", persisted)
        self.assertIn("[DONE]", persisted)

    def test_a_non_json_body_is_persisted_without_becoming_a_workspace_violation(self) -> None:
        """The body this file most needs to keep is the one that is NOT JSON — an HTML error
        page from a proxy. Under a `.json` name, `validate_workspace_root`, which parses every
        `workspace/**/*.json`, turns that evidence into an `invalid json` violation that
        outlives the transport failure and can block a later resume."""
        from tools.validate_workspace_root import _scan_json_for_violations
        self.wired([{"status": 200, "body": "<html><title>502 Bad Gateway</title></html>"}])
        self.assertEqual(self.run_substep().status, "fail")    # transport, as it should be
        bodies = sorted(self.launches().glob("*.http_response.*"))
        self.assertTrue(bodies)
        self.assertIn("502 Bad Gateway", bodies[0].read_text(encoding="utf-8"))
        for path in self.launches().rglob("*.json"):
            self.assertEqual(_scan_json_for_violations(path), [], msg=str(path))

    # --- where the key comes from, and what runs ----------------------------------------

    def test_the_key_comes_from_the_conductors_environment(self) -> None:
        """Every spawned leaf receives the environment the conductor reconstructs from its own;
        an HTTP leaf must read the same one. Reading the process-global environment takes a
        credential the run did not choose, or misses one it did — here the process holds the
        key and the run does not, so the launch must not send it."""
        self.wired(self.passing(), env={})
        with patch.dict("os.environ", {"HTTP_KEY": SECRET}, clear=False):
            outcome = self.run_substep()
        self.assertEqual(outcome.status, "fail")
        self.assertEqual(self.api.main_requests(), [])
        self.assertIn("missing_api_key",
                      " ".join(str(e.get("error", "")) for e in self.events))

    def test_no_process_is_spawned(self) -> None:
        self.wired(self.passing())
        with patch("subprocess.Popen", side_effect=AssertionError("a process was spawned")):
            self.assertEqual(self.run_substep().status, "pass")

    # --- the transport's failures, as the retry budget reads them -----------------------

    def test_a_stream_severed_before_its_terminator_is_a_transport_failure(self) -> None:
        """The correctness risk streaming introduces. A connection cut mid-reply leaves a
        plausible-looking partial; passed through, its calls would run and the loop would blame
        the model for a network fault. It has to arrive as a transport error — the category
        that is RETRIED, not repaired."""
        severed = {"sever": self.calls(*_bundle_writes())}
        self.wired([severed] * (wc.MAX_LEAF_TRANSIENT_RETRIES + 1))
        self.assertEqual(self.run_substep().status, "fail")
        errors = [e for e in self.events if e["event"] == "http_leaf_transport_error"]
        self.assertTrue(errors)
        self.assertIn("stream interrupted", errors[0]["error"])
        self.assertEqual(self.ops, [])                  # a severed reply's calls never run

    def test_a_dead_endpoint_is_retried_and_then_is_a_transport_failure(self) -> None:
        """A 503 is a TRANSIENT tag, so the loop re-launches it within the bounded budget before
        failing closed — one dropped connection must not lose a run that has already paid for
        every earlier phase."""
        self.wired([{"status": 503, "body": "upstream unavailable"}]
                   * (wc.MAX_LEAF_TRANSIENT_RETRIES + 1))
        outcome = self.run_substep()
        self.assertEqual(outcome.status, "fail")
        self.assertIsNotNone(outcome.infra_error)
        self.assertEqual(len(self.api.main_requests()), wc.MAX_LEAF_TRANSIENT_RETRIES + 1)
        self.assertEqual(len(self.slept), wc.MAX_LEAF_TRANSIENT_RETRIES)
        self.assertEqual([e["event"] for e in self.events].count("leaf_transient_retry"),
                         wc.MAX_LEAF_TRANSIENT_RETRIES)
        meta = self.meta()
        self.assertEqual(meta["failure_category"], "pure_transport")
        # Issue #348: a turn with no response records the entry's configured model AS configured.
        self.assertTrue(meta["per_attempt"])
        self.assertEqual([(a["model"], a["model_provenance"]) for a in meta["per_attempt"]],
                         [("m", "configured_entry")] * len(meta["per_attempt"]))

    def test_a_transient_failure_that_clears_lets_the_substep_pass(self) -> None:
        """The point of the retry: an outage that clears must not cost the run. The retry is
        decided by the conductor's own classifier here (`test_workshop_http.py`'s sibling row
        stubs that decision), and it is not a repair turn: it carries no findings."""
        self.wired([{"status": 503, "body": "upstream unavailable"}, *self.passing()])
        self.assertEqual(self.run_substep().status, "pass")
        self.assertEqual(len(self.slept), 1)
        requests = self.requests()
        self.assertEqual([r["workshop_lineage_id"] for r in requests], ["child-1", "child-2"])
        self.assertNotIn("repair_findings", requests[-1])

    def test_a_client_error_is_not_retried(self) -> None:
        """A 4xx is a deterministic misconfiguration; retrying it would report it as a provider
        outage the operator should wait out."""
        self.wired([{"status": 400, "body": '{"error": "max_tokens"}'}] * 3)
        self.assertEqual(self.run_substep().status, "fail")
        self.assertEqual([e["event"] for e in self.events].count("leaf_transient_retry"), 0)
        self.assertEqual(len(self.api.main_requests()), 1)

    # --- what reaches the wire ----------------------------------------------------------

    def test_an_entry_with_stream_false_sends_the_buffered_request(self) -> None:
        """The config key reaching the wire, end to end: the operator's escape hatch for an
        endpoint that cannot speak SSE has to actually change what is posted."""
        c = self.wired(self.passing(), extra="stream: false")
        self.assertIs(c.entry_for("generate", "generate").stream, False)
        self.assertEqual(self.run_substep().status, "pass")
        for body in self.api.main_requests():
            self.assertNotIn("stream", body)
            self.assertNotIn("stream_options", body)

    def test_a_streaming_leaf_produces_the_same_artifacts_as_a_buffered_one(self) -> None:
        """The transport differs, the run does not: the bundle a streamed lineage produces must
        be byte-identical to the one the buffered path produces."""
        written: list[bytes] = []
        for extra in ("stream: false", ""):
            self.setUp()                 # a fresh repo and fixture for each transport
            self.wired(self.passing(), extra=extra)
            self.assertEqual(self.run_substep().status, "pass", msg=extra)
            written.append(
                (self.repo / self.refs.source_dir() / "codegen_bundle.json").read_bytes())
        self.assertEqual(written[0], written[1])

    def test_the_transport_owns_its_own_ceilings(self) -> None:
        """The conductor once passed the CLI leaf's 128000, which made the transport's own
        default unreachable and asked every endpoint for a ceiling it rejects as a client error
        — non-retryable, on the first attempt."""
        self.wired(self.passing())
        self.run_substep()
        sent = self.api.main_requests()[0]
        self.assertEqual(sent["max_tokens"], hl.DEFAULT_MAX_OUTPUT_TOKENS)
        self.assertNotEqual(sent["max_tokens"], wc.LEAF_MAX_OUTPUT_TOKENS)

    def test_an_entrys_own_ceilings_reach_the_request(self) -> None:
        self.wired(self.passing(), extra="max_output_tokens: 4096, timeout_s: 30")
        self.run_substep()
        self.assertEqual(self.api.main_requests()[0]["max_tokens"], 4096)

    # --- the mixed configuration and the node-shape refusal -----------------------------

    def test_an_openai_entry_refuses_a_messages_api_level_at_load(self) -> None:
        """The other half of the anthropic row below: `xhigh` is a Messages-API level, not an
        OpenAI-compatible one, so the same `extra` that reaches the anthropic wire is a load
        error here — the loader checks the vocabulary per provider."""
        with self.assertRaises(lc.LlmConfigError) as ctx:
            self.wired(self.passing(), extra="effort: xhigh")
        self.assertEqual(ctx.exception.rule, "llm_config_invalid_field")

    def test_the_same_run_carries_two_providers(self) -> None:
        """Acceptance 2, at the level this file can assert it without a billed run: one
        conductor, one config, two providers, each leaf resolving to its own."""
        c = self.wired([])
        self.assertEqual(c.entry_for("generate", "generate").backend_token, "openai_compatible")
        self.assertEqual(c.entry_for("generate", "verify").backend_token, "claude")
        self.assertEqual(c.entry_for("validate", "judge").backend_token, "claude")
        self.assertEqual(c.entry_for(None, None).backend_token, "claude")
        self.assertEqual({row["backend"] for row in c.llm_config.provenance_map().values()},
                         {"claude", "openai_compatible"})
        # The HTTP provider holds no session: its warm turn is the host's in-memory history.
        self.assertFalse(c.entry_for("generate", "generate").supports(lc.CAP_WARM_RESUME))

    def test_an_http_provider_on_a_shapeless_node_fails_closed(self) -> None:
        """Config validation cannot see node shape. Since issue #169 NO in-tree node is such a
        node, so the subject is built by stubbing the predicate the shape reader consults
        first. The refusal text is checked because an operator acts on it, and it names BOTH
        inputs the predicate reads (a round-3 reviewer caught one naming the IR alone)."""
        c = self.wired([])
        c._conductor_authors_makefile = lambda refs: False      # type: ignore[assignment]
        outcome = self.run_substep()
        self.assertEqual(outcome.status, "fail")
        assert outcome.infra_error is not None
        self.assertEqual(outcome.infra_error[0], "node_has_no_bundle_shape")
        self.assertIn("no CodegenBundle shape", outcome.infra_error[1])
        self.assertNotIn("M3c", outcome.infra_error[1])
        self.assertIn("node_key", outcome.infra_error[1])
        self.assertEqual(self.api.main_requests(), [])

    def test_a_cli_provider_on_a_shapeless_node_fails_closed_the_same_way(self) -> None:
        """The refusal is about the NODE, not about the provider: the sibling of the row above
        on a claude configuration."""
        c = _HttpProducerFake(
            repo_root=self.repo, orchestration_id=_ORCH, orchestration_agent_run_id="orch",
            env={"HTTP_KEY": SECRET}, llm_config=_cfg("claude", agent_model="opus"))
        c._conductor_authors_makefile = lambda refs: False      # type: ignore[assignment]
        self.assertFalse(c._leaf_substep(self.refs, "generate", "generate"))
        outcome = c.run_substep(self.refs, "generate", "generate")
        self.assertEqual(outcome.status, "fail")
        assert outcome.infra_error is not None
        self.assertEqual(outcome.infra_error[0], "node_has_no_bundle_shape")

    def test_the_launch_argv_builder_refuses_an_http_entry(self) -> None:
        """Defense in depth: nothing should reach `leaf_command` with an HTTP entry, and if
        anything did, building a CLI argv out of it is the wrong recovery — on either kind."""
        c = self.wired([])
        entry = c.entry_for("generate", "generate")
        for kwargs in ({"reading": True}, {"mcp_config_json": "{}"}):
            with self.subTest(**kwargs), self.assertRaises(ValueError) as ctx:
                c.leaf_command(entry, **kwargs)
            self.assertIn("launches no CLI leaf", str(ctx.exception))

    # --- an outer reopen whose target's record is damaged -------------------------------

    def test_a_damaged_launch_record_degrades_instead_of_raising(self) -> None:
        """An outer `reuse` reopen reads the repair target's launch record
        (`_read_launch_request_payload`) in a frame that must not raise: a raise there takes the
        conductor down mid-run with no `step_result.json` for a `--resume`. Its `except` once
        named `json.JSONDecodeError` rather than `ValueError`, so a non-UTF-8 record raised
        `UnicodeDecodeError` out of `run_substep`. Non-UTF-8 bytes rather than malformed JSON,
        because malformed JSON was already caught and would pass against the narrow spelling.
        With no lineage to find, the reopen is a cold launch, and the findings still ride."""
        self.wired(self.passing())
        self.launches().mkdir(parents=True, exist_ok=True)
        (self.launches() / f"{_PRIOR_ARID}.request.json").write_bytes(
            b'{"workshop_lineage_id": "child-0", "source_id": "\xff\xfe"}')
        outcome = self.reopen(_PRIOR_ARID, _EXCERPT)
        self.assertEqual(outcome.status, "pass", outcome)
        first = self.requests()[0]
        self.assertFalse(first.get("warm_resume"))
        self.assertIn(_EXCERPT, first["repair_findings"])
        self.assertEqual((self.reopen_event()["mode"],
                          self.reopen_event()["findings_carried"]), ("cold", True))

    def reopen(self, target: str, findings: str | None) -> wc.SubstepOutcome:
        repair = {"repair_strategy": "reuse", "repair_target_agent_run_id": target,
                  "issue_severity": "major", "repair_reason": "generate_gate_lint"}
        if findings is not None:
            repair["repair_findings"] = findings
        return self.c.run_substep(self.refs, "generate", "generate", repair=repair)

    def reopen_event(self) -> dict:
        [event] = [e for e in self.events if e["event"] == "workshop_reopen"]
        return event

    def test_a_reopen_naming_the_none_target_carries_nothing_and_says_so(self) -> None:
        """A target spelled `"none"` is the payload-absent spelling: no launch is ever recorded
        under it, and `_validate_launch_request_payload` refuses a reuse repair naming it. So
        the reopen is a plain launch — its request carries no repair — and its event must say
        the findings did not ride rather than reporting the excerpt it was handed."""
        self.wired(self.passing())
        read: list = []
        with patch("tools.orchestration_runtime._read_launch_request_payload",
                   side_effect=lambda *a, **k: read.append(k) or None):
            self.assertEqual(self.reopen("none", _EXCERPT).status, "pass")
        self.assertEqual(read, [])                      # no record read for "none"
        first = self.requests()[0]
        self.assertNotIn("repair_findings", first)
        self.assertNotEqual(first.get("repair_strategy"), "reuse")
        self.assertEqual((self.reopen_event()["target"],
                          self.reopen_event()["findings_carried"]), ("none", False))

    def test_a_reopen_without_findings_is_a_plain_launch(self) -> None:
        """No excerpt, nothing to repair: the reopen's request carries no repair, and its event
        says no findings rode."""
        self.wired(self.passing())
        self.assertEqual(self.reopen(_PRIOR_ARID, None).status, "pass")
        first = self.requests()[0]
        self.assertNotIn("repair_findings", first)
        self.assertNotEqual(first.get("repair_strategy"), "reuse")
        self.assertIs(self.reopen_event()["findings_carried"], False)


class AnthropicEffortWiringTests(_GenerateHarness, unittest.TestCase):
    """`generate.generate` on an `anthropic_api` entry (the harness's default dialect): the
    entry's `effort` observed from the CONFIG LOADER to the Messages-API body (issue #560),
    rather than through the `SimpleNamespace` entry `test_llm_workshop_loop.py` drives."""

    def passing(self) -> list:
        return [self.calls(*_bundle_writes()), self.done()]

    def test_an_entrys_effort_reaches_every_request_as_output_config(self) -> None:
        c = self.conductor(self.passing(), extra="effort: xhigh")
        self.assertEqual(c.entry_for("generate", "generate").effort, "xhigh")
        self.assertEqual(self.run_substep().status, "pass")
        bodies = self.api.main_requests()
        self.assertGreaterEqual(len(bodies), 2)
        for body in bodies:
            self.assertEqual(body["output_config"], {"effort": "xhigh"})

    def test_an_entry_with_no_effort_sends_no_output_config(self) -> None:
        c = self.conductor(self.passing())
        self.assertEqual(c.entry_for("generate", "generate").effort, "")
        self.assertEqual(self.run_substep().status, "pass")
        for body in self.api.main_requests():
            self.assertNotIn("output_config", body)


if __name__ == "__main__":
    unittest.main()
