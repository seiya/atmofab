# bwrap leaf-sandboxing verification runbook (live re-verification)

Procedure to verify the leaf bwrap sandbox under a real `claude -p` or `codex exec --json` run. bwrap leaf
sandboxing is **unconditionally mandatory** (Linux + user-namespaces only); there is no
opt-out. Use this runbook to re-verify the sandbox end-to-end after a change to the
profile builder, the leaf-launch path, or the build toolchain.

- **Design / rationale (canonical):** `docs/design/deterministic_conductor.md` §Leaf
  sandboxing. Do not restate the design here — this file is only the operator procedure
  and the pass/fail criteria.
- **Why a live run is required:** `record-launch` builds a per-arid bwrap profile and
  records `sandbox_enforced: true`, and `spawn_leaf` wraps every leaf in
  `render_bwrap_command`. A unit test confirms the wrapping, but only a full `claude -p`
  run exercises real auth, the MCP `build-runtime` spawn, hooks firing, the
  `--session-id` transcript, and — the highest risk — the **compile/build toolchain
  writing `.o`/`.mod`/exe inside the leaf's `write_roots`** under the sandbox.

## 0. Preconditions

1. The host supports unprivileged user namespaces and has `bwrap` on `PATH`
   (`bwrap --version` works as the invoking user). WSL2 / some container hosts disable
   user namespaces — if so, this host is unsupported (the conductor fails closed there,
   which is the correct behavior, not a regression). bwrap is Linux-userns-only.
2. Claude backend preflight already passes (MCP `build-runtime` registered + tool
   permission granted): see `docs/RUNBOOK.md` §0-2. Run the normal preflight first.
3. **Run this standalone.** It is a billed, autonomous `--run-conductor` orchestration; do
   not run it concurrently with other manual workflow activity or it will pollute the
   workspace-global baseline (the truth is `meta=pass` + `aggregate_verdict`; a polluted
   parallel run can false-fail). Use a clean workspace state.
4. **Do not set `METDSL_ORCHESTRATION_ASSUME_BWRAP`.** That env var is a test-only
   affordance that makes the preflight probe *assume* bwrap is available (so unit/
   integration tests can drive the enforced launch path without bwrap installed). On a
   real host it would only mask a missing sandbox — the run must verify bwrap for real.
5. For Codex, set `CODEX_HOME` to the writable Codex state directory when a non-default
   location is required. `METDSL_HOME` is a deprecated compatibility alias. The two variables
   must resolve to the same path when both are set.

## 1. Run one node end-to-end under the sandbox

Pick a small leaf component and run through `validate` so every phase — including the
high-risk **Build** — executes under bwrap:

```
! python3 tools/run_workflow.py \
    spec/component/dynamics/advection_diffusion/dynamics_advdiff_flux_1d_upwind_center2 \
    validate --llm-config configs/llm/claude.yaml
```

bwrap enforcement is unconditional, so the conductor wraps every leaf in
`render_bwrap_command` with no extra flags. (The `!` prefix runs it in this session so
its output lands in the conversation.)

## 2. Pass criteria

The run must reach `orchestration_meta.json` `status=pass` with a real
`aggregate_verdict` (not a sandbox/launch error). Concretely confirm all of:

| Check | How to confirm |
|---|---|
| Leaves actually ran sandboxed | each `agents/<arid>/dialogs/child.response.json` of a CHILD-PROCESS leaf has `sandbox_enforced: true` **and** a `sandbox_command` starting with `bwrap`; the leaf produced a real reply (not an immediate launch error). A leaf answered over HTTPS from the conductor's own process instead carries `leaf_transport: "http"`, `sandbox_enforced: false` and no `sandbox_command` — it runs no model-directed tool, so there is nothing to confine (see `docs/ORCHESTRATION.md` "Leaf LLM configuration") |
| Real auth + `--session-id` transcript worked | `~/.claude/projects/<slug>/<session_id>.jsonl` exists and has assistant turns for each leaf (auth/config-home bind is functional) |
| MCP `build-runtime` invoked | the deterministic conductor substeps (`generate.gate` / `build` / `validate.execute`, run in-process — not LLM leaves) recorded `run_linter` / `run_syntax_check` / `compile_project` / `run_program` evidence (`command_log.jsonl` present, `ok:true`) |
| Hooks fired in-sandbox | the run completed without a `*_violation` due to a missing hook decision; gate-friction behavior is unchanged |
| **Build output landed in write_roots (highest risk)** | the **Build phase passed** — `compile_project` wrote `.o`/`.mod` to the per-run object dir and the exe to `binary/<binary_id>/bin/` with no `unauthorized_write_violation` / EROFS. This is the make-or-break check. |

