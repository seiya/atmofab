#!/usr/bin/env python3
"""Post-mortem diagnostics for incomplete (``dangling``) child launches.

Background
----------
When a child ``Agent`` launch hangs or is interrupted *after* ``record-launch``
opened the active_child window but *before* the child returned, the orchestration
is left mid-launch:

- ``active_child_agent_run_id.txt`` / ``active_children/<arid>.txt`` are set,
- ``child_returns/<arid>.txt`` is absent,
- no terminal ``agent_runs.jsonl`` row exists for ``<arid>``.

The conductor is a plain Python process with no host/parent session, but each
leaf is launched with its ``agent_run_id`` pinned as the Claude session id
(``claude --session-id <arid>``), so the dangling child's OWN transcript is
directly addressable as ``<projects-root>/<slug>/<arid>.jsonl`` — the operator's
``~/.claude/projects``, which since Z4 (issue #171) is the only root there is. A
``pure-function leaf`` is prepared no private ``CLAUDE_CONFIG_DIR``, so it writes where every
other claude session writes. Issue #63 had put an AGENTIC leaf's transcript in the
orchestration's private home and issue #64 made that home durable
(``~/.atmofab/homes/<orchestration_id>/claude``); that leaf is deleted, the home is no longer
prepared, and a transcript written under one before the cut is not reachable from here —
recorded as an accepted loss, and the files are still on disk for
``tools/prune_workflow_homes.py``. Every "when it is still there" below now reads against a
``~/.claude`` an operator may clean. Its last activity, the dead-air before
the abort, and any final API error are the decisive evidence for whether the
launch was a retryable transport blip or a hang; this module recovers them from
that transcript when it is still on disk. That is the only thing it reads a transcript
for: each leaf's token usage is recorded in-repo from the leaf's own output (issue #47,
``tools/leaf_usage.py``), and no workflow path reads ``~/.claude``. Older runs may additionally carry a persisted
``launch_incident.runtime.<uuid>.json`` snapshot, which the audit renderer
surfaces; the conductor writes no new ones.

It is intentionally dependency-free (stdlib only) and **defensive** against the
Claude Code transcript format: parse failures degrade to raw tails and
``found=False`` markers rather than raising.

Callers:
- ``tools/audit_orchestration.py`` invokes it on demand for after-the-fact analysis
  of a dangling launch (open active_child window with no child return / terminal run).
  (Legacy ``launch_incident.runtime.<uuid12>.json`` snapshots from older runs are also
  surfaced when present; the conductor does not write new ones.)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

# The canonical leaf-usage shape lives in its own module: it is the contract the conductor,
# the HTTP transport and the audit all record against, where THIS module is post-mortem
# forensics over the machine-local `~/.claude` transcripts. The pure-attempt sum keys below
# derive from its token-class vocabulary so the two cannot drift.
# Module-level, absolute, and NOT shimmed: this module needs `tools.operator_private_root` to do
# its transcript work, so a consumer that cannot import `tools` must fail here, at import
# time, rather than later inside a caller's `except Exception` (issue #130). Two guards
# hold that, and NEITHER is in this module's own test file: the placement is pinned by
# `tools/tests/test_audit_orchestration.py::DiagnosticsFailsAtImportTimeTests` (an AST
# scan for function-body `tools.*` imports), and `build_launch_incident`'s refusal to
# swallow by `tools/tests/test_orchestration_diagnostics.py::CollectorsDoNotSwallowTests`.
from tools.leaf_usage import LEAF_TOKEN_CLASS_KEYS
from tools.operator_private_root import claude_leaf_projects_roots

# Terminal agent_runs statuses: a row carrying one of these (or any finished_at)
# proves the child completed and the window is NOT dangling.
_TERMINAL_RUN_STATUSES: frozenset[str] = frozenset(
    {"pass", "fail", "fail_closed", "blocked", "timeout", "cancel", "error"}
)

# Substrings that mark a transcript record as an interrupt/abort rather than real
# agent activity. Matched case-insensitively against text blocks.
_INTERRUPT_MARKERS: tuple[str, ...] = (
    "[request interrupted",
    "request interrupted by user",
)


def _read_json(path: Path) -> dict[str, Any] | None:
    # `ValueError` (not just `json.JSONDecodeError`, which subclasses it) because
    # `read_text` raises `UnicodeDecodeError` — also a `ValueError`, NOT an `OSError`
    # — on non-UTF-8 bytes. Catching only the narrower pair let a corrupt-byte file
    # escape this module's "degrade, never raise" contract.
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return records
    for line in text.splitlines():
        token = line.strip()
        if not token:
            continue
        try:
            payload = json.loads(token)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records


def _parse_ts(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp into a tz-aware datetime (``Z`` or offset)."""
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _orch_root(repo_root: Path, orchestration_id: str) -> Path:
    return repo_root / "workspace" / "orchestrations" / orchestration_id


