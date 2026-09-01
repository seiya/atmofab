# Delegating verifiable review work to sonnet: the experiment log

Eight data points. SKILL.md carries the operational conclusion; this is the evidence, newest first.

## Data points 6-8 (PR #107, rounds 1, 3 and 4) — three runs, two more premise refusals

Run as the delegated mechanical-recomputation axis in three of the four rounds, in parallel with
two up-model reviewers each time, same HEAD, checklist-driven.

**Yield.** Round 1: one real finding out of one reported — a document count wrong in a commit
message and in a test docstring, which the up-model reviewers both missed while disagreeing with
each other about the number. Round 3: one real MISMATCH out of one reported (a key-order claim
asserted in a comment with nothing observing it — the shape the branch had criticised an earlier
round for). Round 4: nothing new; every number matched.

**It refused a false premise in its checklist twice more**, which is now the axis's most
reproducible behaviour rather than a one-off: in round 1 it reported that the phrase "eight
surfaces" I asked it to verify appears in none of the commits, and in round 4 that the ceiling /
headroom claim I asked about is not made by either commit under review. Both times I had written
the premise myself. **Keep telling it to report what it cannot locate rather than account for it**
— across four data points that instruction has now returned more value than any single finding.

**Where it is weak, measured.** In round 3 and again in round 4 it returned MATCHES on
code-path claims that were false, having traced the path rather than driven it: "every refusal
invalidates the copy" and "callee raises are covered a fortiori" were both confirmed by reading
and both refuted by the reviewers who ran the refusals. That is not a sonnet-specific failure —
it is what a reading-based verdict is worth on any model — but the mechanical axis is the one most
likely to produce one, because its checklist asks for verdicts on many claims cheaply. SKILL.md
now carries the general rule ("verify a reviewer's POSITIVE claims by asking what it EXECUTED");
the axis-specific version is: **for a claim about control flow, ask the checklist for the command,
not the verdict.**

**Overlap.** Near zero with the up-model axes in all three rounds — it found what they did not and
missed what they found, which is row 3 of the table below (an independent eye), not row 1. Cost
was again roughly comparable per run, so the standing conclusion holds: use it to free a slot, not
because it is cheap.

## Data point 5 (PR #88 / TODO:269, round 1) — the axis paid, and it pushed back on a false premise

**Run as the delegated mechanical-recomputation axis** (the conclusion from data point 4), in
parallel with two opus reviewers on other axes. 133k tok / 11 min, 50 tool uses.

| | sonnet (mechanical) | opus (correctness+doc-truth) |
|---|---|---|
| real findings | 1 | 4 |
| of which unique | 0 | 3 |
| reported FALSE | 0 | 0 |

**The one finding was the ruff count** (ledger said 3/1/7/2, actual 1/0/6/1), found independently
by the opus reviewer as well — so on this branch the axis was **replaceable, not additive**. It
also independently reproduced the mutation-check ledger and the two `git worktree` location
artifacts, which is the cheap half of "verify the author's numbers" and freed the opus slots.

**The result worth keeping is a different one: it refused a false premise I had put in its
prompt.** I told it "the diff claims there are exactly 8 such readers"; that number came from my
planning document, not the diff, which says 6. Rather than confabulating an 8-reader census it
ran its own, reported 6, and wrote that it **could not find the claim I attributed to the diff**.
That is the behaviour a mechanical axis needs most, and it is worth prompting for explicitly:
**tell it to report claims it cannot locate rather than accounting for them.**

Operationally: unchanged. Keep it as the mechanical axis, expect overlap rather than novelty, and
do not read overlap as "no point" — the point is the freed up-model slot.

## Data point 4 (PR #72) — the confound resolved

**Both models got the same checklist.** Result: **sonnet ⊂ opus, with real misses.**

| | sonnet | opus |
|---|---|---|
| reported FALSE | **0** | **2** (prose claiming a wider scope than it has) |
| structural defects | 0 | 1 (one predicate implemented twice) |
| cost | 159k tok / 20 min | 159k tok / 23 min |

