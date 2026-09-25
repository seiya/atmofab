#!/usr/bin/env python3
"""The GPU class's `perf_facts` (issue #289, R4-b).

`ARCHITECTURE_PATTERN` is the grammar of `hardware.architecture` for a GPU profile: a device
compute capability as the device compiler spells its `-arch=` value (`sm_90`, `sm_90a`). It is
asked by the launch gate (`target_profile.target_profile_violations`), so a profile whose
architecture the build could never take is refused before anything runs. A vendor whose
architecture is spelled otherwise does not match, and is refused rather than passed through.
"""

from __future__ import annotations

import re

ARCHITECTURE_PATTERN = re.compile(r"sm_[0-9]+[a-z]?")
