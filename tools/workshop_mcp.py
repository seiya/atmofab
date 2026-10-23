"""The wrapper's transport: one MCP endpoint per `workshop leaf` launch (issue #534).

`docs/design/producer_workshop_leaf.md` §Tool surface: a `workshop leaf` reaches the wrapper's
five operations (`tools/workshop_wrapper.py`) as MCP tools. This module serves them over MCP's
streamable-HTTP transport from the conductor's own process — a `ThreadingHTTPServer` on
`127.0.0.1:<ephemeral port>`, started for ONE launch and stopped when that launch's leaf exits.
The leaf's CLI is told the URL by `--mcp-config` (`pure_leaf.workshop_mcp_config_json`).

The protocol subset, as measured against the claude CLI 2.1.309 (`tools/tests/test_bwrap_
simulation.py` `WorkshopLeafCliTests`, unbilled): JSON-RPC 2.0 over `POST /mcp`, one message per
request, every response a JSON body (the CLI accepts JSON; no SSE stream is offered);
`initialize` answers the client's own `protocolVersion` and an `Mcp-Session-Id` header;
a notification (no `id`) is answered `202` with no body; `ping`, `tools/list`, `tools/call`; any
other method — the CLI's `server/discover` probe among them — is `-32601`. `GET /mcp` (the
client's offer of a server-to-client stream) is `405`, `DELETE` (session end) is `200`.

THE THREAD MODEL. Each request is served on its own thread. A `tools/call` runs the operation
there through `workshop_wrapper.run_wrapper_op`, which reads only the frozen `WrapperContext` —
no conductor state, no conductor method, no event. Every exchange is appended to the lineage
record's `wrapper_calls.jsonl` with the launch's `agent_run_id` and the peer address, and the
main thread reads that file after the leaf exits. Two things cross back to the main thread,
each through a callable the conductor hands in: `on_end(ended_by)`, called when an operation's
result says the lineage has ENDED (a bound ran out), so the conductor ends the leaf; and the
process-group id of every sandboxed command the wrapper starts, kept here so `stop()` can kill a
run still in flight.

No token guards the URL. The endpoint is on the loopback interface for the life of one launch;
a caller on this machine other than the leaf could do no more than compile or run this
lineage's own tree, and what the leaf would gain from that is nothing (`AGENTS.md` §Development
premises). The peer address of every request is recorded.
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from tools import workshop_tree
from tools.workshop_wrapper import (
    WRAPPER_OPS,
    WrapperContext,
    result_text,
    run_wrapper_op,
)

#: The path the endpoint serves.
MCP_PATH = "/mcp"

#: The server name the CLI is told (`--mcp-config`); its tools are `mcp__wrapper__<op>`.
SERVER_NAME = "wrapper"

#: The protocol version answered when a client names none.
DEFAULT_PROTOCOL_VERSION = "2025-06-18"

#: The largest request body read (a `tools/call` carries a few case ids at most).
MAX_REQUEST_BYTES = 1 << 20

#: How long `stop()` waits for in-flight handlers after killing their runs.
STOP_JOIN_SECONDS = 30.0

_JSONRPC_PARSE_ERROR = -32700
_JSONRPC_INVALID_REQUEST = -32600
_JSONRPC_METHOD_NOT_FOUND = -32601
_JSONRPC_INVALID_PARAMS = -32602
_JSONRPC_INTERNAL_ERROR = -32603

_NO_ARGUMENTS = {"type": "object", "properties": {}, "additionalProperties": False}

#: The five tools, with the input each takes. The descriptions say what the operation does and
#: what its result is; the contract they serve is the launch prompt's.
TOOL_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {"name": "derive",
     "description": "Read bundle.yaml and the files it names from your tree, check the bundle "
                    "the way the host will, and derive the build. Returns ok, or the category "
                    "and findings to fix.",
     "inputSchema": _NO_ARGUMENTS},
    {"name": "compile",
     "description": "Build your tree's bundle with the target's toolchain and the staged "
                    "dependency closure. Returns ok, the return code and the build output.",
     "inputSchema": _NO_ARGUMENTS},
    {"name": "lint",
     "description": "Run the target's static lint over your bundle's sources. Returns ok, the "
                    "return code and the lint output.",
     "inputSchema": _NO_ARGUMENTS},
    {"name": "syntax_check",
     "description": "Run the target's syntax-only compiler check over your bundle, including "
                    "the model file compiled alone. Returns ok, the return code and the output.",
     "inputSchema": _NO_ARGUMENTS},
    {"name": "run",
     "description": "Run a binary your last compile built on a case file of your tree. "
                    "Returns ok, the return code and the program's output.",
     "inputSchema": {
         "type": "object",
         "properties": {
             "binary": {"type": "string",
                        "description": "basename of a binary the last compile built"},
             "cases_file": {"type": "string",
                            "description": "tree-relative case file (default spec.ir.yaml)"},
             "case_ids": {"type": "array", "items": {"type": "string"},
                          "description": "case ids to run (default: every case)"},
         },
         "required": ["binary"],
         "additionalProperties": False,
     }},
)

if tuple(t["name"] for t in TOOL_DEFINITIONS) != WRAPPER_OPS:  # pragma: no cover - import guard
    raise RuntimeError("TOOL_DEFINITIONS must name exactly workshop_wrapper.WRAPPER_OPS")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _kill_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


class WrapperMcpServer:
    """The endpoint of one launch. `start()` binds and serves; `stop()` ends it.

    `ctx` is the launch's frozen `WrapperContext` (its `agent_run_id` is the launch's).
    `on_end(ended_by)` is called — once, from a handler thread — when a result reports the
    lineage ended; the conductor's callable ends the leaf. It must not raise; a raise is
    swallowed so it cannot turn a recorded end into a lost response."""

    def __init__(self, ctx: WrapperContext, *,
                 on_end: Callable[[str], None] | None = None,
                 run_op: Callable[..., dict[str, Any]] = run_wrapper_op) -> None:
        self.ctx = ctx
        self._on_end = on_end
        self._run_op = run_op
        self._pgids: set[int] = set()
        self._pgids_lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._threads_lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._ended_lock = threading.Lock()
        self._ended_signalled = False
        self._stopping = False
        self._session_id = f"{ctx.agent_run_id}"
        self._httpd: ThreadingHTTPServer | None = None
        self._serve_thread: threading.Thread | None = None

    # --- lifecycle --------------------------------------------------------------------

    @property
    def url(self) -> str:
        if self._httpd is None:
            raise RuntimeError("the wrapper endpoint is not started")
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}{MCP_PATH}"

    @property
    def port(self) -> int:
        if self._httpd is None:
            raise RuntimeError("the wrapper endpoint is not started")
        return int(self._httpd.server_address[1])

    def start(self) -> WrapperMcpServer:
        if self._httpd is not None:
            raise RuntimeError("the wrapper endpoint is already started")
        server = self

        class _Httpd(ThreadingHTTPServer):
            daemon_threads = True

            def process_request(self, request, client_address):
                thread = threading.Thread(target=self.process_request_thread,
                                          args=(request, client_address), daemon=True)
                with server._threads_lock:
                    server._threads = [t for t in server._threads if t.is_alive()]
                    server._threads.append(thread)
                thread.start()

        self._httpd = _Httpd(("127.0.0.1", 0), _handler_for(self))
        self._serve_thread = threading.Thread(target=self._httpd.serve_forever,
                                              kwargs={"poll_interval": 0.1}, daemon=True)
        self._serve_thread.start()
        return self

    def stop(self, *, join_seconds: float = STOP_JOIN_SECONDS) -> None:
        """Stop serving, kill every sandboxed run this endpoint's calls started, and join the
        in-flight handlers. Kills before and while it joins, so a handler blocked on a run is
        released by the kill rather than by the run's own timeout. Idempotent."""
        httpd = self._httpd
        if httpd is None or self._stopping:
            return
        self._stopping = True
        httpd.shutdown()
        deadline = time.monotonic() + max(0.0, join_seconds)
        while True:
            self.kill_runs()
            with self._threads_lock:
                alive = [t for t in self._threads if t.is_alive()]
            if not alive or time.monotonic() >= deadline:
                break
            for thread in alive:
                thread.join(timeout=0.2)
        self.kill_runs()
        httpd.server_close()

    def kill_runs(self) -> list[int]:
        """Kill every process group a call of this endpoint started and that call has not yet
        returned from; return their ids.

        Only IN-FLIGHT calls' groups: a call's runs are over when the build-runtime library
        returns it (it waits for the run, and kills the group on its timeout), and the id of a
        group that has exited may be reused by an unrelated process group, which a later kill
        would then hit."""
        with self._pgids_lock:
            pgids = sorted(self._pgids)
        for pgid in pgids:
            _kill_group(pgid)
        return pgids

    def in_flight(self) -> int:
        with self._threads_lock:
            return sum(1 for t in self._threads if t.is_alive())

    # --- what a handler calls ----------------------------------------------------------

    def _record_spawn(self, pgid: int, call: set[int] | None = None) -> None:
        with self._pgids_lock:
            self._pgids.add(int(pgid))
            if call is not None:
                call.add(int(pgid))
        if self._stopping:
            # A run that started after `stop()` began is killed at once: nothing of this
            # launch runs past it.
            _kill_group(int(pgid))

    def _call_op(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run one operation, its spawned groups tracked for exactly as long as it runs."""
        call: set[int] = set()
        try:
            return self._run_op(self.ctx, name, arguments,
                                on_spawn=lambda pgid: self._record_spawn(pgid, call))
        finally:
            with self._pgids_lock:
                self._pgids -= call

    def _log(self, entry: Mapping[str, Any]) -> None:
        with self._log_lock:
            workshop_tree.append_jsonl(self.ctx.paths.wrapper_calls, entry)

    def _signal_end(self, ended_by: str) -> None:
        with self._ended_lock:
            if self._ended_signalled:
                return
            self._ended_signalled = True
        if self._on_end is not None:
            try:
                self._on_end(ended_by)
            except Exception:
                pass

    def handle_message(self, message: Any) -> tuple[int, dict[str, Any] | None, dict[str, str]]:
        """Answer one JSON-RPC message: `(http status, body or None, extra headers)`."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return 200, _error(None, _JSONRPC_INVALID_REQUEST,
                               "one JSON-RPC 2.0 message object per request"), {}
        method = message.get("method")
        if "id" not in message:
            # A notification (`notifications/initialized`, a cancellation): accepted, unanswered.
            return 202, None, {}
        rid = message.get("id")
        if not isinstance(method, str):
            return 200, _error(rid, _JSONRPC_INVALID_REQUEST, "method must be a string"), {}
        params = message.get("params")
        if params is not None and not isinstance(params, dict):
            return 200, _error(rid, _JSONRPC_INVALID_PARAMS, "params must be an object"), {}
        params = params or {}
        if method == "initialize":
            version = params.get("protocolVersion")
            return 200, _ok(rid, {
                "protocolVersion": version if isinstance(version, str) and version
                else DEFAULT_PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": "1"},
            }), {"Mcp-Session-Id": self._session_id}
        if method == "ping":
            return 200, _ok(rid, {}), {}
        if method == "tools/list":
            return 200, _ok(rid, {"tools": [dict(t) for t in TOOL_DEFINITIONS]}), {}
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments")
            if not isinstance(name, str) or name not in WRAPPER_OPS:
                return 200, _error(rid, _JSONRPC_INVALID_PARAMS,
                                   f"unknown tool {name!r}; the tools are {list(WRAPPER_OPS)}"), {}
            if arguments is not None and not isinstance(arguments, dict):
                return 200, _error(rid, _JSONRPC_INVALID_PARAMS,
                                   "arguments must be an object"), {}
            result = self._call_op(name, arguments or {})
            if result.get("ended") is True:
                try:
                    recorded = workshop_tree.read_lineage_record(self.ctx.paths).get("ended_by")
                except ValueError:
                    recorded = None  # the result's own category names the end just as well
                self._signal_end(str(recorded or result.get("category") or ""))
            return 200, _ok(rid, {"content": [{"type": "text", "text": result_text(result)}],
                                  "isError": not bool(result.get("ok"))}), {}
        return 200, _error(rid, _JSONRPC_METHOD_NOT_FOUND, f"method {method!r} not found"), {}


