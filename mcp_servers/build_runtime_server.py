#!/usr/bin/env python3
"""Minimal MCP server for build/run/quality operations.

This server intentionally has no THIRD-PARTY dependencies so that it can be used
in constrained environments. It does read two modules of this checkout:
`tools/orchestration_runtime.py` (by file location) and `tools/backends/registry.py`
(by dotted import, because the registry resolves a backend package by module path).
Both are stdlib-only.
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
def _backend_registry() -> Any:
    """Load `tools/backends/registry.py`, the one door to a backend package.

    A DOTTED import: the registry resolves a backend by importing its dotted module path, so
    `tools` has to be importable as a package here rather than merely readable as a file.
    (This module used to carry a second, file-location loader for
    `tools/orchestration_runtime.py`, whose one consumer was the orchestration capability
    gate — retired in issue #171.) `tools/` is a namespace package, so the
    checkout root on `sys.path` is all that takes; the bootstrap mirrors
    `tools/validate_pipeline_semantics.py`'s.

    The registry is stdlib-only and imports no sibling at module level, so it is cheap and
    introduces no cycle (`tools/host_prerequisites.py`
    imports THIS module; this module imports the registry; the registry imports nothing).
    """
    _disable_bytecode_writes()
    try:
        from tools.backends import registry
    except ModuleNotFoundError:
        root = str(Path(__file__).resolve().parent.parent)
        if root not in sys.path:
            sys.path.insert(0, root)
        from tools.backends import registry

    return registry


def _refuse_retired_arguments(args: dict[str, Any], tool_name: str) -> None:
    """Refuse an argument this server used to read and no longer does.

    `capability_token` named a secret in `capabilities/<agent_run_id>.json`, which the
    orchestration gate compared against the launch record before serving a call. The gate
    existed because the caller might be a LEAF holding a grant it should not be able to
    widen; since Z4 (issue #171) no leaf reaches this server at all — a pure leaf launches
    with `--tools ""` and `--strict-mcp-config` and carries no MCP configuration — so the
    only caller under a run is the conductor, in the host process, and what it owes the
    server is attribution rather than authority.

    Refused rather than ignored: a caller still sending one is running against a contract
    this server no longer implements, and reading it as a no-op would serve the call as if
    the check had passed.

    `run_program` retired four more (issue #289, R4-b PR-1): `target_class`, `target.class`,
    `target` and `threads_per_rank`. From them the server derived an OpenMP environment for a
    `cpu` class and ran every other class with none, so a `gpu` target reached a CPU run
    without a word. The launch environment is now composed by the host from the target profile
    (`tools/host_execution.py`) and arrives as `env`; a caller still sending the old arguments
    would otherwise run with no thread variables at all while believing it had set them. They
    are refused on `run_program` alone — `target` is `compile_project`'s build goal.
    """
    # One remedy per retirement the call actually hit, so each refused argument is answered
    # with ITS replacement: a `capability_token` alone on `run_program` was once told to pass
    # an `env` (round 3).
    by_tool = _RETIRED_ARGUMENTS_BY_TOOL.get(tool_name, ())
    offending = sorted(key for key in _RETIRED_ARGUMENTS + by_tool if key in args)
    if offending:
        remedies = []
        if any(key in args for key in _RETIRED_ARGUMENTS):
            remedies.append(_CAPABILITY_TOKEN_REMEDY)
        if any(key in args for key in by_tool):
            remedies.append(_RETIRED_ARGUMENT_REMEDY[tool_name])
        raise ValueError(
            f"{tool_name} no longer accepts " + ", ".join(offending) + ": "
            + "; ".join(remedies)
        )


_RETIRED_ARGUMENTS = ("capability_token",)
_CAPABILITY_TOKEN_REMEDY = (
    "the orchestration capability gate was retired in issue #171; pass "
    "orchestration_id / agent_run_id for attribution instead")
_RETIRED_ARGUMENTS_BY_TOOL: dict[str, tuple[str, ...]] = {
    "run_program": ("target_class", "target.class", "target", "threads_per_rank"),
}
_RETIRED_ARGUMENT_REMEDY: dict[str, str] = {
    "run_program": (
        "the launch environment is the caller's to compose (issue #289: the workflow builds it "
        "from the target profile in tools/host_execution.py); pass it as env"),
}


def _bounded_int(raw: Any, default: int, minimum: int, name: str) -> int:
    """An integer argument, held to the minimum its served schema declares."""
    if raw is None:
        return default
    value = int(raw)
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum} (got {value})")
    return value


_UNSAFE_ENV_OVERRIDE_KEYS = frozenset({
    "BASH_ENV", "ENV", "IFS", "PATH", "PYTHONPATH",
    # gcc/gfortran is a driver: it finds and execs its own front end (`f951`), `as`
    # and `ld` through these, none of which is PATH or LD_*.
    "COMPILER_PATH", "GCC_EXEC_PREFIX", "LIBRARY_PATH",
    # GNU make parses these as command-line switches / extra makefiles, so
    # `--eval=$(shell ...)` runs before the certified Makefile is read.
    "MAKEFLAGS", "GNUMAKEFLAGS", "MAKEFILES", "MAKESHELL",
    # make's own spelling is the special variable `.SHELLFLAGS`, and it is taken from the
    # ENVIRONMENT as well as from the command line. Measured on GNU Make 4.3:
    #   env '.SHELLFLAGS=-c ./evil.sh' make all   ->  ./evil.sh runs, the recipe does not,
    #                                                 and make exits 0
    # The leading dot is normalised off before the lookup (`_normalized_override_name`), so
    # both spellings are refused on both halves. This lived in the argv-only set until the
    # measurement above; "matters only on a command line" was an assumption.
    "SHELLFLAGS",
    # ONE SET FOR BOTH HALVES, and the reason is the history. `SHELL` and `MAKE` sat in an
    # argv-only set on the written premise that they "matter only on a command line", and
    # that premise was wrong for `MAKE`: measured on GNU Make 4.3, against a Makefile whose
    # recipe calls `$(MAKE)`,
    #   env 'MAKE=./evil' make all   ->  ./evil runs, the inner recipe does not, rc 0
    # `MAKE_COMMAND` is the same mechanism under make's other spelling. `SHELL` genuinely is
    # argv-only — make sets it itself and ignores the environment's, verified — and it is
    # here anyway, because the SET DIFFERENCE is the defect class: issue #171 PR-2's review
    # found the same hole three times (`.SHELLFLAGS` in argv then in env, then `MAKE`), and
    # round 6 unified the NORMALISER while leaving the sets apart, so the class reopened one
    # level up. A name that is argv-only costs nothing in the env half; a name missing from
    # one half is a hole by construction.
    "SHELL", "MAKE", "MAKE_COMMAND",
})
_UNSAFE_ENV_OVERRIDE_PREFIXES = ("LD_", "DYLD_")

# The SAME names, refused as a make command-line ASSIGNMENT. A command-line assignment
# overrides even a hard assignment in the Makefile, so this surface carries MORE authority
# than the environment, not less — and `SHELL=` is not the `FC` class it was first grouped
# with: it replaces the interpreter of every recipe line, which is arbitrary execution rather
# than a redirected compiler. `make SHELL=./evil all` runs `./evil` (measured, GNU Make 4.3).
# THE SAME SET, deliberately not a superset. Every name make reads as a redirection of what
# is executed is refused on both halves, whichever way it arrives — an environment key and a
# command-line assignment are one vocabulary to make, so two sets is two answers to one
# question. The alias is kept as a name because the two call sites read differently.
_UNSAFE_ASSIGNMENT_NAMES = _UNSAFE_ENV_OVERRIDE_KEYS

# The make recipe interpolates a make variable's value unquoted (`cd $(RUNDIR) &&
# $(BINDIR)/$(BIN) --cases $(SPEC) $(CASES)`), so a value carrying a character that
# line's shell or make acts on is a command rather than a value. A space is not one of
# them: it splits words in the recipe, but `CASES` is a word LIST by contract and a
# checkout path may legitimately hold one.
#
# THIS SET OVER-REFUSES, deliberately and with a named cost. `~ [ ] * ? { }` are in it and
# only some of them break something: a leading `~` in `$(BINDIR)/$(BIN)` IS tilde-expanded
# by the recipe's shell, and a glob character matching a real file silently redirects the
# command — but `/a/b~c` is literal, `{}` is literal under `/bin/sh`, and a glob that
# matches nothing is literal too. So a checkout under `/home/user-1/x[1]` is refused by
# name, and the recipe would have run. The refusal is LOUD and names the character, the
# operator learns immediately, and narrowing the set means one measurement per character on
# a surface that cannot confine anyone (see the scope paragraph in
# `_validate_build_argv_overrides`) — the longer list the five review rounds on this surface
# argue against. Recorded rather than narrowed.
_SHELL_ACTIVE_CHARS = set("\t\n\r;&|$`'\"\\<>()*?[]{}~#!")


def _normalized_override_name(raw: Any) -> str:
    """The name make reads, from the name a caller spelled.

    ONE normaliser for both halves. `env` keys and `extra_args` assignment names are the
    same vocabulary — make imports an environment name as a variable and reads a
    command-line assignment as one — so a normalisation applied to one half and not the
    other is a hole on the weaker half by construction. That is how `.SHELLFLAGS` stayed
    reachable through `env` after it was refused in `extra_args`: the argv side stripped the
    leading dot and the env side did not.

    Surrounding space, a leading `.` (make's special-variable spelling) and the flavour
    operators `:` `+` `!` `?` come off; the result is upper-cased, which over-refuses
    (make variables are case-sensitive) rather than under-refuses.
    """
    return str(raw).strip().rstrip(":+!?").strip().lstrip(".").upper()


def _validate_env_overrides(env: Any, tool_name: str) -> None:
    """Refuse an environment override that redirects what the command executes.

    Every tool here except `run_program` (whose `command` is caller-chosen argv by
    design) constrains argv to a fixed preset or a build-tool invocation, and the
    environment goes around that constraint: the loader takes `LD_PRELOAD`, the gcc
    driver takes `COMPILER_PATH` to find the front end it execs, and make takes
    `MAKEFLAGS` as command-line switches.

    ONE mode since Z4 (issue #171). There were two: an ALLOWLIST of the make variables
    the workflow declares, switched on by an `orchestration_id` argument or by the
    workflow environment variables, and this denylist for everything else. The allowlist
    existed because the caller might be a LEAF whose grant it had to bound; no leaf
    reaches this server any more (`--tools ""`, `--strict-mcp-config`, no MCP
    configuration at all), so the only caller under a run is the conductor in the host
    process, and the allowlist bounded nobody. What survives is the denylist, which
    catches a mistake rather than confining a caller — and it now applies to the
    conductor's calls too, which it did not before.

    The VALUE rule is the same one `_validate_build_argv_overrides` applies, and it is
    here for the same reason: make imports an environment name as a make variable, so a
    value arriving this way is interpolated unquoted into the host-authored recipe
    exactly as a command-line assignment would be. Refusing it in one half and accepting
    it in the other guards nothing.

    What the retired allowlist did and this does NOT: bound the names a MAKEFILE reads. `FC`
    is not an execution-redirecting name, and make imports it as the variable a certified
    build control file's compiler comes from. That is a real gap and it is recorded rather
    than closed, because closing it means an allowlist, an allowlist bounds a caller's GRANT,
    and there is no longer a caller whose grant needs bounding: no leaf reaches this
    server, and the conductor passes a fixed six-key dict it composes itself. A denylist
    over names does not terminate, which is why this one covers only the names that
    redirect what is EXECUTED rather than what a build control file reads.

    Call this on the raw `env` argument, before the server composes its own addition
    (`PYTHONPATH` for the pytest preset) — that is the server's own decision and is not
    caller-controlled. A parallel runtime's thread variables used to be a second such
    addition on `run_program`; since issue #289 they are the caller's and arrive here, and
    none of them is an execution-redirecting name.
    """
    if not env:
        return
    offending = sorted(
        str(key)
        for key in env
        if (norm := _normalized_override_name(key)) in _UNSAFE_ENV_OVERRIDE_KEYS
        or norm.startswith(_UNSAFE_ENV_OVERRIDE_PREFIXES)
    )
    if offending:
        raise ValueError(
            f"{tool_name} does not accept env overrides that redirect execution: "
            + ", ".join(offending)
        )
    unsafe = sorted(
        str(key) for key, value in env.items()
        if set(str(value)) & _SHELL_ACTIVE_CHARS
    )
    if unsafe:
        raise ValueError(
            f"{tool_name} env values reach the make recipe's shell: refused "
            + ", ".join(unsafe)
        )


# A build-tool command line takes a make VARIABLE ASSIGNMENT and nothing else. The name
# is what decides: `--eval=$(shell ...)` and `--load-average=8` both carry an `=`, and
# make reads them as switches that run before the certified Makefile is read at all.
_MAKE_ASSIGNMENT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# `target` reaches the build tool's argv POSITIONALLY for every build system here
# (`make -jN <target>`, `ninja -jN <target>`, `cmake --build . --target <target>`), so it
# is the same surface `extra_args` is: anything that opens with `-` is a SWITCH the build
# tool reads before the certified control file, and a metacharacter is a command rather
# than a name. The orchestrated arm refused a target outright until Z4 (issue #171) because
# the caller might be a leaf; no leaf reaches this server, but the rule that kept
# `--eval=$(shell ...)` off the command line was doing a second job — catching a defect in
# a caller this repository writes — and that job is not retired, so it applies to every
# caller now.
#
# Stated as what is REFUSED rather than as an allowlist of name characters. A first attempt
# spelled the allowlist and refused real goals across the build systems this server serves:
# `:app:assembleDebug` (gradle), `build:prod` (an npm script), `lib.so:shared_library`
# (meson), `%.o` (a make pattern goal). None of those is dangerous, and an allowlist over a
# grammar this server does not own answers a question it cannot know.
_TARGET_REFUSED_CHARS = _SHELL_ACTIVE_CHARS | set(" ")


def _build_syntax_source_re(suffixes: tuple[str, ...]) -> re.Pattern[str]:
    """A source name: a file name carrying one of the language's source suffixes.

    `suffixes` is the language's `syntax_promotions.SOURCE_SUFFIXES`, the set auto-discovery
    also filters on, so an added suffix cannot make auto-discovery accept a file an explicit
    `sources` list refuses."""
    alternation = "|".join(re.escape(s.lstrip(".")) for s in suffixes)
    return re.compile(rf"^[A-Za-z0-9_][A-Za-z0-9_.+-]*\.({alternation})$", re.IGNORECASE)


#: The build systems whose command line takes a VARIABLE ASSIGNMENT and interpolates its
#: value into a shell recipe. `make` is also the build system the WORKFLOW uses, so it is
#: the one command line this repository composes rather than the operator: the shape rule
#: is here to catch a defect in that composition.
#:
#: The ground is NOT "a switch is harmless elsewhere", which was the first version of this
#: comment and is false for two of the eleven: `cmake` forwards `extra_args` to the native
#: tool after `--`, and `ninja` reads `-f` as its build file and `-t` as a subtool. It is
#: that outside the workflow the caller is the OPERATOR, who chose the argv and owns the
#: machine — defending them against their own switches is out of scope
#: (`AGENTS.md` §Development premises), and refusing them makes the tool unusable for the
#: build systems its own schema advertises. The two rules that are NOT about argv shape —
#: no execution-redirecting assignment, no character a shell acts on — apply to every
#: build system and to `target` as well, because those catch a composition defect rather
#: than bound a caller.
_ASSIGNMENT_ARGV_BUILD_SYSTEMS = frozenset({"make"})


def _is_shell_assignment(element: str) -> bool:
    """``NAME!=<command>`` — make's SHELL ASSIGNMENT, which executes its value.

    Not a spelling of a name: an OPERATOR, and the danger is the operator. `FOO!=touch
    /tmp/x` runs `touch /tmp/x` while make reports the build as succeeding, whatever `FOO`
    is called, so no name denylist can reach it. Measured on GNU Make 4.3:

        make 'FOO!=touch /tmp/mk2/PWN' all   ->  "REAL"   and /tmp/mk2/PWN exists

    A first version of this module's normalisation stripped `!` as if it were part of the
    name, which turned an arbitrary-execution operator into a lookup that could never
    match. Refused on EVERY build system: `cmake` forwards `extra_args` to the native tool
    after `--`, so a make command line is reachable from more than `build_system=make`.
    """
    head = element.split("=", 1)[0] if "=" in element else ""
    return head.rstrip().endswith("!")


def _is_execution_redirecting_assignment(element: str) -> bool:
    """``NAME=value`` whose NAME make reads as a redirection of what is EXECUTED.

    The name is normalised the way MAKE reads it, not the way the string is spelled.
    Measured against GNU Make 4.3 with a `SHELL` that prints instead of running the
    recipe. ALL of these execute `./evil` as every recipe line's interpreter:

        SHELL=./evil    SHELL:=./evil    SHELL::=./evil
        SHELL+=./evil   SHELL!=./evil     SHELL=./evil   (leading space)

    Only `SHELL?=` does not, because `SHELL` is already set. So every assignment operator
    make accepts — `:` `+` `!` `?`, and `::` — and the surrounding space come off before
    the lookup. The list is measured, not derived from the manual: `!=` is the shell-
    assignment operator and was the one a first version of this normalisation missed.

    Without that, this predicate misses `SHELL:=` and the only thing refusing it is the
    make-ONLY assignment-shape rule — two rules each covering half of one hole, which is
    the shape that has reopened three times on this branch. One rule, self-sufficient.
    """
    if "=" not in element:
        return False
    name = _normalized_override_name(element.split("=", 1)[0])
    return (name in _UNSAFE_ASSIGNMENT_NAMES
            or name.startswith(_UNSAFE_ENV_OVERRIDE_PREFIXES))


def _refuse_execution_redirecting_assignments(
    elements: list[str], tool_name: str, where: str
) -> None:
    """One rule, asked of EVERY caller-chosen argv element — whichever argument carries it.

    `target` and `extra_args` land on the same command line, so make cannot tell them apart:
    `make -jN SHELL=./evil` is an assignment whether the caller put that string in `target`
    or in `extra_args`. Refusing it in one argument and accepting it in the other guards
    nothing, which is the sentence this module has now had to learn twice — once for the
    env/argv pair, and once for the target/extra_args pair, where widening the target rule
    from a name allowlist to a metacharacter refusal dropped `=` out of both sets.
    """
    executing = sorted(e for e in elements if _is_shell_assignment(e))
    if executing:
        raise ValueError(
            f"{tool_name} does not accept a shell assignment (NAME!=command) in {where}: "
            "make EXECUTES its value; refused " + ", ".join(executing)
        )
    offending = sorted(e for e in elements if _is_execution_redirecting_assignment(e))
    if offending:
        raise ValueError(
            f"{tool_name} does not accept {where} that redirect execution: "
            + ", ".join(offending)
        )


def _validate_build_argv_overrides(
    target: Any, extra_args: Any, tool_name: str, *, build_system: str = "make"
) -> str | None:
    """Constrain the caller-chosen part of the build argv; return the target to use.

    Both halves of the caller-chosen argv are constrained, because both land on the same
    command line. `extra_args` is appended to it, where a make assignment overrides even
    a hard assignment in the Makefile: an element must ASSIGN a make variable — which is
    what keeps `--eval=$(shell ...)` and every other switch out — its NAME must not be one
    make reads as a redirection of what is executed, and its value must not carry a character
    the recipe's shell acts on. `target` is placed positionally on the same line (`make -jN
    <target>`), so a `target` that opens with `-` is that same switch by another argument, and
    it must be a build GOAL name.

    The NAME rule is the twin of `_validate_env_overrides`'s denylist and must not be weaker,
    because an assignment on the command line overrides even a hard assignment in the Makefile
    while an environment name does not. `SHELL=./evil` replaces the interpreter of every recipe
    line; `MAKEFILES=` reads an attacker's makefile before the certified one; `MAKEFLAGS=-n`
    makes the build execute nothing and still answer rc 0, which is a PASS over a binary that
    was never built. The round-1 fix made only the VALUE rule symmetric and named `FC` —
    the weakest member — as the residue, which understated what was open.

    WHAT THESE RULES ARE, and it bounds how much they are worth. `run_program`'s `command`
    is caller-chosen argv by design and is constrained by nothing, so a caller that wants to
    execute an arbitrary program calls THAT tool. These rules therefore cannot confine the
    caller they are checked against — they catch a MISTAKE in the one composition this
    repository writes (`workflow_conductor._build_inproc`, a fixed three-element list of
    host paths), and they are documented that way everywhere they are documented. A finding
    here is judged as a defect in that composition, never as an escape: rounds 1-5 of issue
    #171 PR-2's review spent five rounds on this surface and each round found another make
    spelling, which is what the shape of the surface predicts — the answer is a rule stated
    once and derived, not a longer list.

    ONE mode since Z4 (issue #171), like `_validate_env_overrides` above and for the same
    reason. The orchestrated arm held THREE further things: an allowlist of six variable
    NAMES, a containment rule on the four whose value is a path, and an outright refusal
    of any `target`. The first two bounded a leaf's grant and no leaf reaches this server,
    so they are retired. The third is not retired but GENERALIZED: refusing every target
    was a grant bound, while refusing a SWITCH spelled as a target catches the second
    defended class — a defect in a caller this repository writes — and so applies to every
    caller, including the standalone one, which had no check on either half before.

    The validated `target` is returned so the string that was checked is the string that
    runs.
    """
    if target is not None and not isinstance(target, str):
        raise ValueError(f"{tool_name} target must be a string")
    if not isinstance(extra_args, list) or not all(isinstance(a, str) for a in extra_args):
        raise ValueError(f"{tool_name} extra_args must be an array of strings")
    resolved_target = target.strip() if isinstance(target, str) and target.strip() else None
    if resolved_target is not None:
        if resolved_target.startswith("-"):
            raise ValueError(
                f"{tool_name} target must be a build goal, not a switch: refused "
                f"{resolved_target}"
            )
        if set(resolved_target) & _TARGET_REFUSED_CHARS:
            raise ValueError(
                f"{tool_name} target must be a build goal, not a switch: refused "
                f"{resolved_target} (it carries whitespace or a character the shell acts on)"
            )
        _refuse_execution_redirecting_assignments(
            [resolved_target], tool_name, "a target")
        if build_system in _ASSIGNMENT_ARGV_BUILD_SYSTEMS and "=" in resolved_target:
            raise ValueError(
                f"{tool_name} target must be a build goal, not a variable assignment: "
                f"refused {resolved_target} (make reads a positional NAME=value as an "
                "assignment, never as a goal; pass it in extra_args, where the assignment "
                "rules apply)"
            )
    # The two rules that hold for EVERY build system run FIRST, so the refusal a caller
    # reads names the strongest reason rather than whichever rule happened to be checked
    # first: `FOO!=touch /tmp/x` is arbitrary execution, not a malformed make assignment,
    # and reporting it as the latter is how round 4 came to believe it was handled.
    _refuse_execution_redirecting_assignments(extra_args, tool_name, "extra_args")
    if build_system in _ASSIGNMENT_ARGV_BUILD_SYSTEMS:
        offending = [
            arg for arg in extra_args
            if "=" not in arg
            or not _MAKE_ASSIGNMENT_NAME_RE.match(arg.split("=", 1)[0])
        ]
        if offending:
            raise ValueError(
                f"{tool_name} accepts only make variable assignments (NAME=value) in "
                "extra_args; refused: " + ", ".join(offending)
            )
    unsafe = sorted(
        arg for arg in extra_args
        if set(arg.split("=", 1)[1] if "=" in arg else arg) & _SHELL_ACTIVE_CHARS
    )
    if unsafe:
        raise ValueError(
            f"{tool_name} extra_args carry a character a shell acts on: refused "
            + ", ".join(unsafe)
        )
    return resolved_target


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
        raise SyntaxSourceNameError(
            f"{tool_name} sources must be {language} source files in project_dir; "
            "refused: " + ", ".join(sorted(offending))
        )


SERVER_VERSION = "0.1.0"
DEFAULT_PROTOCOL_VERSION = "2024-11-05"
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

_ENV_PROPERTY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": {"type": "string"},
    "description": (
        "Environment overrides for the command. Keys that redirect execution (LD_*, "
        "DYLD_*, PATH, PYTHONPATH, BASH_ENV, ENV, IFS, COMPILER_PATH, "
        "GCC_EXEC_PREFIX, LIBRARY_PATH, MAKEFLAGS, GNUMAKEFLAGS, MAKEFILES, "
        ".SHELLFLAGS, MAKESHELL, SHELL, MAKE) are refused, and so is any VALUE carrying a character a shell "
        "acts on -- make imports an environment name as a make variable and the "
        "recipe interpolates it unquoted."
    ),
}

# Attribution, not authority. `capability_token` used to sit here beside them and name a
# secret the orchestration gate checked; the gate was retired in issue #171 and the
# argument is now refused outright (`_refuse_retired_arguments`), so a caller written
# against the old contract fails instead of being served as if it had passed.
_ATTRIBUTION_PROPERTIES: dict[str, Any] = {
    "orchestration_id": {
        "type": "string",
        "description": (
            "Optional. The orchestration this call belongs to. Recorded in the "
            "command log entry so a command can be traced back to the run that issued "
            "it; it decides nothing about what this server will execute."
        ),
    },
    "agent_run_id": {
        "type": "string",
        "description": (
            "Optional. The agent run this call belongs to, recorded in the command log "
            "entry beside orchestration_id."
        ),
    },
    "repo_root": {
        "type": "string",
        "description": (
            "Repository root. Optional and unused by this server since issue #171; "
            "accepted so an existing caller keeps working."
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

    Both fields are optional and neither decides anything: the server runs the same
    command with them, without them, and with any value in them. They are here so a line
    in `command_log.jsonl` can be traced back to the orchestration and agent run that
    issued it — which is what the retired capability gate produced as a SIDE EFFECT of
    checking a token, and the only part of it anything downstream reads
    (`docs/workflow/MCP_COMMAND_LOG_PLACEMENT.md`).
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


def build_system_executable(build_system: str) -> str:
    """The host executable `build_system` builds through.

    argv[0] of the same `_build_command` a build runs, so the launch-time host probe
    (`tools/host_prerequisites.py`) cannot look for a different program than `compile_project`
    later launches. An unsupported build system raises `_build_command`'s own ValueError rather
    than a second refusal written here.
    """
    return _build_command(build_system, None, 1, [])[0]


def tool_detect_build_system(args: dict[str, Any]) -> dict[str, Any]:
    project_dir = str(args.get("project_dir", "."))
    # Advisory and capability-free: it runs nothing, and it reports which of eleven
    # marker files exist in the directory it is pointed at. It used to be REFUSED when
    # the workflow environment variables were set, because a leaf could reach it and the
    # read was outside the boundary the read manifest drew. No leaf reaches this server
    # since Z4 (issue #171), and the conductor does not call this tool at all — the build
    # system comes from the IR's toolchain — so the refusal guarded nothing and the
    # server is a general tool for its operator again.
    _refuse_retired_arguments(args, "detect_build_system")
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
    _refuse_retired_arguments(args, "compile_project")
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
    extra_args = args.get("extra_args", [])
    env = args.get("env")
    if env is not None and not isinstance(env, dict):
        raise ValueError("env must be an object")
    _validate_env_overrides(env, "compile_project")

    build_system = args.get("build_system")
    if build_system:
        build_system = str(build_system).strip().lower()
    else:
        build_system = _recommended_build_system(project_dir, language)["build_system"]

    if build_system not in DEPENDENCY_AWARE_BUILD_SYSTEMS:
        raise ValueError(
            "build_system must be a standard dependency-aware build tool"
        )
    # Resolved FIRST, because the argv rules differ by build system: only make reads an
    # `extra_args` element as a variable assignment interpolated into a shell recipe.
    target = _validate_build_argv_overrides(
        target, extra_args, "compile_project", build_system=build_system)

    if _is_compiled_language(str(language or "")) and build_system not in {
        "make",
        "cmake",
        "meson",
        "ninja",
    }:
        raise ValueError(
            "for a compiled language, use make/cmake/meson/ninja. make is the default."
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
        attribution=_attribution(args),
    )
    result["language"] = language or None
    result["build_system"] = build_system
    return result


def tool_run_program(args: dict[str, Any]) -> dict[str, Any]:
    project_dir = str(args.get("project_dir", "."))
    _refuse_retired_arguments(args, "run_program")
    timeout_sec = _bounded_int(args.get("timeout_sec"), 3600, 1, "timeout_sec")
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
    _validate_env_overrides(env, "run_program")

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
    _refuse_retired_arguments(args, "run_quality_checks")
    timeout_sec = _bounded_int(args.get("timeout_sec"), 1800, 1, "timeout_sec")
    capture_limit = _bounded_int(args.get("capture_limit"), 120000, 1000, "capture_limit")
    command_log_path = args.get("command_log_path")
    if command_log_path is not None and not isinstance(command_log_path, str):
        raise ValueError("command_log_path must be a string")
    env = args.get("env")
    if env is not None and not isinstance(env, dict):
        raise ValueError("env must be an object")
    _validate_env_overrides(env, "run_quality_checks")
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


#: The simple `static lint` presets, in one place. Every one of them now authors its own argv in
#: its backend package; this tuple is the set of NAMES, which is a different fact and stays here.
#:
#: It is not derived from anything in this module on purpose — there is nothing here to derive it
#: from — and a name in it whose record does not declare the `lint` capability fails at import,
#: in the dict comprehension below, rather than at the first call.
_SIMPLE_LINT_PRESETS: tuple[str, ...] = ("fortitude", "cppcheck", "ruff")

#: The argv each simple preset runs, composed once at import. The KEYS are the set above — the
#: set every reader below iterates.
_LINT_PRESET_COMMANDS: dict[str, tuple[str, ...]] = {
    preset: _lint_preset_command(preset) for preset in _SIMPLE_LINT_PRESETS
}

#: A preset that runs several linters in order, named by the presets it COMPOSES rather than by
#: their argv: the previous spelling restated `fortitude`'s and `cppcheck`'s command lines a
#: second time inside the `mixed` branch, so a flag change reached one invocation and not the
#: other.
_LINT_PRESET_COMPOSITES: dict[str, tuple[str, ...]] = {
    "mixed": ("fortitude", "cppcheck"),
}


def _check_lint_preset_declarations() -> None:
    """Fail at import on a preset table these two readers would disagree about.

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


def tool_run_linter(args: dict[str, Any]) -> dict[str, Any]:
    """Run static analysis linters for generated sources (Generate stage only).

    Presets invoke fixed commands; arbitrary user commands are not allowed.
    This is not compile_project and does not route through build_system.
    """
    project_dir = str(args.get("project_dir", "."))
    _refuse_retired_arguments(args, "run_linter")
    timeout_sec = _bounded_int(args.get("timeout_sec"), 1800, 1, "timeout_sec")
    capture_limit = _bounded_int(args.get("capture_limit"), 120000, 1000, "capture_limit")
    command_log_path = args.get("command_log_path")
    if command_log_path is not None and not isinstance(command_log_path, str):
        raise ValueError("command_log_path must be a string")
    env = args.get("env")
    if env is not None and not isinstance(env, dict):
        raise ValueError("env must be an object")
    _validate_env_overrides(env, "run_linter")

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
    runs = [
        _run_command(
            command=list(_LINT_PRESET_COMMANDS[sub]),
            cwd=project_dir,
            tool_name="run_linter",
            timeout_sec=timeout_sec,
            env=run_env,
            capture_limit=capture_limit,
            command_log_path=command_log_path,
            attribution=_attribution(args),
        )
        for sub in sub_presets
    ]
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
    """Run a compiler front-end in syntax-only mode over staged sources.

    Registered adapters only; arbitrary user commands are not allowed. Produces no
    build artifacts (lint-class, not a build; does not route through build_system).
    A missing compiler binary returns {ok: True, skipped: True, ...} — whether a
    stage may be skipped (an optional stage) or must hard-fail (the language's
    mandatory stage) is the caller's policy, not this tool's.

    `compiler` and `std` are REQUIRED: both are the caller's target facts (the language's
    mandatory syntax compiler, the profile's `toolchain.standard`), and a default here would
    be one language's spelling in a tool that serves every language.
    """
    project_dir = str(args.get("project_dir", "."))
    _refuse_retired_arguments(args, "run_syntax_check")
    timeout_sec = _bounded_int(args.get("timeout_sec"), 1800, 1, "timeout_sec")
    capture_limit = _bounded_int(args.get("capture_limit"), 120000, 1000, "capture_limit")
    command_log_path = args.get("command_log_path")
    if command_log_path is not None and not isinstance(command_log_path, str):
        raise ValueError("command_log_path must be a string")
    env = args.get("env")
    if env is not None and not isinstance(env, dict):
        raise ValueError("env must be an object")
    _validate_env_overrides(env, "run_syntax_check")

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

    adapter = syntax_adapter(compiler)
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

    executable = str(adapter.EXECUTABLE)
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
        "compiler_version": _syntax_compiler_version(tuple(adapter.VERSION_ARGV)),
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
            "For a compiled language, make/cmake/meson/ninja are allowed, and make is default."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "project_dir": {"type": "string", "description": "Directory the command runs in."},
                "language": {"type": "string"},
                "build_system": {"type": "string"},
                "target": {
                    "type": "string",
                    "description": (
                        "Build goal. Must not open with - (that is a switch), must "
                        "carry no whitespace or character the shell acts on, must not "
                        "name a redirection of what is executed (SHELL, MAKE, "
                        "MAKEFILES, MAKEFLAGS, LD_*, PATH, ...), and under "
                        "build_system=make must not be a variable assignment at all -- "
                        "make reads a positional NAME=value as an assignment, never as "
                        "a goal. Refused for every caller."
                    ),
                },
                "jobs": {"type": "integer", "minimum": 1},
                "extra_args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Extra build-tool arguments. Under build_system=make each "
                        "element must ASSIGN a make variable (NAME=value), because make "
                        "reads anything else as a switch it applies before the control "
                        "file; other build systems take their own switches. For every "
                        "build system: no element may use make's shell assignment "
                        "(NAME!=command), which EXECUTES its value whatever the name is; "
                        "an assignment must not name something make reads as a "
                        "redirection of what is executed -- the same set the env half "
                        "refuses (SHELL, .SHELLFLAGS, MAKE, MAKEFILES, MAKEFLAGS, LD_*, "
                        "PATH, ...); and no element may carry "
                        "a character a shell acts on, because make interpolates a value "
                        "into the recipe unquoted. Applies to every caller."
                    ),
                },
                "timeout_sec": {"type": "integer", "minimum": 1},
                "capture_limit": {"type": "integer", "minimum": 1000},
                "command_log_path": {
                    "type": "string",
                    "description": (
                        "JSONL path for command logs. Relative paths are resolved "
                        "from project_dir."
                    ),
                },
                "env": _ENV_PROPERTY_SCHEMA,
                **_ATTRIBUTION_PROPERTIES,
            },
            "required": ["project_dir"],
        },
        handler=tool_compile_project,
    ),
    "run_program": Tool(
        name="run_program",
        description=(
            "Run a program without shell expansion and capture stdout/stderr. "
            "The launch environment (e.g. a parallel runtime's thread count) is the "
            "caller's, passed as env; target_class / target.class / target / "
            "threads_per_rank are refused."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "project_dir": {"type": "string", "description": "Directory the command runs in."},
                "command": {"type": "array", "items": {"type": "string"}},
                "timeout_sec": {"type": "integer", "minimum": 1},
                "capture_limit": {"type": "integer", "minimum": 1000},
                "command_log_path": {
                    "type": "string",
                    "description": (
                        "JSONL path for command logs. Relative paths are resolved "
                        "from project_dir."
                    ),
                },
                "env": _ENV_PROPERTY_SCHEMA,
                **_ATTRIBUTION_PROPERTIES,
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
                "project_dir": {"type": "string", "description": "Directory the command runs in."},
                "preset": {"type": "string", "default": "make_test"},
                "timeout_sec": {"type": "integer", "minimum": 1},
                "capture_limit": {"type": "integer", "minimum": 1000},
                "command_log_path": {
                    "type": "string",
                    "description": (
                        "JSONL path for command logs. Relative paths are resolved "
                        "from project_dir."
                    ),
                },
                "env": _ENV_PROPERTY_SCHEMA,
                **_ATTRIBUTION_PROPERTIES,
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
                "project_dir": {"type": "string", "description": "Directory the command runs in."},
                "preset": {
                    "type": "string",
                    "description": "fortitude | cppcheck | ruff | mixed",
                },
                "timeout_sec": {"type": "integer", "minimum": 1},
                "capture_limit": {"type": "integer", "minimum": 1000},
                "command_log_path": {
                    "type": "string",
                    "description": (
                        "JSONL path for command logs. Relative paths are resolved "
                        "from project_dir."
                    ),
                },
                "env": _ENV_PROPERTY_SCHEMA,
                **_ATTRIBUTION_PROPERTIES,
            },
            "required": ["project_dir", "preset"],
        },
        handler=tool_run_linter,
    ),
    "run_syntax_check": Tool(
        name="run_syntax_check",
        description=(
            "Run a compiler front-end in syntax-only mode over staged sources "
            "(Generate-stage gate). Registered compiler adapters only (the `compiler` axis "
            "values whose record declares `syntax_check` in tools/backends/registry.py); "
            "no custom command, no build artifacts, does not route through build_system."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "project_dir": {"type": "string", "description": "Directory the command runs in."},
                "compiler": {
                    "type": "string",
                    "description": (
                        "Registered compiler adapter id; its language decides which files "
                        "are sources and which warning classes are promoted to errors."
                    ),
                },
                "std": {
                    "type": "string",
                    "description": "Language standard from the target profile's toolchain.standard.",
                },
                "openmp": {
                    "type": "boolean",
                    "default": False,
                    "description": "Enable the adapter's OpenMP flag (the target profile's parallel.backend=openmp).",
                },
                "architecture": {
                    "type": "string",
                    "description": (
                        "The target profile's hardware.architecture. An adapter whose "
                        "compiler takes a device architecture passes it; one that has none "
                        "accepts it and does not read it."
                    ),
                },
                "sources": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Source file names in compile order — plain names in project_dir, "
                        "no paths and no compiler options. Omit to let the tool order the "
                        "project_dir sources the way the adapter's language orders them."
                    ),
                },
                "timeout_sec": {"type": "integer", "minimum": 1},
                "capture_limit": {"type": "integer", "minimum": 1000},
                "command_log_path": {
                    "type": "string",
                    "description": (
                        "JSONL path for command logs. Relative paths are resolved "
                        "from project_dir."
                    ),
                },
                "env": _ENV_PROPERTY_SCHEMA,
                **_ATTRIBUTION_PROPERTIES,
            },
            "required": ["project_dir", "compiler", "std"],
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
