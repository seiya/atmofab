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
| `check-phase-certified` | is the target phase already CERTIFIED by the artifacts on disk? The phase has an eligible output under its `derivation key` recomputed now (`docs/ORCHESTRATION.md` §13a, [issue #250](https://github.com/seiya/atmofab/issues/250) PR-2): a stage meta recording `verification_status: pass`, not revoked, whose deliverables still hash to the recorded `artifact_hashes` and whose stamped `derivation_key` equals the key over today's inputs — the selected output among several, whichever run produced it (the answer carries its `ir_ref` / `pipeline_ref` / `source_id` / `binary_id` / `run_id`, the key and the `output_hash`) — and, for the IR, the CURRENT `validate_pipeline_semantics --stage compile` accepts it (issue #238: the same validator `Compile.static` runs, so a rule added after certification refuses the skip and `Compile` re-derives). On yes the phase is recorded as `skipped_certified` in `phase_state.json` — except under `--no-record`, the conductor's ask for a phase named in `--rederive`, which runs although certified and must not be recorded skipped. On no, the conductor re-runs the phase and records NO event carrying the reason (`run_phase` discards it), so the reason table below is read by running this subcommand by hand | the canonical skip-decision path, on EVERY run — a cold run reads it the same way a `--resume` does (there is no resume-only skip). A skip must not be decided by a direct reference to `step_result.json` |
| `revoke-artifact` | rewrite the stage meta of `--step` to `verification_status: revoked`, keeping every other key and recording `prior_verification_status` / `revoked_at` / `revoked_by_agent_run_id` / `revocation_reason`, and `last_fail_reason` / `revocation_severity` when given | the half of a re-derivation decision that reaches the ARTIFACT — and therefore the half a COLD re-run reads, since it consults no record of the orchestration that made the decision. The conductor calls it on every retry route (same-phase, cross-phase, and the dev rollback, which revokes before it terminalizes). Downstream phases need no revocation of their own: each binds, in its derivation key, to the output hash of the phase above it, so a re-derived phase that changes the output moves every downstream key. `noop` when no meta was written, or when the pipeline's `lineage.json` is missing, corrupt or not an object (reason `no_meta`) — never an error. Every phase certifies a meta since [issue #250](https://github.com/seiya/atmofab/issues/250) PR-1 (Validate's is `runs/<run_id>/<node_key_safe>/validate_meta.json`, resolved through `lineage.json#run_id`; before that `--step validate` answered a `step_certifies_no_meta` noop). `check-phase-certified` reads `validate_meta.json` since PR-2 of that issue, so a revoked Validate is refused (`revoked`) like every other phase. A `no_meta` `noop` over a phase that is STILL `certified` is the exception: it means the decision did not reach the artifact, so both the conductor (fail_closed, reason_code `revocation_not_landed`) and the CLI (exit 1) refuse it. The answer carries `still_certified`, resolved by a READ-ONLY predicate — asking `check-phase-certified` instead would write `skipped_certified`, the state the completion vouch reads as an exemption. Repeatable: a second revocation keeps the first `prior_verification_status` and takes the latest values for everything else, including clearing `revocation_severity` when the latest decision carries no grade; `last_fail_reason` is the phase's own field and is overwritten only when findings are given. `--severity` and `--repair-strategy` record the G5 decision so a resumed repair reproduces it; they are needed TOGETHER, because `major` defaults to `reuse` while honouring an explicit `restart` and the grade alone cannot reconstruct that. Without them the repair re-enters as `major` / `reuse`. `--last-fail-reason-from-stdin` because a findings excerpt runs to several thousand characters |
| `reset-phase` | reset `--from-phase` and every phase downstream of it to `not_started`, recording a `phase_reset` event with the routing reason | a RECORD, not a gate: `record-launch` has no phase-state precondition, so a phase can be re-run without it. It is what an operator and the completion vouch read back — a phase left at `step_result_written` while its artifact is revoked describes a run that is not happening. Paired with `revoke-artifact` on every retry route |

## `check-phase-certified` refusal reasons

The `reason` a refusal reports is what a `phase_state_log.jsonl` entry and the
`cannot mark orchestration pass: <node>/<phase> is not certified: <reason>` message hand back.
Each one says which link of the chain refused, so the remedy is "re-derive that phase", never
"edit the record".

| reason | what it means | what to do |
|---|---|---|
| `ir_not_found` / `source_not_found` / `binary_not_found` / `verdict_not_found` | the phase has no output at all under `workspace/` (no stage directory carries its certifying meta) | the run did not get that far; start or `--resume` it |
| `derivation_key_missing` | no output of the phase carries a `derivation_key` — every one was certified before [issue #250](https://github.com/seiya/atmofab/issues/250) PR-1 stamped keys (the legacy corpus), or by a writer that skipped the stamp (a FAILED attempt carries none either: `write-step-result` strips the stamp on a non-pass, and it is not what the diagnosis reads — the mismatch below is diffed against a KEYED output) | re-run the phase (a cold run re-derives it; nothing backfills a key) |
| `derivation_key_mismatch:<input>` | an output exists but its stamped `derivation_inputs` differ from the inputs recomputed now — diffed against the keyed output CLOSEST to today's inputs (fewest differing inputs; the latest on a tie), so a stale sibling stamped under other inputs does not name what separates IT from today — and `<input>` is the FIRST differing one in sorted-key order (`spec.controlled_spec`, `closure[0].source`, `dependency_graph`, `toolchain.compiler_version`, `transformation`, …; `docs/ORCHESTRATION.md` §13a) | re-run the phase — a cold run does this by itself. When `<input>` is a `closure[…]` member, the dependency moved: `--with-deps` re-certifies the closure bottom-up |
| `derivation_inputs_unresolvable: …` | the key cannot be computed: the message names the input — a dependency with no certified output under ITS key (its own reason in parentheses), a closure that does not build from `deps.yaml` + the catalog, a spec file that cannot be read | build the dependency closure first (`--with-deps`), or fix the named registry / spec defect |
| `verification_status_not_pass` | the phase's own verify did not certify it | re-run the phase |
| `revoked` | a retry revoked it deliberately; `last_fail_reason` carries the finding | re-run the phase (the conductor does this automatically) |
| `artifact_hashes_missing` | the stage meta was never stamped (an older artifact, or a phase that never passed) | re-run the phase |
| `artifact_hash_mismatch:<ref>` | the named deliverable's bytes changed after it was certified | restore it, or re-run the phase to re-derive it |
| `ir_rejected_by_current_validator:<n>:<first finding>` | the certified IR fails a rule the compile-stage validator applies today (`n` findings; the first is quoted, truncated) — status, hashes and the key are intact | `--resume`; `Compile` re-derives the IR. No `revoke-artifact` is needed, and `--with-deps` adds nothing (a dependency's IR is not re-validated by readiness, `docs/ORCHESTRATION.md` §13c) |
| `ir_validator_raised:<type>` | the compile-stage validator raised instead of answering; only the exception TYPE travels in the reason | run `python3 tools/validate_pipeline_semantics.py --stage compile --ir-ref <ir_ref>` (the `ir_ref` is in the same JSON answer) for the traceback; this is a defect in the validator or the IR directory, not a stale artifact |
| `spec_ref_unresolved` | the node's spec directory could not be resolved to exactly ONE catalog entry with a path (an entry without paths, two entries for one `spec_id`/version at different directories, or the node absent from `spec/registry/spec_catalog.yaml`) — the key cannot be computed without its spec files | fix the catalog entry; nothing on `workspace/` is stale |
| `dependency_cycle` | the node was reached again while its own selection was in progress (a `dependency_graph.json` sidecar naming the node inside its own closure) | a malformed sidecar; re-derive Compile after fixing `deps.yaml` |
| `stage_meta_unreadable` / `node_key_invalid` / `unsupported_step:<step>` | a malformed record, or a step that is not one of the four derivation steps | inspect it; this is a defect, not a stale artifact |

## Argument-acquisition path

Confirm the required / optional arguments and return-value schema of each subcommand with the following command.

```bash
python3 tools/orchestration_runtime.py <subcommand> --help
```

The argparse output includes the description / the help string of all arguments, and provides details in a way that complements this doc. (A leaf reads no CLI help and invokes no subcommand since Z4, [issue #171](https://github.com/seiya/atmofab/issues/171); the hook policies that used to observe and bound those reads are deleted with it.)

## Links to exception recovery flows

- the use condition of `record-timeout`'s `--force-reason`: `docs/RUNBOOK.md#substep-timeout-recovery`
- the recovery for an incomplete launch (dangling active_child window / `reason_code=launch_incomplete_active_child`), and reading the `launch_incident.runtime.*.json` diagnostics snapshot via `python3 tools/audit_orchestration.py --orchestration-id <id>` ("Dangling launch" section): `docs/RUNBOOK.md#launch-incomplete-recovery`
- the whole resume flow including `check-phase-certified`: [docs/RUNBOOK.md](RUNBOOK.md) §3-1 (the conductor drives resume; `tools/workflow_conductor.py` is the implementation)
