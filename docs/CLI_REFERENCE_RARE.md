# CLI Reference (rare subcommand overview)

## Position of this document

An overview of the **infrequently used** rare subcommands of `tools/orchestration_runtime.py`. For the detailed argument specification, `python3 tools/orchestration_runtime.py <sub> --help` is the canonical source.

For the detailed specification of the frequent subcommands (Tier-A), refer to [docs/CLI_REFERENCE.md](CLI_REFERENCE.md). The information-acquisition policy per tool / subcommand uses the "Information-acquisition policy" section of [docs/CLI_REFERENCE.md](CLI_REFERENCE.md) as the canonical source.

Related canonical sources:
- frequent subcommand details: [docs/CLI_REFERENCE.md](CLI_REFERENCE.md)
- workflow operation / startup: [docs/RUNBOOK.md](RUNBOOK.md) (operator procedure) and [docs/ORCHESTRATION.md](ORCHESTRATION.md) (conductor/orchestration contract)
- exception recovery procedures: [docs/RUNBOOK.md](RUNBOOK.md)

## Common conventions

- `--repo-root` / `--orchestration-id` are **required** in (almost) all subcommands.
- ISO 8601 timestamps are canonically UTC (`Z` suffix).
- For the detailed arguments (required / optional / default values), confirm with `<sub> --help`.

## Rare subcommand list

