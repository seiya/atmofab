"""What a build control file has to say about compiling CUDA C++ (issue #289, R4-b PR-4).

The LANGUAGE half of the `control_file` capability, shaped like the Fortran backend's: the build
system's renderer (`tools/backends/build_system/<id>/control_file.py`) takes `rules()` and writes
one compile rule per source and one link rule. A CUDA C++ source is compiled on its own against
the host-rendered headers (BUNDLE_BINDING.md §1), so each `.cu` is one object and the objects meet
at link — the build graph's ordinary shape. The object directory is on the include path for the
dependency headers a physics node's Build stages there beside each dependency's source
(`workflow_conductor._stage_dependency_sources`, issue #289 R4-b PR-6).

Stdlib only; imports nothing from the neutral core.
"""

from __future__ import annotations

from typing import Any

from tools.backends.language.cuda_cpp.bundle import HOST_SOURCE_EXTENSION

#: Whether the rules READ the target's `hardware.architecture` (`-arch=`): they do, so the build
#: derivation key carries it (`orchestration_runtime._target_toolchain_identity`) — a build for
#: another architecture is another build.
READS_ARCHITECTURE = True


def flags(standard: str, architecture: str | None) -> str:
    """The compile / link flags: the target's standard, relocatable device code (`-rdc=true`: a
    kernel may call a `__host__ __device__` operation another translation unit defines, which
    the device link resolves — BUNDLE_BINDING.md §4), its GPU architecture when the profile
    names one (`-arch=`), and the object directory on the include path."""
    value = f"-std={standard} -O2 -rdc=true"
    if architecture:
        value += f" -arch={architecture}"
    return value + " -I$(OBJDIR)"


def rules(*, standard: str, parallel_backend: str, architecture: str | None = None,
          compiler_wrapper: str | None = None) -> dict[str, Any]:
    """The language facts a build system's control-file renderer composes with its grammar.
    `parallel_backend` is ACCEPTED AND NOT READ: CUDA's parallelism is in the source (kernels
    and launches), not a compiler switch. `compiler_wrapper` is ACCEPTED AND NOT READ: a
    target whose parallel backend declares a wrapper that runs another compiler than this
    language's is refused at launch (`target_profile.target_profile_violations`), and no
    wrapper runs this language's compiler."""
    del parallel_backend, compiler_wrapper  # accepted, not read (see the docstring)
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
