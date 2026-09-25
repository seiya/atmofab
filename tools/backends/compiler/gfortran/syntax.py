#!/usr/bin/env python3
"""The `gfortran` syntax-only adapter the `Generate.gate` syntax stage runs (issue #289, R4-b).

It moved here from `mcp_servers/build_runtime_server.py`, which now reaches it through
`registry.capability_module("compiler", "gfortran", "syntax_check")` and keeps only what is
neutral: the scratch directory, the source-name rule, the run and its command log.

What this module knows is how THIS compiler is asked for a syntax-only pass. What the pass
promotes to errors, which suffixes are sources, and in which order they are handed over are the
LANGUAGE's facts (`tools/backends/language/<LANGUAGE>/syntax.py`), passed in by the caller.

Stdlib only.
"""

from __future__ import annotations

#: The program the stage launches; also what the launch-time host probe looks for
#: (`tools/host_prerequisites.py`) and what the post_generate certification compares a logged
#: argv[0] with.
EXECUTABLE = "gfortran"

#: The language whose sources this adapter reads. `run_syntax_check` asks that language's
#: `syntax_promotions` for the suffixes, the order and the promoted warnings.
LANGUAGE = "fortran"

#: First line of its output is recorded as the stage's `compiler_version`.
VERSION_ARGV: tuple[str, ...] = (EXECUTABLE, "--version")

#: A source valid under every Fortran standard the adapter accepts. `std` reaches the gate from
#: the target profile (`toolchain.standard`) and goes straight into `-std=<value>`: a value the
#: driver does not know (`-std=2008` — the elided-`f` form) makes it reject the COMMAND LINE, so
#: no source is ever parsed and every file in the invocation "fails" at once. Compiling this
#: canary with the same argv separates a broken invocation from broken sources by the compiler's
#: own verdict, without enumerating the stds a given compiler version happens to accept (`f2023`
#: exists on GCC>=13 but not before, so any hard-coded set is wrong on some machine).
CANARY_SOURCE = "module atmofab_syntax_canary\n  implicit none\nend module atmofab_syntax_canary\n"

#: The name the canary is staged under; its suffix is one the language's source rule accepts.
CANARY_FILENAME = "atmofab_syntax_canary.f90"


def argv(*, standard: str, scratch_dir: str, openmp: bool, promotions: tuple[str, ...],
         architecture: str | None, sources: list[str]) -> list[str]:
    """The syntax-only command line.

    `scratch_dir` receives the module files the front end writes even under `-fsyntax-only`
    (and reads back, so a same-invocation `use` resolves); every call gets its own and never
    shares Build's object directory. `architecture` is ACCEPTED AND NOT READ: a CPU Fortran
    front end has no device architecture to be told, and the argument exists for the adapters
    that do. `promotions` come first after the mode flags; the sources stay last so the compiler
    reads them after the module-directory flags.
    """
    del architecture  # accepted, not read (see the docstring)
    command = [EXECUTABLE, "-fsyntax-only", f"-std={standard}", *promotions,
               "-J", scratch_dir, "-I", scratch_dir]
    if openmp:
        command.append("-fopenmp")
    return command + list(sources)
