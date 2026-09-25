#!/usr/bin/env python3
"""The neutral seam between the host and whatever knows how a target's binary is launched.

`Validate.execute` runs the certified binary (`workflow_conductor._execute_inproc`). HOW it is
launched — the process environment a parallel model's runtime reads, a launcher in front of the
binary, the machine it runs on — is backend knowledge (`docs/BACKEND_BOUNDARY.md`): the
environment belongs to the `parallel` axis, and whether this host can run the binary at all
belongs to the `hardware` axis. The DECISION to launch, and the routing to the values that know
how, are neutral and live here (issue #289, R4-b PR-1).

Until then the build-runtime server decided it: `run_program` took the hardware class and a
thread count and set the OpenMP variables itself, for `cpu` alone, and ran anything else with
no environment and no word — a `gpu` profile reached a CPU run silently. The server now refuses
those arguments and runs the `env` it is handed; this module is what composes that `env`.

`LaunchShape.site` names where the binary runs. It is `local` for every shape this module can
produce today: a class this host can run is one whose record declares `execution`, and the only
such record is the in-process one. A remote execution site is a separate feature that declares
`execution` on another class and makes `site` something other than `local` (issue #289 §9);
until it lands, a class with no `execution` is refused here, and before that at launch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tools.backends import registry

#: The site this module launches every shape at: in-process, on this host.
LOCAL_SITE = "local"


class LaunchUnavailable(RuntimeError):
    """The target names a value this host has no launch answer for.

    The launch gate (`target_profile.target_profile_violations`) refuses the same profile before
    anything runs, for any run that reaches `Validate`, so reaching this raise means the gate
    and this seam disagreed — a host defect, not something a leaf could repair.
    """


@dataclass(frozen=True)
class LaunchShape:
    """How one binary is launched: `argv_prefix` goes in front of the binary's own argv, `env`
    is handed to `run_program` as its `env` — OVERRIDES, which the server merges over the host
    process's own environment, so a variable this does not set is inherited — and `site` names
    where it runs."""

    argv_prefix: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    site: str = LOCAL_SITE

    def command(self, argv: list[str]) -> list[str]:
        """The full command line for a binary invoked as `argv`."""
        return [*self.argv_prefix, *argv]

    def record(self) -> dict[str, Any]:
        """The provenance record `trial_meta.json#environment.launch` carries."""
        return {"argv_prefix": list(self.argv_prefix), "env": dict(sorted(self.env.items()))}


def _execution_env(parallel_backend: str, threads_per_rank: int) -> dict[str, str]:
    """The environment a binary built for `parallel_backend` is launched with.

    A value whose record carries `execution_env` in its PACKAGE is asked through
    `registry.capability_module`; one that carries it in the neutral core is the serial case,
    whose core implementation IS "no environment of its own" — that is what the declaration on
    such a record asserts (`tools/backends/registry.py`). A value that declares neither is
    refused: running it with whatever the host process carries is the silent answer this seam
    replaced.
    """
    reason = registry.missing_capability_reason("parallel", parallel_backend, "execution_env")
    if reason is not None:
        raise LaunchUnavailable(f"parallel.backend: {reason}")
    record = registry.get("parallel", parallel_backend)
    if "execution_env" not in record.backend_provides:
        return {}
    module = registry.capability_module("parallel", parallel_backend, "execution_env")
    env = module.environment(threads_per_rank)
    return {str(k): str(v) for k, v in env.items()}


def launch_shape(profile: Any) -> LaunchShape:
    """The launch shape of a binary built for `profile` (a `target_profile.TargetProfile`).

    Refuses (`LaunchUnavailable`) a hardware class whose record does not declare `execution`,
    and a parallel backend whose record does not declare `execution_env`, with the registry's own
    wording.
    """
    reason = registry.missing_capability_reason(
        "hardware", profile.hardware_class, "execution")
    if reason is not None:
        raise LaunchUnavailable(f"hardware.class: {reason}")
    env = _execution_env(profile.parallel_backend, profile.threads_per_rank)
    return LaunchShape(argv_prefix=(), env=env, site=LOCAL_SITE)
