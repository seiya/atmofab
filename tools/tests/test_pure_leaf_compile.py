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
from tools.tests.llm_samples import agentic_only_config as _agentic_cfg
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
    "spec/schema/ir/impl_defaults.schema.json",
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
        "## 1. Tests\n- test_id: t_basic\n  judgment: the metric is under the threshold\n",
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
        specs.append({"spec_kind": "profile", "spec_id": "demo_profile",
                      "spec_version": "0.1.0",
                      "controlled_spec_path": "spec/profile/demo/demo_profile/controlled_spec.md"})
    catalog = repo / "spec" / "registry" / "spec_catalog.yaml"
    catalog.parent.mkdir(parents=True, exist_ok=True)
    catalog.write_text(yaml.safe_dump({"catalog_version": "0.2.0", "specs": specs},
                                      sort_keys=False), encoding="utf-8")

    refs = wc.NodeRefs(node_key=node, spec_path=spec_path, ir_id=_IR_ID,
                       pipeline_id="p_20260907_001")
    ir_dir = repo / refs.ir_ref
    ir_dir.mkdir(parents=True, exist_ok=True)
    (ir_dir / "dependency_graph.json").write_text(
        json.dumps({"node_key": node, "direct_deps": [], "all_nodes": [node]}, indent=2),
        encoding="utf-8")
    (ir_dir / "dependency_surface.json").write_text(
        json.dumps([{"node_key": node, "published_operations": [f"{spec_id}__apply"],
                     "source": "ir_public_api"}], indent=2),
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
        "impl_defaults": {"toolchain": {}},
        "io_contract": {"inputs": [], "outputs": []},
        "dependency": {"node_key": _NODE, "direct_deps": []},
    }
    if kind in ("component", "infrastructure"):
        ir["public_api"] = {"published_operations": [{"operation_id": f"{_SPEC_ID}__apply"}]}
    return ir


def _doc(ir: dict | None = None, reason=None) -> dict:
    return {"ir": ir if ir is not None else _valid_ir(), "last_fail_reason": reason}


def _verdict(status: str = "pass", severity: str = "none", reason=None) -> dict:
    return {"verification_status": status, "issue_severity": severity,
            "last_fail_reason": reason,
            "findings": [] if status == "pass" else [{"summary": "an item is unmet"}]}


def _finalized(conductor) -> list[dict]:
    """The `--agent-run-json` payload of every `finalize-child`, in order."""
    return [cap["--agent-run-json"] for s, cap in conductor.calls if s == "finalize-child"]


