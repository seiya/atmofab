#!/usr/bin/env python3
"""The Fortran language backend.

Submodules, by capability:

* `bundle` — the Fortran facts the neutral `CodegenBundle` contract applies (source extensions,
  compiler-driver families, the identifier bound). Imported below so a caller that reached this
  package through `registry.load` finds it as an attribute rather than composing a module name.
* `runner` — the `runner_render` capability: the text of a physics node's host-authored runner
  glue over the certified harness, plus the harness-interface pin that guards it. Imported below
  for the same reason as `bundle`: `registry.capability_module` returns this package, and the
  neutral seam (`tools/host_render.py`) reads the capability off it as an attribute.
* `lines` — free-form logical-line scanning (comments, `&` continuations, `;` statements).
* `structure` — the tree-sitter-fortran structural front end the model gates read through.
* `signatures` — the language-neutral structured signature form <-> Fortran interface stanzas.
* `structure_differential` — a developer harness that diffs `structure` against a flang oracle.

`docs/BACKEND_BOUNDARY.md` states which capabilities are still inlined in the neutral core.
"""

from tools.backends.language.fortran import bundle as bundle  # noqa: F401  (re-export)
from tools.backends.language.fortran import runner as runner  # noqa: F401  (re-export)
