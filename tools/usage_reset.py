"""The provider-reported reset instant of an exhausted usage window (issue #405).

Under `--wait-usage-reset` an `llm_usage_limit` death is slept out and the substep re-launched.
The conductor asks THIS module when the provider's window resets, sleeps until that instant
plus a margin, and falls back to its fixed schedule for every case where no usable instant
comes back. The conductor spells no provider name: `RESET_READERS` is the one per-provider
declaration, keyed by every provider `tools/llm_config.SUPPORTED_PROVIDERS` declares, and a
provider with no structured source declares `None`.

What is read, per provider (measured 2026-10-04, codex-cli 0.159.2, Claude Code 2.1.289):

  codex_cli   `codex app-server --stdio`, JSON-RPC `account/rateLimits/read`. Its `primary` /
              `secondary` windows each carry `usedPercent` and `resetsAt` (unix seconds). The
              app-server answers the request only while its stdin stays OPEN — closing stdin
              right after writing the request returns exit 0 with no answer — so the
              exchange holds stdin until the answer line arrives.
  claude_cli  a one-line `-p` turn under `--output-format stream-json --verbose`, whose stream
              carries the CLI's own `rate_limit_event` (`status` in allowed / allowed_warning /
              rejected, `resetsAt` unix seconds, `rateLimitType`). One trivial turn when the window
              is open (measured: $0.000628 on haiku); the shut window was not measured. The leaf's own launch keeps
              `--output-format json`, whose single envelope carries no such event, which is why
              this is a probe and not a read of the dead leaf.
  HTTP        none. What tags `llm_usage_limit` on an HTTP leaf is a billing text naming no
              window and no instant, and the transport keeps no response header.

Nothing is read from the dead leaf's output. The probe is not a leaf: its prompt is a host
constant, it holds no tool, and only CLI-authored lines are read — a model's text reaches
stdout JSON-encoded inside an `assistant` / `result` object, never as a top-level event line.
`read_reset_instant` never raises: every failure is a `ResetReading` whose `failure` names it,
and the conductor turns that into the schedule fallback.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any, NamedTuple

# Measured 1.2 s (codex) / 2.6 s (claude). A slow probe only delays the schedule fallback.
USAGE_RESET_PROBE_TIMEOUT_SECONDS = 60.0

_DETAIL_MAX_CHARS = 400

# The spawn the exchange goes through, module-level so the test suite can replace it (its
# autouse fixture makes every probe fail, so no test launches a real provider CLI).
_spawn = subprocess.Popen

FAILURE_NO_SOURCE = "no_source"
FAILURE_PROBE_FAILED = "probe_failed"
FAILURE_NO_EXHAUSTED_WINDOW = "no_exhausted_window"


class ResetReading(NamedTuple):
    epoch: int | None      # provider-reported reset instant, unix seconds
    window: str | None     # the provider's own name for the exhausted window
    failure: str | None    # None, or FAILURE_NO_SOURCE / _PROBE_FAILED / _NO_EXHAUSTED_WINDOW
    detail: str            # clipped, host-rendered summary of what the provider answered


def _clip(text: str) -> str:
    return text if len(text) <= _DETAIL_MAX_CHARS else text[:_DETAIL_MAX_CHARS - 3] + "..."


def _compact(value: Any) -> str:
    return _clip(json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=True))


# Bounds an epoch to what a float clock can subtract from: a JSON integer is unbounded, and
# `epoch - time.time()` raises `OverflowError` past ~1.8e308. Anything this large is not an
# instant anyway; the conductor's own cap rejects it long before.
_EPOCH_ABS_MAX = 2 ** 53


def _epoch_or_none(value: Any) -> int | None:
    """A unix-seconds `int`; `bool`, floats, strings and absurd magnitudes drop out."""
    if isinstance(value, bool) or not isinstance(value, int) or abs(value) > _EPOCH_ABS_MAX:
        return None
    return value


def _failed(reason: str) -> ResetReading:
    return ResetReading(None, None, FAILURE_PROBE_FAILED, _clip(reason))


# --- codex -------------------------------------------------------------------------------

def parse_codex_rate_limits(result: Any) -> ResetReading:
    """The reading of an `account/rateLimits/read` result.

    Among `primary` / `secondary`, a window is exhausted when `usedPercent >= 100` and its
    `resetsAt` is an `int`. Both exhausted → the LATER instant, because the run cannot proceed
    until both are open again."""
    limits = result.get("rateLimits") if isinstance(result, Mapping) else None
    if not isinstance(limits, Mapping):
        return _failed(f"no rateLimits object in the answer: {_compact(result)}")
    summary: dict[str, Any] = {"rateLimitReachedType": limits.get("rateLimitReachedType")}
    best: tuple[int, str] | None = None
    for name in ("primary", "secondary"):
        window = limits.get(name)
        if not isinstance(window, Mapping):
            summary[name] = None
            continue
        used = window.get("usedPercent")
        resets = window.get("resetsAt")
        summary[name] = {"usedPercent": used, "resetsAt": resets}
        epoch = _epoch_or_none(resets)
        exhausted = (not isinstance(used, bool) and isinstance(used, (int, float))
                     and used >= 100)
        if not exhausted or epoch is None:
            continue
        minutes = window.get("windowDurationMins")
        label = (f"{name}({minutes}min)"
                 if isinstance(minutes, int) and not isinstance(minutes, bool) else name)
        if best is None or epoch > best[0]:
            best = (epoch, label)
    detail = _compact(summary)
    if best is None:
        return ResetReading(None, None, FAILURE_NO_EXHAUSTED_WINDOW, detail)
    return ResetReading(best[0], best[1], None, detail)


def _codex_answer_line(line: str) -> bool:
    try:
        obj = json.loads(line)
    except ValueError:
        return False
    # `type(...) is int`: `True == 1` in Python, and `{"id": true}` is not the answer.
    return isinstance(obj, dict) and type(obj.get("id")) is int and obj["id"] == 1


def _read_codex_cli(*, command_base: list[str], model: str, env: Mapping[str, str],
                    cwd: str) -> ResetReading:
    del model  # the account's windows are not per-model on this source
    # The leaf's environment carries no CODEX_HOME (a leaf's home arrives through its sandbox
    # profile alone); the probe reads the operator's ORIGIN home — the one whose `auth.json`
    # that profile binds — from the same resolver the profile builder uses.
    from tools.orchestration_runtime import codex_origin_home
    probe_env = dict(env)
    probe_env["CODEX_HOME"] = str(codex_origin_home())
    send = "".join(json.dumps(msg) + "\n" for msg in (
        {"method": "initialize", "id": 0,
         "params": {"clientInfo": {"name": "atmofab", "title": "atmofab", "version": "0"}}},
        {"method": "initialized"},
        {"method": "account/rateLimits/read", "id": 1},
    ))
    lines, why = _exchange([*command_base, "app-server", "--stdio"], env=probe_env, cwd=cwd,
                           send=send, close_stdin_after_send=False, until=_codex_answer_line)
    for line in reversed(lines):
        if not _codex_answer_line(line):
            continue
        obj = json.loads(line)
        if "result" not in obj:
            return _failed(f"the app-server answered with an error: {_compact(obj.get('error'))}")
        return parse_codex_rate_limits(obj["result"])
    return _failed(f"no answer to account/rateLimits/read ({why})")


# --- claude ------------------------------------------------------------------------------

_CLAUDE_PROBE_SYSTEM_PROMPT = "Reply with the single word: ok."
_CLAUDE_PROBE_PROMPT = "ok\n"


def parse_claude_rate_limit_events(stdout: str) -> ResetReading:
    """The reading of a `stream-json` stdout: the LAST top-level `rate_limit_event` line.

    An instant exactly when its `status == "rejected"` and `resetsAt` is an `int`."""
    last: Mapping[str, Any] | None = None
    for line in stdout.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "rate_limit_event":
            info = obj.get("rate_limit_info")
            last = info if isinstance(info, Mapping) else {}
    if last is None:
        return _failed("the stream carried no rate_limit_event")
    status = last.get("status")
    window = last.get("rateLimitType")
    resets = last.get("resetsAt")
    detail = _compact({"status": status, "rateLimitType": window, "resetsAt": resets})
    epoch = _epoch_or_none(resets)
    if status != "rejected" or epoch is None:
        return ResetReading(None, None, FAILURE_NO_EXHAUSTED_WINDOW, detail)
    return ResetReading(epoch, window if isinstance(window, str) else None, None, detail)


def _read_claude_cli(*, command_base: list[str], model: str, env: Mapping[str, str],
                     cwd: str) -> ResetReading:
    argv = [*command_base, *(["--model", model] if model else []),
            "--safe-mode", "--system-prompt", _CLAUDE_PROBE_SYSTEM_PROMPT, "--tools", "",
            "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence",
            "--output-format", "stream-json", "--verbose", "-p"]
    lines, why = _exchange(argv, env=env, cwd=cwd, send=_CLAUDE_PROBE_PROMPT,
                           close_stdin_after_send=True, until=lambda _line: False)
    reading = parse_claude_rate_limit_events("\n".join(lines))
    if reading.failure == FAILURE_PROBE_FAILED:
        return _failed(f"{reading.detail} ({why})")
    return reading


# --- the one spawn -----------------------------------------------------------------------

# Bounds what one probe may hold in memory. The real answers are a few kilobytes; a CLI that
# floods stdout past this is drained (so it cannot block on a full pipe) but not retained.
_STDOUT_RETAIN_MAX_CHARS = 1_000_000
# After the CLI process itself has exited, how long its stdout may stay open (a descendant it
# left in its group holding the pipe) before the read is abandoned.
_POST_EXIT_READ_GRACE_SECONDS = 2.0


def _exchange(argv: list[str], *, env: Mapping[str, str], cwd: str, send: str,
              close_stdin_after_send: bool, until: Callable[[str], bool],
              timeout: float | None = None) -> tuple[list[str], str]:
    """Spawn `argv`, write `send`, and collect stdout lines until `until(line)`, EOF, the
    process's exit (plus a short grace), or the deadline; then close stdin, reap, and kill the
    process group — always, so a descendant the CLI left behind does not outlive the call.
    Returns the lines read and a short reason the read stopped (for a failure's detail)."""
    deadline = time.monotonic() + (USAGE_RESET_PROBE_TIMEOUT_SECONDS if timeout is None
                                   else timeout)
    proc = _spawn(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                  stderr=subprocess.DEVNULL, env=dict(env), cwd=cwd, text=True,
                  encoding="utf-8", errors="replace", start_new_session=True)
    lines: list[str] = []
    retained = [0]
    done = threading.Event()

    def _reader() -> None:
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                if retained[0] >= _STDOUT_RETAIN_MAX_CHARS:
                    continue  # keep draining, stop retaining
                line = raw.rstrip("\n")
                lines.append(line)
                retained[0] += len(line)
                if until(line):
                    break
        finally:
            done.set()

    thread = threading.Thread(target=_reader, daemon=True)
    thread.start()
    # Everything after the spawn sits inside this `try`: the probe runs in a session of its
    # own, so the driver's Ctrl-C / SIGTERM does not reach it, and an interruption that left
    # this function before the `finally` would leave it running (a claude probe mid-turn).
    try:
        try:
            assert proc.stdin is not None
            proc.stdin.write(send)
            proc.stdin.flush()
            if close_stdin_after_send:
                proc.stdin.close()
        except OSError:
            pass  # the process died early; what it printed (if anything) is still read
        exited_at: float | None = None
        while not done.is_set():
            now = time.monotonic()
            if now >= deadline:
                break
            if exited_at is None and proc.poll() is not None:
                exited_at = now
            if exited_at is not None and now - exited_at >= _POST_EXIT_READ_GRACE_SECONDS:
                break
            done.wait(min(0.05, deadline - now))
        finished = done.is_set()
        why = ("answered" if finished
               else "timed out" if exited_at is None else "stdout held open after exit")
        try:
            if proc.stdin is not None and not proc.stdin.closed:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=max(0.1, min(5.0, deadline - time.monotonic())))
        except subprocess.TimeoutExpired:
            pass
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass  # the group is already empty
        proc.wait()
    if finished and proc.returncode not in (0, None) and not lines:
        why = f"exit {proc.returncode}"
    return list(lines), why


