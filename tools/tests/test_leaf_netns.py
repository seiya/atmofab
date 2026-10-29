"""The two halves of a `workshop leaf`'s network routes (`tools/leaf_netns.py`, issue #534 step 4).

No bwrap here: the host half is driven over its real Unix sockets, and the in-namespace half is
run as the installed copy the host half writes (`relay.py`), under the interpreter running the
suite with `-I`, against loopback stand-ins. The namespace itself — no route out, the read-only
bind of the socket directory, the kill chain through bwrap — is pinned under real bwrap in
`test_bwrap_simulation.BwrapLineageProfileTests`.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tools import leaf_netns


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Upstream:
    """A loopback TCP stand-in that records every byte it receives and answers each chunk with
    `echo:` + the chunk; `half_close_reply` answers only after the client's EOF."""

    def __init__(self, half_close_reply: bytes | None = None) -> None:
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(8)
        self.port = self.server.getsockname()[1]
        self.received = b""
        self.half_close_reply = half_close_reply
        self.done = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        try:
            conn, _ = self.server.accept()
        except OSError:
            return
        with conn:
            while True:
                data = conn.recv(65536)
                if not data:
                    break
                self.received += data
                if self.half_close_reply is None:
                    conn.sendall(b"echo:" + data)
            if self.half_close_reply is not None:
                conn.sendall(self.half_close_reply)
        self.done.set()

    def close(self) -> None:
        self.server.close()


def _recv_until(sock: socket.socket, token: bytes, limit: float = 5.0) -> bytes:
    sock.settimeout(limit)
    buf = b""
    while token not in buf:
        data = sock.recv(65536)
        if not data:
            break
        buf += data
    return buf


