# Producer leaves as sandboxed agents, judging leaves as pure functions

Status: **proposed**. Recorded 2026-10-22 from a zero-base reconsideration of the leaf execution model against the measured cost of the pure-function path. This document extends `zero_base_architecture.md` (A2, A3, A8) and does not replace it: the deterministic `conductor`, the host-owned evidence channel (A4 / Z6), the derivation-keyed store (Z5) and the `CodegenBundle` contract (Z0) are kept as they stand. No item below is implemented. The term `workshop leaf` is defined in this note alone; its `docs/GLOSSARY.md` entry is added by the pull request that adopts the first such leaf, and until then the term appears nowhere else.

## Purpose

Decide which `LLM` `substep` keeps the `pure-function leaf` shape and which one changes to an agent working in a sandbox, state the sandbox each shape requires, and state the evidence on which the change is adopted or declined.

## Scope

- The five `LLM` leaves `compile.generate`, `compile.verify`, `generate.generate`, `generate.verify`, `validate.judge`, and the `escalate` diagnostician.
- The sandbox of a leaf, and the sandbox of the deterministic `Build` and `Validate.execute` substeps.
- Out of scope: the phase sequence, the per-phase I/O contracts, the gates, the verdict evaluator, and the store. None of them changes under this note.

## Premises

The premises of `AGENTS.md` §Development premises apply unchanged. Three of them decide this note:

1. **A leaf is not malicious and takes shortcuts.** A hole is in scope when a leaf that takes it gets closer to reporting its `substep` done. A defense against the operator, who owns the machine, is out of scope.
2. **No vendor lock-in.** A change to the leaf model is complete only when it lands on every declared provider, the HTTP transports included.
3. **Cost per certified artifact is a design objective.** The dominant term is output tokens: a leaf's wall clock is its model generation at a throughput floor of about 100 tok/s (`skills/workflow-timing-audit/SKILL.md`), and prompt caching reduces input cost only.

## Measured baseline

The figures that decide the shape of each leaf, all from recorded runs.

| measurement | value | record |
|---|---|---|
| an `LLM` orchestrator resuming one node (advdiff): turns, context, tokens | 242 turns, 22K to 274K context, about 40M tokens | `deterministic_conductor.md` §Why |
| `validate.judge`, agentic against pure, same verdict | 2,122,319 tokens / 396 s against 202,313 / 109 s | `zero_base_architecture.md` Z3 |
| `compile.generate` launch prompt, `shallow_water2d_channel@0.2.5` | 301 KB, about 98.7k input tokens | issue #526 |
| the same leaf, codex `gpt-6.1-sol`, one orchestration (2026-10-22) | 5 attempts, 640,692 tokens, no IR written | `orch_20261022T120314Z_b2c31943` |
| the same node after the part split of issue #526 | 3,353,660 tokens, then the provider's credits ran out | `orch_20261022T065443Z_9aae2fe2` |
| one small component node, claude `opus-5-5`, first-attempt pass (`rk4`, `fortran_cpu_mpi`) | 3 leaves, 264,875 tokens, 8 min | `orch_20261022T040744Z_2a842495` |
| its `generate.generate` leaf | 40,254 output tokens, 71,527 cache-creation input tokens | same |
| its `generate.generate` prompt | 160,916 bytes, carrying the IR's `primary_predicates` and `test_predicates` | same, `launches/b7323baa-….prompt.txt` |

Two facts follow for the producing leaves.

