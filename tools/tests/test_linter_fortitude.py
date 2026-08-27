"""The declared `fortitude` rule set: that it is real, that it is imposed, and that it is stated.

Three kinds of check live here, and they are not interchangeable.

1. **Declaration shape** — pure, no linter needed.
2. **Resolution against the INSTALLED build** — runs the tool. These are the ones that matter:
   a declared set that the installed build silently resolves to something else is exactly the
   failure this work exists to prevent, and no amount of asserting the constant against itself
   would see it. They do NOT skip when the linter is absent (`.claude/skills/
   metdsl-enforcement-change` judgment rule 2: a machine without the tool is a machine that
   cannot run a workflow, which `tools/tests/test_host_prerequisites.py` already asserts).
3. **Prose coupling** — the leaf-read documents name individual codes of this set, and a code
   they name that the gate does not run is an instruction to satisfy a rule nothing checks.

WHAT IS NOT PINNED HERE, stated rather than implied: cross-VERSION identity. Every run below
uses whichever build is installed. That one build resolves the set correctly is what a test can
see; that 0.8.x and 0.9.x agree was MEASURED and is recorded in
`docs/backends/linter/fortitude/RULES.md`, and re-measuring it needs several builds installed
side by side, which the suite does not do.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.backends.linter.fortitude import lint  # noqa: E402

#: A source that satisfies the declared set, used as the subject of the resolution checks. The
#: `! allow(C003)` pair is the form the leaf-read documents mandate, so its presence here is also
#: the witness that the mandated form still works under an explicit selection.
_CLEAN_SOURCE = """module metdsl_probe_model
  use, intrinsic :: iso_fortran_env, only: real64
  ! allow(C003)
  implicit none
  private
  public :: metdsl_probe__op
contains
  subroutine metdsl_probe__op(x, y)
    real(real64), intent(in) :: x
    real(real64), intent(out) :: y
    y = x
  end subroutine metdsl_probe__op
end module metdsl_probe_model
"""

#: One defect per family the leaf-read documents instruct a leaf about: no default accessibility
#: statement (C131), a bare intrinsic `use` (C122), a literal kind (PORT011), a plain
#: `implicit none` with no allow directive (C003), and a line past the column limit (S001).
_DEFECTIVE_SOURCE = """module metdsl_probe_bad
  use iso_fortran_env, only: real64
  implicit none
contains
  subroutine metdsl_probe_bad__op(x, y)
    real(8), intent(in) :: x
    real(8), intent(out) :: y
    y = x{padding}
  end subroutine metdsl_probe_bad__op
