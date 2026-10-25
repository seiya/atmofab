"""The READING kind of `workshop leaf` (issue #534 step 3): the three judging substeps run by
`Conductor._run_pure_reviewer_substep`'s workshop branch.

The leaf is a fake `spawn_leaf` that writes the reviewer's one document into the lineage's
tree, the way the real CLI's file tools would; the lineage home is a temporary directory.
Everything host-side — the seeds (the bundle under review expanded into files), the lineage
record, the snapshot, the one-file assembly, the document validators, the host projection, the
per-attempt meta — is real. The launch record is the stubbed `runtime` of `_PureFakeConductor`;
the real request builder, renderer and validator are driven for every judging pair below.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tools.llm_config as lc
import tools.orchestration_runtime as ort
import tools.workflow_conductor as wc
from tools import pure_leaf, workshop_tree
from tools.tests.test_pure_leaf_compile import _write_compile_node
from tools.tests.test_pure_leaf_judge import _review, _write_run_node
from tools.tests.test_pure_leaf_producer import _PureFakeConductor, _valid_bundle
from tools.tests.test_pure_leaf_verify import _verdict, _verify_node
from tools.tests.test_workshop_producer import _codex_proc

_ORCH = "o"


def _config(repo: Path, phase: str, substep: str, extra: str = "") -> lc.LlmConfig:
    path = repo / "llm_reading.yaml"
    path.write_text(
        "defaults: {provider: claude_cli, model: opus}\n"
        f"phases: {{{phase}: {{substeps: {{{substep}: {{provider: claude_cli, model: opus, "
        f"leaf: workshop, lineage_timeout_s: 2700{extra}" + "}}}}\n",
        encoding="utf-8")
    return lc.load_llm_config(path)


def _codex_config(repo: Path, phase: str, substep: str) -> lc.LlmConfig:
    path = repo / "llm_reading_codex.yaml"
    path.write_text(
        "defaults: {provider: codex_cli, model: gpt-x}\n"
        f"phases: {{{phase}: {{substeps: {{{substep}: {{provider: codex_cli, model: gpt-x, "
        "leaf: workshop, lineage_timeout_s: 2700}}}}\n",
        encoding="utf-8")
    return lc.load_llm_config(path)


def _envelope() -> str:
    return json.dumps({"result": "done", "is_error": False, "model": "claude-opus-5-5",
                       "usage": {"output_tokens": 10}, "session_id": "s", "num_turns": 4})


class _ReviewerFake(_PureFakeConductor):
    """The pure fake, with the lineage home faked and the leaf scripted:
    `self.leaf_actions[i](conductor, tree, kwargs)` runs as launch i and may return a
    `ProcResult` (default: an envelope, rc 0). No wrapper is faked: a reading lineage is served
    none, and a call to `_start_wrapper_endpoint` / a freeze is a failure here."""

    homes: Path
    leaf_actions: list
    resumable = True

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

    def _start_wrapper_endpoint(self, ctx, on_end):  # type: ignore[override]
        raise AssertionError("a reading lineage is served no endpoint")

    def _workshop_session_resumable(self, paths, session_id):  # type: ignore[override]
        return self.resumable

    def _pure_session_resumable(self, *args, **kwargs):  # type: ignore[override]
        raise AssertionError("the workshop branch never consults the operator's sessions")

    def spawn_leaf(self, prompt_text, child_env, entry=None, **kwargs):  # type: ignore
        self.__dict__.setdefault("spawns", []).append(kwargs)
        request = [c for c in self.calls if c[0] == "record-launch"][-1][1]["--request-json"]
        tree = (self.homes / self.orchestration_id / self._workshop_backend(entry)
                / request["workshop_lineage_id"] / "tree")
        index = len(self.spawns) - 1
        if index > len(self.leaf_actions) + 6:
            raise AssertionError(f"unbounded re-launch: {index + 1} launches")
        action = self.leaf_actions[min(index, len(self.leaf_actions) - 1)]
        result = action(self, tree, kwargs)
        if result is not None:
            return result
        return _codex_proc() if entry.provider == "codex_cli" else wc.ProcResult(
            0, _envelope(), "")


def _writes(name: str, doc):
    def action(c, tree, kwargs):
        (tree / name).write_text(doc if isinstance(doc, str) else json.dumps(doc),
                                 encoding="utf-8")
    return action


def _writes_nothing(c, tree, kwargs):
    return None


def _dies(c, tree, kwargs):
    return wc.ProcResult(1, "", "boom")


def _times_out_after(name: str, doc):
    def action(c, tree, kwargs):
        (tree / name).write_text(json.dumps(doc), encoding="utf-8")
        return wc.ProcResult(124, _envelope(), "", timed_out=True)
    return action


class _Base(unittest.TestCase):
    PHASE = "compile"
    SUBSTEP = "verify"
    OUTPUT = "verdict.json"
    #: What the provider-specific parts of a launch are on this class's entry: the provider,
    #: the session id a warm turn of lineage `child-1` resumes, and the turn count an attempt
    #: row records (codex reports none).
    PROVIDER = "claude_cli"
    RESUMED = "child-1"
    TURNS: int | None = 4

    def stage(self, repo: Path) -> wc.NodeRefs:
        return _write_compile_node(repo, stage_ir=True)

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        (self.repo / "workspace" / "orchestrations" / _ORCH).mkdir(parents=True)
        self.refs = self.stage(self.repo)
        self.c = _ReviewerFake(
            repo_root=self.repo, orchestration_id=_ORCH, orchestration_agent_run_id="orch",
            llm_config=_config(self.repo, self.PHASE, self.SUBSTEP), env={})
        self.c.calls = []
        self.c.homes = self.repo.parent / (self.repo.name + "_homes")
        self.addCleanup(lambda: shutil.rmtree(self.c.homes, ignore_errors=True))
        self.c.leaf_actions = [_writes(self.OUTPUT, self.passing())]

    def passing(self) -> dict:
        return _verdict("pass")

    def failing(self) -> dict:
        return _verdict("fail", severity="minor")

    def run_substep(self) -> wc.SubstepOutcome:
        return self.c.run_substep(self.refs, self.PHASE, self.SUBSTEP)

    def requests(self) -> list[dict]:
        return [c[1]["--request-json"] for c in self.c.calls if c[0] == "record-launch"]

    def paths(self, lineage: str) -> workshop_tree.LineagePaths:
        return workshop_tree.LineagePaths(
            homes_root=self.c.homes, orchestration_id=_ORCH, lineage_id=lineage,
            orchestration_dir=self.repo / "workspace" / "orchestrations" / _ORCH)

    def attempt_meta(self) -> dict:
        return json.loads((self.repo / self.refs.ir_ref
                           / "compile_verify_meta.json").read_text())

    def assert_projected_pass(self) -> None:
        meta = json.loads((self.repo / self.refs.ir_ref / "ir_meta.json").read_text())
        self.assertEqual((meta["verification_status"], meta["issue_severity"]),
                         ("pass", "none"))

    def seeds(self) -> dict[str, str]:
        return dict(wc.Conductor._WORKSHOP_REVIEWER_DOCUMENT_SEEDS[
            (self.PHASE, self.SUBSTEP, "")])


class CompileVerifyTests(_Base):
    def test_a_pass_verdict_in_the_tree_is_projected_as_a_pure_reply_is(self) -> None:
        outcome = self.run_substep()
        self.assertEqual((outcome.status, outcome.leaf_returncode), ("pass", 0))
        self.assert_projected_pass()
        vmeta = self.attempt_meta()
        self.assertEqual((vmeta["result"], vmeta["leaf_mode"],
                          vmeta["workshop_prompt_contract_version"],
                          vmeta["prompt_contract_version"]),
                         ("pass", "workshop", pure_leaf.WORKSHOP_PROMPT_CONTRACT_VERSION,
                          pure_leaf.PURE_PROMPT_CONTRACT_VERSION))
        row = vmeta["per_attempt"][0]
        self.assertEqual((row["lineage_id"], row["warm"], row["model_turns"],
                          row["wrapper_calls"], row["ended_by"]),
                         ("child-1", False, self.TURNS, 0, None))
        self.assertIsInstance(row["leaf_wall_s"], float)

    def test_the_reading_launch_is_bounded_by_its_lineage_timeout(self) -> None:
        """The reading kind's ONLY bound is the spawn timeout (`_workshop_timeout`): the
        lineage's remaining `lineage_timeout_s`, named so, or the environment cap when that is
        smaller."""
        with mock.patch.object(wc, "_leaf_timeout_seconds", return_value=0):
            self.run_substep()
        (spawn,) = self.c.spawns
        self.assertEqual(spawn["timeout_name"], "lineage_timeout_s")
        self.assertTrue(2690 <= spawn["timeout_seconds"] <= 2700, spawn["timeout_seconds"])

    def test_the_kill_s_exit_code_is_in_the_reply_and_the_lineage_record(self) -> None:
        """The two places beside `leaf_rc` the docs name for a bound-ended launch's exit."""
        self.c.leaf_actions = [_times_out_after(self.OUTPUT, self.failing())]
        self.run_substep()
        (reply,) = [c[1]["--reply-text"] for c in self.c.calls if c[0] == "finalize-child"]
        self.assertIn("leaf rc=124", reply)
        record = workshop_tree.read_lineage_record(self.paths("child-1"))
        self.assertEqual(record["last_launch"]["rc"], 124)

    def test_the_launch_is_a_reading_workshop_launch_served_no_endpoint(self) -> None:
        self.run_substep()
        (request,) = self.requests()
        self.assertEqual((request["leaf_mode"], request["workshop_lineage_id"],
                          request["workshop_provider"], request["prompt_contract_version"]),
                         ("workshop", "child-1", self.PROVIDER,
                          pure_leaf.WORKSHOP_PROMPT_CONTRACT_VERSION))
        (spawn,) = self.c.spawns
        self.assertIsNone(spawn["mcp_config_json"])
        self.assertIsNone(spawn["cancel_event"])
        self.assertTrue(spawn["reading"])
        record = workshop_tree.read_lineage_record(self.paths("child-1"))
        self.assertEqual(record["wrapper_calls_used"], 0)

    def test_the_tree_holds_every_context_document_under_its_seed_name(self) -> None:
        self.run_substep()
        tree = self.paths("child-1").tree
        context = self.c._build_pure_compile_verify_context(self.refs)
        for key, name in self.seeds().items():
            with self.subTest(key=key):
                self.assertEqual((tree / name).read_text(),
                                 wc._indented_json_seed(context[key]) if name.endswith(".json")
                                 else context[key])
        self.assertEqual((tree / "CONTRACT.md").read_text(), "PROMPT")
        # The snapshot holds the reviewer's document and no seed.
        names = sorted(p.name for p in self.paths("child-1").snapshot.iterdir())
        self.assertEqual(names, [self.OUTPUT])

    def test_a_valid_fail_verdict_left_by_a_lineage_its_bound_ended_reaches_the_severity_gate(
            self) -> None:
        """MR1's routing row: a lineage killed at its bound whose tree holds a schema-valid
        `fail` verdict returns rc 0, so `run_phase` routes the VERDICT (verify severity), not
        the kill (`leaf_transport_error`)."""
        self.c.leaf_actions = [_times_out_after(self.OUTPUT, self.failing())]
        outcome = self.run_substep()
        self.assertEqual((outcome.status, outcome.leaf_returncode, outcome.infra_error),
                         ("fail", 0, None))
        row = self.attempt_meta()["per_attempt"][0]
        self.assertEqual(row["ended_by"], "lineage_timeout")
        # The kill's own exit is on the attempt row, though the outcome's is 0.
        self.assertEqual(row["leaf_rc"], 124)
        decision = self.c.classify_failure(self.refs, self.PHASE, [None, None, outcome])
        self.assertEqual((decision.action, decision.repair_strategy), ("retry", "reuse"))

    def test_a_pass_left_by_a_lineage_its_bound_ended_is_run_again_from_the_seeds(
            self) -> None:
        """Round-1 F1 and round-2 F-A: a reviewer that writes a provisional `pass` and is
        killed at its bound has stood behind no finished review. It is not REPAIRED — a repair
        turn carrying the document certified it by ending at once (round 2 measured it) — but
        run again: a fresh cold launch, the whole launch prompt and no repair turn, in a tree
        seeded from the seeds alone. A turn that then does nothing leaves no document."""
        trees: list[bool] = []

        def does_nothing(c, tree, kwargs):
            trees.append((tree / self.OUTPUT).exists())

        self.c.leaf_actions = [_times_out_after(self.OUTPUT, self.passing()), does_nothing,
                               _writes(self.OUTPUT, self.passing())]
        outcome = self.run_substep()
        self.assertEqual(outcome.status, "pass")
        self.assertEqual(trees, [False])
        rows = self.attempt_meta()["per_attempt"]
        self.assertEqual(len(rows), 3)
        category = (wc.SEMANTIC_REVIEW_DOCUMENT_VIOLATION if self.PHASE == "validate"
                    else wc.GENERATE_VERDICT_SCHEMA_VIOLATION)
        self.assertEqual((rows[0]["ended_by"], rows[0]["failure_category"]),
                         ("lineage_timeout", category))
        # The finding names THIS kind's document and says what happens next.
        self.assertIn(f"{self.OUTPUT} said pass", rows[0]["failure_excerpt"])
        self.assertIn("run again from the seeds alone", rows[0]["failure_excerpt"])
        second = self.requests()[1]
        self.assertEqual(second["workshop_lineage_id"], "child-2")
        self.assertFalse(second.get("warm_resume"))
        self.assertNotIn("repair_findings", second)
        self.assertEqual(second["repair_strategy"], "none")
        self.assertIn("pure_context", second)
        self.assertIsNone(self.c.spawns[1]["resume_session_id"])
        # The re-run's own empty tree is an ordinary schema repair of ITS lineage.
        self.assertEqual(rows[1]["failure_category"], category)
        self.assertEqual(self.requests()[2]["workshop_lineage_id"], "child-2")

    def test_a_pass_never_finished_exhausts_into_the_schema_category(self) -> None:
        self.c.leaf_actions = [_times_out_after(self.OUTPUT, self.passing())]
        outcome = self.run_substep()
        self.assertEqual((outcome.status, outcome.leaf_returncode), ("fail", 0))
        meta = self.attempt_meta()
        self.assertEqual(meta["result"], "fail")
        self.assertIn("ended by its bound", meta["failure_excerpt"])
        self.assertEqual(len(meta["per_attempt"]), 1 + pure_leaf.MAX_BUNDLE_REPAIR_TURNS)

    def test_a_document_that_is_not_an_object_is_repaired_like_any_schema_defect(
            self) -> None:
        """`assemble_document_file` refuses a JSON value that is not one object (a list here):
        it costs one counted schema repair under the kind's category, never an uncategorised
        terminal failure."""
        self.c.leaf_actions = [_writes(self.OUTPUT, [self.passing()]),
                               _writes(self.OUTPUT, self.passing())]
        outcome = self.run_substep()
        self.assertEqual(outcome.status, "pass")
        first = self.attempt_meta()["per_attempt"][0]
        self.assertEqual(first["failure_category"],
                         wc.SEMANTIC_REVIEW_DOCUMENT_VIOLATION if self.PHASE == "validate"
                         else wc.GENERATE_VERDICT_SCHEMA_VIOLATION)
        self.assertIn("must hold one JSON object; got list", first["failure_excerpt"])

    def test_anything_but_a_valid_fail_at_the_bound_is_run_again_from_the_seeds(self) -> None:
        """Round 3: the unfinished-review rule keyed on a schema-VALID `pass` only, so a
        malformed `pass` (or no document) at the bound took the ordinary repair — a cold reuse
        CARRYING the document under the shape-only framing — and a shape edit certified it.
        Now a launch its bound ended is accepted only with a schema-valid `fail`; anything
        else is re-run fresh from the seeds, carrying nothing and resuming nothing."""
        broken = (dict(self.passing(), unexpected=1) if self.PHASE != "validate"
                  else dict(self.passing(), decision="maybe"))
        for label, left in (("malformed pass", broken), ("no document", None)):
            with self.subTest(left=label):
                self.setUp()
                carried: list[bool] = []

                def first(c, tree, kwargs, left=left):
                    if left is not None:
                        (tree / self.OUTPUT).write_text(json.dumps(left), encoding="utf-8")
                    return wc.ProcResult(124, _envelope(), "", timed_out=True)

                def second(c, tree, kwargs):
                    carried.append((tree / self.OUTPUT).exists())
                    (tree / self.OUTPUT).write_text(json.dumps(self.passing()),
                                                    encoding="utf-8")

                self.c.leaf_actions = [first, second]
                self.assertEqual(self.run_substep().status, "pass")
                self.assertEqual(carried, [False])
                req = self.requests()[1]
                self.assertEqual((req["workshop_lineage_id"], req["repair_strategy"]),
                                 ("child-2", "none"))
                self.assertNotIn("repair_findings", req)
                self.assertIsNone(self.c.spawns[1]["resume_session_id"])
                row = self.attempt_meta()["per_attempt"][0]
                self.assertIn("run again from the seeds alone", row["failure_excerpt"])
                self.assertIn(f"{self.OUTPUT} was refused", row["failure_excerpt"])

    def test_a_killed_warm_repair_is_re_run_with_no_repair_payload(self) -> None:
        """A schema repair in progress whose warm turn is killed with a `pass` in its tree:
        the re-run drops the repair target (no payload naming a document that is not there),
        and runs in a fresh tree."""
        broken = (dict(self.passing(), unexpected=1) if self.PHASE != "validate"
                  else dict(self.passing(), decision="maybe"))
        self.c.leaf_actions = [_writes(self.OUTPUT, broken),
                               _times_out_after(self.OUTPUT, self.passing()),
                               _writes(self.OUTPUT, self.passing())]
        self.assertEqual(self.run_substep().status, "pass")
        first, second, third = self.requests()
        self.assertEqual((second["workshop_lineage_id"], second["repair_strategy"]),
                         ("child-1", "reuse"))
        self.assertEqual((third["workshop_lineage_id"], third["repair_strategy"]),
                         ("child-3", "none"))
        self.assertNotIn("repair_findings", third)
        self.assertIn("pure_context", third)

    def test_a_seed_the_reviewer_edited_is_restored_before_its_warm_turn(self) -> None:
        """The host's documents, as the host placed them: an edit the reviewer made to a seed
        in one turn is gone when its warm repair turn starts."""
        name = next(iter(self.seeds().values()))
        seen: list[str] = []

        def edits_a_seed(c, tree, kwargs):
            (tree / name).write_text("EDITED BY THE LEAF\n", encoding="utf-8")

        def reads_the_seed(c, tree, kwargs):
            seen.append((tree / name).read_text())
            (tree / self.OUTPUT).write_text(json.dumps(self.passing()), encoding="utf-8")

        self.c.leaf_actions = [edits_a_seed, reads_the_seed]
        self.assertEqual(self.run_substep().status, "pass")
        self.assertTrue(self.c.spawns[1]["resume_session_id"])
        self.assertNotEqual(seen, ["EDITED BY THE LEAF\n"])
        self.assertTrue(seen[0])

    def test_an_absent_document_is_the_schema_category_and_is_repaired_warm(self) -> None:
        self.c.leaf_actions = [_writes_nothing, _writes(self.OUTPUT, self.passing())]
        outcome = self.run_substep()
        self.assertEqual(outcome.status, "pass")
        first, second = self.requests()
        self.assertEqual(second["workshop_lineage_id"], "child-1")
        self.assertTrue(second.get("warm_resume"))
        self.assertIn(f"no `{self.OUTPUT}` in the tree", second["repair_findings"])
        self.assertNotIn("prior_document", second)
        self.assertEqual(self.c.spawns[1]["resume_session_id"], self.RESUMED)
        rows = self.attempt_meta()["per_attempt"]
        self.assertEqual(rows[0]["failure_category"], wc.GENERATE_VERDICT_SCHEMA_VIOLATION
                         if self.PHASE != "validate"
                         else wc.SEMANTIC_REVIEW_DOCUMENT_VIOLATION)
        self.assertEqual((rows[1]["lineage_id"], rows[1]["warm"]), ("child-1", True))

    def test_an_unresumable_lineage_is_repaired_by_a_cold_reuse_carrying_the_document(
            self) -> None:
        """A schema repair whose session cannot be resumed is a cold reuse seeded from the
        lineage's snapshot — the document under repair — and its prompt re-states the launch
        body (the verdict repair template)."""
        self.c.resumable = False
        broken = dict(self.passing(), unexpected=1) if self.PHASE != "validate" else dict(
            self.passing(), decision="maybe")
        self.c.leaf_actions = [_writes(self.OUTPUT, broken), _writes_nothing]
        outcome = self.run_substep()
        self.assertEqual(outcome.status, "fail")
        requests = self.requests()
        self.assertEqual(requests[1]["workshop_lineage_id"], "child-2")
        self.assertFalse(requests[1].get("warm_resume"))
        self.assertEqual(json.loads((self.paths("child-2").tree / self.OUTPUT).read_text()),
                         broken)
        self.assertEqual(ort._pure_repair_template_name(requests[1]),
                         "workshop verdict repair")
        self.assertEqual(workshop_tree.read_lineage_record(self.paths("child-1"))["ended_by"],
                         "superseded")

    def test_a_dead_leaf_s_document_is_not_carried_into_its_retry(self) -> None:
        """The reading kind's own reseed rule: a transport death's retry is seeded from the
        seeds ALONE. The dead leaf wrote a passing document and died; had its snapshot been
        carried, the retry — which writes nothing — would have certified it."""
        def writes_pass_and_dies(c, tree, kwargs):
            (tree / self.OUTPUT).write_text(json.dumps(self.passing()), encoding="utf-8")
            return wc.ProcResult(1, "", "connection reset by peer (ECONNRESET)")
        self.c.leaf_actions = [writes_pass_and_dies, _writes_nothing]
        with mock.patch.object(wc.Conductor, "_pure_transient_retry", return_value=True):
            outcome = self.run_substep()
        self.assertEqual(outcome.status, "fail")
        self.assertFalse((self.paths("child-2").tree / self.OUTPUT).exists())
        self.assertNotEqual(self.attempt_meta()["result"], "pass")

    def test_a_dead_warm_repair_is_retried_cold_never_resumed(self) -> None:
        """A repair turn that resumes the reviewer's lineage and then dies — to a usage limit
        that is waited out, or a transient error that is retried — is ended `leaf_exit`, and
        the re-launch starts a NEW lineage seeded from the seeds alone: resuming the dead
        session would continue a half-written turn."""
        for label, patches in (
                ("usage wait", (mock.patch.object(wc, "_leaf_infra_error",
                                                  return_value=("llm_usage_limit", "limit")),
                                mock.patch.object(wc.Conductor, "_usage_limit_wait",
                                                  return_value=True))),
                ("transient retry", (mock.patch.object(wc.Conductor, "_pure_transient_retry",
                                                       return_value=True),))):
            with self.subTest(retry=label):
                self.setUp()
                self.c.leaf_actions = [_writes_nothing, _dies,
                                       _writes(self.OUTPUT, self.passing())]
                with patches[0], (patches[1] if len(patches) > 1 else mock.MagicMock()):
                    outcome = self.run_substep()
                self.assertEqual(outcome.status, "pass")
                first, second, third = self.c.spawns
                self.assertEqual(second["resume_session_id"], self.RESUMED)
                self.assertIsNone(third["resume_session_id"])
                requests = self.requests()
                self.assertEqual([r["workshop_lineage_id"] for r in requests],
                                 ["child-1", "child-1", "child-3"])
                self.assertEqual(
                    workshop_tree.read_lineage_record(self.paths("child-1"))["ended_by"],
                    "leaf_exit")
                # The retry still carries the schema findings (the transport death did not
                # overwrite them).
                self.assertIn(f"no `{self.OUTPUT}` in the tree", requests[2]["repair_findings"])

    def test_a_transport_death_keeps_its_rc(self) -> None:
        self.c.leaf_actions = [_dies]
        outcome = self.run_substep()
        self.assertEqual(outcome.status, "fail")
        self.assertEqual(outcome.leaf_returncode, 1)
        self.assertEqual(self.attempt_meta()["failure_category"], "pure_transport")

    def test_a_schema_exhaustion_is_routed_by_its_category_not_by_a_kill(self) -> None:
        self.c.leaf_actions = [_times_out_after(self.OUTPUT, {"not": "a verdict"})]
        outcome = self.run_substep()
        self.assertEqual((outcome.status, outcome.leaf_returncode, outcome.infra_error),
                         ("fail", 0, None))
        meta = self.attempt_meta()
        self.assertEqual(meta["result"], "fail")
        self.assertIn("the lineage ended by lineage_timeout", meta["failure_excerpt"])


