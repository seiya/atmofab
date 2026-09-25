"""What the `Generate.gate` syntax-only stage has to know about CUDA C++ (issue #289, R4-b PR-4).

The `syntax_promotions` capability: which files are sources, the order they are handed to the
compiler in, and which warning classes the stage promotes to errors. The compiler adapter
(`tools/backends/compiler/nvcc/syntax.py`) builds the command line out of these.

Stdlib only.
"""

from __future__ import annotations

from pathlib import Path

#: The suffixes the stage treats as translation units — for auto-discovery, for the source-name
#: rule an explicit list must satisfy, and for the conductor's "no source to check" test. A
#: model source a runner or a consumer `#include`s is also a translation unit of its own
#: (`bundle.SOURCE_EXTENSIONS`), so every staged `.cu` is checked on its own as well.
SOURCE_SUFFIXES: tuple[str, ...] = (".cu",)

#: NONE, deliberately. The warning classes a CUDA C++ source is held to are the static-lint
#: rule set (`tools/backends/linter/nvcc/lint.py`: every host-compiler warning of `-Wall -Wextra`
#: and every device-front-end warning, as errors), which the `Generate.gate` lint check applies to
#: the same files; promoting a subset here as well would state one rule twice, in two places
#: that could drift. This stage answers "does it compile" for the target's standard and
#: architecture, which the lint invocation does not pin.
PROMOTED_WARNINGS: tuple[str, ...] = ()


def compile_order(project_dir: Path) -> list[str]:
    """The `.cu` sources in `project_dir`, name-sorted. Each is its own translation unit and a
    unit reaches another only by `#include`, which the compiler resolves from the directory, so
    no order between them is needed."""
    return sorted(
        p.name for p in project_dir.iterdir()
        if p.is_file() and p.suffix.lower() in SOURCE_SUFFIXES
    )
