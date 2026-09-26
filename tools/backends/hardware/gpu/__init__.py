#!/usr/bin/env python3
"""The GPU hardware backend.

Submodules, by capability:

* `perf` — the `perf_facts` capability: what a profile's `hardware.architecture` must be for this
  class. Imported below so a caller that reached this package through
  `registry.capability_module` finds it as an attribute rather than composing a module name.
* `execution` — the `execution` capability: the probe that identifies the device at the execution
  site (issue #293). Declaring it opens the registry half of the "can this run execute" gate; a
  run that reaches `Validate` still needs a site that `executes` the class
  (`tools/execution_sites.py`).
"""

from tools.backends.hardware.gpu import execution as execution  # noqa: F401  (re-export)
from tools.backends.hardware.gpu import perf as perf  # noqa: F401  (re-export)
