#!/usr/bin/env python3
"""Replay one recorded JUDGING or DIAGNOSE launch under a chosen leaf model (issue #534 step 3,
PR-J1-b; the `escalate` diagnostician's launches since PR-J4-a).

Usage:
    python3 tools/judge_replay.py <orchestration_id> <agent_run_id> \\
        --leaf pure|workshop --llm-config <path> \\
        [--mutate <json>] [--repeat N] [--label <text>] [--check]

The instrument of the judge A/B (parts 2 and 3 of the plan on issue #534). It reads
`launches/<agent_run_id>.request.json` of a recorded `compile.verify`, `generate.verify` or
`validate.judge` launch and reuses ONLY its `pure_context`, its `pure_shape`, the node refs it
names, its `runner_host_authored` stamp and its `resolved_dependencies` (the dependency facts,
reused verbatim, not re-resolved). Everything else of a launch is built fresh by the REAL
reviewer loop (`Conductor._run_pure_reviewer_substep`): a fresh `agent_run_id`, the replay's
own orchestration, the CURRENT prompt contract version and templates. Each repetition is one
reviewer substep, with its schema repairs, under the leaf model the configuration's entry for
that substep names; `--leaf` must agree with it (the configuration file is the only thing that
says what a leaf launches).

`--mutate` is a JSON object `{document_key: [{"old": "...", "new": "..."}, ...]}` (or
`@<path>` to a file holding one): each
replacement is applied to the recorded document in order and must match EXACTLY ONCE, or the
replay is refused before anything is launched. The same mutated context is handed to either
leaf model, so a seeded defect is the same defect on both arms.

The replay runs in its own orchestration, `workspace/orchestrations/replay_<ts>_<hex>/`,
created like a run's (`init`, the configuration snapshot, `preflight`), whose
`orchestration_meta.json#invocation.kind` is `judge_replay`. It writes NOTHING under
`workspace/ir/`, `workspace/pipelines/` or `workspace/runs/`: the refs it passes are the
recorded ones and the reviewer's two host writers are replaced by writers that keep the
document in memory. Its only result is `<replay dir>/replay_result.json`, one JSON line per
repetition; no audit or node-directory discovery reads a replay (`tools/audit_orchestration.py`
must not be run on one: it composes the SOURCE run's directories from the recorded requests).

Refusals print `{"status": "refused", "reason": <code>, "detail": ...}` and exit 2, before
any launch. Before the orchestration is created: `judge_replay_repeat_invalid` (`--repeat` below
1); `judge_replay_source_unreadable` / `judge_replay_source_ambiguous` (no such record, or an
arid prefix naming several); `judge_replay_pair_not_admitted` (not one of the three judging
pairs or the four diagnose pairs); `judge_replay_no_context` (no `pure_context`, or a non-string value: a warm repair
turn — replay its cold launch); `judge_replay_shape_unknown` (a `pure_shape` the current tables
do not declare); `judge_replay_context_keys_moved` (the key set is not the CURRENT one);
`judge_replay_mutation_invalid` / `_unknown_key` / `_not_once` / `_breaks_document` (below);
`judge_replay_spec_unresolved` (the source's `spec_ref` no longer resolves, through the catalog,
to the request's node — its spec VERSION moved since the record, so its replay window is
closed); `judge_replay_refs_invalid` (the request's `ir_ref` / `pipeline_ref` are not this
node's store refs); `judge_replay_llm_config_invalid`; `judge_replay_leaf_mismatch` (`--leaf`
disagrees with the configuration). After it is created, ending it `fail`:
`judge_replay_preflight_failed` (reason code `preflight_failed`) and `judge_replay_not_ready`
(a launch gate — the two `--check` answers — does not pass). An exception ends it `fail` /
`judge_replay_error` and propagates.

Each `replay_result.json` row: the head (`replay_orchestration_id`, `source_*`, `step`,
`substep`, `leaf`, `label`, `mutated_keys`, `repetition`); the outcome (`status`,
`leaf_returncode`, `infra_error`, `document` — the accepted verdict or review, else null —
and `failure_category`); `attempts` (the reviewer's `per_attempt` rows); `schema_repairs`
(turns launched because the previous document was refused for anything but a cut read) and
`cut_read_repairs` (turns launched because it was refused as a codex cut read, issue #547 —
the two are disjoint); the sums over the attempts of
`cost_usd`, `input_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`,
`output_tokens`, `leaf_wall_s`, `duration_ms` (claude envelope) and `model_turns`, each over the
attempts that recorded it, with `usage_unrecorded_attempts` counting the attempts whose usage
channel failed (a launch a bound killed leaves no envelope: the sums are then a lower bound;
a provider that reports tokens and no cost, as codex does, leaves `cost_usd` null without
counting there); on the pure
arm of a pure record `prompt_matches_record` (null on a mutated row); on the workshop arm
`cli_versions` and `coverage` (below): per seed `lines`, `covered`, `fraction` and `ranges`, the
covered lines as `[first, last]` runs — what the deep row's "was the defect's span read" is
checked against. A seeded defect in `bundle_document` lands in the expanded file named by its
`logical_path`, so its span is that file's lines in the lineage's tree, not the document's.

A DIAGNOSE row (`<phase>.diagnose`, PR-J4-a) is one launch through `escalate`'s own
(`Conductor._diagnose_turn`): no repair, and the row carries `directive` (`action`,
`target_phase`, `severity`, `repair_strategy`, `reason`; null when the launch gave none) in
place of `document`, the single attempt in `attempts`, and the same sums. The diagnostician
runs on `defaults`, which names no leaf model, so `--leaf` IS the arm there and is not compared
with the configuration: `workshop` runs `defaults` as `escalate` does (a reading lineage seeded
with `diagnosis.json`), `pure` runs it as a pure leaf over the pure template (until the pure path
is deleted). `--mutate` applies to `diagnosis_document` as to any context document.

Configuration: on a judging row `--leaf` must agree with the configuration's entry for the
substep, so the two arms of one A/B row take two files — the A/B's `llm.yaml` (workshop on the judging substeps)
and a copy with those substeps' `leaf: workshop` / `lineage_timeout_s` removed.

`--check` creates the orchestration and answers, without launching, whether a launch of this
row would pass the two launch gates that depend on the workspace rather than on the request:
the dependency readiness (`_dependency_ready`, recomputed against the CURRENT transformation
versions, so a row of a node whose dependency key moved answers
`derivation_key_mismatch:<input>`) and the downstream artifact gate. Exit 0 when both pass, 1
when either does not (2 is a refusal). A replay that is not ready is refused anyway, so
`--check` is the dry run over the A/B's rows, not a precondition of a launch.

A replay orchestration and its lineage homes stay after the run, like any run's (its trees
and transcripts are what `coverage` was computed from). Remove them when the measurement is
recorded: the homes with `tools/prune_workflow_homes.py --orchestration-id <replay id>
--delete` (a finished replay is terminal), the directory with `rm -r`.

Read coverage (workshop arm, claude): per seed, the fraction of its lines covered by the union,
over the transcript files of every lineage the repetition opened (a cold reuse or a re-run is a
lineage of its own), of `Read` result ranges and `Grep` content-mode hit lines
(`read_coverage`, below). These are CLI implementation details, not a contract: a `Read` result
that lacks its line triple makes the whole repetition `unmeasured`, never 0.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

#: The judging pairs a replay admits, and (issue #534 PR-J4-a) `escalate`'s diagnose launch of
#: each escalatable phase (`pure_leaf.WORKSHOP_DIAGNOSE_PAIRS`, asserted equal by
#: `test_judge_replay`).
JUDGING_PAIRS: tuple[tuple[str, str], ...] = (
    ("compile", "verify"), ("generate", "verify"), ("validate", "judge"))
DIAGNOSE_PAIRS: tuple[tuple[str, str], ...] = (
    ("build", "diagnose"), ("compile", "diagnose"), ("generate", "diagnose"),
    ("validate", "diagnose"))
ADMITTED_PAIRS: tuple[tuple[str, str], ...] = JUDGING_PAIRS + DIAGNOSE_PAIRS
#: `orchestration_meta.json#invocation.kind` of a replay orchestration.
REPLAY_KIND = "judge_replay"
REPLAY_RESULT_NAME = "replay_result.json"
LEAF_CHOICES = ("pure", "workshop")


class ReplayRefused(Exception):
    """A replay that cannot be run as asked; nothing has been launched."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# --- the source launch ------------------------------------------------------------------------

