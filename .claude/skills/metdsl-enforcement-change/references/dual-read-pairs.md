# Facts read in more than one place (known set)

When adding a new reader, or changing an existing one, **find the counterpart before you
change anything**. In PR #51 this defect shape appeared five times (one of them P1-grade). The
test is always: **is there an input the leaf can write for which the two readings disagree?**
A disagreement that falls to the safe side (the stricter reading) may stay, but write down why.

## Pairs that exist today

| Fact | Reader A | Reader B | State |
|---|---|---|---|
| `impl_defaults.toolchain.build_system` | conductor `str(tc.get(...) or "make").lower()` | `_impl_defaults_toolchain_value` (structural read, `.strip().lower()`) | Agree. B is the stricter side |
| `impl_defaults.toolchain.language` | conductor `_conductor_authors_makefile` / `_conductor_authors_runner` | `_impl_resolved_language` | Agree. **Was a line scanner, hijacked by one line of free text** |
| the `build_system` argument | host gate (absent → make) | server (absent and under an orchestration → make) | Made to agree |
| the `preset` argument | host gate (absent → make_test, strip+lower) | server (exact match against a fixed table) | Every difference falls to a refusal on the server side |
| presence of `orchestration_id` | gate `is not None and strip()` | `_is_orchestrated_call` | Unified into one predicate |
| repository root | gate | each containment check | Unified into `_repo_root_for_call`. **Under the workflow it must be the server's own checkout** |
| the source list | `_fortran_syntax_source_order` (auto-discovery) | `_validate_syntax_sources` (explicit argument) | Unified. **Production only ever goes through auto-discovery** |
| `METDSL_WORKFLOW_MODE` | `tools/hooks/cli.py` (allowlist `{1,true,yes}` / `== "1"`) | server (anything but empty and `0` is workflow) | The server is the widest = the fail-closed side |
| the leaf's configuration layer | preflight `_read_repo_mcp_tool_permissions` and the tests that read the hook wiring | the real files `_prepare_claude_workflow_home` pins and copies | Unified into `leaf_config/claude/settings.json` (issue #63). **A sync test subset-matches the hooks against the dev layer `.claude/settings.json` and compares the grants.** During the migration only the Bash allowlist pin kept reading the dev layer, and **deleting all 16 entries on the leaf side left everything green** |
| where the leaf's transcript lives | `_claude_session_resumable` (whether a warm resume is possible) | `orchestration_diagnostics._leaf_transcript_path` / `_claude_projects_dir` (after-the-fact audit) | `tools/hooks/common.py::claude_leaf_projects_roots` was added for the audit side (issue #63); `_claude_session_resumable` deliberately does NOT call it and builds the one home a launch uses. **The answer differs by purpose**: the audit is right to look at both the private home and the operator home, while resume must look **only at the one that launch uses**. The version that looked at both called a pre-move session resumable and **threw `--resume` at a home that does not exist** |
| where the isolated home lives | the side bwrap binds (`claude_isolation_profile_kwargs`) | the side the Bash read guard forbids (`workflow_private_backend_homes`) | Issue #63 **moved one side only** and opened a hole: binding the operator's credentials into the private home made **the same secret appear at a path the guard does not know** (`~/.claude/...` blocked, `<home>/...` allowed). **CLOSED before this table came into the repository** — the guard now consults `workflow_private_backend_homes`, and `_backend_runtime_bind_paths` states the scope of its own claim for `backend_rw_override` callers. **The home MOVED AGAIN in issue #64**, to `~/.met-dsl/homes/<oid>/<backend>`, and the first account of that move was too kind to itself: "the new location is under a root the guard already refuses, so enforcement moved for free". Not free. `METDSL_WORKFLOW_HOMES_ROOT` relocates the tree, and with it pointed outside `~/.met-dsl` a leaf could read a SIBLING orchestration's transcript (measured, by Codex) while three documents asserted the closure with no mention of the condition — the pair had come apart again along an axis nobody had looked at. It took a code change (the homes ROOT became a protected entry, resolved by the same function the writer uses) to make the claim true. **The lesson the row now carries: "one side moved under something the other side already covers" is a claim about a CONFIGURATION, and it holds only for the configurations you enumerated.** Kept as a row because the SHAPE recurs: one side of a bind/forbid pair moved without the other |
| the agentic claude leaf's launch argv | `Conductor.leaf_command` (what a leaf is LAUNCHED with) | `claude_leaf_roster_probe_argv` (what preflight MEASURES the tool roster of) | Deliberate, because the import runs one way (conductor → runtime) and the preflight cannot call `leaf_command`. Held by FULL-EQUALITY sync test `test_the_roster_probe_argv_is_the_agentic_leaf_argv`, in the test module that can import both (issue #71). Not a subset check: every element of that argv can move the roster the CLI composes — the settings layer decides which permissions and hooks load, `--strict-mcp-config` / `--mcp-config` decide the MCP half outright — so a probe that drifted in ANY element would certify a tool set no leaf is launched with, **and report `pass` while doing it**. Only the executable is outside the equality, by construction: the probe takes the resolved command prefix from its caller so it certifies a configured wrapper rather than the bare binary |
| which launch-prompt shape a launch used (full / deterministic-stub / slim warm-resume) | `orchestration_runtime._required_launch_prompt_markers` (exempts a launch at render time) | `validate_pipeline_semantics._required_launch_prompt_markers_for_role` (re-derives the shape from `launch_text` when sweeping `agent_runs.jsonl` at `pre_judge`) | The validator does not import the runtime and keeps its own copy of each sentinel, so a new exempted shape has to be added to BOTH or a legitimate prompt false-rejects as `missing launch-prompt template markers`. Detection cannot be `SENTINEL in launch_text`: the full substep template explains the slim mechanism in boilerplate and contains the slim sentinel as prose, so a substring match misclassifies every full prompt as slim. Anchor on `launch_text.lstrip().startswith(SENTINEL)` instead (slim renderers always emit it as the first line), check slim before deterministic, and additionally cross-check against the launch REQUEST payload (`warm_resume` + `repair_strategy=='reuse'` + non-empty `repair_findings`, mirrored as `_launch_request_is_slim_repair`) — a prompt-text-only check lets a full launch's RECORDED prompt be swapped for slim-looking text without tripping the request-side signal. Held by `test_sentinel_constants_match_across_modules` plus an e2e that seeds real full/slim prompts and requests on disk |

