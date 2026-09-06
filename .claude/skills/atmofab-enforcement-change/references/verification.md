# Verification procedures (atmofab)

Run these from the atmofab checkout root. **Do not write an assertion into a commit or TODO.md
that you have not measured.**

These are an operator's own dev-session commands. `AGENTS.md` §MCP execution rules — run
`compile` / `run` / checks through the MCP server, avoid direct shell execution — governs what a
WORKFLOW does; it is not a rule against running `pytest`, `ruff`, or a compiler probe by hand
while developing this repository.

## The suite

```bash
python3 -m pytest tools/tests/ -q -p no:randomly
```

- **Before you diagnose a defect, run the FILE that covers the mechanism on `origin/main`.**

  ```bash
  git worktree add --detach <scratch>/main origin/main
  (cd <scratch>/main && python3 -m pytest tools/tests/<the file> -q -p no:randomly)
  ```

  Issue #113 was a freshness gate comparing a `time.time()` instant against a file mtime — two
  different clocks, so a deterministic in-process body that finished inside one timer tick failed
  its own gate. `tools/tests/test_workflow_conductor.py` was **8 failed / 761 passed on
  `origin/main`** for exactly that reason (`VerifyMetaSchemaWarmResumeTests` and both copies of
  `test_retried_judge_cannot_certify_the_dead_attempts_semantic_review`, whose stubs write their
  metas in-process), and it went to 770 passed on the branch. Nobody noticed for two rounds: I
  argued the cause from one incident's artifacts and from a timing measurement, a reviewer showed
  the arithmetic did not carry, and only then did another reviewer run the file on `main`. **The
  strongest evidence a causal argument in this repository can have is often already a red test on
  `main`, and it costs seconds.**

  **Why the full-suite run does not surface it**: the whole-tree verdict at the time was "2 failed
  / ~5460 passed", those two being the then-standing path-depth-coupled
  `ForbidBackendCredentialReadTests` cases (closed 2026-09-02, issue #84), and a branch that FIXES
  eight failures elsewhere reports the same two. The delta rule
  below compares totals, and eight fewer failures reads as eight more passes among thousands. Run
  the file, read the failure NAMES.

  Two directions, both worth the seconds: failures on `main` that your branch turns green are
  evidence FOR your diagnosis (say so in the commit — I did not, and a reviewer had to); failures
  your branch does not touch are the baseline you must name before anyone else reads them as
  yours.

- **Do NOT point `TMPDIR` at `/dev/shm`.** This section used to recommend it, and the
  recommendation cost two false failures on `main` and on a branch alike (measured 2026-08-21):
  `test_hooks_common.py::DevShmWriteBlockTests::test_blocks_dev_shm_via_find_traversal` and
  `::test_blocks_dev_shm_via_tar_chdir`. Those two reason about a write guard over `/dev/shm`, so
  putting the fixture's own scratch directory inside the path under test returns a different
  policy id. It bought nothing either: `/tmp` is tmpfs here too, and four alternating full-suite
  runs did not separate them (96.7 / 103.0 s against 102.3 / 93.8 s). **The rule, not the
  spelling**: a scratch root must not be a path the suite makes assertions about. On a host whose
  `/tmp` is disk-backed, point `TMPDIR` at some OTHER tmpfs
- **Never run two of these in parallel.** They share `TMPDIR`, which produces false failures
  (hit for real once)
- The baseline is the measured value on `origin/main`. Comparing via `git worktree` /
  `git archive` used to cost two failures of its own:
  `test_hooks_common.py::ForbidBackendCredentialReadTests` assumed the checkout sat at a
  particular depth under `$HOME`, so the pair was red wherever it did not (identical on `main`
  and on a branch). Issue #84 closed that on 2026-09-02 — the fixture builds both halves now —
  but a location-dependent row is a class, not one pair. **Compare the DELTA, not absolute
  values**
- **This count rots. Re-measure before handing anything over.** Three measurements of the same
  suite: 5 failures on 2026-08-13; 2 failed + 1 skipped on 2026-08-18
  (`test_blocks_bash_only_tilde_prefixes` / `test_directory_options_anchor_like_cd`, both since
  fixed); 4924 passed and 0 skipped on 2026-08-20, in a full sequential run. `test_hooks_common.py`
  has timing-budget tests (`process_time` under 5s) that depend on the machine: one of them failed
  standalone here at 16.9s while the full run was clean, so **running a suspect test alone is not
  a way to tell load from defect** — compare against `origin/main` instead. **Do not quote this line's numbers
  — measure once yourself**
- **Put it in the prose you hand reviewers, too.** In PR #57 three reviewers each re-derived it,
  and one corrected my claim with its own measurement. Writing it down settles it. Conversely,
  **hand over a stale count and reviewers take it for the baseline and miss a real failure**
- **Skips: measure, do not assume.** On 2026-08-19 the suite reported 4877 passed and **0
  skipped**; every skip in `tools/tests/` is conditional on the host (gfortran, bwrap, `/proc` and
  the rest of `_DECLARED_ENVIRONMENT_SKIPS` in
  `tools/tests/test_skip_reasons_are_declared.py`), so what you see depends on the machine. **If a skip appears, find out which condition produced it** —
  that is the reason to look, and it is the same reason whether the count is 0 or 3. An earlier
  version of this line claimed one permanent calibration skip and sent a reader hunting a test that
  does not exist

## Verification steps that silently do not run, and records that silently do not hold

Each of these looks exactly like a passing step or a kept record. All but the first were hit on
issue #71; the compiler-probe one is issue #153's, added 2026-09-05 — so do not read the list as
one incident's inventory, which is what "all of these were hit on issue #71" made it while that
sentence stood alone. (An earlier version of this heading also said "two ways" over five items — a
hand-typed count, rotting inside the commit that wrote it, in the file that tells you not to
hand-type counts. The fix then was to stop counting them here; the fix now is to stop attributing
them to one issue.)

**A COMPILER PROBE IS STATEFUL IN ITS WORKING DIRECTORY, and the artifact the PREVIOUS shape left
can supply exactly what the next one is missing.** `gfortran` writes `.mod` / `.smod` beside the
source and reads them back on the next invocation, so probing shape B where shape A just ran is not
a measurement of B. Issue #153 shipped a wrong record that way, and the mechanism is narrower than
"reuse of a name" — re-derived here, because a first version of this entry stated it loosely and
did not reproduce:

```
B alone, clean directory                      -fsyntax-only rc=1   ("Module file
                                                                    'hx_model.smod' has not
                                                                    been generated")
A compiled first (same module AND same
procedure name), then B in that directory     -fsyntax-only rc=0    -c rc=0
```

A stale `.smod` for a DIFFERENT procedure changes nothing (measured): the leftover has to satisfy
the very thing the next shape lacks. So the tell is not "these fixtures share a name" but **"the
previous shape could have produced what this one needs"**.

- **One fresh directory per shape.** `tempfile.mkdtemp()` per probe, not one directory for the
  family. `rm -f *.mod *.smod` between runs is the same rule in the form that is one forgotten
  line from failing.
- **The worst version is a cleanup line BETWEEN the two halves of one measurement.** That is what
  actually happened: `-fsyntax-only` ran in the contaminated directory, an `rm` ran next, and `-c`
  ran in the now-clean one. The row recorded "rc=0 / rc=1" as one probe of one shape, and the
  contrast between the two numbers — which looked like a finding about the syntax stage — was
  entirely the directory changing underneath them. **Two numbers in one row must come from one
  environment; if a cleanup sits between them, they are two measurements and neither says what the
  row claims.**
- **This generalises past compilers.** Any tool that writes an artifact beside its input and reads
  it back — a build system's object cache, a linter's cache, `ruff` without `--no-cache` — has the
  shape. On the same issue two ruff figures were wrong for the other reason (taken before the
  round's last edits), which is the other half of the discipline: **one environment per row, and
  every figure taken after the last edit**

**`cmd | tail && git commit` does not gate on `cmd`.** A pipeline exits with the status of its
LAST element, so `python3 -m pytest … | tail -3 && git commit` commits whatever pytest did — the
`&&` is inert and the summary line scrolls past in the same output that reports success. It put a
commit on top of a red suite. Either run the command bare and read the code (`python3 -m pytest …
-q; echo $?`), or put the guard on the command itself and pipe afterwards
(`set -o pipefail` also fixes it, but only if the shell honours it — check rather than assume).

**`git -C <repo> worktree add <name>` resolves `<name>` against `<repo>`, not your cwd.** `-C`
is a global option and changes directory before the subcommand runs — written after the
subcommand it is not even accepted (`error: unknown switch 'C'`, measured; an earlier version of
this entry had it in that unusable position, in the file that says to execute what you write).
So a relative path meant for a scratch directory creates the worktree INSIDE the checkout, and
the next `git add -A` commits it as a gitlink; git warns, in a hint block that is easy to scroll
past, and the commit succeeds. **The general rule is the one to keep: read `git show --stat`
before believing a commit contains what you staged.** Give worktree paths absolutely; `git
worktree list` is the check and `git worktree remove <path>` the repair, before amending.

**A multi-edit script that asserts before writing loses the whole batch, silently.** Three times
on issue #71 an edit reported as landed had not: a Python heredoc making several substitutions
raised on a stale `assert` for one of them, after the earlier substitutions had been computed
and before anything was written. The shell shows a traceback, the commit that follows says the
work is done, and `git log -S` cannot find it. **One edit per script**, each asserting its own
match count. This is the same failure as a commit message claiming work the artifact does not
carry, arriving by a different road.

**Name the mutation survivors in the commit message, individually.** "34 mutants, 30 killed, the
four survivors confirmed equivalent or unreachable" is not a record: a later round cannot check
it, cannot tell which four, and cannot tell equivalent from unreachable. On issue #71 that claim
had to be replaced by re-running the sweep, because nothing could recover the list. A survivor
you accept is a classification, and rule 1-b applies to it.

**An instrument you commit needs a machine-readable verdict and its own test.** `measure_claude_tool.py`
shipped with `main` returning 0 unconditionally, rows a reader classified by eye, and no test —
and a review round then found five defects in it, two functional. If a script's output is
evidence for a decision, it must be able to say PASS or FAIL, and an error (a timeout, a launch
that produced nothing) must fail whatever the row expected rather than being scored as one of
the two answers.

## Whole-tree diff (mandatory whenever you change a gate)

**The single most effective check in this repo.** Point the gate at the real corpus and compare
the verdicts before and after. Across 19 commits and 9 rounds, L128 could confirm "29→27, and
not one file under `problem/` changes verdict" at every commit, and reviewers reproduced it
independently. **It shows "nothing is broken" as a diff instead of an argument.**

```bash
# 1. write a scanner that calls the gate directly and dump JSON (file -> violation list)
#    fix the gate's premises explicitly (node_key, dep_spec_ids, …)
python3 corpus_scan.py > after.json
# 2. run the SAME scanner against the baseline in a throwaway worktree. Feed the scanner in
#    on stdin rather than copying it in: ANY untracked file in the worktree makes
#    `git worktree remove` refuse (exit 128) and leave both the directory and its
#    registration behind. `mktemp -d` so a leftover from an earlier run cannot be scanned
#    by mistake, and a `trap` so a scanner that raises still removes the worktree.
BASE=$(mktemp -d) && rmdir "$BASE"
git worktree add -q --detach "$BASE" origin/main || exit 1
trap 'git worktree remove --force "$BASE" 2>/dev/null' EXIT
(cd "$BASE" && python3 -) < corpus_scan.py > before.json
# 3. report which violation in which file moved, not the counts
```

**Do not reach for `git stash` here.** On a clean tree — which this loop's own commit discipline
requires before you measure anything — `git stash` is a **silent no-op**: it exits 0, creates no
entry, and both scans read the same bytes, so the diff is empty and the check reports that nothing
changed verdict. A trailing `git stash pop` then pops whatever unrelated entry was already on the
stack into your checkout. Running the same scanner on both sides is not incidental either: the
harness has to be identical, or what you measure includes the harness. (`python3 -` puts `''` on `sys.path[0]`, which resolves
to the cwd — the worktree — so repo-relative imports behave as they do on the branch side, where
`sys.path[0]` is the scanner's own directory. What differs is `__file__`, which is `<stdin>`. A scanner that reads its own source, or that needs a
helper module beside it, wants a path outside the worktree instead.)

**A reused worktree path is the same silent no-op in different clothes.** If a previous run's tree
is still there, `git worktree add` fails (an EMPTY directory it accepts), an unguarded script walks
on, and the scan reads whatever revision that leftover tree is at — an empty diff again, from the population most likely to have a
leftover: whoever ran the earlier version of this recipe. That is what `mktemp` and the `|| exit`
above are for.

- **Always record the harness.** Changing how dep_spec_ids are derived changes the absolute
  numbers (29→27 / 31→29 / 35→33 on the same tree). **The diff reproduces; the absolute values are
  harness-dependent.** If a number goes into TODO or a commit, the derivation goes with it
- **Look down to subroutine granularity.** Per-file flag/silent alone hides one violation
  disappearing while another appears in the same file (this was actually missed)
- Measure changes in the fail-closed direction (more refusals) the same way. L128 showed **the
  module dep map for 357 directories byte-identical and 103/103 Makefile verdicts unchanged**

## Extraction diff: what a refactor silently changed (AST, per function)

**A helper extracted from an inline expression is the one change shape where a green suite
proves nothing**, because both sides are meant to behave identically — so nothing new is
exercised and nothing old fails. PR #107 shipped one: extracting an ownership predicate out of
`_cleanup_agent_tmp_root` dropped an `orchestration_id.strip()` that the inline form had, and a
padded id then resolved to a directory that does not exist, ownership silently failed, cleanup
refused, and the run kept its tmp scratch. **The whole suite was green on both sides.** It was
found by diffing the extraction against the text it replaced, and by nothing else.

Read the functions, not the diff. A `git diff` of a refactor is mostly motion, and the one
changed token sits inside it looking like motion too.

```bash
git show <base>:tools/orchestration_runtime.py > /tmp/old_rt.py
git show HEAD:tools/orchestration_runtime.py  > /tmp/new_rt.py
python3 - <<'EOF'
import ast, difflib
def bodies(path):
    tree = ast.parse(open(path).read())
    out = {}
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            f = ast.parse(ast.unparse(n)).body[0]
            if (f.body and isinstance(f.body[0], ast.Expr)
                    and isinstance(f.body[0].value, ast.Constant)
                    and isinstance(f.body[0].value.value, str)):
                f.body = f.body[1:] or [ast.Pass()]      # docstrings are not behaviour
            out[n.name] = ast.unparse(f)
    return out
old, new = bodies("/tmp/old_rt.py"), bodies("/tmp/new_rt.py")
for name in sorted(set(old) | set(new)):
    if old.get(name) != new.get(name):
        print("=== " + name)
        print("\n".join(difflib.unified_diff(
            old.get(name, "").splitlines(), new.get(name, "").splitlines(), lineterm="", n=1)))
EOF
```

- **`ast.unparse` normalises formatting**, so a reflow, a comment, or a docstring rewrite cannot
  hide a token. Stripping the docstring is what makes the output short enough to read — on PR
  #107 it reduced a 1449-line branch diff to **exactly four changed functions**, which is the
  half that matters: it bounded what else could have been dropped
- **Run it over the test file too.** Same command, and it answers "which tests changed behaviour"
  rather than "which lines moved"
- **This does not replace mutation.** It says what changed; mutation says whether anything
  notices. An extraction needs both, and needs them in this order: the diff tells you which
  mutants are worth building
- **The base is the commit before the refactor**, not `origin/main`, when the branch has several
  commits — otherwise the refactor's own delta is buried in the feature's

## ruff shows "identical to the baseline"

Compare per file, not by count. **Add a file to the comparison and the baseline must be retaken**
— an "identical, 1 finding" measured over two files was written unchanged after a third file was
added, and was wrong.

```bash
# ruff answers `E902 No such file` + `Found 1 error.` for a path that does not exist, and
# `tail -1` prints that count. Unguarded, a file the branch ADDS reads as a lint error the
# branch fixed, and a file it DELETES (or the old name of a rename) reads as one it introduced.
# Both sides need the test, not just the baseline side.
BASE=$(mktemp -d) && rmdir "$BASE"
git worktree add -q --detach "$BASE" origin/main || exit 1
trap 'git worktree remove --force "$BASE" 2>/dev/null' EXIT
for f in <touched files>; do
  for side in . "$BASE"; do
    [ -f "$side/$f" ] || { echo "$f ($side): absent this side"; continue; }
    echo -n "$f ($side): "; ruff check "$side/$f" 2>&1 | tail -1
  done
done
```

`tail -1` is a summary, not a verdict. On ruff 0.15.20 a file whose findings carry SAFE fixes ends
`[*] 1 fixable with the --fix option.` and one whose fixes are unsafe or hidden ends `No fixes
available (1 hidden fix can be enabled with the --unsafe-fixes option).` — in both cases the count
is on the line above. Read the full output whenever the last lines differ.

**A FILTER you write over the output is a third thing that can be empty, and an empty-vs-empty
diff is green.** Issue #149 compared the two sides with
`ruff check <f> | grep -E "^tools/" | sed … | uniq -c` and diffed the results: ruff's default
output puts the path on a `-->` continuation line, not at the start of a line, so the filter
selected NOTHING on either side and the diff printed clean. The branch shipped a commit message
saying "histogram identical to the baseline" while it had in fact gone 13 -> 22 errors (four new
`C408`, `RUF012` 3 -> 8), and a review round found it. Two rules, both one line:

- **Assert the filter selected rows on at least one side before you compare.** `[ -s base.txt ]`,
  or print the row counts beside the diff. A comparison of two empty sets is the same shape as
  `assertEqual(rule(corpus), set())` for a rule that returns the empty set unconditionally.
- **Ask for a STABLE format rather than parsing the human one**: `--output-format concise` is one
  finding per line, `path:line:col: CODE message`, and it does not move between ruff versions the
  way the default rendering does. `--statistics` is better still where a count is what you want.

**If you want a NUMBER, ask ruff for it: `ruff check --statistics <f>`.** Do not write a counter
for the occasion. On TODO:269 the count went into the ledger from `ruff check <f> | grep -c
"^[A-Z][0-9]*"`, where `[0-9]*` matches zero digits, so `No fixes available` and its neighbours
were counted as findings — four numbers wrong in one sentence, all four reproduced by a reviewer
in minutes. The comparison itself survived (the same wrong counter ran on both sides), which is
the shape that makes this kind of bug last: **the claim that matters can be true while every
number in it is false.**

Same reason as above, and one of this loop's own rules besides: the earlier form used `git stash`
plus `git checkout -- .`, and `atmofab-review-loop` forbids `git checkout -- <path>` by name for
discarding uncommitted work along with whatever it was meant to revert.

## Tidying tangled commit history (no interactive rebase available)

This environment has no `git rebase -i` / `git add -i` / `git add -p`. To split N tangled commits
(several items touching the same file, build-then-remove churn mixed in) into per-item commits:

1. **Take a safety ref and a fingerprint first.** `git branch -f backup-pre-tidy HEAD`,
   `git rev-parse HEAD^{tree}`, `git diff origin/main | sha256sum`.
2. `git reset --mixed origin/main` — HEAD and the index move to the base; the working tree stays
   at the final state. `git diff` now shows the whole base→final difference.
3. Stage per item: a file wholly owned by one item gets `git add <file>`. A shared file needs
   **hunk selection** — build a partial patch from `git diff -U{ctx} -- <file>` filtered by the
   `@@` old-start line, apply with `git apply --cached` (`-U0` + `--unidiff-zero` when two items'
   changes sit only a few lines apart). Commit.
4. **The trap:** staging part of a shared file shifts the OLD-START line numbers the next item's
   hunks are selected by, because the index moved. Re-read `git diff -- <file> | grep '^@@'`
   immediately before selecting each item's hunks. Appended (end-of-file) changes do not shift
   earlier line numbers.
5. Two items editing the same physical line cannot be hunk-split; fold to whichever item makes
   more sense and say so in the commit message.
6. A size threshold and the body it gates, when they land in different items: bump the threshold
   in the item that first grows the file, to its FINAL value — later items then stay green too.
7. **Verify, always.** `git diff backup-pre-tidy HEAD` must be empty (tree bytes identical); the
   tree hash and net-diff sha from step 1 must match. Checkout each new commit and run the full
   suite (bisectability). End on `git checkout main` — a detached HEAD left behind reads as an
   uncommitted working-tree difference to the harness. Delete the backup ref only after the user
   confirms.

Tree-byte identity is what makes this mechanical rather than trusted: a `git diff` that comes back
non-empty means a hunk was mis-assigned, and it says so immediately rather than at review time.

## Doc size ceilings

Docs that enter a leaf's context have ceiling tests — but only the nine in the `_CEILINGS` table
below, which includes `phase_01_compile.md` and none of the other phase docs. **Check membership
first**: editing a doc that is not in the table and running the command measures nothing.

```bash
python3 -m pytest tools/tests/test_orchestration_runtime.py -q -p no:randomly -k child_context_docs
```

If you exceed one, **cut redundancy rather than raising the ceiling**.

**Because the test is a maximum, a change that shortens a doc cannot structurally fail it** = the
test says nothing at all while you are shortening. Do not treat its green as evidence. **Measure
and write the headroom**:

Do not transcribe the ceilings; **read them from the table in the test** (there are 9 docs, bumped
independently):

```bash
python3 - <<'PY'
import pathlib, sys, importlib
sys.path.insert(0, "tools/tests")
C = importlib.import_module("test_orchestration_runtime").ChildContextDocSizeTests._CEILINGS
for rel, ceil in sorted(C.items()):
    n = pathlib.Path(rel).stat().st_size
    print(f"{n:6d}  headroom {ceil - n:+6d}  {rel}")
PY
```

In PR #55, headroom fell to **1 byte** during work that was shortening a SKILL and went unnoticed
for several rounds (it ended at 37). **Run the snippet; do not read a list from here.** Measured
2026-08-20, 4 of the 9 are at 50 or less — `workflow-validate-judge` +22, `phase_01_compile` +34,
`workflow-compile-generate` +37, `AGENT_CONTRACT` +44 — and the set moves: a 2026-08-13 version of
this line named `workflow-generate-generate` (+6) and `workflow-generate-verify` (+5) as the
tightest, and today they are the roomiest at +125 and +230. It also named 4 of 9 while 6 were at
or under 50, because it listed what someone had looked at rather than what the snippet printed.

## End to end through a real server process

Confirm through `mcp_call.py` rather than `import`, so the JSON-RPC layer and the handling of the
environment are included.

```bash
# standalone works
env -u ATMOFAB_WORKFLOW_MODE -u ATMOFAB_ORCHESTRATION_ID \
  python3 mcp_servers/mcp_call.py --tool run_syntax_check --args-json '{"project_dir": "<abs>"}'
# under the workflow, dropping orchestration_id is refused
ATMOFAB_WORKFLOW_MODE=1 python3 mcp_servers/mcp_call.py --tool run_linter --args-json '{"project_dir": "<abs>"}'
```

## What an LLM CLI actually does (unbilled capture harness)

For work that changes a leaf's launch flags, configuration layers, permission layers, or what is
injected, **capture instead of inferring from what the flags mean**. Point `ANTHROPIC_BASE_URL` at
a local HTTP server and the request body is readable as it is. Issue #63 settled all of it
unbilled this way: whether hooks fired, whether `CLAUDE.md` was injected, permission verdicts,
whether `--resume` works, the matcher semantics, and what gets written into the home.

```python
# skeleton: accept /v1/messages and save the body. Side requests (empty tool roster) get a
# bare end_turn; only the MAIN request (the one carrying the target tool) gets a synthetic
# tool_use. The tool_result in the second request is the permission layer's verdict.
```

- **Always mix in one control.** "Everything passed" and "the layer was dead" are
  indistinguishable without one — for permissions, a form that **must** be refused (`curl` and
  friends); for injection, the same measurement under the old flag
- **Match the kind of leaf you measure to production.** A tool-less leaf and a tool-carrying leaf
  made the CLI create different things in the config dir (2 of 6 were missed)
- **Pass `--debug-file`.** `tool_dispatch_end ... outcome=ok` / `Bash tool permission denied` /
  `Applying permission update: ... destination 'userSettings'` are direct evidence of a verdict
- **A hook that writes a sentinel** put into the settings leaves a trace of whether hooks fired
  (`Found 0 total hooks in registry` is about **plugins** and is unrelated to settings hooks)
- Put the cwd and `CLAUDE_CONFIG_DIR` in scratch. **Make the repo your cwd and the real hooks run
  and write into `workspace/orchestrations/`** (this actually happened)
- **Launch the CLI BY HAND, never through the conductor.** Since the leaf's environment became a
  declared allowlist, `_child_env` strips `ANTHROPIC_BASE_URL` — the conductor would send the leaf
  to the real API and bill it, and the harness would capture nothing. Every capture to date was
  hand-launched, so no past measurement is invalidated; what changed is that the shortcut is now
  closed rather than merely unused. To measure the environment a leaf really gets, render a real
  bwrap profile and swap the leaf command for `/usr/bin/env` (that is a witness, not a capture,
  and it is free)

## Sweeping the prose (whenever you change a rule)

Look for sentences that **cite the rule as grounds**. They are scattered across docstrings,
comments, the violation messages actually emitted, phase docs, skills, and TODO.

```bash
# search by the name of the mechanism you changed (example: dropping a line scan)
rg -n "line-scan|LINE-SCAN|line scan|linear scan" tools/ docs/ skills/ .claude/skills/
# re-measure every claim that says it was measured
rg -n "[Mm]easured" tools/ docs/ | rg -i "<what you changed>"
# emitted strings are not the docstring. Look at violations.append / raise directly
rg -n "violations.append|raise (ValueError|RuntimeError)" <touched file>
```

**If you wrote a measured value as grounds, re-measure it after the change.** In PR #51 the same
string was rewritten four times.

**One match expression is not a census, and calling it one is a false claim about coverage.** On
TODO:269 the post_judge disposition rule was stated in six documents. Reading and grepping found
2; enumerating every `.md` with `os.walk` and matching lines carrying both `disposition` and
`recoverable` found 5, and that was written up as "the record of how many sites there are"; a
reviewer matching `post_judge` + `fail_closed` found the 6th, which states the same rule using the
word **severity**. The remedy is cheap — **run two or three expressions built from DIFFERENT words
in the rule, and stop only when they agree**:

```bash
# enumerate files yourself: `grep` is shadowed in an agent session and respects .gitignore
python3 - <<'PY'
import os, re
PATS = [("disposition", "recoverable"), ("post_judge", "fail_closed"), ("warm-resum", "judge")]
for root, _, files in os.walk("."):
    if "/.git" in root: continue
    for f in files:
        if not f.endswith((".md", ".py")): continue
        p = os.path.join(root, f)
        for i, line in enumerate(open(p, errors="ignore"), 1):
            for a, b in PATS:
                if a in line and b in line: print(f"{p}:{i}  [{a}+{b}]")
PY
```

If the expressions disagree, the rule is stated in more than one vocabulary and the widest answer
is the census. **Say which expressions you ran** — "I enumerated every `.md`" describes the file
walk, not the matching, and the matching is where the miss was.

## The backend-boundary token ratchet (frozen, issue #182: run `--check-baseline` on a ledger PR, and before merging a change to a scanned file that you want measured)

`python3 -m tools.tests.test_backend_boundary --check-baseline` counts technology tokens per file
and exits 1 on growth or a stale entry (issue #182: the comparison is not in the suite until the
second target starts, so nothing runs it unless you do).
**It reads WHOLE FILES, so an ordinary comment trips it** — and a commit whose
verification runs only the test file for the module it changed will not see that. On TODO:269 it
tripped three times, all from prose, and one was caught two commits late for exactly that reason,
after a commit message had already asserted "ratchet still green" without running it.

```bash
python3 -m tools.tests.test_backend_boundary --check-baseline      # the frozen half, explicit; exit 1 names the entries
python3 -m tools.tests.test_backend_boundary --write-baseline      # ONLY after the judgement below
```

**Three trips, three different right answers — the judgement is the whole content of this
section:**

- **A neutral-role citation → regenerate, and say so in the commit message** — but only in a
  ledger pull request. While the ratchet is frozen (issue #182) every other pull request
  RECORDS the finding and leaves the baseline alone; the three answers below are the
  judgement, and regenerating is the ledger's step, not yours. Naming an existing
  symbol the neutral core already exports (`FORTRAN_STRUCTURE_UNAVAILABLE_EXIT_CODE` at a new read
  site) is not new technology knowledge. `AGENTS.md` permits naming an `axis` value as an opaque
  token; the prohibition is on a file extension, keyword, grammar, compiler argument, lint rule
  id, directive spelling, control-file syntax, naming convention or diagnostic format.
- **A genuine addition → withdraw it, do not regenerate.** A RUNBOOK recovery entry spelled two
  parser distribution names that `run_workflow.py`'s `REQUIRED_PYTHON_MODULES` already owns and
  prints. Pointing at the refusal was both the neutral spelling and the better instruction — one
  owner, no copy to go stale.
- **Do NOT rename an identifier to get under the counter.** A reviewer will observe that some of
  the growth was "avoidable by spelling" (a local alias bound once instead of a long name imported
  twice). Reject that: it satisfies the instrument and not the rule, and it is the same habit
  `docs/BACKEND_BOUNDARY.md` §Enforcement warns the ratchet itself can teach — regenerating
  without reading the rule. Record the debt and the reasoning instead, so the operator can
  overturn it.
- **Pin what the rule MEANS, not the file list a run of it happened to produce.** PR #66's import
  scanner pinned an enumerated set of scanned files; adding one ordinary new module then failed it
  for a reason that had nothing to do with a boundary violation. A pin over a RESULT breaks on
  every legitimate change to the result's shape and teaches "regenerate without reading" faster
  than the ratchet itself does.

**A ratchet failure used to be a false KILL in a mutation sweep, and while the ratchet is frozen
it cannot be.** Deleting an identifier to test something else moves the count; when the comparison
ran in the suite the ratchet failed and the mutant read as killed for a reason that had nothing to
do with behaviour, and on TODO:269 that hid an exit-code mapping with no behavioural witness at
all. Out of the suite (issue #182) a sweep is not killed by it, so a mutant lives or dies on
behaviour alone — and the sentence above applies again the moment the removal procedure in
`TODO.md` restores the comparison. When a mutant dies, read WHICH row died.

## A CPU-time budget is still a machine fact

Reach for this whenever a check asserts that something is FAST. Wall clock is obviously
load-dependent; **`time.process_time()` is not the fix, and believing it is has a measured cost**.
On PR #104, the same two hook blocks cost 1.13-1.21s of CPU solo, 1.40-1.48s under three
concurrent pytest runs and 2.30-2.41s under 44 spinners on a 22-core host — 2.1-2.5x of movement
against a 5.0s bound, and a reviewer independently measured one of them at 12.21s. CPU time
inflates under contention because the work itself costs more cycles (cache and memory pressure,
and WSL2 scheduling), so there is no absolute number that is safe.

What the branch shipped, and what it is worth:

- **A bound RELATIVE to a calibration workload measured in the same process, around the block.**
  Across load levels this helps: seconds moved 5.5x while the quotient moved 2.1x in one
  measurement, 5.0x and 3.0x in an independent one. **Within one load level it costs**, because a
  ~0.1s denominator does not average out the way a 1-10s numerator does — a reviewer's heavy batch
  moved 1.36x in seconds and 2.36x in units. **More samples do not fix that**: mean-of-2,
  median-of-5 and median-of-9 estimators all spread 1.8-2.0x across fresh processes, quiet and
  loaded alike, because what moves is the host between one process and the next.
- **So do not justify the bound by the compensation.** Justify it by a BRACKET with both ends
  measured: above every figure ever observed for that block, and below the smallest regression the
  check has ever caught. State both figures. The first version of that comment argued a mechanism
  ("1.4x of spread instead of 2.5x"), a reviewer refuted it in fresh processes, and what survived
  review was deleting the argument and keeping the numbers.
- **The calibrator needs its own witness, and an implausible constant is not enough.** `return 1.0`
  was killed by the self-test; `elapsed = 0.05` — the number the calibrator usually returns on that
  host — passed every bracket and silently turned the quotient back into raw seconds. Pin that the
  price RESPONDS: quadruple the workload and require the measurement to follow.
- **The failure message must say what a unit is and what to do.** A bare "21.9 calibration units"
  names something defined only in the file the reader is not looking at. And do not put a claim
  about the bound's derivation in a shared `describe()` — on that branch the sentence was true of
  two of ten call sites, including two LOWER bounds where it was exactly backwards.

## Mutation check

Use `.claude/skills/atmofab-review-loop/scripts/mutation_check.py`. The procedure is owned by
`atmofab-review-loop`'s "Before you hand it over (round 0)".

## Measuring what a Claude Code TOOL actually reaches

`scripts/measure_claude_tool.py` in this skill. Use it whenever a rule you are writing,
deleting or narrowing rests on **what a vendor tool can reach** — which paths a search
tool walks, whether a filter can leave its root, whether a spelling is inert.

    python3 .claude/skills/atmofab-enforcement-change/scripts/measure_claude_tool.py

It drives the real tool through a loopback stand-in for the Messages endpoint (no model
turn, so nothing is billed), in a fixture where every location a pattern could resolve to
holds a marked file, and each row DECLARES whether it must read or must be inert. It exits
non-zero on any disagreement, so it can be run without classifying rows by eye.

**Why a script and not a probe you write on the spot.** Issue #71's `Glob` question was
answered wrongly four times before this existed — Python's `glob` twice, bare `ripgrep`
once, and a hand-written driver whose fixture could not tell "the tool is confined" from
"the target was absent". Every one of those was written down as a measurement of the tool.
Rule 1-b says a deletion needs an execution record; this is what makes that record
re-takeable by the next person instead of a sentence they have to trust.

`tools/tests/test_measure_claude_tool.py` pins its case-list coverage, fixture saturation,
result detector and verdict — a harness with no witness gets broken again, and this one had
four faults in that layer found in a single review round.
