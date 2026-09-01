#!/usr/bin/env python3
"""Scenario tests for the review instrument `mutation_check.py`.

The script reverts each hunk of a change in a throwaway worktree, runs a test command, and
reports the hunks no test noticed. Its verdicts are what an operator uses to decide a change
is pinned, so a defect in it produces false confidence about someone else's work — and the
dangerous direction is always the same one: reporting that something was tested when nothing
was.

Why this file exists. The script arrived in this repository with no automated coverage, and
six review rounds found eight wrong verdicts in it, every one by an agent constructing a
scenario by hand:

  * `git checkout -- .` between hunks left files an earlier revert had CREATED, so the next
    hunk's suite failed for the leftover and was scored `killed` — an unpinned change reported
    as pinned, with the answer depending on `--jobs`;
  * a hunk carrying a rename was judged by the reversed rename, not by the hunk;
  * a change with no revertible hunk (pure rename, binary, mode, empty new file) fell into the
    same "nothing to check" line as a wrong base and exited 0;
  * `#` was read as a comment in every file type and position — Markdown headings, c/cpp
    directives, shebangs, and text inside Python string literals and YAML block scalars, which
    is exactly the prompt-template text this repository pins;
  * CRLF and non-UTF-8 files were skipped wholesale, by this script's own encoding handling;
  * a baseline hitting `--timeout` crashed with a traceback and exited 1, the code that means
    "hunks survived";
  * a path containing ` b/`, or one git quotes, was mis-parsed, silently disabling the filters.

Each is a test below. The point is not the eight instances: it is that the script had no
witness, and this repository's own rule is that a mechanism with no witness gets broken again.
Nearly every test drives the script as a subprocess over a real git repository, because that is
the only way to observe what it reports; asserting on its helpers would reproduce the shape of
defect it keeps having, where a helper is correct and the run is not. The exception is
`DiffEntryPathTests`, four unit tests over one pure parser whose inputs — git's quoting of a
non-ASCII path, a directory named `pkg b` — are laborious to build as repositories and trivial
to state as strings. Both shapes it covers are exercised end to end as well — that sentence was
false for the quoted non-ASCII path until a scenario was added for it.

**Writing a scenario is not the same as witnessing a mechanism, and this file has been wrong
about that twice.** A test that PINS the change under test can never reach the prose classifier,
because only survivors are classified: three "`#` is not a comment" tests were structurally
incapable of failing until an unpinned variant was added. And the carry-over pair needs two
hunks in ONE file to exercise the tracked-file restore — the version with two separate files
witnessed only half of it. Every mechanism claimed here was confirmed by reverting it and
watching a named test fail.

What these tests do NOT cover, from two censuses run against them by independent reviewers —
187 mutations (96 killed) and then 248 (152 killed), with every survivor below demonstrated to be
a real behaviour change rather than an equivalent mutant. Three decisions added AFTER those
censuses are unwitnessed too, and all three fail toward over-reporting: the VALUE of
`_SUMMARY_TAIL_LINES` (1 and 2 both pass the suite — real pytest puts its counts on the last
line, so the margin is a margin and not a mechanism), the loosening of `_TESTS_DID_FAIL_RE` that
went with it, and the TMPDIR lookbehind as a whole class rather than member by member. The
censused list: the `ast.AsyncFunctionDef` arm of the
docstring blanking; the `git apply -R` refusal branch of
SKIP — no scenario was found that produces it at HEAD; `--keep`, `--skip-baseline`, `--workdir`
placement and the per-job temp roots; a range whose right side is not HEAD; the mode-change and
empty-new-file hunkless shapes; symlinks; a repo-local `diff.noprefix`; the individual members
of the TMPDIR lookbehind class and of the diff-header prefix list; the SKIPPED and INCONCLUSIVE
summary paragraphs (the tests read the per-hunk verdict line instead); and every message's
wording beyond the substrings asserted. Two known behaviours are also unwitnessed and left
deliberately: a prose-only change in a NEW file is reported as an unexplained survivor rather
than labelled, and a comment-only change in a non-UTF-8 module is too, because neither side of
the AST comparison parses. Both over-report, which is the safe direction.

A green run here means the defects listed above stay fixed and the exit-code contract holds —
not that the instrument is correct.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

SCRIPT = (Path(__file__).resolve().parents[2]
          / ".claude" / "skills" / "metforge-review-loop" / "scripts" / "mutation_check.py")

#: Neutralise the operator's git configuration for every git this file runs, its own and the
#: script's. Measured on one developer machine's plausible settings: `commit.gpgsign=true` fails
#: 23 of these tests in `git commit`, a `core.excludesFile` listing `*.py` fails 19 in `git add`,
#: `diff.renames=false` fails both rename tests, and `core.autocrlf=true` makes the CRLF test
#: pass while measuring nothing, because git normalises the file on commit. A witness that
#: depends on whose machine it runs on is not one.
_NO_GIT_CONFIG = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
                  "GIT_CONFIG_NOSYSTEM": "1"}

#: A `--test-cmd` that passes only while the given text is present in the given file. Simpler
#: than running pytest inside pytest, and it makes "pinned" mean exactly one thing.
def _pins(path: str, text: str) -> str:
    return (f"{sys.executable} -c \"import pathlib,sys;"
            f"sys.exit(0 if {text!r} in pathlib.Path({path!r}).read_text(errors='replace')"
            f" else 1)\"")


_ALWAYS_PASSES = f"{sys.executable} -c \"pass\""


class _Repo:
    """A throwaway git repository, committed to with a fixed identity."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.git("init", "-q")

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
            cwd=self.root, text=True, capture_output=True, check=True,
            env={**os.environ, **_NO_GIT_CONFIG})

    def write(self, rel: str, data: str | bytes) -> None:
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            target.write_bytes(data)
        else:
            target.write_text(data)

    def commit(self, message: str) -> None:
        self.git("add", "-A")
        self.git("commit", "-qm", message)


