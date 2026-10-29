"""The network namespace of a `workshop leaf`'s CLI (issue #534 step 4): its two routes out.

`docs/design/producer_workshop_leaf.md` §Sandbox row 1 bounds a leaf's network to "the
provider's endpoint set and the wrapper endpoint only". `render_bwrap_command` puts the leaf in
a namespace of its own (`--unshare-net`: `lo` alone, no route out, no name resolution), and this
module is what gives it exactly those two routes back. It has two halves, one file:

- THE HOST HALF (`HostRoutes`), in the conductor's process for the life of one launch, serves
  two path-bound Unix sockets in a short per-launch directory (`make_socket_dir`) — a path-bound
  `AF_UNIX` socket is reachable across a network namespace, and the renderer binds the directory
  read-only at `NET_MOUNT`. `provider.sock` is an HTTP CONNECT proxy that admits exactly the
  endpoint set it was handed (`host:port`, exact match) and logs every request whose header block it reads to
  `net_routes.jsonl`, refused ones included; TLS passes through it untouched, so the CLI checks
  the real host's certificate. `wrapper.sock` (a producing launch only) is a raw tunnel to the
  wrapper endpoint's loopback port (`tools/workshop_mcp.py`).
- THE IN-NAMESPACE HALF (`main`), run by bwrap as the sandboxed command
  (`/usr/bin/python3 -I <NET_MOUNT>/relay.py`, a copy of this file the host half installs), is
  a byte pump with no policy in it: it listens on `127.0.0.1:PROXY_PORT` (to `provider.sock`)
  and on the wrapper's own port number (to `wrapper.sock`), then starts the CLI as its child and
  exits with the child's code. The CLI is told the proxy by `HTTPS_PROXY`, which the profile
  builder authors (`PROXY_ENV`, set by `orchestration_runtime.build_lineage_bwrap_profile`).

Why a proxy variable and not a route on port 443: both CLIs' OAuth hosts are fixed at `:443`,
and in the leaf's sandbox shape, which mounts `--dev /dev`, a bind of port 443 fails `EACCES` even
under `--cap-add CAP_NET_BIND_SERVICE` with the capability in the effective set (measured, bwrap
0.11.2; the same bind succeeds without `--dev`, for a reason not established). And a route would
reach the host as TLS, whose target the host could tell only by parsing SNI; a CONNECT proxy needs
no privilege and names every target host in cleartext on the host side, which is what makes the
allowlist a host decision.

The in-namespace half runs under the system python with `-I` and no `PYTHONPATH`: this module
imports the standard library alone and nothing from the checkout.
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

#: The fixed in-sandbox mount point of the per-launch socket directory. `/run` is not among the
#: system binds, so bwrap creates the mount point on its own root; the host `/tmp` path would be
#: covered by the sandbox's `--tmpfs /tmp`.
NET_MOUNT = "/run/atmofab/net"
#: The in-namespace proxy listener the leaf's `HTTPS_PROXY` names.
PROXY_HOST = "127.0.0.1"
PROXY_PORT = 3128
PROXY_LISTEN = f"{PROXY_HOST}:{PROXY_PORT}"
#: The installed name of this file in the socket directory, and the two socket names.
RELAY_INSTALL_NAME = "relay.py"
PROVIDER_SOCKET = "provider.sock"
WRAPPER_SOCKET = "wrapper.sock"
#: The interpreter the in-namespace half runs under: the system one, under `/usr`, which the
#: sandbox binds. `-I`: the pump's working directory is the leaf's tree, which the leaf writes,
#: and a leaf-authored `socket.py` there would otherwise be imported.
RELAY_PYTHON = "/usr/bin/python3"
#: `sizeof(sockaddr_un.sun_path) - 1` on Linux: a longer socket path cannot be bound.
SUN_PATH_MAX = 107
#: The longest CONNECT header block the host half reads before refusing the request.
HEADER_LIMIT = 16384
#: The bound on connecting to an admitted upstream. Once connected the tunnel has no timeout: a
#: streamed model reply can be idle for minutes.
UPSTREAM_CONNECT_TIMEOUT_S = 30.0

_BLOCK = 65536

#: The environment the profile builder authors for the leaf: the in-namespace proxy for every
#: HTTPS request, and the loopback names (the wrapper's `http://127.0.0.1:<port>` URL) direct.
PROXY_ENV: dict[str, str] = {"HTTPS_PROXY": f"http://{PROXY_LISTEN}",
                             "NO_PROXY": "127.0.0.1,localhost"}


def network_block(socket_dir: str, endpoints: list[str] | tuple[str, ...],
                  wrapper_port: int | None) -> dict:
    """The `network` block a workshop profile records: what the renderer emits and what every
    reader of the profile requires (`network_block_violations`)."""
    return {"unshare_net": True, "unshare_pid": True, "socket_dir": str(socket_dir),
            "mount": NET_MOUNT, "proxy_listen": PROXY_LISTEN, "wrapper_port": wrapper_port,
            "endpoints": list(endpoints)}


def network_block_violations(block: object, *, endpoints: list[str] | tuple[str, ...],
                             repo_root: str | None) -> list[str]:
    """What is wrong with a profile's `network` block, empty when nothing is. ONE reader for
    the three readers of a persisted profile — the renderer, the spawn admission and the
    launch-record audit — each of which refuses on any answer. `endpoints` is the family's
    CONSTANT (`leaf_contract.WORKSHOP_CLI_ENDPOINTS`): the block's copy is a record checked
    against it, never a source the host half reads. Reads no file: the socket directory is
    gone once the launch ends, and the audit reads the record after that."""
    if not isinstance(block, dict):
        return [(f"the profile carries no network block (got {block!r}); every CLI leaf runs "
                 "in a network namespace since issue #534 step 4")]
    out: list[str] = []
    for key in ("unshare_net", "unshare_pid"):
        if block.get(key) is not True:
            out.append(f"network.{key} must be true (got {block.get(key)!r})")
    if block.get("mount") != NET_MOUNT:
        out.append(f"network.mount must be {NET_MOUNT!r} (got {block.get('mount')!r})")
    if block.get("proxy_listen") != PROXY_LISTEN:
        out.append(f"network.proxy_listen must be {PROXY_LISTEN!r} "
                   f"(got {block.get('proxy_listen')!r})")
    sdir = block.get("socket_dir")
    if not (isinstance(sdir, str) and os.path.isabs(sdir) and sdir == os.path.normpath(sdir)
            and sdir != "/"):
        out.append(f"network.socket_dir must be a normalized absolute path (got {sdir!r})")
    else:
        if len(os.path.join(sdir, PROVIDER_SOCKET).encode()) > SUN_PATH_MAX:
            out.append(f"network.socket_dir is too long for an AF_UNIX path (got {sdir!r})")
        if repo_root:
            repo = os.path.normpath(os.path.abspath(repo_root))
            if (sdir == repo or sdir.startswith(repo.rstrip("/") + "/")
                    or repo.startswith(sdir.rstrip("/") + "/")):
                out.append(f"network.socket_dir must lie outside the checkout {repo} "
                           f"(got {sdir!r})")
    port = block.get("wrapper_port")
    if port is not None and not (isinstance(port, int) and not isinstance(port, bool)
                                 and 0 < port < 65536 and port != PROXY_PORT):
        out.append(f"network.wrapper_port must be null or a port other than {PROXY_PORT} "
                   f"(got {port!r})")
    if block.get("endpoints") != list(endpoints):
        out.append(f"network.endpoints must be the family's endpoint set {list(endpoints)!r} "
                   f"(got {block.get('endpoints')!r})")
    return out


def relay_argv(command_argv: list[str] | tuple[str, ...],
               wrapper_port: int | None = None) -> list[str]:
    """The sandboxed command line: the in-namespace half, then `--`, then the CLI argv."""
    argv = [RELAY_PYTHON, "-I", f"{NET_MOUNT}/{RELAY_INSTALL_NAME}",
            "--proxy", str(PROXY_PORT), f"{NET_MOUNT}/{PROVIDER_SOCKET}"]
    if wrapper_port is not None:
        argv += ["--tunnel", str(wrapper_port), f"{NET_MOUNT}/{WRAPPER_SOCKET}"]
    return argv + ["--", *command_argv]


def socket_dir_parent() -> str:
    """Where a per-launch socket directory is created: `$XDG_RUNTIME_DIR` when it is a writable
    directory whose socket paths fit `SUN_PATH_MAX`, normalized (a profile records only a
    normalized path), else `/tmp` — never `tempfile.gettempdir()`, which follows `TMPDIR` and can
    name a path too long for a socket under it."""
    runtime = os.path.normpath(os.environ.get("XDG_RUNTIME_DIR") or "/nonexistent")
    # The longest socket path under it: `<dir>/atmofab-net-XXXXXXXX/provider.sock`.
    longest = os.path.join(runtime, "atmofab-net-" + "x" * 8, PROVIDER_SOCKET)
    if os.path.isabs(runtime) and os.path.isdir(runtime) \
            and os.access(runtime, os.W_OK | os.X_OK) \
            and len(longest.encode()) <= SUN_PATH_MAX:
        return runtime
    return "/tmp"


def make_socket_dir() -> str:
    """Create one launch's socket directory (0700, this user's) and return its path."""
    path = tempfile.mkdtemp(prefix="atmofab-net-", dir=socket_dir_parent())
    os.chmod(path, 0o700)
    return path


def _splice(a: socket.socket, b: socket.socket) -> None:
    """Pump bytes both ways until both directions end. EOF on one side shuts only the OTHER
    side's write half, so a peer that half-closes still reads the reply."""
    def one_way(src: socket.socket, dst: socket.socket) -> None:
        try:
            while True:
                data = src.recv(_BLOCK)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass
    other = threading.Thread(target=one_way, args=(b, a), daemon=True)
    other.start()
    one_way(a, b)
    other.join()
    for s in (a, b):
        try:
            s.close()
        except OSError:
            pass