class GenerateVerifyTests(CompileVerifyTests):
    PHASE = "generate"

    def stage(self, repo: Path) -> wc.NodeRefs:
        return _verify_node(repo)

    def attempt_meta(self) -> dict:
        return json.loads((self.repo / self.refs.source_dir() / "verdict_meta.json").read_text())

    def assert_projected_pass(self) -> None:
        meta = json.loads((self.repo / self.refs.source_dir() / "source_meta.json").read_text())
        self.assertEqual((meta["verification_status"], meta["issue_severity"]),
                         ("pass", "none"))

    def test_the_tree_holds_every_context_document_under_its_seed_name(self) -> None:
        """The bundle under review is EXPANDED: its manifest under `bundle_manifest.yaml` and
        each file at its `logical_path`, which assemble back to the certified document."""
        self.run_substep()
        tree = self.paths("child-1").tree
        context = self.c._build_pure_verify_context(self.refs)
        for key, name in self.seeds().items():
            with self.subTest(key=key):
                self.assertEqual((tree / name).read_text(),
                                 wc._indented_json_seed(context[key]) if name.endswith(".json")
                                 else context[key])
        for entry in _valid_bundle()["files"]:
            self.assertEqual((tree / entry["logical_path"]).read_text(), entry["content"])
        scratch = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, scratch, True)
        for entry in _valid_bundle()["files"]:
            shutil.copyfile(tree / entry["logical_path"], scratch / entry["logical_path"])
        shutil.copyfile(tree / "bundle_manifest.yaml",
                        scratch / workshop_tree.BUNDLE_MANIFEST_NAME)
        self.assertEqual(workshop_tree.assemble_bundle_document(scratch, ()),
                         json.loads(context["bundle_document"]))
        self.assertFalse((tree / workshop_tree.BUNDLE_MANIFEST_NAME).exists())
        names = sorted(p.name for p in self.paths("child-1").snapshot.iterdir())
        self.assertEqual(names, [self.OUTPUT])


