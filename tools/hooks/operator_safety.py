#!/usr/bin/env python3
"""The two hook policies that are not about a leaf.

Everything else in `tools/hooks/` decides what a WORKFLOW LEAF may do, and is gated on
the environment a leaf runs under. These two are not: they refuse a command that
destroys the operator's own checkout, or that turns a verify gate off, whoever issued
it.

They live here, alone, so the DEV entrypoint (`tools/hooks/dev_cli.py`) can apply them
WITHOUT importing `tools/hooks/cli.py` or `tools/hooks/common.py` (issue #102). That
import boundary is the point of this module, not tidiness: the operator's interactive
session runs the working-tree copy of its hook, so a defect in the leaf-facing modules
would otherwise refuse the operator out of the very session they are editing in. It has
happened once (2026-08-26), and only stdlib imports belong here.

The rule text is defined ONCE, here. `tools/hooks/common.py::evaluate_common_policy`
wraps these into a `HookDecision` for the leaf path; `dev_cli` encodes them itself.

**Known over-refusal, and it fires in ordinary use.** The match is a substring of the
whole command, so a command that merely CONTAINS the text is refused too — a commit
message that quotes the rule, a heredoc that writes documentation about it, a grep for
it. Measured 2026-08-26: the commit that introduced this module was refused by it. The
leaf path has machinery for that (`_strip_quoted_strings`, heredoc blanking in
`common.py`), and this module deliberately does not import it — the boundary above is
worth more than the nuisance, the operator owns the machine and can rephrase, and the
failure direction is refusal rather than a missed one.
"""

from __future__ import annotations

from typing import Any

# Verify-bypass flags. `METDSL_WORKFLOW_EXEC_MODE` unset means dev, which is the
# operator's ordinary case and the one this refusal is for.
VERIFY_BYPASS_TOKENS: tuple[str, ...] = (
    "--allow-missing-orchestration",
    "--allow-missing-llm-review",
    "--allow-soft-fail",
    "--allow-soft-verify",
    "--ignore-verify-fail",
    "--force-pass",
)


def operator_safety_violation(
    command: str,
    *,
    workflow_exec_mode: str | None,
) -> tuple[str, dict[str, Any]] | None:
    """Return `(reason, audit_detail)` for a refused command, or None.

    `workflow_exec_mode` is the raw `METDSL_WORKFLOW_EXEC_MODE` value (None when
    unset). Callers pass it in rather than reading the environment here, so the rule
    stays a pure function of its inputs and a test can drive both modes without
    patching a process.
    """
    if not command:
        return None
    lowered = command.lower()

    if "git reset --hard" in lowered:
        return (
            "blocked by common hook policy: git reset --hard is forbidden",
            {"policy": "forbid_git_reset_hard", "command": command},
        )

    mode = (workflow_exec_mode or "dev").strip().lower()
    if mode == "dev":
        matched = [token for token in VERIFY_BYPASS_TOKENS if token in lowered]
        if matched:
            return (
                "blocked by common hook policy: dev mode forbids verify bypass flags: "
                + ", ".join(matched),
                {
                    "policy": "forbid_verify_bypass_flags_in_dev_mode",
                    "workflow_mode": mode,
                    "command": command,
                    "matched_tokens": matched,
                },
            )
    return None
