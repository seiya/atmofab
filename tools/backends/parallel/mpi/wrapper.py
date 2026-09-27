"""The `compiler_wrapper` capability of the MPI parallel backend (issue #316, R4-c).

An MPI program is compiled and linked through the implementation's compiler wrapper, which adds
the module path of the runtime's language binding and the libraries to link. `mpif90` is the
wrapper name the three implementations this backend has been read against all install (Intel MPI,
Open MPI, MPICH); `mpifort` is absent from Intel MPI. The wrapper runs the compiler the
`compiler` axis names; that axis' adapter (its flags, its diagnostics) is unchanged, and only the
program at `argv[0]` is this one.

The language binding this backend's harness uses is the one the MPI standard recommends for new
Fortran code, `mpi_f08`. An installation whose wrapper cannot compile a `use mpi_f08` source
cannot build the harness, and it is not rare: Intel MPI 2021.10 ships no gfortran `mpi_f08`
module (its gfortran module directory holds `mpi.mod` alone of the two), and its wrapper then
reads the other compiler's module file and fails (measured 2026-09-27, issue #316). The host asks
it at startup with `BINDING_CANARY_SOURCE` (`host_prerequisites.mpi_toolchain_problems`), before
anything is billed.

`SHOW_ARGV`'s output — the compile-and-link command the wrapper would run — identifies the runtime
installation a binary links against, which neither the compiler's version nor the launcher's
does; the build key records it (`orchestration_runtime._target_toolchain_identity`).

Stdlib only.
"""

from __future__ import annotations

from collections.abc import Sequence

#: The program that stands in for the target's compiler.
COMPILER_WRAPPER = "mpif90"

#: The `compiler` axis value the wrapper runs: the syntax stage of THAT compiler is run through
#: the wrapper (`build_runtime_server.tool_run_syntax_check`), and a stage of any other compiler
#: is run as its adapter spells it.
WRAPPED_COMPILER = "gfortran"

#: The argv whose first line is the command the wrapper runs, naming the runtime installation.
SHOW_ARGV: tuple[str, ...] = (COMPILER_WRAPPER, "-show")


#: A source the wrapper must compile, syntax-only, for this backend's harness to build: the
#: binding the harness uses, the procedures and handles it calls through that binding.
BINDING_CANARY_SOURCE = (
    "module atmofab_mpi_binding_canary\n"
    "  use mpi_f08, only: MPI_Comm, MPI_COMM_WORLD, MPI_Init, MPI_Finalize\n"
    "  implicit none\n"
    "  private\n"
    "  type(MPI_Comm), public :: atmofab_mpi_canary_comm\n"
    "end module atmofab_mpi_binding_canary\n"
)

#: The file name the canary is compiled under.
BINDING_CANARY_FILENAME = "atmofab_mpi_binding_canary.f90"

#: The argv, after the wrapper, that compiles the canary syntax-only in a scratch directory
#: (`{source}` and `{scratch}` are filled in by the caller). The module files the front end
#: writes go to the scratch directory.
BINDING_CANARY_ARGV: tuple[str, ...] = ("-fsyntax-only", "-J", "{scratch}", "{source}")


def wrap(argv: Sequence[str]) -> tuple[str, ...]:
    """`argv` with the compiler at `argv[0]` replaced by the wrapper."""
    if not argv:
        raise ValueError("wrap: an empty argv has no compiler to replace")
    return (COMPILER_WRAPPER, *argv[1:])
