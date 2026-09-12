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
- For `fortran` / `c` / `cpp` / `mixed` families, allow only `make/cmake/meson/ninja`.
- For `fortran` / `c` families, when the build tool is unspecified, the default is `make`. An unspecified `build_system` falls back to marker-file detection; the conductor always passes one explicitly (`_build_inproc` reads it from the IR's toolchain), so `detect_build_system`'s recommendation is advisory and is not what a workflow build uses. A separate orchestrated arm that read an omitted `build_system` as `make` outright went with the gate ([issue #171](https://github.com/seiya/atmofab/issues/171) PR-2).
- `repo_root` is accepted and unused. It anchored the capability gate's evidence — preflight, `phase_state.json`, the launch record, the capability file — and was pinned to this server's own checkout under the workflow so a caller could not bring its own; that gate is retired ([issue #171](https://github.com/seiya/atmofab/issues/171) PR-2) and nothing reads the argument.
- **ONE validation mode, for every caller.** `orchestration_id` and `agent_run_id` are optional ATTRIBUTION: the server records them in the `command_log.jsonl` entry so a command can be traced back to the run that issued it, and decides nothing from them. The workflow environment variables (`ATMOFAB_ORCHESTRATION_ID`, `ATMOFAB_WORKFLOW_MODE`) are not read at all. Until [issue #171](https://github.com/seiya/atmofab/issues/171) PR-2 those names switched the server into a second, ALLOWLIST mode and a `capability_token` was required with them; the mode existed because the caller might be a LEAF whose grant had to be bounded, and no leaf reaches this server (a `pure-function leaf` launches with `--tools ""`, `--strict-mcp-config` and no MCP configuration). `capability_token` is now REFUSED outright rather than ignored, so a caller written against the old contract fails instead of being served as if the check had passed. `detect_build_system` was refused under the workflow for the same reason and is available again.
- This file is canonical for the rules below; `AGENTS.md`, `docs/HOOKS.md` and `docs/ORCHESTRATION.md` point here.
- **The caller-supplied `env` is checked one way for every caller, and the check is a DENYLIST over names plus a rule on values.** The names that redirect what is EXECUTED are refused — `LD_*` / `DYLD_*` / `PATH` / `PYTHONPATH` / `BASH_ENV` / `ENV` / `IFS` / `COMPILER_PATH` / `GCC_EXEC_PREFIX` / `LIBRARY_PATH` / `MAKEFLAGS` / `GNUMAKEFLAGS` / `MAKEFILES` / `MAKESHELL` — because the loader, the gcc driver (which execs the `f951` it finds through `COMPILER_PATH`) and make itself all read them ahead of anything the call names. And no value may carry a character the recipe's shell or make acts on, because make imports an environment name as a make VARIABLE and the host-authored control file interpolates all six of `Validate.execute`'s variables unquoted into `cd $(RUNDIR) && $(BINDIR)/$(BIN) --cases $(SPEC) $(CASES)`; that is the same rule the `extra_args` half applies, and applying it to one half only would guard nothing. **What this does NOT do, stated rather than closed:** it does not bound the names a build control file merely READS. `FC` redirects a certified control file's compiler and is not refused. (The names that redirect what is EXECUTED are refused on both halves — see the argv bullet.) Until [issue #171](https://github.com/seiya/atmofab/issues/171) PR-2 an ALLOWLIST of the six names above ran under an orchestration and closed that; it bounded a LEAF's grant, no leaf reaches this server, and the only caller under a run is the conductor composing a fixed six-key dict in the host process — so the gap is recorded here instead of being closed by restoring a grant bound. A denylist over environment names does not terminate, which is why this one draws its line at execution rather than at what make imports. The server's own additions (`OMP_*`, the pytest `PYTHONPATH`) are not caller-supplied and are unaffected. This is a separate question from the leaf's own process environment, which `orchestration_runtime.LEAF_ENV_ALLOWLIST` decides; the two make the same argument for the same reason and neither is derived from the other.
- One constraint on where the checkout lives follows from the rules above: no directory an orchestrated call names — a node's `src/`, `binary/<id>/bin/`, `ir/<id>/`, `workspace/tmp/<agent_run_id>/…` — may be a symlink pointing out of the checkout. That was enforced by the containment checks, which resolved symlinks and failed such a call; those checks went with the leaf's grant in PR-2, so it is now a property of the checkout the operator is responsible for rather than one the server refuses. The second constraint used to be that the path may not contain whitespace or any of `` ;&|$`'"\<>()*?[]{}~#! ``; the metacharacter half still holds (those characters are refused in any value), but WHITESPACE is now accepted, because `CASES` is a word list by contract and the space was taken out of the refused set with it. A checkout under a path holding a space therefore produces a word-split recipe rather than a refusal naming the path.
- The type rules apply to every call: `target` must be a string and `extra_args` an array of strings; a non-string element is refused rather than coerced. `jobs`, `timeout_sec` and `capture_limit` are held to the minimums the served schema declares (1, 1, 1000): an MCP argument schema is advisory, and `make -j-5` waits forever.
- `run_syntax_check` applies its source rule to the list it discovers itself, not only to an explicit `sources` argument — a staged file named `-o.f90` is a gate failure, not a skipped file.
- **The caller-chosen parts of the argv are restricted for every caller.** A `compile_project` `extra_args` element must ASSIGN a make variable (`NAME=value`, the name an identifier) **when the build system is `make`**, because make reads `--eval=$(shell ...)` and every other switch before the certified build control file. For a build system that does not read an element as an assignment interpolated into a shell recipe, an ordinary switch is admissible (`cargo build --release`, `mvn -DskipTests`): this server passes argv directly and never through a shell. Its NAME must not be one make reads as a redirection of what is EXECUTED — the same denylist the `env` half applies (`LD_*` / `DYLD_*` / `PATH` / `PYTHONPATH` / `BASH_ENV` / `ENV` / `IFS` / `COMPILER_PATH` / `GCC_EXEC_PREFIX` / `LIBRARY_PATH` / `MAKEFLAGS` / `GNUMAKEFLAGS` / `MAKEFILES` / `MAKESHELL`), PLUS `SHELL` and `MAKE`, which matter only here: `SHELL=` replaces the interpreter of every recipe line, and `MAKE` is what a sub-make re-invokes itself with. That name rule and the character rule below apply to EVERY build system; only the assignment SHAPE is make's. This half must never be the weaker of the two, since a command-line assignment overrides even a hard assignment in the control file while an environment name does not. And no element may carry a character the recipe's shell or make acts on, because the host-authored control file interpolates it unquoted into `cd $(RUNDIR) && $(BINDIR)/$(BIN) --cases $(SPEC) $(CASES)`. A space is deliberately NOT in that set: `CASES` is a word list by contract. `target` lands positionally on that same command line (`make -jN <target>`), so it takes the same answer, stated as what is REFUSED rather than as an allowlist: it must not open with `-`, and it must carry no whitespace and no character the shell acts on. A switch spelled as a target is refused; a gradle task path, an npm script name, a meson typed target and a make pattern goal are not, because this server does not own those grammars. `run_syntax_check`'s `sources` must be Fortran files staged in `project_dir` (the gcc driver reads `-B<dir>/` and `@file` out of the source list, and execs the `f951` it finds there). A make command-line assignment overrides even a hard assignment in that file, so this surface carries more authority than the environment. Until [issue #171](https://github.com/seiya/atmofab/issues/171) PR-2 the orchestrated arm held four more things: an allowlist of the six variable NAMES; a containment rule on the four whose value is a path; a rule that `project_dir` be absolute and that it and the resolved `command_log_path` resolve inside the repository — so a `command_log_path` outside the checkout is now accepted, and the recorded `cwd` is the string the caller passed rather than a resolved path; and an outright refusal of ANY `target`. The first three bounded a LEAF's grant and are retired with it; the fourth is not retired but generalized, since refusing a switch catches a defect in a caller this repository writes rather than bounding a grant. `run_program`'s `command` is **not** covered: it is caller-chosen argv by design.
- The operation of directly calling `gcc` / `clang` / `gfortran` for a one-off build is forbidden.
- `run_linter` is the tool for `Generate`'s `static lint`. Rather than via `compile_project` or a `Makefile`'s `lint` target, it launches `fortitude` / `cppcheck` / `ruff` with only the `preset`. `preset=mixed` runs `fortitude` and `cppcheck` in order. It is outside the scope of the norm that requires `compile` to go through a standard build tool. No preset's argv is spelled in the server: each is declared by that linter's own backend package and read through `tools/backends/registry.py` (`docs/BACKEND_BOUNDARY.md`; issue #111 for `fortitude`, issue #120 for the other two). What each declaration carries is the rule set the gate applies and the flags that keep a verdict a function of the source — `--select` plus `--isolated` for `fortitude` and `ruff`, and for all three the closure of the in-source suppression channel a leaf can write (`--ignore-allow-comments`, `--ignore-noqa`, and for `cppcheck` the DELETION of `--inline-suppr`, which the server used to pass). Each backend's document under `docs/backends/linter/` states what its invocation does and does not close; `cppcheck`'s states a weaker property than the other two, because that tool has no way to select a rule set. `preset=mixed` has no argv of its own and stays in the neutral core, which is why it is the one `linter` record still carrying `lint` in `core_provides`.
- When `run_program` is given `target.class=cpu` (or `target_class=cpu`) and `threads_per_rank`, it auto-sets `OMP_NUM_THREADS` and `OMP_THREAD_LIMIT`.
- `run_quality_checks` allows only the `preset` specification, and forbids the execution of an arbitrary `command`.
- The `preset=pytest` of `run_quality_checks` prepends `project_dir` to `PYTHONPATH` to ensure the reproducibility of import resolution.
- `run_linter` allows only the `preset` specification, and forbids the execution of an arbitrary `command`.
- `run_syntax_check` is the tool for `Generate`'s deterministic `Generate.syntax` gate: it runs a REGISTERED compiler adapter's syntax-only mode (`gfortran -fsyntax-only -std=<toolchain.standard> -Werror=unused-dummy-argument -Werror=unused-variable -Werror=ampersand`, module files into a throwaway `.mods` scratch dir inside `project_dir`) over the staged Fortran sources in module/use dependency order. Those three warning classes — and only those — are promoted to errors: the sanctioned binding for an intentionally-unused dummy argument is the `associate` idiom in `docs/workflow/CHECKS_MODULE_CONTRACT.md` §5, and a continued character literal must resume with a leading `&` (gfortran accepts a resume line without one as an extension; issue #25 rejects it here so the line-anchored `!$omp` presence floor cannot be evaded from inside a string). Because it produces **no build artifacts**, it is lint-class, not a build — like `run_linter` it is outside the scope of the norm that requires `compile` to go through a standard build tool (the "no one-off `gfortran`" rule above targets builds). Adapters are a registry (`_SYNTAX_COMPILER_ADAPTERS`; currently `gfortran`) — a future target compiler (e.g. Fujitsu `frt`, whose adapter may compile with `-c` into the scratch dir) is added by extending the registry, and the conductor selects stages via the `ATMOFAB_SYNTAX_COMPILERS` env var (default `gfortran`; a stage whose compiler binary is absent is reported `skipped`). It forbids the execution of an arbitrary `command`.
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
    "target.class": "cpu",
    "threads_per_rank": 8,
    "timeout_sec": 1800
  }
}
```

An example of `Generate`'s `static lint` (assuming `fortran`):

```json
{
  "name": "run_linter",
  "arguments": {
    "project_dir": "/path/to/workspace/pipelines/<node_key_safe>/<pipeline_id>/generate/<generation_id>/src",
    "command_log_path": "command_log.jsonl",
    "preset": "fortitude",
    "timeout_sec": 1800
  }
}
```
