# Launcher and compiler wrapper — `mpi`

> **Audience: the operator who runs an `mpi` target, and the maintainer of `Validate.execute`
> and `Build`.** No leaf reads this document. It is the `mpi` binding of the `launcher` and
> `compiler_wrapper` capabilities (issue #316); the execute path is
> `docs/workflow/phases/phase_04_validate.md`, and the code is
> `tools/backends/parallel/mpi/launcher.py` and `tools/backends/parallel/mpi/wrapper.py`.

## 1. What runs

A binary built for the `mpi` parallel model is compiled and linked through the runtime's
compiler wrapper, and it runs under the runtime's launcher at `Validate.execute`:

```
mpif90 <the compiler's own flags> ...                               # Generate.gate syntax stage and Build
mpirun -n <ranks> <binary> --cases <spec.ir.yaml> <case_id>...      # Validate.execute, the run
make test                                                           # the quality check: the same binary, one process
```

- `<ranks>` is the target profile's `execution.ranks` (1 when the profile states none). The
  launcher is used for every run of an `mpi` target, a one-rank target included, so the run is
  under the launcher and the quality check is not, whatever the count.
- The quality check's `make test` re-run starts the binary without a launcher. An MPI program
  started that way runs as one process (a singleton), which makes the re-run the serial
  reference the run is compared against (`phase_04_validate.md` §4-2). The comparison records
  both counts (`quality_check.json#comparison.{reference,candidate}.ranks`).
- The build control file pins its compiler variable to `mpif90`, and the `Generate.gate` syntax
  stage of the compiler the wrapper runs (`WRAPPED_COMPILER`, `gfortran`) runs with `mpif90` at
  `argv[0]`. The compiler's adapter — its flags and its diagnostics — is unchanged. A target
  whose backend is `mpi` does not pin `toolchain.compiler`; the launch gate refuses the pin.
- `mpif90` is the wrapper name that Intel MPI, Open MPI and MPICH all install. `mpifort` is not
  installed by Intel MPI.

## 2. What is recorded

| record | value |
|---|---|
| `trial_meta.json#environment.launch.argv_prefix` | `["mpirun", "-n", "<ranks>"]`; the post-execute gate admits this prefix only (`host_execution.launch_argv_prefix`) |
| `trial_meta.json#environment.ranks` | the profile's `execution.ranks` |
| `trial_meta.json#environment.platform.parallel_runtime` | the first line of `mpirun --version`, `null` when it cannot be read; recorded, not interpreted |
| `perf.json#parallelism.mpi_ranks` | the process count the harness reports; the post-execute gate requires it to equal `execution.ranks` |
| `quality_check.json#comparison.reference.ranks` / `.candidate.ranks` | `execution.ranks` / `1`; the post-execute gate requires both |
| build key `toolchain.compiler_wrapper` / `toolchain.parallel_runtime` | `mpif90`, and the first line of `mpif90 -show`: the runtime installation the binary links against. A change of installation is another build |

## 3. Requirements on the host

- `mpif90` and `mpirun` resolve on `PATH`, from ONE installation (the same directory). A
  launcher of another installation starts the binary's processes without the runtime the binary
  was linked against, and each process runs alone as rank 0 of 1 with exit code 0 (measured
  2026-09-27 in both directions between Intel MPI 2021.10 and Open MPI 4.1.2, `-n 2`). The
  startup probe refuses the pair
  (`parallel_toolchain_unusable`; `host_prerequisites.parallel_toolchain_problems`).
- The wrapper compiles a `use mpi_f08` source with the target's compiler. The harness uses the
  `mpi_f08` binding. An installation whose `mpi_f08` module is built for another compiler cannot
  build it: Intel MPI 2021.10 ships its `mpi_f08` module for the Intel compilers only, and its
  gfortran module directory holds `mpi.mod` without `mpi_f08.mod` (measured 2026-09-27). The
  startup probe compiles `BINDING_CANARY_SOURCE` syntax-only with the resolved wrapper and
  refuses the run when it fails (`parallel_toolchain_unusable`), before anything is billed. Open
  MPI 4.1.2 built for gfortran passes it.
- `mpirun` is asked only of a run that executes the binary: the local site's launch probe
  (`missing_required_site_tools`, `host_execution.execution_executables`).
- The CPUs this process may run on (its affinity mask) are at least `execution.ranks`
  (`site_unfit_for_ranks`).

## 4. Sites

A target whose parallel backend declares `launcher` runs at the `local` site only. A run that
reaches `Validate` for such a target mapped to a remote site is refused at launch
(`target_profile_invalid`, from `execution_sites.site_violations`), and `host_execution.
launch_shape` refuses it as the backstop. The two reasons:

- `Build` runs on this host and the binary is shipped to the site. An MPI binary links against
  this host's runtime installation, which the site need not have; a site with another
  installation cannot load it.
- A batch site runs the whole job script as one task (`docs/backends/scheduler/slurm/
  JOB_SUBMIT.md`). Running `N` tasks would run the script `N` times, and a launcher inside it
  would start ranks within one task's allocation.

Running an `mpi` target at a remote site needs a remote `Build` (linking with the site's
wrapper) and a launcher placement for a batch scheduler. Both are outside issue #316.

## 5. Out of scope

- More than one thread per rank (`execution.threads_per_rank` is 1 on every profile), and a
  hybrid model with threads inside each rank.
- The presence floor of `mpi` in `Generate.static` (`parallel_directives`), and the
  distributed-state harness a domain-decomposed kernel calls. Both belong to later stages of
  issue #316.
