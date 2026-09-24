#!/usr/bin/env python3
"""The OpenMP parallel backend.

Submodules, by capability:

* `execution` — the `execution_env` capability: the process environment a binary built for this
  model is launched with. Imported below so a caller that reached this package through
  `registry.capability_module` finds it as an attribute rather than composing a module name.

The directive knowledge (`parallel_directives`) is still inlined in the neutral core; see
`docs/BACKEND_BOUNDARY.md` and the migration ledger in `TODO.md`.
"""

from tools.backends.parallel.openmp import (
    execution as execution,  # noqa: F401  (re-export)
)