| subcommand | purpose | main caller / situation |
|---|---|---|
| `init` | start an orchestration / generate `orchestration_meta.json` | usually launched via `tools/run_workflow.py`. A direct call is for exceptional operation only. `--agent-model <id>` records the orchestration agent's own model on its `agent_runs.jsonl` row (`run_workflow.py` passes the operator's unpinned claude alias — e.g. `opus`, read from `~/.claude/settings.json` — by default for the claude backend; never a pinned version, which would go stale) |
| `preflight` | execution-platform launchability probe / generate `preflight.json` | called internally by `tools/run_workflow.py`. A manual call is forbidden |
| `preflight-status` | read back an existing `preflight.json` | post-launch state confirmation |
| `record-timeout` | the canonical recovery path for an `Agent` tool API stream idle timeout etc. | manual finalization of a child agent that produced no terminal entry while the driver is alive (a leaf that merely WEDGES is killed and terminalized by the conductor's own per-leaf cap — `docs/RUNBOOK.md#substep-timeout-recovery`). `--force-reason` is the last resort for a marker-check bypass |
| `check-phase-certified` | is the target phase already CERTIFIED by the artifacts on disk? The stage meta records `verification_status: pass`, is bound to the current artifact of the phase above it (`source_ir_id` / `source_source_id` / `trial_meta.source_binary_id`), its deliverables still hash to the recorded `artifact_hashes`, and the dependency freshness the readiness stages apply holds. On yes the phase is recorded as `skipped_certified` in `phase_state.json` | the canonical skip-decision path, on EVERY run — a cold run reads it the same way a `--resume` does (there is no resume-only skip). A skip must not be decided by a direct reference to `step_result.json` |
| `check-step-completed` | with `resume_enabled=true`, confirm the completion state of the target step | superseded by `check-phase-certified` as the skip-decision path; retained for the remaining checkpoint-ledger readers |
| `orchestration-read` | the gate-mediated, audited re-read of a path **inside** the manifest (an out-of-manifest path is not granted: it records a `rule_source_violation` and fails the orchestration) | usually called via `run-gate --gate orchestration_read --args-json '{"read_path": "..."}'` |
| `reopen-phase` | reopen a checkpointed-pass phase (`--from-phase`) and every downstream phase for `--node-key`, so a cross-phase retry (`Validate.judge` `structural_violation`/`ir` → Compile, or `Generate.verify` `ir_inconsistency` → Compile) runs in place. Snapshots the prior attempt's step/substep runs as superseded (exempt from the pass-completion vouch), archives their `step_result.json` aside to `step_result.superseded.<seq>.json`, **revokes the `from_phase` stage meta** (`verification_status: revoked` — the half that reaches the ARTIFACT, so `check-phase-certified` refuses it in this run and in every later one, cold included), drops the affected `completed_steps` checkpoint entries, and resets the affected `phase_state` to `not_started` | used by the orchestration agent when the decision table routes a `Validate` / `Generate` failure back to an already-passed `Compile` (the `pass` upstream phase cannot otherwise be re-pointed: `check-step-completed` reads the stale IR as `integrity=ok`, the phase sits at `step_result_written`, and `retry_decisions` only models within-step retries). Idempotent. `--trigger-agent-run-id` must be a terminal non-pass step/substep strictly downstream of `--from-phase` (the anti-abuse gate — refuses to erase a passing pipeline). The trigger is resolved from `agent_runs.jsonl`; when absent there it falls back to an `agent_runs_invalid.jsonl` entry **only if** a matching `violations/<arid>.unauthorized_write_violation.json` exists (the recovery path for a phase whose failure mode *is* an unauthorized write — that run is diverted to the invalid log and would otherwise be an unusable trigger; the result/log records `trigger_source`). On `--resume` of an `attribution=ir` failure, or an unauthorized-write failure attributed to a single upstream phase, `orchestration_meta.resume_directive` records the parameters to feed here (for details, `RUNBOOK.md` §3-1) |
| `add-superseded-runs` | tombstone `--run-ids` into the superseded set (`reopen/superseded_runs.json`) without a reopen, so they are exempt from the pass-completion vouch | used by the conductor when a phase attempt fail-closes on a leaf transport error (e.g. the `Validate.judge` leaf hit a Claude session limit, `rc!=0`): the attempt's already-terminalized substep agents are recorded in `agent_runs.jsonl` but have no `step_result` (the attempt never wrote one), so on a later `--resume` (which re-runs the phase fresh) `_validate_orchestration_completion_for_pass` would flag them as orphans. Unlike `reopen-phase` this archives no `step_result` and needs no trigger. Idempotent (merges into the existing set); appends an `add_superseded_runs` line to `reopen/reopen_log.jsonl` |

## `check-phase-certified` refusal reasons

The `reason` a refusal reports is what a `phase_state_log.jsonl` entry and the
`cannot mark orchestration pass: <node>/<phase> is not certified: <reason>` message hand back.
Each one says which link of the chain refused, so the remedy is "re-derive that phase", never
"edit the record".

| reason | what it means | what to do |
|---|---|---|
| `ir_not_reserved` / `pipeline_not_reserved` | this orchestration never reserved that phase root | the run did not get that far; start or `--resume` it |
| `ir_not_found` / `pipeline_not_found` | the reserved root does not exist on disk | as above |
| `ir_not_latest` / `pipeline_not_latest` | a NEWER artifact exists under the same root, so this run is standing on a superseded one | re-run the node; the readiness stages evaluate the latest artifact, and a skip must not disagree with them |
| `verification_status_not_pass` | the phase's own verify did not certify it | re-run the phase |
| `revoked` | a retry revoked it deliberately; `last_fail_reason` carries the finding | re-run the phase (the conductor does this automatically) |
| `artifact_hashes_missing` | the stage meta was never stamped (an older artifact, or a phase that never passed) | re-run the phase |
| `artifact_hash_mismatch:<ref>` | the named deliverable's bytes changed after it was certified | restore it, or re-run the phase to re-derive it |
| `source_not_bound` / `binary_not_bound` / `verdict_not_bound` | the artifact names a different upstream id than the one standing | re-run that phase against the current upstream |
| `resolution_stale:<detail>` / `binding_stale:<detail>` | a dependency moved (`docs/ORCHESTRATION.md` §13b); the detail names it | `--with-deps` re-certifies the closure bottom-up |
| `post_judge_not_recorded` / `post_judge_not_pass` | the Validate gate did not record a pass, whatever the verdict says | re-run Validate |
| `stage_meta_unreadable` / `node_key_invalid` | a malformed record | inspect it; this is a defect, not a stale artifact |

## Argument-acquisition path

Confirm the required / optional arguments and return-value schema of each subcommand with the following command.

```bash
python3 tools/orchestration_runtime.py <subcommand> --help
```

The argparse output includes the description / the help string of all arguments, and provides details in a way that complements this doc. The `--help` call itself is outside the scope of `forbid_tools_direct_read`, and its usage frequency is recorded by the `cli_help_invocation_observed` audit policy of `tools/hooks/common.py` (it is not blocked).

## Links to exception recovery flows

- the use condition of `record-timeout`'s `--force-reason`: `docs/RUNBOOK.md#substep-timeout-recovery`
- the recovery for an incomplete launch (dangling active_child window / `reason_code=launch_incomplete_active_child`), and reading the `launch_incident.runtime.*.json` diagnostics snapshot via `python3 tools/audit_orchestration.py --orchestration-id <id>` ("Dangling launch" section): `docs/RUNBOOK.md#launch-incomplete-recovery`
- the whole resume flow including `check-phase-certified`: [docs/RUNBOOK.md](RUNBOOK.md) §3-1 (the conductor drives resume; `tools/workflow_conductor.py` is the implementation)
