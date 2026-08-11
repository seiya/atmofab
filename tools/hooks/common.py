#!/usr/bin/env python3
"""Backend-agnostic hook contracts and validation helpers."""

from __future__ import annotations

from dataclasses import dataclass
import ast
from datetime import datetime, timezone
from enum import Enum
import glob
import json
import os
import re
import shlex
import time
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

# fcntl is POSIX-only.  On Windows we fall through to fail-closed when the
# auto-read seen-set needs an exclusive lock — there is no portable
# equivalent, and Claude Code on Windows has no direct call sites for the
# orchestration auto-read path today.  Guarded so the import does not raise.
try:
    import fcntl as _fcntl  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover — exercised only on non-POSIX
    _fcntl = None  # type: ignore[assignment]

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _lookup_payload_field(payload: dict[str, Any], key: str) -> Any:
    value = payload.get(key)
    if value is not None:
        return value
    inner = payload.get("payload")
    if isinstance(inner, dict):
        return inner.get(key)
    return None


READ_HINT = (
    "Hint: every path inside read_manifests/<agent_run_id>.json allowed_read_roots is "
    "readable directly (Read / Grep / Glob / Bash), as are your own "
    "workspace/orchestrations/<orchestration_id>/output_manifests/<agent_run_id>.json and "
    "read_manifests/<agent_run_id>.json. A path outside allowed_read_roots is rejected by "
    "every route — 'run-gate --gate orchestration_read' terminally fails the orchestration "
    "for it — so re-issue the read against a path under allowed_read_roots instead. "
    "Interpret requirements only from docs/, spec/, and skill_must_read_refs artifacts; "
    "do not derive rules from tools/, validator scripts, or tests. "
    "See docs/RUNBOOK.md#hook-recovery for the full recovery cheatsheet."
)

WRITE_HINT = (
    "Hint: write every artifact (any extension, including managed .json/.txt) "
    "directly with the Edit/Write tool, to a path listed in "
    "output_manifests/<agent_run_id>.json.allowed_file_tool_paths "
    "(guarded-apply-patch is deprecated). The MCP-owned command_log.jsonl is "
    "written only by the build-runtime MCP server and is never file-tool-writable. "
    "For temp files, write directly under the literal allowed_tmp_root path "
    "(workspace/tmp/<agent_run_id>/...); do NOT use `export TMPDIR=...`, "
    "`jq -er ...`, or any bootstrap Bash (Claude Code session sandbox approval "
    "would stall the workflow). See docs/AGENT_CONTRACT.md "
    "for the tmp-area contract."
)

# Repo-relative paths that orchestration agent auto-reads at startup (Claude Code behavior).
# These reads are expected and harmless; silently allow them rather than block.
# Authorization is by exact repo-relative path match (NOT suffix match) to prevent
# absolute-path bypasses like /etc/README.md.
# Scope: orchestration agent only. substep agent has narrower allowed roots and
# must not Read these files.
_AUTO_READ_TOLERATED_REPO_RELPATHS: frozenset[str] = frozenset({
    "MEMORY.md",
    "README.md",
    "TODO.md",
    "CLAUDE.md",
})

# Repo-relative paths and prefixes that the Claude Code harness auto-reads at
# startup regardless of agent role (e.g. MCP discovery, settings parsing).
# These reads happen before any agent prompt runs, so the agent cannot avoid
# them. Allow for ALL agent roles (orchestration + step/substep) when the path
# matches lexically. Authorization rules mirror the orchestration set:
# - exact repo-relative match for the RELPATHS set, OR
# - exact-prefix lexical match for the PREFIXES set, where prefix MUST end with
#   "/" so it cannot extend across path components (no suffix bypass).
_HARNESS_AUTO_READ_TOLERATED_REPO_RELPATHS: frozenset[str] = frozenset({
    ".claude/settings.json",
    # Claude Code's harness auto-reads project config files at startup regardless
    # of the configured backend; `.cursor/mcp.json` is still probed when present in
    # the checkout, so it stays tolerated even though the cursor backend is gone.
    ".cursor/mcp.json",
    "mcp_servers/README.md",
    "mcp_servers/mcp_servers.example.json",
})
_HARNESS_AUTO_READ_TOLERATED_REPO_PREFIXES: frozenset[str] = frozenset({
    "mcp_servers/tools/",
})

# Project-memory file lives outside the repo root under the user's Claude Code state directory.
# We allow it ONLY when the resolved path is inside ~/.claude/projects/ AND ends with
# the canonical "/memory/MEMORY.md" relative tail.
_AUTO_READ_PROJECT_MEMORY_PARENT_TAIL: str = ".claude/projects"
_AUTO_READ_PROJECT_MEMORY_FILE_TAIL: str = "memory/MEMORY.md"

# Claude Code persisted tool-results are written when a tool-result payload exceeds the
# inline size limit.  The payload is saved to:
#   ~/.claude/projects/<repo-slug>/<session-id>/tool-results/<id>.txt
# Agents encounter the `<persisted-output>` wrapper in their context and attempt to Read
# the file to access the full content.  Since these files are written by the Claude Code
# harness (not by the agent), they are never in any agent's read_manifest, causing
# read_manifest_read_guard to fire as a false-positive audit noise entry.
# We quiet-handle these reads for ALL agent roles (not just orchestration), bound to the
# current project's slug to prevent cross-project exfiltration.
_AUTO_READ_PROJECT_TOOL_RESULTS_PARENT_TAIL: str = ".claude/projects"
_AUTO_READ_PROJECT_TOOL_RESULTS_DIR_COMPONENT: str = "tool-results"

MANIFEST_HINT = (
    "Hint: Ensure record-launch generated the manifest for this agent_run_id and that the manifest "
    "JSON structure is valid."
)


def format_block_reason_with_hint(decision: "HookDecision") -> str:
    """Append audit_detail.fix_hint to a BLOCK reason.

    Adapters log audit_detail for forensics, but agents only see the `reason`
    string in the rejection message. Surface the structured fix_hint inline so
    the agent can act on it without consulting the audit log.

    Supported fix_hint fields: next_command (a runnable command), write_under
    (a literal path prefix), docs_ref (doc anchor), note (free text).
    """
    base = decision.reason or "blocked by policy"
    audit = decision.audit_detail or {}
    fix_hint = audit.get("fix_hint") if isinstance(audit, dict) else None
    if not isinstance(fix_hint, dict):
        return base
    next_command = fix_hint.get("next_command")
    write_under = fix_hint.get("write_under")
    docs_ref = fix_hint.get("docs_ref")
    note = fix_hint.get("note")
    appended: list[str] = []
    if isinstance(next_command, str) and next_command.strip():
        appended.append(f"Fix: {next_command.strip()}")
    if isinstance(write_under, str) and write_under.strip():
        appended.append(f"Write under: {write_under.strip()}")
    if isinstance(docs_ref, str) and docs_ref.strip():
        appended.append(f"Docs: {docs_ref.strip()}")
    if isinstance(note, str) and note.strip():
        appended.append(f"Note: {note.strip()}")
    if not appended:
        return base
    return base + "\n\n" + "\n".join(appended)


class HookEventName(str, Enum):
    SESSION_START = "session_start"
    USER_PROMPT_SUBMIT = "user_prompt_submit"
    PRE_COMMAND_EXECUTE = "pre_command_execute"
    PERMISSION_REQUEST = "permission_request"
    POST_COMMAND_EXECUTE = "post_command_execute"
    STOP = "stop"


class HookDecisionAction(str, Enum):
    ALLOW = "allow"
    ALLOW_AUTO_APPROVE = "allow_auto_approve"
    BLOCK = "block"
    CONTINUE_WITH_MESSAGE = "continue_with_message"


@dataclass(frozen=True)
class HookInput:
    event_name: HookEventName
    backend: str
    payload: dict[str, Any]
    command: str | None = None
    prompt: str | None = None
    tool_name: str | None = None
    file_path: str | None = None
    session_id: str | None = None
    agent_session_id: str | None = None


@dataclass(frozen=True)
class HookDecision:
    action: HookDecisionAction
    reason: str | None = None
    additional_context: str | None = None
    continue_processing: bool = True
    audit_detail: dict[str, Any] | None = None


class HookBackendAdapter(Protocol):
    def supported_events(self) -> set[HookEventName]:
        """Return events this adapter can decode/encode."""

    def decode_event(self, event_name: str, payload: dict[str, Any]) -> HookInput:
        """Normalize backend-native event payload to HookInput."""

    def encode_decision(
        self, decision: HookDecision, *, event_name: HookEventName | None = None
    ) -> tuple[int, str]:
        """Return `(exit_code, stdout_text)` for backend hook process protocol."""


def normalize_hook_event_name(event_name: str) -> HookEventName:
    token = event_name.strip()
    mapping = {
        "SessionStart": HookEventName.SESSION_START,
        "UserPromptSubmit": HookEventName.USER_PROMPT_SUBMIT,
        "PreToolUse": HookEventName.PRE_COMMAND_EXECUTE,
        "PermissionRequest": HookEventName.PERMISSION_REQUEST,
        "PostToolUse": HookEventName.POST_COMMAND_EXECUTE,
        "Stop": HookEventName.STOP,
        "session_start": HookEventName.SESSION_START,
        "user_prompt_submit": HookEventName.USER_PROMPT_SUBMIT,
        "pre_command_execute": HookEventName.PRE_COMMAND_EXECUTE,
        "permission_request": HookEventName.PERMISSION_REQUEST,
        "post_command_execute": HookEventName.POST_COMMAND_EXECUTE,
        "stop": HookEventName.STOP,
    }
    if token in mapping:
        return mapping[token]
    raise ValueError(f"unsupported hook event name: {event_name!r}")


def validate_pipeline_semantics_stage(*, step_key: str, args_json: dict[str, Any]) -> str:
    """Validate `validate_pipeline_semantics` stage input for a step capability."""
    allowed_by_step: dict[str, frozenset[str]] = {
        "compile": frozenset({"compile", "full"}),
        "generate": frozenset({"post_generate", "post_build", "full"}),
        "build": frozenset({"post_build", "full"}),
        "validate": frozenset({"post_execute", "pre_judge", "full"}),
    }
    stage = args_json.get("stage") or args_json.get("--stage")
    if not isinstance(stage, str) or not stage.strip():
        raise ValueError(
            "pre_command_execute hook: validate_pipeline_semantics requires args_json.stage "
            "(or --stage) as non-empty string"
        )
    stage_l = stage.strip().lower()
    allowed = allowed_by_step.get(step_key)
    if allowed is not None and stage_l not in allowed:
        raise ValueError(
            "pre_command_execute hook: validate_pipeline_semantics "
            f"--stage {stage_l!r} not permitted for capability step={step_key!r} "
            f"(allowed={sorted(allowed)})"
        )

    if stage_l == "pre_judge":
        for key, val in args_json.items():
            key_s = str(key).lower().replace("_", "-")
            if "allow-missing-orchestration" in key_s or "allow-missing-llm-review" in key_s:
                if val is True or val == 1:
                    raise ValueError(
                        "pre_command_execute hook: pre_judge forbids allow-missing-orchestration "
                        "and allow-missing-llm-review"
                    )
                if isinstance(val, str) and val.strip().lower() in {"true", "1", "yes"}:
                    raise ValueError(
                        "pre_command_execute hook: pre_judge forbids allow-missing-orchestration "
                        "and allow-missing-llm-review"
                    )
    return stage_l


def _extract_command(payload: dict[str, Any]) -> str | None:
    command = payload.get("command")
    if isinstance(command, str) and command.strip():
        return command.strip()
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        command = tool_input.get("command")
        if isinstance(command, str) and command.strip():
            return command.strip()
    return None


def _strip_quoted_strings(cmd: str) -> str:
    """Blank the content inside shell quotes AND comments, preserving length.

    Callers rely on this to keep a `>`, `;` or `<<` that is merely TEXT inside
    an argument from being read as shell syntax, and on the offsets staying
    aligned with the original string.

    One left-to-right scan, honouring whichever quote opens first — the two
    independent regex passes this replaced paired a `"` living inside a
    single-quoted word with the next unrelated `"`, blanking the separators and
    commands between them. `echo 'a"b' ; cat <path> ; echo "c"` then collapsed
    into a single fragment whose argv0 was not a reader, so the read vanished
    from the guard and the command reached the read-only auto-approve.

    An unterminated quote is left alone rather than blanking to end-of-string,
    which is the fail-closed direction here: nothing is hidden from the scan.

    A comment is blanked here rather than later because bash's lexer decides it
    HERE: a `#` comment ends the line before quoting or `<<` mean anything. Doing
    it afterwards let an apostrophe in a comment ("user's file") pair with a
    later quote and blank the newline between them — merging fragments so a read
    on the next line vanished — and let a `<<` written inside a comment blank the
    rest of the command as a heredoc body. The `#` itself is kept: callers scan
    the result for it to refuse auto-approval.
    """
    out = list(cmd)
    idx = 0
    n = len(cmd)
    at_word_start = True
    while idx < n:
        ch = cmd[idx]
        if ch == "#" and at_word_start:
            end = cmd.find("\n", idx)
            end = n if end == -1 else end
            for pos in range(idx + 1, end):
                out[pos] = " "
            idx = end
            at_word_start = True
            continue
        if ch == "\\":  # an escaped character never opens a quote
            idx += 2
            at_word_start = False
            continue
        if ch not in ("'", '"'):
            # NB: `)` does NOT start a word — `$(echo A)#x` is not a comment.
            at_word_start = ch in " \t\n;|&("
            idx += 1
            continue
        at_word_start = False
        end = idx + 1
        while end < n:
            # Backslash escapes apply inside "..." but not inside '...'.
            if ch == '"' and cmd[end] == "\\":
                end += 2
                continue
            if cmd[end] == ch:
                break
            end += 1
        if end >= n:  # unterminated: leave the remainder visible
            idx += 1
            continue
        for pos in range(idx + 1, end):
            out[pos] = " "
        idx = end + 1
    return "".join(out)


# Flags whose VALUE is a detached following token that must never be mistaken
# for a read target (`head -n 5 f` would otherwise extract "5").  Keyed by
# command basename; only commands in _BASH_READ_CMD_NAMES are consulted.
# `sort -o FILE` is a WRITE target, so it is listed here too — consuming the
# operand keeps it out of the read-target list, which is all this table decides.
_DETACHED_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "head": frozenset({"-n", "-c"}),
    "tail": frozenset({"-n", "-c"}),
    "cut": frozenset({"-b", "-c", "-d", "-f"}),
    "paste": frozenset({"-d"}),
    # `od -w[BYTES]` and `xxd -b` take no separate operand — listing them here
    # made the filename their "value".
    "od": frozenset({"-A", "-j", "-N", "-S", "-t"}),
    "xxd": frozenset({"-c", "-g", "-l", "-s", "-o"}),
    "strings": frozenset({"-n", "-t"}),
    "sort": frozenset({"-k", "-t", "-S", "-T", "-o"}),
    "uniq": frozenset({"-f", "-s", "-w"}),
    "comm": frozenset(),
    "diff": frozenset(),
    "nl": frozenset({"-b", "-d", "-f", "-h", "-i", "-l", "-n", "-s", "-v", "-w"}),
    "tac": frozenset({"-s"}),
    # grep/rg/awk have their own grammars below (first positional = pattern /
    # program), but they share this table: an unconsumed detached value takes
    # the pattern's positional slot and PROMOTES the real pattern to a file
    # operand — `grep -C 2 workspace docs/a.md` would report "workspace" as a
    # read and block a legitimate search.
    # ONLY flags whose value is REQUIRED and space-separated. An optional-value
    # flag (`--color[=WHEN]`) or a no-value flag listed here consumes the search
    # PATTERN, which empties the operand list and drops the file entirely —
    # the exact inverse of what this table is for, and it regressed
    # forbid_tools_direct_read. GNU long options here need `=`, so they are not
    # separate tokens and must not be listed.
    # Long forms are listed only when the value is REQUIRED — GNU getopt then
    # accepts the space-separated spelling too. An OPTIONAL-value long option
    # (`--color[=WHEN]`, `--group-separator[=SEP]`) only ever takes `=`, so
    # listing it would eat the search pattern instead.
    "grep": frozenset({
        "-A", "-B", "-C", "-m", "-d", "-D",
        "--max-count", "--after-context", "--before-context", "--context",
        "--directories", "--devices", "--binary-files", "--label",
        "--include", "--exclude", "--exclude-dir", "--exclude-from",
    }),
    "wc": frozenset(),
    "egrep": frozenset({
        "-A", "-B", "-C", "-m", "-d", "-D",
        "--max-count", "--after-context", "--before-context", "--context",
        "--directories", "--devices", "--binary-files", "--label",
        "--include", "--exclude", "--exclude-dir", "--exclude-from",
    }),
    "fgrep": frozenset({
        "-A", "-B", "-C", "-m", "-d", "-D",
        "--max-count", "--after-context", "--before-context", "--context",
        "--directories", "--devices", "--binary-files", "--label",
        "--include", "--exclude", "--exclude-dir", "--exclude-from",
    }),
    "rg": frozenset({
        "-A", "-B", "-C", "-m", "-t", "-T", "-g", "-j", "-M", "-d",
        "--max-count", "--after-context", "--before-context", "--context",
        "--type", "--type-not", "--glob", "--iglob", "--threads", "--max-columns",
        "--max-depth", "--color", "--colors", "--sort", "--sortr", "--engine",
        "--context-separator", "--field-match-separator",
    }),
    "awk": frozenset({"-v", "--assign", "-F", "--field-separator"}),
}

# Leading tokens that are shell syntax rather than a command name.  Without
# this, `if true; then cat X; fi` splits into a fragment whose argv0 is `then`,
# which is not a reader — and the read of X vanishes from the guard entirely.
# That is a plain literal target, NOT the declared unprovable residue.
_BASH_LEADING_SYNTAX_TOKENS: frozenset[str] = frozenset({
    "if", "then", "elif", "else", "fi", "while", "until", "do", "done",
    "case", "esac", "select", "function", "time", "!", "{", "}", "(", ")",
})

# Output redirections (`> f`, `2>> f`, `&> f`, `>& f`) — their operand is a
# WRITE target and must never be reported as a read.  `<` is deliberately not
# here: its operand really is read.
_BASH_REDIRECT_OUT_EXACT_RE = re.compile(r"^\d*(?:>>|>&|&>|>)$")
_BASH_REDIRECT_OUT_GLUED_RE = re.compile(r"^\d*(?:>>|>&|&>|>).+$")

# `<<[-]DELIM` / `<<[-]'DELIM'` — the body that follows is DATA, not commands.
# `(?<!<)` excludes a `<<<` here-string, whose operand is a word on the SAME
# line and which has no body to blank. A quoted delimiter may be any word
# (`'PY-END'`, `'1EOF'`), so the charset is only constrained for the bare form.
_BASH_HEREDOC_RE = re.compile(
    r"(?<!<)<<(?P<dash>-?)\s*"
    r"(?:'(?P<sq>[^']*)'|\"(?P<dq>[^\"]*)\"|\\?(?P<bare>[A-Za-z_][A-Za-z0-9_]*))"
)


def _blank_heredoc_bodies(command: str) -> str:
    """Replace heredoc bodies with spaces, preserving length.

    A heredoc body is a document the agent is WRITING, not a command it is
    running: `cat > note.md <<EOF` followed by a line reading
    `diff a.md b.md` performs no read of a.md.  Leaves are explicitly told to
    write scratch files this way, so scanning the body reports reads that do
    not happen.  Length is preserved so fragment spans stay aligned with the
    original string.
    """
    # Locate the operator in the QUOTE-STRIPPED string: `grep -n "cout << endl"`
    # is not a heredoc, and treating it as one blanked every following line —
    # deleting real read targets. _strip_quoted_strings is length-preserving, so
    # the offsets apply to the original unchanged.
    out = list(command)
    search_from = 0
    # Where the NEXT body starts. A line may declare several heredocs
    # (`cat <<A <<B`); their bodies follow in order, so B's body begins where
    # A's ended — not after B's own line. Advancing past a body to find the
    # next operator would skip `<<B` entirely, because it sits EARLIER in the
    # string than A's terminator, and B's body would then be parsed as
    # commands.
    body_cursor = 0
    while True:
        # Recomputed each pass: quote pairing must not run THROUGH a body we
        # have already blanked. An apostrophe in one heredoc's prose ("don't")
        # otherwise pairs with the opening quote of the next heredoc's
        # delimiter, hiding that `<<` and leaving its body to be read as
        # commands — a false read of whatever the document happens to mention.
        scanned = _strip_quoted_strings("".join(out))
        match = _BASH_HEREDOC_RE.search(scanned, search_from)
        if match is None:
            return "".join(out)
        # A quoted delimiter's own text was blanked in `scanned`; read it back
        # from the original at the same offsets.
        delimiter = (
            command[match.start("sq") : match.end("sq")]
            if match.group("sq") is not None
            else command[match.start("dq") : match.end("dq")]
            if match.group("dq") is not None
            else match.group("bare")
        ).strip()
        if not delimiter:
            search_from = match.end()
            continue
        # `$((1 << n))` and `(( x = y << z ))` are arithmetic, not heredocs.
        # A real heredoc operator is followed by end-of-line or another
        # redirection — never by the `)` that closes an arithmetic context.
        tail = scanned[match.end() :].lstrip(" \t")
        if tail[:1] == ")":
            search_from = match.end()
            continue
        newline = command.find("\n", match.end())
        if newline == -1:
            return "".join(out)
        # First operator on this line: its body starts on the next line.
        # A later operator on the SAME line continues after the previous body.
        if body_cursor <= newline:
            body_cursor = newline + 1
        # Bash ends the body on a line that is EXACTLY the delimiter; `<<-`
        # strips leading TABS only. Accepting any indentation ended the body
        # early on a document that merely contains an indented `EOF`, leaving
        # the rest of the prose to be parsed as commands.
        dash = bool(match.group("dash"))
        idx = body_cursor
        while idx <= len(command):
            line_end = command.find("\n", idx)
            stop = len(command) if line_end == -1 else line_end
            line = command[idx:stop]
            is_delimiter = (line.lstrip("\t") if dash else line) == delimiter
            for pos in range(idx, stop):
                out[pos] = " "
            if is_delimiter or line_end == -1:
                break
            idx = line_end + 1
        # The next body starts after this terminator line; the next OPERATOR
        # may still be on the original command line, so resume the search just
        # past this one rather than past the body.
        body_cursor = (len(command) if line_end == -1 else line_end) + 1
        search_from = match.end()

# Bash commands whose positional operands are file reads.  Everything here is
# routed through _extract_read_targets, which owns the per-command grammar.
_BASH_READ_CMD_NAMES: frozenset[str] = frozenset({
    # historical set (also used by forbid_tools_direct_read)
    "cat", "head", "tail", "less", "more", "bat", "pygmentize", "sed", "rg", "grep", "awk",
    # widened for the read-manifest guard
    "nl", "tac", "od", "xxd", "cut", "paste", "diff", "strings", "comm", "sort", "uniq", "jq",
    # egrep/fgrep are in _SAFE_READONLY_BASH_CMDS (cli.py) — auto-approvable, so
    # leaving them out of THIS set let `egrep PAT <any file>` execute unvalidated.
    # `wc` likewise reads whole file contents.
    "egrep", "fgrep", "wc",
})

# grep-family recursive flags: with one of these and no file operand, the
# search walks the working directory — including from a pipe tail, where stdin
# is ignored (verified: `echo x | grep -r hi` searches the cwd).
_GREP_RECURSIVE_LONG_FLAGS: frozenset[str] = frozenset({
    "--recursive", "-r", "-R", "--dereference-recursive",
})

# Shell separators that end one command fragment.  `&&`/`||` must precede the
# single-character forms in the alternation.
_BASH_FRAGMENT_SEPARATOR_RE = re.compile(r"\|\||&&|;|&|\||\n")

# fd-duplication redirects (`2>&1`, `>&2`). Their `&` is NOT a separator: it
# split `cat 2>&1 file` into a reader with no operand plus an operand with no
# reader, so the read vanished. The RHS digits must be the whole token — bash
# treats `n>&word` as a dup only when `word` is all digits.
_BASH_FD_DUP_RE = re.compile(r"\d*>&\d+(?![\w./-])")
# A backslash-escaped separator is a literal character, not a separator.
_BASH_ESCAPED_SEPARATOR_RE = re.compile(r"\\[&|;]")

# ANSI-C (`$'…'`) and locale (`$"…"`) quoting at a word start. Both are purely
# LEXICAL — bash reads the literal inside — but shlex turns them into a bare
# `$word`, indistinguishable from a `$VAR` expansion, so the residue filter
# dropped them and `cat $'secret/s.md'` reached the auto-approve. Stripping
# the `$` before tokenizing keeps the distinction.
_ANSI_C_QUOTE_PREFIX_RE = re.compile(r"(?<![\w$])\$(?=['\"])")

# Leading `VAR=value` command prefix (`FOO=1 cat x`).
_BASH_ASSIGNMENT_PREFIX_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


# Short options whose VALUE is glued to the rest of the cluster (`-eerror`,
# `-m5`), so any letter after them belongs to that value, not to the cluster.
_GREP_VALUE_TAKING_SHORT_LETTERS: frozenset[str] = frozenset("efmdDABC")

# Modes that make a grep-family call read nothing, or that take no pattern
# operand at all — synthesizing a "." read for these blocks a command the agent
# cannot rephrase, which is a retry loop rather than a recoverable block.
# Per command, because the spellings collide: `-h` is `--help` for ripgrep but
# `--no-filename` for the grep family, so sharing one set made `grep -r -h PAT`
# look like a help invocation and let a whole-checkout recursive read through.
_GREP_NO_READ_FLAGS: dict[str, frozenset[str]] = {
    "grep": frozenset({"--version", "-V", "--help"}),
    "egrep": frozenset({"--version", "-V", "--help"}),
    "fgrep": frozenset({"--version", "-V", "--help"}),
    "rg": frozenset({"--version", "-V", "--help", "-h"}),
}
_RG_NO_PATTERN_FLAGS: frozenset[str] = frozenset({
    "--files", "--type-list", "--pcre2-version",
})