def detect_dangling_active_child(
    repo_root: Path, orchestration_id: str
) -> dict[str, Any] | None:
    """Detect an open active_child window with no child return / terminal run.

    Returns a dict describing the (primary) dangling child, or ``None`` when no
    window is open. Detection keys off the **backend-neutral** per-arid markers
    ``active_children/<arid>.txt``, which ``record_launch`` writes for ALL backends
    — the Claude-only ``active_child_agent_run_id.txt`` pointer is used merely to
    choose the primary among several dangling children (Claude is sequential, so it
    is the one). codex has no such pointer but still leaves the per-arid
    marker, so keying off it covers every backend. Path conventions mirror
    ``tools/orchestration_runtime.py`` (``_active_children_dir`` /
    ``_child_returns_dir``); paths are rebuilt as strings to avoid importing the
    heavy runtime module.
    """
    root = _orch_root(repo_root, orchestration_id)
    markers_dir = root / "active_children"

    # Candidate arids = the Claude sequential pointer (written FIRST by record_launch,
    # so a crash before the per-arid marker leaves a pointer-only open window that
    # still blocks the next record-launch) UNION the backend-neutral per-arid markers
    # (written for ALL backends; codex has no pointer).
    candidate_arids: list[str] = []
    try:
        pointed = (root / "active_child_agent_run_id.txt").read_text(encoding="utf-8").strip()
    except OSError:
        pointed = ""
    if pointed:
        candidate_arids.append(pointed)
    if markers_dir.is_dir():
        for m in sorted(markers_dir.glob("*.txt")):
            if m.stem and m.stem not in candidate_arids:
                candidate_arids.append(m.stem)
    if not candidate_arids:
        return None

    # A terminal agent_runs row proves completion.
    terminal_arids: set[str] = set()
    for run in _read_jsonl(root / "agent_runs.jsonl"):
        rid = str(run.get("agent_run_id") or "").strip()
        if not rid:
            continue
        status = str(run.get("status") or "").strip().lower()
        if run.get("finished_at") or status in _TERMINAL_RUN_STATUSES:
            terminal_arids.add(rid)
    # A child diverted to agent_runs_invalid.jsonl (terminal-payload validation
    # failure: sandbox / session-id / output-manifest) DID reach record-agent-run —
    # record_agent_run raises before clearing the marker AND before appending to
    # agent_runs.jsonl, so without this it would be misclassified as an abandoned
    # (launch_incomplete_active_child) launch, overwriting the real failure. It is an
    # invalid terminal ATTEMPT, not a dangling launch.
    attempted_arids: set[str] = set()
    for rec in _read_jsonl(root / "agent_runs_invalid.jsonl"):
        rid = str(rec.get("agent_run_id") or "").strip()
        if rid:
            attempted_arids.add(rid)

    def _is_dangling(arid: str) -> bool:
        # A child-return ack closes the window even before the terminal run lands.
        if (root / "child_returns" / f"{arid}.txt").is_file():
            return False
        return arid not in terminal_arids and arid not in attempted_arids

    dangling = [a for a in candidate_arids if _is_dangling(a)]
    if not dangling:
        return None

    # Launch metadata per arid from the phase_state_log record_launch events.
    launch_meta: dict[str, dict[str, Any]] = {}
    for entry in _read_jsonl(root / "phase_state_log.jsonl"):
        if entry.get("event") != "record_launch":
            continue
        rid = str(entry.get("agent_run_id") or "").strip()
        if rid and rid not in launch_meta:
            launch_meta[rid] = {
                "ts": entry.get("ts") or entry.get("timestamp"),
                "node_key_safe": entry.get("node_key_safe"),
                "step": entry.get("step"),
            }

    # Primary = the Claude sequential pointer (read above) if it is itself dangling,
    # else the most recently launched dangling child (what was in flight).
    def _launch_dt(arid: str) -> datetime:
        ts = (launch_meta.get(arid) or {}).get("ts")
        return _parse_ts(ts) or datetime.min.replace(tzinfo=timezone.utc)

    primary = pointed if pointed in dangling else max(dangling, key=_launch_dt)

    meta = launch_meta.get(primary) or {}
    launch_recorded_at: str | None = meta.get("ts")
    node_key_safe: str | None = meta.get("node_key_safe")
    step: str | None = meta.get("step")
    response = _read_json(root / "launches" / f"{primary}.response.json") or {}
    if launch_recorded_at is None:
        launch_recorded_at = response.get("started_at")

    # substep is not in phase_state_log; recover from the launch request when present.
    request = _read_json(root / "launches" / f"{primary}.request.json") or {}
    substep = request.get("substep")
    if node_key_safe is None:
        node_key_safe = request.get("node_key") or request.get("node_key_safe")
    if step is None:
        step = request.get("step")

    elapsed_seconds: float | None = None
    launched_dt = _parse_ts(launch_recorded_at)
    if launched_dt is not None:
        elapsed_seconds = (datetime.now(timezone.utc) - launched_dt).total_seconds()

    return {
        "agent_run_id": primary,
        "node_key_safe": node_key_safe,
        "step": step,
        "substep": substep,
        "launch_recorded_at": launch_recorded_at,
        "elapsed_seconds": elapsed_seconds,
        "dangling_child_arids": dangling,
    }


