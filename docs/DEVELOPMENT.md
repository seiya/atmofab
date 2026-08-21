# Developing this repository

## Purpose
Every other document under `docs/` specifies the WORKFLOW — its phases, their contracts, the orchestration that runs them, and the boundary the generated system must respect. This document specifies DEVELOPING this repository: what a fresh machine needs, which configuration layer governs which session, where a record belongs so that it survives the person who made it, and the development rules that no other document owns.

The rule this document exists to enforce on itself: **the canonical copy of a development record is in this repository or on GitHub.** An operator's `~/` holds machine-local runtime state and personal credentials, and nothing else that a second person would need.

## Scope
In scope: operator setup, the configuration layers, record placement, and the development rules with no other owner.

Out of scope, each with its owner named rather than restated here:
- The workflow itself, its phases and contracts — `docs/README.md` indexes the set; `docs/WORKFLOW.md` is its entry point.
- Document authoring style, terminology, and the change checklist — `docs/DOC_STYLE.md`.
- Where knowledge of a concrete target-stack technology may live — `docs/BACKEND_BOUNDARY.md`.
- How a change to this repository is reviewed — the `metdsl-review-loop` skill under `.claude/skills/`.
- What a change to the enforcement machinery must satisfy — the `metdsl-enforcement-change` skill under `.claude/skills/`.
- Running a trial, and recovering from a failure — `docs/RUNBOOK.md`.

## Fresh-machine setup
A fresh clone needs the host tools and the operator's own CLI state. Every file that decides what a leaf loads FROM THIS REPOSITORY is committed; what is left in a home directory is the backend CLI's own state, and it is not nothing. Two members of it decide behaviour rather than merely holding credentials: the Codex CLI's `hooks` feature flag, without which a leaf would run with no policy layer at all (`docs/RUNBOOK.md` §0-3 gates on it), and the leaf-`LLM` configuration, which is untracked by design and is the only thing that says which model runs which leaf.

| step | requirement | canonical source |
|---|---|---|
| 1 | Host CLI tools and Python packages | `docs/RUNBOOK.md` §0-1 |
| 2 | Claude backend: server registration and the leaf's tool grant | `docs/RUNBOOK.md` §0-2 |
| 3 | Codex backend: the CLI feature flag, the credential, the writable state home | `docs/RUNBOOK.md` §0-3 |
| 4 | The leaf-`LLM` configuration file, created by copying a sample | `docs/RUNBOOK.md` §1-3, `README.md` §"Running a workflow" |
| 5 | The sandbox runtime | `docs/BWRAP_ENABLEMENT.md` |

Steps 1, 2, 3 and 5 all read machine-local state, and preflight gates them so that a machine that cannot run the workflow is refused before the first billed leaf rather than part-way through. One requirement is outside that guarantee and is called out where it lives: the Codex credential is checked when the first leaf is prepared, not at the gate (`docs/RUNBOOK.md` §0-3).

## Configuration layers
Two sessions run against this checkout, and they load disjoint configuration. An operator's own interactive session loads the DEV layer; a workflow leaf loads the LEAF layer and nothing else. `docs/HOOKS.md` is canonical for the split and for what keeps the two in step.

| file | layer | read by | tracked |
|---|---|---|---|
| `leaf_config/claude/settings.json` | LEAF | a workflow leaf, as the sole settings layer of a host-prepared private configuration directory | yes |
| `.codex/hooks.json` | LEAF | a workflow leaf, through a digest-verified copy in the isolated home | yes |
| `.mcp.json` | LEAF | a workflow leaf, named explicitly at launch | yes |
| `.claude/settings.json` | DEV | an operator's own interactive session | yes |
| `.claude/settings.local.json` | DEV | the same session, per operator | no |
| `.claude/skills/` | DEV | the same session | yes |
| `llm.yaml` | run input | the driver, to decide what each leaf launches | no |
| the operator's own CLI configuration directories | personal | the operator's own session, and the CLI's authentication | out of tree |
| `~/.met-dsl/` | runtime state | the host, per orchestration | out of tree |

Three consequences worth stating explicitly:

- **A permission or hook the workflow depends on goes in a committed file.** The untracked local files exist for one operator's scratch; a grant that lives only there works on one machine and nowhere else. When both a tracked and an untracked file could hold a setting, the tracked one is the answer.
- **The leaf layer is the owner when a setting appears in both.** Edit the leaf file first; a synchronization test requires the dev layer to be a SUPERSET — every leaf hook and every leaf grant mirrored into it, so an operator's session enforces at least the policy a leaf does, while the dev layer may carry hooks of its own. Equality was tried and refused: it forbade adding any operator-convenience hook to the file whose purpose is the operator's session.
- **A workflow leaf reads none of the repository's top-level instruction documents.** `CLAUDE.md`, `AGENTS.md`, and the dev skills are the DEV layer. A leaf's contract arrives through its launch prompt: `docs/AGENT_CONTRACT.md` plus the phase `SKILL` under `skills/`. A rule a leaf must follow therefore has to land in one of those, never here.

## Repository environment
Facts about this repository that decide how an operator's own session should be configured. They are stated here because a session-level safety configuration is per-user and per-machine, so this repository cannot hold the configuration itself — only the material an operator builds one from.

