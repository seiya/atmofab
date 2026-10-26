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


def _terminated(text: str) -> str:
    """A seed's text as `_workshop_seeds` writes it (issue #548): one `\n` appended to a
    non-empty text that lacks it."""
    return text + "\n" if text and not text.endswith("\n") else text


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


#: What a clean codex launch of the fake shows its model and the host's capture records: one
#: command, whole (issue #547).
_CLEAN_TEXTS = ("Script completed\nWall time 0.1 seconds\nOutput:\n", "# tests\n")
_CLEAN_COMMANDS = (("/usr/bin/zsh -c 'cat tests.md'", 8),)


def _rollout_rows(texts) -> list[dict]:
    """One codex launch's rollout rows: its `task_started` and one `exec` output whose items
    are `texts`."""
    return [{"type": "event_msg", "payload": {"type": "task_started"}},
            {"type": "response_item", "payload": {
                "type": "custom_tool_call_output", "call_id": "call_1",
                "output": [{"type": "input_text", "text": t} for t in texts]}}]


class _ReviewerFake(_PureFakeConductor):
    """The pure fake, with the lineage home faked and the leaf scripted:
    `self.leaf_actions[i](conductor, tree, kwargs)` runs as launch i and may return a
    `ProcResult` (default: an envelope, rc 0). No wrapper is faked: a reading lineage is served
    none, and a call to `_start_wrapper_endpoint` / a freeze is a failure here.

    On a `codex_cli` entry each launch also does what the real CLI leaves for the host's
    cut-read check (issue #547): it APPENDS its rows to its thread's rollout in the lineage's
    `config/sessions/` (a warm launch resumes the cold one's thread, so its rows land after the
    first launch's), and its result carries `command_outputs`. Both default to one clean read;
    an action sets `c.turn_rows` (a list of rollout rows, written as they are) or
    `c.turn_commands` to change them, or `c.turn_rows = None` / `c.turn_commands = None` to
    leave the rollout unwritten / the capture absent."""

    homes: Path
    leaf_actions: list
    resumable = True
    _UNSET = object()

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
        self.turn_rows = self._UNSET
        self.turn_commands = self._UNSET
        result = action(self, tree, kwargs)
        if entry.provider == "codex_cli":
            result = result if result is not None else _codex_proc()
            self._codex_record(tree.parent / "config", kwargs, result)
            return result
        if result is not None:
            return result
        return wc.ProcResult(0, _envelope(), "")

    def _codex_record(self, config: Path, kwargs: dict, result: wc.ProcResult) -> None:
        """Append this launch's rollout rows and set its `command_outputs`."""
        threads = self.__dict__.setdefault("threads", {})
        thread = kwargs.get("resume_session_id") or f"thread-{kwargs['child_arid']}"
        threads[kwargs["child_arid"]] = thread
        rows = (_rollout_rows(_CLEAN_TEXTS) if self.turn_rows is self._UNSET
                else self.turn_rows)
        if rows is not None:
            day = config / "sessions" / "2026" / "10" / "25"
            day.mkdir(parents=True, exist_ok=True)
            with (day / f"rollout-2026-10-25T00-00-00-{thread}.jsonl").open(
                    "a", encoding="utf-8") as fh:
                fh.writelines(json.dumps(r) + "\n" for r in rows)
        if result.command_outputs is None:
            commands = (list(_CLEAN_COMMANDS) if self.turn_commands is self._UNSET
                        else self.turn_commands)
            result.command_outputs = list(commands) if commands is not None else None


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
                self.assertEqual((tree / name).read_text(), _terminated(
                    wc._indented_json_seed(context[key]) if name.endswith(".json")
                    else context[key]))
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

    def test_the_reading_helper_is_a_seed_of_a_codex_tree_only(self) -> None:
        """Issue #534 PR-J3-a: `read.py` is placed in every codex READING tree, with the host's
        text, and in no claude one; it is a host-placed name, so the snapshot never carries it
        (the snapshot row above)."""
        self.run_substep()
        helper = self.paths("child-1").tree / pure_leaf.CODEX_READ_HELPER_NAME
        if self.PROVIDER == "codex_cli":
            self.assertEqual(helper.read_text(), pure_leaf.CODEX_READ_HELPER_TEXT)
        else:
            self.assertFalse(helper.exists())
        self.assertNotIn(pure_leaf.CODEX_READ_HELPER_NAME,
                         [p.name for p in self.paths("child-1").snapshot.iterdir()])

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
        each file at its `logical_path`, which assemble back to the certified document. Each
        copy may differ from the certified content by exactly one appended `\n` (issue #548);
        this fixture's contents are terminated, so here the assembly is the document itself.
        No production path reassembles a reviewer tree."""
        self.run_substep()
        tree = self.paths("child-1").tree
        context = self.c._build_pure_verify_context(self.refs)
        for key, name in self.seeds().items():
            with self.subTest(key=key):
                self.assertEqual((tree / name).read_text(), _terminated(
                    wc._indented_json_seed(context[key]) if name.endswith(".json")
                    else context[key]))
        for entry in _valid_bundle()["files"]:
            self.assertEqual((tree / entry["logical_path"]).read_text(),
                             _terminated(entry["content"]))
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

    def test_the_warm_re_place_writes_the_contract_with_its_last_newline(self) -> None:
        """Issue #548 on the path that rewrites seeds into a tree the leaf may have edited:
        the re-placed `CHECKS_MODULE_CONTRACT.md` ends in `\n`, so a line count reaches its
        last line."""
        name = "CHECKS_MODULE_CONTRACT.md"
        seen: list[str] = []

        def edits_the_contract(c, tree, kwargs):
            (tree / name).write_text("EDITED, no final newline", encoding="utf-8")

        def reads_the_contract(c, tree, kwargs):
            seen.append((tree / name).read_text())
            (tree / self.OUTPUT).write_text(json.dumps(self.passing()), encoding="utf-8")

        self.c.leaf_actions = [edits_the_contract, reads_the_contract]
        self.assertEqual(self.run_substep().status, "pass")
        self.assertTrue(self.c.spawns[1]["resume_session_id"])
        context = self.c._build_pure_verify_context(self.refs)
        self.assertFalse(context["checks_module_contract_document"].endswith("\n"))
        self.assertEqual(seen, [context["checks_module_contract_document"] + "\n"])


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
                    self.assertEqual(text, _terminated(context[key]))
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


def _codex_reading(case) -> None:
    """Put `case`'s fake on a `codex_cli` reading entry of its pair: the thread a launch emits
    is the one it resumed, else `thread-<arid>` (what `_ReviewerFake` recorded)."""
    case.c.llm_config = _codex_config(case.repo, case.PHASE, case.SUBSTEP)
    # The homes OUTSIDE /tmp, as production's `~/.atmofab/homes` is: a codex reading lineage
    # whose home a codex shell can write is refused (`_codex_shell_writable_root`).
    homes = tempfile.TemporaryDirectory(dir="/var/tmp")
    case.addCleanup(homes.cleanup)
    case.c.homes = Path(homes.name).resolve() / "homes"
    case.c._session_id_for_child = (  # type: ignore[assignment]
        lambda arid, entry=None: case.c.__dict__.get("threads", {}).get(arid, f"thread-{arid}"))


def _codex_paths(case, lineage: str) -> workshop_tree.LineagePaths:
    return workshop_tree.LineagePaths(
        homes_root=case.c.homes, orchestration_id=_ORCH, lineage_id=lineage,
        orchestration_dir=case.repo / "workspace" / "orchestrations" / _ORCH, backend="codex")


_BUDGET = pure_leaf.CODEX_EXEC_OUTPUT_BUDGET_BYTES
_CUT_HEADER = ("Warning: truncated output (original token count: 150000)\n"
               "Total output lines: 20000\n\nL00001 x\n\u20269000 tokens truncated\u2026L20000 x\n")


def _reads(*, rows=_ReviewerFake._UNSET, commands=_ReviewerFake._UNSET, then=None):
    """A launch that records `rows` in its rollout and `commands` in the host's capture, then
    does `then` (another action) if given."""
    def action(c, tree, kwargs):
        result = then(c, tree, kwargs) if then is not None else None
        c.turn_rows, c.turn_commands = rows, commands
        return result
    return action


def _over(size: int = 600_000) -> list[tuple[str, int]]:
    return [("/usr/bin/zsh -c 'cat host_verdict.json'", size)]


class _CodexCutReadRows:
    """The host's refusal of a cut read on a codex reading lineage (issue #547), driven through
    `_run_pure_reviewer_substep` with the fake CLI's rollout and capture. Mixed into one class
    per judging pair below (the m3c shape of `generate.verify`; the `harness` shape is
    rendered by `RenderTests` only), so each also runs every row of its pair's class on codex: the lineage
    under the codex container, a warm turn resuming the THREAD the launch emitted, and an attempt
    row with no turn count and the completed-item count."""

    PROVIDER = "codex_cli"
    RESUMED = "thread-child-1"
    TURNS = None

    def setUp(self) -> None:
        super().setUp()  # type: ignore[misc]
        _codex_reading(self)

    def paths(self, lineage: str) -> workshop_tree.LineagePaths:
        return _codex_paths(self, lineage)

    def rows(self) -> list[dict]:
        return self.attempt_meta()["per_attempt"]

    def test_a_pass_after_a_command_over_the_budget_is_refused_and_reviewed_again_cold(
            self) -> None:
        """The cut-read repair is a COLD launch in a new lineage from the seeds alone, with the
        findings and the whole launch body: not a warm turn (its session holds the cut texts and
        the conclusion), and nothing of the refused tree carried."""
        seen: list[bool] = []

        def second(c, tree, kwargs):
            seen.append((tree / self.OUTPUT).exists())
            (tree / self.OUTPUT).write_text(json.dumps(self.passing()), encoding="utf-8")

        self.c.leaf_actions = [_reads(commands=_over(), then=_writes(self.OUTPUT, self.passing())),
                               second]
        outcome = self.run_substep()
        self.assertEqual(outcome.status, "pass")
        self.assert_projected_pass()
        self.assertEqual(seen, [False])
        first, row2 = self.rows()
        self.assertEqual((first["failure_category"], first["cut_outputs"]),
                         (wc.WORKSHOP_CUT_READ, 1))
        self.assertIn("cat host_verdict.json", first["failure_excerpt"])
        self.assertIn("600,000 bytes", first["failure_excerpt"])
        self.assertEqual((row2["cut_outputs"], row2["warm"]), (0, False))
        self.assertNotEqual(row2["lineage_id"], first["lineage_id"])
        self.assertNotIn("failure_category", row2)
        self.assertEqual(workshop_tree.read_lineage_record(self.paths("child-1"))["ended_by"],
                         "superseded")
        _, request = self.requests()
        self.assertFalse(request.get("warm_resume"))
        self.assertIsNone(self.c.spawns[1]["resume_session_id"])
        self.assertEqual(request["repair_reason"], wc.WORKSHOP_CUT_READ_REPAIR_REASON)
        self.assertIn("cat host_verdict.json", request["repair_findings"])
        prompt = ort._render_pure_repair_prompt(request)
        self.assertIn(ort.WORKSHOP_CUT_READ_SCOPE_PARAGRAPH, prompt)
        self.assertNotIn(ort.PURE_REPAIR_SCOPE_PARAGRAPH, prompt)
        self.assertIn("cat host_verdict.json", prompt)
        self.assertIn(pure_leaf.WORKSHOP_READER_TOOL_SURFACE_TEXT["codex_cli"], prompt)
        self.assertIn("Work tree contract", prompt)

    def test_a_rewritten_helper_is_re_placed_before_the_warm_turn(self) -> None:
        name = pure_leaf.CODEX_READ_HELPER_NAME
        seen: list[str] = []

        def rewrites_the_helper(c, tree, kwargs):
            (tree / name).write_text("print('all clear')\n", encoding="utf-8")

        def reads_the_helper(c, tree, kwargs):
            seen.append((tree / name).read_text())
            (tree / self.OUTPUT).write_text(json.dumps(self.passing()), encoding="utf-8")

        self.c.leaf_actions = [rewrites_the_helper, reads_the_helper]
        self.assertEqual(self.run_substep().status, "pass")
        self.assertTrue(self.c.spawns[1]["resume_session_id"])
        self.assertEqual(seen, [pure_leaf.CODEX_READ_HELPER_TEXT])

    def test_a_chained_read_s_finding_says_one_file_per_command(self) -> None:
        chained = [("/usr/bin/zsh -lc \"sed -n '1,125p' tests.md; sed -n '1,125p' m.f90\"",
                    40_963)]
        for commands, clause in ((chained, True), (_over(), False)):
            with self.subTest(clause=clause):
                self.c.calls = []
                self.c.spawns = []
                self.c.leaf_actions = [
                    _reads(commands=commands, then=_writes(self.OUTPUT, self.passing())),
                    _writes(self.OUTPUT, self.passing())]
                self.assertEqual(self.run_substep().status, "pass")
                self.assertIn("printed", self.rows()[0]["failure_excerpt"])
                findings = self.requests()[1]["repair_findings"]
                self.assertEqual(wc._CHAINED_READ_CLAUSE in findings, clause, findings)

    def test_which_commands_count_as_chained_reads(self) -> None:
        """`_reads_chained` over the capture's spelling (`<shell> -lc <script>`): a list
        operator outside quotes, or two read programs on separate lines, is chained; one read,
        a quoted `;`, or an unsplittable text is not."""
        rows = {
            "/usr/bin/zsh -lc \"sed -n '1,9p' a; sed -n '1,9p' b\"": True,
            "/usr/bin/zsh -lc \"sed -n '1,9p' a && cat b\"": True,
            "/usr/bin/zsh -lc \"cat a | head -n 5\"": True,
            # A list operator beside ONE read program: the operator clause alone.
            "/usr/bin/zsh -lc \"cat a | wc -c\"": True,
            "/usr/bin/zsh -lc \"cat a && echo done\"": True,
            "/usr/bin/zsh -lc \"cat a || echo none\"": True,
            "/usr/bin/zsh -lc \"cat a & wait\"": True,
            "/usr/bin/zsh -lc \"sed -n 1,9p a\nsed -n 1,9p b\"": True,
            "/usr/bin/zsh -lc \"python3 read.py tests.md 41\"": False,
            "/bin/bash -lc \"grep -n ';' x\"": False,
            "/usr/bin/zsh -lc \"cat 'unterminated\"": False,
        }
        for command, chained in rows.items():
            with self.subTest(command):
                self.assertEqual(wc._reads_chained(command), chained)

    def test_a_rollout_only_cut_names_its_call_and_a_delegation_is_refused(self) -> None:
        call = {"type": "response_item", "payload": {
            "type": "custom_tool_call", "call_id": "call_1", "name": "exec",
            "input": "text((await tools.exec_command({cmd: 'cat tests.md'})).output);"}}
        spawn = {"type": "response_item", "payload": {
            "type": "function_call", "call_id": "s", "name": "spawn_agent", "arguments": "{}"}}
        for name, rows, expect in (
                ("cut", [_rollout_rows(())[0], call,
                         _rollout_rows(["Output:\n", _CUT_HEADER])[1]],
                 "the output of `text((await tools.exec_command({cmd: 'cat tests.md'}))"),
                ("delegation", [*_rollout_rows(_CLEAN_TEXTS), spawn], "sub-agent")):
            with self.subTest(name):
                self.c.calls = []
                self.c.spawns = []
                self.c.leaf_actions = [_reads(rows=rows, then=_writes(self.OUTPUT, self.passing())),
                                       _writes(self.OUTPUT, self.passing())]
                self.assertEqual(self.run_substep().status, "pass")
                first = self.rows()[0]
                self.assertEqual((first["failure_category"], first["cut_outputs"]),
                                 (wc.WORKSHOP_CUT_READ, 1))
                self.assertIn(expect, first["failure_excerpt"])

    def test_the_findings_name_six_and_count_the_rest(self) -> None:
        commands = [(f"cat part{i}", 50_000) for i in range(9)]
        self.c.leaf_actions = [_reads(commands=commands, then=_writes(self.OUTPUT, self.passing())),
                               _writes(self.OUTPUT, self.passing())]
        self.assertEqual(self.run_substep().status, "pass")
        # The repair turn's findings (the attempt row clips its excerpt shorter).
        excerpt = self.requests()[1]["repair_findings"]
        self.assertEqual(wc.Conductor._CUT_READ_FINDINGS_NAMED, 6)
        self.assertIn("cat part5", excerpt)
        self.assertNotIn("cat part6", excerpt)
        self.assertIn("and 3 more", excerpt)

    def test_the_byte_rule_is_strictly_greater_than_the_budget(self) -> None:
        self.c.leaf_actions = [_reads(commands=_over(_BUDGET),
                                      then=_writes(self.OUTPUT, self.passing()))]
        self.assertEqual(self.run_substep().status, "pass")
        self.assertEqual(self.rows()[0]["cut_outputs"], 0)
        self.c.calls = []
        self.c.spawns = []
        self.c.leaf_actions = [_reads(commands=_over(_BUDGET + 1),
                                      then=_writes(self.OUTPUT, self.passing())),
                               _writes(self.OUTPUT, self.passing())]
        self.assertEqual(self.run_substep().status, "pass")
        self.assertEqual(self.rows()[0]["failure_category"], wc.WORKSHOP_CUT_READ)

    def test_a_cut_text_in_the_rollout_is_refused_whatever_the_capture_says(self) -> None:
        for name, texts in (
                ("header at the item start", ["Script completed\nOutput:\n", _CUT_HEADER]),
                ("inside a stringified result", ["Output:\n", json.dumps({"output": _CUT_HEADER})]),
                ("after a prefix the script printed", ["A\n" + _CUT_HEADER]),
                ("marker alone (a raised budget)", ["L00001\n\u2026138012 tokens truncated\u2026L19202\n"])):
            with self.subTest(name):
                self.c.calls = []
                self.c.spawns = []
                self.c.leaf_actions = [
                    _reads(rows=_rollout_rows(texts), then=_writes(self.OUTPUT, self.passing())),
                    _writes(self.OUTPUT, self.passing())]
                self.assertEqual(self.run_substep().status, "pass")
                first = self.rows()[0]
                self.assertEqual((first["failure_category"], first["cut_outputs"]),
                                 (wc.WORKSHOP_CUT_READ, 1))

    def test_a_wait_output_string_is_scanned(self) -> None:
        rows = [{"type": "event_msg", "payload": {"type": "task_started"}},
                {"type": "response_item", "payload": {
                    "type": "function_call_output", "call_id": "w",
                    "output": "Script running with cell ID 3\n" + _CUT_HEADER}}]
        self.c.leaf_actions = [_reads(rows=rows, then=_writes(self.OUTPUT, self.passing())),
                               _writes(self.OUTPUT, self.passing())]
        self.assertEqual(self.run_substep().status, "pass")
        self.assertEqual(self.rows()[0]["failure_category"], wc.WORKSHOP_CUT_READ)

    def test_the_row_quoted_back_is_not_a_cut(self) -> None:
        row = pure_leaf.WORKSHOP_READER_TOOL_SURFACE_TEXT["codex_cli"]
        self.c.leaf_actions = [_reads(rows=_rollout_rows(["Output:\n", row]),
                                      then=_writes(self.OUTPUT, self.passing()))]
        self.assertEqual(self.run_substep().status, "pass")
        self.assertEqual(self.rows()[0]["cut_outputs"], 0)

    def test_a_repair_turn_that_writes_nothing_is_not_accepted(self) -> None:
        self.c.leaf_actions = [_reads(commands=_over(), then=_writes(self.OUTPUT, self.passing())),
                               _writes_nothing, _writes(self.OUTPUT, self.passing())]
        self.assertEqual(self.run_substep().status, "pass")
        first, second, third = self.rows()
        self.assertEqual(first["failure_category"], wc.WORKSHOP_CUT_READ)
        self.assertEqual(second["failure_category"], self.c._pure_reviewer_spec(
            self.PHASE, self.SUBSTEP).schema_category)
        self.assertNotIn("failure_category", third)

    def test_a_cut_read_beside_a_schema_violation_is_the_cut_read(self) -> None:
        """A malformed document from a launch that read past the budget is refused as the cut
        read, so the next turn cannot fix the shape alone and be accepted on this one's reads."""
        self.c.leaf_actions = [_reads(commands=_over(), then=_writes(self.OUTPUT, {"x": 1})),
                               _writes(self.OUTPUT, self.passing())]
        self.assertEqual(self.run_substep().status, "pass")
        first = self.rows()[0]
        self.assertEqual(first["failure_category"], wc.WORKSHOP_CUT_READ)
        self.assertIn("also failed the host's checks", first["failure_excerpt"])
        self.assertEqual(self.requests()[1]["repair_reason"], wc.WORKSHOP_CUT_READ_REPAIR_REASON)

    def test_cut_reads_on_every_turn_exhaust_their_own_counter(self) -> None:
        self.c.leaf_actions = [_reads(commands=_over(), then=_writes(self.OUTPUT, self.passing()))]
        outcome = self.run_substep()
        self.assertEqual((outcome.status, outcome.leaf_returncode, outcome.infra_error),
                         ("fail", 0, None))
        meta = self.attempt_meta()
        self.assertEqual((meta["result"], meta["failure_category"]),
                         ("fail", wc.WORKSHOP_CUT_READ))
        self.assertEqual(len(meta["per_attempt"]), 1 + pure_leaf.MAX_BUNDLE_REPAIR_TURNS)
        self.assert_cut_read_exhaustion_route(outcome)

    def assert_cut_read_exhaustion_route(self, outcome) -> None:
        """The two verify pairs restart their producer through the verdict tables, driven
        through `classify_failure`; the judge pair overrides this (no review is written, the
        signal `run_phase` routes to its conformance terminus)."""
        prior = [wc.SubstepOutcome("x", "pass", [])] * 2
        decision = self.c.classify_failure(self.refs, self.PHASE, [*prior, outcome])
        prefix = (wc.COMPILE_VERDICT_REASON_PREFIX if self.PHASE == "compile"
                  else wc.GENERATE_VERDICT_REASON_PREFIX)
        self.assertEqual((decision.action, decision.target_phase, decision.repair_strategy,
                          decision.reason),
                         ("retry", self.PHASE, "restart", prefix + wc.WORKSHOP_CUT_READ))

    def test_a_byte_rule_hit_wins_over_an_unreadable_rollout(self) -> None:
        """The refusal is then the leaf's to repair, not the host's to fail closed."""
        self.c.leaf_actions = [_reads(rows=None, commands=_over(),
                                      then=_writes(self.OUTPUT, self.passing())),
                               _writes(self.OUTPUT, self.passing())]
        outcome = self.run_substep()
        self.assertEqual((outcome.status, outcome.leaf_returncode), ("pass", 0))
        first, _ = self.rows()
        self.assertEqual((first["failure_category"], first["cut_outputs"]),
                         (wc.WORKSHOP_CUT_READ, 1))

    def test_a_schema_repair_after_a_cut_read_repair_is_a_schema_repair(self) -> None:
        """The repair reason is per refusal: the cut-read reason does not stick to the next
        refusal's turn."""
        self.c.leaf_actions = [_reads(commands=_over(), then=_writes(self.OUTPUT, self.passing())),
                               _writes(self.OUTPUT, {"x": 1}),
                               _writes(self.OUTPUT, self.passing())]
        self.assertEqual(self.run_substep().status, "pass")
        reasons = [r.get("repair_reason") for r in self.requests()]
        spec = self.c._pure_reviewer_spec(self.PHASE, self.SUBSTEP)
        self.assertEqual(reasons, ["none", wc.WORKSHOP_CUT_READ_REPAIR_REASON, spec.repair_reason])
        self.assertIn(ort.PURE_REPAIR_SCOPE_PARAGRAPH,
                      ort._render_pure_repair_prompt(self.requests()[2]))

    def test_the_two_repair_counters_are_independent(self) -> None:
        self.c.leaf_actions = [_writes(self.OUTPUT, {"x": 1}),
                               _reads(commands=_over(), then=_writes(self.OUTPUT, self.passing())),
                               _writes(self.OUTPUT, {"x": 1}),
                               _reads(commands=_over(), then=_writes(self.OUTPUT, self.passing())),
                               _writes(self.OUTPUT, self.passing())]
        self.assertEqual(self.run_substep().status, "pass")
        self.assertEqual(len(self.rows()), 5)

    def test_a_valid_fail_after_a_cut_read_is_accepted_with_its_count(self) -> None:
        self.c.leaf_actions = [_reads(commands=_over(), then=_writes(self.OUTPUT, self.failing()))]
        outcome = self.run_substep()
        self.assertEqual((outcome.status, outcome.leaf_returncode), ("fail", 0))
        (row,) = self.rows()
        self.assertEqual(row["cut_outputs"], 1)
        self.assertNotIn("failure_category", row)

    def test_an_unreadable_rollout_fails_closed_and_is_never_retried(self) -> None:
        for name, rows, head in (("no rollout", None, "0 rollout files"),
                                 ("no task_started", [_rollout_rows(_CLEAN_TEXTS)[1]],
                                  "no task_started row"),
                                 ("no tool output", [_rollout_rows(_CLEAN_TEXTS)[0]],
                                  "no tool output in the rollout")):
            with self.subTest(name):
                self.c.calls = []
                self.c.spawns = []
                self.c.leaf_actions = [_reads(rows=rows, then=_writes(self.OUTPUT, self.passing()))]
                outcome = self.run_substep()
                self.assertEqual((outcome.status, outcome.leaf_returncode,
                                  outcome.infra_error[0]),
                                 ("fail", 1, wc.WORKSHOP_ROLLOUT_UNREADABLE))
                self.assertIn("MR5-e", outcome.infra_error[1])
                # The kind FIRST: the transport branch keeps only the head of the detail.
                self.assertTrue(outcome.infra_error[1].startswith(head), outcome.infra_error)
                self.assertEqual(len(self.c.spawns), 1)
                (row,) = self.rows()
                self.assertEqual(row["failure_category"], wc.WORKSHOP_ROLLOUT_UNREADABLE)
                self.assertIsNone(row["cut_outputs"])

    def test_a_lineage_home_a_codex_shell_can_write_is_refused_before_a_launch(self) -> None:
        """Codex keeps /tmp writable to the leaf's shell, so a home there would let the leaf
        edit the rollout the refusal reads: the lineage is refused before any launch."""
        self.c.homes = Path(tempfile.mkdtemp(dir="/tmp")) / "homes"
        self.addCleanup(shutil.rmtree, self.c.homes.parent, True)
        self.assertIsNotNone(wc._codex_shell_writable_root(self.c.homes))
        outcome = self.run_substep()
        self.assertEqual((outcome.status, outcome.leaf_returncode, outcome.infra_error[0]),
                         ("fail", 1, "workshop_lineage_setup_failed"))
        self.assertIn("a codex leaf's shell can write", outcome.infra_error[1])
        self.assertEqual(getattr(self.c, "spawns", []), [])
        # The backstop, where the verdict is read.
        paths = workshop_tree.LineagePaths(
            homes_root=self.c.homes, orchestration_id=_ORCH, lineage_id="x",
            orchestration_dir=self.repo, backend="codex")
        turn = mock.Mock(command_outputs=[])
        lines, reason = self.c._codex_cut_reads(turn, "x", None, paths)
        self.assertEqual(lines, [])
        self.assertIn("which a codex shell can write", reason)
        self.assertIsNone(wc._codex_shell_writable_root(Path("/var/tmp/x")))
        # Each root on its own: /tmp whatever the temp dir is, and the temp dir wherever it is.
        elsewhere = Path(tempfile.mkdtemp(dir="/var/tmp")).resolve()
        self.addCleanup(shutil.rmtree, elsewhere, True)
        with mock.patch.object(wc.tempfile, "gettempdir", lambda: str(elsewhere)):
            self.assertEqual(wc._codex_shell_writable_root(Path("/tmp/a/b")), Path("/tmp"))
            self.assertEqual(wc._codex_shell_writable_root(elsewhere / "h"), elsewhere)
            self.assertIsNone(wc._codex_shell_writable_root(Path("/var/tmp/x")))

    def test_an_absent_capture_fails_closed(self) -> None:
        self.c.leaf_actions = [_reads(commands=None, then=_writes(self.OUTPUT, self.passing()))]
        outcome = self.run_substep()
        self.assertEqual((outcome.leaf_returncode, outcome.infra_error[0]),
                         (1, wc.WORKSHOP_ROLLOUT_UNREADABLE))

    def test_a_transport_death_is_not_scanned(self) -> None:
        self.c.leaf_actions = [_reads(rows=None, commands=None, then=_dies),
                               _writes(self.OUTPUT, self.passing())]
        with mock.patch.object(self.c, "_pure_transient_retry", return_value=True):
            self.assertEqual(self.run_substep().status, "pass")
        first, second = self.rows()
        self.assertEqual((first["failure_category"], first["cut_outputs"]),
                         ("pure_transport", None))
        self.assertEqual(second["cut_outputs"], 0)

    def test_a_warm_schema_repair_is_scanned_on_its_own_rows(self) -> None:
        """The one warm turn a reading lineage takes is a schema repair after a clean launch,
        and `exec resume` APPENDS to that launch's rollout: the warm turn's own cut is refused
        although the file's first launch is clean (a scan of the FIRST `task_started`'s rows
        would accept it)."""
        self.c.leaf_actions = [
            _writes(self.OUTPUT, {"x": 1}),
            _reads(rows=_rollout_rows(["Output:\n", _CUT_HEADER]),
                   then=_writes(self.OUTPUT, self.passing())),
            _writes(self.OUTPUT, self.passing())]
        self.assertEqual(self.run_substep().status, "pass")
        first, second, third = self.rows()
        self.assertEqual((first["cut_outputs"], second["warm"]), (0, True))
        self.assertEqual((second["failure_category"], second["cut_outputs"]),
                         (wc.WORKSHOP_CUT_READ, 1))
        (rollout,) = (self.paths("child-1").config / "sessions").glob("*/*/*/rollout-*.jsonl")
        self.assertEqual(rollout.read_text().count('"task_started"'), 2)
        self.assertEqual(third["cut_outputs"], 0)

    def test_an_unresumable_lineage_changes_nothing_for_a_cut_read_repair(self) -> None:
        self.c.resumable = False
        seen: list[bool] = []

        def second(c, tree, kwargs):
            seen.append((tree / self.OUTPUT).exists())
            (tree / self.OUTPUT).write_text(json.dumps(self.passing()), encoding="utf-8")

        self.c.leaf_actions = [_reads(commands=_over(), then=_writes(self.OUTPUT, self.passing())),
                               second]
        self.assertEqual(self.run_substep().status, "pass")
        self.assertEqual(seen, [False])
        _, request = self.requests()
        self.assertFalse(request.get("warm_resume"))
        self.assertEqual(request["repair_reason"], wc.WORKSHOP_CUT_READ_REPAIR_REASON)
        prompt = ort._render_pure_repair_prompt(request)
        self.assertIn(ort.WORKSHOP_CUT_READ_SCOPE_PARAGRAPH, prompt)
        self.assertIn(pure_leaf.WORKSHOP_READER_TOOL_SURFACE_TEXT["codex_cli"], prompt)


class CodexCompileVerifyTests(_CodexCutReadRows, CompileVerifyTests):
    """Every `compile.verify` row, and the cut-read rows, on a `codex_cli` reading entry."""


class CodexGenerateVerifyTests(_CodexCutReadRows, GenerateVerifyTests):
    """Every `generate.verify` row, and the cut-read rows, on a `codex_cli` reading entry."""


class CodexValidateJudgeTests(_CodexCutReadRows, ValidateJudgeTests):
    """Every `validate.judge` row above, and the cut-read rows (issue #547; its exhaustion
    route overridden: the judge's is the conformance terminus), on a `codex_cli` reading
    workshop entry (step 3 PR-J2): the lineage lives under the codex container, a warm turn resumes the THREAD the
    launch emitted rather than the agent_run_id, and an attempt row records no turn count and
    the completed-item count — the places a claude-only assumption of the reviewer branch
    would show."""

    def assert_cut_read_exhaustion_route(self, outcome) -> None:
        """No `semantic_review.json` is written, so `run_phase`'s judge conformance branch
        fires (`validate_judge_conformance_violation`), as on a schema exhaustion."""
        self.assertEqual((outcome.status, outcome.leaf_returncode), ("fail", 0))
        self.assertFalse((self.repo / self.refs.run_node_dir() / "semantic_review.json").exists())

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


class ClaudeReadingLineageIsNotScannedTests(_Base):
    def test_a_claude_attempt_row_carries_no_cut_outputs(self) -> None:
        self.assertEqual(self.run_substep().status, "pass")
        self.assertNotIn("cut_outputs", self.attempt_meta()["per_attempt"][0])


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

    def test_a_non_finite_token_survives_the_layout(self) -> None:
        """`docs/workflow/RUNNER_OUTPUT_CONTRACT.md` admits non-finite metric values, so a
        diagnostics document may carry `NaN` / `Infinity`: the seed keeps them as tokens."""
        text = wc._indented_json_seed('{"m": NaN, "p": Infinity, "n": -Infinity, "ok": 1}')
        self.assertIn('"m": NaN', text)
        self.assertIn('"p": Infinity', text)
        self.assertIn('"n": -Infinity', text)

    def test_a_real_keeps_its_value_through_the_layout(self) -> None:
        """Every double of the seed is the raw text's double, at full precision: the corpus's
        diagnostics carry 17 significant digits, and a judge compares them against bounds."""
        raw = ('{"a": 0.30000000000000004, "b": 6.776263578034403e-21, '
               '"c": 1.1899999999999999E-004, "d": 1.0210470693801136E-016, '
               '"e": -8.8201970000000001E-003, "f": 123456789012345678901234567890}')
        seed = json.loads(wc._indented_json_seed(raw))
        for key, value in json.loads(raw).items():
            with self.subTest(key=key):
                self.assertEqual(seed[key], value)
                self.assertEqual(type(seed[key]), type(value))
                if isinstance(value, float):
                    self.assertEqual(seed[key].hex(), value.hex())

    def test_non_ascii_text_is_kept_as_characters(self) -> None:
        self.assertIn('"note": "Δt μ"', wc._indented_json_seed('{"note": "\\u0394t \\u03bc"}'))

    def test_a_seed_that_is_not_json_is_seeded_as_it_is(self) -> None:
        for text in ("", "not json", "{\"a\": 1", "(absent)\n",
                     '{"checks": {"cfl": {"status": "fail"}, "cfl": {"status": "pass"}}}',
                     '{"a": "\\ud800"}', "[" * 100000 + "]" * 100000):
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

    def test_every_workshop_launch_prompt_ends_in_a_newline(self) -> None:
        """`CONTRACT.md` is the rendered launch prompt of every launch that is not warm — a
        cold reuse's repair prompt included — written as it is, and `_workshop_seeds` does not
        normalize it (issue #548): this pins the premise that every workshop template,
        producing and reading, the three repair templates included, on every provider, renders
        a text ending in `\n`."""
        pairs = (("compile", "generate", ""), ("generate", "generate", ""),
                 ("generate", "generate", "harness"), *self.PAIRS)
        repair = {"repair_strategy": "reuse", "repair_findings": "bad shape",
                  "repair_target_agent_run_id": "ar_w", "prior_document": '{"x": 1}'}
        templates = set()
        for provider in pure_leaf.WORKSHOP_READER_TOOL_SURFACE_TEXT:
            for step, substep, shape in pairs:
                for extra in ({}, repair):
                    with self.subTest(provider=provider, pair=(step, substep, shape),
                                      repair=bool(extra)):
                        req = ort.prepare_launch_request_payload(
                            self._request(step, substep, shape, workshop_provider=provider,
                                          **extra))
                        if extra:
                            templates.add(ort._pure_repair_template_name(req))
                        text = ort.render_launch_prompt_text(req)
                        self.assertTrue(text.startswith(pure_leaf.WORKSHOP_PROMPT_SENTINEL))
                        self.assertTrue(text.endswith("\n"))
        self.assertEqual(templates, {"workshop ir repair", "workshop bundle repair",
                                     "workshop verdict repair"})

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


    def test_a_cut_read_repair_renders_the_reading_paragraph_through_the_entry_point(
            self) -> None:
        """Issue #547: the cut-read repair turn, warm and cold, rendered by the production entry
        point from a request the launch validator accepts, states the READING paragraph and not
        the shape one; any other repair reason of a reading pair states the shape one."""
        for step, substep, shape in self.PAIRS:
            for warm in (True, False):
                with self.subTest(pair=(step, substep, shape), warm=warm):
                    repair = {"repair_strategy": "reuse", "warm_resume": warm,
                              "repair_target_agent_run_id": "ar_w",
                              "repair_findings": "`cat host_verdict.json` printed 600,000 bytes"}
                    req = ort.prepare_launch_request_payload(self._request(
                        step, substep, shape, workshop_provider="codex_cli",
                        repair_reason=ort.WORKSHOP_CUT_READ_REPAIR_REASON, **repair))
                    ort._validate_launch_request_payload(req)
                    text = ort.render_launch_prompt_text(req)
                    self.assertIn(ort.WORKSHOP_CUT_READ_SCOPE_PARAGRAPH, text)
                    self.assertNotIn(ort.PURE_REPAIR_SCOPE_PARAGRAPH, text)
                    self.assertIn("cat host_verdict.json", text)
                    for slot, value in ort.WORKSHOP_VERDICT_REPAIR_CUT_READ_SLOTS.items():
                        self.assertIn(value, text, slot)
                    self.assertNotIn("Correct EXACTLY", text)
                    other = ort.render_launch_prompt_text(ort.prepare_launch_request_payload(
                        self._request(step, substep, shape, workshop_provider="codex_cli",
                                      repair_reason="verify_verdict_schema_repair", **repair)))
                    self.assertIn(ort.PURE_REPAIR_SCOPE_PARAGRAPH, other)
                    self.assertNotIn(ort.WORKSHOP_CUT_READ_SCOPE_PARAGRAPH, other)
                    # A schema repair renders the template's wording before issue #547.
                    for slot, value in ort.WORKSHOP_VERDICT_REPAIR_SHAPE_SLOTS.items():
                        self.assertIn(value, other, slot)
                    self.assertNotIn("<repair_", other)
        self.assertEqual(ort.WORKSHOP_CUT_READ_REPAIR_REASON, wc.WORKSHOP_CUT_READ_REPAIR_REASON)

    def test_the_codex_reader_row_states_the_budget_numbers_of_the_constants(self) -> None:
        """The row's numbers are the byte rule's (skill rule 3): derived from the two measured
        constants, each said once, and the advice under the rule."""
        row = pure_leaf.WORKSHOP_READER_TOOL_SURFACE_TEXT["codex_cli"]
        budget = pure_leaf.CODEX_EXEC_OUTPUT_BUDGET_TOKENS
        rule = pure_leaf.CODEX_EXEC_OUTPUT_BUDGET_BYTES
        advice = pure_leaf.CODEX_READ_ADVICE_BYTES
        self.assertEqual(rule, budget * pure_leaf.CODEX_BYTES_PER_TOKEN)
        self.assertLess(advice, rule)
        for phrase in (f"({budget:,} tokens)", f"A token here is {pure_leaf.CODEX_BYTES_PER_TOKEN} "
                       "bytes", f"under {advice:,} bytes", f"exceeded {rule:,} bytes"):
            self.assertEqual(row.count(phrase), 1, phrase)
        for budget_name in ("`exec` result's own", "`exec_command`'s", "`write_stdin`'s",
                            "`wait`'s `max_tokens`"):
            self.assertIn(budget_name, row)


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
             "launch_body", "repair_instruction"})
        # The instruction slot is filled for each refusal kind (issue #547).
        for slots in (ort.WORKSHOP_VERDICT_REPAIR_SHAPE_SLOTS,
                      ort.WORKSHOP_VERDICT_REPAIR_CUT_READ_SLOTS):
            self.assertEqual(set(slots), {"repair_instruction"})



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
                         "TEXT-runner_output_contract_document\n")
        self.assertNotIn("CHECKS_MODULE_CONTRACT.md", seeds)
        self.assertIn("bundle_manifest.yaml", seeds)
        for entry in _valid_bundle()["files"]:
            self.assertEqual(seeds[entry["logical_path"]], _terminated(entry["content"]))