class ValidateJudgeTests(CompileVerifyTests):
    PHASE = "validate"
    SUBSTEP = "judge"
    OUTPUT = "review.json"

    def stage(self, repo: Path) -> wc.NodeRefs:
        return _write_run_node(repo)

    def passing(self) -> dict:
        return _review("pass")

    def failing(self) -> dict:
        return _review("fail")

    def attempt_meta(self) -> dict:
        return json.loads((self.repo / self.refs.run_node_dir() / "judge_meta.json").read_text())

    def assert_projected_pass(self) -> None:
        review = json.loads((self.repo / self.refs.run_node_dir()
                             / "semantic_review.json").read_text())
        self.assertEqual((review["decision"], review["review_method"]),
                         ("pass", "llm_semantic_review"))

    def test_the_tree_holds_every_context_document_under_its_seed_name(self) -> None:
        self.run_substep()
        tree = self.paths("child-1").tree
        context = self.c._build_pure_judge_context(self.refs)
        for key, name in self.seeds().items():
            with self.subTest(key=key):
                text = (tree / name).read_text()
                if name.endswith(".json"):
                    # The same document, one value per line (`_indented_json_seed`).
                    self.assertEqual(json.loads(text), json.loads(context[key]))
                    self.assertEqual(text, json.dumps(json.loads(context[key]), indent=2,
                                                      ensure_ascii=False) + "\n")
                else:
                    self.assertEqual(text, context[key])
        # The host's verdict is a seed under its own name, never the output's.
        self.assertTrue((tree / "host_verdict.json").is_file())
        names = sorted(p.name for p in self.paths("child-1").snapshot.iterdir())
        self.assertEqual(names, [self.OUTPUT])

    def test_a_valid_fail_verdict_left_by_a_lineage_its_bound_ended_reaches_the_severity_gate(
            self) -> None:
        """The judge's twin: a `fail` review in the tree of a lineage its bound ended reaches
        `escalate("judge_semantic_review_fail")`, not the transport route."""
        self.c.leaf_actions = [_times_out_after(self.OUTPUT, self.failing())]
        outcome = self.run_substep()
        self.assertEqual((outcome.status, outcome.leaf_returncode, outcome.infra_error),
                         ("fail", 0, None))
        review = json.loads((self.repo / self.refs.run_node_dir()
                             / "semantic_review.json").read_text())
        self.assertEqual(review["decision"], "fail")
        # `evidence_refs` name document KEYS, resolved by the host to workspace paths.
        self.assertTrue(review["findings"][0]["evidence_refs"][0].startswith(
            f"{self.refs.run_node_dir()}/diagnostics.json#"))

    def test_an_evidence_ref_naming_a_seed_file_is_refused(self) -> None:
        """The tree shows file names; `evidence_refs` still name document keys, and a file name
        there is a schema violation repaired as any other (a cost, never a resolution)."""
        bad = _review("fail")
        bad["findings"][0]["evidence_refs"] = ["diagnostics.json#per_case"]
        self.c.leaf_actions = [_writes(self.OUTPUT, bad), _writes(self.OUTPUT, _review("fail"))]
        outcome = self.run_substep()
        self.assertEqual(outcome.status, "fail")
        self.assertIn("diagnostics.json", self.requests()[1]["repair_findings"])
        self.assertEqual(len(self.attempt_meta()["per_attempt"]), 2)


