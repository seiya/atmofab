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

## PR #98 — the weaker question, applied mid-loop, and the class that never descended

The second recorded instance of L128's move, and the first where the redesign happened INSIDE a
running loop rather than after it.

`TODO.md` 378(d) asked "which operand does this `cp` write?" — extractable only by re-implementing
bash's word splitting plus each tool's getopt grammar. Rounds 1, 2 and 3 each found the same defect
one function further on: a value read off one copy of the command while a decision was made on
another (`shlex`-token segmentation vs. the read side's string split; operands read from the raw
command while segmentation ran on a sanitized one; a substitution marked at its first byte, with
its spans located on the copy where they had been erased). Every fix regenerated the family, and in
rounds 1 and 2 in both directions at once — `cp src dst 2>/dev/null` refused naming the path `2`,
`cd x; cp a <managed path>` extracting nothing at all. Round 3's were over-refusal only.

**The trigger fired at THREE rounds, not five.** SKILL.md's count for "the shape of the rule is
wrong" is five; the sibling sign — "the same mechanism keeps being broken for three rounds or more
→ change to a weaker question" — is the one that was right here, and it is the one to reach for
first when the defects are all one shape rather than a spread.

The weaker question: **stop naming the destination, recognise the command.** "Is the head of this
fragment a command that writes a file?" is one lookup. It closed the family structurally — every
spelling that had defeated the parser is caught, because none of them has to be modelled to read a
head — and it flipped the failure direction from open to closed.

**Two things this episode shows that L128 does not.**

- **The redesign was put to the USER, not taken.** SKILL.md's rule for a fix that changes the shape
  of the rule is split-or-ask; the options offered were split the branch / continue / redesign in
  place, with the measured basis (every finding traced to the argv grammar or to a regex widening
  done to serve it). **The basis as put to the user overstated one half**: it said the redirect side
  had produced no finding since round 1, and round 2 had two (`2>&-` reported as a write to `-`, and
  the `startswith("&")` guard deleted for dropping a quoted `> "&1"`) — both arising from a regex
  widening done for the argv view, which is the qualification that belonged in the sentence. The
  user chose redesign. Taking that decision silently would have been the same class of error as the loop's own
  defects.
- **A weaker question is not a smaller review surface.** The new mechanism got rounds 4 and 5, and
  each found serious defects IN IT (severities are the reviewers' and mine at the time; the commits
  record the defects, not a severity field) — a wrapper hop broken for six of eight wrappers by their own
  canonical invocations, then `[[ … ]]` matched outside command position blanking a real redirect
  (a fail-open REGRESSION against `origin/main`, introduced by round 4's own fix). **The class
  never descended across all five rounds.** The loop ended at the cap, disclosed as not converged.

**What the remainder looked like at the cap**, and why it was disclosed rather than fixed: the
widest gap was a writer inside a script FILE handed to any interpreter (`bash x.sh`,
`python3 x.py`), which no guard covers — and which a SIBLING guard's remedy text actively steers a
refused leaf toward, over an allow-list entry that permits it. That is worth reading twice: the
review found not a hole in the new rule but a hole the surrounding system's own instructions point
at. Two reviewers' corpus differentials (283 real leaf commands, 51,425 operator commands, ~4,000
generated redirect spellings) found no lost target and no refusal of real leaf work versus
`origin/main`, which is what bounded the remainder well enough to stop.

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

## PR #100 — four rounds, no descent, closed by changing the question twice

