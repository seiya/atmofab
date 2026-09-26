#!/usr/bin/env python3
"""The GPU class's `execution` (issue #293).

`PLATFORM_PROBE` is the argv whose first output line identifies the device a binary of this class
ran on. `Validate.execute` runs it where the binary runs — at a remote site in the job, before the
first command; at the local site in-process, after the commands — and records that line as `trial_meta.json#environment.platform.gpu`: a record, not a
refusal, so a probe that fails or prints nothing records `null`. The vendor's management tool
answers it, so a site whose device another vendor makes records `null` until a probe for it is
written here.
"""

from __future__ import annotations

PLATFORM_PROBE: tuple[str, ...] = (
    "nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader")