- **The repository is public.** Anything pushed to `origin` is published. Only this repository's own work belongs there; credentials and personal data are never made safe by the visibility setting.
- **`main` is the default branch and carries no protection rule.** Nothing on the hosting side refuses a direct push to it. A change lands through a branch and a pull request by convention, and the convention is the only thing enforcing it.
- **There is no CI.** Every check named in `docs/DEVELOPMENT.md` and in the two development skills runs on the operator's own machine, and a green tree is a claim about the machine it was measured on. This is why a measured figure is recorded with the commit it was taken at.
- **There are no deployment targets, cloud accounts, package registries, or internal services.** A workflow run reaches the LLM providers its configuration names, and nothing else.
- **Dotenv files are ignored**, so a secret placed in one is not offered for commit. Nothing scans for a secret placed anywhere else.
- **The routine commands are repo-local**: the test suite, the linter, the workflow driver, and the tools under `tools/`.

## Record placement
One fact has one canonical home. A restatement elsewhere is a twin document, and a twin is a future disagreement rather than a convenience — cite the owner instead.

| record | canonical home |
|---|---|
| a finished specification, contract, or procedure | `docs/` |
| open work, with the evidence that it is open | `TODO.md` |
| a decision about one named technology | `docs/design/`; the note states its `Status` |
| what a review loop taught about reviewing | the relevant skill's `references/` under `.claude/skills/` |
| a measurement episode, and a design that was rejected | a comment on the GitHub issue it belongs to |
| a plan the operator approved | a comment on the GitHub issue it belongs to |
| the history of a change that is finished | its pull request |

**An agent's own memory and plan files are not a home on this list.** Auto-memory, scratch plan files, and anything else that lives in a personal home directory are a working aid for one session on one machine. A fact that a second person would need is not recorded until it is in this repository or on GitHub, and a task is not finished while such a fact exists only in a home directory. `AGENTS.md` §"Canonical record placement" states that rule for every agent on either backend; this table is where it says WHERE.

## The `.claude/` boundary decision
`.claude/` is **out of scope** for `docs/BACKEND_BOUNDARY.md`, and this is the decision that put it there.

The boundary rule bounds knowledge of a concrete target-stack technology in the NEUTRAL CORE — the code, templates, skills, and documents through which a run produces a target-stack system. `.claude/` is none of that. It is the operator's own development session: measured on CLI 2.1.235, a workflow leaf loads zero of its skills and its request carries neither skill's name. `CLAUDE.md` holds that measurement and the three limits it was taken under — a hand-assembled launch, a scratch checkout, and the agentic leaf shape only. The technology spellings it does carry are ABOUT the instrument — a checklist for reviewing this repository's own gates — which is the same reason `tools/tests/` and `docs/design/` are out of scope.

The cost of that decision, stated rather than left to be discovered: those spellings are unmeasured. The ratchet does not read `.claude/`, the migration ledger does not count what is there, and a change of target-stack technology has to sweep that tree by hand. The material is also not where a reader would guess — the explicit spelling checklist in one skill is a small minority of it, and the rest is episodes and identifier names spread across BOTH skills, including the one with no checklist at all.

## Design Policy
- **Complete the canonical document before adding a prohibition.** When an agent's behaviour is undesirable, first ask whether the information that would make the desirable route obvious exists and is reachable. A prohibition removes judgment and stops real work in the cases nobody anticipated; a complete document does not. Reserve an enforcement rule for what a document cannot address, and make its remediation text an anchor to the canonical document rather than a statement of the ban.
- **Do not persist a value an existing artifact already carries.** A second copy splits the source of truth. When restoring a value on resume, inventory what is already written before proposing a new file; when a parsing coupling is the concern, pin it with a round-trip test rather than with a new artifact.
- **Correctness is not traded for throughput.** A change that can lower the rate at which the workflow produces correct results is not adopted, whatever it saves. Optimizations that preserve the judgment — moving a purely deterministic verification out of an `LLM` substep, sharing a read-only prefix across leaves — are.
- **Do not create a second document holding the same information.** When a reader needs a subset of a document, SPLIT the canonical one and cite the parts; do not derive a digest that must then be kept in step.
- **Repair the designed route before adding a second one.** When information fails to reach a consumer, the permanent fix is the route the design names — not an injection that supplies the value from somewhere else. A bypass makes the consumer succeed while destroying the signal that the route is broken, so the defect stops being detectable. Where a bypass is genuinely needed as an interim measure, it ships only together with its REMOVAL CONDITION and removal procedure, written into `TODO.md` at the same time; an interim measure whose removal is a verbal promise is not proposed.
- **State a rejected alternative with the measurement that rejected it.** A design that was considered and dropped is recoverable only if the reason is written down; without it the same option is re-proposed, and re-measured, later.

## Operations Rules
- **One driver per workspace.** Never drive an orchestration by hand while a conductor is running it, and never start a second orchestration against the same workspace. Write attribution is derived from a workspace-global baseline diff, so concurrent runs mis-attribute each other's writes and fail on write-authorization violations. While a run is in flight, treat the workspace as read-only.
- **Verification is performed by a separate persona.** Reusing a generating session to verify its own output is not an optimization with a cost; it is the loss of the property verification exists for. Independence means a fresh inference context that inherits neither the producer's conclusions nor its reasoning. This applies to the workflow's `generate` / `verify` substeps and to a review of a change to this repository alike.
- **A version identifies content, not a re-run.** Do not bump a version to force re-certification while the content is unchanged; freshness is the readiness machinery's responsibility. A change of REPRESENTATION is a content change and does bump.
- **Pin what was used, not what will be used.** Model selection uses an unpinned alias so it does not go stale; the record must carry the exact version that actually ran, resolved at run time. A guessed version written into a record is worse than no record.
- **Measure before asserting.** Do not write a number, a count, or a causal claim into a document, a commit message, or `TODO.md` that was not produced by running something. The verification commands are in the `metdsl-review-loop` and `metdsl-enforcement-change` skills.
