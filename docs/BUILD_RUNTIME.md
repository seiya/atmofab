# Build-runtime library

## Purpose
`tools/build_runtime.py` runs `compile` / `run` / `quality check` / `static lint` / `syntax check` for the workflow. The conductor imports it and calls its entry points in-process; no `LLM` leaf calls it (a `pure-function leaf` holds no tool). This document is canonical for the entry points, the validation they apply, and the command-log record they write. `AGENTS.md` §Build-runtime execution rules and `docs/ORCHESTRATION.md` point here.

The library was an MCP server (`mcp_servers/build_runtime_server.py`) until [issue #444](https://github.com/seiya/atmofab/issues/444), which deleted the protocol layer, the client and the configuration and kept the functions. The `tool_name` values written to `command_log.jsonl` are unchanged.

## Entry points
Each entry point takes one `dict` of arguments and returns one `dict`. An argument outside the rules below raises `ValueError`. A key the entry point does not read is ignored.

| Entry point | `tool_name` in the command log | Conductor caller |
|---|---|---|
| `tool_compile_project` | `compile_project` | `Build` (`Conductor._build_inproc`) |
| `tool_run_program` | `run_program` | `Validate.execute` |
| `tool_run_quality_checks` | `run_quality_checks` | `Validate.execute` |
| `tool_run_linter` | `run_linter` | `Generate.gate` (static lint) |
| `tool_run_syntax_check` | `run_syntax_check` | `Generate.gate` (syntax check) |

The module also exports the facts a build or run at a remote execution site needs without calling an entry point: `build_command`, `build_system_executable`, `default_build_jobs`, `COMPILE_PROJECT_TIMEOUT_SEC`, `RUN_PROGRAM_TIMEOUT_SEC`, `QUALITY_CHECKS_TIMEOUT_SEC`, `quality_check_command`, `_append_command_log`, and the preset and adapter lookups `tools/host_prerequisites.py` reads.

## Operations rules
- `compile_project` allows only standard build tools that can handle dependencies.
- For a compiled language (one whose language backend declares `COMPILED`, `registry.is_compiled_language`), when the build tool is unspecified, the default is `make`, and `compile_project` accepts only `make` / `cmake` / `meson` / `ninja`. An unspecified `build_system` falls back to marker-file detection (`_recommended_build_system`); the conductor always passes one explicitly (`_build_inproc` reads it from the run's target profile, `_read_toolchain`), so the fallback is not what a workflow build uses. The argv a build system runs (`build_command`; a build system whose registry record carries `build_execute` in its package answers from that package — `make`'s `build_argv` since [issue #424](https://github.com/seiya/atmofab/issues/424) — and the library's own table answers the build systems no backend owns), the default timeout (`COMPILE_PROJECT_TIMEOUT_SEC`) and the default parallelism (`default_build_jobs()`) are module-level so that a build at a remote execution site, where `tool_compile_project` is not called, can be handed what this library would have used (issue #333).
- **ONE validation mode, for every caller.** `orchestration_id` and `agent_run_id` are optional ATTRIBUTION: the library records them in the `command_log.jsonl` entry so a command can be traced back to the run that issued it, and decides nothing from them. The workflow environment variables (`ATMOFAB_ORCHESTRATION_ID`, `ATMOFAB_WORKFLOW_MODE`) are not read at all. Until [issue #171](https://github.com/seiya/atmofab/issues/171) PR-2 those names switched the server into a second, ALLOWLIST mode and a `capability_token` was required with them; the mode existed because the caller might be a LEAF whose grant had to be bounded, and no leaf reaches this library. The refusal of the retired arguments (`capability_token`, and `run_program`'s `target_class` / `target.class` / `target` / `threads_per_rank`) was deleted in [issue #444](https://github.com/seiya/atmofab/issues/444): the only callers are the conductor's calls (eight, in the five functions the entry-point table names), none of which sends one, and such a key is now ignored like any other key the entry point does not read.
- **The content of `env`, `target` and `extra_args` is not validated.** The library checks their TYPES (below) and nothing about what they say: an `env` name, an `env` value, a `target` and an `extra_args` element reach the command as the caller composed them. Under the workflow the conductor is the only caller, and it composes all three from host constants — `extra_args` from the build system backend's `build_overrides` (the object and binary directories and the binary name), no `target` at all, `run_program`'s `env` from the target profile's parallel backend (`host_execution.launch_shape`), and the quality check's from `quality_check_env` (four host paths, the binary name `<spec_id>_runner`, and `CASES`, whose case ids the Compile gate and the conductor's `read_case_ids` hold to `CASE_ID_TOKEN_RE`). A denylist over execution-redirecting names (`LD_*`, `PATH`, `MAKEFLAGS`, `.SHELLFLAGS`, `SHELL`, `MAKE`, …), a refusal of switches and of make's `NAME!=` shell assignment, and a refusal of shell-active characters in a value ran here until [issue #457](https://github.com/seiya/atmofab/issues/457) deleted them: no caller composed a value they could refuse, and a leaf reaches none of these arguments. One consequence is the OPERATOR's: make takes `.SHELLFLAGS`, `MAKE` and `MAKEFLAGS` from the environment and `SHELL` from its command line (measured on GNU Make 4.3: `env '.SHELLFLAGS=-c ./evil.sh' make all` runs `./evil.sh` instead of the recipe and exits 0), so such a name must not appear in a target profile's launch environment or in a backend's `build_overrides`; nothing refuses it any more. `run_program`'s `command` is caller-chosen argv by design. This is a separate question from the leaf's own process environment, which `orchestration_runtime.LEAF_ENV_ALLOWLIST` decides.
- One constraint on where the checkout lives follows: no directory a conductor call names — a node's `src/`, `binary/<id>/bin/`, `ir/<id>/`, `workspace/tmp/<agent_run_id>/…` — may be a symlink pointing out of the checkout. That was enforced by the containment checks, which resolved symlinks and failed such a call; those checks went with the leaf's grant in [issue #171](https://github.com/seiya/atmofab/issues/171) PR-2, so it is now a property of the checkout the operator is responsible for rather than one the library refuses. The checkout path must also hold no whitespace and none of `` ;&|$`'"\<>()*?[]{}~#! ``: the build control file interpolates these paths unquoted into a recipe line, so such a path produces a word-split recipe, a command the recipe's shell reinterprets, or — for a glob character matching another directory — a recipe that silently runs the binary at that other path and exits 0 (measured on GNU Make 4.3: `BINDIR=<dir>/ck[12]/bin` beside a `ck1/bin/r` ran `ck1/bin/r`). The library refused the metacharacters by name until [issue #457](https://github.com/seiya/atmofab/issues/457); nothing refuses such a checkout now, so keeping its path plain is the operator's responsibility, as the symlink constraint is.
- The type rules apply to every call: `target` must be a string and `extra_args` an array of strings; a non-string element is refused rather than coerced. `jobs`, `timeout_sec` and `capture_limit` are held to the minimums `_bounded_int` enforces (1, 1, 1000); a value below its minimum is refused rather than clamped, because `make -j-5` waits forever.
- Required arguments: `run_program` requires `command` (a non-empty array); `run_linter` requires `preset`; `run_syntax_check` requires `compiler` and `std`. `project_dir` defaults to `.` on every entry point; the conductor always passes it.
- `run_syntax_check` applies its source rule to the list it discovers itself, not only to an explicit `sources` argument — a staged file named like an option (`-o.<ext>`) is a gate failure, not a skipped file. Discovery walks `project_dir` at ANY depth (a symlinked directory is not descended into), and the rule admits the top level only, so a source below it is a gate failure too, whose message tells the author to move the file to the top level ([issue #420](https://github.com/seiya/atmofab/issues/420): the build compiles and links a nested source, so a walk that skipped it left linked code unchecked). A direct caller that omits `sources` over such a directory is refused where it used to be served a partial check.
- The operation of directly calling `gcc` / `clang` / `gfortran` for a one-off build is forbidden.
- `run_linter` is the entry point for `Generate`'s `static lint`. Rather than via `compile_project` or a `Makefile`'s `lint` target, it launches the preset's linter with only the `preset`. The served presets are the registry's: every linter record carrying `lint` in `backend_provides` (`fortitude` / `cppcheck` / `ruff` / the CUDA compiler driver `nvcc` today), plus each composite in `registry.COMPOSITE_LINTERS`, which runs its members in the order declared there (`preset=mixed` runs `fortitude` and `cppcheck`; issue #424 moved that declaration out of this module so the post_generate validator reads the same one). It is outside the scope of the norm that requires `compile` to go through a standard build tool. No preset's argv is spelled in the library: each is declared by that linter's own backend package and read through `tools/backends/registry.py` (`docs/BACKEND_BOUNDARY.md`; issue #111 for `fortitude`, issue #120 for the other two). What each declaration carries is the rule set the gate applies and the flags that keep a verdict a function of the source — `--select` plus `--isolated` for `fortitude` and `ruff`, and for those three the closure of the in-source suppression channel a leaf can write (`--ignore-allow-comments`, `--ignore-noqa`, and for `cppcheck` the DELETION of `--inline-suppr`, which the library used to pass). The `nvcc` argv closes no in-source suppression — no flag of the driver disables a diagnostic pragma — so for `cuda_cpp` that channel is closed by the language's source gate instead (`docs/backends/language/cuda_cpp/CHECKS_ABI.md` §5, the preprocessor allowlist). Each backend's document under `docs/backends/linter/` states what its invocation does and does not close; `cppcheck`'s states a weaker property than the other two, because that tool has no way to select a rule set. `preset=mixed` has no argv of its own and stays in the neutral core, which is why it is the one `linter` record still carrying `lint` in `core_provides`. A linter's backend also says whether it walks a directory or is handed files (`SOURCE_SUFFIXES` in its `lint` module, `None` for a directory walker; issue #289, R4-b PR-4): the `cuda_cpp` linter, the CUDA compiler driver, walks nothing, so the library hands it every file of its suffixes under `project_dir`, at any depth, by name and `./`-prefixed so no leaf-chosen name reads as an option; a directory holding none runs nothing and is reported clean, which is what a directory walker reports for it.
- `run_program` interprets no target. Its launch environment — a parallel model's thread variables among it — is the caller's, passed as `env` (under the workflow, `tools/host_execution.py` composes it from the target profile, issue #289). At a remote execution site the library's entry point is not called: `tools/remote_execution.py` runs the same command there and writes the same `command_log.jsonl` entry through this library's `_append_command_log`, with the timeout this library would have applied (`RUN_PROGRAM_TIMEOUT_SEC` / `QUALITY_CHECKS_TIMEOUT_SEC`) and the quality-check argv of its preset table (`quality_check_command`), passed explicitly (issue #293). `target` is `compile_project`'s build goal and is not read by `run_program`.
- `run_quality_checks` allows only the `preset` specification (default `make_test`), and forbids the execution of an arbitrary `command`. A preset's argv is its build-system backend's when one owns it (`make_test` / `make_check`: the `make` backend's `build_execute.QUALITY_CHECK_COMMANDS`, issue #424), and the library's own table's otherwise (`ctest`, `pytest`); a preset name declared by two of them refuses the module at import.
- The `preset=pytest` of `run_quality_checks` prepends `project_dir` to `PYTHONPATH` to ensure the reproducibility of import resolution.
- `run_linter` allows only the `preset` specification, and forbids the execution of an arbitrary `command`. `preset` is REQUIRED (issue #289, R4-b PR-2): which linter a node is linted with is its language's answer (`registry.linter_for_language`, from each linter backend's `LANGUAGES`, and for a composite's own token from `registry.COMPOSITE_LINTERS`), and the default it had was one language's linter.
- `run_syntax_check` is the entry point for `Generate`'s deterministic `Generate.gate` syntax check: it runs a REGISTERED compiler adapter's syntax-only mode over the staged sources, with module files written to a throwaway `.mods` scratch dir inside `project_dir`. The adapters are the `compiler` records that declare `syntax_check` in `tools/backends/registry.py` (the argv, the executable, the version probe and a canary source live in `tools/backends/compiler/<id>/syntax.py`); each names the LANGUAGE whose sources it reads, and that language's `syntax_promotions` (`tools/backends/language/<id>/syntax.py`) says which files are sources, their order, and which warning classes are promoted to errors (issue #289, R4-b PR-2 — the Fortran facts sat inline here until then; they are stated in `docs/backends/language/fortran/GENERATE_RULES.md` §2). `compiler` and `std` are REQUIRED — both are the caller's target facts, and a default was one language's spelling. `architecture` (the target profile's `hardware.architecture`) is accepted and passed to the adapter; an adapter whose compiler takes no device architecture does not read it. Because it produces **no build artifacts**, it is lint-class, not a build — like `run_linter` it is outside the scope of the norm that requires `compile` to go through a standard build tool (the "no one-off compiler call" rule above targets builds). A new target compiler is added as a backend package declaring `syntax_check`, and the conductor selects stages via the `ATMOFAB_SYNTAX_COMPILERS` env var (default the language's mandatory compiler, `bundle_facts.MANDATORY_SYNTAX_COMPILER`; a stage whose compiler binary is absent, or whose adapter reads another language, is reported `skipped`). It forbids the execution of an arbitrary `command`.

## Command log
- `compile_project` / `run_program` / `run_quality_checks` / `run_linter` / `run_syntax_check` always record the executed command in `JSONL` format. Where the conductor places each log is canonical in `docs/workflow/COMMAND_LOG_PLACEMENT.md`.
- The default for `command_log_path` when unspecified is `<project_dir>/command_log.jsonl`; a relative `command_log_path` resolves against `project_dir`.
- Each entry carries `version`, `command_id`, `tool_name`, `started_at_utc`, `ended_at_utc`, `elapsed_ms`, `cwd`, `command`, `executed_command`, `timeout_sec`, `capture_limit`, `env_override_keys` (the names, not the values), `ok`, `return_code`, `error` on a timeout, and the attribution fields when they are passed.
- The execution result returns `command_id`, `executed_command`, and `command_log_path`, and when the log is under the current working directory (the checkout, for the conductor), returns `command_log_ref`.

## Call examples
Building `fortran` with `make`:

```python
from tools.build_runtime import tool_compile_project

tool_compile_project({
    "project_dir": "/path/to/project",
    "command_log_path": "logs/build_commands.jsonl",
    "language": "fortran",
    "build_system": "make",
    "target": "all",
    "jobs": 8,
})
```

Running a binary:

```python
from tools.build_runtime import tool_run_program

tool_run_program({
    "project_dir": "/path/to/project",
    "command_log_path": "logs/run_commands.jsonl",
    "command": ["./bin/simulate", "--case", "case.resolved.yaml"],
    "env": {"OMP_NUM_THREADS": "8", "OMP_THREAD_LIMIT": "8"},
    "timeout_sec": 1800,
})
```

`Generate`'s `static lint` (assuming `fortran`):

```python
from tools.build_runtime import tool_run_linter

tool_run_linter({
    "project_dir": "/path/to/workspace/pipelines/<node_key_safe>/<target_id>/<pipeline_id>/generate/<generation_id>/src",
    "command_log_path": "command_log.jsonl",
    "preset": "fortitude",
    "timeout_sec": 1800,
})
```
