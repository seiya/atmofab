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

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

import tools.llm_config as lc
import tools.orchestration_runtime as ort
import tools.workflow_conductor as wc
from tools.tests import llm_samples
from tools import leaf_contract, workshop_tree
from tools.tests.test_leaf_producer import (
    _PureFakeConductor,
    _SPEC_ID,
    _valid_bundle,
    _write_node,
)

_ORCH = "o"


def _config(repo: Path, extra: str = "") -> lc.LlmConfig:
    path = repo / "llm_workshop.yaml"
    path.write_text(
        llm_samples.workshop_yaml(
            "defaults: {lineage_timeout_s: 2700, provider: claude_cli, model: opus}\n"
            "phases: {generate: {substeps: {generate: {provider: claude_cli, model: opus, "
            f"max_wrapper_calls: 30, lineage_timeout_s: 2700{extra}" + "}}}}\n"),
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
    """`_PureFakeConductor`, with the lineage home, the wrapper context and the endpoint faked and the
    leaf scripted: `self.leaf_actions[i](conductor, tree, kwargs)` runs as launch i and returns
    a `ProcResult` (default: an empty envelope, rc 0)."""

    homes: Path
    leaf_actions: list
    resumable = True
    #: How many launches past the end of `leaf_actions` the fake serves before raising.
    MAX_SPAWNS_PAST_SCRIPT = 8

    def _prepare_workshop_home(self, entry, lineage_id, *, resume):  # type: ignore[override]
        home = self.homes / self.orchestration_id / self._workshop_backend(entry) / lineage_id
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

    numbered_prompts = False
    #: When true, a WARM record-launch answers `workshop_lineage_home_missing`.
    home_missing_on_warm = False

    def runtime(self, args, *, input=None):  # type: ignore[override]
        self.__dict__.setdefault("argvs", []).append(list(args))
        if (args[0] == "record-launch" and self.home_missing_on_warm
                and "--workshop-lineage-id" in args):
            return {"workshop_lineage_home_missing": True}
        result = super().runtime(args, input=input)
        if args[0] == "record-launch" and self.numbered_prompts:
            n = sum(1 for a in self.argvs if a[0] == "record-launch")
            return {"launch_prompt_text": f"PROMPT-{n}"}
        return result

    def spawn_leaf(self, prompt_text, child_env, entry=None, **kwargs):  # type: ignore
        self.__dict__.setdefault("spawns", []).append(kwargs)
        request = [c for c in self.calls if c[0] == "record-launch"][-1][1]["--request-json"]
        tree = (self.homes / self.orchestration_id / self._workshop_backend(entry)
                / request["workshop_lineage_id"] / "tree")
        self.__dict__.setdefault("order", []).append(("spawn", kwargs["child_arid"]))
        index = len(self.spawns) - 1
        actions = self.leaf_actions
        if index >= len(actions) + self.MAX_SPAWNS_PAST_SCRIPT:
            # A loop that lost a budget would re-launch the repeated last action forever; name
            # it instead of hanging (a hang is neither a kill nor a survivor).
            raise AssertionError(f"unbounded re-launch: {index + 1} launches for a script of "
                                 f"{len(actions)}")
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
                                source_id="src_20260715_002")
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
    def test_a_tree_holding_a_clean_bundle_passes_and_is_written(
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
        self.assertEqual(meta["prompt_contract_version"], leaf_contract.LEAF_PROMPT_CONTRACT_VERSION)
        # One version since issue #534 PR-J4-b: the retired workshop counter is not written.
        self.assertNotIn("workshop_prompt_contract_version", meta)
        row = meta["per_attempt"][0]
        self.assertEqual((row["lineage_id"], row["model_turns"], row["wrapper_calls"],
                          row["ended_by"], row["warm"]), ("child-1", 3, 0, None, False))
        self.assertIsInstance(row["leaf_wall_s"], float)

    def test_the_launch_is_a_workshop_launch_of_its_own_lineage(self) -> None:
        self.run_substep()
        (request,) = self.requests()
        self.assertEqual(request["leaf_mode"], "workshop")
        self.assertEqual(request["workshop_lineage_id"], "child-1")
        self.assertEqual(request["prompt_contract_version"],
                         leaf_contract.LEAF_PROMPT_CONTRACT_VERSION)
        self.assertNotIn("workshop_prompt_contract_version", request)
        self.assertIn("pure_context", request)
        (spawn,) = self.c.spawns
        self.assertEqual(spawn["mcp_config_json"],
                         leaf_contract.workshop_mcp_config_json("http://127.0.0.1:4242/mcp"))
        self.assertIsNotNone(spawn["cancel_event"])
        self.assertIsNone(spawn["resume_session_id"])
        # The endpoint was stopped before the tree was read.
        self.assertTrue(self.c.endpoints[0].stopped)

    def test_the_launch_s_routes_tunnel_the_wrapper_endpoint_s_own_port(self) -> None:
        """Issue #534 step 4: a producing launch's routes carry the wrapper tunnel at the port
        the endpoint serves (the leaf's `--mcp-config` URL names that port inside the
        namespace), with the family's constant endpoint set, and stop with the launch."""
        from tools import leaf_netns
        served: list = []
        real = leaf_netns.HostRoutes

        class Spy(real):  # type: ignore[misc,valid-type]
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                served.append(self)
        with mock.patch.object(leaf_netns, "HostRoutes", Spy):
            self.assertEqual(self.run_substep().status, "pass")
        (routes,) = served
        self.assertEqual(routes.wrapper_port, 4242)
        self.assertEqual(routes.endpoints,
                         frozenset(leaf_contract.WORKSHOP_CLI_ENDPOINTS["claude"]))
        self.assertFalse(os.path.exists(routes.socket_dir))
        launch = next(a for a in self.c.argvs if a[0] == "record-launch")
        self.assertEqual(launch[launch.index("--netns-socket-dir") + 1], routes.socket_dir)

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
        # The recorded launch is finalized, failed, rather than left dangling.
        finals = [c[1] for c in self.c.calls if c[0] == "finalize-child"]
        self.assertEqual(len(finals), 1)
        self.assertEqual(finals[0]["--agent-run-json"]["status"], "fail")


class RepairTests(_Base):
    def test_an_empty_tree_is_repaired_warm_over_the_same_lineage(self) -> None:
        self.c.leaf_actions = [_writes_nothing, _writes_bundle]
        outcome = self.run_substep()
        self.assertEqual(outcome.status, "pass", outcome)
        _first, second = self.requests()
        self.assertEqual(second["workshop_lineage_id"], "child-1")
        self.assertTrue(second["warm_resume"])
        self.assertEqual(second["repair_target_agent_run_id"], "child-1")
        self.assertIn(workshop_tree.BUNDLE_MANIFEST_NAME, second["repair_findings"])
        self.assertNotIn("pure_context", second)
        self.assertEqual(self.c.spawns[1]["resume_session_id"], "child-1")
        rows = self.bundle_meta()["per_attempt"]
        self.assertEqual(rows[0]["failure_category"], "workshop_tree_empty")
        self.assertEqual([r["lineage_id"] for r in rows], ["child-1", "child-1"])
        self.assertEqual([r["warm"] for r in rows], [False, True])
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
        _first, second = self.requests()
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
        # The abandoned lineage is recorded as such; the one that passed stays open.
        self.assertEqual(workshop_tree.read_lineage_record(self.paths("child-1"))["ended_by"],
                         "superseded")
        self.assertIsNone(workshop_tree.read_lineage_record(self.paths("child-2"))["ended_by"])

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
        self.assertEqual(meta["attempts"], 1 + leaf_contract.MAX_BUNDLE_REPAIR_TURNS)
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
        with mock.patch.object(self.c, "_leaf_transient_retry", return_value=True), \
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

        def short_lived(conductor, refs, entry, lineage_id, seeds, prior, spec):
            lineage = real_open(conductor, refs, entry, lineage_id, seeds, prior, spec)
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


class PrunedHomeTests(_Base):
    def test_a_warm_turn_whose_lineage_home_is_gone_falls_back_to_a_fresh_lineage(self) -> None:
        self.c.leaf_actions = [_writes_nothing, _writes_bundle]
        real_spawn = wc.Conductor._spawn_workshop_turn

        def prune_before_warm(conductor, refs, request, entry, lineage, **kw):
            if kw["warm"]:
                __import__("shutil").rmtree(lineage.paths.home)
            return real_spawn(conductor, refs, request, entry, lineage, **kw)

        with mock.patch.object(wc.Conductor, "_spawn_workshop_turn", prune_before_warm):
            outcome = self.run_substep()
        self.assertEqual(outcome.status, "pass", outcome)
        lineages = [r["workshop_lineage_id"] for r in self.requests()]
        # The warm turn records nothing; the next launch starts a fresh lineage, and the
        # pruned lineage's record says nothing will continue it.
        self.assertEqual(lineages, ["child-1", "child-3"])
        self.assertEqual(workshop_tree.read_lineage_record(self.paths("child-1"))["ended_by"],
                         "superseded")


class WarmPreparationRefusalTests(_Base):
    def test_a_warm_preparations_refusal_is_the_cold_refusals_category(self) -> None:
        """A credential file deleted mid-run: the warm preparation refuses, and the outcome is
        the cold route's, not a `conductor_error` out of `run_substep`. Nothing is launched."""
        self.c.leaf_actions = [_writes_nothing, _writes_bundle]
        real = _WorkshopFakeConductor._prepare_workshop_home

        def refuse_warm(conductor, entry, lineage_id, *, resume):
            if resume:
                raise ValueError("Codex auth.json not found for isolated home: /x/auth.json")
            return real(conductor, entry, lineage_id, resume=resume)

        with mock.patch.object(_WorkshopFakeConductor, "_prepare_workshop_home", refuse_warm):
            outcome = self.run_substep()
        self.assertEqual(outcome.status, "fail", outcome)
        self.assertEqual(outcome.infra_error[0], "workshop_lineage_setup_failed", outcome)
        self.assertIn("auth.json", outcome.infra_error[1])
        self.assertEqual(len(self.requests()), 1, "the warm turn recorded no launch")


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
        # A bound's reason stands over the supersession.
        self.assertEqual(workshop_tree.read_lineage_record(self.paths("child-1"))["ended_by"],
                         "leaf_timeout")

    def test_an_open_unresumable_lineage_is_superseded_on_reopen(self) -> None:
        self.assertEqual(self.run_substep().status, "pass")
        self.c.resumable = False
        self.c.leaf_actions = [_writes_bundle]
        self.assertEqual(self._reopen("child-1").status, "pass")
        self.assertEqual(self.requests()[-1]["workshop_lineage_id"], "child-2")
        self.assertEqual(workshop_tree.read_lineage_record(self.paths("child-1"))["ended_by"],
                         "superseded")

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


def _seeds(conductor, refs, context, request=None):
    """`_workshop_seeds` under the generate producer spec the loop seeds a lineage by."""
    spec = conductor._leaf_producer_spec("generate", conductor._bundle_shape(refs))
    return conductor._workshop_seeds(refs, context, request or {}, spec)


class SeedTests(_Base):
    def test_a_context_key_with_no_tree_name_is_refused(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            _seeds(self.c, self.refs, {"new_document": "x"})
        self.assertIn("new_document", str(ctx.exception))

    def test_every_pure_context_key_of_the_generate_producer_has_a_tree_name(self) -> None:
        for shape in ("", "harness"):
            key = ("generate", "generate", shape) if shape else ("generate", "generate")
            keys = (ort.PURE_CONTEXT_REQUIRED_KEYS_BY_SHAPE[key] if shape
                    else ort.PURE_CONTEXT_REQUIRED_KEYS[key])
            with self.subTest(shape=shape):
                self.assertEqual(
                    set(keys) - set(wc.Conductor._WORKSHOP_DOCUMENT_SEEDS_BY_PHASE["generate"]) - {"runner_document"},
                    set())

    def test_the_node_s_own_interface_header_is_a_seed(self) -> None:
        with mock.patch.object(wc.Conductor, "_interface_header_text",
                               return_value=("node_model.h", "// header\n")):
            seeds = _seeds(self.c, self.refs, self.c._build_leaf_context(self.refs))
        self.assertEqual(seeds["node_model.h"], "// header\n")

    def test_every_file_the_host_placed_in_src_is_a_seed(self) -> None:
        src = self.repo / self.refs.source_dir() / "src"
        (src / "dep_model.h").write_text("// dep\n", encoding="utf-8")
        (src / self.c._control_file_basename(self.refs)).write_text("x", encoding="utf-8")
        seeds = _seeds(self.c, self.refs, self.c._build_leaf_context(self.refs))
        self.assertEqual(seeds["dep_model.h"], "// dep\n")
        self.assertNotIn(self.c._control_file_basename(self.refs), seeds)


# --- the templates ---------------------------------------------------------------------

_TEMPLATES = ("workshop_generate_generate.txt", "workshop_generate_generate_harness.txt",
              "workshop_bundle_repair.txt", "workshop_compile_generate.txt",
              "workshop_ir_repair.txt",
              # Step 3: the reading kind's templates (the judging substeps).
              "workshop_compile_verify.txt", "workshop_generate_verify.txt",
              "workshop_generate_verify_harness.txt", "workshop_validate_judge.txt",
              "workshop_verdict_repair.txt",
              # PR-J4-a: the diagnostician's reading template (`workshop-9` onward).
              "workshop_escalate_diagnose.txt")
_DIR = Path(ort.__file__).resolve().parent / "prompt_templates"

#: HISTORY, frozen (issue #534 PR-J4-b): the retired `WORKSHOP_PROMPT_CONTRACT_VERSION` -> the
#: digest that shipped under it. Nothing recomputes these any more and nothing asserts over the
#: literal — the workshop leaf's contract is pinned by `test_leaf_prompt_contract_drift`, whose
#: tuple is the union of this pin's members and the pure pin's. Kept as the record of what
#: shipped. Each was a sha256 over the workshop templates,
#: `WORKSHOP_SYSTEM_PROMPT` and `WORKSHOP_TOOL_SURFACE_TEXT` (and, from `workshop-4`, the reading
#: kind's `WORKSHOP_READER_SYSTEM_PROMPT` and `WORKSHOP_READER_TOOL_SURFACE_TEXT`; from `workshop-6`,
#: `orchestration_runtime.WORKSHOP_CUT_READ_SCOPE_PARAGRAPH` and the verdict repair's
#: instruction-slot tables; from `workshop-7`, `leaf_contract.CODEX_READ_HELPER_TEXT`). A change to any is a change of the
#: workshop leaf's contract and takes a new version, keeping every shipped pin (the no-revert
#: check reads them). `workshop-1` (shipped in step 1's A/B, main f684c756) was taken over the
#: first three templates and the system prompt alone.
_WORKSHOP_CONTRACT_DIGESTS = {
    "workshop-1": "434bbd2a7d61d8ad6352b857a08398ed9ea0c33af5f7904823a841af6cc0d17e",
    "workshop-2": "79daa75c9cdc3233d8ea0697e63d0b4c2d1a1cf8d7ad60eaaed39066dae66c14",
    # Step 2 PR-2: the `codex_cli` row of `WORKSHOP_TOOL_SURFACE_TEXT` (`workshop-2` shipped
    # with step 2 PR-1, main 7cfbe1b5).
    "workshop-3": "1aee42ef7d0c2df40921cec4baecf3ab10af42c3386dfda58520b265b7b6ab4c",
    # Step 3 PR-J1: the reading kind — five templates, its system prompt and tool surface
    # (`workshop-3` shipped with step 2 PR-2, main ca41247f).
    "workshop-4": "69d6a21bea79dd5eeb5984836cb6409740bfc0d1148823ab68418e8e72ec1e58",
    # Step 3 PR-J2: the `codex_cli` reader row, and the paging rule moved from the reading
    # templates into the reader rows (`workshop-4` shipped with step 3 PR-J1/J1-b, main
    # 23a0e972).
    "workshop-5": "2b809056237a5565749a6357188c207bdba15c804d8ccf5b426e3039130500ec",
    # Issue #547: the `codex_cli` reader row names every output budget and the host's cut-read
    # refusal; the cut-read scope paragraph and the verdict repair's instruction slot (a schema
    # repair renders as before) join the digest (`workshop-5` shipped
    # with step 3 PR-J2, main bb119b5a).
    "workshop-6": "a0290f056603e92866cbc2ac40aef49ab82053ea1dabb66b12be824da2d8c822",
    # Issue #534 PR-J3-a: the `codex_cli` reader row leads with the host's reading helper, at
    # the lowered advice size, and the helper's text joins the digest (`workshop-6` shipped
    # with issue #547's PR #550, main d5d849be).
    "workshop-7": "7581618a41ef397919ad96295ba8ec5f7a71797bc5f293ccba310492e467c854",
    # Issue #534 PR-J3-b: the two HTTP providers' tool-surface rows and the five file tools'
    # declarations; the producer system prompt and producing templates name the host's
    # operations without the MCP transport (`workshop-7` shipped with PR-J3-a's PR #552, main
    # 3422004c).
    "workshop-8": "b4538ab4db91a37483f9d69e5ebe6e800c6c3ddd960e6d625d75caee75e531ca",
    # Issue #534 PR-J4-a: the escalate diagnostician's reading template joins the digest
    # (`workshop-8` shipped with PR-J3-b's PR #554, main 7a153ade).
    "workshop-9": "ba020ee490b16a045e6c93e5e9a5c5a8fb072a08f6feef7c9c526d5f02aac290",
}


class TemplateTests(unittest.TestCase):
    def test_each_opens_with_the_workshop_sentinel(self) -> None:
        for name in _TEMPLATES:
            with self.subTest(template=name):
                self.assertTrue((_DIR / name).read_text().startswith(
                    leaf_contract.WORKSHOP_PROMPT_SENTINEL))

    def test_no_document_is_inlined(self) -> None:
        for phase, names in (("generate", _TEMPLATES[:2]),
                             ("compile", ("workshop_compile_generate.txt",))):
            documents = set(wc.Conductor._WORKSHOP_DOCUMENT_SEEDS_BY_PHASE[phase]) | {
                "runner_document", "dependency_facts", "exemplar"}
            for name in names:
                with self.subTest(template=name):
                    slots = set(ort._LEAF_PLACEHOLDER_RE.findall((_DIR / name).read_text()))
                    self.assertEqual(slots & documents, set())
                    self.assertIn("tool_surface", slots)

    def test_each_seed_name_the_templates_cite_is_one_the_loop_places(self) -> None:
        text = (_DIR / "workshop_generate_generate.txt").read_text()
        for name in ("harness_capabilities.json", "target_profile.json", "spec.ir.yaml",
                     "tests.md", "DEPENDENCIES.md", "EXEMPLAR.md", "CONTRACT.md",
                     workshop_tree.BUNDLE_MANIFEST_NAME):
            self.assertIn(f"`{name}`", text)
        placed = set(wc.Conductor._WORKSHOP_DOCUMENT_SEEDS_BY_PHASE["generate"].values()) | {
            wc.Conductor._WORKSHOP_DEPENDENCIES_SEED, wc.Conductor._WORKSHOP_EXEMPLAR_SEED,
            wc.Conductor._WORKSHOP_CONTRACT_SEED}
        harness = (_DIR / "workshop_generate_generate_harness.txt").read_text()
        for name in placed:
            self.assertTrue(f"`{name}`" in text or f"`{name}`" in harness, name)
        for op in ("derive", "compile", "lint", "syntax_check", "run"):
            self.assertIn(f"`{op}`", text)
        compile_text = (_DIR / "workshop_compile_generate.txt").read_text()
        compile_placed = set(
            wc.Conductor._WORKSHOP_DOCUMENT_SEEDS_BY_PHASE["compile"].values()) | {
            wc.Conductor._WORKSHOP_DEPENDENCIES_SEED, wc.Conductor._WORKSHOP_CONTRACT_SEED,
            workshop_tree.IR_DOCUMENT_NAME, workshop_tree.COMPILE_FAIL_NAME, "check_ir"}
        for name in sorted(compile_placed):
            self.assertIn(f"`{name}`", compile_text, name)
        self.assertNotIn("`derive`", compile_text)
        self.assertNotIn("`bundle.yaml`", compile_text)

    def _render(self, **overrides) -> str:
        from tools.tests.test_workshop_launch import _workshop_request
        req = _workshop_request("ar_x", pure_language="fortran", pure_parallel="openmp",
                                **overrides)
        return ort.render_launch_prompt_text(ort.prepare_launch_request_payload(req))

    def test_a_workshop_request_renders_through_the_production_entry_point(self) -> None:
        prompt = self._render()
        self.assertTrue(prompt.startswith(leaf_contract.WORKSHOP_PROMPT_SENTINEL))
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
            self.assertTrue(prompt.startswith(leaf_contract.WORKSHOP_PROMPT_SENTINEL))
            self.assertIn("fix it", prompt)
        self.assertIn("Work tree contract", cold)
        self.assertNotIn("Work tree contract", warm)



class DriverPromptTests(unittest.TestCase):
    """What the prompts say since the host renders the driver (issue #589 PR-1b), read from each
    rendered launch on both language targets. A template's fragments are composed per target at
    render, so a read of the template files alone would not see a language's half."""

    TARGETS = ("fortran_cpu", "cpp_gpu")
    PAIRS = (("generate", "generate", ""), ("generate", "generate", "harness"),
             ("generate", "verify", ""), ("generate", "verify", "harness"),
             ("compile", "generate", ""), ("compile", "verify", ""), ("validate", "judge", ""))

    #: The words of the deleted checks module. `_checks` as a token suffix: `<spec_id>_checks`,
    #: `x_checks`, and so on, however the prefix is spelled.
    _STALE = re.compile(r"case_run|case_setup|state_bindings?|state_registration|checks[- ]module"
                        r"|[\w<>]*_checks\b", re.IGNORECASE)

    @classmethod
    def _stale_words(cls, text: str) -> list[str]:
        return sorted({m.group(0) for m in cls._STALE.finditer(text)})

    @staticmethod
    def _profile(target_id: str):
        from tools.target_profile import load_target_profile
        return load_target_profile(Path(wc.__file__).resolve().parents[1], target_id)

    def _render(self, step: str, substep: str, shape: str, target_id: str) -> str:
        """Through `build_launch_request` -> `prepare_launch_request_payload` ->
        `render_launch_prompt_text`, telling the language and the parallel backend only to the
        phase `Conductor._leaf_language` tells them to."""
        profile = self._profile(target_id)
        keys = (ort.PURE_CONTEXT_REQUIRED_KEYS_BY_SHAPE[(step, substep, shape)] if shape
                else ort.PURE_CONTEXT_REQUIRED_KEYS[(step, substep)])
        refs = wc.NodeRefs(target_id=target_id, node_key="component/x@0.1.0",
                           spec_path="spec/component/x", ir_id="ir_20260908_001",
                           pipeline_id="p_20260908_001", source_id="src_20260908_001",
                           binary_id="bin_20260908_001", run_id="run_20260908_001")
        told = step == "generate"
        req = wc.build_launch_request(
            refs, step=step, substep=substep, orchestration_id=_ORCH,
            orchestration_agent_run_id="orch", child_agent_run_id="ar_x",
            agent_model="opus", workflow_mode="dev", pure_leaf=True, pure_shape=shape,
            pure_language=str(profile.toolchain["language"]) if told else "",
            pure_parallel=str(profile.parallel_backend) if told else "",
            pure_context={k: f"DOCUMENT-{k}" for k in keys},
            workshop_lineage_id="ar_x", workshop_provider="claude_cli")
        return ort.render_launch_prompt_text(ort.prepare_launch_request_payload(req))

    def test_no_rendered_prompt_carries_a_word_of_the_checks_module(self) -> None:
        for target_id in self.TARGETS:
            for pair in self.PAIRS:
                with self.subTest(target=target_id, pair=pair):
                    text = self._render(*pair, target_id)
                    self.assertNotIn("{{", text)
                    self.assertEqual(self._stale_words(text), [])

    def test_no_composed_template_carries_one_either(self) -> None:
        """The repair and diagnose templates, which no row here renders from a request: every
        registered template, composed for each target."""
        from tools.tests.target_fixtures import composed_pure_template
        self.assertGreater(len(ort._PROMPT_TEMPLATE_FILES), len(self.PAIRS))
        for target_id in self.TARGETS:
            profile = self._profile(target_id)
            for key in sorted(ort._PROMPT_TEMPLATE_FILES):
                with self.subTest(target=target_id, template=key):
                    self.assertEqual(self._stale_words(composed_pure_template(key, profile)), [])

    def test_no_host_held_seed_document_carries_one(self) -> None:
        """A seed is read as the prompt is. The real documents the context builders hand over,
        through the language's own `prompt_fragments`, and the tool list the leaf is served. Not
        the spec documents: `tests.md` of 14 unconverted nodes still names the retired module until
        PR-3 converts them."""
        from tools.backends import registry as backend_registry
        from tools.workshop_mcp import TOOL_DEFINITIONS
        root = Path(wc.__file__).resolve().parents[1]
        phase_02 = (root / ort.WORKFLOW_PHASE_DOC_BY_STEP["generate"]).read_text(encoding="utf-8")
        shared = {
            "the runner-output contract": (root / ort.RUNNER_OUTPUT_CONTRACT_REF).read_text(
                encoding="utf-8"),
            "the severity rubric": wc._generate_verify_severity_rubric_section(phase_02),
            "the compile phase contract": (root / ort.WORKFLOW_PHASE_DOC_BY_STEP["compile"])
            .read_text(encoding="utf-8"),
            "the operations": json.dumps(TOOL_DEFINITIONS),
        }
        for name, text in shared.items():
            with self.subTest(document=name):
                self.assertGreater(len(text), 100)
                self.assertEqual(self._stale_words(text), [])
        for target_id in self.TARGETS:
            language = self._profile(target_id).toolchain["language"]
            module = backend_registry.capability_module("language", language, "prompt_fragments")
            rules = module.generate_rules_document()
            for name, text in (("generate rules", rules),
                               ("gate guards", wc._gate_guards_section(rules)),
                               ("runner-output binding", module.runner_output_document())):
                with self.subTest(language=language, document=name):
                    self.assertGreater(len(text), 100)
                    self.assertEqual(self._stale_words(text), [])

    def test_the_sweep_sees_each_word_planted_in_a_template_it_renders(self) -> None:
        """The witness of the two rows above, through the loader and the renderer: planting into
        the scanned string would witness the regex alone, and a render that dropped the template
        would pass both."""
        real = ort._load_launch_prompt_templates()
        words = ("case_run", "case_setup", "state_bindings", "state_registration@1",
                 "checks module", "the checks-module contract", "shallow_water2d_checks",
                 "`<spec_id>_checks`")
        for step, substep, shape in self.PAIRS:
            key = f"workshop {step}.{substep}" + (f".{shape}" if shape else "")
            for word in words:
                with self.subTest(template=key, word=word):
                    planted = {**real, key: real[key] + f"\nPlanted: {word}.\n"}
                    with mock.patch.object(ort, "_load_launch_prompt_templates",
                                           lambda p=planted: p):
                        found = self._stale_words(self._render(step, substep, shape,
                                                               self.TARGETS[0]))
                    self.assertTrue(found, f"{word!r} planted in {key} was not read")
                    self.assertTrue(all(f.lower() in word.lower() for f in found), found)

    def test_the_two_targets_render_their_own_language(self) -> None:
        """The sweep would be one prompt read twice if the target did not reach the composer."""
        fortran = self._render("generate", "generate", "", "fortran_cpu")
        cuda = self._render("generate", "generate", "", "cpp_gpu")
        self.assertIn("gfortran", fortran)
        self.assertNotIn("__global__", fortran)
        self.assertIn("__global__", cuda)
        self.assertNotIn("gfortran", cuda)

    # ---------------------------------------------------------------------- the driver's sentences

    @staticmethod
    def _stated_roles(text: str) -> set[str]:
        """The backticked names of the `role` clause of the output contract's `files` entry."""
        _, sep, rest = text.partition("`role` (")
        if not sep:
            raise AssertionError("the output contract has no `role` (...) clause")
        clause = rest.partition("), `language`")[0]
        return set(re.findall(r"`([a-z_]+)`", clause))

    def _assert_producer_contract(self, text: str) -> None:
        """Each statement is compared with the code that holds it, or the wording that decides
        its polarity, so a reversal fails here rather than reaching a leaf on a version bump."""
        from tools import codegen_bundle as cb
        from tools import workshop_wrapper as ww
        self.assertEqual(self._stated_roles(text), set(cb.FILE_ROLES))
        self.assertIn("a `runner` file is refused because the host renders the driver", text)
        self.assertIn("You author the model and nothing else", text)
        self.assertIn("`kind` (the literal `operation`: every entrypoint is one)", text)
        self.assertEqual(set(cb.ROLE_FOR_ENTRYPOINT_KIND), {"operation"})
        self.assertIn(f"`{ww.SEEDED_IR_NAME}` (the default case file of `run`)", text)
        self.assertIn("The driver alone reads the case file and writes every capture", text)
        self.assertIn("so what your operations leave in their arguments and their result logical "
                      "is the whole of the evidence", text)
        self.assertIn('Output contract (CodegenBundle, schema '
                      f'{cb.CODEGEN_BUNDLE_SCHEMA_VERSION})', text)

    def test_the_producer_is_told_what_the_host_holds_it_to_on_both_targets(self) -> None:
        for target_id in self.TARGETS:
            with self.subTest(target=target_id):
                self._assert_producer_contract(
                    self._render("generate", "generate", "", target_id))

    def test_the_producer_pin_fails_on_the_document_saying_the_opposite(self) -> None:
        text = self._render("generate", "generate", "", self.TARGETS[0])
        self._assert_producer_contract(text)
        for name, old, new in (
                ("a checks role", "`model`, or `helper`", "`model`, `checks`, or `helper`"),
                ("a role dropped", " / `internal_module` for a private file", " for a private file"),
                ("a runner authored", "a `runner` file is refused", "a `runner` file is required"),
                ("more than the model", "You author the model and nothing else",
                 "You author the model and the checks"),
                ("another kind", "(the literal `operation`:", "(the literal `checks_interface`:"),
                ("another case file", "`spec.ir.yaml` (the default case file",
                 "`cases.yaml` (the default case file"),
                ("the model captures", "The driver alone reads the case file",
                 "Your model reads the case file"),
                ("a kernel's own report", "is the whole of the evidence", "is part of the evidence"),
                ("schema 1", "Output contract (CodegenBundle, schema 2.0.0)",
                 "Output contract (CodegenBundle, schema 1.2.0)")):
            with self.subTest(name):
                self.assertIn(old, text, "the reversal must act on the rendered text")
                with self.assertRaises(AssertionError):
                    self._assert_producer_contract(text.replace(old, new))

    def _assert_reviewer_scope(self, text: str) -> None:
        self.assertIn("The host-rendered driver reads each case from a host-written case file, "
                      "holds the state, owns any time loop, writes every capture", text)
        self.assertIn("the model does none of that, and you do not fail the bundle for driver "
                      "code you cannot see", text)

    def test_the_reviewer_does_not_fail_a_bundle_for_the_driver_on_both_targets(self) -> None:
        for target_id in self.TARGETS:
            with self.subTest(target=target_id):
                text = self._render("generate", "verify", "", target_id)
                self._assert_reviewer_scope(text)
                for old, new in (("the model does none of that", "the model does all of that"),
                                 ("you do not fail the bundle for driver",
                                  "you fail the bundle for driver"),
                                 ("reads each case from a host-written case file",
                                  "reads each case from the checks module")):
                    self.assertIn(old, text)
                    with self.assertRaises(AssertionError):
                        self._assert_reviewer_scope(text.replace(old, new))

    def _assert_judge_capture(self, text: str) -> None:
        self.assertIn("A capture is what the host-rendered driver wrote from what the published "
                      "operations left in their arguments", text)
        self.assertIn("is read from the driver's own `series` fold, never from a kernel's report",
                      text)

    def test_the_judge_reads_a_capture_as_the_driver_s(self) -> None:
        text = self._render("validate", "judge", "", self.TARGETS[0])
        self._assert_judge_capture(text)
        for old, new in (("the host-rendered driver wrote", "the checks module wrote"),
                         ("never from a kernel's report", "or from a kernel's report")):
            self.assertIn(old, text)
            with self.assertRaises(AssertionError):
                self._assert_judge_capture(text.replace(old, new))

    def test_every_refusal_the_producer_is_quoted_is_the_one_the_host_gives(self) -> None:
        """The prompt quotes the refusal a leaf will read. Each is driven through the real
        acceptance layer with the defect the sentence names, and the quote must occur on both
        sides, so a reworded refusal or a sentence left behind is red and names which."""
        from tools.tests.test_leaf_producer import _conductor, _NODE

        def plant_two_models(d: dict) -> None:
            d["capability_requirements"] = ["sync_single_case@1", "batched_cases@1"]

        def plant_missing(d: dict) -> None:
            d["entrypoints"] = [dict(d["entrypoints"][0], symbol="extra_op")]

        def plant_extra(d: dict) -> None:
            d["entrypoints"].append(dict(d["entrypoints"][0], symbol="extra_op"))

        def plant_profile(d: dict) -> None:
            d["optimization_unit"]["members"] = ["profile/p@0.1.0"]
            d["files"][0]["member_node_key"] = "profile/p@0.1.0"
            d["entrypoints"][0]["node_key"] = "profile/p@0.1.0"

        rows = (
            ("capability_requirements must be a non-empty array",
             lambda d: d.update(capability_requirements=[])),
            ("must declare exactly one execution-model capability", plant_two_models),
            ("capability_requirements not satisfied by harness",
             lambda d: d.update(capability_requirements=["batched_cases@1"])),
            ("IR public_api operation(s) with no matching operation entrypoint", plant_missing),
            ("operation entrypoint symbol(s) not in the IR public_api", plant_extra),
            ("is a profile; a profile is a compile-time selection policy", plant_profile),
        )
        prompt = self._render("generate", "generate", "", self.TARGETS[0])
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        refs = _write_node(repo)
        conductor = _conductor(repo)
        self.assertEqual(refs.node_key, _NODE)
        for quote, plant in rows:
            with self.subTest(quote):
                self.assertIn(quote, prompt)
                bad = _valid_bundle()
                plant(bad)
                found = conductor._leaf_bundle_violations(refs, bad)
                self.assertIsNotNone(found)
                assert found is not None
                self.assertIn(quote, found[1])


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

    def test_a_codex_workshop_meta_reports_its_items_not_a_zero_turn_count(self) -> None:
        """A codex attempt records `model_turns: null` and `items_completed` (issue #534 step 2
        PR-2): the summary sums the items and leaves the turn count unrecorded, not 0."""
        from tools.audit_orchestration import _render_pure_leaf_row
        from tools.orchestration_diagnostics import _summarize_one_pure_meta
        meta = {"result": "pass", "attempts": 2, "leaf_mode": "workshop", "per_attempt": [
            {"lineage_id": "c1", "wrapper_calls": 2, "model_turns": None,
             "items_completed": 41},
            {"lineage_id": "c1", "wrapper_calls": 1, "model_turns": None,
             "items_completed": 12}]}
        row = _summarize_one_pure_meta(meta)
        self.assertEqual((row["model_turns"], row["items_completed"]), (None, 53))
        lines: list[str] = []
        _render_pure_leaf_row("compile", row, lines)
        text = "\n".join(lines)
        self.assertIn("wrapper calls 3, items completed 53", text)
        self.assertNotIn("model turns", text)


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
            self.assertTrue(prompt.startswith(leaf_contract.WORKSHOP_PROMPT_SENTINEL))


class GapTests(_Base):
    """Rows for mechanisms round 1's mutants found unobserved (issue #534 PR-3)."""

    def test_a_failed_attempt_s_bundle_meta_is_marked_workshop(self) -> None:
        self.c.leaf_actions = [_writes_nothing]
        self.run_substep()
        meta = self.bundle_meta()
        self.assertEqual((meta["result"], meta["leaf_mode"]), ("fail", "workshop"))

    def test_the_real_session_check_reads_the_lineage_home(self) -> None:
        paths = self.paths("lin")
        check = wc.Conductor._workshop_session_resumable
        self.assertFalse(check(self.c, paths, "sid"))
        (paths.config / "projects" / "slug").mkdir(parents=True)
        (paths.config / "projects" / "slug" / "sid.jsonl").write_text("{}\n")
        self.assertTrue(check(self.c, paths, "sid"))
        for other in ("other", "", None):
            self.assertFalse(check(self.c, paths, other), other)
        # The operator's own home is not where a workshop session lives.
        self.assertFalse(check(self.c, self.paths("lin2"), "sid"))

    def test_a_warm_launch_names_its_lineage_to_record_launch_and_a_cold_one_does_not(
            self) -> None:
        self.c.leaf_actions = [_writes_nothing, _writes_bundle]
        self.run_substep()
        argvs = [a for a in self.c.argvs if a[0] == "record-launch"]
        self.assertNotIn("--workshop-lineage-id", argvs[0])
        self.assertEqual(argvs[1][argvs[1].index("--workshop-lineage-id") + 1], "child-1")

    def test_the_contract_copy_is_not_overwritten_by_a_warm_turn(self) -> None:
        self.c.numbered_prompts = True
        self.c.leaf_actions = [_writes_nothing, _writes_bundle]
        self.run_substep()
        self.assertEqual((self.paths("child-1").tree / "CONTRACT.md").read_text(), "PROMPT-1")

    def test_a_transport_death_does_not_replace_the_carried_findings(self) -> None:
        self.c.leaf_actions = [_writes_nothing,
                               lambda c, t, kw: wc.ProcResult(1, "", "API Error: 529 overloaded"),
                               _writes_bundle]
        with mock.patch.object(self.c, "_leaf_transient_retry", return_value=True):
            self.assertEqual(self.run_substep().status, "pass")
        third = self.requests()[2]
        self.assertIn(workshop_tree.BUNDLE_MANIFEST_NAME, third["repair_findings"])
        self.assertNotIn("529", third["repair_findings"])

    def test_a_usage_wait_retries_from_the_snapshot(self) -> None:
        def dies_after_a_draft(c, tree, kwargs):
            (tree / "draft.txt").write_text("kept\n", encoding="utf-8")
            return wc.ProcResult(1, "", "x")

        seen: list[str] = []

        def finishes(c, tree, kwargs):
            seen.append((tree / "draft.txt").read_text())
            _write_bundle(tree)

        self.c.leaf_actions = [dies_after_a_draft, finishes]
        with mock.patch.object(wc, "_leaf_infra_error",
                               return_value=("llm_usage_limit", "limit")), \
                mock.patch.object(self.c, "_usage_limit_wait", return_value=True):
            self.assertEqual(self.run_substep().status, "pass")
        self.assertEqual(seen, ["kept\n"])

    def test_a_dead_warm_repair_is_retried_in_a_new_lineage_never_resumed(self) -> None:
        """A warm repair turn that dies — waited out as a usage limit, or retried as a
        transient — ended its lineage `leaf_exit`; the re-launch is a cold reuse of that
        lineage's snapshot in a NEW lineage, never a resume of the dead session
        (`_WorkshopLineageMachine.after_transport`)."""
        def dies(c, tree, kwargs):
            return wc.ProcResult(1, "", "x")

        for label, extra in (
                ("usage wait", mock.patch.object(wc, "_leaf_infra_error",
                                                 return_value=("llm_usage_limit", "limit"))),
                ("transient retry", mock.patch.object(wc.Conductor, "_leaf_transient_retry",
                                                      return_value=True))):
            with self.subTest(retry=label):
                self.setUp()
                self.c.leaf_actions = [_writes_nothing, dies, _writes_bundle]
                with extra, mock.patch.object(self.c, "_usage_limit_wait", return_value=True):
                    self.assertEqual(self.run_substep().status, "pass")
                self.assertEqual([s["resume_session_id"] for s in self.c.spawns],
                                 [None, "child-1", None])
                self.assertEqual([r["workshop_lineage_id"] for r in self.requests()],
                                 ["child-1", "child-1", "child-3"])

    def test_the_wrapper_is_given_the_lineage_deadline(self) -> None:
        import time
        before = time.time()
        self.run_substep()
        deadline = self.c.frozen[0]["lineage_deadline"]
        self.assertTrue(before + 2700 - 5 <= deadline <= time.time() + 2700)

    def test_an_outer_reopen_keeps_the_lineage_s_clock(self) -> None:
        self.assertEqual(self.run_substep().status, "pass")
        paths = self.paths("child-1")
        record = workshop_tree.read_lineage_record(paths)
        record["started_at_utc"] = "2000-01-01T00:00:00Z"
        paths.record_json.write_text(json.dumps(record), encoding="utf-8")
        reopen = OuterReopenTests._reopen
        self.c.leaf_actions = [_writes_bundle]
        self.assertEqual(reopen(self, "child-1").status, "pass")
        # The year-2000 clock has run out: the reopened lineage is ended and a fresh one runs.
        self.assertEqual(self.requests()[-1]["workshop_lineage_id"], "child-2")
        self.assertEqual(workshop_tree.read_lineage_record(paths)["ended_by"], "lineage_timeout")

    def test_a_warm_turn_is_accounted_against_the_resumed_envelope(self) -> None:
        self.c.leaf_actions = [_writes_nothing, _writes_bundle]
        seen: list = []
        real = wc._leaf_usage_row

        def spy(proc, entry, **kw):
            seen.append(kw.get("resumed"))
            return real(proc, entry, **kw)

        with mock.patch.object(wc, "_leaf_usage_row", spy):
            self.run_substep()
        self.assertIsNone(seen[0])
        self.assertEqual(seen[1][0], "child-1")


class SeedRefusalTests(_Base):
    def test_two_seeds_of_one_name_are_refused(self) -> None:
        src = self.repo / self.refs.source_dir() / "src"
        (src / "tests.md").write_text("x", encoding="utf-8")
        with self.assertRaises(RuntimeError) as ctx:
            _seeds(self.c, self.refs, self.c._build_leaf_context(self.refs))
        self.assertIn("tests.md", str(ctx.exception))

    def test_a_context_carrying_the_runner_needs_it_in_src(self) -> None:
        context = self.c._build_leaf_context(self.refs)
        (self.repo / self.refs.source_dir() / "src"
         / self.c._runner_basename(self.refs)).unlink()
        with self.assertRaises(RuntimeError) as ctx:
            _seeds(self.c, self.refs, context)
        self.assertIn("runner", str(ctx.exception))

    def test_a_host_file_named_like_the_contract_is_refused(self) -> None:
        src = self.repo / self.refs.source_dir() / "src"
        (src / "CONTRACT.md").write_text("x", encoding="utf-8")
        with self.assertRaises(RuntimeError) as ctx:
            _seeds(self.c, self.refs, self.c._build_leaf_context(self.refs))
        self.assertIn("CONTRACT.md", str(ctx.exception))


class Round2Tests(_Base):
    """Rows for round 2's findings (issue #534 PR-3)."""

    def test_an_exemplar_the_host_no_longer_resolves_leaves_the_reopened_tree(self) -> None:
        with mock.patch.object(_WorkshopFakeConductor, "_resolve_exemplar",
                               return_value=_EXEMPLAR):
            self.assertEqual(self.run_substep().status, "pass")
        tree = self.paths("child-1").tree
        self.assertTrue((tree / "EXEMPLAR.md").is_file())
        seen: list[bool] = []

        def checks(c, t, kw):
            seen.append((t / "EXEMPLAR.md").exists())
            _write_bundle(t)

        self.c.leaf_actions = [checks]
        with mock.patch.object(_WorkshopFakeConductor, "_resolve_exemplar", return_value=None):
            self.assertEqual(OuterReopenTests._reopen(self, "child-1").status, "pass")
        self.assertTrue(self.requests()[-1]["warm_resume"])
        self.assertEqual(seen, [False])
        self.assertIsNone(self.bundle_meta()["per_attempt"][-1]["exemplar_ref"])
        self.assertNotIn("EXEMPLAR.md",
                         [p.name for p in self.paths("child-1").snapshot.iterdir()])

    def test_a_reopen_keeping_its_exemplar_records_it(self) -> None:
        with mock.patch.object(_WorkshopFakeConductor, "_resolve_exemplar",
                               return_value=_EXEMPLAR):
            self.assertEqual(self.run_substep().status, "pass")
            self.c.leaf_actions = [_writes_bundle]
            self.assertEqual(OuterReopenTests._reopen(self, "child-1").status, "pass")
        self.assertEqual(self.bundle_meta()["per_attempt"][-1]["exemplar_ref"],
                         _EXEMPLAR["source_ref"])

    def test_the_host_judges_the_snapshot_not_the_live_tree(self) -> None:
        real = workshop_tree.snapshot_tree

        def drops_the_model(paths, host_placed):
            out = real(paths, host_placed)
            (paths.snapshot / f"{_SPEC_ID}_model.f90").unlink()
            return out

        with mock.patch.object(workshop_tree, "snapshot_tree", drops_the_model):
            self.c.leaf_actions = [_writes_bundle]
            outcome = self.run_substep()
        self.assertEqual(outcome.status, "fail")
        self.assertIn(f"{_SPEC_ID}_model.f90", self.bundle_meta()["failure_excerpt"])

    def test_the_retry_and_wait_budgets_are_counted_per_substep_run(self) -> None:
        retries: list[int] = []
        waits: list[int] = []

        def retry(**kw):
            retries.append(kw["retries_done"])
            return kw["retries_done"] < 2

        def wait(**kw):
            waits.append(kw["waits_done"])
            return kw["waits_done"] < 3

        overloaded = lambda c, t, kw: wc.ProcResult(1, "", "API Error: 529 overloaded")
        self.c.leaf_actions = [overloaded] * 3
        with mock.patch.object(self.c, "_leaf_transient_retry", side_effect=retry):
            self.assertEqual(self.run_substep().status, "fail")
        self.assertEqual(retries, [0, 1, 2])
        self.assertEqual(len({r["workshop_lineage_id"] for r in self.requests()}), 3)
        self.c.leaf_actions = [lambda c, t, kw: wc.ProcResult(1, "", "usage")] * 4
        self.fresh_source()
        with mock.patch.object(wc, "_leaf_infra_error",
                               return_value=("llm_usage_limit", "limit")), \
                mock.patch.object(self.c, "_usage_limit_wait", side_effect=wait):
            self.assertEqual(self.run_substep().status, "fail")
        self.assertEqual(waits, [0, 1, 2, 3])

    def test_the_real_wrapper_context_carries_the_seed_texts(self) -> None:
        """The conductor half of round 1's F1: the REAL `_freeze_wrapper_context` hands the
        seed texts to the wrapper (the fake overrides it everywhere else)."""
        paths = self.paths("frozen")
        ctx = wc.Conductor._freeze_wrapper_context(
            self.c, self.refs, paths, agent_run_id="a1", seed_names=("tests.md",),
            reserved_names=frozenset({"tests.md"}), closure_file_names=(),
            max_wrapper_calls=3, lineage_deadline=1.0, seed_texts={"tests.md": "T"})
        self.assertEqual(ctx.seed_texts, (("tests.md", "T"),))



class Round3Tests(_Base):
    def test_record_launch_answering_home_missing_falls_back_without_a_launch(self) -> None:
        self.c.home_missing_on_warm = True
        self.c.leaf_actions = [_writes_nothing, _writes_bundle]
        self.assertEqual(self.run_substep().status, "pass")
        # Two launches spawned (cold, then a fresh cold lineage); the warm attempt spawned none.
        self.assertEqual(len(self.c.spawns), 2)
        self.assertEqual(self.requests()[-1]["workshop_lineage_id"], "child-3")
        self.assertEqual(workshop_tree.read_lineage_record(self.paths("child-1"))["ended_by"],
                         "superseded")

    def test_a_reopen_carrying_no_findings_is_a_cold_reuse(self) -> None:
        self.assertEqual(self.run_substep().status, "pass")
        self.fresh_source()
        evidence = self.c.__dict__["evidence"]
        self.c.leaf_actions = [_writes_bundle]
        with mock.patch.object(ort, "_read_launch_request_payload",
                               lambda r, o, *, agent_run_id:
                               evidence.get(f"{agent_run_id}.request.input.json")):
            self.assertEqual(self.run_substep(repair={
                "repair_strategy": "reuse", "repair_target_agent_run_id": "child-1",
                "issue_severity": "major", "repair_reason": "r"}).status, "pass")
        request = self.requests()[-1]
        self.assertEqual(request["workshop_lineage_id"], "child-2")
        self.assertFalse(request.get("warm_resume"))
        self.assertEqual(workshop_tree.read_lineage_record(self.paths("child-1"))["ended_by"],
                         "superseded")



def _codex_config(repo: Path) -> lc.LlmConfig:
    path = repo / "llm_codex_workshop.yaml"
    path.write_text(
        llm_samples.workshop_yaml(
            "defaults: {lineage_timeout_s: 2700, provider: codex_cli, model: gpt-x}\n"
            "phases: {generate: {substeps: {generate: {provider: codex_cli, model: gpt-x, "
            "max_wrapper_calls: 30, lineage_timeout_s: 2700}}}}\n"),
        encoding="utf-8")
    return lc.load_llm_config(path)


def _codex_proc(text: str = "done", items: int = 3) -> wc.ProcResult:
    lines = [json.dumps({"type": "thread.started", "thread_id": "t"})]
    lines += [json.dumps({"type": "item.completed", "item": {"type": "command_execution"}})
              for _ in range(items)]
    lines.append(json.dumps({"type": "turn.completed", "usage": {"output_tokens": 7}}))
    return wc.ProcResult(0, text, "", usage={"output_tokens": 7, "usage_source": "codex"},
                         raw_stdout="\n".join(lines) + "\n", items_completed=items)


class CodexWorkshopLoopTests(OuterReopenTests):
    """The loop on a `codex_cli` workshop entry (issue #534 step 2 PR-2): the lineage lives
    under the codex container, the attempt record is the synthetic envelope's (the host-pinned
    model, codex's usage, no turn count, the completed-item count), and a warm turn resumes the
    THREAD the launch emitted, not the agent_run_id."""

    def setUp(self) -> None:
        super().setUp()
        self.c.llm_config = _codex_config(self.repo)
        self.c._session_id_for_child = (  # type: ignore[assignment]
            lambda arid, entry=None: f"thread-{arid}")

    def paths(self, lineage: str) -> workshop_tree.LineagePaths:
        return workshop_tree.LineagePaths(
            homes_root=self.c.homes, orchestration_id=_ORCH, lineage_id=lineage,
            orchestration_dir=self.repo / "workspace" / "orchestrations" / _ORCH,
            backend="codex")

    def test_a_codex_attempt_records_the_synthetic_envelope(self) -> None:
        def writes(c, tree, kwargs):
            _write_bundle(tree)
            proc = _codex_proc(items=4)
            # The read loop's count, not a re-count of the capped raw capture (4 here).
            proc.items_completed = 9
            # A producing lineage is not held to the cut-read rule (issue #547), whatever its
            # commands printed: its tree is judged by the host's gates.
            proc.command_outputs = [("cat big", 600_000)]
            return proc

        self.c.leaf_actions = [writes]
        self.assertEqual(self.run_substep().status, "pass")
        self.assertTrue(self.paths("child-1").tree.is_dir())
        self.assertFalse((self.c.homes / _ORCH / "claude").exists())
        row = self.bundle_meta()["per_attempt"][0]
        self.assertEqual((row["model"], row["model_turns"], row["items_completed"]),
                         ("gpt-x", None, 9))
        self.assertEqual(row["usage"]["output_tokens"], 7)
        self.assertNotIn("cut_outputs", row)
        self.assertEqual(self.requests()[0]["workshop_provider"], "codex_cli")

    def test_a_warm_repair_resumes_the_emitted_thread(self) -> None:
        self.c.leaf_actions = [lambda c, tree, kw: _codex_proc(),
                               lambda c, tree, kw: (_write_bundle(tree), _codex_proc())[1]]
        self.assertEqual(self.run_substep().status, "pass")
        self.assertEqual(self.c.spawns[1]["resume_session_id"], "thread-child-1")
        self.assertEqual(self.requests()[1]["workshop_lineage_id"], "child-1")

    def test_a_warm_attempt_records_its_turn_not_the_thread_total(self) -> None:
        """The warm attempt's row is decumulated against the resumed launch of its thread
        (`_codex_thread_resumed_usage`, asked for the thread the launch resumed)."""
        asked: list[tuple] = []

        def resumed(tid, arid):
            asked.append((tid, arid))
            return "child-1", {"output_tokens": 7}

        self.c._codex_thread_resumed_usage = resumed  # type: ignore[assignment]

        def warm(c, tree, kw):
            _write_bundle(tree)
            proc = _codex_proc()
            proc.usage = {"output_tokens": 19}
            return proc

        self.c.leaf_actions = [lambda c, tree, kw: _codex_proc(), warm]
        self.assertEqual(self.run_substep().status, "pass")
        rows = self.bundle_meta()["per_attempt"]
        self.assertEqual([r["usage"]["output_tokens"] for r in rows], [7, 12])
        self.assertEqual(rows[1]["usage"]["provider_details"]["thread_running_total"],
                         {"output_tokens": 19})
        self.assertEqual(asked, [("thread-child-1", "child-2")])

    def test_an_open_lineage_is_reopened_warm(self) -> None:
        self.c.leaf_actions = [lambda c, tree, kw: (_write_bundle(tree), _codex_proc())[1]]
        self.assertEqual(self.run_substep().status, "pass")
        self.assertEqual(self._reopen("child-1").status, "pass")
        self.assertTrue(self.requests()[-1]["warm_resume"])
        self.assertEqual(self.c.spawns[-1]["resume_session_id"], "thread-child-1")

    def test_the_real_session_check_reads_the_rollout_under_codex_home(self) -> None:
        paths = self.paths("lin")
        check = wc.Conductor._workshop_session_resumable
        self.assertFalse(check(self.c, paths, "01a1-thread"))
        day = paths.config / "sessions" / "2026" / "10" / "24"
        day.mkdir(parents=True)
        (day / "rollout-2026-10-24T10-47-36-01a1-thread.jsonl").write_text("{}\n")
        self.assertTrue(check(self.c, paths, "01a1-thread"))
        for other in ("01a1", "thread", "", None, "../x", "*"):
            self.assertFalse(check(self.c, paths, other), other)
        # A claude transcript of the same id is not a codex session.
        claude = workshop_tree.LineagePaths(
            homes_root=self.c.homes, orchestration_id=_ORCH, lineage_id="lin",
            orchestration_dir=paths.orchestration_dir)
        self.assertFalse(check(self.c, claude, "01a1-thread"))


# --- `compile.generate` (issue #534 step 2) -----------------------------------------------

def _compile_config(repo: Path) -> lc.LlmConfig:
    path = repo / "llm_workshop_compile.yaml"
    path.write_text(
        llm_samples.workshop_yaml(
            "defaults: {lineage_timeout_s: 2700, provider: claude_cli, model: opus}\n"
            "phases: {compile: {substeps: {generate: {provider: claude_cli, model: opus, "
            "max_wrapper_calls: 30, lineage_timeout_s: 2700}}}}\n"),
        encoding="utf-8")
    return lc.load_llm_config(path)


def _writes_ir(ir: dict | None = None):
    from tools.tests.test_leaf_compile import _assembled_ir

    def action(c, tree, kwargs):
        (tree / workshop_tree.IR_DOCUMENT_NAME).write_text(
            yaml.safe_dump(ir if ir is not None else _assembled_ir(), sort_keys=False),
            encoding="utf-8")
    return action


def _writes_declaration(c, tree, kwargs):
    (tree / workshop_tree.COMPILE_FAIL_NAME).write_text(
        json.dumps({"input": "tests", "where": "§1", "what": "declares no test"}),
        encoding="utf-8")


#: A declaration whose reason is longer than the 300 characters a summary keeps.
_LONG_WHAT = "the section declares no test id the host can read; " * 8


def _writes_long_declaration(c, tree, kwargs):
    (tree / workshop_tree.COMPILE_FAIL_NAME).write_text(
        json.dumps({"input": "tests", "where": "§1", "what": _LONG_WHAT}), encoding="utf-8")


_SURFACE = ({"node_key": "component/dep@0.1.0",
             "published_operations": ["dep__apply"], "source": "ir_public_api",
             "public_api": {}},)


class WorkshopCompileProducerTests(unittest.TestCase):
    """The loop on a `compile.generate` workshop entry: the leaf authors the WHOLE IR in its
    tree (`spec.ir.yaml`, or `compile_fail.json` instead), and the host takes it through the
    IR document checks, writers, routing and declared-fail exit. The wrapper's
    context is the REAL `_freeze_ir_check_context` (no subprocess), the endpoint the fake."""

    def setUp(self) -> None:
        from tools.tests.test_leaf_compile import _write_compile_node
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        (self.repo / "workspace" / "orchestrations" / _ORCH).mkdir(parents=True)
        self.refs = _write_compile_node(self.repo)
        self.c = _WorkshopFakeConductor(
            repo_root=self.repo, orchestration_id=_ORCH, orchestration_agent_run_id="orch",
            llm_config=_compile_config(self.repo), env={})
        self.c.homes = self.repo.parent / (self.repo.name + "_homes")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.c.homes, ignore_errors=True))
        self.c.leaf_actions = [_writes_ir()]

    def run_substep(self, repair=None) -> wc.SubstepOutcome:
        return self.c.run_substep(self.refs, "compile", "generate", repair=repair,
                                  dependency_surface=_SURFACE)

    def requests(self) -> list[dict]:
        return [c[1]["--request-json"] for c in getattr(self.c, "calls", [])
                if c[0] == "record-launch"]

    def paths(self, lineage: str) -> workshop_tree.LineagePaths:
        return workshop_tree.LineagePaths(
            homes_root=self.c.homes, orchestration_id=_ORCH, lineage_id=lineage,
            orchestration_dir=self.repo / "workspace" / "orchestrations" / _ORCH)

    def meta(self) -> dict:
        return json.loads((self.repo / self.refs.ir_ref
                           / "compile_generate_meta.json").read_text())

    def test_a_tree_holding_the_whole_ir_passes_and_is_written(
            self) -> None:
        from tools.tests.test_leaf_compile import _assembled_ir
        outcome = self.run_substep()
        self.assertEqual(outcome.status, "pass", outcome)
        ir_dir = self.repo / self.refs.ir_ref
        self.assertEqual((ir_dir / "spec.ir.yaml").read_text(),
                         wc.Conductor._leaf_ir_yaml(_assembled_ir()))
        self.assertEqual(json.loads((ir_dir / "ir_meta.json").read_text())
                         ["verification_status"], "pending")
        meta = self.meta()
        self.assertEqual((meta["result"], meta["leaf_mode"], meta["prompt_contract_version"]),
                         ("pass", "workshop", leaf_contract.LEAF_PROMPT_CONTRACT_VERSION))
        # One version since issue #534 PR-J4-b: the retired workshop counter is not written.
        self.assertNotIn("workshop_prompt_contract_version", meta)
        self.assertNotIn("parts", meta)
        row = meta["per_attempt"][0]
        self.assertEqual((row["lineage_id"], row["warm"], row["ended_by"]),
                         ("child-1", False, None))
        self.assertIsInstance(row["leaf_wall_s"], float)
        # The part split did not run: one launch, one lineage.
        self.assertEqual(len(self.requests()), 1)
        self.assertEqual(json.loads(
            self.paths("child-1").record_json.read_text())["last_launch"]["rc"], 0)

    def test_the_request_is_a_whole_ir_workshop_request(self) -> None:
        self.run_substep()
        (request,) = self.requests()
        self.assertEqual((request["leaf_mode"], request["workshop_provider"],
                          request["workshop_lineage_id"]), ("workshop", "claude_cli", "child-1"))
        self.assertNotIn("pure_shape", request)
        self.assertNotIn("pure_part", request)
        self.assertEqual(request["dependency_surface"], list(_SURFACE))
        payload = ort.prepare_launch_request_payload(dict(request))
        ort._validate_launch_request_payload(payload)
        prompt = ort.render_launch_prompt_text(payload)
        ort._validate_launch_prompt_text(payload, prompt)
        self.assertTrue(prompt.startswith(leaf_contract.WORKSHOP_PROMPT_SENTINEL))
        self.assertIn("`compile.generate` producer", prompt.splitlines()[0])
        self.assertIn(leaf_contract.WORKSHOP_TOOL_SURFACE_TEXT["claude_cli"], prompt)
        self.assertIn("`check_ir`", prompt)

    def test_the_tree_holds_the_compile_documents_and_the_published_catalog(self) -> None:
        self.run_substep()
        tree = self.paths("child-1").tree
        names = {p.name for p in tree.iterdir()}
        expected = set(wc.Conductor._WORKSHOP_DOCUMENT_SEEDS_BY_PHASE["compile"].values())
        self.assertTrue(expected <= names, expected - names)
        self.assertEqual((tree / "tests.md").read_text(),
                         (self.repo / self.refs.spec_path / "tests.md").read_text())
        self.assertIn("dep__apply", (tree / "DEPENDENCIES.md").read_text())
        # Nothing of Generate's: no src/ file, no interface header, no exemplar.
        self.assertFalse(names & {"EXEMPLAR.md", "harness_capabilities.json",
                                  "target_profile.json"})
        self.assertEqual(names - expected - {"DEPENDENCIES.md", "CONTRACT.md",
                                             workshop_tree.IR_DOCUMENT_NAME}, set())

    def test_no_generate_source_is_seeded_even_where_one_would_render(self) -> None:
        """Round 1: the fixture's language renders no interface header and its node has no
        `src/` at compile time, so "nothing of Generate's" was observed only by absence. Here a
        header WOULD render; the compile lineage still does not seed it."""
        with mock.patch.object(wc.Conductor, "_interface_header_text",
                               return_value=("node_model.h", "// header\n")):
            self.assertEqual(self.run_substep().status, "pass")
        self.assertFalse((self.paths("child-1").tree / "node_model.h").exists())

    def test_every_pure_context_key_of_the_compile_producer_has_a_tree_name(self) -> None:
        self.assertEqual(set(ort.PURE_CONTEXT_REQUIRED_KEYS[("compile", "generate")]),
                         set(wc.Conductor._WORKSHOP_DOCUMENT_SEEDS_BY_PHASE["compile"]))

    def test_the_wrapper_context_serves_check_ir_over_the_host_s_sidecars(self) -> None:
        frozen: list = []
        real = wc.Conductor._freeze_ir_check_context

        def spy(conductor, refs, paths, **kw):
            ctx = real(conductor, refs, paths, **kw)
            frozen.append(ctx)
            return ctx

        with mock.patch.object(wc.Conductor, "_freeze_ir_check_context", spy):
            self.run_substep()
        (ctx,) = frozen
        self.assertEqual(ctx.ops, ("check_ir",))
        self.assertEqual({n for n, _ in ctx.sidecar_texts},
                         {"dependency_graph.json", "dependency_surface.json"})
        self.assertEqual(json.loads(ctx.ir_meta_text)["verification_status"], "pending")
        self.assertIn(workshop_tree.IR_DOCUMENT_NAME, ctx.reserved_names)
        self.assertIn(workshop_tree.COMPILE_FAIL_NAME, ctx.reserved_names)

    def test_a_declaration_is_the_third_exit_and_ends_the_lineage(self) -> None:
        self.c.leaf_actions = [_writes_declaration]
        outcome = self.run_substep()
        self.assertEqual((outcome.status, outcome.infra_error), ("fail", None))
        ir_dir = self.repo / self.refs.ir_ref
        self.assertFalse((ir_dir / "spec.ir.yaml").exists())
        ir_meta = json.loads((ir_dir / "ir_meta.json").read_text())
        self.assertEqual((ir_meta["verification_status"], ir_meta["last_fail_reason"]),
                         ("fail", "tests §1: declares no test"))
        meta = self.meta()
        self.assertEqual((meta["failure_category"], meta["leaf_mode"]),
                         (wc.COMPILE_DECLARED_FAIL, "workshop"))
        self.assertEqual(meta["per_attempt"][0]["ended_by"], "declared_fail")
        self.assertEqual(workshop_tree.read_lineage_record(self.paths("child-1"))["ended_by"],
                         "declared_fail")
        self.assertEqual(len(self.requests()), 1, "a declaration was repaired")
        finals = [c[1]["--agent-run-json"] for c in self.c.calls if c[0] == "finalize-child"]
        self.assertEqual(finals[-1]["result_summary"],
                         f"workshop_compile_fail: {wc.COMPILE_DECLARED_FAIL}: "
                         "tests §1: declares no test")
        self.assertNotIn(wc.COMPILE_DECLARED_FAIL, wc.COMPILE_DOCUMENT_FAILURE_ROUTING)

    def test_the_summary_keeps_300_characters_of_a_long_declaration(self) -> None:
        self.c.leaf_actions = [_writes_long_declaration]
        self.run_substep()
        reason = f"tests §1: {_LONG_WHAT.strip()}"
        self.assertGreater(len(reason), 300)
        finals = [c[1]["--agent-run-json"] for c in self.c.calls if c[0] == "finalize-child"]
        self.assertEqual(finals[-1]["result_summary"],
                         f"workshop_compile_fail: {wc.COMPILE_DECLARED_FAIL}: {reason[:300]}")

    def test_a_declaration_beside_an_ir_is_repaired_warm_with_the_ir_repair_turn(self) -> None:
        def both(c, tree, kwargs):
            _writes_ir()(c, tree, kwargs)
            _writes_declaration(c, tree, kwargs)

        def fixed(c, tree, kwargs):
            (tree / workshop_tree.COMPILE_FAIL_NAME).unlink()

        self.c.leaf_actions = [both, fixed]
        outcome = self.run_substep()
        self.assertEqual(outcome.status, "pass", outcome)
        _first, second = self.requests()
        self.assertTrue(second["warm_resume"])
        self.assertEqual(second["workshop_lineage_id"], "child-1")
        self.assertIn("two different answers", second["repair_findings"])
        prompt = ort.render_launch_prompt_text(ort.prepare_launch_request_payload(dict(second)))
        self.assertIn("The IR the host read from your work tree", prompt)
        self.assertEqual(self.meta()["per_attempt"][0]["failure_category"],
                         wc.COMPILE_IR_DOCUMENT_VIOLATION)

    def test_an_empty_tree_is_workshop_tree_empty_routed_to_a_compile_reuse(self) -> None:
        self.c.leaf_actions = [_writes_nothing]
        outcome = self.run_substep()
        self.assertEqual(outcome.status, "fail")
        meta = self.meta()
        self.assertEqual(meta["failure_category"], workshop_tree.WORKSHOP_TREE_EMPTY)
        self.assertEqual(wc.COMPILE_DOCUMENT_FAILURE_ROUTING[meta["failure_category"]],
                         ("compile", "reuse"))
        self.assertEqual(len(meta["per_attempt"]), 1 + leaf_contract.MAX_BUNDLE_REPAIR_TURNS)

    def test_a_non_mapping_ir_is_a_finding_not_a_raise(self) -> None:
        def scalar(c, tree, kwargs):
            (tree / workshop_tree.IR_DOCUMENT_NAME).write_text("just a string\n")

        self.c.leaf_actions = [scalar, _writes_ir()]
        self.assertEqual(self.run_substep().status, "pass")
        self.assertEqual(self.meta()["per_attempt"][0]["failure_category"],
                         wc.COMPILE_IR_DOCUMENT_VIOLATION)

    def test_the_document_contract_judges_the_tree_s_ir(self) -> None:
        from tools.tests.test_leaf_compile import _assembled_ir
        wrong = _assembled_ir()
        wrong["meta"]["spec_kind"] = "problem"
        self.c.leaf_actions = [_writes_ir(wrong), _writes_ir()]
        self.assertEqual(self.run_substep().status, "pass")
        self.assertIn("ir.meta.spec_kind",
                      self.meta()["per_attempt"][0]["failure_excerpt"])

    def test_an_ir_judged_against_a_tests_copy_in_the_tree_is_refused(self) -> None:
        """Round 1 (issue #534 step 2): the leaf writes a cut-down `tests.md` into its tree and
        points `meta.source_refs.tests` at it. The ref is the host's fact, so the document
        contract refuses it, the repair turn names the node's own path, and nothing of the
        copy reaches the written IR."""
        from tools.tests.test_leaf_compile import _assembled_ir

        def decoy(c, tree, kwargs):
            (tree / "mytests.md").write_text("## 1. Tests\n", encoding="utf-8")
            ir = _assembled_ir()
            ir["meta"]["source_refs"] = {"tests": str(tree / "mytests.md")}
            _writes_ir(ir)(c, tree, kwargs)

        self.c.leaf_actions = [decoy, _writes_ir()]
        self.assertEqual(self.run_substep().status, "pass")
        first = self.meta()["per_attempt"][0]
        self.assertEqual(first["failure_category"], wc.COMPILE_IR_DOCUMENT_VIOLATION)
        self.assertIn(f"{self.refs.spec_path}/tests.md", first["failure_excerpt"])
        self.assertNotIn("mytests.md", (self.repo / self.refs.ir_ref / "spec.ir.yaml").read_text())

    def test_a_compile_static_reopen_continues_the_open_lineage_warm(self) -> None:
        self.assertEqual(self.run_substep().status, "pass")
        evidence = self.c.__dict__.setdefault("evidence", {})
        self.c.leaf_actions = [_writes_ir()]
        with mock.patch.object(ort, "_read_launch_request_payload",
                               lambda r, o, *, agent_run_id:
                               evidence.get(f"{agent_run_id}.request.input.json")):
            outcome = self.run_substep(repair={
                "repair_strategy": "reuse", "repair_target_agent_run_id": "child-1",
                "repair_findings": "zero_rhs_invariance carries per_case_status",
                "issue_severity": "major", "repair_reason": "compile_static_violation"})
        self.assertEqual(outcome.status, "pass", outcome)
        request = self.requests()[-1]
        self.assertEqual((request["workshop_lineage_id"], request["warm_resume"]),
                         ("child-1", True))
        self.assertEqual(self.c.spawns[-1]["resume_session_id"], "child-1")
        # The re-attached lineage's seeds are rendered with the surface too: its catalog stays.
        self.assertIn("dep__apply",
                      (self.paths("child-1").tree / "DEPENDENCIES.md").read_text())

    def test_a_handler_outliving_the_stop_is_summarized_as_a_compile_failure(self) -> None:
        self.c.live_handlers = 1
        outcome = self.run_substep()
        self.assertEqual(outcome.infra_error[0], "workshop_wrapper_not_stopped")
        finals = [c[1]["--agent-run-json"] for c in self.c.calls if c[0] == "finalize-child"]
        self.assertEqual(finals[0]["result_summary"],
                         "workshop_compile_fail: workshop_wrapper_not_stopped")

    def test_a_seed_named_after_an_output_file_is_refused(self) -> None:
        spec = self.c._leaf_producer_spec("compile")
        bad = spec._replace(workshop_document_seeds={
            **spec.workshop_document_seeds, "tests_document": workshop_tree.IR_DOCUMENT_NAME})
        with self.assertRaises(RuntimeError) as ctx:
            self.c._workshop_seeds(self.refs, self.c._build_leaf_compile_context(self.refs),
                                   {}, bad)
        self.assertIn("named after an output file", str(ctx.exception))

    def test_compile_generate_dispatches_to_the_workshop_producer(self) -> None:
        """Dispatch: `compile.generate` runs the workshop producer loop with its tool surface.
        (Its pure half — a pure entry running the part split — went with the part split in
        issue #534 PR-J4-b.)"""
        with mock.patch.object(wc.Conductor, "_run_workshop_producer_substep",
                               return_value="WORKSHOP") as workshop:
            self.assertEqual(self.run_substep(), "WORKSHOP")
            self.assertEqual(workshop.call_args.args[-1], _SURFACE)
        self.assertEqual(workshop.call_count, 1)


