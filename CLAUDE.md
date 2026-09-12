# CLAUDE.md

@AGENTS.md

The shared / cross-backend conventions are imported from `AGENTS.md` above. This file adds only Claude Code-specific notes that every Claude sub-agent needs.

> **This file is DEV-ONLY: a workflow leaf does not read it.** Since Z4 ([issue #171](https://github.com/seiya/atmofab/issues/171)) every LLM substep runs as a `pure-function leaf`: `tools/pure_leaf.py` launches `claude -p --safe-mode --tools ""`, and `--safe-mode` refuses every settings layer, so CLAUDE.md / AGENTS.md auto-injection, the dev skills under `.claude/skills/`, and every hook are closed by the launch shape rather than by a flag chosen for that purpose. The leaf holds no tool with which to read a document either: its whole contract is the launch prompt the host renders (`tools/prompt_templates/pure_*.txt`), and it returns one JSON document. What that leaves for this file is the DEV audience alone.
>
> The measurement the paragraph above used to carry was about the AGENTIC leaf — `CLAUDE_CONFIG_DIR=<private home> --setting-sources user` (issue #63), measured on CLI 2.1.235 by capturing the leaf's own request, showing `project: 0` skills and zero occurrences of either skill name against a `--setting-sources project` control. That launch shape no longer exists. It is kept in the record rather than here: the commit that deleted it, and `docs/design/leaf_must_read_restructure.md`.
>
> The one rule that used to arrive this way and has no second home for a leaf — `AGENTS.md` §Backend boundary rules — is not actionable by a leaf, because a leaf cannot write a `neutral core` file. `_write_roots_for_launch` is the sole producer of write authority, and every root it returns is either under `workspace/orchestrations/<id>/`, `<ir_ref>/` or `<pipeline_ref>/`, or — for the `promote` step alone — under `releases/<spec_kind>/…` and the single file `spec/registry/spec_catalog.yaml`. None of those is `neutral core`: `docs/BACKEND_BOUNDARY.md` lists the core as `tools/`, `mcp_servers/`, `skills/`, `docs/`, the prompt templates and the three top-level documents, and puts `spec/` explicitly out of scope. (An earlier version of this sentence named only the first three roots and so was false for `promote`.) A pure leaf has no write authority at all — the host writes every artifact from the document it returns — so the rule is doubly out of its reach.

The development-facing counterpart of `AGENTS.md` is `docs/DEVELOPMENT.md`: fresh-machine setup, the configuration layers, where a development record belongs, and the `.claude/` boundary decision. It is backend-independent, so the Claude-specific notes below cite it rather than repeat it.

Claude-specific operator / maintenance references (not needed by a running sub-agent):
- Claude backend preflight requirements (build-runtime MCP registration + permission): [docs/RUNBOOK.md](docs/RUNBOOK.md) §0-2.
- Hook implementation, the matcher rule, and the dev-layer / leaf-layer split: [docs/HOOKS.md](docs/HOOKS.md).
- How a review loop over your own change is run — rounds, exclusion lists, when Codex enters, how convergence is judged: the `atmofab-review-loop` skill in `.claude/skills/`.
- The traps specific to changing enforcement machinery, and the rules for classifying a review finding as residual or out of scope: the `atmofab-enforcement-change` skill in `.claude/skills/`.
- There is ONE hook layer now, and it is the DEV one: `.claude/settings.json` and `.codex/hooks.json`, for an operator's interactive session, naming `tools/hooks/dev_cli.py`. The LEAF layer — `leaf_config/claude/settings.json`, `leaf_config/codex/hooks.json` and `tools/hooks/cli.py` — went with the agentic leaf in Z4 (issue #171): a pure leaf holds no tool, so there is no tool call for a hook to judge. `docs/HOOKS.md` is canonical for what remains.
