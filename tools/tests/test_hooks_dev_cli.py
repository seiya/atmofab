#!/usr/bin/env python3
"""The DEV entrypoint (`tools/hooks/dev_cli.py`), issue #102.

Two properties, and they pull in opposite directions on purpose: it must refuse the two
operator-safety commands, and it must refuse NOTHING else — including when its input is
malformed, because the session it guards is the one an operator edits these hooks from.
"""

from __future__ import annotations

import ast
import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tools.hooks import dev_cli
from tools.hooks.adapters.claude import ClaudeHookAdapter
from tools.hooks.adapters.codex import CodexHookAdapter
from tools.hooks.common import HookDecision, HookDecisionAction, HookEventName

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run(backend: str, event: str, command, extra_env=None) -> tuple[int, str, str]:
    payload = {"tool_name": "Bash", "tool_input": {"command": command}}
    out, err = io.StringIO(), io.StringIO()
    env = {k: v for k, v in os.environ.items() if k != "METDSL_WORKFLOW_EXEC_MODE"}
    env.update(extra_env or {})
    with patch.dict(os.environ, env, clear=True):
        with redirect_stdout(out), redirect_stderr(err):
            code = dev_cli.main(
                ["--backend", backend, "--event", event, "--input-json", json.dumps(payload)]
            )
    return code, out.getvalue(), err.getvalue()


class DevCliRefusesTheTwoOperatorSafetyCommands(unittest.TestCase):
    def test_git_reset_hard_is_refused(self) -> None:
        code, out, err = _run("claude", "PreToolUse", "git reset --hard HEAD~1")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["decision"], "block")
        self.assertIn("git reset --hard is forbidden", err)

    def test_verify_bypass_flag_is_refused_in_dev_mode(self) -> None:
        code, _out, err = _run("claude", "PreToolUse", "python3 tools/x.py --force-pass")
        self.assertEqual(code, 2)
        self.assertIn("--force-pass", err)

    def test_verify_bypass_flag_is_not_refused_outside_dev_mode(self) -> None:
        code, _out, _err = _run(
            "claude", "PreToolUse", "python3 tools/x.py --force-pass",
            extra_env={"METDSL_WORKFLOW_EXEC_MODE": "workflow"})
        self.assertEqual(code, 0)


class DevCliRefusesNothingElse(unittest.TestCase):
    """The direction that matters more. Each row is a shape that must NOT refuse."""

    def test_ordinary_commands_and_odd_inputs_are_allowed(self) -> None:
        cases = [
            ("claude", "PreToolUse", "echo hello"),
            ("claude", "PreToolUse", "cat ~/.claude.json"),      # a LEAF policy
            ("claude", "PreToolUse", "cat tools/hooks/cli.py"),  # a LEAF policy
            ("claude", "PreToolUse", ""),
            ("claude", "PreToolUse", None),
            ("claude", "PreToolUse", {"nested": "shape"}),
            ("claude", "Stop", "git reset --hard HEAD~1"),       # not a command event
            ("claude", "UserPromptSubmit", "git reset --hard HEAD~1"),
            ("codex", "session_start", "git reset --hard HEAD~1"),
        ]
        for backend, event, command in cases:
            with self.subTest(backend=backend, event=event, command=command):
                code, _out, _err = _run(backend, event, command)
                self.assertEqual(code, 0)

    def test_malformed_payloads_are_allowed(self) -> None:
        for raw in ("", "   ", "not json", "[]", "null", '{"tool_input": 5}'):
            with self.subTest(raw=raw):
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    code = dev_cli.main(
                        ["--backend", "claude", "--event", "PreToolUse", "--input-json", raw])
                self.assertEqual(code, 0, msg=(out.getvalue(), err.getvalue()))


