#!/usr/bin/env python3
"""The Slurm scheduler backend.

Submodules, by capability:

* `submit` — the `job_submit` capability: the argv prefix that runs a job script as one Slurm job
  in the foreground of the site's ssh call, the program it needs at the site, and the variable a
  job reads its own id from (issue #293). Imported below so a caller that reached this package
  through `registry.capability_module` finds it as an attribute rather than composing a module
  name.
"""

from tools.backends.scheduler.slurm import submit as submit  # noqa: F401  (re-export)
