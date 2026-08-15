#!/usr/bin/env python3
"""The neutral seam between the host and whichever language backend renders a node's runner glue.

On an M3c node the host — not a leaf — authors the runner that drives the physics kernel over
the certified harness. The TEXT of that runner is language knowledge and lives in the language
backend (`tools/backends/<axis>/<backend_id>/`, `docs/BACKEND_BOUNDARY.md`); the DECISION to
author it, and the routing to the value that can, are neutral and live here.

Every function takes the node's `language` (the `runner_render` capability's axis, read from
`ir.impl_defaults.toolchain.language`) and dispatches through `registry.capability_module`. A
value that does not declare `runner_render` is REFUSED with the registry's own
`missing_capability_reason` wording, verbatim — the alternative, falling through to whichever
backend happens to be extracted, is the authorship flip the capability question exists to
prevent.

`RenderError` is defined HERE rather than in the backend, and that is deliberate. Three call
sites catch it (`workflow_conductor._write_runner`, and the compile gate through
`ir_content_violations`), and a class obtained through `registry.load` is not the class an
`except` clause in a neutral module names — the clause would have to be `except Exception`,
which is the same as no clause at all. The backend imports it from here and raises it.
"""

from __future__ import annotations

from types import ModuleType
from typing import Any

from tools.backends import registry



class RenderError(RuntimeError):
    """A physics IR that cannot be faithfully rendered into runner glue.

    Raised for a structural impossibility (an unrenderable shape, a rank the harness has no
    emitter for, a reserved-key collision, unsupported verdict fields, >1 infra dep, an
    over-long identifier). The conductor routes it to transport fail_closed — it is a spec/IR
    defect, not content a Generate retry could repair by re-authoring the model.

    ``identity=True`` marks the subset whose offending value is the node's IDENTITY (spec_id / a
    derived symbol's length, or >1 infra dep) rather than authored IR *content*. Re-authoring the
    IR cannot repair an identity defect, so the compile.static mirror (``ir_content_violations``)
    excludes it — it belongs to spec-input validation (`tools/spec_input_gates.py`), NOT a
    compile.generate warm-resume retry. Every other RenderError (``identity=False``) is
    Compile-authored content the compile gate hoists so a defect routes to compile.generate
    instead of killing the workflow at conductor render time."""

    def __init__(self, message: str, *, identity: bool = False) -> None:
        super().__init__(message)
        self.identity = identity


class RunnerRenderUnavailable(RuntimeError):
    """The node's language does not declare `runner_render`, so no backend can author its runner.

    Distinct from `RenderError`: nothing was found wrong with the IR. A caller that reached here
    asked a language that has no host renderer to produce one, which means the dispatch that
    decides authorship (`_conductor_authors_runner`) and this seam disagreed — a host bug, or a
    node that should have taken the leaf-authored path.
    """


def _module(language: Any) -> ModuleType:
    """The backend module that implements `runner_render` for `language`, or raise.

    `registry.capability_module` refuses on both grounds that matter — a value whose backend is
    not extracted, and one whose record does not claim this job — so this seam never loads a
    backend that has not said it does the work.
    """
    lang = str(language or "").strip()
    # The axis and the capability are spelled as LITERALS, not module constants, so the
    # neutral-core scan in `test_backend_boundary` that inventories which capabilities are
    # dispatched on can see this seam. A constant here would make the one real dispatch of
    # `runner_render` invisible to the instrument that pins dispatches against declarations.
    reason = registry.missing_capability_reason("language", lang, "runner_render")
    if reason is not None:
        raise RunnerRenderUnavailable(reason)
    return registry.capability_module("language", lang, "runner_render")


def runner_render_refusal(language: Any) -> str | None:
    """`None` when `language` has a host runner renderer; else the registry's refusal clause.

    For the deterministic gates, which append a violation string rather than raising. The wording
    is `registry.missing_capability_reason`'s, unaltered: one owner for the sentence that names
    what would have had to be implemented.
    """
    return registry.missing_capability_reason(
        "language", str(language or "").strip(), "runner_render")


def render_runner(language: Any, ir: dict[str, Any], spec_id: str, harness_spec_id: str) -> str:
    """The complete text of the node's runner source, rendered from the IR alone.

    Deterministic and pure. Raises `RenderError` for an IR the backend cannot faithfully render,
    and `RunnerRenderUnavailable` when `language` declares no renderer.
    """
    return _module(language).render_runner(ir, spec_id, harness_spec_id)


def assert_harness_pin(
    language: Any,
    ir: dict[str, Any],
    spec_id: str,
    harness_spec_id: str,
    harness_signatures: Any,
    harness_source: str,
) -> None:
    """Fail-closed guard run BEFORE rendering: the certified harness the consumer will link
    against must still publish exactly the interface the backend's template was written for, in
    both its IR ``public_api.signatures`` and its generated model source. Drift raises
    `RenderError`, never a Generate content retry."""
    _module(language).assert_harness_pin(
        ir, spec_id, harness_spec_id, harness_signatures, harness_source)


def ir_content_violations(
    language: Any, ir: dict[str, Any], spec_id: str, harness_spec_id: str,
) -> list[str]:
    """The Compile-authored render preconditions, as violation messages (``[]`` when the IR
    renders, or when the only defect is a node-identity one — see `RenderError.identity`).

    The compile.static gate calls this so an IR defect routes back to compile.generate instead of
    surfacing only as a conductor-time fail_closed that kills the run.
    """
    return _module(language).ir_content_violations(ir, spec_id, harness_spec_id)


def checks_public_names(language: Any) -> tuple[str, ...]:
    """The fixed public names of the leaf-authored checks module for `language`.

    An ABI, not a convention: the host-rendered runner calls exactly these, so the neutral
    consumers that verify a leaf authored them (the codegen bundle contract, the checks-source
    validator) must read the same list the renderer emits against, not restate it.
    See `docs/workflow/CHECKS_MODULE_CONTRACT.md`.
    """
    return tuple(_module(language).CHECKS_PUBLIC_NAMES)
