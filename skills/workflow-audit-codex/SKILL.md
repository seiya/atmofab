---
name: workflow-audit-codex
description: Use this when investigating the orchestration logs of an executed workflow and surfacing/reporting hook blocks, information-gathering behavior, and redos due to check failures. The target orchestration_id and session_id are auto-detected. Codex-only
---

# Workflow Audit For Codex

## Purpose
Investigate the logs of a completed or interrupted workflow execution across the board, and enumerate problems in the following 3 categories.

1. **hook blocks** - operations for which a hook returned `action=block`
2. **information-gathering behavior** - places where, due to unclear CLI specifications or insufficient state awareness, a `--help` reference or file exploration was performed
3. **redos due to check failures** - gate / validator failures, multiple phase-launch attempts, status-setting mistakes

## Log collection sources

| log | collection source |
|---|---|
| hook blocks | `workspace/orchestrations/<orch_id>/hooks/native_hook_events.jsonl` |
| workflow hook history | `workspace/orchestrations/<orch_id>/hooks/workflow_hooks.jsonl` |
| agent execution results | `workspace/orchestrations/<orch_id>/agent_runs.jsonl` |
| phase state transitions | `workspace/orchestrations/<orch_id>/phase_state_log.jsonl` |
| gate results | `workspace/orchestrations/<orch_id>/gates/<agent_run_id>/*.json` |
| sandbox violations | `workspace/orchestrations/<orch_id>/violations/*.json` |
| access logs | `workspace/orchestrations/<orch_id>/access_logs/<agent_run_id>.jsonl` |
| failure analysis | `workspace/orchestrations/<orch_id>/failure_analysis.json` |
| session conversation log | `<codex-home>/sessions/YYYY/MM/DD/rollout-*-<session_id>.jsonl` |

> **`<codex-home>` is the ISOLATED per-orchestration home, read from
> `workspace/orchestrations/<orch_id>/orchestration_meta.json#codex_workflow_home`** — not
> `~/.codex`. A workflow leaf has run against a private home since before issue #63, so
> `~/.codex/sessions` holds an operator's own codex sessions; for a run that used the
> isolated home, none of the run's are there. Issue #64 made that private home durable at
> `~/.met-dsl/homes/<orch_id>/codex`, which is why this audit works at all after a host
> restart; before it, a rollout was gone with the next reboot.
>
> **The Step 2 script searches `~/.codex/sessions` anyway, and that is a deliberate
> fallback for a run predating the isolated home — so read its output with the root it
> prints.** A hit under `~/.codex/sessions` for a run that DID use an isolated home is an
> operator's own session that happens to carry the same id, not the leaf's; the script
> prints the roots it searched and the full path of every match so the two can be told
> apart.
>
> **Operator context only.** Both of those paths are protected read roots for Bash — the
> credential/session home and, since issue #64, `~/.met-dsl` — so the guard rejects these
> reads fail-closed whenever `METDSL_WORKFLOW_MODE=1` (policies
> `forbid_backend_credential_direct_read` / `forbid_operator_secret_direct_read`;
> canonical: `docs/HOOKS.md` §"Layer boundary"). Run this audit from an operator terminal
> outside a workflow run — inside one, every such read blocks, and that block is correct.

## Investigation procedure

### Step 1 - Identify the orchestration_id

```bash
ls workspace/orchestrations/
```

When there are multiple targets, choose the most recent `orch_YYYYMMDDTHHMMSSZ_*` directory.
To investigate a specific orchestration, use the instructed `orchestration_id`.

### Step 2 - Auto-detect the session_id

Read the `payload_summary.session_id` recorded in `native_hook_events.jsonl`, and identify the corresponding `rollout-*.jsonl` under the orchestration's own codex home.

