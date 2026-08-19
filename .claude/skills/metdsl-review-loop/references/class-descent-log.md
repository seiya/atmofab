# Class-descent histories, per loop

SKILL.md's stopping conditions are read as **class descent plus a bounded remainder**. These are
the transitions each recorded loop actually went through, and what ended it. Read them for the
shape, not the counts.

Headline shape: L128 ran 9 rounds with one empty round and no Codex available; PR #53 ran 3 rounds
with none empty and Codex never clean (it found a hole on the first pass); in PR #55 round 3 both
a subagent and Codex produced new findings.

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
| the 50 decisions common to both versions (before → after the rebuild) | 20 → 28 | 20 → **11** |
| the 20 decisions the rebuild **added** | 2 | **15 (75%)** |

The existing part improved clearly and the recurrence was **localized to the additions**: the shape
of the rule was right, and the problem was the habit of writing a fix without its witness. Stopping
there would have handed over as unresolved something bounded and fixable.

## PR #67 — the census reproduces, and the only Codex clean so far

233 decisions (production 112 / test assertions 115 / doc rules 6), 111 mutants: witnessed 86 /
corpus-dependent 22 / vacuous 3 / **killed only by the token ratchet 1**. That fourth class is why
it must be separated — an abandoned mirror was hiding there (a spelling that does not increase the
token count passes straight through).

This is also the seventh loop and **the first time Codex returned clean** (native `review`, about 3
minutes, "no actionable correctness regression on the changed production paths"), which breaks the
premise that Codex always finds something. **But the same round's two subagents produced
unwitnessed mechanisms, over-refusals, and the abandoned mirror, so clean was not evidence of
convergence.** Treat it as one independent signal and do not launch a second time.