class _Run:
    """One invocation of the script, with its output and exit code."""

    def __init__(self, proc: subprocess.CompletedProcess[str]) -> None:
        self.code = proc.returncode
        self.out = proc.stdout + proc.stderr

    def __str__(self) -> str:  # shown when an assertion fails
        return f"exit={self.code}\n{self.out}"


class MutationCheckScenarioTests(unittest.TestCase):
    """Drive the script over a real repository, one change shape per test."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        (self.tmp / "repo").mkdir()
        self.repo = _Repo(self.tmp / "repo")
        self.workdir = self.tmp / "wd"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_check(self, *extra: str, rng: str = "HEAD~1..HEAD",
                  test_cmd: str = _ALWAYS_PASSES) -> _Run:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--range", rng, "--test-cmd", test_cmd,
             "--jobs", "1", "--workdir", str(self.workdir), *extra],
            cwd=self.repo.root, text=True, capture_output=True,
            env={**os.environ, **_NO_GIT_CONFIG, "TMPDIR": str(self.tmp)})
        return _Run(proc)

    # --- the verdicts themselves -------------------------------------------------------------

    def test_a_pinned_hunk_is_killed_and_the_run_is_clean(self) -> None:
        self.repo.write("k.py", "x = 1\n")
        self.repo.commit("base")
        self.repo.write("k.py", "x = 2\n")
        self.repo.commit("change")
        run = self.run_check(test_cmd=_pins("k.py", "x = 2"))
        self.assertIn("killed", run.out, msg=str(run))
        self.assertIn("every hunk is pinned", run.out, msg=str(run))
        self.assertEqual(0, run.code, msg=str(run))

    def test_an_unpinned_hunk_survives_and_fails_the_run(self) -> None:
        self.repo.write("k.py", "x = 1\n")
        self.repo.commit("base")
        self.repo.write("k.py", "x = 2\n")
        self.repo.commit("change")
        run = self.run_check()
        self.assertIn("SURVIVED", run.out, msg=str(run))
        self.assertEqual(1, run.code, msg=str(run))

    def test_a_deleted_file_does_not_carry_into_the_next_hunk(self) -> None:
        """The `git clean` fix: without it the deletion's revert leaves the file behind, the
        NEXT hunk's command fails for that leftover, and an unpinned change is scored `killed`.

        The names matter. Hunks are checked in the order the diff lists them, which is
        alphabetical, so the hunk that recreates a file has to sort BEFORE the hunk that must
        stay unpinned. The first version of this test named them the other way round and the
        mutation removing `git clean` survived it.
        """
        self.repo.write("a_legacy.py", "old = 1\n")
        self.repo.write("z_target.py", "x = 1\n")
        self.repo.commit("base")
        (self.repo.root / "a_legacy.py").unlink()
        self.repo.commit("delete legacy")
        self.repo.write("z_target.py", "x = 2\n")
        self.repo.commit("unpinned change")
        run = self.run_check(
            rng="HEAD~2..HEAD",
            test_cmd=f"{sys.executable} -c \"import pathlib,sys;"
                     f"sys.exit(1 if pathlib.Path('a_legacy.py').exists() else 0)\"")
        self.assertIn("SURVIVED", run.out, msg=str(run))
        self.assertNotIn("every hunk is pinned", run.out, msg=str(run))
        self.assertEqual(1, run.code, msg=str(run))

    def test_a_hunk_carrying_a_rename_is_skipped_not_judged(self) -> None:
        """Only when git DETECTS the rename, which is what the fixture's shared body buys:
        below the similarity threshold git reports a delete plus an add, two ordinary hunks."""
        body = "".join(f"value_{n} = {n}\n" for n in range(20))
        self.repo.write("src/a.py", f'"""doc a."""\n{body}')
        self.repo.commit("base")
        self.repo.git("mv", "src/a.py", "src/b.py")
        self.repo.write("src/b.py", f'"""doc b."""\n{body}')
        self.repo.commit("rename and edit")
        run = self.run_check()
        self.assertIn("SKIP (carries a rename", run.out, msg=str(run))
        self.assertEqual(1, run.code, msg=str(run))

    def test_two_hunks_in_one_file_are_judged_independently(self) -> None:
        """The other half of the carry-over fix, and the half that had no witness.

        `git clean` removes what a revert CREATED; `git checkout -- .` restores what it
        MODIFIED. Only the first was tested, and deleting the second reproduced the original
        false green verbatim: the first hunk's revert stayed in the tree, the second hunk's
        command failed for it, and an unpinned change was scored `killed` with a clean exit.
        Two hunks in one file is the most common change shape there is.
        """
        filler = "".join(f"pad_{n} = {n}\n" for n in range(30))
        self.repo.write("a.py", f"first = 1\n{filler}second = 1\n")
        self.repo.commit("base")
        self.repo.write("a.py", f"first = 2\n{filler}second = 2\n")
        self.repo.commit("two hunks, one pinned")
        run = self.run_check(test_cmd=_pins("a.py", "first = 2"))
        self.assertIn("killed", run.out, msg=str(run))
        self.assertIn("SURVIVED", run.out, msg=str(run))
        self.assertNotIn("every hunk is pinned", run.out, msg=str(run))
        self.assertEqual(1, run.code, msg=str(run))

    def test_hunks_are_judged_independently_when_a_worktree_is_recycled(self) -> None:
        """The carry-over bug's defining symptom was a verdict that changed with the job count,
        so the parallel case needs MORE hunks than jobs — three at `--jobs 2`, which forces one
        worktree to take a second hunk. The first version used two hunks at two jobs, where
        nothing is ever recycled, and both carry-over mutations survived it."""
        filler = "".join(f"pad_{n} = {n}\n" for n in range(30))
        self.repo.write("a.py", f"first = 1\n{filler}second = 1\n{filler}third = 1\n")
        self.repo.commit("base")
        self.repo.write("a.py", f"first = 2\n{filler}second = 2\n{filler}third = 2\n")
        self.repo.commit("three hunks, one pinned")
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--range", "HEAD~1..HEAD", "--jobs", "2",
             "--workdir", str(self.workdir), "--test-cmd", _pins("a.py", "first = 2")],
            cwd=self.repo.root, text=True, capture_output=True,
            env={**os.environ, **_NO_GIT_CONFIG, "TMPDIR": str(self.tmp)})
        run = _Run(proc)
        # BOTH unpinned hunks must survive. Asserting that one of them did is what let the
        # carry-over mutations through: whichever hunk inherited the recycled worktree came
        # back `killed`, and the other one's SURVIVED satisfied the assertion.
        self.assertEqual(2, run.out.count("SURVIVED"), msg=str(run))
        self.assertEqual(1, run.code, msg=str(run))

    def test_a_hunk_that_times_out_is_inconclusive_not_killed(self) -> None:
        """Only the baseline timeout was witnessed; a hunk that times out was free to be
        scored `killed`, which says a test noticed a change while nothing ran."""
        self.repo.write("k.py", "x = 1\n")
        self.repo.commit("base")
        self.repo.write("k.py", "x = 2\n")
        self.repo.commit("change")
        # The baseline passes fast; only the reverted state sleeps past the timeout.
        cmd = (f"{sys.executable} -c \"import pathlib,time,sys;"
               f"time.sleep(30) if 'x = 1' in pathlib.Path('k.py').read_text() else sys.exit(0)\"")
        run = self.run_check("--timeout", "2", test_cmd=cmd)
        # Read the per-hunk verdict line, not the summary paragraph below it: the paragraph
        # names `--timeout` in its remedy, so asserting on the word alone passed even with the
        # word removed from the verdict.
        verdict = next(line for line in run.out.splitlines() if "INCONCLUSIVE" in line)
        self.assertIn("--timeout", verdict, msg=str(run))
        self.assertEqual(1, run.code, msg=str(run))

    def test_a_deleted_directory_does_not_carry_into_the_next_hunk(self) -> None:
        """`git clean -qfdx`, one flag at a time: without `-d` the DIRECTORY a revert recreates
        stays, and the next hunk's command fails for it — the file-level version of this test
        passes with `-fx`, so the `-d` had no witness."""
        self.repo.write("pkg/legacy.py", "old = 1\n")
        self.repo.write("z_target.py", "x = 1\n")
        self.repo.commit("base")
        (self.repo.root / "pkg" / "legacy.py").unlink()
        (self.repo.root / "pkg").rmdir()
        self.repo.commit("delete the package")
        self.repo.write("z_target.py", "x = 2\n")
        self.repo.commit("unpinned change")
        run = self.run_check(
            rng="HEAD~2..HEAD",
            test_cmd=f"{sys.executable} -c \"import pathlib,sys;"
                     f"sys.exit(1 if pathlib.Path('pkg').exists() else 0)\"")
        self.assertIn("SURVIVED", run.out, msg=str(run))
        self.assertNotIn("every hunk is pinned", run.out, msg=str(run))
        self.assertEqual(1, run.code, msg=str(run))

    def test_a_skipped_hunk_fails_the_run_beside_a_prose_survivor(self) -> None:
        """The survivor branch has its own exit expression, and `skipped` in it was unwitnessed:
        a SKIP beside a prose-only survivor exited 0 with "NOTHING was tested" printed."""
        body = "".join(f"value_{n} = {n}\n" for n in range(20))
        self.repo.write("src/a.py", f'"""doc a."""\n{body}')
        self.repo.write("m.py", '"""doc."""\nx = 1\n')
        self.repo.commit("base")
        self.repo.git("mv", "src/a.py", "src/b.py")
        self.repo.write("src/b.py", f'"""doc b."""\n{body}')
        self.repo.write("m.py", '"""doc changed."""\nx = 1\n')
        self.repo.commit("rename plus a docstring")
        run = self.run_check()
        self.assertIn("SKIP", run.out, msg=str(run))
        self.assertIn("prose-only", run.out, msg=str(run))
        self.assertEqual(1, run.code, msg=str(run))

    def test_an_inconclusive_hunk_fails_the_run_beside_a_prose_survivor(self) -> None:
        """Same branch, the `inconclusive` term."""
        self.repo.write("m.py", '"""doc."""\nx = 1\n')
        self.repo.write("z.py", "y = 1\n")
        self.repo.commit("base")
        self.repo.write("m.py", '"""doc changed."""\nx = 1\n')
        self.repo.write("z.py", "y = 2\n")
        self.repo.commit("a docstring and a change")
        cmd = (f"{sys.executable} -c \"import pathlib,sys;"
               f"print('ERROR collecting z.py');"
               f"sys.exit(2 if 'y = 1' in pathlib.Path('z.py').read_text() else 0)\"")
        run = self.run_check(test_cmd=cmd)
        self.assertIn("INCONCLUSIVE", run.out, msg=str(run))
        self.assertIn("prose-only", run.out, msg=str(run))
        self.assertEqual(1, run.code, msg=str(run))

    # --- changes with nothing to revert ------------------------------------------------------

    def test_a_pure_rename_beside_a_pinned_hunk_still_fails_the_run(self) -> None:
        """The shape that read as a pass: the rename is listed, the hunk is pinned, and the
        exit code used to be 0 because the hunkless list was printed and then forgotten."""
        self.repo.write("old.py", "f = 1\n")
        self.repo.write("k.py", "x = 1\n")
        self.repo.commit("base")
        self.repo.git("mv", "old.py", "new.py")
        self.repo.write("k.py", "x = 2\n")
        self.repo.commit("rename plus change")
        run = self.run_check(test_cmd=_pins("k.py", "x = 2"))
        self.assertIn("no revertible hunk", run.out, msg=str(run))
        self.assertIn("new.py", run.out, msg=str(run))
        self.assertIn("not a clean run", run.out, msg=str(run))
        self.assertEqual(1, run.code, msg=str(run))

    def test_a_binary_change_is_reported_rather_than_silently_skipped(self) -> None:
        self.repo.write("blob.bin", bytes(range(0, 200)))
        self.repo.commit("base")
        self.repo.write("blob.bin", bytes(range(50, 250)))
        self.repo.commit("binary change")
        run = self.run_check()
        self.assertIn("no revertible hunk", run.out, msg=str(run))
        self.assertIn("blob.bin", run.out, msg=str(run))
        self.assertEqual(1, run.code, msg=str(run))

    def test_reported_failures_outrank_a_collection_marker(self) -> None:
        """A run that reports failures observed something, whatever else went wrong beside it.

        Reading the marker alone made the remedy this script prints useless: with
        `--continue-on-collection-errors` pytest keeps the same `ERROR collecting` line AND runs
        the rest, so a hunk its tests really killed came back INCONCLUSIVE however many times
        the reader followed the advice.
        """
        self.repo.write("k.py", "x = 1\n")
        self.repo.commit("base")
        self.repo.write("k.py", "x = 2\n")
        self.repo.commit("change")
        cmd = (f"{sys.executable} -c \"import pathlib,sys;"
               f"reverted = 'x = 1' in pathlib.Path('k.py').read_text();"
               f"print('ERROR collecting other.py');"
               f"print('== 3 failed, 41 passed in 2s ==') if reverted else None;"
               f"sys.exit(1 if reverted else 0)\"")
        run = self.run_check("--skip-baseline", test_cmd=cmd)
        self.assertIn("killed", run.out, msg=str(run))
        self.assertNotIn("INCONCLUSIVE", run.out, msg=str(run))

    def test_a_failure_count_in_the_body_does_not_outrank_the_marker(self) -> None:
        """The counts are read from the tail. A test that PRINTS a failure count — a captured
        log, an assertion message quoting one — decided the verdict from the body before that,
        so a hunk nothing ran for came back `killed`."""
        self.repo.write("k.py", "x = 1\n")
        self.repo.commit("base")
        self.repo.write("k.py", "x = 2\n")
        self.repo.commit("change")
        cmd = (f"{sys.executable} -c \"import sys;"
               f"print('captured log: 3 failed, 41 passed in 2s');"
               f"[print('collecting ...') for _ in range(8)];"
               f"print('ERROR collecting tools/tests/x.py');"
               f"print('1 error in 0.1s');sys.exit(2)\"")
        run = self.run_check("--skip-baseline", test_cmd=cmd)
        self.assertIn("INCONCLUSIVE", run.out, msg=str(run))
        self.assertEqual(1, run.code, msg=str(run))

    def test_a_new_file_hunk_is_measured(self) -> None:
        """No test added a file, so reverting an addition — which DELETES the file, the case the
        classifier's `OSError` arm exists for — was never driven end to end."""
        self.repo.write("k.py", "x = 1\n")
        self.repo.commit("base")
        self.repo.write("added.py", "y = 1\n")
        self.repo.commit("add a module nobody imports")
        run = self.run_check()
        self.assertIn("added.py", run.out, msg=str(run))
        self.assertIn("SURVIVED", run.out, msg=str(run))
        self.assertNotIn("Traceback", run.out, msg=str(run))
        self.assertEqual(1, run.code, msg=str(run))

    def test_each_marker_of_a_suite_that_never_ran_is_recognised(self) -> None:
        """Five markers, each its own decision: only one was witnessed, and dropping any of the
        other four turned "nothing ran" into `killed` — the false green this detector exists
        for. The last case also proves the markers are read from stderr, not stdout alone."""
        self.repo.write("k.py", "x = 1\n")
        self.repo.commit("base")
        self.repo.write("k.py", "x = 2\n")
        self.repo.commit("change")
        cases = [("stdout", "ERROR collecting tools/tests/x.py"),
                 ("stdout", "2 errors during collection"),
                 ("stdout", "1 error during collection"),
                 ("stdout", "!!!!! Interrupted: 1 error !!!!!"),
                 ("stdout", "no tests ran in 0.01s"),
                 ("stderr", "ERROR collecting tools/tests/x.py")]
        for stream, marker in cases:
            with self.subTest(stream=stream, marker=marker):
                target = "sys.stderr" if stream == "stderr" else "sys.stdout"
                cmd = (f"{sys.executable} -c \"import sys;"
                       f"print({marker!r}, file={target});sys.exit(2)\"")
                run = self.run_check("--skip-baseline", test_cmd=cmd)
                self.assertIn("INCONCLUSIVE", run.out, msg=str(run))
                self.assertEqual(1, run.code, msg=str(run))

    # --- what counts as prose ----------------------------------------------------------------

    def test_a_python_comment_change_is_checked_and_labelled(self) -> None:
        self.repo.write("m.py", "# old\nx = 1\n")
        self.repo.commit("base")
        self.repo.write("m.py", "# new\nx = 1\n")
        self.repo.commit("comment")
        run = self.run_check()
        self.assertIn("prose-only", run.out, msg=str(run))
        self.assertEqual(0, run.code, msg=str(run))

    def test_a_hash_line_inside_a_python_string_is_not_prose(self) -> None:
        """`#` inside a string literal is content — and prompt-template text in exactly this
        shape is pinned by this repository. It must be checked, and it must not be labelled."""
        self.repo.write("m.py", 'TEMPLATE = """\nrule\n"""\n')
        self.repo.commit("base")
        self.repo.write("m.py", 'TEMPLATE = """\nrule\n# Rule: never\n"""\n')
        self.repo.commit("add a line to the template")
        run = self.run_check(test_cmd=_pins("m.py", "# Rule: never"))
        # Only the "it is checked at all" half is assertable here: the hunk is pinned, so there
        # are no survivors and the classifier never runs. Its labelling is witnessed by the
        # unpinned sibling below.
        self.assertIn("killed", run.out, msg=str(run))
        self.assertEqual(0, run.code, msg=str(run))

    def test_a_markdown_heading_change_is_checked(self) -> None:
        """`#` opens a heading, and `test_cli_reference_sync` / `test_readme_sync` read `##`
        sections out of committed documents, so a heading round is a pinned round here."""
        self.repo.write("guide.md", "# Guide\n\n## Old section\n\ntext\n")
        self.repo.commit("base")
        self.repo.write("guide.md", "# Guide\n\n## New section\n\ntext\n")
        self.repo.commit("rename the heading")
        run = self.run_check(test_cmd=_pins("guide.md", "## New section"))
        self.assertIn("killed", run.out, msg=str(run))
        self.assertNotIn("nothing to check", run.out, msg=str(run))
        self.assertEqual(0, run.code, msg=str(run))

    def test_an_unpinned_markdown_change_is_reported_unlabelled(self) -> None:
        """The prose label is an AST comparison, so it must not reach a file that is not
        Python. Markdown often parses AS Python — `# Guide` is a comment, `text` is an
        expression — so without the suffix guard a heading change compares equal and is
        labelled "expected", which turns an unpinned change into a clean exit 0.
        """
        self.repo.write("guide.md", "# Guide\n\n## Old section\n\ntext\n")
        self.repo.commit("base")
        self.repo.write("guide.md", "# Guide\n\n## New section\n\ntext\n")
        self.repo.commit("heading nobody pins")
        run = self.run_check()
        self.assertIn("SURVIVED", run.out, msg=str(run))
        self.assertNotIn("prose-only", run.out, msg=str(run))
        self.assertEqual(1, run.code, msg=str(run))

    def test_a_preprocessor_directive_change_is_checked(self) -> None:
        self.repo.write("m.c", "#include <stdio.h>\n#define L 10\n")
        self.repo.commit("base")
        self.repo.write("m.c", "#include <stdlib.h>\n#define L 99\n")
        self.repo.commit("directives")
        run = self.run_check(test_cmd=_pins("m.c", "#define L 99"))
        self.assertIn("killed", run.out, msg=str(run))
        self.assertEqual(0, run.code, msg=str(run))

    def test_a_shebang_change_is_checked(self) -> None:
        self.repo.write("s.sh", "#!/bin/sh\necho hi\n")
        self.repo.commit("base")
        self.repo.write("s.sh", "#!/usr/bin/env bash\necho hi\n")
        self.repo.commit("shebang")
        run = self.run_check(test_cmd=_pins("s.sh", "env bash"))
        self.assertIn("killed", run.out, msg=str(run))
        self.assertEqual(0, run.code, msg=str(run))

    def test_a_file_python_cannot_parse_is_not_labelled_prose(self) -> None:
        """`_stripped` returns None for an unparseable module, and both sides must be checked:
        a version returning "" made the two compare equal, so a real change in a file Python
        cannot parse was labelled expected prose and the run exited 0."""
        self.repo.write("broken.py", "VALUE = 1\ndef (:\n")
        self.repo.commit("base")
        self.repo.write("broken.py", "VALUE = 2\ndef (:\n")
        self.repo.commit("change nobody pins")
        run = self.run_check()
        self.assertIn("SURVIVED", run.out, msg=str(run))
        self.assertNotIn("prose-only", run.out, msg=str(run))
        self.assertEqual(1, run.code, msg=str(run))

    def test_a_deleted_python_file_is_not_labelled_prose(self) -> None:
        """The file is not at head, so `git show` fails; treating that as prose labelled a
        whole deleted module "expected" and exited 0."""
        self.repo.write("doomed.py", '"""doc."""\nx = 1\n')
        self.repo.commit("base")
        (self.repo.root / "doomed.py").unlink()
        self.repo.commit("delete it")
        run = self.run_check()
        self.assertIn("SURVIVED", run.out, msg=str(run))
        self.assertNotIn("prose-only", run.out, msg=str(run))
        self.assertEqual(1, run.code, msg=str(run))

    def test_a_docstring_inside_a_function_is_prose_too(self) -> None:
        """Every fixture used a MODULE docstring, so the class and function arms of the AST
        blanking had no witness — and those are the commonest prose hunks there are."""
        self.repo.write("m.py", 'class C:\n    """old class doc."""\n\n'
                                'def f():\n    """old doc."""\n    return 1\n')
        self.repo.commit("base")
        self.repo.write("m.py", 'class C:\n    """new class doc."""\n\n'
                                'def f():\n    """new doc."""\n    return 1\n')
        self.repo.commit("docstrings")
        run = self.run_check()
        self.assertIn("prose-only", run.out, msg=str(run))
        self.assertEqual(0, run.code, msg=str(run))

    # --- encodings ---------------------------------------------------------------------------

    def test_a_crlf_file_is_measured_rather_than_skipped(self) -> None:
        self.repo.write("c.txt", b"a\r\nb\r\n")
        self.repo.commit("base")
        self.repo.write("c.txt", b"a\r\nZ\r\n")
        self.repo.commit("crlf change")
        run = self.run_check()
        self.assertNotIn("SKIP", run.out, msg=str(run))
        self.assertNotIn("Traceback", run.out, msg=str(run))
        self.assertIn("SURVIVED", run.out, msg=str(run))
        self.assertEqual(1, run.code, msg=str(run))

    def test_a_non_utf8_file_is_measured_rather_than_skipped(self) -> None:
        self.repo.write("m.py", b"# caf\xe9\nx = 1\n")
        self.repo.commit("base")
        self.repo.write("m.py", b"# caf\xe9\nx = 2\n")
        self.repo.commit("latin-1 change")
        run = self.run_check()
        self.assertNotIn("SKIP", run.out, msg=str(run))
        self.assertNotIn("Traceback", run.out, msg=str(run))
        self.assertIn("SURVIVED", run.out, msg=str(run))
        self.assertEqual(1, run.code, msg=str(run))

    def test_a_quoted_non_ascii_path_is_handled_end_to_end(self) -> None:
        """git quotes such a path in the `diff --git` header, so there is no ` b/` to split on
        at all and every filter silently stops applying to it. The unit test over the parser
        covers the header; this covers the run."""
        self.repo.write("日.py", '"""doc."""\nx = 1\n')
        self.repo.commit("base")
        self.repo.write("日.py", '"""doc changed."""\nx = 1\n')
        self.repo.commit("docstring")
        run = self.run_check()
        self.assertIn("日.py", run.out, msg=str(run))
        self.assertIn("prose-only", run.out, msg=str(run))
        self.assertEqual(0, run.code, msg=str(run))

    def test_a_path_containing_the_b_separator_is_named_correctly(self) -> None:
        """`split(" b/")` took the last occurrence, so this file was reported under a garbled
        name and its prose classification failed, surfacing as a false unexplained survivor."""
        self.repo.write("pkg b/lib.py", '"""doc."""\nx = 1\n')
        self.repo.commit("base")
        self.repo.write("pkg b/lib.py", '"""doc changed."""\nx = 1\n')
        self.repo.commit("docstring")
        run = self.run_check()
        self.assertIn("pkg b/lib.py", run.out, msg=str(run))
        self.assertIn("prose-only", run.out, msg=str(run))
        self.assertEqual(0, run.code, msg=str(run))

    def test_an_unpinned_hash_line_inside_a_string_is_not_labelled_prose(self) -> None:
        """The classifier only runs on SURVIVORS, so the pinned version of this scenario can
        never reach it: that test asserts the hunk was killed, which it would be either way.
        This one leaves the change unpinned, so the label — or its absence — is the verdict.
        Measured: with `_stripped` emptying every string constant, this test fails and the
        pinned one stays green.
        """
        self.repo.write("m.py", 'TEMPLATE = """\nrule\n"""\n')
        self.repo.commit("base")
        self.repo.write("m.py", 'TEMPLATE = """\nrule\n# Rule: never\n"""\n')
        self.repo.commit("add a line nobody pins")
        run = self.run_check()
        self.assertIn("SURVIVED", run.out, msg=str(run))
        self.assertNotIn("prose-only", run.out, msg=str(run))
        self.assertEqual(1, run.code, msg=str(run))

    def test_a_python_interface_stub_is_classified_like_a_module(self) -> None:
        """`.pyi` reaches the classifier because the suffix set says so — a live behaviour
        change from the `endswith(".py")` the script used to carry, and one no test made."""
        self.repo.write("m.pyi", '"""doc."""\nx: int\n')
        self.repo.commit("base")
        self.repo.write("m.pyi", '"""doc changed."""\nx: int\n')
        self.repo.commit("stub docstring")
        run = self.run_check()
        self.assertIn("prose-only", run.out, msg=str(run))
        self.assertEqual(0, run.code, msg=str(run))

    def test_an_untested_change_fails_the_run_beside_a_prose_survivor(self) -> None:
        """The hunkless term has two arms in the exit expression and only one was witnessed:
        the arm reached when the surviving hunks are all prose, where the run would otherwise
        exit 0 with the untested change printed at the top and forgotten."""
        self.repo.write("old.py", "f = 1\n")
        self.repo.write("m.py", '"""doc."""\nx = 1\n')
        self.repo.commit("base")
        self.repo.git("mv", "old.py", "new.py")
        self.repo.write("m.py", '"""doc changed."""\nx = 1\n')
        self.repo.commit("rename plus a docstring")
        run = self.run_check()
        self.assertIn("no revertible hunk", run.out, msg=str(run))
        self.assertIn("prose-only", run.out, msg=str(run))
        self.assertEqual(1, run.code, msg=str(run))

    # --- the exit-code contract --------------------------------------------------------------

    def test_a_red_baseline_exits_two(self) -> None:
        self.repo.write("k.py", "x = 1\n")
        self.repo.commit("base")
        self.repo.write("k.py", "x = 2\n")
        self.repo.commit("change")
        run = self.run_check(test_cmd=f"{sys.executable} -c \"raise SystemExit(1)\"")
        self.assertIn("BASELINE RED", run.out, msg=str(run))
        self.assertEqual(2, run.code, msg=str(run))

    def test_a_red_baseline_names_the_two_paths_this_harness_chose(self) -> None:
        """The message must accuse the HARNESS before the suite.

        Both of these defaults were once inputs to met-forge's own suite — a `/dev/shm` scratch
        root reddened the two tests that reason about `/dev/shm`, and the default worktree
        location reddens the hook tests that resolve `..` against the checkout's depth. The
        message said "Fix the suite", which is the one thing that was not wrong, so a full-suite
        `--test-cmd` looked impossible to run. What is pinned is that both levers are NAMED with
        their live values, not the wording around them.
        """
        self.repo.write("k.py", "x = 1\n")
        self.repo.commit("base")
        self.repo.write("k.py", "x = 2\n")
        self.repo.commit("change")
        run = self.run_check(test_cmd=f"{sys.executable} -c \"raise SystemExit(1)\"")
        self.assertIn("BASELINE RED", run.out, msg=str(run))
        self.assertIn("TMPDIR", run.out, msg=str(run))
        # Each lever asserted as ONE span joining its wording to its live value. Asserting the
        # two separately passed for the wrong reason: the run's first line already prints the
        # worktree root, so deleting the lever sentence left the assertion satisfied.
        self.assertIn(f"scratch root is {self.tmp}", run.out,
                      msg="the scratch root in force is not named by the message")
        self.assertIn(f"worktrees are under {self.workdir}", run.out,
                      msg="the worktree root in force is not named by the message")
        self.assertIn("DEPTH differs", run.out, msg=str(run))

    def test_the_scratch_root_falls_back_to_the_platform_not_to_dev_shm(self) -> None:
        """With TMPDIR unset, the root is `tempfile.gettempdir()`.

        Read out of the red-baseline message, which is the only place the script publishes it.
        The default used to be `/dev/shm` unconditionally, and a scratch root must not be a path
        the suite under test makes assertions about — which the script cannot know, so it takes
        the platform's answer rather than choosing one for speed it did not deliver.
        """
        self.repo.write("k.py", "x = 1\n")
        self.repo.commit("base")
        self.repo.write("k.py", "x = 2\n")
        self.repo.commit("change")
        env = {k: v for k, v in os.environ.items() if k != "TMPDIR"}
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--range", "HEAD~1..HEAD",
             "--test-cmd", f"{sys.executable} -c \"raise SystemExit(1)\"",
             "--jobs", "1", "--workdir", str(self.workdir)],
            cwd=self.repo.root, text=True, capture_output=True,
            env={**env, **_NO_GIT_CONFIG})
        out = proc.stdout + proc.stderr
        self.assertIn("BASELINE RED", out, msg=out[-2000:])
        self.assertIn(f"scratch root is {tempfile.gettempdir()}", out, msg=out[-2000:])
        self.assertNotIn("/dev/shm", out, msg=out[-2000:])

    def test_the_workdir_help_says_its_depth_is_load_bearing(self) -> None:
        """A reader who hits the depth failure looks at `--help` before the source."""
        proc = subprocess.run([sys.executable, str(SCRIPT), "--help"],
                              text=True, capture_output=True)
        # The REMEDY, not the word `DEPTH`: the help says "does NOT match your checkout's
        # DEPTH" a sentence earlier, so asserting the word alone was satisfied by the
        # diagnosis while the instruction that fixes it could be deleted unnoticed.
        # Whitespace collapsed: argparse re-wraps help text, so the phrase is split across
        # lines at a width nobody controls.
        flattened = " ".join(proc.stdout.split())
        self.assertIn("same depth as your checkout", flattened, msg=proc.stdout)

    def test_a_baseline_that_times_out_exits_two_without_a_traceback(self) -> None:
        self.repo.write("k.py", "x = 1\n")
        self.repo.commit("base")
        self.repo.write("k.py", "x = 2\n")
        self.repo.commit("change")
        run = self.run_check("--timeout", "1",
                             test_cmd=f"{sys.executable} -c \"import time;time.sleep(30)\"")
        self.assertIn("BASELINE TIMED OUT", run.out, msg=str(run))
        self.assertNotIn("Traceback", run.out, msg=str(run))
        self.assertEqual(2, run.code, msg=str(run))

    def test_a_range_that_does_not_resolve_exits_two(self) -> None:
        self.repo.write("k.py", "x = 1\n")
        self.repo.commit("base")
        run = self.run_check(rng="nosuchref...HEAD")
        self.assertIn("cannot run:", run.out, msg=str(run))
        self.assertNotIn("Traceback", run.out, msg=str(run))
        self.assertEqual(2, run.code, msg=str(run))
        # The failure here is the `git diff` at the top, before any directory is made — not the
        # `rev-parse` further down. Nothing constructed a range `git diff` accepts and
        # `rev-parse` rejects, so the ordering of that call is unwitnessed.
        self.assertFalse(self.workdir.exists() and any(self.workdir.iterdir()),
                         msg="a failure before any test ran must leave nothing behind")

    def test_a_tmpdir_prefix_with_several_jobs_is_refused(self) -> None:
        """The per-job temp roots exist so concurrent suites do not fight; a `TMPDIR=` prefix
        in the command overrides them, and this repository has been bitten by that already."""
        filler = "".join(f"pad_{n} = {n}\n" for n in range(20))
        self.repo.write("a.py", f"x = 1\n{filler}z = 1\n")
        self.repo.commit("base")
        self.repo.write("a.py", f"x = 2\n{filler}z = 2\n")
        self.repo.commit("two hunks far apart")
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--range", "HEAD~1..HEAD", "--jobs", "2",
             "--workdir", str(self.workdir),
             "--test-cmd", f"TMPDIR=/dev/shm {_ALWAYS_PASSES}"],
            cwd=self.repo.root, text=True, capture_output=True,
            env={**os.environ, **_NO_GIT_CONFIG, "TMPDIR": str(self.tmp)})
        run = _Run(proc)
        self.assertIn("sets TMPDIR", run.out, msg=str(run))
        self.assertEqual(2, run.code, msg=str(run))

    def test_an_empty_range_says_so_and_exits_zero(self) -> None:
        self.repo.write("k.py", "x = 1\n")
        self.repo.commit("base")
        self.repo.write("k.py", "x = 2\n")
        self.repo.commit("change")
        run = self.run_check("--paths", "does/not/exist")
        self.assertIn("nothing to check", run.out, msg=str(run))
        self.assertEqual(0, run.code, msg=str(run))

    def test_a_test_file_hunk_is_excluded_by_default(self) -> None:
        self.repo.write("tests/test_x.py", "def test_a():\n    assert True\n")
        self.repo.commit("base")
        self.repo.write("tests/test_x.py", "def test_a():\n    assert 1 == 1\n")
        self.repo.commit("test change")
        run = self.run_check()
        self.assertIn("hunk(s) in test files", run.out, msg=str(run))
        self.assertEqual(0, run.code, msg=str(run))

    def test_a_suite_that_never_ran_is_inconclusive_not_killed(self) -> None:
        """A nonzero exit with a collection marker means nothing observed the hunk; scoring it
        `killed` is the false green this detector exists for."""
        self.repo.write("k.py", "x = 1\n")
        self.repo.commit("base")
        self.repo.write("k.py", "x = 2\n")
        self.repo.commit("change")
        run = self.run_check(
            "--skip-baseline",
            test_cmd=f"{sys.executable} -c \"print('ERROR collecting tests');"
                     f"raise SystemExit(2)\"")
        self.assertIn("INCONCLUSIVE", run.out, msg=str(run))
        self.assertEqual(1, run.code, msg=str(run))

    def test_a_resolving_three_dot_range_is_measured(self) -> None:
        """`origin/main...HEAD` is the spelling both the skill and this script's own comment
        name as the review target, and no test resolved one: the range-parsing fix could be
        reverted to `split("..")[-1]` with the suite green."""
        self.repo.write("k.py", "x = 1\n")
        self.repo.commit("base")
        self.repo.git("branch", "mainline")
        self.repo.write("k.py", "x = 2\n")
        self.repo.commit("change")
        run = self.run_check(rng="mainline...HEAD", test_cmd=_pins("k.py", "x = 2"))
        self.assertIn("killed", run.out, msg=str(run))
        self.assertEqual(0, run.code, msg=str(run))

    def test_a_quoted_tmpdir_prefix_is_refused_too(self) -> None:
        """The refusal must see what the SHELL sees: an earlier version anchored on whitespace
        and `;&|` read straight past `sh -c "TMPDIR=… …"`, and the suite could not tell."""
        filler = "".join(f"pad_{n} = {n}\n" for n in range(30))
        self.repo.write("a.py", f"x = 1\n{filler}z = 1\n")
        self.repo.commit("base")
        self.repo.write("a.py", f"x = 2\n{filler}z = 2\n")
        self.repo.commit("two hunks")
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--range", "HEAD~1..HEAD", "--jobs", "2",
             "--workdir", str(self.workdir),
             "--test-cmd", f"sh -c \"TMPDIR=/dev/shm {_ALWAYS_PASSES}\""],
            cwd=self.repo.root, text=True, capture_output=True,
            env={**os.environ, **_NO_GIT_CONFIG, "TMPDIR": str(self.tmp)})
        run = _Run(proc)
        self.assertIn("sets TMPDIR", run.out, msg=str(run))
        self.assertEqual(2, run.code, msg=str(run))

    def test_a_repo_that_is_not_one_exits_two(self) -> None:
        self.repo.write("k.py", "x = 1\n")
        self.repo.commit("base")
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--range", "HEAD", "--test-cmd", _ALWAYS_PASSES,
             "--repo", str(self.tmp / "not-a-repo"), "--workdir", str(self.workdir)],
            cwd=self.repo.root, text=True, capture_output=True,
            env={**os.environ, **_NO_GIT_CONFIG, "TMPDIR": str(self.tmp)})
        run = _Run(proc)
        self.assertIn("cannot run:", run.out, msg=str(run))
        self.assertNotIn("Traceback", run.out, msg=str(run))
        self.assertEqual(2, run.code, msg=str(run))

    def test_include_tests_puts_the_excluded_hunks_back(self) -> None:
        self.repo.write("tests/test_x.py", "def test_a():\n    assert True\n")
        self.repo.commit("base")
        self.repo.write("tests/test_x.py", "def test_a():\n    assert 1 == 1\n")
        self.repo.commit("test change")
        run = self.run_check("--include-tests")
        self.assertNotIn("nothing to check", run.out, msg=str(run))
        self.assertIn("SURVIVED", run.out, msg=str(run))
        self.assertEqual(1, run.code, msg=str(run))

    def test_each_spelling_of_a_test_file_is_excluded(self) -> None:
        """Three independent clauses decide this, and one fixture satisfying two of them
        leaves the third free to be deleted."""
        for rel in ("pkg/tests/helper.py", "pkg/test_thing.py", "pkg/thing_test.py"):
            with self.subTest(rel=rel):
                root = self.tmp / f"r{rel.replace('/', '_')}"
                root.mkdir()
                repo = _Repo(root)
                repo.write(rel, "x = 1\n")
                repo.commit("base")
                repo.write(rel, "x = 2\n")
                repo.commit("change")
                proc = subprocess.run(
                    [sys.executable, str(SCRIPT), "--range", "HEAD~1..HEAD", "--jobs", "1",
                     "--workdir", str(self.tmp / "wd2"), "--test-cmd", _ALWAYS_PASSES],
                    cwd=repo.root, text=True, capture_output=True,
                    env={**os.environ, **_NO_GIT_CONFIG, "TMPDIR": str(self.tmp)})
                run = _Run(proc)
                self.assertIn("hunk(s) in test files", run.out, msg=str(run))
                self.assertEqual(0, run.code, msg=str(run))

    # --- the checkout it runs against --------------------------------------------------------

    def test_the_run_leaves_no_worktree_registered(self) -> None:
        self.repo.write("k.py", "x = 1\n")
        self.repo.commit("base")
        self.repo.write("k.py", "x = 2\n")
        self.repo.commit("change")
        self.run_check()
        listing = self.repo.git("worktree", "list").stdout
        self.assertEqual(1, len(listing.strip().splitlines()),
                         msg=f"worktrees left registered:\n{listing}")
        self.assertFalse(any(self.workdir.iterdir()) if self.workdir.exists() else False,
                         msg="worktree directories left behind")


