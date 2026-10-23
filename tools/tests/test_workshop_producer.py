"""The `workshop leaf` producer loop (issue #534 PR-3): `Conductor._run_workshop_producer_substep`,
its seeds, its repair paths, its bounds, and the three workshop templates.

The leaf is a fake `spawn_leaf` that WRITES into the lineage's tree, the way the real CLI's file
tools would, and returns an envelope; the wrapper endpoint is a fake whose `stop()` is observed.
Everything host-side — the seeds, the lineage record, the snapshot, the assembly, the bundle
checks, the artifact writers, `bundle_meta.json` — is real. The launch record is the stubbed
`runtime` of `_PureFakeConductor`; the real `record_launch` validators are driven with a
workshop request by `test_workshop_launch.py`, and the real templates by the rows below.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

import tools.llm_config as lc
import tools.orchestration_runtime as ort
import tools.workflow_conductor as wc
from tools import pure_leaf, workshop_tree
from tools.tests.test_pure_leaf_producer import (
    _PureFakeConductor,
    _SPEC_ID,
    _valid_bundle,
    _write_node,
)

_ORCH = "o"


def _config(repo: Path, extra: str = "") -> lc.LlmConfig:
    path = repo / "llm_workshop.yaml"
    path.write_text(
        "defaults: {provider: claude_cli, model: opus}\n"
        "phases: {generate: {substeps: {generate: {provider: claude_cli, model: opus, "
        f"leaf: workshop, max_wrapper_calls: 30, lineage_timeout_s: 2700{extra}" + "}}}}\n",
        encoding="utf-8")
    return lc.load_llm_config(path)


class _FakeEndpoint:
    def __init__(self, ctx, on_end, log) -> None:
        self.ctx, self.on_end, self.log = ctx, on_end, log
        self.url = "http://127.0.0.1:4242/mcp"
        self.stopped = False
        self.live = 0

    def stop(self) -> None:
        self.stopped = True
        self.log.append(("stop", self.ctx.agent_run_id))
        if self.late_write is not None:
            # What a run still in flight would have written, had stop() not ended it first.
            (self.ctx.paths.tree / self.late_write).write_text("late\n", encoding="utf-8")

    late_write: str | None = None

    def in_flight(self) -> int:
        return self.live


class _WorkshopFakeConductor(_PureFakeConductor):
    """The pure fake, with the lineage home, the wrapper context and the endpoint faked and the
    leaf scripted: `self.leaf_actions[i](conductor, tree, kwargs)` runs as launch i and returns
    a `ProcResult` (default: an empty envelope, rc 0)."""

    homes: Path
    leaf_actions: list
    resumable = True

    def _prepare_workshop_home(self, lineage_id, *, resume):  # type: ignore[override]
        home = self.homes / self.orchestration_id / "claude" / lineage_id
        if resume:
            if not home.is_dir():
                return None
        else:
            home.mkdir(parents=True)
            (home / "config").mkdir()
        return {"homes_root": str(self.homes), "home": str(home),
                "config": str(home / "config"), "tree": str(home / "tree"),
                "lineage_id": lineage_id}

    def _stage_workshop_closure(self, refs, paths):  # type: ignore[override]
        # The closure staging is PR-1's (`test_workshop_tree` / the real-bwrap rows); this
        # fixture's node has no derivation record to stage from.
        self.__dict__.setdefault("staged", []).append(paths.lineage_id)
        return ()

    def _freeze_wrapper_context(self, refs, paths, *, agent_run_id, **kw):  # type: ignore
        self.__dict__.setdefault("frozen", []).append(dict(kw, agent_run_id=agent_run_id))
        return SimpleNamespace(paths=paths, agent_run_id=agent_run_id)

    def _start_wrapper_endpoint(self, ctx, on_end):  # type: ignore[override]
        endpoint = _FakeEndpoint(ctx, on_end, self.__dict__.setdefault("order", []))
        endpoint.late_write = getattr(self, "late_write", None)
        endpoint.live = getattr(self, "live_handlers", 0)
        self.__dict__.setdefault("endpoints", []).append(endpoint)
        return endpoint

    def _workshop_session_resumable(self, paths, session_id):  # type: ignore[override]
        return self.resumable

    def spawn_leaf(self, prompt_text, child_env, entry=None, **kwargs):  # type: ignore
        self.__dict__.setdefault("spawns", []).append(kwargs)
        request = [c for c in self.calls if c[0] == "record-launch"][-1][1]["--request-json"]
        tree = (self.homes / self.orchestration_id / "claude"
                / request["workshop_lineage_id"] / "tree")
        self.__dict__.setdefault("order", []).append(("spawn", kwargs["child_arid"]))
        index = len(self.spawns) - 1
        actions = self.leaf_actions
        action = actions[min(index, len(actions) - 1)] if actions else None
        result = action(self, tree, kwargs) if action is not None else None
        return result if result is not None else wc.ProcResult(0, _envelope(), "")


def _envelope(turns: int = 3) -> str:
    return json.dumps({"result": "done", "is_error": False, "model": "claude-opus-5-5",
                       "usage": {"output_tokens": 10}, "session_id": "s",
                       "num_turns": turns})


def _write_bundle(tree: Path, bundle: dict | None = None, *, manifest: str | None = None) -> None:
    doc = json.loads(json.dumps(bundle or _valid_bundle()))
    for entry in doc["files"]:
        (tree / entry["logical_path"]).write_text(entry.pop("content"), encoding="utf-8")
    (tree / workshop_tree.BUNDLE_MANIFEST_NAME).write_text(
        manifest if manifest is not None else yaml.safe_dump(doc), encoding="utf-8")


def _writes_bundle(c, tree, kwargs):
    _write_bundle(tree)


def _writes_nothing(c, tree, kwargs):
    return None


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        (self.repo / "workspace" / "orchestrations" / _ORCH).mkdir(parents=True)
        self.refs = _write_node(self.repo)
        self.c = _WorkshopFakeConductor(
            repo_root=self.repo, orchestration_id=_ORCH, orchestration_agent_run_id="orch",
            llm_config=_config(self.repo), env={})
        self.c.homes = self.repo.parent / (self.repo.name + "_homes")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.c.homes, ignore_errors=True))
        self.c.leaf_actions = [_writes_bundle]

    def fresh_source(self) -> None:
        """What `run_phase` does before a reopened attempt (`_ensure_fresh_producer_id`): a new
        source directory holding only what the host places in it."""
        runner = self.c._runner_basename(self.refs)
        text = (self.repo / self.refs.source_dir() / "src" / runner).read_text()
        self.refs = wc.NodeRefs(target_id=self.refs.target_id, node_key=self.refs.node_key,
                                spec_path=self.refs.spec_path, ir_id=self.refs.ir_id,
                                pipeline_id=self.refs.pipeline_id,
                                source_id=self.refs.source_id + "_r")
        src = self.repo / self.refs.source_dir() / "src"
        src.mkdir(parents=True)
        (src / runner).write_text(text, encoding="utf-8")

    def run_substep(self, repair=None) -> wc.SubstepOutcome:
        return self.c.run_substep(self.refs, "generate", "generate", repair=repair)

    def requests(self) -> list[dict]:
        return [c[1]["--request-json"] for c in getattr(self.c, "calls", [])
                if c[0] == "record-launch"]

    def paths(self, lineage: str) -> workshop_tree.LineagePaths:
        return workshop_tree.LineagePaths(
            homes_root=self.c.homes, orchestration_id=_ORCH, lineage_id=lineage,
            orchestration_dir=self.repo / "workspace" / "orchestrations" / _ORCH)

    def bundle_meta(self) -> dict:
        return json.loads((self.repo / self.refs.source_dir() / "bundle_meta.json").read_text())


class HappyPathTests(_Base):
    def test_a_tree_holding_a_clean_bundle_passes_and_is_written_as_the_pure_path_writes(
            self) -> None:
        outcome = self.run_substep()
        self.assertEqual(outcome.status, "pass", outcome)
        src = self.repo / self.refs.source_dir() / "src"
        for entry in _valid_bundle()["files"]:
            self.assertEqual((src / entry["logical_path"]).read_text(), entry["content"])
        written = json.loads((self.repo / self.refs.source_dir()
                              / "codegen_bundle.json").read_text())
        self.assertEqual(written, _valid_bundle())
        meta = self.bundle_meta()
        self.assertEqual(meta["result"], "pass")
        self.assertEqual(meta["leaf_mode"], "workshop")
        self.assertEqual(meta["prompt_contract_version"], pure_leaf.PURE_PROMPT_CONTRACT_VERSION)
        self.assertEqual(meta["workshop_prompt_contract_version"],
                         pure_leaf.WORKSHOP_PROMPT_CONTRACT_VERSION)
        row = meta["per_attempt"][0]
        self.assertEqual((row["lineage_id"], row["model_turns"], row["wrapper_calls"],
                          row["ended_by"], row["warm"]), ("child-1", 3, 0, None, False))

    def test_the_launch_is_a_workshop_launch_of_its_own_lineage(self) -> None:
        self.run_substep()
        (request,) = self.requests()
        self.assertEqual(request["leaf_mode"], "workshop")
        self.assertEqual(request["workshop_lineage_id"], "child-1")
        self.assertEqual(request["prompt_contract_version"],
                         pure_leaf.WORKSHOP_PROMPT_CONTRACT_VERSION)
        self.assertIn("pure_context", request)
        (spawn,) = self.c.spawns
        self.assertEqual(spawn["mcp_config_json"],
                         pure_leaf.workshop_mcp_config_json("http://127.0.0.1:4242/mcp"))
        self.assertIsNotNone(spawn["cancel_event"])
        self.assertIsNone(spawn["resume_session_id"])
        # The endpoint was stopped before the tree was read.
        self.assertTrue(self.c.endpoints[0].stopped)

    def test_the_tree_is_seeded_with_every_host_placed_file(self) -> None:
        self.run_substep()
        tree = self.paths("child-1").tree
        runner = self.c._runner_basename(self.refs)
        self.assertEqual((tree / runner).read_text(),
                         (self.repo / self.refs.source_dir() / "src" / runner).read_text())
        self.assertEqual((tree / "tests.md").read_text(),
                         (self.repo / self.refs.spec_path / "tests.md").read_text())
        self.assertEqual(yaml.safe_load((tree / "spec.ir.yaml").read_text()),
                         yaml.safe_load((self.repo / self.refs.ir_ref
                                         / "spec.ir.yaml").read_text()))
        for name in ("harness_capabilities.json", "target_profile.json"):
            self.assertTrue((tree / name).is_file(), name)
        self.assertEqual((tree / "CONTRACT.md").read_text(), "PROMPT")
        frozen = self.c.frozen[0]
        self.assertIn("CONTRACT.md", frozen["seed_names"])
        # Every seed but the contract copy is frozen for the wrapper to re-place.
        self.assertEqual(set(frozen["seed_texts"]),
                         set(frozen["seed_names"]) - {"CONTRACT.md"})
        self.assertEqual(frozen["seed_texts"]["tests.md"], (tree / "tests.md").read_text())
        self.assertIn(runner, frozen["seed_names"])
        self.assertIn(workshop_tree.BUNDLE_MANIFEST_NAME, frozen["reserved_names"])
        self.assertIn(self.c._control_file_basename(self.refs), frozen["reserved_names"])
        self.assertEqual(frozen["max_wrapper_calls"], 30)

    def test_the_snapshot_leaves_out_the_seeds(self) -> None:
        self.run_substep()
        snapshot = self.paths("child-1").snapshot
        names = sorted(p.name for p in snapshot.iterdir())
        self.assertEqual(names, sorted([workshop_tree.BUNDLE_MANIFEST_NAME,
                                        *(e["logical_path"] for e in _valid_bundle()["files"])]))

    def test_the_snapshot_is_taken_after_the_endpoint_stops(self) -> None:
        """A file the endpoint's stop() writes — standing for a run still in flight — is in the
        snapshot, so the snapshot was not taken before the stop."""
        self.c.late_write = "late.txt"
        self.run_substep()
        self.assertTrue((self.paths("child-1").snapshot / "late.txt").is_file())

    def test_a_handler_outliving_the_stop_fails_closed_without_reading_the_tree(self) -> None:
        self.c.live_handlers = 1
        outcome = self.run_substep()
        self.assertEqual(outcome.status, "fail")
        self.assertEqual(outcome.infra_error[0], "workshop_wrapper_not_stopped")
        self.assertFalse(any(self.paths("child-1").snapshot.iterdir()))


class RepairTests(_Base):
    def test_an_empty_tree_is_repaired_warm_over_the_same_lineage(self) -> None:
        self.c.leaf_actions = [_writes_nothing, _writes_bundle]
        outcome = self.run_substep()
        self.assertEqual(outcome.status, "pass", outcome)
        first, second = self.requests()
        self.assertEqual(second["workshop_lineage_id"], "child-1")
        self.assertTrue(second["warm_resume"])
        self.assertEqual(second["repair_target_agent_run_id"], "child-1")
        self.assertIn(workshop_tree.BUNDLE_MANIFEST_NAME, second["repair_findings"])
        self.assertNotIn("pure_context", second)
        self.assertEqual(self.c.spawns[1]["resume_session_id"], "child-1")
        rows = self.bundle_meta()["per_attempt"]
        self.assertEqual(rows[0]["failure_category"], "workshop_tree_empty")
        self.assertEqual([r["lineage_id"] for r in rows], ["child-1", "child-1"])
        self.assertFalse(self.paths("child-2").home.exists())

    def test_a_seed_the_leaf_edited_is_restored_before_a_warm_launch(self) -> None:
        def edits_seed(c, tree, kwargs):
            (tree / "tests.md").write_text("edited\n", encoding="utf-8")

        seen: list[str] = []

        def reads_seed(c, tree, kwargs):
            seen.append((tree / "tests.md").read_text())
            _write_bundle(tree)

        self.c.leaf_actions = [edits_seed, reads_seed]
        self.run_substep()
        self.assertEqual(seen, [(self.repo / self.refs.spec_path / "tests.md").read_text()])

    def test_an_ended_lineage_is_repaired_by_a_cold_reuse_seeded_from_its_snapshot(
            self) -> None:
        def ends_by_the_cap(c, tree, kwargs):
            (tree / "draft.txt").write_text("my work\n", encoding="utf-8")
            (tree / workshop_tree.BUNDLE_MANIFEST_NAME).write_text("[not a mapping]\n")
            paths = self.paths("child-1")
            workshop_tree.end_lineage(paths, "max_wrapper_calls")
            kwargs["cancel_event"].set()
            return wc.ProcResult(1, "", wc.LEAF_CANCELLED_MARKER)

        seen: dict = {}

        def finishes(c, tree, kwargs):
            seen["draft"] = (tree / "draft.txt").read_text()
            seen["runner"] = (tree / c._runner_basename(self.refs)).is_file()
            _write_bundle(tree)

        self.c.leaf_actions = [ends_by_the_cap, finishes]
        outcome = self.run_substep()
        self.assertEqual(outcome.status, "pass", outcome)
        first, second = self.requests()
        self.assertEqual(second["workshop_lineage_id"], "child-2")
        self.assertFalse(second.get("warm_resume"))
        self.assertIn("pure_context", second)
        self.assertEqual(second["repair_target_agent_run_id"], "child-1")
        self.assertIn("max_wrapper_calls", second["repair_findings"])
        self.assertIsNone(self.c.spawns[1]["resume_session_id"])
        self.assertEqual(seen, {"draft": "my work\n", "runner": True})
        rows = self.bundle_meta()["per_attempt"]
        self.assertEqual(rows[0]["ended_by"], "max_wrapper_calls")
        self.assertEqual(rows[0]["failure_category"], "bundle_schema_violation")

    def test_a_transcript_gone_from_the_lineage_home_takes_a_cold_reuse(self) -> None:
        self.c.resumable = False
        self.c.leaf_actions = [_writes_nothing, _writes_bundle]
        self.run_substep()
        second = self.requests()[1]
        self.assertEqual(second["workshop_lineage_id"], "child-2")
        self.assertFalse(second.get("warm_resume"))

    def test_an_entry_without_warm_resume_takes_a_cold_reuse(self) -> None:
        self.c = _WorkshopFakeConductor(
            repo_root=self.repo, orchestration_id=_ORCH, orchestration_agent_run_id="orch",
            llm_config=_config(self.repo, ", capabilities: [workshop]"), env={})
        self.c.homes = self.repo.parent / (self.repo.name + "_homes")
        self.c.leaf_actions = [_writes_nothing, _writes_bundle]
        self.assertEqual(self.run_substep().status, "pass")
        self.assertEqual(self.requests()[1]["workshop_lineage_id"], "child-2")

    def test_a_file_at_a_reserved_name_is_a_schema_violation(self) -> None:
        bad = _valid_bundle()
        bad["files"][0]["logical_path"] = "tests.md"

        def writes_bad(c, tree, kwargs):
            _write_bundle(tree, bad)

        self.c.leaf_actions = [writes_bad]
        outcome = self.run_substep()
        self.assertEqual(outcome.status, "fail")
        meta = self.bundle_meta()
        self.assertEqual(meta["failure_category"], "bundle_schema_violation")
        self.assertEqual(meta["attempts"], 1 + pure_leaf.MAX_BUNDLE_REPAIR_TURNS)
        self.assertIn("host-placed", meta["failure_excerpt"])

    def test_the_spec_s_own_checks_judge_the_assembled_document(self) -> None:
        bad = _valid_bundle()
        del bad["capability_requirements"]

        def writes_bad(c, tree, kwargs):
            _write_bundle(tree, bad)

        self.c.leaf_actions = [writes_bad]
        self.run_substep()
        self.assertEqual(self.bundle_meta()["failure_category"], "bundle_schema_violation")
        self.assertIn("capability_requirements", self.bundle_meta()["failure_excerpt"])

    def test_an_exhausted_empty_tree_routes_to_a_generate_reuse(self) -> None:
        self.c.leaf_actions = [_writes_nothing]
        outcome = self.run_substep()
        self.assertEqual(outcome.status, "fail")
        self.assertIsNone(outcome.infra_error)
        self.assertEqual(self.bundle_meta()["failure_category"], "workshop_tree_empty")
        self.assertEqual(wc.GENERATE_BUNDLE_FAILURE_ROUTING["workshop_tree_empty"],
                         ("generate", "reuse"))


class EndOfLineageTests(_Base):
    def test_a_dead_leaf_is_a_transport_failure_and_ends_its_lineage(self) -> None:
        self.c.leaf_actions = [lambda c, tree, kw: wc.ProcResult(1, "", "boom")]
        outcome = self.run_substep()
        self.assertEqual(outcome.status, "fail")
        self.assertEqual(outcome.leaf_returncode, 1)
        self.assertEqual(len(self.requests()), 1)
        record = workshop_tree.read_lineage_record(self.paths("child-1"))
        self.assertEqual(record["ended_by"], "leaf_exit")
        self.assertEqual(self.bundle_meta()["failure_category"], "pure_transport")

    def test_a_transient_death_is_retried_in_a_new_lineage_from_the_snapshot(self) -> None:
        def dies_after_a_draft(c, tree, kwargs):
            (tree / "draft.txt").write_text("kept\n", encoding="utf-8")
            return wc.ProcResult(1, "", "API Error: 529 overloaded_error")

        seen: list[str] = []

        def finishes(c, tree, kwargs):
            seen.append((tree / "draft.txt").read_text())
            _write_bundle(tree)

        self.c.leaf_actions = [dies_after_a_draft, finishes]
        with mock.patch.object(self.c, "_pure_transient_retry", return_value=True), \
                mock.patch("time.sleep"):
            outcome = self.run_substep()
        self.assertEqual(outcome.status, "pass", outcome)
        self.assertEqual(seen, ["kept\n"])
        self.assertEqual(self.requests()[1]["workshop_lineage_id"], "child-2")

    def test_a_leaf_killed_at_the_lineage_bound_is_judged_by_its_tree(self) -> None:
        def finishes_then_times_out(c, tree, kwargs):
            _write_bundle(tree)
            return wc.ProcResult(-9, "", "[conductor] leaf_timeout", timed_out=True)

        self.c.leaf_actions = [finishes_then_times_out]
        with mock.patch.object(wc, "_leaf_timeout_seconds", return_value=0):
            outcome = self.run_substep()
        self.assertEqual(outcome.status, "pass", outcome)
        self.assertEqual(self.c.spawns[0]["timeout_name"], "lineage_timeout_s")
        record = workshop_tree.read_lineage_record(self.paths("child-1"))
        self.assertEqual(record["ended_by"], "lineage_timeout")

    def test_a_leaf_killed_at_the_environment_cap_ends_its_lineage_as_leaf_timeout(
            self) -> None:
        self.c.leaf_actions = [
            lambda c, tree, kw: wc.ProcResult(-9, "", "[conductor] leaf_timeout",
                                              timed_out=True),
            _writes_bundle]
        with mock.patch.object(wc, "_leaf_timeout_seconds", return_value=60):
            self.run_substep()
        self.assertEqual(self.c.spawns[0]["timeout_name"], wc.LEAF_TIMEOUT_ENV_BOUND_NAME)
        self.assertEqual(self.c.spawns[0]["timeout_seconds"], 60)
        record = workshop_tree.read_lineage_record(self.paths("child-1"))
        self.assertEqual(record["ended_by"], "leaf_timeout")
        # Ended, so the repair is a cold reuse, not a warm turn on an ended lineage.
        self.assertEqual(self.requests()[1]["workshop_lineage_id"], "child-2")

    def test_the_bound_is_the_smaller_of_the_cap_and_the_remaining_lineage_time(self) -> None:
        import time
        deadline = time.time() + 100
        with mock.patch.object(wc, "_leaf_timeout_seconds", return_value=0):
            seconds, name = self.c._workshop_timeout(deadline)
        self.assertEqual(name, "lineage_timeout_s")
        self.assertTrue(98 <= seconds <= 100)
        with mock.patch.object(wc, "_leaf_timeout_seconds", return_value=50):
            self.assertEqual(self.c._workshop_timeout(deadline),
                             (50, wc.LEAF_TIMEOUT_ENV_BOUND_NAME))
        with mock.patch.object(wc, "_leaf_timeout_seconds", return_value=500):
            self.assertEqual(self.c._workshop_timeout(deadline)[1], "lineage_timeout_s")
        with mock.patch.object(wc, "_leaf_timeout_seconds", return_value=0):
            self.assertEqual(self.c._workshop_timeout(time.time() - 5)[0], 1)

    def test_a_lineage_with_no_time_left_is_not_resumed(self) -> None:
        """The warm repair's lineage ran out of `lineage_timeout_s` between launches: it is
        ended `lineage_timeout` and the repair takes a cold reuse."""
        self.c.leaf_actions = [_writes_nothing, _writes_bundle]
        real_open = wc.Conductor._open_workshop_lineage

        def short_lived(conductor, refs, entry, lineage_id, seeds, prior):
            lineage = real_open(conductor, refs, entry, lineage_id, seeds, prior)
            return lineage._replace(deadline=0.0) if lineage_id == "child-1" else lineage

        with mock.patch.object(wc.Conductor, "_open_workshop_lineage", short_lived), \
                mock.patch.object(wc, "_leaf_timeout_seconds", return_value=0):
            outcome = self.run_substep()
        self.assertEqual(outcome.status, "pass", outcome)
        # The arid of the turn that found the lineage out of time starts the new one.
        self.assertEqual(self.requests()[1]["workshop_lineage_id"], "child-2")
        record = workshop_tree.read_lineage_record(self.paths("child-1"))
        self.assertEqual(record["ended_by"], "lineage_timeout")


class SpentBudgetTests(_Base):
    def test_a_lineage_that_spent_every_call_is_not_resumed(self) -> None:
        """A leaf that used exactly `max_wrapper_calls` and exited leaves its lineage open with
        nothing left; the repair ends it `max_wrapper_calls` and takes a cold reuse rather than a
        warm turn whose first call would be refused."""
        def spends_the_budget(c, tree, kwargs):
            paths = self.paths(kwargs["child_arid"])
            for _ in range(30):
                self.assertTrue(workshop_tree.count_wrapper_call(paths, 30))

        self.c.leaf_actions = [spends_the_budget, _writes_bundle]
        self.assertEqual(self.run_substep().status, "pass")
        second = self.requests()[1]
        self.assertEqual(second["workshop_lineage_id"], "child-2")
        self.assertFalse(second.get("warm_resume"))
        record = workshop_tree.read_lineage_record(self.paths("child-1"))
        self.assertEqual(record["ended_by"], "max_wrapper_calls")
        rows = self.bundle_meta()["per_attempt"]
        self.assertEqual((rows[0]["wrapper_calls"], rows[1]["wrapper_calls"]), (30, 0))

    def test_a_lineage_with_calls_left_is_resumed(self) -> None:
        def spends_some(c, tree, kwargs):
            paths = self.paths(kwargs["child_arid"])
            for _ in range(29):
                workshop_tree.count_wrapper_call(paths, 30)

        self.c.leaf_actions = [spends_some, _writes_bundle]
        self.run_substep()
        self.assertEqual(self.requests()[1]["workshop_lineage_id"], "child-1")
        # Per attempt: the warm turn on the same record counts its own calls, not the
        # lineage's running total.
        self.assertEqual([r["wrapper_calls"] for r in self.bundle_meta()["per_attempt"]],
                         [29, 0])


class OuterReopenTests(_Base):
    def _reopen(self, target: str) -> wc.SubstepOutcome:
        evidence = self.c.__dict__.setdefault("evidence", {})
        self.fresh_source()

        def read(repo_root, oid, *, agent_run_id):
            return evidence.get(f"{agent_run_id}.request.input.json")

        with mock.patch.object(ort, "_read_launch_request_payload", read):
            return self.run_substep(repair={
                "repair_strategy": "reuse", "repair_target_agent_run_id": target,
                "repair_findings": "lint: unused variable x", "issue_severity": "major",
                "repair_reason": "generate_gate_lint"})

    def test_an_open_lineage_is_reopened_warm(self) -> None:
        self.assertEqual(self.run_substep().status, "pass")
        self.c.leaf_actions = [_writes_bundle]
        self.assertEqual(self._reopen("child-1").status, "pass")
        request = self.requests()[-1]
        self.assertEqual(request["workshop_lineage_id"], "child-1")
        self.assertTrue(request["warm_resume"])
        self.assertEqual(request["repair_findings"], "lint: unused variable x")
        self.assertEqual(self.c.spawns[-1]["resume_session_id"], "child-1")

    def test_an_ended_lineage_is_reopened_by_a_cold_reuse(self) -> None:
        self.assertEqual(self.run_substep().status, "pass")
        workshop_tree.end_lineage(self.paths("child-1"), "leaf_timeout")
        seen: list[bool] = []

        def checks_snapshot(c, tree, kwargs):
            seen.append((tree / workshop_tree.BUNDLE_MANIFEST_NAME).is_file())
            _write_bundle(tree)

        self.c.leaf_actions = [checks_snapshot]
        self.assertEqual(self._reopen("child-1").status, "pass")
        request = self.requests()[-1]
        self.assertEqual(request["workshop_lineage_id"], "child-2")
        self.assertFalse(request.get("warm_resume"))
        self.assertEqual(request["repair_target_agent_run_id"], "child-1")
        self.assertEqual(seen, [True])

    def test_a_target_with_no_lineage_starts_a_fresh_one_carrying_the_findings(self) -> None:
        self.c.leaf_actions = [_writes_bundle]
        self.assertEqual(self._reopen("nobody").status, "pass")
        request = self.requests()[-1]
        self.assertEqual(request["workshop_lineage_id"], "child-1")
        self.assertEqual(request["repair_findings"], "lint: unused variable x")

    def test_another_node_s_lineage_is_not_continued(self) -> None:
        self.assertEqual(self.run_substep().status, "pass")
        paths = self.paths("child-1")
        record = workshop_tree.read_lineage_record(paths)
        record["node_key"] = "problem/other@0.1.0"
        paths.record_json.write_text(json.dumps(record), encoding="utf-8")
        seen: list[bool] = []

        def checks_fresh(c, tree, kwargs):
            seen.append((tree / workshop_tree.BUNDLE_MANIFEST_NAME).exists())
            _write_bundle(tree)

        self.c.leaf_actions = [checks_fresh]
        self.assertEqual(self._reopen("child-1").status, "pass")
        self.assertEqual(self.requests()[-1]["workshop_lineage_id"], "child-2")
        self.assertEqual(seen, [False])

    def test_a_restart_seeds_a_fresh_tree(self) -> None:
        self.assertEqual(self.run_substep().status, "pass")
        seen: list[bool] = []

        def checks_fresh(c, tree, kwargs):
            seen.append((tree / workshop_tree.BUNDLE_MANIFEST_NAME).exists())
            _write_bundle(tree)

        self.c.leaf_actions = [checks_fresh]
        self.fresh_source()
        self.run_substep(repair={"repair_strategy": "restart",
                                 "repair_target_agent_run_id": "child-1",
                                 "issue_severity": "major", "repair_reason": "verify"})
        self.assertEqual(seen, [False])
        self.assertNotIn("repair_findings", self.requests()[-1])


class SeedTests(_Base):
    def test_a_context_key_with_no_tree_name_is_refused(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            self.c._workshop_seeds(self.refs, {"new_document": "x"}, {})
        self.assertIn("new_document", str(ctx.exception))

    def test_every_pure_context_key_of_the_generate_producer_has_a_tree_name(self) -> None:
        for shape in ("", "harness"):
            key = ("generate", "generate", shape) if shape else ("generate", "generate")
            keys = (ort.PURE_CONTEXT_REQUIRED_KEYS_BY_SHAPE[key] if shape
                    else ort.PURE_CONTEXT_REQUIRED_KEYS[key])
            with self.subTest(shape=shape):
                self.assertEqual(
                    set(keys) - set(self.c._WORKSHOP_DOCUMENT_SEEDS) - {"runner_document"},
                    set())

    def test_the_node_s_own_interface_header_is_a_seed(self) -> None:
        with mock.patch.object(wc.Conductor, "_interface_header_text",
                               return_value=("node_model.h", "// header\n")):
            seeds = self.c._workshop_seeds(self.refs, self.c._build_pure_context(self.refs), {})
        self.assertEqual(seeds["node_model.h"], "// header\n")

    def test_every_file_the_host_placed_in_src_is_a_seed(self) -> None:
        src = self.repo / self.refs.source_dir() / "src"
        (src / "dep_model.h").write_text("// dep\n", encoding="utf-8")
        (src / self.c._control_file_basename(self.refs)).write_text("x", encoding="utf-8")
        seeds = self.c._workshop_seeds(self.refs, self.c._build_pure_context(self.refs), {})
        self.assertEqual(seeds["dep_model.h"], "// dep\n")
        self.assertNotIn(self.c._control_file_basename(self.refs), seeds)


# --- the templates ---------------------------------------------------------------------

_TEMPLATES = ("workshop_generate_generate.txt", "workshop_generate_generate_harness.txt",
              "workshop_bundle_repair.txt")
_DIR = Path(ort.__file__).resolve().parent / "prompt_templates"

#: `WORKSHOP_PROMPT_CONTRACT_VERSION` -> sha256 over the three templates and
#: `WORKSHOP_SYSTEM_PROMPT`. A change to either is a change of the workshop leaf's contract and
#: takes a new version, keeping every shipped pin (the no-revert check reads them).
_WORKSHOP_CONTRACT_DIGESTS = {
    "workshop-1": "7b815a1b6c0d4639012927da03a9b67390c0d88047e743ad85cd356c8b09f331",
}


def _workshop_digest() -> str:
    h = hashlib.sha256()
    for name in _TEMPLATES:
        h.update(name.encode() + b"\0" + (_DIR / name).read_bytes() + b"\0")
    h.update(pure_leaf.WORKSHOP_SYSTEM_PROMPT.encode("utf-8"))
    return h.hexdigest()


class TemplateTests(unittest.TestCase):
    def test_the_workshop_contract_is_pinned_to_its_version(self) -> None:
        self.assertEqual(_WORKSHOP_CONTRACT_DIGESTS[pure_leaf.WORKSHOP_PROMPT_CONTRACT_VERSION],
                         _workshop_digest(),
                         "a workshop template or WORKSHOP_SYSTEM_PROMPT changed: bump "
                         "WORKSHOP_PROMPT_CONTRACT_VERSION and pin the new digest")
        self.assertEqual(len(set(_WORKSHOP_CONTRACT_DIGESTS.values())),
                         len(_WORKSHOP_CONTRACT_DIGESTS))

    def test_each_opens_with_the_workshop_sentinel(self) -> None:
        for name in _TEMPLATES:
            with self.subTest(template=name):
                self.assertTrue((_DIR / name).read_text().startswith(
                    pure_leaf.WORKSHOP_PROMPT_SENTINEL))

    def test_the_producer_rules_are_the_pure_templates_verbatim(self) -> None:
        """Every line of a pure producer template after its persona — the output contract, the
        rules, the input labels, the language and parallel markers, the identity block — is in
        its workshop twin, in order, except the document slots, which the twin replaces with
        the seed's tree name. So a rule edited in one and not the other fails here (and the
        bundle_meta's `prompt_contract_version` keeps naming the rules both answer)."""
        for pure_name, workshop_name in (
                ("pure_generate_generate.txt", "workshop_generate_generate.txt"),
                ("pure_generate_generate_harness.txt",
                 "workshop_generate_generate_harness.txt")):
            with self.subTest(template=workshop_name):
                pure = (_DIR / pure_name).read_text().split("\n")[2:]
                workshop = (_DIR / workshop_name).read_text().split("\n")
                kept = [ln for ln in pure if not ort._PURE_PLACEHOLDER_RE.fullmatch(ln)]
                position = 0
                for line in kept:
                    self.assertIn(line, workshop[position:], line[:80])
                    position = workshop.index(line, position) + 1

    def test_no_document_is_inlined(self) -> None:
        documents = set(wc.Conductor._WORKSHOP_DOCUMENT_SEEDS) | {
            "runner_document", "dependency_facts", "exemplar"}
        for name in _TEMPLATES[:2]:
            with self.subTest(template=name):
                slots = set(ort._PURE_PLACEHOLDER_RE.findall((_DIR / name).read_text()))
                self.assertEqual(slots & documents, set())

    def test_each_seed_name_the_templates_cite_is_one_the_loop_places(self) -> None:
        text = (_DIR / "workshop_generate_generate.txt").read_text()
        for name in ("harness_capabilities.json", "target_profile.json", "spec.ir.yaml",
                     "tests.md", "DEPENDENCIES.md", "EXEMPLAR.md", "CONTRACT.md",
                     workshop_tree.BUNDLE_MANIFEST_NAME):
            self.assertIn(f"`{name}`", text)
        placed = set(wc.Conductor._WORKSHOP_DOCUMENT_SEEDS.values()) | {
            wc.Conductor._WORKSHOP_DEPENDENCIES_SEED, wc.Conductor._WORKSHOP_EXEMPLAR_SEED,
            wc.Conductor._WORKSHOP_CONTRACT_SEED}
        harness = (_DIR / "workshop_generate_generate_harness.txt").read_text()
        for name in placed:
            self.assertTrue(f"`{name}`" in text or f"`{name}`" in harness, name)
        for op in ("derive", "compile", "lint", "syntax_check", "run"):
            self.assertIn(f"`{op}`", text)

    def _render(self, **overrides) -> str:
        from tools.tests.test_workshop_launch import _workshop_request
        req = _workshop_request("ar_x", pure_language="fortran", pure_parallel="openmp",
                                **overrides)
        return ort.render_launch_prompt_text(ort.prepare_launch_request_payload(req))

    def test_a_workshop_request_renders_through_the_production_entry_point(self) -> None:
        prompt = self._render()
        self.assertTrue(prompt.startswith(pure_leaf.WORKSHOP_PROMPT_SENTINEL))
        self.assertNotIn("{{", prompt)
        for marker in ort._required_launch_prompt_markers(
                ort.prepare_launch_request_payload(
                    __import__("tools.tests.test_workshop_launch", fromlist=["x"])
                    ._workshop_request("ar_x"))):
            self.assertIn(marker, prompt)
        ort._validate_launch_prompt_text(
            ort.prepare_launch_request_payload(
                __import__("tools.tests.test_workshop_launch", fromlist=["x"])
                ._workshop_request("ar_x", pure_language="fortran", pure_parallel="openmp")),
            prompt)

    def test_a_cold_repair_restates_the_launch_body_and_a_warm_one_does_not(self) -> None:
        cold = self._render(repair_findings="fix it", repair_strategy="reuse",
                            repair_target_agent_run_id="ar_w")
        warm = self._render(repair_findings="fix it", repair_strategy="reuse",
                            repair_target_agent_run_id="ar_w", warm_resume=True)
        for prompt in (cold, warm):
            self.assertTrue(prompt.startswith(pure_leaf.WORKSHOP_PROMPT_SENTINEL))
            self.assertIn("fix it", prompt)
        self.assertIn("Work tree contract", cold)
        self.assertNotIn("Work tree contract", warm)


