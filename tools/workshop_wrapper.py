"""The wrapper of a `workshop leaf`: the operations it may ask the host to run.

`docs/design/producer_workshop_leaf.md` §Tool surface (issue #534): a `generate.generate`
`workshop leaf` derives, compiles, lints, syntax-checks and runs through the host, never by
itself (`WRAPPER_OPS`); a `compile.generate` one has its IR checked the way `Compile.static`
will check it (`IR_CHECK_OPS`, step 2). Each operation here
runs in the conductor's process, from a `WrapperContext` the conductor freezes in its main thread
before the leaf is launched (`Conductor._freeze_wrapper_context` / `_freeze_ir_check_context`),
so a call — served on a transport thread while the leaf runs — reads no conductor state. The
one conductor method it calls is the IR contract an IR context binds
(`Conductor._pure_ir_document_violations`, and the declaration reader beside it), which reads
only the frozen node refs and class constants.

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
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from tools import workshop_tree
from tools.workshop_tree import AssemblyFailure, LineagePaths

#: The operations of a bundle (`generate.generate`) lineage, in the order the leaf is told them.
WRAPPER_OPS: tuple[str, ...] = ("derive", "compile", "lint", "syntax_check", "run")
#: The operation of an IR (`compile.generate`) lineage.
IR_CHECK_OPS: tuple[str, ...] = ("check_ir",)
#: Every operation any lineage may serve.
ALL_OPS: tuple[str, ...] = WRAPPER_OPS + IR_CHECK_OPS

#: The categories a refused call carries. A bound's category ends the lineage.
CATEGORY_MAX_WRAPPER_CALLS = "max_wrapper_calls"
CATEGORY_LINEAGE_TIMEOUT = "lineage_timeout"
CATEGORY_LINEAGE_ENDED = "lineage_ended"
CATEGORY_BAD_ARGUMENTS = "wrapper_arguments_invalid"
CATEGORY_WRAPPER_ERROR = "wrapper_error"
CATEGORY_RUN_UNAVAILABLE = "run_unavailable"
CATEGORY_STAGED_CLOSURE = "staged_closure_incomplete"
CATEGORY_BUILD_GRAPH = "build_graph_failed"
CATEGORY_IR_STATIC = "compile_static_violation"

#: The text a leaf is shown of a command's output, per stream.
DEFAULT_CAPTURE_LIMIT = 20000

_BINARY_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")

#: One lock per lineage home: the operations share the lineage's view, stage, object and
#: binary directories, which each call empties and refills, so two calls of one lineage (a
#: harness's sub-agents share the surface, `docs/design/producer_workshop_leaf.md`) run one
#: after the other rather than through each other's directories.
_OP_LOCKS: dict[str, threading.Lock] = {}
_OP_LOCKS_GUARD = threading.Lock()


def _op_lock(ctx: "WrapperContext") -> threading.Lock:
    with _OP_LOCKS_GUARD:
        return _OP_LOCKS.setdefault(str(ctx.paths.home), threading.Lock())


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def wrapper_base_env(host_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment every wrapper run is declared (`--clearenv` drops the rest): the host's
    `PATH`, so the toolchain resolves as it does for the host's own gates, a `HOME` and
    `TMPDIR` on the run's private `/tmp`, and `LANG=C.UTF-8`."""
    source = os.environ if host_env is None else host_env
    return {
        "PATH": str(source.get("PATH") or "/usr/bin:/bin"),
        "HOME": "/tmp",
        "TMPDIR": "/tmp",
        "LANG": "C.UTF-8",
    }


