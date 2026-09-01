#!/usr/bin/env python3
"""Measure what a Claude Code TOOL actually reaches, by driving it, and JUDGE the result.

WHY THIS EXISTS. Issue #71's `Glob` pattern check refuses only a pattern beginning with
`/` or `~`, and that narrowing deleted 164 lines of defence on the strength of ONE
measurement: every relative spelling reads nothing. A rule that rests on a vendor
behaviour needs the measurement to be re-runnable, and four earlier attempts to answer
this question were wrong because they measured something ELSE and wrote it down as the
tool — Python's `glob` twice, bare `ripgrep` once, and a tool-driving harness whose
fixture could not tell "the tool is confined" from "the target was absent". So: drive the
tool, saturate the fixture, and print a VERDICT rather than rows for a human to classify.

HOW IT WORKS. A loopback stand-in for the Messages endpoint answers turn one with a
synthetic `tool_use` for the tool under test; the CLI executes it locally and sends the
`tool_result` in its next request, which this reads. No model runs, so nothing is billed.

FOUR TRAPS, each of which cost a measurement here:
  * The CLI sends MORE THAN ONE request per turn. A stand-in that counts requests answers
    the second one as if it were the tool result and ends the conversation before the tool
    runs. Decide on message CONTENT: keep replying with the `tool_use` until a
    `tool_result` appears.
  * "No files found" is only evidence if the target EXISTS. The fixture puts the
    repository two levels down and a marked file in `secret/` and `outside/` at every
    ancestor a pattern could resolve to, plus symlinks out of the granted directory. Every
    absolute row names a path INSIDE that fixture rather than a host path such as
    `/etc/hostname`, which proves nothing on a host that lacks it — and `$HOME` is pointed
    at the fixture too, so the `~` rows are saturated like the others instead of relying
    on the operator happening to have a `~/.bashrc`.
  * The environment is an ALLOWLIST, not `os.environ` minus a few names. A denylist here
    has the polarity issue #71 rejected for the leaf launch: `CLAUDE_CODE_USE_BEDROCK`,
    `CLAUDE_CODE_USE_VERTEX` or a proxy variable sends the CLI somewhere other than the
    stand-in, and a measurement taken through a real endpoint is both billed and wrong.
  * A row a reader has to classify by eye is a row that gets classified wrong. Each case
    declares whether it must READ or must be INERT, `main` compares, and the exit code is
    the answer — 0 only if every expectation held.

USAGE
    python3 .claude/skills/metforge-enforcement-change/scripts/measure_claude_tool.py
    python3 ...  --cases my_cases.json    # [[expectation, tool, tool_input], ...] where
                                          # expectation is "reads" or "inert"; the strings
                                          # {BASE} and {REPO} are substituted with the
                                          # fixture paths. `--tools` is derived from the
                                          # tool names present, so any tool can be driven.

The default case list covers every spelling the check stopped examining, the absolute
spellings it still refuses, and the controls that make an empty result mean something.
Re-run it after a CLI upgrade; if an INERT row starts reading, the narrowing's premise is
gone and `tools/hooks/cli.py`'s pattern check has to widen again.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# (expectation, tool, tool_input). "reads" = the tool must return files; "inert" = it must
# not. A control is an "reads" row whose pattern is plainly inside the granted directory:
# without one, every row returning nothing reads as confinement when it may be breakage.
DEFAULT_CASES = [
    # --- INERT: relative spellings. This is what the narrowing rests on. ---
    ["inert", "Glob", {"path": "docs", "pattern": "../secret/*"}],
    ["inert", "Glob", {"path": "docs", "pattern": "../../secret/*"}],
    ["inert", "Glob", {"path": "docs", "pattern": "../../../secret/*"}],
    ["inert", "Glob", {"path": "docs", "pattern": "*/../../secret/*"}],
    ["inert", "Glob", {"path": "docs", "pattern": "sub/../../secret/*"}],
    ["inert", "Glob", {"path": "docs", "pattern": "{../secret,sub}/*"}],
    ["inert", "Glob", {"path": "docs", "pattern": "{sub,../secret}/*"}],
    # An ABSOLUTE alternative inside a brace, BOTH ORDERS. `docs/HOOKS.md` rests its
    # strongest sentence on these two rows ("the tool asks `isAbsolute` of the WHOLE
    # string"), and they were the ones the first version of this list omitted.
    ["inert", "Glob", {"path": "docs", "pattern": "{{BASE}/secret,sub}/*"}],
    ["inert", "Glob", {"path": "docs", "pattern": "{sub,{BASE}/secret}/*"}],
    # Symlinks out of the granted directory: a directory link and a file link.
    ["inert", "Glob", {"path": "docs", "pattern": "linkdir/*"}],
    ["inert", "Glob", {"path": "docs", "pattern": "docs/linkdir/*"}],
    ["inert", "Glob", {"path": "docs", "pattern": "docs/linkfile.txt"}],
    # `~` and `$HOME` — saturated, because HOME is the fixture base (see `probe`).
    ["inert", "Glob", {"path": "docs", "pattern": "~/secret/*"}],
    ["inert", "Glob", {"path": "docs", "pattern": "$HOME/secret/*"}],
    # Leading whitespace before an absolute path: the tool asks `isAbsolute` of the whole
    # string, so neither is absolute to it. The TAB row is why `.strip()` was deleted.
    ["inert", "Glob", {"path": "docs", "pattern": " {BASE}/secret/*"}],
    ["inert", "Glob", {"path": "docs", "pattern": "\t{BASE}/secret/*"}],
    # --- READS: absolute spellings. This is what the check still refuses. ---
    ["reads", "Glob", {"path": "docs", "pattern": "{BASE}/secret/*"}],
    ["reads", "Glob", {"path": "docs", "pattern": "/{BASE}/secret/*"}],
    # Braces and `..` and a character class AFTER the leading slash: still absolute.
    ["reads", "Glob", {"path": "docs", "pattern": "{BASE}/{secret,outside}/*"}],
    ["reads", "Glob", {"path": "docs", "pattern": "{BASE}/secret/../secret/*"}],
    ["reads", "Glob", {"path": "docs", "pattern": "{BASE}/[s]ecret/*"}],
    # --- CONTROLS: must read, or every empty result above proves nothing. ---
    ["reads", "Glob", {"path": "docs", "pattern": "docs/**/*.md"}],
    ["reads", "Glob", {"path": "docs", "pattern": "*.md"}],
    # --- `Grep`'s filters: the neighbouring residue, closed as measured-negative. ---
    ["inert", "Grep", {"path": "docs", "pattern": "MARK", "glob": "../../secret/*",
                       "output_mode": "files_with_matches"}],
    ["inert", "Grep", {"path": "docs", "pattern": "MARK", "glob": "{BASE}/secret/*",
                       "output_mode": "files_with_matches"}],
    ["reads", "Grep", {"path": "docs", "pattern": "MARK",
                       "output_mode": "files_with_matches"}],
]

# The CLI is given exactly these host variables, plus the endpoint override. PATH finds
# the executable and the tools it shells out to; HOME is overridden per probe.
_ENV_ALLOWLIST = ("PATH", "LANG", "LC_ALL", "TERM", "TMPDIR", "SHELL", "USER", "LOGNAME")

# What the tool says when it found nothing. Read as a SUBSTRING of the tool_result, so a
# row is judged on the tool's own words rather than on the length of a list.
_EMPTY_MARKERS = ("No files found", "No matches found")

# The launch did not produce a tool result at all. These are NOT an answer in either
# direction and are failed regardless of what the row expects: scored as "inert" they read
# as confinement, scored as "reads" they satisfy an absolute row — either way the harness
# would report a premise it never measured.
_ERROR_MARKERS = ("TIMEOUT", "NO RESULT")


def substitute(value, base: Path, repo: Path):
    """Replace `{BASE}` / `{REPO}` in every string of a case, recursively.

    Plain `str.replace`, not `str.format`: half these patterns contain a brace pair that
    is the thing under test, and `format` would raise on them.
    """
    if isinstance(value, str):
        return value.replace("{BASE}", str(base)).replace("{REPO}", str(repo))
    if isinstance(value, dict):
        return {k: substitute(v, base, repo) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, base, repo) for v in value]
    return value


def build_fixture() -> Path:
    """A repository two levels down, with a marked file everywhere a pattern could land."""
    base = Path(tempfile.mkdtemp(prefix="metforge-toolmeasure-"))
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


def read_files(result: str) -> bool:
    """Did the tool return anything? Decided on the tool's own empty-result wording."""
    return not any(marker in result for marker in _EMPTY_MARKERS)


