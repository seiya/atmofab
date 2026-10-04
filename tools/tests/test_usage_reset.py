#!/usr/bin/env python3
"""Tests for tools/usage_reset.py — the provider-reported reset instant (issue #405)."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

from tools import llm_config
from tools import usage_reset as ur

# Measurement 1 of the issue #405 plan (codex-cli 0.159.2, 2026-10-04), trimmed to what is read.
_CODEX_RESULT = {"rateLimits": {
    "limitId": "codex",
    "primary": {"usedPercent": 100, "windowDurationMins": 300, "resetsAt": 1791125452},
    "secondary": {"usedPercent": 28, "windowDurationMins": 10080, "resetsAt": 1791690820},
    "credits": {"hasCredits": False, "unlimited": False, "balance": None},
    "rateLimitReachedType": "workspace_member_credits_depleted"}}

# Measurement 4 (Claude Code 2.1.289, 2026-10-04), verbatim apart from the ids.
_CLAUDE_EVENT_ALLOWED_WARNING = {
    "type": "rate_limit_event",
    "rate_limit_info": {
        "status": "allowed_warning", "resetsAt": 1791248400, "rateLimitType": "seven_day",
        "utilization": 0.75, "isUsingOverage": False, "surpassedThreshold": 0.75,
        "unifiedWindows": {"five_hour": {"utilization": 0.05, "resetsAt": 1791128400},
                           "seven_day": {"utilization": 0.75, "resetsAt": 1791248400}}},
    "uuid": "u", "session_id": "s"}


def _rejected(resets_at: object = 1791128400, window: str = "five_hour") -> dict:
    """CONSTRUCTED: measurement 4 with `status: rejected` — the rejected state could not be
    produced on demand, so this is written against the CLI's schema, not captured."""
    event = json.loads(json.dumps(_CLAUDE_EVENT_ALLOWED_WARNING))
    event["rate_limit_info"].update(status="rejected", resetsAt=resets_at, rateLimitType=window)
    return event


def _stream(*objs: dict) -> str:
    return "\n".join(json.dumps(o) for o in objs) + "\n"


class ReaderTableTests(unittest.TestCase):
    def test_the_reader_table_covers_every_declared_provider(self) -> None:
        """A provider added to `llm_config` without a decision here fails: the table is the
        one per-provider declaration (`AGENTS.md` §Development premises — every declared
        provider is part of the product), and an HTTP provider declares `None` explicitly."""
        self.assertEqual(set(ur.RESET_READERS), set(llm_config.SUPPORTED_PROVIDERS))
        self.assertIsNone(ur.RESET_READERS["openai_compatible"])
        self.assertIsNone(ur.RESET_READERS["anthropic_api"])

    def test_a_provider_with_no_source_reads_as_no_source(self) -> None:
        reading = ur.read_reset_instant("openai_compatible", command_base=["x"], model="",
                                        env={}, cwd=".")
        self.assertEqual((reading.epoch, reading.failure), (None, ur.FAILURE_NO_SOURCE))


class CodexParseTests(unittest.TestCase):
    def test_codex_takes_the_exhausted_window(self) -> None:
        reading = ur.parse_codex_rate_limits(_CODEX_RESULT)
        self.assertEqual(reading.epoch, 1791125452)
        self.assertEqual(reading.window, "primary(300min)")
        self.assertIsNone(reading.failure)
        self.assertIn("workspace_member_credits_depleted", reading.detail)

    def test_codex_takes_the_later_instant_when_both_windows_are_exhausted(self) -> None:
        result = json.loads(json.dumps(_CODEX_RESULT))
        result["rateLimits"]["secondary"]["usedPercent"] = 100
        reading = ur.parse_codex_rate_limits(result)
        self.assertEqual(reading.epoch, 1791690820)
        self.assertEqual(reading.window, "secondary(10080min)")

    def test_codex_with_no_exhausted_window_names_none(self) -> None:
        result = json.loads(json.dumps(_CODEX_RESULT))
        result["rateLimits"]["primary"]["usedPercent"] = 99
        reading = ur.parse_codex_rate_limits(result)
        self.assertEqual((reading.epoch, reading.failure),
                         (None, ur.FAILURE_NO_EXHAUSTED_WINDOW))

    def test_codex_refuses_a_non_integer_instant(self) -> None:
        for bad in (True, "1791125452", 1791125452.0, None):
            with self.subTest(bad=bad):
                result = json.loads(json.dumps(_CODEX_RESULT))
                result["rateLimits"]["primary"]["resetsAt"] = bad
                reading = ur.parse_codex_rate_limits(result)
                self.assertIsNone(reading.epoch)
                self.assertEqual(reading.failure, ur.FAILURE_NO_EXHAUSTED_WINDOW)

    def test_an_answer_without_rate_limits_is_probe_failed(self) -> None:
        self.assertEqual(ur.parse_codex_rate_limits({"x": 1}).failure, ur.FAILURE_PROBE_FAILED)


