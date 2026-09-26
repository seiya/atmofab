"""The CUDA parallel backend.

Submodules, by capability:

* `execution` — the `execution_env` capability: the process environment a binary built for this
  model is launched with (none of its own).
* `directives` — the `parallel_directives` capability: the `Generate.static` presence floor for a
  CUDA C++ model source built for a GPU.
"""

from tools.backends.parallel.cuda import (
    directives as directives,  # re-export
)
from tools.backends.parallel.cuda import (
    execution as execution,  # re-export
)