- **A producer that must write a whole document in one reply gives up on a large node.** The leaf of issue #526 stated in every attempt that no input was defective and that it could not complete the document; the part split of that issue is a host-side decomposition of the work that an agent harness performs itself.
- **Every repair re-emits the whole document.** The repair contract on both the warm and the cold path is one complete corrected document (`docs/ORCHESTRATION.md` §pure-function leaf, issue #510). A one-line gate finding against a bundle costs the bundle's output tokens again.

One fact for the judging leaves: a judging leaf reads and decides, performs no search, and is the leaf on which the agentic shape measured ten times the cost of the pure shape.

One fact about what the producer is shown: `Generate` is shown the judging predicates, because they are part of `spec.ir.yaml`, which is its whole contract. What it is not shown is `tests.md` prose, the host evaluator, the judge's excerpt policy, and every past artifact. The defense of the current design is therefore not secrecy of the predicates but host ownership of the evidence channel (A4, Z6): a leaf that reads a predicate and holds no write path to captured state gains nothing from reading it.

## Design Policy

The leaf model is asymmetric.

| role | leaves | shape | reason |
|---|---|---|---|
| judging | `compile.verify`, `generate.verify`, `validate.judge`, `escalate` | `pure-function leaf`, unchanged | reads and decides; the agentic shape measured 10× the cost; an independent reviewer must share no context with the producer |
| producing | `compile.generate`, `generate.generate` | `workshop leaf` (defined below) | the one-reply shape gives up on large nodes and re-emits whole documents on repair |

What is NOT adopted, and the rule that rejects it: an `LLM` that drives the phase loop, or that holds the gates, the evaluator or the verdict. The first is the O(turns²) cost in the baseline table; the second places what judges the result within reach of a leaf under a goal, which is the `leaf shortcut` class, and would require the enforcement complex that Z4 ([issue #171](https://github.com/seiya/atmofab/issues/171)) deleted.

### `workshop leaf`

A `workshop leaf` is an `LLM` agent that runs with a tool surface inside a host-prepared, host-confined work tree, and whose result is the final state of that tree. It replaces the one-reply document of a producing `substep`; it replaces nothing downstream of the producer.

- **Seeding.** The host writes into the work tree exactly what it inlines into the producer's prompt today: `spec.ir.yaml`, the target profile, the harness's public interface, each dependency's certified interface, the exemplar, and the phase contract text. Nothing else is placed and nothing else is reachable.
- **Tool surface.** Read and write within the work tree; compile, `static lint`, `syntax check`, and execution of the leaf's own cases, each through a wrapper over the build-runtime library (`tools/build_runtime.py`) that records every invocation in the leaf's `command_log.jsonl`. No shell outside the wrapper, no network except the leaf's own provider endpoint.
- **Not on the surface.** `tests.md`, the verdict evaluator (`tools/verdict_evaluator.py`, `tools/primary_evidence.py`), the gates, every `workspace/` artifact of any other attempt, `spec/`, `tools/`, `docs/`, `.git/`. The leaf can compile and run what it wrote; it cannot run the judgment on it.
- **Hand-off.** At the leaf's end the host snapshots the work tree into the existing `CodegenBundle` (for `generate.generate`) or the IR documents (for `compile.generate`), validates it under the existing contract, and runs the existing gates in its own process. The work tree itself is kept as the attempt's dialog record and is an input to nothing.
- **Repair.** A gate finding re-enters the SAME work tree and the SAME session with the finding appended; the leaf edits in place. The whole-document repair contract is retired for the producing leaves. A `restart` repair discards the tree and seeds a new one, as the cold path does today.
- **Independence of review.** A `workshop leaf` may use its harness's sub-agents internally. `generate.verify` and `compile.verify` remain pure leaves outside the work tree, launched by the host from the snapshot, sharing no session and no tree with the producer.
- **Record.** The full transcript of the agent, every tool call, and every wrapper invocation are recorded under the attempt, as the pure leaf's prompt and reply are today. Usage is recorded per turn, with the running-total trap of `TODO.md` (issue #281) resolved for the agentic transport before the first billed comparison.

### Sandbox

Two sandboxes, one per role, plus one the host owes independently of this note.

| sandbox | holds | writable | network | technology |
|---|---|---|---|---|
| `workshop leaf` | the seeded work tree; the toolchain and the provider CLI read-only; the CLI's credential home as today | the work tree only | the provider endpoint only, through a network namespace and a proxy | `bwrap` + network namespace |
| pure leaf | nothing (an empty tmpfs at the checkout path, as since issue #227) | its scratch root only | the provider endpoint only | `bwrap`, unchanged |
| `Build` / `Validate.execute` (host, deterministic) | the staged source, the binary, the run directory | the object dir, `binary/<binary_id>/bin/`, `runs/<run_id>/` | none | `bwrap` (NOT applied today: both run unconfined in the conductor's process) |

Virtualization stronger than user-namespace containment (a VM, a user-space kernel) is not required, and the rule that declines it is premise 1: the holes a VM closes beyond `bwrap` are holes against the operator's machine, and a leaf gains nothing toward its verdict from any of them. The one place that executes untrusted code on the operator's machine with no sandbox at all is the third row, and it is the host's own step. Confining it closes the Z6 accepted residual as well: a generated `checks_compute` that rewrites `raw/state_snapshots/<case_id>.json` after the runner wrote it cannot do so when the snapshot leaves the process through a pipe to the host and the run directory is not writable by the binary. That change is independent of the `workshop leaf` decision and precedes it in the migration order.

### The judging boundary under an agentic producer

Three things stay out of the producer's reach, and each is enforced by placement rather than by a rule in a prompt.

1. **Write authority over anything the verdict reads.** Held by the hand-off: the host snapshots the tree, and every later artifact is host-written.
2. **The judgment itself.** The evaluator and the full predicate run are absent from the work tree. A producer that can run the verdict iterates against it until the visible cases pass; a producer that can run only its own cases cannot. The predicates remain visible (they are the IR), as today.
3. **Past artifacts and sibling attempts.** Not bound, as today.

One defense becomes available only under this design and is adopted with it: **held-out cases.** `Validate.execute` runs, beside the IR's `test_case_set`, cases the host derives by perturbing declared inputs within the ranges `tests.md` states, and the host-evaluated primary predicates must hold on them. A producer that fit the visible cases does not certify. Which perturbations are admissible is stated per test kind in `docs/TESTS.md` by the adopting pull request; a `tests.md` that states no range yields no held-out case for that test and is not refused.

### Provider neutrality

A `workshop leaf` needs an agent loop. The CLI providers (`claude_cli`, `codex_cli`) carry one; the HTTP providers (`openai_compatible`, `anthropic_api`) carry none. The rule: the host owns a minimal tool-use loop in `tools/` with exactly the tool surface above (read, write, run-through-wrapper), driven over each provider's tool-calling API, and every provider runs a `workshop leaf` through it or through its CLI harness with the same surface. A CLI harness's additional capabilities (sub-agents, context compaction) are permitted on top of that surface and are not required by any contract. Keeping the HTTP providers on the pure shape instead is rejected, because it reintroduces a second launch shape, which Z4 removed.

## Decision Criteria

The `workshop leaf` is adopted per producing `substep`, and the first is `generate.generate`.

- **Comparison.** A billed A/B against the pure producer on two nodes: one small component (the `rk4` row of the baseline, or its equal) and `shallow_water2d_channel@0.2.5`. Both arms on the same model and effort.
- **Measured dimensions**, each reported separately and none traded for another: certified outcome (equal per-test statuses required), output tokens per certified node, wall clock per certified node, first-attempt gate pass rate, repair count, and the number of agent turns.
- **Adoption rule.** Adopt when the certified outcome is equal and the large node certifies at all (the pure arm's recorded outcome is a give-up), and when the small node's output tokens and wall clock are not worse than the pure arm's. A cost regression on the small node with a pass on the large one is adopted only with a per-node cap on agent turns that the small node stays under.
- **Decline rule.** When the large node does not certify under the `workshop leaf` either, or the small node regresses by more than the cap allows, the shape is declined and the fallback below is taken.
- **Cost targets** of `zero_base_architecture.md` §Decision Criteria stand: at most 10 min wall and 100k output tokens per certified node.

## Migration order

1. Confine `Build` and `Validate.execute` under `bwrap` with the snapshot channel out of the process. Independent of the A/B; closes the Z6 residual.
2. `workshop leaf` for `generate.generate` on `claude_cli`; the A/B above.
3. On adoption: `compile.generate`, replacing the part split of issue #526 (the leaf splits its own work); `codex_cli`; the host tool-use loop for the HTTP providers.
4. Retire the whole-document repair contract for the producing leaves; the pure template pair `pure_generate_generate*.txt` / `pure_compile_generate*.txt` becomes the seed contract text of the work tree.
5. Held-out cases at `Validate.execute`.

## Fallback

When the A/B declines the shape, the producing leaves stay pure and two changes are made within that shape: the repair reply becomes a patch against the prior document that the host applies, and `generate.generate` is produced in host-named parts as `compile.generate` is since issue #526. This keeps the one-reply model and its cost on repair, and keeps the host responsible for splitting the work.

## References

- `zero_base_architecture.md` (A2, A3, A4, A8, Z3, Z4, Z6)
- `deterministic_conductor.md` §Why, §Leaf sandboxing
- `z4_enforcement_retirement.md`
- `docs/ORCHESTRATION.md` §pure-function leaf, §repair / retry
- `docs/BWRAP_ENABLEMENT.md`
- [issue #171](https://github.com/seiya/atmofab/issues/171), [issue #227](https://github.com/seiya/atmofab/issues/227), [issue #255](https://github.com/seiya/atmofab/issues/255), [issue #510](https://github.com/seiya/atmofab/issues/510), [issue #526](https://github.com/seiya/atmofab/issues/526)
