#!/usr/bin/env python3
"""Tests for the review instrument `measure_claude_tool.py`.

The script drives a real Claude Code tool against a saturated fixture and reports whether
each declared expectation held. Its verdict is what a maintainer uses to decide that issue
#71's narrowing premise still holds after a CLI upgrade — that no RELATIVE `Glob` pattern
reaches outside `path` — and on that verdict rests a deletion of 164 lines of enforcement.
A defect here therefore produces false confidence about the read boundary, and the
dangerous direction is the same one as in every other instrument in this repository:
reporting that something was measured when it was not.

Nothing here launches the CLI: that costs minutes and needs one installed. What is tested
is everything the script decides BEFORE and AFTER the launch — the case list's coverage,
the substitution that builds the patterns, the detector that reads the tool's answer, the
fixture's saturation, and the verdict. Round 15 found four faults in exactly that layer,
none of which a green CLI run would have revealed:

  * the case list omitted the two rows `docs/HOOKS.md` rests its strongest sentence on
    (an ABSOLUTE alternative inside a brace, both orders) while a test docstring claimed
    the list WAS the rows the narrowing rests on;
  * rows were unlabelled, `main` returned 0 unconditionally, and a reader classified 19
    rows by eye — so a relative row that started reading would have printed as a row;
  * the `~` and `$HOME` rows named `~/.bashrc`, which the fixture does not create, so on a
    host without one they measured nothing while looking identical to the rows that did —
    the "the target was absent" trap the script's own docstring warns about;
  * the environment was `os.environ` minus one name, the denylist polarity issue #71
    rejected for the leaf launch.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

SCRIPT = (Path(__file__).resolve().parents[2]
          / ".claude/skills/metforge-enforcement-change/scripts/measure_claude_tool.py")


def _load():
    spec = importlib.util.spec_from_file_location("measure_claude_tool_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Import without leaving a `__pycache__` beside the script: `.claude/` is not a place a
    # test in this suite should write into, gitignored or not.
    previous, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


class CaseListCoverageTests(unittest.TestCase):
    """The default list must ask every question the prose says it asks.

    This is the fault round 15 found: the list is cited by `TODO.md`, `docs/HOOKS.md` and
    `tools/hooks/cli.py` as the re-check for the narrowing, and it omitted the shapes the
    narrowing's strongest sentence depends on. A future CLI that expanded braces before
    testing `isAbsolute` is the named reopening scenario, and the list could not see it.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.module = _load()
        cls.patterns = {
            case[2].get("pattern"): case[0]
            for case in cls.module.DEFAULT_CASES if case[1] == "Glob"
        }

    def test_a_brace_holding_an_absolute_alternative_is_asked_in_both_orders(self) -> None:
        self.assertEqual(self.patterns.get("{{BASE}/secret,sub}/*"), "inert")
        self.assertEqual(self.patterns.get("{sub,{BASE}/secret}/*"), "inert")

    def test_the_whitespace_prefixes_that_deleted_strip_are_both_asked(self) -> None:
        """A leading SPACE was the measured shape; a TAB was the original witness for the
        `.strip()` this branch deleted, and was filed in TODO as never sampled."""
        self.assertEqual(self.patterns.get(" {BASE}/secret/*"), "inert")
        self.assertEqual(self.patterns.get("\t{BASE}/secret/*"), "inert")

    def test_the_absolute_rows_the_hook_comment_records_are_asked(self) -> None:
        """`tools/hooks/cli.py`'s table records braces and `..` AFTER the leading slash as
        still reading. Those are the rows that say WHY the trigger is the first character
        rather than the resolved location."""
        for pattern in ("{BASE}/{secret,outside}/*", "{BASE}/secret/../secret/*",
                        "/{BASE}/secret/*"):
            self.assertEqual(self.patterns.get(pattern), "reads", pattern)

    def test_every_tool_driven_has_a_control_row(self) -> None:
        """An empty result means nothing without a row that must not be empty."""
        for tool in {case[1] for case in self.module.DEFAULT_CASES}:
            reads = [c for c in self.module.DEFAULT_CASES if c[1] == tool and c[0] == "reads"]
            self.assertTrue(reads, f"{tool} has no row that must return files")

    def test_every_case_declares_reads_or_inert(self) -> None:
        for expect, tool, _input in self.module.DEFAULT_CASES:
            self.assertIn(expect, ("reads", "inert"), tool)


class SubstitutionTests(unittest.TestCase):
    """`{BASE}` substitution must survive the braces that are the thing under test."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.module = _load()

    def test_a_brace_pattern_is_substituted_not_formatted(self) -> None:
        """`str.format` raises on `{sub,/x/secret}/*`. Using it would have made exactly
        the rows this list was missing impossible to add."""
        out = self.module.substitute(
            {"pattern": "{sub,{BASE}/secret}/*"}, Path("/fx"), Path("/fx/a/b/repo"))
        self.assertEqual(out, {"pattern": "{sub,/fx/secret}/*"})

    def test_substitution_reaches_nested_values(self) -> None:
        out = self.module.substitute(
            {"glob": "{BASE}/secret/*", "n": 1, "l": ["{REPO}/docs"]},
            Path("/fx"), Path("/fx/a/b/repo"))
        self.assertEqual(out, {"glob": "/fx/secret/*", "n": 1, "l": ["/fx/a/b/repo/docs"]})


class ResultDetectorTests(unittest.TestCase):
    """SELF-TEST of the detector, per this repository's rule for negative assertions.

    Every INERT row asserts an absence. If `read_files` broke so that it always answered
    "inert", every relative row would pass and the escape it exists to catch would be
    invisible — so the detector is fed one answer that must count as a read and one that
    must not, with the rule stated once and used by both sides.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.module = _load()

    def test_the_tools_empty_wording_is_not_a_read(self) -> None:
        self.assertFalse(self.module.read_files('["No files found"]'))
        self.assertFalse(self.module.read_files('["No matches found"]'))

    def test_returned_paths_are_a_read(self) -> None:
        self.assertTrue(self.module.read_files('["/fx/secret/MARK.txt"]'))
        self.assertTrue(self.module.read_files('["Found 2 files\\ndocs/a.md"]'))

    def test_a_failed_launch_is_neither_reads_nor_inert(self) -> None:
        """TIMEOUT and "the tool did not run" must fail whatever the row expected.

        The first draft of this rewrite made them count as a READ, which fails an inert
        row correctly and PASSES an absolute one — so a run where every launch timed out
        would have reported the premise holding while measuring nothing at all. Scoring
        them as inert has the mirror fault. They are an error, not an answer.
        """
        for result in ("TIMEOUT", "NO RESULT (the tool did not run)"):
            self.assertTrue(self.module.launch_failed(result), result)
        self.assertFalse(self.module.launch_failed('["No files found"]'))
        self.assertFalse(self.module.launch_failed('["/fx/secret/MARK.txt"]'))


