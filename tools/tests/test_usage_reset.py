"""Tests for tools/usage_reset.py — the provider-reported reset instant (issue #405)."""

from __future__ import annotations

import json
import os
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
from tools.tests.private_root_fixture import (
    refuse_provider_probes_for_module,
    restore_provider_probes_for_module,
)


def setUpModule() -> None:
    # Outside pytest conftest does not load, so this module refuses a real reset probe itself
    # (issue #405; `private_root_fixture.refuse_provider_probes_for_module`).
    refuse_provider_probes_for_module(__name__)


def tearDownModule() -> None:
    restore_provider_probes_for_module(__name__)


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

    def test_only_a_rate_limit_event_line_is_read(self) -> None:
        """The `type` decides, not the presence of a `rate_limit_info` member: another
        top-level line carrying one is not the CLI's rate-limit event."""
        other = dict(_rejected(), type="system")
        reading = ur.parse_claude_rate_limit_events(_stream(other))
        self.assertIsNone(reading.epoch)
        self.assertEqual(reading.failure, ur.FAILURE_PROBE_FAILED)

    def test_a_malformed_last_event_supersedes_an_earlier_rejected_one(self) -> None:
        """The LAST event is the reading even when it carries no `rate_limit_info`: an earlier
        `rejected` instant is not resurrected past it."""
        stdout = _stream(_rejected(), {"type": "rate_limit_event"})
        reading = ur.parse_claude_rate_limit_events(stdout)
        self.assertIsNone(reading.epoch)
        self.assertEqual(reading.failure, ur.FAILURE_NO_EXHAUSTED_WINDOW)

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
    import os
    open(os.environ["FAKE_RECORD"], "w").write(os.environ.get("CODEX_HOME", "<unset>"))
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
        record = self.tmp / "codex_home.txt"
        origin = self.tmp / "origin-home"
        start = time.monotonic()
        with mock.patch.dict("os.environ", {"CODEX_HOME": str(origin)}), \
                mock.patch.object(ur, "USAGE_RESET_PROBE_TIMEOUT_SECONDS", 30.0):
            reading = ur.read_reset_instant(
                "codex_cli", command_base=[sys.executable, str(script)], model="",
                env={"PATH": "/usr/bin:/bin", "FAKE_RECORD": str(record)}, cwd=str(self.tmp))
        self.assertEqual(reading.epoch, 1791125452, reading)
        # The read stops at the answer line and closes stdin then (the fake exits on EOF);
        # a read that did not stop there would wait out the deadline.
        self.assertLess(time.monotonic() - start, 10)
        # The leaf's environment carries no CODEX_HOME; the probe reads the ORIGIN home.
        self.assertEqual(record.read_text(), str(origin))
        # The fake refuses a caller that closes stdin first, which is what `subprocess.run(
        # input=...)` does: the same script under that shape gives no `id: 1` line.
        done = subprocess.run([sys.executable, str(script)], text=True, capture_output=True,
                              env={"PATH": "/usr/bin:/bin", "FAKE_RECORD": str(record)},
                              input='{"id":0}\n{}\n{"id":1}\n', timeout=10, check=False)
        self.assertNotIn('"id": 1', done.stdout)

    def test_the_claude_exchange_reads_to_eof(self) -> None:
        record = self.tmp / "argv.json"
        script = self._script(
            "claude.py", f"STREAM = {_stream({'type': 'assistant'}, _rejected())!r}\n"
            + _FAKE_CLAUDE)
        start = time.monotonic()
        with mock.patch.object(ur, "USAGE_RESET_PROBE_TIMEOUT_SECONDS", 30.0):
            reading = ur.read_reset_instant(
                "claude_cli", command_base=[sys.executable, str(script), str(record)],
                model="haiku", env={"PATH": "/usr/bin:/bin"}, cwd=str(self.tmp))
        # `-p` reads its prompt to EOF, so stdin is closed right after the prompt; a probe
        # that held it open would reach the answer only at the deadline.
        self.assertLess(time.monotonic() - start, 10)
        self.assertEqual((reading.epoch, reading.window), (1791128400, "five_hour"), reading)
        seen = json.loads(record.read_text())
        # The WHOLE argv: measurement 4 was taken with exactly these flags (`--verbose` is what
        # puts the event into a `-p` stream-json stream).
        self.assertEqual(seen["argv"], [
            "--model", "haiku", "--safe-mode", "--system-prompt",
            ur._CLAUDE_PROBE_SYSTEM_PROMPT, "--tools", "", "--strict-mcp-config",
            "--disable-slash-commands", "--no-session-persistence",
            "--output-format", "stream-json", "--verbose", "-p"])
        self.assertEqual(seen["prompt"], ur._CLAUDE_PROBE_PROMPT)

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


    def test_a_cli_that_exits_leaving_a_descendant_on_stdout_is_not_waited_out(self) -> None:
        """A CLI process that exits while a descendant in its group still holds stdout: the
        read is abandoned shortly after the exit, not at the deadline, and the descendant is
        killed with the group rather than outliving the call."""
        pidfile = self.tmp / "child.pid"
        script = self._script("leave.sh", textwrap.dedent(f'''
            /usr/bin/tail -f /dev/null &
            echo $! > {pidfile}
            exit 0
        '''))
        start = time.monotonic()
        with mock.patch.object(ur, "USAGE_RESET_PROBE_TIMEOUT_SECONDS", 30.0):
            reading = ur.read_reset_instant(
                "claude_cli", command_base=["/bin/sh", str(script)], model="",
                env={"PATH": "/usr/bin:/bin"}, cwd=str(self.tmp))
        self.assertLess(time.monotonic() - start, 10)
        self.assertEqual(reading.failure, ur.FAILURE_PROBE_FAILED)
        self.assertIn("stdout held open after exit", reading.detail)
        child = int(pidfile.read_text())
        # Reaped by init once killed; a zombie or a missing pid both mean it is gone.
        try:
            os.kill(child, 0)
            stat = Path(f"/proc/{child}/stat").read_text()
            alive = stat.split(") ", 1)[1][:1] != "Z"
        except (ProcessLookupError, FileNotFoundError):
            alive = False
        self.assertFalse(alive, "the descendant outlived the probe")

    def test_a_flooding_cli_is_drained_but_not_retained(self) -> None:
        script = self._script("flood.py", "import sys\nfor _ in range(20000):\n"
                                          "    sys.stdout.write('x' * 99 + '\\n')\n")
        with mock.patch.object(ur, "_STDOUT_RETAIN_MAX_CHARS", 10_000):
            lines, _why = ur._exchange([sys.executable, str(script)], env={"PATH": "/usr/bin"},
                                       cwd=str(self.tmp), send="", close_stdin_after_send=True,
                                       until=lambda _l: False, timeout=30)
        retained = sum(len(line) for line in lines)
        self.assertGreater(20000 * 99, 10_000)          # the probe straddles the cap
        self.assertLessEqual(retained, 10_000 + 99)


