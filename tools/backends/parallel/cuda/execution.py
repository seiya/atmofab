"""The launch environment of a binary built for CUDA (`execution_env`, issue #289, R4-b PR-4).

EMPTY, and that is an answer (`registry.CAPABILITIES["execution_env"]`): a CUDA binary reads no
variable the host has to set for a run of the profile's shape. Which device a run uses is the
execution site's fact, not the parallel model's (issue #289 §9 leaves it to remote execution).
"""

from __future__ import annotations


def environment(threads_per_rank: int) -> dict[str, str]:
    """No overrides. `threads_per_rank` is validated like the OpenMP backend validates it, so a
    malformed profile value is refused the same way whichever model the profile names."""
    if isinstance(threads_per_rank, bool) or not isinstance(threads_per_rank, int) \
            or threads_per_rank < 1:
        raise ValueError(f"threads_per_rank must be an integer >= 1, got {threads_per_rank!r}")
    return {}