class HostRoutesTests(unittest.TestCase):

    def setUp(self) -> None:
        self.dir = leaf_netns.make_socket_dir()
        self.log = Path(tempfile.mkdtemp()) / "net_routes.jsonl"
        self.upstream = _Upstream()
        self.addCleanup(self.upstream.close)

    def _routes(self, endpoints: list[str] | None = None, **kw) -> leaf_netns.HostRoutes:
        routes = leaf_netns.HostRoutes(
            self.dir, endpoints if endpoints is not None else [f"localhost:{self.upstream.port}"],
            str(self.log), "arid-1", **kw).start()
        self.addCleanup(routes.stop)
        return routes

    def _client(self) -> socket.socket:
        c = socket.socket(socket.AF_UNIX)
        c.connect(os.path.join(self.dir, leaf_netns.PROVIDER_SOCKET))
        self.addCleanup(c.close)
        return c

    def _rows(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_a_connect_to_an_admitted_target_is_tunnelled_both_ways(self) -> None:
        self._routes()
        c = self._client()
        c.sendall(f"CONNECT LOCALHOST:{self.upstream.port} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        self.assertIn(b"200 Connection Established", _recv_until(c, b"\r\n\r\n"))
        c.sendall(b"ping")
        self.assertEqual(_recv_until(c, b"echo:ping"), b"echo:ping")
        self.assertEqual(self._rows(), [{
            "recorded_at_utc": self._rows()[0]["recorded_at_utc"], "agent_run_id": "arid-1",
            "target": f"localhost:{self.upstream.port}", "allowed": True}])

    def test_a_connect_to_another_target_is_refused_and_logged(self) -> None:
        self._routes(endpoints=["api.example.invalid:443"])
        c = self._client()
        c.sendall(f"CONNECT localhost:{self.upstream.port} HTTP/1.1\r\n\r\n".encode())
        self.assertTrue(_recv_until(c, b"\r\n\r\n").startswith(b"HTTP/1.1 403 "))
        self.assertEqual(c.recv(10), b"")
        rows = self._rows()
        self.assertEqual([(r["target"], r["allowed"]) for r in rows],
                         [(f"localhost:{self.upstream.port}", False)])
        self.assertEqual(self.upstream.received, b"")

    def test_a_non_connect_request_is_refused(self) -> None:
        self._routes()
        for line in (f"GET http://localhost:{self.upstream.port}/ HTTP/1.1",
                     f"CONNECT localhost:{self.upstream.port}",
                     "CONNECT localhost HTTP/1.1"):
            c = self._client()
            c.sendall(line.encode() + b"\r\n\r\n")
            self.assertTrue(_recv_until(c, b"\r\n\r\n").startswith(b"HTTP/1.1 400 "), line)
        self.assertEqual({r["allowed"] for r in self._rows()}, {False})
        self.assertEqual(self.upstream.received, b"")

    def test_bytes_pipelined_after_the_connect_header_reach_the_upstream(self) -> None:
        self._routes()
        c = self._client()
        c.sendall(f"CONNECT localhost:{self.upstream.port} HTTP/1.1\r\n\r\nhello".encode())
        got = _recv_until(c, b"echo:hello")
        self.assertIn(b"200 Connection Established", got)
        self.assertIn(b"echo:hello", got)

    def test_an_idle_tunnel_is_not_torn_down_by_a_socket_timeout(self) -> None:
        # The connect bound applies to the connect alone: a tunnel idle past it still carries.
        with mock.patch.object(leaf_netns, "UPSTREAM_CONNECT_TIMEOUT_S", 0.2):
            self._routes()
            c = self._client()
            c.sendall(f"CONNECT localhost:{self.upstream.port} HTTP/1.1\r\n\r\n".encode())
            _recv_until(c, b"\r\n\r\n")
            time.sleep(0.6)
            c.sendall(b"late")
            self.assertEqual(_recv_until(c, b"echo:late"), b"echo:late")

    def test_half_close_is_honored(self) -> None:
        upstream = _Upstream(half_close_reply=b"after-eof")
        self.addCleanup(upstream.close)
        self._routes(endpoints=[f"localhost:{upstream.port}"])
        c = self._client()
        c.sendall(f"CONNECT localhost:{upstream.port} HTTP/1.1\r\n\r\n".encode())
        _recv_until(c, b"\r\n\r\n")
        c.sendall(b"request")
        c.shutdown(socket.SHUT_WR)
        self.assertEqual(_recv_until(c, b"after-eof"), b"after-eof")
        self.assertEqual(upstream.received, b"request")

    def test_the_wrapper_socket_is_a_raw_tunnel_to_the_wrapper_port(self) -> None:
        self._routes(wrapper_port=self.upstream.port)
        c = socket.socket(socket.AF_UNIX)
        self.addCleanup(c.close)
        c.connect(os.path.join(self.dir, leaf_netns.WRAPPER_SOCKET))
        c.sendall(b"POST /mcp")
        self.assertEqual(_recv_until(c, b"echo:POST /mcp"), b"echo:POST /mcp")
        self.assertFalse(self.log.exists())

    def test_a_reading_launch_serves_no_wrapper_socket(self) -> None:
        self._routes()
        self.assertEqual(sorted(os.listdir(self.dir)),
                         [leaf_netns.PROVIDER_SOCKET, leaf_netns.RELAY_INSTALL_NAME])

    def test_stop_closes_live_pumps_and_removes_the_directory(self) -> None:
        routes = self._routes()
        c = self._client()
        c.sendall(f"CONNECT localhost:{self.upstream.port} HTTP/1.1\r\n\r\n".encode())
        _recv_until(c, b"\r\n\r\n")
        routes.stop()
        self.assertFalse(os.path.exists(self.dir))
        self.assertTrue(self.upstream.done.wait(5), "the upstream half was not shut down")
        c.settimeout(5)
        self.assertEqual(c.recv(10), b"")

    def test_a_socket_dir_too_long_for_af_unix_is_refused_before_anything_is_served(self) -> None:
        long_dir = os.path.join(self.dir, "x" * 100)
        os.mkdir(long_dir)
        with self.assertRaisesRegex(ValueError, "exceeds AF_UNIX"):
            leaf_netns.HostRoutes(long_dir, [], str(self.log), "arid-1").start()
        self.assertEqual(os.listdir(long_dir), [])

    def test_the_socket_dir_is_private_and_short(self) -> None:
        self.assertEqual(os.stat(self.dir).st_mode & 0o777, 0o700)
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": "", "TMPDIR": "/" + "t" * 90}):
            self.assertEqual(leaf_netns.socket_dir_parent(), "/tmp")


class RelayMainTests(unittest.TestCase):
    """The in-namespace half, run as the copy the host half installs."""

    def setUp(self) -> None:
        self.dir = leaf_netns.make_socket_dir()
        self.log = Path(tempfile.mkdtemp()) / "net_routes.jsonl"
        self.upstream = _Upstream()
        self.addCleanup(self.upstream.close)
        self.routes = leaf_netns.HostRoutes(
            self.dir, [f"localhost:{self.upstream.port}"], str(self.log), "arid-1").start()
        self.addCleanup(self.routes.stop)
        self.port = _free_port()

    def _relay(self, child_code: str, **kw) -> subprocess.Popen:
        argv = [sys.executable, "-I", os.path.join(self.dir, leaf_netns.RELAY_INSTALL_NAME),
                "--proxy", str(self.port), os.path.join(self.dir, leaf_netns.PROVIDER_SOCKET),
                "--", sys.executable, "-I", "-c", child_code]
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stdin=subprocess.PIPE, **kw)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        return proc

    def test_the_relay_argv_is_the_installed_copy_under_the_system_python(self) -> None:
        self.assertEqual(
            leaf_netns.relay_argv(["claude", "-p"], wrapper_port=4321),
            ["/usr/bin/python3", "-I", "/run/atmofab/net/relay.py", "--proxy", "3128",
             "/run/atmofab/net/provider.sock", "--tunnel", "4321",
             "/run/atmofab/net/wrapper.sock", "--", "claude", "-p"])
        with open(os.path.join(self.dir, leaf_netns.RELAY_INSTALL_NAME), "rb") as f:
            self.assertEqual(f.read(), Path(leaf_netns.__file__).read_bytes())

    def test_the_pump_binds_before_it_forks(self) -> None:
        # The child's FIRST action is the connect; no retry, so a listener bound after the fork
        # would refuse it.
        code = (f"import socket,sys\ns=socket.create_connection(('127.0.0.1',{self.port}))\n"
                f"s.sendall(b'CONNECT localhost:{self.upstream.port} HTTP/1.1\\r\\n\\r\\nhi')\n"
                "b=b''\nwhile b'echo:hi' not in b:\n    d=s.recv(999)\n    if not d: break\n"
                "    b+=d\nsys.stdout.write(b.decode()); sys.exit(0 if b'echo:hi' in b else 3)\n")
        proc = self._relay(code)
        out, _ = proc.communicate(timeout=20)
        self.assertEqual(proc.returncode, 0, out)
        self.assertIn(b"200 Connection Established", out)

    def test_the_child_reads_the_inherited_stdin(self) -> None:
        proc = self._relay("import sys; sys.stdout.write(sys.stdin.read().upper())")
        out, _ = proc.communicate(b"prompt", timeout=20)
        self.assertEqual(out, b"PROMPT")

    def test_the_pump_mirrors_the_childs_exit_code_and_signal(self) -> None:
        proc = self._relay("import sys; sys.exit(7)")
        proc.communicate(timeout=20)
        self.assertEqual(proc.returncode, 7)
        proc = self._relay("import os,signal; os.kill(os.getpid(), signal.SIGKILL)")
        proc.communicate(timeout=20)
        self.assertEqual(proc.returncode, 128 + signal.SIGKILL)

    def test_the_pump_forwards_sigterm_to_its_child(self) -> None:
        code = ("import signal,sys,time\n"
                "signal.signal(signal.SIGTERM, lambda *a: sys.exit(5))\n"
                "print('ready', flush=True)\nwhile True: time.sleep(1)\n")
        proc = self._relay(code)
        self.assertEqual(proc.stdout.readline(), b"ready\n")
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=20), 5)

    def test_the_child_carries_the_death_signal(self) -> None:
        code = "import os,time\nprint(os.getpid(), flush=True)\nwhile True: time.sleep(1)\n"
        proc = self._relay(code)
        child_pid = int(proc.stdout.readline())
        proc.kill()
        proc.wait(timeout=20)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with open(f"/proc/{child_pid}/stat") as f:
                    state = f.read().rsplit(")", 1)[1].split()[0]
            except FileNotFoundError:
                return
            if state == "Z":
                return
            time.sleep(0.05)
        self.fail(f"the relay's child {child_pid} outlived the relay")


if __name__ == "__main__":
    unittest.main()