```bash
python3 - <<'EOF'
import json
import pathlib

orch_id = "<orchestration_id>"
hook_log = pathlib.Path(f"workspace/orchestrations/{orch_id}/hooks/native_hook_events.jsonl")
session_ids = set()
for line in hook_log.read_text().splitlines():
    if not line.strip():
        continue
    obj = json.loads(line)
    sid = obj.get("payload_summary", {}).get("session_id")
    if isinstance(sid, str) and sid.strip():
        session_ids.add(sid.strip())

# The isolated home the HOST recorded, with `~/.codex` kept only as the fallback for a
# run that predates the isolated home.
# Degrades to the operator's home rather than raising, the way
# `skills/workflow-timing-audit/scripts/analyze_timing.py` does: a run whose metadata is
# missing or unreadable is exactly the kind this audit is opened for.
try:
    meta = json.loads(
        pathlib.Path(f"workspace/orchestrations/{orch_id}/orchestration_meta.json").read_text())
except (OSError, ValueError):
    meta = {}
if not isinstance(meta, dict):
    meta = {}   # a metadata document that is valid JSON but not an object
recorded = (meta.get("codex_workflow_home") or "").strip()
roots = [pathlib.Path(recorded) / "sessions"] if recorded else []
roots.append(pathlib.Path.home() / ".codex/sessions")
print(f"searching: {[str(r) for r in roots]}")
for sid in sorted(session_ids):
    matches = sorted(m for root in roots for m in root.glob(f"**/rollout-*-{sid}.jsonl"))
    if matches:
        print(f"{sid}: found")
        for p in matches:
            print(f"  {p}")
    else:
        print(f"{sid}: NOT FOUND")
EOF
```

For each detected session_id, fix the path of the corresponding `rollout-*.jsonl`.

### Step 3 - Extract hook blocks

Extract all records with `action=block` from `native_hook_events.jsonl`.

```bash
python3 - <<'EOF'
import json

orch_id = "<orchestration_id>"
path = f"workspace/orchestrations/{orch_id}/hooks/native_hook_events.jsonl"
blocks = []
with open(path) as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        if obj.get("action") == "block":
            blocks.append(obj)

for b in blocks:
    print(json.dumps(b, ensure_ascii=False, indent=2))
EOF
```

Record the following for each block.

- `ts` - the time of occurrence
- `tool_name` - the blocked tool (`Read` / `Bash` / `Write` etc.)
- `reason` - the block reason
- `audit_detail.policy` - the applied policy name
- `payload_summary` - the operation-target path or command (first 200 characters)

### Step 4 - Extract information-gathering behavior

Extract `function_call` records from Codex's `rollout-*.jsonl`, and detect `--help` calls, file-exploration commands, and `grep` / `sed` / direct `Read` of the runtime implementation.

```bash
python3 - <<'EOF'
import json

SESSION = "<session_jsonl_path>"

patterns = [
    "--help",
    "grep -n",
    "grep -rn",
    "sed -n",
    "find .",
    "ls /home",
    "ls workspace",
    "cat tools/",
]

results = []
with open(SESSION) as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("type") != "response_item":
            continue
        payload = obj.get("payload", {})
        if payload.get("type") != "function_call":
            continue
        name = payload.get("name")
        args_raw = payload.get("arguments", "{}")
        try:
            args = json.loads(args_raw)
        except Exception:
            args = {}
        if name == "exec_command":
            cmd = args.get("cmd", "")
            if not isinstance(cmd, str):
                continue
            for pat in patterns:
                if pat in cmd:
                    results.append({"tool": "exec_command", "match": pat, "command": cmd[:300]})
                    break
        elif name == "read_mcp_resource":
            uri = args.get("uri", "")
            if isinstance(uri, str) and "tools/" in uri:
                results.append({"tool": "read_mcp_resource", "match": "tools/ direct read", "uri": uri})

for r in results:
    print(json.dumps(r, ensure_ascii=False))
EOF
```

Classify the extracted results by the following perspectives.

- `--help` references - places where the CLI specification was unclear (argument format, subcommand name, etc.)
- `tools/` direct `grep` / `sed` / `read` - places where a rule was attempted to be derived from the runtime implementation (forbidden by hook policy)
- file-existence confirmation (`ls`, `find`) - state awareness before phase-artifact generation

### Step 5 - Extract check failures and redos

#### 5-a. Multiple phase-launch attempts

When the `pre_phase_launch` of the same `node_key + step` appears 2 or more times in `workflow_hooks.jsonl`, a redo due to a launch failure has occurred.

