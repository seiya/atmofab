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
- How a change to this repository is reviewed — the `atmofab-review-loop` skill under `.claude/skills/`.
- What a change to the enforcement machinery must satisfy — the `atmofab-enforcement-change` skill under `.claude/skills/`.
- Running a trial, and recovering from a failure — `docs/RUNBOOK.md`.

## Fresh-machine setup
A fresh clone needs the host tools and the operator's own CLI state. Every file that decides what a leaf loads FROM THIS REPOSITORY is committed; what is left in a home directory is the backend CLI's own state, and it is not nothing. Two members of it reach past holding credentials. The Codex CLI's `hooks` feature flag is what `docs/RUNBOOK.md` §0-3 gates on, so a machine without it cannot start a run. The leaf-`LLM` configuration is untracked by design, and it is authoritative for a leaf's model **when it declares one**; when it does not, the CLI's own default decides — and for a `pure` claude leaf, which is launched with no private configuration directory, that default can come from the operator's own settings.

| step | requirement | canonical source |
|---|---|---|
| 1 | Host CLI tools, Python packages, and the target `spec`'s toolchain and `static lint` tool | `docs/RUNBOOK.md` §0-1 |
| 2 | Claude backend: server registration and the leaf's tool grant | `docs/RUNBOOK.md` §0-2 |
| 3 | Codex backend: the CLI feature flag, the credential, the writable state home | `docs/RUNBOOK.md` §0-3 |
| 4 | The leaf-`LLM` configuration file, created by copying a sample | `docs/RUNBOOK.md` §1-3, `README.md` §"Running a workflow" |
| 5 | The sandbox runtime | `docs/BWRAP_ENABLEMENT.md` |
| 6 | **To run the TEST SUITE** — every `static lint` tool, not only the one this tree's nodes select | this section, below |

**Step 6 is a DEVELOPER requirement and it disagrees with step 1 on purpose.** `docs/RUNBOOK.md` §0-1 is written for someone running a workflow, and it says in as many words that an operator installs `fortitude` and neither of the other two — correct, because the linter a run selects follows from its `toolchain.language` and every node in this tree is `fortran`. The SUITE is a different question: each linter backend's tests drive the real tool and deliberately FAIL rather than skip when it is absent, because a machine without the tool cannot certify anything and a green suite there would report that a gate is fine when nothing ran it (`.claude/skills/atmofab-enforcement-change` judgment rule 2). So a fresh clone that installs only what §0-1 lists gets a red suite whose message points back at §0-1. Install all three — two of them from the dev requirements file:

```
pip install -r requirements-dev.txt        # the two pip-installable linters, plus pytest and the
                                           # runtime dependencies the suite imports
sudo apt-get install cppcheck              # tools/tests/test_linter_cppcheck.py
```

Nothing above states a version range, and that is the point: the ranges are the backends' own
`SUPPORTED_VERSION_SPEC`, and the two places that DO spell them are both checked against the
declarations — `requirements-dev.txt` by `tools/tests/test_dependency_declaration.py`, and
`docs/RUNBOOK.md` §0-1's table (which also covers the one apt installs above) by
`tools/tests/test_host_prerequisites.py`. This block used to spell them a third time, unchecked,
which is the shape that let an install line drift out of the range the launch probe accepts.

**On a PEP 668 host the `pip` line above aborts, and that is a change this repository made.** A
current LTS — Ubuntu 24.04+, Debian 12+, Fedora — marks the system interpreter
`externally-managed`, and `pip install` into it refuses with `error:
externally-managed-environment`. Step 6 used to say `pipx install`, which is immune because it
builds its own environment per tool; installing from a requirements file is not. Use a virtualenv,
or `pip install --user`, which is how THIS development host was actually built (both linters and
`pytest` live in `~/.local/bin`; measured Ubuntu 22.04, Python 3.10.12, no
`EXTERNALLY-MANAGED` marker, so the plain command works here and this note is for the machine you
are on, not for this one). `docs/RUNBOOK.md` §0-1's `pip install -r requirements.txt` has the same
property and always did.

