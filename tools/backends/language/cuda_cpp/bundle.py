"""What the `CodegenBundle` contract has to know about CUDA C++ (issue #289, R4-b PR-4).

The `bundle_facts` capability: the facts the neutral bundle contract (`tools/codegen_bundle.py`)
applies to a file of this language, the names the host gives this language's files, and the
compiler it defaults to. `docs/backends/language/cuda_cpp/BUNDLE_BINDING.md` states the binding
these implement for a reader.

Stdlib only, and no import of the rest of this package.
"""

from __future__ import annotations

#: The source extension a bundle file of this language may carry. ONE extension, `.cu`: every
#: declaration a leaf source needs of a published surface is in the header the HOST renders
#: (`header.py`, BUNDLE_BINDING.md §1), so a leaf ships no header — and cannot ship one under the
#: header's name.
SOURCE_EXTENSIONS: tuple[str, ...] = (".cu",)

#: Compiler-driver program names for this language, for the `toolchain.compiler` / `linker` echo
#: in the derived build graph. `nvcc` is the only driver that compiles a `.cu` translation unit
#: with its device code here; an unrecognized selector is dropped and the build uses its default.
COMPILER_SELECTOR_FAMILIES: tuple[str, ...] = ("nvcc",)

#: An identifier is 1-1024 characters: the number of significant initial characters of an
#: internal identifier the C++ standard's implementation quantities recommend (Annex B). The
#: bundle contract rejects a longer entrypoint `symbol` / `module` before assembly.
IDENTIFIER_MAX = 1024

#: The same bound as a whole-string pattern, in the portable spelling the bundle schema uses
#: (see the Fortran backend's `IDENTIFIER_PATTERN` for why `(?![\s\S])` rather than `$`). A
#: leading underscore is legal C++ but excluded, as the other languages' patterns exclude it:
#: every name the host derives starts with the spec_id.
IDENTIFIER_PATTERN = rf"^[A-Za-z][A-Za-z0-9_]{{0,{IDENTIFIER_MAX - 1}}}(?![\s\S])"

#: The compiler the host uses for this language when the target profile pins no
#: `toolchain.compiler` — both the build compiler and the mandatory `Generate.gate` syntax stage,
#: which must be one value (the Fortran backend's `DEFAULT_COMPILER` comment gives the reason).
DEFAULT_COMPILER = "nvcc"

#: The syntax-only stage `Generate.gate` must run, and pass, for this language.
MANDATORY_SYNTAX_COMPILER = DEFAULT_COMPILER

#: Sources of this language are compiled, so the build needs a tool that tracks dependencies.
COMPILED = True

#: The extension of every source file the HOST names for this language.
HOST_SOURCE_EXTENSION = ".cu"


def model_basename(spec_id: str) -> str:
    """The node's model source: the definitions of its published operations."""
    return f"{spec_id}_model{HOST_SOURCE_EXTENSION}"


def checks_basename(spec_id: str) -> str:
    """The node's checks source."""
    return f"{spec_id}_checks{HOST_SOURCE_EXTENSION}"


def runner_basename(spec_id: str) -> str:
    """The node's runner source: the harness's own self-test entry on a harness node."""
    return f"{spec_id}_runner{HOST_SOURCE_EXTENSION}"