# sed's value-taking short options. `-i[SUFFIX]` takes only a GLUED suffix, so
# it ends a cluster without consuming the next token; the rest take a value
# either glued or detached.
_SED_VALUE_TAKING_SHORT_LETTERS: frozenset[str] = frozenset("efl")
_SED_GLUED_ONLY_SHORT_LETTERS: frozenset[str] = frozenset("i")


def _sed_short_cluster(token: str) -> tuple[str, str] | None:
    """Split a sed short cluster at its first value-taking letter.

    GNU sed clusters like any getopt program, so `-nf FILE` is `-n -f FILE` and
    opens FILE. Recognizing `-f` only as a whole token or a `-f`-prefixed one
    left `-nf` / `-nfFILE` to the generic flag skip, and the script file — a
    real read of an out-of-manifest path — disappeared.
    """
    if not token.startswith("-") or token.startswith("--") or len(token) < 3:
        return None
    for pos, letter in enumerate(token[1:], start=1):
        if letter in _SED_VALUE_TAKING_SHORT_LETTERS:
            return letter, token[pos + 1 :]
        if letter in _SED_GLUED_ONLY_SHORT_LETTERS:
            return None  # `-i.bak`: the remainder belongs to -i, not to a flag
        if not letter.isalpha():
            return None
    return None


def _grep_short_cluster(cmd: str, token: str) -> tuple[str, str] | None:
    """Split a grep-family short cluster at its first value-taking letter.

    Returns `(letter, glued_value)` — `glued_value` empty when the value is the
    next token — or None when the token is not such a cluster.

    `-e`/`-f` were recognized only at the START of a token, and the generic
    cluster rule derived its letters from the detached table, which excludes
    them. So in `-ieTOP` neither half saw the `e`: the pattern was promoted to
    the "first positional is the pattern" slot and the FILE was consumed as the
    pattern instead — the read vanished and was auto-approved. `-Ff pats.txt`
    lost the pattern file the same way.
    """
    if cmd not in {"grep", "egrep", "fgrep", "rg"}:
        return None
    if not token.startswith("-") or token.startswith("--") or len(token) < 3:
        return None
    # ripgrep's value-taking short options differ from GNU grep's, and its
    # idiomatic glued form (`-tmd` is `-t md`) means the remainder belongs to
    # the flag — but a cluster ENDING in one (`-nf FILE`) still takes the next
    # token, which is how the pattern-file read was lost.
    value_letters = set("efgjmtT") if cmd == "rg" else _GREP_VALUE_TAKING_SHORT_LETTERS
    for pos, letter in enumerate(token[1:], start=1):
        if letter in value_letters:
            # Only the FLAG letters must be alphabetic; the glued value can be
            # anything. Requiring the whole token to be letters meant any real
            # pattern — `-ie2024`, `-ieTOP_X`, `-Ffspec/pats.txt` — fell through
            # to the generic flag skip, and the file was eaten as the pattern.
            return letter, token[pos + 1 :]
        if not letter.isalpha():
            return None
    return None


def _grep_directories_recurse(args: list[str]) -> bool:
    """`grep -d recurse` / `--directories=recurse` is a spelling of `-r`."""
    for idx, token in enumerate(args):
        if token in {"-d", "--directories"} and idx + 1 < len(args):
            if args[idx + 1] == "recurse":
                return True
        if token.startswith("--directories=") and token.split("=", 1)[1] == "recurse":
            return True
        if token.startswith("-d") and len(token) > 2 and token[2:] == "recurse":
            return True
    return False


def _searches_working_directory(
    cmd: str, args: list[str], *, stdin_from_pipe: bool
) -> bool:
    """Whether a grep-family call with no file operand still walks the tree."""
    no_read_flags = _GREP_NO_READ_FLAGS.get(cmd, frozenset())
    if any(token in no_read_flags for token in args):
        return False
    if cmd == "rg":
        # ripgrep is recursive by default, but a pipe tail reads stdin instead.
        return not stdin_from_pipe
    if _grep_directories_recurse(args):
        return True
    for token in args:
        if token in _GREP_RECURSIVE_LONG_FLAGS:
            return True
        # Short-flag clusters: `-rn`, `-Rl`. `--foo` is never a cluster, and a
        # value-taking letter ends the cluster (`-eerror` is `-e error`, not a
        # cluster containing `-r`).
        if token.startswith("-") and not token.startswith("--"):
            for letter in token[1:]:
                if letter in {"r", "R"}:
                    return True
                if letter in _GREP_VALUE_TAKING_SHORT_LETTERS:
                    break
    return False


def _extract_simple_positional_read_targets(cmd: str, args: list[str]) -> list[str]:
    """Positional operands of a command whose args are all input files.

    Flags are dropped, detached flag values are consumed, and `--` ends option
    parsing.  Used for the plain readers (cat, nl, od, cut, sort, ...) where
    every remaining operand names a file to read.
    """
    detached = _DETACHED_VALUE_FLAGS.get(cmd, frozenset())
    targets: list[str] = []
    idx = 0
    while idx < len(args):
        token = args[idx]
        if token == "--":
            targets.extend(args[idx + 1 :])
            break
        read_target, consumed = _long_option_read_target(cmd, token, args, idx)
        if consumed:
            if read_target:
                targets.append(read_target)
            idx += consumed
            continue
        if token in detached:
            idx += 2
            continue
        if token.startswith("-") and token != "-":
            idx += 1
            continue
        targets.append(token)
        idx += 1
    return targets


# Long options whose VALUE is a file the command opens. These are not flags to
# skip and not detached values to swallow — they are reads, and several of them
# echo the file's content back (`wc --files0-from` and `sort --files0-from`
# print it in their diagnostics, `diff --from-file` prints it as a diff). Both
# spellings count: `--opt=PATH` and `--opt PATH`.
#
# Enumerated from each command's `--help` in one pass, deliberately: the same
# defect kept arriving one option at a time (`--from-file`, `--exclude-from`,
# `--files0-from`), and a table is the only form that closes the class.
_LONG_OPTION_READ_TARGETS: dict[str, frozenset[str]] = {
    "wc": frozenset({"--files0-from"}),
    "sort": frozenset({"--files0-from", "--random-source"}),
    "diff": frozenset({"--from-file", "--to-file", "--exclude-from", "--starting-file"}),
    "grep": frozenset({"--exclude-from", "--file"}),
    "egrep": frozenset({"--exclude-from", "--file"}),
    "fgrep": frozenset({"--exclude-from", "--file"}),
    "sed": frozenset({"--file"}),
    # ripgrep opens --ignore-file and echoes any line that is not a valid glob.
    "rg": frozenset({"--file", "--ignore-file"}),
    "awk": frozenset({"--file"}),
    "jq": frozenset({"--from-file", "--slurpfile", "--rawfile"}),
    "comm": frozenset(),
    "uniq": frozenset(),
}


def _long_option_read_target(
    cmd: str, token: str, args: list[str], idx: int
) -> tuple[str | None, int]:
    """`(target, tokens_consumed)` when `token` names a file the command reads."""
    if not token.startswith("--") or token == "--":
        return None, 0
    name, _, glued = token[2:].partition("=")
    if f"--{name}" not in _LONG_OPTION_READ_TARGETS.get(cmd, frozenset()):
        return None, 0
    if "=" in token:
        return (glued or None), 1
    return (args[idx + 1] if idx + 1 < len(args) else None), 2


def _is_long_option_abbreviation(token: str, names: tuple[str, ...]) -> bool:
    """Whether `token` is an abbreviated spelling of one of `names`.

    GNU getopt_long accepts any unambiguous prefix, so `--regex=PAT` and
    `--reg=PAT` are `--regexp=PAT`. Only the options that supply a PATTERN or
    SCRIPT matter here: mistaking one for an ordinary flag makes the grammar
    consume the file operand in its place. Full spellings are handled by their
    own branches; this covers the shortened ones.
    """
    if not token.startswith("--") or token == "--":
        return False
    name = token[2:].split("=", 1)[0]
    if not name:
        return False
    return any(full.startswith(name) and full != name for full in names)


def _extract_diff_read_targets(args: list[str]) -> list[str]:
    """Read targets of a `diff` invocation.

    `--from-file=PATH` / `--to-file=PATH` name files diff reads AND PRINTS, but
    they look like any other long option, so the generic flag skip dropped them
    silently — and `diff` is auto-approvable, so the read was granted.
    """
    targets: list[str] = []
    rest: list[str] = []
    idx = 0
    while idx < len(args):
        token = args[idx]
        if token == "--":
            rest.extend(args[idx + 1 :])
            break
        read_target, consumed = _long_option_read_target("diff", token, args, idx)
        if consumed:
            if read_target:
                targets.append(read_target)
            idx += consumed
            continue
        if token == "-X":  # --exclude-from's short form
            if idx + 1 < len(args):
                targets.append(args[idx + 1])
            idx += 2
            continue
        if token.startswith("-X") and len(token) > 2:
            targets.append(token[2:])  # glued: `-Xfile`
            idx += 1
            continue
        if token.startswith("-") and token != "-":
            idx += 1
            continue
        rest.append(token)
        idx += 1
    return targets + rest


def _extract_jq_read_targets(args: list[str]) -> list[str]:
    """Read targets of a `jq` invocation.

    The first positional is the FILTER program, not a file — the orchestration
    agent routinely runs `jq -er <filter> workspace/...`, so treating operand 0
    as a read would flood the guard with phantom targets.  `-f/--from-file` and
    the `--slurpfile`/`--rawfile NAME FILE` pairs do name real files.
    """
    targets: list[str] = []
    positional: list[str] = []
    idx = 0
    filter_from_file = False
    while idx < len(args):
        token = args[idx]
        if token == "--":
            positional.extend(args[idx + 1 :])
            break
        if re.fullmatch(r"-[A-Za-z]*f[A-Za-z]*", token) and token != "-f":
            # jq clusters (`-rf prog.jq`), and `-f`'s value is always the next
            # token — jq rejects the glued `-fprog.jq` itself. Unrecognized, the
            # program file landed in the filter slot and was discarded.
            if idx + 1 < len(args):
                targets.append(args[idx + 1])
            filter_from_file = True
            idx += 2
            continue
        if token in {"-f", "--from-file"}:
            if idx + 1 < len(args):
                targets.append(args[idx + 1])
            filter_from_file = True
            idx += 2
            continue
        if token.startswith("--from-file="):
            value = token.split("=", 1)[1]
            if value:
                targets.append(value)
            filter_from_file = True
            idx += 1
            continue
        if token in {"--slurpfile", "--rawfile"}:
            # `--slurpfile NAME FILE`: the second operand is the file.
            if idx + 2 < len(args):
                targets.append(args[idx + 2])
            idx += 3
            continue
        if token in {"--arg", "--argjson"}:
            idx += 3
            continue
        if token in {"--indent", "--seq-separator"}:
            idx += 2
            continue
        if token.startswith("-") and token != "-":
            idx += 1
            continue
        positional.append(token)
        idx += 1
    if filter_from_file:
        return targets + positional
    return targets + positional[1:]


def _extract_read_targets(
    cmd_name: str, cmd_tokens: list[str], *, stdin_from_pipe: bool = False
) -> list[str]:
    args = cmd_tokens[1:]
    cmd = cmd_name.lower()
    if not args:
        # `rg PAT` with no operand walks the working directory, but only when
        # it is not consuming a pipe (verified: `echo x | rg hi` reads stdin).
        if cmd == "rg" and not stdin_from_pipe:
            return ["."]
        return []

    if cmd == "jq":
        return _extract_jq_read_targets(args)

    if cmd == "diff":
        return _extract_diff_read_targets(args)

    if cmd in {
        "cat", "head", "tail", "less", "more", "bat", "pygmentize",
        "nl", "tac", "od", "xxd", "cut", "paste", "strings",
        "comm", "sort", "uniq", "wc",
    }:
        return _extract_simple_positional_read_targets(cmd, args)

    if cmd == "sed":
        positional: list[str] = []
        read_targets: list[str] = []
        has_explicit_script_source = False
        explicit_script_after_positional = False
        idx = 0
        while idx < len(args):
            token = args[idx]
            if token == "--":
                positional.extend(args[idx + 1 :])
                break
            if token.startswith("--") and "=" in token:
                key, value = token.split("=", 1)
                if key == "--file" and value:
                    if positional:
                        explicit_script_after_positional = True
                    read_targets.append(value)
                    has_explicit_script_source = True
                    idx += 1
                    continue
                if key == "--expression":
                    if positional:
                        explicit_script_after_positional = True
                    has_explicit_script_source = True
                    idx += 1
                    continue
            if token in {"-e", "-f"}:
                if positional:
                    explicit_script_after_positional = True
                has_explicit_script_source = True
                if token == "-f" and idx + 1 < len(args):
                    read_targets.append(args[idx + 1])
                idx += 2
                continue
            sed_cluster = _sed_short_cluster(token)
            if sed_cluster is not None:
                letter, glued = sed_cluster
                if letter in {"e", "f"}:
                    if positional:
                        explicit_script_after_positional = True
                    has_explicit_script_source = True
                    if letter == "f":
                        if glued:
                            read_targets.append(glued)
                        elif idx + 1 < len(args):
                            read_targets.append(args[idx + 1])
                idx += 1 if glued else 2
                continue
            if token.startswith("-e") and token != "-e":
                if positional:
                    explicit_script_after_positional = True
                has_explicit_script_source = True
                idx += 1
                continue
            if token.startswith("-f") and token != "-f":
                if positional:
                    explicit_script_after_positional = True
                has_explicit_script_source = True
                read_targets.append(token[2:])
                idx += 1
                continue
            read_target, consumed = _long_option_read_target(cmd, token, args, idx)
            if consumed:
                if positional:
                    explicit_script_after_positional = True
                has_explicit_script_source = True
                if read_target:
                    read_targets.append(read_target)
                idx += consumed
                continue
            if _is_long_option_abbreviation(token, ("file",)) and "=" in token:
                # `--fil=PATH` is `--file=PATH`: a script file sed reads.
                if positional:
                    explicit_script_after_positional = True
                has_explicit_script_source = True
                value = token.split("=", 1)[1]
                if value:
                    read_targets.append(value)
                idx += 1
                continue
            if _is_long_option_abbreviation(token, ("expression", "file")):
                # `--expr=p` supplies the script just as `--expression=p` does;
                # treating it as an ordinary flag consumed the FILE as the
                # script.
                if positional:
                    explicit_script_after_positional = True
                has_explicit_script_source = True
                idx += 1
                continue
            if token.startswith("-"):
                idx += 1
                continue
            positional.append(token)
            idx += 1
        if has_explicit_script_source:
            if explicit_script_after_positional and positional:
                return read_targets + positional[1:]
            return read_targets + positional
        if len(positional) <= 1:
            return read_targets
        return read_targets + positional[1:]

    if cmd in {"rg", "grep", "egrep", "fgrep"}:
        positional: list[str] = []
        idx = 0
        has_explicit_pattern = False
        unrecognized_long_option = False
        read_targets: list[str] = []
        detached = _DETACHED_VALUE_FLAGS.get(cmd, frozenset())
        while idx < len(args):
            token = args[idx]
            if token == "--":
                positional.extend(args[idx + 1 :])
                break
            long_read, long_consumed = _long_option_read_target(cmd, token, args, idx)
            if long_consumed and token.split("=", 1)[0] != "--file":
                # A file this reader OPENS (`--exclude-from`, rg's
                # `--ignore-file`). `--file` is excluded here because it also
                # supplies the PATTERN, which the branch below must record.
                if long_read:
                    read_targets.append(long_read)
                idx += long_consumed
                continue
            if token in detached:
                idx += 2
                continue
            cluster = _grep_short_cluster(cmd, token)
            if cluster is not None:
                letter, glued = cluster
                if letter in {"e", "f"}:
                    has_explicit_pattern = True
                    if letter == "f":
                        if glued:
                            read_targets.append(glued)
                        elif idx + 1 < len(args):
                            read_targets.append(args[idx + 1])
                idx += 1 if glued else 2
                continue
            if token.startswith("--") and "=" in token:
                key, value = token.split("=", 1)
                if key in {"--file", "--regexp"}:
                    has_explicit_pattern = True
                    if key == "--file" and value:
                        read_targets.append(value)
                    idx += 1
                    continue
            if token in {"-e", "-f", "--regexp", "--file"}:
                has_explicit_pattern = True
                if token in {"-f", "--file"} and idx + 1 < len(args):
                    read_targets.append(args[idx + 1])
                idx += 2
                continue
            if token.startswith("-e") and token != "-e":
                has_explicit_pattern = True
                idx += 1
                continue
            if token.startswith("-f") and token != "-f":
                has_explicit_pattern = True
                read_targets.append(token[2:])
                idx += 1
                continue
            if _is_long_option_abbreviation(token, ("regexp", "file")):
                # GNU getopt_long accepts any unambiguous abbreviation, so
                # `--regex=PAT` / `--reg=PAT` really do supply the pattern —
                # and treating them as ordinary flags consumed the FILE as the
                # pattern and auto-approved the read.
                unrecognized_long_option = True
                idx += 1
                continue
            if token.startswith("-"):
                idx += 1
                continue
            positional.append(token)
            idx += 1
        # `rg --files docs` (and friends) take NO pattern operand, so the first
        # positional is already a path. Consuming it as a pattern emptied the
        # operand list and substituted "." — blocking on the repo root for a
        # command that named a directory the manifest allows.
        takes_no_pattern = cmd == "rg" and any(
            token in _RG_NO_PATTERN_FLAGS for token in args
        )
        if has_explicit_pattern or takes_no_pattern or unrecognized_long_option:
            file_operands = positional
        else:
            file_operands = positional[1:]
        if not file_operands and _searches_working_directory(
            cmd, args, stdin_from_pipe=stdin_from_pipe
        ):
            # No file operand, yet the search still walks the tree: `grep -rn PAT`
            # reads the whole checkout. This is the same read a pathless Grep
            # tool call makes, and that one blocks — so name the same target.
            file_operands = ["."]
        return read_targets + file_operands

    if cmd == "awk":
        positional: list[str] = []
        idx = 0
        read_targets: list[str] = []
        has_program_file = False
        detached = _DETACHED_VALUE_FLAGS.get(cmd, frozenset())
        while idx < len(args):
            token = args[idx]
            if token == "--":
                positional.extend(args[idx + 1 :])
                break
            if token in detached:
                idx += 2
                continue
            if token.startswith("--file="):
                value = token.split("=", 1)[1]
                if value:
                    read_targets.append(value)
                    has_program_file = True
                idx += 1
                continue
            if token in {"-f", "--file"}:
                if idx + 1 < len(args):
                    read_targets.append(args[idx + 1])
                has_program_file = True
                idx += 2
                continue
            if token.startswith("-f") and token != "-f":
                read_targets.append(token[2:])
                has_program_file = True
                idx += 1
                continue
            if token.startswith("-"):
                idx += 1
                continue
            positional.append(token)
            idx += 1
        if not positional:
            return read_targets
        if has_program_file:
            return read_targets + positional
        return read_targets + positional[1:]

    return []


def _bash_comment_start(span_text: str) -> int | None:
    """Index of the `#` that starts a comment, or None.

    Bash treats `#` as a comment only at the start of a WORD, so `a#b` and
    `--color=#fff` are not comments. Callers pass the quote-stripped span, where
    a `#` inside quotes has already been blanked.
    """
    for pos, char in enumerate(span_text):
        if char != "#":
            continue
        if pos == 0 or span_text[pos - 1] in " \t\n":
            return pos
    return None


def _strip_bash_fragment_syntax(
    tokens: list[str],
) -> tuple[list[str], list[str], bool]:
    """Split a fragment into (argv tokens, input-redirect reads, stdin-redirected).

    Handles the shapes the separator split cannot: a leading keyword or grouping
    token (`then cat X`, `{ cat X`, `(cat X)`), and redirections — an output
    redirection's operand is a write target rather than a read, while an input
    redirection's operand is a read no matter what the command is.
    """
    out: list[str] = []
    idx = 0
    # Leading `VAR=value` prefixes and shell syntax, in any order.
    while idx < len(tokens):
        token = tokens[idx]
        if _BASH_ASSIGNMENT_PREFIX_RE.match(token) or token in _BASH_LEADING_SYNTAX_TOKENS:
            idx += 1
            continue
        break
    # A grouping character glued to the command name (`(cat`); when we strip
    # one, the fragment's closing `)`/`}` is glued to its last token.
    stripped_leading_group = False
    if idx < len(tokens) and tokens[idx][:1] in {"(", "{"}:
        tokens = list(tokens)
        tokens[idx] = tokens[idx].lstrip("({")
        stripped_leading_group = True
        if not tokens[idx]:
            idx += 1
    redirect_reads: list[str] = []
    stdin_redirected = False
    while idx < len(tokens):
        token = tokens[idx]
        # A redirection may carry a file-descriptor number: `0<f` is `<f`, and
        # `0<<EOF` is a heredoc. Strip the digits before classifying, or a
        # literal path arrives as the ordinary operand `0<f`, fails the
        # existence check, and the read is never validated.
        bare = token.lstrip("0123456789") if token[:1].isdigit() else token
        if bare.startswith("<<<"):  # here-string: the operand is literal text
            stdin_redirected = True
            # `<<<hi` carries its own operand; `<<< hi` takes the next token.
            idx += 1 if len(bare) > 3 else 2
            continue
        if bare.startswith("<<"):  # heredoc: the operand is a delimiter word
            stdin_redirected = True
            idx += 1
            continue
        if _BASH_REDIRECT_OUT_EXACT_RE.match(token):
            idx += 2  # `> path` — the operand is written, not read
            continue
        if _BASH_REDIRECT_OUT_GLUED_RE.match(token):
            idx += 1  # `>path` / `2>/dev/null`
            continue
        # `< path` is a read whatever the command is. Reported separately
        # because the caller only consults the argv0 grammar, and this read
        # happens even when argv0 is `while`, `done`, or nothing at all
        # (`< file cat`).
        if bare.startswith("<&"):  # `0<&3`: an fd duplication, not a file
            stdin_redirected = True
            idx += 1
            continue
        if bare == "<":
            stdin_redirected = True
            if idx + 1 < len(tokens):
                redirect_reads.append(tokens[idx + 1])
            idx += 2
            continue
        if bare.startswith("<") and len(bare) > 1:
            stdin_redirected = True
            redirect_reads.append(bare[1:])
            idx += 1
            continue
        out.append(token)
        idx += 1
    if stripped_leading_group and out and out[-1][-1:] in {")", "}"}:
        out[-1] = out[-1].rstrip(")}")
        if not out[-1]:
            out.pop()
    if redirect_reads:
        # Strip a trailing group character from the last redirect operand too
        # (`(cat < f)`), for the same reason as above.
        if stripped_leading_group and redirect_reads[-1][-1:] in {")", "}"}:
            redirect_reads[-1] = redirect_reads[-1].rstrip(")}")
            if not redirect_reads[-1]:
                redirect_reads.pop()
    return out, redirect_reads, stdin_redirected


def expand_bash_braces(token: str) -> list[str]:
    """Expand `a{b,c}d` and `a{1..3}` the way the shell does.

    Brace expansion is purely lexical — unlike `$VAR` or `$(...)`, nothing about
    it needs runtime state — so a token carrying one names real files and must
    not be waved through as "a path that does not exist".

    This delegates to `_brace_expand`, the bounded expander the operator-secret
    guard already uses: ranges matter as much as comma groups here (a range left
    unexpanded fails the existence check and the read reaches the auto-approve),
    and two expanders would drift apart on exactly the cases that matter.
    """
    return _brace_expand(token)


