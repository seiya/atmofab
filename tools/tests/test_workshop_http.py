"""The HTTP `workshop leaf` through the REAL conductor loops (issue #534 PR-J3-b): MH2 (the
producing substeps) and MH3 (the reading ones), on both dialects, against the loopback
stand-ins (`http_stand_ins`).

What is real: `_spawn_workshop_turn`'s HTTP arm, `_run_workshop_http_loop`, the host loop
(`tools/llm_workshop_loop.py`) and its five file tools on a real lineage tree, the lineage
record, the snapshot, the assembly, the document validators, the per-attempt meta, the warm
history and its drop. What is faked: the launch record (`_PureFakeConductor.runtime`, whose
prompt is `PROMPT`), the lineage home's location, and on `generate.generate` the wrapper's
context and operations (`run_wrapper_op` is recorded and answered; the real operations under a
real bwrap through the same loop are `test_bwrap_simulation.WorkshopWrapperSandboxTests`'s HTTP
row). `compile.generate` runs the REAL `_freeze_ir_check_context` and `check_ir`."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

import tools.llm_config as lc
import tools.orchestration_runtime as ort
import tools.workflow_conductor as wc
from tools import pure_leaf, workshop_tree
from tools.tests.http_stand_ins import (
    ScriptedChatApi,
    ScriptedMessagesApi,
    chat_call,
    tool_use,
)
from tools.tests.test_pure_leaf_compile import _assembled_ir, _write_compile_node
from tools.tests.test_pure_leaf_judge import _review, _write_run_node
from tools.tests.test_pure_leaf_producer import _valid_bundle
from tools.tests.test_pure_leaf_verify import _verdict, _verify_node
from tools.tests.test_workshop_producer import _WorkshopFakeConductor, _write_node
from tools.tests.test_workshop_reviewer import _ReviewerFake

_ORCH = "o"
SECRET = "sk-http-workshop-secret"
FILES = list(pure_leaf.HTTP_FILE_TOOLS)


def _config(repo: Path, provider: str, base_url: str, phase: str, substep: str,
            producing: bool) -> lc.LlmConfig:
    bounds = "max_wrapper_calls: 30, lineage_timeout_s: 2700" if producing \
        else "lineage_timeout_s: 2700"
    path = repo / f"llm_http_{phase}_{substep}.yaml"
    path.write_text(
        "defaults: {provider: claude_cli, model: opus}\n"
        f"phases: {{{phase}: {{substeps: {{{substep}: {{provider: {provider}, model: m, "
        f"base_url: '{base_url}', api_key_env: HTTP_KEY, leaf: workshop, {bounds}"
        "}}}}\n", encoding="utf-8")
    return lc.load_llm_config(path)


def _unmarked(value):
    """`value` with every `cache_control` key removed (the breakpoint moves request by
    request; the stored history carries none)."""
    if isinstance(value, dict):
        return {k: _unmarked(v) for k, v in value.items() if k != "cache_control"}
    if isinstance(value, list):
        return [_unmarked(v) for v in value]
    return value


class _HttpMixin:
    """The HTTP-specific half of both fakes: the REAL session check (the in-memory history),
    no endpoint, no `spawn_leaf`, and an `http/` home with no `config/`."""

    def _prepare_workshop_home(self, entry, lineage_id, *, resume):  # type: ignore[override]
        home = self.homes / self.orchestration_id / self._workshop_backend(entry) / lineage_id
        if resume:
            if not home.is_dir():
                return None
        else:
            home.mkdir(parents=True)
        return {"homes_root": str(self.homes), "home": str(home),
                "tree": str(home / "tree"), "lineage_id": lineage_id}

    def _workshop_session_resumable(self, paths, session_id):  # type: ignore[override]
        return wc.Conductor._workshop_session_resumable(self, paths, session_id)

    def _start_wrapper_endpoint(self, ctx, on_end):  # type: ignore[override]
        raise AssertionError("an HTTP workshop launch starts no endpoint")

    def spawn_leaf(self, *args, **kwargs):  # type: ignore[override]
        raise AssertionError("an HTTP workshop launch never reaches spawn_leaf")


class _HttpProducerFake(_HttpMixin, _WorkshopFakeConductor):
    pass


class _HttpReviewerFake(_HttpMixin, _ReviewerFake):
    pass


class _Dialect:
    """One dialect's script vocabulary."""

    PROVIDER = "anthropic_api"

    def server(self, script: list):
        return ScriptedMessagesApi(script)

    def calls(self, *calls: tuple[str, str, dict]) -> tuple:
        return [tool_use(cid, name, args) for cid, name, args in calls], "tool_use"

    def done(self, text: str = "done") -> tuple:
        return [{"type": "text", "text": text}], "end_turn"

    def cut(self, cid: str, name: str, args: dict) -> tuple:
        return [tool_use(cid, name, args)], "max_tokens"

    def declared(self, body: dict) -> list[str]:
        return [t["name"] for t in body["tools"]]

    def results(self, body: dict) -> list[dict]:
        return [json.loads(b["content"]) for m in body["messages"] if m["role"] == "user"
                and isinstance(m["content"], list)
                for b in m["content"] if b.get("type") == "tool_result"]


