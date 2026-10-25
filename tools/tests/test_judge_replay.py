"""The judge replay tool (`tools/judge_replay.py`, issue #534 step 3 PR-J1-b).

MR3: a replay of a recorded `validate.judge` launch, run end to end through the tool's own
`main` over a certified node — the REAL orchestration creation (`init`, the configuration
snapshot, `preflight`, each runtime command run in-process), the REAL `record_launch` and
`finalize-child`, the REAL reviewer loop — with only the leaf process scripted. It pins that
the pure arm's rendered prompt equals the recorded one but for the two identity tokens, that
the workshop arm's tree holds the recorded (or mutated) context under the seed names, that a
mutation applies exactly once or nothing runs, that `--check` answers the dependency gate's
`derivation_key_mismatch`, and that a replay leaves `workspace/ir`, `pipelines` and `runs`
byte-identical, writes no step result and no reservation, and is invisible to the source's
A/B rollup, to `--resume`'s latest-orchestration choice and to the audit.

MR4: the read-coverage reader over a two-file claude lineage.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

import tools.judge_replay as jr
import tools.orchestration_runtime as ort
import tools.run_workflow as rw
import tools.workflow_conductor as wc
from tools import operator_private_root
from tools.pure_leaf import PURE_PROMPT_CONTRACT_VERSION
from tools.tests.orchestration_fixtures import (
    certify_node,
    claude_launch_checks,
    ensure_spec_entry,
    record_orchestration_target,
    spec_ref_of,
)

_DEP = "component/dep_a@0.1.0"
_USER = "component/user@0.1.0"
_SRC = "orch_src"
_SRC_ARID = "11111111-2222-4333-8444-555555555555"
_STEP, _SUBSTEP = "validate", "judge"

#: One line per document, numbered, so a seed's line count and a mutation's target are known.
_CONTEXT = {
    key: "".join(f"{key} line {n}\n" for n in range(1, 6))
    for key in ("diagnostics_document", "verdict_document", "perf_document",
                "trial_meta_document", "quality_check_document", "tests_document",
                "io_contract_document", "runner_output_contract_document",
                "binary_meta_document", "source_meta_document",
                "raw_evidence_excerpt_document")
}
_REVIEW = {"decision": "pass", "findings": [], "notes": "replayed"}
_FACTS = ({"node_key": _DEP, "pipeline_ref": "workspace/pipelines/x", "run_id": "run_x"},)

_PURE_YAML = "defaults:\n  provider: claude_cli\n  model: opus\n"
_WORKSHOP_YAML = (_PURE_YAML + "phases:\n  validate:\n    substeps:\n      judge:\n"
                  "        provider: claude_cli\n        model: opus\n"
                  "        leaf: workshop\n        lineage_timeout_s: 600\n")


def _deps_yaml(*component_ids: str) -> str:
    entries = "".join(f"    - component_id: {cid}\n      version_constraint: '>=0.1.0'\n"
                      for cid in component_ids)
    return (f"dependencies:\n  components:\n{entries}  profiles: []\n"
            "  infrastructure: []\n")


def _in_process_runtime(repo: Path, args: list[str], input: str | None = None) -> dict:
    """One `orchestration_runtime.py` subcommand, in this process and in `repo` (the callers
    pass repository-relative payload paths): the tool and the conductor call the CLI as a
    subprocess run from a checkout with `tools/`, which a temporary repository is not."""
    out = io.StringIO()
    cwd = os.getcwd()
    os.chdir(repo)
    try:
        with contextlib.redirect_stdout(out), mock.patch.object(sys, "stdin",
                                                                io.StringIO(input or "")):
            rc = ort.main(list(args))
    finally:
        os.chdir(cwd)
    if rc not in (0, None):
        raise RuntimeError(f"runtime {args[0]} failed: rc={rc} {out.getvalue()[:400]}")
    text = out.getvalue().strip()
    return json.loads(text) if text else {}


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for sub in ("ir", "pipelines", "runs"):
        base = root / "workspace" / sub
        for path in sorted(base.rglob("*")) if base.is_dir() else ():
            if path.is_file():
                digest.update(str(path.relative_to(root)).encode())
                digest.update(path.read_bytes())
    return digest.hexdigest()


class _Leaf:
    """The scripted leaf: a pure turn returns the review in its envelope; a workshop turn
    writes `review.json` into its tree and one transcript that reads `tests.md` whole."""

    def __init__(self, homes: Path) -> None:
        self.homes = homes
        self.calls: list[dict] = []
        #: Workshop launches that read `io_contract.yaml`, write NO document and leave their
        #: transcript under another session's name (so the lineage cannot be resumed and the
        #: repair is a cold reuse in a new lineage).
        self.unfinished = 0

    def __call__(self, conductor, prompt_text, child_env, entry=None, **kw):
        self.calls.append({"prompt": prompt_text, **kw})
        sid = kw.get("session_id") or kw["child_arid"]
        envelope = {"type": "result", "is_error": False, "result": json.dumps(_REVIEW),
                    "session_id": sid, "num_turns": 1, "duration_ms": 10,
                    "total_cost_usd": 0.25,
                    "usage": {"input_tokens": 1, "output_tokens": 2,
                              "cache_creation_input_tokens": 0, "cache_read_input_tokens": 3},
                    "modelUsage": {"claude-opus-5-5": {
                        "inputTokens": 1, "outputTokens": 2, "cacheReadInputTokens": 3,
                        "cacheCreationInputTokens": 0}}}
        if entry is not None and entry.is_workshop:
            home = self.homes / conductor.orchestration_id / "claude" / sid
            tree = home / "tree"
            unfinished = self.unfinished > 0
            self.unfinished -= 1
            if not unfinished:
                (tree / "review.json").write_text(json.dumps(_REVIEW), encoding="utf-8")
            project = home / "config" / "projects" / "p"
            project.mkdir(parents=True, exist_ok=True)
            tests = f"{tree}/{'io_contract.yaml' if unfinished else 'tests.md'}"
            lines = [
                {"version": "9.9.9", "message": {"content": [
                    {"type": "tool_use", "id": f"r1-{sid}", "name": "Read",
                     "input": {"file_path": tests}}]}},
                {"version": "9.9.9", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": f"r1-{sid}"}]},
                 "toolUseResult": {"file": {"filePath": tests, "startLine": 1,
                                            "numLines": 5, "totalLines": 5}}},
            ]
            (project / f"{'other' if unfinished else sid}.jsonl").write_text(
                "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
            envelope["result"] = "wrote review.json"
        return wc.ProcResult(0, json.dumps(envelope), "")


class JudgeReplayEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.repo = base / "repo"
        self.repo.mkdir()
        self.homes = base / "homes"
        credentials = base / "cred" / ".credentials.json"
        credentials.parent.mkdir()
        credentials.write_text('{"claudeAiOauth": {"accessToken": "x"}}', encoding="utf-8")
        os.chmod(credentials, 0o600)
        self.pure_cfg = base / "pure.yaml"
        self.pure_cfg.write_text(_PURE_YAML, encoding="utf-8")
        self.workshop_cfg = base / "workshop.yaml"
        self.workshop_cfg.write_text(_WORKSHOP_YAML, encoding="utf-8")
        self.leaf = _Leaf(self.homes)
        leaf = self.leaf
        preflight = {"status": "pass", "backend": "claude", "probe_command": "claude",
                     "sandbox_runtime": "bwrap", "sandbox_enforced": True,
                     "can_launch_step_agents": True, "can_launch_substep_agents": True,
                     "checks": claude_launch_checks()}
        for patcher in (
            mock.patch.dict(os.environ, {
                "ATMOFAB_WORKFLOW_HOMES_ROOT": str(self.homes),
                "ATMOFAB_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT": "0",
                "ATMOFAB_ORCHESTRATION_ASSUME_BWRAP": "1"}),
            mock.patch.object(operator_private_root, "claude_credentials_file",
                              lambda: credentials),
            mock.patch.object(ort, "probe_execution_platform",
                              lambda *a, **k: json.loads(json.dumps(preflight))),
            # A workshop surface's preflight row runs the CLI's own `--help`; the scripted leaf
            # has no CLI behind it (and CI has no `claude` on PATH).
            mock.patch.object(ort, "_workshop_flag_set_check",
                              lambda *a, **k: {"name": "claude_workshop_flag_set_available",
                                               "pass": True, "detail": "stubbed"}),
            mock.patch.object(rw, "_runtime_command",
                              lambda root, env, args: rw.RuntimeResult(
                                  _in_process_runtime(root, args), "")),
            mock.patch.object(wc.Conductor, "runtime",
                              lambda self, args, *, input=None: _in_process_runtime(
                                  self.repo_root, args, input)),
            mock.patch.object(wc.Conductor, "new_agent_run_id",
                              lambda self: str(uuid.uuid4())),
            mock.patch.object(wc.Conductor, "spawn_leaf",
                              lambda self, *a, **k: leaf(self, *a, **k)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self._seed()
        self._record_source()

    # --- the fixture: a certified node and one recorded judge launch ----------------------

    def _seed(self) -> None:
        certify_node(self.repo, "orch_dep", _DEP, ir_id="dep-a_20260101_001",
                     pipeline_id="dep-a_20260101_001")
        ensure_spec_entry(self.repo, _USER, deps_yaml=_deps_yaml("dep_a"))
        self.refs_doc = certify_node(self.repo, "orch_user", _USER,
                                     ir_id="user_20260101_001",
                                     pipeline_id="user_20260101_001")

    def _conductor(self, oid: str, cfg: Path) -> wc.Conductor:
        from tools.llm_config import load_llm_config
        from tools.tests.target_fixtures import fixture_target
        return wc.Conductor(repo_root=self.repo, orchestration_id=oid,
                            orchestration_agent_run_id="orch-arid", env={},
                            llm_config=load_llm_config(cfg),
                            target_profile=fixture_target(self.repo, None))

    def _record_source(self) -> None:
        spec_ref = spec_ref_of(_USER)
        ort.init_orchestration(repo_root=self.repo, orchestration_id=_SRC, spec_ref=spec_ref,
                               source_dependency_ref=f"{spec_ref}/deps.yaml")
        record_orchestration_target(self.repo, _SRC)
        ort.write_preflight(repo_root=self.repo, orchestration_id=_SRC, payload={
            "status": "pass", "sandbox_runtime": "bwrap", "sandbox_enforced": True,
            "can_launch_step_agents": True, "can_launch_substep_agents": True,
            "backend": "claude", "checks": claude_launch_checks()})
        conductor = self._conductor(_SRC, self.pure_cfg)
        self.node_refs = wc.NodeRefs(
            node_key=_USER, spec_path=spec_ref, ir_id="user_20260101_001",
            pipeline_id="user_20260101_001", target_id=conductor.target.target_id,
            source_id="src_20260101_001", binary_id="bin_20260101_001",
            run_id="run_20260101_001")
        # What every recorded judge launch found: an executed run node (the downstream gate).
        run_node = self.repo / self.node_refs.run_node_dir()
        (run_node / "raw").mkdir(parents=True, exist_ok=True)
        for name in ("diagnostics.json", "perf.json", "command_log.jsonl"):
            if not (run_node / name).exists():
                (run_node / name).write_text("{}\n", encoding="utf-8")
        request = wc.build_launch_request(
            self.node_refs, step=_STEP, substep=_SUBSTEP, orchestration_id=_SRC,
            orchestration_agent_run_id="orch-arid", child_agent_run_id=_SRC_ARID,
            agent_model="opus", workflow_mode="dev", runner_host_authored=True,
            resolved_dependencies=_FACTS, pure_leaf=True, pure_context=dict(_CONTEXT),
            pure_language=conductor._pure_language(_STEP),
            pure_parallel=conductor._pure_parallel(_STEP))
        conductor.record_launch(_SRC_ARID, request, conductor.entry_for(_STEP, _SUBSTEP))
        self.source_prompt = (self.repo / "workspace/orchestrations" / _SRC / "launches"
                              / f"{_SRC_ARID}.prompt.txt").read_text(encoding="utf-8")

    def _record_another(self, arid: str, overrides: dict) -> str:
        """Record a second judge launch of the source orchestration, with some context
        documents replaced; returns the recorded prompt."""
        conductor = self._conductor(_SRC, self.pure_cfg)
        request = wc.build_launch_request(
            self.node_refs, step=_STEP, substep=_SUBSTEP, orchestration_id=_SRC,
            orchestration_agent_run_id="orch-arid", child_agent_run_id=arid,
            agent_model="opus", workflow_mode="dev", runner_host_authored=True,
            resolved_dependencies=_FACTS, pure_leaf=True,
            pure_context={**_CONTEXT, **overrides},
            pure_language=conductor._pure_language(_STEP),
            pure_parallel=conductor._pure_parallel(_STEP))
        # The first recorded launch was never finalized (nothing replays a finalize); close its
        # window the way a finished launch leaves it, so the sequential-child gate admits this.
        for marker in (self.repo / "workspace/orchestrations" / _SRC).rglob(
                "active_child_agent_run_id.txt"):
            marker.unlink()
        conductor.record_launch(arid, request, conductor.entry_for(_STEP, _SUBSTEP))
        return (self.repo / "workspace/orchestrations" / _SRC / "launches"
                / f"{arid}.prompt.txt").read_text(encoding="utf-8")

    # --- helpers ----------------------------------------------------------------------------

    def _main(self, *extra: str, cfg: Path | None = None, leaf: str = "pure") -> tuple[int, str]:
        out = io.StringIO()
        argv = [_SRC, _SRC_ARID, "--leaf", leaf,
                "--llm-config", str(cfg or (self.workshop_cfg if leaf == "workshop"
                                            else self.pure_cfg)),
                "--repo-root", str(self.repo), *extra]
        with contextlib.redirect_stdout(out):
            rc = jr.main(argv)
        return rc, out.getvalue()

    def _replays(self) -> list[Path]:
        return sorted((self.repo / "workspace" / "orchestrations").glob("replay_*"))

    def _rows(self, replay_dir: Path) -> list[dict]:
        return [json.loads(line) for line in
                (replay_dir / jr.REPLAY_RESULT_NAME).read_text(encoding="utf-8").splitlines()]

    # --- MR3 --------------------------------------------------------------------------------

    def test_the_pure_arm_renders_the_recorded_prompt_but_for_its_identity(self) -> None:
        rc, out = self._main("--repeat", "2", "--label", "agreement")
        self.assertEqual(rc, 0, out)
        [replay] = self._replays()
        rows = self._rows(replay)
        self.assertEqual([r["repetition"] for r in rows], [1, 2])
        for row in rows:
            self.assertEqual(row["status"], "pass")
            self.assertEqual(row["document"], _REVIEW)
            self.assertIs(row["prompt_matches_record"], True)
            self.assertEqual(row["source_prompt_contract_version"],
                             PURE_PROMPT_CONTRACT_VERSION)
            self.assertEqual(row["label"], "agreement")
            # The scripted envelope's figures, summed over the one attempt.
            self.assertEqual((row["duration_ms"], row["model_turns"], row["input_tokens"],
                              row["output_tokens"], row["cache_read_input_tokens"],
                              row["schema_repairs"], row["cost_usd"],
                              row["usage_unrecorded_attempts"]),
                             (10.0, 1.0, 1.0, 2.0, 3.0, 0, 0.25, 0))
            self.assertIsInstance(row["leaf_wall_s"], float)
        first = rows[0]["attempts"][0]["agent_run_id"]
        replayed = (replay / "launches" / f"{first}.prompt.txt").read_text(encoding="utf-8")
        self.assertNotEqual(replayed, self.source_prompt)       # the identity differs ...
        source = jr.load_source(self.repo, _SRC, _SRC_ARID)
        self.assertEqual(jr._as_source(replayed, replay.name, first, source),
                         self.source_prompt)                    # ... only
        # The recorded dependency facts reached the replayed request verbatim.
        request = json.loads((replay / "launches" / f"{first}.request.json")
                             .read_text(encoding="utf-8"))
        self.assertEqual(request["resolved_dependencies"], list(_FACTS))
        self.assertIs(request["runner_host_authored"], True)
        self.assertEqual(request["pure_context"], _CONTEXT)
        meta = json.loads((replay / "orchestration_meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["invocation"]["kind"], jr.REPLAY_KIND)
        self.assertEqual(meta["invocation"]["source_agent_run_id"], _SRC_ARID)
        self.assertEqual((meta["status"], meta.get("reason_code")),
                         ("cancel", "judge_replay_finished"))

    def test_an_exhausted_repair_budget_counts_the_repair_turns(self) -> None:
        from tools.pure_leaf import MAX_BUNDLE_REPAIR_TURNS
        bad = wc.ProcResult(0, json.dumps({"type": "result", "is_error": False,
                                           "result": json.dumps({"decision": "maybe"}),
                                           "num_turns": 1, "duration_ms": 1}), "")
        with mock.patch.object(wc.Conductor, "spawn_leaf", lambda self, *a, **k: bad):
            rc, out = self._main()
        self.assertEqual(rc, 0, out)
        [row] = self._rows(self._replays()[0])
        self.assertEqual((row["status"], row["document"]), ("fail", None))
        self.assertEqual(len(row["attempts"]), MAX_BUNDLE_REPAIR_TURNS + 1)
        # The scripted envelope carries no usage: every attempt's usage channel failed, and
        # no sum is made.
        self.assertTrue(all(a["usage"]["status"] == "unavailable" for a in row["attempts"]))
        self.assertEqual((row["usage_unrecorded_attempts"], row["cost_usd"]),
                         (MAX_BUNDLE_REPAIR_TURNS + 1, None))
        self.assertEqual(row["schema_repairs"], MAX_BUNDLE_REPAIR_TURNS)
        self.assertEqual(row["cut_read_repairs"], 0)

    def test_a_cut_read_repair_is_counted_apart_from_the_schema_repairs(self) -> None:
        """Issue #547: the refusals before the last attempt, by kind; a transport death and the
        last attempt's own refusal launched no repair."""
        attempts = [{"failure_category": wc.WORKSHOP_CUT_READ},
                    {"failure_category": "semantic_review_document_violation"},
                    {"failure_category": "pure_transport"},
                    {"failure_category": wc.WORKSHOP_CUT_READ},
                    {"failure_category": wc.WORKSHOP_CUT_READ}]
        self.assertEqual(jr._repair_counts(attempts),
                         {"schema_repairs": 1, "cut_read_repairs": 2})
        self.assertEqual(jr._repair_counts([]), {"schema_repairs": 0, "cut_read_repairs": 0})

    def test_usage_recorded_without_a_cost_is_not_unrecorded(self) -> None:
        """A codex turn reports tokens and no money: its attempt's usage is recorded."""
        tokens_only = wc.ProcResult(0, json.dumps({
            "type": "result", "is_error": False, "result": json.dumps(_REVIEW),
            "num_turns": 1, "duration_ms": 1,
            "usage": {"input_tokens": 5, "output_tokens": 6, "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0}}), "")
        with mock.patch.object(wc.Conductor, "spawn_leaf", lambda self, *a, **k: tokens_only):
            rc, out = self._main()
        self.assertEqual(rc, 0, out)
        [row] = self._rows(self._replays()[0])
        self.assertNotIn("cost_usd", row["attempts"][0]["usage"])
        self.assertEqual((row["usage_unrecorded_attempts"], row["cost_usd"],
                          row["output_tokens"]), (0, None, 6.0))

    def test_a_workshop_record_is_not_compared_with_a_pure_prompt(self) -> None:
        path = (self.repo / "workspace/orchestrations" / _SRC / "launches"
                / f"{_SRC_ARID}.request.json")
        request = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps({**request, "leaf_mode": "workshop"}), encoding="utf-8")
        rc, out = self._main()
        self.assertEqual(rc, 0, out)
        [row] = self._rows(self._replays()[0])
        self.assertEqual(row["source_leaf_mode"], "workshop")
        self.assertNotIn("prompt_matches_record", row)

    def test_a_recorded_document_naming_the_source_orchestration_still_matches(self) -> None:
        """A remote site's `remote_dir` puts the SOURCE orchestration id inside a recorded
        document (149 judge records: 146 cpp_gpu, 3 fortran_cpu); masking every occurrence of
        it on the source
        side only would make an unchanged template read as moved."""
        arid = "22222222-2222-4333-8444-555555555555"
        recorded = self._record_another(arid, {"trial_meta_document":
                                               f"remote_dir: /site/jobs/{_SRC}/run\n"})
        self.assertIn(f"/site/jobs/{_SRC}/run", recorded)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = jr.main([_SRC, arid, "--leaf", "pure", "--llm-config", str(self.pure_cfg),
                          "--repo-root", str(self.repo)])
        self.assertEqual(rc, 0, out.getvalue())
        [row] = self._rows(self._replays()[0])
        self.assertIs(row["prompt_matches_record"], True)

    def test_a_mutated_pure_replay_makes_no_prompt_comparison(self) -> None:
        rc, out = self._main("--mutate", json.dumps({"tests_document": [
            {"old": "tests_document line 2\n", "new": "DEFECT\n"}]}))
        self.assertEqual(rc, 0, out)
        [row] = self._rows(self._replays()[0])
        self.assertIsNone(row["prompt_matches_record"])

    def test_a_template_change_is_reported_as_a_prompt_mismatch(self) -> None:
        real = ort._load_launch_prompt_templates()
        moved = {**real, "pure validate.judge": real["pure validate.judge"] + "\nMOVED\n"}
        with mock.patch.object(ort, "_load_launch_prompt_templates", lambda: moved):
            rc, out = self._main()
        self.assertEqual(rc, 0, out)
        [row] = self._rows(self._replays()[0])
        self.assertIs(row["prompt_matches_record"], False)

    def test_the_workshop_arm_seeds_the_mutated_context_and_measures_its_reads(self) -> None:
        old = "tests_document line 3\n"
        mutation = json.dumps({"tests_document": [{"old": old, "new": "SEEDED DEFECT\n"}]})
        rc, out = self._main("--mutate", mutation, leaf="workshop")
        self.assertEqual(rc, 0, out)
        [replay] = self._replays()
        [row] = self._rows(replay)
        self.assertEqual((row["status"], row["document"]), ("pass", _REVIEW))
        self.assertEqual(row["mutated_keys"], ["tests_document"])
        [attempt] = row["attempts"]
        tree = self.homes / replay.name / "claude" / attempt["lineage_id"] / "tree"
        spec = self._conductor(replay.name, self.workshop_cfg)._pure_judge_spec()
        expected = dict(_CONTEXT, tests_document=_CONTEXT["tests_document"].replace(
            old, "SEEDED DEFECT\n"))
        for key, name in spec.workshop_document_seeds.items():
            self.assertEqual((tree / name).read_text(encoding="utf-8"), expected[key], key)
        self.assertEqual(row["cli_versions"], ["9.9.9"])
        self.assertEqual(row["coverage"]["tests.md"],
                         {"lines": 5, "covered": 5, "fraction": 1.0, "ranges": [[1, 5]]})
        self.assertEqual(row["coverage"]["host_verdict.json"]["covered"], 0)
        self.assertNotIn("prompt_matches_record", row)

    def test_coverage_is_the_union_over_the_repetitions_lineages(self) -> None:
        self.leaf.unfinished = 1
        rc, out = self._main(leaf="workshop")
        self.assertEqual(rc, 0, out)
        [row] = self._rows(self._replays()[0])
        lineages = [a["lineage_id"] for a in row["attempts"]]
        self.assertEqual(len(set(lineages)), 2, row["attempts"])
        self.assertEqual(row["schema_repairs"], 1)
        # Both attempts recorded their usage, and the sums are over both.
        self.assertEqual((row["usage_unrecorded_attempts"], row["cost_usd"],
                          row["output_tokens"]), (0, 0.5, 4.0))
        self.assertGreater(row["leaf_wall_s"], 0)
        self.assertEqual(row["coverage"]["io_contract.yaml"]["covered"], 5)
        self.assertEqual(row["coverage"]["tests.md"]["covered"], 5)

    def _status(self, replay: Path) -> tuple[str, str | None]:
        meta = json.loads((replay / "orchestration_meta.json").read_text(encoding="utf-8"))
        return meta["status"], meta.get("reason_code")

    def test_a_replay_that_raises_ends_fail_and_leaves_no_scratch(self) -> None:
        def die(self, *a, **k):
            raise RuntimeError("leaf host broke")
        with mock.patch.object(wc.Conductor, "spawn_leaf", die), \
                self.assertRaises(RuntimeError):
            self._main()
        [replay] = self._replays()
        self.assertEqual(self._status(replay), ("fail", "judge_replay_error"))
        meta = json.loads((replay / "orchestration_meta.json").read_text(encoding="utf-8"))
        self.assertFalse((self.repo / "workspace" / "tmp"
                          / meta["orchestration_agent_run_id"]).exists())

    def test_a_failing_preflight_refuses_and_ends_fail(self) -> None:
        with mock.patch.object(ort, "probe_execution_platform",
                               lambda *a, **k: {"status": "fail", "backend": "claude",
                                                "checks": [], "can_launch_step_agents": False,
                                                "can_launch_substep_agents": False}):
            rc, out = self._main()
        self.assertEqual((rc, json.loads(out.strip().splitlines()[-1])["reason"]),
                         (2, "judge_replay_preflight_failed"))
        [replay] = self._replays()
        self.assertEqual(self._status(replay), ("fail", "preflight_failed"))
        self.assertEqual(self.leaf.calls, [])

    def test_a_replay_writes_nothing_a_run_or_a_rollup_reads(self) -> None:
        from tools.audit_orchestration import collect_pure_leaf_ab_summary
        src_meta = json.loads((self.repo / "workspace/orchestrations" / _SRC
                               / "orchestration_meta.json").read_text(encoding="utf-8"))
        before = (_tree_digest(self.repo),
                  collect_pure_leaf_ab_summary(self.repo, _SRC, src_meta))
        self.assertEqual(self._main()[0], 0)
        self.assertEqual(self._main(leaf="workshop")[0], 0)
        after = (_tree_digest(self.repo),
                 collect_pure_leaf_ab_summary(self.repo, _SRC, src_meta))
        self.assertEqual(before, after)
        for replay in self._replays():
            self.assertEqual(list(replay.rglob("step_result.json")), [])
            self.assertFalse((replay / "reservations").exists()
                             and any((replay / "reservations").rglob("*.json")))
        # `--resume` without an id never picks a replay, and the audit refuses one.
        self.assertFalse(str(rw._find_latest_orchestration(self.repo)).startswith("replay_"))
        with mock.patch.object(sys, "argv", ["audit", "--repo-root", str(self.repo),
                                             "--orchestration-id", self._replays()[0].name]), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ex:
            from tools.audit_orchestration import main as audit_main
            audit_main()
        self.assertEqual(ex.exception.code, 2)

    def test_check_answers_the_gates_without_launching(self) -> None:
        rc, out = self._main("--check")
        self.assertEqual(rc, 0, out)
        self.assertTrue(json.loads(out.strip().splitlines()[-1])["check"]["ready"])
        # A dependency's key moves: the tests of `dep_a` are edited within its version.
        (self.repo / spec_ref_of(_DEP) / "tests.md").write_text("changed\n", encoding="utf-8")
        rc, out = self._main("--check")
        self.assertEqual(rc, 1, out)
        check = json.loads(out.strip().splitlines()[-1])["check"]
        self.assertFalse(check["dependency_ready"])
        self.assertIn("derivation_key_mismatch:spec.tests", check["dependency_reason"])
        self.assertEqual(self.leaf.calls, [])

    def test_a_row_that_is_not_ready_is_refused_before_any_launch(self) -> None:
        (self.repo / spec_ref_of(_DEP) / "tests.md").write_text("changed\n", encoding="utf-8")
        rc, out = self._main()
        refusal = json.loads(out.strip().splitlines()[-1])
        self.assertEqual((rc, refusal["reason"]), (2, "judge_replay_not_ready"))
        self.assertIn("derivation_key_mismatch", refusal["detail"]["dependency_reason"])
        [replay] = self._replays()
        self.assertEqual(self._status(replay), ("fail", "judge_replay_not_ready"))
        self.assertEqual(self.leaf.calls, [])

    def test_finishing_an_orchestration_the_runtime_terminalized_keeps_its_status(self) -> None:
        rc, _ = self._main("--check")
        [replay] = self._replays()
        meta_path = replay / "orchestration_meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta_path.write_text(json.dumps({**meta, "status": "fail_closed"}), encoding="utf-8")
        handle = jr.ReplayOrchestration(replay.name, meta["orchestration_agent_run_id"], {},
                                        self.repo / "workspace" / "tmp" / "x")
        jr.finish_replay_orchestration(self.repo, handle, "fail", "judge_replay_error", "x")
        self.assertEqual(self._status(replay)[0], "fail_closed")

    def test_a_mutation_that_does_not_match_once_refuses_before_any_orchestration(self) -> None:
        for items in ([{"old": "absent text", "new": "x"}],
                      [{"old": "tests_document line", "new": "x"}]):
            with self.subTest(items=items):
                rc, out = self._main("--mutate", json.dumps({"tests_document": items}))
                self.assertEqual(rc, 2)
                self.assertEqual(json.loads(out)["reason"], "judge_replay_mutation_not_once")
        rc, out = self._main("--mutate", json.dumps({"no_such_document": [
            {"old": "a", "new": "b"}]}))
        self.assertEqual(json.loads(out)["reason"], "judge_replay_mutation_unknown_key")
        self.assertEqual(self._replays(), [])

    def test_check_answers_the_downstream_gate(self) -> None:
        (self.repo / self.node_refs.run_node_dir() / "diagnostics.json").unlink()
        rc, out = self._main("--check")
        check = json.loads(out.strip().splitlines()[-1])["check"]
        self.assertEqual((rc, check["dependency_ready"], check["downstream_ready"]),
                         (1, True, False))
        self.assertIn("diagnostics.json", check["downstream_reason"])

    def test_refusals_before_any_orchestration(self) -> None:
        # A prefix naming two launches.
        launches = self.repo / "workspace/orchestrations" / _SRC / "launches"
        (launches / "11111111-ffff.request.json").write_text("{}", encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = jr.main([_SRC, "11111111", "--leaf", "pure", "--llm-config",
                          str(self.pure_cfg), "--repo-root", str(self.repo)])
        self.assertEqual((rc, json.loads(out.getvalue())["reason"]),
                         (2, "judge_replay_source_ambiguous"))
        # A repeat count below one.
        rc, out = self._main("--repeat", "0")
        self.assertEqual((rc, json.loads(out)["reason"]), (2, "judge_replay_repeat_invalid"))
        # A source whose spec_ref now resolves to another node (its version moved).
        meta_path = self.repo / "workspace/orchestrations" / _SRC / "orchestration_meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta_path.write_text(json.dumps({**meta, "spec_ref": spec_ref_of(_DEP)}),
                             encoding="utf-8")
        rc, out = self._main()
        self.assertEqual((rc, json.loads(out)["reason"]), (2, "judge_replay_spec_unresolved"))
        self.assertEqual(self._replays(), [])

    def test_a_request_whose_refs_are_not_its_nodes_is_refused(self) -> None:
        path = (self.repo / "workspace/orchestrations" / _SRC / "launches"
                / f"{_SRC_ARID}.request.json")
        request = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps({**request, "ir_ref": "workspace/ir/other__x__1/ir_1"}),
                        encoding="utf-8")
        rc, out = self._main()
        self.assertEqual((rc, json.loads(out)["reason"]), (2, "judge_replay_refs_invalid"))
        self.assertEqual(self._replays(), [])

    def test_a_failure_after_init_ends_the_orchestration_fail(self) -> None:
        with mock.patch.object(rw, "_write_llm_config_snapshot",
                               side_effect=OSError("disk full")), \
                self.assertRaises(OSError):
            self._main()
        [replay] = self._replays()
        self.assertEqual(self._status(replay), ("fail", "judge_replay_error"))

    def test_the_leaf_must_be_the_configurations(self) -> None:
        rc, out = self._main(cfg=self.pure_cfg, leaf="workshop")
        self.assertEqual((rc, json.loads(out)["reason"]), (2, "judge_replay_leaf_mismatch"))
        self.assertEqual(self._replays(), [])


class SourceRefusalTests(unittest.TestCase):
    def _source(self, **request) -> jr.SourceLaunch:
        base = {"step": "validate", "substep": "judge", "pure_context": dict(_CONTEXT)}
        return jr.SourceLaunch("o", "a", {**base, **request}, {}, None)

    def test_only_a_judging_pair_with_its_current_context_is_replayed(self) -> None:
        jr.check_source(self._source())
        for request, code in (
                ({"step": "generate", "substep": "generate"}, "judge_replay_pair_not_admitted"),
                ({"step": "build", "substep": "diagnose"}, "judge_replay_pair_not_admitted"),
                ({"pure_context": None}, "judge_replay_no_context"),
                ({"pure_context": {**_CONTEXT, "tests_document": None}},
                 "judge_replay_no_context"),
                ({"pure_shape": "no_such_shape"}, "judge_replay_shape_unknown"),
                ({"pure_context": {**_CONTEXT, "extra_document": "x"}},
                 "judge_replay_context_keys_moved")):
            with self.subTest(code=code), self.assertRaises(jr.ReplayRefused) as ctx:
                jr.check_source(self._source(**request))
            self.assertEqual(ctx.exception.code, code)

    def test_mutations_apply_in_order_each_exactly_once(self) -> None:
        mutations = jr.parse_mutations(json.dumps({"tests_document": [
            {"old": "line 1\n", "new": "line one\n"},
            {"old": "line one\n", "new": "line ONE\n"}]}))
        out = jr.apply_mutations(_CONTEXT, mutations)
        self.assertTrue(out["tests_document"].startswith("tests_document line ONE\n"))
        self.assertEqual(out["verdict_document"], _CONTEXT["verdict_document"])
        # A recorded JSON document stays JSON: a raw line break inside one of its strings
        # would seed nothing on the workshop arm while the pure arm ran.
        bundle = {"bundle_document": json.dumps({"files": [{"content": "a\nb\n"}]})}
        self.assertIn("a\\nb", bundle["bundle_document"])
        ok = jr.apply_mutations(bundle, jr.parse_mutations(json.dumps(
            {"bundle_document": [{"old": "a\\nb", "new": "a\\nB"}]})))
        self.assertEqual(json.loads(ok["bundle_document"])["files"][0]["content"], "a\nB\n")
        with self.assertRaises(jr.ReplayRefused) as ctx:
            jr.apply_mutations(bundle, jr.parse_mutations(json.dumps(
                {"bundle_document": [{"old": "a\\nb", "new": "a\nB"}]})))
        self.assertEqual(ctx.exception.code, "judge_replay_mutation_breaks_document")
        # A document that was never JSON is not held to it.
        self.assertEqual(jr.apply_mutations({"k": "x: [1"}, {"k": [("[1", "{1")]}), {"k": "x: {1"})
        for text in ('{"tests_document": [{"old": "a", "new": "a"}]}', "[]", "{}",
                     '{"tests_document": [{"old": "", "new": "x"}]}',
                     '{"tests_document": [{"old": "a"}]}'):
            with self.subTest(text=text), self.assertRaises(jr.ReplayRefused):
                jr.parse_mutations(text)


class ReadCoverageTests(unittest.TestCase):
    """MR4: two transcript files of one lineage (a cold launch and a fork that repeats its
    history), with a partial `Read`, a token-capped `Read`, a block streamed twice, an
    oversize-error `Read`, a `Grep` content hit with context lines and a `Grep` count."""

    TREE = "/h/o/claude/lin/tree"

    def _use(self, tid: str, name: str, **inp) -> dict:
        return {"version": "2.1.310", "message": {"content": [
            {"type": "tool_use", "id": tid, "name": name, "input": inp}]}}

    def _result(self, tid: str, result, *, error: bool = False) -> dict:
        block = {"type": "tool_result", "tool_use_id": tid}
        if error:
            block["is_error"] = True
        return {"version": "2.1.310", "message": {"content": [block]},
                "toolUseResult": result}

    def _read(self, start: int, num: int, total: int = 100, **extra) -> dict:
        return {"type": "text", "file": {"filePath": f"{self.TREE}/spec.ir.yaml",
                                         "startLine": start, "numLines": num,
                                         "totalLines": total, **extra}}

    def _cold(self) -> list[dict]:
        ir = f"{self.TREE}/spec.ir.yaml"
        return [
            self._use("r1", "Read", file_path=ir),
            self._use("r1", "Read", file_path=ir),                       # streamed twice
            self._result("r1", self._read(1, 30, truncatedByTokenCap=True)),
            self._use("r2", "Read", file_path=ir, offset=10, limit=5),
            self._result("r2", self._read(10, 5)),
            self._use("r3", "Read", file_path=f"{self.TREE}/host_verdict.json"),
            self._result("r3", "File content (300KB) exceeds maximum allowed size",
                         error=True),
            self._use("g1", "Grep", path=ir, pattern="x", output_mode="content", C=1),
            self._result("g1", {"mode": "content", "numFiles": 0, "filenames": [],
                                "content": "39-ctx\n40:hit\n41-ctx", "numLines": 3}),
            self._use("g2", "Grep", path=self.TREE, pattern="y", output_mode="count"),
            self._result("g2", {"mode": "count", "numFiles": 2,
                                "filenames": [ir], "content": f"{ir}:7"}),
            self._use("g3", "Grep", path=self.TREE, pattern="z", output_mode="content"),
            self._result("g3", {"mode": "content", "numFiles": 2,
                                "filenames": [ir, f"{self.TREE}/tests.md"],
                                "content": f"{ir}:90:z\n{self.TREE}/tests.md:2:z"}),
            # CLI 2.1.310 then writes an `api-request-blob` line that repeats the conversation
            # (tool uses and their results) with no `toolUseResult` (86 of 148 real Read ids):
            # neither the repeat of a use nor the result-less repeat of a result is a new call.
            {"type": "api-request-blob", "message": {"content": [
                {"type": "tool_use", "id": "r1", "name": "Read", "input": {"file_path": ir}},
                {"type": "tool_result", "tool_use_id": "r1"},
                {"type": "tool_result", "tool_use_id": "r2"},
                {"type": "tool_result", "tool_use_id": "g1"}]}},
        ]

    def _write(self, base: Path, name: str, lines: list[dict]) -> Path:
        path = base / name
        path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
        return path

    def _seeds(self) -> dict[str, str]:
        return {"spec.ir.yaml": "x\n" * 100, "tests.md": "t\n" * 4,
                "host_verdict.json": "{}\n" * 50}

    def test_the_planted_reads_give_the_planted_fraction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cold = self._write(Path(tmp), "cold.jsonl", self._cold())
            # The fork repeats the cold history, then reads one more range.
            fork = self._write(Path(tmp), "fork.jsonl", [
                *self._cold(),
                # A hit past the end of the seed (a stale transcript line) counts for nothing,
                # and an errored result is no read whatever it carries.
                self._use("g4", "Grep", path=f"{self.TREE}/tests.md", pattern="w",
                          output_mode="content"),
                self._result("g4", {"mode": "content", "numFiles": 1, "filenames": [],
                                    "content": "900:w"}),
                self._use("r5", "Read", file_path=f"{self.TREE}/host_verdict.json"),
                self._result("r5", {"file": {"filePath": f"{self.TREE}/host_verdict.json",
                                             "startLine": 1, "numLines": 50,
                                             "totalLines": 50}}, error=True),
                self._use("r4", "Read", file_path=f"{self.TREE}/spec.ir.yaml",
                          offset=60, limit=10),
                self._result("r4", self._read(60, 10))])
            out = jr.read_coverage([cold, fork], [self.TREE], self._seeds())
        covered = set(range(1, 31)) | set(range(60, 70)) | {39, 40, 41, 90}
        self.assertEqual(out["cli_versions"], ["2.1.310"])
        self.assertEqual(out["coverage"]["spec.ir.yaml"],
                         {"lines": 100, "covered": len(covered),
                          "fraction": round(len(covered) / 100, 4),
                          "ranges": [[1, 30], [39, 41], [60, 69], [90, 90]]})
        self.assertEqual(out["coverage"]["tests.md"],
                         {"lines": 4, "covered": 1, "fraction": 0.25, "ranges": [[2, 2]]})
        # The oversize Read errored: no read, not an unmeasured one.
        self.assertEqual(out["coverage"]["host_verdict.json"]["covered"], 0)

    def test_a_relative_path_names_the_seed_in_the_tree(self) -> None:
        """CLI 2.1.309 records a relative `file_path` as given (16 of 141 real Read results,
        in two lineages of 2026-10-23); the tree is the leaf's working directory."""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(Path(tmp), "t.jsonl", [
                self._use("r1", "Read", file_path="tests.md"),
                self._result("r1", {"file": {"filePath": "tests.md", "startLine": 1,
                                             "numLines": 4, "totalLines": 4}}),
                self._use("r2", "Read", file_path="./spec.ir.yaml", offset=5, limit=2),
                self._result("r2", {"file": {"filePath": "./spec.ir.yaml", "startLine": 5,
                                             "numLines": 2, "totalLines": 100}}),
                self._use("g1", "Grep", path="host_verdict.json", pattern="x",
                          output_mode="content"),
                self._result("g1", {"mode": "content", "numFiles": 1, "filenames": [],
                                    "content": "7:x"}),
                self._use("g2", "Grep", path=f"{self.TREE}/", pattern="y",
                          output_mode="content"),
                self._result("g2", {"mode": "content", "numFiles": 1,
                                    "filenames": ["host_verdict.json"], "content": "9:y"})])
            out = jr.read_coverage([path], [self.TREE], self._seeds())
        self.assertEqual(out["coverage"]["tests.md"]["covered"], 4)
        self.assertEqual(out["coverage"]["spec.ir.yaml"]["covered"], 2)
        self.assertEqual(out["coverage"]["host_verdict.json"]["covered"], 2)

    def test_a_grep_covers_only_the_whole_lines_it_showed(self) -> None:
        ir = f"{self.TREE}/spec.ir.yaml"
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(Path(tmp), "t.jsonl", [
                # The CLI omits a line too long to show (measured, CLI 2.1.309).
                self._use("g1", "Grep", path=ir, pattern="x", output_mode="content"),
                self._result("g1", {"mode": "content", "filenames": [],
                                    "content": "1:[Omitted long matching line]\n2:shown"}),
                # `-o` shows the matched fragment only; `-n false` shows no line number.
                self._use("g2", "Grep", path=ir, pattern="x", output_mode="content",
                          **{"-o": True}),
                self._result("g2", {"mode": "content", "filenames": [], "content": "3:x"}),
                self._use("g3", "Grep", path=ir, pattern="x", output_mode="content",
                          **{"-n": False}),
                self._result("g3", {"mode": "content", "filenames": [],
                                    "content": "4: text that begins with digits"})])
            out = jr.read_coverage([path], [self.TREE], self._seeds())
        self.assertEqual(out["coverage"]["spec.ir.yaml"]["covered"], 1)

    def test_no_transcript_is_unmeasured_not_zero(self) -> None:
        out = jr.read_coverage([], [self.TREE], self._seeds())
        self.assertEqual(out["coverage"], "unmeasured")

    def test_a_result_less_repeat_read_first_does_not_hide_the_measured_result(self) -> None:
        """Transcript files are read in name order, not in time order: a fork file sorting
        before the cold one would put the CLI's result-less repeat first."""
        cold = self._cold()
        blob = cold[-1]
        self.assertEqual(blob["type"], "api-request-blob")
        with tempfile.TemporaryDirectory() as tmp:
            first = self._write(Path(tmp), "a.jsonl", [blob])
            second = self._write(Path(tmp), "b.jsonl", cold)
            out = jr.read_coverage([first, second], [self.TREE], self._seeds())
            ordered = jr.read_coverage([second], [self.TREE], self._seeds())
            only_blob = jr.read_coverage([first], [self.TREE], self._seeds())
        self.assertEqual(out["coverage"], ordered["coverage"])
        self.assertEqual(only_blob["coverage"], "unmeasured")

    def test_a_repeated_read_of_an_unchanged_file_covers_nothing_new(self) -> None:
        tests = f"{self.TREE}/tests.md"
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(Path(tmp), "t.jsonl", [
                self._use("r1", "Read", file_path=tests, limit=2),
                self._result("r1", {"file": {"filePath": tests, "startLine": 1,
                                             "numLines": 2, "totalLines": 4}}),
                self._use("r2", "Read", file_path=tests, limit=2),
                self._result("r2", {"type": "file_unchanged", "file": {"filePath": tests}})])
            out = jr.read_coverage([path], [self.TREE], self._seeds())
        self.assertEqual(out["coverage"]["tests.md"]["ranges"], [[1, 2]])

    def test_only_a_line_answering_one_call_with_its_result_settles_it(self) -> None:
        """Three shapes a line can take; only the first is attributable."""
        ir = f"{self.TREE}/spec.ir.yaml"
        measured = {"file": {"filePath": ir, "startLine": 1, "numLines": 10, "totalLines": 100}}
        cases = {
            # one result, with toolUseResult: settles r1 (10 lines), and is not overwritten by
            # a later attributable line for the same id (first wins)
            "first_wins": ([self._result("r1", measured),
                            self._result("r1", {**measured, "file": {
                                **measured["file"], "numLines": 90}})], 10),
            # one result, no toolUseResult: not attributable
            "no_payload": ([{"message": {"content": [
                {"type": "tool_result", "tool_use_id": "r1"}]}},
                self._result("r1", measured)], 10),
            # two results with a toolUseResult: the payload is the line's, not either call's
            "two_results": ([{"message": {"content": [
                {"type": "tool_result", "tool_use_id": "r1"},
                {"type": "tool_result", "tool_use_id": "r2"}]},
                "toolUseResult": {**measured, "file": {**measured["file"], "numLines": 90}}},
                self._result("r1", measured)], 10),
        }
        for name, (lines, expected) in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                path = self._write(Path(tmp), "t.jsonl", [
                    self._use("r1", "Read", file_path=ir),
                    self._use("r2", "Glob", pattern="*"), *lines])
                out = jr.read_coverage([path], [self.TREE], self._seeds())
                self.assertEqual(out["coverage"]["spec.ir.yaml"]["covered"], expected)

    def test_a_read_result_without_its_line_triple_is_unmeasured_not_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(Path(tmp), "t.jsonl", [
                *self._cold(),
                self._use("r9", "Read", file_path=f"{self.TREE}/tests.md"),
                self._result("r9", {"type": "text", "file": {
                    "filePath": f"{self.TREE}/tests.md", "content": "t\n"}})])
            out = jr.read_coverage([path], [self.TREE], self._seeds())
        self.assertEqual(out["coverage"], "unmeasured")
        self.assertEqual(out["unmeasured_reads"], 1)


if __name__ == "__main__":
    unittest.main()
