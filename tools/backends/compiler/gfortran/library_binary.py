"""The `gfortran` compiler's `library_binary` capability (issue #644, Part 3 P1).

What a binary this compiler links reads at run time, and what its objects' symbol tables say:
the dynamic loader's search variable and how a directory list is prefixed to it, the name a
shared object is loaded under, and the symbols an object imports or defines and a library
exports. On the `compiler` axis because the compiler driver chooses the linker and, through it,
the dynamic loader's input (docs/BACKEND_BOUNDARY.md, the `compiler` axis).

The loader path is PREFIXED, never replaced: replacing it drops the operator's MPI and compiler
runtime directories. A binary linked with an `RPATH` instead is defeated under an MPI compiler
wrapper, which links with new dtags; a loader-path prefix holds under both.

Stdlib only.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable
from pathlib import Path

#: The dynamic loader's search variable.
LOADER_VARIABLE = "LD_LIBRARY_PATH"
#: The programs this module runs. The host refuses to start a run that declares a library on a
#: host without them (`tools/host_prerequisites.py`).
EXECUTABLES: tuple[str, ...] = ("nm", "objdump")
#: The first bytes of an archive.
_ARCHIVE_MAGIC = b"!<arch>\n"


def loader_env(library_dirs: Iterable[str], inherited: str | None) -> dict[str, str]:
    """`{LOADER_VARIABLE: value}`: the directories first, then the inherited value; no separator
    when the inherited value is empty, because the loader reads an empty element as the current
    directory. Empty when `library_dirs` is."""
    dirs = [str(d) for d in library_dirs]
    if not dirs:
        return {}
    value = ":".join(dirs)
    if inherited:
        value = f"{value}:{inherited}"
    return {LOADER_VARIABLE: value}


def _run(argv: list[str]) -> str:
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=300, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(argv)} exited {proc.returncode}: {proc.stderr.strip()}")
    return proc.stdout


def is_archive(path: str | Path) -> bool:
    with open(path, "rb") as handle:
        return handle.read(len(_ARCHIVE_MAGIC)) == _ARCHIVE_MAGIC


def soname(link_file: str | Path) -> str | None:
    """The name the loader loads a shared object under; None for an archive or a shared object
    that records none."""
    if is_archive(link_file):
        return None
    for line in _run(["objdump", "-p", str(link_file)]).splitlines():
        words = line.split()
        if len(words) == 2 and words[0] == "SONAME":
            return words[1]
    return None


def _symbols(argv: list[str]) -> set[str]:
    """The names of `nm -P` output, skipping an archive's member headers (`lib.a[x.o]:`)."""
    names: set[str] = set()
    for line in _run(argv).splitlines():
        words = line.split()
        if len(words) >= 2 and not words[0].endswith(":"):
            names.add(words[0])
    return names


def imports(object_file: str | Path) -> set[str]:
    """The symbols an object leaves undefined: what it takes from outside itself."""
    return _symbols(["nm", "-P", "-u", str(object_file)])


def defines(object_file: str | Path) -> set[str]:
    """The global symbols an object defines."""
    return _symbols(["nm", "-P", "-g", "--defined-only", str(object_file)])


def exports(library_file: str | Path) -> set[str]:
    """The global symbols a library defines: an archive's members' symbol tables, a shared
    object's dynamic symbol table."""
    if is_archive(library_file):
        return _symbols(["nm", "-P", "-g", "--defined-only", str(library_file)])
    return _symbols(["nm", "-P", "-D", "--defined-only", str(library_file)])
