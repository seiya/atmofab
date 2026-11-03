"""The `nvcc` compiler's `checked_flags` capability (issue #589 PR-0).

None: the CUDA compiler driver has no bounds-checked compile. A device-side memory check is a
launch wrapper (a sanitizer the binary runs under), which is the execution half's, not the
build's; until one lands, a target that builds with this compiler records `checked_build: null`
and runs no `checked` row (issue #589, PR-0 decision 3).

Stdlib only.
"""

from __future__ import annotations

#: Empty: no checked build is made for this compiler.
CHECKED_FLAGS: tuple[str, ...] = ()
