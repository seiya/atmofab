"""The `make` build system's `build_execute` capability (issue #424 PR-2).

What the in-process Build / Validate.execute bodies (`tools/workflow_conductor.py`
`_build_inproc` / `_execute_inproc`), the build-runtime server's `compile_project` /
`run_quality_checks` (`mcp_servers/build_runtime_server.py`) and the post_execute
quality-check gate (`tools/validate_pipeline_semantics.py`) need to know about driving `make`:
the argv a build runs, the variables it is handed, the quality-check preset it re-runs the
binary through, and what a build that reported success without a binary means. Every member
moved unchanged out of those three modules; the neutral core now asks the registry
(`registry.capability_module("build_system", <value>, "build_execute")`).

The file make reads, and where it writes its command log, are `control_file`'s
(`CONTROL_FILE_BASENAME`, `BUILDS_IN_SOURCE`).

Stdlib only; imports nothing from the neutral core.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

#: The `run_quality_checks` preset Validate.execute re-runs the built binary through (the
#: `make test` re-run the quality check compares against `run_program`, phase_04 §4-1). One of
#: `QUALITY_CHECK_COMMANDS`; it is also an input of the validate derivation key
#: (`orchestration_runtime.phase_derivation_inputs`, `run_policy.preset`).
QUALITY_CHECK_PRESET = "make_test"

#: The argv of each `run_quality_checks` preset this build system serves. The keys are
#: `control_file.QUALITY_CHECK_PRESETS`' (the target each requires the control file to declare);
#: `tools/tests/test_build_system_make.py` holds the two equal.
QUALITY_CHECK_COMMANDS: dict[str, tuple[str, ...]] = {
    "make_test": ("make", "test"),
    "make_check": ("make", "check"),
}

#: The suffixes of what a build writes beside the sources it compiles (objects and libraries),
#: which a fingerprint of the authored source tree skips. A language's module artifact is the
#: language's (`source_reading.MODULE_ARTIFACT_SUFFIX`).
BUILD_ARTIFACT_SUFFIXES: tuple[str, ...] = (".o", ".a", ".so")

#: `(failure_category, excerpt)` of a build that reported success and left no binary at the
#: imposed `$(BINDIR)/$(BIN)`: the control file's build rule does not honour `BIN`, which
#: regenerating the control file repairs.
BINARY_MISSING: tuple[str, str] = (
    "make_error", "the Makefile build rule must produce $(BINDIR)/$(BIN)")


def build_overrides(obj_dir: str, bin_dir: str, exe: str) -> tuple[str, ...]:
    """The command-line variables a build is handed: the out-of-source object and binary
    directories, and `BIN` imposed to the canonical binary name (a command-line assignment wins
    over any assignment in the Makefile). From one set of paths — the local ones or a remote
    job directory's — so the two sites cannot build differently. Validate.execute imposes the
    same `BIN` through `quality_check_env`."""
    return (f"OBJDIR={obj_dir}", f"BINDIR={bin_dir}", f"BIN={exe}")


def build_argv(target: str | None, jobs: int, extra_args: Iterable[str]) -> list[str]:
    """The argv `compile_project` runs: `make -j<jobs> [<target>] <extra_args>…`."""
    return ["make", f"-j{jobs}", *([target] if target else []), *extra_args]


def quality_check_preset(argv: Iterable[str]) -> str | None:
    """The preset of `QUALITY_CHECK_COMMANDS` a recorded quality-check argv ran, or `None`.

    argv[0]'s basename must be the preset's executable and the preset's target must be among the
    remaining tokens (case-insensitive, as the record is compared). `None` for anything else —
    another executable, or `make` naming neither target — which the gate refuses."""
    tokens = [str(token).strip().lower() for token in argv if str(token).strip()]
    if not tokens:
        return None
    executable, rest = Path(tokens[0]).name, set(tokens[1:])
    for preset, (preset_executable, target) in QUALITY_CHECK_COMMANDS.items():
        if executable == preset_executable and target in rest:
            return preset
    return None


def quality_check_env(obj_dir: str, bin_dir: str, run_dir: str, exe: str, spec_path: str,
                      case_ids: Iterable[str]) -> dict[str, str]:
    """The environment the quality-check re-run (`make test`) is handed.

    `BIN` is imposed to the canonical `<spec_id>_runner` so the test target's
    `$(BINDIR)/$(BIN)` guard resolves the binary Build produced; the preset passes overrides
    through the environment only, which overrides the Makefile's `BIN ?=` form (enforced by
    post_generate). `SPEC` / `CASES` are imposed so `make test` invokes the runner as
    `run_program` does (`--cases <spec.ir.yaml> <case_id>…`) — without them the test target's
    `--cases $(SPEC) $(CASES)` would fall back to the Makefile's baked defaults, and pinning them
    to the run's spec and case set keeps the quality check a true value comparison (the runner
    requires `--cases`). No dependency source is staged for it: the `test:` target has no build
    prerequisite, so it never recompiles and needs no closure source or module artifact in
    `OBJDIR`."""
    return {"OBJDIR": obj_dir, "BINDIR": bin_dir, "RUNDIR": run_dir, "BIN": exe,
            "SPEC": spec_path, "CASES": " ".join(case_ids)}
