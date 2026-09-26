#!/usr/bin/env python3
"""The GPU class's `perf_facts` (issue #289, R4-b).

`ARCHITECTURE_PATTERN` is the grammar of `hardware.architecture` for a GPU profile: a device
compute capability as the device compiler spells its `-arch=` value (`sm_90`, `sm_90a`). It is
asked by the launch gate (`target_profile.target_profile_violations`), so a profile whose
architecture the build could never take is refused before anything runs. A vendor whose
architecture is spelled otherwise does not match, and is refused rather than passed through.

`parallelism` is what a run of this class states in its performance record (issue #289, R4-b
PR-6): the host-rendered runner writes it into `perf.json` through the harness's `write_perf`.
"""

from __future__ import annotations

import re

ARCHITECTURE_PATTERN = re.compile(r"sm_[0-9]+[a-z]?")


def parallelism(threads_per_rank: int) -> tuple[int, int, int]:
    """`(mpi_ranks, threads_per_rank, gpu_devices)` of one run of this class: one rank, the
    profile's threads, and the one device the rank drives."""
    return (1, int(threads_per_rank), 1)
