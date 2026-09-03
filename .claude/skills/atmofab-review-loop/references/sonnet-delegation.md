# Delegating verifiable review work to sonnet: the experiment log

Twelve runs narrated here, newest first. SKILL.md counts THIRTEEN, and the extra one is not a
miscount: PR #116's run is cited there (it reported that a ten-item mutant list a commit message
referenced was recorded nowhere it could find) and was never written up in this file. Numbering
skips 10 for that reason. SKILL.md carries the operational conclusion; this is the evidence.

## Data points 11-13 (issue #149 / PR #151, rounds 2 and 3) — three runs, and the first time the axis was load-bearing

**The circumstance is the point.** Five consecutive up-model launches died to HTTP 500 / 529
before their first tool call (`references/round-conduct.md`), so for two rounds sonnet was not an
added axis in parallel with opus — it was the only reviewer that ran. That makes these three runs
evidence about a question the previous nine could not answer: what does the delegation cover when
it is carrying the round rather than supplementing it?

**11. Census + doc-truth (round 2).** Re-measured every numeric claim in four commit messages and
the docstrings and matched all of them (74/51, 1303/378, 5685/2949, 31 configurations, the ruff
histogram at both revisions, 3031/1602 for a stub measurement in a pristine base worktree).
Reproduced six of twelve stated controls, deliberately including both a RED claim and a green one,
all six matching. Verified both exemption reasons in the new machinery by mutation. Established
that a `sys.settrace`-based test measures under `pytest` and under `python3 -m unittest` and leaks
no tracer. **0 findings, and it named the six controls and the several census rows it did not
run** rather than accounting for them — the instruction that keeps paying.
Its one substantive contribution was a REFUSAL, and it is data point five of the same kind:
handed a checklist item asserting a sweep was vacuous, it mutated the SUBJECT instead of the test
and reported RED, contradicting the up-model security axis that had reported the opposite
(`references/mutation-testing.md`).

**12. Attack checklist (round 3, blank-slate).** The security-bypass axis, converted from an
open-ended brief into seventeen NAMED mutations across three mechanisms plus five over-refusal
probes, each with a stated expected verdict. Executed all of them; every severity-injection
attempt in the first three parts was correctly RED, it
enumerated one helper's field coverage against the renderer's call sites and reported no gap, and
it correctly classified two results as over-refusals rather than holes plus one as borderline. It
also checked, unprompted, whether a regex matched a probe string it had been told to use — and
reported that it did not, which changed why one row was red. **0 holes, 2 over-refusals, 0 false
positives.**
**What this run does NOT establish**: the items were the author's. A checklist measures
the author's imagination with someone else's hands, and that is the whole of the difference
between this and the axis it replaced.

**13. Disclosure (round 3).** Two jobs — verify every claim in the commit messages at HEAD, and
read the branch as the next maintainer. 602 s, 61 tool calls (the other two runs' figures were not
captured). **1 finding, real, found by no other axis**: a sentence in a fix commit's message, and
its twin in a docstring, spliced two configurations' measurements — "`generate.verify` claimed 42
lines of which 41 rendered from nothing, one of them the severity-bearing line". It rebuilt both
configurations at the parent commit and showed 42/41 belongs to `generate.generate`, while the
line is `generate.verify`'s at 36/35, so no single configuration has both properties. It then
answered the deletion question properly — enumerated the three assertions the branch removed,
named each one's successor, red-then-red tested the pairing that mattered, and **reported the one
residual it could not clear** (a defect shape the removed textual-diff check might have caught
that the membership check replacing it does not).

**Reading.** The delegation held on all three, including the disclosure brief, which is the least
mechanical thing it has been given and which produced the round's only defect. What did NOT get
tested is the line SKILL.md actually draws: the open-ended security brief was converted to a
checklist rather than delegated as-is, so **"sonnet carried the security axis" is not what
happened and must not be read out of this**. What happened is that three checklist-shaped axes
carried a round, and the one finding they produced was a false record — the same descriptive class
every earlier data point has returned.

## Data point 9 (issue #142 / PR #144, round 1) — three findings, all real, one overlap

**Run as the delegated mechanical-recomputation axis**, in parallel with two opus axes on the same
HEAD, with an explicit checklist (re-measure every number in the diff; back-check every "recorded
in X" / "pinned by Y" claim by mutating the CODE the test is about; a failure-class -&gt; test
correspondence table; prose-vs-implementation contradictions; count the call sites making the same
decision). 426 s, 70 tool calls.

**3 findings, 3 real, 0 false positives. Overlap with the up-model axes: 1 of 3.**

- A `TODO.md` sentence named `_validate_pure_launch_request`, a function that does not exist
  anywhere in the repository (the real name has a `_payload` suffix). **Found by no other axis** —
  and it is the fifth time this delegation has returned "the claim points at nothing I can locate",
  which remains the single most reproducible thing about it. The instruction that produces it is
  "report claims you cannot locate rather than accounting for them".
- A comment claiming three code sites are unreachable "on their `leaf_returncode=1`": true
  conclusion, but three different guards fire and only one is the one named. **Also found by the
  opus correctness axis** — the one overlap.
- A docstring claiming a new test was "the one test that drives the real handler": three other
  tests in the same file do too. **Found by no other axis.**

**Reading**: sonnet ∖ opus ≠ ∅ (two of three were unique), so it kept working as an independent
eye rather than as a substitute, and every finding was in the descriptive class — a name, an
attribution, an overclaim. Nothing it found required a hypothesis-mutate-run cycle, which is the
line SKILL.md draws. The axis cost one slot and paid for it; the conclusion is unchanged.

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