def _record_text_blocks(record: dict[str, Any]) -> list[str]:
    """Extract human-readable text fragments from a transcript record."""
    texts: list[str] = []
    tur = record.get("toolUseResult")
    if isinstance(tur, str):
        texts.append(tur)
    message = record.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    texts.append(block["text"])
                elif block.get("type") == "tool_result":
                    rc = block.get("content")
                    if isinstance(rc, str):
                        texts.append(rc)
                    elif isinstance(rc, list):
                        for sub in rc:
                            if isinstance(sub, dict) and isinstance(sub.get("text"), str):
                                texts.append(sub["text"])
    return texts


def _is_interrupt_record(record: dict[str, Any]) -> bool:
    for text in _record_text_blocks(record):
        low = text.lower()
        if any(marker in low for marker in _INTERRUPT_MARKERS):
            return True
    return False


# HTTP statuses that the Claude transport retries / that are transient and safe to
# `--resume` without investigation (overload, rate limit, gateway/server blips).
_RETRYABLE_API_STATUSES: frozenset[int] = frozenset({429, 500, 502, 503, 529})


def _api_error(record: dict[str, Any]) -> dict[str, Any] | None:
    """Extract a transient API-error marker from a transcript record.

    Claude Code writes a synthetic assistant record with ``isApiErrorMessage:true``
    and ``apiErrorStatus:<code>`` when a model turn fails (e.g. ``529 Overloaded``).
    Surfacing it structurally lets the operator see at a glance that a dangling
    launch was a transient transport failure (safe to resume) rather than a hang.
    """
    if record.get("isApiErrorMessage") is not True:
        return None
    status = record.get("apiErrorStatus")
    status_int = status if isinstance(status, int) else None
    blocks = _record_text_blocks(record)
    message = blocks[-1][:200] if blocks else None
    return {
        "status": status_int,
        "message": message,
        "retryable": status_int in _RETRYABLE_API_STATUSES,
    }


def api_error_from_records(records: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """Derive the transient API error to report from a sequence of transcript records.

    Reports an API error only when it is the FINAL relevant activity: any later
    non-interrupt, non-error record means the error was recovered, so it is cleared
    (otherwise a later unrelated hang would be mislabeled as a retryable transport
    blip). Shared by `summarize_transcript_tail` and the audit renderer's fallback
    for legacy incident snapshots that predate the structured `api_error` field but
    still carry `isApiErrorMessage` / `apiErrorStatus` in their `raw_tail`.
    """
    if not records:
        return None
    api_error: dict[str, Any] | None = None
    for record in records:
        if not isinstance(record, dict):
            continue
        if _is_interrupt_record(record):
            continue
        err = _api_error(record)
        api_error = err if err is not None else None
    return api_error


def _last_tool_use(record: dict[str, Any]) -> dict[str, Any] | None:
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, list):
        return None
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            return {
                "name": block.get("name"),
                "input_preview": json.dumps(block.get("input", {}), ensure_ascii=False)[:200],
            }
    return None


