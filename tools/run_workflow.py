#!/usr/bin/env python3
"""Bootstrap workflow orchestration startup for a target spec."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import uuid

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX; the cold-start claim degrades
    fcntl = None  # type: ignore[assignment]
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Direct-CLI import bootstrap. When this script is executed as
# `python3 tools/run_workflow.py ...` (the canonical entrypoint per
# AGENTS.md), `sys.path[0]` is `tools/`, not the repo root, so absolute
# package imports like `from tools.validate_pipeline_semantics import ...`
# fail with `ModuleNotFoundError` before any structured error handling
# can run. Mirror the pattern used by `tools/validate_pipeline_semantics.py`
# and `tools/orchestration_runtime.py`: detect the missing import and
# prepend the repo root to `sys.path`. The probe import is intentionally
# small (a stdlib-style module name we know lives next to this script)
# so the side effect is just sys.path adjustment.
try:
    from tools import validate_pipeline_semantics as _probe  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover - direct CLI execution
    _THIS_FILE = Path(__file__).resolve()
    _REPO_ROOT = _THIS_FILE.parent.parent
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    # Re-probe so the in-function imports later in main() succeed.
    from tools import validate_pipeline_semantics as _probe  # noqa: F401

from tools.llm_config import (
    LlmConfig,
    LlmConfigError,
    apply_defaults_overrides,
    config_sha256,
    llm_config_from_legacy,
    load_llm_config,
    shipped_config_path,
)


# Orchestration is conductor-only (the deterministic Python phase loop in
# tools/workflow_conductor.py). The conductor has leaf launchers for claude and
# codex; the former LLM-orchestrator driver and the cursor backend (which only ran
# under that driver) were removed.
SUPPORTED_LLMS = ("codex", "claude")
SUPPORTED_WORKFLOW_MODES = ("dev", "prod")
# Applied when --llm / --mode are omitted on a non-resume run, so plain
# `run_workflow.py <spec> <phase>` uses the claude backend by default.
DEFAULT_LLM = "claude"
DEFAULT_WORKFLOW_MODE = "dev"
DEFAULT_LLM_COMMANDS = {
    "codex": "codex",
    "claude": "claude",
}
# Default orchestration-agent model recorded on the orchestration agent_runs row
# for the Claude backend, as an UNPINNED alias (e.g. "opus") read from the
# operator's settings — never a pinned version, which would go stale as versions
# update. Operators on a different Claude model override it with --agent-model.
# Codex is intentionally excluded: its fresh and resume workflows require an
# explicit model slug, which the conductor pins in every `codex exec --model`
# launch and records as host-side provenance.
def _default_claude_agent_model() -> str:
    from tools.orchestration_runtime import resolve_claude_model_alias
    return resolve_claude_model_alias()

PHASE_ALIASES = {
    "compile": "Compile",
    "generate": "Generate",
    "build": "Build",
    "validate": "Validate",
}
PHASE_ORDER = ["Compile", "Generate", "Build", "Validate"]

# CLI tools the workflow runtime depends on (used internally by orchestration_runtime
# subcommands such as run-gate / guarded-apply-patch, and by git-based status probes
# in tools/run_workflow.py itself). Missing any one fails the run before init, so
# agents never hit a partial-failure state where (e.g.) jq is unavailable to runtime
# but already in the agent's environment.
REQUIRED_CLI_TOOLS = ("python3", "jq", "git")


def _check_required_cli_tools() -> list[str]:
    return [tool for tool in REQUIRED_CLI_TOOLS if shutil.which(tool) is None]


@dataclass(frozen=True)
class RuntimeResult:
    payload: dict[str, Any]
    raw_stdout: str


def _normalize_workflow_mode(token: str) -> str:
    normalized = token.strip().lower()
    if normalized not in SUPPORTED_WORKFLOW_MODES:
        choices = ", ".join(SUPPORTED_WORKFLOW_MODES)
        raise ValueError(f"unknown workflow mode: {token!r} (expected one of: {choices})")
    return normalized


def _normalize_phase(token: str) -> str:
    normalized = token.strip().lower()
    if normalized not in PHASE_ALIASES:
        choices = ", ".join(PHASE_ALIASES.keys())
        raise ValueError(f"unknown phase: {token!r} (expected one of: {choices})")
    return PHASE_ALIASES[normalized]


def _new_orchestration_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = uuid.uuid4().hex[:8]
    return f"orch_{ts}_{suffix}"


def _repo_relative(path: str | Path, repo_root: Path) -> str:
    """`path` relative to `repo_root` when it lives inside it, else its absolute form.

    Recorded in the invocation block, so a config shipped with the repository reads as
    `configs/llm/claude.yaml` on any machine while an operator's own file outside the tree keeps
    the only spelling that can find it again. Relative to the RUN's root, because that is the
    root the resume gate re-joins it against."""
    p = Path(path)
    try:
        return str(p.resolve().relative_to(Path(repo_root).resolve()))
    except ValueError:
        # ABSOLUTE, not the caller's spelling: the recorded path is re-joined to `repo_root` on
        # resume, so a relative spelling naming a file outside the root would resolve to a
        # different file (or to nothing).
        return str(p.resolve())


def _build_invocation_record(
    *,
    argv: list[str] | None,
    spec_ref: str,
    until_phase: str,
    llm: str,
    llm_command: str,
    workflow_mode: str,
    agent_model: str | None,
    with_deps: bool,
    llm_config: LlmConfig | None = None,
    llm_config_overrides: dict[str, str] | None = None,
    repo_root: Path = Path("."),
    wait_usage_reset: bool = False,
    closure_id: str | None = None,
    closure_target_spec_ref: str | None = None,
    closure_until_phase: str | None = None,
) -> dict[str, Any]:
    """Assemble the reproduction/provenance record persisted to
    `orchestration_meta.json#invocation`.

    Records BOTH the raw argv (as invoked) and the resolved/canonical params: spec
    paths are canonicalized by `_canonicalize_spec_ref`, so the raw argv alone is not
    enough to reproduce the run. The `closure_*` fields are present only for nodes of
    a `--with-deps` closure; closure-aware resume reads `closure_id` /
    `closure_target_spec_ref` / `closure_until_phase` from here to detect closure
    membership and re-derive the closure (`_index_closure_orchestrations`)."""
    raw_argv = list(argv) if argv is not None else list(sys.argv[1:])
    record: dict[str, Any] = {
        "argv": raw_argv,
        "command": shlex.join(["python3", "tools/run_workflow.py", *raw_argv]),
        "spec_ref": spec_ref,
        "until_phase": until_phase,
        "llm": llm,
        "llm_command": llm_command,
        "mode": workflow_mode,
        "with_deps": bool(with_deps),
        # --wait-usage-reset is recorded for provenance/observability only. It is a PER-INVOCATION
        # runtime preference, NOT auto-recovered on --resume (that decision is intentional — see the
        # flag help and RUNBOOK); a resume must re-pass the flag to keep the wait active. On resume
        # the field is REFRESHED to the effective re-passed value by enable_checkpoint_resume (this
        # cold-init value is the original run's), so the recorded field always matches the behavior
        # of the run that produced the current result rather than going stale.
        "wait_usage_reset": bool(wait_usage_reset),
        # Z2 executor provenance. Since M-F the generate-executor is always `pure` (legacy removed),
        # so this is a hardcoded provenance stamp rather than a per-run choice. It is still the value
        # read by the M-F resume fail-close gate in main(): a resume of an orchestration whose
        # recorded executor is not `pure` (legacy, or the field absent = a pre-adoption run) is
        # rejected with `generate_executor_legacy_removed`.
        "generate_executor": "pure",
    }
    if llm_config is not None:
        # The leaf-model authority, pinned three ways. The PATH says which file; the SHA256 of
        # its BYTES is what a resume re-checks, because a config edited between the launch and
        # the resume would silently change what the remaining substeps run on; and
        # `llm_leaf_map` records the resolved per-leaf provider/model so a mixed closure stays
        # legible to a cost or A/B audit without re-reading a file that may since have changed.
        record["llm_config_path"] = _repo_relative(llm_config.path, repo_root)
        record["llm_config_sha256"] = llm_config.sha256
        record["llm_leaf_map"] = llm_config.provenance_map()
        # The deprecated flags do not live in the file, so their literals are recorded
        # separately and compared on resume alongside the hash.
        record["llm_config_overrides"] = dict(llm_config_overrides or {})
    if agent_model:
        record["agent_model"] = agent_model
    if closure_id:
        record["closure_id"] = closure_id
        record["closure_target_spec_ref"] = closure_target_spec_ref or ""
        record["closure_until_phase"] = closure_until_phase or ""
    return record


def _runtime_command(repo_root: Path, env: dict[str, str], args: list[str]) -> RuntimeResult:
    command = ["python3", "tools/orchestration_runtime.py", *args]
    completed = subprocess.run(
        command,
        cwd=repo_root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        stdout = completed.stdout.strip()
        detail = stderr or stdout or f"exit={completed.returncode}"
        raise RuntimeError(f"runtime command failed ({' '.join(args)}): {detail}")
    stdout = completed.stdout.strip()
    if not stdout:
        raise RuntimeError(f"runtime command returned empty output ({' '.join(args)})")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"runtime command returned non-JSON output ({' '.join(args)}): {stdout}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"runtime command must return JSON object ({' '.join(args)})")
    return RuntimeResult(payload=payload, raw_stdout=stdout)




def _build_orchestration_prompt(
    *,
    orchestration_id: str,
    orchestration_agent_run_id: str,
    spec_ref: str,
    source_dependency_ref: str,
    until_phase: str,
    workflow_mode: str,
) -> str:
    """Render the orchestration start record written to
    `launches/orchestration.start.prompt.txt`.

    Orchestration is conductor-driven (Python, no parent orchestration LLM), so
    this is no longer an LLM prompt — it is the canonical carrier of the run's
    startup parameters. `--resume` recovers `spec_ref` / `until_phase` /
    `workflow_mode` from this file via `_extract_prompt_params`, so the
    `target_spec_ref:` / `end phase:` / `workflow_mode:` markers are load-bearing
    and pinned by a round-trip unit test. Keep them when editing the wording.
    """
    phase_list = ", ".join(PHASE_ORDER[: PHASE_ORDER.index(until_phase) + 1])
    return textwrap.dedent(
        f"""
        Conductor workflow start record (driver: conductor).

        ## startup context
        - orchestration_id: `{orchestration_id}`
        - orchestration_agent_run_id: `{orchestration_agent_run_id}`
        - workflow_mode: `{workflow_mode}`
        - target_spec_ref: `{spec_ref}`
        - dependency_ref: `{source_dependency_ref}`
        - target_phases: `{phase_list}` (end phase: `{until_phase}`)
        """
    ).strip() + "\n"


def _canonicalize_spec_ref(repo_root: Path, spec_ref: str) -> str:
    resolved = _resolve_existing_ref_path(repo_root, spec_ref, field_name="spec_ref")
    try:
        rel = resolved.relative_to(repo_root)
        return rel.as_posix()
    except ValueError:
        return str(resolved)


def _validate_source_dependency_ref(source_dependency_ref: str) -> str:
    normalized = source_dependency_ref.strip().replace("\\", "/").strip("/")
    if not normalized:
        raise ValueError("source_dependency_ref must be non-empty")
    if not (normalized.startswith("spec/") and normalized.endswith("/deps.yaml")):
        raise ValueError("source_dependency_ref must match spec/.../deps.yaml")
    return normalized


def _discover_source_dependency_ref(repo_root: Path, spec_ref: str) -> str:
    spec_path = _resolve_existing_ref_path(repo_root, spec_ref, field_name="spec_ref")
    dep_path = (spec_path / "deps.yaml") if spec_path.is_dir() else (spec_path.parent / "deps.yaml")
    if not dep_path.exists():
        raise ValueError(f"source_dependency_ref must exist: {dep_path}")
    try:
        dep_ref = dep_path.relative_to(repo_root).as_posix()
    except ValueError as exc:
        raise ValueError(f"source_dependency_ref must be under repo root: {dep_path}") from exc
    return _validate_source_dependency_ref(dep_ref)


def _read_json_if_exists(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
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


def _tail_text(path: Path, *, max_chars: int = 4000) -> str:
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def _collect_noncanonical_write_violations(repo_root: Path, orchestration_id: str) -> list[dict[str, Any]]:
    orch_root = repo_root / "workspace" / "orchestrations" / orchestration_id
    violations_root = orch_root / "violations"
    if not violations_root.is_dir():
        return []
    collected: list[dict[str, Any]] = []
    for path in sorted(violations_root.glob("*.noncanonical_phase_write_attempt.json")):
        payload = _read_json_if_exists(path)
        if not isinstance(payload, dict):
            continue
        attempted = payload.get("attempted_paths")
        attempted_paths = (
            [str(item).strip() for item in attempted if isinstance(item, str) and str(item).strip()]
            if isinstance(attempted, list)
            else []
        )
        collected.append(
            {
                "violation_ref": str(path.relative_to(repo_root)),
                "agent_run_id": str(payload.get("agent_run_id") or "").strip(),
                "reason_code": str(payload.get("reason_code") or "").strip(),
                "attempted_paths": attempted_paths,
            }
        )
    return collected


def _collect_unauthorized_write_violations(repo_root: Path, orchestration_id: str) -> list[dict[str, Any]]:
    orch_root = repo_root / "workspace" / "orchestrations" / orchestration_id
    violations_root = orch_root / "violations"
    if not violations_root.is_dir():
        return []
    collected: list[dict[str, Any]] = []
    for path in sorted(violations_root.glob("*.unauthorized_write_violation.json")):
        payload = _read_json_if_exists(path)
        if not isinstance(payload, dict):
            continue
        unauthorized_obj = payload.get("unauthorized_paths")
        unauthorized_paths = (
            [str(item).strip() for item in unauthorized_obj if isinstance(item, str) and str(item).strip()]
            if isinstance(unauthorized_obj, list)
            else []
        )
        collected.append(
            {
                "violation_ref": str(path.relative_to(repo_root)),
                "agent_run_id": str(payload.get("agent_run_id") or "").strip(),
                "reason_code": "unauthorized_write_violation",
                "attempted_paths": unauthorized_paths,
            }
        )
    return collected


def _collect_failure_analysis(repo_root: Path, orchestration_id: str) -> dict[str, Any]:
    orch_root = repo_root / "workspace" / "orchestrations" / orchestration_id
    meta_path = orch_root / "orchestration_meta.json"
    meta = _read_json_if_exists(meta_path) or {}
    runs = _read_jsonl(orch_root / "agent_runs.jsonl")
    terminal_fail_statuses = {"fail", "blocked", "timeout", "cancel"}

    def _run_key(run: dict[str, Any]) -> tuple[str, str, str]:
        return (
            str(run.get("node_key") or ""),
            str(run.get("step") or ""),
            str(run.get("substep") or ""),
        )

    # Index of the last passing run per (node_key, step, substep). A terminal-nonpass
    # run is "resolved" (superseded) when a *later* run of the same key passed — e.g. the
    # judge timeout that the orchestration re-ran to pass, or a blocked/cancelled verify
    # later re-run green. Such runs must not be reported as the workflow failure. The
    # agent_runs `superseded`/`superseded_by` fields are not currently written, so
    # reconcile by key + replay order instead.
    last_pass_index: dict[tuple[str, str, str], int] = {}
    for idx, run in enumerate(runs):
        if isinstance(run.get("status"), str) and str(run.get("status")).strip().lower() == "pass":
            last_pass_index[_run_key(run)] = idx

    failed_runs = [
        run
        for idx, run in enumerate(runs)
        if isinstance(run.get("status"), str)
        and str(run.get("status")).strip().lower() in terminal_fail_statuses
        and last_pass_index.get(_run_key(run), -1) < idx
    ]
    failed_run = failed_runs[-1] if failed_runs else None

    failed_step_results: list[dict[str, Any]] = []
    for step_result_path in sorted(orch_root.glob("steps/*/*/*/step_result.json")):
        payload = _read_json_if_exists(step_result_path)
        if not payload:
            continue
        status = str(payload.get("status") or "").strip().lower()
        if status and status != "pass":
            failed_step_results.append(
                {
                    "path": str(step_result_path.relative_to(repo_root)),
                    "status": status,
                    "required_outputs": payload.get("required_outputs"),
                    "failed_substeps": payload.get("failed_substeps"),
                }
            )

    launch_reply_tail = ""
    agent_summary_tail = ""
    if isinstance(failed_run, dict):
        launch_reply_ref = failed_run.get("launch_reply_ref")
        if isinstance(launch_reply_ref, str) and launch_reply_ref.strip():
            launch_reply_tail = _tail_text(repo_root / launch_reply_ref.strip())
        agent_summary_ref = failed_run.get("agent_summary_ref")
        if isinstance(agent_summary_ref, str) and agent_summary_ref.strip():
            agent_summary_tail = _tail_text(repo_root / agent_summary_ref.strip())

    # Surface any dangling-launch incident snapshot (written at incident time by the
    # synchronous-launch capture in main()) so failure_analysis links to it. Globbed
    # rather than threaded through a parameter so it also resolves on resume / re-collect.
    launch_incident_refs = [
        str(p.relative_to(repo_root))
        for p in sorted(orch_root.glob("launch_incident.runtime.*.json"))
    ]

    noncanonical_write_violations = _collect_noncanonical_write_violations(repo_root, orchestration_id)
    unauthorized_write_violations = _collect_unauthorized_write_violations(repo_root, orchestration_id)
    write_contract_violations = [*noncanonical_write_violations, *unauthorized_write_violations]
    recommended_retry_decisions: list[dict[str, Any]] = []
    for violation in write_contract_violations:
        target_run = str(violation.get("agent_run_id") or "").strip()
        if not target_run:
            continue
        paths = violation.get("attempted_paths")
        attempted_paths = paths if isinstance(paths, list) else []
        reason_code = str(violation.get("reason_code") or "").strip() or "noncanonical_phase_write_attempt"
        recommended_retry_decisions.append(
            {
                "issue_severity": "major",
                "repair_strategy": "restart",
                "repair_target_agent_run_id": target_run,
                "repair_reason": reason_code + ": " + ",".join(attempted_paths),
            }
        )

    return {
        "status": "fail",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "orchestration_id": orchestration_id,
        "orchestration_agent_run_id": meta.get("orchestration_agent_run_id"),
        "orchestration_started_at": meta.get("started_at"),
        "orchestration_status": meta.get("status"),
        "reason_code": meta.get("reason_code"),
        "reason_detail": meta.get("reason_detail"),
        "failed_agent_run": failed_run,
        "failed_step_results": failed_step_results,
        "noncanonical_write_violations": noncanonical_write_violations,
        "unauthorized_write_violations": unauthorized_write_violations,
        "recommended_retry_decisions": recommended_retry_decisions,
        "launch_reply_tail": launch_reply_tail,
        "agent_summary_tail": agent_summary_tail,
        "launch_incident_refs": launch_incident_refs,
    }


_FAILURE_STATUS_VALUES: frozenset[str] = frozenset(
    {"fail", "fail_closed", "blocked", "timeout", "cancel"}
)

# Statuses that make an orchestration safe to auto-select as "the latest" for
# implicit (`--resume` without `--orchestration-id`) resume. A non-terminal status
# (e.g. `running`) is ambiguous — it may be an active concurrent run whose shared
# workspace/tmp/<arid> resume would clobber, or a crashed run — so implicit resume
# refuses it and asks for an explicit id.
_RESUMABLE_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"pass", "fail", "fail_closed", "blocked", "timeout", "cancel"}
)


# `/proc/<pid>/stat` states that mean the process is no longer executing: `Z` is a
# zombie (exited, not yet reaped by its parent) and `X`/`x` is a dead/exiting entry.
# The stat file — and therefore `starttime` — survives in those states, so a probe
# that only compares pid + start ticks calls a corpse `alive`. That misclassification
# is the worst possible one here: it makes the resume gate refuse recovery AND the
# cold gate refuse a fresh run, locking the spec harder than the bug this all fixes.
_DEAD_PROC_STATES: frozenset[str] = frozenset({"Z", "X", "x"})


def _parse_proc_stat(raw: str) -> tuple[str, str] | None:
    """Extract `(state, starttime_ticks)` — fields 3 and 22 — from a `/proc/<pid>/stat` body.

    Split out from the read so the parsing is directly testable against real stat
    bodies, including the awkward ones: the comm field (2) is parenthesised and may
    itself contain spaces and parentheses (a process can name itself `we ird) (name`),
    so a naive `split()` misaligns every later field. Splitting AFTER the last `)` puts
    field 3 (`state`) at index 0, hence field 22 at index 19.

    Returns None on any malformed body rather than a partial answer: a non-numeric
    starttime recorded as an identity would never compare equal again, so a live driver
    would classify `dead` and get terminalized under a running workload.
    """
    close = raw.rfind(")")
    if close < 0:
        return None
    fields = raw[close + 1 :].split()
    if len(fields) < 20:
        return None
    state, ticks = fields[0], fields[19]
    if not ticks.isdigit():
        return None
    return state, ticks


def _read_proc_stat(pid: int) -> tuple[str, str] | None:
    """Return `(state, starttime_ticks)` for a pid, or None if unreadable/malformed.

    The start ticks paired with the pid make the recorded driver identity resistant to
    PID reuse: a recycled pid belongs to a process that started later, so its ticks
    differ. Both values come from ONE read so they describe the same instant. A None
    here is reported by the probe as `unknown` rather than guessing.
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None
    return _parse_proc_stat(raw)


def _read_proc_starttime(pid: int) -> str | None:
    """Field 22 (`starttime`) of `/proc/<pid>/stat` alone, for identity capture."""
    stat = _read_proc_stat(pid)
    return None if stat is None else stat[1]


def _read_boot_id() -> str | None:
    """Return this boot's `/proc/sys/kernel/random/boot_id`, or None if unreadable.

    Recorded alongside the pid so a driver identity cannot survive a reboot: after a
    restart the same pid may exist again with the same starttime ticks (ticks are
    measured *since boot*), which would otherwise read as `alive`.
    """
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None
    return value or None


def _current_hostname() -> str:
    try:
        return socket.gethostname().strip()
    except OSError:
        return ""


def _read_pid_namespace_inode() -> int | None:
    """Inode of this process's PID namespace (`/proc/self/ns/pid`), or None.

    Recorded so a later probe can answer the question every `/proc`-derived verdict
    depends on: *does the local `/proc` answer for the recorded process at all?* PID
    numbers are namespace-local, so a probe in a different namespace looks up a number
    that means something else there — or nothing at all — and would read a live driver
    as dead, either because the entry is absent or because the unrelated process it
    finds has different start ticks. Reading one's OWN namespace inode is always permitted;
    reading another process's requires PTRACE_MODE_READ, which is why this has to be
    captured driver-side rather than derived at probe time.
    """
    try:
        return os.stat("/proc/self/ns/pid").st_ino
    except (OSError, AttributeError):
        return None