A documentation change (land a sizing rule out of a gitignored operator config) that grew a
committed instrument and its tests. Twelve subagent reviews over four rounds, no Codex pass (a
prose-centred diff, per SKILL.md's "cases where not launching is better").

**The class never descended.** Every round produced FALSE EVIDENCE findings, and in every round the
worst of them sat inside the previous round's fix:

| Round | What the previous fix had introduced |
|---|---|
| 1 | a hand-transcribed reasoning value that does not exist in the run it cites |
| 2 | a boldface provenance claim false for three figure groups, withdrawn by its own paragraph 4 000 characters later; and "elapsed cannot be re-taken", which was false and suppressed a re-takeable measurement |
| 3 | the rule exported to a second provider's sample it was never measured on — wrong field name, the censoring rule inverted, a warning about an unreachable code path. Reverted whole |
| 4 | a requirement inferred from one closure, falsified by the next day's closure in the same workspace; and the instrument's own vocabulary written into the document as the system's classification |

**What ended it was not a round, it was two shape changes**, both in round 4 and both instances of
"change to a weaker question that can be answered":

- the document stopped stating an inferred requirement and started stating the largest COMPLETED
  draw across a named population, as a bound that moves;
- the sample config stopped summarising a spread of rates in prose — a sentence that had been wrong
  in three consecutive rounds, each version written to fix the last — and stated the figures
  instead.

**Two readings for the next loop.** First, the recurrence signal fired correctly and I was slow to
act on it: SKILL.md says three rounds of the same mechanism means the question is wrong, and this
loop ran four. Second, the strongest finding of the branch came from the blank-slate reviewer the
budget mandates from round four, on a branch where rounds 1-3 had all inherited the same evidence
base — which is an argument for spending one no-history slot EARLIER than round four when the
evidence is a fixed artifact everyone is handed.

Stopped at the budget with the branch merged and the conditions disclosed in the PR body: class
descent not achieved, blank-slate zero-functional-defects not achieved twice. The instrument's
census is explicitly not closed — an independent census found 24 of 40 mutants surviving in round
4, and the author's own 12-mutant sweep afterwards is a statement about those 12.

## PR #146 / issue #143 — the first loop to run to the cap, and what the cap bought

A rubric for choosing a `verify` verdict's `issue_severity`, defined once in a phase document and
injected verbatim into the pure reviewer's prompt. Round 0 plus **five** rounds — the cap — with
two subagents per round, no Codex (adds checking machinery AND a prose-centred diff: two of the
three "better not to launch" cases). Ten `PURE_PROMPT_CONTRACT_VERSION` bumps' worth of leaf-input
change ended at `pure-30`.

**The class did not descend, and the shape of what did not descend is the point.** Round 1 found
the DELIVERABLE wrong — the rubric's own drop clause was strictly wider than the two existing
statements of the same rule, re-creating issue #142's failure class inside the text written to
close #143, and the wording came from the approved plan. Every round after that found defects
inside the previous round's fix, and rounds 3-5 found leaf- or operator-facing ones rather than
weak pins:

| Round | What the previous fix had introduced |
|---|---|
| 2 | the coupling that fixed round 1 pinned PRESENCE, not exclusivity (an appended widening passed); the over-refusal fix to the bullet counter threw away the enum closure it replaced |
| 3 | a half-followable operator remedy replaced by an unfollowable one, pinned by a check that ratified it; an editing-note bound whose marker occurs zero times in the document, so the note could be gutted to say the opposite |
| 4 | a commit claiming to delete a duplicate created one — four shadowed test methods, two of that round's own fixes in the dead half; the severity sweep's predicate broken from BOTH sides in one round; a checklist clause widened on the rubric side and not on its own |
| 5 | a tie-break ordering the value its own `major` bullet forbids — and both sentences had been pinned literally in round 4, so the CONTRADICTION was pinned rather than caught |

**Two lessons the histories above did not already carry.**

1. **Record fixes fail at the same rate as code fixes and nobody re-runs them.** Three in this one
   loop; `signs-episodes.md` §"The fix was to a RECORD" has them. This is the sub-case of "a
   finding sits inside the previous round's fix" that gets skipped because prose does not look
   like something you execute.
2. **A pin can pin a contradiction.** Round 4 pinned both tie-break sentences positively, which was
   the right move against a digest-only guard — and one of them ordered a verdict the rubric's own
   `major` bullet forbids, so the literal pin froze the disagreement instead of surfacing it.
   Pinning a sentence proves it survived; it proves nothing about whether it agrees with the
   sentence three lines up. Round 5 found it by RENDERING the leaf's prompt and reading the
   documents against each other, which no mutation sweep and no census can do.

**What the cap bought.** Round 5 was the most productive round of the loop by severity: the
shadowed-test-method defect (which had silently removed four of the branch's own checks), the
tie-break contradiction, and an end anchor that made three documents' promise to the operator false
(`## On-failure behavior and retry` still matched a word-boundary terminator). Stopping at the
default of three would have shipped all three. That is not an argument for raising the cap — it is
the cost side of the budget, recorded so the next reader can weigh it (§"The budget" below).

**The shape changes the signs prescribe were all made, and all made late**: predicate → set identity
(round 4, after the predicate was broken from both sides), source-substring pin → drive the
artifact (round 4), re-enumerating a rule → defer to its single statement (round 5, after two
successive half-fixes of the same enumeration). A loop that reached the cap with the shape changes
only just landed is `SKILL.md`'s "five rounds without the class descending → the shape of the rule
is wrong" arriving at the same reading from the other side.

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

## The gain cut — the two directions it fails in

`SKILL.md` §"Fix or out of scope" cuts a finding by whether it gets a leaf closer to reporting its
task done. Each half of that cut has a recorded failure, and they are opposite, so neither half is
a simplification of the other.

**Keeping an adversary half: issue #71's `Glob` pattern check.** `TODO.md` records it as "where
most of issue #71's eleven review rounds found defects" — an escape it missed, an over-refusal it
caused, an infinite loop in its brace expander, and four claims about the tool that were
measurements of something else. **Every one of those is a defect in the check, not in anything it
protected**, and the design was withdrawn in the end for defending reads that cannot happen. The
corrected lesson is not that its escapes were unreal: it is that **reading the operator's
credentials never moved a leaf one step toward generating a kernel**, so the rounds bought nothing
whatever they found. Read with the entry above, which is the same branch measured on the
stopping-condition axis.

**Dropping the shortcut half** has no single-branch episode here because the premise that would
have caused it was corrected before landing (2026-08-25). The standing evidence is structural and
belongs on this side of the file: the whole verdict apparatus — `gate`, `validator`, `judge`,
`verdict.json`, the audit trail — exists because a leaf that cannot reach a green verdict honestly
reaches it the other way, and `docs/design/zero_base_architecture.md` §A4 states the sharpest
form, a kernel co-generated with checks that report its defect as passing. A cut that classified
those as "the leaf would have to be trying" would have taken the review off the machinery that
makes a verdict mean anything. **The axis is the gain, never how deliberate the route looks.**

## The budget — why SKILL.md caps the rounds, and what the cap costs

`SKILL.md` §"Stopping conditions" opens with round 0 plus three rounds as the default and five as
the cap. **The default is the Codex launch's shape, not a round number picked for size**: the
launch belongs in round 2, and its findings need a round after them, because the regularity this
whole file records is that most findings sit inside the previous round's fix — PR #51 three rounds
running, PR #68 all six. A two-round default would have ended exactly where that regularity says
the next defect is. Every other condition in that section is a property of the findings, so none of them bounds
what a loop costs, and the histories above are what that looks like: **17 subagent rounds plus 3
Codex passes** (PR #51), **15** (issue #71), **9** (L128), **8 segments and stopped by the user
rather than by a condition** (PR #58 — and `SKILL.md`'s own "five rounds without the class
descending" sign would have stopped it earlier), **6** (PR #68). The short ones are the changes
that fixed existing machinery: **4** (issue #63), **3** (PR #53).

**The cap is calibrated on a real loop, not chosen round.** The skill split (2026-08-25, the entry
above) ran round 0 plus four — exactly the cap — on a change that ADDS checking machinery, the
expensive class, and its ~50 findings were nearly all introduced by the change under review. So
the cap sits one round above the default, and that gap is where a change of that class lives
rather than being spare budget for an ordinary one.

**What the cap costs is real and is accepted knowingly.** Rounds past five have produced genuine
defects here: issue #71's round 15 found five in a committed measurement script, two of them
functional; PR #72's third Codex pass found what four subagent rounds had missed. **Do not restate
the budget as "nothing is left after five"** — that sentence is false, and this repository punishes
a false claim harder than an unfound defect.

**What it is weighed against, measured at `811dfff` over 2026-08-01..2026-09-01** (a numeric
breakdown rots, so read the conclusion and re-take the figures rather than citing these): 486
commits, of which `spec/` took **4** while `tools/tests` took 258, `TODO.md` 143, `docs/` 120 and
`.claude/skills` 46 — and every open issue on the repository was infrastructure spun out of a
review loop, none about the generation the repository exists for. **The conclusion that survives
re-measurement**: the loop's cost is not paid out of slack, it is paid out of the work the
repository exists to do, and no finding-defined condition can see that.

**The two halves of the cap, kept apart.** It ends the SEARCH, never the REPAIR — an in-scope
finding already on the table at round 5 is fixed before the branch moves, and the fix commit
answering it is not a sixth round. A budget that let a known `leaf shortcut` ship would be a
fail-open dressed as a process rule.
