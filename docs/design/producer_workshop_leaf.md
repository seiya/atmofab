# Producer leaves as sandboxed agents, judging leaves as pure functions

Status: **proposed**. Recorded 2026-10-22 from a zero-base reconsideration of the leaf execution model against the measured cost of the pure-function path. This document extends `zero_base_architecture.md` and departs from three of its items (§Departures): the deterministic `conductor`, the host-owned evidence channel (A4 / Z6), the derivation-keyed store (Z5) and the `CodegenBundle` contract (Z0) are kept as they stand. No item below is implemented. The term `workshop leaf` is defined in `docs/GLOSSARY.md` with a `proposed` mark that the adopting pull request removes; the mark is a convention this note introduces for a term whose definition precedes its adoption.

## Purpose

Decide which `LLM` `substep` keeps the `pure-function leaf` shape and which one changes to an agent working in a sandbox, state the sandbox each shape requires, state what the producer is shown and what that leaves unprotected, and state the evidence on which the change is adopted or declined.

## Scope

- The five `LLM` leaves `compile.generate`, `compile.verify`, `generate.generate`, `generate.verify`, `validate.judge`, and the `escalate` diagnostician.
- The sandbox of a leaf.
- Out of scope: the phase sequence, the gates, the verdict evaluator, the store, and the sandbox of the deterministic `Build` and `Validate.execute` substeps (§Not adopted). None of them changes under this note. The per-phase I/O contract sentences the adopting pull request rewrites are listed in §Departures.

## Terms

- **`workshop leaf`**: an `LLM` agent that runs with a tool surface inside a host-prepared, host-confined work tree, and whose result is the final state of that tree. Defined in `docs/GLOSSARY.md` (proposed).
- **work tree**: the directory the host seeds for one `workshop leaf` lineage and the leaf writes into. It is not an artifact: no gate, no later `phase` and no other lineage reads it, and a warm repair of the same lineage is the only later reader.
- **lineage**: one `workshop leaf` session together with its work tree: the cold launch that started it plus every warm repair of it. The word is used as `codex home lineage` uses it (`docs/GLOSSARY.md`), and is distinct from `attempt`, which the glossary defines as one execution of a `phase`.
- **seed**: the files the host writes into a work tree before launch; one file per key of the producer's pure context (§Seeding). A seed is an input: the snapshot never copies one, and the host's own copy is what every gate reads.
- **snapshot**: the host's copy of the work tree's final state into the producer's existing output contract (§Hand-off), taken after the leaf exits.
- **wrapper**: the host-side service through which a `workshop leaf` compiles, lints, syntax-checks and runs. A tool call crosses the sandbox boundary to the conductor's process, which runs the build-runtime library (`tools/build_runtime.py`) under the lineage's own confinement (§Sandbox). No file of `tools/` is mounted into the leaf's sandbox.

## Premises

The premises of `AGENTS.md` §Development premises apply unchanged, and one operator-stated constraint of `zero_base_architecture.md` §Premises (its item 5) joins them. Three decide this note:

1. **A leaf is not malicious and takes shortcuts** (`AGENTS.md`). A hole is in scope when a leaf that takes it gets closer to reporting its `substep` done. A defense against the operator, who owns the machine, is out of scope.
2. **No vendor lock-in** (`AGENTS.md`). A change to the leaf model is complete only when it lands on every declared provider, the HTTP transports included.
3. **Cost per certified artifact is a design objective** (`zero_base_architecture.md` premise 5). The dominant term is output tokens: a leaf's wall clock is its model generation at a throughput floor of about 100 tok/s on Opus (`skills/workflow-timing-audit/SKILL.md`; an HTTP endpoint of the recorded corpus ran at 17.6 to 59.8 tok/s, `docs/ORCHESTRATION.md` §Leaf LLM configuration), and prompt caching reduces input cost only.

## Measured baseline

The figures that decide the shape of each leaf, all from recorded runs. Every orchestration of 2026-10-22 ran AFTER the part split of issue #526 (PR #528 merged 2026-10-21T20:24:58Z). The channel node `shallow_water2d_channel@0.2.5` compiles as 6 parts: the core and one verification part per test.