def summarize_transcript_tail(path: Path, *, n: int = 40) -> dict[str, Any]:
    """Summarize the last ``n`` records of a transcript jsonl.

    Returns last activity timestamp, last tool_use, interrupt-marker presence,
    the dead-air gap (last real activity -> interrupt / now), and the raw tail
    records (so the decisive evidence survives ``~/.claude`` cleanup even if the
    parsing assumptions later drift).
    """
    if not path.exists():
        return {"found": False, "path": str(path)}
    records = _read_jsonl(path)
    tail = records[-n:] if len(records) > n else records

    last_activity_ts: str | None = None
    last_activity_dt: datetime | None = None
    last_event_type: str | None = None
    last_tool: dict[str, Any] | None = None
    interrupt_ts: str | None = None
    interrupt_dt: datetime | None = None
    interrupt_text: str | None = None

    for record in records:
        ts = record.get("timestamp") or record.get("ts")
        dt = _parse_ts(ts)
        if _is_interrupt_record(record):
            if isinstance(ts, str):
                interrupt_ts = ts
            interrupt_dt = dt
            blocks = _record_text_blocks(record)
            interrupt_text = blocks[-1][:200] if blocks else None
            continue
        if isinstance(ts, str):
            last_activity_ts = ts
        if dt is not None:
            last_activity_dt = dt
        last_event_type = record.get("type")
        tu = _last_tool_use(record)
        if tu is not None:
            last_tool = tu

    # Surface an API error only when it is the final relevant activity (see helper).
    api_error = api_error_from_records(records)

    dead_air_seconds: float | None = None
    if last_activity_dt is not None:
        end_dt = interrupt_dt or datetime.now(timezone.utc)
        dead_air_seconds = (end_dt - last_activity_dt).total_seconds()

    return {
        "found": True,
        "path": str(path),
        "record_count": len(records),
        "last_activity_ts": last_activity_ts,
        "last_event_type": last_event_type,
        "last_tool_use": last_tool,
        "interrupted": interrupt_ts is not None,
        "interrupt_ts": interrupt_ts,
        "interrupt_text": interrupt_text,
        "dead_air_seconds": dead_air_seconds,
        "api_error": api_error,
        "raw_tail": tail,
    }


# The vocabulary of `match_method` is closed, and the two halves have different
# owners. LIVE incidents are always `session_id`: the conductor pins the leaf's
# Claude session id to its agent_run_id (`claude --session-id <arid>`), so the
# lookup below is an exact filename match on that id. `tool_use_id` /
# `arid_in_body` belong ONLY to persisted `launch_incident.runtime.*.json`
# snapshots written by the host-session-era conductor (`resolve_transcripts`,
# deleted in `977bd75`); nothing produces them now, and the audit renderer still
# has to display them. See `tools/audit_orchestration.py::_render_incident_body`.
LEAF_TRANSCRIPT_MATCH_METHOD = "session_id"


class LeafTranscriptMatch(NamedTuple):
    """Where a leaf transcript was found, not just which file it is.

    ``projects_root`` is the root returned by ``claude_leaf_projects_roots`` that
    actually held the hit. Since Z4 (issue #171) there is exactly one, the operator's
    ``~/.claude/projects``: a pure leaf is prepared no private home, so it writes where every
    claude session writes. The field is KEPT rather than dropped — it says where the search
    landed, which an incident has to state whether or not there is a choice, and a second root
    reappearing would otherwise arrive unrecorded.
    """

    path: Path
    projects_root: Path


