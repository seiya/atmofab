# met-dsl

`met-dsl` generates, validates, and certifies weather and climate compute kernels from natural-language specifications.

`controlled_spec.md` (physics and algorithm definition), `tests.md` (verification profile), and `deps.yaml` (dependency declaration) are authored by humans and are the canonical source. Every phase after them is executed by a deterministic conductor (`tools/workflow_conductor.py`), which fulfils the `orchestration agent` role: it launches each judgment-bearing `substep` as one isolated `substep agent` (an `LLM` leaf) under a fixed input/output contract, runs the deterministic gates and the build itself in its own process, and performs every build, execution, lint, and syntax check through the capability-gated MCP build/runtime server.

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

`Tune` (implementation-variant exploration) and `Promote` (publication to `releases/`) are optional flows outside the core workflow. Neither is implemented: their contracts are deferred (`docs/WORKFLOW.md` §Optional flows), and no `spec` in this tree carries an official release.

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

Required CLI: `python3`, `jq`, `git`. Their absence stops the run at startup with `missing_required_cli_tools`. A run additionally requires PyYAML (imported by the workflow runtime; the repository declares no dependency manifest), the sandbox runtime `bwrap`, the CLI or API credentials of every provider the leaf-`LLM` configuration names, the toolchain the target `spec` declares (`gfortran` and `make` for the Fortran nodes in this tree), and the `static lint` tool for its language (`fortitude` for Fortran).

Create the leaf-`LLM` configuration once. It selects the provider, model, and reasoning effort of each `LLM` leaf, and is the only thing that says what a leaf launches: no flag overrides its contents, and `--llm-config` only selects which file is read. It is gitignored.

```bash
cp docs/examples/llm_claude.example.yaml llm.yaml
```

Every `LLM` leaf is a billed provider call. One node spends at least five of them per full `Compile` → `Validate` pass — more when a phase re-rolls — a `--with-deps` closure spends them per node, and a leaf that dies on a provider quota terminalizes the run unless `--wait-usage-reset` is passed.

Start a run:

```bash
python3 tools/run_workflow.py <spec_ref> <until_phase> [--llm-config <path>]

# the dependency closure of one problem node, then the node itself
python3 tools/run_workflow.py spec/problem/dynamics/advection_diffusion/advdiff1d_linear validate --with-deps
```

`<spec_ref>` is the `spec` directory of the target node and `<until_phase>` is one of `compile` / `generate` / `build` / `validate`. `--llm-config` defaults to `./llm.yaml`; a missing default stops the run with `llm_config_default_missing` rather than being filled in. `python3 tools/run_workflow.py --help` is canonical for the full option set (`docs/CLI_REFERENCE.md` §Information-acquisition policy); the options a first run needs are:

| option | effect |
|---|---|
| `--with-deps` | resolve the transitive dependency closure and run each not-yet-ready dependency node bottom-up before the target |
| `--resume` | continue the latest (or `--orchestration-id`) orchestration from its checkpoint, recovering `spec_ref` / `until_phase` / the launched configuration |
| `--mode` | `dev` (default): a `major` / `critical` verify finding terminalizes the run. `prod`: it is routed to the diagnostician, which decides how far back to recover |
| `--wait-usage-reset` | sleep out a provider usage limit in place and re-launch the dead substep, instead of terminalizing (bounded; opt-in per invocation) |

`docs/RUNBOOK.md` is the canonical operational procedure: preflight requirements per backend, the minimal loop, the failure-to-phase routing table, and the recovery procedures. On the Claude backend, preflight requires `build-runtime` to be enabled in the committed `.claude/settings.json` and permission-granted to the child agent session (`docs/RUNBOOK.md` §0-2).

## MCP tools

`mcp_servers/build_runtime_server.py` is the standard server (stdio JSON-RPC, no dependency packages). Every `compile`, `run`, `quality check`, `static lint`, and `syntax check` goes through it; one-off `gcc` / `clang` / `gfortran` builds are forbidden.

| tool | purpose |
|---|---|
| `compile_project` | build through a standard build tool that handles dependencies (`make` by default for the `fortran` / `c` families) |
| `run_program` | run the built `runner` |
| `run_quality_checks` | run a quality-check `preset` |
| `run_linter` | run the `Generate` `static lint` `preset` (`fortitude` / `cppcheck` / `ruff` / `mixed`) |
| `run_syntax_check` | run a compiler front end in syntax-only mode, producing no build artifacts |
| `detect_build_system` | recommend a build system from the marker files present (standalone use only) |

