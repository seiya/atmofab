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
proxy 1**: R15 found five defects in a measurement script the branch had COMMITTED, two of them
functional — a denylist environment that could send an unbilled probe to a real endpoint, and a
timed-out launch scored as a successful read, which would have reported the premise holding for a
run where nothing launched. (This file said "one of them functional" from before the split, while
`SKILL.md` described both incidents and called them functional defects, plural; the count is
corrected to two on both sides. Five were found in total, two of them described.) Nobody
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

## The skill split itself (2026-08-25) — a loop whose findings were all in the compression

Recorded here because this file is where a loop's history belongs, and because the loop that
produced it found ~50 defects, nearly all introduced by the change being reviewed. Four rounds,
plus round 0; no Codex (a change that ADDS checking machinery and is otherwise prose-centred is
the skill's own non-launch case, so this branch could not use Codex as a convergence signal).

**The class, round by round.** R1: content lost or distorted by compression, plus a vacuous new
check. R2: the SAME class, larger — the round-1 deletion of a "duplicate" block had taken ten
distinctive tokens out of the tree — plus a widened check that had loosened past its own
argument. R3: a CENSUS instead of another opinion round, which turned the remainder into a list
(9 weakened clauses, 8 reference-only imperatives, 1 absent rule, 7 absent evidence tokens, all
closed) — and the blank-slate reviewer independently reported no rule had left the tree. R4:
zero content findings; everything left was in the checking machinery's own witnesses, and a
disclosure round found four operator-actionable clauses the census's instrument could not see.

**What the loop is worth keeping for:**

- **The instrument that ended it was the census, and its limit was the shape it enumerated.** It
  extracted `**bold**` spans, so every rule dropped as a NON-BOLD TRAILING CLAUSE on a bold-headed
  bullet was invisible to it — four of them, found afterwards by a disclosure round reading for
  audience rather than for structure. A census bounds the remainder only within the shape it can
  see; say what that shape is when you report one.
- **Three defects were "the fix applied to one side of a pair".** The reporting-layer extraction
  made for the orphan row and not for its twin; a depth guard added to the walk and not to the
  corpus filter; a `scripts/` exemption whose argument covered helpers and whose implementation
  covered the entry script. Each was found one round after the fix it belonged to.
- **One sentence was corrected four times** ("five functional defects" → "one of them functional"
  → the count left unedited beside a corrected parenthetical → "two of them functional"). Round 1
  resolved the disagreement by adopting the reference file's number, which was the wrong side.
  Reconcile against the OLDEST source, not the nearest one.
- **Deleting duplicate prose needs the opposite polarity from writing it.** Restore by default and
  subtract only what duplication is proven for: a 6-gram check said 42 sentences had no surviving
  match, and the judgment that the instrument was crude, rather than the reading of its output,
  cost a whole round.

**The size measurement, as a historical record.** Command:
`for f in .claude/skills/*/SKILL.md; do wc -c "$f"; done`, summed.

| at | loaded-at-invocation total |
|---|---|
| `d8d48c7` (before the split) | 116,596 B |
| `e2de95a` (the split) | 66,397 B |
| `2cb9b84` (after three rounds of restoration) | 71,739 B |

The property, which does not move: **every byte added back after `e2de95a` is material a reviewer
measured as lost, and nothing pins the total** — the reachability check pins that a reference is
reachable, not that the entry point stays small. A reader who needs today's figure runs the
command.

## Census practical notes: the episode behind each (restored 2026-08-25)

These six were compressed to one line each in `SKILL.md` §"Stopping conditions" and their
evidence was carried in the block deleted as duplicate in round 1 — wrongly: the block was a
second spelling of the RULES, and these EPISODES existed nowhere else. Restored verbatim.

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