def extract_bash_read_targets(
    command: str | None, *, repo_root: Path | None = None
) -> list[str]:
    """Extract the file paths a Bash command reads, per fragment.

    Best-effort by design (issue #42 decision 2): the goal is to widen what is
    provable, not to fail closed on what is not.

    What it can see is bounded by `_BASH_READ_CMD_NAMES` and their grammars, so
    the residue is two classes, not one:
      * a command OUTSIDE that set handed a literal path. Notably
        `python3 workspace/tmp/<arid>/x.py`, which docs/AGENT_CONTRACT.md tells
        leaves to use and `.claude/settings.json` allowlists, and whose heredoc
        body this module deliberately blanks — so the paths that script reads
        are invisible here by construction. Confining that class is the bwrap
        read-confinement work, not this function.
      * targets that exist only at runtime — `xargs cat`, `find -exec`,
        `$(...)`/backtick substitution, `$VAR`, and a `cd` from an EARLIER Bash
        call (a `cd` in THIS command is followed).

    Callers must therefore treat an empty result as "nothing to authorize",
    never as "safe".
    """
    if not command:
        return []
    targets: list[str] = []
    # A backslash-newline is a line continuation, not a fragment boundary; the
    # newline below is a real separator, so join the halves first (both strings
    # stay the same length, keeping the span recovery aligned).
    command = command.replace("\\\n", "  ")
    # A heredoc body is data being written, not commands being run.
    command = _blank_heredoc_bodies(command)
    # Split the QUOTE-STRIPPED string so a separator inside a quoted argument is
    # not treated as one, then recover each fragment's span from the original so
    # quoted filenames survive intact (same idiom as the tee handling in
    # _detect_bash_write_targets; _strip_quoted_strings is length-preserving).
    scanned = _strip_quoted_strings(command)
    # Blank the `&`s that are not separators before splitting (length-preserving,
    # so fragment spans stay aligned with the original).
    scanned = _BASH_FD_DUP_RE.sub(lambda m: " " * len(m.group()), scanned)
    scanned = _BASH_ESCAPED_SEPARATOR_RE.sub(lambda m: " " * len(m.group()), scanned)
    spans: list[tuple[int, int, bool]] = []
    cursor = 0
    piped = False
    for match in _BASH_FRAGMENT_SEPARATOR_RE.finditer(scanned):
        spans.append((cursor, match.start(), piped))
        # Only a real pipe feeds the NEXT fragment's stdin; `;`/`&&` do not.
        # A separator that closed an EMPTY span carries the previous one's
        # meaning forward: in `cat x |\n  rg PAT` the newline must not erase
        # the pipe, or the rg is read as a fresh recursive tree search.
        if command[cursor : match.start()].strip():
            piped = match.group() == "|"
        elif match.group() == "|":
            piped = True
        cursor = match.end()
    spans.append((cursor, len(scanned), piped))
    # Directory the following fragments' relative targets resolve against.
    # "" is repo_root; None means the scan lost track, in which case targets are
    # validated UN-anchored — the direction that still checks something, rather
    # than dropping the read. Only within THIS command string: a `cd` in an
    # earlier Bash call is invisible here (declared residue; agents are told
    # never to `cd`).
    cwd: str | None = ""
    previous_cwd: str | None = ""   # for `cd -`
    dir_stack: list[str | None] = []  # for pushd/popd
    subshell_stack: list[str | None] = []  # cwd to restore when a `(` closes

    def _joined(base: str | None, operand: str) -> str | None:
        if Path(operand).is_absolute():
            return operand
        if base is None:
            return None  # unknown stays unknown; a relative cd cannot recover it
        return str(Path(base) / operand) if base else operand

    for start, end, stdin_from_pipe in spans:
        blob = command[start:end].strip()
        if not blob:
            continue
        # A `cd` inside `( ... )` is undone when the subshell closes. Count on
        # the quote-stripped span so parens inside an argument do not count;
        # `$(` opens a substitution we never enter.
        span_text = scanned[start:end]
        # An unquoted `#` starting a word begins a comment: the rest of the
        # fragment is prose, and a path merely MENTIONED there is not a read.
        comment_at = _bash_comment_start(span_text)
        if comment_at is not None:
            blob = command[start : start + comment_at].strip()
            span_text = span_text[:comment_at]
            if not blob:
                continue
        # `(` opens a subshell whose `cd` is undone when it closes — but a `$(`
        # opens a substitution we never enter, and counting ITS closing paren
        # popped the stack early, un-anchoring every later read.
        opens = closes = 0
        substitution_depth = 0
        for pos, char in enumerate(span_text):
            if char == "(":
                if pos and span_text[pos - 1] == "$":
                    substitution_depth += 1
                else:
                    subshell_stack.append(cwd)
                    opens += 1
            elif char == ")":
                if substitution_depth:
                    substitution_depth -= 1
                else:
                    closes += 1
        try:
            tokens = shlex.split(_ANSI_C_QUOTE_PREFIX_RE.sub("", blob))
        except ValueError:
            tokens = blob.split()
        tokens, redirect_reads, stdin_redirected = _strip_bash_fragment_syntax(tokens)
        if closes > opens:
            # A subshell that OPENED in an earlier fragment closes here, so its
            # `)` is glued to this fragment's last operand (`cat public.md)`).
            for seq in (redirect_reads, tokens):
                if seq and seq[-1].endswith(")"):
                    seq[-1] = seq[-1].rstrip(")")
                    if not seq[-1]:
                        seq.pop()
                    break

        def _record(candidates: list[str]) -> None:
            for target in candidates:
                if not target:
                    continue
                # A token still carrying `$` or a backtick names a path only the
                # shell can compute; there is nothing to validate, so it joins
                # the declared residue rather than being validated as a literal.
                if "$" in target or "`" in target:
                    continue
                anchored = target
                if not Path(target).is_absolute():
                    if cwd:
                        anchored = str(Path(cwd) / target)
                    elif cwd is None:
                        # Unknown anchor: the target is validated un-anchored,
                        # but a leading `..` would then resolve OUTSIDE the repo
                        # and be dropped as bwrap's domain — turning a real
                        # in-repo read into an allow. Clamp to the repo instead.
                        anchored = os.path.normpath(target)
                        while anchored.startswith(".." + os.sep):
                            anchored = anchored[3:]
                        if anchored == "..":
                            anchored = "."
                targets.append(anchored)

        _record(redirect_reads)
        argv0 = tokens[0].split("/")[-1].lower() if tokens else ""
        if argv0 in {"cd", "pushd", "popd"}:
            # Anchor the targets that follow: without this, `cd spec && cat
            # private.md` resolved "private.md" at the repo root, found nothing
            # and authorized nothing, while bash read the file. The operand is
            # literal and known at hook time, so it is not the residue.
            # First non-option operand: `cd -P spec` / `cd -- spec` /
            # `pushd -n spec` anchored at the FLAG, so the following read
            # resolved nowhere and was dropped. `-` alone is `cd -`.
            operand = ""
            rest = tokens[1:]
            if "--" in rest:
                rest = rest[rest.index("--") + 1 :]
            for token in rest:
                if token == "-" or not token.startswith("-"):
                    operand = token
                    break
            if argv0 == "popd":
                cwd, previous_cwd = (dir_stack.pop() if dir_stack else None), cwd
            elif operand == "-":
                cwd, previous_cwd = previous_cwd, cwd
            elif not operand or "$" in operand or "`" in operand:
                # `cd $D`, or a bare `cd` to the home directory.
                previous_cwd, cwd = cwd, None
            else:
                if argv0 == "pushd":
                    dir_stack.append(cwd)
                previous_cwd, cwd = cwd, _joined(cwd, operand)
                # bash leaves the directory unchanged when `cd` FAILS. Anchoring
                # to a directory that is not there sent every later relative
                # target to a path that cannot exist, so the existence filter
                # dropped them and nothing was validated — `cd nosuchdir; cat
                # <path>` walked straight past the guard.
                if repo_root is not None and cwd is not None:
                    try:
                        is_dir = _resolve_target_path(repo_root, cwd).is_dir()
                    except OSError:
                        is_dir = False
                    if not is_dir:
                        cwd = None
        elif argv0 in _BASH_READ_CMD_NAMES:
            _record(
                _extract_read_targets(
                    argv0,
                    tokens,
                    # `rg PAT < file` and `rg PAT <<< text` read stdin exactly
                    # as a pipe tail does, so neither walks the tree.
                    stdin_from_pipe=stdin_from_pipe or stdin_redirected,
                )
            )
        for _ in range(closes):
            if subshell_stack:
                cwd = subshell_stack.pop()
    return targets


# --- Pipe-tail inline-Python AST allowlist ---------------------------------
# Modules a read-only stdin-parsing snippet may legitimately import.  Anything
# capable of file I/O, subprocess, networking, or dynamic import is excluded.
_PIPE_TAIL_ALLOWED_IMPORT_ROOTS: frozenset[str] = frozenset({
    # NB: `string` is intentionally NOT allowed — string.Formatter().get_field()
    # resolves a string-literal attribute path to a LIVE object (e.g.
    # "0.__class__.__bases__[0].__subclasses__"), an RCE primitive the AST
    # inspector cannot see because the dunder chain lives inside a string.
    "json", "sys", "re", "csv", "math", "collections",
    "itertools", "functools", "decimal", "fractions", "statistics",
    "textwrap", "datetime", "unicodedata", "hashlib", "base64", "html",
})
# Builtins that enable dynamic code execution, attribute reflection, or file
# access.  A call to any of these in the body forces a block.
_PIPE_TAIL_DANGEROUS_CALLS: frozenset[str] = frozenset({
    "eval", "exec", "compile", "__import__", "getattr", "setattr",
    "delattr", "globals", "locals", "vars", "open", "input",
    "breakpoint", "memoryview", "exit", "quit", "help",
})
# Bare names that, if referenced, signal a sandbox-escape attempt even when the
# corresponding import was rejected (e.g. relying on an ambient global).
_PIPE_TAIL_DANGEROUS_NAMES: frozenset[str] = frozenset({
    "os", "subprocess", "socket", "shutil", "pathlib", "importlib",
    "ctypes", "builtins", "multiprocessing", "threading", "signal",
    "pty", "fcntl", "mmap", "resource", "platform", "sysconfig",
    "code", "codeop", "runpy", "pickle", "marshal", "gc", "inspect",
    # string.Formatter / operator.attrgetter etc. resolve attribute paths from
    # string literals, defeating AST attribute inspection — block the names too.
    "Formatter", "attrgetter", "methodcaller", "itemgetter",
})
# Non-dunder attributes that are dangerous on an otherwise-allowed module
# (notably `sys.modules`, which reaches every imported module including os;
# and Formatter.get_field, which returns a live object for a string field path).
_PIPE_TAIL_DANGEROUS_ATTRS: frozenset[str] = frozenset({
    "modules", "system", "popen", "spawn", "spawnv", "fork", "execv",
    "execve", "execl", "load_module", "import_module", "find_module",
    "get_field", "get_value", "vformat", "format_field", "convert_field",
})

# Leaf-attribute ALLOWLIST for pipe-tail `-c` bodies.  A blocklist is unsound:
# allowed modules RE-EXPORT other modules (and builtins) as plain non-dunder
# attributes — e.g. `json.codecs.builtins.open`, `statistics.random._os.environ`,
# `re.enum` — reaching arbitrary sinks via ordinary ast.Attribute chains with no
# dunder, no dangerous Name, and no `__` string literal.  So attribute access is
# DENY-BY-DEFAULT: only these well-known data/parse method+attribute names are
# permitted.  Module re-export names (codecs/enum/random/operator/builtins/_os…)
# are absent → the traversal to any dangerous module is severed.
_PIPE_TAIL_ALLOWED_ATTRS: frozenset[str] = frozenset({
    # streams (read-only stdin parsing; .write only reachable on stdout/stderr
    # since `open` is blocked as both Name and attribute)
    "stdin", "stdout", "stderr", "read", "readline", "readlines", "buffer",
    "flush", "write", "writelines", "argv", "maxsize", "byteorder",
    # str / bytes methods
    "strip", "lstrip", "rstrip", "split", "rsplit", "splitlines", "join",
    "replace", "lower", "upper", "casefold", "title", "capitalize",
    "swapcase", "startswith", "endswith", "find", "rfind", "index", "rindex",
    "count", "encode", "decode", "format", "format_map", "zfill", "ljust",
    "rjust", "center", "expandtabs", "partition", "rpartition", "translate",
    "maketrans", "isdigit", "isalpha", "isalnum", "isspace", "isupper",
    "islower", "isnumeric", "isdecimal", "isidentifier", "removeprefix",
    "removesuffix", "hex", "bit_length", "to_bytes", "from_bytes",
    # dict / list / set methods
    "get", "keys", "values", "items", "setdefault", "update", "pop",
    "popitem", "append", "extend", "insert", "remove", "add", "discard",
    "sort", "reverse", "copy", "clear", "fromkeys", "union", "intersection",
    "difference", "issubset", "issuperset", "most_common", "elements",
    "subtract", "total",
    # json
    "loads", "load", "dumps", "dump", "JSONDecodeError",
    # re
    "findall", "match", "search", "fullmatch", "finditer", "sub", "subn",
    "compile", "escape", "group", "groups", "groupdict", "start", "end",
    "span", "expand", "purge", "flags", "pattern",
    "I", "M", "S", "X", "A", "L", "U", "IGNORECASE", "MULTILINE", "DOTALL",
    "VERBOSE", "ASCII", "LOCALE", "UNICODE",
    # csv
    "reader", "writer", "DictReader", "DictWriter", "field_size_limit",
    "excel", "unix_dialect", "register_dialect", "fieldnames",
    "QUOTE_MINIMAL", "QUOTE_ALL", "QUOTE_NONNUMERIC", "QUOTE_NONE",
    # base64 / hashlib
    "b64decode", "b64encode", "b16decode", "b16encode", "b32decode",
    "b32encode", "urlsafe_b64decode", "urlsafe_b64encode",
    "standard_b64decode", "standard_b64encode", "decodebytes", "encodebytes",
    "md5", "sha1", "sha256", "sha512", "sha224", "sha384", "new",
    "hexdigest", "digest", "blake2b", "blake2s",
    # math / statistics / decimal / fractions
    "pi", "e", "tau", "inf", "nan", "sqrt", "floor", "ceil", "trunc", "log",
    "log2", "log10", "exp", "fabs", "factorial", "gcd", "lcm", "isclose",
    "isnan", "isinf", "isfinite", "sin", "cos", "tan", "atan", "atan2",
    "hypot", "degrees", "radians", "mean", "median", "mode", "stdev",
    "variance", "fmean", "fsum", "prod", "comb", "perm",
    "Decimal", "Fraction", "quantize", "numerator", "denominator",
    "as_integer_ratio", "real", "imag", "conjugate",
    # datetime
    "datetime", "date", "time", "timedelta", "timezone", "now", "today",
    "utcnow", "fromisoformat", "fromtimestamp", "utcfromtimestamp",
    "strftime", "strptime", "isoformat", "year", "month", "day", "hour",
    "minute", "second", "microsecond", "weekday", "isoweekday", "timestamp",
    "astimezone", "combine", "utctimetuple", "days", "seconds",
    "total_seconds", "utc",
    # itertools / functools
    "chain", "islice", "cycle", "product", "permutations", "combinations",
    "combinations_with_replacement", "groupby", "accumulate", "starmap",
    "takewhile", "dropwhile", "tee", "zip_longest", "filterfalse", "compress",
    "from_iterable", "reduce", "partial", "lru_cache", "cmp_to_key", "wraps",
    # collections
    "OrderedDict", "defaultdict", "Counter", "deque", "namedtuple",
    "ChainMap", "appendleft", "popleft", "rotate", "maxlen",
    # unicodedata / textwrap
    "normalize", "name", "category", "numeric", "digit", "bidirectional",
    "wrap", "fill", "dedent", "indent", "shorten",
})


def _home_dir() -> Path:
    """The host home directory, read the same way the bwrap profile reads it."""
    raw = (os.environ.get("HOME") or "").strip()
    return Path(raw) if raw else Path.home()


def operator_secret_root() -> Path:
    """`~/.met-dsl/` — where the operator-only dismiss-violation tokens live."""
    return (_home_dir() / ".met-dsl").resolve()


BACKEND_CREDENTIAL_BACKEND_TYPES = ("claude", "codex")


def backend_credential_home_paths(backend_type: str) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """The backend CLI's config/credential home, as `(dirs, files)`.

    CANONICAL for two consumers that must not diverge:
      * `tools/orchestration_runtime.py::_backend_runtime_bind_paths`, which
        rw-binds these into a leaf's bwrap sandbox (the CLI refreshes auth and
        writes its session transcript there);
      * the Bash read guard below, which must forbid reading exactly what that
        profile makes reachable.
    Split dirs/files because the bind side materializes a missing config *dir*
    but existence-gates the auth *file* (it cannot be fabricated).
    """
    home = _home_dir()
    btype = (backend_type or "").strip().lower()
    if btype == "claude":
        return (home / ".claude",), (home / ".claude.json",)
    if btype == "codex":
        # Mirror preflight's codex-home resolution so the guarded path is the
        # bound one even when CODEX_HOME relocates it.
        raw = os.environ.get("CODEX_HOME", "").strip() or os.environ.get("METDSL_HOME", "").strip()
        codex_home = Path(raw).expanduser() if raw else home / ".codex"
        if not codex_home.is_absolute():
            codex_home = codex_home.resolve()
        return (codex_home,), ()
    return (), ()


def protected_host_read_roots() -> tuple[Path, ...]:
    """Out-of-repo host paths a Bash command in workflow mode may never read.

    Two classes, one rule: the operator-secret root (dismiss-violation tokens)
    and every backend credential home the sandbox rw-binds (OAuth credentials +
    session transcripts).  The Read tool reaches neither — the read manifest's
    allowed_read_roots are repo-relative — so this closes the Bash-only route.
    """
    roots: list[Path] = [operator_secret_root()]
    for btype in BACKEND_CREDENTIAL_BACKEND_TYPES:
        dirs, files = backend_credential_home_paths(btype)
        roots.extend(dirs)
        roots.extend(files)
    seen: set[str] = set()
    unique: list[Path] = []
    for root in roots:
        try:
            resolved = root.resolve()
        except (OSError, ValueError, RuntimeError):
            resolved = root
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        unique.append(resolved)
    # Longest path first, so a nested/suffixed root is attributed to itself:
    # `~/.claude.json` must be named by the block message rather than `~/.claude`,
    # whose marker regex also matches it (the `.` after "claude" is a word
    # boundary). Only the message differs — either way the read is blocked.
    unique.sort(key=lambda p: len(str(p)), reverse=True)
    return tuple(unique)


def _protected_root_marker_regex(root: Path) -> re.Pattern[str]:
    """Regex catching the ~ / $HOME / ${HOME} / <abs-home> spellings of `root`.

    Matching the raw command string (not tokens) is what catches the spellings
    adjacent shell punctuation would mangle.

    The root must end where the regex ends: the next character is `/` or not a
    filename character at all. A bare `\b` would be satisfied by `-` and `.`, so
    `~/.claude-notes.txt` would be read as a `~/.claude` hit — and, worse, every
    `~/.claude.json` hit would be attributed to the directory root.
    """
    boundary = r"(?:/|(?![\w.\-]))"
    home = str(_home_dir()).rstrip("/")
    root_s = str(root)
    if root_s.startswith(home + "/"):
        tail = root_s[len(home) + 1 :]
        # `\$\{HOME[^}]*\}` — not just `${HOME}`: every bash parameter expansion
        # of HOME (`${HOME:-/x}`, `${HOME:+$HOME}`, `${HOME%%x}`, `${HOME/x/y}`)
        # expands to the home directory in practice, and `os.path.expandvars`
        # understands none of them, so the token layer below cannot be the one to
        # catch them.
        prefix = r"(?:~|\$HOME\b|\$\{HOME[^}]*\}|" + re.escape(home) + r")/"
        return re.compile(prefix + re.escape(tail) + boundary)
    return re.compile(re.escape(root_s) + boundary)


# `${NAME…}` / `${!NAME…}` — a parameter expansion with any operator body. The
# `[^}]*` tail is deliberately opaque: this guard only needs to know the
# expansion is of NAME.
_PARAM_EXPANSION_RE = re.compile(r"\$\{(!?)([A-Za-z_][A-Za-z0-9_]*)([^}]*)\}")
# The word after a `:-` / `:=` / `:+` / `-` / `=` / `+` / `#` / `%` operator.
_PARAM_EXPANSION_OPERATOR_RE = re.compile(r"^(?::?[-=+]|#{1,2}|%{1,2})(.*)$")
_ASSIGNMENT_TOKEN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
# Any `$NAME` / `${NAME…}` reference, used to keep only the assignments that
# something in the command actually reads.
_VAR_REFERENCE_SCAN_RE = re.compile(r"\$\{?!?([A-Za-z_][A-Za-z0-9_]*)")
# `${NAME}` or `$NAME`, for one-pass substitution from a resolved value map.
# A `$` that cannot begin a variable reference and is not a trailing regex
# anchor — i.e. one left behind by a stripped `$'…'` / `$"…"` construct.
_OBFUSCATING_DOLLAR_RE = re.compile(r"\$[^A-Za-z_{\s]")
_SIMPLE_VAR_REFERENCE_RE = re.compile(
    r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)"
)
# `$'…'` — ANSI-C quoting. bash decodes the escapes inside, so `$'\057etc'` IS
# `/etc`: a spelling in which no protected path appears literally anywhere.
_ANSI_C_QUOTE_RE = re.compile(r"\$'((?:[^'\\]|\\.)*)'")
# `$"…"` — locale translation. With no catalogue it is the string itself, so it
# is pure obfuscation: `~/$".claude.json"` is `~/.claude.json`.
_LOCALE_QUOTE_RE = re.compile(r'\$"((?:[^"\\]|\\.)*)"')
_ANSI_C_SIMPLE_ESCAPES = {
    "a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n",
    "r": "\r", "t": "\t", "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?",
}
# Bounds. This predicate runs synchronously on every tool call, and every axis is
# attacker-chosen: N assignments x M tokens (an `re.sub` each) and N `cd` anchors
# x M relative tokens (a `.resolve()` syscall each) are quadratic uncapped, and a
# cyclic assignment chain (`A=$B$B$B$B; B=$C$C$C$C; C=$A$A$A$A`) grows the
# substituted string exponentially — a 56-character command took 134s.
#
# The bound is on WORK (candidate length, and tokens that can carry an
# expansion), NOT on how many assignments or anchors are considered. Capping the
# latter is fail-OPEN to padding: an evader writes eight throwaway assignments,
# or four throwaway `cd`s, and puts the real one last.
_ASSIGNMENT_PASSES = 3
_CANDIDATE_MAX_LEN = 4096
# Passes over the parameter-expansion phase, for nested `${A:-${HOME}}`.
_PARAM_EXPANSION_PASSES = 2
# (positions x pattern elements) steps allowed in one `${X//pat/rep}` scan.
_GLOB_SUBSTITUTION_OPS_MAX = 60_000
# Total transformation work allowed for ONE command, across every expansion.
_TOTAL_TRANSFORM_OPS_MAX = 120_000
# Ceiling on (candidate x base) path checks in one call. High because each
# check is now a lexical normpath, with a syscall only for a path that exists.
_RESOLVE_OPS_MAX = 40_000
_CD_ANCHOR_MAX_DEPTH = 32
# Matches an exempt glob may have before the exemption is refused outright.
_GLOB_EXEMPT_MATCH_MAX = 256
_CD_FOLD_MAX = 512
# How far past a `cd` to look for its target while skipping the command's own
# options. A literal, not `len(_CD_OPTION_TOKENS)`: the window must not silently
# move when an option spelling is added to or removed from that set.
_CD_OPTION_SCAN_MAX = 8
# `cd`'s own options, skipped when looking for its target directory. `-n` is
# pushd's (it suppresses the directory change, so its operand is not a target —
# skipping it keeps a bogus anchor out of the fold).
_CD_OPTION_TOKENS = frozenset({"--", "-L", "-P", "-e", "-@", "-LP", "-PL", "-n"})
# Options that name a working directory — but only for the commands that use
# them that way. `rsync -C` is `--cvs-exclude`, `scp -C` is compress, `ls -C` and
# `tree -C` take no operand at all, and treating them as directory changes
# deleted the NEXT token (the real read target) from the ancestor rule.
# `${NAME/pat/rep}` / `${NAME//pat/rep}` / `${NAME/#pat/rep}` / `${NAME/%pat/rep}`
# — pattern substitution; the pattern may carry a backslash-escaped `/`.
_PARAM_PATTERN_SUB_RE = re.compile(r"^(//|/#|/%|/)((?:\\.|[^/])*)(?:/(.*))?$")
# `${NAME^^}` / `${NAME,,}` / `${NAME^}` / `${NAME,}`, with an optional glob
# selecting which characters convert.
_PARAM_CASE_RE = re.compile(r"^(\^\^|,,|\^|,)(.*)$")
# `${NAME#pfx}` / `${NAME##pfx}` / `${NAME%sfx}` / `${NAME%%sfx}` — affix strip.
_PARAM_AFFIX_RE = re.compile(r"^(#{1,2}|%{1,2})(.*)$")
# `${NAME:off}` / `${NAME:off:len}` — substring. The negative lookahead keeps the
# alternate-word operators (`:-`, `:+`, `:=`, `:?`) out; bash needs a space
# before a negative offset. The offset is an arithmetic expression.
_PARAM_SUBSTRING_RE = re.compile(r"^:(?![-+=?])([\d +-]*?)(?::([\d +-]+))?$")
_ARITH_TERM_RE = re.compile(r"([+-]?)\s*(\d+)")
_SHELL_SEPARATOR_TOKENS = frozenset({"&&", "||", ";", "|", "&"})
_SHELL_SEPARATOR_CHARS = ";|&"
_DIRECTORY_OPTION_TOKENS = frozenset({"-C", "--directory", "--cd", "--chdir"})
_DIRECTORY_OPTION_COMMANDS = frozenset({
    "tar", "gtar", "bsdtar", "git", "make", "gmake", "cpio", "pax", "ninja",
    "cmake", "patch", "env",
})
# Commands that prefix another command rather than being the one that runs.
_COMMAND_PREFIX_WRAPPERS = frozenset({"sudo", "env", "nohup", "time", "nice", "stdbuf", "xargs"})


def _directory_option_indices(cmd_tokens: Sequence[str]) -> set[int]:
    """Indices of `-C` / `--directory` / `--chdir` tokens that name a directory.

    One FORWARD pass, tracking each segment's argv0: the backward scan this
    replaces was O(index) per token and quadratic over the command (13.6s of CPU
    on a 32 KB one), and it accepted a command NAME appearing as an ARGUMENT, so
    `ls -R tar -C ~` deleted `~` from the ancestor rule.
    """
    marked: set[int] = set()
    argv0: str | None = None
    expect_command = True
    for index, tok in enumerate(cmd_tokens):
        bare = tok.strip().strip("<>();|&\"'`")
        if not bare:
            continue
        if bare in _SHELL_SEPARATOR_TOKENS or any(
            ch in tok for ch in _SHELL_SEPARATOR_CHARS
        ):
            expect_command = True
            argv0 = None
            if bare in _SHELL_SEPARATOR_TOKENS:
                continue
        if expect_command and not bare.startswith("-"):
            name = bare.split("/")[-1]
            if name in _COMMAND_PREFIX_WRAPPERS:
                # `sudo` / `env` pass the real command through, but `env` also
                # has its own `-C`, so it owns the options that follow IT until
                # the real command name arrives.
                argv0 = name
                continue
            argv0 = name
            expect_command = False
            continue
        head = bare.split("=")[0]
        if head in _DIRECTORY_OPTION_TOKENS and (
            head != "-C" or (argv0 in _DIRECTORY_OPTION_COMMANDS)
        ):
            marked.add(index)
    return marked