def _locate_leaf_transcript(child_arid: str, repo_root: Path,
                            orchestration_id: str | None = None) -> LeafTranscriptMatch | None:
    """Locate a conductor-spawned leaf's OWN transcript, or ``None``.

    The conductor pins each leaf's Claude session id to its ``agent_run_id``
    (``claude --session-id <arid>``, workflow_conductor.spawn_leaf), so the leaf's
    transcript is directly addressable as ``<projects-root>/<slug>/<arid>.jsonl``
    — no host/parent session is involved. Mirrors the wildcard-slug lookup in
    ``workflow_conductor._claude_session_resumable`` so a leaf that ran under a
    slightly different cwd slug is still found; the arid (a uuid) is unique, so the
    wildcard cannot collide across projects. The operator's ``~/.claude`` is searched,
    through the canonical ``claude_leaf_projects_roots`` resolver — since Z4 (issue #171)
    the only root, because a pure leaf is prepared no private home. Returns WHICH root held
    the hit alongside the path: an incident record that says only "found" cannot say where it
    looked, and the resolver is the one place a second root would come back.
    """
    arid = str(child_arid or "").strip()
    if not arid:
        return None
    try:
        roots = claude_leaf_projects_roots(repo_root, orchestration_id)
    except (OSError, ValueError):
        return None
    for projects in roots:
        try:
            matches = sorted(projects.glob(f"*/{arid}.jsonl"))
        except OSError:
            continue
        if matches:
            return LeafTranscriptMatch(path=matches[0], projects_root=projects)
    return None


# Token fields summed across a pure leaf's repair attempts: the CLI result-envelope
# `usage` keys the conductor persists per attempt into bundle_meta.json /
# verdict_meta.json (`per_attempt[].usage`), the ~/.claude-free provenance for
# pure-leaf cost (transcripts are ephemeral; these files are in-repo). Exactly the
# CLI token classes — `total_tokens` is derived here, and `assistant_turns` is
# meaningless for a single-turn pure envelope, so neither belongs.
_PURE_ATTEMPT_USAGE_KEYS: tuple[str, ...] = LEAF_TOKEN_CLASS_KEYS


# The structural discriminator of a pure-leaf meta envelope. `per_attempt` is the
# measurement payload the conductor always writes — by `_write_bundle_meta` /
# `_write_verdict_meta` for the generate pair and `_write_compile_generate_meta` /
# `_write_compile_verify_meta` for the compile pair, all four through one writer —
# and is what an unrelated or stale JSON document at the
# same path will not carry. Keying on it (rather than on common keys like `result`
# / `attempts`) keeps a foreign `{"result": "ok"}` from being reported as a
# pure-leaf row of all-zero metrics.
_PURE_META_PAYLOAD_KEY = "per_attempt"