class _ChatDialect(_Dialect):
    PROVIDER = "openai_compatible"

    def server(self, script: list):
        return ScriptedChatApi(script)

    def calls(self, *calls: tuple[str, str, dict]) -> tuple:
        return {"tool_calls": [chat_call(cid, name, args) for cid, name, args in calls]}, \
            "tool_calls"

    def done(self, text: str = "done") -> tuple:
        return {"content": text}, "stop"

    def cut(self, cid: str, name: str, args: dict) -> tuple:
        return {"tool_calls": [chat_call(cid, name, args)]}, "length"

    def declared(self, body: dict) -> list[str]:
        return [t["function"]["name"] for t in body["tools"]]

    def results(self, body: dict) -> list[dict]:
        return [json.loads(m["content"]) for m in body["messages"] if m["role"] == "tool"]


def _bundle_writes() -> list[tuple[str, str, dict]]:
    doc = json.loads(json.dumps(_valid_bundle()))
    out = []
    for i, entry in enumerate(doc["files"]):
        out.append((f"w{i}", "write_file", {"path": entry["logical_path"],
                                            "content": entry.pop("content")}))
    out.append(("wm", "write_file", {"path": workshop_tree.BUNDLE_MANIFEST_NAME,
                                     "content": yaml.safe_dump(doc)}))
    return out


# --- MH2: the producing substeps ---------------------------------------------------------


