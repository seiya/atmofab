# Host toolchain — `gfortran`

> **Audience: the operator who sets up a host for a `fortran` target, and the maintainer of the
> launch-time host probe.** No leaf reads this document. It is the `gfortran` binding of the
> compiler role `docs/RUNBOOK.md` §0-1 states neutrally (`missing_required_host_tools`); the code
> is `tools/backends/compiler/gfortran/syntax.py`, and the probe that asks for the program is
> `tools/host_prerequisites.py`.

## 1. Roles

`gfortran` is the `fortran` language backend's `MANDATORY_SYNTAX_COMPILER` and its
`DEFAULT_COMPILER` (`tools/backends/language/fortran/bundle.py`, bound to one value), so on a
target whose `toolchain.language` is `fortran` it serves two roles:

| role | where it runs | binding |
|---|---|---|
| the mandatory `Generate.gate` syntax-only stage | the host that runs `Generate.gate` | the argv is `tools/backends/compiler/gfortran/syntax.py`; the flags and the staged source set are `docs/backends/language/fortran/GENERATE_RULES.md` §2; on `fortran_cpu_mpi` the `mpi` backend's wrapper runs in its place (argv[0], and its `--version` is the one recorded — `docs/backends/parallel/mpi/LAUNCHER.md` §3) |
| the compiler the build control file pins (`FC`, the `fortran` control file's compiler variable, `tools/backends/language/fortran/control_file.py`) | the site that builds the binary (issue #333) | used when the target profile pins no `toolchain.compiler` and its parallel backend declares no compiler wrapper (`workflow_conductor.build_compiler`); the `mpi` backend's wrapper takes this role on `fortran_cpu_mpi` (`docs/backends/parallel/mpi/LAUNCHER.md` §3) |

A missing `gfortran` used to surface at `Generate.gate`: `run_syntax_check` reports a missing
compiler as `skipped`, and the conductor turns a skipped mandatory stage into a `fail_closed`. The
launch probe asks for it first.

## 2. Checked build

`gfortran` declares `checked_flags` ([issue #589](https://github.com/seiya/spec2science/issues/589)
PR-0): `CHECKED_FLAGS = ("-fcheck=bounds",)` in `tools/backends/compiler/gfortran/checked_flags.py`.
`Build` compiles the certified sources a second time with the `fortran` control file's own
`FFLAGS` string followed by it ([CONTROL_FILE.md](../../build_system/make/CONTROL_FILE.md) §1),
and `Validate.execute` runs that binary as the quality check's `checked` row
(`docs/workflow/phases/phase_04_validate.md` §4-2). On `fortran_cpu_mpi` the build runs through
the `mpi` backend's wrapper, which runs this compiler, so the same flag applies there.

An index outside an array's declared bounds then aborts the run with the runtime's diagnostic,
for example `Fortran runtime error: Index '0' of dimension 1 of array 'u_out' below lower bound
of 1`. Measured at `dfce11ce` (issue #589 Plan A §12 B-C): the 17 certified `fortran_cpu` physics
nodes and the certified harness self-test pass under it (captures byte-identical on the case
compared), the channel's suite in 572.4 s against 441 s (+30%), `shallow_water2d` 20.9 s against
15 s, every other node at most 1.5 s; the four out-of-bounds mutants of issue #588 abort. Re-run
at `314352d4` with this repository's own composition of the variables: the 17 nodes pass, the
four mutants abort with the runtime's bounds diagnostic, and the `fortran_cpu_mpi` harness
self-test builds through the `mpi` wrapper (Open MPI) and runs bare with captures identical to an
unchecked build's.

What the flag does not bound, so the row cannot see:

- a write through a pointer, or through `transfer`, that the dummy declarations do not bound;
- a write through an assumed-size dummy (`u(*)`) past the actual argument's extent — the
  dummy carries no upper bound to check (measured: exit 0, no diagnostic, issue #589 round 2);
- a local array allocated larger than it needs and indexed inside its allocation;
- sequence association of an array section to an explicit-shape dummy of another extent inside
  a kernel's internal helper (no diagnostic, measured; the published dummies' extents are pinned
  by the §5.1 signature gate's `dims`);
- a model procedure declared `bind(c)` under the name of the runtime's error entry, which
  shadows the diagnostic itself (measured to crash or abort either way — `run_ok: false` — by
  undefined behaviour, not by a gate; this language has no counterpart of the `extern "C"`
  refusal the `cuda_cpp` source gate makes).

## 3. External libraries

`gfortran` declares `library_flags` and `library_binary` ([issue #644](https://github.com/seiya/spec2science/issues/644)
Part 3 P1; `tools/backends/compiler/gfortran/library_flags.py`, `library_binary.py`). This is
their binding; the neutral rule is `docs/ORCHESTRATION.md` §Execution sites, and the reason both
sit on the `compiler` axis is `docs/BACKEND_BOUNDARY.md`.

- **Include flags.** One `-I<dir>` per include directory, in closure order. A declared module
  `<module>` resolves to `<include>/<module, lower case>.mod`; when the site entry names no
  `include`, a declared module resolves to nothing (`site_library_unresolved`).
- **Link words.** Each link name resolves to `lib<name>.so` in the site's `lib` directory, else
  `lib<name>.a` there (the shared object first, as the linker's own `-l` search takes it). With no
  `lib` directory, `gfortran -print-file-name=<file>` is asked for the same two names; an answer
  that is not an absolute path to a file is no answer. The link words are the resolved files BY
  PATH, in link order — never `-L<dir> -l<name>`: under the MPI compiler wrapper a `-L` of a
  system directory pulled a second MPI into the binary, and by path the file linked is the file
  the build key hashed — followed by `-Wl,-rpath-link,<dir>` for each library directory, so a
  declared library's own shared dependencies in the same directory are found at link time
  without the inherited `LD_LIBRARY_PATH`. Nothing is recorded in the binary: no `RPATH` or
  `RUNPATH` is linked.
- **The load name.** A shared object's `SONAME` is read from `objdump -p`; the file of that name
  beside the link file is the file the loader takes, and the build key hashes it. An archive
  has none.
- **The run.** The loader variable is `LD_LIBRARY_PATH`. Its value for a run is the library
  directories, `:`-joined, followed by `:` and the inherited value when that is non-empty; no
  separator is written before an empty inherited value, because the loader reads an empty
  element as the current directory. The value is prefixed, never replaced: replacing it drops the
  operator's MPI and compiler runtime directories. An `RPATH` is not used because the MPI
  compiler wrapper links with `--enable-new-dtags`, which turns it into a `RUNPATH` searched after
  `LD_LIBRARY_PATH` (`docs/design/scale_migration.md`).
- **The symbol reads** (the post-build checks, `docs/workflow/phases/phase_03_build.md`): an
  object's imports are `nm -P -u`, its definitions `nm -P -g --defined-only`; a library's exports
  are `nm -P -g --defined-only` over an archive (an archive is recognised by its `!<arch>` magic)
  and `nm -P -D --defined-only` over a shared object. `library_binary.EXECUTABLES` is
  `("nm", "objdump")`; the host refuses a run whose closure declares a
  library on a host without them (`docs/RUNBOOK.md` §0-1).

## 4. Installation

Install it with the platform's own package manager, e.g. on Debian/Ubuntu:

```
sudo apt-get install gfortran
```

No version range is declared for it. Where it runs the syntax stage itself, the first line of
`gfortran --version` is recorded as the stage's `compiler_version` (`VERSION_ARGV`).
