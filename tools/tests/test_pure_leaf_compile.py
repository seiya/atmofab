#!/usr/bin/env python3
"""Z1 (issue #168): the pure `compile.generate` producer and `compile.verify` reviewer.

Covers the host side of the pure compile channel added across `tools/workflow_conductor.py`
(the two context builders, the IR-document shape validator, the host writers, the routing
tables and the defensive freshness branch), `tools/llm_config.py` (the pure-capable table) and
`tools/orchestration_runtime.py` (the required-context keys and the two templates).

The loop ITSELF is shared with the generate pair — `_run_pure_producer_substep` /
`_run_pure_reviewer_substep`, parametrised by a `_PureProducerSpec` / `_PureReviewerSpec` — so
the transport-shaped behaviour (usage waits, transient retries, tombstones, warm/cold repair
turns) is pinned once by `test_pure_leaf_producer.py` / `test_pure_leaf_verify.py` and is not
re-pinned here. What IS re-pinned here is everything the spec record decides, because that is
what a compile-side defect would live in.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import unittest
from unittest import mock
from pathlib import Path

os.environ.setdefault("ATMOFAB_DEP_READINESS_ALLOW_PERSISTED_FALLBACK", "1")

import yaml

import tools.llm_config as lc
import tools.orchestration_runtime as ort
import tools.workflow_conductor as wc
from tools.backends import registry as backend_registry
from tools.tests.target_fixtures import TARGET_ID as _TARGET_ID
from tools.tests.test_pure_leaf_producer import (
    _conductor,
    _envelope,
    _PureFakeConductor,
)

_NODE = "component/dynamics_advdiff_flux_1d_upwind_center2@0.2.0"
_SAFE = wc.node_key_safe(_NODE)
_SPEC_ID = "dynamics_advdiff_flux_1d_upwind_center2"
_SPEC_PATH = f"spec/component/dynamics/advection_diffusion/{_SPEC_ID}"
_IR_ID = "advdiff-uc2_20260907_001"

_PROBLEM_NODE = "problem/shallow_water2d@0.4.0"
_PROBLEM_SPEC_ID = "shallow_water2d"
_PROBLEM_SPEC_PATH = "spec/problem/dynamics/shallow_water/shallow_water2d"

#: The repository documents `_build_pure_compile_context` reads. Staged into the fixture repo by
#: COPYING the real files, so the slicers and the presence checks run against what production
#: reads rather than against a stand-in whose anchors a test author chose.
_REPO_DOCUMENTS = (
    "docs/workflow/CHECKS_MODULE_CONTRACT.md",
    "docs/examples/spec_ir_algorithm_section.example.yaml",
    "docs/examples/spec_ir_algorithm_2d_problem_contract.example.yaml",
)

_REAL_REPO = Path(__file__).resolve().parents[2]


def _stage_repo_documents(repo: Path) -> None:
    refs = list(_REPO_DOCUMENTS) + [ort.WORKFLOW_PHASE_DOC_BY_STEP["compile"]]
    for rel in refs:
        src = _REAL_REPO / rel
        dst = repo / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)


def _write_compile_node(repo: Path, *, kind: str = "component", profile: bool = False,
                        stage_ir: bool = False) -> wc.NodeRefs:
    """Stage what a Compile phase finds on disk: the spec's three documents, a catalog entry,
    and the two host-authored sidecars `run_phase` writes before `compile.generate` runs.

    Deliberately NOT `test_pure_leaf_producer._write_node`, which stages a lowered IR and a
    rendered runner: at `compile.generate` time neither exists, and a fixture that provided them
    would let a context builder read an artifact production cannot have.
    """
    _stage_repo_documents(repo)
    node = _NODE if kind == "component" else _PROBLEM_NODE
    spec_id = _SPEC_ID if kind == "component" else _PROBLEM_SPEC_ID
    spec_path = _SPEC_PATH if kind == "component" else _PROBLEM_SPEC_PATH
    safe = wc.node_key_safe(node)

    spec_dir = repo / spec_path
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "controlled_spec.md").write_text(
        f"# {spec_id}\n\n## 5. Published interface\n"
        f"The only published `operation_id` is `{spec_id}__apply`.\n", encoding="utf-8")
    (spec_dir / "tests.md").write_text(
        "## 1. Tests\n- `test_id`: `t_basic`\n  judgment: the metric is under the threshold\n",
        encoding="utf-8")
    deps: dict = {"spec_id": spec_id, "spec_kind": kind,
                  "dependencies": {"components": [], "profiles": [],
                                   "infrastructure": [
                                       {"infrastructure_id": "harness_fortran_cpu",
                                        "version_constraint": ">=0.3.0 <1.0.0"}]}}
    if profile:
        deps["dependencies"]["profiles"] = [
            {"profile_id": "demo_profile", "version_constraint": ">=0.1.0 <1.0.0"}]
    (spec_dir / "deps.yaml").write_text(yaml.safe_dump(deps, sort_keys=False), encoding="utf-8")

    specs = [{"spec_kind": kind, "spec_id": spec_id, "spec_version": node.split("@")[1],
              "controlled_spec_path": f"{spec_path}/controlled_spec.md",
              "tests_path": f"{spec_path}/tests.md",
              "deps_path": f"{spec_path}/deps.yaml"}]
    if profile:
        prof_dir = repo / "spec/profile/demo/demo_profile"
        prof_dir.mkdir(parents=True, exist_ok=True)
        (prof_dir / "controlled_spec.md").write_text(
            "# demo_profile\n\nThe profile fixes the component set.\n", encoding="utf-8")
        # `deps_path` is what `resolve_spec_ref_for` reads to answer WHERE the profile lives —
        # the same resolver the closure expansion uses (issue #175), so the fixture resolves
        # the profile by the production rule rather than by a bare-id catalog scan.
        (prof_dir / "deps.yaml").write_text(yaml.safe_dump(
            {"spec_id": "demo_profile", "spec_kind": "profile",
             "dependencies": {"components": [], "profiles": []}}, sort_keys=False),
            encoding="utf-8")
        specs.append({"spec_kind": "profile", "spec_id": "demo_profile",
                      "spec_version": "0.1.0",
                      "controlled_spec_path": "spec/profile/demo/demo_profile/controlled_spec.md",
                      "deps_path": "spec/profile/demo/demo_profile/deps.yaml"})
    catalog = repo / "spec" / "registry" / "spec_catalog.yaml"
    catalog.parent.mkdir(parents=True, exist_ok=True)
    catalog.write_text(yaml.safe_dump({"catalog_version": "0.2.0", "specs": specs},
                                      sort_keys=False), encoding="utf-8")

    refs = wc.NodeRefs(target_id=_TARGET_ID, node_key=node, spec_path=spec_path, ir_id=_IR_ID,
                       pipeline_id="p_20260907_001")
    ir_dir = repo / refs.ir_ref
    ir_dir.mkdir(parents=True, exist_ok=True)
    # `profiles` is the sidecar key the host writes for the adopted-profile record (issue
    # #175); it is what `_pure_profile_spec_document` reads, so a profile fixture has to carry
    # it here rather than only in `deps.yaml`.
    (ir_dir / "dependency_graph.json").write_text(
        json.dumps({"node_key": node, "direct_deps": [], "all_nodes": [node],
                    "profiles": ([{"node_key": "profile/demo_profile@0.1.0",
                                   "profile_id": "demo_profile",
                                   "profile_version": "0.1.0",
                                   "version_constraint": ">=0.1.0 <1.0.0",
                                   "components": []}] if profile else [])}, indent=2),
        encoding="utf-8")
    (ir_dir / "dependency_surface.json").write_text(
        # The document shape `_write_dependency_surface` writes (round 1 of issue #486: this was
        # a bare list, which no conductor writes).
        json.dumps({"generated_by": "workflow_conductor._write_dependency_surface",
                    "consumer_node_key": node,
                    "dependencies": [{
                        "node_key": node, "published_operations": [f"{spec_id}__apply"],
                        "source": "ir_public_api",
                        # Issue #486: a resolved entry carries the dep IR's ABI keys.
                        "public_api": {"interfaces": [{"name": f"{spec_id}_rhs", "signature": {
                            "kind": "subroutine", "name": f"{spec_id}_rhs", "args": []}}]}}]},
                   indent=2),
        encoding="utf-8")
    if stage_ir:
        (ir_dir / "spec.ir.yaml").write_text(yaml.safe_dump(_valid_ir(kind), sort_keys=False),
                                             encoding="utf-8")
    assert safe in refs.ir_ref
    return refs


def _valid_ir(kind: str = "component") -> dict:
    ir: dict = {
        "schema_version": "1.0",
        "meta": {"spec_id": _SPEC_ID, "spec_kind": kind},
        "case": {"test_case_set": []},
        "algorithm": {"execution_mode": "sequence", "steps": []},
        "io_contract": {"inputs": [], "outputs": []},
        "dependency": {"node_key": _NODE, "direct_deps": []},
    }
    if kind in ("component", "infrastructure"):
        ir["public_api"] = {"published_operations": [{"operation_id": f"{_SPEC_ID}__apply"}]}
    return ir


def _doc(ir: dict | None = None) -> dict:
    return {"ir": ir if ir is not None else _valid_ir(), "compile_fail": None}


#: The one test id the fixture's tests document declares, so the one verification part.
_TEST_ID = "t_basic"


def _entries(test_id: str = _TEST_ID) -> dict:
    """One test's three verification lists, the shape a part returns them in (issue #526)."""
    return {
        "test_evidence_requirements": [{"test_id": test_id, "required_raw_variables": ["q"]}],
        "test_predicates": [{"test_id": test_id, "expected_outcome": "pass",
                             "target_cases": ["c1"],
                             "pass_when": {"all": [{"ref": "verdict.overall", "op": "eq",
                                                    "value": "pass", "quantity": "q"}]}}],
        "primary_predicates": [{"test_id": test_id, "quantity": "q",
                                "target_cases": ["c1"], "expr": "max(final.q)",
                                "op": "le", "value": 1.0}],
    }


def _part(test_id: str = _TEST_ID) -> dict:
    return {"verification": _entries(test_id), "compile_fail": None}


def _assembled_ir(*test_ids: str) -> dict:
    """`_valid_ir()` as the host writes it once every part passed: each test's entries in the
    order given, in `io_contract`'s schema order (`inputs`, `outputs`, the evidence list, then
    the two predicate lists — the core carries no other `io_contract` key)."""
    ir = _valid_ir()
    parts = [_entries(t) for t in (test_ids or (_TEST_ID,))]
    io = ir["io_contract"]
    ir["io_contract"] = {
        "inputs": io["inputs"], "outputs": io["outputs"],
        "test_evidence_requirements": [e for p in parts for e in p["test_evidence_requirements"]],
        "test_predicates": [e for p in parts for e in p["test_predicates"]],
        "primary_predicates": [e for p in parts for e in p["primary_predicates"]],
    }
    return ir


def _declared(input_name: str = "tests", where: str = "§6",
              what: str = "declares no test_id") -> dict:
    """A well-formed `Compile fail` declaration, and the reason the host records for it."""
    return {"ir": None, "compile_fail": {"input": input_name, "where": where, "what": what}}


def _declared_reason(doc: dict) -> str:
    d = doc["compile_fail"]
    return f"{d['input']} {d['where']}: {d['what']}"


def _verdict(status: str = "pass", severity: str = "none", reason=None) -> dict:
    return {"verification_status": status, "issue_severity": severity,
            "last_fail_reason": reason,
            "findings": [] if status == "pass" else [{"summary": "an item is unmet"}]}


def _finalized(conductor) -> list[dict]:
    """The `--agent-run-json` payload of every `finalize-child`, in order."""
    return [cap["--agent-run-json"] for s, cap in conductor.calls if s == "finalize-child"]


class _Fixture(unittest.TestCase):
    KIND = "component"
    PROFILE = False
    STAGE_IR = False

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        self.refs = _write_compile_node(self.repo, kind=self.KIND, profile=self.PROFILE,
                                        stage_ir=self.STAGE_IR)

    def conductor(self, *envelopes: str) -> _PureFakeConductor:
        c = _conductor(self.repo)
        c.envelopes = list(envelopes) or ["{}"]
        c.calls = []
        return c