```bash
python3 - <<'EOF'
import json
from collections import Counter

orch_id = "<orchestration_id>"
path = f"workspace/orchestrations/{orch_id}/hooks/workflow_hooks.jsonl"
counter = Counter()
entries = []
with open(path) as f:
    for line in f:
        obj = json.loads(line.strip())
        entries.append(obj)

for e in entries:
    if e.get("hook") == "pre_phase_launch":
        key = f"{e.get('node_key')}::{e.get('step')}"
        counter[key] += 1

for key, cnt in counter.items():
    if cnt > 1:
        print(f"RETRY x{cnt}: {key}")
EOF
```

#### 5-b. Gate failures and re-execution counts

When `hook=pre_command_execute` and the same `gate` appears multiple times in `workflow_hooks.jsonl`, a fix loop after a gate failure has occurred.

```bash
python3 - <<'EOF'
import json
from collections import Counter

orch_id = "<orchestration_id>"
path = f"workspace/orchestrations/{orch_id}/hooks/workflow_hooks.jsonl"
counter = Counter()
with open(path) as f:
    for line in f:
        obj = json.loads(line.strip())
        if obj.get("hook") == "pre_command_execute" and obj.get("gate"):
            key = f"{obj['gate']}::{obj.get('step')}"
            counter[key] += 1

for key, cnt in counter.items():
    if cnt > 1:
        print(f"GATE RETRY x{cnt}: {key}")
EOF
```

For the actual gate-failure content, read `gates/<agent_run_id>/<gate_name>.json` and confirm the `violations` field.

```bash
ls workspace/orchestrations/<orch_id>/gates/
python3 -c "
import json, pathlib
orch_id = '<orchestration_id>'
for p in sorted(pathlib.Path(f'workspace/orchestrations/{orch_id}/gates').rglob('*.json')):
    obj = json.loads(p.read_text())
    if obj.get('status') != 'pass':
        print(p)
        print(json.dumps(obj, ensure_ascii=False, indent=2))
        print()
"
```

#### 5-c. Confirm sandbox violations

```bash
ls workspace/orchestrations/<orch_id>/violations/
python3 -c "
import json, pathlib
orch_id = '<orchestration_id>'
for p in sorted(pathlib.Path(f'workspace/orchestrations/{orch_id}/violations').glob('*.json')):
    print(p.name)
    print(json.dumps(json.loads(p.read_text()), ensure_ascii=False, indent=2))
    print()
"
```

#### 5-d. Confirm fail/fail_closed in phase_state_log

```bash
python3 -c "
import json
orch_id = '<orchestration_id>'
with open(f'workspace/orchestrations/{orch_id}/phase_state_log.jsonl') as f:
    for line in f:
        obj = json.loads(line.strip())
        if obj.get('event') in ('set_status',) and obj.get('to') in ('fail', 'fail_closed'):
            print(json.dumps(obj, ensure_ascii=False))
"
```

### Step 6 - Report the results

Report grouped into the 3 categories in the following format.

---

#### 1. Blocked by hooks

Enumerate each block in a table.

| time (UTC) | agent | tool | policy | operation target |
|---|---|---|---|---|
| ... | ... | ... | ... | ... |

Group those with the same policy and explain the cause in one line.

#### 2. Performed information gathering

Enumerate `--help` references, `tools/` `grep`, and state-awareness `ls` / `find` respectively, and add one sentence on what was unclear.

#### 3. Redos due to check failures

Enumerate multiple phase-launch attempts, gate-failure loops, sandbox violations, and status-setting mistakes chronologically, and note the cause and final result of each redo.

---

## Notes

- During workflow execution, the implementation under `tools/` is forbidden to read directly by hook policy. Derive workflow rules by referencing only `docs/` and `spec/`. During repository improvement, maintenance, testing, and refactoring, `tools/*.py` may be inspected directly.
- The session `jsonl` can be tens of thousands of lines. Do not read all lines from the top; extract only the necessary fields with `python`.
- When `payload_summary.session_id` is missing, prefer the `agent_session_id` of `agent_runs.jsonl`, and reverse-look it up with `<codex-home>/sessions/**/rollout-*-<agent_session_id>.jsonl`, resolving `<codex-home>` from `orchestration_meta.json#codex_workflow_home` as in Step 2.
