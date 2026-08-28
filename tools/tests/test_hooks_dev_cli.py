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


class DevCliRefusesASleepBasedWait(unittest.TestCase):
    """The DEV-only session-hygiene rule (`tools/hooks/dev_session_hygiene.py`).

    A review subagent that had been told in its own prompt not to poll left 144 shells and
    `sleep` children on a shared machine in 36 minutes and returned no report. The prompt
    already said it; three earlier accidents happened with the rule in the prompt too. So it
    moved into the layer that can refuse.

    Driven through the ENTRYPOINT, not only the predicate, because the mutation sweep on this
    repository has twice found a gate whose helper was pinned while the call was deletable.

    The rule module is imported LAZILY inside the two rows that need it. At module level, a
    mutant that renames it turns the whole file into a COLLECTION ERROR, and the naming row in
    `DevCliImportBoundary` — the row whose entire job is to notice that rename — never runs.
    Measured: it read as "killed" while observing nothing, which is the shape this repository's
    review skill warns a `FAILED`-line scorer reads as green.
    """

    #: Split so this FILE never carries a refusable command literal — the rule matches raw text,
    #: and a test file quoting it would be refused if it were ever read as a command.
    _W = "sl" + "eep"

    def _run(self, command: str) -> int:
        return dev_cli.main([
            "--backend", "claude", "--event", "PreToolUse",
            "--input-json", json.dumps({"tool_input": {"command": command}})])

    def test_the_shapes_that_actually_appeared_are_refused(self) -> None:
        """Every row is a spelling observed in the incident or an obvious neighbour of one.

        `command <wait>` is the one that matters most: it is how the harness's own block on a
        foreground wait was got past, so a rule that missed it would refuse only the spelling
        nobody used."""
        for command in (f"{self._W} 60",
                        f"command {self._W} 10",
                        f"eval '{self._W} 1799; true'",
                        f"/bin/{self._W} 30",
                        f"/usr/bin/{self._W} 30",
                        # A leading backslash defeats a shell function or alias and is the one
                        # prefix the separator branch cannot reach — measured, and the row exists
                        # because the sweep found that group unwitnessed.
                        f"\\{self._W} 2",
                        f"nohup {self._W} 100 &",
                        f"until [ -s out ]; do {self._W} 5; done",
                        f"for i in $(seq 1 30); do {self._W} 10; done",
                        f"cat f; {self._W} $N"):
            with self.subTest(command=command):
                self.assertEqual(2, self._run(command))

    def test_the_word_without_a_duration_is_not_refused(self) -> None:
        """THE OVER-REFUSING DIRECTION, which is this repository's recorded default error.

        Every row here is a command an operator or a reviewer runs while dealing with the very
        problem the rule exists for — inspecting or killing the processes. A rule that refused
        them would block the cleanup, and the first version of a rule like this would have: a
        bare substring match on the word catches all of them."""
        for command in (f"pkill -f {self._W}",
                        f"grep {self._W} tools/hooks/dev_session_hygiene.py",
                        f'ps -eo args | grep -cE "^{self._W} [0-9]"',
                        f"ps aux | grep {self._W}",
                        f"rg -n '{self._W}' docs/",
                        f"ls {self._W}y_dir",
                        f"{self._W}less 5",
                        # A longer word ENDING in the wait, which is what the pattern's
                        # separator class exists to stop matching. A mechanism sweep found
                        # that class survivable — no row distinguished it — so these two are
                        # the rows it was missing, not decoration.
                        f"over{self._W} 5",
                        f"my_{self._W} 10",
                        "python3 -m pytest -q"):
            with self.subTest(command=command):
                self.assertEqual(0, self._run(command))

    def test_the_refusal_names_the_alternatives_in_reachability_order(self) -> None:
        """A remedy is read in order and the first line is the one followed.

        The most likely truth is that the session is polling work the harness already tracks, so
        that comes first; the tool for a condition it cannot see comes second; the escape hatch
        for a genuine pause comes last. Pinned by ORDER, not by presence, because a message
        carrying all three in the wrong order sends the reader to the rarest cause."""
        from tools.hooks import dev_session_hygiene  # lazy: see the class docstring
        reason = dev_session_hygiene.polling_wait_violation(f"{self._W} 60")
        assert reason is not None
        text = reason[0]
        self.assertLess(text.index("DO NOT POLL"), text.index("Monitor tool"))
        self.assertLess(text.index("Monitor tool"), text.index("another terminal"))

    def test_the_audit_detail_names_the_policy(self) -> None:
        """The record has to say which rule fired, or a refusal is unattributable after the
        fact — the same reason `operator_safety` carries a `policy` key."""
        from tools.hooks import dev_session_hygiene  # lazy: see the class docstring
        violation = dev_session_hygiene.polling_wait_violation(f"{self._W} 5")
        assert violation is not None
        self.assertEqual(violation[1]["policy"], "forbid_sleep_wait_in_agent_session")

    def test_the_leaf_path_does_not_carry_this_rule(self) -> None:
        """DEV-ONLY, and that is a decision rather than an omission.

        A leaf that sleeps wastes its own budget and gets no closer to reporting its task done,
        which `AGENTS.md` §Development premises puts out of the defended set. Pinned by reading
        the leaf-facing sources, so importing it there fails here rather than silently widening
        a leaf policy."""
        for rel in ("tools/hooks/cli.py", "tools/hooks/common.py"):
            source = (REPO_ROOT / rel).read_text(encoding="utf-8")
            self.assertNotIn("dev_session_hygiene", source, rel)