if __name__ == "__main__":
    unittest.main()


class AuditTests(unittest.TestCase):
    def test_a_workshop_bundle_meta_is_summarized_with_its_lineages_calls_and_turns(
            self) -> None:
        from tools.audit_orchestration import _render_pure_leaf_row
        from tools.orchestration_diagnostics import _summarize_one_pure_meta
        meta = {"result": "pass", "attempts": 2, "per_attempt": [
            {"lineage_id": "a1", "wrapper_calls": 4, "model_turns": 9,
             "failure_category": "workshop_tree_empty"},
            {"lineage_id": "a1", "wrapper_calls": 3, "model_turns": 5}],
            "leaf_mode": "workshop", "prompt_contract_version": "x"}
        row = _summarize_one_pure_meta(meta)
        self.assertEqual((row["lineages"], row["wrapper_calls"], row["model_turns"]),
                         (["a1"], 7, 14))
        lines: list[str] = []
        _render_pure_leaf_row("generate", row, lines)
        self.assertIn("workshop leaf — lineages 1 (`a1`), wrapper calls 7, model turns 14",
                      "\n".join(lines))
        pure = _summarize_one_pure_meta({**meta, "leaf_mode": None})
        self.assertNotIn("lineages", pure)


class TimeoutEventTests(_Base):
    def test_the_workshop_launch_labels_its_timeout_event(self) -> None:
        self.run_substep()
        self.assertEqual(self.c.spawns[0]["timeout_context"]["leaf_mode"], "workshop")
        events: list[tuple[str, dict]] = []
        with mock.patch.object(self.c, "emit", lambda e, **kw: events.append((e, kw))):
            self.c._timed_out_result(-9, "", "", timeout_seconds=5, started=0.0,
                                     context=self.c.spawns[0]["timeout_context"],
                                     timeout_name="lineage_timeout_s")
            self.c._timed_out_result(-9, "", "", timeout_seconds=5, started=0.0,
                                     context={"step": "generate"})
        self.assertEqual(events[0][1]["leaf_mode"], "workshop")
        self.assertNotIn("leaf_mode", events[1][1])