# ======================================================================================
# Context assembly
# ======================================================================================
class PureCompileContextTests(_Fixture):
    # The reviewer's context reads the producer's output, so this class stages one.
    STAGE_IR = True

    def test_the_two_builders_produce_exactly_the_declared_key_sets(self) -> None:
        """Set identity against `PURE_CONTEXT_REQUIRED_KEYS`, in both directions: a key the
        builder invents is never inlined by the renderer (the template has no slot for it), and
        a key the table declares but the builder omits fails the launch validator."""
        c = self.conductor()
        self.assertEqual(
            set(c._build_pure_compile_context(self.refs)),
            set(ort.PURE_CONTEXT_REQUIRED_KEYS[("compile", "generate")]))
        self.assertEqual(
            set(c._build_pure_compile_verify_context(self.refs)),
            set(ort.PURE_CONTEXT_REQUIRED_KEYS[("compile", "verify")]))

    def test_a_missing_node_artifact_fails_closed_before_any_launch(self) -> None:
        """NOT a `""` degradation (the generate builders had one until issue #467), and the
        difference is the point.

        Every key these builders return is declared in `PURE_CONTEXT_REQUIRED_KEYS`, and
        `_validate_pure_launch_request_payload` refuses an empty declared key — inside
        `record_launch`, whose `runtime` helper raises `RuntimeError` on a non-zero exit, from a
        call the pure loop does not guard. So an empty value would abort the CONDUCTOR with a
        launch-validation error rather than produce the `pure_context_assembly_failed` outcome
        the loop has a branch for. Raising at assembly is the same refusal one frame earlier,
        where the caller can recover it.

        Driven through `run_substep`, not the builder, so what is pinned is the recovery.
        """
        for artifact, marker in (("controlled_spec.md", "pure_controlled_spec_document_missing"),
                                 ("tests.md", "pure_tests_document_missing"),
                                 ("deps.yaml", "pure_deps_document_missing")):
            with self.subTest(artifact=artifact):
                _write_compile_node(self.repo, kind=self.KIND)
                (self.repo / self.refs.spec_path / artifact).unlink()
                c = self.conductor(_envelope(_doc()))
                outcome = c.run_substep(self.refs, "compile", "generate")
                self.assertEqual(outcome.status, "fail")
                self.assertEqual(outcome.infra_error[0], "pure_context_assembly_failed")
                self.assertIn(marker, outcome.infra_error[1])
                self.assertEqual([s for s, _ in c.calls].count("record-launch"), 0)

    #: Every declared context key that comes from a FILE, and the file it comes from. The one
    #: that does not is `profile_spec_document` (a host sentence when no profile is declared),
    #: covered by rows of its own in this class. (`toolchain_document`, derived from the backend
    #: registry, was the second until R4-a PR-3 removed it with the IR's `impl_defaults`.)
    _KEY_SOURCE = {
        "generate": {
            "controlled_spec_document": "{spec}/controlled_spec.md",
            "tests_document": "{spec}/tests.md",
            "deps_document": "{spec}/deps.yaml",
            "dependency_graph_document": "{ir}/dependency_graph.json",
            "phase_contract_document": "docs/workflow/phases/phase_01_compile.md",
            "ir_algorithm_example_document":
                "docs/examples/spec_ir_algorithm_section.example.yaml",
            "ir_algorithm_2d_example_document":
                "docs/examples/spec_ir_algorithm_2d_problem_contract.example.yaml",
            "checks_module_contract_document": "docs/workflow/CHECKS_MODULE_CONTRACT.md",
        },
        "verify": {
            "controlled_spec_document": "{spec}/controlled_spec.md",
            "tests_document": "{spec}/tests.md",
            "deps_document": "{spec}/deps.yaml",
            "ir_document": "{ir}/spec.ir.yaml",
            "dependency_surface_document": "{ir}/dependency_surface.json",
            "phase_contract_document": "docs/workflow/phases/phase_01_compile.md",
            "ir_algorithm_example_document":
                "docs/examples/spec_ir_algorithm_section.example.yaml",
            "ir_algorithm_2d_example_document":
                "docs/examples/spec_ir_algorithm_2d_problem_contract.example.yaml",
        },
    }

    def test_every_file_backed_key_raises_when_its_file_is_gone(self) -> None:
        """The property, stated as the property. An earlier version of this row asserted that
        every declared key is non-empty ON A COMPLETE FIXTURE — which cannot see whether a read
        DEGRADES or RAISES, and that is the whole question: the launch validator refuses an empty
        declared key inside `record_launch`, whose `runtime` helper raises out of the unguarded
        call, so a `""` here aborts the conductor instead of producing the recoverable
        `pure_context_assembly_failed` outcome. Measured: with that row in place, restoring the
        `""` degradation on the REVIEWER's two node reads left the whole suite green.

        So each key's own file is removed and the builder must raise. The table is checked
        against `PURE_CONTEXT_REQUIRED_KEYS` for set identity below, so a key added to the
        contract without a row here is red rather than unobserved.
        """
        for substep, sources in self._KEY_SOURCE.items():
            builder = (self._build(substep))
            for key, template in sources.items():
                with self.subTest(builder=substep, key=key):
                    _write_compile_node(self.repo, kind=self.KIND, stage_ir=True)
                    rel = template.format(spec=self.refs.spec_path, ir=self.refs.ir_ref)
                    (self.repo / rel).unlink()
                    with self.assertRaises(RuntimeError) as caught:
                        builder(self.refs)
                    message = str(caught.exception)
                    self.assertIn("_missing", message)
                    # BOTH halves of the path, and the basename FIRST: the operator meets this
                    # string through a 200-character `reason_detail`, so the message leads with
                    # the file and trails with its directory — asserting the joined path would
                    # pin the old order and would go green again if the two were swapped back.
                    self.assertIn(Path(rel).name, message)
                    self.assertIn(str(Path(rel).parent), message)
                    self.assertLess(message.index(Path(rel).name),
                                    message.index(str(Path(rel).parent)),
                                    "the basename must precede the directory: it is what "
                                    "survives the reason_detail cap")

    def test_the_key_source_table_covers_every_declared_key(self) -> None:
        """Set identity between the table above and the contract, with the derived key named
        explicitly — so a new key is either given a file row or a deliberate exemption, and
        never silently neither."""
        derived = {"generate": {"profile_spec_document"}, "verify": set()}
        for substep, sources in self._KEY_SOURCE.items():
            with self.subTest(builder=substep):
                self.assertEqual(
                    set(sources) | derived[substep],
                    set(ort.PURE_CONTEXT_REQUIRED_KEYS[("compile", substep)]))

    def test_the_derived_keys_are_non_empty_by_construction(self) -> None:
        """The key with no file: `profile_spec_document` returns the host's fixed sentence when
        the node declares no profile, and a named line when the catalog cannot resolve one — so
        it has no path that yields `""`."""
        c = self.conductor()
        ctx = c._build_pure_compile_context(self.refs)
        self.assertEqual(ctx["profile_spec_document"],
                         wc.Conductor._PURE_PROFILE_ABSENT_DOCUMENT)

    def _build(self, substep: str):
        c = self.conductor()
        return (c._build_pure_compile_context if substep == "generate"
                else c._build_pure_compile_verify_context)

    def test_the_operator_sees_the_filename_the_raise_names(self) -> None:
        """The whole chain, driven end to end: a missing repository document -> the raise ->
        the infra_error the loop recovers it as -> the composed `reason_detail`.

        Each link was changed for one reason — the raise names the BASENAME first so the cap
        eats the least actionable end, `_pure_assembly_detail` drops the redundant exception
        class, and the transport branch derives its evidence clip instead of fixing it at 110 —
        and NONE of them is observable from its own side. Measured before those changes, on this
        tree's longest spec path, the operator got a directory prefix with no filename and a
        remedy nobody can follow.
        """
        (self.repo / self.refs.spec_path / "controlled_spec.md").unlink()
        c = self.conductor(_envelope(_doc()))
        outcome = c.run_substep(self.refs, "compile", "generate")
        tag, detail = outcome.infra_error
        self.assertEqual(tag, "pure_context_assembly_failed")
        # `_pure_assembly_detail`: the message already names its reason, so the class is dropped.
        self.assertTrue(detail.startswith("pure_controlled_spec_document_missing:"), detail)
        # The composition the operator actually reads. Built here exactly as run_phase's
        # transport branch builds it, from the module's own budget constant.
        head = f"leaf_transport_error: leaf_exit=1 (tag: {tag}; )"
        room = max(0, wc._PHASE_REASON_DETAIL_MAX_CHARS - len(head))
        reason = f"leaf_transport_error: leaf_exit=1 (tag: {tag}; {detail[:room]})"
        self.assertLessEqual(len(reason), wc._PHASE_REASON_DETAIL_MAX_CHARS)
        self.assertIn("controlled_spec.md", reason,
                      "the filename is the actionable half and must survive the cap")
        self.assertIn("pure_controlled_spec_document_missing", reason)

    def test_the_event_carries_the_whole_raise_the_reason_detail_clips(self) -> None:
        """`docs/RUNBOOK.md` sends the operator to the `pure_context_assembly_failed` event for
        the full string once `reason_detail` has spent its 200 characters. That promise is only
        as good as the EVENT's own cap, which is why it is a named constant and not a literal:
        the generate pair's raises still put an absolute path last and are the longest this tree
        produces."""
        (self.repo / self.refs.spec_path / "controlled_spec.md").unlink()
        c = self.conductor(_envelope(_doc()))
        events: list = []
        c.emit = (  # type: ignore[assignment]
            lambda ev, _sink=events, **f: _sink.append((ev, f)))
        outcome = c.run_substep(self.refs, "compile", "generate")
        detail = [f["detail"] for e, f in events if e == "pure_context_assembly_failed"][0]
        self.assertGreater(wc._PURE_ASSEMBLY_EVENT_DETAIL_MAX_CHARS,
                           wc._PHASE_REASON_DETAIL_MAX_CHARS,
                           "the event exists to carry what reason_detail cannot")
        # The whole raise, not a prefix of it: reason, filename AND directory.
        self.assertIn("pure_controlled_spec_document_missing", detail)
        self.assertIn("controlled_spec.md", detail)
        self.assertIn(self.refs.spec_path, detail)
        self.assertEqual(detail, outcome.infra_error[1])

    def test_a_missing_repository_document_raises_and_spawns_nothing(self) -> None:
        """A document the leaf cannot repair is a fail_closed BEFORE any launch. Driven through
        `run_substep` rather than the builder, so what is pinned is the recovery, not the raise."""
        for rel, marker in (
            (ort.WORKFLOW_PHASE_DOC_BY_STEP["compile"], "pure_phase_contract_document_missing"),
            ("docs/workflow/CHECKS_MODULE_CONTRACT.md", "pure_checks_contract_document_missing"),
            (f"{self.refs.ir_ref}/dependency_graph.json",
             "pure_dependency_graph_document_missing"),
        ):
            with self.subTest(document=rel):
                _stage_repo_documents(self.repo)
                (self.repo / rel).unlink()
                c = self.conductor(_envelope(_doc()))
                events: list = []
                c.emit = (  # type: ignore[assignment]
                    lambda ev, _sink=events, **f: _sink.append((ev, f)))
                outcome = c.run_substep(self.refs, "compile", "generate")
                self.assertEqual(outcome.status, "fail")
                self.assertEqual(outcome.infra_error[0], "pure_context_assembly_failed")
                self.assertIn(marker, outcome.infra_error[1])
                self.assertEqual([s for s, _ in c.calls].count("record-launch"), 0)
                self.assertIn("pure_context_assembly_failed", [e for e, _ in events])

    def test_a_node_with_no_profile_dependency_gets_the_host_sentence(self) -> None:
        c = self.conductor()
        ctx = c._build_pure_compile_context(self.refs)
        self.assertEqual(ctx["profile_spec_document"],
                         wc.Conductor._PURE_PROFILE_ABSENT_DOCUMENT)

    def test_the_templates_name_no_backend_identifier(self) -> None:
        """The two templates are `neutral core` files (`docs/BACKEND_BOUNDARY.md`), so no
        toolchain value may be spelled in them — and since R4-a PR-3 (issue #284) the compile
        leaf is told none at all: the IR is target-free.

        The probe set is DERIVED from the registry, so a newly registered id is checked too.
        One id is excluded by name and the exclusion is self-tested: `make` is an ordinary
        English word, so matching it as text would refuse any sentence that uses it and would
        say nothing about the boundary. That is a real residue — a template that genuinely named
        that build system would pass this row — and it is bounded by
        `tools/tests/test_backend_boundary.py`, which reads these files with a scanner that does
        not decide membership by substring.
        """
        english_words = {"make"}
        tokens = set()
        for axis in ("language", "build_system", "compiler", "linter"):
            tokens |= set(backend_registry.implemented_backend_ids(axis))
        self.assertTrue(tokens, "no backend ids; this observes nothing")
        # Self-test of the exclusion: every excluded name must still BE a registered id, so a
        # rename leaves an entry here that no longer excludes anything and is red.
        self.assertLessEqual(english_words, tokens)
        probes = sorted(tokens - english_words)
        self.assertTrue(probes, "every backend id was excluded; this observes nothing")
        tpl_dir = Path(ort.__file__).resolve().parent / "prompt_templates"
        for name in ("pure_compile_generate.txt", "pure_compile_verify.txt"):
            text = (tpl_dir / name).read_text(encoding="utf-8")
            for token in probes:
                with self.subTest(template=name, token=token):
                    self.assertNotIn(token, text)

    def test_the_verify_context_carries_the_ir_and_the_resolved_surface(self) -> None:
        (self.repo / self.refs.ir_ref / "spec.ir.yaml").write_text(
            yaml.safe_dump(_valid_ir(), sort_keys=False), encoding="utf-8")
        c = self.conductor()
        ctx = c._build_pure_compile_verify_context(self.refs)
        self.assertIn("schema_version", ctx["ir_document"])
        self.assertIn(f"{_SPEC_ID}__apply", ctx["dependency_surface_document"])
        # The reviewer reads the file verbatim, so the prototype the producer is shown reaches it.
        self.assertIn(f'"name": "{_SPEC_ID}_rhs"', ctx["dependency_surface_document"])

    #: The V3 coverage rule as both compile leaves read it (issue #386). A `problem` `tests.md`
    #: names no `checks.<id>` key in its Diagnostics contract and writes each judgment as test
    #: prose, so a rule keyed on the keys alone left the check list to the producer's taste —
    #: and, read with "neither more nor less", refused the `cfl` id every guard test names.
    #: Each phrase must sit on the V3 bullet itself, not anywhere in the document.
    _V3_PREFIX = "- Every `io_contract.diagnostics_contract.checks[].id` answers to `tests.md`"
    _V3_PHRASES = (
        "to a check a test's `pass_when` names",
        "or to a judgment a test (§6) applies; an id that answers to none is a defect",
        "every judgment a test applies and reports WITHOUT making it a condition of its "
        "`pass_when`",
        "nor does one whose threshold is `informational_only`",
        "A cross-target judgment is no source either way",
    )
    #: Matches both stale spellings, `tests.md §3` and ``tests.md` §3``.
    _STALE = re.compile(r"tests\.md`?\s*§[34]\b")

    def test_both_compile_leaves_read_the_non_gating_coverage_rule(self) -> None:
        """Read through the two builders, so a document the leaves stop inlining is red too.
        What this does NOT hold is the rule's meaning: a rewording that keeps each phrase and
        negates the sentence is green here, and is held by the `COMPILE_INLINED_DOCUMENTS_VERSION`
        digest (`test_derivation_transformation_drift.py`), a review gate on every byte.
        The stale `tests.md §3` / `§4` spellings (the Diagnostics contract is §5 and the tests
        §6 in every `tests.md`) must not come back beside it."""
        # The stale-spelling pattern must be able to match, in both spellings.
        for probe in ("tests.md §3", "`tests.md` §4", "tests.md`§3"):
            self.assertRegex(probe, self._STALE)
        self.assertNotRegex("tests.md §5", self._STALE)
        c = self.conductor()
        for substep, ctx in (("generate", c._build_pure_compile_context(self.refs)),
                             ("verify", c._build_pure_compile_verify_context(self.refs))):
            doc = ctx["phase_contract_document"]
            with self.subTest(substep=substep):
                bullets = [ln for ln in doc.splitlines() if ln.startswith(self._V3_PREFIX)]
                self.assertEqual(len(bullets), 1, "the V3 coverage bullet is not one line")
                for phrase in self._V3_PHRASES:
                    self.assertIn(phrase, bullets[0])
                self.assertIn("one `tests.md` marks non-gating included", doc)
                self.assertNotRegex(doc, self._STALE)


