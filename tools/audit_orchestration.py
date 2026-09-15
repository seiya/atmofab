#!/usr/bin/env python3
"""Read-only audit helper for workflow orchestrations.

Usage:
    python3 tools/audit_orchestration.py --orchestration-id <id> [--format json|markdown]

Collects and aggregates, from the in-repo records under
workspace/orchestrations/<id>/ (docs/WORKSPACE_LAYOUT.md is canonical for them):
- every `fail` / `fail_closed` transition in phase_state_log.jsonl (the `set_status`
  rows, with their reason), and the instant of the latest `fail_closed`
- failure_analysis.json (the host's own failure diagnosis) and its runtime / fallback
  sidecars
- Dangling launch (open active_child window with no child return / terminal run),
  correlated with the leaf transcript tail under the operator's ~/.claude/projects (see
  orchestration_diagnostics.build_launch_incident) — the one read outside the
  repository. Also surfaced: any persisted launch_incident.runtime.*.json snapshots
  (which survive after --resume clears the window or the transcript is expired)
- violations/*.json (sandbox enforcement)
- per-leaf token cost from the `usage` rows of agent_runs.jsonl
- the pure-leaf A/B metrics from bundle_meta.json / verdict_meta.json
- agent_runs.jsonl completion status, including substeps attempted more than once
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

# One import identity, always `tools.`: a bare-first shim would let `leaf_usage` and
# `tools.leaf_usage` coexist as two module objects once the root is on the path. The
# consumers below reach for other `tools.*` modules at run time, so a shim that merely
# makes THIS module importable buys nothing (issue #130).
try:
    from tools.leaf_usage import LEAF_USAGE_SOURCE_UNRECORDED, normalize_leaf_usage
    from tools.llm_config import LLM_LEAF_SUBSTEPS as _LLM_LEAF_SUBSTEPS
    from tools.orchestration_diagnostics import (
        api_error_from_records,
        build_launch_incident,
        summarize_pure_leaf_metas,
    )
except ModuleNotFoundError:  # pragma: no cover - import bootstrap for direct CLI execution
    _REPO_ROOT = Path(__file__).resolve().parent.parent
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    from tools.leaf_usage import LEAF_USAGE_SOURCE_UNRECORDED, normalize_leaf_usage
    from tools.llm_config import LLM_LEAF_SUBSTEPS as _LLM_LEAF_SUBSTEPS
    from tools.orchestration_diagnostics import (
        api_error_from_records,
        build_launch_incident,
        summarize_pure_leaf_metas,
    )


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Load records, ignoring malformed lines.

    Use `_load_jsonl_with_errors` when caller needs visibility into parse
    failures. This wrapper preserves the simple records-only interface for
    callers that don't need integrity reporting.
    """
    records, _errors = _load_jsonl_with_errors(path)
    return records


