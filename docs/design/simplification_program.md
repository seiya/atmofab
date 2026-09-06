# Simplification program

Status: **decided** (2026-09-06). The operator's premise statements below are in force. The work is tracked by issue #167 and the open issues it lists; this note is the in-repository record, so that the decisions do not depend on the issue tracker being read.

## Purpose

Record the 2026-09-06 survey of the tree at `c131639`, the independent verification of each finding, the operator's decisions, and the resulting issue map. The stated purpose of the repository that every item is judged against: generate correct code from a `spec` (`controlled_spec.md` / `tests.md` / `deps.yaml`) and certify it by execution, with machinery no larger than that purpose needs.

## Premise statements by the operator

1. **Simplicity.** The machinery has grown too complex; it is kept as simple as the purpose allows. Deletion and the pure-function leaf path are preferred over a new guard, backend, or record.
2. **No vendor lock-in.** Written into `AGENTS.md` §Development premises on 2026-09-06 (with a `vendor lock-in` entry in `docs/GLOSSARY.md` §12). A leaf provider that no configuration on the operator's machine names, and that no recorded run has used, is still part of the product. Issue #172 (delete the `codex_cli` provider) was rejected on this premise; the reviewer had derived the deletion from the single-operator premise, which does not make the operator's `llm.yaml` the product's backend set. The target-stack half applies the same way: CUDA, C++ and Python targets are planned for the near term, so issue #173 (delete the lint presets and build systems that no in-tree node can select today) was rejected as well.
3. **`Tune` and `Promote` are required future flows.** Neither is implemented and no run has used them, and both are required. Their reserved surface stays: `skills/workflow-tune-generate/`, `skills/workflow-tune-verify/`, `skills/workflow-promote/`, `docs/TUNING_WORKFLOW.md`, `spec_catalog.yaml#official_releases`, `releases/registry/component_catalog.yaml`, and the `tune` / `promote` write-root branches in `tools/orchestration_runtime.py`. Issue #174 (delete that surface) was rejected. What the verification found and a future design repairs first: the write-root branches are unreachable because `_required_child_agent_kind` refuses both step tokens before a capability is minted; a comment cites `docs/workflow/phases/phase_07_promote.md`, deleted in `537475d` (2026-05-11); the CLI help names steps (`plan`, `execute`, `judge`) the step table does not contain; the "separate plan" that `docs/WORKFLOW.md` and `docs/workflow/WORKFLOW_CORE.md` cite has no home; and neither `zero_base_architecture.md` nor `workflow_scaling_redesign.md` names the flows, so their relation to the target lowering plan and the target profile's performance policy is undefined.
4. **The role of `spec_kind=profile`, and where runtime scheme selection lives.** For a part (a `component` or a `problem`) the numerical scheme is decided at compile time, and one binary per scheme is acceptable. Runtime selection among schemes (for example third-order upwind against fourth-order central) is the responsibility of the driver of the assembled weather model, which selects among promoted subroutines and calls the chosen one. That driver will itself be built spec-driven by this workflow, as a `node` of its own in the way the `infrastructure` harness is, so switching inside one binary is in the future scope as that node's concern. Consequences: a `profile` is a compile-time selection policy resolved by the host at Compile for the adopting `node`, not a certified code `node`; the resolver writes `profile_selection` per case; a closure may hold several `component`s of one role; no validator rule may forbid a node adopting several profiles. Issue #175 carries the rewritten proposal.

## Method

