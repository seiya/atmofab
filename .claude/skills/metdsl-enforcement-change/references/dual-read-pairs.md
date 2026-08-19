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
| where the leaf's transcript lives | `_claude_session_resumable` (whether a warm resume is possible) | `orchestration_diagnostics._leaf_transcript_path` / `_claude_projects_dir` (after-the-fact audit) | Unified into `tools/hooks/common.py::claude_leaf_projects_roots` (issue #63). **But the answer differs by purpose**: the audit is right to look at both the private home and the operator home, while resume must look **only at the one that launch uses**. The version that looked at both called a pre-move session resumable and **threw `--resume` at a home that does not exist** |
| where the isolated home lives | the side bwrap binds (`claude_isolation_profile_kwargs`) | the side the Bash read guard forbids (`workflow_private_backend_homes`) | Issue #63 **moved one side only** and opened a hole. Binding the operator's credentials into the private home made **the same secret appear at a path the guard does not know** (`~/.claude/...` blocked, `<home>/...` allowed). The claim in `_backend_runtime_bind_paths`'s docstring that "the set bound equals the set the guard forbids" **does not apply to calls that pass `backend_rw_override`** |

## Facts with three or more readers (they do not fit the pair table)

**Stop at "find one counterpart" and you will miss.** In PR #55, opening the readers in two
installments led to getting the severity wrong three times (rule 1-c). For a fact with three or
more readers, **enumerate them all before opening any**.

| Fact | Readers (full enumeration) | State |
|---|---|---|
| a substep's output set | the conductor's `allowed_output_paths` declaration / runtime `compile_required` (membership, `_matches_phase_contract`) / the derived `allowed_file_tool_paths` / `output_manifest_write_guard` (hook) / the terminal FS diff / the prose in each `SKILL.md` | **There is no single definition.** In PR #55 `algorithm.summary.md` disagreed across three readers (the SKILL said to write it, the conductor did not declare it, the runtime allowed it). The work of moving the definition to one place has not started, so **an outside test can only sample rejections** |
| where the harness saved a tool result | **four sites in two files**: `_is_persisted_tool_result_shape` (the Bash block path) / `_blank_persisted_tool_results` (preprocessing before the marker scan) / two scan sites for auto-approve in `cli.py` / `_is_persisted_tool_result_read` (the Read tool) | Issue #63 left all of them hardcoded to `~/.claude`. **I fixed the block path only and wrote "fixed"** — auto-approve is a different call that still did not receive the id, so the read is not blocked but is not auto-approved either, and with no `cat /tmp/...` in the committed permissions it stays unreadable. **"No longer refused" and "now usable" are different measurements** |
| case_id grammar | **four**: `CASE_ID_TOKEN_RE` (`tools/spec_input_gates.py`) as read by the conductor's argv builder, by the validator's case-id gate and by the runner emitter — its own comment names those three — plus `_MAKE_NAME_VALUE_RE` for the CASES value | `CASE_ID_TOKEN_RE` is a proper subset of `_MAKE_NAME_VALUE_RE`. Listed here rather than in the pair table because the same rule below applies: three or more readers get enumerated before any is opened |
| `agent_role` | **six**: one inference (`build_capability_document`) / three skips (`_allowed_output_paths_for_launch` / `_validate_child_write_contract_preflight` / `_build_task_card`) / `record_launch`'s own fallback / the conductor's `or "substep"` in `_register_codex_thread` | **CLOSED = PR #57** (fail-closed at two chokepoints plus normalization at the head of `prepare_launch_request_payload`). The initial estimate of "five readers" was **actually six**. TODO.md's entry is canonical for the details and the measurements |

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

- `tools/validate_pipeline_semantics.py` is over 13,000 lines. **Look the name up before adding a
  new module constant**
- Use the existing constant if it fits. If the meaning differs, **put the difference in the name**
  (`_FORTRAN_UNIT_OPEN` also covers `subroutine`; if you want host units only, use
  `_FORTRAN_HOST_UNIT_OPEN`)
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