@dataclass(frozen=True)
class WrapperContext:
    """Everything an operation reads, frozen before the launch.

    One class for both producing substeps (issue #534 step 2): `ops` names the operations the
    lineage serves — `WRAPPER_OPS` on a `generate.generate` lineage, `IR_CHECK_OPS` on a
    `compile.generate` one — and each operation reads only the fields of its own kind. The
    fields below the common block default to "absent", and an operation whose fields are absent
    is refused by `run_wrapper_op` before it is counted, because `ops` does not name it."""

    paths: LineagePaths
    repo_root: Path
    orchestration_id: str
    agent_run_id: str
    #: Tree paths a document may not name (`workshop_tree.reserved_tree_names`).
    reserved_names: frozenset[str]
    #: The seeds' tree paths: the host-placed files a source view carries beside the bundle's.
    seed_names: tuple[str, ...]
    max_wrapper_calls: int
    #: Epoch seconds at which the lineage's `lineage_timeout_s` runs out.
    lineage_deadline: float
    #: The operations this lineage serves, in the order the leaf is told them.
    ops: tuple[str, ...] = WRAPPER_OPS
    #: (document) -> None, or (category, findings): `pure_bundle_contract_violation` bound on a
    #: bundle lineage, `Conductor._pure_ir_document_violations` bound on an IR one.
    violations: Callable[[dict[str, Any]], tuple[str, str] | None] | None = None
    # --- a bundle lineage (`WRAPPER_OPS`) ---------------------------------------------------
    control_file_basename: str = ""
    #: (document) -> the build graph: `derive_build_graph`, bound.
    build_graph: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    #: (graph=...) -> the control file's text: the build system's `render_from_graph`, bound
    #: (keyword-only, as that renderer takes it).
    render_control_file: Callable[..., str] | None = None
    #: The basenames the staged closure placed in `staged/closure` (sources and headers).
    closure_file_names: tuple[str, ...] = ()
    language: str = ""
    build_system: str = ""
    #: The build system's command-line overrides for the staged object / binary directories.
    build_overrides: tuple[str, ...] = ()
    lint_preset: str = ""
    syntax_compiler: str = ""
    syntax_std: str = ""
    syntax_architecture: str | None = None
    syntax_parallel_backend: str | None = None
    syntax_staged_suffixes: tuple[str, ...] = ()
    #: The node's model file and its host-rendered interface header (None for a language
    #: with none): what the model-alone probe stages.
    model_basename: str = ""
    interface_header_basename: str | None = None
    #: The local launch shape (`host_execution.launch_shape`), or None with `run_unavailable`.
    launch_argv_prefix: tuple[str, ...] | None = None
    launch_env: tuple[tuple[str, str], ...] = ()
    run_unavailable: str | None = None
    #: The directories `Validate.execute` creates under the run's working directory before it
    #: runs the binary (`Conductor._run_raw_dirs`), as one nested path.
    run_raw_dirs: tuple[str, ...] = ()
    base_env: tuple[tuple[str, str], ...] = ()
    toolchain_ro_roots: tuple[str, ...] = ()
    # --- an IR lineage (`IR_CHECK_OPS`) -----------------------------------------------------
    #: (tree, reserved names) -> the document, or an `AssemblyFailure`:
    #: `workshop_tree.assemble_ir_document`.
    assemble: Callable[[Path, frozenset[str]], Any] | None = None
    #: (document) -> the leaf's own "cannot be completed" declaration as one line, or None:
    #: `Conductor._pure_ir_declared_fail`.
    declared_fail: Callable[[dict[str, Any]], str | None] | None = None
    #: (ir object) -> the IR text the host would write: `Conductor._pure_ir_yaml`.
    ir_yaml: Callable[[dict[str, Any]], str] | None = None
    #: (ir_meta text) the pending `ir_meta.json` the host would write beside it.
    ir_meta_text: str = ""
    #: `(name, text)` of the host's sidecars the validators read beside the IR
    #: (`dependency_graph.json`, `dependency_surface.json`), copied from `<ir_ref>`.
    sidecar_texts: tuple[tuple[str, str], ...] = ()
    #: (repo_root, ir directory) -> the `Compile.static` violations:
    #: `validate_pipeline_semantics.validate_compile_ir_dir`.
    validate_ir_dir: Callable[[Path, Path], list[str]] | None = None
    # --- both -------------------------------------------------------------------------------
    #: `(name, text)` of every seed the host re-places before each operation (the contract
    #: copy excepted, which no operation reads), so an operation reads the host's seeds rather
    #: than a copy the leaf edited (issue #534 PR-3). Empty: nothing is re-placed.
    seed_texts: tuple[tuple[str, str], ...] = ()
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
    (issue #534 PR-2's) turns into the end of the leaf. `on_spawn` is told the process-group id of every sandboxed
    command as it starts, so the host can kill a run still in flight when it ends the lineage."""
    if op not in ctx.ops or op not in _OPS:
        # Before the bounds: a call to an operation this lineage does not serve is not counted
        # and ends nothing (a bundle lineage has no `check_ir`, an IR one no `compile`).
        return _result(False, category=CATEGORY_BAD_ARGUMENTS,
                       findings=f"unknown operation {op!r}; this lineage's operations are "
                                f"{list(ctx.ops)}")
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
        with _op_lock(ctx):
            _replace_seeds(ctx)
            return handler(ctx, args, on_spawn)
    except Exception as exc:  # noqa: BLE001 - every failure is feedback, never a raise
        return _result(False, category=CATEGORY_WRAPPER_ERROR,
                       findings=f"{type(exc).__name__}: {exc}")


def _replace_seeds(ctx: WrapperContext) -> None:
    """Write every frozen seed back into the tree, through no link, before an operation runs:
    the runner, the headers and `spec.ir.yaml` an operation reads are the host's, whatever the
    leaf did to its copies."""
    for name, text in ctx.seed_texts:
        workshop_tree.write_host_file(ctx.paths.tree, name, text)


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
    outcome = _derive_outcome(ctx)
    if not outcome.get("ok"):
        # A failed derive leaves no control file behind, so the tree never shows the leaf a
        # control file derived from an EARLIER manifest. (`compile` derives for itself.)
        target = ctx.paths.tree / ctx.control_file_basename
        if target.is_symlink() or target.is_file():
            target.unlink()
    return outcome


def _derivation(ctx: WrapperContext) -> tuple[dict[str, Any], str] | dict[str, Any]:
    """`(document, control file text)` for the tree as it stands — the host's Generate-time
    derivation: assembly, the producer's contract, the build graph, the control file — or the
    failure as feedback."""
    if ctx.violations is None or ctx.build_graph is None or ctx.render_control_file is None:
        return _result(False, category=CATEGORY_WRAPPER_ERROR,
                       findings="this lineage serves no bundle operation (a host fault)")
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
    missing = [n for n in ctx.closure_file_names
               if not (ctx.paths.staged_closure / n).is_file()]
    if missing:
        return _result(False, category=CATEGORY_STAGED_CLOSURE,
                       findings=f"the staged dependency closure lacks {missing} (a host fault)")
    return doc, ctx.render_control_file(graph=graph)


def _derive_outcome(ctx: WrapperContext) -> dict[str, Any]:
    derived = _derivation(ctx)
    if isinstance(derived, dict):
        return derived
    doc, control = derived
    workshop_tree.write_host_file(ctx.paths.tree, ctx.control_file_basename, control)
    files = [str(e.get("logical_path")) for e in doc.get("files") or [] if isinstance(e, dict)]
    return _result(True, control_file=ctx.control_file_basename, files=files)


def _fresh_dir(path: Path) -> Path:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


def _op_compile(ctx: WrapperContext, args: Mapping[str, Any],
                on_spawn: Any) -> dict[str, Any]:
    """Build the tree as the host's `Build` would build its snapshot: the derivation of the
    tree as it stands NOW (`_derivation`; not an earlier `derive`'s control file, and not a
    control file the leaf edited), in a source view of the manifest's files and the seeds
    with the host-rendered control file beside them (what `src/` holds), into an EMPTY object
    directory holding the staged closure and an empty binary directory, so no object, module
    file or binary of an earlier compile answers for this tree (the host builds in a fresh
    `workspace/tmp/<arid>/build` every time)."""
    from tools.build_runtime import tool_compile_project

    refused = _no_arguments("compile", args)
    if refused is not None:
        return refused
    derived = _derivation(ctx)
    if isinstance(derived, dict):
        return derived
    doc, control = derived
    view = _source_view(ctx, "_build", doc)
    if isinstance(view, dict):
        return view
    workshop_tree.write_host_file(view, ctx.control_file_basename, control)
    _fresh_dir(ctx.paths.staged_obj)
    _fresh_dir(ctx.paths.staged_bin)
    for name in ctx.closure_file_names:
        shutil.copy2(ctx.paths.staged_closure / name, ctx.paths.staged_obj / name)
    env = dict(ctx.base_env)
    result = tool_compile_project({
        "project_dir": str(view),
        "language": ctx.language,
        "build_system": ctx.build_system,
        "extra_args": list(ctx.build_overrides),
        "env": env,
        "command_log_path": str(ctx.paths.command_log),
        "capture_limit": ctx.capture_limit,
        "timeout_sec": _bounded_timeout(ctx, ctx.compile_timeout_sec),
        "sandbox_argv_prefix": _sandbox(
            ctx, project_dir=view, rw=[view, ctx.paths.staged_obj, ctx.paths.staged_bin],
            env=env),
        "on_spawn": on_spawn,
        **_attribution(ctx),
    })
    binaries = sorted(p.name for p in ctx.paths.staged_bin.iterdir() if p.is_file())
    return _command_feedback(result) | {"binaries": binaries}


def _source_view(ctx: WrapperContext, name: str,
                 doc: dict[str, Any] | None = None) -> Path | dict[str, Any]:
    """What the host's `Generate.gate` reads, laid out for one lint / syntax call: the files
    the manifest names, with the content assembly reads (`assemble_bundle_document`), plus
    the seeds — among them the host-placed files `src/` also holds (the runner, the headers),
    and the contract documents `src/` does not hold, which no linter or compiler reads. A file of
    the tree the manifest does not name (a note, a scratch source) is not in it, as it would
    not be in `src/`. `doc` is an assembly the caller already holds. Returns the view
    directory, or the assembly's failure as feedback."""
    if doc is None:
        assembled = workshop_tree.assemble_bundle_document(ctx.paths.tree, ctx.reserved_names)
        if isinstance(assembled, AssemblyFailure):
            return _result(False, category=assembled.category, findings=assembled.findings)
        doc = assembled
    view = _fresh_dir(ctx.paths.syntax / name)
    for entry in doc.get("files") or []:
        if not isinstance(entry, dict):
            continue
        logical, content = entry.get("logical_path"), entry.get("content")
        rel = workshop_tree.plain_relative_path(logical) if isinstance(logical, str) else None
        if rel is None or not isinstance(content, str):
            continue
        (view / rel).parent.mkdir(parents=True, exist_ok=True)
        (view / rel).write_text(content, encoding="utf-8")
    workshop_tree._copy_files(
        ctx.paths.tree, view,
        [PurePosixPath(n) for n in ctx.seed_names if workshop_tree.plain_relative_path(n)])
    return view


def _op_lint(ctx: WrapperContext, args: Mapping[str, Any], on_spawn: Any) -> dict[str, Any]:
    from tools.build_runtime import tool_run_linter

    refused = _no_arguments("lint", args)
    if refused is not None:
        return refused
    view = _source_view(ctx, "_lint")
    if isinstance(view, dict):
        return view
    env = dict(ctx.base_env)
    result = tool_run_linter({
        "preset": ctx.lint_preset,
        "project_dir": str(view),
        "env": env,
        "command_log_path": str(ctx.paths.command_log),
        "capture_limit": ctx.capture_limit,
        "timeout_sec": _bounded_timeout(ctx, ctx.compile_timeout_sec),
        "sandbox_argv_prefix": _sandbox(ctx, project_dir=view, rw=[view], env=env),
        "on_spawn": on_spawn,
        **_attribution(ctx),
    })
    if "runs" in result:
        return _result(bool(result.get("ok")), preset=ctx.lint_preset,
                       runs=[{"sub_preset": r.get("sub_preset"), **_command_feedback(r)}
                             for r in result.get("runs") or []])
    return _command_feedback(result) | {"preset": ctx.lint_preset}


def _syntax_run(ctx: WrapperContext, stage_dir: Path, on_spawn: Any) -> dict[str, Any]:
    from tools.build_runtime import tool_run_syntax_check

    env = dict(ctx.base_env)
    return tool_run_syntax_check({
        "compiler": ctx.syntax_compiler,
        "std": ctx.syntax_std,
        "architecture": ctx.syntax_architecture,
        "parallel_backend": ctx.syntax_parallel_backend,
        "project_dir": str(stage_dir),
        "env": env,
        "command_log_path": str(ctx.paths.command_log),
        "capture_limit": ctx.capture_limit,
        "timeout_sec": _bounded_timeout(ctx, ctx.compile_timeout_sec),
        "sandbox_argv_prefix": _sandbox(ctx, project_dir=stage_dir, rw=[stage_dir], env=env),
        "on_spawn": on_spawn,
        **_attribution(ctx),
    })


def _op_syntax_check(ctx: WrapperContext, args: Mapping[str, Any],
                     on_spawn: Any) -> dict[str, Any]:
    """The host gate's mandatory syntax stage over the source view (`_source_view`, what
    `src/` would hold), then — when it passes — its model-alone probe (issue #389): the same
    compiler over exactly what a node depending on this one is staged, the closure, the model
    file and the node's interface header. The view is staged as
    `workflow_conductor.stage_syntax_inputs` stages `src/` (every file of a staged suffix, at
    its own relative path)."""
    from tools.build_runtime import SyntaxSourceNameError

    refused = _no_arguments("syntax_check", args)
    if refused is not None:
        return refused
    view = _source_view(ctx, "_src")
    if isinstance(view, dict):
        return view
    stage_dir = _fresh_dir(ctx.paths.syntax / ctx.syntax_compiler)
    files, _links = workshop_tree._walk_tree_files(view)
    workshop_tree._copy_files(
        view, stage_dir,
        [rel for rel in files if rel.suffix.lower() in ctx.syntax_staged_suffixes])
    for name in ctx.closure_file_names:
        shutil.copy2(ctx.paths.staged_closure / name, stage_dir / name)
    try:
        result = _syntax_run(ctx, stage_dir, on_spawn)
    except SyntaxSourceNameError as exc:
        return _result(False, category="syntax_error", findings=str(exc))
    if result.get("skipped"):
        return _result(True, skipped=True, reason=result.get("reason"),
                       compiler=ctx.syntax_compiler)
    out = _command_feedback(result) | {"compiler": ctx.syntax_compiler}
    if not out["ok"]:
        return out
    model, _why = workshop_tree.read_regular_file_under(view, PurePosixPath(ctx.model_basename))
    if model is None:
        # A missing model is `derive`'s finding (the bundle contract), not this probe's.
        return out
    alone_dir = _fresh_dir(ctx.paths.syntax / f"{ctx.syntax_compiler}_model_alone")
    for name in ctx.closure_file_names:
        shutil.copy2(ctx.paths.staged_closure / name, alone_dir / name)
    (alone_dir / ctx.model_basename).write_bytes(model)
    if ctx.interface_header_basename:
        header, _ = workshop_tree.read_regular_file_under(
            view, PurePosixPath(ctx.interface_header_basename))
        if header is not None:
            (alone_dir / ctx.interface_header_basename).write_bytes(header)
    alone = _command_feedback(_syntax_run(ctx, alone_dir, on_spawn))
    if not alone["ok"]:
        alone["findings"] = (
            f"the whole tree passed, but {ctx.model_basename} alone did not: a node that "
            f"depends on this one is staged exactly the model file (and its interface header) "
            f"with the dependency closure, so the model may not use or include any other file "
            f"of this bundle; move what it needs into the model file")
    return _result(bool(alone["ok"]), compiler=ctx.syntax_compiler, rc=out.get("rc"),
                   stdout=out.get("stdout"), stderr=out.get("stderr"), model_alone=alone)


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
    if ctx.run_raw_dirs:
        current = ctx.paths.tree
        for part in ctx.run_raw_dirs:
            current = current / part
            if current.is_symlink() or (current.exists() and not current.is_dir()):
                return _result(False, category=CATEGORY_BAD_ARGUMENTS,
                               findings=f"{current.relative_to(ctx.paths.tree)} must be a "
                                        f"directory of the tree (the run writes its captures "
                                        f"there)")
        ctx.paths.tree.joinpath(*ctx.run_raw_dirs).mkdir(parents=True, exist_ok=True)
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


def _op_check_ir(ctx: WrapperContext, args: Mapping[str, Any],
                 on_spawn: Any) -> dict[str, Any]:
    """Check the tree's IR the way the host will (issue #534 step 2): assemble it
    (`workshop_tree.assemble_ir_document`), judge the document by the producer's contract
    (`Conductor._pure_ir_document_violations`), and — when it carries an IR rather than a
    declaration — run the `Compile.static` validators over a scratch IR directory holding the
    host-normalized `spec.ir.yaml`, a pending `ir_meta.json` and the host's sidecars, as
    `<ir_ref>` would hold them. Feedback only: the host's own `Compile.static` runs on the
    snapshot and is the authority. No subprocess (`on_spawn` is unused).

    The scratch directory is the lineage home's `check/`, outside the tree, emptied on every
    call. The validators read `tests.md` and the controlled spec through the IR's
    `meta.source_refs`, which the document contract has already pinned to the node's own spec
    documents (step (2) below), so they read the host's copies under `repo_root` — never a seed
    the leaf edited, nor a copy it wrote into its tree."""
    refused = _no_arguments("check_ir", args)
    if refused is not None:
        return refused
    if (ctx.assemble is None or ctx.violations is None or ctx.declared_fail is None
            or ctx.ir_yaml is None or ctx.validate_ir_dir is None):
        return _result(False, category=CATEGORY_WRAPPER_ERROR,
                       findings="check_ir is not served by this lineage (a host fault)")
    started = _utc_now_iso()
    outcome = _check_ir(ctx)
    workshop_tree.append_jsonl(ctx.paths.command_log, {
        "version": 1,
        "command_id": uuid.uuid4().hex,
        "tool_name": "check_ir",
        "started_at_utc": started,
        "ended_at_utc": _utc_now_iso(),
        "ok": bool(outcome.get("ok")),
        **({"category": outcome["category"]} if "category" in outcome else {}),
        **_attribution(ctx),
    })
    return outcome


def _check_ir(ctx: WrapperContext) -> dict[str, Any]:
    assert (ctx.assemble is not None and ctx.violations is not None
            and ctx.declared_fail is not None and ctx.ir_yaml is not None
            and ctx.validate_ir_dir is not None)
    doc = ctx.assemble(ctx.paths.tree, ctx.reserved_names)
    if isinstance(doc, AssemblyFailure):
        return _result(False, category=doc.category, findings=doc.findings)
    violation = ctx.violations(doc)
    if violation is not None:
        category, findings = violation
        return _result(False, category=category, findings=findings)
    declared = ctx.declared_fail(doc)
    if declared is not None:
        # The host would take its third exit on this tree: no IR, the declaration recorded.
        return _result(True, declared_fail=declared)
    scratch = _fresh_dir(ctx.paths.home / "check")
    (scratch / "spec.ir.yaml").write_text(ctx.ir_yaml(doc["ir"]), encoding="utf-8")
    (scratch / "ir_meta.json").write_text(ctx.ir_meta_text, encoding="utf-8")
    for name, text in ctx.sidecar_texts:
        (scratch / name).write_text(text, encoding="utf-8")
    violations = [str(v) for v in ctx.validate_ir_dir(ctx.repo_root, scratch)]
    if not violations:
        return _result(True)
    # The HEAD is kept: the validators report in their own order, and the first findings are
    # the ones a later one may follow from.
    text = "\n".join(violations)
    truncated = len(text) > ctx.capture_limit
    if truncated:
        text = text[:ctx.capture_limit] + f"...<omitted {len(text) - ctx.capture_limit} chars>"
    return _result(False, category=CATEGORY_IR_STATIC, findings=text,
                   truncated=truncated or None)


_OPS: dict[str, Callable[[WrapperContext, Mapping[str, Any], Any], dict[str, Any]]] = {
    "check_ir": _op_check_ir,
    "derive": _op_derive,
    "compile": _op_compile,
    "lint": _op_lint,
    "syntax_check": _op_syntax_check,
    "run": _op_run,
}


def result_text(result: Mapping[str, Any]) -> str:
    """The JSON text a transport returns to the leaf for one result."""
    return json.dumps(result, ensure_ascii=False, sort_keys=True)