| measurement | value | record |
|---|---|---|
| an `LLM` orchestrator resuming one node (advdiff): turns, context, tokens | 242 turns, 22K to 274K context, about 40M tokens | `deterministic_conductor.md` §Why |
| `validate.judge`, agentic against pure, same verdict | 2,122,319 tokens / 396 s against 202,313 / 109 s | `zero_base_architecture.md` Z3 |
| `compile.generate` of the channel node before the part split, codex `gpt-6.1-sol`: one-reply launch prompt; outcome | 301 KB, about 98.7k input tokens; `compile_declared_fail` 3 of 3, no IR written, each reply stating that no input was defective | issue #526 |
| the same node after the part split, codex, one orchestration | 5 part leaves launched, of 300,926 to 333,608 bytes each, one attempt each; 4 passed, the fifth declared the contract defect issue #532 fixed, so the sixth was not launched; 640,692 tokens, no IR assembled | `orch_20261022T120314Z_b2c31943` |
| the same node after the part split, codex, `--with-deps` | 17 of 18 compile leaves passed, the IR was assembled twice and revoked twice (`Compile.static`, then `Compile.verify`); 3,353,660 tokens, then the provider's credits ran out | `orch_20261022T065443Z_9aae2fe2` |
| the same node, claude `opus-5-5`, `fortran_cpu`: `compile.generate` | 12 part leaves passed (the first assembled IR revoked by `Compile.static`, the second certified); 1,236,670 + 1,570,339 tokens, 380,704 + 317,084 of them output | `orch_20261022T025156Z_10ba647b` |
| the same orchestration: `generate.generate` on the channel node, one reply | passed on its first attempt; 116,854 output tokens, 530,679 total; `generate.verify` passed; the run stopped at `Validate.execute` (`validate_execute_physics_fail`) | same |
| the same node, claude `opus-5-5`, `fortran_cpu_mpi`: `generate.generate` | first reply `pure_response_unparseable` at 136,714 output tokens (1,104,639 total); the warm repair reply passed at 39,145 output tokens; a second source after a gate finding passed at 38,366 output tokens; the run stopped at `Validate.execute` (`validate_execute_physics_fail`) | `orch_20261022T032513Z_166b03d0` |
| the same node, claude `opus-5-5`, core part only | the core part passed in one reply, 169,451 tokens; the driver was interrupted (`driver_interrupted`) while the second part ran | `orch_20261022T064517Z_5689df1d` |
| one small component node, claude `opus-5-5`, first-attempt pass (`rk4`, `fortran_cpu_mpi`) | 3 leaves, 264,875 tokens; the orchestration's wall clock, which held no other `LLM` leaf, 8 min | `orch_20261022T040744Z_2a842495` |
| its `generate.generate` leaf | 40,254 output tokens, 71,527 cache-creation input tokens | same |
| its `generate.generate` prompt | 160,916 bytes, carrying the whole `tests.md` and the IR's `primary_predicates` and `test_predicates` | same, `launches/b7323baa-….prompt.txt` |

Four facts follow for the producing leaves.

