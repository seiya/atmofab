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

`L128` and `L174` below name entries of `TODO.md` by the line they sat on when the work happened
(as does `L118` in the sibling skill); the lines have moved since, so search TODO.md for
the subject rather than the number.

Reference files, loaded when you need them:

- `references/mutation-testing.md` — the episodes behind the round-0 rules
- `references/codex-episodes.md` — the stall / filter case log and the launch mechanics
- `references/sonnet-delegation.md` — the four-data-point experiment log
- `references/class-descent-log.md` — the per-PR stopping-condition histories

## Review target and commit granularity

**The target is `git diff origin/main...HEAD`** (everything the branch has stacked on main).
Show only the most recent commit and you miss the defects the previous round's fix introduced —
in practice **most findings sat inside the previous round's fix**.

**Confirm the working tree matches the commits before handing it over** (`git status
--porcelain` empty). Leave uncommitted changes and the diff the reviewer reads is not the
thing.

**One commit per finding is the default.** Three reasons:

- the mutation check works as-is with `--range HEAD~1..HEAD`
- which fix answers which finding stays traceable afterwards (put the gist of the finding in
  the commit message)
- the next round can be told to look hardest at the files you just fixed

Not committing separately is allowed when you judge it better (several findings in one place
where splitting is unnatural, an experimental check, something you expect to revert
immediately). When you do that, **say in one line why you are not splitting**.

**If you fold a whole round into one commit, the message must describe everything in it.** In
PR #67 I judged splitting unnatural, ran `git add -A`, and wrote a message covering only the
first two fixes — **saying nothing about the other six in the same commit**. That is a defect on
the "false evidence" side (the message disagrees with the artifact), and I caught it myself
rather than a reviewer. Once you decide to fold, **read `git show --stat` before writing the
message**.

**`git commit --amend` for a message-only fix requires an empty index** (or pass `--only`).
`--amend` silently absorbs whatever is staged. In PR #58 an `--amend` meant to fix one false
sentence swallowed a file replacement that happened to be staged, leaving **a commit that
claims work `git log -S` cannot find**. I did not notice until a reviewer raised it as high. If
it is unpushed you can fix it by squashing — and say in the message that you did.

## If the plan staged the PRs, close each stage before starting the next

**Implement stage A → review → fix → merge → then implement stage B.** Do not implement both
and split the history into two afterwards. The review target is `origin/main...HEAD` = **the
whole stack**, so splitting later **misaligns the review unit from the PR unit**.

L174 (2026-08-14) did exactly this: the plan said "PR A = swap the front end (semantics
unchanged), PR B =
visibility (moves verdicts)", and I implemented A and B back to back, split them with `git
branch` + `git reset --hard`, then ran four rounds against the whole stack. **Most defects the
review found were in A's code** — a recursion crash, a forgeable classification channel, a
label-induced over-refusal and a silently renamed grammar, none of which a
"semantics-preserving swap" is supposed to be able to produce — **and every fix commit landed on
B**, so PR A's tip was
"A before review" and **merging it alone would have put the unfixed state on main**. I
closed PR A and folded the stack back into one; the point of staging was gone.

**The judgment**: staging exists so that "a semantics-preserving swap" and "a change that moves
meaning" can be read and reverted separately, so **the moment stage A is not correct on its own,
that purpose is lost**. Recovery is (1) fold into one, (2) move A's fixes forward onto A and
rebase, or (3) merge the stack in order. (2) needs history rebuilt and re-measurement on both
branches, so **keeping the order from the start is always cheaper**.

Merge stage A before starting stage B and stage B's review target is automatically stage B
alone (`origin/main...HEAD` equals stage B's diff). That is the shape the staging was for.

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

**Pass `-x`.** The exit code decides the verdict — the output is read only to tell a real
failure from a suite that never ran — so a killed hunk may stop at the first failure. Hunks run in
separate worktrees, `min(cores - 2, 4)` at a time by default and never more than the hunk count
(`--jobs`); 4 hunks × 805 tests measured 5m52s → 43s. **Do not put a `TMPDIR=` prefix in `--test-cmd`**: the script gives each job its own
temp root, a prefix overrides it and puts every job back on one, and that is the shape that
produces failures belonging to no hunk — recorded as `killed`, i.e. a false pin. At more than one
job it refuses the combination (exit 2) rather than mismeasuring; at `--jobs 1` there is nothing to
share, so it only says so. **Run the un-mutated baseline once first**: an already-red suite makes every hunk
look "killed", and a red baseline exits 2 (`--skip-baseline` removes the check — write down why
if you use it).

The rules below are compact; `references/mutation-testing.md` carries the episodes each one
came from, and the reasoning you need when a rule does not obviously apply.

- **Pass `--continue-on-collection-errors` when a hunk comes back INCONCLUSIVE, and drop `-x`
  when you do.** A mutation can kill pytest during collection, and a scorer reading only `FAILED`
  lines records that as green (PR #68: 3 mutants hid 41-47 real failures). The two flags cancel:
  measured, `-x` stops at the first error, so a collection error can end the run with nothing else
  attempted and the same INCONCLUSIVE comes back. Put both facts in your scorer **and** in the
  reviewer's instructions
- **Run the baseline for handwritten sweeps too.** A stale worktree makes a red baseline look like
  every mutant was killed (PR #67). A sweep interrupted by a timeout never writes the mutation
  back, so rebuild the worktree or always print the baseline line
- **If the change is a move, hunk mutation answers almost nothing.** `--range` cannot reach code
  that moved without changing (PR #68: my 39 mutants had 0 unexpected survivors; a reviewer's
  independent sweep found 11 real unwitnessed decisions). For move / rename / extract PRs, build
  **mechanism-level mutants over every mechanism in the files you touched**
- **Past 50 lines, a hunk hides the decisions inside it** — re-target each judgment individually
- **Survivors** mean no pin, or a neighbouring check killing it. Fix them or write down why not.
Test-file hunks are excluded by default (`--include-tests`
  keeps them). **Nothing else is excluded on a guess about `#`**: it opens a heading in Markdown
  (which this repository pins), a preprocessor directive in the c/cpp families, a shebang, a lint
  pragma, and inside a Python string or a YAML block scalar it is the prompt-template text met-dsl
  pins. Prose hunks are checked and, for Python only, LABELLED by comparing ASTs. One half of a
  code move is expected to survive, so read the pair together — though a move between Python
  modules usually reports INCONCLUSIVE for the halves whose import breaks at collection, so pass
  `--continue-on-collection-errors` first