Reader = Callable[..., ResetReading]

# THE per-provider declaration. `test_usage_reset` pins that its keys are exactly
# `llm_config.SUPPORTED_PROVIDERS`, so a provider added there without a decision here fails.
RESET_READERS: Mapping[str, Reader | None] = {
    "claude_cli": _read_claude_cli,
    "codex_cli": _read_codex_cli,
    "openai_compatible": None,
    "anthropic_api": None,
}


def no_source_reading(provider: str) -> ResetReading | None:
    """The `no_source` reading when `provider` declares no reader, else None. Lets a caller
    skip assembling the probe's environment for a provider that will never be probed."""
    if RESET_READERS.get(provider) is not None:
        return None
    return ResetReading(None, None, FAILURE_NO_SOURCE,
                        f"provider {provider!r} declares no reset source")


def read_reset_instant(provider: str, *, command_base: list[str], model: str,
                       env: Mapping[str, str], cwd: str) -> ResetReading:
    """The reset instant `provider` reports for its exhausted window. Never raises."""
    unsourced = no_source_reading(provider)
    if unsourced is not None:
        return unsourced
    reader = RESET_READERS[provider]
    assert reader is not None
    try:
        return reader(command_base=command_base, model=model, env=env, cwd=cwd)
    except Exception as exc:  # noqa: BLE001 — a probe failure must read as the fallback
        return _failed(f"{type(exc).__name__}: {exc}")
