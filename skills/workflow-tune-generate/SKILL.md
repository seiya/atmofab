---
name: workflow-tune-generate
description: Use this when running the generate of the Tune stage and generating lowering-plan (`target_lowering_plan`) override candidates while keeping the `spec.ir.yaml` finalized in the core workflow and the target profile invariant. As an optional flow, it applies to the work of expanding trial candidates from the performance-exploration `tuning.spec`.
---

# Workflow Tune Generate

## Purpose
Fix the candidate-generation responsibility of the Tune stage, and create performance-exploration candidates under fixed physics conditions. This flow is an **optional flow** separated from the core workflow, and treats `spec.ir.yaml` and the target profile (`spec/targets/<target_id>.yaml`) as invariant premises.

## Scope
- the work of generating override variants of the CodegenBundle's `target_lowering_plan` from `tuning.spec`
- the work of generating exploration-space expansion candidates with `LLM` assistance

## Requirements
- Fix the `case` / `algorithm` / `io_contract` / `dependency` sections of `spec.ir.yaml`, and do not change the physics algorithm.
- Generate candidates by changing **only the lowering plan** (`target_lowering_plan`: `parallelization` / `data_layout` / `fusion` / `decomposition` / `communication` / `accelerator_mapping`). Changing `spec.ir.yaml` or the target profile (`hardware.*` / `toolchain.*` / `parallel.backend` / `execution.*` / `harness`) is forbidden (canonical boundary: "The override-allowed boundary" section of `docs/TUNING_WORKFLOW.md`).
- When `tuning.spec` includes an entry that overrides the IR or the target profile, do not launch Tune and stop with `fail_closed`.
- Prioritize safe knobs such as `tile`, `fuse`, `vectorize`, and `layout`.
- Keep `target_lowering_plan.parallelization` an object whose `model` member names the parallel model (`"none"` when nothing is parallelized). The `Generate.gate` parallel-directive floor reads the model from that member (and the `method` / `scheme` / `kind` spellings; `docs/workflow/CODEGEN_BUNDLE_CONTRACT.md` §Target lowering plan), and only a model naming `none` or another model exempts the variant from it — a model named under any other key declines nothing, so the target's OpenMP floor applies. A brand-new member inside an optional plan section is fine.
- When proposing a new implementation pattern, record the basis for adding it to the `search_space` of `tuning.spec`.
- When using the `LLM`, apply the `LLM` conventions of `SPEC.md` and output `<stage>_meta.json`.

## Operations Rules
1. Issue an `impl_hash` per candidate, and do not re-run a duplicate candidate. `impl_hash` is computed from the target profile and the final value of the candidate's `target_lowering_plan` (after the override; `docs/PERFORMANCE_DIAGNOSTICS.md` §3).
2. After candidate generation, save the variant lowering plan to a separate path, and run the same `Generate` / `Build` / `Validate` as the core workflow with the same `case` and the same target profile.
3. Make `debug_mode=false` the standard, and do not save failed-attempt artifacts.
4. On a candidate-generation failure, update `last_fail_reason` and hand it to verify.

## Decision Criteria
- All candidates satisfy the `case` / `algorithm` / `io_contract` / `dependency` fixed conditions of `spec.ir.yaml`.
- The candidate diff is limited to the lowering plan (`target_lowering_plan`).
- The metadata holds the information needed for re-execution.