`pipx` installs a COMMAND-LINE TOOL into its own environment, so it is the right instrument for
the two linters and the wrong one for the rest of the file. A `pipx` user runs
`pipx install '<line>'` for each of the two linter lines of `requirements-dev.txt`, quoting each
as written — and then still needs `pip install -r requirements.txt` plus `pytest` in the
environment the suite runs in, because those are IMPORTED rather than executed. Doing only the
`pipx` half leaves a machine with the two linters and no test runner.

Steps 1, 2, 3 and 5 all read machine-local state, and each is checked before the first billed leaf — though not all by the same mechanism. Step 1 fail-fasts when `tools/run_workflow.py` starts, before an orchestration exists — with one reason code per family (`missing_required_cli_tools` / `missing_required_python_modules` / `missing_required_host_tools`); steps 2, 3 and 5 are `preflight.json` checks. One requirement is outside both and is called out where it lives: the Codex credential is checked when the first leaf is prepared, not at any gate (`docs/RUNBOOK.md` §0-3).

## Configuration layers
Two sessions run against this checkout, and they load disjoint configuration. An operator's own interactive session loads the DEV layer; a workflow leaf loads the LEAF layer and nothing else. Since issue #102 that disjointness reaches the HOOK as well as the file: the DEV rows name `tools/hooks/dev_cli.py`, which applies `tools/hooks/operator_safety.py` and `tools/hooks/dev_session_hygiene.py` and nothing else (`docs/HOOKS.md` is canonical for which of the two a leaf also gets), and the LEAF rows name `tools/hooks/cli.py`, which fails closed when it cannot name an orchestration. `docs/HOOKS.md` is canonical for the split.

| file | layer | read by | tracked |
|---|---|---|---|
| `leaf_config/claude/settings.json` | LEAF | a workflow leaf, as the sole settings layer of a host-prepared private configuration directory | yes |
| `leaf_config/codex/hooks.json` | LEAF | a workflow leaf, through a digest-verified copy in the isolated home | yes |
| `.codex/hooks.json` | DEV | an operator's own interactive codex session, as the project hook layer | yes |
| `.mcp.json` | LEAF | a workflow leaf, named explicitly at launch | yes |
| `.claude/settings.json` | DEV | an operator's own interactive session | yes |
| `.claude/settings.local.json` | DEV | the same session, per operator | no |
| `.claude/skills/` | DEV | the same session | yes |
| `llm.yaml` | run input | the driver, to decide what each leaf launches | no |
| the operator's own CLI configuration directories | personal | the operator's own session, and the CLI's authentication | out of tree |
| `~/.atmofab/` | runtime state | the host, per orchestration | out of tree |

Three consequences worth stating explicitly:

- **A permission or hook the workflow depends on goes in a committed file.** The untracked local files exist for one operator's scratch; a grant that lives only there works on one machine and nowhere else. When both a tracked and an untracked file could hold a setting, the tracked one is the answer.
- **The leaf layer is the owner when a setting appears in both.** Edit the leaf file first; a synchronization test requires the dev layer to carry every leaf permission GRANT, so an operator can reproduce what a leaf does. It requires nothing of the hooks any more except that the two layers stay APART: since issue #102 they name different entrypoints and share no command, while the dev layer may carry hooks of its own. Equality was tried and refused: it forbade adding any operator-convenience hook to the file whose purpose is the operator's session.
- **A workflow leaf reads none of the repository's top-level instruction documents.** `CLAUDE.md`, `AGENTS.md`, and the dev skills are the DEV layer. A leaf's contract arrives through its launch prompt: `docs/AGENT_CONTRACT.md` plus the phase `SKILL` under `skills/`. A rule a leaf must follow therefore has to land in one of those, never here.

## Repository environment
Facts about this repository that decide how an operator's own session should be configured. They are stated here because a session-level safety configuration is per-user and per-machine, so this repository cannot hold the configuration itself — only the material an operator builds one from.

