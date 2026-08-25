# Class-descent histories, per loop

SKILL.md's stopping conditions are read as **class descent plus a bounded remainder**. These are
the transitions each recorded loop actually went through, and what ended it. Read them for the
shape, not the counts.

Headline shape: L128 ran 9 rounds with one empty round and no usable Codex signal (four launches, three lost
to the content filter — `codex-episodes.md` has them); PR #53 ran 3 rounds
with none empty and Codex never clean (it found a hole on the first pass); in PR #55 round 3 both
a subagent and Codex produced new findings.

## issue #71 — the proxies read as held from R11 and four more rounds found real defects

15 rounds, the longest recorded. R1-R5 "witnesses weaker than their names, and four of my own
'measurements' of a vendor tool were measurements of something else — Python's `glob` twice and
bare `ripgrep` once, each written down as the tool" → R8 "a brace alternation walked out of the
manifest, found by a reviewer" → R11 **"the rounds are reliable about code and unreliable about
their own record"** → R12 narrowing (a net 164 lines out of `tools/hooks/cli.py`) → R13-R15 "no
defect in the enforcement code, and a false or missing statement every round".

Evidence for two of SKILL.md's stopping conditions — that "only prose remains" is a claim about
severity, and that a disclosure round is worth running before stopping. **And for the limit of
proxy 1**: R15 found five defects in a measurement script the branch had COMMITTED, one of them
functional (a denylist environment that could send an unbilled probe to a real endpoint). Nobody
had been asked to review it, because the briefs named the enforcement code — so "a reviewer with
no exclusions returns zero functional defects" was true of the files anyone looked at. A
committed instrument is review surface.

Defects introduced by the fixes themselves recurred in most rounds — four in the final round
alone, of which the one recorded nowhere else is a correction to the leaf-read contract that
blew its byte ceiling: the fix was right and its LENGTH was the defect.

## issue #63 — the first time both proxies held at once (R4)

R1 "mechanism holes in the original design (credential exposure / cross-leaf transcript reads) plus
tests spinning in neutral" → R2 "a mechanism hole in the original design (cross-leaf prompt
injection) plus over-refusal introduced by R1's fix" → R3 "one regression from R2's fix, plus
prose" (an independent sweep killing 27/27) → **R4 "zero functional defects; four unwitnessed
decisions and prose"**. R4's remainder was enumerable, so it was closed out and stopped.

