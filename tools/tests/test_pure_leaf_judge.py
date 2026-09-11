#!/usr/bin/env python3
"""Z3 (issue #169): the pure `validate.judge` reviewer.

Covers the host side of the pure judge channel: the context builder and its document-ref map,
the semantic-review host writer (including the `evidence_refs` resolution that turns the slot
name the leaf cites back into a workspace path), the per-attempt record, the post_judge
reclassification that follows from `semantic_review.json` becoming host-authored, and the
defensive freshness branch.

The loop ITSELF is shared with the two older reviewers — `_run_pure_reviewer_substep`,
parametrised by a `_PureReviewerSpec` — so the transport-shaped behaviour (usage waits,
transient retries, tombstones, warm/cold repair turns) is pinned once by
`test_pure_leaf_verify.py` and is not re-pinned here. What IS re-pinned here is everything the
judge's spec record decides, because that is where a judge-side defect would live. The
document's own schema belongs to `test_pure_leaf.py`, and the raw-evidence excerpt to
`test_raw_evidence_excerpt.py`.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("ATMOFAB_DEP_READINESS_ALLOW_PERSISTED_FALLBACK", "1")

import yaml

import tools.llm_config as lc
import tools.raw_evidence_excerpt as rex
import tools.orchestration_runtime as ort
import tools.workflow_conductor as wc
from tools.pure_leaf import PURE_PROMPT_CONTRACT_VERSION
from tools.tests.llm_samples import agentic_only_config as _agentic_cfg
from tools.tests.llm_samples import sample_config as _cfg
from tools.tests.test_pure_leaf_producer import (
    _conductor,
    _envelope,
    _PureFakeConductor,
)

_NODE = "component/dynamics_advdiff_flux_1d_upwind_center2@0.2.0"
_SPEC_ID = "dynamics_advdiff_flux_1d_upwind_center2"
_SPEC_PATH = f"spec/component/dynamics/advection_diffusion/{_SPEC_ID}"

_REAL_REPO = Path(__file__).resolve().parents[2]

#: The one repository document the judge's context reads, staged by COPYING the real file so
#: the slicer runs against the anchors production reads rather than a stand-in's.
_RUNNER_OUTPUT_CONTRACT = "docs/workflow/RUNNER_OUTPUT_CONTRACT.md"


def _io_contract() -> dict:
    return {
        "inputs": [], "outputs": [],
        "test_evidence_requirements": [
            {"test_id": "t_basic", "required_raw_variables": ["u", "dx"]},
        ],
        "test_predicates": [
            {"test_id": "t_basic", "expected_outcome": "pass",
             "target_cases": ["case_a"]},
        ],
        "raw_requirements": {
            "required_evidence": [
                {"artifact": "metrics_basis.json", "required": True},
                {"artifact": "state_snapshots", "required": True, "min_samples": 1},
            ],
        },
    }


def _ir() -> dict:
    return {
        "schema_version": "1.0",
        "meta": {"spec_id": _SPEC_ID, "spec_kind": "component"},
        "case": {"test_case_set": [{"case_id": "case_a", "test_id": "t_basic"}]},
        "algorithm": {"execution_mode": "sequence", "steps": []},
        "impl_defaults": {"toolchain": {}},
        "io_contract": _io_contract(),
        "dependency": {"node_key": _NODE, "direct_deps": []},
        "public_api": {"published_operations": [{"operation_id": f"{_SPEC_ID}__apply"}]},
    }


def _write_run_node(repo: Path, *, bundle: bool = False) -> wc.NodeRefs:
    """Stage what a `validate.judge` finds on disk: the spec's tests, the lowered IR, the
    build and source metas, and a completed run node with its raw evidence."""
    src = _REAL_REPO / _RUNNER_OUTPUT_CONTRACT
    dst = repo / _RUNNER_OUTPUT_CONTRACT
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)

    spec_dir = repo / _SPEC_PATH
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "tests.md").write_text(
        "## 1. Tests\n- test_id: t_basic\n  judgment: the metric is under the threshold\n",
        encoding="utf-8")

    refs = wc.NodeRefs(node_key=_NODE, spec_path=_SPEC_PATH, ir_id="ir_20260908_001",
                       pipeline_id="p_20260908_001", source_id="src_20260908_001",
                       binary_id="bin_20260908_001", run_id="run_20260908_001")

    ir_dir = repo / refs.ir_ref
    ir_dir.mkdir(parents=True, exist_ok=True)
    (ir_dir / "spec.ir.yaml").write_text(yaml.safe_dump(_ir(), sort_keys=False),
                                         encoding="utf-8")

    source_dir = repo / refs.source_dir()
    (source_dir / "src").mkdir(parents=True, exist_ok=True)
    (source_dir / "source_meta.json").write_text(
        json.dumps({"verification_status": "pass"}), encoding="utf-8")
    (source_dir / "src" / f"{_SPEC_ID}_model.f90").write_text("model\n", encoding="utf-8")
    (source_dir / "src" / f"{_SPEC_ID}_runner.f90").write_text("runner\n", encoding="utf-8")
    if bundle:
        (source_dir / "codegen_bundle.json").write_text(json.dumps({"files": [
            {"role": "model", "logical_path": "declared_model.f90"},
            {"role": "checks", "logical_path": "declared_checks.f90"},
        ]}), encoding="utf-8")
        (source_dir / "src" / "declared_model.f90").write_text("model\n", encoding="utf-8")

    binary_dir = repo / refs.binary_dir()
    binary_dir.mkdir(parents=True, exist_ok=True)
    (binary_dir / "binary_meta.json").write_text(
        json.dumps({"verification_status": "pass"}), encoding="utf-8")

    run_dir = repo / refs.run_node_dir()
    (run_dir / "raw" / "state_snapshots").mkdir(parents=True, exist_ok=True)
    for name, doc in (
        ("diagnostics.json", {"per_case": {"case_a": {"metrics": {"l2": 0.01}}}}),
        ("verdict.json", {"per_test": [{"test_id": "t_basic", "status": "pass"}]}),
        ("perf.json", {"wall_time_s": 1.5}),
        ("trial_meta.json", {"trial": 1}),
        ("quality_check.json", {"status": "pass"}),
    ):
        (run_dir / name).write_text(json.dumps(doc), encoding="utf-8")
    (run_dir / "raw" / "metrics_basis.json").write_text(json.dumps(
        {"per_test": [{"test_id": "t_basic", "case_id": "case_a",
                       "u": [[1.0, 2.0]], "dx": 0.5}]}), encoding="utf-8")
    (run_dir / "raw" / "state_snapshots" / "snapshot_schema.json").write_text(json.dumps(
        {"variables": [{"name": "u", "shape_expr": "[nx]"}], "time_variable": "t",
         "time_shape_expr": "scalar", "min_samples": 1}), encoding="utf-8")
    (run_dir / "raw" / "state_snapshots" / "case_a.json").write_text(
        json.dumps({"u": [1.0, 2.0], "t": 0.5}), encoding="utf-8")
    return refs


def _review(decision: str = "pass", **overrides) -> dict:
    doc: dict = {"decision": decision, "findings": []}
    if decision == "fail":
        doc["findings"] = [{
            "attribution": "code",
            "evidence_refs": ["diagnostics_document#per_case.case_a.metrics.l2"],
            "confidence": "high",
            "description": "the reported l2 is not supported by the raw evidence",
        }]
    doc.update(overrides)
    return doc


def _finalized(conductor) -> list[dict]:
    return [cap["--agent-run-json"] for s, cap in conductor.calls if s == "finalize-child"]


def _superseded_reasons(conductor) -> list[str]:
    return [cap["--reason"] for s, cap in conductor.calls if s == "add-superseded-runs"]


class _Fixture(unittest.TestCase):
    BUNDLE = False

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        self.refs = _write_run_node(self.repo, bundle=self.BUNDLE)

    def conductor(self, *envelopes: str) -> _PureFakeConductor:
        c = _conductor(self.repo)
        c.envelopes = list(envelopes) or ["{}"]
        c.calls = []
        return c

    def run_node(self, name: str) -> Path:
        return self.repo / self.refs.run_node_dir() / name


# ======================================================================================
# Context assembly
# ======================================================================================
class PureJudgeContextTests(_Fixture):
    def test_context_keys_are_exactly_the_required_set(self) -> None:
        """Set identity against the table the launch validator reads, so a slot added to one
        without the other is red here rather than at a launch."""
        ctx = self.conductor()._build_pure_judge_context(self.refs)
        self.assertEqual(set(ctx),
                         set(ort.PURE_CONTEXT_REQUIRED_KEYS[("validate", "judge")]))
        for key, value in ctx.items():
            self.assertTrue(value.strip(), f"{key} is empty; the validator would refuse it")

    def test_the_document_map_covers_every_inlined_slot(self) -> None:
        """`evidence_refs` resolution reads this map, so a slot missing from it is a citation
        that survives into `semantic_review.json` unresolved."""
        c = self.conductor()
        self.assertEqual(set(c._pure_judge_document_refs(self.refs)),
                         set(ort.PURE_CONTEXT_REQUIRED_KEYS[("validate", "judge")]))

    def test_io_contract_is_the_irs_section_and_not_the_whole_ir(self) -> None:
        ctx = self.conductor()._build_pure_judge_context(self.refs)
        self.assertEqual(yaml.safe_load(ctx["io_contract_document"]), _io_contract())
        # The rest of the IR is deliberately absent: 52 KB of which the judge needs one section.
        self.assertNotIn("published_operations", ctx["io_contract_document"])

    def test_runner_output_contract_is_the_two_evidence_sections(self) -> None:
        ctx = self.conductor()._build_pure_judge_context(self.refs)
        headings = [ln for ln in ctx["runner_output_contract_document"].splitlines()
                    if ln.startswith("## ")]
        self.assertEqual(headings, ["## 1. `diagnostics.json`", "## 3. `raw/` primary evidence"])

    def test_the_excerpt_is_the_hosts_and_carries_no_array(self) -> None:
        ctx = self.conductor()._build_pure_judge_context(self.refs)
        excerpt = json.loads(ctx["raw_evidence_excerpt_document"])
        self.assertEqual(excerpt["coverage"]["present"], [["t_basic", "case_a"]])
        self.assertEqual(excerpt["coverage"]["missing"], [])
        self.assertEqual(excerpt["problems"], [])

    def test_every_missing_document_raises(self) -> None:
        """The judge's disposition, and the one thing that must not be copied from its
        `generate.verify` sibling, which degrades a missing node artifact to `""`.

        NOT because a blank value would reach the leaf — it would not, and an earlier version of
        this docstring said so wrongly, which is the same false sentence round 2 corrected in
        the code and the reference doc and missed HERE, in the file that is this disposition's
        canonical pin. `_validate_pure_launch_request_payload` counts a whitespace-only
        `pure_context` value as missing. What degrading actually buys is a refusal one frame
        later, inside `record_launch`, escaping this loop's named `pure_context_assembly_failed`
        branch and aborting the conductor instead."""
        c = self.conductor()
        for key, rel in sorted(c._pure_judge_document_refs(self.refs).items()):
            if key == "raw_evidence_excerpt_document":
                continue  # a directory, and its absence is a FACT the excerpt reports
            path = self.repo / rel
            moved = path.with_suffix(path.suffix + ".moved")
            path.rename(moved)
            with self.subTest(document=key):
                with self.assertRaises(RuntimeError) as caught:
                    c._build_pure_judge_context(self.refs)
                # The EXACT name, not `"pure_" in ...`: the weaker form this replaced was
                # green for any of the nine keys against any other key's message, so the
                # names could have been swapped wholesale without a red row. The name is what
                # reaches the operator through `pure_context_assembly_failed`.
                self.assertIn(f"pure_{key}_missing", str(caught.exception))
            moved.rename(path)

    def test_an_absent_raw_directory_does_not_raise(self) -> None:
        """The judge is precisely the leaf that should SEE missing evidence, so the excerpt
        reports it and the context still assembles."""
        shutil.rmtree(self.run_node("raw"))
        ctx = self.conductor()._build_pure_judge_context(self.refs)
        excerpt = json.loads(ctx["raw_evidence_excerpt_document"])
        self.assertFalse(excerpt["raw_dir_present"])
        self.assertEqual(excerpt["coverage"]["missing"], [["t_basic", "case_a"]])

    def test_an_ir_without_an_io_contract_raises(self) -> None:
        ir = _ir()
        del ir["io_contract"]
        (self.repo / self.refs.ir_ref / "spec.ir.yaml").write_text(
            yaml.safe_dump(ir), encoding="utf-8")
        with self.assertRaises(RuntimeError) as caught:
            self.conductor()._build_pure_judge_context(self.refs)
        self.assertIn("pure_io_contract_document_missing", str(caught.exception))

    def test_an_unparseable_ir_raises_its_own_named_outcome(self) -> None:
        """The IR is read twice — once as bytes, once as YAML — and the second failure had no
        test. A file that exists and does not parse is a different outcome from one that is
        absent, and the operator is told which."""
        (self.repo / self.refs.ir_ref / "spec.ir.yaml").write_text(
            "io_contract: [unclosed\n", encoding="utf-8")
        with self.assertRaises(RuntimeError) as caught:
            self.conductor()._build_pure_judge_context(self.refs)
        self.assertIn("pure_io_contract_document_unreadable", str(caught.exception))

    def test_an_unsliceable_runner_output_contract_raises(self) -> None:
        (self.repo / _RUNNER_OUTPUT_CONTRACT).write_text(
            "# Runner output contract\n\nno numbered sections here\n", encoding="utf-8")
        with self.assertRaises(RuntimeError) as caught:
            self.conductor()._build_pure_judge_context(self.refs)
        self.assertIn("unsliceable", str(caught.exception))

    def test_assembly_failure_spawns_no_leaf(self) -> None:
        (self.repo / self.refs.run_node_dir() / "diagnostics.json").unlink()
        c = self.conductor(_envelope(json.dumps(_review())))
        outcome = c.run_substep(self.refs, "validate", "judge")
        self.assertEqual(outcome.status, "fail")
        self.assertEqual(outcome.infra_error[0], "pure_context_assembly_failed")
        self.assertEqual([s for s, _ in c.calls if s == "record-launch"], [])


# ======================================================================================
# The judge document -> `semantic_review.json`
# ======================================================================================
class SemanticReviewWriterTests(_Fixture):
    def test_pass_document_becomes_a_gate_shaped_file(self) -> None:
        c = self.conductor()
        c._write_semantic_review(self.refs, _review("pass"), attempts=1)
        doc = json.loads(self.run_node("semantic_review.json").read_text())
        # The literal the `--stage pre_judge` gate requires. Host-written, so it cannot be
        # written wrong — which is what a paragraph of the SKILL used to be for.
        self.assertEqual(doc["review_method"], "llm_semantic_review")
        self.assertEqual((doc["decision"], doc["findings"]), ("pass", []))
        self.assertEqual(doc["node_key"], self.refs.node_key)
        self.assertEqual(doc["run_id"], self.refs.run_id)
        self.assertEqual(doc["attempt_count"], 1)
        # FROM the constant, not a literal: the stamp exists so a recorded review says which
        # window its judge looked through, which a hardcoded number would not track.
        self.assertEqual(doc["raw_excerpt_policy_version"], rex.RAW_EXCERPT_POLICY_VERSION)
        self.assertEqual(doc["prompt_contract_version"], PURE_PROMPT_CONTRACT_VERSION)
        self.assertNotIn("notes", doc)

    def test_scope_refs_exist_and_are_workspace_rooted(self) -> None:
        """What the gate actually checks about `scope`, asserted the way the gate does."""
        c = self.conductor()
        c._write_semantic_review(self.refs, _review("pass"), attempts=1)
        scope = json.loads(self.run_node("semantic_review.json").read_text())["scope"]
        for key in ("model_ref", "runner_ref"):
            with self.subTest(ref=key):
                self.assertTrue(scope[key].startswith("workspace/"))
                self.assertTrue((self.repo / scope[key]).exists())
        self.assertTrue(scope["raw_refs"])
        for ref in scope["raw_refs"]:
            self.assertTrue(ref.startswith("workspace/"))
            self.assertTrue((self.repo / ref).is_file())
        self.assertEqual(scope["raw_refs"], sorted(scope["raw_refs"]))

    def test_notes_are_carried_when_present(self) -> None:
        c = self.conductor()
        c._write_semantic_review(self.refs, _review("pass", notes="quality check clean"),
                                 attempts=2)
        doc = json.loads(self.run_node("semantic_review.json").read_text())
        self.assertEqual(doc["notes"], "quality check clean")

    def test_evidence_refs_are_resolved_to_the_paths_they_were_inlined_from(self) -> None:
        c = self.conductor()
        c._write_semantic_review(self.refs, _review("fail"), attempts=1)
        doc = json.loads(self.run_node("semantic_review.json").read_text())
        ref = doc["findings"][0]["evidence_refs"][0]
        path, _, fragment = ref.partition("#")
        self.assertEqual(path, f"{self.refs.run_node_dir()}/diagnostics.json")
        self.assertTrue((self.repo / path).is_file())
        self.assertEqual(fragment, "per_case.case_a.metrics.l2")

    def test_a_ref_without_a_fragment_resolves_to_the_bare_path(self) -> None:
        review = _review("fail")
        review["findings"][0]["evidence_refs"] = ["tests_document"]
        self.conductor()._write_semantic_review(self.refs, review, attempts=1)
        doc = json.loads(self.run_node("semantic_review.json").read_text())
        self.assertEqual(doc["findings"][0]["evidence_refs"],
                         [f"{_SPEC_PATH}/tests.md"])

    def test_an_unresolvable_ref_is_kept_verbatim_rather_than_dropped(self) -> None:
        """The in-loop validator refuses an unknown key, so this is unreachable through the
        live path — and it is written this way deliberately: a finding is the operator's
        evidence, and silently deleting a reference to tidy the file is worse than leaving an
        unresolved one."""
        review = _review("fail")
        review["findings"][0]["evidence_refs"] = ["not_a_document#x"]
        self.conductor()._write_semantic_review(self.refs, review, attempts=1)
        doc = json.loads(self.run_node("semantic_review.json").read_text())
        self.assertEqual(doc["findings"][0]["evidence_refs"], ["not_a_document#x"])

class SemanticReviewScopeWithBundleTests(_Fixture):
    BUNDLE = True

    def test_a_declared_model_role_names_the_model_ref(self) -> None:
        c = self.conductor()
        scope = c._semantic_review_scope(self.refs)
        self.assertEqual(scope["model_ref"],
                         f"{self.refs.source_dir()}/src/declared_model.f90")
        # No `runner` role in bundle v1, so the runner ref stays the conductor's own naming —
        # and that naming is not a new spelling invented here.
        self.assertEqual(scope["runner_ref"],
                         f"{self.refs.source_dir()}/src/{_SPEC_ID}_runner.f90")

    def test_two_files_in_one_role_leave_the_conductors_naming(self) -> None:
        """Guessing which of two was "the model" would put an arbitrary path in the record."""
        path = self.repo / self.refs.source_dir() / "codegen_bundle.json"
        path.write_text(json.dumps({"files": [
            {"role": "model", "logical_path": "a.f90"},
            {"role": "model", "logical_path": "b.f90"}]}), encoding="utf-8")
        scope = self.conductor()._semantic_review_scope(self.refs)
        self.assertEqual(scope["model_ref"],
                         f"{self.refs.source_dir()}/src/{_SPEC_ID}_model.f90")

    def test_an_unreadable_bundle_leaves_the_conductors_naming(self) -> None:
        (self.repo / self.refs.source_dir() / "codegen_bundle.json").write_text(
            "{not json", encoding="utf-8")
        scope = self.conductor()._semantic_review_scope(self.refs)
        self.assertEqual(scope["model_ref"],
                         f"{self.refs.source_dir()}/src/{_SPEC_ID}_model.f90")


# ======================================================================================
# The reviewer loop, driven end to end with a fake transport
# ======================================================================================
class PureJudgeLoopTests(_Fixture):
    def test_a_pass_decision_writes_both_records_and_a_vouched_row(self) -> None:
        c = self.conductor(_envelope(json.dumps(_review("pass"))))
        outcome = c.run_substep(self.refs, "validate", "judge")
        self.assertEqual(outcome.status, "pass")
        self.assertEqual(json.loads(self.run_node("semantic_review.json").read_text())[
            "decision"], "pass")
        meta = json.loads(self.run_node("judge_meta.json").read_text())
        self.assertEqual(meta["result"], "pass")
        self.assertEqual(meta["prompt_contract_version"], PURE_PROMPT_CONTRACT_VERSION)
        row = _finalized(c)[-1]
        # A pure row carries no output_refs, so the summary is the only thing that can satisfy
        # `_validate_agent_summary_text`.
        self.assertEqual(row["output_refs"], [])
        self.assertIn("pure_judge_pass", row["result_summary"])
        self.assertEqual(row["substep"], "judge")

    def test_the_launch_is_a_pure_one_with_no_write_authority(self) -> None:
        c = self.conductor(_envelope(json.dumps(_review("pass"))))
        c.run_substep(self.refs, "validate", "judge")
        request = [cap["--request-json"] for s, cap in c.calls if s == "record-launch"][-1]
        self.assertEqual(request["leaf_mode"], "pure")
        self.assertEqual(request["allowed_output_paths"], [])
        self.assertEqual(request["skill_must_read_refs"], "")
        self.assertEqual(set(request["pure_context"]),
                         set(ort.PURE_CONTEXT_REQUIRED_KEYS[("validate", "judge")]))

    def test_the_request_stamps_the_nodes_own_host_authorship(self) -> None:
        """Not the M3c constant the two older reviewers pass: the judge runs on every node
        kind, and `_payload_is_m3c_physics` believes what the request says.

        Only `runner_host_authored` reaches the payload (and only when TRUE, so absence is the
        node's False); `makefile_host_authored` decides the Makefile deliverable, which a pure
        launch empties anyway. The fixture node is one the host renders NEITHER for, which is
        exactly the case the M3c constant would have got wrong."""
        c = self.conductor(_envelope(json.dumps(_review("pass"))))
        c.run_substep(self.refs, "validate", "judge")
        request = [cap["--request-json"] for s, cap in c.calls if s == "record-launch"][-1]
        self.assertFalse(c._conductor_authors_runner(self.refs))
        self.assertFalse(request.get("runner_host_authored", False))

    def test_a_fail_decision_is_a_review_rejection_not_an_error(self) -> None:
        c = self.conductor(_envelope(json.dumps(_review("fail"))))
        outcome = c.run_substep(self.refs, "validate", "judge")
        self.assertEqual(outcome.status, "fail")
        self.assertIsNone(outcome.infra_error)
        doc = json.loads(self.run_node("semantic_review.json").read_text())
        self.assertEqual(doc["decision"], "fail")
        self.assertEqual(json.loads(self.run_node("judge_meta.json").read_text())["result"],
                         "pass")
        row = _finalized(c)[-1]
        self.assertIn("pure_judge_fail: code:", row["result_summary"])

    def test_a_document_violation_is_repaired_in_loop(self) -> None:
        bad = _envelope(json.dumps({"decision": "pass", "findings": [], "extra": 1}))
        good = _envelope(json.dumps(_review("pass")))
        c = self.conductor(bad, good)
        outcome = c.run_substep(self.refs, "validate", "judge")
        self.assertEqual(outcome.status, "pass")
        self.assertEqual(outcome.attempts, 2)

    def test_the_tombstone_names_the_decision_the_repaired_document_carried(self) -> None:
        """`superseded_detail` is the judge's own: a verify verdict's `verify_status=` clause
        would be the wrong noun in a judge's record, and only the reason's PREFIX was pinned."""
        bad = _envelope(json.dumps({"decision": "pass", "findings": [], "extra": 1}))
        c = self.conductor(bad, _envelope(json.dumps(_review("pass"))))
        c.run_substep(self.refs, "validate", "judge")

    def test_a_failed_attempt_says_which_document_was_missing(self) -> None:
        """`document_name` is the judge's own too: a judge that returned nothing did not fail
        to return a verify verdict, and that reply is what the agent-run record carries."""
        c = self.conductor(_envelope("not a json document at all"))
        c.spawn_leaf = lambda *a, **k: wc.ProcResult(1, "", "boom")  # type: ignore[assignment]
        c.run_substep(self.refs, "validate", "judge")
        replies = [cap["--reply-text"] for s, cap in c.calls
                   if s == "record-child-return" and "--reply-text" in cap]
        replies += [cap.get("--reply-text", "") for s, cap in c.calls if s == "finalize-child"]
        self.assertTrue(any("semantic review: none" in (r or "") for r in replies),
                        f"replies were {replies}")
        self.assertFalse(any("verify verdict" in (r or "") for r in replies))

    def test_a_failed_attempt_emits_its_own_event(self) -> None:
        """`pure_semantic_review_attempt_failed` was the one event name in its family with no
        test anywhere."""
        c = self.conductor(_envelope(json.dumps({"decision": "maybe", "findings": []})))
        events: list[dict] = []
        c.emit = lambda name, **kw: events.append({"event": name, **kw})  # type: ignore[assignment]
        c.run_substep(self.refs, "validate", "judge")
        names = [e["event"] for e in events]
        self.assertIn("pure_semantic_review_attempt_failed", names)

    def test_an_evidence_ref_naming_a_document_it_was_not_given_is_a_violation(self) -> None:
        """The in-loop check that keeps `semantic_review.json` a path list: the leaf may cite
        only what it was handed, and the host resolves that."""
        review = _review("fail")
        review["findings"][0]["evidence_refs"] = ["workspace/runs/r1/raw/metrics_basis.json"]
        c = self.conductor(_envelope(json.dumps(review)),
                           _envelope(json.dumps(_review("pass"))))
        outcome = c.run_substep(self.refs, "validate", "judge")
        # Refused, then repaired: the first attempt is tombstoned under the schema reason.
        self.assertEqual((outcome.status, outcome.attempts), ("pass", 2))
        # And the repair turn was told what was wrong with the citation.
        request = [cap["--request-json"] for s, cap in c.calls if s == "record-launch"][-1]
        self.assertIn("not one of the documents you were given",
                      request["repair_findings"])

    def test_an_exhausted_budget_writes_no_review(self) -> None:
        """The signal `run_phase` routes on: no `semantic_review.json`, so
        `_judge_semantic_decision` reads nothing and the existing conformance branch fires —
        `validate_judge_conformance_violation`, the reason that already existed for this class —
        NOT the `validate_pre_judge_violation` a post_judge conformance violation takes."""
        bad = _envelope(json.dumps({"decision": "maybe", "findings": []}))
        c = self.conductor(bad, bad, bad, bad)
        outcome = c.run_substep(self.refs, "validate", "judge")
        self.assertEqual(outcome.status, "fail")
        self.assertFalse(self.run_node("semantic_review.json").exists())
        meta = json.loads(self.run_node("judge_meta.json").read_text())
        self.assertEqual(meta["result"], "fail")
        self.assertEqual(meta["failure_category"], wc.SEMANTIC_REVIEW_DOCUMENT_VIOLATION)

    def test_a_transport_death_is_not_a_document_violation(self) -> None:
        c = self.conductor(_envelope(json.dumps(_review("pass"))))
        c.spawn_leaf = lambda *a, **k: wc.ProcResult(1, "", "boom")  # type: ignore[assignment]
        outcome = c.run_substep(self.refs, "validate", "judge")
        self.assertEqual(outcome.status, "fail")
        self.assertFalse(self.run_node("semantic_review.json").exists())
        self.assertEqual(json.loads(self.run_node("judge_meta.json").read_text())[
            "failure_category"], "pure_transport")

    def test_a_host_write_failure_is_a_named_transport_outcome(self) -> None:
        c = self.conductor(_envelope(json.dumps(_review("pass"))))

        def _boom(*a, **k):
            raise OSError("no space left on device")

        c._write_semantic_review = _boom  # type: ignore[assignment]
        outcome = c.run_substep(self.refs, "validate", "judge")
        self.assertEqual(outcome.status, "fail")
        self.assertEqual(outcome.infra_error[0], "pure_judge_host_write_failed")


# ======================================================================================
# Everything downstream of `semantic_review.json` becoming HOST-authored
# ======================================================================================
class PostJudgeReclassificationTests(_Fixture):
    """The disposition `_post_judge_inproc` WRITES, driven end to end.

    An earlier version of this class asserted the two INPUTS of the pure-judge reclassification
    — `classify_post_judge_violations(...) == "recoverable"` and `_pure_leaf_substep(...) is
    True` — and never called `_post_judge_inproc`. Three independent review axes reported the
    same thing: deleting the reclassification left the whole suite green. Asserting the inputs
    of a branch is not asserting the branch, so these rows read `post_judge_meta.json`.

    Issue #176 removed the reclassification itself, as DEAD rather than as wrong: with the
    warm-resume mini-loop gone, `recoverable` and `unrecoverable` both write `fail_closed`, so
    upgrading one to the other changed nothing. The rows below therefore assert the same
    disposition on BOTH transports — the pure judge's reason (the host authored the file, so a
    re-run emits the identical one) is unchanged, and the agentic judge now terminalizes for
    the separate reason that nothing consumes a `warm_resume`.
    """

    _VIOLATION = ("workspace/pipelines/p/runs/r/n/semantic_review.json: review_method must be "
                  "the literal 'llm_semantic_review'")

    def _disposition(self, config, violation: str | None = None) -> str:
        """Run the real `_post_judge_inproc` against a gate that fails with `violation`
        (default: one naming `semantic_review.json`), and return the disposition it recorded."""
        c = wc.Conductor(repo_root=self.repo, orchestration_id="o",
                         orchestration_agent_run_id="orch", llm_config=config, env={})
        c._author_derived_validate_artifacts = lambda refs: None  # type: ignore[assignment]

        bullet = self._VIOLATION if violation is None else violation

        def _gate(cmd, **kwargs):
            return wc.subprocess.CompletedProcess(
                cmd, 1, stdout=f"FAIL\n- {bullet}\n", stderr="")

        with mock.patch.object(wc.subprocess, "run", _gate):
            c._post_judge_inproc(self.refs, "child-1", "tok")
        meta = json.loads(self.run_node("post_judge_meta.json").read_text())
        return meta["disposition"]

    def test_the_classifier_still_calls_this_violation_recoverable(self) -> None:
        """The premise the reclassification acts on. Kept so a row below cannot go green
        because the CLASSIFIER changed — it must go green because the host reclassified."""
        self.assertEqual(wc.classify_post_judge_violations([self._VIOLATION]), "recoverable")

    def test_an_unknown_violation_is_written_as_escalate(self) -> None:
        """The OTHER value the fold writes, and the half nothing observed.

        `disposition = "escalate" if severity == "unknown" else "fail_closed"` has two
        outcomes and only one of them had a witness: every test of the `escalate` ROUTE seeds
        `post_judge_meta.json` by hand, so collapsing this expression to a bare
        `"fail_closed"` left the whole suite green — measured at c73376f, and measured green on
        `origin/main` too for the equivalent narrowing, so the gap is older than the fold. It is
        pinned here because the fold is where the expression now lives: without it, a change
        that stops writing `escalate` silently sends a prod `unknown` to `fail_closed` instead
        of to the escalate diagnostician.

        The classifier's own verdict is asserted first, so this row cannot go green because
        `classify_post_judge_violations` changed its mind about the probe.
        """
        unknown = "workspace/pipelines/p/runs/r/n/diagnostics.json: missing key"
        self.assertEqual(wc.classify_post_judge_violations([unknown]), "unknown")
        for label, config in (("pure", _cfg("claude")), ("agentic", _agentic_cfg("claude"))):
            with self.subTest(transport=label):
                self.assertEqual(self._disposition(config, violation=unknown), "escalate")

    def test_a_review_violation_terminalizes_on_either_transport(self) -> None:
        """`recoverable` used to mean "the judge wrote it wrong, so re-run the judge". That
        premise was already false for a PURE judge — the HOST wrote the file, so the re-run
        emits the identical one — and since issue #176 nothing re-runs either transport's
        judge, so both record `fail_closed`. Two configs, one expectation: a reclassification
        reintroduced on one side only would separate them again."""
        self.assertEqual(self._disposition(_cfg("claude")), "fail_closed")
        self.assertEqual(self._disposition(_agentic_cfg("claude")), "fail_closed")


class PureJudgeFreshnessTests(_Fixture):
    """The defensive branch of `determine_substep_status`. Unreachable through the live path —
    the pure substep computes its own status and returns early — and it must not be left to
    the generic tail, where a pure judge's EMPTY `allowed_output_paths` is vacuously fresh."""

    def _status(self, min_mtime: float) -> str:
        c = self.conductor()
        return c.determine_substep_status(
            self.refs, "validate", "judge", [], min_mtime=min_mtime)[0]

    def _seed(self, *, decision: str = "pass", result: str = "pass") -> None:
        self.run_node("semantic_review.json").write_text(
            json.dumps({"decision": decision}), encoding="utf-8")
        self.run_node("judge_meta.json").write_text(
            json.dumps({"result": result}), encoding="utf-8")

    def test_a_fresh_pass_passes(self) -> None:
        self._seed()
        self.assertEqual(self._status(0.0), "pass")

    def test_a_stale_review_cannot_certify(self) -> None:
        self._seed()
        stale = self.run_node("semantic_review.json").stat().st_mtime + 100.0
        self.assertEqual(self._status(stale), "fail")

    def test_a_fail_decision_fails(self) -> None:
        self._seed(decision="fail")
        self.assertEqual(self._status(0.0), "fail")

    def test_no_judge_meta_fails(self) -> None:
        self._seed()
        self.run_node("judge_meta.json").unlink()
        self.assertEqual(self._status(0.0), "fail")

    def test_an_exhausted_judge_meta_fails_even_beside_a_passing_review(self) -> None:
        self._seed(result="fail")
        self.assertEqual(self._status(0.0), "fail")


