#!/usr/bin/env python3
"""The `gfortran` compiler backend.

Submodules, by capability:

* `syntax` — the `syntax_check` capability: the argv of the syntax-only stage `Generate.gate`
  runs, the canary that separates a broken invocation from broken sources, and the version
  probe. Imported below so a caller that reached this package through
  `registry.capability_module` finds it as an attribute rather than composing a module name.
"""

from tools.backends.compiler.gfortran import syntax as syntax  # noqa: F401  (re-export)
