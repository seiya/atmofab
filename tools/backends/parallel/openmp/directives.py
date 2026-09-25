#!/usr/bin/env python3
"""The OpenMP presence floor of `Generate.static` (issue #22; moved here by issue #289, R4-b PR-3).

The `parallel_directives` capability: when the floor applies to a node built for this parallel
model (`presence_floor`), how a source states an OpenMP directive in the node's language, and when
a bundle's `target_lowering_plan` declines OpenMP (`lowering_plan_declines`). The loops a directive
would parallelize are counted by the LANGUAGE backend (`source_reading.counted_loops`); the
validator (`validate_pipeline_semantics._validate_parallel_presence_floor`) composes the two and
decides which node kinds the floor is asked of.

Moved unchanged out of `tools/validate_pipeline_semantics.py`, where the floor fired on
`cpu ∧ openmp ∧ fortran` spelled inline.

Stdlib only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# `[ \t\f]` is gfortran's blank set: a form feed is a blank it accepts before a directive
# sentinel (verified against the compiler). The language backend's loop patterns use the same set.
_BLANK = r"[ \t\f]"

# The OpenMP sentinel of free-form Fortran, anchored at a PHYSICAL line start. A bare `omp` is a
# substring of `component` / `compute` / `compile`, so `!$` is what makes it a directive; and free
# form requires the sentinel to be preceded by blanks only, so the anchor IS the language rule. It
# is what keeps a doc comment reading "the `!$omp parallel do` directives would go here (not
# added)" — a shape a real generated source contains — from satisfying the floor, and likewise a
# commented-out `!!$omp`. Why a line-start anchor is enough, with no literal or continuation
# state, is the language backend's argument (`tools/backends/language/fortran/source.py`, above
# its loop patterns).
_OMP_DIRECTIVE_RE = re.compile(rf"^{_BLANK}*!\$omp\b", re.IGNORECASE | re.MULTILINE)

# Values of a parallelization model that mean "no parallelism here". Read generously — this
# direction only ever fails the floor OPEN.
_NO_PARALLELISM_VALUES = frozenset({"none", "off", "serial", "sequential", "false", "disabled"})

# Inside `target_lowering_plan.parallelization` (an object, `codegen_bundle`'s envelope), the
# members that name the execution MODEL. `model` is the one the producer's template names; the other
# three are the spellings the Compile-authored knob layer used to carry the model under (until R4-a
# PR-3, issue #284), kept so a producer reusing them states a claim rather than dodging one. The
# sibling members are scope / schedule / granularity prose (`apply_to`, `schedule`, `loops`, …) and
# must not be read as a model, or a correctly serial `{model: none, apply_to: parallelizable_loops}`
# licenses the floor to reject its own source.
_LOWERING_PARALLELIZATION_MODEL_KEYS = frozenset({"model", "method", "scheme", "kind"})


@dataclass(frozen=True)
class PresenceFloor:
    """What the floor asks of one (language, hardware class) pair."""

    #: A source that matches carries at least one directive of this model.
    directive: re.Pattern[str]
    #: The finding, given the model file and the number of counted loops it holds.
    remedy: Any


def _fortran_cpu_remedy(model_file: Path, counted: int) -> str:
    return (
        f"{model_file}: the target profile resolves to OpenMP on CPU "
        "(hardware.class=cpu, parallel.backend=openmp, toolchain.language=fortran) and the "
        "bundle's target_lowering_plan.parallelization does not decline OpenMP, but this "
        f"generated model source has {counted} counted `do` loop(s) and not one `!$omp` "
        "directive — add `!$omp parallel do` to the parallelizable loops (a `do concurrent` "
        "loop already counts as parallel). Only when a loop genuinely cannot be "
        "parallelized (a carried dependence) is `\"model\": \"none\"` in the plan the "
        "answer, with the reason stated; the independent reviewer holds that declaration to "
        "the loops, so never force a directive you believe is wrong and never decline one "
        "a loop can take"
    )


#: The (language, hardware class) pairs the floor is stated for. A pair absent here has no floor:
#: the directive syntax of another language, or OpenMP offload to another class, is not knowledge
#: this module has.
_FLOORS: dict[tuple[str, str], PresenceFloor] = {
    ("fortran", "cpu"): PresenceFloor(directive=_OMP_DIRECTIVE_RE, remedy=_fortran_cpu_remedy),
}


def presence_floor(*, language: str, hardware_class: str) -> PresenceFloor | None:
    """The floor for a node built for OpenMP in `language` on `hardware_class`, or None when this
    model states none for the pair (the floor then does not apply — fail-open, as it always did
    for a pair other than Fortran on CPU)."""
    return _FLOORS.get((language, hardware_class))


def lowering_plan_declines(plan: Any) -> bool:
    """True when a bundle's ``target_lowering_plan`` EXPLICITLY declines OpenMP: a model-bearing
    member of its ``parallelization`` object names no parallelism (``none`` / ``serial`` / …) or
    a model other than OpenMP, and no model-bearing member names OpenMP.

    This is the floor's one exemption, and it has to be a DECLARATION. On an OpenMP target the
    target's backend is the default model: a plan with no ``parallelization`` object, with no
    model member, or with a value that is not a string declines nothing, so the floor applies.
    Until R4-a PR-3 (issue #284) the exemption was the IR's Compile-authored knob layer, which
    the Generate leaf could not edit; the knob layer is now the SAME leaf's plan, so the floor
    reading "no claim" as an exemption let the producer switch its own floor off by omission —
    the round-1 finding this shape closes. What stays is the explicit ``"model": "none"`` for a
    model source whose every counted loop carries a dependence — a construct the corpus does not
    contain (measured in R4-a PR-3's round 3: the only passing counted-loop, directive-free
    physics sources are three July 2026 ones whose IRs claimed OpenMP and which predate the
    floor, and the five real ``none`` plans belong to the harness and two whole-array nodes);
    whether that declaration is honest is ``Generate.verify`` G6's judgment, told in its
    template that a ``none`` over plainly parallelizable loops, or with a reason the loops
    contradict, is itself the finding.

    OpenMP specifically: a plan naming ``mpi`` or ``cuda_streams`` declines OpenMP rather than
    being told to add a directive — a demand that would contradict its own plan. Substring, so
    ``openmp+simd`` / ``openmp_tasks`` name OpenMP."""
    if not isinstance(plan, dict):
        return False
    par = plan.get("parallelization")
    if not isinstance(par, dict):
        return False
    declined = False
    for key, value in par.items():
        if str(key).strip().lower() not in _LOWERING_PARALLELIZATION_MODEL_KEYS:
            continue
        if not isinstance(value, str) or not value.strip():
            continue
        token = value.strip().lower()
        if "openmp" in token and token not in _NO_PARALLELISM_VALUES:
            return False
        declined = True
    return declined