# `[[:alpha:]]` and friends, which bash accepts inside a `[…]` class.
_POSIX_CLASS_PREDICATES = {
    "alpha": str.isalpha, "digit": str.isdigit, "alnum": str.isalnum,
    "upper": str.isupper, "lower": str.islower, "space": str.isspace,
    "punct": lambda c: not c.isalnum() and not c.isspace() and c.isprintable(),
    "print": str.isprintable, "graph": lambda c: c.isprintable() and not c.isspace(),
    "blank": lambda c: c in " \t", "cntrl": lambda c: not c.isprintable(),
    "xdigit": lambda c: c in "0123456789abcdefABCDEF",
}


def _decode_ansi_c_quotes(text: str) -> str:
    """`$'\\057etc'` -> `/etc`. Unknown escapes are left as their literal char."""

    def _decode(match: "re.Match[str]") -> str:
        body = match.group(1)
        out: list[str] = []
        i = 0
        while i < len(body):
            ch = body[i]
            if ch != "\\" or i + 1 >= len(body):
                out.append(ch)
                i += 1
                continue
            nxt = body[i + 1]
            if nxt in _ANSI_C_SIMPLE_ESCAPES:
                out.append(_ANSI_C_SIMPLE_ESCAPES[nxt])
                i += 2
            elif nxt in "01234567":
                octal = ""
                for d in body[i + 1 : i + 4]:
                    if d not in "01234567":
                        break
                    octal += d
                out.append(chr(int(octal, 8) % 256))
                i += 1 + len(octal)
            elif nxt in "xuU":
                width = {"x": 2, "u": 4, "U": 8}[nxt]
                hexs = ""
                for d in body[i + 2 : i + 2 + width]:
                    if d not in "0123456789abcdefABCDEF":
                        break
                    hexs += d
                if hexs:
                    codepoint = int(hexs, 16)
                    # `$'\\UFFFFFFFF'` is above Unicode's maximum: `chr` raises, and
                    # the raise escaped as a generic hook entrypoint failure on an
                    # otherwise valid command. Leave it literal.
                    out.append(
                        chr(codepoint) if codepoint < 0x110000 else body[i : i + 2 + len(hexs)]
                    )
                    i += 2 + len(hexs)
                else:
                    out.append(nxt)
                    i += 2
            else:
                out.append(nxt)
                i += 2
        return "".join(out)

    decoded = _LOCALE_QUOTE_RE.sub(lambda m: m.group(1), text)
    decoded = _ANSI_C_QUOTE_RE.sub(_decode, decoded)
    if decoded == text and text.startswith("$") and "\\" in text:
        # shlex strips the quotes before this guard ever sees the token, leaving
        # `$\\057etc` — the same string with no `'` for the pattern above to
        # anchor on. Decode the remainder directly.
        return _ANSI_C_QUOTE_RE.sub(_decode, "$'" + text[1:] + "'")
    return decoded


def _local_shell_assignments(cmd_tokens: Sequence[str]) -> dict[str, str]:
    """`NAME=value` assignments made INSIDE the same command.

    `H=$HOME; cat $H/.claude.json` is one command string to this hook, so the
    variable is resolvable here even though it is not in the environment.
    """
    joined = " ".join(cmd_tokens)
    referenced = {m.group(1) for m in _VAR_REFERENCE_SCAN_RE.finditer(joined)}
    indirect = "${!" in joined
    found: dict[str, str] = {}
    for tok in cmd_tokens:
        match = _ASSIGNMENT_TOKEN_RE.match(tok.strip().strip("<>();|&\"'`"))
        if not match:
            continue
        if not indirect and match.group(1) not in referenced:
            # Unreferenced: cannot change where a read lands.
            continue
        # The value carries the same glued shell punctuation a path token does
        # (`H=$HOME;` when shlex keeps the separator attached). Truncated per
        # VALUE, the same bound every candidate carries — but the NUMBER of
        # assignments is deliberately unbounded here, and so is their total size.
        # Every bound tried on this axis (a count, then a byte budget) was
        # fail-open the same way: an evader pads with assignments that satisfy
        # the bound, and appends the real one after it. The work is linear in the
        # command's own length, which the OS already bounds.
        found[match.group(1)] = match.group(2).strip().strip("<>();|&\"'`")[:_CANDIDATE_MAX_LEN]
    if indirect:
        # `${!V}` names its target by V's VALUE, not by any `$`-spelling of the
        # target's own name, so relevance-by-reference would drop exactly the
        # assignment the read uses (`T=.codex; V=T; cat ${!V}/config.toml`).
        referenced |= {value.strip() for value in found.values()}
    # An assignment nothing references cannot change where a read lands. This is
    # a RELEVANCE bound, not a count: truncating at N is fail-open to padding.
    return {name: value for name, value in found.items() if name in referenced}


def _resolved_assignment_map(assignments: dict[str, str]) -> dict[str, str]:
    """`assignments` with each value expanded through the others, to a fixpoint.

    Resolving the map ONCE, rather than substituting every assignment into every
    token, is what keeps the per-token cost independent of how many assignments
    the command carries — the property that lets this bound relevance instead of
    count. Bounded by pass count and value length: a cyclic chain
    (`A=$B$B$B$B; B=$C$C$C$C; C=$A$A$A$A`) otherwise multiplies each value per
    pass, and a 56-character command took over a minute.
    """
    resolved = dict(assignments)
    for _pass in range(_ASSIGNMENT_PASSES):
        changed = False
        for name, value in list(resolved.items()):
            if "$" not in value:
                continue
            new_value = _substitute_variables(value, resolved)
            if new_value != value:
                resolved[name] = new_value
                changed = True
        if not changed:
            break
    return resolved


def _substitute_variables(text: str, values: dict[str, str]) -> str:
    r"""One pass of `$NAME` / `${NAME…}` substitution from `values`, length-bounded.

    A single regex pass over the text, so the cost is the text's length rather
    than the number of assignments.

    The `_CANDIDATE_MAX_LEN` bound is applied DURING the pass, not to the result:
    one `re.sub` over a token carrying N references to an M-character value
    allocates N*M bytes before any caller could truncate it, and both factors are
    attacker-chosen — a single command drove a hook process to 18 GB RSS. Once
    the budget is spent the remaining references are left unexpanded.

    Never uses a value as an `re.sub` replacement TEMPLATE: a value is command
    text, and a ``\1`` / ``\d`` / trailing backslash in one makes re.sub raise —
    which crashed the hook on ordinary commands like ``PAT='\d+' grep -E "$PAT" f``.
    """
    if "$" not in text or not values:
        return text[:_CANDIDATE_MAX_LEN]
    budget = _CANDIDATE_MAX_LEN

    def _replace(match: "re.Match[str]") -> str:
        nonlocal budget
        literal = match.group(0)
        name = match.group(1) or match.group(2)
        value = values.get(name)
        if value is None:
            budget -= len(literal)
            return literal
        if len(value) > budget:
            budget = 0
            return literal
        budget -= len(value)
        return value

    return _SIMPLE_VAR_REFERENCE_RE.sub(_replace, text)[:_CANDIDATE_MAX_LEN]


def _glob_class_members(body: str) -> tuple[Callable[[str], bool], bool]:
    """The characters a `[…]` class body matches, and whether it is negated.

    A `]` as the FIRST member is a literal `]`, which is why the caller's
    terminator search skips it.
    """
    negate = body.startswith(("!", "^"))
    if negate:
        body = body[1:]
    singles: set[str] = set()
    ranges: list[tuple[int, int]] = []
    classes: list[str] = []
    i = 0
    while i < len(body):
        if body.startswith("[:", i):
            close = body.find(":]", i + 2)
            if close != -1:
                if body[i + 2 : close] in _POSIX_CLASS_PREDICATES:
                    classes.append(body[i + 2 : close])
                i = close + 2
                continue
        if i + 2 < len(body) and body[i + 1] == "-":
            # Kept as a RANGE, never expanded: `[a-\U0010ffff]` is one element in
            # the pattern but 1.1M characters as a set, and materializing it took
            # 293s of CPU in a hook that runs on every tool call.
            ranges.append((ord(body[i]), ord(body[i + 2])))
            i += 3
        else:
            singles.add(body[i])
            i += 1
    predicates = [_POSIX_CLASS_PREDICATES[name] for name in classes]

    def _member(ch: str) -> bool:
        if ch in singles:
            return True
        code = ord(ch)
        if any(low <= code <= high for low, high in ranges):
            return True
        return any(predicate(ch) for predicate in predicates)

    return _member, negate


def _glob_class_end(pattern: str, open_index: int) -> int:
    """Index of the `]` closing the class opened at `open_index`, or -1."""
    scan = open_index + 1
    if pattern[scan : scan + 1] in ("!", "^"):
        scan += 1
    if pattern[scan : scan + 1] == "]":  # a leading `]` is a literal member
        scan += 1
    while pattern.startswith("[:", scan):
        # A POSIX class carries its own `]`; the class ends at the one AFTER it.
        inner = pattern.find(":]", scan + 2)
        if inner == -1:
            break
        scan = inner + 2
    return pattern.find("]", scan)


def _glob_match_frontier(
    pattern: str, text: str, start: int = 0, mask_cache: dict[str, int] | None = None
) -> int:
    """The reachable-position bitset after matching `pattern` from `start`."""
    return _glob_match_lengths(pattern, text, start, mask_cache, _frontier=True)


def _glob_match_lengths(
    pattern: str,
    text: str,
    start: int = 0,
    mask_cache: dict[str, int] | None = None,
    _frontier: bool = False,
):
    """Lengths of every prefix of `text[start:]` that the glob `pattern` matches.

    A bitset dynamic program, NOT a regex: `${V##*a*a*a*a*b}` makes a translated
    regex backtrack catastrophically — measured 8s at a 240-character value and
    no return at all at the 4096-character bound, in a hook that runs
    synchronously on every tool call. Each pattern element is one pass over a
    single big integer whose bit `i` means "position `i` is reachable", so a
    whole match set costs O(len(pattern) x len(text)/64) with no backtracking.

    `mask_cache` holds the per-character and per-class position masks for THIS
    text. Passing one across the calls of a `${X//pat/rep}` scan is what keeps
    that scan linear: rebuilding a `[a-z]` mask per start position is a Python
    loop over the whole value, and a 4 KB value with a few classes took 10-65s.
    """
    n = len(text)
    full = (1 << (n + 1)) - 1
    if mask_cache is None:
        mask_cache = {}

    def _mask(key: str, matches) -> int:
        cached = mask_cache.get(key)
        if cached is None:
            cached = 0
            for pos, tc in enumerate(text):
                if matches(tc):
                    cached |= 1 << pos
            mask_cache[key] = cached
        return cached

    frontier = 1 << start
    i = 0
    while i < len(pattern):
        if not frontier:
            return 0 if _frontier else []
        ch = pattern[i]
        if ch == "*":
            low = frontier & -frontier
            frontier = full & ~(low - 1)
            i += 1
            continue
        if ch == "?":
            frontier = (frontier << 1) & full
            i += 1
            continue
        if ch == "[":
            close = _glob_class_end(pattern, i)
            if close != -1:
                body = pattern[i + 1 : close]
                member, negate = _glob_class_members(body)
                frontier = (
                    (frontier & _mask("k" + body, lambda tc: member(tc) != negate)) << 1
                ) & full
                i = close + 1
                continue
        if ch == "\\" and i + 1 < len(pattern):
            i += 1
            ch = pattern[i]
        frontier = ((frontier & _mask("c" + ch, lambda tc, _c=ch: tc == _c)) << 1) & full
        i += 1
    if _frontier:
        return frontier
    return [pos - start for pos in range(start, n + 1) if frontier >> pos & 1]


def _glob_match_bounds(
    pattern: str, text: str, start: int = 0, mask_cache: dict[str, int] | None = None
) -> tuple[int, int] | None:
    """`(shortest, longest)` match length at `start`, or None if no match.

    Bit arithmetic on the frontier, not a list comprehension over the text: the
    comprehension made every call O(len(text)) even when the caller only needed
    the extremes, which is what left a `${X//[a-z]/_}` scan quadratic after its
    per-position work was supposedly bounded.
    """
    frontier = _glob_match_frontier(pattern, text, start, mask_cache)
    if not frontier:
        return None
    return (
        (frontier & -frontier).bit_length() - 1 - start,
        frontier.bit_length() - 1 - start,
    )


