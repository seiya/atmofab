"""The wrapper of a `workshop leaf`: the five operations it may ask the host to run.

`docs/design/producer_workshop_leaf.md` §Tool surface (issue #534): a `workshop leaf` derives,
compiles, lints, syntax-checks and runs through the host, never by itself. Each operation here
runs in the conductor's process, from a `WrapperContext` the conductor freezes in its main thread
before the leaf is launched (`Conductor._freeze_wrapper_context`), so a call — served on a
transport thread while the leaf runs — reads no conductor state and calls no conductor method.

What the leaf chooses is the CONTENT of its tree and, for `run`, a binary basename, a
tree-relative case file and case ids. Every path and every build-runtime argument is composed
here, under the lineage's own directories, so the build-runtime library keeps its type-only
validation (`docs/BUILD_RUNTIME.md` §Operations rules, issue #457). Every build-runtime call runs
under the wrapper's sandbox (`orchestration_runtime.render_wrapper_sandbox_prefix`), because the
toolchain, the lint tool and above all the built binary act on leaf-authored content.

Results are FEEDBACK. The host's own gates run on the snapshot and are the authority; nothing a
gate reads is written here. Every call is counted against the lineage's `max_wrapper_calls` and
refused once the lineage's `lineage_timeout_s` has run out; either bound ends the lineage.

Neutral core (`docs/BACKEND_BOUNDARY.md`): every toolchain fact is a value the conductor froze
from the registry.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from tools import workshop_tree
from tools.workshop_tree import AssemblyFailure, LineagePaths

#: The operations, in the order the leaf is told them.
WRAPPER_OPS: tuple[str, ...] = ("derive", "compile", "lint", "syntax_check", "run")

#: The categories a refused call carries. A bound's category ends the lineage.
CATEGORY_MAX_WRAPPER_CALLS = "max_wrapper_calls"
CATEGORY_LINEAGE_TIMEOUT = "lineage_timeout"
CATEGORY_LINEAGE_ENDED = "lineage_ended"
CATEGORY_BAD_ARGUMENTS = "wrapper_arguments_invalid"
CATEGORY_NOT_DERIVED = "control_file_not_derived"
CATEGORY_WRAPPER_ERROR = "wrapper_error"
CATEGORY_RUN_UNAVAILABLE = "run_unavailable"
CATEGORY_STAGED_CLOSURE = "staged_closure_incomplete"
CATEGORY_BUILD_GRAPH = "build_graph_failed"

#: The text a leaf is shown of a command's output, per stream.
DEFAULT_CAPTURE_LIMIT = 20000

_BINARY_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def wrapper_base_env(host_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment every wrapper run is declared (`--clearenv` drops the rest): the host's
    `PATH`, so the toolchain resolves as it does for the host's own gates, and a `HOME` and
    `TMPDIR` on the run's private `/tmp`."""
    source = os.environ if host_env is None else host_env
    return {
        "PATH": str(source.get("PATH") or "/usr/bin:/bin"),
        "HOME": "/tmp",
        "TMPDIR": "/tmp",
        "LANG": "C.UTF-8",
    }