class CodexValidateJudgeTests(ValidateJudgeTests):
    """Every `validate.judge` row above, on a `codex_cli` reading workshop entry (step 3
    PR-J2): the lineage lives under the codex container, a warm turn resumes the THREAD the
    launch emitted rather than the agent_run_id, and an attempt row records no turn count and
    the completed-item count — the places a claude-only assumption of the reviewer branch
    would show."""

    PROVIDER = "codex_cli"
    RESUMED = "thread-child-1"
    TURNS = None

    def setUp(self) -> None:
        super().setUp()
        self.c.llm_config = _codex_config(self.repo, self.PHASE, self.SUBSTEP)
        self.c._session_id_for_child = (  # type: ignore[assignment]
            lambda arid, entry=None: f"thread-{arid}")

    def paths(self, lineage: str) -> workshop_tree.LineagePaths:
        return workshop_tree.LineagePaths(
            homes_root=self.c.homes, orchestration_id=_ORCH, lineage_id=lineage,
            orchestration_dir=self.repo / "workspace" / "orchestrations" / _ORCH,
            backend="codex")

    def test_a_codex_attempt_records_the_synthetic_envelope(self) -> None:
        self.assertEqual(self.run_substep().status, "pass")
        self.assertTrue(self.paths("child-1").tree.is_dir())
        self.assertFalse((self.c.homes / _ORCH / "claude").exists())
        vmeta = self.attempt_meta()
        self.assertEqual((vmeta["leaf_mode"], vmeta["workshop_prompt_contract_version"]),
                         ("workshop", pure_leaf.WORKSHOP_PROMPT_CONTRACT_VERSION))
        row = vmeta["per_attempt"][0]
        self.assertEqual((row["model"], row["model_turns"], row["items_completed"],
                          row["wrapper_calls"]), ("gpt-x", None, 3, 0))
        (spawn,) = self.c.spawns
        self.assertTrue(spawn["reading"])
        self.assertIsNone(spawn["mcp_config_json"])

    def test_a_schema_violating_review_is_repaired_warm_on_the_emitted_thread(self) -> None:
        self.c.leaf_actions = [_writes(self.OUTPUT, {"decision": "maybe"}),
                               _writes(self.OUTPUT, self.passing())]
        self.assertEqual(self.run_substep().status, "pass")
        _, second = self.requests()
        self.assertTrue(second.get("warm_resume"))
        self.assertEqual(second["workshop_lineage_id"], "child-1")
        self.assertEqual(second["workshop_provider"], "codex_cli")
        self.assertEqual(ort._pure_repair_template_name(second), "workshop verdict repair")
        self.assertEqual(self.c.spawns[1]["resume_session_id"], "thread-child-1")
        self.assertTrue(self.c.spawns[1]["reading"])


