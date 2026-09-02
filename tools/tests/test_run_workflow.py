#!/usr/bin/env python3
"""Tests for workflow startup bootstrap script."""

from __future__ import annotations

import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from tools import llm_config as lc
from tools import run_workflow
from tools.validate_pipeline_semantics import _BUNDLED_SHAPE_EXPR_SCHEMA_PATH

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_DIR = REPO_ROOT / "docs" / "examples"


def _sample_config(backend: str = "claude") -> lc.LlmConfig:
    """The committed sample configuration for a CLI backend — what an operator copies to the
    `./llm.yaml` a run loads by default."""
    return lc.load_llm_config(SAMPLE_DIR / f"llm_{backend}.example.yaml")


def _seed_default_llm_config_into(repo_root: Path) -> None:
    """Give a scratch `repo_root` the `./llm.yaml` a cold run reads.

    Production has no fallback: without this, every cold `main()` against a scratch tree stops
    at `llm_config_default_missing` before reaching whatever the test is about."""
    repo_root.mkdir(parents=True, exist_ok=True)
    shutil.copy(SAMPLE_DIR / "llm_claude.example.yaml", repo_root / "llm.yaml")


def _recorded_sample_pin(repo_root: Path, backend: str = "claude") -> str:
    """The sample's path as a pin recorded by a run rooted at `repo_root` spells it.

    It must be the same string `_repo_relative` derives for the loaded configuration —
    otherwise the resume gate's path-mismatch arm refuses on a difference the test did not
    mean to introduce. Against a scratch `repo_root` (the samples live in THIS checkout, not
    in it) that string is the absolute path, which `repo_root / recorded` then resolves back
    to the real file."""
    return run_workflow._repo_relative(
        str(SAMPLE_DIR / f"llm_{backend}.example.yaml"), repo_root)


def _seed_shape_expr_schema_into(repo_root: Path) -> None:
    """Copy the validator-bundled shape_expr.schema.json into a tmp repo so
    `run_workflow.main()`'s startup assertion (canonical schema must exist
    at <repo_root>/spec/schema/ir/shape_expr.schema.json) passes for tests
    that exercise normal main() flows. Tests that intentionally exercise the
    missing-schema path must NOT call this helper."""
    target = repo_root / "spec" / "schema" / "ir" / "shape_expr.schema.json"
    if target.is_file():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_BUNDLED_SHAPE_EXPR_SCHEMA_PATH.read_bytes())


_CLAIM_ROOT_TMPDIR: tempfile.TemporaryDirectory | None = None
_SAVED_CLAIM_ROOT: str | None = None


def setUpModule() -> None:
    """Keep start-claim lock files out of the operator's home.

    Every test drives a fresh temporary repo root, and the claim path is keyed on it,
    so without this the suite deposits one 0-byte file per (root, spec/orchestration)
    under `~/.atmofab/start_claims/` on every run — thousands of dentries that nothing
    ever reaps. Production leaves the variable unset.
    """
    global _CLAIM_ROOT_TMPDIR, _SAVED_CLAIM_ROOT
    _SAVED_CLAIM_ROOT = os.environ.get("ATMOFAB_START_CLAIM_ROOT")
    _CLAIM_ROOT_TMPDIR = tempfile.TemporaryDirectory(prefix="atmofab_claims_")
    os.environ["ATMOFAB_START_CLAIM_ROOT"] = _CLAIM_ROOT_TMPDIR.name


def tearDownModule() -> None:
    if _SAVED_CLAIM_ROOT is None:
        os.environ.pop("ATMOFAB_START_CLAIM_ROOT", None)
    else:
        os.environ["ATMOFAB_START_CLAIM_ROOT"] = _SAVED_CLAIM_ROOT
    if _CLAIM_ROOT_TMPDIR is not None:
        _CLAIM_ROOT_TMPDIR.cleanup()


def _unallocatable_pid() -> int:
    """A pid the kernel can never hand out — so a driver block carrying it is foreign
    to every process, including this one.

    Fixtures that fabricate "another process's driver" as `{**our_identity, "pid": X}`
    have X as their only discriminator in `_is_own_driver`, so a merely *unlikely* X
    (a `pid + 1` neighbour, or a fixed constant like 424242) leaves a rare
    nondeterministic failure. `pid_max` is the exclusive upper bound on allocation, so
    `pid_max + 1` is out of range by construction — and `/proc/<it>` can never exist,
    which keeps the real probe answering `dead` if a caller forgets to force a verdict.
    """
    try:
        pid_max = int(Path("/proc/sys/kernel/pid_max").read_text(encoding="utf-8"))
    except (OSError, ValueError):  # non-Linux: fall back to the 64-bit PID_MAX_LIMIT
        pid_max = 4 * 1024 * 1024
    return pid_max + 1


@contextmanager
def _forced_liveness():
    """Route `_probe_driver_liveness` to a `driver.verdict` field on the seeded meta.

    The driver-liveness FLOWS (terminalize / refuse / warn) are what the gate tests
    pin; the /proc classification behind the verdict is pinned separately by
    `DriverLivenessProbeTests`. A seeded block with no `verdict` still goes through the
    real probe, so an unseeded orchestration keeps answering `unknown`.
    """
    original = run_workflow._probe_driver_liveness

    def fake(meta):  # type: ignore[no-untyped-def]
        driver = meta.get("driver") if isinstance(meta, dict) else None
        if isinstance(driver, dict) and isinstance(driver.get("verdict"), str):
            return driver["verdict"]
        return original(meta)

    run_workflow._probe_driver_liveness = fake  # type: ignore[assignment]
    try:
        yield
    finally:
        run_workflow._probe_driver_liveness = original  # type: ignore[assignment]


class RunWorkflowTests(unittest.TestCase):
    def test_collect_failure_analysis_includes_unauthorized_write_violation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            orch_root = repo_root / "workspace" / "orchestrations" / "orch_vio"
            violations = orch_root / "violations"
            violations.mkdir(parents=True, exist_ok=True)
            (orch_root / "orchestration_meta.json").write_text(
                json.dumps({"orchestration_id": "orch_vio", "status": "fail"}, ensure_ascii=False),
                encoding="utf-8",
            )
            (violations / "run_001.unauthorized_write_violation.json").write_text(
                json.dumps(
                    {
                        "agent_run_id": "run_001",
                        "unauthorized_paths": ["workspace/pipelines/x/test3.tmp"],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            analysis = run_workflow._collect_failure_analysis(repo_root, "orch_vio")
            self.assertEqual(len(analysis.get("unauthorized_write_violations", [])), 1)
            decisions = analysis.get("recommended_retry_decisions", [])
            self.assertTrue(isinstance(decisions, list) and decisions)
            self.assertEqual(decisions[0].get("repair_strategy"), "restart")
            self.assertIn("unauthorized_write_violation", str(decisions[0].get("repair_reason")))

    def test_collect_failure_analysis_excludes_superseded_nonpass_runs(self) -> None:
        """A terminal-nonpass agent_run that a *later* same-(node,step,substep) run
        resolved to pass must not be reported as the workflow failure (audit:
        orch_20260615T095217Z_74450292 — a judge timeout superseded by a passing
        re-run produced a false workflow_failed). A genuinely unresolved failure
        (no later pass for its key) is still selected."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch_root = repo_root / "workspace" / "orchestrations" / "orch_sup"
            orch_root.mkdir(parents=True, exist_ok=True)
            (orch_root / "orchestration_meta.json").write_text(
                json.dumps({"orchestration_id": "orch_sup", "status": "pass"}, ensure_ascii=False),
                encoding="utf-8",
            )
            node = "component/x@0.1.0"
            rows = [
                # judge timeout, then a later passing judge re-run of the same key
                {"agent_run_id": "judge_to", "node_key": node, "step": "validate",
                 "substep": "judge", "status": "timeout"},
                {"agent_run_id": "judge_ok", "node_key": node, "step": "validate",
                 "substep": "judge", "status": "pass"},
                # genuinely unresolved failure: no later pass for its key
                {"agent_run_id": "build_fail", "node_key": node, "step": "build",
                 "substep": "", "status": "fail"},
            ]
            (orch_root / "agent_runs.jsonl").write_text(
                "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                encoding="utf-8",
            )
            analysis = run_workflow._collect_failure_analysis(repo_root, "orch_sup")
            failed = analysis.get("failed_agent_run")
            self.assertIsNotNone(failed)
            # The superseded judge timeout must NOT be the reported failure.
            self.assertEqual(failed.get("agent_run_id"), "build_fail")

    def test_collect_failure_analysis_none_when_all_nonpass_superseded(self) -> None:
        """When every terminal-nonpass run was resolved by a later passing re-run of
        the same key, failed_agent_run is None (the run materially passed)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch_root = repo_root / "workspace" / "orchestrations" / "orch_allok"
            orch_root.mkdir(parents=True, exist_ok=True)
            (orch_root / "orchestration_meta.json").write_text(
                json.dumps({"orchestration_id": "orch_allok", "status": "pass"}, ensure_ascii=False),
                encoding="utf-8",
            )
            node = "component/x@0.1.0"
            rows = [
                {"agent_run_id": "verify_blocked", "node_key": node, "step": "generate",
                 "substep": "verify", "status": "blocked"},
                {"agent_run_id": "verify_ok", "node_key": node, "step": "generate",
                 "substep": "verify", "status": "pass"},
                {"agent_run_id": "judge_to", "node_key": node, "step": "validate",
                 "substep": "judge", "status": "timeout"},
                {"agent_run_id": "judge_ok", "node_key": node, "step": "validate",
                 "substep": "judge", "status": "pass"},
            ]
            (orch_root / "agent_runs.jsonl").write_text(
                "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                encoding="utf-8",
            )
            analysis = run_workflow._collect_failure_analysis(repo_root, "orch_allok")
            self.assertIsNone(analysis.get("failed_agent_run"))

    def test_is_valid_failure_analysis_accepts_launch_incident_refs_only(self) -> None:
        """In the degraded dangling-launch path the incident ref is the sole evidence
        (no reason_code/detail, no failed_agent_run). It must count as evidence so the
        canonical failure_analysis.json is not misclassified as stale (Codex P3)."""
        obj = {
            "orchestration_id": "orch_x",
            "status": "fail",
            "orchestration_agent_run_id": "orch_arid_1",
            "reason_code": None,
            "reason_detail": None,
            "failed_agent_run": None,
            "failed_step_results": [],
            "recommended_retry_decisions": [],
            "launch_reply_tail": "",
            "agent_summary_tail": "",
            "launch_incident_refs": [
                "workspace/orchestrations/orch_x/launch_incident.runtime.0123456789ab.json"
            ],
        }
        self.assertTrue(
            run_workflow._is_valid_failure_analysis(
                obj, "orch_x", orchestration_agent_run_id="orch_arid_1"
            )
        )
        # With no evidence at all (empty incident refs too), it is invalid.
        obj_no_evidence = {**obj, "launch_incident_refs": []}
        self.assertFalse(
            run_workflow._is_valid_failure_analysis(
                obj_no_evidence, "orch_x", orchestration_agent_run_id="orch_arid_1"
            )
        )

    def test_collect_failure_analysis_includes_launch_incident_refs(self) -> None:
        """A `launch_incident.runtime.*.json` snapshot is linked from failure_analysis."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch_root = repo_root / "workspace" / "orchestrations" / "orch_inc"
            orch_root.mkdir(parents=True, exist_ok=True)
            (orch_root / "orchestration_meta.json").write_text(
                json.dumps({"orchestration_id": "orch_inc", "status": "fail"}, ensure_ascii=False),
                encoding="utf-8",
            )
            snap = orch_root / "launch_incident.runtime.0123456789ab.json"
            snap.write_text(json.dumps({"schema": "launch_incident/v1"}), encoding="utf-8")
            analysis = run_workflow._collect_failure_analysis(repo_root, "orch_inc")
            self.assertEqual(
                analysis.get("launch_incident_refs"),
                ["workspace/orchestrations/orch_inc/launch_incident.runtime.0123456789ab.json"],
            )





    def test_discover_source_dependency_ref_from_file_spec_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            spec_dir = repo_root / "spec" / "problem"
            spec_dir.mkdir(parents=True, exist_ok=True)
            (spec_dir / "test.md").write_text("spec\n", encoding="utf-8")
            (spec_dir / "deps.yaml").write_text("nodes: []\n", encoding="utf-8")

            dep_ref = run_workflow._discover_source_dependency_ref(repo_root, "spec/problem/test.md")
            self.assertEqual(dep_ref, "spec/problem/deps.yaml")

    def test_discover_source_dependency_ref_from_directory_spec_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            spec_dir = repo_root / "spec" / "problem"
            spec_dir.mkdir(parents=True, exist_ok=True)
            (spec_dir / "deps.yaml").write_text("nodes: []\n", encoding="utf-8")

            dep_ref = run_workflow._discover_source_dependency_ref(repo_root, "spec/problem")
            self.assertEqual(dep_ref, "spec/problem/deps.yaml")

    def test_discover_source_dependency_ref_from_spec_root_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            spec_dir = repo_root / "spec"
            spec_dir.mkdir(parents=True, exist_ok=True)
            (spec_dir / "task.md").write_text("spec\n", encoding="utf-8")
            (spec_dir / "deps.yaml").write_text("nodes: []\n", encoding="utf-8")

            dep_ref = run_workflow._discover_source_dependency_ref(repo_root, "spec/task.md")
            self.assertEqual(dep_ref, "spec/deps.yaml")

    def test_discover_source_dependency_ref_rejects_missing_deps_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            spec_dir = repo_root / "spec" / "problem"
            spec_dir.mkdir(parents=True, exist_ok=True)
            (spec_dir / "test.md").write_text("spec\n", encoding="utf-8")

            with self.assertRaises(ValueError):
                run_workflow._discover_source_dependency_ref(repo_root, "spec/problem/test.md")

    def test_validate_source_dependency_ref_rejects_non_spec_deps_path(self) -> None:
        with self.assertRaises(ValueError):
            run_workflow._validate_source_dependency_ref("workspace/ir/x/spec.ir.yaml")

    def test_normalize_phase_accepts_known_values(self) -> None:
        self.assertEqual(run_workflow._normalize_phase("compile"), "Compile")
        self.assertEqual(run_workflow._normalize_phase("VALIDATE"), "Validate")

    def test_normalize_phase_rejects_unknown_value(self) -> None:
        with self.assertRaises(ValueError):
            run_workflow._normalize_phase("spec")

    def test_new_orchestration_id_prefix(self) -> None:
        value = run_workflow._new_orchestration_id()
        self.assertTrue(value.startswith("orch_"))

    def test_preflight_pass_conditions(self) -> None:
        ok, detail = run_workflow._ensure_preflight_pass(
            {
                "status": "pass",
                "can_launch_step_agents": True,
                "can_launch_substep_agents": True,
            }
        )
        self.assertTrue(ok)
        self.assertEqual(detail, "pass")

    def test_preflight_fail_conditions(self) -> None:
        ok, detail = run_workflow._ensure_preflight_pass(
            {
                "status": "fail",
                "can_launch_step_agents": False,
                "can_launch_substep_agents": True,
            }
        )
        self.assertFalse(ok)
        self.assertIn("status='fail'", detail)
        self.assertIn("can_launch_step_agents=False", detail)

    def test_prompt_contains_required_inputs(self) -> None:
        text = run_workflow._build_orchestration_prompt(
            orchestration_id="orch_test",
            orchestration_agent_run_id="run_orch_001",
            spec_ref="spec/problem/sample.md",
            source_dependency_ref="spec/problem/deps.yaml",
            until_phase="Validate",
            workflow_mode="dev",
        )
        self.assertIn("orch_test", text)
        self.assertIn("run_orch_001", text)
        # Load-bearing resume markers parsed by _extract_prompt_params.
        self.assertIn("target_spec_ref: `spec/problem/sample.md`", text)
        self.assertIn("end phase: `Validate`", text)
        self.assertIn("workflow_mode: `dev`", text)
        self.assertIn("dependency_ref: `spec/problem/deps.yaml`", text)
        self.assertNotIn("(not specified)", text)
        # Conductor-only: the record is no longer an LLM prompt.
        self.assertIn("driver: conductor", text)

    def test_parse_args_defaults(self) -> None:
        ns = run_workflow._parse_args(["spec/problem.md", "generate"])
        # --mode defaults to None so main() can tell "omitted" from "explicitly passed";
        # the historical dev default is applied in main().
        self.assertIsNone(ns.mode)
        self.assertIsNone(ns.llm_config)
        self.assertFalse(ns.resume)
        self.assertTrue(ns.run_conductor)
        self.assertFalse(ns.wait_usage_reset)  # opt-in, default OFF

    def test_parse_args_supports_wait_usage_reset(self) -> None:
        ns = run_workflow._parse_args(["spec/problem.md", "generate", "--wait-usage-reset"])
        self.assertTrue(ns.wait_usage_reset)

    def test_parse_args_allows_omitted_positionals_for_resume(self) -> None:
        ns = run_workflow._parse_args(["--resume", "--no-run-conductor"])
        self.assertTrue(ns.resume)
        self.assertIsNone(ns.spec_ref)
        self.assertIsNone(ns.until_phase)

    def test_parse_args_supports_no_run_conductor_flag(self) -> None:
        ns = run_workflow._parse_args(
            [
                "spec/problem.md",
                "generate",
                "--no-run-conductor",
            ]
        )
        self.assertFalse(ns.run_conductor)

    def test_parse_args_deprecated_invoke_llm_aliases(self) -> None:
        # The legacy --invoke-llm / --no-invoke-llm spellings still work (they map
        # onto the canonical run_conductor dest) so existing operator muscle memory
        # and scripts keep functioning after the rename.
        ns = run_workflow._parse_args(["spec/problem.md", "generate", "--no-invoke-llm"])
        self.assertFalse(ns.run_conductor)
        ns = run_workflow._parse_args(["--resume", "--invoke-llm"])
        self.assertTrue(ns.run_conductor)



    def test_prompt_params_roundtrip(self) -> None:
        # The resume extractor must recover until_phase/mode/spec_ref from the
        # exact text emitted by _build_orchestration_prompt(). This pins the two
        # functions together so a prompt wording change that breaks resume fails here.
        for until_phase, mode in (("Build", "dev"), ("Validate", "prod"), ("Compile", "dev")):
            prompt = run_workflow._build_orchestration_prompt(
                orchestration_id="orch_x",
                orchestration_agent_run_id="arid_x",
                spec_ref="spec/problem/test.md",
                source_dependency_ref="spec/problem/deps.yaml",
                until_phase=until_phase,
                workflow_mode=mode,
            )
            extracted = run_workflow._extract_prompt_params(prompt)
            self.assertEqual(extracted.get("until_phase"), until_phase)
            self.assertEqual(extracted.get("mode"), mode)
            self.assertEqual(extracted.get("spec_ref"), "spec/problem/test.md")

    def test_prompt_params_recovers_legacy_japanese_start_prompt(self) -> None:
        # Backward compatibility: an orchestration.start.prompt.txt written before
        # the English translation used the Japanese "終了 phase:" label. Resume must
        # still recover until_phase from such persisted prompts.
        legacy_prompt = (
            "target_phases: `compile, generate`（終了 phase: `generate`）\n"
            "workflow_mode: `dev`\n"
            "target_spec_ref: `spec/problem/test.md`\n"
        )
        extracted = run_workflow._extract_prompt_params(legacy_prompt)
        self.assertEqual(extracted.get("until_phase"), "generate")
        self.assertEqual(extracted.get("mode"), "dev")
        self.assertEqual(extracted.get("spec_ref"), "spec/problem/test.md")

    def _seed_resumable_orchestration(
        self,
        repo_root: Path,
        orchestration_id: str,
        *,
        spec_ref: str,
        until_phase: str,
        mode: str,
        backend: str,
        started_at: str = "2026-01-01T00:00:00.000000Z",
        source_dependency_ref: str = "spec/problem/deps.yaml",
        probe_command: str | None = None,
        status: str = "fail",
        invocation: dict | None = None,
        record_executor: str | None = "pure",
        pin: bool = True,
        driver: dict | None = None,
    ) -> None:
        """Create the on-disk artifacts a resume recovers params from.

        Since M-F every real orchestration records `invocation.generate_executor = "pure"` (the
        resume fail-close gate rejects anything else), so this helper injects `pure` by default —
        `setdefault`, so a caller that passes its own `generate_executor` (e.g. a legacy/garbage
        record under test) wins. Pass `record_executor=None` to seed a pre-field orchestration
        (no executor key at all) for the fail-close path.

        Likewise every real orchestration records a leaf-LLM configuration pin, and a resume of
        one that does not is refused outright (`llm_config_legacy_flags_removed`) — so this
        helper writes a real file and pins it, matching production. `pin=False` seeds the
        unpinned pre-issue-#28 shape the refusal tests need.

        The pinned file is named per BACKEND rather than being the default `./llm.yaml`: the
        resume derives its backend from the recorded configuration, so a codex fixture that
        shared one file with `_seed_spec_tree`'s claude default would silently resume as
        claude and its `backend="codex"` argument would reach nothing but `preflight.json`."""
        orch_root = repo_root / "workspace" / "orchestrations" / orchestration_id
        (orch_root / "launches").mkdir(parents=True, exist_ok=True)
        if pin:
            rel = f"llm_{backend}.yaml"
            config = repo_root / rel
            if not config.exists():
                config.parent.mkdir(parents=True, exist_ok=True)
                provider = "codex_cli" if backend == "codex" else "claude_cli"
                model = "  model: gpt-5.6-sol\n" if backend == "codex" else ""
                config.write_text(
                    f"defaults:\n  provider: {provider}\n{model}", encoding="utf-8")
            invocation = dict(invocation or {})
            invocation.setdefault("llm_config_path", rel)
            invocation.setdefault("llm_config_sha256", run_workflow.config_sha256(config))
        dep_ref = source_dependency_ref
        meta = {
            "orchestration_id": orchestration_id,
            "status": status,
            "started_at": started_at,
            "spec_ref": spec_ref,
            "source_dependency_ref": dep_ref,
            "orchestration_agent_run_id": "orch_agent_prev",
        }
        if record_executor is not None:
            invocation = dict(invocation or {})
            invocation.setdefault("generate_executor", record_executor)
        if invocation is not None:
            meta["invocation"] = invocation
        if driver is not None:
            meta["driver"] = driver
        (orch_root / "orchestration_meta.json").write_text(
            json.dumps(meta, ensure_ascii=False),
            encoding="utf-8",
        )
        (orch_root / "preflight.json").write_text(
            json.dumps(
                {
                    "status": "pass",
                    "backend": backend,
                    "probe_command": probe_command if probe_command is not None else backend,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        prompt = run_workflow._build_orchestration_prompt(
            orchestration_id=orchestration_id,
            orchestration_agent_run_id="orch_agent_prev",
            spec_ref=spec_ref,
            source_dependency_ref=dep_ref,
            until_phase=until_phase,
            workflow_mode=mode,
        )
        (orch_root / "launches" / "orchestration.start.prompt.txt").write_text(
            prompt, encoding="utf-8"
        )

    def _seed_spec_tree(self, repo_root: Path) -> None:
        _seed_shape_expr_schema_into(repo_root)
        (repo_root / "tools").mkdir(parents=True, exist_ok=True)
        (repo_root / "workspace").mkdir(parents=True, exist_ok=True)
        (repo_root / "spec" / "problem").mkdir(parents=True, exist_ok=True)
        (repo_root / "spec" / "problem" / "test.md").write_text("spec\n", encoding="utf-8")
        (repo_root / "spec" / "problem" / "deps.yaml").write_text("nodes: []\n", encoding="utf-8")
        _seed_default_llm_config_into(repo_root)

    def _run_main_with_fake_runtime(
        self, argv: list[str]
    ) -> tuple[int, dict, list[list[str]]]:
        observed_calls: list[list[str]] = []
        # The subprocess ENV is part of the runtime contract, not incidental: a runtime
        # call that inherits an env without PYTHONDONTWRITEBYTECODE writes bytecode into
        # the repo source tree, which a later child's write-diff reports as an
        # unauthorized write. Keep it observable so a test can assert on it.
        self._last_runtime_envs: list[dict[str, str]] = []

        def fake_runtime_command(root, env, args):  # type: ignore[no-untyped-def]
            observed_calls.append(args)
            self._last_runtime_envs.append(dict(env or {}))
            if args[0] == "init":
                return run_workflow.RuntimeResult(
                    payload={"status": "ok", "orchestration_agent_run_id": "orch_agent_run_002"},
                    raw_stdout="{}",
                )
            if args[0] == "preflight":
                return run_workflow.RuntimeResult(
                    payload={
                        "status": "pass",
                        "can_launch_step_agents": True,
                        "can_launch_substep_agents": True,
                    },
                    raw_stdout="{}",
                )
            return run_workflow.RuntimeResult(payload={"status": "ok"}, raw_stdout="{}")

        original = run_workflow._runtime_command
        buf = io.StringIO()
        # Force JSONL stdout so the harness can parse the final summary line
        # regardless of the main() default (which is human-readable).
        argv_with_jsonl = list(argv)
        if "--stdout-format" not in argv_with_jsonl:
            argv_with_jsonl += ["--stdout-format", "jsonl"]
        try:
            run_workflow._runtime_command = fake_runtime_command  # type: ignore[assignment]
            with redirect_stdout(buf):
                code = run_workflow.main(argv_with_jsonl)
        finally:
            run_workflow._runtime_command = original  # type: ignore[assignment]
        # Every emitted event, for tests that assert on gate warns rather than only on
        # the final summary line.
        self._last_events = [
            json.loads(line) for line in buf.getvalue().splitlines() if line.strip()
        ]
        out = json.loads(buf.getvalue().strip().splitlines()[-1])
        return code, out, observed_calls

    def _forced_liveness(self):
        return _forced_liveness()

    def test_node_start_event_emitted_once_on_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)

            def fake_runtime_command(root, env, args):  # type: ignore[no-untyped-def]
                if args[0] == "init":
                    return run_workflow.RuntimeResult(
                        payload={"status": "ok", "orchestration_agent_run_id": "orch_agent_run_002"},
                        raw_stdout="{}",
                    )
                if args[0] == "preflight":
                    return run_workflow.RuntimeResult(
                        payload={
                            "status": "pass",
                            "can_launch_step_agents": True,
                            "can_launch_substep_agents": True,
                        },
                        raw_stdout="{}",
                    )
                return run_workflow.RuntimeResult(payload={"status": "ok"}, raw_stdout="{}")

            original = run_workflow._runtime_command
            buf = io.StringIO()
            try:
                run_workflow._runtime_command = fake_runtime_command  # type: ignore[assignment]
                with redirect_stdout(buf):
                    code = run_workflow.main(
                        [
                            "spec/problem/test.md",
                            "build",
                            "--repo-root",
                            str(repo_root),
                            "--orchestration-id",
                            "orch_node_start",
                            "--no-run-conductor",
                            "--stdout-format",
                            "jsonl",
                        ]
                    )
            finally:
                run_workflow._runtime_command = original  # type: ignore[assignment]

            self.assertEqual(code, 0)
            events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
            node_starts = [e for e in events if e.get("event") == "node_start"]
            self.assertEqual(len(node_starts), 1)
            self.assertEqual(node_starts[0]["spec_ref"], "spec/problem/test.md")
            self.assertEqual(node_starts[0]["until_phase"], "Build")
            self.assertEqual(node_starts[0]["orchestration_id"], "orch_node_start")
            self.assertFalse(node_starts[0]["resume"])
            # node_start carries no `ts` (consistent with sibling info events)
            self.assertNotIn("ts", node_starts[0])

    def test_conductor_dev_failure_writes_failure_analysis(self) -> None:
        # In dev mode, a non-pass conductor run must persist failure_analysis.json
        # (the documented dev-failure artifact that init --resume-from-checkpoint
        # reads to build the cross-phase reopen resume_directive).
        import tools.workflow_conductor as wc
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)

            def fake_runtime_command(root, env, args):  # type: ignore[no-untyped-def]
                if args[0] == "init":
                    return run_workflow.RuntimeResult(
                        payload={"status": "ok", "orchestration_agent_run_id": "oar"},
                        raw_stdout="{}",
                    )
                if args[0] == "preflight":
                    return run_workflow.RuntimeResult(
                        payload={"status": "pass", "can_launch_step_agents": True,
                                 "can_launch_substep_agents": True},
                        raw_stdout="{}",
                    )
                return run_workflow.RuntimeResult(payload={"status": "ok"}, raw_stdout="{}")

            orig_rt = run_workflow._runtime_command
            orig_rc = wc.run_conductor
            buf = io.StringIO()
            try:
                run_workflow._runtime_command = fake_runtime_command  # type: ignore[assignment]
                wc.run_conductor = lambda **kw: "fail"  # type: ignore[assignment]
                with redirect_stdout(buf):
                    code = run_workflow.main([
                        "spec/problem/test.md", "build",
                        "--repo-root", str(repo_root),
                        "--orchestration-id", "orch_devfail",
                        "--mode", "dev",
                        "--stdout-format", "jsonl",
                    ])
            finally:
                run_workflow._runtime_command = orig_rt  # type: ignore[assignment]
                wc.run_conductor = orig_rc  # type: ignore[assignment]

            out = json.loads(buf.getvalue().strip().splitlines()[-1])
            self.assertEqual(code, 2, out)
            self.assertEqual(out["status"], "fail")
            self.assertIn("analysis_ref", out)
            fa = repo_root / "workspace" / "orchestrations" / "orch_devfail" / "failure_analysis.json"
            self.assertTrue(fa.exists(), "conductor dev failure must write failure_analysis.json")

    def test_wait_usage_reset_flag_threads_into_run_conductor(self) -> None:
        # The opt-in flag must reach the conductor: argparse -> _run_node -> run_conductor kwarg.
        import tools.workflow_conductor as wc
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)

            def fake_runtime_command(root, env, args):  # type: ignore[no-untyped-def]
                if args[0] == "init":
                    return run_workflow.RuntimeResult(
                        payload={"status": "ok", "orchestration_agent_run_id": "oar"},
                        raw_stdout="{}")
                if args[0] == "preflight":
                    return run_workflow.RuntimeResult(
                        payload={"status": "pass", "can_launch_step_agents": True,
                                 "can_launch_substep_agents": True},
                        raw_stdout="{}")
                return run_workflow.RuntimeResult(payload={"status": "ok"}, raw_stdout="{}")

            captured: dict = {}
            orig_rt = run_workflow._runtime_command
            orig_rc = wc.run_conductor
            try:
                run_workflow._runtime_command = fake_runtime_command  # type: ignore[assignment]

                def _fake_rc(**kw):  # capture the conductor kwargs
                    captured.update(kw)
                    return "pass"

                wc.run_conductor = _fake_rc  # type: ignore[assignment]
                with redirect_stdout(io.StringIO()):
                    run_workflow.main([
                        "spec/problem/test.md", "build",
                        "--repo-root", str(repo_root),
                        "--orchestration-id", "orch_waitflag",
                        "--mode", "dev",
                        "--wait-usage-reset", "--stdout-format", "jsonl",
                    ])
            finally:
                run_workflow._runtime_command = orig_rt  # type: ignore[assignment]
                wc.run_conductor = orig_rc  # type: ignore[assignment]
            self.assertTrue(captured.get("wait_usage_reset"))

    def test_resume_recovers_params_and_uses_checkpoint_init(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            self._seed_resumable_orchestration(
                repo_root,
                "orch_20260101T000000Z_aaaaaaaa",
                spec_ref="spec/problem/test.md",
                until_phase="Build",
                mode="dev",
                backend="claude",
            )
            code, out, calls = self._run_main_with_fake_runtime(
                ["--resume", "--repo-root", str(repo_root), "--no-run-conductor"]
            )
            self.assertEqual(code, 0, out)
            self.assertEqual(out["status"], "ok")
            self.assertTrue(out["resumed"])
            # Latest (only) orchestration reused, params recovered from artifacts.
            self.assertEqual(out["orchestration_id"], "orch_20260101T000000Z_aaaaaaaa")
            self.assertEqual(out["until_phase"], "Build")
            self.assertEqual(out["llm"], "claude")
            self.assertEqual(out["workflow_mode"], "dev")
            # init must use --resume-from-checkpoint (not a fresh init), and pass the
            # resolved spec/dep refs so meta stays in sync with the resumed run.
            init_calls = [c for c in calls if c and c[0] == "init"]
            self.assertEqual(len(init_calls), 1)
            self.assertIn("--resume-from-checkpoint", init_calls[0])
            idx = init_calls[0].index("--spec-ref")
            self.assertEqual(init_calls[0][idx + 1], "spec/problem/test.md")


    def test_resume_with_wait_usage_reset_refreshes_it_in_init(self) -> None:
        """A resume that re-passes --wait-usage-reset forwards it to the resume init, so
        enable_checkpoint_resume refreshes invocation.wait_usage_reset to the effective value (the
        flag is not recovered from the record). Omitting it forwards nothing (records False)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            self._seed_resumable_orchestration(
                repo_root, "orch_20260101T000000Z_aaaaaaaa",
                spec_ref="spec/problem/test.md", until_phase="Build", mode="dev", backend="claude")
            code, out, calls = self._run_main_with_fake_runtime(
                ["--resume", "--repo-root", str(repo_root), "--no-run-conductor",
                 "--wait-usage-reset"])
            self.assertEqual(code, 0, out)
            init_calls = [c for c in calls if c and c[0] == "init"]
            self.assertIn("--resume-from-checkpoint", init_calls[0])
            self.assertIn("--wait-usage-reset", init_calls[0])

    def test_resume_without_wait_usage_reset_omits_it_from_init(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            self._seed_resumable_orchestration(
                repo_root, "orch_20260101T000000Z_aaaaaaaa",
                spec_ref="spec/problem/test.md", until_phase="Build", mode="dev", backend="claude")
            code, out, calls = self._run_main_with_fake_runtime(
                ["--resume", "--repo-root", str(repo_root), "--no-run-conductor"])
            self.assertEqual(code, 0, out)
            init_calls = [c for c in calls if c and c[0] == "init"]
            self.assertNotIn("--wait-usage-reset", init_calls[0])

    def test_resume_forwards_the_recorded_orchestration_agent_model(self) -> None:
        """The orchestration AGENT's own recorded model reaches the resume init (and thus
        repair-agent-runs), so its agent_runs row keeps the attribution the original run had.
        There is no flag to override it any more: the record is the only source."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            self._seed_resumable_orchestration(
                repo_root, "orch_20260101T000000Z_aaaaaaaa",
                spec_ref="spec/problem/test.md", until_phase="Build",
                mode="dev", backend="claude",
                invocation={"agent_model": "claude-opus-4-8"},
            )
            code, out, calls = self._run_main_with_fake_runtime(
                ["--resume", "--repo-root", str(repo_root), "--no-run-conductor"]
            )
            self.assertEqual(code, 0, out)
            init_calls = [c for c in calls if c and c[0] == "init"]
            self.assertIn("--resume-from-checkpoint", init_calls[0])
            idx = init_calls[0].index("--agent-model")
            self.assertEqual(init_calls[0][idx + 1], "claude-opus-4-8")

    def test_a_resume_survives_a_missing_preflight_json(self) -> None:
        """A run killed between `init` and `write_preflight` leaves no `preflight.json`. Its
        backend comes from the RECORDED leaf-LLM configuration, so nothing about it is
        unrecoverable — and gating the resume on the preflight's `backend` made such a run
        permanently unresumable while telling the operator to pass a flag that no longer
        exists."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            self._seed_resumable_orchestration(
                repo_root, "orch_20260101T000000Z_aaaaaaaa",
                spec_ref="spec/problem/test.md", until_phase="Build",
                mode="dev", backend="claude",
            )
            (repo_root / "workspace" / "orchestrations"
             / "orch_20260101T000000Z_aaaaaaaa" / "preflight.json").unlink()
            code, out, calls = self._run_main_with_fake_runtime(
                ["--resume", "--repo-root", str(repo_root), "--no-run-conductor"]
            )
            self.assertEqual(code, 0, out)
            self.assertEqual(out["llm"], "claude")     # derived from the recorded pin
            self.assertIn("--resume-from-checkpoint",
                          next(c for c in calls if c and c[0] == "init"))

    def test_a_resume_of_a_run_that_recorded_no_agent_model_omits_it(self) -> None:
        """`init --agent-model` is NOT injected when the record has none, so repair uses the
        more-accurate sibling_uniform derivation rather than a possibly-wrong default."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            self._seed_resumable_orchestration(
                repo_root, "orch_20260101T000000Z_aaaaaaaa",
                spec_ref="spec/problem/test.md", until_phase="Build",
                mode="dev", backend="claude",
            )
            code, out, calls = self._run_main_with_fake_runtime(
                ["--resume", "--repo-root", str(repo_root), "--no-run-conductor"]
            )
            self.assertEqual(code, 0, out)
            init_calls = [c for c in calls if c and c[0] == "init"]
            self.assertNotIn("--agent-model", init_calls[0])

    def test_fresh_claude_run_records_orchestration_agent_model(self) -> None:
        """A fresh (non-resume) claude run threads --agent-model into init so the
        orchestration agent_runs row records the model (P2). The default is the
        operator's UNPINNED alias (e.g. 'opus'), not a pinned version."""
        from tools.orchestration_runtime import resolve_claude_model_alias
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            code, out, calls = self._run_main_with_fake_runtime(
                ["spec/problem/test.md", "compile",
                 "--repo-root", str(repo_root), "--no-run-conductor"]
            )
            self.assertEqual(code, 0, out)
            init_calls = [c for c in calls if c and c[0] == "init"]
            self.assertEqual(len(init_calls), 1)
            self.assertNotIn("--resume-from-checkpoint", init_calls[0])
            idx = init_calls[0].index("--agent-model")
            recorded = init_calls[0][idx + 1]
            self.assertEqual(recorded, resolve_claude_model_alias())
            # never a pinned version id
            self.assertNotRegex(recorded, r"-\d+-\d+$")

    def test_a_codex_configuration_without_a_slug_stops_before_launching(self) -> None:
        """Codex has no alias to resolve at runtime, and the run-wide flag that used to
        supply one is gone — so the configuration file is the only place a slug can come
        from, and a blank one must stop the run rather than reach a leaf launch."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            config = repo_root / "bare_codex.yaml"
            config.write_text("defaults:\n  provider: codex_cli\n", encoding="utf-8")
            code, out, calls = self._run_main_with_fake_runtime(
                ["spec/problem/test.md", "compile", "--llm-config", str(config),
                 "--repo-root", str(repo_root), "--no-run-conductor"]
            )
            self.assertEqual(code, 2, out)
            self.assertEqual(out["reason"], "invalid_startup_input")
            self.assertIn("llm_config_codex_cli_requires_model", out["detail"])
            self.assertEqual(calls, [])

    def test_a_codex_configuration_slug_reaches_init(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            config = repo_root / "codex.yaml"
            config.write_text(
                "defaults:\n  provider: codex_cli\n  model: gpt-5.3-codex\n",
                encoding="utf-8")
            code, out, calls = self._run_main_with_fake_runtime(
                ["spec/problem/test.md", "compile", "--llm-config", str(config),
                 "--repo-root", str(repo_root), "--no-run-conductor"]
            )
            self.assertEqual(code, 0, out)
            init_call = next(c for c in calls if c and c[0] == "init")
            self.assertEqual(init_call[init_call.index("--agent-backend") + 1], "codex")
            # The orchestration AGENT's own model is not asserted for codex: only the
            # claude backend on its unmodified command has an alias worth recording.
            self.assertNotIn("--agent-model", init_call)

    def test_resume_codex_restores_recorded_agent_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            self._seed_resumable_orchestration(
                repo_root, "orch_20260101T000000Z_aaaaaaaa",
                spec_ref="spec/problem/test.md", until_phase="Build", mode="dev", backend="codex",
                invocation={"agent_model": "gpt-5.3-codex"},
            )
            code, out, calls = self._run_main_with_fake_runtime(
                ["--resume", "--repo-root", str(repo_root), "--no-run-conductor"]
            )
            self.assertEqual(code, 0, out)
            init_call = next(c for c in calls if c and c[0] == "init")
            idx = init_call.index("--agent-model")
            self.assertEqual(init_call[idx + 1], "gpt-5.3-codex")
            # ...and this is genuinely a CODEX resume. Without this the test passed identically
            # against a claude configuration: the backend comes from the recorded pin, so a
            # fixture that pinned the shared `./llm.yaml` made `backend="codex"` reach nothing.
            self.assertEqual(out["llm"], "codex")
            preflight = next(c for c in calls if c and c[0] == "preflight")
            self.assertEqual(preflight[preflight.index("--backend") + 1], "codex")

    def test_a_configured_claude_command_omits_the_opus_default(self) -> None:
        """A configured `command:` may launch a non-Opus model, so the Opus default must NOT
        be asserted; agent_model is left to sibling backfill rather than wrongly recording
        Opus. The wrapper now comes from `defaults.command` — the flag that used to say it
        is gone, and this is the surface that replaced it."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            config = repo_root / "wrapped.yaml"
            config.write_text(
                "defaults:\n  provider: claude_cli\n"
                "  command: claude --model claude-sonnet-4-6\n", encoding="utf-8")
            code, out, calls = self._run_main_with_fake_runtime(
                ["spec/problem/test.md", "compile", "--llm-config", str(config),
                 "--repo-root", str(repo_root), "--no-run-conductor"]
            )
            self.assertEqual(code, 0, out)
            init_calls = [c for c in calls if c and c[0] == "init"]
            self.assertNotIn("--agent-model", init_calls[0])

    def test_resume_picks_latest_by_started_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            for oid, phase, started in (
                ("orch_20260101T000000Z_aaaaaaaa", "Compile", "2026-01-01T00:00:00.000000Z"),
                ("orch_20260301T000000Z_bbbbbbbb", "Validate", "2026-03-01T00:00:00.000000Z"),
            ):
                self._seed_resumable_orchestration(
                    repo_root, oid, spec_ref="spec/problem/test.md",
                    until_phase=phase, mode="dev", backend="codex", started_at=started,
                    invocation={"agent_model": "gpt-5.3-codex"},
                )
            code, out, _ = self._run_main_with_fake_runtime(
                ["--resume", "--repo-root", str(repo_root), "--no-run-conductor"]
            )
            self.assertEqual(code, 0, out)
            self.assertEqual(out["orchestration_id"], "orch_20260301T000000Z_bbbbbbbb")
            self.assertEqual(out["until_phase"], "Validate")

    def test_resume_latest_uses_started_at_not_id_text(self) -> None:
        # Regression for the lexical-max bug: the newest started_at must win even
        # when its id sorts BEFORE another candidate, and even when a custom
        # (non-timestamp) id that sorts lexically last is present.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            # newest start, but lexically-smallest id
            self._seed_resumable_orchestration(
                repo_root, "orch_20260101T000000Z_aaaaaaaa", spec_ref="spec/problem/test.md",
                until_phase="Validate", mode="dev", backend="claude",
                started_at="2026-05-01T00:00:00.000000Z",
            )
            # older start, lexically-larger timestamp id
            self._seed_resumable_orchestration(
                repo_root, "orch_20260301T000000Z_bbbbbbbb", spec_ref="spec/problem/test.md",
                until_phase="Compile", mode="dev", backend="codex",
                started_at="2026-02-01T00:00:00.000000Z",
            )
            # custom id that sorts lexically last ('u' > '2') but is oldest
            self._seed_resumable_orchestration(
                repo_root, "orch_unit_run", spec_ref="spec/problem/test.md",
                until_phase="Build", mode="dev", backend="codex",
                started_at="2026-01-01T00:00:00.000000Z",
            )
            code, out, _ = self._run_main_with_fake_runtime(
                ["--resume", "--repo-root", str(repo_root), "--no-run-conductor"]
            )
            self.assertEqual(code, 0, out)
            # The 2026-05-01 start wins despite its lexically-smaller id.
            self.assertEqual(out["orchestration_id"], "orch_20260101T000000Z_aaaaaaaa")
            self.assertEqual(out["until_phase"], "Validate")
            self.assertEqual(out["llm"], "claude")

    def test_resume_includes_custom_orchestration_ids(self) -> None:
        # A run launched with a custom --orchestration-id (no `orch_` prefix) must
        # still be resumable as "the latest" when it is the newest started.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            self._seed_resumable_orchestration(
                repo_root, "orch_20260101T000000Z_aaaaaaaa", spec_ref="spec/problem/test.md",
                until_phase="Compile", mode="dev", backend="codex",
                started_at="2026-01-01T00:00:00.000000Z",
            )
            self._seed_resumable_orchestration(
                repo_root, "customrun", spec_ref="spec/problem/test.md",
                until_phase="Validate", mode="dev", backend="claude",
                started_at="2026-05-01T00:00:00.000000Z",
            )
            code, out, _ = self._run_main_with_fake_runtime(
                ["--resume", "--repo-root", str(repo_root), "--no-run-conductor"]
            )
            self.assertEqual(code, 0, out)
            self.assertEqual(out["orchestration_id"], "customrun")
            self.assertEqual(out["until_phase"], "Validate")

    def test_resume_reuses_recovered_dependency_ref(self) -> None:
        # The dependency ref recorded at init must be reused on resume rather than
        # rediscovered from the spec path, so resume stays stable even when the
        # default deps.yaml next to the spec is absent/moved.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            (repo_root / "tools").mkdir(parents=True, exist_ok=True)
            (repo_root / "workspace").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem" / "test.md").write_text("spec\n", encoding="utf-8")
            # Intentionally NO spec/problem/deps.yaml: _discover_source_dependency_ref
            # would raise here, so success proves the recovered ref is used instead.
            self._seed_resumable_orchestration(
                repo_root, "orch_20260101T000000Z_aaaaaaaa", spec_ref="spec/problem/test.md",
                until_phase="Build", mode="dev", backend="claude",
                source_dependency_ref="spec/problem/sub/deps.yaml",
            )
            code, out, _ = self._run_main_with_fake_runtime(
                ["--resume", "--repo-root", str(repo_root), "--no-run-conductor"]
            )
            self.assertEqual(code, 0, out)
            prompt = (
                repo_root / "workspace" / "orchestrations"
                / "orch_20260101T000000Z_aaaaaaaa" / "launches"
                / "orchestration.start.prompt.txt"
            ).read_text(encoding="utf-8")
            self.assertIn("spec/problem/sub/deps.yaml", prompt)

    def test_resume_preserves_custom_llm_command(self) -> None:
        # A custom command belongs to the PINNED CONFIGURATION now, not to a recovered
        # preflight field: a resume reloads the file it launched with, so the wrapper it
        # names is what preflight probes and what the leaves launch through.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            config = repo_root / "wrapped.yaml"
            config.write_text(
                "defaults:\n  provider: claude_cli\n"
                "  command: /opt/wrappers/claude-wrapper\n", encoding="utf-8")
            self._seed_resumable_orchestration(
                repo_root, "orch_20260101T000000Z_aaaaaaaa", spec_ref="spec/problem/test.md",
                until_phase="Build", mode="dev", backend="claude",
                invocation={"llm_config_path": "wrapped.yaml",
                            "llm_config_sha256": run_workflow.config_sha256(config)},
            )
            code, out, calls = self._run_main_with_fake_runtime(
                ["--resume", "--repo-root", str(repo_root), "--no-run-conductor"]
            )
            self.assertEqual(code, 0, out)
            self.assertEqual(out["llm_command"], "/opt/wrappers/claude-wrapper")
            preflight_calls = [c for c in calls if c and c[0] == "preflight"]
            self.assertEqual(len(preflight_calls), 1)
            idx = preflight_calls[0].index("--agent-command")
            self.assertEqual(preflight_calls[0][idx + 1], "/opt/wrappers/claude-wrapper")

    def test_resume_same_spec_explicit_keeps_recovered_dependency(self) -> None:
        # Restating the SAME spec_ref explicitly is not a change: the recovered
        # (possibly non-default) dependency must still be reused, not rediscovered.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            (repo_root / "tools").mkdir(parents=True, exist_ok=True)
            (repo_root / "workspace").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem" / "test.md").write_text("spec\n", encoding="utf-8")
            # No spec/problem/deps.yaml: rediscovery would fail, proving reuse.
            self._seed_resumable_orchestration(
                repo_root, "orch_20260101T000000Z_aaaaaaaa", spec_ref="spec/problem/test.md",
                until_phase="Build", mode="dev", backend="claude",
                source_dependency_ref="spec/problem/sub/deps.yaml",
            )
            code, out, _ = self._run_main_with_fake_runtime(
                ["--resume", "spec/problem/test.md",
                 "--repo-root", str(repo_root), "--no-run-conductor"]
            )
            self.assertEqual(code, 0, out)
            prompt = (
                repo_root / "workspace" / "orchestrations"
                / "orch_20260101T000000Z_aaaaaaaa" / "launches"
                / "orchestration.start.prompt.txt"
            ).read_text(encoding="utf-8")
            self.assertIn("spec/problem/sub/deps.yaml", prompt)

    def test_a_resume_cannot_switch_backend_at_all(self) -> None:
        """Switching a run's backend mid-flight was what `--llm` on a resume did. There is no
        flag left that can: the recorded pin decides, and even an explicit `--llm-config`
        naming a different file is announced-and-ignored rather than applied."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            self._seed_resumable_orchestration(
                repo_root, "orch_20260101T000000Z_aaaaaaaa", spec_ref="spec/problem/test.md",
                until_phase="Build", mode="dev", backend="claude",
            )
            other = repo_root / "other.yaml"
            other.write_text(
                "defaults:\n  provider: codex_cli\n  model: gpt-5.3-codex\n",
                encoding="utf-8")
            err = io.StringIO()
            with mock.patch.object(sys, "stderr", err):
                code, out, _ = self._run_main_with_fake_runtime(
                    ["--resume", "--llm-config", str(other),
                     "--repo-root", str(repo_root), "--no-run-conductor"]
                )
            self.assertEqual(code, 0, out)
            self.assertEqual(out["llm"], "claude")
            self.assertIn("--llm-config is ignored on --resume", err.getvalue())

    def test_resume_cli_overrides_recovered_until_phase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            self._seed_resumable_orchestration(
                repo_root, "orch_20260101T000000Z_aaaaaaaa",
                spec_ref="spec/problem/test.md", until_phase="Compile",
                mode="dev", backend="claude",
            )
            code, out, _ = self._run_main_with_fake_runtime(
                ["--resume", "build", "--repo-root", str(repo_root), "--no-run-conductor"]
            )
            self.assertEqual(code, 0, out)
            self.assertEqual(out["until_phase"], "Build")

    def _seed_running_orchestration(
        self, repo_root: Path, oid: str, *, verdict: str | None,
        spec_ref: str = "spec/problem/test.md",
    ) -> None:
        """Seed a non-terminal (`running`) orchestration whose driver probes to `verdict`."""
        driver = {"pid": 424242}
        if verdict is not None:
            driver["verdict"] = verdict
        self._seed_resumable_orchestration(
            repo_root, oid, spec_ref=spec_ref, until_phase="Build", mode="dev",
            backend="claude", status="running", driver=driver,
        )

    def test_implicit_resume_terminalizes_dead_running_latest(self) -> None:
        # A crashed driver leaves status='running' forever; the probe proves the corpse,
        # so the resume terminalizes it FIRST (fail/driver_crashed) and only then runs
        # init --resume-from-checkpoint — which is what routes the resume through
        # `terminal_reset`, where the crash reconciliations live.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            oid = "orch_20260101T000000Z_aaaaaaaa"
            self._seed_running_orchestration(repo_root, oid, verdict="dead")
            # The bytecode assertion below reads the env this call BUILDS. With the
            # variable already in the ambient environment it would pass for free via
            # the `dict(os.environ)` fallback — and the environment where it is already
            # set is exactly the one this repo mandates, i.e. where the guard would stop
            # being tested. Clear it so the assertion can only pass if the code sets it.
            ambient = os.environ.pop("PYTHONDONTWRITEBYTECODE", None)
            try:
                with self._forced_liveness():
                    code, out, calls = self._run_main_with_fake_runtime(
                        ["--resume", "--repo-root", str(repo_root), "--no-run-conductor"]
                    )
            finally:
                if ambient is not None:
                    os.environ["PYTHONDONTWRITEBYTECODE"] = ambient
            self.assertEqual(code, 0, out)
            self.assertEqual(out["orchestration_id"], oid)
            verbs = [c[0] for c in calls]
            self.assertEqual(verbs[0], "set-status")
            self.assertIn("init", verbs)
            self.assertLess(verbs.index("set-status"), verbs.index("init"))
            set_status = calls[0]
            self.assertEqual(set_status[set_status.index("--status") + 1], "fail")
            self.assertEqual(
                set_status[set_status.index("--reason-code") + 1], "driver_crashed"
            )
            init_call = calls[verbs.index("init")]
            self.assertIn("--resume-from-checkpoint", init_call)
            events = [e.get("event") for e in self._last_events]
            self.assertIn("dead_driver_terminalized", events)
            # This terminalization runs BEFORE base_env exists, so it must supply the
            # no-bytecode setting itself; without it the runtime subprocess writes
            # tools/__pycache__/*.pyc into the repo tree and a later child's write-diff
            # reports it as an unauthorized write.
            self.assertEqual(
                self._last_runtime_envs[0].get("PYTHONDONTWRITEBYTECODE"), "1"
            )

    def test_implicit_resume_still_refuses_alive_running_latest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            self._seed_running_orchestration(
                repo_root, "orch_20260101T000000Z_aaaaaaaa", verdict="alive")
            with self._forced_liveness():
                code, out, calls = self._run_main_with_fake_runtime(
                    ["--resume", "--repo-root", str(repo_root), "--no-run-conductor"]
                )
            self.assertEqual(code, 2, out)
            self.assertEqual(out["reason"], "latest_orchestration_not_resumable")
            self.assertEqual(out["driver_pid"], 424242)
            self.assertEqual(calls, [])

    def test_explicit_resume_alive_running_refused(self) -> None:
        # An explicit id is the deliberate override for the implicit-latest guard, but
        # it cannot override a driver that is demonstrably still running.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            oid = "orch_20260101T000000Z_aaaaaaaa"
            self._seed_running_orchestration(repo_root, oid, verdict="alive")
            with self._forced_liveness():
                code, out, calls = self._run_main_with_fake_runtime(
                    ["--resume", "--orchestration-id", oid,
                     "--repo-root", str(repo_root), "--no-run-conductor"]
                )
            self.assertEqual(code, 2, out)
            self.assertEqual(out["reason"], "orchestration_driver_alive")
            self.assertEqual(calls, [])

    def test_explicit_resume_dead_running_terminalizes_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            oid = "orch_20260101T000000Z_aaaaaaaa"
            self._seed_running_orchestration(repo_root, oid, verdict="dead")
            with self._forced_liveness():
                code, out, calls = self._run_main_with_fake_runtime(
                    ["--resume", "--orchestration-id", oid,
                     "--repo-root", str(repo_root), "--no-run-conductor"]
                )
            self.assertEqual(code, 0, out)
            self.assertEqual(calls[0][0], "set-status")
            self.assertEqual(calls[0][calls[0].index("--reason-code") + 1], "driver_crashed")

    def test_explicit_resume_indeterminate_running_warns_and_proceeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            oid = "orch_20260101T000000Z_aaaaaaaa"
            self._seed_running_orchestration(repo_root, oid, verdict="unknown")
            with self._forced_liveness():
                code, out, calls = self._run_main_with_fake_runtime(
                    ["--resume", "--orchestration-id", oid,
                     "--repo-root", str(repo_root), "--no-run-conductor"]
                )
            self.assertEqual(code, 0, out)
            events = [e.get("event") for e in self._last_events]
            self.assertIn("resume_liveness_indeterminate", events)
            self.assertNotIn("set-status", [c[0] for c in calls])

    def test_resume_fails_when_dead_driver_cannot_be_terminalized(self) -> None:
        # Fail closed rather than resuming a still-`running` meta: the crash
        # reconciliations would silently not run.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            oid = "orch_20260101T000000Z_aaaaaaaa"
            self._seed_running_orchestration(repo_root, oid, verdict="dead")

            def failing_runtime(root, env, args):  # type: ignore[no-untyped-def]
                raise RuntimeError("runtime command failed (set-status): boom")

            original = run_workflow._runtime_command
            buf = io.StringIO()
            try:
                run_workflow._runtime_command = failing_runtime  # type: ignore[assignment]
                with self._forced_liveness(), redirect_stdout(buf):
                    code = run_workflow.main(
                        ["--resume", "--orchestration-id", oid, "--repo-root",
                         str(repo_root), "--no-run-conductor",
                         "--stdout-format", "jsonl"]
                    )
            finally:
                run_workflow._runtime_command = original  # type: ignore[assignment]
            self.assertEqual(code, 2)
            out = json.loads(buf.getvalue().strip().splitlines()[-1])
            self.assertEqual(out["reason"], "dead_driver_terminalize_failed")

    def test_cold_run_warns_about_prior_incomplete_orchestration(self) -> None:
        # B: a cold run that leaves a resumable checkpoint behind must say so — the
        # issue-#11 incident was a cold re-run silently discarding a 5083s compile pass.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            stale = "orch_20260101T000000Z_aaaaaaaa"
            self._seed_running_orchestration(repo_root, stale, verdict="dead")
            with self._forced_liveness():
                code, out, calls = self._run_main_with_fake_runtime(
                    ["spec/problem/test.md", "build", "--repo-root", str(repo_root),
                     "--orchestration-id", "orch_cold", "--no-run-conductor"]
                )
            self.assertEqual(code, 0, out)
            warns = [
                e for e in self._last_events
                if e.get("event") == "prior_incomplete_orchestration"
            ]
            self.assertEqual(len(warns), 1)
            self.assertEqual(warns[0]["orchestration_id"], stale)
            self.assertEqual(warns[0]["liveness"], "dead")
            self.assertEqual(
                warns[0]["resume_command"],
                f"python3 tools/run_workflow.py --resume --orchestration-id {stale}",
            )
            # Warned, not blocked: the cold run still proceeds (inform over prohibit).
            self.assertIn("init", [c[0] for c in calls])

    def test_cold_run_warns_when_prior_liveness_indeterminate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            stale = "orch_20260101T000000Z_aaaaaaaa"
            # No verdict seeded and no usable driver block → the real probe answers
            # `unknown`, which must never block a cold run.
            self._seed_resumable_orchestration(
                repo_root, stale, spec_ref="spec/problem/test.md", until_phase="Build",
                mode="dev", backend="claude", status="running",
            )
            code, out, calls = self._run_main_with_fake_runtime(
                ["spec/problem/test.md", "build", "--repo-root", str(repo_root),
                 "--orchestration-id", "orch_cold", "--no-run-conductor"]
            )
            self.assertEqual(code, 0, out)
            warns = [
                e for e in self._last_events
                if e.get("event") == "prior_incomplete_orchestration"
            ]
            self.assertEqual([w["liveness"] for w in warns], ["unknown"])
            self.assertIn("init", [c[0] for c in calls])

    def test_cold_run_blocked_by_live_concurrent_orchestration(self) -> None:
        # C: two live runs of one spec derive their pipeline_id from the same
        # workspace/pipelines/<node_key_safe>/ tree and then write into it, so the
        # second corrupts the first's in-flight state. Refuse before any orchestration
        # state is created. (The workspace/tmp/<arid> collision is a RESUME hazard —
        # a cold run always mints a fresh orchestration_agent_run_id.)
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            live = "orch_20260101T000000Z_aaaaaaaa"
            self._seed_running_orchestration(repo_root, live, verdict="alive")
            with self._forced_liveness():
                code, out, calls = self._run_main_with_fake_runtime(
                    ["spec/problem/test.md", "build", "--repo-root", str(repo_root),
                     "--orchestration-id", "orch_cold", "--no-run-conductor"]
                )
            self.assertEqual(code, 2, out)
            self.assertEqual(out["reason"], "concurrent_orchestration_running")
            self.assertEqual(out["orchestration_id"], live)
            self.assertEqual(
                out["resume_command"],
                f"python3 tools/run_workflow.py --resume --orchestration-id {live}",
            )
            self.assertEqual(calls, [])

    def test_cold_run_ignores_terminal_orchestration_of_the_same_spec(self) -> None:
        # A completed (or failed) prior run of the same spec is not "incomplete": it
        # has nothing to resume, so warning about it would be noise on every re-run.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            for status in ("pass", "fail", "fail_closed", "cancel"):
                with self.subTest(status=status):
                    self._seed_resumable_orchestration(
                        repo_root, f"orch_prior_{status}",
                        spec_ref="spec/problem/test.md", until_phase="Build",
                        mode="dev", backend="claude", status=status,
                        driver={"pid": 424242, "verdict": "alive"},
                    )
            with self._forced_liveness():
                code, out, calls = self._run_main_with_fake_runtime(
                    ["spec/problem/test.md", "build", "--repo-root", str(repo_root),
                     "--orchestration-id", "orch_cold", "--no-run-conductor"]
                )
            self.assertEqual(code, 0, out)
            self.assertEqual(
                [e for e in self._last_events
                 if e.get("event") == "prior_incomplete_orchestration"],
                [],
            )
            self.assertIn("init", [c[0] for c in calls])

    def test_cold_guard_ignores_an_orchestration_this_process_drives(self) -> None:
        # The guard rescans the workspace on every call (a closure reaches its later
        # nodes hours after it starts, and a competing run launched inside that window
        # is exactly what must be caught), so it necessarily sees the orchestrations
        # THIS invocation just started — whose driver is us and therefore probes
        # `alive`. Without the self-exclusion a closure would block itself at its
        # second node.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            identity = run_workflow._current_driver_identity()
            self.assertIsNotNone(identity)
            self._seed_resumable_orchestration(
                repo_root, "orch_started_by_us", spec_ref="spec/problem/test.md",
                until_phase="Build", mode="dev", backend="claude", status="running",
                driver=dict(identity),
            )
            buf = io.StringIO()
            with redirect_stdout(buf):
                conflict = run_workflow._cold_start_running_guard(
                    repo_root, "spec/problem/test.md", stdout_format="jsonl",
                    driver_identity=identity,
                )
            self.assertIsNone(conflict)
            self.assertEqual(buf.getvalue().strip(), "")
            # Another live driver on the same spec is still caught. Each fixture below
            # differs from our identity in EXACTLY ONE key, so the exclusion cannot be
            # satisfied by any single key: matching on `pid` alone would re-admit the
            # pid-reuse hazard the whole design is built around (a genuinely different
            # earlier process, or a pre-reboot run, silently treated as "ours").
            others = {
                "orch_other_pid": {**identity, "pid": identity["pid"] + 1},
                "orch_other_ticks": {
                    **identity,
                    "pid_start_ticks": str(int(identity["pid_start_ticks"]) + 1)},
                "orch_other_boot": {**identity, "boot_id": "00000000-0000-0000-0000-000000000000"},
                # A block that records a different namespace or uid is by its own
                # account not this process, whatever the pid says.
                "orch_other_ns": {**identity, "pid_ns": identity["pid_ns"] + 1},
                "orch_other_uid": {**identity, "uid": identity["uid"] + 1},
            }
            for oid, other in others.items():
                with self.subTest(differing_key=oid):
                    for stale in others:
                        shutil.rmtree(
                            repo_root / "workspace" / "orchestrations" / stale,
                            ignore_errors=True)
                    self._seed_resumable_orchestration(
                        repo_root, oid, spec_ref="spec/problem/test.md",
                        until_phase="Build", mode="dev", backend="claude",
                        status="running", driver={**other, "verdict": "alive"},
                    )
                    with self._forced_liveness(), redirect_stdout(io.StringIO()):
                        conflict = run_workflow._cold_start_running_guard(
                            repo_root, "spec/problem/test.md", stdout_format="jsonl",
                            driver_identity=identity,
                        )
                    self.assertIsNotNone(conflict)
                    self.assertEqual(conflict["orchestration_id"], oid)

    def test_guards_skip_an_orchestration_whose_meta_is_gone(self) -> None:
        # A meta that was deleted (or is unreadable) reads as `{}`, which has no
        # status — so without the emptiness check it would be treated as non-terminal,
        # probed as `unknown`, and reported as a "prior incomplete orchestration"
        # pointing at a `--resume` that cannot possibly work.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            original_index = run_workflow._index_incomplete_orchestrations_by_spec
            run_workflow._index_incomplete_orchestrations_by_spec = (  # type: ignore[assignment]
                lambda root: {"spec/problem/test.md": ["orch_meta_deleted"]})
            buf = io.StringIO()
            try:
                with redirect_stdout(buf):
                    conflict = run_workflow._cold_start_running_guard(
                        repo_root, "spec/problem/test.md", stdout_format="jsonl")
            finally:
                run_workflow._index_incomplete_orchestrations_by_spec = (  # type: ignore[assignment]
                    original_index)
            self.assertIsNone(conflict)
            self.assertEqual(buf.getvalue().strip(), "")

            # Same for the warm-resume guard: an id with no meta on disk is not a
            # liveness question, it is a resume that init will reject on its own.
            buf2 = io.StringIO()
            with redirect_stdout(buf2):
                self.assertIsNone(run_workflow._warm_resume_liveness_guard(
                    repo_root, "orch_meta_deleted", stdout_format="jsonl"))
            self.assertEqual(buf2.getvalue().strip(), "")

    def test_repeated_in_process_run_is_not_blocked_by_its_own_prior_run(self) -> None:
        # Pins the WIRING of the self-exclusion at the single-node call site: `main()`
        # must hand the guard this process's identity. A caller that invokes `main()`
        # more than once against one repo (an embedding caller, or a `--no-run-conductor`
        # run, which never terminalizes) would otherwise have its second call refuse,
        # naming a run that has already returned.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            identity = run_workflow._current_driver_identity()
            self.assertIsNotNone(identity)
            # Exactly the block `init --driver-json` persists, recorded_at included.
            self._seed_resumable_orchestration(
                repo_root, "orch_first_call", spec_ref="spec/problem/test.md",
                until_phase="Build", mode="dev", backend="claude", status="running",
                driver={**identity, "recorded_at": "2026-01-01T00:00:00.000000Z"},
            )
            code, out, calls = self._run_main_with_fake_runtime(
                ["spec/problem/test.md", "build", "--repo-root", str(repo_root),
                 "--orchestration-id", "orch_second_call", "--no-run-conductor"]
            )
            self.assertEqual(code, 0, out)
            self.assertIn("init", [c[0] for c in calls])
            self.assertEqual(
                [e for e in self._last_events
                 if e.get("event") == "prior_incomplete_orchestration"],
                [],
            )

    def test_claim_root_is_relocatable(self) -> None:
        # The override is what keeps a caller that drives many throwaway repo roots
        # (this suite) from depositing a 0-byte file per key in the operator's home.
        with tempfile.TemporaryDirectory() as tmp:
            claim_root = Path(tmp) / "claims"
            saved = os.environ.get("ATMOFAB_START_CLAIM_ROOT")
            os.environ["ATMOFAB_START_CLAIM_ROOT"] = str(claim_root)
            try:
                path = run_workflow._claim_lock_path(Path(tmp), "spec", "spec/x")
                self.assertEqual(path.parent, claim_root)
                with run_workflow._exclusive_claim(Path(tmp), "spec", "spec/x") as held:
                    self.assertTrue(held)
                self.assertTrue(path.is_file())
                # Distinct kinds and keys never collide on one file.
                self.assertNotEqual(
                    path, run_workflow._claim_lock_path(Path(tmp), "orch", "spec/x"))
                self.assertNotEqual(
                    path, run_workflow._claim_lock_path(Path(tmp), "spec", "spec/y"))
            finally:
                if saved is None:
                    os.environ.pop("ATMOFAB_START_CLAIM_ROOT", None)
                else:
                    os.environ["ATMOFAB_START_CLAIM_ROOT"] = saved

    def test_the_claim_root_default_hangs_off_the_operator_secret_root(self) -> None:
        """The DEFAULT is the half `test_claim_root_is_relocatable` cannot see.

        That test sets the override, which is also what this module's `setUpModule`
        does, so the production resolution — no override, `~/.atmofab/start_claims` —
        ran in no test at all. It is the branch that matters for issue #132: the claims
        are the third writer under the operator-private root, and until this commit it
        spelled `Path.home() / ".atmofab"` for itself.

        Pinned at the SHARED resolver, not against a transcribed path: `_home_dir` is
        patched in `tools.hooks.common`, the module `operator_secret_root` reads, so if
        the default stopped going through it this lands somewhere else and fails.
        """
        import tools.hooks.common as hooks_common
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            env = {k: v for k, v in os.environ.items()
                   if k != run_workflow.START_CLAIMS_ROOT_ENV}
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(hooks_common, "_home_dir",
                                      return_value=fake_home):
                path = run_workflow._claim_lock_path(Path(tmp), "spec", "spec/x")
            self.assertEqual(path.parent, (fake_home / ".atmofab").resolve()
                             / "start_claims")

    def test_a_tilde_claim_root_override_is_expanded(self) -> None:
        """`~` is expanded, and until round 1 nothing observed that.

        The third of three relocators, and the only one whose `expanduser` had no test —
        `test_a_tilde_homes_root_override_is_expanded` and
        `test_a_tilde_token_store_override_is_expanded` cover the other two, and both
        reviewers reported this one as a surviving mutant.

        The behaviour it pins is a CHANGE this branch made deliberately: a quoted
        `ATMOFAB_START_CLAIM_ROOT='~/claims'` is a plausible spelling and the shell does
        not expand inside quotes, so before this branch the value became a literal `~`
        directory under the caller's working directory — which for the conductor is the
        checkout, where it then shows up as an untracked path.
        """
        with mock.patch.dict(
                os.environ,
                {"HOME": "/tmp/fake-home-probe",
                 run_workflow.START_CLAIMS_ROOT_ENV: "~/big/claims"},
                clear=False):
            resolved = run_workflow._start_claims_root()
        self.assertEqual(resolved, Path("/tmp/fake-home-probe/big/claims"))
        self.assertNotIn("~", str(resolved))

    def test_an_unresolvable_claim_root_degrades_to_proceed(self) -> None:
        """The promise the docstring makes, which the `expanduser()` broke.

        `_exclusive_claim` says a host where the lock cannot be taken yields True — the
        claim strengthens the driver-liveness guard and is never a precondition for
        running — and `_start_claims_root` and `docs/RUNBOOK.md` both said the override is
        checked by nothing. Adding `expanduser()` on this branch made that false for one
        spelling: `~account` naming no account raises `RuntimeError`, which is not
        `OSError`, and `_claim_lock_path` was called outside every `except` in this
        function. Measured: it raised at HEAD and yielded True at `origin/main` — a
        regression this branch introduced against a sentence this branch wrote.

        Found by a round-4 reviewer reading the branch as the next maintainer.
        """
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(
                    os.environ,
                    {run_workflow.START_CLAIMS_ROOT_ENV: "~nosuchaccount12345/claims"},
                    clear=False), \
                run_workflow._exclusive_claim(Path(tmp), "spec", "x") as held:
            self.assertTrue(
                held, "an unresolvable claim root must degrade to proceed, not raise")

    def test_the_claims_guard_assertion_is_not_swallowed_by_that_degradation(self) -> None:
        """The other half, and the reason the `except` is by TYPE rather than bare.

        `tools/tests/conftest.py` wraps `_start_claims_root` in a guard that raises
        `AssertionError` when a test is about to resolve the operator's real
        `~/.atmofab`. Catching it in `_exclusive_claim` would turn that guard into a
        silent "proceed" — the suite would go back to writing into the operator's tree
        and nothing would say so, which is the failure mode the guard exists to replace.
        """
        if not getattr(run_workflow._start_claims_root,
                       "_atmofab_private_root_guard_installed", False):
            self.skipTest("the suite's operator-private-root guard is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            env = {k: v for k, v in os.environ.items()
                   if k != run_workflow.START_CLAIMS_ROOT_ENV}
            with mock.patch.dict(os.environ, env, clear=True), \
                    self.assertRaisesRegex(
                        AssertionError, "operator's real secret root"), \
                    run_workflow._exclusive_claim(Path(tmp), "spec", "x"):
                pass

    def test_each_caught_type_degrades_to_proceed_and_leaves_no_exception_context(self) -> None:
        """The tuple, element by element, and the chaining the first version left behind.

        `except (OSError, RuntimeError, ValueError)` was pinned only through the
        `RuntimeError` arm, and a round-5 reviewer measured the other two as reachable
        rather than vacuous: `.absolute()` calls `getcwd()` and raises `OSError` when the
        working directory is gone, and the key's `.encode("utf-8")` raises a
        `UnicodeEncodeError` — a `ValueError` — for a surrogate in `repo_root`. Narrowing
        the tuple to `RuntimeError` alone left the whole suite green.

        The second assertion is the chaining. Yielding from inside the handler left the
        swallowed exception as `__context__` for the whole `with` body, so any later
        failure was reported "During handling of the above exception…" under an error
        this function decided did not matter — a false lead in the run's own error record.
        """
        for exc in (OSError("gone"), RuntimeError("no home"), ValueError("surrogate")):
            with self.subTest(raised=type(exc).__name__):
                held_value = None
                # One `with`: the claim exits FIRST (innermost), so `assertRaises`
                # catches what comes out of its `__exit__`, which is the point.
                with tempfile.TemporaryDirectory() as tmp, \
                        mock.patch.object(run_workflow, "_claim_lock_path",
                                          side_effect=exc), \
                        self.assertRaises(KeyError) as caught, \
                        run_workflow._exclusive_claim(Path(tmp), "spec", "x") as held:
                    held_value = held
                    raise KeyError("the real failure")
                self.assertTrue(held_value)
                self.assertIsNone(
                    caught.exception.__context__,
                    "a later failure is chained under the claim error the claim "
                    "decided did not matter — the `yield` is inside the handler")

    def test_the_claim_path_follows_the_module_level_resolver(self) -> None:
        """`_claim_lock_path` asks `_start_claims_root`, so patching it is enough.

        This is what makes the suite's session guard able to wrap ONE function and cover
        every claim the suite takes — the same arrangement as
        `orchestration_runtime._workflow_homes_root`. Patched through the module
        attribute rather than the closure, because that is how the guard installs itself.
        """
        with tempfile.TemporaryDirectory() as tmp:
            elsewhere = Path(tmp) / "elsewhere"
            with mock.patch.object(run_workflow, "_start_claims_root",
                                   return_value=elsewhere):
                path = run_workflow._claim_lock_path(Path(tmp), "spec", "spec/x")
            self.assertEqual(path.parent, elsewhere)

    def test_the_suites_own_claims_guard_raises_before_anything_is_created(self) -> None:
        """A witness for `tools/tests/conftest.py`'s guard over the claims root.

        The twin of `test_the_suites_own_homes_guard_raises_before_anything_is_created`
        in `tools/tests/test_orchestration_runtime.py`, and it exists for the same
        reason: a guard nothing observes is a claim, not a layer. Driven the way the
        accident would happen — the redirect removed, so the resolver falls back to
        `operator_secret_root()/start_claims` — and asserting BOTH halves of "prevent,
        not detect": it raises, and nothing appeared in the operator's tree.
        """
        from tools.hooks.common import operator_secret_root
        if not getattr(run_workflow._start_claims_root,
                       "_atmofab_private_root_guard_installed", False):
            # The subject is a pytest fixture. Under plain `unittest` there is no guard
            # to observe, and asserting anyway would fail for the absence of the harness.
            self.skipTest("the suite's operator-private-root guard is not installed")
        real_root = operator_secret_root() / "start_claims"
        existed_before = real_root.exists()
        env = {k: v for k, v in os.environ.items()
               if k != run_workflow.START_CLAIMS_ROOT_ENV}
        with mock.patch.dict(os.environ, env, clear=True), \
                self.assertRaisesRegex(AssertionError, "operator's real secret root"):
            run_workflow._start_claims_root()
        self.assertEqual(real_root.exists(), existed_before,
                         "the guard let something be created in the operator's tree")

    def test_cold_start_claim_is_exclusive_per_spec(self) -> None:
        # The guard alone cannot see a run that has not written its meta yet, so two
        # cold starts of one spec launched together both scan clean. The claim is what
        # closes that window; it is per (repo, spec) and released on exit.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            with run_workflow._exclusive_claim(repo_root, "spec", "spec/problem/test.md") as a:
                self.assertTrue(a)
                with run_workflow._exclusive_claim(
                    repo_root, "spec", "spec/problem/test.md"
                ) as b:
                    self.assertFalse(b, "a second claim on the same spec must refuse")
                # A different spec, and a different repo, are independent claims.
                with run_workflow._exclusive_claim(repo_root, "spec", "spec/problem/other.md") as c:
                    self.assertTrue(c)
                with run_workflow._exclusive_claim(Path(tmp) / "elsewhere", "spec",
                                                  "spec/problem/test.md") as d:
                    self.assertTrue(d)
            # Released on exit: the same claim is available again.
            with run_workflow._exclusive_claim(repo_root, "spec", "spec/problem/test.md") as e:
                self.assertTrue(e)

    def test_concurrent_cold_start_is_refused_before_any_state_is_created(self) -> None:
        # End to end: with the claim held (as a genuinely concurrent run would hold
        # it), a cold run refuses with `concurrent_orchestration_running` and issues no
        # runtime call at all — nothing is initialized behind the refusal.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            with run_workflow._exclusive_claim(repo_root, "spec", "spec/problem/test.md") as held:
                self.assertTrue(held)
                code, out, calls = self._run_main_with_fake_runtime(
                    ["spec/problem/test.md", "build", "--repo-root", str(repo_root),
                     "--orchestration-id", "orch_second", "--no-run-conductor"]
                )
            self.assertEqual(code, 2, out)
            self.assertEqual(out["reason"], "concurrent_orchestration_running")
            self.assertEqual(out["spec_ref"], "spec/problem/test.md")
            self.assertEqual(calls, [])

    def test_cold_start_claim_is_released_for_the_next_run(self) -> None:
        # The claim must not outlive the run that took it: a second cold start after
        # the first returns has to be able to proceed.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            for oid in ("orch_first", "orch_second"):
                code, out, calls = self._run_main_with_fake_runtime(
                    ["spec/problem/test.md", "build", "--repo-root", str(repo_root),
                     "--orchestration-id", oid, "--no-run-conductor"]
                )
                self.assertEqual(code, 0, out)
                self.assertIn("init", [c[0] for c in calls])

    def test_concurrent_resume_of_one_orchestration_is_refused(self) -> None:
        # The liveness gate cannot close this one: two `--resume` invocations of the
        # same run both observe the same dead driver, both terminalize it, and both
        # reach `init --resume-from-checkpoint`, which PRESERVES
        # `orchestration_agent_run_id` — so both would drive one orchestration through
        # one `workspace/tmp/<arid>`, and whichever finishes first deletes the other's.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            oid = "orch_20260101T000000Z_aaaaaaaa"
            self._seed_resumable_orchestration(
                repo_root, oid, spec_ref="spec/problem/test.md", until_phase="Build",
                mode="dev", backend="claude",
            )
            with run_workflow._exclusive_claim(repo_root, "orch", oid) as held:
                self.assertTrue(held)
                code, out, calls = self._run_main_with_fake_runtime(
                    ["--resume", "--repo-root", str(repo_root), "--no-run-conductor"]
                )
            self.assertEqual(code, 2, out)
            self.assertEqual(out["reason"], "concurrent_orchestration_running")
            self.assertEqual(out["orchestration_id"], oid)
            # Refused before init: no orchestration_agent_run_id was ever claimed.
            self.assertNotIn("init", [c[0] for c in calls])
            # Released with the run: the next resume proceeds.
            code2, out2, calls2 = self._run_main_with_fake_runtime(
                ["--resume", "--repo-root", str(repo_root), "--no-run-conductor"]
            )
            self.assertEqual(code2, 0, out2)
            self.assertIn("init", [c[0] for c in calls2])

    def test_orchestration_claim_outlives_the_tmp_cleanup(self) -> None:
        # Ordering matters: a waiting driver that acquired the claim while this one is
        # still deleting workspace/tmp/<arid> would re-init into the directory being
        # removed. Observed directly — a second claim taken from inside the cleanup
        # must still be refused.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            observed: list[bool] = []
            original_rmtree = run_workflow.shutil.rmtree

            def observing_rmtree(path, **kwargs):  # type: ignore[no-untyped-def]
                with run_workflow._exclusive_claim(
                    repo_root, "orch", "orch_cleanup"
                ) as free:
                    observed.append(free)
                return original_rmtree(path, **kwargs)

            run_workflow.shutil.rmtree = observing_rmtree  # type: ignore[assignment]
            try:
                code, out, _calls = self._run_main_with_fake_runtime(
                    ["spec/problem/test.md", "build", "--repo-root", str(repo_root),
                     "--orchestration-id", "orch_cleanup", "--no-run-conductor"]
                )
            finally:
                run_workflow.shutil.rmtree = original_rmtree  # type: ignore[assignment]
            self.assertEqual(code, 0, out)
            self.assertEqual(observed, [False],
                             "the claim must still be held while tmp is being removed")

    def test_orchestration_claim_is_keyed_to_the_orchestration(self) -> None:
        # A claim on some OTHER orchestration must not block this one.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            with run_workflow._exclusive_claim(repo_root, "orch", "orch_unrelated"):
                code, out, calls = self._run_main_with_fake_runtime(
                    ["spec/problem/test.md", "build", "--repo-root", str(repo_root),
                     "--orchestration-id", "orch_mine", "--no-run-conductor"]
                )
            self.assertEqual(code, 0, out)
            self.assertIn("init", [c[0] for c in calls])

    def test_a_resume_is_serialized_against_a_cold_run_of_the_same_spec(self) -> None:
        # The spec claim is not a cold-start-only guard: a resume and a cold run of one
        # spec derive their pipeline_id from the same
        # workspace/pipelines/<node_key_safe>/ tree and then write into it, so they
        # corrupt each other exactly as two cold runs would. The resume takes the spec
        # claim inside `_run_node` (its own liveness gate is about the orchestration,
        # which says nothing about a DIFFERENT orchestration for the same spec).
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            self._seed_resumable_orchestration(
                repo_root, "orch_20260101T000000Z_aaaaaaaa",
                spec_ref="spec/problem/test.md", until_phase="Build",
                mode="dev", backend="claude",
            )
            with run_workflow._exclusive_claim(
                repo_root, "spec", "spec/problem/test.md"
            ) as held:
                self.assertTrue(held)
                code, out, calls = self._run_main_with_fake_runtime(
                    ["--resume", "--repo-root", str(repo_root), "--no-run-conductor"]
                )
            self.assertEqual(code, 2, out)
            self.assertEqual(out["reason"], "concurrent_orchestration_running")
            self.assertEqual(out["spec_ref"], "spec/problem/test.md")
            self.assertNotIn("init", [c[0] for c in calls])
            # Released with the cold run: the resume then proceeds.
            code2, out2, calls2 = self._run_main_with_fake_runtime(
                ["--resume", "--repo-root", str(repo_root), "--no-run-conductor"]
            )
            self.assertEqual(code2, 0, out2)
            self.assertIn("init", [c[0] for c in calls2])

    def test_resume_claims_the_orchestration_before_probing_its_driver(self) -> None:
        # The probe's `dead` verdict authorizes a WRITE on another run's meta
        # (`set-status fail/driver_crashed`). If the claim came after that decision,
        # two resumes of one corpse would both perform it — the second landing after
        # the first had reset the meta to `running`, flipping an actively-resumed run
        # back to `fail`, which its later claim failure cannot undo. So the refusal has
        # to happen before any probe or terminalization.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            oid = "orch_20260101T000000Z_aaaaaaaa"
            self._seed_running_orchestration(repo_root, oid, verdict="dead")
            with run_workflow._exclusive_claim(repo_root, "orch", oid) as held:
                self.assertTrue(held)
                with self._forced_liveness():
                    code, out, calls = self._run_main_with_fake_runtime(
                        ["--resume", "--orchestration-id", oid,
                         "--repo-root", str(repo_root), "--no-run-conductor"]
                    )
            self.assertEqual(code, 2, out)
            self.assertEqual(out["reason"], "concurrent_orchestration_running")
            # No set-status: the dead-driver terminalization never ran.
            self.assertEqual(calls, [])

    def test_cold_guard_survives_an_uncapturable_driver_identity(self) -> None:
        # `_current_driver_identity()` returns None wherever /proc is unavailable —
        # the documented degrade path — and that None is handed straight to the guard.
        # It must still classify the candidate, not raise: an AttributeError here is an
        # uncaught crash inside the recovery gate, on the exact hosts that have no
        # other recovery signal.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            self._seed_resumable_orchestration(
                repo_root, "orch_prior", spec_ref="spec/problem/test.md",
                until_phase="Build", mode="dev", backend="claude", status="running",
                driver={"pid": _unused_pid(), "pid_start_ticks": "1",
                        "hostname": run_workflow._current_hostname()},
            )
            buf = io.StringIO()
            with redirect_stdout(buf):
                conflict = run_workflow._cold_start_running_guard(
                    repo_root, "spec/problem/test.md", stdout_format="jsonl",
                    driver_identity=None,
                )
            self.assertIsNone(conflict)
            warns = [json.loads(line) for line in buf.getvalue().splitlines()
                     if line.strip()]
            self.assertEqual([w["event"] for w in warns],
                             ["prior_incomplete_orchestration"])

    def test_cold_guard_rechecks_status_at_probe_time(self) -> None:
        # A candidate that terminalized between the scan and the probe must be dropped:
        # acting on it would refuse a cold run because of a finished orchestration and
        # point the operator at a `--resume` that makes no sense.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            oid = "orch_finished_since"
            self._seed_resumable_orchestration(
                repo_root, oid, spec_ref="spec/problem/test.md", until_phase="Build",
                mode="dev", backend="claude", status="running",
                driver={"pid": 424242, "verdict": "alive"},
            )
            meta_path = (repo_root / "workspace" / "orchestrations" / oid
                         / "orchestration_meta.json")
            original_index = run_workflow._index_incomplete_orchestrations_by_spec

            def index_then_terminalize(root):  # type: ignore[no-untyped-def]
                # Scan sees it as incomplete; it terminalizes before the probe reads it.
                index = original_index(root)
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                meta["status"] = "pass"
                meta_path.write_text(json.dumps(meta), encoding="utf-8")
                return index

            run_workflow._index_incomplete_orchestrations_by_spec = (  # type: ignore[assignment]
                index_then_terminalize)
            buf = io.StringIO()
            try:
                with self._forced_liveness(), redirect_stdout(buf):
                    conflict = run_workflow._cold_start_running_guard(
                        repo_root, "spec/problem/test.md", stdout_format="jsonl")
            finally:
                run_workflow._index_incomplete_orchestrations_by_spec = (  # type: ignore[assignment]
                    original_index)
            self.assertIsNone(conflict)
            self.assertEqual(buf.getvalue().strip(), "")

    def test_cold_run_ignores_running_orchestration_of_another_spec(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            self._seed_running_orchestration(
                repo_root, "orch_20260101T000000Z_aaaaaaaa", verdict="alive",
                spec_ref="spec/problem/other.md",
            )
            with self._forced_liveness():
                code, out, calls = self._run_main_with_fake_runtime(
                    ["spec/problem/test.md", "build", "--repo-root", str(repo_root),
                     "--orchestration-id", "orch_cold", "--no-run-conductor"]
                )
            self.assertEqual(code, 0, out)
            self.assertEqual(
                [e for e in self._last_events
                 if e.get("event") == "prior_incomplete_orchestration"],
                [],
            )
            self.assertIn("init", [c[0] for c in calls])

    @unittest.skipUnless(
        Path("/proc/self/stat").exists(), "driver identity capture requires Linux /proc"
    )
    def test_cold_init_records_driver_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            code, out, calls = self._run_main_with_fake_runtime(
                ["spec/problem/test.md", "build", "--repo-root", str(repo_root),
                 "--orchestration-id", "orch_cold", "--no-run-conductor"]
            )
            self.assertEqual(code, 0, out)
            init_call = [c for c in calls if c and c[0] == "init"][0]
            driver = json.loads(init_call[init_call.index("--driver-json") + 1])
            self.assertEqual(driver["pid"], os.getpid())
            self.assertTrue(driver["pid_start_ticks"].isdigit())

    @unittest.skipUnless(
        Path("/proc/self/stat").exists(), "driver identity capture requires Linux /proc"
    )
    def test_resume_init_refreshes_driver_identity(self) -> None:
        # The RESUMING process becomes the driver; leaving the dead one's pid recorded
        # would let a probe call the live resumed run dead (or, after pid reuse, alive
        # on an unrelated process).
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            self._seed_resumable_orchestration(
                repo_root, "orch_20260101T000000Z_aaaaaaaa",
                spec_ref="spec/problem/test.md", until_phase="Build",
                mode="dev", backend="claude",
            )
            code, out, calls = self._run_main_with_fake_runtime(
                ["--resume", "--repo-root", str(repo_root), "--no-run-conductor"]
            )
            self.assertEqual(code, 0, out)
            init_call = [c for c in calls if c and c[0] == "init"][0]
            self.assertIn("--resume-from-checkpoint", init_call)
            driver = json.loads(init_call[init_call.index("--driver-json") + 1])
            self.assertEqual(driver["pid"], os.getpid())

    def _run_cold_with_conductor(self, repo_root: Path, oid: str, exc: BaseException,
                                 stdout_format: str = "jsonl"):
        """Cold run whose conductor raises `exc`; returns the observed runtime calls."""
        import tools.workflow_conductor as workflow_conductor

        observed: list[list[str]] = []

        def fake_runtime_command(root, env, args):  # type: ignore[no-untyped-def]
            observed.append(args)
            if args[0] == "init":
                return run_workflow.RuntimeResult(
                    payload={"status": "ok",
                             "orchestration_agent_run_id": "orch_agent_run_002"},
                    raw_stdout="{}",
                )
            if args[0] == "preflight":
                return run_workflow.RuntimeResult(
                    payload={"status": "pass", "can_launch_step_agents": True,
                             "can_launch_substep_agents": True},
                    raw_stdout="{}",
                )
            return run_workflow.RuntimeResult(payload={"status": "ok"}, raw_stdout="{}")

        def raising_conductor(**_kwargs):  # type: ignore[no-untyped-def]
            raise exc

        original_runtime = run_workflow._runtime_command
        original_conductor = workflow_conductor.run_conductor
        run_workflow._runtime_command = fake_runtime_command  # type: ignore[assignment]
        workflow_conductor.run_conductor = raising_conductor  # type: ignore[assignment]
        try:
            with redirect_stdout(io.StringIO()):
                with self.assertRaises(type(exc)):
                    run_workflow.main(
                        ["spec/problem/test.md", "build", "--repo-root", str(repo_root),
                         "--orchestration-id", oid, "--stdout-format", stdout_format]
                    )
        finally:
            run_workflow._runtime_command = original_runtime  # type: ignore[assignment]
            workflow_conductor.run_conductor = original_conductor  # type: ignore[assignment]
        return observed

    def test_interrupted_run_is_terminalized_as_cancel(self) -> None:
        # Without this the meta stays `running` forever: an implicit --resume refuses it
        # and a cold re-run restarts the node from phase 1.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            calls = self._run_cold_with_conductor(
                repo_root, "orch_interrupt", KeyboardInterrupt())
            set_status = [c for c in calls if c and c[0] == "set-status"]
            self.assertEqual(len(set_status), 1)
            self.assertEqual(set_status[0][set_status[0].index("--status") + 1], "cancel")
            self.assertEqual(
                set_status[0][set_status[0].index("--reason-code") + 1],
                "driver_interrupted",
            )

    def test_sigterm_system_exit_is_terminalized_as_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            calls = self._run_cold_with_conductor(
                repo_root, "orch_sigterm", SystemExit(143))
            set_status = [c for c in calls if c and c[0] == "set-status"]
            self.assertEqual(len(set_status), 1)
            self.assertEqual(
                set_status[0][set_status[0].index("--reason-code") + 1],
                "driver_interrupted",
            )

    def test_interrupt_event_stays_raw_json_in_the_run_log(self) -> None:
        # `human` is the DEFAULT stdout format, and this event is the only new gate
        # event emitted from inside _run_node, where the tee is installed. The tee
        # mirrors inbound bytes verbatim into run_logs/*.jsonl, which is contracted to
        # hold the full JSON payload of every event (--stdout-format help + the
        # _StdoutTee docstring) and is parsed line-by-line by the timing-audit skill.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            oid = "orch_interrupt_human"
            self._run_cold_with_conductor(
                repo_root, oid, KeyboardInterrupt(), stdout_format="human")
            logs = sorted(
                (repo_root / "workspace" / "orchestrations" / oid / "run_logs").iterdir()
            )
            self.assertEqual(len(logs), 1)
            events = [
                json.loads(line)
                for line in logs[0].read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertIn("driver_interrupted", [e.get("event") for e in events])

    def test_interrupt_before_init_commits_does_not_terminalize(self) -> None:
        # Nothing to terminalize yet: init never returned, so there is no committed
        # orchestration this driver owns. Calling set-status here would either fail
        # (no meta) or, worse on a reused --orchestration-id, terminalize a run this
        # invocation never started.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            observed: list[list[str]] = []

            def interrupting_runtime(root, env, args):  # type: ignore[no-untyped-def]
                observed.append(args)
                raise KeyboardInterrupt()

            original = run_workflow._runtime_command
            run_workflow._runtime_command = interrupting_runtime  # type: ignore[assignment]
            try:
                with redirect_stdout(io.StringIO()):
                    with self.assertRaises(KeyboardInterrupt):
                        run_workflow.main(
                            ["spec/problem/test.md", "build", "--repo-root", str(repo_root),
                             "--orchestration-id", "orch_interrupt_at_init",
                             "--stdout-format", "jsonl"]
                        )
            finally:
                run_workflow._runtime_command = original  # type: ignore[assignment]
            self.assertEqual([c[0] for c in observed], ["init"])

    def test_interrupt_still_propagates_when_terminalization_fails(self) -> None:
        # The terminalization is best-effort by design: the process is on its way out
        # and the operator's Ctrl-C must surface as an interrupt, not as a RuntimeError
        # traceback from the set-status that happened to fail underneath it.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            import tools.workflow_conductor as workflow_conductor

            state = {"phase": "setup"}

            def fake_runtime_command(root, env, args):  # type: ignore[no-untyped-def]
                if state["phase"] == "interrupting":
                    raise RuntimeError("runtime command failed (set-status): disk full")
                if args[0] == "init":
                    return run_workflow.RuntimeResult(
                        payload={"status": "ok",
                                 "orchestration_agent_run_id": "orch_agent_run_002"},
                        raw_stdout="{}")
                return run_workflow.RuntimeResult(
                    payload={"status": "pass", "can_launch_step_agents": True,
                             "can_launch_substep_agents": True},
                    raw_stdout="{}")

            def raising_conductor(**_kwargs):  # type: ignore[no-untyped-def]
                state["phase"] = "interrupting"
                raise KeyboardInterrupt()

            original_runtime = run_workflow._runtime_command
            original_conductor = workflow_conductor.run_conductor
            run_workflow._runtime_command = fake_runtime_command  # type: ignore[assignment]
            workflow_conductor.run_conductor = raising_conductor  # type: ignore[assignment]
            try:
                with redirect_stdout(io.StringIO()):
                    # Must be KeyboardInterrupt, NOT the RuntimeError from set-status.
                    with self.assertRaises(KeyboardInterrupt):
                        run_workflow.main(
                            ["spec/problem/test.md", "build", "--repo-root",
                             str(repo_root), "--orchestration-id", "orch_swallow",
                             "--stdout-format", "jsonl"])
            finally:
                run_workflow._runtime_command = original_runtime  # type: ignore[assignment]
                workflow_conductor.run_conductor = original_conductor  # type: ignore[assignment]

    def test_interrupt_still_propagates_when_the_event_cannot_be_printed(self) -> None:
        # Second swallow: the terminalization committed, but stdout is gone (a closed
        # pipe when the terminal died with the operator's shell). Announcing it must
        # not turn a successful terminalization into a crash.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch_dir = repo_root / "workspace" / "orchestrations" / "orch_pipe"
            orch_dir.mkdir(parents=True, exist_ok=True)
            (orch_dir / "orchestration_meta.json").write_text(
                json.dumps({"orchestration_id": "orch_pipe", "status": "running"}),
                encoding="utf-8")

            class _BrokenStdout:
                def write(self, _text):  # type: ignore[no-untyped-def]
                    raise BrokenPipeError("stdout is gone")

                def flush(self):  # type: ignore[no-untyped-def]
                    raise BrokenPipeError("stdout is gone")

            calls: list[list[str]] = []

            def fake_runtime_command(root, env, args):  # type: ignore[no-untyped-def]
                calls.append(args)
                return run_workflow.RuntimeResult(payload={"status": "ok"}, raw_stdout="{}")

            original = run_workflow._runtime_command
            run_workflow._runtime_command = fake_runtime_command  # type: ignore[assignment]
            saved_stdout = sys.stdout
            sys.stdout = _BrokenStdout()  # type: ignore[assignment]
            try:
                # Returns normally; the set-status still committed.
                run_workflow._terminalize_interrupted_orchestration(
                    repo_root, {}, "orch_pipe")
            finally:
                sys.stdout = saved_stdout
                run_workflow._runtime_command = original  # type: ignore[assignment]
            self.assertEqual([c[0] for c in calls], ["set-status"])

    def test_interrupt_during_init_terminalizes_a_meta_this_run_committed(self) -> None:
        # `init_committed` only flips when the runtime call RETURNS, but the runtime
        # writes the `running` meta well before that (an operator token and ~130 more
        # lines follow, then the subprocess round-trip). A signal landing in that
        # window used to skip terminalization and leave the orchestration stuck at
        # `running` — the very state this whole change exists to prevent. The durable
        # evidence is the `driver` block: a meta naming THIS process was written by
        # this invocation's init.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            oid = "orch_interrupt_mid_init"
            identity = run_workflow._current_driver_identity()
            self.assertIsNotNone(identity)
            observed: list[list[str]] = []

            def runtime_that_commits_then_dies(root, env, args):  # type: ignore[no-untyped-def]
                observed.append(args)
                if args[0] == "init":
                    # Exactly what the real init does before it returns: commit the
                    # meta (with our driver block), then die before answering.
                    d = repo_root / "workspace" / "orchestrations" / oid
                    d.mkdir(parents=True, exist_ok=True)
                    (d / "orchestration_meta.json").write_text(
                        json.dumps({"orchestration_id": oid, "status": "running",
                                    "spec_ref": "spec/problem/test.md",
                                    "driver": {**identity, "recorded_at": "now"}}),
                        encoding="utf-8")
                    raise KeyboardInterrupt()
                return run_workflow.RuntimeResult(payload={"status": "ok"}, raw_stdout="{}")

            original = run_workflow._runtime_command
            run_workflow._runtime_command = runtime_that_commits_then_dies  # type: ignore[assignment]
            try:
                with redirect_stdout(io.StringIO()):
                    with self.assertRaises(KeyboardInterrupt):
                        run_workflow.main(
                            ["spec/problem/test.md", "build", "--repo-root",
                             str(repo_root), "--orchestration-id", oid,
                             "--stdout-format", "jsonl"])
            finally:
                run_workflow._runtime_command = original  # type: ignore[assignment]
            set_status = [c for c in observed if c and c[0] == "set-status"]
            self.assertEqual(len(set_status), 1)
            self.assertEqual(
                set_status[0][set_status[0].index("--reason-code") + 1],
                "driver_interrupted")

    def test_interrupt_during_init_leaves_another_runs_meta_alone(self) -> None:
        # The mirror of the case above: a reused `--orchestration-id` naming an
        # orchestration this invocation did NOT write must not be terminalized just
        # because we were interrupted near its id.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            oid = "orch_someone_elses"
            identity = run_workflow._current_driver_identity()
            self.assertIsNotNone(identity)
            d = repo_root / "workspace" / "orchestrations" / oid
            d.mkdir(parents=True, exist_ok=True)
            (d / "orchestration_meta.json").write_text(
                json.dumps({"orchestration_id": oid, "status": "running",
                            "spec_ref": "spec/problem/test.md",
                            # Another process's driver block. Both the verdict and the
                            # pid are pinned by construction: a `pid + 1` neighbour with
                            # our own start ticks can accidentally denote a live
                            # same-tick sibling, which makes the cold guard refuse
                            # before the runtime (hence the interrupt) is ever reached
                            # — issue #32. `dead` is the intended scenario here.
                            "driver": {**identity, "pid": _unallocatable_pid(),
                                       "verdict": "dead"}}),
                encoding="utf-8")
            observed: list[list[str]] = []

            def interrupting_runtime(root, env, args):  # type: ignore[no-untyped-def]
                observed.append(args)
                raise KeyboardInterrupt()

            original = run_workflow._runtime_command
            run_workflow._runtime_command = interrupting_runtime  # type: ignore[assignment]
            try:
                with self._forced_liveness(), redirect_stdout(io.StringIO()):
                    with self.assertRaises(KeyboardInterrupt):
                        run_workflow.main(
                            ["spec/problem/test.md", "build", "--repo-root",
                             str(repo_root), "--orchestration-id", oid,
                             "--stdout-format", "jsonl"])
            finally:
                run_workflow._runtime_command = original  # type: ignore[assignment]
            self.assertEqual([c for c in observed if c and c[0] == "set-status"], [])

    def test_interrupt_cannot_fire_when_a_live_foreign_driver_holds_the_id(self) -> None:
        # The `alive` half of the case above, on purpose: when the reused
        # `--orchestration-id` names a LIVE foreign driver, the cold guard refuses
        # before the runtime runs, so no interrupt can be raised and the other run's
        # meta stays untouched. (This is the mechanism that used to make the neighbor
        # test flaky — issue #32 — pinned here deliberately.)
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            oid = "orch_someone_elses_live"
            identity = run_workflow._current_driver_identity()
            self.assertIsNotNone(identity)
            d = repo_root / "workspace" / "orchestrations" / oid
            d.mkdir(parents=True, exist_ok=True)
            seeded = {"orchestration_id": oid, "status": "running",
                      "spec_ref": "spec/problem/test.md",
                      # Foreign by construction — see the neighbor above.
                      "driver": {**identity, "pid": _unallocatable_pid(),
                                 "verdict": "alive"}}
            meta_path = d / "orchestration_meta.json"
            meta_path.write_text(json.dumps(seeded), encoding="utf-8")
            observed: list[list[str]] = []

            def interrupting_runtime(root, env, args):  # type: ignore[no-untyped-def]
                observed.append(args)
                raise KeyboardInterrupt()

            original = run_workflow._runtime_command
            run_workflow._runtime_command = interrupting_runtime  # type: ignore[assignment]
            buf = io.StringIO()
            code = None
            reached_runtime = None
            try:
                with self._forced_liveness(), redirect_stdout(buf):
                    try:
                        code = run_workflow.main(
                            ["spec/problem/test.md", "build", "--repo-root",
                             str(repo_root), "--orchestration-id", oid,
                             "--stdout-format", "jsonl"])
                    except KeyboardInterrupt as exc:
                        # Caught, not propagated: an escaping KeyboardInterrupt aborts
                        # the whole pytest session (and reads like an operator Ctrl-C),
                        # which would hide the regression instead of reporting it.
                        reached_runtime = exc
            finally:
                run_workflow._runtime_command = original  # type: ignore[assignment]
            if reached_runtime is not None:
                self.fail(
                    "cold guard let the run reach the runtime despite a live foreign "
                    f"driver: {observed}")
            out = json.loads(buf.getvalue().strip().splitlines()[-1])
            self.assertEqual(code, 2, out)
            self.assertEqual(out["reason"], "concurrent_orchestration_running")
            # ("the runtime was never reached" is pinned by the `reached_runtime`
            # check above — the stub cannot be entered without raising.)
            self.assertEqual(
                json.loads(meta_path.read_text(encoding="utf-8")), seeded)

    def test_interrupt_preserves_an_already_terminal_status(self) -> None:
        # The conductor/runtime may have recorded a more specific terminal outcome
        # (e.g. fail_closed) just before the signal; terminal→terminal is rejected and
        # the specific narrative must win.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            oid = "orch_interrupt_terminal"
            orch_dir = repo_root / "workspace" / "orchestrations" / oid
            orch_dir.mkdir(parents=True, exist_ok=True)
            (orch_dir / "orchestration_meta.json").write_text(
                json.dumps({"orchestration_id": oid, "status": "fail_closed",
                            "spec_ref": "spec/problem/test.md"}),
                encoding="utf-8",
            )
            calls = self._run_cold_with_conductor(repo_root, oid, KeyboardInterrupt())
            self.assertEqual([c for c in calls if c and c[0] == "set-status"], [])

    @staticmethod
    @contextmanager
    def _mkdir_boom(name: str, parent_name: str, exc: BaseException):
        """Raise `exc` from exactly one `Path.mkdir` target, passing every other through.

        A blanket `Path.mkdir` failure would abort somewhere unrelated (the spec tree, the
        run-log dir) and the test would pass for the wrong reason."""
        real = Path.mkdir

        def boom(self, *a, **k):  # type: ignore[no-untyped-def]
            if self.name == name and self.parent.name == parent_name:
                raise exc
            return real(self, *a, **k)

        with mock.patch.object(Path, "mkdir", boom):
            yield

    def test_unexpected_oserror_after_init_terminalizes_and_reports(self) -> None:
        # The #37 window: `init` has committed (the meta says `running`), and a host-side
        # OSError — here the `workspace/tmp/<arid>` mkdir on a full disk — happens before
        # any handler that names it. Without the backstop it escapes to
        # `raise SystemExit(main())`: a traceback instead of the envelope, and an
        # orchestration stuck at `running` that only an explicit resume can clear.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            err = io.StringIO()
            with self._mkdir_boom(
                "orch_agent_run_002", "tmp",
                OSError(28, "No space left on device"),
            ), redirect_stderr(err):
                code, out, observed = self._run_main_with_fake_runtime(
                    ["spec/problem/test.md", "build", "--repo-root", str(repo_root),
                     "--orchestration-id", "orch_boom_nospace"])
            self.assertEqual(code, 2, out)
            self.assertEqual(out["status"], "fail")
            self.assertEqual(out["reason"], "driver_exception")
            self.assertEqual(out["orchestration_id"], "orch_boom_nospace")
            self.assertIn("No space left on device", out["detail"])
            set_status = [c for c in observed if c and c[0] == "set-status"]
            self.assertEqual(len(set_status), 1, observed)
            argv = set_status[0]
            # WHICH orchestration is terminalized is the whole point of the guards —
            # stamping the wrong id would leave the crashed one `running` forever.
            self.assertEqual(argv[argv.index("--orchestration-id") + 1],
                             "orch_boom_nospace")
            self.assertEqual(argv[argv.index("--status") + 1], "fail")
            self.assertEqual(argv[argv.index("--reason-code") + 1], "driver_exception")
            # The traceback still reaches stderr: a genuine bug stays debuggable while
            # stdout keeps its envelope contract.
            self.assertIn("Traceback", err.getvalue())

    def test_backstop_still_reports_when_terminalization_itself_fails(self) -> None:
        # The exception that reaches the backstop is typically the very condition that
        # also breaks the runtime subprocess (a full disk, a read-only workspace), so a
        # failing `set-status` is the LIKELY case, not an exotic one. Unswallowed it
        # would escape `_run_node` and restore the whole bug: traceback, no envelope,
        # orchestration left `running`. (Mirror of the interrupt path's
        # `test_interrupt_propagates_when_terminalization_fails`.)
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            oid = "orch_boom_and_setstatus_dies"
            observed: list[list[str]] = []

            def runtime_whose_set_status_dies(root, env, args):  # type: ignore[no-untyped-def]
                observed.append(args)
                if args[0] == "set-status":
                    raise RuntimeError("runtime command failed (set-status): ENOSPC")
                if args[0] == "init":
                    return run_workflow.RuntimeResult(
                        payload={"status": "ok",
                                 "orchestration_agent_run_id": "orch_agent_run_002"},
                        raw_stdout="{}")
                return run_workflow.RuntimeResult(payload={"status": "ok"}, raw_stdout="{}")

            original = run_workflow._runtime_command
            run_workflow._runtime_command = runtime_whose_set_status_dies  # type: ignore[assignment]
            buf = io.StringIO()
            try:
                with self._mkdir_boom(
                    "orch_agent_run_002", "tmp",
                    OSError(28, "No space left on device"),
                ), redirect_stderr(io.StringIO()), redirect_stdout(buf):
                    code = run_workflow.main(
                        ["spec/problem/test.md", "build", "--repo-root", str(repo_root),
                         "--orchestration-id", oid, "--stdout-format", "jsonl"])
            finally:
                run_workflow._runtime_command = original  # type: ignore[assignment]
            out = json.loads(buf.getvalue().strip().splitlines()[-1])
            self.assertEqual(code, 2, out)
            self.assertEqual(out["reason"], "driver_exception")
            # It really did try — the swallow is what keeps the envelope reachable.
            self.assertEqual(
                len([c for c in observed if c and c[0] == "set-status"]), 1, observed)

    def test_backstop_truncates_reason_detail_to_an_argv_safe_length(self) -> None:
        # `_runtime_command` passes `--reason-detail` as a single subprocess argv
        # element, and Linux caps one element at MAX_ARG_STRLEN (128 KiB) — this repo
        # has been bitten twice (#30/#31). A `RuntimeError` from `_runtime_command`
        # embeds the runtime subprocess's whole stderr, so a multi-KB detail is
        # reachable. Untruncated, `set-status` would raise E2BIG, the swallow above
        # would eat it, and the orchestration would silently stay `running`.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            huge = "HEAD-CONTEXT " + "x" * 200_000 + " ACTUAL-ROOT-CAUSE"
            with self._mkdir_boom(
                "orch_agent_run_002", "tmp", OSError(28, huge),
            ), redirect_stderr(io.StringIO()):
                code, out, observed = self._run_main_with_fake_runtime(
                    ["spec/problem/test.md", "build", "--repo-root", str(repo_root),
                     "--orchestration-id", "orch_huge_detail"])
            self.assertEqual(code, 2, out["reason"])
            status = next(c for c in observed if c and c[0] == "set-status")
            recorded = status[status.index("--reason-detail") + 1]
            # Short enough for the argv, and BOTH ends survive. A head-only cut is the
            # tempting implementation and the wrong one: `_runtime_command` builds its
            # message as `runtime command failed (<the whole argv>): <the real error>`,
            # so the head alone is pure context and the operator reading
            # `orchestration_meta.json#reason_detail` learns nothing.
            self.assertEqual(len(recorded), 200)
            self.assertTrue(recorded.startswith("OSError: [Errno 28] HEAD-CONTEXT"))
            self.assertTrue(recorded.endswith("ACTUAL-ROOT-CAUSE"))
            # The elision marker is the only signal that the middle is gone; without
            # it a spliced head+tail reads as one coherent sentence and the operator
            # draws a conclusion about a message that was never written.
            self.assertIn("...", recorded)
            # The envelope itself is untruncated — it never crosses an argv boundary.
            self.assertIn(huge, out["detail"])

    def test_a_runtime_command_failure_records_the_error_not_just_the_argv_echo(
        self,
    ) -> None:
        # The realistic shape of the detail on this path: `_runtime_command` prefixes
        # its message with the ENTIRE argv it ran, and a cold `init` / `preflight`
        # argv (repo paths, `--invocation-json`, `--driver-json`, a 64-char sha) is
        # already longer than the cap on its own. A head-only truncation would record
        # 200 characters of argv echo and drop every word about what went wrong.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            oid = "orch_argv_echo"
            long_argv = " ".join(
                ["preflight", "--repo-root", str(repo_root), "--orchestration-id", oid,
                 "--backend", "claude", "--agent-command", "claude", "--llm-config",
                 str(repo_root / "docs" / "examples" / "llm_claude.example.yaml"),
                 "--llm-config-sha256", "a" * 64])
            self.assertGreater(len(long_argv), 200)
            observed: list[list[str]] = []

            def runtime_whose_preflight_dies(root, env, args):  # type: ignore[no-untyped-def]
                observed.append(args)
                if args[0] == "init":
                    return run_workflow.RuntimeResult(
                        payload={"status": "ok",
                                 "orchestration_agent_run_id": "orch_agent_run_002"},
                        raw_stdout="{}")
                if args[0] == "preflight":
                    raise RuntimeError(
                        f"runtime command failed ({long_argv}): "
                        "ModuleNotFoundError: No module named 'yaml'")
                return run_workflow.RuntimeResult(payload={"status": "ok"}, raw_stdout="{}")

            original = run_workflow._runtime_command
            run_workflow._runtime_command = runtime_whose_preflight_dies  # type: ignore[assignment]
            buf = io.StringIO()
            try:
                with redirect_stdout(buf):
                    code = run_workflow.main(
                        ["spec/problem/test.md", "build", "--repo-root", str(repo_root),
                         "--orchestration-id", oid, "--stdout-format", "jsonl"])
            finally:
                run_workflow._runtime_command = original  # type: ignore[assignment]
            self.assertEqual(code, 2, buf.getvalue())
            status = next(c for c in observed if c and c[0] == "set-status")
            recorded = status[status.index("--reason-detail") + 1]
            self.assertLessEqual(len(recorded), 200)
            self.assertIn("No module named 'yaml'", recorded)
            # The head still names WHICH runtime command died.
            self.assertTrue(recorded.startswith("runtime command failed (preflight"))
            # The envelope keeps the whole message — it never crosses an argv boundary.
            out = json.loads(buf.getvalue().strip().splitlines()[-1])
            self.assertIn("No module named 'yaml'", out["detail"])
            self.assertIn(long_argv, out["detail"])

    def test_a_runtime_command_failure_after_init_is_terminalized_too(self) -> None:
        # The `except RuntimeError` handler spans `init` AND `preflight`, and returned
        # its envelope without terminalizing — so a preflight subprocess that died
        # after `init` committed left the orchestration `running` forever, the same
        # stuck state as an escaping exception. Same guards, more precise reason code.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            oid = "orch_preflight_subprocess_died"
            observed: list[list[str]] = []

            def runtime_whose_preflight_dies(root, env, args):  # type: ignore[no-untyped-def]
                observed.append(args)
                if args[0] == "init":
                    return run_workflow.RuntimeResult(
                        payload={"status": "ok",
                                 "orchestration_agent_run_id": "orch_agent_run_002"},
                        raw_stdout="{}")
                if args[0] == "preflight":
                    raise RuntimeError("runtime command failed (preflight): exit 1")
                return run_workflow.RuntimeResult(payload={"status": "ok"}, raw_stdout="{}")

            original = run_workflow._runtime_command
            run_workflow._runtime_command = runtime_whose_preflight_dies  # type: ignore[assignment]
            buf = io.StringIO()
            try:
                with redirect_stdout(buf):
                    code = run_workflow.main(
                        ["spec/problem/test.md", "build", "--repo-root", str(repo_root),
                         "--orchestration-id", oid, "--stdout-format", "jsonl"])
            finally:
                run_workflow._runtime_command = original  # type: ignore[assignment]
            out = json.loads(buf.getvalue().strip().splitlines()[-1])
            self.assertEqual(code, 2, out)
            self.assertEqual(out["reason"], "runtime_command_failed")
            set_status = [c for c in observed if c and c[0] == "set-status"]
            self.assertEqual(len(set_status), 1, observed)
            argv = set_status[0]
            self.assertEqual(argv[argv.index("--orchestration-id") + 1], oid)
            self.assertEqual(argv[argv.index("--status") + 1], "fail")
            self.assertEqual(argv[argv.index("--reason-code") + 1],
                             "runtime_command_failed")
            # This is the path the truncation exists for — a `_runtime_command`
            # RuntimeError embeds the runtime subprocess's own stderr — so the detail
            # must actually carry it rather than be dropped on the floor.
            self.assertIn("exit 1", argv[argv.index("--reason-detail") + 1])

    def test_a_runtime_command_failure_during_init_terminalizes_our_own_meta(
        self,
    ) -> None:
        # `init_committed` only flips when the runtime call RETURNS, and this handler
        # spans `init` too: an `init` that wrote the `running` meta and then failed —
        # here by answering without an `orchestration_agent_run_id` — reaches the
        # handler with `init_committed` still False. The `driver` block is then the
        # only proof the run is ours, and without it the orchestration stays `running`.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            oid = "orch_init_committed_then_failed"
            identity = run_workflow._current_driver_identity()
            self.assertIsNotNone(identity)
            observed: list[list[str]] = []

            def runtime_that_commits_then_answers_short(root, env, args):  # type: ignore[no-untyped-def]
                observed.append(args)
                if args[0] == "init":
                    d = repo_root / "workspace" / "orchestrations" / oid
                    d.mkdir(parents=True, exist_ok=True)
                    (d / "orchestration_meta.json").write_text(
                        json.dumps({"orchestration_id": oid, "status": "running",
                                    "spec_ref": "spec/problem/test.md",
                                    "driver": {**identity, "recorded_at": "now"}}),
                        encoding="utf-8")
                    return run_workflow.RuntimeResult(
                        payload={"status": "ok"}, raw_stdout="{}")
                return run_workflow.RuntimeResult(payload={"status": "ok"}, raw_stdout="{}")

            original = run_workflow._runtime_command
            run_workflow._runtime_command = runtime_that_commits_then_answers_short  # type: ignore[assignment]
            buf = io.StringIO()
            try:
                with redirect_stdout(buf):
                    code = run_workflow.main(
                        ["spec/problem/test.md", "build", "--repo-root", str(repo_root),
                         "--orchestration-id", oid, "--stdout-format", "jsonl"])
            finally:
                run_workflow._runtime_command = original  # type: ignore[assignment]
            out = json.loads(buf.getvalue().strip().splitlines()[-1])
            self.assertEqual(code, 2, out)
            self.assertEqual(out["reason"], "runtime_command_failed")
            set_status = [c for c in observed if c and c[0] == "set-status"]
            self.assertEqual(len(set_status), 1, observed)
            argv = set_status[0]
            self.assertEqual(argv[argv.index("--orchestration-id") + 1], oid)
            self.assertEqual(argv[argv.index("--reason-code") + 1],
                             "runtime_command_failed")

    def test_a_runtime_command_failure_leaves_another_runs_meta_alone(self) -> None:
        # The ownership half of the case above. `init` failing means this invocation
        # committed nothing, so a reused `--orchestration-id` that happens to name a
        # foreign `running` orchestration must not be terminalized: the other run's
        # driver is still the only thing entitled to decide its outcome.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            oid = "orch_rtfail_someone_elses"
            identity = run_workflow._current_driver_identity()
            self.assertIsNotNone(identity)
            d = repo_root / "workspace" / "orchestrations" / oid
            d.mkdir(parents=True, exist_ok=True)
            meta_path = d / "orchestration_meta.json"
            meta_path.write_text(
                json.dumps({"orchestration_id": oid, "status": "running",
                            "spec_ref": "spec/problem/test.md",
                            # Foreign by construction — unallocatable pid plus an
                            # explicit `dead` verdict, so the cold guard lets the run
                            # reach the runtime (issue #32).
                            "driver": {**identity, "pid": _unallocatable_pid(),
                                       "verdict": "dead"}}),
                encoding="utf-8")
            seeded_bytes = meta_path.read_bytes()
            observed: list[list[str]] = []

            def runtime_whose_init_dies(root, env, args):  # type: ignore[no-untyped-def]
                observed.append(args)
                raise RuntimeError("runtime command failed (init): exit 1")

            original = run_workflow._runtime_command
            run_workflow._runtime_command = runtime_whose_init_dies  # type: ignore[assignment]
            buf = io.StringIO()
            try:
                with self._forced_liveness(), redirect_stdout(buf):
                    code = run_workflow.main(
                        ["spec/problem/test.md", "build", "--repo-root", str(repo_root),
                         "--orchestration-id", oid, "--stdout-format", "jsonl"])
            finally:
                run_workflow._runtime_command = original  # type: ignore[assignment]
            out = json.loads(buf.getvalue().strip().splitlines()[-1])
            self.assertEqual(code, 2, out)
            self.assertEqual(out["reason"], "runtime_command_failed")
            self.assertEqual([c for c in observed if c and c[0] == "set-status"], [])
            self.assertEqual(meta_path.read_bytes(), seeded_bytes)

    def test_backstop_still_reports_when_the_traceback_print_fails(self) -> None:
        # The stderr report is diagnostics; the stdout envelope is the contract. A
        # broken stderr (a closed pipe — the shape this repo has been bitten by) must
        # not displace the envelope, or a caller parsing stdout sees nothing at all.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            with mock.patch("traceback.print_exc",
                            side_effect=BrokenPipeError("closed stderr")):
                with self._mkdir_boom(
                    "orch_agent_run_002", "tmp",
                    OSError(28, "No space left on device"),
                ):
                    code, out, observed = self._run_main_with_fake_runtime(
                        ["spec/problem/test.md", "build", "--repo-root", str(repo_root),
                         "--orchestration-id", "orch_stderr_gone"])
            self.assertEqual(code, 2, out)
            self.assertEqual(out["reason"], "driver_exception")
            # ...and the terminalization still happened, too.
            self.assertEqual(
                len([c for c in observed if c and c[0] == "set-status"]), 1, observed)

    def test_the_backstop_envelope_goes_through_the_tee(self) -> None:
        # Every other backstop test forces `--stdout-format jsonl`, which hides WHICH
        # stream the envelope is printed to. It must be the installed tee, not the
        # saved real stdout: the tee is what renders the human form the operator
        # actually reads (`human` is the default) and what mirrors the event into
        # `run_logs/run_*.jsonl`, the only copy that outlives the terminal.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            oid = "orch_tee_render"
            # Non-ASCII, so the raw JSONL copy also pins that the payload is written
            # readably rather than escaped into \uXXXX (asserted on the file's bytes
            # at the bottom — a decoded assertion could not tell the two apart).
            boom = OSError(28, "ディスクに空きがありません")

            def fake_runtime_command(root, env, args):  # type: ignore[no-untyped-def]
                if args[0] == "init":
                    return run_workflow.RuntimeResult(
                        payload={"status": "ok",
                                 "orchestration_agent_run_id": "orch_agent_run_002"},
                        raw_stdout="{}")
                return run_workflow.RuntimeResult(payload={"status": "ok"}, raw_stdout="{}")

            original = run_workflow._runtime_command
            run_workflow._runtime_command = fake_runtime_command  # type: ignore[assignment]
            buf = io.StringIO()
            try:
                with self._mkdir_boom("orch_agent_run_002", "tmp", boom), \
                        redirect_stderr(io.StringIO()), redirect_stdout(buf):
                    code = run_workflow.main(
                        ["spec/problem/test.md", "build", "--repo-root", str(repo_root),
                         "--orchestration-id", oid, "--stdout-format", "human"])
            finally:
                run_workflow._runtime_command = original  # type: ignore[assignment]
            self.assertEqual(code, 2, buf.getvalue())
            terminal = buf.getvalue().strip().splitlines()[-1]
            self.assertTrue(terminal.startswith("[FAIL] reason=driver_exception"),
                            terminal)
            self.assertIn("ディスクに空きがありません", terminal)
            logs = sorted(
                (repo_root / "workspace" / "orchestrations" / oid / "run_logs")
                .glob("run_*.jsonl"))
            self.assertEqual(len(logs), 1, logs)
            events = [json.loads(line)
                      for line in logs[0].read_text(encoding="utf-8").splitlines()
                      if line.strip()]
            envelope = events[-1]
            self.assertEqual(envelope["reason"], "driver_exception")
            self.assertIn("ディスクに空きがありません", envelope["detail"])
            # On the RAW bytes, not the decoded payload: `\uXXXX` escapes round-trip
            # through `json.loads`, so a decoded assertion cannot tell an escaped log
            # from a readable one, and the run log is read by a human.
            self.assertIn("ディスクに空きがありません",
                          logs[0].read_text(encoding="utf-8"))

    def test_a_terminal_status_is_preserved_whatever_its_spelling(self) -> None:
        # `set-status` writes the operator's `--status` through verbatim, and the
        # RUNBOOK documents a manual `set-status` recovery an operator types by hand.
        # So a terminal status can reach the guard as `FAIL_CLOSED` or ` fail_closed `,
        # and a case-sensitive comparison would miss it and overwrite the specific
        # narrative with the generic `driver_exception`.
        for spelling in ("FAIL_CLOSED", " fail_closed "):
            with self.subTest(spelling=spelling):
                with tempfile.TemporaryDirectory() as tmp:
                    repo_root = Path(tmp)
                    self._seed_spec_tree(repo_root)
                    oid = "orch_terminal_spelling"
                    orch_dir = repo_root / "workspace" / "orchestrations" / oid
                    orch_dir.mkdir(parents=True, exist_ok=True)
                    (orch_dir / "orchestration_meta.json").write_text(
                        json.dumps({"orchestration_id": oid, "status": spelling,
                                    "spec_ref": "spec/problem/test.md"}),
                        encoding="utf-8")
                    with self._mkdir_boom(
                        "orch_agent_run_002", "tmp",
                        OSError(28, "No space left on device"),
                    ), redirect_stderr(io.StringIO()):
                        code, out, observed = self._run_main_with_fake_runtime(
                            ["spec/problem/test.md", "build", "--repo-root",
                             str(repo_root), "--orchestration-id", oid])
                    self.assertEqual(code, 2, out)
                    self.assertEqual(out["reason"], "driver_exception")
                    self.assertEqual(
                        [c for c in observed if c and c[0] == "set-status"], [])

    def test_unwritable_workspace_tmp_reports_instead_of_escaping(self) -> None:
        # `workspace/tmp` is the driver's FIRST write into the workspace, so on a
        # read-only checkout it is the first thing to fail. It used to be created
        # before the try, outside the envelope contract, so that exact failure class
        # still escaped as a traceback even with the backstop in place.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            with self._mkdir_boom(
                "tmp", "workspace", OSError(30, "Read-only file system"),
            ), redirect_stderr(io.StringIO()):
                code, out, observed = self._run_main_with_fake_runtime(
                    ["spec/problem/test.md", "build", "--repo-root", str(repo_root),
                     "--orchestration-id", "orch_ro_workspace"])
            self.assertEqual(code, 2, out)
            self.assertEqual(out["reason"], "driver_exception")
            self.assertIn("Read-only file system", out["detail"])
            # `init` never ran, so there is nothing committed to terminalize.
            self.assertEqual([c for c in observed if c and c[0] == "set-status"], [])

    def test_unexpected_exception_before_init_commit_reports_without_terminalizing(
        self,
    ) -> None:
        # Nothing was committed, so there is no `running` to clear — but the stdout
        # envelope contract covers the whole function, so it still prints and exits 2.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            observed: list[list[str]] = []

            def runtime_that_dies_on_init(root, env, args):  # type: ignore[no-untyped-def]
                observed.append(args)
                if args[0] == "init":
                    raise ValueError("boom")
                return run_workflow.RuntimeResult(payload={"status": "ok"}, raw_stdout="{}")

            original = run_workflow._runtime_command
            run_workflow._runtime_command = runtime_that_dies_on_init  # type: ignore[assignment]
            buf = io.StringIO()
            try:
                with redirect_stderr(io.StringIO()), redirect_stdout(buf):
                    code = run_workflow.main(
                        ["spec/problem/test.md", "build", "--repo-root", str(repo_root),
                         "--orchestration-id", "orch_pre_init_boom",
                         "--stdout-format", "jsonl"])
            finally:
                run_workflow._runtime_command = original  # type: ignore[assignment]
            out = json.loads(buf.getvalue().strip().splitlines()[-1])
            self.assertEqual(code, 2, out)
            self.assertEqual(out["reason"], "driver_exception")
            # Type-prefixed: a bare `KeyError` would stringify to just `'key'`.
            self.assertEqual(out["detail"], "ValueError: boom")
            self.assertEqual([c for c in observed if c and c[0] == "set-status"], [])

    def test_unexpected_exception_during_init_terminalizes_a_meta_this_run_committed(
        self,
    ) -> None:
        # The `init_committed` flag only flips when the runtime call RETURNS, so an
        # exception raised after the meta is on disk falls back to the durable evidence:
        # a `driver` block naming THIS process was written by this invocation's init.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            oid = "orch_exc_mid_init"
            identity = run_workflow._current_driver_identity()
            self.assertIsNotNone(identity)
            observed: list[list[str]] = []

            def runtime_that_commits_then_dies(root, env, args):  # type: ignore[no-untyped-def]
                observed.append(args)
                if args[0] == "init":
                    d = repo_root / "workspace" / "orchestrations" / oid
                    d.mkdir(parents=True, exist_ok=True)
                    (d / "orchestration_meta.json").write_text(
                        json.dumps({"orchestration_id": oid, "status": "running",
                                    "spec_ref": "spec/problem/test.md",
                                    "driver": {**identity, "recorded_at": "now"}}),
                        encoding="utf-8")
                    raise ValueError("boom")
                return run_workflow.RuntimeResult(payload={"status": "ok"}, raw_stdout="{}")

            original = run_workflow._runtime_command
            run_workflow._runtime_command = runtime_that_commits_then_dies  # type: ignore[assignment]
            buf = io.StringIO()
            try:
                with redirect_stderr(io.StringIO()), redirect_stdout(buf):
                    code = run_workflow.main(
                        ["spec/problem/test.md", "build", "--repo-root", str(repo_root),
                         "--orchestration-id", oid, "--stdout-format", "jsonl"])
            finally:
                run_workflow._runtime_command = original  # type: ignore[assignment]
            out = json.loads(buf.getvalue().strip().splitlines()[-1])
            self.assertEqual(code, 2, out)
            self.assertEqual(out["reason"], "driver_exception")
            set_status = [c for c in observed if c and c[0] == "set-status"]
            self.assertEqual(len(set_status), 1, observed)
            self.assertEqual(
                set_status[0][set_status[0].index("--reason-code") + 1],
                "driver_exception")

    def test_unexpected_exception_during_init_leaves_another_runs_meta_alone(self) -> None:
        # The mirror: a reused `--orchestration-id` naming an orchestration this
        # invocation did NOT write must not be terminalized just because we crashed
        # near its id.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            oid = "orch_exc_someone_elses"
            identity = run_workflow._current_driver_identity()
            self.assertIsNotNone(identity)
            d = repo_root / "workspace" / "orchestrations" / oid
            d.mkdir(parents=True, exist_ok=True)
            seeded = {"orchestration_id": oid, "status": "running",
                      "spec_ref": "spec/problem/test.md",
                      # Foreign by construction: an unallocatable pid plus an explicit
                      # `dead` verdict, so no live same-tick sibling can make the cold
                      # guard refuse before the runtime is reached — issue #32.
                      "driver": {**identity, "pid": _unallocatable_pid(),
                                 "verdict": "dead"}}
            meta_path = d / "orchestration_meta.json"
            meta_path.write_text(json.dumps(seeded), encoding="utf-8")
            seeded_bytes = meta_path.read_bytes()
            observed: list[list[str]] = []

            def raising_runtime(root, env, args):  # type: ignore[no-untyped-def]
                observed.append(args)
                raise ValueError("boom")

            original = run_workflow._runtime_command
            run_workflow._runtime_command = raising_runtime  # type: ignore[assignment]
            buf = io.StringIO()
            try:
                with self._forced_liveness(), redirect_stderr(io.StringIO()), \
                        redirect_stdout(buf):
                    code = run_workflow.main(
                        ["spec/problem/test.md", "build", "--repo-root", str(repo_root),
                         "--orchestration-id", oid, "--stdout-format", "jsonl"])
            finally:
                run_workflow._runtime_command = original  # type: ignore[assignment]
            out = json.loads(buf.getvalue().strip().splitlines()[-1])
            self.assertEqual(code, 2, out)
            self.assertEqual(out["reason"], "driver_exception")
            self.assertEqual([c for c in observed if c and c[0] == "set-status"], [])
            self.assertEqual(meta_path.read_bytes(), seeded_bytes)

    def test_unexpected_exception_preserves_an_already_terminal_status(self) -> None:
        # A more specific terminal status recorded before the exception wins: the
        # generic `driver_exception` must not clobber e.g. a `fail_closed` narrative.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            oid = "orch_exc_terminal"
            orch_dir = repo_root / "workspace" / "orchestrations" / oid
            orch_dir.mkdir(parents=True, exist_ok=True)
            (orch_dir / "orchestration_meta.json").write_text(
                json.dumps({"orchestration_id": oid, "status": "fail_closed",
                            "spec_ref": "spec/problem/test.md"}),
                encoding="utf-8",
            )
            with self._mkdir_boom(
                "orch_agent_run_002", "tmp",
                OSError(28, "No space left on device"),
            ), redirect_stderr(io.StringIO()):
                code, out, observed = self._run_main_with_fake_runtime(
                    ["spec/problem/test.md", "build", "--repo-root", str(repo_root),
                     "--orchestration-id", oid])
            self.assertEqual(code, 2, out)
            self.assertEqual(out["reason"], "driver_exception")
            self.assertEqual([c for c in observed if c and c[0] == "set-status"], [])

    def test_resume_refuses_running_latest_without_explicit_id(self) -> None:
        # Implicit `--resume` must not auto-attach to a non-terminal (running) latest.
        # This seeds NO driver block, so the probe answers `unknown` — the branch that
        # preserves both pre-liveness behaviors (implicit refuses, explicit bypasses).
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            oid = "orch_20260101T000000Z_aaaaaaaa"
            self._seed_resumable_orchestration(
                repo_root, oid, spec_ref="spec/problem/test.md",
                until_phase="Build", mode="dev", backend="claude",
            )
            meta_path = (
                repo_root / "workspace" / "orchestrations" / oid / "orchestration_meta.json"
            )
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["status"] = "running"
            meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

            code, out, calls = self._run_main_with_fake_runtime(
                ["--resume", "--repo-root", str(repo_root), "--no-run-conductor"]
            )
            self.assertEqual(code, 2)
            self.assertEqual(out["reason"], "latest_orchestration_not_resumable")
            self.assertEqual(calls, [])

            # An explicit --orchestration-id bypasses the guard (deliberate choice).
            code2, out2, _ = self._run_main_with_fake_runtime(
                ["--resume", "--orchestration-id", oid,
                 "--repo-root", str(repo_root), "--no-run-conductor"]
            )
            self.assertEqual(code2, 0, out2)
            self.assertEqual(out2["orchestration_id"], oid)

    def test_resume_passes_overridden_spec_ref_to_init(self) -> None:
        # An explicit spec_ref override on resume must be forwarded to
        # init --resume-from-checkpoint so meta is updated (not left stale).
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            (repo_root / "spec" / "other").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "other" / "alt.md").write_text("spec\n", encoding="utf-8")
            (repo_root / "spec" / "other" / "deps.yaml").write_text("nodes: []\n", encoding="utf-8")
            self._seed_resumable_orchestration(
                repo_root, "orch_20260101T000000Z_aaaaaaaa", spec_ref="spec/problem/test.md",
                until_phase="Build", mode="dev", backend="claude",
            )
            code, out, calls = self._run_main_with_fake_runtime(
                ["--resume", "spec/other/alt.md",
                 "--repo-root", str(repo_root), "--no-run-conductor"]
            )
            self.assertEqual(code, 0, out)
            self.assertEqual(out["target_spec_ref"], "spec/other/alt.md")
            init_calls = [c for c in calls if c and c[0] == "init"]
            idx = init_calls[0].index("--spec-ref")
            self.assertEqual(init_calls[0][idx + 1], "spec/other/alt.md")
            # Overridden spec rediscovers its own deps, not the recovered one.
            didx = init_calls[0].index("--source-dependency-ref")
            self.assertEqual(init_calls[0][didx + 1], "spec/other/deps.yaml")

    def test_resume_fails_when_no_orchestration_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            code, out, calls = self._run_main_with_fake_runtime(
                ["--resume", "--repo-root", str(repo_root), "--no-run-conductor"]
            )
            self.assertEqual(code, 2)
            self.assertEqual(out["reason"], "no_resumable_orchestration")
            self.assertEqual(calls, [])

    def test_resume_fails_when_until_phase_unrecoverable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            oid = "orch_20260101T000000Z_aaaaaaaa"
            self._seed_resumable_orchestration(
                repo_root, oid, spec_ref="spec/problem/test.md",
                until_phase="Build", mode="dev", backend="claude",
            )
            # Corrupt the prompt so until_phase/mode cannot be extracted.
            (
                repo_root / "workspace" / "orchestrations" / oid
                / "launches" / "orchestration.start.prompt.txt"
            ).write_text("no parseable params here\n", encoding="utf-8")
            code, out, calls = self._run_main_with_fake_runtime(
                ["--resume", "--repo-root", str(repo_root), "--no-run-conductor"]
            )
            self.assertEqual(code, 2)
            self.assertEqual(out["reason"], "resume_params_unrecoverable")
            self.assertIn("until_phase", out["detail"])
            self.assertEqual(calls, [])

    # ------------------------------------------------------------------
    # invocation record + closure-aware resume
    # ------------------------------------------------------------------
    def test_build_invocation_record_single_node_has_no_closure(self) -> None:
        rec = run_workflow._build_invocation_record(
            argv=["spec/problem/a", "validate"],
            spec_ref="spec/problem/a",
            until_phase="Validate",
            llm="claude",
            llm_command="claude",
            workflow_mode="dev",
            agent_model="opus",
            with_deps=False,
        )
        self.assertEqual(rec["argv"], ["spec/problem/a", "validate"])
        self.assertEqual(rec["generate_executor"], "pure")  # M-F: always the hardcoded provenance
        self.assertIn("python3 tools/run_workflow.py", rec["command"])
        self.assertEqual(rec["spec_ref"], "spec/problem/a")
        self.assertEqual(rec["until_phase"], "Validate")
        self.assertEqual(rec["agent_model"], "opus")
        self.assertFalse(rec["with_deps"])
        self.assertFalse(rec["wait_usage_reset"])  # provenance stamp, default OFF
        self.assertNotIn("closure_id", rec)

    def test_build_invocation_record_records_wait_usage_reset(self) -> None:
        rec = run_workflow._build_invocation_record(
            argv=["spec/problem/a", "validate", "--wait-usage-reset"],
            spec_ref="spec/problem/a",
            until_phase="Validate",
            llm="claude",
            llm_command="claude",
            workflow_mode="dev",
            agent_model="opus",
            with_deps=False,
            wait_usage_reset=True,
        )
        self.assertTrue(rec["wait_usage_reset"])

    def test_build_invocation_record_closure_fields_present(self) -> None:
        rec = run_workflow._build_invocation_record(
            argv=["spec/problem/a", "validate", "--with-deps"],
            spec_ref="spec/component/c",
            until_phase="Validate",
            llm="claude",
            llm_command="claude",
            workflow_mode="dev",
            agent_model=None,
            with_deps=True,
            closure_id="orch_target",
            closure_target_spec_ref="spec/problem/a",
            closure_until_phase="Validate",
        )
        self.assertTrue(rec["with_deps"])
        self.assertEqual(rec["closure_id"], "orch_target")
        self.assertEqual(rec["closure_target_spec_ref"], "spec/problem/a")
        self.assertEqual(rec["closure_until_phase"], "Validate")
        # agent_model omitted when falsy
        self.assertNotIn("agent_model", rec)

    def test_load_resume_params_recovers_closure_from_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            oid = "orch_20260101T000000Z_dep00000"
            self._seed_resumable_orchestration(
                repo_root, oid, spec_ref="spec/component/c",
                until_phase="Validate", mode="dev", backend="claude",
                invocation={
                    "closure_id": "orch_target",
                    "closure_target_spec_ref": "spec/problem/a",
                    "closure_until_phase": "Validate",
                },
            )
            params = run_workflow._load_resume_params(repo_root, oid)
            self.assertEqual(params["closure_id"], "orch_target")
            self.assertEqual(params["closure_target_spec_ref"], "spec/problem/a")
            self.assertEqual(params["closure_until_phase"], "Validate")

    def test_load_resume_params_closure_none_when_no_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            oid = "orch_20260101T000000Z_legacy00"
            self._seed_resumable_orchestration(
                repo_root, oid, spec_ref="spec/problem/test.md",
                until_phase="Validate", mode="dev", backend="claude",
            )
            params = run_workflow._load_resume_params(repo_root, oid)
            self.assertIsNone(params["closure_id"])
            self.assertIsNone(params["closure_target_spec_ref"])
            self.assertIsNone(params["closure_until_phase"])
            # non-closure params still recovered
            self.assertEqual(params["spec_ref"], "spec/problem/test.md")

    # --- Z2 executor provenance + M-F legacy-removal fail-close ----------------
    def test_build_invocation_record_persists_generate_executor(self) -> None:
        # M-F: the executor is no longer a per-run choice; the record always stamps "pure" as a
        # provenance value (the `generate_executor` kwarg was removed from the builder).
        rec = run_workflow._build_invocation_record(
            argv=["spec/problem/a", "generate"], spec_ref="spec/problem/a",
            until_phase="Generate", llm="claude", llm_command="claude",
            workflow_mode="dev", agent_model=None, with_deps=False)
        self.assertEqual(rec["generate_executor"], "pure")

    def test_load_resume_params_recovers_generate_executor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            oid = "orch_20260101T000000Z_pure0000"
            self._seed_resumable_orchestration(
                repo_root, oid, spec_ref="spec/problem/test.md",
                until_phase="Generate", mode="dev", backend="claude",
                invocation={"generate_executor": "pure"})
            params = run_workflow._load_resume_params(repo_root, oid)
            self.assertEqual(params["generate_executor"], "pure")
            # An orchestration predating the field recovers None — the M-F resume gate rejects it
            # (see test_resume_prefield_orchestration_fails_closed).
            oid2 = "orch_20260101T000000Z_nofield0"
            self._seed_resumable_orchestration(
                repo_root, oid2, spec_ref="spec/problem/test.md",
                until_phase="Generate", mode="dev", backend="claude",
                invocation={}, record_executor=None)
            self.assertIsNone(run_workflow._load_resume_params(repo_root, oid2)["generate_executor"])

    def _resume_capture(self, repo_root: Path, oid: str,
                        extra_argv: list[str]) -> tuple[int, dict]:
        """Resume with the fake runtime and return (exit_code, final_json)."""
        code, out, _ = self._run_main_with_fake_runtime(
            ["--resume", "--repo-root", str(repo_root), "--no-run-conductor", *extra_argv])
        return code, out

    def test_resume_pure_recorded_orchestration_succeeds(self) -> None:
        # M-F: a pure-recorded run resumes normally, and the executor env is NOT touched (the env
        # var was removed — the executor is no longer threaded through the environment).
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            oid = "orch_20260101T000000Z_pure0001"
            self._seed_resumable_orchestration(
                repo_root, oid, spec_ref="spec/problem/test.md", until_phase="Generate",
                mode="dev", backend="claude", invocation={"generate_executor": "pure"})
            prev = os.environ.pop("ATMOFAB_GENERATE_EXECUTOR", None)
            try:
                code, out = self._resume_capture(repo_root, oid, [])
                self.assertEqual(code, 0, out)
                self.assertNotIn("ATMOFAB_GENERATE_EXECUTOR", os.environ)
            finally:
                if prev is not None:
                    os.environ["ATMOFAB_GENERATE_EXECUTOR"] = prev

    def test_resume_legacy_recorded_orchestration_fails_closed(self) -> None:
        # M-F: a legacy-recorded run cannot be resumed — legacy execution was removed. Resume must
        # fail-closed with generate_executor_legacy_removed, NOT silently switch to pure.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            oid = "orch_20260101T000000Z_legacy01"
            self._seed_resumable_orchestration(
                repo_root, oid, spec_ref="spec/problem/test.md", until_phase="Generate",
                mode="dev", backend="claude", invocation={"generate_executor": "legacy"})
            code, out = self._resume_capture(repo_root, oid, [])
            self.assertEqual(code, 2, out)
            self.assertEqual(out["reason"], "generate_executor_legacy_removed")

    def test_resume_prefield_orchestration_fails_closed(self) -> None:
        # An orchestration predating the field recovers None -> a pre-adoption legacy run -> the
        # same fail-close (inversion of the old "stays legacy" behavior).
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            oid = "orch_20260101T000000Z_nofield1"
            self._seed_resumable_orchestration(
                repo_root, oid, spec_ref="spec/problem/test.md", until_phase="Generate",
                mode="dev", backend="claude", invocation={}, record_executor=None)
            code, out = self._resume_capture(repo_root, oid, [])
            self.assertEqual(code, 2, out)
            self.assertEqual(out["reason"], "generate_executor_legacy_removed")

    def test_resume_garbage_recorded_executor_fails_closed(self) -> None:
        # A garbage recorded value ("pur") must NEVER be read as pure — fail-closed with the same
        # reason (pins that the gate does not do a fuzzy pure match).
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            oid = "orch_20260101T000000Z_garbage1"
            self._seed_resumable_orchestration(
                repo_root, oid, spec_ref="spec/problem/test.md", until_phase="Generate",
                mode="dev", backend="claude", invocation={"generate_executor": "pur"})
            code, out = self._resume_capture(repo_root, oid, [])
            self.assertEqual(code, 2, out)
            self.assertEqual(out["reason"], "generate_executor_legacy_removed")

    def test_ambient_env_executor_is_inert(self) -> None:
        # M-F: ATMOFAB_GENERATE_EXECUTOR was removed and is fully inert. A stale ambient value (even
        # an old "legacy" or a typo) neither blocks a pure resume nor changes its outcome.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            oid = "orch_20260101T000000Z_pure0004"
            self._seed_resumable_orchestration(
                repo_root, oid, spec_ref="spec/problem/test.md", until_phase="Generate",
                mode="dev", backend="claude", invocation={"generate_executor": "pure"})
            prev = os.environ.get("ATMOFAB_GENERATE_EXECUTOR")
            os.environ["ATMOFAB_GENERATE_EXECUTOR"] = "legacy"  # stale ambient value: must be inert
            try:
                code, out = self._resume_capture(repo_root, oid, [])
                self.assertEqual(code, 0, out)
            finally:
                if prev is not None:
                    os.environ["ATMOFAB_GENERATE_EXECUTOR"] = prev
                else:
                    os.environ.pop("ATMOFAB_GENERATE_EXECUTOR", None)

    def test_generate_executor_flag_removed(self) -> None:
        # M-F: the --generate-executor flag was deleted. A cold run that still passes it (legacy OR
        # pure) is rejected at argparse — SystemExit(2), not a JSON envelope.
        import contextlib
        for value in ("legacy", "pure"):
            with self.assertRaises(SystemExit) as ctx, \
                    contextlib.redirect_stderr(io.StringIO()):
                run_workflow.main(
                    ["spec/problem/test.md", "generate", "--generate-executor", value])
            self.assertEqual(ctx.exception.code, 2)

    def test_index_closure_orchestrations_latest_wins_and_filters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)

            def seed(oid, spec, closure, started):
                self._seed_resumable_orchestration(
                    repo_root, oid, spec_ref=spec, until_phase="Validate",
                    mode="dev", backend="claude", started_at=started,
                    invocation={"closure_id": closure},
                )

            # two orchs for the same spec under one closure — latest started_at wins
            seed("orch_c_old", "spec/component/c", "orch_target",
                 "2026-01-01T00:00:00.000000Z")
            seed("orch_c_new", "spec/component/c", "orch_target",
                 "2026-02-01T00:00:00.000000Z")
            seed("orch_b", "spec/component/b", "orch_target",
                 "2026-01-15T00:00:00.000000Z")
            # a foreign closure — must be excluded
            seed("orch_foreign", "spec/component/c", "orch_other",
                 "2026-03-01T00:00:00.000000Z")

            index = run_workflow._index_closure_orchestrations(repo_root, "orch_target")
            self.assertEqual(index, {
                "spec/component/c": "orch_c_new",
                "spec/component/b": "orch_b",
            })

    def _run_main_with_closure_spy(self, argv):
        """Run main() with _run_with_dependency_closure and _run_node replaced by
        spies. Returns (code, closure_kwargs_or_None, run_node_kwargs_or_None)."""
        closure_kwargs: dict = {}
        run_node_kwargs: dict = {}

        def spy_closure(**kw):
            closure_kwargs.update(kw)
            return 0

        def spy_run_node(**kw):
            run_node_kwargs.update(kw)
            return 0

        orig_closure = run_workflow._run_with_dependency_closure
        orig_run_node = run_workflow._run_node
        buf = io.StringIO()
        argv2 = list(argv)
        if "--stdout-format" not in argv2:
            argv2 += ["--stdout-format", "jsonl"]
        try:
            run_workflow._run_with_dependency_closure = spy_closure  # type: ignore[assignment]
            run_workflow._run_node = spy_run_node  # type: ignore[assignment]
            with redirect_stdout(buf):
                code = run_workflow.main(argv2)
        finally:
            run_workflow._run_with_dependency_closure = orig_closure  # type: ignore[assignment]
            run_workflow._run_node = orig_run_node  # type: ignore[assignment]
        return (
            code,
            closure_kwargs or None,
            run_node_kwargs or None,
        )

    def _seed_closure_target_specs(self, repo_root: Path) -> None:
        """Minimal on-disk target spec + deps.yaml so main()'s startup validation
        (canonicalize + discover dep ref) succeeds for the closure target."""
        _write_deps(repo_root, "spec/problem/a", "problem", "a",
                    components=[("c", ">=0.1.0 <1.0.0")])
        _write_deps(repo_root, "spec/component/c", "component", "c")

    def test_resume_enters_closure_driver_when_closure_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            self._seed_closure_target_specs(repo_root)
            # entry orch is a dependency node carrying the closure back-link
            self._seed_resumable_orchestration(
                repo_root, "orch_target", spec_ref="spec/component/c",
                until_phase="Validate", mode="dev", backend="claude",
                source_dependency_ref="spec/component/c/deps.yaml",
                invocation={
                    "closure_id": "orch_target",
                    "closure_target_spec_ref": "spec/problem/a",
                    "closure_until_phase": "Validate",
                },
            )
            code, closure_kwargs, run_node_kwargs = self._run_main_with_closure_spy(
                ["--resume", "--repo-root", str(repo_root), "--no-run-conductor"]
            )
            self.assertEqual(code, 0)
            self.assertIsNotNone(closure_kwargs, "should enter closure driver")
            self.assertIsNone(run_node_kwargs, "must not fall through to single _run_node")
            self.assertTrue(closure_kwargs["resume"])
            self.assertEqual(closure_kwargs["target_orchestration_id"], "orch_target")
            self.assertEqual(closure_kwargs["target_spec_ref"], "spec/problem/a")
            self.assertEqual(closure_kwargs["until_phase"], "Validate")
            self.assertEqual(
                closure_kwargs["prior_orch_by_spec"],
                {"spec/component/c": "orch_target"},
            )

    def test_resume_without_closure_uses_single_node(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            self._seed_resumable_orchestration(
                repo_root, "orch_legacy", spec_ref="spec/problem/test.md",
                until_phase="Validate", mode="dev", backend="claude",
            )
            code, closure_kwargs, run_node_kwargs = self._run_main_with_closure_spy(
                ["--resume", "--repo-root", str(repo_root), "--no-run-conductor"]
            )
            self.assertEqual(code, 0)
            self.assertIsNone(closure_kwargs, "legacy resume must not enter closure driver")
            self.assertIsNotNone(run_node_kwargs)
            self.assertTrue(run_node_kwargs["resume_mode"])

    def _find_init_invocation(self, observed_calls) -> dict | None:
        for args in observed_calls:
            if args and args[0] == "init" and "--invocation-json" in args:
                return json.loads(args[args.index("--invocation-json") + 1])
        return None

    def test_cold_single_node_init_carries_invocation_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            code, out, calls = self._run_main_with_fake_runtime(
                ["spec/problem/test.md", "validate", "--repo-root", str(repo_root),
                 "--no-run-conductor"]
            )
            self.assertEqual(code, 0)
            inv = self._find_init_invocation(calls)
            self.assertIsNotNone(inv, "cold init must carry --invocation-json")
            self.assertFalse(inv["with_deps"])
            self.assertNotIn("closure_id", inv)
            self.assertEqual(inv["spec_ref"], "spec/problem/test.md")

    def test_cold_with_deps_init_carries_closure_invocation(self) -> None:
        from tools.orchestration_runtime import _load_spec_catalog
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_default_llm_config_into(repo_root)
            _seed_shape_expr_schema_into(repo_root)
            (repo_root / "tools").mkdir(parents=True, exist_ok=True)
            (repo_root / "workspace").mkdir(parents=True, exist_ok=True)
            # Full diamond (catalog + deps) so the real closure resolver runs.
            DependencyClosureTests._seed_diamond(self, repo_root)
            _load_spec_catalog.cache_clear()
            code, out, calls = self._run_main_with_fake_runtime(
                ["spec/problem/a", "validate", "--with-deps",
                 "--repo-root", str(repo_root), "--no-run-conductor"]
            )
            # closure stops at the first dep (not ready after a no-op run), but its
            # cold init must already carry the closure back-link.
            inv = self._find_init_invocation(calls)
            self.assertIsNotNone(inv)
            self.assertTrue(inv["with_deps"])
            self.assertEqual(inv["closure_target_spec_ref"], "spec/problem/a")
            self.assertTrue(inv["closure_id"])  # = the target orchestration id

    def test_resume_closure_until_recovered_from_target_prompt(self) -> None:
        # After a phase-override resume, the target's own prompt end-phase is the
        # authoritative closure end-phase; a later plain resume entering via a dep
        # (whose copied closure_until_phase is stale "Compile") must recover the
        # refreshed "Validate" from the target, not revert to Compile.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            self._seed_closure_target_specs(repo_root)
            self._seed_resumable_orchestration(
                repo_root, "orch_dep_c", spec_ref="spec/component/c",
                until_phase="Validate", mode="dev", backend="claude",
                source_dependency_ref="spec/component/c/deps.yaml",
                invocation={"spec_ref": "spec/component/c", "closure_id": "ORCHT",
                            "closure_target_spec_ref": "spec/problem/a",
                            "closure_until_phase": "Compile"})
            # target ORCHT belongs to this closure; its prompt end-phase is Validate
            self._seed_resumable_orchestration(
                repo_root, "ORCHT", spec_ref="spec/problem/a",
                until_phase="Validate", mode="dev", backend="claude",
                source_dependency_ref="spec/problem/a/deps.yaml",
                invocation={"spec_ref": "spec/problem/a", "closure_id": "ORCHT",
                            "closure_target_spec_ref": "spec/problem/a",
                            "closure_until_phase": "Compile"})
            code, closure_kwargs, _ = self._run_main_with_closure_spy(
                ["--resume", "--orchestration-id", "orch_dep_c",
                 "--repo-root", str(repo_root), "--no-run-conductor"])
            self.assertEqual(code, 0)
            self.assertIsNotNone(closure_kwargs)
            self.assertEqual(closure_kwargs["until_phase"], "Validate")

    def test_resume_closure_until_ignores_unrelated_target(self) -> None:
        # If the reserved target id names an UNRELATED orchestration (its own
        # invocation.closure_id differs), its phase must NOT be trusted; keep the
        # entry node's recorded closure_until_phase.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            self._seed_closure_target_specs(repo_root)
            self._seed_resumable_orchestration(
                repo_root, "orch_dep_c", spec_ref="spec/component/c",
                until_phase="Validate", mode="dev", backend="claude",
                source_dependency_ref="spec/component/c/deps.yaml",
                invocation={"spec_ref": "spec/component/c", "closure_id": "ORCHT",
                            "closure_target_spec_ref": "spec/problem/a",
                            "closure_until_phase": "Compile"})
            # ORCHT prompt says Validate, but it belongs to a DIFFERENT closure
            self._seed_resumable_orchestration(
                repo_root, "ORCHT", spec_ref="spec/problem/a",
                until_phase="Validate", mode="dev", backend="claude",
                source_dependency_ref="spec/problem/a/deps.yaml",
                invocation={"spec_ref": "spec/problem/a", "closure_id": "OTHER"})
            code, closure_kwargs, _ = self._run_main_with_closure_spy(
                ["--resume", "--orchestration-id", "orch_dep_c",
                 "--repo-root", str(repo_root), "--no-run-conductor"])
            self.assertEqual(code, 0)
            self.assertIsNotNone(closure_kwargs)
            self.assertEqual(closure_kwargs["until_phase"], "Compile")

    def test_resume_partial_closure_block_falls_back_to_single_node(self) -> None:
        # A corrupt/partial invocation block (missing closure_until_phase) must NOT
        # drive the closure with a wrong until_phase; fall back to single-node resume.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            self._seed_resumable_orchestration(
                repo_root, "orch_partial", spec_ref="spec/problem/test.md",
                until_phase="Validate", mode="dev", backend="claude",
                invocation={
                    "closure_id": "orch_partial",
                    "closure_target_spec_ref": "spec/problem/a",
                    # closure_until_phase intentionally omitted
                },
            )
            code, closure_kwargs, run_node_kwargs = self._run_main_with_closure_spy(
                ["--resume", "--repo-root", str(repo_root), "--no-run-conductor"]
            )
            self.assertEqual(code, 0)
            self.assertIsNone(closure_kwargs, "partial closure block must not drive closure")
            self.assertIsNotNone(run_node_kwargs)

    def test_resume_explicit_spec_override_forces_single_node(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            self._seed_closure_target_specs(repo_root)
            self._seed_resumable_orchestration(
                repo_root, "orch_target", spec_ref="spec/component/c",
                until_phase="Validate", mode="dev", backend="claude",
                source_dependency_ref="spec/component/c/deps.yaml",
                invocation={
                    "closure_id": "orch_target",
                    "closure_target_spec_ref": "spec/problem/a",
                    "closure_until_phase": "Validate",
                },
            )
            # explicit spec positional (non-phase) → single-node escape hatch
            code, closure_kwargs, run_node_kwargs = self._run_main_with_closure_spy(
                ["spec/component/c", "--resume", "--orchestration-id", "orch_target",
                 "--repo-root", str(repo_root), "--no-run-conductor"]
            )
            self.assertEqual(code, 0)
            self.assertIsNone(closure_kwargs)
            self.assertIsNotNone(run_node_kwargs)

    def test_main_writes_prompt_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_default_llm_config_into(repo_root)
            _seed_shape_expr_schema_into(repo_root)
            (repo_root / "tools").mkdir(parents=True, exist_ok=True)
            (repo_root / "workspace").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem" / "test.md").write_text("spec\n", encoding="utf-8")
            dep_ref = "spec/problem/deps.yaml"
            (repo_root / dep_ref).write_text("nodes: []\n", encoding="utf-8")

            observed_calls: list[list[str]] = []

            def fake_runtime_command(root: Path, env: dict[str, str], args: list[str]) -> run_workflow.RuntimeResult:
                observed_calls.append(args)
                if args[0] == "init":
                    return run_workflow.RuntimeResult(
                        payload={"status": "ok", "orchestration_agent_run_id": "orch_agent_run_001"},
                        raw_stdout="{}",
                    )
                if args[0] == "preflight":
                    return run_workflow.RuntimeResult(
                        payload={
                            "status": "pass",
                            "can_launch_step_agents": True,
                            "can_launch_substep_agents": True,
                        },
                        raw_stdout="{}",
                    )
                return run_workflow.RuntimeResult(payload={"status": "ok"}, raw_stdout="{}")

            original_runtime = run_workflow._runtime_command
            try:
                run_workflow._runtime_command = fake_runtime_command  # type: ignore[assignment]
                code = run_workflow.main(
                    [
                        "spec/problem/test.md",
                        "build",
                        "--repo-root",
                        str(repo_root),
                        "--orchestration-id",
                        "orch_unit",
                        "--no-run-conductor",
                    ]
                )
            finally:
                run_workflow._runtime_command = original_runtime  # type: ignore[assignment]

            self.assertEqual(code, 0)
            self.assertTrue(any(call[0] == "init" for call in observed_calls))
            self.assertTrue(any(call[0] == "preflight" for call in observed_calls))
            prompt_path = (
                repo_root
                / "workspace"
                / "orchestrations"
                / "orch_unit"
                / "launches"
                / "orchestration.start.prompt.txt"
            )
            self.assertTrue(prompt_path.exists())
            prompt_text = prompt_path.read_text(encoding="utf-8")
            self.assertIn("orchestration_agent_run_id: `orch_agent_run_001`", prompt_text)














    def test_direct_script_invocation_does_not_crash_on_module_import(self) -> None:
        """Regression: `python3 tools/run_workflow.py ...` is the canonical
        entrypoint per CLAUDE.md. Under direct-script invocation `sys.path[0]`
        is `tools/`, NOT the repo root, so `from tools.X import Y` raises
        `ModuleNotFoundError` unless the script bootstraps `sys.path` first.
        Previously the new schema-load guard imported `tools.validate_pipeline_semantics`
        without that bootstrap, crashing direct-CLI invocation with a raw
        traceback instead of the intended structured failure. This test
        spawns the actual subprocess to exercise the real direct-script
        codepath that `run_workflow.main()` from in-process import would
        otherwise mask."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_default_llm_config_into(repo_root)
            # Build a minimal valid spec so we reach the schema guard.
            (repo_root / "spec" / "problem").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem" / "test.md").write_text("spec\n", encoding="utf-8")
            (repo_root / "spec" / "problem" / "deps.yaml").write_text("nodes: []\n", encoding="utf-8")
            # Deliberately omit the canonical schema so the guard fires.
            run_workflow_path = (
                Path(__file__).resolve().parent.parent / "run_workflow.py"
            )
            # Strip PYTHONPATH so the subprocess only has its own bootstrap.
            env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
            proc = subprocess.run(
                [
                    sys.executable,
                    str(run_workflow_path),
                    "spec/problem/test.md",
                    "build",
                    "--repo-root", str(repo_root),
                    "--orchestration-id", "orch_direct_cli",
                    "--no-run-conductor",
                    # This test parses stdout as JSON; the default (`human`) now
                    # renders startup envelopes too, so ask for the machine form.
                    # Unrelated to what it pins (import-crash safety).
                    "--stdout-format", "jsonl",
                ],
                cwd=str(repo_root),
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
        # Must NOT crash with a Python traceback (ModuleNotFoundError or otherwise).
        self.assertNotIn(
            "Traceback",
            proc.stderr,
            f"direct-CLI invocation must not produce a traceback; stderr:\n{proc.stderr}",
        )
        self.assertEqual(
            proc.returncode, 2,
            f"expected exit 2 (structured fail); stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}",
        )
        # Must emit structured JSON identifying the schema gap.
        last_line = proc.stdout.strip().splitlines()[-1]
        payload = json.loads(last_line)
        self.assertEqual(payload["status"], "fail")
        self.assertEqual(payload["reason"], "missing_canonical_schema")
        self.assertIn("shape_expr.schema.json", payload["missing_path"])

    def test_main_fails_fast_when_canonical_schema_missing(self) -> None:
        """Regression: tools/run_workflow.py must abort BEFORE init/preflight
        if `<repo_root>/spec/schema/ir/shape_expr.schema.json` is missing,
        because validate_pipeline_semantics is now fail-closed under repo
        scope and would otherwise collapse every downstream phase gate with
        `schema_load_failed` after orchestration state has already been
        mutated. Emits structured `missing_canonical_schema` JSON on stdout."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_default_llm_config_into(repo_root)
            # Deliberately do NOT seed the schema (this test exercises absence).
            (repo_root / "spec" / "problem").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem" / "test.md").write_text("spec\n", encoding="utf-8")
            (repo_root / "spec" / "problem" / "deps.yaml").write_text("nodes: []\n", encoding="utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = run_workflow.main(
                    [
                        "spec/problem/test.md",
                        "build",
                        "--repo-root", str(repo_root),
                        "--orchestration-id", "orch_no_schema",
                        "--no-run-conductor",
                        "--stdout-format", "jsonl",
                    ]
                )
            output = buf.getvalue()
        self.assertEqual(code, 2)
        # Verify structured JSON output with the right reason code.
        payload = json.loads(output.strip().splitlines()[-1])
        self.assertEqual(payload["status"], "fail")
        self.assertEqual(payload["reason"], "missing_canonical_schema")
        self.assertIn("spec/schema/ir/shape_expr.schema.json", payload["missing_path"])
        # Critical: orchestration state must NOT have been created — the
        # check must run before init().
        self.assertFalse(
            (repo_root / "workspace" / "orchestrations").exists(),
            "init/preflight must not run before the schema-existence check",
        )

    def test_main_fails_fast_when_canonical_schema_is_malformed(self) -> None:
        """Regression: the startup guard must surface NOT only missing-file
        but also malformed JSON, invalid regex, and structural-classifier
        failures BEFORE any orchestration state mutation. Previously the
        guard only did `is_file()`, so a corrupted schema slipped through and
        crashed mid-phase after `workspace/tmp/<arid>/` was already created."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_default_llm_config_into(repo_root)
            schema_dir = repo_root / "spec" / "schema" / "ir"
            schema_dir.mkdir(parents=True)
            # Schema EXISTS as a file but has malformed JSON.
            (schema_dir / "shape_expr.schema.json").write_text(
                "{ this is not json", encoding="utf-8"
            )
            (repo_root / "spec" / "problem").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem" / "test.md").write_text("spec\n", encoding="utf-8")
            (repo_root / "spec" / "problem" / "deps.yaml").write_text("nodes: []\n", encoding="utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = run_workflow.main(
                    [
                        "spec/problem/test.md",
                        "build",
                        "--repo-root", str(repo_root),
                        "--orchestration-id", "orch_corrupt_schema",
                        "--no-run-conductor",
                        "--stdout-format", "jsonl",
                    ]
                )
            output = buf.getvalue()
        self.assertEqual(code, 2)
        payload = json.loads(output.strip().splitlines()[-1])
        self.assertEqual(payload["status"], "fail")
        self.assertEqual(payload["reason"], "missing_canonical_schema")
        # Detail must surface the underlying parse error so operators can fix
        # the schema rather than just learning "something is wrong".
        self.assertIn("malformed JSON", payload["detail"])
        # Critical: NO orchestration state was touched.
        self.assertFalse(
            (repo_root / "workspace" / "orchestrations").exists(),
            "init/preflight must not run before the schema-load check",
        )
        self.assertFalse(
            (repo_root / "workspace" / "tmp").exists(),
            "workspace/tmp must not be created before the schema-load check",
        )

    def test_main_fails_when_spec_ref_does_not_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            # Seeded so the run reaches the spec check: without a default configuration it
            # would stop earlier, at `llm_config_default_missing`, and this test would pass
            # for a reason that has nothing to do with the spec.
            _seed_default_llm_config_into(repo_root)
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = run_workflow.main(
                    [
                        "spec/problem/missing.md",
                        "build",
                        "--repo-root",
                        str(repo_root),
                        "--orchestration-id",
                        "orch_missing",
                        "--no-run-conductor",
                        # The envelope is read as JSON below; the default `human`
                        # format now renders startup envelopes as `[FAIL] ...`.
                        "--stdout-format",
                        "jsonl",
                    ]
                )
            self.assertEqual(code, 2)
            # `code == 2` alone is satisfied by ANY startup refusal — including the one the
            # seeding above exists to avoid — so the envelope is read. What that pins is the
            # BEHAVIOUR (a nonexistent spec_ref is refused, and the refusal names it), not
            # which line raised: on this cold path `_canonicalize_spec_ref` and
            # `_discover_source_dependency_ref` both run `_resolve_existing_ref_path` on the
            # ref under the same `field_name`, so deleting either alone leaves the envelope
            # byte-identical. That redundancy is why a single-site mutation here proves
            # nothing — and it is cold-path only: a resume that reuses its recovered
            # dependency ref skips the second check entirely.
            payload = json.loads(buf.getvalue().strip().splitlines()[-1])
            self.assertEqual(payload["reason"], "invalid_startup_input")
            self.assertIn("spec/problem/missing.md", payload["detail"])
            self.assertNotIn("llm_config", payload["detail"])

    def test_main_returns_structured_error_when_init_runtime_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_default_llm_config_into(repo_root)
            _seed_shape_expr_schema_into(repo_root)
            (repo_root / "tools").mkdir(parents=True, exist_ok=True)
            (repo_root / "workspace").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem" / "test.md").write_text("spec\n", encoding="utf-8")
            dep_ref = "spec/problem/deps.yaml"
            (repo_root / dep_ref).write_text("nodes: []\n", encoding="utf-8")

            def fake_runtime_command(root: Path, env: dict[str, str], args: list[str]) -> run_workflow.RuntimeResult:
                if args[0] == "init":
                    raise RuntimeError("runtime command failed (init): boom")
                return run_workflow.RuntimeResult(payload={"status": "ok"}, raw_stdout="{}")

            original_runtime = run_workflow._runtime_command
            try:
                run_workflow._runtime_command = fake_runtime_command  # type: ignore[assignment]
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    code = run_workflow.main(
                        [
                            "spec/problem/test.md",
                            "build",
                            "--repo-root",
                            str(repo_root),
                            "--orchestration-id",
                            "orch_init_fail",
                            "--no-run-conductor",
                            "--stdout-format",
                            "jsonl",
                        ]
                    )
            finally:
                run_workflow._runtime_command = original_runtime  # type: ignore[assignment]

            self.assertEqual(code, 2)
            payload = json.loads(stdout.getvalue().strip().splitlines()[-1])
            self.assertEqual(payload["status"], "fail")
            self.assertEqual(payload["reason"], "runtime_command_failed")
            self.assertEqual(payload["orchestration_id"], "orch_init_fail")
            self.assertIn("init", payload["detail"])

    def test_main_returns_structured_error_when_preflight_runtime_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_default_llm_config_into(repo_root)
            _seed_shape_expr_schema_into(repo_root)
            (repo_root / "tools").mkdir(parents=True, exist_ok=True)
            (repo_root / "workspace").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem" / "test.md").write_text("spec\n", encoding="utf-8")
            dep_ref = "spec/problem/deps.yaml"
            (repo_root / dep_ref).write_text("nodes: []\n", encoding="utf-8")

            def fake_runtime_command(root: Path, env: dict[str, str], args: list[str]) -> run_workflow.RuntimeResult:
                if args[0] == "preflight":
                    raise RuntimeError("runtime command failed (preflight): boom")
                if args[0] == "init":
                    return run_workflow.RuntimeResult(
                        payload={"status": "ok", "orchestration_agent_run_id": "orch_agent_run_preflight_fail"},
                        raw_stdout="{}",
                    )
                return run_workflow.RuntimeResult(payload={"status": "ok"}, raw_stdout="{}")

            original_runtime = run_workflow._runtime_command
            try:
                run_workflow._runtime_command = fake_runtime_command  # type: ignore[assignment]
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    code = run_workflow.main(
                        [
                            "spec/problem/test.md",
                            "build",
                            "--repo-root",
                            str(repo_root),
                            "--orchestration-id",
                            "orch_preflight_fail",
                            "--no-run-conductor",
                            "--stdout-format",
                            "jsonl",
                        ]
                    )
            finally:
                run_workflow._runtime_command = original_runtime  # type: ignore[assignment]

            self.assertEqual(code, 2)
            payload = json.loads(stdout.getvalue().strip().splitlines()[-1])
            self.assertEqual(payload["status"], "fail")
            self.assertEqual(payload["reason"], "runtime_command_failed")
            self.assertEqual(payload["orchestration_id"], "orch_preflight_fail")
            self.assertIn("preflight", payload["detail"])

    def test_main_returns_structured_error_when_init_result_lacks_orchestration_agent_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_default_llm_config_into(repo_root)
            _seed_shape_expr_schema_into(repo_root)
            (repo_root / "tools").mkdir(parents=True, exist_ok=True)
            (repo_root / "workspace").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem" / "test.md").write_text("spec\n", encoding="utf-8")
            dep_ref = "spec/problem/deps.yaml"
            (repo_root / dep_ref).write_text("nodes: []\n", encoding="utf-8")

            def fake_runtime_command(root: Path, env: dict[str, str], args: list[str]) -> run_workflow.RuntimeResult:
                if args[0] == "init":
                    return run_workflow.RuntimeResult(payload={"status": "ok"}, raw_stdout="{}")
                return run_workflow.RuntimeResult(payload={"status": "ok"}, raw_stdout="{}")

            original_runtime = run_workflow._runtime_command
            try:
                run_workflow._runtime_command = fake_runtime_command  # type: ignore[assignment]
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    code = run_workflow.main(
                        [
                            "spec/problem/test.md",
                            "build",
                            "--repo-root",
                            str(repo_root),
                            "--orchestration-id",
                            "orch_init_missing_run_id",
                            "--no-run-conductor",
                            "--stdout-format",
                            "jsonl",
                        ]
                    )
            finally:
                run_workflow._runtime_command = original_runtime  # type: ignore[assignment]

            self.assertEqual(code, 2)
            payload = json.loads(stdout.getvalue().strip().splitlines()[-1])
            self.assertEqual(payload["status"], "fail")
            self.assertEqual(payload["reason"], "runtime_command_failed")
            self.assertEqual(payload["orchestration_id"], "orch_init_missing_run_id")
            self.assertIn("missing orchestration_agent_run_id", payload["detail"])


    def test_main_resolves_dependency_ref_from_spec_deps_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_default_llm_config_into(repo_root)
            _seed_shape_expr_schema_into(repo_root)
            (repo_root / "tools").mkdir(parents=True, exist_ok=True)
            (repo_root / "workspace").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem" / "test.md").write_text("spec\n", encoding="utf-8")
            dep_ref = "spec/problem/deps.yaml"
            (repo_root / dep_ref).write_text("nodes: []\n", encoding="utf-8")

            observed_calls: list[list[str]] = []

            def fake_runtime_command(root: Path, env: dict[str, str], args: list[str]) -> run_workflow.RuntimeResult:
                observed_calls.append(args)
                if args[0] == "preflight":
                    return run_workflow.RuntimeResult(
                        payload={
                            "status": "pass",
                            "can_launch_step_agents": True,
                            "can_launch_substep_agents": True,
                        },
                        raw_stdout="{}",
                    )
                if args[0] == "init":
                    return run_workflow.RuntimeResult(
                        payload={"status": "ok", "orchestration_agent_run_id": "orch_agent_run_auto_dep"},
                        raw_stdout="{}",
                    )
                return run_workflow.RuntimeResult(payload={"status": "ok"}, raw_stdout="{}")

            original_runtime = run_workflow._runtime_command
            try:
                run_workflow._runtime_command = fake_runtime_command  # type: ignore[assignment]
                code = run_workflow.main(
                    [
                        "spec/problem/test.md",
                        "build",
                        "--repo-root",
                        str(repo_root),
                        "--orchestration-id",
                        "orch_auto_dep",
                        "--no-run-conductor",
                    ]
                )
            finally:
                run_workflow._runtime_command = original_runtime  # type: ignore[assignment]

            self.assertEqual(code, 0)
            init_call = next(call for call in observed_calls if call[0] == "init")
            self.assertIn("--source-dependency-ref", init_call)
            dep_idx = init_call.index("--source-dependency-ref") + 1
            self.assertEqual(init_call[dep_idx], dep_ref)

    def test_main_fails_when_dependency_ref_cannot_be_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            # As above: the dependency check runs AFTER configuration resolution, so without
            # this the assertions below are satisfied by `llm_config_default_missing` and the
            # check they name could be deleted outright with the test still green.
            _seed_default_llm_config_into(repo_root)
            (repo_root / "tools").mkdir(parents=True, exist_ok=True)
            (repo_root / "workspace").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem" / "test.md").write_text("spec\n", encoding="utf-8")

            observed_calls: list[list[str]] = []

            def fake_runtime_command(root: Path, env: dict[str, str], args: list[str]) -> run_workflow.RuntimeResult:
                observed_calls.append(args)
                if args[0] == "preflight":
                    return run_workflow.RuntimeResult(
                        payload={
                            "status": "pass",
                            "can_launch_step_agents": True,
                            "can_launch_substep_agents": True,
                        },
                        raw_stdout="{}",
                    )
                if args[0] == "init":
                    return run_workflow.RuntimeResult(
                        payload={"status": "ok", "orchestration_agent_run_id": "orch_agent_run_no_dep"},
                        raw_stdout="{}",
                    )
                return run_workflow.RuntimeResult(payload={"status": "ok"}, raw_stdout="{}")

            original_runtime = run_workflow._runtime_command
            try:
                run_workflow._runtime_command = fake_runtime_command  # type: ignore[assignment]
                code = run_workflow.main(
                    [
                        "spec/problem/test.md",
                        "build",
                        "--repo-root",
                        str(repo_root),
                        "--orchestration-id",
                        "orch_no_dep",
                        "--no-run-conductor",
                    ]
                )
            finally:
                run_workflow._runtime_command = original_runtime  # type: ignore[assignment]

            self.assertEqual(code, 2)
            self.assertFalse(observed_calls)

    def test_main_fails_fast_when_a_required_python_package_is_missing(self) -> None:
        """The same guarantee as the CLI-tool check below, for the packages the runtime imports
        MID-RUN. Without this the run does not fail at launch: it fails at the first `Generate`
        node's gate, after lint and syntax have passed, in a billed run, and terminalizes —
        because an absent Fortran structure front end is fail-closed by design. The failure is
        correct and the timing is not. Nothing in this repository installs these packages, so a
        machine that satisfies the RUNBOOK today does not have them (Codex review).

        The message must carry the DISTRIBUTION names, since `tree_sitter_fortran` is not what an
        operator types into pip."""
        import importlib.util as importlib_util

        real_find_spec = importlib_util.find_spec

        def fake_find_spec(name, *args, **kwargs):  # type: ignore[no-untyped-def]
            if name == "tree_sitter_fortran":
                return None
            return real_find_spec(name, *args, **kwargs)

        observed_calls: list[list[str]] = []

        def fake_runtime(repo_root, env, args):  # type: ignore[no-untyped-def]
            observed_calls.append(list(args))
            raise AssertionError("orchestration_runtime must not be invoked")

        original_runtime = run_workflow._runtime_command
        importlib_util.find_spec = fake_find_spec  # type: ignore[assignment]
        run_workflow._runtime_command = fake_runtime  # type: ignore[assignment]
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = run_workflow.main([
                    "spec/problem/dummy.md",
                    "Compile",
                    "--stdout-format",
                    "jsonl",
                ])
        finally:
            importlib_util.find_spec = real_find_spec  # type: ignore[assignment]
            run_workflow._runtime_command = original_runtime  # type: ignore[assignment]

        self.assertEqual(code, 2)
        self.assertFalse(observed_calls)
        payload = json.loads(buf.getvalue().strip())
        self.assertEqual(payload.get("status"), "fail")
        self.assertEqual(payload.get("reason"), "missing_required_python_modules")
        self.assertEqual(payload.get("missing"), ["tree-sitter-fortran"])
        self.assertIn("tree-sitter", payload.get("required", []))
        self.assertIn("pip install tree-sitter-fortran", payload.get("detail", ""))

    def test_main_fails_fast_when_a_required_host_tool_is_missing(self) -> None:
        """The third family, and the one issue #109 reproduced: the executables the run's own
        axis selection implies. Without this the run does not fail at launch — an absent
        `static lint` tool fails the first `Generate` node's gate, after `Compile` and
        `Generate.generate` have been BILLED, and `--resume` re-runs the whole `generate` phase.
        An absent compiler lands in the same place one check later, and an absent build system
        one phase later still, at `Build`.

        The tool name is asserted through the probe rather than spelled here: this test must not
        become the place a `neutral core` file learns a technology name either."""
        from tools.host_prerequisites import required_host_executables

        target = required_host_executables()[0].executable
        original_which = run_workflow.shutil.which

        def fake_which(name: str) -> str | None:
            if name == target:
                return None
            return original_which(name)

        observed_calls: list[list[str]] = []

        def fake_runtime(repo_root, env, args):  # type: ignore[no-untyped-def]
            observed_calls.append(list(args))
            raise AssertionError("orchestration_runtime must not be invoked")

        original_runtime = run_workflow._runtime_command
        run_workflow.shutil.which = fake_which  # type: ignore[assignment]
        run_workflow._runtime_command = fake_runtime  # type: ignore[assignment]
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = run_workflow.main([
                    "spec/problem/dummy.md",
                    "Compile",
                    "--stdout-format",
                    "jsonl",
                ])
        finally:
            run_workflow.shutil.which = original_which  # type: ignore[assignment]
            run_workflow._runtime_command = original_runtime  # type: ignore[assignment]

        self.assertEqual(code, 2)
        self.assertFalse(observed_calls)
        payload = json.loads(buf.getvalue().strip())
        self.assertEqual(payload.get("status"), "fail")
        self.assertEqual(payload.get("reason"), "missing_required_host_tools")
        self.assertEqual(payload.get("missing"), [target])
        self.assertIn(target, payload.get("required", []))
        self.assertIn(target, payload.get("detail", ""))
        self.assertEqual(payload.get("docs_ref"), "docs/RUNBOOK.md#0-1")

    def test_the_host_tool_rejection_enumerates_every_missing_tool(self) -> None:
        """Same format contract the CLI-tool rejection has: comma-separated, no spaces, so a
        separator change cannot drift away from what an operator is told to install."""
        from tools.host_prerequisites import required_host_executables

        targets = [item.executable for item in required_host_executables()]
        self.assertGreater(len(targets), 1)
        original_which = run_workflow.shutil.which
        run_workflow.shutil.which = lambda name: (  # type: ignore[assignment]
            None if name in targets else original_which(name)
        )
        original_runtime = run_workflow._runtime_command
        run_workflow._runtime_command = lambda *a, **k: (_ for _ in ()).throw(  # type: ignore[assignment]
            AssertionError("orchestration_runtime must not be invoked"))
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = run_workflow.main([
                    "spec/problem/dummy.md", "Compile", "--stdout-format", "jsonl"])
        finally:
            run_workflow.shutil.which = original_which  # type: ignore[assignment]
            run_workflow._runtime_command = original_runtime  # type: ignore[assignment]

        self.assertEqual(code, 2)
        payload = json.loads(buf.getvalue().strip())
        self.assertEqual(payload.get("missing"), targets)
        self.assertIn(f"missing host tools: {','.join(targets)}", payload.get("detail", ""))

    def test_check_required_host_tools_returns_empty_when_all_present(self) -> None:
        """Sanity check of the same kind as the CLI-tool one below: if this fails, the machine
        running the suite could not run a workflow."""
        self.assertEqual(run_workflow._check_required_host_tools(), [])

    def test_main_fails_fast_when_a_required_host_tool_is_of_an_unmeasured_version(self) -> None:
        """The VERSION half of the same family (issue #111).

        The tool is present, so the arm above says nothing; what it is, is a build this
        repository has not measured its gate's rule set against. Left to the gate, that surfaces
        at the first `Generate.gate` and burns the whole `Generate` retry budget on findings no
        leaf can act on.

        The refusal is driven through the probe rather than by installing a second build: the
        version verdict is the backend's, and substituting it here is what keeps this
        `neutral core` test from learning a tool name or a version number.
        """
        from tools import host_prerequisites

        refusal = host_prerequisites.HostToolVersion(
            "linter", "probe_backend", "probe_tool", "probe_tool 0.0.1",
            "probe_tool 0.0.1 is outside the measured range")
        original_arm = host_prerequisites.unsupported_host_tool_versions
        original_runtime = run_workflow._runtime_command
        host_prerequisites.unsupported_host_tool_versions = (  # type: ignore[assignment]
            lambda selection=None: (refusal,))
        run_workflow._runtime_command = lambda *a, **k: (_ for _ in ()).throw(  # type: ignore[assignment]
            AssertionError("orchestration_runtime must not be invoked"))
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = run_workflow.main([
                    "spec/problem/dummy.md", "Compile", "--stdout-format", "jsonl"])
        finally:
            host_prerequisites.unsupported_host_tool_versions = original_arm  # type: ignore[assignment]
            run_workflow._runtime_command = original_runtime  # type: ignore[assignment]

        self.assertEqual(code, 2)
        payload = json.loads(buf.getvalue().strip())
        self.assertEqual(payload.get("status"), "fail")
        self.assertEqual(payload.get("reason"), "unsupported_required_host_tool_versions")
        self.assertIn(refusal.reason, payload.get("detail", ""))
        self.assertEqual(payload.get("unsupported"), [{
            "executable": "probe_tool",
            "axis": "linter",
            "backend_id": "probe_backend",
            "version": "probe_tool 0.0.1",
            "reason": refusal.reason,
        }])
        self.assertEqual(payload.get("docs_ref"), "docs/RUNBOOK.md#0-1")

    def test_the_version_arm_is_checked_after_the_presence_arm(self) -> None:
        """Ordered by reachability: an absent program has no version to read.

        A machine with nothing installed must be told to install the tools, not handed a version
        clause about a program it does not have. Driven by making BOTH arms refuse and asserting
        which message comes out.
        """
        from tools import host_prerequisites
        from tools.host_prerequisites import required_host_executables

        target = required_host_executables()[0].executable
        original_which = run_workflow.shutil.which
        original_arm = host_prerequisites.unsupported_host_tool_versions
        original_runtime = run_workflow._runtime_command
        run_workflow.shutil.which = lambda name: (  # type: ignore[assignment]
            None if name == target else original_which(name))
        host_prerequisites.unsupported_host_tool_versions = (  # type: ignore[assignment]
            lambda selection=None: (host_prerequisites.HostToolVersion(
                "linter", "probe_backend", target, None, "must not be reported"),))
        run_workflow._runtime_command = lambda *a, **k: (_ for _ in ()).throw(  # type: ignore[assignment]
            AssertionError("orchestration_runtime must not be invoked"))
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = run_workflow.main([
                    "spec/problem/dummy.md", "Compile", "--stdout-format", "jsonl"])
        finally:
            run_workflow.shutil.which = original_which  # type: ignore[assignment]
            host_prerequisites.unsupported_host_tool_versions = original_arm  # type: ignore[assignment]
            run_workflow._runtime_command = original_runtime  # type: ignore[assignment]

        self.assertEqual(code, 2)
        payload = json.loads(buf.getvalue().strip())
        self.assertEqual(payload.get("reason"), "missing_required_host_tools")

    def test_check_host_tool_versions_returns_empty_when_the_host_is_supported(self) -> None:
        """The companion sanity row: a workflow started on this machine is not refused here."""
        self.assertEqual(run_workflow._check_host_tool_versions(), [])

    def test_main_fails_fast_when_required_cli_tool_missing(self) -> None:
        """If jq (or any REQUIRED_CLI_TOOLS entry) is not on PATH, main() must
        return 2 with status=fail/reason=missing_required_cli_tools BEFORE
        running any orchestration_runtime command. This protects against
        partial-failure states where downstream procedures (e.g. TMPDIR
        extraction via jq) would otherwise be prescribed despite the tool
        being absent."""
        original_which = run_workflow.shutil.which

        def fake_which(name: str) -> str | None:
            if name == "jq":
                return None
            return original_which(name)

        observed_calls: list[list[str]] = []

        def fake_runtime(repo_root, env, args):  # type: ignore[no-untyped-def]
            observed_calls.append(list(args))
            raise AssertionError("orchestration_runtime must not be invoked")

        original_runtime = run_workflow._runtime_command
        run_workflow.shutil.which = fake_which  # type: ignore[assignment]
        run_workflow._runtime_command = fake_runtime  # type: ignore[assignment]
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = run_workflow.main([
                    "spec/problem/dummy.md",
                    "Compile",
                    "--stdout-format",
                    "jsonl",
                ])
        finally:
            run_workflow.shutil.which = original_which  # type: ignore[assignment]
            run_workflow._runtime_command = original_runtime  # type: ignore[assignment]

        self.assertEqual(code, 2)
        self.assertFalse(observed_calls)
        payload = json.loads(buf.getvalue().strip())
        self.assertEqual(payload.get("status"), "fail")
        self.assertEqual(payload.get("reason"), "missing_required_cli_tools")
        self.assertEqual(payload.get("missing"), ["jq"])
        self.assertIn("python3", payload.get("required", []))
        self.assertEqual(payload.get("detail"), "missing tools: jq")

    def test_check_required_cli_tools_returns_empty_when_all_present(self) -> None:
        """Sanity check: in the test environment all required tools are
        present, so the helper returns []. If this fails, the test environment
        is missing a tool needed for workflow runs."""
        self.assertEqual(run_workflow._check_required_cli_tools(), [])

    def test_main_reports_multiple_missing_tools_in_detail(self) -> None:
        """When multiple required tools are missing, `detail` must enumerate
        all of them as a comma-separated list (no spaces). This pins the
        format so future separator changes don't silently drift away from the
        documented shape in docs/RUNBOOK.md#0-1."""
        original_which = run_workflow.shutil.which

        def fake_which(name: str) -> str | None:
            if name in {"jq", "git"}:
                return None
            return original_which(name)

        original_runtime = run_workflow._runtime_command

        def fake_runtime(repo_root, env, args):  # type: ignore[no-untyped-def]
            raise AssertionError("orchestration_runtime must not be invoked")

        run_workflow.shutil.which = fake_which  # type: ignore[assignment]
        run_workflow._runtime_command = fake_runtime  # type: ignore[assignment]
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = run_workflow.main([
                    "spec/problem/dummy.md",
                    "Compile",
                    "--stdout-format",
                    "jsonl",
                ])
        finally:
            run_workflow.shutil.which = original_which  # type: ignore[assignment]
            run_workflow._runtime_command = original_runtime  # type: ignore[assignment]

        self.assertEqual(code, 2)
        payload = json.loads(buf.getvalue().strip())
        self.assertEqual(payload.get("missing"), ["jq", "git"])
        self.assertEqual(payload.get("detail"), "missing tools: jq,git")


# Every non-infrastructure spec must declare exactly one `infrastructure` (runner-harness)
# direct dependency (spec_input_gates.infra_dep_count_violation), so a fixture registry is only
# usable if the harness node itself is registered and readable. `_write_catalog` seeds it and
# `_write_deps` declares the edge by default, matching the shipped spec corpus — which means
# every closure below legitimately contains the harness node.
_HARNESS_ID = "harness_fortran_cpu"
_HARNESS_REF = "spec/infrastructure/harness_fortran_cpu"


def _write_catalog(repo_root: Path, entries: list[dict]) -> None:
    """Write a minimal spec_catalog.yaml from a list of {spec_kind, spec_id,
    spec_version, deps_path} dicts.

    Unless the caller registers an `infrastructure` spec itself, the runner harness is
    appended (and its deps.yaml written), so the default `_write_deps` harness edge
    resolves."""
    if not any(e.get("spec_kind") == "infrastructure" for e in entries):
        entries = list(entries) + [
            {"spec_kind": "infrastructure", "spec_id": _HARNESS_ID,
             "spec_version": "0.7.0", "deps_path": f"{_HARNESS_REF}/deps.yaml"},
        ]
        _write_deps(repo_root, _HARNESS_REF, "infrastructure", _HARNESS_ID)
    lines = ["catalog_version: 0.2.0", "updated_at: 2026-06-18", "specs:"]
    for e in entries:
        lines.append(f"  - spec_kind: {e['spec_kind']}")
        lines.append(f"    spec_id: {e['spec_id']}")
        lines.append(f"    spec_version: \"{e['spec_version']}\"")
        lines.append(f"    deps_path: {e['deps_path']}")
    (repo_root / "spec" / "registry").mkdir(parents=True, exist_ok=True)
    (repo_root / "spec" / "registry" / "spec_catalog.yaml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def _write_deps(repo_root: Path, spec_ref: str, spec_kind: str, spec_id: str,
                components: list[tuple[str, str]] | None = None,
                profiles: list[tuple[str, str]] | None = None,
                infrastructure: list[tuple[str, str]] | None = None) -> None:
    """Write a deps.yaml under <spec_ref>/. components/profiles/infrastructure are
    (id, version_constraint) tuples.

    `infrastructure` defaults to the single harness edge every non-infrastructure spec is
    required to declare (an `infrastructure` spec defaults to none). Pass an explicit list
    — including `[]` — to exercise the count gate."""
    if infrastructure is None:
        infrastructure = ([] if spec_kind == "infrastructure"
                          else [(_HARNESS_ID, ">=0.1.0")])
    d = repo_root / spec_ref
    d.mkdir(parents=True, exist_ok=True)
    lines = [f"spec_id: {spec_id}", f"spec_kind: {spec_kind}", "dependencies:"]
    lines.append("  components:")
    for cid, c in (components or []):
        lines.append(f"    - component_id: {cid}")
        lines.append(f"      version_constraint: \"{c}\"")
    if not components:
        lines[-1] = "  components: []"
    lines.append("  profiles:")
    for pid, c in (profiles or []):
        lines.append(f"    - profile_id: {pid}")
        lines.append(f"      version_constraint: \"{c}\"")
    if not profiles:
        lines[-1] = "  profiles: []"
    if infrastructure:
        lines.append("  infrastructure:")
        for iid, c in infrastructure:
            lines.append(f"    - infrastructure_id: {iid}")
            lines.append(f"      version_constraint: \"{c}\"")
    (d / "deps.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


class DependencyClosureTests(unittest.TestCase):
    def _seed_diamond(self, repo_root: Path) -> None:
        # problem A → component B + harness C ; B → harness C ; C leaf.
        # C plays the runner-harness role every non-infrastructure spec must declare
        # exactly once, so the diamond needs no extra node to satisfy that rule.
        _write_catalog(repo_root, [
            {"spec_kind": "problem", "spec_id": "a", "spec_version": "0.3.0",
             "deps_path": "spec/problem/a/deps.yaml"},
            {"spec_kind": "component", "spec_id": "b", "spec_version": "0.1.0",
             "deps_path": "spec/component/b/deps.yaml"},
            {"spec_kind": "infrastructure", "spec_id": "c", "spec_version": "0.1.0",
             "deps_path": "spec/component/c/deps.yaml"},
        ])
        _write_deps(repo_root, "spec/problem/a", "problem", "a",
                    components=[("b", ">=0.1.0 <1.0.0")],
                    infrastructure=[("c", ">=0.1.0 <1.0.0")])
        _write_deps(repo_root, "spec/component/b", "component", "b",
                    infrastructure=[("c", ">=0.1.0 <1.0.0")])
        _write_deps(repo_root, "spec/component/c", "infrastructure", "c")

    def _seed_prior_member(self, repo_root: Path, orch_id: str, spec_ref: str,
                           *, executor: str | None = "pure",
                           status: str | None = "fail",
                           pin: bool = True,
                           driver: dict | None = None) -> None:
        """Seed a minimal member orchestration_meta.json for a warm-resumed closure node.

        Production always has this on disk (the id came from `_index_closure_orchestrations`
        scanning existing metas). `executor` defaults to `pure` (post-M-F reality); pass a
        non-pure value / None to exercise the per-member fail-close gate. `status`
        defaults to a terminal `fail` — the normal reason a closure member is warm-resumed
        — because `init_orchestration` ALWAYS writes a status, so a status-less meta is a
        shape production never produces and would silently route every liveness check
        down its non-terminal branch.

        The leaf-LLM pin is likewise post-issue-#28 reality: it names the SAME configuration
        the closure is being driven with (`_drive_closure_raw`'s claude sample), so the
        per-member gate passes. `pin=False` seeds the unpinned pre-#28 shape, which the gate
        refuses under `llm_config_legacy_flags_removed`."""
        meta_path = (repo_root / "workspace" / "orchestrations" / orch_id
                     / "orchestration_meta.json")
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        invocation: dict = {"closure_id": "orch_target"}
        if executor is not None:
            invocation["generate_executor"] = executor
        if pin:
            cfg = _sample_config("claude")
            invocation["llm_config_path"] = _recorded_sample_pin(repo_root)
            invocation["llm_config_sha256"] = cfg.sha256
        meta: dict = {"spec_ref": spec_ref, "invocation": invocation}
        if status is not None:
            meta["status"] = status
        if driver is not None:
            meta["driver"] = driver
        meta_path.write_text(json.dumps(meta), encoding="utf-8")


    def _pin_member(self, repo_root: Path, orch_id: str, spec_ref: str,
                    config_rel: str, sha: str) -> None:
        """A closure member that recorded a leaf-LLM configuration pin."""
        meta_path = (repo_root / "workspace" / "orchestrations" / orch_id
                     / "orchestration_meta.json")
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(json.dumps({
            "spec_ref": spec_ref, "status": "fail",
            "invocation": {"closure_id": "orch_target", "generate_executor": "pure",
                           "llm_config_path": config_rel, "llm_config_sha256": sha,
                           "llm_config_overrides": {}},
        }), encoding="utf-8")

    def _closure_config(self, repo_root: Path, model: str | None = "opus"):
        import tools.llm_config as _lc
        path = repo_root / "closure.yaml"
        body = "defaults:\n  provider: claude_cli\n"
        if model:
            body += f"  model: {model}\n"
        path.write_text(body, encoding="utf-8")
        return path, _lc.load_llm_config(path)

    def test_closure_member_resume_is_refused_when_its_config_changed(self) -> None:
        """The entry gate never sees a member: a closure resume warm-resumes members the entry
        orchestration's gate never looked at. Mutating both closure gates to `continue` past
        every rejection left the whole suite green."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            self._seed_diamond(repo_root)
            path, cfg = self._closure_config(repo_root)
            self._pin_member(repo_root, "orch_c_prev", "spec/component/c",
                             "closure.yaml", cfg.sha256)
            # The file moves on after the member launched.
            path.write_text("defaults:\n  provider: claude_cli\n  model: changed\n",
                            encoding="utf-8")
            import tools.llm_config as _lc
            rc, captured, stdout = self._drive_closure_raw(
                repo_root, resume=True,
                prior_orch_by_spec={"spec/component/c": "orch_c_prev"},
                llm_config=_lc.load_llm_config(path))
            self.assertEqual(rc, 2)
            self.assertIn("llm_config_changed_since_launch", stdout)
            self.assertIn("orch_c_prev", stdout)
            self.assertEqual(captured, [])       # the member never ran

    def test_the_closure_TARGET_gate_refuses_a_changed_config_too(self) -> None:
        """A closure resume warm-resumes the target as well as its members, and `main`'s entry
        gate looked at whichever orchestration the operator named — which may be a dependency."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            self._seed_diamond(repo_root)
            path, cfg = self._closure_config(repo_root)
            self._pin_member(repo_root, "orch_target", "spec/problem/a",
                             "closure.yaml", cfg.sha256)
            path.write_text("defaults:\n  provider: claude_cli\n  model: changed\n",
                            encoding="utf-8")
            import tools.llm_config as _lc
            rc, captured, stdout = self._drive_closure_raw(
                repo_root, resume=True, prior_orch_by_spec={},
                llm_config=_lc.load_llm_config(path))
            self.assertEqual(rc, 2)
            self.assertIn("llm_config_changed_since_launch", stdout)
            self.assertIn("orch_target", stdout)
            self.assertNotIn("spec/problem/a", [c["spec_ref"] for c in captured])

    def test_an_unchanged_closure_member_resumes(self) -> None:
        """The control: the gate must not refuse the run it is meant to allow."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            self._seed_diamond(repo_root)
            path, cfg = self._closure_config(repo_root)
            self._pin_member(repo_root, "orch_c_prev", "spec/component/c",
                             "closure.yaml", cfg.sha256)
            rc, captured, stdout = self._drive_closure_raw(
                repo_root, resume=True,
                prior_orch_by_spec={"spec/component/c": "orch_c_prev"},
                llm_config=cfg)
            self.assertEqual(rc, 0, msg=stdout)
            self.assertIn("spec/component/c", [c["spec_ref"] for c in captured])

    def test_the_closure_driver_pins_the_same_configuration_on_every_node(self) -> None:
        """Each closure node gets its own orchestration and its own invocation record, so the
        pin has to be written per node — a driver that recorded it only on the target would
        leave every dependency unresumable (`llm_config_legacy_flags_removed`)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            self._seed_diamond(repo_root)
            _, cfg = self._closure_config(repo_root, model=None)
            rc, captured, _ = self._drive_closure_raw(
                repo_root, resume=False, prior_orch_by_spec=None, llm_config=cfg)
            self.assertEqual(rc, 0)
            self.assertTrue(captured)
            for node in captured:
                self.assertEqual(node["invocation"]["llm_config_sha256"], cfg.sha256)
                self.assertEqual(node["invocation"]["llm_config_path"],
                                 "closure.yaml")
                # The removed flags' literals are not recorded any more, by anyone.
                self.assertNotIn("llm_config_overrides", node["invocation"])

    def test_topological_order_dependencies_before_dependents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_diamond(repo_root)
            ordered, err = run_workflow._resolve_dependency_closure(
                repo_root, "spec/problem/a")
            self.assertIsNone(err)
            refs = [n["spec_id"] for n in ordered]
            # target 'a' excluded; c precedes b (b depends on c).
            self.assertEqual(refs, ["c", "b"])
            self.assertTrue(all(n["spec_versions"] == ["0.1.0"] for n in ordered))

    def test_cycle_detection_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_catalog(repo_root, [
                {"spec_kind": "component", "spec_id": "b", "spec_version": "0.1.0",
                 "deps_path": "spec/component/b/deps.yaml"},
                {"spec_kind": "component", "spec_id": "c", "spec_version": "0.1.0",
                 "deps_path": "spec/component/c/deps.yaml"},
            ])
            # b → c → b
            _write_deps(repo_root, "spec/component/b", "component", "b",
                        components=[("c", ">=0.1.0")])
            _write_deps(repo_root, "spec/component/c", "component", "c",
                        components=[("b", ">=0.1.0")])
            ordered, err = run_workflow._resolve_dependency_closure(
                repo_root, "spec/component/b")
            self.assertEqual(ordered, [])
            self.assertEqual(err["reason"], "dependency_cycle")

    def test_overlong_spec_id_dependency_fails_closed(self) -> None:
        # M3d closure-build gate: an over-length dependency spec_id is rejected at closure
        # resolution — before any node runs and before an already-ready dep is skipped, so
        # it cannot slip the per-node resolve_node gate. Mirrors spec_input_gates.MAX_SPEC_ID_LEN.
        from tools.spec_input_gates import MAX_SPEC_ID_LEN
        long_id = "d" * (MAX_SPEC_ID_LEN + 6)
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_catalog(repo_root, [
                {"spec_kind": "problem", "spec_id": "a", "spec_version": "0.3.0",
                 "deps_path": "spec/problem/a/deps.yaml"},
                {"spec_kind": "component", "spec_id": long_id, "spec_version": "0.1.0",
                 "deps_path": f"spec/component/{long_id}/deps.yaml"},
            ])
            _write_deps(repo_root, "spec/problem/a", "problem", "a",
                        components=[(long_id, ">=0.1.0 <1.0.0")])
            _write_deps(repo_root, f"spec/component/{long_id}", "component", long_id)
            ordered, err = run_workflow._resolve_dependency_closure(
                repo_root, "spec/problem/a")
            self.assertEqual(ordered, [])
            self.assertEqual(err["reason"], "spec_id_too_long")
            self.assertIn(str(MAX_SPEC_ID_LEN), err["detail"])

    def test_infra_dep_count_violation_fails_closed(self) -> None:
        # Closure-build mirror of resolve_node's spec-input gate: every non-infrastructure
        # spec in the closure — target included — declares exactly one `infrastructure`
        # dependency. Gating only per-node would let an ALREADY-READY dependency (skipped
        # before `_run_node` → resolve_node) slip past.
        def closure(target_infra, dep_infra):
            with tempfile.TemporaryDirectory() as tmp:
                repo_root = Path(tmp)
                _write_catalog(repo_root, [
                    {"spec_kind": "problem", "spec_id": "a", "spec_version": "0.3.0",
                     "deps_path": "spec/problem/a/deps.yaml"},
                    {"spec_kind": "component", "spec_id": "b", "spec_version": "0.1.0",
                     "deps_path": "spec/component/b/deps.yaml"},
                ])
                _write_deps(repo_root, "spec/problem/a", "problem", "a",
                            components=[("b", ">=0.1.0")], infrastructure=target_infra)
                _write_deps(repo_root, "spec/component/b", "component", "b",
                            infrastructure=dep_infra)
                return run_workflow._resolve_dependency_closure(
                    repo_root, "spec/problem/a")

        one = [(_HARNESS_ID, ">=0.1.0")]
        two = [(_HARNESS_ID, ">=0.1.0"), (_HARNESS_ID, ">=0.1.0")]
        # Target declares none.
        ordered, err = closure([], one)
        self.assertEqual(ordered, [])
        self.assertEqual(err["reason"], "infra_dep_count_invalid")
        self.assertIn("spec/problem/a", err["detail"])
        self.assertIn("exactly one", err["detail"])
        # Dependency declares two.
        ordered, err = closure(one, two)
        self.assertEqual(ordered, [])
        self.assertEqual(err["reason"], "infra_dep_count_invalid")
        self.assertIn("spec/component/b", err["detail"])
        self.assertIn("found 2", err["detail"])
        # Both declare exactly one -> resolves.
        ordered, err = closure(one, one)
        self.assertIsNone(err)
        self.assertEqual([n["spec_id"] for n in ordered], [_HARNESS_ID, "b"])

    def test_infrastructure_dependency_is_exempt_from_the_count_gate(self) -> None:
        # The harness node itself declares no infrastructure dependency, and it is a
        # closure member of every other node — so the gate must exempt its kind.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_catalog(repo_root, [
                {"spec_kind": "component", "spec_id": "b", "spec_version": "0.1.0",
                 "deps_path": "spec/component/b/deps.yaml"},
            ])
            _write_deps(repo_root, "spec/component/b", "component", "b")
            ordered, err = run_workflow._resolve_dependency_closure(
                repo_root, "spec/component/b")
            self.assertIsNone(err)
            self.assertEqual([n["spec_id"] for n in ordered], [_HARNESS_ID])

    def test_the_catalog_kind_outranks_a_self_declared_spec_kind(self) -> None:
        # Kind decides EXEMPTION from the count gate, and deps.yaml's top-level `spec_kind`
        # is carried by no schema — a spec could write `spec_kind: infrastructure` there and
        # exempt itself, while resolve_node (which reads the catalog) still rejects it after
        # every dependency in the closure has already been billed. The catalog wins.
        one = [(_HARNESS_ID, ">=0.1.0")]

        def closure(target_declares, target_infra, dep_declares, dep_infra):
            with tempfile.TemporaryDirectory() as tmp:
                repo_root = Path(tmp)
                _write_catalog(repo_root, [
                    {"spec_kind": "problem", "spec_id": "a", "spec_version": "0.3.0",
                     "deps_path": "spec/problem/a/deps.yaml"},
                    {"spec_kind": "component", "spec_id": "b", "spec_version": "0.1.0",
                     "deps_path": "spec/component/b/deps.yaml"},
                ])
                _write_deps(repo_root, "spec/problem/a", target_declares, "a",
                            components=[("b", ">=0.1.0")], infrastructure=target_infra)
                _write_deps(repo_root, "spec/component/b", dep_declares, "b",
                            infrastructure=dep_infra)
                return run_workflow._resolve_dependency_closure(
                    repo_root, "spec/problem/a")

        # Baseline: the same shapes with honest kinds and one harness edge each resolve.
        ordered, err = closure("problem", one, "component", one)
        self.assertIsNone(err)
        # The TARGET lies: registered `problem`, self-declares `infrastructure`, zero infra.
        ordered, err = closure("infrastructure", [], "component", one)
        self.assertEqual(ordered, [])
        self.assertEqual(err["reason"], "infra_dep_count_invalid")
        self.assertIn("spec/problem/a", err["detail"])
        # A DEPENDENCY lies the same way; its kind comes from the catalog-validated edge.
        ordered, err = closure("problem", one, "infrastructure", [])
        self.assertEqual(ordered, [])
        self.assertEqual(err["reason"], "infra_dep_count_invalid")
        self.assertIn("spec/component/b", err["detail"])

    def test_a_dependencys_kind_comes_from_its_edge_not_a_second_lookup(self) -> None:
        # The edge short-circuit is the structural half of the rule: a dependency's kind was
        # already fixed by the catalog-validated edge that pulled it in. Pin it directly —
        # a spec_id lookup would answer the same way for a REGISTERED dep, so only an edge
        # whose spec dir does not match its registered spec_id can tell the two apart.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_catalog(repo_root, [
                {"spec_kind": "problem", "spec_id": "a", "spec_version": "0.3.0",
                 "deps_path": "spec/problem/a/deps.yaml"},
                # `b` lives in a directory whose basename is NOT its spec_id, so a
                # `Path(spec_ref).name` lookup finds nothing and only the edge knows the kind.
                {"spec_kind": "component", "spec_id": "b", "spec_version": "0.1.0",
                 "deps_path": "spec/component/b_dir/deps.yaml"},
            ])
            _write_deps(repo_root, "spec/problem/a", "problem", "a",
                        components=[("b", ">=0.1.0")])
            _write_deps(repo_root, "spec/component/b_dir", "infrastructure", "b",
                        infrastructure=[])
            ordered, err = run_workflow._resolve_dependency_closure(
                repo_root, "spec/problem/a")
            # Without the edge short-circuit the self-declared `infrastructure` would exempt
            # `b` and the closure would resolve.
            self.assertEqual(ordered, [])
            self.assertEqual(err["reason"], "infra_dep_count_invalid")
            self.assertIn("spec/component/b_dir", err["detail"])

    def test_an_unconfirmable_kind_reports_the_registry_not_the_dep_count(self) -> None:
        # The verdict rests on the kind. When the catalog cannot confirm it, rejecting on the
        # dep count sends the operator to edit deps.yaml during what is actually a registry
        # problem — and the count may well be right for the node's true kind.
        def run(catalog_text, declared_kind="component", infra=0):
            with tempfile.TemporaryDirectory() as tmp:
                repo_root = Path(tmp)
                (repo_root / "spec" / "registry").mkdir(parents=True)
                (repo_root / "spec" / "registry" / "spec_catalog.yaml").write_text(
                    catalog_text, encoding="utf-8")
                _write_deps(repo_root, "spec/component/z", declared_kind, "z",
                            infrastructure=[(_HARNESS_ID, ">=0.1.0")] * infra)
                if infra:
                    _write_deps(repo_root, _HARNESS_REF, "infrastructure", _HARNESS_ID)
                from tools.orchestration_runtime import _load_spec_catalog
                _load_spec_catalog.cache_clear()
                return run_workflow._resolve_dependency_closure(
                    repo_root, "spec/component/z")

        # Corrupt registry: the reason names the registry, not the dependency count.
        ordered, err = run("{ this is not a catalog\n")
        self.assertEqual(ordered, [])
        self.assertEqual(err["reason"], "spec_catalog_corrupt")
        # Same spec_id registered under two kinds: spec_id must be unique repo-wide
        # (docs/SPEC.md req. 4), and `resolve_node` returns the FIRST match without noticing
        # the duplicate — so resolving it here by catalog order would make the two capture
        # points disagree by luck of ordering.
        dup = ("catalog_version: 0.2.0\nupdated_at: 2026-06-18\nspecs:\n"
               "  - spec_kind: component\n    spec_id: z\n    spec_version: \"0.1.0\"\n"
               "    deps_path: spec/component/z/deps.yaml\n"
               "  - spec_kind: infrastructure\n    spec_id: z\n    spec_version: \"0.1.0\"\n"
               "    deps_path: spec/component/z/deps.yaml\n")
        ordered, err = run(dup)
        self.assertEqual(ordered, [])
        self.assertEqual(err["reason"], "spec_catalog_corrupt")
        self.assertIn("multiple spec_kinds", err["detail"])
        self.assertIn("docs/SPEC.md req. 4", err["detail"])
        # Reported even when the declared kind WOULD have exempted the node. An exemption
        # granted on an unresolvable kind is as unfounded as a rejection, and honoring it
        # would let a spec self-declare `infrastructure` to skip the gate — after the whole
        # closure has been billed, since resolve_node only refuses the target at the end.
        ordered, err = run(dup, declared_kind="infrastructure")
        self.assertEqual(ordered, [])
        self.assertEqual(err["reason"], "spec_catalog_corrupt")
        # ...and even at the dep count that would otherwise PASS. The count check
        # short-circuits on exactly 1 before the kind is consulted at all, so relying on a
        # violation being produced would leave the common, well-formed shape unreported —
        # the two capture points then disagree by luck of catalog order, silently.
        ordered, err = run(dup, declared_kind="component", infra=1)
        self.assertEqual(ordered, [])
        self.assertEqual(err["reason"], "spec_catalog_corrupt")

    def test_an_unregistered_spec_is_judged_by_its_declared_kind(self) -> None:
        # Registered under no kind at all is NOT the same as "the registry could not answer":
        # the declared value decides, exemption included. That is safe because an
        # unregistered spec_ref is refused by resolve_node (target) and by
        # `_matching_dep_versions` (dependency edge) whatever it declares, so it can never
        # reach a phase — and reporting a registry defect here would mislabel a plain
        # unregistered-spec mistake.
        for declared, expect_err in (("component", "infra_dep_count_invalid"),
                                     ("infrastructure", None)):
            with tempfile.TemporaryDirectory() as tmp:
                repo_root = Path(tmp)
                _write_catalog(repo_root, [
                    {"spec_kind": "component", "spec_id": "other", "spec_version": "0.1.0",
                     "deps_path": "spec/component/other/deps.yaml"},
                ])
                _write_deps(repo_root, "spec/component/z", declared, "z", infrastructure=[])
                from tools.orchestration_runtime import _load_spec_catalog
                _load_spec_catalog.cache_clear()
                _, err = run_workflow._resolve_dependency_closure(
                    repo_root, "spec/component/z")
                self.assertEqual((err or {}).get("reason"), expect_err, declared)

    def test_a_registered_harness_target_is_exempt_without_a_declared_kind(self) -> None:
        # The mirror of the test above: the catalog must also be able to EXEMPT. A harness
        # deps.yaml that omits the (schema-less) `spec_kind` line must not be read as a
        # non-infrastructure spec and told to add a harness dependency to itself.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_catalog(repo_root, [
                {"spec_kind": "infrastructure", "spec_id": "h", "spec_version": "0.7.0",
                 "deps_path": "spec/infrastructure/h/deps.yaml"},
            ])
            (repo_root / "spec" / "infrastructure" / "h").mkdir(parents=True)
            (repo_root / "spec" / "infrastructure" / "h" / "deps.yaml").write_text(
                "dependencies:\n  components: []\n  profiles: []\n", encoding="utf-8")
            ordered, err = run_workflow._resolve_dependency_closure(
                repo_root, "spec/infrastructure/h")
            self.assertIsNone(err)
            self.assertEqual(ordered, [])

    def test_malformed_deps_keeps_its_own_reason(self) -> None:
        # The count gate runs AFTER the readable/well-formed checks, so an unreadable or
        # schema-broken deps.yaml still reports the reason that names the real defect.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_catalog(repo_root, [
                {"spec_kind": "component", "spec_id": "b", "spec_version": "0.1.0",
                 "deps_path": "spec/component/b/deps.yaml"},
            ])
            (repo_root / "spec" / "component" / "b").mkdir(parents=True)
            (repo_root / "spec" / "component" / "b" / "deps.yaml").write_text(
                "spec_id: b\nspec_kind: component\ndependencies:\n  infrastructures: []\n",
                encoding="utf-8")
            ordered, err = run_workflow._resolve_dependency_closure(
                repo_root, "spec/component/b")
            self.assertEqual(ordered, [])
            self.assertEqual(err["reason"], "dependency_deps_malformed")
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_catalog(repo_root, [
                {"spec_kind": "component", "spec_id": "b", "spec_version": "0.1.0",
                 "deps_path": "spec/component/b/deps.yaml"},
            ])
            ordered, err = run_workflow._resolve_dependency_closure(
                repo_root, "spec/component/b")
            self.assertEqual(ordered, [])
            self.assertEqual(err["reason"], "dependency_deps_unreadable")

    def test_unresolvable_dependency_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_catalog(repo_root, [
                {"spec_kind": "problem", "spec_id": "a", "spec_version": "0.3.0",
                 "deps_path": "spec/problem/a/deps.yaml"},
                {"spec_kind": "component", "spec_id": "b", "spec_version": "0.1.0",
                 "deps_path": "spec/component/b/deps.yaml"},
            ])
            # constraint matches no catalog version of b
            _write_deps(repo_root, "spec/problem/a", "problem", "a",
                        components=[("b", ">=2.0.0")])
            _write_deps(repo_root, "spec/component/b", "component", "b")
            ordered, err = run_workflow._resolve_dependency_closure(
                repo_root, "spec/problem/a")
            self.assertEqual(ordered, [])
            self.assertEqual(err["reason"], "dependency_unresolvable")

    def test_version_conflict_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_catalog(repo_root, [
                {"spec_kind": "problem", "spec_id": "a", "spec_version": "0.3.0",
                 "deps_path": "spec/problem/a/deps.yaml"},
                {"spec_kind": "component", "spec_id": "b", "spec_version": "1.0.0",
                 "deps_path": "spec/component/b/deps.yaml"},
                {"spec_kind": "component", "spec_id": "b", "spec_version": "2.0.0",
                 "deps_path": "spec/component/b/deps.yaml"},
                {"spec_kind": "component", "spec_id": "c", "spec_version": "0.1.0",
                 "deps_path": "spec/component/c/deps.yaml"},
            ])
            # a → b==1.0.0, c ; c → b==2.0.0  → same spec dir, different version
            _write_deps(repo_root, "spec/problem/a", "problem", "a",
                        components=[("b", "==1.0.0"), ("c", ">=0.1.0")])
            _write_deps(repo_root, "spec/component/b", "component", "b")
            _write_deps(repo_root, "spec/component/c", "component", "c",
                        components=[("b", "==2.0.0")])
            ordered, err = run_workflow._resolve_dependency_closure(
                repo_root, "spec/problem/a")
            self.assertEqual(ordered, [])
            self.assertEqual(err["reason"], "dependency_version_conflict")

    def test_driver_runs_dependencies_bottom_up_then_target(self) -> None:
        from tools.orchestration_runtime import _load_spec_catalog
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            self._seed_diamond(repo_root)
            _load_spec_catalog.cache_clear()

            calls: list[tuple[str, str]] = []
            ran: set[str] = set()

            def fake_run_node(**kw):
                calls.append((kw["spec_ref"], kw["until_phase"]))
                ran.add(kw["spec_ref"])
                return 0

            # A node becomes ready once it has run (simulates artifact production
            # without a real workflow). Exercises both the pre-run skip check and
            # the post-run readiness verification.
            def fake_ready(repo_root, node, required_stages):
                return node["spec_ref"] in ran

            orig = run_workflow._run_node
            orig_ready = run_workflow._dependency_node_ready
            run_workflow._run_node = fake_run_node  # type: ignore[assignment]
            run_workflow._dependency_node_ready = fake_ready  # type: ignore[assignment]
            try:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = run_workflow._run_with_dependency_closure(
                        repo_root=repo_root,
                        base_env={"PATH": os.environ.get("PATH", "")},
                        target_orchestration_id="orch_target",
                        target_spec_ref="spec/problem/a",
                        target_source_dependency_ref="spec/problem/a/deps.yaml",
                        until_phase="Validate",
                        llm="claude",
                        llm_command="claude",
                        llm_config=_sample_config("claude"),
                        workflow_mode="dev",
                        agent_model=None,
                        status="running",
                        run_conductor=False,
                    )
            finally:
                run_workflow._run_node = orig  # type: ignore[assignment]
                run_workflow._dependency_node_ready = orig_ready  # type: ignore[assignment]

            self.assertEqual(rc, 0)
            # deps (c, b) run before the target a; target last.
            self.assertEqual([c[0] for c in calls],
                             ["spec/component/c", "spec/component/b", "spec/problem/a"])
            # target until_phase >= generate → deps run to Validate.
            self.assertTrue(all(c[1] == "Validate" for c in calls))

    def _drive_closure_raw(self, repo_root, *, resume, prior_orch_by_spec,
                           llm_config=None):
        """Run the closure driver over the seeded diamond with _run_node captured.
        Nodes become ready once run. Returns (rc, captured kwargs list, stdout text)."""
        from tools.orchestration_runtime import _load_spec_catalog
        _load_spec_catalog.cache_clear()
        captured: list[dict] = []
        ran: set[str] = set()

        def fake_run_node(**kw):
            captured.append(kw)
            ran.add(kw["spec_ref"])
            return 0

        def fake_ready(repo_root, node, required_stages):
            return node["spec_ref"] in ran

        orig = run_workflow._run_node
        orig_ready = run_workflow._dependency_node_ready
        run_workflow._run_node = fake_run_node  # type: ignore[assignment]
        run_workflow._dependency_node_ready = fake_ready  # type: ignore[assignment]
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = run_workflow._run_with_dependency_closure(
                    repo_root=repo_root,
                    base_env={"PATH": os.environ.get("PATH", "")},
                    target_orchestration_id="orch_target",
                    target_spec_ref="spec/problem/a",
                    target_source_dependency_ref="spec/problem/a/deps.yaml",
                    until_phase="Validate",
                    llm="claude",
                    llm_command="claude",
                    llm_config=llm_config or _sample_config("claude"),
                    workflow_mode="dev",
                    agent_model=None,
                    status="running",
                    run_conductor=False,
                    resume=resume,
                    prior_orch_by_spec=prior_orch_by_spec,
                    raw_argv=["spec/problem/a", "validate", "--with-deps"],
                )
        finally:
            run_workflow._run_node = orig  # type: ignore[assignment]
            run_workflow._dependency_node_ready = orig_ready  # type: ignore[assignment]
        return rc, captured, buf.getvalue()

    def _drive_closure_capture(self, repo_root, *, resume, prior_orch_by_spec):
        """Like `_drive_closure_raw` but asserts a clean (rc 0) run and returns just the
        captured _run_node kwargs dicts."""
        rc, captured, _ = self._drive_closure_raw(
            repo_root, resume=resume, prior_orch_by_spec=prior_orch_by_spec)
        self.assertEqual(rc, 0)
        return captured

    def test_fresh_closure_records_closure_id_on_every_node(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            self._seed_diamond(repo_root)
            captured = self._drive_closure_capture(
                repo_root, resume=False, prior_orch_by_spec=None)
            # c, b, then target a
            self.assertEqual([c["spec_ref"] for c in captured],
                             ["spec/component/c", "spec/component/b", "spec/problem/a"])
            for kw in captured:
                self.assertFalse(kw["resume_mode"])
                self.assertIsNotNone(kw["invocation"])
                self.assertEqual(kw["invocation"]["closure_id"], "orch_target")
                self.assertEqual(kw["invocation"]["closure_target_spec_ref"],
                                 "spec/problem/a")
                self.assertTrue(kw["invocation"]["with_deps"])

    def test_closure_resume_reuses_prior_orch_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            self._seed_diamond(repo_root)
            self._seed_prior_member(repo_root, "orch_c_prev", "spec/component/c")
            captured = self._drive_closure_capture(
                repo_root, resume=True,
                prior_orch_by_spec={"spec/component/c": "orch_c_prev"})
            by_spec = {c["spec_ref"]: c for c in captured}
            # c has a prior orch → resumed warm, no fresh invocation
            self.assertEqual(by_spec["spec/component/c"]["orchestration_id"], "orch_c_prev")
            self.assertTrue(by_spec["spec/component/c"]["resume_mode"])
            self.assertIsNone(by_spec["spec/component/c"]["invocation"])
            # b has no prior orch → fresh, records the closure invocation
            self.assertFalse(by_spec["spec/component/b"]["resume_mode"])
            self.assertIsNotNone(by_spec["spec/component/b"]["invocation"])
            self.assertEqual(
                by_spec["spec/component/b"]["invocation"]["closure_id"], "orch_target")

    def test_closure_resume_refreshes_closure_until_on_resumed_deps(self) -> None:
        # A resumed dependency gets the effective closure until_phase forwarded so its
        # persisted copy stays current (durable phase override); a freshly cold-inited
        # node relies on its written invocation, not this arg.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            self._seed_diamond(repo_root)
            self._seed_prior_member(repo_root, "orch_c_prev", "spec/component/c")
            captured = self._drive_closure_capture(
                repo_root, resume=True,
                prior_orch_by_spec={"spec/component/c": "orch_c_prev"})
            by_spec = {c["spec_ref"]: c for c in captured}
            # c is resumed → closure_until_phase forwarded (= the closure until)
            self.assertEqual(by_spec["spec/component/c"]["closure_until_phase"], "Validate")
            # b is fresh → not forwarded (cold init writes it via invocation)
            self.assertIsNone(by_spec["spec/component/b"]["closure_until_phase"])

    def test_closure_resume_target_resume_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            self._seed_diamond(repo_root)
            # target orchestration already exists AND is this closure's prior target
            # run (matching spec_ref AND invocation.closure_id) → target is warm-resumed.
            target_meta = (repo_root / "workspace" / "orchestrations"
                           / "orch_target" / "orchestration_meta.json")
            target_meta.parent.mkdir(parents=True, exist_ok=True)
            target_meta.write_text(json.dumps(
                {"spec_ref": "spec/problem/a",
                 "invocation": {"closure_id": "orch_target", "generate_executor": "pure",
                                "llm_config_path": _recorded_sample_pin(repo_root),
                                "llm_config_sha256": _sample_config("claude").sha256}}),
                encoding="utf-8")
            captured = self._drive_closure_capture(
                repo_root, resume=True, prior_orch_by_spec={})
            target = [c for c in captured if c["spec_ref"] == "spec/problem/a"][0]
            self.assertEqual(target["orchestration_id"], "orch_target")
            self.assertTrue(target["resume_mode"])
            self.assertIsNone(target["invocation"])

    def test_closure_resume_legacy_recorded_dependency_fails_closed(self) -> None:
        # M-F: a closure resume must validate EVERY warm-resumed member, not just the entry. A
        # dependency orchestration recorded `legacy` (a mixed closure) must fail-close here rather
        # than silently resume under the pure-only dispatch.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            self._seed_diamond(repo_root)
            self._seed_prior_member(repo_root, "orch_c_prev", "spec/component/c",
                                    executor="legacy")
            rc, captured, out = self._drive_closure_raw(
                repo_root, resume=True,
                prior_orch_by_spec={"spec/component/c": "orch_c_prev"})
            self.assertEqual(rc, 2, out)
            self.assertIn("generate_executor_legacy_removed", out)
            # the legacy dependency was NOT resumed (fail-closed before _run_node)
            self.assertNotIn("spec/component/c",
                             [c["spec_ref"] for c in captured])

    def test_closure_resume_prefield_recorded_target_fails_closed(self) -> None:
        # M-F: a warm-resumed TARGET whose recorded executor is absent (a pre-adoption run) must
        # also fail-close, mirroring the per-dependency gate.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            self._seed_diamond(repo_root)
            target_meta = (repo_root / "workspace" / "orchestrations"
                           / "orch_target" / "orchestration_meta.json")
            target_meta.parent.mkdir(parents=True, exist_ok=True)
            # spec_ref + closure_id match → target_resume True, but no generate_executor recorded.
            target_meta.write_text(json.dumps(
                {"spec_ref": "spec/problem/a",
                 "invocation": {"closure_id": "orch_target"}}),
                encoding="utf-8")
            rc, captured, out = self._drive_closure_raw(
                repo_root, resume=True, prior_orch_by_spec={})
            self.assertEqual(rc, 2, out)
            self.assertIn("generate_executor_legacy_removed", out)
            self.assertNotIn("spec/problem/a",
                             [c["spec_ref"] for c in captured])

    def test_closure_resume_target_unrelated_meta_cold_inits(self) -> None:
        # The reserved target id already holds an UNRELATED pre-existing
        # orchestration (different spec) that the failed closure never reached.
        # It must be cold-initialized as the intended target, NOT warm-resumed off
        # the unrelated run's checkpoint.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            self._seed_diamond(repo_root)
            target_meta = (repo_root / "workspace" / "orchestrations"
                           / "orch_target" / "orchestration_meta.json")
            target_meta.parent.mkdir(parents=True, exist_ok=True)
            target_meta.write_text(
                json.dumps({"spec_ref": "spec/problem/UNRELATED", "status": "pass"}),
                encoding="utf-8")
            captured = self._drive_closure_capture(
                repo_root, resume=True, prior_orch_by_spec={})
            target = [c for c in captured if c["spec_ref"] == "spec/problem/a"][0]
            self.assertEqual(target["orchestration_id"], "orch_target")
            self.assertFalse(target["resume_mode"], "unrelated meta must not warm-resume")
            # cold init → a fresh closure invocation is written for the real target
            self.assertIsNotNone(target["invocation"])
            self.assertEqual(target["invocation"]["closure_target_spec_ref"],
                             "spec/problem/a")

    def test_closure_resume_target_same_spec_unlinked_cold_inits(self) -> None:
        # The reserved target id holds an orchestration for the SAME spec but not
        # created as THIS closure's target (e.g. a standalone run under a reused id):
        # no invocation.closure_id link. Must cold-init, not warm-resume its stale
        # checkpoint. Guards spec-match-only false positive.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            self._seed_diamond(repo_root)
            target_meta = (repo_root / "workspace" / "orchestrations"
                           / "orch_target" / "orchestration_meta.json")
            target_meta.parent.mkdir(parents=True, exist_ok=True)
            # same spec as the target, but NO closure link (standalone prior run)
            target_meta.write_text(
                json.dumps({"spec_ref": "spec/problem/a", "status": "pass"}),
                encoding="utf-8")
            captured = self._drive_closure_capture(
                repo_root, resume=True, prior_orch_by_spec={})
            target = [c for c in captured if c["spec_ref"] == "spec/problem/a"][0]
            self.assertFalse(target["resume_mode"],
                             "same-spec but unlinked orch must not warm-resume")
            self.assertIsNotNone(target["invocation"])

    def test_driver_renders_closure_events_in_human_mode(self) -> None:
        # In human mode the closure-level events (dependency_node_begin and the
        # final failure summary) must NOT leak raw JSON: they go through the same
        # _format_event_human renderer the per-node tee uses.
        from tools.orchestration_runtime import _load_spec_catalog
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            self._seed_diamond(repo_root)
            _load_spec_catalog.cache_clear()

            def fake_run_node(**kw):
                return 0  # success, but no artifacts → not_ready_after_run

            orig = run_workflow._run_node
            run_workflow._run_node = fake_run_node  # type: ignore[assignment]
            try:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = run_workflow._run_with_dependency_closure(
                        repo_root=repo_root,
                        base_env={"PATH": os.environ.get("PATH", "")},
                        target_orchestration_id="orch_target",
                        target_spec_ref="spec/problem/a",
                        target_source_dependency_ref="spec/problem/a/deps.yaml",
                        until_phase="Validate",
                        llm="claude",
                        llm_command="claude",
                        llm_config=_sample_config("claude"),
                        workflow_mode="dev",
                        agent_model=None,
                        status="running",
                        run_conductor=False,
                        stdout_format="human",
                    )
            finally:
                run_workflow._run_node = orig  # type: ignore[assignment]

            self.assertEqual(rc, 2)
            lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
            # No raw JSON braces leak onto the terminal in human mode.
            self.assertFalse(
                any(ln.lstrip().startswith("{") for ln in lines), lines)
            # dependency_node_begin renders with the [dep ] prefix.
            self.assertTrue(
                any(ln.startswith("[dep ]") and "component/c" in ln
                    for ln in lines),
                lines,
            )
            # The closure failure summary renders with the [FAIL] prefix.
            self.assertTrue(
                any(ln.startswith("[FAIL]")
                    and "dependency_not_ready_after_run" in ln
                    for ln in lines),
                lines,
            )

    def test_driver_stops_when_dependency_not_ready_after_run(self) -> None:
        # A dependency that exits 0 without producing readiness evidence
        # (e.g. --no-run-conductor) must stop the run before the dependent/target.
        from tools.orchestration_runtime import _load_spec_catalog
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            self._seed_diamond(repo_root)
            _load_spec_catalog.cache_clear()

            calls: list[str] = []

            def fake_run_node(**kw):
                calls.append(kw["spec_ref"])
                return 0  # success, but no artifacts are produced

            orig = run_workflow._run_node
            run_workflow._run_node = fake_run_node  # type: ignore[assignment]
            try:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = run_workflow._run_with_dependency_closure(
                        repo_root=repo_root,
                        base_env={"PATH": os.environ.get("PATH", "")},
                        target_orchestration_id="orch_target",
                        target_spec_ref="spec/problem/a",
                        target_source_dependency_ref="spec/problem/a/deps.yaml",
                        until_phase="Validate",
                        llm="claude",
                        llm_command="claude",
                        llm_config=_sample_config("claude"),
                        workflow_mode="dev",
                        agent_model=None,
                        status="running",
                        run_conductor=False,
                    )
            finally:
                run_workflow._run_node = orig  # type: ignore[assignment]

            self.assertEqual(rc, 2)
            # Stops after the first dependency (c); b and target never run.
            self.assertEqual(calls, ["spec/component/c"])
            last = json.loads(buf.getvalue().strip().splitlines()[-1])
            self.assertEqual(last["reason"], "dependency_not_ready_after_run")
            self.assertEqual(last["failed_dependency_node"], "infrastructure/c@0.1.0")

    def test_driver_stops_on_first_dependency_failure(self) -> None:
        from tools.orchestration_runtime import _load_spec_catalog
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            self._seed_diamond(repo_root)
            _load_spec_catalog.cache_clear()

            calls: list[str] = []

            def fake_run_node(**kw):
                calls.append(kw["spec_ref"])
                # Fail the first dependency (c).
                return 2 if kw["spec_ref"] == "spec/component/c" else 0

            orig = run_workflow._run_node
            run_workflow._run_node = fake_run_node  # type: ignore[assignment]
            try:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = run_workflow._run_with_dependency_closure(
                        repo_root=repo_root,
                        base_env={"PATH": os.environ.get("PATH", "")},
                        target_orchestration_id="orch_target",
                        target_spec_ref="spec/problem/a",
                        target_source_dependency_ref="spec/problem/a/deps.yaml",
                        until_phase="Validate",
                        llm="claude",
                        llm_command="claude",
                        llm_config=_sample_config("claude"),
                        workflow_mode="dev",
                        agent_model=None,
                        status="running",
                        run_conductor=False,
                    )
            finally:
                run_workflow._run_node = orig  # type: ignore[assignment]

            self.assertEqual(rc, 2)
            # Stopped after c failed; b and the target a never ran.
            self.assertEqual(calls, ["spec/component/c"])
            last = json.loads(buf.getvalue().strip().splitlines()[-1])
            self.assertEqual(last["reason"], "dependency_node_failed")
            self.assertEqual(last["failed_dependency_node"], "infrastructure/c@0.1.0")

    def test_leaf_target_closure_does_not_require_catalog(self) -> None:
        # A leaf target (empty deps) must resolve to an empty closure without
        # loading the catalog, so a missing/corrupt registry does not break an
        # otherwise-launchable leaf --with-deps run. The harness is the only spec
        # kind that legitimately declares no dependency at all (every other kind
        # must declare its one `infrastructure` edge), so it is the leaf case.
        # This also pins the surviving half of the infra-count gate's registry handling:
        # with no catalog to confirm the kind, the DECLARED `infrastructure` may still grant
        # the exemption (only a rejection resting on an unconfirmable kind is suppressed and
        # re-reported as the registry defect).
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_deps(repo_root, "spec/infrastructure/leaf", "infrastructure", "leaf")
            # Intentionally NO spec/registry/spec_catalog.yaml on disk.
            ordered, err = run_workflow._resolve_dependency_closure(
                repo_root, "spec/infrastructure/leaf")
            self.assertIsNone(err)
            self.assertEqual(ordered, [])

    def test_resolve_spec_ref_for_uses_deps_path_dirname(self) -> None:
        from tools.orchestration_runtime import resolve_spec_ref_for, _load_spec_catalog
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_catalog(repo_root, [
                {"spec_kind": "component", "spec_id": "b", "spec_version": "0.1.0",
                 "deps_path": "spec/component/b/deps.yaml"},
            ])
            _load_spec_catalog.cache_clear()
            self.assertEqual(
                resolve_spec_ref_for(repo_root, "component", "b"),
                "spec/component/b",
            )
            self.assertIsNone(resolve_spec_ref_for(repo_root, "component", "missing"))

    def _drive_closure_with_runtime(self, repo_root, *, resume, prior_orch_by_spec):
        """Like `_drive_closure_raw` but also captures the runtime subprocess calls the
        driver makes (the liveness gate's `set-status` terminalizations)."""
        observed: list[list[str]] = []

        def fake_runtime_command(root, env, args):  # type: ignore[no-untyped-def]
            observed.append(args)
            return run_workflow.RuntimeResult(payload={"status": "ok"}, raw_stdout="{}")

        original = run_workflow._runtime_command
        run_workflow._runtime_command = fake_runtime_command  # type: ignore[assignment]
        try:
            rc, captured, stdout = self._drive_closure_raw(
                repo_root, resume=resume, prior_orch_by_spec=prior_orch_by_spec)
        finally:
            run_workflow._runtime_command = original  # type: ignore[assignment]
        return rc, captured, stdout, observed

    def test_closure_resume_terminalizes_dead_prior_dependency(self) -> None:
        # A dependency whose own driver died mid-closure is stuck at `running`; the
        # entry-point resume gate never sees it, so the closure driver terminalizes it
        # here — otherwise its warm resume skips the crash reconciliations.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            self._seed_diamond(repo_root)
            self._seed_prior_member(
                repo_root, "orch_c_prev", "spec/component/c",
                status="running", driver={"pid": 4242, "verdict": "dead"})
            with _forced_liveness():
                rc, captured, stdout, calls = self._drive_closure_with_runtime(
                    repo_root, resume=True,
                    prior_orch_by_spec={"spec/component/c": "orch_c_prev"})
            self.assertEqual(rc, 0)
            set_status = [c for c in calls if c and c[0] == "set-status"]
            self.assertEqual(len(set_status), 1)
            self.assertEqual(
                set_status[0][set_status[0].index("--reason-code") + 1], "driver_crashed")
            self.assertEqual(
                set_status[0][set_status[0].index("--orchestration-id") + 1], "orch_c_prev")
            # Terminalized, then still warm-resumed (the checkpoint is the whole point).
            by_spec = {c["spec_ref"]: c for c in captured}
            self.assertTrue(by_spec["spec/component/c"]["resume_mode"])
            self.assertEqual(
                by_spec["spec/component/c"]["orchestration_id"], "orch_c_prev")
            self.assertIn("dead_driver_terminalized", stdout)

    def test_closure_resume_claims_a_member_before_terminalizing_it(self) -> None:
        # The per-member warm guard WRITES when it finds a dead driver
        # (`set-status fail/driver_crashed`). Two closures resuming the same member
        # would both perform that write, the later one flipping a member another
        # closure had already reset to `running` back to `fail`. The member's
        # orchestration claim has to be taken before its guard runs, not after.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            self._seed_diamond(repo_root)
            self._seed_prior_member(
                repo_root, "orch_c_prev", "spec/component/c",
                status="running", driver={"pid": 4242, "verdict": "dead"})
            with run_workflow._exclusive_claim(repo_root, "orch", "orch_c_prev") as held:
                self.assertTrue(held)
                with _forced_liveness():
                    rc, captured, stdout, calls = self._drive_closure_with_runtime(
                        repo_root, resume=True,
                        prior_orch_by_spec={"spec/component/c": "orch_c_prev"})
            self.assertEqual(rc, 2)
            self.assertEqual(captured, [])
            # Refused before the guard: nothing was terminalized.
            self.assertEqual([c for c in calls if c and c[0] == "set-status"], [])
            last = json.loads(stdout.strip().splitlines()[-1])
            self.assertEqual(last["reason"], "concurrent_orchestration_running")
            self.assertEqual(last["orchestration_id"], "orch_c_prev")

    def test_closure_resume_refuses_live_prior_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            self._seed_diamond(repo_root)
            self._seed_prior_member(
                repo_root, "orch_c_prev", "spec/component/c",
                status="running", driver={"pid": 4242, "verdict": "alive"})
            with _forced_liveness():
                rc, captured, stdout, calls = self._drive_closure_with_runtime(
                    repo_root, resume=True,
                    prior_orch_by_spec={"spec/component/c": "orch_c_prev"})
            self.assertEqual(rc, 2)
            self.assertEqual(captured, [])
            self.assertEqual([c for c in calls if c and c[0] == "set-status"], [])
            last = json.loads(stdout.strip().splitlines()[-1])
            # Same reason code the entry-point resume gate uses for an explicit-id
            # refusal: a consumer must not need to know which gate refused.
            self.assertEqual(last["reason"], "orchestration_driver_alive")
            self.assertEqual(last["failed_dependency_node"], "infrastructure/c@0.1.0")

    def test_cold_closure_blocks_on_live_orchestration_for_a_node(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            self._seed_diamond(repo_root)
            self._seed_prior_member(
                repo_root, "orch_c_live", "spec/component/c",
                status="running", driver={"pid": 4242, "verdict": "alive"})
            with _forced_liveness():
                rc, captured, stdout, _calls = self._drive_closure_with_runtime(
                    repo_root, resume=False, prior_orch_by_spec=None)
            self.assertEqual(rc, 2)
            self.assertEqual(captured, [])
            last = json.loads(stdout.strip().splitlines()[-1])
            self.assertEqual(last["reason"], "concurrent_orchestration_running")
            self.assertEqual(last["orchestration_id"], "orch_c_live")

    def test_closure_resume_skips_the_liveness_probe_for_a_terminal_member(self) -> None:
        # The normal warm-resume case: the member terminalized on its own (`fail`), so
        # there is no driver to probe. Probing anyway would let a recycled pid recorded
        # on a FINISHED run refuse the whole closure.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            self._seed_diamond(repo_root)
            self._seed_prior_member(
                repo_root, "orch_c_prev", "spec/component/c",
                status="fail", driver={"pid": 4242, "verdict": "alive"})
            with _forced_liveness():
                rc, captured, stdout, calls = self._drive_closure_with_runtime(
                    repo_root, resume=True,
                    prior_orch_by_spec={"spec/component/c": "orch_c_prev"})
            self.assertEqual(rc, 0)
            self.assertEqual(len(captured), 3)
            self.assertEqual([c for c in calls if c and c[0] == "set-status"], [])
            self.assertNotIn("orchestration_driver_alive", stdout)

    def test_closure_resume_warns_when_member_liveness_indeterminate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            self._seed_diamond(repo_root)
            self._seed_prior_member(
                repo_root, "orch_c_prev", "spec/component/c",
                status="running", driver={"pid": 4242, "verdict": "unknown"})
            with _forced_liveness():
                rc, captured, stdout, calls = self._drive_closure_with_runtime(
                    repo_root, resume=True,
                    prior_orch_by_spec={"spec/component/c": "orch_c_prev"})
            self.assertEqual(rc, 0)
            warns = [
                json.loads(line) for line in stdout.splitlines()
                if line.strip().startswith("{")
                and json.loads(line).get("event") == "resume_liveness_indeterminate"
            ]
            self.assertEqual([w["orchestration_id"] for w in warns], ["orch_c_prev"])
            # Indeterminate never terminalizes, and never blocks the resume.
            self.assertEqual([c for c in calls if c and c[0] == "set-status"], [])
            self.assertEqual(len(captured), 3)

    def test_closure_resume_fails_when_member_terminalize_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            self._seed_diamond(repo_root)
            self._seed_prior_member(
                repo_root, "orch_c_prev", "spec/component/c",
                status="running", driver={"pid": 4242, "verdict": "dead"})

            def failing_runtime(root, env, args):  # type: ignore[no-untyped-def]
                raise RuntimeError("runtime command failed (set-status): boom")

            original = run_workflow._runtime_command
            run_workflow._runtime_command = failing_runtime  # type: ignore[assignment]
            try:
                with _forced_liveness():
                    rc, captured, stdout = self._drive_closure_raw(
                        repo_root, resume=True,
                        prior_orch_by_spec={"spec/component/c": "orch_c_prev"})
            finally:
                run_workflow._runtime_command = original  # type: ignore[assignment]
            self.assertEqual(rc, 2)
            self.assertEqual(captured, [])
            last = json.loads(stdout.strip().splitlines()[-1])
            self.assertEqual(last["reason"], "dead_driver_terminalize_failed")

    def test_cold_closure_blocks_on_live_orchestration_for_the_target(self) -> None:
        # The target's own gate: the dependencies are all ready/ran, and only then is
        # the target checked. Without it the closure would drive the target into a
        # live run's workspace state after paying for every dependency.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            self._seed_diamond(repo_root)
            self._seed_prior_member(
                repo_root, "orch_a_live", "spec/problem/a",
                status="running", driver={"pid": 4242, "verdict": "alive"})
            with _forced_liveness():
                rc, captured, stdout, _calls = self._drive_closure_with_runtime(
                    repo_root, resume=False, prior_orch_by_spec=None)
            self.assertEqual(rc, 2)
            # Both dependencies ran; only the target was refused.
            self.assertEqual([c["spec_ref"] for c in captured],
                             ["spec/component/c", "spec/component/b"])
            last = json.loads(stdout.strip().splitlines()[-1])
            self.assertEqual(last["reason"], "concurrent_orchestration_running")
            self.assertEqual(last["orchestration_id"], "orch_a_live")
            self.assertEqual(last["target_spec_ref"], "spec/problem/a")

    def test_cold_closure_detects_a_run_started_after_the_closure_began(self) -> None:
        # The concurrency guard's whole point is the hours-long window a --with-deps
        # closure occupies: the competing run is far more likely to be launched DURING
        # the dependency phase than before it. A workspace scan sampled once at closure
        # start is blind to exactly that case, so the guard rescans per node.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            self._seed_diamond(repo_root)
            captured: list[dict] = []
            ran: set[str] = set()

            def fake_run_node(**kw):
                captured.append(kw)
                ran.add(kw["spec_ref"])
                if kw["spec_ref"] == "spec/component/c":
                    # Operator B starts a run of the TARGET spec while this closure is
                    # still working through its dependencies.
                    self._seed_prior_member(
                        repo_root, "orch_operator_b", "spec/problem/a",
                        status="running", driver={"pid": 4242, "verdict": "alive"})
                return 0

            def fake_ready(repo_root_, node, required_stages):
                return node["spec_ref"] in ran

            from tools.orchestration_runtime import _load_spec_catalog
            _load_spec_catalog.cache_clear()
            orig, orig_ready = run_workflow._run_node, run_workflow._dependency_node_ready
            run_workflow._run_node = fake_run_node  # type: ignore[assignment]
            run_workflow._dependency_node_ready = fake_ready  # type: ignore[assignment]
            buf = io.StringIO()
            try:
                with _forced_liveness(), redirect_stdout(buf):
                    rc = run_workflow._run_with_dependency_closure(
                        repo_root=repo_root,
                        base_env={"PATH": os.environ.get("PATH", "")},
                        target_orchestration_id="orch_target",
                        target_spec_ref="spec/problem/a",
                        target_source_dependency_ref="spec/problem/a/deps.yaml",
                        until_phase="Validate",
                        llm="claude", llm_command="claude",
                        llm_config=_sample_config("claude"), workflow_mode="dev",
                        agent_model=None, status="running", run_conductor=False,
                        resume=False, prior_orch_by_spec=None,
                        raw_argv=["spec/problem/a", "validate", "--with-deps"],
                    )
            finally:
                run_workflow._run_node = orig  # type: ignore[assignment]
                run_workflow._dependency_node_ready = orig_ready  # type: ignore[assignment]
            self.assertEqual(rc, 2)
            # Both dependencies ran; the target was refused because of the run that
            # appeared after the closure started.
            self.assertEqual([c["spec_ref"] for c in captured],
                             ["spec/component/c", "spec/component/b"])
            last = json.loads(buf.getvalue().strip().splitlines()[-1])
            self.assertEqual(last["reason"], "concurrent_orchestration_running")
            self.assertEqual(last["orchestration_id"], "orch_operator_b")

    def test_closure_is_not_blocked_by_orchestrations_this_process_drives(self) -> None:
        # Pins the WIRING of the self-exclusion at both closure call sites (dependency
        # and target): `closure_driver_identity` must reach each node's guard. Both
        # node specs already carry a `running` orchestration whose driver is this
        # process — without the identity they would probe `alive` and refuse.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            self._seed_diamond(repo_root)
            identity = run_workflow._current_driver_identity()
            self.assertIsNotNone(identity)
            own = {**identity, "recorded_at": "2026-01-01T00:00:00.000000Z"}
            self._seed_prior_member(repo_root, "orch_ours_dep", "spec/component/c",
                                    status="running", driver=own)
            self._seed_prior_member(repo_root, "orch_ours_target", "spec/problem/a",
                                    status="running", driver=own)
            rc, captured, stdout, calls = self._drive_closure_with_runtime(
                repo_root, resume=False, prior_orch_by_spec=None)
            self.assertEqual(rc, 0, stdout)
            self.assertEqual(len(captured), 3)
            self.assertNotIn("concurrent_orchestration_running", stdout)
            self.assertNotIn("prior_incomplete_orchestration", stdout)
            self.assertEqual([c for c in calls if c and c[0] == "set-status"], [])

    def test_closure_resume_terminalizes_dead_target_before_resuming(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            self._seed_diamond(repo_root)
            # A target that is warm-resumed must carry this closure's back-link and
            # matching spec, which is what makes `target_resume` true.
            self._seed_prior_member(
                repo_root, "orch_target", "spec/problem/a",
                status="running", driver={"pid": 4242, "verdict": "dead"})
            with _forced_liveness():
                rc, captured, stdout, calls = self._drive_closure_with_runtime(
                    repo_root, resume=True, prior_orch_by_spec={})
            self.assertEqual(rc, 0)
            set_status = [c for c in calls if c and c[0] == "set-status"]
            self.assertEqual(len(set_status), 1)
            self.assertEqual(
                set_status[0][set_status[0].index("--orchestration-id") + 1],
                "orch_target")
            self.assertEqual(
                set_status[0][set_status[0].index("--reason-code") + 1],
                "driver_crashed")
            self.assertTrue(captured[-1]["resume_mode"])
            self.assertEqual(captured[-1]["spec_ref"], "spec/problem/a")

    def test_cold_closure_warns_about_dead_prior_node_orchestration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            self._seed_diamond(repo_root)
            self._seed_prior_member(
                repo_root, "orch_c_dead", "spec/component/c",
                status="running", driver={"pid": 4242, "verdict": "dead"})
            with _forced_liveness():
                rc, captured, stdout, calls = self._drive_closure_with_runtime(
                    repo_root, resume=False, prior_orch_by_spec=None)
            self.assertEqual(rc, 0)
            warns = [
                json.loads(line) for line in stdout.splitlines()
                if line.strip().startswith("{")
                and json.loads(line).get("event") == "prior_incomplete_orchestration"
            ]
            self.assertEqual(len(warns), 1)
            self.assertEqual(warns[0]["orchestration_id"], "orch_c_dead")
            self.assertEqual(warns[0]["spec_ref"], "spec/component/c")
            # Warned, not blocked: every node still runs, and the cold path does not
            # terminalize anything on its own.
            self.assertEqual(len(captured), 3)
            self.assertEqual([c for c in calls if c and c[0] == "set-status"], [])


class StdoutTeeTests(unittest.TestCase):
    """Cover the host-side run-log tee added to run_workflow: stdout mirroring,
    best-effort IO suppression, attribute fall-through, and the open helper's
    success / failure (None) contract plus filename collision-safety."""

    def test_tee_mirrors_to_both_stream_and_log(self) -> None:
        terminal = io.StringIO()
        logf = io.StringIO()
        tee = run_workflow._StdoutTee(terminal, logf)
        n = tee.write("hello\n")
        self.assertEqual(n, len("hello\n"))
        self.assertEqual(terminal.getvalue(), "hello\n")
        self.assertEqual(logf.getvalue(), "hello\n")

    def test_tee_swallows_log_write_errors_without_losing_terminal(self) -> None:
        terminal = io.StringIO()

        class _BrokenLog:
            def write(self, data: str) -> int:
                raise OSError("disk full")

            def flush(self) -> None:
                raise OSError("disk full")

        tee = run_workflow._StdoutTee(terminal, _BrokenLog())
        # Must not raise, and the terminal must still receive the data.
        tee.write("payload\n")
        tee.flush()
        self.assertEqual(terminal.getvalue(), "payload\n")

    def test_tee_attribute_fall_through(self) -> None:
        # fileno() is load-bearing: subprocesses derive stdout from the parent fd.
        tee = run_workflow._StdoutTee(sys.__stdout__, io.StringIO())
        self.assertEqual(tee.fileno(), sys.__stdout__.fileno())

    def test_open_run_log_writes_unique_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            oid = "orch_log_001"
            f1 = run_workflow._open_run_log(repo_root, oid)
            f2 = run_workflow._open_run_log(repo_root, oid)
            self.assertIsNotNone(f1)
            self.assertIsNotNone(f2)
            try:
                run_logs = repo_root / "workspace" / "orchestrations" / oid / "run_logs"
                files = sorted(run_logs.glob("run_*.jsonl"))
                # Two opens against the SAME orchestration_id (the --resume case)
                # must not collide.
                self.assertEqual(len(files), 2)
                for p in files:
                    self.assertTrue(p.name.startswith("run_"))
                    self.assertTrue(p.name.endswith(".jsonl"))
            finally:
                for f in (f1, f2):
                    if f is not None:
                        f.close()

    def test_open_run_log_returns_none_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            # Make `workspace` a regular file so mkdir of the run_logs dir fails;
            # the helper must degrade to None rather than raise.
            (repo_root / "workspace").write_text("not a dir", encoding="utf-8")
            self.assertIsNone(run_workflow._open_run_log(repo_root, "orch_x"))

    def test_run_node_closes_log_and_restores_stdout_when_node_start_print_raises(
        self,
    ) -> None:
        """Regression: the tee swap + node_start print must be INSIDE the try so a
        raising print (e.g. a broken terminal pipe, which the tee does not swallow
        for the real stream) still triggers the finally — closing the log file and
        restoring stdout — instead of leaking the handle and leaving stdout
        wrapped."""

        class _TrackedLog:
            def __init__(self) -> None:
                self.closed = False

            def write(self, data: str) -> int:
                return len(data)

            def flush(self) -> None:
                pass

            def close(self) -> None:
                self.closed = True

        class _BrokenStdout:
            def write(self, data: str) -> int:
                raise BrokenPipeError("closed pipe")

            def flush(self) -> None:
                pass

        tracked = _TrackedLog()
        orig_open = run_workflow._open_run_log
        run_workflow._open_run_log = lambda *a, **k: tracked  # type: ignore[assignment]
        saved_stdout = sys.stdout
        broken = _BrokenStdout()
        with tempfile.TemporaryDirectory() as tmp:
            sys.stdout = broken  # type: ignore[assignment]
            try:
                with self.assertRaises(BrokenPipeError):
                    run_workflow._run_node(
                        repo_root=Path(tmp),
                        base_env={},
                        orchestration_id="orch_leak_001",
                        spec_ref="spec/x",
                        source_dependency_ref="spec/x/deps.yaml",
                        until_phase="compile",
                        llm="claude",
                        llm_command="claude",
                        llm_config=_sample_config("claude"),
                        workflow_mode="dev",
                        agent_model=None,
                        status="running",
                        run_conductor=False,
                        resume_mode=False,
                    )
                # stdout restored to the original stream (not left wrapped), and the
                # log file handle closed — no leak.
                self.assertIs(sys.stdout, broken)
                self.assertNotIsInstance(sys.stdout, run_workflow._StdoutTee)
                self.assertTrue(tracked.closed)
            finally:
                sys.stdout = saved_stdout
                run_workflow._open_run_log = orig_open  # type: ignore[assignment]


class StdoutFormatTests(unittest.TestCase):
    """Cover the new --stdout-format flag, the human formatter, and the
    run_logs always-full-jsonl contract."""

    def _seed(self, repo_root: Path) -> None:
        _seed_shape_expr_schema_into(repo_root)
        (repo_root / "tools").mkdir(parents=True, exist_ok=True)
        (repo_root / "workspace").mkdir(parents=True, exist_ok=True)
        (repo_root / "spec" / "problem").mkdir(parents=True, exist_ok=True)
        (repo_root / "spec" / "problem" / "test.md").write_text(
            "spec\n", encoding="utf-8"
        )
        (repo_root / "spec" / "problem" / "deps.yaml").write_text(
            "nodes: []\n", encoding="utf-8"
        )
        _seed_default_llm_config_into(repo_root)

    def _fake_runtime(self, args, *, oar: str = "orch_agent_run_fmt"):
        # Minimal fake init/preflight so main() can reach the final summary.
        if args[0] == "init":
            return run_workflow.RuntimeResult(
                payload={"status": "ok", "orchestration_agent_run_id": oar},
                raw_stdout="{}",
            )
        if args[0] == "preflight":
            return run_workflow.RuntimeResult(
                payload={
                    "status": "pass",
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                },
                raw_stdout="{}",
            )
        return run_workflow.RuntimeResult(payload={"status": "ok"}, raw_stdout="{}")

    def test_human_format_renders_node_start_and_final_summary(self) -> None:
        """In human mode the operator sees compact lines, not raw JSON, for the
        node-start announcement and the final ok summary."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed(repo_root)
            orig = run_workflow._runtime_command
            buf = io.StringIO()
            try:
                run_workflow._runtime_command = (  # type: ignore[assignment]
                    lambda root, env, args: self._fake_runtime(args))
                with redirect_stdout(buf):
                    code = run_workflow.main([
                        "spec/problem/test.md", "build",
                        "--repo-root", str(repo_root),
                        "--orchestration-id", "orch_human_fmt",
                        "--no-run-conductor",
                        "--stdout-format", "human",
                    ])
            finally:
                run_workflow._runtime_command = orig  # type: ignore[assignment]
            self.assertEqual(code, 0)
            lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
            # No JSON braces leaking onto the terminal in human mode.
            self.assertFalse(any(ln.lstrip().startswith("{") for ln in lines), lines)
            # node_start renders with the [node] prefix and the spec/until fields.
            self.assertTrue(
                any(ln.startswith("[node]") and "spec=spec/problem/test.md" in ln
                    and "until=Build" in ln for ln in lines),
                lines,
            )
            # The final ok summary renders with the [ok  ] prefix.
            self.assertTrue(any(ln.startswith("[ok") for ln in lines), lines)

    def test_jsonl_format_keeps_raw_json_on_stdout(self) -> None:
        """--stdout-format jsonl emits the raw structured payload so existing
        parsers see the same JSONL contract they always have."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed(repo_root)
            orig = run_workflow._runtime_command
            buf = io.StringIO()
            try:
                run_workflow._runtime_command = (  # type: ignore[assignment]
                    lambda root, env, args: self._fake_runtime(args))
                with redirect_stdout(buf):
                    code = run_workflow.main([
                        "spec/problem/test.md", "build",
                        "--repo-root", str(repo_root),
                        "--orchestration-id", "orch_jsonl_fmt",
                        "--no-run-conductor",
                        "--stdout-format", "jsonl",
                    ])
            finally:
                run_workflow._runtime_command = orig  # type: ignore[assignment]
            self.assertEqual(code, 0)
            # Every non-empty line must parse as JSON in jsonl mode.
            events = [json.loads(ln) for ln in buf.getvalue().splitlines() if ln.strip()]
            self.assertTrue(any(e.get("event") == "node_start" for e in events))
            self.assertEqual(events[-1].get("status"), "ok")

    def test_run_logs_always_contain_full_jsonl_regardless_of_mode(self) -> None:
        """Whichever stdout format the operator picked, the per-run jsonl file
        under workspace/orchestrations/<oid>/run_logs/ must hold the raw JSON
        payloads of every event — it is the workspace-side full-fidelity
        record."""
        for mode, oid in (("human", "orch_log_human"), ("jsonl", "orch_log_jsonl")):
            with self.subTest(mode=mode):
                with tempfile.TemporaryDirectory() as tmp:
                    repo_root = Path(tmp)
                    self._seed(repo_root)
                    orig = run_workflow._runtime_command
                    try:
                        run_workflow._runtime_command = (  # type: ignore[assignment]
                            lambda root, env, args: self._fake_runtime(args))
                        code = run_workflow.main([
                            "spec/problem/test.md", "build",
                            "--repo-root", str(repo_root),
                            "--orchestration-id", oid,
                            "--no-run-conductor",
                            "--stdout-format", mode,
                        ])
                    finally:
                        run_workflow._runtime_command = orig  # type: ignore[assignment]
                    self.assertEqual(code, 0)
                    run_logs = (
                        repo_root / "workspace" / "orchestrations" / oid
                        / "run_logs"
                    )
                    files = sorted(run_logs.glob("run_*.jsonl"))
                    self.assertEqual(len(files), 1, mode)
                    contents = files[0].read_text(encoding="utf-8")
                    events = [
                        json.loads(ln) for ln in contents.splitlines() if ln.strip()
                    ]
                    self.assertTrue(
                        any(e.get("event") == "node_start" for e in events), mode)
                    self.assertEqual(events[-1].get("status"), "ok", mode)

    def test_format_event_human_known_events(self) -> None:
        """Spot-check the human formatter for each shape the conductor and the
        run_workflow driver actually emit, so a wording change is a deliberate
        edit rather than a silent drift."""
        f = run_workflow._format_event_human
        self.assertEqual(
            f({"status": "info", "event": "node_start",
               "spec_ref": "spec/x", "until_phase": "Build",
               "orchestration_id": "orch_1", "resume": False}),
            "[node] spec=spec/x until=Build orch=orch_1",
        )
        self.assertIn(
            "[resume]",
            f({"status": "info", "event": "node_start",
               "spec_ref": "spec/x", "until_phase": "Build",
               "orchestration_id": "orch_1", "resume": True}) or "",
        )
        self.assertEqual(
            f({"status": "info", "event": "phase_start",
               "node_key": "n", "phase": "compile", "attempt": 2,
               "orchestration_id": "o"}),
            "  [phase   ] compile (attempt 2)",
        )
        self.assertEqual(
            f({"status": "info", "event": "phase_complete",
               "node_key": "n", "phase": "generate", "result": "pass",
               "elapsed_seconds": 12.34, "orchestration_id": "o"}),
            "  [phase   ] generate ok (12.34s)",
        )
        self.assertIn(
            "skipped (resumed)",
            f({"status": "info", "event": "phase_complete",
               "node_key": "n", "phase": "compile", "result": "skipped",
               "orchestration_id": "o"}) or "",
        )
        self.assertEqual(
            f({"status": "info", "event": "substep_start",
               "node_key": "n", "phase": "validate", "substep": "execute",
               "attempt": 1, "orchestration_id": "o"}),
            "    [substep] validate.execute ...",
        )
        self.assertEqual(
            f({"status": "info", "event": "substep_complete",
               "node_key": "n", "phase": "validate", "substep": "judge",
               "result": "pass", "elapsed_seconds": 4.5,
               "agent_run_id": "ar_judge", "orchestration_id": "o"}),
            "    [substep] validate.judge ok (4.5s)",
        )
        # Non-pass substep tags arid so the operator can jump to its dir.
        self.assertIn(
            "FAIL".lower() if False else "",  # placeholder to keep test stable
            f({"status": "info", "event": "substep_complete",
               "node_key": "n", "phase": "build", "substep": "step",
               "result": "fail", "elapsed_seconds": 2.0,
               "agent_run_id": "ar_x", "orchestration_id": "o"}) or "",
        )
        fail_line = f({"status": "info", "event": "substep_complete",
                       "node_key": "n", "phase": "build", "substep": "step",
                       "result": "fail", "elapsed_seconds": 2.0,
                       "agent_run_id": "ar_x", "orchestration_id": "o"})
        self.assertIn("fail", fail_line or "")
        self.assertIn("arid=ar_x", fail_line or "")
        # A transient-transport retry: the run is NOT stuck and the operator should not kill it,
        # so the wait is announced rather than left as a silent gap in the stream.
        retry_line = f({"status": "info", "event": "leaf_transient_retry",
                        "node_key": "n", "step": "compile", "substep": "verify",
                        "tag": "llm_transport_flake", "attempt": 1, "max_attempts": 3,
                        "backoff_seconds": 2.0, "dead_agent_run_id": "ar_dead",
                        "orchestration_id": "o"})
        self.assertEqual(
            retry_line,
            "    [warn   ] transient leaf failure (llm_transport_flake) in compile.verify "
            "[attempt 1/3]: retrying in 2.0s",
        )
        # Its sibling: the retry that was POSSIBLE and was refused on the wall-clock budget.
        # This one is read at the moment a phase fails closed, and it is the only record that
        # distinguishes a decline from a count exhaustion — the fail_closed reason cannot, since
        # its `[attempts=<n>]` counts repair turns and usage waits too. The two want opposite
        # remedies (`docs/RUNBOOK.md` routes on this event), so a silent drift to raw JSON here
        # costs the operator the wrong one.
        declined_line = f({"status": "info", "event": "leaf_transient_retry_declined",
                           "node_key": "n", "step": "generate", "substep": "generate",
                           "tag": "llm_transport_flake", "reason": "wall_clock_budget",
                           "attempt": 1, "elapsed_seconds": 613.6, "spent_seconds": 0.0,
                           "budget_seconds": 600.0, "dead_agent_run_id": "ar_dead",
                           "evidence": "HTTP 504 from provider: <html>",
                           "orchestration_id": "o"})
        self.assertEqual(
            declined_line,
            "    [warn   ] transient retry DECLINED (llm_transport_flake) in "
            "generate.generate: the attempt ran 613.6s and 0.0s was already spent, over the "
            "600.0s wall-clock budget — a failure this slow reproduces, so the run fails "
            "closed rather than re-launching it",
        )
        # A leaf killed at the hard per-leaf cap. This is the event that ends a 2-hour silent
        # block, and it is read at the moment a phase fails closed — it must not be the one
        # leaf-lifecycle event rendered as a raw JSON blob among the formatted lines.
        timeout_line = f({"status": "info", "event": "leaf_timeout",
                          "node_key": "n", "step": "generate", "substep": "generate",
                          "agent_run_id": "ar_dead", "backend": "claude",
                          "timeout_seconds": 7200, "elapsed_seconds": 7205.3, "leaf_exit": -9,
                          "stdout_chars": 0, "stderr_chars": 812, "orchestration_id": "o"})
        self.assertEqual(
            timeout_line,
            "    [warn   ] leaf timeout in generate.generate: no answer after 7205.3s "
            "(cap 7200s, ATMOFAB_LEAF_TIMEOUT_SECONDS) — process group killed, "
            "phase fails closed",
        )
        # Defence for a future `spawn_leaf` caller that omits (or half-fills) `timeout_context`:
        # the line must still name the step rather than reading `leaf timeout in build.:`. No
        # production path emits this today — `Build` runs its substep in-process and never
        # reaches `spawn_leaf` — which is exactly why the fallback needs a test.
        self.assertEqual(
            f({"status": "info", "event": "leaf_timeout", "node_key": "n", "step": "build",
               "substep": "", "agent_run_id": "ar_dead", "backend": "claude",
               "timeout_seconds": 7200, "elapsed_seconds": 7205.3, "leaf_exit": -9,
               "stdout_chars": 0, "stderr_chars": 812, "orchestration_id": "o"}),
            "    [warn   ] leaf timeout in build.step: no answer after 7205.3s "
            "(cap 7200s, ATMOFAB_LEAF_TIMEOUT_SECONDS) — process group killed, "
            "phase fails closed",
        )
        # An opt-in usage-limit wait: the run is deliberately parked until the reset, so the wait is
        # announced rather than left as a silent multi-hour gap the operator might kill.
        wait_line = f({"status": "info", "event": "leaf_usage_limit_wait",
                       "node_key": "n", "step": "generate", "substep": "generate",
                       "reset_epoch": 1752200000, "wait_seconds": 420.0, "wait_attempt": 1,
                       "reset_source": "scrape_human", "window": None,
                       "dead_agent_run_id": "ar_dead", "orchestration_id": "o"})
        self.assertEqual(
            wait_line,
            "    [warn   ] usage limit in generate.generate [wait 1] (source=scrape_human): "
            "waiting 420.0s for the reset, then re-launching",
        )
        # A probe-sourced instant additionally names the window it was observed on — the operator
        # can tell a host-observed reset from one scraped out of the dead leaf's own output, which
        # is the only one that can be wrong about which window stopped the run.
        self.assertEqual(
            f({"status": "info", "event": "leaf_usage_limit_wait", "node_key": "n",
               "step": "generate", "substep": "generate", "reset_epoch": 1752200000,
               "wait_seconds": 420.0, "wait_attempt": 1, "reset_source": "probe",
               "window": "session", "dead_agent_run_id": "ar_dead", "orchestration_id": "o"}),
            "    [warn   ] usage limit in generate.generate [wait 1] (source=probe/session): "
            "waiting 420.0s for the reset, then re-launching",
        )
        # Item C: a transport-substep resume announces the producer reuse, the skipped producer
        # substep, and (when it declines) the fallback to a full phase re-run.
        self.assertEqual(
            f({"status": "info", "event": "transport_substep_resume", "node_key": "n",
               "step": "compile", "resume_substep": "verify", "producer_arid": "ar_prod",
               "artifact_id": "ir_x_001", "orchestration_id": "o"}),
            "    [resume ] compile resumes at verify — producer ar_prod / ir_x_001 reused",
        )
        self.assertEqual(
            f({"status": "info", "event": "substep_resumed", "node_key": "n",
               "phase": "compile", "substep": "generate", "agent_run_id": "ar_prod",
               "orchestration_id": "o"}),
            "    [substep] compile.generate reused (resumed)",
        )
        self.assertEqual(
            f({"status": "info", "event": "transport_resume_declined", "node_key": "n",
               "reason": "artifact_dir_missing", "orchestration_id": "o"}),
            "    [warn   ] transport substep resume declined: artifact_dir_missing "
            "— full phase re-run",
        )
        # Driver-liveness gates. These four are the operator-visible output of the
        # issue-#11 recovery path, and `human` is the default stdout format, so they
        # are what an operator actually reads when a run collides with a corpse.
        self.assertEqual(
            f({"status": "info", "event": "prior_incomplete_orchestration",
               "spec_ref": "spec/x", "orchestration_id": "orch_prev",
               "liveness": "dead",
               "resume_command": "python3 tools/run_workflow.py --resume "
                                 "--orchestration-id orch_prev"}),
            "    [warn   ] prior incomplete orchestration orch_prev (driver dead) — this "
            "cold run starts over; to continue it: python3 tools/run_workflow.py "
            "--resume --orchestration-id orch_prev",
        )
        self.assertEqual(
            f({"status": "info", "event": "dead_driver_terminalized",
               "orchestration_id": "orch_prev", "prior_status": "running",
               "driver_pid": 4242, "reason_code": "driver_crashed"}),
            "    [warn   ] driver of orch_prev (pid 4242) is gone while 'running' — "
            "terminalized as fail/driver_crashed, resuming from its checkpoint",
        )
        self.assertEqual(
            f({"status": "info", "event": "resume_liveness_indeterminate",
               "orchestration_id": "orch_prev", "orchestration_status": "running"}),
            "    [warn   ] orch_prev is 'running' and its driver liveness is unknown — "
            "resuming anyway (crash reconciliations will not run)",
        )
        self.assertEqual(
            f({"status": "info", "event": "driver_interrupted",
               "orchestration_id": "orch_1", "reason_code": "driver_interrupted",
               "resume_command": "python3 tools/run_workflow.py --resume "
                                 "--orchestration-id orch_1"}),
            "    [warn   ] driver interrupted — orch_1 terminalized as "
            "cancel/driver_interrupted; resume with: python3 tools/run_workflow.py "
            "--resume --orchestration-id orch_1",
        )
        # Final ok / fail summaries.
        self.assertTrue(
            (f({"status": "ok", "orchestration_id": "orch_1",
                "workflow_status": "pass", "llm_invoked": True}) or "")
            .startswith("[ok"),
        )
        self.assertTrue(
            (f({"status": "fail", "orchestration_id": "orch_1",
                "reason": "preflight_failed", "detail": "x"}) or "")
            .startswith("[FAIL]"),
        )
        # The driver-exception backstop's envelope rides the same generic fail branch,
        # so its reason is part of the pinned operator-visible vocabulary too.
        self.assertEqual(
            f({"status": "fail", "orchestration_id": "orch_1",
               "reason": "driver_exception", "detail": "OSError: [Errno 28] boom"}),
            "[FAIL] reason=driver_exception orch=orch_1 detail=OSError: [Errno 28] boom",
        )
        # Unknown event shapes return None so the caller falls back to JSON.
        self.assertIsNone(f({"status": "info", "event": "unknown_marker"}))
        self.assertIsNone(f({"hello": "world"}))

    def test_fail_detail_elision_is_the_default_and_is_opt_outable(self) -> None:
        """The fail arm's two lossy operations — newline collapse and the 240-char head cut —
        were unpinned, which is what let `elide_detail` be added without any test noticing
        either could change. In-run they are correct (the run log holds the untouched JSON);
        outside a node run, where `_emit_unlogged_event` opts out, they would destroy the
        remedy the message exists to deliver. Pin both directions."""
        f = run_workflow._format_event_human
        # Leading whitespace on purpose: the lossless arm `rstrip()`s rather than
        # `strip()`s, and a real remedy is indented (`llm_config_default_missing` puts
        # its `cp` lines under two spaces). Without a fixture that HAS leading
        # whitespace, `rstrip` -> `strip` is an undetectable mutation.
        detail = "  first line\nsecond line\n" + ("x" * 400)
        elided = f({"status": "fail", "reason": "r", "detail": detail})
        # Default: one line, and cut at exactly 240 characters of the collapsed text.
        collapsed = detail.replace("\n", " ").strip()
        self.assertEqual(elided, f"[FAIL] reason=r detail={collapsed[:240]}...")
        self.assertNotIn("\n", elided or "")
        # The cut is a HEAD cut of 240 chars, not "some shortening": the 241st character of
        # the collapsed detail is absent and the 240th is present.
        self.assertIn(collapsed[:240], elided or "")
        self.assertNotIn(collapsed[:241], elided or "")
        # Opt-out: newlines survive, nothing is cut, and only trailing whitespace goes.
        lossless = f({"status": "fail", "reason": "r", "detail": detail + "\n\n"},
                     elide_detail=False)
        self.assertEqual(lossless, f"[FAIL] reason=r detail={detail}")
        self.assertIn("\nsecond line\n", lossless or "")
        self.assertTrue((lossless or "").endswith("x" * 400))
        # ...only TRAILING whitespace: the indent that structures a multi-line remedy is
        # part of the message. (`strip()` here would still satisfy every assert above.)
        self.assertIn("detail=  first line", lossless or "")
        # `elide_detail` is keyword-only and touches the fail arm ONLY: every other event
        # renders identically under both values.
        info = {"status": "info", "event": "phase_start", "phase": "compile", "attempt": 1}
        self.assertEqual(f(info), f(info, elide_detail=False))
        ok = {"status": "ok", "orchestration_id": "o", "workflow_status": "pass"}
        self.assertEqual(f(ok), f(ok, elide_detail=False))
        with self.assertRaises(TypeError):
            f({"status": "fail", "reason": "r"}, False)  # type: ignore[misc]
        # `docs_ref` survives the render. Every other extra key is dropped because the
        # detail restates it; this one names the section that fixes the failure and is
        # nowhere in the detail, so a rendered line without it is less useful than the
        # raw JSON it replaced.
        self.assertEqual(
            f({"status": "fail", "reason": "missing_required_cli_tools",
               "detail": "missing tools: jq", "missing": ["jq"],
               "required": ["jq", "git"], "docs_ref": "docs/RUNBOOK.md#0-1"}),
            "[FAIL] reason=missing_required_cli_tools detail=missing tools: jq "
            "docs_ref=docs/RUNBOOK.md#0-1",
        )


class StartupEnvelopeStdoutFormatTests(unittest.TestCase):
    """Issue #40: `_run_main`'s startup rejections are emitted before `_run_node` installs
    the stdout tee, so until now they ignored `--stdout-format` entirely and printed raw
    JSON under the default (`human`) — worst for the most likely first-run failure, whose
    multi-line `cp` remedy arrived as `\\n` escapes inside one unreadable line.

    These envelopes also have no second copy: `run_logs/run_*.jsonl` is opened inside
    `_run_node`, which has not run. Human rendering here is therefore lossless."""

    def _seed(self, repo_root: Path) -> None:
        """An operator's checkout: samples in the tree, spec + canonical schema present.
        Deliberately WITHOUT `./llm.yaml` — that absence is the headline failure."""
        _seed_shape_expr_schema_into(repo_root)
        for d in ("tools", "workspace", "spec/problem"):
            (repo_root / d).mkdir(parents=True, exist_ok=True)
        (repo_root / "spec" / "problem" / "test.md").write_text("spec\n", encoding="utf-8")
        (repo_root / "spec" / "problem" / "deps.yaml").write_text(
            "nodes: []\n", encoding="utf-8")
        (repo_root / "docs" / "examples").mkdir(parents=True, exist_ok=True)
        for name in lc.SAMPLE_CONFIG_NAMES:
            shutil.copy(SAMPLE_DIR / name, repo_root / "docs" / "examples" / name)

    def _main(self, argv: list[str]) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = run_workflow.main(argv)
        return code, out.getvalue()

    def test_the_missing_default_config_remedy_survives_human_mode_intact(self) -> None:
        """The issue's headline case, driven through the REAL writer
        (`resolve_default_config_path`) rather than a fake short message — a fake would make
        every assertion below vacuous, since nothing short can be truncated."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed(repo_root)
            code, out = self._main([
                "spec/problem/test.md", "build",
                "--repo-root", str(repo_root),
                "--orchestration-id", "orch_human_startup",
                "--no-run-conductor",
                # NO --stdout-format: this is what an operator's first run looks like.
            ])
        self.assertEqual(code, 2)
        self.assertTrue(
            out.startswith("[FAIL] reason=invalid_startup_input "),
            f"startup rejection must be rendered, not raw JSON; got:\n{out}")
        # The bug symptom, both halves: no raw-JSON line, and no `\n` ESCAPE standing in
        # for the line breaks of the remedy.
        self.assertNotIn("\\n", out)
        for line in out.splitlines():
            self.assertFalse(
                line.startswith("{"),
                f"a JSON envelope leaked into human-mode stdout:\n{out}")
        # The remedy is multi-line and every offered `cp` is present — asserted at BOTH ends
        # of the message, so a future re-truncation (head OR tail) cannot pass this test.
        self.assertIn("\n", out)
        first_cp = (f"cp {repo_root / 'docs' / 'examples' / 'llm_claude.example.yaml'} "
                    f"{repo_root / 'llm.yaml'}")
        second_cp = (f"cp {repo_root / 'docs' / 'examples' / 'llm_codex.example.yaml'} "
                     f"{repo_root / 'llm.yaml'}")
        last_cp = (f"cp {repo_root / 'docs' / 'examples' / lc.SAMPLE_CONFIG_NAMES[-1]} "
                   f"{repo_root / 'llm.yaml'}")
        self.assertIn(first_cp, out)
        self.assertIn(second_cp, out)
        self.assertEqual(out.rstrip("\n").splitlines()[-1].strip(), last_cp)
        # ...and the head of the message, which the `...` cut would have kept while dropping
        # everything above, is there too.
        self.assertIn("llm_config_default_missing", out)
        self.assertIn(str(repo_root / "llm.yaml"), out)

    def test_a_second_startup_site_is_rendered_too(self) -> None:
        """`no_resumable_orchestration` comes from a different emission site on a different
        branch (the resume gate, long before the config is resolved). Pinning it keeps the
        fix from being one path's special case."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed(repo_root)
            shutil.copy(SAMPLE_DIR / "llm_claude.example.yaml", repo_root / "llm.yaml")
            code, out = self._main([
                "--repo-root", str(repo_root), "--resume", "--no-run-conductor",
            ])
        self.assertEqual(code, 2)
        self.assertEqual(
            out.strip(),
            "[FAIL] reason=no_resumable_orchestration detail=no orchestration found "
            "under workspace/orchestrations to resume")

    def test_the_same_site_still_emits_parsable_jsonl_when_asked(self) -> None:
        """The machine contract is unchanged: `--stdout-format jsonl` still yields the same
        payload, with the detail carrying the full multi-line message.

        Not a duplicate of `LlmConfigStartupTests.test_a_missing_default_stops_the_run_...`
        (which pins the envelope's CONTENT): what is pinned here is parity between the two
        formats over one invocation that differs from the human test above by the flag
        alone — the property this change could break in either direction."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed(repo_root)
            code, out = self._main([
                "spec/problem/test.md", "build",
                "--repo-root", str(repo_root),
                "--orchestration-id", "orch_jsonl_startup",
                "--no-run-conductor",
                "--stdout-format", "jsonl",
            ])
        self.assertEqual(code, 2)
        payload = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(payload["status"], "fail")
        self.assertEqual(payload["reason"], "invalid_startup_input")
        self.assertIn("llm_config_default_missing", payload["detail"])
        self.assertIn(
            f"cp {repo_root / 'docs' / 'examples' / 'llm_claude.example.yaml'} "
            f"{repo_root / 'llm.yaml'}", payload["detail"])
        self.assertIn("\n", payload["detail"])

    def test_a_dead_stdout_does_not_turn_a_refusal_into_a_traceback(self) -> None:
        """`python3 tools/run_workflow.py ... | head -5` is a normal way to read a refusal,
        and more likely now that a human-mode envelope spans several lines. The reader
        exits, the write breaks, and because the helper flushes eagerly the error surfaces
        inside the failure path instead of at interpreter shutdown — turning a clean
        refusal into a traceback. Emitting must survive that."""
        # Both halves, because a real `TextIOWrapper` on a pipe buffers the write and
        # raises from the FLUSH — which is the `flush=True` this is about. A fake that
        # only breaks `write` would stay green against a `print(line)` +
        # unguarded `sys.stdout.flush()` refactor, i.e. against the actual bug.
        class _DeadOnWrite(io.StringIO):
            def write(self, s: str) -> int:
                raise BrokenPipeError(32, "Broken pipe")

        class _DeadOnFlush(io.StringIO):
            def flush(self) -> None:
                raise BrokenPipeError(32, "Broken pipe")

        for dead in (_DeadOnWrite, _DeadOnFlush):
            for fmt in ("human", "jsonl"):
                with redirect_stdout(dead()):
                    run_workflow._emit_unlogged_event(
                        {"status": "fail", "reason": "invalid_startup_input",
                         "detail": "x\ny"}, fmt)
        # Only THIS error is swallowed: a stdout that fails for another reason is a real
        # fault and must not be hidden behind the same clause.
        class _BrokenStdout(io.StringIO):
            def write(self, s: str) -> int:
                raise OSError(28, "No space left on device")

        with self.assertRaises(OSError):
            with redirect_stdout(_BrokenStdout()):
                run_workflow._emit_unlogged_event({"status": "fail", "reason": "r"}, "human")

    def test_a_real_dead_reader_gets_no_traceback_from_the_driver(self) -> None:
        """The in-process fakes above pin the clause; this pins the thing itself. A real
        pipe whose read end is closed before the child writes is the only faithful
        version of "the reader is already gone" — buffering, the flush boundary and
        CPython's shutdown handling all participate, and none of them is modelled by a
        StringIO subclass."""
        read_end, write_end = os.pipe()
        os.close(read_end)                       # the reader is gone before we start
        try:
            proc = subprocess.run(
                [sys.executable, str(Path(run_workflow.__file__).resolve()),
                 "spec/problem/definitely_missing.md", "build",
                 "--repo-root", str(REPO_ROOT), "--no-run-conductor"],
                stdout=write_end, stderr=subprocess.PIPE, timeout=60)
        finally:
            os.close(write_end)
        self.assertNotIn(
            "Traceback", proc.stderr.decode("utf-8", "replace"),
            "a dead stdout must not turn a startup refusal into a traceback; stderr:\n"
            + proc.stderr.decode("utf-8", "replace"))

    def test_run_main_writes_stdout_only_through_the_emit_helper(self) -> None:
        """Structural guard for the whole class of sites: any NEW startup envelope added to
        `_run_main` with a bare `print(json.dumps(...))` reintroduces the bug for that path,
        and no behaviour test would cover it. Only 2 of the 17 emissions are pinned
        behaviourally, so this guard carries the other 15 — which is why it checks the
        exact shape of the emission set rather than a lower bound.

        The no-raw-write half covers every function that emits outside a node run, not
        just `_run_main`: the closure driver and the three gate emitters shared between
        the two paths stand on exactly the same rule ("the elision is a property of the
        emission point"), and a bare print added to one of THEM reintroduces the leak for
        the `--with-deps` path — the divergence this branch's own review caught. They
        contain no such write today, so the assertion costs nothing to hold.

        Walked as an AST, not grepped: the sites are written as a multi-line
        `print(\\n    json.dumps(` that plain text search misses."""
        import ast
        import inspect

        def calls_in(func) -> list:  # type: ignore[no-untyped-def]
            tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
            return [node for node in ast.walk(tree) if isinstance(node, ast.Call)]

        untee_d_emitters = {
            "_run_main": run_workflow._run_main,
            "_run_with_dependency_closure": run_workflow._run_with_dependency_closure,
            "_cold_start_running_guard": run_workflow._cold_start_running_guard,
            "_warm_resume_liveness_guard": run_workflow._warm_resume_liveness_guard,
            "_terminalize_dead_driver": run_workflow._terminalize_dead_driver,
        }
        for name, func in untee_d_emitters.items():
            calls = calls_in(func)
            prints = [
                node.lineno for node in calls
                if isinstance(node.func, ast.Name) and node.func.id == "print"
            ]
            self.assertEqual(
                prints, [],
                f"{name} must emit stdout only via _emit_unlogged_event (which honors "
                f"--stdout-format); bare print() at {name}-relative line(s) {prints}")
            # `print` is not the only way to write a line. A `sys.stdout.write(json.dumps(...))`
            # bypasses the helper just as completely while leaving the assertion above green.
            # Scoped to STDOUT: `_run_main`'s one `sys.stderr.write` is the deliberate advisory
            # about an ignored `--llm-config` on resume, which is not an envelope.
            stdout_writes = [
                node.lineno for node in calls
                if isinstance(node.func, ast.Attribute)
                and node.func.attr in {"write", "writelines"}
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "stdout"
            ]
            self.assertEqual(
                stdout_writes, [],
                f"{name} must not write to sys.stdout directly; every envelope goes "
                f"through _emit_unlogged_event — lines {stdout_writes}")
        # The exact-count half stays scoped to `_run_main`: it is what keeps the 15
        # sites no behaviour test reaches from quietly disappearing, and a count over
        # the closure driver would just be churn on every future closure event.
        calls = calls_in(run_workflow._run_main)
        # ...the helper is actually used there, so the assertions above cannot be
        # satisfied by a `_run_main` that simply stopped emitting anything.
        helper_calls = [
            node for node in calls
            if isinstance(node.func, ast.Name) and node.func.id == "_emit_unlogged_event"
        ]
        # EXACT, not a lower bound: a lower bound is silently satisfied while a site is
        # rerouted to some other emitter (verified — swapping one site passed `>= 14`),
        # and a site that stops emitting is exactly how a rejection becomes silent.
        # 17 -> 18: the missing-Python-package startup rejection (the Fortran structure front
        # end's packages, checked at launch so their absence is not discovered part-way into a
        # billed run).
        # 18 -> 19: the missing-host-tool startup rejection (issue #109 — the `static lint` tool
        # and the toolchain the run's own axis selection implies, for the same reason and one
        # phase earlier than where they used to surface).
        # 19 -> 20: the same family's VERSION half (issue #111) — a required tool that is present
        # but of a build this repository has not measured its gate's rule set against.
        self.assertEqual(len(helper_calls), 20)
        # Every one of them is handed the parsed flag — a hardcoded "jsonl"/"human" at any
        # site would silently pin that site to one format.
        for call in helper_calls:
            self.assertEqual(len(call.args), 2, ast.dump(call))
            fmt = call.args[1]
            self.assertTrue(
                isinstance(fmt, ast.Attribute)
                and fmt.attr == "stdout_format"
                and isinstance(fmt.value, ast.Name)
                and fmt.value.id == "args",
                f"startup emit must pass args.stdout_format, got {ast.dump(fmt)}")

    def test_a_closure_gate_envelope_keeps_the_remedy_its_detail_carries(self) -> None:
        """The closure driver emits the SAME refusals as the startup path — the payload
        builders (`_concurrent_cold_start_envelope`, `_llm_config_legacy_pin_rejection`,
        `_llm_config_resume_rejection`) and two of the emitters (`_cold_start_running_guard`,
        `_terminalize_dead_driver`) are shared — and a closure gate that refuses before the
        first node runs has no `run_logs/` copy either. Eliding there and not here would
        mean the identical refusal printing its remedy or dropping it according to whether
        `--with-deps` was passed."""
        envelope = run_workflow._concurrent_cold_start_envelope("spec/problem/sw2d/spec.md")
        # The premise: this detail is long enough for the cut to bite, and the remedy sits
        # beyond it. Without both, the assertions below would hold for a renderer that
        # still elides — which is how this test would go quietly vacuous.
        self.assertGreater(len(envelope["detail"]), 240)
        remedy = "--resume --orchestration-id"
        self.assertIn(remedy, envelope["detail"][240:],
                      "the remedy must sit BEYOND the cut, or this test proves nothing")
        buf = io.StringIO()
        with redirect_stdout(buf):
            run_workflow._emit_unlogged_event(envelope, "human")
        rendered = buf.getvalue()
        self.assertTrue(rendered.startswith("[FAIL] reason=concurrent_orchestration_running"))
        self.assertIn(remedy, rendered)
        self.assertNotIn("...", rendered)
        # ...and the same call still emits the raw payload under jsonl.
        buf = io.StringIO()
        with redirect_stdout(buf):
            run_workflow._emit_unlogged_event(envelope, "jsonl")
        self.assertEqual(json.loads(buf.getvalue()), envelope)
        # The `--with-deps` path wraps the same envelope with closure context before
        # emitting it (`_run_with_dependency_closure`); that must not reintroduce the cut.
        buf = io.StringIO()
        with redirect_stdout(buf):
            run_workflow._emit_unlogged_event(
                {**envelope, "dependency_runs": [], "target_spec_ref": "spec/x"}, "human")
        self.assertIn(remedy, buf.getvalue())


class ReasonDetailTruncationTests(unittest.TestCase):
    """`--reason-detail` crosses a subprocess argv boundary, where Linux caps one
    element at MAX_ARG_STRLEN. The cut therefore has to be bounded — and has to keep
    the part that says WHY, which for a `_runtime_command` RuntimeError is the tail."""

    def test_a_detail_at_or_under_the_limit_is_passed_through_byte_identical(self) -> None:
        # The `<=` boundary specifically: a detail of exactly the limit needs no
        # eliding, and eliding it anyway destroys three characters of a message that
        # fitted, for nothing.
        f = run_workflow._truncate_reason_detail
        limit = run_workflow._REASON_DETAIL_LIMIT
        for length in (0, 1, limit - 1, limit):
            with self.subTest(length=length):
                detail = "y" * length
                self.assertEqual(f(detail), detail)

    def test_an_oversized_detail_keeps_both_ends_within_the_limit(self) -> None:
        f = run_workflow._truncate_reason_detail
        limit = run_workflow._REASON_DETAIL_LIMIT
        for length in (limit + 1, limit * 5, 200_000):
            with self.subTest(length=length):
                detail = "WHICH-COMMAND" + "z" * length + "WHY-IT-DIED"
                out = f(detail)
                self.assertEqual(len(out), limit)
                self.assertTrue(out.startswith("WHICH-COMMAND"))
                self.assertTrue(out.endswith("WHY-IT-DIED"))
                self.assertIn("...", out)

    def test_the_cut_is_by_codepoint_so_a_multibyte_detail_stays_readable(self) -> None:
        # Slicing bytes would split a character and hand the runtime a mojibake
        # argument; 200 codepoints is at most 800 bytes, far under MAX_ARG_STRLEN.
        f = run_workflow._truncate_reason_detail
        out = f("日" * 5_000)
        self.assertEqual(len(out), run_workflow._REASON_DETAIL_LIMIT)
        self.assertEqual(out.replace("...", ""), "日" * (len(out) - 3))


class SubstepEventTests(unittest.TestCase):
    """The conductor must surface per-substep activity (start/complete) so the
    host event stream is informative even during long substep loops."""

    def test_run_phase_emits_substep_start_and_complete(self) -> None:
        import tools.workflow_conductor as wc

        # Drive run_phase via a minimal stub conductor. We only need to verify
        # that the substep_start/substep_complete pair fire for every substep
        # of a phase, in order, with the phase + substep labels and a result.
        captured: list[dict[str, object]] = []

        class _Stub(wc.Conductor):
            def __init__(self):
                pass

            orchestration_id = "orch_sub"
            orchestration_agent_run_id = "orch_agent_run"
            workflow_mode = "dev"
            # `__init__` is bypassed, so the leaf-model authority is supplied directly.
            llm_config = _sample_config("claude")

            def emit(self, event, **fields):
                captured.append({"event": event, **fields})

            def check_step_completed(self, *_a, **_k):
                return None

            def workflow_launch_check(self, *_a, **_k):
                return None

            def _ensure_fresh_producer_id(self, *_a, **_k):
                return None

            def _write_lineage(self, *_a, **_k):
                return ()

            def _conductor_authors_makefile(self, *_a, **_k):
                return False

            def _judge_pre_spawn_dag_block(self, *_a, **_k):
                return None

            def run_substep(self, refs, phase, substep, repair=None,
                            resolved_dependencies=(), dependency_surface=()):
                return wc.SubstepOutcome(
                    agent_run_id=f"ar_{phase}_{substep or 'step'}",
                    status="pass", output_refs=[], leaf_returncode=0,
                )

            def write_step_result(self, *_a, **_k):
                return None

            def _resolve_exe_name(self, *_a, **_k):
                return None

        stub = _Stub()
        # Validate uses four substeps (pre_judge, execute, judge, post_judge).
        refs = wc.NodeRefs(
            node_key="component/x@0.1.0", spec_path="spec/x",
            ir_id="ir1", pipeline_id="pl1",
            source_id="src", binary_id="bin", run_id="r1", source_binary_id="bin",
        )
        outcome = stub.run_phase(refs, "validate")
        self.assertEqual(outcome.status, "pass")
        starts = [e for e in captured if e["event"] == "substep_start"]
        completes = [e for e in captured if e["event"] == "substep_complete"]
        self.assertEqual([(e["phase"], e["substep"]) for e in starts],
                         [("validate", "pre_judge"), ("validate", "execute"),
                          ("validate", "judge"), ("validate", "post_judge")])
        self.assertEqual([(e["phase"], e["substep"], e["result"]) for e in completes],
                         [("validate", "pre_judge", "pass"),
                          ("validate", "execute", "pass"),
                          ("validate", "judge", "pass"),
                          ("validate", "post_judge", "pass")])
        # Every complete carries a numeric elapsed_seconds and the substep's arid.
        for e in completes:
            self.assertIsInstance(e["elapsed_seconds"], (int, float))
            self.assertTrue(str(e["agent_run_id"]).startswith("ar_validate_"))

    def test_run_phase_build_emits_step_label_for_none_substep(self) -> None:
        """Build's SUBSTEPS == (None,) — the host event stream must still label
        the substep field so the operator gets a readable line. We render
        ``None`` as ``"step"`` (the agent_role of the single child)."""
        import tools.workflow_conductor as wc

        captured: list[dict[str, object]] = []

        class _Stub(wc.Conductor):
            def __init__(self):
                pass

            orchestration_id = "orch_sub_build"
            orchestration_agent_run_id = "orch_agent_run"
            workflow_mode = "dev"
            # `__init__` is bypassed, so the leaf-model authority is supplied directly.
            llm_config = _sample_config("claude")

            def emit(self, event, **fields):
                captured.append({"event": event, **fields})

            def check_step_completed(self, *_a, **_k):
                return None

            def workflow_launch_check(self, *_a, **_k):
                return None

            def _ensure_fresh_producer_id(self, *_a, **_k):
                return None

            def _write_lineage(self, *_a, **_k):
                return ()

            def _conductor_authors_makefile(self, *_a, **_k):
                return False

            def run_substep(self, refs, phase, substep, repair=None,
                            resolved_dependencies=(), dependency_surface=()):
                return wc.SubstepOutcome(
                    agent_run_id="ar_build", status="pass",
                    output_refs=[], leaf_returncode=0,
                )

            def write_step_result(self, *_a, **_k):
                return None

            def _resolve_exe_name(self, *_a, **_k):
                return None

        stub = _Stub()
        refs = wc.NodeRefs(
            node_key="component/x@0.1.0", spec_path="spec/x",
            ir_id="ir1", pipeline_id="pl1",
            source_id="src", binary_id="bin", run_id="r1", source_binary_id="bin",
        )
        outcome = stub.run_phase(refs, "build")
        self.assertEqual(outcome.status, "pass")
        starts = [e for e in captured if e["event"] == "substep_start"]
        self.assertEqual(starts[0]["substep"], "step")


_HAS_PROC = Path("/proc/self/stat").exists()


def _unused_pid() -> int:
    """A pid that cannot name a live process (one past the kernel's pid ceiling)."""
    try:
        return int(Path("/proc/sys/kernel/pid_max").read_text(encoding="utf-8").strip()) + 1
    except (OSError, ValueError):
        return 4194305


@unittest.skipUnless(_HAS_PROC, "driver liveness probing requires Linux /proc")
class DriverLivenessProbeTests(unittest.TestCase):
    """`_probe_driver_liveness` — the read-only classifier behind resume/cold gating.

    Fail directions are asymmetric on purpose: only an unambiguous `dead` unblocks a
    resume and only an unambiguous `alive` blocks a cold run, so every test here that
    ends in `unknown` is pinning a case that must preserve the pre-existing behavior.
    """

    def _self_identity(self) -> dict:
        identity = run_workflow._current_driver_identity()
        self.assertIsNotNone(identity)
        return dict(identity)

    def test_self_pid_with_matching_ticks_is_alive(self) -> None:
        self.assertEqual(
            run_workflow._probe_driver_liveness({"driver": self._self_identity()}),
            "alive",
        )

    def test_pid_reuse_is_dead_not_alive(self) -> None:
        # Same (live) pid, different start time = the recorded process is gone and its
        # pid was recycled. Reporting `alive` here would wedge recovery forever.
        identity = self._self_identity()
        identity["pid_start_ticks"] = str(int(identity["pid_start_ticks"]) + 1)
        self.assertEqual(run_workflow._probe_driver_liveness({"driver": identity}), "dead")

    def test_absent_pid_is_dead(self) -> None:
        identity = self._self_identity()
        identity["pid"] = _unused_pid()
        self.assertEqual(run_workflow._probe_driver_liveness({"driver": identity}), "dead")

    def test_absent_pid_is_unknown_without_proven_visibility(self) -> None:
        # "Absent from /proc" only proves death where the entry WOULD have been
        # visible. PID numbers are namespace-local, and a `hidepid` mount hides other
        # users' entries — either way a live driver would be read as dead and
        # auto-terminalized under load. Both facts are recorded at capture time.
        base = self._self_identity()
        base["pid"] = _unused_pid()
        self.assertEqual(base.get("pid_ns"), os.stat("/proc/self/ns/pid").st_ino)
        self.assertEqual(base.get("uid"), os.getuid())
        for label, driver in (
            ("no pid_ns recorded (a pre-field block)",
             {k: v for k, v in base.items() if k != "pid_ns"}),
            ("no uid recorded",
             {k: v for k, v in base.items() if k != "uid"}),
            ("a different PID namespace", {**base, "pid_ns": base["pid_ns"] + 1}),
            ("a different uid", {**base, "uid": base["uid"] + 1}),
            ("pid_ns of the wrong type", {**base, "pid_ns": str(base["pid_ns"])}),
        ):
            with self.subTest(case=label):
                self.assertEqual(
                    run_workflow._probe_driver_liveness({"driver": driver}), "unknown")

    def test_recorded_int_match_refuses_bools_and_unreadable_locals(self) -> None:
        # `True == 1` in Python, so a corrupt `"uid": true` would otherwise match a
        # real uid of 1 and be read as proof that an absent /proc entry means death.
        match = run_workflow._matches_recorded_int
        self.assertTrue(match(4026532221, 4026532221))
        self.assertFalse(match(True, 1))
        self.assertFalse(match(False, 0))
        self.assertFalse(match(1, None))          # local value unreadable
        self.assertFalse(match(None, 1))          # nothing recorded
        self.assertFalse(match("1", 1))           # recorded as a string
        # `1.0 == 1` too, so a JSON number that arrived as a float must not match.
        self.assertFalse(match(1.0, 1))
        self.assertFalse(match(2, 1))

    def test_read_verdicts_are_gated_on_observability_too(self) -> None:
        # Having READ an entry is not proof that it is the recorded process: it proves
        # the pid NUMBER resolves in our numbering. Across PID namespaces the same
        # number names a different process whose start ticks differ — which the
        # PID-reuse branch would otherwise call proof of death, terminalizing a live
        # driver. `hostname` and `boot_id` are not namespaced, so neither earlier guard
        # catches it. Both post-read `dead` verdicts are therefore gated as well.
        full = self._self_identity()
        pre_field = {k: v for k, v in full.items() if k not in ("pid_ns", "uid")}
        foreign_ns = {**full, "pid_ns": full["pid_ns"] + 1}

        def with_ticks(driver: dict, delta: int) -> dict:
            return {**driver,
                    "pid_start_ticks": str(int(driver["pid_start_ticks"]) + delta)}

        # PID reuse: proof only when the pid is ours to read.
        self.assertEqual(
            run_workflow._probe_driver_liveness({"driver": with_ticks(full, 1)}), "dead")
        for label, driver in (("pre-field block", pre_field),
                              ("foreign namespace", foreign_ns)):
            with self.subTest(case=f"pid reuse / {label}"):
                self.assertEqual(
                    run_workflow._probe_driver_liveness({"driver": with_ticks(driver, 1)}),
                    "unknown")

        # Zombie state: same gating.
        original = run_workflow._read_proc_stat
        try:
            for label, driver, expected in (
                ("observable", full, "dead"),
                ("pre-field block", pre_field, "unknown"),
                ("foreign namespace", foreign_ns, "unknown"),
            ):
                with self.subTest(case=f"zombie / {label}"):
                    run_workflow._read_proc_stat = (  # type: ignore[assignment]
                        lambda pid, _t=driver["pid_start_ticks"]: ("Z", _t))
                    self.assertEqual(
                        run_workflow._probe_driver_liveness({"driver": driver}), expected)
        finally:
            run_workflow._read_proc_stat = original  # type: ignore[assignment]

        # A reboot is the one `dead` that needs no entry at all: `boot_id` is not
        # namespaced, so a mismatch proves it outright and stays ungated.
        for label, driver in (("pre-field block", pre_field),
                              ("foreign namespace", foreign_ns)):
            with self.subTest(case=f"reboot / {label}"):
                rebooted = {**driver, "boot_id": "00000000-0000-0000-0000-000000000000",
                            "pid": _unused_pid()}
                self.assertEqual(
                    run_workflow._probe_driver_liveness({"driver": rebooted}), "dead")

        # A live driver is still `alive` regardless: that verdict blocks rather than
        # unblocks, so it is the conservative direction and needs no gate.
        self.assertEqual(
            run_workflow._probe_driver_liveness({"driver": pre_field}), "alive")

    @unittest.skipUnless(
        Path("/proc/sys/kernel/random/boot_id").exists(),
        "boot_id comparison requires a readable /proc/sys/kernel/random/boot_id",
    )
    def test_boot_id_mismatch_is_dead(self) -> None:
        # Start ticks are measured since boot, so after a reboot the same pid can
        # legitimately carry the same ticks — the boot id is what rules that out.
        # On a host where boot_id is masked the probe answers `unknown` instead, which
        # is why this case is gated on the file actually being readable.
        identity = self._self_identity()
        identity["boot_id"] = "00000000-0000-0000-0000-000000000000"
        self.assertEqual(run_workflow._probe_driver_liveness({"driver": identity}), "dead")

    def test_zombie_process_is_dead_not_alive(self) -> None:
        # A process that exited but has not been reaped keeps its /proc entry AND its
        # start ticks, so a pid+ticks-only probe calls the corpse alive. That verdict
        # is the worst one available: it makes the resume gate refuse recovery and the
        # cold gate refuse a fresh run, locking the spec harder than issue #11 did.
        pid = os.fork()
        if pid == 0:  # pragma: no cover - child never returns to the test runner
            os._exit(0)
        try:
            deadline = time.monotonic() + 5.0
            state = ""
            while time.monotonic() < deadline:
                stat = run_workflow._read_proc_stat(pid)
                if stat is not None and stat[0] in run_workflow._DEAD_PROC_STATES:
                    state = stat[0]
                    break
                time.sleep(0.02)
            self.assertIn(state, run_workflow._DEAD_PROC_STATES,
                          "child did not reach a zombie state")
            identity = self._self_identity()
            identity["pid"] = pid
            identity["pid_start_ticks"] = run_workflow._read_proc_starttime(pid)
            self.assertEqual(
                run_workflow._probe_driver_liveness({"driver": identity}), "dead")
        finally:
            os.waitpid(pid, 0)

    def test_every_dead_proc_state_classifies_dead(self) -> None:
        # A fork can only produce `Z`; `X`/`x` (dead / exiting) are the other half of
        # the contract and are unreachable from a test process, so they are pinned
        # against the parsed state directly.
        identity = self._self_identity()
        original = run_workflow._read_proc_stat
        try:
            # Literal, not `_DEAD_PROC_STATES`: iterating the constant under test makes
            # the assertion self-referential — shrinking the set would shrink the loop
            # and the test would still pass.
            for state in ("Z", "X", "x"):
                with self.subTest(state=state):
                    run_workflow._read_proc_stat = (  # type: ignore[assignment]
                        lambda pid, _s=state: (_s, identity["pid_start_ticks"]))
                    self.assertEqual(
                        run_workflow._probe_driver_liveness({"driver": identity}), "dead")
            # A live driver may legitimately sit in states other than R/S — a stopped
            # (`T`) or uninterruptible (`D`) process is still there and must stay alive.
            for state in ("R", "S", "D", "T", "t", "I"):
                with self.subTest(state=state):
                    run_workflow._read_proc_stat = (  # type: ignore[assignment]
                        lambda pid, _s=state: (_s, identity["pid_start_ticks"]))
                    self.assertEqual(
                        run_workflow._probe_driver_liveness({"driver": identity}), "alive")
        finally:
            run_workflow._read_proc_stat = original  # type: ignore[assignment]

    def test_unreadable_boot_id_is_unknown(self) -> None:
        # A meta recorded with a boot_id on a host where boot_id is now unreadable
        # (masked /proc, some containers) cannot prove or disprove a reboot.
        identity = self._self_identity()
        self.assertIn("boot_id", identity)
        original = run_workflow._read_boot_id
        run_workflow._read_boot_id = lambda: None  # type: ignore[assignment]
        try:
            self.assertEqual(
                run_workflow._probe_driver_liveness({"driver": identity}), "unknown")
        finally:
            run_workflow._read_boot_id = original  # type: ignore[assignment]

    def test_unreadable_stat_for_an_existing_pid_is_unknown(self) -> None:
        # The pid exists but its stat cannot be read (hardened /proc, or the process
        # exited between the two syscalls). Answering `dead` here would terminalize a
        # possibly-live run; answering `alive` would block recovery. Neither is proven.
        identity = self._self_identity()
        original = run_workflow._read_proc_stat
        run_workflow._read_proc_stat = lambda pid: None  # type: ignore[assignment]
        try:
            self.assertEqual(
                run_workflow._probe_driver_liveness({"driver": identity}), "unknown")
        finally:
            run_workflow._read_proc_stat = original  # type: ignore[assignment]

    def test_absent_proc_filesystem_is_unknown(self) -> None:
        identity = self._self_identity()
        original = run_workflow.Path.is_dir
        run_workflow.Path.is_dir = lambda self: False  # type: ignore[assignment]
        try:
            self.assertEqual(
                run_workflow._probe_driver_liveness({"driver": identity}), "unknown")
        finally:
            run_workflow.Path.is_dir = original  # type: ignore[assignment]

    def test_no_recorded_hostname_never_yields_dead(self) -> None:
        # `hostname` is omitted only when `socket.gethostname()` raises, so a block can
        # legitimately lack it — and then nothing establishes that it was written on
        # THIS machine. Every `dead` verdict reasons from local evidence (this /proc,
        # this boot_id), so all of them must degrade to `unknown`: a live driver on
        # another host reaching a shared workspace would otherwise be terminalized.
        # `pid_ns` cannot stand in for the hostname — the initial PID namespace inode
        # is a per-kernel constant, so two ordinary hosts agree on it.
        base = {k: v for k, v in self._self_identity().items() if k != "hostname"}
        for label, driver in (
            ("absent pid", {**base, "pid": _unused_pid()}),
            ("boot id mismatch",
             {**base, "boot_id": "ffffffff-ffff-ffff-ffff-ffffffffffff"}),
            ("pid reuse",
             {**base,
              "pid_start_ticks": str(int(base["pid_start_ticks"]) + 1)}),
            ("blank hostname recorded",
             {**base, "hostname": "   ", "pid": _unused_pid()}),
        ):
            with self.subTest(case=label):
                self.assertEqual(
                    run_workflow._probe_driver_liveness({"driver": driver}), "unknown")
        # The same block WITH this machine's hostname resolves normally again.
        proven = {**base, "hostname": run_workflow._current_hostname(),
                  "pid": _unused_pid()}
        self.assertEqual(run_workflow._probe_driver_liveness({"driver": proven}), "dead")

    def test_hostname_mismatch_is_unknown(self) -> None:
        # A pid recorded on another machine says nothing about the local /proc.
        identity = self._self_identity()
        identity["hostname"] = "some-other-host.invalid"
        self.assertEqual(run_workflow._probe_driver_liveness({"driver": identity}), "unknown")

    def test_missing_or_malformed_block_is_unknown(self) -> None:
        for meta in (
            {},
            {"driver": {}},
            {"driver": "nope"},
            {"driver": {"pid": 0}},
            {"driver": {"pid": "1234"}},
            {"driver": {"pid": True}},
            {"driver": {"pid": os.getpid()}},  # no recorded ticks to compare
        ):
            with self.subTest(meta=meta):
                self.assertEqual(run_workflow._probe_driver_liveness(meta), "unknown")

    def test_current_identity_round_trips_through_the_probe(self) -> None:
        identity = self._self_identity()
        self.assertEqual(identity["pid"], os.getpid())
        self.assertTrue(identity["pid_start_ticks"].isdigit())

    def test_identity_is_none_when_start_ticks_cannot_be_read(self) -> None:
        # The documented degrade: record NOTHING rather than a pid with a null start
        # time. A pid alone cannot survive pid reuse, and a persisted null would make
        # every later probe answer `unknown` while looking like a real identity.
        original = run_workflow._read_proc_stat
        run_workflow._read_proc_stat = lambda pid: None  # type: ignore[assignment]
        try:
            self.assertIsNone(run_workflow._current_driver_identity())
        finally:
            run_workflow._read_proc_stat = original  # type: ignore[assignment]

    def test_proc_reads_degrade_instead_of_propagating(self) -> None:
        # The degraded VALUES are pinned elsewhere by patching each reader to return
        # them; this pins the catching itself. A host where the read genuinely raises
        # (a masked boot_id, a restricted /proc) must degrade to `unknown`, not
        # propagate an OSError out of the recovery gate.
        original_read_text = Path.read_text
        original_stat = os.stat

        def raising_read_text(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            if str(self).startswith("/proc"):
                raise PermissionError(f"denied: {self}")
            return original_read_text(self, *args, **kwargs)

        def raising_stat(path, *args, **kwargs):  # type: ignore[no-untyped-def]
            # Only the namespace link: `/proc` itself must stay stat-able, since
            # `Path.is_dir()` does not swallow EACCES and the probe's own
            # `Path("/proc").is_dir()` is not the arm under test here.
            if str(path).endswith("/ns/pid"):
                raise PermissionError(f"denied: {path}")
            return original_stat(path, *args, **kwargs)

        Path.read_text = raising_read_text  # type: ignore[assignment]
        os.stat = raising_stat  # type: ignore[assignment]
        try:
            self.assertIsNone(run_workflow._read_proc_stat(os.getpid()))
            self.assertIsNone(run_workflow._read_boot_id())
            self.assertIsNone(run_workflow._read_pid_namespace_inode())
            self.assertIsNone(run_workflow._current_driver_identity())
            self.assertEqual(
                run_workflow._probe_driver_liveness(
                    {"driver": {"pid": os.getpid(), "pid_start_ticks": "1",
                                "hostname": run_workflow._current_hostname()}}),
                "unknown",
            )
        finally:
            Path.read_text = original_read_text  # type: ignore[assignment]
            os.stat = original_stat  # type: ignore[assignment]

    def test_unresolvable_hostname_is_not_fatal(self) -> None:
        original = run_workflow.socket.gethostname
        run_workflow.socket.gethostname = (  # type: ignore[assignment]
            lambda: (_ for _ in ()).throw(OSError("no hostname")))
        try:
            self.assertEqual(run_workflow._current_hostname(), "")
            # An unresolvable local hostname cannot confirm a recorded one either.
            self.assertEqual(
                run_workflow._probe_driver_liveness(
                    {"driver": {"pid": os.getpid(), "pid_start_ticks": "1",
                                "hostname": "somehost"}}),
                "unknown",
            )
        finally:
            run_workflow.socket.gethostname = original  # type: ignore[assignment]

    def test_non_dict_meta_is_unknown(self) -> None:
        for meta in (None, [], "running", 7):
            with self.subTest(meta=meta):
                self.assertEqual(run_workflow._probe_driver_liveness(meta), "unknown")


class IncompleteOrchestrationIndexTests(unittest.TestCase):
    """`_index_incomplete_orchestrations_by_spec` — the candidate list the cold-start
    guard probes. It must use the same terminal-status predicate as the resume gate,
    or the two gates disagree about what counts as an incomplete orchestration."""

    def _write_meta(self, repo_root: Path, oid: str, meta: dict) -> None:
        d = repo_root / "workspace" / "orchestrations" / oid
        d.mkdir(parents=True, exist_ok=True)
        (d / "orchestration_meta.json").write_text(json.dumps(meta), encoding="utf-8")

    def test_indexes_every_non_terminal_status_and_skips_terminal_ones(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            for status in sorted(run_workflow._RESUMABLE_TERMINAL_STATUSES):
                self._write_meta(repo_root, f"orch_t_{status}",
                                 {"status": status, "spec_ref": "spec/x"})
            # `running` is the common case; an operator-supplied `--status` (the CLI
            # exposes it, defaulting to `running`) is non-terminal just the same.
            self._write_meta(repo_root, "orch_running",
                             {"status": "running", "spec_ref": "spec/x"})
            self._write_meta(repo_root, "orch_custom",
                             {"status": "in_progress", "spec_ref": "spec/x"})
            index = run_workflow._index_incomplete_orchestrations_by_spec(repo_root)
            self.assertEqual(sorted(index["spec/x"]), ["orch_custom", "orch_running"])

    def test_groups_by_spec_orders_by_started_at_and_skips_unusable_metas(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._write_meta(repo_root, "orch_late", {
                "status": "running", "spec_ref": "spec/a",
                "started_at": "2026-02-01T00:00:00.000000Z"})
            self._write_meta(repo_root, "orch_early", {
                "status": "running", "spec_ref": "spec/a",
                "started_at": "2026-01-01T00:00:00.000000Z"})
            self._write_meta(repo_root, "orch_other",
                             {"status": "running", "spec_ref": "spec/b"})
            # No spec_ref → cannot be attributed to a spec; must not be indexed.
            self._write_meta(repo_root, "orch_nospec", {"status": "running"})
            # Corrupt meta → skipped rather than raising.
            broken = repo_root / "workspace" / "orchestrations" / "orch_broken"
            broken.mkdir(parents=True, exist_ok=True)
            (broken / "orchestration_meta.json").write_text("{not json", encoding="utf-8")
            index = run_workflow._index_incomplete_orchestrations_by_spec(repo_root)
            self.assertEqual(index["spec/a"], ["orch_early", "orch_late"])
            self.assertEqual(index["spec/b"], ["orch_other"])
            self.assertNotIn("", index)
            self.assertEqual(sorted(index), ["spec/a", "spec/b"])

    def test_ordering_follows_started_at_not_the_directory_or_id_order(self) -> None:
        # The docstring makes stable ordering a contract ("so the emitted warnings are
        # stable"). Pinning it needs a fixture where started_at order contradicts BOTH
        # the id order and the directory order — otherwise dropping the sort, or
        # dropping the started_at key, still yields the expected list by accident.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._write_meta(repo_root, "orch_aaa", {
                "status": "running", "spec_ref": "spec/a",
                "started_at": "2026-02-01T00:00:00.000000Z"})
            self._write_meta(repo_root, "orch_zzz", {
                "status": "running", "spec_ref": "spec/a",
                "started_at": "2026-01-01T00:00:00.000000Z"})
            original_iterdir = Path.iterdir

            def name_ordered_iterdir(self):  # type: ignore[no-untyped-def]
                # Directory order is a filesystem property; make it deterministic (and
                # deliberately id-ascending, i.e. the wrong order) for this assertion.
                return iter(sorted(original_iterdir(self), key=lambda p: p.name))

            Path.iterdir = name_ordered_iterdir  # type: ignore[assignment]
            try:
                index = run_workflow._index_incomplete_orchestrations_by_spec(repo_root)
            finally:
                Path.iterdir = original_iterdir  # type: ignore[assignment]
            self.assertEqual(index["spec/a"], ["orch_zzz", "orch_aaa"])

    def test_missing_orchestrations_dir_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                run_workflow._index_incomplete_orchestrations_by_spec(Path(tmp)), {})


class ProcStatParsingTests(unittest.TestCase):
    """`_parse_proc_stat` — field extraction from a `/proc/<pid>/stat` body.

    A wrong answer here is not a parse error, it is a wrong liveness verdict: a
    misaligned field 22 never compares equal to the recorded ticks, so a live driver
    reads as `dead` and gets terminalized while it is still running."""

    def _stat_body(self, comm: str, state: str, starttime: str) -> str:
        # Real layout: pid (comm) state ...fields 4..21... starttime(22) ...
        middle = " ".join(str(i) for i in range(4, 22))
        return f"4242 ({comm}) {state} {middle} {starttime} 0 0 0\n"

    def test_parses_a_plain_stat_body(self) -> None:
        self.assertEqual(
            run_workflow._parse_proc_stat(self._stat_body("python3", "S", "8236241")),
            ("S", "8236241"),
        )

    def test_parses_a_comm_containing_spaces_and_parentheses(self) -> None:
        # A process can rename itself; `split()` on the whole line misaligns every
        # field after comm, which is why the parser splits after the LAST ')'.
        self.assertEqual(
            run_workflow._parse_proc_stat(self._stat_body("we ird) (name", "R", "99")),
            ("R", "99"),
        )

    def test_rejects_malformed_bodies(self) -> None:
        # 19 post-`)` fields is the exact boundary: one short of the index the parser
        # reads. A guard that lets it through raises IndexError out of the probe —
        # an uncaught crash inside the recovery gate, not a `None` verdict.
        nineteen = "4242 (python3) " + " ".join(str(i) for i in range(19)) + "\n"
        self.assertEqual(len(nineteen[nineteen.rfind(")") + 1:].split()), 19)
        for label, raw in (
            ("no closing paren", "4242 python3 S 1 2 3\n"),
            ("truncated fields", "4242 (python3) S 1 2 3\n"),
            ("exactly 19 fields after the paren", nineteen),
            # Without a `)` the fields cannot be located at all; a long body must be
            # rejected rather than silently parsed at the wrong offsets.
            ("no closing paren but plenty of fields",
             "4242 python3 " + " ".join(str(i) for i in range(40)) + "\n"),
            ("non-numeric starttime", self._stat_body("python3", "S", "not-a-number")),
            ("empty body", ""),
        ):
            with self.subTest(case=label):
                self.assertIsNone(run_workflow._parse_proc_stat(raw))

    def test_accepts_the_minimum_field_count(self) -> None:
        twenty = "4242 (python3) S " + " ".join(str(i) for i in range(4, 22)) + " 777\n"
        self.assertEqual(len(twenty[twenty.rfind(")") + 1:].split()), 20)
        self.assertEqual(run_workflow._parse_proc_stat(twenty), ("S", "777"))

    @unittest.skipUnless(_HAS_PROC, "requires Linux /proc")
    def test_matches_the_real_proc_entry_for_this_process(self) -> None:
        # Guards against the crafted bodies above drifting from the real layout. The
        # expected pair is derived HERE, from the documented field offsets, rather than
        # by calling back into the function under test — otherwise a parser that reads
        # the wrong index would agree with itself and the check would prove nothing.
        raw = Path(f"/proc/{os.getpid()}/stat").read_text(encoding="utf-8")
        fields = raw[raw.rfind(")") + 1:].split()
        expected_state = fields[0]          # field 3
        expected_ticks = fields[19]         # field 22
        self.assertTrue(expected_ticks.isdigit())
        self.assertEqual(run_workflow._parse_proc_stat(raw),
                         (expected_state, expected_ticks))
        # And the offsets themselves are right: field 22 is this process's start time,
        # so it must place the process after boot and before now.
        uptime = float(
            Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
        ticks_per_sec = os.sysconf("SC_CLK_TCK")
        self.assertLessEqual(int(expected_ticks) / ticks_per_sec, uptime)
        self.assertEqual(expected_state, "R")  # the process running this assertion


class DriverIdentityContractTests(unittest.TestCase):
    def test_resumable_statuses_match_runtime_idempotent_terminals(self) -> None:
        # run_workflow decides "is this resumable?" from its own copy of the terminal
        # set (the subprocess boundary to the 21k-line runtime is deliberate), while
        # enable_checkpoint_resume decides "does terminal_reset fire?" from the
        # runtime's. A drift between them silently skips the crash reconciliations.
        from tools.orchestration_runtime import IDEMPOTENT_TERMINAL_STATUSES

        self.assertEqual(
            set(run_workflow._RESUMABLE_TERMINAL_STATUSES),
            set(IDEMPOTENT_TERMINAL_STATUSES),
        )

    def test_sigterm_handler_raises_system_exit_143(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            run_workflow._sigterm_to_exit(15, None)
        self.assertEqual(ctx.exception.code, 143)

    def test_install_signal_handlers_actually_installs_the_converter(self) -> None:
        # The two halves of the SIGTERM story are pinned separately (the converter
        # raises; _run_node catches SystemExit and terminalizes). This pins the seam:
        # if the disposition is left at SIG_DFL the interpreter dies outright — no
        # `except`, no `finally` — and the meta stays `running` forever, which is the
        # exact issue-#11 symptom this change exists to remove.
        import signal as signal_module

        previous = signal_module.getsignal(signal_module.SIGTERM)
        try:
            signal_module.signal(signal_module.SIGTERM, signal_module.SIG_DFL)
            run_workflow._install_signal_handlers()
            self.assertIs(
                signal_module.getsignal(signal_module.SIGTERM),
                run_workflow._sigterm_to_exit,
            )
        finally:
            signal_module.signal(signal_module.SIGTERM, previous)

    def test_main_block_installs_the_handlers_before_running(self) -> None:
        # `_install_signal_handlers` is deliberately NOT called from `main()` (tests and
        # embedding callers invoke that in-process and must not have their signal
        # disposition rewritten), so the only wiring is the `__main__` block. Source-pin
        # it: an in-process test cannot execute that block.
        source = Path(run_workflow.__file__).read_text(encoding="utf-8")
        tail = source[source.index('if __name__ == "__main__":'):]
        self.assertIn("_install_signal_handlers()", tail)
        self.assertLess(tail.index("_install_signal_handlers()"), tail.index("main()"))

    @unittest.skipUnless(hasattr(signal, "SIGTERM"), "requires POSIX signals")
    def test_real_sigterm_exits_143_through_the_real_entry_point(self) -> None:
        # Runs `tools/run_workflow.py` as a real process and sends it a real SIGTERM,
        # so the `__main__` block itself is exercised. The unit pins above and the
        # source-pin below both survive a COMMENTED-OUT installer call; this does not —
        # without it the interpreter dies on the default disposition (returncode -15),
        # no `finally` runs, and the meta is left `running` forever, which is the
        # issue-#11 symptom the whole change exists to remove.
        repo = Path(run_workflow.__file__).parent.parent
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp)
            _seed_shape_expr_schema_into(scratch)
            (scratch / "spec" / "problem").mkdir(parents=True, exist_ok=True)
            (scratch / "spec" / "problem" / "test.md").write_text("spec\n", encoding="utf-8")
            (scratch / "spec" / "problem" / "deps.yaml").write_text(
                "nodes: []\n", encoding="utf-8")
            (scratch / "workspace").mkdir(exist_ok=True)
            _seed_default_llm_config_into(scratch)
            # A runtime stub that parks: the driver then sits in `subprocess.run`
            # inside `_run_node`, i.e. inside the try the interrupt clause guards.
            (scratch / "tools").mkdir(exist_ok=True)
            (scratch / "tools" / "orchestration_runtime.py").write_text(
                "import pathlib, time\n"
                "pathlib.Path(__file__).parent.parent.joinpath('entered.marker')"
                ".write_text('x')\n"
                "time.sleep(300)\n",
                encoding="utf-8",
            )
            marker = scratch / "entered.marker"
            proc = subprocess.Popen(
                [sys.executable, str(repo / "tools" / "run_workflow.py"),
                 "spec/problem/test.md", "compile", "--repo-root", str(scratch),
                 "--no-run-conductor", "--stdout-format", "jsonl"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                start_new_session=True,  # so the parked grandchild can be reaped
            )
            try:
                deadline = time.monotonic() + 60.0
                while not marker.exists() and time.monotonic() < deadline:
                    if proc.poll() is not None:  # pragma: no cover - early exit
                        self.fail(f"driver exited early: {proc.communicate()}")
                    time.sleep(0.05)
                self.assertTrue(marker.exists(), "driver never reached the runtime call")
                proc.send_signal(signal.SIGTERM)
                out, err = proc.communicate(timeout=60)
            finally:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):  # pragma: no cover
                    pass
                if proc.poll() is None:  # pragma: no cover - only on a hung driver
                    proc.kill()
                    proc.communicate()
            # 143 = 128 + SIGTERM: SystemExit unwound the stack normally.
            # -15 would mean the default disposition killed the interpreter outright.
            self.assertEqual(proc.returncode, 143, f"stdout={out!r} stderr={err!r}")


class LlmConfigStartupTests(unittest.TestCase):
    """Issue #28 / #36: the leaf-model authority `run_workflow` loads and threads.

    `./llm.yaml` is the default and the operator's own file; `--llm-config` names another. The
    tests that matter most are the ones a silent fallback would hide: that a missing default
    fails rather than resolving to something nobody chose, and that a copied sample and an
    explicitly named one are the same run."""

    REPO = Path(__file__).resolve().parent.parent.parent

    def _seed(self, repo_root: Path) -> None:
        _seed_shape_expr_schema_into(repo_root)
        for d in ("tools", "workspace", "spec/problem"):
            (repo_root / d).mkdir(parents=True, exist_ok=True)
        (repo_root / "spec" / "problem" / "test.md").write_text("spec\n", encoding="utf-8")
        (repo_root / "spec" / "problem" / "deps.yaml").write_text("nodes: []\n", encoding="utf-8")
        # What an operator's checkout looks like: the samples in the tree, and one copied to
        # the default path. Both are needed against a scratch root — there is no fallback to
        # the installed copy any more, and a run that records a repo-relative path must be
        # able to RE-READ it from this root on resume.
        (repo_root / "docs" / "examples").mkdir(parents=True, exist_ok=True)
        for name in lc.SAMPLE_CONFIG_NAMES:
            shutil.copy(SAMPLE_DIR / name, repo_root / "docs" / "examples" / name)
        shutil.copy(SAMPLE_DIR / "llm_claude.example.yaml", repo_root / "llm.yaml")

    def _fake_runtime(self, root, env, args):  # type: ignore[no-untyped-def]
        self._runtime_calls.append(list(args))
        if args[0] == "init":
            return run_workflow.RuntimeResult(
                payload={"status": "ok", "orchestration_agent_run_id": "oar"}, raw_stdout="{}")
        if args[0] == "preflight":
            return run_workflow.RuntimeResult(
                payload={"status": "pass", "can_launch_step_agents": True,
                         "can_launch_substep_agents": True}, raw_stdout="{}")
        return run_workflow.RuntimeResult(payload={"status": "ok"}, raw_stdout="{}")

    def _run(self, repo_root: Path, extra: list[str], *, oid: str = "orch_cfg",
             positional: bool = True) -> tuple[int, dict, str, list[dict]]:
        """Run `main` with a fake runtime and a captured conductor. Returns
        (exit code, conductor kwargs, stderr text, parsed stdout lines)."""
        import tools.workflow_conductor as wc
        captured: dict = {}
        self._runtime_calls: list[list[str]] = []
        orig_rt, orig_rc, orig_err = (
            run_workflow._runtime_command, wc.run_conductor, sys.stderr)
        err = io.StringIO()
        out = io.StringIO()
        try:
            run_workflow._runtime_command = self._fake_runtime  # type: ignore[assignment]
            wc.run_conductor = lambda **kw: (captured.update(kw) or "pass")  # type: ignore
            sys.stderr = err
            with redirect_stdout(out):
                # A positional spec_ref forces the single-node path (`force_single_node`), so
                # a closure-resume test must omit it — exactly as the operator does.
                code = run_workflow.main([
                    *(["spec/problem/test.md", "build"] if positional else []),
                    "--repo-root", str(repo_root),
                    "--orchestration-id", oid, "--stdout-format", "jsonl", *extra])
        finally:
            run_workflow._runtime_command = orig_rt  # type: ignore[assignment]
            wc.run_conductor = orig_rc  # type: ignore[assignment]
            sys.stderr = orig_err
        lines = [json.loads(ln) for ln in out.getvalue().splitlines() if ln.strip()]
        return code, captured, err.getvalue(), lines

    # --- threading + equivalence ---------------------------------------------------

    def test_main_hands_the_closure_driver_the_configuration_it_loaded(self) -> None:
        """The closure driver pins every member against this object, so `main` handing it a
        different one (or none) would leave each member's gate comparing against something the
        run never used."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed(repo_root)
            cfg = repo_root / "mycodex.yaml"
            cfg.write_text("defaults:\n  provider: codex_cli\n  model: gpt-5.6-sol\n",
                           encoding="utf-8")
            captured: dict = {}
            orig = run_workflow._run_with_dependency_closure
            run_workflow._run_with_dependency_closure = (   # type: ignore[assignment]
                lambda **kw: (captured.update(kw) or 0))
            try:
                self._run(repo_root, ["--llm-config", "mycodex.yaml",
                                      "--with-deps"], oid="orch_wd")
            finally:
                run_workflow._run_with_dependency_closure = orig  # type: ignore[assignment]
            self.assertEqual(captured["llm_config"].defaults.model, "gpt-5.6-sol")
            self.assertEqual(captured["llm_config"].sha256, lc.config_sha256(cfg))

    def test_main_hands_the_closure_RESUME_driver_the_recorded_configuration(self) -> None:
        """The other call site: a closure resume, which loads the RECORDED pin rather than a
        flag, and hands that to every member gate."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed(repo_root)
            shipped = repo_root / "docs" / "examples" / "llm_claude.example.yaml"
            self._seed_resumable(repo_root, "orch_cl", {
                "llm_config_path": "docs/examples/llm_claude.example.yaml",
                "llm_config_sha256": lc.config_sha256(shipped),
                "closure_id": "orch_cl", "closure_target_spec_ref": "spec/problem/test.md",
                "closure_until_phase": "Build",
            })
            captured: dict = {}
            orig = run_workflow._run_with_dependency_closure
            run_workflow._run_with_dependency_closure = (   # type: ignore[assignment]
                lambda **kw: (captured.update(kw) or 0))
            try:
                self._run(repo_root, ["--resume"], oid="orch_cl", positional=False)
            finally:
                run_workflow._run_with_dependency_closure = orig  # type: ignore[assignment]
            self.assertEqual(captured.get("llm_config", None) and
                             captured["llm_config"].sha256, lc.config_sha256(shipped),
                             msg=f"closure driver not reached; captured={sorted(captured)}")

    def test_llm_config_threads_into_run_conductor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed(repo_root)
            _, kw, _, _ = self._run(repo_root, ["--llm-config", "docs/examples/llm_claude.example.yaml"])
            cfg = kw["llm_config"]
            self.assertEqual(cfg.providers, frozenset({"claude_cli"}))
            self.assertNotIn("backend", kw)      # the identity kwarg is gone
            self.assertNotIn("agent_model", kw)

    def test_a_default_run_uses_the_operators_own_llm_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed(repo_root)
            _, kw, _, _ = self._run(repo_root, [], oid="orch_default")
            self.assertEqual(kw["llm_config"].providers, frozenset({"claude_cli"}))
            self.assertEqual(Path(kw["llm_config"].path), repo_root / "llm.yaml")
            self.assertEqual(self._invocation()["llm_config_path"], "llm.yaml")

    def test_a_missing_default_stops_the_run_before_anything_is_launched(self) -> None:
        """No fallback: a run resolved from a file nobody chose is a run nobody can reproduce.
        The refusal has to carry the fix, because the operator's next question is where to get
        one — and it must land before init, or a half-created orchestration outlives it."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed(repo_root)
            (repo_root / "llm.yaml").unlink()
            code, kw, _, lines = self._run(repo_root, [], oid="orch_nodefault")
            self.assertEqual(code, 2)
            self.assertEqual(kw, {})                  # the conductor was never reached
            self.assertEqual(self._runtime_calls, [])  # ...nor init, nor preflight
            self.assertEqual(lines[-1]["reason"], "invalid_startup_input")
            detail = lines[-1]["detail"]
            self.assertIn("llm_config_default_missing", detail)
            self.assertIn(str(repo_root / "llm.yaml"), detail)
            self.assertIn(
                f"cp {repo_root / 'docs' / 'examples' / 'llm_claude.example.yaml'} "
                f"{repo_root / 'llm.yaml'}", detail)

    def test_a_copied_sample_and_the_named_sample_are_the_same_run(self) -> None:
        """`cp docs/examples/llm_claude.example.yaml llm.yaml` must be exactly the run that
        naming the sample gives. This is the acceptance for demoting the shipped configs to
        samples: the copy is not a different configuration, only a different spelling."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed(repo_root)          # already copies the sample to ./llm.yaml
            _, default_kw, _, _ = self._run(repo_root, [], oid="orch_eq_a")
            _, named_kw, _, _ = self._run(
                repo_root, ["--llm-config", "docs/examples/llm_claude.example.yaml"],
                oid="orch_eq_b")
            self.assertEqual(default_kw["llm_config"].entries, named_kw["llm_config"].entries)
            self.assertEqual(default_kw["llm_config"].defaults, named_kw["llm_config"].defaults)
            self.assertEqual(default_kw["llm_config"].sha256, named_kw["llm_config"].sha256)
            self.assertEqual(default_kw["llm_config"].provenance_map(),
                             named_kw["llm_config"].provenance_map())

    def test_the_file_is_the_only_thing_that_says_what_a_leaf_launches(self) -> None:
        """`--llm` / `--agent-model` / `--llm-command` are gone: what a run launches is what
        the configuration says, with no flag able to move it afterwards. `llm` / `llm_command`
        — which the runtime's init/preflight surface still speaks — are DERIVED from it."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed(repo_root)
            cfg = repo_root / "wrapped-codex.yaml"
            cfg.write_text("defaults:\n  provider: codex_cli\n  model: gpt-5.6-sol\n"
                           "  command: mywrap --x\n", encoding="utf-8")
            code, kw, _, lines = self._run(
                repo_root, ["--llm-config", "wrapped-codex.yaml"], oid="orch_only")
            self.assertEqual(code, 0, msg=json.dumps(lines[-1] if lines else {}))
            self.assertEqual(kw["llm_config"].defaults.model, "gpt-5.6-sol")
            self.assertEqual(kw["llm_config"].defaults.command, "mywrap --x")
            self.assertEqual(self._invocation()["llm"], "codex")
            self.assertEqual(self._invocation()["llm_command"], "mywrap --x")

    def test_each_removed_flag_fails_by_name_and_says_where_the_setting_went(self) -> None:
        """`--llm` is the one that has to be REGISTERED to fail: it is an unambiguous prefix of
        `--llm-config`, so simply deleting it let argparse abbreviate `--llm claude` into
        `--llm-config claude`, which then died as an unreadable file the operator never named.
        The other two would fail as "unrecognized arguments", which says nothing about where
        the setting went — and `--agent-model` has a live namesake on the runtime's own `init`
        surface to be confused with."""
        for flag, value, replacement in (("--llm", "claude", "`provider:`"),
                                         ("--agent-model", "opus", "`model:`"),
                                         ("--llm-command", "wrapper", "`command:`")):
            err = io.StringIO()
            with mock.patch.object(sys, "stderr", err), self.assertRaises(SystemExit):
                run_workflow._parse_args(["spec/problem.md", "generate", flag, value])
            self.assertIn(f"{flag} was removed", err.getvalue(), msg=flag)
            self.assertIn(replacement, err.getvalue(), msg=flag)
            self.assertIn("llm.yaml", err.getvalue(), msg=flag)
        # ...and none of them leaves a namespace attribute a later branch could read.
        ns = run_workflow._parse_args(["spec/problem.md", "generate"])
        for removed in ("llm", "agent_model", "llm_command"):
            self.assertFalse(hasattr(ns, removed), msg=removed)
        # The flag they are NOT allowed to be abbreviated into still parses exactly.
        self.assertEqual(
            run_workflow._parse_args(
                ["spec/problem.md", "generate", "--llm-config", "f.yaml"]).llm_config,
            "f.yaml")

    def test_llm_config_alone_warns_about_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed(repo_root)
            _, _, err, _ = self._run(repo_root, ["--llm-config", "docs/examples/llm_claude.example.yaml"])
            self.assertNotIn("deprecated", err)

    def test_a_named_config_rule_surfaces_as_invalid_startup_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed(repo_root)
            bad = repo_root / "bad.yaml"
            bad.write_text("defaults:\n  provider: gemini_cli\n", encoding="utf-8")
            code, _, _, lines = self._run(repo_root, ["--llm-config", str(bad)])
            self.assertEqual(code, 2)
            self.assertEqual(lines[-1]["reason"], "invalid_startup_input")
            self.assertIn("llm_config_unknown_provider", lines[-1]["detail"])

    def test_preflight_is_told_the_resolved_defaults_so_it_probes_what_will_launch(self) -> None:
        """Preflight is a subprocess that only gets the PATH, and it re-applies these onto the
        file it reloads. What is asserted here is that `main` sends the `defaults` IT resolved
        — the transport, not its effect: the values equal the file's own today, so a preflight
        that ignored them would still probe the right command. `test_orchestration_runtime`'s
        `test_the_probed_command_is_the_one_the_run_will_launch` covers the effect by supplying
        an override the file does not declare."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed(repo_root)
            cfg = repo_root / "wrapped.yaml"
            cfg.write_text("defaults:\n  provider: claude_cli\n  model: opus\n"
                           "  command: mywrap --x\n", encoding="utf-8")
            self._run(repo_root, ["--llm-config", "wrapped.yaml"], oid="orch_ovp")
            args = next(a for a in self._runtime_calls if a and a[0] == "preflight")
            self.assertEqual(args[args.index("--llm-config-defaults-command") + 1],
                             "mywrap --x")
            self.assertEqual(args[args.index("--llm-config-defaults-model") + 1], "opus")
            # ...and the top-level probe gets the same command.
            self.assertEqual(args[args.index("--agent-command") + 1], "mywrap --x")

    def test_preflight_is_told_which_snapshot_to_probe(self) -> None:
        """Preflight is a subprocess that reloads the file; without the hash, an edit between
        the load and the probe certifies commands the conductor will never launch."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed(repo_root)
            _, kw, _, _ = self._run(
                repo_root, ["--llm-config", "docs/examples/llm_claude.example.yaml"], oid="orch_snap")
            args = next(a for a in self._runtime_calls if a and a[0] == "preflight")
            self.assertEqual(args[args.index("--llm-config-sha256") + 1],
                             kw["llm_config"].sha256)

    def test_a_file_that_declares_its_own_values_sends_those(self) -> None:
        """The resolved defaults are sent unconditionally: re-applying a value the file already
        declares is a no-op, so nothing has to track which came from a flag."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed(repo_root)
            cfg = repo_root / "declared.yaml"
            cfg.write_text("defaults:\n  provider: claude_cli\n  model: from-file\n"
                           "  command: from-file-cmd\n", encoding="utf-8")
            self._run(repo_root, ["--llm-config", "declared.yaml"], oid="orch_novp")
            args = next(a for a in self._runtime_calls if a and a[0] == "preflight")
            self.assertEqual(args[args.index("--llm-config-defaults-command") + 1],
                             "from-file-cmd")
            self.assertEqual(args[args.index("--llm-config-defaults-model") + 1], "from-file")

    def test_a_mixed_config_runs_and_preflight_is_asked_to_probe_the_file(self) -> None:
        """Preflight probes every provider the file names, so a mixed config is admissible —
        and the file is what it is asked to probe, not the derived single backend."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed(repo_root)
            mixed = repo_root / "mixed.yaml"
            mixed.write_text(
                "defaults:\n  provider: claude_cli\n"
                "phases:\n  generate:\n    substeps:\n      generate:\n"
                "        provider: openai_compatible\n"
                "        base_url: http://localhost:8000/v1\n"
                "        api_key_env: LOCAL_KEY\n        model: m\n", encoding="utf-8")
            code, kw, _, lines = self._run(repo_root, ["--llm-config", str(mixed)])
            self.assertEqual(code, 0, msg=json.dumps(lines[-1] if lines else {}))
            self.assertFalse(kw["llm_config"].is_uniform)
            preflight = next(a for a in self._runtime_calls if a and a[0] == "preflight")
            self.assertIn("--llm-config", preflight)
            self.assertEqual(preflight[preflight.index("--llm-config") + 1], str(mixed))
            # The top-level backend still describes `defaults`.
            self.assertEqual(preflight[preflight.index("--backend") + 1], "claude")

    # --- the invocation record ------------------------------------------------------

    def _invocation(self) -> dict:
        """The invocation block `main` handed to `init` on the last `self._run(...)`.

        Read off the runtime argv rather than `orchestration_meta.json`: the meta file is
        written by the REAL runtime, which these tests replace, so the argv is where the record
        actually is."""
        for args in self._runtime_calls:
            if args and args[0] == "init" and "--invocation-json" in args:
                return json.loads(args[args.index("--invocation-json") + 1])
        self.fail("init was called without an --invocation-json record")

    def test_invocation_records_the_config_path_hash_and_leaf_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed(repo_root)
            self._run(repo_root, ["--llm-config", "docs/examples/llm_claude.example.yaml"], oid="orch_rec")
            inv = self._invocation()
            self.assertEqual(inv["llm_config_path"], "docs/examples/llm_claude.example.yaml")
            self.assertEqual(
                inv["llm_config_sha256"],
                lc.config_sha256(repo_root / "docs" / "examples" / "llm_claude.example.yaml"))
            self.assertEqual(set(inv["llm_leaf_map"]),
                             {"defaults"} | {f"{p}.{s}" for p, s in lc.LLM_LEAF_SUBSTEPS})
            self.assertEqual(inv["llm_leaf_map"]["validate.judge"]["backend"], "claude")
            # The old keys stay, for tooling that reads them.
            self.assertEqual(inv["llm"], "claude")
            self.assertIn("llm_command", inv)

    def test_the_invocation_no_longer_records_flag_overrides(self) -> None:
        """The key held the removed flags' literals. Writing an empty dict would be worse than
        omitting it: a resume reads the key back, and an always-empty one is indistinguishable
        from a run that genuinely had none."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed(repo_root)
            self._run(repo_root, ["--llm-config", "docs/examples/llm_codex.example.yaml"], oid="orch_ov")
            inv = self._invocation()
            self.assertNotIn("llm_config_overrides", inv)
            self.assertEqual(inv["llm_config_path"], "docs/examples/llm_codex.example.yaml")

    # --- resume refusal --------------------------------------------------------------

    def _rejection(self, recorded: dict, **kw) -> dict | None:
        base = dict(repo_root=Path("/tmp/repo"), effective_path="llm.yaml",
                    effective_sha256="sha256:aaa", effective_overrides={})
        base.update(kw)
        return run_workflow._llm_config_resume_rejection("orch_x", recorded, **base)

    def test_a_record_with_no_pin_is_refused_under_its_own_reason(self) -> None:
        """Not `llm_config_changed_since_launch`: nothing changed. The run predates the pin
        entirely, and the flags that described its models no longer exist."""
        out = self._rejection({"path": "", "sha256": "", "overrides": {}})
        assert out is not None
        self.assertEqual(out["reason"], "llm_config_legacy_flags_removed")
        self.assertIn("Start a fresh run", out["detail"])

    def test_the_no_pin_predicate_is_the_same_one_the_byte_gate_delegates_to(self) -> None:
        """Two gates, one answer. `_llm_config_resume_rejection` delegates rather than
        re-testing, so a caller reaching it cannot accept an unpinned record by falling off
        the end of the byte comparison."""
        for recorded in ({}, {"path": "", "sha256": "", "overrides": {}}):
            direct = run_workflow._llm_config_legacy_pin_rejection("orch_x", recorded)
            assert direct is not None
            self.assertEqual(direct, self._rejection(recorded))
        # ...and a record that DOES pin passes the predicate through to the byte gate.
        self.assertIsNone(run_workflow._llm_config_legacy_pin_rejection(
            "orch_x", {"path": "docs/examples/llm_claude.example.yaml", "sha256": "sha256:aaa"}))

    def test_a_changed_config_file_refuses_the_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            path = repo_root / "llm.yaml"
            path.write_text("defaults:\n  provider: claude_cli\n", encoding="utf-8")
            recorded = {"path": "llm.yaml",
                        "sha256": lc.config_sha256(path), "overrides": {}}
            self.assertIsNone(self._rejection(
                recorded, repo_root=repo_root, effective_sha256=recorded["sha256"]))
            path.write_text("defaults:\n  provider: claude_cli\n  model: pinned\n",
                            encoding="utf-8")
            out = self._rejection(recorded, repo_root=repo_root,
                                  effective_sha256=lc.config_sha256(path))
            assert out is not None
            self.assertEqual(out["reason"], "llm_config_changed_since_launch")
            self.assertIn("has changed since launch", out["detail"])

    def test_the_loaded_snapshot_is_compared_not_only_the_file(self) -> None:
        """`effective_sha256` is the bytes the run will actually use. Checking only the file
        leaves a window: an atomic replace between the gate and the load resolves the entries
        from bytes neither hash describes, and the resume proceeds unpinned."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            path = repo_root / "llm.yaml"
            path.write_text("defaults:\n  provider: claude_cli\n", encoding="utf-8")
            recorded = {"path": "llm.yaml",
                        "sha256": lc.config_sha256(path), "overrides": {}}
            # The file on disk still matches; the SNAPSHOT the run loaded does not.
            out = self._rejection(recorded, repo_root=repo_root,
                                  effective_sha256="sha256:something-else")
            assert out is not None
            self.assertEqual(out["reason"], "llm_config_changed_since_launch")
            self.assertIn("as loaded", out["detail"])

    def test_an_unreadable_config_refuses_the_resume_without_a_traceback(self) -> None:
        """`config_sha256` used to raise `PermissionError` here, and the startup handler
        catches only `ValueError` — so a permission change turned the documented refusal into
        an uncaught traceback."""
        import os
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            path = repo_root / "llm.yaml"
            path.write_text("defaults:\n  provider: claude_cli\n", encoding="utf-8")
            recorded = {"path": "llm.yaml",
                        "sha256": lc.config_sha256(path), "overrides": {}}
            os.chmod(path, 0o000)
            try:
                if os.access(path, os.R_OK):    # running as root: the mode does not apply
                    self.skipTest("cannot make a file unreadable as this user")
                out = self._rejection(recorded, repo_root=repo_root,
                                      effective_sha256=recorded["sha256"])
            finally:
                os.chmod(path, 0o644)
            assert out is not None
            self.assertEqual(out["reason"], "llm_config_changed_since_launch")
            self.assertIn("cannot be read", out["detail"])

    def test_a_deleted_config_file_refuses_the_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = self._rejection(
                {"path": "llm.yaml", "sha256": "sha256:aaa", "overrides": {}},
                repo_root=Path(tmp))
            assert out is not None
            self.assertEqual(out["reason"], "llm_config_changed_since_launch")
            self.assertIn("is gone", out["detail"])

    def test_resuming_with_a_different_config_file_is_refused(self) -> None:
        out = self._rejection(
            {"path": "docs/examples/llm_codex.example.yaml", "sha256": "sha256:aaa", "overrides": {}},
            effective_path="docs/examples/llm_claude.example.yaml")
        assert out is not None
        self.assertEqual(out["reason"], "llm_config_changed_since_launch")
        self.assertIn("start a fresh run", out["detail"])

    def test_a_record_carrying_flag_overrides_can_no_longer_be_resumed(self) -> None:
        """The overrides were the removed flags' literals, so this resume can only ever apply
        `{}`. The comparison therefore always fails for such a record — deliberately: those
        phases ran on a model nothing on disk names, and the fix is to write it into the file
        and start fresh."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            path = repo_root / "llm.yaml"
            path.write_text("defaults:\n  provider: claude_cli\n", encoding="utf-8")
            recorded = {"path": "llm.yaml",
                        "sha256": lc.config_sha256(path), "overrides": {"model": "opus"}}
            out = self._rejection(recorded, repo_root=repo_root,
                                  effective_sha256=recorded["sha256"])
            assert out is not None
            self.assertIn("cannot be re-passed", out["detail"])
            # ...and a record that carried none resumes.
            recorded["overrides"] = {}
            self.assertIsNone(self._rejection(
                recorded, repo_root=repo_root, effective_sha256=recorded["sha256"]))

    def _seed_resumable(self, repo_root: Path, oid: str, invocation: dict,
                        *, backend: str = "claude") -> None:
        """An orchestration a resume can find: the meta block both resume gates read, plus the
        start-prompt artifact `_load_resume_params` recovers `until_phase` / `mode` from.

        `preflight.json` is written for fixture realism — production always has one — but
        nothing on the resume path reads it any more, so `backend` reaches only that file.
        What makes a test using this helper a CODEX resume is the configuration it pins."""
        d = repo_root / "workspace" / "orchestrations" / oid
        (d / "launches").mkdir(parents=True, exist_ok=True)
        (d / "orchestration_meta.json").write_text(json.dumps({
            "orchestration_id": oid, "status": "fail", "spec_ref": "spec/problem/test.md",
            "invocation": {"generate_executor": "pure", **invocation},
        }), encoding="utf-8")
        (d / "preflight.json").write_text(
            json.dumps({"backend": backend, "probe_command": backend}), encoding="utf-8")
        (d / "launches" / "orchestration.start.prompt.txt").write_text(
            "end phase: `Build`\nworkflow_mode: `dev`\n"
            "target_spec_ref: `spec/problem/test.md`\n",
            encoding="utf-8")

    def test_the_entry_gate_refuses_a_resume_whose_config_changed(self) -> None:
        """End to end through `main`, not just the predicate."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed(repo_root)
            shipped = repo_root / "docs" / "examples" / "llm_claude.example.yaml"
            self._seed_resumable(repo_root, "orch_res", {
                "llm_config_path": "docs/examples/llm_claude.example.yaml",
                "llm_config_sha256": lc.config_sha256(shipped),
                "llm_config_overrides": {},
            })
            # Unchanged file: the resume proceeds and the conductor is reached.
            code, kw, _, lines = self._run(repo_root, ["--resume"], oid="orch_res")
            self.assertEqual(code, 0, msg=json.dumps(lines[-1] if lines else {}))
            self.assertIn("llm_config", kw)
            shipped.write_text("defaults:\n  provider: claude_cli\n  model: pinned\n",
                               encoding="utf-8")
            code, kw, _, lines = self._run(repo_root, ["--resume"], oid="orch_res")
            self.assertEqual(code, 2)
            self.assertEqual(kw, {})
            self.assertEqual(lines[-1]["reason"], "llm_config_changed_since_launch")

    def test_a_legacy_record_is_refused_end_to_end(self) -> None:
        """A run predating issue #28 pinned nothing, and the flags that named its models are
        gone. Refused BEFORE the default is resolved, so the reason names what is actually
        wrong rather than a file the operator would then go and create for nothing."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed(repo_root)
            self._seed_resumable(repo_root, "orch_old", {"llm": "claude", "agent_model": "opus"})
            code, kw, _, lines = self._run(repo_root, ["--resume"], oid="orch_old")
            self.assertEqual(code, 2)
            self.assertEqual(kw, {})
            self.assertEqual(lines[-1]["reason"], "llm_config_legacy_flags_removed")
            self.assertEqual(lines[-1]["orchestration_id"], "orch_old")

    def test_a_legacy_record_is_refused_before_the_default_is_even_resolved(self) -> None:
        """This is what the ENTRY gate buys, and it is invisible while a `./llm.yaml` happens
        to exist: without the early check, a pre-#28 resume on a machine that has not created
        one yet stops at `llm_config_default_missing` and tells the operator to `cp` a sample
        — for a run that can never be resumed no matter what they copy."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed(repo_root)
            (repo_root / "llm.yaml").unlink()          # the operator has not created one
            self._seed_resumable(repo_root, "orch_old_nodefault",
                                 {"llm": "claude", "agent_model": "opus"})
            code, kw, _, lines = self._run(
                repo_root, ["--resume"], oid="orch_old_nodefault")
            self.assertEqual(code, 2)
            self.assertEqual(kw, {})
            self.assertEqual(lines[-1]["reason"], "llm_config_legacy_flags_removed")
            self.assertNotIn("llm_config_default_missing", lines[-1]["detail"])

    def test_a_closure_member_with_no_pin_is_refused_too(self) -> None:
        """The entry gate never sees a member, so the closure gate has to carry the same
        refusal — a pre-#28 member would otherwise be warm-resumed onto today's models."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed(repo_root)
            member = repo_root / "workspace" / "orchestrations" / "orch_member"
            member.mkdir(parents=True)
            (member / "orchestration_meta.json").write_text(json.dumps({
                "spec_ref": "spec/problem/test.md", "status": "fail",
                "invocation": {"closure_id": "orch_cl2", "generate_executor": "pure"},
            }), encoding="utf-8")
            out = run_workflow._llm_config_resume_rejection(
                "orch_member", run_workflow._recorded_llm_config(repo_root, "orch_member"),
                repo_root=repo_root, effective_path="docs/examples/llm_claude.example.yaml",
                effective_sha256="sha256:aaa", effective_overrides={})
            assert out is not None
            self.assertEqual(out["reason"], "llm_config_legacy_flags_removed")


    def test_a_config_pinned_codex_run_can_be_resumed(self) -> None:
        """A codex resume takes its slug from the RECORDED configuration and nothing else. The
        guard that used to demand a run-wide model here keyed on `--llm-config` being passed —
        which a resume never does, because it recovers the pin — so it fired on every resume of
        a config-pinned codex run, for a model the file already named."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed(repo_root)
            cfg = repo_root / "mycodex.yaml"
            cfg.write_text("defaults:\n  provider: codex_cli\n  model: gpt-5.6-sol\n",
                           encoding="utf-8")
            self._seed_resumable(repo_root, "orch_cx", {
                "llm_config_path": "mycodex.yaml",
                "llm_config_sha256": lc.config_sha256(cfg),
                "llm_config_overrides": {},
            }, backend="codex")
            code, kw, _, lines = self._run(repo_root, ["--resume"], oid="orch_cx")
            self.assertEqual(code, 0, msg=json.dumps(lines[-1] if lines else {}))
            self.assertEqual(kw["llm_config"].defaults.model, "gpt-5.6-sol")

    def test_the_recorded_pin_is_what_a_resume_loads(self) -> None:
        """Not the shipped config for the recovered backend: replacing this branch with the
        fallback left the whole suite green, because no test pinned a NON-shipped file."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed(repo_root)
            cfg = repo_root / "custom.yaml"
            cfg.write_text("defaults:\n  provider: claude_cli\n  model: pinned-by-file\n"
                           "phases:\n  validate:\n    substeps:\n      judge:\n"
                           "        model: judge-only\n", encoding="utf-8")
            self._seed_resumable(repo_root, "orch_pin", {
                "llm_config_path": "custom.yaml",
                "llm_config_sha256": lc.config_sha256(cfg),
                "llm_config_overrides": {},
            })
            code, kw, _, lines = self._run(repo_root, ["--resume"], oid="orch_pin")
            self.assertEqual(code, 0, msg=json.dumps(lines[-1] if lines else {}))
            self.assertEqual(kw["llm_config"].defaults.model, "pinned-by-file")
            self.assertEqual(kw["llm_config"].entry_for("validate", "judge").model,
                             "judge-only")

    def test_the_recorded_overrides_are_read_back_and_refuse_the_resume(self) -> None:
        """Making `_recorded_llm_config` always return `{}` left the suite green: the
        comparison was only ever fed dicts the tests themselves supplied. End to end, a record
        carrying overrides must now reach the gate and be refused."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed(repo_root)
            shipped = repo_root / "docs" / "examples" / "llm_claude.example.yaml"
            self._seed_resumable(repo_root, "orch_ovr", {
                "llm_config_path": "docs/examples/llm_claude.example.yaml",
                "llm_config_sha256": lc.config_sha256(shipped),
                "llm_config_overrides": {"model": "opus"},
            })
            code, kw, _, lines = self._run(repo_root, ["--resume"], oid="orch_ovr")
            self.assertEqual(code, 2)
            self.assertEqual(kw, {})
            self.assertEqual(lines[-1]["reason"], "llm_config_changed_since_launch")
            self.assertIn("cannot be re-passed", lines[-1]["detail"])

    def test_a_deleted_pinned_config_says_restore_it(self) -> None:
        """The pin is compared BEFORE the load, so a missing file gets the resume rejection
        rather than a generic `llm_config_unreadable`."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed(repo_root)
            cfg = repo_root / "custom.yaml"
            cfg.write_text("defaults:\n  provider: claude_cli\n", encoding="utf-8")
            self._seed_resumable(repo_root, "orch_gone", {
                "llm_config_path": "custom.yaml",
                "llm_config_sha256": lc.config_sha256(cfg),
                "llm_config_overrides": {},
            })
            cfg.unlink()
            code, _, _, lines = self._run(repo_root, ["--resume"], oid="orch_gone")
            self.assertEqual(code, 2)
            self.assertEqual(lines[-1]["reason"], "llm_config_changed_since_launch")
            self.assertIn("is gone", lines[-1]["detail"])

    def test_a_resume_that_passes_nothing_is_not_warned_at(self) -> None:
        """The notice exists for a flag the operator actually typed; announcing one on every
        resume would train them to ignore it."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed(repo_root)
            shipped = repo_root / "docs" / "examples" / "llm_claude.example.yaml"
            self._seed_resumable(repo_root, "orch_quiet", {
                "llm_config_path": "docs/examples/llm_claude.example.yaml",
                "llm_config_sha256": lc.config_sha256(shipped),
                "agent_model": "opus",           # recorded by the original run
            })
            code, _, err, _ = self._run(repo_root, ["--resume"], oid="orch_quiet")
            self.assertEqual(code, 0)
            self.assertNotIn("is ignored on --resume", err)

    def test_an_llm_config_dropped_in_favour_of_the_pin_is_announced(self) -> None:
        """The recorded pin wins even over an explicit flag — the finished phases ran on it —
        so the flag is dropped, and this notice is the only signal that it was."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed(repo_root)
            shipped = repo_root / "docs" / "examples" / "llm_claude.example.yaml"
            self._seed_resumable(repo_root, "orch_warn", {
                "llm_config_path": "docs/examples/llm_claude.example.yaml",
                "llm_config_sha256": lc.config_sha256(shipped),
            })
            code, kw, err, _ = self._run(
                repo_root, ["--resume", "--llm-config", "docs/examples/llm_codex.example.yaml"],
                oid="orch_warn")
            self.assertEqual(code, 0)
            self.assertEqual(kw["llm_config"].providers, frozenset({"claude_cli"}))
            self.assertIn("--llm-config is ignored on --resume", err)

    # --- the launch-time snapshot ----------------------------------------------------

    def _snapshot(self, repo_root: Path, oid: str) -> Path:
        return (repo_root / "workspace" / "orchestrations" / oid
                / run_workflow.LLM_CONFIG_SNAPSHOT_NAME)

    def test_a_cold_run_snapshots_the_bytes_it_recorded_the_hash_of(self) -> None:
        """The default configuration is an operator-owned file no version control has a copy
        of, so an edit between the launch and a resume is otherwise unrecoverable. The snapshot
        must be the SAME bytes the pin describes — a second read would capture whatever the
        file says now, which is exactly the state this exists to survive."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed(repo_root)
            cfg = repo_root / "snap.yaml"
            cfg.write_text("defaults:\n  provider: claude_cli\n  model: launched\n",
                           encoding="utf-8")
            code, _, _, lines = self._run(
                repo_root, ["--llm-config", "snap.yaml"], oid="orch_snapw")
            self.assertEqual(code, 0, msg=json.dumps(lines[-1] if lines else {}))
            snapshot = self._snapshot(repo_root, "orch_snapw")
            self.assertTrue(snapshot.exists())
            self.assertEqual(snapshot.read_bytes(), cfg.read_bytes())
            self.assertEqual(lc._sha256_bytes(snapshot.read_bytes()),
                             self._invocation()["llm_config_sha256"])

    def test_a_cold_re_init_of_the_same_id_replaces_the_snapshot(self) -> None:
        """Reusing an `--orchestration-id` cold rewrites `invocation` with the NEW
        configuration's hash. A snapshot that skipped an existing file would then describe the
        PREVIOUS run — and the refusal that names it would be unactionable forever: restoring
        from it reproduces bytes whose hash still does not match, so the resume refuses again
        with the same instruction. The invariant is that these bytes are what
        `invocation.llm_config_sha256` describes."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed(repo_root)
            snapshot = self._snapshot(repo_root, "orch_reinit")
            for name, model in (("first.yaml", "was-launched-first"),
                                ("second.yaml", "launched-on-the-re-run")):
                cfg = repo_root / name
                cfg.write_text(f"defaults:\n  provider: claude_cli\n  model: {model}\n",
                               encoding="utf-8")
                code, _, _, lines = self._run(
                    repo_root, ["--llm-config", name], oid="orch_reinit")
                self.assertEqual(code, 0, msg=json.dumps(lines[-1] if lines else {}))
                self.assertEqual(snapshot.read_bytes(), cfg.read_bytes(), msg=name)
                self.assertEqual(lc._sha256_bytes(snapshot.read_bytes()),
                                 self._invocation()["llm_config_sha256"], msg=name)

    def test_the_snapshot_replaces_the_directory_entry_rather_than_writing_through_it(
        self,
    ) -> None:
        """A cold re-init writes over whatever the name already holds. `write_bytes` OPENS the
        existing name, so a snapshot that is a symlink — stale workspace state, an archive
        restored with links preserved — would have its TARGET overwritten with YAML while the
        entry stayed a link, leaving the run with a snapshot it does not own and a file it
        never meant to touch."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = repo_root / "workspace" / "orchestrations" / "o"
            orch.mkdir(parents=True)
            victim = repo_root / "victim.txt"
            victim.write_text("IMPORTANT", encoding="utf-8")
            (orch / run_workflow.LLM_CONFIG_SNAPSHOT_NAME).symlink_to(victim)
            cfg = _sample_config("claude")
            run_workflow._write_llm_config_snapshot(repo_root, "o", cfg)
            snapshot = orch / run_workflow.LLM_CONFIG_SNAPSHOT_NAME
            self.assertEqual(victim.read_text(encoding="utf-8"), "IMPORTANT")
            self.assertFalse(snapshot.is_symlink())
            self.assertEqual(snapshot.read_bytes(), cfg.raw)

    def test_an_interrupted_snapshot_write_leaves_the_previous_bytes(self) -> None:
        """The whole contract is that these bytes hash to the recorded pin. A truncating
        in-place write that dies mid-way leaves a short file that still LOOKS like a snapshot
        and no longer restores — the unrecoverable state this file exists to prevent. The
        temp-file + rename gives the old bytes or the new bytes, never a prefix, and cleans up
        after itself so a signal during a closure run leaves no `.tmp` litter per node."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = repo_root / "workspace" / "orchestrations" / "o"
            orch.mkdir(parents=True)
            snapshot = orch / run_workflow.LLM_CONFIG_SNAPSHOT_NAME
            snapshot.write_bytes(b"PREVIOUS-GOOD-BYTES\n")
            with mock.patch("os.replace", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    run_workflow._write_llm_config_snapshot(
                        repo_root, "o", _sample_config("claude"))
            self.assertEqual(snapshot.read_bytes(), b"PREVIOUS-GOOD-BYTES\n")
            self.assertEqual([p.name for p in orch.iterdir() if p.name.endswith(".tmp")], [])

    def test_an_unwritable_snapshot_terminalizes_instead_of_escaping(self) -> None:
        """`init` has already committed by the time the snapshot is written, so an escaping
        `OSError` (a full disk, a read-only workspace) kills the driver with a traceback: no
        envelope for a caller parsing stdout, and an orchestration left `running` that only a
        later explicit `--resume --orchestration-id` can clear. It fails the way a preflight
        failure does instead — `set-status fail` plus a named envelope."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed(repo_root)
            real = os.replace

            def boom(src, dst, *a, **k):
                if str(dst).endswith(run_workflow.LLM_CONFIG_SNAPSHOT_NAME):
                    raise OSError(28, "No space left on device", str(dst))
                return real(src, dst, *a, **k)

            with mock.patch("os.replace", boom):
                code, kw, _, lines = self._run(repo_root, [], oid="orch_nospace")
            self.assertEqual(code, 2)
            self.assertEqual(kw, {})                      # the conductor was never reached
            self.assertEqual(lines[-1]["reason"], "llm_config_snapshot_unwritable")
            self.assertEqual(lines[-1]["orchestration_id"], "orch_nospace")
            self.assertIn("No space left on device", lines[-1]["detail"])
            # ...and the committed orchestration is terminalized rather than left `running`.
            status = next(a for a in self._runtime_calls if a and a[0] == "set-status")
            self.assertEqual(status[status.index("--status") + 1], "fail")
            self.assertEqual(status[status.index("--reason-code") + 1],
                             "llm_config_snapshot_unwritable")

    def test_a_resume_does_not_overwrite_the_launch_time_snapshot(self) -> None:
        """It is a record of what the run LAUNCHED with. Refreshing it on resume would make it
        agree with whatever the operator has since written, i.e. destroy the only copy of the
        bytes the finished phases actually ran on."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed(repo_root)
            cfg = repo_root / "docs" / "examples" / "llm_claude.example.yaml"
            self._seed_resumable(repo_root, "orch_snapr", {
                "llm_config_path": "docs/examples/llm_claude.example.yaml",
                "llm_config_sha256": lc.config_sha256(cfg),
            })
            snapshot = self._snapshot(repo_root, "orch_snapr")
            snapshot.write_bytes(b"defaults:\n  provider: claude_cli\n  model: at-launch\n")
            code, _, _, lines = self._run(repo_root, ["--resume"], oid="orch_snapr")
            self.assertEqual(code, 0, msg=json.dumps(lines[-1] if lines else {}))
            self.assertEqual(snapshot.read_bytes(),
                             b"defaults:\n  provider: claude_cli\n  model: at-launch\n")

    def test_every_closure_node_gets_its_own_snapshot(self) -> None:
        """Each closure node is its own orchestration with its own resume gate, so a snapshot
        written only for the target would leave every dependency's refusal unactionable.

        Driven through the REAL `_run_node` (only the runtime subprocess is faked): calling the
        writer directly for two ids would prove nothing but that the path join uses its
        argument, and would stay green with the call deleted from `_run_node` entirely."""
        from tools.orchestration_runtime import _load_spec_catalog
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed(repo_root)
            DependencyClosureTests._seed_diamond(self, repo_root)   # type: ignore[arg-type]
            _load_spec_catalog.cache_clear()
            self._runtime_calls = []
            orig_ready = run_workflow._dependency_node_ready
            orig_rt = run_workflow._runtime_command
            real_run_node = run_workflow._run_node
            ran: set[str] = set()

            def _tracking_run_node(**kw):
                ran.add(kw["spec_ref"])
                return real_run_node(**kw)

            try:
                run_workflow._runtime_command = self._fake_runtime  # type: ignore[assignment]
                run_workflow._dependency_node_ready = (            # type: ignore[assignment]
                    lambda root, node, stages: node["spec_ref"] in ran)
                run_workflow._run_node = _tracking_run_node        # type: ignore[assignment]
                with redirect_stdout(io.StringIO()):
                    rc = run_workflow.main([
                        "spec/problem/a", "compile", "--with-deps",
                        "--repo-root", str(repo_root),
                        "--orchestration-id", "orch_snap_tgt",
                        "--no-run-conductor", "--stdout-format", "jsonl"])
            finally:
                run_workflow._run_node = real_run_node             # type: ignore[assignment]
                run_workflow._dependency_node_ready = orig_ready   # type: ignore[assignment]
                run_workflow._runtime_command = orig_rt            # type: ignore[assignment]
            self.assertEqual(rc, 0)
            self.assertEqual(ran, {"spec/component/c", "spec/component/b", "spec/problem/a"})
            written = sorted(
                p.parent.name for p in
                (repo_root / "workspace" / "orchestrations").glob(
                    f"*/{run_workflow.LLM_CONFIG_SNAPSHOT_NAME}"))
            # One per node of the diamond (c, b, a), not just the target.
            self.assertEqual(len(written), 3, msg=written)
            for oid in written:
                self.assertEqual(self._snapshot(repo_root, oid).read_bytes(),
                                 _sample_config("claude").raw)

    def test_the_refusal_names_the_snapshot_only_when_there_is_one(self) -> None:
        """Pre-snapshot records have none, and naming a file the operator will not find is
        worse than naming nothing."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            recorded = {"path": "llm.yaml", "sha256": "sha256:aaa",
                        "overrides": {}}
            out = self._rejection(recorded, repo_root=repo_root)
            assert out is not None
            self.assertIn("is gone", out["detail"])
            self.assertNotIn(run_workflow.LLM_CONFIG_SNAPSHOT_NAME, out["detail"])
            # ...and with a snapshot on disk, the refusal says where to restore it from.
            run_workflow._write_llm_config_snapshot(repo_root, "orch_x", _sample_config())
            out = self._rejection(recorded, repo_root=repo_root)
            assert out is not None
            self.assertIn(run_workflow.LLM_CONFIG_SNAPSHOT_NAME, out["detail"])
            self.assertIn("restore the file from it", out["detail"])

    def test_a_changed_file_refusal_names_the_snapshot_too(self) -> None:
        """The other arm an operator can act on: the file still exists, but not as launched."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            path = repo_root / "llm.yaml"
            path.write_text("defaults:\n  provider: claude_cli\n", encoding="utf-8")
            recorded = {"path": "llm.yaml",
                        "sha256": lc.config_sha256(path), "overrides": {}}
            run_workflow._write_llm_config_snapshot(repo_root, "orch_x", _sample_config())
            path.write_text("defaults:\n  provider: claude_cli\n  model: edited\n",
                            encoding="utf-8")
            out = self._rejection(recorded, repo_root=repo_root,
                                  effective_sha256=lc.config_sha256(path))
            assert out is not None
            self.assertIn("has changed since launch", out["detail"])
            self.assertIn(run_workflow.LLM_CONFIG_SNAPSHOT_NAME, out["detail"])

    def test_the_default_and_a_relative_flag_both_resolve_against_repo_root(self) -> None:
        """Every other path this driver resolves is repo-root-relative. Resolving either of
        these against the CWD would run one file and record a spelling naming another."""
        for extra, expected_path in (
            ([], "llm.yaml"),
            (["--llm-config", "mine.yaml"], "mine.yaml"),
        ):
            with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as cwd:
                repo_root = Path(tmp)
                self._seed(repo_root)
                for root, which in ((repo_root, "repo-root"), (Path(cwd), "cwd")):
                    for name in ("llm.yaml", "mine.yaml"):
                        (root / name).write_text(
                            f"defaults:\n  provider: claude_cli\n  model: from-{which}\n",
                            encoding="utf-8")
                original = os.getcwd()
                os.chdir(cwd)
                try:
                    _, kw, _, _ = self._run(repo_root, extra, oid="orch_cwd")
                finally:
                    os.chdir(original)
                self.assertEqual(kw["llm_config"].defaults.model, "from-repo-root",
                                 msg=str(extra))
                self.assertEqual(self._invocation()["llm_config_path"], expected_path)

    def test_a_config_outside_the_repo_is_recorded_absolutely(self) -> None:
        """A relative spelling for a file outside `repo_root` would be re-joined to the root on
        resume and resolve to a different file, or to nothing."""
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as elsewhere:
            repo_root = Path(tmp)
            self._seed(repo_root)
            cfg = Path(elsewhere) / "mine.yaml"
            cfg.write_text("defaults:\n  provider: claude_cli\n  model: outside\n",
                           encoding="utf-8")
            _, kw, _, _ = self._run(repo_root, ["--llm-config", str(cfg)], oid="orch_out")
            self.assertEqual(kw["llm_config"].defaults.model, "outside")
            recorded = self._invocation()["llm_config_path"]
            self.assertTrue(Path(recorded).is_absolute(), msg=recorded)
            self.assertEqual(Path(recorded).resolve(), cfg.resolve())

    def test_the_default_configuration_is_gitignored(self) -> None:
        """`./llm.yaml` is the operator's own file. Tracking it would force everyone who edits
        which model runs which leaf to carry a permanent diff — and a run that leaves the tree
        dirty trips the workflow's own cleanliness checks. ANCHORED, deliberately against this
        file's house style: the contract is repo-root-relative, and an unanchored pattern would
        silently ignore a committed `llm.yaml` anywhere in the tree."""
        lines = [ln.strip() for ln in
                 (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()]
        self.assertIn(f"/{lc.DEFAULT_CONFIG_FILENAME}", lines)
        self.assertNotIn(lc.DEFAULT_CONFIG_FILENAME, lines)

    def test_the_closure_and_entry_gates_are_the_same_predicate(self) -> None:
        """Twin gates: both call sites must reach `_llm_config_resume_rejection`, or a closure
        member could resume onto a config the entry gate would have refused."""
        import ast
        src = Path(run_workflow.__file__).read_text(encoding="utf-8")
        calls = [n for n in ast.walk(ast.parse(src))
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == "_llm_config_resume_rejection"]
        self.assertGreaterEqual(len(calls), 3)   # entry + dependency member + target


if __name__ == "__main__":
    unittest.main()
