# Migration of SCALE onto this framework

Status: **proposed**. The operator decisions below were recorded on 2026-10-31. Nothing is implemented, and the delivery order has not been approved. Measured by reading `scale-met/scale-dev` at `dc58155` (SCALE 5.5.5) and this repository at `50cf603a`. No part of the migration has been run.

## Purpose

This note states what migrating SCALE requires of this framework: the operator decisions that bound the migration, what the framework lacks, and the order in which the gaps are closed. `TODO.md` carries the open-work pointer to this note.

## Scope

Migration means re-specifying the SCALE schemes as `spec` nodes and regenerating the code. The existing source is material for `spec` authoring and the reference for comparison. It is not input to `Generate`.

The migration covers `scalelib`, `scale-rm`, `scale-gm` and `contrib`, and then LETKF.

## Operator decisions

| topic | decision |
|---|---|
| GPU | The implementation optimal for each target environment is chosen, and OpenACC is expected not to be among the choices. A target that needs OpenACC reopens this row. |
| `contrib` | Rewritten as `spec` nodes, except JMA PP. |
| JMA PP | `contrib/JMA` holds only a `README`; the library `libpp.a` comes from outside the tree. A `spec` implements a wrapper that calls the external Fortran library. |
| `scale-gm` | Migrated. |
| LETKF | Included, after the main body. |
| Distribution | Generated output is distributed, from a repository other than this one. |
| Repository focus | This repository holds the framework and sample `spec` nodes. The SCALE `spec` nodes are expected to live in a separate repository; this row is the operator's current inclination rather than a final decision. |
| Validation | Comparison with the existing code as a test oracle is one form of validation. |
| Conversion | Converting existing code into a `spec` is a feature of this repository. |

Two of these amend recorded premises, and the change that implements each amends the text it contradicts: the oracle and the conversion against "Humans author only ... no human reference implementations" (`zero_base_architecture.md` premise 2), and the conversion against the manual `Spec` phase (`docs/workflow/phases/phase_00_spec.md`).

## Measured size

| item | SCALE |
|---|---|
| Fortran sources (`.F90` / `.f90`) | `scalelib` 261 files, 258,797 lines; `scale-rm` 151, 110,994; `scale-gm` 91, 58,486; `contrib` 44, 104,223 |
| largest files | `contrib/AMPS/mod_amps_core.F90` 28,332 lines; `scale_atmos_phy_mp_sn14.F90` 8,169 |
| directive and call lines | `!$acc` 7,780 (166 files), `!$omp` 6,916, `MPI_` calls 985, `nf90_` 309, `namelist` 454 |
| case tests | 260 `Makefile`s; 4 `evaluate` directories; 157 `visualize` directories |

| item | this repository |
|---|---|
| `spec` nodes | 22, all in the `dynamics` and `infra` domains |
| `controlled_spec.md` + `tests.md` per node | median 27,756 B, maximum 142,776 B |
| component or harness certification (`claude`, `fortran_cpu`, closure of `shallow_water2d_channel`, 2026-10-30/31) | mean 877 s of leaf time, $5.84 |
| `shallow_water2d_channel` 0.2.5 | 4,561 s, $18.96, `spec.ir.yaml` 861,861 B, generated source 4,311 lines |

Arithmetic on these figures, assuming one node per source file and a first-pass certification on one target: 547 nodes (the four trees above), about $3,200, about 133 hours of leaf time, and about 15 MB of `spec` text. This is a lower bound for planning and was not measured.

## Requirements the framework does not meet