**The count barely fell from R3 to R4** — what fell was the class: a hole in the original design →
a hole in my fix → a hole in the witnesses. This is also the example showing the two proxies
("a blank-slate reviewer returns zero functional defects" and "an independent mutation sweep is
fully killed") can be applied on the spot rather than in hindsight.

Codex on this loop is the counter-example to "clean means converged": both completed runs returned
real defects and both were subagent blind spots — a warm-resume home mix-up (also found
independently by a blank-slate subagent) and **"fixed one of two layers and verified only that
one"** (I fixed the block path and never looked at auto-approve). The budget rule of "do not spend
launch 1 on the coarse layer" is what made rounds 2-3 the right place.

## L128 — the question could not be answered at that level

"Holes in the scope mechanism itself (infinitely many)" → redesign → "missing declaration spellings
(bounded)" → pin the enumeration one element at a time. The cut-off point is where **the class
dropped and the remainder was demonstrably bounded**.

The redesign came from asking a **weaker question that can be answered**: "is this name a constant
here", attempted by regex, was broken 16 ways; "is there no other declaration anywhere in the file"
closed it. If the simple and the complex version produce identical measured diffs, the complexity
bought nothing — take that diff first.

## PR #53 — the last round changed one behaviour

"Holes in the mechanism itself; a deleted defense is reachable" → "missed spellings and contexts
(construct names / tabs / nested interface / `type is`)" → **"one leak of internal state and one
missing pin"**. Only one finding in the final round changed behaviour; the rest were prose and
tests. Disclosure: 5 fail-opens and 1 false positive came from my own fixes.

## PR #55 — the class did not drop, only the shape changed

"The contract itself, with three readers disagreeing" → "the pin broken **in a different shape every
time**" (three names → substring → directory branch). **The class did not descend**, so boundedness
was never demonstrated. Stopping was not convergence: it was the finding that **the way to close it
lies outside this branch** (move the contract's definition into one place). In that case **name what
would make it bounded and hand it over** — the three remaining escape routes were listed with the
reason for not chasing them.

## PR #57 — severity rose in round 3

"One field with six readers and four kinds of fallback" → "missed readers and an unpinned mirror" →
**"the fix ran after the reader it was supposed to protect" (severity rose)** → "a vanished witness
and an isomorph left on the neighbouring branch of the same function". Rounds 1-3 were each inside
the previous round's fix, and round 3 was an **outright regression** (not a prose error: it shipped
a change that broke the audit trail). Boundedness could be claimed in round 4, where both proxies
held.

**"Findings keep appearing inside the previous round's fix" is neither a denial nor a confirmation
of convergence — read it by whether severity rose or fell.**

## PR #68 — counts flat, the breakable range narrowing (6 rounds, a move PR)

"A false ledger + over-refusal + 5 unwitnessed branches" → "**a fail-open in my own new witness** +
6 witnesses + 3 defects in a pattern-matching test" → "3 unwitnessed holes (2 again in my own
witnesses)" → "the same (1) + 4 descriptions" → "**the generalized scan refuses the next migration
area** + a bypass" → "relative imports + **the redesign has no witness**".

**The counts did not fall, but the range that could be broken narrowed every round** (a whole edge
coming back → one spelling → one line). What allowed stopping was neither a count nor "zero new":
it was **the census turning the remainder into a list, and being able to build a witness for the
instrument on a synthetic tree**. Judge by "can I enumerate what is left to break", not by counting.

Zero defects were found in the moved code itself across all six rounds; everything was in the fixes
and the prose.

## PR #58 — stopped by decision, not convergence

"The instrument's question cannot be answered (path spellings are infinite)" → **rebuild the
instrument** → "the detection surface is a smaller version of the same error" → "where `reason` is
read from is entirely unpinned" → "2 routes + **an overclaim of any route**" → "the predicate's
scope is wrong + 4 spellings" → "the same, breakable by nesting + 2 spellings" → "4 spellings + 2
false positives".

**Rounds 5-7 showed no class descent and it was stopped by decision.** What decided it was not the
count but that **every finding in those three rounds was a construct occurring zero times in the
real corpus** (the corpus had 23 skips in 2 spellings). Handing over, the instrument was named:
**cross-check the skips the runner reports at runtime against the ledger** — no Python semantics
needed, and all seven rounds' escape routes close at once.

The sweep recommended stopping at round 4 (193 mutants) and round 6 (170 mutants) while the same
rounds' blank-slate reviewer returned live routes with end-to-end reproductions. That is the origin
of "for changes that add machinery, a sweep is not grounds for stopping".

## PR #66 — the witness census, and inherited vs added decisions

R3-R5 had only the feeling of "the same class keeps recurring"; running the census in R6 settled it
on the spot. 70 decisions were classified, separating **5 structurally unobservable** (an assertion
cannot observe its own weakening — loosen `assertEqual` to containment and the loosened one is what
runs; external mutation testing's territory) from **21 simply unwritten, 16 of them with inputs
already constructed**. Finishing the latter was the stopping point.

R6 also matched, on its face, "you rebuilt the instrument and the second behaved the same → do not
build a third". Measured, it did not:

| | witnessed | unwitnessed |
|---|---|---|
| the decisions common to both versions (before → after the rebuild) | 20 → 28 | 20 → **11** |
| the decisions the rebuild **added** | 2 | **15** |

(The recorded totals — 50 common, 20 added — do not match those rows, which sum to 40/39 and 17.
The rows are what the census produced; the totals came from the round's prose and one of the two
was miscounted, as with the 111-vs-112 above.)

The existing part improved clearly and the recurrence was **localized to the additions**: the shape
of the rule was right, and the problem was the habit of writing a fix without its witness. Stopping
there would have handed over as unresolved something bounded and fixable.

## PR #67 — the census reproduces, and the only Codex clean so far

233 decisions (production 112 / test assertions 115 / doc rules 6): witnessed 86 /
corpus-dependent 22 / vacuous 3 / **killed only by the token ratchet 1**. The record also says
111 mutants, which does not match that sum of 112 — one of the two was miscounted at the time,
and the classification is the half worth carrying. That fourth class is why
it must be separated — an abandoned mirror was hiding there (a spelling that does not increase the
token count passes straight through).

This is also the loop in which **Codex returned clean for the first time** (native `review`, about 3
minutes, "no actionable correctness regression on the changed production paths"), which breaks the
premise that Codex always finds something. **But the same round's two subagents produced
unwitnessed mechanisms, over-refusals, and the abandoned mirror, so clean was not evidence of
convergence.** Treat it as one independent signal and do not launch a second time.

## issue #40 / PR #41 — a distinct convergence pattern: only unmeasured prose is left

A 14-site print-statement replacement ran 14 subagent review rounds. **Code defects were gone by
round 4.** Rounds 5-14 found the same class every time, in a different sentence: a commit message,
code comment, or doc line **asserting something about behaviour the PR never touched and no test
covers.** The same two sentences were wrong in four DIFFERENT ways across four consecutive rounds
(what happens to a dead reader before init, what category a specific failure terminalizes as, how
many events elapse before the next flush) — a moving target, not one typo fixed four times.

This is a class distinct from "findings inside the previous round's fix" (PR #57, #68, #70): here
the class does not descend at all across rounds, because deleting a false claim about existing
behaviour cannot itself introduce a new defect the way editing production code can. **The stopping
rule this loop supplies**: once every remaining finding is "this prose asserts something
unmeasured about code the PR does not touch," the fix is to DELETE the claim rather than to write
a test proving it — a PR is not obligated to characterize behaviour it left alone, and a doc that
tries anyway is one guess away from being the next round's finding.

## Stopping-condition detail moved from SKILL.md (2026-08-25)

`SKILL.md` §"Stopping conditions" keeps the conditions, the proxies and the census instruction.
This section is the evidence and the practical notes it used to carry inline.

for two consecutive rounds** (a disclosure round, which runs no security agent, is skipped in
that count rather than breaking it). It has **never been achieved in any recorded loop** — the
histories in `references/class-descent-log.md`, plus PR #51. **Run assuming you will not reach
it** — do not add rounds waiting for it.

**Do not make "Codex is clean" a stopping condition.** As a condition it becomes **a motive to
relaunch a Codex you have no budget for**. Use Codex as one independent pass and finish once you
have classified the result. Clean did come back once (PR #67), and **the same
round's two subagents produced unwitnessed mechanisms, over-refusals and an abandoned mirror — so
clean was not evidence of convergence**. TODO:269 is the second such data point: Codex returned
clean in round 2, and **round 3 found a mechanism with no behavioural witness at all**. Issue #63 is the opposite data point: both completed runs
returned real defects, both in subagent blind spots.

**Practical proxies for "the remainder is bounded"** (so you can say it on the spot, not in
hindsight). In PR #57 defects kept appearing inside the previous round's fix and **round 3 (severity
rose) was indistinguishable in the moment from round 4 (bounded)**. What actually marked the
boundary was these two holding together:

- **a reviewer with no exclusions returns zero functional defects** — only prose and consistency
  findings remain
- **an independent mutation sweep is fully killed** (one the reviewer built, not one you ran)

When both hold, the remainder has fallen to "an enumerable, finite set of descriptive fixes". One
alone is not enough.

**"Only prose remains" is a claim about SEVERITY, and it is wrong whenever the prose is executed
by someone.** Issue #71 is the counterexample: both proxies above read as held from round 11 — the second
of them wrongly, see below — and rounds
12 through 15 each still carried statements that were false, including a refusal message a leaf
follows and a remedy an operator follows. Two of round 15's "prose" findings had real
consequence: one made a leaf report absence after obeying half a remedy, the other told an
operator to permanently subtract a tool from a required set to paper over a one-line
configuration bug.

**Be careful what you conclude from "the code was clean".** On that branch it was not quite
true, and the way it was untrue is the more useful lesson: round 15 found five defects in a
measurement SCRIPT the branch had committed — an environment built as a denylist, so one
variable would have sent an unbilled probe to a real endpoint, and a timed-out launch scored as
a successful read, so a run where nothing launched would have reported its premise holding.
Those are functional defects. They went unfound for two rounds because **the reviewers were
told to look at the enforcement code, and a committed instrument is not obviously that**. When
a branch adds a script, a harness or a fixture generator to the repository, name it as review
surface explicitly; "zero functional defects" otherwise means "zero in the files anyone
looked at".

So before calling the remainder descriptive, **classify each remaining finding by audience and
consequence, not by whether it is code**:

- **Text a leaf or an operator ACTS ON is behaviour delivered as prose** — refusal messages,
  remedies, the leaf-read contract, the runbook step for a failure mode. Treat a defect there
  at the severity of the action it causes
- **Text a maintainer reads to decide** — a residue entry, a justification comment, a measured
  number — is descriptive, and belongs in the bounded remainder
- The tell that you are in the first category: the sentence contains an imperative, or names a
  condition under which something is refused

**The move that finds them: spend one round on the disclosure axis alone**, with no functional
brief. Note what this implies about the standing arrangement: doc-truth is already bundled into
the correctness axis of every round (see "Running a round"), and bundled it loses to whatever
functional question shares the brief. Give it a round of its own, with two briefs: "verify
every claim in the commit messages at HEAD" and "read it as the next maintainer: what would
mislead you, can the deletion's measurement be re-taken from what is written, what does a LEAF
see, what does an OPERATOR see, would you merge". On issue #71 that round returned two
real-consequence items and five documents stating the rule's own trigger wrongly — one of them
a document every leaf reads — in a branch whose ENFORCEMENT CODE had been clean for four
rounds. **Run it before stopping, not as an extra round after deciding to stop** — and if it returns items in the first category
above, the record has not converged even though the enforcement code has.

**But the two are not equal in standing. Use them by change type.**

- **changes that fix existing machinery** (closing a fail-open, tightening a gate): both apply. The
  sweep measures "is what I fixed pinned", which is exactly what is being asked
- **changes that add checking machinery** (a new guard, validator, meta-test): **the sweep is not
  grounds for stopping.** It answers "is what I built pinned", not "is what I built enough" —
  **it can only mutate mechanisms that exist, so a missing mechanism is structurally invisible**. In
  PR #58 the sweep recommended stopping at round 4 (193 mutants) and round 6 (170 mutants), while
  **the same rounds' blank-slate reviewer returned live routes with end-to-end reproductions**, and
  every finding from round 3 on was a missing mechanism. Here the proxy becomes **a blank-slate
  review with zero functional defects for two consecutive rounds**. Still run the sweep, but read it
  as a list of "unpinned but correct behaviour"

**For a change that adds checking machinery, run a witness census once**, rather than only the
negative proxies. In PR #66, rounds R3-R5 had only the feeling of "the same class keeps recurring",
and running this in R6 settled it on the spot. Instruct a dedicated reviewer:

> Enumerate **every decision** in the checking machinery (predicates, constants, each table entry,
> each regex branch, globs, exclusions, file-format assumptions, every assertion added), and
> classify each **by execution** as **witnessed** (a constructed input witnesses it) /
> **corpus-dependent** (green only because today's tree happens to contain no violation) /
> **vacuous** (already observing nothing). Classification by reading is not allowed. For the
> unwitnessed ones, construct a violating input yourself and report whether the suite notices.

What comes back turns "the remainder is bounded" from a feeling into a list, and the instrument
reproduces: PR #66 classified 70 decisions, PR #67 233 decisions with 111 mutants. Practical
notes (the numbers and
transitions are in `references/class-descent-log.md`):

- **Have "killed only by the token ratchet" separated out as a fourth class.** Folded into
  "killed", it counts as a witness something `docs/BACKEND_BOUNDARY.md` §Enforcement calls a bound
  on growth rather than a detector — in PR #67 an abandoned mirror hid exactly there
- **A vacuous finding may be closed by marking, not deleting.** PR #67 proved two calls unreachable —
  `_require_axis` inside `provides`, and `LANGUAGES`'s `implemented_backend_ids`, which the
  following filter subsumes — and the right response was a comment saying "a marker of intent,
  not a live guard". The problem
  is not the redundant call; it is that it **reads as a live guard**
- **The census makes you doubt your own instrument too** — the verification test built from PR #67's
  census was wrong twice while being built. Aim "does it wrongly refuse legitimate work" at the
  instrument as well
- **A corpus measurement does not prove vacuity.** In PR #68 a guard labelled unreachable from
  "0 empty atoms / 151,633 logical lines / 876 files" did fire, because the scanner and the normalizer
  **disagreed on the definition of blank** (gfortran's space/tab/FF vs Python's wider `\s`), so a
  line of only U+00A0 survived. Vacuous may be claimed **only when unreachability is shown by
  construction**; "not in today's tree" is corpus-dependent — and because the label invites
  deletion, a wrong vacuous is worse than no label
- **A census conclusion rots in one round; re-run it the round after you consume it** (PR #68's
  "zero ratchet-only decisions" was falsified the next round). Record conclusions that survive
  re-measurement, not the numeric breakdown — numbers always rot (the suite count was wrong four
  times on that PR alone)
- **When you replace an enumeration with a computation, witness the computation on a synthetic
  tree.** In PR #68 the scan target became a reachability closure, and deleting the transitive
  expansion entirely stayed all green: a test against real data confirms "today's graph happens to
  be like this" and never observes the algorithm (cf.
  `test_backend_boundary.py::ScannedSetTests`)

**Before concluding "the shape of the rule is wrong" from recurrence, measure inherited decisions
separately from decisions the last fix added.** The sign below ("you rebuilt the instrument and the
second behaved the same → do not build a third") **can produce a wrong judgment if followed
literally**. PR #66 at the start of R6 matched that condition on its face; measured, the decisions
common to both versions had improved (20 → 28 witnessed) while **15 of the decisions the rebuild added were
unwitnessed** — the recurrence was localized to the additions, the shape of the rule was
right, and the problem was the habit of writing a fix without its witness. Stopping there would
have handed over as unresolved something bounded and fixable. **The test**: inherited decisions got
worse → a problem of shape (stop and hand over); concentrated in what the last fix added → a
problem of habit (write the witnesses and it closes). The full table is in
`references/class-descent-log.md`.

**Another proxy: does the finding exist in the real corpus?** This is cheap to measure and saves
several rounds. **If every finding in a round is a construct that occurs zero times in the real
corpus, what remains is not implementation but a written scope declaration.** PR #58's guard read
skip declarations statically, and the corpus had 23 skips in 2 spellings; from round 4 on every
finding was a future form an adversarial reviewer wrote to break it, present nowhere in the tree —
and I dutifully kept closing them for three rounds. The right response was **declaring the scope**
("regression prevention for ordinary spelling, not enforcement against circumvention") and stopping.
Each round, measure the count per finding class with one `grep`.

**Look at `ListAgents` before judging a round finished** — as part of the stopping decision, not
only as orphan hygiene. I once issued a "merge recommended" without checking which reviewers were
still running, and the report that arrived afterwards carried a real defect.

In every case, **finish with one convergence judgment**. The question is not "any new findings" but
**"is any finding left that would change code, tests, or a description a reader relies on?"** If
only preferences remain, have it recommend stopping explicitly.

When you stop short, **state the condition you did not meet** and hand it to the user. Do not say
"converged". **Put the class transitions and the defects your own fixes introduced into the PR body
as a disclosure** (PR #53: 5 fail-opens and 1 false positive came from my own fixes).

