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
- The ranks form a process grid that the host, not the program, chooses: the harness reads it
  from the environment variable `ATMOFAB_PROCESS_GRID` (`p1,p2`) and arranges the ranks as
  `(ranks, 1)` when it is unset (`harness_fortran_cpu_mpi` §3.3, issue #512). The launch shape
  sets it from the profile's `execution.process_grid` (`tools/backends/parallel/mpi/
  execution.py`; `fortran_cpu_mpi` imposes `[2, 2]`), and sets nothing when the profile states
  none. The singleton `make test` re-run is handed `1,1`, so it runs on `(1, 1)` whatever the
  conductor's own environment carries (`host_execution.serial_execution_env`).
- Each grid of the profile's `execution.sweep.grids` is a further run of the same binary and
  cases (issue #512): `mpirun -n <p1 p2>` with `ATMOFAB_PROCESS_GRID=p1,p2`, after the quality
  check, each compared with the singleton re-run (`phase_04_validate.md` §4-2). A grid arranges
  at most `execution.ranks` ranks; `fortran_cpu_mpi` sweeps `[4, 1]`, `[1, 4]`, `[3, 1]`,
  `[1, 3]`, `[2, 1]`, `[1, 2]`.
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
| `trial_meta.json#environment.launch.env` | `{"ATMOFAB_PROCESS_GRID": "p1,p2"}` when the profile imposes a grid, `{}` otherwise |
| `perf.json#parallelism.process_grid` | `[p1, p2]`, the grid the harness arranged the ranks in; the post-execute gate requires the profile's `execution.process_grid`, or `[execution.ranks, 1]` when it imposes none (issue #512). The harness writes it from the grid it established; on the harness's own Validate run the gate also requires the self-test snapshots' `p1`, `p2` (the grid `__grid_dims` reports) to be the launch grid, so a perf record that echoes the imposed grid while the harness arranged its ranks another way is refused (`validate_pipeline_semantics._validate_harness_reported_grid`) |
| `quality_check.json#comparison.reference.ranks` / `.candidate.ranks` | `execution.ranks` / `1`; the post-execute gate requires both |
| `quality_check.json#shapes[]` | one row per execution shape, the certified run's first: its `ranks` and `process_grid`, and `observed_mpi_ranks` / `observed_process_grid`, what its harness wrote in `perf.json`; the post-execute gate requires the rows to be the profile's shapes and each observation to be its shape's (issue #512) |
| `trial_meta.json#environment.sweep[]` | each sweep shape's launch: `["mpirun", "-n", "<p1 p2>"]` and `{"ATMOFAB_PROCESS_GRID": "p1,p2"}` |
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
launch_shape` refuses it as the backstop. The reason:

- A batch site runs the whole job script as one task (`docs/backends/scheduler/slurm/
  JOB_SUBMIT.md`). Running `N` tasks would run the script `N` times, and a launcher inside it
  would start ranks within one task's allocation.
- A site with no scheduler would run the launcher in the job script as this host does, and is
  not yet asked what the local site is: whether its CPUs fit the ranks (`site_unfit_for_ranks`
  reads this process's affinity mask), and which runtime it has. Issue #337 is that work.

Issue #316 gave a second reason, and issue #333 removed it. `Build` used to run on this host,
and the binary, linked against this host's runtime installation, was shipped to the site. Since
issue #333 the binary is built at the site that runs it (with the site's wrapper), and runs only
there.

## 5. Out of scope

- More than one thread per rank, and a hybrid model with threads inside each rank: the
  backend's launch environment refuses a thread count other than one, and the launch gate asks it
  of `execution.threads_per_rank` and of each `execution.sweep.threads_per_rank`
  (`target_profile._thread_count_violations`).
- The presence floor of `mpi` in `Generate.static` (`parallel_directives`), and the
  distributed-state harness a domain-decomposed kernel calls. Both belong to later stages of
  issue #316.
