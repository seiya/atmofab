"""The launch environment of a binary built for CUDA (`execution_env`, issues #289, #512).

What the host sets is the thread-block size a profile imposes on the run's kernel launches
(`execution.block_size`, and each value of `execution.sweep.block_size`): the target's harness
reads it at start from `BLOCK_SIZE_VARIABLE` (`harness_cpp_gpu` §3.3), and the host-rendered
runner hands it to the node's sources, which launch every kernel with it. With no block size
imposed the environment is empty and the harness uses its default. Which device a run uses is
the execution site's fact, not the parallel model's (`tools/execution_sites.py`, issue #293).
"""

from __future__ import annotations

#: The variable the harness reads the threads per block from, a decimal integer.
BLOCK_SIZE_VARIABLE = "ATMOFAB_CUDA_BLOCK"
#: The block sizes a run may be launched with: the powers of two from a warp to the largest
#: block every device of the class accepts. The harness stops a run handed any other value.
BLOCK_SIZES = frozenset({32, 64, 128, 256, 512, 1024})


def environment(threads_per_rank: int, block_size: int | None = None) -> dict[str, str]:
    """The overrides a run is launched with: the imposed block size, when one is given.
    `threads_per_rank` is validated like the OpenMP backend validates it, so a malformed profile
    value is refused the same way whichever model the profile names; a count other than one
    is refused, since the host side of a CUDA binary runs one thread and nothing here would make
    it run more (the launch gate asks this, `target_profile._thread_count_violations`). A block
    size outside `BLOCK_SIZES` is refused too (the launch gate asks it,
    `target_profile._block_size_violations`)."""
    if isinstance(threads_per_rank, bool) or not isinstance(threads_per_rank, int) \
            or threads_per_rank < 1:
        raise ValueError(f"threads_per_rank must be an integer >= 1, got {threads_per_rank!r}")
    if threads_per_rank != 1:
        raise ValueError(f"the host side runs one thread, got threads_per_rank={threads_per_rank}")
    if block_size is None:
        return {}
    if type(block_size) is not int or block_size not in BLOCK_SIZES:
        raise ValueError(f"block_size must be one of {sorted(BLOCK_SIZES)}, got {block_size!r}")
    return {BLOCK_SIZE_VARIABLE: str(block_size)}