end module metdsl_probe_bad
""".format(padding="  ! " + "p" * 120)


def _linter_path() -> str:
    """The installed linter, or a failure that says what to install.

    Deliberately not a skip. A host without it cannot run a workflow at all
    (`docs/RUNBOOK.md` §0-1 refuses the launch), so a green suite on such a machine would be
    reporting that a gate is fine when nothing has run it.
    """
    import shutil

    found = shutil.which(lint.EXECUTABLE)
    if found is None:
        raise AssertionError(
            f"{lint.EXECUTABLE} is not on PATH; the workflow's lint gate cannot run and neither "
            f"can these checks — install {lint.SUPPORTED_VERSION_SPEC} (docs/RUNBOOK.md#0-1)"
        )
    return found


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=str(cwd), text=True, capture_output=True,
                          timeout=120, check=False)


def _resolved_rule_codes(cwd: Path, target: str) -> tuple[str, ...]:
    """The rule codes the installed build ACTUALLY enables for the declared invocation.

    Read from the tool's own settings dump rather than inferred from findings: a rule that is
    enabled and simply finds nothing is indistinguishable from a rule that was dropped, if the
    only evidence is the diagnostics.
    """
    argv = [*lint.check_argv(target), "--show-settings"]
    completed = _run(argv, cwd)
    if completed.returncode not in (0, 1):
        raise AssertionError(
            f"the declared invocation was refused by the installed build "
            f"(rc={completed.returncode}): {(completed.stderr or completed.stdout).strip()}"
        )
    block = re.search(r"linter\.rules\.enabled = \[(.*?)\n\]", completed.stdout, re.S)
    assert block is not None, f"no resolved rule listing in:\n{completed.stdout[:2000]}"
    return tuple(sorted(set(re.findall(r"\(([A-Z]+[0-9]+)\)", block.group(1)))))


class DeclarationTests(unittest.TestCase):
    def test_the_argv_imposes_the_declared_set_and_ignores_config_files(self) -> None:
        self.assertEqual(
            lint.check_argv("."),
            (lint.EXECUTABLE, "check", "--isolated", "--select",
             ",".join(lint.RULE_CODES), "."),
        )

    def test_the_declared_set_is_sorted_and_free_of_repeats(self) -> None:
        # Not cosmetic: the set is compared against a resolved listing and against the codes the
        # documents name, and both comparisons are over sets — a duplicate would make the
        # constant's length lie about what is checked.
        self.assertEqual(len(set(lint.RULE_CODES)), len(lint.RULE_CODES))
        self.assertEqual(list(lint.RULE_CODES), sorted(lint.RULE_CODES))

    def test_the_incident_rule_is_excluded_and_says_why(self) -> None:
        """`S241` is the rule of issue #110 and must not re-enter by a careless widening.

        Paired with the ground, because a bare exclusion is indistinguishable from an oversight
        the next reader "fixes".
        """
        self.assertNotIn("S241", lint.RULE_CODES)
        self.assertIn("S241", lint.EXCLUDED_RULE_CODES)
        self.assertIn("OB001", lint.EXCLUDED_RULE_CODES)
        self.assertEqual(set(lint.EXCLUDED_RULE_CODES) & set(lint.RULE_CODES), set())

    def test_the_version_range_is_ordered_and_spelled_consistently(self) -> None:
        self.assertLess(lint.MIN_VERSION, lint.BELOW_VERSION)
        floor = ".".join(str(p) for p in lint.MIN_VERSION[:2])
        ceiling = ".".join(str(p) for p in lint.BELOW_VERSION[:2])
        self.assertEqual(lint.SUPPORTED_VERSION_SPEC, f">={floor},<{ceiling}")


class VersionGateTests(unittest.TestCase):
    def test_a_build_below_the_floor_is_refused(self) -> None:
        below = (lint.MIN_VERSION[0], lint.MIN_VERSION[1] - 1, 0)
        reason = lint.unsupported_version_reason(f"{lint.EXECUTABLE} {below[0]}.{below[1]}.0")
        self.assertIsNotNone(reason)
        self.assertIn("below the supported floor", reason)

    def test_a_build_at_or_above_the_ceiling_is_refused(self) -> None:
        at = lint.BELOW_VERSION
        self.assertIsNotNone(lint.unsupported_version_reason(f"x {at[0]}.{at[1]}.{at[2]}"))

    def test_an_unreadable_version_is_refused_rather_than_assumed_good(self) -> None:
        """The fail-CLOSED polarity, driven in both of its shapes.

        A probe that could not start the program and one that read a line with no version in it
        arrive here identically, and both mean the same thing: the build that will decide a
        certification is unidentified.
        """
        for text in (None, "", "fortitude (unknown build)"):
            with self.subTest(text=text):
                self.assertIsNotNone(lint.unsupported_version_reason(text))

    def test_a_build_inside_the_range_is_accepted(self) -> None:
        inside = f"{lint.EXECUTABLE} {lint.MIN_VERSION[0]}.{lint.MIN_VERSION[1]}.0"
        self.assertIsNone(lint.unsupported_version_reason(inside))

    def test_the_installed_build_is_inside_the_declared_range(self) -> None:
        _linter_path()
        completed = _run(list(lint.version_argv()), REPO_ROOT)
        first_line = (completed.stdout or completed.stderr).strip().splitlines()[0]
        self.assertIsNone(lint.unsupported_version_reason(first_line))


class ResolutionAgainstTheInstalledBuildTests(unittest.TestCase):
    """What the tool does with the declaration, as opposed to what the declaration says."""

    def setUp(self) -> None:
        _linter_path()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        (self.dir / "metdsl_probe_model.f90").write_text(_CLEAN_SOURCE)

    def test_every_declared_code_is_selectable_on_the_installed_build(self) -> None:
        """An unknown or withdrawn code is an ARGUMENT error, so nothing is checked at all.

        Measured on 0.8.0 and 0.9.x: `--select` validation happens before any file is read, and a
        withdrawn code (`OB001`) is rejected outright. A gate whose argv is refused this way does
        not report findings — it reports a tool failure on every node.
        """
        completed = _run(list(lint.check_argv("metdsl_probe_model.f90")), self.dir)
        self.assertIn(completed.returncode, (0, 1),
                      f"the declared argv was refused: "
                      f"{(completed.stderr or completed.stdout).strip()}")

    def test_the_resolved_rule_set_equals_the_declared_one(self) -> None:
        """The pin that survives a silent vendor RENAME.

        A retired code can be redirected to its successor rather than refused (measured:
        `S051` resolves to `MOD021`), so the declaration is confirmed by what the build resolves
        it to, never by the spelling being accepted.
        """
        self.assertEqual(_resolved_rule_codes(self.dir, "metdsl_probe_model.f90"),
                         tuple(sorted(lint.RULE_CODES)))

    def test_a_neighbouring_config_file_cannot_change_the_verdict(self) -> None:
        """The witness for `--isolated`, and the reason it is not cosmetic.

        Without the flag the tool discovers a configuration file in the directory it is checking,
        and that file can switch off the rules the gate declares. Driven with a file that
        silences every rule the defective source trips: with the flag the verdict must be
        unchanged.
        """
        (self.dir / "metdsl_probe_bad.f90").write_text(_DEFECTIVE_SOURCE)
        without_config = _run(list(lint.check_argv("metdsl_probe_bad.f90")), self.dir)
        self.assertEqual(without_config.returncode, 1, "the defective source must fail")

        silencing = ", ".join(f'"{code}"' for code in lint.RULE_CODES)
        (self.dir / "fortitude.toml").write_text(f"[check]\nignore = [{silencing}]\n")
        with_config = _run(list(lint.check_argv("metdsl_probe_bad.f90")), self.dir)
        self.assertEqual(with_config.returncode, 1,
                         "a configuration file next to the sources changed the verdict — the "
                         "declared invocation is not isolated from it")
        # And the control: the same run WITHOUT the flag must be silenced, or the case above
        # proves nothing (a config the tool never reads would pass either way).
        unisolated = [arg for arg in lint.check_argv("metdsl_probe_bad.f90")
                      if arg != "--isolated"]
        self.assertEqual(_run(unisolated, self.dir).returncode, 0,
                         "the config file did not silence anything, so this case observes "
                         "nothing about --isolated")

    def test_the_mandated_allow_directive_still_suppresses(self) -> None:
        """`! allow(C003)` is the form four leaf-read documents mandate.

        An explicit `--select` replaces the default set outright, so the allow machinery is not
        implied to survive it — the clean source above carries the directive, and this asserts
        the pair works: remove the directive and the same source must fail.
        """
        self.assertEqual(
            _run(list(lint.check_argv("metdsl_probe_model.f90")), self.dir).returncode, 0)
        (self.dir / "metdsl_probe_model.f90").write_text(
            _CLEAN_SOURCE.replace("  ! allow(C003)\n", ""))
        self.assertEqual(
            _run(list(lint.check_argv("metdsl_probe_model.f90")), self.dir).returncode, 1,
            "C003 did not fire without its allow directive, so the directive's effect above is "
            "not evidence of anything")

    def test_an_unknown_allow_code_is_reported(self) -> None:
        """`FORT001` is in the declared set precisely so a leaf cannot invent a suppression."""
        (self.dir / "metdsl_probe_model.f90").write_text(
            _CLEAN_SOURCE.replace("  ! allow(C003)\n",
                                  "  ! allow(ZZZ999)\n  ! allow(C003)\n"))
        completed = _run(list(lint.check_argv("metdsl_probe_model.f90")), self.dir)
        self.assertEqual(completed.returncode, 1)
        self.assertIn("FORT001", completed.stdout)


class WiringTests(unittest.TestCase):
    def test_the_registry_reaches_this_module_as_the_lint_capability(self) -> None:
        from tools.backends import registry

        self.assertIs(registry.capability_module("linter", "fortitude", "lint"), lint)
        self.assertIn("lint", registry.get("linter", "fortitude").backend_provides)

    def test_the_server_runs_the_argv_this_module_declares(self) -> None:
        """The row the MCP tool actually launches, composed from here rather than spelled there.

        Pinned at the TABLE the tool reads, not at `_lint_preset_command`: a wiring that computed
        the right argv and then failed to put it in the table would satisfy the helper.
        """
        if str(REPO_ROOT / "mcp_servers") not in sys.path:
            sys.path.insert(0, str(REPO_ROOT / "mcp_servers"))
        import build_runtime_server as server

        self.assertEqual(server._LINT_PRESET_COMMANDS["fortitude"], lint.check_argv())
        self.assertNotIn("fortitude", server._INLINE_LINT_PRESET_COMMANDS)
        # argv[0] is what the launch probe looks for; a flag added ahead of it would send the
        # probe after the wrong program.
        self.assertEqual(server.lint_preset_executables("fortitude"), (lint.EXECUTABLE,))

    def test_the_server_starts_from_a_foreign_working_directory(self) -> None:
        """The witness for the dotted-import bootstrap the server gained for this.

        The composition happens at import, so a `sys.path` that does not contain the checkout
        root would break every launch of the server — including the one a leaf spawns, whose cwd
        is not this repository.
        """
        completed = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, %r); import build_runtime_server as s;"
             "print(s._LINT_PRESET_COMMANDS['fortitude'][0])"
             % str(REPO_ROOT / "mcp_servers")],
            cwd=str(Path(tempfile.gettempdir())), text=True, capture_output=True,
            timeout=120, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), lint.EXECUTABLE)


class ProseCouplingTests(unittest.TestCase):
    """The documents that state this rule set, coupled to the code that defines it.

    Four leaf-read sites name individual codes, and `.claude/skills/metdsl-enforcement-change`
    rule 3-a is explicit that three or more statement sites is where discipline has already lost.
    They are coupled by CONTAINMENT and a POINTER, not by members: each names the handful of
    codes a generated source most often trips, and pinning the full set into a leaf's checklist
    would fail on every legitimate widening.
    """

    #: (path, the byte-identical anchor that PRECEDES the rule statement, how far the region runs)
    #:
    #: The anchor is chosen to be text this change did NOT write. Anchoring on the corrected
    #: sentence would pin that the correction survived, which is not the same claim.
    _SITES = (
        ("docs/workflow/phases/phase_02_generate.md",
         "- `static lint` is NOT run by the `Generate.generate` leaf.", 12),
        ("skills/workflow-generate-generate/SKILL.md",
         "- **Write source that passes `static lint` AND the compiler syntax gate", 1),
        ("tools/prompt_templates/pure_generate_generate.txt",
         "(1) Style lint (the `Generate.gate` lint check", 1),
        ("docs/workflow/CHECKS_MODULE_CONTRACT.md",
         "does not suppress this class:", 6),
    )

    _CODE_RE = re.compile(r"\b([A-Z]{1,5}[0-9]{3})\b")

    def _region(self, path: str, anchor: str, lines: int) -> str:
        text = (REPO_ROOT / path).read_text()
        index = text.find(anchor)
        self.assertNotEqual(
            index, -1,
            f"{path}: the anchor this coupling reads from is gone; re-point it at text that "
            f"PRECEDES the rule statement and is not itself the statement")
        return "\n".join(text[index:].splitlines()[:lines])

    def test_every_rule_code_a_leaf_read_site_names_is_in_the_declared_set(self) -> None:
        for path, anchor, lines in self._SITES:
            with self.subTest(path=path):
                named = set(self._CODE_RE.findall(self._region(path, anchor, lines)))
                self.assertNotEqual(named, set(), f"{path}: the region named no rule code, so "
                                                  f"this row observes nothing")
                self.assertEqual(
                    named - set(lint.RULE_CODES), set(),
                    f"{path} instructs a leaf about a rule the gate does not run; either add it "
                    f"to tools/backends/linter/fortitude/lint.py or stop stating it")

    def test_the_region_bound_excludes_text_outside_it(self) -> None:
        """The bound is self-tested, or an unrelated sentence elsewhere keeps a site green.

        `S241` is the ideal probe: it appears nowhere in the tree, so a region that swallowed the
        whole file would still pass the containment row above. This asserts the opposite
        property — that the region is SHORT — by checking it against the file's own length.
        """
        for path, anchor, lines in self._SITES:
            with self.subTest(path=path):
                whole = (REPO_ROOT / path).read_text()
                region = self._region(path, anchor, lines)
                self.assertLess(len(region), len(whole),
                                f"{path}: the region is the whole file")
                self.assertNotIn("METDSL_REGION_BOUND_PROBE", region)

    def test_every_leaf_read_site_cites_where_the_set_is_defined(self) -> None:
        """A leaf-read contract has to be self-contained, so it repeats part of the rule.

        What it must not do is leave a reader with no way back to the definition — that is how
        four sites drift into four different rule sets.
        """
        for path, anchor, lines in self._SITES:
            with self.subTest(path=path):
                self.assertIn("tools/backends/linter/fortitude/lint.py",
                              self._region(path, anchor, lines))

    def test_the_backend_document_states_the_declared_set_and_the_exclusions(self) -> None:
        """MEMBERS, both tables, and this is the row that makes narrowing the set REVIEWABLE.

        The resolution check against the installed build cannot see a narrowing: it runs
        `--select <declared>` and reads back what resolved, so dropping a code drops it from both
        sides. Measured — deleting `S101` from `RULE_CODES` left that check green. What a
        narrowing must not be is silent, so the canonical document carries the members and is
        compared here. The direction is one-way: the code is the authority, the document is what
        is checked, and both are edited in the same change.

        Only codes named IN A TABLE ROW are read, so prose elsewhere in the document that
        mentions a code (the measurement section names `S241` and `S051`) does not satisfy it.
        """
        doc = (REPO_ROOT / "docs" / "backends" / "linter" / "fortitude" / "RULES.md").read_text()
        rows = re.findall(r"^\| `([A-Z]+[0-9]+)` \| (.*?) \|", doc, re.M)
        declared_rows = {code for code, rest in rows if "|" not in rest}
        self.assertEqual(
            declared_rows - set(lint.EXCLUDED_RULE_CODES), set(lint.RULE_CODES),
            "docs/backends/linter/fortitude/RULES.md and RULE_CODES disagree about what the "
            "gate checks; the code is the authority — update the document to match it")
        self.assertEqual(declared_rows & set(lint.EXCLUDED_RULE_CODES),
                         set(lint.EXCLUDED_RULE_CODES))
        self.assertIn(lint.SUPPORTED_VERSION_SPEC, doc)
        self.assertIn("RULE_CODES", doc)

    def test_every_version_range_the_runbook_states_is_the_declared_one(self) -> None:
        """EVERY spelling, not "the range appears somewhere".

        The document states it twice — once in the host-tool table and once in the install line —
        and a presence check is satisfied while the other one drifts. Measured: editing the first
        occurrence to a different range left an `assertIn` green. So the assertion is over the
        SET of ranges the document contains, which is what makes an operator's install line and
        the launch refusal impossible to disagree.
        """
        runbook = (REPO_ROOT / "docs" / "RUNBOOK.md").read_text()
        spellings = set(re.findall(r">=\d+\.\d+(?:\.\d+)?,<\d+\.\d+(?:\.\d+)?", runbook))
        self.assertEqual(
            spellings, {lint.SUPPORTED_VERSION_SPEC},
            "docs/RUNBOOK.md states a version range that is not the declared one "
            f"({lint.SUPPORTED_VERSION_SPEC}, from tools/backends/linter/fortitude/lint.py)")
        self.assertGreaterEqual(runbook.count(lint.SUPPORTED_VERSION_SPEC), 2,
                                "the range must reach both the tool table and the install line")
        self.assertIn("unsupported_required_host_tool_versions", runbook)


if __name__ == "__main__":
    unittest.main()
