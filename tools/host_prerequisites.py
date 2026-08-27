"""What the HOST must have installed before a run's first leaf is launched.

A prerequisite whose absence terminalizes a run is checked at launch. This repository has
re-derived that rule three times — `tools/run_workflow.py`'s `REQUIRED_CLI_TOOLS` and
`REQUIRED_PYTHON_MODULES` comments state it, and `TODO.md` records a third member of the class
still open. What was missing is a place that enumerates the LAST family: the executables the
run's own axis selection implies — the `static lint` tool of the node's language, its build
system, and its compiler. Without this, an absent linter first surfaces at `Generate.gate`, after
`Compile` and `Generate.generate` have already been billed; an absent compiler lands in the same
place one check later (the gate turns a skipped mandatory syntax stage into a fail_closed), and an
absent build system one phase later still, at `Build` (issue #109).

Two properties are the point:

- **No tool name is written here.** Every executable is argv[0] of the command that will
  actually run it, read out of the table that runs it — `lint_preset_executables` /
  `build_system_executable` / `syntax_compiler_executable` in
  `mcp_servers/build_runtime_server.py`. A probe that spelled its own name could look for a
  program the gate never launches. This one cannot, and it adds no technology knowledge to a
  `neutral core` file (`AGENTS.md` §Backend boundary rules, `docs/BACKEND_BOUNDARY.md`).
- **Every axis value is asked of the registry first.** `tools/backends/registry.py` answers
  whether a value is a declared, implemented member. An unregistered one is a build-tooling bug,
  not a host one, and is refused with the registry's own clause rather than probed — the
  `unimplemented_reason` question, because this code is about to decide what a run will execute.

LIMIT, stated rather than implied. This resolves the selection a run gets when no IR pins
otherwise. It does NOT read a node's `spec.ir.yaml`: at a cold start the first phase is
`Compile`, which is the phase that AUTHORS that file, so at launch there is nothing to read.
That is not a guess — `_validate_toolchain_backend_supported`
(`tools/validate_pipeline_semantics.py`) fails any other `(build_system, language)` pair at
`Compile.static`, so the default selection is the only one a run can reach. Two consequences to
keep in view:

- An IR that pins `impl_defaults.toolchain.compiler` has its BUILD compiler unprobed. Its
  mandatory syntax stage is still covered, since that stage is the conductor's `DEFAULT_COMPILER`
  whatever the IR says, and a skipped mandatory stage is a `Generate.gate` fail_closed rather than
  a silent pass.
- A `--with-deps` closure is not walked; the target node's selection stands for the closure,
  which holds only while the toolchain gate above pins every node to the same pair.

The mid-run gates stay as the backstop. This is an earlier detector, not a replacement.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

_REPO_ROOT = Path(__file__).resolve().parent.parent


class HostExecutable(NamedTuple):
    """One program the host must have, and the axis selection that asked for it."""

    axis: str
    backend_id: str
    executable: str


class HostToolVersion(NamedTuple):
    """One required program whose installed version is refused, and why.

    `version` is what the program reported, or `None` when nothing could be read — both states
    are refusals, and keeping the text lets the launch message say which one happened.
    """

    axis: str
    backend_id: str
    executable: str
    version: str | None
    reason: str


def _build_runtime_server():
    """The MCP server module, reached the way the conductor's in-process gate bodies reach it.

    It pulls in no third-party package and imports in milliseconds, so paying for it on the
    launch path costs nothing measurable; the alternative — a second copy of the argv tables — is
    the drift this module exists to prevent. (It is no longer stdlib-ONLY: a `linter` row whose
    argv has moved into its backend package is composed by reaching
    `tools/backends/registry.py`. Both modules it reads are themselves stdlib-only.)
    """
    mcp_dir = str(_REPO_ROOT / "mcp_servers")
    if mcp_dir not in sys.path:
        sys.path.insert(0, mcp_dir)
    import build_runtime_server

    return build_runtime_server


def _require_implemented(axis: str, backend_id: str) -> None:
    """Refuse an axis value the registry does not answer for, carrying its clause verbatim."""
    from tools.backends import registry as backend_registry

    reason = backend_registry.unimplemented_reason(axis, backend_id)
    if reason is not None:
        raise RuntimeError(
            f"launch host prerequisite probe: {axis}={backend_id!r} is not runnable — {reason}"
        )


def resolve_launch_axis_selection() -> dict[str, str]:
    """The axis values a run started now will select, per the LIMIT in the module docstring."""
    # The language -> linter mapping, from the module that owns it. The conductor's
    # `_gate_lint_check` reaches the same private name for the same reason: a second copy is a
    # drift pair, and this one would send the probe after a linter the gate never runs.
    from tools.validate_pipeline_semantics import _LINT_PRESET_FOR_LANGUAGE
    from tools.workflow_conductor import DEFAULT_COMPILER, _ir_build_system, _ir_language

    # The `language` and `build_system` defaults are not written here: they are what the
    # conductor's OWN readers answer for an IR that pins nothing, obtained by ASKING them with an
    # empty document. A constant restated here would be a second spelling of the same default —
    # the drift `_validate_toolchain_backend_supported` already carries three SHAPE checks
    # against — and it would put two technology tokens into this file for nothing.
    language = _ir_language({})
    build_system = _ir_build_system({})

    preset = _LINT_PRESET_FOR_LANGUAGE.get(language)
    if preset is None:
        raise RuntimeError(
            f"launch host prerequisite probe: toolchain.language={language!r} has no static lint "
            f"preset mapping (expected one of {sorted(_LINT_PRESET_FOR_LANGUAGE)})"
        )
    return {
        "language": language,
        "build_system": build_system,
        "linter": preset,
        # The build control file's `FC` default and the mandatory syntax stage are this one
        # value; see the constant's own comment for why it is not the server's.
        "compiler": DEFAULT_COMPILER,
    }


def required_host_executables(
    selection: dict[str, str] | None = None,
) -> tuple[HostExecutable, ...]:
    """Every program the resolved selection needs on the host, in probe order, without repeats."""
    server = _build_runtime_server()
    selection = selection if selection is not None else resolve_launch_axis_selection()

    found: list[HostExecutable] = []
    seen: set[str] = set()

    def add(axis: str, backend_id: str, executable: str) -> None:
        if executable in seen:
            return
        seen.add(executable)
        found.append(HostExecutable(axis, backend_id, executable))

    _require_implemented("language", selection["language"])

    # A composite preset (one that runs several linters in order) is attributed to the SUB-preset
    # that needs each program, not to the composite: the sub-preset is the registered `linter`
    # member, so it is what the registry can be asked about and what an operator installs.
    for sub_preset in server.lint_preset_sub_presets(selection["linter"]):
        _require_implemented("linter", sub_preset)
        for executable in server.lint_preset_executables(sub_preset):
            add("linter", sub_preset, executable)

    build_system = selection["build_system"]
    _require_implemented("build_system", build_system)
    add("build_system", build_system, server.build_system_executable(build_system))

    compiler = selection["compiler"]
    _require_implemented("compiler", compiler)
    add("compiler", compiler, server.syntax_compiler_executable(compiler))

    return tuple(found)


def _tool_version_text(version_argv: tuple[str, ...]) -> str | None:
    """The first line the program prints for its own version, or `None` when it cannot be read.

    Copied in shape from `_syntax_compiler_version` in `mcp_servers/build_runtime_server.py`,
    including the failure polarity: a program that cannot be started, times out, or prints
    nothing yields `None`, and the CALLER decides what an unreadable version means. Here the
    caller is a launch gate, and the backend's own clause refuses it.
    """
    try:
        completed = subprocess.run(
            list(version_argv), text=True, capture_output=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    first_line = (completed.stdout or completed.stderr or "").strip().splitlines()
    return first_line[0].strip() if first_line else None


#: The capabilities whose backend package decides whether the installed build of its program may
#: run at all. Membership here is what MAKES the two-name protocol below mandatory: a capability
#: outside this tuple is not asked (its package need not answer, and a `runner_render` package
#: does not), and one inside it that cannot answer fails the suite rather than the launch
#: (`tools/tests/test_host_prerequisites.py`).
#:
#: An explicit tuple rather than "ask whichever module happens to have the names": duck-typing a
#: MANDATORY protocol makes a rename in a backend package turn this whole arm off silently, which
#: is the fail-open shape this repository keeps re-introducing. The first version of this module
#: asked every `backend_provides` capability and would have raised `AttributeError` at launch the
#: first time a language-axis executable reached it.
#:
#: These are capability names, not technology names — the property the module docstring states.
_VERSION_GATED_CAPABILITIES = ("lint",)


def _version_gated_capability_modules(item: HostExecutable):
    """The capability modules of `item`'s record that gate on the installed version.

    The protocol is two names — `version_argv` and `unsupported_version_reason` — mandatory for
    every `_VERSION_GATED_CAPABILITIES` member a record implements in its own package.

    A capability still inlined in the neutral core declares no package and is not reached here;
    its version, if it ever needs one, is that area's to add when it migrates.
    """
    from tools.backends import registry as backend_registry

    record = backend_registry.get(item.axis, item.backend_id)
    for capability in sorted(record.backend_provides & set(_VERSION_GATED_CAPABILITIES)):
        yield backend_registry.capability_module(item.axis, item.backend_id, capability)


def unsupported_host_tool_versions(
    selection: dict[str, str] | None = None,
) -> tuple[HostToolVersion, ...]:
    """Those required programs whose installed version must not decide a certification.

    The second half of the same launch-time question `missing_host_executables` asks. A tool that
    is PRESENT but of an unmeasured version is the failure this half exists for, and it is not
    hypothetical: a `linter` vendor turned 18 rules on by default in one minor release, which
    made every node of that language uncertifiable on a freshly installed host while nothing in
    this repository had changed (issue #110 / #111).

    No version, range, or tool name is written in this module — the same property the executable
    half has, for the same reason. Both the probe argv and the verdict come from the backend
    package that declares the capability, so this file cannot look for a build the gate never
    runs, and cannot disagree with the rule set that build will be handed.
    """
    found: list[HostToolVersion] = []
    for item in required_host_executables(selection):
        for module in _version_gated_capability_modules(item):
            version_text = _tool_version_text(tuple(module.version_argv()))
            reason = module.unsupported_version_reason(version_text)
            if reason is not None:
                found.append(
                    HostToolVersion(item.axis, item.backend_id, item.executable,
                                    version_text, reason)
                )
    return tuple(found)


def missing_host_executables(
    selection: dict[str, str] | None = None,
) -> tuple[HostExecutable, ...]:
    """Those of `required_host_executables` this host cannot resolve on `PATH`.

    `shutil.which`, the same probe `tools/run_workflow.py` uses for `REQUIRED_CLI_TOOLS` and
    `tool_run_syntax_check` uses before it runs a stage.
    """
    return tuple(
        item
        for item in required_host_executables(selection)
        if shutil.which(item.executable) is None
    )