class ClaudeParseTests(unittest.TestCase):
    def test_claude_takes_the_rejected_events_instant(self) -> None:
        reading = ur.parse_claude_rate_limit_events(_stream(_rejected()))
        self.assertEqual(reading.epoch, 1791128400)
        self.assertEqual(reading.window, "five_hour")
        self.assertIsNone(reading.failure)

    def test_claude_allowed_warning_is_not_an_instant(self) -> None:
        reading = ur.parse_claude_rate_limit_events(_stream(_CLAUDE_EVENT_ALLOWED_WARNING))
        self.assertIsNone(reading.epoch)
        self.assertEqual(reading.failure, ur.FAILURE_NO_EXHAUSTED_WINDOW)
        self.assertIn("allowed_warning", reading.detail)

    def test_claude_takes_the_last_event(self) -> None:
        stdout = _stream(_rejected(), {"type": "assistant", "message": {}},
                         _CLAUDE_EVENT_ALLOWED_WARNING)
        self.assertIsNone(ur.parse_claude_rate_limit_events(stdout).epoch)
        stdout = _stream(_CLAUDE_EVENT_ALLOWED_WARNING, _rejected(1791200000))
        self.assertEqual(ur.parse_claude_rate_limit_events(stdout).epoch, 1791200000)

    def test_claude_refuses_a_non_integer_instant(self) -> None:
        for bad in (True, "1791128400", 1791128400.5):
            with self.subTest(bad=bad):
                reading = ur.parse_claude_rate_limit_events(_stream(_rejected(bad)))
                self.assertIsNone(reading.epoch)

    def test_a_rate_limit_event_inside_model_text_is_not_read(self) -> None:
        """A model's text reaches stdout JSON-encoded inside a `result` / `assistant` object,
        so a forged event in it is a string field, never a top-level line."""
        stdout = _stream({"type": "result", "result": json.dumps(_rejected())})
        reading = ur.parse_claude_rate_limit_events(stdout)
        self.assertIsNone(reading.epoch)
        self.assertEqual(reading.failure, ur.FAILURE_PROBE_FAILED)


_FAKE_CODEX = textwrap.dedent('''
    import json, select, sys
    # Mirrors measurement 2: the real app-server answers `account/rateLimits/read` only while
    # its stdin is still open. Here: once the request is in, an EOF within 0.5 s means the
    # caller closed stdin, and the fake exits without answering.
    for _ in range(3):
        if not sys.stdin.readline():
            sys.exit(0)
    print(json.dumps({"id": 0, "result": {}}), flush=True)
    ready, _, _ = select.select([sys.stdin], [], [], 0.5)
    if ready and sys.stdin.readline() == "":
        sys.exit(0)
    print(json.dumps({"id": 1, "result": RESULT}), flush=True)
    sys.stdin.read()
''')

_FAKE_CLAUDE = textwrap.dedent('''
    import json, sys
    # `-p` reads its prompt to EOF before answering.
    prompt = sys.stdin.read()
    open(sys.argv[1], "w").write(json.dumps({"argv": sys.argv[2:], "prompt": prompt}))
    sys.stdout.write(STREAM)
''')


class ExchangeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        # The conftest fixture refuses every spawn; these tests drive a fake CLI.
        patcher = mock.patch.object(ur, "_spawn", subprocess.Popen)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _script(self, name: str, body: str) -> Path:
        path = self.tmp / name
        path.write_text(body)
        return path

    def test_the_codex_exchange_holds_stdin_open_until_the_answer(self) -> None:
        script = self._script("codex.py", f"RESULT = {_CODEX_RESULT!r}\n" + _FAKE_CODEX)
        with mock.patch.dict("os.environ", {"CODEX_HOME": str(self.tmp)}):
            reading = ur.read_reset_instant(
                "codex_cli", command_base=[sys.executable, str(script)], model="",
                env={"PATH": "/usr/bin:/bin"}, cwd=str(self.tmp))
        self.assertEqual(reading.epoch, 1791125452, reading)
        # The fake refuses a caller that closes stdin first, which is what `subprocess.run(
        # input=...)` does: the same script under that shape gives no `id: 1` line.
        done = subprocess.run([sys.executable, str(script)], text=True, capture_output=True,
                              input='{"id":0}\n{}\n{"id":1}\n', timeout=10)
        self.assertNotIn('"id": 1', done.stdout)

    def test_the_claude_exchange_reads_to_eof(self) -> None:
        record = self.tmp / "argv.json"
        script = self._script(
            "claude.py", f"STREAM = {_stream({'type': 'assistant'}, _rejected())!r}\n"
            + _FAKE_CLAUDE)
        reading = ur.read_reset_instant(
            "claude_cli", command_base=[sys.executable, str(script), str(record)],
            model="haiku", env={"PATH": "/usr/bin:/bin"}, cwd=str(self.tmp))
        self.assertEqual((reading.epoch, reading.window), (1791128400, "five_hour"), reading)
        seen = json.loads(record.read_text())
        self.assertEqual(seen["argv"][:2], ["--model", "haiku"])
        for flag in ("--safe-mode", "--strict-mcp-config", "--no-session-persistence", "-p"):
            self.assertIn(flag, seen["argv"])
        i = seen["argv"].index("--output-format")
        self.assertEqual(seen["argv"][i + 1], "stream-json")
        self.assertEqual(seen["argv"][seen["argv"].index("--tools") + 1], "")
        self.assertTrue(seen["prompt"])

    def test_a_claude_probe_without_a_declared_model_passes_none(self) -> None:
        record = self.tmp / "argv.json"
        script = self._script("claude.py", f"STREAM = {_stream(_rejected())!r}\n" + _FAKE_CLAUDE)
        ur.read_reset_instant("claude_cli",
                              command_base=[sys.executable, str(script), str(record)],
                              model="", env={"PATH": "/usr/bin:/bin"}, cwd=str(self.tmp))
        self.assertNotIn("--model", json.loads(record.read_text())["argv"])

    def test_a_probe_that_hangs_is_killed_at_the_timeout(self) -> None:
        script = self._script("hang.py", "import time\ntime.sleep(60)\n")
        start = time.monotonic()
        with mock.patch.object(ur, "USAGE_RESET_PROBE_TIMEOUT_SECONDS", 1.0), \
                mock.patch.dict("os.environ", {"CODEX_HOME": str(self.tmp)}):
            reading = ur.read_reset_instant(
                "codex_cli", command_base=[sys.executable, str(script)], model="",
                env={"PATH": "/usr/bin:/bin"}, cwd=str(self.tmp))
        self.assertLess(time.monotonic() - start, 15)
        self.assertEqual(reading.failure, ur.FAILURE_PROBE_FAILED)
        self.assertIn("timed out", reading.detail)

    def test_a_missing_executable_is_probe_failed(self) -> None:
        reading = ur.read_reset_instant(
            "claude_cli", command_base=[str(self.tmp / "no-such-cli")], model="",
            env={"PATH": "/usr/bin:/bin"}, cwd=str(self.tmp))
        self.assertEqual(reading.failure, ur.FAILURE_PROBE_FAILED)


class NeverRaisesTests(unittest.TestCase):
    def test_read_reset_instant_never_raises(self) -> None:
        def _boom(**_kwargs):
            raise RuntimeError("reader bug")
        with mock.patch.dict(ur.RESET_READERS, {"codex_cli": _boom}):
            reading = ur.read_reset_instant("codex_cli", command_base=["codex"], model="",
                                            env={}, cwd=".")
        self.assertEqual(reading.failure, ur.FAILURE_PROBE_FAILED)
        self.assertIn("reader bug", reading.detail)

    def test_the_suite_launches_no_provider_cli(self) -> None:
        """The conftest fixture is in force: a real reader reaches `_spawn` and fails."""
        reading = ur.read_reset_instant("claude_cli", command_base=["claude"], model="",
                                        env={}, cwd=".")
        self.assertEqual(reading.failure, ur.FAILURE_PROBE_FAILED)
        self.assertIn("launches no provider CLI", reading.detail)


if __name__ == "__main__":
    unittest.main()