class PureCompileProfileContextTests(_Fixture):
    PROFILE = True

    def test_an_adopted_profile_is_resolved_through_the_sidecar(self) -> None:
        c = self.conductor()
        doc = c._build_pure_compile_context(self.refs)["profile_spec_document"]
        self.assertIn("demo_profile", doc)
        self.assertIn("The profile fixes the component set.", doc)

    def test_the_sidecar_and_not_deps_yaml_decides_which_profiles_are_inlined(self) -> None:
        """Issue #175: the adopted set is the host's, taken from the record
        `_write_dependency_graph` wrote. Reading `deps.yaml` here instead would resolve the
        same fact by a second rule — by bare id, first-wins, with no version constraint — and
        the two could name different catalog entries."""
        graph_path = self.repo / self.refs.ir_ref / "dependency_graph.json"
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
        graph["profiles"] = []
        graph_path.write_text(json.dumps(graph, indent=2), encoding="utf-8")
        # `deps.yaml` still declares `demo_profile`; the sidecar says none is adopted.
        deps = yaml.safe_load(
            (self.repo / self.refs.spec_path / "deps.yaml").read_text(encoding="utf-8"))
        self.assertTrue(deps["dependencies"]["profiles"])
        c = self.conductor()
        doc = c._build_pure_compile_context(self.refs)["profile_spec_document"]
        self.assertEqual(doc, wc.Conductor._PURE_PROFILE_ABSENT_DOCUMENT)
        self.assertNotIn("demo_profile", doc)

    def test_a_sidecar_with_no_profiles_record_is_a_recoverable_assembly_failure(self) -> None:
        # Not a silent empty document: the key is declared, and a blind prompt would ask the
        # producer to invent the constraints its algorithm must honour.
        graph_path = self.repo / self.refs.ir_ref / "dependency_graph.json"
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
        del graph["profiles"]
        graph_path.write_text(json.dumps(graph, indent=2), encoding="utf-8")
        c = self.conductor()
        with self.assertRaises(RuntimeError) as caught:
            c._build_pure_compile_context(self.refs)
        self.assertIn("pure_profile_spec_document_missing", str(caught.exception))

    def test_the_absent_document_sentinel_is_what_the_launch_validator_requires(self) -> None:
        """`profile_spec_document` is a DECLARED context key, and
        `_validate_pure_launch_request_payload` refuses a declared key that is empty — inside
        `record_launch`, from a call the pure loop does not guard, so the `ValueError` would
        escape the loop's named `pure_context_assembly_failed` branch and abort the conductor.
        The sentinel's non-emptiness is therefore the only thing between EVERY profile-less
        node — every `component` and `infrastructure` spec in this tree, the majority path —
        and that abort. A round-5 census found the property claimed as pinned in a comment and
        pinned by nothing: emptying the sentinel left 2477 tests green."""
        self.assertTrue(wc.Conductor._PURE_PROFILE_ABSENT_DOCUMENT.strip())
        graph_path = self.repo / self.refs.ir_ref / "dependency_graph.json"
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
        graph["profiles"] = []
        graph_path.write_text(json.dumps(graph, indent=2), encoding="utf-8")
        ctx = self.conductor()._build_pure_compile_context(self.refs)
        self.assertEqual(ctx["profile_spec_document"],
                         wc.Conductor._PURE_PROFILE_ABSENT_DOCUMENT)
        # Driven through the real validator, not asserted about: this is the frame that refuses.
        from tools.pure_leaf import PURE_PROMPT_CONTRACT_VERSION
        base = {"agent_role": "substep", "step": "compile", "substep": "generate",
                "leaf_mode": ort.PURE_LEAF_MODE,
                "prompt_contract_version": PURE_PROMPT_CONTRACT_VERSION}
        ort._validate_pure_launch_request_payload({**base, "pure_context": ctx})
        with self.assertRaises(ValueError) as caught:
            ort._validate_pure_launch_request_payload(
                {**base, "pure_context": {**ctx, "profile_spec_document": ""}})
        self.assertIn("profile_spec_document", str(caught.exception))

    def test_only_a_WARM_reuse_repair_is_exempt_from_carrying_pure_context(self) -> None:
        """The exemption is a THREE-term conjunction (`warm and reuse and findings`), and issue
        #209 makes `(cold, reuse, findings)` a production shape for the first time: an outer
        reopen on a provider with no warm resume now renders a repair turn that MUST re-inline
        the context, because nothing holds it. A round-1 security axis relaxed the conjunction to
        `not (reuse and findings)` and every test file naming `pure_context` stayed green (nine,
        enumerated with `git ls-files`, measured at 1ad8e3b4^) — so the term that keeps a
        cold repair from shipping without its context documents was pinned by nothing.

        Driven through the validator on all three shapes, so the exemption is measured at the
        frame that grants it rather than asserted about.
        """
        from tools.pure_leaf import PURE_PROMPT_CONTRACT_VERSION
        ctx = self.conductor()._build_pure_compile_context(self.refs)
        base = {"agent_role": "substep", "step": "compile", "substep": "generate",
                "leaf_mode": ort.PURE_LEAF_MODE,
                "prompt_contract_version": PURE_PROMPT_CONTRACT_VERSION,
                "repair_strategy": "reuse", "repair_findings": "a static gate finding"}
        # Warm: the resumed session holds the context, so omitting it is accepted.
        ort._validate_pure_launch_request_payload({**base, "warm_resume": True})
        # Cold, same reuse repair with the same findings: refused. This is #209's own shape.
        with self.assertRaises(ValueError) as caught:
            ort._validate_pure_launch_request_payload({**base, "warm_resume": False})
        self.assertIn("pure_context", str(caught.exception))
        # ... and accepted once the context rides along, which is what the loop sends.
        ort._validate_pure_launch_request_payload(
            {**base, "warm_resume": False, "pure_context": ctx})

    def test_an_unresolvable_profile_is_named_rather_than_failing_the_substep(self) -> None:
        (self.repo / "spec/profile/demo/demo_profile/controlled_spec.md").unlink()
        c = self.conductor()
        doc = c._build_pure_compile_context(self.refs)["profile_spec_document"]
        self.assertIn("demo_profile", doc)
        self.assertIn("does not resolve", doc)


