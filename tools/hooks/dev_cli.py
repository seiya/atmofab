#!/usr/bin/env python3
"""Hook entrypoint for an OPERATOR's interactive session (the DEV layer).

`.claude/settings.json` and `.codex/hooks.json` register this; a workflow leaf never
reaches it and it never reaches a leaf policy. The leaf entrypoint is
`tools/hooks/cli.py`, whose settings sources are `leaf_config/claude/settings.json` and
`leaf_config/codex/hooks.json`. Issue #102 separated the two; before it, one entrypoint
served both and told them apart by the environment.

**This module imports the standard library and `tools.hooks.operator_safety`, and
nothing else. Keep it that way.** The operator's session runs the working-tree copy of
its own hook, so anything this file imports can refuse the operator out of the session
they are editing in — measured 2026-08-26, when a half-applied edit to
`tools/hooks/cli.py` made every tool call in an interactive session fail and the session
had to be repaired from outside. That is the whole reason this file duplicates a little
protocol encoding instead of importing the adapters;
`tools/tests/test_hooks_dev_cli.py` pins its output against the real adapters so the two
cannot drift silently.

What it enforces is `tools/hooks/operator_safety.py`, and only that: `git reset --hard`,
and the verify-bypass flags in dev mode. Everything else in `tools/hooks/` decides what
a LEAF may do and does not apply to the operator, who owns the machine.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from tools.hooks.operator_safety import operator_safety_violation

# The event spellings that carry a command. Both backends' names, normalized the way
# `tools/hooks/common.py::normalize_hook_event_name` does, but spelled here so this
# module keeps its import boundary.
_PRE_COMMAND_EVENTS = frozenset({"pretooluse", "pre_tool_use", "precommandexecute"})
_PERMISSION_REQUEST_EVENTS = frozenset({"permissionrequest", "permission_request"})


def _normalize_event(raw: str) -> str:
    return (raw or "").strip().lower().replace("-", "_")


def _payload_field(payload: dict[str, Any], key: str) -> Any:
    value = payload.get(key)
    if value is not None:
        return value
    inner = payload.get("payload")
    if isinstance(inner, dict):
        return inner.get(key)
    return None


def _extract_command(payload: dict[str, Any]) -> str:
    command = _payload_field(payload, "command")
    if isinstance(command, str) and command.strip():
        return command.strip()
    tool_input = _payload_field(payload, "tool_input")
    if isinstance(tool_input, dict):
        inner = tool_input.get("command")
        if isinstance(inner, str) and inner.strip():
            return inner.strip()
    return ""


def _encode_block(backend: str, event: str, reason: str) -> tuple[int, str]:
    """Mirror of the adapters' BLOCK encodings, pinned by this module's own test."""
    if backend == "codex" and event in _PERMISSION_REQUEST_EVENTS:
        # PermissionRequest consumes the structured decision; exit 2 would report the
        # hook as failed and can discard it.
        return 0, json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": {"behavior": "deny", "message": reason},
                }
            },
            ensure_ascii=False,
        )
    body: dict[str, Any] = {"decision": "block", "reason": reason}
    if backend == "codex":
        body["continue_processing"] = False
    return 2, json.dumps(body, ensure_ascii=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", required=True, choices=["codex", "claude"])
    parser.add_argument("--event")
    parser.add_argument("--input-json")
    parser.add_argument("--repo-root")
    args = parser.parse_args(argv)

    # Anything unparseable is ALLOWED, and deliberately so: this hook guards the
    # operator's own checkout against two commands, and refusing a session because its
    # payload had an unexpected shape is the failure mode this module exists to avoid.
    # The leaf entrypoint fails closed; this one does not.
    try:
        raw = args.input_json if args.input_json else sys.stdin.read()
        payload = json.loads(raw) if raw and raw.strip() else {}
        if not isinstance(payload, dict):
            return 0
        event = _normalize_event(args.event or str(_payload_field(payload, "event_name") or ""))
        if event not in _PRE_COMMAND_EVENTS and event not in _PERMISSION_REQUEST_EVENTS:
            return 0
        command = _extract_command(payload)
        violation = operator_safety_violation(
            command, workflow_exec_mode=os.environ.get("METDSL_WORKFLOW_EXEC_MODE")
        )
        if violation is None:
            return 0
        reason = violation[0]
        exit_code, stdout_text = _encode_block(args.backend, event, reason)
    except Exception:  # noqa: BLE001 - see the ALLOW-on-anything note above
        return 0
    sys.stdout.write(stdout_text + "\n")
    if exit_code != 0:
        sys.stderr.write(reason + "\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
