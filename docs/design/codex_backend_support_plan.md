# Codex Backend Support Restoration Plan

## Purpose

This plan implements GitHub issue
[seiya/met-dsl#13](https://github.com/seiya/met-dsl/issues/13) in one pull
request. The implementation keeps workflow contracts and validation
backend-neutral. Backend-specific behavior is confined to transport, session
management, hooks, resume, and usage-limit handling.

The Codex backend targets the current Codex CLI JSON Lines (JSONL) and session
contracts. A Codex pure-function leaf uses a sandboxed structured-output
approximation. The implementation records that isolation level separately from
the strict tool-free isolation provided by the Claude backend.

## Requirements

### Backend-neutral leaf execution

- Define a shared leaf execution result containing the exit status, final
  response, backend session ID, usage, model metadata, and normalized failure
  details.
- Run Codex with `codex exec --json` and consume JSONL incrementally.
- Require an explicit Codex model slug, pass it as `codex exec --model` on
  every initial and resumed leaf, and record that host-authored launch value as
  model provenance. Do not derive model provenance from JSONL or hook audit
  files because those are not authoritative model channels.
- When Codex emits `thread.started`, atomically register its `thread_id` against
  the allocated `agent_run_id` before processing later events.
- Keep the identifier responsibilities separate:
  - `agent_run_id` and `context_id` are stable, orchestration-owned identifiers.
  - `agent_session_id` is the Claude session ID or Codex thread ID.
- Fail closed when Codex reaches a hook before session registration, emits
  conflicting thread IDs, or terminates without a usable session ID.
- Store the real backend session ID in launch records, `agent_run.json`, and
  `session_run_index.json`. Do not assume that it equals `agent_run_id`.

### Hooks and Codex launch isolation

- Decode the current Codex `apply_patch` payload from
  `tool_input.command`. Retain `patch` and `patch_text` only as compatibility
  fallbacks.
- Reject a workflow-mode `apply_patch` call when the payload is missing,
  unparseable, or resolves to no target paths.
- Emit the current Codex `PermissionRequest` response envelope:
  `hookSpecificOutput.hookEventName = "PermissionRequest"` with an explicit
  allow or deny decision.
- Cover canonical Codex tool names and supported aliases in
  `leaf_config/codex/hooks.json` (this plan predates issue #102, which moved the leaf
  hook source there and left `.codex/hooks.json` as the DEV layer) without changing
  Claude behavior.
- Build a dedicated Codex launch configuration that:
  - loads only the repository's validated hooks;
  - ignores ambient user instructions and unrelated user hooks;
  - supplies repository trust explicitly;
  - enables hooks and bypasses only the validated project-hook hash check.
- Use `CODEX_HOME` as the canonical Codex configuration location. Support
  `METFORGE_HOME` temporarily as a deprecated compatibility alias and reject
  conflicting values.
- Preflight must parse the repository hook configuration and confirm that each
  workflow-relevant command resolves to
  `tools.hooks.cli --backend codex`.

### Capability preflight, resume, and failures

- Replace `multi_agent=true` as a launch requirement with checks for:
  - headless execution;
  - JSON event streaming;
  - thread-ID capture;
  - session resume;
  - enabled and loadable hooks;
  - a validated project-hook source;
  - structured-output support;
  - a writable backend state directory;
  - outer `bwrap` availability.
- Preserve the reported `multi_agent` state as advisory diagnostics only.
- Resolve warm-resume targets through `session_run_index.json`.
  - Claude forks from the recorded session.
  - Codex invokes `codex exec resume <thread_id>` and continues the same thread.
- Record the resume mode as `forked` or `in_place` in audit metadata.
- Normalize Codex JSONL `error` and `turn.failed` events and stderr into the
  existing infrastructure-failure classifications.
- Record `turn.completed.usage` when Codex emits it.
- For `--wait-usage-reset`, wait only when Codex reports a trustworthy,
  parseable reset time within the configured cap. Otherwise, return
  `reset_time_unavailable` and require manual resume. The implementation must
  not estimate a reset time.

### Pure Generate CLI approximation

- Move pure-leaf validation, `CodegenBundle` parsing, host-authored output
  writes, and error reporting into backend-neutral code.
- Run a Codex pure-function leaf with:
  - `codex exec --json --output-schema`;
  - an isolated scratch working directory;
  - ignored user configuration and instruction files;
  - the read-only Codex sandbox and outer `bwrap` profile;
  - no writable repository paths;
  - only the prompt inputs and generated output schema exposed.
- Apply the same result schema and semantic validators used by Claude.
- Record an explicit isolation level:
  - Claude: `closed_tool_free`.
  - Codex CLI: `sandboxed_structured_approximation`.
- Do not describe the Codex CLI approximation as equivalent to strict
  tool-free isolation. OpenAI Responses API integration is outside this plan.

## Verification

- Add fixture-driven tests for successful Codex JSONL execution, missing and
  conflicting thread IDs, failed turns, usage metadata, and resumable threads.
- Add hook tests for the canonical `apply_patch` payload, compatibility
  fallbacks, empty-target fail-closed behavior, and exact
  `PermissionRequest` allow and deny output.
- Add preflight tests proving that `multi_agent=false` is accepted when the
  required capabilities exist and that invalid, untrusted, or missing hooks are
  rejected.
- Add fake-Codex integration tests proving that session registration occurs
  after `thread.started` and before the initial simulated hook request.
- Add warm-resume tests proving that an `agent_run_id` resolves to the real
  Codex thread ID and invokes `codex exec resume`.
- Run focused orchestration, hook, pure-leaf, and CLI tests through the standard
  MCP server. Run the complete repository pytest quality check through the same
  server.
- Run the smallest complete workflow with Codex and Claude when the operator
  explicitly authorizes billed and networked execution. Acceptance requires
  equivalent phase progression, validation outcomes, host-authored artifacts,
  audit completeness, and successful resume behavior. The recorded isolation
  level may differ.

## Delivery Rules

- Deliver the changes in one pull request. Organize the implementation into
  reviewable commits for session transport, hooks and preflight, and pure
  Generate with tests and documentation.
- Keep the public workflow CLI syntax unchanged.
- Do not introduce an OpenAI API dependency or require API keys.
- Do not use an environment-provided `agent_run_id` as hook authorization. The
  host-owned session index remains authoritative.
- Treat the existing Codex `multi_agent` preflight test failures as baseline
  behavior to replace. Do not hide unrelated pre-existing failures.
- Update the orchestration, hooks, sandbox, and runbook documentation in the
  implementation pull request.