def _superseded_reasons(conductor) -> list[str]:
    """The `--reason` of every superseded-run tombstone, in order."""
    return [cap["--reason"] for s, cap in conductor.calls if s == "add-superseded-runs"]


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
        """NOT the generate producer's `""` degradation, and the difference is the point.

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

    #: Every declared context key that comes from a FILE, and the file it comes from. The two
    #: that do not are `profile_spec_document` (a host sentence when no profile is declared) and
    #: `toolchain_document` (derived from the backend registry, and non-empty by its own raise) —
    #: both are covered by rows of their own in this class.
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
            "impl_defaults_schema_document": "spec/schema/ir/impl_defaults.schema.json",
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
                    self.assertIn("_missing", str(caught.exception))
                    self.assertIn(rel, str(caught.exception))

    def test_the_key_source_table_covers_every_declared_key(self) -> None:
        """Set identity between the table above and the contract, with the two derived keys
        named explicitly — so a new key is either given a file row or a deliberate exemption,
        and never silently neither."""
        derived = {"generate": {"profile_spec_document", "toolchain_document"}, "verify": set()}
        for substep, sources in self._KEY_SOURCE.items():
            with self.subTest(builder=substep):
                self.assertEqual(
                    set(sources) | derived[substep],
                    set(ort.PURE_CONTEXT_REQUIRED_KEYS[("compile", substep)]))

    def test_the_derived_keys_are_non_empty_by_construction(self) -> None:
        """The two keys with no file. `toolchain_document` raises rather than returning an empty
        admissible set; `profile_spec_document` returns the host's fixed sentence when the node
        declares no profile, and a named line when the catalog cannot resolve one — so neither
        has a path that yields `""`."""
        c = self.conductor()
        ctx = c._build_pure_compile_context(self.refs)
        self.assertTrue(ctx["toolchain_document"].strip())
        self.assertEqual(ctx["profile_spec_document"],
                         wc.Conductor._PURE_PROFILE_ABSENT_DOCUMENT)
        # The registry-empty branch of `_pure_toolchain_document` raises rather than shipping
        # `[]`, which is the same disposition every file-backed key has.
        with mock.patch.object(backend_registry, "implemented_backend_ids",
                               return_value=()):
            with self.assertRaises(RuntimeError) as caught:
                c._pure_toolchain_document(self.refs)
        self.assertIn("pure_toolchain_document_unresolvable", str(caught.exception))

    def _build(self, substep: str):
        c = self.conductor()
        return (c._build_pure_compile_context if substep == "generate"
                else c._build_pure_compile_verify_context)

    def test_a_missing_repository_document_raises_and_spawns_nothing(self) -> None:
        """A document the leaf cannot repair is a fail_closed BEFORE any launch. Driven through
        `run_substep` rather than the builder, so what is pinned is the recovery, not the raise."""
        for rel, marker in (
            (ort.WORKFLOW_PHASE_DOC_BY_STEP["compile"], "pure_phase_contract_document_missing"),
            ("docs/workflow/CHECKS_MODULE_CONTRACT.md", "pure_checks_contract_document_missing"),
            ("spec/schema/ir/impl_defaults.schema.json",
             "pure_impl_defaults_schema_document_missing"),
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

    def test_the_toolchain_document_is_derived_from_the_registry(self) -> None:
        """The expected value is COMPUTED from the registry, never transcribed: transcribing it
        would turn a legitimate new backend into a red test instead of a wider document."""
        c = self.conductor()
        doc = json.loads(c._build_pure_compile_context(self.refs)["toolchain_document"])
        expected = [
            {"language": lang, "build_system": bs}
            for lang in backend_registry.implemented_backend_ids("language")
            if backend_registry.provides("language", lang, "control_file")
            and backend_registry.provides("language", lang, "runner_render")
            for bs in backend_registry.implemented_backend_ids("build_system")
            if backend_registry.provides("build_system", bs, "build_execute")
            and backend_registry.provides("build_system", bs, "control_file")
        ]
        self.assertTrue(expected, "the registry offers no admissible pair; this observes nothing")
        self.assertEqual(doc["admissible_toolchains"], expected)

    def test_the_templates_name_no_backend_identifier(self) -> None:
        """The whole reason `toolchain_document` exists: the two templates are `neutral core`
        files (`docs/BACKEND_BOUNDARY.md`), so the toolchain values must travel as DATA.

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


