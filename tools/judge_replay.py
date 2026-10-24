#!/usr/bin/env python3
"""Replay one recorded JUDGING launch under a chosen leaf model (issue #534 step 3, PR-J1-b).

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

`--mutate` is a JSON object `{document_key: [{"old": "...", "new": "..."}, ...]}`: each
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

`--check` creates the orchestration and answers, without launching, whether a launch of this
row would pass the two launch gates that depend on the workspace rather than on the request:
the dependency readiness (`_dependency_ready`, recomputed against the CURRENT transformation
versions, so a row of a node whose dependency key moved answers
`derivation_key_mismatch:<input>`) and the downstream artifact gate. Exit 0 when both pass.

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

#: The judging pairs a replay admits. `escalate`'s diagnose launch is replayed in PR-J4.
ADMITTED_PAIRS: tuple[tuple[str, str], ...] = (
    ("compile", "verify"), ("generate", "verify"), ("validate", "judge"))
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
    required = (PURE_CONTEXT_REQUIRED_KEYS_BY_SHAPE[shape_key]
                if source.shape and shape_key in PURE_CONTEXT_REQUIRED_KEYS_BY_SHAPE
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
    in the document as the replacements before it left it: a zero or a double match refuses."""
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
        out[key] = text
    return out


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
    """Terminalize the replay orchestration and remove its scratch directory."""
    from tools.run_workflow import _runtime_command
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
    has_result: bool = False


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
            for block in results:
                call = calls.get(str(block.get("tool_use_id") or ""))
                if call is None or call.has_result:
                    continue
                call.has_result = True
                call.is_error = block.get("is_error") is True
                # `toolUseResult` belongs to the line; it is attributable only when the line
                # answers one call.
                call.result = obj.get("toolUseResult") if len(results) == 1 else None
    return calls, versions


_GREP_LINE = re.compile(r"(\d+)[:-]")


def read_coverage(transcripts: Iterable[Path], tree_roots: Iterable[str],
                  seeds: Mapping[str, str]) -> dict[str, Any]:
    """Per seed, the fraction of its lines a reading lineage's transcripts show it read.

    A `Read` result covers `[startLine, startLine + numLines)`; an errored `Read` (the file is
    over the CLI's size limit) covers nothing; a `Read` result without the line triple makes
    the answer `unmeasured`. A `Grep` in `content` mode covers each numbered line it returned
    (`N:` hits and `N-` context lines); `count` / `files_with_matches` modes and `Glob` cover
    nothing. `tree_roots` are the lineages' tree directories as the leaf saw them."""
    roots = [str(r).rstrip("/") for r in tree_roots]
    by_path: dict[str, str] = {}
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
        if call.name == "Read":
            if not call.has_result or call.is_error or not isinstance(call.result, dict):
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
                    or result.get("mode") != "content"):
                continue
            target = str(call.input.get("path") or "").rstrip("/")
            single = by_path.get(target)
            if single is None and target in roots:
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
                if name is not None and match:
                    covered[name].add(int(match.group(1)))
    out: dict[str, Any] = {"cli_versions": sorted(versions)}
    if unmeasured:
        out["coverage"] = "unmeasured"
        out["unmeasured_reads"] = len(unmeasured)
        return out
    per_seed: dict[str, Any] = {}
    for name, text in seeds.items():
        total = _line_count(text)
        lines = {n for n in covered[name] if 1 <= n <= total}
        per_seed[name] = {"lines": total, "covered": len(lines),
                          "fraction": (round(len(lines) / total, 4) if total else None)}
    out["coverage"] = per_seed
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


def _identity_free(text: str, oid: str, arid: str) -> str:
    return text.replace(oid, "<orchestration_id>").replace(arid, "<agent_run_id>")


def _sum(values: Iterable[Any]) -> float | None:
    nums = [float(v) for v in values if isinstance(v, (int, float))]
    return round(sum(nums), 6) if nums else None


def run_repetition(conductor: Any, repo_root: Path, source: SourceLaunch, refs: Any,
                   context: dict[str, str], repetition: int) -> dict[str, Any]:
    """One reviewer substep over the recorded context, and its row."""
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
        "schema_repairs": sum(1 for a in attempts
                              if a.get("failure_category")
                              and a.get("failure_category") != "pure_transport"),
        "cost_usd": _sum((a.get("usage") or {}).get("cost_usd") for a in attempts),
        "output_tokens": _sum((a.get("usage") or {}).get("output_tokens") for a in attempts),
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
        if prompt_path.is_file():
            row["prompt_matches_record"] = (
                _identity_free(prompt_path.read_text(encoding="utf-8"), oid, first)
                == _identity_free(source.prompt_text, source.orchestration_id,
                                  source.agent_run_id))
    if entry.is_workshop:
        seeds = conductor._workshop_seeds(
            refs, context, {"resolved_dependencies": list(resolved)}, spec)
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
    entry = llm_config.entry_for(step, substep)
    if entry.leaf != args.leaf:
        raise ReplayRefused(
            "judge_replay_leaf_mismatch",
            f"--leaf {args.leaf}, but {args.llm_config} runs {step}.{substep} as "
            f"leaf: {entry.leaf}")
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
        conductor = Conductor(
            repo_root=repo_root, orchestration_id=oid,
            orchestration_agent_run_id=replay_orch.orchestration_agent_run_id,
            env=replay_orch.env, workflow_mode=str(invocation["mode"]),
            llm_config=llm_config, target_profile=load_target_profile(repo_root, target_id))
        for repetition in range(1, args.repeat + 1):
            row = {**head, **run_repetition(conductor, repo_root, source, refs, context,
                                            repetition)}
            _append_result(result_path, row)
            print(json.dumps({k: row[k] for k in (
                "replay_orchestration_id", "repetition", "status", "failure_category",
                "cost_usd", "leaf_wall_s")}, ensure_ascii=False), flush=True)
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
    sys.exit(main())
