# Historical launch prompts (pre-Z4)

Two launch prompts rendered by `origin/main` at `2b8db403`, the last commit before Z4
(issue #171) retired the agentic leaf: the FULL `generate.generate` substep prompt and the
warm-resume SLIM repair turn. Nothing renders either shape any more — `_render_launch_prompt_template`
refuses a request that is neither deterministic nor pure.

They are kept because `tools/validate_pipeline_semantics.py` audits a PERSISTED record, and an
operator's `workspace/` holds orchestrations launched before Z4. Its `slim` and full-leaf marker
arms exist for those records alone, for the same reason `_LEGACY_LAUNCH_PROMPT_MARKERS` still maps
the Japanese markers of an even older one: `--stage full` is the documented CI pass-condition, so
a stale artifact that the sweep cannot classify fails the gate with no artifact saying why.

`tools/tests/test_validate_pipeline_semantics.py` drives the validator against these two files.
They are a FROZEN RECORD: regenerating them from today's tree is impossible and re-authoring them
by hand would pin what someone believed the old renderer emitted. Reproduce with
`git show 2b8db403` and the commit message of the Z4 PR-1 commit that added this directory.