class PureCompileProfileContextTests(_Fixture):
    PROFILE = True

    def test_a_declared_profile_is_resolved_through_the_catalog(self) -> None:
        c = self.conductor()
        doc = c._build_pure_compile_context(self.refs)["profile_spec_document"]
        self.assertIn("demo_profile", doc)
        self.assertIn("The profile fixes the component set.", doc)

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
        self.assertIsNone(self.violations({"ir": None, "last_fail_reason": "tests.md is empty"}))

    def test_each_shape_defect_is_one_ir_document_violation(self) -> None:
        for label, doc in (
            ("not an object", ["ir"]),
            ("extra key", dict(_doc(), notes="hello")),
            ("missing key", {"ir": _valid_ir()}),
            ("reason wrong type", {"ir": None, "last_fail_reason": {"why": "x"}}),
            ("reason empty", {"ir": None, "last_fail_reason": "   "}),
            # The family must STRADDLE the `isinstance(ir, dict)` guard, and it did not: a
            # STRING is iterable, so the section check below it (`k not in ir`, a substring
            # test) produces a violation anyway and the guard could be deleted with this row
            # green. A value that is NOT iterable is what reaches the guard — and without it
            # the section check raises `TypeError`, which escapes `run_substep` uncaught and
            # kills the conductor mid-run rather than costing one repair turn.
            ("ir not a mapping (iterable)", {"ir": "schema_version: 1.0",
                                             "last_fail_reason": None}),
            ("ir not a mapping (a list)", {"ir": ["schema_version"], "last_fail_reason": None}),
            ("ir is null", {"ir": None, "last_fail_reason": None}),
            ("ir is a number", {"ir": 5, "last_fail_reason": None}),
            ("ir is a bool", {"ir": True, "last_fail_reason": None}),
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
        # (`_validate_published_surface`, `_validate_toolchain_backend_supported`,
        # `_conductor_authors_runner`). Refusing a spelling those readers agree on would be an
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
                                                                    "last_fail_reason": None}))

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
        ir["impl_defaults"]["toolchain"] = {"on": "yes", "off": "no", "n": "1_000"}
        ir["meta"]["notes"] = "  leading and trailing  "
        self.assertIsNone(self.violations(_doc(ir)))

    def test_an_unencodable_document_is_a_repairable_violation_not_a_crash(self) -> None:
        """The UTF-8 probe in the producer loop. `json.loads` accepts a lone surrogate, and
        writing it raises — so it must be classified as a document defect before the write."""
        c = self.conductor(_envelope(json.dumps(_doc()).replace(
            '"last_fail_reason": null', '"last_fail_reason": null, "x": "\\ud800"')))
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
        c = self.conductor(_envelope(_doc()))
        outcome = c.run_substep(self.refs, "compile", "generate")
        self.assertEqual(outcome.status, "pass")
        # The leaf holds no write authority, so its terminal row carries no output_refs and
        # explains itself through the summary instead.
        self.assertEqual(outcome.output_refs, [])
        ir_dir = self.repo / self.refs.ir_ref
        self.assertEqual(yaml.safe_load((ir_dir / "spec.ir.yaml").read_text(encoding="utf-8")),
                         _valid_ir())
        meta = json.loads((ir_dir / "ir_meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["verification_status"], "pending")
        self.assertEqual(meta["ir_id"], self.refs.ir_id)
        self.assertEqual(meta["node_key"], _NODE)
        self.assertIsNone(meta["last_fail_reason"])
        self.assertTrue(meta["context_isolated"])
        gmeta = json.loads((ir_dir / "compile_generate_meta.json").read_text(encoding="utf-8"))
        self.assertEqual(gmeta["result"], "pass")
        self.assertEqual(gmeta["attempts"], 1)
        self.assertEqual(gmeta["prompt_contract_version"], ort.PURE_PROMPT_CONTRACT_VERSION)

    def test_the_pending_status_is_not_read_as_a_certified_dependency(self) -> None:
        """`"pending"` satisfies the meta contract's non-empty-string requirement and is not
        `"pass"`, which is what every dependency-certification reader compares against."""
        c = self.conductor(_envelope(_doc()))
        c.run_substep(self.refs, "compile", "generate")
        meta = json.loads((self.repo / self.refs.ir_ref
                           / "ir_meta.json").read_text(encoding="utf-8"))
        self.assertNotEqual(meta["verification_status"], "pass")

    def test_the_host_written_meta_satisfies_the_stage_meta_contract(self) -> None:
        """The five keys and their types, checked by the contract module itself rather than by a
        list transcribed here — `_stage_meta_contract_findings` runs the same functions inside
        `classify_failure`, so a meta that fails them escalates every compile failure."""
        import tools.meta_contracts as mc
        c = self.conductor(_envelope(_doc()))
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
        c = self.conductor(_envelope({"ir": _valid_ir()}), _envelope(_doc()))
        outcome = c.run_substep(self.refs, "compile", "generate")
        self.assertEqual(outcome.status, "pass")
        self.assertEqual(outcome.attempts, 2)
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
        document table so the verify-severity gate sees it, which is where the agentic leaf's
        identical declaration lands."""
        reason = "tests.md declares no test_id, so the verification contract cannot be derived"
        c = self.conductor(_envelope({"ir": None, "last_fail_reason": reason}))
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

    def test_a_declared_compile_fail_lands_on_the_severity_gate(self) -> None:
        reason = "deps.yaml names a dependency the registry does not carry"
        c = self.conductor(_envelope({"ir": None, "last_fail_reason": reason}))
        outcome = c.run_substep(self.refs, "compile", "generate")
        decision = c.classify_failure(self.refs, "compile", [outcome])
        # dev terminalizes a `major`; the point is that it is NOT a document retry.
        self.assertNotEqual(decision.action, "retry")
        self.assertIn(decision.action, ("fail_closed", "escalate"))

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
        c = self.conductor(_envelope(_doc()))

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
        c = self.conductor(_envelope(_verdict("fail", "minor", "step_03 has no update target")))
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
        self.assertEqual(meta["last_fail_reason"], "step_03 has no update target")
        decision = c.classify_failure(self.refs, "compile", [None, None, outcome])
        # `minor` is the verify-severity gate's warm same-phase repair. `target_phase` is None
        # there BY DESIGN — the caller reopens the phase it is already in — and the reason is what
        # selects the meta the findings come from.
        self.assertEqual(decision.action, "retry")
        self.assertEqual(decision.repair_strategy, "reuse")
        self.assertEqual(decision.reason, "verify_minor")
        self.assertEqual(
            c._read_repair_findings(self.refs, decision.reason, "compile"),
            "step_03 has no update target")

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
        c = self.conductor(_envelope(_doc()))
        c.run_substep(self.refs, "compile", "generate")
        row = _finalized(c)[-1]
        self.assertEqual(row["status"], "pass")
        self.assertEqual(row["result_summary"],
                         "pure_compile_pass: IR accepted (attempts=1)")

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
        reason = "tests.md declares no test_id"
        c = self.conductor(_envelope({"ir": None, "last_fail_reason": reason}))
        c.run_substep(self.refs, "compile", "generate")
        self.assertEqual(
            _finalized(c)[-1]["result_summary"],
            f"pure_compile_fail: {wc.COMPILE_DECLARED_FAIL}: {reason}")

    def test_the_producer_tombstone_reasons_name_the_compile_repair(self) -> None:
        """Both directions: a repaired PASS and an exhausted FAIL tombstone their superseded
        attempts, and the two reasons are different strings."""
        c = self.conductor(_envelope({"ir": _valid_ir()}), _envelope(_doc()))
        c.run_substep(self.refs, "compile", "generate")
        self.assertEqual(_superseded_reasons(c),
                         ["pure_ir_document_repair_superseded_pass: attempts=2"])

        c2 = self.conductor(_envelope({"ir": _valid_ir()}))
        c2.run_substep(self.refs, "compile", "generate")
        self.assertEqual(
            _superseded_reasons(c2),
            [f"pure_ir_document_repair_superseded: {wc.COMPILE_IR_DOCUMENT_VIOLATION}"])

    def test_the_producer_host_write_failure_reason_names_the_compile_pair(self) -> None:
        """The tombstone half of the host-write recovery, which the outcome-level row above does
        not reach: it fires only when an EARLIER attempt was superseded."""
        c = self.conductor(_envelope({"ir": _valid_ir()}), _envelope(_doc()))

        def boom(*a, **k):
            raise OSError("no space left on device")

        c._write_pure_ir_artifacts = boom  # type: ignore[assignment]
        outcome = c.run_substep(self.refs, "compile", "generate")
        self.assertEqual(outcome.infra_error[0], "pure_compile_host_write_failed")
        self.assertEqual(_superseded_reasons(c),
                         ["pure_compile_host_write_failed_superseded: OSError"])

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
        self.assertEqual(
            _superseded_reasons(c),
            [f"pure_ir_verdict_repair_superseded: {wc.GENERATE_VERDICT_SCHEMA_VIOLATION}"])

    def test_a_non_object_reply_says_a_verdict_was_expected(self) -> None:
        """`non_object_findings` reaches the repair turn's `repair_findings`, which is the only
        place the reviewer is told what shape it got wrong."""
        c = self.conductor(_envelope("[1, 2, 3]"), _envelope(_verdict()))
        outcome = c.run_substep(self.refs, "compile", "verify")
        self.assertEqual(outcome.status, "pass")
        repair = [cap["--request-json"] for s, cap in c.calls if s == "record-launch"][1]
        self.assertEqual(repair["repair_reason"], "pure_ir_verdict_repair")
        self.assertIn("expected a verdict", repair["repair_findings"])

    def test_the_reviewer_host_write_failure_reason_names_the_compile_pair(self) -> None:
        c = self.conductor(_envelope({"verification_status": "maybe"}), _envelope(_verdict()))

        def boom(*a, **k):
            raise OSError("no space left on device")

        c._write_compile_verify_meta = boom  # type: ignore[assignment]
        outcome = c.run_substep(self.refs, "compile", "verify")
        self.assertEqual(outcome.infra_error[0], "pure_compile_verify_host_write_failed")
        self.assertEqual(_superseded_reasons(c),
                         ["pure_compile_verify_host_write_failed_superseded: OSError"])

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
class PureCompileSubstepStatusTests(_Fixture):
    def test_a_stale_ir_beside_a_failing_meta_does_not_pass(self) -> None:
        """`determine_substep_status`'s generic tail treats an EMPTY `allowed_output_paths` as
        "every deliverable is fresh", which is a fail-open. The pure path returns before it, so
        this branch is defence in depth — and it is what stops a declared-fail attempt from
        reading as a pass because an earlier attempt's `spec.ir.yaml` is still on disk."""
        ir_dir = self.repo / self.refs.ir_ref
        (ir_dir / "spec.ir.yaml").write_text(yaml.safe_dump(_valid_ir()), encoding="utf-8")
        (ir_dir / "compile_generate_meta.json").write_text(
            json.dumps({"result": "fail", "failure_category": wc.COMPILE_DECLARED_FAIL,
                        "attempts": 1, "per_attempt": []}), encoding="utf-8")
        c = self.conductor()
        status, refs_out = c.determine_substep_status(
            self.refs, "compile", "generate", [])
        self.assertEqual(status, "fail")
        self.assertEqual(refs_out, [])

    def test_a_fresh_ir_beside_a_passing_meta_passes(self) -> None:
        ir_dir = self.repo / self.refs.ir_ref
        (ir_dir / "spec.ir.yaml").write_text(yaml.safe_dump(_valid_ir()), encoding="utf-8")
        (ir_dir / "compile_generate_meta.json").write_text(
            json.dumps({"result": "pass", "failure_category": None,
                        "attempts": 1, "per_attempt": []}), encoding="utf-8")
        c = self.conductor()
        status, _ = c.determine_substep_status(self.refs, "compile", "generate", [])
        self.assertEqual(status, "pass")

    def test_a_pass_meta_with_a_stale_ir_does_not_pass(self) -> None:
        ir_dir = self.repo / self.refs.ir_ref
        (ir_dir / "spec.ir.yaml").write_text(yaml.safe_dump(_valid_ir()), encoding="utf-8")
        (ir_dir / "compile_generate_meta.json").write_text(
            json.dumps({"result": "pass", "failure_category": None,
                        "attempts": 1, "per_attempt": []}), encoding="utf-8")
        c = self.conductor()
        stale_after = (ir_dir / "spec.ir.yaml").stat().st_mtime + 1000
        status, _ = c.determine_substep_status(
            self.refs, "compile", "generate", [], stale_after)
        self.assertEqual(status, "fail")


# ======================================================================================
# Provider matrix
# ======================================================================================
class PureCompileProviderMatrixTests(_Fixture):
    def test_a_capability_restricted_entry_keeps_the_compile_leaves_agentic(self) -> None:
        c = _PureFakeConductor(repo_root=self.repo, orchestration_id="o",
                               orchestration_agent_run_id="orch",
                               llm_config=_agentic_cfg("claude"), env={})
        for substep in ("generate", "verify"):
            self.assertFalse(c._pure_leaf_substep(self.refs, "compile", substep))

    def test_both_compile_leaves_are_pure_without_any_ir_on_disk(self) -> None:
        """No shape condition: at `compile.generate` time there is no IR to read a shape from,
        which is exactly why the predicate cannot use the generate pair's M3c test here."""
        self.assertFalse((self.repo / self.refs.ir_ref / "spec.ir.yaml").exists())
        c = self.conductor()
        for substep in ("generate", "verify"):
            self.assertTrue(c._pure_leaf_substep(self.refs, "compile", substep))

    def test_the_declared_pure_set_is_exactly_the_four_migrated_pairs(self) -> None:
        self.assertEqual(
            sorted(lc.PURE_CAPABLE_SUBSTEPS),
            sorted(ort.PURE_CONTEXT_REQUIRED_KEYS))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
