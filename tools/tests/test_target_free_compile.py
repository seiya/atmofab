"""R4-a PR-3 (issue #284): the IR is target-free and the harness is the target's.

What each class pins:

- `PipelineClosureTests` — `pipeline_closure_nodes`: a non-`infrastructure` node's pipeline
  closure is its target's harness, first, then the Compile sidecar's closure; an
  `infrastructure` node's is the sidecar's alone; a sidecar that already lists the harness does
  not list it twice.
- `WithTargetHarnessTests` — `with_target_harness`: the harness joins a dependency block's
  `direct_deps` and `all_nodes` (only the lists the block has), never twice, never for an
  `infrastructure` node, and not at all without a target.
- `DerivationKeyTests` — the compile key names no target (no `toolchain_document`, no harness
  in its closure, the same key for two targets) and the generate / build keys bind the target's
  harness through the pipeline closure and the generate `harness` member.
- `LaunchGateDirectSetTests` — the launch gate's direct set gains the target's harness on a
  non-`infrastructure` node, the kind read from the catalog.
- `DependencyGraphRefusesAHarnessEdgeTests` — `build_dependency_graph` refuses an
  `infrastructure` entry in `deps.yaml`.
- `ClosureScheduleTests` — `--with-deps`' closure schedules the target's harness and makes
  every non-`infrastructure` member wait on it.
- `InitialReadinessTests` — the persisted initial readiness does not record a node with a
  harness as a trivial leaf, and `write_preflight` computes it for the orchestration's target.
- `DependencyFactsTests` — `_resolve_dependency_facts` (the producer's dependency facts) carries
  the target's harness beside the IR's direct dependencies.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("ATMOFAB_DEP_READINESS_ALLOW_PERSISTED_FALLBACK", "1")

import tools.orchestration_runtime as ort
from tools import target_profile as tp
from tools.tests.orchestration_fixtures import (
    EMPTY_DEPS_YAML,
    accept_any_certified_ir,
    certify_node,
    ensure_spec_entry,
    ensure_target_harness_certified,
)
from tools.tests.target_fixtures import (
    FORTRAN_CPU,
    SECOND_TARGET,
    install_target_profile,
    profile_with,
)

_NK = "component/spec_x@0.1.0"
_HARNESS_ID = FORTRAN_CPU.harness["infrastructure_id"]


def _repo_with_harness(tmp: str, version: str = "0.7.0") -> tuple[Path, str]:
    repo = Path(tmp)
    install_target_profile(repo, FORTRAN_CPU)
    harness_nk = f"infrastructure/{_HARNESS_ID}@{version}"
    ensure_spec_entry(repo, harness_nk)
    return repo, harness_nk


def _write_sidecar(repo: Path, ir_ref: str, doc: dict) -> None:
    path = repo / ir_ref / "dependency_graph.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


class PipelineClosureTests(unittest.TestCase):
    def test_the_harness_comes_first_then_the_sidecar_closure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, harness_nk = _repo_with_harness(tmp)
            ir_ref = "workspace/ir/component__spec_x__0.1.0/spec-x_20260101_001"
            _write_sidecar(repo, ir_ref, {"all_nodes": [
                {"node_key": _NK, "topo_level": 2},
                {"node_key": "component/b@0.1.0", "topo_level": 1},
                {"node_key": "component/a@0.1.0", "topo_level": 0},
            ]})
            self.assertEqual(
                ort.pipeline_closure_nodes(repo, ir_ref, _NK, FORTRAN_CPU),
                [harness_nk, "component/a@0.1.0", "component/b@0.1.0"])

    def test_a_leaf_node_closure_is_the_harness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, harness_nk = _repo_with_harness(tmp)
            ir_ref = "workspace/ir/component__spec_x__0.1.0/spec-x_20260101_001"
            _write_sidecar(repo, ir_ref, {"all_nodes": [{"node_key": _NK, "topo_level": 0}]})
            self.assertEqual(ort.pipeline_closure_nodes(repo, ir_ref, _NK, FORTRAN_CPU),
                             [harness_nk])

    def test_an_infrastructure_node_closure_is_its_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, harness_nk = _repo_with_harness(tmp)
            ir_ref = "workspace/ir/x/y_20260101_001"
            _write_sidecar(repo, ir_ref, {"all_nodes": [{"node_key": harness_nk,
                                                         "topo_level": 0}]})
            self.assertEqual(ort.pipeline_closure_nodes(repo, ir_ref, harness_nk, FORTRAN_CPU),
                             [])

    def test_a_sidecar_that_lists_the_harness_does_not_list_it_twice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, harness_nk = _repo_with_harness(tmp)
            ir_ref = "workspace/ir/component__spec_x__0.1.0/spec-x_20260101_001"
            _write_sidecar(repo, ir_ref, {"all_nodes": [
                {"node_key": _NK, "topo_level": 1},
                {"node_key": harness_nk, "topo_level": 0},
            ]})
            self.assertEqual(ort.pipeline_closure_nodes(repo, ir_ref, _NK, FORTRAN_CPU),
                             [harness_nk])

    def test_the_harness_is_the_one_the_profile_names(self) -> None:
        """The catalog's highest version the profile's constraint matches — narrowed profile,
        narrowed harness."""
        with tempfile.TemporaryDirectory() as tmp:
            repo, high = _repo_with_harness(tmp, "0.7.0")
            ensure_spec_entry(repo, f"infrastructure/{_HARNESS_ID}@0.4.0")
            ir_ref = "workspace/ir/component__spec_x__0.1.0/spec-x_20260101_001"
            _write_sidecar(repo, ir_ref, {"all_nodes": [{"node_key": _NK, "topo_level": 0}]})
            self.assertEqual(ort.pipeline_closure_nodes(repo, ir_ref, _NK, FORTRAN_CPU), [high])
            narrowed = profile_with(harness={"version_constraint": ">=0.3.0 <0.5.0"})
            self.assertEqual(ort.pipeline_closure_nodes(repo, ir_ref, _NK, narrowed),
                             [f"infrastructure/{_HARNESS_ID}@0.4.0"])

    def test_a_harness_the_catalog_does_not_resolve_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            ensure_spec_entry(repo, _NK)
            ir_ref = "workspace/ir/component__spec_x__0.1.0/spec-x_20260101_001"
            _write_sidecar(repo, ir_ref, {"all_nodes": [{"node_key": _NK, "topo_level": 0}]})
            with self.assertRaises(tp.TargetProfileError):
                ort.pipeline_closure_nodes(repo, ir_ref, _NK, FORTRAN_CPU)


class WithTargetHarnessTests(unittest.TestCase):
    def test_the_harness_joins_the_lists_the_block_has(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, harness_nk = _repo_with_harness(tmp)
            ir_dep = {"node_key": _NK,
                      "direct_deps": [{"node_key": "component/a@0.1.0", "operations": ["x"]}]}
            out = ort.with_target_harness(ir_dep, repo, _NK, FORTRAN_CPU)
            self.assertEqual([d["node_key"] for d in out["direct_deps"]],
                             ["component/a@0.1.0", harness_nk])
            self.assertEqual(out["direct_deps"][-1],
                             {"node_key": harness_nk, "kind": "infrastructure", "operations": []})
            self.assertNotIn("all_nodes", out)
            # The input is not mutated.
            self.assertEqual(len(ir_dep["direct_deps"]), 1)
            sidecar = {"all_nodes": [{"node_key": _NK, "topo_level": 0}], "transitive_deps": []}
            out = ort.with_target_harness(sidecar, repo, _NK, FORTRAN_CPU)
            self.assertEqual(out["all_nodes"][0], {"node_key": harness_nk, "topo_level": 0})
            self.assertNotIn("direct_deps", out)

    def test_no_harness_twice_none_for_infrastructure_and_none_without_a_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, harness_nk = _repo_with_harness(tmp)
            listed = {"direct_deps": [harness_nk], "all_nodes": [{"node_key": harness_nk}]}
            self.assertEqual(ort.with_target_harness(listed, repo, _NK, FORTRAN_CPU), listed)
            block = {"direct_deps": [], "all_nodes": []}
            self.assertEqual(ort.with_target_harness(block, repo, harness_nk, FORTRAN_CPU), block)
            self.assertEqual(ort.with_target_harness(block, repo, _NK, None), block)
            self.assertEqual(ort.with_target_harness(None, repo, _NK, FORTRAN_CPU), {})


class DerivationKeyTests(unittest.TestCase):
    def setUp(self) -> None:
        patch = accept_any_certified_ir()
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_compile_key_names_no_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = certify_node(repo, "o1", _NK, through="compile")
            install_target_profile(repo, SECOND_TARGET)
            first = ort.phase_derivation(repo, node_key=_NK, step="compile",
                                         spec_ref="spec/component/spec_x", target=FORTRAN_CPU)
            second = ort.phase_derivation(repo, node_key=_NK, step="compile",
                                          spec_ref="spec/component/spec_x", target=SECOND_TARGET)
            self.assertEqual(set(first["derivation_inputs"]),
                             {"spec", "profiles", "dependency_graph", "closure",
                              "dependency_surface"})
            self.assertEqual(first["derivation_inputs"]["closure"], [])
            self.assertEqual(first["derivation_key"], second["derivation_key"])
            self.assertTrue(refs["ir_ref"])

    def test_the_generate_and_build_keys_bind_the_targets_harness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = certify_node(repo, "o1", _NK, through="build")
            harness_nk = tp.harness_node_key_for_target(repo, FORTRAN_CPU)
            gen = json.loads((repo / refs["source_meta"]).read_text())["derivation_inputs"]
            self.assertEqual(gen["harness"]["node_key"], harness_nk)
            self.assertEqual([c["node_key"] for c in gen["closure"]], [harness_nk])
            self.assertTrue(gen["closure"][0]["ir"].startswith("sha256:"))
            self.assertTrue(gen["closure"][0]["source"].startswith("sha256:"))
            build = json.loads((repo / refs["binary_meta"]).read_text())["derivation_inputs"]
            self.assertEqual([c["node_key"] for c in build["closure"]], [harness_nk])
            # The selected consumer chain stands on the certified harness.
            resolver = ort.DerivationResolver(repo, target=FORTRAN_CPU)
            self.assertTrue(resolver.select(_NK, "build").ok)

    def test_an_uncertified_harness_leaves_the_consumer_unresolvable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = certify_node(repo, "o1", _NK, through="compile")
            ensure_spec_entry(repo, f"infrastructure/{_HARNESS_ID}@0.7.0")
            with self.assertRaises(ort.DerivationInputsUnresolvable) as cm:
                ort.phase_derivation_inputs(repo, node_key=_NK, step="generate",
                                            spec_ref="spec/component/spec_x",
                                            ir_ref=refs["ir_ref"], target=FORTRAN_CPU)
            self.assertIn(_HARNESS_ID, str(cm.exception))


class LaunchGateDirectSetTests(unittest.TestCase):
    def setUp(self) -> None:
        patch = accept_any_certified_ir()
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_non_infrastructure_node_gains_the_harness_and_an_infrastructure_one_does_not(
            self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            ensure_spec_entry(repo, _NK)
            harness_nk = ensure_target_harness_certified(repo, "o1", FORTRAN_CPU)
            snap = ort._certify_and_collect_dep_artifacts(
                repo, "spec/component/spec_x", target=FORTRAN_CPU)
            self.assertTrue(snap["has_entries"])
            self.assertEqual(snap["certified_entries"],
                             [("infrastructure", _HARNESS_ID, harness_nk.rsplit("@", 1)[1], 3)])
            # Without a target the deps.yaml set is all there is (an empty one here).
            self.assertFalse(ort._certify_and_collect_dep_artifacts(
                repo, "spec/component/spec_x")["has_entries"])
            harness_ref = ort.resolve_spec_ref_for(repo, "infrastructure", _HARNESS_ID)
            self.assertFalse(ort._certify_and_collect_dep_artifacts(
                repo, harness_ref, target=FORTRAN_CPU)["has_entries"])

    def test_an_uncertified_harness_makes_the_node_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, _harness_nk = _repo_with_harness(tmp)
            ensure_spec_entry(repo, _NK)
            verified, certified, reason = ort._compute_dep_readiness(
                repo, "spec/component/spec_x", target=FORTRAN_CPU)
            self.assertIsNone(reason)
            self.assertFalse(verified["ir_ref_verified"])
            self.assertEqual(certified[0]["spec_id"], _HARNESS_ID)
            details = ort._stale_dependency_details(
                repo, "spec/component/spec_x", target=FORTRAN_CPU)
            self.assertEqual(len(details), 1, details)
            self.assertIn(_HARNESS_ID, details[0])


class DependencyGraphRefusesAHarnessEdgeTests(unittest.TestCase):
    def test_an_infrastructure_entry_is_refused(self) -> None:
        from tools.dependency_graph import build_dependency_graph

        with tempfile.TemporaryDirectory() as tmp:
            repo, _harness_nk = _repo_with_harness(tmp)
            ensure_spec_entry(repo, _NK, deps_yaml=(
                "dependencies:\n  components: []\n  profiles: []\n  infrastructure:\n"
                f"    - infrastructure_id: {_HARNESS_ID}\n"
                "      version_constraint: \">=0.3.0 <1.0.0\"\n"))
            graph, error = build_dependency_graph(
                repo, target_spec_ref="spec/component/spec_x", target_node_key=_NK)
            self.assertIsNone(graph)
            self.assertEqual(error["reason"], "infrastructure_dependency_declared_in_deps")
            ensure_spec_entry(repo, _NK, deps_yaml=EMPTY_DEPS_YAML)
            graph, error = build_dependency_graph(
                repo, target_spec_ref="spec/component/spec_x", target_node_key=_NK)
            self.assertIsNone(error)
            self.assertEqual([n["node_key"] for n in graph["all_nodes"]], [_NK])


class ClosureScheduleTests(unittest.TestCase):
    def test_the_harness_is_a_member_every_other_member_waits_on(self) -> None:
        from tools.run_workflow import _resolve_dependency_closure

        with tempfile.TemporaryDirectory() as tmp:
            repo, harness_nk = _repo_with_harness(tmp)
            ensure_spec_entry(repo, "component/a@0.1.0")
            ensure_spec_entry(repo, _NK, deps_yaml=(
                "dependencies:\n  components:\n    - component_id: a\n"
                "      version_constraint: \">=0.1.0\"\n  profiles: []\n"))
            harness_ref = ort.resolve_spec_ref_for(repo, "infrastructure", _HARNESS_ID)
            ordered, error = _resolve_dependency_closure(
                repo, "spec/component/spec_x", FORTRAN_CPU)
            self.assertIsNone(error)
            self.assertEqual([n["spec_ref"] for n in ordered],
                             [harness_ref, "spec/component/a"])
            by_ref = {n["spec_ref"]: n for n in ordered}
            self.assertEqual(by_ref[harness_ref]["direct_deps"], [])
            self.assertEqual(by_ref["spec/component/a"]["direct_deps"], [harness_ref])
            self.assertEqual(by_ref[harness_ref]["spec_versions"],
                             [harness_nk.rsplit("@", 1)[1]])
            # Without a target the closure is deps.yaml's alone.
            ordered, error = _resolve_dependency_closure(repo, "spec/component/spec_x")
            self.assertIsNone(error)
            self.assertEqual([n["spec_ref"] for n in ordered], ["spec/component/a"])

    def test_a_declared_harness_is_refused_at_closure_build(self) -> None:
        from tools.run_workflow import _resolve_dependency_closure

        with tempfile.TemporaryDirectory() as tmp:
            repo, _harness_nk = _repo_with_harness(tmp)
            ensure_spec_entry(repo, _NK, deps_yaml=(
                "dependencies:\n  components: []\n  profiles: []\n  infrastructure:\n"
                f"    - infrastructure_id: {_HARNESS_ID}\n"
                "      version_constraint: \">=0.3.0 <1.0.0\"\n"))
            _ordered, error = _resolve_dependency_closure(
                repo, "spec/component/spec_x", FORTRAN_CPU)
            self.assertEqual(error["reason"], "infrastructure_dependency_declared_in_deps")


class InitialReadinessTests(unittest.TestCase):
    def test_a_node_with_a_harness_is_not_a_trivial_leaf_under_a_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, _harness_nk = _repo_with_harness(tmp)
            ensure_spec_entry(repo, _NK)
            leaf = ort._compute_initial_dependency_readiness(repo, "spec/component/spec_x")
            self.assertTrue(leaf["direct_dependency_compile_readiness"])
            self.assertEqual(leaf["certified_deps"], [])
            with_target = ort._compute_initial_dependency_readiness(
                repo, "spec/component/spec_x", target=FORTRAN_CPU)
            self.assertFalse(with_target["direct_dependency_compile_readiness"])
            self.assertNotIn("certified_deps", with_target)
            harness_ref = ort.resolve_spec_ref_for(repo, "infrastructure", _HARNESS_ID)
            self.assertTrue(ort._compute_initial_dependency_readiness(
                repo, harness_ref, target=FORTRAN_CPU)["direct_dependency_compile_readiness"])

    def test_write_preflight_computes_it_for_the_orchestrations_target(self) -> None:
        """The persisted record `write_preflight` writes asks the question for the target the
        orchestration was launched for (`invocation.target`). Without the target a node whose
        only dependency is the uncertified harness reads as a trivial leaf, ready."""
        with tempfile.TemporaryDirectory() as tmp:
            repo, _harness_nk = _repo_with_harness(tmp)
            ensure_spec_entry(repo, _NK)
            ort.init_orchestration(
                repo_root=repo, orchestration_id="o1", spec_ref="spec/component/spec_x",
                invocation={"until_phase": "validate",
                            "target": {"target_id": FORTRAN_CPU.target_id}})
            ort.write_preflight(
                repo_root=repo, orchestration_id="o1",
                payload={"status": "pass", "sandbox_runtime": "bwrap",
                         "sandbox_enforced": True, "can_launch_step_agents": True,
                         "can_launch_substep_agents": True,
                         "feature_states": {"multi_agent": True, "hooks": True},
                         "checks": [{"name": "multi_agent_enabled", "pass": True},
                                    {"name": "hooks_enabled", "pass": True},
                                    {"name": "codex_home_writable", "pass": True},
                                    {"name": "sandbox_bwrap_available", "pass": True},
                                    {"name": "sandbox_bwrap_userns", "pass": True}]})
            meta = json.loads((repo / "workspace" / "orchestrations" / "o1"
                               / "orchestration_meta.json").read_text(encoding="utf-8"))
            self.assertFalse(
                meta["dependency_readiness"]["direct_dependency_compile_readiness"], meta)


class DependencyFactsTests(unittest.TestCase):
    def _facts(self, repo: Path, dependency: dict) -> list[str]:
        ir_ref = "workspace/ir/component__spec_x__0.1.0/spec-x_20260101_001"
        (repo / ir_ref).mkdir(parents=True, exist_ok=True)
        (repo / ir_ref / "spec.ir.yaml").write_text(
            json.dumps({"dependency": dependency}), encoding="utf-8")
        return [f["node_key"] for f in ort._resolve_dependency_facts(
            repo, ir_ref, target=FORTRAN_CPU)]

    def test_the_targets_harness_is_a_fact_of_the_node(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, _ = _repo_with_harness(tmp)
            harness_nk = ensure_target_harness_certified(repo, "orch_dep", FORTRAN_CPU)
            self.assertEqual(
                self._facts(repo, {"node_key": _NK, "direct_deps": []}), [harness_nk])
            # An infrastructure node's facts carry no harness of its own kind.
            self.assertEqual(self._facts(
                repo, {"node_key": "infrastructure/other@0.1.0", "direct_deps": []}), [])



if __name__ == "__main__":
    unittest.main()