@dataclass(frozen=True)
class WrapperContext:
    """Everything an operation reads, frozen before the launch."""

    paths: LineagePaths
    repo_root: Path
    orchestration_id: str
    agent_run_id: str
    #: Tree paths a bundle may not name (`workshop_tree.reserved_tree_names`).
    reserved_names: frozenset[str]
    control_file_basename: str
    #: (document) -> None, or (category, findings): `pure_bundle_contract_violation`, bound.
    violations: Callable[[dict[str, Any]], tuple[str, str] | None]
    #: (document) -> the build graph: `derive_build_graph`, bound.
    build_graph: Callable[[dict[str, Any]], dict[str, Any]]
    #: (graph=...) -> the control file's text: the build system's `render_from_graph`, bound
    #: (keyword-only, as that renderer takes it).
    render_control_file: Callable[[dict[str, Any]], str]
    #: The basenames the staged closure placed in `staged/obj` (sources and headers).
    closure_file_names: tuple[str, ...]
    language: str
    build_system: str
    #: The build system's command-line overrides for the staged object / binary directories.
    build_overrides: tuple[str, ...]
    lint_preset: str
    syntax_compiler: str
    syntax_std: str
    syntax_architecture: str | None
    syntax_parallel_backend: str | None
    syntax_staged_suffixes: tuple[str, ...]
    #: The local launch shape (`host_execution.launch_shape`), or None with `run_unavailable`.
    launch_argv_prefix: tuple[str, ...] | None
    launch_env: tuple[tuple[str, str], ...]
    run_unavailable: str | None
    base_env: tuple[tuple[str, str], ...]
    toolchain_ro_roots: tuple[str, ...]
    max_wrapper_calls: int
    #: Epoch seconds at which the lineage's `lineage_timeout_s` runs out.
    lineage_deadline: float
    capture_limit: int = DEFAULT_CAPTURE_LIMIT
    #: The longest a single run may take, before the lineage's remaining time shortens it.
    run_timeout_sec: int = field(default=3600)
    compile_timeout_sec: int = field(default=1800)


def _result(ok: bool, **fields: Any) -> dict[str, Any]:
    return {"ok": ok, **{k: v for k, v in fields.items() if v is not None}}


def _command_feedback(result: Mapping[str, Any]) -> dict[str, Any]:
    """What the leaf is shown of one build-runtime call."""
    stdout = str(result.get("stdout") or "")
    stderr = str(result.get("stderr") or "")
    truncated = "...<omitted " in stdout or "...<omitted " in stderr
    return _result(bool(result.get("ok")), rc=result.get("return_code"), stdout=stdout,
                   stderr=stderr, truncated=truncated or None, error=result.get("error"))


def _remaining_seconds(ctx: WrapperContext) -> float:
    return ctx.lineage_deadline - time.time()


def _bounded_timeout(ctx: WrapperContext, default: int) -> int:
    return max(1, min(int(default), int(_remaining_seconds(ctx))))


def _sandbox(ctx: WrapperContext, *, project_dir: Path, rw: list[Path],
             ro: tuple[Path, ...] | list[Path] = (), env: Mapping[str, str],
             declared: tuple[str, ...] = ()) -> list[str]:
    from tools.orchestration_runtime import render_wrapper_sandbox_prefix

    return render_wrapper_sandbox_prefix(
        repo_root=ctx.repo_root, project_dir=project_dir, rw_binds=rw, ro_binds=list(ro),
        toolchain_ro_roots=ctx.toolchain_ro_roots, env=env, declared_env_names=declared)


def _attribution(ctx: WrapperContext) -> dict[str, str]:
    return {"orchestration_id": ctx.orchestration_id, "agent_run_id": ctx.agent_run_id}


def run_wrapper_op(ctx: WrapperContext, op: str, arguments: Mapping[str, Any] | None = None, *,
                   on_spawn: Callable[[int], None] | None = None) -> dict[str, Any]:
    """Run one operation and return its JSON-able result. Never raises.

    The bounds come first: a lineage whose time has run out, or whose call budget is spent, is
    ended (`ended_by` recorded) and the call is refused with `ended: True`, which the transport
    turns into the end of the leaf. `on_spawn` is told the process-group id of every sandboxed
    command as it starts, so the host can kill a run still in flight when it ends the lineage."""
    if op not in WRAPPER_OPS:
        return _result(False, category=CATEGORY_BAD_ARGUMENTS,
                       findings=f"unknown operation {op!r}; the operations are {WRAPPER_OPS}")
    args = dict(arguments or {})
    try:
        ended_by = workshop_tree.read_lineage_record(ctx.paths).get("ended_by")
        if ended_by is None and _remaining_seconds(ctx) <= 0:
            ended_by = workshop_tree.end_lineage(ctx.paths, "lineage_timeout")
        if ended_by is None and not workshop_tree.count_wrapper_call(
                ctx.paths, ctx.max_wrapper_calls):
            ended_by = workshop_tree.read_lineage_record(ctx.paths).get("ended_by")
        if ended_by is not None:
            # The recorded reason answers, whichever bound this call would also have hit.
            return _ended(ctx, str(ended_by))
        handler = _OPS[op]
        return handler(ctx, args, on_spawn)
    except Exception as exc:  # noqa: BLE001 - every failure is feedback, never a raise
        return _result(False, category=CATEGORY_WRAPPER_ERROR,
                       findings=f"{type(exc).__name__}: {exc}")