# ======================================================================================
# `_pure_ir_document_violations`
# ======================================================================================
class PureIrDocumentViolationTests(_Fixture):
    def violations(self, doc, refs=None):
        return self.conductor()._pure_ir_document_violations(refs or self.refs, doc)

    def test_a_clean_document_has_no_violations(self) -> None:
        self.assertIsNone(self.violations(_doc()))

    def test_a_declared_fail_has_no_violations_and_needs_no_ir(self) -> None:
        self.assertIsNone(self.violations(_declared()))

    def test_each_shape_defect_is_one_ir_document_violation(self) -> None:
        for label, doc in (
            ("not an object", ["ir"]),
            ("extra key", dict(_doc(), notes="hello")),
            ("missing key", {"ir": _valid_ir()}),
            ("the old key", {"ir": None, "last_fail_reason": "tests.md is empty"}),
            # The old key BESIDE the new envelope: an allowed-key set that kept it would accept
            # the reply and drop the value unread.
            ("the old key beside the new", dict(_doc(), last_fail_reason="tests.md is empty")),
            # The family must STRADDLE the `isinstance(ir, dict)` guard, and it did not: a
            # STRING is iterable, so the section check below it (`k not in ir`, a substring
            # test) produces a violation anyway and the guard could be deleted with this row
            # green. A value that is NOT iterable is what reaches the guard — and without it
            # the section check raises `TypeError`, which escapes `run_substep` uncaught and
            # kills the conductor mid-run rather than costing one repair turn.
            ("ir not a mapping (iterable)", {"ir": "schema_version: 1.0",
                                             "compile_fail": None}),
            ("ir not a mapping (a list)", {"ir": ["schema_version"], "compile_fail": None}),
            ("ir is null", {"ir": None, "compile_fail": None}),
            ("ir is a number", {"ir": 5, "compile_fail": None}),
            ("ir is a bool", {"ir": True, "compile_fail": None}),
        ):
            with self.subTest(shape=label):
                result = self.violations(doc)
                self.assertIsNotNone(result, label)
                self.assertEqual(result[0], wc.COMPILE_IR_DOCUMENT_VIOLATION)
                self.assertTrue(result[1].strip())

    def test_each_required_section_is_killed_one_at_a_time(self) -> None:
        """One probe per element of the floor, not one probe deleting all of them: a missing
        element of an enumeration is invisible when the enumeration is checked together."""
        required = list(wc.Conductor._PURE_IR_REQUIRED_SECTIONS) + ["public_api"]
        for section in required:
            with self.subTest(section=section):
                ir = _valid_ir()
                del ir[section]
                result = self.violations(_doc(ir))
                self.assertIsNotNone(result, section)
                self.assertIn(section, result[1])

    def test_public_api_is_required_by_the_nodes_kind_not_by_its_own_meta(self) -> None:
        """The node's kind comes from `refs`. A leaf that could choose its own key requirement —
        by writing `meta.spec_kind: problem` into the document — would be choosing which floor it
        is held to, which is the `leaf shortcut` class this floor exists inside."""
        ir = _valid_ir()
        del ir["public_api"]
        ir["meta"]["spec_kind"] = "problem"
        result = self.violations(_doc(ir))
        self.assertIsNotNone(result)
        self.assertIn("public_api", result[1])

    def test_a_document_may_not_declare_a_kind_other_than_the_nodes_own(self) -> None:
        """The other half of the row above, and the half that was missing: requiring the KEY by
        the node's kind while leaving the VALUE leaf-chosen closes half a door.

        `meta.spec_kind` is not a description. `--stage compile`'s published-surface gate reads
        the kind from THIS field and returns without checking anything when it names a kind that
        publishes no surface — so a `component` document declaring `problem` carries an EMPTY
        `public_api` past V8 with the whole §5 / §5.1 transcription unchecked, and
        `Generate.static` (which resolves the kind from the node_key instead) then fail-closes
        and routes the finding to a substep that cannot edit the IR.

        A leaf taking it skips rule 8 of its own prompt — every `operation_id`, published type,
        structured signature and module parameter copied verbatim — and still reports the
        substep done behind a green Compile phase.
        """
        for declared in ("problem", "profile", "", None, "Component"):
            with self.subTest(declared=declared):
                ir = _valid_ir()
                if declared is None:
                    del ir["meta"]["spec_kind"]
                else:
                    ir["meta"]["spec_kind"] = declared
                result = self.violations(_doc(ir))
                self.assertIsNotNone(result, declared)
                self.assertEqual(result[0], wc.COMPILE_IR_DOCUMENT_VIOLATION)
                self.assertIn("spec_kind", result[1])
        # ...and the node's own kind is accepted, so this is not a blanket refusal — including
        # PADDED, which every reader of this field resolves with `.strip()` and no case folding
        # (`_validate_published_surface`, `_m3c_language`, `_conductor_authors_runner`). Refusing a spelling those readers agree on would be an
        # over-refusal; `"Component"` above is refused because they do NOT case-fold.
        self.assertIsNone(self.violations(_doc()))
        padded = _valid_ir()
        padded["meta"]["spec_kind"] = "  component  "
        self.assertIsNone(self.violations(_doc(padded)))

    def test_a_non_mapping_meta_is_a_violation_rather_than_a_crash(self) -> None:
        """`meta` reaches the kind check, which must not assume the earlier presence check said
        anything about its TYPE."""
        for meta in (None, "component", ["component"], 5):
            with self.subTest(meta=meta):
                ir = _valid_ir()
                ir["meta"] = meta
                result = self.violations(_doc(ir))
                self.assertIsNotNone(result, meta)
                self.assertEqual(result[0], wc.COMPILE_IR_DOCUMENT_VIOLATION)

    def test_a_problem_node_is_not_asked_for_public_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _write_compile_node(repo, kind="problem")
            c = _conductor(repo)
            ir = _valid_ir("problem")
            self.assertIsNone(c._pure_ir_document_violations(refs, {"ir": ir,
                                                                    "compile_fail": None}))

    def test_an_impl_defaults_section_is_refused(self) -> None:
        """The IR is target-free (issue #284, R4-a PR-3): a document that still carries the
        section the target and the lowering knobs lived in is a repairable violation naming it,
        so no certified IR records a target choice nothing reads."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _write_compile_node(repo)
            c = _conductor(repo)
            ir = _valid_ir()
            ir["impl_defaults"] = {"toolchain": {"language": "x"}}
            found = c._pure_ir_document_violations(refs, {"ir": ir, "compile_fail": None})
            self.assertIsNotNone(found)
            assert found is not None
            self.assertEqual(found[0], wc.COMPILE_IR_DOCUMENT_VIOLATION)
            self.assertIn("impl_defaults", found[1])
            self.assertIn("target-free", found[1])
            self.assertEqual(wc.Conductor._PURE_IR_FORBIDDEN_SECTIONS, ("impl_defaults",))
            self.assertNotIn("impl_defaults", wc.Conductor._PURE_IR_REQUIRED_SECTIONS)

    def test_the_round_trip_probe_accepts_an_adversarial_json_document(self) -> None:
        """The OVER-REFUSAL direction of the serialize/parse probe, which is the direction that
        matters here: the values a JSON document can carry are exactly the ones `yaml.safe_dump`
        quotes when their bare form would re-read as another type, so the probe must pass every
        one of them. A regression that made it refuse would fail every compile attempt.

        NOT PINNED, and said rather than implied: the violating direction has no witness. Every
        value `json.loads` can produce survives `safe_dump` -> `safe_load` on this loader, so the
        branch is a fail-closed defence against a future serializer change, not a reachable
        classification. `atmofab-enforcement-change` rule 1-b is why it stays.
        """
        ir = _valid_ir()
        ir["algorithm"]["temporaries"] = [
            {"name": "no", "shape_expr": "scalar"},
            {"name": "yes", "shape_expr": "[1.0]"},
            {"name": "null", "shape_expr": "~"},
            {"name": "on", "shape_expr": "0x10"},
        ]
        ir["io_contract"]["notes"] = {"on": "yes", "off": "no", "n": "1_000"}
        ir["meta"]["notes"] = "  leading and trailing  "
        self.assertIsNone(self.violations(_doc(ir)))

    def test_an_unencodable_document_is_a_repairable_violation_not_a_crash(self) -> None:
        """The UTF-8 probe in the producer loop. `json.loads` accepts a lone surrogate, and
        writing it raises — so it must be classified as a document defect before the write."""
        c = self.conductor(_envelope(json.dumps(_doc()).replace(
            '"compile_fail": null', '"compile_fail": null, "x": "\\ud800"')))
        outcome = c.run_substep(self.refs, "compile", "generate")
        meta = json.loads((self.repo / self.refs.ir_ref
                           / "compile_generate_meta.json").read_text(encoding="utf-8"))
        self.assertEqual(outcome.status, "fail")
        self.assertEqual(meta["failure_category"], wc.COMPILE_IR_DOCUMENT_VIOLATION)


# ======================================================================================
# The producer loop, bound to the compile spec
# ======================================================================================
class PureCompileProducerTests(_Fixture):
    def test_a_clean_document_is_written_by_the_host_after_the_window_closes(self) -> None:
        c = self.conductor(_envelope(_doc()), _envelope(_part()))
        outcome = c.run_substep(self.refs, "compile", "generate")
        self.assertEqual(outcome.status, "pass")
        # The leaf holds no write authority, so its terminal row carries no output_refs and
        # explains itself through the summary instead.
        self.assertEqual(outcome.output_refs, [])
        ir_dir = self.repo / self.refs.ir_ref
        self.assertEqual(yaml.safe_load((ir_dir / "spec.ir.yaml").read_text(encoding="utf-8")),
                         _assembled_ir())
        meta = json.loads((ir_dir / "ir_meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["verification_status"], "pending")
        self.assertEqual(meta["ir_id"], self.refs.ir_id)
        self.assertEqual(meta["node_key"], _NODE)
        self.assertIsNone(meta["last_fail_reason"])
        self.assertTrue(meta["context_isolated"])
        gmeta = json.loads((ir_dir / "compile_generate_meta.json").read_text(encoding="utf-8"))
        self.assertEqual(gmeta["result"], "pass")
        # One attempt per loop: the core and the one part.
        self.assertEqual(gmeta["attempts"], 2)
        self.assertEqual(meta["attempt_count"], 2)
        self.assertEqual(gmeta["prompt_contract_version"], ort.PURE_PROMPT_CONTRACT_VERSION)

    def test_the_pending_status_is_not_read_as_a_certified_dependency(self) -> None:
        """`"pending"` satisfies the meta contract's non-empty-string requirement and is not
        `"pass"`, which is what every dependency-certification reader compares against."""
        c = self.conductor(_envelope(_doc()), _envelope(_part()))
        c.run_substep(self.refs, "compile", "generate")
        meta = json.loads((self.repo / self.refs.ir_ref
                           / "ir_meta.json").read_text(encoding="utf-8"))
        self.assertNotEqual(meta["verification_status"], "pass")

    def test_the_host_written_meta_satisfies_the_stage_meta_contract(self) -> None:
        """The five keys and their types, checked by the contract module itself rather than by a
        list transcribed here — `_stage_meta_contract_findings` runs the same functions inside
        `classify_failure`, so a meta that fails them escalates every compile failure."""
        import tools.meta_contracts as mc
        c = self.conductor(_envelope(_doc()), _envelope(_part()))
        c.run_substep(self.refs, "compile", "generate")
        meta = json.loads((self.repo / self.refs.ir_ref
                           / "ir_meta.json").read_text(encoding="utf-8"))
        self.assertEqual(mc.missing_required_meta_keys(meta, step_token="compile"), [])
        self.assertEqual(mc.stage_meta_type_violations(meta, step_token="compile"), [])

    def test_the_request_is_a_pure_launch_carrying_the_dependency_surface(self) -> None:
        c = self.conductor(_envelope(_doc()))
        surface = ({"node_key": _NODE, "published_operations": [f"{_SPEC_ID}__apply"],
                    "source": "ir_public_api"},)
        c.run_substep(self.refs, "compile", "generate", dependency_surface=surface)
        req = [cap["--request-json"] for s, cap in c.calls if s == "record-launch"][0]
        self.assertEqual(req["leaf_mode"], "pure")
        self.assertEqual(req["allowed_output_paths"], [])
        self.assertEqual(req["skill_must_read_refs"], "")
        self.assertEqual(req["dependency_surface"], list(surface))
        self.assertEqual(set(req["pure_context"]),
                         set(ort.PURE_CONTEXT_REQUIRED_KEYS[("compile", "generate")]))

    def test_a_document_violation_is_repaired_within_budget(self) -> None:
        c = self.conductor(_envelope({"ir": _valid_ir()}), _envelope(_doc()),
                           _envelope(_part()))
        outcome = c.run_substep(self.refs, "compile", "generate")
        self.assertEqual(outcome.status, "pass")
        # Two core attempts and the part's one.
        self.assertEqual(outcome.attempts, 3)
        gmeta = json.loads((self.repo / self.refs.ir_ref
                            / "compile_generate_meta.json").read_text(encoding="utf-8"))
        self.assertEqual(gmeta["result"], "pass")
        self.assertEqual(gmeta["per_attempt"][0]["failure_category"],
                         wc.COMPILE_IR_DOCUMENT_VIOLATION)
        repair = [cap["--request-json"] for s, cap in c.calls if s == "record-launch"][1]
        self.assertEqual(repair["repair_reason"], "pure_ir_document_repair")

    def test_exhaustion_routes_back_to_the_producer_with_its_excerpt(self) -> None:
        c = self.conductor(_envelope({"ir": _valid_ir()}))
        outcome = c.run_substep(self.refs, "compile", "generate")
        self.assertEqual(outcome.status, "fail")
        gmeta = json.loads((self.repo / self.refs.ir_ref
                            / "compile_generate_meta.json").read_text(encoding="utf-8"))
        self.assertEqual(gmeta["result"], "fail")
        self.assertEqual(gmeta["failure_category"], wc.COMPILE_IR_DOCUMENT_VIOLATION)
        decision = c.classify_failure(self.refs, "compile", [outcome])
        self.assertEqual(decision.action, "retry")
        self.assertEqual(decision.target_phase, "compile")
        self.assertEqual(decision.repair_strategy, "reuse")
        self.assertEqual(
            decision.reason,
            f"{wc.COMPILE_DOCUMENT_REASON_PREFIX}{wc.COMPILE_IR_DOCUMENT_VIOLATION}")
        self.assertTrue(c._read_repair_findings(self.refs, decision.reason, "compile"))

    def test_a_declared_compile_fail_is_the_third_exit(self) -> None:
        """Neither a pass nor a repairable defect: the leaf answered that the phase cannot be
        completed from its inputs. rc stays 0 — nothing crashed — and the routing declines the
        document table and terminalizes it fail_closed in both modes (issue #355)."""
        doc = _declared("tests", "§6", "declares no test_id, so the verification contract "
                                       "cannot be derived")
        reason = _declared_reason(doc)
        self.assertEqual(reason, "tests §6: declares no test_id, so the verification contract "
                                 "cannot be derived")
        c = self.conductor(_envelope(doc))
        events: list = []
        c.emit = lambda ev, **f: events.append((ev, f))  # type: ignore[assignment]
        outcome = c.run_substep(self.refs, "compile", "generate")
        self.assertEqual(outcome.status, "fail")
        self.assertEqual(outcome.leaf_returncode, 0)
        ir_dir = self.repo / self.refs.ir_ref
        self.assertFalse((ir_dir / "spec.ir.yaml").exists())
        meta = json.loads((ir_dir / "ir_meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["verification_status"], "fail")
        self.assertEqual(meta["issue_severity"], "major")
        self.assertEqual(meta["last_fail_reason"], reason)
        gmeta = json.loads((ir_dir / "compile_generate_meta.json").read_text(encoding="utf-8"))
        self.assertEqual(gmeta["failure_category"], wc.COMPILE_DECLARED_FAIL)
        self.assertNotIn(wc.COMPILE_DECLARED_FAIL, wc.COMPILE_DOCUMENT_FAILURE_ROUTING)
        self.assertIn("pure_compile_fail_declared", [e for e, _ in events])
        # The leaf ANSWERED; it did not fail an attempt. The bookkeeping the loop runs for a
        # failed attempt is guarded on `category`, not on `status`, precisely so this exit does
        # not run it — and both halves of that are asserted here because the guard change is
        # what distinguishes the two predicates, and nothing else in the suite can tell them
        # apart (for the generate pair they are the same predicate).
        self.assertNotIn("pure_ir_document_attempt_failed", [e for e, _ in events])
        self.assertEqual(len(gmeta["per_attempt"]), 1)
        self.assertNotIn("failure_category", gmeta["per_attempt"][0])
        self.assertNotIn("failure_excerpt", gmeta["per_attempt"][0])
        # ...and the finalized agent_run row says `fail`, not `pass`. Its `result_summary`
        # already says `pure_compile_fail`, so a row recording `pass` would contradict the one
        # field a pure row has to explain itself with.
        row = _finalized(c)[-1]
        self.assertEqual(row["status"], "fail")

    def test_a_declared_compile_fail_is_terminal_in_both_modes(self) -> None:
        """The declaration never reaches the verify-severity gate, whose prod answer is an
        escalation that could hand the declaration back to the leaf as repair findings
        (issue #355). The facts stay where a repair read would find them — which is what makes
        the terminal route, not an empty read, the thing that keeps them out of a repair."""
        doc = _declared("deps", "deps", "names a dependency the registry does not carry")
        reason = _declared_reason(doc)
        c = self.conductor(_envelope(doc))
        outcome = c.run_substep(self.refs, "compile", "generate")
        for mode in ("dev", "prod"):
            with self.subTest(mode=mode):
                c.workflow_mode = mode
                decision = c.classify_failure(self.refs, "compile", [outcome])
                self.assertEqual(
                    (decision.action, decision.reason, decision.target_phase,
                     decision.repair_strategy),
                    ("fail_closed", wc.COMPILE_DECLARED_FAIL, None, None))
        self.assertIsNone(
            c._read_repair_findings(self.refs, wc.COMPILE_DECLARED_FAIL, "compile"))
        meta = json.loads((self.repo / self.refs.ir_ref / "ir_meta.json")
                          .read_text(encoding="utf-8"))
        self.assertEqual(meta["last_fail_reason"], reason)
        self.assertEqual(
            c._read_repair_findings(self.refs, "verify_severity_major", "compile"), reason)

    def test_a_transport_death_is_not_a_document_defect(self) -> None:
        c = self.conductor(_envelope(_doc()))
        c.spawn_leaf = lambda *a, **k: wc.ProcResult(  # type: ignore[assignment]
            1, "", "API Error: 500")
        outcome = c.run_substep(self.refs, "compile", "generate")
        self.assertEqual(outcome.status, "fail")
        self.assertNotEqual(outcome.leaf_returncode, 0)
        gmeta = json.loads((self.repo / self.refs.ir_ref
                            / "compile_generate_meta.json").read_text(encoding="utf-8"))
        self.assertEqual(gmeta["failure_category"], "pure_transport")

    def test_a_host_write_failure_recovers_as_a_transport_fail_closed(self) -> None:
        c = self.conductor(_envelope(_doc()), _envelope(_part()))

        def boom(*a, **k):
            raise OSError("no space left on device")

        c._write_pure_ir_artifacts = boom  # type: ignore[assignment]
        outcome = c.run_substep(self.refs, "compile", "generate")
        self.assertEqual(outcome.status, "fail")
        self.assertEqual(outcome.leaf_returncode, 1)
        self.assertEqual(outcome.infra_error[0], "pure_compile_host_write_failed")


# ======================================================================================
# The reviewer loop, bound to the compile spec
# ======================================================================================
class PureCompileReviewerTests(_Fixture):
    STAGE_IR = True

    def test_a_pass_verdict_is_projected_onto_the_ir_meta(self) -> None:
        c = self.conductor(_envelope(_verdict()))
        outcome = c.run_substep(self.refs, "compile", "verify")
        self.assertEqual(outcome.status, "pass")
        ir_dir = self.repo / self.refs.ir_ref
        meta = json.loads((ir_dir / "ir_meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["verification_status"], "pass")
        self.assertEqual(meta["issue_severity"], "none")
        self.assertIsNone(meta["last_fail_reason"])
        self.assertEqual(meta["ir_id"], self.refs.ir_id)
        vmeta = json.loads((ir_dir / "compile_verify_meta.json").read_text(encoding="utf-8"))
        self.assertEqual(vmeta["result"], "pass")

    def test_a_fail_verdict_reaches_the_severity_gate_with_its_reason(self) -> None:
        verdict = _verdict("fail", "minor", "step_03 has no update target")
        verdict["findings"] = [{"summary": "an item is unmet"},
                               {"summary": "V3 misses the snapshot of h"}]
        c = self.conductor(_envelope(verdict))
        outcome = c.run_substep(self.refs, "compile", "verify")
        self.assertEqual(outcome.status, "fail")
        meta = json.loads((self.repo / self.refs.ir_ref
                           / "ir_meta.json").read_text(encoding="utf-8"))
        # `verification_status` first, and it is the load-bearing one: the compile phase's own
        # routing runs off `issue_severity`, so a projection that wrote `"pass"` here would
        # change no verdict IN the run — and `ir_meta.json#verification_status` is the SOLE
        # certification carrier a downstream consumer reads (`_dep_ir_meta_passes` compares it
        # against `"pass"` with no cross-check against a passing step_result). A rejected IR
        # would publish as certified. The generate-side twin pins this; this row did not.
        self.assertEqual(meta["verification_status"], "fail")
        self.assertEqual(meta["issue_severity"], "minor")
        # The reason, then every finding's summary (`verify_repair_text`, issue #347).
        self.assertEqual(meta["last_fail_reason"],
                         "step_03 has no update target\n1. an item is unmet"
                         "\n2. V3 misses the snapshot of h")
        decision = c.classify_failure(self.refs, "compile", [None, None, outcome])
        # `minor` is the verify-severity gate's warm same-phase repair. `target_phase` is None
        # there BY DESIGN — the caller reopens the phase it is already in — and the reason is what
        # selects the meta the findings come from.
        self.assertEqual(decision.action, "retry")
        self.assertEqual(decision.repair_strategy, "reuse")
        self.assertEqual(decision.reason, "verify_minor")
        self.assertEqual(
            c._read_repair_findings(self.refs, decision.reason, "compile"),
            "step_03 has no update target\n1. an item is unmet"
            "\n2. V3 misses the snapshot of h")

    def test_a_major_fail_verdict_reaches_the_severity_gate_in_both_modes(self) -> None:
        """A `Compile.verify` `major` is the one compile route the verify-severity gate still
        grades above `minor`: dev terminalizes, prod escalates. Until issue #355 the declared
        compile fail took this gate too, and its row was the only one that drove a compile
        `major` through `classify_failure`; the declaration now stops before the gate, so this
        row keeps the gate's compile branch pinned."""
        verdict = _verdict("fail", "major", "controlled_spec.md names no east boundary")
        c = self.conductor(_envelope(verdict))
        outcome = c.run_substep(self.refs, "compile", "verify")
        self.assertEqual(outcome.status, "fail")
        for mode, expected in (("dev", ("fail_closed", "dev_verify_major")),
                               ("prod", ("escalate", "verify_severity_major"))):
            with self.subTest(mode=mode):
                c.workflow_mode = mode
                decision = c.classify_failure(self.refs, "compile", [None, None, outcome])
                self.assertEqual((decision.action, decision.reason), expected)

    def test_a_schema_exhausted_verdict_writes_no_projection_and_restarts(self) -> None:
        """Proof-of-work: no valid verdict, no stage meta. The producer's `"pending"` therefore
        survives, which is what tells a later reader the reviewer never answered."""
        c = self.conductor(_envelope({"verification_status": "maybe"}))
        c._write_ir_meta(self.refs, verification_status="pending", last_fail_reason=None,
                         issue_severity=None, attempts=1)
        outcome = c.run_substep(self.refs, "compile", "verify")
        self.assertEqual(outcome.status, "fail")
        ir_dir = self.repo / self.refs.ir_ref
        meta = json.loads((ir_dir / "ir_meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["verification_status"], "pending")
        vmeta = json.loads((ir_dir / "compile_verify_meta.json").read_text(encoding="utf-8"))
        self.assertEqual(vmeta["failure_category"], wc.GENERATE_VERDICT_SCHEMA_VIOLATION)
        decision = c.classify_failure(self.refs, "compile", [None, None, outcome])
        self.assertEqual(decision.action, "retry")
        self.assertEqual(decision.repair_strategy, "restart")
        self.assertEqual(
            decision.reason,
            f"{wc.COMPILE_VERDICT_REASON_PREFIX}{wc.GENERATE_VERDICT_SCHEMA_VIOLATION}")

    def test_the_reviewer_request_carries_the_ir_under_review(self) -> None:
        c = self.conductor(_envelope(_verdict()))
        c.run_substep(self.refs, "compile", "verify")
        req = [cap["--request-json"] for s, cap in c.calls if s == "record-launch"][0]
        self.assertEqual(req["leaf_mode"], "pure")
        self.assertEqual(req["allowed_output_paths"], [])
        self.assertIn("schema_version", req["pure_context"]["ir_document"])

    def test_a_host_write_failure_recovers_as_a_transport_fail_closed(self) -> None:
        c = self.conductor(_envelope(_verdict()))

        def boom(*a, **k):
            raise OSError("no space left on device")

        c._write_verify_ir_meta = boom  # type: ignore[assignment]
        outcome = c.run_substep(self.refs, "compile", "verify")
        self.assertEqual(outcome.status, "fail")
        self.assertEqual(outcome.infra_error[0], "pure_compile_verify_host_write_failed")


# ======================================================================================
# The values the SPEC record decides
# ======================================================================================
class PureCompileSpecValueTests(_Fixture):
    """One row per field of `_PureProducerSpec` / `_PureReviewerSpec` whose value reaches an
    artifact, an event or a record.

    These exist because the round-0 mutation sweep found them unpinned as a CLASS: reverting a
    hunk that replaced a generate literal with `spec.<field>` restores the GENERATE value, which
    the generate tests still accept and nothing on the compile side was looking at. Fourteen
    hunks survived that way. A wrong value here is not cosmetic — the summary is the only thing a
    pure terminal row can say for itself (`_validate_agent_summary_text` requires it), the emit
    is what an operator greps for, and the tombstone reason is the audit trail of a superseded
    attempt.
    """

    def test_the_producer_pass_summary_names_the_compile_artifact(self) -> None:
        c = self.conductor(_envelope(_doc()), _envelope(_part()))
        c.run_substep(self.refs, "compile", "generate")
        rows = _finalized(c)
        self.assertEqual([r["status"] for r in rows], ["pass", "pass"])
        self.assertEqual([r["result_summary"] for r in rows],
                         ["pure_compile_pass: IR core accepted (attempts=1)",
                          f"pure_compile_pass: verification part {_TEST_ID} accepted "
                          "(attempts=1)"])

    def test_the_producer_failure_summary_and_event_name_the_compile_pair(self) -> None:
        c = self.conductor(_envelope({"ir": _valid_ir()}))
        events: list = []
        c.emit = (  # type: ignore[assignment]
            lambda ev, _sink=events, **f: _sink.append((ev, f)))
        c.run_substep(self.refs, "compile", "generate")
        self.assertEqual(_finalized(c)[-1]["result_summary"],
                         f"pure_compile_fail: {wc.COMPILE_IR_DOCUMENT_VIOLATION}")
        self.assertIn("pure_ir_document_attempt_failed", [e for e, _ in events])
        self.assertNotIn("pure_bundle_attempt_failed", [e for e, _ in events])

    def test_the_producer_declared_fail_summary_carries_the_token_and_the_reason(self) -> None:
        doc = _declared()
        reason = _declared_reason(doc)
        c = self.conductor(_envelope(doc))
        c.run_substep(self.refs, "compile", "generate")
        self.assertEqual(
            _finalized(c)[-1]["result_summary"],
            f"pure_compile_fail: {wc.COMPILE_DECLARED_FAIL}: {reason}")

    def test_the_producer_carries_no_exemplar(self) -> None:
        """`wants_exemplar=False`. `_resolve_exemplar` reads an IR that does not exist yet at
        `compile.generate` time, and `build_launch_request` attaches an exemplar only to
        `(generate, generate)` — so the request is where the property is observable."""
        c = self.conductor(_envelope(_doc()))
        c.run_substep(self.refs, "compile", "generate")
        req = [cap["--request-json"] for s, cap in c.calls if s == "record-launch"][0]
        self.assertNotIn("exemplar", req)


class PureCompileReviewerSpecValueTests(_Fixture):
    STAGE_IR = True

    def test_the_reviewer_pass_summary_names_the_compile_reviewer(self) -> None:
        c = self.conductor(_envelope(_verdict()))
        c.run_substep(self.refs, "compile", "verify")
        self.assertEqual(
            _finalized(c)[-1]["result_summary"],
            "pure_compile_verify_pass: verdict pass (severity=none, attempts=1)")

    def test_the_reviewer_fail_summary_carries_the_verdicts_reason(self) -> None:
        c = self.conductor(_envelope(_verdict("fail", "minor", "step_03 has no update target")))
        c.run_substep(self.refs, "compile", "verify")
        self.assertEqual(_finalized(c)[-1]["result_summary"],
                         "pure_compile_verify_fail: step_03 has no update target")

    def test_the_reviewer_malformed_reply_names_the_compile_pair_everywhere(self) -> None:
        c = self.conductor(_envelope({"verification_status": "maybe"}))
        events: list = []
        c.emit = (  # type: ignore[assignment]
            lambda ev, _sink=events, **f: _sink.append((ev, f)))
        c.run_substep(self.refs, "compile", "verify")
        self.assertEqual(
            _finalized(c)[-1]["result_summary"],
            f"pure_compile_verify_fail: {wc.GENERATE_VERDICT_SCHEMA_VIOLATION}")
        self.assertIn("pure_ir_verdict_attempt_failed", [e for e, _ in events])
        self.assertNotIn("pure_verdict_attempt_failed", [e for e, _ in events])

    def test_a_non_object_reply_says_a_verdict_was_expected(self) -> None:
        """`non_object_findings` reaches the repair turn's `repair_findings`, which is the only
        place the reviewer is told what shape it got wrong."""
        c = self.conductor(_envelope("[1, 2, 3]"), _envelope(_verdict()))
        outcome = c.run_substep(self.refs, "compile", "verify")
        self.assertEqual(outcome.status, "pass")
        repair = [cap["--request-json"] for s, cap in c.calls if s == "record-launch"][1]
        self.assertEqual(repair["repair_reason"], "pure_ir_verdict_repair")
        self.assertIn("expected a verdict", repair["repair_findings"])

# ======================================================================================
# The transport-shaped failure categories, on the COMPILE routing tables
# ======================================================================================
class PureCompileTransportCategoryTests(_Fixture):
    """`pure_response_unparseable` and `pure_response_truncated` are members of BOTH new routing
    tables and had no compile-side witness: the shared loop produces them, the generate tests
    observe them on the GENERATE tables, and nothing checked that the compile tables route them.
    A missing element of an enumeration is invisible when the enumeration is checked together,
    so there is one row per (category, table) and two end-to-end drives besides.

    Route: a provider that reports the reply was cut off, or a reply that is not one parseable
    JSON document, on `compile.generate` / `compile.verify`. Without the table entry the phase
    cold-restarts instead of warm-repairing, discarding a producer session that held the
    diagnosis — and on `compile.verify` it would reuse a session that cannot help.
    """

    def test_the_producer_table_routes_every_declared_category(self) -> None:
        from tools.pure_leaf import RESPONSE_TRUNCATED, RESPONSE_UNPARSEABLE
        self.assertEqual(
            set(wc.COMPILE_DOCUMENT_FAILURE_CATEGORIES),
            {RESPONSE_UNPARSEABLE, RESPONSE_TRUNCATED, wc.COMPILE_IR_DOCUMENT_VIOLATION},
            "the producer table's members are the shared loop's own categories plus this "
            "phase's document violation; a member added to one side and not the other is what "
            "this row exists to catch")
        for category in wc.COMPILE_DOCUMENT_FAILURE_CATEGORIES:
            with self.subTest(category=category):
                self.assertEqual(wc.COMPILE_DOCUMENT_FAILURE_ROUTING[category],
                                 ("compile", "reuse"))

    def test_the_reviewer_table_routes_every_declared_category(self) -> None:
        from tools.pure_leaf import RESPONSE_TRUNCATED, RESPONSE_UNPARSEABLE
        self.assertEqual(
            set(wc.COMPILE_VERDICT_FAILURE_CATEGORIES),
            {RESPONSE_UNPARSEABLE, RESPONSE_TRUNCATED, wc.GENERATE_VERDICT_SCHEMA_VIOLATION})
        for category in wc.COMPILE_VERDICT_FAILURE_CATEGORIES:
            with self.subTest(category=category):
                self.assertEqual(wc.COMPILE_VERDICT_FAILURE_ROUTING[category],
                                 ("compile", "restart"))

    def test_an_unparseable_producer_reply_is_recorded_and_routed(self) -> None:
        """Driven end to end, not read off the table: the loop must CLASSIFY the reply into the
        category the table then routes."""
        from tools.pure_leaf import RESPONSE_UNPARSEABLE
        c = self.conductor(_envelope("this reply is prose, not a document"))
        outcome = c.run_substep(self.refs, "compile", "generate")
        gmeta = json.loads((self.repo / self.refs.ir_ref
                            / "compile_generate_meta.json").read_text(encoding="utf-8"))
        self.assertEqual(gmeta["failure_category"], RESPONSE_UNPARSEABLE)
        decision = c.classify_failure(self.refs, "compile", [outcome])
        self.assertEqual((decision.action, decision.target_phase, decision.repair_strategy),
                         ("retry", "compile", "reuse"))
        self.assertEqual(decision.reason,
                         f"{wc.COMPILE_DOCUMENT_REASON_PREFIX}{RESPONSE_UNPARSEABLE}")

    def test_a_provider_reported_truncation_is_believed_over_the_document(self) -> None:
        """The one category no reply CONTENT can produce: the provider says the answer was cut
        off, and the loop must believe it over a partial document that happens to parse. The
        payload here is a COMPLETE, valid IR document, so a loop that read the document instead
        of the flag would pass and write it."""
        from tools.pure_leaf import RESPONSE_TRUNCATED
        c = self.conductor(_envelope(_doc()))
        env = _envelope(_doc())
        c.spawn_leaf = (  # type: ignore[assignment]
            lambda *a, _e=env, **k: wc.ProcResult(0, _e, "", response_truncated=True))
        outcome = c.run_substep(self.refs, "compile", "generate")
        self.assertEqual(outcome.status, "fail")
        self.assertFalse((self.repo / self.refs.ir_ref / "spec.ir.yaml").exists(),
                         "a truncated reply must not be written even when it parses")
        gmeta = json.loads((self.repo / self.refs.ir_ref
                            / "compile_generate_meta.json").read_text(encoding="utf-8"))
        self.assertEqual(gmeta["failure_category"], RESPONSE_TRUNCATED)
        decision = c.classify_failure(self.refs, "compile", [outcome])
        self.assertEqual(decision.reason,
                         f"{wc.COMPILE_DOCUMENT_REASON_PREFIX}{RESPONSE_TRUNCATED}")


# ======================================================================================
# The defensive freshness branch
# ======================================================================================
# ======================================================================================
# The structured declaration (issue #526)
# ======================================================================================
class PureCompileFailDeclarationTests(_Fixture):
    """A `compile_fail` names the input at fault, or it is not a declaration.

    The give-up this exists for (issue #526): three channel 0.2.5 attempts returned a
    `last_fail_reason` saying no input was missing or contradictory, and the host recorded each
    as a terminal spec-level failure. Now a declaration that names no inlined input is a
    document violation — a warm repair turn, then the producer-failure route."""

    def violation(self, compile_fail):
        return self.conductor()._pure_ir_document_violations(
            self.refs, {"ir": None, "compile_fail": compile_fail})

    def test_every_admissible_input_is_a_declaration(self) -> None:
        for name in wc.Conductor._compile_fail_inputs(""):
            with self.subTest(input=name):
                self.assertIsNone(self.violation({"input": name, "where": "§1", "what": "x"}))

    def test_each_malformed_declaration_is_one_document_violation(self) -> None:
        good = {"input": "tests", "where": "§6", "what": "declares no test_id"}
        family = (
            # The give-up the issue records, verbatim in shape: prose naming no input.
            ("a string", "no missing or contradictory input; the section is too large"),
            # STRADDLES the `isinstance(value, dict)` guard: a list of the three key names has
            # the right `set()`, so only the type test refuses it before `.get` would raise.
            ("a list of the key names", ["input", "where", "what"]),
            ("a number", 3),
            ("an empty object", {}),
            ("an extra key", dict(good, severity="major")),
            ("a missing key", {"input": "tests", "where": "§6"}),
            ("input names the model", dict(good, input="model")),
            ("input names the output", dict(good, input="ir")),
            ("input is empty", dict(good, input="")),
            ("input is a filename", dict(good, input="tests.md")),
            ("input is not a string", dict(good, input=["tests"])),
            ("where is empty", dict(good, where="  ")),
            ("what is empty", dict(good, what="")),
            ("what is not a string", dict(good, what=None)),
        )
        self.assertTrue(any(not isinstance(v, dict) and set(v) == set(good)
                            for _, v in family if isinstance(v, list)),
                        "the family must hold a non-mapping with the right key set")
        for label, value in family:
            with self.subTest(shape=label):
                result = self.violation(value)
                self.assertIsNotNone(result, label)
                self.assertEqual(result[0], wc.COMPILE_IR_DOCUMENT_VIOLATION)
                # The finding is an instruction that converges: it names the admissible inputs
                # and says the other answer is the document.
                self.assertIn("return the document", result[1])
                self.assertIn("do not restate that reason under an input's name", result[1])
                for name in wc.Conductor._compile_fail_inputs(""):
                    self.assertIn(f"`{name}`", result[1])

    def test_an_ir_beside_a_declaration_is_two_answers_not_a_declaration(self) -> None:
        """A hedge — the IR written out AND a declaration beside it — must not be recorded as a
        declared fail with the IR discarded: it is a document violation, and the repair turn
        asks for one answer. Every declaration shape the other rows accept is refused here."""
        for name in wc.Conductor._compile_fail_inputs(""):
            with self.subTest(input=name):
                result = self.conductor()._pure_ir_document_violations(
                    self.refs, {"ir": _valid_ir(),
                                "compile_fail": {"input": name, "where": "§6", "what": "x"}})
                self.assertIsNotNone(result)
                self.assertEqual(result[0], wc.COMPILE_IR_DOCUMENT_VIOLATION)
                self.assertIn("two different answers", result[1])
        doc = {"ir": _valid_ir(), "compile_fail": _declared()["compile_fail"]}
        c = self.conductor(_envelope(doc), _envelope(_doc()), _envelope(_part()))
        outcome = c.run_substep(self.refs, "compile", "generate")
        self.assertEqual((outcome.status, outcome.attempts), ("pass", 3))
        ir_dir = self.repo / self.refs.ir_ref
        meta = json.loads((ir_dir / "ir_meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["verification_status"], "pending")

    def test_a_give_up_is_repaired_into_a_document(self) -> None:
        give_up = {"ir": None, "compile_fail": "no input is missing; the IR is too large"}
        c = self.conductor(_envelope(give_up), _envelope(_doc()), _envelope(_part()))
        outcome = c.run_substep(self.refs, "compile", "generate")
        self.assertEqual(outcome.status, "pass")
        self.assertEqual(outcome.attempts, 3)
        ir_dir = self.repo / self.refs.ir_ref
        self.assertTrue((ir_dir / "spec.ir.yaml").exists())
        gmeta = json.loads((ir_dir / "compile_generate_meta.json").read_text(encoding="utf-8"))
        self.assertEqual(gmeta["per_attempt"][0]["failure_category"],
                         wc.COMPILE_IR_DOCUMENT_VIOLATION)
        repair = [cap["--request-json"] for s, cap in c.calls if s == "record-launch"][1]
        self.assertEqual(repair["repair_reason"], "pure_ir_document_repair")

    def test_a_persistent_give_up_is_a_producer_failure_not_a_declared_fail(self) -> None:
        """End to end: the repair budget runs out, the substep's terminal category is the
        document violation, and the router reopens `(compile, reuse)` — the route a retry takes
        — rather than `fail_closed` on `compile_declared_fail`. No `ir_meta.json` blames an
        input."""
        give_up = {"ir": None, "compile_fail": {"input": "none", "where": "-",
                                                "what": "the section is too large"}}
        c = self.conductor(_envelope(give_up))
        outcome = c.run_substep(self.refs, "compile", "generate")
        self.assertEqual(outcome.status, "fail")
        ir_dir = self.repo / self.refs.ir_ref
        gmeta = json.loads((ir_dir / "compile_generate_meta.json").read_text(encoding="utf-8"))
        self.assertEqual(gmeta["failure_category"], wc.COMPILE_IR_DOCUMENT_VIOLATION)
        self.assertEqual(gmeta["attempts"], 1 + ort_repair_turns())
        self.assertFalse((ir_dir / "ir_meta.json").exists())
        for mode in ("dev", "prod"):
            with self.subTest(mode=mode):
                c.workflow_mode = mode
                decision = c.classify_failure(self.refs, "compile", [outcome])
                self.assertEqual(
                    (decision.action, decision.target_phase, decision.repair_strategy),
                    ("retry", "compile", "reuse"))
                self.assertEqual(
                    decision.reason,
                    f"{wc.COMPILE_DOCUMENT_REASON_PREFIX}{wc.COMPILE_IR_DOCUMENT_VIOLATION}")

    def test_the_template_enumerates_exactly_the_admissible_inputs(self) -> None:
        """The list is the code's (`_compile_fail_inputs`); the template's sentence is read at
        its statement position and compared element by element, in order."""
        tpl = (Path(ort.__file__).resolve().parent / "prompt_templates"
               / "pure_compile_generate.txt").read_text(encoding="utf-8")
        head = ("the input at fault, which is one of these names and nothing else (they name "
                "the documents inlined below, in the order they appear there, then the "
                "catalog): ")
        self.assertEqual(tpl.count(head), 1)
        statement = tpl.split(head, 1)[1].split(";", 1)[0]
        self.assertLess(len(statement), 400, "the reader ran past the enumeration")
        listed = re.findall(r"`([a-z0-9_]+)`", statement)
        self.assertEqual(tuple(listed), wc.Conductor._compile_fail_inputs(""))
        # The names are not shown beside the documents they name, so the sentence tells the leaf
        # they follow the order of the inlined documents — which must then be true.
        slots = [k.removesuffix("_document")
                 for k in re.findall(r"^<(\w+_document)>$", tpl, flags=re.MULTILINE)]
        self.assertEqual(tuple(slots) + ("published_operations",), tuple(listed))
        self.assertLess(tpl.index("<checks_module_contract_document>"),
                        tpl.index("<dependency_facts>"))

    def test_the_inputs_are_the_inlined_documents_plus_the_catalog(self) -> None:
        names = wc.Conductor._compile_fail_inputs("")
        self.assertEqual(
            names[:-1],
            tuple(k.removesuffix("_document")
                  for k in ort.PURE_CONTEXT_REQUIRED_KEYS[("compile", "generate")]))
        self.assertEqual(names[-1], "published_operations")
        with self.assertRaises(ValueError):
            wc.Conductor._compile_fail_inputs("harness")


# ======================================================================================
# The verification parts (issue #526): a core loop, then one loop per test, then assembly
# ======================================================================================
def _requests(conductor) -> list[dict]:
    """The `--request-json` of every `record-launch`, in order."""
    return [cap["--request-json"] for s, cap in conductor.calls if s == "record-launch"]


def c_core_violations(case: "_Fixture", doc: dict):
    return case.conductor()._pure_ir_core_violations(case.refs, doc)


class PureCompilePartsTests(_Fixture):
    """The part machinery with ONE test per part: the bound is 0, so every test is a group of
    its own (`_verification_part_groups`). `PureCompilePartGroupTests` drives the grouping."""
    TESTS = ("t_alpha", "t_beta")

    def setUp(self) -> None:
        super().setUp()
        bound = mock.patch.object(wc.Conductor, "_VERIFICATION_PART_BUDGET_BYTES", 0)
        bound.start()
        self.addCleanup(bound.stop)
        (self.repo / self.refs.spec_path / "tests.md").write_text(
            "## 6. Tests\n\n### 6-1. `t_alpha`\n\nThe first judgment.\n\n"
            "### 6-2. `t_beta`\n\nThe second judgment.\n", encoding="utf-8")

    def meta(self) -> dict:
        return json.loads((self.repo / self.refs.ir_ref / "compile_generate_meta.json")
                          .read_text(encoding="utf-8"))

    def run_clean(self):
        c = self.conductor(_envelope(_doc()), *(_envelope(_part(t)) for t in self.TESTS))
        return c, c.run_substep(self.refs, "compile", "generate")

    def test_the_host_assembles_the_core_and_every_part_in_the_tests_order(self) -> None:
        c, outcome = self.run_clean()
        self.assertEqual((outcome.status, outcome.attempts), ("pass", 3))
        written = yaml.safe_load((self.repo / self.refs.ir_ref / "spec.ir.yaml")
                                 .read_text(encoding="utf-8"))
        self.assertEqual(written, _assembled_ir(*self.TESTS))
        # The schema block's order, not the order the lists were added in.
        self.assertEqual(list(written["io_contract"]),
                         ["inputs", "outputs", "test_evidence_requirements",
                          "test_predicates", "primary_predicates"])
        self.assertEqual([e["test_id"] for e in written["io_contract"]["test_predicates"]],
                         list(self.TESTS))
        meta = self.meta()
        self.assertEqual((meta["result"], meta["attempts"]), ("pass", 3))
        self.assertEqual([r["part"] for r in meta["per_attempt"]], ["core", *self.TESTS])
        self.assertEqual(
            [(r["part"], r["test_ids"], r["agent_run_id"], r["attempts"], r["result"])
             for r in meta["parts"]],
            [("core", None, "child-1", 1, "pass"), ("t_alpha", ["t_alpha"], "child-2", 1, "pass"),
             ("t_beta", ["t_beta"], "child-3", 1, "pass")])
        self.assertEqual(outcome.agent_run_id, "child-3")
        ir_meta = json.loads((self.repo / self.refs.ir_ref / "ir_meta.json")
                             .read_text(encoding="utf-8"))
        self.assertEqual((ir_meta["verification_status"], ir_meta["attempt_count"]),
                         ("pending", 3))

    def test_the_assembled_io_contract_follows_the_schema_order(self) -> None:
        """The three lists are not appended at the end: the evidence list sits after
        `raw_requirements` and the two predicate lists after `semantic_dependency`, as the
        phase contract's schema block orders them, and a key the block does not name keeps its
        place after them. The fixture core above carries none of the keys between, so only a
        core that does can tell the reorder from an append."""
        core = _valid_ir()
        core["io_contract"] = {"notes": "kept", "semantic_dependency": {"required_sources": ["q"]},
                               "diagnostics_contract": {"checks": []}, "raw_requirements": {},
                               "outputs": [], "inputs": []}
        assembled = self.conductor()._assemble_pure_ir(core, [_entries(t) for t in self.TESTS],
                                                       list(self.TESTS))
        self.assertEqual(list(assembled["io_contract"]),
                         ["inputs", "outputs", "raw_requirements", "test_evidence_requirements",
                          "diagnostics_contract", "semantic_dependency", "test_predicates",
                          "primary_predicates", "notes"])
        self.assertEqual(list(core["io_contract"])[0], "notes", "the core was modified")

    def test_each_part_is_a_verification_launch_with_the_core_and_its_test(self) -> None:
        c = self.conductor(_envelope(_doc()), *(_envelope(_part(t)) for t in self.TESTS))
        surface = ({"node_key": _NODE, "published_operations": [f"{_SPEC_ID}__apply"],
                    "source": "ir_public_api"},)
        c.run_substep(self.refs, "compile", "generate", dependency_surface=surface)
        core_req, *part_reqs = _requests(c)
        self.assertNotIn("pure_shape", core_req)
        self.assertEqual(core_req["dependency_surface"], list(surface))
        self.assertEqual(set(core_req["pure_context"]),
                         set(ort.PURE_CONTEXT_REQUIRED_KEYS[("compile", "generate")]))
        self.assertEqual(len(part_reqs), len(self.TESTS))
        for test_id, req in zip(self.TESTS, part_reqs):
            with self.subTest(test_id=test_id):
                self.assertEqual(req["pure_shape"], "verification")
                self.assertEqual(
                    list(req["pure_context"]),
                    list(ort.PURE_CONTEXT_REQUIRED_KEYS_BY_SHAPE[
                        ("compile", "generate", "verification")]))
                self.assertEqual(json.loads(req["pure_context"]["ir_core_document"]),
                                 _valid_ir())
                self.assertEqual(req["pure_context"]["verification_tests_document"], test_id)
                for key in ("controlled_spec_document", "tests_document",
                            "checks_module_contract_document"):
                    self.assertEqual(req["pure_context"][key], core_req["pure_context"][key])
                # The phase contract is the excerpt of the core's whole document.
                self.assertEqual(req["pure_context"]["phase_contract_document"],
                                 wc._markdown_heading_sections(
                                     core_req["pure_context"]["phase_contract_document"],
                                     wc.Conductor._VERIFICATION_PHASE_CONTRACT_HEADINGS,
                                     subject="x"))
                # A part is shown no catalog: it transcribes no operation name, and its
                # template has no slot that would render one.
                self.assertNotIn("dependency_surface", req)

    def test_every_launch_of_a_part_names_its_test_and_no_other_launch_does(self) -> None:
        """`pure_part` is what a record's reader tells a part's launch from a retry by, so it
        is on EVERY launch of the part — its warm repair turn included, which carries no
        context to read the test from — and on no launch of the core."""
        c = self.conductor(_envelope(_doc()), _envelope(_part("t_beta")),
                           _envelope(_part("t_alpha")), _envelope(_part("t_beta")))
        c.run_substep(self.refs, "compile", "generate")
        reqs = _requests(c)
        self.assertEqual([r.get("pure_part") for r in reqs],
                         [None, "t_alpha", "t_alpha", "t_beta"])
        self.assertTrue(reqs[2]["warm_resume"])
        self.assertNotIn("pure_context", reqs[2])
        for req in reqs:
            ort._validate_launch_request_payload(ort.prepare_launch_request_payload(
                dict(req, dependency_ref=f"{self.refs.spec_path}/deps.yaml")))

    def test_a_pure_part_is_refused_off_the_verification_shape(self) -> None:
        c = self.conductor(_envelope(_doc()), _envelope(_part("t_alpha")),
                           _envelope(_part("t_beta")))
        c.run_substep(self.refs, "compile", "generate")
        core_req, part_req, _ = _requests(c)
        for req, part in ((core_req, "t_alpha"), (part_req, ""), (part_req, 3)):
            with self.subTest(part=part, shape=req.get("pure_shape")):
                with self.assertRaises(ValueError) as caught:
                    ort._validate_pure_launch_request_payload(dict(req, pure_part=part))
                self.assertIn("pure_part", str(caught.exception))

    def test_a_part_naming_another_test_is_repaired(self) -> None:
        c = self.conductor(_envelope(_doc()), _envelope(_part("t_beta")),
                           _envelope(_part("t_alpha")), _envelope(_part("t_beta")))
        outcome = c.run_substep(self.refs, "compile", "generate")
        self.assertEqual((outcome.status, outcome.attempts), ("pass", 4))
        meta = self.meta()
        failed = meta["per_attempt"][1]
        self.assertEqual((failed["part"], failed["failure_category"]),
                         ("t_alpha", wc.COMPILE_IR_DOCUMENT_VIOLATION))
        self.assertIn("'t_alpha'", failed["failure_excerpt"])
        self.assertEqual([r["attempts"] for r in meta["parts"]], [1, 2, 1])
        self.assertEqual(_requests(c)[2]["repair_reason"], "pure_ir_document_repair")

    def test_a_part_declaring_an_input_defect_ends_the_substep_with_no_ir(self) -> None:
        declared = {"verification": None,
                    "compile_fail": {"input": "tests", "where": "§6-2",
                                     "what": "the judgment names no threshold"}}
        c = self.conductor(_envelope(_doc()), _envelope(_part("t_alpha")), _envelope(declared))
        outcome = c.run_substep(self.refs, "compile", "generate")
        self.assertEqual((outcome.status, outcome.leaf_returncode), ("fail", 0))
        ir_dir = self.repo / self.refs.ir_ref
        self.assertFalse((ir_dir / "spec.ir.yaml").exists())
        ir_meta = json.loads((ir_dir / "ir_meta.json").read_text(encoding="utf-8"))
        self.assertEqual(ir_meta["last_fail_reason"],
                         "tests §6-2: the judgment names no threshold")
        self.assertEqual((ir_meta["verification_status"], ir_meta["attempt_count"]),
                         ("fail", 3))
        meta = self.meta()
        self.assertEqual((meta["result"], meta["failure_category"], meta["attempts"]),
                         ("fail", wc.COMPILE_DECLARED_FAIL, 3))
        # The substep's launch count on a failing exit too, not the last loop's.
        self.assertEqual(outcome.attempts, 3)
        self.assertEqual(meta["parts"][-1]["part"], "t_beta")
        self.assertEqual(meta["parts"][-1]["result"], "fail")
        decision = c.classify_failure(self.refs, "compile", [outcome])
        self.assertEqual((decision.action, decision.reason),
                         ("fail_closed", wc.COMPILE_DECLARED_FAIL))

    def test_a_declared_fail_whose_host_write_fails_still_counts_every_launch(self) -> None:
        """The declared-fail projection is written before the loop records its rows, so when
        that write raises, the loop's launch is in no shared row: it is added on its own."""
        declared = {"verification": None,
                    "compile_fail": {"input": "tests", "where": "§6-2", "what": "x"}}
        c = self.conductor(_envelope(_doc()), _envelope(_part("t_alpha")), _envelope(declared))

        def boom(*a, **k):
            raise OSError("no space left on device")

        c._write_declared_compile_fail = boom  # type: ignore[assignment]
        outcome = c.run_substep(self.refs, "compile", "generate")
        self.assertEqual(outcome.infra_error[0], "pure_compile_host_write_failed")
        self.assertEqual(outcome.attempts, 3)

    def test_a_core_that_never_passes_launches_no_part(self) -> None:
        c = self.conductor(_envelope({"ir": _valid_ir()}))
        outcome = c.run_substep(self.refs, "compile", "generate")
        self.assertEqual(outcome.status, "fail")
        self.assertEqual(len(_requests(c)), 1 + ort_repair_turns())
        self.assertEqual(outcome.attempts, 1 + ort_repair_turns())
        self.assertTrue(all("pure_shape" not in r for r in _requests(c)))
        meta = self.meta()
        self.assertEqual(meta["failure_category"], wc.COMPILE_IR_DOCUMENT_VIOLATION)
        self.assertEqual([r["part"] for r in meta["parts"]], ["core"])
        decision = c.classify_failure(self.refs, "compile", [outcome])
        self.assertEqual((decision.action, decision.target_phase, decision.repair_strategy),
                         ("retry", "compile", "reuse"))
        self.assertTrue(c._read_repair_findings(self.refs, decision.reason, "compile"))

    def test_a_core_carrying_a_verification_list_is_repaired(self) -> None:
        for key in wc.Conductor._PURE_IR_VERIFICATION_LISTS:
            with self.subTest(key=key):
                ir = _valid_ir()
                ir["io_contract"][key] = []
                found = self.conductor()._pure_ir_core_violations(self.refs, _doc(ir))
                self.assertIsNotNone(found)
                self.assertEqual(found[0], wc.COMPILE_IR_DOCUMENT_VIOLATION)
                self.assertIn(f"`{key}`", found[1])
        ir = _valid_ir()
        ir["io_contract"] = []
        found = self.conductor()._pure_ir_core_violations(self.refs, _doc(ir))
        self.assertIn("`ir.io_contract` must be a JSON object", found[1])
        self.assertIsNone(self.conductor()._pure_ir_core_violations(self.refs, _doc()))
        self.assertIsNone(self.conductor()._pure_ir_core_violations(self.refs, _declared()))

    def test_an_id_declared_twice_is_one_part(self) -> None:
        """The host's part list is `_parse_test_ids_from_tests_md`, which keeps the first of
        an id declared in both forms; without that, the test's entries would be authored twice
        and assembled twice, and the gate would refuse the duplicate."""
        (self.repo / self.refs.spec_path / "tests.md").write_text(
            "## 6. Tests\n\n### 6-1. `t_alpha`\n\n- `test_id`: `t_alpha`\n\n"
            "### 6-2. `t_beta`\n", encoding="utf-8")
        c, outcome = self.run_clean()
        self.assertEqual(outcome.status, "pass")
        self.assertEqual([r["part"] for r in self.meta()["parts"]], ["core", *self.TESTS])

    def test_a_tests_document_with_no_test_id_launches_nothing(self) -> None:
        (self.repo / self.refs.spec_path / "tests.md").write_text(
            "## 6. Tests\n\n- test_id: written without the backticks\n", encoding="utf-8")
        c = self.conductor(_envelope(_doc()))
        outcome = c.run_substep(self.refs, "compile", "generate")
        self.assertEqual(outcome.status, "fail")
        self.assertEqual(outcome.infra_error[0], "pure_context_assembly_failed")
        self.assertTrue(outcome.infra_error[1].startswith("pure_tests_document_no_test_ids: "))
        self.assertEqual(_requests(c), [])

    def test_a_document_that_vanishes_after_the_core_ran_fails_no_part(self) -> None:
        """The parts' documents are read before the core launches, so a spec document that
        disappears while the core runs cannot fail a LATER part's context assembly — which would
        return before any meta write and leave the core's attempts unrecorded."""
        c = self.conductor(_envelope(_doc()), *(_envelope(_part(t)) for t in self.TESTS))
        real_spawn = c.spawn_leaf

        def spawn_then_delete(*a, **k):
            proc = real_spawn(*a, **k)
            (self.repo / self.refs.spec_path / "controlled_spec.md").unlink(missing_ok=True)
            return proc

        c.spawn_leaf = spawn_then_delete  # type: ignore[assignment]
        outcome = c.run_substep(self.refs, "compile", "generate")
        self.assertEqual((outcome.status, outcome.attempts), ("pass", 3))
        self.assertEqual([r["part"] for r in self.meta()["parts"]], ["core", *self.TESTS])

    def test_an_assembly_the_host_cannot_vouch_for_is_a_host_write_failure(self) -> None:
        c = self.conductor(_envelope(_doc()), *(_envelope(_part(t)) for t in self.TESTS))
        broken = _assembled_ir(*self.TESTS)
        broken["meta"]["spec_kind"] = "problem"
        c._assemble_pure_ir = lambda core, parts, ids: broken  # type: ignore[assignment]
        outcome = c.run_substep(self.refs, "compile", "generate")
        self.assertEqual((outcome.status, outcome.leaf_returncode), ("fail", 1))
        self.assertEqual(outcome.infra_error[0], "pure_compile_host_write_failed")
        self.assertIn("spec_kind", outcome.infra_error[1])
        self.assertFalse((self.repo / self.refs.ir_ref / "spec.ir.yaml").exists())

    # --- a reopen -------------------------------------------------------------------

    _PRIOR = {"core": "prior-core", "t_alpha": "prior-alpha"}

    def test_a_test_named_core_does_not_take_the_cores_session(self) -> None:
        """`core` is a legal test id (the tests.md grammar reserves none), and the core loop's
        `part` label is `core` too; the previous run's rows are keyed by `test_ids`, so on a
        reopen the core resumes the core and the test resumes the test."""
        (self.repo / self.refs.spec_path / "tests.md").write_text(
            "## 6. Tests\n\n### 6-1. `core`\n\nA test called core.\n", encoding="utf-8")
        self.TESTS = ("core",)
        self.stage_previous_run({}, rows=[("core", None, "prior-core"),
                                          ("core", "core", "prior-test")])
        core, part = self.reopen(warm=True)
        self.assertEqual(core["repair_target_agent_run_id"], "prior-core")
        self.assertEqual(part["repair_target_agent_run_id"], "prior-test")

    def stage_previous_run(self, parts: dict[str, str], rows=None,
                           failed: str | None = None) -> str:
        """The directory a previous `compile.generate` wrote into, its meta's `parts[]`, its
        IR, and one launch record per previous loop naming it."""
        prior_ref = f"{self.refs.ir_ref.rsplit('/', 1)[0]}/advdiff-uc2_20260101_000"
        prior = self.repo / prior_ref
        prior.mkdir(parents=True, exist_ok=True)
        (prior / "spec.ir.yaml").write_text(
            yaml.safe_dump(_assembled_ir(*self.TESTS), sort_keys=False), encoding="utf-8")
        rows = rows if rows is not None else [
            (k, None if k == "core" else k, v) for k, v in parts.items()]
        (prior / "compile_generate_meta.json").write_text(json.dumps({
            "result": "fail" if failed else "pass",
            "parts": [{"part": k, "test_ids": None if t is None else [t], "agent_run_id": v,
                       "result": "fail" if k == failed else "pass"}
                      for k, t, v in rows]}), encoding="utf-8")
        launches = self.repo / "workspace" / "orchestrations" / "o" / "launches"
        launches.mkdir(parents=True, exist_ok=True)
        for _part_name, _test_id, arid in rows:
            (launches / f"{arid}.request.json").write_text(json.dumps(
                {"ir_ref": prior_ref, "pipeline_ref": self.refs.pipeline_ref,
                 "agent_run_id": arid}), encoding="utf-8")
        return prior_ref

    def reopen(self, *, warm: bool):
        c = self.conductor(_envelope(_doc()), *(_envelope(_part(t)) for t in self.TESTS))
        c._claude_session_resumable = lambda arid, **kw: warm  # type: ignore[assignment]
        launches = self.repo / "workspace" / "orchestrations" / "o" / "launches"
        target = sorted(p.name for p in launches.glob("prior-*.request.json"))[-1]
        repair = {"issue_severity": "major", "repair_strategy": "reuse",
                  "repair_target_agent_run_id": target.removesuffix(".request.json"),
                  "repair_reason": "compile_static_compile_static_violation",
                  "repair_findings": "io_contract.primary_predicates[1]: reads no state"}
        outcome = c.run_substep(self.refs, "compile", "generate", repair=repair)
        self.assertEqual(outcome.status, "pass")
        return _requests(c)

    def test_a_reopen_resumes_each_loop_on_its_own_previous_attempt(self) -> None:
        self.stage_previous_run(self._PRIOR)
        core, alpha, beta = self.reopen(warm=True)
        self.assertEqual(core["repair_target_agent_run_id"], "prior-core")
        self.assertEqual(alpha["repair_target_agent_run_id"], "prior-alpha")
        # The same findings; a part's copy adds the host's locator note (its own row).
        self.assertTrue(alpha["repair_findings"].startswith(core["repair_findings"]))
        # The core resumes its session; a part does not, even when its session is resumable:
        # that session holds the previous core, and a warm repair re-sends no context. Its cold
        # repair carries the core this reopen produced.
        self.assertTrue(core["warm_resume"])
        self.assertFalse(alpha.get("warm_resume"))
        self.assertEqual(json.loads(alpha["pure_context"]["ir_core_document"]), _valid_ir())
        self.assertIn("prior_document", alpha)
        # A loop the previous run never reached resumes nothing: the only target on offer
        # is another loop's session.
        self.assertEqual(beta["repair_strategy"], "none")
        self.assertFalse(beta.get("warm_resume"))

    def test_a_gate_finding_reaches_every_part_with_where_its_entries_sat(self) -> None:
        """Every loop passed, so the findings come from a gate on the whole IR, which names an
        entry by its index; each part's copy closes with the host's note of its own indices."""
        self.stage_previous_run(self._PRIOR)
        core, alpha, _ = self.reopen(warm=True)
        self.assertNotIn("Host note", core["repair_findings"])
        self.assertTrue(alpha["repair_findings"].startswith(
            "io_contract.primary_predicates[1]: reads no state\n\nHost note: "))
        self.assertIn("test 't_alpha' were io_contract.test_evidence_requirements[0]; "
                      "io_contract.test_predicates[0]; io_contract.primary_predicates[0]",
                      alpha["repair_findings"])

    def test_a_loop_that_ended_the_run_is_the_only_one_handed_its_refusal(self) -> None:
        """The previous run ENDED on t_alpha's document violation, so the findings are that
        part's own refusal: handing them to the core as "correct exactly these violations" is
        an instruction its validator refuses. The core starts fresh; t_alpha repairs."""
        self.stage_previous_run(self._PRIOR, failed="t_alpha")
        core, alpha, beta = self.reopen(warm=True)
        self.assertEqual(core["repair_strategy"], "none")
        self.assertNotIn("repair_findings", core)
        self.assertEqual(alpha["repair_target_agent_run_id"], "prior-alpha")
        self.assertNotIn("Host note", alpha["repair_findings"])
        self.assertEqual(beta["repair_strategy"], "none")

    def test_a_part_with_no_entries_in_the_previous_ir_gets_no_prior_document(self) -> None:
        """A previous IR without this test's entries (one written before issue #526, or one
        whose tests changed) has nothing to correct: no prior document, not three empty lists
        presented as the leaf's own."""
        prior_ref = self.stage_previous_run(self._PRIOR)
        ir = _assembled_ir("t_beta")
        (self.repo / prior_ref / "spec.ir.yaml").write_text(
            yaml.safe_dump(ir, sort_keys=False), encoding="utf-8")
        record = {"ir_ref": prior_ref}
        c = self.conductor()
        self.assertIsNone(c._pure_ir_verification_prior_document(("t_alpha",))(record))
        self.assertIsNotNone(c._pure_ir_verification_prior_document(("t_beta",))(record))

    def test_a_cold_reopen_carries_each_part_its_own_entries(self) -> None:
        self.stage_previous_run(self._PRIOR)
        core, alpha, _ = self.reopen(warm=False)
        # The core's copy is the assembled IR WITHOUT the three lists: it is refused for
        # carrying them, so they must not come back to it as its own document.
        self.assertEqual(json.loads(core["prior_document"]), _doc())
        self.assertIsNone(c_core_violations(self, json.loads(core["prior_document"])))
        self.assertEqual(json.loads(alpha["prior_document"]),
                         {"verification": _entries("t_alpha"), "compile_fail": None})

    def test_a_reopen_of_a_run_with_no_parts_keeps_the_core_on_the_outer_target(self) -> None:
        """A run from before issue #526 had one loop, the core's predecessor, so its target is
        the core's; no part can resume it."""
        self.stage_previous_run({})
        launches = self.repo / "workspace" / "orchestrations" / "o" / "launches"
        (launches / "prior-alpha.request.json").write_text(json.dumps(
            {"ir_ref": "workspace/nowhere", "agent_run_id": "prior-alpha"}), encoding="utf-8")
        core, alpha, beta = self.reopen(warm=True)
        self.assertEqual(core["repair_target_agent_run_id"], "prior-alpha")
        self.assertEqual((alpha["repair_strategy"], beta["repair_strategy"]), ("none", "none"))


class PureCompilePartGroupTests(_Fixture):
    """Consecutive tests share a `verification` part up to `_VERIFICATION_PART_BUDGET_BYTES` of
    their tests.md definitions, and every part is shown an excerpt of the phase contract."""
    TESTS = ("t_alpha", "t_beta", "t_gamma")

    def setUp(self) -> None:
        super().setUp()
        (self.repo / self.refs.spec_path / "tests.md").write_text(
            "## 6. Tests\n\n### 6-1. `t_alpha`\n\nThe first judgment.\n\n"
            "### 6-2. `t_beta`\n\nThe second judgment.\n\n"
            "### 6-3. `t_gamma`\n\nThe third judgment.\n", encoding="utf-8")

    def meta(self) -> dict:
        return json.loads((self.repo / self.refs.ir_ref / "compile_generate_meta.json")
                          .read_text(encoding="utf-8"))

    @staticmethod
    def _group(*test_ids: str, reverse: bool = False) -> dict:
        """One part's document for `test_ids`, its entries listed test by test — in reverse
        when `reverse`, which a part is free to do."""
        ids = list(reversed(test_ids)) if reverse else list(test_ids)
        body = {key: [e for t in ids for e in _entries(t)[key]]
                for key in wc.Conductor._PURE_IR_VERIFICATION_LISTS}
        return {"verification": body, "compile_fail": None}

    def test_tests_within_the_bound_share_one_part(self) -> None:
        """The default bound holds the fixture's three short definitions in one part: one
        launch, named by its first test, shown every id, and its entries assembled in the tests
        document's order whatever order the part listed them in."""
        c = self.conductor(_envelope(_doc()), _envelope(self._group(*self.TESTS, reverse=True)))
        outcome = c.run_substep(self.refs, "compile", "generate")
        self.assertEqual((outcome.status, outcome.attempts), ("pass", 2))
        core_req, part_req = _requests(c)
        self.assertEqual(part_req["pure_part"], "t_alpha")
        self.assertEqual(part_req["pure_context"]["verification_tests_document"],
                         "t_alpha\nt_beta\nt_gamma")
        written = yaml.safe_load((self.repo / self.refs.ir_ref / "spec.ir.yaml")
                                 .read_text(encoding="utf-8"))
        self.assertEqual(written, _assembled_ir(*self.TESTS))
        self.assertEqual([(r["part"], r["test_ids"]) for r in self.meta()["parts"]],
                         [("core", None), ("t_alpha", list(self.TESTS))])

    def test_a_test_past_the_bound_opens_the_next_part(self) -> None:
        from tools.validate_pipeline_semantics import _tests_md_test_block_bytes
        text = (self.repo / self.refs.spec_path / "tests.md").read_text(encoding="utf-8")
        block = _tests_md_test_block_bytes(text)
        bound = block["t_alpha"] + block["t_beta"]
        with mock.patch.object(wc.Conductor, "_VERIFICATION_PART_BUDGET_BYTES", bound):
            c = self.conductor(_envelope(_doc()), _envelope(self._group("t_alpha", "t_beta")),
                               _envelope(self._group("t_gamma")))
            outcome = c.run_substep(self.refs, "compile", "generate")
        self.assertEqual((outcome.status, outcome.attempts), ("pass", 3))
        self.assertEqual([r.get("pure_part") for r in _requests(c)], [None, "t_alpha", "t_gamma"])
        self.assertEqual([r["test_ids"] for r in self.meta()["parts"]],
                         [None, ["t_alpha", "t_beta"], ["t_gamma"]])

    def test_the_grouping_rule(self) -> None:
        c = self.conductor()
        ids = ["a", "b", "c", "d", "e"]
        with mock.patch.object(wc.Conductor, "_VERIFICATION_PART_BUDGET_BYTES", 10):
            # Up to the bound joins; one byte past opens a new part; a test larger than the
            # bound is a part of its own, and nothing joins it after; an id with no measured
            # definition weighs nothing.
            self.assertEqual(
                c._verification_part_groups(ids, {"a": 4, "b": 6, "c": 1, "d": 30}),
                [("a", "b"), ("c",), ("d",), ("e",)])
            self.assertEqual(c._verification_part_groups(["a", "b", "c"], {"b": 10, "c": 1}),
                             [("a", "b"), ("c",)])
            self.assertEqual(c._verification_part_groups(["a", "b"], {"a": 11, "b": 11}),
                             [("a",), ("b",)])
            self.assertEqual(c._verification_part_groups([], {}), [])
            # A new part starts counting from its own first test.
            self.assertEqual(c._verification_part_groups(["a", "b", "c"],
                                                         {"a": 6, "b": 6, "c": 3}),
                             [("a",), ("b", "c")])

    def test_a_group_reopen_resumes_the_group_it_was(self) -> None:
        """The previous run's rows are keyed by the group's ids, so the same group resumes its
        own session, and a group the previous run did not have starts cold."""
        prior_ref = f"{self.refs.ir_ref.rsplit('/', 1)[0]}/advdiff-uc2_20260101_000"
        prior = self.repo / prior_ref
        prior.mkdir(parents=True, exist_ok=True)
        (prior / "spec.ir.yaml").write_text(
            yaml.safe_dump(_assembled_ir(*self.TESTS), sort_keys=False), encoding="utf-8")
        rows = [("core", None, "prior-core"), ("t_alpha", list(self.TESTS), "prior-group")]
        (prior / "compile_generate_meta.json").write_text(json.dumps({
            "result": "pass",
            "parts": [{"part": k, "test_ids": t, "agent_run_id": v, "result": "pass"}
                      for k, t, v in rows]}), encoding="utf-8")
        launches = self.repo / "workspace" / "orchestrations" / "o" / "launches"
        launches.mkdir(parents=True, exist_ok=True)
        for _k, _t, arid in rows:
            (launches / f"{arid}.request.json").write_text(json.dumps(
                {"ir_ref": prior_ref, "pipeline_ref": self.refs.pipeline_ref,
                 "agent_run_id": arid}), encoding="utf-8")
        repair = {"issue_severity": "major", "repair_strategy": "reuse",
                  "repair_target_agent_run_id": "prior-group",
                  "repair_reason": "compile_static_compile_static_violation",
                  "repair_findings": "io_contract.primary_predicates[2]: reads no state"}
        c = self.conductor(_envelope(_doc()), _envelope(self._group(*self.TESTS)))
        c._claude_session_resumable = lambda arid, **kw: False  # type: ignore[assignment]
        self.assertEqual(c.run_substep(self.refs, "compile", "generate", repair=repair).status,
                         "pass")
        _core, part = _requests(c)
        self.assertEqual(part["repair_target_agent_run_id"], "prior-group")
        self.assertIn("tests 't_alpha', 't_beta', 't_gamma' were "
                      "io_contract.test_evidence_requirements[0, 1, 2]", part["repair_findings"])
        self.assertEqual(json.loads(part["prior_document"]), self._group(*self.TESTS))
        # A different grouping finds no row of its own and starts cold.
        with mock.patch.object(wc.Conductor, "_VERIFICATION_PART_BUDGET_BYTES", 0):
            c = self.conductor(_envelope(_doc()),
                               *(_envelope(self._group(t)) for t in self.TESTS))
            c._claude_session_resumable = lambda arid, **kw: False  # type: ignore[assignment]
            self.assertEqual(
                c.run_substep(self.refs, "compile", "generate", repair=repair).status, "pass")
        self.assertEqual([r["repair_strategy"] for r in _requests(c)[1:]], ["none"] * 3)

    def test_a_phase_contract_missing_a_selected_heading_launches_nothing(self) -> None:
        doc = self.repo / ort.WORKFLOW_PHASE_DOC_BY_STEP["compile"]
        doc.write_text(doc.read_text(encoding="utf-8").replace(
            "#### V3. io_contract consistency", "#### V3. renamed"), encoding="utf-8")
        c = self.conductor(_envelope(_doc()))
        outcome = c.run_substep(self.refs, "compile", "generate")
        self.assertEqual(outcome.infra_error[0], "pure_context_assembly_failed")
        self.assertIn("pure_phase_contract_document_unsliceable", outcome.infra_error[1])
        self.assertIn("V3. io_contract consistency", outcome.infra_error[1])
        self.assertEqual(_requests(c), [])

    def test_the_real_phase_contract_excerpt(self) -> None:
        """Every selected heading is in the real document once, the excerpt is those sections
        alone, and it leaves out the sections about the IR's other parts."""
        text = (_REAL_REPO / ort.WORKFLOW_PHASE_DOC_BY_STEP["compile"]).read_text(
            encoding="utf-8")
        excerpt = wc._markdown_heading_sections(
            text, wc.Conductor._VERIFICATION_PHASE_CONTRACT_HEADINGS, subject="x")
        headings = [ln for ln in excerpt.splitlines() if re.match(r"^#{1,6} ", ln)]
        self.assertEqual(tuple(headings), wc.Conductor._VERIFICATION_PHASE_CONTRACT_HEADINGS)
        self.assertIn("  primary_predicates:", excerpt)
        for absent in ("## Overview", "#### V8. public_api surface", "#### Decision Criteria",
                       "#### Severity of a finding (`issue_severity`)"):
            self.assertNotIn(absent, excerpt)
        self.assertLess(len(excerpt), len(text))


class MarkdownHeadingSectionsTests(unittest.TestCase):
    TEXT = ("# T\n\n## A\n\na body\n\n```yaml\n# a comment\n## not a heading\n```\n\n"
            "### A.1\n\nsub body\n\n## B\n\nb body\n")

    def test_a_section_is_its_heading_and_own_body_in_document_order(self) -> None:
        self.assertEqual(
            wc._markdown_heading_sections(self.TEXT, ("## B", "## A"), subject="t"),
            "## A\n\na body\n\n```yaml\n# a comment\n## not a heading\n```\n\n"
            "## B\n\nb body")
        self.assertEqual(wc._markdown_heading_sections(self.TEXT, ("### A.1",), subject="t"),
                         "### A.1\n\nsub body")

    def test_a_missing_or_repeated_heading_raises(self) -> None:
        for text, heading in ((self.TEXT, "## C"), (self.TEXT, "## not a heading"),
                              (self.TEXT + "\n## B\n", "## B")):
            with self.subTest(heading=heading):
                with self.assertRaises(ValueError) as caught:
                    wc._markdown_heading_sections(text, (heading,), subject="t")
                self.assertIn(repr(heading), str(caught.exception))


class TestsBlockBytesTests(unittest.TestCase):
    def test_each_definition_runs_to_the_next_declaration_or_section(self) -> None:
        from tools.validate_pipeline_semantics import _tests_md_test_block_bytes
        text = ("## 6. Tests\n### 6-1. `a`\nxx\n- `test_id`: `b`\nyyy\n"
                "## 7. Notes\nignored\n### 6-2. `a`\nz\n")
        self.assertEqual(_tests_md_test_block_bytes(text),
                         {"a": len("### 6-1. `a`\nxx\n") + len("### 6-2. `a`\nz\n"),
                          "b": len("- `test_id`: `b`\nyyy\n")})
        self.assertEqual(list(_tests_md_test_block_bytes("### 6-1. `é`\né\n").values()),
                         [len("### 6-1. `é`\né\n".encode("utf-8"))])


class PureCompileVerificationDocumentTests(_Fixture):
    def violations(self, doc, test_ids: tuple[str, ...] = (_TEST_ID,)):
        return self.conductor()._pure_ir_verification_violations(test_ids)(self.refs, doc)

    def test_a_clean_part_is_accepted(self) -> None:
        self.assertIsNone(self.violations(_part()))
        many = _part()
        many["verification"]["primary_predicates"].append(
            dict(many["verification"]["primary_predicates"][0], quantity="r"))
        self.assertIsNone(self.violations(many))

    def test_each_malformed_part_is_one_document_violation(self) -> None:
        def with_body(**changes):
            body = _entries()
            body.update(changes)
            return {"verification": body, "compile_fail": None}

        two = _entries()["test_predicates"] * 2
        cases = {
            "not an object": ([], "not a JSON object"),
            "the IR envelope": (_doc(), "EXACTLY the keys `verification` and `compile_fail`"),
            "an extra key": (dict(_part(), ir=None), "EXACTLY the keys"),
            "a list missing": ({"verification": {k: v for k, v in _entries().items()
                                                 if k != "test_predicates"},
                                "compile_fail": None}, "EXACTLY the keys"),
            "not a list": (with_body(test_predicates={}), "must be a JSON array"),
            "another test": (_part("t_other"), "'t_other'"),
            "an entry that is not an object": (with_body(test_predicates=["x"]),
                                               "must be an object"),
            "two predicates": (with_body(test_predicates=two), "EXACTLY one entry"),
            "no evidence entry": (with_body(test_evidence_requirements=[]),
                                  "EXACTLY one entry"),
            "no primary predicate": (with_body(primary_predicates=[]), "at least one entry"),
            "both answers": ({"verification": _entries(),
                              "compile_fail": {"input": "tests", "where": "§6",
                                               "what": "x"}}, "two different answers"),
        }
        for name, (doc, needle) in cases.items():
            with self.subTest(case=name):
                found = self.violations(doc)
                self.assertIsNotNone(found)
                self.assertEqual(found[0], wc.COMPILE_IR_DOCUMENT_VIOLATION)
                self.assertIn(needle, found[1])

    def test_a_group_part_must_answer_for_each_of_its_tests(self) -> None:
        group = ("t_a", "t_b")

        def doc(*ids: str) -> dict:
            body = {k: [e for t in ids for e in _entries(t)[k]]
                    for k in wc.Conductor._PURE_IR_VERIFICATION_LISTS}
            return {"verification": body, "compile_fail": None}

        self.assertIsNone(self.violations(doc("t_b", "t_a"), group))
        found = self.violations(doc("t_a"), group)
        self.assertIn("'t_b' has 0", found[1])
        found = self.violations(doc("t_a", "t_b", "t_c"), group)
        self.assertIn("'t_c'", found[1])
        self.assertIn("'t_a', 't_b'", found[1])
        missing_primary = doc("t_a", "t_b")
        missing_primary["verification"]["primary_predicates"].pop()
        self.assertIn("at least one entry for 't_b'",
                      self.violations(missing_primary, group)[1])
        # A test id is matched whole, never as a substring of another.
        self.assertIsNotNone(self.violations(doc("t"), ("t_a",)))

    def test_a_part_declares_only_against_its_own_inputs(self) -> None:
        for name in wc.Conductor._compile_fail_inputs("verification"):
            with self.subTest(input=name):
                self.assertIsNone(self.violations(
                    {"verification": None,
                     "compile_fail": {"input": name, "where": "§6", "what": "x"}}))
        # The core is the phase's own output, and the catalog was never shown to a part.
        for name in ("ir_core", "verification_tests", "published_operations", "deps"):
            with self.subTest(input=name):
                found = self.violations(
                    {"verification": None,
                     "compile_fail": {"input": name, "where": "§6", "what": "x"}})
                self.assertIsNotNone(found)
                self.assertIn(f"got `input` {name!r}", found[1])

    def test_the_part_inputs_are_its_inlined_documents_without_the_core_and_the_id(self) -> None:
        self.assertEqual(wc.Conductor._compile_fail_inputs("verification"),
                         ("controlled_spec", "tests", "phase_contract",
                          "checks_module_contract"))

    def test_the_part_template_enumerates_exactly_the_admissible_inputs(self) -> None:
        tpl = (Path(ort.__file__).resolve().parent / "prompt_templates"
               / "pure_compile_generate_verification.txt").read_text(encoding="utf-8")
        head = ("the input at fault, which is one of these names and nothing else (they name "
                "the inputs inlined below, in the order they appear there): ")
        self.assertEqual(tpl.count(head), 1)
        statement = tpl.split(head, 1)[1].split(";", 1)[0]
        self.assertLess(len(statement), 200, "the reader ran past the enumeration")
        listed = tuple(re.findall(r"`([a-z0-9_]+)`", statement))
        self.assertEqual(listed, wc.Conductor._compile_fail_inputs("verification"))
        slots = [k.removesuffix("_document")
                 for k in re.findall(r"^<(\w+_document)>$", tpl, flags=re.MULTILINE)]
        self.assertEqual(tuple(slots[:len(listed)]), listed)
        self.assertEqual(slots[len(listed):], ["ir_core", "verification_tests"])


def ort_repair_turns() -> int:
    from tools.pure_leaf import MAX_BUNDLE_REPAIR_TURNS
    return MAX_BUNDLE_REPAIR_TURNS


# ======================================================================================
# Provider matrix
# ======================================================================================
class PureCompileProviderMatrixTests(_Fixture):
    def test_both_compile_leaves_are_pure_without_any_ir_on_disk(self) -> None:
        """No shape condition: at `compile.generate` time there is no IR to read a shape from,
        which is exactly why the predicate cannot use the generate pair's M3c test here."""
        self.assertFalse((self.repo / self.refs.ir_ref / "spec.ir.yaml").exists())
        c = self.conductor()
        for substep in ("generate", "verify"):
            self.assertTrue(c._pure_leaf_substep(self.refs, "compile", substep))

    def test_the_declared_pure_set_is_exactly_the_migrated_leaf_pairs(self) -> None:
        """`LLM_LEAF_SUBSTEPS` is the set of LLM LEAF pairs that run pure; the launch
        table additionally keys the escalate diagnostician, which is not a leaf of any phase
        (no `SUBSTEPS` entry, no per-leaf configuration — it runs on `defaults`). Subtracting
        exactly that set keeps the identity, so a new pure LEAF pair added to one table and not
        the other is still red here."""
        self.assertEqual(
            sorted(lc.LLM_LEAF_SUBSTEPS),
            sorted(set(ort.PURE_CONTEXT_REQUIRED_KEYS) - ort.DIAGNOSE_LAUNCH_PAIRS))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
