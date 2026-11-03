"""The bounds-checked second build of a node's certified sources (issue #589 PR-0): the neutral
seam between the host and the backends that say what it is.

A capture-only verdict cannot see a write outside the captured arrays (issue #588). So `Build`
compiles the same control file and sources a second time under the target compiler's
`checked_flags` (a `compiler` axis capability: the flags are the backend's, held here as opaque
tokens), into `binary/<binary_id>/bin_checked/`, and `Validate.execute` runs that binary last, as
the quality check's `checked` row — one process, one thread, no launcher, under the serial
reference's environment — where an out-of-bounds write aborts the run. The post-execute gate
expects the row from the same declaration, never from a record the run wrote.

The three readers — the conductor's Build and Validate.execute, the post-execute gate
(`tools/validate_pipeline_semantics.py`) and the test fixtures — ask this module, so none of them
resolves the compiler, the control file's flag rules or the row's shape on its own. Kept apart
from `tools/host_execution.py`, whose whole file the render key digests: nothing here shapes a
rendered byte.

Imports only the registry (and `target_profile` lazily), so the validator can import it.
"""

from __future__ import annotations

from typing import Any

from tools.backends import registry


class CheckedBuildUnavailable(RuntimeError):
    """The target's compiler does not declare `checked_flags`, so nothing says whether a
    bounds-checked build of its sources is made (issue #589 PR-0). A host-side declaration
    defect, not a source's: the build and the gate both refuse rather than skip the row."""


def target_compiler(profile: Any) -> str:
    """The `compiler` axis value a binary of `profile` (a `target_profile.TargetProfile`) is
    compiled with: the profile's `toolchain.compiler` pin, else the language's
    `DEFAULT_COMPILER` (`bundle_facts`). The same answer
    `orchestration_runtime._target_toolchain_identity` records as `compiler` (a test couples
    the two). Under a parallel backend's compiler wrapper it is the compiler the wrapper runs —
    the launch gate refuses a pin beside a wrapper and a wrapper whose `WRAPPED_COMPILER` is not
    the language's default (`target_profile.target_profile_violations`) — and NOT the program
    the control file names, which is the wrapper (`workflow_conductor.build_compiler`)."""
    tc = profile.toolchain
    return str(tc.get("compiler") or "") or str(registry.capability_module(
        "language", tc["language"], "bundle_facts").DEFAULT_COMPILER)


def control_file_rules(profile: Any) -> dict[str, Any]:
    """The language backend's `control_file.rules(...)` for `profile`: its standard, parallel
    backend, hardware architecture and the backend's compiler wrapper — the inputs the
    conductor's control-file writer hands the same function (`Conductor._control_file_rules`; a
    test couples the two). The post-execute gate composes the checked build's expected
    variables from it (`build_execute.checked_build_overrides`)."""
    tc = profile.toolchain
    backend = profile.parallel_backend
    wrapper = (str(registry.capability_module(
        "parallel", backend, "compiler_wrapper").COMPILER_WRAPPER)
        if registry.provides("parallel", backend, "compiler_wrapper") else None)
    architecture = (profile.doc.get("hardware") or {}).get("architecture")
    return registry.capability_module("language", tc["language"], "control_file").rules(
        standard=str(tc["standard"]), parallel_backend=backend,
        architecture=str(architecture) if architecture else None, compiler_wrapper=wrapper)


def checked_flags_for(profile: Any) -> tuple[str, ...]:
    """The flags the target's compiler adds for the bounds-checked second build (its
    `checked_flags` capability, issue #589 PR-0), in order; empty when it declares none, in
    which case no checked binary is built and no `checked` row runs. `Build` composes the
    build from them and the post-execute gate expects the row from them, so neither reads the
    answer off a record the run wrote.

    Refuses (`CheckedBuildUnavailable`) a compiler whose record does not declare the
    capability: an absent declaration is not an empty one."""
    compiler = target_compiler(profile)
    reason = registry.missing_capability_reason("compiler", compiler, "checked_flags")
    if reason is not None:
        raise CheckedBuildUnavailable(f"toolchain compiler {compiler}: {reason}")
    module = registry.capability_module("compiler", compiler, "checked_flags")
    return tuple(str(f) for f in module.CHECKED_FLAGS)


#: The label of the bounds-checked binary's row in `quality_check.json#shapes` (issue #589
#: PR-0); no sweep label takes it (those are `grid_*`, `threads_*`, `block_*`).
CHECKED_SHAPE_LABEL = "checked"


def checked_shape(profile: Any) -> Any:
    """The `checked` row's `target_profile.ExecutionShape` for `profile`, or None when its
    compiler declares no checked flags (`checked_flags_for`). One process, one thread, no
    imposed process grid, the certified block size: the binary runs BARE — no launcher prefix
    on any target, under `host_execution.serial_execution_env`, as the quality check's serial reference does —
    so on a launcher target it is one process in the grid `(1, 1)`, not a shape of the profile.
    `Validate.execute` runs it last, after every sweep shape, and the post-execute gate expects
    it after them (`_validate_execution_shapes`)."""
    if not checked_flags_for(profile):
        return None
    from tools.target_profile import ExecutionShape

    return ExecutionShape(CHECKED_SHAPE_LABEL, 1, 1, None, profile.block_size)
