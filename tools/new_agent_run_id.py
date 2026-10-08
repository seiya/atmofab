#!/usr/bin/env python3
"""Mint a fresh UUID4 for use as `agent_run_id`.

Canonical UUID source for a child `agent_run_id` that must exist before
`record-launch`. The conductor calls it as a subprocess, and so can an operator
recovering by hand. Kept as a minimal standalone script (only stdlib `uuid`) so
an unrelated import or syntax break in `tools/orchestration_runtime.py` cannot
block minting.
"""

from __future__ import annotations

import sys
import uuid


def main() -> int:
    print(str(uuid.uuid4()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