class WorkshopSpecTests(_Base):
    def test_both_generate_shapes_carry_the_same_workshop_half(self) -> None:
        default = self.c._leaf_producer_spec("generate", "m3c")
        harness = self.c._leaf_producer_spec("generate", "harness")
        fields = [f for f in wc.Conductor._LeafProducerSpec._fields if f.startswith("workshop_")]
        self.assertEqual(len(fields), 8)
        for name in fields:
            with self.subTest(field=name):
                self.assertEqual(getattr(harness, name), getattr(default, name))
                self.assertIsNotNone(getattr(default, name))
        self.assertTrue(default.workshop_seeds_host_sources and default.workshop_control_file)

    def test_a_workshop_request_names_its_lineage_and_provider_together(self) -> None:
        base = dict(step="generate", substep="generate", orchestration_id="o",
                    orchestration_agent_run_id="orch", child_agent_run_id="c",
                    agent_model="opus", workflow_mode="dev", pure_leaf=True,
                    pure_context=self.c._build_leaf_context(self.refs))
        for kw in ({"workshop_lineage_id": "c"}, {"workshop_provider": "claude_cli"}):
            with self.subTest(given=kw):
                with self.assertRaises(ValueError) as ctx:
                    wc.build_launch_request(self.refs, **base, **kw)
                self.assertIn("names its lineage and its provider together", str(ctx.exception))
        req = wc.build_launch_request(self.refs, **base, workshop_lineage_id="c",
                                      workshop_provider="claude_cli")
        self.assertEqual(req["workshop_provider"], "claude_cli")
        # Neither named: since issue #534 PR-J4-b no launch shape serves a host-rendered
        # request without a lineage (the pure leaf's), so the builder refuses it.
        with self.assertRaises(ValueError) as ctx:
            wc.build_launch_request(self.refs, **base)
        self.assertIn("names its lineage (workshop_lineage_id)", str(ctx.exception))


