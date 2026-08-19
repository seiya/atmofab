# Verification procedures (met-dsl)

Run these from the met-dsl checkout root. **Do not write an assertion into a commit or TODO.md
that you have not measured.**

These are an operator's own dev-session commands. `AGENTS.md` §MCP execution rules — run
`compile` / `run` / checks through the MCP server, avoid direct shell execution — governs what a
WORKFLOW does; it is not a rule against running `pytest`, `ruff`, or a compiler probe by hand
while developing this repository.

## The suite

```bash
TMPDIR=/dev/shm python3 -m pytest tools/tests/ -q -p no:randomly
```

- **Never run two of these in parallel.** They share `TMPDIR`, which produces false failures
  (hit for real once)
- The baseline is the measured value on `origin/main`. If you compare via `git worktree` /
  `git archive`, placing the checkout outside `$HOME` makes
  `test_hooks_common.py::ForbidBackendCredentialReadTests` fail (it assumes `../..` / `~`
  resolve inside home; a path-depth-dependent pre-existing behaviour, identical on `main` and on
  a branch). **Compare the DELTA, not absolute values**
- **This count rots. Re-measure before handing anything over.** On 2026-08-13 it was 5; the
  **2026-08-18 measurement was 2 failed + 1 skipped**
  (`test_blocks_bash_only_tilde_prefixes` / `test_directory_options_anchor_like_cd`, identical on
  `origin/main`). It changes whenever tests are added or removed, so **do not quote this line's
  numbers — measure once yourself**
- **Put it in the prose you hand reviewers, too.** In PR #57 three reviewers each re-derived it,
  and one corrected my claim with its own measurement. Writing it down settles it. Conversely,
  **hand over a stale count and reviewers take it for the baseline and miss a real failure**
- **Skips: measure, do not assume.** On 2026-08-19 the suite reported 4877 passed and **0
  skipped**; every skip in `tools/tests/` is conditional (gfortran, bwrap, `/proc`), so what you see
  depends on the machine. An earlier version of this line claimed one permanent calibration skip
  and sent a reader hunting a test that does not exist

## Whole-tree diff (mandatory whenever you change a gate)

**The single most effective check in this repo.** Point the gate at the real corpus and compare
the verdicts before and after. Across 19 commits and 9 rounds, L128 could confirm "29→27, and
not one file under `problem/` changes verdict" at every commit, and reviewers reproduced it
independently. **It shows "nothing is broken" as a diff instead of an argument.**

```bash
# 1. write a scanner that calls the gate directly and dump JSON (file -> violation list)
#    fix the gate's premises explicitly (node_key, dep_spec_ids, …)
python3 corpus_scan.py > after.json
# 2. run the SAME scanner against the baseline in a throwaway worktree
git worktree add -q --detach ~/.cache/corpus-base origin/main
cp corpus_scan.py ~/.cache/corpus-base/
(cd ~/.cache/corpus-base && python3 corpus_scan.py) > before.json
git worktree remove ~/.cache/corpus-base
# 3. report which violation in which file moved, not the counts
```

**Do not reach for `git stash` here.** On a clean tree — which this loop's own commit discipline
requires before you measure anything — `git stash` is a **silent no-op**: it exits 0, creates no
entry, and both scans read the same bytes, so the diff is empty and the check reports that nothing
changed verdict. A trailing `git stash pop` then pops whatever unrelated entry was already on the
stack into your checkout. Copying the scanner into the worktree is not incidental either: the same
harness has to run on both sides, or what you measure includes the harness.

- **Always record the harness.** Changing how dep_spec_ids are derived changes the absolute
  numbers (29→27 / 31→29 / 35→33 on the same tree). **The diff reproduces; the absolute values are
  harness-dependent.** If a number goes into TODO or a commit, the derivation goes with it
- **Look down to subroutine granularity.** Per-file flag/silent alone hides one violation
  disappearing while another appears in the same file (this was actually missed)
- Measure changes in the fail-closed direction (more refusals) the same way. L128 showed **the
  module dep map for 357 directories byte-identical and 103/103 Makefile verdicts unchanged**

## ruff shows "identical to the baseline"

Compare per file, not by count. **Add a file to the comparison and the baseline must be retaken**
— an "identical, 1 finding" measured over two files was written unchanged after a third file was
added, and was wrong.

```bash
for f in <touched files>; do echo -n "$f: "; ruff check "$f" 2>&1 | tail -1; done
git worktree add -q --detach ~/.cache/ruff-base origin/main
for f in <touched files>; do echo -n "$f: "; ruff check ~/.cache/ruff-base/"$f" 2>&1 | tail -1; done
git worktree remove ~/.cache/ruff-base
```

Same reason as above, and one of this loop's own rules besides: the earlier form used `git stash`
plus `git checkout -- .`, and `metdsl-review-loop` forbids `git checkout -- <path>` by name for
discarding uncommitted work along with whatever it was meant to revert.

## Doc size ceilings

Docs that enter a leaf's context have ceiling tests. After touching
`docs/workflow/phases/*.md`:

```bash
TMPDIR=/dev/shm python3 -m pytest tools/tests/test_orchestration_runtime.py -q -p no:randomly -k child_context_docs
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
for several rounds (it ended at 37). **As measured on 2026-08-13, 4 of the 9 docs had headroom
under 50** (`workflow-generate-generate` +6, `workflow-generate-verify` +5, `AGENT_CONTRACT` +47,
`phase_01_compile` +50). **One added sentence fails those.** Measure before touching them.

## End to end through a real server process

Confirm through `mcp_call.py` rather than `import`, so the JSON-RPC layer and the handling of the
environment are included.

```bash
# standalone works
env -u METDSL_WORKFLOW_MODE -u METDSL_ORCHESTRATION_ID \
  python3 mcp_servers/mcp_call.py --tool run_syntax_check --args-json '{"project_dir": "<abs>"}'
# under the workflow, dropping orchestration_id is refused
METDSL_WORKFLOW_MODE=1 python3 mcp_servers/mcp_call.py --tool run_linter --args-json '{"project_dir": "<abs>"}'
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

## Mutation check

Use `.claude/skills/metdsl-review-loop/scripts/mutation_check.py`. The procedure is owned by
`metdsl-review-loop`'s "Before you hand it over (round 0)".