class JsonSeedLayoutTests(unittest.TestCase):
    """Every JSON seed of every reading pair is laid out one value per line (step 3 PR-J2): the
    reviewer's file tools page by line, and both CLIs cut a long single line with no way to page
    on. Driven through the reviewer spec's own expanders, on a one-line document over the cut."""

    def test_each_json_seed_of_each_pair_is_indented(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        c = _PureFakeConductor(repo_root=repo, orchestration_id=_ORCH,
                               orchestration_agent_run_id="orch",
                               llm_config=_config(repo, "validate", "judge"), env={})
        doc = {"per_case": {f"case_{i:03d}": {"metrics": {"x": i * 0.5, "ok": True}}
                            for i in range(3000)}}
        one_line = json.dumps(doc, separators=(",", ":"))
        self.assertGreater(len(one_line), 120000)
        self.assertNotIn("\n", one_line)
        seen_json = 0
        for (phase, substep, shape), seeds in wc.Conductor._WORKSHOP_REVIEWER_DOCUMENT_SEEDS.items():
            fields = c._workshop_reviewer_fields(phase, substep, shape, "x")
            for key, name in seeds.items():
                with self.subTest(pair=(phase, substep, shape), key=key):
                    expand = fields["workshop_seed_expanders"].get(key)
                    if not name.endswith(".json"):
                        self.assertIsNone(expand)
                        continue
                    seen_json += 1
                    (out_name, text), = expand(one_line).items()
                    self.assertEqual(out_name, name)
                    self.assertEqual(json.loads(text), doc)
                    self.assertLess(max(len(ln) for ln in text.splitlines()), 200)
        self.assertGreaterEqual(seen_json, 10)

    def test_a_seed_that_is_not_json_is_seeded_as_it_is(self) -> None:
        for text in ("", "not json", "{\"a\": 1", "(absent)\n"):
            with self.subTest(text=text):
                self.assertEqual(wc._indented_json_seed(text), text)


class LaunchShapeTests(unittest.TestCase):
    """The argv and the recorded setting surface of a reading launch."""

    def _conductor(self, phase: str = "validate", substep: str = "judge") -> wc.Conductor:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        return _PureFakeConductor(repo_root=repo, orchestration_id=_ORCH,
                                  orchestration_agent_run_id="orch",
                                  llm_config=_config(repo, phase, substep), env={})

    def test_a_reading_argv_carries_no_endpoint_and_no_wrapper_grant(self) -> None:
        c = self._conductor()
        entry = c.entry_for("validate", "judge")
        argv = c.leaf_command(entry, session_id="a", reading=True)
        self.assertNotIn("--mcp-config", argv)
        self.assertNotIn("--allowedTools", argv)
        self.assertIn("--strict-mcp-config", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Write,Edit,Glob,Grep")
        self.assertEqual(argv[argv.index("--system-prompt") + 1],
                         pure_leaf.WORKSHOP_READER_SYSTEM_PROMPT)

    def test_the_kind_and_the_endpoint_must_agree(self) -> None:
        c = self._conductor()
        entry = c.entry_for("validate", "judge")
        mcp = pure_leaf.workshop_mcp_config_json("http://127.0.0.1:5/mcp")
        with self.assertRaises(ValueError):
            c.leaf_command(entry, session_id="a", reading=True, mcp_config_json=mcp)
        with self.assertRaises(ValueError):
            c.leaf_command(entry, session_id="a")
        with self.assertRaises(ValueError):
            c.leaf_command(c.entry_for("compile", "generate"), session_id="a", reading=True)

    def test_the_recorded_surface_of_a_reading_launch_names_no_endpoint(self) -> None:
        c = self._conductor()
        entry = c.entry_for("validate", "judge")
        surface = c._launch_setting_surface(
            "a", {"step": "validate", "substep": "judge"}, entry)
        self.assertEqual(surface, {"claude_tools": ["Read,Write,Edit,Glob,Grep"]})

    def _codex_conductor(self) -> wc.Conductor:
        """Codex on `validate.judge` as a reading workshop entry (step 3 PR-J2)."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        return _PureFakeConductor(repo_root=repo, orchestration_id=_ORCH,
                                  orchestration_agent_run_id="orch",
                                  llm_config=_codex_config(repo, "validate", "judge"), env={})

    def test_the_recorded_surface_of_a_codex_reading_launch_names_no_endpoint(self) -> None:
        c = self._codex_conductor()
        surface = c._launch_setting_surface(
            "a", {"step": "validate", "substep": "judge"}, c.entry_for("validate", "judge"))
        self.assertEqual(surface, {})

    def test_a_codex_reading_argv_is_shaped_without_the_wrapper(self) -> None:
        """The codex reading argv (the lineage a codex judging entry launches) names no
        wrapper server and no approval for its tools, cold and on resume."""
        c = self._codex_conductor()
        entry = c.entry_for("validate", "judge")
        self.assertEqual(entry.leaf, "workshop")
        for resume in (None, "thread-1"):
            with self.subTest(resume=resume):
                argv = c.leaf_command(entry, resume_session_id=resume, reading=True)
                joined = " ".join(argv)
                self.assertNotIn("mcp_servers", joined)
                self.assertIn('approval_policy="never"', argv)
                self.assertIn("features.apps=false", argv)
                self.assertIn("workspace-write", joined)

    def test_the_validator_refuses_a_reading_request_on_a_provider_with_no_reader_row(
            self) -> None:
        """The validator asks the table of the request's KIND, independently of the renderer
        (which runs first in `prepare_launch_request_payload`): a codex judging request passes
        while the reader table names codex, and is refused once it does not, while the same
        provider on a producing pair is not."""
        req = RenderTests()._request("validate", "judge", workshop_provider="codex_cli")
        ort._validate_pure_launch_request_payload(req)
        claude_only = {"claude_cli": pure_leaf.WORKSHOP_READER_TOOL_SURFACE_TEXT["claude_cli"]}
        with mock.patch.dict(pure_leaf.WORKSHOP_READER_TOOL_SURFACE_TEXT, claude_only,
                             clear=True):
            with self.assertRaises(ValueError) as ctx:
                ort._validate_pure_launch_request_payload(req)
            self.assertIn("one of claude_cli; got 'codex_cli'", str(ctx.exception))
            producing = RenderTests()._request("compile", "generate",
                                               workshop_provider="codex_cli")
            ort._validate_pure_launch_request_payload(producing)

    def test_the_codex_reading_overrides_name_no_wrapper(self) -> None:
        self.assertEqual(pure_leaf.codex_workshop_overrides(None, reading=True),
                         ["--config", 'approval_policy="never"',
                          "--config", "features.apps=false"])
        with self.assertRaises(ValueError):
            pure_leaf.codex_workshop_overrides(
                pure_leaf.workshop_mcp_config_json("http://127.0.0.1:5/mcp"), reading=True)


class RenderTests(unittest.TestCase):
    """Every judging pair's workshop request renders through the production entry point and
    passes the launch validator; the cold prompt states the seed names, not the documents."""

    def _request(self, step: str, substep: str, shape: str = "", **extra) -> dict:
        keys = (ort.PURE_CONTEXT_REQUIRED_KEYS_BY_SHAPE[(step, substep, shape)] if shape
                else ort.PURE_CONTEXT_REQUIRED_KEYS[(step, substep)])
        refs = wc.NodeRefs(target_id="fortran_cpu", node_key="component/x@0.1.0",
                           spec_path="spec/component/x", ir_id="ir_20260908_001",
                           pipeline_id="p_20260908_001", source_id="src_20260908_001",
                           binary_id="bin_20260908_001", run_id="run_20260908_001")
        req = wc.build_launch_request(
            refs, step=step, substep=substep, orchestration_id=_ORCH,
            orchestration_agent_run_id="orch", child_agent_run_id="ar_x",
            agent_model="opus", workflow_mode="dev", pure_leaf=True, pure_shape=shape,
            pure_language="fortran", pure_parallel="openmp",
            pure_context={k: f"DOCUMENT-{k}" for k in keys},
            workshop_lineage_id="ar_x", workshop_provider="claude_cli")
        req.update(extra)
        return req

    PAIRS = (("compile", "verify", ""), ("generate", "verify", ""),
             ("generate", "verify", "harness"), ("validate", "judge", ""))

    #: The claude paging rule, which the reading templates do not carry since `workshop-5`:
    #: true of claude's `Read`, false of codex's shell (its output cut keeps the last line).
    PAGING = ("A read of a file may stop before the end of the file and say so; continue "
              "from the last line it returned until you reach the end.")

    def test_each_pair_renders_its_reading_template_and_passes_the_validator(self) -> None:
        rows = pure_leaf.WORKSHOP_READER_TOOL_SURFACE_TEXT
        for provider, other in (("claude_cli", "codex_cli"), ("codex_cli", "claude_cli")):
            for step, substep, shape in self.PAIRS:
                with self.subTest(provider=provider, pair=(step, substep, shape)):
                    req = ort.prepare_launch_request_payload(
                        self._request(step, substep, shape, workshop_provider=provider))
                    ort._validate_launch_request_payload(req)
                    text = ort.render_launch_prompt_text(req)
                    self.assertTrue(text.startswith(pure_leaf.WORKSHOP_PROMPT_SENTINEL))
                    self.assertNotIn("DOCUMENT-", text)
                    self.assertNotIn("mcp__wrapper", text)
                    self.assertIn(rows[provider], text)
                    self.assertNotIn(rows[other], text)
                    # Case-folded: the templates carried it as "…judge: a read of a file…".
                    self.assertEqual(text.lower().count(self.PAGING.lower()),
                                     1 if provider == "claude_cli" else 0)
                    self.assertIn(f"prompt_contract_version: "
                                  f"{pure_leaf.WORKSHOP_PROMPT_CONTRACT_VERSION}", text)
        for step, substep, shape in self.PAIRS:
            with self.subTest(pair=(step, substep, shape)):
                req = ort.prepare_launch_request_payload(self._request(step, substep, shape))
                text = ort.render_launch_prompt_text(req)
                output = wc.Conductor._WORKSHOP_REVIEW_OUTPUT_BY_SUBSTEP[substep]
                self.assertIn(f"`{output}`", text)
                for key, name in wc.Conductor._WORKSHOP_REVIEWER_DOCUMENT_SEEDS[
                        (step, substep, shape)].items():
                    self.assertIn(f"In your tree: `{name}` (document key `{key}`)", text)

    def test_a_reading_request_on_a_provider_with_no_reader_row_is_refused(self) -> None:
        req = self._request("validate", "judge", workshop_provider="codex_cli")
        ort.prepare_launch_request_payload(dict(req))
        claude_only = {"claude_cli": pure_leaf.WORKSHOP_READER_TOOL_SURFACE_TEXT["claude_cli"]}
        with (mock.patch.dict(pure_leaf.WORKSHOP_READER_TOOL_SURFACE_TEXT, claude_only,
                              clear=True),
              self.assertRaises(ValueError) as ctx):
            ort.prepare_launch_request_payload(req)
        self.assertIn("one of claude_cli", str(ctx.exception))

    def test_the_repair_turn_of_a_reviewer_is_the_verdict_repair(self) -> None:
        for step, substep, shape in self.PAIRS:
            with self.subTest(pair=(step, substep, shape)):
                req = self._request(step, substep, shape, repair_strategy="reuse",
                                    repair_findings="bad shape", warm_resume=False)
                self.assertEqual(ort._pure_repair_template_name(req),
                                 "workshop verdict repair")
                text = ort._render_pure_repair_prompt(req)
                self.assertIn(ort.PURE_REPAIR_SCOPE_PARAGRAPH, text)
                self.assertNotIn("Your prior document under repair", text)
                # The cold repair re-states the whole launch body, seeds included.
                self.assertIn("Work tree contract", text)
                self.assertEqual(text.count(self.PAGING), 1)
                codex = ort._render_pure_repair_prompt(
                    self._request(step, substep, shape, repair_strategy="reuse",
                                  repair_findings="bad shape", warm_resume=False,
                                  workshop_provider="codex_cli"))
                self.assertIn(pure_leaf.WORKSHOP_READER_TOOL_SURFACE_TEXT["codex_cli"], codex)
                self.assertNotIn(self.PAGING, codex)
                # A warm repair's session holds the launch prompt: neither is re-stated.
                warm = ort._render_pure_repair_prompt(
                    self._request(step, substep, shape, repair_strategy="reuse",
                                  repair_findings="bad shape", warm_resume=True))
                self.assertNotIn(self.PAGING, warm)
                self.assertNotIn("<tool_surface>", warm)


class ReaderTemplateTests(unittest.TestCase):
    _DIR = Path(ort.__file__).resolve().parent / "prompt_templates"
    _PAIRS = (("pure_compile_verify.txt", "workshop_compile_verify.txt"),
              ("pure_generate_verify.txt", "workshop_generate_verify.txt"),
              ("pure_generate_verify_harness.txt", "workshop_generate_verify_harness.txt"),
              ("pure_validate_judge.txt", "workshop_validate_judge.txt"))
    #: The one pure unit a reading twin REPLACES: a reviewer in a tree reads with file tools.
    _REPLACED = "Do not use filesystem or shell tools: everything you need is inlined below."

    def test_the_reviewer_rules_are_the_pure_templates_verbatim(self) -> None:
        """Every line of a pure reviewer template after its persona — the output contract, the
        checklist, the input labels, the language and parallel markers, the identity block — is
        in its reading twin, in order, except the document slots, which the twin replaces with
        the seed's tree name and document key; and the persona's no-tools sentence is the one
        unit replaced. So a rule edited in one and not the other fails here."""
        for pure_name, twin_name in self._PAIRS:
            with self.subTest(template=twin_name):
                pure = (self._DIR / pure_name).read_text().split("\n")
                twin = (self._DIR / twin_name).read_text().split("\n")
                self.assertIn(self._REPLACED, pure[0])
                self.assertNotIn(self._REPLACED, "\n".join(twin))
                kept = [ln for ln in pure[2:] if not ort._PURE_PLACEHOLDER_RE.fullmatch(ln)]
                position = 0
                for line in kept:
                    self.assertIn(line, twin[position:], line[:80])
                    position = twin.index(line, position) + 1

    def test_no_document_is_inlined(self) -> None:
        for _pure, name in self._PAIRS:
            with self.subTest(template=name):
                slots = set(ort._PURE_PLACEHOLDER_RE.findall((self._DIR / name).read_text()))
                documents = set().union(*(set(v) for v in
                                          wc.Conductor._WORKSHOP_REVIEWER_DOCUMENT_SEEDS
                                          .values())) | {"bundle_document", "dependency_facts"}
                self.assertEqual(slots & documents, set())
                self.assertIn("tool_surface", slots)

    def test_the_verdict_repair_has_the_slots_its_renderer_fills(self) -> None:
        text = (self._DIR / "workshop_verdict_repair.txt").read_text()
        self.assertTrue(text.startswith(pure_leaf.WORKSHOP_PROMPT_SENTINEL))
        self.assertEqual(
            set(ort._PURE_PLACEHOLDER_RE.findall(text)),
            {"node_key", "step", "substep", "orchestration_id", "agent_run_id",
             "prompt_contract_version", "repair_scope", "findings", "tool_surface",
             "launch_body"})



class HarnessSpecTests(unittest.TestCase):
    """The `harness` shape's reviewer carries its own seed table: its context has the
    runner-output contract where the m3c reviewer's has the checks-module contract."""

    def test_the_harness_reviewer_seeds_its_own_context(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        refs = _verify_node(repo)
        c = _PureFakeConductor(repo_root=repo, orchestration_id=_ORCH,
                               orchestration_agent_run_id="orch",
                               llm_config=_config(repo, "generate", "verify"), env={})
        spec = c._pure_reviewer_spec("generate", "verify", "harness")
        self.assertEqual(spec.pure_shape, "harness")
        self.assertEqual(spec.workshop_outputs, ("verdict.json",))
        context = {k: f"TEXT-{k}" for k in
                   ort.PURE_CONTEXT_REQUIRED_KEYS_BY_SHAPE[("generate", "verify", "harness")]}
        context["bundle_document"] = json.dumps(_valid_bundle())
        seeds = c._workshop_seeds(refs, context, {}, spec)
        self.assertEqual(seeds["runner_output_contract.md"],
                         "TEXT-runner_output_contract_document")
        self.assertNotIn("CHECKS_MODULE_CONTRACT.md", seeds)
        self.assertIn("bundle_manifest.yaml", seeds)
        for entry in _valid_bundle()["files"]:
            self.assertEqual(seeds[entry["logical_path"]], entry["content"])


class PureRecordTests(unittest.TestCase):
    """`leaf_wall_s` is a field of EVERY attempt row, the pure loops' included."""

    def test_pure_rows_carry_the_launch_wall_clock(self) -> None:
        from tools.tests.test_pure_leaf_producer import _conductor, _envelope
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        refs = _verify_node(repo)
        c = _conductor(repo)
        c.envelopes = [_envelope(_verdict("pass"))]
        self.assertEqual(c.run_substep(refs, "generate", "verify").status, "pass")
        row = json.loads((repo / refs.source_dir() / "verdict_meta.json").read_text())[
            "per_attempt"][0]
        self.assertIsInstance(row["leaf_wall_s"], float)
        self.assertNotIn("leaf_mode", json.loads(
            (repo / refs.source_dir() / "verdict_meta.json").read_text()))
        c.envelopes = [_envelope(_valid_bundle())]
        c.run_substep(refs, "generate", "generate")
        row = json.loads((repo / refs.source_dir() / "bundle_meta.json").read_text())[
            "per_attempt"][0]
        self.assertIsInstance(row["leaf_wall_s"], float)


class LaunchSiteTests(unittest.TestCase):
    def test_every_launch_site_passing_an_endpoint_passes_the_kind(self) -> None:
        """The kind is passed in (`ResolvedLeafEntry` names no substep), so a production call
        to `leaf_command` / `spawn_leaf` that hands over `mcp_config_json` must hand over
        `reading` beside it: one that did not would launch a reviewer with the producer's flag
        set, or refuse every reading launch. Read off the conductor's own source; the twin of
        `test_every_leaf_launch_site_passes_its_own_entry`."""
        import ast
        src = Path(wc.__file__).read_text(encoding="utf-8")
        sites, offenders = 0, []
        for node in ast.walk(ast.parse(src)):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("leaf_command", "spawn_leaf")
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "self"):
                continue
            names = {k.arg for k in node.keywords}
            if "mcp_config_json" in names:
                sites += 1
                if "reading" not in names:
                    offenders.append(f"line {node.lineno}: {node.func.attr}")
        self.assertGreaterEqual(sites, 3)
        self.assertEqual(offenders, [])

if __name__ == "__main__":
    unittest.main()