class ConductorConstantIsNotAFieldTests(unittest.TestCase):
    """`_PURE_JUDGE_RUN_NODE_DOCUMENTS` is annotated `ClassVar`, and `Conductor` is a
    dataclass. Drop `ClassVar` from the annotation (or from the module's imports, which is how
    `dataclasses` resolves it) and the constant silently becomes a FIELD: `Conductor` grows a
    constructor parameter and every instance gets its own copy. Measured on this branch — the
    import-line mutant survived the whole suite, because nothing anywhere pins the field set."""

    def test_the_document_table_is_a_class_constant_not_a_field(self) -> None:
        import dataclasses
        names = {f.name for f in dataclasses.fields(wc.Conductor)}
        self.assertNotIn("_PURE_JUDGE_RUN_NODE_DOCUMENTS", names)
        # Self-test the probe: it must be reading a populated field set, or the assertion above
        # is green because `fields()` returned nothing.
        self.assertIn("repo_root", names)


class PureJudgeTableTests(unittest.TestCase):
    """The tables that decide the judge is pure, each read where it is defined."""

    def test_the_judge_is_pure_capable(self) -> None:
        self.assertIn(("validate", "judge"), lc.PURE_CAPABLE_SUBSTEPS)

    def test_every_llm_leaf_is_now_pure_capable(self) -> None:
        # Issue #169's completion criterion, in the module that holds both tables.
        self.assertEqual(lc.LLM_LEAF_SUBSTEPS, lc.PURE_CAPABLE_SUBSTEPS)

    def test_the_template_states_the_cap_the_validator_enforces(self) -> None:
        """A dual-read pair, and the reason it is worth a row: the leaf is told a number by the
        TEMPLATE and refused by the VALIDATOR, so if they disagree a compliant judge is
        rejected for obeying its instructions — this repository's recorded default error
        direction, and exactly what the cap's own measurement was correcting. Neither number is
        transcribed here; the template is required to state whatever the constant says."""
        from tools.pure_leaf import SEMANTIC_REVIEW_NOTES_MAX_CHARS
        template = (Path(wc.__file__).resolve().parents[1] / "tools" / "prompt_templates"
                    / "pure_validate_judge.txt").read_text(encoding="utf-8")
        self.assertIn(f"at most {SEMANTIC_REVIEW_NOTES_MAX_CHARS} characters", template)
        # Self-test the search: the assertion above must be able to fail.
        self.assertNotIn(f"at most {SEMANTIC_REVIEW_NOTES_MAX_CHARS + 1} characters", template)

    def test_the_judge_has_a_template_and_required_keys(self) -> None:
        self.assertIn(("validate", "judge"), ort.PURE_CONTEXT_REQUIRED_KEYS)
        self.assertEqual(ort._PROMPT_TEMPLATE_FILES["pure validate.judge"],
                         "pure_validate_judge.txt")

    def test_a_capability_restricted_entry_keeps_the_judge_agentic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _write_run_node(repo)
            c = wc.Conductor(repo_root=repo, orchestration_id="o",
                             orchestration_agent_run_id="orch",
                             llm_config=_agentic_cfg("claude"), env={})
            self.assertFalse(c._pure_leaf_substep(refs, "validate", "judge"))


if __name__ == "__main__":
    unittest.main()
