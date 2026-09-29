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

A parallel backend that declares `launcher` (issue #316) starts the binary's ranks: its
`argv_prefix(ranks)`, with the profile's `execution.ranks`, is the OUTER part of
`LaunchShape.argv_prefix` (a device trace's prefix, when a backend declares both, goes between
it and the binary), and its `RUNTIME_PROBE` is `LaunchShape.runtime_probe`, whose first line is
recorded as `platform.parallel_runtime`. A remote site does not place a launcher's ranks yet (a
batch site runs the job as one task; a direct site is issue #337), so such a target runs at the
local site only: the launch gate refuses another site (`execution_sites.site_violations`) and
`launch_shape` refuses it as the backstop.
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
    runs — the site half for any run that reaches `Build`, whose remote build asks this seam too
    (issue #333), the registry half for one that reaches `Validate` — so reaching this raise
    means the gate and this seam disagreed — a host defect, not something a leaf could repair.
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
    non-interactive login environment after the site's `setup` lines (`tools/remote_execution.py`), so a variable this does not
    set is inherited from wherever the binary runs — and `site` names where it runs. `platform_probe` is the argv that identifies the class's device where it
    runs, None for a class that names none. `trace` is the summary half of a device trace when
    the parallel backend declares `device_trace` (its profiling half is `argv_prefix`), None
    otherwise. `runtime_probe` is the argv whose first line names the parallel model's runtime
    when the backend declares `launcher`, None otherwise."""

    argv_prefix: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    site: str = LOCAL_SITE
    platform_probe: tuple[str, ...] | None = None
    trace: TraceShape | None = None
    runtime_probe: tuple[str, ...] | None = None

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


def declares_launcher(parallel_backend: str) -> bool:
    """Whether a binary built for `parallel_backend` runs under a launcher (`launcher`). A
    value with no record answers False, as `registry.provides` does; the gate that refuses it is
    the profile's (`target_profile.target_profile_violations`)."""
    return registry.provides("parallel", parallel_backend, "launcher") and \
        "launcher" in registry.get("parallel", parallel_backend).backend_provides


def _launcher(parallel_backend: str, ranks: int) -> tuple[str, ...]:
    """The launcher prefix that starts `ranks` processes of a binary built for
    `parallel_backend`, none for a value that declares no launcher (issue #316)."""
    if not declares_launcher(parallel_backend):
        return ()
    module = registry.capability_module("parallel", parallel_backend, "launcher")
    return tuple(str(a) for a in module.argv_prefix(ranks))


def launch_argv_prefix(parallel_backend: str, ranks: int) -> tuple[str, ...]:
    """The argv prefix a binary built for `parallel_backend` runs under with `ranks` ranks
    (`launch_shape` composes the same two parts): the launcher's, outermost, then the device
    trace's; none
    for a value that declares neither. The post-execute gate admits a recorded prefix only when
    it is this one."""
    return (*_launcher(parallel_backend, ranks), *_device_trace(parallel_backend)[0])


def execution_executables(parallel_backend: str) -> tuple[str, ...]:
    """The programs the machine that executes a binary built for `parallel_backend` needs
    beyond the binary itself: its launcher's (`launcher`) and its device trace's
    (`device_trace`), none for a value that declares neither."""
    found: list[str] = []
    backend_provides = registry.get("parallel", parallel_backend).backend_provides
    for capability in ("launcher", "device_trace"):
        if capability in backend_provides:
            module = registry.capability_module("parallel", parallel_backend, capability)
            found += [str(e) for e in module.EXECUTABLES if str(e) not in found]
    return tuple(found)


def _runtime_probe(parallel_backend: str) -> tuple[str, ...] | None:
    """The launcher's runtime probe, None for a value that declares no launcher."""
    if not declares_launcher(parallel_backend):
        return None
    module = registry.capability_module("parallel", parallel_backend, "launcher")
    return tuple(str(a) for a in module.RUNTIME_PROBE)


def launch_shape(profile: Any, site: Any = None) -> LaunchShape:
    """The launch shape of a binary built for `profile` (a `target_profile.TargetProfile`) at
    `site` (an `execution_sites.Site`; None is the local site with its default `executes`, the
    configuration with no `sites.yaml`).

    The argv prefix is the parallel backend's launcher with the profile's `ranks` when it
    declares one, then its device trace's when it declares one (`launch_argv_prefix`); `trace` is
    the trace's summary half.

    Refuses (`LaunchUnavailable`) a hardware class whose record does not declare `execution`,
    a site whose `executes` does not list the class, a parallel backend whose record does not
    declare `execution_env` (with the registry's own wording for the first and the third), a
    launcher backend at a site other than `local`, and more ranks than one for a backend with no
    launcher.
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
    backend = profile.parallel_backend
    if declares_launcher(backend) and site_id != LOCAL_SITE:
        raise LaunchUnavailable(
            f"parallel.backend: {backend} runs its binary under a launcher, whose ranks a remote "
            f"site does not place yet; site {site_id} is not {LOCAL_SITE}")
    if profile.ranks != 1 and not declares_launcher(backend):
        raise LaunchUnavailable(
            f"execution.ranks: {profile.ranks} ranks need a launcher, and parallel backend "
            f"{backend} declares none")
    env = _execution_env(backend, profile.threads_per_rank)
    trace_prefix, trace = _device_trace(backend)
    return LaunchShape(argv_prefix=(*_launcher(backend, profile.ranks), *trace_prefix), env=env,
                       site=site_id, platform_probe=_platform_probe(profile.hardware_class),
                       trace=trace, runtime_probe=_runtime_probe(backend))


def perf_parallelism(target: dict[str, Any]) -> tuple[int, int, int]:
    """`(mpi_ranks, threads_per_rank, gpu_devices)` a run of `target` (a target profile DOCUMENT,
    `TargetProfile.doc`) states in its performance record — what a host-rendered runner passes the
    harness's `write_perf`. The rank count is the profile's `execution.ranks` (1 when it states
    none, issue #316): a CONFIGURED value, which a runner of a launcher target must not pass —
    the post-execute gate compares the recorded count with this same value, so it has to be the
    count the harness observes at run time (`validate_pipeline_semantics.
    _validate_launched_ranks`). The CUDA C++ runner passes it for a backend with no launcher,
    whose runs are one process. A hardware class that declares `perf_facts` answers the per-rank half
    (`parallelism`, whose rank member this replaces); one that declares none is the profile's
    threads on no device, which is what the in-process CPU launch runs. Pure; raises
    `KeyError` / `ValueError` for a document without the fields the profile loader requires."""
    hardware_class = str(target["hardware"]["class"])
    threads = int(target["execution"]["threads_per_rank"])
    ranks = int(target["execution"].get("ranks", 1))
    if "perf_facts" in registry.get("hardware", hardware_class).backend_provides:
        facts = registry.capability_module("hardware", hardware_class, "perf_facts")
        _one_rank, per_rank, devices = facts.parallelism(threads)
        return (ranks, int(per_rank), int(devices))
    return (ranks, threads, 0)


def probe_first_line(probe: tuple[str, ...] | None) -> str | None:
    """The first line `probe` prints when it exits 0; None when it names nothing, cannot run,
    or exits non-zero."""
    if not probe:
        return None
    try:
        proc = subprocess.run(list(probe), text=True, capture_output=True, check=False,
                              timeout=PROBE_TIMEOUT_SEC, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout.splitlines() or [""])[0].strip() or None


def local_platform_record(probe: tuple[str, ...] | None = None, *,
                          runtime_probe: tuple[str, ...] | None = None
                          ) -> dict[str, str | None]:
    """The machine a local Validate run executed on, for `trial_meta.json#environment.platform`:
    `platform.machine()`, `platform.node()`, the CPU model name from `/proc/cpuinfo`, and the
    first line `probe` prints when it exits 0 (`gpu`). A launch with a `runtime_probe` (a
    launcher's, issue #316) adds `parallel_runtime`, its first line the same way. Each fact that
    cannot be read is `None` — a record, never a refusal. The remote executor builds the same
    shape from the site's own answers (`tools/remote_execution.py`); a launcher target never runs
    there."""
    cpu_model: str | None = None
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8",
                                                    errors="replace").splitlines():
            if line.lower().startswith("model name"):
                cpu_model = line.split(":", 1)[1].strip() or None
                break
    except OSError:
        cpu_model = None
    record: dict[str, str | None] = {
        "machine": platform.machine(), "node": platform.node(), "cpu_model": cpu_model,
        "gpu": probe_first_line(probe)}
    if runtime_probe:
        record["parallel_runtime"] = probe_first_line(runtime_probe)
    return record