# --- issue #616: the stale-credential re-launch ------------------------------------------

#: The recorded claude death of issue #616, verbatim (`orch_20261108T120034Z_f727c989`, agent
#: `34dbd3a7-2155-40e0-a2a1-7a97aacbf90e`, `dialogs/leaf.stdout.log`): exit 1, empty stderr.
_OAUTH_DEATH_STDOUT = (
    '{"duration_api_ms":353508,"stop_reason":"stop_sequence","session_id":"34dbd3a7-2155-40e0'
    '-a2a1-7a97aacbf90e","total_cost_usd":1.6675778,"usage":{"input_tokens":16,"cache_creatio'
    'n_input_tokens":86240,"cache_read_input_tokens":316869,"output_tokens":45711,"output_tok'
    'ens_details":{"thinking_tokens":9886},"server_tool_use":{"web_search_requests":0,"web_fe'
    'tch_requests":0},"service_tier":"standard","cache_creation":{"ephemeral_1h_input_tokens"'
    ':86240,"ephemeral_5m_input_tokens":0},"inference_geo":"global","iterations":[],"speed":"'
    'standard","fallback_credit":null},"modelUsage":{"claude-opus-5-5":{"inputTokens":16,"out'
    'putTokens":45711,"cacheReadInputTokens":316869,"cacheCreationInputTokens":86240,"webSear'
    'chRequests":0,"costUSD":1.6675778,"contextWindow":1000000,"maxOutputTokens":128000,"thin'
    'kingTokens":9886,"cacheCreationEphemeral5mInputTokens":0,"cacheCreationEphemeral1hInputT'
    'okens":86240,"canonicalModel":"claude-opus-5-5","provider":"firstParty","costBasis":"lis'
    't"}},"permission_denials":[],"terminal_reason":"api_error","fast_mode_state":"off","fast'
    '_mode_disabled_reason":"sdk_opt_in_required","subagent_stats":{"spawned":0,"requested":{'
    '"background":0,"foreground":0,"unset":0},"started_in_background":0,"max_depth":0,"spawne'
    'd_by_subagents":0,"completed":0,"failed":0,"killed":{"parent":0,"user":0,"system":0},"re'
    'fused":{"depth_limit":0,"concurrency_limit":0,"budget":0},"by_type":{}},"safety_stops":0'
    ',"is_error":true,"num_turns":17,"subtype":"success","api_error_status":null,"result":"Fa'
    'iled to authenticate: OAuth session expired and could not be refreshed","type":"result",'
    '"duration_ms":358714,"uuid":"2b107e87-a5ed-4032-928e-601e23c32484","queued_turn_count":0'
    ',"result_index":0}')