class DevCliReadsTheCodexPayloadShape(unittest.TestCase):
    """The nested payload shape, which every other test in this file skips.

    A witness census found `_payload_field`'s `payload["payload"]` fallback and
    `_extract_command`'s top-level `"command"` key unwitnessed: every dev-cli test used
    the flat claude shape. The nested one is the codex shape that `cli.py::_inner_payload`
    exists for, and `.codex/hooks.json` registers this entrypoint — so a wrong fallback
    would mean the two operator-safety policies silently never fire for a codex operator,
    which is exactly the failure that looks like nothing at all.
    """

    def _run(self, payload) -> int:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            return dev_cli.main(["--backend", "codex", "--event", "PreToolUse",
                                 "--input-json", json.dumps(payload)])

    def test_the_command_is_found_in_each_shape_the_backends_send(self) -> None:
        for label, payload in (
            ("flat tool_input", {"tool_input": {"command": 'git reset --hard HEAD~1'}}),
            ("nested tool_input", {"payload": {"tool_input": {"command": 'git reset --hard HEAD~1'}}}),
            ("flat command", {"command": 'git reset --hard HEAD~1'}),
            ("nested command", {"payload": {"command": 'git reset --hard HEAD~1'}}),
        ):
            with self.subTest(shape=label):
                self.assertEqual(self._run(payload), 2)

    def test_the_same_shapes_allow_an_ordinary_command(self) -> None:
        """The control: each shape above must be able to say 0, or the rows prove only
        that this entrypoint refuses everything it cannot parse."""
        for label, payload in (
            ("flat tool_input", {"tool_input": {"command": "echo hello"}}),
            ("nested tool_input", {"payload": {"tool_input": {"command": "echo hello"}}}),
            ("flat command", {"command": "echo hello"}),
            ("nested command", {"payload": {"command": "echo hello"}}),
        ):
            with self.subTest(shape=label):
                self.assertEqual(self._run(payload), 0)


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