def _matches_recorded_int(recorded: Any, local: int | None) -> bool:
    """Equality for an integer identity field, with `True == 1` refused.

    Python compares `bool` equal to `int`, so a corrupt or hand-edited `"uid": true`
    would otherwise match a real uid of 1 and be read as proof. Every field this
    guards decides whether the local `/proc` may be read as evidence about the recorded
    process at all, so a spurious match terminalizes a live run: reject anything that
    is not a plain int, or that could not be read locally.
    """
    if isinstance(recorded, bool) or not isinstance(recorded, int):
        return False
    # `local is None` (the value could not be read here) needs no branch of its own:
    # an int never equals None.
    return recorded == local


def _same_machine_proven(driver: dict[str, Any]) -> bool:
    """True when the block records a hostname and it is this machine's.

    Every `dead` verdict reasons from LOCAL evidence — this `/proc`, this `boot_id` —
    so all of them need this first. An ABSENT hostname is not a pass: `hostname` is
    omitted only when `socket.gethostname()` raises, and a block written on another
    host that reaches a shared workspace would then have its differing `boot_id` read
    as "this machine rebooted" and its missing `/proc` entry as "the process exited".
    `pid_ns` cannot stand in for it — the initial PID namespace inode is a per-kernel
    constant (typically 4026531836 everywhere), so two hosts routinely agree on it.
    """
    recorded_host = driver.get("hostname")
    if not isinstance(recorded_host, str) or not recorded_host.strip():
        return False
    local_host = _current_hostname()
    return bool(local_host) and local_host == recorded_host.strip()


def _can_observe_recorded_pid(driver: dict[str, Any]) -> bool:
    """True when the local `/proc` may be read as evidence about the recorded process.

    Three conditions make a local observation conclusive, and all are recorded at capture time
    (the first being that the block was written on this machine at all):
    the same PID namespace (so the recorded number is in our numbering), and the same
    uid (so no `hidepid` mode can hide that entry from us — hidepid restricts other
    users' entries, never one's own). Anything missing or mismatched returns False, and
    the probe answers `unknown` instead of `dead`.

    This gates every `dead` verdict that reasons about a LOCAL `/proc` entry: the
    absence inference, the PID-reuse inference, and the zombie state. Having read an
    entry is NOT a substitute — it proves the pid number resolves in our numbering, not
    that it resolves to the recorded process, and across namespaces those are different
    processes whose start ticks differ (which the reuse branch would otherwise call
    proof of death). Verified against a real `unshare -Upf --mount-proc` namespace,
    whose `hostname` and `boot_id` are identical to the host's and so pass the earlier
    guards untouched.

    The boot-id verdict is deliberately NOT gated: `boot_id` is not namespaced, and a
    mismatch proves a reboot outright without reference to any entry.

    A block written before these fields existed therefore keeps only reboot-based
    recovery, degrading to the pre-liveness behavior. That is the fail-safe direction
    and the one this module's asymmetry requires: only an unambiguous `dead` may
    unblock a resume.
    """
    if not _same_machine_proven(driver):
        return False
    if not _matches_recorded_int(driver.get("pid_ns"), _read_pid_namespace_inode()):
        return False
    try:
        local_uid = os.getuid()
    except AttributeError:  # pragma: no cover - non-POSIX
        return False
    return _matches_recorded_int(driver.get("uid"), local_uid)


def _current_driver_identity() -> dict[str, Any] | None:
    """Identity of THIS driver process, for `orchestration_meta.json#driver`.

    Returns None when the pid's start time cannot be read (non-Linux, or a hardened
    /proc): without it a pid alone cannot be trusted after reuse, so we record nothing
    and every later probe degrades to `unknown` — i.e. exactly today's behavior.
    """
    pid = os.getpid()
    ticks = _read_proc_starttime(pid)
    if ticks is None:
        return None
    identity: dict[str, Any] = {"pid": pid, "pid_start_ticks": ticks}
    boot_id = _read_boot_id()
    if boot_id:
        identity["boot_id"] = boot_id
    hostname = _current_hostname()
    if hostname:
        identity["hostname"] = hostname
    # PID namespace + uid: the pair that makes "absent from /proc" conclusive later
    # (see _can_observe_recorded_pid). Both are free to read about oneself.
    pid_ns = _read_pid_namespace_inode()
    if pid_ns is not None:
        identity["pid_ns"] = pid_ns
    try:
        identity["uid"] = os.getuid()
    except AttributeError:  # pragma: no cover - non-POSIX
        pass
    return identity


def _probe_driver_liveness(meta: dict[str, Any] | None) -> str:
    """Classify the driver recorded on an orchestration meta: alive / dead / unknown.

    A `running` orchestration is ambiguous on its own — it may be an active concurrent
    run or the corpse of a host that died without terminalizing. This read-only probe
    resolves that from `orchestration_meta.json#driver`.

    The fail directions are asymmetric on purpose (deterministic-gate principle: a
    necessary-condition gate must not act on an ambiguous signal): only an unambiguous
    `dead` unblocks a resume, and only an unambiguous `alive` blocks a cold run. Every
    indeterminate case — no/invalid block, a meta written on another host, an
    unreadable /proc entry, or a recorded `pid_ns`/`uid` that does not match this
    process — answers `unknown`. That last case is the dominant one in practice: it
    covers every driver block written before those two fields existed.

    One inference is deliberately NOT gated on observability: a `boot_id` mismatch
    proves a reboot outright. It rests instead on the hostname comparison above having
    established that the block was written on this machine, which compares hostname
    STRINGS — two hosts sharing a workspace under one hostname would misclassify a live
    driver. Give the hosts distinct hostnames if a workspace is ever shared.
    """
    if not isinstance(meta, dict):
        return "unknown"
    driver = meta.get("driver")
    if not isinstance(driver, dict) or not driver:
        return "unknown"
    pid = driver.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return "unknown"
    recorded_host = driver.get("hostname")
    if isinstance(recorded_host, str) and recorded_host.strip():
        # A pid from another machine says nothing about a local /proc entry.
        local_host = _current_hostname()
        if not local_host or local_host != recorded_host.strip():
            return "unknown"
    recorded_boot = driver.get("boot_id")
    if isinstance(recorded_boot, str) and recorded_boot.strip():
        local_boot = _read_boot_id()
        if local_boot is None:
            return "unknown"
        if local_boot != recorded_boot.strip():
            # A differing boot id means "this machine rebooted" only once the block is
            # known to have been written on THIS machine. Without that, it is equally
            # consistent with a live driver on another host reaching a shared
            # workspace — so an unproven machine yields `unknown`, never a
            # terminalization.
            if not _same_machine_proven(driver):
                return "unknown"
            # The host rebooted since the run started: that process cannot exist.
            return "dead"
    if not Path("/proc").is_dir():
        return "unknown"
    if not Path(f"/proc/{pid}").exists():
        # Absence is proof of death only where we could have seen the entry: the pid
        # must be in our own namespace's numbering, and not hidden from us by a
        # `hidepid` mount. Otherwise a live driver would be terminalized under load.
        return "dead" if _can_observe_recorded_pid(driver) else "unknown"
    recorded_ticks = driver.get("pid_start_ticks")
    if not isinstance(recorded_ticks, str) or not recorded_ticks.strip():
        return "unknown"
    stat = _read_proc_stat(pid)
    if stat is None:
        # The pid exists but its stat is unreadable (permissions, or it exited
        # between the two syscalls) — indeterminate, not proof of either state.
        return "unknown"
    state, ticks = stat
    # Reading an entry proves the pid NUMBER resolves here — not that it resolves to
    # the recorded process. Across PID namespaces the same number names a different
    # process, whose start ticks naturally differ, which would otherwise be read as
    # proof that the driver died. So both remaining `dead` verdicts are gated on the
    # same observability check as the absence branch.
    if ticks != recorded_ticks.strip():
        # Either the pid was recycled here (the driver is gone) or we are reading an
        # unrelated process in our own numbering. Only the first is proof of death.
        return "dead" if _can_observe_recorded_pid(driver) else "unknown"
    # Same start ticks — the same process, barring an astronomical coincidence. A
    # zombie/exiting entry is a corpse the parent has not reaped, not a working driver.
    if state in _DEAD_PROC_STATES:
        return "dead" if _can_observe_recorded_pid(driver) else "unknown"
    return "alive"


def _resume_command_for(orchestration_id: str) -> str:
    return (
        "python3 tools/run_workflow.py --resume --orchestration-id "
        f"{orchestration_id}"
    )


def _is_valid_failure_analysis(
    obj: Any,
    orchestration_id: str,
    *,
    orchestration_agent_run_id: str | None,
) -> bool:
    """Return True only when obj is a substantive failure analysis for this orchestration run.

    Validity requires:
    1. obj is a non-empty dict
    2. orchestration_id matches exactly
    3. status is a recognised failure value
    4. at least one failure-evidence field is non-None / non-empty
    5. Run-identity: orchestration_agent_run_id must be known (non-None) and must match
       the value embedded in obj.  Any other condition — current ID unknown, canonical
       missing the field, or field mismatch — is treated as unverifiable/invalid.
       Timestamp comparison is NOT used: it cannot distinguish same-run from concurrent
       or reused-ID runs and is therefore not a reliable identity proof.
    """
    if not isinstance(obj, dict) or not obj:
        return False
    if obj.get("orchestration_id") != orchestration_id:
        return False
    status = obj.get("status")
    if not isinstance(status, str) or status.strip().lower() not in _FAILURE_STATUS_VALUES:
        return False
    evidence_fields = (
        "reason_code",
        "reason_detail",
        "failed_agent_run",
        "failed_step_results",
        "recommended_retry_decisions",
        "launch_reply_tail",
        "agent_summary_tail",
        # In the degraded dangling-launch path (both terminalize set-status calls
        # failed), the dangling child has no terminal agent_runs row and meta carries
        # no reason_code/detail, so the incident snapshot ref is the only evidence.
        "launch_incident_refs",
    )
    has_evidence = any(
        obj.get(f) not in (None, "", []) for f in evidence_fields
    )
    if not has_evidence:
        return False

    # Run-identity: exact orchestration_agent_run_id match is the only accepted proof.
    current_run_id = (
        orchestration_agent_run_id.strip()
        if isinstance(orchestration_agent_run_id, str) and orchestration_agent_run_id.strip()
        else None
    )
    if current_run_id is None:
        # Current run ID unavailable (meta missing/corrupt) — cannot verify ownership.
        return False
    obj_run_id = obj.get("orchestration_agent_run_id")
    return isinstance(obj_run_id, str) and obj_run_id.strip() == current_run_id


def _write_failure_analysis(
    repo_root: Path,
    orchestration_id: str,
    payload: dict[str, Any],
    *,
    tmp_dir: Path | None = None,
) -> tuple[str, str | None, str | None]:
    """Write failure analysis and return (analysis_ref, runtime_ref_or_None, stale_canonical_ref_or_None).

    analysis_ref always points to current-run-valid failure data so callers always
    receive an accurate primary reference regardless of what existed on disk.
    runtime_ref and stale_canonical_ref are supplementary references.

    Ownership contract (startup_contract.md):
    - When failure_analysis.json does not exist: write payload there as safety-net.
      → analysis_ref = failure_analysis.json, runtime_ref = None, stale_canonical_ref = None
    - When failure_analysis.json exists and is valid for this run: preserve canonical,
      write sidecar with existing_file_status="valid".
      → analysis_ref = failure_analysis.json, runtime_ref = failure_analysis.runtime.json,
        stale_canonical_ref = None
    - When failure_analysis.json exists but is invalid/stale: preserve canonical (agent
      owns it), write current payload to sidecar with existing_file_status="invalid".
      analysis_ref is redirected to the sidecar so callers always get current-run data.
      → analysis_ref = failure_analysis.runtime.json, runtime_ref = None,
        stale_canonical_ref = failure_analysis.json
    """
    rel = Path("workspace") / "orchestrations" / orchestration_id / "failure_analysis.json"
    path = repo_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    effective_tmp = tmp_dir or path.parent
    canonical_written = _atomic_write_json_exclusive(path, payload, tmp_dir=effective_tmp)
    if canonical_written:
        return str(rel), None, None
    # File already existed (or appeared concurrently) — agent owns canonical; write sidecar only.
    existing = _read_json_if_exists(path)
    orchestration_agent_run_id = payload.get("orchestration_agent_run_id") if isinstance(payload.get("orchestration_agent_run_id"), str) else None
    existing_is_valid = _is_valid_failure_analysis(
        existing,
        orchestration_id,
        orchestration_agent_run_id=orchestration_agent_run_id,
    )
    existing_file_status = "valid" if existing_is_valid else "invalid"
    # Use a UUID-suffixed sidecar name so concurrent runs with the same orchestration_id
    # do not overwrite each other's runtime analysis.
    runtime_slug = uuid.uuid4().hex[:12]
    runtime_rel = (
        Path("workspace") / "orchestrations" / orchestration_id
        / f"failure_analysis.runtime.{runtime_slug}.json"
    )
    _atomic_write_json(
        repo_root / runtime_rel,
        {**payload, "existing_file_status": existing_file_status},
        tmp_dir=effective_tmp,
    )
    if existing_is_valid:
        # Canonical is current-run data → keep it as primary reference.
        return str(rel), str(runtime_rel), None
    # Canonical is stale — redirect analysis_ref to sidecar so callers get current-run data.
    return str(runtime_rel), None, str(rel)