Eight findings from the survey were each verified by an independent reviewer that re-measured every figure (line counts by AST, execution records under `workspace*/`, callers by grep, the repository's design documents), sought counter-evidence, and applied `AGENTS.md` §Development premises, `.claude/skills/atmofab-enforcement-change` rule 1-b, and `docs/design/zero_base_architecture.md`. Seven were adopted with corrections; one and several sub-items were rejected. Every issue body states its measurements with the command used, at `c131639`.

## Adopted work (issues)

Independent, can land now:

| issue | subject |
|---|---|
| #175 | `profile` becomes a host-resolved compile-time selection policy; no longer certified as code (premise 4) |
| #176 | delete the recovery paths no record has reached (legacy record repair, step-executor repair, dismiss-violation, `--backfill`, the checkpoint read subcommands, two warm-resume mini-loops, the transport resume directive) |
| #178 | one dependency-readiness primitive; the launch gate routes through it |
| #179 | one workflow-audit skill that calls `tools/audit_orchestration.py`; the pre-#47 transcript usage path removed |
| #180 | retire `tools/check_artifact_syntax.py` (subsumed at both conductor gates); delete the retired and phantom meta names (`tune_meta` stays, premise 3) |
| #181 | `TODO.md` becomes a task list; finished records move to their PR or issue; `deterministic_followups.md` is frozen |
| #182 | freeze the backend-boundary token ratchet until the second target starts; the migration ledger continues (CUDA, C++ and Python targets are planned) |
| #183 | retire the meta-tests that pin a documentation or process preference; move the two review-instrument tests beside their scripts |

Sequenced (the pure-leaf migration and what it makes dead):

1. #168 Z1: `compile.generate` and `compile.verify` as pure leaves, on every declared provider including the CLI transports' pure paths (premise 2).
2. #169 `validate.judge`, the `infrastructure` node's Generate, and the escalate diagnostician as pure leaves. The `--mode prod` / `escalate` regime stays: an automatic diagnosis path for failures the deterministic tables cannot classify will be needed in unattended runs, and the diagnostician already reads no file and calls no tool.
3. #170 `--wait-usage-reset` text parsing replaced by a bounded backoff on the `llm_usage_limit` tag.
4. #171 Z4: the authoring-side enforcement complex deleted once no leaf holds a shell; blocked on 1 and 2. The read-only sandbox profile and the codex home / probe / feature-cache functions stay (premise 2).
5. #177 resume from certified artifacts; the checkpoint ledger, reopen-phase and the superseded set retired. Smaller after #176.

## Rejected

- Deleting the `codex_cli` leaf provider (#172): premise 2.
- Deleting the Tune / Promote surface (#174): premise 3.
- Deleting the lint presets and build systems no in-tree node can select today (#173): premise 2, target-stack half; the targets that use them are planned.
- Deleting the `--mode prod` / `escalate` regime (the first form of #170): a diagnosis path for unclassifiable failures is needed in unattended runs; the diagnostician is migrated to the pure transport (#169) instead.
- Folding `profile` into its problems and retiring the kind (the first form of #175): premise 4 gives the kind a role; the issue was rewritten rather than closed.
- On-disk clutter (51 `workspace_2026*` snapshots, 11 GB; `transcript_data/`; probe files): every path is ignored by an explicit `.gitignore` rule, no tracked file reads them, and the decision not to adopt a `workspace*` retention rule is already recorded. Local housekeeping; no repository change.
- Removing the `claude_cli` provider after the pure migration: its `--safe-mode` pure path is the only subscription-billed route; `anthropic_api` requires an API key and has never run.
- Merging `build_launch_request` (conductor) and `record_launch` (runtime): one builder and one validating recorder, the producer/validator separation the premises call for.
- Deleting `mcp_servers/mcp_call.py`: the enforcement-change procedure requires proving a gate refusal through the real JSON-RPC layer.
- Merging the twelve live `*_meta.json` kinds into one per phase: would collapse almost no validator code and would cross the leaf/host authorship line the single-file write roots enforce.
- Deleting the two dev skills under `.claude/skills/` and their scripts: `docs/DEVELOPMENT.md` §Record placement names them as the home of what the review loop taught; only their tests move (#183).
- A retroactive sweep of comment prose (33% of lines in the eleven main modules): most is rationale needed to change the code safely; a rewrite of that size is itself the second defended class.
- Deleting the backend registry: seven importers, 46 call sites, and the lint-rule pinning of issues #111 / #120 went through it; the ledger continues and only the ratchet freezes until the second target starts (#182).

## Corrections the verification made to the survey

- Agentic launches number six, not five: the escalate diagnostician is the sixth (#169).
- The sandbox wraps LLM CLI processes only; build and execute run in-process and unsandboxed today, so "keep the sandbox on build/execute" would be new work (#171).
- The judge recomputes from `raw/` with scripts it writes, which a tool-free leaf cannot do; its pure form needs a host-side excerpting policy (#169).
- Non-default target-stack values are refused at `Compile.static`, not at spec-input (recorded on #173).
- Reopen and supersede are the default retry loop, used by 39 of 118 passing orchestrations, not recovery machinery; they are replaced by an attempt model, not deleted (#177).
- The certified profile code is a second implementation of the problem's integration step with unjudged output, not a version-constraint check (#175).

## Decision Criteria

- A work issue is closed by a PR whose completion criterion is the one the issue states, or by the operator's comment declining it.
- A premise statement above changes only by the operator's statement, recorded here and, for premise 2, in `AGENTS.md` §Development premises.
