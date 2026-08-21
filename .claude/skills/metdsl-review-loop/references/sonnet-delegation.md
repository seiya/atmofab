# Delegating verifiable review work to sonnet: the experiment log

Five data points. SKILL.md carries the operational conclusion; this is the evidence, newest first.

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
