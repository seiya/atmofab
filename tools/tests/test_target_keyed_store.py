"""R4-a PR-2 (issue #284): the store, the keys, readiness and the reservation are per
`node_key × target`.

What each class pins:

- `TargetKeyedCertificationTests` — the plan's verification row: a node certified for one
  target answers `certified` for it and `source_not_found` for a second target whose profile
  differs only in its id (`target_fixtures.SECOND_TARGET`), through `check-phase-certified`
  and through dependency readiness; Compile, which is target-free, answers the same for both.
- `DerivationKeyTargetTests` — the generate / build / validate keys carry the target, the
  compile key does not read it.
- `StoreCoordinateTests` — the store coordinate: where a pipeline lives, how a path names its
  target, and that a pipeline directory from before the change is not a candidate.
- `ReservationAndLaunchRefTests` — a pipeline reservation names its target and a launch's
  `pipeline_ref` carries it.
- `ClaimKeyTests` — the start claim is per spec AND target.
- `ConductorReadsTheTargetTests` / `ValidatorReadsTheTargetTests` / `RunnerRenderTargetTests` —
  the host reads the toolchain, the fixed layer a generate producer is shown, the exemplar, the
  pipeline a validator stage accepts and the runner's perf record off the target, never off
  the IR.
- `EveryStageRefusesAnUnresolvableTargetTests` — the resolve check is wired into post_generate,
  post_build and the execution stages.
- `LoaderRefusesAStoreIdTests` — the loader and the shape check refuse a store-id-shaped id.
- `ValidatorToolchainReadsTests` — the m3c language and the tamper gate's build-graph
  toolchain are the target's, driven with an IR that contradicts it.
- `WorkspaceLayoutTargetLayerTests` — `validate_workspace_root` accepts the target layer and
  checks the pipeline ids beneath it.

Round 0's mutation sweep over this branch found these rows missing; the rows elsewhere it
added are in `test_run_workflow` (claim keys at the closure driver's two sites, the host
probes), `test_workflow_conductor` (`--target` on the reservation and the certification
question), `test_orchestration_runtime` (the per-target release path, the audit-log build
system, no exemplar without a target) and `test_validate_pipeline_semantics` (the OpenMP
floor's and the quality-check commands' toolchain).
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from tools.tests.orchestration_fixtures import ensure_spec_entry
from pathlib import Path

os.environ.setdefault("ATMOFAB_DEP_READINESS_ALLOW_PERSISTED_FALLBACK", "1")

import tools.orchestration_runtime as ort
from tools import target_profile as tp
from tools.orchestration_runtime import init_orchestration, write_preflight
from tools.tests.orchestration_fixtures import accept_any_certified_ir, certify_node
from tools.tests.target_fixtures import (
    FORTRAN_CPU,
    SECOND_TARGET,
    SECOND_TARGET_ID,
    install_target_profile,
    pipe_ref,
)

_NK = "component/spec_x@0.1.0"
_SAFE = "component__spec_x__0.1.0"


def _preflight(repo_root: Path, oid: str = "o1") -> None:
    init_orchestration(repo_root=repo_root, orchestration_id=oid,
                       invocation={"until_phase": "validate"})
    write_preflight(
        repo_root=repo_root,
        orchestration_id=oid,
        payload={
            "status": "pass",
            "sandbox_runtime": "bwrap",
            "sandbox_enforced": True,
            "can_launch_step_agents": True,
            "can_launch_substep_agents": True,
            "feature_states": {"multi_agent": True, "hooks": True},
            "checks": [{"name": "multi_agent_enabled", "pass": True},
                       {"name": "hooks_enabled", "pass": True},
                       {"name": "codex_home_writable", "pass": True},
                       {"name": "sandbox_bwrap_available", "pass": True},
                       {"name": "sandbox_bwrap_userns", "pass": True}],
        },
    )


class TargetKeyedCertificationTests(unittest.TestCase):
    def setUp(self) -> None:
        patch = accept_any_certified_ir()
        patch.start()
        self.addCleanup(patch.stop)

    def _repo(self, tmp: str) -> Path:
        repo = Path(tmp)
        _preflight(repo)
        certify_node(repo, "o1", _NK, through="validate")
        install_target_profile(repo, SECOND_TARGET)
        return repo

    def test_check_phase_certified_answers_per_target(self) -> None:
        """The plan's PR-2 verification row, through the CLI function the conductor calls."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            for step in ("compile", "generate", "build", "validate"):
                out = ort.check_phase_certified(
                    repo_root=repo, orchestration_id="o1", node_key=_NK, step=step,
                    record=False, target_id=FORTRAN_CPU.target_id)
                self.assertTrue(out["certified"], (step, out["reason"]))
            second = ort.check_phase_certified(
                repo_root=repo, orchestration_id="o1", node_key=_NK, step="generate",
                record=False, target_id=SECOND_TARGET_ID)
            self.assertFalse(second["certified"])
            self.assertEqual(second["reason"], "source_not_found")
            # Compile is target-free: the IR stands for every target.
            compiled = ort.check_phase_certified(
                repo_root=repo, orchestration_id="o1", node_key=_NK, step="compile",
                record=False, target_id=SECOND_TARGET_ID)
            self.assertTrue(compiled["certified"], compiled["reason"])

    def test_without_target_the_orchestrations_recorded_one_is_used(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            out = ort.check_phase_certified(
                repo_root=repo, orchestration_id="o1", node_key=_NK, step="validate",
                record=False)
            self.assertTrue(out["certified"], out["reason"])
            # An orchestration that records no target is certified for nothing past Compile.
            meta_path = repo / "workspace/orchestrations/o1/orchestration_meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            del meta["invocation"]["target"]
            meta_path.write_text(json.dumps(meta), encoding="utf-8")
            out = ort.check_phase_certified(
                repo_root=repo, orchestration_id="o1", node_key=_NK, step="build",
                record=False)
            self.assertFalse(out["certified"])
            self.assertEqual(out["reason"], "target_unresolved")

    def test_an_undeclared_target_is_refused_even_for_compile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            with self.assertRaises(tp.TargetProfileError):
                ort.check_phase_certified(
                    repo_root=repo, orchestration_id="o1", node_key=_NK, step="compile",
                    record=False, target_id="no_such_target")

    def test_dependency_readiness_answers_per_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            kind, rest = _NK.split("/", 1)
            sid, version = rest.split("@", 1)
            for stage in ("ir_ref", "pipeline_ref", "aggregate_verdict"):
                ok, why = ort._verify_dep_stage_detail(
                    repo, kind, sid, version, stage, target=FORTRAN_CPU)
                self.assertTrue(ok, (stage, why))
            ok, why = ort._verify_dep_stage_detail(
                repo, kind, sid, version, "pipeline_ref", target=SECOND_TARGET)
            self.assertFalse(ok)
            self.assertIn("source_not_found", str(why))
            # No target at all: the target-free stage answers, the others refuse by name.
            self.assertTrue(ort._verify_dep_stage_detail(
                repo, kind, sid, version, "ir_ref")[0])
            ok, why = ort._verify_dep_stage_detail(repo, kind, sid, version, "pipeline_ref")
            self.assertFalse(ok)
            self.assertIn("target_unresolved", str(why))

    def test_a_pre_target_pipeline_directory_is_not_a_candidate(self) -> None:
        """The pre-R4-a layout (`workspace/pipelines/<safe>/<pipeline_id>/`) is read by
        nothing: moving the certified pipeline there un-certifies it."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            target_dir = repo / tp.pipelines_dir(_SAFE, FORTRAN_CPU.target_id)
            (pipe,) = [p for p in target_dir.iterdir() if p.is_dir()]
            pipe.rename(target_dir.parent / pipe.name)
            resolver = ort.DerivationResolver(repo, target=FORTRAN_CPU)
            self.assertEqual(resolver.select(_NK, "generate").reason, "source_not_found")
            self.assertTrue(resolver.select(_NK, "compile").ok)


class DerivationKeyTargetTests(unittest.TestCase):
    def setUp(self) -> None:
        patch = accept_any_certified_ir()
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_resolver_refuses_a_pipeline_phase_without_a_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            certify_node(repo, "o1", _NK, through="validate")
            resolver = ort.DerivationResolver(repo)
            self.assertTrue(resolver.select(_NK, "compile").ok)
            for step in ("generate", "build", "validate"):
                self.assertEqual(resolver.select(_NK, step).reason, "target_unresolved")

    def test_pipeline_keys_carry_the_target_and_the_compile_key_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = certify_node(repo, "o1", _NK, through="validate")
            spec_ref = ort.DerivationResolver(repo).spec_ref(_NK)
            compile_keys = {
                ort.phase_derivation(repo, node_key=_NK, step="compile", spec_ref=spec_ref,
                                     target=t)["derivation_key"]
                for t in (None, FORTRAN_CPU, SECOND_TARGET)}
            self.assertEqual(len(compile_keys), 1, "the compile key read the target")

            generate = ort.phase_derivation_inputs(
                repo, node_key=_NK, step="generate", spec_ref=spec_ref,
                ir_ref=refs["ir_ref"], target=FORTRAN_CPU)
            self.assertEqual(generate["target"], {"target_id": FORTRAN_CPU.target_id,
                                                  "profile": FORTRAN_CPU.sha256})
            build = ort.phase_derivation_inputs(
                repo, node_key=_NK, step="build", spec_ref=spec_ref, ir_ref=refs["ir_ref"],
                source_ref=f"{refs['pipeline_ref']}/source/{refs['source_id']}",
                target=FORTRAN_CPU)
            tc = FORTRAN_CPU.toolchain
            self.assertEqual(
                {k: build["toolchain"][k] for k in
                 ("target_id", "language", "standard", "build_system", "backend")},
                {"target_id": FORTRAN_CPU.target_id, "language": tc["language"],
                 "standard": tc["standard"], "build_system": tc["build_system"],
                 "backend": FORTRAN_CPU.parallel_backend})
            validate = ort.phase_derivation_inputs(
                repo, node_key=_NK, step="validate", spec_ref=spec_ref,
                ir_ref=refs["ir_ref"],
                binary_ref=f"{refs['pipeline_ref']}/binary/{refs['binary_id']}",
                target=FORTRAN_CPU)
            self.assertEqual(validate["run_policy"], {
                "target_id": FORTRAN_CPU.target_id, "profile": FORTRAN_CPU.sha256,
                "threads_per_rank": FORTRAN_CPU.threads_per_rank, "preset": "make_test"})

    def test_a_pipeline_key_without_a_target_is_unresolvable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = certify_node(repo, "o1", _NK, through="compile")
            with self.assertRaises(ort.DerivationInputsUnresolvable):
                ort.phase_derivation_inputs(
                    repo, node_key=_NK, step="generate",
                    spec_ref=ort.DerivationResolver(repo).spec_ref(_NK),
                    ir_ref=refs["ir_ref"])


class StoreCoordinateTests(unittest.TestCase):
    def test_a_target_id_is_never_a_store_id(self) -> None:
        self.assertTrue(tp.is_target_id(FORTRAN_CPU.target_id))
        self.assertFalse(tp.is_target_id("x_20260101_001"))
        self.assertFalse(tp.is_target_id("Fortran"))
        self.assertFalse(tp.is_target_id(None))
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / tp.TARGETS_DIR).mkdir(parents=True)
            (repo / tp.TARGETS_DIR / "x_20260101_001.yaml").write_text("{}\n", "utf-8")
            with self.assertRaises(tp.TargetProfileError):
                tp.list_target_ids(repo)
            with self.assertRaises(tp.TargetProfileError):
                tp.load_target_profile(repo, "x_20260101_001")

    def test_a_path_names_its_target(self) -> None:
        ref = pipe_ref(_SAFE, "x_20260101_001")
        self.assertEqual(tp.pipeline_target_id(ref), FORTRAN_CPU.target_id)
        self.assertEqual(tp.pipeline_target_id(f"{ref}/source/src_20260101_001/src"),
                         FORTRAN_CPU.target_id)
        self.assertEqual(tp.pipeline_target_id(f"/abs/checkout/{ref}"), FORTRAN_CPU.target_id)
        # The pre-R4-a layout, a bare target directory, and anything else name no target.
        self.assertIsNone(tp.pipeline_target_id(f"workspace/pipelines/{_SAFE}/x_20260101_001"))
        self.assertIsNone(tp.pipeline_target_id(tp.pipelines_dir(_SAFE, FORTRAN_CPU.target_id)))
        self.assertIsNone(tp.pipeline_target_id("workspace/ir/x/y/z"))
        self.assertIsNone(tp.pipeline_target_id(""))

    def test_loading_a_pipelines_target_refuses_a_path_without_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            install_target_profile(repo)
            self.assertEqual(
                tp.load_pipeline_target(repo, pipe_ref(_SAFE, "x_20260101_001")).sha256,
                FORTRAN_CPU.sha256)
            with self.assertRaises(tp.TargetProfileError) as ctx:
                tp.load_pipeline_target(repo, f"workspace/pipelines/{_SAFE}/x_20260101_001")
            self.assertEqual(ctx.exception.reason, "target_unresolved")
            with self.assertRaises(tp.TargetProfileError) as ctx:
                tp.load_pipeline_target(repo, pipe_ref(_SAFE, "x_20260101_001", "absent"))
            self.assertEqual(ctx.exception.reason, "target_profile_invalid")


class ReservationAndLaunchRefTests(unittest.TestCase):
    def test_a_pipeline_reservation_names_its_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(repo_root=repo, orchestration_id="o1")
            kw = {"orchestration_id": "o1", "node_key": _NK,
                  "reserved_id": "spec-x_20260101_001", "reserved_by_agent_run_id": "arid"}
            with self.assertRaises(ValueError):
                ort.reserve_phase_root(repo, step="generate", **kw)
            with self.assertRaises(ValueError):
                ort.reserve_phase_root(repo, step="compile", target_id=FORTRAN_CPU.target_id,
                                       **kw)
            with self.assertRaises(ValueError):
                ort.reserve_phase_root(repo, step="generate", target_id="x_20260101_001", **kw)
            out = ort.reserve_phase_root(
                repo, step="generate", target_id=FORTRAN_CPU.target_id, **kw)
            self.assertEqual(out["target_id"], FORTRAN_CPU.target_id)
            self.assertNotIn(
                "target_id", ort.reserve_phase_root(repo, step="compile", **kw))
            res_dir = repo / "workspace/orchestrations/o1/reservations" / _SAFE
            self.assertEqual(
                ort._reserved_pipeline_dir(repo, res_dir),
                repo / pipe_ref(_SAFE, "spec-x_20260101_001"))

    def test_the_cli_carries_the_target(self) -> None:
        """The conductor reaches both functions through `orchestration_runtime.main`; the
        rows above call them directly, so the flag's wiring is pinned here."""
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _preflight(repo)
            base = ["--repo-root", str(repo), "--orchestration-id", "o1", "--node-key", _NK]
            with contextlib.redirect_stdout(io.StringIO()):
                rc = ort.main(["reserve-phase-root", *base, "--step", "generate",
                               "--reserved-id", "spec-x_20260101_001",
                               "--reserved-by-agent-run-id", "arid",
                               "--target", FORTRAN_CPU.target_id])
            self.assertEqual(rc, 0)
            res = json.loads((repo / "workspace/orchestrations/o1/reservations" / _SAFE
                              / "generate.json").read_text(encoding="utf-8"))
            self.assertEqual(res["target_id"], FORTRAN_CPU.target_id)
            # An undeclared --target on check-phase-certified is refused with exit 1 (the
            # profile is loaded whenever the flag is given, Compile included).
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                rc = ort.main(["check-phase-certified", *base, "--step", "compile",
                               "--no-record", "--target", "no_such_target"])
            self.assertEqual(rc, 1)
            self.assertIn("no_such_target", err.getvalue())

    def test_a_reservation_without_a_target_names_no_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            res_dir = repo / "workspace/orchestrations/o1/reservations" / _SAFE
            res_dir.mkdir(parents=True)
            (res_dir / "generate.json").write_text(json.dumps(
                {"reserved_ir_id": "spec-x_20260101_001"}), encoding="utf-8")
            self.assertIsNone(ort._reserved_pipeline_dir(repo, res_dir))

    def test_a_launch_pipeline_ref_carries_its_target(self) -> None:
        good = pipe_ref(_SAFE, "spec-x_20260101_001")
        ort._validate_canonical_workspace_root_ref(
            ref=good, node_safe=_SAFE, kind="pipelines", label="pipeline_ref")
        for bad in (f"workspace/pipelines/{_SAFE}/spec-x_20260101_001",
                    f"workspace/pipelines/{_SAFE}/spec-x_20260101_002/spec-x_20260101_001",
                    f"{good}/source"):
            with self.assertRaises(ValueError, msg=bad):
                ort._validate_canonical_workspace_root_ref(
                    ref=bad, node_safe=_SAFE, kind="pipelines", label="pipeline_ref")
        # The ir_ref shape is unchanged: target-free.
        ort._validate_canonical_workspace_root_ref(
            ref=f"workspace/ir/{_SAFE}/spec-x_20260101_001", node_safe=_SAFE, kind="ir",
            label="ir_ref")


class ClaimKeyTests(unittest.TestCase):
    def test_the_spec_claim_is_per_target(self) -> None:
        from tools.run_workflow import _claim_lock_path, _spec_claim_key

        spec = "spec/component/x"
        first = _spec_claim_key(spec, FORTRAN_CPU)
        second = _spec_claim_key(spec, SECOND_TARGET)
        self.assertNotEqual(first, second)
        self.assertNotEqual(_claim_lock_path(Path("/r"), "spec", first),
                            _claim_lock_path(Path("/r"), "spec", second))
        self.assertEqual(_spec_claim_key(spec, None), spec)


class ConductorReadsTheTargetTests(unittest.TestCase):
    """The conductor's host reads of the target are the profile's, never the IR's."""

    def _conductor(self, repo: Path, target=FORTRAN_CPU):
        import tools.workflow_conductor as wc
        from tools.tests.llm_samples import sample_config as _cfg
        return wc.Conductor(repo_root=repo, orchestration_id="o1",
                            orchestration_agent_run_id="ORCH", llm_config=_cfg(),
                            target_profile=target)

    def _refs(self):
        import tools.workflow_conductor as wc
        return wc.NodeRefs(node_key=_NK, spec_path="spec/component/spec_x",
                           ir_id="spec-x_20260101_001", pipeline_id="spec-x_20260101_001",
                           target_id=FORTRAN_CPU.target_id)

    def test_a_conductor_without_a_target_refuses_to_read_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            c = self._conductor(Path(tmp), target=None)
            with self.assertRaises(RuntimeError) as ctx:
                c._read_toolchain(self._refs())
            self.assertIn("conductor_target_unresolved", str(ctx.exception))

    def test_the_toolchain_is_the_profiles_whatever_the_ir_declares(self) -> None:
        """An IR declaring another toolchain (the bridge gate refuses it before any phase
        reads it, but the READ must not depend on that) does not move the answer."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = self._refs()
            ir_dir = repo / refs.ir_ref
            ir_dir.mkdir(parents=True)
            (ir_dir / "spec.ir.yaml").write_text(
                "impl_defaults:\n  target: {class: gpu, backend: serial}\n"
                "  toolchain: {language: other, standard: s, build_system: other}\n",
                encoding="utf-8")
            c = self._conductor(repo)
            tc = FORTRAN_CPU.toolchain
            self.assertEqual(c._read_toolchain(refs), {
                "language": tc["language"], "standard": tc["standard"],
                "build_system": tc["build_system"], "compiler": str(tc.get("compiler") or ""),
                "backend": FORTRAN_CPU.parallel_backend})
            self.assertEqual(refs.pipeline_ref, pipe_ref(_SAFE, "spec-x_20260101_001"))

    def test_the_exemplar_is_selected_for_the_conductors_target(self) -> None:
        """Without the target the selector offers nothing (a sibling is certified per
        target), so a conductor that stopped passing it would silently lose every exemplar."""
        from unittest import mock
        with mock.patch("tools.orchestration_runtime._resolve_exemplar_source",
                        autospec=True, return_value={"node_key": "x"}) as select:
            c = self._conductor(Path("/nonexistent"))
            self.assertEqual(c._resolve_exemplar(self._refs()), {"node_key": "x"})
        select.assert_called_once_with(Path("/nonexistent"), self._refs().ir_ref,
                                       target=FORTRAN_CPU)

    def test_the_generate_producer_is_shown_the_whole_profile(self) -> None:
        from tools.tests.target_fixtures import profile_with
        # A profile whose class / backend / architecture differ from the checked-in one, so no
        # literal can stand in for it. Since R4-a PR-3 (issue #284) the producer is shown the
        # profile document itself — there is no IR knob layer to merge it with, and nothing of
        # the IR enters (the method takes no IR).
        target = profile_with(hardware={"class": "fpga", "architecture": "arch_t"},
                              parallel={"backend": "vendor_x"},
                              execution={"threads_per_rank": 5})
        c = self._conductor(Path("/nonexistent"), target=target)
        self.assertEqual(json.loads(c._pure_target_profile_document()), target.doc)


class ValidatorReadsTheTargetTests(unittest.TestCase):
    def test_a_stage_pipeline_root_must_carry_a_target(self) -> None:
        import tools.validate_pipeline_semantics as vps
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            good = repo / pipe_ref(_SAFE, "spec-x_20260101_001")
            legacy = repo / f"workspace/pipelines/{_SAFE}/spec-x_20260101_001"
            good.mkdir(parents=True)
            legacy.mkdir(parents=True)
            self.assertEqual(
                vps._resolve_pipeline_dir_for_stage(repo, "workspace", str(good)),
                good.resolve())
            with self.assertRaises(ValueError):
                vps._resolve_pipeline_dir_for_stage(repo, "workspace", str(legacy))
            with self.assertRaises(ValueError):
                vps._resolve_pipeline_roots(repo, "workspace", [str(legacy)])
            # The whole-workspace sweep reads the per-target tree only.
            self.assertEqual(vps._pipeline_targets(repo / "workspace", None), [good])

    def test_a_pipeline_whose_target_does_not_resolve_is_a_violation(self) -> None:
        import tools.validate_pipeline_semantics as vps
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            install_target_profile(repo)
            known = repo / pipe_ref(_SAFE, "spec-x_20260101_001")
            unknown = repo / pipe_ref(_SAFE, "spec-x_20260101_001", "not_declared")
            violations: list[str] = []
            vps._validate_pipeline_targets_resolve(repo, [known, unknown], violations)
            self.assertEqual(len(violations), 1)
            self.assertIn("not_declared", violations[0])


class EveryStageRefusesAnUnresolvableTargetTests(unittest.TestCase):
    """`_validate_pipeline_targets_resolve` is WIRED into each stage that reads the target —
    the function's own row above does not see a stage that stops calling it. Each stage is
    driven on a pipeline whose store coordinate names a target no profile declares."""

    _REFUSAL = "the target this pipeline was built for does not resolve"

    def _undeclared_pipeline(self, repo: Path) -> str:
        ref = pipe_ref(_SAFE, "spec-x_20260101_001", "not_declared")
        (repo / ref).mkdir(parents=True)
        return ref

    def test_post_generate(self) -> None:
        import tools.validate_pipeline_semantics as vps
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            ref = self._undeclared_pipeline(repo)
            violations = vps._validate_post_generate_stage_impl(repo, "workspace", ref, None)
            self.assertTrue(any(self._REFUSAL in v for v in violations), violations)

    def test_post_build(self) -> None:
        import tools.validate_pipeline_semantics as vps
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            ref = self._undeclared_pipeline(repo)
            violations = vps._validate_post_build_stage_impl(repo, "workspace", ref, None)
            self.assertTrue(any(self._REFUSAL in v for v in violations), violations)

    def test_the_execution_stages(self) -> None:
        """`_validate_impl` (post_execute / pre_judge / the full run): a real execution tree
        whose target profile is then removed from the repository."""
        import tools.validate_pipeline_semantics as vps
        from tools.tests.test_validate_pipeline_semantics import (
            _create_minimal_execution_tree,
            _seed_shape_expr_schema_into,
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _seed_shape_expr_schema_into(repo)
            _create_minimal_execution_tree(
                repo, dep_spec_id="dep_a",
                model_text="module shallow_water2d_model\nend module shallow_water2d_model\n",
                runner_text="program r\nend program r\n",
                run_command=["./simulate"])
            profile = repo / tp.TARGETS_DIR / f"{FORTRAN_CPU.target_id}{tp.TARGET_PROFILE_SUFFIX}"
            self.assertTrue(profile.is_file())  # the fixture declared it; the row removes it
            self.assertFalse(any(self._REFUSAL in v for v in vps.validate(
                repo_root=repo, workspace_root="workspace")))
            profile.unlink()
            violations = vps.validate(repo_root=repo, workspace_root="workspace")
            self.assertTrue(any(self._REFUSAL in v for v in violations), violations)


class LoaderRefusesAStoreIdTests(unittest.TestCase):
    """A target id can never be spelled as a store id, or `<safe>/<target_id>/` and a
    pre-target `<safe>/<pipeline_id>/` could not be told apart. `list_target_ids` refuses it;
    so do the loader and the shape check, which a caller naming the id reaches directly."""

    _STORE_SHAPED = "specx_20260101_001"

    def test_the_probe_is_target_shaped_and_store_shaped(self) -> None:
        # FULL matches: the probe must sit in the overlap, or the rows below observe nothing.
        self.assertIsNotNone(tp.TARGET_ID_PATTERN.fullmatch(self._STORE_SHAPED))
        self.assertIsNotNone(tp.STORE_ID_PATTERN.fullmatch(self._STORE_SHAPED))

    def test_load_refuses_it(self) -> None:
        import yaml
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            doc = {**FORTRAN_CPU.doc, "target_id": self._STORE_SHAPED}
            path = repo / tp.TARGETS_DIR / f"{self._STORE_SHAPED}{tp.TARGET_PROFILE_SUFFIX}"
            path.parent.mkdir(parents=True)
            path.write_text(yaml.safe_dump(doc), encoding="utf-8")
            with self.assertRaisesRegex(tp.TargetProfileError, "store id"):
                tp.load_target_profile(repo, self._STORE_SHAPED)

    def test_the_shape_check_refuses_it(self) -> None:
        doc = {**FORTRAN_CPU.doc, "target_id": self._STORE_SHAPED}
        self.assertTrue(any("store id" in v for v in tp._shape_violations(doc)),
                        tp._shape_violations(doc))
        self.assertFalse(any("target_id" in v for v in tp._shape_violations(FORTRAN_CPU.doc)))


class ValidatorToolchainReadsTests(unittest.TestCase):
    """Validator reads whose inputs agree in every other fixture (the IR's toolchain equals
    the profile's there), driven with an IR that CONTRADICTS the target."""

    def _pipeline_with_ir(self, repo: Path, ir: dict) -> Path:
        import yaml
        install_target_profile(repo)
        ir_ref = f"workspace/ir/{_SAFE}/spec-x_20260101_001"
        (repo / ir_ref).mkdir(parents=True)
        (repo / ir_ref / "spec.ir.yaml").write_text(yaml.safe_dump(ir), encoding="utf-8")
        pipe = repo / pipe_ref(_SAFE, "spec-x_20260101_001")
        pipe.mkdir(parents=True)
        (pipe / "lineage.json").write_text(json.dumps({"node_key": _NK, "ir_ref": ir_ref}),
                                           encoding="utf-8")
        return pipe

    def test_the_m3c_language_is_the_targets(self) -> None:
        import tools.validate_pipeline_semantics as vps
        ir = {"meta": {"spec_kind": "component", "spec_id": "spec_x"},
              "impl_defaults": {"toolchain": {"language": "c", "build_system": "cmake"}},
              "dependency": {"direct_deps": [
                  {"node_key": "infrastructure/harness_fortran_cpu@0.7.0"}]}}
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            pipe = self._pipeline_with_ir(repo, ir)
            execution = vps._stub_execution(pipe, _NK)
            self.assertEqual(vps._execution_m3c_language(repo, execution),
                             FORTRAN_CPU.toolchain["language"])

    def test_the_gate_build_graph_toolchain_is_the_targets(self) -> None:
        import tools.validate_pipeline_semantics as vps
        from tools.tests.target_fixtures import profile_with
        target = profile_with(toolchain={"standard": "f2018", "compiler": "xfc"},
                              parallel={"backend": "serial"})
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            # The pipeline closure starts with the target's harness (issue #284), resolved in
            # the catalog; a stale IR toolchain section moves nothing.
            ensure_spec_entry(repo, f"infrastructure/{target.harness['infrastructure_id']}@0.7.0")
            self._pipeline_with_ir(repo, {"impl_defaults": {
                "toolchain": {"language": "c", "standard": "c99", "build_system": "cmake"},
                "target": {"backend": "openmp"}}})
            toolchain, _closure, _edges = vps._pure_gate_build_graph_inputs(
                repo, f"workspace/ir/{_SAFE}/spec-x_20260101_001", _NK, target)
            self.assertEqual(toolchain, {
                "language": target.toolchain["language"], "standard": "f2018",
                "build_system": target.toolchain["build_system"], "backend": "serial",
                "compiler": "xfc"})


class WorkspaceLayoutTargetLayerTests(unittest.TestCase):
    def test_the_target_layer_is_accepted_and_its_pipeline_ids_are_checked(self) -> None:
        from tools.validate_workspace_root import _scan_workspace_layout
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "workspace"
            (ws / pipe_ref(_SAFE, "spec-x_20260101_001").split("workspace/", 1)[1]).mkdir(
                parents=True)
            self.assertEqual(_scan_workspace_layout(ws), [])
            bad = ws / "pipelines" / _SAFE / FORTRAN_CPU.target_id / "Not_A_Store_Id"
            bad.mkdir()
            violations = _scan_workspace_layout(ws)
            self.assertEqual(len(violations), 1, violations)
            self.assertIn("Not_A_Store_Id", violations[0])


class RunnerRenderTargetTests(unittest.TestCase):
    def test_the_perf_record_names_the_targets_class_and_threads(self) -> None:
        """The rendered runner's perf record reads the target, not the IR's knob."""
        from tools.backends.language.fortran import runner
        self.assertEqual(runner._target_class(FORTRAN_CPU.doc), FORTRAN_CPU.hardware_class)
        self.assertEqual(runner._threads(FORTRAN_CPU.doc), FORTRAN_CPU.threads_per_rank)
        other = {**FORTRAN_CPU.doc, "execution": {"threads_per_rank": 7}}
        self.assertEqual(runner._threads(other), 7)


if __name__ == "__main__":
    unittest.main()
