#!/usr/bin/env python3
"""The launch environment of a binary built for OpenMP (`execution_env`, issue #289).

`tools/host_execution.py` asks this through the registry and hands the result to
`run_program` as its `env`. Until R4-b PR-1 the build-runtime server set the same two variables
itself, and only when told `target.class == cpu` — so a thread count reached the runtime or not
depending on a hardware class the server had no business interpreting.
"""

from __future__ import annotations


def environment(threads_per_rank: int) -> dict[str, str]:
    """The variables the OpenMP runtime reads for a run of `threads_per_rank` threads.

    `OMP_NUM_THREADS` sets the team size and `OMP_THREAD_LIMIT` caps it, so a nested or
    `num_threads(...)` region cannot run wider than the profile says the run is.
    """
    if isinstance(threads_per_rank, bool) or not isinstance(threads_per_rank, int) \
            or threads_per_rank < 1:
        raise ValueError(f"threads_per_rank must be an integer >= 1, got {threads_per_rank!r}")
    count = str(threads_per_rank)
    return {"OMP_NUM_THREADS": count, "OMP_THREAD_LIMIT": count}