- **The repository is public.** Anything pushed to `origin` is published. Only this repository's own work belongs there; credentials and personal data are never made safe by the visibility setting.
- **`main` is the default branch and carries no protection rule.** Nothing on the hosting side refuses a direct push to it. A change lands through a branch and a pull request by convention, and the convention is the only thing enforcing it.
- **CI runs the suite, and it does not run everything.** `.github/workflows/tests.yml` runs `python3 -m pytest tools/tests/ -q -rs` on every pull request and on a push to `main`, on `ubuntu-22.04` whose SYSTEM interpreter is Python 3.10 — the version every measurement in this repository was taken on — with the packages `requirements.txt` and `requirements-dev.txt` pin. It is not a second opinion about other machines: its job is to make the operator's own claim on a round the operator forgot to measure, which is why there is no version matrix. **A measured figure is still recorded with the commit it was taken at**, and that has not changed: CI reports the suite, not the numbers this repository quotes.
  - **What CI does NOT cover**, so a green check is not read as more than it is: a billed `LLM` run (no leaf is launched); the mutation check, which is a procedure an author runs over their own diff; the linter-statistics delta, likewise; a host whose `static lint` tool is outside its backend's declared range, since CI installs one inside it; and the differential harness that checks the language front end against an independent one, which needs a program neither the runner nor the development host has (`TODO.md` records the split). The sandbox IS covered — the runner lifts `kernel.apparmor_restrict_unprivileged_userns` so `bwrap` runs rather than skipping. Two more, added after a review round asked whether the list was complete: **a branch pushed with no open pull request is not checked at all** (the triggers are `pull_request` and a push to `main`), and **no real backend CLI is exercised anywhere** — the tests that used to invoke one did so only because the developer's machine had it, and each now supplies what it needs, so CI proves nothing about a CLI's actual behaviour.
  - **The runner's own versions are printed by the job**, for the same reason a figure names its commit: without that step a red run cannot be attributed to a version, and the log is the only place that information exists.
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

The boundary rule bounds knowledge of a concrete target-stack technology in the NEUTRAL CORE — the code, templates, skills, and documents through which a run produces a target-stack system. `.claude/` is none of that. It is the operator's own development session: measured on CLI 2.1.235, a workflow leaf loads zero of its skills and its request carries neither skill's name. `CLAUDE.md` holds that measurement and the three limits it was taken under — a hand-assembled launch, a scratch checkout, and the agentic leaf shape only. The technology spellings it does carry are ABOUT the instrument — a checklist for reviewing this repository's own gates — which is the reason `tools/tests/` is out of scope. (`docs/design/` is out for a different one that `docs/BACKEND_BOUNDARY.md` states: a decision note is expected to name the technology it decides about.)

The cost of that decision, stated rather than left to be discovered: those spellings are unmeasured. The ratchet does not read `.claude/`, the migration ledger does not count what is there, and a change of target-stack technology has to sweep that tree by hand. The material is also not where a reader would guess. Measured at `a2cc438`, where the directories are spelled `metdsl-*` because that commit predates the rename (`docs/GLOSSARY.md` §13): 38 occurrences, of which 35 are in `atmofab-enforcement-change` and 3 in `atmofab-review-loop`, and only 3 of the 35 are the explicit spelling checklist — the rest are episodes and identifier names. A sweep that fixes the checklist has moved almost nothing; a sweep that skips the second skill has missed almost nothing. `TODO.md` holds the per-file breakdown and the commit it was taken at — including a later re-measurement, taken after the two skills were split into rule + episode files, at which point **none of the occurrences is in either `SKILL.md`**: the explicit spelling checklist moved whole to `atmofab-enforcement-change/references/source-text-surface.md`, so a sweep starts there rather than at the figures quoted above.

