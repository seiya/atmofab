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
Every test drives the script as a subprocess over a real git repository built in `tmp_path`,
because that is the only way to observe what it actually reports; asserting on its functions
in isolation would reproduce the shape of defect it keeps having, where a helper is correct and
the run is not.

What these tests do NOT cover, stated so nobody reads the file as more than it is: timing,
`--jobs` above 2, the docstring/AST classification for anything but the cases below, and every
message's wording beyond the substrings asserted. A green run here means the eight defects
above stay fixed and the exit-code contract holds — not that the instrument is correct.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

SCRIPT = (Path(__file__).resolve().parents[2]
          / ".claude" / "skills" / "metdsl-review-loop" / "scripts" / "mutation_check.py")

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
            cwd=self.root, text=True, capture_output=True, check=True)

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
            env={**os.environ, "TMPDIR": str(self.tmp)})
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
        self.assertIn("killed", run.out, msg=str(run))
        self.assertNotIn("prose-only", run.out, msg=str(run))
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

    # --- encodings ---------------------------------------------------------------------------

    def test_a_crlf_file_is_measured_rather_than_skipped(self) -> None:
        self.repo.write("c.txt", b"a\r\nb\r\n")
        self.repo.commit("base")
        self.repo.write("c.txt", b"a\r\nZ\r\n")
        self.repo.commit("crlf change")
        run = self.run_check()
        self.assertNotIn("SKIP", run.out, msg=str(run))
        self.assertIn("SURVIVED", run.out, msg=str(run))

    def test_a_non_utf8_file_is_measured_rather_than_skipped(self) -> None:
        self.repo.write("m.py", b"# caf\xe9\nx = 1\n")
        self.repo.commit("base")
        self.repo.write("m.py", b"# caf\xe9\nx = 2\n")
        self.repo.commit("latin-1 change")
        run = self.run_check()
        self.assertNotIn("SKIP", run.out, msg=str(run))
        self.assertIn("SURVIVED", run.out, msg=str(run))

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

    # --- the exit-code contract --------------------------------------------------------------

    def test_a_red_baseline_exits_two(self) -> None:
        self.repo.write("k.py", "x = 1\n")
        self.repo.commit("base")
        self.repo.write("k.py", "x = 2\n")
        self.repo.commit("change")
        run = self.run_check(test_cmd=f"{sys.executable} -c \"raise SystemExit(1)\"")
        self.assertIn("BASELINE RED", run.out, msg=str(run))
        self.assertEqual(2, run.code, msg=str(run))

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
            env={**os.environ, "TMPDIR": str(self.tmp)})
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
        spec.loader.exec_module(cls.module)

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