class _GenerateProducer(_Dialect):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        (self.repo / "workspace" / "orchestrations" / _ORCH).mkdir(parents=True)
        self.refs = _write_node(self.repo)
        self.ops: list[tuple[str, dict]] = []

        # `workshop_mcp` binds `run_wrapper_op` by name at import, and the conductor imports it
        # lazily (for the tool declarations) inside the patched window below: imported first
        # here, it keeps the real function for every later test of the process.
        import tools.workshop_mcp  # noqa: F401

        def fake_op(ctx, op, args=None, *, on_spawn=None):
            self.ops.append((op, dict(args or {})))
            return {"ok": True, "op": op}

        patcher = mock.patch("tools.workshop_wrapper.run_wrapper_op", fake_op)
        patcher.start()
        self.addCleanup(patcher.stop)

    def conductor(self, script: list) -> _HttpProducerFake:
        self.api = self.server(script)
        self.addCleanup(self.api.close)
        c = _HttpProducerFake(
            repo_root=self.repo, orchestration_id=_ORCH, orchestration_agent_run_id="orch",
            llm_config=_config(self.repo, self.PROVIDER, self.api.base_url, "generate",
                               "generate", True),
            env={"HTTP_KEY": SECRET})
        c.homes = self.repo.parent / (self.repo.name + "_homes")
        self.addCleanup(lambda: shutil.rmtree(c.homes, ignore_errors=True))
        c.leaf_actions = []
        self.c = c
        return c

    def run_substep(self, c=None) -> wc.SubstepOutcome:
        return (c or self.c).run_substep(self.refs, "generate", "generate")

    def paths(self, lineage: str) -> workshop_tree.LineagePaths:
        return workshop_tree.LineagePaths(
            homes_root=self.c.homes, orchestration_id=_ORCH, lineage_id=lineage,
            orchestration_dir=self.repo / "workspace" / "orchestrations" / _ORCH,
            backend="http")

    def meta(self) -> dict:
        return json.loads((self.repo / self.refs.source_dir() / "bundle_meta.json").read_text())

    def requests(self) -> list[dict]:
        return [c[1]["--request-json"] for c in getattr(self.c, "calls", [])
                if c[0] == "record-launch"]

    def test_a_bundle_written_through_the_loop_passes(self) -> None:
        self.conductor([self.calls(*_bundle_writes()),
                        self.calls(("d", "derive", {})),
                        self.done()])
        outcome = self.run_substep()
        self.assertEqual(outcome.status, "pass", outcome)
        first = self.api.main_requests()[0]
        self.assertEqual(self.declared(first), [*FILES, "derive", "compile", "lint",
                                                "syntax_check", "run"])
        self.assertEqual(self.ops, [("derive", {})])
        row = self.meta()["per_attempt"][0]
        self.assertEqual((row["lineage_id"], row["model_provenance"], row["ended_reason"],
                          row["truncated"], row["warm"]),
                         ("child-1", "http_response", self.done()[1], None, False))
        self.assertEqual(row["model_turns"], 3)
        self.assertEqual(row["usage"]["usage_source"], "http_provider")
        self.assertEqual(row["usage"]["provider_details"]["requests"], 3)
        # The record: every request, response, call and result; no endpoint record.
        record = self.paths("child-1").record_dir
        rows = [json.loads(line) for line in
                (record / "loop_turns.jsonl").read_text().splitlines()]
        self.assertEqual([r["event"] for r in rows].count("request"), 3)
        self.assertEqual([r["name"] for r in rows if r["event"] == "tool_call"],
                         [w[1] for w in _bundle_writes()] + ["derive"])
        self.assertFalse((record / "wrapper_calls.jsonl").exists())
        self.assertNotIn(SECRET, (record / "loop_turns.jsonl").read_text())
        response = (self.repo / "workspace" / "orchestrations" / _ORCH / "launches"
                    / "child-1.http_response.txt")
        self.assertTrue(response.is_file())
        # The history is dropped once the substep is done? No: the lineage is open (a pass
        # ends nothing), so its conversation stays for an outer reopen in this process.
        self.assertIn("child-1", self.c._workshop_http_histories())

    def test_a_seed_write_is_refused_and_the_lineage_carries_on(self) -> None:
        self.conductor([self.calls(("s", "write_file", {"path": "CONTRACT.md",
                                                         "content": "mine"})),
                        self.calls(*_bundle_writes()), self.done()])
        outcome = self.run_substep()
        self.assertEqual(outcome.status, "pass")
        results = self.results(self.api.main_requests()[1])
        self.assertEqual(results[0]["category"], "tool_refused")
        self.assertEqual((self.paths("child-1").tree / "CONTRACT.md").read_text(), "PROMPT")

    def test_a_turn_with_no_tool_call_is_repaired_warm_with_the_finding(self) -> None:
        self.conductor([self.done("I would write it"), self.calls(*_bundle_writes()),
                        self.done()])
        outcome = self.run_substep()
        self.assertEqual(outcome.status, "pass")
        requests = self.requests()
        self.assertEqual([(r["workshop_lineage_id"], bool(r.get("warm_resume")))
                          for r in requests], [("child-1", False), ("child-1", True)])
        self.assertIn("the endpoint returned no tool call", requests[1]["repair_findings"])
        # Warm: the repair launch's first request carries the first launch's conversation.
        bodies = [_unmarked(b) for b in self.api.main_requests()]
        self.assertGreater(len(bodies[1]["messages"]), len(bodies[0]["messages"]))
        self.assertEqual(bodies[1]["messages"][0], bodies[0]["messages"][0])

    def test_a_truncated_reply_is_repaired_with_the_cut_finding(self) -> None:
        self.conductor([self.cut("w", "write_file", {"path": "x.f90", "content": "..."}),
                        self.calls(*_bundle_writes()), self.done()])
        outcome = self.run_substep()
        self.assertEqual(outcome.status, "pass")
        finding = self.requests()[1]["repair_findings"]
        self.assertIn("cut at the output limit", finding)
        self.assertIn("write_file", finding)
        self.assertFalse((self.paths("child-1").tree / "x.f90").exists())
        row = self.meta()["per_attempt"][0]
        self.assertEqual(row["ended_reason"], "truncated")

    def test_a_transport_death_drops_the_history_and_reopens_cold(self) -> None:
        self.conductor([{"status": 503, "body": "upstream"}, self.calls(*_bundle_writes()),
                        self.done()])
        with mock.patch.object(_HttpProducerFake, "_pure_transient_retry",
                               side_effect=[True]):
            outcome = self.run_substep()
        self.assertEqual(outcome.status, "pass", outcome)
        requests = self.requests()
        self.assertEqual([r["workshop_lineage_id"] for r in requests], ["child-1", "child-2"])
        self.assertNotIn("child-1", self.c._workshop_http_histories())
        record = workshop_tree.read_lineage_record(self.paths("child-1"))
        self.assertEqual(record["ended_by"], "leaf_exit")
        # The cold retry's first request holds the launch prompt alone.
        self.assertEqual([m["role"] for m in self.api.main_requests()[1]["messages"]
                          if m["role"] != "system"], ["user"])

    def test_a_fresh_process_does_not_resume_the_history(self) -> None:
        self.conductor([self.calls(*_bundle_writes()), self.done()])
        self.run_substep()
        paths = self.paths("child-1")
        self.assertTrue(self.c._workshop_session_resumable(paths, "child-1"))
        fresh = _HttpProducerFake(
            repo_root=self.repo, orchestration_id=_ORCH, orchestration_agent_run_id="orch",
            llm_config=self.c.llm_config, env={"HTTP_KEY": SECRET})
        self.assertFalse(fresh._workshop_session_resumable(paths, "child-1"))
        # An ended lineage is not resumed either.
        workshop_tree.end_lineage(paths, "superseded")
        self.assertFalse(self.c._workshop_session_resumable(paths, "child-1"))

    def test_the_request_passes_the_real_validator_and_renders_the_http_row(self) -> None:
        self.conductor([self.calls(*_bundle_writes()), self.done()])
        self.run_substep()
        [request] = self.requests()
        self.assertEqual(request["workshop_provider"], self.PROVIDER)
        payload = ort.prepare_launch_request_payload(dict(request))
        ort._validate_launch_request_payload(payload)
        prompt = ort.render_launch_prompt_text(payload)
        ort._validate_launch_prompt_text(payload, prompt)
        self.assertIn(pure_leaf.HTTP_PRODUCER_TOOL_SURFACE_TEXT, prompt)
        self.assertNotIn("MCP", prompt)

    def test_a_cold_repair_restates_the_tool_surface_row(self) -> None:
        """Round 2 of PR-J3-b: what to do about a cut reply is the row's, outside the
        findings' fence, so a COLD repair (here after the lineage timed out) carries it."""
        self.conductor([{"sleep": 3, "then": self.done("late")},
                        self.calls(*_bundle_writes()), self.done()])
        with mock.patch.object(wc.Conductor, "_workshop_timeout",
                               side_effect=[(1, "lineage_timeout_s"),
                                            (600, "lineage_timeout_s")]):
            self.assertEqual(self.run_substep().status, "pass")
        cold_repair = self.requests()[1]
        self.assertFalse(cold_repair.get("warm_resume"))
        self.assertTrue(cold_repair.get("repair_findings"))
        payload = ort.prepare_launch_request_payload(dict(cold_repair))
        prompt = ort.render_launch_prompt_text(payload)
        self.assertIn(pure_leaf.HTTP_PRODUCER_TOOL_SURFACE_TEXT, prompt)
        fence = prompt.index(pure_leaf.PURE_DOC_FENCE_END)
        self.assertGreater(prompt.index(pure_leaf.HTTP_PRODUCER_TOOL_SURFACE_TEXT), fence)

    def test_a_tool_argument_with_a_lone_surrogate_is_recorded_not_raised(self) -> None:
        """Codex, round 2: JSON accepts `"\\ud800"`; the write is refused and the record row
        is written escaped rather than aborting the conductor."""
        lone = '{"path": "x.f90", "content": "\\ud800"}'
        if self.PROVIDER == "anthropic_api":
            bad = ([tool_use("s", "write_file", {}, raw_input=lone)], "tool_use")
        else:
            bad = ({"tool_calls": [chat_call("s", "write_file", lone)]}, "tool_calls")
        self.conductor([bad, self.calls(*_bundle_writes()), self.done()])
        self.assertEqual(self.run_substep().status, "pass")
        rows = (self.paths("child-1").record_dir / "loop_turns.jsonl").read_text()
        self.assertIn("\\ud800", rows)
        self.assertEqual(self.results(self.api.main_requests()[1])[0]["category"],
                         "tool_refused")

    def test_the_persisted_reply_is_redacted(self) -> None:
        """The model's answer channel is persisted redacted (`persist_stdout`), as on the pure
        HTTP path."""
        self.conductor([self.calls(*_bundle_writes()), self.done(f"the key is {SECRET}")])
        self.assertEqual(self.run_substep().status, "pass")
        text = (self.repo / "workspace" / "orchestrations" / _ORCH / "agents" / "child-1"
                / "dialogs" / "leaf.stdout.log").read_text()
        self.assertIn("the key is", text)
        self.assertNotIn(SECRET, text)

    def test_an_outer_reopen_in_the_same_process_is_warm(self) -> None:
        """`repair_strategy: reuse` on an open HTTP lineage this process holds is a WARM turn:
        the reopen's request carries the first launch's conversation (`warm_capable` holds
        for an HTTP entry, which declares no `warm_resume`)."""
        self.conductor([self.calls(*_bundle_writes()), self.done(),
                        self.calls(*_bundle_writes()), self.done()])
        self.assertEqual(self.run_substep().status, "pass")
        evidence = self.c.__dict__.setdefault("evidence", {})
        runner = self.c._runner_basename(self.refs)
        text = (self.repo / self.refs.source_dir() / "src" / runner).read_text()
        self.refs = wc.NodeRefs(target_id=self.refs.target_id, node_key=self.refs.node_key,
                                spec_path=self.refs.spec_path, ir_id=self.refs.ir_id,
                                pipeline_id=self.refs.pipeline_id,
                                source_id="src_20260715_002")
        src = self.repo / self.refs.source_dir() / "src"
        src.mkdir(parents=True)
        (src / runner).write_text(text, encoding="utf-8")

        def read(repo_root, oid, *, agent_run_id):
            return evidence.get(f"{agent_run_id}.request.input.json")

        with mock.patch.object(ort, "_read_launch_request_payload", read):
            outcome = self.c.run_substep(self.refs, "generate", "generate", repair={
                "repair_strategy": "reuse", "repair_target_agent_run_id": "child-1",
                "repair_findings": "lint: unused variable x", "issue_severity": "major",
                "repair_reason": "generate_gate_lint"})
        self.assertEqual(outcome.status, "pass", outcome)
        request = self.requests()[-1]
        self.assertEqual((request["workshop_lineage_id"], request.get("warm_resume")),
                         ("child-1", True))
        bodies = [_unmarked(b) for b in self.api.main_requests()]
        self.assertGreater(len(bodies[2]["messages"]), len(bodies[0]["messages"]))

    def test_the_setting_surface_names_the_declared_tools(self) -> None:
        self.conductor([self.done()])
        entry = self.c.entry_for("generate", "generate")
        surface = self.c._launch_setting_surface(
            "child-9", {"step": "generate", "substep": "generate"}, entry)
        self.assertEqual(surface, {"http_tool_surface": [
            *FILES, "derive", "compile", "lint", "syntax_check", "run"]})