- **A producer that must write a whole document in one reply gave up on a large node on one provider, and writes about 117k to 137k output tokens per reply on the other.** The give-up record is codex before the part split (issue #526). The claude records are the channel bundle at 116,854 output tokens in one reply (about 20 min of generation at the Opus floor), and one reply that reached 136,714 output tokens and could not be parsed. One such reply alone exceeds the 100k output tokens per certified node that `zero_base_architecture.md` §Decision Criteria targets.
- **The part split pays the whole prompt per part.** It is a host-side decomposition of the work that an agent harness performs itself, and it does not shrink the prompt: each part is handed the whole input again, so the 6-part channel node pays 6 launch prompts of about 100k tokens, twice when `Compile.static` revokes the first assembly (the claude row: 2,807,009 tokens for one certified IR).
- **Every repair re-emits the whole document.** The repair contract on both the warm and the cold path is one complete corrected document (`tools/prompt_templates/pure_bundle_repair.txt`; `docs/ORCHESTRATION.md` §pure-function leaf; issue #510). A one-line gate finding against a bundle costs a whole-document reply again; the recorded warm repair of the channel bundle cost 39,145 output tokens, and a cold regeneration after `Validate.execute` costs the full first-reply figure.
- **A physics failure at `Validate.execute` regenerates cold.** Both claude channel runs ended there; the next attempt pays the first-reply figure again with no tree to return to.

One fact for the judging leaves: a judging leaf reads and decides, performs no search, and is the leaf on which the agentic shape measured ten times the cost of the pure shape.

One fact about what the producer is shown, and it is a fact about the CURRENT design: on an `m3c` node `generate.generate` is handed the whole `tests.md` (`tests_document`), the host-rendered runner (`runner_document`), the whole `spec.ir.yaml` with its `primary_predicates` and `test_predicates`, the target profile and the harness capabilities — the five keys of its pure context (`tools/workflow_conductor.py` `_build_pure_context`; `tools/tests/data/conductor_launch_requests/generate_generate.request.json`), plus the dependency facts and the exemplar the template carries; on the `harness` shape the keys are those of `_build_pure_harness_context` (no runner, the runner output contract, the gate guards and the lint rules instead). `compile.generate` is handed `tests.md` and `controlled_spec.md` as primary inputs. What no producer is shown is the host evaluator (`tools/verdict_evaluator.py`, `tools/primary_evidence.py`), the judge's excerpt policy, and every past artifact. The defense of the current design is therefore not secrecy of the judgment but host ownership of the evidence channel (A4, Z6): a leaf that reads every predicate and holds no write path to captured state gains nothing from reading them, and the one gain it keeps — registering state that fits the visible cases — is the accepted residual A4 names, accepted there on the ground that such a state is a harder defect to produce BY ERROR than checks that report pass.

## Design Policy

The leaf model is asymmetric.

| role | leaves | shape | reason |
|---|---|---|---|
| judging | `compile.verify`, `generate.verify`, `validate.judge`, `escalate` | `pure-function leaf`, unchanged | reads and decides; the agentic shape measured 10× the cost; an independent reviewer must share no context with the producer |
| producing | `compile.generate`, `generate.generate` | `workshop leaf` | the one-reply shape gave up on a large node on one provider, writes 117k to 137k output tokens per reply on the other, pays the whole prompt per part, and re-emits whole documents on repair |

What is NOT adopted, and the rule that rejects it: an `LLM` that drives the phase loop, or that holds the gates, the evaluator or the verdict. The first is the $O(\text{turns}^2)$ cost in the baseline table; the second places what judges the result within reach of a leaf under a goal, which is the `leaf shortcut` class.

### `workshop leaf`

A `workshop leaf` replaces the one-reply document of a producing `substep`; it replaces nothing downstream of the producer.

- **Seeding.** The host writes into the work tree one file per key of the producer's pure context as it is rendered today, plus the dependency facts, the exemplar and the phase contract text the template carries. Nothing else is placed and nothing else is reachable. The runner is a seed on an `m3c` node: `run_phase` renders it from the IR before any generate substep runs (`tools/workflow_conductor.py` `_write_runner`), and the leaf's compile reads the seeded copy while the host's `Build` reads the host's. Whether a producer should be shown less than today is a separate decision (§What the producer is shown).
- **Tool surface.** Read and write within the work tree, and the wrapper's five operations: `derive` (the Generate-time derivation the host performs on an accepted bundle — the build graph from `bundle.yaml`'s roles (`tools/codegen_bundle.py` `derive_build_graph`, applied by `tools/workflow_conductor.py` `_build_pure_bundle_graph`) and the control files rendered from it (`_render_pure_makefile_from_graph`) — applied to the tree as it stands, so it needs `bundle.yaml` to exist and refuses a tree without one); compile of the derived graph; `static lint`; `syntax check`; and execution of the built binary on inputs the leaf writes, the IR's declared cases included. The lint and syntax results are feedback: the host's `Generate.gate` and `Compile.static` still run on the snapshot and are the authority, and a seed the leaf edited changes the feedback and nothing else.
- **Not on the surface.** The verdict evaluator (`tools/verdict_evaluator.py`, `tools/primary_evidence.py`), the gates, every `workspace/` artifact of any other lineage, the CLI's session store (§Sandbox), `spec/`, `tools/` (the wrapper is a service, not a mount), `docs/`, `.git/`.
- **Output files in the tree.** `generate.generate` writes `bundle.yaml` at the tree root carrying every member the bundle schema requires (`spec/schema/generate/codegen_bundle.schema.json`) other than `files[].content`, which the snapshot reads from the files the manifest names. `compile.generate` writes `spec.ir.yaml` at the tree root, or, when it finds an input absent, self-contradictory or insufficient, `compile_fail.json` in the structured form the pure leaf returns today (`{input, where, what}`, with `input` validated against the admissible context keys, `docs/ORCHESTRATION.md` §pure-function leaf), which the snapshot treats as the `compile_declared_fail` exit under the same rules. A tree holding none of its output files — the `max_wrapper_calls` stop among the causes — is snapshotted as `workshop_tree_empty`, a failure category routed as a malformed document is today (bounded repair, then a cold restart).
- **Hand-off.** The snapshot is validated under the existing contract and the existing gates run in the host's own process. Every artifact a later `phase` reads is host-written from the snapshot, as today.
- **Repair.** The directive selects the path, as `docs/ORCHESTRATION.md` §repair / retry defines it; what changes is what each path carries. **`reuse`, session continuable (warm)**: the gate finding is appended to the SAME session over the SAME work tree, and the leaf edits in place; a CLI provider continues its session as `warm_resume` does today, and the host tool-use loop holds the conversation in memory as `_run_http_leaf` does today. A cross-phase `reuse` reopen of the producer is this path when the lineage still exists. **`reuse`, session not continuable (cold reuse)**: a new lineage whose tree is seeded from the snapshot of the lineage being repaired plus the findings. **`restart`**: a new lineage seeded from the seed alone, with no snapshot and no findings, because a `restart` re-derives from scratch and reads none of the discarded attempt (`docs/ORCHESTRATION.md` §repair / retry; `deterministic_conductor.md` §Design). On no path does the leaf re-emit the whole document; the whole-document repair contract is retired for the producing leaves.
- **Independence of review.** A `workshop leaf` may use its harness's sub-agents internally; they share its tree and its surface. `generate.verify` and `compile.verify` remain pure leaves outside the work tree, launched by the host from the snapshot, sharing no session and no tree with the producer.
- **Record.** Under the host tool-use loop every request, response and tool call passes through the host and is recorded under the lineage, as the pure HTTP leaf's `http_response.txt` is today: the record is total by construction. Under a CLI harness the host records what the CLI emits (its result envelope and transcript), and `docs/ORCHESTRATION.md` §pure-function leaf already records that a CLI leaf's input is not a function of the host prompt alone. On either transport the wrapper writes `command_log.jsonl` under the lineage's record outside the tree — the host's own record of every build-runtime call, which the leaf cannot edit. Usage is recorded per turn, with the running-total trap of `TODO.md` (issue #281) resolved for each agentic transport before its first billed comparison.
- **Call cap.** A `workshop leaf` entry in `llm.yaml` carries a required `max_wrapper_calls` field; there is no default, for the reason `docs/ORCHESTRATION.md` §Leaf LLM configuration gives for `llm.yaml` itself. The wrapper counts its own calls, which the host sees on every transport, and at the cap refuses further calls and the host terminates the leaf; its tree is snapshotted as it stands. A CLI turn count is not the mechanism: measured 2026-10-22, `claude --help` (2.1.308) and `codex exec --help` (0.163.0) expose no turn bound.

### What the producer is shown

Two options; the first A/B runs under option S, and option W is the operator's decision, not decided by this note.

- **Option S (shown, as today).** The seed is the producer's current pure context. The residual, named on its own ground: a producer that can execute its binary on the IR's declared cases, read every predicate and read its own captured state can fit the state to the visible predicates by feedback and confirm the fit before hand-off. A4's acceptance of the fitted-state residual does not transfer, because A4 accepts it as a defect harder to produce by error, and under S it is produced by a goal-directed loop. What stands against it under S is `Generate.verify`'s advisory rows G1 / G3 / G5 (`docs/workflow/phases/phase_02_generate.md` §2-2) and nothing deterministic. The route is `generate.generate` to snapshot to a passing verdict; the gain is a certified kernel correct on the declared cases and on nothing else. The A/B's choice of S is made with this gap named.
- **Option W (withheld).** The seed carries the IR with `io_contract.test_predicates[].pass_when` and `io_contract.primary_predicates` removed and `tests.md` removed, keeping `diagnostics_contract` (the check ids and verdict fields the runner must emit) and the case set. Undefined under this note and refused by it: which IR sections a producer needs to emit a conforming runner, and whether `Compile.generate` can be run under W at all (its primary input is `tests.md`). Adopting W is a phase-contract change (`docs/workflow/phases/phase_02_generate.md`) with its own review.
- **Held-out cases.** Deferred and undefined. Running host-derived cases at `Validate.execute` needs two things neither `docs/TESTS.md` nor `tools/primary_evidence.py` provides: a predicate scope for a case outside a predicate's `target_cases` (the grammar ranges over exactly those, `tools/primary_evidence.py` `_predicate_scope`), and a reference computed from the inputs rather than a band calibrated against one (`docs/TESTS.md` §Refinement judgment, §Symmetry pairs). No `tests.md` in the tree declares an admissible input range (enumerated 2026-10-22 over the 18 `spec/**/tests.md` with `os.walk`; 0 matches for an admissible-range statement). This note counts held-out cases as no defense.

### Sandbox

Three rows. Every row describes the LOCAL site; a remote `execution site` (`docs/ORCHESTRATION.md` §Execution sites) runs no `LLM` leaf.

| sandbox | holds | writable | network | technology |
|---|---|---|---|---|
| `workshop leaf`, CLI transport (the leaf process) | the work tree; the provider CLI read-only; a PRIVATE per-lineage config home holding the provider's credential material | the work tree and the private home | the configured provider endpoint only | `bwrap` + a network namespace |
| the wrapper's runs (every transport): the toolchain, the lint tool, the built binary | the work tree; the toolchain read-only | the work tree | none | `bwrap`, run by the conductor's process |
| pure leaf, CLI transports | nothing (an empty tmpfs at the checkout path, as since issue #227); the CLI's config home read-write, as today | its scratch root and the CLI's config home | unrestricted today; the configured provider endpoint only under this note | `bwrap` |

A `workshop leaf` on an HTTP transport has no leaf process: the host tool-use loop runs in the conductor's process and the second row is its whole sandbox. The second row exists because the binary the wrapper runs is leaf-authored code: it is confined to exactly what the leaf is confined to, so nothing the leaf cannot reach through a tool becomes reachable through a program it wrote. The `Build` and `Validate.execute` of the SNAPSHOT stay where they are (§Not adopted).

The private home is required because a tool-bearing leaf can read what a tool-free one cannot: today the CLI's config home (`~/.claude` with `~/.claude.json`) is bound read-write into every claude leaf for auth and the session transcript (`tools/orchestration_runtime.py`, the rw bind its profile builder documents), and that home holds the transcripts of every other leaf of every orchestration under `~/.claude/projects/`. A `workshop leaf` with a read tool in that sandbox could read a sibling lineage's bundle, the reviewer's findings on its own previous attempt, or the attempt a `restart` discarded — each a past artifact read as input, the `leaf shortcut` `docs/workflow/WORKFLOW_CORE.md` forbids. The granularity is one home per lineage, because a warm repair resumes the session the lineage started (`--resume <arid> --fork-session`, `tools/pure_leaf.py`) and needs that transcript and no other: the codex lineage home of issue #245 (one `CODEX_HOME` per thread, `auth.json` alone contributed from the original) is the shape. The claude counterpart is one `CLAUDE_CONFIG_DIR` per lineage; what the claude CLI needs in it beyond the credential file is undefined here and is measured by the adopting pull request (the per-orchestration home of issue #63 held a settings file and a credential placeholder over which the operator's credential file was bound, and is not this shape). The network rule applies to the first and third rows: a namespace whose only route is a host-provided forwarder to the configured endpoint, so that no leaf reaches an endpoint the host did not configure. It is a new requirement on the pure CLI leaf as well, whose profile today carries no network namespace (`tools/orchestration_runtime.py` `render_bwrap_command`), and it does not reverse the leaf-environment rule of issue #83 (`LEAF_ENV_NAMED_EXCLUSIONS`): the forwarder is reached by the namespace's route, not by a proxy variable.

Virtualization stronger than user-namespace containment (a VM, a user-space kernel) is not required, and the rule that declines it is premise 1: the holes a VM closes beyond `bwrap` are holes against the operator's machine, and a leaf gains nothing toward its verdict from any of them.

### Departures

This note departs from three items of `zero_base_architecture.md`, and names what returns with each.

- **A2 (every LLM stage a pure function with no gate invocation).** A `workshop leaf` invokes lint, syntax and compile through the wrapper. What does not move: the gates the host runs on the snapshot remain the authority, and the leaf's invocations are feedback with no standing in any record a gate reads.
- **A3 (the model cannot write).** A `workshop leaf` writes into its work tree. What does not move: the tree is not an artifact, every artifact is host-written from the snapshot, and no FS-diff authorization is needed because no write of the leaf is authorized or refused — it is copied or not copied by the snapshot's contract.
- **Z4 (enforcement retirement).** Two of the retired items return in a narrower form: a per-lineage writable `bwrap` profile (the work tree and the private home), and a tool-call record (the wrapper's `command_log.jsonl`). What does not return: capability tokens, output and read manifests, PreToolUse hooks, write-scope baselines, launch-marker validation.

The contract sentences the adopting pull request rewrites: `docs/workflow/phases/phase_02_generate.md` "`static lint` is NOT run by the `Generate.generate` leaf" and "the `Generate.generate` leaf is a `pure-function leaf`"; `docs/ORCHESTRATION.md` §pure-function leaf "exactly two launch shapes" (a third, the `workshop leaf`, is added for the two producing substeps); `tools/llm_config.py`'s rule that every LLM leaf requires the `pure` capability (a producing leaf requires `workshop` instead); and the `Data egress` statement of `docs/ORCHESTRATION.md` §Leaf LLM configuration that an HTTP leaf runs no tool.

### Provider neutrality

A `workshop leaf` needs an agent loop. The CLI providers (`claude_cli`, `codex_cli`) carry one; the HTTP providers (`openai_compatible`, `anthropic_api`) carry none. The rule: the host owns a minimal tool-use loop in `tools/` with exactly the tool surface above (read, write, the wrapper's five operations), driven over each provider's tool-calling API, and every provider runs a `workshop leaf` through it or through its CLI harness with the same surface. A CLI harness's additional capabilities (sub-agents, context compaction) are permitted on top of that surface and are not required by any contract. Keeping the HTTP providers on the pure producer instead is rejected by premise 2: a leaf-model change is complete only when it lands on every declared provider.

## Decision Criteria

The `workshop leaf` is adopted per producing `substep`, and the first is `generate.generate`.

- **Comparison.** A billed A/B against the pure producer on two nodes on `claude_cli`: one small component (the `rk4` row of the baseline, or its equal) and the channel node. Both arms on the same model and effort, under option S. The pure arm's figures for the channel node are already recorded (the two claude rows of the baseline); the A/B re-runs the pure arm only where its record is on another model or effort.
- **Measured dimensions**, each reported separately and none traded for another: certified outcome, output tokens per certified node, wall clock per certified node, first-attempt gate pass rate, repair count, and the number of wrapper calls and model turns.
- **Certified outcome, defined.** For a node that holds a certified pure-arm outcome on the same target, equal per-test statuses. For a node that holds none (the channel node holds none; both claude runs stopped at `Validate.execute`), certification with `basis.corroboration=agree` on every test of the verdict.
- **Adoption rule.** Adopt when both nodes certify under the `workshop leaf`, the certified outcome holds as defined, the small node's output tokens and wall clock are not worse than the pure arm's, and the channel node's `generate.generate` output tokens per certified node are not worse than the pure arm's recorded first reply (116,854). The `max_wrapper_calls` sweep is at most two values, each a billed run; a cost regression on the small node is adopted only with a value under which the small node stays within the pure arm's cost, stated in the record.
- **Decline rule.** When either node does not certify under the `workshop leaf`, or the small node's cost regresses under both values tried, the shape is declined and the fallback below is taken.
- **Cost target.** The target of `zero_base_architecture.md` §Decision Criteria (at most 10 min wall and 100k output tokens per certified node) is reported against, not a condition of adoption: the pure arm already exceeds it on the channel node with one reply.

## Migration order

1. `workshop leaf` for `generate.generate` on `claude_cli`, with the private per-lineage home, the network namespace, the wrapper and `max_wrapper_calls`; the A/B above.
2. On adoption: `compile.generate`, replacing the part split of issue #526 (the leaf splits its own work); `codex_cli`; the host tool-use loop for the HTTP providers.
3. Retire the whole-document repair contract for the producing leaves; the pure template pair `pure_generate_generate*.txt` / `pure_compile_generate*.txt` becomes the seed contract text of the work tree.
4. The network namespace on the pure CLI leaf's profile.

## Not adopted

- **Confining the `Build` and `Validate.execute` of the snapshot under `bwrap`.** Both run in the conductor's own process at the local site with no sandbox (`docs/BWRAP_ENABLEMENT.md`; `z4_enforcement_retirement.md` item 3), and as a submitted job at a remote site (issue #333). The judgment uses premise 1's decision criterion: what the operator's machine would gain is out of scope, and what a leaf would gain from the in-scope half — rewriting a snapshot file after the runner wrote it — is what Z6 already records as nil, because decoy storage gives the same value without it (`zero_base_architecture.md` Z6, the accepted residual). New evidence of a route that closure alone cuts reopens the item. The wrapper's runs (§Sandbox, second row) are a different case: they are confined because they are the leaf's own actions, not because of what they could reach.

## Fallback

When the A/B declines the shape, the producing leaves stay pure and two changes are made within that shape: the repair reply becomes a patch against the prior document that the host applies (no such form exists today), and `generate.generate` is produced in host-named parts as `compile.generate` is since issue #526. This keeps the one-reply model and its per-part prompt cost, and keeps the host responsible for splitting the work.

## References

- `zero_base_architecture.md` (§Premises, A2, A3, A4, A8, Z3, Z4, Z6, §Decision Criteria)
- `deterministic_conductor.md` §Why, §Design (the "Leaf sandboxing" paragraph)
- `z4_enforcement_retirement.md`
- `docs/ORCHESTRATION.md` §Leaf LLM configuration, §Execution sites, §repair / retry, §pure-function leaf
- `docs/BWRAP_ENABLEMENT.md`
- `docs/workflow/phases/phase_02_generate.md` §2-2
- `spec/schema/generate/codegen_bundle.schema.json`
- [issue #63](https://github.com/seiya/atmofab/issues/63), [issue #83](https://github.com/seiya/atmofab/issues/83), [issue #171](https://github.com/seiya/atmofab/issues/171), [issue #227](https://github.com/seiya/atmofab/issues/227), [issue #245](https://github.com/seiya/atmofab/issues/245), [issue #255](https://github.com/seiya/atmofab/issues/255), [issue #333](https://github.com/seiya/atmofab/issues/333), [issue #510](https://github.com/seiya/atmofab/issues/510), [issue #526](https://github.com/seiya/atmofab/issues/526), [issue #532](https://github.com/seiya/atmofab/issues/532)
