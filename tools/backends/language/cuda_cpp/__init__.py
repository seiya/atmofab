"""The CUDA C++ language backend (issue #289, R4-b PR-4).

Submodules, by capability. Each capability submodule is imported below so a caller that reached
this package through `registry.capability_module` finds it as an attribute rather than composing
a module name:

* `bundle` — `bundle_facts`: the source extension, the identifier bound, the compiler-driver
  family, the compiler the host defaults to, and the names the host gives this language's files.
* `syntax` — `syntax_promotions`: which files the `Generate.gate` syntax stage reads and in what
  order.
* `checks_abi` — `checks_abi`: `docs/backends/language/cuda_cpp/CHECKS_ABI.md`, whose §5 (the
  legality and gate-guard rules) the `harness` producer is shown.
* `prompts` — `prompt_fragments`: the `harness` shape's fragments, the runner-output binding and
  the exemplar note.
* `source` — `source_reading`: how the deterministic `Generate` gates read a CUDA C++ source.
* `signatures` — `signatures`: the lowering of the neutral §5.1 form to CUDA C++ and its pin on a
  generated source.
* `control_file` — the language half of `control_file`: the compile rules the build system's
  renderer writes the node's Makefile with.
* `header` — `interface_header`: the published-surface header the host renders from the IR.
* `lines` — comment / literal masking; `declarations` — the namespace-scope declaration reader.

WHAT IT DOES NOT DECLARE: `runner_render`. The host does not render a runner over a `cuda_cpp`
harness yet, so a `cuda_cpp` physics node is refused at launch
(`target_profile.toolchain_servable_reasons`), and only an `infrastructure` node — the harness,
whose leaf authors the model and the runner — can run.
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
from tools.backends.language.cuda_cpp import (
    signatures as signatures,  # re-export
)
from tools.backends.language.cuda_cpp import source as source  # re-export
from tools.backends.language.cuda_cpp import syntax as syntax  # re-export
