# Mid-loop signs: the episodes behind each sign

Moved out of `SKILL.md` verbatim (2026-08-25). `SKILL.md` §"Signs to catch mid-loop" keeps each
sign and its criterion in one or two lines; read the matching section here once a sign fires, for
the case history that tells you how it closed.

- **A finding sits inside the previous round's fix** → not a coincidence. **Name the fixed files as
  the focus** in the next round's prompt (PR #51: three rounds running; PR #68: **all six rounds**,
  with zero defects in the moved code itself and everything in the fixes and the prose). The more
  a change is a move or rename where the body is known correct, the more the review is really about
  **your own fixes** — put the focus instruction in from the first round
- **You have rewritten the same string three times** → the problem is not the rule but the prose
  citing it. Switch to the grep sweep
  (`.claude/skills/metdsl-enforcement-change/references/verification.md`). **Rewriting one statement
  repeatedly is a SWEEP problem, which this row owns; several sites that each state the rule is a
  COUPLING problem, which `metdsl-enforcement-change` rule 3-a owns and states the threshold
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
- **A reviewer said "it is environment-dependent"** (`metdsl-enforcement-change` judgment rule 2
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