class MachineHistoryTests(unittest.TestCase):
    """The lineage machine forgets an HTTP lineage's conversation when it sets the lineage
    aside without the turn's tail having ended it: a supersede in `next_launch` (here a warm
    lineage with no time left) and a pruned home (`home_gone`)."""

    def _machine(self, deadline: float):
        from types import SimpleNamespace
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        paths = workshop_tree.LineagePaths(homes_root=root / "h", orchestration_id="o",
                                           lineage_id="lin", orchestration_dir=root / "o",
                                           backend="http")
        paths.home.mkdir(parents=True)
        paths.make_work_dirs()
        workshop_tree.write_lineage_record(paths, node_key="n", started_at_utc="t")
        conductor = SimpleNamespace(
            _workshop_http_histories=lambda: histories,
            _drop_workshop_http_history=lambda lid: histories.pop(lid, None),
            _workshop_session_resumable=lambda p, s: True)
        histories = {"lin": object()}
        lineage = SimpleNamespace(paths=paths, deadline=deadline)
        entry = SimpleNamespace(is_http=True, supports=lambda cap: False, max_wrapper_calls=5)
        spec = SimpleNamespace(workshop_freeze_context=object())
        machine = wc.Conductor._WorkshopLineageMachine(
            conductor, None, entry, spec, lineage=lineage, resume_session_id="lin")
        return machine, histories, paths

    def test_a_superseded_lineage_s_history_is_dropped(self) -> None:
        import time as _time
        machine, histories, paths = self._machine(_time.time() - 5)
        self.assertFalse(machine.next_launch())
        self.assertEqual(histories, {})
        self.assertEqual(workshop_tree.read_lineage_record(paths)["ended_by"],
                         "lineage_timeout")

    def test_an_open_lineage_with_time_left_goes_warm_and_keeps_it(self) -> None:
        import time as _time
        machine, histories, _paths = self._machine(_time.time() + 600)
        self.assertTrue(machine.next_launch())
        self.assertIn("lin", histories)

    def test_a_pruned_home_drops_the_history(self) -> None:
        import time as _time
        machine, histories, _paths = self._machine(_time.time() + 600)
        machine.home_gone()
        self.assertEqual(histories, {})


