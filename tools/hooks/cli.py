#!/usr/bin/env python3
"""Unified backend hook entrypoint."""

from __future__ import annotations

import argparse
import dataclasses
import glob
import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any

from tools.hooks.adapters import ClaudeHookAdapter, CodexHookAdapter
from tools.hooks.codex_feature import read_codex_feature_cache
from tools.hooks.common import (
    HookDecision,
    HookDecisionAction,
    HookEventName,
    _is_path_under_root,
    _is_self_agent_manifest_read_path,
    _load_read_manifest_allowed_roots,
    _read_target_in_allowed_roots,
    _resolve_target_path,
    _strip_quoted_strings,
    _utc_now_iso,
    append_hook_access_log,
    check_cli_managed_path,
    evaluate_common_policy,
    expand_bash_braces,
    extract_bash_read_targets,
    normalize_hook_event_name,
    READ_HINT,
    WRITE_HINT,
    validate_read_access,
    validate_write_access,
)


def _load_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.input_json:
        raw = args.input_json
    else:
        raw = sys.stdin.read()
    if not raw.strip():
        return {}
    loaded = json.loads(raw)
    if not isinstance(loaded, dict):
        raise ValueError("hook payload must be a JSON object")
    return loaded


def _resolve_event_name(args: argparse.Namespace, payload: dict[str, Any]) -> HookEventName:
    if args.event:
        return normalize_hook_event_name(args.event)
    for key in ("event_name", "event", "hook_event", "hook_event_name"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return normalize_hook_event_name(value)
    raise ValueError("hook event name is required (--event or payload.event_name)")


def _adapter_for_backend(backend: str):
    token = backend.strip().lower()
    if token == "codex":
        return CodexHookAdapter()
    if token == "claude":
        return ClaudeHookAdapter()
    raise ValueError(f"unsupported backend: {backend!r}")


def _decision_error(message: str) -> HookDecision:
    return HookDecision(
        action=HookDecisionAction.BLOCK,
        reason=message,
        continue_processing=False,
    )


def _inner_payload(payload: dict[str, Any]) -> dict[str, Any]:
    inner = payload.get("payload")
    return inner if isinstance(inner, dict) else {}


def _payload_value(payload: dict[str, Any], key: str) -> Any:
    value = payload.get(key)
    if value is not None:
        return value
    return _inner_payload(payload).get(key)


def _tool_input(payload: dict[str, Any]) -> dict[str, Any]:
    value = _payload_value(payload, "tool_input")
    return value if isinstance(value, dict) else {}


def _redact_sensitive_text(text: str) -> str:
    redacted = re.sub(r"(--capability-token(?:=|\s+))\S+", r"\1<redacted>", text)
    redacted = re.sub(
        r'("capability_token"\s*:\s*")([^"]+)(")',
        r'\1<redacted>\3',
        redacted,
    )
    return redacted


def _trim_audit_text(text: str, *, limit: int = 500) -> str:
    safe = _redact_sensitive_text(text)
    if len(safe) <= limit:
        return safe
    return safe[:limit] + f"...<truncated {len(safe) - limit} chars>"


def _extract_apply_patch_paths(patch_text: str) -> list[str]:
    paths: list[str] = []
    for line in patch_text.splitlines():
        for prefix in (
            "*** Add File: ",
            "*** Update File: ",
            "*** Delete File: ",
            "*** Move to: ",
        ):
            if line.startswith(prefix):
                token = line[len(prefix):].strip()
                if token:
                    paths.append(token)
                break
    deduped: list[str] = []
    seen: set[str] = set()
    for path in paths:
        if path in seen:
            continue
        deduped.append(path)
        seen.add(path)
    return deduped


def _sanitize_audit_detail(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_s = str(key)
            if key_s.lower() in {"capability_token", "token", "secret"}:
                sanitized[key_s] = "<redacted>"
            else:
                sanitized[key_s] = _sanitize_audit_detail(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_audit_detail(item) for item in value]
    if isinstance(value, str):
        return _trim_audit_text(value)
    return value


def _audit_payload_summary(payload: dict[str, Any], tool_name: str | None) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    model = _payload_value(payload, "model")
    if isinstance(model, str) and model.strip():
        # Codex SessionStart supplies the effective model even though the
        # non-interactive JSONL stream does not guarantee a model field.
        summary["model"] = _trim_audit_text(model.strip(), limit=200)
    session_id = _payload_value(payload, "session_id")
    if isinstance(session_id, str) and session_id.strip():
        summary["session_id"] = session_id.strip()
    agent_session_id = _payload_value(payload, "agent_session_id")
    if isinstance(agent_session_id, str) and agent_session_id.strip():
        summary["agent_session_id"] = agent_session_id.strip()

    tool_input = _tool_input(payload)
    file_path = tool_input.get("file_path")
    if isinstance(file_path, str) and file_path.strip():
        summary["file_path"] = file_path.strip()

    # Grep/Glob name their target with `path`, not `file_path`. Without these the
    # record carries no payload_summary at all, so a search an agent retries in a
    # loop is indistinguishable from a one-off. `audit_orchestration._repeat_key_of`
    # keys on these fields when `command` is absent — both halves are needed for
    # repeated-block detection to see a Grep/Glob loop.
    search_path = tool_input.get("path")
    if isinstance(search_path, str) and search_path.strip():
        summary["path"] = search_path.strip()
    pattern = tool_input.get("pattern")
    if isinstance(pattern, str) and pattern.strip():
        summary["pattern"] = _trim_audit_text(pattern.strip(), limit=200)

    command = _payload_value(payload, "command")
    if not isinstance(command, str) or not command.strip():
        candidate = tool_input.get("command")
        command = candidate if isinstance(candidate, str) and candidate.strip() else None
    if isinstance(command, str) and command.strip():
        summary["command"] = _trim_audit_text(command.strip())

    if (tool_name or "").strip() == "apply_patch":
        # Current Codex sends apply_patch's complete patch program as
        # tool_input.command.  The older patch / patch_text shapes remain only
        # for fixture and older-CLI compatibility.
        patch_value = tool_input.get("command")
        if not isinstance(patch_value, str):
            patch_value = tool_input.get("patch")
        if not isinstance(patch_value, str):
            patch_value = tool_input.get("patch_text")
        if isinstance(patch_value, str):
            summary["apply_patch_paths"] = _extract_apply_patch_paths(patch_value)
            summary["patch_line_count"] = len(patch_value.splitlines())
    return summary


def _append_hook_audit(
    *,
    backend: str,
    event_name: HookEventName,
    payload: dict[str, Any],
    decision: HookDecision,
    orchestration_id_override: str | None = None,
) -> None:
    orchestration_id = orchestration_id_override
    if not isinstance(orchestration_id, str) or not orchestration_id.strip():
        orchestration_id = payload.get("orchestration_id")
        if not isinstance(orchestration_id, str) or not orchestration_id.strip():
            inner = payload.get("payload")
            if isinstance(inner, dict):
                orchestration_id = inner.get("orchestration_id")
    if not isinstance(orchestration_id, str) or not orchestration_id.strip():
        return
    normalized_orch = orchestration_id.strip()
    inner_payload = _inner_payload(payload)
    inner_tool_name = inner_payload.get("tool_name")
    tool_name_raw = payload.get("tool_name")
    tool_name = tool_name_raw if isinstance(tool_name_raw, str) and tool_name_raw.strip() else inner_tool_name
    workflow_mode = os.environ.get("METDSL_WORKFLOW_MODE", "").strip().lower()
    if (
        normalized_orch == "_global"
        and isinstance(tool_name, str)
        and tool_name.strip().lower() == "shell"
        and workflow_mode not in {"1", "true", "yes"}
    ):
        return
    payload_has_repo_root = isinstance(payload.get("repo_root"), str) and bool(
        str(payload.get("repo_root")).strip()
    )
    inner_has_repo_root = isinstance(inner_payload, dict) and isinstance(
        inner_payload.get("repo_root"), str
    ) and bool(str(inner_payload.get("repo_root")).strip())
    env_repo_root = os.environ.get("METDSL_HOOK_REPO_ROOT", "").strip()
    if (
        normalized_orch == "_global"
        and workflow_mode not in {"1", "true", "yes"}
        and not payload_has_repo_root
        and not inner_has_repo_root
        and not env_repo_root
    ):
        return
    repo_root_raw = payload.get("repo_root")
    if not (isinstance(repo_root_raw, str) and repo_root_raw.strip()):
        if isinstance(inner_payload, dict):
            inner_repo_root = inner_payload.get("repo_root")
            if isinstance(inner_repo_root, str) and inner_repo_root.strip():
                repo_root_raw = inner_repo_root

    # For an ambient hook call where `repo_root` is unspecified, do not pollute the
    # actual workspace. To persist the audit log, give an explicit `repo_root`
    # (or `METDSL_HOOK_REPO_ROOT` via env).
    if not (isinstance(repo_root_raw, str) and repo_root_raw.strip()):
        if env_repo_root:
            repo_root = Path(env_repo_root).resolve()
        else:
            return
    else:
        repo_root = Path(repo_root_raw).resolve()
    path = (
        repo_root
        / "workspace"
        / "orchestrations"
        / normalized_orch
        / "hooks"
        / "native_hook_events.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": _utc_now_iso(),
        "backend": backend,
        "event": event_name.value,
        "action": decision.action.value,
        "reason": decision.reason,
        "continue_processing": decision.continue_processing,
        "tool_name": tool_name,
    }
    payload_summary = _audit_payload_summary(payload, tool_name if isinstance(tool_name, str) else None)
    if payload_summary:
        entry["payload_summary"] = payload_summary
    if decision.audit_detail is not None:
        entry["audit_detail"] = _sanitize_audit_detail(decision.audit_detail)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _resolve_repo_root(payload: dict[str, Any], backend: str = "") -> Path:
    del backend
    env_repo_root = os.environ.get("METDSL_HOOK_REPO_ROOT", "").strip()
    if env_repo_root:
        return Path(env_repo_root).resolve()
    repo_root_raw = payload.get("repo_root")
    return (
        Path(repo_root_raw).resolve()
        if isinstance(repo_root_raw, str) and repo_root_raw.strip()
        else Path.cwd()
    )


def _env_flag_true(name: str, default: str = "0") -> bool:
    raw = os.environ.get(name, default).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _extract_orchestration_id(payload: dict[str, Any]) -> str | None:
    orchestration_id = payload.get("orchestration_id")
    if isinstance(orchestration_id, str) and orchestration_id.strip():
        return orchestration_id.strip()
    inner = payload.get("payload")
    if isinstance(inner, dict):
        inner_id = inner.get("orchestration_id")
        if isinstance(inner_id, str) and inner_id.strip():
            return inner_id.strip()
    env_value = os.environ.get("METDSL_ORCHESTRATION_ID")
    if isinstance(env_value, str) and env_value.strip():
        return env_value.strip()
    return None


def _active_child_agent_run_id_path(repo_root: Path, orchestration_id: str) -> Path:
    return (
        repo_root
        / "workspace"
        / "orchestrations"
        / orchestration_id
        / "active_child_agent_run_id.txt"
    )


def _get_orchestration_agent_run_id(repo_root: Path, orchestration_id: str) -> str | None:
    """Obtain orchestration_agent_run_id from orchestration_meta.json."""
    meta_path = (
        repo_root
        / "workspace"
        / "orchestrations"
        / orchestration_id
        / "orchestration_meta.json"
    )
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    run_id = meta.get("orchestration_agent_run_id")
    return run_id.strip() if isinstance(run_id, str) and run_id.strip() else None


# shell redirection: cmd > path, cmd >> path
_BASH_REDIRECT_RE = re.compile(r"(?:>>?)\s+([^\s;&|<>)]+)")
# tee: tee [-opts] path
_BASH_TEE_RE = re.compile(r"\btee\b(?:\s+-\w+)*\s+([^\n;&|<>]+)")
_REDIRECT_SKIP = frozenset({
    "/dev/null", "/dev/stderr", "/dev/stdout", "/dev/stdin", "1", "2",
})
def _skip_single_quoted(s: str, i: int) -> int:
    """Advance past a single-quoted string starting after the opening quote."""
    n = len(s)
    while i < n and s[i] != "'":
        i += 1
    return i + 1 if i < n else i


def _skip_double_quoted(s: str, i: int) -> int:
    """Advance past a double-quoted string starting after the opening quote.
    Does NOT recurse into $() / backticks — callers handle those separately.
    """
    n = len(s)
    while i < n and s[i] != '"':
        if s[i] == "\\" and i + 1 < n:
            i += 2
        else:
            i += 1
    return i + 1 if i < n else i


def _scan_backtick(s: str, start: int) -> tuple[str, int]:
    """Return (body, index_after_closing_backtick) for a `...` substitution."""
    i = start
    n = len(s)
    while i < n and s[i] != "`":
        if s[i] == "\\" and i + 1 < n:
            i += 2
        else:
            i += 1
    return s[start:i], (i + 1 if i < n else i)


def _scan_arithmetic_expansion(s: str, start: int) -> tuple[list[str], int]:
    """Advance past $((...)) starting after the two opening '((' chars.

    $((…)) is arithmetic expansion — bare '>' inside is comparison, not redirect.
    However nested $(...) and backtick substitutions inside arithmetic DO execute,
    so their bodies are collected and returned for recursive write-target scanning.

    Returns (nested_bodies, index_after_closing_'))')
    """
    depth = 2  # two open parens from '$((' to close
    i = start
    n = len(s)
    bodies: list[str] = []
    while i < n and depth > 0:
        c = s[i]
        if c == "\\" and i + 1 < n:
            i += 2
        elif c == "'":
            i = _skip_single_quoted(s, i + 1)
        elif s[i : i + 3] == "$((":
            # nested arithmetic — recurse to capture its inner bodies
            nested, i = _scan_arithmetic_expansion(s, i + 3)
            bodies.extend(nested)
        elif s[i : i + 2] == "$(":
            # nested command substitution inside arithmetic — executes
            body, i = _scan_subshell(s, i + 2)
            bodies.append(body)
        elif c == "`":
            body, i = _scan_backtick(s, i + 1)
            bodies.append(body)
        elif c == "(":
            depth += 1
            i += 1
        elif c == ")":
            depth -= 1
            i += 1
        else:
            i += 1
    return bodies, i


def _scan_subshell(s: str, start: int) -> tuple[str, int]:
    """Return (body, index_after_closing_paren) for a $(...) starting after '('.

    Tracks depth while respecting single/double-quoted strings and escape sequences,
    so characters like '(' inside single quotes do not affect nesting depth.
    """
    depth = 1
    i = start
    n = len(s)
    while i < n and depth > 0:
        c = s[i]
        if c == "\\" and i + 1 < n:
            i += 2
        elif c == "'":
            i = _skip_single_quoted(s, i + 1)
        elif c == '"':
            i = _skip_double_quoted(s, i + 1)
        elif c == "`":
            _, i = _scan_backtick(s, i + 1)
        elif s[i : i + 2] == "$(":
            depth += 1
            i += 2
        elif c == "(":
            depth += 1
            i += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return s[start:i], i + 1
            i += 1
        else:
            i += 1
    return s[start:i], i


def _extract_command_substitution_bodies(command: str) -> list[str]:
    """Return bodies of every $(...) and `...` substitution, respecting shell quoting.

    Double-quoted strings do NOT suppress $() or backtick substitutions in bash,
    so these must be scanned for write targets even after the outer quotes are stripped.
    Parentheses inside single/double quotes are ignored for depth counting.
    """
    bodies: list[str] = []
    i = 0
    n = len(command)
    while i < n:
        c = command[i]
        if c == "\\" and i + 1 < n:
            i += 2
        elif c == "'":
            i = _skip_single_quoted(command, i + 1)
        elif c == '"':
            # Inside double-quotes $() and backticks still expand — scan for them.
            i += 1
            while i < n and command[i] != '"':
                if command[i] == "\\" and i + 1 < n:
                    i += 2
                elif command[i : i + 3] == "$((":
                    nested, i = _scan_arithmetic_expansion(command, i + 3)
                    for b in nested:
                        bodies.append(b)
                        bodies.extend(_extract_command_substitution_bodies(b))
                elif command[i : i + 2] == "$(":
                    body, i = _scan_subshell(command, i + 2)
                    bodies.append(body)
                    bodies.extend(_extract_command_substitution_bodies(body))
                elif command[i] == "`":
                    body, i = _scan_backtick(command, i + 1)
                    bodies.append(body)
                    bodies.extend(_extract_command_substitution_bodies(body))
                else:
                    i += 1
            if i < n:
                i += 1  # skip closing "
        elif c == "`":
            body, i = _scan_backtick(command, i + 1)
            bodies.append(body)
            bodies.extend(_extract_command_substitution_bodies(body))
        elif command[i : i + 3] == "$((":
            nested, i = _scan_arithmetic_expansion(command, i + 3)
            for b in nested:
                bodies.append(b)
                bodies.extend(_extract_command_substitution_bodies(b))
        elif command[i : i + 2] == "$(":
            body, i = _scan_subshell(command, i + 2)
            bodies.append(body)
            bodies.extend(_extract_command_substitution_bodies(body))
        else:
            i += 1
    return bodies


_SHELL_CONTROL_TOKENS = frozenset({"|", "||", "&&", ";"})

# Bash commands whose ONLY file-write vector is shell redirection (which
# _detect_bash_write_targets catches) — they carry no own write/exec flags.
# Deliberately EXCLUDES find (-exec/-delete), awk (in-program `print > f`),
# sed (`w`/`e`/`-i`), sort (-o), env/xargs (exec), all interpreters, and all
# network tools, because those can write or execute without a shell redirect
# the detector would see. Also EXCLUDES ripgrep (`rg`): its `--pre`/`--pre-glob`
# run an arbitrary preprocessor command (and `RIPGREP_CONFIG_PATH` can inject
# the same out-of-band) — GNU grep/egrep/fgrep have no such flag and stay.
# Used only by _is_auto_approvable_readonly_bash.
_SAFE_READONLY_BASH_CMDS = frozenset({
    "grep", "egrep", "fgrep", "ls", "cat", "wc", "head", "tail",
    "echo", "printf", "date", "dirname", "basename", "realpath", "readlink",
    "pwd", "true", "false", "test", "[", "nl", "tac", "cut", "tr", "comm",
    "diff", "jq",
})

# fd-duplication redirects (`2>&1`, `>&2`, `1>&2`) are NOT file writes. The RHS
# digits must be the WHOLE token (negative lookahead for a following filename
# char): bash treats `n>&word` as fd-dup only when `word` is all digits, else
# `word` is a file. Without the lookahead, `1>&9secret` would match the `1>&9`
# prefix, get stripped, and leave `secret` as an inert token — auto-approving a
# file write. With it, such glued forms survive and trip the residual `>`/`&`
# rejection below, while genuine dups (`2>&1`, `1>&12`) still strip cleanly.
_FD_DUP_RE = re.compile(r"\d*>&\d+(?![\w./-])")
# Leading `VAR=value` assignment prefix on a command segment.
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Shell command separators we split on for per-command argv0 validation. `||`
# must precede `|` in the alternation. Split is applied to the quote-stripped
# command string (NOT the shlex token list), because shlex does not surface a
# separator glued to an adjacent word (`cat a;curl x` -> ['cat','a;curl','x']).
_SEPARATOR_RE = re.compile(r"\|\||&&|;|\|")


def _looks_like_sed_script(token: str) -> bool:
    if not token:
        return False
    lowered = token.lower()
    if lowered.startswith(("s/", "y/", "c\\", "i\\", "a\\")):
        return True
    return "=" in token and lowered.split("=", 1)[0] in {"s", "y"}


def _detect_sed_inplace_targets(command: str) -> list[str]:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    targets: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.split("/")[-1] != "sed":
            i += 1
            continue
        j = i + 1
        segment: list[str] = []
        while j < len(tokens) and tokens[j] not in _SHELL_CONTROL_TOKENS:
            segment.append(tokens[j])
            j += 1
        k = 0
        while k < len(segment):
            arg = segment[k]
            if arg == "-i" or arg.startswith("-i"):
                candidate_idx = k + 1
                if candidate_idx >= len(segment):
                    k += 1
                    continue
                candidate = segment[candidate_idx]
                if _looks_like_sed_script(candidate) and candidate_idx + 1 < len(segment):
                    candidate = segment[candidate_idx + 1]
                if candidate and not candidate.startswith("-"):
                    targets.append(candidate)
            k += 1
        i = j + 1 if j < len(tokens) else j
    return targets


def _detect_bash_write_targets(command: str | None) -> list[str]:
    """Extract write-target paths from a Bash command."""
    if not command:
        return []
    targets: list[str] = []
    # Recurse into $(...) and `...` bodies first.  Double-quoted strings do NOT
    # suppress command substitutions in bash, so a redirect inside "$(cmd > path)"
    # is real even though the outer quotes would otherwise be stripped below.
    for body in _extract_command_substitution_bodies(command):
        targets.extend(_detect_bash_write_targets(body))
    # Strip quoted string content before redirect/tee scanning to avoid false-positives
    # caused by `>` or `tee` appearing as literal text inside CLI argument values
    # (e.g. --reply-text "exit code > 0").
    # _detect_sed_inplace_targets uses shlex.split internally and is not affected.
    scanned = _strip_quoted_strings(command)
    for m in _BASH_REDIRECT_RE.finditer(scanned):
        path = m.group(1)
        if path not in _REDIRECT_SKIP and not path.startswith("&"):
            targets.append(path)
    for m in _BASH_TEE_RE.finditer(scanned):
        # Match on scanned to skip `tee` inside quoted strings, but recover the
        # original blob (same span) so quoted paths like tee "out.log" are preserved.
        # _strip_quoted_strings is length-preserving, so span indices stay aligned.
        start, end = m.span(1)
        blob = command[start:end]
        try:
            tee_args = shlex.split(blob)
        except ValueError:
            tee_args = blob.split()
        for arg in tee_args:
            if arg.startswith("-"):
                continue
            if arg in {"|", "||", "&&", ";"}:
                break
            targets.append(arg)
    targets.extend(_detect_sed_inplace_targets(command))
    return targets


def _is_auto_approvable_readonly_bash(command: str | None) -> bool:
    """True iff `command` is a provably read-only composition safe to auto-approve.

    Auto-approval (ALLOW_AUTO_APPROVE -> permissionDecision="allow") bypasses the
    harness's native permission allowlist, so this is fail-closed: it returns True
    only for compositions (pipe / ; / && / ||) of a tight set of read-only
    commands, with no command substitution, no process substitution / subshell,
    and no file-output redirection (fd-duplication like `2>&1` is allowed).
    Anything it cannot prove safe returns False, falling back to the existing
    allowlist-governed behavior. Callers must only invoke this when there are no
    authorized write targets (purely read-only commands); writes are out of scope
    for this increment.
    """
    if not command or not command.strip():
        return False
    # No command substitution ($(...) / backticks).
    if _extract_command_substitution_bodies(command):
        return False
    scanned = _strip_quoted_strings(command)
    # Remove fd-duplication, then blank out the RECOGNIZED control operators so a
    # stray `&` (background `cmd &`, stderr-pipe `|&`) is not confused with the
    # allowed `&&`. After that, any remaining redirect (`>`/`<`), subshell /
    # process-substitution paren, stray `&`, comment `#`, or newline means we
    # cannot prove the command read-only. `shlex.split` (used below) treats
    # `\n`, `&`, and `#` as ordinary word characters rather than command
    # separators, so without this guard a trailing command glued on after them
    # (`cat a\ncurl ...`) would be swallowed into a safe segment and silently
    # auto-approved — this residual scan is what closes that hole.
    residual = _FD_DUP_RE.sub("", scanned)
    for op in ("&&", "||", ";", "|"):
        residual = residual.replace(op, " ")
    if any(ch in residual for ch in (">", "<", "(", ")", "&", "#", "\n")):
        return False
    # Segment on shell command separators and validate every command's argv0.
    # Split the QUOTE-STRIPPED string (`scanned`) — not the shlex token list —
    # so a separator glued to an adjacent word (`cat a;curl x`) still segments
    # and the trailing command cannot escape the argv0 check. Quote-stripping
    # first ensures a separator inside a quoted argument is not a real separator.
    fragments = _SEPARATOR_RE.split(scanned)
    if not fragments:
        return False
    for fragment in fragments:
        try:
            toks = shlex.split(fragment)
        except ValueError:
            return False
        # A leading `VAR=value` command-prefix is an in-process code-execution
        # channel independent of argv0 (LD_PRELOAD / LD_AUDIT / LD_LIBRARY_PATH /
        # BASH_ENV / ENV / IFS / ...), so reject any fragment that carries one
        # (and any empty / bare-assignment fragment). Benign env prefixes on
        # read-only commands are rare; falling back to the allowlist is fine.
        if not toks or _ASSIGNMENT_RE.match(toks[0]):
            return False
        argv0 = toks[0].split("/")[-1]
        if argv0 not in _SAFE_READONLY_BASH_CMDS:
            return False
    return True


def _resolve_codex_agent_run_id_from_session(
    *,
    repo_root: Path,
    orchestration_id: str,
    session_id: str | None,
    agent_session_id: str | None,
) -> tuple[str | None, int]:
    tokens = {
        value.strip()
        for value in (session_id, agent_session_id)
        if isinstance(value, str) and value.strip()
    }
    if not tokens:
        return None, 0
    orch_root = repo_root / "workspace" / "orchestrations" / orchestration_id
    index_path = orch_root / "session_run_index.json"
    if index_path.is_file():
        try:
            index_doc = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            index_doc = None
        if isinstance(index_doc, dict):
            entries_obj = index_doc.get("entries")
            if isinstance(entries_obj, list):
                matched_from_index: set[str] = set()
                for item in entries_obj:
                    if not isinstance(item, dict):
                        continue
                    run_id_obj = item.get("agent_run_id")
                    if not isinstance(run_id_obj, str) or not run_id_obj.strip():
                        continue
                    candidate_tokens: set[str] = set()
                    for key in ("agent_session_id", "context_id", "session_id"):
                        value_obj = item.get(key)
                        if isinstance(value_obj, str) and value_obj.strip():
                            candidate_tokens.add(value_obj.strip())
                    if tokens.isdisjoint(candidate_tokens):
                        continue
                    matched_from_index.add(run_id_obj.strip())
                if len(matched_from_index) == 1:
                    return next(iter(matched_from_index)), 1
                if len(matched_from_index) > 1:
                    # Codex `exec resume` continues the same thread in place.
                    # Its prior terminal row and the newly allocated repair row
                    # therefore share a thread ID. The active repair owns file
                    # access while it is the sole running candidate; concurrent
                    # running rows remain fail-closed as ambiguous.
                    active_matches = {
                        str(item.get("agent_run_id")).strip()
                        for item in entries_obj
                        if isinstance(item, dict)
                        and str(item.get("agent_run_id") or "").strip() in matched_from_index
                        and str(item.get("status") or "").strip().lower() == "running"
                    }
                    if len(active_matches) == 1:
                        return next(iter(active_matches)), 1
                    return None, len(matched_from_index)
    runs_path = orch_root / "agent_runs.jsonl"
    if not runs_path.is_file():
        return None, 0
    session_match_ids: set[str] = set()
    context_match_ids: set[str] = set()
    with runs_path.open("r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            backend = str(item.get("agent_backend", "")).strip().lower()
            if backend != "codex":
                continue
            run_id = item.get("agent_run_id")
            if not isinstance(run_id, str) or not run_id.strip():
                continue
            normalized_run_id = run_id.strip()
            entry_session = str(item.get("agent_session_id", "")).strip()
            if entry_session in tokens:
                session_match_ids.add(normalized_run_id)
                continue
            entry_context = str(item.get("context_id", "")).strip()
            if entry_context in tokens:
                context_match_ids.add(normalized_run_id)
    if len(session_match_ids) == 1:
        return next(iter(session_match_ids)), 1
    if len(session_match_ids) > 1:
        return None, len(session_match_ids)
    if len(context_match_ids) == 1:
        return next(iter(context_match_ids)), 1
    if len(context_match_ids) > 1:
        return None, len(context_match_ids)
    return None, 0


def _get_agent_role_from_capability(
    repo_root: Path, orchestration_id: str, agent_run_id: str
) -> str | None:
    """Return the agent_role for `agent_run_id`.

    Resolution order:
    1. `capabilities/<agent_run_id>.json` (step/substep agents, written at
       record-launch time).
    2. `orchestration_meta.json` — if `agent_run_id` matches
       `orchestration_agent_run_id`, the role is "orchestration". The
       orchestration agent has no capability file because it is initialized
       directly by `init_orchestration` rather than launched as a child.
    Returns None if neither source identifies the agent.
    """
    cap_path = (
        repo_root
        / "workspace"
        / "orchestrations"
        / orchestration_id
        / "capabilities"
        / f"{agent_run_id}.json"
    )
    if cap_path.is_file():
        try:
            doc = json.loads(cap_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            doc = None
        if isinstance(doc, dict):
            role = doc.get("agent_role")
            if isinstance(role, str) and role.strip():
                return role.strip().lower()

    # Fallback: orchestration agent has no capability file. Match the run id
    # against orchestration_meta.json.
    orch_run_id = _get_orchestration_agent_run_id(repo_root, orchestration_id)
    if orch_run_id and orch_run_id.strip() == agent_run_id.strip():
        return "orchestration"
    return None


def _is_pure_readonly_capability(
    repo_root: Path, orchestration_id: str, agent_run_id: str
) -> bool:
    """Whether this run is a host-mediated pure leaf.

    Codex's pure transport has a read-only sandbox, not Claude's absence of
    tools.  Shell access must therefore be denied explicitly rather than
    relying on the output/write policy to make a read-only command harmless.
    """
    cap_path = (
        repo_root / "workspace" / "orchestrations" / orchestration_id
        / "capabilities" / f"{agent_run_id}.json"
    )
    try:
        doc = json.loads(cap_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(doc, dict) and str(doc.get("mode") or "").strip() == "pure_readonly"


def _hint_for_file_tool(tool_name: str) -> str:
    # Grep/Glob are reads too: handing a blocked search the Edit/Write
    # remediation sends the agent off to think about output manifests.
    return READ_HINT if tool_name in {"Read", "Grep", "Glob"} else WRITE_HINT


def _resolve_agent_run_id_for_file_tool(
    *,
    backend: str,
    repo_root: Path,
    orchestration_id: str,
    session_id: str | None,
    agent_session_id: str | None,
    tool_name: str,
) -> tuple[str | None, HookDecision | None]:
    if backend == "claude":
        active_path = _active_child_agent_run_id_path(repo_root, orchestration_id)
        if active_path.exists():
            active_agent_run_id = active_path.read_text(encoding="utf-8").strip()
            if not active_agent_run_id:
                hint = _hint_for_file_tool(tool_name)
                return None, HookDecision(
                    action=HookDecisionAction.BLOCK,
                    reason=(
                        "active child agent_run_id is empty for Claude backend. "
                        f"{hint}"
                    ),
                    continue_processing=False,
                )
            return active_agent_run_id, None
        orch_agent_run_id = _get_orchestration_agent_run_id(repo_root, orchestration_id)
        if not orch_agent_run_id:
            hint = _hint_for_file_tool(tool_name)
            return None, HookDecision(
                action=HookDecisionAction.BLOCK,
                reason=(
                    "no orchestration_agent_run_id found in orchestration_meta.json. "
                    f"{hint}"
                ),
                continue_processing=False,
            )
        return orch_agent_run_id, None
    # Codex creates its thread id inside `exec`; the first hook can run after
    # it writes `thread.started` to stdout but before the parent drains that
    # pipe and updates session_run_index.json.  record-launch has already
    # created this child's capability and active marker, and the Codex process
    # passes its inherited child identity unchanged to hooks.  Prefer that
    # launch-scoped binding during this narrow bootstrap interval.
    child_run_id = os.environ.get("METDSL_CHILD_AGENT_RUN_ID", "").strip()
    if child_run_id:
        orch_root = repo_root / "workspace" / "orchestrations" / orchestration_id
        cap_path = orch_root / "capabilities" / f"{child_run_id}.json"
        active_path = orch_root / "active_children" / f"{child_run_id}.txt"
        if cap_path.is_file() and active_path.is_file():
            return child_run_id, None
    mapped_agent_run_id, match_count = _resolve_codex_agent_run_id_from_session(
        repo_root=repo_root,
        orchestration_id=orchestration_id,
        session_id=session_id,
        agent_session_id=agent_session_id,
    )
    if not mapped_agent_run_id:
        hint = _hint_for_file_tool(tool_name)
        suffix = (
            f" (ambiguous candidates={match_count})"
            if isinstance(match_count, int) and match_count > 1
            else ""
        )
        return None, HookDecision(
            action=HookDecisionAction.BLOCK,
            reason=f"session-to-run mapping not found{suffix}. {hint}",
            continue_processing=False,
        )
    return mapped_agent_run_id, None


def _validate_write_targets(
    *,
    repo_root: Path,
    orchestration_id: str,
    agent_run_id: str,
    targets: list[str],
    tool_name: str,
    bash_command: str | None = None,
) -> HookDecision:
    for target in targets:
        cli_guard = check_cli_managed_path(repo_root, target)
        candidate = cli_guard if cli_guard is not None else validate_write_access(
            repo_root,
            orchestration_id,
            agent_run_id,
            target,
            tool_name=tool_name,
            bash_command=bash_command,
        )
        if candidate.action == HookDecisionAction.BLOCK:
            return candidate
    return HookDecision(action=HookDecisionAction.ALLOW)


def _log_read_decision(
    *,
    repo_root: Path,
    orchestration_id: str,
    agent_run_id: str,
    tool_name: str,
    path: str,
    decision: HookDecision,
) -> None:
    """Record one hook-layer read decision in the agent's access log."""
    append_hook_access_log(
        repo_root,
        orchestration_id,
        agent_run_id,
        tool_name=tool_name,
        path=path,
        decision="block" if decision.action == HookDecisionAction.BLOCK else "allow",
        policy=(decision.audit_detail or {}).get("policy"),
    )


# Search tools whose read boundary is enforced by validating their `path` root.
# Their `pattern` is NOT validated: a Glob pattern can still reach outside the
# validated root via an absolute or `../` pattern (documented residue, issue #42).
# `**` only recurses within `path`, so it is not part of that residue.
_PATH_SEARCH_TOOLS = frozenset({"Grep", "Glob"})

# Shell glob metacharacters. A token carrying one is expanded by the shell, so
# it must be resolved against the filesystem rather than tested for existence.
_GLOB_META_RE = re.compile(r"[*?\[]")


def _evaluate_grep_glob_read_policy(
    *,
    decoded: Any,
    repo_root: Path,
    orchestration_id: str,
    backend: str,
    tool_name: str,
) -> HookDecision:
    """Authorize a Grep/Glob search root against the agent's read manifest.

    A search is a read: `Grep` returns matching lines and `Glob` returns paths,
    both from wherever `path` points. Unlike step 2's Write/Edit/Read branch we
    must NOT fail open when the path is absent — a pathless Grep searches the
    repo root, which is the widest read the tool offers, so it is validated as
    "." and blocks unless the manifest actually grants the repo root.
    """
    raw_path = _tool_input(decoded.payload).get("path")
    search_path = raw_path.strip() if isinstance(raw_path, str) and raw_path.strip() else ""
    path_missing = not search_path
    if path_missing:
        search_path = "."
    resolved_run_id, resolution_error = _resolve_agent_run_id_for_file_tool(
        backend=backend,
        repo_root=repo_root,
        orchestration_id=orchestration_id,
        session_id=decoded.session_id,
        agent_session_id=decoded.agent_session_id,
        tool_name=tool_name,
    )
    if resolution_error is not None:
        return resolution_error
    if resolved_run_id is None:
        return HookDecision(action=HookDecisionAction.ALLOW)
    agent_role = _get_agent_role_from_capability(repo_root, orchestration_id, resolved_run_id)
    decision = validate_read_access(
        repo_root,
        orchestration_id,
        resolved_run_id,
        search_path,
        agent_role=agent_role,
        session_id=decoded.session_id,
    )
    if decision.action == HookDecisionAction.BLOCK and path_missing:
        decision = dataclasses.replace(
            decision,
            reason=(
                f"{decision.reason or ''} "
                f"{tool_name} was called without a 'path', which searches the repository "
                "root. Pass path= a directory listed in read_manifest allowed_read_roots."
            ).strip(),
        )
    _log_read_decision(
        repo_root=repo_root,
        orchestration_id=orchestration_id,
        agent_run_id=resolved_run_id,
        tool_name=tool_name,
        path=search_path,
        decision=decision,
    )
    return decision


def _is_active_child_return_token_path(
    repo_root: Path, orchestration_id: str, agent_run_id: str, target: str
) -> bool:
    """Whether `target` is the return token of the currently active child.

    `record-child-return` needs the token, and the documented procedure
    (docs/RUNBOOK.md §substep-timeout-recovery) is a bare
    `cat launches/<child_arid>.parent_return_token`, because the Claude Code
    Bash tool rejects the `$(cat ...)` substitution form as un-analyzable and
    the `Read` tool is already blocked here. During the active-child window the
    hook resolves to the CHILD's agent_run_id — whose manifest never lists
    `launches/` — so without this the only working form of a documented
    recovery step would block. Reading this path via Bash was permitted before
    the Bash read guard existed; this keeps that exact behavior rather than
    widening it (the `Read` tool stays blocked).
    """
    orch = orchestration_id.strip()
    rid = agent_run_id.strip()
    if not orch or not rid:
        return False
    abs_target = _resolve_target_path(repo_root, target)
    try:
        rel = abs_target.relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return False
    return rel == f"workspace/orchestrations/{orch}/launches/{rid}.parent_return_token"


def _evaluate_bash_read_manifest_policy(
    *,
    decoded: Any,
    repo_root: Path,
    orchestration_id: str,
    backend: str,
    resolved_run_id: str | None,
) -> tuple[HookDecision | None, str | None]:
    """Authorize the read targets of a Bash command against the read manifest.

    Returns `(decision, resolved_run_id)`; a non-None decision must be returned
    to the caller as-is.  Only targets that EXIST on disk and resolve inside
    repo_root are validated: a nonexistent path leaks nothing (this also absorbs
    over-extraction), and paths outside the repo are bwrap's domain — blocking
    them here would break benign commands while adding no confinement.  Every
    real repo file is inside the sandbox's ro-bind, so the incident class this
    guard exists for is fully covered.

    With no surviving target the command is left on its previous path entirely,
    including the read-only auto-approve; the extractor's blind spots
    (`xargs cat`, substitution) are accepted residue, not proof of safety.
    """
    repo_root_resolved = repo_root.resolve()
    surviving: list[tuple[str, Path]] = []
    extracted: list[str] = []
    for raw_target in extract_bash_read_targets(decoded.command):
        # Brace expansion is lexical, so `cat spec/{a,b}.md` names real files;
        # leaving it unexpanded would drop it as "nonexistent" and auto-approve.
        extracted.extend(expand_bash_braces(raw_target))
    for target in extracted:
        if _GLOB_META_RE.search(target):
            # "A nonexistent path leaks nothing" does NOT hold for a glob: the
            # shell expands it to real files. Dropping it would hand
            # `cat secret/*.txt` to the read-only auto-approve. Validate every
            # file it currently matches instead, naming the match (not the
            # pattern) so the block reason points at a real path.
            for match in sorted(glob.glob(str(_resolve_target_path(repo_root, target)))):
                abs_match = Path(match)
                if not _is_path_under_root(abs_match, repo_root_resolved):
                    continue
                surviving.append((
                    abs_match.relative_to(repo_root_resolved).as_posix(),
                    abs_match,
                ))
            continue
        abs_target = _resolve_target_path(repo_root, target)
        if not _is_path_under_root(abs_target, repo_root_resolved):
            continue
        if not abs_target.exists():
            continue
        surviving.append((target, abs_target))
    if not surviving:
        return None, resolved_run_id
    if resolved_run_id is None:
        resolved_run_id, resolution_error = _resolve_agent_run_id_for_file_tool(
            backend=backend,
            repo_root=repo_root,
            orchestration_id=orchestration_id,
            session_id=decoded.session_id,
            agent_session_id=decoded.agent_session_id,
            tool_name="Read",
        )
        if resolution_error is not None:
            return resolution_error, None
    if resolved_run_id is None:
        return (
            HookDecision(
                action=HookDecisionAction.BLOCK,
                reason=f"session-to-run mapping not found. {READ_HINT}",
                continue_processing=False,
            ),
            None,
        )
    allowed_roots, manifest_block = _load_read_manifest_allowed_roots(
        repo_root, orchestration_id, resolved_run_id
    )
    if manifest_block is not None or allowed_roots is None:
        return manifest_block, resolved_run_id
    for target, _abs_target in surviving:
        if (
            _is_self_agent_manifest_read_path(
                repo_root, orchestration_id, resolved_run_id, target
            )
            or _is_active_child_return_token_path(
                repo_root, orchestration_id, resolved_run_id, target
            )
            or _read_target_in_allowed_roots(repo_root, allowed_roots, target)
        ):
            append_hook_access_log(
                repo_root,
                orchestration_id,
                resolved_run_id,
                tool_name="Bash",
                path=target,
                decision="allow",
            )
            continue
        decision = HookDecision(
            action=HookDecisionAction.BLOCK,
            reason=(
                f"unauthorized read: Bash command reads {target!r}, which is not in "
                f"read_manifest allowed_read_roots (agent_run_id={resolved_run_id!r}). "
                "Re-issue the command against a path under allowed_read_roots; a path "
                "outside them is unreadable by every tool, so there is no alternative "
                "command that reaches it. "
                f"{READ_HINT}"
            ),
            continue_processing=False,
            audit_detail={
                "policy": "read_manifest_read_guard",
                "via": "bash",
                "command": decoded.command,
                "read_target": target,
                "agent_run_id": resolved_run_id,
                "allowed_read_roots": allowed_roots,
                "fix_hint": {
                    "note": (
                        "re-issue the command against a path under allowed_read_roots; "
                        "there is no command that reaches a path outside them"
                    ),
                    "docs_ref": "docs/RUNBOOK.md#hook-recovery",
                },
            },
        )
        append_hook_access_log(
            repo_root,
            orchestration_id,
            resolved_run_id,
            tool_name="Bash",
            path=target,
            decision="block",
            policy="read_manifest_read_guard",
        )
        return decision, resolved_run_id
    return None, resolved_run_id


def _evaluate_pre_command_file_access_policy(
    *,
    decoded: Any,
    repo_root: Path,
    orchestration_id: str,
    backend: str,
) -> HookDecision | None:
    tool_name = (decoded.tool_name or "").strip()
    if decoded.event_name != HookEventName.PRE_COMMAND_EXECUTE:
        return None
    workflow_mode = os.environ.get("METDSL_WORKFLOW_MODE", "0").strip()

    # step 1: apply_patch write guard
    if tool_name == "apply_patch":
        if workflow_mode != "1":
            return None
        patch_text = ""
        decoded_tool_input = _tool_input(decoded.payload)
        patch_value = decoded_tool_input.get("command")
        if not isinstance(patch_value, str):
            patch_value = decoded_tool_input.get("patch")
        if not isinstance(patch_value, str):
            patch_value = decoded_tool_input.get("patch_text")
        if isinstance(patch_value, str):
            patch_text = patch_value
        apply_patch_paths = _extract_apply_patch_paths(patch_text)
        # A workflow-mode apply_patch without a parseable target is not an
        # authorization-free operation.  In particular, never turn a changed
        # Codex payload shape into an empty target allow.
        if not apply_patch_paths:
            return _decision_error(
                "apply_patch payload is missing, unparseable, or has no target paths"
            )
        resolved_run_id, resolution_error = _resolve_agent_run_id_for_file_tool(
            backend=backend,
            repo_root=repo_root,
            orchestration_id=orchestration_id,
            session_id=decoded.session_id,
            agent_session_id=decoded.agent_session_id,
            tool_name=tool_name,
        )
        if resolution_error is not None:
            return resolution_error
        if resolved_run_id is None:
            return HookDecision(action=HookDecisionAction.ALLOW)
        return _validate_write_targets(
            repo_root=repo_root,
            orchestration_id=orchestration_id,
            agent_run_id=resolved_run_id,
            targets=apply_patch_paths,
            tool_name=tool_name,
        )

    # step 2: Write / Edit / Read file tool guard
    if tool_name in {"Write", "Edit", "Read"}:
        if workflow_mode != "1" or not decoded.file_path:
            return HookDecision(action=HookDecisionAction.ALLOW)
        resolved_run_id, resolution_error = _resolve_agent_run_id_for_file_tool(
            backend=backend,
            repo_root=repo_root,
            orchestration_id=orchestration_id,
            session_id=decoded.session_id,
            agent_session_id=decoded.agent_session_id,
            tool_name=tool_name,
        )
        if resolution_error is not None:
            return resolution_error
        if resolved_run_id is None:
            return HookDecision(action=HookDecisionAction.ALLOW)
        if tool_name == "Read":
            agent_role = _get_agent_role_from_capability(repo_root, orchestration_id, resolved_run_id)
            read_decision = validate_read_access(
                repo_root,
                orchestration_id,
                resolved_run_id,
                decoded.file_path,
                agent_role=agent_role,
                session_id=decoded.session_id,
            )
            _log_read_decision(
                repo_root=repo_root,
                orchestration_id=orchestration_id,
                agent_run_id=resolved_run_id,
                tool_name=tool_name,
                path=decoded.file_path,
                decision=read_decision,
            )
            return read_decision
        # Write / Edit: on a manifest match, return permissionDecision=allow to bypass
        # the harness's permission prompt. A manifest mismatch propagates as BLOCK.
        write_decision = _validate_write_targets(
            repo_root=repo_root,
            orchestration_id=orchestration_id,
            agent_run_id=resolved_run_id,
            targets=[decoded.file_path],
            tool_name=tool_name,
        )
        if write_decision.action == HookDecisionAction.ALLOW:
            return HookDecision(
                action=HookDecisionAction.ALLOW_AUTO_APPROVE,
                reason=write_decision.reason,
                additional_context=write_decision.additional_context,
                continue_processing=write_decision.continue_processing,
                audit_detail={
                    "policy": "output_manifest_write_allow",
                    "tool_name": tool_name,
                    "file_path": decoded.file_path,
                    "agent_run_id": resolved_run_id,
                },
            )
        return write_decision

    # step 2b: Grep / Glob search-root guard
    if tool_name in _PATH_SEARCH_TOOLS:
        if workflow_mode != "1":
            return HookDecision(action=HookDecisionAction.ALLOW)
        return _evaluate_grep_glob_read_policy(
            decoded=decoded,
            repo_root=repo_root,
            orchestration_id=orchestration_id,
            backend=backend,
            tool_name=tool_name,
        )

    # step 3: Bash/Shell read/write guard
    if tool_name in {"Bash", "bash", "Shell", "shell"}:
        common_decision = evaluate_common_policy(decoded)
        if common_decision.action == HookDecisionAction.BLOCK:
            return common_decision
        if workflow_mode != "1":
            return common_decision
        resolved_run_id: str | None = None
        # Resolve before the read-only fast path only for Codex. A Codex pure
        # leaf can run `cat` in its read-only sandbox; unlike the Read tool,
        # that command would otherwise bypass the empty read manifest.
        if backend.strip().lower() == "codex":
            resolved_run_id, resolution_error = _resolve_agent_run_id_for_file_tool(
                backend=backend,
                repo_root=repo_root,
                orchestration_id=orchestration_id,
                session_id=decoded.session_id,
                agent_session_id=decoded.agent_session_id,
                tool_name=tool_name,
            )
            # Codex leaves can read the repository through Shell even in a
            # read-only sandbox.  Do not allow a missing or ambiguous session
            # mapping to bypass the pure-leaf boundary via the read-only fast
            # path.  Non-file-access diagnostic events must be handled outside
            # this PreToolUse policy.
            if resolution_error is not None:
                return resolution_error
            if resolved_run_id and _is_pure_readonly_capability(
                repo_root, orchestration_id, resolved_run_id
            ):
                return HookDecision(
                    action=HookDecisionAction.BLOCK,
                    reason="pure-function leaves may not invoke Bash or Shell; use only the host-inlined context",
                    continue_processing=False,
                )
        # Read-manifest guard for Bash reads. This runs BEFORE the read-only
        # auto-approve below: a command the manifest rejects must never be
        # auto-approved, which is exactly the historical behavior this changes.
        read_decision, resolved_run_id = _evaluate_bash_read_manifest_policy(
            decoded=decoded,
            repo_root=repo_root,
            orchestration_id=orchestration_id,
            backend=backend,
            resolved_run_id=resolved_run_id,
        )
        if read_decision is not None:
            return read_decision
        write_targets = _detect_bash_write_targets(decoded.command)
        if not write_targets:
            # Purely read-only command: if it is a provably-safe composition,
            # auto-approve to bypass the harness's native `;`/pipe permission
            # decomposition (the source of compound-command friction). Anything
            # not proven safe falls back to the allowlist-governed path.
            if (
                common_decision.action == HookDecisionAction.ALLOW
                and _is_auto_approvable_readonly_bash(decoded.command)
            ):
                return HookDecision(
                    action=HookDecisionAction.ALLOW_AUTO_APPROVE,
                    reason="read-only Bash composition auto-approved (no file writes)",
                    audit_detail={
                        "policy": "bash_readonly_auto_approve",
                        "tool_name": "Bash",
                        "command": decoded.command,
                    },
                )
            return common_decision
        if resolved_run_id is None:
            resolved_run_id, resolution_error = _resolve_agent_run_id_for_file_tool(
                backend=backend,
                repo_root=repo_root,
                orchestration_id=orchestration_id,
                session_id=decoded.session_id,
                agent_session_id=decoded.agent_session_id,
                tool_name=tool_name,
            )
            if resolution_error is not None:
                return resolution_error
        if resolved_run_id is None:
            return HookDecision(
                action=HookDecisionAction.BLOCK,
                reason=f"session-to-run mapping not found. {WRITE_HINT}",
                continue_processing=False,
            )
        write_decision = _validate_write_targets(
            repo_root=repo_root,
            orchestration_id=orchestration_id,
            agent_run_id=resolved_run_id,
            targets=write_targets,
            tool_name=tool_name,
            bash_command=decoded.command,
        )
        if write_decision.action == HookDecisionAction.BLOCK:
            return write_decision
        return common_decision
    return None


def _emit_hook_response(
    exit_code: int,
    stdout_text: str,
    *,
    event_name: HookEventName | None = None,
) -> int:
    suppress_stdout = event_name == HookEventName.STOP and exit_code == 0
    if stdout_text and not suppress_stdout:
        sys.stdout.write(stdout_text + "\n")
    if exit_code != 0:
        message = "hook failed"
        if stdout_text:
            try:
                body = json.loads(stdout_text)
                if isinstance(body, dict):
                    reason = body.get("reason")
                    if isinstance(reason, str) and reason.strip():
                        message = reason.strip()
                    else:
                        decision = body.get("decision")
                        if isinstance(decision, str) and decision.strip():
                            message = f"hook decision={decision.strip()}"
            except json.JSONDecodeError:
                message = stdout_text.strip() or message
        sys.stderr.write(message + "\n")
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", required=True, choices=["codex", "claude"])
    parser.add_argument("--event")
    parser.add_argument("--input-json")
    parser.add_argument("--repo-root")
    args = parser.parse_args(argv)

    payload: dict[str, Any] = {}
    event_name: HookEventName = HookEventName.STOP
    try:
        payload = _load_payload(args)
        if args.repo_root:
            payload = dict(payload)
            payload["repo_root"] = args.repo_root
        event_name = _resolve_event_name(args, payload)
        adapter = _adapter_for_backend(args.backend)
        orchestration_id = _extract_orchestration_id(payload)
        if orchestration_id is None:
            orchestration_id = "_global"
        if orchestration_id == "_global":
            exit_code, stdout_text = adapter.encode_decision(
                HookDecision(action=HookDecisionAction.ALLOW), event_name=event_name
            )
            return _emit_hook_response(exit_code, stdout_text, event_name=event_name)

        repo_root = _resolve_repo_root(payload, backend=args.backend)

        if args.backend == "codex":
            require_flag = os.environ.get("METDSL_REQUIRE_CODEX_HOOKS_FEATURE", "1").strip().lower()
            if require_flag not in {"0", "false", "no"}:
                # Read-only: the codex-hooks feature is probed HOST-side by the conductor
                # (Conductor._ensure_codex_feature_cache) and written to a cache at the
                # orchestration-dir root, which is read-only inside the bwrap sandbox. The
                # hook never probes or writes — a confined leaf must not be able to forge
                # `enabled=true` (the cache used to live in the leaf-writable hooks/ dir).
                # A missing/invalid/disabled cache fail-closes: under mandatory bwrap a
                # codex leaf is only launched after the host wrote the cache, so absence
                # means the host did not certify the feature (or the file was tampered).
                try:
                    cached = read_codex_feature_cache(
                        repo_root=repo_root,
                        orchestration_id=orchestration_id,
                    )
                except ValueError as exc:
                    cached = None
                    detail = f"codex feature cache malformed: {exc}"
                else:
                    detail = (
                        "codex feature cache missing (host did not certify the hooks "
                        "feature for this orchestration)"
                        if cached is None
                        else cached[1]
                    )
                enabled = bool(cached[0]) if cached is not None else False
                if not enabled:
                    decision = _decision_error(
                        "hooks feature is required but not enabled: " + detail
                    )
                    _append_hook_audit(
                        backend=args.backend,
                        event_name=event_name,
                        payload=payload,
                        decision=decision,
                        orchestration_id_override=orchestration_id,
                    )
                    exit_code, stdout_text = adapter.encode_decision(decision, event_name=event_name)
                    return _emit_hook_response(exit_code, stdout_text, event_name=event_name)

        if event_name not in adapter.supported_events():
            decision = _decision_error(
                f"backend={args.backend} does not support event={event_name.value}"
            )
        else:
            decoded = adapter.decode_event(event_name.value, payload)
            decision = _evaluate_pre_command_file_access_policy(
                decoded=decoded,
                repo_root=repo_root,
                orchestration_id=orchestration_id,
                backend=args.backend,
            )
            if decision is None:
                decision = evaluate_common_policy(decoded)
        _append_hook_audit(
            backend=args.backend,
            event_name=event_name,
            payload=payload,
            decision=decision,
            orchestration_id_override=orchestration_id,
        )
        exit_code, stdout_text = adapter.encode_decision(decision, event_name=event_name)
    except Exception as exc:
        fallback_adapter = _adapter_for_backend(args.backend)
        decision = _decision_error(f"hook entrypoint failure: {exc}")
        fallback_orchestration_id = _extract_orchestration_id(payload) or "_global"
        _append_hook_audit(
            backend=args.backend,
            event_name=event_name,
            payload=payload,
            decision=decision,
            orchestration_id_override=fallback_orchestration_id,
        )
        exit_code, stdout_text = fallback_adapter.encode_decision(decision, event_name=event_name)
    return _emit_hook_response(exit_code, stdout_text, event_name=event_name)


if __name__ == "__main__":
    raise SystemExit(main())
