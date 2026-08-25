# Adding a new deterministic (conductor in-process) substep

`docs/ORCHESTRATION.md` §"Deterministic in-process execution" describes the substeps that exist
today (`Compile.static`, `Generate.gate`, `Build`, `Validate.pre_judge`, `Validate.execute`,
`Validate.post_judge`). This file is the wiring checklist for adding another one — every one of
these was hit adding `Generate.static` (later folded into `Generate.gate`), and every miss left
the unit suite green while the real flow fail-closed, because the conductor-level tests drive a
mock conductor that captures `record-launch` / `reopen-phase` as call tuples and never reaches
`orchestration_runtime.py`'s own CLI enforcement.

Grep the existing deterministic substep's name across **both** `tools/workflow_conductor.py` and
`tools/orchestration_runtime.py` — every site that special-cases it is a site the new substep also
needs. Items 1-3 below live in the conductor (measured: `_is_deterministic_substep` and
`determine_substep_status` have zero occurrences of any kind in `orchestration_runtime.py`), items
4-9 in `orchestration_runtime.py`, and `classify_failure` / `build_launch_request` appear in both.
Grepping only the one file the first version of this line named misses exactly the classification
and dispatch sites — which is how the green-unit-suite failure above happens:

1. The substep-set declaration and its `_is_deterministic_substep` classification, and the
   conductor dispatch that routes to the in-process body instead of a leaf.
2. `determine_substep_status` / `classify_failure` — the routing that decides warm-resume vs
   terminal for this substep's violations.
3. `build_launch_request`'s deterministic flag (the minimal-stub launch prompt, no `SKILL.md`
   section, reduced launch-prompt-marker set).
4. `_validate_launch_request_payload`'s deterministic=True allowlist.
5. `_matches_phase_contract` — which artifact filenames this substep may write (a host-authored
   certificate needs an explicit allowance here, or it is judged as an unauthorized write).
6. `reopen-phase`'s same-phase carve-out — which triggering substep is permitted to reopen the
   phase this one belongs to.
7. `_mandatory_file_tool_pins_for_launch`'s early-return set, if this substep's output is
   host-written and should be exempt from leaf file-tool pins.
8. `_allowed_file_tool_paths_for_launch` — both the auto-derive and the explicit-list branches —
   so a host-authored artifact this substep writes is leaf-non-writable.
9. `ALLOWED_VALIDATE_PIPELINE_STAGES` and `_build_gate_runbook`, and the matching table in
   `docs/workflow/LAUNCH_PROMPT_REFERENCE.md` — keep them in sync in the same commit.

**Verification that actually exercises this**: a test that calls the real
`orchestration_runtime.py` functions directly, not the mock conductor — the mock's call-tuple
capture cannot observe any of the nine sites above.

A related trap on the SAME kind of change: a gate keyed on `source_meta.verification_status ==
"pass"` silently stops firing if you move what it certifies to run BEFORE verification succeeds
(the value is not yet `"pass"` at that point in the pipeline). The fix is an OR — evidence-file
presence OR the pass status — not reordering the gate.
