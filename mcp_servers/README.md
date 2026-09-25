# MCP Server: Build/Runtime Operations

## Purpose
This directory provides the implementation for running `compile` / `run` / `quality check` / `static lint` / `syntax check` for `Generate` via the MCP server.

## Provided server
- `build_runtime_server.py`
  - a minimal MCP server that works without dependency packages (stdio JSON-RPC)
  - provided tools:
    - `detect_build_system`
    - `compile_project`
    - `run_program`
    - `run_quality_checks`
    - `run_linter`
    - `run_syntax_check`

## Important operational rules
- `compile_project` allows only standard build tools that can handle dependencies.
- For a compiled language (one whose language backend declares `COMPILED`, `registry.is_compiled_language`), when the build tool is unspecified, the default is `make`, and `compile_project` accepts only `make` / `cmake` / `meson` / `ninja`. An unspecified `build_system` falls back to marker-file detection; the conductor always passes one explicitly (`_build_inproc` reads it from the run's target profile, `_read_toolchain`), so `detect_build_system`'s recommendation is advisory and is not what a workflow build uses. A separate orchestrated arm that read an omitted `build_system` as `make` outright went with the gate ([issue #171](https://github.com/seiya/atmofab/issues/171) PR-2).
- `repo_root` is accepted and unused. It anchored the capability gate's evidence — preflight, `phase_state.json`, the launch record, the capability file — and was pinned to this server's own checkout under the workflow so a caller could not bring its own; that gate is retired ([issue #171](https://github.com/seiya/atmofab/issues/171) PR-2) and nothing reads the argument.
- **ONE validation mode, for every caller.** `orchestration_id` and `agent_run_id` are optional ATTRIBUTION: the server records them in the `command_log.jsonl` entry so a command can be traced back to the run that issued it, and decides nothing from them. The workflow environment variables (`ATMOFAB_ORCHESTRATION_ID`, `ATMOFAB_WORKFLOW_MODE`) are not read at all. Until [issue #171](https://github.com/seiya/atmofab/issues/171) PR-2 those names switched the server into a second, ALLOWLIST mode and a `capability_token` was required with them; the mode existed because the caller might be a LEAF whose grant had to be bounded, and no leaf reaches this server (a `pure-function leaf` launches with `--tools ""`, `--strict-mcp-config` and no MCP configuration). `capability_token` is now REFUSED outright rather than ignored, so a caller written against the old contract fails instead of being served as if the check had passed. `detect_build_system` was refused under the workflow for the same reason and is available again.
- This file is canonical for the rules below; `AGENTS.md`, `docs/HOOKS.md` and `docs/ORCHESTRATION.md` point here.
- **The caller-supplied `env` is checked one way for every caller, and the check is a DENYLIST over names plus a rule on values.** The names that redirect what is EXECUTED are refused — `LD_*` / `DYLD_*` / `PATH` / `PYTHONPATH` / `BASH_ENV` / `ENV` / `IFS` / `COMPILER_PATH` / `GCC_EXEC_PREFIX` / `LIBRARY_PATH` / `MAKEFLAGS` / `GNUMAKEFLAGS` / `MAKEFILES` / `MAKESHELL` / `.SHELLFLAGS` / `SHELL` / `MAKE` / `MAKE_COMMAND` — because the loader, the gcc driver (which execs the `f951` it finds through `COMPILER_PATH`) and make itself all read them ahead of anything the call names. `.SHELLFLAGS` supplies the arguments the recipe's shell is invoked with and make takes it from the ENVIRONMENT as well as the command line (measured: `env '.SHELLFLAGS=-c ./evil.sh' make all` runs `./evil.sh`, not the recipe, and exits 0). The name is normalised the same way on both halves — surrounding space, a leading `.`, and the flavour operators come off — by one shared normaliser, since a normalisation applied to one half and not the other is a hole on the weaker half by construction. And no value may carry a character the recipe's shell or make acts on, because make imports an environment name as a make VARIABLE and the host-authored control file interpolates all six of `Validate.execute`'s variables unquoted into `cd $(RUNDIR) && $(BINDIR)/$(BIN) --cases $(SPEC) $(CASES)`; that is the same rule the `extra_args` half applies, and applying it to one half only would guard nothing. **What this does NOT do, stated rather than closed:** it does not bound the names a build control file merely READS. `FC` redirects a certified control file's compiler and is not refused. (The names that redirect what is EXECUTED are refused on both halves — see the argv bullet.) Until [issue #171](https://github.com/seiya/atmofab/issues/171) PR-2 an ALLOWLIST of the six names above ran under an orchestration and closed that; it bounded a LEAF's grant, no leaf reaches this server, and the only caller under a run is the conductor composing a fixed six-key dict in the host process — so the gap is recorded here instead of being closed by restoring a grant bound. A denylist over environment names does not terminate, which is why this one draws its line at execution rather than at what make imports. The server's own addition (the pytest `PYTHONPATH`) is not caller-supplied and is unaffected; a parallel runtime's thread variables are caller-supplied since issue #289 and pass the denylist, since none of them redirects what is executed. This is a separate question from the leaf's own process environment, which `orchestration_runtime.LEAF_ENV_ALLOWLIST` decides; the two make the same argument for the same reason and neither is derived from the other.
- One constraint on where the checkout lives follows from the rules above: no directory an orchestrated call names — a node's `src/`, `binary/<id>/bin/`, `ir/<id>/`, `workspace/tmp/<agent_run_id>/…` — may be a symlink pointing out of the checkout. That was enforced by the containment checks, which resolved symlinks and failed such a call; those checks went with the leaf's grant in PR-2, so it is now a property of the checkout the operator is responsible for rather than one the server refuses. The second constraint used to be that the path may not contain whitespace or any of `` ;&|$`'"\<>()*?[]{}~#! ``; the metacharacter half still holds (those characters are refused in any value), but WHITESPACE is now accepted, because `CASES` is a word list by contract and the space was taken out of the refused set with it. A checkout under a path holding a space therefore produces a word-split recipe rather than a refusal naming the path.
- The type rules apply to every call: `target` must be a string and `extra_args` an array of strings; a non-string element is refused rather than coerced. `jobs`, `timeout_sec` and `capture_limit` are held to the minimums the served schema declares (1, 1, 1000): an MCP argument schema is advisory, and `make -j-5` waits forever.
- `run_syntax_check` applies its source rule to the list it discovers itself, not only to an explicit `sources` argument — a staged file named like an option (`-o.<ext>`) is a gate failure, not a skipped file.
- **What the argv and `env` rules below ARE.** `run_program`'s `command` is caller-chosen argv by design and constrained by nothing, so a caller that wants to execute an arbitrary program calls that tool. The rules on the other surfaces therefore do not CONFINE the caller they are checked against; they catch a mistake in the one command line this repository composes (`workflow_conductor._build_inproc`, a fixed three-element list of host paths). Read a finding against them as a defect in that composition, not as an escape.
- **The caller-chosen parts of the argv are restricted for every caller.** A `compile_project` `extra_args` element must ASSIGN a make variable (`NAME=value`, the name an identifier) **when the build system is `make`**, because make reads `--eval=$(shell ...)` and every other switch before the certified build control file. For a build system that does not read an element as an assignment interpolated into a shell recipe, an ordinary switch is admissible (`cargo build --release`, `mvn -DskipTests`): this server passes argv directly and never through a shell. Two rules apply here whatever the build system is, because `cmake` forwards `extra_args` to the native tool after `--` and a make command line is reachable from more than `build_system=make`. **First, the OPERATOR**: `NAME!=command` is make's shell assignment and EXECUTES its value — measured, `make 'FOO!=touch /tmp/x' all` creates the file and reports the build as succeeding — so it is refused whatever the variable is called, which is the point: no name denylist can reach it. **Second, the NAME**, normalised the way make reads it (surrounding space, a leading `.`, and the flavour operators `:` `+` `?` stripped, since `SHELL:=./evil` and `SHELL+=./evil` execute exactly as `SHELL=./evil` does): it must not be one make reads as a redirection of what is EXECUTED — the same denylist the `env` half applies, whose members are enumerated in the `env` bullet above rather than a second time here, and it is the SAME set, not a superset: an environment key and a command-line assignment are one vocabulary to make, so two sets would be two answers to one question. `SHELL=` replaces the interpreter of every recipe line and `MAKE` / `MAKE_COMMAND` are what a sub-make re-invokes itself with — measured, `env 'MAKE=./evil' make all` runs `./evil` against a Makefile whose recipe calls `$(MAKE)`, and exits 0. `SHELL` is genuinely command-line-only (make sets it itself and ignores the environment's) and is in the shared set anyway, because a name missing from one half is a hole by construction and that was this surface's defect three times over. That name rule and the character rule below apply to EVERY build system; only the assignment SHAPE is make's. This half must never be the weaker of the two, since a command-line assignment overrides even a hard assignment in the control file while an environment name does not. And no element may carry a character the recipe's shell or make acts on, because the host-authored control file interpolates it unquoted into `cd $(RUNDIR) && $(BINDIR)/$(BIN) --cases $(SPEC) $(CASES)`. A space is deliberately NOT in that set: `CASES` is a word list by contract. `target` lands positionally on that same command line (`make -jN <target>`), so make cannot tell it from an `extra_args` element and it takes the same answers — all of them. It must not open with `-`; it must carry no whitespace and no character the shell acts on; it must not be an assignment to an execution-redirecting name; and under `make` it must not be an assignment at all, since a positional `NAME=value` is never a goal there. Stated as what is REFUSED rather than as an allowlist, so a gradle task path, an npm script name, a meson typed target and a make pattern goal all pass — this server does not own those grammars. `run_syntax_check`'s `sources` must be source files of the adapter's language (its `syntax_promotions.SOURCE_SUFFIXES`) staged in `project_dir` (a gcc-family driver reads `-B<dir>/` and `@file` out of the source list, and execs the front end it finds there). A make command-line assignment overrides even a hard assignment in that file, so this surface carries more authority than the environment. Until [issue #171](https://github.com/seiya/atmofab/issues/171) PR-2 the orchestrated arm held four more things: an allowlist of the six variable NAMES; a containment rule on the four whose value is a path; a rule that `project_dir` be absolute and that it and the resolved `command_log_path` resolve inside the repository — so a `command_log_path` outside the checkout is now accepted, and the recorded `cwd` is the string the caller passed rather than a resolved path; and an outright refusal of ANY `target`. The first three bounded a LEAF's grant and are retired with it; the fourth is not retired but generalized, since refusing a switch catches a defect in a caller this repository writes rather than bounding a grant. `run_program`'s `command` is **not** covered: it is caller-chosen argv by design.
- The operation of directly calling `gcc` / `clang` / `gfortran` for a one-off build is forbidden.
- `run_linter` is the tool for `Generate`'s `static lint`. Rather than via `compile_project` or a `Makefile`'s `lint` target, it launches `fortitude` / `cppcheck` / `ruff` with only the `preset`. `preset=mixed` runs `fortitude` and `cppcheck` in order. It is outside the scope of the norm that requires `compile` to go through a standard build tool. No preset's argv is spelled in the server: each is declared by that linter's own backend package and read through `tools/backends/registry.py` (`docs/BACKEND_BOUNDARY.md`; issue #111 for `fortitude`, issue #120 for the other two). What each declaration carries is the rule set the gate applies and the flags that keep a verdict a function of the source — `--select` plus `--isolated` for `fortitude` and `ruff`, and for all three the closure of the in-source suppression channel a leaf can write (`--ignore-allow-comments`, `--ignore-noqa`, and for `cppcheck` the DELETION of `--inline-suppr`, which the server used to pass). Each backend's document under `docs/backends/linter/` states what its invocation does and does not close; `cppcheck`'s states a weaker property than the other two, because that tool has no way to select a rule set. `preset=mixed` has no argv of its own and stays in the neutral core, which is why it is the one `linter` record still carrying `lint` in `core_provides`. A linter's backend also says whether it walks a directory or is handed files (`SOURCE_SUFFIXES` in its `lint` module, `None` for a directory walker; issue #289, R4-b PR-4): the `cuda_cpp` linter, the CUDA compiler driver, walks nothing, so the server hands it every file of its suffixes under `project_dir`, at any depth, by name and `./`-prefixed so no leaf-chosen name reads as an option; a directory holding none runs nothing and is reported clean, which is what a directory walker reports for it.
- `run_program` interprets no target. Its launch environment — a parallel model's thread variables among it — is the caller's, passed as `env` (under the workflow, `tools/host_execution.py` composes it from the target profile, issue #289). `target_class`, `target.class`, `target` and `threads_per_rank` are REFUSED on `run_program`: from them the server used to set the OpenMP variables for a `cpu` class and run every other class with none, and a caller still sending them would otherwise run believing it had set a thread count. `target` stays `compile_project`'s build goal.
- `run_quality_checks` allows only the `preset` specification, and forbids the execution of an arbitrary `command`.
- The `preset=pytest` of `run_quality_checks` prepends `project_dir` to `PYTHONPATH` to ensure the reproducibility of import resolution.
- `run_linter` allows only the `preset` specification, and forbids the execution of an arbitrary `command`. `preset` is REQUIRED (issue #289, R4-b PR-2): which linter a node is linted with is its language's answer (`registry.linter_for_language`, from each linter backend's `LANGUAGES`), and the default it had was one language's linter.
- `run_syntax_check` is the tool for `Generate`'s deterministic `Generate.gate` syntax check: it runs a REGISTERED compiler adapter's syntax-only mode over the staged sources, with module files written to a throwaway `.mods` scratch dir inside `project_dir`. The adapters are the `compiler` records that declare `syntax_check` in `tools/backends/registry.py` (the argv, the executable, the version probe and a canary source live in `tools/backends/compiler/<id>/syntax.py`); each names the LANGUAGE whose sources it reads, and that language's `syntax_promotions` (`tools/backends/language/<id>/syntax.py`) says which files are sources, their order, and which warning classes are promoted to errors (issue #289, R4-b PR-2 — the Fortran facts sat inline here until then; they are stated in `docs/backends/language/fortran/GENERATE_RULES.md` §2). `compiler` and `std` are REQUIRED — both are the caller's target facts, and a default was one language's spelling. `architecture` (the target profile's `hardware.architecture`) is accepted and passed to the adapter; an adapter whose compiler takes no device architecture does not read it. Because it produces **no build artifacts**, it is lint-class, not a build — like `run_linter` it is outside the scope of the norm that requires `compile` to go through a standard build tool (the "no one-off compiler call" rule above targets builds). A new target compiler is added as a backend package declaring `syntax_check`, and the conductor selects stages via the `ATMOFAB_SYNTAX_COMPILERS` env var (default the language's mandatory compiler, `bundle_facts.MANDATORY_SYNTAX_COMPILER`; a stage whose compiler binary is absent, or whose adapter reads another language, is reported `skipped`). It forbids the execution of an arbitrary `command`.
- `compile_project` / `run_program` / `run_quality_checks` / `run_linter` / `run_syntax_check` always record the executed command in `JSONL` format.
- The default for `command_log_path` when unspecified is `<project_dir>/command_log.jsonl`.
- The execution result returns `command_id`, `executed_command`, and `command_log_path`, and when the log is under the repository, returns `command_log_ref`.

## MCP configuration examples

The canonical configuration file differs per backend. The repository bundles the following 2 files.

### Claude Code: `.mcp.json` (repository root)

Claude Code reads `.mcp.json` directly under the project root and defines the server. Bundled content:

```json
{
  "mcpServers": {
    "build-runtime": {
      "command": "python3",
      "args": ["./mcp_servers/build_runtime_server.py"]
    }
  }
}
```

`.mcp.json` is **the server definition** only, and enabling it (enablement) per project requires separate approval. The approval sources are (a) the workspace trust dialog at interactive `claude` launch (recorded per-user in `~/.claude.json`), and (b) the `enabledMcpjsonServers` / `enableAllProjectMcpServers` of the repository-committed `.claude/settings.json`.

**A workflow leaf does not take that path, and since Z4 ([issue #171](https://github.com/seiya/atmofab/issues/171)) it takes no MCP path at all.** A `pure-function leaf` launches with `--strict-mcp-config` and no `--mcp-config`, under `--safe-mode` with `--tools ""`: it has no server set, no tool to call one with, and nothing for an enablement decision to decide. The MCP tools are called by the CONDUCTOR's own in-process deterministic substeps. The enablement rules below therefore describe the OPERATOR's interactive session and the preflight gate that certifies it.

The `preflight` verified the enablement of `build-runtime` for a `claude_cli` run, using only the committed `.claude/settings.json` of (b) as the canonical source (`~/.claude.json` was not referenced, because it harms per-machine reproducibility). That check (`_probe_claude_mcp_registry`) is deleted with the agentic leaf in Z4: the enablement it certified governed a leaf's session, and no leaf has one now. An operator enabling the server for their own session reads (a) and (b) above; `claude mcp list` shows what a session came up with.

**In addition to enablement, the tool-call permission is also required for an operator's own session.** Place the server-level grant `mcp__build-runtime` in the `permissions.allow` of the committed `.claude/settings.json` (Claude Code's permission rule does not interpret the tool-name wildcard `mcp__build-runtime__*`, so use the server level). This used to be a LEAF requirement, stated on `leaf_config/claude/settings.json` with a preflight check behind it; both are deleted with the agentic leaf, which was the only thing that called an MCP tool from inside a session.

There is ONE file: the repository's own `.claude/settings.json`, the DEV layer for an operator's
interactive session, which carries the `enabledMcpjsonServers` enablement key and the grant above.
`leaf_config/claude/settings.json` was the second, loaded by a workflow leaf as the sole layer of a
host-prepared private configuration directory; it is deleted with the agentic leaf, and so is the
sync test that required the dev layer to be a superset of it.

```json
// .claude/settings.json  (the operator's own session)
{
  "enabledMcpjsonServers": ["build-runtime"],
  "permissions": { "allow": ["mcp__build-runtime"] }
}
```

To temporarily disable it in a personal environment, place `"disabledMcpjsonServers": ["build-runtime"]` in `.claude/settings.local.json`. That is a statement about the OPERATOR's own session: the preflight check that read it and made the run `status=fail` went with `_probe_claude_mcp_registry` in Z4 (issue #171), so it no longer stops a run — and it subtracts nothing from a leaf, which calls no MCP tool. What it does affect is the conductor's own in-process MCP calls, which run in the operator's environment.

### Cursor: `.cursor/mcp.json`

Cursor reads `.cursor/mcp.json`. The repository-bundled `.cursor/mcp.json` already specifies `build-runtime` with an absolute path (to match Cursor's resolution convention).

### General MCP clients

A reference example for other clients:

```json
{
  "mcpServers": {
    "build-runtime": {
      "command": "python",
      "args": [
        "/path/to/atmofab/mcp_servers/build_runtime_server.py"
      ]
    }
  }
}
```

## Tool call examples
An example of building `fortran` with `make`:

```json
{
  "name": "compile_project",
  "arguments": {
    "project_dir": "/path/to/project",
    "command_log_path": "logs/build_commands.jsonl",
    "language": "fortran",
    "build_system": "make",
    "target": "all",
    "jobs": 8
  }
}
```

An example of running a binary:

```json
{
  "name": "run_program",
  "arguments": {
    "project_dir": "/path/to/project",
    "command_log_path": "logs/run_commands.jsonl",
    "command": ["./bin/simulate", "--case", "case.resolved.yaml"],
    "env": {"OMP_NUM_THREADS": "8", "OMP_THREAD_LIMIT": "8"},
    "timeout_sec": 1800
  }
}
```

An example of `Generate`'s `static lint` (assuming `fortran`):

```json
{
  "name": "run_linter",
  "arguments": {
    "project_dir": "/path/to/workspace/pipelines/<node_key_safe>/<target_id>/<pipeline_id>/generate/<generation_id>/src",
    "command_log_path": "command_log.jsonl",
    "preset": "fortitude",
    "timeout_sec": 1800
  }
}
```