- **Prose-only hunks are annotated, not excluded** (`[prose-only (comment/docstring) —
  expected]`): comments and docstrings alike are checked, because real tests pin prompt templates
  and contract text. The classification is an AST comparison over Python files, so a `#` line
  added inside a string literal is NOT prose — it changes a constant — and a prose hunk in any
  other file type is reported unlabelled. **An unannotated survivor exits 1, and so
  does any inconclusive or skipped hunk, or a change with no revertible hunk. Exit 0 means a clean
  run, or one whose sole survivors are prose-only (comments or docstrings) — and also a range with
  nothing left to check, which is why the hunk count is what you read. Exit 2 means the run
  itself cannot be trusted** — a red baseline, a baseline that hits `--timeout` (1800s default), a
  range that does not resolve, a `--repo` that is not one, or a `TMPDIR=` in `--test-cmd` with
  several jobs. In PR #67, 2 of 5 survivors were docstrings listed
  beside 3 real defects.
  The annotation is decided mechanically by AST comparison (blank the docstring, is it
  isomorphic), so **a hunk that also carries a code move is never annotated** and stays an
  unmarked SURVIVED — the absence of the annotation is not evidence that a hunk is code
- **Get `--range`'s base wrong and "no hunks in range" looks green** — it exits 0, because a
  range that resolves to nothing is not a failure. Check that the output states a hunk count. The
  causes: a base that resolves but is wrong (after a MERGE `origin/main..HEAD` is empty; after a
  REBASE it is not — it is your own commits, replayed), a `--paths` that matches nothing, and a
  round whose diff is all test files. A change with no revertible hunk — a pure rename, a binary
  file, a mode change, an empty new file — is listed by name and exits 1 whatever else the run
  found, because nothing was tested for it
- **If the change's mechanism lives inside a test file, hunk mutation does not apply.** "Nothing to
  check" with a correct base is **not applicable, not a pass** (PR #58). `--include-tests` does not
  rescue it: reverting an ADDED test hunk deletes an assertion, so it always survives. (A hunk
  that CHANGED an assertion is different — reverting it makes the old assertion contradict the
  fixed code, so it reports `killed` while saying nothing about the code under review.) Build mutants that kill each decision of the new machinery one at a time