## Design Policy
- **Complete the canonical document before adding a prohibition.** When an agent's behaviour is undesirable, first ask whether the information that would make the desirable route obvious exists and is reachable. A prohibition removes judgment and stops real work in the cases nobody anticipated; a complete document does not. Reserve an enforcement rule for what a document cannot address, and make its remediation text an anchor to the canonical document rather than a statement of the ban.
- **Do not persist a value an existing artifact already carries.** A second copy splits the source of truth. When restoring a value on resume, inventory what is already written before proposing a new file; when a parsing coupling is the concern, pin it with a round-trip test rather than with a new artifact.
- **Correctness is not traded for throughput.** A change that can lower the rate at which the workflow produces correct results is not adopted, whatever it saves. Optimizations that preserve the judgment — moving a purely deterministic verification out of an `LLM` substep, sharing a read-only prefix across leaves — are.
- **Do not create a second document holding the same information.** When a reader needs a subset of a document, SPLIT the canonical one and cite the parts; do not derive a digest that must then be kept in step.
- **Repair the designed route before adding a second one.** When information fails to reach a consumer, the permanent fix is the route the design names — not an injection that supplies the value from somewhere else. A bypass makes the consumer succeed while destroying the signal that the route is broken, so the defect stops being detectable. Where a bypass is genuinely needed as an interim measure, it ships only together with its REMOVAL CONDITION and removal procedure, written into `TODO.md` at the same time; an interim measure whose removal is a verbal promise is not proposed.
- **State a rejected alternative with the measurement that rejected it.** A design that was considered and dropped is recoverable only if the reason is written down; without it the same option is re-proposed, and re-measured, later.
- **A deterministic gate flags only unambiguous structural failure; delegate the rest to a semantic check that can read intent.** A required-dataflow gate tried to decide whether a value reaches a required output using only regex plus flow-insensitive reasoning; every variant that added call-graph awareness traded one false positive for a different fail-open, because a threading chain and a dead-write overwrite are the same syntactic shape with opposite meanings — separating them needs callee argument intent and reaching-definitions, which a deterministic gate does not have and a semantic-review leaf that reads the spec does. Before writing a new deterministic gate, ask whether the judgment needs callee intent, flow-sensitivity, or the spec's prose; if it does, do not force it into a regex — delegate to the semantic check, and record why in `TODO.md` so the question is not re-litigated. The same review-loop signal applies here as elsewhere: a heuristic that draws a fresh finding every round on the same function is not under-tuned, it is over its ceiling.
- **Do not distill a canonical rule from prose when the rule already exists as code or as a generated artifact — read or inject the authority itself.** A prompt section built by paraphrasing a canonical document, rather than by reading the gate implementation that enforces it (and running the real tool), reproduces the paraphrase's errors invisibly: a prose-level review checks the paraphrase against the prose it came from and both agree, while only a review that opens the enforcing code catches the drift. Where a host already renders the fact deterministically (a generated artifact, a compiler's own output), the more reliable fix is not a better paraphrase — it is injecting that artifact verbatim, so the correctness surface moves from a distillation step to a copy.

## Operations Rules
- **One driver per workspace.** Never drive an orchestration by hand while a conductor is running it, and never start a second orchestration against the same workspace. Write attribution is derived from a workspace-global baseline diff, so concurrent runs mis-attribute each other's writes and fail on write-authorization violations. While a run is in flight, treat the workspace as read-only.
- **Verification is performed by a separate persona.** Reusing a generating session to verify its own output is not an optimization with a cost; it is the loss of the property verification exists for. Independence means a fresh inference context that inherits neither the producer's conclusions nor its reasoning. This applies to the workflow's `generate` / `verify` substeps and to a review of a change to this repository alike.
- **A version identifies content, not a re-run.** Do not bump a version to force re-certification while the content is unchanged; freshness is the readiness machinery's responsibility. A change of REPRESENTATION is a content change and does bump.
- **Pin what was used, not what will be used.** Model selection uses an unpinned alias so it does not go stale; the record must carry the exact version that actually ran, resolved at run time. A guessed version written into a record is worse than no record.
- **Measure before asserting.** Do not write a number, a count, or a causal claim into a document, a commit message, or `TODO.md` that was not produced by running something. The verification commands are in the `atmofab-review-loop` and `atmofab-enforcement-change` skills.
