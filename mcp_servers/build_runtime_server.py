#!/usr/bin/env python3
"""Minimal MCP server for build/run/quality operations.

This server intentionally has no external dependencies so that it can be used
in constrained environments.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

JSONRPC_VERSION = "2.0"
SERVER_NAME = "build-runtime-server"


def _disable_bytecode_writes() -> None:
    """Stop this interpreter (and the build/gate subprocesses it spawns) from writing
    `.pyc` files.

    The MCP server runs inside a read-only bwrap sandbox where neither
    `workspace/.pycache` nor the in-repo source `__pycache__` is writable. Importing the
    large orchestration runtime would otherwise attempt a bytecode write that EROFSes
    before any build runs (the previous code unconditionally `mkdir`-ed
    `workspace/.pycache`, which succeeded only when that dir happened to pre-exist from a
    non-sandboxed run). A runtime `PYTHONDONTWRITEBYTECODE` env var alone is too late to
    flip `sys.dont_write_bytecode` for the already-started interpreter, so set it
    directly; also export the env var so subprocesses inherit it. Disabling the cache is
    negligible here — the server is short-lived and re-imported per leaf launch — and it
    also avoids ever polluting the repo source tree with `.pyc`.
    """
    sys.dont_write_bytecode = True
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"


@lru_cache(maxsize=1)
def _load_orchestration_runtime() -> Any:
    """Load `tools/orchestration_runtime.py` without requiring `tools` as a package."""
    root = Path(__file__).resolve().parent.parent
    path = root / "tools" / "orchestration_runtime.py"
    _disable_bytecode_writes()
    import importlib.util

    spec = importlib.util.spec_from_file_location("orchestration_runtime", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load orchestration runtime from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_WORKFLOW_MODE_ENV_VARS = ("METDSL_WORKFLOW_MODE", "METDSL_ORCHESTRATION_ID")


def _workflow_mode_env_signal() -> str | None:
    """The workflow environment variable that puts this server under a run, if any.

    `tools/run_workflow.py` sets `METDSL_WORKFLOW_MODE=1` in the node environment and
    the conductor adds `METDSL_ORCHESTRATION_ID` per child; bwrap passes the environment
    through, so both reach the leaf's CLI and the MCP server it spawns. Read
    `os.environ` on every call — tests substitute the environment per case, so a cached
    answer would be wrong.
    """
    for name in _WORKFLOW_MODE_ENV_VARS:
        value = os.environ.get(name, "").strip()
        if not value:
            continue
        if name == "METDSL_WORKFLOW_MODE" and value == "0":
            continue
        return name
    return None


def _maybe_enforce_orchestration_mcp_gate(
    *,
    tool_name: str,
    project_dir: str,
    args: dict[str, Any],
) -> None:
    """When `orchestration_id` is set, require launch + capability token + phase_state.

    Under the workflow the argument itself is mandatory: omitting it would otherwise
    buy an unattributed call with no capability, no role/phase check, and no audit
    record, which is the whole gate. Outside a run (no workflow environment) the server
    stays usable standalone, where there is no orchestration to attribute a call to.
    """
    orch_raw = args.get("orchestration_id")
    if not _is_orchestrated_call(args):
        signal = _workflow_mode_env_signal()
        if signal is not None:
            raise ValueError(
                f"{tool_name} requires orchestration_id under the workflow "
                f"({signal} is set in this server's environment)"
            )
        return
    orch_id = str(orch_raw).strip()
    agent_raw = args.get("agent_run_id")
    cap_raw = args.get("capability_token")
    if agent_raw is None or not str(agent_raw).strip():
        raise ValueError(f"{tool_name} requires agent_run_id when orchestration_id is set")
    if cap_raw is None or not str(cap_raw).strip():
        raise ValueError(f"{tool_name} requires capability_token when orchestration_id is set")
    repo_root = _repo_root_for_call(args, project_dir)
    rt = _load_orchestration_runtime()
    rt.validate_mcp_build_tool_invocation(
        repo_root,
        orchestration_id=orch_id,
        agent_run_id=str(agent_raw).strip(),
        capability_token=str(cap_raw).strip(),
        tool_name=tool_name,
        mcp_args=args,
    )


def _bounded_int(raw: Any, default: int, minimum: int, name: str) -> int:
    """An integer argument, held to the minimum its served schema declares."""
    if raw is None:
        return default
    value = int(raw)
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum} (got {value})")
    return value


def _repo_root_for_call(args: dict[str, Any], project_dir: str) -> Path:
    """The root a call's paths are judged against.

    One spelling, shared by the capability gate and by every containment check, so the
    root a capability is validated at is the root its paths are measured from. Only an
    ABSENT `repo_root` falls back to `project_dir`: a present but empty value is a
    value, and reading it as an omission in one place and not the other would let the
    caller choose its own containment root."""
    raw = args.get("repo_root")
    return Path(str(raw if raw is not None else project_dir)).resolve()


def _is_orchestrated_call(args: dict[str, Any]) -> bool:
    """Whether this call is attributed to an orchestration.

    The predicate is the gate's, spelled the same way: only `None` and a blank string
    are absent. Reading any falsy value as absent instead would hand a call the gate
    validated (`orchestration_id` of `0` or `False` becomes the id `"0"` / `"False"`)
    the rules meant for an unattributed one."""
    raw = args.get("orchestration_id")
    return raw is not None and bool(str(raw).strip())


# The make variables `Validate.execute` passes to the make_test re-run (canonical:
# docs/workflow/phases/phase_04_validate.md), which are also the only assignments
# `compile_project` accepts in `extra_args`. Adding a key here is adding a way to
# influence a certified build, so it belongs with the phase contract that needs it.
_MAKE_VARIABLE_ALLOWLIST = frozenset(
    {"OBJDIR", "BINDIR", "RUNDIR", "BIN", "SPEC", "CASES"}
)

# Which tools may carry any of them at all. The workflow passes `env` to exactly one
# tool; for the other three an empty allowlist says so, rather than advertising make
# variables that mean nothing to a linter or a syntax check.
_ORCHESTRATED_ENV_KEYS_BY_TOOL: dict[str, frozenset[str]] = {
    # Build passes these on the make command line (`extra_args`), not in the
    # environment, so `compile_project` accepts no caller `env` either.
    "compile_project": frozenset(),
    "run_quality_checks": _MAKE_VARIABLE_ALLOWLIST,
    "run_program": frozenset(),
    "run_linter": frozenset(),
    "run_syntax_check": frozenset(),
}

_UNSAFE_ENV_OVERRIDE_KEYS = frozenset({
    "BASH_ENV", "ENV", "IFS", "PATH", "PYTHONPATH",
    # gcc/gfortran is a driver: it finds and execs its own front end (`f951`), `as`
    # and `ld` through these, none of which is PATH or LD_*.
    "COMPILER_PATH", "GCC_EXEC_PREFIX", "LIBRARY_PATH",
    # GNU make parses these as command-line switches / extra makefiles, so
    # `--eval=$(shell ...)` runs before the certified Makefile is read.
    "MAKEFLAGS", "GNUMAKEFLAGS", "MAKEFILES", "MAKESHELL",
})
_UNSAFE_ENV_OVERRIDE_PREFIXES = ("LD_", "DYLD_")


def _validate_env_overrides(
    env: Any, tool_name: str, *, orchestrated: bool, repo_root: Path
) -> None:
    """Constrain caller-supplied environment overrides.

    Every tool here except `run_program` (whose `command` is caller-chosen argv by
    design) constrains argv to a fixed preset or a build-tool invocation, and the
    environment goes around that constraint. Listing what to keep out does not finish:
    each program these tools run reads its own configuration from the environment. The
    loader takes `LD_PRELOAD`, the gcc driver
    takes `COMPILER_PATH` to find the front end it execs, make takes `MAKEFLAGS` as
    command-line switches and imports any other name as a make variable, so `FC` alone
    redirects a certified Makefile's compiler. Enumerating those names is a list that
    grows by one every time someone reads it.

    So under an orchestration only the keys the workflow declares are accepted, and
    every other key is refused whether or not anyone has thought of it. Outside an
    orchestration the denylist applies: the operator chose the argv, so the check is
    there to catch a mistake, not to confine the caller.

    The accepted keys' VALUES are checked too: they reach the make recipe's shell
    unquoted, so a value carrying a shell metacharacter is a command, not a value.

    Refusing names the key instead of dropping it silently, so a mistake and an attack
    are both visible in the caller's result. `tools/hooks/cli.py` refuses a `VAR=value`
    prefix on a Bash command for the same reason.

    Call this on the raw `env` argument, before the server composes its own additions
    (`OMP_*` for run_program, `PYTHONPATH` for the pytest preset) — those are the
    server's own decisions and are not caller-controlled.
    """
    if not env:
        return
    if orchestrated:
        # Exact names: `objdir` is a different environment variable, and accepting it
        # would leave the make_test re-run on the Makefile's own default.
        allowed = _ORCHESTRATED_ENV_KEYS_BY_TOOL.get(tool_name, frozenset())
        offending = sorted(str(key) for key in env if str(key) not in allowed)
        if offending:
            permitted = ", ".join(sorted(allowed)) or "none"
            raise ValueError(
                f"{tool_name} accepts only these env overrides under an orchestration "
                f"({permitted}); refused: " + ", ".join(offending)
            )
        _refuse_unsafe_values(
            [(str(k), str(v)) for k, v in env.items()], tool_name, "env", repo_root)
        return
    offending = sorted(
        str(key)
        for key in env
        if (norm := str(key).strip().upper()) in _UNSAFE_ENV_OVERRIDE_KEYS
        or norm.startswith(_UNSAFE_ENV_OVERRIDE_PREFIXES)
    )
    if offending:
        raise ValueError(
            f"{tool_name} does not accept env overrides that redirect execution: "
            + ", ".join(offending)
        )


# The declared make variables split by what their value IS: four name a location, two
# name what to run and which cases to run. A path is checked mainly by where it lands
# rather than by its spelling, so a checkout path holding a non-ASCII character stays
# usable.
_ORCHESTRATED_PATH_VALUE_KEYS = frozenset({"OBJDIR", "BINDIR", "RUNDIR", "SPEC"})
# The recipe interpolates every one of them unquoted (`cd $(RUNDIR) &&
# $(BINDIR)/$(BIN) --cases $(SPEC) $(CASES)`), so no value may carry a character that
# line's shell or make acts on — whitespace included, since it splits the words, and
# `#`, which ends a make line. A checkout whose own path holds one of these is not
# usable under the workflow; that is a real constraint on where a repository lives,
# stated in mcp_servers/README.md.
_SHELL_ACTIVE_CHARS = set(" \t\n\r;&|$`'\"\\<>()*?[]{}~#!")
_MAKE_NAME_VALUE_RE = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_.+-]*$")


def _build_syntax_source_re() -> re.Pattern[str]:
    """A source name: a Fortran file name, over the one suffix set the tool uses.

    Derived from `_FORTRAN_SYNTAX_SOURCE_SUFFIXES` so an added suffix cannot make
    auto-discovery accept a file an explicit `sources` list refuses."""
    suffixes = "|".join(s.lstrip(".") for s in _FORTRAN_SYNTAX_SOURCE_SUFFIXES)
    return re.compile(rf"^[A-Za-z0-9_][A-Za-z0-9_.+-]*\.({suffixes})$", re.IGNORECASE)


def _refuse_unsafe_values(
    values: list[tuple[str, str]], tool_name: str, kind: str, repo_root: Path
) -> None:
    """Refuse an accepted key whose VALUE would carry more than a value.

    A path value must be absolute and land inside the repository — the value names
    where the build writes and what it runs, and `BINDIR` alone points the recipe at any
    executable on the machine. Absolute because a relative one has no single base: make
    reads `OBJDIR` from its own working directory and `BINDIR` / `SPEC` from wherever
    `cd $(RUNDIR)` left it, so a check here would be measuring a different path from the
    one that runs. `BIN` must be one identifier — it is a command name, and a space in
    it appends an argument to the runner — and `CASES` a space-separated list of them.
    No value may carry a character the recipe's shell acts on.

    An empty value is refused rather than skipped, for every key the recipe uses as a
    path or a command name. Make imports it as a variable that IS set, so `?=` does not
    restore the default: `RUNDIR=` turns `cd $(RUNDIR)` into a bare `cd`, which lands in
    the home directory, and `$(OBJDIR)/x` into an absolute path at the root. Only
    `CASES` may be empty — an empty case list is a list.

    Takes pairs rather than a mapping: two assignments to the same key both reach the
    command line, so both are checked.
    """
    offending: list[str] = []
    for key, value in values:
        if not value:
            if key != "CASES":
                offending.append(f"{key}=")
            continue
        if set(value) & _SHELL_ACTIVE_CHARS - {" "}:
            offending.append(f"{key}={value}")
        elif key in _ORCHESTRATED_PATH_VALUE_KEYS:
            candidate = Path(value)
            resolved = candidate.resolve()
            if (" " in value or not candidate.is_absolute()
                    or (resolved != repo_root and repo_root not in resolved.parents)):
                offending.append(f"{key}={value}")
        elif key == "CASES":
            if not all(_MAKE_NAME_VALUE_RE.match(word) for word in value.split(" ") if word):
                offending.append(f"{key}={value}")
        elif not _MAKE_NAME_VALUE_RE.match(value):
            offending.append(f"{key}={value}")
    if offending:
        message = (
            f"{tool_name} {kind} values reach the make recipe's shell: a path must be "
            "absolute and inside the repository, a name must be an identifier, and only "
            "CASES may be empty; refused: "
            + ", ".join(sorted(offending))
        )
        # A refusal caused by the CHECKOUT's own path (a space, a parenthesis) reads as a
        # complaint about a value the caller composed correctly, so name the real cause.
        if set(str(repo_root)) & _SHELL_ACTIVE_CHARS:
            message += (
                f". The repository path itself ({str(repo_root)!r}) contains a character "
                "this rule refuses, so no value derived from it can pass — the checkout "
                "has to live somewhere without one"
            )
        raise ValueError(message)


def _validate_build_argv_overrides(
    target: Any, extra_args: Any, tool_name: str, *, orchestrated: bool, repo_root: Path
) -> str | None:
    """Constrain the caller-chosen part of the build argv; return the target to use.

    `target` and `extra_args` are appended to the build tool's command line, and for
    make a command-line assignment overrides even a hard assignment in the Makefile —
    more authority than the environment. `FC=/tmp/x` there replaces the compiler a
    certified Makefile invokes, and `--eval=$(shell ...)` runs before the Makefile is
    read at all.

    So under an orchestration the same declared set governs the argv as governs the
    environment: an `extra_args` element must assign one of those make variables and its
    value must be a value. `target` is refused outright — Build names no target, and a
    target is a way to run whatever the Makefile's other rules do under a grant that
    covers compiling. Outside an orchestration the operator chose the argv already.

    The validated `target` is returned so the string that was checked is the string
    that runs.
    """
    if target is not None and not isinstance(target, str):
        raise ValueError(f"{tool_name} target must be a string")
    if not isinstance(extra_args, list) or not all(isinstance(a, str) for a in extra_args):
        raise ValueError(f"{tool_name} extra_args must be an array of strings")
    resolved_target = target.strip() if isinstance(target, str) and target.strip() else None
    if not orchestrated:
        return resolved_target
    if resolved_target is not None:
        raise ValueError(
            f"{tool_name} does not accept a target under an orchestration "
            f"(got {resolved_target!r}); the build is the Makefile's default goal"
        )
    permitted = sorted(_MAKE_VARIABLE_ALLOWLIST)
    offending = [
        arg for arg in extra_args
        if "=" not in arg or arg.split("=", 1)[0] not in _MAKE_VARIABLE_ALLOWLIST
    ]
    if offending:
        raise ValueError(
            f"{tool_name} accepts only assignments to these make variables in "
            f"extra_args under an orchestration ({', '.join(permitted)}); refused: "
            + ", ".join(offending)
        )
    _refuse_unsafe_values(
        [(arg.split("=", 1)[0], arg.split("=", 1)[1]) for arg in extra_args],
        tool_name, "extra_args", repo_root)
    return resolved_target


class SyntaxSourceNameError(ValueError):
    """A staged file whose NAME the syntax check refuses.

    Its own class because the caller's response differs: the leaf authored the name and
    can rename it, so `Generate.gate` records this as a content failure, while every
    other argument refusal from this module is the caller's own bug and must stay a
    transport failure. Catching by `ValueError` there would blame the leaf for both."""


def _validate_syntax_sources(sources: list[str], project_dir: str, tool_name: str) -> None:
    """Constrain the source list appended to the compiler front-end argv.

    A source is a Fortran file sitting in `project_dir`. Anything else the driver would
    accept there — an option, a response file, a path out of the directory, a symlink
    to one — makes the check compile something other than what was staged, and it
    reports `ok: True` either way. Refused in every mode; the workflow never passes
    this argument at all, and an operator passing it means file names.
    """
    root = Path(project_dir).resolve()
    source_re = _build_syntax_source_re()
    offending = []
    for name in sources:
        if not source_re.match(name):
            offending.append(name)
            continue
        resolved = (root / name).resolve()
        if resolved.parent != root or not resolved.is_file():
            offending.append(name)
    if offending:
        raise SyntaxSourceNameError(
            f"{tool_name} sources must be Fortran source files in project_dir; "
            "refused: " + ", ".join(sorted(offending))
        )


SERVER_VERSION = "0.1.0"
DEFAULT_PROTOCOL_VERSION = "2024-11-05"
DEFAULT_COMMAND_LOG_FILE = "command_log.jsonl"

FORTRAN_C_FAMILY = {
    "fortran",
    "c",
    "cpp",
    "c++",
    "cuda_fortran",
    "cuda_c",
    "mixed",
}

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

def _env_property_schema(tool_name: str) -> dict[str, Any]:
    """The `env` argument as the tool actually accepts it.

    The three tools the workflow passes no `env` to accept none under an orchestration,
    so their schema says that rather than listing make variables that mean nothing to a
    linter or a syntax check."""
    if not _ORCHESTRATED_ENV_KEYS_BY_TOOL.get(tool_name):
        return {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": (
                "Environment overrides for the command. Under an orchestration this "
                "tool accepts none. Standalone, keys that redirect execution (LD_*, "
                "DYLD_*, PATH, PYTHONPATH, BASH_ENV, ENV, IFS, COMPILER_PATH, "
                "GCC_EXEC_PREFIX, LIBRARY_PATH, MAKEFLAGS, GNUMAKEFLAGS, MAKEFILES, "
                "MAKESHELL) are refused."
            ),
        }
    return dict(_MAKE_VARIABLE_ENV_SCHEMA)


_MAKE_VARIABLE_ENV_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": {"type": "string"},
    "description": (
        "Environment overrides for the command. Under an orchestration only the "
        "exact keys OBJDIR, BINDIR, RUNDIR, BIN, SPEC, CASES are accepted, a path "
        "value must be absolute and resolve inside the repository, a name value must "
        "be an identifier, and only CASES may be empty; any other key is refused. Standalone, keys that redirect execution (LD_*, "
        "DYLD_*, PATH, PYTHONPATH, BASH_ENV, ENV, IFS, COMPILER_PATH, "
        "GCC_EXEC_PREFIX, LIBRARY_PATH, MAKEFLAGS, GNUMAKEFLAGS, MAKEFILES, "
        "MAKESHELL) are refused."
    ),
}

_ORCHESTRATION_GATE_PROPERTIES: dict[str, Any] = {
    "orchestration_id": {
        "type": "string",
        "description": (
            "Required under the workflow (METDSL_ORCHESTRATION_ID set, or "
            "METDSL_WORKFLOW_MODE set to a non-empty value other than 0, in the "
            "server's environment): "
            "with agent_run_id and capability_token it enforces preflight, record-launch "
            "artifacts, phase_state child_running, and capability permissions. Omitting "
            "it is refused, not exempted."
        ),
    },
    "agent_run_id": {
        "type": "string",
        "description": "Child agent_run_id that owns the capability token.",
    },
    "capability_token": {
        "type": "string",
        "description": "Secret from workspace/orchestrations/<id>/capabilities/<agent_run_id>.json.",
    },
    "repo_root": {
        "type": "string",
        "description": (
            "Repository root containing workspace/orchestrations/. Omit to use "
            "project_dir; an empty value is not an omission."
        ),
    },
}


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], dict[str, Any]]


def _write_message(payload: dict[str, Any]) -> None:
    # The MCP stdio transport frames messages as newline-delimited JSON.
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _read_message() -> dict[str, Any] | None:
    stream = sys.stdin.buffer
    while True:
        first_line = stream.readline()
        if not first_line:
            return None
        if not first_line.strip():
            continue

        if first_line.lower().startswith(b"content-length:"):
            length = int(first_line.split(b":", 1)[1].strip())
            # Skip remaining headers.
            while True:
                header_line = stream.readline()
                if not header_line:
                    return None
                if header_line in (b"\r\n", b"\n"):
                    break
            body = stream.read(length)
            if not body:
                return None
            return json.loads(body.decode("utf-8"))

        # Fallback for newline-delimited JSON.
        return json.loads(first_line.decode("utf-8"))


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


def _validate_orchestrated_paths(
    command_log_path: Any, args: dict[str, Any], project_dir: str, tool_name: str
) -> None:
    """Keep the working directory and the command log inside the repository.

    The log path is a caller-chosen path that `_append_command_log` creates directories
    for and appends to, so it is a write, and the only placement rule the phase gate
    carries covers `run_program` at Validate. Under an orchestration a write belongs
    inside the checkout the capability is scoped to; the conductor's own paths (the node
    directory, `workspace/tmp/<agent_run_id>/...`) all are.
    """
    if not _is_orchestrated_call(args):
        return
    root = _repo_root_for_call(args, project_dir)
    # project_dir is the subprocess cwd and the base a relative log path resolves
    # against, so it belongs inside the same root the capability was validated at.
    # A relative project_dir has two bases: the gate resolves it against repo_root, and
    # `_run_command` hands it to the subprocess, which resolves it against the server's
    # own working directory. Refuse rather than check one and run the other.
    if not Path(project_dir).is_absolute():
        raise ValueError(
            f"{tool_name} project_dir must be an absolute path under an orchestration "
            f"(got {project_dir!r})"
        )
    for label, candidate in (
        ("project_dir", Path(project_dir).resolve()),
        *(() if command_log_path is None else
          (("command_log_path",
            _resolve_command_log_path(project_dir, str(command_log_path)).resolve()),)),
    ):
        if root != candidate and root not in candidate.parents:
            raise ValueError(
                f"{tool_name} {label} must stay under the repository root under an "
                f"orchestration (got {str(candidate)!r})"
            )


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


def _run_command(
    command: list[str],
    cwd: str,
    tool_name: str,
    timeout_sec: int,
    env: dict[str, str] | None,
    capture_limit: int,
    command_log_path: str | None,
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
        }
        _append_command_log(log_path, entry)
        result["command_id"] = command_id
        result["command_log_path"] = str(log_path)
        log_ref = _path_to_ref(log_path)
        if log_ref is not None:
            result["command_log_ref"] = log_ref
        return result


def _resolve_target_class(args: dict[str, Any]) -> str | None:
    raw_target_class = args.get("target_class")
    if raw_target_class is None:
        raw_target_class = args.get("target.class")

    if raw_target_class is None:
        target_obj = args.get("target")
        if isinstance(target_obj, dict):
            raw_target_class = target_obj.get("class")

    if raw_target_class is None:
        return None

    target_class = str(raw_target_class).strip().lower()
    if not target_class:
        return None
    return target_class


def _parse_threads_per_rank(args: dict[str, Any]) -> int | None:
    raw_threads = args.get("threads_per_rank")
    if raw_threads is None:
        return None
    threads_per_rank = int(raw_threads)
    if threads_per_rank < 1:
        raise ValueError("threads_per_rank must be >= 1")
    return threads_per_rank


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

    if lang in FORTRAN_C_FAMILY:
        return {
            "build_system": "make",
            "reason": "for Fortran/C family, make is the default standard build tool",
        }

    return {
        "build_system": "make",
        "reason": "fallback default",
    }


def _build_command(
    build_system: str,
    target: str | None,
    jobs: int,
    extra_args: list[str],
) -> list[str]:
    if build_system == "make":
        cmd = ["make", f"-j{jobs}"]
        if target:
            cmd.append(target)
        return cmd + extra_args
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


def tool_detect_build_system(args: dict[str, Any]) -> dict[str, Any]:
    project_dir = str(args.get("project_dir", "."))
    # Advisory and capability-free — it runs nothing and no substep is granted it — but
    # it does report which of eleven marker files exist in any directory it is pointed
    # at, and the resolved path of that directory. Under the workflow that is a read
    # outside the boundary the read manifest draws, with no command log to attribute it,
    # so the tool is refused there rather than gated.
    signal = _workflow_mode_env_signal()
    if signal is not None:
        raise ValueError(
            "detect_build_system is not available under the workflow "
            f"({signal} is set in this server's environment); the build system comes "
            "from the IR's toolchain, not from marker files"
        )
    language = str(args.get("language", "")).strip().lower()
    recommended = _recommended_build_system(project_dir, language)
    return {
        "project_dir": str(Path(project_dir).resolve()),
        "language": language or None,
        "recommended_build_system": recommended["build_system"],
        "reason": recommended["reason"],
    }


def tool_compile_project(args: dict[str, Any]) -> dict[str, Any]:
    project_dir = str(args.get("project_dir", "."))
    _maybe_enforce_orchestration_mcp_gate(
        tool_name="compile_project",
        project_dir=project_dir,
        args=args,
    )
    language = str(args.get("language", "")).strip().lower()
    target = args.get("target")
    # The served schema declares these minimums; an MCP argument schema is advisory, so
    # enforce them here. `make -j-5` waits forever, which spends the caller's whole
    # timeout on nothing.
    jobs = _bounded_int(args.get("jobs"), max(1, (os.cpu_count() or 1) // 2), 1, "jobs")
    timeout_sec = _bounded_int(args.get("timeout_sec"), 1800, 1, "timeout_sec")
    capture_limit = _bounded_int(args.get("capture_limit"), 120000, 1000, "capture_limit")
    command_log_path = args.get("command_log_path")
    if command_log_path is not None and not isinstance(command_log_path, str):
        raise ValueError("command_log_path must be a string")
    _validate_orchestrated_paths(command_log_path, args, project_dir, "compile_project")
    extra_args = args.get("extra_args", [])
    env = args.get("env")
    if env is not None and not isinstance(env, dict):
        raise ValueError("env must be an object")
    _validate_env_overrides(
        env, "compile_project", orchestrated=_is_orchestrated_call(args),
        repo_root=_repo_root_for_call(args, project_dir))
    target = _validate_build_argv_overrides(
        target, extra_args, "compile_project", orchestrated=_is_orchestrated_call(args),
        repo_root=_repo_root_for_call(args, project_dir))

    build_system = args.get("build_system")
    if build_system:
        build_system = str(build_system).strip().lower()
    elif _is_orchestrated_call(args):
        # Under an orchestration the phase gate reads an omitted build_system as make
        # (as record_launch does for an IR that omits toolchain.build_system), so
        # detecting one from marker files here would run a build the gate never saw:
        # a project_dir without a Makefile but with a CMakeLists.txt was the bypass.
        # The workflow is make-only, so make is also the right answer.
        build_system = "make"
    else:
        build_system = _recommended_build_system(project_dir, language)["build_system"]

    if build_system not in DEPENDENCY_AWARE_BUILD_SYSTEMS:
        raise ValueError(
            "build_system must be a standard dependency-aware build tool"
        )

    if language in FORTRAN_C_FAMILY and build_system not in {
        "make",
        "cmake",
        "meson",
        "ninja",
    }:
        raise ValueError(
            "for Fortran/C family, use make/cmake/meson/ninja. make is the default."
        )

    command = _build_command(build_system, target, jobs, extra_args)
    result = _run_command(
        command=command,
        cwd=project_dir,
        tool_name="compile_project",
        timeout_sec=timeout_sec,
        env=env,
        capture_limit=capture_limit,
        command_log_path=command_log_path,
    )
    result["language"] = language or None
    result["build_system"] = build_system
    return result


def tool_run_program(args: dict[str, Any]) -> dict[str, Any]:
    project_dir = str(args.get("project_dir", "."))
    _maybe_enforce_orchestration_mcp_gate(
        tool_name="run_program",
        project_dir=project_dir,
        args=args,
    )
    timeout_sec = _bounded_int(args.get("timeout_sec"), 3600, 1, "timeout_sec")
    capture_limit = _bounded_int(args.get("capture_limit"), 120000, 1000, "capture_limit")
    command_log_path = args.get("command_log_path")
    if command_log_path is not None and not isinstance(command_log_path, str):
        raise ValueError("command_log_path must be a string")
    _validate_orchestrated_paths(command_log_path, args, project_dir, "run_program")
    env = args.get("env")
    target_class = _resolve_target_class(args)
    threads_per_rank = _parse_threads_per_rank(args)
    command = args.get("command")
    if not isinstance(command, list) or not command:
        raise ValueError("command must be a non-empty string array")
    command = [str(item) for item in command]
    if env is not None and not isinstance(env, dict):
        raise ValueError("env must be an object")
    _validate_env_overrides(
        env, "run_program", orchestrated=_is_orchestrated_call(args),
        repo_root=_repo_root_for_call(args, project_dir))

    run_env: dict[str, str] | None
    if env is None:
        run_env = None
    else:
        run_env = {str(k): str(v) for k, v in env.items()}

    openmp_env_applied = False
    if target_class == "cpu" and threads_per_rank is not None:
        if run_env is None:
            run_env = {}
        thread_count = str(threads_per_rank)
        run_env["OMP_NUM_THREADS"] = thread_count
        run_env["OMP_THREAD_LIMIT"] = thread_count
        openmp_env_applied = True

    result = _run_command(
        command=command,
        cwd=project_dir,
        tool_name="run_program",
        timeout_sec=timeout_sec,
        env=run_env,
        capture_limit=capture_limit,
        command_log_path=command_log_path,
    )
    result["target_class"] = target_class
    result["threads_per_rank"] = threads_per_rank
    result["openmp_env_applied"] = openmp_env_applied
    if openmp_env_applied:
        result["openmp_env"] = {
            "OMP_NUM_THREADS": str(threads_per_rank),
            "OMP_THREAD_LIMIT": str(threads_per_rank),
        }
    return result


def tool_run_quality_checks(args: dict[str, Any]) -> dict[str, Any]:
    project_dir = str(args.get("project_dir", "."))
    _maybe_enforce_orchestration_mcp_gate(
        tool_name="run_quality_checks",
        project_dir=project_dir,
        args=args,
    )
    timeout_sec = _bounded_int(args.get("timeout_sec"), 1800, 1, "timeout_sec")
    capture_limit = _bounded_int(args.get("capture_limit"), 120000, 1000, "capture_limit")
    command_log_path = args.get("command_log_path")
    if command_log_path is not None and not isinstance(command_log_path, str):
        raise ValueError("command_log_path must be a string")
    _validate_orchestrated_paths(command_log_path, args, project_dir, "run_quality_checks")
    env = args.get("env")
    if env is not None and not isinstance(env, dict):
        raise ValueError("env must be an object")
    _validate_env_overrides(
        env, "run_quality_checks", orchestrated=_is_orchestrated_call(args),
        repo_root=_repo_root_for_call(args, project_dir))
    preset = str(args.get("preset", "make_test"))

    presets: dict[str, list[str]] = {
        "make_test": ["make", "test"],
        "make_check": ["make", "check"],
        "ctest": ["ctest", "--output-on-failure"],
        "pytest": ["pytest", "-q"],
    }

    if "command" in args:
        raise ValueError("run_quality_checks does not allow custom command; use preset")

    if preset in presets:
        command = presets[preset]
    else:
        supported = ", ".join(sorted(presets.keys()))
        raise ValueError(f"unsupported preset: {preset}. supported={supported}")

    run_env: dict[str, str] | None
    if env is None:
        run_env = None
    else:
        run_env = {str(k): str(v) for k, v in env.items()}

    if preset == "pytest":
        if run_env is None:
            run_env = {}
        project_path = str(Path(project_dir).resolve())
        # The caller cannot contribute PYTHONPATH (_validate_env_overrides), so the
        # only inherited value is this server's own.
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
    )
    result["preset"] = preset
    return result


def tool_run_linter(args: dict[str, Any]) -> dict[str, Any]:
    """Run static analysis linters for generated sources (Generate stage only).

    Presets invoke fixed commands; arbitrary user commands are not allowed.
    This is not compile_project and does not route through build_system.
    """
    project_dir = str(args.get("project_dir", "."))
    _maybe_enforce_orchestration_mcp_gate(
        tool_name="run_linter",
        project_dir=project_dir,
        args=args,
    )
    timeout_sec = _bounded_int(args.get("timeout_sec"), 1800, 1, "timeout_sec")
    capture_limit = _bounded_int(args.get("capture_limit"), 120000, 1000, "capture_limit")
    command_log_path = args.get("command_log_path")
    if command_log_path is not None and not isinstance(command_log_path, str):
        raise ValueError("command_log_path must be a string")
    _validate_orchestrated_paths(command_log_path, args, project_dir, "run_linter")
    env = args.get("env")
    if env is not None and not isinstance(env, dict):
        raise ValueError("env must be an object")
    _validate_env_overrides(
        env, "run_linter", orchestrated=_is_orchestrated_call(args),
        repo_root=_repo_root_for_call(args, project_dir))
    preset = str(args.get("preset", "fortitude")).strip().lower()

    if "command" in args:
        raise ValueError("run_linter does not allow custom command; use preset")

    run_env: dict[str, str] | None
    if env is None:
        run_env = None
    else:
        run_env = {str(k): str(v) for k, v in env.items()}

    if preset == "fortitude":
        command = ["fortitude", "check", "."]
        return _run_command(
            command=command,
            cwd=project_dir,
            tool_name="run_linter",
            timeout_sec=timeout_sec,
            env=run_env,
            capture_limit=capture_limit,
            command_log_path=command_log_path,
        ) | {"preset": preset}

    if preset == "cppcheck":
        command = [
            "cppcheck",
            "--error-exitcode=1",
            "--enable=warning,style,performance",
            "--inline-suppr",
            ".",
        ]
        return _run_command(
            command=command,
            cwd=project_dir,
            tool_name="run_linter",
            timeout_sec=timeout_sec,
            env=run_env,
            capture_limit=capture_limit,
            command_log_path=command_log_path,
        ) | {"preset": preset}

    if preset == "ruff":
        command = ["ruff", "check", "."]
        return _run_command(
            command=command,
            cwd=project_dir,
            tool_name="run_linter",
            timeout_sec=timeout_sec,
            env=run_env,
            capture_limit=capture_limit,
            command_log_path=command_log_path,
        ) | {"preset": preset}

    if preset == "mixed":
        r1 = _run_command(
            command=["fortitude", "check", "."],
            cwd=project_dir,
            tool_name="run_linter",
            timeout_sec=timeout_sec,
            env=run_env,
            capture_limit=capture_limit,
            command_log_path=command_log_path,
        )
        r2 = _run_command(
            command=[
                "cppcheck",
                "--error-exitcode=1",
                "--enable=warning,style,performance",
                "--inline-suppr",
                ".",
            ],
            cwd=project_dir,
            tool_name="run_linter",
            timeout_sec=timeout_sec,
            env=run_env,
            capture_limit=capture_limit,
            command_log_path=command_log_path,
        )
        return {
            "ok": bool(r1.get("ok")) and bool(r2.get("ok")),
            "preset": "mixed",
            "runs": [
                {"sub_preset": "fortitude", **r1},
                {"sub_preset": "cppcheck", **r2},
            ],
        }

    supported = "fortitude, cppcheck, ruff, mixed"
    raise ValueError(f"unsupported preset: {preset}. supported={supported}")


# --- run_syntax_check: compiler-frontend syntax gate (Generate stage only) ----------------
#
# Runs a real compiler front-end in syntax-only mode over the staged Fortran sources so the
# Generate stage catches, before Build, the whole class of syntax / standard-conformance
# errors the (non-compiling) post_generate text heuristics could only approximate one
# observed failure at a time. Producing NO build artifacts (module files go to a throwaway
# scratch dir inside project_dir), this is lint-class, not a build — it sits with
# run_linter outside the "compile must go through a standard build tool" rule.
#
# Compilers are an adapter REGISTRY (no custom commands, mirroring run_linter's
# preset-only rule). Each adapter builds the full argv from (std, scratch_dir, openmp,
# sources); the scratch dir is passed so a future adapter without a true syntax-only mode
# (e.g. Fujitsu frt, which would `-c` with objects discarded into the scratch dir) fits
# the same interface. Module files are compiler-/version-specific formats: every call
# gets its own scratch dir and must never share Build's $(OBJDIR).
#
# Three warning classes are promoted to errors over the whole staged set:
# unused-dummy-argument, unused-variable and ampersand. A dummy an interface fixes but the
# algorithm never consumes (an inert input, an ABI-fixed `name` / `case_id`) must stay a live
# dummy bound by `associate (unused_<name> => <name>); end associate` — the canonical idiom is
# docs/workflow/CHECKS_MODULE_CONTRACT.md §5, and this gate is what makes it load-bearing
# rather than advisory. The third promotes gfortran's extension of resuming a continued
# character literal with NO leading `&`: accepting it (issue #23 measured it at rc=0) let a
# counted-`do` spelling written inside a string reach a PHYSICAL line start, where the
# fail_closed OpenMP presence floor — anchored at line starts, deliberately stateless — counted
# it and falsely rejected a source this gate had passed (issue #25). Rejecting the shape here
# is what makes the floor's anchoring argument sound; a conforming literal resumes with `&` and
# is unaffected. Only these three classes are promoted: promoting -Wall/-Wextra wholesale would
# reject correct-as-written sources — -Wcompare-reals fires on the harness self-test runner's
# deliberate bitwise equality comparisons, which are the point of the assertion.

_FORTRAN_SYNTAX_SOURCE_SUFFIXES = (".f90", ".f95", ".f03", ".f08")
_SYNTAX_SCRATCH_DIR_NAME = ".mods"


def _gfortran_syntax_argv(
    std: str, scratch_dir: str, openmp: bool, sources: list[str]
) -> list[str]:
    argv = [
        "gfortran", "-fsyntax-only", f"-std={std}",
        # `-Werror=<class>` self-enables the warning, so no companion `-W<class>` is needed.
        "-Werror=unused-dummy-argument", "-Werror=unused-variable", "-Werror=ampersand",
        "-J", scratch_dir, "-I", scratch_dir,
    ]
    if openmp:
        argv.append("-fopenmp")
    return argv + list(sources)


_SYNTAX_COMPILER_ADAPTERS: dict[str, dict[str, Any]] = {
    "gfortran": {
        "exe": "gfortran",
        "argv": _gfortran_syntax_argv,
        "version_argv": ["gfortran", "--version"],
    },
}

# A source valid under every Fortran standard the adapters accept. `std` reaches the gate
# from the LLM-authored IR (`impl_defaults.toolchain.standard`) and goes straight into
# `-std=<value>`: a value the driver does not know (`-std=2008` — the elided-`f` form)
# makes it reject the COMMAND LINE, so no source is ever parsed and every file in the
# invocation "fails" at once. Compiling this canary with the same argv separates a broken
# invocation from broken sources by the compiler's own verdict, without enumerating the
# stds a given compiler version happens to accept (`f2023` exists on GCC>=13 but not
# before, so any hard-coded set is wrong on some machine).
SYNTAX_CANARY_SOURCE = "module metdsl_syntax_canary\n  implicit none\nend module metdsl_syntax_canary\n"

# `module <name>` definitions (excluding submodule-procedure headers) and `use <name>`
# references, scanned to order the staged sources so each module is compiled before its
# consumers within ONE compiler invocation (gfortran resolves same-invocation `use`
# against the module files it just wrote to the scratch dir, even under -fsyntax-only).
# Deliberately approximate: a mis-ordering only reorders the argv and the compiler then
# reports the real diagnosis; correctness judgment always stays with the compiler.
_FORTRAN_MODULE_DECL_RE = re.compile(
    r"^\s*module\s+(?!procedure\b|subroutine\b|function\b)([a-z][a-z0-9_]*)\s*(?:!.*)?$",
    re.IGNORECASE | re.MULTILINE,
)
# `use\b` (word boundary) so only a real `use` STATEMENT matches — `use foo`, `use::foo`,
# `use, intrinsic :: foo` — and an ordinary identifier that merely starts with the letters
# "use" (`user_flag = ...`, `usedcount = ...`) does NOT (there is no word boundary between
# `use` and a following word char). Without the boundary the old `use\s*` over-matched such
# names and minted a spurious dependency edge in the source ordering.
_FORTRAN_USE_STMT_RE = re.compile(
    r"^\s*use\b\s*(?:,\s*(?:non_)?intrinsic\s*)?(?:::)?\s*([a-z][a-z0-9_]*)",
    re.IGNORECASE | re.MULTILINE,
)


def _fortran_syntax_source_order(project_dir: Path) -> list[str]:
    """Topologically order the free-form Fortran sources in `project_dir` (define-before-use).

    `use` of a module no local file defines (intrinsic modules, and genuinely missing
    dependencies) is ignored for ordering — if it is a real omission the compiler emits
    the authoritative "Cannot open module file" finding. On a definition cycle the
    remaining files are appended name-sorted and the compiler diagnoses the cycle.
    """
    names = sorted(
        p.name
        for p in project_dir.iterdir()
        if p.is_file() and p.suffix.lower() in _FORTRAN_SYNTAX_SOURCE_SUFFIXES
    )
    provided_by: dict[str, str] = {}
    uses: dict[str, set[str]] = {}
    for name in names:
        try:
            text = (project_dir / name).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
        for mod in _FORTRAN_MODULE_DECL_RE.findall(text):
            provided_by.setdefault(mod.lower(), name)
        uses[name] = {mod.lower() for mod in _FORTRAN_USE_STMT_RE.findall(text)}

    ordered: list[str] = []
    placed: set[str] = set()
    remaining = list(names)
    while remaining:
        progressed = False
        for name in list(remaining):
            deps = {
                provided_by[mod]
                for mod in uses.get(name, set())
                if mod in provided_by and provided_by[mod] != name
            }
            if deps <= placed:
                ordered.append(name)
                placed.add(name)
                remaining.remove(name)
                progressed = True
        if not progressed:
            ordered.extend(remaining)
            break
    return ordered


@lru_cache(maxsize=8)
def _syntax_compiler_version(version_argv: tuple[str, ...]) -> str | None:
    """First line of `<compiler> --version`, cached per argv. A compiler's version is
    invariant for the process lifetime, so probe it once rather than re-spawning the
    extra subprocess on every syntax stage and every warm-resume retry (the conductor
    runs this tool in-process across the whole orchestration)."""
    try:
        proc = subprocess.run(
            list(version_argv), text=True, capture_output=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    first_line = (proc.stdout or proc.stderr or "").strip().splitlines()
    return first_line[0].strip() if first_line else None


def tool_run_syntax_check(args: dict[str, Any]) -> dict[str, Any]:
    """Run a compiler front-end in syntax-only mode over staged Fortran sources.

    Adapter-registry only; arbitrary user commands are not allowed. Produces no
    build artifacts (lint-class, not a build; does not route through build_system).
    A missing compiler binary returns {ok: True, skipped: True, ...} — whether a
    stage may be skipped (optional target-compiler stage) or must hard-fail
    (the mandatory gfortran stage) is the caller's policy, not this tool's.
    """
    project_dir = str(args.get("project_dir", "."))
    _maybe_enforce_orchestration_mcp_gate(
        tool_name="run_syntax_check",
        project_dir=project_dir,
        args=args,
    )
    timeout_sec = _bounded_int(args.get("timeout_sec"), 1800, 1, "timeout_sec")
    capture_limit = _bounded_int(args.get("capture_limit"), 120000, 1000, "capture_limit")
    command_log_path = args.get("command_log_path")
    if command_log_path is not None and not isinstance(command_log_path, str):
        raise ValueError("command_log_path must be a string")
    _validate_orchestrated_paths(command_log_path, args, project_dir, "run_syntax_check")
    env = args.get("env")
    if env is not None and not isinstance(env, dict):
        raise ValueError("env must be an object")
    _validate_env_overrides(
        env, "run_syntax_check", orchestrated=_is_orchestrated_call(args),
        repo_root=_repo_root_for_call(args, project_dir))
    compiler = str(args.get("compiler", "gfortran")).strip().lower()
    std = str(args.get("std", "f2008")).strip().lower()
    openmp = bool(args.get("openmp", False))

    if "command" in args:
        raise ValueError(
            "run_syntax_check does not allow custom command; use a registered compiler adapter"
        )

    adapter = _SYNTAX_COMPILER_ADAPTERS.get(compiler)
    if adapter is None:
        supported = ", ".join(sorted(_SYNTAX_COMPILER_ADAPTERS))
        raise ValueError(f"unsupported compiler: {compiler}. supported={supported}")

    sources = args.get("sources")
    if sources is not None and (
        not isinstance(sources, list) or not all(isinstance(s, str) for s in sources)
    ):
        raise ValueError("sources must be an array of source file names")

    proj = Path(project_dir)
    if not proj.is_dir():
        raise ValueError(f"project_dir is not a directory: {project_dir}")

    if shutil.which(str(adapter["exe"])) is None:
        return {
            "ok": True,
            "skipped": True,
            "compiler": compiler,
            "std": std,
            "reason": f"compiler not available: {adapter['exe']}",
        }

    ordered_sources = list(sources) if sources is not None else _fortran_syntax_source_order(proj)
    # The same rule for both readings. Auto-discovery filters on suffix alone, so a
    # staged file named `-o.f90` or `@resp.f90` walked into the compiler argv as an
    # option — and the workflow always takes this branch, since it passes no `sources`.
    # A stray one is a visible gate failure rather than a silently skipped file.
    _validate_syntax_sources(ordered_sources, project_dir, "run_syntax_check")
    if not ordered_sources:
        return {
            "ok": True,
            "skipped": True,
            "compiler": compiler,
            "std": std,
            "reason": "no fortran sources found",
        }

    (proj / _SYNTAX_SCRATCH_DIR_NAME).mkdir(exist_ok=True)

    run_env: dict[str, str] | None
    if env is None:
        run_env = None
    else:
        run_env = {str(k): str(v) for k, v in env.items()}

    argv_builder: Callable[[str, str, bool, list[str]], list[str]] = adapter["argv"]
    command = argv_builder(std, _SYNTAX_SCRATCH_DIR_NAME, openmp, ordered_sources)
    result = _run_command(
        command=command,
        cwd=project_dir,
        tool_name="run_syntax_check",
        timeout_sec=timeout_sec,
        env=run_env,
        capture_limit=capture_limit,
        command_log_path=command_log_path,
    )
    return result | {
        "compiler": compiler,
        "compiler_version": _syntax_compiler_version(tuple(adapter["version_argv"])),
        "std": std,
        "openmp": openmp,
        "skipped": False,
    }


TOOLS: dict[str, Tool] = {
    "detect_build_system": Tool(
        name="detect_build_system",
        description="Detect and recommend a dependency-aware build system in a project directory.",
        input_schema={
            "type": "object",
            "properties": {
                "project_dir": {"type": "string", "default": "."},
                "language": {"type": "string"},
            },
        },
        handler=tool_detect_build_system,
    ),
    "compile_project": Tool(
        name="compile_project",
        description=(
            "Compile using a dependency-aware standard build tool. "
            "For Fortran/C family, make/cmake/meson/ninja are allowed, and make is default."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "project_dir": {"type": "string", "description": "Absolute path under an orchestration."},
                "language": {"type": "string"},
                "build_system": {"type": "string"},
                "target": {
                    "type": "string",
                    "description": (
                        "Build target name. Refused under an orchestration — the build "
                        "is the Makefile's default goal."
                    ),
                },
                "jobs": {"type": "integer", "minimum": 1},
                "extra_args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Extra build-tool arguments. Under an orchestration only "
                        "assignments to OBJDIR, BINDIR, RUNDIR, BIN, SPEC, CASES are "
                        "accepted; a path value must be absolute and resolve inside "
                        "the repository and a name value must be an identifier, because "
                        "every value reaches the make recipe's shell unquoted."
                    ),
                },
                "timeout_sec": {"type": "integer", "minimum": 1},
                "capture_limit": {"type": "integer", "minimum": 1000},
                "command_log_path": {
                    "type": "string",
                    "description": (
                        "JSONL path for command logs. Relative paths are resolved "
                        "from project_dir; under an orchestration the path must stay "
                        "under the repository root."
                    ),
                },
                "env": _env_property_schema("compile_project"),
                **_ORCHESTRATION_GATE_PROPERTIES,
            },
            "required": ["project_dir"],
        },
        handler=tool_compile_project,
    ),
    "run_program": Tool(
        name="run_program",
        description=(
            "Run a program without shell expansion and capture stdout/stderr. "
            "When target_class is cpu and threads_per_rank is specified, "
            "set OpenMP thread env vars."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "project_dir": {"type": "string", "description": "Absolute path under an orchestration."},
                "command": {"type": "array", "items": {"type": "string"}},
                "timeout_sec": {"type": "integer", "minimum": 1},
                "capture_limit": {"type": "integer", "minimum": 1000},
                "command_log_path": {
                    "type": "string",
                    "description": (
                        "JSONL path for command logs. Relative paths are resolved "
                        "from project_dir; under an orchestration the path must stay "
                        "under the repository root."
                    ),
                },
                "target_class": {"type": "string"},
                "target.class": {"type": "string"},
                "target": {
                    "type": "object",
                    "properties": {
                        "class": {"type": "string"},
                    },
                    "additionalProperties": True,
                },
                "threads_per_rank": {"type": "integer", "minimum": 1},
                "env": _env_property_schema("run_program"),
                **_ORCHESTRATION_GATE_PROPERTIES,
            },
            "required": ["project_dir", "command"],
        },
        handler=tool_run_program,
    ),
    "run_quality_checks": Tool(
        name="run_quality_checks",
        description=(
            "Run quality checks through standard workflows. "
            "Supports presets (make_test/make_check/ctest/pytest)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "project_dir": {"type": "string", "description": "Absolute path under an orchestration."},
                "preset": {"type": "string", "default": "make_test"},
                "timeout_sec": {"type": "integer", "minimum": 1},
                "capture_limit": {"type": "integer", "minimum": 1000},
                "command_log_path": {
                    "type": "string",
                    "description": (
                        "JSONL path for command logs. Relative paths are resolved "
                        "from project_dir; under an orchestration the path must stay "
                        "under the repository root."
                    ),
                },
                "env": _env_property_schema("run_quality_checks"),
                **_ORCHESTRATION_GATE_PROPERTIES,
            },
            "required": ["project_dir"],
        },
        handler=tool_run_quality_checks,
    ),
    "run_linter": Tool(
        name="run_linter",
        description=(
            "Run static linters for Generate-stage source (fortitude/cppcheck/ruff/mixed). "
            "Does not use build_system or compile_project; preset-only, no custom command."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "project_dir": {"type": "string", "description": "Absolute path under an orchestration."},
                "preset": {
                    "type": "string",
                    "default": "fortitude",
                    "description": "fortitude | cppcheck | ruff | mixed",
                },
                "timeout_sec": {"type": "integer", "minimum": 1},
                "capture_limit": {"type": "integer", "minimum": 1000},
                "command_log_path": {
                    "type": "string",
                    "description": (
                        "JSONL path for command logs. Relative paths are resolved "
                        "from project_dir; under an orchestration the path must stay "
                        "under the repository root."
                    ),
                },
                "env": _env_property_schema("run_linter"),
                **_ORCHESTRATION_GATE_PROPERTIES,
            },
            "required": ["project_dir"],
        },
        handler=tool_run_linter,
    ),
    "run_syntax_check": Tool(
        name="run_syntax_check",
        description=(
            "Run a compiler front-end in syntax-only mode over staged Fortran sources "
            "(Generate-stage gate). Registered compiler adapters only (gfortran); "
            "no custom command, no build artifacts, does not route through build_system."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "project_dir": {"type": "string", "description": "Absolute path under an orchestration."},
                "compiler": {
                    "type": "string",
                    "default": "gfortran",
                    "description": "Registered compiler adapter id (gfortran).",
                },
                "std": {
                    "type": "string",
                    "default": "f2008",
                    "description": "Language standard from impl_defaults.toolchain.standard.",
                },
                "openmp": {
                    "type": "boolean",
                    "default": False,
                    "description": "Enable the adapter's OpenMP flag (target.backend=openmp).",
                },
                "sources": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Source file names in compile order — plain names in project_dir, "
                        "no paths and no compiler options. Omit to let the tool order the "
                        "project_dir Fortran sources by a module/use scan."
                    ),
                },
                "timeout_sec": {"type": "integer", "minimum": 1},
                "capture_limit": {"type": "integer", "minimum": 1000},
                "command_log_path": {
                    "type": "string",
                    "description": (
                        "JSONL path for command logs. Relative paths are resolved "
                        "from project_dir; under an orchestration the path must stay "
                        "under the repository root."
                    ),
                },
                "env": _env_property_schema("run_syntax_check"),
                **_ORCHESTRATION_GATE_PROPERTIES,
            },
            "required": ["project_dir"],
        },
        handler=tool_run_syntax_check,
    ),
}


def _tool_descriptor(tool: Tool) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "inputSchema": tool.input_schema,
    }


def _error_response(message_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": message_id,
        "error": {
            "code": code,
            "message": message,
        },
    }


def _success_response(message_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": message_id,
        "result": result,
    }


def _handle_request(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    message_id = message.get("id")
    params = message.get("params", {}) or {}

    if method == "initialize":
        protocol_version = params.get("protocolVersion", DEFAULT_PROTOCOL_VERSION)
        return _success_response(
            message_id,
            {
                "protocolVersion": protocol_version,
                "capabilities": {
                    "tools": {},
                },
                "serverInfo": {
                    "name": SERVER_NAME,
                    "version": SERVER_VERSION,
                },
            },
        )

    if method == "notifications/initialized":
        return None

    if method == "ping":
        return _success_response(message_id, {})

    if method == "tools/list":
        return _success_response(
            message_id,
            {
                "tools": [_tool_descriptor(tool) for tool in TOOLS.values()],
            },
        )

    if method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments", {}) or {}
        if tool_name not in TOOLS:
            return _error_response(message_id, -32602, f"unknown tool: {tool_name}")
        tool = TOOLS[tool_name]
        try:
            data = tool.handler(arguments)
            text = json.dumps(data, ensure_ascii=False, indent=2)
            return _success_response(
                message_id,
                {
                    "content": [{"type": "text", "text": text}],
                    "structuredContent": data,
                    "isError": False,
                },
            )
        except Exception as exc:  # noqa: BLE001
            error_data = {
                "error": str(exc),
            }
            text = json.dumps(error_data, ensure_ascii=False, indent=2)
            return _success_response(
                message_id,
                {
                    "content": [{"type": "text", "text": text}],
                    "structuredContent": error_data,
                    "isError": True,
                },
            )

    if message_id is None:
        return None
    return _error_response(message_id, -32601, f"method not found: {method}")


def main() -> int:
    while True:
        message = _read_message()
        if message is None:
            return 0
        if not isinstance(message, dict):
            continue
        response = _handle_request(message)
        if response is not None:
            _write_message(response)


if __name__ == "__main__":
    sys.exit(main())
