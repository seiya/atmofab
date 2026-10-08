#!/usr/bin/env python3
"""The build-runtime library: compile / run / quality check / lint / syntax check.

The conductor imports this module and calls its five `tool_*` entry points in-process
(`docs/BUILD_RUNTIME.md` is canonical for them and for the validation they apply). It was
an MCP server until issue #444; no leaf has called it since Z4 (issue #171), so the
protocol layer was deleted and the functions stayed.

It has no THIRD-PARTY dependencies. It reads one module of this checkout,
`tools/backends/registry.py` (by dotted import, because the registry resolves a backend
package by module path), which is stdlib-only.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any


@lru_cache(maxsize=1)
def _backend_registry() -> Any:
    """Load `tools/backends/registry.py`, the one door to a backend package.

    A DOTTED import: the registry resolves a backend by importing its dotted module path, so
    `tools` has to be importable as a package. This module is itself imported as
    `tools.build_runtime` (issue #444), so it always is. (Until then it was loaded by file
    location from `mcp_servers/`, and a `sys.path` bootstrap here put the checkout root on
    the path; with the module inside `tools/` nothing reaches that branch, and it went.)

    The registry is stdlib-only and imports no sibling at module level, so it is cheap and
    introduces no cycle (`tools/host_prerequisites.py`
    imports THIS module; this module imports the registry; the registry imports nothing).
    """
    from tools.backends import registry

    return registry


def _bounded_int(raw: Any, default: int, minimum: int, name: str) -> int:
    """An integer argument, held to `minimum`; below it is refused rather than clamped."""
    if raw is None:
        return default
    value = int(raw)
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum} (got {value})")
    return value


def _build_syntax_source_re(suffixes: tuple[str, ...]) -> re.Pattern[str]:
    """A source name: a file name carrying one of the language's source suffixes.

    `suffixes` is the language's `syntax_promotions.SOURCE_SUFFIXES`, the set auto-discovery
    also filters on, so an added suffix cannot make auto-discovery accept a file an explicit
    `sources` list refuses."""
    alternation = "|".join(re.escape(s.lstrip(".")) for s in suffixes)
    return re.compile(rf"^[A-Za-z0-9_][A-Za-z0-9_.+-]*\.({alternation})$", re.IGNORECASE)


class SyntaxSourceNameError(ValueError):
    """A staged file whose NAME the syntax check refuses.

    Its own class because the caller's response differs: the leaf authored the name and
    can rename it, so `Generate.gate` records this as a content failure, while every
    other argument refusal from this module is the caller's own bug and must stay a
    transport failure. Catching by `ValueError` there would blame the leaf for both."""


def _validate_syntax_sources(sources: list[str], project_dir: str, tool_name: str, *,
                             suffixes: tuple[str, ...], language: str) -> None:
    """Constrain the source list appended to the compiler front-end argv.

    A source is a file of the adapter's language (`suffixes`) sitting in `project_dir`. Anything else the driver would
    accept there — an option, a response file, a path out of the directory, a symlink
    to one — makes the check compile something other than what was staged, and it
    reports `ok: True` either way. Refused in every mode; the workflow never passes
    this argument at all, and an operator passing it means file names.
    """
    root = Path(project_dir).resolve()
    source_re = _build_syntax_source_re(suffixes)
    offending = []
    for name in sources:
        if not source_re.match(name):
            offending.append(name)
            continue
        resolved = (root / name).resolve()
        if resolved.parent != root or not resolved.is_file():
            offending.append(name)
    if offending:
        message = (
            f"{tool_name} sources must be {language} source files in project_dir; "
            "refused: " + ", ".join(sorted(offending))
        )
        # Auto-discovery walks at any depth (`compile_order`) so that a nested source reaches
        # this rule instead of being skipped while the build still compiles and links it
        # (issue #420). The refusal is the only place the author learns the rule, so it says
        # what to do: the conductor hands this text to the leaf as its failure excerpt.
        if any("/" in name for name in offending):
            message += (
                "; the syntax check compiles only the top level of project_dir, so a source "
                "below it is never checked — move every source file to the top level of the "
                "directory"
            )
        raise SyntaxSourceNameError(message)


DEFAULT_COMMAND_LOG_FILE = "command_log.jsonl"

def _is_compiled_language(language: str) -> bool:
    """Whether `language` is compiled — its language backend's `bundle_facts.COMPILED`. A
    compiled language needs a build tool that tracks dependencies between its sources; which
    languages are compiled is the backend's fact, not a set spelled here (issue #289)."""
    return bool(_backend_registry().is_compiled_language((language or "").strip().lower()))


DEPENDENCY_AWARE_BUILD_SYSTEMS = {
    "make",
    "cmake",
    "meson",
    "ninja",
    "cargo",
    "go",
    "gradle",
    "maven",
    "npm",
    "pnpm",
    "poetry",
}


