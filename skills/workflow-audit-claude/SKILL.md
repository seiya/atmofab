---
name: workflow-audit
description: Use this when investigating the orchestration logs of an executed workflow and surfacing/reporting hook blocks, information-gathering behavior, and redos due to check failures. The target orchestration_id and session_id are auto-detected. Claude Code-only
---

# Workflow Audit

## Purpose
Investigate the logs of a completed or interrupted workflow execution across the board, and enumerate problems in the following 3 categories.

1. **hook blocks** — operations for which a hook returned `action=block`
2. **information-gathering behavior** — places where, due to unclear CLI specifications or insufficient state awareness, a `--help` reference or file exploration was performed
3. **redos due to check failures** — gate / validator failures, multiple phase-launch attempts, status-setting mistakes

## Log collection sources

| log | collection source |
|---|---|
| hook blocks | `workspace/orchestrations/<orch_id>/hooks/native_hook_events.jsonl` |
| workflow hook history | `workspace/orchestrations/<orch_id>/hooks/workflow_hooks.jsonl` |
| agent execution results | `workspace/orchestrations/<orch_id>/agent_runs.jsonl` |
| phase state transitions | `workspace/orchestrations/<orch_id>/phase_state_log.jsonl` |
| gate results | `workspace/orchestrations/<orch_id>/gates/<agent_run_id>/*.json`. **Audit from this file, not from the copy.** `run-gate` also leaves its stderr summary at `workspace/tmp/<agent_run_id>/gate_results/<gate>.json` so a leaf can re-read a result later; that copy is wiped at terminal status, so it is usually absent by the time you audit. The two are DIFFERENT documents, but they overlap in six fields: compare `gate`, `status`, `args_json`, `exit_code`, `evaluated_at` and `violations`. Only `gate_result_ref` is copy-only, and only `orchestration_id` / `agent_run_id` are record-only (plus a conditional `result` / `arg_validation_error`), so expect exactly that much difference and no more. `args_json` and `exit_code` are the two that say WHICH invocation produced a verdict, which is why they are the ones to compare rather than skip. A disagreement on any shared field IS worth investigating: neither file is out of a leaf's reach (`gates/<arid>/` is rw-bound into the leaf's sandbox, the terminal FS-diff exempts the prefix, and the interpreter route reaches both — `docs/HOOKS.md` §"Layer boundary"), so a mismatch is evidence about the run, not a dismissible one |
| sandbox violations | `workspace/orchestrations/<orch_id>/violations/*.json` |
| access logs | `workspace/orchestrations/<orch_id>/access_logs/<agent_run_id>.jsonl` |
| failure analysis | `workspace/orchestrations/<orch_id>/failure_analysis.json` |
| session conversation log | `<projects-root>/<cwd-slug>/<session_id>.jsonl` (`<cwd-slug>` is the repo's absolute path with `/` replaced by `-`). Since issue #63 a workflow LEAF writes into the orchestration's private home: `<projects-root>` is searched in BOTH places, private home first: `orchestration_meta.json#claude_workflow_home` + `/projects` when that key is present, and `~/.claude/projects` (the operator's own sessions, and any run recorded before that change) always. Not either/or — the Step 2 script below appends the operator root unconditionally, and an orchestration resumed across the issue-#63 migration has its older leaves in one and its newer ones in the other, so a session is resolved per id rather than per directory. Since issue #64 that private home is DURABLE (`~/.atmofab/homes/<orch_id>/claude`), so a leaf's transcript survives a host restart and this audit still finds it; before that it was under `/tmp` and went with the next reboot. It is kept indefinitely and removed only by an operator running `tools/prune_workflow_homes.py` — see `docs/RUNBOOK.md` §"The operator-private root". |

> **Operator context only.** Both roots in the row above are protected read roots for Bash
> — the backend CLI's credential/session home, and since issue #64 `~/.atmofab` — so the
> guard rejects these reads fail-closed whenever `ATMOFAB_WORKFLOW_MODE=1` (policies
> `forbid_backend_credential_direct_read` / `forbid_operator_secret_direct_read`;
> canonical: `docs/HOOKS.md` §"Layer boundary"). Run this audit from an operator terminal
> outside a workflow run — inside one, every such read blocks, and that block is correct.

## Investigation procedure

### Step 1 — Identify the orchestration_id

```bash
ls workspace/orchestrations/
```

When there are multiple targets, choose the most recent `orch_YYYYMMDDTHHMMSSZ_*` directory.
To investigate a specific orchestration, use the instructed `orchestration_id`.

### Step 2 — Auto-detect the session_id

Read the `payload_summary.session_id` recorded in `native_hook_events.jsonl`, and
identify the corresponding `.jsonl` file under `<projects-root>/<cwd-slug>/` (resolved as in the table above).
`<cwd-slug>` is the repo's absolute path with `/` replaced by `-` (e.g. `/home/alice/work/met-dsl` → `-home-alice-work-met-dsl`).

```bash
python3 - <<'EOF'
import json, pathlib

orch_id = "<orchestration_id>"   # substitute the value fixed in Step 1
hook_log = pathlib.Path(f"workspace/orchestrations/{orch_id}/hooks/native_hook_events.jsonl")
session_ids = set()
for line in hook_log.read_text().splitlines():
    if not line.strip():
        continue
    obj = json.loads(line)
    sid = obj.get("payload_summary", {}).get("session_id")
    if sid:
        session_ids.add(sid)

cwd_slug = str(pathlib.Path.cwd().resolve()).replace("/", "-")
# The isolated home the HOST recorded FIRST — that is where a leaf's transcript is — with
# the operator's `~/.claude` kept as the fallback for a run recorded before issue #63.
# This must match the table above; hardcoding `~/.claude/projects` here made the script
# report NOT FOUND for every leaf of every post-#63 run.
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
_raw = meta.get("claude_workflow_home")
# The VALUE's type as well as the document's: `(x or "").strip()` raises on a number or a
# list, and this is the audit you open when the metadata is damaged.
recorded = _raw.strip() if isinstance(_raw, str) else ""
projects_dirs = [pathlib.Path(recorded) / "projects" / cwd_slug] if recorded else []
projects_dirs.append(pathlib.Path.home() / ".claude/projects" / cwd_slug)
print(f"searching: {[str(d) for d in projects_dirs]}")
for sid in sorted(session_ids):
    found = [d / f"{sid}.jsonl" for d in projects_dirs if (d / f"{sid}.jsonl").exists()]
    if found:
        print(f"{sid}: found  ({found[0]})")
    else:
        print(f"{sid}: NOT FOUND  (looked in {len(projects_dirs)} roots)")
EOF
```

For each detected session_id, fix the path of the corresponding `.jsonl`.

### Step 3 — Extract hook blocks

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

- `ts` — the time of occurrence
- `tool_name` — the blocked tool (`Read` / `Bash` / `Write` etc.)
- `reason` — the block reason
- `audit_detail.policy` — the applied policy name
- `payload_summary` — the operation-target path or command (first 200 characters)

### Step 3.5 — Aggregate block counts per policy

Count the `blocks` obtained in Step 3 per policy, and highlight 5 or more as a **repeated error pattern**.

```bash
python3 - <<'EOF'
import json
from collections import Counter

orch_id = "<orchestration_id>"
path = f"workspace/orchestrations/{orch_id}/hooks/native_hook_events.jsonl"
policy_counter: Counter = Counter()
with open(path) as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        if obj.get("action") == "block":
            policy = (obj.get("audit_detail") or {}).get("policy", "unknown")
            policy_counter[policy] += 1

print("=== Policy block counts ===")
for policy, cnt in policy_counter.most_common():
    flag = " *** REPEATED ERROR PATTERN ***" if cnt >= 5 else ""
    print(f"  {policy}: {cnt}{flag}")
EOF
```

When a repeated error pattern (5 or more) is detected, refer to the corresponding row of the repair cheat sheet (`docs/RUNBOOK.md#hook-recovery`) to identify the root cause.

### Step 4 — Extract information-gathering behavior

Extract `--help` calls, file-exploration commands, and grep/sed of the runtime implementation from the session's `.jsonl`.

```bash
python3 - <<'EOF'
import json

SESSION = "<session_jsonl_path>"   # the path fixed in Step 2

patterns = ["--help", "grep -n", "grep -rn", "sed -n", r"find \.", "ls /home", "ls workspace", "cat tools/"]

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
        msg = obj.get("message", {})
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_use":
                continue
            name = block.get("name", "")
            inp = block.get("input", {})
            if name == "Bash":
                cmd = inp.get("command", "")
                for pat in patterns:
                    if pat in cmd:
                        results.append({"tool": "Bash", "match": pat, "command": cmd[:300]})
                        break
            elif name == "Read":
                fp = inp.get("file_path", "")
                if "tools/" in fp:
                    results.append({"tool": "Read", "match": "tools/ direct read", "file_path": fp})

for r in results:
    print(json.dumps(r, ensure_ascii=False))
EOF
```

Classify the extracted results by the following perspectives.

- `--help` references — places where the CLI specification was unclear (argument format, subcommand name, etc.)
- `tools/` direct grep/sed/read — places where a rule was attempted to be derived from the runtime implementation (forbidden by hook policy)
- file-existence confirmation (`ls`, `find`) — state awareness before phase-artifact generation

### Step 4.5 — Aggregate the utilization status of `audit_detail.fix_hint`

Classify whether the blocks in `native_hook_events.jsonl` carry an actionable `fix_hint` (`next_command`, or `note` where no command can work — a read block outside `allowed_read_roots` has no command that reaches the path) or were empty.
Focus on identifying cases where "the hint was provided but the agent ignored it and repeated the same operation".

```bash
python3 - <<'EOF'
import json
from collections import Counter, defaultdict

orch_id = "<orchestration_id>"
path = f"workspace/orchestrations/{orch_id}/hooks/native_hook_events.jsonl"
hint_present: Counter = Counter()   # policy → count of blocks WITH fix_hint
hint_absent: Counter = Counter()    # policy → count of blocks WITHOUT fix_hint
hint_ignored: defaultdict = defaultdict(list)  # policy → list of repeated commands

prev_commands: list[str] = []
with open(path) as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        if obj.get("action") != "block":
            continue
        policy = (obj.get("audit_detail") or {}).get("policy", "unknown")
        fix_hint = (obj.get("audit_detail") or {}).get("fix_hint")
        summary = obj.get("payload_summary") or {}
        detail = obj.get("audit_detail") or {}
        # Bash blocks carry `command`; Grep/Glob carry `path` (+ `pattern`).
        # Keying on `command` alone hides a search an agent retries in a loop,
        # and the raw dict fallback used to crash the slice below. Scope the key
        # to the agent and tool: two agents blocked once each on the same path
        # is not a retry.
        who = detail.get("agent_run_id") or summary.get("session_id") or ""
        target = summary.get("command") or "::".join(
            str(summary[k]) for k in ("path", "pattern", "file_path") if summary.get(k))
        cmd = "::".join(x for x in (who, obj.get("tool_name") or "", target) if x) if target else ""
        if fix_hint and (fix_hint.get("next_command") or fix_hint.get("note")):
            hint_present[policy] += 1
        else:
            hint_absent[policy] += 1
        if cmd and cmd in prev_commands:
            hint_ignored[policy].append(cmd[:200])
        prev_commands.append(cmd)

print("=== fix_hint present (structured recovery hint available) ===")
for p, c in hint_present.most_common():
    print(f"  {p}: {c}")
print("=== fix_hint absent (no structured hint — potential docs gap) ===")
for p, c in hint_absent.most_common():
    print(f"  {p}: {c}")
if hint_ignored:
    print("=== Hint possibly ignored (same command blocked multiple times) ===")
    for p, cmds in hint_ignored.items():
        print(f"  {p}: {len(cmds)} repeat(s)")
        for c in cmds[:3]:
            print(f"    {c}")
EOF
```

If there is a policy with many "hint_absent", add the corresponding row to `docs/RUNBOOK.md#hook-recovery` (the target of Stream B-3).

### Step 5 — Extract check failures and redos

#### 5-a. Multiple phase-launch attempts

Confirm the number of times the `pre_phase_launch` of the same `node_key + step` appears in `workflow_hooks.jsonl`.

**Note:** `pre_phase_launch` is written from both the `workflow-launch-check` command and the `record-launch` command.
When there are multiple substeps like a plan step, "1 workflow-launch-check + record-launch for the number of substeps" is the normal pattern, not a launch failure.
Compare with the number of agent_run_id present in the agents directory (`workspace/orchestrations/<orch_id>/agents/`), and judge it a retry only when the `pre_phase_launch` count exceeds "1 + the number of actually-launched agents".

```bash
python3 - <<'EOF'
import json, pathlib
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

# the number of actually-launched agents (those whose record-launch succeeded and a capability exists)
caps = list(pathlib.Path(f"workspace/orchestrations/{orch_id}/capabilities").glob("*.json"))
launched_per_step: Counter = Counter()
for p in caps:
    obj = json.loads(p.read_text())
    key = f"{obj.get('node_key')}::{obj.get('step')}"
    launched_per_step[key] += 1

for key, cnt in counter.items():
    expected = 1 + launched_per_step.get(key, 0)  # 1 for workflow-launch-check
    if cnt > expected:
        print(f"RETRY x{cnt - expected} (pre_phase_launch={cnt}, expected={expected}): {key}")
    else:
        print(f"OK (pre_phase_launch={cnt}, expected={expected}): {key}")
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
# confirm all gate results per agent_run_id
python3 -c "
import json, pathlib, sys
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

### Step 5.5 — Display the 5 hook events just before fail_closed chronologically

When `fail_closed` occurred, go back through the immediately preceding hook events to confirm what triggered it.

```bash
python3 - <<'EOF'
import json

orch_id = "<orchestration_id>"
hook_log = f"workspace/orchestrations/{orch_id}/hooks/native_hook_events.jsonl"
phase_log = f"workspace/orchestrations/{orch_id}/phase_state_log.jsonl"

# obtain the fail_closed timestamp from phase_state_log
fail_ts = None
with open(phase_log) as f:
    for line in f:
        obj = json.loads(line.strip())
        if obj.get("to") == "fail_closed" or obj.get("event") == "set_status" and obj.get("new_state") == "fail_closed":
            fail_ts = obj.get("ts") or obj.get("timestamp")

if fail_ts is None:
    print("No fail_closed event found in phase_state_log.")
else:
    print(f"fail_closed at: {fail_ts}")
    # Collect all hook events before fail_ts, take last 5
    events_before = []
    with open(hook_log) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            ts = obj.get("ts") or obj.get("timestamp", "")
            if ts <= fail_ts:
                events_before.append(obj)
    last_5 = events_before[-5:]
    print(f"\n=== Last {len(last_5)} hook events before fail_closed ===")
    for e in last_5:
        ts = e.get("ts") or e.get("timestamp", "?")
        action = e.get("action", "?")
        tool = e.get("tool_name") or (e.get("payload_summary") or {}).get("tool_name", "?")
        policy = (e.get("audit_detail") or {}).get("policy", "")
        summary = str(e.get("payload_summary", ""))[:120]
        print(f"  [{ts}] {action} | {tool} | {policy} | {summary}")
EOF
```

### Step 6 — Report the results

Report grouped into the 3 categories in the following format.

---

#### 1. Blocked by hooks

Enumerate each block in a table.

| time (UTC) | agent | tool | policy | operation target |
|---|---|---|---|---|
| … | … | … | … | … |

Group those with the same policy and explain the cause in one line.

#### 2. Performed information gathering

Enumerate `--help` references, `tools/` grep, and state-awareness `ls` / `find` respectively, and add one sentence on **what was unclear**.

#### 3. Redos due to check failures

Enumerate multiple phase-launch attempts, gate-failure loops, sandbox violations, and status-setting mistakes chronologically, and note the **cause** and **final result** of each redo.

#### 4. Summary of repair hints (new)

From Step 3.5 / 4.5 / 5.5, summarize the **legitimate action the agent should take next** per policy.

| policy | block count | fix_hint present/absent | recommended action |
|---|---|---|---|
| read_manifest_read_guard | … | … | the path is outside `allowed_read_roots`. In-manifest paths are read directly (`Read` / `Grep` / `Glob` / a `Bash` reader); an out-of-manifest path is unreadable by every route — `run-gate orchestration_read` fails the orchestration for it — so the fix is a read re-issued under `allowed_read_roots`, or a relaunch with a corrected manifest. `audit_detail.via == "bash"` marks a Bash block; for the other routes read the record's top-level `tool_name` (`Read` / `Grep` / `Glob`) |
| output_manifest_write_guard | … | … | write with the `Write` tool to the literal path of `allowed_tmp_root` (`workspace/tmp/<agent_run_id>/...`). Bootstrap Bash such as `export TMPDIR=...` / `jq -er ...` is forbidden (the workflow stops on a Claude Code session sandbox approval) |
| forbid_python_inline_write | … | … | use the Edit/Write tool |
| forbid_tools_direct_read | … | … | during workflow execution, reference only `docs/` / `spec/` |
| forbid_unauthorized_file_write | … | … | write directly with the Edit/Write tool; ensure the path is in `allowed_file_tool_paths` |

Highlight a repeated error pattern (5 or more) in bold, and add the corresponding line number of `docs/RUNBOOK.md#hook-recovery`.

---

## Notes

- During workflow execution, the implementation under `tools/` is forbidden to read directly by hook policy. Derive workflow rules by referencing only `docs/` and `spec/`. During repository improvement, maintenance, testing, and refactoring, `tools/*.py` may be inspected directly.
- The session `.jsonl` can be tens of thousands of lines. Do not read all lines from the top; extract only the necessary fields with Python.
- When the orchestration agent and a child agent are mixed under the same session_id (on the Claude backend they are recorded in the same session), determine the agent_role not by `payload_summary.session_id` but by `capabilities/<agent_run_id>.json` corresponding to the `agent_run_id` of `native_hook_events.jsonl`.