#: A transient transport death the real `_leaf_transient_retry` re-launches.
_FLAKE = "API Error: Connection closed mid-response."


def _replace_file(path: Path) -> None:
    """What a host-side refresh does: a NEW file renamed over `path`, so the path names
    another inode (an in-place write would keep it)."""
    staged = path.with_name(path.name + ".staged")
    staged.write_text('{"claudeAiOauth": {"accessToken": "refreshed"}}', encoding="utf-8")
    os.replace(staged, path)


def _rewrite_in_place(path: Path) -> None:
    """What the leaf's own refresh does: new content written onto the SAME inode, and the
    mtime moved (set an hour ahead, so a coarse filesystem clock cannot hide the move)."""
    before = os.stat(path)
    with open(path, "r+", encoding="utf-8") as fh:
        fh.seek(0)
        fh.write('{"claudeAiOauth": {"accessToken": "rewritten in place"}}')
        fh.truncate()
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 3_600 * 10**9))
    after = os.stat(path)
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    assert after.st_mtime_ns != before.st_mtime_ns


def _identity(path: Path) -> list[int]:
    st = os.stat(path)
    return [st.st_dev, st.st_ino]


class _StaleCredentialFake(_WorkshopFakeConductor):
    """The workshop fake with the REAL identity read (the shared base stubs it to None) and the
    backoff sleep recorded."""

    _credential_identity = wc.Conductor._credential_identity

    def _sleep_backoff(self, seconds):  # type: ignore[override]
        self.slept.append(seconds)


