# Mutation checking: the episodes behind the rules

SKILL.md's round-0 rules are compressed. This file holds what each one came from, for the cases
where the rule does not obviously apply.

## Collection errors read as green (PR #68)

Three mutants in the PR #68 census killed pytest during **collection** rather than in a test: the
tests build the §5.1 fixture in the class body, so breaking it takes collection down with it. A
scorer that reads only `FAILED` lines saw no failures and recorded them as killed. Re-running with
`--continue-on-collection-errors` produced 41-47 real failures per mutant. Put the flag in your own
scorer and in the reviewer's instructions.

## A stale worktree makes every mutant look killed (PR #67)

The script forces a baseline run; handwriting does not. In PR #67 a handwritten sweep ran in a
worktree that still held an **old copy** of the tree, so the baseline was already red and every
mutant "failed" = looked killed. Nothing was killed at all.

The dangerous variant is **a sweep interrupted by a timeout**: the mutation is never written back,
so the worktree stays mutated for the next run. Rebuild the worktree every time, or always print
the baseline line.

## Move PRs: `--range` cannot reach what moved (PR #68)

PR #68 moved the §5.1 layer into a backend. My mutants — 31 hunk plus 8 mechanism — had **zero
unexpected survivors**, while a reviewer's independent sweep found **11 real unwitnessed
decisions**, most of them existing patterns that appear nowhere in the diff (`_MODULE_PARAM_RE` and
`_INTENT_RE`'s `re.IGNORECASE` among them). Hunk mutation on a move answers only "is the other half
of the move alive as a pair".

In the same PR one decision, `closed = True`, sat inside a hunk of `@@ -36,11 +40,200 @@` — 200
lines as one block, which any of those 200 lines kills. Past 50 lines, re-target each judgment
inside the hunk individually.

## Handwritten harnesses: three harms in one PR (PR #53)

- `str.replace` rewrites **every occurrence at once**. The same rule lived in three gates, so
  "mutating all three at once kills it" looked sufficient; hitting them **one at a time showed 2 of
  3 surviving**, both reachable fail-opens. Those three were in **separate hunks** (#3 / #5 / #7,
  confirmed), so the script would have hit them individually
- It **mutated the working tree directly** (the script runs in a separate worktree under
  `~/.cache/mutation-check`) — while the reviewers were being told not to modify the checkout
- A reviewer wrote a file with the same name into the scratchpad and overwrote the harness

The script is not universal either: a hunk bundles neighbouring changes, so a pinned change and an
unpinned one in the same hunk means the hunk dies and hides the unpinned side. When one rule lives
in N places, or one hunk holds several judgments, follow up once at line granularity.

## `git checkout -- <file>` deletes your work with the mutation (issue #63)

Done **twice** on that PR. The first time it took an uncommitted P1 fix with it, noticed only when
the next edit to the same file failed on a mismatched anchor; the second time the same move
recurred on another file. `git checkout` discards **the whole difference from HEAD**, not just the
mutation, so it always does this when a mutation and your work share a file.

Revert by (a) running in a separate worktree (the script's default) or (b) taking `cp <file>
<scratchpad>/<file>.bak` first. Committing right before a handwritten mutation also works, but a
backup is faster than verifying "I should have committed" every time.

## A mutation that did not apply is indistinguishable from green (PR #76)

A substitution script's `assert old in t` failed, the test ran **unmutated**, and `1 passed` came
back. It nearly became "this pin works" in the prose — the pin was in fact false and went green
against `origin/main`'s wording, which only replacing the whole file revealed.

- Count the occurrences before substituting and **exit non-zero on zero matches**.
  `scripts/mutation_check.py` does this for the case that matches it — a hunk `git apply -R`
  refuses is reported as SKIPPED and exits 1 (until 2026-08-19 it was counted as neither a survivor
  nor inconclusive, so a run where every hunk skipped printed "every hunk is pinned" and exited 0).
  It does **not** do it for a range that produced no hunks at all: that prints "nothing to check"
  and exits 0, which is why the rule above is to read the hunk count rather than the exit code
- The certain method is **replacing the whole file with `git show origin/main:<path>`** — string
  identification cannot fail
- Put it in the reviewer launch prompt as well: PR #76's two reviewers built harnesses that abort
  on patch failure and caught two real application failures

## Tests spinning in neutral

Hunk-level reverting cannot see "the hunk is alive, merely unobserved". In L128, replacing the
scope analysis with `return joined_masked` — **deleting the mechanism entirely** — left everything
green, and the five tests written to pin it were all inert. The cause was the fixture producing the
same violation through another declaration path.

**Your own mutation list carries your own blind spots.** In L174 a new **offset-translation layer**
(the only conversion, effective solely when the fallback of two readings is used) was not in my
mechanism list, and **replacing it wholesale with the identity function left all 825 tests green**.
A reviewer's independently built sweep found it. That is why the launch prompt asks reviewers to
build their own mutants and never hands over mine.

**A negative assertion is green when the detector breaks.** "This document contains no forbidden
spelling" stays green when the scanning regex is replaced by one that cannot match. PR #76's
document-inspection test had seven witnessed surfaces, all of which proved only "it works on
today's tree", and one mutant survived silently. The fix is six lines: feed one string that must be
flagged and one that must be admitted, with the rule defined in one place (a class attribute regex)
called by both the body and the self-test.

**When a mutant dies, read why.** The most frequent false positive, twice in PR #57: reverting the
mutation fails the test for a reason other than the one the test claims. An unbuilt fixture killed
`_write_json_transaction` with `FileNotFoundError`, which simply did not match
`assertRaisesRegex(RuntimeError, ...)` — and the actual fail-open (a guessed role written durably)
was never observed at all. A kill from a setup error is worth exactly as much as green.

**Rewriting a test can delete a witness.** PR #57's round 3 replaced a test with a better one and
removed the only test observing a validator-side backstop it had deliberately kept. The mechanism
lived on, and mutants went green. Sibling of "pin at the handler, not the helper".

**Reproducing the wiring is not observing the wiring.** A test meant to pin what `run_substep`
passes as `pure=` computed `pure` itself and handed it to `_resolve_reuse_resume` directly, so
rewriting the production line did not kill it (issue #63's PR). One test: does the test call the
production entry point? If not, it pins "the argument is forwarded", not "that value is chosen".

That shape lies in its docstring easily: the same test's docstring claimed to assert "the value
that actually reaches the probe on each production path" while the body never called one. When you
write "drives X" or "the value that actually arrives", grep for that function name in the body. If
it is absent, the docstring is false — a defect on the "false evidence" side, and a reviewer had to
point it out.

**Enumerations die one element at a time.** Checked together in one test, a missing element goes
unnoticed: dropping `logical` let every `logical flux_ok` through, hit for real.

**One test per occurrence of a rule.** PR #53's `out_scope` had the same line in three gates; one
test satisfied me while the other two survived mutation, both reachable fail-opens.

**Stateful code needs a fixture matching the lifetime of the state.** If the counter lives for the
whole file, a single-procedure fixture cannot pin it. PR #53's `select` leak reproduced only with a
two-procedure fixture, and its nested variant additionally required "another kind of select inside
plus a following guard". Always include a version with one level of syntactic nesting: the round
after the flat version was fixed, the nested version ate the fix.
