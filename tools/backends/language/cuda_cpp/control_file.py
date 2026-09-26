"""What a build control file has to say about compiling CUDA C++ (issue #289, R4-b PR-4).

The LANGUAGE half of the `control_file` capability, shaped like the Fortran backend's: the build
system's renderer (`tools/backends/build_system/<id>/control_file.py`) takes `rules()` and writes
one compile rule per source and one link rule. A CUDA C++ source is compiled on its own against
the host-rendered headers (BUNDLE_BINDING.md §1), so each `.cu` is one object and the objects meet
at link — the build graph's ordinary shape. The staged dependency headers sit in the object
directory, which is why it is on the include path.

Stdlib only; imports nothing from the neutral core.
"""

from __future__ import annotations

from typing import Any

from tools.backends.language.cuda_cpp.bundle import HOST_SOURCE_EXTENSION


def flags(standard: str, architecture: str | None) -> str:
    """The compile / link flags: the target's standard, its GPU architecture when the profile
    names one (`-arch=`), and the object directory on the include path."""
    value = f"-std={standard} -O2"
    if architecture:
        value += f" -arch={architecture}"
    return value + " -I$(OBJDIR)"


def rules(*, standard: str, parallel_backend: str, architecture: str | None = None
          ) -> dict[str, Any]:
    """The language facts a build system's control-file renderer composes with its grammar.
    `parallel_backend` is ACCEPTED AND NOT READ: CUDA's parallelism is in the source (kernels
    and launches), not a compiler switch."""
    del parallel_backend  # accepted, not read (see the docstring)
    return {
        "language": "cuda_cpp",
        "compiler_variable": "NVCC",
        "flags_variable": "NVCCFLAGS",
        "flags": flags(standard, architecture),
        "source_suffix": HOST_SOURCE_EXTENSION,
        # A compile leaves nothing beside its object.
        "compile_artifact_globs": (),
        "compiler_pin_comment": (
            "NVCC is pinned with := so an environment value of the same name cannot redirect the",
            "build. The pinned value is the target profile's toolchain.compiler when it sets one,",
            "else nvcc.",
        ),
    }
