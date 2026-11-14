#!/usr/bin/env python3
"""The `gfortran` compiler backend.

Submodules, by capability:

* `syntax` — the `syntax_check` capability: the argv of the syntax-only stage `Generate.gate`
  runs, the canary that separates a broken invocation from broken sources, and the version
  probe. Imported below so a caller that reached this package through
  `registry.capability_module` finds it as an attribute rather than composing a module name.
* `checked_flags` — the `checked_flags` capability: the flags the bounds-checked second build
  adds to the target's own (issue #589 PR-0); this compiler adds `-fcheck=bounds`.
* `library_flags` — the `library_flags` capability: the include and link-line words that reach an
  external library, the file a link name or a module resolves to (issue #644, Part 3 P1).
* `library_binary` — the `library_binary` capability: the loader variable a run is prefixed with,
  a shared object's load name, and the symbol tables the post-build checks read (issue #644).
"""

from tools.backends.compiler.gfortran import (
    checked_flags as checked_flags,
)
from tools.backends.compiler.gfortran import (
    library_binary as library_binary,
)
from tools.backends.compiler.gfortran import (
    library_flags as library_flags,
)
from tools.backends.compiler.gfortran import syntax as syntax
