#!/usr/bin/env python3
"""The Fortran language backend.

Submodules, by capability. Each capability submodule is imported below so a caller that reached
this package through `registry.capability_module` finds it as an attribute rather than composing
a module name:

* `bundle` — the `bundle_facts` capability: the Fortran facts the neutral `CodegenBundle`
  contract applies (source extensions, compiler-driver families, the identifier bound) and the
  compiler the host defaults to for this language.
* `runner` — the `runner_render` capability: the text of a physics node's host-authored runner
  glue over the certified harness, plus the harness-interface pin that guards it.
* `syntax` — the `syntax_promotions` capability: which files the `Generate.gate` syntax stage
  reads, their order, and the warning classes it promotes to errors.
* `checks_abi` — the `checks_abi` capability: the Fortran binding of the checks-module contract
  and the legality / gate-guard rules (`docs/backends/language/fortran/CHECKS_ABI.md`).
* `prompts` — the `prompt_fragments` capability: the Fortran authoring and review rules the
  pure `generate` prompt templates carry (`tools/prompt_templates/backends/language/fortran/`).
* `source` — the `source_reading` capability: how the deterministic `Generate` gates read a
  Fortran source (views, declarations, envelopes, calls, module map, checks-module facts) and
  the gates whose every rule is Fortran syntax.
* `signatures` — the `signatures` capability: the language-neutral structured signature form
  <-> Fortran interface stanzas, the §5.1 comparison atoms, and a certified source's published
  interface.
* `control_file` — the language half of the `control_file` capability: what a build control
  file must say to compile and link Fortran (the compiler variable, the flags, the module
  artifacts), composed by the build system's renderer.
* `lines` — free-form logical-line scanning (comments, `&` continuations, `;` statements).
* `structure` — the tree-sitter-fortran structural front end the model gates read through.
* `structure_differential` — a developer harness that diffs `structure` against a flang oracle.

`docs/BACKEND_BOUNDARY.md` states which capabilities are still inlined in the neutral core.
"""

from tools.backends.language.fortran import bundle as bundle  # noqa: F401  (re-export)
from tools.backends.language.fortran import (
    checks_abi as checks_abi,  # noqa: F401  (re-export)
)
from tools.backends.language.fortran import (
    control_file as control_file,  # noqa: F401  (re-export)
)
from tools.backends.language.fortran import (
    prompts as prompts,  # noqa: F401  (re-export)
)
from tools.backends.language.fortran import runner as runner  # noqa: F401  (re-export)
from tools.backends.language.fortran import (
    signatures as signatures,  # noqa: F401  (re-export)
)
from tools.backends.language.fortran import source as source  # noqa: F401  (re-export)
from tools.backends.language.fortran import syntax as syntax  # noqa: F401  (re-export)
