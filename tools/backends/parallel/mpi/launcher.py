"""The `launcher` capability of the MPI parallel backend (issue #316, R4-c).

The one place this repository spells the program that starts an MPI binary's ranks. The neutral
core holds what this returns as opaque tokens (`tools/host_execution.py`: an argv prefix, the
programs the executing machine needs, a probe whose first line is recorded).

`mpirun -n <ranks>` is the spelling the three implementations this backend has been read against
share (Intel MPI, Open MPI, MPICH): each accepts `-n` for the process count. The prefix is used
for every run of a target whose backend is this one, a one-rank target included, so that "the run
is under the launcher, the quality check's `make test` re-run is not" does not depend on the rank
count (`docs/backends/parallel/mpi/LAUNCHER.md`).

Stdlib only.
"""

from __future__ import annotations

#: The program that starts the ranks.
EXECUTABLE = "mpirun"

#: The programs the machine that executes the binary must resolve
#: (`host_execution.execution_executables`).
EXECUTABLES: tuple[str, ...] = (EXECUTABLE,)

#: The argv whose first line names the runtime the launcher belongs to. Recorded as
#: `trial_meta.json#environment.platform.parallel_runtime`; its value is not interpreted.
RUNTIME_PROBE: tuple[str, ...] = (EXECUTABLE, "--version")


def argv_prefix(ranks: int) -> tuple[str, ...]:
    """The argv a binary runs under to start `ranks` processes."""
    if isinstance(ranks, bool) or not isinstance(ranks, int) or ranks < 1:
        raise ValueError(f"ranks must be an integer >= 1, got {ranks!r}")
    return (EXECUTABLE, "-n", str(ranks))