**The decisive part is that sonnet returned the same checklist items opus flagged, explicitly
marked "clean"** — it looked and passed them, rather than missing them. What it dropped was
**judgment, not arithmetic**: re-measuring numbers, back-checking "X pins this", counting call
sites were all accurate, while "is the **scope** of this claim right" (does it hold for the pure
leaf too; is this docstring speaking about all leaves) was not. The lesson of this round is that
**checklists contain judgment items too**.

Operationally: follow the "sonnet ⊂ opus (with misses)" row — **move the mechanical-recomputation
axis permanently to sonnet, keep scope and consistency judgments on the up-model**. Costs were
about equal, so "it is cheap, so run more" does not hold; use it only to free a slot.

## Data point 3 (PR #68, round 1)

Same direction, same confound. sonnet's checklist agent found one real defect ("the moved
definitions are twelve" was actually 13), and **opus's correctness reviewer found the same one
independently** = zero independent discoveries. 105k tokens / 7.5 min against opus's 152k and 178k
/ 27 and 29 min. **It correctly recomputed 11 of the 12 checklist items, and did the
`--collect-only` before/after comparison and the delete-and-restore of a witness test on its own.**
Findings needing judgment (over-refusal, mechanisms with no witness, description drifting from
implementation) came only from opus in all three data points.

## Data point 2 (PR #67, round 1)

Same conclusion, same confound. sonnet's checklist agent found one real defect (the ledger's
`74 -> 51` measured 54), **which opus also found independently** = zero independent discoveries.
68k tokens / 11 min against opus's 157-225k / 26-59 min. Re-measuring numbers and back-checking "X
pins this" were **exhaustive and accurate**; every judgment-requiring finding (mechanisms with no
witness, over-refusal, an abandoned mirror) came only from opus.

## Data point 1 (PR #66, round 1)

**sonnet ⊂ opus, zero independent discoveries, confounded.** It found the recomputation class (the
ledger's per-area figure said 801, actually 799) and an environment-dependent suite count. **Not one
judgment-requiring finding** — "following the doc's procedure produces a backend that does not
work" and "the ledger contradicts the canonical doc" both came only from opus. Its back-checking
work (mutating 9 tests individually and confirming **each failed for the reason it claims**) was
exhaustive and useful.

**The confound**: sonnet got a checklist and opus free-form prose, so neither substitutability nor
"a different axis" follows — I broke my own "do not confound the comparison" rule. Data points 2
and 3 broke it as well; PR #72 finally fixed it.

## The observation that started it (L174, ~20 findings)

A substantial share of the findings were decided not by depth but by **whether someone dutifully
re-measured**: "44 of 48" was actually 38 (just re-checking a subtraction), byte counts and suite
counts gone stale, "recorded in TODO" with no record present, three call sites making the same
decision left unscanned. **That layer does not need an up-model.**

## Method: how to keep measuring (moved from SKILL.md, 2026-08-25)

**Numbers to collect**: real findings / total findings, elapsed time, and the **overlap count**
with the up-model. One up-model plus one sonnet against the same HEAD in parallel gets this for
free.

**How to read overlap — high overlap does not mean "no point adding", it means "replaceable".**
Look only at the adding axis and forget the removing axis and you misread it (the first version of
this section did exactly that, and was wrong).

| Result | Meaning | What to do |
|---|---|---|
| sonnet ⊇ opus | this axis is fine on the cheap model | **move the axis to sonnet permanently**; spend the freed up-model slot on deep layers |
| sonnet ⊂ opus (with misses) | partial substitute | look at what it dropped: mechanical → reject; judgment-requiring → the axis is cut wrong |
| sonnet ∖ opus ≠ ∅ | it works as an independent eye | keep both |
| mostly false positives | triage costs more than it yields | reject |

**Do not confound the comparison.** To measure substitutability (row 1), **give both the same
checklist**. To measure whether it works as a different axis, use different prompts. A comparison
with a checklist on one side and free-form on the other yields neither conclusion.

**This conclusion holds only inside the axis delegated.** "sonnet matched opus at recomputing
numbers" does not give "it matches on parser semantics". Measure per axis.

**The reverse is not an experiment.** When the implementation was sonnet or fable, reviewing with
opus is plain quality escalation — do it without hesitating. Finding a defect is often harder than
writing the code (L174's offset-translation layer: 10 lines to write, a purpose-built mutant to
find).
