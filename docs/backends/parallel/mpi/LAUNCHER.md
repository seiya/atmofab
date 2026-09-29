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
| `trial_meta.json#environment.launch.argv_prefix` | `["mpirun", "-n", "<ranks>"]`; the post-execute gate admits this prefix only (`host_execution.launch_argv_prefix`), and refuses an empty or absent one, which no run of the target has |
| `trial_meta.json#environment.ranks` | the profile's `execution.ranks` |
| `trial_meta.json#environment.platform.parallel_runtime` | the first line of `mpirun --version`, `null` when it cannot be read; recorded, not interpreted |
| `perf.json#parallelism.mpi_ranks` | the process count the runner writes; the post-execute gate requires it to equal `execution.ranks`. It detects a launch that started independent one-process runs only when the runner writes the count the harness observes at run time, which the distributed runner a launcher target runs does (issue #316 PR-4) |
| `quality_check.json#comparison.reference.ranks` / `.candidate.ranks` | `execution.ranks` / `1`; the post-execute gate requires both |
| build key `toolchain.compiler_wrapper` / `toolchain.parallel_runtime` / `toolchain.compiler_version` | `mpif90`; the first line of `mpif90 -show`, the runtime installation the binary links against; and the compiler's version asked as `mpif90 --version`, which the wrapper passes to the compiler it is configured with. A change of installation or of that compiler is another build. The syntax stage's recorded `compiler_version` is asked the same way |

## 3. Requirements on the host

- `mpif90` and `mpirun` resolve on `PATH`, from ONE installation. A launcher of another
  installation starts the binary's processes without the runtime the binary was linked
  against, and each process runs alone as rank 0 of 1 with exit code 0 (measured 2026-09-27 in
  both directions between Intel MPI 2021.10 and Open MPI 4.1.2, `-n 2`). Where the two
  programs sit does not decide it — Debian's alternatives switch `mpirun` and the wrapper
  separately, both in `/usr/bin` — so the startup probe asks what the pair DOES: it builds
  `LAUNCH_CANARY_SOURCE` with `mpif90`, starts it with `mpirun -n 2`, and refuses the run
  (`parallel_toolchain_unusable`) unless both processes report a run of 2
  (`host_prerequisites.parallel_toolchain_problems`). Asked only of a run that starts the
  binary under the launcher with more than one rank — one that reaches `Validate`, or drives a
  dependency there (`--with-deps`, or the `--resume` of such a run, unless it stops at
  `Compile`), for a profile whose
  `execution.ranks` is above 1 — and when `mpirun` resolves: a one-rank run is one process
  whichever installation starts it. A launch that does not complete (too few slots, a user
  the launcher will not run as, a hang) is refused with the head of the launcher's own
  message, not as a pairing problem; for too few slots, give the launcher slots for
  `execution.ranks` processes (its host file) or lower `execution.ranks`.
- The wrapper compiles a `use mpi_f08` source with the target's compiler. The harness uses the
  `mpi_f08` binding. An installation whose `mpi_f08` module is built for another compiler cannot
  build it: Intel MPI 2021.10 ships its `mpi_f08` module for the Intel compilers only, and its
  gfortran module directory holds `mpi.mod` without `mpi_f08.mod` (measured 2026-09-27). The
  startup probe compiles `BINDING_CANARY_SOURCE` syntax-only with the resolved wrapper and
  refuses the run when it fails (`parallel_toolchain_unusable`), before anything is billed. Open
  MPI 4.1.2 built for gfortran passes it. The binding canary is asked first; the launch canary
  is written in the same binding.
- `mpirun` is asked only of a run that executes the binary: the local site's launch probe
  (`missing_required_site_tools`, `host_execution.execution_executables`).
- The CPUs this process may run on (its affinity mask) are at least `execution.ranks`
  (`site_unfit_for_ranks`). The mask counts logical CPUs. A launcher may count its slots
  differently (Open MPI's default is one slot per physical core), so on a host with more than
  one hardware thread per core a count that passes this check can still be refused by the
  launcher at `Validate.execute`. Not measured: the host this was written on has one thread per
  core.

## 4. Sites

A target whose parallel backend declares `launcher` runs at the `local` site only. A run that
reaches `Build` for such a target mapped to a remote site is refused at launch
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
