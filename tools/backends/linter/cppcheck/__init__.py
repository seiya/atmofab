#!/usr/bin/env python3
"""The `cppcheck` linter backend.

Submodules, by capability:

* `lint` — the `lint` capability: the argv the `Generate.gate` static-lint step runs, the check
  set that argv declares, and the linter versions it was measured against. Imported below so a
  caller that reached this package through `registry.capability_module` finds the implementation
  as an attribute rather than composing a module name.
"""

from tools.backends.linter.cppcheck import lint as lint  # noqa: F401  (re-export)
