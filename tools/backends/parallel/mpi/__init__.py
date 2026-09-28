"""The MPI parallel backend (issue #316, R4-c).

Submodules, by capability. Each is imported below so a caller that reached this package through
`registry.capability_module` finds it as an attribute rather than composing a module name:

* `execution` — the `execution_env` capability: the process environment a binary built for this
  model is launched with (none of its own).
* `launcher` — the `launcher` capability: the program that starts the binary's ranks, the argv
  it takes in front of the binary, and the probe that names the runtime it belongs to.
* `wrapper` — the `compiler_wrapper` capability: the program that stands in for the target's
  compiler at compile, link and the syntax stage, so the runtime's modules and libraries are
  found.
* `directives` — the `parallel_directives` capability: the `Generate.static` presence floor, a
  whole-node judgment for this model (`directives.PresenceFloor.node_violations`).
* `prompts` — the `prompt_fragments` capability: the text a pure `generate` template's
  `{{parallel:<name>}}` markers are replaced with.
"""

from tools.backends.parallel.mpi import (
    directives as directives,  # re-export
)
from tools.backends.parallel.mpi import (
    execution as execution,  # re-export
)
from tools.backends.parallel.mpi import (
    launcher as launcher,  # re-export
)
from tools.backends.parallel.mpi import (
    prompts as prompts,  # re-export
)
from tools.backends.parallel.mpi import (
    wrapper as wrapper,  # re-export
)