class DevCliWrapperCommandsExecute(unittest.TestCase):
    """The committed DEV wrapper STRINGS, run as a shell the way the harness runs them.

    The leaf wrappers have had this since before the split
    (`test_hooks_cli.py::test_hooks_json_command_works_from_subdirectory` and its
    fail-fast sibling). The dev ones had nothing: when issue #102 repointed
    `.claude/settings.json` and `.codex/hooks.json` at this module, those two tests
    followed the LEAF file, and no test executed a dev wrapper at all — measured, every
    one of the 41 places that read `.claude/settings.json` does so without a shell.

    What that leaves open is the exact failure this module exists to prevent: a quoting
    or `PYTHONPATH` break in the dev wrapper refuses every tool call in an operator's
    session, and it would arrive with the suite green. It happened once already on
    2026-08-26, from the other direction.

    The assertion is a REFUSAL, not `rc 0` with empty stdout. A broken wrapper exits
    non-zero with an empty stdout too, and an allow-shaped assertion is satisfied by a
    program that cannot refuse anything — the trap `38c2711` recorded when the codex
    wrapper test silently moved onto `dev_cli`.
    """

    @staticmethod
    def _dev_commands():
        repo_root = REPO_ROOT
        found = []
        for rel in (".claude/settings.json", ".codex/hooks.json"):
            doc = json.loads((repo_root / rel).read_text(encoding="utf-8"))
            for event, blocks in (doc.get("hooks") or {}).items():
                for block in blocks:
                    for hook in block.get("hooks", []):
                        found.append((rel, event, hook["command"]))
        return found

    def test_every_committed_dev_wrapper_refuses_through_a_real_shell(self) -> None:
        commands = self._dev_commands()
        self.assertTrue(commands, "no dev wrapper command found to execute")
        for rel, event, command in commands:
            if "PreToolUse" not in event and "PermissionRequest" not in event:
                continue
            with self.subTest(source=rel, event=event):
                payload = {"tool_name": "Bash", "tool_input": {"command": 'git reset --hard HEAD~1'}}
                # PYTHONPATH is STRIPPED: the wrapper sets it itself, and inheriting
                # the runner's copy is what made a mutation deleting the wrapper's
                # assignment survive — the module resolved through the ambient value
                # instead. The rows then proved nothing about the wrapper.
                env = {k: v for k, v in os.environ.items()
                       if not k.startswith("METDSL_") and k != "PYTHONPATH"}
                # FROM A SUBDIRECTORY, like the leaf wrapper's own test: run from the
                # repository root and `python3 -m` resolves the module through the cwd,
                # so the wrapper's `PYTHONPATH=` assignment is doing nothing observable
                # and a mutation deleting it survives (measured). An operator's session
                # is not always at the root.
                proc = subprocess.run(
                    command, cwd=str(REPO_ROOT / "tools"), env=env, text=True,
                    capture_output=True, input=json.dumps(payload), shell=True)
                # `PermissionRequest` carries its deny in the body at rc 0; the rest
                # refuse with rc 2. Either way the DECISION must arrive.
                self.assertIn("block" if proc.returncode == 2 else "deny", proc.stdout,
                              msg=f"{proc.returncode} / {proc.stdout!r} / {proc.stderr!r}")

    def test_a_dev_wrapper_allows_an_ordinary_command_through_a_real_shell(self) -> None:
        """The control. Without it the rows above hold for a wrapper that is simply
        broken, since a wrapper that cannot run refuses everything."""
        rel, event, command = next(
            (r, e, c) for r, e, c in self._dev_commands() if "PreToolUse" in e)
        payload = {"tool_name": "Bash", "tool_input": {"command": "echo hello"}}
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("METDSL_") and k != "PYTHONPATH"}
        proc = subprocess.run(
            command, cwd=str(REPO_ROOT / "tools"), env=env, text=True,
            capture_output=True, input=json.dumps(payload), shell=True)
        self.assertEqual(proc.returncode, 0, msg=(rel, event, proc.stderr))


