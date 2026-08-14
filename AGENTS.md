# AGENTS.md

Conventions every agent (Codex / Claude Code) working in this repository must follow. This file is kept to content that **all agents** need; role- or task-specific rules live in dedicated documents (below). Claude Code loads this file by importing it from `CLAUDE.md` via `@AGENTS.md`.

## Dedicated rule documents
- **Authoring repository documents** (style, terminology, math notation, forbidden expressions, structure, checklist): `docs/DOC_STYLE.md`.
- **CLI argument information-acquisition policy** (which subcommand uses a doc vs `--help`): the "Information-acquisition policy" section of `docs/CLI_REFERENCE.md`.
- **Hook implementation structure** (where hook validation / invocations are defined): `docs/HOOKS.md`.
- **Backend boundary** (where knowledge of a concrete language / build tool / compiler / linter / parallel model may live): `docs/BACKEND_BOUNDARY.md`.

## Backend boundary rules
- A concrete technology is chosen per **`axis`**, and the knowledge each choice implies lives in exactly one **`backend`** package, `tools/backends/<axis>/<backend_id>/`, registered in `tools/backends/registry.py`. `docs/BACKEND_BOUNDARY.md` is canonical for the rule, the placement table (code / documents / prompt templates / `SKILL`), and the procedure for adding a `backend` or an `axis`.
- The **`neutral core`** — every other module, template, `SKILL`, and document — may name an `axis` value as an opaque token. It must not contain a file extension, keyword, statement grammar, compiler argument, lint rule id, directive spelling, control-file syntax, symbol-naming convention, or diagnostic format that the value implies.
- Do not write a new technology-specific rule into a `neutral core` file, and do not import a `backend` module outside `tools/backends/registry.py`. `tools/tests/test_backend_boundary.py` catches the second as a set identity, and catches the first only when it is spelled with one of the tokens that check samples — it is a bound on growth, not a detector. `docs/BACKEND_BOUNDARY.md` §Enforcement states what each half does and does not prove; a green check is not evidence that a change respects the rule.
- Ask the `registry` the question you mean: `unsupported_reason` for whether an `axis` value is a declared member, `unavailable_reason` for whether it is declared **and** its `backend` has been extracted. Code that is about to run `backend` behaviour asks the second.
- The `neutral core` still carries measured pre-existing debt. Adding to it is forbidden; the per-area migration plan is in `TODO.md`. Do not restate the boundary rule in another document — cite `docs/BACKEND_BOUNDARY.md`.

## MCP execution rules
- Always run `compile` / `run` / `quality check` through the MCP server.
- The standard server is `mcp_servers/build_runtime_server.py`; use `compile_project` / `run_program` / `run_quality_checks` / `run_linter` / `run_syntax_check`. (`detect_build_system` is a standalone-only helper: under the workflow the build system comes from the IR's toolchain and the tool is refused.)
- When `compile` handles `fortran` / `c` / `cpp` / `mixed` families, only allow standard build tools that can handle dependencies. The default is `make`.
- Forbid one-off builds that call `gcc` / `clang` / `gfortran` directly.
- Under the workflow, `compile_project` / `run_program` / `run_quality_checks` / `run_linter` / `run_syntax_check` require `orchestration_id`, `agent_run_id`, and `capability_token`; a call that omits them is refused, not exempted from the capability gate. There, every caller-supplied input that decides what runs — `env`, `extra_args`, `target`, `sources`, `project_dir`, `command_log_path` — is an allowlist over what the workflow declares; everything else is refused. `run_program`'s `command` is the exception: it is caller-chosen argv by design. Canonical: `mcp_servers/README.md`.
- `Generate` runs `static lint` via the MCP `run_linter`, and its deterministic `Generate.syntax` gate runs a compiler front-end in syntax-only mode (producing no build artifacts) via the MCP `run_syntax_check`. Both are separate steps from builds via `compile` / `compile_project` / `toolchain.build_system`, and both are outside the scope of the rule that requires `compile` to go through a standard build tool (and outside the ban on calling `gfortran` directly, which targets builds).
- For processing other than `compile` / `run` where MCP applies (e.g. build system detection, test execution, check execution), implement MCP tools likewise and avoid direct shell execution.
- For MCP client configuration, refer to `mcp_servers/mcp_servers.example.json`; for operational details, `mcp_servers/README.md`.

## Project Local Skills rules
- Treat the `SKILL.md` files under `skills/` as the canonical source for the execution procedure of each workflow phase.
- For the mapping between phases and `SKILL`, refer to `docs/AGENT_SKILLS.md`.
- For phases that have a `generate -> verify -> regenerate` loop, apply the corresponding `generate` `SKILL` and `verify` `SKILL` separately.
- On `Codex` / `Claude Code` alike, before starting work read the `SKILL.md` for the target phase and follow the defined input/output contract and decision criteria.

## Workflow document reference rules
- The entry point to the workflow specification is `docs/WORKFLOW.md`. `docs/workflow/WORKFLOW_CORE.md` is the canonical source for the common invariants, phase sequence, and the per-`phase` I/O contract list; the files under `docs/workflow/phases/` are the canonical source for each `phase`'s detailed contract.
- `tools/workflow_conductor.py` drives the deterministic phase/substep loop and launches each `step agent` / `substep agent` as a leaf. `docs/ORCHESTRATION.md` is the canonical orchestration design + contract spec; `docs/AGENT_CONTRACT.md` is the canonical child step/substep agent contract.
- `docs/AGENT_SKILLS.md` is the canonical source for the phase-to-`SKILL` mapping, the decision on where rules are documented, and the phase-switching rules.
- Do not restate workflow-specific prohibitions, the ban on referencing past artifacts, or the independent-`agent` execution-evidence requirements in `AGENTS.md`; refer to the corresponding canonical source.
- The canonical entrypoint for starting the workflow is the user running `python3 tools/run_workflow.py <spec_ref> <until_phase> [--llm-config <path>]`, which selects the LLM of each phase / `substep` from a configuration file (default `./llm.yaml`, which the operator creates with `cp docs/examples/llm_claude.example.yaml llm.yaml`); a codex configuration also requires an explicit `model:` (add `--with-deps` to run the dependency closure, `--resume` to recover a failed run; see `docs/RUNBOOK.md`). The configuration file is the only thing that says what a leaf launches; `docs/ORCHESTRATION.md` "Leaf LLM configuration" is canonical for it.
- When the workflow runs, the canonical source for `METDSL_WORKFLOW_MODE=1` and `METDSL_ORCHESTRATION_ID=<orchestration_id>` is the values set by the `tools/run_workflow.py` that the user started.
