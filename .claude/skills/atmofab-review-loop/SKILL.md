---
name: atmofab-review-loop
description: Use when implementation in this repository reaches a pause and review begins, when running subagent or Codex review rounds, when fixing findings and moving to the next round, and when judging whether the loop has converged. Required reading for 「レビューして」「レビュー回して」「指摘を直して」「codex review」「この PR merge していい？」「まだ見るべきところある？」 and immediately after finishing the implementation of an audit finding or an issue. The subject is **review of changes you made**; do not use it for reading existing spec, docs, or implementation to judge whether they are sound (review without a change).
---

# The review convergence loop

What this skill holds is **how to run a review**: round structure, what to tell reviewers,
convergence criteria, mutation checking. If the change touches enforcement machinery (a gate,
validator, hook, or capability), **start `atmofab-enforcement-change` as well** — it owns the
domain-specific traps (dual-read pairs, failure attribution, verification commands) and they
are not duplicated here.

Track record: PR #51 converged after 17 subagent rounds plus 3 Codex passes, and **15 of the
defects had been introduced by the fixes themselves**. What follows was derived backwards from
that breakdown.

**This file carries the rules; `references/` carries the episode each rule came from.** A rule
here that does not obviously apply to your case is answered in its reference file, not by
guessing. `L128` / `L174` name entries of `TODO.md` by the line they sat on when the work
happened, and `TODO:269` / `TODO:414` name two more the same way (as does `L118` in the sibling
skill). **Those entries are no longer in `TODO.md`**: issue #181 moved every finished entry to a
comment on the issue or pull request it belongs to, so the label is a name for an episode and
nothing resolves it in the file. Resolve a label at the merge base of the pull request that used it — `git show <pr merge>^:TODO.md | sed -n '<n>p'`,
and find that merge with `git log --oneline --merges --ancestry-path <the commit that did the work>..origin/main | tail -2`
when the label carries no pull request number, which is the case for `L118`, `L128` and `L174`. That is how these were checked: `L118` is [the duplicate `_split_top_level_commas`](https://github.com/seiya/atmofab/issues/181#issuecomment-5559628555),
`L128` [the `&`-continuation blindness](https://github.com/seiya/atmofab/issues/181#issuecomment-5559628999),
`L174` [the tree-sitter front-end swap](https://github.com/seiya/atmofab/issues/181#issuecomment-5559629951),
`TODO:269` [the `STALE_DEPENDENCY_IR_MARKER` exit-code work](https://github.com/seiya/atmofab/pull/88#issuecomment-5560285483)
and `TODO:414` [the development-documentation branch](https://github.com/seiya/atmofab/pull/90#issuecomment-5559633234).
`L200`, which `PR #57`'s title names, is [the `agent_role` work](https://github.com/seiya/atmofab/pull/57#issuecomment-5559631060).

- `references/mutation-testing.md` — round 0's episodes, and the "spinning in neutral" family
- `references/measurement-records.md` — why a measured number rots once per round
- `references/round-conduct.md` — commit granularity, PR staging, orphan waits, over-refusal
- `references/codex-episodes.md` — the launch mechanics and the stall / filter case log
- `references/sonnet-delegation.md` — the delegation experiment log and how to keep measuring
- `references/class-descent-log.md` — per-PR stopping-condition histories and the proxies' evidence
- `references/signs-episodes.md` — the case history behind each mid-loop sign

## Review target and commit granularity

**The target is `git diff origin/main...HEAD`** (everything the branch has stacked on main).
Show only the most recent commit and you miss the defects the previous round's fix introduced —
in practice **most findings sat inside the previous round's fix**.

**Confirm the working tree matches the commits before handing it over** (`git status
--porcelain` empty).

**One commit per finding is the default**: the mutation check works as-is with `--range
HEAD~1..HEAD`, which fix answers which finding stays traceable — **put the gist of the finding in the commit
message** — and the next round can be told to look hardest at the files you just fixed. Folding
is allowed when splitting is unnatural, for an experimental check, or for something you expect to
revert immediately — **say in one line why**, and **read `git show --stat` before writing the message**, which must
describe everything in the commit (PR #67 folded eight fixes and described two).

**This is not only a record rule, and it is not only about folded commits.** A reviewer reads the
message to decide where to look, so a file the message omits is a file the round does not attack.
On issue #142 the first commit's message described the code and the prompt template and said
nothing about the four documents it also edited — and one of those documents carried a COPY of the
rule the template stated, in the unbounded form. Round 1 fixed the template; round 2 had to find
the same defect again in the document, as a `leaf shortcut` on the other transport. **One omitted
line in a message cost a whole round.** Read the stat, and name every file, especially the ones you
edited as an afterthought — those are exactly the copies.

**`git commit --amend` for a message-only fix requires an empty index** (or `--only`); it
silently absorbs whatever is staged (PR #58: an amend swallowed a file replacement, leaving a
commit that claims work `git log -S` cannot find). **If it is unpushed you can fix it by
squashing — and say in the message that you did.**

**If the plan staged the PRs, close each stage before starting the next** — implement A →
review → fix → merge → then B. The review target is the whole stack, so splitting the history
afterwards misaligns the review unit from the PR unit, and every fix lands on B while A's tip
stays "A before review". **The moment stage A is not correct on its own, the point of staging
is gone.** Recovery: fold into one, or move A's fixes onto A and rebase, or merge in order —
all more expensive than keeping the order (L174).

Episodes for all of the above: `references/round-conduct.md`.

## Before you hand it over (round 0)

1. **Pass the mutation check.** Spending a round on a reviewer telling you the tests are weak is
   waste.

```bash
# right after a fix commit (the default)
python3 .claude/skills/atmofab-review-loop/scripts/mutation_check.py \
  --range HEAD~1..HEAD --paths <sources you touched> \
  --test-cmd "python3 -m pytest tools/tests/<relevant file> -q -p no:randomly -x"
# to look at the whole branch at once (three-dot, like the review target: it excludes the
# commits main gained after you branched)
#   --range origin/main...HEAD
```

**Pass `-x` — but only with a `--test-cmd` whose baseline is green** (see the handwritten-sweep
bullet below, which is where that bites). The exit code decides the verdict — the output is read
only to tell a real failure from a suite that never ran. Hunks run in separate worktrees, `min(cores - 2, 4)` at a
time by default and never more than the hunk count (`--jobs`); 4 hunks × 805 tests measured 5m52s → 43s. **Do not put a `TMPDIR=` prefix in
`--test-cmd`**: each job already gets its own temp root, a prefix puts them all back on one, and
failures then belong to no hunk and are recorded as `killed` — a false pin (at more than one job
the script refuses the combination, exit 2). **Run the un-mutated baseline first**; a red
baseline exits 2 (`--skip-baseline` removes the check — write down why).

**Exit codes**: 0 = clean, or the sole survivors are prose-only, or nothing was left to check —
**so read the hunk count**. 1 = an unannotated survivor, an inconclusive or skipped hunk, or a
change with no revertible hunk. 2 = the run itself cannot be trusted (red baseline, baseline
`--timeout`, unresolvable range, `--repo` that is not one, `TMPDIR=` with several jobs).

The rules, one line each; `references/mutation-testing.md` has the episode and the reasoning for
when a rule does not obviously apply:

- **A red baseline is the HARNESS's fault before it is the suite's** — the per-job temp root and
  the `--workdir` location are inputs to the suite. **The harness's scratch paths must not be
  paths the suite asserts about.** The BASELINE RED message names both levers; try them before
  touching a test
- **A documentation-only diff reports every hunk SURVIVED, and that is a MEASUREMENT** — split
  the hunks: mechanically checkable claims get a check, the rest is declared prose in the commit
  message with the count. Do not write a check that parses prose to decide what a claim IS
- **Pass `--continue-on-collection-errors` when a hunk comes back INCONCLUSIVE, and drop `-x`
  when you do** — a mutant can kill pytest during collection and a `FAILED`-line scorer reads
  that as green (PR #68: 3 mutants hid 41-47 real failures). Put both facts in the reviewer's
  instructions too
- **Run the baseline for handwritten sweeps too, and read it before you trust `-x`** — the script
  refuses a red baseline (exit 2); a sweep you write yourself will not. Two causes, same false
  green: a stale worktree (PR #67), a sweep KILLED mid-run — a timeout or a Ctrl-C skips the
  `finally` that restores the file, so the NEXT run measures a mutated baseline (PR #104; the
  script caught it and exited 2, which is the behaviour to keep) — and a suite that ALREADY has a
  failure unrelated to the change,
  where `-x` stops every mutant at that same failure so every mutant reads as `killed` — a false
  green over the whole run, not a per-hunk slip. A fourth cause is the SHELL, and it is the one
  that leaves no trace in a summary line: **this environment's shell is zsh, which does not
  word-split an unquoted `$var`**, so a sweep that builds its test command in a variable
  (`run(){ pytest $1 }`) hands pytest one argument that is several paths glued together and
  collects **zero tests** — every mutant then reports `no tests ran`, which a scorer reading only
  the last line scores as not-failed. Hit for real on issue #142's round 0. **Read the baseline's
  COUNT, not its colour**: `197 passed` and `no tests ran in 0.00s` are both non-red. atmofab's standing instance USED to be the
  path-depth-coupled `ForbidBackendCredentialReadTests` cases — one PR #98 reviewer reported 12 of
  12 mutants killed that way before re-running — and issue #84 closed it on 2026-09-02: those
  cases now build their own `$HOME` and checkout, so they pass at any depth and in a worktree.
  The class is not closed with them: **check the baseline is green for the `--test-cmd` you pass,
  and if it is not, deselect the failures or drop `-x`**
- **If the change is a move, hunk mutation answers almost nothing** — `--range` cannot reach code
  that moved without changing. For move / rename / extract PRs build **mechanism-level mutants
  over every mechanism in the files you touched** (PR #68: my 39 mutants, 0 unexpected survivors;
  a reviewer's independent sweep found 11)
- **Past 50 lines, a hunk hides the decisions inside it** — re-target each judgment individually
- **Survivors** mean no pin, or a neighbouring check killing it. Fix them or write down why not.
  Test-file hunks are excluded by default (`--include-tests` keeps them); **nothing else is
  excluded on a guess about `#`**: it opens a heading in Markdown, a preprocessor directive in the
  c/cpp families, a shebang, a lint pragma, and inside a Python string or a YAML block scalar it is
  the prompt-template text this repository pins. Prose-only hunks are **annotated, not excluded**
  (`[prose-only (comment/docstring) — expected]`), by AST comparison over Python files only — so
  a `#` inside a string literal is not prose, a prose hunk in another file type is unlabelled,
  and **a hunk that also carries a code move is never annotated**: a missing annotation is not
  evidence that a hunk is code. One half of a code move is expected to survive — read the pair
  together, and for a move between Python modules pass `--continue-on-collection-errors` FIRST:
  the halves whose import breaks at collection report INCONCLUSIVE otherwise, and you have spent
  a run to learn it
- **Get `--range`'s base wrong and "no hunks in range" looks green** (exit 0). Causes: a base
  that resolves but is wrong (`origin/main..HEAD` is empty after a merge; after a rebase it is
  your own commits replayed), a `--paths` matching nothing, a round that is all test files.
  A change with no revertible hunk — pure rename, binary, mode change, empty new file — is
  listed by name and **exits 1 whatever else the run found, because nothing was tested for it**
- **The worktree is checked out at the range's END COMMIT, so a pin that is still UNCOMMITTED is
  invisible and its hunk reports SURVIVED.** Not a false positive — the script is answering about
  the commit — but it reads exactly like "this behaviour has no test", and the natural response
  is to go and write the test you already wrote. **Commit the tests before the sweep**, and when
  a survivor surprises you, revert that one hunk by hand against your working tree before
  believing it (`references/mutation-testing.md`)
- **Hand-revert a KILL that surprises you, not only a survivor.** The rule above doubts survivors;
  the reverse happened on issue #153. A re-run reported EVERY hunk killed, including one whose whole
  content was a widened type annotation and a deleted unused local — no behaviour to pin — and a
  hand revert of that same hunk was **green**. Cause unidentified; the runs differed in `--paths`
  and `--test-cmd`, baseline green in both. **The verdict you keep is the one you can reproduce by
  hand**; report an unreproducible kill as such rather than banking it, because "every hunk is
  pinned" is what a reader will quote back. **The tell is a kill on a hunk you cannot name a
  behaviour for — and the sharpest instance of that tell is a hunk that is half of a code
  MOTION**, which the prose-annotation bullet above already calls an EXPECTED survivor. When the
  script reports one of those `killed`, two rules of this file contradict each other, and the
  hand revert is how you find out which. Issue #153 hit it a second time: the extraction of
  `_structure_reading` out of `_fortran_procedure_envelopes` was reported killed, and reverting
  exactly that hunk by hand — restoring the inline block while the extracted function stayed —
  left the file green, as a behaviour-preserving motion must. Cause unidentified both times, so
  do not spend the round on it; **spend the two minutes on the revert, and report the kill as
  unreproducible rather than counting it toward "every hunk is pinned"**
- **If the change's mechanism lives inside a test file, hunk mutation does not apply** — "nothing
  to check" with a correct base is **not applicable, not a pass**, and `--include-tests` does not
  rescue it (reverting an ADDED test hunk deletes an assertion, so it always survives; a hunk
  that CHANGED an assertion is different — reverting it makes the old assertion contradict the
  fixed code, so it reports `killed` while saying nothing about the code under review). Build
  mutants that kill each decision of the new machinery one at a time
- **Do not handwrite a mutation harness** (PR #53's `str.replace` rewrote all occurrences at
  once, hiding 2 reachable fail-opens). If you must: hit occurrences one at a time, **assert the
  patch applied AND that it changed behaviour**, and **re-point a mutant list reused across
  rounds** — a stale target reports as a survivor (PR #86, visible only because the harness
  printed `PATCH DID NOT APPLY` instead of counting them green), and a not-applied patch is a
  failed run, not a finding — **report it as its own category and count it out of the denominator**.
  "Applied" is not enough on its own; a semantically identical rewrite patches cleanly and proves
  nothing. **And do not PATCH a mutant list between rounds — write it out**, and **read the RUN's
  own output, not only the artifact it produces** (PR #104: three silent misses, two of them
  reporting plausible totals for a sweep that measured nothing new). **The script is
  not universal either: one hunk can bundle a pinned and an unpinned change, so follow up at line
  granularity when one rule lives in N places**. **A handwritten sweep over a SINGLE file must run
  with `PYTHONDONTWRITEBYTECODE=1` and `-p no:cacheprovider`** — consecutive same-byte-length
  rewrites within one second reuse a stale `.pyc`, and the mutant that reports `killed` is the
  PREVIOUS one. Two reviewers hit it independently on PR #100, one of them reporting three live
  mutants as killed; the skill's own script is unaffected because each hunk gets its own worktree
- **Never revert a mutation with `git checkout -- <file>`. TWO reasons, and the second is worse:
  it deletes uncommitted work, and on an UNTRACKED file it is a SILENT NO-OP** — PR #104 shipped a
  mutant for two commits that way. Use a worktree (the script's default; `--keep` leaves it
  REGISTERED, and `git worktree prune` will NOT unregister one whose directory still exists —
  `git worktree remove <path>` does) or a `cp` backup, and **never `cp` out of a worktree you have
  been mutating without checking it is clean first**
- **Mutation checking cannot detect a test spinning in neutral.** A live-but-unobserved hunk
  looks like a pass (L128: deleting the scope mechanism outright kept everything green and all 5
  tests meant to pin it were inert). Countermeasures — each with its episode in the reference:
  - **run one mutation that deletes a whole mechanism** (pass-through `return`, constant condition)
  - **a mutation list you build carries your own blind spots**; hence the standing instruction to
    reviewers to build their own, and **do not hand over your list**
  - **doubt once why a test passed**, especially when it felt obvious: until every other path in
    the fixture that could produce the expected violation is removed, it does not pin its name
  - **a negative assertion is green when the detector breaks — self-test the detector**
  - **when a mutant dies, read why**: a kill from a setup error is worth exactly as much as green
  - **when you rewrite a test, diff what the old version observed**
  - **a test that reproduces the wiring does not observe the wiring** — and that shape lies in its
    docstring easily; grep the body for the function names the docstring claims to drive
  - **a CLASS docstring goes stale by ADDITION** — re-read it when you append a row
  - **a test can pass because of the SUITE'S OWN ENVIRONMENT** (a `conftest.py` redirect). The
    tell: the test reasons about a RELATIONSHIP whose two halves are not both built by the
    fixture. The check is one command — run the new classes under pytest and under
    `env -u <VAR> python3 -m unittest <dotted.path>` and diff the verdicts. The fix is to build
    the relationship in the fixture, not to unset the variable.
    **When the relationship is with the HOST rather than with a variable, the diff has to be over
    a FAMILY of environments, and the family needs every property the rows spell — not just the
    one that is red.** Issue #84's rows reasoned about `$HOME` and the checkout: a sweep over the
    checkout's DEPTH found a third coupled row the red one had hidden, and a round-1 reviewer then
    found two more by varying the SHAPE of `$HOME` (`/home/<name>`, a path containing the letter
    a glob operand spells), which the depth sweep could not see. Enumerate the properties the
    assertions name, then vary each; a sweep that varies one property answers for one property
  - **a mechanism that guards the HARNESS cannot be witnessed from inside the suite** — put the
    witness in a subprocess outside the runner
  - **a rule whose answer on THIS tree is "nothing" cannot be observed through the assertion that
    consumes it.** `assertEqual(rule(corpus), set())` is green whether the rule computes the right
    empty set or returns one unconditionally, and mutating it mutates an assertion, which survives
    by construction. **Move the rule into a module and drive it on synthetic input where the answer
    is not nothing** — both directions, because for a rule that REFUSES something the over-refusing
    direction is the one you will get wrong (PR #104, three mechanisms, same fix)
  - **when a comment JUSTIFIES a rule, mutate the property the justification names** — the rule
    has a witness, the property holding it up usually does not
  - **kill enumerations one element at a time** (regex alternatives, keyword tables); checked
    together, a missing element goes unnoticed
  - **generating the probes FROM a constant gives set identity, not a family that can distinguish
    anything** — the sign "you measured a family and reported the conclusion" below carries the
    two checks; reach for it whenever a table-driven test is the evidence you are about to cite
  - **one test per occurrence of a rule, not per rule** — and **the sharpest trigger is a TWIN**:
    when a change touches one of a matched pair, list the pair, list your witnesses, compare the
    two lists before handing over
  - **when the guard tests a TYPE, the family must STRADDLE that type test, and assert that it
    does** — the type case of the family sign below, and its own line because it recurred three
    times in one branch after two diagnoses (issue #153). A family of malformed values assembled by
    imagination comes out malformed the SAME WAY: ten members, every one iterable, cannot kill an
    `isinstance(x, list)` guard. **More members do not close it; a self-test in the test body does**
    — loop the members through `iter()` and require both a `TypeError` and a success, because that
    is what a later member added on one side only turns red. **Trigger**: any `isinstance` /
    `hasattr` in a guard whose purpose is to not raise — name the operation that would raise
    (`for … in`, `sort`, `len`, `[k]`) and ask which member reaches it
  - **a hand-built fixture can test a shape that does not exist** — check the construct against
    the real corpus before writing the witness
  - **for stateful code, match the fixture to the lifetime of the state**, and always include a
    version with one level of syntactic nesting

2. **Do not type measured values by hand; generate them from the artifact you measured** — what
   worked was a script that substitutes the numbers in TODO / docs from the measurement
   artifacts, run and diffed. **Leave no path where a human transcribes.** A list
   of "every place you wrote a number, re-measured at the end" is not enough, and **"re-measure
   at the end" fails outright once a review loop starts, because the end keeps moving** — a fix
   commit adds a test or a hunk, so answering a finding invalidates the ledger describing the
   branch. **The form is the bug, not the numbers**:

   - **Write every measurement as a HISTORICAL RECORD naming the commit it was taken at** ("at
     `7c6d187`: 17 hunks, 10 killed …"). A record cannot go stale; a claim about the present can,
     and will, once per round
   - **State the PROPERTY the count stands for, next to the count** ("every behavioural hunk is
     pinned") — that is what the reader needs and what survives when the number is obsolete
   - **A count with no unit is not reproducible** — "14 hunks" came back as 19 and 16 because the
     figure depends on the diff CONTEXT WIDTH. Name the width, the command, and the exclusions
   - **Name WHERE a suite figure was measured.** A number can be right and still be a defect if it
     does not say which checkout produced it: atmofab's suite USED to be one lower in a `/tmp`
     worktree than in the primary checkout, because a declared skip fired there — on PR #104 a
     reviewer correctly reported a commit message as wrong for that reason, and the message was
     right, it simply had not said where. That particular skip is gone (issue #84, 2026-09-02) and
     the rule is not: a figure still has to name its checkout, because the next environment-shaped
     difference is not announced in advance
   - **A count that no method can reproduce is worse than no count.** PR #104 quoted five figures
     for "the names this tree reads"; the two that named their reader re-derived exactly, and three
     named no spelling, no file set and no reader — so the sentence claiming each answered a
     different question could not be checked. **Either write the command beside the number, or
     write the property instead and check it**
   - **Recording that a number "was right when written" does not stop the next rot.** Only
     changing the form stopped it
   - **A BATCH EDIT THAT RAISES BEFORE IT WRITES LOSES THE EDITS THAT SUCCEEDED, and the commit
     message then describes work that did not happen.** On PR #116 one script made three
     corrections to a canonical document and asserted its way to a fourth; the fourth assertion
     failed, the process exited before `write_text`, and I read the traceback as being about the
     fourth alone — so three corrections the commit message claimed were never applied, and the
     document shipped two rounds later still stating what the code had explicitly retracted. Two
     rules, both cheap: **one write per edit, verified after the write** (re-read the file and
     assert the new text is in it, not that the old text was in it), and **never let an assertion
     for edit N+1 sit between edit N and its write**. A review round found it; nothing else could
     have, because every symptom was in a document no test read.
     **The trigger is MORE THAN ONE EDIT IN ONE SCRIPT, not "a script that substitutes numbers"** —
     and that distinction is why this rule failed a second time, on issue #161, after I had read it
     the same session. There the two edits were PROSE, in `TODO.md`, and neither raised: the second
     `write_text` was built from the pre-edit-1 text and silently overwrote the first, so the commit
     shipped claiming a correction the file did not carry. Nothing about numbers was involved, which
     is exactly why the rule did not come to mind while writing the script. Verify the write, not the
     intent, for **every** edit — including the ones that are only words
   - **And then verify the substitution RAN.** The remedy has its own failure mode: PR #98 escalated
     to scripted substitution after four hand-typed numbers came out wrong, and the very next commit
     shipped with **eight unsubstituted `{PLACEHOLDER}` tokens in its message**, directly under the
     sentence saying the numbers were no longer typed (an f-string ate the doubled braces). Assert
     that no placeholder survives before you commit — one `re.search` — because a message cannot be
     fixed after it is pushed, and this failure looks exactly like success
   - **When the measurement is a FAILURE read out of a sectioned log, map each line to its own
     section before you quote it.** A CI log, a `pytest` FAILURES block and a mutation sweep's
     output all print `header` then `detail`, repeatedly, and the eye slides from one section's
     header to the next section's error. On issue #161 I attributed a test's failure to an error
     belonging to the section BELOW it, wrote that into a commit message and then into `TODO.md`,
     and a review round found it two commits later — after which my own correction got the same
     detail wrong for one of the three runs. The mechanical form is one loop: walk the log, keep
     the last header seen, and emit `(header, error)` pairs; quote from the pairs, never from the
     scroll. **Reading it yourself is not the safeguard** — this is the same class as writing
     someone else's measurement as your own, with the same cure, and the fact that you ran the
     command does not make what you read off it a measurement
   - **Before you DERIVE a threshold from a measurement, enumerate the comparable runs you already
     have.** Generating a number from the artifact stops transcription errors and does nothing
     about this one: on PR #100 a requirement inferred from one closure was falsified by the NEXT
     DAY's closure — same node, same endpoint, same models, running the very configuration the
     document recommended, and sitting in the same `workspace/` the measurement came from. Three
     rounds of reviewers re-derived the number correctly because they, too, were handed the one
     run. **A derived threshold needs its POPULATION stated** ("largest completed draw across the
     N runs that have one"), and the sweep that finds the population is one loop over the corpus,
     not a judgment call. Where the population is one, say so and call the number a bound rather
     than a requirement
   - **When you PUBLISH the instrument beside the number, republish it whenever either changes, or
     stamp it with the commit it was run at.** Generating a figure from the artifact is not enough
     once the record carries the script that generated it: on issue #181 a pull request published a
     verification script in a COMMENT beside a body quoting its output, changed the script two
     commits later, and republished neither. A reviewer ran what was published against the tree it
     certifies and it failed — on the newer commit's own deliberate edits, which from outside are
     indistinguishable from work the change lost. **A published instrument and a published number
     are one record.** Note where the surface is: a record split across a body and its comments
     drifts without either looking edited. The cheap form is the stamp — "run at `<sha>`" makes a
     listing historical rather than wrong, which is the same cure this list already prescribes for
     a figure

   Episodes: `references/measurement-records.md`.

3. **Run the verification set** and record the measurements. The commands are in
   `.claude/skills/atmofab-enforcement-change/references/verification.md` (suite baseline, ruff
   diff against origin/main, doc size ceilings; its `mcp_call` end-to-end section is for
   enforcement machinery).

4. **Leave the list of surfaces you touched** in the commit message or the pull request. That is where
   reviewers attack from.

## Running a round

**One round = two subagents in parallel**, on separate axes (security-bypass /
correctness+regression+doc-truth).

**One round of the loop puts BOTH agents on the disclosure axis instead** — doc-truth bundled
with correctness loses to it, measured. Plan it before you decide to stop; the two briefs are
under "Stopping conditions". **A disclosure round runs no security agent, so it neither advances
nor resets the two-consecutive-clean-security-rounds condition.**

- **Do not edit until both results are in** — touch a file while one is running and its findings
  go stale. **Do not rely on the prohibition; absorb it in the launch prompt**:

  > "**HEAD may advance while you run. Re-verify each finding against the current HEAD before
  > reporting, and state which revision you measured on.**"

  If you do start editing, **write the fact and the time into the next round's prompt**.
- Always in the prompt: **the target is `git diff origin/main...HEAD`**, and **"do not modify the
  checkout; run mutation tests against a `git archive` snapshot or a separate worktree"**
- **A scratchpad per agent**: "**create your own subdirectory, named uniquely to you (add your
  PID); do not reuse or overwrite existing paths, and delete only the subdirectory you created,
  never the round's shared root**". Put your own working files in a subdirectory the same way.
  Two agents given the same round directory is enough: on this skill's own review round, one
  reviewer's cleanup deleted another's scratch out from under its running pytest
- **Say where a worktree may be created.** `git worktree add` with a relative path under `-C
  <repo>` puts the worktree INSIDE the checkout, where it is untracked clutter another reviewer
  then prunes. Ask for an absolute path under the agent's own scratch directory, and for
  `git worktree remove` when finished
- **Hand over this environment's trap**: in an agent session `grep` may be shadowed — a shell
  function execing `ugrep --ignore-files`, so it **respects `.gitignore`** (check with `type
  grep`). Have corpus measurements enumerate with `find` / `os.walk` (1 of 365 files was visible
  once, and I nearly designed on "`interface` occurs 0 times")
- Write "report only what you ran. Give reproduction steps and file:line. **State explicitly if
  you found nothing**" — this prevents filler
- **What you may hand over to reduce duplication is the axis, not the list.** "Hunk mutation over
  the diff range is done and green" moves budget to sibling rules outside the diff and to
  mechanism level; handing over your mutation list breaks independence
- **Include "build your own mutants that delete one mechanism at a time, run them, and report
  survivors"**
- **Say "mutate the SUBJECT, not the test", in the launch prompt.** Round 0's rules already state
  that reverting an added test hunk deletes an assertion and survives by construction, but that is
  written for the script and reviewers do not read it: on issue #149 a security axis reported a
  whole sweep as vacuous on the strength of replacing the test's own `for line in CONSTANT:` with
  `for line in []:` — which of course stayed green. A second axis mutated the CONSTANT in the
  production module the same round and it was red. **A vacuity claim is only worth the mutation
  behind it**, so ask for the mutation to be named with the claim, and re-run any that mutated the
  test rather than the thing the test is about
- **Spell out the process and shared-resource rules**: no unscoped `pkill`; **create only waits
  whose exit condition can be satisfied, and no background polling**; `/tmp` is a shared tmpfs,
  delete the trees you create. Four accidents happened for real, each spilling into other
  sessions. **Handing over the rules is not enough** — all four were in the prompt and broken
  anyway, and I have broken them myself for 5.7 hours at a stretch. **The DEV hook now
  refuses a sleep-based wait outright** (`tools/hooks/dev_session_hygiene.py`, added 2026-08-28),
  which is the only
  countermeasure in this section that does not depend on the reviewer reading it; the wording
  below is what to hand over anyway, because a refusal that arrives after the agent has already
  chosen to poll still costs the round:
  - **Forbid the MECHANISM, not the behaviour, and forbid it by name.** "No background polling"
    was in the prompt of the agent that produced 144 orphaned shells in 36 minutes and returned
    no report. What worked, measured over the two rounds after it — zero orphans — was four
    concrete sentences: do not use the Bash tool's `run_in_background`; do not write `sleep`,
    `until`, `while … done` polling, `pgrep` loops, or `&`; run every command in the FOREGROUND
    with a bounded `timeout` and a narrow scope; **do not run the whole suite in one command** —
    per-file, one at a time. The last one is what makes the third affordable, and leaving it out
    is what makes an agent reach for a background job in the first place.
  - **Say what to do when the work does not fit**, or the rules above just get broken quietly:
    "if a sweep would take too long, SHRINK IT and say what you cut — a smaller sweep you ran
    beats a large one you waited on." Two reviewers cut their sweeps and said so; both still
    found real defects, and one of them found the largest gap of its round inside the part it
    kept.
  - **A wait must not carry the name of what it waits for in its own command line.** Safe forms:
    (a) for work the harness tracks, **do not poll — the completion notification arrives**,
    (b) **wait on the PID**, (c) split the matching string
  - **Evaluate the exit condition ONCE, by hand, before you leave a wait running.** "Can be
    satisfied" is not visible by reading; run it and look at the exit status
  - **The end-of-round check is `ListAgents` and `ps`, both** — pick orphans up with
    `ps -eo pid,ppid,etimes,args | grep -E "sleep|until|pgrep"` and `kill` by PID (`pkill -f`
    re-enacts the first accident), after confirming no process doing real work is alive at the
    same time. It is a **reconciliation**: keep the PID of every wait you start and match the
    list — and remember **a backgrounded wait returns immediately**, so a turn that launches one
    has not waited
  - **The symptom disguises itself as "the subagent is running and never returns"** — when
    `ListAgents` shows running, suspect that agent's child processes
  - **Check the checkout itself at the end of a round**, not only processes: `git status
    --porcelain` and `git worktree list`. A reviewer's stray worktree inside the repository
    contaminated another reviewer's suite run on this skill's own review
- **If the reported HEAD is a hash you do not recognize, find out what commit it is first** —
  the user or another session can commit to the same branch. That is concurrency, not staleness:
  read it and **judge whether it collides with your scope**
- **When a round's reviewers cannot LAUNCH, the round is not clean and it is not spent.** A
  subagent that dies to a transport error before its first tool call has reviewed nothing; check
  the transcript for tool calls rather than reading the failure as a result. Retry once, and if
  the up-model stays unavailable there are two moves and they are not equal: **convert the axis
  into an enumerated checklist and delegate it** (that is verifiable work, so the sonnet line
  applies), or **run it yourself and lose the independence**. Say which you did, in the commit and
  to the user — an axis run by the author advances neither stopping condition. On issue #149 SEVEN
  consecutive up-model launches died to HTTP 500 / 529 across two rounds, and the round that
  recovered did so as a checklist of seventeen named mutations plus five over-refusal probes; what
  that CANNOT tell you is whether an open-ended attacker would have found an eighteenth, because
  the list was the author's
- **Hand over the premises in one paragraph** (`AGENTS.md` §"Development premises" is canonical;
  hand over this short form): "a single-operator research workflow platform. What is defended
  against is a **`leaf shortcut`** — an `LLM` leaf is not malicious and takes shortcuts, so
  anything getting it closer to reporting its task DONE without earning it (a loosened assertion,
  a hardcoded expected value, a check recorded as run, a gate edited rather than satisfied, a past
  artifact read as input) is in scope at full severity — and the defects my own changes introduce.
  **A hole getting the leaf no closer to done is out of scope**: the operator's credentials, a
  read outside the checkout, another orchestration, anything outliving the run. So is hardening a
  path only the operator can reach, and a construct occurring zero times in the real corpus."
  **Both halves are load-bearing**: without the first, the review skips the machinery that makes a
  verdict mean anything; without the second, it fills with escapes that gain a leaf nothing
- **Make each finding carry its own gain sentence** — "a leaf taking this gets ⟨what⟩ toward
  reporting its task done". **A finding that cannot carry one is not a finding**, it is a mechanism
  description, and the sentence is what you triage against afterwards. It also states the
  over-refusal probe's other half: for a check, the same sentence is what a legitimate input says
- **Require a WORKFLOW ROUTE on every finding, in the launch prompt.** The gain sentence says what
  a leaf wins; the route says where the damage comes out. Together they are the whole filter, and
  without the second a round fills with code that is wrong in the abstract and reaches nothing.
  Hand this over verbatim:

  > "Report a finding only when you can name the route by which it shows up in a real `workflow`
  > run: **which `phase` / `step` / `substep` reaches the code, what input reaches it** (a shape
  > that exists in this tree's `spec` and generated artifacts, not one you invented), **and what
  > comes out wrong** — a wrong `verdict`, a broken artifact contract, a false record, a lost
  > `--resume`, or a `leaf shortcut` that lets a leaf report its task done without earning it.
  > **State the route with the finding, in one sentence.** If you cannot establish the route, do
  > not report it as a finding and do not drop it either: list it under a separate heading
  > `route not established`, one line each."

  **Three things are INSIDE the route, and leaving them out is how this instruction goes wrong**:
  a **test that would not notice the defect** — its route is the next workflow that runs on
  unpinned code, so "the implementation is right but the test is weak" stays real; **text a leaf
  or an operator ACTS ON** — its route is the action it causes (see "Only prose remains" under
  Stopping conditions); and **a record about the branch that is false** — its route is the audit
  trail, which is a workflow output. What the requirement actually cuts is the other family: a
  mechanism description with no run behind it, a construct occurring zero times in the real
  corpus, a hardening of a path only the operator can reach, and style.

  **Read the `route not established` section yourself every round** — it is what keeps the
  requirement from becoming an exclusion list inside the reviewer's head, and it is exactly where
  a real defect whose route nobody has measured sits (PR #98's widest gap spent three rounds
  inside a list of things already believed covered). Items there are input to your own
  reproduction, not a residue list, and they are **not** carried into the next round's prompt
- **Do not let the route requirement narrow the SEARCH.** It governs what is REPORTED, not where a
  reviewer looks: the mutation sweep, the witness census and the over-refusal probe are run in
  full, and a survivor is reported with the route of the workflow that would run on unpinned code.
  If a round comes back small, check that it did not shrink because the reviewer pre-filtered its
  own sweep — ask what it ran, not only what it found
- But **do not name individual findings as excluded** — that is an exclusion list, subject to the
  three-round rule below
- **For a change that adds checking machinery, include "construct legitimate work that this check
  wrongly refuses".** Ask only for misses and the over-refusals stay. The criterion is whether the
  pin is on **the rule** or on **the result the rule produced**; pinning results makes ordinary
  work fail and teaches the habit of regenerating without reading the rule
- **Over-refusal is not a one-off trap; it is my default error direction. Put a probe in every
  round.** Five instances across PR #66 / #67, three of them recurring **each time I rewrote the
  same rule**. Miss-direction bugs come one per round; over-refusal comes in a new shape with
  every rewrite. Four countermeasures that DETECT it: (a) for each check you write, construct one
  piece of correct work that violates it, (b) **keep the probe in the reviewer instructions
  through the final round**, (c) if over-refusals persist after two rewrites, conclude **the rule
  is not an invariant** and change its shape, (d) **build probes from the project's own "what we
  do next", not from imagination** — implementing the next TODO / plan item is the cheapest
  over-refusal probe there is
- **The fifth countermeasure, and the one that actually worked: when you add a FLOOR, default to
  FILL rather than REFUSE.** An input that omits a value has said nothing that contradicts you;
  if the value is something the layer already knows, supply it. Refuse only a DISAGREEMENT, where
  two sources make incompatible claims and you cannot tell which is wrong. **Before writing a
  refusal into a floor, name the legitimate input it rejects** — if you cannot, you have not
  looked; if you can, that input is the test

Episodes for the last three bullets, and the four accidents in full: `references/round-conduct.md`.

**Mid-round mutation is where the round-0 rules actually bite, and they read as if they do not.**
Everything under "Before you hand it over" is written for the round-0 sweep, but you will mutate
by hand in every round after it — to reproduce a finding, to check a fix, to see whether a
reviewer's mutant is real. The two that cost the most are the two easiest to read as round-0-only:
**never revert with `git checkout -- <file>`** (it deletes uncommitted work, and is a silent
no-op on an untracked file), and **run the baseline before trusting a green result**. PR #107 hit
the first in round 2, on the fix commit's own tests — four uncommitted test edits destroyed, redone
from context, an hour lost — with the rule sitting in this file in those words. `cp` the file to a
scratch path first and restore from that, or mutate in a worktree; the discipline is the same at
every point in the loop, not just at round 0.

**Reproduce a finding yourself before classifying it.** Real / false positive / residual /
**real but out of scope** (below) are decided only with a record of a reproduction you ran. **The
route the reviewer stated is a claim, not the reproduction** — a route you cannot drive is not a
route, and a finding whose route was reconstructed by reading is the shape "verify a reviewer's
POSITIVE claims by asking what it EXECUTED" below is about. Treat
"the implementation is right but the test is weak" as real. (`atmofab-enforcement-change` judgment
rules 1 and 1-b own the residual / unreachable half and state what a "record" has to be there;
the false-positive and out-of-scope classes are this skill's own, below. This line is the round's
trigger, not a second statement of the rule.)

**Verify a reviewer's negative claims the same way.** In PR #66 a sweep reported "only the token
ratchet kills this hunk"; a test in another file caught it correctly and was simply outside the
sweep's gate. **When told "only X caught it", check whether the files holding the other guards
were inside that reviewer's test command.** If not, it is a report about the measurement scope.

**And verify a reviewer's POSITIVE claims by asking what it EXECUTED.** A "verified true" is
worth exactly the run behind it, and a reviewer that traces a code path instead of driving it
will report MATCHES on a claim that is false. PR #107, twice in one round: the mechanical axis
read `run_gate` top to bottom and confirmed "every refusal invalidates the copy" and "callee
raises are covered a fortiori" — both false, because two guards sat above the invalidation and
an ownership guard returned silently, and both were found by the reviewers who RAN the refusals.
**The tell is a verdict justified by structure** ("X runs before Y, so Y is covered") **rather
than by an observation** ("I called it with a blank token and the file survived"). Ask for the
command; a claim that cannot name one is a reading. This is not a reason to distrust the
axis — the same reviewer refused two false premises I had planted in its own checklist in the
same run — it is a reason to re-run the positive verdicts your change actually rests on.

## Delegate verifiable work to sonnet

**Operational conclusion (14 data points; the confound resolved in PR #72 by giving both models
the same checklist, the axis run as delegated in PR #88): sonnet ⊂ opus, with real misses.** Move **the mechanical-recomputation axis**
permanently to sonnet and keep judgment on the up-model. Costs came out roughly equal, so "it is
cheap, so run more" does not hold — **use it only to free up a slot**. Run one via `Agent` with
`model: "sonnet"`, **in parallel** with the up-model reviewers; it adds an axis rather than
replacing one.

**Work you may delegate (verifiable = running it settles the truth)**: re-measuring every number
in the diff **and reporting mismatches**; back-checking "recorded in X" / "pinned by Y" /
"covered by Z" (grep for existence, then **delete what the test is ABOUT and see the test fail**
— deleting the test itself USUALLY proves nothing, since removing a passing `unittest` method
usually cannot turn another one red, and a reviewer who reads that instruction literally will
report the whole axis as vacuous. Not always, though, and the exceptions are worth knowing: two
rows here DO notice a deleted sibling — `AgentRoleFailClosedTests::test_this_class_census_is_accurate`
compares an in-class census against `dir(type(self))`, and `SkillReachabilityTests._run_row`
constructs a test by name); a correspondence
table of whether each new failure class has a test; counting the call sites that make the same
decision; contradictions between prose and implementation.

**Work you must not delegate**: open-ended "find bugs" (plausible noise grows and **I** pay the
triage), and layers needing a hypothesis → mutate → run cycle (gate semantics, parsers, offset
arithmetic).

**Make the prompt a checklist**, not free-form, and hand over the same ground rules as above.
**This conclusion holds only inside the axis delegated** — "sonnet matched opus at recomputing
numbers" does not give "it matches on parser semantics". **Measure per axis.**

**Tell it to report claims it cannot locate rather than accounting for them** — refusing a false
premise I had put in its prompt is the most valuable thing this axis has done — and the most
reproducible, having now happened in seven of the fourteen data points (5, twice on PR #107, on
issue #142, on PR #116 — where it reported that the ten-item mutant list a commit message referenced
was recorded nowhere it could find, true, and the thing I would least have checked myself — on
issue #149, where it refused a checklist item of mine asserting a sweep was vacuous, by mutating
the subject rather than the test and getting RED, and on issue #153, where a checklist item of mine
attributed a "pure and NEVER raises" docstring to the wrong function: it reported the premise as not
locatable, went and checked the functions that DO make that claim, and found one that could raise).
**That last one is the shape to design the checklist for.** The item was wrong about WHERE the claim
lived, and a reviewer that answered the question as asked would have returned "not applicable"; what
made it pay is the instruction to report an unlocatable claim, which turned a defective checklist
item into the round's live defect.
**This stays an experiment**: collect real/total findings, elapsed time and the overlap count,
add a data point each time, and delete this section if it stops paying. How to read overlap, how
not to confound the comparison, and why the reverse (opus reviewing sonnet's implementation) is
not an experiment at all: `references/sonnet-delegation.md`.

## Exclusion lists (the most important section)

As rounds accumulate, it gets tempting to hand over a list of "already reported, do not repeat"
and "accepted residual". It saves tokens and **propagates your own errors with it**. PR #51's only
P1 survived five rounds inside that list (two reviewers had found it, and an unverified premise
had dropped it into residual).

- **Your own RESIDUE list rots the same way, and nothing above covers it.** A published "what this
  does not catch" list is read as a completeness claim — `docs/RUNBOOK.md` pointed an operator at
  one — so re-verify every entry each round, in BOTH directions: an entry that is no longer residue,
  and a route the list calls covered that is not. On PR #98 the list omitted its largest member for
  two rounds and asserted the interpreter route was covered for three, and round 5 established that
  route was the widest gap on the branch
- Hand over an exclusion list **for at most three rounds**
- From round four, **run at least one reviewer with no exclusions**: "you get no history — attack
  from scratch"
- Above all, **never put anything you classified as residual or unreachable into the exclusion
  list**. That is exactly the dangerous part

## When to bring in Codex (budget: once per branch by default)

**Codex's token budget is scarce. One launch per branch as a rule, two at the most.** Do not launch
in round 1 — round 1 findings are the coarse layer subagents also produce, and spending the launch
there **leaves nothing for the moment independence pays most: after your own fixes have piled up**.
Launch once **in round 2** (do not save it for the end). Round 3 is not an equal alternative under
the budget above: the launch has to leave a round behind it for its own findings' fixes to be
reviewed in, and at the default of round 0 plus three, only a round-2 launch does.

**Cases where not launching is better** (spend a blank-slate subagent review instead): a change
that **adds checking machinery** (Codex structurally almost always finds "one more construct", so
clean never comes back and it is not a convergence signal — use it once as a source of test
cases, or not at all); a prose / doc-centred diff; a HEAD whose
previous findings are still unfixed.

**`/codex:review` and `/codex:adversarial-review` are `disable-model-invocation: true` — I cannot
launch them**; the command bodies are one line of the companion script, so call that directly.
The mechanics, the flags and the failure modes are in `references/codex-episodes.md`. The rules
that decide what you do:

- **Prefer native `review`; `adversarial-review` stalls more often** (measured), even though only
  adversarial takes focus text. **Run native once first even when you want to narrow the focus**
- **Never wrap it in `timeout`; `--background` does not detach.** Let the harness's background
  execution do the waiting; add no polling of your own
- **Check the base before launching** — on a merged branch `origin/main` is your own merge commit
- **Before suspecting a stall, check whether you killed it** (commands cut off at one instant =
  an external kill; a true stall shows `phase` not advancing **and no commands accumulating**).
  **Treat the same phase for more than 15 minutes as a stall and cancel**
- **`result <job-id>` returns `No job found` before completion** — take in-flight information from
  status's `Progress:` and **the launch command's stdout**, where a partial verdict sometimes
  appears. **Read the output up to the stall**
- **Do not count a stall as clean** (same for a filter drop) — but **stalls are intermittent**, so
  do not conclude "Codex cannot be used on this branch". The two-launch cap is a budget, not
  evidence of quality; decide a third by how large the remaining doubt is. If you stop, **write in
  the PR / TODO that this branch could not use Codex as a convergence signal**
- **It can be dropped by the content filter** — engineering-flavoured phrasing gets through, limit
  the target files explicitly, ask for counterexample construction rather than attack. **One
  rewrite-and-retry at most**

**Use it as one independent pass on another axis, not as a convergence signal**: not "run until
clean" but "pass once over the surfaces subagents structurally do not look at" — dependencies,
preflight, execution policy, consistency with repository conventions. It fails
differently from a subagent because **it does not share the premises you handed over** —
subagents get the diff and the threat model, Codex gets only `AGENTS.md`. **So do not over-brief
Codex on history either.** **Interaction with
elements that come from anywhere other than "the input I built"** is the subagent blind spot it
covers (PR #72). **Do not expect a clean axis split** — L174 split cleanly, PR #66 did not
reproduce it and one of its two runs fully duplicated a subagent's finding.

## Fix or out of scope (cut by this repository's purpose)

**Reviewers always report down to the details. Fixing everything is not correct.** Beyond real /
false positive / residual, keep an explicit fourth class: **real but out of scope**. When you drop
something there, **write one line of justification and do not carry the list into the next round**
(same reason as exclusion lists — and if reachability is unverified, do not drop it, run the
reproduction).

**`AGENTS.md` §"Development premises" is canonical for the model** — what this repository is, the
two defended classes, and the decision criterion. It moved there because Codex gets only
`AGENTS.md` (see "When to bring in Codex"), so the independent pass was running with no premise at
all. Do not restate it here; what this section holds is what a REVIEW does with it.

**The leaf is not malicious and does not hold back. Those are one premise, not a middle.** The
reviewer's question is never "could a leaf do this" — a capable model under a goal can do nearly
anything — but **"does doing it get the leaf closer to reporting its task DONE?"** That is the
cut, and it runs both ways:

- **On the shortest route to done** — a `leaf shortcut` (`docs/GLOSSARY.md` §12): an assertion
  loosened until it passes, an expected value hardcoded, a check recorded as run, a `gate`
  satisfied by editing it, an unmeasured value written down, a past artifact read as input. **In
  scope**, and the leaf will spell it however it has to: "a leaf would not phrase it that way" is
  no defense here, because reaching done is the motive that makes it phrase it that way
- **Buying the leaf nothing toward done** — the operator's credentials, a read outside the
  checkout, another `orchestration`, anything outliving the run. **Out of scope however real the
  mechanism**, and saying so **costs no reproduction**: name what the leaf would gain, show it is
  nothing, stop. That permission is narrow and is not rule 1's — `atmofab-enforcement-change`
  rule 1-e states the two apart

**Both halves have a recorded failure, in opposite directions, so apply both** (episodes:
`references/class-descent-log.md` §"The gain cut").

**Fix (directly serves the purpose):**

- **a wrong verdict** — a violation passed, or a correct artifact failed. `gate` / `validator` /
  `judge` fail-opens are here
- **a broken artifact contract** — a schema or invariant of the IR, `verdict.json`, or a sidecar
  not upheld
- **false evidence** — recording something as run that was not, breaking the audit trail, a commit
  message / PR / doc disagreeing with the artifact
- **loss of reproducibility** — `--resume` breaks, non-determinism enters a verdict
- **a `leaf shortcut`** — anything letting a leaf report done without earning it. It subsumes the
  four above whenever a leaf is what reaches them
- **a hole an `LLM` leaf hits with ordinary spelling** — a shape that exists in the real corpus
  (this tree's spec and generated artifacts). For a shortcut, ordinary spelling **also covers
  whatever spelling reaches done**

**Do not fix (declare out of scope and stop):**

- **anything a leaf gains nothing from** — the operator's credentials, a read outside the checkout,
  another `orchestration`'s state, persistence past the run. No route through one of them ends at a
  green verdict. **Check the gain before the mechanism.** Mind the near miss: reading a PAST
  ARTIFACT is on the other side of this line, forbidden by `docs/workflow/WORKFLOW_CORE.md`
  precisely because it shortens the route to done, so "a read the manifest does not grant" is by
  itself neither verdict
- **defenses against paths only the operator can reach** — hand-crafting invalid argv, editing
  files to bypass a gate. The operator owns the machine
- **handling constructs that occur zero times in the real corpus** — future forms an adversarial
  reviewer wrote to break the rule. Declare the scope: "this is regression prevention for ordinary
  spelling, not enforcement against someone trying to circumvent it". **This is a cheap proxy for
  the gain test, not a second rule**: one `grep` decides it, so reach for it first, and when the
  two disagree the gain test governs — a shape absent from today's corpus is still in scope when
  taking it shortens a leaf's route to done
- **generalization for future extension** — unnecessary while there is no second caller
- **optimization that measurement shows is not the bottleneck** — the bottleneck is thinking, not
  Python
- **style and wording preferences** — but **errors in descriptions a reader relies on** get fixed,
  on the "false evidence" side
- **rough edges in existing behaviour this PR does not touch** — file them in TODO.md and hand
  them over. Pulling them in compounds "fixes calling for fixes"
- **the SIBLING file a reviewer noticed has the same gap** — when the gap is structural but your
  EVIDENCE does not reach it, the gap is the honest thing to leave. PR #100 took a reviewer's
  "this other sample omits the same field" and wrote the rule into a second provider's sample on
  the strength of the omission being identical; the guidance was not, and the next round found a
  wrong field name, the rule's own censoring principle inverted, and a warning about a code path
  that cannot execute. The whole edit was reverted. **The test is not "is the gap the same" but
  "does my measurement cover the sibling"** — if it does not, say in the PR that the gap is
  deliberate and belongs to whoever measures it

**When the change ADDS A REFUSAL, read the remedy texts of the neighbouring guards.** A sibling's
remedy can steer the refused party straight onto the route you did not close, and then the hole is
not in your rule but in what the surrounding system tells someone to do instead. On PR #98 the new
refusal's own residue was "a writer inside a script FILE handed to an interpreter" — and
`forbid_python_inline_write`'s block text says "use a real script file (`python3 script.py`)", over
an allow-list entry that permits exactly that and a leaf-read document that teaches it. Three
sources agreeing on a route none of them guards is not something any single-file review sees.

The route the reviewer handed over is what this section triages against — check it, do not
rebuild it, and when a finding arrives with no route, treat it as the reviewer's own
`route not established` entry: reproduce it or declare the scope, never fix it on the strength of
the mechanism alone.

**Two questions when in doubt**, both of which must answer yes: "if this stays unfixed and one
`workflow` runs, does a **wrong certification** or a **false record** come out?" and "**does a leaf
get closer to reporting its task done by taking it?**" If either is no, out of scope is fine — and
when the second is the no, say what the leaf would have gained. Write the count and
the breakdown of what you dropped into the PR body / your report to the user — never drop silently.

## Stopping conditions

**The budget comes first. Every condition below is a property of the FINDINGS, and none of them
bounds the cost** — which is why no recorded loop was ended by one of them alone.

**Round 0 plus THREE rounds is the default; FIVE rounds is the cap.** At the cap you stop, whatever
the class did.

**The default is three because of where Codex lands.** The launch goes in round 2 (see "When to
bring in Codex": not round 1, which is the coarse layer, and not the end), it is the pass that
structurally sees what subagents do not, and **what it finds then has to be fixed — so round 3 is
the round that reviews those fixes**. Ending at round 2 would ship the answers to Codex's findings
unreviewed, against this loop's strongest recorded regularity: **most findings sit inside the
previous round's fix**. Round 3 is not slack in the budget; it is the round Codex's launch creates.
A change that fixes existing machinery, or one where the Codex launch is deliberately not spent,
often closes at round 0 plus one or two — spend fewer than the default when the reason is that
kind, never to reach a deadline.

What counts: every round counts, the disclosure round and the census round included — the
disclosure round is one of the budgeted rounds, never an addition to them. A Codex pass rides
inside a round and is not a round of its own.

**What the cap ends is the SEARCH, not the REPAIR.** An in-scope finding already on the table at
the cap — a `leaf shortcut`, a wrong verdict, a broken contract, a false record — is fixed before
the branch goes anywhere. **The budget never ships a known one**, and a fix commit answering a
finding you already hold is not a new round. What stops is looking for the next finding.

**Rounds past the cap do find real defects. The budget is a decision to pay that cost, not a claim
that nothing is left** — writing it down the second way would be false, and recorded false: issue
#71's round 15 found five defects in a committed measurement script, two of them functional, and
PR #72's third Codex pass found what four subagent rounds had missed. What the budget weighs
against them is the work not being done meanwhile (`references/class-descent-log.md` §"The budget").

**Stopping at the cap is not convergence. Say which condition you did not meet**, in the PR body
and to the user, and never write "converged" for it.

**The remainder goes into the PR body as a disclosure, not into a new issue.** Filing it as an
issue reads as closure while the backlog is what actually grew; measured, every open issue on this
repository is infrastructure spun out of a review loop. File an issue only for work someone has
decided to do, not for a remainder nobody has. This is about a ROUND's remainder and leaves the
residue convention alone: a rough edge in existing behaviour the PR does not touch still goes to
`TODO.md` under "Fix or out of scope" above.

**Reaching the cap with something in scope still open is a signal about the CHANGE.** Do not
answer it with a sixth round — split the branch, narrow the rule, or hand it over with what would
have to be built to make a strong claim. The sign below ("five rounds without the class descending
→ the shape of the rule is wrong") is the same reading arrived at from the findings' side.
**Past the cap, adding a round is the USER's decision**, put to them with the count, what the last
round found, and what you would spend the round on.

**The main condition is "class descent plus a demonstration that the remainder is bounded".** Stop
once the severity class of the findings has dropped **and** you can show the remainder is bounded.
Read class descent as **"is it a hole in the original design / a hole in my fix / a hole in the
witnesses"**, not as a count — in issue #63 the count barely fell between the last two rounds.

**A third shape, from a loop that ended on prose alone: once every remaining finding is "this
prose asserts something unmeasured about code the PR does not touch", the fix is to DELETE the
claim, not to write a test proving it** — a PR is not obliged to characterise behaviour it left
alone (issue #40 / PR #41; `references/class-descent-log.md`).

**A second stopping shape, equal in standing: if every finding in one round falls into "real but
out of scope", stop at that round.** More rounds produce the same layer, and fixing them moves the
diff away from the purpose. State the out-of-scope breakdown and why you declared the scope.

**The superior condition (stop there if you reach it)**: the security axis produces **nothing new
for two consecutive rounds** (a disclosure round is skipped in that count rather than breaking
it). It has **never been achieved in any recorded loop**. **Run assuming you will not reach it** — do
not add rounds waiting for it.

**Do not make "Codex is clean" a stopping condition** — as a condition it becomes a motive to
relaunch a Codex you have no budget for. Clean has come back twice, and BOTH times the same round's
subagent work was not: on PR #67 two subagents produced unwitnessed mechanisms, over-refusals and an
abandoned mirror, and on issue #153 the blank-slate subagent sharing that round returned four
findings, one of them a guard whose test family could not fail. On TODO:269 Codex was clean in round
2 and round 3 found a mechanism with no behavioural witness at all.

**Practical proxies for "the remainder is bounded"**, both of which must hold:

- **a reviewer with no exclusions returns zero functional defects** — only prose and consistency
  findings remain
- **an independent mutation sweep is fully killed** (one the reviewer built, not one you ran)

**But the two are not equal in standing.** For changes that **fix existing machinery** both apply.
For changes that **add checking machinery the sweep is not grounds for stopping**: it answers "is
what I built pinned", not "is what I built enough", and **can only mutate mechanisms that exist,
so a missing mechanism is structurally invisible**. There the proxy becomes **a blank-slate review
with zero functional defects for two consecutive rounds**; still run the sweep, but read it as a
list of "unpinned but correct behaviour".

**"Only prose remains" is a claim about SEVERITY, and it is wrong whenever the prose is executed
by someone.** Classify each remaining finding by audience and consequence, not by whether it is
code:

- **Text a leaf or an operator ACTS ON is behaviour delivered as prose** — refusal messages,
  remedies, the leaf-read contract, the runbook step for a failure mode. Treat a defect there at
  the severity of the action it causes
- **Text a maintainer reads to decide** — a residue entry, a justification comment, a measured
  number — is descriptive, and belongs in the bounded remainder
- **Text only a maintainer reads gets no ROUND of its own**, which is a statement about the budget
  and not about whether it is fixed. `TODO.md`, `references/`, a skill file, a commit-message body:
  correct a defect there in the commit that notices it — "false evidence" keeps it in the fix list
  — but it never advances or resets a stopping condition and never justifies spending a budgeted
  round. These are the files where a round can always find one more thing, and measured over one
  month `TODO.md` alone took more than an order of magnitude more commits than `spec/` did
- The tell that you are in the first category: the sentence contains an imperative, or names a
  condition under which something is refused

**Also name any script, harness or fixture generator the branch committed as review surface** —
"zero functional defects" otherwise means "zero in the files anyone looked at" (issue #71's round
15 found five defects in a committed measurement script, two of them functional).

**The move that finds them: spend one round on the disclosure axis alone**, with no functional
brief, **before stopping rather than as an extra round after deciding to stop**. Two briefs:
"verify every claim in the commit messages at HEAD" and "read it as the next maintainer: what
would mislead you, can the deletion's measurement be re-taken from what is written, **what CHECK
went with what was deleted**, what does a LEAF see, what does an OPERATOR see, would you merge". If
it returns first-category items, the record has not converged even though the enforcement code has.

**The added clause is not a flourish.** On PR #125 it is what surfaced the branch's only blocker —
a check `origin/main` had that a round-2 fix narrowed away — and no other instrument in this loop
could have: the sweep mutates what exists, the census enumerates what exists, and a blank-slate
reviewer reads HEAD. **Everything else compares HEAD against itself.** The disclosure round is the
one place a reviewer is pointed at the previous revision, so it is the only place a deleted
guarantee is visible.

**For a change that adds checking machinery, run a witness census once.** Instruct a dedicated
reviewer:

> Enumerate **every decision** in the checking machinery (predicates, constants, each table entry,
> each regex branch, globs, exclusions, file-format assumptions, every assertion added), and
> classify each **by execution** as **witnessed** (a constructed input witnesses it) /
> **corpus-dependent** (green only because today's tree happens to contain no violation) /
> **vacuous** (already observing nothing). Classification by reading is not allowed. For the
> unwitnessed ones, construct a violating input yourself and report whether the suite notices.

Practical notes on reading a census: keep **"killed only by the token ratchet" as a fourth
class** (empty while the ratchet is frozen, issue #182, unless the census ran `--check-baseline`
explicitly and says so); **a vacuous finding may be closed by marking rather than deleting**; **aim "does it wrongly refuse
legitimate work" at the instrument too**; **claim vacuity only by construction** — a corpus
measurement does not prove it; **a census conclusion rots in one round, so re-run it the round
after you consume it, recording the conclusions that survive re-measurement rather than the
numeric breakdown**; **when you replace an enumeration with a computation, witness the
computation on a synthetic tree** (cf. `tools/tests/test_backend_boundary.py::ScannedSetTests`).

**Before concluding "the shape of the rule is wrong" from recurrence, measure inherited decisions
separately from decisions the last fix added.** Inherited got worse → a problem of shape (stop and
hand over); concentrated in what the last fix added → a problem of habit (write the witnesses and
it closes).

**Another proxy: does the finding exist in the real corpus?** **If every finding in a round is a
construct that occurs zero times in the real corpus, what remains is not implementation but a
written scope declaration.** Measure the count per finding class with one `grep` each round.

**Look at `ListAgents` before judging a round finished** — as part of the stopping decision, not
only as orphan hygiene. I once issued a "merge recommended" without checking which reviewers were
still running, and the report that arrived afterwards carried a real defect.

In every case, **finish with one convergence judgment**. The question is not "any new findings" but
**"is any finding left that would change code, tests, or a description a reader relies on?"** If
only preferences remain, have it recommend stopping explicitly.

When you stop short, **state the condition you did not meet** and hand it to the user. Do not say
"converged". **Put the class transitions and the defects your own fixes introduced into the PR body
as a disclosure**.

The per-PR histories, and the episode behind each census note and most of the proxies:
`references/class-descent-log.md`.

## Signs to catch mid-loop

Each sign is one line plus its criterion; `references/signs-episodes.md` carries the case history
that tells you how it closed.

- **A finding sits inside the previous round's fix** → not a coincidence. **Name the fixed files
  as the focus** in the next round's prompt. The more the change is a move or rename whose body is
  known correct, the more the review is really about **your own fixes** — put that instruction in
  from round 1
- **The fix was to a RECORD, so you verified it by reading** → a record fix carries the same
  defect rate as a code fix, and this is the sub-case of the row above that gets skipped, because
  prose does not look like something you run. Criterion: **the corrected sentence must pass the
  check the wrong one failed.** If the defect was "this key does not exist", resolve the new key
  against the artifact; if it was "this count is wrong", compute the new count; if it was "this
  function is not named", drive the function. PR #146 shipped three in a row — a half-followable
  operator remedy replaced by an unfollowable one (a `failure_analysis.json` key nothing writes on
  that route), a commit message that described the author's uncommitted working tree as the
  previous commit's state, and a "measured" claim repeated from a reviewer's report without
  re-running it. **And the check written to hold a corrected record must observe the thing the
  record describes**: the first of those was pinned by asserting the key's name occurred in a
  module — it did, in a CONSUMER the same sentence says never runs — so the check ratified the
  error (`references/signs-episodes.md`).
  **The sharpest form is a correction you make ON A REVIEWER'S MEASUREMENT**, where the rule you
  already know does not fire: `atmofab-enforcement-change` rule 3 says not to write someone else's
  measurement as your own, and issue #153 broke it there — replacing a correct figure with a wrong
  one, one item after condemning that substitution. **A reviewer's finding arrives already carrying
  evidence, so the correction feels verified before you write it**, and a corrections entry is the
  one place a reader will not re-check. **Re-measure at the commit you are about to NAME, not at
  HEAD**; for a per-commit figure take the whole series in one worktree-per-revision loop, so the
  attribution is visible rather than inferred. **Do not delete a wrong correction — record that it
  was wrong**, or nobody can tell an audited corrections bullet from an unaudited one
  (`references/measurement-records.md`)
- **You have rewritten the same string three times** → the problem is not the rule but the prose
  citing it. Switch to the grep sweep
  (`.claude/skills/atmofab-enforcement-change/references/verification.md`). **Rewriting one
  statement repeatedly is a SWEEP problem, which this row owns; several sites that each state the
  rule is a COUPLING problem, which `atmofab-enforcement-change` rule 3-a owns and states the
  threshold for.** Do not restate its number here — that is the drift this pair is about.
  **A sweep does not help when the sentence SUMMARISES a measurement** — "at the top of the band",
  "above every rate observed" — because there is one site and it is wrong on its own terms. PR #100
  got that sentence wrong in three consecutive rounds, each version written to fix the previous
  one. What closed it was DELETING the summary: state the figures, state what they are being
  compared against, and leave the comparison to the reader. A summary of a spread is a claim with
  no witness; the spread itself has one.
  **And when you do reach for the sweep, sweep the FACT, not the spelling.** PR #125 corrected one
  figure three times, each correction sweeping the wording it had just written, and each missing a
  different phrasing of the same measurement — the fourth spelling sat eleven lines from the third
  and survived four rounds. Sweep for the NUMBER and for what it is a number OF, then read every
  hit; a `grep` for last round's sentence finds last round's sentence
- **A comment or docstring RESTATES a measurement the assertion beside it already carries** →
  delete the restatement; do not correct it. Criterion: could this sentence and the line under it
  disagree? Then they will, and the sentence is the one that will be wrong, because nothing runs
  it. PR #125's four-round figure lived three lines above `assertEqual(..., [])` saying something
  the assertion contradicted, and each round corrected the prose rather than asking why a
  measurement was stated twice. **This is not the "prose that enumerates entities" row**: that one
  says turn prose into a check, this one says the check is already there
- **A term you coined for a tool has appeared in a document as if the system used it** → check it
  against the vocabulary the repository already defines. On PR #100 a reporting script labelled any
  body it could not parse "body is not an event stream", and that phrase was then written into
  `docs/ORCHESTRATION.md` as the run's own classification — where
  `response_not_an_event_stream` is a DIFFERENT thing that fails closed on the first attempt, so
  the document had one leaf both spending a retry budget and belonging to a class that cannot. **A
  new term needs one `grep` against the canonical documents before it ships**, and a tool whose
  output will be transcribed should say what it OBSERVED ("no frames parsed") rather than what it
  concluded
- **Prose that enumerates entities in the code** (test names, call-site counts, numbers of
  readers) → **re-measuring loses. Turn it into a check.** Criterion: should this prose break if
  one test is renamed? Then make it a check (the general form, and when a check is the wrong
  answer, is rule 3-a — this row is the enumeration case of it)
- **Your fix NARROWS a check to stop it over-refusing** → the OLD check is a mutant, and you owe
  it a run against BOTH revisions. Narrowing is the standard fix for over-refusal and it removes
  coverage by construction; what you have to establish is that what it stops catching is only the
  legitimate input. Procedure, two commands: take the defect the old check caught, apply it at
  `origin/main` and at `HEAD`, and require red-then-red. Red-then-GREEN is the finding. PR #125
  narrowed a whole-document version-range identity to one table column and un-pinned the operator's
  own install line with it — drifting `pipx install 'fortitude-lint>=0.8,<0.10'` to `<0.11` left
  HEAD green and `origin/main` red, and no round noticed until the disclosure axis read the branch
  as the next maintainer. **Nothing else in this loop looks backwards**: every other instrument
  compares HEAD against itself, so a check the branch deleted is invisible to all of them
- **Your change REMOVES something from the default run, and you then write a witness for it** →
  the witness is how it comes back. A row that drives the real thing over the real corpus
  re-couples the default run to exactly what the change decoupled, and it reads as extra coverage
  while doing it. This is the previous row's twin from the other side: there a check the branch
  deleted stayed invisible, here a check the branch deleted comes back and nothing says so.
  Criterion, one command: **inject the input the change was supposed to stop reacting to, and run
  the default suite** — green is the property the change claims, red is the finding. **None of
  this loop's instruments can see it**: the mechanism lives in a test file, so the sweep's mutants
  of the production code all still die, and the census counts the row as a witness rather than as
  a coupling. Ask it of every witness written for something the change moved OUT of the default
  run, and put the injection in the next round's prompt rather than asking a reviewer to notice.
  **The fix is injection, not deletion** — give the production function the root or path the
  witness needs and drive a synthetic one; the coverage is real, it just must not be taken from
  the corpus the change stopped reading (issue #182)
- **Your change SELECTS a subset — of text, of behaviour, of an allowlist — and you are choosing
  what goes IN** → the DEFAULT for an element nobody considered is a design decision made once, and
  it decides whether a MISS is loud or silent. Criterion, one question: **if I never think about
  element X, what happens to it, and would I notice?** **The answer decides the polarity; the sign
  does not.** Where dropping X produces a REFUSAL — a gate allowlist, a write root, an import
  roster — drop-by-default is the loud direction and is correct, which is `atmofab-enforcement-change`
  surface 8 and not negotiable. Where dropping X produces silence, keeping unless explicitly
  dropped costs size and nothing else. A third answer is often the best: make an unconsidered X
  REFUSE until someone decides, which is what `_UNSCANNED_ROOT_FILES` does after two files landed
  outside a scan unnoticed. On issue #181 three consecutive rounds found the same shapes — a
  dropped fix direction, a pointer whose antecedent went, a claim outliving its qualifier — each
  time in the items the previous round had not opened, and each time they were fixed one at a time;
  inverting the default lowered the rate and the loop still ended at its cap. **Reach for the
  question at the SECOND round of one class, not the third**: the recurrence is not evidence that
  you are careless, it is evidence that the misses are invisible by construction. This is the
  selection case of "the same family appeared two rounds running" below — that row asks whether the
  rule is placed where a later addition is protected automatically, and the default IS that
  placement. **Inverting a default is a shape change, so the "split everything after that into
  another PR" row below applies to it**
- **A fix changed the shape of the rule** (denylist → allowlist and the like) → split everything
  after that into another PR. If you continue without splitting, **give the user the options and
  ask**
- **Five rounds or more without the class descending** → the shape of the rule is wrong. Change
  the design instead of adding a round
- **You rebuilt the instrument itself and the second one behaved the same** → **do not build a
  third.** Declare the scope and hand it over, naming what would have to be built to make a strong
  claim. **But do not confuse "a third instrument" with "a defect in the second"**: the test is
  whether what broke it was **the shape** or **an implementation bug / a missing witness**. Shape
  → stop; bug and witness → fix
- **The same family appeared two rounds running** → change shape without waiting for a third. The
  criterion is whether you placed the rule **where a rule added later is protected automatically**
  (PR #53 closed it by normalizing ahead of the structural decision)
- **The pin was broken in a different shape every time** → looks like the family sign but **closes
  differently: normalization will not close it.** The pin is in the wrong **place** — the rule has
  no single definition. Criterion: **"can this test claim set identity, or can it only produce
  rejection samples?"** If only samples, stop adding samples and move the definition to one place
- **A mechanism you fixed one round ago is eaten together with the fix** → your fix granularity is
  too fine (PR #53 closed it by moving from two counters to a stack of construct kinds)
- **The same mechanism keeps being broken for three rounds or more** → suspect that **the question
  the rule is trying to answer** cannot be answered at this level of analysis; change to a weaker
  question that can be. **If the simple and the complex version give identical measured diffs, the
  complexity bought nothing.** When the defects are all ONE shape rather than a spread, reach for
  this before the five-round "the shape of the rule is wrong" count below — on PR #98 it was the
  correct read at three. **Changing the question is a shape change, so it is split-or-ask**, and
  **it buys no smaller review surface**: the replacement mechanism drew two more rounds of HIGH
  findings, one of them a fail-open regression against `origin/main`
  (`references/class-descent-log.md`)
- **A reviewer said "it is environment-dependent"** → do not close it with a mock on the test side.
  Ask first what happens in production on that environment (`atmofab-enforcement-change` rule 2
  owns this)
- **You rebuilt the design and tests carrying the old mechanism's name remain** → test names are
  read as evidence that the mechanism is still protected. **Do not delete them; annotate at the
  head of the group what they pin and what they do not**
- **You extracted a predicate to one place and shared it** → **the call sites need their own pin.**
  **The predicate's test and the "the call site writes the expected value" test are different
  things**, and the latter can only be written by reading the artifact
- **A change has "mirrors of the same predicate" and you fixed one** → there can be three mirrors.
  **`grep` first for prose saying "mirrors", "cannot disagree", "same decision"** — mirrors usually
  announce themselves in a comment
- **That mirror's agreement test is written as a reconstructed copy of the real thing** → **it
  cannot see a disagreement structurally.** Extract the real thing as a function and call it from
  both the body and the test
- **A witness test's probe value contains the implemented value as a substring** → the assertion is
  automatically true via another clause (`"cmake"` contains `"make"`). **Assert inside the test
  that the probe has the property it needs**
- **Your assertion searches a TOOL'S OUTPUT for a code, a name, or a marker** → check what else
  that output carries. A linter, a compiler and a test runner all ECHO THE SOURCE LINE under the
  diagnostic, so `assertIn("C122", stdout)` matches the very `! allow(C122, …)` comment whose
  suppression the test is meant to disprove. On PR #116 that made the single behavioural witness
  for a security fix pass with the fix REVERTED, leaving one string equality on the argv between a
  reproduced `leaf shortcut` and a green suite. **Parse the structured form** (the diagnostic
  line's `path:line:col: CODE` shape, or `--output-format json`) **and add the negative control**:
  the same input WITHOUT the mechanism must lose exactly what the mechanism was supposed to keep.
  This is `atmofab-enforcement-change`'s surface 5 — caller-controlled data mixed into a
  classification channel — asked of a TEST rather than of a gate
- **You added a prose pin: construct the document SAYING THE OPPOSITE and run it** → a pin that a
  document mentions the rule is not a pin that it states the rule. On PR #116 three leaf-read
  contracts were held to not CARRYING a forbidden directive and to citing where the rule lives;
  replacing each prohibition with its exact reversal ("… is the accepted way to clear a stubborn
  style finding") passed 1294 tests. **The reversal is the mutation for a prose pin**, and the
  literal you require should be DERIVED from the code (the flag, the constant) so a rename breaks
  both together
- **You measured a family and reported the conclusion** → ask whether the family could have come
  out the other way. **Two checks, and the second is the one that finds things.** (a) Name a member
  for which the measurement could have failed; if you cannot, you measured a family that
  structurally cannot answer, however many members it had — 48 spellings, 8 spellings and 8
  spellings each did this on PR #98. (b) **Is the generated spelling one the ACCEPTING PARTY takes?**
  The accepting party is whatever produces the input in production, and it is often not the code
  under test: for a wrapper table it is bash (`timeout cp a b` parses fine and `timeout(1)` then
  refuses it, so the row is inert), for a detector fed arbitrary argv it is the detector itself, for
  a source-text scan it is Python's grammar. Check (a) cleared a real defect on this repo's own
  corpus that (b) caught — a restatement scan generating one quote style where the neighbouring
  test loops both. This is the generated-family case of **"a hand-built fixture can test a shape
  that does not exist"** above; that bullet is the same rule for a hand-written probe.
  **Carve-out**: a loop asserting a property every member is SUPPOSED to have (`assertTrue(name.strip())`
  over a curated constant) is a future-guard, not a measurement — no current member can fail by
  construction and that is the point. The checks apply when you are reporting a conclusion FROM the
  family. **And the family can generate a degenerate member**: `'verdict.json'` is a substring of
  `'aggregate_verdict.json'` in the same tuple, so the substring sign above and this one compose —
  check members against EACH OTHER, not only against the implementation. The remedies differ
  (`assertNotIn(implemented_id, probe)` vs. widening the family), so name both when they overlap.
  Episodes, including a 13-family sweep of this repository's own tests: `references/mutation-testing.md`

## Finally

Judge whether this skill itself, `scripts/mutation_check.py`, or memory needs updating. If this
loop showed a procedure was missing, gave a false positive, or failed to fire when it should have,
**tell the user** rather than rewriting silently. If you judge no update is needed, say so in one
line.