class LoopNoteTests(unittest.TestCase):
    def test_the_cut_note_says_what_was_cut(self) -> None:
        from types import SimpleNamespace
        note = wc.Conductor._WorkshopLineageMachine.loop_note
        proc = SimpleNamespace(returncode=0, timed_out=False)
        inside = SimpleNamespace(truncated="max_output_tokens=9 inside a `write_file` call",
                                 ended_reason="truncated", tool_call_made=True, proc=proc,
                                 cancelled=False)
        self.assertIn("none of its tool calls ran", note(inside))
        text_only = SimpleNamespace(truncated="max_output_tokens=9", ended_reason="truncated",
                                    tool_call_made=True, proc=proc, cancelled=False)
        self.assertNotIn("tool calls", note(text_only))
        self.assertIn("before it finished", note(text_only))


class AnthropicGenerateProducerTests(_GenerateProducer, unittest.TestCase):
    pass


class ChatGenerateProducerTests(_ChatDialect, _GenerateProducer, unittest.TestCase):
    pass


class _CompileProducer(_Dialect):
    """`compile.generate` on HTTP: the REAL IR-check context and `check_ir`."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        (self.repo / "workspace" / "orchestrations" / _ORCH).mkdir(parents=True)
        self.refs = _write_compile_node(self.repo)

    def test_the_ir_is_checked_in_process_and_accepted(self) -> None:
        ir = yaml.safe_dump(_assembled_ir(), sort_keys=False)
        self.api = self.server([
            self.calls(("w", "write_file", {"path": workshop_tree.IR_DOCUMENT_NAME,
                                             "content": ir})),
            self.calls(("k", "check_ir", {})), self.done()])
        self.addCleanup(self.api.close)
        c = _HttpProducerFake(
            repo_root=self.repo, orchestration_id=_ORCH, orchestration_agent_run_id="orch",
            llm_config=_config(self.repo, self.PROVIDER, self.api.base_url, "compile",
                               "generate", True), env={"HTTP_KEY": SECRET})
        c.homes = self.repo.parent / (self.repo.name + "_homes")
        self.addCleanup(lambda: shutil.rmtree(c.homes, ignore_errors=True))
        c.leaf_actions = []
        outcome = c.run_substep(self.refs, "compile", "generate", dependency_surface=())
        self.assertEqual(outcome.status, "pass", outcome)
        bodies = self.api.main_requests()
        self.assertEqual(self.declared(bodies[0]), [*FILES, "check_ir"])
        [check] = self.results(bodies[2])[-1:]
        # `check_ir` RAN, in process, on the tree's IR: its answer is the static gate's (this
        # fixture's IR is not gate-clean, so it reports findings; the host's own document
        # checks, which the pass reads, are the authority).
        self.assertEqual(check.get("category"), "compile_static_violation", check)
        self.assertIn("/http/child-1/check/spec.ir.yaml", check["findings"])
        record = workshop_tree.read_lineage_record(workshop_tree.LineagePaths(
            homes_root=c.homes, orchestration_id=_ORCH, lineage_id="child-1",
            orchestration_dir=self.repo / "workspace" / "orchestrations" / _ORCH,
            backend="http"))
        self.assertEqual(record["wrapper_calls_used"], 1)


class AnthropicCompileProducerTests(_CompileProducer, unittest.TestCase):
    pass


class ChatCompileProducerTests(_ChatDialect, _CompileProducer, unittest.TestCase):
    pass


# --- MH3: the reading substeps -----------------------------------------------------------


class _Reading(_Dialect):
    PHASE = "compile"
    SUBSTEP = "verify"
    OUTPUT = "verdict.json"

    def stage(self, repo: Path) -> wc.NodeRefs:
        return _write_compile_node(repo, stage_ir=True)

    def passing(self) -> dict:
        return _verdict("pass")

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        (self.repo / "workspace" / "orchestrations" / _ORCH).mkdir(parents=True)
        self.refs = self.stage(self.repo)

    def conductor(self, script: list) -> _HttpReviewerFake:
        self.api = self.server(script)
        self.addCleanup(self.api.close)
        c = _HttpReviewerFake(
            repo_root=self.repo, orchestration_id=_ORCH, orchestration_agent_run_id="orch",
            llm_config=_config(self.repo, self.PROVIDER, self.api.base_url, self.PHASE,
                               self.SUBSTEP, False), env={"HTTP_KEY": SECRET})
        c.calls = []
        c.homes = self.repo.parent / (self.repo.name + "_homes")
        self.addCleanup(lambda: shutil.rmtree(c.homes, ignore_errors=True))
        c.leaf_actions = []
        self.c = c
        return c

    def seed(self) -> str:
        return min(wc.Conductor._WORKSHOP_REVIEWER_DOCUMENT_SEEDS[
            (self.PHASE, self.SUBSTEP, "")].values())

    def write_output(self, doc: dict, cid: str = "o") -> tuple:
        return (cid, "write_file", {"path": self.OUTPUT, "content": json.dumps(doc)})

    def test_reads_ranged_and_greps_then_its_document_is_accepted(self) -> None:
        seed = self.seed()
        self.conductor([
            self.calls(("r", "read_file", {"path": seed, "start_line": 1, "max_lines": 2}),
                       ("g", "grep_file", {"path": seed, "pattern": "."}),
                       ("l", "list_files", {})),
            self.calls(self.write_output(self.passing())), self.done()])
        outcome = self.c.run_substep(self.refs, self.PHASE, self.SUBSTEP)
        self.assertEqual(outcome.status, "pass", outcome)
        bodies = self.api.main_requests()
        self.assertEqual(self.declared(bodies[0]), FILES)
        read, grep, listed = self.results(bodies[1])
        self.assertTrue(read["ok"], read)
        self.assertEqual((read["start_line"], read["end_line"]), (1, min(2, read["total_lines"])))
        self.assertGreaterEqual(read["total_lines"], 1)
        self.assertTrue(grep["matches"] and grep["matches"][0]["line"] >= 1, grep)
        self.assertIn(seed, [f["path"] for f in listed["files"]])
        # The reading system prompt.
        system = json.dumps(bodies[0].get("system") or bodies[0]["messages"][0])
        self.assertIn("reviewer", system)

    def test_a_write_of_a_seed_is_refused(self) -> None:
        seed = self.seed()
        self.conductor([self.calls(("s", "write_file", {"path": seed, "content": "x"})),
                        self.calls(self.write_output(self.passing())), self.done()])
        self.assertEqual(self.c.run_substep(self.refs, self.PHASE, self.SUBSTEP).status,
                         "pass")
        self.assertEqual(self.results(self.api.main_requests()[1])[0]["category"],
                         "tool_refused")

    def test_a_missing_document_is_repaired_warm_naming_the_no_tool_turn(self) -> None:
        self.conductor([self.done("looks fine"),
                        self.calls(self.write_output(self.passing())), self.done()])
        outcome = self.c.run_substep(self.refs, self.PHASE, self.SUBSTEP)
        self.assertEqual(outcome.status, "pass", outcome)
        requests = [c[1]["--request-json"] for c in self.c.calls if c[0] == "record-launch"]
        self.assertEqual([(r["workshop_lineage_id"], bool(r.get("warm_resume")))
                          for r in requests], [("child-1", False), ("child-1", True)])
        self.assertIn("the endpoint returned no tool call", requests[1]["repair_findings"])
        bodies = [_unmarked(b) for b in self.api.main_requests()]
        # The repair launch's request opens with the first launch's prompt and its reply.
        first = [m for m in bodies[0]["messages"] if m["role"] != "system"]
        second = [m for m in bodies[1]["messages"] if m["role"] != "system"]
        self.assertEqual(second[0], first[0])
        self.assertEqual(second[1]["role"], "assistant")
        self.assertIn("looks fine", json.dumps(second[1]))

    def test_the_request_renders_the_http_reader_row(self) -> None:
        self.conductor([self.calls(self.write_output(self.passing())), self.done()])
        self.c.run_substep(self.refs, self.PHASE, self.SUBSTEP)
        [request] = [c[1]["--request-json"] for c in self.c.calls if c[0] == "record-launch"]
        payload = ort.prepare_launch_request_payload(dict(request))
        ort._validate_launch_request_payload(payload)
        prompt = ort.render_launch_prompt_text(payload)
        self.assertIn(pure_leaf.HTTP_READER_TOOL_SURFACE_TEXT, prompt)
        # No codex reading helper on an HTTP reading tree.
        tree = workshop_tree.LineagePaths(
            homes_root=self.c.homes, orchestration_id=_ORCH, lineage_id="child-1",
            orchestration_dir=self.repo / "workspace" / "orchestrations" / _ORCH,
            backend="http").tree
        self.assertFalse((tree / pure_leaf.CODEX_READ_HELPER_NAME).exists())

    def test_a_timed_out_launch_is_an_unfinished_review(self) -> None:
        self.conductor([{"sleep": 4, "then": self.calls(self.write_output(self.passing()))},
                        self.calls(self.write_output(self.passing())), self.done()])
        with mock.patch.object(wc.Conductor, "_workshop_timeout",
                               side_effect=[(1, "lineage_timeout_s"),
                                            (600, "lineage_timeout_s")]):
            outcome = self.c.run_substep(self.refs, self.PHASE, self.SUBSTEP)
        self.assertEqual(outcome.status, "pass", outcome)
        requests = [c[1]["--request-json"] for c in self.c.calls if c[0] == "record-launch"]
        self.assertEqual([r["workshop_lineage_id"] for r in requests], ["child-1", "child-2"])
        record = workshop_tree.read_lineage_record(workshop_tree.LineagePaths(
            homes_root=self.c.homes, orchestration_id=_ORCH, lineage_id="child-1",
            orchestration_dir=self.repo / "workspace" / "orchestrations" / _ORCH,
            backend="http"))
        self.assertEqual(record["ended_by"], "lineage_timeout")


    def test_a_cut_last_reply_makes_an_unfinished_review(self) -> None:
        """Round 1 of PR-J3-b: the judge writes a provisional `pass`, then its last reply is
        cut at the output limit. That `pass` is not accepted: the review is run again from the
        seeds alone, as for a launch its bound ended."""
        self.conductor([self.calls(self.write_output(self.passing())),
                        self.cut("o2", "write_file", {"path": self.OUTPUT, "content": "{}"}),
                        self.calls(self.write_output(self.passing())), self.done()])
        events: list = []
        with mock.patch.object(_HttpReviewerFake, "emit",
                               lambda _self, name, **kw: events.append((name, kw))):
            outcome = self.c.run_substep(self.refs, self.PHASE, self.SUBSTEP)
        self.assertEqual(outcome.status, "pass", outcome)
        requests = [c[1]["--request-json"] for c in self.c.calls if c[0] == "record-launch"]
        self.assertEqual([r["workshop_lineage_id"] for r in requests], ["child-1", "child-2"])
        self.assertFalse(requests[1].get("warm_resume"))
        [failed] = [kw for name, kw in events if name.endswith("_attempt_failed")]
        self.assertIn("cut at the output limit", failed["detail"])
        # The cut lineage is not left open: nothing continues it, and its conversation goes.
        first = workshop_tree.LineagePaths(
            homes_root=self.c.homes, orchestration_id=_ORCH, lineage_id="child-1",
            orchestration_dir=self.repo / "workspace" / "orchestrations" / _ORCH,
            backend="http")
        self.assertEqual(workshop_tree.read_lineage_record(first)["ended_by"], "superseded")
        self.assertNotIn("child-1", self.c._workshop_http_histories())

    def test_a_timeout_before_any_tool_call_carries_no_no_tool_call_note(self) -> None:
        self.conductor([{"sleep": 3, "then": self.done("late")},
                        self.calls(self.write_output(self.passing())), self.done()])
        failed_events: list = []
        with mock.patch.object(wc.Conductor, "_workshop_timeout",
                               side_effect=[(1, "lineage_timeout_s"),
                                            (600, "lineage_timeout_s")]), \
                mock.patch.object(_HttpReviewerFake, "emit",
                                  lambda _self, name, **kw: failed_events.append(kw)
                                  if name.endswith("_attempt_failed") else None):
            self.assertEqual(self.c.run_substep(self.refs, self.PHASE, self.SUBSTEP).status,
                             "pass")
        self.assertEqual(len(failed_events), 1)
        self.assertIn("ended by its bound", failed_events[0]["detail"])
        self.assertNotIn("returned no tool call", json.dumps(failed_events))

    def test_cut_replies_to_exhaustion_leave_no_open_lineage(self) -> None:
        """Round 3 of PR-J3-b: every launch writes a provisional `pass` and is then cut; the
        budget runs out and the substep fails — and the last lineage, which no bound ended, is
        ended `superseded` with its conversation forgotten, like the ones before it."""
        cut = self.cut("o2", "write_file", {"path": self.OUTPUT, "content": "{}"})
        self.conductor([self.calls(self.write_output(self.passing())), cut] * 3)
        outcome = self.c.run_substep(self.refs, self.PHASE, self.SUBSTEP)
        self.assertEqual(outcome.status, "fail")
        self.assertEqual(self.c._workshop_http_histories(), {})
        for lineage in ("child-1", "child-2", "child-3"):
            record = workshop_tree.read_lineage_record(workshop_tree.LineagePaths(
                homes_root=self.c.homes, orchestration_id=_ORCH, lineage_id=lineage,
                orchestration_dir=self.repo / "workspace" / "orchestrations" / _ORCH,
                backend="http"))
            self.assertEqual(record["ended_by"], "superseded", lineage)

    def test_a_lineage_ended_by_its_bound_leaves_no_history(self) -> None:
        """Every launch's lineage times out: the review is run again until the budget is spent
        and the substep fails; no conversation of an ended lineage stays in memory — the last
        one is dropped by the turn's own tail, which no machine transition follows."""
        late = {"sleep": 3, "then": self.calls(self.write_output(self.passing()))}
        self.conductor([late, late, late, late])
        with mock.patch.object(wc.Conductor, "_workshop_timeout",
                               return_value=(1, "lineage_timeout_s")):
            outcome = self.c.run_substep(self.refs, self.PHASE, self.SUBSTEP)
        self.assertEqual(outcome.status, "fail")
        self.assertEqual(self.c._workshop_http_histories(), {})


