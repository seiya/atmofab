# Hook implementation policy

Repository-maintenance reference for the hook system. There is ONE hook layer and its audience is
the OPERATOR's own interactive session.

Until Z4 ([issue #171](https://github.com/seiya/atmofab/issues/171)) there were two, separated all
the way down to the entrypoint by issue #102: a LEAF layer (`leaf_config/claude/settings.json`,
`leaf_config/codex/hooks.json`, both naming `tools/hooks/cli.py`) that decided what a workflow leaf
could read, write and run, and this DEV layer. The leaf layer is deleted with the agentic leaf that
made it necessary: a `pure-function leaf` launches with no tools and no shell, so it issues no tool
call for a hook to judge, and the read-boundary, write-boundary and Bash-grammar machinery that
layer carried — `read_manifest_read_guard`, `output_manifest_write_guard`, `forbid_python_inline_write`,
`forbid_tools_direct_read`, `extract_bash_read_targets` and the rest — has no subject left. What
that machinery enforced, why each grammar was bounded the way it was, and what it measurably did
NOT reach are in the record rather than here: `git log` at the Z4 PR-1 commit that removed it, and
the episode files under `.claude/skills/atmofab-enforcement-change/references/`.

## The DEV layer
- `.claude/settings.json` and `.codex/hooks.json` register `tools/hooks/dev_cli.py`. It applies two
  stdlib-only rule modules and nothing else: `tools/hooks/operator_safety.py` (the hard-reset
  command, the verify-bypass flags in dev mode — the operator's own checkout) and
  `tools/hooks/dev_session_hygiene.py` (an agent session must not wait by sleeping — the session's
  own process table).
- **`dev_cli.py` imports the standard library and those two modules, deliberately, and encodes each
  backend's block protocol itself.** The operator's session runs the WORKING-TREE copy of its own
  hook, so anything it imports can refuse the operator out of the session they are editing in. That
  happened once, on 2026-08-26, from a half-applied edit to the (now deleted) leaf entrypoint, and
  the boundary is what keeps it from happening again. `tools/tests/test_hooks_dev_cli.py` pins the
  encodings and the import boundary by reading the source.
- A Codex `PermissionRequest` response uses `hookSpecificOutput.hookEventName="PermissionRequest"`
  with an explicit `decision.behavior` of `allow` or `deny`; a Claude block is exit 2 with the
  reason on stderr. Both encodings live in `dev_cli.py` and are pinned by its test.
- The `matcher` of a Claude hook entry is a **regular expression matched in full** — measured on CLI
  2.1.235: `Bash` and `Bash|Write` and `.*` all match, a prefix that is not the whole tool name does
  not. A matcher that matches nothing is silently inert, which is why the dev layer's entries are
  pinned by test rather than read by eye.

## Not event hooks
- `tools/hooks/lint_evidence.py` / `tools/hooks/syntax_evidence.py` are not event hooks but the host-authored, leaf-non-writable evidence certificates of the deterministic `Generate.gate` substep's lint and syntax checks (`<pipeline_root>/lint_evidence/<source_id>.json` / `<pipeline_root>/syntax_evidence/<source_id>.json`). The conductor writes them in-process (`workflow_conductor._gate_lint_check` / `_gate_syntax_check`, composed by `_gate_inproc`); `validate_pipeline_semantics --stage post_generate` certifies them; the write-attribution check in `tools/orchestration_runtime.py` exempts exactly those files scoped to the `gate` substep. See each module's docstring for the non-forgeability rationale. The same substep-granular scoping now also governs the bwrap `write_roots`: on an AGENTIC launch `compile.verify` / `generate.verify` / `validate.judge` are pinned to a single file (`ir_meta.json` / `source_meta.json` / `semantic_review.json`) rather than the step's whole directory (a `pure-function leaf` — which since [issue #168](https://github.com/seiya/atmofab/issues/168) is the default for both `compile` LLM substeps — has `write_roots: []` and is pinned to nothing, because it writes nothing), so the sandbox and the terminal FS-diff enforce the per-substep write scope structurally — a second layer beneath the pattern-based file-tool hook (canonical: `docs/ORCHESTRATION.md` §capability / write_root).