def _ended(ctx: WrapperContext, ended_by: str) -> dict[str, Any]:
    if ended_by == "max_wrapper_calls":
        return _result(False, category=CATEGORY_MAX_WRAPPER_CALLS, ended=True,
                       findings=f"the lineage has used its max_wrapper_calls="
                                f"{ctx.max_wrapper_calls}; the tree is taken as it stands")
    if ended_by == "lineage_timeout":
        return _result(False, category=CATEGORY_LINEAGE_TIMEOUT, ended=True,
                       findings="the lineage's lineage_timeout_s has run out; the tree is "
                                "taken as it stands")
    return _result(False, category=CATEGORY_LINEAGE_ENDED, ended=True,
                   findings=f"the lineage has ended ({ended_by})")


def _no_arguments(op: str, args: Mapping[str, Any]) -> dict[str, Any] | None:
    if args:
        return _result(False, category=CATEGORY_BAD_ARGUMENTS,
                       findings=f"{op} takes no arguments (got {sorted(args)})")
    return None


def _op_derive(ctx: WrapperContext, args: Mapping[str, Any],
               on_spawn: Any) -> dict[str, Any]:
    """Assemble the tree's manifest into a bundle document, judge it by the producer's
    contract, derive its build graph and write the control file into the tree."""
    refused = _no_arguments("derive", args)
    if refused is not None:
        return refused
    started = _utc_now_iso()
    outcome = _derive(ctx)
    workshop_tree.append_jsonl(ctx.paths.command_log, {
        "version": 1,
        "command_id": uuid.uuid4().hex,
        "tool_name": "derive",
        "started_at_utc": started,
        "ended_at_utc": _utc_now_iso(),
        "ok": bool(outcome.get("ok")),
        **({"category": outcome["category"]} if "category" in outcome else {}),
        **_attribution(ctx),
    })
    return outcome


def _derive(ctx: WrapperContext) -> dict[str, Any]:
    doc = workshop_tree.assemble_bundle_document(ctx.paths.tree, ctx.reserved_names)
    if isinstance(doc, AssemblyFailure):
        return _result(False, category=doc.category, findings=doc.findings)
    violation = ctx.violations(doc)
    if violation is not None:
        category, findings = violation
        return _result(False, category=category, findings=findings)
    try:
        graph = ctx.build_graph(doc)
    except RuntimeError as exc:
        return _result(False, category=CATEGORY_BUILD_GRAPH, findings=str(exc))
    workshop_tree.write_host_file(ctx.paths.tree, ctx.control_file_basename,
                                  ctx.render_control_file(graph=graph))
    missing = [n for n in ctx.closure_file_names if not (ctx.paths.staged_obj / n).is_file()]
    if missing:
        return _result(False, category=CATEGORY_STAGED_CLOSURE,
                       findings=f"the staged dependency closure lacks {missing} (a host fault)")
    files = [str(e.get("logical_path")) for e in doc.get("files") or [] if isinstance(e, dict)]
    return _result(True, control_file=ctx.control_file_basename, files=files)


def _require_derived(ctx: WrapperContext) -> dict[str, Any] | None:
    if not workshop_tree.is_host_file(ctx.paths.tree, ctx.control_file_basename):
        return _result(False, category=CATEGORY_NOT_DERIVED,
                       findings="run derive first: the tree holds no derived control file")
    return None