## Facts with three or more readers (they do not fit the pair table)

**Stop at "find one counterpart" and you will miss.** In PR #55, opening the readers in two
installments led to getting the severity wrong three times (rule 1-c). For a fact with three or
more readers, **enumerate them all before opening any**.

| Fact | Readers (full enumeration) | State |
|---|---|---|
| a substep's output set | the conductor's `allowed_output_paths` declaration / runtime `compile_required` (membership, `_matches_phase_contract`) / the derived `allowed_file_tool_paths` / `output_manifest_write_guard` (hook) / the terminal FS diff / the prose in each `SKILL.md` | **There is no single definition.** In PR #55 `algorithm.summary.md` disagreed across three readers (the SKILL said to write it, the conductor did not declare it, the runtime allowed it). The work of moving the definition to one place has not started, so **an outside test can only sample rejections** |
| where the harness saved a tool result | **five sites across two files** (nine reader call sites in all): `_is_persisted_tool_result_shape` (the Bash block path) / `_blank_persisted_tool_results` (preprocessing before the marker scan) / two scan sites for auto-approve in `cli.py` / `_is_persisted_tool_result_read` (the Read tool) | Issue #63 left all of them hardcoded to `~/.claude`. **I fixed the block path only and wrote "fixed"** — auto-approve was a different call that did not receive the id, so the read was not blocked but not auto-approved either, and stayed unreadable. **CLOSED before this table came into the repository**: both auto-approve sites now pass the orchestration id. The lesson is why the row stays: **"no longer refused" and "now usable" are different measurements** |
| case_id grammar | **four**: `CASE_ID_TOKEN_RE` (`tools/spec_input_gates.py`) as read by the conductor's argv builder, by the validator's case-id gate and by the runner emitter — its own comment names those three — plus `_MAKE_NAME_VALUE_RE` for the CASES value | `CASE_ID_TOKEN_RE` is a proper subset of `_MAKE_NAME_VALUE_RE`. Listed here rather than in the pair table because the same rule below applies: three or more readers get enumerated before any is opened |
| how a validator gate failure is CLASSIFIED | **six** conductor readers: `_gate_static_check` (post_generate) / `_execute_inproc` (post_execute) / `classify_failure`'s execute branch (reads the category `_execute_inproc` recorded) / `_post_judge_inproc` (pre_judge) / `_build_inproc`'s post_build gate / `_compile_static_inproc` | **CLOSED = TODO:269 (2026-08-21)**, and the row stays because the SHAPE recurs. Four of the six decided TERMINAL vs warm; each now reads a dedicated **exit code**, never the output text — violations interpolate a leaf-chosen path, so every text scan was forgeable (the frontend marker was defeated three times, each fix a tighter sample of the same prose). The other two read nothing terminal and carry a comment saying so, proved by a call-graph closure rather than by reading the file. **Two traps specific to this pair**: the channel is the violation's TYPE, so any rebuild of the violations list between emit and decision silently degrades it — put the witness in a real subprocess; and `classify_failure` is a reader that never sees the exit code at all (it reads the category from `trial_meta.json`), so "count the sites that read the exit code" undercounts by one. Downstream of it, `conduct` maps `fail_closed` to a reason_code, and **which dev category set a `--resume` consults depends on that code** — the first pin here guarded the set the route never reaches |
| `agent_role` | **six**: one inference (`build_capability_document`) / three skips (`_allowed_output_paths_for_launch` / `_validate_child_write_contract_preflight` / `_build_task_card`) / `record_launch`'s own fallback / the conductor's fallback in `_register_codex_thread` (an `or "substep"` then; it normalizes and raises now) | **CLOSED = PR #57** (fail-closed at two chokepoints plus normalization at the head of `prepare_launch_request_payload`). The initial estimate of "five readers" was **actually six**. TODO.md's entry is canonical for the details and the measurements |
| "is this node's Makefile authored by the host or the leaf" | **three**: the conductor's own decision (`_resolved_makefile_host_authored`, IR-derived via `_ir_is_m3c_physics`) / the write-attribution check that trusts the LAUNCH REQUEST'S copy of that flag rather than re-deriving it / `_ir_is_m3c_physics` itself, read again by the validator for an unrelated dependency-count assertion | Registering a `backend` for one of the two gates that dispatch through it (§5.1 signature parsing) does not update the other two mirrors: a capability declared for one dispatch point can leave the Makefile double-owned (host-authored per the registry, leaf-authored per a mirror that still hardcodes `(make, fortran)`), and `_validate_checks_source_files` silently no-ops on a host-rendered runner node. Discovered building the backend-boundary capability registry (PR #67). **A capability declaration in the registry is not evidence the mirrors read it** — grep every site that spells the same predicate, not just the one you are converting |

## A neighbouring shape: the same name arriving in two **different payloads**

Every row above is "one value, several readers". **Separately from that, the same field name can
arrive as two different inputs.** Enumerate every reader and you still miss it if you think
there is one payload.

- `agent_role` rides on **both** the launch request (`record-launch`) and the terminal payload
  (`record-agent-run` / `finalize-child`). The fix described in PR #57's TODO entry was
  "normalize once at record-launch and every reader sees the same value", but
  `_validate_actual_write_paths` (the audit itself) reads **the terminal payload**, so **fixing
  the launch side alone does not close the audit**. That is where the need for two chokepoints
  came from
- Procedure: having enumerated the readers, check one by one **which payload each reader reads**.
  "Same key name, therefore same value" does not hold
- Symptom: "I fixed every reader, yet one layer's behaviour did not change"

## Deliberately not unified

- `_impl_is_leaf_node` remains a line scan. It is **anchored on a `dependency:` at indent 0**, so
  a value nested under another key cannot produce a physical line at column 0 and the free-text
  hijack does not work. If you change it, verify that premise along with it.

## A neighbouring shape: one name defined twice in one file (shadowing)

Sibling of "two places read the same fact", in which **the later definition silently overrides
the earlier**. The duplicate `_split_top_level_commas` closed by TODO L118 (the two definitions
9,500 lines apart) is this class, and **I reproduced it** in L128: I defined
`_FORTRAN_UNIT_OPEN` / `_FORTRAN_UNIT_END` as new constants while the same names already existed
1,500 lines below, and my definitions were never used at runtime. I only noticed when a test
failed in a way I could not explain.

- `tools/validate_pipeline_semantics.py` is over 14,000 lines. **Look the name up before adding a
  new module constant**
- Use the existing constant if it fits. If the meaning differs, **put the difference in the name**
  (`_FORTRAN_UNIT_OPEN` also covers `subroutine`, so a constant for host units only would be named
  something like `_FORTRAN_HOST_UNIT_OPEN` — a name that does not exist today)
- Symptoms: "I fixed the pattern and nothing changed", "I reverted the mutation and no test
  failed" → **print the compiled value and look** (`print(vps._FOO.pattern)`)

## How to look

Run from the met-dsl checkout root.

```bash
# sweep the places that read a given fact (example: the toolchain language)
rg -n "toolchain.*language|_impl_resolved_language|_conductor_authors" tools/ mcp_servers/
# find line scans (the shape that diverges from a structural read)
rg -n "splitlines\(\)" tools/orchestration_runtime.py
# does the name you are adding already exist / is it defined twice
rg -n "^_FORTRAN_MY_NEW_NAME\b" tools/
python3 -c "import tools.validate_pipeline_semantics as m; print(m._FORTRAN_UNIT_OPEN.pattern)"
```
