#!/usr/bin/env python3
"""The `make` build-system backend.

Submodules, by capability. Each capability submodule is imported below so a caller that reached
this package through `registry.capability_module` finds it as an attribute rather than composing
a module name:

* `control_file` — the `control_file` capability: the renderers of the host-authored
  `src/Makefile` and the deterministic gates over a Makefile, plus the file name make reads.
* `parse` — the GNU-make sublanguage reader the gates stand on.
* `gates` — the Makefile gates themselves (re-exported through `control_file`).
* `failure` — the mechanical classification of a failed build's output.

`build_execute` is still carried by the neutral core; see `docs/BACKEND_BOUNDARY.md`.
"""

from tools.backends.build_system.make import (
    control_file as control_file,  # noqa: F401  (re-export)
)