@dataclass(frozen=True)
class SourceLaunch:
    orchestration_id: str
    agent_run_id: str
    request: dict[str, Any]
    meta: dict[str, Any]
    #: The recorded rendered prompt, when the record kept one.
    prompt_text: str | None

    @property
    def pair(self) -> tuple[str, str]:
        return (str(self.request.get("step") or ""), str(self.request.get("substep") or ""))

    @property
    def shape(self) -> str:
        return str(self.request.get("pure_shape") or "")


def load_source(repo_root: Path, orchestration_id: str, agent_run_id: str) -> SourceLaunch:
    """The recorded request, the source orchestration's meta and the recorded prompt."""
    orch = repo_root / "workspace" / "orchestrations" / orchestration_id
    request_path = orch / "launches" / f"{agent_run_id}.request.json"
    if not request_path.is_file() and re.fullmatch(r"[0-9a-f-]{4,}", agent_run_id):
        # A unique prefix names the launch (the records are cited by their first 8 digits).
        found = sorted((orch / "launches").glob(f"{agent_run_id}*.request.json"))
        if len(found) > 1:
            raise ReplayRefused("judge_replay_source_ambiguous",
                                f"{agent_run_id!r} prefixes {len(found)} launches")
        if found:
            request_path = found[0]
            agent_run_id = request_path.name[: -len(".request.json")]
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        meta = json.loads((orch / "orchestration_meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReplayRefused("judge_replay_source_unreadable", str(exc)) from exc
    if not isinstance(request, dict) or not isinstance(meta, dict):
        raise ReplayRefused("judge_replay_source_unreadable",
                            f"{request_path} or its orchestration meta is not an object")
    prompt_path = orch / "launches" / f"{agent_run_id}.prompt.txt"
    prompt = prompt_path.read_text(encoding="utf-8") if prompt_path.is_file() else None
    return SourceLaunch(orchestration_id, agent_run_id, request, meta, prompt)


def check_source(source: SourceLaunch) -> None:
    """Refuse a row the replay cannot carry: not a judging pair, not a cold launch with its
    whole context, or a context whose key set is not the CURRENT one (the template's slots)."""
    from tools.orchestration_runtime import (
        PURE_CONTEXT_REQUIRED_KEYS, PURE_CONTEXT_REQUIRED_KEYS_BY_SHAPE)
    if source.pair not in ADMITTED_PAIRS:
        raise ReplayRefused(
            "judge_replay_pair_not_admitted",
            f"{'.'.join(source.pair)} is not one of "
            f"{', '.join('.'.join(p) for p in ADMITTED_PAIRS)}")
    context = source.request.get("pure_context")
    if not isinstance(context, dict) or not context:
        raise ReplayRefused(
            "judge_replay_no_context",
            "the recorded request carries no pure_context (a warm repair turn); replay the "
            "attempt's cold launch")
    if not all(isinstance(v, str) for v in context.values()):
        raise ReplayRefused("judge_replay_no_context", "a pure_context value is not a string")
    shape_key = (*source.pair, source.shape)
    if source.shape and shape_key not in PURE_CONTEXT_REQUIRED_KEYS_BY_SHAPE:
        raise ReplayRefused("judge_replay_shape_unknown",
                            f"pure_shape {source.shape!r} is not declared for "
                            f"{'.'.join(source.pair)}")
    required = (PURE_CONTEXT_REQUIRED_KEYS_BY_SHAPE[shape_key] if source.shape
                else PURE_CONTEXT_REQUIRED_KEYS[source.pair])
    if set(context) != set(required):
        raise ReplayRefused(
            "judge_replay_context_keys_moved",
            f"recorded keys {sorted(context)} != current keys {sorted(required)}")


def parse_mutations(text: str | None) -> dict[str, list[tuple[str, str]]]:
    """`--mutate`'s JSON (or `@<path>` to a file holding it) as ordered replacements."""
    if text is None:
        return {}
    raw = text
    if text.startswith("@"):
        try:
            raw = Path(text[1:]).read_text(encoding="utf-8")
        except OSError as exc:
            raise ReplayRefused("judge_replay_mutation_invalid", str(exc)) from exc
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise ReplayRefused("judge_replay_mutation_invalid", f"not JSON: {exc}") from exc
    if not isinstance(doc, dict) or not doc:
        raise ReplayRefused("judge_replay_mutation_invalid",
                            "expected a non-empty object {document_key: [{old, new}, ...]}")
    out: dict[str, list[tuple[str, str]]] = {}
    for key, items in doc.items():
        if not isinstance(items, list) or not items:
            raise ReplayRefused("judge_replay_mutation_invalid",
                                f"{key}: expected a non-empty list of {{old, new}}")
        pairs: list[tuple[str, str]] = []
        for item in items:
            if (not isinstance(item, dict) or set(item) != {"old", "new"}
                    or not isinstance(item["old"], str) or not isinstance(item["new"], str)
                    or not item["old"] or item["old"] == item["new"]):
                raise ReplayRefused(
                    "judge_replay_mutation_invalid",
                    f"{key}: each replacement is {{\"old\": <non-empty>, \"new\": <different>}}")
            pairs.append((item["old"], item["new"]))
        out[str(key)] = pairs
    return out


def apply_mutations(context: Mapping[str, str],
                    mutations: Mapping[str, list[tuple[str, str]]]) -> dict[str, str]:
    """The context with every replacement applied in order. Each `old` must occur EXACTLY ONCE
    in the document as the replacements before it left it: a zero or a double match refuses.
    The text matched is the document AS RECORDED: in a JSON document (the bundle under review)
    a source line break is the two characters `\\n`. A replacement that leaves a recorded JSON
    document unparseable is refused too: the workshop arm parses `bundle_document` to seed the
    bundle's files, so a broken one would give the pair a pure half and no workshop half; and
    every other JSON document a judge is shown was written and checked by the host, so an
    unparseable one is not a defect any run presents to a judge."""
    out = dict(context)
    for key, pairs in mutations.items():
        if key not in out:
            raise ReplayRefused("judge_replay_mutation_unknown_key",
                                f"{key!r} is not a document of this launch ({sorted(out)})")
        text = out[key]
        for index, (old, new) in enumerate(pairs):
            count = text.count(old)
            if count != 1:
                raise ReplayRefused(
                    "judge_replay_mutation_not_once",
                    f"{key}[{index}]: 'old' matches {count} times (exactly once required)")
            text = text.replace(old, new, 1)
        if _parses_as_json(out[key]) and not _parses_as_json(text):
            raise ReplayRefused(
                "judge_replay_mutation_breaks_document",
                f"{key}: the recorded document is JSON and the mutated one does not parse "
                "(spell a line break inside a JSON string as \\n)")
        out[key] = text
    return out


def _parses_as_json(text: str) -> bool:
    try:
        json.loads(text)
    except ValueError:
        return False
    return True


def node_refs_of(repo_root: Path, source: SourceLaunch) -> Any:
    """The node refs the recorded request names. The spec path is resolved from the source
    orchestration's `spec_ref` through the catalog and must name the request's node; the stage
    ids the request does not carry are read from the pipeline's `lineage.json`."""
    from tools.workflow_conductor import NodeRefs, node_key_safe, resolve_node
    req = source.request
    node_key = str(req.get("node_key") or "")
    spec_ref = str(source.meta.get("spec_ref") or "")
    try:
        resolved_key, spec_path = resolve_node(repo_root, spec_ref)
    except (ValueError, OSError) as exc:
        raise ReplayRefused("judge_replay_spec_unresolved", f"{spec_ref!r}: {exc}") from exc
    if resolved_key != node_key:
        raise ReplayRefused("judge_replay_spec_unresolved",
                            f"{spec_ref!r} resolves to {resolved_key}, the request names "
                            f"{node_key}")
    safe = node_key_safe(node_key)
    ir_parts = str(req.get("ir_ref") or "").split("/")
    pipe_parts = str(req.get("pipeline_ref") or "").split("/")
    if (len(ir_parts) != 4 or ir_parts[:3] != ["workspace", "ir", safe]
            or len(pipe_parts) != 5 or pipe_parts[:3] != ["workspace", "pipelines", safe]):
        raise ReplayRefused("judge_replay_refs_invalid",
                            f"ir_ref {req.get('ir_ref')!r} / pipeline_ref "
                            f"{req.get('pipeline_ref')!r} are not this node's store refs")
    lineage_path = repo_root / str(req["pipeline_ref"]) / "lineage.json"
    try:
        lineage = json.loads(lineage_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        lineage = {}
    lineage = lineage if isinstance(lineage, dict) else {}

    def _id(name: str) -> str | None:
        value = req.get(name) or lineage.get(name)
        return str(value) if isinstance(value, str) and value.strip() else None

    return NodeRefs(node_key=node_key, spec_path=spec_path, ir_id=ir_parts[3],
                    pipeline_id=pipe_parts[4], target_id=pipe_parts[3],
                    source_id=_id("source_id"), binary_id=_id("binary_id"),
                    run_id=_id("run_id"), source_binary_id=_id("binary_id"))


# --- the replay orchestration -----------------------------------------------------------------

def new_replay_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"replay_{ts}_{secrets.token_hex(4)}"


def replay_invocation(source: SourceLaunch, *, leaf: str, llm_config: Any,
                      argv: list[str], check: bool, label: str | None,
                      mutations: Mapping[str, list[tuple[str, str]]],
                      repo_root: Path) -> dict[str, Any]:
    """The replay's `invocation` record. The source's target block is copied (the launch gates
    read the target from it); its closure fields are NOT (a replay belongs to no closure)."""
    import shlex
    from tools.run_workflow import _repo_relative
    src_inv = source.meta.get("invocation")
    src_inv = src_inv if isinstance(src_inv, dict) else {}
    record: dict[str, Any] = {
        "kind": REPLAY_KIND,
        "argv": list(argv),
        "command": shlex.join(["python3", "tools/judge_replay.py", *argv]),
        "spec_ref": source.meta.get("spec_ref"),
        "mode": src_inv.get("mode") or "dev",
        "source_orchestration_id": source.orchestration_id,
        "source_agent_run_id": source.agent_run_id,
        "source_prompt_contract_version": source.request.get("prompt_contract_version"),
        "step": source.pair[0],
        "substep": source.pair[1],
        "leaf": leaf,
        "check_only": check,
        "label": label,
        "mutations": {k: [{"old": o, "new": n} for o, n in v] for k, v in mutations.items()},
        "llm_config_path": _repo_relative(llm_config.path, repo_root),
        "llm_config_sha256": llm_config.sha256,
        "llm_leaf_map": llm_config.provenance_map(),
    }
    if isinstance(src_inv.get("target"), dict):
        record["target"] = src_inv["target"]
    return record


@dataclass
class ReplayOrchestration:
    orchestration_id: str
    orchestration_agent_run_id: str
    env: dict[str, str]
    tmp_dir: Path

    def directory(self, repo_root: Path) -> Path:
        return repo_root / "workspace" / "orchestrations" / self.orchestration_id


def create_replay_orchestration(repo_root: Path, source: SourceLaunch, llm_config: Any,
                                invocation: dict[str, Any]) -> ReplayOrchestration:
    """`init`, the configuration snapshot and `preflight`, as `run_workflow.py` creates a
    run's orchestration; raises `ReplayRefused` when the preflight does not pass."""
    from tools.run_workflow import (
        _ensure_preflight_pass, _runtime_command, _write_llm_config_snapshot)
    oid = new_replay_id()
    workflow_mode = str(invocation.get("mode") or "dev")
    env = dict(os.environ)
    env["ATMOFAB_WORKFLOW_MODE"] = "1"
    env["ATMOFAB_WORKFLOW_EXEC_MODE"] = workflow_mode
    env["PYTHONPATH"] = str(repo_root) + (
        f":{env['PYTHONPATH']}" if env.get("PYTHONPATH") else "")
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    env["ATMOFAB_ORCHESTRATION_ID"] = oid
    llm, llm_command = llm_config.cli_launch_identity()
    init_args = [
        "init", "--repo-root", str(repo_root), "--orchestration-id", oid,
        "--spec-ref", str(source.meta.get("spec_ref") or ""),
        "--status", "running", "--agent-backend", llm,
        "--source-dependency-ref", str(source.meta.get("source_dependency_ref") or ""),
        "--invocation-json", json.dumps(invocation, ensure_ascii=False),
    ]
    init = _runtime_command(repo_root, env, init_args).payload
    orch_arid = str(init.get("orchestration_agent_run_id") or "").strip()
    if not orch_arid:
        raise RuntimeError("init returned no orchestration_agent_run_id")
    tmp_dir = repo_root / "workspace" / "tmp" / orch_arid
    env["TMPDIR"] = str(tmp_dir)
    env["ORCHESTRATION_AGENT_RUN_ID"] = orch_arid
    replay = ReplayOrchestration(oid, orch_arid, env, tmp_dir)
    try:
        _write_llm_config_snapshot(repo_root, oid, llm_config)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        preflight = _runtime_command(repo_root, env, [
            "preflight", "--repo-root", str(repo_root), "--orchestration-id", oid,
            "--backend", llm, "--agent-command", llm_command,
            "--llm-config", str(llm_config.path),
            "--llm-config-defaults-model", llm_config.defaults.model,
            "--llm-config-defaults-command", llm_config.defaults.command,
            "--llm-config-sha256", llm_config.sha256,
        ]).payload
    except BaseException as exc:
        # `init` committed: an orchestration left `running` would read as a live run.
        finish_replay_orchestration(repo_root, replay, "fail", "judge_replay_error",
                                    f"{type(exc).__name__}: {exc}")
        raise
    passed, detail = _ensure_preflight_pass(preflight)
    if not passed:
        finish_replay_orchestration(repo_root, replay, "fail", "preflight_failed", detail)
        raise ReplayRefused("judge_replay_preflight_failed", detail)
    return replay


def finish_replay_orchestration(repo_root: Path, replay: ReplayOrchestration, status: str,
                                reason_code: str | None = None,
                                detail: str | None = None) -> None:
    """Terminalize the replay orchestration and remove its scratch directory. An orchestration
    the runtime already terminalized (`record-launch` ends a refused launch `fail_closed`) keeps
    its status: a terminal-to-terminal transition is refused."""
    from tools.orchestration_runtime import IDEMPOTENT_TERMINAL_STATUSES
    from tools.run_workflow import _read_orchestration_meta, _runtime_command
    if _read_orchestration_meta(repo_root, replay.orchestration_id).get("status") in (
            IDEMPOTENT_TERMINAL_STATUSES):
        if replay.tmp_dir.exists():
            shutil.rmtree(replay.tmp_dir, ignore_errors=True)
        return
    args = ["set-status", "--repo-root", str(repo_root),
            "--orchestration-id", replay.orchestration_id, "--status", status]
    if reason_code:
        args += ["--reason-code", reason_code]
    if detail:
        args += ["--reason-detail", detail[:2000]]
    try:
        _runtime_command(repo_root, replay.env, args)
    finally:
        if replay.tmp_dir.exists():
            shutil.rmtree(replay.tmp_dir, ignore_errors=True)


def launch_gates(repo_root: Path, orchestration_id: str, source: SourceLaunch
                 ) -> dict[str, Any]:
    """The two workspace-dependent launch gates `record_launch` would run for this row."""
    from tools.orchestration_runtime import _dependency_ready, _downstream_phase_launch_gate
    step = source.pair[0]
    dep_ok, dep_reason = _dependency_ready(repo_root, orchestration_id, step=step)
    down_ok, down_reason = _downstream_phase_launch_gate(
        repo_root, node_key=str(source.request.get("node_key") or ""), step=step,
        pipeline_ref=str(source.request.get("pipeline_ref") or ""),
        launch_request=source.request)
    return {
        "ready": bool(dep_ok and down_ok),
        "dependency_ready": bool(dep_ok), "dependency_reason": dep_reason,
        "downstream_ready": bool(down_ok), "downstream_reason": down_reason,
    }


# --- read coverage ----------------------------------------------------------------------------

def _line_count(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


@dataclass
class _ToolCall:
    name: str
    input: dict[str, Any]
    result: Any = None
    is_error: bool = False
    #: A result attributable to this call was read (its line answers this call alone and
    #: carries the CLI's `toolUseResult`).
    has_result: bool = False
    #: Some line answered this call, attributable or not (the CLI's `api-request-blob` line
    #: repeats results with no `toolUseResult`).
    answered: bool = False


def _transcript_calls(files: Iterable[Path]) -> tuple[dict[str, _ToolCall], set[str]]:
    """Every tool call of the transcripts, by tool-use id (a block streamed twice, or a fork
    file repeating the cold file's history, is one call), and the CLI versions they carry."""
    calls: dict[str, _ToolCall] = {}
    versions: set[str] = set()
    for path in files:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            if isinstance(obj.get("version"), str):
                versions.add(obj["version"])
            message = obj.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            results = [b for b in content
                       if isinstance(b, dict) and b.get("type") == "tool_result"]
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                    calls.setdefault(block["id"], _ToolCall(
                        str(block.get("name") or ""),
                        block.get("input") if isinstance(block.get("input"), dict) else {}))
            # `toolUseResult` belongs to the line: it is attributable only when the line answers
            # one call. Only an attributable result settles a call, whatever the order the
            # files are read in, so a result-less repeat read FIRST does not hide the measured
            # one.
            attributable = len(results) == 1 and "toolUseResult" in obj
            for block in results:
                call = calls.get(str(block.get("tool_use_id") or ""))
                if call is None:
                    continue
                call.answered = True
                if call.has_result or not attributable:
                    continue
                call.has_result = True
                call.is_error = block.get("is_error") is True
                call.result = obj.get("toolUseResult")
    return calls, versions


_GREP_LINE = re.compile(r"(\d+)[:-](.*)\Z", re.S)
#: What the CLI prints in place of a matched or context line too long to show (measured on
#: CLI 2.1.309: `1:[Omitted long matching line]`): the line was not shown, so it was not read.
_GREP_OMITTED = re.compile(r"\[Omitted long [a-z ]*line\]")


def read_coverage(transcripts: Iterable[Path], tree_roots: Iterable[str],
                  seeds: Mapping[str, str]) -> dict[str, Any]:
    """Per seed, the fraction of its lines a reading lineage's transcripts show it read.

    A `Read` result covers `[startLine, startLine + numLines)`; an errored `Read` (the file is
    over the CLI's size limit) covers nothing; a `Read` result without the line triple makes
    the answer `unmeasured` (a `file_unchanged` result — the CLI's answer to a repeated Read
    of an unchanged file, which shows nothing new — covers nothing and is not one), and so do
    transcripts that hold no tool call at all (no
    transcript was found, or the CLI wrote it elsewhere): a row says 0 only when a read was
    observed to be absent. A `Grep` in `content` mode covers each numbered line it SHOWED
    (`N:` hits and `N-` context lines); a line the CLI omitted as too long, a `-o` (only the
    matched part) search and a `-n false` (unnumbered) search show no whole line and cover
    nothing, as do `count` / `files_with_matches` modes and `Glob`. A `Read` or `Grep` answered
    only by lines that carry no attributable result is `unmeasured` too. `tree_roots` are the lineages' tree directories as the leaf saw them; the tree is
    the leaf's working directory, so a path the CLI records RELATIVE (`tests.md`, `./tests.md`;
    CLI 2.1.309 records a relative `file_path` as given) names the same seed."""
    roots = [str(r).rstrip("/") for r in tree_roots]
    by_path: dict[str, str] = {}
    for name in seeds:
        by_path[name] = name
        by_path[f"./{name}"] = name
    for root in roots:
        for name in seeds:
            by_path[f"{root}/{name}"] = name
    covered: dict[str, set[int]] = {name: set() for name in seeds}
    calls, versions = _transcript_calls(transcripts)
    unmeasured: list[str] = []
    # Longest first, so a seed whose name prefixes another's is not matched for it.
    prefixes = sorted(by_path.items(), key=lambda kv: -len(kv[0]))
    names_longest = sorted(seeds, key=len, reverse=True)
    for tool_id, call in calls.items():
        if call.name in ("Read", "Grep") and call.answered and not call.has_result:
            # Answered, but by no line that says what was shown: not a measured "no read".
            unmeasured.append(tool_id)
            continue
        if call.name == "Read":
            if not call.has_result or call.is_error or not isinstance(call.result, dict):
                continue
            if call.result.get("type") == "file_unchanged":
                # A repeat of an earlier Read of an unchanged file: the CLI shows nothing new
                # and records no line triple; the earlier Read carries what was shown.
                continue
            file = call.result.get("file")
            triple = (file.get("filePath"), file.get("startLine"), file.get("numLines"),
                      file.get("totalLines")) if isinstance(file, dict) else (None,) * 4
            if not (isinstance(triple[0], str) and all(isinstance(v, int) for v in triple[1:])):
                unmeasured.append(tool_id)
                continue
            name = by_path.get(triple[0])
            if name is not None:
                covered[name].update(range(triple[1], triple[1] + triple[2]))
        elif call.name == "Grep":
            result = call.result
            if (not call.has_result or call.is_error or not isinstance(result, dict)
                    or result.get("mode") != "content"
                    or call.input.get("-o") is True or call.input.get("-n") is False):
                continue
            target = str(call.input.get("path") or "").rstrip("/")
            single = by_path.get(target)
            if single is None and (target in roots or target in ("", ".")):
                filenames = result.get("filenames")
                if isinstance(filenames, list) and len(filenames) == 1:
                    single = by_path.get(str(filenames[0]))
            for line in str(result.get("content") or "").splitlines():
                name, rest = None, line
                for path, seed in prefixes:
                    if line.startswith(path) and line[len(path):len(path) + 1] in (":", "-"):
                        name, rest = seed, line[len(path) + 1:]
                        break
                if name is None:
                    for seed in names_longest:
                        if line.startswith(seed) and line[len(seed):len(seed) + 1] in (":", "-"):
                            name, rest = seed, line[len(seed) + 1:]
                            break
                if name is None:
                    name = single
                match = _GREP_LINE.match(rest)
                if (name is not None and match
                        and not _GREP_OMITTED.fullmatch(match.group(2).strip())):
                    covered[name].add(int(match.group(1)))
    out: dict[str, Any] = {"cli_versions": sorted(versions)}
    if not calls:
        out["coverage"] = "unmeasured"
        out["unmeasured_reads"] = 0
        return out
    if unmeasured:
        out["coverage"] = "unmeasured"
        out["unmeasured_reads"] = len(unmeasured)
        return out
    per_seed: dict[str, Any] = {}
    for name, text in seeds.items():
        total = _line_count(text)
        lines = {n for n in covered[name] if 1 <= n <= total}
        per_seed[name] = {"lines": total, "covered": len(lines),
                          "fraction": (round(len(lines) / total, 4) if total else None),
                          "ranges": _ranges(lines)}
    out["coverage"] = per_seed
    return out


def _ranges(lines: set[int]) -> list[list[int]]:
    """The covered line numbers as inclusive `[first, last]` runs, so a defect's span can be
    checked against them (`all(any(a <= n <= b for a, b in ranges) for n in span)`)."""
    out: list[list[int]] = []
    for n in sorted(lines):
        if out and n == out[-1][1] + 1:
            out[-1][1] = n
        else:
            out.append([n, n])
    return out


# --- the run ----------------------------------------------------------------------------------

@dataclass
class _Captured:
    document: dict[str, Any] | None = None
    meta: dict[str, Any] = field(default_factory=dict)


def _replay_spec(conductor: Any, source: SourceLaunch, context: dict[str, str],
                 captured: _Captured) -> Any:
    """The reviewer's spec with the recorded context and the two host writers held in memory:
    nothing a replay decides reaches the store."""
    step, substep = source.pair
    spec = conductor._pure_reviewer_spec(step, substep, shape=source.shape or None)
    if spec.pure_shape != source.shape:
        raise ReplayRefused("judge_replay_shape_unknown",
                            f"recorded pure_shape {source.shape!r}; the spec carries "
                            f"{spec.pure_shape!r}")
    stamp = bool(source.request.get("runner_host_authored", False))

    def _write_project_meta(_refs: Any, document: dict[str, Any], **_kw: Any) -> None:
        captured.document = json.loads(json.dumps(document))

    def _write_meta(_refs: Any, **kwargs: Any) -> None:
        captured.meta = json.loads(json.dumps(kwargs, default=str))

    return spec._replace(build_context=lambda _refs: dict(context),
                         write_project_meta=_write_project_meta, write_meta=_write_meta,
                         runner_host_authored=lambda _refs: stamp)


def _envelope(repo_root: Path, oid: str, arid: str) -> dict[str, Any]:
    path = (repo_root / "workspace" / "orchestrations" / oid / "agents" / arid / "dialogs"
            / "leaf.stdout.log")
    try:
        doc = json.loads(path.read_text(encoding="utf-8").strip().splitlines()[-1])
    except (OSError, ValueError, IndexError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _as_source(text: str, oid: str, arid: str, source: SourceLaunch) -> str:
    """The replay's prompt with ITS identity tokens put back to the source's. Only the
    replay's own ids are rewritten: a recorded document may carry the SOURCE ids (a remote
    site's `remote_dir` names the source orchestration), and those stay as they are on both
    sides. The replay's ids are fresh, so the context cannot carry them."""
    return text.replace(oid, source.orchestration_id).replace(arid, source.agent_run_id)


def _sum(values: Iterable[Any]) -> float | None:
    nums = [float(v) for v in values if isinstance(v, (int, float))]
    return round(sum(nums), 6) if nums else None


def _repair_counts(attempts: list[dict[str, Any]]) -> dict[str, int]:
    """The turns launched because the previous one's document was refused (a repair, or the
    re-run of an unfinished review): an exhausted budget's last refusal launched nothing. A
    cut-read refusal (issue #547, codex) is counted apart, as `cut_read_repairs`: a reading
    cost on its own repair counter, not a defect of the document's shape."""
    from tools.workflow_conductor import WORKSHOP_CUT_READ
    before = [str(a.get("failure_category") or "") for a in attempts[:-1]]
    return {
        "schema_repairs": sum(1 for c in before
                              if c and c not in ("pure_transport", WORKSHOP_CUT_READ)),
        "cut_read_repairs": sum(1 for c in before if c == WORKSHOP_CUT_READ),
    }


def run_repetition(conductor: Any, repo_root: Path, source: SourceLaunch, refs: Any,
                   context: dict[str, str], repetition: int) -> dict[str, Any]:
    """One reviewer substep over the recorded context, and its row."""
    from tools.leaf_usage import LEAF_USAGE_UNAVAILABLE
    from tools.workshop_tree import LineagePaths
    from tools.operator_private_root import workflow_homes_root
    step, substep = source.pair
    captured = _Captured()
    spec = _replay_spec(conductor, source, context, captured)
    deps = source.request.get("resolved_dependencies")
    resolved = tuple(d for d in deps if isinstance(d, dict)) if isinstance(deps, list) else ()
    outcome = conductor._run_pure_reviewer_substep(refs, step, substep, resolved, spec)
    entry = conductor.entry_for(step, substep)
    attempts = list(captured.meta.get("per_attempt") or [])
    oid = conductor.orchestration_id
    envelopes = [_envelope(repo_root, oid, str(a.get("agent_run_id"))) for a in attempts]
    row: dict[str, Any] = {
        "repetition": repetition,
        "status": outcome.status,
        "leaf_returncode": outcome.leaf_returncode,
        "infra_error": list(outcome.infra_error) if outcome.infra_error else None,
        "document": captured.document,
        "failure_category": captured.meta.get("failure_category"),
        "attempts": attempts,
        **_repair_counts(attempts),
        "cost_usd": _sum((a.get("usage") or {}).get("cost_usd") for a in attempts),
        # An attempt a bound killed has no envelope, so its usage channel failed
        # (`leaf_usage.leaf_usage_unavailable`): the sums below leave it out, and this says how
        # many they left out (the sums are then a lower bound). An attempt whose usage was
        # recorded without a cost (a codex turn reports tokens and no money) is not counted.
        "usage_unrecorded_attempts": sum(
            1 for a in attempts
            if not isinstance(a.get("usage"), dict)
            or a["usage"].get("status") == LEAF_USAGE_UNAVAILABLE),
        **{name: _sum((a.get("usage") or {}).get(name) for a in attempts)
           for name in ("input_tokens", "cache_creation_input_tokens",
                        "cache_read_input_tokens", "output_tokens")},
        "leaf_wall_s": _sum(a.get("leaf_wall_s") for a in attempts),
        "duration_ms": _sum(e.get("duration_ms") for e in envelopes),
        "model_turns": (_sum(a.get("model_turns") for a in attempts) if entry.is_workshop
                        else _sum(e.get("num_turns") for e in envelopes)),
    }
    if (attempts and not entry.is_workshop and source.prompt_text is not None
            and source.request.get("leaf_mode") == "pure"):
        first = str(attempts[0].get("agent_run_id"))
        prompt_path = (repo_root / "workspace" / "orchestrations" / oid / "launches"
                       / f"{first}.prompt.txt")
        if context != source.request.get("pure_context"):
            # A mutated context renders a different prompt by design: the comparison says
            # nothing about the template, so it is not made (the unmutated row of the same
            # launch answers it).
            row["prompt_matches_record"] = None
        elif prompt_path.is_file():
            row["prompt_matches_record"] = (
                _as_source(prompt_path.read_text(encoding="utf-8"), oid, first, source)
                == source.prompt_text)
    if entry.is_workshop:
        seeds = conductor._workshop_seeds(
            refs, context, {"resolved_dependencies": list(resolved),
                            "workshop_provider": entry.provider}, spec)
        homes = workflow_homes_root()
        backend = conductor._workshop_backend(entry)
        trees: list[str] = []
        transcripts: list[Path] = []
        for lineage_id in dict.fromkeys(str(a.get("lineage_id")) for a in attempts
                                        if a.get("lineage_id")):
            paths = LineagePaths(homes_root=homes, orchestration_id=oid, lineage_id=lineage_id,
                                 orchestration_dir=repo_root / "workspace" / "orchestrations"
                                 / oid, backend=backend)
            trees.append(str(paths.tree))
            projects = paths.config / "projects"
            if projects.is_dir():
                transcripts += sorted(projects.glob("*/*.jsonl"))
        if entry.provider == "claude_cli":
            row.update(read_coverage(transcripts, trees, seeds))
        else:
            row["coverage"] = None
    return row


def run_diagnose_repetition(conductor: Any, repo_root: Path, source: SourceLaunch, refs: Any,
                            context: dict[str, str], repetition: int, *,
                            pure: bool) -> dict[str, Any]:
    """One diagnose launch over the recorded `diagnosis_document`, through `escalate`'s own
    launch (`Conductor._diagnose_turn`, issue #534 PR-J4-a) and its row: the DIRECTIVE in place
    of a document. The workshop arm runs on the configuration's `defaults` entry as `escalate`
    does; the pure arm on that entry as a pure one (`leaf` pure, no bound) — the arm is
    `--leaf`, since `defaults` names no leaf model. One launch, no repair, as in `escalate`."""
    import dataclasses

    from tools.leaf_usage import LEAF_USAGE_UNAVAILABLE
    from tools.llm_config import LEAF_PURE
    from tools.operator_private_root import workflow_homes_root
    from tools.workshop_tree import LineagePaths
    step, _substep = source.pair
    entry = conductor.entry_for(None, None)
    if pure:
        entry = dataclasses.replace(entry, leaf=LEAF_PURE, lineage_timeout_s=None,
                                    max_wrapper_calls=None)
    child = conductor.new_agent_run_id()
    request = conductor._diagnose_request(refs, step, entry, child,
                                          context["diagnosis_document"], pure=pure)
    turn = conductor._diagnose_turn(refs, request, entry, child, step, pure=pure)
    if turn is None:
        raise RuntimeError("the diagnose launch did not happen")
    conductor._finalize_diagnose_turn(refs, step, entry, child, turn)
    decision = turn.decision
    usage = turn.usage if isinstance(turn.usage, dict) else None
    attempt = {"agent_run_id": child, "model": turn.model, "usage": turn.usage,
               "lineage_id": turn.lineage_id, "leaf_rc": turn.proc.returncode,
               "leaf_wall_s": turn.leaf_wall_s}
    row: dict[str, Any] = {
        "repetition": repetition,
        "status": "pass" if decision is not None else "fail",
        "leaf_returncode": turn.proc.returncode,
        "directive": ({"action": decision.action, "target_phase": decision.target_phase,
                       "severity": decision.severity,
                       "repair_strategy": decision.repair_strategy,
                       "reason": decision.reason} if decision is not None else None),
        "attempts": [attempt],
        "cost_usd": _sum([(usage or {}).get("cost_usd")]),
        "usage_unrecorded_attempts": int(
            usage is None or usage.get("status") == LEAF_USAGE_UNAVAILABLE),
        **{name: _sum([(usage or {}).get(name)])
           for name in ("input_tokens", "cache_creation_input_tokens",
                        "cache_read_input_tokens", "output_tokens")},
        "leaf_wall_s": _sum([turn.leaf_wall_s]),
    }
    oid = conductor.orchestration_id
    if pure and source.prompt_text is not None and source.request.get("leaf_mode") == "pure":
        prompt_path = (repo_root / "workspace" / "orchestrations" / oid / "launches"
                       / f"{child}.prompt.txt")
        if context != source.request.get("pure_context"):
            row["prompt_matches_record"] = None
        elif prompt_path.is_file():
            row["prompt_matches_record"] = (
                _as_source(prompt_path.read_text(encoding="utf-8"), oid, child, source)
                == source.prompt_text)
    if not pure and turn.lineage_id is not None:
        if entry.provider == "claude_cli":
            seeds = conductor._workshop_seeds(refs, context, request,
                                              conductor._diagnose_spec())
            paths = LineagePaths(homes_root=workflow_homes_root(), orchestration_id=oid,
                                 lineage_id=turn.lineage_id,
                                 orchestration_dir=repo_root / "workspace" / "orchestrations"
                                 / oid, backend=conductor._workshop_backend(entry))
            projects = paths.config / "projects"
            transcripts = sorted(projects.glob("*/*.jsonl")) if projects.is_dir() else []
            row.update(read_coverage(transcripts, [str(paths.tree)], seeds))
        else:
            row["coverage"] = None
    return row


def _append_result(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def replay(repo_root: Path, args: argparse.Namespace, argv: list[str]) -> int:
    from tools.llm_config import LlmConfigError, load_llm_config
    from tools.target_profile import load_target_profile
    from tools.workflow_conductor import Conductor

    source = load_source(repo_root, args.orchestration_id, args.agent_run_id)
    check_source(source)
    mutations = parse_mutations(args.mutate)
    context = apply_mutations(source.request["pure_context"], mutations)
    refs = node_refs_of(repo_root, source)
    try:
        llm_config = load_llm_config(args.llm_config)
        llm_config.validate_runnable()
    except (LlmConfigError, OSError) as exc:
        raise ReplayRefused("judge_replay_llm_config_invalid", str(exc)) from exc
    step, substep = source.pair
    diagnose = source.pair in DIAGNOSE_PAIRS
    entry = llm_config.entry_for(step, substep)
    if diagnose:
        # The diagnostician runs on `defaults`, which names no leaf model (the loader makes it
        # a reading workshop entry): `--leaf` is the arm (`run_diagnose_repetition`).
        entry = llm_config.defaults
    elif entry.leaf != args.leaf:
        raise ReplayRefused(
            "judge_replay_leaf_mismatch",
            f"--leaf {args.leaf}, but {args.llm_config} runs {step}.{substep} as "
            f"leaf: {entry.leaf}")
    if entry.is_workshop and entry.is_http and not (diagnose and args.leaf == "pure"):
        # Issue #534 PR-J3-b (operator decision O3): an HTTP lineage's transcript is the host
        # loop's `loop_turns.jsonl`, which `read_coverage` does not read; refused before any
        # orchestration is created, `--check` included. A diagnose row's PURE arm opens no
        # lineage (it runs `defaults` as a pure leaf), so it is not refused.
        raise ReplayRefused(
            "judge_replay_http_lineage_unsupported",
            f"a replay of an HTTP lineage ({entry.provider}) reads `loop_turns.jsonl`, which "
            "this tool does not read yet (issue #534 follow-up)")
    target_id = refs.target_id
    invocation = replay_invocation(source, leaf=args.leaf, llm_config=llm_config, argv=argv,
                                   check=args.check, label=args.label, mutations=mutations,
                                   repo_root=repo_root)
    replay_orch = create_replay_orchestration(repo_root, source, llm_config, invocation)
    oid = replay_orch.orchestration_id
    result_path = replay_orch.directory(repo_root) / REPLAY_RESULT_NAME
    head = {
        "replay_orchestration_id": oid,
        "source_orchestration_id": source.orchestration_id,
        "source_agent_run_id": source.agent_run_id,
        "source_prompt_contract_version": source.request.get("prompt_contract_version"),
        "source_leaf_mode": source.request.get("leaf_mode"),
        "step": step, "substep": substep, "leaf": args.leaf, "label": args.label,
        "mutated_keys": sorted(mutations),
    }
    # A replay certifies nothing, so it never ends `pass` (which the runtime reserves for a
    # run whose every reserved node completed): a finished replay ends `cancel`, named.
    status, reason, detail = "cancel", "judge_replay_finished", None
    try:
        gates = launch_gates(repo_root, oid, source)
        if args.check:
            print(json.dumps({**head, "check": gates}, ensure_ascii=False))
            return 0 if gates["ready"] else 1
        if not gates["ready"]:
            # `record-launch` would refuse it and terminalize the orchestration `fail_closed`.
            status, reason = "fail", "judge_replay_not_ready"
            detail = json.dumps(gates, ensure_ascii=False)
            print(json.dumps({"status": "refused", "reason": reason, "detail": gates,
                              "replay_orchestration_id": oid}, ensure_ascii=False))
            return 2
        conductor = Conductor(
            repo_root=repo_root, orchestration_id=oid,
            orchestration_agent_run_id=replay_orch.orchestration_agent_run_id,
            env=replay_orch.env, workflow_mode=str(invocation["mode"]),
            llm_config=llm_config, target_profile=load_target_profile(repo_root, target_id))
        for repetition in range(1, args.repeat + 1):
            row = {**head, **(
                run_diagnose_repetition(conductor, repo_root, source, refs, context,
                                        repetition, pure=(args.leaf == "pure"))
                if diagnose else
                run_repetition(conductor, repo_root, source, refs, context, repetition))}
            _append_result(result_path, row)
            print(json.dumps({k: row.get(k) for k in (
                "replay_orchestration_id", "repetition", "status", "failure_category",
                "directive", "cost_usd", "leaf_wall_s") if k in row}, ensure_ascii=False),
                flush=True)
        return 0
    except BaseException as exc:
        status, reason, detail = "fail", "judge_replay_error", f"{type(exc).__name__}: {exc}"
        raise
    finally:
        finish_replay_orchestration(repo_root, replay_orch, status, reason, detail)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay one recorded judging launch under a chosen leaf model.")
    parser.add_argument("orchestration_id")
    parser.add_argument("agent_run_id")
    parser.add_argument("--leaf", required=True, choices=LEAF_CHOICES)
    parser.add_argument("--llm-config", required=True)
    parser.add_argument("--mutate", default=None,
                        help="JSON {document_key: [{old, new}, ...]}, or @<path> to one")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--label", default=None)
    parser.add_argument("--check", action="store_true",
                        help="answer the launch gates without launching")
    parser.add_argument("--repo-root", default=str(REPO_ROOT))
    return parser


def main(argv: list[str] | None = None) -> int:
    sys.dont_write_bytecode = True
    raw = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(raw)
    if args.repeat < 1:
        print(json.dumps({"status": "refused", "reason": "judge_replay_repeat_invalid",
                          "detail": "--repeat must be at least 1"}))
        return 2
    repo_root = Path(args.repo_root).resolve()
    try:
        return replay(repo_root, args, raw)
    except ReplayRefused as exc:
        print(json.dumps({"status": "refused", "reason": exc.code, "detail": exc.detail},
                         ensure_ascii=False))
        return 2


if __name__ == "__main__":
    # SIGTERM / SIGHUP (a `timeout`, a closed ssh session) become `SystemExit(143)`, as for
    # the driver (`run_workflow._install_signal_handlers`), so `replay`'s `finally`
    # terminalizes the replay orchestration instead of leaving it `running` — where
    # `run_workflow` would offer it as a run to resume.
    from tools.run_workflow import _install_signal_handlers
    _install_signal_handlers()
    sys.exit(main())
