#!/usr/bin/env python3
"""What the `CodegenBundle` contract has to know about Fortran.

The bundle contract itself (`tools/codegen_bundle.py`) is neutral: it validates roles, logical
paths, entrypoints and the build order without knowing any language. Four facts it applies are
Fortran's, and they used to sit in that neutral module as per-language lookup tables — the
`language` axis half of the migration ledger's `codegen_bundle` area (`TODO.md`,
`docs/BACKEND_BOUNDARY.md`). They are read through `tools/backends/registry.py`.

Stdlib only, and no import of the rest of this package: the neutral core loads it for every
bundle it validates.
"""

from __future__ import annotations

#: The source extensions a bundle file of this language may carry. It is an ALLOWLIST, not a
#: recognizer: `.f`/`.f95` are Fortran too, but the generated sources are free-form f2008 and the
#: build rules the host authors match `.f90` only, so admitting another spelling here would
#: produce a file nothing compiles.
SOURCE_EXTENSIONS: tuple[str, ...] = (".f90",)

#: Compiler-driver program names for this language, for the `toolchain.compiler` / `linker` echo
#: in the derived build graph. A driver for the WRONG language would be pinned as `FC` and
#: deterministically fail on the sources (`gcc` cannot compile `.f90`), which is why the echo is
#: filtered by the bundle's language rather than by a generic "looks like a compiler" test. An
#: unrecognized selector is dropped and the build uses its default.
COMPILER_SELECTOR_FAMILIES: tuple[str, ...] = (
    "gfortran", "flang", "flang-new", "f95", "g95", "ifort", "ifx", "nvfortran",
    "pgfortran", "pgf90", "pgf95", "xlf", "xlf90", "xlf95", "armflang", "crayftn", "ftn",
    "nagfor", "mpif90", "mpifort", "mpif77", "frt", "frtpx",
)

#: An identifier is 1-63 characters (the f2008/f2018 limit). An entrypoint `symbol` / `module`,
#: or a `state_bindings` `state_variable` / `storage_symbol`, longer than this cannot pass the
#: mandatory `Generate.gate` syntax check, so the bundle contract rejects it before assembly
#: rather than deferring the failure to the build.
IDENTIFIER_MAX = 63

#: The same bound as a whole-string pattern, in the portable spelling the bundle schema uses:
#: `^` … `(?![\s\S])` rather than `^` … `$`, because under Python `re` a `$` also matches before
#: a trailing newline while `\A`/`\Z` are invalid in ECMA-262. The negative lookahead means "no
#: character follows" and is identical in both.
IDENTIFIER_PATTERN = rf"^[A-Za-z][A-Za-z0-9_]{{0,{IDENTIFIER_MAX - 1}}}(?![\s\S])"

#: The compiler the host uses for this language when the target profile pins no
#: `toolchain.compiler`. It is BOTH the compiler the build control file pins and the mandatory
#: `Generate.gate` syntax stage, and it has to be one value for the two: the syntax gate
#: certifies that stage and the build then runs this one, so a divergence would certify one
#: compiler and build with another. The equality is also what lets the launch-time host probe
#: (`tools/host_prerequisites.py`) cover the BUILD compiler by probing the mandatory SYNTAX stage.
DEFAULT_COMPILER = "gfortran"

#: The syntax-only stage `Generate.gate` must run, and pass, for this language whatever
#: `ATMOFAB_SYNTAX_COMPILERS` lists; the post_generate certification requires its stage. Named
#: separately from `DEFAULT_COMPILER` because the readers ask different questions, and bound to
#: it because the answers must not differ (see above).
MANDATORY_SYNTAX_COMPILER = DEFAULT_COMPILER