def launch_failed(result: str) -> bool:
    """Did the CLI fail to produce a tool result? Neither `reads` nor `inert`."""
    return any(result.startswith(marker) for marker in _ERROR_MARKERS)


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


def probe(repo: Path, tool: str, tool_input: dict, command: str, timeout: int,
          tools: str) -> str:
    """Drive one tool call and return the tool_result, truncated.

    `HOME` is the fixture base so that `~` and `$HOME` rows land on a marked file; the
    CLI's own state is in `CLAUDE_CONFIG_DIR`, which is a fresh directory either way, so
    nothing of the operator's is read or written.
    """
    with tempfile.TemporaryDirectory() as scratch:
        out = Path(scratch) / "result.json"
        home = Path(scratch) / "home"
        home.mkdir()
        server = _serve(tool, tool_input, out)
        try:
            host, port = server.server_address[0], server.server_address[1]
            env = {name: os.environ[name] for name in _ENV_ALLOWLIST if name in os.environ}
            env.update(HOME=str(repo.parent.parent.parent),
                       ANTHROPIC_BASE_URL=f"http://{host}:{port}",
                       ANTHROPIC_API_KEY="metforge-tool-measurement",
                       CLAUDE_CONFIG_DIR=str(home))
            try:
                subprocess.run(
                    [command, "--setting-sources", "", "--dangerously-skip-permissions",
                     "--tools", tools, "--output-format", "json", "-p"],
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
    parser.add_argument("--cases", help="JSON file of [[expectation, tool, input], ...]")
    parser.add_argument("--command", default="claude")
    parser.add_argument("--timeout", type=int, default=200)
    parser.add_argument("--keep-fixture", action="store_true",
                        help="Do not delete the fixture tree on exit.")
    args = parser.parse_args(argv)

    cases = json.loads(Path(args.cases).read_text()) if args.cases else DEFAULT_CASES
    # Derived, not hardcoded: the docstring advertises that any tool can be driven, and a
    # fixed `--tools Glob,Grep` made that false — a `Read` case returned "No such tool".
    tools = ",".join(sorted({tool for _expect, tool, _input in cases}))
    repo = build_fixture()
    base = repo.parent.parent.parent
    version = subprocess.run([args.command, "--version"], capture_output=True,
                             text=True).stdout.strip()
    print(f"cli: {version}\nfixture: {repo}\ntools: {tools}\n")
    failures = []
    try:
        for expect, tool, raw_input in cases:
            tool_input = substitute(raw_input, base, repo)
            result = probe(repo, tool, tool_input, args.command, args.timeout, tools)
            got = "ERROR" if launch_failed(result) else (
                "reads" if read_files(result) else "inert")
            ok = got == expect
            if not ok:
                failures.append((expect, tool, tool_input, result))
            label = json.dumps(tool_input).replace(str(base), "{BASE}")
            print(f"[{'ok ' if ok else 'FAIL'}] {expect:5s} {tool:5s} "
                  f"{label[:56]:58s} -> {result}", flush=True)
    finally:
        if not args.keep_fixture:
            shutil.rmtree(base, ignore_errors=True)
    if failures:
        print(f"\nFAIL: {len(failures)} of {len(cases)} rows did not behave as declared.")
        print("An INERT row that READ is the end of issue #71's narrowing premise:\n"
              "`tools/hooks/cli.py`'s pattern check would have to widen again.\n"
              "A CONTROL or absolute row that came back inert means the measurement is\n"
              "broken, not that the tool is confined — fix the harness before believing\n"
              "any row above it.")
        return 1
    print(f"\nPASS: all {len(cases)} rows behaved as declared. Issue #71's narrowing "
          "premise holds on this CLI.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