def _apply_parameter_transformation(
    value: str, tail: str, work_budget: list[int] | None = None
) -> str:
    """Apply the `${NAME…}` operator in `tail` to `value`, as bash would.

    Only the transforming operators: the operators that select an ALTERNATE word
    (`:-`, `:=`, `:+`) are the caller's business, since which side bash takes is
    decided by whether the variable is set. The operand of `/`, `#` and `%` is a
    GLOB in bash, not a literal — `${X/x*/seiya}` turns `/home/x` into
    `/home/seiya` — so it is matched as one, with bash's shortest (`#`, `%`) vs
    longest (`##`, `%%`) semantics.
    """
    if not tail:
        return value
    if work_budget is not None:
        if work_budget[0] <= 0:
            # The per-expansion budget below bounds ONE substitution; nothing
            # bounded their NUMBER, and n assignments x n expansions reached 26s
            # of CPU on a 1.6 MB command. This budget is per COMMAND.
            return value
        work_budget[0] -= len(value) + len(tail)
    match = _PARAM_SUBSTRING_RE.match(tail)
    if match:
        # `${X:2}` / `${X:2:5}` / `${X: -4}`. NOT `${X:-word}`, which is the
        # alternate-word operator — the pattern requires a digit, a `+`, or the
        # space bash itself requires before a negative offset.
        offset = 0 if not match.group(1).strip() else _evaluate_integer_arithmetic(match.group(1))
        if offset is None:
            return value
        if offset < 0:
            offset = max(0, len(value) + offset)
        if match.group(2) is None:
            return value[offset:]
        length = _evaluate_integer_arithmetic(match.group(2))
        if length is None:
            return value
        return value[offset:length] if length < 0 else value[offset : offset + length]
    match = _PARAM_PATTERN_SUB_RE.match(tail)
    if match:
        pattern = match.group(2)
        # bash removes the escapes from the REPLACEMENT too: the operand of
        # `${V/#\/tmp/\/home}` is `/home`, not `\/home`.
        replacement = re.sub(r"\\(.)", r"\1", match.group(3) or "")
        anchor = match.group(1)
        if not pattern and anchor in ("//", "/"):
            return value
        if anchor in ("/#", "/%") and not pattern:
            # `${V/#/rep}` with an EMPTY pattern prepends (or appends).
            return replacement + value if anchor == "/#" else value + replacement
        if anchor in ("/#", "/%"):
            # `${V/#pat/rep}` / `${V/%pat/rep}` replace an anchored prefix/suffix.
            if anchor == "/#":
                bounds = _glob_match_bounds(pattern, value)
                return replacement + value[bounds[1]:] if bounds else value
            bounds = _glob_match_bounds(_reverse_glob(pattern), value[::-1])
            return value[: len(value) - bounds[1]] + replacement if bounds else value
        out: list[str] = []
        pos = 0
        first_only = anchor == "/"
        mask_cache: dict[str, int] = {}
        # Work budget: the scan is (positions x pattern elements) big-integer
        # steps, and BOTH are attacker-chosen — a 100-class operand over a 4 KB
        # value costs 2.8s. Past the budget the tail is left untransformed, the
        # same direction as every other work bound here.
        budget = _GLOB_SUBSTITUTION_OPS_MAX
        # Each position costs O(len(pattern) x len(value)/64) big-integer steps;
        # debiting only the pattern length left the real work uncounted, and a
        # 2.3 KB command still cost seconds.
        per_position = max(1, len(pattern)) * (len(value) // 64 + 1)
        while pos <= len(value):
            budget -= per_position
            if budget <= 0:
                return "".join(out) + value[pos:]
            bounds = _glob_match_bounds(pattern, value, pos, mask_cache)
            if bounds and bounds[1] > 0:
                out.append(replacement)
                pos += bounds[1]
                if first_only:
                    return "".join(out) + value[pos:]
                continue
            if pos < len(value):
                out.append(value[pos])
            pos += 1
        return "".join(out)
    match = _PARAM_CASE_RE.match(tail)
    if match:
        op, selector = match.group(1), match.group(2)
        # `${V,,pat}` / `${V^^pat}` case-convert only the characters the glob
        # `pat` matches; with no pattern, every character.
        if selector:
            # The selector is a GLOB, not a class or a literal: `${V,,?}` and
            # `${V,,*}` select every character, and both were left unconverted.
            def _selected(ch: str, _sel: str = selector) -> bool:
                return _glob_matches_whole(_sel, ch)

        else:
            def _selected(ch: str) -> bool:
                return True

        convert = str.upper if op in ("^", "^^") else str.lower
        if op in ("^^", ",,"):
            return "".join(convert(ch) if _selected(ch) else ch for ch in value)
        if value and _selected(value[0]):
            return convert(value[0]) + value[1:]
        return value
    match = _PARAM_AFFIX_RE.match(tail)
    if match:
        op, affix = match.group(1), match.group(2)
        if not affix:
            return value
        longest = len(op) == 2
        if op.startswith("#"):
            bounds = _glob_match_bounds(affix, value)
            return value[(bounds[1] if longest else bounds[0]):] if bounds else value
        # A suffix is a prefix of the reversed text against the reversed glob;
        # a glob's structure is symmetric, so this needs no separate matcher and
        # avoids the O(len(value)**2) backward scan it replaces.
        bounds = _glob_match_bounds(_reverse_glob(affix), value[::-1])
        if not bounds:
            return value
        strip = bounds[1] if longest else bounds[0]
        return value[: len(value) - strip]
    return value


def _evaluate_integer_arithmetic(text: str) -> int | None:
    """`1+1` / `-3` / ` 2 ` — bash evaluates a substring offset arithmetically."""
    terms = _ARITH_TERM_RE.findall(text)
    if not terms or _ARITH_TERM_RE.sub("", text).strip():
        return None
    return sum(int(sign + digits) if sign else int(digits) for sign, digits in terms)


def _reverse_glob(pattern: str) -> str:
    """`pattern` reversed, keeping `[…]` classes and `\\x` escapes intact."""
    parts: list[str] = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "[":
            close = _glob_class_end(pattern, i)
            if close != -1:
                parts.append(pattern[i : close + 1])
                i = close + 1
                continue
        if ch == "\\" and i + 1 < len(pattern):
            # A LONE trailing backslash stays literal; pairing it with the next
            # character after reversal would turn it into an escape.
            parts.append(pattern[i : i + 2])
            i += 2
            continue
        parts.append(ch)
        i += 1
    return "".join(reversed(parts))


def _shell_expansion_variants(
    text: str, assignments: dict[str, str], work_budget: list[int] | None = None
) -> list[str]:
    """Fail-closed candidate spellings of `text` after shell expansion.

    `os.path.expandvars` handles only bare `$NAME` / `${NAME}`, and only from the
    real environment. Three gaps it leaves, all of which reached a protected
    path: a `${NAME…}` form carrying any operator, `${!NAME}` indirection, and a
    variable assigned earlier in the same command (possibly through another
    variable, hence the bounded fixpoint). All are answered by generating
    candidates rather than by emulating bash: a candidate that over-approximates
    can only make this predicate block more, which is the safe direction for a
    read guard.
    """
    variants = [text]
    # `os.path.expanduser` knows `~` and `~user` only. bash also has `~+` ($PWD),
    # `~-` ($OLDPWD) and `~N` (dirstack) — all of which can BE the home directory,
    # and none of which expanduser touches, so the token would stay lexically
    # relative and resolve inside the repo.
    tilde = re.match(r"^~(\+|-|\d+)(/.*)?$", text)
    if tilde:
        tail = (tilde.group(2) or "").lstrip("/")
        base_dir = os.getcwd() if tilde.group(1) == "+" else str(_home_dir())
        variants.append(os.path.join(base_dir, tail) if tail else base_dir)
    decoded = _decode_ansi_c_quotes(text)
    if decoded != text and decoded:
        variants.append(decoded)
    # Substitution comes BEFORE the heuristic candidates below, so a caller that
    # takes the first plausible spelling (the `cd` fold) gets the real expansion
    # rather than a `$`-stripped guess.
    if assignments:
        for base in list(variants):
            substituted = _substitute_variables(base, assignments)
            if substituted != base and substituted not in variants:
                variants.append(substituted)
    if "$" in text:
        # A stripped `$'c'laude.json` reaches this guard as `$claude.json`: the
        # `$` is followed by a LETTER, so it looks like a variable reference and
        # the punctuation rule below does not fire. A reference to a name that is
        # defined nowhere is not a reference — bash would expand it to nothing,
        # and the only way the token names a real path is if the `$` was a
        # quoting construct's remains. Dropping only the UNDEFINED ones keeps
        # `$HOME` / `$PATH` out of this heuristic.
        def _drop_undefined(match: re.Match[str]) -> str:
            name = match.group(1)
            if name in assignments or name in os.environ:
                return match.group(0)
            return name

        undefined_dropped = re.sub(r"\$([A-Za-z_][A-Za-z0-9_]*)", _drop_undefined, text)
        if undefined_dropped != text and undefined_dropped not in variants:
            variants.append(undefined_dropped)
    if _OBFUSCATING_DOLLAR_RE.search(text):
        # shlex strips the quotes of a MID-token construct before this guard sees
        # it, leaving `~/$.claude.json` for `~/$'.'claude.json` — nothing left to
        # decode, but dropping the `$` recovers the path. Restricted to a `$`
        # that CANNOT start a variable reference: applying it to every `$` made
        # `cd ~ && grep -c '\.claude\.json$' notes.txt` a protected read, because
        # the trailing regex anchor also decodes to `.claude.json`.
        dropped = text.replace("$", "")
        if dropped and dropped not in variants:
            variants.append(dropped)
        if "\\" in dropped:
            # `/home/sei$'\171'a/…` reaches this guard as `/home/sei$\171a/…`:
            # the quotes are gone, so the escapes have to be decoded where they
            # sit rather than inside a `$'…'` construct.
            unescaped = _decode_ansi_c_quotes("$'" + dropped + "'")
            if unescaped and unescaped not in variants:
                variants.append(unescaped)
    expanded: list[str] = []
    # Two passes: `${A:-${HOME}}` is truncated by the expansion pattern's
    # `[^}]*`, and the candidate it recovers (`${HOME}/…`) is itself an
    # expansion. One pass produced it and then never looked at it again.
    for _pass in range(_PARAM_EXPANSION_PASSES):
      pass_input = list(variants) if _pass == 0 else list(expanded)
      for base in pass_input:
          if "${" not in base:
              continue
          # Every expansion in the token is substituted — the substitution is
          # linear in their number and the fan-out is 2 candidates regardless, so
          # there is nothing here to cap. Capping the COUNT would only be
          # fail-open to a `${Z1}…${Z9}${HOME}` padding shape.
          if not _PARAM_EXPANSION_RE.search(base):
              continue
          # bash decides each `${X:-word}` INDEPENDENTLY, and which side it takes
          # is not a guess: `:-` / `:=` take the operand exactly when the variable
          # is unset or empty, and `-` / `=` / `+` when it is unset. So resolve
          # each one, rather than enumerating combinations — enumerating was both
          # exponential and incomplete past its bit cap, and
          # `A=$HOME/; cat ${A:-x}${B:-.codex}/auth.json` (A's value with B's
          # operand) is exactly the mixed shape a uniform choice cannot express.
          # The two uniform spellings are kept as extra candidates, for the cases
          # where "set" here and "set" in the real shell disagree.
          for operand_mode in ("resolved", "never", "always"):

              def _replace(match: re.Match[str], _mode: str = operand_mode) -> str:
                  indirect, name, tail = match.group(1), match.group(2), match.group(3)
                  operand = _PARAM_EXPANSION_OPERATOR_RE.match(tail)
                  _use_operand = False
                  if operand:
                      raw = assignments.get(name, os.environ.get(name))
                      if _mode == "always":
                          _use_operand = True
                      elif _mode == "resolved":
                          colon = tail.startswith(":")
                          op_char = tail[1] if colon and len(tail) > 1 else tail[:1]
                          present = raw is not None and (bool(raw) or not colon)
                          # `+` INVERTS the test: `${X:+w}` yields the word when X
                          # is set and non-empty, `${X:-w}` when it is not.
                          _use_operand = present if op_char == "+" else not present
                  if _use_operand:
                      return operand.group(1)
                  value = assignments.get(name)
                  if value is None:
                      value = os.environ.get(name, "")
                  if not _use_operand:
                      # `${X/x/y}` / `${X^^}` / `${X#pfx}` TRANSFORM the value.
                      # Falling through to the untransformed value let
                      # `X=/home/x; cat ${X/x/seiya}/.codex/auth.json` reach the
                      # credential home while this predicate said nothing.
                      value = _apply_parameter_transformation(value, tail, work_budget)
                  if indirect:
                      # `${!V}` names the variable whose NAME is V's value.
                      target = value.strip()
                      if not target:
                          return ""
                      indirect_value = assignments.get(target)
                      if indirect_value is None:
                          indirect_value = os.environ.get(target, "")
                      return indirect_value
                  return value

              candidate = _PARAM_EXPANSION_RE.sub(_replace, base)
              if candidate and candidate not in variants and candidate not in expanded:
                  expanded.append(candidate)
    return variants + expanded


def _blank_persisted_tool_results(command: str, repo_root: Path) -> str:
    """`command` with any persisted tool-result path replaced by a placeholder."""
    try:
        prefix = str(
            _home_dir() / _AUTO_READ_PROJECT_TOOL_RESULTS_PARENT_TAIL
            / _claude_project_slug(repo_root.resolve())
        )
    except (OSError, RuntimeError, ValueError):
        return command
    home = str(_home_dir()).rstrip("/")
    tail = prefix[len(home) + 1 :] if prefix.startswith(home + "/") else None
    heads = [re.escape(prefix)]
    if tail:
        # The same file is spelled `~/…`, `$HOME/…` and `${HOME}/…` too; blanking
        # only the absolute form left those reaching the marker scan.
        heads.append(r"(?:~|\$HOME|\$\{HOME\})/" + re.escape(tail))
    if not any(re.search(head, command) for head in heads):
        return command
    # Components, not "anything": `[^\s'\"]+` can swallow slashes, and the
    # backtracking that follows cost 6s of CPU on a 120 KB command.
    body = r"/[^\s'\"/]+/" + re.escape(_AUTO_READ_PROJECT_TOOL_RESULTS_DIR_COMPONENT) + r"/[^\s'\"/]+"
    for head in heads:
        command = re.sub(head + body + r"\.txt", "PERSISTED_TOOL_RESULT", command)
        # A glob over the same directory is the same read.
        command = re.sub(head + body, "PERSISTED_TOOL_RESULT", command)
    return command


def _is_persisted_tool_result_shape(repo_root: Path, target: Path) -> bool:
    """Whether `target` is a persisted tool-result of THIS repo's project.

    `~/.claude/projects/<repo-slug>/<session>/tool-results/<id>.txt` is where the
    harness saves an oversized tool output and then tells the agent to read it —
    the Read tool has always permitted it (`_is_persisted_tool_result_read`), and
    blocking it for Bash made that mechanism unreachable from a shell. Three real
    commands in this repository's own hook logs are of this shape.

    Shape-and-slug only: the per-session check the Read path makes needs an
    agent_run_id this guard does not have. What it exempts is a tool output, not
    a credential; the file-tool layer still enforces the session for `Read`.
    """
    try:
        if target.suffix != ".txt" or target.parent.name != _AUTO_READ_PROJECT_TOOL_RESULTS_DIR_COMPONENT:
            return False
        project_root = (
            _home_dir() / _AUTO_READ_PROJECT_TOOL_RESULTS_PARENT_TAIL
            / _claude_project_slug(repo_root.resolve())
        )
        if len(target.relative_to(project_root).parts) != 3:
            return False
        # A hardlink resolves to ITSELF, so resolution cannot tell one from the
        # credential file it points at. A persisted output has one link.
        try:
            return target.stat().st_nlink <= 1
        except (OSError, ValueError):
            return True
    except (OSError, RuntimeError, ValueError):
        return False


def _cd_operand_indices(cmd_tokens: Sequence[str]) -> set[int]:
    """Indices of the tokens that are a `cd` / `pushd` / `-C` operand.

    By INDEX, not by spelling: excluding the spelling let a no-op `cd ~` delete
    `~` from every later position too, so `cd ~ && grep -r sk-ant- ~` stopped
    being a recursive read of the home directory.
    """
    operands: set[int] = set()
    option_indices = _directory_option_indices(cmd_tokens)
    for index, tok in enumerate(cmd_tokens):
        bare = tok.strip().strip("<>();|&\"'`")
        if bare.split("/")[-1] not in ("cd", "pushd") and index not in option_indices:
            continue
        for offset, follower in enumerate(
            cmd_tokens[index + 1 : index + 1 + _CD_OPTION_SCAN_MAX]
        ):
            candidate = follower.strip().strip("<>();|&\"'`")
            for sep in _SHELL_SEPARATOR_CHARS:
                candidate = candidate.split(sep, 1)[0]
            candidate = candidate.strip()
            if not candidate or candidate in _SHELL_SEPARATOR_TOKENS:
                break
            if candidate in _CD_OPTION_TOKENS:
                continue
            operands.add(index + 1 + offset)
            break
    return operands


def _cd_anchor_dirs(
    cmd_tokens: Sequence[str],
    repo_root: Path,
    assignments: dict[str, str],
    work_budget: list[int] | None = None,
) -> list[Path]:
    """Directories a `cd` in this command makes later relative paths resolve from.

    `cd ~ && cat .claude.json` reads a protected path with no protected spelling
    anywhere in the token list. The `cd`s are FOLDED in order — each resolves
    against the directory the previous one reached — so a relative walk
    (`cd ..; cd ..`) lands where bash lands, and every intermediate directory is
    kept as an anchor. Anchors then apply to every relative token, not only to
    the ones that syntactically follow their `cd`: an over-approximation in the
    blocking direction, which keeps this a cheap predicate rather than a shell
    simulator.

    Covered spellings: `cd`, `pushd`, a `cd` glued to a separator or wrapped in
    `(`…`)`, a separator glued to its TARGET (`cd ~;cat x`), `cd` with its own
    options (`-P`, `--`, `-n`, …), and a BARE `cd`, which goes to `$HOME` — the
    same shape as `cd ~`, and the one an evader reaches for once `cd ~` closes.

    Every fold step is kept as an anchor. Capping their NUMBER was fail-open:
    ten throwaway `cd`s after the real one pushed it out of the window. The
    number is already bounded by `_CD_FOLD_MAX` folds.
    """
    anchors: list[Path] = []
    home = _home_dir()
    current = repo_root
    # `-C <dir>` / `--directory <dir>` change the directory the way `cd` does:
    # `tar cf - -C ~ .codex`, `git -C ~ log`, `make -C ~ x` all read from there.
    directory_option_indices = _directory_option_indices(cmd_tokens)
    cd_indices = [
        i
        for i, tok in enumerate(cmd_tokens)
        if tok.strip().strip("<>();|&\"'`").split("/")[-1] in ("cd", "pushd")
        or i in directory_option_indices
    ]
    # Fold at most the LAST `_CD_FOLD_MAX` of them, from repo_root. Each fold
    # step is a `.resolve()`, so an unbounded chain is quadratic; taking the tail
    # rather than the head keeps the padding shape (`cd a; … ; cd ~`) closed, and
    # starting the truncated fold at repo_root only widens what is tested.
    for index in cd_indices[-_CD_FOLD_MAX:]:
        tok = cmd_tokens[index]
        target: str | None = None
        # A separator glued to the `cd` token itself (`cd; cat x`) means the cd
        # took no argument — the following token is the next COMMAND, not a
        # directory, and a bare `cd` goes to $HOME.
        glued = tok.strip().strip("<>();|&\"'`")
        if "=" in glued and glued.split("=")[0] in _DIRECTORY_OPTION_TOKENS:
            # `--directory=../..` carries its operand in the same token.
            followers = [glued.split("=", 1)[1]]
        else:
            followers = (
                []
                if any(ch in tok for ch in _SHELL_SEPARATOR_CHARS)
                else cmd_tokens[index + 1 : index + 1 + _CD_OPTION_SCAN_MAX]
            )
        for follower in followers:
            candidate = follower.strip().strip("<>();|&\"'`")
            # A separator glued to the TARGET (`cd ~;cat x`) leaves the next
            # command attached, which end-stripping cannot remove.
            for sep in _SHELL_SEPARATOR_CHARS:
                candidate = candidate.split(sep, 1)[0]
            candidate = candidate.strip()
            if not candidate or candidate in _SHELL_SEPARATOR_TOKENS:
                break  # bare `cd` — target stays None
            if candidate in _CD_OPTION_TOKENS:
                continue
            target = candidate
            break
        if target is None:
            spellings = [str(home)]
        else:
            spellings = _shell_expansion_variants(target, assignments, work_budget)
            # `cd $UNSET` / `cd ${NOPE}` / `cd ""` — bash treats an empty or
            # unresolvable operand as no operand at all, which is `cd $HOME`.
            # Decided on the TARGET, not on the derived spellings: the heuristic
            # candidates include `$`-stripped guesses, which always look
            # resolvable and would hide this case.
            probe = os.path.expanduser(
                os.path.expandvars(_substitute_variables(target, assignments))
            )
            if not probe.strip() or "$" in probe:
                spellings = [*spellings, str(home)]
        landed: Path | None = None
        for spelling in spellings:
            expanded = os.path.expanduser(os.path.expandvars(spelling))
            if any(ch in expanded for ch in "*?[{"):
                continue
            if "$" in expanded:
                # An unexpanded reference is not a directory. Accepting it as one
                # (`.resolve()` succeeds on any non-glob string) made the literal
                # `$H` win and discarded the resolved spelling behind it, so
                # `H=/home/x; cd $H && cat .claude.json` anchored nowhere.
                continue
            try:
                anchor = (
                    Path(expanded).resolve()
                    if os.path.isabs(expanded)
                    else (current / expanded).resolve()
                )
            except (OSError, ValueError, RuntimeError):
                continue
            if len(anchor.parts) > _CD_ANCHOR_MAX_DEPTH:
                # Do not deepen past this: `.resolve()` is O(depth), so letting a
                # chain of throwaway `cd`s grow `current` unboundedly makes the
                # fold itself quadratic. `continue`, not `break` — the token's
                # remaining spellings may still name a shallow directory.
                continue
            if anchor not in anchors:
                anchors.append(anchor)
            # Fold: the first PLAUSIBLE spelling is where this cd landed, and the
            # next cd resolves from there. Every spelling is still kept as an
            # anchor, since which one bash took is not knowable here.
            if landed is None:
                landed = anchor
        if landed is not None:
            current = landed
    # Drop implausibly deep anchors before capping. A folded chain of throwaway
    # `cd`s (`cd d0 && … && cd d799`) builds an 800-component path, and every
    # later token then resolves against it — `.resolve()` is O(depth), so the
    # pair is quadratic (measured 16s). No real working directory is that deep,
    # and dropping these costs no coverage: the tokens still resolve against
    # repo_root and against the anchors that survive.
    anchors = [a for a in anchors if len(a.parts) <= _CD_ANCHOR_MAX_DEPTH]
    # Keep the TAIL: an evader pads with throwaway `cd`s and puts `cd ~` last.
    return anchors


def _glob_matches_stay_exempt(repo_root: Path, pattern: str) -> bool:
    """Whether every existing match of `pattern` is still exempt once RESOLVED.

    The literal path re-tests the exemption after `.resolve()`; the glob branch
    did not, so a symlink planted at an exempt-shaped name was readable through
    `cat …/tool-results/z*` while its literal spelling blocked.
    """
    try:
        matches = glob.glob(pattern)
    except (OSError, ValueError):
        return False
    for match in matches[:_GLOB_EXEMPT_MATCH_MAX]:
        try:
            resolved = Path(match).resolve()
        except (OSError, ValueError, RuntimeError):
            return False
        if not _is_persisted_tool_result_shape(repo_root, resolved):
            return False
    return len(matches) <= _GLOB_EXEMPT_MATCH_MAX


def _glob_pattern_reaches_root(
    pattern: str, roots: Sequence[Path], repo_root: Path, anchors: Sequence[Path]
) -> Path | None:
    """The root a wildcard read target can reach, else None.

    A RELATIVE pattern is anchored the same way a literal relative target is:
    against repo_root and against every `cd` the command carries. Checking only
    the repo-relative form let `cd ~ && cat .clau*e.json` through —
    `_glob_pattern_targets_root` rejects a non-absolute pattern outright, and
    `glob.glob` would have run from the hook process's own cwd.
    """
    literal_prefix = pattern.split("*")[0].split("?")[0].split("[")[0]
    if (
        literal_prefix
        and ".." not in pattern.split("/")
        and _is_persisted_tool_result_shape(repo_root, Path(literal_prefix + "x.txt"))
        and _glob_matches_stay_exempt(repo_root, pattern)
    ):
        # A glob over the persisted tool-results directory is the same read the
        # literal spelling is exempt for.
        return None
    bases: list[Path] = [repo_root] if os.path.isabs(pattern) else [repo_root, *anchors]
    candidates = [
        pattern if os.path.isabs(pattern) else os.path.join(str(base), pattern)
        for base in bases
    ]
    # Cheap lexical check FIRST for every (pattern, root) pair (no filesystem
    # touch), only then the bounded real-glob — never an unbounded glob.glob on
    # attacker patterns, and never one root's filesystem walk ahead of another
    # root's free lexical answer.
    for candidate in candidates:
        for root in roots:
            if _glob_pattern_targets_root(candidate, root):
                return root
    for candidate in candidates:
        for root in roots:
            if _glob_targets_secret_bounded(candidate, root):
                return root
    return None


# Commands that read a whole subtree, for which naming an ANCESTOR of a
# protected root reads the root. `rg` recurses by default; the rest need a flag.
# Commands that read a whole subtree, for which naming an ANCESTOR of a
# protected root reads the root. The value is the short-flag letters that make
# THAT command recursive; an empty set means it always is. Read per command, not
# as one letter soup: `cp -a` is an archive copy, while `ls -a` merely shows
# dotfiles and `ls -la ~` must stay an ordinary listing.
_RECURSIVE_READ_COMMANDS: dict[str, set[str]] = {
    "find": set(), "tar": set(), "du": set(), "cpio": set(), "pax": set(),
    "7z": set(), "7za": set(), "rg": set(), "ag": set(), "ack": set(),
    "grep": {"r", "R"}, "egrep": {"r", "R"}, "fgrep": {"r", "R"},
    "cp": {"r", "R", "a"}, "rsync": {"r", "R", "a"}, "scp": {"r", "R"},
    "ls": {"R"}, "zip": {"r"}, "tree": set(), "chmod": {"R"}, "chown": {"R"},
}
_RECURSIVE_READ_LONG_FLAGS = ("--recursive", "--archive", "--dereference-recursive")


def _command_reads_recursively(command: str, cmd_tokens: Sequence[str]) -> bool:
    """Whether the command walks a directory tree rather than named files."""
    lowered = command.lower()
    if any(flag in lowered for flag in _RECURSIVE_READ_LONG_FLAGS) or "-d recurse" in lowered:
        return True
    letters: set[str] | None = None
    for tok in cmd_tokens:
        bare = tok.strip().strip("<>();|&\"'`")
        entry = _RECURSIVE_READ_COMMANDS.get(bare.split("/")[-1])
        if entry is not None:
            if not entry:
                return True
            letters = entry
            continue
        if letters and bare.startswith("-") and not bare.startswith("--"):
            if set(bare[1:]) & letters:
                return True
    return False


def _protected_component_in(
    text: str, roots: Sequence[Path], repo_root: Path, left_repo: bool = False
) -> Path | None:
    """The root whose own final path component `text` spells literally, if any.

    `~/.claude.json` and `~/.codex` are distinctive names — `.claude.json`,
    `.codex`, `.met-dsl` — and a token that carries one as a whole path
    component is naming that root, whatever the rest of it expands to.

    A name that ALSO exists inside the repository is not distinctive and is
    skipped: this checkout has its own `.claude/`, so `cat ${PWD}/.claude/settings.json`
    is an ordinary read of that, not of the credential home.
    """
    components: set[str] = set()
    for part in text.split("/"):
        # A component can be glued to the substitution that follows it
        # (`.claude$(echo /)creds.json` arrives as one token).
        for fragment in re.split(r"[$(`)\"' ]", part):
            if fragment:
                components.add(fragment)
    # A token that references HOME (or starts with `~`) is reaching for the home
    # directory whatever the expansion resolves to; without that, an in-repo name
    # is the likelier reading.
    home = str(_home_dir()).rstrip("/")
    home_ward = (
        "HOME" in text
        or text.startswith("~")
        or (home and home in text)
        or left_repo
        or "$(" in text
        or "`" in text
    )
    for root in roots:
        if not root.name or root.name not in components:
            continue
        if (repo_root / root.name).exists() and not home_ward:
            continue
        return root
    return None


def _command_reads_protected_host_path(
    command: str,
    cmd_tokens: list[str],
    repo_root: Path,
    roots: Sequence[Path],
) -> Path | None:
    """The first root of `roots` a Bash command appears to read, else None.

    This guard is NOT gated on the command name (an earlier version only fired
    for cat/head/etc., letting `od`, `xxd`, `cut`, `read X < ...`, and
    `x=$(cat ...)` slip through).  Two complementary checks:
      (1) a raw-command marker regex catching ~ / $HOME / ${HOME…} / <abs-home>
          spellings even when adjacent shell punctuation mangles tokenization;
      (2) per-token path resolution catching `..` traversal and symlinks
          (.resolve() normalizes both) regardless of the leading command, over
          the shell-expansion candidates of each token (parameter expansions and
          same-command variable assignments) and against every `cd` anchor in
          the command as well as repo_root.

    A CREDENTIAL root with any containment relationship to repo_root is dropped —
    one that contains it (a misconfigured `CODEX_HOME` above the checkout would
    make every ordinary in-repo read a credential-home read) and one inside it
    (an in-repo `CODEX_HOME` would fail-close reads of the workspace).
    `_resolve_backend_rw_binds` rejects both directions on the bind side, so
    nothing under such a root is bound writable and there is nothing here to
    protect. The OPERATOR-SECRET root is never dropped: that justification does
    not apply to it (it is not an rw bind at all), so a checkout placed inside or
    around `~/.met-dsl` must keep failing closed rather than lose the guard.
    """
    repo_resolved = repo_root.resolve()
    secret_root = operator_secret_root()
    roots = [
        root
        for root in roots
        if root == secret_root
        or (
            not _is_path_under_root(repo_resolved, root)
            and not _is_path_under_root(root, repo_resolved)
        )
    ]
    if not roots:
        return None
    # Blank the harness's own persisted tool-results before the marker scan: the
    # path lives under `~/.claude`, so the marker matches it, but reading it is
    # what the "Full output saved to …" mechanism tells the agent to do (the Read
    # tool has always permitted it). Three real commands in this repository's
    # hook logs are of this shape.
    command = _blank_persisted_tool_results(command, repo_root)
    marker_res = [(root, _protected_root_marker_regex(root)) for root in roots]
    for root, marker_re in marker_res:
        if marker_re.search(command):
            return root
    # Also test a quote/backslash-collapsed copy of the whole command: shlex
    # normally removes embedded quotes (`~/.met-d''sl`) and escapes (`~/\.met-dsl`),
    # but on a shlex parse failure evaluate_common_policy falls back to
    # command.split(), which does NOT — so collapse them here too (mirrors
    # _command_invokes_dismiss_violation).
    collapsed_cmd = re.sub(r"""['"\\]""", "", command)
    if collapsed_cmd != command:
        for root, marker_re in marker_res:
            if marker_re.search(collapsed_cmd):
                return root
    candidate_tokens = list(cmd_tokens)
    if collapsed_cmd != command:
        candidate_tokens += collapsed_cmd.split()
    # Resolve the assignment map ONCE — `$B` may be `$A` may be `$HOME` — so each
    # token needs only a single substitution pass.
    assignments = _resolved_assignment_map(_local_shell_assignments(candidate_tokens))
    # `$(…)` / backticks are unresolvable for the same reason a nested `${…}` is,
    # and they can put the protected component in a DIFFERENT token from the
    # substitution (`cat $HOME/$(echo .claude.json)` splits at the space), so the
    # signal is the command's, not the token's.
    unresolvable_command = "$(" in command or "`" in command
    recursive_read = _command_reads_recursively(command, cmd_tokens)
    # Quote-collapsing turns prose into tokens: `echo "docs / runtime"` yields a
    # bare `/`, which is an ancestor of every root and blocked a real command
    # from this repository's own logs. A `/` that appears only inside quotes is
    # not a read target.

    work_budget = [_TOTAL_TRANSFORM_OPS_MAX]
    anchors = _cd_anchor_dirs(candidate_tokens, repo_root, assignments, work_budget)
    candidates: list[str] = []
    seen_candidates: set[str] = set()
    # The ancestor rule (a recursive reader handed a parent of a root) applies
    # only to REAL operands: the quote/backslash-collapsed copy splits prose into
    # words (`echo "docs / runtime"` yields a bare `/`), and a `cd`'s own operand
    # is a directory change, not a read (`cd .. && grep -rn foo repo/docs` was
    # resolved against the anchor that same `cd` produced, landing on $HOME).
    # Both fail-closed real commands from this repository's logs.
    operand_candidates: set[str] = set()
    cd_operand_indices = _cd_operand_indices(cmd_tokens)
    for token_index, tok in enumerate(candidate_tokens):
        # Strip shell punctuation that can wrap a path token (redirects,
        # substitution parens, quotes) but keep `$` (expandvars), glob
        # metacharacters `[` `]` `*` `?`, and braces `{` `}` (brace expansion,
        # all handled explicitly below).
        stripped = tok.strip().strip("<>();|&\"'`")
        if not stripped:
            continue
        for spelling in _shell_expansion_variants(stripped, assignments, work_budget):
            # Dedup through a SET: an `in list` test here is a linear scan, and
            # the list is one entry per (token, variant), so a long command made
            # the dedup itself quadratic.
            if spelling and spelling not in seen_candidates:
                seen_candidates.add(spelling)
                candidates.append(spelling)
            if (
                spelling
                and token_index < len(cmd_tokens)
                and token_index not in cd_operand_indices
            ):
                operand_candidates.add(spelling)
    if anchors and len(anchors) * len(candidates) > _RESOLVE_OPS_MAX:
        # Last work bound: every (candidate, base) pair is a `.resolve()` syscall,
        # and both factors are attacker-chosen. Past the budget, keep the anchors
        # with a containment relationship to a root (the ones a `..`-free token
        # can reach) and as much of the TAIL as still fits — the tail because an
        # evader pads AFTER the real `cd`. Residue documented in docs/HOOKS.md.
        keep = max(1, _RESOLVE_OPS_MAX // max(1, len(candidates)))
        related = [
            a
            for a in anchors
            if any(_is_path_under_root(a, root) or _is_path_under_root(root, a) for root in roots)
        ]
        head_keep = max(1, keep // 2)
        ends = [*anchors[:head_keep], *anchors[-(keep - head_keep) :]]
        # BOTH ends: padding `cd`s can precede the decisive one (round 4) or
        # follow it (round 10), and keeping only the tail lost the second case.
        picked: list[Path] = []
        for anchor in [*related, *ends]:
            if anchor not in picked:
                picked.append(anchor)
        anchors = picked[:keep] or anchors[-1:]
    # A `cd` that leaves the checkout makes an in-repo name the UNLIKELY reading:
    # `cd ../.. && cat $(echo .claude)/creds.json` is the credential directory.
    repo_resolved_root = repo_root.resolve()
    left_repo = any(
        not _is_path_under_root(a, repo_resolved_root) for a in anchors
    ) or any(
        ".." in candidate.split("/")
        and not _is_path_under_root(
            Path(os.path.normpath(os.path.join(str(repo_root), candidate))), repo_resolved_root
        )
        for candidate in candidates
    )
    for t in candidates:
        # An expansion this guard could not resolve leaves `${` in the candidate.
        # Emulating bash one syntactic corner at a time is a losing game —
        # nesting depth, arithmetic bases, POSIX classes, the next operator — so
        # stop chasing it: if the token ALSO spells a protected root's own path
        # component literally, the unresolved part is treated as reaching it.
        # That is the whole class (`cat ${A:-${B:-${HOME}}}/.claude.json`,
        # `${A:0x7}/.claude.json`, and whatever syntax comes next), and the false
        # positives it costs are the ones already accepted: a command that names
        # a protected path.
        if (unresolvable_command or "${" in t) and _blank_persisted_tool_results(
            t, repo_root
        ) == t:
            # `$(…)` and backticks are unresolvable for the same reason a nested
            # `${…}` is, and `cat $HOME/$(echo .claude.json)` puts the component
            # inside the substitution body.
            named = _protected_component_in(t, roots, repo_root, left_repo)
            if named is not None:
                return named
        # Brace expansion (`~/.met-{dsl,x}/...`, `{k..m}`, nested) happens in the
        # shell before the path exists; expanduser/glob never see it.  Expand to
        # the cartesian product and test every variant precisely.
        brace_variants = _brace_expand(t)
        if len(brace_variants) > BRACE_EXPAND_MAX_RESULTS:
            # The expander is bounded, and past the bound it returns a TRUNCATED
            # product rather than something still carrying braces — so the
            # fail-closed fallback below never fired and the dropped
            # alternatives were simply unchecked (`cat ~/{x0,…,x256,.codex}/…`).
            # Add the glob form explicitly, which is what that fallback does.
            brace_variants = [*brace_variants, _braces_to_glob(t)]
        for variant in brace_variants:
            # If braces REMAIN (bounded-out >8 groups, or malformed), fall back
            # to the fail-closed `{...}`→`*` glob catch-all for this variant.
            # (Precise variants skip this, so legit `~/.{config,local}` reads are
            # not over-blocked.)
            if "{" in variant:
                _bg = os.path.expanduser(os.path.expandvars(_braces_to_glob(variant)))
                matched = _glob_pattern_reaches_root(_bg, roots, repo_root, anchors)
                if matched is not None:
                    return matched
                continue
            expanded = os.path.expanduser(os.path.expandvars(variant))
            # Glob metacharacters (`*?[`) are expanded by the shell at runtime;
            # a literal .resolve() would keep them and miss the match.  e.g.
            # `cat ~/.met-d*/operator_tokens/x.txt` reads the real token.
            if any(ch in expanded for ch in "*?["):
                matched = _glob_pattern_reaches_root(expanded, roots, repo_root, anchors)
                if matched is not None:
                    return matched
                continue
            # A relative path resolves against repo_root, and against EVERY `cd`
            # anchor the command carries (`cd ~ && cat .claude.json`). An earlier
            # version restricted `..`-free tokens to the anchors related to a
            # root, on the reasoning that such a token cannot leave its base —
            # but `.resolve()` follows symlinks, so it can (`cd /tmp/w && cat
            # link/.claude.json` where `link -> ~`).
            bases: list[Path] = [repo_root] if os.path.isabs(expanded) else [repo_root, *anchors]
            for base in bases:
                # Lexical first, no syscall: `..` is normalized here, and every
                # spelling that reaches a root WITHOUT crossing a symlink is
                # answered for free. `.resolve()` is what makes this loop
                # expensive (it walks every component), and both the token count
                # and the anchor count are attacker-chosen.
                joined = expanded if os.path.isabs(expanded) else os.path.join(str(base), expanded)
                try:
                    lexical = Path(os.path.normpath(joined))
                except (OSError, ValueError):
                    continue
                if _is_persisted_tool_result_shape(repo_root, lexical):
                    # After resolution, not before: `~/.claude` is an rw bind, so
                    # a leaf can plant a symlink at an exempt-shaped path and
                    # read the credential file through it.
                    try:
                        resolved_exempt = lexical.resolve()
                    except (OSError, ValueError, RuntimeError):
                        resolved_exempt = lexical
                    if _is_persisted_tool_result_shape(repo_root, resolved_exempt):
                        continue
                for root in roots:
                    if _is_path_under_root(lexical, root):
                        return root
                    if recursive_read and t in operand_candidates and _is_path_under_root(
                        root, lexical
                    ):
                        # A RECURSIVE reader handed an ANCESTOR reads the root
                        # too: `grep -r sessionKey ~`, `ls -R ~`,
                        # `find ~ -exec cat {} +`, `tar cf - ~`. Containment was
                        # only ever tested one way.
                        return root
                # The remaining way in is a symlink, which needs the path to
                # exist — and a path that is not there leaks nothing, the same
                # rule the read-manifest guard applies to a nonexistent target.
                if not os.path.lexists(lexical):
                    continue
                try:
                    p = lexical.resolve()
                except (OSError, ValueError, RuntimeError):
                    continue
                for root in roots:
                    if _is_path_under_root(p, root):
                        return root
    return None


def _split_top_commas(s: str) -> list[str]:
    """Split on commas that are NOT inside a nested `{...}` group."""
    parts: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in s:
        if ch == "{":
            depth += 1
            cur.append(ch)
        elif ch == "}":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


_BRACE_SEQUENCE_MAX_SPAN = 1024


def _expand_sequence(spec: str) -> list[str] | None:
    """Bash sequence expansion `{a..z}` / `{0..9}` / `{lo..hi..step}` -> list.

    Returns None for anything not a recognized 2- or 3-part sequence, OR for a
    span larger than _BRACE_SEQUENCE_MAX_SPAN — the caller preserves the braces
    in that case so the fail-closed `{...}`→`*` glob fallback fires.  Bounding
    here is essential: a single `{0..100000000}` has only one `{` (so the
    8-group cap does not apply) and would otherwise materialize 100M strings
    (multi-GB, ~13s) inside this synchronous hook before the product-loop cap.
    """
    m = re.fullmatch(r"([A-Za-z0-9]+)\.\.([A-Za-z0-9]+)(?:\.\.(-?\d+))?", spec)
    if not m:
        return None
    a, b, step_s = m.group(1), m.group(2), m.group(3)
    step = abs(int(step_s)) if step_s else 1
    if step == 0:
        step = 1
    if a.isdigit() and b.isdigit():
        lo, hi = int(a), int(b)
    elif len(a) == 1 and len(b) == 1 and a.isalpha() and b.isalpha():
        lo, hi = ord(a), ord(b)
    else:
        return None
    if (abs(hi - lo) // step) + 1 > _BRACE_SEQUENCE_MAX_SPAN:
        return None
    rng = range(lo, hi + 1, step) if lo <= hi else range(lo, hi - 1, -step)
    if a.isdigit():
        return [str(n) for n in rng]
    return [chr(n) for n in rng]


# Bound on brace expansion in this synchronous hook. Exported because a
# caller must be able to tell "fully expanded" from "gave up at the bound"
# — past it the real file is never checked, so the caller has to fall back
# to the `_braces_to_glob` form instead of trusting the (partial) list.
BRACE_EXPAND_MAX_RESULTS = 256
BRACE_EXPAND_MAX_GROUPS = 8


def _brace_expand(s: str) -> list[str]:
    """Bash brace expansion: comma groups `{x,y}`, sequences `{k..m}`, and
    nested groups `{a,{b,c}}` — cartesian product, balanced-brace aware.

    Bounded to avoid exponential blowup in this synchronous PreToolUse hook:
    more than 8 brace groups, or more than 256 results, → stop expanding (the
    `_braces_to_glob` fail-closed fallback in the caller still blocks anything
    that lexically targets the secret root).
    """
    if s.count("{") > BRACE_EXPAND_MAX_GROUPS:
        return [s]
    # Find the first balanced top-level {...} group.
    depth = 0
    start = -1
    for idx, ch in enumerate(s):
        if ch == "{":
            if depth == 0:
                start = idx
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                inner = s[start + 1 : idx]
                pre, post = s[:start], s[idx + 1 :]
                parts = _split_top_commas(inner)
                out: list[str] = []
                if len(parts) == 1:
                    seq = _expand_sequence(parts[0])
                    if seq is None:
                        # Not a comma list and not a recognized/bounded sequence
                        # (e.g. an unparsed step form, an oversized range, or a
                        # literal `{x}` bash would leave alone).  PRESERVE the
                        # braces — do NOT substitute literally — so the caller's
                        # `{`-present `_braces_to_glob` fallback still fires.
                        # (Substituting literally would drop the `{`, skip the
                        # fallback, and let `~/.met-ds{k..m..1}/x` through.)
                        for tail in _brace_expand(post):
                            out.append(pre + "{" + inner + "}" + tail)
                            if len(out) > BRACE_EXPAND_MAX_RESULTS:
                                return out
                        return out
                    options = seq
                else:
                    options = parts
                for opt in options:
                    for sub in _brace_expand(opt):
                        for tail in _brace_expand(post):
                            out.append(pre + sub + tail)
                            if len(out) > BRACE_EXPAND_MAX_RESULTS:
                                return out
                return out
    return [s]


def _braces_to_glob(s: str) -> str:
    """Replace every `{...}` run with `*` (innermost-first, repeatedly).

    Fail-closed catch-all for ANY brace form — comma groups, sequence
    expansion `{k..m}`, and nested braces — without emulating bash exactly.
    e.g. `~/.met-ds{k..m}/x` -> `~/.met-ds*/x`, `~/.{met-{dsl,x},y}/z` -> `~/.*/z`.
    The result is then matched as a glob pattern against the secret root.
    """
    prev = None
    while prev != s:
        prev = s
        s = re.sub(r"\{[^{}]*\}", "*", s)
    return s


def _glob_pattern_targets_root(pattern: str, root: Path) -> bool:
    """True if an absolute glob `pattern` could match a path under `root`.

    Component-wise glob match: each literal component of `root` must be matched
    by the corresponding glob component of `pattern` (e.g. `.met-d*` / `.m?t-dsl`
    / `.[m]et-dsl` all match the literal `.met-dsl`).  Used to fail-closed on
    globbed operator-secret reads even when the file does not yet exist.
    """
    pat = Path(pattern)
    if not pat.is_absolute():
        return False
    pat_parts = pat.parts
    root_parts = root.parts
    if len(pat_parts) < len(root_parts):
        return False
    # The repo's own matcher, not `fnmatch`: fnmatch does not know POSIX classes,
    # so `~/.met-d[[:alpha:]]l/…` and `~/.[[:lower:]]laude.json` slipped past a
    # check that caught their `[a-z]` / `[c]` twins.
    return all(
        _glob_matches_whole(pp, rp) for pp, rp in zip(pat_parts, root_parts)
    )


def _glob_matches_whole(pattern: str, text: str) -> bool:
    """Whether the glob `pattern` matches the WHOLE of `text`."""
    bounds = _glob_match_bounds(pattern, text)
    return bounds is not None and bounds[1] == len(text)


def _glob_targets_secret_bounded(pattern: str, root: Path) -> bool:
    """Real `glob.glob` (catches symlink redirection the lexical check misses),
    BOUNDED to avoid a synchronous-hook DoS.

    A pattern with multiple wildcard path components (`~/*/*/*/x`) makes
    glob.glob recursively scandir the entire $HOME subtree (multi-second hang).
    Such patterns already lexically target the secret root (a `*` at the
    .met-dsl depth fnmatches it) and are caught by `_glob_pattern_targets_root`
    BEFORE this is called — so here we only run glob when at most ONE component
    carries a wildcard, keeping the filesystem walk cheap.
    """
    if sum(1 for comp in pattern.split(os.sep) if any(c in comp for c in "*?[")) > 1:
        return False
    try:
        for match in glob.glob(pattern):
            if _is_path_under_root(Path(match).resolve(), root):
                return True
    except (OSError, ValueError):
        pass
    return False


_DISMISS_VIOLATION_TOKEN = "dismiss-violation"


def _command_invokes_dismiss_violation(command: str, cmd_tokens: list[str]) -> bool:
    """True if a Bash command invokes the operator-only `dismiss-violation`.

    A raw `\\bdismiss-violation\\b` regex is evaded by shell reassembly the
    runtime ultimately sees as one argv token.  This is best-effort hardening
    against the common forms — quote-splitting (`dismiss-vio""lation`),
    backslash-splitting (`dismiss-vi\\olation`), `$VAR`/`${VAR}` indirection,
    and `${VAR//from/to}` pattern substitution (`V=dismiss_violation;
    ${V//_/-}`).  Fully general shell reassembly (command substitution,
    arrays, `eval`, IFS tricks) is undecidable here; the AUTHORITATIVE gate is
    the operator token required by `dismiss_violation` itself.
    """
    if any(tok == _DISMISS_VIOLATION_TOKEN for tok in cmd_tokens):
        return True
    assigns: dict[str, str] = {}
    for m in re.finditer(
        r"(?:^|[;&|]|\s)\s*([A-Za-z_][A-Za-z0-9_]*)=([^\s;&|]+)", command
    ):
        assigns[m.group(1)] = m.group(2)

    # Bash pattern substitution `${NAME//from/to}` (all) / `${NAME/from/to}`
    # (first).  Apply BEFORE the simple `$NAME` pass so the simple regex does
    # not partially consume `${V` and leave `//_/-}` behind.
    def _pat_sub(m: "re.Match[str]") -> str:
        name, flag, frm, to = m.group(1), m.group(2), m.group(3), m.group(4)
        val = assigns.get(name)
        if val is None:
            return m.group(0)
        return val.replace(frm, to) if flag == "//" else val.replace(frm, to, 1)

    resolved = re.sub(
        r"\$\{([A-Za-z_]\w*)(//|/)([^/}]*)/([^}]*)\}", _pat_sub, command
    )

    # Bash case modification `${NAME,,}` (lower-all) / `${NAME^^}` (upper-all) /
    # `${NAME,}` / `${NAME^}` (first char).  Apply BEFORE the simple `$NAME` pass.
    def _case_sub(m: "re.Match[str]") -> str:
        name, op = m.group(1), m.group(2)
        val = assigns.get(name)
        if val is None:
            return m.group(0)
        if op == ",,":
            return val.lower()
        if op == "^^":
            return val.upper()
        if op == ",":
            return val[:1].lower() + val[1:]
        return val[:1].upper() + val[1:]

    resolved = re.sub(
        r"\$\{([A-Za-z_]\w*)(,,|\^\^|,|\^)\}", _case_sub, resolved
    )
    for name, val in assigns.items():
        # A lambda, never `val` as a replacement TEMPLATE. `val` is command text:
        # a `\1` / `\d` / trailing `\` in it makes re.sub raise, and the raise
        # escapes evaluate_common_policy as a `hook entrypoint failure` block on
        # a command that has nothing to do with dismiss-violation — `PAT='\d+'
        # grep -E "$PAT" f` was unrunnable in workflow mode. (Pre-existing;
        # `_shell_expansion_variants` had the same defect and is fixed the same
        # way.)
        resolved = re.sub(
            r"\$\{?" + re.escape(name) + r"\}?", lambda _m, v=val: v, resolved
        )
    # Case-fold the collapsed string so case-mangled spellings still match the
    # (lowercase) dismiss-violation token.
    collapsed = re.sub(r"""['"\\]""", "", resolved).lower()
    return _DISMISS_VIOLATION_TOKEN in collapsed


def _pipe_tail_body_is_safe(body: str) -> bool:
    """Return True only when an inline `-c` body is a read-only stdin parser.

    Allowlist AST validation (replaces the prior substring blocklist, which was
    trivially defeated by `__import__("os").system(...)`, `exec(input())`,
    `__builtins__.__dict__["open"]`, etc.).  Fail-closed on any parse error.

    Reflection-via-string-literal is also blocked: any string CONSTANT containing
    `__` is rejected, because attribute paths embedded in strings (the
    `string.Formatter().get_field("0.__class__.__bases__...")` /
    `operator.attrgetter("__globals__")` family) reach a live object without ever
    producing an ast.Attribute node the walker can see.  Combined with dropping
    `string` from the import allowlist and blocking the Formatter/attrgetter
    names+methods, this closes the format-string RCE.
    """
    try:
        tree = ast.parse(body, mode="exec")
    except (SyntaxError, ValueError):
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in _PIPE_TAIL_ALLOWED_IMPORT_ROOTS:
                    return False
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] not in _PIPE_TAIL_ALLOWED_IMPORT_ROOTS:
                return False
        elif isinstance(node, ast.Attribute):
            # Any dunder attribute (e.g. __class__, __globals__, __dict__,
            # __subclasses__) is an introspection-escape vector.
            if node.attr.startswith("__") and node.attr.endswith("__"):
                return False
            if node.attr in _PIPE_TAIL_DANGEROUS_ATTRS:
                return False
            # DENY-BY-DEFAULT: allowed modules re-export builtins/os/operator as
            # plain attributes (json.codecs.builtins.open, statistics.random._os),
            # so only an explicit leaf-attribute allowlist is sound.
            if node.attr not in _PIPE_TAIL_ALLOWED_ATTRS:
                return False
        elif isinstance(node, ast.Name):
            # Reject dangerous builtins/modules whenever referenced as a bare
            # Name — NOT only as a Call callee.  `e=eval; e(stdin)`,
            # `w=open; w(p,"w")`, `g=getattr; g(obj, name)` alias the builtin
            # through a local, so a Call-callee-only check (the prior bug) let
            # them through.  Every Name node is inspected here.
            if (
                node.id in _PIPE_TAIL_DANGEROUS_NAMES
                or node.id in _PIPE_TAIL_DANGEROUS_CALLS
            ):
                return False
            if node.id.startswith("__") and node.id.endswith("__"):
                return False
        elif isinstance(node, ast.Constant):
            # Reject attribute paths smuggled inside string literals.
            if isinstance(node.value, str) and "__" in node.value:
                return False
    return True


def evaluate_common_policy(hook_input: HookInput) -> HookDecision:
    """Apply backend-agnostic policy checks."""
    if hook_input.event_name not in {
        HookEventName.PRE_COMMAND_EXECUTE,
        HookEventName.PERMISSION_REQUEST,
    }:
        return HookDecision(action=HookDecisionAction.ALLOW)

    command = hook_input.command or _extract_command(hook_input.payload)
    if not command:
        return HookDecision(action=HookDecisionAction.ALLOW)
    lowered = command.lower()
    if "git reset --hard" in lowered:
        return HookDecision(
            action=HookDecisionAction.BLOCK,
            reason="blocked by common hook policy: git reset --hard is forbidden",
            continue_processing=False,
            audit_detail={"policy": "forbid_git_reset_hard", "command": command},
        )
    workflow_mode_raw = os.environ.get("METDSL_WORKFLOW_EXEC_MODE")
    workflow_mode = (workflow_mode_raw or "dev").strip().lower()
    if workflow_mode == "dev":
        forbidden_tokens = (
            "--allow-missing-orchestration",
            "--allow-missing-llm-review",
            "--allow-soft-fail",
            "--allow-soft-verify",
            "--ignore-verify-fail",
            "--force-pass",
        )
        matched = [token for token in forbidden_tokens if token in lowered]
        if matched:
            return HookDecision(
                action=HookDecisionAction.BLOCK,
                reason=(
                    "blocked by common hook policy: dev mode forbids verify bypass flags: "
                    + ", ".join(matched)
                ),
                continue_processing=False,
                audit_detail={
                    "policy": "forbid_verify_bypass_flags_in_dev_mode",
                    "workflow_mode": workflow_mode,
                    "command": command,
                    "matched_tokens": matched,
                },
            )
    workflow_mode_val = os.environ.get("METDSL_WORKFLOW_MODE", "0").strip()
    cli_help_audit: dict[str, Any] | None = None
    if workflow_mode_val == "1":
        bash_read_cmds = frozenset(
            {"cat", "head", "tail", "less", "more", "bat", "pygmentize", "sed", "rg", "grep", "awk"}
        )
        try:
            cmd_tokens = shlex.split(command)
        except ValueError:
            cmd_tokens = command.split()
        lowered_tokens = [tok.lower() for tok in cmd_tokens]
        first_cmd = lowered_tokens[0].split("/")[-1] if lowered_tokens else ""
        repo_root_raw = hook_input.payload.get("repo_root")
        repo_root = (
            Path(repo_root_raw).resolve()
            if isinstance(repo_root_raw, str) and repo_root_raw.strip()
            else Path.cwd()
        )
        # Out-of-repo host paths that must never enter agent context: ~/.met-dsl/
        # (operator-only dismiss-violation tokens) and the backend credential
        # homes the bwrap profile rw-binds (~/.claude, ~/.claude.json, ~/.codex —
        # OAuth credentials + session transcripts).  NOT gated on the command
        # name: any command that reads them (cat, od, xxd, cut, `read X < ...`,
        # `x=$(cat ...)`, ..-traversal, etc.) is blocked.  The Read tool already
        # excludes all of them (allowed_read_roots is repo-relative); this closes
        # the Bash path, which is the only other route.
        met_dsl_root = operator_secret_root()
        matched_root = _command_reads_protected_host_path(
            command, cmd_tokens, repo_root, protected_host_read_roots()
        )
        if matched_root is not None:
            if matched_root == met_dsl_root:
                return HookDecision(
                    action=HookDecisionAction.BLOCK,
                    reason=(
                        "blocked: direct read from ~/.met-dsl/ via Bash is forbidden in "
                        "workflow mode. Operator tokens live there and must not enter "
                        "agent context; dismiss-violation is an operator-only action."
                    ),
                    continue_processing=False,
                    audit_detail={
                        "policy": "forbid_operator_secret_direct_read",
                        "command": command,
                    },
                )
            return HookDecision(
                action=HookDecisionAction.BLOCK,
                reason=(
                    f"blocked: direct read from {matched_root} via Bash is forbidden in "
                    "workflow mode. That path is the backend CLI's credential/session "
                    "home (the sandbox binds it writable so the CLI can refresh its own "
                    "auth); no agent task requires reading it."
                ),
                continue_processing=False,
                audit_detail={
                    "policy": "forbid_backend_credential_direct_read",
                    "command": command,
                    "protected_root": str(matched_root),
                },
            )
        if first_cmd in bash_read_cmds:
            repo_tools_root = (repo_root / "tools").resolve()
            read_targets = _extract_read_targets(first_cmd, cmd_tokens)
            if any(
                _is_path_under_root(_resolve_target_path(repo_root, target), repo_tools_root)
                for target in read_targets
            ):
                return HookDecision(
                    action=HookDecisionAction.BLOCK,
                    reason=(
                        "blocked: direct read from tools/ via Bash is forbidden in workflow mode. "
                        "Derive rules only from docs/, spec/, and skill_must_read_refs artifacts."
                    ),
                    continue_processing=False,
                    audit_detail={"policy": "forbid_tools_direct_read", "command": command},
                )
        cli_help_audit = _detect_cli_help_invocation(cmd_tokens, command)
        if "python" in lowered:
            # Inline Python policy (fail-closed with one narrow exception):
            #
            # DEFAULT: ALL standalone `python3 -c` snippets and `python3 - <<EOF`
            # heredocs are blocked.  Regex-based write detection is fundamentally
            # unreliable — alias bypasses like `from pathlib import Path as P;
            # P('x').write_text(...)` or `Path('x').open('w').write(...)` would
            # slip through any finite pattern set.  Agents that need to run Python
            # should use a real script file (`python3 script.py`), which goes
            # through normal write/read manifest validation.
            #
            # EXCEPTION: `... | python3 -c '...'` pipe-tail read-only invocations
            # are allowed when the `-c` body contains no file-write patterns.
            # Pipe-tail means stdin is consumed from the previous stage (not from
            # a file argument), which substantially limits the attack surface.
            # The `-c` body is scanned for write-API patterns; if none are found
            # the invocation is permitted.  Risk: alias-based write bypasses
            # remain theoretically possible; this is a conscious trade-off
            # accepted by the project operator (see plan pure-wibbling-oasis).
            #
            # Tokenization: shlex puts `-c` and `<<` into separate tokens.
            _py_inline_blocked = False
            _py_inline_reason = ""
            tokens_for_python: list[str] = cmd_tokens
            # Detect `python[3]` invocations specifically (`python` substring
            # in `lowered` is broad — narrow to a token whose basename starts
            # with python).
            has_python_invocation = any(
                tok.split("/")[-1].lower().startswith("python")
                for tok in tokens_for_python
            )
            if has_python_invocation:
                # heredoc form: `python3 - <<EOF` (still detected via regex
                # because heredoc syntax is not a single token) — always blocked.
                if re.search(r"""python3?\s+-\s*<<""", command):
                    _py_inline_blocked = True
                    _py_inline_reason = (
                        "python - <<EOF heredoc inline execution is forbidden in workflow mode"
                    )
                # `-c` form — check for the pipe-tail read-only exception first.
                elif "-c" in tokens_for_python:
                    # --- Pipe-tail read-only exception ---
                    # A `... | python3 -c '...'` invocation where python reads
                    # only from stdin is much lower risk than a standalone `-c`
                    # call.  Allow it only if ALL of the following hold:
                    #   (1) there is exactly ONE python3 -c in the entire command
                    #       (mixed commands like `... | python3 -c '...'; python3
                    #       -c 'open(...)'` are rejected in full — the first
                    #       invocation's pipe-tail status must not whitelist a
                    #       second standalone invocation in the same string), AND
                    #   (2) that single python3 -c is immediately preceded by a
                    #       `|` separator in the command string, AND
                    #   (3) the `-c` body contains no recognized file-write API
                    #       calls (open-for-write, write_text, shutil.copy, etc.)
                    # python[0-9.]* — match ANY interpreter version (python,
                    # python2, python3, python3.11), not just python3.  Otherwise
                    # a benign `... | python3 -c '<safe>'` coexisting with a
                    # second `; python2 -c '<evil>'` would count as 1 and wrongly
                    # qualify for the pipe-tail exception while python2 runs
                    # unguarded.
                    _total_python_c_count = len(
                        re.findall(r"(?:\S*/)?python[0-9.]*\s+-c\b", command)
                    )
                    _is_pipe_tail = (
                        _total_python_c_count == 1
                        and bool(
                            re.search(
                                # (?<!\|)\|(?!\|) — match a SINGLE pipe only.  The
                                # negative lookbehind/lookahead exclude `||` (logical
                                # OR), so `cat x || python3 -c '...'` is NOT treated
                                # as a read-only pipe-tail (it would run python3 even
                                # when the left side fails — not a stdin consumer).
                                # [^|;&\n]* — stop at any shell separator so that
                                # `echo x | cat; python3 -c '...'` (semicoloned
                                # standalone) does NOT match as pipe-tail.
                                # (?:\S*/)? — matches optional path prefix so that
                                # `/usr/bin/python3 -c` is detected the same as
                                # bare `python3 -c`.
                                r"(?<!\|)\|(?!\|)[^|;&\n]*(?:\S*/)?python[0-9.]*\s+-c\b",
                                command,
                            )
                        )
                    )
                    # Extract the inline code body for write-pattern scanning.
                    # Use the -c immediately following python3/python in the token
                    # list, not the first -c overall (which could belong to grep -c
                    # or another command that precedes the pipe).
                    # _c_body_reliable tracks whether we successfully extracted the
                    # actual `-c` body.  If shlex fails to parse (unmatched quote) or
                    # the body token is absent, the write-pattern scan cannot be
                    # trusted — we MUST fail-closed (block) rather than silently
                    # scanning an empty string that matches no write pattern.
                    _c_body = ""
                    _c_body_reliable = False
                    try:
                        import shlex as _shlex_mod
                        _toks_s = _shlex_mod.split(command)
                        _ci = None
                        for _tok_idx in range(len(_toks_s) - 1):
                            # Use basename so that path-qualified interpreters
                            # like /usr/bin/python3 are matched correctly.
                            _base = _toks_s[_tok_idx].split("/")[-1]
                            if (
                                _base in ("python3", "python")
                                and _toks_s[_tok_idx + 1] == "-c"
                            ):
                                _ci = _tok_idx + 1
                                break
                        if _ci is not None and _ci + 1 < len(_toks_s):
                            _c_body = _toks_s[_ci + 1]
                            _c_body_reliable = True
                    except Exception:
                        _c_body = ""
                        _c_body_reliable = False
                    # Validate the body with an allowlist AST check (not a
                    # substring blocklist).  The legitimate pipe-tail use case
                    # (parsing stdin JSON/text) only imports read-only modules
                    # and reads from sys.stdin; anything capable of file I/O,
                    # subprocess, networking, dynamic exec, or attribute-escape
                    # is rejected.  See _pipe_tail_body_is_safe.
                    _c_body_safe = _c_body_reliable and _pipe_tail_body_is_safe(_c_body)
                    if _is_pipe_tail and _c_body_safe:
                        # Pipe-tail + reliably-extracted + AST-allowlisted body
                        # → allow.  Fall through (do not set _py_inline_blocked).
                        pass
                    else:
                        _py_inline_blocked = True
                        if not _is_pipe_tail:
                            _py_inline_reason = (
                                "python -c inline execution is forbidden in workflow mode"
                            )
                        elif not _c_body_reliable:
                            _py_inline_reason = (
                                "python -c pipe-tail body could not be parsed reliably "
                                "(unmatched quote / malformed) — blocked fail-closed"
                            )
                        else:
                            _py_inline_reason = (
                                "python -c pipe-tail body is not a read-only stdin parser "
                                "(disallowed import / call / attribute) — blocked fail-closed"
                            )
            if _py_inline_blocked:
                # Intent classification — uuid / json_read / write (default).
                # The block is unconditional for matched cases, but the recovery
                # hint differs by intent: agents commonly reach for `python -c`
                # to (a) generate a UUID, (b) inspect a JSON file, or (c) write
                # a file.  Pointing them at the canonical alternative eliminates
                # the retry loop.
                intent = "write"
                hint_next = (
                    "Write the artifact with the Edit/Write tool to a path listed in "
                    "output_manifests/<agent_run_id>.json.allowed_file_tool_paths "
                    "(any extension, including managed .json/.txt). Do not write files "
                    "from inline Python."
                )
                if re.search(r"uuid\.uuid[1345]\s*\(", command):
                    # Cover uuid1/uuid3/uuid4/uuid5 — agents typically reach
                    # for uuid4, but uuid1 (host+time) and uuid5 (namespace
                    # SHA-1) also appear. Pattern requires `uuid.<fn>(` so
                    # bare `uuid` strings (e.g. paths/log lines) don't match.
                    intent = "uuid"
                    hint_next = "python3 tools/new_agent_run_id.py"
                elif re.search(r"json\s*\.\s*loads?\s*\(", command):
                    intent = "json_read"
                    hint_next = (
                        "Use the Read tool for the JSON file directly; if Python is "
                        "required, write a script to workspace/tmp/<agent_run_id>/x.py "
                        "and run `python3 workspace/tmp/<agent_run_id>/x.py` "
                        "(literal path, no $TMPDIR env reference needed)."
                    )
                return HookDecision(
                    action=HookDecisionAction.BLOCK,
                    reason=(
                        f"blocked: {_py_inline_reason}. "
                        "Inline Python is fail-closed because regex-based "
                        "filtering cannot reliably catch alias/string-literal "
                        "bypasses (exception: pipe-tail with no write patterns). "
                        "Use a real script file (python3 script.py) "
                        "for execution, or tools/audit_orchestration.py for "
                        "log inspection. "
                        "Write artifacts (any extension) with the Edit/Write tool to a "
                        "path in allowed_file_tool_paths. "
                        "See docs/RUNBOOK.md#hook-recovery."
                    ),
                    continue_processing=False,
                    audit_detail={
                        "policy": "forbid_python_inline_write",
                        "command": command,
                        "intent_detected": intent,
                        "fix_hint": {
                            "next_command": hint_next,
                            "docs_ref": "docs/RUNBOOK.md#hook-recovery",
                        },
                    },
                )
        # Block dismiss-violation in all workflow sessions regardless of how the
        # runtime is invoked (script path, -m module, or wrapper) and regardless
        # of shell reassembly (quote/backslash splitting, variable indirection).
        # An agent cannot bypass this by using METDSL_WORKFLOW_MODE=0 prefix
        # because the hook reads its OWN os.environ (set by run_workflow.py at
        # session start), not the subprocess env override.
        if _command_invokes_dismiss_violation(command, cmd_tokens):
            return HookDecision(
                action=HookDecisionAction.BLOCK,
                reason=(
                    "blocked: dismiss-violation is an operator-only command and "
                    "cannot be invoked from within a running workflow session. "
                    "Run it from the operator terminal outside the workflow."
                ),
                continue_processing=False,
                audit_detail={
                    "policy": "forbid_dismiss_violation_in_workflow",
                    "command": command,
                },
            )
        # Block any Bash command that touches /dev/shm in workflow mode.
        # We scan EVERY token of the entire command — not just positional args
        # of the first command — to defeat bypasses via shell control tokens
        # (`cd . && cp ... /dev/shm/x`), wrapper commands (`env cp ...`,
        # `bash -c '...'`), option-arg forms (`install -t /dev/shm`), and
        # long-form options (`cp --target-directory=/dev/shm ...`). The policy
        # is intentionally strict: workflow mode never legitimately needs
        # /dev/shm, since a per-agent $TMPDIR (workspace/tmp/<agent_run_id>/)
        # is provided.
        offending = _find_dev_shm_token(command, cmd_tokens)
        if offending is not None:
            return HookDecision(
                action=HookDecisionAction.BLOCK,
                reason=(
                    f"blocked: command touches {offending!r} which is forbidden. "
                    "/dev/shm reads/writes are not permitted; write under the literal "
                    "allowed_tmp_root path (workspace/tmp/<agent_run_id>/) for temporary files. "
                    "See docs/AGENT_CONTRACT.md."
                ),
                continue_processing=False,
                audit_detail={
                    "policy": "output_manifest_write_guard",
                    "command": command,
                    "destination": offending,
                    "fix_hint": {
                        "write_under": "workspace/tmp/<agent_run_id>/...",
                        "docs_ref": "docs/AGENT_CONTRACT.md",
                    },
                },
            )
    if cli_help_audit is not None:
        return HookDecision(action=HookDecisionAction.ALLOW, audit_detail=cli_help_audit)
    return HookDecision(action=HookDecisionAction.ALLOW)


def _detect_cli_help_invocation(
    cmd_tokens: list[str], command: str
) -> dict[str, Any] | None:
    """Detect `python[3] tools/<name>.py [<sub>] --help` invocations.

    Returns audit_detail dict for ALLOW logging, or None if not a help call.
    `--help` against tools/ is permitted (argparse output only, not implementation
    read). Logging frequency informs Tier-A/Tier-B doc split decisions.
    """
    if "--help" not in cmd_tokens and "-h" not in cmd_tokens:
        return None
    py_idx = next(
        (
            i
            for i, tok in enumerate(cmd_tokens)
            if tok.split("/")[-1].lower().startswith("python")
        ),
        -1,
    )
    if py_idx < 0:
        return None
    script_idx = py_idx + 1
    while script_idx < len(cmd_tokens) and cmd_tokens[script_idx].startswith("-"):
        script_idx += 1
    if script_idx >= len(cmd_tokens):
        return None
    script = cmd_tokens[script_idx]
    if not script.startswith("tools/") or not script.endswith(".py"):
        return None
    subcommand: str | None = None
    if script_idx + 1 < len(cmd_tokens):
        candidate = cmd_tokens[script_idx + 1]
        if not candidate.startswith("-"):
            subcommand = candidate
    return {
        "policy": "cli_help_invocation_observed",
        "tool": script,
        "subcommand": subcommand,
        "command": command,
    }


_DEV_SHM_PATH_ACCESS_CMDS: frozenset[str] = frozenset({
    # Commands that take filesystem path arguments and would directly access
    # `/dev/shm` if one is passed.  Search/text commands (grep, rg, awk, sed,
    # echo) are intentionally excluded — `grep '/dev/shm' file.log` is a
    # legitimate diagnostic that does not touch /dev/shm.
    "cp", "mv", "rsync", "install", "dd", "tee", "cat", "ln",
    "ls", "stat", "rm", "mkdir", "rmdir", "touch", "truncate",
    # Archive/search/traversal commands that read or write paths.
    "tar", "zip", "unzip", "gzip", "gunzip", "bzip2", "xz", "7z",
    "find", "fd", "du", "df",
    # Interpreters that can be coaxed into accessing arbitrary paths via
    # script arguments — bare `/dev/shm` here means "the interpreter is
    # invoked with /dev/shm as a script/cwd/argv element".  Inline -c
    # snippets (python3 -c "open('/dev/shm/...')") are caught by the
    # fail-closed inline-execution policy below, not here.
    "python", "python3", "perl", "ruby", "node", "lua", "php",
})

_DEV_SHM_WRAPPER_CMDS: frozenset[str] = frozenset({
    "env", "sudo", "nice", "ionice", "stdbuf", "time", "exec",
})

_DEV_SHM_SHELL_CONTROL: frozenset[str] = frozenset({"&&", "||", ";", "|"})


def _find_dev_shm_token(command: str, cmd_tokens: list[str]) -> str | None:
    """Scan a Bash command for any token that touches /dev/shm.

    Strategy:
    - Tokens with an explicit path suffix (`/dev/shm/foo`) are unambiguously
      filesystem references and ALWAYS flagged.
    - Bare tokens (`/dev/shm`) and option-arg destinations are only flagged
      when the surrounding command is a path-access command (cp/mv/rsync/etc.)
      — otherwise `grep '/dev/shm' file` and `echo /dev/shm` would over-block.
    - Quoted shell snippets (`bash -c "..."`) are re-tokenized recursively.
    """
    def _check_token_with_suffix(tok: str) -> str | None:
        """Match `/dev/shm/<...>` (explicit path), `option=/dev/shm[/...]`,
        or shell-redirection-prefixed forms like `>/dev/shm/x`,
        `</dev/shm/x`, `>>/dev/shm/x`, `1>/dev/shm/x`, `&>/dev/shm/x`.

        `shlex.split()` keeps the redirection operator glued to the path
        when there is no whitespace (`echo hi >/dev/shm/x` →
        `['echo', 'hi', '>/dev/shm/x']`); without this branch the redirect
        bypasses the path-suffix check.
        """
        if tok.startswith("/dev/shm/"):
            return tok
        eq_idx = tok.find("=")
        if eq_idx >= 0:
            after = tok[eq_idx + 1 :]
            if after == "/dev/shm" or after.startswith("/dev/shm/"):
                return tok
        # Shell redirection-prefixed forms.  The redirection operator is one
        # of: `>`, `>>`, `<`, `<<`, `<<<`, `&>`, `&>>`, optionally preceded
        # by a single fd digit (`1>`, `2>>`, `3<`, ...).
        # Strip the operator+optional-digit and re-check.
        for prefix_len in range(1, 5):
            if len(tok) <= prefix_len:
                continue
            head = tok[:prefix_len]
            tail = tok[prefix_len:]
            # Pattern: optional fd digit, then redirection operator
            if not head:
                continue
            i = 0
            if i < len(head) and head[i].isdigit():
                i += 1
            op = head[i:]
            if op in (">", ">>", "<", "<<", "<<<", "&>", "&>>"):
                if tail == "/dev/shm" or tail.startswith("/dev/shm/"):
                    return tok
        return None

    def _is_bare_dev_shm(tok: str) -> bool:
        return tok == "/dev/shm"

    def _split_segments(tokens: list[str]) -> list[list[str]]:
        segments: list[list[str]] = []
        current: list[str] = []
        for t in tokens:
            if t in _DEV_SHM_SHELL_CONTROL:
                if current:
                    segments.append(current)
                current = []
            else:
                current.append(t)
        if current:
            segments.append(current)
        return segments

    def _segment_cmd_args(segment: list[str]) -> tuple[str, list[str]]:
        """Strip leading wrappers (env, sudo, ...) and env-VAR=value pairs.

        Returns (basename(cmd_lower), remaining_args).
        """
        i = 0
        # Skip wrapper commands and their VAR=value arguments
        while i < len(segment) and segment[i].lower() in _DEV_SHM_WRAPPER_CMDS:
            i += 1
            while (
                i < len(segment)
                and "=" in segment[i]
                and not segment[i].startswith("-")
                and "/" not in segment[i].split("=", 1)[0]
            ):
                i += 1
        if i >= len(segment):
            return ("", [])
        cmd = segment[i].split("/")[-1].lower()
        return (cmd, segment[i + 1 :])

    # Pass 1: explicit path-suffix or option=value forms — always flag.
    for tok in cmd_tokens:
        hit = _check_token_with_suffix(tok)
        if hit is not None:
            return hit

    # Pass 2: bare `/dev/shm` in path-access command segments.
    for seg in _split_segments(cmd_tokens):
        cmd, args = _segment_cmd_args(seg)
        if cmd in _DEV_SHM_PATH_ACCESS_CMDS:
            for tok in args:
                if _is_bare_dev_shm(tok):
                    return tok

    # Pass 3: re-tokenize quoted shell snippets (e.g. `bash -c "..."`).
    for tok in cmd_tokens:
        if " " not in tok and "\t" not in tok and "\n" not in tok:
            continue
        try:
            inner = shlex.split(tok)
        except ValueError:
            continue
        for itok in inner:
            hit = _check_token_with_suffix(itok)
            if hit is not None:
                return hit
        for inner_seg in _split_segments(inner):
            cmd, args = _segment_cmd_args(inner_seg)
            if cmd in _DEV_SHM_PATH_ACCESS_CMDS:
                for itok in args:
                    if _is_bare_dev_shm(itok):
                        return itok

    return None


def _resolve_target_path(repo_root: Path, path_token: str) -> Path:
    raw = path_token.strip()
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate.resolve()
    return (repo_root / candidate).resolve()


def _resolve_manifest_root(repo_root: Path, root_token: str) -> Path:
    raw = root_token.strip()
    if not raw:
        return repo_root
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate.resolve()
    return (repo_root / candidate).resolve()


def _normalize_rel_posix(path_token: str) -> str:
    """Normalize repo-relative path into stable POSIX token."""
    token = path_token.strip().replace("\\", "/").lstrip("/")
    while "//" in token:
        token = token.replace("//", "/")
    return token.rstrip("/")


# Extensionless filenames permitted under a directory allowlist entry.
# Build-control names (makefile, gnumakefile) are intentionally excluded — they must be
# declared as explicit file pins to prevent undeclared command-execution injection.
_ALLOWED_EXTENSIONLESS_BYPRODUCT_NAMES: frozenset[str] = frozenset({
    "readme", "license", "changelog", "authors", "install", "notice", "copying",
})

# True compiler byproducts — created directly by the compiler as subprocess output.
# Terminal validation accepts these under a directory allowlist as confined build output.
# (NOTE: the legacy "gate provenance / gate_changed_paths" terminal model is gone —
# Phase-2 authorizes step/substep writes by write_roots-containment of the FS-diff.
# See docs/ORCHESTRATION.md.)
_COMPILER_BYPRODUCT_EXTENSIONS: frozenset[str] = frozenset({".mod", ".o", ".a"})

# Allowlist of extensions permitted under a directory allowlist entry via the
# Edit/Write file tools. Restricted to source code only.
#
# Excluded (must use explicit file pins):
#   - Build control files (.mk, .cmake, .toml, .cfg, .ini, .nml) — can alter downstream
#     build behaviour or inject arbitrary commands via CMakeLists.txt / Makefile fragments.
#   - Structured data/documents (.json, .yaml, .xml, .csv, .md, .txt, etc.) — undeclared
#     data injection is unauditable and can poison downstream steps.
#   - Compiler byproducts (.mod, .o, .a) — created directly by the compiler as subprocess
#     output, never via Edit/Write. File-tool writes of these extensions are blocked here;
#     terminal validation also rejects them unless they land under the step's write_roots —
#     agents must clean up build artefacts before record-agent-run.
#
# Extensionless files are gated by _ALLOWED_EXTENSIONLESS_BYPRODUCT_NAMES.
# Everything else is rejected (fail-closed).
_ALLOWED_BYPRODUCT_EXTENSIONS: frozenset[str] = frozenset({
    # Fortran source — primary intended output of the generate step
    ".f90", ".f", ".f95", ".f03", ".f08", ".fpp",
    # C/C++ source — primary intended output of the generate step
    ".c", ".h", ".cpp", ".hpp", ".cc", ".hh", ".cxx", ".inc",
})


def _is_path_under_root(target: Path, root: Path) -> bool:
    target_s = str(target)
    root_s = str(root)
    return target_s == root_s or target_s.startswith(root_s.rstrip("/") + "/")


def _is_self_agent_manifest_read_path(
    repo_root: Path,
    orchestration_id: str,
    agent_run_id: str,
    file_path: str,
) -> bool:
    """Allow a Read of the relevant child's output / read manifest JSON even outside run-gate."""
    orch = orchestration_id.strip()
    rid = agent_run_id.strip()
    if not orch or not rid:
        return False
    abs_target = _resolve_target_path(repo_root, file_path)
    try:
        rel = abs_target.relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return False
    rel_norm = _normalize_rel_posix(rel)
    out_rel = _normalize_rel_posix(f"workspace/orchestrations/{orch}/output_manifests/{rid}.json")
    read_rel = _normalize_rel_posix(f"workspace/orchestrations/{orch}/read_manifests/{rid}.json")
    return rel_norm == out_rel or rel_norm == read_rel


@dataclass(frozen=True)
class _CliManagedPath:
    pattern: re.Pattern[str]
    cli_hint: str


_CLI_MANAGED_PATHS: list[_CliManagedPath] = [
    _CliManagedPath(
        re.compile(r"workspace/orchestrations/[^/]+/launches/[^/]+\.(?:response\.json|reply\.txt|prompt\.txt|request\.json)$"),
        "python3 tools/orchestration_runtime.py record-launch ...",
    ),
    # Separate entry, not a widening of the one above: no subcommand produces these. The
    # conductor writes them itself, BEFORE the call, as the evidence of what it sent — so
    # "re-run record-launch" would be the wrong remedy to print.
    _CliManagedPath(
        re.compile(r"workspace/orchestrations/[^/]+/launches/[^/]+\.(?:request|agent_run)\.input\.json$"),
        "nothing — these are the payload files the conductor writes for itself before "
        "record-launch / finalize-child, kept as the evidence of what was sent. "
        "They are never authored or edited by an agent",
    ),
    _CliManagedPath(
        re.compile(r"workspace/orchestrations/[^/]+/agent_runs\.jsonl$"),
        "python3 tools/orchestration_runtime.py record-agent-run ...",
    ),
    _CliManagedPath(
        re.compile(r"workspace/orchestrations/[^/]+/step_results/[^/]+\.json$"),
        "python3 tools/orchestration_runtime.py write-step-result ...",
    ),
    _CliManagedPath(
        re.compile(r"workspace/orchestrations/[^/]+/orchestration_meta\.json$"),
        "python3 tools/orchestration_runtime.py init-orchestration / run_workflow.py (auto-generated)",
    ),
    _CliManagedPath(
        re.compile(r"workspace/orchestrations/[^/]+/(?:output|read)_manifests/[^/]+\.json$"),
        "python3 tools/orchestration_runtime.py record-launch (manifests are auto-generated)",
    ),
    _CliManagedPath(
        re.compile(r"workspace/orchestrations/[^/]+/preflight\.json$"),
        "python3 tools/run_workflow.py ... (preflight is auto-generated)",
    ),
    _CliManagedPath(
        re.compile(r"workspace/orchestrations/[^/]+/capabilities/[^/]+\.json$"),
        "python3 tools/orchestration_runtime.py record-launch (capability is auto-generated)",
    ),
    _CliManagedPath(
        re.compile(r"workspace/orchestrations/[^/]+/orchestration_checkpoint\.json$"),
        "python3 tools/orchestration_runtime.py write-step-result (checkpoint is auto-updated)",
    ),
    _CliManagedPath(
        re.compile(r"workspace/orchestrations/[^/]+/phase_state\.json$"),
        "python3 tools/orchestration_runtime.py (phase_state is managed by the runtime)",
    ),
]


def check_cli_managed_path(repo_root: Path, file_path: str) -> "HookDecision | None":
    """Return a BLOCK HookDecision if it matches a CLI-managed path. None on no match."""
    abs_target = _resolve_target_path(repo_root, file_path)
    try:
        rel = abs_target.relative_to(repo_root).as_posix()
    except ValueError:
        rel = file_path
    for entry in _CLI_MANAGED_PATHS:
        if entry.pattern.search(rel):
            return HookDecision(
                action=HookDecisionAction.BLOCK,
                reason=(
                    f"Direct write to CLI-managed path is forbidden: {rel!r}\n"
                    f"Use: {entry.cli_hint}"
                ),
                continue_processing=False,
                audit_detail={"policy": "cli_managed_path", "path": rel, "cli_hint": entry.cli_hint},
            )
    return None


def _detect_tmpdir_fallback_or_hardcode(bash_command: str | None) -> bool:
    """Heuristic: did the agent use TMPDIR fallback syntax or hardcoded /tmp paths?

    Triggers when the offending Bash command contains either:
      - "${TMPDIR:-..." or "$TMPDIR:-..." parameter-default expansion (the agent
        wrote a fallback inline instead of using the literal allowed_tmp_root path)
      - hardcoded "/tmp/" or "/dev/shm/" path inside a redirect/heredoc target
    Both indicate the agent should switch to the literal `workspace/tmp/<agent_run_id>/`
    path declared in the manifest's `allowed_tmp_root`.
    """
    if not bash_command:
        return False
    if "${TMPDIR:-" in bash_command or "$TMPDIR:-" in bash_command:
        return True
    if "/tmp/" in bash_command or "/dev/shm/" in bash_command:
        return True
    return False


def validate_write_access(
    repo_root: Path,
    orchestration_id: str,
    agent_run_id: str,
    file_path: str,
    tool_name: str | None = None,
    bash_command: str | None = None,
) -> HookDecision:
    """Verify the write/edit target against the output manifest's allowed_output_paths."""
    manifest_path = (
        repo_root
        / "workspace"
        / "orchestrations"
        / orchestration_id
        / "output_manifests"
        / f"{agent_run_id}.json"
    )
    if not manifest_path.exists():
        return HookDecision(
            action=HookDecisionAction.BLOCK,
            reason=(
                f"output manifest not found for agent_run_id={agent_run_id!r}. "
                f"{MANIFEST_HINT}"
            ),
            continue_processing=False,
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return HookDecision(
            action=HookDecisionAction.BLOCK,
            reason=(
                f"output manifest is unreadable or invalid JSON for agent_run_id={agent_run_id!r}. "
                f"{MANIFEST_HINT}"
            ),
            continue_processing=False,
        )
    if not isinstance(manifest, dict):
        return HookDecision(
            action=HookDecisionAction.BLOCK,
            reason=(
                f"output manifest must be a JSON object for agent_run_id={agent_run_id!r}. "
                f"{MANIFEST_HINT}"
            ),
            continue_processing=False,
        )
    abs_target = _resolve_target_path(repo_root, file_path)
    try:
        rel_target = abs_target.relative_to(repo_root).as_posix()
    except ValueError:
        rel_target = str(abs_target).replace("\\", "/")
    rel_target_norm = _normalize_rel_posix(rel_target)

    tmp_root = manifest.get("allowed_tmp_root", "")
    if isinstance(tmp_root, str) and tmp_root.strip():
        tmp_norm = _normalize_rel_posix(tmp_root.strip())
        tmp_prefix = tmp_norm + "/"
        if rel_target_norm == tmp_norm or rel_target_norm.startswith(tmp_prefix):
            return HookDecision(action=HookDecisionAction.ALLOW)

    allowed_file_tool_paths_obj = manifest.get("allowed_file_tool_paths")
    if not isinstance(allowed_file_tool_paths_obj, list):
        return HookDecision(
            action=HookDecisionAction.BLOCK,
            reason=(
                f"output manifest missing allowed_file_tool_paths list for agent_run_id={agent_run_id!r}. "
                f"{MANIFEST_HINT}"
            ),
            continue_processing=False,
        )
    allowed_file_tool_paths: set[str] = set()
    for item in allowed_file_tool_paths_obj:
        if not isinstance(item, str):
            return HookDecision(
                action=HookDecisionAction.BLOCK,
                reason=(
                    "output manifest allowed_file_tool_paths must contain only strings "
                    f"for agent_run_id={agent_run_id!r}. {MANIFEST_HINT}"
                ),
                continue_processing=False,
            )
        token = _normalize_rel_posix(item)
        if token:
            allowed_file_tool_paths.add(token)

    allowed_paths_obj = manifest.get("allowed_output_paths")
    if not isinstance(allowed_paths_obj, list):
        return HookDecision(
            action=HookDecisionAction.BLOCK,
            reason=(
                f"output manifest missing allowed_output_paths list for agent_run_id={agent_run_id!r}. "
                f"{MANIFEST_HINT}"
            ),
            continue_processing=False,
        )
    allowed_paths = [str(item).strip() for item in allowed_paths_obj if isinstance(item, str) and item.strip()]
    # Directory allowlist entries end with '/'; file entries are exact-match only.
    normalized_allowed_files: set[str] = set()
    normalized_allowed_dirs: list[str] = []
    for p in allowed_paths:
        norm = _normalize_rel_posix(p)
        if p.endswith("/"):
            normalized_allowed_dirs.append(norm)
        else:
            normalized_allowed_files.add(norm)
    path_is_allowed = rel_target_norm in normalized_allowed_files
    if not path_is_allowed and normalized_allowed_dirs:
        under_dir = any(
            rel_target_norm == d or rel_target_norm.startswith(d + "/")
            for d in normalized_allowed_dirs
        )
        if under_dir:
            ext = os.path.splitext(rel_target_norm)[1].lower()
            if ext in _ALLOWED_BYPRODUCT_EXTENSIONS:
                path_is_allowed = True
            elif ext == "" and os.path.basename(rel_target_norm).lower() in _ALLOWED_EXTENSIONLESS_BYPRODUCT_NAMES:
                path_is_allowed = True
    if not path_is_allowed:
        allowed_tmp_root_value = manifest.get("allowed_tmp_root", "")
        tmp_root_str = (
            allowed_tmp_root_value.strip()
            if isinstance(allowed_tmp_root_value, str) and allowed_tmp_root_value.strip()
            else f"workspace/tmp/{agent_run_id}"
        )
        used_fallback_or_hardcode = _detect_tmpdir_fallback_or_hardcode(bash_command)
        fix_hint_block: dict[str, Any] = {
            # Recommend the literal allowed_tmp_root path. Do NOT recommend `export TMPDIR=...`
            # or `jq -er ...` — those Bash patterns trigger Claude Code session sandbox approval
            # prompts that can stall the workflow indefinitely. The hook only checks whether the
            # write target sits under allowed_tmp_root and ignores $TMPDIR env, so a literal
            # path works without any shell variable setup.
            "write_under": f"{tmp_root_str}/...",
            "docs_ref": "docs/AGENT_CONTRACT.md",
            "note": (
                "Write under the literal allowed_tmp_root path "
                f"({tmp_root_str}/...). Do not use `export TMPDIR=...`, `jq -er ...`, "
                "`${TMPDIR:-fallback}` syntax, or hardcoded /tmp//dev/shm paths."
            ),
        }
        if used_fallback_or_hardcode:
            fix_hint_block["tmpdir_fallback_or_hardcode"] = True
            fix_hint_block["canonical_doc"] = (
                "docs/AGENT_CONTRACT.md"
            )
        return HookDecision(
            action=HookDecisionAction.BLOCK,
            reason=(
                f"unauthorized write: {file_path!r} is not in output_manifest allowed_output_paths "
                f"(agent_run_id={agent_run_id!r}). {WRITE_HINT}"
            ),
            continue_processing=False,
            audit_detail={
                "policy": "output_manifest_write_guard",
                "file_path": file_path,
                "agent_run_id": agent_run_id,
                "allowed_output_paths": allowed_paths,
                "allowed_tmp_root": manifest.get("allowed_tmp_root", ""),
                "fix_hint": fix_hint_block,
            },
        )
    # Phase-2: shell writes (Bash redirect `cat > path` / `tee` / `sed -i`) are
    # NEVER an authorized artifact-write path — not even when the target is in
    # `allowed_file_tool_paths`. Managed artifacts are written with the structured
    # file-edit tools (Edit / Write, or `apply_patch` on the Codex backend), which
    # are auditable; Bash writes are confined to `allowed_tmp_root` (the tmp check
    # above already ALLOWed those, so any Bash target reaching here is non-tmp).
    # Blocking it regardless of `allowed_file_tool_paths` membership is what keeps a
    # managed output — now Edit/Write-eligible under the direct-write contract —
    # from ALSO silently authorizing shell writes (e.g. `cat > lineage.json`, or a
    # command-substitution exfil like `echo $(cat secret) > out.json`) to a
    # canonical path. The leaf must use the Edit/Write tool instead.
    if tool_name == "Bash":
        return HookDecision(
            action=HookDecisionAction.BLOCK,
            reason=(
                "shell writes (redirect / tee / sed -i) are forbidden for managed "
                "artifacts. Write the artifact with the Edit/Write tool to a path in "
                "output_manifest allowed_file_tool_paths; Bash may only write scratch "
                f"under allowed_tmp_root (workspace/tmp/{agent_run_id}/...)."
            ),
            continue_processing=False,
            audit_detail={
                # `forbid_unauthorized_file_write` is the stable forensic identifier for
                # the whole "a direct artifact write was rejected — use the Edit/Write
                # tool" class (docs/RUNBOOK.md#hook-recovery and the audit-claude SKILL
                # key on it). The id is forensic-only and never surfaced to the leaf
                # (only `reason` + `fix_hint` are).
                "policy": "forbid_unauthorized_file_write",
                "tool_name": tool_name,
                "file_path": file_path,
                "agent_run_id": agent_run_id,
                "allowed_file_tool_paths": list(allowed_file_tool_paths),
                "fix_hint": {
                    "write_under": f"workspace/tmp/{agent_run_id}/...",
                    "docs_ref": "docs/AGENT_CONTRACT.md",
                    "note": (
                        "Write managed artifacts with the Edit/Write tool (not a shell "
                        "redirect / tee / sed -i). Do NOT use `export TMPDIR=...` or "
                        "$TMPDIR env (session approval would stall)."
                    ),
                },
            },
        )
    if tool_name in {"Edit", "Write", "apply_patch"} and rel_target_norm not in allowed_file_tool_paths:
        # The target is a declared output (it passed the allowed_output_paths check
        # above) but is not Edit/Write-eligible — i.e. it is excluded from
        # `allowed_file_tool_paths` (a canonical MCP audit log, or a path the
        # orchestration did not declare as a file-tool output). The recovery is to
        # add the path to `allowed_file_tool_paths` (the orchestration's launch
        # request), not a shell write.
        return HookDecision(
            action=HookDecisionAction.BLOCK,
            reason=(
                f"direct write via {tool_name} is forbidden for this target path: it is "
                "not in output_manifest allowed_file_tool_paths (e.g. an MCP-owned audit "
                "log written only by the build-runtime MCP server). Write only paths "
                "enumerated in allowed_file_tool_paths."
            ),
            continue_processing=False,
            audit_detail={
                "policy": "forbid_unauthorized_file_write",
                "tool_name": tool_name,
                "file_path": file_path,
                "agent_run_id": agent_run_id,
                "allowed_file_tool_paths": list(allowed_file_tool_paths),
                "fix_hint": {
                    "docs_ref": "docs/AGENT_CONTRACT.md",
                    "note": (
                        "The path must be listed in output_manifest allowed_file_tool_paths "
                        "to be written with the Edit/Write tool."
                    ),
                },
            },
        )
    return HookDecision(action=HookDecisionAction.ALLOW)


def _is_persisted_tool_result_read(
    repo_root: Path,
    agent_role: str | None,
    agent_run_id: str,
    file_path: str,
    session_id: str | None = None,
) -> bool:
    """True iff file_path is a persisted Claude Code tool-result this agent may read.

    Matches: ~/.claude/projects/<repo-slug>/<session_dir>/tool-results/<id>.txt
    The session_dir component must equal either `agent_run_id` or `session_id`.
    Two IDs are checked because:
    - Claude Code backend records agent_run_id as agent_session_id (see
      docs/ORCHESTRATION.md), so tool-results may be stored under agent_run_id.
    - The hook payload's session_id is the live Claude Code session identifier
      actually used to name the directory; pass it to cover cases where the two
      differ.
    This prevents reads from a different agent's or previous session's directory.
    Applies to all agent roles — all can receive <persisted-output> wrappers.
    """
    valid_session_dirs: set[str] = {s for s in (agent_run_id, session_id) if s}
    if not valid_session_dirs:
        return False
    try:
        abs_target = _absolute_lexical(repo_root, file_path)
        repo_root_abs = _absolute_lexical(repo_root, str(repo_root))
    except (OSError, ValueError):
        return False
    if not _path_has_no_symlink_redirect(abs_target):
        return False
    try:
        home_abs = Path.home()
        slug = _claude_project_slug(repo_root_abs)
        project_root = home_abs / _AUTO_READ_PROJECT_TOOL_RESULTS_PARENT_TAIL / slug
        if (
            abs_target.name.endswith(".txt")
            and abs_target.parent.name == _AUTO_READ_PROJECT_TOOL_RESULTS_DIR_COMPONENT
        ):
            rel = abs_target.relative_to(project_root)
            parts = rel.parts
            # parts = (session_dir, "tool-results", filename)
            if (
                len(parts) == 3
                and parts[1] == _AUTO_READ_PROJECT_TOOL_RESULTS_DIR_COMPONENT
                and parts[0] in valid_session_dirs
            ):
                return True
    except (OSError, RuntimeError, ValueError):
        pass
    return False


def _is_auto_read_tolerated(
    repo_root: Path,
    agent_role: str | None,
    file_path: str,
) -> bool:
    """Return True if it is a Claude Code auto-read target.

    Two categories of tolerated auto-reads are recognised:

    1. Harness-mandatory auto-reads (all agent roles):
       Files the Claude Code harness reads at startup regardless of agent
       role (MCP discovery, settings parsing). Apply to orchestration AND
       step/substep agents. Path must lexically match
       `_HARNESS_AUTO_READ_TOLERATED_REPO_RELPATHS` or have a component-aligned
       prefix from `_HARNESS_AUTO_READ_TOLERATED_REPO_PREFIXES`.

    2. Orchestration-only auto-reads:
       Project state files (MEMORY/README/TODO/CLAUDE) that orchestration
       agent reads during MCP discovery. Apply only when agent_role ==
       "orchestration". Path either lexically matches
       `_AUTO_READ_TOLERATED_REPO_RELPATHS`, or is the project-memory file
       under <home>/.claude/projects/<repo-slug>/memory/MEMORY.md.

    Security invariants for both categories:
    - The requested path itself must NOT traverse any symlink component
      (lstat-based check) to prevent tolerance from being redirected to
      arbitrary host files via symlink swap.
    - Path comparison is done lexically (no .resolve()), so an attacker
      cannot bypass via filesystem symlinks pointing at a tolerated path.
    """
    try:
        abs_target = _absolute_lexical(repo_root, file_path)
        repo_root_abs = _absolute_lexical(repo_root, str(repo_root))
    except (OSError, ValueError):
        return False

    if not _path_has_no_symlink_redirect(abs_target):
        return False

    try:
        rel = abs_target.relative_to(repo_root_abs)
    except ValueError:
        rel = None
    rel_posix = rel.as_posix() if rel is not None else None

    # Category 1: harness-mandatory auto-read (all roles).
    if rel_posix is not None:
        if rel_posix in _HARNESS_AUTO_READ_TOLERATED_REPO_RELPATHS:
            return True
        for prefix in _HARNESS_AUTO_READ_TOLERATED_REPO_PREFIXES:
            # Prefix must end with "/" so match is component-aligned and
            # cannot extend across path segments (no suffix bypass).
            if prefix.endswith("/") and rel_posix.startswith(prefix):
                return True

    # Category 2a: persisted tool-results (all agent roles) are handled upstream
    # in validate_read_access via _is_persisted_tool_result_read, which requires
    # agent_run_id for session-binding and returns ALLOW before this function
    # is called. No fallback needed here.

    # Category 2b: orchestration-only auto-read.
    if agent_role != "orchestration":
        return False

    # (a) repo-contained exact lexical match
    if rel_posix is not None and rel_posix in _AUTO_READ_TOLERATED_REPO_RELPATHS:
        return True

    # (b) project-memory file outside the repo: must lexically equal
    # <home>/.claude/projects/<repo-slug>/memory/MEMORY.md, where <repo-slug>
    # is derived from the current repo_root. This binds tolerance to the
    # current project's slot only — preventing cross-project memory
    # exfiltration.
    try:
        home_abs = Path.home()
    except (OSError, RuntimeError):
        return False
    expected_slug = _claude_project_slug(repo_root_abs)
    expected_path = (
        home_abs
        / _AUTO_READ_PROJECT_MEMORY_PARENT_TAIL
        / expected_slug
        / "memory"
        / "MEMORY.md"
    )
    return abs_target == expected_path


def _absolute_lexical(repo_root: Path, path_token: str) -> Path:
    """Return absolute, lexically-normalized path WITHOUT following symlinks."""
    raw = path_token.strip()
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    # os.path.normpath collapses '.', '..' lexically without following symlinks.
    return Path(os.path.normpath(str(candidate)))


def _path_has_no_symlink_redirect(target: Path) -> bool:
    """True iff no segment of `target` is itself a symlink.

    Walks each path component (root → leaf) and lstat's it. A non-existent
    component is fine (no symlink possible). Any S_ISLNK component returns
    False — refusing tolerance whenever the path could be redirected.
    """
    import stat as _stat
    parts = list(target.parts)
    accumulator = Path(parts[0]) if parts else Path("/")
    # On absolute POSIX paths, parts[0] is "/", subsequent parts are segments.
    for part in parts[1:]:
        accumulator = accumulator / part
        try:
            st = os.lstat(str(accumulator))
        except FileNotFoundError:
            # A non-existent intermediate (or leaf) cannot be a symlink target.
            continue
        except OSError:
            return False
        if _stat.S_ISLNK(st.st_mode):
            return False
    return True


def _claude_project_slug(repo_root: Path) -> str:
    """Derive Claude Code's project-directory slug from a repo root.

    Claude Code stores per-project state under ~/.claude/projects/<slug>/, where
    <slug> is the absolute repo path with each '/' replaced by '-'. For example,
    /home/<user>/work/met-dsl → -home-<user>-work-met-dsl.
    """
    abs_str = str(repo_root)
    return abs_str.replace("/", "-")


def _auto_reads_seen_path(repo_root: Path, orchestration_id: str, agent_run_id: str) -> Path:
    return (
        repo_root
        / "workspace"
        / "orchestrations"
        / orchestration_id
        / "audit"
        / f"{agent_run_id}.auto_reads_seen.json"
    )


def _canonical_auto_read_key(repo_root: Path, file_path: str) -> str:
    """Return a canonical key for the auto-read seen-set.

    Different spellings of the same file (`MEMORY.md`, `./MEMORY.md`, the
    absolute repo path) MUST produce the same key, otherwise the first-read
    invariant can be defeated by re-spelling. We normalize via the same
    `_absolute_lexical` helper used by `_is_auto_read_tolerated` and key by
    the absolute lexical path string.
    """
    try:
        abs_target = _absolute_lexical(repo_root, file_path)
    except (OSError, ValueError):
        # Fall back to a stripped form rather than the raw string so trivial
        # whitespace differences don't multiply keys.
        return file_path.strip()
    return str(abs_target)


_AUTO_READ_STARTUP_WINDOW_SECONDS: int = 120


def _orchestration_started_at(repo_root: Path, orchestration_id: str) -> datetime | None:
    """Return orchestration_meta.json's `started_at` as a tz-aware datetime."""
    meta_path = (
        repo_root
        / "workspace"
        / "orchestrations"
        / orchestration_id
        / "orchestration_meta.json"
    )
    if not meta_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(meta, dict):
        return None
    raw = meta.get("started_at")
    if not isinstance(raw, str) or not raw.strip():
        return None
    s = raw.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _record_and_check_first_auto_read(
    repo_root: Path,
    orchestration_id: str,
    agent_run_id: str,
    file_path: str,
) -> bool:
    """Track per-agent first-read of allowlisted auto-read paths.

    Returns True iff this read should be classified as a benign Claude Code
    startup auto-read.  TWO conditions must hold:
    (a) This is the FIRST time `agent_run_id` has read `file_path` (within
        an allowlisted path).  Path identity is determined by
        `_canonical_auto_read_key`, so different spellings collapse to a
        single seen-set entry.
    (b) The read happened within a startup window after orchestration
        `started_at`.  Outside the window, even a first-read is treated as
        prompt-induced (substantive) — the platform's auto-reads should
        complete in the first few seconds, so a much later "first read"
        of MEMORY.md is far more likely to be agent behavior than a
        delayed startup probe.
    """
    # (b) Time-window check — fail-closed: if `started_at` is missing,
    # malformed, or outside the startup window, classify the read as
    # substantive.  Without a verifiable startup signal we cannot prove
    # the read is benign platform behavior, so we must err on the side of
    # surfacing it as a real policy hit.
    started_at = _orchestration_started_at(repo_root, orchestration_id)
    if started_at is None:
        return False
    elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
    if elapsed < 0 or elapsed > _AUTO_READ_STARTUP_WINDOW_SECONDS:
        return False

    # (a) First-read check.  We perform a serialized read-modify-write on
    # the seen-set file via fcntl.flock so that concurrent hook invocations
    # (multiple Read tool calls in flight) cannot both classify the same
    # file as "first read" by racing on an empty set.  If we cannot persist
    # the updated set (read-only audit dir, ENOSPC, etc.) we fail-CLOSED:
    # without a durable record of "seen," we cannot honor the first-read
    # invariant on the next call, so we refuse benign classification now
    # rather than risk hiding a real policy hit on subsequent reads.
    if _fcntl is None:
        # Non-POSIX (Windows): no portable file lock available — fail-closed.
        return False
    state_path = _auto_reads_seen_path(repo_root, orchestration_id, agent_run_id)
    canonical_key = _canonical_auto_read_key(repo_root, file_path)
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False  # cannot establish persistent state → fail-closed
    try:
        # O_RDWR | O_CREAT — open existing or create empty; flock then
        # truncate-and-write the updated set under exclusive lock.
        fd = os.open(str(state_path), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return False  # fail-closed: cannot acquire state file
    try:
        # Acquire the exclusive lock with a bounded retry — a stuck holder
        # (zombie sibling, NFS lock-server hiccup, debugger-paused process)
        # would otherwise hang every subsequent Read hook on this
        # orchestration indefinitely. Retry a small number of times with a
        # short backoff, then fail-closed.
        _LOCK_RETRY_LIMIT = 5
        _LOCK_RETRY_BACKOFF_S = 0.1
        _lock_acquired = False
        for _ in range(_LOCK_RETRY_LIMIT):
            try:
                _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                _lock_acquired = True
                break
            except BlockingIOError:
                time.sleep(_LOCK_RETRY_BACKOFF_S)
            except OSError:
                return False  # locking unavailable → fail-closed
        if not _lock_acquired:
            return False  # persistent contention → fail-closed
        # Read current contents under lock.  Cap at 64 KiB — far above
        # legitimate need (the seen-set holds ≤ a handful of allowlisted
        # paths) and small enough that an oversized file is a clear
        # corruption/attack signal.  Read in a loop until EOF or cap so
        # that no payload below the cap is silently truncated.
        _MAX_SEEN_BYTES = 64 * 1024
        seen: set[str] = set()
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                file_size = os.fstat(fd).st_size
            except OSError:
                file_size = 0
            if file_size > _MAX_SEEN_BYTES:
                # Suspicious / corrupted seen-set — fail-closed; never reset
                # the file silently (would discard legitimate prior entries
                # in the recoverable case, and would aid an attacker in the
                # corruption case).
                return False
            buf = b""
            while len(buf) < _MAX_SEEN_BYTES:
                chunk = os.read(fd, _MAX_SEEN_BYTES - len(buf))
                if not chunk:
                    break
                buf += chunk
            raw = buf.decode("utf-8")
            if raw.strip():
                data = json.loads(raw)
                if isinstance(data, list):
                    seen = {str(x) for x in data if isinstance(x, str)}
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            seen = set()
        if canonical_key in seen:
            return False
        seen.add(canonical_key)
        # Truncate and write updated set
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            payload = json.dumps(sorted(seen), ensure_ascii=False).encode("utf-8")
            os.write(fd, payload)
            os.fsync(fd)
        except OSError:
            return False  # write/fsync failure → fail-closed
        return True
    finally:
        try:
            _fcntl.flock(fd, _fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


def _load_read_manifest_allowed_roots(
    repo_root: Path,
    orchestration_id: str,
    agent_run_id: str,
) -> tuple[list[str] | None, HookDecision | None]:
    """Load `allowed_read_roots` for one agent run.

    Returns `(roots, None)` on success and `(None, block_decision)` for each of
    the four fail-closed cases (manifest absent / unreadable / not an object /
    missing the list).  Every caller that authorizes a read against the manifest
    must propagate the block decision unchanged — a missing manifest is never an
    allow.
    """
    manifest_path = (
        repo_root
        / "workspace"
        / "orchestrations"
        / orchestration_id
        / "read_manifests"
        / f"{agent_run_id}.json"
    )
    if not manifest_path.exists():
        return None, HookDecision(
            action=HookDecisionAction.BLOCK,
            reason=(
                f"read manifest not found for agent_run_id={agent_run_id!r}. "
                f"{MANIFEST_HINT}"
            ),
            continue_processing=False,
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, HookDecision(
            action=HookDecisionAction.BLOCK,
            reason=(
                f"read manifest is unreadable or invalid JSON for agent_run_id={agent_run_id!r}. "
                f"{MANIFEST_HINT}"
            ),
            continue_processing=False,
        )
    if not isinstance(manifest, dict):
        return None, HookDecision(
            action=HookDecisionAction.BLOCK,
            reason=(
                f"read manifest must be a JSON object for agent_run_id={agent_run_id!r}. "
                f"{MANIFEST_HINT}"
            ),
            continue_processing=False,
        )
    allowed_roots_obj = manifest.get("allowed_read_roots")
    if not isinstance(allowed_roots_obj, list):
        return None, HookDecision(
            action=HookDecisionAction.BLOCK,
            reason=(
                f"read manifest missing allowed_read_roots list for agent_run_id={agent_run_id!r}. "
                f"{MANIFEST_HINT}"
            ),
            continue_processing=False,
        )
    return [str(item) for item in allowed_roots_obj], None


def _read_target_in_allowed_roots(
    repo_root: Path, allowed_roots: list[str], file_path: str
) -> bool:
    """Whether `file_path` resolves under (or equal to) one of `allowed_roots`."""
    abs_target = _resolve_target_path(repo_root, file_path)
    for root in allowed_roots:
        abs_root = _resolve_manifest_root(repo_root, root.rstrip("/"))
        if _is_path_under_root(abs_target, abs_root):
            return True
    return False


def append_hook_access_log(
    repo_root: Path,
    orchestration_id: str,
    agent_run_id: str,
    *,
    tool_name: str,
    path: str,
    decision: str,
    policy: str | None = None,
) -> None:
    """Append one hook-layer read decision to `access_logs/<agent_run_id>.jsonl`.

    Best-effort observability, never an authorization step: the whole body is
    swallowed on OSError so a logging failure can never change a hook decision.
    The directory is never created — inside the leaf's bwrap sandbox only the
    per-arid file is bound writable and `access_logs/` itself is read-only, so a
    mkdir would fail rather than help.  The record is additive over the shape
    `log_orchestration_read` writes (gate lines carry no "source" key).
    """
    entry = {
        "ts": _utc_now_iso(),
        "path": path,
        "source": "hook",
        "tool": tool_name,
        "decision": decision,
        "policy": policy,
    }
    try:
        log_path = (
            repo_root
            / "workspace"
            / "orchestrations"
            / orchestration_id
            / "access_logs"
            / f"{agent_run_id}.jsonl"
        )
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def validate_read_access(
    repo_root: Path,
    orchestration_id: str,
    agent_run_id: str,
    file_path: str,
    agent_role: str | None = None,
    session_id: str | None = None,
) -> HookDecision:
    """Verify the read target against the read manifest's allowed_read_roots."""
    # Category 2a: persisted tool-results for any agent role.
    # These are harness-internal files (never in read_manifest) that agents must
    # be able to read when large tool outputs have been persisted as
    # <persisted-output>.  Return ALLOW directly — do not route through the
    # block-as-noise path used for startup auto-reads.
    # session_id (the live Claude Code session identifier from the hook payload)
    # is checked alongside agent_run_id because Claude Code stores tool-results
    # under its own session directory, which may differ from agent_run_id.
    if _is_persisted_tool_result_read(
        repo_root, agent_role, agent_run_id, file_path, session_id=session_id
    ):
        return HookDecision(action=HookDecisionAction.ALLOW)
    if _is_auto_read_tolerated(repo_root, agent_role, file_path):
        # Keep the read-trust boundary intact: persistent state files
        # (MEMORY.md, README.md, ~/.claude/projects/.../memory/MEMORY.md) must
        # NOT enter the orchestration agent's context, even though Claude Code
        # auto-issues these reads at session start.
        #
        # Only the FIRST read of each allowlisted path by this agent is
        # classified as benign platform noise (`auto_read_expected_block`).
        # Subsequent reads of the same path indicate a prompt-induced
        # post-startup access and fall through to the normal substantive
        # policy, where they show up in audit as real read_manifest_read_guard
        # violations rather than benign noise.
        if _record_and_check_first_auto_read(
            repo_root, orchestration_id, agent_run_id, file_path
        ):
            return HookDecision(
                action=HookDecisionAction.BLOCK,
                reason=(
                    f"blocked (expected auto-read): {file_path!r} is a Claude Code "
                    "auto-read path that must not enter orchestration context. "
                    "This block is harmless platform behavior; ignore in retry logic."
                ),
                continue_processing=False,
                audit_detail={
                    "policy": "auto_read_expected_block",
                    "file_path": file_path,
                    "agent_role": agent_role,
                    "agent_run_id": agent_run_id,
                    "orchestration_id": orchestration_id,
                },
            )
        # Fall through to the substantive read-manifest path below — repeated
        # reads of the same allowlisted file are not classified as benign.
    if _is_self_agent_manifest_read_path(repo_root, orchestration_id, agent_run_id, file_path):
        return HookDecision(action=HookDecisionAction.ALLOW)
    allowed_roots, manifest_block = _load_read_manifest_allowed_roots(
        repo_root, orchestration_id, agent_run_id
    )
    if manifest_block is not None or allowed_roots is None:
        return manifest_block or HookDecision(
            action=HookDecisionAction.BLOCK,
            reason=(
                f"read manifest allowed_read_roots unavailable for agent_run_id={agent_run_id!r}. "
                f"{MANIFEST_HINT}"
            ),
            continue_processing=False,
        )
    if _read_target_in_allowed_roots(repo_root, allowed_roots, file_path):
        return HookDecision(action=HookDecisionAction.ALLOW)
    return HookDecision(
        action=HookDecisionAction.BLOCK,
        reason=(
            f"unauthorized read: {file_path!r} is not in read_manifest allowed_read_roots "
            f"(agent_run_id={agent_run_id!r}). {READ_HINT}"
        ),
        continue_processing=False,
        audit_detail={
            "policy": "read_manifest_read_guard",
            "file_path": file_path,
            "agent_run_id": agent_run_id,
            "allowed_read_roots": allowed_roots,
            "fix_hint": {
                # `note`, not `next_command`: format_block_reason_with_hint only
                # renders the four fields it names, so a new key would never
                # reach the agent. And deliberately not a run-gate command —
                # log_orchestration_read terminally fails the orchestration for
                # an out-of-manifest path (rule_source_violation + status=fail),
                # so steering a blocked agent there turns a recoverable block
                # into a dead run.
                "note": (
                    "re-issue the read against a path under allowed_read_roots, or relaunch "
                    "with a manifest that declares this path; it is unreadable by every tool "
                    "until then"
                ),
                "docs_ref": "docs/RUNBOOK.md#hook-recovery",
            },
        },
    )
