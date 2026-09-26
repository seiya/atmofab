#!/usr/bin/env python3
"""What the `Generate.gate` syntax-only stage has to know about Fortran (issue #289, R4-b).

The `syntax_promotions` capability: which files are sources, the order they are handed to the
compiler in, and which warning classes the stage promotes to errors. The compiler adapter
(`tools/backends/compiler/<id>/syntax.py`) builds the command line out of these; both moved
here from `mcp_servers/build_runtime_server.py`.

Stdlib only.
"""

from __future__ import annotations

import re
from pathlib import Path

#: The suffixes the stage treats as free-form Fortran sources — for auto-discovery, for the
#: source-name rule an explicit list must satisfy, and for the conductor's "no source to check"
#: test. One tuple for all three, so auto-discovery cannot accept a file an explicit list
#: refuses.
SOURCE_SUFFIXES: tuple[str, ...] = (".f90", ".f95", ".f03", ".f08")

#: The files copied into the stage directory: the sources themselves (nothing a Fortran source
#: needs is a separate file of another suffix — a dependency's module is its staged source).
STAGED_SUFFIXES: tuple[str, ...] = SOURCE_SUFFIXES

#: The warning classes promoted to errors over the whole staged set, spelled as the language's
#: mandatory syntax compiler (`bundle.MANDATORY_SYNTAX_COMPILER`) takes them. `-Werror=<class>`
#: self-enables the warning, so no companion `-W<class>` is needed.
#:
#: unused-dummy-argument and unused-variable: a dummy an interface fixes but the algorithm never
#: consumes (an inert input, an ABI-fixed `name` / `case_id`) must stay a live dummy bound by
#: `associate (unused_<name> => <name>); end associate` — the canonical idiom is the checks ABI
#: binding (`docs/backends/language/fortran/CHECKS_ABI.md`), and this promotion is what makes it
#: load-bearing rather than advisory. ampersand: gfortran's extension of resuming a continued
#: character literal with NO leading `&` let a counted-`do` spelling written inside a string reach
#: a PHYSICAL line start, where the fail_closed OpenMP presence floor — anchored at line starts,
#: deliberately stateless — counted it and falsely rejected a source this gate had passed (issue
#: #25). Rejecting the shape here is what makes the floor's anchoring argument sound; a
#: conforming literal resumes with `&` and is unaffected. Only these three: promoting
#: -Wall/-Wextra wholesale would reject correct-as-written sources — -Wcompare-reals fires on the
#: harness self-test runner's deliberate bitwise equality comparisons, which are the point of the
#: assertion.
PROMOTED_WARNINGS: tuple[str, ...] = (
    "-Werror=unused-dummy-argument", "-Werror=unused-variable", "-Werror=ampersand",
)

# `module <name>` definitions (excluding submodule-procedure headers) and `use <name>`
# references, scanned to order the staged sources so each module is compiled before its
# consumers within ONE compiler invocation (gfortran resolves same-invocation `use`
# against the module files it just wrote to the scratch dir, even under -fsyntax-only).
# Deliberately approximate: a mis-ordering only reorders the argv and the compiler then
# reports the real diagnosis; correctness judgment always stays with the compiler.
_MODULE_DECL_RE = re.compile(
    r"^\s*module\s+(?!procedure\b|subroutine\b|function\b)([a-z][a-z0-9_]*)\s*(?:!.*)?$",
    re.IGNORECASE | re.MULTILINE,
)
# `use\b` (word boundary) so only a real `use` STATEMENT matches — `use foo`, `use::foo`,
# `use, intrinsic :: foo` — and an ordinary identifier that merely starts with the letters
# "use" (`user_flag = ...`, `usedcount = ...`) does NOT (there is no word boundary between
# `use` and a following word char). Without the boundary the old `use\s*` over-matched such
# names and minted a spurious dependency edge in the source ordering.
_USE_STMT_RE = re.compile(
    r"^\s*use\b\s*(?:,\s*(?:non_)?intrinsic\s*)?(?:::)?\s*([a-z][a-z0-9_]*)",
    re.IGNORECASE | re.MULTILINE,
)


def compile_order(project_dir: Path) -> list[str]:
    """Topologically order the free-form Fortran sources in `project_dir` (define-before-use).

    `use` of a module no local file defines (intrinsic modules, and genuinely missing
    dependencies) is ignored for ordering — if it is a real omission the compiler emits
    the authoritative "Cannot open module file" finding. On a definition cycle the
    remaining files are appended name-sorted and the compiler diagnoses the cycle.
    """
    names = sorted(
        p.name
        for p in project_dir.iterdir()
        if p.is_file() and p.suffix.lower() in SOURCE_SUFFIXES
    )
    provided_by: dict[str, str] = {}
    uses: dict[str, set[str]] = {}
    for name in names:
        try:
            text = (project_dir / name).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
        for mod in _MODULE_DECL_RE.findall(text):
            provided_by.setdefault(mod.lower(), name)
        uses[name] = {mod.lower() for mod in _USE_STMT_RE.findall(text)}

    ordered: list[str] = []
    placed: set[str] = set()
    remaining = list(names)
    while remaining:
        progressed = False
        for name in list(remaining):
            deps = {
                provided_by[mod]
                for mod in uses.get(name, set())
                if mod in provided_by and provided_by[mod] != name
            }
            if deps <= placed:
                ordered.append(name)
                placed.add(name)
                remaining.remove(name)
                progressed = True
        if not progressed:
            ordered.extend(remaining)
            break
    return ordered
