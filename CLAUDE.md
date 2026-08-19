# CLAUDE.md

@AGENTS.md

The shared / cross-backend conventions are imported from `AGENTS.md` above. This file adds only Claude Code-specific notes that every Claude sub-agent needs.

> **This file is DEV-ONLY: a workflow leaf does not read it.** `tools/workflow_conductor.py` launches each `step agent` / `substep agent` as a leaf (`claude -p`) with `CLAUDE_CONFIG_DIR=<private home> --setting-sources user` (issue #63), and that closes CLAUDE.md / AGENTS.md auto-injection along with the settings layers — measured on CLI 2.1.235 by capturing the leaf's own request. A leaf's contract is [docs/AGENT_CONTRACT.md](docs/AGENT_CONTRACT.md) plus its phase `SKILL`, delivered through the launch prompt; nothing here reaches it. The one rule that used to arrive this way and has no second home for a leaf — `AGENTS.md` §Backend boundary rules — is not actionable by a leaf, because a leaf cannot write a `neutral core` file. `_write_roots_for_launch` is the sole producer of write authority, and every root it returns is either under `workspace/orchestrations/<id>/`, `<ir_ref>/` or `<pipeline_ref>/`, or — for the `promote` step alone — under `releases/<spec_kind>/…` and the single file `spec/registry/spec_catalog.yaml`. None of those is `neutral core`: `docs/BACKEND_BOUNDARY.md` lists the core as `tools/`, `mcp_servers/`, `skills/`, `docs/`, the prompt templates and the three top-level documents, and puts `spec/` explicitly out of scope. (An earlier version of this sentence named only the first three roots and so was false for `promote`.)

Claude-specific operator / maintenance references (not needed by a running sub-agent):
- Claude backend preflight requirements (build-runtime MCP registration + permission): [docs/RUNBOOK.md](docs/RUNBOOK.md) §0-2.
- Hook implementation, the matcher rule, and the dev-layer / leaf-layer split: [docs/HOOKS.md](docs/HOOKS.md).
- The leaf's own configuration is `leaf_config/claude/settings.json` (hooks + permission grants). This file's sibling `.claude/settings.json` is the DEV layer, for an operator's interactive session; a sync test keeps their hook commands identical.
