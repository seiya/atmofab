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
| `orchestration-read` | the gate-mediated, audited re-read of a path **inside** the manifest (an out-of-manifest path is not granted: it records a `rule_source_violation` and fails the orchestration) | usually called via `run-gate --gate orchestration_read --args-json '{"read_path": "..."}'` |
| `revoke-artifact` | rewrite the stage meta of `--step` to `verification_status: revoked`, keeping every other key and recording `prior_verification_status` / `revoked_at` / `revoked_by_agent_run_id` / `revocation_reason`, and `last_fail_reason` / `revocation_severity` when given | the half of a re-derivation decision that reaches the ARTIFACT — and therefore the half a COLD re-run reads, since it consults no record of the orchestration that made the decision. The conductor calls it on every retry route (same-phase, cross-phase, and the dev rollback, which revokes before it terminalizes). Downstream phases need no revocation of their own: each binds to the id of the phase above it, so a re-derived phase leaves them unbound. `noop` when the step certifies no meta (`validate`), when none was written, or when the pipeline has no `lineage.json` naming one — never an error. The conductor treats a `noop` over a phase that is STILL certified as fail-closed, because there the word means the decision did not reach the artifact. Repeatable: a second revocation keeps the first `prior_verification_status` and takes the latest values for everything else. `--severity` records the G5 grade so a resumed repair can choose `restart` over `reuse`; without it the repair re-enters as `major`. `--last-fail-reason-from-stdin` because a findings excerpt runs to several thousand characters |
| `reset-phase` | reset `--from-phase` and every phase downstream of it to `not_started`, recording a `phase_reset` event with the routing reason | a RECORD, not a gate: `record-launch` has no phase-state precondition, so a phase can be re-run without it. It is what an operator and the completion vouch read back — a phase left at `step_result_written` while its artifact is revoked describes a run that is not happening. Paired with `revoke-artifact` on every retry route |

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