class DevCliEncodingMatchesTheAdapters(unittest.TestCase):
    """`dev_cli` re-implements the BLOCK encodings rather than importing the adapters
    (that import is what the module's boundary forbids). This is the coupling that
    keeps the copy honest: the real adapters are asked for the same decision and the
    bytes are compared."""

    REASON = "blocked by common hook policy: git reset --hard is forbidden"

    def _decision(self) -> HookDecision:
        return HookDecision(
            action=HookDecisionAction.BLOCK, reason=self.REASON, continue_processing=False)

    def test_claude_pre_tool_use_block_is_byte_identical(self) -> None:
        want = ClaudeHookAdapter().encode_decision(
            self._decision(), event_name=HookEventName.PRE_COMMAND_EXECUTE)
        got = dev_cli._encode_block("claude", "pretooluse", self.REASON)
        self.assertEqual(got, want)

    def test_codex_pre_tool_use_block_is_byte_identical(self) -> None:
        want = CodexHookAdapter().encode_decision(
            self._decision(), event_name=HookEventName.PRE_COMMAND_EXECUTE)
        got = dev_cli._encode_block("codex", "pre_tool_use", self.REASON)
        self.assertEqual(got, want)

    def test_codex_permission_request_deny_is_byte_identical(self) -> None:
        want = CodexHookAdapter().encode_decision(
            self._decision(), event_name=HookEventName.PERMISSION_REQUEST)
        got = dev_cli._encode_block("codex", "permission_request", self.REASON)
        self.assertEqual(got, want)


class DevCliImportBoundary(unittest.TestCase):
    """The boundary is the module's purpose, so it is pinned rather than asked for.

    Read as SOURCE, not by inspecting a loaded module: `tools.hooks.cli` may already be
    in `sys.modules` because another test imported it, which would make an
    import-observing check pass for the wrong reason.
    """

    FORBIDDEN = ("tools.hooks.cli", "tools.hooks.common", "tools.hooks.adapters",
                 "tools.orchestration_runtime")

    def test_dev_cli_imports_only_stdlib_and_operator_safety(self) -> None:
        tree = ast.parse((REPO_ROOT / "tools" / "hooks" / "dev_cli.py").read_text(
            encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        repo_imports = {name for name in imported if name.startswith("tools.")}
        self.assertEqual(repo_imports, {"tools.hooks.operator_safety"})
        for name in self.FORBIDDEN:
            for spelling in imported:
                self.assertFalse(
                    spelling == name or spelling.startswith(name + "."),
                    f"dev_cli must not import {spelling}")

    def test_operator_safety_imports_only_stdlib(self) -> None:
        """The one module `dev_cli` does import carries the same obligation, or the
        boundary is one hop long."""
        tree = ast.parse((REPO_ROOT / "tools" / "hooks" / "operator_safety.py").read_text(
            encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        self.assertEqual({n for n in imported if n.startswith("tools.")}, set())

    def test_dev_cli_runs_with_the_leaf_entrypoint_unimportable(self) -> None:
        """The property the boundary buys, executed rather than argued: with
        `tools/hooks/cli.py` replaced by a file that raises on import, the dev hook
        still answers. This is the shape that locked a session out on 2026-08-26."""
        import shutil
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp)
            for rel in ("tools/__init__.py", "tools/hooks/__init__.py",
                        "tools/hooks/dev_cli.py", "tools/hooks/operator_safety.py"):
                (fake / rel).parent.mkdir(parents=True, exist_ok=True)
                # `tools/` is a namespace package here - no `__init__.py` on disk.
                if (REPO_ROOT / rel).exists():
                    shutil.copy(REPO_ROOT / rel, fake / rel)
            (fake / "tools" / "hooks" / "cli.py").write_text(
                "raise RuntimeError('half-applied edit')\n", encoding="utf-8")
            (fake / "tools" / "hooks" / "common.py").write_text(
                "raise RuntimeError('half-applied edit')\n", encoding="utf-8")
            payload = json.dumps(
                {"tool_name": "Bash", "tool_input": {"command": "git reset --hard HEAD~1"}})
            proc = subprocess.run(
                [sys.executable, "-m", "tools.hooks.dev_cli", "--backend", "claude",
                 "--event", "PreToolUse", "--input-json", payload],
                capture_output=True, text=True, cwd=str(fake),
                env={"PATH": "/usr/bin:/bin", "HOME": tmp, "PYTHONPATH": str(fake)})
            self.assertEqual(proc.returncode, 2, msg=proc.stderr)
            self.assertIn("git reset --hard is forbidden", proc.stderr)


if __name__ == "__main__":
    unittest.main()
