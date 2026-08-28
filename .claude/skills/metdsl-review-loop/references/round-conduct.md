# Round conduct: the episodes behind the rules

Moved out of `SKILL.md` verbatim (2026-08-25) so the skill body carries the rules and this file
carries the evidence. Open it when a rule in `SKILL.md` §"Review target and commit granularity"
or §"Running a round" does not obviously apply, or when you want to
know what it cost.

## Commit granularity, and staging the PRs

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


## What each launch-prompt clause cost before it was a clause

`SKILL.md` §"Running a round" lists the clauses; these are the incidents that put them there.

- **"Do not edit until both results are in"**: in PR #53 one reviewer ran for **78 minutes** and I
  broke the rule twice waiting. That is why the rule is absorbed into the launch prompt as "HEAD
  may advance while you run" rather than kept as a prohibition on myself — and the one report that
  re-verified against current HEAD unprompted was **the most useful of that round**.
- **"Do not modify the checkout"**: one agent left the working tree mutated and **invalidated
  another review's results**, which were running against what it had changed.
- **"Create your own scratchpad subdirectory"**: one agent wrote a file with the same name as mine,
  `mutate.py`, and broke the harness mid-run. It recurred twice while this very branch was under
  review, and the second shape is the one the clause did not cover: **two reviewers were given the
  same round directory**, and one's cleanup deleted the other's scratch out from under its running
  pytest, which then died in `os.chdir`. Naming a shared round root is not naming a private one —
  ask for a PID-unique subdirectory, and for deletion of that subdirectory only.
- **"Say where a worktree may be created"**: a reviewer's `git worktree add` with a relative path
  under `-C <repo>` landed the worktree INSIDE the checkout. It was removed, but before that it was
  picked up by a third reviewer's run as a spurious test failure, and the cleanup also pruned a
  stale entry belonging to someone else. Absolute paths under the agent's own scratch dir, and
  `git worktree remove` when done.
- **"If the reported HEAD is a hash you do not recognize"**: on L174 a reviewer reported `d24a7bb`,
  which was not my commit — the user had committed to the same branch. Concurrency, not staleness;
  read it and judge whether it collides with your scope.

## Process and shared-resource discipline (orphan waits, /tmp, pkill)

- **Spell out the process and shared-resource rules.** FOUR accidents happened for real, each
  spilling into other sessions: an unscoped `pkill -f "pytest tools/tests/"` took out my run and
  another session's; a `until ! kill -0 $(pgrep -f "pytest tools/tests")` wait **matched its own
  cmdline** and left four orphans spinning for 45 minutes; agents left 4.6GB in `/tmp` and 14.7GB in
  `~/.cache`, and the next reviewer hit `0 bytes free`. So the prompt says: no unscoped `pkill`;
  **create only waits whose exit condition can be satisfied, and no background polling**; `/tmp` is
  a shared tmpfs, delete the trees you create.
  - **The fourth, and the one that produced a hook (issue #112's review, 2026-08-28).** A review
    subagent polled a background job with ONE `Bash` call per poll, each leaving a shell and a
    waiting child behind. After ~36 minutes there were **144** of them and the agent had produced
    no report; the OPERATOR noticed before the session did. Killing the agent reaped every
    process, so no `pkill` was needed — worth knowing, because they were children of live
    wrappers rather than true orphans, and `TaskStop` is the first thing to try. Three details
    make this one different from the three above:
    - **It was not a loop, so no loop-detector would have seen it.** One wait per tool call.
    - **It was a bypass of the harness's own block on a foreground wait.** The harness refuses
      that wait in the foreground; the wrapper is what waits, so the letter of the rule was kept
      while its purpose was not. Prefixing with `command` does the same to a shell function.
    - **The prompt forbade background polling, in those words.** That is the fourth data point
      for "handing over the rules is not enough", and what justified moving the rule into the DEV
      hook (`tools/hooks/session_hygiene.py`, PR #118) rather than rewording the prompt again.
      The hook refuses the wait only when a DURATION follows it, in command position; the bare
      word is not matched, so `pkill -f`, `grep` and `ps | grep` over it — the commands you run
      while CLEANING UP after this accident — still work. That carve-out is the point: a rule
      that blocks its own cleanup gets turned off. It keeps the same known over-refusal
      `operator_safety.py` records, and demonstrated it immediately by refusing two commands of
      the very commit that introduced it.
    - What replaced the prompt wording is in `SKILL.md` §Running a round: forbid the MECHANISM by
      name, require the foreground with a bounded `timeout`, forbid running the whole suite in
      one command, and say what to do when the work does not fit. Measured over the two rounds
      after the change: zero orphans, and both reviewers that cut their sweeps still found real
      defects.
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

## Over-refusal: the five countermeasures and where each came from

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
