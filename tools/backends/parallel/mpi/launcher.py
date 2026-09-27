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


#: The rank count the launch canary runs with: the smallest that tells one run of several
#: processes from several one-process runs.
LAUNCH_CANARY_RANKS = 2

#: A program each of whose processes prints the size of the run it belongs to. Built with the
#: backend's compiler wrapper and started under this launcher with `LAUNCH_CANARY_RANKS`, it
#: prints that count once per process when the launcher and the runtime the wrapper links
#: against are one installation — and `1` per process when they are not, which the exit code
#: does not say (measured 2026-09-27 in both directions between Intel MPI 2021.10 and Open MPI
#: 4.1.2: each process reports rank 0 of 1 and the launch exits 0).
LAUNCH_CANARY_SOURCE = (
    "program atmofab_mpi_launch_canary\n"
    "  use mpi_f08, only: MPI_Init, MPI_Finalize, MPI_Comm_size, MPI_COMM_WORLD\n"
    "  implicit none\n"
    "  integer :: n\n"
    "  call MPI_Init()\n"
    "  call MPI_Comm_size(MPI_COMM_WORLD, n)\n"
    "  print '(a,i0)', 'atmofab-mpi-size ', n\n"
    "  call MPI_Finalize()\n"
    "end program atmofab_mpi_launch_canary\n"
)

#: The file name the launch canary is built from.
LAUNCH_CANARY_FILENAME = "atmofab_mpi_launch_canary.f90"

#: The argv, after the compiler wrapper, that builds the launch canary (`{scratch}`,
#: `{source}` and `{exe}` are filled in by the caller).
LAUNCH_CANARY_BUILD_ARGV: tuple[str, ...] = ("-J", "{scratch}", "-o", "{exe}", "{source}")

_SIZE_LINE = "atmofab-mpi-size "


def launch_canary_problem(returncode: int, stdout: str) -> str | None:
    """None when the launch canary's output shows ONE run of `LAUNCH_CANARY_RANKS` processes;
    otherwise what it shows. Other output lines (a runtime's own messages) are ignored."""
    if returncode != 0:
        return f"the launch exited {returncode}"
    sizes = [line[len(_SIZE_LINE):].strip() for line in stdout.splitlines()
             if line.startswith(_SIZE_LINE)]
    expected = [str(LAUNCH_CANARY_RANKS)] * LAUNCH_CANARY_RANKS
    if sizes == expected:
        return None
    return (f"{LAUNCH_CANARY_RANKS} processes were started and they reported run sizes "
            f"{sizes or 'none'}, where one run reports {expected}")


def argv_prefix(ranks: int) -> tuple[str, ...]:
    """The argv a binary runs under to start `ranks` processes."""
    if isinstance(ranks, bool) or not isinstance(ranks, int) or ranks < 1:
        raise ValueError(f"ranks must be an integer >= 1, got {ranks!r}")
    return (EXECUTABLE, "-n", str(ranks))
