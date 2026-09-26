#!/usr/bin/env python3
"""What a build control file has to say about compiling Fortran (issue #289, R4-b PR-3).

The LANGUAGE half of the `control_file` capability. The control file's grammar is the build
system's (`tools/backends/build_system/<id>/control_file.py`); what it must say to compile and
link this language — the compiler variable, the flags, the artifacts a compile leaves beside its
object, the reason the compiler is pinned the way it is — is this module's, and the build system's
renderer takes it as `rules()`. Moved out of `tools/workflow_conductor.py`'s two Makefile
templates, whose output is unchanged byte for byte.

Stdlib only.
"""

from __future__ import annotations

from typing import Any

from tools.backends.language.fortran.bundle import HOST_SOURCE_EXTENSION


def flags(standard: str, parallel_backend: str) -> str:
    """The compile / link flags for a target's `toolchain.standard` and parallel backend.

    `-J`/`-I` put the compiled `.mod` files beside the objects, under the out-of-source object
    directory the build system passes as `object_dir`. `-fopenmp` is the OpenMP backend's switch
    as this language's default compiler spells it; it enables the directives a source carries and
    adds none (why every node of a target takes it, the harness included, is the conductor's
    `_write_makefile` docstring)."""
    value = f"-std={standard} -O2"
    if parallel_backend == "openmp":
        value += " -fopenmp"
    return value + " -J$(OBJDIR) -I$(OBJDIR)"


#: Whether the rules READ the target's `hardware.architecture`: they do not (it is accepted by
#: `rules` and ignored), so it stays out of the build derivation key, which is byte-identical to
#: the key before the argument existed.
READS_ARCHITECTURE = False


def rules(*, standard: str, parallel_backend: str, architecture: str | None = None
          ) -> dict[str, Any]:
    """The language facts a build system's control-file renderer composes with its grammar.
    `architecture` (the target profile's `hardware.architecture`) is ACCEPTED AND NOT READ: a CPU
    Fortran compile takes no device architecture, and the argument exists for the languages whose
    compiler does."""
    del architecture  # accepted, not read (see the docstring)
    return {
        # The name the renderer's header comment gives the language.
        "language": "fortran",
        # The variable the compiler is bound to, and the one its flags are.
        "compiler_variable": "FC",
        "flags_variable": "FFLAGS",
        "flags": flags(standard, parallel_backend),
        # The suffix of every source the host names (model, checks, runner, a staged dependency).
        "source_suffix": HOST_SOURCE_EXTENSION,
        # What a compile leaves beside its object, for the clean target (the object itself is the
        # build system's).
        "compile_artifact_globs": ("*.mod",),
        # Why the compiler variable is PINNED rather than defaulted, as comment lines. It is a
        # fact about this language's variable in the build system this language is paired with,
        # which is why it is stated here and printed verbatim by the renderer.
        "compiler_pin_comment": (
            ("FC is pinned with := (not ?=): make ships a built-in FC=f77 (origin default), and "
             "?= does"),
            ("NOT override a default-origin variable, so `FC ?= gfortran` would silently leave "
             "FC=f77."),
            ("The pinned value is the target profile's toolchain.compiler when it sets one, else "
             "gfortran."),
        ),
    }