def _op_compile(ctx: WrapperContext, args: Mapping[str, Any],
                on_spawn: Any) -> dict[str, Any]:
    from tools.build_runtime import tool_compile_project

    refused = _no_arguments("compile", args) or _require_derived(ctx)
    if refused is not None:
        return refused
    env = dict(ctx.base_env)
    result = tool_compile_project({
        "project_dir": str(ctx.paths.tree),
        "language": ctx.language,
        "build_system": ctx.build_system,
        "extra_args": list(ctx.build_overrides),
        "env": env,
        "command_log_path": str(ctx.paths.command_log),
        "capture_limit": ctx.capture_limit,
        "timeout_sec": _bounded_timeout(ctx, ctx.compile_timeout_sec),
        "sandbox_argv_prefix": _sandbox(
            ctx, project_dir=ctx.paths.tree, rw=[ctx.paths.tree, ctx.paths.staged], env=env),
        "on_spawn": on_spawn,
        **_attribution(ctx),
    })
    binaries = sorted(p.name for p in ctx.paths.staged_bin.iterdir() if p.is_file())
    return _command_feedback(result) | {"binaries": binaries}


def _op_lint(ctx: WrapperContext, args: Mapping[str, Any], on_spawn: Any) -> dict[str, Any]:
    from tools.build_runtime import tool_run_linter

    refused = _no_arguments("lint", args)
    if refused is not None:
        return refused
    env = dict(ctx.base_env)
    result = tool_run_linter({
        "preset": ctx.lint_preset,
        "project_dir": str(ctx.paths.tree),
        "env": env,
        "command_log_path": str(ctx.paths.command_log),
        "capture_limit": ctx.capture_limit,
        "timeout_sec": _bounded_timeout(ctx, ctx.compile_timeout_sec),
        "sandbox_argv_prefix": _sandbox(ctx, project_dir=ctx.paths.tree,
                                        rw=[ctx.paths.tree], env=env),
        "on_spawn": on_spawn,
        **_attribution(ctx),
    })
    if "runs" in result:
        return _result(bool(result.get("ok")), preset=ctx.lint_preset,
                       runs=[{"sub_preset": r.get("sub_preset"), **_command_feedback(r)}
                             for r in result.get("runs") or []])
    return _command_feedback(result) | {"preset": ctx.lint_preset}


def _op_syntax_check(ctx: WrapperContext, args: Mapping[str, Any],
                     on_spawn: Any) -> dict[str, Any]:
    from tools.build_runtime import SyntaxSourceNameError, tool_run_syntax_check
    from tools.workflow_conductor import stage_syntax_inputs

    refused = _no_arguments("syntax_check", args)
    if refused is not None:
        return refused
    stage_dir = ctx.paths.syntax / ctx.syntax_compiler
    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    stage_dir.mkdir(parents=True)
    stage_syntax_inputs(ctx.paths.tree, stage_dir, ctx.syntax_staged_suffixes)
    for name in ctx.closure_file_names:
        shutil.copy2(ctx.paths.staged_obj / name, stage_dir / name)
    env = dict(ctx.base_env)
    try:
        result = tool_run_syntax_check({
            "compiler": ctx.syntax_compiler,
            "std": ctx.syntax_std,
            "architecture": ctx.syntax_architecture,
            "parallel_backend": ctx.syntax_parallel_backend,
            "project_dir": str(stage_dir),
            "env": env,
            "command_log_path": str(ctx.paths.command_log),
            "capture_limit": ctx.capture_limit,
            "timeout_sec": _bounded_timeout(ctx, ctx.compile_timeout_sec),
            "sandbox_argv_prefix": _sandbox(ctx, project_dir=stage_dir, rw=[stage_dir],
                                            env=env),
            "on_spawn": on_spawn,
            **_attribution(ctx),
        })
    except SyntaxSourceNameError as exc:
        return _result(False, category="syntax_error", findings=str(exc))
    if result.get("skipped"):
        return _result(True, skipped=True, reason=result.get("reason"),
                       compiler=ctx.syntax_compiler)
    return _command_feedback(result) | {"compiler": ctx.syntax_compiler}


