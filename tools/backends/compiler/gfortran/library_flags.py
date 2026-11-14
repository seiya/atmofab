"""The `gfortran` compiler's `library_flags` capability (issue #644, Part 3 P1).

How a build of this compiler reaches an external library the operator installed: the include
flag that finds its module files, the link-line words that link it, where the linker finds the
library's own dependencies, which file the linker takes for a link name, and which file a module
name is read from. The host holds the library's declaration (`spec/libraries/<id>.yaml`) and the
site's directories (`sites.yaml`) and asks this module; it composes no flag itself.

The library is linked BY PATH, never `-L<dir> -l<name>`: under an MPI compiler wrapper a `-L` of
a system directory pulled a second MPI into the binary, and by path the file linked is the file
the build key hashed. The linker's `-rpath-link` names each library directory, so a declared
library's own dependencies in the same directory are found at link time without the inherited
loader path (otherwise the host link passed or failed by the operator's environment, and the
workshop's cleared environment always failed). It records nothing in the binary: the run finds
the libraries through the loader path the `library_binary` capability composes.

Stdlib only.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable
from pathlib import Path

#: The driver asked for its own search when a site entry names no `lib:` directory.
DRIVER = "gfortran"
#: The suffix of the module file this compiler writes and reads for a module.
MODULE_SUFFIX = ".mod"
#: The file names a link name resolves to, in preference order: the shared object when the
#: directory holds both kinds, as the linker's own `-l` search takes it.
_LINK_FILE_FORMS: tuple[str, ...] = ("lib{name}.so", "lib{name}.a")


def compile_flags(include_dirs: Iterable[str]) -> tuple[str, ...]:
    """The words that put each include directory on the module search path."""
    return tuple(f"-I{d}" for d in include_dirs)


def link_flags(files: Iterable[str], lib_dirs: Iterable[str]) -> tuple[str, ...]:
    """The link-line words: each resolved library file by path, in link order, then the
    indirect-dependency search option for every library directory."""
    return (*(str(f) for f in files), *(f"-Wl,-rpath-link,{d}" for d in lib_dirs))


def _driver_search(file_name: str) -> Path | None:
    """The file the driver's own search finds for `file_name`, or None. The driver echoes the
    bare name back when it finds nothing."""
    try:
        proc = subprocess.run([DRIVER, f"-print-file-name={file_name}"], capture_output=True,
                              text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    answer = proc.stdout.strip()
    if proc.returncode != 0 or not answer or not os.path.isabs(answer):
        return None
    path = Path(os.path.normpath(answer))
    return path if path.is_file() else None


def resolve(name: str, lib_dir: str | None) -> Path | None:
    """The file the linker takes for link name `name`: in `lib_dir` when given, else through the
    driver's own search; the shared object before the archive. None when neither exists."""
    for form in _LINK_FILE_FORMS:
        file_name = form.format(name=name)
        if lib_dir is not None:
            path = Path(lib_dir) / file_name
            if path.is_file():
                return Path(os.path.normpath(path))
        else:
            found = _driver_search(file_name)
            if found is not None:
                return found
    return None


def module_file(module: str, include_dir: str | None) -> Path | None:
    """The module file a `use <module>` reads from `include_dir`, or None. The compiler writes
    the module name in lower case."""
    if include_dir is None:
        return None
    path = Path(include_dir) / f"{module.lower()}{MODULE_SUFFIX}"
    return path if path.is_file() else None
