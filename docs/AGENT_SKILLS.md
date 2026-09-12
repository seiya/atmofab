# Agent Skills Mapping

This document defines the reference conventions for the `skills` used in the project.

> **No workflow leaf reads a `SKILL`.** Since Z4 ([issue #171](https://github.com/seiya/atmofab/issues/171))
> every LLM substep of the core workflow runs as a `pure-function leaf`: it holds no tools, reads
> nothing from disk, and receives its whole contract inlined in the launch prompt the host renders
> (`tools/prompt_templates/pure_*.txt`). The five core phase `SKILL`s are deleted with the agentic
> leaf that read them. What is left under `skills/` is the procedures an OPERATOR runs — the
> optional `Tune` / `Promote` flows, the audits, the spec input check, the timing audit — plus the
> reserved `Tune` / `Promote` surface. This document maps those, and records where each core
> phase's contract went.

## Purpose
- Use the same phase definitions on `Codex` / `Gemini` / `Claude Code`.

## Scope
- The conductor (`tools/workflow_conductor.py`) that drives the whole `workflow`
- The core workflow phases `Compile` / `Generate` / `Build` / `Validate`
- The launch template each core phase's leaf is rendered from, and the `skills/<skill_name>/SKILL.md` of each operator flow
- The SKILL of the optional flows `Tune` / `Promote` is handled separately from the core workflow.
- OUT OF SCOPE: the skills under `.claude/skills/`. Those are dev-only procedures for an operator's
  interactive session (how to review a change, how to change enforcement machinery); no phase maps to
  them, and no leaf can load them — a pure leaf runs under `--safe-mode`, which refuses every
  settings layer (`CLAUDE.md`).

## Requirements
- An OPERATOR running one of the flows below identifies the target phase, then reads the corresponding `SKILL.md`. A workflow leaf does not: the host renders its contract into its prompt.
- A phase that has `generate -> verify -> regenerate` has two contracts, one for `generate` and one for `verify`, applied separately — two pure templates for a core phase, two `SKILL`s for an operator flow.
- The workflow common invariants (the ban on referencing past artifacts, the `dummy` prohibition, verification-contract derivation, the `workspace/` root constraint, the `quality check` judgment axis) use `docs/workflow/WORKFLOW_CORE.md` as the canonical source. The detailed contract of each `phase` uses the files under `docs/workflow/phases/` as the canonical source. The entry point to the specification is `docs/WORKFLOW.md`.
- The execution contract of the agent hierarchy (`orchestration -> step` and `orchestration -> substep`) uses `ORCHESTRATION.md` as the canonical source.
- There is no common child-agent contract document any more. `docs/AGENT_CONTRACT.md` was the single common leaf must-read and is deleted with the agentic leaf: everything it carried that was about holding a tool (capability_token usage, read/write permission guards, the `Write` / `Edit` procedure, tmp-area rules, the no-inline-write constraints) has no subject for a leaf that holds none, and the two parts that were not — the leaf-actionable workflow invariants and the `<stage>_meta.json` key rules — are `docs/workflow/WORKFLOW_CORE.md` §"Workflow common invariants" and §"Stage meta keys", where the host applies them.
- The runner program-output contract (`diagnostics.json` / `perf.json` / `raw/` composition + the Fortran JSON-descriptor rules) is canonical in `docs/workflow/RUNNER_OUTPUT_CONTRACT.md`. It reaches `Validate.judge` as an INLINED slice under the pure judge (§1 and §3 — what `diagnostics.json` must carry and what `raw/` must hold; issue #169) and it reaches the pure `Generate` leaves of an `infrastructure` harness self-test — the only node kind that authors its own runner — INLINED WHOLE, as `runner_output_contract_document` of the `harness` bundle shape's producer and reviewer templates (issue #169). It stays the force-read must-read for a residual agentic judge and for a residual agentic runner-authoring `Generate` leaf. **M3d node-aware:** it is **not** injected for an *M3c physics* `Generate` leaf — that leaf authors only `<spec_id>_model.f90` + `<spec_id>_checks.f90` (its runner is host-rendered glue over the certified harness), so its runner-side contract is `CHECKS_MODULE_CONTRACT.md`, not this doc (see `leaf_contract_doc_refs`). It supersedes the runner-output prose previously duplicated in `phase_02_generate.md` / `phase_04_validate.md` / `PERFORMANCE_DIAGNOSTICS.md`; new runner-output rules go there, and those docs reference it.
- The checks-module contract `docs/workflow/CHECKS_MODULE_CONTRACT.md` is force-read by **every residual agentic** `Generate` leaf (`generate` and `verify`), not only an M3c one — and since issue #169 no in-tree leaf is one, so each half of it now reaches its leaf inlined instead (below). Its §1-4 are the fixed checks-module ABI an *M3c physics* leaf authors `<spec_id>_checks.f90` against; its §5 (Fortran legality and gate guards — the idioms the deterministic `Generate.gate` lint and syntax checks enforce, such as the `associate` binding of an intentionally-unused dummy argument) binds **every leaf-authored Fortran source of any node**, including the hand-authored runner of an `infrastructure` self-test. `Validate.judge` never sees the checks source, so it is not injected there — and since Z3 the judge is a `pure` leaf with no force-read set at all, so this is a statement about the residual agentic path. Force-reading is the **agentic** leaf's route, and since issue #169 NO in-tree `Generate` leaf takes it, so each half of the document now reaches its leaf INLINED instead: a `pure` M3c producer is told the ten ABI names and shown the host-rendered runner; since `pure-24` a `pure` `Generate.verify` reviewer on the `m3c` shape receives §1-4 as `checks_module_contract_document` (issue #142) — the `harness` shape's reviewer does not, having no checks module to review, and receives the runner-output contract in its place; and since `pure-37` a `pure` producer on the `harness` shape — the runner-authoring leaf §5 names — receives §5 as `gate_guards_document`, because a `pure` leaf force-reads nothing and would otherwise be held to that rule set with no document stating it (issue #169). Since `pure-25` the same reviewer also receives `docs/workflow/phases/phase_02_generate.md` §2-2's severity rubric inlined as `severity_rubric_document`, which is the sole rule for the `issue_severity` its verdict carries (issue #143).
- **Where a leaf-actionable rule goes now that nothing is force-read** (the minimization this supersedes: `docs/design/leaf_must_read_restructure.md`). A rule reaches a leaf only by being in its launch template, or in a document the host INLINES into that template — `Conductor._build_pure_*_context` is the complete list of what is inlined, and `docs/workflow/LAUNCH_PROMPT_REFERENCE.md` describes the shapes. So: put a one-substep rule in that substep's `pure_*.txt` template; a rule binding both compile leaves in `phase_01_compile.md` (inlined whole); a runner-output rule in `RUNNER_OUTPUT_CONTRACT.md` (inlined whole for the `harness` shape, sliced for the judge); a rule binding every `Generate` leaf in `CHECKS_MODULE_CONTRACT.md` (§1-4 the checks ABI, §5 the Fortran legality / gate idioms). A rule written anywhere else reaches no leaf at all, silently — which is the failure mode this bullet exists to prevent, unchanged in kind from when the list was a must-read set.
- The overall policy and `spec` management requirements (`spec_kind` / registry / official-version placement / naming rules) use `SPEC.md` as the canonical source.
- `Build` and `Validate.execute` are non-LLM steps the **conductor runs in-process** (no leaf agent, no `SKILL.md`); their contract is `docs/workflow/phases/phase_03_build.md` / `phase_04_validate.md`. They still go through the `MCP` server (`compile_project` / `run_program` / `run_quality_checks`) and apply the `MCP execution rules` of `AGENTS.md`.
- Each phase must not drop the required outputs defined in the corresponding `SKILL.md` (e.g. `ir_meta.json`, `source_meta.json`, `binary_meta.json`, `verdict.json`, `validate_meta.json`).
- Write into `SKILL.md` the execution procedure and the procedures specific to that `SKILL`, and do not duplicate the `phase`'s I/O contract / artifact format / numerical canonical requirements in a form that contradicts `docs/workflow/WORKFLOW_CORE.md` or `docs/workflow/phases/phase_*.md`.
- There is no leaf hook layer. A pure leaf makes no tool call, so there is nothing for a hook to judge; `tools/hooks/` holds the operator's DEV entrypoint alone (`docs/HOOKS.md` is canonical).

## Responsibility-decision flow
1. Judge whether the rule to add/change directly affects the validity of a workflow artifact.
2. When it directly affects validity, write it into `docs/workflow/WORKFLOW_CORE.md` or the relevant `docs/workflow/phases/phase_*.md`.
3. When it defines an overall policy such as `spec` registry / naming / placement / promotion, rather than a workflow common norm, write it into `SPEC.md`.
4. When the rule is a detail of the execution method such as the tool-call procedure, input-collection order, regeneration procedure, or on-failure operations, write it into the corresponding `SKILL.md`.
5. Agent-specific execution conveniences (e.g. prompt order, log-organization procedure) are limited to `SKILL.md`, and are not mixed into `docs/workflow/WORKFLOW_CORE.md` or `docs/workflow/phases/`.
6. When the decision is hard, use as the decision axis whether the impact of a rule violation extends to the destruction of auditability / reproducibility / judgment consistency. When it destroys, choose the contract documents under `docs/workflow/`; when it does not, choose `SKILL.md`.

## phase-to-contract correspondence table (core workflow)
Every row is host-side: either a `pure-function leaf` reading one template, or a deterministic
substep the conductor runs in-process. No row names a `SKILL`.

- `Compile generate`: `tools/prompt_templates/pure_compile_generate.txt`, with `phase_01_compile.md` and the node's `spec` documents inlined (`_build_pure_compile_context`)
- `Compile static`: conductor in-process (deterministic `validate_workspace_root` + `validate_pipeline_semantics --stage compile`, see `docs/workflow/phases/phase_01_compile.md`)
- `Compile verify`: `tools/prompt_templates/pure_compile_verify.txt` (`_build_pure_compile_verify_context`)
- `Generate generate`: `tools/prompt_templates/pure_generate_generate.txt` on the `m3c` bundle shape, `pure_generate_generate_harness.txt` on the `harness` shape
- `Generate gate`: conductor in-process (deterministic lint check `run_linter`, syntax check `run_syntax_check` compiler front-end gate gfortran `-fsyntax-only`, and static check `validate_workspace_root` + `validate_pipeline_semantics --stage post_generate` — the static check runs only when the lint and syntax checks both pass; see `docs/workflow/phases/phase_02_generate.md`)
- `Generate verify`: `tools/prompt_templates/pure_generate_verify.txt` / `pure_generate_verify_harness.txt`, per bundle shape
- `Build`: conductor in-process (see `docs/workflow/phases/phase_03_build.md`)
- `Validate execute`: conductor in-process (see `docs/workflow/phases/phase_04_validate.md`)
- `Validate judge`: `tools/prompt_templates/pure_validate_judge.txt`
- `Escalate diagnose`: `tools/prompt_templates/pure_escalate_diagnose.txt`
- Repair turns: `tools/prompt_templates/pure_bundle_repair.txt`

## phase-to-Skill correspondence table (optional flows)
- `Tune generate`: `skills/workflow-tune-generate/SKILL.md`
- `Tune verify`: `skills/workflow-tune-verify/SKILL.md`
- `Promote`: `skills/workflow-promote/SKILL.md`

## Auxiliary Skills
- `Workflow audit (Codex)`: `skills/workflow-audit-codex/SKILL.md`
- `Workflow audit (Claude Code)`: `skills/workflow-audit-claude/SKILL.md`
- `Spec input check`: `skills/spec-input-check/SKILL.md` (pre-`Compile` advisory check of `controlled_spec.md` / `deps.yaml` / `tests.md`; proposal only, does not modify the spec)
- `Workflow timing & token audit`: `skills/workflow-timing-audit/SKILL.md` (Claude Code-only post-run diagnostic; breaks an orchestration down into per-leaf elapsed time and output tokens via `phase_state_log.jsonl` + the per-leaf session transcripts, collapsing the transcript multiple-counting traps; an HTTP leaf has no transcript, so its per-request tokens, elapsed and throughput come from the run's `launches/` artifacts instead; read-only, does not modify any artifact)
- `Workflow escalate (failure diagnostician)`: **no `SKILL`**. The read-only, one-shot escalate LLM the conductor consults when the deterministic decision tables cannot classify a phase failure — it emits a routing directive deciding rollback distance, severity `minor|major|critical`, and reuse-vs-discard of existing artifacts. Its persona, directive schema and decision criteria are the static body of the pure launch template `tools/prompt_templates/pure_escalate_diagnose.txt`, which is the canonical source and is hashed by the prompt-contract drift guard. There was a `skills/workflow-escalate/SKILL.md`, read host-side and rendered into the prompt; issue #169 moved the diagnostician onto the pure transport, where the template is what a leaf receives, and deleted the `SKILL` rather than leave a twin of the same text.

## Operations Rules
1. When handling multiple phases in one piece of work, switch the corresponding `SKILL` per phase.
2. When `verify` fails, go back to the `generate` of the same phase, and re-verify after regeneration.
3. Record the loop state and the failure reason in the metadata of the relevant phase.
4. When the `SKILL` definition is changed, update this correspondence table in the same change.
5. When changing a workflow contract, first update `docs/workflow/WORKFLOW_CORE.md` or the relevant `docs/workflow/phases/phase_*.md`, and update `SKILL.md` following that change.
6. Write a change to the workflow common norms into `docs/workflow/WORKFLOW_CORE.md`, a change to each `phase`'s detailed contract into `docs/workflow/phases/`, a change to the hierarchical execution contract into `ORCHESTRATION.md`, and a change to the phase procedure into the corresponding `SKILL.md`.
7. Do not restate the rule body in `AGENT_SKILLS.md`; write the reference target and the responsibility decision.

## Decision Criteria
- The `SKILL` path used in the target phase can be explained.
- The generated artifacts and judgment artifacts match the contract of the corresponding `SKILL`.
- The phase choice for the same input is consistent across agents.
- The reference target for the workflow common norms, the hierarchical execution contract, and the phase procedure is uniquely determined.
- The same rule is not duplicated/restated across `docs/workflow/WORKFLOW_CORE.md` or `docs/workflow/phases/`, `ORCHESTRATION.md`, and `SKILL.md`.
