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

`LaunchShape.site` names where the binary runs: the execution site the operator's `sites.yaml`
maps the target to (`tools/execution_sites.py`, issue #293), `local` when it maps it nowhere.
"Can this run execute a binary of this class" has two halves and this seam asks both, as the
backstop of the launch gate: the class's record must declare `execution` (the registry half —
there is code), and the site must list the class in its `executes` (the machine half — there is
a machine). `LaunchShape.platform_probe` is the argv a class's package names to identify its
device at the site, recorded as `platform.gpu`.

A parallel backend that declares `device_trace` (issue #307) runs the binary under a device trace:
its profiling argv is `LaunchShape.argv_prefix`, and `LaunchShape.trace` is the command that
writes the trace's per-kernel summary in the run's working directory and the file it writes, which
`Validate.execute` runs after the binary and promotes as `KERNEL_TRACE_ARTIFACT`. The spellings are
the backend's; this module holds them as opaque tokens.
"""

from __future__ import annotations

import platform
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tools.backends import registry

#: The site that runs a binary in-process, on this host; the one a target maps to when the
#: operator's `sites.yaml` maps it nowhere.
LOCAL_SITE = "local"
#: Seconds the local device probe may take; one that hangs records `null`.
PROBE_TIMEOUT_SEC = 60
#: The stem a device trace is written under, relative to the run's cwd (issue #307).
KERNEL_TRACE_STEM = "kernel_trace"
#: The node-dir name the conductor promotes a device trace's per-kernel summary to.
KERNEL_TRACE_ARTIFACT = "kernel_trace.csv"


class LaunchUnavailable(RuntimeError):
    """The target names a value this host has no launch answer for, or the site it runs at does
    not execute its hardware class.

    The launch gate (`target_profile.target_profile_violations` for the registry half,
    `execution_sites.site_violations` for the site half) refuses the same run before anything
    runs, for any run that reaches `Validate`, so reaching this raise means the gate and this
    seam disagreed — a host defect, not something a leaf could repair.
    """


@dataclass(frozen=True)
class TraceShape:
    """The second half of a traced launch: `summary_argv` runs in the run's cwd after the binary
    and before the quality check, and writes `summary_file` there (relative to that cwd)."""

    summary_argv: tuple[str, ...]
    summary_file: str


@dataclass(frozen=True)
class LaunchShape:
    """How one binary is launched: `argv_prefix` goes in front of the binary's own argv, `env`
    is a set of OVERRIDES — at `local` handed to `run_program`, which merges them over the host
    process's own environment, and at a remote site set by the job script over the site's
    non-interactive login environment (`tools/remote_execution.py`), so a variable this does not
    set is inherited from wherever the binary runs — and `site` names where it runs. `platform_probe` is the argv that identifies the class's device where it
    runs, None for a class that names none. `trace` is the summary half of a device trace when
    the parallel backend declares `device_trace` (its profiling half is `argv_prefix`), None
    otherwise."""

    argv_prefix: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    site: str = LOCAL_SITE
    platform_probe: tuple[str, ...] | None = None
    trace: TraceShape | None = None

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


def _platform_probe(hardware_class: str) -> tuple[str, ...] | None:
    """The device probe of `hardware_class`: its package's `PLATFORM_PROBE` when the record
    declares `execution` in a package, None when the neutral core implements it (a class whose
    device is the CPU the platform record already names)."""
    if "execution" not in registry.get("hardware", hardware_class).backend_provides:
        return None
    module = registry.capability_module("hardware", hardware_class, "execution")
    return tuple(str(a) for a in module.PLATFORM_PROBE)


def _device_trace(parallel_backend: str) -> tuple[tuple[str, ...], TraceShape | None]:
    """The argv prefix and summary half of the device trace a binary built for
    `parallel_backend` runs under: its package's `device_trace` answer, written under
    `KERNEL_TRACE_STEM`, when its record declares the capability; no prefix and no trace
    otherwise (issue #307)."""
    if "device_trace" not in registry.get("parallel", parallel_backend).backend_provides:
        return (), None
    module = registry.capability_module("parallel", parallel_backend, "device_trace")
    prefix = tuple(str(a) for a in module.profile_argv_prefix(KERNEL_TRACE_STEM))
    return prefix, TraceShape(
        summary_argv=tuple(str(a) for a in module.summary_argv(KERNEL_TRACE_STEM)),
        summary_file=str(module.summary_file(KERNEL_TRACE_STEM)))


def execution_executables(parallel_backend: str) -> tuple[str, ...]:
    """The programs the machine that executes a binary built for `parallel_backend` needs
    beyond the binary itself: its device trace's (`device_trace`), none for a value that
    declares no trace."""
    if "device_trace" not in registry.get("parallel", parallel_backend).backend_provides:
        return ()
    module = registry.capability_module("parallel", parallel_backend, "device_trace")
    return tuple(str(e) for e in module.EXECUTABLES)


def launch_shape(profile: Any, site: Any = None) -> LaunchShape:
    """The launch shape of a binary built for `profile` (a `target_profile.TargetProfile`) at
    `site` (an `execution_sites.Site`; None is the local site with its default `executes`, the
    configuration with no `sites.yaml`).

    The argv prefix and `trace` are the parallel backend's device trace when it declares one
    (`_device_trace`), none otherwise.

    Refuses (`LaunchUnavailable`) a hardware class whose record does not declare `execution`,
    a site whose `executes` does not list the class, and a parallel backend whose record does not
    declare `execution_env`, with the registry's own wording for the first and the third.
    """
    reason = registry.missing_capability_reason(
        "hardware", profile.hardware_class, "execution")
    if reason is not None:
        raise LaunchUnavailable(f"hardware.class: {reason}")
    if site is None:
        # Imported here: `execution_sites` imports `LOCAL_SITE` from this module.
        from tools.execution_sites import LOCAL_DEFAULT_EXECUTES

        site_id, executes = LOCAL_SITE, LOCAL_DEFAULT_EXECUTES
    else:
        site_id, executes = site.site_id, tuple(site.executes)
    if profile.hardware_class not in executes:
        raise LaunchUnavailable(
            f"hardware.class: {profile.hardware_class} is not executed at site {site_id}, "
            f"which executes {', '.join(executes)}")
    env = _execution_env(profile.parallel_backend, profile.threads_per_rank)
    argv_prefix, trace = _device_trace(profile.parallel_backend)
    return LaunchShape(argv_prefix=argv_prefix, env=env, site=site_id,
                       platform_probe=_platform_probe(profile.hardware_class), trace=trace)


def perf_parallelism(target: dict[str, Any]) -> tuple[int, int, int]:
    """`(mpi_ranks, threads_per_rank, gpu_devices)` a run of `target` (a target profile DOCUMENT,
    `TargetProfile.doc`) states in its performance record — what a host-rendered runner passes the
    harness's `write_perf`. A hardware class that declares `perf_facts` answers it
    (`parallelism`); one that declares none is one rank of the profile's threads on no device,
    which is what the in-process CPU launch runs. Pure; raises `KeyError` / `ValueError` for a
    document without the two fields, which the profile loader requires."""
    hardware_class = str(target["hardware"]["class"])
    threads = int(target["execution"]["threads_per_rank"])
    if "perf_facts" in registry.get("hardware", hardware_class).backend_provides:
        facts = registry.capability_module("hardware", hardware_class, "perf_facts")
        ranks, per_rank, devices = facts.parallelism(threads)
        return (int(ranks), int(per_rank), int(devices))
    return (1, threads, 0)


def local_platform_record(probe: tuple[str, ...] | None = None) -> dict[str, str | None]:
    """The machine a local Validate run executed on, for `trial_meta.json#environment.platform`:
    `platform.machine()`, `platform.node()`, the CPU model name from `/proc/cpuinfo`, and the
    first line `probe` prints when it exits 0 (`gpu`). Each fact that cannot be read is `None` —
    a record, never a refusal. The remote executor builds the same shape from the site's own
    answers (`tools/remote_execution.py`)."""
    cpu_model: str | None = None
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8",
                                                    errors="replace").splitlines():
            if line.lower().startswith("model name"):
                cpu_model = line.split(":", 1)[1].strip() or None
                break
    except OSError:
        cpu_model = None
    gpu: str | None = None
    if probe:
        try:
            proc = subprocess.run(list(probe), text=True, capture_output=True, check=False,
                                  timeout=PROBE_TIMEOUT_SEC, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            proc = None
        if proc is not None and proc.returncode == 0:
            gpu = (proc.stdout.splitlines() or [""])[0].strip() or None
    return {"machine": platform.machine(), "node": platform.node(), "cpu_model": cpu_model,
            "gpu": gpu}
