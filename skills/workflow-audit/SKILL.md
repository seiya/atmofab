---
name: workflow-audit
description: Use this when investigating why a workflow orchestration failed or fail-closed, which substeps it attempted more than once, whether sandbox enforcement recorded a violation, or what the run cost. Runs tools/audit_orchestration.py over the in-repo records of one orchestration; backend-neutral (claude_cli / codex_cli / HTTP providers).
---

# Workflow Audit

## Purpose
Enumerate, for one `orchestration`, the following five findings from the records the host wrote under `workspace/orchestrations/<orchestration_id>/`.

1. **Terminal state and failure diagnosis** — every `fail` / `fail_closed` transition, and the host's own `failure_analysis.json`.
2. **Redos** — `substep`s attempted more than once, and runs rejected at terminal validation.
3. **Sandbox enforcement violations** — the records under `violations/`.
4. **Token cost per leaf** — the `usage` rows of `agent_runs.jsonl`.
5. **Dangling launch** — an `active_child` window left open by a driver that died mid-launch.

The audit reads in-repo records only. The one read outside the checkout is the leaf transcript tail that finding 5 correlates, which `tools/audit_orchestration.py` performs itself (`## Transcripts`).

## Scope
- Run from an operator terminal at the checkout root. The audit is not part of a workflow run and is never launched by one.
- Read-only. No artifact under `workspace/` is modified.
- Backend-neutral: every record the audit reads is written by the host, so a `claude_cli`, `codex_cli`, `openai_compatible` or `anthropic_api` leaf is audited by the same command.

## Canonical sources
- Record placement and the meaning of each file: `docs/WORKSPACE_LAYOUT.md`.
- The tool's arguments, output formats and exit codes: `python3 tools/audit_orchestration.py --help` (`docs/CLI_REFERENCE.md` makes `--help` canonical for this tool).
- The dangling-launch section and the recovery it leads to: `docs/RUNBOOK.md` §"Incomplete launch recovery" (`{#launch-incomplete-recovery}`). Exit code `2` is defined there and in `--help`; this document does not restate it.

## Procedure

### Step 1 — Fix the orchestration_id

```bash
ls workspace/orchestrations/
```

Take the instructed `orchestration_id`, or the newest `orch_YYYYMMDDTHHMMSSZ_*` directory when none is instructed.

### Step 2 — Run the tool

```bash
python3 tools/audit_orchestration.py --orchestration-id <orchestration_id> --format markdown
```

`--format json` returns the same content as one document for a script. Exit code `2` means the report cannot be trusted at face value; read the `## ⚠` sections first and follow `docs/RUNBOOK.md` §"Incomplete launch recovery" for the cause before concluding anything from the other sections.

### Step 3 — Read the sections

Each section states one conclusion and names the record to open for the detail.

