---
name: metdsl-review-loop
description: Use when implementation in this repository reaches a pause and review begins, when running subagent or Codex review rounds, when fixing findings and moving to the next round, and when judging whether the loop has converged. Required reading for 「レビューして」「レビュー回して」「指摘を直して」「codex review」「この PR merge していい？」「まだ見るべきところある？」 and immediately after finishing the implementation of an audit finding or an issue. The subject is **review of changes you made**; do not use it for reading existing spec, docs, or implementation to judge whether they are sound (review without a change).
---

# The review convergence loop

What this skill holds is **how to run a review**: round structure, what to tell reviewers,
convergence criteria, mutation checking. If the change touches enforcement machinery (a gate,
validator, hook, or capability), **start `metdsl-enforcement-change` as well** — it owns the
domain-specific traps (dual-read pairs, failure attribution, verification commands) and they
are not duplicated here.

Track record: PR #51 converged after 17 subagent rounds plus 3 Codex passes, and **15 of the
defects had been introduced by the fixes themselves**. What follows was derived backwards from
that breakdown.

**This file carries the rules; `references/` carries the episode each rule came from.** A rule
here that does not obviously apply to your case is answered in its reference file, not by
guessing. `L128` / `L174` name entries of `TODO.md` by the line they sat on when the work
happened (as does `L118` in the sibling skill); the lines have moved — search TODO.md for the
subject, not the number.

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
python3 .claude/skills/metdsl-review-loop/scripts/mutation_check.py \
  --range HEAD~1..HEAD --paths <sources you touched> \
  --test-cmd "python3 -m pytest tools/tests/<relevant file> -q -p no:randomly -x"
