# Mid-loop signs: the episodes behind each sign

Moved out of `SKILL.md` verbatim (2026-08-25). `SKILL.md` §"Signs to catch mid-loop" keeps each
sign and its criterion in one or two lines; read the matching section here once a sign fires, for
the case history that tells you how it closed.

- **A finding sits inside the previous round's fix** → not a coincidence. **Name the fixed files as
  the focus** in the next round's prompt (PR #51: three rounds running; PR #68: **all six rounds**,
  with zero defects in the moved code itself and everything in the fixes and the prose). The more
  a change is a move or rename where the body is known correct, the more the review is really about
  **your own fixes** — put the focus instruction in from the first round
- **The fix was to a RECORD, so you verified it by reading** (PR #146, issue #143). Five rounds,
  and every round after the first found defects inside the previous round's fix — the row above.
  What that row did not say is that the RECORD fixes fail at the same rate as the code fixes, and
  they are the ones nobody re-runs, because a sentence does not look like something you execute.
  Three in one loop:
  - Round 3 was told an operator remedy was **followable by half**: `docs/RUNBOOK.md` §3-1 sent
    an operator to run `reopen-phase` by hand without naming where its `--trigger-agent-run-id`
    comes from. The fix named `failure_analysis.json#original_finding.failed_substep_agent_run_id`
    — copied from the entry directly above, which is the AGENT-driven route — and on the
    conductor-driven route nothing writes that key. Two round-4 reviewers measured it
    independently: five reads in `orchestration_runtime.py` and zero writers; nine
    `failure_analysis.json` in the local `workspace/`, zero carrying it. **A remedy that was half
    followable became one that is not followable at all**, which is worse than the gap it replaced.
  - The check round 3 wrote to hold it asserted the key's name occurred in
    `orchestration_runtime.py`. It does — inside `_derive_resume_directive`, the CONSUMER the same
    RUNBOOK sentence correctly says never fires for this reason. So the check **ratified** the
    error. What closed it was driving the WRITER: build the artifact
    `run_workflow._collect_failure_analysis` produces for that stop and resolve the documented
    dotted path against it. A pin on a source substring cannot see this class; a pin on the
    artifact can.
  - Round 4's commit message reported fixing a splice defect "in the previous commit". An AST walk
    over every commit on the branch showed no duplicated class member in any of them: the
    duplication had been in the author's uncommitted working tree, mid-splice, and was written up
    as a committed state without checking. The same commit then **created** the duplication it
    claimed to delete — four test methods shadowed in one class, 66 `def test_` lines against 62
    collected, with two of that round's own fixes in the dead half. Reproduced by a reviewer
    inserting `self.fail("dead")` into a dead copy: green.
  - A fourth, smaller: a mutant result was written into a commit message from a round-2
    reviewer's report without re-running it, and round 3 re-ran it and found the opposite. The
    standing rule "do not write someone else's measurement as your own"
    (`atmofab-enforcement-change` rule 3) covers it and was broken anyway, in a COMMIT MESSAGE,
    which is the site the rule most often gets skipped on because nothing there is red.
  A fifth, hit while WRITING this entry, which is the reason the sign is worth its lines: the
  citation fix below was verified by re-running `test_skill_citations` against an unstaged edit.
  That check reads the INDEX (`git show :<path>`, deliberately — a tree whose index and worktree
  disagree otherwise kills it with `FileNotFoundError` instead of reporting), so it was reading
  the text that was still wrong and stayed red; staging it turned it green. **Re-running the
  check is not enough if the check does not read what you edited** — ask what source it reads
  before believing either colour.
  Closure: the corrected sentence must pass the check the wrong one failed, and the check must
  observe the thing the record describes rather than a string that co-occurs with it. The
  repository-wide guard that came out of the third item is
  `tools/tests/test_suite_environment_isolation.py::NoShadowedTestMethodTests` — a shadowed test
  method is invisible from inside the suite: nothing goes red, the collected count silently drops,
  and a `grep` lands on the dead half first.
- **You have rewritten the same string three times** → the problem is not the rule but the prose
  citing it. Switch to the grep sweep
  (`.claude/skills/atmofab-enforcement-change/references/verification.md`). **Rewriting one statement
  repeatedly is a SWEEP problem, which this row owns; several sites that each state the rule is a
  COUPLING problem, which `atmofab-enforcement-change` rule 3-a owns and states the threshold
  for.** Do not restate its number here — that is the drift this pair is about
- **Prose that enumerates entities in the code** (lists of test names, counts of call sites,
  numbers of readers) → **re-measuring loses. Turn it into a check.** Unlike a number measured once,
  this kind of prose **rots silently on every rename or addition**. PR #57's breakdown of test
  classes ("which iterate the definition, which sample") was written in prose three times and wrong
  three times — "all 7 iterate" (5) → a test name that a rename had removed → "8" (9). The fourth
  fix **stopped fixing the number** and put `_DEFINITION_DRIVEN` / `_SAMPLE_DRIVEN` in data with one
  test cross-checking them against the class's real methods (confirmed to fail on rename).
  **Criterion: should this prose break if one test is renamed? Then make it a check** (the
  general form of this, and when a check is the wrong answer, is rule 3-a — this row is the
  enumeration case of it)
- **A fix changed the shape of the rule** (denylist → allowlist and the like) → split everything
  after that into another PR. Stacking 25 commits on one branch compounds fixes calling for fixes.
  If you continue without splitting, **give the user the options and ask** (L128 redesigned in
  place, but that was a deviation taken after asking)
- **Five rounds or more without the class descending** → the shape of the rule is wrong. Change
  the design instead of adding a round (the next sign in this file is the case where you already did that
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
- **A reviewer said "it is environment-dependent"** (`atmofab-enforcement-change` judgment rule 2
  owns this rule; this row is the episode) → do not close it with a mock on the test side.
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
- **You measured a family and reported the conclusion** → PR #98 did this three times, once per
  round, each time in the commit message that announced the fix. The three families and the
  one-question check are in `references/mutation-testing.md`. What makes it a distinct sign from
  the substring one above: there a single probe is degenerate and reading it shows that; here every
  probe is individually fine, and only the SET is wrong — nothing in any one of them looks off, so
  it survives review by anyone who reads the test rather than asking what the family spans

- **Your assertion searches a TOOL'S OUTPUT for a code, a name, or a marker** → PR #116, and the
  most expensive single test defect this loop has produced. The change closed a `leaf shortcut`: a
  leaf could put `! allow(C122, C131, C061, PORT011, C003)` above a `module` statement and take a
  five-finding module to `All checks passed`. The fix added `--ignore-allow-comments`; the witness
  asserted `assertIn("C122", completed.stdout)` for each suppressed code, plus a non-zero exit.
  **Every one of those substrings is present when the fix is REVERTED**, because the linter prints
  the offending source line under each diagnostic — and the offending line here is the allow
  comment itself, which names all five codes. The exit stayed 1 through an unrelated finding the
  directive did not name. So reverting the security half of the fix left one `assertEqual` on the
  argv as the only failure, and a reviewer reproduced exactly that. The rewrite parses
  `path:line:col: CODE` and adds the control the sibling row already had — the same input without
  the flag must LOSE those codes. Two rows in that file now carry such a control; the one that did
  not is the one that broke
- **You added a prose pin: construct the document SAYING THE OPPOSITE and run it** → PR #116 again,
  one round later. Four leaf-read contracts were coupled by three rows: no copyable
  `! allow(<code>)` spelling anywhere in the file, every rule code named must be one the repository
  has a position on, and each region must cite where the set is defined. A blank-slate reviewer
  replaced each prohibition with its reversal — "an `! allow(...)` comment above the offending line
  is the accepted way to clear a stubborn style finding" — and **1294 tests passed**. All three
  rows are about what the document CARRIES; none is about what it ASSERTS. The fix requires each
  region to contain the flag that makes the rule true, derived from `CHECK_FLAGS` so a rename
  breaks the code and the documents together, and self-tests the detector against the reversal that
  passed. Note which side of the tree was already safe: the PURE prompt template was pinned by a
  token literal in `tools/tests/test_pure_leaf_wiring.py` and its reversal died there. The agentic
  path — including the one document every `generate` leaf is force-read — had no counterpart
- **Sweep the FACT, not the spelling** → PR #125, issue #120. One measurement — what a discovered
  `per-file-ignores` key does to a five-finding fixture — was written into three places as "five
  findings to one" when the answer is five to NONE, and the assertion three lines below one of
  those places read `assertEqual(_reported_codes(...), [])`. Round 2 corrected all three and its
  commit message said, correctly, that "a rewrite that touches a sentence and does not re-read the
  clause it is attached to is how both of these survived" — and then did it: an eleventh line below
  the comment it had just fixed, in the same file, in the same commit, sat "the same file silences
  four of the five". My sweep after that commit searched `five findings to one` and `four channels`;
  the survivor was a THIRD spelling of the same fact and was found by a reviewer, not by me, one
  round later. Four rounds, three spellings, one number.
  **Two lessons, and the second is the one that closed it.** Sweep for the NUMBER and for what it is
  a number OF, then read every hit — a `grep` for last round's sentence finds last round's sentence.
  And the fourth correction was not a correction: the comment was DELETED. It restated a measurement
  the assertion beside it already carried, so the two could disagree and only one of them ran.
  Related and different: the figures for this repository's own `ruff` count ([a comment on issue #120](https://github.com/seiya/atmofab/issues/120#issuecomment-5559639219)) are a
  *dated record* and are correct as such; what rots is a claim about the present.
- **A comment or docstring RESTATES a measurement the assertion beside it already carries** →
  the same PR #125 episode, read from the other end. The question that would have closed it in round
  1 is not "is this figure right" but "why is this measurement written twice". A test's assertion is
  executed on every run; the sentence above it is executed by nobody. Wherever the two can disagree,
  the sentence is the half that will be wrong, and correcting it buys one round.
  **Do not confuse this with "prose that enumerates entities in the code"**, which says turn prose
  into a check. Here the check already exists; the prose is the redundant copy, and the fix is
  subtraction. The tell is that the sentence and the line under it are about the same measurement.
- **Your fix NARROWS a check to stop it over-refusing** → PR #125, and it produced the only
  BLOCKER of that branch's disclosure round. `origin/main` carried
  `test_every_version_range_the_runbook_states_is_the_declared_one`, set identity over EVERY
  `>=x.y,<a.b` in `docs/RUNBOOK.md`, with a docstring saying why: it "makes an operator's install
  line and the launch refusal impossible to disagree". Round 2 of that loop found it over-refusing —
  any legitimate `python3` or `cmake` prerequisite documented in the operator's own runbook turned
  the suite red with a message blaming the document for a linter fact — and narrowed it to the
  §0-1 table's range column. Correct fix, real over-refusal, and it deleted the coverage of the one
  line an operator actually types. Measured in the disclosure round: drifting
  `pipx install 'fortitude-lint>=0.8,<0.10'` to `<0.11` left **HEAD green and `origin/main` red**.
  **Why no instrument saw it.** The mutation sweep mutates what exists; the census enumerates what
  exists; a blank-slate reviewer reads HEAD. Every instrument in this loop compares HEAD against
  itself, so a check the branch REMOVED is outside all of them. Only a reader who went looking for
  what was deleted found it, and the brief that sent them was "is anything DELETED whose measurement
  can no longer be re-taken".
  **The procedure is two commands and it is cheap.** Take the defect the old check caught, apply it
  at `origin/main` and at `HEAD`, require red-then-red, and treat red-then-green as the finding. Run
  it in the same commit as the narrowing, because the narrowing is where the knowledge of what the
  old check caught is at its freshest. The eventual fix on that branch was a scope BETWEEN the two —
  any range on a line naming a linter's executable must be that linter's — which catches the install
  line and cannot fire on a prerequisite that names no linter; the general shape is that a
  narrowing usually has a middle, and "whole document" versus "one column" was a false choice.

- **Your change REMOVES something from the default run, and you then write a witness for it** →
  issue #182 / PR #187, found in round 5 of five by BOTH reviewers of that round, independently,
  and by nothing else in four rounds before them.

  The branch froze a tree-versus-baseline comparison out of `pytest tools/tests/`: the token
  ratchet in `tools/tests/test_backend_boundary.py` became the explicit command
  `--check-baseline`, so that an unrelated pull request could no longer be forced to re-measure
  the recorded baseline. Round 3 added a disclosure to `--write-baseline` — it prints the entries
  it is about to bless — and round 4 added witnesses for that printing. One of them,
  `test_write_baseline_says_so_when_the_regeneration_changes_nothing`, called the real
  `_write_baseline()` with no injection and asserted `"the tree already matches the baseline"` in
  its output. That sentence is printed only when the comparison over the REAL tree and the REAL
  baseline is empty in both directions, so the row was the frozen comparison, running in the
  default suite and in CI, in both directions, under a class named `DirectImportPinTests`.

  **What it cost, measured at the tip before the fix.** Appending one ordinary prose comment
  naming a sampled token to a scanned document turned the module file red (`1 failed, 108
  passed`); so did an inflated ceiling committed in the baseline, a garbage baseline, and a token
  rename in a neutral module. `.github/workflows/tests.yml` runs `pytest tools/tests/`, so this
  was every pull request. The failure names a row about `--write-baseline` and its printed body
  ends with `wrote tools/tests/data/backend_boundary_baseline.json`, so the cheapest green was to
  regenerate the baseline in a non-ledger pull request — the precise `leaf shortcut` the freeze
  exists to remove, reintroduced by a witness written to protect it. The prose two earlier rounds had written — "a green suite says nothing at all about growth" in
  `AGENTS.md`, "nothing in the suite and nothing in CI runs either command" in
  `docs/BACKEND_BOUNDARY.md`, and the same claim in `TODO.md`, `tools/backends/registry.py`, the
  module docstring and both dev skills — had been false since that commit. Do not take that list
  as a count: what makes it long is that a claim about what the default run does gets restated
  wherever the rule is cited, which is `atmofab-enforcement-change` rule 3-a's trigger.

  **Why every instrument missed it for a round.** The mechanism was in a test file: the round-4
  sweep mutated the production functions and all of them died, because they are genuinely pinned —
  the row that re-coupled the suite is not a mutant of anything. Round 5's blank-slate reviewer
  reported 20 of 20 production mutants killed in the same run in which it found this, which is
  the cleanest statement of the gap: the sweep was right and answered a different question. The witness census counted the row
  as a witness, which it is; what it also was is a coupling, and a census asks "does this observe
  something", never "does this observe something the change promised to stop observing". A
  blank-slate reviewer reads HEAD and sees a green suite. What found it, twice, was injecting a
  defect into the tree and running the default suite — the same move the row above prescribes for a
  narrowed check, pointed forwards instead of backwards.

  **The fix is injection.** `_print_what_is_about_to_be_blessed` and `_write_baseline` took `root`
  and `baseline_path` exactly as `_check_baseline` already had, and every row observing the printed
  output moved to a synthetic tree and a synthetic baseline. One row still drives the real command
  — that it writes `measure()` to the real path — and that one is tree-state-independent by
  construction, since both sides are measured from the same tree. Deleting the witnesses instead
  would have been the wrong repair: the disclosure they cover is real, and round 4 had added it for
  a real reason.

  **The wider reading.** Round 3's fix introduced a functional regression that round 4 found;
  round 4's fix defeated the branch's purpose and round 5 found that. The class did not descend
  across the last three rounds, and the loop ended at the cap without meeting a stopping condition.
  A change whose subject is REMOVING a check should expect its own witnesses to be the highest-risk
  part of it, and should carry the injection command from the first round rather than discovering
  it at the cap.

