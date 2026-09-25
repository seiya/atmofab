"""The `nvcc` linter backend: the static-lint step for `cuda_cpp` sources.

Submodules, by capability:

* `lint` — the `lint` and `lint_rules` capabilities: the invocation the `Generate.gate` lint check
  runs (the CUDA compiler driver with every warning an error), its version range and launch
  self-check, and the rule set as a document a leaf is handed.
"""

from tools.backends.linter.nvcc import lint as lint  # re-export