`python3 tools/audit_orchestration.py <orchestration_id>` summarizes per-run cost and
status for a quick read.

## Codex session and hook criteria

For a Codex run, confirm that every leaf launch has a distinct `thread.started` event before a
tool request, and that `session_run_index.json`, `launches/<agent_run_id>.response.json`, and the
terminal `agent_run.json` record that thread ID as `agent_session_id`. A missing or conflicting
thread ID is a launch failure. `codex exec resume <thread_id>` continues the recorded thread in
place; it is not a Claude-style fork.

For an M3c node, Codex pure Generate uses `codex exec --json --output-schema` with the
CLI read-only sandbox and the outer read-only bwrap profile. The host validates and writes the
returned bundle/verdict, so the leaf has no repository write authority. Its recorded isolation
level is `sandboxed_structured_approximation`; it is not equivalent to Claude
`closed_tool_free` isolation.

`codex exec` and `codex exec resume` do **not** accept the same options: `resume` has no
`--sandbox`. A pure repair turn therefore re-pins the read-only policy with
`--config sandbox_mode="read-only"` rather than inheriting it from the resumed thread. Confirm this
on a live run: every pure repair attempt must reach a `thread.started`, not exit at argv
parsing. The `codex_exec_resume` preflight check probes `exec resume --help` for exactly the
option set the resume argv emits (`CODEX_EXEC_RESUME_REQUIRED_FLAGS`); probing `exec --help`
alone would certify a command the CLI rejects.

The leaf prompt is **not** an argv element on either backend. A single argv element is capped
at `MAX_ARG_STRLEN` (128 KiB on Linux) and a node's rendered prompt exceeds it, so `execve`
fails with `E2BIG` before the model starts. The conductor writes the prompt to the leaf's
stdin instead: `claude -p` with no prompt argument, and `codex exec` / `codex exec resume`
with the `-` stdin sentinel as the positional prompt.

Both forms are certified at preflight, by different probes because the two CLIs fail
differently. `codex_prompt_stdin` requires BOTH codex helps to document the sentinel: a
codex that stopped treating `-` as a sentinel would take it as a one-character prompt and
answer something plausible, so the loss is silent. `claude_prompt_stdin` runs `claude -p`
on empty input and requires the refusal to name `stdin`; that path fails loudly rather
than silently, but the claude contract is the ABSENCE of a positional argument, which
`claude --help` documents nowhere, so the CLI's own refusal is the only machine-readable
statement of it. Both probes cost zero tokens and reach no model.

The whole JSONL event stream of each Codex leaf is kept at
`agents/<arid>/dialogs/leaf.stdout.jsonl`. `leaf.stdout.log` holds only the extracted final
`agent_message`, so the `.jsonl` file is the sole record of what the leaf actually did and is
where a failed Codex leaf is diagnosed.

For every Codex orchestration, the host creates an isolated `CODEX_HOME` outside the repository.
It contains only a SHA-256-verified copy of this repository's `.codex/hooks.json`; the original
home contributes only `auth.json` as a read-only bwrap bind. Its `config.toml` marks the repository
project `untrusted`, preventing the project hook layer from being loaded a second time. Therefore
`--dangerously-bypass-hook-trust` applies only to that verified user-level hook source, never to
ambient user or plugin hooks. The same isolated home is reused by `codex exec resume` for the
orchestration's thread state.

## 3. If it fails

- **Build fails with a write/`Read-only file system`/`unauthorized_write_violation`
  error.** The build toolchain wrote outside the bwrap write scope. The fix is to add the
  offending path to the leaf's bwrap write scope: `build_bwrap_profile`
  (`tools/orchestration_runtime.py`) confines writes to the capability `write_roots`
  + `workspace/tmp/<arid>`. Identify the path from the error and ensure the Build phase's
  `write_roots` (or the `OBJDIR`/`BINDIR` overrides `Build` passes to `compile_project`)
  resolve under an authorized root. Re-run step 1 after the profile fix.
- **`SandboxError` / leaf raises before launching.** The host lacks a usable bwrap profile
  or user namespaces. Confirm precondition 0.1; the conductor failing closed here is
  correct — this host is unsupported, do not work around it.
- **Preflight rejects** with `sandbox_not_enforced`: expected only if a profile is missing;
  see `docs/RUNBOOK.md` §0-2 and the design doc.