class _GenerateReading(_Reading):
    PHASE = "generate"

    def stage(self, repo: Path) -> wc.NodeRefs:
        return _verify_node(repo)


class _JudgeReading(_Reading):
    PHASE = "validate"
    SUBSTEP = "judge"
    OUTPUT = "review.json"

    def stage(self, repo: Path) -> wc.NodeRefs:
        return _write_run_node(repo)

    def passing(self) -> dict:
        return _review("pass")


class AnthropicCompileVerifyTests(_Reading, unittest.TestCase):
    pass


class ChatCompileVerifyTests(_ChatDialect, _Reading, unittest.TestCase):
    pass


class AnthropicGenerateVerifyTests(_GenerateReading, unittest.TestCase):
    pass


class ChatGenerateVerifyTests(_ChatDialect, _GenerateReading, unittest.TestCase):
    pass


class AnthropicValidateJudgeTests(_JudgeReading, unittest.TestCase):
    pass


class ChatValidateJudgeTests(_ChatDialect, _JudgeReading, unittest.TestCase):
    pass


class OpsTableTests(unittest.TestCase):
    def test_the_producer_specs_freeze_the_table_s_operations(self) -> None:
        """`WORKSHOP_OPS_BY_PAIR` is what the audit compares an HTTP record's surface against;
        the contexts the conductor freezes serve exactly it."""
        from tools import workshop_wrapper as ww
        self.assertEqual(ww.WrapperContext.__dataclass_fields__["ops"].default,
                         ww.WORKSHOP_OPS_BY_PAIR[("generate", "generate")])
        self.assertEqual(set(ww.WORKSHOP_OPS_BY_PAIR), set(pure_leaf.WORKSHOP_PRODUCING_PAIRS))
        self.assertEqual(ww.WORKSHOP_OPS_BY_PAIR[("compile", "generate")], ww.IR_CHECK_OPS)


if __name__ == "__main__":
    unittest.main()