def _dev_cli_repo_imports() -> set[str]:
    """The `tools.` modules the DEV entrypoint imports, read from its SOURCE.

    Source, not a loaded module: another test may already have imported something, which would
    make an import-observing read answer for the wrong reason."""
    tree = ast.parse((REPO_ROOT / "tools" / "hooks" / "dev_cli.py").read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name.startswith("tools."))
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("tools."):
            found.add(node.module)
    return found


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
        repo_imports = _dev_cli_repo_imports()
        self.assertEqual(
            repo_imports,
            {"tools.hooks.operator_safety", "tools.hooks.dev_session_hygiene"},
            "the DEV entrypoint may import only its stdlib-only rule modules; anything else "
            "can refuse the operator out of the session they are editing in")
        for name in self.FORBIDDEN:
            for spelling in imported:
                self.assertFalse(
                    spelling == name or spelling.startswith(name + "."),
                    f"dev_cli must not import {spelling}")

    def test_a_dev_only_rule_module_is_named_dev_something(self) -> None:
        """The naming convention, checked rather than remembered.

        `dev_cli.py` already carried the prefix; the second dev-only module did not until it was
        renamed, and by then five files spelled the old name. The rule is derivable, so it is
        derived: a module the DEV entrypoint imports and the LEAF entrypoint does not is dev-only
        and must say so in its name. A module BOTH import (`operator_safety`) must not, because
        the prefix would then be a lie about its audience — that half is what makes this a check
        and not a substring rule.

        Read from source on both sides for the same reason the boundary rows do: another test may
        already have imported something, which would answer for the wrong reason."""
        leaf_imports: set[str] = set()
        for rel in ("tools/hooks/cli.py", "tools/hooks/common.py"):
            tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    leaf_imports.update(a.name for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    leaf_imports.add(node.module)
        dev_only = _dev_cli_repo_imports() - leaf_imports
        shared = _dev_cli_repo_imports() & leaf_imports
        self.assertTrue(dev_only, "the DEV entrypoint has no module of its own any more; if that "
                                  "is deliberate, delete this row and say why")
        for module in sorted(dev_only):
            name = module.rsplit(".", 1)[-1]
            self.assertTrue(
                name.startswith("dev_"),
                f"{module} is imported by tools/hooks/dev_cli.py and by no leaf entrypoint, so it "
                f"is DEV-ONLY and its file name must start with `dev_` (see docs/HOOKS.md). "
                f"Rename it, or — if it is meant to be shared — import it from the leaf path too.")
        for module in sorted(shared):
            name = module.rsplit(".", 1)[-1]
            self.assertFalse(
                name.startswith("dev_"),
                f"{module} is imported by BOTH entrypoints, so a `dev_` prefix misstates its "
                f"audience: a reader would take a rule that also binds a leaf for one that does "
                f"not.")

    def test_the_rule_modules_import_only_stdlib(self) -> None:
        """Every module `dev_cli` imports carries the same obligation, or the boundary is
        one hop long. Enumerated from the entrypoint's OWN imports rather than listed here,
        so a third rule module cannot be added without arriving in this check."""
        modules = _dev_cli_repo_imports()
        self.assertTrue(modules)
        for module in sorted(modules):
            rel = Path(*module.split(".")).with_suffix(".py")
            tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"))
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module)
            self.assertEqual({n for n in imported if n.startswith("tools.")}, set(), module)

    def test_dev_cli_runs_with_the_leaf_entrypoint_unimportable(self) -> None:
        """The property the boundary buys, executed rather than argued: with
        `tools/hooks/cli.py` replaced by a file that raises on import, the dev hook
        still answers. This is the shape that locked a session out on 2026-08-26."""
        import shutil
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp)
            # The rule modules are DERIVED from the entrypoint's own imports, not listed: a
            # third one added without arriving here would make this row fail on an import
            # error and read as a boundary breach rather than a stale fixture, which is how
            # it failed once.
            rule_modules = [
                str(Path(*m.split("."))) + ".py"
                for m in _dev_cli_repo_imports()
            ]
            for rel in ["tools/__init__.py", "tools/hooks/__init__.py",
                        "tools/hooks/dev_cli.py", *rule_modules]:
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