# to look at the whole branch at once (three-dot, like the review target: it excludes the
# commits main gained after you branched)
#   --range origin/main...HEAD
```

**Pass `-x` — but only once you know the baseline is green FOR THE TEST COMMAND YOU PASS.**
The exit code decides the verdict, and `-x` stops at the first failure: if the suite already has
one that is nothing to do with your change, every mutant stops there and every mutant reads as
`killed`. That is a false green over the whole run, not a per-hunk slip. In met-dsl the standing
instance is the two path-depth-coupled `ForbidBackendCredentialReadTests` cases, which fail in a
worktree under `/tmp` and pass in the checkout — so the script's own baseline goes red (exit 2)
and tells you, but a HANDWRITTEN sweep in a scratch copy will not. **Deselect the known failures
in `--test-cmd`, or drop `-x`.** The output is otherwise read only to tell a real failure from a
suite that never ran. Hunks run in separate worktrees, `min(cores - 2, 4)` at a
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
- **Run the baseline for handwritten sweeps too** — a stale worktree makes a red baseline look
  like every mutant was killed (PR #67)
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
- **If the change's mechanism lives inside a test file, hunk mutation does not apply** — "nothing
  to check" with a correct base is **not applicable, not a pass**, and `--include-tests` does not
  rescue it (reverting an ADDED test hunk deletes an assertion, so it always survives; a hunk
  that CHANGED an assertion is different — reverting it makes the old assertion contradict the
  fixed code, so it reports `killed` while saying nothing about the code under review). Build
  mutants that kill each decision of the new machinery one at a time
- **Do not handwrite a mutation harness** (PR #53's `str.replace` rewrote all occurrences at
  once, hiding 2 reachable fail-opens). If you must: hit occurrences one at a time, **assert the
  patch applied**, and **re-point a mutant list reused across rounds** — a stale target reports
  as a survivor (PR #86, visible only because the harness printed `PATCH DID NOT APPLY` instead
  of counting them green), and a not-applied patch is a failed run, not a finding. **The script is
  not universal either: one hunk can bundle a pinned and an unpinned change, so follow up at line
  granularity when one rule lives in N places**
- **Never revert a mutation with `git checkout -- <file>`; it deletes uncommitted work too.** Use
  a worktree (the script's default; `--keep` leaves it REGISTERED, and `git worktree prune` will NOT
  unregister one whose directory still exists — `git worktree remove <path>` does) or a `cp` backup
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
    the relationship in the fixture, not to unset the variable
  - **a mechanism that guards the HARNESS cannot be witnessed from inside the suite** — put the
    witness in a subprocess outside the runner
  - **when a comment JUSTIFIES a rule, mutate the property the justification names** — the rule
    has a witness, the property holding it up usually does not
  - **kill enumerations one element at a time** (regex alternatives, keyword tables); checked
    together, a missing element goes unnoticed
  - **generating the probes FROM the constant gives set identity, not a family that can
    distinguish anything.** A table-driven test that builds one input per member is the right
    shape — a member added without a probe gets one — but the SPELLING you generate can be the
    one shape every member survives. Ask of the generated family: *is there a member for which
    this input could not fail?* Then check that the spelling is one the thing under test actually
    accepts. PR #98 shipped three of these, one per round, and each was reported as having settled
    the question it could not reach. Episode: `references/mutation-testing.md`
  - **one test per occurrence of a rule, not per rule** — and **the sharpest trigger is a TWIN**:
    when a change touches one of a matched pair, list the pair, list your witnesses, compare the
    two lists before handing over
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
   - **Recording that a number "was right when written" does not stop the next rot.** Only
     changing the form stopped it

   Episodes: `references/measurement-records.md`.

3. **Run the verification set** and record the measurements. The commands are in
   `.claude/skills/metdsl-enforcement-change/references/verification.md` (suite baseline, ruff
   diff against origin/main, doc size ceilings; its `mcp_call` end-to-end section is for
   enforcement machinery).

4. **Leave the list of surfaces you touched** in the commit message or TODO.md. That is where
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
- **Spell out the process and shared-resource rules**: no unscoped `pkill`; **create only waits
  whose exit condition can be satisfied, and no background polling**; `/tmp` is a shared tmpfs,
  delete the trees you create. Three accidents happened for real, each spilling into other
  sessions. **Handing over the rules is not enough** — all three were in the prompt and broken
  anyway, and I have broken them myself for 5.7 hours at a stretch:
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

Episodes for the last three bullets, and the three accidents in full: `references/round-conduct.md`.

**Reproduce a finding yourself before classifying it.** Real / false positive / residual /
**real but out of scope** (below) are decided only with a record of a reproduction you ran. Treat
"the implementation is right but the test is weak" as real. (`metdsl-enforcement-change` judgment
rules 1 and 1-b own the residual / unreachable half and state what a "record" has to be there;
the false-positive and out-of-scope classes are this skill's own, below. This line is the round's
trigger, not a second statement of the rule.)

**Verify a reviewer's negative claims the same way.** In PR #66 a sweep reported "only the token
ratchet kills this hunk"; a test in another file caught it correctly and was simply outside the
sweep's gate. **When told "only X caught it", check whether the files holding the other guards
were inside that reviewer's test command.** If not, it is a report about the measurement scope.

## Delegate verifiable work to sonnet

**Operational conclusion (5 data points; the confound resolved in PR #72 by giving both models
the same checklist, the axis run as delegated in PR #88): sonnet ⊂ opus, with real misses.** Move **the mechanical-recomputation axis**
permanently to sonnet and keep judgment on the up-model. Costs came out roughly equal, so "it is
cheap, so run more" does not hold — **use it only to free up a slot**. Run one via `Agent` with
`model: "sonnet"`, **in parallel** with the up-model reviewers; it adds an axis rather than
replacing one.

**Work you may delegate (verifiable = running it settles the truth)**: re-measuring every number
in the diff **and reporting mismatches**; back-checking "recorded in X" / "pinned by Y" /
"covered by Z" (grep for existence, then **delete what the test is ABOUT and see the test
fail** — deleting the test itself proves nothing, since removing a passing `unittest` method can
never turn another one red, and a reviewer who reads that instruction literally will report the
whole axis as vacuous, correctly); a correspondence
table of whether each new failure class has a test; counting the call sites that make the same
decision; contradictions between prose and implementation.

**Work you must not delegate**: open-ended "find bugs" (plausible noise grows and **I** pay the
triage), and layers needing a hypothesis → mutate → run cycle (gate semantics, parsers, offset
arithmetic).

**Make the prompt a checklist**, not free-form, and hand over the same ground rules as above.
**This conclusion holds only inside the axis delegated** — "sonnet matched opus at recomputing
numbers" does not give "it matches on parser semantics". **Measure per axis.**

**Tell it to report claims it cannot locate rather than accounting for them** — refusing a false
premise I had put in its prompt is the most valuable thing this axis has done (data point 5).
**This stays an experiment**: collect real/total findings, elapsed time and the overlap count,
add a data point each time, and delete this section if it stops paying. How to read overlap, how
not to confound the comparison, and why the reverse (opus reviewing sonnet's implementation) is
not an experiment at all: `references/sonnet-delegation.md`.

## Exclusion lists (the most important section)

As rounds accumulate, it gets tempting to hand over a list of "already reported, do not repeat"
and "accepted residual". It saves tokens and **propagates your own errors with it**. PR #51's only
P1 survived five rounds inside that list (two reviewers had found it, and an unverified premise
had dropped it into residual).

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
  nothing, stop. That permission is narrow and is not rule 1's — `metdsl-enforcement-change`
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
relaunch a Codex you have no budget for. Clean came back once (PR #67) and the same round's two
subagents produced unwitnessed mechanisms, over-refusals and an abandoned mirror; on TODO:269
Codex was clean in round 2 and round 3 found a mechanism with no behavioural witness at all.

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
would mislead you, can the deletion's measurement be re-taken from what is written, what does a
LEAF see, what does an OPERATOR see, would you merge". If it returns first-category items, the
record has not converged even though the enforcement code has.

**For a change that adds checking machinery, run a witness census once.** Instruct a dedicated
reviewer:

> Enumerate **every decision** in the checking machinery (predicates, constants, each table entry,
> each regex branch, globs, exclusions, file-format assumptions, every assertion added), and
> classify each **by execution** as **witnessed** (a constructed input witnesses it) /
> **corpus-dependent** (green only because today's tree happens to contain no violation) /
> **vacuous** (already observing nothing). Classification by reading is not allowed. For the
> unwitnessed ones, construct a violating input yourself and report whether the suite notices.

Practical notes on reading a census: keep **"killed only by the token ratchet" as a fourth
class**; **a vacuous finding may be closed by marking rather than deleting**; **aim "does it wrongly refuse
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
- **You have rewritten the same string three times** → the problem is not the rule but the prose
  citing it. Switch to the grep sweep
  (`.claude/skills/metdsl-enforcement-change/references/verification.md`). **Rewriting one
  statement repeatedly is a SWEEP problem, which this row owns; several sites that each state the
  rule is a COUPLING problem, which `metdsl-enforcement-change` rule 3-a owns and states the
  threshold for.** Do not restate its number here — that is the drift this pair is about
- **Prose that enumerates entities in the code** (test names, call-site counts, numbers of
  readers) → **re-measuring loses. Turn it into a check.** Criterion: should this prose break if
  one test is renamed? Then make it a check (the general form, and when a check is the wrong
  answer, is rule 3-a — this row is the enumeration case of it)
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
  Ask first what happens in production on that environment (`metdsl-enforcement-change` rule 2
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
- **You measured a family and reported the conclusion** → ask whether the family could have come
  out the other way. The criterion is one question: **name a member for which the measurement
  could have failed.** If you cannot, you measured a family that structurally cannot answer, and
  the conclusion is unsupported however many members it had — 48 spellings, 8 spellings and 17
  spellings each did this on PR #98. It is not the substring sign above: there the single probe
  is degenerate; here every probe is fine and the SET is chosen from one corner. **The most
  dangerous version is a generated probe that is not a spelling the thing under test accepts at
  all** (`timeout cp a b` is not valid bash), because then the test is green on an input that
  cannot occur. The three families and how each was closed: `references/mutation-testing.md`

## Finally

Judge whether this skill itself, `scripts/mutation_check.py`, or memory needs updating. If this
loop showed a procedure was missing, gave a false positive, or failed to fire when it should have,
**tell the user** rather than rewriting silently. If you judge no update is needed, say so in one
line.