class SeedNewlineTests(unittest.TestCase):
    """Issue #548: every non-empty text seed of every workshop substep ends in a newline (an
    empty one stays empty), because a file tool or `wc -l` that counts newline characters
    misses an unterminated last line. The host
    sources a generate producer seeds from the node's `src/` (and the node's own interface
    header) are the exception, byte-exact: the wrapper's stages read them where the host gates
    read `src/`."""

    #: Every workshop spec `_workshop_seeds` seeds a lineage by: (phase, substep, shape).
    SPECS = (("compile", "generate", ""), ("generate", "generate", ""),
             ("generate", "generate", "harness"), ("compile", "verify", ""),
             ("generate", "verify", ""), ("generate", "verify", "harness"),
             ("validate", "judge", ""))
    #: The host sources the fixture places: one unterminated `src/` file and an unterminated
    #: interface header, beside the runner `_write_node` stages.
    DEP_HEADER = ("dep_model.h", "// dep, no final newline")
    NODE_HEADER = ("node_model.h", "// header, no final newline")

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        self.c = _PureFakeConductor(repo_root=self.repo, orchestration_id=_ORCH,
                                    orchestration_agent_run_id="orch",
                                    llm_config=_config(self.repo, "generate", "verify"),
                                    env={})

    def spec(self, phase: str, substep: str, shape: str):
        if substep == "generate":
            return self.c._pure_producer_spec(
                phase, (shape or "m3c") if phase == "generate" else None)
        return self.c._pure_reviewer_spec(phase, substep, shape or None)

    def bundle(self) -> dict:
        """`_valid_bundle()` with its first file unterminated: the existing bundle content is
        terminated, so the normalization of a bundle copy is otherwise untested."""
        bundle = _valid_bundle()
        bundle["files"][0]["content"] = bundle["files"][0]["content"].rstrip("\n")
        return bundle

    def context(self, phase: str, substep: str, shape: str) -> dict[str, str]:
        keys = (ort.PURE_CONTEXT_REQUIRED_KEYS_BY_SHAPE[(phase, substep, shape)] if shape
                else ort.PURE_CONTEXT_REQUIRED_KEYS[(phase, substep)])
        context = {k: f"TEXT-{k}" for k in keys}
        if "bundle_document" in context:
            context["bundle_document"] = json.dumps(self.bundle())
        return context

    def test_the_reading_helper_is_seeded_on_a_codex_reading_spec_only(self) -> None:
        """Issue #534 PR-J3-a: `read.py` is a seed exactly when the spec is the READING kind and
        the request names `codex_cli` — not on a producing spec, not on claude, and not on a
        request that names no provider."""
        refs = _verify_node(self.repo)
        for phase, substep, shape in self.SPECS:
            spec = self.spec(phase, substep, shape)
            context = self.context(phase, substep, shape)
            for provider in ("codex_cli", "claude_cli", None):
                with self.subTest(spec=(phase, substep, shape), provider=provider), \
                        mock.patch.object(wc.Conductor, "_interface_header_text",
                                          return_value=self.NODE_HEADER):
                    request = {} if provider is None else {"workshop_provider": provider}
                    seeds = self.c._workshop_seeds(refs, context, request, spec)
                    expect = provider == "codex_cli" and substep != "generate"
                    self.assertEqual(pure_leaf.CODEX_READ_HELPER_NAME in seeds, expect)
                    if expect:
                        self.assertEqual(seeds[pure_leaf.CODEX_READ_HELPER_NAME],
                                         pure_leaf.CODEX_READ_HELPER_TEXT)

    def test_every_seed_but_the_host_sources_ends_in_a_newline(self) -> None:
        refs = _verify_node(self.repo)
        src = self.repo / refs.source_dir() / "src"
        (src / self.DEP_HEADER[0]).write_text(self.DEP_HEADER[1], encoding="utf-8")
        # The runner `_write_node` staged, unterminated too: a host source is exempt by where
        # it came from, not by its name's suffix.
        (runner,) = [p for p in src.iterdir() if p.name != self.DEP_HEADER[0]]
        runner.write_text(runner.read_text(encoding="utf-8").rstrip("\n"), encoding="utf-8")
        host_sources = {p.name: p.read_text(encoding="utf-8") for p in src.iterdir()}
        self.assertFalse(any(text.endswith("\n") for text in host_sources.values()))
        host_sources[self.NODE_HEADER[0]] = self.NODE_HEADER[1]
        for phase, substep, shape in self.SPECS:
            spec = self.spec(phase, substep, shape)
            context = self.context(phase, substep, shape)
            # A `.json` seed holding a repeated key — on a reviewer spec it takes
            # `_indented_json_seed`'s raw fallback, on a producer spec it is a document seed with
            # no expander — and a seed already ending in a blank line, which is kept as it is.
            json_keys = [k for k in context
                         if spec.workshop_document_seeds.get(k, "").endswith(".json")
                         or (k in spec.workshop_seed_expanders and k != "bundle_document")]
            if json_keys:
                context[json_keys[0]] = '{"a":1,"a":2}'
            blank = next(k for k in context if k not in json_keys
                         and k not in ("bundle_document", "runner_document"))
            context[blank] = "ends in a blank line\n\n"
            # An empty document, which stays empty (`wc -l` gives 0, which is right).
            empty = next(k for k in context if k not in json_keys and k != blank
                         and k in spec.workshop_document_seeds)
            context[empty] = ""
            with self.subTest(spec=(phase, substep, shape)), \
                    mock.patch.object(wc.Conductor, "_interface_header_text",
                                      return_value=self.NODE_HEADER):
                seeds = self.c._workshop_seeds(refs, context, {}, spec)
                exempt = host_sources if spec.workshop_seeds_host_sources else {}
                for name, text in seeds.items():
                    if name in exempt:
                        self.assertEqual(text, exempt[name], name)
                    else:
                        self.assertTrue(text == "" or text.endswith("\n"), name)
                self.assertEqual(set(exempt) - set(seeds), set())
                self.assertIn("ends in a blank line\n\n", seeds.values())
                self.assertEqual(seeds[spec.workshop_document_seeds[empty]], "")
                if json_keys:
                    self.assertIn('{"a":1,"a":2}\n', seeds.values())
                if "bundle_document" in context:
                    first = self.bundle()["files"][0]
                    self.assertEqual(seeds[first["logical_path"]], first["content"] + "\n")
                if phase == "generate" and substep == "generate":
                    # A lineage that seeds host sources still normalizes its documents.
                    for name in ("harness_capabilities.json", "target_profile.json"):
                        self.assertTrue(seeds[name].endswith("\n"), name)

    def test_the_generate_producer_s_ir_seed_is_the_ir_file(self) -> None:
        """`spec.ir.yaml` is read by the wrapper's `run` while Validate reads
        `<ir_ref>/spec.ir.yaml`: the normalization is a no-op on it, asserted rather than
        relied on."""
        from tools.tests.test_pure_leaf_producer import _write_node
        refs = _write_node(self.repo)
        spec = self.c._pure_producer_spec("generate", "m3c")
        seeds = self.c._workshop_seeds(refs, self.c._build_pure_context(refs), {}, spec)
        self.assertEqual(seeds["spec.ir.yaml"],
                         (self.repo / refs.ir_ref / "spec.ir.yaml").read_text(encoding="utf-8"))

    def test_the_real_contracts_count_every_line(self) -> None:
        """Through the real slicers on the repository's documents: a seed's newline count is its
        line count, which is what `wc -l` and `awk 'END{print NR}'` disagreed on in run (2)."""
        verify = _verify_node(self.repo)
        judge_repo = self.repo / "judge"
        judge_repo.mkdir()
        judge_c = _PureFakeConductor(repo_root=judge_repo, orchestration_id=_ORCH,
                                     orchestration_agent_run_id="orch",
                                     llm_config=_config(judge_repo, "validate", "judge"),
                                     env={})
        judge = _write_run_node(judge_repo)
        rows = (
            (self.c, verify, self.c._build_pure_verify_context(verify),
             self.c._pure_reviewer_spec("generate", "verify"),
             ("CHECKS_MODULE_CONTRACT.md", "SEVERITY_RUBRIC.md")),
            (judge_c, judge, judge_c._build_pure_judge_context(judge),
             judge_c._pure_reviewer_spec("validate", "judge"),
             ("runner_output_contract.md",)))
        for conductor, refs, context, spec, names in rows:
            seeds = conductor._workshop_seeds(refs, context, {}, spec)
            for name in names:
                with self.subTest(seed=name):
                    text = seeds[name]
                    self.assertGreater(len(text.splitlines()), 1)
                    self.assertEqual(text.count("\n"), len(text.splitlines()))


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
