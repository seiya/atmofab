#!/usr/bin/env python3
"""Measure what a Claude Code TOOL actually reaches, by driving it. Unbilled.

WHY THIS EXISTS. Issue #71's `Glob` pattern check refuses only an ABSOLUTE pattern, and
that narrowing deleted 164 lines of defence on the strength of ONE measurement: every
relative spelling reads nothing. A rule that rests on a vendor behaviour needs the
measurement to be re-runnable, and four earlier attempts to answer this question were
wrong because they measured something ELSE and wrote it down as the tool — Python's `glob`
twice, bare `ripgrep` once, and a tool-driving harness whose fixture could not tell "the
tool is confined" from "the target was absent". So: drive the tool, and saturate the
fixture.

HOW IT WORKS. A loopback stand-in for the Messages endpoint answers turn one with a
synthetic `tool_use` for the tool under test; the CLI executes it locally and sends the
`tool_result` in its next request, which this reads. No model runs, so nothing is billed.

TWO TRAPS, both of which cost a measurement here:
  * The CLI sends MORE THAN ONE request per turn. A stand-in that counts requests answers
    the second one as if it were the tool result and ends the conversation before the tool
    runs. Decide on message CONTENT: keep replying with the `tool_use` until a
    `tool_result` appears.
  * "No files found" is only evidence if the target EXISTS. The fixture below puts the
    repository two levels down and a marked file in `secret/` and `outside/` at every
    ancestor a pattern could resolve to, plus symlinks out of the granted directory.

USAGE
    python3 .claude/skills/metdsl-enforcement-change/scripts/measure_claude_tool.py
    python3 ...  --cases my_cases.json     # [[tool, tool_input], ...]

The default case list is the one issue #71's narrowing rests on: every spelling the check
stopped examining, plus the absolute spellings it still refuses. Re-run it after a CLI
upgrade; if a RELATIVE row starts reading, the narrowing's premise is gone and
`tools/hooks/cli.py`'s pattern check has to widen again.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_CASES = [
    # Relative — measured INERT, which is what the narrowing rests on.
    ["Glob", {"path": "docs", "pattern": "../secret/*"}],
    ["Glob", {"path": "docs", "pattern": "../../secret/*"}],
    ["Glob", {"path": "docs", "pattern": "../../../secret/*"}],
    ["Glob", {"path": "docs", "pattern": "*/../../secret/*"}],
    ["Glob", {"path": "docs", "pattern": "{../secret,sub}/*"}],
    ["Glob", {"path": "docs", "pattern": "{sub,../secret}/*"}],
    ["Glob", {"path": "docs", "pattern": "linkdir/*"}],
    ["Glob", {"path": "docs", "pattern": "docs/linkdir/*"}],
    ["Glob", {"path": "docs", "pattern": "docs/linkfile.txt"}],
    ["Glob", {"path": "docs", "pattern": "~/.bashrc"}],
    ["Glob", {"path": "docs", "pattern": "$HOME/.bashrc"}],
    ["Glob", {"path": "docs", "pattern": " /etc/hostname"}],
    ["Glob", {"path": "docs", "pattern": "\t/etc/hostname"}],
    # Absolute — measured to READ, which is what the check still refuses.
    ["Glob", {"path": "docs", "pattern": "/etc/hostname"}],
    ["Glob", {"path": "docs", "pattern": "//etc/hostname"}],
    # Controls: these MUST return files, or an empty result above proves nothing.
    ["Glob", {"path": "docs", "pattern": "docs/**/*.md"}],
    ["Glob", {"path": "docs", "pattern": "*.md"}],
    # `Grep`'s filters, the neighbouring residue, closed as measured-negative.
    ["Grep", {"path": "docs", "pattern": "MARK", "glob": "../../secret/*",
              "output_mode": "files_with_matches"}],
    ["Grep", {"path": "docs", "pattern": "MARK", "output_mode": "files_with_matches"}],
]


def build_fixture() -> Path:
    """A repository two levels down, with a marked file everywhere a pattern could land."""
    base = Path(tempfile.mkdtemp(prefix="metdsl-toolmeasure-"))
    repo = base / "a" / "b" / "repo"
    (repo / "docs" / "sub").mkdir(parents=True)
    (repo / "docs" / "a.md").write_text("MARK docs/a.md")
    (repo / "docs" / "sub" / "b.md").write_text("MARK docs/sub/b.md")
    for ancestor in (repo, repo.parent, repo.parent.parent, base):
        for name in ("secret", "outside"):
            directory = ancestor / name
            directory.mkdir(exist_ok=True)
            (directory / "MARK.txt").write_text(f"MARK {directory}")
    os.symlink(base / "outside", repo / "docs" / "linkdir")
    os.symlink(base / "outside" / "MARK.txt", repo / "docs" / "linkfile.txt")
    return repo


def _serve(tool: str, tool_input: dict, out: Path) -> ThreadingHTTPServer:
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            """The default logs every request to stderr."""

        def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's spelling
            length = int(self.headers.get("Content-Length") or 0)
            try:
                document = json.loads(self.rfile.read(length) or b"{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                document = {}
            results = [
                block.get("content")
                for message in document.get("messages", [])
                if isinstance(message.get("content"), list)
                for block in message["content"]
                if isinstance(block, dict) and block.get("type") == "tool_result"
            ]
            if results:
                with lock:
                    out.write_text(json.dumps(results, default=str))
                payload = {"id": "m2", "type": "message", "role": "assistant",
                           "model": "probe", "stop_reason": "end_turn",
                           "stop_sequence": None,
                           "content": [{"type": "text", "text": "done"}],
                           "usage": {"input_tokens": 1, "output_tokens": 1}}
            else:
                payload = {"id": "m1", "type": "message", "role": "assistant",
                           "model": "probe", "stop_reason": "tool_use",
                           "stop_sequence": None,
                           "content": [{"type": "tool_use", "id": "t1", "name": tool,
                                        "input": tool_input}],
                           "usage": {"input_tokens": 1, "output_tokens": 1}}
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's spelling
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = False
    threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05),
                     daemon=True).start()
    return server


def probe(repo: Path, tool: str, tool_input: dict, command: str, timeout: int) -> str:
    with tempfile.TemporaryDirectory() as scratch:
        out = Path(scratch) / "result.json"
        server = _serve(tool, tool_input, out)
        try:
            host, port = server.server_address[0], server.server_address[1]
            env = dict(os.environ)
            env.pop("ANTHROPIC_AUTH_TOKEN", None)
            env.update(ANTHROPIC_BASE_URL=f"http://{host}:{port}",
                       ANTHROPIC_API_KEY="metdsl-tool-measurement",
                       CLAUDE_CONFIG_DIR=tempfile.mkdtemp(prefix="metdsl-toolhome-"))
            try:
                subprocess.run(
                    [command, "--setting-sources", "", "--dangerously-skip-permissions",
                     "--tools", "Glob,Grep", "--output-format", "json", "-p"],
                    input=".", env=env, cwd=str(repo), capture_output=True, text=True,
                    timeout=timeout)
            except subprocess.TimeoutExpired:
                return "TIMEOUT"
        finally:
            server.shutdown()
            server.server_close()
        if not out.exists():
            return "NO RESULT (the tool did not run)"
        return json.dumps(json.loads(out.read_text()))[:150]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", help="JSON file of [[tool, tool_input], ...]")
    parser.add_argument("--command", default="claude")
    parser.add_argument("--timeout", type=int, default=200)
    args = parser.parse_args(argv)

    cases = json.loads(Path(args.cases).read_text()) if args.cases else DEFAULT_CASES
    repo = build_fixture()
    version = subprocess.run([args.command, "--version"], capture_output=True,
                             text=True).stdout.strip()
    print(f"cli: {version}\nfixture: {repo}\n")
    for tool, tool_input in cases:
        label = json.dumps(tool_input)
        print(f"{tool:5s} {label[:56]:58s} -> {probe(repo, tool, tool_input, args.command, args.timeout)}",
              flush=True)
    print("\nEvery RELATIVE row must read nothing and every CONTROL row must read files.\n"
          "A relative row that reads is the end of issue #71's narrowing premise:\n"
          "`tools/hooks/cli.py`'s pattern check would have to widen again.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