def _trim(text: str, limit: int) -> str:
    if limit < 0:
        return text
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-(limit // 2) :]
    omitted = len(text) - len(head) - len(tail)
    return f"{head}\n...<omitted {omitted} chars>...\n{tail}"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _resolve_command_log_path(project_dir: str, command_log_path: str | None) -> Path:
    base_dir = Path(project_dir).resolve()
    if command_log_path is None or not str(command_log_path).strip():
        return base_dir / DEFAULT_COMMAND_LOG_FILE

    raw_path = Path(str(command_log_path))
    if raw_path.is_absolute():
        return raw_path
    return base_dir / raw_path


def _path_to_ref(path: Path) -> str | None:
    repo_root = Path.cwd().resolve()
    try:
        relative = path.resolve().relative_to(repo_root)
    except ValueError:
        return None
    return relative.as_posix()


def _append_command_log(log_path: Path, entry: dict[str, Any]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False))
        stream.write("\n")


def _attribution(args: dict[str, Any]) -> dict[str, str]:
    """The run this call belongs to, as the command log records it.

    Both fields are optional and neither decides anything: the same command runs
    with them, without them, and with any value in them. They are here so a line
    in `command_log.jsonl` can be traced back to the orchestration and agent run that
    issued it — the only part of the retired capability gate (issue #171) anything
    downstream reads (`docs/workflow/COMMAND_LOG_PLACEMENT.md`).
    """
    out: dict[str, str] = {}
    for key in ("orchestration_id", "agent_run_id"):
        raw = args.get(key)
        if raw is not None and str(raw).strip():
            out[key] = str(raw).strip()
    return out