def _load_jsonl_with_errors(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Load records AND return a list of parse errors for integrity reporting.

    Each error entry is `{"path": str, "line_number": int, "message": str}`.
    Missing files return ([], []).
    """
    if not path.exists():
        return [], []
    records: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    text = path.read_text(encoding="utf-8")
    for idx, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            errors.append({
                "path": str(path),
                "line_number": idx,
                "message": str(exc),
            })
    return records, errors


def _orch_root(repo_root: Path, orchestration_id: str) -> Path:
    return repo_root / "workspace" / "orchestrations" / orchestration_id


def _load_json_if_dict(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _parse_ts(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp into a timezone-aware datetime.

    Accepts both `Z` (UTC) and offset suffixes. Naive timestamps are assumed
    UTC. Returns None if value is missing or unparseable.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip()
    # Python <3.11 only accepts +00:00, not Z.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _latest_fail_closed_at(phase_log: list[dict[str, Any]]) -> str | None:
    """The timestamp of the LATEST `fail_closed` transition in `phase_state_log.jsonl`.

    By parsed datetime rather than raw string max, which can disagree with chronological
    order under mixed offsets or precisions. `None` when the run never fail-closed.

    This is what is left of `collect_fail_closed_timeline`, which also sliced the five hook
    events preceding that instant; the hook trace it read is deleted with the leaf hook layer
    (issue #171).
    """
    fail_entries: list[tuple[datetime, str]] = []
    for entry in phase_log:
        new_state = entry.get("to") or entry.get("new_state", "")
        if new_state == "fail_closed" or (
            entry.get("event") == "set_status" and new_state == "fail_closed"
        ):
            raw_ts = entry.get("ts") or entry.get("timestamp")
            parsed = _parse_ts(raw_ts)
            if parsed is not None and isinstance(raw_ts, str):
                fail_entries.append((parsed, raw_ts))
    if not fail_entries:
        return None
    fail_entries.sort(key=lambda x: x[0])
    return fail_entries[-1][1]


def collect_agent_run_summary(
    agent_runs: list[dict[str, Any]],
    invalid_runs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Aggregate per-status counts across both terminal records and
    fail-validation fallback records.

    `agent_runs` carries successful terminal records; `invalid_runs` carries
    entries appended to `agent_runs_invalid.jsonl` when terminal payload
    validation rejected an otherwise-completed run.  Operators investigating
    a stuck workflow need to see those failed-validation runs in the
    per-status breakdown — not just in the separate `invalid_run_count`
    field — so they're rolled into `status_counts` (typically as `fail`).

    `repeated_substeps` lists every `(node_key, step, substep)` that has more than one
    row, with the statuses in the order the attempts STARTED. The conductor allocates a
    fresh `agent_run_id` per attempt (`docs/WORKSPACE_LAYOUT.md`), so the row count IS the
    attempt count, and a key that appears twice was retried — a repair loop, a transient
    retry, or a resume. Rows from both files count: a run rejected at terminal validation
    was an attempt too — and because it sits in a separate file, the two files are merged
    by `started_at` (parsed; a row without a parseable one keeps its file position, after
    the dated rows) rather than concatenated, which would show a rejected first attempt
    AFTER the retry that recovered from it and read as the retry having failed.
    A `step` row has no `substep` (`Build` is recorded as `agent_role: step`, `step:
    build`, `substep: None`) and is keyed with `substep=None`; a row without a `node_key`
    and a `step` (the conductor's own `orchestration` row) is not an attempt at anything
    and is left out.
    """
    status_counts: Counter = Counter()
    missing_entries: list[str] = []
    attempts: dict[tuple[str, str, str | None], list[str]] = {}
    for run in agent_runs:
        status = run.get("status", "unknown")
        status_counts[status] += 1
        if not run.get("finished_at"):
            missing_entries.append(run.get("agent_run_id", "?"))
    for run in (invalid_runs or []):
        status = run.get("status", "fail")
        status_counts[status] += 1
    merged = list(agent_runs) + list(invalid_runs or [])
    merged.sort(key=lambda run: (
        (0, ts) if (ts := _parse_ts(run.get("started_at"))) is not None
        else (1, datetime.min.replace(tzinfo=timezone.utc))))
    for run in merged:
        node_key, step, substep = (run.get(k) for k in ("node_key", "step", "substep"))
        if not (isinstance(node_key, str) and node_key and isinstance(step, str) and step):
            continue
        key = (node_key, step, substep if isinstance(substep, str) and substep else None)
        attempts.setdefault(key, []).append(str(run.get("status", "unknown")))
    repeated = [
        {"node_key": k[0], "step": k[1], "substep": k[2],
         "attempts": len(v), "statuses": v}
        for k, v in attempts.items() if len(v) > 1
    ]
    return {
        "status_counts": dict(status_counts),
        "missing_finished_at": missing_entries,
        "repeated_substeps": repeated,
    }


def collect_phase_state_failures(phase_log: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every `phase_state_log.jsonl` entry whose `to` is `fail` or `fail_closed`, in file
    order, whatever its `event`.

    File order rather than sorted by `ts`: the conductor appends, so file order is the
    chronology, and a row with an unparseable timestamp is still a recorded failure. These
    states are recorded only by the orchestration-status writer — `set_status`, and its
    `set_status_noop_replay` / `set_status_cleanup_retry` events on a repeated terminal
    call, which carry no reason — whose row is orchestration-level and names no node,
    step or attempt (the node-step transition writer records other states). Which node
    and attempt failed is `failure_analysis.json`'s to say; this section names the
    instant and the reason. A missing field renders as `None`; nothing is invented.
    """
    out: list[dict[str, Any]] = []
    for entry in phase_log:
        if not isinstance(entry, dict):
            continue
        new_state = entry.get("to") or entry.get("new_state")
        if new_state not in ("fail", "fail_closed"):
            continue
        out.append({
            "ts": entry.get("ts") or entry.get("timestamp"),
            "event": entry.get("event"),
            "to": new_state,
            "reason_code": entry.get("reason_code"),
            "reason_detail": entry.get("reason_detail"),
        })
    return out


# The `kind` the live `violations/` writer stamps (`_write_sandbox_enforcement_violation`).
SANDBOX_VIOLATION_KIND = "sandbox_enforcement_violation"


def collect_sandbox_violations(root: Path) -> dict[str, Any]:
    """Read `violations/*.json` — the sandbox enforcement record.

    One live writer, `_write_sandbox_enforcement_violation`, five reasons; the reason
    vocabulary is NOT restated here (`docs/WORKSPACE_LAYOUT.md` §`violations/` names it),
    the file's value is reported as written. Three states are told apart, because they
    mean three things: `directory_present=False` — no violation was recorded (since issue
    #171 PR-2 the directory is created only when one occurs); present and `records=[]` — a
    run from before that change pre-created it, and recorded nothing; present with records
    — the enforcement fired, and the reasons say what it saw.

    A record is a sandbox enforcement violation only when its `kind` says so. The
    directory also holds records of other kinds (`unauthorized_write_violation`, whose
    writer was deleted in issue #171 PR-2, is on disk in the corpus); those carry no
    `reason`, and reporting one under the sandbox heading with reason `unknown` would be
    a false finding about the leaf's confinement. They are kept in `records` with their
    `kind` and listed apart by the renderer.

    Raises on an unreadable or non-JSON file rather than dropping it: a violation record
    that cannot be read is exactly the one this section must not report as absent.
    """
    vdir = root / "violations"
    if not vdir.is_dir():
        return {"directory_present": False, "records": [],
                "by_reason": {}, "by_kind": {}}
    records: list[dict[str, Any]] = []
    by_reason: Counter = Counter()
    by_kind: Counter = Counter()
    for path in sorted(vdir.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise TypeError(f"{path.name}: violation record is not a JSON object")
        kind = str(payload.get("kind") or "unknown")
        reason = payload.get("reason")
        by_kind[kind] += 1
        if kind == SANDBOX_VIOLATION_KIND:
            by_reason[str(reason or "unknown")] += 1
        records.append({
            "file": path.name,
            "kind": kind,
            "reason": reason if isinstance(reason, str) else None,
            "agent_run_id": payload.get("agent_run_id"),
            # The live writer stamps `evaluated_at`; the retired one stamped `detected_at`.
            "evaluated_at": payload.get("evaluated_at") or payload.get("detected_at"),
        })
    return {"directory_present": True, "records": records,
            "by_reason": dict(by_reason), "by_kind": dict(by_kind)}


def _summarize_failure_analysis_doc(doc: dict[str, Any]) -> dict[str, Any]:
    failed_run = doc.get("failed_agent_run")
    failed_run = failed_run if isinstance(failed_run, dict) else {}
    step_results = doc.get("failed_step_results")
    step_results = step_results if isinstance(step_results, list) else []
    retries = doc.get("recommended_retry_decisions")
    refs = doc.get("launch_incident_refs")
    return {
        "status": doc.get("status"),
        "reason_code": doc.get("reason_code"),
        "reason_detail": doc.get("reason_detail"),
        "orchestration_status": doc.get("orchestration_status"),
        "failed_agent_run": {
            k: failed_run.get(k)
            for k in ("agent_run_id", "node_key", "step", "substep", "status")
        } if failed_run else None,
        "failed_step_results": [
            {"path": r.get("path"), "status": r.get("status")}
            for r in step_results if isinstance(r, dict)
        ],
        "recommended_retry_decision_count": len(retries) if isinstance(retries, list) else 0,
        "launch_incident_refs": [r for r in refs if isinstance(r, str)]
                                if isinstance(refs, list) else [],
    }


def collect_failure_analysis(root: Path) -> dict[str, Any]:
    """Read `failure_analysis.json` — the host's own diagnosis of a failed run — and every
    sidecar next to it.

    The canonical file is written once, exclusively; when it already existed the host
    writes `failure_analysis.runtime.<uuid12>.json` with `existing_file_status` saying
    whether the canonical one was `valid` for this run or `invalid` (stale), and an
    emergency path writes `failure_analysis.fallback.<uuid12>.json`
    (`tools/run_workflow.py::_write_failure_analysis`). All three are summarized; the
    canonical one is `canonical`, the others are `sidecars` in name order.

    `present=False` is not a verdict by itself: a run that passed writes none, a run that
    failed should have one. The renderer puts `orchestration_meta.json#status` next to it
    so the reader can tell which. An unreadable or non-JSON file RAISES — `audit()` records
    it under `diagnostic_failures` — rather than being read as absent (`TODO.md` records
    the `_load_json_if_dict` swallow this section deliberately does not use).
    """
    canonical_path = root / "failure_analysis.json"
    result: dict[str, Any] = {"present": canonical_path.is_file(),
                              "canonical": None, "sidecars": []}
    if result["present"]:
        doc = json.loads(canonical_path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            raise TypeError("failure_analysis.json is not a JSON object")
        result["canonical"] = _summarize_failure_analysis_doc(doc)
    sidecar_paths = sorted(
        list(root.glob("failure_analysis.runtime.*.json"))
        + list(root.glob("failure_analysis.fallback.*.json")))
    for path in sidecar_paths:
        doc = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            raise TypeError(f"{path.name} is not a JSON object")
        summary = _summarize_failure_analysis_doc(doc)
        summary["file"] = path.name
        summary["existing_file_status"] = doc.get("existing_file_status")
        result["sidecars"].append(summary)
    return result


def collect_token_cost_summary(
    repo_root: Path,
    meta: dict[str, Any] | None,
    agent_runs: list[dict[str, Any]],
    invalid_runs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Sum token cost over the orchestration's leaves.

    Every number comes from the durable ``usage`` field the conductor writes into
    ``agent_runs.jsonl`` via ``finalize_child``. Since issue #47 every leaf writes one: a
    normalized dict from its own output, or an explicit marker saying why it has no
    numbers. The two markers are NOT equivalent and are counted separately
    (``not_measured`` — a deterministic in-process substep that launched no leaf, nothing
    to measure; ``unavailable`` — a usage channel that failed, i.e. a defect).

    This is the whole source. The conductor is a Python process with no session of its
    own, so there is no "parent" usage to add to the leaves', and a row that carries no
    ``usage`` is reported as unaccounted rather than reconstructed from a transcript (the
    ``~/.claude`` reconstruction that used to be opt-in read a layout no conductor-spawned
    leaf produces; issue #179 deleted it). Best-effort — reports ``available=False``
    (never raises) when no row yields data.

    ``repo_root`` is not read: nothing this collector reports lives outside the rows it is
    handed.
    """
    meta = meta or {}
    # The orchestration agent's own arid appears in agent_runs.jsonl (the conductor records
    # itself under `agent_role: orchestration`) but is not a leaf — exclude it so it isn't
    # reported as an unaccounted row.
    parent_arid = str(meta.get("orchestration_agent_run_id") or "").strip()
    arids: list[str] = []
    persisted: dict[str, dict[str, Any]] = {}
    markers: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for run in list(agent_runs) + list(invalid_runs or []):
        arid = run.get("agent_run_id")
        if not (isinstance(arid, str) and arid and arid != parent_arid and arid not in seen):
            continue
        seen.add(arid)
        arids.append(arid)
        # Durable per-child usage persisted into agent_runs.jsonl by finalize_child
        # survives ~/.claude cleanup. A `status` marker (no numeric total) is not data —
        # it is kept aside so the render can say WHICH kind of non-measurement it was.
        u = run.get("usage")
        if not isinstance(u, dict):
            continue
        # A row written before `total_tokens` was derived at finalize time carries only the
        # raw classes — the HTTP leaf's `input_tokens`/`output_tokens` pair. Deriving it here
        # too is what makes those runs readable at all; the rule is the same one
        # `normalize_leaf_usage` applies, so the two cannot disagree. A marker has no token
        # keys and normalizes to None, so it can never be mistaken for data.
        numeric = (dict(u) if isinstance(u.get("total_tokens"), int)
                   else normalize_leaf_usage(
                       u, source=str(u.get("usage_source") or LEAF_USAGE_SOURCE_UNRECORDED)))
        if numeric is not None:
            numeric.setdefault("source", "agent_runs.jsonl")
            persisted[arid] = numeric
        elif isinstance(u.get("status"), str):
            markers[arid] = dict(u)

    per_child: dict[str, Any] = dict(persisted)

    sum_keys = (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "total_tokens",
        "assistant_turns",
        # SUBSETS of output/input respectively — summed so the render can report the share
        # they take, never added into `total_tokens` (see `normalize_leaf_usage`).
        "reasoning_tokens",
        "cached_tokens",
    )
    children_total = {k: 0 for k in sum_keys}
    for u in per_child.values():
        for k in sum_keys:
            children_total[k] += int(u.get(k, 0) or 0)
    children_total["peak_context_tokens"] = max(
        (int(u.get("peak_context_tokens", 0) or 0) for u in per_child.values()),
        default=0,
    )
    cost = sum(float(u.get("cost_usd", 0) or 0) for u in per_child.values())
    if cost > 0:
        children_total["cost_usd"] = round(cost, 6)
    children: dict[str, Any] = {
        "available": bool(per_child) or bool(markers),
        "per_child": per_child,
        "children_total": children_total,
        "matched_count": len(per_child),
        # A row that SAID why it has no numbers is accounted for, not missing. Only a row
        # with neither numbers nor a marker is genuinely unaccounted.
        "unmatched_arids": sorted(set(arids) - set(per_child) - set(markers)),
        "not_measured": sorted(a for a, m in markers.items()
                               if m.get("status") == "not_measured"),
        "usage_unavailable": sorted(a for a, m in markers.items()
                                    if m.get("status") == "unavailable"),
        "markers": markers,
    }
    if not per_child and not markers:
        # Only when NOTHING was located. A markers-only run did locate something — every leaf
        # said why it has no numbers — and a `--format json` consumer reading this field
        # would otherwise be told the opposite of what the rows say.
        children["reason"] = "no leaf usage located"

    # When no row carried anything, report unavailable rather than a misleading 0-token
    # breakdown. `markers` counts toward availability: a run whose every leaf recorded WHY
    # it has no numbers (all-deterministic, or every leaf dead) is not an absent
    # measurement — it is a measurement that says "none, because …", and reporting it as
    # "no usage located" would send an operator looking for data that was deliberately
    # not there.
    return {
        "available": bool(per_child) or bool(markers),
        "children": children,
        "children_total_tokens": int(
            (children.get("children_total") or {}).get("total_tokens", 0) or 0),
    }


# The `generate-executor` vocabulary as HISTORICALLY RECORDED on `invocation.generate_executor`.
# Since M-F, run_workflow no longer validates this value on cold init (legacy execution was removed
# and the executor is not selectable — cold runs always record `pure`), but past orchestrations on
# disk still carry `legacy`, so the audit keeps both to stay a faithful reader of historical
# records. Duplicated as a literal rather than imported so the read-only audit does not pull in the
# workflow launcher; the render only uses it to flag an out-of-vocabulary value, never to reject one.
_KNOWN_GENERATE_EXECUTORS: tuple[str, ...] = ("legacy", "pure")


# The `llm_leaf_map` keys whose provider this section attributes: every LLM leaf, all of which
# are pure since Z4 (issue #171). Read from `llm_config` rather than restated, so adding one
# cannot silently leave it out.
_PURE_LEAF_MAP_KEYS: frozenset[str] = frozenset(
    f"{phase}.{substep}" for phase, substep in _LLM_LEAF_SUBSTEPS)


def _clean_str(value: Any) -> str | None:
    """Return a stripped non-empty string, else None.

    A missing / null / wrong-typed provenance field (`generate_executor`,
    `agent_version`) is reported as absent rather than rendered as a spurious
    recorded value. The value is stripped, not just validated: these render inline
    into markdown, so surrounding whitespace from a hand-edited or foreign
    artifact would otherwise break the line. (The live writers already strip —
    `_probe_claude_backend` stores `claude --version` as `stdout.strip()` — so
    this is defence in depth, not a live defect.)"""
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _pure_source_dirs_of(
    repo_root: Path, orchestration_id: str
) -> tuple[list[str], list[str]]:
    """Every repo-relative source directory under this orchestration's node
    pipeline(s), in sorted order.

    Discovery reads this orchestration's OWN pipeline reservations
    (`reservations/<node_key_safe>/generate.json#reserved_ir_id`), which
    `prepare_node` writes before Compile runs and which `resume_node_refs` already
    treats as the authority for the pipeline id.

    Globbing `<pipeline_ref>/source/*` (rather than reading a pass-only ledger) is what
    keeps a terminally-failed generate and
    every cold-restart-rotated source dir measured — otherwise the pure-arm totals
    silently undercount. The node's `pipeline_id` is allocated once per orchestration
    node and reused across restarts, so the glob is exactly this orchestration's
    generate attempts. A legacy source dir under the same pipeline carries no
    bundle_meta/verdict_meta and is dropped by the caller's `found` filter.

    Returns `(source_dirs, pipeline_refs)`. The accepted `pipeline_refs` are returned
    alongside so the caller can tell an EMPTY result apart: no pipeline reservation
    at all (the node was never prepared) versus a reserved pipeline whose `source/`
    does not exist yet (`Generate` has not run — the normal state of a run stopped at
    Compile, e.g. `run_workflow.py <spec> Compile`, and of a `--with-deps` dependency
    node when the TARGET stops at Compile: `dep_until_phase` follows the target, so a
    `generate`/`validate` target drives its deps all the way to Validate and those do
    produce source dirs). Those two states must not share a diagnosis. A
    `reserved_ir_id` that is not a single clean path segment is rejected (it would
    escape the pipeline root).
    """
    dirs: list[str] = []
    pipeline_refs: list[str] = []
    res_root = _orch_root(repo_root, orchestration_id) / "reservations"
    if not res_root.is_dir():
        return dirs, pipeline_refs
    for node_dir in sorted(res_root.iterdir()):
        if not node_dir.is_dir():
            continue
        reserved = (_load_json_if_dict(node_dir / "generate.json") or {}).get("reserved_ir_id")
        if not (isinstance(reserved, str) and reserved):
            continue
        # The reserved id is JSON-sourced: require a single clean segment so it can
        # never traverse out of `workspace/pipelines/<node_key_safe>/`.
        if reserved in {".", ".."} or PurePosixPath(reserved).parts != (reserved,):
            continue
        pref = f"workspace/pipelines/{node_dir.name}/{reserved}"
        if pref not in pipeline_refs:
            pipeline_refs.append(pref)
    for pref in pipeline_refs:
        source_root = repo_root / pref / "source"
        if not source_root.is_dir():
            continue
        for child in sorted(source_root.iterdir()):
            if not child.is_dir():
                continue
            rel = f"{pref}/source/{child.name}"
            if rel not in dirs:
                dirs.append(rel)
    return dirs, pipeline_refs


def _pure_ir_dirs_of(
    repo_root: Path, orchestration_id: str
) -> list[str]:
    """Every repo-relative IR directory this orchestration launched a PURE `compile` leaf into,
    in sorted order.

    Discovery reads this orchestration's own persisted launch requests
    (`launches/<arid>.request.json`) and keeps the `ir_ref` of every row whose `step` is
    `compile` and whose `leaf_mode` is `pure`. That is deliberately NOT the reservation the
    source-dir discovery above uses: `reservations/<node>/compile.json#reserved_ir_id` is
    OVERWRITTEN by `_ensure_fresh_producer_id` on every rotation, so it names only the LIVE ir
    directory and would drop every repaired or restarted attempt — the same undercount
    `_pure_source_dirs_of` warns about, in the one place the A/B numbers are the point.

    A launch request is written before the leaf runs and is never rewritten, so one row exists
    per attempt and the set of `ir_ref`s is exactly the directories this orchestration wrote an
    IR into. `ir_ref` is JSON-sourced, so it is required to be a repo-relative `workspace/ir/...`
    path with no traversal segment.
    """
    dirs: list[str] = []
    launches = _orch_root(repo_root, orchestration_id) / "launches"
    if not launches.is_dir():
        return dirs
    for path in sorted(launches.glob("*.request.json")):
        row = _load_json_if_dict(path) or {}
        if _clean_str(row.get("step")) != "compile":
            continue
        if _clean_str(row.get("leaf_mode")) != "pure":
            continue
        ref = _clean_str(row.get("ir_ref"))
        if not ref:
            continue
        parts = PurePosixPath(ref).parts
        if not parts or parts[0] != "workspace" or any(p in {".", ".."} for p in parts):
            continue
        if ref not in dirs:
            dirs.append(ref)
    return sorted(dirs)


def _pure_run_node_dirs_of(repo_root: Path, orchestration_id: str) -> list[str]:
    """Every repo-relative run-node directory this orchestration launched a PURE `validate`
    leaf into, in sorted order (Z3, issue #169).

    Discovered from the persisted launch requests for the same reason `_pure_ir_dirs_of` is:
    one row per attempt, never rewritten. A `validate` request carries `pipeline_ref` and
    `run_id` rather than the run-node path, so the directory is composed the way `NodeRefs`
    composes it: `<pipeline_ref>/runs/<run_id>/<node_key_safe>`. The safe node key is READ OUT
    of `pipeline_ref` (`workspace/pipelines/<safe>/<pipeline_id>`, the same `NodeRefs`
    property) rather than recomputed from `node_key` — this tree already carries two spellings
    of that transform, and a third living in an audit tool would be the one nothing checks.
    """
    dirs: list[str] = []
    launches = _orch_root(repo_root, orchestration_id) / "launches"
    if not launches.is_dir():
        return dirs
    for path in sorted(launches.glob("*.request.json")):
        row = _load_json_if_dict(path) or {}
        if _clean_str(row.get("step")) != "validate":
            continue
        if _clean_str(row.get("leaf_mode")) != "pure":
            continue
        pipeline_ref = _clean_str(row.get("pipeline_ref"))
        run_id = _clean_str(row.get("run_id"))
        if not (pipeline_ref and run_id):
            continue
        parts = PurePosixPath(pipeline_ref).parts
        if len(parts) != 4 or parts[:2] != ("workspace", "pipelines"):
            continue
        if any(p in {".", ".."} for p in parts):
            continue
        safe = parts[2]
        if "/" in run_id or run_id in {".", ".."}:
            continue
        ref = f"{pipeline_ref}/runs/{run_id}/{safe}"
        if ref not in dirs:
            dirs.append(ref)
    return sorted(dirs)


def _pure_leaf_keys_that_ran(repo_root: Path, orchestration_id: str) -> frozenset[str]:
    """The `llm_leaf_map` keys of the pure leaves this orchestration ACTUALLY LAUNCHED, read
    from its own persisted launch requests.

    Attribution has to follow what ran, not what was configured. A run stopped at `Compile`
    launches no `generate` leaf at all, so folding the whole configured map into the attributed
    surface labels a compile-only measurement with a provider that never executed and — when
    that provider differs from the probed one — suppresses the CLI version of the provider that
    DID run. Both are false provenance in the one instrument a billed A/B is read from.

    Returns the EMPTY set when no launches directory exists or no pure launch is recorded; the
    caller falls back to the configured map there, because an orchestration with no launch
    records is one this function can say nothing about, and reporting the configured set is the
    older behaviour rather than a new claim.
    """
    keys: set[str] = set()
    launches = _orch_root(repo_root, orchestration_id) / "launches"
    if not launches.is_dir():
        return frozenset()
    for path in sorted(launches.glob("*.request.json")):
        row = _load_json_if_dict(path) or {}
        if _clean_str(row.get("leaf_mode")) != "pure":
            continue
        step = _clean_str(row.get("step"))
        substep = _clean_str(row.get("substep"))
        if step and substep:
            keys.add(f"{step}.{substep}")
    return frozenset(keys & _PURE_LEAF_MAP_KEYS)


def collect_pure_leaf_ab_summary(
    repo_root: Path,
    orchestration_id: str,
    meta: dict[str, Any] | None,
) -> dict[str, Any]:
    """A/B-measurement rollup for the pure leaves — the `generate` pair (Z2, milestone M-E)
    and, since issue #168, the `compile` pair, discovered and reported separately.

    Surfaces the executor selection (`orchestration_meta.json#invocation.
    generate_executor`) and the probed backend's CLI version
    (`preflight.json#agent_version` — already persisted, so no new file is written;
    it is `claude --version` on a claude run and `codex --version` on a codex run,
    so `backend` is carried with it) alongside per-node pure-leaf metrics read from
    `bundle_meta.json` / `verdict_meta.json` (generate) and `compile_generate_meta.json` /
    `compile_verify_meta.json` (compile). `available` is true when a pure node of EITHER phase was
    located,
    so a legacy (agentic) run reports `available=False` with the executor still
    surfaced; `reason` then says why, distinguishing "this run wrote no pure meta"
    from "Generate has not produced a source dir yet" and from "the node was never
    prepared", which would otherwise all render as a silent, legacy-looking zero.

    I/O: `meta` is passed in because `audit()` already loads it for its other
    sections; `preflight.json` and the pipeline reservations are read here because
    this is their only consumer. Best-effort — the caller wraps it so a diagnostics
    failure never breaks the audit.
    """
    meta = meta or {}
    invocation = meta.get("invocation")
    invocation = invocation if isinstance(invocation, dict) else {}
    generate_executor = _clean_str(invocation.get("generate_executor"))

    root = _orch_root(repo_root, orchestration_id)
    preflight = _load_json_if_dict(root / "preflight.json") or {}
    # `agent_version` is backend-agnostic: `probe_execution_platform` stores whatever
    # the selected backend's prober returned — `claude --version` on a claude run,
    # `codex --version` on a codex run. Carry the recorded `backend` so the renderer
    # can label it truthfully; naming it "claude" unconditionally would report false
    # provenance for every codex orchestration (which this section still renders,
    # whose leaves are pure too — `codex_cli` holds the `pure` capability).
    backend = _clean_str(preflight.get("backend"))
    agent_cli_version = _clean_str(preflight.get("agent_version"))
    # Since issue #28 the leaf LLM is per-`(phase, substep)`, so `preflight.json#backend` and
    # `#agent_version` describe `defaults` ONLY — and this section exists to attribute PURE-LEAF
    # A/B metrics, whose leaves are exactly the ones an operator is most likely to have moved
    # elsewhere. Carry the recorded per-leaf map so the renderer reports what the pure leaves
    # actually ran on rather than what the orchestration's default was; when it names anything
    # other than the top-level backend, the version line is the WRONG CLI's and is suppressed.
    leaf_map = invocation.get("llm_leaf_map")
    leaf_map = leaf_map if isinstance(leaf_map, dict) else {}
    # (backend, command) — not the backend token alone. Two leaves can share a token and run
    # different EXECUTABLES (`defaults` on `claude`, a substep on a wrapper), and
    # `preflight.json#agent_version` describes only the command it probed. A version attributed
    # across that difference names an executable that did not produce the metrics.
    default_command = _clean_str(preflight.get("probe_command")) or ""
    def _surface(token: str, command: str) -> tuple[str, str]:
        """`(backend, command)` with the bare-binary spellings folded together.

        An empty `command`, and a `command` that IS the backend token, both mean "launch the
        bare binary". Normalizing only one side reported a difference between two spellings of
        the same executable, and suppressed a version that was perfectly valid."""
        command = command.strip()
        return (token, "" if command == token else command)

    # WHAT RAN, not what was configured (see `_pure_leaf_keys_that_ran`). The fallback to the
    # configured set is deliberate and narrow: it applies only when this orchestration recorded
    # no pure launch at all, where the older behaviour is the honest one.
    attributed_keys = _pure_leaf_keys_that_ran(repo_root, orchestration_id) or _PURE_LEAF_MAP_KEYS
    pure_leaf_surfaces = sorted({
        _surface(_clean_str(row.get("backend")) or "", _clean_str(row.get("command")) or "")
        for key, row in leaf_map.items()
        if key in attributed_keys and isinstance(row, dict)
        and _clean_str(row.get("backend"))
    })
    pure_leaf_providers = sorted({surface[0] for surface in pure_leaf_surfaces})
    default_surface = _surface(backend or "", default_command)
    pure_leaf_provider_differs = (bool(pure_leaf_surfaces)
                                  and pure_leaf_surfaces != [default_surface])
    if pure_leaf_provider_differs:
        backend = "/".join(pure_leaf_providers)
        agent_cli_version = ""

    source_dirs, pipeline_refs = _pure_source_dirs_of(repo_root, orchestration_id)
    nodes: list[dict[str, Any]] = []
    for source_dir in source_dirs:
        summary = summarize_pure_leaf_metas(repo_root / source_dir, "generate")
        if summary.get("found"):
            # Label repo-relative here (the callee sets no `source_dir`). Build a new
            # dict rather than mutating the returned one, so the ownership is a
            # visible fact of this expression, not an implicit callee obligation.
            nodes.append({**summary, "source_dir": source_dir})
    # The Z1 compile pair (issue #168), discovered from the launch requests rather than from a
    # reservation, and labelled by its own directory key so the two phases never share one.
    compile_nodes: list[dict[str, Any]] = []
    ir_dirs = _pure_ir_dirs_of(repo_root, orchestration_id)
    for ir_dir in ir_dirs:
        summary = summarize_pure_leaf_metas(repo_root / ir_dir, "compile")
        if summary.get("found"):
            compile_nodes.append({**summary, "ir_ref": ir_dir})
    # The Z3 judge (issue #169), discovered the same way and labelled by its run-node dir. One
    # substep, so its row reads `judge` where the other two read `generate` / `verify`.
    validate_nodes: list[dict[str, Any]] = []
    for run_node_dir in _pure_run_node_dirs_of(repo_root, orchestration_id):
        summary = summarize_pure_leaf_metas(repo_root / run_node_dir, "validate")
        if summary.get("found"):
            validate_nodes.append({**summary, "run_node_dir": run_node_dir})

    result: dict[str, Any] = {
        "available": bool(nodes or compile_nodes or validate_nodes),
        "generate_executor": generate_executor,
        "backend": backend,
        "agent_cli_version": agent_cli_version,
        # True when the pure leaves ran on a provider the top-level `backend` / `agent_version`
        # do not describe; the renderer then names the provider instead of borrowing the
        # default backend's CLI version.
        "pure_leaf_provider_differs": pure_leaf_provider_differs,
        "pure_nodes": nodes,
        "pure_compile_nodes": compile_nodes,
        "pure_validate_nodes": validate_nodes,
    }
    if not nodes and not compile_nodes and not validate_nodes:
        if not pipeline_refs:
            # No pipeline reservation at all: `prepare_node` never ran for any node of
            # this orchestration (or the reservations were removed). Name it — this is
            # the only case where discovery itself could not proceed, and it must not
            # be confused with the routine "Generate hasn't run yet" below.
            result["reason"] = (
                "no pipeline reservation under this orchestration "
                "— discovery found no node to measure"
            )
        elif not source_dirs:
            result["reason"] = (
                "no generate source directory under the pipeline yet "
                "(Generate has not produced one)"
            )
        else:
            result["reason"] = "no pure-leaf meta located"
    return result


def audit(repo_root: Path, orchestration_id: str) -> dict[str, Any]:
    root = _orch_root(repo_root, orchestration_id)
    # An absent root is NOT an orchestration with nothing wrong with it. Every collector
    # below reads missing files as empty and would report a clean negative over a
    # directory nobody ever looked in — the same false negative issue #130 was about,
    # reached by a mistyped id or by running the RUNBOOK's command from a cwd where the
    # default `--repo-root .` does not name this checkout.
    orchestration_found = root.is_dir()
    # NO HOOK EVENTS. `hooks/native_hook_events.jsonl` was the in-sandbox leaf hook's trace,
    # and every section derived from it — the per-policy block counts, the benign/substantive
    # split and its volume budget, the `fix_hint` presence report, the `allow_auto_approve`
    # count and the five events before `fail_closed` — described decisions a hook made about a
    # leaf's tool call. Z4 ([issue #171](https://github.com/seiya/atmofab/issues/171)) deleted
    # the hook with the leaf that held tools; PR-2 deletes this half of the audit, which had
    # been reporting zero over a file nothing writes. The `fail_closed` timestamp survives,
    # read from `phase_state_log.jsonl` as it always was.
    phase_log, phase_errs = _load_jsonl_with_errors(root / "phase_state_log.jsonl")
    agent_runs, runs_errs = _load_jsonl_with_errors(root / "agent_runs.jsonl")
    invalid_runs, inv_errs = _load_jsonl_with_errors(root / "agent_runs_invalid.jsonl")
    meta = _load_json_if_dict(root / "orchestration_meta.json") or {}

    parse_errors = phase_errs + runs_errs + inv_errs
    fail_closed_at = _latest_fail_closed_at(phase_log)
    # Each best-effort section below still refuses to break the audit, but a swallowed
    # failure is RECORDED in `diagnostic_failures` and surfaced by the renderer: a
    # section that failed must not print its clean-negative sentence, which reads as a
    # measurement that was made (issue #130).
    diagnostic_failures: list[dict[str, str]] = []

    def _record_failure(section: str, exc: BaseException) -> None:
        diagnostic_failures.append(
            {"section": section, "error_type": type(exc).__name__, "error": str(exc)}
        )

    # Dangling launch (open active_child window with no child return / terminal
    # run): reproduces the post-mortem of an interrupted/hung child launch and
    # correlates the (ephemeral) ~/.claude transcript. None when the window is
    # closed — and also None when detection FAILED, which is why the failure is
    # recorded rather than degraded silently to "no window".
    try:
        launch_incident = build_launch_incident(repo_root, orchestration_id)
    except Exception as exc:  # noqa: BLE001 - diagnostics must never break the audit
        launch_incident = None
        _record_failure("launch_incident", exc)
    # Per-leaf token cost, from the durable `usage` rows in agent_runs.jsonl and nothing
    # else (see `collect_token_cost_summary`). Best-effort — must never break the audit.
    try:
        token_cost_summary = collect_token_cost_summary(
            repo_root, meta, agent_runs, invalid_runs)
    except Exception as exc:  # noqa: BLE001 - diagnostics must never break the audit
        token_cost_summary = {
            "available": False,
            "reason": f"token-cost collection failed: {type(exc).__name__}: {exc}",
        }
        _record_failure("token_cost_summary", exc)
    # Pure-leaf A/B rollup (Z2 M-E): executor selection + claude --version +
    # per-node bundle_meta/verdict_meta metrics. Reads only in-repo artifacts
    # (no ~/.claude). Best-effort — must never break the audit.
    try:
        pure_leaf_ab_summary = collect_pure_leaf_ab_summary(repo_root, orchestration_id, meta)
    except Exception as exc:  # noqa: BLE001 - diagnostics must never break the audit
        pure_leaf_ab_summary = {
            "available": False,
            "reason": f"pure-leaf A/B collection failed: {type(exc).__name__}: {exc}",
        }
        _record_failure("pure_leaf_ab_summary", exc)
    # Sandbox enforcement violations and the host's failure diagnosis. Both read files
    # nothing else in this audit reads, and an unreadable one is recorded as a failure of
    # the section rather than rendered as its clean negative.
    try:
        sandbox_violations: dict[str, Any] | None = collect_sandbox_violations(root)
    except Exception as exc:  # noqa: BLE001 - diagnostics must never break the audit
        sandbox_violations = None
        _record_failure("sandbox_violations", exc)
    try:
        failure_analysis: dict[str, Any] | None = collect_failure_analysis(root)
    except Exception as exc:  # noqa: BLE001 - diagnostics must never break the audit
        failure_analysis = None
        _record_failure("failure_analysis", exc)
    # Persisted incident snapshots captured at run time. These survive after
    # `--resume` clears the active-child markers (live detection then returns None)
    # and after ~/.claude cleanup removes the transcript, so they are the durable
    # diagnosis source for the documented later-analysis path. Surfaced even when
    # the live window is closed.
    launch_incident_snapshots: list[dict[str, Any]] = []
    for snap_path in sorted(root.glob("launch_incident.runtime.*.json")):
        doc = _load_json_if_dict(snap_path)
        if doc is None:
            continue
        launch_incident_snapshots.append(
            {"ref": str(snap_path.relative_to(repo_root)), "incident": doc}
        )

    return {
        "orchestration_id": orchestration_id,
        "orchestration_status": meta.get("status"),
        "fail_closed_at": fail_closed_at,
        "phase_state_failures": collect_phase_state_failures(phase_log),
        "failure_analysis": failure_analysis,
        "sandbox_violations": sandbox_violations,
        "launch_incident": launch_incident,
        "launch_incident_snapshots": launch_incident_snapshots,
        "agent_run_summary": collect_agent_run_summary(agent_runs, invalid_runs),
        "token_cost_summary": token_cost_summary,
        "pure_leaf_ab_summary": pure_leaf_ab_summary,
        "invalid_run_count": len(invalid_runs),
        "invalid_run_ids": [r.get("agent_run_id") for r in invalid_runs if r.get("agent_run_id")],
        "data_integrity_warning": len(parse_errors) > 0,
        "parse_error_count": len(parse_errors),
        "parse_errors": parse_errors,
        "diagnostic_failures": diagnostic_failures,
        "orchestration_found": orchestration_found,
        "orchestration_root": str(root),
    }


def _render_api_error_line(api_error: Any, lines: list[str]) -> None:
    """Render a transient-API-error line (e.g. 529 Overloaded) when present, so the
    reader sees the dangling launch was a transport blip rather than a hang."""
    if not isinstance(api_error, dict) or api_error.get("status") is None:
        return
    retry_hint = " (retryable — safe to `--resume`)" if api_error.get("retryable") else ""
    msg = str(api_error.get("message") or "").strip()
    lines.append(f"- transient API error: `{api_error.get('status')}` {msg}{retry_hint}")


def _render_incident_body(incident: dict[str, Any], lines: list[str]) -> None:
    """Render the decisive fields of one launch-incident dict (live or persisted)."""
    child = incident.get("dangling_child", {})
    lines.append("| field | value |")
    lines.append("|---|---|")
    lines.append(f"| child agent_run_id | `{child.get('agent_run_id')}` |")
    lines.append(f"| node_key | `{child.get('node_key_safe')}` |")
    lines.append(f"| step / substep | `{child.get('step')}` / `{child.get('substep')}` |")
    lines.append(f"| launch_recorded_at | `{child.get('launch_recorded_at')}` |")
    elapsed = child.get("elapsed_seconds")
    lines.append(f"| elapsed since launch | {f'{elapsed:.0f}s' if isinstance(elapsed, (int, float)) else 'n/a'} |")
    lines.append("")

    transcripts = incident.get("transcripts", {})
    ct = transcripts.get("child_transcript", {})
    if ct.get("found"):
        dead_air = ct.get("dead_air_seconds")
        lines.append("Child subagent transcript (decisive evidence):")
        lines.append("")
        # The `matched via` clause is CANONICAL here for a closed vocabulary with two
        # OWNERS and three values.
        # A LIVE incident is always `session_id` and additionally carries the projects
        # root the hit came from, which is what the `under` suffix shows: an agentic
        # leaf's private home, or the operator's `~/.claude` — which a PURE leaf uses on
        # a current run (it is prepared no private home) as well as any pre-#63 run. A PERSISTED `launch_incident.runtime.*.json`
        # snapshot written by the host-session-era conductor carries `tool_use_id` or
        # `arid_in_body` and no root; all 5 snapshots on this machine hold the former
        # (measured 2026-09-02), which is why the clause stays instead of being deleted.
        # No fallback for a missing key: `None` here is the visible signal that the live
        # producer (`orchestration_diagnostics.build_launch_incident`) has regressed.
        matched_root = ct.get("matched_projects_root")
        under = f" under `{matched_root}`" if matched_root else ""
        lines.append(
            f"- transcript: `{ct.get('path')}` (matched via `{ct.get('match_method')}`{under})")
        lines.append(f"- last activity: `{ct.get('last_activity_ts')}` (event `{ct.get('last_event_type')}`)")
        last_tool = ct.get("last_tool_use") or {}
        if last_tool:
            lines.append(f"- last tool_use: `{last_tool.get('name')}` — {last_tool.get('input_preview')}")
        lines.append(
            f"- dead-air before abort: "
            f"{f'{dead_air:.0f}s' if isinstance(dead_air, (int, float)) else 'n/a'}"
        )
        if ct.get("interrupted"):
            lines.append(
                f"- abort marker: `{ct.get('interrupt_text')}` at `{ct.get('interrupt_ts')}`"
            )
        # Fall back to parsing raw_tail for legacy snapshots captured before the
        # structured api_error field existed.
        _render_api_error_line(
            ct.get("api_error") or api_error_from_records(ct.get("raw_tail")), lines
        )
    else:
        # Live re-derivation: ~/.claude transcript ephemeral. A persisted snapshot
        # (rendered from "Captured incident snapshots" below) keeps the evidence even
        # then, since the decisive tail was copied in-repo at incident time.
        abort = incident.get("abort_marker")
        if isinstance(abort, dict) and abort:
            dead_air = abort.get("dead_air_seconds")
            lines.append("Child subagent transcript (decisive evidence, from snapshot):")
            lines.append("")
            lines.append(f"- last activity: `{abort.get('last_activity_ts')}`")
            lines.append(
                f"- dead-air before abort: "
                f"{f'{dead_air:.0f}s' if isinstance(dead_air, (int, float)) else 'n/a'}"
            )
            if abort.get("interrupted"):
                lines.append(
                    f"- abort marker: `{abort.get('interrupt_text')}` at `{abort.get('interrupt_ts')}`"
                )
            # Legacy snapshot fallback: abort_marker predates api_error; recover it
            # from the child transcript's raw_tail if that field is missing.
            _render_api_error_line(
                abort.get("api_error") or api_error_from_records(ct.get("raw_tail")), lines
            )
        else:
            lines.append(
                f"Child subagent transcript not available: {ct.get('reason', 'unknown')} "
                "(leaf transcripts are machine-local: the orchestration's private "
                "home, else ~/.claude)."
            )
    lines.append("")


def _render_launch_incident(
    incident: dict[str, Any] | None,
    snapshots: list[dict[str, Any]] | None,
    lines: list[str],
    failure: dict[str, str] | None = None,
    unmeasured_reason: str | None = None,
) -> None:
    """Render the dangling-launch section: live window and/or persisted snapshots.

    The clean negative ("No dangling active_child window detected") is printed ONLY when
    detection actually ran over a real orchestration — `incident is None` on its own
    cannot tell that from the two ways of not having looked (issue #130):
    ``failure`` is the recorded `diagnostic_failures` entry when detection RAISED, and
    ``unmeasured_reason`` is set when there was nothing to detect over.
    """
    snapshots = snapshots or []
    lines.append("## Dangling launch (active_child window)")
    lines.append("")

    note = unmeasured_reason
    if failure and not note:
        note = (
            f"Dangling-launch detection FAILED "
            f"(`{failure.get('error_type')}: {failure.get('error')}`)"
        )
    if note:
        lines.append(
            f"{note} — whether an active_child window is open is **UNKNOWN**; do not "
            "read this section as 'no window'."
        )
        lines.append("")
        if not snapshots:
            return
        lines.append(
            "Captured incident snapshot(s) below are independent of the live detection."
        )
        lines.append("")
    elif incident:
        lines.append(
            "An open active_child window was found with no child return / terminal "
            "agent_runs row — the child launch never completed."
        )
        lines.append("")
        _render_incident_body(incident, lines)
    elif not snapshots:
        lines.append(
            "No dangling active_child window detected and no captured incident snapshots."
        )
        lines.append("")
        return
    else:
        lines.append(
            "No active_child window is currently open (e.g. cleared by `--resume`), but "
            "incident snapshot(s) captured at run time are preserved in-repo below."
        )
        lines.append("")

    if snapshots:
        lines.append("### Captured incident snapshots (`launch_incident.runtime.*.json`)")
        lines.append("")
        for snap in snapshots:
            ref = snap.get("ref")
            doc = snap.get("incident")
            lines.append(f"- `{ref}`")
            lines.append("")
            if isinstance(doc, dict):
                _render_incident_body(doc, lines)
            else:
                lines.append("  (unreadable snapshot)")
                lines.append("")


def _fmt_tok(n: Any) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return "n/a"


def _render_token_cost(summary: dict[str, Any] | None, lines: list[str]) -> None:
    """Render the per-leaf token cost — the leaf total, its reasoning / cache shares, the
    rows that said why they have no numbers, and the ranked per-leaf table."""
    lines.append("## Token cost (per leaf)")
    lines.append("")
    if not isinstance(summary, dict) or not summary.get("available"):
        reason = (summary or {}).get("reason") or (
            (summary or {}).get("children", {}) or {}
        ).get("reason", "unavailable")
        lines.append(
            f"Leaf token cost unavailable: {reason}. "
            "(Per-leaf usage is written into `agent_runs.jsonl` at finalize time; a run "
            "recorded before that carries none.)"
        )
        lines.append("")
        return

    children = summary.get("children", {}) or {}
    # "available" with zero numeric rows (every row a marker) is not a measurement —
    # showing "0" would read as "the leaves cost nothing".
    children_ok = bool(children.get("available")) and int(children.get("matched_count", 0) or 0) > 0
    child_t = summary.get("children_total_tokens", 0)
    if not children_ok:
        # No row yielded a number. The section is still rendered — the marker lines below
        # say what each row reported — but the total must not read `0 tokens`, which says
        # the run was free. This is reachable since a marker counts as "available": a run
        # whose every substep was deterministic, or whose every leaf died.
        lines.append("- **leaf total**: unavailable (no launch reported a measurement)")
    else:
        lines.append(f"- **leaf total**: {_fmt_tok(child_t)} tokens")
    totals = children.get("children_total") or {}
    reasoning = int(totals.get("reasoning_tokens", 0) or 0)
    out_tokens = int(totals.get("output_tokens", 0) or 0)
    if reasoning:
        # The term that made `output_tokens` alone misleading: on
        # `orch_20260807T002410Z_acf2b996` reasoning was 84-99.6% of the output tokens.
        share = f" ({reasoning / out_tokens:.0%} of output)" if out_tokens else ""
        lines.append(f"  - of which reasoning: {_fmt_tok(reasoning)}{share}")
    cached = int(totals.get("cached_tokens", 0) or 0)
    in_tokens = int(totals.get("input_tokens", 0) or 0)
    if cached:
        share = f" ({cached / in_tokens:.0%} of input)" if in_tokens else ""
        lines.append(f"  - of which prompt-cache hits: {_fmt_tok(cached)}{share}")
    if isinstance(totals.get("cost_usd"), (int, float)):
        lines.append(f"  - provider-reported cost: ${totals['cost_usd']:.4f}")
    # `not_measured` is not a gap. A deterministic in-process substep launched no leaf, so
    # there was never a number; reporting it as missing data would send an operator looking
    # for a defect that does not exist. `unavailable` IS a gap and is called one.
    not_measured = children.get("not_measured") or []
    if not_measured:
        lines.append(
            f"  - {len(not_measured)} run(s) not measured (no leaf launched — "
            "deterministic in-process substeps)"
        )
    usage_unavailable = children.get("usage_unavailable") or []
    if usage_unavailable:
        count = len(usage_unavailable)
        lines.append(
            f"  - ⚠ {count} {'leaf' if count == 1 else 'leaves'} reported no usage "
            "(see each row's `usage.reason`)"
        )
    unmatched = children.get("unmatched_arids") or []
    if unmatched:
        lines.append(
            f"  - ⚠ {len(unmatched)} leaf arid(s) carry no usage field at all "
            "(recorded before per-leaf usage was durable)"
        )
    lines.append("")
    per_child = children.get("per_child") or {}
    if per_child:
        ranked = sorted(
            per_child.items(),
            key=lambda kv: int(kv[1].get("total_tokens", 0) or 0),
            reverse=True,
        )
        lines.append("| leaf agent_run_id | total | reasoning | source |")
        lines.append("|---|---|---|---|")
        for arid, usage in ranked:
            reasoning_cell = (_fmt_tok(usage["reasoning_tokens"])
                              if isinstance(usage.get("reasoning_tokens"), int) else "n/a")
            lines.append(
                f"| `{arid}` | {_fmt_tok(usage.get('total_tokens'))} | {reasoning_cell} | "
                f"{usage.get('usage_source') or usage.get('source') or 'n/a'} |"
            )
        lines.append("")


def _render_pure_leaf_row(label: str, row: dict[str, Any], lines: list[str]) -> None:
    """Render one pure-leaf (`generate` / `verify`) metrics row. Within a located
    pure node an absent sub-row means only that leaf's meta was not written (not
    that the node is legacy)."""
    if not isinstance(row, dict) or not row.get("found"):
        lines.append(f"- `{label}`: no {label} meta recorded")
        return
    usage = row.get("usage_total") or {}
    # Collapse the per-attempt model list: a repair loop normally resolves the same
    # alias every turn, and joining it raw renders "model(s): m, m, m", which reads
    # as several distinct models (or as an attempt count). Distinct-in-order keeps
    # the genuine multi-model case visible.
    models = list(dict.fromkeys(row.get("models") or []))
    model_str = f", model(s): {', '.join(models)}" if models else ""
    cat = row.get("failure_category")
    cat_str = f", failure: `{cat}`" if cat else ""
    # The prompt contract version is the A/B comparability check — two arms run
    # under different contract versions are not measuring the same thing.
    contract = row.get("prompt_contract_version")
    contract_str = f", contract=`{contract}`" if contract else ""
    lines.append(
        f"- `{label}`: result=`{row.get('result')}`, attempts={row.get('attempts')} "
        f"(repair turns={row.get('repair_turns')}){cat_str}{contract_str}"
    )
    # `total` sums all four token classes; show cache_creation too so the four
    # displayed numbers reconcile with `total`.
    lines.append(
        f"  - tokens — in {_fmt_tok(usage.get('input_tokens'))}, "
        f"out {_fmt_tok(usage.get('output_tokens'))}, "
        f"cache_read {_fmt_tok(usage.get('cache_read_input_tokens'))}, "
        f"cache_creation {_fmt_tok(usage.get('cache_creation_input_tokens'))}, "
        f"total {_fmt_tok(usage.get('total_tokens'))}{model_str}"
    )


def _render_pure_leaf_ab(summary: dict[str, Any] | None, lines: list[str]) -> None:
    """Render the Z2 pure-leaf A/B rollup: executor + claude --version + per-node
    generate/verify attempt and token metrics (the P-arm provenance of a billed
    A/B comparison)."""
    lines.append("## Pure-leaf A/B metrics")
    lines.append("")
    summary = summary if isinstance(summary, dict) else {}
    recorded_executor = summary.get("generate_executor")
    executor = recorded_executor or "unknown"
    # `raw_version` before the placeholder: `or "unrecorded"` makes the value unconditionally
    # truthy, so testing the placeholder would take the version branch even when the collector
    # deliberately blanked it to say "the pure leaves did not run on the CLI this version
    # describes".
    version = _clean_str(summary.get("agent_cli_version")) or "unrecorded"
    backend = summary.get("backend")
    # An EXPLICIT flag from the collector, not "the version is blank": a uniform run whose
    # preflight recorded no version still reports `unrecorded` against its own backend.
    provider_differs = bool(summary.get("pure_leaf_provider_differs"))
    lines.append(f"- generate-executor: `{executor}`")
    # Report the recorded value verbatim (diagnostics say what IS recorded, not what
    # should be) but flag an out-of-vocabulary one: the branches below key on the
    # exact value, so an unrecognized executor must not be silently read as legacy.
    if recorded_executor and recorded_executor not in _KNOWN_GENERATE_EXECUTORS:
        lines.append(
            f"  - ⚠ unrecognized executor value (expected one of "
            f"{', '.join(f'`{e}`' for e in _KNOWN_GENERATE_EXECUTORS)})"
        )
    # Label the version by the backend that was actually probed. `agent_version` is
    # whichever CLI ran, so a fixed "claude --version" label would misreport every
    # codex orchestration's version as Claude's.
    if not provider_differs:
        version_label = f"{backend} --version" if backend else "backend CLI --version"
        lines.append(f"- {version_label}: `{version}` (from `preflight.json#agent_version`)")
    else:
        # The pure leaves ran somewhere the preflight's top-level version does not describe
        # (a per-substep provider). Naming the provider without a version is the honest report;
        # borrowing the default backend's version would be false provenance.
        lines.append(f"- pure-leaf provider: `{backend}` "
                     f"(from `orchestration_meta.json#invocation.llm_leaf_map`; no CLI version "
                     f"is recorded for it)")
    nodes = summary.get("pure_nodes") or []
    compile_nodes = summary.get("pure_compile_nodes") or []
    validate_nodes = summary.get("pure_validate_nodes") or []
    if not summary.get("available") or not (nodes or compile_nodes or validate_nodes):
        # Say which case this is. Under executor=pure, "legacy/agentic run" would
        # contradict the executor line rendered directly above; under an unknown or
        # unrecognized executor we cannot claim either arm.
        reason = summary.get("reason")
        if executor == "pure":
            hint = reason or "pure run with no `bundle_meta.json` / `verdict_meta.json` written"
        elif executor == "legacy":
            hint = "legacy/agentic run"
            hint += f", or {reason}" if reason else ", or the pure metas are absent"
        else:
            hint = reason or "executor not recorded or unrecognized; no pure metas on disk"
        lines.append(f"- no pure-leaf node located ({hint})")
        lines.append("")
        return
    lines.append("")
    for node in compile_nodes:
        # The row keys name the SUBSTEP, so a compile node's rows read `generate` / `verify`
        # exactly as a generate node's do; the heading names the phase and the directory.
        lines.append(f"### compile `{node.get('ir_ref')}`")
        _render_pure_leaf_row("generate", node.get("generate") or {}, lines)
        _render_pure_leaf_row("verify", node.get("verify") or {}, lines)
        lines.append("")
    for node in nodes:
        lines.append(f"### generate `{node.get('source_dir')}`")
        _render_pure_leaf_row("generate", node.get("generate") or {}, lines)
        _render_pure_leaf_row("verify", node.get("verify") or {}, lines)
        lines.append("")
    for node in validate_nodes:
        # One row, and it is named `judge`: the phase has a single pure leaf.
        lines.append(f"### validate `{node.get('run_node_dir')}`")
        _render_pure_leaf_row("judge", node.get("judge") or {}, lines)
        lines.append("")


def _render_phase_state_failures(result: dict[str, Any], lines: list[str]) -> None:
    """Render every `fail` / `fail_closed` transition, after the latest `fail_closed`
    instant. One line per entry, in file order."""
    lines.append("## Phase state failures")
    lines.append("")
    fail_closed_at = result.get("fail_closed_at")
    entries = result.get("phase_state_failures") or []
    if fail_closed_at:
        lines.append(f"fail_closed at: `{fail_closed_at}`")
        lines.append("")
    if entries:
        for e in entries:
            reason = ""
            if e.get("reason_code") or e.get("reason_detail"):
                reason = f" — `{e.get('reason_code')}`: {e.get('reason_detail')}"
            lines.append(f"- [{e.get('ts')}] {e.get('event')} → `{e.get('to')}`{reason}")
    else:
        # `fail_closed_at` is derived from the same rows, so it is None here too.
        lines.append("No fail / fail_closed transition recorded.")
    lines.append("")


def _render_failure_analysis_doc(doc: dict[str, Any], lines: list[str]) -> None:
    lines.append(
        f"- status `{doc.get('status')}`, orchestration status "
        f"`{doc.get('orchestration_status')}`, reason `{doc.get('reason_code')}`: "
        f"{doc.get('reason_detail')}"
    )
    run = doc.get("failed_agent_run")
    if isinstance(run, dict):
        lines.append(
            f"- failed agent run: `{run.get('agent_run_id')}` — `{run.get('node_key')}` "
            f"{run.get('step')}.{run.get('substep')} (status `{run.get('status')}`)"
        )
    else:
        lines.append("- failed agent run: none recorded")
    step_results = doc.get("failed_step_results") or []
    if step_results:
        lines.append(f"- failed step results: {len(step_results)}")
        for r in step_results:
            lines.append(f"  - `{r.get('path')}` (status `{r.get('status')}`)")
    lines.append(
        f"- recommended retry decisions: {doc.get('recommended_retry_decision_count', 0)}"
    )
    for ref in doc.get("launch_incident_refs") or []:
        lines.append(f"- launch incident: `{ref}`")


def _render_failure_analysis(result: dict[str, Any], lines: list[str], *,
                             failure: dict[str, Any] | None = None) -> None:
    """Render the host's failure diagnosis next to the orchestration's terminal status,
    so an absent file reads as normal for a passed run and as a finding for a failed
    one."""
    lines.append("## failure_analysis")
    lines.append("")
    status = result.get("orchestration_status")
    if failure is not None:
        lines.append(
            f"failure_analysis could not be read — `{failure.get('error_type')}: "
            f"{failure.get('error')}`. Its content is UNKNOWN, not absent "
            f"(`orchestration_meta.json#status` = `{status}`)."
        )
        lines.append("")
        return
    fa = result.get("failure_analysis") or {}
    if not fa.get("present"):
        lines.append(
            f"`failure_analysis.json` absent (`orchestration_meta.json#status` = `{status}`)."
        )
    else:
        lines.append(f"`failure_analysis.json` (`orchestration_meta.json#status` = `{status}`):")
        _render_failure_analysis_doc(fa.get("canonical") or {}, lines)
    for side in fa.get("sidecars") or []:
        lines.append("")
        lines.append(
            f"sidecar `{side.get('file')}` "
            f"(existing_file_status `{side.get('existing_file_status')}`):"
        )
        _render_failure_analysis_doc(side, lines)
    lines.append("")


def _render_sandbox_violations(summary: dict[str, Any] | None, lines: list[str], *,
                               failure: dict[str, Any] | None = None) -> None:
    """Render `violations/` with its three states kept apart: absent (nothing recorded),
    present-and-empty (pre-created by an older run), and populated."""
    lines.append("## Sandbox enforcement violations")
    lines.append("")
    if failure is not None:
        lines.append(
            f"`violations/` could not be read — `{failure.get('error_type')}: "
            f"{failure.get('error')}`. Its content is UNKNOWN, not empty."
        )
        lines.append("")
        return
    summary = summary or {}
    records = summary.get("records") or []
    sandbox = [r for r in records if r.get("kind") == SANDBOX_VIOLATION_KIND]
    other = [r for r in records if r.get("kind") != SANDBOX_VIOLATION_KIND]
    if not summary.get("directory_present"):
        lines.append("`violations/` absent — no sandbox enforcement violation was recorded.")
    elif not records:
        lines.append(
            "`violations/` directory present, no record (pre-created by a run before "
            "issue #171 PR-2)."
        )
    elif not sandbox:
        lines.append(
            "`violations/` directory present, no sandbox enforcement record (the records "
            "below are of another kind)."
        )
    else:
        lines.append(f"{len(sandbox)} sandbox enforcement record(s):")
        for reason, cnt in sorted((summary.get("by_reason") or {}).items()):
            lines.append(f"- `{reason}`: {cnt}")
        lines.append("")
        for r in sandbox:
            lines.append(
                f"- [{r.get('evaluated_at')}] `{r.get('reason')}` arid=`{r.get('agent_run_id')}` "
                f"(`{r.get('file')}`)"
            )
    if other:
        # Another kind's record: named by its kind so it is not read as enforcement.
        lines.append("")
        lines.append(
            f"{len(other)} record(s) of another kind (not a sandbox enforcement finding):"
        )
        for r in other:
            lines.append(
                f"- [{r.get('evaluated_at')}] kind `{r.get('kind')}` "
                f"arid=`{r.get('agent_run_id')}` (`{r.get('file')}`)"
            )
    lines.append("")


def _render_markdown(result: dict[str, Any]) -> str:
    lines: list[str] = []
    orch_id = result["orchestration_id"]
    lines.append(f"# Audit: {orch_id}")
    lines.append("")

    if not result.get("orchestration_found", True):
        lines.append("## ⚠ orchestration not found")
        lines.append("")
        lines.append(
            f"`{result.get('orchestration_root')}` is not a directory. Every count below "
            "is zero because nothing was read, NOT because nothing is wrong. Two causes: "
            "`--repo-root` does not name the checkout (it defaults to the CURRENT "
            "DIRECTORY, so this is what running the command from elsewhere looks like), "
            "or the orchestration id is wrong."
        )
        lines.append("")

    failures = result.get("diagnostic_failures") or []
    failures_by_section = {f.get("section"): f for f in failures if isinstance(f, dict)}

    _render_phase_state_failures(result, lines)
    _render_failure_analysis(result, lines,
                             failure=failures_by_section.get("failure_analysis"))
    # `orchestration_found` defaults to True so a caller holding an older result dict
    # renders exactly as before rather than growing a spurious banner.
    unmeasured = None
    if not result.get("orchestration_found", True):
        unmeasured = ("No orchestration was found at the path above, so NOTHING was "
                      "measured here")
    _render_launch_incident(
        result.get("launch_incident"), result.get("launch_incident_snapshots"), lines,
        failure=failures_by_section.get("launch_incident"),
        unmeasured_reason=unmeasured,
    )

    if failures:
        lines.append("## ⚠ diagnostic failures")
        lines.append("")
        lines.append(
            "A best-effort section raised and was swallowed. Its result is UNKNOWN, not "
            "negative — re-run after fixing the cause before concluding anything from it."
        )
        lines.append("")
        for f in failures:
            lines.append(
                f"- `{f.get('section')}` — `{f.get('error_type')}: {f.get('error')}`"
            )
        lines.append("")

    if result.get("data_integrity_warning"):
        lines.append("## ⚠ data integrity warning")
        lines.append("")
        lines.append(f"Parse errors: {result['parse_error_count']}")
        lines.append("")
        for err in result.get("parse_errors", [])[:10]:
            lines.append(f"- `{err['path']}:{err['line_number']}` — {err['message']}")
        lines.append("")

    _render_sandbox_violations(result.get("sandbox_violations"), lines,
                               failure=failures_by_section.get("sandbox_violations"))

    _render_token_cost(result.get("token_cost_summary"), lines)

    _render_pure_leaf_ab(result.get("pure_leaf_ab_summary"), lines)

    ar = result["agent_run_summary"]
    lines.append("## agent_runs summary")
    lines.append("")
    for status, cnt in ar["status_counts"].items():
        lines.append(f"- `{status}`: {cnt}")
    if ar["missing_finished_at"]:
        lines.append("")
        lines.append("Missing `finished_at` (incomplete records):")
        for run_id in ar["missing_finished_at"]:
            lines.append(f"- `{run_id}`")
    repeated = ar.get("repeated_substeps") or []
    if repeated:
        lines.append("")
        lines.append("Repeated substeps (more than one attempt):")
        for row in repeated:
            statuses = ", ".join(f"`{st}`" for st in row.get("statuses") or [])
            step = row.get("step")
            if row.get("substep"):
                step = f"{step}.{row['substep']}"
            lines.append(
                f"- `{row.get('node_key')}` {step}: {row.get('attempts')} attempts ({statuses})"
            )
    lines.append("")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only orchestration audit helper. Exits 2 when the audit cannot be "
            "trusted at face value: log corruption was detected "
            "(`data_integrity_warning`), a best-effort diagnostic section raised and "
            "its result is UNKNOWN rather than negative (`diagnostic_failures`), or "
            "there is no such orchestration under --repo-root (`orchestration_found`)."
        )
    )
    parser.add_argument("--orchestration-id", required=True, help="Orchestration ID to audit")
    parser.add_argument(
        "--format",
        choices=["json", "markdown"],
        default="markdown",
        help="Output format (default: markdown)",
    )
    parser.add_argument(
        "--repo-root",
        default=".",
        help="Repository root (default: current directory)",
    )
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    result = audit(repo_root, args.orchestration_id)

    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(_render_markdown(result))

    # Exit non-zero when log corruption is detected, when a diagnostic section failed,
    # or when there was no orchestration to read — each may have printed a false
    # negative, so CI / scripts can flag it.
    if (result.get("data_integrity_warning") or result.get("diagnostic_failures")
            or not result.get("orchestration_found", True)):
        sys.exit(2)


if __name__ == "__main__":
    main()
