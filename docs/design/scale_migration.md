# Migration of SCALE onto this framework

Status: **proposed**. The operator decisions below were recorded on 2026-10-31 and 2026-11-01. The operator accepted item 1 of §Order and the retirement of the checks module in its item 3; each item still receives its own plan. Nothing is implemented, and no part of the migration has been run. Measured by reading `scale-met/scale-dev` at `dc58155` (SCALE 5.5.5) and this repository at `9383347a`, whose `tools/`, `spec/`, `releases/` and `skills/` are those of `50cf603a`.

## Purpose

This note states what migrating SCALE requires of this framework: the operator decisions that bound the migration, what the framework lacks, what dominates its cost at this scale, and the order of the work. `TODO.md` carries the open-work pointer to this note.

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
| Repository focus | This repository holds the framework and sample `spec` nodes. The SCALE `spec` nodes live in a separate repository. |
| Validation | Comparison with the existing code as a test oracle is one form of validation. |
| Conversion | Converting existing code into a `spec` is a feature of this repository. |
| Derivation key | The derivation key and the contract versions are redesigned as item 1 of §Order, under the constraints §Cost structure states, by [issue #574](https://github.com/seiya/atmofab/issues/574). |
| Leaf-authored checks | The checks module a leaf authors is retired, after the history output carries what it judges (§Cost structure). |

These amend recorded decisions, and the change that implements each amends the text it contradicts: the oracle and the conversion against "Humans author only ... no human reference implementations" (`zero_base_architecture.md` premise 2), the conversion against the manual `Spec` phase (`docs/workflow/phases/phase_00_spec.md`), and the derivation key against A1's key table and its rule that every downstream reference is a selected output hash, against "Prompt contracts as versioned inputs" (`zero_base_architecture.md` A1, A7), and against issue #250.

## Measured size

| item | SCALE |
|---|---|
| Fortran sources (`.F90` / `.f90`) | `scalelib` 261 files, 258,797 lines; `scale-rm` 151, 110,994; `scale-gm` 91, 58,486; `contrib` 44, 104,223 |
| largest files | `contrib/AMPS/mod_amps_core.F90` 28,332 lines; `scale_atmos_phy_mp_sn14.F90` 8,169 |
| lines in those sources, by a case-insensitive line regular expression | `^\s*!\$acc` 7,692 (175 files), `^\s*!\$omp` 6,766, `^\s*call\s+MPI_` 1,158, `^\s*namelist\s*/` 506, `nf90_` 1,099, of which 0 in `scalelib` |
| C sources (`.c`) in those trees | 7 files, 5,651 lines. The NetCDF layer of `scalelib` is `scalelib/src/file/scale_file_netcdf.c`. |
| case tests | 260 `Makefile`s; 4 `evaluate` directories; 157 `visualize` directories |

| item | this repository |
|---|---|
| `spec` nodes | 22, all in the `dynamics` and `infra` domains |
| `controlled_spec.md` + `tests.md` per node | median 27,756 B, maximum 142,776 B |
| component or harness certification (`claude`, `fortran_cpu`, closure of `shallow_water2d_channel`, 2026-10-30/31) | $5.84 mean; 877 s of leaf time by `analyze_timing.py`, 719 s from the leaf launch records |
| `shallow_water2d_channel` 0.2.5 | $18.96; 4,561 s by `analyze_timing.py`, 3,035 s from the leaf launch records; `spec.ir.yaml` 861,861 B; generated source 4,311 lines |

Arithmetic on these figures, assuming one node per source file and a first-pass certification on one target: 547 nodes (the four trees above), about $3,200, between 109 and 133 hours of leaf time, and about 15 MB of `spec` text. The two time methods have not been reconciled. This is a lower bound for planning and was not measured.

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
| R8 | A `spec` root separate from the framework | `--repo-root` names one root for both. §Structural layers states the separation and what it owes. |
| R9 | Releases and performance tuning for distribution | `Tune` and `Promote` are reserved and not designed (`docs/workflow/WORKFLOW_CORE.md`). The generated output is a runner under the harness, with no executable for an end user. |

### Backends on existing axes

- `compiler`: `gfortran` and `nvcc` are implemented. The Fujitsu, NVIDIA Fortran and Intel compilers that `sysdep/` names are not.
- Compiler flags: `Makefile` flags are derived from `toolchain.standard`, `parallel.backend` and, where the language reads it, `hardware.architecture`. No target profile or site field carries arbitrary flags, and `docs/TUNING_WORKFLOW.md` excludes `toolchain.*` from the knobs.
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
- `scale-dev` remains under active development, and no procedure keeps a `spec` in step with it during the migration.
- `dc_utils`, `suzuki10`, `contrib/Gadgetry` and `contrib/mt` carry their own copyright notices. Their treatment under `spec` conversion is undefined and is settled before distribution.

## Cost structure

Three properties of the framework dominate its cost at this scale, measured on `orch_20261030T225323Z_9d78424b` and its closure.

- **A change to a shared `spec` reached every consumer's `LLM` substeps, until issue #574.** The compile and generate keys hashed the certified outputs of every closure node, and a re-derived IR is rarely byte-identical (`docs/RUNBOOK.md` §Updating a shared dependency spec). Ten passing IRs of `shallow_water2d_channel` 0.2.5 hold 11 to 19 `algorithm.steps` and 482 to 765 `primary_predicates`. `use scale_const` occurs in 136 files of `scalelib/src`, and 172 depend on it transitively. The compile key hashed every closure node, so $172 \times \$5.84$ is about $1,000 per edit, by arithmetic.
- **A contract version bump re-certifies the corpus.** Commits that moved one of the seven constants `LEAF_PROMPT_CONTRACT_VERSION`, `PURE_PROMPT_CONTRACT_VERSION`, `COMPILE_INLINED_DOCUMENTS_VERSION`, `VERDICT_VERSION`, `EXECUTE_VERSION`, `RENDER_VERSION` and `DERIVATION_KEY_VERSION` under `tools/` since 2026-09-18 number 66, by `git log -G` on an assignment at the line start, and `docs/RUNBOOK.md` carries ten "One-time cost" paragraphs. One bump over 547 nodes is about $3,200, by arithmetic.
- **`LLM` work concentrates on the test representation.** In the channel IR, `primary_predicates` holds 671,170 B of 861,861 B. The generated model is 345 lines against 1,391 lines of checks. `Compile` takes 56% of the channel's cost, and 34% to 43% on the components and the harness.

Constraints on item 1 of §Order:
- `closure[]` is removed from a key only where its stated reason does not hold, decided separately for `Compile` and for `Generate`. The compile reviewer and the compile gate read `dependency_surface.json` rather than a dependency's IR, although the comment above `phase_derivation_inputs` states otherwise. The generate producer, the generate reviewer and the judge read `DEPENDENCIES.md`, which holds the published interfaces and the certified refs, rather than a dependency's source. The host-rendered runner is glue over the harness's certified source and IR.
- A producer's prompt contract influences which valid output an attempt produces, and it may become advisory (`zero_base_architecture.md` A1). A verify or judge template states what valid means, and it stays in the key, as do the fragments both kinds read, `COMPILE_INLINED_DOCUMENTS_VERSION` (`docs/workflow/phases/phase_01_compile.md` is seeded to both `Compile` leaves and its verification items define the verdict) and the severity slices. `zero_base_architecture.md` calls the judge an advisory reviewer; the judge stays in the key because `judge_semantic_review_fail` revokes a certification. One digest in `tools/tests/test_leaf_prompt_contract_drift.py` pins all of them today, so the split needs a digest per kind and a rule for the members both kinds read.
- A dependency's source change continues to reach a consumer's `Build` and `Validate`, whose keys are unchanged. `Validate.judge` therefore still runs per consumer, so the cost of an edit falls and does not reach zero. `Validate` catches a behaviour change under an unchanged surface only where the consumer's `tests.md` states the property.
- A bump of a reader's contract re-derives the whole phase, the producer included, unless verify becomes its own derivation. The plan decides that.

Constraint on retiring the checks module: the history output comes before the retirement, and the retirement waits until the history output carries everything the checks module judges. A test is not narrowed to fit the mechanism.

## Structural layers

A run of the framework reads three kinds of content, and one root holds all of them today. These three terms are this note's; they enter `docs/GLOSSARY.md` with the change that implements them.

| layer | content | location |
|---|---|---|
| Framework | `tools/`, the backends, `docs/`, prompt templates, `spec/schema`, operator skills | this repository |
| Project | `spec` nodes and the catalog, target profiles, harness `spec` nodes, the reference to the source being converted, comparison data, external library declarations, release settings and `releases/` | a separate repository for SCALE; inside this repository for the sample `spec` nodes |
| State | `workspace/`, `llm.yaml`, `sites.yaml` | machine-local, untracked |

Rules of the separation:
- One run reads one Project. No multi-root resolution exists.
- The sample `spec` nodes form a Project, and the framework's test suite runs them as an external Project, so the suite holds the boundary.
- A harness `spec` belongs to its Project. Duplication across Projects is accepted while two Projects exist, and the placement is reviewed when a third appears. Item 5 of §Order is what makes this rule implementable.

What the separation owes, measured on this tree:
- `repo_root` appears on 1,549 lines in 18 modules under `tools/`. A literal join `repo_root / "<dir>"`, with `REPO_ROOT` and `Path(repo_root)` as equivalent spellings, occurs 74 times: `workspace` 66, `spec` 7, `tools` 1.
- The root that State hangs from is undefined after the separation. `llm.yaml` and `sites.yaml` resolve against `--repo-root`, and `~/.atmofab/` holds further runtime state.
- `spec/schema` is read two ways: from `Path(__file__)` in `tools/codegen_bundle.py`, and from `repo_root` in `tools/run_workflow.py` and `tools/validate_pipeline_semantics.py`.
- A derivation key holds content hashes, node keys and the target's toolchain identity, and no path that depends on the tree. Measured: the four keys of `problem/shallow_water2d_channel@0.2.5` recompute to their stamps under another root and with the nodes under another directory. Moving the root invalidates no certification.
- Moving a node directory relative to the root does invalidate them. A certified IR's `meta.source_refs` are root-relative paths, and `_phase_certification` re-runs the compile validator, which resolves them (`ir_rejected_by_current_validator`). The separation therefore makes the Project root the root those paths are read against, and keeps `spec/<kind>/...` under it.
- `spec/registry`, `spec/targets` and `spec/schema` are fixed paths under the root. A recorded `spec_ref` is root-relative (`_canonicalize_spec_ref`, `tools/run_workflow.py`) and is joined back on `--resume`. `_validate_source_dependency_ref`, `record-launch` and `tools/validate_workspace_root.py` require the `spec/` prefix at every launch.
- A stamped `artifact_hashes` key is a `workspace/...` path re-hashed under the root, so certifications depend on where State hangs.
- 29 test files match `spec/(problem|component|profile|infrastructure|targets|registry)`, and 24 match `REPO_ROOT = Path(__file__)`.

## Order

Item 1 and the retirement of the checks module in item 3 are accepted by the operator. The remainder is proposed. Items 1 and 2 are independent and may proceed together. Array conventions, data layout and other generated-code optimization are out of this list; they are taken up once `Tune` and the system tier exist.

| # | item | requirements | content |
|---|---|---|---|
| 1 | Derivation key and contract versions | | Redesign under the constraints of §Cost structure, implemented by [issue #574](https://github.com/seiya/atmofab/issues/574). The compile key keeps `dependency_surface`, as §5.1 of each dependency's spec, and drops `closure[]`. The generate key's `closure[]` binds the published surface of every pipeline closure member, not a document hash: the rendered document carries ids and source-derived text, and a Generate output depends on the members beyond the direct dependencies. The build and validate keys are unchanged. Producer templates become advisory. Re-certifying the channel closure on `fortran_cpu` costs $59.83, the `cost_usd` of its 8 runs; the other closures and targets are unmeasured, and whether existing stamps are re-keyed or re-derived is the plan's decision. |
| 2 | Kessler measurement | | Take `scale_atmos_phy_mp_kessler.F90` (558 lines, seven public procedures) through the framework as a hand-written `component` of the adjustment and terminal-velocity physics, with the saturation adjustment and the saturation function as one `component` dependency, the constants as `module_parameters`, and the history, log, profiling and abort calls omitted and recorded as deviations. It yields the size of the `spec`, the node's cost and `Compile` share, the framework changes it needed, and an offline comparison with the existing code driven column-wise. The common-layer boundary is the static `use` closure, not a result of the run: 9 modules directly (11,974 lines) and 18 transitively (21,046 lines, `scale_file` 6,648 among them), `#include` headers excluded. |
| 3 | System tier and history output | R1, R2, R3, R4 | A tier above the node represents the assembled model and the distributed artifact, and states what certifies them: comparison with the existing code and properties of the history output. Run-time configuration, file I/O and external libraries take their shape here. The checks module is retired after the history output, as §Cost structure states. |
| 4 | Conversion and oracle comparison | R6, R7 | The existing code is declared as a pseudo-target whose `Generate` takes no leaf, read through the existing `comparand` grammar, subject to the Kessler result. `PHASE_ORDER` keeps its four phases; conversion, `Tune` and `Promote` are flows outside the core, and shared parts are extracted only as those flows need them. The reserved `tune` and `promote` branches of `tools/orchestration_runtime.py` are judged in that plan. |
| 5 | Project ownership of the harness | R8 | The framework stops fixing a harness name, version and interface (`HARNESS_CAPABILITY_MANIFESTS` in `tools/codegen_bundle.py`, `EXPECTED_HARNESS_SPEC_ID` and `_HARNESS_PINS` in `tools/backends/language/fortran/runner.py`). The shape follows the Kessler result, and the expressiveness limits above that it reports land here. |
| 6 | Execution model | R5, Backends | A submitted job is detached from the conductor, so a long batch job survives and resumes. A target profile expresses several parallel models together. The target-site compilers and scheduler land here. |
| 7 | Layer separation and repository split | R8 | Separate Framework, Project and State as §Structural layers states, and move the SCALE `spec` nodes out. |
| 8 | Migration of the code | | `scale-rm`, `scale-gm`, `contrib` and the JMA PP wrapper, and LETKF, in that order. `Promote` and `Tune` land with the distribution. |

Each item receives its own plan and review before implementation.

The Kessler result changes the order under these conditions:
- A share of leaf cost on the test representation (`Compile.generate`, `Compile.verify` and the checks-module part of `Generate.generate`) under 30% lowers the priority of retiring the checks module.
- A `FILE_HISTORY_in` call whose argument cannot become an output argument, because it is module state or an intermediate the caller cannot recompute, places item 3 before item 2. The Kessler kernel holds one `FILE_HISTORY_in` call, on a local array, so it does not meet this condition.
- `spec` authoring that takes more than one person-week places conversion before item 3.

## Not investigated

- The LETKF submodule.
- The behaviour of the Fujitsu job manager on the machine.
- The difference between the two leaf-time methods in §Measured size.
- Performance of generated code against the existing code.
