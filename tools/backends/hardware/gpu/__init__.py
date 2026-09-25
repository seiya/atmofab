#!/usr/bin/env python3
"""The GPU hardware backend.

Submodules, by capability:

* `perf` — the `perf_facts` capability: what a profile's `hardware.architecture` must be for this
  class. Imported below so a caller that reached this package through
  `registry.capability_module` finds it as an attribute rather than composing a module name.

The record declares no `execution`: this host cannot launch a binary on a GPU, so a profile
naming this class is refused for a run that reaches `Validate` (issue #289).
"""

from tools.backends.hardware.gpu import perf as perf  # noqa: F401  (re-export)
