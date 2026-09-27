"""The launch environment of a binary built for MPI (`execution_env`, issue #316).

EMPTY, and that is an answer (`registry.CAPABILITIES["execution_env"]`): the rank count is the
launcher's argument (`launcher.argv_prefix`), not a variable the host sets, and each rank runs one
thread (`execution.threads_per_rank` is 1 on every profile, `target_profile._shape_violations`).
"""

from __future__ import annotations


def environment(threads_per_rank: int) -> dict[str, str]:
    """No overrides. `threads_per_rank` is validated like the other backends validate it, so a
    malformed profile value is refused the same way whichever model the profile names."""
    if isinstance(threads_per_rank, bool) or not isinstance(threads_per_rank, int) \
            or threads_per_rank < 1:
        raise ValueError(f"threads_per_rank must be an integer >= 1, got {threads_per_rank!r}")
    return {}
