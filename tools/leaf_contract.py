#!/usr/bin/env python3
"""Leaf-contract helpers shared by every LLM leaf — since issue #534 PR-J4-b, every one a
`workshop leaf`.

The module began as the PURE-FUNCTION leaf's transport (Z2; `docs/design/
zero_base_architecture.md` A2): a host-assembled closed context, one typed JSON document
returned, no tools. That leaf model, its launch flag set (`pure_leaf_flags`), its fixed system
prompt and its reply-text extractor went in PR-J4-b, and PR-J4-d renamed the module from
`tools/pure_leaf.py`. What it holds now is STAGE-AGNOSTIC: the leaf contract version
(`LEAF_PROMPT_CONTRACT_VERSION`, a derivation-key input), the workshop leaf's launch flags and
tool-surface tables, the result-envelope parser, the verify verdict and semantic-review
contracts, and the record predicates (`is_pure_request` and the pure sentinel stay so a
record written by the pure leaf is still read). It is deliberately free of any
`generate`-specific knowledge (the `CodegenBundle` producer lives in
`tools/workflow_conductor.py`; the bundle contract in `tools/codegen_bundle.py`).

Idiom (`tools/codegen_bundle.py`, `tools/meta_contracts.py`): stdlib only, pure functions,
an explicit `_MISSING` sentinel so a present JSON `null` is never confused with an absent
key, and prefix-free violation clauses the caller frames in its own reporting idiom.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, NamedTuple

# The leaf prompt-contract version stamped into every launch request, `bundle_meta.json` and
# its twins, and hashed into the compile / generate / validate derivation keys
# (`tools/derivation.py`), so a contract change is an observable event (A7). Bumped when any
# text a leaf reads changes — a workshop template, a fragment, a document slice, a tool-surface
# row, a system prompt — in a way that affects leaf behaviour; the drift pin
# (`tools/tests/test_leaf_prompt_contract_drift.py`) names what it hashes. ONE counter since
# issue #534 PR-J4-b: the workshop leaf's own (`WORKSHOP_PROMPT_CONTRACT_VERSION`, `workshop-1`
# through `workshop-9`, which entered no key) is retired, and the VALUE stays `pure-84` (no key
# moves); the next bump writes `leaf-85`.
LEAF_PROMPT_CONTRACT_VERSION = "pure-84"

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
# `LEAF_PROMPT_CONTRACT_VERSION`).
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

#: The phases whose failure the `escalate` diagnostician may be launched for — the ones that
#: launch a child agent (`orchestration_runtime.STEP_REQUIRED_CHILD_AGENT`, asserted equal by
#: `test_workshop_launch`); spelled here because this module must not import the runtime.
DIAGNOSE_STEPS: frozenset[str] = frozenset({"compile", "generate", "build", "validate"})
#: The diagnostician's substep token (`orchestration_runtime.DIAGNOSE_SUBSTEP`).
DIAGNOSE_SUBSTEP = "diagnose"
#: The diagnostician's pairs (`orchestration_runtime.DIAGNOSE_LAUNCH_PAIRS`): the READING kind
#: by construction since issue #534 PR-J4-a — a seeded tree holding `diagnosis.json`, file
#: tools, no operation, one output document (`directive.json`). Not a substep an operator
#: assigns (`tools/llm_config.py` serves it from `defaults`), so the config's
#: `WORKSHOP_READING_SUBSTEPS` does not mirror it.
WORKSHOP_DIAGNOSE_PAIRS: frozenset[tuple[str, str]] = frozenset(
    (step, DIAGNOSE_SUBSTEP) for step in DIAGNOSE_STEPS)

#: Every READING pair a workshop launch may serve: the judges and the diagnostician.
WORKSHOP_READING_LAUNCH_PAIRS: frozenset[tuple[str, str]] = (
    WORKSHOP_READING_PAIRS | WORKSHOP_DIAGNOSE_PAIRS)

#: Every `(step, substep)` pair a workshop leaf may serve (`tools/llm_config.py` refuses any
#: other entry, and mirrors this table minus `WORKSHOP_DIAGNOSE_PAIRS` as `WORKSHOP_LEAF_SUBSTEPS`).
WORKSHOP_LAUNCH_PAIRS: frozenset[tuple[str, str]] = (
    WORKSHOP_PRODUCING_PAIRS | WORKSHOP_READING_LAUNCH_PAIRS)


def is_workshop_reading_pair(step: Any, substep: Any) -> bool:
    """True when `(step, substep)` is one of `WORKSHOP_READING_LAUNCH_PAIRS` (a judge or the
    diagnostician), case/space-insensitively: the one predicate that says which KIND a
    workshop launch of that pair is."""
    return (str(step or "").strip().lower(),
            str(substep or "").strip().lower()) in WORKSHOP_READING_LAUNCH_PAIRS


#: The first line of every host-rendered workshop launch prompt, anchored as the pure sentinel
#: is (`startswith`). A distinct literal, so a record's prompt says which of the two leaf
#: models it was written for and the validators compare that against the request.
WORKSHOP_PROMPT_SENTINEL = "Workshop leaf turn (host-seeded work tree)"

#: The system prompt a workshop leaf runs under (`--system-prompt`), which replaces the CLI's
#: default because the default carries per-machine DYNAMIC sections (cwd, environment, memory
#: paths, git status). It takes them out of the system prompt but not out of the request: the
#: CLI still adds text of its own (`docs/ORCHESTRATION.md`, issue #454). A change here is a
#: prompt-contract change (`LEAF_PROMPT_CONTRACT_VERSION`).
WORKSHOP_SYSTEM_PROMPT = (
    "You are the producer of a host-mediated workflow, working in a host-seeded work tree. "
    "Your file tools reach that tree and nothing else; you hold no shell. The host runs the "
    "operations you ask for through the host's operation tools and judges your tree after you "
    "finish. The user message is your complete task."
)

#: The system prompt a READING workshop leaf (a judging substep, step 3) runs under. Its own
#: text because the producer's names the host's operation tools, which a reading lineage is not
#: served.
WORKSHOP_READER_SYSTEM_PROMPT = (
    "You are a reviewer of a host-mediated workflow, working in a host-seeded work tree. Your "
    "file tools reach that tree and nothing else; you hold no shell and run no operation. The "
    "host reads the one document you write and judges nothing else. The user message is your "
    "complete task."
)

#: The page of an HTTP workshop leaf's `read_file` (issue #534 PR-J3-b, operator decision O2):
#: at most this many lines per call — the default `max_lines` and its ceiling — and at most this
#: many UTF-8 bytes of text, a page stopping at the last WHOLE line under the cap. Host
#: constants, not entry fields: 2,000 lines is the claude `Read` default under which the reading
#: judges were adopted, and 100,000 bytes (about 25,000 tokens) bounds a page of long JSON lines.
HTTP_READ_PAGE_LINES = 2000
HTTP_READ_PAGE_BYTES = 100_000

#: The file tools an HTTP workshop leaf is declared, in the order they are declared: the twins
#: of claude's `Read / Write / Edit / Glob / Grep` (`WORKSHOP_FILE_TOOLS`), which the host loop
#: (`tools/llm_workshop_loop.py`) runs on the lineage's tree.
HTTP_FILE_TOOLS: tuple[str, ...] = (
    "read_file", "write_file", "edit_file", "list_files", "grep_file")

_PATH_PROPERTY = {"type": "string",
                  "description": "a path relative to your work tree's root, with `/` separators"}

#: What the model reads of the five file tools: name, description and JSON schema, in one table
#: both dialects serialize (`llm_workshop_loop`). Leaf-read contract text: a change here is a
#: prompt-contract change (`LEAF_PROMPT_CONTRACT_VERSION`).
HTTP_FILE_TOOL_DECLARATIONS: tuple[dict[str, Any], ...] = (
    {"name": "read_file",
     "description": (
         f"Read lines of one file of your work tree. One call returns at most "
         f"{HTTP_READ_PAGE_LINES:,} lines and at most {HTTP_READ_PAGE_BYTES:,} bytes of text, "
         "stopping at the last whole line under both, and says `total_lines` and "
         "`next_start_line`; call again from `next_start_line` until it is null to read the "
         "rest. A single line longer than the byte page is not returned: the result names it "
         "in `skipped_line`, and `grep_file` shows its start."),
     "input_schema": {
         "type": "object",
         "properties": {
             "path": _PATH_PROPERTY,
             "start_line": {"type": "integer", "description": "first line to return (from 1)"},
             "max_lines": {"type": "integer",
                           "description": f"at most this many lines (at most "
                                          f"{HTTP_READ_PAGE_LINES:,})"},
         },
         "required": ["path"],
         "additionalProperties": False,
     }},
    {"name": "write_file",
     "description": ("Write a whole file of your work tree, creating it and its directories "
                     "or replacing it. The seeds and CONTRACT.md are read-only."),
     "input_schema": {
         "type": "object",
         "properties": {"path": _PATH_PROPERTY,
                        "content": {"type": "string", "description": "the file's whole text"}},
         "required": ["path", "content"],
         "additionalProperties": False,
     }},
    {"name": "edit_file",
     "description": ("Replace one exact occurrence of `old_text` in a file of your work tree "
                     "with `new_text`. Refused when `old_text` occurs zero times or more than "
                     "once. The seeds and CONTRACT.md are read-only."),
     "input_schema": {
         "type": "object",
         "properties": {"path": _PATH_PROPERTY,
                        "old_text": {"type": "string"},
                        "new_text": {"type": "string"}},
         "required": ["path", "old_text", "new_text"],
         "additionalProperties": False,
     }},
    {"name": "list_files",
     "description": ("List every file of your work tree with its size in bytes, and any "
                     "symbolic link (not followed, not readable)."),
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "grep_file",
     "description": (
         "Find the lines of ONE file of your work tree that match a Python regular expression. "
         "Returns each matching line with its line number, at most `max_matches` of them "
         f"(default and ceiling 200) and at most {HTTP_READ_PAGE_BYTES:,} bytes of text, a "
         "long line cut to fit, and `total_matches`."),
     "input_schema": {
         "type": "object",
         "properties": {
             "path": _PATH_PROPERTY,
             "pattern": {"type": "string", "description": "a Python `re` pattern"},
             "max_matches": {"type": "integer",
                             "description": "at most this many matches (at most 200)"},
         },
         "required": ["path", "pattern"],
         "additionalProperties": False,
     }},
)

#: The default and ceiling of `grep_file`'s `max_matches`.
HTTP_GREP_MAX_MATCHES = 200

if tuple(d["name"] for d in HTTP_FILE_TOOL_DECLARATIONS) != HTTP_FILE_TOOLS:  # pragma: no cover
    raise RuntimeError("HTTP_FILE_TOOL_DECLARATIONS must declare HTTP_FILE_TOOLS in order")

#: The `<tool_surface>` sentence of a PRODUCING workshop launch on either HTTP provider: one
#: text, because the host loop is one transport whichever dialect it speaks.
HTTP_PRODUCER_TOOL_SURFACE_TEXT = (
    "You have no shell. Your tools are `read_file`, `write_file`, `edit_file`, `list_files` "
    "and `grep_file`, which reach your work tree and nothing else, and the host's operations, "
    "declared to you as tools under their own names, which run on your tree when you call "
    f"them. `read_file` returns at most {HTTP_READ_PAGE_LINES:,} lines and "
    f"{HTTP_READ_PAGE_BYTES:,} bytes per call and says `next_start_line`; `grep_file` finds "
    "lines in one file; `list_files` says each file's size. The seeds and `CONTRACT.md` are "
    "read-only: a write to one is refused. A file reaches your tree only through `write_file` "
    "or `edit_file`, never through the text of your reply. A reply longer than your output "
    "limit is cut and none of its tool calls run: write one file per reply, and change a long "
    "file with `edit_file` rather than writing it whole again.")

#: The `<tool_surface>` sentence of a READING workshop launch on either HTTP provider.
HTTP_READER_TOOL_SURFACE_TEXT = (
    "You have no shell. Your tools are `read_file`, `write_file`, `edit_file`, `list_files` "
    "and `grep_file`, which reach your work tree and nothing else; you are served no host "
    f"operation. `read_file` returns at most {HTTP_READ_PAGE_LINES:,} lines and "
    f"{HTTP_READ_PAGE_BYTES:,} bytes per call: read each file from its first line, continuing "
    "from `next_start_line` until it is null, and search one file with `grep_file`. Write your "
    "one document with `write_file`: it reaches your tree that way only, never through the text "
    "of your reply. A reply longer than your output limit is cut and none of its tool calls "
    "run, and a session whose last reply is cut is reviewed again from the seeds: write your "
    "document in a reply of its own, and keep each reply short. The seeds and `CONTRACT.md` "
    "are read-only: a write to one is refused.")

#: The lineage-home directory of each provider that runs a workshop leaf, keyed by PROVIDER
#: (issue #534 PR-J3-b): the two CLIs keep their backend tokens, the two HTTP providers share
#: one transport and one directory, `http` (`workshop_tree.LINEAGE_BACKENDS`). The one place the
#: provider-to-dirname mapping lives: the conductor's `_workshop_backend`, `record_launch`'s
#: workshop block and the launch-record audit all read it.
WORKSHOP_LINEAGE_BACKENDS: dict[str, str] = {
    "claude_cli": "claude",
    "codex_cli": "codex",
    "anthropic_api": "http",
    "openai_compatible": "http",
}

#: What a claude workshop leaf's file tools are: the reader row's opening, and the producer
#: row's before it names the wrapper.
_CLAUDE_FILE_TOOLS_TEXT = ("You have no shell: your file tools (Read, Write, Edit, Glob, Grep) "
                           "reach your work tree and nothing else.")

#: What a workshop launch prompt's `<tool_surface>` slot says the leaf holds, by the provider
#: the launch request names (`workshop_provider`): the one sentence of the contract that
#: differs by transport (`docs/design/producer_workshop_leaf.md` §Provider neutrality). A
#: provider with no row cannot serve a workshop launch (`_validate_leaf_launch_request_payload`).
WORKSHOP_TOOL_SURFACE_TEXT: dict[str, str] = {
    "claude_cli": (_CLAUDE_FILE_TOOLS_TEXT[:-1]
                   + ", and the host's operations are the `wrapper` MCP server's tools "
                     "(`mcp__wrapper__<operation>`)."),
    "codex_cli": ("Your shell and patch tools run with your work tree as the working directory, "
                  "and the host reads your tree and nothing else you write. A command you run "
                  "there is not one of the host's operations: those are the `wrapper` MCP "
                  "server's tools (`mcp__wrapper__<operation>`)."),
    "anthropic_api": HTTP_PRODUCER_TOOL_SURFACE_TEXT,
    "openai_compatible": HTTP_PRODUCER_TOOL_SURFACE_TEXT,
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
#: (`workflow_conductor.Conductor._run_leaf_reviewer_substep`, issue #547).
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
    "claude_cli": (_CLAUDE_FILE_TOOLS_TEXT
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
    "anthropic_api": HTTP_READER_TOOL_SURFACE_TEXT,
    "openai_compatible": HTTP_READER_TOOL_SURFACE_TEXT,
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
# Launch flags
# --------------------------------------------------------------------------------------

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