def _nonneg_int_or_none(value: Any) -> int | None:
    """Return a JSON value as a non-negative count, or None when it is not one.

    Only a real non-negative `int` qualifies. A `bool` (an `int` subclass), a
    float (`1.9`; or `Infinity`, which Python's JSON decoder accepts), a numeric
    string, or a negative value is corrupt metadata and yields None. Rejection is
    structural — the `isinstance` guard runs *before* any coercion, so nothing is
    ever passed to `int()` and the aggregation cannot truncate `1.9`, sign-flip a
    negative, or raise `OverflowError` on `Infinity`. Callers pick their own
    fallback (0 for a token sum, the valid-attempt count for `attempts`).
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _nonneg_int(value: Any) -> int:
    """`_nonneg_int_or_none` with 0 as the fallback (token sums)."""
    coerced = _nonneg_int_or_none(value)
    return 0 if coerced is None else coerced


def _sum_pure_attempt_usage(
    per_attempt: list[dict[str, Any]],
) -> tuple[dict[str, int], list[str]]:
    """Sum `per_attempt[].usage` token counts and collect the per-attempt models.

    Each attempt is `{"agent_run_id", "model": str|None, "usage": dict|None}`
    (conductor `_run_pure_producer_substep` / `_run_pure_reviewer_substep`). Missing
    or malformed `usage` / `model` entries are skipped rather than raising —
    diagnostics degrade, never break (see `_nonneg_int`). `models` preserves
    attempt order (a repair loop may resolve a different model per turn; the alias
    is recorded, never pinned).
    """
    totals = {k: 0 for k in _PURE_ATTEMPT_USAGE_KEYS}
    models: list[str] = []
    for attempt in per_attempt:
        if not isinstance(attempt, dict):
            continue
        usage = attempt.get("usage")
        if isinstance(usage, dict):
            for key in _PURE_ATTEMPT_USAGE_KEYS:
                totals[key] += _nonneg_int(usage.get(key))
        model = attempt.get("model")
        if isinstance(model, str) and model:
            models.append(model)
    totals["total_tokens"] = sum(totals[k] for k in _PURE_ATTEMPT_USAGE_KEYS)
    return totals, models


def _summarize_one_pure_meta(meta: dict[str, Any] | None) -> dict[str, Any]:
    """Project one pure-leaf per-attempt record into an A/B metrics row.

    All four of them — `bundle_meta.json` / `verdict_meta.json` (generate) and
    `compile_generate_meta.json` / `compile_verify_meta.json` (compile) — share a
    schema: `{result, failure_category, attempts, prompt_contract_version,
    per_attempt[], failure_excerpt?}`. `result` is `pass`/`fail`; `attempts`
    counts every LAUNCH (== len(per_attempt)). Not every launch is a repair turn:
    an opt-in `--wait-usage-reset` WAIT re-launches the same substep in place
    without advancing the repair budget, so `repair_turns = attempts - 1 - waits`.
    The wait count is recovered from `per_attempt` (no separate counter is
    persisted): a `pure_transport` attempt that is NOT the terminating (last) row
    is necessarily a wait — a transport death that is not waited is terminal, hence
    always the last row. `found=False` when the file is absent, unparseable, or not
    a pure-leaf meta envelope (see `_PURE_META_PAYLOAD_KEY`). When `attempts` is
    absent or corrupt it falls back to the count of structurally valid (dict)
    attempt entries.
    """
    if not isinstance(meta, dict) or _PURE_META_PAYLOAD_KEY not in meta:
        return {"found": False}
    per_attempt = meta.get(_PURE_META_PAYLOAD_KEY)
    per_attempt = per_attempt if isinstance(per_attempt, list) else []
    valid_attempt_count = sum(1 for a in per_attempt if isinstance(a, dict))
    usage_total, models = _sum_pure_attempt_usage(per_attempt)
    attempts = _nonneg_int_or_none(meta.get("attempts"))
    if attempts is None:
        attempts = valid_attempt_count
    # A non-terminal `pure_transport` row is a waited-and-relaunched usage limit, not a repair
    # turn: exclude it from the repair count so a substep that only waited (never repaired) reads
    # `repair_turns=0`. per_attempt[:-1] drops the terminating attempt (the only row a genuine
    # terminal transport death would occupy).
    usage_wait_rows = sum(
        1 for a in per_attempt[:-1]
        if isinstance(a, dict) and a.get("failure_category") == "pure_transport")
    return {
        "found": True,
        "result": meta.get("result"),
        "attempts": attempts,
        "repair_turns": max(attempts - 1 - usage_wait_rows, 0),
        "failure_category": meta.get("failure_category"),
        "prompt_contract_version": meta.get("prompt_contract_version"),
        "usage_total": usage_total,
        "models": models,
    }


#: The per-attempt record each pure phase's producer / reviewer writes, by phase. The two files
#: of a phase share one schema (`_summarize_one_pure_meta`) and differ only in name and
#: directory, so the rollup reads them through one function rather than two.
#: Each phase's pure per-attempt records, keyed by the SUBSTEP that wrote one. A mapping
#: rather than a producer/reviewer PAIR since Z3 (issue #169): `validate` has one pure leaf,
#: not two, and a table shaped for exactly two would have had to invent a second.
PURE_LEAF_META_FILES: dict[str, dict[str, str]] = {
    "generate": {"generate": "bundle_meta.json", "verify": "verdict_meta.json"},
    "compile": {"generate": "compile_generate_meta.json",
                "verify": "compile_verify_meta.json"},
    "validate": {"judge": "judge_meta.json"},
}


def summarize_pure_leaf_metas(artifact_dir: Path, phase: str) -> dict[str, Any]:
    """A/B metrics for one phase's pure producer / reviewer leaves in one artifact directory.

    Reads the phase's per-attempt records — the in-repo, ~/.claude-free per-attempt
    usage/model provenance — and returns one row per SUBSTEP plus `found`. For `generate` that
    is `<source_dir>/bundle_meta.json` + `verdict_meta.json` (Z2, milestone M-E); for `compile`
    it is `<ir_ref>/compile_generate_meta.json` + `compile_verify_meta.json` (Z1, issue #168);
    for `validate` it is `<run_node_dir>/judge_meta.json` alone (Z3, issue #169). The keys name
    the SUBSTEP, which is what the A/B table compares, not the phase — so a `compile` node's
    rows read `generate` / `verify` exactly as a `generate` node's do, and a `validate` node's
    reads `judge`.

    Each substep key holds a per-leaf row (see `_summarize_one_pure_meta`); `found` is true
    when any of the phase's files was present. Best-effort: never raises. An agentic node has neither file and
    yields `found=False`, so the caller can tell a pure node from an agentic one by presence
    alone. The row carries no directory key: the caller passes the directory in and owns how it
    labels the result (the audit rollup labels it repo-relative), so there is no second,
    conflicting notion of the same field.

    `phase` is REQUIRED and has no default. It selects which filenames are read, and the phases'
    records live in different directories under different names — so a caller that forgot it
    would silently read the wrong ones and report `found=False`, which is indistinguishable from
    an agentic node. A caller that has not decided must be refused, not defaulted.

    An UNKNOWN phase returns the same shape a known phase with no records would, except that it
    can name no substeps: `{"found": False}`. "Best-effort: never raises" is the contract every
    caller relies on, and a `KeyError` out of a DIAGNOSTICS helper would break an audit rather
    than report a gap.
    """
    rows = {substep: _summarize_one_pure_meta(_read_json(artifact_dir / basename))
            for substep, basename in PURE_LEAF_META_FILES.get(phase, {}).items()}
    return {**rows, "found": any(row.get("found") for row in rows.values())}


def build_launch_incident(
    repo_root: Path, orchestration_id: str
) -> dict[str, Any] | None:
    """Assemble a launch-incident report, or ``None`` if no dangling window.

    Combines dangling-child detection (in-repo artifacts) with the dangling leaf's
    OWN ``~/.claude`` transcript. The conductor pins each leaf's Claude session id
    to its ``agent_run_id``, so that transcript is directly addressable by the
    child arid (no host/parent session is needed); it yields the child's last
    activity, the dead-air before the abort, and the final API error (so a
    retryable 529 is distinguishable from other failures). Degrades gracefully to
    the in-repo facts when ``~/.claude`` is absent or cleaned. Persisted
    ``launch_incident.runtime.*.json`` snapshots from older runs are additionally
    surfaced by the audit renderer.

    Only conductor-spawned leaves (arid-pinned sessions) are correlated: a transcript
    is located by ``<projects-root>/<slug>/<arid>.jsonl`` and by nothing else.
    """
    dangling = detect_dangling_active_child(repo_root, orchestration_id)
    if dangling is None:
        return None

    match = _locate_leaf_transcript(
        dangling["agent_run_id"], repo_root, orchestration_id)
    if match is not None:
        child = summarize_transcript_tail(match.path)
        # `summarize_transcript_tail` is a tail summarizer and knows nothing about
        # HOW the file was found, so the locator's answer is stamped here — the same
        # split the deleted host-session code used.
        child["match_method"] = LEAF_TRANSCRIPT_MATCH_METHOD
        child["matched_projects_root"] = str(match.projects_root)
    else:
        child = {"found": False,
                 # OPERATOR-FACING (`audit_orchestration` prints it), so it must name a place
                 # that was actually searched. It said "private home pruned" until Z4 (issue
                 # #171) removed that root — pointing an operator at a directory this function
                 # does not look in. A pre-Z4 agentic run's transcript IS under one, and is
                 # unreachable from here; that is the accepted loss the module docstring
                 # records, and naming it is how an operator diagnosing an old run learns it.
                 "reason": "no leaf transcript located under ~/.claude/projects (cleaned, or "
                           "a pre-Z4 agentic run whose transcript is in the orchestration's "
                           "private home, which is no longer searched)"}

    abort_marker = None
    if child.get("found"):
        abort_marker = {
            "interrupted": child.get("interrupted"),
            "interrupt_ts": child.get("interrupt_ts"),
            "interrupt_text": child.get("interrupt_text"),
            "last_activity_ts": child.get("last_activity_ts"),
            "dead_air_seconds": child.get("dead_air_seconds"),
            "api_error": child.get("api_error"),
        }

    return {
        "schema": "launch_incident/v1",
        "orchestration_id": orchestration_id,
        "detected_at": datetime.now(timezone.utc).isoformat(),
        "dangling_child": dangling,
        "transcripts": {"child_transcript": child},
        "abort_marker": abort_marker,
    }
