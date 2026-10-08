#!/usr/bin/env python3
"""The two hook policies that are not about a leaf.

They refuse a command that destroys the operator's own checkout, or that turns a verify
gate off, whoever issued it. Until Z4 (issue #171) they were the only policies in
`tools/hooks/` with that audience — everything else there decided what a WORKFLOW LEAF
may do and was gated on the environment a leaf runs under. Those went with the agentic
leaf; these two did not, because their subject is the operator's machine.

They live in a module of their own so the DEV entrypoint (`tools/hooks/dev_cli.py`) can
apply them without importing anything heavier (issue #102). That import boundary is the
point of this module, not tidiness: the operator's interactive session runs the
working-tree copy of its hook, so a defect in a module it imports would otherwise refuse
the operator out of the very session they are editing in. It has happened once
(2026-08-26), and only stdlib imports belong here.

The rule text is defined ONCE, here; `dev_cli` encodes the decision itself.

**Both rules match raw text, with no shell parsing.** The hard-reset rule matches a
substring of the whole command, so a command that merely CONTAINS the text is refused too —
a commit message that quotes the rule, a heredoc that writes documentation about it, a grep
for it. Measured 2026-08-26: the commit that introduced this module was refused by it. It
stands because the failure direction is refusal rather than a missed one and the operator
can rephrase. The verify-bypass rule is narrower since issue #445: it refuses only a command
whose text names the validator that defines the flags AND carries one of them. A grep for a
flag over `docs/`, an echo of a document quoting one, and a flag given to another program
pass; a grep for a flag IN the validator's own source names both and is refused — the same
over-refusal as the hard-reset rule, with the same answer (rephrase).

**What the verify-bypass rule does not see, and why that is accepted.** It reads text, so a
command that never spells both halves passes: the script name held in a shell variable, a
flag assembled from pieces, and an argparse abbreviation of a flag (the validator's parser
keeps `allow_abbrev`). A `python3 -c` that imports the validator by module name and passes a
flag in full IS refused — both spellings are in its text. The
rule guards the operator's own development session against an accidental bypass, and
`AGENTS.md` §Development premises puts a defense against the operator outside the defended
set — no workflow leaf issues a command at all.
"""

from __future__ import annotations

from typing import Any

# The validator whose flags these are, and the flags themselves — exactly the two
# `tools/validate_pipeline_semantics.py`'s argument parser defines that waive a verify
# requirement. A flag no tool accepts is not listed: refusing it guards nothing.
VERIFY_BYPASS_SCRIPT = "validate_pipeline_semantics"
VERIFY_BYPASS_TOKENS: tuple[str, ...] = (
    "--allow-missing-orchestration",
    "--allow-missing-llm-review",
)


def operator_safety_violation(command: str) -> tuple[str, dict[str, Any]] | None:
    """Return `(reason, audit_detail)` for a refused command, or None.

    A pure function of its argument, so a test drives every branch without patching a
    process.
    """
    if not command:
        return None
    lowered = command.lower()

    if "git reset --hard" in lowered:
        return (
            "blocked by common hook policy: git reset --hard is forbidden",
            {"policy": "forbid_git_reset_hard", "command": command},
        )

    if VERIFY_BYPASS_SCRIPT in lowered:
        matched = [token for token in VERIFY_BYPASS_TOKENS if token in lowered]
        if matched:
            return (
                "blocked by common hook policy: the verify bypass flags of "
                f"{VERIFY_BYPASS_SCRIPT} are forbidden: " + ", ".join(matched),
                {
                    "policy": "forbid_verify_bypass_flags",
                    "command": command,
                    "matched_tokens": matched,
                },
            )
    return None