class StaleCredentialRelaunchTests(_Base):
    """`Conductor._stale_credential_relaunch` in the producer loop (issue #616). The host change
    happens INSIDE the scripted leaf action, on a temporary credentials file the real resolver
    (`operator_private_root.claude_credentials_file`) is patched to name, and the real
    `_credential_identity` stats it — so every row also pins the family-to-path mapping and
    where in the launch the identity is read."""

    def setUp(self) -> None:
        super().setUp()
        cred = tempfile.TemporaryDirectory()
        self.addCleanup(cred.cleanup)
        self.cred = Path(cred.name) / ".credentials.json"
        self.cred.write_text('{"claudeAiOauth": {"accessToken": "x"}}', encoding="utf-8")
        from tools import operator_private_root
        patcher = mock.patch.object(operator_private_root, "claude_credentials_file",
                                    lambda: self.cred)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.c = _StaleCredentialFake(
            repo_root=self.repo, orchestration_id=_ORCH, orchestration_agent_run_id="orch",
            llm_config=_config(self.repo), env={})
        self.c.homes = self.repo.parent / (self.repo.name + "_homes")
        self.c.leaf_actions = [_writes_bundle]
        self.c.slept = []
        self.events: list[dict] = []
        self.c.emit = lambda event, **f: self.events.append({"event": event, **f})  # type: ignore

    def dies(self, *, replace: bool = False, remove: bool = False, rewrite: bool = False,
             advance: float = 0.0, stdout: str = _OAUTH_DEATH_STDOUT, stderr: str = ""):
        """A launch that dies (rc 1) after optionally replacing, rewriting in place or
        removing the credentials file and advancing the patched monotonic clock by `advance`
        seconds."""
        def action(c, tree, kwargs):
            (tree / "draft.txt").write_text("kept\n", encoding="utf-8")
            if replace:
                _replace_file(self.cred)
            if rewrite:
                _rewrite_in_place(self.cred)
            if remove:
                self.cred.unlink()
            if advance:
                self.clock[0] += advance
            return wc.ProcResult(1, stdout, stderr)
        return action

    def named(self, event: str) -> list[dict]:
        return [e for e in self.events if e["event"] == event]

    def declined(self) -> dict:
        (event,) = self.named("leaf_stale_credential_relaunch_declined")
        self.assertEqual(self.named("leaf_stale_credential_relaunch"), [])
        return event

    def assert_terminal(self, outcome: wc.SubstepOutcome, spawns: int = 1) -> None:
        self.assertEqual(outcome.status, "fail", outcome)
        self.assertEqual(outcome.infra_error[0], "llm_auth_expired")
        self.assertEqual(len(self.c.spawns), spawns)
        self.assertEqual(self.c.slept, [])

    def test_a_death_on_a_replaced_credential_is_re_launched_in_a_new_lineage(self) -> None:
        """Changed identity: a cold reuse in a NEW lineage seeded from the dead one's snapshot,
        one `leaf_stale_credential_relaunch` naming both identities and the classifier's
        evidence, no sleep, then pass. Kills a capture after `spawn_leaf` (it would read the
        post-rename inode twice), a stat of any other path, a branch that does not re-launch,
        and an event whose `evidence` is emptied."""
        before = _identity(self.cred)
        seen: list[str] = []

        def finishes(c, tree, kwargs):
            seen.append((tree / "draft.txt").read_text())
            _write_bundle(tree)

        self.c.leaf_actions = [self.dies(replace=True), finishes]
        outcome = self.run_substep()
        self.assertEqual(outcome.status, "pass", outcome)
        self.assertEqual(seen, ["kept\n"])
        _first, second = self.requests()
        self.assertEqual(second["workshop_lineage_id"], "child-2")
        self.assertFalse(second.get("warm_resume"))
        self.assertIsNone(self.c.spawns[1]["resume_session_id"])
        self.assertEqual(workshop_tree.read_lineage_record(self.paths("child-1"))["ended_by"],
                         "leaf_exit")
        (event,) = self.named("leaf_stale_credential_relaunch")
        self.assertEqual((event["tag"], event["attempt"], event["max_relaunches"],
                          event["dead_agent_run_id"], event["substep"]),
                         ("llm_auth_expired", 1, 1, "child-1", "generate"))
        # The classifier's evidence line for the death's streams, carried as it is.
        tag, evidence = wc._classify_leaf_infra_error("", _OAUTH_DEATH_STDOUT)
        self.assertEqual(tag, "llm_auth_expired")
        self.assertTrue(evidence)
        self.assertEqual(event["evidence"], evidence)
        self.assertEqual(event["identity_at_launch"], before)
        self.assertEqual(event["identity_now"], _identity(self.cred))
        self.assertNotEqual(event["identity_at_launch"], event["identity_now"])
        self.assertEqual(self.named("leaf_stale_credential_relaunch_declined"), [])
        self.assertEqual(self.c.slept, [])

    def test_a_death_on_an_unchanged_credential_is_terminal(self) -> None:
        """Same inode: declined `credential_unchanged`, one launch, the outcome carrying the
        tag the phase's fail_closed reason reports. Kills a gate that re-launches without an
        identity change."""
        self.c.leaf_actions = [self.dies()]
        outcome = self.run_substep()
        self.assert_terminal(outcome)
        event = self.declined()
        self.assertEqual(event["reason"], "credential_unchanged")
        self.assertEqual(event["identity_at_launch"], _identity(self.cred))
        self.assertEqual(event["identity_now"], _identity(self.cred))
        self.assertEqual(self.named("leaf_transient_retry"), [])

    def test_an_in_place_rewrite_with_a_moved_mtime_is_unchanged(self) -> None:
        """The leaf's own refresh writes onto the shared inode and moves its mtime while host
        and leaf still agree: declined `credential_unchanged`, one launch, terminal. Kills an
        identity that adds `st_mtime_ns` (or any content-derived field) to `(st_dev, st_ino)`."""
        self.c.leaf_actions = [self.dies(rewrite=True)]
        outcome = self.run_substep()
        self.assert_terminal(outcome)
        event = self.declined()
        self.assertEqual(event["reason"], "credential_unchanged")
        self.assertEqual(event["identity_at_launch"], _identity(self.cred))
        self.assertEqual(event["identity_now"], _identity(self.cred))

    def test_a_401_on_stderr_beside_the_sentence_on_stdout_takes_the_gate(self) -> None:
        """The promotion over a terminal stderr tag: a stderr `401 authentication_error` and
        the stdout sentence are `llm_auth_expired`, terminal when unchanged and re-launched
        once when changed. Kills dropping the tag from `_CROSS_STREAM_PROMOTING_TAGS` (the
        stderr `llm_client_error` would stand and the changed half would not re-launch)."""
        stderr = ('API Error: 401 {"type":"error","error":{"type":"authentication_error",'
                  '"message":"Invalid authentication credentials"}}')
        for replace in (False, True):
            with self.subTest(changed=replace):
                self.setUp()
                self.c.leaf_actions = [self.dies(replace=replace, stderr=stderr),
                                       _writes_bundle]
                outcome = self.run_substep()
                if replace:
                    self.assertEqual(outcome.status, "pass", outcome)
                    self.assertEqual(len(self.c.spawns), 2)
                    self.assertEqual(len(self.named("leaf_stale_credential_relaunch")), 1)
                else:
                    self.assert_terminal(outcome)
                    self.assertEqual(self.declined()["reason"], "credential_unchanged")

    def test_a_credential_removed_during_the_launch_is_declined_as_missing(self) -> None:
        """The comparison-side None: declined `credential_file_missing` with `identity_now`
        null, the tag kept (not `workshop_lineage_setup_failed`). Kills a gate that reads
        `None != (dev, ino)` as a change."""
        before = _identity(self.cred)
        self.c.leaf_actions = [self.dies(remove=True)]
        outcome = self.run_substep()
        self.assert_terminal(outcome)
        event = self.declined()
        self.assertEqual((event["reason"], event["identity_at_launch"], event["identity_now"]),
                         ("credential_file_missing", before, None))

    def test_a_launch_identity_that_could_not_be_read_is_declined_as_missing(self) -> None:
        """The launch-side None on a CLI entry (the first read answers None, the comparison
        the real stat): declined `credential_file_missing` with `identity_at_launch` null.
        Kills a gate that compares only `identity_now` against the launch value."""
        calls: list[int] = []

        def first_none(entry):
            calls.append(1)
            if len(calls) == 1:
                return None
            return wc.Conductor._credential_identity(self.c, entry)

        self.c._credential_identity = first_none  # type: ignore[assignment]
        self.c.leaf_actions = [self.dies(replace=True)]
        outcome = self.run_substep()
        self.assert_terminal(outcome)
        event = self.declined()
        self.assertEqual((event["reason"], event["identity_at_launch"], event["identity_now"]),
                         ("credential_file_missing", None, _identity(self.cred)))
        self.assertEqual(len(calls), 2)

    def test_a_second_stale_death_spends_the_budget(self) -> None:
        """`MAX_STALE_CREDENTIAL_RELAUNCHES` is one: the second changed-identity death is
        declined `budget_spent`. Kills a budget that never counts."""
        self.c.leaf_actions = [self.dies(replace=True), self.dies(replace=True)]
        outcome = self.run_substep()
        self.assert_terminal(outcome, spawns=2)
        self.assertEqual(len(self.named("leaf_stale_credential_relaunch")), 1)
        (event,) = self.named("leaf_stale_credential_relaunch_declined")
        self.assertEqual((event["reason"], event["attempt"], event["dead_agent_run_id"]),
                         ("budget_spent", 2, "child-2"))
        # The budget is checked before the identity, so this death's file is never compared
        # (docs/RUNBOOK.md routes a genuine expiry on that).
        self.assertIsNone(event["identity_now"])
        self.assertIsNotNone(event["identity_at_launch"])

    def test_the_identity_is_read_per_launch_not_per_lineage(self) -> None:
        """Launch 1 leaves an empty tree and its action replaces the file (after launch 1's
        identity was read, before the warm repair launch's); the warm repair dies with no
        further change → declined `credential_unchanged`. Kills an identity captured once per
        lineage, which would compare launch 1's inode with the new one and re-launch."""
        def empty_and_replaced(c, tree, kwargs):
            _replace_file(self.cred)

        self.c.leaf_actions = [empty_and_replaced, self.dies()]
        outcome = self.run_substep()
        self.assert_terminal(outcome, spawns=2)
        self.assertEqual(self.c.spawns[1]["resume_session_id"], "child-1")
        self.assertEqual([r["workshop_lineage_id"] for r in self.requests()],
                         ["child-1", "child-1"])
        self.assertEqual(self.declined()["reason"], "credential_unchanged")

    def test_a_stale_death_of_a_warm_repair_is_re_launched_cold(self) -> None:
        """Launch 1 leaves an empty tree (a repairable defect, so launch 2 is a WARM repair of
        lineage child-1); the warm repair dies on a replaced credential → launch 3 is COLD, in
        a new lineage, resuming no session (the dead session still holds the stale view).
        Kills deleting `machine.after_transport()` from the producer stale branch: with it
        gone the open lineage and its session id survive, so launch 3 warm-resumes child-1.
        (Every other row dies on a cold first launch, where that mutant is equivalent.)"""
        def empty(c, tree, kwargs):
            return None

        self.c.leaf_actions = [empty, self.dies(replace=True), _writes_bundle]
        outcome = self.run_substep()
        self.assertEqual(outcome.status, "pass", outcome)
        self.assertEqual(len(self.c.spawns), 3)
        self.assertEqual(self.c.spawns[1]["resume_session_id"], "child-1")
        self.assertIsNone(self.c.spawns[2]["resume_session_id"])
        lineages = [r["workshop_lineage_id"] for r in self.requests()]
        self.assertEqual(lineages, ["child-1", "child-1", "child-3"])
        self.assertFalse(self.requests()[2].get("warm_resume"))
        (event,) = self.named("leaf_stale_credential_relaunch")
        self.assertEqual(event["dead_agent_run_id"], "child-2")

    def test_the_identity_is_read_after_record_launch(self) -> None:
        """Round-3 L1: `record_launch`'s host-side probes may refresh the token, so a rename
        inside `record_launch` is the launch's own identity, not a change during it → declined
        `credential_unchanged`. Kills a capture placed before `record_launch`."""
        real_runtime = self.c.runtime

        def runtime(args, *, input=None):
            result = real_runtime(args, input=input)
            if args[0] == "record-launch":
                _replace_file(self.cred)
            return result

        self.c.runtime = runtime  # type: ignore[assignment]
        self.c.leaf_actions = [self.dies()]
        outcome = self.run_substep()
        self.assert_terminal(outcome)
        self.assertEqual(self.declined()["reason"], "credential_unchanged")

    def test_a_flake_after_the_stale_re_launch_keeps_both_transient_retries(self) -> None:
        """Stale (re-launched), then two flakes: both retried by the REAL transient branch,
        then pass. Kills a stale re-launch charged to `transient_retries`."""
        flake = lambda c, t, kw: wc.ProcResult(1, "", _FLAKE)
        self.c.leaf_actions = [self.dies(replace=True), flake, flake, _writes_bundle]
        outcome = self.run_substep()
        self.assertEqual(outcome.status, "pass", outcome)
        self.assertEqual(len(self.c.spawns), 4)
        self.assertEqual([e["attempt"] for e in self.named("leaf_transient_retry")], [1, 2])
        self.assertEqual(len(self.c.slept), 2)
        self.assertEqual(len(self.named("leaf_stale_credential_relaunch")), 1)

    def test_a_stale_death_after_a_flake_is_still_re_launched(self) -> None:
        """A flake (retried), then a changed-identity death: still re-launched. Kills
        `relaunches_done` fed `transient_retries` (or the two summed)."""
        flake = lambda c, t, kw: wc.ProcResult(1, "", _FLAKE)
        self.c.leaf_actions = [flake, self.dies(replace=True), _writes_bundle]
        outcome = self.run_substep()
        self.assertEqual(outcome.status, "pass", outcome)
        self.assertEqual(len(self.c.spawns), 3)
        (event,) = self.named("leaf_stale_credential_relaunch")
        self.assertEqual((event["attempt"], event["dead_agent_run_id"]), (1, "child-2"))

    def run_clocked(self) -> wc.SubstepOutcome:
        self.clock = [1_000.0]
        with mock.patch.object(wc.time, "monotonic", lambda: self.clock[0]):
            return self.run_substep()

    def test_the_stale_attempt_is_not_charged_to_the_transient_wall_clock(self) -> None:
        """A 500 s stale death (re-launched), then a 200 s flake: the flake is still retried
        (0 + 200 <= 600 s). Kills the branch placed after `transient_spent += elapsed_s`,
        which computes 700 s and declines the flake."""
        def flake(c, tree, kwargs):
            self.clock[0] += 200.0
            return wc.ProcResult(1, "", _FLAKE)

        self.c.leaf_actions = [self.dies(replace=True, advance=500.0), flake, _writes_bundle]
        outcome = self.run_clocked()
        self.assertEqual(outcome.status, "pass", outcome)
        self.assertEqual(len(self.c.spawns), 3)
        self.assertEqual(self.named("leaf_transient_retry_declined"), [])
        self.assertEqual(len(self.named("leaf_transient_retry")), 1)

    def test_a_stale_death_over_the_wall_clock_budget_is_still_re_launched(self) -> None:
        """A changed-identity death that ran 700 s, over
        `TRANSIENT_RETRY_WALL_CLOCK_BUDGET_SECONDS`, is re-launched: the budget bounds the
        transient branch alone. Kills a stale branch that applies the wall-clock budget."""
        self.assertGreater(700.0, wc.TRANSIENT_RETRY_WALL_CLOCK_BUDGET_SECONDS)
        self.c.leaf_actions = [self.dies(replace=True, advance=700.0), _writes_bundle]
        outcome = self.run_clocked()
        self.assertEqual(outcome.status, "pass", outcome)
        self.assertEqual(len(self.named("leaf_stale_credential_relaunch")), 1)
        self.assertEqual(self.named("leaf_transient_retry_declined"), [])

    def test_a_flake_with_a_changed_identity_takes_the_transient_branch(self) -> None:
        """The branch requires the tag: a transport flake whose launch also saw the file
        replaced is a transient retry, not a stale re-launch. Kills a branch armed by the
        identity alone."""
        self.c.leaf_actions = [self.dies(replace=True, stdout="", stderr=_FLAKE),
                               _writes_bundle]
        outcome = self.run_substep()
        self.assertEqual(outcome.status, "pass", outcome)
        self.assertEqual(len(self.named("leaf_transient_retry")), 1)
        self.assertEqual(self.named("leaf_stale_credential_relaunch"), [])
        self.assertEqual(self.named("leaf_stale_credential_relaunch_declined"), [])


if __name__ == "__main__":
    unittest.main()
