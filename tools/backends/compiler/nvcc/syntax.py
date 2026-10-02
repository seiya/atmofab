"""The `nvcc` syntax-only adapter the `Generate.gate` syntax stage runs (issue #289, R4-b PR-4).

What this module knows is how the CUDA compiler driver is asked for a syntax-only pass over CUDA
C++: `-Xcompiler -fsyntax-only -c` has the host compiler parse and stop, while the device side is
still compiled and discarded — measured on 13.4: the argv is accepted, a source whose kernel
calls a host function fails (front end), invalid inline PTX fails with exit 255 (device
assembler), and no object is left beside any source (`-odir` names the scratch
directory anyway, so a driver version that did write one would write it there). The target's
standard and GPU architecture reach it from the target profile; the language's facts (which
suffixes are sources, their order, what is promoted) are `tools/backends/language/cuda_cpp/syntax.py`.
The argv carries `-rdc=true`, as the build does (`docs/backends/language/cuda_cpp/BUNDLE_BINDING.md`
§4); under it a kernel over the static shared-memory limit passes here and fails at the device
link in Build (re-measured 2026-10-02, issue #380).

Stdlib only.
"""

from __future__ import annotations

#: The program the stage launches; also what the launch-time host probe looks for.
EXECUTABLE = "nvcc"

#: The language whose sources this adapter reads.
LANGUAGE = "cuda_cpp"

#: Its first line carrying a version number is recorded as the stage's `compiler_version` (the
#: server's `_syntax_compiler_version`: this driver prints its release on the fourth line).
VERSION_ARGV: tuple[str, ...] = (EXECUTABLE, "--version")

#: A translation unit valid under every C++ standard the driver accepts, with one kernel so the
#: device side is exercised too. Compiled with the stage's own argv, it separates a broken
#: invocation (a `toolchain.standard` or `hardware.architecture` the driver refuses) from broken
#: sources by the driver's own verdict.
CANARY_SOURCE = ("__global__ void atmofab_syntax_canary_kernel(int n) { (void)n; }\n"
                 "int atmofab_syntax_canary(void) { return 0; }\n")

#: The name the canary is staged under; its suffix is one the language's source rule accepts.
CANARY_FILENAME = "atmofab_syntax_canary.cu"

#: How this driver spells a standard, for the remedy an operator reads.
STANDARD_SPELLING_EXAMPLE = "`c++17`, not `17`"


def argv(*, standard: str, scratch_dir: str, openmp: bool, promotions: tuple[str, ...],
         architecture: str | None, sources: list[str]) -> list[str]:
    """The syntax-only command line.

    `architecture` is the target profile's `hardware.architecture`, passed as `-arch=`; `None`
    leaves the driver's default. `openmp` is ACCEPTED AND NOT READ: a CUDA target's parallel
    model is not OpenMP, and the argument exists for the adapters it applies to. The sources stay
    last."""
    del openmp  # accepted, not read (see the docstring)
    command = [EXECUTABLE, f"-std={standard}"]
    if architecture:
        command.append(f"-arch={architecture}")
    # `-rdc=true`: without it the device code generator refuses a kernel's call to a
    # `__host__ __device__` operation another file defines (`ptxas fatal : Unresolved extern
    # function`), even in syntax-only mode — measured on 13.4 (issue #380).
    command += ["-Xcompiler", "-fsyntax-only", *promotions, "-odir", scratch_dir, "-rdc=true",
                "-c"]
    return command + list(sources)
