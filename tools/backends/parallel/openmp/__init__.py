#!/usr/bin/env python3
"""The OpenMP parallel backend.

Submodules, by capability. Each is imported below so a caller that reached this package through
`registry.capability_module` finds it as an attribute rather than composing a module name:

* `execution` — the `execution_env` capability: the process environment a binary built for this
  model is launched with.
* `directives` — the `parallel_directives` capability: the `Generate.static` presence floor (when
  it applies, how a directive is spelled in a node's language, when a lowering plan declines the
  model).
"""

from tools.backends.parallel.openmp import (
    directives as directives,  # noqa: F401  (re-export)
)
from tools.backends.parallel.openmp import (
    execution as execution,  # noqa: F401  (re-export)
)
