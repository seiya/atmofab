# Recording a measurement so it cannot rot

Moved out of `SKILL.md` verbatim (2026-08-25). `SKILL.md` round 0 item 2 keeps the four rules;
this file keeps the episodes that produced them.

2. **Do not type measured values by hand; generate them from the artifact you measured.** Keeping
   "a list of every place you wrote a number, re-measured at the end" is not enough — in PR #66 I
   **mistyped it immediately after measuring** (measured suite 4679, wrote 4680). Seven numeric
   errors appeared on that one branch, the last of them in the PR body's disclosure section. What
   worked was **a script that substitutes the numbers in TODO / docs from the measurement
   artifacts**, run and diffed. Leave no path where a human transcribes.

   **"Re-measure at the end" fails outright once a review loop starts, because the end keeps
   moving.** On TODO:269 one paragraph rotted **three times, twice inside a commit whose stated
   purpose was to restate it** — each restatement was correct when written and was falsified by
   the next round's own fix commits, and reviewers reported the same two numbers in three
   consecutive rounds. Note the shape: a fix commit adds a test or a hunk, so the very act of
   answering a review finding invalidates the ledger that describes the branch.

   - **The form is the bug, not the numbers. Write every measurement as a HISTORICAL RECORD that
     names the commit it was taken at** ("at `7c6d187`: 17 hunks, 10 killed …"). A record cannot
     go stale; a claim about the present can, and will, once per round. A reader who needs today's
     figure runs the command.
   - **State the PROPERTY the count stands for, next to the count** ("every behavioural hunk is
     pinned"). That is the part the reader needs and the part that does not move — and it is what
     survives when the number is obsolete anyway.
   - **A count with no unit is not reproducible.** "14 hunks" was reported as 19 and as 16 by two
     reviewers before anyone noticed the figure depends on the diff CONTEXT WIDTH (`-U0` / `-U3` /
     `-U5` gave 21 / 17 / 15 on one range). Name the width, the command, and the exclusions.
   - **Recording that a number "was right when written" does not stop the next rot** — that
     sentence itself was written twice and rotted twice. Only changing the form stopped it.

## PR #100 — a generated number, falsified by a run nobody opened

The branch that produced `SKILL.md`'s "enumerate the comparable runs" rule did everything the four
rules above ask. The figures were produced by a committed instrument rather than typed, the
document named the run they came from, and three rounds of reviewers re-derived every one of them
independently and found no arithmetic wrong.

Round 4 pointed the same instrument at a DIFFERENT run. `orch_20260807T002410Z_acf2b996` — the next
day's closure of the same node, the same endpoint, the same two models, running the very
configuration the document recommended, and PASSING — drew 82 210 output tokens where the document
had inferred a requirement of 72 674-74 302 from the previous closure. It had been on disk in the
same `workspace/` the whole time.

Three things this episode establishes that generating-from-the-artifact does not cover:

- **The rot was not in the number, it was in the QUANTIFIER.** "The requirement is 72 674-74 302"
  was arithmetic over the one closure that had been opened; nothing in it was mistyped, and no
  re-measurement of that closure would ever have caught it.
- **Every reviewer inherited the population.** Rounds 1-3 were handed the same single run and
  checked the arithmetic over it, correctly, three times. A reviewer instruction to "re-take the
  measurement" reproduces the sample; only "find the comparable runs" changes it. The finding came
  from a blank-slate reviewer with no history, which is the round the budget places at four.
- **The fix was to change the question, not the number.** Patching 72 674-74 302 to 82 210 would
  have invited the next closure to falsify that. The document now states the largest COMPLETED
  draw across the runs available, names the population (two closures, 11 leaf requests), and calls
  it a bound that moves. That form cannot be falsified by a new run — it is updated by one.

The corpus sweep that would have caught it is one loop: every `workspace*/orchestrations/orch_*`
with a `launches/*.http_response.txt`, run through the instrument. It found two runs, and took
under a minute.

## PR #116 — three corrections a script reported as made, and never wrote

One `python3 - <<PY` block made three replacements in a canonical document and then asserted the
presence of a fourth. The fourth assertion failed, the process exited, and `write_text` — the last
statement in the block — never ran. The traceback named the fourth replacement, so I fixed that one
in a follow-up script and moved on. The three that had "succeeded" were discarded with the process.

What shipped for two more rounds: a document stating that the invocation closed TWO channels while
the code closed three and its own module docstring said the count was stated *because an earlier
version said two and was wrong*; a paragraph promising a diagnostic the same commit had measured
false and removed from four other documents; and an unqualified requirement whose measured
counter-example two other files cited THAT document as recording. The commit message described all
three as done. A disclosure round found it by reading the document.

The rules this produces are in `SKILL.md` §"Before you hand it over" item 2. Both are mechanical:
**one write per edit, verified by re-reading the file after the write** — asserting the OLD text was
present proves nothing about whether the new text landed — and **never place an assertion for edit
N+1 between edit N and its write**. The general form is the same one the placeholder episode above
teaches: the remedy for hand-typed numbers has its own failure mode, and so does the remedy for
hand-edited prose. Verify the write, not the intent.


## Issue #153 — a corrections bullet that committed the error it was condemning

Round 1's two axes independently re-measured every number the branch recorded and found five wrong.
The fix commit rewrote them as historical records naming the commit and the command, and added a
`TODO.md` sub-bullet titled "Corrections to this branch's OWN records, because a wrong baseline makes
the next delta wrong". Its three items:

1. the three-suite baseline at `59fb060` was recorded as 2314 and is 2336 — 2314 was my own count
   taken mid-implementation with 22 tests failing, written in as if it were the baseline;
2. the full-tree baseline was **cited rather than measured** — "5683 / 2949 at `59fb060`, the figure
   recorded by issue #149's own entry above", where that entry records 5683 at `de53c04`, nine
   commits earlier; the measured figure is 5685;
3. a `ruff` I001 delta "was 227 when that commit landed".

**Item 3 was wrong, and wrong in item 2's shape.** Measured per revision, one fresh detached worktree
each: `59fb060` 220, `17b7a8f` 226, **`8c77ad4` 226**, `bcaa99b` 227, `b134d68` 227, `aed433d` 229.
The original figure (220 → 226) was correct at the commit carrying it; 227 is `bcaa99b`'s; the
correction attributed a later commit's figure to `8c77ad4`. So the bullet performed the
wrong-attribution error one item after condemning it, and replaced a correct number with a wrong one.

**The cause is nameable and it is not carelessness about arithmetic.** Two round-1 reviewers reported
227, both measured at branch HEAD. `atmofab-enforcement-change` rule 3 says, in these words, not to
write someone else's measurement as your own — cite it or re-measure. I had read that rule the same
session. What defeated it: **a reviewer's finding arrives already carrying evidence.** The number came
with a table and a command, so the correction felt verified before it was written, and the one thing
neither reviewer had done was measure at the commit the sentence would name.

**Two rules that follow, both cheap:**

- **Re-measure at the commit you are about to NAME, not at HEAD.** When the finding is about a
  per-commit figure, take the whole series in one worktree-per-revision loop; the attribution is then
  visible instead of inferred, and the loop is six lines.
- **Record that a correction was wrong rather than deleting it.** Round 3 verified the branch's
  records and the third item's own correction is now in the ledger with the per-revision table. A
  corrections bullet that silently loses an entry is worse than one carrying its own history: the
  next reader cannot tell an audited bullet from an unaudited one.

**What the form change did buy.** Round 3 re-executed every other executable claim in eight commit
messages — several to the exact traceback line, assertion text and per-file digit — and found nothing
else wrong. The rewrite into "historical record naming the commit and the command" held; the one
failure was the entry written from someone else's measurement.
