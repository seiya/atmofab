# Adding a new deterministic (conductor in-process) substep

`docs/ORCHESTRATION.md`'s "Deterministic in-process execution" (a bold bullet, not a heading) describes the substeps that exist
today (`Compile.static`, `Generate.gate`, `Build`, `Validate.pre_judge`, `Validate.execute`,
`Validate.post_judge`). This file is the wiring checklist for adding another one — every one of
these was hit adding `Generate.static` (later folded into `Generate.gate`), and every miss left
the unit suite green while the real flow fail-closed, because the conductor-level tests drive a
mock conductor that captures `record-launch` / `revoke-artifact` as call tuples and never reaches
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
6. The same-phase re-derivation route in `conduct` — which triggering substep is permitted to
   re-derive the phase this one belongs to. There is no carve-out to add on the runtime side any
   more: `revoke-artifact` takes the phase to re-derive rather than a trigger to validate, so the
   permission lives entirely in `conduct`'s routing conditions (issue #177 retired `reopen-phase`,
   whose trigger gate was the thing this item used to point at).
7. ~~`_mandatory_file_tool_pins_for_launch`'s early-return set~~ and
   8. ~~`_allowed_file_tool_paths_for_launch`~~ — **both RETIRED**, so this is a checklist of
   SEVEN sites now. They pinned which paths a leaf's `Edit`/`Write` grant covered; both
   functions went with that grant in Z4 ([issue #171](https://github.com/seiya/spec2science/issues/171)
   PR-2), when the `pure-function leaf` (deleted since in issue #534 PR-J4-b) held no tool and
   the host wrote every artifact from the document the leaf returned. There is nothing to make
   leaf-non-writable: a host-authored artifact is unreachable by construction (a `workshop leaf`,
   issue #534, writes only its own work tree outside the checkout, and the host writes every
   artifact from that tree's snapshot).
9. `ALLOWED_VALIDATE_PIPELINE_STAGES` and `_build_gate_runbook`, and the matching table in
   `docs/workflow/LAUNCH_PROMPT_REFERENCE.md` — keep them in sync in the same commit.

**Verification that actually exercises this**: a test that calls the real
`orchestration_runtime.py` functions directly, not the mock conductor — the mock's call-tuple
capture cannot observe any of the nine sites above.

A related trap on the SAME kind of change: a gate keyed on `source_meta.verification_status ==
"pass"` silently stops firing if you move what it certifies to run BEFORE verification succeeds
(the value is not yet `"pass"` at that point in the pipeline). The fix is an OR — evidence-file
presence OR the pass status — not reordering the gate.

## Adding a deliverable to an existing deterministic substep

Issue #589 PR-0 (PR #590) made `Build` write a second binary, `binary/<id>/bin_checked/<exe>`,
when the target's compiler declares checked flags, and listed it in `build_launch_request`'s
`allowed_output_paths`. Three places decide what a deterministic substep's outputs are, and the
first commit touched one:

1. `build_launch_request` — the launch's `allowed_output_paths`, which `determine_substep_status`
   reads for freshness;
2. `orchestration_runtime._matches_phase_contract` — the matcher `record-launch` runs over that
   list (through `_allowed_output_paths_for_launch`); its `build` branch admitted `/bin/`,
   `binary_meta.json` and `<binary_id>/command_log.jsonl` and raised for anything else;
3. `phase_required_outputs` — the step result's `required_outputs`, the set
   `_certifiable_artifact_refs` takes `artifact_hashes` over. A launch list that carries a path
   the step result does not, or the reverse, is refused at `write-step-result`
   (`required_outputs must be satisfied by substep output_refs`).

What shipped through round 0: (1) only. Round 1 reproduced the refusal on a recorded real build
request (`tools/tests/data/conductor_launch_requests/`): every `fortran_cpu` /
`fortran_cpu_mpi` Build would have failed at `record-launch`. The suite was green because the
row meant to pin the wiring read `inspect.getsource` text, and the at-site and unit rows call
`_build_inproc` / `_run_deterministic_substep` directly, below `record_launch`. Round 2 then found
the two Conductor call sites computing the flag (`run_substep`'s launch, `run_phase`'s step
result) each survived a `False`, because the round-1 rows called the two builders directly.

What closed it: the matcher admits the new path at exactly its depth; `phase_required_outputs`
takes the same flag; one row drives `_validate_launch_request_payload` and
`_allowed_output_paths_for_launch` on a recorded real request and `_certifiable_artifact_refs`
on the required outputs; another drives the real `run_phase("build")` with the runtime's I/O
stubbed and reads the recorded request and the step result (`tools/tests/test_checked_build.py`
`BuildTests` and `RunPhaseCallSiteTests`). A stub of `run_phase` that bypasses `__init__` (no
target) then needs the new target read stubbed too (`test_run_workflow.py`'s build stub).

