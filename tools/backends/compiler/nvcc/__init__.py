"""The `nvcc` compiler backend.

Submodules, by capability:

* `syntax` — the `syntax_check` capability: the argv of the syntax-only stage `Generate.gate`
  runs over CUDA C++ sources, the canary that separates a broken invocation from broken sources,
  and the version probe.
* `checked_flags` — the `checked_flags` capability: the flags the bounds-checked second build
  adds to the target's own (issue #589 PR-0); this compiler declares none.
"""

from tools.backends.compiler.nvcc import checked_flags as checked_flags  # re-export
from tools.backends.compiler.nvcc import syntax as syntax  # re-export
