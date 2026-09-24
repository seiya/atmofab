# Target Profile and Lowering Plan

## Position
Implementation discretion (B) is held in two places, and neither is `spec.ir.yaml`.

- The **target profile** (`spec/targets/<target_id>.yaml`, `docs/GLOSSARY.md` §1 `target profile`) fixes what one run builds for: the hardware, the toolchain, the parallel backend, the execution shape, and the harness. The operator authors it; no leaf chooses or edits it. The run selects it with `tools/run_workflow.py --target <target_id>`, and the launch gate (`tools/target_profile.py:target_profile_violations`) refuses it before anything runs when an axis value is not one this repository implements or when its harness does not resolve.
- The **lowering plan** (the CodegenBundle's `target_lowering_plan`, `docs/workflow/CODEGEN_BUNDLE_CONTRACT.md`) states how the computation of one node is lowered onto that target: the parallelization model and the loops it covers, the schedule, the data layout, fusion, tiling, and vectorization. The `Generate` producer authors it together with the source.

`spec.ir.yaml` is target-free: its sections are `case` / `algorithm` / `io_contract` / `dependency` (plus `public_api` on a `component` or `infrastructure` node), and a Compile reply that carries an `impl_defaults` section is refused by the host. Until R4-a PR-3 (issue #284) the IR carried an `impl_defaults` section holding the toolchain and a knob layer; the toolchain moved to the target profile and the knob layer moved to the lowering plan.

In the core workflow the target profile is a fixed input of every stage from `Generate` onward, and the lowering plan is fixed once `Generate` certifies the bundle. Variant exploration of implementation discretion is the responsibility of the optional flow `Tune` (`docs/TUNING_WORKFLOW.md`), whose variants are over the lowering plan.

## Design Policy
- The target profile names each axis value as an opaque token. The host asks the backend registry (`tools/backends/registry.py`) about the token and does not interpret it (`docs/BACKEND_BOUNDARY.md`).
- The lowering plan expresses the intent of a lowering choice — which loops are parallel, which layout, which fusion — rather than compiler flags or the concrete spelling of a directive. The concrete spelling is the source's.
- The plan is the `Generate` producer's own declaration, and `Generate.verify` G6 holds the source to it (`docs/workflow/phases/phase_02_generate.md`).

## 1. The boundary of generalization
- Generalize (lowering plan): the intent of a loop transformation — parallel model and scope, schedule, chunk size, collapse, tiling, fusion, vectorization, the memory-layout policy, the async/overlap policy.
- Do not generalize: compiler-specific flags, GPU-architecture-specific details, the concrete way of writing a pragma or attribute. These are not recorded in the target profile or in the plan; the source and the host-authored build control file carry them.

## 2. Required items (target profile)
A target profile requires the following fields. `spec/schema/targets/target_profile.schema.json` is a declarative copy of the shape; the canonical validator is `tools/target_profile.py:load_target_profile`.

- `hardware.class` (`cpu` / `gpu`).
- `hardware.architecture` (e.g. `x86_64`).
- `toolchain.language` (the `language` axis value).
- `toolchain.standard` (the language standard spelled the way the compiler names it; it is passed verbatim to the compiler driver, so an elided spelling is rejected by the driver).
- `toolchain.build_system` (the `build_system` axis value).
- `parallel.backend` (the `parallel` axis value).
- `execution.threads_per_rank` (the loader accepts only `1`; `docs/GLOSSARY.md` §1 `target profile`).
- `harness` (`infrastructure_id`, `version_constraint`): the `infrastructure` node every non-infrastructure node built for this target runs over. The host resolves it to the highest catalog version that satisfies the constraint (`tools/target_profile.py:harness_node_key_for_target`).

Rules:
- The toolchain is fixed by the target profile, not by `Compile`. `Compile` reads no toolchain and produces the same IR for every target.
- The launch gate requires every axis value to be implemented, and requires the toolchain to be servable for the node's kind: an `infrastructure` node needs an executable build system; every other kind also needs the host to author the build control file and to render the runner (`tools/target_profile.py:toolchain_servable_reasons`). A profile that fails is refused at launch as `target_profile_invalid`.
- An `infrastructure` node run for a target must be that target's harness; any other `infrastructure` node is refused at launch as `target_harness_mismatch`.
- Adding another toolchain is a repository-level change (a `backend` package that implements the missing capabilities, `docs/BACKEND_BOUNDARY.md`), not a per-spec decision.

## 3. Optional items
### Target profile
- `toolchain.compiler` / `toolchain.linker` are optional pins.
- State them only to fix the compiler or linker (for reproducibility). When absent, the execution environment's default is used.
- When `toolchain.compiler` is set, the launch gate requires it to be an implemented `compiler` axis value, and the host-authored build control file uses it as the build compiler. The deterministic `Generate.gate` syntax check always runs against `toolchain.standard` regardless of the build compiler.
- Calling a compiler directly for a one-off build is forbidden; every build runs through `toolchain.build_system` (`AGENTS.md` §MCP execution rules).

### Lowering plan
The remaining implementation choices are the bundle's `target_lowering_plan`, authored by the `Generate` producer. `precision` and `state_residency` are required members; `data_layout`, `parallelization`, `decomposition`, `communication`, `accelerator_mapping`, and `fusion` are optional (`tools/codegen_bundle.py:LOWERING_PLAN_REQUIRED_KEYS` / `LOWERING_PLAN_OPTIONAL_KEYS`).

- `parallelization` is an object whose `model` member names the parallel model, and `"none"` when nothing is parallelized. Its other members state which loops the model covers and with what schedule, chunk size, and collapse.
- When the target's `hardware.class` is `cpu` and the user does not specify the loop parallelization method, the producer parallelizes the parallelizable loops with the target's `parallel.backend`.
- A user-specified parallelization method takes precedence. Forcing parallelization onto a loop that is not parallelizable is forbidden.
- On a `component` or `problem` node whose target has `hardware.class` `cpu`, `parallel.backend` `openmp`, and the language the floor is implemented for, and whose plan names OpenMP as its `parallelization` model, the `Generate.gate` static check fails a model source that contains counted loops and no parallel directive (`tools/validate_pipeline_semantics.py:_validate_openmp_presence_floor`). A plan that declares `none` over loops that are plainly parallelizable is a `Generate.verify` G6 finding.

## 4. Composition rules of the output (common across languages)
- Regardless of language, the generated code separates `model` (physics computation) and `runner` (input/output / judgment coordination).
- The `runner` calls the `model` via `call` / `use` / `import`.
- The physics-update logic must not be duplicated on the `runner` side.
