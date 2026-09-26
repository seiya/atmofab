"""The CUDA presence floor of `Generate.static` (issue #289, R4-b PR-4).

The `parallel_directives` capability, stated like the OpenMP backend's
(`tools/backends/parallel/openmp/directives.py`): a `component` / `problem` model source built for
CUDA on a GPU that has counted loops (the language backend's `source_reading.counted_loops`) must
carry at least one kernel — a `__global__` function or a `<<<...>>>` launch — unless the bundle's
`target_lowering_plan.parallelization` explicitly declines CUDA. It is a PRESENCE floor only;
whether the kernels cover the loops the plan names is `Generate.verify` G6's judgment. The
validator decides which node kinds it is asked of (never an `infrastructure` node).

Stdlib only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# A kernel definition or a launch, over the source's CODE: the validator hands `search` the raw
# text, so a comment naming `__global__` would satisfy a plain pattern. The pattern is therefore
# applied by `KernelPattern.search` to the masked text (comments and literal contents blanked).
_KERNEL_RE = re.compile(r"\b__global__\b|<<<")

_NO_PARALLELISM_VALUES = frozenset({"none", "off", "serial", "sequential", "false", "disabled"})
_LOWERING_PARALLELIZATION_MODEL_KEYS = frozenset({"model", "method", "scheme", "kind"})


class KernelPattern:
    """The floor's `directive`: `search(text)` is truthy when the CODE of `text` defines or
    launches a kernel (the language backend's masking decides what is code)."""

    def search(self, text: str) -> re.Match[str] | None:
        # What counts as code is the language's reading, asked of the registry rather than
        # imported from the language backend's package.
        from tools.backends import registry
        reader = registry.capability_module("language", "cuda_cpp", "source_reading")
        return _KERNEL_RE.search(reader.code_view(text))


@dataclass(frozen=True)
class PresenceFloor:
    """What the floor asks of one (language, hardware class) pair."""

    directive: Any
    remedy: Any


def _cuda_cpp_gpu_remedy(model_file: Path, counted: int) -> str:
    return (
        f"{model_file}: the target profile resolves to CUDA on a GPU (hardware.class=gpu, "
        "parallel.backend=cuda, toolchain.language=cuda_cpp) and the bundle's "
        "target_lowering_plan.parallelization does not decline CUDA, but this generated model "
        f"source has {counted} counted `for` loop(s) and not one kernel — move the "
        "parallelizable loops into a `__global__` kernel launched with `<<<grid, block>>>`. Only "
        "when no loop can run on the device is `\"model\": \"none\"` in the plan the answer, with "
        "the reason stated; the independent reviewer holds that declaration to the loops")


_FLOORS: dict[tuple[str, str], PresenceFloor] = {
    ("cuda_cpp", "gpu"): PresenceFloor(directive=KernelPattern(), remedy=_cuda_cpp_gpu_remedy),
}


def presence_floor(*, language: str, hardware_class: str) -> PresenceFloor | None:
    """The floor for a node built for CUDA in `language` on `hardware_class`, or None when this
    model states none for the pair."""
    return _FLOORS.get((language, hardware_class))


def lowering_plan_declines(plan: Any) -> bool:
    """True when a bundle's ``target_lowering_plan`` EXPLICITLY declines CUDA: a model-bearing
    member of its ``parallelization`` object names no parallelism or a model other than CUDA, and
    none names CUDA. An absent plan, object or model declines nothing (the target's backend is
    the default), as for OpenMP."""
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
        if "cuda" in token and token not in _NO_PARALLELISM_VALUES:
            return False
        declined = True
    return declined
