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

The selection is the run's TARGET PROFILE (`spec/targets/<target_id>.yaml`, issue #284): the
operator-authored document that names the language and build system every node of the run is
built with, so at launch there is something to read even though no IR exists yet. One run is one
target, and a `--with-deps` closure inherits it, so the target's selection stands for every
member. One LIMIT, stated rather than implied:

- A profile that pins `toolchain.compiler` has its BUILD compiler unprobed. Its mandatory syntax
  stage is still covered, since that stage is the language backend's
  `MANDATORY_SYNTAX_COMPILER` whatever the profile says, and a skipped mandatory stage is a
  `Generate.gate` fail_closed rather than a silent pass.

A parallel backend that declares `compiler_wrapper` (issue #316) adds its wrapper: the build
control file's compiler variable and the syntax stage's `argv[0]` are that program, so it is
needed from the first `Generate.gate`. `parallel_toolchain_problems` then asks the two questions
a resolved wrapper can still fail: whether it compiles the backend's binding canary, and — when
the backend's launcher resolves too — whether a program it builds, started under that launcher,
runs as one run.

The mid-run gates stay as the backstop. This is an earlier detector, not a replacement.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
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


def resolve_launch_axis_selection(target) -> dict[str, str]:
    """The axis values a run for `target` (a `TargetProfile`) will select. There is no default
    target: the checkout declares several profiles (issue #289, R4-b PR-5), and which one a run
    builds for is the launch's own target resolution (`tools/run_workflow.py`)."""
    # The language -> linter answer is the registry's, the one the conductor's
    # `_gate_lint_check` runs and the certification expects: a second copy would be a drift
    # pair, and this one would send the probe after a linter the gate never runs.
    from tools.backends import registry as backend_registry

    # The values are the profile's; nothing is defaulted, so this file spells no technology.
    language = target.toolchain["language"]
    build_system = target.toolchain["build_system"]

    preset = backend_registry.linter_for_language(language)
    if preset is None:
        raise RuntimeError(
            f"launch host prerequisite probe: toolchain.language={language!r} has no static lint "
            f"preset: no linter backend declares it in LANGUAGES (tools/backends/linter/)"
        )
    _require_implemented("language", language)
    return {
        "language": language,
        "build_system": build_system,
        "linter": preset,
        # The build control file's `FC` default and the mandatory syntax stage are this one
        # value, and it is the language backend's (`bundle_facts`); see the constant's comment.
        "compiler": str(backend_registry.capability_module(
            "language", language, "bundle_facts").MANDATORY_SYNTAX_COMPILER),
        # What the machine that executes the binary needs beyond it is the parallel backend's
        # (`execution_executables`, issue #307); the host's build tools do not read it.
        "parallel": target.parallel_backend,
    }


def required_host_executables(
    selection: dict[str, str],
) -> tuple[HostExecutable, ...]:
    """Every program the resolved selection needs on the host, in probe order, without repeats."""
    server = _build_runtime_server()

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

    # The program that stands in for the compiler at build and at the syntax stage (issue #316).
    parallel = selection.get("parallel")
    wrapper = _parallel_capability_module(parallel, "compiler_wrapper") if parallel else None
    if wrapper is not None:
        add("parallel", parallel, str(wrapper.COMPILER_WRAPPER))

    return tuple(found)


def _parallel_capability_module(backend_id: str, capability: str):
    """The package module of `backend_id`'s `capability`, None when its record does not declare
    it in a package."""
    from tools.backends import registry as backend_registry

    _require_implemented("parallel", backend_id)
    if capability not in backend_registry.get("parallel", backend_id).backend_provides:
        return None
    return backend_registry.capability_module("parallel", backend_id, capability)


#: Seconds each canary command (a compile, a build, a launch) may take.
BINDING_CANARY_TIMEOUT_SEC = 120


def _canary_run(argv: list[str], cwd: str) -> subprocess.CompletedProcess | str:
    """Run one canary command; its completed process, or why it could not run."""
    try:
        return subprocess.run(argv, text=True, capture_output=True, check=False,
                              timeout=BINDING_CANARY_TIMEOUT_SEC, cwd=cwd,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc)


def parallel_toolchain_problems(selection: dict[str, str], *, launches: bool,
                                ranks: int) -> list[str]:
    """What is wrong with the parallel backend's compiler wrapper this host resolves, beyond its
    presence (`missing_host_executables` asks that first): empty for a backend that declares no
    `compiler_wrapper`, and for a wrapper this host does not resolve. Two canaries, both the
    backend's, run in a scratch directory:

    * The wrapper must compile `BINDING_CANARY_SOURCE` syntax-only (`BINDING_CANARY_ARGV`): an
      installation whose wrapper cannot compile the language binding this backend's harness
      uses fails every node's syntax stage, the first of them after `Compile` and
      `Generate.generate` have been billed.
    * When the run LAUNCHES the binary (`launches`: it, or a dependency it drives, reaches
      `Validate`) with more than one rank, and the backend declares `launcher` and this host
      resolves it, a program built with the wrapper (`LAUNCH_CANARY_SOURCE`,
      `LAUNCH_CANARY_BUILD_ARGV`) and started under the launcher with `LAUNCH_CANARY_RANKS`
      must report ONE run of that many processes (`launch_canary_problem`). A run that stops
      before `Validate` starts nothing under the launcher, and a one-rank run is one process
      whichever installation starts it, so neither is asked: a launcher that cannot start two
      processes here must not refuse them. A launcher of another installation starts processes that each
      run alone and exit 0, and the node's own run would show it only after a billed Build.
      This asks the pair what it DOES: where the two programs sit says nothing, since one
      directory can hold two installations' programs (Debian's alternatives switch the
      launcher and the wrapper separately, both in `/usr/bin`). Asked only when the binding
      canary passed, since the launch canary is written in that binding.
    """
    parallel = selection.get("parallel")
    if not parallel:
        return []
    wrapper = _parallel_capability_module(parallel, "compiler_wrapper")
    if wrapper is None:
        return []
    wrapper_exe = str(wrapper.COMPILER_WRAPPER)
    wrapper_path = shutil.which(wrapper_exe)
    if wrapper_path is None:
        return []
    launcher = _parallel_capability_module(parallel, "launcher")
    launcher_path = shutil.which(str(launcher.EXECUTABLE)) if launcher is not None else None
    with tempfile.TemporaryDirectory(prefix="atmofab_parallel_canary_") as scratch:
        source = Path(scratch) / str(wrapper.BINDING_CANARY_FILENAME)
        source.write_text(str(wrapper.BINDING_CANARY_SOURCE), encoding="utf-8")
        completed = _canary_run(
            [wrapper_exe, *(str(a).format(scratch=scratch, source=str(source))
                            for a in wrapper.BINDING_CANARY_ARGV)], scratch)
        if isinstance(completed, str):
            return [f"parallel/{parallel}: the binding canary could not be compiled with "
                    f"{wrapper_path} ({completed})"]
        if completed.returncode != 0:
            tail = (completed.stderr or completed.stdout or "").strip()[-400:]
            return [f"parallel/{parallel}: {wrapper_path} does not compile the language binding "
                    f"this backend's harness uses (rc={completed.returncode}: {tail}); resolve "
                    f"the wrapper and the launcher to a {parallel} installation that provides "
                    f"it for the target's compiler"]
        if launcher is None or launcher_path is None or not launches or ranks <= 1:
            return []
        source = Path(scratch) / str(launcher.LAUNCH_CANARY_FILENAME)
        source.write_text(str(launcher.LAUNCH_CANARY_SOURCE), encoding="utf-8")
        exe = Path(scratch) / "launch_canary"
        built = _canary_run(
            [wrapper_exe, *(str(a).format(scratch=scratch, source=str(source), exe=str(exe))
                            for a in launcher.LAUNCH_CANARY_BUILD_ARGV)], scratch)
        if isinstance(built, str) or built.returncode != 0:
            detail = built if isinstance(built, str) else (
                f"rc={built.returncode}: "
                f"{(built.stderr or built.stdout or '').strip()[-400:]}")
            return [f"parallel/{parallel}: {wrapper_path} does not build the launch canary "
                    f"({detail})"]
        canary_ranks = int(launcher.LAUNCH_CANARY_RANKS)
        launched = _canary_run([*launcher.argv_prefix(canary_ranks), str(exe)], scratch)
        started = (f"a program built with {wrapper_path} and started under {launcher_path} "
                   f"with {canary_ranks} processes")
        # A launch that did not complete is the launcher's own refusal (too few slots, a user
        # it will not run as, a hang): its message is the diagnosis, and it says nothing about
        # the pairing. Only a launch that completed and reported the wrong run sizes does.
        if isinstance(launched, str) or launched.returncode != 0:
            detail = launched if isinstance(launched, str) else (
                f"exit {launched.returncode}: "
                f"{(launched.stderr or launched.stdout or '').strip()[-600:]}")
            return [f"parallel/{parallel}: {started} did not complete ({detail}); a run of "
                    f"{ranks} ranks is started the same way at Validate.execute"]
        problem = launcher.launch_canary_problem(launched.returncode, launched.stdout or "")
        if problem is not None:
            return [f"parallel/{parallel}: {started} did not run as one run: {problem}. The "
                    f"launcher and the compiler wrapper must come from one {parallel} "
                    f"installation"]
    return []


def execution_executables(selection: dict[str, str]) -> tuple[str, ...]:
    """The programs the machine that EXECUTES the binary needs beyond the binary itself, for the
    resolved selection: the parallel backend's launcher's and device trace's
    (`host_execution.execution_executables`; issues #316, #307), none for a backend that
    declares neither. Asked of a
    remote site through `required_site_executables`, and of this host when the local site
    executes the run (`run_workflow._sites_rejection`)."""
    from tools.host_execution import execution_executables as _for_backend

    return _for_backend(selection["parallel"])


def required_site_executables(selection: dict[str, str], *, scheduler: str) -> tuple[str, ...]:
    """The programs a remote execution site's non-interactive LOGIN must resolve for a job of the
    resolved selection (issue #293): what the job script itself needs beyond the POSIX utilities
    (`remote_execution.REMOTE_EXECUTABLES`), the build system, whose test target the quality
    check runs there, what the binary runs under (`execution_executables`, issue #307) — only
    for a site that runs the job on the login it reaches (`execution_sites.DIRECT_SCHEDULER`) —
    and what the site's `scheduler` runs the job under (`remote_execution.scheduler_executables`).
    Read out of the tables that run them, like `required_host_executables`.

    A batch scheduler runs the job on another node, and what the binary runs under need not be
    installed on the login this probe reaches: at the `cpp_gpu` site the device trace's program
    is on the compute nodes' PATH and absent from the login node (measured 2026-09-27, issue
    #307), so asking the login refused every run there. For such a site it is asked where the
    binary runs, by the job script, which checks each command's program before running it
    (`remote_execution.render_job_script`, exit 4, a transport `fail_closed` that `--resume`
    retries). The build system is still asked of the login, as it was before issue #307."""
    from tools.execution_sites import DIRECT_SCHEDULER
    from tools.remote_execution import REMOTE_EXECUTABLES, scheduler_executables

    server = _build_runtime_server()
    build_system = selection["build_system"]
    _require_implemented("build_system", build_system)
    runs_on_login = scheduler == DIRECT_SCHEDULER
    found: list[str] = []
    for executable in (*REMOTE_EXECUTABLES, server.build_system_executable(build_system),
                       *(execution_executables(selection) if runs_on_login else ()),
                       *scheduler_executables(scheduler)):
        if executable not in found:
            found.append(executable)
    return tuple(found)


def _tool_version_text(version_argv: tuple[str, ...]) -> str | None:
    """What the program prints for its own version, whole, or `None` when it cannot be read.

    Copied in shape from `_syntax_compiler_version` in `mcp_servers/build_runtime_server.py`,
    including the failure polarity: a program that cannot be started, times out, or prints
    nothing yields `None`, and the CALLER decides what an unreadable version means. Here the
    caller is a launch gate, and the backend's own clause refuses it.

    WHOLE, not the first line: where the version sits in the output is the backend's knowledge
    (its `parse_version` reads it), and one supported program prints it on the fourth line
    (issue #289, R4-b PR-4: the CUDA compiler driver's first line is its name). The other
    backends' programs print one line, so for them the two are the same text.
    """
    try:
        completed = subprocess.run(
            list(version_argv), text=True, capture_output=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (completed.stdout or completed.stderr or "").strip()
    return text or None


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


def _self_check_reason(module) -> str | None:
    """Run a capability module's own launch self-check, or `None` when it has none to run.

    The version arm answers "is this build one we measured against"; this answers the question
    that survives a yes — "can this build actually run what we declare". They are different: a
    supported version can still refuse the declared invocation if a code in it was withdrawn in
    a patch release nobody measured. The check runs over an EMPTY directory, so the answer is a
    bare exit status and nothing is parsed; the backend module that answers it states why.
    """
    build = getattr(module, "self_check_argv", None)
    verdict = getattr(module, "self_check_reason", None)
    if build is None or verdict is None:
        return None
    with tempfile.TemporaryDirectory() as empty:
        try:
            completed = subprocess.run(
                list(build(empty)), text=True, capture_output=True, timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"the launch self-check could not be run ({exc})"
    return verdict(completed.returncode, completed.stdout or "", completed.stderr or "")


def unsupported_host_tool_versions(
    selection: dict[str, str],
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
            # A supported version that still cannot run what this repository declares is refused
            # here too, and for the same reason the version half exists: the alternative is that
            # it surfaces mid-run as a gate failure attributed to a leaf's source.
            if reason is None:
                reason = _self_check_reason(module)
            if reason is not None:
                found.append(
                    HostToolVersion(item.axis, item.backend_id, item.executable,
                                    version_text, reason)
                )
    return tuple(found)


def missing_host_executables(
    selection: dict[str, str],
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