Under the workflow, `compile_project` / `run_program` / `run_quality_checks` / `run_linter` / `run_syntax_check` require `orchestration_id`, `agent_run_id`, and `capability_token`, and a call that omits them is refused; `detect_build_system` holds no capability and is refused outright. Outside a run the server works without them. `mcp_servers/README.md` is canonical for the argument allowlists and the operational rules; `mcp_servers/mcp_servers.example.json` holds client configuration examples.

## Artifacts

Trial artifacts are confined to `workspace/`, and a write outside it fails the phase. `docs/GLOSSARY.md` defines the identifiers below (`node_key_safe`, `ir_id`, `pipeline_id`, `source_id`, `binary_id`, `run_id`).

```text
workspace/ir/<node_key_safe>/<ir_id>/                   spec.ir.yaml, ir_meta.json, dependency_graph.json
workspace/pipelines/<node_key_safe>/<pipeline_id>/      lineage.json
  source/<source_id>/                                   Generate output
  binary/<binary_id>/                                   Build output
  runs/<run_id>/<node_key_safe>/                        Validate output and raw evidence
workspace/orchestrations/<orchestration_id>/            orchestration_meta.json, agent_graph.json, agent_runs.jsonl
```

`Validate` emits `diagnostics.json`, `perf.json`, `verdict.json`, `aggregate_verdict.json`, `summary.json`, and `semantic_review.json` per `run_id`. Promoted official-version artifacts live under `releases/<spec_kind>/<domain>/<family>/<spec_id>/<target_architecture>/<toolchain_language>/<release_id>/`. `docs/WORKSPACE_LAYOUT.md` is the canonical layout source.

The driver prints the run's event stream on stdout (`--stdout-format human`, or `jsonl` for a caller that parses it) and mirrors it to `workspace/orchestrations/<orchestration_id>/run_logs/`. A run's terminal state is `orchestration_meta.json#status`; the node's physics judgment, including its dependencies, is `aggregate_verdict.json`.

## Repository layout

```text
docs/         workflow contracts, phase specifications, runbook, glossary
spec/         source specs (problem / component / profile / infrastructure) and the registry
skills/       per-phase execution procedures (SKILL.md) for the agentic leaves
tools/        workflow driver, conductor, orchestration runtime, gates, validators, tests
mcp_servers/  MCP build/runtime server and client configuration examples
leaf_config/  the committed configuration a workflow leaf is launched with
.claude/      the operator's own interactive session: settings, and the development skills
releases/     the component registry, and the promoted official artifacts of the Promote flow (none yet)
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

## Tests

Full suite (the default):

```bash
python3 -m pytest tools/tests/ -q
```

Fast local loop, deselecting the wall-clock-bound `slow` tests:

```bash
python3 -m pytest tools/tests/ -q -m "not slow"
```

`pytest.ini` registers the `slow` marker and sets `-p no:randomly`. The marker is opt-out
because nothing else runs the full set: deselecting it by default would leave the leaf
deadline / abandon / teardown guards executed by nobody.

## Documentation entry points

| document | content |
|---|---|
| `docs/WORKFLOW.md` | workflow entry point; `docs/workflow/WORKFLOW_CORE.md` and `docs/workflow/phases/` hold the contracts |
| `docs/SPEC.md` | overall policy, `spec` management requirements, registry requirements |
| `docs/RUNBOOK.md` | operational procedure, preflight, recovery |
| `docs/ORCHESTRATION.md` | orchestration design and contract; `docs/AGENT_CONTRACT.md` holds the child-agent contract |
| `docs/AGENT_SKILLS.md` | phase-to-`SKILL` mapping |
| `docs/GLOSSARY.md` | canonical terminology and artifact definitions |
| `docs/BACKEND_BOUNDARY.md` | where knowledge of a concrete target-stack technology may live, and which `axis` selects it |
| `AGENTS.md` | conventions every agent working in this repository follows |
| `TODO.md` | incomplete tasks aggregated across the repository |

## License

BSD 2-Clause. See `LICENSE`.