class SetupFailureTests(_Base):
    def test_a_wrapper_context_that_cannot_be_frozen_fails_closed_unlaunched(self) -> None:
        with mock.patch.object(_WorkshopFakeConductor, "_freeze_wrapper_context",
                               side_effect=RuntimeError("no static lint preset")):
            outcome = self.run_substep()
        self.assertEqual(outcome.status, "fail")
        self.assertEqual(outcome.infra_error[0], "workshop_lineage_setup_failed")
        self.assertIn("no static lint preset", outcome.infra_error[1])
        self.assertEqual(self.requests(), [])
        self.assertFalse(hasattr(self.c, "spawns"))

    def test_an_outer_reopen_whose_seeds_cannot_be_rendered_fails_closed(self) -> None:
        self.assertEqual(self.run_substep().status, "pass")
        evidence = self.c.__dict__["evidence"]
        with mock.patch.object(ort, "_read_launch_request_payload",
                               lambda r, o, *, agent_run_id:
                               evidence.get(f"{agent_run_id}.request.input.json")), \
                mock.patch.object(_WorkshopFakeConductor, "_workshop_seeds",
                                  side_effect=RuntimeError("seed boom")):
            outcome = self.run_substep(repair={
                "repair_strategy": "reuse", "repair_target_agent_run_id": "child-1",
                "repair_findings": "x", "issue_severity": "major", "repair_reason": "r"})
        self.assertEqual(outcome.infra_error[0], "workshop_lineage_setup_failed")
        self.assertIn("seed boom", outcome.infra_error[1])