def _read_header_block(conn: socket.socket) -> tuple[bytes, bytes] | None:
    """The bytes up to and including the blank line, and whatever followed them; None on EOF
    or an over-long header block."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        if len(buf) > HEADER_LIMIT:
            return None
        data = conn.recv(4096)
        if not data:
            return None
        buf += data
    head, _, rest = buf.partition(b"\r\n\r\n")
    return head, rest


def parse_connect_target(head: bytes) -> str | None:
    """The `host:port` of a `CONNECT host:port HTTP/1.x` request line (host lower-cased), or None
    for any other request."""
    line = head.split(b"\r\n", 1)[0].decode("latin-1")
    parts = line.split(" ")
    if len(parts) != 3 or parts[0] != "CONNECT" or not parts[2].startswith("HTTP/1."):
        return None
    host, sep, port = parts[1].rpartition(":")
    # ASCII digits only: `str.isdigit` also accepts `²`, which `int()` refuses.
    if not sep or not host or not (port.isascii() and port.isdigit()):
        return None
    return f"{host.lower()}:{int(port)}"


class HostRoutes:
    """The host half for ONE launch: `start()` installs the relay and serves the sockets,
    `stop()` closes every listener and live tunnel and removes the directory."""

    def __init__(self, socket_dir: str, endpoints: list[str] | tuple[str, ...], log_path: str,
                 agent_run_id: str, wrapper_port: int | None = None) -> None:
        self.socket_dir = os.path.abspath(socket_dir)
        self.endpoints = frozenset(endpoints)
        self.log_path = log_path
        self.agent_run_id = agent_run_id
        self.wrapper_port = wrapper_port
        self._listeners: list[socket.socket] = []
        self._acceptors: list[threading.Thread] = []
        self._live: set[socket.socket] = set()
        self._lock = threading.Lock()
        self._stopped = False

    def start(self) -> HostRoutes:
        for name in (PROVIDER_SOCKET, WRAPPER_SOCKET):
            path = os.path.join(self.socket_dir, name)
            if len(path.encode()) > SUN_PATH_MAX:
                raise ValueError(f"the socket path {path!r} exceeds AF_UNIX's {SUN_PATH_MAX} "
                                 "bytes; the socket directory must be short (make_socket_dir)")
        relay = os.path.join(self.socket_dir, RELAY_INSTALL_NAME)
        with open(__file__, "rb") as src:
            source = src.read()
        fd = os.open(relay, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        with os.fdopen(fd, "wb") as out:
            out.write(source)
        try:
            self._serve(PROVIDER_SOCKET, self._handle_connect)
            if self.wrapper_port is not None:
                self._serve(WRAPPER_SOCKET, self._handle_wrapper)
        except BaseException:
            # A half-started host half (the wrapper's socket failed after the provider's was
            # served) is stopped here: the caller holds no object to stop, and its accept
            # thread would otherwise outlive the launch.
            self.stop()
            raise
        return self

    def _serve(self, name: str, handler) -> None:  # type: ignore[no-untyped-def]
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(os.path.join(self.socket_dir, name))
        listener.listen(64)
        self._listeners.append(listener)

        def accept_loop() -> None:
            while True:
                try:
                    conn, _ = listener.accept()
                except OSError:
                    return
                threading.Thread(target=self._guarded, args=(handler, conn),
                                 daemon=True).start()
        acceptor = threading.Thread(target=accept_loop, daemon=True)
        acceptor.start()
        self._acceptors.append(acceptor)

    def _track(self, *socks: socket.socket) -> bool:
        with self._lock:
            if self._stopped:
                return False
            self._live.update(socks)
            return True

    def _guarded(self, handler, conn: socket.socket) -> None:  # type: ignore[no-untyped-def]
        if not self._track(conn):
            conn.close()
            return
        try:
            handler(conn)
        except OSError:
            pass
        finally:
            with self._lock:
                self._live.discard(conn)
            try:
                conn.close()
            except OSError:
                pass

    def _log(self, target: str, allowed: bool, request_line: str) -> None:
        row = {"recorded_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "agent_run_id": self.agent_run_id, "target": target[:200], "allowed": allowed}
        if not target:
            row["request_line"] = request_line[:200]
        with self._lock, open(self.log_path, "a", encoding="utf-8") as out:
            out.write(json.dumps(row, sort_keys=True) + "\n")

    def _handle_connect(self, conn: socket.socket) -> None:
        got = _read_header_block(conn)
        if got is None:
            return
        head, rest = got
        target = parse_connect_target(head)
        allowed = target is not None and target in self.endpoints
        self._log(target or "", allowed, head.split(b"\r\n", 1)[0].decode("latin-1"))
        if target is None:
            conn.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n"
                         b"Connection: close\r\n\r\n")
            return
        if not allowed:
            conn.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n"
                         b"Connection: close\r\n\r\n")
            return
        host, _, port = target.rpartition(":")
        try:
            upstream = socket.create_connection((host, int(port)),
                                                timeout=UPSTREAM_CONNECT_TIMEOUT_S)
        except OSError:
            conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n"
                         b"Connection: close\r\n\r\n")
            return
        upstream.settimeout(None)
        if not self._track(upstream):
            upstream.close()
            return
        try:
            conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            if rest:
                upstream.sendall(rest)
            _splice(conn, upstream)
        finally:
            with self._lock:
                self._live.discard(upstream)

    def _handle_wrapper(self, conn: socket.socket) -> None:
        upstream = socket.create_connection(("127.0.0.1", int(self.wrapper_port or 0)),
                                            timeout=UPSTREAM_CONNECT_TIMEOUT_S)
        upstream.settimeout(None)
        if not self._track(upstream):
            upstream.close()
            return
        try:
            _splice(conn, upstream)
        finally:
            with self._lock:
                self._live.discard(upstream)

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            live = list(self._live)
        # `close()` alone does not wake a thread blocked in `accept()` on Linux: that thread,
        # and with it the listener, would outlive the launch, one pair per launch for the life
        # of the conductor. `shutdown` wakes it (`accept` fails), and the join bounds the wait.
        for listener in self._listeners:
            for close in (lambda: listener.shutdown(socket.SHUT_RDWR), listener.close):
                try:
                    close()
                except OSError:
                    pass
        for acceptor in self._acceptors:
            acceptor.join(timeout=5)
        for s in live:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        shutil.rmtree(self.socket_dir, ignore_errors=True)


# --- the in-namespace half -----------------------------------------------------------------

_PR_SET_PDEATHSIG = 1


def _parse_main_args(args: list[str]) -> tuple[list[tuple[int, str]], list[str]]:
    routes: list[tuple[int, str]] = []
    i = 0
    seen_proxy = False
    while i < len(args) and args[i] != "--":
        flag = args[i]
        if flag not in ("--proxy", "--tunnel") or i + 2 >= len(args):
            raise SystemExit(f"relay: bad argument {flag!r}")
        if flag == "--proxy":
            if seen_proxy:
                raise SystemExit("relay: one --proxy")
            seen_proxy = True
        routes.append((int(args[i + 1]), args[i + 2]))
        i += 3
    argv = args[i + 1:]
    # The production head always carries `--proxy` (the renderer emits it); a nested relay a
    # test runs inside it carries tunnels alone.
    if not routes or not argv:
        raise SystemExit("relay: usage: [--proxy PORT SOCK] [--tunnel PORT SOCK]... -- ARGV")
    return routes, argv


def _death_signal_preexec():  # type: ignore[no-untyped-def]
    """A `preexec_fn` that ties the child to this process: SIGKILL when the pump dies (the
    death signal bwrap's `--die-with-parent` sets is on the pump and is cleared on fork). Built
    before the fork: the libc handle is loaded here, not in the child."""
    libc = ctypes.CDLL(None, use_errno=True)
    prctl = libc.prctl
    parent = os.getpid()

    def preexec() -> None:
        prctl(_PR_SET_PDEATHSIG, int(signal.SIGKILL), 0, 0, 0)
        if os.getppid() != parent:
            os._exit(137)
    return preexec


def _tunnel_to(sock_path: str):  # type: ignore[no-untyped-def]
    def handle(conn: socket.socket) -> None:
        unix = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            unix.connect(sock_path)
        except OSError:
            unix.close()
            conn.close()
            return
        _splice(conn, unix)
    return handle


def main(args: list[str]) -> int:
    routes, argv = _parse_main_args(args)
    listeners: list[tuple[socket.socket, str]] = []
    # Every listener is bound BEFORE the CLI starts: its first connection waits in the backlog.
    for port, sock_path in routes:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((PROXY_HOST, port))
        listener.listen(128)
        listeners.append((listener, sock_path))
    # The fork happens while this process is single-threaded (`preexec_fn` is not safe beside
    # other threads); the accept threads start after it. The listeners are not inherited
    # (`close_fds`), and the child keeps this process's stdio: the prompt arrives on stdin.
    child = subprocess.Popen(argv, preexec_fn=_death_signal_preexec())  # noqa: PLW1509 — single-threaded here

    def forward(signum: int, _frame) -> None:  # type: ignore[no-untyped-def]
        try:
            child.send_signal(signum)
        except OSError:
            pass
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, forward)

    for listener, sock_path in listeners:
        handle = _tunnel_to(sock_path)

        def accept_loop(listener: socket.socket = listener, handle=handle) -> None:  # type: ignore[no-untyped-def]
            while True:
                try:
                    conn, _ = listener.accept()
                except OSError:
                    return
                threading.Thread(target=handle, args=(conn,), daemon=True).start()
        threading.Thread(target=accept_loop, daemon=True).start()
    rc = child.wait()
    # A signal death is reported the way a shell reports it, `128 + n`.
    return rc if rc >= 0 else 128 - rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
