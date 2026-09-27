"""The CUDA C++ language backend (issue #289, R4-b PR-4).

Submodules, by capability. Each capability submodule is imported below so a caller that reached
this package through `registry.capability_module` finds it as an attribute rather than composing
a module name:

* `bundle` — `bundle_facts`: the source extension, the identifier bound, the compiler-driver
  family, the compiler the host defaults to, and the names the host gives this language's files.
* `syntax` — `syntax_promotions`: which files the `Generate.gate` syntax stage reads and in what
  order.
* `checks_abi` — `checks_abi`: `docs/backends/language/cuda_cpp/CHECKS_ABI.md` and the checks
  ABI's declaration table (`CHECKS_PUBLIC_NAMES`, `CHECKS_ABI_PARAMS`).
* `prompts` — `prompt_fragments`: the `harness` and physics shapes' fragments, the runner-output
  binding and the exemplar note.
* `source` — `source_reading`: how the deterministic `Generate` gates read a CUDA C++ source.
* `signatures` — `signatures`: the lowering of the neutral §5.1 form to CUDA C++ and its pin on a
  generated source.
* `control_file` — the language half of `control_file`: the compile rules the build system's
  renderer writes the node's Makefile with.
* `header` — `interface_header`: the published-surface header the host renders from the IR.
* `runner` — `runner_render`: a physics node's runner and checks header, rendered over the
  certified harness (issue #289, R4-b PR-6).
* `lines` — comment / literal masking; `declarations` — the namespace-scope declaration reader.
"""

from tools.backends.language.cuda_cpp import bundle as bundle  # re-export
from tools.backends.language.cuda_cpp import (
    checks_abi as checks_abi,  # re-export
)
from tools.backends.language.cuda_cpp import (
    control_file as control_file,  # re-export
)
from tools.backends.language.cuda_cpp import header as header  # re-export
from tools.backends.language.cuda_cpp import (
    prompts as prompts,  # re-export
)
from tools.backends.language.cuda_cpp import runner as runner  # re-export
from tools.backends.language.cuda_cpp import (
    signatures as signatures,  # re-export
)
from tools.backends.language.cuda_cpp import source as source  # re-export
from tools.backends.language.cuda_cpp import syntax as syntax  # re-export
