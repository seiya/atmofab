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
| the mandatory `Generate.gate` syntax-only stage | the host that runs `Generate.gate` | the argv is `tools/backends/compiler/gfortran/syntax.py`; the flags and the staged source set are `docs/backends/language/fortran/GENERATE_RULES.md` §2 |
| the compiler the build control file pins (`FC`, the `fortran` control file's compiler variable, `tools/backends/language/fortran/control_file.py`) | the site that builds the binary (issue #333) | used when the target profile pins no `toolchain.compiler` and its parallel backend declares no compiler wrapper (`workflow_conductor.build_compiler`); the `mpi` backend's wrapper takes this role on `fortran_cpu_mpi` (`docs/backends/parallel/mpi/LAUNCHER.md` §3) |

A missing `gfortran` used to surface at `Generate.gate`: `run_syntax_check` reports a missing
compiler as `skipped`, and the conductor turns a skipped mandatory stage into a `fail_closed`. The
launch probe asks for it first.

## 2. Installation

Install it with the platform's own package manager, e.g. on Debian/Ubuntu:

```
sudo apt-get install gfortran
```

No version range is declared for it. The first line of `gfortran --version` is recorded as the
syntax stage's `compiler_version` (`VERSION_ARGV`).
