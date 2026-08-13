# met-dsl

`met-dsl` generates, validates, and certifies weather and climate compute kernels from natural-language specifications.

`controlled_spec.md` (physics and algorithm definition), `tests.md` (verification profile), and `deps.yaml` (dependency declaration) are authored by humans and are the canonical source. Every phase after them is executed by a deterministic conductor (`tools/workflow_conductor.py`), which launches each judgment-bearing `substep` as one isolated `LLM` leaf under a fixed input/output contract, runs the deterministic gates and the build itself in its own process, and performs every build, execution, lint, and syntax check through the capability-gated MCP build/runtime server.

## Scope

- Generate the `model` (physics computation) and the `runner` (execution and judgment coordination) for a computation task defined by a `spec`, targeting `CPU` and `GPU` hardware. The material certified in this tree is Fortran on `CPU`.
- Manage specifications under four `spec_kind` values: `problem` (integration scenario), `component` (reusable operation), `profile` (component selection policy), and `infrastructure` (the shared runner harness, one node per `(language, hardware)` target).
- Keep the physics definition separated from execution optimization. `spec.ir.yaml` carries physics-affecting structure in its `case` / `algorithm` / `io_contract` sections and execution discretion in `impl_defaults`.
- Judge each `node` from its own execution evidence, and aggregate the judgment across its dependency closure.

## Workflow

The core workflow is five phases. Each phase produces exactly one kind of primary artifact. `docs/WORKFLOW.md` is the entry point of the contract, and `docs/workflow/WORKFLOW_CORE.md` is the canonical source for the common invariants and the per-phase I/O contract.

| # | phase | role | primary artifact |
|---|-------|------|------------------|
| 0 | Spec | manual authoring of the natural-language specification | `controlled_spec.md` / `tests.md` / `deps.yaml` |
| 1 | Compile | natural-language specification → structured IR | `spec.ir.yaml` |
| 2 | Generate | IR → source code | `source/<source_id>/` |
| 3 | Build | source → binary (deterministic) | `binary/<binary_id>/bin/` |
| 4 | Validate | execution and pass/fail judgment | `verdict.json` / `aggregate_verdict.json` |

`Tune` (implementation-variant exploration) and `Promote` (publication to `releases/`) are optional flows outside the core workflow.

From `Generate` onward, `spec.ir.yaml` is the sole generation and verification contract; reading `controlled_spec.md` is forbidden except at `Generate.verify`, which reads it as a secondary requirement-fidelity cross-check.

Each phase runs an ordered substep sequence. An `LLM` substep runs as one isolated leaf; a deterministic substep runs in the conductor's own process.

| phase | substeps | `LLM` substeps |
|---|---|---|
| Compile | `generate` → `static` → `verify` | `generate`, `verify` |
| Generate | `generate` → `gate` → `verify` | `generate`, `verify` |
| Build | none (a single step) | none |
| Validate | `pre_judge` → `execute` → `judge` → `post_judge` | `judge` |

The deterministic `Compile.static` and `Generate.gate` substeps route a violation back to the phase's `generate` substep, so the `verify` leaf is reached only on a deterministically-clean artifact. `Validate.pre_judge` blocks execution when the dependency DAG is not ready, and `Validate.post_judge` classifies the severity of a violation found after the judge returns.

## Running a workflow

Required CLI: `python3`, `jq`, `git`. Their absence stops the run at startup. A workflow run additionally requires the sandbox runtime `bwrap`, the toolchain the target `spec` declares (`gfortran` and `make` for the Fortran nodes in this tree), and the linters the `Generate` gate invokes (`fortitude` for Fortran).

Create the leaf-`LLM` configuration once. It selects the provider, model, and reasoning effort of each `LLM` leaf, is gitignored, and has no command-line override.

```bash
cp docs/examples/llm_claude.example.yaml llm.yaml
```

Start a run:

```bash
python3 tools/run_workflow.py <spec_ref> <until_phase> [--llm-config <path>]
```

`<until_phase>` is one of `compile` / `generate` / `build` / `validate`. `--llm-config` defaults to `./llm.yaml`; a missing default stops the run with `llm_config_default_missing` rather than being filled in. Frequently used options:

| option | effect |
|---|---|
| `--with-deps` | resolve the transitive dependency closure and run each not-yet-ready dependency node bottom-up before the target |
| `--resume` | continue the latest (or `--orchestration-id`) orchestration from its checkpoint, recovering `spec_ref` / `until_phase` / the launched configuration |
| `--mode` | `dev` (default) or `prod` |
| `--no-run-conductor` | prepare the orchestration artifacts without running the conductor |