- **`## Phase state failures`** — every `fail` / `fail_closed` transition (a `set_status` row) with its `reason_code` and `reason_detail`, and the `fail_closed` instant. The row is orchestration-level and names no node or step; which agent run failed is the next section's. Detail: the matching line of `phase_state_log.jsonl`.
- **`## failure_analysis`** — the host's diagnosis: `reason_code`, the failed agent run (`agent_run_id`, node, step, substep), the failed `step_result.json` paths, and each sidecar with its `existing_file_status`. The line carries `orchestration_meta.json#status` so that an absent file is read against the terminal status: absent on a `pass` run is normal, absent on a `fail` / `fail_closed` run is a finding. Detail: `launches/<agent_run_id>.reply.txt`, `agents/<agent_run_id>/dialogs/agent.result.json`, and the named `step_result.json`.
- **`## Dangling launch (active_child window)`** — read as `docs/RUNBOOK.md` §"Incomplete launch recovery" instructs.
- **`## Sandbox enforcement violations`** — the record that bwrap confinement was in force, or that it failed. One writer, five reasons: `sandbox_profile_build_failed` at `record-launch`; `sandbox_runtime_not_bwrap`, `sandbox_not_enforced`, `sandbox_profile_missing` and `sandbox_profile_not_found` at `record-agent-run`. bwrap is the only confinement a leaf runs under, so any of the five is a finding about that leaf's launch. Three states are rendered and must be read apart: `violations/` absent (nothing recorded — the directory is created only when a violation occurs), directory present with no record (pre-created by a run before issue #171 PR-2), and sandbox enforcement records present (counted per reason, then listed with `agent_run_id` and `evaluated_at`). A record of another `kind` (a writer that no longer exists) is listed apart under its kind and is not a sandbox enforcement finding. Detail: `violations/<agent_run_id>.sandbox_enforcement_violation.json`, and the leaf's `sandbox_profiles/<agent_run_id>.json`.
- **`## Token cost (per leaf)`** — the leaf total, its reasoning and prompt-cache shares, the provider-reported cost, and a per-leaf table ranked by total. `not_measured` rows are deterministic in-process substeps that launched no leaf and are not a gap; `unavailable` rows are leaves whose usage channel failed and are a gap. `docs/GLOSSARY.md` §`per-leaf usage` defines both markers.
- **`## Pure-leaf A/B metrics`** — per node, the attempt and repair-turn counts and the per-attempt usage of each pure leaf. Detail: the `*_meta.json` next to the node's artifact (`docs/WORKSPACE_LAYOUT.md`).
- **`## agent_runs summary`** — the run count per status, the rows missing `finished_at`, and `Repeated substeps` — every `(node_key, step, substep)` with more than one attempt, with the status sequence. A run rejected at terminal validation (`agent_runs_invalid.jsonl`) is counted under its status, typically `fail`.

### Step 4 — Report

Report in three groups, each item with its cause and its final result.

1. **Terminal state and failure diagnosis** — the terminal status, the `reason_code`, the failed agent run and the record that explains it.
2. **Redos** — each repeated substep in chronological order: how many attempts, which statuses, and what changed between them (from the launch replies of the attempts).
3. **Sandbox enforcement** — each violation with its reason and `agent_run_id`, or the statement that none was recorded (naming which of the two negative states the tool printed).

Close with one line on cost: the leaf total and the most expensive leaf.

## Transcripts
The audit needs no transcript. A transcript is the raw stream a CLI leaf's backend kept; everything a `pure-function leaf` received is `launches/<agent_run_id>.prompt.txt`, and everything it returned is `launches/<agent_run_id>.reply.txt` and `agents/<agent_run_id>/dialogs/`. A pure leaf launches with no tool, so a transcript holds no tool call to inspect. Where one exists, per backend:

- **`claude_cli`**: `~/.claude/projects/<cwd-slug>/<agent_run_id>.jsonl`, where `<cwd-slug>` is the checkout's absolute path with `/` replaced by `-`, and the session id is the leaf's `agent_run_id`. The CLI expires sessions, so a transcript is often absent for a run older than the retention window; `tools/audit_orchestration.py` locates it through `tools/operator_private_root.py::claude_leaf_projects_roots` for the dangling-launch section and reads it for nothing else.
- **HTTP providers (`openai_compatible` / `anthropic_api`)**: no transcript exists. The raw provider response is `launches/<agent_run_id>.http_response.txt`.
- **`codex_cli`**: `<orchestration_meta.json#codex_workflow_home>/sessions/YYYY/MM/DD/rollout-*-<agent_session_id>.jsonl`, the orchestration's own durable home under `~/.atmofab/homes/<orchestration_id>/codex` (`docs/RUNBOOK.md` §"The operator-private root"; `tools/prune_workflow_homes.py` is the only thing that removes it). `agent_session_id` is read from `session_run_index.json`, which maps `agent_run_id` to it. `~/.codex/sessions` holds the operator's own sessions, not a leaf's.

## Notes
- The conductor is a Python process with no session of its own; the row with `agent_role: orchestration` in `agent_runs.jsonl` is the conductor's own record and is excluded from the per-leaf cost.
- A transcript can be tens of thousands of lines. Extract the needed fields rather than reading from the top.