class FixtureSaturationTests(unittest.TestCase):
    """Every location a case names must EXIST, or its empty result proves nothing."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.module = _load()

    def test_every_ancestor_a_pattern_can_resolve_to_holds_a_marked_file(self) -> None:
        import shutil
        repo = self.module.build_fixture()
        base = repo.parent.parent.parent
        try:
            for ancestor in (repo, repo.parent, repo.parent.parent, base):
                for name in ("secret", "outside"):
                    self.assertTrue((ancestor / name / "MARK.txt").is_file(),
                                    f"{ancestor / name} is not saturated")
            # The `~` / `$HOME` rows resolve against HOME, which `probe` sets to the base.
            # Round 15 found them pointing at `~/.bashrc`, which the fixture never creates.
            self.assertTrue((base / "secret" / "MARK.txt").is_file())
            self.assertTrue((repo / "docs" / "linkdir").is_symlink())
            self.assertTrue((repo / "docs" / "linkfile.txt").is_symlink())
        finally:
            shutil.rmtree(base, ignore_errors=True)


class EnvironmentPolarityTests(unittest.TestCase):
    """The launch environment is an allowlist, not `os.environ` minus a few names.

    `CLAUDE_CODE_USE_BEDROCK` or a proxy variable sends the CLI to a real endpoint: the
    measurement is then billed and is not of the stand-in. It failed LOUD when measured
    (a control row timed out), but "fails loud" is a property of today's code, and the
    docstring's first line says "Unbilled" unconditionally.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.module = _load()

    def test_the_redirecting_variables_are_outside_the_allowlist(self) -> None:
        for name in ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                     "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "HTTPS_PROXY",
                     "ANTHROPIC_API_KEY", "HOME"):
            self.assertNotIn(name, self.module._ENV_ALLOWLIST, name)

    def test_the_allowlist_carries_what_a_launch_needs(self) -> None:
        self.assertIn("PATH", self.module._ENV_ALLOWLIST)


class VerdictTests(unittest.TestCase):
    """`main` must answer, and its exit code must be the answer.

    It used to `return 0` whatever the rows said, so nothing could consume it and a reader
    classified every row by eye against a sentence printed at the end.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.module = _load()

    def _run(self, cases, answers, tmp):
        path = Path(tmp) / "cases.json"
        path.write_text(json.dumps(cases))
        calls = []

        def fake_probe(repo, tool, tool_input, command, timeout, tools):
            calls.append((tool, tool_input, tools))
            return answers.pop(0)

        original = self.module.probe
        self.module.probe = fake_probe
        try:
            code = self.module.main(["--cases", str(path), "--command", "true"])
        finally:
            self.module.probe = original
        return code, calls

    def test_all_rows_as_declared_passes(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            code, _ = self._run(
                [["inert", "Glob", {"pattern": "../x/*"}],
                 ["reads", "Glob", {"pattern": "{BASE}/secret/*"}]],
                ['["No files found"]', '["/fx/secret/MARK.txt"]'], tmp)
        self.assertEqual(code, 0)

    def test_an_inert_row_that_reads_fails(self) -> None:
        """The reopening scenario. This is the only thing the script exists to detect."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            code, _ = self._run(
                [["inert", "Glob", {"pattern": "../x/*"}]],
                ['["/fx/secret/MARK.txt"]'], tmp)
        self.assertEqual(code, 1)

    def test_a_timed_out_row_fails_whatever_it_expected(self) -> None:
        import tempfile
        for expect in ("reads", "inert"):
            with self.subTest(expect), tempfile.TemporaryDirectory() as tmp:
                code, _ = self._run([[expect, "Glob", {"pattern": "*.md"}]],
                                    ["TIMEOUT"], tmp)
                self.assertEqual(code, 1, expect)

    def test_a_control_row_that_reads_nothing_fails(self) -> None:
        """A broken measurement must not print as confinement."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            code, _ = self._run(
                [["reads", "Glob", {"pattern": "docs/**/*.md"}]],
                ['["No files found"]'], tmp)
        self.assertEqual(code, 1)

    def test_the_tools_flag_is_derived_from_the_cases(self) -> None:
        """The docstring advertises that any tool can be driven; `--tools Glob,Grep` was
        hardcoded, so a `Read` case came back "No such tool available: Read"."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            _code, calls = self._run(
                [["reads", "Read", {"file_path": "x"}],
                 ["reads", "Glob", {"pattern": "*.md"}]],
                ['["x"]', '["x"]'], tmp)
        self.assertEqual({tools for _t, _i, tools in calls}, {"Glob,Read"})


if __name__ == "__main__":
    unittest.main()