`docs/RUNBOOK.md` is the canonical operational procedure: preflight requirements per backend, the minimal loop, the failure-to-phase routing table, and the recovery procedures. On the Claude backend, preflight requires `build-runtime` to be enabled in the committed `.claude/settings.json` and permission-granted to the child agent session (`docs/RUNBOOK.md` §0-2).

## MCP tools

`mcp_servers/build_runtime_server.py` is the standard server (stdio JSON-RPC, no dependency packages). Every `compile`, `run`, `quality check`, `static lint`, and `syntax check` goes through it; one-off `gcc` / `clang` / `gfortran` builds are forbidden.

| tool | purpose |
|---|---|
| `compile_project` | build through a standard build tool that handles dependencies (`make` by default) |
| `run_program` | run the built `runner` |
| `run_quality_checks` | run a quality-check `preset` |
| `run_linter` | run the `Generate` static lint `preset` (`fortitude` / `cppcheck` / `ruff`) |
| `run_syntax_check` | run a compiler front end in syntax-only mode, producing no build artifacts |
| `detect_build_system` | report which build-system marker files exist (standalone use only) |

Under the workflow, `compile_project` / `run_program` / `run_quality_checks` / `run_linter` / `run_syntax_check` require `orchestration_id`, `agent_run_id`, and `capability_token`, and a call that omits them is refused; `detect_build_system` holds no capability and is refused outright. Outside a run the server works without them. `mcp_servers/README.md` is canonical for the argument allowlists and the operational rules; `mcp_servers/mcp_servers.example.json` holds client configuration examples.

## Artifacts

Trial artifacts are confined to `workspace/`, and a write outside it fails the phase.

```text
workspace/ir/<node_key_safe>/<ir_id>/                   spec.ir.yaml, ir_meta.json, dependency_graph.json
workspace/pipelines/<node_key_safe>/<pipeline_id>/      lineage.json
  source/<source_id>/                                   Generate output
  binary/<binary_id>/                                   Build output
  runs/<run_id>/<node_key_safe>/                        Validate output and raw evidence
workspace/orchestrations/<orchestration_id>/            orchestration_meta.json, agent_graph.json, agent_runs.jsonl
```

`Validate` emits `diagnostics.json`, `perf.json`, `verdict.json`, `aggregate_verdict.json`, `summary.json`, and `semantic_review.json` per `run_id`. Promoted official-version artifacts live under `releases/<spec_kind>/<domain>/<family>/<spec_id>/<target_architecture>/<toolchain_language>/<release_id>/`. `docs/WORKSPACE_LAYOUT.md` is the canonical layout source.

## Repository layout

```text
docs/         workflow contracts, phase specifications, runbook, glossary
spec/         source specs (problem / component / profile / infrastructure) and the registry
skills/       per-phase execution procedures (SKILL.md) for the agentic leaves
tools/        workflow driver, conductor, orchestration runtime, gates, validators, tests
mcp_servers/  MCP build/runtime server and client configuration examples
releases/     promoted official artifacts and the component registry
workspace/    trial artifacts
```

## In-tree specifications

| spec_kind | spec_id |
|---|---|
| `problem` | `advdiff1d_linear`, `shallow_water2d` |
| `component` | `dynamics_advdiff_flux_1d_upwind_center2`, `dynamics_advection_diffusion_boundary_1d_periodic_copy`, `dynamics_advection_diffusion_time_update_1d_euler1`, `dynamics_shallow_water_flux_2d_rusanov_p0`, `dynamics_shallow_water_boundary_2d_periodic_copy`, `dynamics_shallow_water_time_update_2d_ssprk2` |
| `profile` | `dynamics_advdiff_profile_1d_upwind_center2_euler1`, `dynamics_shallow_water_profile_2d_rusanov_p0_ssprk2` |
| `infrastructure` | `harness_fortran_cpu` |

`spec/registry/spec_catalog.yaml` is the registry of record for placement and state.

## Documentation entry points

| document | content |
|---|---|
| `docs/WORKFLOW.md` | workflow entry point; `docs/workflow/WORKFLOW_CORE.md` and `docs/workflow/phases/` hold the contracts |
| `docs/SPEC.md` | overall policy, `spec` management requirements, registry requirements |
| `docs/RUNBOOK.md` | operational procedure, preflight, recovery |
| `docs/ORCHESTRATION.md` | orchestration design and contract; `docs/AGENT_CONTRACT.md` holds the child-agent contract |
| `docs/AGENT_SKILLS.md` | phase-to-`SKILL` mapping |
| `docs/GLOSSARY.md` | canonical terminology and artifact definitions |
| `AGENTS.md` | conventions every agent working in this repository follows |
| `TODO.md` | incomplete tasks aggregated across the repository |

## License

BSD 2-Clause. See `LICENSE`.