class DiffEntryPathTests(unittest.TestCase):
    """The one parser that decides which file a diff entry is about."""

    @classmethod
    def setUpClass(cls) -> None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("mutation_check_under_test", SCRIPT)
        assert spec and spec.loader
        cls.module = importlib.util.module_from_spec(spec)
        # Import without leaving a `__pycache__` beside the script: `.claude/` is not a place a
        # test in this suite should write into, gitignored or not.
        previous, sys.dont_write_bytecode = sys.dont_write_bytecode, True
        try:
            spec.loader.exec_module(cls.module)
        finally:
            sys.dont_write_bytecode = previous

    def test_ordinary_path(self) -> None:
        self.assertEqual("lib.py", self.module._diff_entry_path("diff --git a/lib.py b/lib.py"))

    def test_nested_path(self) -> None:
        self.assertEqual("x/y.txt",
                         self.module._diff_entry_path("diff --git a/x/y.txt b/x/y.txt"))

    def test_path_containing_the_separator(self) -> None:
        self.assertEqual(
            "pkg b/lib.py",
            self.module._diff_entry_path("diff --git a/pkg b/lib.py b/pkg b/lib.py"))

    def test_quoted_non_ascii_path(self) -> None:
        self.assertEqual(
            "日.py",
            self.module._diff_entry_path(
                'diff --git "a/\\346\\227\\245.py" "b/\\346\\227\\245.py"'))


if __name__ == "__main__":  # pragma: no cover - convenience for a single-file run
    unittest.main()