_EXEMPLAR = {"node_key": "problem/sibling@0.1.0", "source_ref": "workspace/pipelines/sib/src_1",
             "sources": [{"filename": "sibling_model.f90", "text": "module m\nend module\n"}]}


class ExemplarRecordTests(_Base):
    """Every attempt whose tree holds `EXEMPLAR.md` records the exemplar, in its
    `per_attempt[].exemplar_ref` and on its launch request — a repair turn included."""

    def _run(self, actions) -> None:
        self.c.leaf_actions = actions
        with mock.patch.object(_WorkshopFakeConductor, "_resolve_exemplar",
                               return_value=_EXEMPLAR):
            self.assertEqual(self.run_substep().status, "pass")

    def test_a_warm_repair_records_the_exemplar_its_tree_holds(self) -> None:
        self._run([_writes_nothing, _writes_bundle])
        rows = self.bundle_meta()["per_attempt"]
        self.assertEqual([r["exemplar_ref"] for r in rows], [_EXEMPLAR["source_ref"]] * 2)
        self.assertTrue((self.paths("child-1").tree / "EXEMPLAR.md").is_file())
        self.assertEqual([r.get("exemplar") for r in self.requests()], [_EXEMPLAR] * 2)

    def test_a_cold_reuse_reseeds_and_records_the_exemplar(self) -> None:
        self._run([lambda c, t, kw: wc.ProcResult(-9, "", "x", timed_out=True),
                   _writes_bundle])
        self.assertIn("sibling_model.f90",
                      (self.paths("child-2").tree / "EXEMPLAR.md").read_text())
        rows = self.bundle_meta()["per_attempt"]
        self.assertEqual([r["exemplar_ref"] for r in rows], [_EXEMPLAR["source_ref"]] * 2)

    def test_no_exemplar_no_seed_no_record(self) -> None:
        self.run_substep()
        self.assertFalse((self.paths("child-1").tree / "EXEMPLAR.md").exists())
        self.assertIsNone(self.bundle_meta()["per_attempt"][0]["exemplar_ref"])

    def test_each_request_the_loop_builds_passes_the_real_validator(self) -> None:
        """The cold, warm and cold-reuse requests, through the real request validator and
        renderer (the fake runtime does not run them)."""
        self._run([_writes_nothing, lambda c, t, kw: wc.ProcResult(-9, "", "x", timed_out=True),
                   _writes_bundle])
        requests = self.requests()
        self.assertEqual([(r["workshop_lineage_id"], bool(r.get("warm_resume")))
                          for r in requests],
                         [("child-1", False), ("child-1", True), ("child-3", False)])
        for request in requests:
            payload = ort.prepare_launch_request_payload(dict(request))
            ort._validate_launch_request_payload(payload)
            prompt = ort.render_launch_prompt_text(payload)
            ort._validate_launch_prompt_text(payload, prompt)
            self.assertTrue(prompt.startswith(pure_leaf.WORKSHOP_PROMPT_SENTINEL))