class BoundsTests(unittest.TestCase):
    def test_an_absurd_epoch_is_not_an_instant(self) -> None:
        """A JSON integer is unbounded and `epoch - time.time()` overflows past ~1.8e308."""
        for bad in (10 ** 400, -(10 ** 400), 2 ** 53 + 1):
            with self.subTest(bad=bad):
                result = json.loads(json.dumps(_CODEX_RESULT))
                result["rateLimits"]["primary"]["resetsAt"] = bad
                self.assertIsNone(ur.parse_codex_rate_limits(result).epoch)
                self.assertIsNone(ur.parse_claude_rate_limit_events(
                    _stream(_rejected(bad))).epoch)
        self.assertEqual(ur.parse_codex_rate_limits(_CODEX_RESULT).epoch, 1791125452)

    def test_an_id_of_true_is_not_the_answer(self) -> None:
        self.assertTrue(ur._codex_answer_line('{"id": 1, "result": {}}'))
        self.assertFalse(ur._codex_answer_line('{"id": true, "result": {}}'))
        self.assertFalse(ur._codex_answer_line('{"id": 1.0, "result": {}}'))

    def test_the_detail_is_clipped(self) -> None:
        result = json.loads(json.dumps(_CODEX_RESULT))
        result["rateLimits"]["rateLimitReachedType"] = "x" * 5000
        detail = ur.parse_codex_rate_limits(result).detail
        self.assertEqual(len(detail), ur._DETAIL_MAX_CHARS)
        self.assertEqual(ur._DETAIL_MAX_CHARS, 400)   # the bound docs/ORCHESTRATION.md states


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
        """The refusal is in force (conftest under pytest, `setUpModule` under unittest): a
        reader pointed at a CLI that WOULD answer with an instant still reads `probe_failed`
        with the refusal's own text."""
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "claude.py"
            script.write_text(f"STREAM = {_stream(_rejected())!r}\n"
                              "import sys\nsys.stdin.read()\nsys.stdout.write(STREAM)\n")
            reading = ur.read_reset_instant(
                "claude_cli", command_base=[sys.executable, str(script)], model="",
                env={"PATH": "/usr/bin:/bin"}, cwd=tmp)
        self.assertEqual(reading.failure, ur.FAILURE_PROBE_FAILED)
        self.assertIn("launches no provider CLI", reading.detail)


class UnittestRunnerHermeticityTests(unittest.TestCase):
    """The refusal holds OUTSIDE pytest too, where conftest does not load. Witnessed from a
    subprocess, because inside this process conftest has already patched `_spawn` and a
    deleted `setUpModule` would change nothing the suite can see."""

    _MODULES = ("tools.tests.test_usage_reset", "tools.tests.test_workflow_conductor",
                "tools.tests.test_pure_leaf_producer", "tools.tests.test_pure_leaf_verify")

    def test_each_module_that_drives_the_wait_refuses_a_real_probe_under_unittest(self) -> None:
        repo = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "spawned"
            script = Path(tmp) / "cli.py"
            script.write_text(f"open({str(marker)!r}, 'a').write('x')\n"
                              "import sys\nsys.stdin.read()\n")
            code = textwrap.dedent(f'''
                import importlib, sys
                from tools import usage_reset as ur
                for name in {self._MODULES!r}:
                    mod = importlib.import_module(name)
                    mod.setUpModule()
                    try:
                        r = ur.read_reset_instant("claude_cli",
                            command_base=[sys.executable, {str(script)!r}], model="",
                            env={{"PATH": "/usr/bin:/bin"}}, cwd={tmp!r})
                        assert "launches no provider CLI" in r.detail, (name, r)
                    finally:
                        mod.tearDownModule()
                # Self-test: with every module torn down the same call DOES spawn.
                ur.read_reset_instant("claude_cli",
                    command_base=[sys.executable, {str(script)!r}], model="",
                    env={{"PATH": "/usr/bin:/bin"}}, cwd={tmp!r})
            ''')
            done = subprocess.run([sys.executable, "-c", code], cwd=repo, text=True,
                                  capture_output=True, timeout=300, check=False)
            self.assertEqual(done.returncode, 0, done.stderr[-2000:])
            self.assertEqual(marker.read_text(), "x")   # only the self-test's spawn


if __name__ == "__main__":
    unittest.main()