def _tree_file(ctx: WrapperContext, name: Any) -> tuple[Path | None, str | None]:
    rel = workshop_tree.plain_relative_path(name) if isinstance(name, str) else None
    if rel is None:
        return None, f"cases_file {name!r} must be a tree-relative path with no '..' segment"
    data, why = workshop_tree.read_regular_file_under(ctx.paths.tree, PurePosixPath(rel))
    if data is None:
        return None, f"cases_file {name} {why}"
    return ctx.paths.tree / rel, None


def _op_run(ctx: WrapperContext, args: Mapping[str, Any], on_spawn: Any) -> dict[str, Any]:
    """Run a binary the leaf's `compile` built, on a case file of its tree."""
    from tools.build_runtime import tool_run_program
    from tools.spec_input_gates import CASE_ID_TOKEN_RE

    if ctx.run_unavailable is not None or ctx.launch_argv_prefix is None:
        return _result(False, category=CATEGORY_RUN_UNAVAILABLE,
                       findings=str(ctx.run_unavailable or "no launch shape"))
    unknown = sorted(set(args) - {"binary", "cases_file", "case_ids"})
    if unknown:
        return _result(False, category=CATEGORY_BAD_ARGUMENTS,
                       findings=f"run takes binary, cases_file, case_ids (got {unknown})")
    binary = args.get("binary")
    if not (isinstance(binary, str) and _BINARY_NAME_RE.fullmatch(binary)):
        return _result(False, category=CATEGORY_BAD_ARGUMENTS,
                       findings=f"binary must be the basename of a built binary (got {binary!r})")
    binary_path = ctx.paths.staged_bin / binary
    if binary_path.is_symlink() or not binary_path.is_file():
        built = sorted(p.name for p in ctx.paths.staged_bin.iterdir() if p.is_file())
        return _result(False, category=CATEGORY_BAD_ARGUMENTS,
                       findings=f"no built binary {binary!r}; compile built {built}")
    cases_path, why = _tree_file(ctx, args.get("cases_file", "spec.ir.yaml"))
    if cases_path is None:
        return _result(False, category=CATEGORY_BAD_ARGUMENTS, findings=why)
    case_ids = args.get("case_ids", [])
    if not (isinstance(case_ids, list)
            and all(isinstance(c, str) and CASE_ID_TOKEN_RE.fullmatch(c) for c in case_ids)):
        return _result(False, category=CATEGORY_BAD_ARGUMENTS,
                       findings="case_ids must be a list of case ids")
    env = dict(ctx.base_env) | dict(ctx.launch_env)
    command = [*ctx.launch_argv_prefix, str(binary_path), "--cases", str(cases_path), *case_ids]
    result = tool_run_program({
        "command": command,
        "project_dir": str(ctx.paths.tree),
        "env": env,
        "command_log_path": str(ctx.paths.command_log),
        "capture_limit": ctx.capture_limit,
        "timeout_sec": _bounded_timeout(ctx, ctx.run_timeout_sec),
        "sandbox_argv_prefix": _sandbox(
            ctx, project_dir=ctx.paths.tree, rw=[ctx.paths.tree], ro=[ctx.paths.staged_bin],
            env=env, declared=tuple(k for k, _ in ctx.launch_env)),
        "on_spawn": on_spawn,
        **_attribution(ctx),
    })
    return _command_feedback(result)


_OPS: dict[str, Callable[[WrapperContext, Mapping[str, Any], Any], dict[str, Any]]] = {
    "derive": _op_derive,
    "compile": _op_compile,
    "lint": _op_lint,
    "syntax_check": _op_syntax_check,
    "run": _op_run,
}


def result_text(result: Mapping[str, Any]) -> str:
    """The JSON text a transport returns to the leaf for one result."""
    return json.dumps(result, ensure_ascii=False, sort_keys=True)