def _ok(rid: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _error(rid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _handler_for(server: WrapperMcpServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _peer(self) -> str:
            host, port = self.client_address[:2]
            return f"{host}:{port}"

        def _send(self, status: int, body: dict[str, Any] | None,
                  headers: Mapping[str, str] = ()) -> None:  # type: ignore[assignment]
            data = b"" if body is None else json.dumps(body).encode("utf-8")
            self.send_response(status)
            if body is not None:
                self.send_header("Content-Type", "application/json")
            for key, value in dict(headers).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _record(self, request: Any, status: int, response: Any, started: float) -> None:
            server._log({
                "recorded_at_utc": _utc_now_iso(),
                "agent_run_id": server.ctx.agent_run_id,
                "peer": self._peer(),
                "http_method": self.command,
                "path": self.path,
                "request": request,
                "status": status,
                "response": response,
                "elapsed_s": round(time.monotonic() - started, 3),
            })

        def do_POST(self) -> None:
            started = time.monotonic()
            if self.path.split("?", 1)[0] != MCP_PATH:
                self._record(None, 404, None, started)
                self._send(404, None)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_REQUEST_BYTES:
                body = _error(None, _JSONRPC_INVALID_REQUEST, "request body length refused")
                self._record(None, 413, body, started)
                self._send(413, body)
                return
            raw = self.rfile.read(length)
            try:
                message = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                body = _error(None, _JSONRPC_PARSE_ERROR, "the body is not JSON")
                self._record(raw.decode("utf-8", "replace")[:2000], 200, body, started)
                self._send(200, body)
                return
            try:
                status, body, headers = server.handle_message(message)
            except Exception as exc:  # noqa: BLE001 - every exchange is answered and recorded
                rid = message.get("id") if isinstance(message, dict) else None
                status, headers = 200, {}
                body = _error(rid, _JSONRPC_INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
            self._record(message, status, body, started)
            self._send(status, body, headers)

        def do_GET(self) -> None:
            started = time.monotonic()
            self._record(None, 405, None, started)
            self._send(405, None, {"Allow": "POST, DELETE"})

        def do_DELETE(self) -> None:
            started = time.monotonic()
            self._record(None, 200, None, started)
            self._send(200, None)

        def log_message(self, *args: Any) -> None:  # stdlib spelling; the record is the log
            pass

    return Handler
