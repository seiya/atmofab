"""What a codex launch's tool outputs showed its model: the rollout reader behind the host's
refusal of a cut read on a codex READING workshop lineage (issue #547).

Codex cuts the MIDDLE of a tool output over its token budget, and the only record of what the
model RECEIVED is the thread's rollout,
`<config>/sessions/<yyyy>/<mm>/<dd>/rollout-<timestamp>-<thread_id>.jsonl` in the lineage's CLI
home. A warm `exec resume` appends to the same file, so one launch's own rows are the ones after
the LAST `event_msg` whose payload is `task_started`. Every `response_item` whose payload type
ends in `_call_output` (`custom_tool_call_output` for the JavaScript `exec` tool,
`function_call_output` for `wait`) carries what one call returned to the model: a string, or a
list of `{type: "input_text", text}` items of any length.

A cut text holds the inline marker `…<N> tokens truncated…`, usually under a two-line header
`Warning: truncated output (original token count: <N>)` / `Total output lines: <M>`. The header
can sit at a text's start (the `exec` result's own budget), inside a JSON string the leaf's
script emitted (an `exec_command` result it stringified, newlines escaped), or after a prefix
the script printed. So both are matched, unanchored, in raw and escaped spellings, and a text
counts once however many of either it holds. The digits are what tell a cut from a quotation of
the codex reader row, which spells both with `N`.

The rollout is NOT the whole record of what the model received in one case: when the leaf
RAISES the `exec` result's budget (its first-line `// @exec:` pragma), the rollout keeps the
text whole and the cut happens later, on the way to the model, with the marker and no header
(measured on codex-cli 0.164.0, `test_bwrap_simulation.CodexReadingLeafCliTests` MR5-e: a
600,000-character text in the rollout, about 48,000 characters reaching the model). At the
default budget no text over `CODEX_EXEC_OUTPUT_BUDGET_BYTES` is ever recorded uncut, so a
text over it is counted as cut too: it is either cut, or the trace of a raised budget, which
the codex reader row forbids. (A default-budget cut text, its header included, is itself just
over that size.)

What this does not see, by construction: an output the leaf's script cut itself (a lowered
`exec_command` budget) and then stripped of both marks before emitting it. The host's own
capture of each command's bytes (`workflow_conductor`, the byte rule) closes that shape at the
default budget; the lowered-budget variant is the residual `docs/ORCHESTRATION.md` names.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.pure_leaf import CODEX_EXEC_OUTPUT_BUDGET_BYTES

#: The header codex writes above a cut output. Its two lines may be joined by a raw newline or
#: by an escaped one (one or more backslashes, as JSON nests).
CUT_HEADER_RE = re.compile(
    r"Warning: truncated output \(original token count: \d+\)(?:\n|\\+n)"
    r"Total output lines: \d+")

#: The inline marker codex writes where it removed the middle. `…` is U+2026, which a script's
#: own JSON encoder may have escaped.
CUT_MARKER_RE = re.compile(r"(?:…|\\+u2026)\d+ tokens truncated(?:…|\\+u2026)")


@dataclass(frozen=True)
class RolloutScan:
    """One launch's rollout, read. `reason` is None when it was readable; otherwise it says why
    not, and the counts are 0."""

    reason: str | None
    #: The `*_call_output` rows after the launch's `task_started`.
    output_rows: int = 0
    #: Of their texts, how many reached the model cut.
    cut_texts: int = 0


def text_is_cut(text: str) -> bool:
    """Whether one text a tool returned to the model reached it cut: it holds codex's cut
    header or marker, or it is longer than the default budget lets through whole (a raised
    budget, whose cut the rollout does not record)."""
    return bool(CUT_HEADER_RE.search(text) or CUT_MARKER_RE.search(text)
                or len(text.encode("utf-8", "backslashreplace"))
                > CODEX_EXEC_OUTPUT_BUDGET_BYTES)


def _texts(value: Any) -> list[str]:
    """Every string inside a call output: the string itself, or each item's text."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [t for item in value for t in _texts(item)]
    if isinstance(value, dict):
        return [t for item in value.values() for t in _texts(item)]
    return []


def rollout_paths(config_dir: Path, thread_id: str) -> list[Path]:
    """The rollout files of `thread_id` under a codex home's `config/` (the glob
    `Conductor._workshop_session_resumable` uses). The name's timestamp is fixed-width, so the
    pattern names the WHOLE thread id."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", thread_id or ""):
        return []
    try:
        return sorted((Path(config_dir) / "sessions").glob(
            f"*/*/*/rollout-????-??-??T??-??-??-{thread_id}.jsonl"))
    except OSError:
        return []


def scan_launch(config_dir: Path, thread_id: str | None) -> RolloutScan:
    """Read the rollout of `thread_id` and count, over the rows of its LAST launch, the tool
    outputs and how many of their texts reached the model cut. Unreadable — no thread id, no
    file or more than one, a row that is not JSON, no `task_started` — is a `reason`, never a
    zero: a caller that cannot read what the model saw has not shown it saw nothing cut."""
    if not isinstance(thread_id, str) or not thread_id.strip():
        return RolloutScan("the launch reported no thread id")
    found = rollout_paths(config_dir, thread_id.strip())
    if len(found) != 1:
        return RolloutScan(f"{len(found)} rollout files name thread {thread_id.strip()} "
                           f"under {Path(config_dir) / 'sessions'} (one is expected)")
    try:
        lines = found[0].read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        return RolloutScan(f"rollout {found[0].name} could not be read: {exc}")
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, RecursionError):
            return RolloutScan(f"rollout {found[0].name} line {number} is not JSON")
        if not isinstance(row, dict):
            return RolloutScan(f"rollout {found[0].name} line {number} is not an object")
        rows.append(row)
    starts = [i for i, row in enumerate(rows)
              if row.get("type") == "event_msg"
              and isinstance(row.get("payload"), dict)
              and row["payload"].get("type") == "task_started"]
    if not starts:
        return RolloutScan(f"rollout {found[0].name} holds no task_started row")
    output_rows = 0
    cut = 0
    for row in rows[starts[-1] + 1:]:
        payload = row.get("payload")
        if row.get("type") != "response_item" or not isinstance(payload, dict):
            continue
        if not str(payload.get("type") or "").endswith("_call_output"):
            continue
        output_rows += 1
        cut += sum(1 for text in _texts(payload.get("output")) if text_is_cut(text))
    return RolloutScan(None, output_rows, cut)
