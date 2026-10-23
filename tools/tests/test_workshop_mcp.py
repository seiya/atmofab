"""The wrapper's MCP endpoint (`tools/workshop_mcp.py`, issue #534 PR-2).

Driven over real HTTP on the loopback interface, as the claude CLI drives it. The operation
behind `tools/call` is stubbed (`run_op=`) except where a row is about the bound the real
operation enforces; what is under test is the transport: the protocol subset, the record of
every exchange, the end signal, and the teardown that kills a run still in flight.
"""

from __future__ import annotations

import http.client
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools import workshop_mcp, workshop_tree
from tools.workshop_tree import LineagePaths
from tools.workshop_wrapper import WRAPPER_OPS

#: The local port of the last request `_http` made, so a row can match the recorded peer.
_LAST_LOCAL_PORT: list[int] = []


def _http(url: str, method: str, data: bytes | None = None
          ) -> tuple[int, bytes, dict[str, str]]:
    """One request over `http.client`, straight to the loopback port. Not `urllib.request.
    urlopen`: its module-level opener is built ONCE per process from the proxy environment of
    that moment, so another test that patched `HTTP_PROXY` around its own first `urlopen`
    leaves every later call in the same worker routed to a proxy that is gone. Measured: a
    `urlopen` of a live loopback server after `HTTP_PROXY` was set for one earlier `urlopen`
    and then unset is still `Connection refused`; under `-n 24` these rows failed that way."""
    parts = urllib.parse.urlsplit(url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=30)
    try:
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"} if data is not None else {}
        conn.connect()
        _LAST_LOCAL_PORT[:] = [conn.sock.getsockname()[1]]
        conn.request(method, parts.path, body=data, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read(), {k: v for k, v in resp.getheaders()}
    finally:
        conn.close()


def _post(url: str, body: object, *, raw: bytes | None = None) -> tuple[int, dict | None, dict]:
    data = raw if raw is not None else json.dumps(body).encode("utf-8")
    status, text, headers = _http(url, "POST", data)
    return status, (json.loads(text) if text else None), headers


def _request(url: str, method: str) -> int:
    return _http(url, method)[0]


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.paths = LineagePaths(homes_root=root / "homes", orchestration_id="oid",
                                  lineage_id="lin1", orchestration_dir=root / "orch")
        self.paths.home.mkdir(parents=True)
        self.paths.make_work_dirs()
        workshop_tree.write_lineage_record(self.paths, node_key="component/x/y",
                                           started_at_utc="2026-10-23T00:00:00Z")
        self.ctx = SimpleNamespace(paths=self.paths, agent_run_id="arid-launch-1",
                                   max_wrapper_calls=3, seed_texts=())
        self.calls: list[tuple[str, dict]] = []

    def _server(self, run_op=None, on_end=None) -> workshop_mcp.WrapperMcpServer:
        def stub(ctx, op, args, *, on_spawn=None):
            self.calls.append((op, dict(args)))
            return {"ok": True, "op": op}

        server = workshop_mcp.WrapperMcpServer(self.ctx, on_end=on_end, run_op=run_op or stub)
        server.start()
        self.addCleanup(server.stop)
        return server

    def _log(self) -> list[dict]:
        if not self.paths.wrapper_calls.is_file():
            return []
        return [json.loads(line) for line in
                self.paths.wrapper_calls.read_text(encoding="utf-8").splitlines()]


class ProtocolTests(_Fixture):
    def test_initialize_answers_the_clients_version_and_a_session_header(self) -> None:
        server = self._server()
        status, body, headers = _post(server.url, {
            "jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                       "clientInfo": {"name": "claude-code", "version": "x"}}})
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["protocolVersion"], "2025-11-25")
        self.assertEqual(body["result"]["capabilities"], {"tools": {}})
        self.assertEqual(body["result"]["serverInfo"]["name"], "wrapper")
        self.assertEqual(headers.get("Mcp-Session-Id"), "arid-launch-1")
        self.assertTrue(headers.get("Content-Type", "").startswith("application/json"))

    def test_a_notification_is_accepted_unanswered(self) -> None:
        server = self._server()
        status, body, _ = _post(server.url, {"jsonrpc": "2.0",
                                             "method": "notifications/initialized"})
        self.assertEqual((status, body), (202, None))

    def test_ping_and_unknown_methods(self) -> None:
        server = self._server()
        self.assertEqual(_post(server.url, {"jsonrpc": "2.0", "id": 1, "method": "ping"})[1],
                         {"jsonrpc": "2.0", "id": 1, "result": {}})
        # The CLI's own `server/discover` probe arrives before `initialize` (measured on
        # 2.1.309) and must be answered as an unknown method, not as a transport error.
        for method in ("server/discover", "resources/list", "prompts/list"):
            with self.subTest(method=method):
                body = _post(server.url, {"jsonrpc": "2.0", "id": 2, "method": method})[1]
                self.assertEqual(body["error"]["code"], -32601)

    def test_tools_list_names_exactly_the_wrapper_operations(self) -> None:
        server = self._server()
        body = _post(server.url, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"})[1]
        tools = body["result"]["tools"]
        self.assertEqual(tuple(t["name"] for t in tools), WRAPPER_OPS)
        for tool in tools:
            self.assertEqual(tool["inputSchema"]["type"], "object", msg=tool["name"])
            self.assertIs(tool["inputSchema"]["additionalProperties"], False, msg=tool["name"])
        run = next(t for t in tools if t["name"] == "run")
        self.assertEqual(set(run["inputSchema"]["properties"]),
                         {"binary", "cases_file", "case_ids"})

    def test_tools_call_dispatches_and_answers_the_result_as_text(self) -> None:
        server = self._server()
        body = _post(server.url, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                  "params": {"name": "run",
                                             "arguments": {"binary": "b", "case_ids": ["c1"]}}})[1]
        self.assertEqual(self.calls, [("run", {"binary": "b", "case_ids": ["c1"]})])
        result = body["result"]
        self.assertIs(result["isError"], False)
        self.assertEqual(json.loads(result["content"][0]["text"]), {"ok": True, "op": "run"})

    def test_a_failed_operation_is_an_error_result_not_a_protocol_error(self) -> None:
        server = self._server(run_op=lambda ctx, op, args, *, on_spawn=None: {
            "ok": False, "category": "build_graph_failed", "findings": "x"})
        result = _post(server.url, {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                                    "params": {"name": "derive", "arguments": {}}})[1]["result"]
        self.assertIs(result["isError"], True)
        self.assertEqual(json.loads(result["content"][0]["text"])["category"],
                         "build_graph_failed")

    def test_an_unknown_tool_or_malformed_call_is_refused_before_any_operation(self) -> None:
        server = self._server()
        for params in ({"name": "shell", "arguments": {}}, {"name": "derive", "arguments": []},
                       {"arguments": {}}):
            with self.subTest(params=params):
                body = _post(server.url, {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                                          "params": params})[1]
                self.assertEqual(body["error"]["code"], -32602)
        self.assertEqual(self.calls, [])

    def test_malformed_messages(self) -> None:
        server = self._server()
        self.assertEqual(_post(server.url, None, raw=b"{not json")[1]["error"]["code"], -32700)
        self.assertEqual(_post(server.url, [{"jsonrpc": "2.0", "id": 1, "method": "ping"}])[1]
                         ["error"]["code"], -32600)
        self.assertEqual(_post(server.url, {"id": 1, "method": "ping"})[1]["error"]["code"],
                         -32600)

    def test_get_delete_and_other_paths(self) -> None:
        server = self._server()
        self.assertEqual(_request(server.url, "GET"), 405)
        self.assertEqual(_request(server.url, "DELETE"), 200)
        other = server.url.replace("/mcp", "/other")
        self.assertEqual(_post(other, {"jsonrpc": "2.0", "id": 1, "method": "ping"})[0], 404)


class RecordTests(_Fixture):
    def test_every_exchange_is_recorded_with_the_launch_and_the_peer(self) -> None:
        server = self._server()
        _post(server.url, {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
        _post(server.url, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        _post(server.url, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "lint", "arguments": {}}})
        _request(server.url, "GET")
        log = self._log()
        self.assertEqual([(e["http_method"], (e["request"] or {}).get("method"), e["status"])
                          for e in log],
                         [("POST", "initialize", 200),
                          ("POST", "notifications/initialized", 202),
                          ("POST", "tools/call", 200), ("GET", None, 405)])
        for entry in log:
            self.assertEqual(entry["agent_run_id"], "arid-launch-1")
            self.assertTrue(entry["peer"].startswith("127.0.0.1:"), entry["peer"])
        # The peer is the CALLER's address: the last line names the port the last request
        # left from.
        self.assertEqual(log[-1]["peer"], f"127.0.0.1:{_LAST_LOCAL_PORT[0]}")
        self.assertEqual(json.loads(log[2]["response"]["result"]["content"][0]["text"]),
                         {"ok": True, "op": "lint"})


class FailureTests(_Fixture):
    def test_an_operation_that_raises_is_answered_and_recorded(self) -> None:
        def boom(ctx, op, args, *, on_spawn=None):
            raise RuntimeError("operation failed")

        server = self._server(run_op=boom)
        status, body, _ = _post(server.url, {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                             "params": {"name": "lint", "arguments": {}}})
        self.assertEqual((status, body["id"], body["error"]["code"]), (200, 9, -32603))
        self.assertIn("operation failed", body["error"]["message"])
        self.assertEqual(self._log()[-1]["response"], body)

    def test_an_end_whose_record_is_unreadable_still_answers_and_signals(self) -> None:
        ends: list[str] = []
        server = self._server(
            run_op=lambda ctx, op, args, *, on_spawn=None: {"ok": False, "ended": True,
                                                            "category": "max_wrapper_calls"},
            on_end=ends.append)
        self.paths.record_json.unlink()
        body = _post(server.url, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": "derive", "arguments": {}}})[1]
        self.assertIs(body["result"]["isError"], True)
        self.assertEqual(ends, ["max_wrapper_calls"])
        self.assertEqual(self._log()[-1]["status"], 200)


class EndAndTeardownTests(_Fixture):
    def test_the_real_cap_ends_the_lineage_and_signals_the_conductor_once(self) -> None:
        """Driven through the REAL `run_wrapper_op`: the bound is the operation's, the signal
        is the transport's. The fourth call of a lineage capped at three is refused, records
        `ended_by=max_wrapper_calls`, and is answered `ended: true`; the end callback fires
        once however many calls follow."""
        from tools import workshop_wrapper

        ends: list[str] = []
        self.ctx.lineage_deadline = time.time() + 3600

        def op(ctx, name, args, *, on_spawn=None):
            # Only the bounds of the real operation: call a handler that does nothing.
            original = workshop_wrapper._OPS[name]
            workshop_wrapper._OPS[name] = lambda c, a, s: {"ok": True}
            try:
                return workshop_wrapper.run_wrapper_op(ctx, name, args, on_spawn=on_spawn)
            finally:
                workshop_wrapper._OPS[name] = original

        server = self._server(run_op=op, on_end=ends.append)
        results = []
        for i in range(5):
            body = _post(server.url, {"jsonrpc": "2.0", "id": i, "method": "tools/call",
                                      "params": {"name": "derive", "arguments": {}}})[1]
            results.append(json.loads(body["result"]["content"][0]["text"]))
        self.assertEqual([r.get("ok") for r in results], [True, True, True, False, False])
        self.assertTrue(all(r.get("ended") for r in results[3:]))
        self.assertEqual(results[3]["category"], "max_wrapper_calls")
        self.assertEqual(ends, ["max_wrapper_calls"])
        self.assertEqual(workshop_tree.read_lineage_record(self.paths)["ended_by"],
                         "max_wrapper_calls")

    def test_a_raising_end_callback_does_not_lose_the_response(self) -> None:
        workshop_tree.end_lineage(self.paths, "lineage_timeout")

        def boom(_why: str) -> None:
            raise RuntimeError("callback failed")

        server = self._server(
            run_op=lambda ctx, op, args, *, on_spawn=None: {"ok": False, "ended": True,
                                                            "category": "lineage_timeout"},
            on_end=boom)
        body = _post(server.url, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": "compile", "arguments": {}}})[1]
        self.assertIs(body["result"]["isError"], True)

    def test_stop_kills_a_run_in_flight_and_joins_its_handler(self) -> None:
        """A handler blocked on a sandboxed run is released by the KILL, not by the run's own
        timeout: `stop()` kills every process group a call reported through `on_spawn` while it
        joins. The run here is a real process in its own session, as the build-runtime
        library starts one."""
        started = threading.Event()
        pgids: list[int] = []
        finished: list[int] = []

        def op(ctx, name, args, *, on_spawn=None):
            proc = subprocess.Popen(["sleep", "120"], start_new_session=True)
            on_spawn(proc.pid)
            pgids.append(proc.pid)
            started.set()
            finished.append(proc.wait())
            return {"ok": False, "rc": proc.returncode}

        server = self._server(run_op=op)
        def call() -> None:
            # `stop()` closes the connection under the in-flight call, so this client loses
            # its response — the call's answer goes nowhere once the endpoint is stopping.
            try:
                _post(server.url, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": "run", "arguments": {"binary": "b"}}})
            except (OSError, http.client.HTTPException):
                pass

        caller = threading.Thread(target=call, daemon=True)
        caller.start()
        self.assertTrue(started.wait(30))
        self.assertEqual(server.in_flight(), 1)
        t0 = time.monotonic()
        server.stop(join_seconds=20)
        self.assertLess(time.monotonic() - t0, 15, "stop() waited for the run's own end")
        self.assertEqual(server.in_flight(), 0)
        self.assertEqual(finished, [-signal.SIGKILL])
        with self.assertRaises(ProcessLookupError):
            os.killpg(pgids[0], 0)
        # The call has returned, so its group is no longer the endpoint's to kill.
        self.assertEqual(server.kill_runs(), [])

    def test_a_finished_calls_group_is_never_killed_later(self) -> None:
        """A group id outlives nothing: once a call returns, its runs are over and the id may
        be reused by an unrelated group, so `kill_runs` (and `stop`) must not signal it."""
        seen: list[int] = []

        def op(ctx, name, args, *, on_spawn=None):
            proc = subprocess.Popen(["true"], start_new_session=True)
            on_spawn(proc.pid)
            seen.append(proc.pid)
            proc.wait()
            return {"ok": True}

        server = self._server(run_op=op)
        _post(server.url, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "compile", "arguments": {}}})
        self.assertEqual(len(seen), 1)
        with mock.patch.object(workshop_mcp, "_kill_group") as kill:
            self.assertEqual(server.kill_runs(), [])
            server.stop()
        kill.assert_not_called()

    def test_stop_closes_a_kept_alive_connection_and_no_call_runs_after_it(self) -> None:
        """Codex, round 2: a client holding an HTTP/1.1 connection open kept its handler
        reading, so `stop()` waited out its whole join and the same connection could still ask
        for an operation afterwards. Here the connection is held open across `stop()`."""
        import time
        server = self._server()
        parts = urllib.parse.urlsplit(server.url)
        conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
        self.addCleanup(conn.close)
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "lint", "arguments": {}}})
        conn.request("POST", parts.path, body=body,
                     headers={"Content-Type": "application/json"})
        self.assertEqual(conn.getresponse().read() != b"", True)
        self.assertEqual(server.in_flight(), 1)  # the handler waits for the next request
        t0 = time.monotonic()
        server.stop(join_seconds=20)
        self.assertLess(time.monotonic() - t0, 5, "stop() waited out an idle connection")
        self.assertEqual(server.in_flight(), 0)
        with self.assertRaises((OSError, http.client.HTTPException)):
            conn.request("POST", parts.path, body=body,
                         headers={"Content-Type": "application/json"})
            conn.getresponse().read()
        self.assertEqual([op for op, _ in self.calls], ["lint"])

    def test_a_call_while_stopping_runs_no_operation(self) -> None:
        server = self._server()
        server._stopping = True
        status, body, _ = server.handle_message({"jsonrpc": "2.0", "id": 3, "method":
                                                 "tools/call", "params": {"name": "compile"}})
        server._stopping = False
        self.assertEqual(body["error"]["code"], -32603)
        self.assertEqual(self.calls, [])

    def test_a_run_reported_after_stop_began_is_killed_at_once(self) -> None:
        server = self._server()
        server._stopping = True  # the state `stop()` is in while it joins
        proc = subprocess.Popen(["sleep", "120"], start_new_session=True)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        server._record_spawn(proc.pid)
        self.assertEqual(proc.wait(timeout=10), -signal.SIGKILL)
        server._stopping = False

    def test_stop_is_idempotent_and_closes_the_port(self) -> None:
        server = self._server()
        url = server.url
        server.stop()
        server.stop()
        with self.assertRaises(ConnectionRefusedError):
            _http(url, "POST", b"{}")


if __name__ == "__main__":
    unittest.main()