- **Do not handwrite a mutation harness.** PR #53's produced three real harms, the worst being
  `str.replace` rewriting all occurrences at once so per-site mutation showed 2 of the 3
  surviving, both of them reachable fail-opens. If you must, **count the occurrences and hit them
  one at a time**, and **assert the
  patch applied** — a mutation that did not apply is indistinguishable from green (PR #76).
  **A mutant list REUSED across rounds goes stale as the source moves**, and then reports as a
  survivor: on PR #86 three "survivors" in one sweep were stale target strings, visible only
  because the harness printed `PATCH DID NOT APPLY` instead of counting them green. Re-point them
  before reading the result, and treat a not-applied patch as a failed run rather than a finding. The
  script is not universal either: one hunk can bundle a pinned and an unpinned change, so **follow
  up at line granularity when one rule lives in N places**
- **Never revert a mutation with `git checkout -- <file>`; it deletes uncommitted work too** (done
  twice on the issue #63 PR, once losing an uncommitted P1 fix). Use a separate worktree (the
  script's default; with `--keep` those worktrees stay REGISTERED in your repository, and
  `git worktree prune` will NOT unregister one whose directory still exists — `git worktree remove
  <path>` does) or a `cp` backup
- **Mutation checking cannot detect a test spinning in neutral.** A live-but-unobserved hunk looks
  like a pass (L128: deleting the scope mechanism outright kept everything green and all 5 tests
  meant to pin it were inert). The countermeasures, each with its episode in the reference file:
  - **run one mutation that deletes a whole mechanism** (pass-through `return`, constant condition)
  - **a mutation list you build carries your own blind spots** — a layer you did not recognize as a
    mechanism is never mutated (L174: an offset-translation layer replaced by the identity kept 825
    tests green). Hence the standing instruction to reviewers to build their own; **do not hand
    over your list**
  - **doubt once why a test passed**, especially when it felt obvious: until every other path in
    the fixture that could produce the expected violation is removed, it does not pin its name
  - **a negative assertion is green when the detector breaks — self-test the detector** by feeding
    it one string that must be flagged and one that must be admitted, with the rule defined once
    and called from both sides (PR #76)
  - **when a mutant dies, read why**: a kill from a setup error is worth exactly as much as green
    (PR #57, twice)
  - **when you rewrite a test, diff what the old version observed** — PR #57 deleted the only
    witness of a mechanism it deliberately kept
  - **a test that reproduces the wiring does not observe the wiring**: if it does not call the
    production entry point, it pins "the argument is forwarded", not "that value is chosen" (issue
    #63). **And that shape lies in its docstring easily** — grep the body for the function names
    the docstring claims to drive
  - **kill enumerations one element at a time** (regex alternatives, keyword tables); checked
    together, a missing element goes unnoticed
  - **a test can pass because of the SUITE'S OWN ENVIRONMENT.** Distinct from "two paths to the
    outcome": here the fixture is fine and `conftest.py` is what decides the verdict. On PR #86 a
    session fixture redirected `METDSL_WORKFLOW_HOMES_ROOT` for every test, so a test asserting
    which protected root a path falls under was reasoning about a nesting that did not exist while
    it ran — green under pytest, **failing under `env -u <VAR> python3 -m unittest <dotted.path>`**,
    which is the production resolution. Two tests on that branch had it.
    - **The tell**: the test reasons about a RELATIONSHIP (this path is under that root, this id
      matches that record) whose two halves are not both built by the fixture
    - **The check is one command.** Run the branch's new test classes both ways and diff the
      verdicts. Cheap enough to be routine when conftest touches the environment at all
    - **The fix is not to unset the variable — it is to build the relationship in the fixture**, so
      the test asserts the workflow's answer instead of the harness's
  - **a mechanism that guards the HARNESS cannot be witnessed from inside the suite.** If the same
    protection also comes from `conftest.py`, every mutant of it is green there, and the thing it
    prevents happens only where conftest is not loaded. PR #86's module-level redirect — added
    after two reviewers wrote real directories into the operator's home — survived every mutation
    until the witness left the process: **a subprocess running a dependent class under plain
    `unittest` with a fake `$HOME`, asserting the directory never appears.** Reach for this
    whenever the mechanism's whole purpose is what happens outside the runner you are testing under
  - **when a comment JUSTIFIES a rule, mutate the property the justification names.** The rule
    usually has a witness and the property holding it up usually does not. On PR #81 the
    surviving justification for passing `METDSL_*` by prefix was "the names that redirect a leaf
    are outside the prefix BY CONSTRUCTION" — true only because the match is anchored, and
    `startswith` -> `in` kept all 4972 tests green, admitting `MY_METDSL_API_KEY`. The neighbouring
    spelling too: the prefix STRING was separately unpinned, and shortening `"METDSL_"` to
    `"METDS"` stayed green while widening the namespace to one the repo does not own. Read your own
    justification as a list of claims and write one mutant per claim — and note this is the sign's
    other half: rewriting a justification three times (below) is when its supporting property is
    newest and least witnessed
  - **one test per occurrence of a rule, not per rule** (PR #53: the same line in three gates, two
    of them surviving as reachable fail-opens)
  - **for stateful code, match the fixture to the lifetime of the state**, and always include a
    version with one level of syntactic nesting (PR #53: the round after the flat version was
    fixed, the nested version ate the fix)

2. **Do not type measured values by hand; generate them from the artifact you measured.** Keeping
   "a list of every place you wrote a number, re-measured at the end" is not enough — in PR #66 I
   **mistyped it immediately after measuring** (measured suite 4679, wrote 4680). Seven numeric
   errors appeared on that one branch, the last of them in the PR body's disclosure section. What
   worked was **a script that substitutes the numbers in TODO / docs from the measurement
   artifacts**, run and diffed. Leave no path where a human transcribes.

3. **Run the verification set** and record the measurements. The commands are in
   `.claude/skills/metdsl-enforcement-change/references/verification.md` (suite baseline, ruff
   diff against origin/main, doc size ceilings; its `mcp_call` end-to-end section is for enforcement
   machinery).

4. **Leave the list of surfaces you touched** in the commit message or TODO.md. That is where
   reviewers attack from.

## Running a round

**One round = two subagents in parallel**, on separate axes (security-bypass /
correctness+regression+doc-truth).

- **Do not edit until both results are in.** Touch a file while one is running and its findings
  go stale (this happened). But in PR #53 one ran for **78 minutes** and I broke this rule twice.
  **Do not rely on the prohibition; absorb it in the launch prompt**:

  > "**HEAD may advance while you run. Re-verify each finding against the current HEAD before
  > reporting, and state which revision you measured on.**"

  The one report that did this unprompted was the most useful of that round. If you do start
  editing, **write the fact and the time into the next round's prompt**.
- Always in the prompt: **the target is `git diff origin/main...HEAD`**, and **"do not modify the
  checkout; run mutation tests against a `git archive` snapshot or a separate worktree"** (one
  agent left the working tree mutated and invalidated another review's results)
- **A scratchpad per agent.** Spell out: "**create your own subdirectory; do not reuse or
  overwrite existing paths**" (one agent wrote a file with the same name as mine, `mutate.py`,
  and broke the harness). Put your own working files in a subdirectory the same way
- **Hand over this environment's trap**: in an agent session `grep` may be shadowed — measured
  here, a shell function that execs `ugrep` with `--ignore-files`, so it **respects `.gitignore`**
  (a plain interactive shell has the real `grep`; check with `type grep`). Have corpus measurements enumerate with `find` / `os.walk` (1 of
  365 files was visible, and I nearly designed on the false premise that "`interface` occurs 0
  times")
- Write "report only what you ran. Give reproduction steps and file:line. **State explicitly if
  you found nothing**" — this prevents filler
- **What you may hand over to reduce duplication is the axis, not the list.** Saying "hunk
  mutation over the diff range is done and green" moves budget to *sibling rules outside the
  diff* and *mechanism level*; handing over my mutation list breaks independence, naming which
  axes are covered does not. In PR #68 most of the reviewer's sweep was a re-run of my mutants
  (the yield was concentrated outside the diff)
- **Spell out the process and shared-resource rules.** Three accidents happened for real, each
  spilling into other sessions: an unscoped `pkill -f "pytest tools/tests/"` took out my run and
  another session's; a `until ! kill -0 $(pgrep -f "pytest tools/tests")` wait **matched its own
  cmdline** and left four orphans spinning for 45 minutes; agents left 4.6GB in `/tmp` and 14.7GB in
  `~/.cache`, and the next reviewer hit `0 bytes free`. So the prompt says: no unscoped `pkill`;
  **create only waits whose exit condition can be satisfied, and no background polling**; `/tmp` is
  a shared tmpfs, delete the trees you create.
  - **Handing over the rules is not enough. At the end of the round, check for orphans and kill
    them by PID.** All three were written explicitly in the prompt and broken anyway, in the shape
    this skill gives as its example (`until ! pgrep -f mut5.py` matching its own zsh command line).
    Pick them up with `ps -eo pid,ppid,etimes,args | grep -E "sleep|until|pgrep"` and `kill` by PID
    — `pkill -f` re-enacts the first accident. **Before killing anything, confirm no process doing
    real work is alive at the same time**: the orphan and the work it was waiting for look alike
  - **The symptom disguises itself as "the subagent is running and never returns".** I once waited
    on the precedent that long runs happen, when there was no work to wait for. When `ListAgents`
    shows running, suspect that agent's child processes
  - **This trap catches me too.** On the issue #63 PR I forbade the shape to three reviewers,
    killed the one that broke it, and ran four of my own waits in the same shape for 5.7 hours
    (`until ! pgrep -f "mutation_check.py"` matching itself; the real run had finished hours
    earlier). The user noticed. My end-of-round check was `ListAgents` only — **I never looked at
    my own background shells**
  - **A wait must not carry the name of what it waits for in its own command line.** Safe forms:
    (a) for work the harness tracks, **do not poll — the completion notification arrives**, (b)
    **wait on the PID** (`while kill -0 <pid> 2>/dev/null; do sleep N; done`), (c) split the
    matching string. **The end-of-round check is `ListAgents` and `ps`, both**
  - **Evaluate the exit condition ONCE, by hand, before you leave a wait running.** "Can be
    satisfied" is not a property you can see by reading — twice on PR #81 I wrote a
    condition that was false for every possible input, the second being
    `until grep -qE "…" a.txt b.txt | grep -c . | grep -q 2`: `-q` prints nothing, so the count is
    always `0` and no suite result could ever end it. It also survives the thing it waits for:
    the other one watched an output file whose producer I had killed, so it polled a
    permanently empty file. Run the condition once and look at the exit status; if it is already the
    value that ends the loop, the loop is pointless, and if it cannot reach that value it is an
    orphan you have not noticed yet
  - **Two things make the end-of-round `ps` check actually fire.** (i) it is a reconciliation, not
    a memory: keep the PID of every wait you start and match the list, because "did I leave one
    running" is exactly the question a tired reviewer answers wrong; (ii) **a backgrounded wait
    returns immediately** — you get a task id, not a pause — so a turn that launches one and then
    keeps working has NOT waited, and the loop is still out there. On PR #81 I noticed
    that mid-session, said so, and still left two behind for the user to find
- **Include "build your own mutants that delete one mechanism at a time, run them, and report
  survivors".** Do not hand over your mutation list — independence is the point (see L174 above)
- **If the reported HEAD is a hash you do not recognize, find out what commit it is first.** Even
  when you have not edited, the user or another session can commit to the same branch (L174: a
  reviewer reported `d24a7bb`, which was not my commit). That is concurrency, not staleness — read
  it and judge whether it collides with your scope
- **Hand over the threat model and the purpose in one paragraph** (the first half of "Fix or out
  of scope" below): "a single-operator research workflow platform; what is defended against is a
  deviating `LLM` leaf and the defects my own changes introduce; hardening paths only the operator
  can reach, and handling constructs that exist nowhere in the real corpus, are out of scope."
  **Without it the report fills with future forms of details.** But **do not name individual
  findings as excluded** — that is an exclusion list, subject to the three-round rule below

- **For a change that adds checking machinery, include "construct legitimate work that this check
  wrongly refuses".** Ask only for misses and the over-refusals stay. PR #66 hit this twice:
  pinning the **list** of scanned files made **adding one ordinary new module a scope violation**,
  and moving a migration doc to the location the rule specifies reported "knowledge grew in the
  neutral core". Both harm the same way — **they teach the habit of regenerating without reading
  the rule, and destroy the value of the pin**. The criterion is whether the pin is on **the rule**
  or on **the result the rule produced**; pinning results makes ordinary work fail
- **Over-refusal is not a one-off trap; it is my default error direction. Put a probe in every
  round.** Twice in PR #66 and **three more times in PR #67** (five total), and PR #67's three
  **recurred each time I rewrote the same rule**: ① "every record should be implemented" refused
  registering before writing the backend (a state the docs call the default) → ② "every axis has
  one implemented member" **refused the docs' three-step "Adding an axis" outright** (a new axis
  has neither an implementation nor a declarable capability) → ③ extending the duplicate-definition
  guard to dict values flagged a legitimate mapping. **Miss-direction bugs come one per round;
  over-refusal comes in a new shape with every rewrite.** Four countermeasures that DETECT it —
  a fifth that prevents it is the next bullet: (a) for each check
  you write, construct one piece of correct work that violates it, (b) **keep the over-refusal probe
  in the reviewer instructions through the final round** (not once), (c) if over-refusals persist
  after two rewrites, conclude **the rule is not an invariant** and change its shape, (d) **build
  probes from the project's own "what we do next", not from imagination** — in PR #68, widening the
  scan to the whole backend directory refused **the very area the migration ledger names as
  next** (`build_system/make`),
  and a reviewer produced it just by reading the ledger. Implementing the next TODO / plan item is
  the cheapest over-refusal probe there is (PR #67 landed on "a census that constrains nobody's
  registration" = moving the target from a rule to **checking the declaration**)
- **A fifth countermeasure, and the one that actually worked: when you add a FLOOR, default to
  FILL rather than REFUSE.** The others detect over-refusal after you write it; this one stops you
  writing it. Ask what the missing thing IS. An input that omits a value has said nothing that
  contradicts you — and if the value is something the layer already knows (its own arguments, a
  field the record carries), supply it. Refuse only a DISAGREEMENT, where two sources make
  incompatible claims and you cannot tell which is wrong. PR #81 got this backwards first: told
  that a fallback path skipped an id check, I made it refuse, and one exported variable took the
  suite from 4971 passed to 152 failed while production turned the same `ValueError` into
  `fail_closed` — an unreachable stale record traded for a reachable killed run. Rewritten as
  "overwrite the stale value, refuse only a disagreement" it fixed the finding and broke nothing.
  The two later floors on that PR were written FILL-first for this reason, and the second is the
  clearest case: refusing a profile that lacked the ids would have rejected every profile
  persisted before the branch, i.e. broken `--resume` of an older run, while filling them from the
  record's own fields cost one line. **Before writing a refusal into a floor, name the legitimate
  input it rejects.** If you cannot, you have not looked; if you can, that input is the test.

**Reproduce a finding yourself before classifying it.** Real / false positive / residual /
**real but out of scope** (below) are decided only with a record of a reproduction you ran. Treat
"the implementation is right but the test is weak" as real.

**Verify a reviewer's negative claims the same way.** This is the flip side of "verify agent
findings by execution" and is easy to miss. In PR #66 a sweep reported "only the token ratchet
kills this hunk (= the pin is weak)"; checking showed **a test in another file caught it
correctly** — that file was simply not in the sweep's narrow gate. **When told "only X caught
it", check whether the files holding the other guards were inside that reviewer's test command.**
If not, it is a report about the measurement scope, not a measurement.

## Delegate verifiable work to sonnet

**Operational conclusion (4 data points, the confound resolved in PR #72 by giving both models
the same checklist): sonnet ⊂ opus, with real misses.** Move **the mechanical-recomputation axis**
permanently to sonnet and keep judgment on the up-model. Costs came out roughly equal, so "it is
cheap, so run more" does not hold — **use it only to free up a slot**.

Run one via `Agent` with `model: "sonnet"`, **in parallel** with the up-model reviewers. It adds
an axis, so add it to the default two rather than replacing them.

**This stays an experiment: add a data point each time you use it, and delete this section if it
stops paying.**

**Work you may delegate (verifiable = running it settles the truth)**:

- **re-measure every number in the diff** and report mismatches (counts, byte counts, suite
  counts, "M of N")
- **back-checking claims** of the form "recorded in X", "pinned by Y", "covered by Z" (grep for
  existence; delete the test and see it fail)
- **a correspondence table of whether each new failure class has a test**
- **counting the call sites that make the same decision** (marker scans, category classification,
  early returns)
- contradictions between prose and implementation (docstring / doc / SKILL behaviour vs the code)

**Work you must not delegate**:

- open-ended "find bugs" — plausible noise grows and triage gets expensive. Since every finding
  is reproduced by me, low-precision search consumes **my** time
- layers needing a hypothesis → mutate → run cycle: gate semantics, parsers, offset arithmetic.
  L174's three findings of that kind all needed the round trip — the unobserved offset-translation
  layer, an exemption granted by `result`, and the label family

**Make the prompt a checklist** (not free-form). Hand over the same ground rules as above: do not
modify the checkout / a dedicated scratchpad / `grep` may be shadowed and respect `.gitignore` /
report only what you
ran / HEAD moves.

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

The experiment log for all four data points is in `references/sonnet-delegation.md`.

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

Launch once **in round 2 or 3** (do not save it for the end). Its perspective is independent.

**Cases where not launching is better** (spend a blank-slate subagent review instead):

- **a change that adds checking machinery** — Codex structurally almost always finds "one more
  construct", so clean never comes back and it is not a convergence signal. Use it once as a source
  of test cases, or not at all
- the diff is prose / doc centred and changes no mechanism behaviour
- the previous round's findings are still unfixed — spend the launch on the fixed HEAD

**`/codex:review` and `/codex:adversarial-review` are `disable-model-invocation: true` — I cannot
launch them** (they are for the user to type). The command bodies are one line of the companion
script, so **call that directly**; the mechanics, the flags, and the failure modes are in
`references/codex-episodes.md`. The operating rules that decide what you do:

- **Prefer native `review`. `adversarial-review` stalls more often** (measured). Only adversarial
  takes focus text, but the probability of getting an answer back outweighs that. Run native once
  first even when you want to narrow the focus
- **Never wrap it in `timeout`. `--background` does not detach** — killing the launcher kills the
  codex child, and the job record stays `running`, indistinguishable from a stall. **Let the
  harness's background execution do the waiting** (`run_in_background`); do not add your own
  timeout or polling
- **Check the base before launching.** On a merged branch `origin/main` points at your own merge
  commit and you review an empty diff: look at `git diff --shortstat <base>...HEAD`
- **Before suspecting a stall, check whether you killed it.** The tell is the timestamp of the
  log's last line: investigation commands at even intervals that cut off at one instant means an
  external kill, not a stall. A true stall shows `phase` not advancing **and no commands
  accumulating**
- **Treat the same phase for more than 15 minutes as a stall and cancel.** Waiting collapses the
  "do not edit until both are in" rule with it
- **`result <job-id>` returns `No job found` before completion** (a different channel from status).
  Take in-flight information from status's `Progress:` and **the launch command's stdout** — a
  partial verdict sometimes appears there (PR #57's second run emitted
  `Assistant message captured: {"verdict":"needs-attention", ...}` minutes before stalling, and its
  gist matched what a subagent found independently). **Read the output up to the stall**
- **Do not count a stall as clean** (same for a filter drop). **Stalls are intermittent, though:
  do not conclude "Codex cannot be used on this branch"** — PR #72 stalled twice and **the third
  run finished in about 2 minutes with the one defect that four subagent rounds, census and
  convergence judgment included, had all missed**. So **the two-launch cap is a budget, not
  evidence of quality**: whether to stop or draw a third is decided by **how large the remaining
  doubt is**, with the log's command accumulation as the evidence. If you stop, **write in the PR /
  TODO that this branch could not use Codex as a convergence signal**
- **It can be dropped by the content filter.** Engineering-flavoured phrasing gets through
  ("evaluate this static analyser's parsing soundness", not "find the fail-opens"); **limit the
  target files explicitly** so it does not wander; ask for **counterexample construction** rather
  than attack. **One rewrite-and-retry at most** (which consumes the second launch)

It fails differently from a subagent. In PR #51 it caught in one pass what 17 subagent rounds had
walked past — not from capability but because **it did not share the premises I had handed over**.
So do not over-brief Codex on history either.

**Subagents are weak where the premise was handed to them (PR #72).** Codex's single finding was
"if the operator writes the same flag in the `command:` prefix, the conductor appends its own, two
appear in argv, and `argv.index()` reads the operator's". **Four subagent rounds took argv as
"something the conductor builds" and nobody looked at the interaction with the prefix.** The harm
was not only a mis-record but **a fail-open in the very fail-closed check that PR added**.
**Interaction with elements that come from anywhere other than "the input I built"** is a blind
spot for a subagent handed the diff and the threat model.

**The axes sometimes differ — but do not generalize from one case; PR #66 did not reproduce it.**
L174 split cleanly (Codex = environment and operations, subagents = the diff's internal logic),
while PR #66's two Codex runs were both **internal logic** and one **fully duplicated** a
blank-slate subagent's finding. Allocate the single launch believing it is "a net on another axis"
and you will miss. **Expect no split; use it as one independent pass.** The right framing is Codex
as **a net on a different axis, not a convergence signal**: not "run until clean" but "pass once
over the surfaces subagents structurally do not look at" (dependencies, preflight, execution
policy, consistency with repository conventions). That asymmetry also follows from what each is
given — **subagents get the diff and the threat model, Codex gets only `AGENTS.md`**.

## Fix or out of scope (cut by this repository's purpose)

**Reviewers always report down to the details. Fixing everything is not correct.** Beyond real /
false positive / residual, keep an explicit fourth class: **real but out of scope**. When you drop
something there, **write one line of justification and do not carry the list into the next round**
(same reason as exclusion lists — and if reachability is unverified, do not drop it, run the
reproduction).

**What this repository is**: a **single-operator research workflow platform** that generates and
certifies weather and climate kernels from a `spec` — `README.md` §Scope is canonical for what it
builds, while the threat model below is this skill's framing of it —
`docs/design/zero_base_architecture.md` states the leaf half, and the "defects my own changes
introduce" half appears nowhere else. What is defended
against is **a deviating `LLM` leaf and defects my own changes introduce**, not a malicious third
party and not an unknown user population. It is neither a distributed artifact nor a long-lived API.

**Fix (directly serves the purpose):**

- **a wrong verdict** — a violation passed, or a correct artifact failed. `gate` / `validator` /
  `judge` fail-opens are here
- **a broken artifact contract** — a schema or invariant of the IR, `verdict.json`, or a sidecar
  not upheld
- **false evidence** — recording something as run that was not, breaking the audit trail, a commit
  message / PR / doc disagreeing with the artifact
- **loss of reproducibility** — `--resume` breaks, non-determinism enters a verdict
- **a hole an `LLM` leaf hits with ordinary spelling** — a shape that exists in the real corpus
  (this tree's spec and generated artifacts)

**Do not fix (declare out of scope and stop):**

- **defenses against paths only the operator can reach** — hand-crafting invalid argv, editing
  files to bypass a gate. Outside the threat model
- **handling constructs that occur zero times in the real corpus** — future forms an adversarial
  reviewer wrote to break the rule. Declare the scope: "this is regression prevention for ordinary
  spelling, not enforcement against someone trying to circumvent it"
- **generalization for future extension** — unnecessary while there is no second caller
- **optimization that measurement shows is not the bottleneck** — the bottleneck is thinking, not
  Python
- **style and wording preferences** — but **errors in descriptions a reader relies on** get fixed,
  on the "false evidence" side
- **rough edges in existing behaviour this PR does not touch** — file them in TODO.md and hand
  them over. Pulling them in compounds "fixes calling for fixes"

**One question when in doubt**: "if this stays unfixed and one `workflow` runs, does a **wrong
certification** or a **false record** come out?" If not, out of scope is fine. Write the count and
the breakdown of what you dropped into the PR body / your report to the user — never drop silently.

## Stopping conditions

**The main condition is "class descent plus a demonstration that the remainder is bounded".** Stop
once the severity class of the findings has dropped **and** you can show the remainder is bounded.
Per-PR transition histories are in `references/class-descent-log.md`; read class descent as **"is
it a hole in the original design / a hole in my fix / a hole in the witnesses"**, not as a count —
in issue #63 the count barely fell between the last two rounds.

**A second stopping shape, equal in standing: if every finding in one round falls into "real but
out of scope", stop at that round.** More rounds produce the same layer, and fixing them moves the
diff away from the purpose. State the out-of-scope breakdown and why you declared the scope.

**The superior condition (stop there if you reach it)**: the security axis produces **nothing new
for two consecutive rounds**. It has **never been achieved in any recorded loop** — the
nine histories in `references/class-descent-log.md`, plus PR #51. **Run assuming you will not reach
it** — do not add rounds waiting for it.

**Do not make "Codex is clean" a stopping condition.** As a condition it becomes **a motive to
relaunch a Codex you have no budget for**. Use Codex as one independent pass and finish once you
have classified the result. Clean did come back once (PR #67), and **the same
round's two subagents produced unwitnessed mechanisms, over-refusals and an abandoned mirror — so
clean was not evidence of convergence**. Issue #63 is the opposite data point: both completed runs
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

## Signs to catch mid-loop

- **A finding sits inside the previous round's fix** → not a coincidence. **Name the fixed files as
  the focus** in the next round's prompt (PR #51: three rounds running; PR #68: **all six rounds**,
  with zero defects in the moved code itself and everything in the fixes and the prose). The more
  a change is a move or rename where the body is known correct, the more the review is really about
  **your own fixes** — put the focus instruction in from the first round
- **You have rewritten the same string three times** → the problem is not the rule but the prose
  citing it. Switch to the grep sweep
  (`.claude/skills/metdsl-enforcement-change/references/verification.md`)
- **Prose that enumerates entities in the code** (lists of test names, counts of call sites,
  numbers of readers) → **re-measuring loses. Turn it into a check.** Unlike a number measured once,
  this kind of prose **rots silently on every rename or addition**. PR #57's breakdown of test
  classes ("which iterate the definition, which sample") was written in prose three times and wrong
  three times — "all 7 iterate" (5) → a test name that a rename had removed → "8" (9). The fourth
  fix **stopped fixing the number** and put `_DEFINITION_DRIVEN` / `_SAMPLE_DRIVEN` in data with one
  test cross-checking them against the class's real methods (confirmed to fail on rename).
  **Criterion: should this prose break if one test is renamed? Then make it a check**
- **A fix changed the shape of the rule** (denylist → allowlist and the like) → split everything
  after that into another PR. Stacking 25 commits on one branch compounds fixes calling for fixes.
  If you continue without splitting, **give the user the options and ask** (L128 redesigned in
  place, but that was a deviation taken after asking)
- **Five rounds or more without the class descending** → the shape of the rule is wrong. Change
  the design instead of adding a round (the sign below is the case where you already did that
  once)
- **You rebuilt the instrument itself and the second one behaved the same** → **do not build a
  third.** The first rebuild was right (the question could not be answered). If the second keeps
  breaking the same way, the sign is that **the question is at the wrong level**, and lining up
  instruments will not fix it. Declare the scope and hand it over. PR #58 went from a guard reading
  path expressions (infinite spellings) to a ledger of skip reasons (a closed domain, but Python's
  binding forms have a long tail), and the second produced 3-6 isomorphic findings every round from
  5 to 7. The user stopped it at round 7; **this rule would have stopped it at 6.** When handing
  over, name what would have to be built to make a strong claim (here: **cross-check the skips the
  runner reported at runtime against the ledger**, which needs no Python semantics and closes all
  seven rounds' escape routes at once)

  **But do not confuse "a third instrument" with "a defect in the second".** The test is whether
  what broke it was **the shape** or **an implementation bug / a missing witness**. PR #68 rebuilt
  twice (enumeration → full scan → reachability closure) and broke on the sixth round, but the cause
  was **one line not resolving relative imports** plus **the redesign having no witness**, not the
  closure design. Applying "do not build a third" mechanically there would have handed over as
  residual a hole that a single line fixed. Shape → stop; bug and witness → fix
- **The same family appeared two rounds running** → the sign to change shape, without waiting for a
  third. In PR #53 "keywords are not reserved words" came back as `=` (assignment to a variable)
  then `:` (a construct name). Rather than adding a third guard, it closed by **moving to a
  normalization stage ahead of the structural decision** (strip labels, strip construct names,
  detect assignment). The criterion is whether you placed it **where a rule added later is protected
  automatically**
- **The pin was broken in a different shape every time** → this looks like the family sign but
  **closes differently: normalization will not close it.** It means the pin is in the wrong
  **place** — the rule has no single definition. PR #55 was broken three times in three rounds, all
  differently: a three-name denylist → a substring → "a file and **not a directory**" (the predicate
  had another branch). The criterion is **"can this test claim set identity, or can it only produce
  rejection samples?"** If only samples, stop adding samples and **switch to the work of moving the
  definition to one place** — adding rounds just yields one finding per shape. The three escape
  routes left in PR #55's round 3 (another literal name / a subtree prefix / an extension family)
  went unchased for this reason: **a fourth sample was the same mistake for the fourth time**
- **A mechanism you fixed one round ago is eaten together with the fix** → your fix granularity is
  too fine. PR #53 fixed the flat version of the `select` leak with a test, and the next round the
  nested version came back eating that decrement. Closed by moving from two counters to **a stack of
  construct kinds**
- **The same mechanism keeps being broken for three rounds or more** → suspect that **the question
  the rule is trying to answer** cannot be answered at this level of analysis. L128 tried to decide
  "is this name a constant here" by regex and was broken 16 ways; it was solved by changing to **a
  weaker question that can be answered** (is there no other declaration anywhere in the file).
  **If the simple and the complex version give identical measured diffs, the complexity bought
  nothing** — take that diff first
- **A reviewer said "it is environment-dependent"** → do not close it with a mock on the test side.
  Ask first what happens in production on that environment
- **You rebuilt the design and tests carrying the old mechanism's name remain** → test names are
  read as evidence that the mechanism is still protected. In L128, 10 tests named after a deleted
  scope mechanism were in fact a behavioural regression that only one shared mutation killed.
  **Do not delete them; annotate at the head of the group what they pin and what they do not**
- **You extracted a predicate to one place and shared it** → **the call sites need their own pin.**
  Give the function a witness and still nobody observes that it is **called**. In PR #67,
  hardcoding `record_launch`'s `_resolved_makefile_host_authored` to the constant `True` left
  **all 4718 tests green** (only the True-side assertion existed). **The predicate's test and the
  "the call site writes the expected value" test are different things**, and the latter can only be
  written by reading the artifact (payload / artifact / file)
- **A change has "mirrors of the same predicate" and you fixed one** → there can be three mirrors.
  PR #67 found `_ir_is_m3c_physics` mirrored across the conductor, `orchestration_runtime` and
  the validator (today the live copies have moved — grep before quoting this);
  moving one to the registry and abandoning two makes one declared line doubly own an artifact and
  silently disables another gate. **`grep` first for prose saying "mirrors", "cannot disagree",
  "same decision"** — mirrors usually announce themselves in a comment
- **That mirror's agreement test is written as a reconstructed copy of the real thing** → **it
  cannot see a disagreement structurally.** PR #67's agreement test reimplemented one side inside
  the test and stayed green with the old spelling after the body moved to the registry. **Extract
  the real thing as a function and call it from both the body and the test**
- **A witness test's probe value contains the implemented value as a substring** → the assertion is
  automatically true via another clause. PR #67 asserted that `missing_capability_reason(...,
  "cmake", ...)`'s message contains the implemented `make`, which is always true because **`"cmake"`
  contains `"make"`** (found independently by two reviewers). **Assert inside the test that the
  probe has the property it needs** (`assertNotIn(implemented_id, probe)`)

## Finally

Judge whether this skill itself, `scripts/mutation_check.py`, or memory needs updating. If this
loop showed a procedure was missing, gave a false positive, or failed to fire when it should have,
**tell the user** rather than rewriting silently. If you judge no update is needed, say so in one
line.
