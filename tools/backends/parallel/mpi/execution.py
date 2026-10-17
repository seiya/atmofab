"""The launch environment of a binary built for MPI (`execution_env`, issues #316, #512).

The rank count is the launcher's argument (`launcher.argv_prefix`), not a variable the host sets,
and each rank runs one thread (`execution.threads_per_rank` is 1 on every profile,
`target_profile._shape_violations`). What the host does set is the process grid a profile
imposes on the ranks (`execution.process_grid`): the target's harness reads it at start from
`PROCESS_GRID_VARIABLE` and arranges the ranks in it (`harness_fortran_cpu_mpi` §3.3); with no
grid imposed the environment is empty, and the harness arranges the ranks as `(ranks, 1)`.
"""

from __future__ import annotations

#: The variable the harness reads the process grid from, `"p1,p2"`.
PROCESS_GRID_VARIABLE = "ATMOFAB_PROCESS_GRID"


def environment(threads_per_rank: int,
                process_grid: tuple[int, int] | None = None) -> dict[str, str]:
    """The overrides a run is launched with: the imposed grid, when one is given.
    `threads_per_rank` is validated like the other backends validate it, so a malformed profile
    value is refused the same way whichever model the profile names; so is a grid that is not
    two positive integers."""
    if isinstance(threads_per_rank, bool) or not isinstance(threads_per_rank, int) \
            or threads_per_rank < 1:
        raise ValueError(f"threads_per_rank must be an integer >= 1, got {threads_per_rank!r}")
    if process_grid is None:
        return {}
    if not (isinstance(process_grid, (tuple, list)) and len(process_grid) == 2
            and all(type(p) is int and p >= 1 for p in process_grid)):
        raise ValueError(f"process_grid must be two integers >= 1, got {process_grid!r}")
    return {PROCESS_GRID_VARIABLE: f"{process_grid[0]},{process_grid[1]}"}