def _atomic_write_json_exclusive(path: Path, payload: dict[str, Any], *, tmp_dir: Path) -> bool:
    """Write payload to path only if path does not already exist; return True on success.

    Uses write-to-temp + O_CREAT|O_EXCL link/rename to eliminate the TOCTOU window
    between an existence check and the write.  If path already exists (FileExistsError),
    returns False without touching the existing file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    try:
        same_device = os.stat(tmp_dir).st_dev == os.stat(path.parent).st_dev
    except OSError:
        same_device = False
    write_dir = tmp_dir if same_device else path.parent
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_path_str = tempfile.mkstemp(dir=write_dir, suffix=".json.tmp")
    tmp = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        # O_CREAT|O_EXCL semantics: link fails atomically if destination exists.
        try:
            os.link(tmp, path)
            return True
        except FileExistsError:
            return False
        except OSError:
            # Fallback for filesystems that don't support hard links (e.g. some overlayfs).
            # Write to a second temp file, then install it via O_CREAT|O_EXCL rename-equivalent:
            # open the destination exclusively, copy bytes, then close.  If the write fails
            # mid-stream, remove the partial destination to avoid poisoning later runs.
            try:
                excl_fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
            except FileExistsError:
                return False
            dest_created = True
            try:
                with os.fdopen(excl_fd, "w", encoding="utf-8") as ef:
                    ef.write(text)
                dest_created = False  # write succeeded; don't remove on exit
                return True
            finally:
                if dest_created:
                    # Write failed — remove the partial canonical so it doesn't corrupt future runs.
                    path.unlink(missing_ok=True)
    finally:
        tmp.unlink(missing_ok=True)


def _atomic_write_json(path: Path, payload: dict[str, Any], *, tmp_dir: Path) -> None:
    """Write payload as JSON to path atomically via a unique temp file.

    Temp file is placed in tmp_dir when it is on the same device as path.parent
    (guarantees atomic rename).  Falls back to path.parent otherwise to avoid
    EXDEV on cross-device rename (e.g. split/bind mounts).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    # Proactively avoid EXDEV: rename is atomic only within the same filesystem.
    try:
        same_device = os.stat(tmp_dir).st_dev == os.stat(path.parent).st_dev
    except OSError:
        same_device = False
    write_dir = tmp_dir if same_device else path.parent
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_path_str = tempfile.mkstemp(dir=write_dir, suffix=".json.tmp")
    tmp = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        tmp.replace(path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise












def _ensure_preflight_pass(preflight: dict[str, Any]) -> tuple[bool, str]:
    status = preflight.get("status")
    can_step = preflight.get("can_launch_step_agents")
    can_substep = preflight.get("can_launch_substep_agents")
    reasons: list[str] = []
    if status != "pass":
        reasons.append(f"status={status!r}")
    if can_step is not True:
        reasons.append(f"can_launch_step_agents={can_step!r}")
    if can_substep is not True:
        reasons.append(f"can_launch_substep_agents={can_substep!r}")
    if reasons:
        return False, ", ".join(reasons)
    return True, "pass"


def _find_latest_orchestration(repo_root: Path) -> str | None:
    """Return the most recent orchestration_id under workspace/orchestrations.

    Ranking uses orchestration_meta.json#started_at (UTC ISO8601, microsecond
    precision — chronologically sortable as text) rather than the directory name.
    A lexical max over ids is wrong because: (1) ids may be caller-supplied via
    --orchestration-id (e.g. `orch_unit`) and would sort after timestamp ids like
    `orch_202606...`, and (2) ids generated in the same second differ only by a
    random suffix, so name order is not creation order. Orchestrations whose meta
    lacks a usable started_at sort oldest; the id is a deterministic tie-breaker.

    Any subdirectory carrying an orchestration_meta.json is an orchestration —
    the id need not start with `orch_`, since --orchestration-id accepts arbitrary
    caller-supplied ids and those runs must remain resumable as "the latest".
    """
    orch_root = repo_root / "workspace" / "orchestrations"
    if not orch_root.is_dir():
        return None
    candidates: list[tuple[str, str]] = []
    for path in orch_root.iterdir():
        if not path.is_dir():
            continue
        meta = _read_json_if_exists(path / "orchestration_meta.json")
        if not isinstance(meta, dict):
            continue
        started_at = meta.get("started_at")
        started_key = started_at.strip() if isinstance(started_at, str) else ""
        candidates.append((started_key, path.name))
    if not candidates:
        return None
    # max over (started_at, id): newest start wins; equal/empty starts fall back
    # to a stable lexical id tie-break.
    return max(candidates, key=lambda item: (item[0], item[1]))[1]


def _index_closure_orchestrations(repo_root: Path, closure_id: str) -> dict[str, str]:
    """Map `spec_ref -> orchestration_id` for every orchestration recorded as part of
    the given closure (`orchestration_meta.json#invocation.closure_id == closure_id`).

    Keeps the latest per `spec_ref` by `started_at` (id as a deterministic tie-break),
    mirroring `_find_latest_orchestration`'s ranking, so a dependency that was run more
    than once under one closure resolves to its most recent orchestration. Used by
    closure-aware resume to find each not-ready node's prior orchestration so it can be
    resumed (warm, from its checkpoint) rather than re-run cold."""
    orch_root = repo_root / "workspace" / "orchestrations"
    if not orch_root.is_dir():
        return {}
    # spec_ref -> (started_at_key, orch_id) best seen so far
    best: dict[str, tuple[str, str]] = {}
    for path in orch_root.iterdir():
        if not path.is_dir():
            continue
        meta = _read_json_if_exists(path / "orchestration_meta.json")
        if not isinstance(meta, dict):
            continue
        invocation = meta.get("invocation")
        if not isinstance(invocation, dict) or invocation.get("closure_id") != closure_id:
            continue
        spec_ref = meta.get("spec_ref")
        if not isinstance(spec_ref, str) or not spec_ref.strip():
            continue
        spec_ref = spec_ref.strip()
        started_at = meta.get("started_at")
        started_key = started_at.strip() if isinstance(started_at, str) else ""
        candidate = (started_key, path.name)
        prior = best.get(spec_ref)
        if prior is None or candidate > prior:
            best[spec_ref] = candidate
    return {spec_ref: value[1] for spec_ref, value in best.items()}


def _read_orchestration_meta(repo_root: Path, orchestration_id: str) -> dict[str, Any]:
    meta = _read_json_if_exists(
        repo_root / "workspace" / "orchestrations" / orchestration_id
        / "orchestration_meta.json"
    )
    return meta if isinstance(meta, dict) else {}


def _is_non_terminal_status(meta: dict[str, Any]) -> bool:
    """True when this meta's status is not one the resume gate treats as terminal.

    The same predicate the resume gate uses, so the two gates agree on what counts as
    an incomplete orchestration. Testing for `!= "running"` instead would miss a run
    started with an operator-supplied `--status`, and would disagree with the doc.
    """
    return str(meta.get("status") or "").strip().lower() not in _RESUMABLE_TERMINAL_STATUSES


def _index_incomplete_orchestrations_by_spec(repo_root: Path) -> dict[str, list[str]]:
    """Map `spec_ref -> [orchestration_id, ...]` for every orchestration whose meta is
    not in a terminal status.

    One linear scan of `workspace/orchestrations` (same shape as
    `_index_closure_orchestrations`), run by the cold-start guard on every call: a
    fresh run of a spec that already has a non-terminal orchestration is either
    concurrent with a live driver (refuse) or about to discard a resumable checkpoint
    (warn). All matching ids are kept — a spec can accumulate several corpses —
    ordered by `started_at` (id as a deterministic tie-break) so the emitted warnings
    are stable.

    The result is a candidate list, not a verdict: the guard re-reads each candidate's
    meta and re-checks the status before acting on it, so a run that terminalized
    between this scan and the probe is dropped rather than reported.
    """
    orch_root = repo_root / "workspace" / "orchestrations"
    if not orch_root.is_dir():
        return {}
    found: dict[str, list[tuple[str, str]]] = {}
    for path in orch_root.iterdir():
        if not path.is_dir():
            continue
        meta = _read_json_if_exists(path / "orchestration_meta.json")
        if not isinstance(meta, dict):
            continue
        if not _is_non_terminal_status(meta):
            continue
        spec_ref = meta.get("spec_ref")
        if not isinstance(spec_ref, str) or not spec_ref.strip():
            continue
        found.setdefault(spec_ref.strip(), []).append(
            (
                meta.get("started_at").strip()
                if isinstance(meta.get("started_at"), str)
                else "",
                path.name,
            )
        )
    return {
        spec_ref: [oid for _, oid in sorted(entries)]
        for spec_ref, entries in found.items()
    }


def _terminalize_dead_driver(
    repo_root: Path,
    orchestration_id: str,
    meta: dict[str, Any],
    *,
    stdout_format: str,
    env: dict[str, str] | None = None,
) -> str | None:
    """Terminalize an orchestration whose driver was PROVEN dead. Returns an error
    string on failure, None on success.

    Recording `fail` / `driver_crashed` is what makes the corpse recoverable: the
    subsequent `init --resume-from-checkpoint` then takes the `terminal_reset` path,
    which is where the crash reconciliations live (stale active_child markers, orphan
    agent_graph edges, stale `child_running` phase state, orphan launch tombstones).
    Resuming a still-`running` meta skips all of them. Only ever called with a `dead`
    probe verdict — an `unknown` must never mint a terminal status for a run that may
    still be alive.
    """
    driver = meta.get("driver") if isinstance(meta.get("driver"), dict) else {}
    prior_status = str(meta.get("status") or "").strip().lower() or "unknown"
    # This can run BEFORE base_env exists (the entry resume gate), so build the one
    # setting the runtime subprocess must not go without: bytecode written into the
    # repo source tree lands in a later child's write-diff as an unauthorized write.
    runtime_env = {**(env or dict(os.environ)), "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        _runtime_command(
            repo_root,
            runtime_env,
            [
                "set-status",
                "--repo-root",
                str(repo_root),
                "--orchestration-id",
                orchestration_id,
                "--status",
                "fail",
                "--reason-code",
                "driver_crashed",
                "--reason-detail",
                (
                    f"driver process (pid {driver.get('pid')}) is gone while the "
                    f"orchestration was still '{prior_status}'; terminalized by a "
                    "later run_workflow invocation so the checkpoint stays resumable"
                ),
            ],
        )
    except RuntimeError as exc:
        return str(exc)
    # Announced only after the write committed, so the event never claims a
    # terminalization that did not happen.
    _emit_closure_event(
        {
            "status": "info",
            "event": "dead_driver_terminalized",
            "orchestration_id": orchestration_id,
            "prior_status": prior_status,
            "driver_pid": driver.get("pid"),
            "reason_code": "driver_crashed",
        },
        stdout_format,
    )
    return None


def _warm_resume_liveness_guard(
    repo_root: Path,
    orchestration_id: str,
    *,
    stdout_format: str,
    env: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Liveness gate for a node about to be WARM-resumed (closure driver).

    Mirrors the entry-point resume gate in `main()` for every closure member, which the
    entry gate never sees: a dependency whose own driver died mid-closure is
    terminalized (so its resume runs the crash reconciliations), a live one refuses the
    whole closure, and an indeterminate one proceeds with a warn. Returns a fail
    envelope to emit, or None to proceed.
    """
    meta = _read_orchestration_meta(repo_root, orchestration_id)
    if not meta:
        return None
    status = str(meta.get("status") or "").strip().lower()
    if status in _RESUMABLE_TERMINAL_STATUSES:
        return None
    liveness = _probe_driver_liveness(meta)
    driver = meta.get("driver") if isinstance(meta.get("driver"), dict) else {}
    if liveness == "dead":
        error = _terminalize_dead_driver(
            repo_root, orchestration_id, meta, stdout_format=stdout_format, env=env
        )
        if error is not None:
            return {
                "status": "fail",
                "reason": "dead_driver_terminalize_failed",
                "detail": (
                    f"orchestration {orchestration_id} has a dead driver but could not "
                    f"be terminalized: {error}"
                ),
                "orchestration_id": orchestration_id,
            }
        return None
    if liveness == "alive":
        # Same reason code as the entry-point resume gate's explicit-id refusal: both
        # are "this orchestration cannot be resumed, its driver is still running", and
        # an operator (or script) must not have to know which gate refused to match on
        # it. `concurrent_orchestration_running` stays reserved for the cold path.
        return {
            "status": "fail",
            "reason": "orchestration_driver_alive",
            "detail": (
                f"orchestration {orchestration_id} is still running and its driver "
                f"(pid {driver.get('pid')}) is alive; the closure cannot resume it "
                "while the live run owns its workspace state. Wait for it to finish."
            ),
            "orchestration_id": orchestration_id,
            "driver_pid": driver.get("pid"),
            "resume_command": _resume_command_for(orchestration_id),
        }
    _emit_closure_event(
        {
            "status": "info",
            "event": "resume_liveness_indeterminate",
            "orchestration_id": orchestration_id,
            "orchestration_status": status or "unknown",
        },
        stdout_format,
    )
    return None


def _terminalize_interrupted_orchestration(
    repo_root: Path,
    env: dict[str, str],
    orchestration_id: str,
) -> None:
    """Best-effort `cancel` / `driver_interrupted` terminalization for a run whose
    driver was interrupted (Ctrl-C or SIGTERM).

    Called only from inside `_run_node`, so its event goes to the installed
    `_StdoutTee` as raw JSON (see the print below) rather than through
    `_emit_closure_event`, which pre-renders for the terminal.

    An already-terminal status is preserved: the conductor/runtime may have recorded a
    more specific outcome (e.g. `fail_closed` / `sandbox_enforcement_violation`) before
    the signal arrived, and terminal→terminal is rejected anyway. Every failure is
    swallowed — the process is on its way out and the interrupt must still propagate.
    """
    meta_now = _read_orchestration_meta(repo_root, orchestration_id)
    current = str(meta_now.get("status") or "").strip().lower()
    if current in _RESUMABLE_TERMINAL_STATUSES:
        return
    try:
        _runtime_command(
            repo_root,
            env,
            [
                "set-status",
                "--repo-root",
                str(repo_root),
                "--orchestration-id",
                orchestration_id,
                "--status",
                "cancel",
                "--reason-code",
                "driver_interrupted",
                "--reason-detail",
                (
                    "driver process was interrupted (SIGTERM / KeyboardInterrupt); "
                    "terminalized so the checkpoint stays resumable"
                ),
            ],
        )
    except Exception:  # noqa: BLE001 - the interrupt must still propagate
        return
    try:
        # Printed as RAW JSON, not via _emit_closure_event: this is the one new gate
        # event emitted from INSIDE _run_node, where `_StdoutTee` is installed. The tee
        # mirrors the inbound bytes verbatim into run_logs/run_*.jsonl and renders the
        # human form only for the terminal, so pre-rendering here would put a plain
        # text line into a file contracted to hold the full JSON payload of every event
        # (and break every consumer that json.loads it line by line).
        print(
            json.dumps(
                {
                    "status": "info",
                    "event": "driver_interrupted",
                    "orchestration_id": orchestration_id,
                    "reason_code": "driver_interrupted",
                    "resume_command": _resume_command_for(orchestration_id),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    except Exception:  # noqa: BLE001 - the interrupt must still propagate
        pass


def _is_own_driver(meta: dict[str, Any], identity: dict[str, Any] | None) -> bool:
    """True when this meta's `driver` block names THIS process.

    An orchestration driven by this process is not a concurrent run: probing it would
    report `alive` and block us against our own work. Comparing every recorded identity
    field identifies our own runs exactly, with no bookkeeping to keep in sync.

    Within one `--with-deps` closure this cannot trigger — every node has a distinct
    `spec_ref` and each node's guard runs before its own `init` — so it exists for the
    case that CAN: a process that calls `main()` more than once against the same repo,
    where an earlier call left a non-terminal orchestration for the spec a later call
    cold-runs (e.g. `--no-run-conductor`, which never terminalizes). Without it that
    second call would refuse, naming a run that has already returned.
    """
    if not identity:
        return False
    driver = meta.get("driver")
    if not isinstance(driver, dict):
        return False
    # Every recorded field is compared, including `pid_ns` and `uid`. Concluding "this
    # is us" from a subset while the block explicitly records a DIFFERENT namespace or
    # uid would be the same error the probe's gate exists to prevent: a conclusion
    # drawn past evidence that contradicts it. This direction fails open (a skipped
    # candidate means a concurrent run goes unblocked) rather than terminalizing a live
    # driver, so it is defense in depth — but the asymmetry is not a reason to compare
    # less than what is on record.
    return all(
        driver.get(key) == identity.get(key)
        for key in ("pid", "pid_start_ticks", "boot_id", "pid_ns", "uid")
    )


def _claim_lock_path(repo_root: Path, kind: str, key: str) -> Path:
    """Where a per-(repo, kind, key) start claim lives.

    Outside the repository, alongside the operator tokens, for the same reason those
    are: a file under `workspace/` created while a leaf is running lands in that leaf's
    terminal write-diff and is misattributed as an unauthorized write. The
    write-snapshot exemptions are all keyed to a specific `orchestration_id`, which a
    per-SPEC claim taken BEFORE an id is minted cannot use.
    """
    digest = hashlib.sha256(
        f"{repo_root}\0{kind}\0{key}".encode("utf-8")).hexdigest()[:32]
    # `METDSL_START_CLAIM_ROOT` relocates the whole set. It exists so a caller that
    # drives many throwaway repo roots — the test suite does exactly this — does not
    # accumulate one 0-byte file per (root, key) in the operator's home. Production
    # leaves it unset: one file per orchestration and per spec, which the OS releases
    # on process death, is the point.
    override = os.environ.get("METDSL_START_CLAIM_ROOT", "").strip()
    root = Path(override) if override else Path.home() / ".met-dsl" / "start_claims"
    return root / f"{kind}.{digest}.lock"


@contextlib.contextmanager
def _exclusive_claim(repo_root: Path, kind: str, key: str):
    """Hold an advisory, process-scoped claim on `(repo_root, kind, key)`.

    `kind="spec"` serializes cold starts of one spec against each other;
    `kind="orch"` serializes drivers of one orchestration — two `--resume` invocations
    of the same run would otherwise both pass the liveness gate, both terminalize the
    dead driver, and both `init --resume-from-checkpoint` into the SAME preserved
    `orchestration_agent_run_id`, sharing one `workspace/tmp/<arid>` that either one's
    cleanup then deletes.

    Yields True when the claim is held (proceed) and False when another process holds
    it (refuse). A host where the lock cannot be taken at all (no `fcntl`, an
    unsupported filesystem, an unwritable home) yields True: the claim strengthens the
    driver-liveness guard, it is never a precondition for running.
    """
    path = _claim_lock_path(repo_root, kind, key)
    handle = None
    if fcntl is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                path.parent.chmod(0o700)
            except OSError:
                pass
            handle = path.open("a+", encoding="utf-8")
        except OSError:
            handle = None
    if handle is None:
        yield True
        return
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Held by another cold start of this spec — the case this exists for.
            yield False
            return
        except OSError:
            # Locking unsupported here (e.g. some network filesystems). Degrade to the
            # driver-liveness guard alone rather than refusing to run.
            yield True
            return
        yield True
    finally:
        # Closing the descriptor releases the flock; the OS releases it anyway if this
        # process dies, so a crashed run never leaves the spec claimed. Removing this
        # close is not observable under CPython — refcounting would collect the handle
        # here regardless — but that is an implementation detail, not a guarantee, so
        # the release stays explicit rather than resting on it.
        try:
            handle.close()
        except OSError:
            pass


def _concurrent_cold_start_envelope(spec_ref: str) -> dict[str, Any]:
    return {
        "status": "fail",
        "reason": "concurrent_orchestration_running",
        "detail": (
            f"another cold run of {spec_ref} is starting or running in this repository "
            "(its start claim is held). Two concurrent runs of one spec derive their "
            "pipeline_id from the same workspace/pipelines/<node_key_safe>/ tree and "
            "then write into it. Wait for it to finish, or resume it with "
            "--resume --orchestration-id <its id>."
        ),
        "spec_ref": spec_ref,
    }


def _cold_start_running_guard(
    repo_root: Path,
    spec_ref: str,
    *,
    stdout_format: str,
    driver_identity: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Guard a COLD run of `spec_ref` against this spec's non-terminal orchestrations.

    A live driver → return a fail envelope (`concurrent_orchestration_running`): two
    concurrent runs of one spec derive their `pipeline_id` from the same
    `workspace/pipelines/<node_key_safe>/` tree and then write into it, so the second
    run corrupts the first's.

    A dead or indeterminate driver → emit a `prior_incomplete_orchestration` warn
    naming the exact `--resume` command, and proceed. The cold path deliberately does
    NOT terminalize (that would be a write on a run we were not asked to touch, and an
    `unknown` may still be alive); it only makes sure the operator sees that a
    resumable checkpoint is about to be left behind — inform over prohibit.

    The workspace is rescanned on every call, not sampled once per invocation: a
    `--with-deps` closure reaches its later nodes hours after it started, and the
    concurrent run this guard exists to catch is most likely to have been launched
    inside that window. A snapshot taken at closure start is blind to exactly that
    case. Orchestrations this process itself drives are excluded (`_is_own_driver`).
    """
    candidates = _index_incomplete_orchestrations_by_spec(repo_root).get(spec_ref) or []
    if not candidates:
        return None
    probed = []
    for oid in candidates:
        meta = _read_orchestration_meta(repo_root, oid)
        if not meta or not _is_non_terminal_status(meta):
            continue
        if _is_own_driver(meta, driver_identity):
            continue
        probed.append((oid, meta, _probe_driver_liveness(meta)))
    for oid, meta, liveness in probed:
        if liveness != "alive":
            continue
        driver = meta.get("driver") if isinstance(meta.get("driver"), dict) else {}
        return {
            "status": "fail",
            "reason": "concurrent_orchestration_running",
            "detail": (
                f"orchestration {oid} for {spec_ref} is still running and its driver "
                f"(pid {driver.get('pid')}) is alive; a second run would corrupt its "
                "in-flight pipeline state. Wait for it, or resume it with: "
                f"{_resume_command_for(oid)}"
            ),
            "orchestration_id": oid,
            "spec_ref": spec_ref,
            "driver_pid": driver.get("pid"),
            "resume_command": _resume_command_for(oid),
        }
    for oid, _meta, liveness in probed:
        _emit_closure_event(
            {
                "status": "info",
                "event": "prior_incomplete_orchestration",
                "spec_ref": spec_ref,
                "orchestration_id": oid,
                "liveness": liveness,
                "resume_command": _resume_command_for(oid),
            },
            stdout_format,
        )
    return None


def _extract_prompt_params(prompt_text: str) -> dict[str, str]:
    """Recover startup params embedded by _build_orchestration_prompt().

    Returns whichever of {until_phase, mode, spec_ref} can be parsed from the
    `orchestration.start.prompt.txt` body. A round-trip unit test pins this
    extractor to the prompt format so a wording change cannot silently break it.
    """
    found: dict[str, str] = {}
    mode_match = re.search(r"workflow_mode:\s*`([^`]+)`", prompt_text)
    if mode_match:
        found["mode"] = mode_match.group(1).strip()
    phase_match = re.search(r"end phase:\s*`([^`]+)`", prompt_text)
    if phase_match is None:
        # Backward compatibility: orchestrations created before the English
        # translation of the start prompt used the Japanese "終了 phase:" label.
        phase_match = re.search(r"終了 phase:\s*`([^`]+)`", prompt_text)
    if phase_match:
        found["until_phase"] = phase_match.group(1).strip()
    spec_match = re.search(r"target_spec_ref:\s*`([^`]+)`", prompt_text)
    if spec_match:
        found["spec_ref"] = spec_match.group(1).strip()
    return found


def _load_resume_params(repo_root: Path, orchestration_id: str) -> dict[str, str | None]:
    """Recover launch params for a resume from an orchestration's existing artifacts.

    No dedicated params file is persisted: every value is recovered from artifacts
    that run_workflow.py already writes on every start.
    - spec_ref / source_dependency_ref ← orchestration_meta.json
    - llm                              ← preflight.json#backend
    - llm_command                      ← preflight.json#probe_command
    - until_phase / mode               ← launches/orchestration.start.prompt.txt
    - agent_model                      ← orchestration_meta.json#invocation
    - llm_config_path / llm_config_sha256 / llm_config_overrides
                                       ← orchestration_meta.json#invocation
    - closure_id / closure_target_spec_ref / closure_until_phase
                                       ← orchestration_meta.json#invocation
    Missing/unparseable values are returned as None for the caller to validate. The
    `closure_*` keys are set only when the run was a `--with-deps` node (older
    orchestrations lack the `invocation` block → None → single-node resume).
    """
    orch_root = repo_root / "workspace" / "orchestrations" / orchestration_id
    meta = _read_json_if_exists(orch_root / "orchestration_meta.json") or {}
    preflight = _read_json_if_exists(orch_root / "preflight.json") or {}
    prompt_path = orch_root / "launches" / "orchestration.start.prompt.txt"
    prompt_text = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else ""
    prompt_params = _extract_prompt_params(prompt_text)
    invocation = meta.get("invocation")
    invocation = invocation if isinstance(invocation, dict) else {}

    def _clean(value: Any) -> str | None:
        return value.strip() if isinstance(value, str) and value.strip() else None

    return {
        "spec_ref": _clean(meta.get("spec_ref")) or prompt_params.get("spec_ref"),
        "source_dependency_ref": _clean(meta.get("source_dependency_ref")),
        "llm": _clean(preflight.get("backend")),
        # probe_command is the agent command run_workflow used for both preflight
        # and launch on the original run; reuse it so a custom --llm-command (e.g.
        # a wrapper / non-PATH binary) survives resume.
        "llm_command": _clean(preflight.get("probe_command")),
        "until_phase": prompt_params.get("until_phase"),
        "mode": prompt_params.get("mode"),
        "agent_model": _clean(invocation.get("agent_model")),
        # Leaf-LLM configuration (issue #28). Absent on an orchestration launched before the
        # field existed — that is the LEGACY branch, recovered through `llm`/`llm_command`/
        # `agent_model` exactly as before and never rejected.
        "llm_config_path": _clean(invocation.get("llm_config_path")),
        "llm_config_sha256": _clean(invocation.get("llm_config_sha256")),
        "closure_id": _clean(invocation.get("closure_id")),
        "closure_target_spec_ref": _clean(invocation.get("closure_target_spec_ref")),
        "closure_until_phase": _clean(invocation.get("closure_until_phase")),
        # Z2 executor. Since M-F the recovered value is used ONLY by the resume fail-close gate in
        # main(): anything other than `pure` (a `legacy` record, or None from an orchestration
        # predating the field) is rejected — legacy execution was removed, so those runs cannot be
        # resumed and must be re-run cold.
        "generate_executor": _clean(invocation.get("generate_executor")),
    }


def _recorded_generate_executor(repo_root: Path, orchestration_id: str) -> str | None:
    """The cleaned `invocation.generate_executor` recorded on `orchestration_id`, or None.

    Same normalization as `_load_resume_params` (strip; non-string / empty → None), but reads
    only `orchestration_meta.json` so the closure-member resume gate can validate every member
    orchestration cheaply (no preflight / prompt parse)."""
    meta = _read_json_if_exists(
        repo_root / "workspace" / "orchestrations" / orchestration_id / "orchestration_meta.json"
    ) or {}
    invocation = meta.get("invocation")
    invocation = invocation if isinstance(invocation, dict) else {}
    value = invocation.get("generate_executor")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _generate_executor_resume_rejection(
    orchestration_id: str, recorded_executor: str | None
) -> dict[str, Any] | None:
    """Fail-close envelope (M-F) when `recorded_executor` is not exactly `pure`, else None.

    Legacy generate execution was removed, so any resume of an orchestration recorded `legacy`,
    with the field absent (None → a pre-adoption legacy run), or carrying garbage must be rejected
    rather than silently promoted to the pure-only shape-based dispatch. Shared by the entry gate
    in `main()` and the per-member gate in `_run_with_dependency_closure` so a closure resume
    validates EVERY member it warm-resumes, not just the entry orchestration."""
    if recorded_executor == "pure":
        return None
    return {
        "status": "fail",
        "reason": "generate_executor_legacy_removed",
        "detail": (
            f"cannot resume orchestration {orchestration_id}: its recorded generate-executor is "
            f"{recorded_executor!r} (None = a run predating the field, i.e. a legacy run), but "
            f"legacy generate execution was removed at M-F. The run is NOT silently switched to "
            f"pure and legacy is not run; start a fresh run instead."
        ),
        "orchestration_id": orchestration_id,
    }


def _recorded_llm_config(repo_root: Path, orchestration_id: str) -> dict[str, Any]:
    """The recorded leaf-LLM configuration pin of `orchestration_id`.

    Reads only `orchestration_meta.json`, so the closure-member resume gate can validate every
    member cheaply (no preflight / prompt parse) — the same shape as
    `_recorded_generate_executor`. An orchestration launched before issue #28 has none of these
    keys; the caller reads that as the legacy branch, not as a mismatch."""
    meta = _read_json_if_exists(
        repo_root / "workspace" / "orchestrations" / orchestration_id / "orchestration_meta.json"
    ) or {}
    invocation = meta.get("invocation")
    invocation = invocation if isinstance(invocation, dict) else {}

    def _clean(value: Any) -> str:
        return value.strip() if isinstance(value, str) else ""

    overrides = invocation.get("llm_config_overrides")
    return {
        "path": _clean(invocation.get("llm_config_path")),
        "sha256": _clean(invocation.get("llm_config_sha256")),
        "overrides": {str(k): str(v) for k, v in overrides.items()}
                     if isinstance(overrides, dict) else {},
    }


def _llm_config_resume_rejection(
    orchestration_id: str, recorded: dict[str, Any], *,
    repo_root: Path, effective_path: str, effective_sha256: str,
    effective_overrides: dict[str, str],
) -> dict[str, Any] | None:
    """Fail-close envelope when the leaf-LLM configuration is not the one the run launched with.

    A resume continues an IN-FLIGHT run: its finished phases ran on the models the recorded
    config named, and its remaining ones would run on whatever the file says now. Silently
    switching them is the same class of hazard as switching the generate executor
    (`_generate_executor_resume_rejection`, whose shape this follows), so the file's BYTES are
    re-hashed and compared, and the deprecated-flag overrides — which are not in the file —
    are compared as recorded literals.

    Returns None (no rejection) when the orchestration recorded no config pin at all: that is
    a run predating issue #28, which is recovered through the legacy `llm`/`llm_command`/
    `agent_model` route and carries no promise to break. Shared by the entry gate in `main()`
    and the per-member gate in `_run_with_dependency_closure`."""
    if not recorded.get("path") and not recorded.get("sha256"):
        return None                       # legacy record: nothing was pinned

    def _fail(detail: str) -> dict[str, Any]:
        return {
            "status": "fail",
            "reason": "llm_config_changed_since_launch",
            "detail": detail,
            "orchestration_id": orchestration_id,
            "recorded_llm_config_path": recorded.get("path", ""),
            "recorded_llm_config_sha256": recorded.get("sha256", ""),
            "effective_llm_config_path": effective_path,
            "effective_llm_config_sha256": effective_sha256,
        }

    recorded_path = str(recorded.get("path") or "")
    if recorded_path and recorded_path != effective_path:
        return _fail(
            f"cannot resume orchestration {orchestration_id}: it launched with leaf-LLM "
            f"configuration {recorded_path!r}, but this resume would use "
            f"{effective_path!r}. Resume without --llm-config (or pass the same file); to "
            f"change the configuration, start a fresh run."
        )
    on_disk = config_sha256(repo_root / recorded_path) if recorded_path else "sha256:missing"
    if on_disk == "sha256:missing":
        return _fail(
            f"cannot resume orchestration {orchestration_id}: its leaf-LLM configuration "
            f"{recorded_path!r} is gone. Restore that file, or start a fresh run."
        )
    if on_disk == "sha256:unreadable":
        return _fail(
            f"cannot resume orchestration {orchestration_id}: its leaf-LLM configuration "
            f"{recorded_path!r} exists but cannot be read, so it cannot be shown to be the one "
            f"the run launched with. Restore access to that file, or start a fresh run."
        )
    # BOTH hashes must match the record. `effective_sha256` is the snapshot the run will
    # actually use — the bytes `load_llm_config` read and resolved — and `on_disk` is what the
    # file says right now. Comparing only the file leaves a window: an atomic replace between
    # this gate and the load (or between the load and here) resolves the entries from bytes
    # neither hash describes, and the resume proceeds on a configuration nothing pinned. The
    # caller passes the on-disk hash before the load and the snapshot hash after it, so between
    # the two invocations both are checked.
    for label, digest in (("on disk", on_disk), ("as loaded", effective_sha256)):
        if digest and digest != str(recorded.get("sha256") or ""):
            return _fail(
                f"cannot resume orchestration {orchestration_id}: its leaf-LLM configuration "
                f"{recorded_path!r} has changed since launch (recorded "
                f"{recorded.get('sha256')}, {label} {digest}). The phases already run used the "
                f"recorded models; resuming would silently run the rest on the new ones. "
                f"Restore the file, or start a fresh run."
            )
    if dict(recorded.get("overrides") or {}) != dict(effective_overrides or {}):
        return _fail(
            f"cannot resume orchestration {orchestration_id}: its deprecated leaf-LLM flag "
            f"overrides differ from this resume's ({recorded.get('overrides')} vs "
            f"{effective_overrides}). Re-pass the original flags, or start a fresh run."
        )
    return None


class _DeprecatedAliasAction(argparse.Action):
    """A flag alias that sets a fixed value and warns once toward its canonical name.

    Used to keep the legacy ``--invoke-llm`` / ``--no-invoke-llm`` spellings working
    after the option was renamed to ``--run-conductor`` (the flag no longer invokes an
    LLM; it runs the deterministic conductor).
    """

    def __init__(
        self,
        option_strings: list[str],
        dest: str,
        canonical: str,
        store_value: bool,
        **kwargs: Any,
    ) -> None:
        self._canonical = canonical
        self._store_value = store_value
        super().__init__(option_strings, dest, nargs=0, **kwargs)

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        sys.stderr.write(
            f"warning: {option_string} is a deprecated alias; use {self._canonical} instead\n"
        )
        setattr(namespace, self.dest, self._store_value)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bootstrap workflow startup (init + preflight + prompt).",
    )
    parser.add_argument(
        "spec_ref",
        nargs="?",
        help="Target spec path/reference. Optional with --resume (recovered from the resumed orchestration).",
    )
    parser.add_argument(
        "until_phase",
        nargs="?",
        help=(
            "Final phase to execute (compile/generate/build/validate). "
            "Optional with --resume (recovered from the resumed orchestration)."
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Resume the latest orchestration (or --orchestration-id) from its checkpoint. "
            "spec_ref / until_phase / --llm / --mode are recovered from the resumed "
            "orchestration when omitted. When the resumed orchestration is a node of a "
            "--with-deps closure (recorded in orchestration_meta.json#invocation), the "
            "whole closure is re-derived and continued to the target — not just one node."
        ),
    )
    parser.add_argument(
        "--mode",
        default=None,
        choices=SUPPORTED_WORKFLOW_MODES,
        help="Workflow execution mode: dev (default) or prod.",
    )
    parser.add_argument(
        "--llm-config",
        default=None,
        metavar="PATH",
        help=(
            "Leaf-LLM configuration file (YAML): which provider/model runs each phase and "
            "substep. Defaults to configs/llm/claude.yaml. See docs/ORCHESTRATION.md "
            "'Leaf LLM configuration' for the resolution order, the provider capability "
            "matrix, and the named rejection rules. Mutually exclusive with --llm."
        ),
    )
    parser.add_argument(
        "--llm", default=None, choices=SUPPORTED_LLMS,
        help=(
            "DEPRECATED (use --llm-config): run every LLM leaf on this backend. Maps onto "
            "the shipped configs/llm/<backend>.yaml."
        ),
    )
    parser.add_argument(
        "--llm-command",
        help=(
            "DEPRECATED (use --llm-config `command:`): override backend command used by "
            "preflight and optional launch."
        ),
    )
    # NOTE (M-F): the `--generate-executor` flag and the `METDSL_GENERATE_EXECUTOR` env var were
    # removed when legacy generate execution was deleted — `pure` is the only executor. A cold run
    # that still passes `--generate-executor …` therefore fails at argparse ("unrecognized
    # arguments", SystemExit 2), not via a JSON envelope. The JSON fail-close (with reason
    # `generate_executor_legacy_removed`) is implemented only on the resume path, where a
    # legacy-recorded orchestration is the actual hazard.
    parser.add_argument(
        "--agent-model",
        default=None,
        help=(
            "DEPRECATED (use --llm-config `model:`). "
            "Model id (or unpinned alias) of the orchestration agent itself, recorded "
            "on its agent_runs row for cost attribution / reproducibility. Defaults to "
            "the operator's configured claude alias (e.g. 'opus') only for the claude "
            "backend running the unmodified default command; with a custom --llm-command "
            "(which may launch a different model) it is omitted unless given here. When "
            "omitted, repair-agent-runs backfills it from sibling rows on resume. Codex "
            "requires an explicit non-'codex' model slug on a fresh run; a resume recovers "
            "the recorded invocation.agent_model unless this option overrides it. Prefer an "
            "unpinned alias over a pinned version so it does not go stale."
        ),
    )
    parser.add_argument(
        "--with-deps",
        action="store_true",
        help=(
            "Before running the target, resolve its transitive dependency closure "
            "(deps.yaml + spec_catalog.yaml) and run each not-yet-ready dependency "
            "node's workflow bottom-up (dependency order), one orchestration per "
            "node. Dependency nodes run to Compile when the target ends at compile, "
            "else to Validate (matching compile / execution readiness). Already-ready "
            "dependencies are skipped. On --resume the closure is re-derived and "
            "continued automatically from the recorded invocation (no need to re-pass "
            "--with-deps)."
        ),
    )
    parser.add_argument(
        "--wait-usage-reset",
        action="store_true",
        help=(
            "Opt in to waiting out a leaf usage limit IN PLACE instead of fail-closing. "
            "Only takes effect when the dead leaf carried a MACHINE-FORM reset time (a "
            "trailing unix epoch); a human-worded reset is never guessed at. Bounded: one "
            "wait per substep, at most 6h, +120s margin. Default OFF (a usage limit stays "
            "terminal for a manual --resume after the reset). NOT recovered automatically on "
            "--resume — re-pass --wait-usage-reset to keep it active."
        ),
    )
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--orchestration-id", help="If omitted, generated automatically (or, with --resume, the latest orchestration).")
    parser.add_argument("--status", default="running", help="Initial orchestration status for init.")
    parser.set_defaults(run_conductor=True)
    parser.add_argument(
        "--run-conductor",
        dest="run_conductor",
        action="store_true",
        help="Run the deterministic conductor over the prepared orchestration (default: enabled).",
    )
    parser.add_argument(
        "--no-run-conductor",
        dest="run_conductor",
        action="store_false",
        help="Prepare orchestration artifacts only; do not run the conductor.",
    )
    parser.add_argument(
        "--invoke-llm",
        dest="run_conductor",
        action=_DeprecatedAliasAction,
        canonical="--run-conductor",
        store_value=True,
        help="Deprecated alias for --run-conductor.",
    )
    parser.add_argument(
        "--no-invoke-llm",
        dest="run_conductor",
        action=_DeprecatedAliasAction,
        canonical="--no-run-conductor",
        store_value=False,
        help="Deprecated alias for --no-run-conductor.",
    )
    parser.add_argument(
        "--stdout-format",
        choices=("human", "jsonl"),
        default="human",
        help=(
            "Stdout output format for the orchestration event stream. 'human' "
            "(default) renders the node/phase/substep events as compact human-"
            "readable lines so an operator can follow progress at a glance. "
            "'jsonl' emits the raw structured JSON payload of every event "
            "(suitable for piping into a parser). Regardless of this flag, the "
            "run_logs/ jsonl file under the orchestration directory always "
            "receives the full raw JSON payload of every event."
        ),
    )
    return parser.parse_args(argv)


def _resolve_existing_ref_path(repo_root: Path, ref: str, *, field_name: str) -> Path:
    path = Path(ref)
    resolved = path if path.is_absolute() else (repo_root / path)
    resolved = resolved.resolve()
    if not resolved.exists():
        raise ValueError(f"{field_name} must exist: {ref}")
    return resolved


def _validated_pycache_redirect_root(repo_root: Path) -> Path:
    """The host bytecode-cache redirect root (`<repo>/workspace/.pycache`), proven safe to trust.

    The host both WRITES bytecode here and, on later runs, LOADS it — so a cache root that is a
    symlink (or sits under one) is a code-execution vector: `.resolve()` follows it and the
    trusted, UNSANDBOXED host would import bytecode from an attacker-chosen location, either
    outside the repo (invisible to the terminal FS-diff) or inside a leaf-writable subtree (cache
    poisoning). The workspace validator does not catch this — `_scan_workspace_layout` tests
    `child.is_dir()`, which follows symlinks.

    Requiring resolution to be an IDENTITY rejects a symlink at any component and simultaneously
    proves the target stays inside repo_root (which the caller has already resolved). Raises
    ValueError so the caller can fail the run closed rather than silently trust the target.
    """
    root = repo_root / "workspace" / ".pycache"
    resolved = root.resolve()
    if resolved != root:
        raise ValueError(
            f"{root} must not be a symlink nor sit under one (it resolves to {resolved}); the "
            f"host writes AND loads bytecode there, so a redirected cache root is a "
            f"code-execution vector. Remove/replace it with a real directory and re-run."
        )
    if root.exists():
        if not root.is_dir():
            raise ValueError(f"{root} exists but is not a directory; remove it and re-run.")
        # Descendant symlinks are exactly as dangerous as a symlinked root: CPython follows a
        # symlinked directory in the mirrored source path when it writes AND when it LOADS a
        # cached module, so a link planted below the root (e.g. `.pycache/<mirror>/tools ->` a
        # leaf-writable pipeline dir) redirects trusted-host bytecode just the same — and because
        # this whole subtree is exempt from the terminal write-diff, the payload leaves no trace
        # there. Walk without following links (so a symlinked dir is reported, not descended).
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            for name in (*dirnames, *filenames):
                entry = Path(dirpath) / name
                if entry.is_symlink():
                    raise ValueError(
                        f"{entry} is a symlink inside the bytecode-cache redirect root {root}; "
                        f"the host loads bytecode from this subtree, so any symlink in it is a "
                        f"code-execution vector. Delete {root} and re-run."
                    )
    return resolved


def main(argv: list[str] | None = None) -> int:
    """Entry point. Thin wrapper that scopes the process-global `sys.pycache_prefix` redirect
    (installed by `_run_main` once repo_root is known) to THIS call.

    `main()` is also called IN-PROCESS — repeatedly, and often against temporary repo roots, by
    tools/tests/test_run_workflow.py, and potentially by an embedding caller. Leaking the redirect
    past the run would leave the caller's interpreter writing bytecode into that run's cache dir,
    which for a temporary root is deleted afterwards (later imports would silently recreate it).
    Restore the prior value unconditionally so the redirect lasts exactly as long as the run.
    """
    saved_pycache_prefix = sys.pycache_prefix
    try:
        # Owns the lifetime of any start claim `_run_main` takes and has to keep for
        # the whole invocation (the resume gate's, which must span its own liveness
        # decision and the run that follows). Closed on every exit path, before the
        # pycache restore below.
        with contextlib.ExitStack() as claims:
            return _run_main(argv, claims=claims)
    finally:
        sys.pycache_prefix = saved_pycache_prefix


def _run_main(
    argv: list[str] | None = None,
    *,
    claims: contextlib.ExitStack | None = None,
) -> int:
    args = _parse_args(argv)
    # Raw command line as invoked, for the reproduction record persisted to
    # orchestration_meta.json#invocation. Captured before any normalization so it
    # reflects exactly what the operator typed.
    raw_argv = list(argv) if argv is not None else list(sys.argv[1:])
    missing_tools = _check_required_cli_tools()
    if missing_tools:
        print(
            json.dumps(
                {
                    "status": "fail",
                    "reason": "missing_required_cli_tools",
                    "detail": f"missing tools: {','.join(missing_tools)}",
                    "missing": missing_tools,
                    "required": list(REQUIRED_CLI_TOOLS),
                    "docs_ref": "docs/RUNBOOK.md#0-1",
                },
                ensure_ascii=False,
            )
        )
        return 2
    if args.llm_config and args.llm:
        print(json.dumps({
            "status": "fail",
            "reason": "invalid_startup_input",
            "detail": (
                "--llm and --llm-config both name the leaf LLM; pass only one. --llm is the "
                "deprecated run-wide spelling and maps onto configs/llm/<backend>.yaml, so "
                "`--llm-config` alone can say everything it said."
            ),
        }, ensure_ascii=False))
        return 2
    # Deprecation is a warning, not a failure: the trio still works and still maps onto the
    # shipped configs. Removal is a later issue, and this is the notice that precedes it.
    for flag, value, replacement in (
        ("--llm", args.llm, "--llm-config configs/llm/<backend>.yaml"),
        ("--agent-model", args.agent_model, "the `model:` field of an --llm-config file"),
        ("--llm-command", args.llm_command, "the `command:` field of an --llm-config file"),
    ):
        if value:
            sys.stderr.write(
                f"warning: {flag} is deprecated and will be removed; use {replacement} "
                f"instead (see docs/ORCHESTRATION.md 'Leaf LLM configuration')\n")
    repo_root = Path(args.repo_root).resolve()

    # Redirect THIS host interpreter's bytecode cache out of the repo SOURCE tree, as early as
    # repo_root allows, so every module imported from here on (notably the conductor's
    # lazy build_runtime_server / tools.hooks.lint_evidence during compile.static / generate.gate)
    # compiles into workspace/.pycache/ instead of mcp_servers/__pycache__/ etc. Those in-repo
    # writes land in a child window's FS-diff and are misattributed as unauthorized_write_violation
    # — the defect this prevents. base_env's PYTHONDONTWRITEBYTECODE (set below) cannot do this
    # job: it governs SUBPROCESSES only, and sys.dont_write_bytecode is fixed at interpreter start.
    #
    # The prefix is a LITERAL on purpose: importing orchestration_runtime here to read its
    # _HOST_PYCACHE_REDIRECT_PREFIX would itself compile that ~20k-line module and write
    # tools/__pycache__/orchestration_runtime.*.pyc into the source tree BEFORE this redirect is
    # active (it is not yet in sys.modules at this point — validate_pipeline_semantics
    # deliberately does not import it, and _default_claude_agent_model runs much later). The
    # literal is drift-guarded against that constant by
    # test_orchestration_runtime.HostPycacheRedirectExemptionTest, the same test-pin technique
    # validate_workspace_root's allowlist entry uses to avoid the identical heavy import.
    # This attribute is process-global; `main()` (the wrapper above) saves and restores it so an
    # in-process caller does not inherit this run's redirect.
    #
    # Integrity-gated before use (see _validated_pycache_redirect_root): a symlinked cache root
    # would let the trusted host load bytecode from an attacker-chosen location.
    try:
        _pycache_resolved = _validated_pycache_redirect_root(repo_root)
    except ValueError as exc:
        print(
            json.dumps(
                {
                    "status": "fail",
                    "reason": "invalid_pycache_redirect_root",
                    "detail": str(exc),
                },
                ensure_ascii=False,
            )
        )
        return 2
    sys.pycache_prefix = str(_pycache_resolved)

    resume_mode = bool(args.resume)

    # Z2 generate-executor (M-F). The executor is no longer selectable: legacy execution was
    # removed, so `pure` is hardcoded. Both generate LLM substeps (the CodegenBundle producer and
    # the verdict reviewer) go through the pure path when `_pure_leaf_substep` matches (claude
    # backend ∧ M3c node); a non-M3c or codex node runs the shared agentic leaf loop as a recorded
    # residual, NOT as a selectable executor. The `--generate-executor` flag and
    # METDSL_GENERATE_EXECUTOR env were deleted; a cold run that still passes the flag fails at
    # argparse. On RESUME the recorded executor is recovered below and a non-`pure` record is
    # rejected fail-closed (`generate_executor_legacy_removed`) — legacy runs cannot be resumed.

    # Resolve effective startup inputs. With --resume, omitted spec_ref /
    # until_phase / --llm / --mode are recovered from the target orchestration's
    # existing artifacts (orchestration_meta.json + preflight.json + the start
    # prompt). Without --resume the defaults (claude / dev) apply. (`resume_mode` is
    # resolved above, before the executor block, which gates on it.)
    # Recovered resume metadata (populated in the resume branch). The reuse decision
    # in the try block compares the effective spec/backend against these to tell an
    # actual change from an explicit no-op restate.
    resume_recovered_spec_ref: str | None = None
    resume_recovered_dep_ref: str | None = None
    resume_recovered_llm: str | None = None
    resume_recovered_llm_command: str | None = None
    # The leaf-LLM configuration pin recorded on the resumed orchestration (empty dict on a
    # fresh run; all-empty fields on a run that predates issue #28).
    resume_recorded_llm_config: dict[str, Any] = {}
    # Closure-aware resume state (populated in the resume branch when the resumed
    # orchestration is a node of a `--with-deps` closure).
    resume_is_closure = False
    # Claims taken in the resume gate below and held for the rest of this
    # invocation; closed by the interpreter when `_run_main` returns.
    resume_claim = claims if claims is not None else contextlib.ExitStack()
    resume_closure_id: str | None = None
    if resume_mode:
        explicit_id = bool(args.orchestration_id)
        orchestration_id = args.orchestration_id or _find_latest_orchestration(repo_root)
        if not orchestration_id:
            print(
                json.dumps(
                    {
                        "status": "fail",
                        "reason": "no_resumable_orchestration",
                        "detail": "no orchestration found under workspace/orchestrations to resume",
                    },
                    ensure_ascii=False,
                )
            )
            return 2
        # A non-terminal target is ambiguous: it is either an active concurrent run
        # (whose orchestration_agent_run_id we would share, and whose
        # workspace/tmp/<arid> this resume's cleanup could delete) or the corpse of a
        # driver that died without terminalizing. `orchestration_meta.json#driver` is
        # what tells them apart, so probe it — for BOTH the implicit-latest and the
        # explicit-id path.
        # The claim is taken BEFORE the probe, not after it: the probe's `dead` verdict
        # authorizes a WRITE (`set-status fail/driver_crashed`) on someone else's
        # orchestration, and two resumes that both probed the same corpse would both
        # perform it — the second landing after the first had already reset the meta to
        # `running`, flipping an actively-resumed run back to `fail`. Refusing at the
        # claim later cannot undo that write, so the decision and the write have to sit
        # inside it. Held for the rest of this invocation, which is why `_run_node` is
        # told not to re-acquire it.
        if not resume_claim.enter_context(
            _exclusive_claim(repo_root, "orch", orchestration_id)
        ):
            print(
                json.dumps(
                    {
                        "status": "fail",
                        "reason": "concurrent_orchestration_running",
                        "detail": (
                            f"orchestration {orchestration_id} is already being driven "
                            "by another process in this repository. Wait for it to finish."
                        ),
                        "orchestration_id": orchestration_id,
                    },
                    ensure_ascii=False,
                )
            )
            return 2
        resume_meta = _read_orchestration_meta(repo_root, orchestration_id)
        resume_status = str(resume_meta.get("status") or "").strip().lower()
        if resume_status not in _RESUMABLE_TERMINAL_STATUSES:
            liveness = _probe_driver_liveness(resume_meta) if resume_meta else "unknown"
            resume_driver = (
                resume_meta.get("driver")
                if isinstance(resume_meta.get("driver"), dict)
                else {}
            )
            if liveness == "dead":
                # Proven corpse: terminalize it so the resume below enters
                # `terminal_reset` and the crash reconciliations actually run. The
                # status/meta is deliberately NOT re-read afterwards — the resume
                # proceeds on the strength of this call having succeeded.
                terminalize_error = _terminalize_dead_driver(
                    repo_root,
                    orchestration_id,
                    resume_meta,
                    stdout_format=args.stdout_format,
                )
                if terminalize_error is not None:
                    print(
                        json.dumps(
                            {
                                "status": "fail",
                                "reason": "dead_driver_terminalize_failed",
                                "detail": (
                                    f"orchestration {orchestration_id} has a dead driver but could "
                                    f"not be terminalized: {terminalize_error}"
                                ),
                                "orchestration_id": orchestration_id,
                            },
                            ensure_ascii=False,
                        )
                    )
                    return 2
            elif liveness == "alive":
                # An explicit id is normally the deliberate override for the
                # implicit-latest guard, but it cannot override physics: the run is
                # demonstrably still being driven.
                reason = (
                    "orchestration_driver_alive"
                    if explicit_id
                    else "latest_orchestration_not_resumable"
                )
                print(
                    json.dumps(
                        {
                            "status": "fail",
                            "reason": reason,
                            "detail": (
                                f"orchestration {orchestration_id} has non-terminal status "
                                f"'{resume_status or 'unknown'}' and its driver "
                                f"(pid {resume_driver.get('pid')}) is alive; resuming it would "
                                "collide with the live run. Wait for it to finish."
                            ),
                            "orchestration_id": orchestration_id,
                            "driver_pid": resume_driver.get("pid"),
                        },
                        ensure_ascii=False,
                    )
                )
                return 2
            elif not explicit_id:
                # Indeterminate liveness on the implicit path keeps the pre-existing
                # refusal: an unknown must never auto-attach to a possibly-live run.
                print(
                    json.dumps(
                        {
                            "status": "fail",
                            "reason": "latest_orchestration_not_resumable",
                            "detail": (
                                f"latest orchestration {orchestration_id} has non-terminal status "
                                f"'{resume_status or 'unknown'}'; pass --orchestration-id to resume a specific run"
                            ),
                            "orchestration_id": orchestration_id,
                        },
                        ensure_ascii=False,
                    )
                )
                return 2
            elif resume_meta:
                # Explicit id + indeterminate liveness: today's deliberate bypass, but
                # say so — the operator is resuming a run that may still be live, and
                # the crash reconciliations will NOT fire (status stays non-terminal).
                _emit_closure_event(
                    {
                        "status": "info",
                        "event": "resume_liveness_indeterminate",
                        "orchestration_id": orchestration_id,
                        "orchestration_status": resume_status or "unknown",
                    },
                    args.stdout_format,
                )
        recovered = _load_resume_params(repo_root, orchestration_id)
        spec_ref_arg = args.spec_ref
        until_phase_arg = args.until_phase
        # A lone positional is ambiguous on resume: argparse binds it to spec_ref,
        # but overriding until_phase (e.g. extending the run further) is the common
        # case while overriding spec_ref is not. If only spec_ref was given and it
        # names a known phase, treat it as the until_phase override instead.
        if spec_ref_arg and not until_phase_arg and spec_ref_arg.strip().lower() in PHASE_ALIASES:
            until_phase_arg = spec_ref_arg
            spec_ref_arg = None
        # Closure-aware resume: if the resumed orchestration is a node of a
        # `--with-deps` closure (recorded in orchestration_meta.json#invocation), the
        # whole closure is re-walked and driven to the TARGET spec — not just this one
        # node. Retarget spec_ref/until_phase to the closure target so the shared
        # startup validation below canonicalizes the target and discovers the target's
        # dependency ref. An explicit spec override (a non-phase positional) is the
        # escape hatch back to single-node resume of that spec.
        closure_id_recovered = recovered.get("closure_id")
        closure_target_recovered = recovered.get("closure_target_spec_ref")
        closure_until_recovered = recovered.get("closure_until_phase")
        # The closure end-phase lives authoritatively on the TARGET orchestration: its
        # start-prompt end-phase is rewritten by _run_node on every run/resume, so a
        # prior phase override survives there, whereas a DEPENDENCY node's copied
        # closure_until_phase goes stale. When we entered via a dependency (entry id !=
        # closure/target id) AND the target orchestration exists and belongs to this
        # closure (its own invocation.closure_id matches — guarding a reused
        # --orchestration-id that names an unrelated run), prefer the target's
        # recovered until_phase. When we entered via the target itself, its own
        # recovered value is already freshest (and must not override the partial-block
        # guard below).
        if closure_id_recovered and closure_id_recovered != orchestration_id:
            target_recovered = _load_resume_params(repo_root, closure_id_recovered)
            if (
                target_recovered.get("closure_id") == closure_id_recovered
                and target_recovered.get("until_phase")
            ):
                closure_until_recovered = target_recovered.get("until_phase")
        force_single_node = bool(spec_ref_arg)
        # All three closure fields are co-written by _build_invocation_record, so
        # require all three: if any is missing (corrupt/partial block), fall back to
        # single-node resume rather than driving the closure with a wrong until_phase
        # (the recovered dep until_phase, e.g. Compile, is NOT the target's).
        if (
            closure_id_recovered
            and closure_target_recovered
            and closure_until_recovered
            and not force_single_node
        ):
            resume_is_closure = True
            resume_closure_id = closure_id_recovered
            spec_ref_in = closure_target_recovered
            until_phase_in = until_phase_arg or closure_until_recovered
        else:
            spec_ref_in = spec_ref_arg or recovered.get("spec_ref")
            until_phase_in = until_phase_arg or recovered.get("until_phase")
        llm_in = args.llm or recovered.get("llm")
        mode_in = args.mode or recovered.get("mode")
        # A model slug belongs to its backend.  Do not pass (for example) a
        # recovered Claude model to a Codex resume after --llm switches the
        # backend; Codex then requires the operator to provide its own slug.
        agent_model_in = args.agent_model
        if not agent_model_in and llm_in == recovered.get("llm"):
            agent_model_in = recovered.get("agent_model")
        # Carry the recovered values; the reuse decision happens in the try block
        # below, keyed on whether the *effective* spec/backend actually changed
        # (not merely whether the arg was passed) — passing the same value
        # explicitly must still reuse the recovered dependency/command.
        resume_recovered_spec_ref = recovered.get("spec_ref")
        resume_recovered_dep_ref = recovered.get("source_dependency_ref")
        resume_recovered_llm = recovered.get("llm")
        resume_recovered_llm_command = recovered.get("llm_command")
        resume_recorded_llm_config = _recorded_llm_config(repo_root, orchestration_id)
        # Z2 executor fail-close on resume (M-F). Legacy generate execution was removed: `pure` is
        # the only executor. The recorded executor in the immutable invocation block is now used
        # solely to REJECT a resume that would otherwise silently switch execution + write-authority
        # models. Reject fail-closed unless the recovered value is exactly `pure`:
        #   - `legacy`  → the run genuinely used the deleted legacy leaves; there is no legacy path
        #     to resume onto, and silently promoting it to pure would change the execution model of
        #     an in-flight run.
        #   - None      → the orchestration predates the `generate_executor` field (a pre-adoption
        #     legacy run); same hazard.
        #   - garbage (e.g. "pur") → an unknown value must never be read as pure; fail loud.
        # This gate validates the ENTRY orchestration. A closure resume additionally validates
        # every warm-resumed member inside `_run_with_dependency_closure` (a mixed closure — e.g. a
        # reused closure id pairing a `pure` entry with a `legacy` member — must not slip a legacy
        # member past this entry check). A pure-recorded run resumes normally. A non-M3c/codex run
        # is ALSO recorded `pure` (the executor is a provenance stamp, not the leaf-mode decision),
        # so it is NOT rejected — it simply runs its agentic leaves as before.
        entry_executor_rejection = _generate_executor_resume_rejection(
            orchestration_id, recovered.get("generate_executor")
        )
        if entry_executor_rejection is not None:
            print(json.dumps(entry_executor_rejection, ensure_ascii=False))
            return 2
        missing = [
            name
            for name, value, ok in (
                ("spec_ref", spec_ref_in, bool(spec_ref_in)),
                ("until_phase", until_phase_in, bool(until_phase_in)),
                ("llm", llm_in, llm_in in SUPPORTED_LLMS),
                ("mode", mode_in, bool(mode_in)),
            )
            if not ok
        ]
        if missing:
            print(
                json.dumps(
                    {
                        "status": "fail",
                        "reason": "resume_params_unrecoverable",
                        "detail": (
                            f"could not recover {', '.join(missing)} for orchestration "
                            f"{orchestration_id}; pass them explicitly"
                        ),
                        "orchestration_id": orchestration_id,
                    },
                    ensure_ascii=False,
                )
            )
            return 2
    else:
        orchestration_id = args.orchestration_id or _new_orchestration_id()
        spec_ref_in = args.spec_ref
        until_phase_in = args.until_phase
        llm_in = args.llm or DEFAULT_LLM
        mode_in = args.mode or DEFAULT_WORKFLOW_MODE
        agent_model_in = args.agent_model

    # Resume restores the model that the original Codex invocation pinned; an
    # explicit flag is the sole override.  Make every downstream init/launch
    # consumer use this effective value rather than the raw argparse field. The raw one is
    # kept: it is the only way to tell "the operator passed this" from "we recovered it".
    raw_agent_model = args.agent_model
    args.agent_model = agent_model_in

    try:
        workflow_mode = _normalize_workflow_mode(mode_in)
        if not until_phase_in:
            raise ValueError(
                "until_phase is required unless --resume is set; "
                f"choose one of: {', '.join(PHASE_ORDER)}"
            )
        until_phase = _normalize_phase(until_phase_in)
        llm = llm_in
        # The conductor only has a leaf launcher for claude/codex; reject an
        # unsupported backend up front instead of failing at the first substep
        # after init/preflight already created the orchestration.
        if llm not in ("claude", "codex"):
            raise ValueError(
                f"conductor orchestration supports --llm claude|codex, not {llm!r}"
            )
        # `config_pinned` — not `args.llm_config` — is what says "the model lives in a file".
        # A resume of a config-pinned run passes no --llm-config (it recovers the pin), so
        # testing the flag alone made every such codex run unresumable: the guard demanded an
        # --agent-model for a model the file already names.
        config_pinned = bool(args.llm_config) or bool(
            (resume_recorded_llm_config if resume_mode else {}).get("path"))
        if not config_pinned and llm == "codex" and (
            not isinstance(agent_model_in, str)
            or not agent_model_in.strip()
            or agent_model_in.strip().lower() == "codex"
        ):
            raise ValueError(
                "Codex workflow execution requires --agent-model with an explicit model slug; "
                "for --resume, the original invocation.agent_model must be present or pass "
                "--agent-model explicitly"
            )
        # Reuse the recovered agent command unless --llm-command was given or the
        # backend actually changed; restating the same --llm must keep the command.
        # BOTH this rule and the model-belongs-to-its-backend rule above are LEGACY-branch
        # rules: they reconstruct what the deprecated flag trio meant. A run pinned to an
        # --llm-config file recovers the file itself instead (below), which already says who
        # runs what.
        if args.llm_command:
            llm_command = args.llm_command
        elif resume_recovered_llm_command and llm == resume_recovered_llm:
            llm_command = resume_recovered_llm_command
        else:
            llm_command = DEFAULT_LLM_COMMANDS[llm]

        # --- the leaf-LLM configuration (issue #28) --------------------------------------
        # Three ways in, in priority order, and they converge on ONE object:
        #   1. --llm-config PATH               — the operator named a file.
        #   2. a resume whose orchestration recorded a config pin — the SAME file, re-hashed
        #      and refused if it moved or changed (`_llm_config_resume_rejection`).
        #   3. the deprecated trio             — mapped onto configs/llm/<backend>.yaml with
        #      the run-wide model/command applied to `defaults`, which is what makes
        #      `--llm claude` and `--llm-config configs/llm/claude.yaml` the same run.
        recorded_pin = resume_recorded_llm_config if resume_mode else {}
        if recorded_pin.get("path"):
            # The recorded pin WINS on a resume, including over an explicitly passed
            # --llm-config: the finished phases ran on the recorded file, and this is a
            # continuation of that run, not a new one. Say so rather than dropping the flag in
            # silence — this notice is the ONLY signal, since the effective path is then always
            # the recorded one and the gate below can only ever compare it against itself. (The
            # gate's path-mismatch arm stays reachable from the closure gates, which compare a
            # member's recorded pin against the closure's effective configuration.)
            # `raw_agent_model`, not `args.agent_model`: the latter has already been
            # overwritten with the value recovered from the record, so reading it here
            # announced a flag the operator never passed.
            for flag, value in (("--llm-config", args.llm_config), ("--llm", args.llm),
                                ("--agent-model", raw_agent_model),
                                ("--llm-command", args.llm_command)):
                if value:
                    sys.stderr.write(
                        f"warning: {flag} is ignored on --resume; orchestration "
                        f"{orchestration_id} is pinned to its recorded leaf-LLM configuration "
                        f"{recorded_pin['path']!r}. Start a fresh run to change it.\n")
            llm_config_path = repo_root / str(recorded_pin["path"])
            llm_config_overrides = dict(recorded_pin.get("overrides") or {})
            # Compare BEFORE loading: a pinned file that has been deleted must surface as
            # `llm_config_changed_since_launch` ("restore that file"), not as a generic
            # `llm_config_unreadable` from the loader.
            rejection = _llm_config_resume_rejection(
                orchestration_id, recorded_pin, repo_root=repo_root,
                effective_path=str(recorded_pin["path"]),
                effective_sha256=config_sha256(llm_config_path),
                effective_overrides=llm_config_overrides)
            if rejection is not None:
                print(json.dumps(rejection, ensure_ascii=False))
                return 2
        elif args.llm_config:
            # Relative to `--repo-root`, like every other path this driver resolves — resolving
            # against the process CWD would run one file and record a spelling naming another.
            llm_config_path = Path(args.llm_config)
            if not llm_config_path.is_absolute():
                llm_config_path = repo_root / llm_config_path
            llm_config_overrides = {
                k: v for k, v in (("model", (args.agent_model or "").strip()),
                                  ("command", (args.llm_command or "").strip())) if v
            }
        else:
            llm_config_path = shipped_config_path(llm, repo_root)
            # `llm_command` always has a value — `DEFAULT_LLM_COMMANDS[llm]` is the bare binary
            # name — but that is the default, not an OVERRIDE. Recording it as one would make
            # `--llm claude` and `--llm-config configs/llm/claude.yaml` resolve to configs that
            # differ in a field (`command: "claude"` vs `command: ""`) that means the same thing,
            # which is the equivalence this whole mapping exists to preserve.
            llm_config_overrides = {
                k: v for k, v in (
                    ("model", (agent_model_in or "").strip()),
                    ("command", "" if llm_command.strip() == DEFAULT_LLM_COMMANDS.get(llm)
                     else llm_command.strip()),
                ) if v
            }
        _loaded_llm_config = load_llm_config(llm_config_path)
        llm_config = apply_defaults_overrides(
            _loaded_llm_config,
            model=llm_config_overrides.get("model", ""),
            command=llm_config_overrides.get("command", ""))
        # A run-wide override reaches `defaults` and everything that inherited from it, and
        # deliberately leaves a value the FILE declared for a specific leaf alone. That rule is
        # right, and it makes the deprecated flag a NO-OP against a configuration that declares
        # every leaf — which the shipped ones now do. Say so: an operator who passes
        # `--agent-model` and gets the file's model would otherwise have no way to tell.
        # `model_declared` is cleared by an override that lands, so what survives it is
        # exactly a per-leaf declaration; `command` has no such marker and is read from the
        # entry's own declared set (which excludes `defaults`).
        _kept_by = {
            "model": lambda entry: entry.model_declared,
            "command": lambda entry: "command" in entry.declared,
        }
        for flag, field in (("--agent-model", "model"), ("--llm-command", "command")):
            if not llm_config_overrides.get(field):
                continue
            kept = sorted(f"{phase}.{substep}"
                          for (phase, substep), entry in llm_config.entries.items()
                          if _kept_by[field](entry))
            if kept:
                sys.stderr.write(
                    f"warning: {flag} does not change {', '.join(kept)} — "
                    f"{llm_config_path} declares a {field} for {'them' if len(kept) > 1 else 'it'} "
                    f"explicitly, and a per-leaf value is not overridden run-wide. Edit the "
                    f"configuration to change {'those' if len(kept) > 1 else 'that'} leaf/leaves.\n")
        llm_config.validate_runnable()
        # Downstream (preflight, the recorded invocation, the closure driver) still speaks the
        # single-backend vocabulary; derive it FROM the config so there is one authority.
        llm = llm_config.defaults.backend_token
        llm_command = llm_config.defaults.command or llm
        if not spec_ref_in:
            raise ValueError("spec_ref is required unless --resume is set")
        spec_ref = _canonicalize_spec_ref(repo_root, spec_ref_in)
        # Reuse the recovered dependency ref when the spec is unchanged (compared
        # canonically, so restating the same spec still counts as unchanged).
        # Format-validate only — no existence check — so resume stays stable even if
        # the dependency file moved/was renamed after the original run. A genuine
        # spec change rediscovers the dependency next to the new spec.
        if resume_recovered_dep_ref and spec_ref == resume_recovered_spec_ref:
            source_dependency_ref = _validate_source_dependency_ref(resume_recovered_dep_ref)
        else:
            source_dependency_ref = _discover_source_dependency_ref(repo_root, spec_ref)
    except (ValueError, LlmConfigError) as exc:
        # LlmConfigError IS a ValueError, and is named anyway: its `rule` is the operator's
        # search key and the class is what makes that intent legible here.
        print(
            json.dumps(
                {
                    "status": "fail",
                    "reason": "invalid_startup_input",
                    "detail": str(exc),
                },
                ensure_ascii=False,
            )
        )
        return 2

    # Fail-close a resume whose leaf-LLM configuration is no longer the one the run launched
    # with. Placed AFTER the startup try/except (the effective config must exist to compare)
    # and BEFORE any orchestration state is touched. The closure driver applies the same gate
    # to every member it warm-resumes.
    if resume_mode:
        rejection = _llm_config_resume_rejection(
            orchestration_id, resume_recorded_llm_config, repo_root=repo_root,
            effective_path=_repo_relative(llm_config.path, repo_root),
            effective_sha256=llm_config.sha256,
            effective_overrides=llm_config_overrides)
        if rejection is not None:
            print(json.dumps(rejection, ensure_ascii=False))
            return 2

    # Startup assertion: validate_pipeline_semantics now fail-closes when the
    # active repo_root's `spec/schema/ir/shape_expr.schema.json` is missing,
    # malformed, contains an invalid regex, or fails the structural classifier.
    # We must surface ALL of those failure modes here BEFORE any orchestration
    # state mutation (init/preflight/launches/...), otherwise the run would
    # create `workspace/tmp/<arid>/` and orchestration_meta.json only to
    # collapse later with `schema_load_failed` mid-phase, leaving partially
    # initialized state to clean up.
    #
    # Reuse the validator's actual schema loader so the check exercises the
    # same code path as the gate it is guarding — `is_file()` alone would
    # miss malformed JSON, invalid regex, and structural-classifier failures.
    required_schema = repo_root / "spec" / "schema" / "ir" / "shape_expr.schema.json"
    try:
        from tools.validate_pipeline_semantics import (
            _get_shape_expr_patterns,
            _load_shape_expr_patterns_cached,
        )
        _load_shape_expr_patterns_cached.cache_clear()
        _get_shape_expr_patterns(repo_root=repo_root)
    except (RuntimeError, ModuleNotFoundError) as exc:
        try:
            missing_path_rel = str(required_schema.relative_to(repo_root))
        except ValueError:
            missing_path_rel = str(required_schema)
        print(
            json.dumps(
                {
                    "status": "fail",
                    "reason": "missing_canonical_schema",
                    "detail": (
                        f"canonical schema invalid or missing: {missing_path_rel}. "
                        f"{exc}"
                    ),
                    "missing_path": missing_path_rel,
                },
                ensure_ascii=False,
            )
        )
        return 2

    # Base env shared by every node. METDSL_ORCHESTRATION_ID / TMPDIR /
    # ORCHESTRATION_AGENT_RUN_ID are per-node and set inside _run_node so a
    # dependency-closure run (one orchestration per node) never leaks the
    # previous node's ids/tmp into the next.
    base_env = dict(os.environ)
    base_env["METDSL_WORKFLOW_MODE"] = "1"
    base_env["METDSL_WORKFLOW_EXEC_MODE"] = workflow_mode
    base_env["METDSL_MISSING_ORCHESTRATION_ID_POLICY"] = "strict"
    # Warm-resume minor-fix repairs are ALWAYS active (claude only; no env gate): a
    # generate.gate / compile.static finding (and the build->generate reuse
    # repairs) re-run the phase's producer substep (generate.generate / compile.generate) by
    # resuming the prior leaf's session with context intact, instead of a cold restart —
    # avoiding the cold-start re-read cost. `restart` repairs stay cold (anchoring avoidance).
    # The conductor falls back to a cold launch if the producer session transcript is gone.
    base_env["PYTHONPATH"] = str(repo_root) + (
        f":{base_env['PYTHONPATH']}" if base_env.get("PYTHONPATH") else ""
    )
    # Prevent Python from writing *.pyc / __pycache__ bytecode under tools/.
    # Without this, any `python3 tools/orchestration_runtime.py` call made by
    # the orchestration agent (or child subprocesses) generates
    # tools/__pycache__/orchestration_runtime.cpython-<ver>.pyc, which is not
    # in any agent's output_manifest and triggers unauthorized_write_violation
    # at record-agent-run terminal validation.  Setting this in the shared env
    # dict ensures it propagates to: (a) _runtime_command() subprocesses,
    # (b) the orchestration agent launch subprocess, and (c) any grandchild
    # `python3 tools/...` invocations the agent makes.
    base_env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    # NOTE: this only covers SUBPROCESSES. The IN-PROCESS conductor host's own bytecode is kept
    # out of the repo source tree by the `sys.pycache_prefix` redirect installed near the top of
    # main() (see the comment there for why it must run that early and why it uses a literal).

    # Closure-aware resume: the resumed orchestration is a node of a `--with-deps`
    # closure, so re-derive the closure and drive it to the TARGET (spec_ref is the
    # closure target here). Prior node orchestrations are resumed; not-yet-run nodes
    # run fresh; already-ready nodes are skipped.
    if resume_mode and resume_is_closure and resume_closure_id:
        prior_map = _index_closure_orchestrations(repo_root, resume_closure_id)
        return _run_with_dependency_closure(
            preclaimed_orchestration_id=orchestration_id,
            repo_root=repo_root,
            base_env=base_env,
            target_orchestration_id=resume_closure_id,
            target_spec_ref=spec_ref,
            target_source_dependency_ref=source_dependency_ref,
            until_phase=until_phase,
            llm=llm,
            llm_command=llm_command,
            llm_config=llm_config,
            llm_config_overrides=llm_config_overrides,
            workflow_mode=workflow_mode,
            agent_model=args.agent_model,
            status=args.status,
            run_conductor=args.run_conductor,
            wait_usage_reset=args.wait_usage_reset,
            stdout_format=args.stdout_format,
            resume=True,
            prior_orch_by_spec=prior_map,
            raw_argv=raw_argv,
        )

    # `--with-deps` runs the target's transitive dependency closure bottom-up
    # (one orchestration per node) before the target. Scoped to fresh runs: a
    # `--resume` of a `--with-deps` run is handled by the closure-aware branch above.
    if getattr(args, "with_deps", False) and not resume_mode:
        return _run_with_dependency_closure(
            repo_root=repo_root,
            base_env=base_env,
            target_orchestration_id=orchestration_id,
            target_spec_ref=spec_ref,
            target_source_dependency_ref=source_dependency_ref,
            until_phase=until_phase,
            llm=llm,
            llm_command=llm_command,
            llm_config=llm_config,
            llm_config_overrides=llm_config_overrides,
            workflow_mode=workflow_mode,
            agent_model=args.agent_model,
            status=args.status,
            run_conductor=args.run_conductor,
            wait_usage_reset=args.wait_usage_reset,
            stdout_format=args.stdout_format,
            resume=False,
            prior_orch_by_spec=None,
            raw_argv=raw_argv,
        )

    # Cold-start guard (single node): a fresh run of a spec that still has a
    # non-terminal orchestration either collides with a live driver (refuse) or
    # silently discards a resumable checkpoint (warn, naming the resume command).
    # The claim is held across BOTH the guard and the run: the orchestration the guard
    # looks for is not written until `init` inside `_run_node`, so without it two runs
    # started together would both scan clean.
    cold_start_claim = contextlib.ExitStack()
    with cold_start_claim:
        if not resume_mode:
            if not cold_start_claim.enter_context(
                _exclusive_claim(repo_root, "spec", spec_ref)
            ):
                _emit_closure_event(
                    _concurrent_cold_start_envelope(spec_ref), args.stdout_format)
                return 2
            cold_conflict = _cold_start_running_guard(
                repo_root,
                spec_ref,
                stdout_format=args.stdout_format,
                driver_identity=_current_driver_identity(),
            )
            if cold_conflict is not None:
                _emit_closure_event(cold_conflict, args.stdout_format)
                return 2

        # Plain single node. A cold run records the reproduction block (no closure); a
        # single-node resume passes None (the runtime preserves the existing block).
        single_node_invocation = (
            None
            if resume_mode
            else _build_invocation_record(
                argv=raw_argv,
                spec_ref=spec_ref,
                until_phase=until_phase,
                llm=llm,
                llm_command=llm_command,
                llm_config=llm_config,
                llm_config_overrides=llm_config_overrides,
                repo_root=repo_root,
                workflow_mode=workflow_mode,
                agent_model=args.agent_model,
                with_deps=False,
                wait_usage_reset=args.wait_usage_reset,
            )
        )
        return _run_node(
            repo_root=repo_root,
            base_env=base_env,
            orchestration_id=orchestration_id,
            spec_ref=spec_ref,
            source_dependency_ref=source_dependency_ref,
            until_phase=until_phase,
            llm=llm,
            llm_command=llm_command,
            llm_config=llm_config,
            workflow_mode=workflow_mode,
            agent_model=args.agent_model,
            status=args.status,
            run_conductor=args.run_conductor,
            resume_mode=resume_mode,
            wait_usage_reset=args.wait_usage_reset,
            invocation=single_node_invocation,
            stdout_format=args.stdout_format,
            # Cold: main holds the spec claim across the guard above. Resume: main
            # holds the orchestration claim across the liveness gate. Either way
            # `_run_node` must not re-acquire what this process already has.
            spec_claim_held=not resume_mode,
            orch_claim_held=resume_mode,
        )


def _format_event_human(payload: dict[str, Any]) -> str | None:
    """Render a structured event payload as a compact human-readable line.

    The event vocabulary is small and stable: the node/dependency announcements
    written here by run_workflow.py, the conductor's `phase_start` /
    `phase_complete` / `substep_start` / `substep_complete` / warn emits, and
    the run's final `status: ok` / `status: fail` summary. An unknown payload
    shape returns None so the caller can fall back to the raw JSON — the
    human-mode formatter is best-effort presentation and must not swallow
    information it cannot classify.

    Indentation conveys nesting: node = column 0, phase = 2 spaces, substep /
    warn = 4 spaces. Pass results are tagged `ok`; non-pass results carry the
    raw verdict text (`fail`, `fail_closed`, `blocked`, ...) so the operator
    sees the actual classification rather than a uniform red flag.
    """
    status = payload.get("status")
    event = payload.get("event")

    if status == "info" and event == "node_start":
        spec = payload.get("spec_ref", "?")
        until = payload.get("until_phase", "?")
        orch = payload.get("orchestration_id", "?")
        flag = " [resume]" if payload.get("resume") else ""
        return f"[node] spec={spec} until={until} orch={orch}{flag}"

    if status == "info" and event == "dependency_node_begin":
        node = payload.get("node", "?")
        spec = payload.get("spec_ref", "?")
        until = payload.get("until_phase", "?")
        orch = payload.get("orchestration_id", "?")
        return f"[dep ] node={node} spec={spec} until={until} orch={orch}"

    if status == "info" and event == "phase_start":
        phase = payload.get("phase", "?")
        attempt = payload.get("attempt", 1)
        return f"  [phase   ] {phase} (attempt {attempt})"

    if status == "info" and event == "phase_complete":
        phase = payload.get("phase", "?")
        result = payload.get("result", "?")
        if result == "skipped":
            return f"  [phase   ] {phase} skipped (resumed)"
        marker = "ok" if result == "pass" else result
        elapsed = payload.get("elapsed_seconds")
        suffix = f" ({elapsed}s)" if elapsed is not None else ""
        return f"  [phase   ] {phase} {marker}{suffix}"

    if status == "info" and event == "substep_start":
        phase = payload.get("phase", "?")
        substep = payload.get("substep") or "step"
        return f"    [substep] {phase}.{substep} ..."

    if status == "info" and event == "substep_complete":
        phase = payload.get("phase", "?")
        substep = payload.get("substep") or "step"
        result = payload.get("result", "?")
        marker = "ok" if result == "pass" else result
        elapsed = payload.get("elapsed_seconds")
        suffix = f" ({elapsed}s)" if elapsed is not None else ""
        arid = payload.get("agent_run_id")
        arid_suffix = f" arid={arid}" if arid and result != "pass" else ""
        return f"    [substep] {phase}.{substep} {marker}{suffix}{arid_suffix}"

    if status == "info" and event == "resume_session_unavailable":
        phase = payload.get("phase", "?")
        substep = payload.get("substep") or "?"
        target = payload.get("target", "?")
        return f"    [warn   ] resume session unavailable: {phase}.{substep} target={target}"

    if status == "info" and event == "leaf_transient_retry":
        phase = payload.get("step", "?")
        substep = payload.get("substep") or "step"
        tag = payload.get("tag", "?")
        attempt = payload.get("attempt", "?")
        total = payload.get("max_attempts", "?")
        backoff = payload.get("backoff_seconds", "?")
        return (f"    [warn   ] transient leaf failure ({tag}) in {phase}.{substep} "
                f"[attempt {attempt}/{total}]: retrying in {backoff}s")

    if status == "info" and event == "leaf_timeout":
        # The one event that reports "your leaf was killed after N hours". Rendered like its
        # siblings rather than falling through to the raw-JSON line: it is the most consequential
        # leaf-lifecycle event and the operator reads it at the moment a phase fails closed.
        phase = payload.get("step", "?")
        substep = payload.get("substep") or "step"
        elapsed = payload.get("elapsed_seconds", "?")
        cap = payload.get("timeout_seconds", "?")
        return (f"    [warn   ] leaf timeout in {phase}.{substep}: no answer after {elapsed}s "
                f"(cap {cap}s, METDSL_LEAF_TIMEOUT_SECONDS) — process group killed, "
                f"phase fails closed")

    if status == "info" and event == "leaf_usage_limit_wait":
        phase = payload.get("step", "?")
        substep = payload.get("substep") or "step"
        wait = payload.get("wait_seconds", "?")
        attempt = payload.get("wait_attempt", "?")
        # Name the source: a host-side `/usage` probe observed the reset (and which window), while a
        # scraped instant was read out of the dead leaf's own output and can be wrong about the
        # window. The operator deciding whether to let a multi-hour park stand needs that distinction
        # without grepping the raw event stream.
        source = payload.get("reset_source")
        window = payload.get("window")
        origin = f" (source={source}{f'/{window}' if window else ''})" if source else ""
        return (f"    [warn   ] usage limit in {phase}.{substep} [wait {attempt}]{origin}: "
                f"waiting {wait}s for the reset, then re-launching")

    if status == "info" and event == "transport_substep_resume":
        step = payload.get("step", "?")
        substep = payload.get("resume_substep", "?")
        producer = payload.get("producer_arid", "?")
        artifact = payload.get("artifact_id", "?")
        return (f"    [resume ] {step} resumes at {substep} — producer {producer} / "
                f"{artifact} reused")

    if status == "info" and event == "substep_resumed":
        phase = payload.get("phase", "?")
        substep = payload.get("substep") or "step"
        return f"    [substep] {phase}.{substep} reused (resumed)"

    if status == "info" and event == "transport_resume_declined":
        reason = payload.get("reason", "?")
        return (f"    [warn   ] transport substep resume declined: {reason} "
                f"— full phase re-run")

    if status == "info" and event == "prior_incomplete_orchestration":
        orch = payload.get("orchestration_id", "?")
        liveness = payload.get("liveness", "?")
        cmd = payload.get("resume_command", "?")
        return (f"    [warn   ] prior incomplete orchestration {orch} "
                f"(driver {liveness}) — this cold run starts over; to continue it: {cmd}")

    if status == "info" and event == "dead_driver_terminalized":
        orch = payload.get("orchestration_id", "?")
        pid = payload.get("driver_pid", "?")
        prior = payload.get("prior_status", "?")
        return (f"    [warn   ] driver of {orch} (pid {pid}) is gone while '{prior}' "
                f"— terminalized as fail/driver_crashed, resuming from its checkpoint")

    if status == "info" and event == "resume_liveness_indeterminate":
        orch = payload.get("orchestration_id", "?")
        st = payload.get("orchestration_status", "?")
        return (f"    [warn   ] {orch} is '{st}' and its driver liveness is unknown "
                f"— resuming anyway (crash reconciliations will not run)")

    if status == "info" and event == "driver_interrupted":
        orch = payload.get("orchestration_id", "?")
        cmd = payload.get("resume_command", "?")
        return (f"    [warn   ] driver interrupted — {orch} terminalized as "
                f"cancel/driver_interrupted; resume with: {cmd}")

    if status == "info" and event == "diagnose_launch_failed":
        phase = payload.get("phase", "?")
        err = payload.get("error", "")
        return f"    [warn   ] diagnose launch failed in {phase}: {err}"

    if status == "ok":
        orch = payload.get("orchestration_id", "?")
        ws = payload.get("workflow_status") or "ok"
        invoked = payload.get("llm_invoked")
        suffix = "" if invoked is None else ("" if invoked else " (no-launch)")
        deps = payload.get("dependency_runs")
        dep_suffix = f" deps={len(deps)}" if isinstance(deps, list) and deps else ""
        return f"[ok  ] orch={orch} workflow_status={ws}{suffix}{dep_suffix}"

    if status == "fail":
        orch = payload.get("orchestration_id")
        reason = payload.get("reason", "?")
        detail = payload.get("detail")
        parts = [f"reason={reason}"]
        if orch:
            parts.append(f"orch={orch}")
        if detail:
            d = str(detail).replace("\n", " ").strip()
            if len(d) > 240:
                d = d[:240] + "..."
            parts.append(f"detail={d}")
        return "[FAIL] " + " ".join(parts)

    return None


def _emit_closure_event(payload: dict[str, Any], stdout_format: str) -> None:
    """Print a dependency-closure-level event, honoring `--stdout-format`.

    The closure driver (`_run_with_dependency_closure`) emits its own events
    (`dependency_node_begin` and the various closure failure summaries) OUTSIDE
    any `_run_node` call, so no `_StdoutTee` is installed to translate them. In
    `human` mode this would leak raw JSON; route the payload through the same
    `_format_event_human` renderer the tee uses, falling back to the raw JSON
    line when the event shape is unknown so no information is dropped.
    """
    line = json.dumps(payload, ensure_ascii=False)
    if stdout_format == "human":
        human = _format_event_human(payload)
        if human is not None:
            line = human
    print(line, flush=True)


class _StdoutTee:
    """Mirror writes to a run-log file while optionally rendering JSON event
    lines to the real terminal in a compact human-readable form.

    Installed for the duration of a node run so the workflow event stream
    (``node_start``, the conductor's ``phase_start`` / ``phase_complete`` /
    ``substep_start`` / ``substep_complete`` emits, and the final ok/fail
    summary) is uniformly persisted to the workspace and is presented to the
    operator in the mode they asked for.

    The ``mode`` parameter governs the terminal stream:
    - ``"jsonl"`` (legacy default): every byte is passed through to the wrapped
      terminal stream unchanged, identical to the pre-format-aware tee.
    - ``"human"``: each completed stdout line is buffered, parsed as JSON, and
      — if it matches a known event shape — rendered as a compact human-
      readable line on the terminal. Lines that don't parse / don't match are
      forwarded verbatim so the operator never loses output.

    Run-log writes are mode-independent: the file ALWAYS receives the original
    raw bytes (which, for the workflow event stream, is the full structured
    JSON payload of every event). This means ``run_logs/run_*.jsonl`` is a
    full-fidelity record regardless of ``--stdout-format``.

    Writes to the log file are best-effort: a log-file IO error must never
    break the run or swallow terminal output, so file errors are silently
    ignored. Attribute access falls through to the wrapped stream so the
    object remains a drop-in ``sys.stdout`` (e.g. subprocesses derive
    ``fileno()`` from the parent fd via this fall-through).
    """

    def __init__(self, stream: Any, log_file: Any, mode: str = "jsonl") -> None:
        self._stream = stream
        self._log = log_file
        self._mode = mode if mode in ("human", "jsonl") else "jsonl"
        # Buffer of bytes received but not yet terminated by a newline; only
        # used in human mode (jsonl mode pipes straight through).
        self._buffer = ""

    def write(self, data: str) -> int:
        # The run-log mirrors the inbound bytes verbatim, before any human-mode
        # rewriting — so the workspace record stays canonical even when the
        # operator picked the compact terminal format.
        try:
            self._log.write(data)
        except Exception:  # noqa: BLE001 - never let log IO break the run
            pass
        if self._mode != "human":
            return self._stream.write(data)
        self._buffer += data
        while True:
            nl = self._buffer.find("\n")
            if nl == -1:
                break
            line = self._buffer[:nl]
            self._buffer = self._buffer[nl + 1:]
            self._stream.write(self._render_line(line) + "\n")
        return len(data)

    def _render_line(self, line: str) -> str:
        stripped = line.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                human = _format_event_human(payload)
                if human is not None:
                    return human
        return line

    def flush(self) -> None:
        # In human mode a trailing partial line (no newline yet) is held in the
        # buffer; flush forwards it through the formatter so an operator sees
        # the tail promptly. The run-log already saw it on the inbound write().
        if self._mode == "human" and self._buffer:
            try:
                self._stream.write(self._render_line(self._buffer))
            except Exception:  # noqa: BLE001
                pass
            self._buffer = ""
        self._stream.flush()
        try:
            self._log.flush()
        except Exception:  # noqa: BLE001
            pass

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def _open_run_log(repo_root: Path, orchestration_id: str) -> Any:
    """Open a fresh timestamped run-log file under the orchestration dir.

    The name is `run_<UTC timestamp>_<uuid8>.jsonl` so repeated runs against the
    same orchestration_id (notably `--resume`) never collide. The `run_logs/`
    prefix is exempt from the runtime write-snapshot
    (`_should_ignore_runtime_snapshot_path`), so this host-side write never
    contaminates a leaf's terminal write-diff. Returns the open file object, or
    None if it could not be created (logging is best-effort)."""
    try:
        run_logs_dir = (
            repo_root / "workspace" / "orchestrations" / orchestration_id / "run_logs"
        )
        run_logs_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = run_logs_dir / f"run_{stamp}_{uuid.uuid4().hex[:8]}.jsonl"
        return path.open("w", encoding="utf-8")
    except Exception:  # noqa: BLE001 - logging must never break the run
        return None


def _run_node(
    *,
    repo_root: Path,
    base_env: dict[str, str],
    orchestration_id: str,
    spec_ref: str,
    source_dependency_ref: str,
    until_phase: str,
    llm: str,
    llm_command: str,
    # The leaf-model authority. None means "derive it from the deprecated trio above", which is
    # the same mapping `main` applies — one derivation, not a second source. The deprecated-flag
    # OVERRIDES are not threaded here: they are already applied to `llm_config`, and their
    # literals are recorded by `_build_invocation_record`, which the caller builds.
    llm_config: LlmConfig | None = None,
    workflow_mode: str = DEFAULT_WORKFLOW_MODE,
    agent_model: str | None = None,
    status: str = "",
    run_conductor: bool = True,
    resume_mode: bool = False,
    wait_usage_reset: bool = False,
    invocation: dict[str, Any] | None = None,
    closure_until_phase: str | None = None,
    extra_output: dict[str, Any] | None = None,
    stdout_format: str = "jsonl",
    spec_claim_held: bool = False,
    orch_claim_held: bool = False,
) -> int:
    """Run a single node's orchestration (init → preflight → prompt → launch →
    terminalize) and print its JSON result. Returns the process exit code
    (0 = ok). Each call uses its own orchestration_id / TMPDIR so the
    dependency-closure driver can run one orchestration per node without
    cross-node env/tmp leakage. `extra_output`, when given, is merged into the
    final ok/fail JSON (used to carry the `dependency_runs` summary onto the
    target node's result). `invocation`, when given, is persisted immutably to
    `orchestration_meta.json#invocation` on the COLD init path only (the resume
    path preserves the existing block); it carries the reproduction record and the
    closure back-link that drives closure-aware resume."""
    if llm_config is None:
        llm_config = llm_config_from_legacy(llm, agent_model or "", llm_command)
    env = dict(base_env)
    env["METDSL_ORCHESTRATION_ID"] = orchestration_id

    tmp_parent = repo_root / "workspace" / "tmp"
    tmp_parent.mkdir(parents=True, exist_ok=True)
    # TMPDIR must match output_manifest.allowed_tmp_root for the active agent (orchestration uses
    # workspace/tmp/<orchestration_agent_run_id>). Set only after init returns that id; cleanup only
    # that directory so concurrent workflows' workspace/tmp/<other_agent_run_id>/ are untouched.
    orchestration_tmp_for_cleanup: Path | None = None
    # Identity of this driver process, recorded on the orchestration meta by init.
    driver_identity = _current_driver_identity()
    # True once init has committed this orchestration's meta — i.e. once there is a
    # `running` status that an interrupt would otherwise leave behind forever.
    init_committed = False

    # Tee this node's stdout JSONL event stream to a timestamped run-log file
    # under the orchestration dir, so the same information (node_start, the
    # conductor's phase_start/phase_complete emits, final ok/fail summary) is
    # recoverable from the workspace afterwards, not only on the terminal.
    # `_open_run_log` is internally exception-safe (returns None on failure), and
    # the `saved_stdout` capture cannot raise, so both stay outside the try. The
    # stdout swap and the node_start print, however, go INSIDE the try: otherwise
    # a raising print (e.g. BrokenPipeError when terminal stdout is a closed pipe,
    # which the tee does not swallow for the real stream) would skip the finally,
    # leaking the log handle and leaving sys.stdout wrapped.
    run_log_file = _open_run_log(repo_root, orchestration_id)
    saved_stdout = sys.stdout
    # Two exclusive claims cover this node for its whole life:
    #   ("orch", orchestration_id) — two drivers of ONE orchestration would share its
    #     preserved `orchestration_agent_run_id` and `workspace/tmp/<arid>`, and
    #     whichever finished first would delete the other's.
    #   ("spec", spec_ref) — two runs of one spec (in any mix of cold and resumed)
    #     derive their `pipeline_id` from the same
    #     `workspace/pipelines/<node_key_safe>/` tree and then write into it.
    # A caller that already holds one — it had to, to make a liveness decision about
    # this orchestration or this spec without racing — says so, since re-acquiring a
    # claim this process already holds would conflict with itself.
    node_claim = contextlib.ExitStack()

    try:
        if run_log_file is not None:
            sys.stdout = _StdoutTee(saved_stdout, run_log_file, mode=stdout_format)

        for kind, key, held, detail in (
            (
                "orch", orchestration_id, orch_claim_held,
                f"orchestration {orchestration_id} is already being driven by another "
                "process in this repository; two drivers would share its "
                "orchestration_agent_run_id and workspace/tmp/<arid>.",
            ),
            (
                "spec", spec_ref, spec_claim_held,
                f"another run of {spec_ref} is in progress in this repository; two "
                "runs of one spec derive their pipeline_id from the same "
                "workspace/pipelines/<node_key_safe>/ tree and then write into it.",
            ),
        ):
            if held:
                continue
            if not node_claim.enter_context(_exclusive_claim(repo_root, kind, key)):
                print(
                    json.dumps(
                        {
                            "status": "fail",
                            "reason": "concurrent_orchestration_running",
                            "detail": detail + " Wait for it to finish.",
                            "orchestration_id": orchestration_id,
                            "spec_ref": spec_ref,
                        },
                        ensure_ascii=False,
                    )
                )
                return 2

        # Announce node start on stdout (uniform for the single/target/dependency
        # nodes), matching the JSONL info-event stream the rest of this driver emits.
        print(
            json.dumps(
                {
                    "status": "info",
                    "event": "node_start",
                    "spec_ref": spec_ref,
                    "until_phase": until_phase,
                    "orchestration_id": orchestration_id,
                    "resume": resume_mode,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

        if resume_mode:
            # Resume an existing orchestration: enable checkpoint resume (sets
            # resume_enabled=true and preserves orchestration_agent_run_id) instead
            # of re-initializing. The returned meta carries orchestration_agent_run_id.
            # Pass the resolved spec/dependency refs so meta stays in sync when they
            # were overridden on the CLI — otherwise a later implicit resume would
            # recover the stale meta value and revert the override.
            init_args = [
                "init",
                "--repo-root",
                str(repo_root),
                "--orchestration-id",
                orchestration_id,
                "--resume-from-checkpoint",
                "--spec-ref",
                spec_ref,
                "--source-dependency-ref",
                source_dependency_ref,
            ]
            # Forward an EXPLICIT --agent-model to the resume repair (it overrides
            # repair-agent-runs' sibling derivation, e.g. for a `needs_manual` row).
            # Do NOT apply the claude default here: with no override, sibling_uniform
            # derives the run's actual model, which is more accurate than a default.
            if agent_model:
                init_args += ["--agent-model", agent_model]
            # Refresh this node's persisted closure end-phase to the effective closure
            # until_phase, so an operator phase override survives on the dependency
            # nodes themselves (durable even if the target orchestration never starts).
            if closure_until_phase:
                init_args += ["--closure-until-phase", closure_until_phase]
            # Refresh invocation.wait_usage_reset to the effective (re-passed) flag so the
            # recorded provenance matches THIS resumed run's behavior (the flag is not recovered
            # from the record — it is re-passed per invocation). Omitting it records False, which
            # correctly resets a run that was started WITH the flag but is now resumed without it.
            if wait_usage_reset:
                init_args += ["--wait-usage-reset"]
            # THIS process is now the orchestration's driver, so its identity replaces
            # whatever (often dead) driver the meta named before.
            if driver_identity:
                init_args += [
                    "--driver-json",
                    json.dumps(driver_identity, ensure_ascii=False),
                ]
        else:
            init_args = [
                "init",
                "--repo-root",
                str(repo_root),
                "--orchestration-id",
                orchestration_id,
                "--spec-ref",
                spec_ref,
                "--status",
                status,
                "--agent-backend",
                llm,
                "--source-dependency-ref",
                source_dependency_ref,
            ]
            # Record the orchestration agent's own model so its agent_runs row is
            # not a cost-attribution blind spot. Explicit --agent-model wins.
            # Otherwise default to the operator's configured (unpinned) claude alias
            # ONLY for the claude backend running the UNMODIFIED default command — an
            # overridden --llm-command (e.g. a wrapper selecting a different model)
            # could launch a different model, so we must not assert the alias there;
            # leave it for sibling backfill on resume instead.
            orchestration_model = agent_model
            if (
                not orchestration_model
                and llm == "claude"
                and llm_command == DEFAULT_LLM_COMMANDS["claude"]
            ):
                orchestration_model = _default_claude_agent_model()
            if orchestration_model:
                init_args += ["--agent-model", orchestration_model]
            # Persist the reproduction/closure record on the cold init only. On the
            # resume branch above the runtime preserves the original block, so we must
            # not re-pass it there (that would be a no-op at best, and risks recording
            # a divergent block if the immutability guard were ever relaxed).
            if invocation:
                init_args += ["--invocation-json", json.dumps(invocation, ensure_ascii=False)]
            # Driver liveness identity (pid + start ticks + boot id + hostname): what
            # lets a later run tell this orchestration's corpse apart from a live run
            # if this process dies without terminalizing. Omitted when it cannot be
            # captured (non-Linux), which degrades every probe to `unknown`.
            if driver_identity:
                init_args += [
                    "--driver-json",
                    json.dumps(driver_identity, ensure_ascii=False),
                ]
        try:
            init_result = _runtime_command(repo_root, env, init_args).payload
            orchestration_agent_run_id = str(init_result.get("orchestration_agent_run_id", "")).strip()
            if not orchestration_agent_run_id:
                raise RuntimeError(
                    "runtime command failed (init): missing orchestration_agent_run_id in init result"
                )
            init_committed = True
            orch_tmp = tmp_parent / orchestration_agent_run_id
            orch_tmp.mkdir(parents=True, exist_ok=True)
            env["TMPDIR"] = str(orch_tmp)
            env["ORCHESTRATION_AGENT_RUN_ID"] = orchestration_agent_run_id
            orchestration_tmp_for_cleanup = orch_tmp

            preflight_args = [
                "preflight",
                "--repo-root",
                str(repo_root),
                "--orchestration-id",
                orchestration_id,
                "--backend",
                llm,
                "--agent-command",
                llm_command,
                # Probe EVERY provider the configuration can launch, not just `defaults`. The
                # top-level payload still describes `--backend`, so a consumer that predates
                # `providers` reads exactly what it always did.
                "--llm-config",
                str(llm_config.path),
                # ...and probe the EFFECTIVE configuration. Preflight is a subprocess, so it
                # reloads the file, and a deprecated-flag override lives only in this process's
                # object — without it the probe certifies a command this run will not launch.
                # The RESOLVED defaults are what is sent, not "which fields were overridden":
                # re-applying a value the file already declares is a no-op, so one pair of
                # arguments covers both cases and nothing has to track the provenance.
                "--llm-config-defaults-model",
                llm_config.defaults.model,
                "--llm-config-defaults-command",
                llm_config.defaults.command,
                # The snapshot THIS process resolved. Preflight reloads the file, so without
                # it an edit in between would certify commands the conductor never launches.
                "--llm-config-sha256",
                llm_config.sha256,
            ]
            preflight_result = _runtime_command(repo_root, env, preflight_args).payload
        except RuntimeError as exc:
            print(
                json.dumps(
                    {
                        "status": "fail",
                        "reason": "runtime_command_failed",
                        "detail": str(exc),
                        "orchestration_id": orchestration_id,
                    },
                    ensure_ascii=False,
                )
            )
            return 2
        passed, detail = _ensure_preflight_pass(preflight_result)
        if not passed:
            _runtime_command(
                repo_root,
                env,
                [
                    "set-status",
                    "--repo-root",
                    str(repo_root),
                    "--orchestration-id",
                    orchestration_id,
                    "--status",
                    "fail",
                    "--reason-code",
                    "preflight_failed",
                    "--reason-detail",
                    detail,
                    "--blocking-policy-scope",
                    "preflight",
                ],
            )
            print(
                json.dumps(
                    {
                        "status": "fail",
                        "reason": "preflight_failed",
                        "detail": detail,
                        "orchestration_id": orchestration_id,
                    },
                    ensure_ascii=False,
                )
            )
            return 2

        prompt_text = _build_orchestration_prompt(
            orchestration_id=orchestration_id,
            orchestration_agent_run_id=orchestration_agent_run_id,
            spec_ref=spec_ref,
            source_dependency_ref=source_dependency_ref,
            until_phase=until_phase,
            workflow_mode=workflow_mode,
        )
        prompt_path = (
            repo_root
            / "workspace"
            / "orchestrations"
            / orchestration_id
            / "launches"
            / "orchestration.start.prompt.txt"
        )
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(prompt_text, encoding="utf-8")

        launched = False
        workflow_status = "running"
        if run_conductor:
            # Deterministic conductor: drive the phase loop in Python (no parent
            # orchestration LLM). The leaf substeps are spawned by the conductor.
            from tools.workflow_conductor import run_conductor

            try:
                workflow_status = run_conductor(
                    repo_root=repo_root,
                    orchestration_id=orchestration_id,
                    orchestration_agent_run_id=orchestration_agent_run_id,
                    spec_ref=spec_ref,
                    source_dependency_ref=source_dependency_ref,
                    until_phase=until_phase,
                    llm_config=llm_config,
                    workflow_mode=workflow_mode,
                    env=env,
                    resume=resume_mode,
                    wait_usage_reset=wait_usage_reset,
                )
            except Exception as exc:  # noqa: BLE001 - terminalize on conductor error
                # If the conductor/runtime already terminalized with a specific terminal
                # status (e.g. record-launch set fail_closed/sandbox_enforcement_violation
                # when a bwrap profile could not be built), preserve it rather than
                # clobbering it with a generic conductor_error.
                meta_now = _read_json_if_exists(
                    repo_root / "workspace" / "orchestrations" / orchestration_id
                    / "orchestration_meta.json") or {}
                cur_status = str(meta_now.get("status") or "").strip().lower()
                if cur_status in {"fail_closed", "blocked", "timeout", "cancel"}:
                    print(json.dumps(
                        {"status": cur_status,
                         "reason": meta_now.get("reason_code") or "conductor_terminal",
                         "detail": str(exc), "orchestration_id": orchestration_id},
                        ensure_ascii=False))
                    return 2
                _runtime_command(
                    repo_root, env,
                    ["set-status", "--repo-root", str(repo_root), "--orchestration-id",
                     orchestration_id, "--status", "fail", "--reason-code",
                     "conductor_error", "--reason-detail", str(exc)[:200]],
                )
                print(json.dumps(
                    {"status": "fail", "reason": "conductor_error", "detail": str(exc),
                     "orchestration_id": orchestration_id}, ensure_ascii=False))
                return 2
            launched = True
            # The conductor terminalizes meta itself; report a non-pass terminal here
            # and exit nonzero (otherwise a failed run falls through to the generic ok
            # output with exit 0). In dev mode, also collect + persist
            # `failure_analysis.json` — the documented dev-failure artifact that
            # `init --resume-from-checkpoint` reads (`_derive_resume_directive`) to
            # build the cross-phase reopen `resume_directive` on resume.
            if workflow_status.strip().lower() != "pass":
                if workflow_mode == "dev":
                    analysis = _collect_failure_analysis(repo_root, orchestration_id)
                    fail_output: dict[str, Any] = {
                        "status": "fail",
                        "reason": "workflow_failed",
                        "detail": analysis.get("reason_detail") or "workflow execution failed",
                        "orchestration_id": orchestration_id,
                        "workflow_mode": workflow_mode,
                        "workflow_status": workflow_status,
                    }
                    if extra_output:
                        fail_output.update(extra_output)
                    try:
                        analysis_ref, runtime_analysis_ref, stale_canonical_ref = _write_failure_analysis(
                            repo_root,
                            orchestration_id,
                            analysis,
                            tmp_dir=orchestration_tmp_for_cleanup,
                        )
                        fail_output["analysis_ref"] = analysis_ref
                        if runtime_analysis_ref is not None:
                            fail_output["runtime_analysis_ref"] = runtime_analysis_ref
                        if stale_canonical_ref is not None:
                            fail_output["stale_canonical_ref"] = stale_canonical_ref
                    except Exception as primary_exc:  # noqa: BLE001
                        # Primary write failed — attempt an emergency exclusive-create write so
                        # at least some artifact survives without clobbering agent-owned canonical.
                        orch_dir = (
                            repo_root / "workspace" / "orchestrations" / orchestration_id
                        )
                        emergency_payload = {**analysis, "emergency_write": True}
                        canonical_path = orch_dir / "failure_analysis.json"
                        try:
                            orch_dir.mkdir(parents=True, exist_ok=True)
                            wrote_canonical = _atomic_write_json_exclusive(
                                canonical_path, emergency_payload, tmp_dir=orch_dir
                            )
                            if wrote_canonical:
                                fallback_ref = str(canonical_path.relative_to(repo_root))
                            else:
                                _MAX_SIDECAR_ATTEMPTS = 5
                                fallback_ref = None
                                for _ in range(_MAX_SIDECAR_ATTEMPTS):
                                    slug = uuid.uuid4().hex[:12]
                                    sidecar = orch_dir / f"failure_analysis.fallback.{slug}.json"
                                    if _atomic_write_json_exclusive(
                                        sidecar, emergency_payload, tmp_dir=orch_dir
                                    ):
                                        fallback_ref = str(sidecar.relative_to(repo_root))
                                        break
                                if fallback_ref is None:
                                    raise OSError(
                                        "emergency sidecar write failed after "
                                        f"{_MAX_SIDECAR_ATTEMPTS} attempts"
                                    )
                            fail_output["analysis_ref"] = fallback_ref
                            fail_output["analysis_ref_error"] = str(primary_exc)
                            fail_output["analysis_ref_write_mode"] = "emergency_fallback"
                        except Exception as fallback_exc:  # noqa: BLE001
                            fail_output["reason"] = "failure_analysis_persist_failed"
                            fail_output["analysis_ref_error"] = str(primary_exc)
                            fail_output["analysis_ref_fallback_error"] = str(fallback_exc)
                    print(json.dumps(fail_output, ensure_ascii=False))
                    return 2
                fail_output = {
                    "status": "fail",
                    "reason": "workflow_failed",
                    "orchestration_id": orchestration_id,
                    "workflow_mode": workflow_mode,
                    "workflow_status": workflow_status,
                }
                if extra_output:
                    fail_output.update(extra_output)
                print(json.dumps(fail_output, ensure_ascii=False))
                return 2
        ok_output: dict[str, Any] = {
            "status": "ok",
            "orchestration_id": orchestration_id,
            "resumed": resume_mode,
            "llm": llm,
            "llm_command": llm_command,
            "target_spec_ref": spec_ref,
            "until_phase": until_phase,
            "workflow_mode": workflow_mode,
            "metdsl_workflow_mode": env["METDSL_WORKFLOW_MODE"],
            "metdsl_workflow_exec_mode": env["METDSL_WORKFLOW_EXEC_MODE"],
            "workflow_status": workflow_status,
            "prompt_ref": str(prompt_path.relative_to(repo_root)),
            "llm_invoked": launched,
        }
        if extra_output:
            ok_output.update(extra_output)
        print(json.dumps(ok_output, ensure_ascii=False))
        return 0
    except (KeyboardInterrupt, SystemExit):
        # Ctrl-C, or the SIGTERM converter installed in __main__. Without this the
        # orchestration's meta stays `running` forever: nothing else terminalizes it,
        # so an implicit --resume refuses it and a cold re-run silently starts a new
        # orchestration from phase 1, discarding the checkpoint. Terminalizing here
        # makes the interrupted run recoverable via the normal resume path (a terminal
        # status is what routes `init --resume-from-checkpoint` through
        # `terminal_reset`, where the crash reconciliations live).
        # `init_committed` is set when the runtime call RETURNS, but the runtime writes
        # the `running` meta well before that (an operator token, several more writes,
        # and the subprocess round-trip all follow). A signal landing in that window
        # would leave exactly the stuck-`running` orchestration this clause exists to
        # prevent. So fall back to the durable evidence: a meta on disk whose `driver`
        # block names THIS process was necessarily written by this invocation's init,
        # which makes it ours to terminalize. Anything else — a reused
        # `--orchestration-id` naming someone else's run, a meta we never wrote — is
        # left untouched.
        if init_committed or _is_own_driver(
            _read_orchestration_meta(repo_root, orchestration_id), driver_identity
        ):
            _terminalize_interrupted_orchestration(repo_root, env, orchestration_id)
        raise
    finally:
        if run_log_file is not None:
            sys.stdout = saved_stdout
            try:
                run_log_file.close()
            except Exception:  # noqa: BLE001
                pass
        if orchestration_tmp_for_cleanup is not None and orchestration_tmp_for_cleanup.exists():
            shutil.rmtree(orchestration_tmp_for_cleanup, ignore_errors=True)
        # Released LAST, after the tmp cleanup: a waiting driver that acquired the
        # claim mid-cleanup would re-init into `workspace/tmp/<arid>` while this
        # process was still deleting that very directory.
        node_claim.close()


def _dependency_node_ready(
    repo_root: Path, node: dict[str, Any], required_stages: list[str]
) -> bool:
    """True iff the dependency node already satisfies `required_stages`.

    Mirrors the runtime readiness contract (`_verify_dependency_readiness`): a
    node is ready when ANY single matching catalog version has a coherent
    artifact chain across all required stages (the same version V must satisfy
    every stage). Kept module-level so the closure driver uses one consistent
    readiness rule for both the pre-run skip check and the post-run
    verification.

    R6-lite rides on the same `_verify_dep_stage` call: its `ir_ref` stage also requires the
    node's RECORDED dependency resolution (its `dependency_graph.json` sidecar) to match the
    one today's `deps.yaml` + `spec_catalog.yaml` derive. So a node certified against an older
    version of one of ITS dependencies (e.g. harness 0.2.1 after the catalog moved to 0.3.0)
    reports not-ready here and this driver re-runs it — which is how "a dependency spec was
    updated, so its dependents are regenerated" becomes a mechanism rather than an operator
    ritual. No content-free version bump of the dependents is required."""
    from tools.orchestration_runtime import _verify_dep_stage

    kind, sid = node["spec_kind"], node["spec_id"]
    return any(
        all(_verify_dep_stage(repo_root, kind, sid, v, st) for st in required_stages)
        for v in node["spec_versions"]
    )


def _resolve_dependency_closure(
    repo_root: Path, target_spec_ref: str
) -> tuple[list[dict[str, Any]], dict[str, str] | None]:
    """Resolve the target's transitive dependency closure in topological order.

    Returns `(ordered, error)`:
      - `ordered`: dependency nodes in dependency order (dependencies before
        dependents), EXCLUDING the target. Each is
        `{spec_ref, spec_kind, spec_id, spec_versions}`. `spec_versions` is the
        descending list of catalog versions satisfying the requiring edge's
        constraint (intersected across edges when a node is required more than
        once). The readiness check mirrors the runtime contract
        (`_verify_dependency_readiness`): a node is ready when ANY one of these
        versions has a coherent artifact chain — so we keep all of them, not
        just the highest, to avoid re-running a dependency that an older
        matching version already satisfies.
      - `error`: None on success; else `{reason, detail}` for a cycle,
        unresolvable dependency, version conflict, malformed/missing deps.yaml,
        or catalog corruption (all fail-closed — no node is run).

    Edges come from `<spec_ref>/deps.yaml` resolved against `spec_catalog.yaml`
    via the canonical runtime helpers (`_parse_dep_entries`,
    `_matching_dep_versions`, `resolve_spec_ref_for`). Post-order DFS yields the
    topological order; a node already on the DFS stack is a cycle.
    """
    from tools.orchestration_runtime import (
        SpecCatalogCorruption,
        _load_spec_catalog,
        _matching_dep_versions,
        _parse_dep_entries,
        _read_deps_yaml,
        resolve_spec_ref_for,
    )
    from tools.runner_renderer import infra_dep_count_violation, spec_id_length_violation

    # The catalog is loaded lazily — only once a dependency edge is actually
    # encountered. A leaf target (empty deps.yaml) needs no catalog, so a
    # missing/corrupt registry must not turn an otherwise-launchable leaf
    # workflow into a failure (matching the runtime readiness path, which
    # treats no-deps specs as vacuously ready without the catalog).
    catalog_cache: dict[tuple[str, str], tuple[str, ...]] | None = None

    def _get_catalog() -> dict[tuple[str, str], tuple[str, ...]]:
        nonlocal catalog_cache
        if catalog_cache is None:
            catalog_cache = _load_spec_catalog(str(repo_root.resolve()))
        return catalog_cache

    ordered_refs: list[str] = []
    # Per spec_ref: the (kind, sid) identity, and the set of catalog versions
    # satisfying every edge that required it (intersection across edges).
    kindid_by_ref: dict[str, tuple[str, str]] = {}
    matched_by_ref: dict[str, tuple[str, ...]] = {}
    visiting: set[str] = set()
    done: set[str] = set()
    error: dict[str, str] | None = None

    # Sentinel for "the registry answered, and its answer is self-contradictory". It is an
    # object() rather than a string on purpose: `infra_dep_count_violation` exempts only the
    # exact string "infrastructure" and treats a non-string as unexempt, so a kind that
    # reduces to this sentinel can never be waved through — which is what lets the caller
    # report it with the single `_registry_defect and _infra_violation` condition.
    _UNRESOLVABLE_KIND = object()

    def _kind_for_gate(spec_ref: str, deps_doc: dict) -> tuple[Any, str | None]:
        """The `spec_kind` the infra-dep-count gate judges `spec_ref` by.

        Returns `(kind, registry_defect_detail)`. The detail is non-None when the catalog
        could not answer authoritatively, and the two cases differ in how the caller must
        treat it — see below.

        Kind decides EXEMPTION (an `infrastructure` spec declares no harness of its own), so
        it must not be self-declared: `deps.yaml`'s top-level `spec_kind` is carried by no
        schema — `_parse_dep_entries` validates only the `dependencies` block — and a spec
        that writes `spec_kind: infrastructure` there would exempt itself from the gate while
        `resolve_node` (which reads the CATALOG) still rejects it, phases and billed
        dependency runs later.

        So the catalog is authoritative wherever it can answer. For a dependency that is
        already true structurally — `kindid_by_ref` was set from the catalog-validated edge
        that pulled it in, before the recursion. For the TARGET (on no edge yet) look the
        spec_id up in the catalog directly.

        Two shapes leave the catalog unable to answer:
        - the registry is missing / corrupt / unreadable. The declared value is still
          returned, so a declared `infrastructure` leaf stays launchable under a silent
          registry — the lazy-catalog property this function must not break. The caller
          therefore uses the detail to suppress only the REJECTION half (the half that
          needs proof); downgrading a repo-wide registry outage into "your deps.yaml is
          wrong" would send the operator to edit a file that is not the problem, which is
          the same reason `_load_spec_catalog` raises instead of returning `{}`.
        - the spec_id is registered under more than one `spec_kind`. `docs/SPEC.md` req. 4
          requires spec_id to be unique repository-wide, and `resolve_node` does NOT detect
          the duplicate — it returns the FIRST matching entry — so resolving it here by
          catalog order would make the two capture points disagree by luck of ordering.
          Here there is no lazy-catalog property to protect (the registry WAS read, it is
          simply self-contradictory), so `_UNRESOLVABLE_KIND` is returned and the caller
          reports the registry defect unconditionally. Returning the declared value instead
          would let a spec self-declare `infrastructure` and skip the gate entirely — the
          exact bypass this function exists to close, and one that costs a full billed
          dependency closure before `resolve_node` refuses the target.
        """
        edge_kind = (kindid_by_ref.get(spec_ref) or (None,))[0]
        if edge_kind:
            return edge_kind, None
        spec_id = Path(spec_ref).name
        try:
            kinds = {k for (k, sid) in _get_catalog() if sid == spec_id}
        except (SpecCatalogCorruption, RuntimeError, OSError) as exc:
            return deps_doc.get("spec_kind"), (
                f"spec/registry/spec_catalog.yaml could not be read ({exc}), so the "
                f"spec_kind of {spec_ref} could not be confirmed")
        if len(kinds) == 1:
            return next(iter(kinds)), None
        if len(kinds) > 1:
            # Reported unconditionally by the caller — see `_kind_unresolvable` there. Unlike
            # the unreadable-registry case there is no lazy-catalog property to protect: the
            # registry WAS read, it is simply self-contradictory.
            return _UNRESOLVABLE_KIND, (
                f"spec_id {spec_id!r} is registered under multiple spec_kinds "
                f"{sorted(kinds)} in spec/registry/spec_catalog.yaml; spec_id must be "
                f"unique repository-wide (docs/SPEC.md req. 4), and until it is, this "
                f"spec's kind cannot be resolved")
        # Registered under no kind at all: an unregistered spec. The declared value decides
        # here — including the exemption, so a self-declared `infrastructure` does pass this
        # gate. That is not a hole worth closing here: an unregistered spec_ref is rejected
        # by `resolve_node` (target) and by `_matching_dep_versions` (dependency edge)
        # regardless of what it declares, so it can never reach a phase.
        return deps_doc.get("spec_kind"), None

    def visit(spec_ref: str) -> None:
        nonlocal error
        if error is not None or spec_ref in done:
            return
        if spec_ref in visiting:
            error = {
                "reason": "dependency_cycle",
                "detail": f"dependency cycle detected at {spec_ref}",
            }
            return
        # M3d spec-input gate at closure-build: reject an over-length spec_id here, before
        # any node runs. resolve_node gates each node's own run, but an ALREADY-READY
        # dependency is skipped before it reaches `_run_node` → resolve_node — so gating
        # only there could let an over-length ready dep slip past. Checking every visited
        # spec (target + all deps) here is the closure-level mirror of resolve_node's bound
        # (runner_renderer.MAX_SPEC_ID_LEN). A >55 fortran node cannot certify (so cannot be
        # ready), but this makes the canonical capture point robust regardless.
        _sid_violation = spec_id_length_violation(Path(spec_ref).name)
        if _sid_violation:
            error = {"reason": "spec_id_too_long", "detail": f"{spec_ref}: {_sid_violation}"}
            return
        visiting.add(spec_ref)
        deps_doc = _read_deps_yaml(repo_root, spec_ref)
        if not isinstance(deps_doc, dict):
            error = {
                "reason": "dependency_deps_unreadable",
                "detail": f"{spec_ref}/deps.yaml is missing or unparseable",
            }
            return
        entries, well_formed = _parse_dep_entries(deps_doc)
        if not well_formed:
            error = {
                "reason": "dependency_deps_malformed",
                "detail": f"{spec_ref}/deps.yaml has a malformed dependency schema",
            }
            return
        # M3d spec-input gate at closure-build (sibling of the spec_id bound above): every
        # non-infrastructure spec declares EXACTLY ONE `infrastructure` (runner-harness)
        # dependency. `resolve_node` gates each node's own run, but an ALREADY-READY
        # dependency is skipped before it reaches `_run_node` → resolve_node, so gating only
        # there could let a violating ready dep slip past.
        # A missing/malformed deps.yaml keeps its existing reason (both checks above run
        # first), so this check only ever sees a readable, well-formed dependency schema.
        own_kind, _registry_defect = _kind_for_gate(spec_ref, deps_doc)
        infra_count = sum(1 for kind, _sid, _c in entries if kind == "infrastructure")
        _infra_violation = infra_dep_count_violation(own_kind, infra_count)
        # A multi-kind registry defect always lands here: `_UNRESOLVABLE_KIND` is not a
        # string, so `infra_dep_count_violation` can never exempt it and always produces a
        # violation — which this branch then re-reports as the registry defect it really is.
        # That is deliberate: an EXEMPTION granted on an unknown kind would be as unfounded
        # as a rejection, and honoring a self-declared `infrastructure` there would skip the
        # gate outright. An unreadable registry is different — there the declared value MAY
        # still grant the exemption, and only the rejection half is suppressed: pointing the
        # operator at deps.yaml during a registry outage sends them to the wrong file, and
        # the dep count may well be correct for the node's true kind.
        if _registry_defect and _infra_violation:
            error = {
                "reason": "spec_catalog_corrupt",
                "detail": _registry_defect,
            }
            return
        if _infra_violation:
            error = {
                "reason": "infra_dep_count_invalid",
                "detail": f"{spec_ref}: {_infra_violation}",
            }
            return
        for kind, sid, constraint in entries:
            try:
                matched = _matching_dep_versions(_get_catalog(), kind, sid, constraint)
            except SpecCatalogCorruption as exc:
                error = {"reason": "spec_catalog_corrupt", "detail": str(exc)}
                return
            if not matched:
                error = {
                    "reason": "dependency_unresolvable",
                    "detail": (
                        f"{kind}/{sid} constraint {constraint!r} has no matching "
                        "catalog version"
                    ),
                }
                return
            dep_spec_ref = resolve_spec_ref_for(repo_root, kind, sid)
            if not dep_spec_ref:
                error = {
                    "reason": "dependency_spec_ref_unresolved",
                    "detail": f"no unique spec directory in catalog for {kind}/{sid}",
                }
                return
            prior_kindid = kindid_by_ref.get(dep_spec_ref)
            if prior_kindid is not None and prior_kindid != (kind, sid):
                error = {
                    "reason": "dependency_identity_conflict",
                    "detail": (
                        f"{dep_spec_ref} required as both {prior_kindid} and "
                        f"{(kind, sid)}"
                    ),
                }
                return
            kindid_by_ref[dep_spec_ref] = (kind, sid)
            # Intersect the matching-version sets across edges. An empty
            # intersection means two edges pin incompatible version ranges for
            # the same node — a genuine conflict, fail-closed.
            prior_versions = matched_by_ref.get(dep_spec_ref)
            if prior_versions is None:
                matched_by_ref[dep_spec_ref] = tuple(matched)
            else:
                matched_set = set(matched)
                intersection = tuple(v for v in prior_versions if v in matched_set)
                if not intersection:
                    error = {
                        "reason": "dependency_version_conflict",
                        "detail": (
                            f"{dep_spec_ref} ({kind}/{sid}) required with "
                            f"incompatible constraints: {prior_versions} vs {tuple(matched)}"
                        ),
                    }
                    return
                matched_by_ref[dep_spec_ref] = intersection
            visit(dep_spec_ref)
            if error is not None:
                return
        visiting.discard(spec_ref)
        done.add(spec_ref)
        ordered_refs.append(spec_ref)

    visit(target_spec_ref)
    if error is not None:
        return [], error
    ordered: list[dict[str, Any]] = []
    for ref in ordered_refs:
        if ref == target_spec_ref:
            continue
        kind, sid = kindid_by_ref[ref]
        ordered.append(
            {
                "spec_ref": ref,
                "spec_kind": kind,
                "spec_id": sid,
                "spec_versions": list(matched_by_ref[ref]),
            }
        )
    return ordered, None


def _run_with_dependency_closure(
    *,
    repo_root: Path,
    base_env: dict[str, str],
    target_orchestration_id: str,
    target_spec_ref: str,
    target_source_dependency_ref: str,
    until_phase: str,
    llm: str,
    llm_command: str,
    llm_config: LlmConfig | None = None,
    llm_config_overrides: dict[str, str] | None = None,
    workflow_mode: str = DEFAULT_WORKFLOW_MODE,
    agent_model: str | None = None,
    status: str = "",
    run_conductor: bool = True,
    wait_usage_reset: bool = False,
    stdout_format: str = "jsonl",
    resume: bool = False,
    prior_orch_by_spec: dict[str, str] | None = None,
    raw_argv: list[str] | None = None,
    preclaimed_orchestration_id: str | None = None,
) -> int:
    """Run the target's dependency closure bottom-up, then the target.

    Each not-ready dependency node runs as its own orchestration (one per node);
    nodes already satisfying the required readiness are skipped. On the first
    dependency failure the run stops (the target is not launched). The target's
    final JSON result carries a `dependency_runs` summary.

    This drives BOTH the fresh `--with-deps` path and closure-aware `--resume`:
    - Fresh (`resume=False`, `prior_orch_by_spec=None`): every not-ready node gets a
      fresh orchestration id and a cold run. Behavior is unchanged from before, with
      the one additive effect that each node now records an `invocation` block whose
      `closure_id` = `target_orchestration_id`, which is what makes a LATER resume
      closure-aware.
    - Resume (`resume=True`): `prior_orch_by_spec` maps a node's spec_ref to its prior
      orchestration id; a not-ready node with a prior orchestration is resumed (warm,
      from its checkpoint) instead of re-run cold, and the target reuses
      `target_orchestration_id` (= closure_id), resumed when its orchestration dir
      already exists. The closure itself is re-derived here deterministically, so
      already-ready deps are skipped and any deps.yaml/catalog change is reflected.

    `raw_argv` is threaded into each node's `invocation` record so the reproduction
    command is captured on every closure node.
    """
    prior_orch_by_spec = prior_orch_by_spec or {}
    if llm_config is None:
        llm_config = llm_config_from_legacy(llm, agent_model or "", llm_command)
    ordered, error = _resolve_dependency_closure(repo_root, target_spec_ref)
    if error is not None:
        _emit_closure_event(
            {
                "status": "fail",
                "reason": "dependency_closure_unresolved",
                "detail": error.get("detail"),
                "reason_code": error.get("reason"),
                "target_spec_ref": target_spec_ref,
            },
            stdout_format,
        )
        return 2

    # Dependency depth follows the target: Compile-only readiness when the
    # target stops at Compile, else full execution readiness (Build+Validate).
    dep_until_phase = "Compile" if until_phase == "Compile" else "Validate"
    required_stages = (
        ["ir_ref"]
        if dep_until_phase == "Compile"
        else ["ir_ref", "pipeline_ref", "aggregate_verdict"]
    )

    # This driver's identity, so the per-node cold-start guard below can tell the
    # orchestrations THIS invocation starts apart from a genuinely concurrent run. The
    # guard rescans the workspace per node rather than working from a snapshot taken
    # here: a closure reaches its later nodes hours after it starts, and a competing
    # run launched inside that window is precisely what the guard must catch.
    closure_driver_identity = _current_driver_identity()

    dependency_runs: list[dict[str, Any]] = []
    for node in ordered:
        kind, sid, spec_ref = node["spec_kind"], node["spec_id"], node["spec_ref"]
        node_label = f"{kind}/{sid}@{node['spec_versions'][0]}"
        if _dependency_node_ready(repo_root, node, required_stages):
            dependency_runs.append(
                {"node": node_label, "spec_ref": spec_ref, "skipped": True, "status": "ready"}
            )
            continue

        # Closure-aware resume: a not-ready node with a prior orchestration under this
        # closure is resumed (warm) from its checkpoint; otherwise mint a fresh id and
        # cold-run it. Fresh `--with-deps` runs pass an empty map → always fresh/cold.
        prior_dep_orch_id = prior_orch_by_spec.get(spec_ref) if resume else None
        dep_orch_id = prior_dep_orch_id or _new_orchestration_id()
        dep_resume = prior_dep_orch_id is not None
        # M-F executor fail-close, per warm-resumed member. The entry gate in main() only checked
        # the entry orchestration; a mixed closure could otherwise resume a legacy-recorded
        # dependency here under the pure-only dispatch. A cold (fresh) dep node records `pure` and
        # is not gated.
        if dep_resume:
            # Twin gate, same reasoning one level down: the leaf-LLM configuration a member
            # launched with must still be the one on disk, or its remaining phases would run on
            # different models than its finished ones did.
            for rejection in (
                _generate_executor_resume_rejection(
                    dep_orch_id, _recorded_generate_executor(repo_root, dep_orch_id)),
                _llm_config_resume_rejection(
                    dep_orch_id, _recorded_llm_config(repo_root, dep_orch_id),
                    repo_root=repo_root, effective_path=_repo_relative(llm_config.path, repo_root),
                    effective_sha256=llm_config.sha256,
                    effective_overrides=dict(llm_config_overrides or {})),
            ):
                if rejection is None:
                    continue
                _emit_closure_event(
                    {
                        **rejection,
                        "failed_dependency_node": node_label,
                        "spec_ref": spec_ref,
                        "dependency_runs": dependency_runs,
                        "target_spec_ref": target_spec_ref,
                    },
                    stdout_format,
                )
                return 2
        # Claims are held across this node's guard AND its run. A cold node needs the
        # SPEC claim (the orchestration its guard looks for is not written until `init`
        # inside `_run_node`, so a competing run started in that window would scan
        # clean); a warm-resumed node needs the ORCHESTRATION claim, because its guard
        # may WRITE — terminalizing a dead driver — and two closures resuming the same
        # member would otherwise both perform that write, the later one flipping an
        # actively-resumed run back to `fail`.
        dep_orch_preclaimed = dep_orch_id == preclaimed_orchestration_id
        with contextlib.ExitStack() as node_claim:
            if dep_resume:
                node_claim_ok = dep_orch_preclaimed or node_claim.enter_context(
                    _exclusive_claim(repo_root, "orch", dep_orch_id))
            else:
                node_claim_ok = node_claim.enter_context(
                    _exclusive_claim(repo_root, "spec", spec_ref))
            if not node_claim_ok:
                _emit_closure_event(
                    {
                        **_concurrent_cold_start_envelope(spec_ref),
                        "orchestration_id": dep_orch_id,
                        "failed_dependency_node": node_label,
                        "dependency_runs": dependency_runs,
                        "target_spec_ref": target_spec_ref,
                    },
                    stdout_format,
                )
                return 2
            # Driver-liveness gate for this node: a warm-resumed member is terminalized
            # when its own driver crashed (and refused when it is still live); a cold node
            # is guarded against this spec's other non-terminal orchestrations.
            node_conflict = (
                _warm_resume_liveness_guard(
                    repo_root, dep_orch_id, stdout_format=stdout_format, env=base_env
                )
                if dep_resume
                else _cold_start_running_guard(
                    repo_root, spec_ref, stdout_format=stdout_format,
                    driver_identity=closure_driver_identity,
                )
            )
            if node_conflict is not None:
                _emit_closure_event(
                    {
                        **node_conflict,
                        "failed_dependency_node": node_label,
                        "spec_ref": spec_ref,
                        "dependency_runs": dependency_runs,
                        "target_spec_ref": target_spec_ref,
                    },
                    stdout_format,
                )
                return 2
            try:
                dep_source_dependency_ref = _discover_source_dependency_ref(repo_root, spec_ref)
            except ValueError as exc:
                _emit_closure_event(
                    {
                        "status": "fail",
                        "reason": "dependency_dep_ref_unresolved",
                        "detail": str(exc),
                        "failed_dependency_node": node_label,
                        "spec_ref": spec_ref,
                        "dependency_runs": dependency_runs,
                        "target_spec_ref": target_spec_ref,
                    },
                    stdout_format,
                )
                return 2
            # The per-node `node_start` event is emitted uniformly inside _run_node;
            # here we only announce which dependency node (with its pretty label) the
            # closure is about to drive, so the stream stays human-traceable.
            _emit_closure_event(
                {
                    "status": "info",
                    "event": "dependency_node_begin",
                    "node": node_label,
                    "spec_ref": spec_ref,
                    "until_phase": dep_until_phase,
                    "orchestration_id": dep_orch_id,
                    "resume": dep_resume,
                },
                stdout_format,
            )
            # Cold run records the reproduction/closure block; a resumed node preserves
            # the block it already carries, so pass None there.
            dep_invocation = None if dep_resume else _build_invocation_record(
                argv=raw_argv,
                spec_ref=spec_ref,
                until_phase=dep_until_phase,
                llm=llm,
                llm_command=llm_command,
                llm_config=llm_config,
                llm_config_overrides=llm_config_overrides,
                repo_root=repo_root,
                workflow_mode=workflow_mode,
                agent_model=agent_model,
                with_deps=True,
                wait_usage_reset=wait_usage_reset,
                closure_id=target_orchestration_id,
                closure_target_spec_ref=target_spec_ref,
                closure_until_phase=until_phase,
            )
            rc = _run_node(
                repo_root=repo_root,
                base_env=base_env,
                orchestration_id=dep_orch_id,
                spec_ref=spec_ref,
                source_dependency_ref=dep_source_dependency_ref,
                until_phase=dep_until_phase,
                llm=llm,
                llm_command=llm_command,
                llm_config=llm_config,
                                workflow_mode=workflow_mode,
                agent_model=agent_model,
                status=status,
                run_conductor=run_conductor,
                resume_mode=dep_resume,
                wait_usage_reset=wait_usage_reset,
                invocation=dep_invocation,
                # On resume, refresh this dep's persisted closure end-phase to the
                # effective closure until_phase so an operator phase override stays durable
                # on the dependency nodes even if the target orchestration is never created.
                closure_until_phase=until_phase if dep_resume else None,
                stdout_format=stdout_format,
                spec_claim_held=not dep_resume,
                orch_claim_held=dep_resume,
            )
        dependency_runs.append(
            {
                "node": node_label,
                "spec_ref": spec_ref,
                "skipped": False,
                "resumed": dep_resume,
                "orchestration_id": dep_orch_id,
                "exit_code": rc,
            }
        )
        if rc != 0:
            _emit_closure_event(
                {
                    "status": "fail",
                    "reason": "dependency_node_failed",
                    "failed_dependency_node": node_label,
                    "spec_ref": spec_ref,
                    "orchestration_id": dep_orch_id,
                    "exit_code": rc,
                    "dependency_runs": dependency_runs,
                    "target_spec_ref": target_spec_ref,
                },
                stdout_format,
            )
            return rc

        # A zero exit code does not by itself prove the dependency reached the
        # required readiness: `--no-run-conductor` only prepares artifacts, and a
        # launched agent can exit cleanly with the orchestration still
        # non-terminal ("running") without producing the ir/pipeline/verdict
        # evidence. Re-verify before launching the dependent/target node;
        # otherwise the next node would just fail-close at workflow-launch-check.
        if not _dependency_node_ready(repo_root, node, required_stages):
            dependency_runs[-1]["status"] = "not_ready_after_run"
            _emit_closure_event(
                {
                    "status": "fail",
                    "reason": "dependency_not_ready_after_run",
                    "detail": (
                        f"{node_label} ran (exit 0) but did not produce the "
                        f"required readiness ({'/'.join(required_stages)}); "
                        "common causes: --no-run-conductor, or the agent exited "
                        "without recording a terminal pass (status still running)."
                    ),
                    "failed_dependency_node": node_label,
                    "spec_ref": spec_ref,
                    "orchestration_id": dep_orch_id,
                    "dependency_runs": dependency_runs,
                    "target_spec_ref": target_spec_ref,
                },
                stdout_format,
            )
            return 2

    # All dependencies are ready — run the target node, carrying the summary. On
    # closure-aware resume, reuse the closure id as the target orchestration id and
    # resume it when its orchestration is actually THIS closure's target from a prior
    # attempt (it may have failed after the deps, or never started). Warm-resume ONLY
    # when the existing meta is linked to this closure — its own invocation.closure_id
    # equals the closure/target id — AND its spec matches. A reserved id that already
    # named an UNRELATED pre-existing orchestration (a reused --orchestration-id, even
    # one for the SAME spec from a standalone run) must be cold-initialized as the
    # intended target, not resumed off the unrelated run's stale checkpoint/phase state.
    target_meta_path = (
        repo_root / "workspace" / "orchestrations" / target_orchestration_id
        / "orchestration_meta.json"
    )
    target_meta = _read_json_if_exists(target_meta_path) if resume else None
    target_meta_invocation = (
        target_meta.get("invocation") if isinstance(target_meta, dict) else None
    )
    target_resume = (
        resume
        and isinstance(target_meta, dict)
        and target_meta.get("spec_ref") == target_spec_ref
        and isinstance(target_meta_invocation, dict)
        and target_meta_invocation.get("closure_id") == target_orchestration_id
    )
    # M-F executor fail-close for the warm-resumed target (mirrors the per-dependency gate above);
    # a cold target records `pure` and is not gated.
    if target_resume:
        for rejection in (
            _generate_executor_resume_rejection(
                target_orchestration_id,
                _recorded_generate_executor(repo_root, target_orchestration_id)),
            _llm_config_resume_rejection(
                target_orchestration_id,
                _recorded_llm_config(repo_root, target_orchestration_id),
                repo_root=repo_root, effective_path=_repo_relative(llm_config.path, repo_root),
                effective_sha256=llm_config.sha256,
                effective_overrides=dict(llm_config_overrides or {})),
        ):
            if rejection is None:
                continue
            _emit_closure_event(
                {**rejection, "dependency_runs": dependency_runs},
                stdout_format,
            )
            return 2
    # As with each dependency node, the claim spans this node's guard and its run.
    target_preclaimed = target_orchestration_id == preclaimed_orchestration_id
    with contextlib.ExitStack() as target_claim:
        if target_resume:
            target_claim_ok = target_preclaimed or target_claim.enter_context(
                _exclusive_claim(repo_root, "orch", target_orchestration_id))
        else:
            target_claim_ok = target_claim.enter_context(
                _exclusive_claim(repo_root, "spec", target_spec_ref))
        if not target_claim_ok:
            _emit_closure_event(
                {
                    **_concurrent_cold_start_envelope(target_spec_ref),
                    "dependency_runs": dependency_runs,
                    "target_spec_ref": target_spec_ref,
                },
                stdout_format,
            )
            return 2
        # Same liveness gate for the target node (warm-resumed vs cold), after every
        # dependency is ready and before the target's own orchestration is touched.
        target_conflict = (
            _warm_resume_liveness_guard(
                repo_root, target_orchestration_id, stdout_format=stdout_format, env=base_env
            )
            if target_resume
            else _cold_start_running_guard(
                repo_root, target_spec_ref, stdout_format=stdout_format,
                driver_identity=closure_driver_identity,
            )
        )
        if target_conflict is not None:
            _emit_closure_event(
                {
                    **target_conflict,
                    "spec_ref": target_spec_ref,
                    "dependency_runs": dependency_runs,
                    "target_spec_ref": target_spec_ref,
                },
                stdout_format,
            )
            return 2
        target_invocation = None if target_resume else _build_invocation_record(
            argv=raw_argv,
            spec_ref=target_spec_ref,
            until_phase=until_phase,
            llm=llm,
            llm_command=llm_command,
            llm_config=llm_config,
            llm_config_overrides=llm_config_overrides,
            repo_root=repo_root,
            workflow_mode=workflow_mode,
            agent_model=agent_model,
            with_deps=True,
            wait_usage_reset=wait_usage_reset,
            closure_id=target_orchestration_id,
            closure_target_spec_ref=target_spec_ref,
            closure_until_phase=until_phase,
        )
        return _run_node(
            repo_root=repo_root,
            base_env=base_env,
            orchestration_id=target_orchestration_id,
            spec_ref=target_spec_ref,
            source_dependency_ref=target_source_dependency_ref,
            until_phase=until_phase,
            llm=llm,
            llm_command=llm_command,
            llm_config=llm_config,
                        workflow_mode=workflow_mode,
            agent_model=agent_model,
            status=status,
            run_conductor=run_conductor,
            resume_mode=target_resume,
            wait_usage_reset=wait_usage_reset,
            invocation=target_invocation,
            closure_until_phase=until_phase if target_resume else None,
            extra_output={"dependency_runs": dependency_runs},
            stdout_format=stdout_format,
            spec_claim_held=not target_resume,
            orch_claim_held=target_resume,
        )


def _sigterm_to_exit(signum: int, frame: Any) -> None:  # noqa: ARG001 - signal API
    """Convert SIGTERM into `SystemExit(143)` (128 + SIGTERM).

    Default SIGTERM handling kills the interpreter outright: no `except` clause and no
    `finally` runs, so the orchestration meta stays `running` forever. Raising instead
    routes the termination through `_run_node`'s interrupt clause, which terminalizes
    the run as `cancel` / `driver_interrupted` and leaves the checkpoint resumable.
    """
    raise SystemExit(143)


def _install_signal_handlers() -> None:
    """Install the SIGTERM converter. Called ONLY from the `__main__` block.

    Not from `main()`: the unit tests (and any embedding caller) invoke `main()`
    in-process, and a library call must not rewrite the host process's signal
    disposition. Failures are ignored — signal handling is a recovery nicety, never a
    precondition for running a workflow.
    """
    try:
        signal.signal(signal.SIGTERM, _sigterm_to_exit)
    except (ValueError, OSError, AttributeError):  # pragma: no cover - platform dependent
        pass


if __name__ == "__main__":
    _install_signal_handlers()
    raise SystemExit(main())
