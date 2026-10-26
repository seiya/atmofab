#!/usr/bin/env python3
"""Pure-function leaf transport (Z2) — host-mediated pure-leaf helpers.

A *pure leaf* is an LLM stage run as a host-mediated pure function
(`docs/design/zero_base_architecture.md` A2): the host assembles a closed context, the
model returns exactly one typed JSON document, and the host validates and writes it. The
model holds no workflow-gate or repository-write authority. Claude launches with tools and
slash commands disabled and returns through the `--output-format json` result envelope;
Codex runs a tool-bearing CLI inside a read-only sandbox and the host parses its one reply.

This module is the STAGE-AGNOSTIC substrate of that channel: the launch flag set, the
result-envelope parser, the single-document extractor with its truncation classifier, and
the verify verdict contract. It is deliberately free of any `generate`-specific knowledge
(the `CodegenBundle` producer lives in `tools/workflow_conductor.py`; the bundle contract
in `tools/codegen_bundle.py`). Z1 (`compile.generate` / `compile.verify`, issue #168) reuses
this transport unchanged, which is what it was written stage-agnostic for; Z3
(`validate.judge`) is still to come.

**Inert at introduction (M-A).** No caller passes `pure=True` yet; `Conductor.leaf_command`
grows the branch, the producer/verify inversions arrive in M-C/M-D. The functions here are
pure and unit-tested ahead of the wiring so the transport is settled before the dominant
leaf depends on it.

Idiom (`tools/codegen_bundle.py`, `tools/meta_contracts.py`): stdlib only, pure functions,
an explicit `_MISSING` sentinel so a present JSON `null` is never confused with an absent
key, and prefix-free violation clauses the caller frames in its own reporting idiom.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from typing import Any, NamedTuple

# The prompt-contract version stamped into `bundle_meta.json` and the launch record, so a
# contract change is an observable event (A7). Bumped when the pure prompt templates, the
# fixed `PURE_SYSTEM_PROMPT`, or the transport's request shape change in a way that affects
# producer behavior.
PURE_PROMPT_CONTRACT_VERSION = "pure-84"

# Claude's pure leaf system prompt is REPLACED with this fixed string via `--system-prompt`. The
# default Claude Code system prompt injects per-machine DYNAMIC sections (cwd, environment,
# memory paths, git status); `--exclude-dynamic-system-prompt-sections` only relocates them
# into the first user message (still host-varying). A fixed `--system-prompt` takes them out
# of the system prompt, but not out of the request: the CLI still adds text of its own, so the
# model's total input is NOT a byte-stable function of the host-assembled `-p` body alone.
# `docs/ORCHESTRATION.md` §pure-function leaf ("What the model receives, per transport") is
# canonical for what the CLI adds and what that costs (issue #454).
# Deliberately minimal: the full persona, output contract, and inlined context live in the `-p`
# body (rendered in M-B); this string is the host's part of the system channel and states the
# pure-function shape. A change here is a prompt-contract change
# (bump `PURE_PROMPT_CONTRACT_VERSION`).
PURE_SYSTEM_PROMPT = (
    "You are a host-mediated pure function. You have no tools, no filesystem, and no shell. "
    "The user message is a complete, self-contained task. Return exactly one JSON document as "
    "your entire reply, with no surrounding prose or explanation."
)

# Schema-violation repair is a bounded in-conversation continuation (warm `--resume
# --fork-session`): the producer sees its own prior answer plus the violation excerpt and
# re-emits. Two turns caps the cost; exhaustion fails the substep with the terminal category
# recorded, never a silent "no bundle".
MAX_BUNDLE_REPAIR_TURNS = 2

# The first line of every host-rendered pure launch prompt (`-p` body). The host anchors pure
# detection on this being the very first line (`startswith`), so a prompt that merely mentions
# the string inside its body never reads as pure, and a pure request whose prompt does not open
# with it is rejected. Defined HERE, once, so `orchestration_runtime.py` and
# `validate_pipeline_semantics.py` import the ONE literal — the pure prompt templates pin their
# line 0 against it via a parity test. The slim-repair sentinel, whose anchor this mirrored, was
# COPIED into each module instead; it survives in the validator alone, for the pre-Z4 records it
# still has to classify (issue #171). A change here is a prompt-contract change (bump
# `PURE_PROMPT_CONTRACT_VERSION`).
PURE_PROMPT_SENTINEL = "Pure-function leaf turn (host-mediated)"

# Data-only fence around an untrusted document inlined into a pure launch prompt (`tests.md`,
# `controlled_spec.md`, the IR, the bundle under repair). The prompt directs the closed-context
# leaf not to use tools; Claude enforces that transport strictly, while Codex runs inside its
# read-only sandbox. An injected instruction can therefore at most
# corrupt the returned bundle content (caught downstream by `validate_bundle` / the gates /
# verify), never gain repository write authority. The fence also tells the model to treat the
# span as DATA and, load-bearingly, marks the region for the gate-allowlist lint's scan carve-out
# (a `validate_pipeline_semantics --stage` string legitimately appearing inside an inlined doc
# must not fail-close the launch). The host neutralizes any embedded copy of these markers in a
# document body before fencing it.
PURE_DOC_FENCE_BEGIN = "----- BEGIN PURE INPUT DOCUMENT (data only) -----"
PURE_DOC_FENCE_END = "----- END PURE INPUT DOCUMENT -----"

# The `leaf_mode` value that selects the pure-function path. Single source (imported by
# orchestration_runtime and validate_pipeline_semantics) so the sentinel cannot drift between
# the producer and the validators — a typo in one inlined `"pure"` literal would silently split
# the detection. A `PURE_CAPABILITY_MODE = "pure_readonly"` tag stood beside it for the
# capability document a pure launch stamped; that document is deleted (issue #171 PR-2).
PURE_LEAF_MODE = "pure"


def is_pure_request(request_payload: Any) -> bool:
    """True when a launch request selects the pure-function path (`leaf_mode == "pure"`,
    case/space-insensitively). THE single detection predicate — orchestration_runtime and
    validate_pipeline_semantics both delegate here so the producer and the validators cannot
    disagree about what "pure" is. An absent/other `leaf_mode` is not a pure request: since Z4
    (issue #171) it means a deterministic substep or a malformed request."""
    if not isinstance(request_payload, dict):
        return False
    return str(request_payload.get("leaf_mode", "")).strip().lower() == PURE_LEAF_MODE


# --------------------------------------------------------------------------------------
# The `workshop leaf` (issue #534; `docs/design/producer_workshop_leaf.md`): a PRODUCING leaf
# that edits a host-seeded work tree and reaches its substep's host operations through the
# wrapper's MCP endpoint (`tools/workshop_mcp.py`), instead of returning one document. It shares the
# pure leaf's host-rendered prompt and its launch-record shape — no write authority under the
# checkout, the host writes every artifact from the snapshot after the leaf exits — and differs
# in what it holds: file tools confined to its tree, and the wrapper.
# --------------------------------------------------------------------------------------

#: The `leaf_mode` value that selects the workshop path.
WORKSHOP_LEAF_MODE = "workshop"

#: The PRODUCING pairs a workshop leaf may serve. Step 1 of the design note's migration order
#: adopted `generate.generate`, step 2 `compile.generate`. A producing lineage is served the
#: wrapper's operations over a per-launch MCP endpoint.
WORKSHOP_PRODUCING_PAIRS: frozenset[tuple[str, str]] = frozenset({
    ("generate", "generate"), ("compile", "generate")})

#: The JUDGING pairs a workshop leaf may serve (step 3; adopted 2026-10-25): the READING kind —
#: a seeded tree, file tools, NO wrapper operation and no endpoint, one output document at the
#: tree root (`docs/ORCHESTRATION.md` §workshop leaf). The judge contract already forbids running a
#: validator or a gate, so there is nothing for an endpoint to serve.
WORKSHOP_READING_PAIRS: frozenset[tuple[str, str]] = frozenset({
    ("compile", "verify"), ("generate", "verify"), ("validate", "judge")})

#: Every `(step, substep)` pair a workshop leaf may serve (`tools/llm_config.py` refuses any
#: other entry, and mirrors this table as `WORKSHOP_LEAF_SUBSTEPS`).
WORKSHOP_LAUNCH_PAIRS: frozenset[tuple[str, str]] = (
    WORKSHOP_PRODUCING_PAIRS | WORKSHOP_READING_PAIRS)


def is_workshop_reading_pair(step: Any, substep: Any) -> bool:
    """True when `(step, substep)` is one of `WORKSHOP_READING_PAIRS`, case/space-insensitively:
    the one predicate that says which KIND a workshop launch of that pair is."""
    return (str(step or "").strip().lower(),
            str(substep or "").strip().lower()) in WORKSHOP_READING_PAIRS


#: The prompt-contract version a workshop request carries in `prompt_contract_version` (the
#: field the pure request uses for its own). Its own counter, so a workshop template change
#: never moves `PURE_PROMPT_CONTRACT_VERSION` and the reverse. `workshop-7`: the `codex_cli`
#: reader row leads with the host's reading helper (`CODEX_READ_HELPER_TEXT`, a seed of every
#: codex reading tree) at the lowered `CODEX_READ_ADVICE_BYTES` (issue #534 PR-J3-a). `workshop-6`: the `codex_cli`
#: reader row names every output budget and the host's refusal of a cut read; the cut-read
#: repair's scope paragraph; and the verdict repair template's instruction slot, which renders a
#: schema repair as before on both providers (issue #547; `workshop-5` shipped with step 3 PR-J2,
#: main bb119b5a).
WORKSHOP_PROMPT_CONTRACT_VERSION = "workshop-7"

#: The first line of every host-rendered workshop launch prompt, anchored as the pure sentinel
#: is (`startswith`). A distinct literal, so a record's prompt says which of the two leaf
#: models it was written for and the validators compare that against the request.
WORKSHOP_PROMPT_SENTINEL = "Workshop leaf turn (host-seeded work tree)"

#: The system prompt a workshop leaf runs under (`--system-prompt`), for the reason
#: `PURE_SYSTEM_PROMPT` replaces the CLI's default: the default carries per-machine sections.
WORKSHOP_SYSTEM_PROMPT = (
    "You are the producer of a host-mediated workflow, working in a host-seeded work tree. "
    "Your file tools reach that tree and nothing else; you hold no shell. The host runs the "
    "operations you ask for through the `wrapper` tools and judges your tree after you "
    "finish. The user message is your complete task."
)

#: The system prompt a READING workshop leaf (a judging substep, step 3) runs under. Its own
#: text because the producer's names the `wrapper` tools, which a reading lineage is not
#: served.
WORKSHOP_READER_SYSTEM_PROMPT = (
    "You are a reviewer of a host-mediated workflow, working in a host-seeded work tree. Your "
    "file tools reach that tree and nothing else; you hold no shell and run no operation. The "
    "host reads the one document you write and judges nothing else. The user message is your "
    "complete task."
)

#: What a workshop launch prompt's `<tool_surface>` slot says the leaf holds, by the provider
#: the launch request names (`workshop_provider`): the one sentence of the contract that
#: differs by transport (`docs/design/producer_workshop_leaf.md` §Provider neutrality). A
#: provider with no row cannot serve a workshop launch (`_validate_pure_launch_request_payload`).
WORKSHOP_TOOL_SURFACE_TEXT: dict[str, str] = {
    "claude_cli": ("You have no shell: your file tools (Read, Write, Edit, Glob, Grep) reach "
                   "your work tree and nothing else."),
    "codex_cli": ("Your shell and patch tools run with your work tree as the working directory, "
                  "and the host reads your tree and nothing else you write. A command you run "
                  "there is not one of the host's operations: those are the `wrapper` MCP "
                  "server's tools (`mcp__wrapper__<operation>`)."),
}

#: The token budget codex applies to a direct `exec` result (and, by default, to each
#: `exec_command` / `write_stdin` / `wait` output): an output over it reaches the model with its
#: middle removed. Measured on codex-cli 0.164.0 (`test_bwrap_simulation.CodexReadingLeafCliTests`
#: MR5-e), as is the next constant.
CODEX_EXEC_OUTPUT_BUDGET_TOKENS = 10_000

#: How codex counts a token of command output: `original_token_count == ceil(bytes / 4)` on
#: every command measured (MR5-e (vi)), so a command whose captured output exceeds
#: `CODEX_EXEC_OUTPUT_BUDGET_BYTES` cannot have reached the model whole at the default budget
#: (MR5-e (vii): 40,000 bytes arrive whole, 40,001 cut).
CODEX_BYTES_PER_TOKEN = 4

#: The byte rule the host applies to a codex reading lineage's own capture of each command
#: (`workflow_conductor.Conductor._run_pure_reviewer_substep`, issue #547).
CODEX_EXEC_OUTPUT_BUDGET_BYTES = CODEX_EXEC_OUTPUT_BUDGET_TOKENS * CODEX_BYTES_PER_TOKEN

#: The size the codex reader row tells a leaf to keep each command's output under, and the page
#: of the host's reading helper (`CODEX_READ_HELPER_TEXT`): HALF the byte rule (5,000 tokens at
#: `CODEX_BYTES_PER_TOKEN`), not a margin under it, because a page must survive a budget the
#: leaf lowers on its own — codex closing run (3)'s judges passed `max_output_tokens` 7,800
#: (with 6,000 and 7,000 in the same launch: the findings of its repair launch,
#: `orch_20261025T224100Z_ea947f2e/launches/213e6ecf-….prompt.txt`), 7,500 and 8,500
#: (`src_20261025_003/verdict_meta.json`) to `exec_command` (issue #534 PR-J3-a), which the row tells it not to do; a page fits the
#: smallest of them (6,000 tokens is 24,000 bytes). It was 32,000 (four fifths of the rule) until `workshop-7`.
CODEX_READ_ADVICE_BYTES = CODEX_EXEC_OUTPUT_BUDGET_BYTES // 2

#: The tree name of the host's reading helper, a seed of every codex READING tree (issue #534
#: PR-J3-a): a host-placed name like `CONTRACT.md`, so it is reserved, re-placed on a warm
#: launch, and carried by no snapshot.
CODEX_READ_HELPER_NAME = "read.py"

#: The helper itself: a stdlib-only script that pages a file of the tree by whole lines, so
#: that everything ONE call prints — the lines and its one trailer line — is under
#: `CODEX_READ_ADVICE_BYTES` UTF-8 bytes, and says the command for the next page. It makes
#: compliance with the reader row the default; it replaces nothing the host checks: its output
#: is a command's output like any other, and the host's byte rule and rollout scan read it so.
#: The cap is the only number of more than one digit in the text (pinned in
#: `test_codex_read_helper`).
CODEX_READ_HELPER_TEXT = f'''"""Page a file of this work tree: python3 read.py <file> [<first-line>]

Prints whole lines from <first-line> (default 1) while everything one call prints stays under
{CODEX_READ_ADVICE_BYTES} bytes, then one line starting `== read.py` that says where the page
ended and the command for the next one. Written by the host; reads, never writes."""
import os
import sys

CAP = {CODEX_READ_ADVICE_BYTES}


def say(text):
    return ("== read.py " + text + "\\n").encode("utf-8", "replace")


def fail(text):
    sys.stdout.buffer.write(say(text))
    return 2


def main(argv):
    if len(argv) not in (2, 3):
        return fail("usage: python3 read.py <file> [<first-line>]")
    name = argv[1]
    try:
        first = int(argv[2]) if len(argv) == 3 else 1
    except ValueError:
        first = 0
    if first < 1:
        return fail(name + ": <first-line> must be a whole number, 1 or more")
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        return fail(name + ": cannot be read (" + (exc.strerror or "error") + ")")
    parts = data.split(b"\\n")
    lines = [part + b"\\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])
    total = len(lines)
    if total == 0:
        sys.stdout.buffer.write(say(name + ": empty file; end of file"))
        return 0
    if first > total:
        return fail(name + ": line " + str(first) + " is past the end (" + str(total)
                    + " lines)")
    page = []
    size = 0
    k = first - 1
    while k < total:
        line = lines[k]
        sep = b"" if line.endswith(b"\\n") else b"\\n"
        if k + 1 == total:
            end = say(name + ": lines " + str(first) + "-" + str(total) + " of " + str(total)
                      + "; end of file")
        else:
            end = say(name + ": lines " + str(first) + "-" + str(k + 1) + " of " + str(total)
                      + "; next: python3 read.py " + name + " " + str(k + 2))
        if size + len(line) + len(sep) + len(end) >= CAP:
            break
        page.append(line + sep)
        size += len(line) + len(sep)
        k += 1
    if not page:
        piece = CAP // 2
        size = len(lines[k].rstrip(b"\\n"))
        line = "sed -n '" + str(first) + "p' " + name + " | cut -b "
        sys.stdout.buffer.write(say(
            name + ": line " + str(first) + " is " + str(size) + " bytes, too long for one "
            + "page with this line; read it in pieces, one command each: `" + line + "1-"
            + str(piece) + "`, then `" + line + str(piece + 1) + "-" + str(2 * piece)
            + "`, and so on to byte " + str(size)
            + ("; after it: python3 read.py " + name + " " + str(first + 1)
               if first < total else "; after its pieces, end of file")))
        return 0
    if k == total:
        end = say(name + ": lines " + str(first) + "-" + str(total) + " of " + str(total)
                  + "; end of file")
    else:
        end = say(name + ": lines " + str(first) + "-" + str(k) + " of " + str(total)
                  + "; next: python3 read.py " + name + " " + str(k + 1))
    sys.stdout.buffer.write(b"".join(page) + end)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
'''

#: The `<tool_surface>` sentence of a READING workshop launch, keyed by provider as
#: `WORKSHOP_TOOL_SURFACE_TEXT` is. A provider with no row cannot serve a reading launch, and
#: `tools/llm_config.py` refuses a judging workshop entry on a provider this table does not name.
#: Each row carries that transport's PAGING rule, which differs and is measured (step 3 PR-J2,
#: `test_bwrap_simulation.CodexReadingLeafCliTests` MR5-c / MR5-e): claude's `Read` stops at a
#: token cap and says so; codex cuts the MIDDLE of an output over any of its budgets, so its last
#: line is the file's last line — the claude rule ("continue from the last line it returned")
#: would read as done there. A row's sentence about reading is a request only: on codex the host
#: also refuses a `pass` written in a turn whose commands printed more than the budget or reached
#: the model cut (issue #547, `tools/codex_rollout.py`), which is what the row's last sentence
#: states. The `codex_cli` row rests on MR5-a (no `mcp__` tool on the reading argv) and MR5-b (a
#: command-written document assembles).
WORKSHOP_READER_TOOL_SURFACE_TEXT: dict[str, str] = {
    "claude_cli": (WORKSHOP_TOOL_SURFACE_TEXT["claude_cli"]
                   + " A read of a file may stop before the end of the file and say so; "
                     "continue from the last line it returned until you reach the end."),
    "codex_cli": ("Your shell and patch tools run with your work tree as the working directory, "
                  "and the host reads the one document you write there and nothing else. You "
                  "are served no host operation: no `wrapper` server exists on this lineage, "
                  "and a command you run is not a check the host counts. Read every file "
                  f"through the host's helper, one file per command: `python3 "
                  f"{CODEX_READ_HELPER_NAME} <file>` prints the first page and the command for "
                  "the next; run that command until it says `end of file`. Everything one call "
                  f"of it prints is under {CODEX_READ_ADVICE_BYTES:,} bytes, so it reaches you "
                  "whole at the default budget. Do not read two files in one command, do not "
                  "`cat` or `sed` a file whole, and do not pass `max_output_tokens` or "
                  "`max_tokens` (a lower budget cuts a page; a higher one does not help). Run each "
                  "command in an `exec` of its own and print only its output: what one `exec` "
                  "prints, all its `text()` together, is one output too, so pages printed "
                  f"together by one `exec` can arrive cut. `{CODEX_READ_HELPER_NAME}` is a host "
                  "seed like `CONTRACT.md` — the host placed it, though the list of seeds in "
                  "your work-tree contract does not name it: do not edit it. Why: "
                  "everything a command "
                  "prints reaches you through a token budget — the `exec` result's own "
                  f"({CODEX_EXEC_OUTPUT_BUDGET_TOKENS:,} tokens), `exec_command`'s and "
                  "`write_stdin`'s `max_output_tokens`, `wait`'s `max_tokens` — and an output "
                  "over its budget arrives with its MIDDLE removed: the first and last lines, "
                  "and a `…N tokens truncated…` marker where the rest was cut, usually under a "
                  "`Warning: truncated output (original token count: N)` header. That holds for "
                  "a `cat`, a `sed`, an `rg` and a script of your own alike; the rows between "
                  "the kept head and tail were never shown to you, and a cut output is a read "
                  f"you have not made. A token here is {CODEX_BYTES_PER_TOKEN} bytes, and two "
                  "files read in one command are one output. Print a script's own result in pieces "
                  "the helper's size, one `exec` per piece, and read for yourself: what a "
                  "sub-agent reads is never shown to you. The host reads its own record of what "
                  "each command printed and of what reached you: a `pass` written in a turn in "
                  f"which any command's output exceeded {CODEX_EXEC_OUTPUT_BUDGET_BYTES:,} "
                  "bytes, any output reached you cut, or a read was delegated to a sub-agent is "
                  "refused, and the review is run again from the seeds with those reads named."),
}

#: The MCP server name the wrapper is registered under in `--mcp-config`. The CLI names its
#: tools `mcp__<server>__<tool>`, and `--allowedTools mcp__<server>` grants all of them.
WORKSHOP_MCP_SERVER_NAME = "wrapper"

#: The built-in tools a workshop leaf holds (`--tools`). File tools only: `--restricted`
#: confines them to the working directory, which is the tree.
WORKSHOP_FILE_TOOLS: tuple[str, ...] = ("Read", "Write", "Edit", "Glob", "Grep")


def is_workshop_request(request_payload: Any) -> bool:
    """True when a launch request selects the workshop path (`leaf_mode == "workshop"`,
    case/space-insensitively). The workshop twin of `is_pure_request`, and like it the single
    detection predicate the producer and the validators share."""
    if not isinstance(request_payload, dict):
        return False
    return str(request_payload.get("leaf_mode", "")).strip().lower() == WORKSHOP_LEAF_MODE


def is_host_rendered_request(request_payload: Any) -> bool:
    """True for a pure OR a workshop request: a launch whose prompt the host renders from a
    template and whose artifacts the host writes after the leaf exits. The question every
    launch-record carve-out that is about "the leaf holds no write authority" asks; a question
    about what the leaf HOLDS (no tool at all) asks `is_pure_request`."""
    return is_pure_request(request_payload) or is_workshop_request(request_payload)


def workshop_mcp_config_json(url: str) -> str:
    """The `--mcp-config` JSON string naming the wrapper's endpoint for one launch."""
    return json.dumps({"mcpServers": {WORKSHOP_MCP_SERVER_NAME: {"type": "http", "url": url}}},
                      sort_keys=True)

# A JSON `null` is a PRESENT value, not an absent key. The two are distinguished with an
# explicit sentinel (the `tools/codegen_bundle.py` idiom) so an envelope carrying
# `"result": null` falls through to its own handling instead of being read as "no key".
_MISSING = object()


# --------------------------------------------------------------------------------------
# Response-failure categories. `extract_json_document` returns one of these when the model's
# answer is not a single parseable JSON document; the caller (M-C) routes every category to a
# bounded repair turn and records the terminal one in `bundle_meta.json`. Keeping the two
# transport-level categories here (not as magic strings at the call site) lets the extractor
# and its tests agree on the exact tokens.
# --------------------------------------------------------------------------------------

RESPONSE_UNPARSEABLE = "pure_response_unparseable"
RESPONSE_TRUNCATED = "pure_response_truncated"
RESPONSE_CATEGORIES: tuple[str, ...] = (RESPONSE_UNPARSEABLE, RESPONSE_TRUNCATED)


# --------------------------------------------------------------------------------------
# Launch flags
# --------------------------------------------------------------------------------------

def pure_leaf_flags() -> list[str]:
    """The `claude -p` flags that turn a leaf into a pure function.

    A fresh list per call (never a shared constant) so a caller that splices it into an
    argv cannot mutate the canonical set. The set is the load-bearing contract:

    - `--restricted`       ignores the user, project and local SETTINGS FILES (managed
                            settings and `--settings` still apply; the leaf passes no
                            `--settings`). The leaf's `HOME` is the operator's, so without it
                            the operator's `~/.claude/settings.json` decided an unpinned
                            leaf's model, remapped a declared alias through its `env` block,
                            and redirected the endpoint (measured on CLI 2.1.294 against a
                            loopback endpoint; issues #446, #453). It does not touch the
                            CLI's credential store or the session transcript under
                            `~/.claude`, which are not settings files: subscription OAuth and
                            the warm `--resume` both work under it (measured, issue #453). A
                            credential the SETTINGS file supplies — `apiKeyHelper`, an `env`
                            block's `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` — is ignored
                            with the rest of the file, so a leaf authenticates through the
                            credential store or not at all ("Not logged in", measured). Nor
                            does it touch the CLI's global config (`~/.claude.json`, or the
                            legacy `~/.claude/.config.json`), whose `env` block still reaches
                            the leaf; preflight refuses one
                            (`claude_global_config_env_absent`, issue #453). Its other
                            effects — removing the code-running built-in tools, confining
                            the file tools — are already moot under `--tools ""`.
    - `--safe-mode`        disables ALL ambient customizations — `CLAUDE.md`, skills,
                            plugins, MCP servers, custom commands/agents, and crucially the
                            configured HOOKS (the `UserPromptSubmit`
                            hook would otherwise fire on the `-p` prompt and inject context
                            or run side effects). This closes the customization half of
                            the context (A2): without it, `claude -p` loads `CLAUDE.md` and
                            runs the configured hooks. The text the CLI itself adds is not a
                            customization and stays (`docs/ORCHESTRATION.md` §pure-function
                            leaf, "What the model receives, per transport"). It disables
                            customizations, not settings (that is `--restricted`'s half).
                            Auth works normally — so subscription billing is preserved
                            (this is why `--safe-mode`, not `--bare`, which forces API-key auth
                            and would break subscription billing).
    - `--system-prompt <PURE_SYSTEM_PROMPT>` replaces the default system prompt, which
                            otherwise carries per-machine DYNAMIC sections (cwd, env, memory
                            paths, git status). `--safe-mode` does not remove those (they are
                            the base prompt, not a customization); replacing the system prompt
                            omits them from the system prompt. It does NOT make the input a
                            function of the `-p` body alone: the CLI still adds text of its
                            own (`docs/ORCHESTRATION.md` §pure-function leaf, "What the
                            model receives, per transport"; issue #454).
    - `--tools ""`         no file/shell/gate/write tool is available to the model
                            (`--safe-mode` disables customizations, not the built-in tools).
    - `--strict-mcp-config` defense-in-depth: no ambient MCP server even if a future
                            `--safe-mode` narrows its scope (the build-runtime server is a
                            deterministic-stage concern, never the producer's).
    - `--disable-slash-commands` defense-in-depth: no skill/slash command injects behavior.
    - `--output-format json` the answer arrives in a machine-parseable result envelope,
                            which is how the host reads `result` / `model` / `usage`
                            without touching the session transcript (~/.claude is not read).

    WHAT THIS SET DOES NOT DO, and it is deliberate: a pure leaf takes NO
    `--setting-sources` and gets NO private `CLAUDE_CONFIG_DIR`. `--safe-mode` disables the
    customizations a private home existed to keep out (CLAUDE.md, skills, hooks, MCP
    servers, …) and `--restricted` the settings files, and preflight refuses an `env` block
    in the CLI's global config, so what the leaf still reads from the operator's home is
    the CLI's credential store, its global state and its own transcript — which is why
    `HOME` stays on `LEAF_ENV_ALLOWLIST`. An unpinned leaf therefore runs the
    CLI's own default model, and a declared alias stays the CLI's alias
    (`orchestration_runtime.default_agent_model_for_backend`). Admin-managed settings
    still apply; they are the operator's machine and out of scope.

    `--session-id`, the warm-repair `--resume <arid> --fork-session`, and the trailing `-p`
    are added by `Conductor.leaf_command` around this set. `-p` takes no prompt argument:
    the prompt is written to the leaf's stdin, because a single argv element is capped at
    128 KiB and a node's prompt exceeds it. `--bare` and
    `--no-session-persistence` are intentionally NOT here: `--bare` forces API-key auth
    (breaking the subscription billing the operator requires), and
    `--no-session-persistence` would break the warm-resume repair path.
    """
    return ["--restricted", "--safe-mode", "--system-prompt", PURE_SYSTEM_PROMPT, "--tools", "",
            "--strict-mcp-config", "--disable-slash-commands", "--output-format", "json"]


def workshop_leaf_flags(mcp_config_json: str | None, *, reading: bool = False) -> list[str]:
    """The `claude -p` flags of a `workshop leaf` (issue #534). A fresh list per call.

    `reading=True` is the READING kind (a judging substep, step 3): no `mcp_config_json` (a
    reading lineage is served no endpoint), so `--mcp-config` and `--allowedTools
    mcp__wrapper` are dropped and `--strict-mcp-config` is kept, so no MCP server from any
    configuration is loaded; the system prompt is `WORKSHOP_READER_SYSTEM_PROMPT`. Everything
    else is the producer's set below. A producing call without the JSON, or a reading call with
    it, is refused: the two must agree or a reviewer would hold the wrapper, or a producer lack
    it.

    Measured on CLI 2.1.309 against a loopback API stand-in and a loopback wrapper endpoint
    (`tools/tests/test_bwrap_simulation.py` `WorkshopLeafCliTests`, unbilled):

    - `--restricted` as for the pure leaf (the operator's settings FILES are ignored), and
      here load-bearing in two more ways: it removes every code-running built-in tool, and it
      CONFINES the file tools to the working directory, which is the tree (a `Write` to
      `<tree>/../x` or to the CLI's own `config/` is refused, "outside ... --restricted
      confines the file tools to the working directory"). It also keeps out every instruction
      file the CLI discovers by itself — `CLAUDE.md`, `.claude/CLAUDE.md`, `CLAUDE.local.md`
      and `.claude/settings.json` in the tree, `CLAUDE.md` in `CLAUDE_CONFIG_DIR` — on a cold
      launch and on a warm resume (M2: none of five planted markers reached a request; with
      `--restricted` removed, all five did). No `--setting-sources` is needed beside it.
    - `--system-prompt <WORKSHOP_SYSTEM_PROMPT>` for the reason the pure leaf takes its own.
    - `--tools Read,Write,Edit,Glob,Grep` (`WORKSHOP_FILE_TOOLS`): the built-in tool list. The
      wrapper's MCP tools survive it (M5: the request lists the five file tools and
      `mcp__wrapper__<op>`).
    - `--mcp-config <json> --strict-mcp-config`: the wrapper's endpoint for this launch, and
      no other MCP server from any configuration (M3: `initialize`, `tools/list` and
      `tools/call` reach the endpoint, JSON — not SSE — responses accepted).
    - `--permission-mode acceptEdits --permission-prompts none --allowedTools mcp__wrapper`
      (M4): `acceptEdits` lets the file tools write inside the working directory with no
      prompt; an MCP call needs approval under every mode, and with `--permission-prompts
      none` nobody can give it, so it is denied unless `--allowedTools mcp__wrapper` grants
      the server's tools (measured: denied with the first two flags alone, and under
      `dontAsk`; executed with the grant).
    - `--disable-slash-commands`, `--output-format json` as for the pure leaf.

    It drops `--safe-mode`, which disables MCP servers, and `--tools ""`. The leaf runs with
    `CLAUDE_CONFIG_DIR` set to its lineage's private `config/` (`orchestration_runtime.
    _prepare_claude_workshop_home`), so the CLI's global config, transcripts and caches of this
    lineage live there and the operator's `~/.claude` is not bound at all; the operator's
    credentials file is bound over `config/.credentials.json`, read-write, because the CLI
    refreshes the OAuth token in place."""
    if reading:
        if mcp_config_json is not None:
            raise ValueError("workshop_leaf_flags: a reading lineage is served no endpoint")
        return ["--restricted", "--system-prompt", WORKSHOP_READER_SYSTEM_PROMPT,
                "--tools", ",".join(WORKSHOP_FILE_TOOLS), "--strict-mcp-config",
                "--permission-mode", "acceptEdits", "--permission-prompts", "none",
                "--disable-slash-commands", "--output-format", "json"]
    if not isinstance(mcp_config_json, str) or not mcp_config_json.strip():
        raise ValueError("workshop_leaf_flags requires the wrapper's --mcp-config JSON")
    return ["--restricted", "--system-prompt", WORKSHOP_SYSTEM_PROMPT,
            "--tools", ",".join(WORKSHOP_FILE_TOOLS),
            "--mcp-config", mcp_config_json, "--strict-mcp-config",
            "--permission-mode", "acceptEdits", "--permission-prompts", "none",
            "--allowedTools", f"mcp__{WORKSHOP_MCP_SERVER_NAME}",
            "--disable-slash-commands", "--output-format", "json"]


def workshop_mcp_url(mcp_config_json: str) -> str:
    """The wrapper endpoint's URL out of `workshop_mcp_config_json`'s JSON: the one value the
    codex launch carries (`codex_workshop_overrides`), so both CLIs are handed one endpoint
    description."""
    try:
        url = json.loads(mcp_config_json)["mcpServers"][WORKSHOP_MCP_SERVER_NAME]["url"]
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError(f"not a workshop --mcp-config JSON: {mcp_config_json!r}") from exc
    if not (isinstance(url, str) and url.startswith("http://127.0.0.1:")):
        raise ValueError(f"the wrapper endpoint is a loopback http URL (got {url!r})")
    return url


def codex_workshop_overrides(mcp_config_json: str | None, *,
                             reading: bool = False) -> list[str]:
    """The `--config` overrides a codex `workshop leaf` launch adds on BOTH subcommands (issue
    #534 step 2 PR-2). A fresh list per call. Measured on codex-cli 0.164.0 against a loopback
    Responses-API stand-in and the real wrapper endpoint (unbilled;
    `tools/tests/test_bwrap_simulation.py` `CodexWorkshopLeafCliTests`, MX2):

    - `mcp_servers.wrapper.url="<url>"`: the wrapper's streamable-HTTP endpoint for this launch,
      the per-launch twin of claude's `--mcp-config` (`initialize`, `tools/list` and
      `tools/call` reach `tools/workshop_mcp.py`; no `features.*` flag is needed for it).
    - `mcp_servers.wrapper.default_tools_approval_mode="approve"`: an MCP tool call needs
      approval under every policy, and under `approval_policy="never"` nobody can give it, so
      without this every wrapper call fails with "MCP tool call requires approval, but approval
      policy is never" (measured; `"auto"` fails the same way). It is the twin of claude's
      `--allowedTools mcp__wrapper`.
    - `approval_policy="never"`: no command or patch waits for an approver (`codex exec`'s own
      default, pinned rather than inherited).
    - `features.apps=false`: the CLI otherwise starts its own `codex_apps` MCP client, which
      connects to the vendor's connector service with the operator's credential at every
      launch (measured: an `initialize` refused with HTTP 451 from outside the loopback) — an
      MCP server the launch did not name, as `--strict-mcp-config` keeps one out for claude.

    The tool surface the CLI then offers the model is its own (measured on 0.164.0 with a model
    the stand-in does not describe): a `functions` namespace — a JavaScript `exec` tool, `wait`
    and `request_user_input` — whose `exec` reaches nested tools (`exec_command`,
    `apply_patch`, `view_image`, … and, as "deferred" nested tools, the wrapper's
    `mcp__wrapper__<op>`), and a `collaboration` namespace of sub-agent tools (`spawn_agent`,
    `send_message`, `wait_agent`, …; `features.multi_agent=false` does not remove it). The
    launch prompt names the wrapper's tools by their name (`WORKSHOP_TOOL_SURFACE_TEXT`).

    `reading=True` (a judging substep, step 3) takes no `mcp_config_json` and keeps only the
    two overrides that do not name the wrapper. Measured on 0.164.0 (step 3 PR-J2,
    `test_bwrap_simulation.CodexReadingLeafCliTests`): the lineage offers no `mcp__` tool
    (MR5-a), a document a command writes at the tree root assembles (MR5-b), and `exec
    resume` continues the thread under the same overrides (MR5-d)."""
    if reading:
        if mcp_config_json is not None:
            raise ValueError("codex_workshop_overrides: a reading lineage is served no "
                             "endpoint")
        return ["--config", 'approval_policy="never"', "--config", "features.apps=false"]
    if mcp_config_json is None:
        raise ValueError("codex_workshop_overrides requires the wrapper's --mcp-config JSON")
    url = workshop_mcp_url(mcp_config_json)
    server = f"mcp_servers.{WORKSHOP_MCP_SERVER_NAME}"
    return ["--config", 'approval_policy="never"',
            "--config", "features.apps=false",
            "--config", f"{server}.url={json.dumps(url)}",
            "--config", f'{server}.default_tools_approval_mode="approve"']


# --------------------------------------------------------------------------------------
# Result-envelope parsing
# --------------------------------------------------------------------------------------

class ResultEnvelope(NamedTuple):
    """The parsed `claude -p --output-format json` result envelope.

    `parsed` is True only when stdout was a JSON object. Every payload field is the raw
    value via `.get(key, _MISSING)`, so an absent key is `_MISSING` and a present `null`
    is `None` — the two are never conflated. `parse_error` carries a short reason when the
    envelope itself could not be read (empty stdout, non-JSON, non-object); it is None on a
    well-formed envelope regardless of what the model answered inside `result`.
    """
    parsed: bool
    result: Any
    is_error: Any
    model: Any
    usage: Any
    session_id: Any
    raw: Any
    parse_error: "str | None"


# The `modelUsage` row keys that identify the PRIMARY model, against the top-level `usage`
# block's own names. The envelope reports `usage` for one model and `modelUsage` for every
# model the leaf ran, so the row that equals `usage` names the one that answered.
_PRIMARY_MODEL_MATCH: tuple[tuple[str, str], ...] = (
    ("inputTokens", "input_tokens"),
    ("outputTokens", "output_tokens"),
    ("cacheReadInputTokens", "cache_read_input_tokens"),
    ("cacheCreationInputTokens", "cache_creation_input_tokens"),
)


def _is_count(value: Any) -> bool:
    """A token count: an `int` that is not a `bool` (`True` is an `int` in Python)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _resolve_model(envelope: dict) -> Any:
    """The resolved model string from an envelope, or `_MISSING`.

    Tolerant of envelope-format drift (the CLI's JSON shape is not a pinned contract), in
    three steps:

    1. a top-level `model` string wins;
    2. a single-key `modelUsage` map names the one model the leaf ran under;
    3. a multi-key `modelUsage` — the ordinary case, because a leaf routinely runs a helper
       model alongside the one that answers — is resolved by finding the row whose token
       counts equal the top-level `usage` block, which reports the PRIMARY model alone.

    Step 3 is not a heuristic reaching for a value: across the 136 result envelopes recorded
    in this repository none carries a top-level `model` and 78 carry two `modelUsage` rows,
    and in every one of those 78 exactly one row matches. Without it those leaves record no
    exact model at all and fall back to the launch request's unpinned alias. A row that
    matches ambiguously (or not at all) still returns `_MISSING` rather than guessing — the
    value is recorded for provenance, never gates a decision, so a missing model is a
    provenance gap, not a fail-open."""
    model = envelope.get("model", _MISSING)
    if isinstance(model, str) and model.strip():
        return model
    model_usage = envelope.get("modelUsage")
    if not isinstance(model_usage, dict) or not model_usage:
        return _MISSING
    if len(model_usage) == 1:
        (name,) = model_usage.keys()
        if isinstance(name, str) and name.strip():
            return name
        return _MISSING
    usage = envelope.get("usage")
    if not isinstance(usage, dict):
        return _MISSING

    def _matches(row: Any) -> bool:
        if not isinstance(row, dict):
            return False
        compared = 0
        for reported, canonical in _PRIMARY_MODEL_MATCH:
            counted = usage.get(canonical)
            if not _is_count(counted):
                continue
            # `_is_count` on BOTH sides: `True == 1` in Python, so a row reporting a flag
            # where a count belongs would otherwise match a row that reported 1.
            if not _is_count(row.get(reported)) or row.get(reported) != counted:
                return False
            compared += 1
        # At least one real count has to have agreed. Without this an EMPTY row matches an
        # empty `usage` on `None == None` four times over and is named the primary model.
        return compared > 0

    matches = [name for name, row in model_usage.items()
               if isinstance(name, str) and name.strip() and _matches(row)]
    return matches[0] if len(matches) == 1 else _MISSING


def parse_result_envelope(stdout: Any) -> ResultEnvelope:
    """Parse a leaf's captured stdout as a `--output-format json` result envelope.

    Never raises: leaf stdout is untrusted, and a crash in the transport is a worse failure
    than a structured `parsed=False` the caller can route. Empty / whitespace stdout, a
    non-JSON body, and a JSON scalar/array (not an object) each yield `parsed=False` with a
    reason; the caller treats all three as "no bundle to read" and repairs or fails closed.
    """
    if not isinstance(stdout, str) or not stdout.strip():
        return ResultEnvelope(False, _MISSING, _MISSING, _MISSING, _MISSING, _MISSING,
                              None, "empty stdout")
    try:
        envelope = json.loads(stdout)
    except (ValueError, RecursionError) as exc:
        # `json.JSONDecodeError` is a `ValueError`; deeply nested hostile stdout raises
        # `RecursionError` (a `RuntimeError`, not a `ValueError`). Both are caught so the
        # transport keeps its "never raises" contract — a crash here would fail the substep
        # hard instead of routing to bounded repair.
        return ResultEnvelope(False, _MISSING, _MISSING, _MISSING, _MISSING, _MISSING,
                              None, f"stdout is not valid JSON: {exc}")
    if not isinstance(envelope, dict):
        return ResultEnvelope(False, _MISSING, _MISSING, _MISSING, _MISSING, _MISSING,
                              envelope, "result envelope is not a JSON object")
    return ResultEnvelope(
        parsed=True,
        result=envelope.get("result", _MISSING),
        is_error=envelope.get("is_error", _MISSING),
        model=_resolve_model(envelope),
        usage=envelope.get("usage", _MISSING),
        session_id=envelope.get("session_id", _MISSING),
        raw=envelope,
        parse_error=None,
    )


# --------------------------------------------------------------------------------------
# Single-document extraction + truncation classification
# --------------------------------------------------------------------------------------

def _object_from_unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """`json.loads` `object_pairs_hook` that REJECTS a duplicate key instead of silently
    keeping the last value.

    The model document is the trust boundary, and a stock parse of
    `{"verification_status": "fail", …, "verification_status": "pass"}` collapses to a
    passing dict — a duplicate key could suppress a failure the validators would otherwise
    catch. Raising here turns an ambiguous document into a parse failure, routing it to
    bounded repair rather than letting the last value win. Checked at every nesting level
    (the hook fires per object)."""
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"duplicate JSON object key {key!r}")
        obj[key] = value
    return obj


def _reject_non_finite_constant(name: str) -> float:
    """`json.loads` `parse_constant` hook: reject the non-standard bare constants `NaN`,
    `Infinity`, `-Infinity` that Python's parser accepts by default but strict JSON forbids.

    A non-finite value would survive `validate_bundle` (a lowering-plan section like
    `precision` is an intentionally open object) and then serialize to non-standard JSON, so
    it is refused at the parse boundary and routed to bounded repair instead."""
    raise ValueError(f"non-finite JSON constant {name!r} is not allowed")


def _finite_float(token: str) -> float:
    """`json.loads` `parse_float` hook: reject a float TOKEN that overflows to a non-finite
    value (`1e999` → `inf`), which `parse_constant` does not see (it is a number, not a bare
    `Infinity`). Finite floats pass through unchanged."""
    value = float(token)
    if not math.isfinite(value):
        raise ValueError(f"non-finite JSON number {token!r} is not allowed")
    return value


def _loads_strict(text: str) -> Any:
    """`json.loads` hardened for the model-document trust boundary: duplicate object keys
    rejected (`_object_from_unique_pairs`) and non-finite values rejected (bare
    `NaN`/`Infinity` via `parse_constant`, overflow floats via `parse_float`)."""
    return json.loads(
        text,
        object_pairs_hook=_object_from_unique_pairs,
        parse_constant=_reject_non_finite_constant,
        parse_float=_finite_float,
    )


def _fenced_json_payload(text: str) -> tuple["str | None", "str | None"]:
    """Isolate a single fenced JSON block from `text`.

    Returns `(payload, None)` when the reply is exactly one fenced block spanning the whole
    reply (only whitespace outside), `(None, None)` when there is no fence (parse the whole
    text), or `(None, category)` when the fencing is a failure: a fence opened but never closed
    is a cut-off answer (`pure_response_truncated`); content before the opening fence, content
    after the closing fence, or an empty block is ambiguous (`pure_response_unparseable`).

    The outer fence is parsed STRUCTURALLY — anchored on the leading fence line and the FINAL
    ``` — never by counting every ``` in the text. A JSON string value may legitimately contain
    a ``` sequence (a CodegenBundle source file, a verdict message), and a marker count would
    misread such a valid single document as truncated/unparseable. Because the payload runs to
    the LAST ```, an in-string ``` stays inside the payload and parses as string data.

    The whole-reply span (nothing outside the fence) closes a multi-document trust hole: this
    path is reached only when the WHOLE text failed to parse (parse-first ran first), so a bare
    document followed by a fenced one lands here, and returning just the fenced payload would
    silently drop the bare document — object, array, OR scalar like `"fail"` — so a bare `fail`
    followed by a fenced `pass` must not read as pass. Rejecting any content outside the fence
    covers every competing document AND stray prose without a fragile "is this prose a document"
    heuristic; the system prompt tells the model to emit no surrounding prose, so a preamble
    reply is false-rejected into a bounded repair — the safe direction for a trust boundary.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        # A fence anywhere but the start means content precedes it (a competing document or
        # prose); no fence at all means "parse the whole text" (the caller then classifies).
        return (None, RESPONSE_UNPARSEABLE) if "```" in stripped else (None, None)
    newline = stripped.find("\n")
    if newline == -1:
        # "```json" with no newline: opened, no content, never closed.
        return None, RESPONSE_TRUNCATED
    if "```" in stripped[3:newline]:
        # A one-line "```json ... ```" opening is malformed.
        return None, RESPONSE_UNPARSEABLE
    rest = stripped[newline + 1:]
    if "```" not in rest:
        return None, RESPONSE_TRUNCATED          # opened, never closed
    if not rest.endswith("```"):
        return None, RESPONSE_UNPARSEABLE         # closed, then trailing content
    payload = rest[:-3]
    if not payload.strip():
        return None, RESPONSE_UNPARSEABLE
    return payload, None


def _classify_json_failure(payload: str) -> str:
    """Classify a `json.loads` failure on `payload` as truncated vs unparseable.

    Truncation (the model hit the output-token ceiling) shows as an unbalanced container —
    more `{`/`[` opened than closed — or a decode error at the very end of the input. Either
    signal returns `pure_response_truncated`; anything else is `pure_response_unparseable`.
    The two categories both route to a bounded repair turn, so a borderline misclassification
    only mislabels the operator's diagnosis excerpt, never changes the control flow. Brace
    counting is over the raw text (a string literal could hold an unbalanced brace), which is
    why the tie is broken toward the safe, bounded repair rather than a hard reject.
    """
    if payload.count("{") > payload.count("}") or payload.count("[") > payload.count("]"):
        return RESPONSE_TRUNCATED
    try:
        json.loads(payload)
    except RecursionError:
        # Deeply nested balanced input the brace count did not catch: over-limit output.
        return RESPONSE_TRUNCATED
    except json.JSONDecodeError as exc:
        if exc.pos >= len(payload.rstrip()):
            return RESPONSE_TRUNCATED
    return RESPONSE_UNPARSEABLE


def extract_json_document(result_text: Any) -> tuple[Any, "str | None"]:
    """Extract the single JSON document a pure leaf returns in its `result` text.

    Returns `(doc, None)` on success (doc is whatever `json.loads` produced — the caller's
    schema validator checks its type), or `(None, category)` where category is one of
    `RESPONSE_CATEGORIES`. A `_MISSING` result (absent key), a JSON `null` result, a
    non-string result, and an empty string are all `pure_response_unparseable` — there is no
    document to read.

    PARSE-FIRST: the whole (stripped) text is tried as JSON before any fence handling, so a
    clean document that merely mentions a ```` ``` ```` fence inside a STRING value — a
    verdict `last_fail_reason`, a Fortran comment in a bundle file — is never rejected by
    fence bookkeeping. Fence extraction is the FALLBACK for a markdown-wrapped answer,
    reached only when the whole text is not itself valid JSON; exactly one ```json fence is
    tolerated there.
    """
    if result_text is _MISSING or result_text is None or not isinstance(result_text, str):
        return None, RESPONSE_UNPARSEABLE
    text = result_text.strip()
    if not text:
        return None, RESPONSE_UNPARSEABLE
    try:
        return _loads_strict(text), None
    except (ValueError, RecursionError):
        pass
    payload, fence_category = _fenced_json_payload(text)
    if fence_category is not None:
        return None, fence_category
    if payload is None:
        # No fence, and the whole-text parse above already failed.
        return None, _classify_json_failure(text)
    try:
        return _loads_strict(payload), None
    except (ValueError, RecursionError):
        return None, _classify_json_failure(payload)


# --------------------------------------------------------------------------------------
# Verify verdict contract
# --------------------------------------------------------------------------------------

# The verify persona returns a verdict JSON; the host projects `verification_status` and
# `issue_severity` onto `source_meta.json` / `ir_meta.json` (value-type contract:
# `tools/meta_contracts.py`) verbatim. The verdict is a distinct, richer intermediate: it adds
# a structured `findings[]` the meta has no key for, so its shape is pinned here, not in the
# meta contract. `last_fail_reason` and `findings[]` are both model-authored, and the host folds
# the two into the meta's one `last_fail_reason` string with `verify_repair_text` — the text the
# producer's repair reads (via `last_fail_reason`, on the warm, cold and `--resume` paths alike),
# so a finding the reviewer left out of its one-line reason still reaches the repair (issue
# #347). The verdict document itself is kept only in the reviewer's own transport record (its
# dialog logs, and for an HTTP provider the saved response under `launches/`).
# Vocabularies match the conductor's routing (`classify_verify_severity`): status is
# `pass`/`fail`, severity is `none`/`minor`/`major`/`critical`.
VERDICT_STATUSES: tuple[str, ...] = ("pass", "fail")
VERDICT_SEVERITIES: tuple[str, ...] = ("none", "minor", "major", "critical")
VERDICT_REQUIRED_KEYS: tuple[str, ...] = (
    "verification_status", "issue_severity", "last_fail_reason", "findings")


def verify_verdict_violations(doc: Any) -> list[str]:
    """Validate a verify verdict document; an empty list is a valid verdict.

    Schema layer first (presence, enum, type, closed keys), then the joint invariants only
    on a schema-sound document — so one defect is never reported twice and an invariant never
    defends against a mistyped field. Enum checks are EXACT: `verification_status` is the
    literal `"pass"`, so `"PASS"` is rejected rather than case-folded into a pass (a folded
    accept would let a producer's casing choice silence a fail).

    Joint invariants tie the four fields into a single coherent verdict:
    - `pass`  <=> issue_severity `none` <=> findings empty <=> last_fail_reason null;
    - `fail`   => a non-`none` severity, at least one finding, and a non-empty last_fail_reason.
    """
    if not isinstance(doc, dict):
        return ["verdict must be a JSON object"]

    violations: list[str] = []
    for key in VERDICT_REQUIRED_KEYS:
        if key not in doc:
            violations.append(f"{key} is required")
    for key in sorted((k for k in doc if k not in VERDICT_REQUIRED_KEYS), key=repr):
        violations.append(f"unknown key {key!r} (the verdict object is closed)")

    status = doc.get("verification_status", _MISSING)
    if status is not _MISSING and status not in VERDICT_STATUSES:
        violations.append(f"verification_status must be one of {', '.join(VERDICT_STATUSES)}")
    severity = doc.get("issue_severity", _MISSING)
    if severity is not _MISSING and severity not in VERDICT_SEVERITIES:
        violations.append(f"issue_severity must be one of {', '.join(VERDICT_SEVERITIES)}")
    reason = doc.get("last_fail_reason", _MISSING)
    if reason is not _MISSING and reason is not None and not isinstance(reason, str):
        violations.append("last_fail_reason must be a string or null")
    findings = doc.get("findings", _MISSING)
    if findings is not _MISSING:
        if not isinstance(findings, list):
            violations.append("findings must be an array")
        else:
            for index, finding in enumerate(findings):
                if not isinstance(finding, dict):
                    violations.append(f"findings[{index}] must be an object")
                    continue
                summary = finding.get("summary")
                if not isinstance(summary, str) or not summary.strip():
                    violations.append(f"findings[{index}].summary must be a non-empty string")

    # Joint invariants run only on a schema-sound document, so a type error below cannot
    # cascade into a spurious invariant clause.
    if violations:
        return violations

    status = doc["verification_status"]
    severity = doc["issue_severity"]
    reason = doc["last_fail_reason"]
    findings = doc["findings"]
    if status == "pass":
        if severity != "none":
            violations.append("verification_status 'pass' requires issue_severity 'none'")
        if findings:
            violations.append("verification_status 'pass' requires an empty findings array")
        if reason is not None:
            violations.append("verification_status 'pass' requires last_fail_reason null")
    else:  # fail
        if severity == "none":
            violations.append(
                "verification_status 'fail' requires a non-'none' issue_severity")
        if not findings:
            violations.append("verification_status 'fail' requires at least one finding")
        if not (isinstance(reason, str) and reason.strip()):
            violations.append(
                "verification_status 'fail' requires a non-empty last_fail_reason")
    return violations


def verify_repair_text(verdict: dict[str, Any]) -> str | None:
    """The `last_fail_reason` the host projects from a schema-valid verify verdict.

    The ONE place the verdict's reason and its `findings[]` are folded into the meta's single
    repair string. The repair readers of `last_fail_reason` (the same-phase repair, the revoke
    that writes it back, the `--resume` seed) take it unchanged; the escalate diagnostician
    reads the whole meta through its per-artifact context budget, which clips like any other
    inlined meta.
    `pass` gives `None`. `fail` gives the stripped reason, then one line per finding in the
    verdict's order, `1. <summary>`, `2. <summary>`, … with each summary stripped. Nothing is
    deduplicated (a summary that repeats the reason is kept) and nothing is capped: a clipped
    finding is the defect this function exists to remove."""
    if verdict["verification_status"] == "pass":
        return None
    lines = [verdict["last_fail_reason"].strip()]
    lines.extend(f"{index}. {finding['summary'].strip()}"
                 for index, finding in enumerate(verdict["findings"], start=1))
    return "\n".join(lines)


def judge_repair_text(review: Any) -> str | None:
    """The repair findings the host folds from a `semantic_review.json` whose decision is `fail`.

    `verify_repair_text`'s twin for the judge (issue #455): `_read_repair_findings` reads it at
    the conduct reopen point, so a revocation the diagnostician's directive causes carries the
    judge's findings as `last_fail_reason` and the re-derived producer gets them as
    `repair_findings`. One entry per finding in the review's order, each on a new line,
    `[<attribution>/<confidence>] <description> (evidence: <ref>, <ref>)`, the description
    stripped (a newline inside it is kept) and nothing capped or deduplicated. The input is a
    file read back from disk, so anything that is not a `fail` review with at least one object
    finding gives `None` (the repair then falls back to the full prompt) rather than raising."""
    if not isinstance(review, dict) or review.get("decision") != "fail":
        return None
    findings = review.get("findings")
    if not isinstance(findings, list):
        return None
    lines = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        refs = finding.get("evidence_refs")
        refs_text = ", ".join(str(ref) for ref in refs) if isinstance(refs, list) else ""
        lines.append(f"[{finding.get('attribution')}/{finding.get('confidence')}] "
                     f"{str(finding.get('description') or '').strip()} (evidence: {refs_text})")
    return "\n".join(lines) or None


# --------------------------------------------------------------------------------------
# The `validate.judge` semantic-review document (issue #169, Z3)
# --------------------------------------------------------------------------------------
#
# The judge leaf returns its JUDGEMENT and nothing else. `semantic_review.json` itself is
# host-authored: everything the `--stage pre_judge` gate reads besides the decision — the
# `review_method` literal, `scope.model_ref` / `scope.runner_ref` / `scope.raw_refs` — are
# facts about the workspace that a closed-context leaf cannot know and must not guess.
#
# The shape below is `docs/workflow/phases/phase_04_validate.md`'s "Classification fields the
# `judge` records as required" table, which was the only definition of it and which no
# validator read. MEASURED 2026-09-08 over every `semantic_review.json` in this machine's
# untracked `workspace/` (so the figures are of the recorded runs, not of a commit):
# 29 reviews, **0 findings in any of them**, so the finding shape has never actually
# been produced — and 34 distinct top-level keys across those 29 files, of which only
# `review_method`, `decision`, `scope` and `findings` appear in all. Nineteen of the 34 appear
# once or twice (`tests_md_intent_check`, `observations_not_findings`, `perf_review`, …): the
# leaf improvises a structure per run and nothing reads it. Closing the object is what stops
# that, and this is the first machine check the finding table has ever had.
#
# `finding_id` is deliberately NOT a key. The plan named one; the phase document's table does
# not, and no reader of `semantic_review.json#findings[*]` resolves one.
# Adding a field nothing reads is the surface this migration exists to remove.
SEMANTIC_REVIEW_DECISIONS: tuple[str, ...] = ("pass", "fail")
SEMANTIC_REVIEW_ATTRIBUTIONS: tuple[str, ...] = ("code", "ir", "spec", "evidence")
SEMANTIC_REVIEW_CONFIDENCES: tuple[str, ...] = ("high", "medium", "low")
SEMANTIC_REVIEW_REQUIRED_KEYS: tuple[str, ...] = ("decision", "findings")
SEMANTIC_REVIEW_OPTIONAL_KEYS: tuple[str, ...] = ("notes",)
SEMANTIC_REVIEW_FINDING_KEYS: tuple[str, ...] = (
    "attribution", "evidence_refs", "confidence", "description")
#: `notes` is the one free-text field, and it is the whole budget for everything the leaf used
#: to spread across those 34 improvised keys — so it is bounded rather than open. The bound is
#: MEASURED over the population it has to serve, not chosen: across the 29 recorded
#: `semantic_review.json` files, the JSON of everything outside the four core keys runs 1,518 to
#: 9,773 characters (median 6,010), and one recorded `notes` field is already 4,272. The first
#: value here was 4,000, which a review round showed would have REFUSED that real review and
#: spent a repair turn on a judge that had done its job — this repository's recorded default
#: error direction. 12,000 clears the observed maximum with headroom while keeping the field
#: bounded; the template states the limit to the leaf, so a compliant one compresses rather
#: than being refused.
SEMANTIC_REVIEW_NOTES_MAX_CHARS = 12000


def semantic_review_document_violations(
    doc: Any, *, document_keys: Sequence[str] = ()
) -> list[str]:
    """Validate a `validate.judge` semantic-review document; an empty list is a valid one.

    Same two-layer shape as `verify_verdict_violations`: schema first (presence, enum, type,
    closed keys), then the joint invariant only on a schema-sound document, so one defect is
    never reported twice and the invariant never defends against a mistyped field. Enum checks
    are EXACT, for that function's reason — a case-folded accept would let the leaf's casing
    choice decide a verdict.

    `document_keys` is the set of `pure_context` keys the host inlined for this launch. Every
    `evidence_refs[]` entry must be `<document_key>[#<fragment>]` naming one of them: the leaf
    cites what it was GIVEN, and the host resolves each key to the workspace path it inlined
    from when it writes `semantic_review.json`, which is what keeps that file's "path list" a
    path list. An empty `document_keys` skips only the key check (the shape is still checked),
    so a caller that does not know them cannot be silently given a weaker validation.
    """
    if not isinstance(doc, dict):
        return ["the semantic review must be a JSON object"]

    violations: list[str] = []
    known = (*SEMANTIC_REVIEW_REQUIRED_KEYS, *SEMANTIC_REVIEW_OPTIONAL_KEYS)
    for key in SEMANTIC_REVIEW_REQUIRED_KEYS:
        if key not in doc:
            violations.append(f"{key} is required")
    for key in sorted((k for k in doc if k not in known), key=repr):
        violations.append(
            f"unknown key {key!r} (the semantic review object is closed; put anything else "
            f"in `notes`)")

    decision = doc.get("decision", _MISSING)
    if decision is not _MISSING and decision not in SEMANTIC_REVIEW_DECISIONS:
        violations.append(
            f"decision must be one of {', '.join(SEMANTIC_REVIEW_DECISIONS)}")
    notes = doc.get("notes", _MISSING)
    if notes is not _MISSING:
        if not isinstance(notes, str):
            violations.append("notes must be a string")
        elif len(notes) > SEMANTIC_REVIEW_NOTES_MAX_CHARS:
            violations.append(
                f"notes must be at most {SEMANTIC_REVIEW_NOTES_MAX_CHARS} characters "
                f"(got {len(notes)})")

    findings = doc.get("findings", _MISSING)
    if findings is not _MISSING:
        if not isinstance(findings, list):
            violations.append("findings must be an array")
        else:
            for index, finding in enumerate(findings):
                violations.extend(
                    _semantic_review_finding_violations(finding, index, document_keys))

    if violations:
        return violations

    # The one joint invariant: the decision and the findings must agree. A `fail` with no
    # finding gives the operator and the diagnostician nothing to act on and the repair no
    # findings to carry (`judge_repair_text`); a `pass` with findings is a review that reports
    # a defect and waves it through, which is the shape this whole substep exists to refuse.
    if doc["decision"] == "pass":
        if doc["findings"]:
            violations.append("decision 'pass' requires an empty findings array")
    elif not doc["findings"]:
        violations.append("decision 'fail' requires at least one finding")
    return violations


def _semantic_review_finding_violations(
    finding: Any, index: int, document_keys: Sequence[str]
) -> list[str]:
    """The schema of one `findings[]` entry. Split out so the loop above stays one screen."""
    if not isinstance(finding, dict):
        return [f"findings[{index}] must be an object"]

    violations: list[str] = []
    for key in SEMANTIC_REVIEW_FINDING_KEYS:
        if key not in finding:
            violations.append(f"findings[{index}].{key} is required")
    for key in sorted((k for k in finding if k not in SEMANTIC_REVIEW_FINDING_KEYS), key=repr):
        violations.append(
            f"findings[{index}] has unknown key {key!r} (a finding object is closed)")

    attribution = finding.get("attribution", _MISSING)
    if attribution is not _MISSING and attribution not in SEMANTIC_REVIEW_ATTRIBUTIONS:
        violations.append(
            f"findings[{index}].attribution must be one of "
            f"{', '.join(SEMANTIC_REVIEW_ATTRIBUTIONS)}")
    confidence = finding.get("confidence", _MISSING)
    if confidence is not _MISSING and confidence not in SEMANTIC_REVIEW_CONFIDENCES:
        violations.append(
            f"findings[{index}].confidence must be one of "
            f"{', '.join(SEMANTIC_REVIEW_CONFIDENCES)}")
    description = finding.get("description", _MISSING)
    if description is not _MISSING and not (
            isinstance(description, str) and description.strip()):
        violations.append(f"findings[{index}].description must be a non-empty string")

    refs = finding.get("evidence_refs", _MISSING)
    if refs is _MISSING:
        return violations
    if not isinstance(refs, list) or not refs:
        violations.append(f"findings[{index}].evidence_refs must be a non-empty array")
        return violations
    for ref_index, ref in enumerate(refs):
        label = f"findings[{index}].evidence_refs[{ref_index}]"
        if not isinstance(ref, str) or not ref.strip():
            violations.append(f"{label} must be a non-empty string")
            continue
        key = ref.split("#", 1)[0].strip()
        if not key:
            violations.append(
                f"{label} must name an inlined document before its '#' fragment")
        elif document_keys and key not in document_keys:
            violations.append(
                f"{label} names {key!r}, which is not one of the documents you were given "
                f"({', '.join(sorted(document_keys))})")
    return violations