| # | SCALE requires | current state |
|---|---|---|
| R1 | A driver with a time loop, per-process call intervals, owned state and submodel coupling | No `spec_kind` expresses a whole model. The driver is a future node (`docs/SPEC.md` §Design Policy; issue #175). |
| R2 | Run-time configuration and scheme selection | The runner's input is `--cases`, and case values are inline in the IR (`docs/workflow/RUNNER_OUTPUT_CONTRACT.md`, `docs/workflow/phases/phase_01_compile.md`). |
| R3 | External libraries, JMA PP and the I/O libraries among them | The link line holds objects only (`tools/backends/build_system/make/control_file.py`). A dependency must be a registered node (`docs/SPEC.md`). |
| R4 | Input data files, history and restart output | A bundle holds source files only (`docs/workflow/CODEGEN_BUNDLE_CONTRACT.md`). The runner writes JSON, with one state snapshot per case (`RUNNER_OUTPUT_CONTRACT.md`). |
| R5 | Threads inside MPI ranks, MPI with a device backend, several nodes, long runs | `parallel.backend` holds one value. MPI refuses more than one thread per rank (`tools/backends/parallel/mpi/execution.py`). The MPI wrapper runs one compiler and refuses a language compiled with another (`tools/target_profile.py`). A launcher target runs at the `local` site only (`docs/backends/parallel/mpi/LAUNCHER.md` §4; issue #337 covers `scheduler: none` only). A remote job runs as one task. A run is bounded by `RUN_PROGRAM_TIMEOUT_SEC` = 3600 s (`tools/build_runtime.py`). |
| R6 | Comparison with the existing code | No test compares with a stored or externally produced dataset. Bands may be calibrated offline against an independent implementation (`docs/TESTS.md`). `regression` is a comparison with the previously certified version of the same node (`workflow_scaling_redesign.md`), which is a different comparison. Statistical and ensemble metrics are future work (`docs/GLOSSARY.md`). |
| R7 | Conversion from code to `spec` | No flow exists. `skills/spec-input-check` checks an authored input only. |
| R8 | A `spec` root separate from the framework | `--repo-root` names one root for both. `spec/` paths appear on about 143 lines in 17 modules under `tools/` (a regular-expression count). The placement of target profiles and harness `spec` nodes is undefined. |
| R9 | Releases and performance tuning for distribution | `Tune` and `Promote` are reserved and not designed (`docs/workflow/WORKFLOW_CORE.md`). The generated output is a runner under the harness, with no executable for an end user. |

### Backends on existing axes

- `compiler`: `gfortran` and `nvcc` are implemented. The Fujitsu, NVIDIA Fortran and Intel compilers that `sysdep/` names are not.
- Compiler flags: `Makefile` flags are derived from `toolchain.standard` and the parallel backend. No target profile or site field reaches them, and `docs/TUNING_WORKFLOW.md` excludes `toolchain.*` from the knobs.
- `scheduler`: `none` and `slurm` are implemented, and `pjsub` is not. Whether the Fujitsu job manager satisfies the foreground, one-task contract of `job_submit` is unverified.

### Expressiveness of `spec`, IR and harness

Limits of the framework:
- A signature entity has no optional form (`tools/structured_signatures.py`).
- No rule covers a setup called once followed by repeated runs, or module state in a model.
- A capture has rank 1 to 4 (`tools/runner_ir.py`), and `shape_expr` admits no arithmetic.
- Bound case state is float64 (`docs/workflow/CHECKS_MODULE_CONTRACT.md`), and no single-precision capture path was found. SCALE builds in single precision on request.

Content of the current harness `spec` nodes, to be extended by `infrastructure` authoring. No rule forbidding the extension was found, and whether the workflow generates it is unverified:
- Halo exchange exists for ranks 1 and 2, with the partitioned axis 1 or 2. SCALE arrays are `(KA,IA,JA[,QA])`.
- One communicator and a two-dimensional process grid, with state gathered onto rank 0. Icosahedral grids, nesting and communicator splitting for bulk jobs are absent.
- `harness_cpp_gpu` provides no `distributed_state`.

### Existing capability the migration uses

Several operations per `component`; signature rank up to 15; procedure and derived arguments; a two-dimensional MPI decomposition with its sweep; the OpenMP backend; building at the execution site; dependency closure with `--jobs`; re-derivation on a dependency change. `CPP` and `ERB` are not required of generated code. A `domain` value is not constrained by code (`docs/GLOSSARY.md` calls it a fixed vocabulary, which is inconsistent).

## Constraints on the new features

- **Oracle comparison establishes equivalence, not correctness.** A `spec` converted from the code and an oracle running the same code agree on the code's defects. `tests.md` keeps checks independent of the oracle: analytic solutions, convergence orders and conservation. This follows `docs/PHYSICAL_VALIDATION.md` and is not replaced by the comparison.
- **An intended difference is recorded.** A generated result that differs from the existing code on purpose carries a recorded deviation rather than a widened band.
- **External library wrapper.** The library's identity enters the derivation key, so replacing the library does not leave an old certification standing. The library location is site data. A site without the library records the node as not applicable rather than failed. Certification of a wrapper covers the call interface and agreement with the existing code; it does not cover the library's internals.
- **Declaring an external library serves R3 as a whole.** JMA PP and the I/O and numerical libraries use one mechanism.
- **LETKF is not designed yet.** The main-body design keeps communicator splitting and file input open for it. The submodule has not been read, and the division of `scalelib/src/data-assim` (9,351 lines, `scale_letkf.F90` among them) between the main body and LETKF is undefined.

## Known risks

- A single node already reaches leaf input limits. See the `codex` 1 MiB input cap in issue #534 (step 2) and issue #547. The largest SCALE files exceed the channel kernel's generated size.
- A change to a widely used `spec` re-derives its dependents. `scale_const` is named on 425 `use` lines and `scale_prc` on 522 in `scalelib/src`. Byte-identical output stops the propagation (`docs/RUNBOOK.md` §Updating a shared dependency spec).
- `scale-dev` remains under active development, and no procedure keeps a `spec` in step with it during the migration.
- `dc_utils`, `suzuki10`, `contrib/Gadgetry` and `contrib/mt` carry their own copyright notices. Their treatment under `spec` conversion is undefined and is settled before distribution.

## Proposed order

1. Separate the `spec` root from the framework (R8).
2. Pilot on the Kessler microphysics scheme: conversion, generation and oracle comparison (R6, R7). `scale_atmos_phy_mp_kessler.F90` has 558 lines and seven public procedures. Whether it passes the current framework is unverified. The pilot measures `spec` quality and the cost of one node.
3. External library declaration, file I/O and run-time configuration (R2, R3, R4).
4. Execution shapes, the target-site compilers and scheduler, and the harness extensions (R5, Backends).
5. The driver and the whole-model node (R1), designed from the pilot's result.
6. `scale-rm`.
7. `scale-gm`.
8. `contrib` and the JMA PP wrapper.
9. LETKF.
10. `Promote` and distribution from the separate repository (R9).

Each item receives its own plan and review before implementation.

## Not investigated

- The LETKF submodule.
- The behaviour of the Fujitsu job manager on the machine.
- Performance of generated code against the existing code.
