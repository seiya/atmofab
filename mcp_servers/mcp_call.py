#!/usr/bin/env python3
"""Minimal MCP client for build_runtime_server.py."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


def _write_message(stream, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    stream.write(header)
    stream.write(body)
    stream.flush()


def _read_message(stream) -> dict[str, Any]:
    while True:
        first = stream.readline()
        if not first:
            raise RuntimeError("unexpected EOF while reading MCP message header")
        if not first.strip():
            continue
        if first.lower().startswith(b"content-length:"):
            length = int(first.split(b":", 1)[1].strip())
            while True:
                header_line = stream.readline()
                if not header_line:
                    raise RuntimeError("unexpected EOF while reading MCP headers")
                if header_line in (b"\r\n", b"\n"):
                    break
            body = stream.read(length)
            if not body:
                raise RuntimeError("unexpected EOF while reading MCP body")
            return json.loads(body.decode("utf-8"))
        return json.loads(first.decode("utf-8"))


#: The server this client speaks to, resolved from THIS file rather than from the caller's working
#: directory. It used to be the relative path `mcp_servers/build_runtime_server.py`, which made
#: every documented use of this client silently require the checkout root as `cwd` — and the
#: failure did not say so: the spawn failed, `stderr` was captured and never read, and what the
#: caller saw was `unexpected EOF while reading MCP message header`, a framing error naming the
#: wrong layer. That matters more here than in an ordinary script, because this client is the
#: instrument `docs/RUNBOOK.md` and `.claude/skills/atmofab-enforcement-change` hand an operator
#: to PROVE a capability-gate refusal: with the old behaviour, "the gate refused the call" and
#: "the client never started" were the same non-zero exit, so a verification could be recorded as
#: passed by someone standing in the wrong directory.
_SERVER = Path(__file__).resolve().parent / "build_runtime_server.py"


def _mcp_call(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    proc = subprocess.Popen(
        [sys.executable, str(_SERVER)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None

    def _server_stderr() -> str:
        """Whatever the server said before it stopped talking.

        Read only on the failure path, and only after the process is gone, so it cannot deadlock
        on a server that is still running. Without it a server that dies at import — a missing
        dependency, a syntax error, a bad path — is reported as a framing problem.
        """
        try:
            return (proc.stderr.read().decode("utf-8", "replace").strip()
                    if proc.stderr is not None else "")
        except Exception:  # noqa: BLE001 - diagnostics must not replace the original failure
            return ""

    try:
        _write_message(
            proc.stdin,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
            },
        )
        _ = _read_message(proc.stdout)

        _write_message(
            proc.stdin,
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        )

        _write_message(
            proc.stdin,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            },
        )
        response = _read_message(proc.stdout)
    except (RuntimeError, OSError, ValueError) as exc:
        # EVERY failure of the exchange, not only the last read. Two narrowings were measured and
        # both left a real case uncovered: the first version wrapped the `tools/call` read alone,
        # and a server that dies at import fails at the INITIALIZE read one line earlier; the
        # second caught `json.JSONDecodeError` and missed the BARE `ValueError` that
        # `int(header_value)` raises on a non-numeric `Content-Length` — a framing failure this
        # repository's own tests record as reachable. `ValueError` covers both, since
        # `JSONDecodeError` is one. `OSError` is the broken pipe seen from the write side.
        #
        # KILL FIRST, THEN READ. `proc.stderr.read()` reads to EOF, so on a failure where the
        # server is still alive — a non-JSON line on its stdout reaches `json.loads` while the
        # process keeps running on a stdin pipe this client still holds open — reading before the
        # kill blocks for ever, and this client has no timeout of its own. The order is the whole
        # difference between a diagnostic and a hang.
        proc.kill()
        proc.wait(timeout=2)
        detail = _server_stderr()
        raise RuntimeError(
            f"{exc}; the server ({_SERVER}) produced: {detail or '(nothing on stderr)'}"
        ) from exc
    finally:
        proc.kill()
        proc.wait(timeout=2)

    if "error" in response:
        raise RuntimeError(response["error"])
    result = response.get("result", {})
    if result.get("isError"):
        structured = result.get("structuredContent", {})
        raise RuntimeError(json.dumps(structured, ensure_ascii=False))
    return result.get("structuredContent", {})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tool", required=True)
    parser.add_argument("--args-json", required=True)
    args = parser.parse_args()

    tool_args = json.loads(args.args_json)
    data = _mcp_call(args.tool, tool_args)
    json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
