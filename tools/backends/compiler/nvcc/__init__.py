"""The `nvcc` compiler backend.

Submodules, by capability:

* `syntax` — the `syntax_check` capability: the argv of the syntax-only stage `Generate.gate`
  runs over CUDA C++ sources, the canary that separates a broken invocation from broken sources,
  and the version probe.
"""

from tools.backends.compiler.nvcc import syntax as syntax  # re-export