def _run_command(
    command: list[str],
    cwd: str,
    tool_name: str,
    timeout_sec: int,
    env: dict[str, str] | None,
    capture_limit: int,
    command_log_path: str | None,
    attribution: dict[str, str] | None = None,
) -> dict[str, Any]:
    if not command:
        raise ValueError("command must not be empty")

    path = Path(cwd)
    if not path.exists():
        raise ValueError(f"project_dir does not exist: {cwd}")
    if not path.is_dir():
        raise ValueError(f"project_dir is not a directory: {cwd}")

    merged_env = os.environ.copy()
    if env:
        merged_env.update({str(k): str(v) for k, v in env.items()})

    log_path = _resolve_command_log_path(cwd, command_log_path)
    command_id = uuid.uuid4().hex
    started_at = _utc_now_iso()
    started = time.monotonic()

    try:
        proc = subprocess.run(
            command,
            cwd=str(path),
            env=merged_env,
            text=True,
            capture_output=True,
            timeout=timeout_sec,
            check=False,
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        result = {
            "ok": proc.returncode == 0,
            "return_code": proc.returncode,
            "command": command,
            "executed_command": shlex.join(command),
            "cwd": str(path),
            "stdout": _trim(proc.stdout, capture_limit),
            "stderr": _trim(proc.stderr, capture_limit),
        }
        entry = {
            "version": 1,
            "command_id": command_id,
            "tool_name": tool_name,
            "started_at_utc": started_at,
            "ended_at_utc": _utc_now_iso(),
            "elapsed_ms": elapsed_ms,
            "cwd": str(path),
            "command": command,
            "executed_command": shlex.join(command),
            "timeout_sec": timeout_sec,
            "capture_limit": capture_limit,
            "env_override_keys": sorted(env.keys()) if env else [],
            "ok": result["ok"],
            "return_code": result["return_code"],
            **(attribution or {}),
        }
        _append_command_log(log_path, entry)
        result["command_id"] = command_id
        result["command_log_path"] = str(log_path)
        log_ref = _path_to_ref(log_path)
        if log_ref is not None:
            result["command_log_ref"] = log_ref
        return result
    except subprocess.TimeoutExpired as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        result = {
            "ok": False,
            "return_code": None,
            "command": command,
            "executed_command": shlex.join(command),
            "cwd": str(path),
            "stdout": _trim(exc.stdout or "", capture_limit),
            "stderr": _trim(exc.stderr or "", capture_limit),
            "error": f"timeout: exceeded {timeout_sec} sec",
        }
        entry = {
            "version": 1,
            "command_id": command_id,
            "tool_name": tool_name,
            "started_at_utc": started_at,
            "ended_at_utc": _utc_now_iso(),
            "elapsed_ms": elapsed_ms,
            "cwd": str(path),
            "command": command,
            "executed_command": shlex.join(command),
            "timeout_sec": timeout_sec,
            "capture_limit": capture_limit,
            "env_override_keys": sorted(env.keys()) if env else [],
            "ok": result["ok"],
            "return_code": result["return_code"],
            "error": result["error"],
            **(attribution or {}),
        }
        _append_command_log(log_path, entry)
        result["command_id"] = command_id
        result["command_log_path"] = str(log_path)
        log_ref = _path_to_ref(log_path)
        if log_ref is not None:
            result["command_log_ref"] = log_ref
        return result


def _recommended_build_system(project_dir: str, language: str) -> dict[str, str]:
    root = Path(project_dir)
    lang = (language or "").strip().lower()

    checks = [
        ("Makefile", "make"),
        ("makefile", "make"),
        ("CMakeLists.txt", "cmake"),
        ("meson.build", "meson"),
        ("build.ninja", "ninja"),
        ("Cargo.toml", "cargo"),
        ("go.mod", "go"),
        ("pom.xml", "maven"),
        ("build.gradle", "gradle"),
        ("package.json", "npm"),
        ("pyproject.toml", "poetry"),
    ]
    for marker, build_system in checks:
        if (root / marker).exists():
            return {
                "build_system": build_system,
                "reason": f"{marker} was detected",
            }

    if _is_compiled_language(lang):
        return {
            "build_system": "make",
            "reason": "for a compiled language, make is the default standard build tool",
        }

    return {
        "build_system": "make",
        "reason": "fallback default",
    }


#: The bound `compile_project` applies when its caller names none. Module-level for a build at a
#: remote site (issue #333), where `tool_compile_project` does not apply it: the remote executor
#: (`tools/remote_execution.py`) runs a command with the timeout its caller passes, as
#: `RUN_PROGRAM_TIMEOUT_SEC` is passed for a run.
COMPILE_PROJECT_TIMEOUT_SEC = 1800


def default_build_jobs() -> int:
    """The parallelism `compile_project` builds with when its caller names none: half this
    host's CPUs, at least 1. Module-level so that a build at a remote site (issue #333) can be
    handed the same number, which is then an upper bound on the build system's parallelism
    carried from this host to the site, not a measurement of the site."""
    return max(1, (os.cpu_count() or 1) // 2)


def _build_execute_module(build_system: str) -> Any | None:
    """The `build_execute` module of `build_system`'s package, or `None` when its record does
    not carry the job in a package (no record, or a value only the table below runs).

    Matched EXACTLY, as the table's rows are: the registry case-folds and strips a value, and
    asking it with `MAKE` would serve a spelling `build_command` never accepted."""
    registry = _backend_registry()
    value = str(build_system or "")
    if value not in registry.backend_ids("build_system"):
        return None
    if "build_execute" not in registry.get("build_system", value).backend_provides:
        return None
    return registry.capability_module("build_system", value, "build_execute")


def build_command(
    build_system: str,
    target: str | None,
    jobs: int,
    extra_args: list[str],
) -> list[str]:
    """The argv `compile_project` runs for `build_system`. Public so that a build at a remote
    site (issue #333), which `tool_compile_project` does not run, can be handed the argv this table gives, and run
    what a build here would. Raises `ValueError` for a build system this module does not run.

    A build system whose registry record carries `build_execute` in its package answers from
    that package (`build_argv`, issue #424 PR-2), so a second extracted build system needs no
    edit here. The rows below are the build systems no backend owns yet; a backend that lands
    for one of them takes its row."""
    execute = _build_execute_module(build_system)
    if execute is not None:
        return list(execute.build_argv(target, jobs, extra_args))
    if build_system == "cmake":
        cmd = ["cmake", "--build", ".", "-j", str(jobs)]
        if target:
            cmd += ["--target", target]
        if extra_args:
            cmd += ["--"] + extra_args
        return cmd
    if build_system == "meson":
        cmd = ["meson", "compile", "-j", str(jobs)]
        if target:
            cmd.append(target)
        return cmd + extra_args
    if build_system == "ninja":
        cmd = ["ninja", f"-j{jobs}"]
        if target:
            cmd.append(target)
        return cmd + extra_args
    if build_system == "cargo":
        return ["cargo", "build"] + extra_args
    if build_system == "go":
        return ["go", "build"] + extra_args
    if build_system == "maven":
        return ["mvn", "package"] + extra_args
    if build_system == "gradle":
        cmd = ["gradle"]
        cmd.append(target if target else "build")
        return cmd + extra_args
    if build_system == "npm":
        cmd = ["npm", "run"]
        cmd.append(target if target else "build")
        return cmd + extra_args
    if build_system == "pnpm":
        cmd = ["pnpm", "run"]
        cmd.append(target if target else "build")
        return cmd + extra_args
    if build_system == "poetry":
        return ["poetry", "build"] + extra_args
    raise ValueError(f"unsupported build_system: {build_system}")


def build_system_executable(build_system: str) -> str:
    """The host executable `build_system` builds through.

    argv[0] of the same `build_command` a build runs, so the launch-time host probe
    (`tools/host_prerequisites.py`) cannot look for a different program than `compile_project`
    later launches. An unsupported build system raises `build_command`'s own ValueError rather
    than a second refusal written here.
    """
    return build_command(build_system, None, 1, [])[0]


def tool_compile_project(args: dict[str, Any]) -> dict[str, Any]:
    project_dir = str(args.get("project_dir", "."))
    language = str(args.get("language", "")).strip().lower()
    target = args.get("target")
    # Each bound has a minimum, enforced here. `make -j-5` waits forever, which spends the caller's whole
    # timeout on nothing.
    jobs = _bounded_int(args.get("jobs"), default_build_jobs(), 1, "jobs")
    timeout_sec = _bounded_int(args.get("timeout_sec"), COMPILE_PROJECT_TIMEOUT_SEC, 1,
                               "timeout_sec")
    capture_limit = _bounded_int(args.get("capture_limit"), 120000, 1000, "capture_limit")
    command_log_path = args.get("command_log_path")
    if command_log_path is not None and not isinstance(command_log_path, str):
        raise ValueError("command_log_path must be a string")
    extra_args = args.get("extra_args", [])
    env = args.get("env")
    if env is not None and not isinstance(env, dict):
        raise ValueError("env must be an object")
    if target is not None and not isinstance(target, str):
        raise ValueError("compile_project target must be a string")
    if not isinstance(extra_args, list) or not all(isinstance(a, str) for a in extra_args):
        raise ValueError("compile_project extra_args must be an array of strings")

    build_system = args.get("build_system")
    if build_system:
        build_system = str(build_system).strip().lower()
    else:
        build_system = _recommended_build_system(project_dir, language)["build_system"]

    if build_system not in DEPENDENCY_AWARE_BUILD_SYSTEMS:
        raise ValueError(
            "build_system must be a standard dependency-aware build tool"
        )
    if _is_compiled_language(str(language or "")) and build_system not in {
        "make",
        "cmake",
        "meson",
        "ninja",
    }:
        raise ValueError(
            "for a compiled language, use make/cmake/meson/ninja. make is the default."
        )

    command = build_command(build_system, target, jobs, extra_args)
    result = _run_command(
        command=command,
        cwd=project_dir,
        tool_name="compile_project",
        timeout_sec=timeout_sec,
        env=env,
        capture_limit=capture_limit,
        command_log_path=command_log_path,
        attribution=_attribution(args),
    )
    result["language"] = language or None
    result["build_system"] = build_system
    return result


#: The bound `run_program` applies when its caller names none. The remote executor
#: (`tools/remote_execution.py`) does not apply it, so the conductor passes it there.
RUN_PROGRAM_TIMEOUT_SEC = 3600
#: The same for `run_quality_checks`.
QUALITY_CHECKS_TIMEOUT_SEC = 1800

#: The argv of each `run_quality_checks` preset no build-system backend owns yet. A backend
#: that lands for one of them takes its row.
_UNOWNED_QUALITY_CHECK_PRESET_COMMANDS: dict[str, tuple[str, ...]] = {
    "ctest": ("ctest", "--output-on-failure"),
    "pytest": ("pytest", "-q"),
}


def _quality_check_preset_commands() -> dict[str, tuple[str, ...]]:
    """The argv of each `run_quality_checks` preset: every build-system backend that carries
    `build_execute` serves its own (`QUALITY_CHECK_COMMANDS`, issue #424 PR-2), and the table
    above serves the rest. A preset name declared twice is refused rather than resolved by
    order — the two would run different commands under one name."""
    registry = _backend_registry()
    commands: dict[str, tuple[str, ...]] = {}
    owner: dict[str, str] = {}
    sources = [(f"build_system backend {value!r}",
                registry.capability_module("build_system", value,
                                           "build_execute").QUALITY_CHECK_COMMANDS)
               for value in registry.backend_ids("build_system")
               if "build_execute" in registry.get("build_system", value).backend_provides]
    sources.append(("this module's unowned table", _UNOWNED_QUALITY_CHECK_PRESET_COMMANDS))
    for source, table in sources:
        for preset, argv in table.items():
            if preset in commands:
                raise ValueError(
                    f"run_quality_checks preset {preset!r} is declared by both {owner[preset]} "
                    f"and {source}")
            commands[preset] = tuple(argv)
            owner[preset] = source
    return commands


#: Composed once at import, so a preset declared twice refuses the module rather than the first
#: call (the same shape as `_check_lint_preset_declarations`).
_QUALITY_CHECK_PRESET_COMMANDS: dict[str, tuple[str, ...]] = _quality_check_preset_commands()


def quality_check_command(preset: str) -> list[str]:
    """The argv `run_quality_checks` runs for `preset`: the one table both `tool_run_quality_checks` and the
    remote executor's caller read, so a quality check at a site runs what one here would. Raises
    `ValueError` for a preset this module does not run."""
    if preset not in _QUALITY_CHECK_PRESET_COMMANDS:
        supported = ", ".join(sorted(_QUALITY_CHECK_PRESET_COMMANDS))
        raise ValueError(f"unsupported preset: {preset}. supported={supported}")
    return list(_QUALITY_CHECK_PRESET_COMMANDS[preset])


def tool_run_program(args: dict[str, Any]) -> dict[str, Any]:
    project_dir = str(args.get("project_dir", "."))
    timeout_sec = _bounded_int(args.get("timeout_sec"), RUN_PROGRAM_TIMEOUT_SEC, 1, "timeout_sec")
    capture_limit = _bounded_int(args.get("capture_limit"), 120000, 1000, "capture_limit")
    command_log_path = args.get("command_log_path")
    if command_log_path is not None and not isinstance(command_log_path, str):
        raise ValueError("command_log_path must be a string")
    env = args.get("env")
    command = args.get("command")
    if not isinstance(command, list) or not command:
        raise ValueError("command must be a non-empty string array")
    command = [str(item) for item in command]
    if env is not None and not isinstance(env, dict):
        raise ValueError("env must be an object")

    run_env: dict[str, str] | None
    if env is None:
        run_env = None
    else:
        run_env = {str(k): str(v) for k, v in env.items()}

    return _run_command(
        command=command,
        cwd=project_dir,
        tool_name="run_program",
        timeout_sec=timeout_sec,
        env=run_env,
        capture_limit=capture_limit,
        command_log_path=command_log_path,
        attribution=_attribution(args),
    )


def tool_run_quality_checks(args: dict[str, Any]) -> dict[str, Any]:
    project_dir = str(args.get("project_dir", "."))
    timeout_sec = _bounded_int(args.get("timeout_sec"), QUALITY_CHECKS_TIMEOUT_SEC, 1, "timeout_sec")
    capture_limit = _bounded_int(args.get("capture_limit"), 120000, 1000, "capture_limit")
    command_log_path = args.get("command_log_path")
    if command_log_path is not None and not isinstance(command_log_path, str):
        raise ValueError("command_log_path must be a string")
    env = args.get("env")
    if env is not None and not isinstance(env, dict):
        raise ValueError("env must be an object")
    preset = str(args.get("preset", "make_test"))

    if "command" in args:
        raise ValueError("run_quality_checks does not allow custom command; use preset")

    command = quality_check_command(preset)

    run_env: dict[str, str] | None
    if env is None:
        run_env = None
    else:
        run_env = {str(k): str(v) for k, v in env.items()}

    if preset == "pytest":
        if run_env is None:
            run_env = {}
        project_path = str(Path(project_dir).resolve())
        # This module's own value replaces any PYTHONPATH the caller passed; the only
        # value inherited is the process environment's.
        existing = os.environ.get("PYTHONPATH", "")
        if existing:
            run_env["PYTHONPATH"] = f"{project_path}{os.pathsep}{existing}"
        else:
            run_env["PYTHONPATH"] = project_path

    result = _run_command(
        command=command,
        cwd=project_dir,
        tool_name="run_quality_checks",
        timeout_sec=timeout_sec,
        env=run_env,
        capture_limit=capture_limit,
        command_log_path=command_log_path,
        attribution=_attribution(args),
    )
    result["preset"] = preset
    return result


def _lint_preset_command(preset: str) -> tuple[str, ...]:
    """One preset's argv, asked of the backend package that authors it.

    No simple preset's argv is spelled in this module any more. The preset NAME survives here —
    naming an axis value is what the neutral core may do; knowing what the value implies (a rule
    set, a compiler-family argument, an executable) is what it may not
    (`docs/BACKEND_BOUNDARY.md` §Design Policy). `_INLINE_LINT_PRESET_COMMANDS`, the table this
    function used to fall back to, held `cppcheck`'s and `ruff`'s argv until issue #120; a
    `KeyError` from it was how a preset with no package used to surface, and the refusal is now
    `registry.capability_module`'s, which names the record and the capability.

    `tool_run_linter` runs the result and `lint_preset_executables` answers the launch-time host
    probe (`tools/host_prerequisites.py`) out of the same rows, so what the probe looks for
    cannot drift from what the gate later launches.
    """
    registry = _backend_registry()
    return tuple(registry.capability_module("linter", preset, "lint").check_argv())


#: The simple `static lint` presets: every linter whose record carries `lint` in
#: `backend_provides`, read from the registry (issue #424). Each authors its own argv in its
#: backend package, so the set of NAMES is the registry's too — the literal tuple this replaced
#: had to be edited beside each new linter record, and the MCP schema text beside it (deleted in
#: issue #444) had already missed `nvcc`.
_SIMPLE_LINT_PRESETS: tuple[str, ...] = tuple(
    bid for bid in _backend_registry().backend_ids("linter")
    if "lint" in _backend_registry().get("linter", bid).backend_provides
)

#: The argv each simple preset runs, composed once at import. The KEYS are the set above — the
#: set every reader below iterates.
_LINT_PRESET_COMMANDS: dict[str, tuple[str, ...]] = {
    preset: _lint_preset_command(preset) for preset in _SIMPLE_LINT_PRESETS
}

#: A preset that runs several linters in order, named by the presets it COMPOSES rather than by
#: their argv: the previous spelling restated `fortitude`'s and `cppcheck`'s command lines a
#: second time inside the `mixed` branch, so a flag change reached one invocation and not the
#: other. Declared once, in the registry (`COMPOSITE_LINTERS`, issue #424), which the
#: post_generate validator reads too.
_LINT_PRESET_COMPOSITES: dict[str, tuple[str, ...]] = dict(_backend_registry().COMPOSITE_LINTERS)


def _check_lint_preset_declarations() -> None:
    """Fail at import on a preset table these two readers would disagree about.

    Both tables are now the registry's (issue #424), whose own `_check_declarations` refuses the
    same two shapes for the records; these arms witness that declaration as this module composed
    it.

    A name in both tables would make `lint_preset_sub_presets` and the result-shape branch in
    `tool_run_linter` disagree about whether it is simple, so one preset would return two shapes
    depending on which reader asked. A composite naming a preset with no command row would
    `KeyError` mid-run, after its earlier sub-runs had already executed.

    A raise rather than an `assert`, for the reason `tools/backends/registry._check_declarations`
    gives: `python -O` strips an assert, and refusing the module is cheaper than either failure.
    """
    both = sorted(set(_LINT_PRESET_COMMANDS) & set(_LINT_PRESET_COMPOSITES))
    if both:
        raise ValueError(f"lint preset is either simple or composite, not both: {both}")
    for preset, subs in _LINT_PRESET_COMPOSITES.items():
        unknown = sorted(set(subs) - set(_LINT_PRESET_COMMANDS))
        if unknown:
            raise ValueError(f"lint preset {preset!r} composes unregistered presets: {unknown}")


_check_lint_preset_declarations()


def lint_preset_sub_presets(preset: str) -> tuple[str, ...]:
    """The simple presets `preset` runs, in order. A simple preset composes itself.

    Raises for an unregistered preset with the same message the dispatch used to end in, so a
    caller that names one still learns the supported set rather than getting an empty run.
    """
    if preset in _LINT_PRESET_COMMANDS:
        return (preset,)
    composite = _LINT_PRESET_COMPOSITES.get(preset)
    if composite is not None:
        return composite
    supported = ", ".join(list(_LINT_PRESET_COMMANDS) + list(_LINT_PRESET_COMPOSITES))
    raise ValueError(f"unsupported preset: {preset}. supported={supported}")


def lint_preset_executables(preset: str) -> tuple[str, ...]:
    """The host executables `preset` needs, in run order and without repeats."""
    executables: list[str] = []
    for sub in lint_preset_sub_presets(preset):
        exe = _LINT_PRESET_COMMANDS[sub][0]
        if exe not in executables:
            executables.append(exe)
    return tuple(executables)


def _lint_source_files(project_dir: str, suffixes: tuple[str, ...]) -> list[str]:
    """Every regular file under `project_dir`, at any depth, carrying one of `suffixes`, as
    `./<relative path>` sorted — the files a linter that takes files by name is handed. Depth,
    because the directory linters walk it (the conductor's lint attribution partitions the whole
    tree between two probes, and a file below the top level must be in one of them). The `./`
    prefix is what keeps a leaf-chosen name such as `-o.cu` or `@args.cu` from reading as an
    option or a response file; a symbolic link is not followed."""
    root = Path(project_dir)
    lowered = tuple(s.lower() for s in suffixes)
    return sorted(
        "./" + path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink() and path.suffix.lower() in lowered
    )


def _lint_command_over(preset: str, project_dir: str) -> list[str] | None:
    """The argv `preset` runs over `project_dir`, or `None` when it takes files by name and the
    directory holds none.

    A linter's backend says which kind it is (`SOURCE_SUFFIXES`, part of the `lint` capability
    contract): `None` for one that walks the directory it is pointed at, the suffixes it is
    handed otherwise (issue #289, R4-b PR-4: the CUDA compiler driver, which walks nothing)."""
    module = _backend_registry().capability_module("linter", preset, "lint")
    suffixes = module.SOURCE_SUFFIXES
    if suffixes is None:
        return list(_LINT_PRESET_COMMANDS[preset])
    sources = _lint_source_files(project_dir, tuple(suffixes))
    if not sources:
        return None
    return list(module.source_argv(sources))


def _no_lint_sources_result(preset: str) -> dict[str, Any]:
    """The result of a file-taking linter over a directory with no file of its suffixes: nothing
    was judged and nothing ran, which is CLEAN — the answer a directory-walking linter gives an
    empty directory (0 files scanned, exit 0), so the conductor's lint attribution probes read
    the two kinds alike. No command runs, so nothing is logged: a certification cites a logged
    run, and a node whose lint directory holds no source has no run to cite."""
    return {
        "ok": True,
        "return_code": 0,
        "stdout": "",
        "stderr": "",
        "command": list(_LINT_PRESET_COMMANDS[preset]),
        "skipped": True,
        "reason": f"no source file for {preset} under the lint target",
    }


def tool_run_linter(args: dict[str, Any]) -> dict[str, Any]:
    """Run static analysis linters for generated sources (Generate stage only).

    Presets invoke fixed commands; arbitrary user commands are not allowed.
    This is not compile_project and does not route through build_system.
    """
    project_dir = str(args.get("project_dir", "."))
    timeout_sec = _bounded_int(args.get("timeout_sec"), 1800, 1, "timeout_sec")
    capture_limit = _bounded_int(args.get("capture_limit"), 120000, 1000, "capture_limit")
    command_log_path = args.get("command_log_path")
    if command_log_path is not None and not isinstance(command_log_path, str):
        raise ValueError("command_log_path must be a string")
    env = args.get("env")
    if env is not None and not isinstance(env, dict):
        raise ValueError("env must be an object")

    if "command" in args:
        raise ValueError("run_linter does not allow custom command; use preset")
    # REQUIRED: which linter a node is linted with is its language's answer
    # (`registry.linter_for_language`), and a default here was one language's (issue #289).
    raw_preset = args.get("preset")
    if not isinstance(raw_preset, str) or not raw_preset.strip():
        raise ValueError("run_linter requires a non-empty string 'preset'")
    preset = raw_preset.strip().lower()

    run_env: dict[str, str] | None
    if env is None:
        run_env = None
    else:
        run_env = {str(k): str(v) for k, v in env.items()}

    # The unsupported-preset refusal comes first, out of the same table the runs come from, so
    # it cannot drift from what is actually runnable.
    sub_presets = lint_preset_sub_presets(preset)
    runs = []
    for sub in sub_presets:
        command = _lint_command_over(sub, project_dir)
        if command is None:
            runs.append(_no_lint_sources_result(sub))
            continue
        runs.append(_run_command(
            command=command,
            cwd=project_dir,
            tool_name="run_linter",
            timeout_sec=timeout_sec,
            env=run_env,
            capture_limit=capture_limit,
            command_log_path=command_log_path,
            attribution=_attribution(args),
        ))
    # A simple preset keeps the FLAT result shape (the command's own keys plus `preset`); a
    # composite keeps the `runs` shape, one entry per sub-run in order. Both are what the
    # conductor's `_gate_lint_check` normalizes and what the lint evidence records.
    if preset in _LINT_PRESET_COMMANDS:
        return runs[0] | {"preset": preset}
    return {
        "ok": all(bool(run.get("ok")) for run in runs),
        "preset": preset,
        "runs": [{"sub_preset": sub, **run} for sub, run in zip(sub_presets, runs)],
    }


# --- run_syntax_check: compiler-frontend syntax gate (Generate stage only) ----------------
#
# Runs a real compiler front-end in syntax-only mode over the staged sources so the Generate
# stage catches, before Build, the whole class of syntax / standard-conformance errors the
# (non-compiling) post_generate text heuristics could only approximate one observed failure at
# a time. Producing NO build artifacts (module files go to a throwaway scratch dir inside
# project_dir), this is lint-class, not a build — it sits with run_linter outside the "compile
# must go through a standard build tool" rule.
#
# Compilers are the `syntax_check` capability of the `compiler` axis (no custom commands,
# mirroring run_linter's preset-only rule): each adapter builds the full argv, knows its
# executable, its version probe and a canary source, and names the LANGUAGE whose sources it
# reads. That language's `syntax_promotions` says which files are sources, their order and the
# warning classes the stage promotes to errors — the facts this section held inline for one
# language until issue #289 (R4-b PR-2). The scratch dir is passed so a future adapter without
# a true syntax-only mode (one that would compile with objects discarded into it) fits the same
# interface. Module files are compiler-/version-specific formats: every call gets its own
# scratch dir and must never share Build's object directory.

_SYNTAX_SCRATCH_DIR_NAME = ".mods"


def syntax_check_compilers() -> tuple[str, ...]:
    """The compiler ids with a syntax-only adapter, sorted — the set a refusal names."""
    registry = _backend_registry()
    return tuple(c for c in registry.backend_ids("compiler")
                 if registry.provides("compiler", c, "syntax_check"))


def syntax_adapter(compiler: str) -> Any:
    """The `syntax_check` module of `compiler`, or `ValueError` naming the supported set.

    A `ValueError`, as the inline table's miss was, because every caller already routes that
    class: the conductor treats an unregistered OPTIONAL stage as skipped before asking, and a
    direct caller naming an unknown compiler made a caller-side mistake."""
    registry = _backend_registry()
    try:
        return registry.capability_module("compiler", compiler, "syntax_check")
    except (registry.UnsupportedBackend, registry.BackendNotExtracted) as exc:
        supported = ", ".join(syntax_check_compilers())
        raise ValueError(
            f"unsupported compiler: {compiler}. supported={supported} ({exc})") from None


def syntax_language(adapter: Any) -> Any:
    """The `syntax_promotions` module of the language `adapter` reads.

    Raises the registry's own refusal: an adapter naming a language that declares no syntax
    facts is a declaration defect in `tools/backends/`, not a caller mistake."""
    return _backend_registry().capability_module(
        "language", adapter.LANGUAGE, "syntax_promotions")


def syntax_compiler_wrapper(parallel_backend: str | None, compiler: str) -> Any:
    """The `compiler_wrapper` module a syntax stage of `compiler` runs through for a target
    built for `parallel_backend`, None when the backend declares no wrapper or the wrapper runs
    another compiler (issue #316). An unknown backend declares nothing, as the registry's
    `provides` answers it; the launch gate is what refuses one."""
    if not parallel_backend:
        return None
    registry = _backend_registry()
    backend = parallel_backend.strip().lower()
    if not registry.provides("parallel", backend, "compiler_wrapper"):
        return None
    wrapper = registry.capability_module("parallel", backend, "compiler_wrapper")
    return wrapper if str(wrapper.WRAPPED_COMPILER) == compiler else None


def syntax_compiler_executable(compiler: str) -> str:
    """The host executable a registered syntax-check adapter launches.

    The same executable `tool_run_syntax_check` probes before it runs the stage, so the
    launch-time host probe (`tools/host_prerequisites.py`) cannot look for a different program.
    Raises for a compiler with no registered adapter, which is a build-tooling bug rather than a
    host one.
    """
    return str(syntax_adapter(compiler).EXECUTABLE)


@lru_cache(maxsize=8)
def _syntax_compiler_version(version_argv: tuple[str, ...]) -> str | None:
    """The first line of `<compiler> --version` that carries a dotted version number, else its
    first line; cached per argv. A compiler's version is invariant for the process lifetime, so
    probe it once rather than re-spawning the extra subprocess on every syntax stage and every
    warm-resume retry (the conductor runs this tool in-process across the whole orchestration).

    The first VERSIONED line, not the first line (issue #289, R4-b PR-4): the value is the
    `compiler_version` of the build derivation key's toolchain identity
    (`orchestration_runtime._target_toolchain_identity`), and a compiler whose first line is its
    NAME (one supported driver prints its name first and its release on the fourth line) would leave the key unchanged across an upgrade, reusing a binary the old compiler
    built. For a compiler whose first line carries its version the two readings are
    the same line, so no existing key moves."""
    try:
        proc = subprocess.run(
            list(version_argv), text=True, capture_output=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = [line.strip() for line in (proc.stdout or proc.stderr or "").strip().splitlines()]
    versioned = next((line for line in lines if re.search(r"\d+\.\d+", line)), None)
    return versioned or (lines[0] if lines else None)


def tool_run_syntax_check(args: dict[str, Any]) -> dict[str, Any]:
    """Run a compiler front-end in syntax-only mode over staged sources.

    Registered adapters only; arbitrary user commands are not allowed. Produces no
    build artifacts (lint-class, not a build; does not route through build_system).
    A missing compiler binary returns {ok: True, skipped: True, ...} — whether a
    stage may be skipped (an optional stage) or must hard-fail (the language's
    mandatory stage) is the caller's policy, not this tool's.

    `compiler` and `std` are REQUIRED: both are the caller's target facts (the language's
    mandatory syntax compiler, the profile's `toolchain.standard`), and a default here would
    be one language's spelling in a tool that serves every language.

    `parallel_backend` (optional, the profile's `parallel.backend`) names a backend whose
    `compiler_wrapper` stands in for the compiler (issue #316): when it declares one and this
    stage's `compiler` is the one the wrapper runs (`WRAPPED_COMPILER`), the command's `argv[0]`
    is the wrapper, so the model files of the backend's runtime are found. Replacing the program
    that runs is decided HERE, from the registry, never from a caller-supplied program name.
    """
    project_dir = str(args.get("project_dir", "."))
    timeout_sec = _bounded_int(args.get("timeout_sec"), 1800, 1, "timeout_sec")
    capture_limit = _bounded_int(args.get("capture_limit"), 120000, 1000, "capture_limit")
    command_log_path = args.get("command_log_path")
    if command_log_path is not None and not isinstance(command_log_path, str):
        raise ValueError("command_log_path must be a string")
    env = args.get("env")
    if env is not None and not isinstance(env, dict):
        raise ValueError("env must be an object")

    if "command" in args:
        raise ValueError(
            "run_syntax_check does not allow custom command; use a registered compiler adapter"
        )
    for required in ("compiler", "std"):
        value = args.get(required)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"run_syntax_check requires a non-empty string {required!r}")
    compiler = str(args["compiler"]).strip().lower()
    std = str(args["std"]).strip().lower()
    openmp = bool(args.get("openmp", False))
    architecture = args.get("architecture")
    if architecture is not None and not isinstance(architecture, str):
        raise ValueError("architecture must be a string")
    parallel_backend = args.get("parallel_backend")
    if parallel_backend is not None and (
            not isinstance(parallel_backend, str) or not parallel_backend.strip()):
        raise ValueError("parallel_backend must be a non-empty string")

    adapter = syntax_adapter(compiler)
    wrapper = syntax_compiler_wrapper(parallel_backend, compiler)
    language = syntax_language(adapter)

    sources = args.get("sources")
    if sources is not None and (
        not isinstance(sources, list) or not all(isinstance(s, str) for s in sources)
    ):
        raise ValueError("sources must be an array of source file names")

    proj = Path(project_dir)
    if not proj.is_dir():
        raise ValueError(f"project_dir is not a directory: {project_dir}")

    suffixes = tuple(language.SOURCE_SUFFIXES)
    ordered_sources = list(sources) if sources is not None else language.compile_order(proj)
    # The same rule for both readings, and BEFORE the compiler-availability skip below:
    # the rule is about the names, not about what a compiler would do with them, and an
    # optional stage skipping on a machine without that compiler must not be the reason
    # a bad name goes unnoticed. Auto-discovery filters on suffix alone, so a staged file
    # named `-o.<suffix>` or `@resp.<suffix>` walked into the compiler argv as an option — and
    # the workflow always takes that branch, since it passes no `sources`. A stray one is a
    # visible gate failure rather than a silently skipped file.
    _validate_syntax_sources(ordered_sources, project_dir, "run_syntax_check",
                             suffixes=suffixes, language=str(adapter.LANGUAGE))

    executable = str(adapter.EXECUTABLE) if wrapper is None else str(wrapper.COMPILER_WRAPPER)
    if shutil.which(executable) is None:
        return {
            "ok": True,
            "skipped": True,
            "compiler": compiler,
            "std": std,
            "reason": f"compiler not available: {executable}",
        }

    if not ordered_sources:
        return {
            "ok": True,
            "skipped": True,
            "compiler": compiler,
            "std": std,
            "reason": f"no {adapter.LANGUAGE} sources found",
        }

    (proj / _SYNTAX_SCRATCH_DIR_NAME).mkdir(exist_ok=True)

    run_env: dict[str, str] | None
    if env is None:
        run_env = None
    else:
        run_env = {str(k): str(v) for k, v in env.items()}

    command = adapter.argv(
        standard=std, scratch_dir=_SYNTAX_SCRATCH_DIR_NAME, openmp=openmp,
        promotions=tuple(language.PROMOTED_WARNINGS), architecture=architecture,
        sources=ordered_sources)
    if wrapper is not None:
        command = list(wrapper.wrap(command))
    result = _run_command(
        command=command,
        cwd=project_dir,
        tool_name="run_syntax_check",
        timeout_sec=timeout_sec,
        env=run_env,
        capture_limit=capture_limit,
        command_log_path=command_log_path,
        attribution=_attribution(args),
    )
    return result | {
        "compiler": compiler,
        # Through the wrapper when the stage ran through it: the wrapper runs the compiler IT is
        # configured with, which need not be the one this name resolves to on PATH.
        "compiler_version": _syntax_compiler_version(
            tuple(adapter.VERSION_ARGV) if wrapper is None
            else tuple(wrapper.wrap(adapter.VERSION_ARGV))),
        "std": std,
        "openmp": openmp,
        "skipped": False,
    }
