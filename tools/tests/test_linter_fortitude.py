"""The declared `fortitude` rule set: that it is real, that it is imposed, and that it is stated.

Three kinds of check live here, and they are not interchangeable.

1. **Declaration shape** — pure, no linter needed.
2. **Resolution against the INSTALLED build** — runs the tool. These are the ones that matter:
   a declared set that the installed build silently resolves to something else is exactly the
   failure this work exists to prevent, and no amount of asserting the constant against itself
   would see it. They do NOT skip when the linter is absent (`.claude/skills/
   atmofab-enforcement-change` judgment rule 2: a machine without the tool is a machine that
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

#: A source that satisfies the declared set, used as the subject of the resolution checks. It
#: carries a plain `implicit none` with NO allow directive above it — the form the leaf-read
#: documents now require, and one that only passes because `C003` is out of the declared set.
_CLEAN_SOURCE = """module metdsl_probe_model
  use, intrinsic :: iso_fortran_env, only: real64
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

#: One defect per family the leaf-read documents instruct a leaf about, and ONLY families the
#: declared set actually runs: no default accessibility statement (C131), a bare intrinsic `use`
#: (C122), a literal kind (PORT011, twice — one per `real(8)` declaration), and a line past the
#: column limit (S001). It deliberately does NOT probe `C003`, which left the declared set, nor
#: `C061`: an earlier version of this comment named both and so described families that produce
#: nothing here.
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


#: A finding line, as opposed to the source echo fortitude prints under it. The distinction is
#: the whole of finding F1 of round 2: `assertIn("C122", stdout)` matched the ECHO of the
#: `! allow(C122, ...)` comment inside another diagnostic's context block, so the row claiming to
#: witness the closure passed with the closure removed.
_DIAGNOSTIC_RE = re.compile(r"^\S+\.f90:\d+:\d+: ([A-Z]+[0-9]+)\b", re.M)


def _reported_codes(completed: subprocess.CompletedProcess) -> set[str]:
    """The rule codes fortitude REPORTED, read from the diagnostic lines alone."""
    return set(_DIAGNOSTIC_RE.findall(completed.stdout))


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
            (lint.EXECUTABLE, "check", "--isolated", "--ignore-allow-comments",
             "--no-respect-gitignore", "--select", ",".join(lint.RULE_CODES), "."),
        )

    def test_the_default_target_is_the_directory_the_gate_points_at(self) -> None:
        """`check_argv()` with no argument must lint the whole `project_dir`.

        Unwitnessed until the round-3 census: the server's table row is compared against
        `lint.check_argv()`, i.e. against itself, so changing the default from `.` to any other
        path kept the whole suite green while the gate would have linted somewhere else — or
        nothing, which is the same `All checks passed!` a clean tree gives.
        """
        self.assertEqual(lint.check_argv()[-1], ".")
        self.assertEqual(lint.check_argv("src")[-1], "src")

    def test_the_declared_set_is_sorted_and_free_of_repeats(self) -> None:
        # Not cosmetic: the set is compared against a resolved listing and against the codes the
        # documents name, and both comparisons are over sets — a duplicate would make the
        # constant's length lie about what is checked.
        self.assertEqual(len(set(lint.RULE_CODES)), len(lint.RULE_CODES))
        self.assertEqual(list(lint.RULE_CODES), sorted(lint.RULE_CODES))

    def test_the_incident_rule_is_excluded_and_says_why(self) -> None:
        """`S241` is the rule of issue #110 and must not re-enter by a careless widening.

        Paired with the ground, because a bare exclusion is indistinguishable from an oversight
        the next reader "fixes". `C003` is here for a second reason: re-selecting it would make
        every plain `implicit none` a finding again, and the only escape from that is the allow
        directive `--ignore-allow-comments` exists to disable — so the two decisions are one.
        """
        self.assertNotIn("S241", lint.RULE_CODES)
        self.assertNotIn("C003", lint.RULE_CODES)
        self.assertIn("S241", lint.EXCLUDED_RULE_CODES)
        self.assertIn("C003", lint.EXCLUDED_RULE_CODES)
        self.assertIn("OB001", lint.EXCLUDED_RULE_CODES)
        self.assertEqual(set(lint.EXCLUDED_RULE_CODES) & set(lint.RULE_CODES), set())

    def test_the_version_range_is_ordered_and_spelled_consistently(self) -> None:
        self.assertLess(lint.MIN_VERSION, lint.BELOW_VERSION)
        floor = ".".join(str(p) for p in lint.MIN_VERSION[:2])
        ceiling = ".".join(str(p) for p in lint.BELOW_VERSION[:2])
        self.assertEqual(lint.SUPPORTED_VERSION_SPEC, f">={floor},<{ceiling}")


class UnusableInvocationTests(unittest.TestCase):
    """An invocation that judged nothing must not be read as a verdict about the source.

    Declaring the rule set put `--select` in the argv, and the tool validates it before reading a
    file — so this change created an exit status that says "the invocation was refused", where
    before there was none. Routed to `generate.generate` as a lint finding it is issue #110's
    unwinnable loop in a new place: the leaf would be sent to fix `lint.py`.
    """

    def test_a_refused_invocation_is_not_a_verdict(self) -> None:
        reason = lint.unusable_invocation_reason(2, "", "error: invalid value 'ZZZ999'")
        self.assertIsNotNone(reason)
        self.assertIn("tools/backends/linter/fortitude/lint.py", reason)

    def test_an_ordinary_findings_run_is_a_verdict(self) -> None:
        """Exit 1 is left alone, deliberately, and this row is why.

        The first version of this function ALSO refused an exit 1 that printed no diagnostic
        line — the withdrawn-code case — and it immediately false-refused a legitimate content
        failure whose output shape it had not been measured against. That case is caught at
        launch instead (`self_check_*`), where the answer is a bare exit status.
        """
        self.assertIsNone(lint.unusable_invocation_reason(1, "a.f90:1:1: C131 x", ""))
        self.assertIsNone(lint.unusable_invocation_reason(1, "anything at all", ""))
        self.assertIsNone(lint.unusable_invocation_reason(0, "All checks passed!", ""))

    def test_the_launch_self_check_accepts_this_host(self) -> None:
        _linter_path()
        with tempfile.TemporaryDirectory() as empty:
            completed = _run(list(lint.self_check_argv(empty)), Path(empty))
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIsNone(
            lint.self_check_reason(completed.returncode, completed.stdout, completed.stderr))

    def test_the_launch_self_check_refuses_a_set_this_build_cannot_impose(self) -> None:
        """Driven with a real withdrawn code rather than a synthetic exit status.

        `OB001` is default-enabled on every supported build and cannot be selected — the exact
        shape a vendor's patch release would produce for a code we declare. Over an EMPTY
        directory a usable build exits 0, so the refusal needs no output parsing.
        """
        _linter_path()
        with tempfile.TemporaryDirectory() as empty:
            argv = [a if a != ",".join(lint.RULE_CODES)
                    else ",".join(lint.RULE_CODES) + ",OB001"
                    for a in lint.self_check_argv(empty)]
            completed = _run(argv, Path(empty))
        self.assertNotEqual(completed.returncode, 0)
        reason = lint.self_check_reason(
            completed.returncode, completed.stdout, completed.stderr)
        self.assertIsNotNone(reason)
        self.assertIn("re-measure", reason)


class VersionGateTests(unittest.TestCase):
    def test_a_build_below_the_floor_is_refused(self) -> None:
        below = (lint.MIN_VERSION[0], lint.MIN_VERSION[1] - 1, 0)
        reason = lint.unsupported_version_reason(f"{lint.EXECUTABLE} {below[0]}.{below[1]}.0")
        self.assertIsNotNone(reason)
        self.assertIn("below the supported floor", reason)

    def test_a_build_at_or_above_the_ceiling_is_refused(self) -> None:
        """The probe is BUILT from the ceiling's first two components, like the floor row.

        It used to interpolate `BELOW_VERSION` verbatim, which made it true of whatever the
        constant said: the census measured that raising the ceiling's PATCH component to
        `(0, 10, 5)` kept the suite green while `SUPPORTED_VERSION_SPEC` still promised `<0.10`,
        so 0.10.0 through 0.10.4 were silently accepted.
        """
        ceiling = f"x {lint.BELOW_VERSION[0]}.{lint.BELOW_VERSION[1]}.0"
        self.assertIsNotNone(lint.unsupported_version_reason(ceiling))

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

    def test_an_allow_directive_suppresses_nothing(self) -> None:
        """The `leaf shortcut` this configuration exists to close, with a NEGATIVE CONTROL.

        A leaf AUTHORS its own source, so an in-source suppression directive is the one channel
        into the lint verdict it can actually reach — and one line above `module` covers the whole
        module. The assertion is over the codes fortitude REPORTS, parsed from its diagnostic
        lines: the first version of this row matched raw stdout, which contains fortitude's ECHO
        of the allow comment inside another finding's context block, so it passed with
        `--ignore-allow-comments` REMOVED (measured — the whole closure reverted, one string
        equality on the argv the only thing left standing).

        The control is what makes the positive half mean something: the same source under the
        same argv MINUS the flag must lose exactly those codes. Without it, a source the
        directive never suppressed would satisfy the row.
        """
        blanket = "! allow(C122, C131, C061, PORT011, C003)\n" + _DEFECTIVE_SOURCE
        (self.dir / "metdsl_probe_bad.f90").write_text(blanket)
        suppressible = {"C122", "C131", "PORT011"}

        declared = _run(list(lint.check_argv("metdsl_probe_bad.f90")), self.dir)
        self.assertEqual(declared.returncode, 1)
        self.assertEqual(
            suppressible - _reported_codes(declared), set(),
            "the allow directive suppressed a finding under the declared invocation")

        without_flag = [a for a in lint.check_argv("metdsl_probe_bad.f90")
                        if a != "--ignore-allow-comments"]
        control = _run(without_flag, self.dir)
        self.assertEqual(
            suppressible & _reported_codes(control), set(),
            "the directive suppressed nothing even WITHOUT the flag, so the case above "
            "observes nothing about the flag")

    def test_a_gitignore_beside_the_sources_cannot_hide_them(self) -> None:
        """The third channel, with the same negative control as the other two.

        Fortitude walks with `--respect-gitignore` by default, so a `.gitignore` whose pattern
        matches the sources removes them from the walk entirely — measured: a five-finding tree
        becomes `0 files scanned. All checks passed!`, exit 0, with no diagnostic at all. An
        ancestor file counts when its pattern matches the files; a directory pattern above the
        walk root does not.
        Quieter than the allow-comment channel, and reached by whatever can write one byte into
        the node's own `src/`.

        Only a git work tree is affected, which `workspace/orchestrations/.../src` always is; the
        fixture therefore initialises one, or the case observes nothing on either side.
        """
        _run(["git", "init", "-q", "."], self.dir)
        (self.dir / "metdsl_probe_bad.f90").write_text(_DEFECTIVE_SOURCE)
        self.assertEqual(
            _run(list(lint.check_argv(".")), self.dir).returncode, 1,
            "the defective source must fail before the .gitignore is written")

        (self.dir / ".gitignore").write_text("*.f90\n")
        self.assertEqual(
            _run(list(lint.check_argv(".")), self.dir).returncode, 1,
            "a .gitignore beside the sources hid them from the declared invocation")
        without_flag = [a for a in lint.check_argv(".") if a != "--no-respect-gitignore"]
        self.assertEqual(
            _run(without_flag, self.dir).returncode, 0,
            "the .gitignore hid nothing even WITHOUT the flag, so the case above observes "
            "nothing about it")

    def test_a_plain_implicit_none_needs_no_directive(self) -> None:
        """The other half of dropping `C003`, and the reason the two are one decision.

        The clean fixture carries no allow comment. Before this configuration it could not: C003
        fired on every plain `implicit none`, which is why four leaf-read documents mandated a
        directive — the rule set required the channel. Add the old mandated directive back and
        the source must now FAIL, or the documents that stopped teaching it are lying.
        """
        self.assertEqual(
            _run(list(lint.check_argv("metdsl_probe_model.f90")), self.dir).returncode, 0)
        (self.dir / "metdsl_probe_model.f90").write_text(
            _CLEAN_SOURCE.replace("  implicit none", "  ! allow(C003)\n  implicit none", 1))
        completed = _run(list(lint.check_argv("metdsl_probe_model.f90")), self.dir)
        self.assertEqual(completed.returncode, 1)
        self.assertIn("FORT005", completed.stdout)

    def test_an_unknown_allow_code_is_reported(self) -> None:
        """`FORT001` still fires with allow comments disabled, and that is worth pinning.

        Disabling a directive family could plausibly have taken its diagnostics with it, which
        would leave an invented code silent. Measured: it does not — the code is reported, on top
        of the `FORT005` the directive itself earns.
        """
        (self.dir / "metdsl_probe_model.f90").write_text(
            "! allow(ZZZ999)\n" + _CLEAN_SOURCE)
        completed = _run(list(lint.check_argv("metdsl_probe_model.f90")), self.dir)
        self.assertEqual(completed.returncode, 1)
        self.assertIn("FORT001", completed.stdout)


class HostRenderedRunnerTests(unittest.TestCase):
    """The file the retry loop cannot fix, linted with the argv the gate actually runs.

    This is the class issue #110 is: a finding in the host-rendered runner routes a
    `Generate.generate` retry to a leaf with no write authority over that file, so the loop
    cannot converge and burns the whole budget. The renderer is therefore held to the gate's
    rule set — and until this class existed, nothing checked that. Measured on the round-3
    census: making the renderer emit a trailing space (`S101`) or a bare intrinsic `use`
    (`C122`) left the ENTIRE suite green while producing a real gate failure in that file.

    `tools/tests/test_fortran_runner.py` pins the runner's SHAPE; this pins that the shape the
    gate judges it by is satisfied. The two are different questions and the second is the one
    that costs a billed run.
    """

    def setUp(self) -> None:
        _linter_path()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def _render(self, ir_name: str) -> str:
        from tools.tests import test_fortran_runner as fixtures

        return fixtures.render_runner(
            getattr(fixtures, ir_name)(), fixtures.BOUNDARY_SID, fixtures.HARNESS)

    def test_the_rendered_runner_passes_the_declared_rule_set(self) -> None:
        for ir_name in ("_boundary_ir", "_metrics_ir"):
            with self.subTest(ir=ir_name):
                target = self.dir / f"{ir_name.strip('_')}_runner.f90"
                target.write_text(self._render(ir_name))
                completed = _run(list(lint.check_argv(target.name)), self.dir)
                self.assertEqual(
                    completed.returncode, 0,
                    "the host-rendered runner does not satisfy the rule set the gate applies; "
                    "a leaf would be sent to fix a file it cannot write:\n"
                    + completed.stdout[:2000])
                target.unlink()

    def test_the_check_would_notice_a_renderer_regression(self) -> None:
        """The negative control, because the row above is an `assertEqual(rc, 0)`.

        A green `rc == 0` is what a lint run over ZERO files also produces, and what a runner
        that stopped being rendered at all would produce. Perturbing the rendered text the way
        a renderer bug would must make the same command fail — otherwise the row above observes
        the absence of a file rather than the cleanliness of one.
        """
        text = self._render("_boundary_ir")
        target = self.dir / "regressed_runner.f90"
        target.write_text(text.replace("  implicit none", "  implicit none   ", 1))
        completed = _run(list(lint.check_argv(target.name)), self.dir)
        self.assertEqual(completed.returncode, 1)
        self.assertIn("S101", _reported_codes(completed))


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

    Four leaf-read sites name individual codes, and `.claude/skills/atmofab-enforcement-change`
    rule 3-a is explicit that three or more statement sites is where discipline has already lost.
    They are coupled by CONTAINMENT and a POINTER, not by members: each names the handful of
    codes a generated source most often trips, and pinning the full set into a leaf's checklist
    would fail on every legitimate widening.
    """

    #: (path, an anchor that PRECEDES the rule statement, region length in lines, a marker that
    #: sits OUTSIDE the region in the same file).
    #:
    #: The anchor is text this work did not write, and in particular not the sentence being
    #: corrected: anchoring on the correction pins that the correction survived, which is a
    #: different claim, and it BREAKS the moment the sentence is reworded again — measured, twice.
    #:
    #: The outside marker is what bounds the region. Without it the bound is unobserved: widening
    #: every entry below to 5000 lines was measured to leave this whole class green, because the
    #: only bound assertion was a probe string that occurs nowhere in the tree.
    _SITES = (
        ("docs/workflow/phases/phase_02_generate.md",
         "- `static lint` is NOT run by the `Generate.generate` leaf.", 12,
         "- **The `Generate.gate` static check traces the dependency dataflow"),
        ("skills/workflow-generate-generate/SKILL.md",
         "- **Write source that passes `static lint` AND the compiler syntax gate", 1,
         "- The `verification_status` of `source_meta.json` presumes `fail_closed`"),
        # Three lines, not one: the template's lint contract is rules (1)-(3), and line 7
        # already named a rule code OUTSIDE the one-line region — the same shape as the round-2
        # defect, one file over, found by the round-3 attack axis.
        ("tools/prompt_templates/pure_generate_generate.txt",
         "(1) Style lint (the `Generate.gate` lint check", 3,
         "(4) Dependency-dataflow gate"),
        # Anchored at the bullet ABOVE the lint paragraph, not at the paragraph: round 2 found
        # a MANDATE for the abolished directive one line above the old anchor, outside every
        # region this class watched. Three axes reported it independently; none of them was this
        # test.
        ("docs/workflow/CHECKS_MODULE_CONTRACT.md",
         "- **`spec_id` \u2264 55 characters**", 19,
         "- **A dummy argument no interface fixes is deleted, not bound.**"),
    )

    #: Every file a leaf is handed that could carry the directive, which is WIDER than the four
    #: regioned sites above: the force-read contract set for a `generate` leaf also includes
    #: `docs/AGENT_CONTRACT.md` and `docs/workflow/RUNNER_OUTPUT_CONTRACT.md`, the `verify`
    #: reviewer reads its own SKILL, and a node's `controlled_spec.md` is read by the reviewer
    #: and inlined for some producers. The round-3 census found the abolished idiom taught in
    #: exactly such a file, outside the four this class watched. The regioned rows stay on the
    #: four sites that STATE the rule; the whole-file row below covers everything a leaf reads.
    _LEAF_READ_FILES = tuple(path for path, _a, _l, _o in _SITES) + (
        "docs/AGENT_CONTRACT.md",
        "docs/workflow/RUNNER_OUTPUT_CONTRACT.md",
        "skills/workflow-generate-verify/SKILL.md",
        "spec/component/dynamics/shallow_water/"
        "dynamics_shallow_water_time_update_2d_ssprk2/controlled_spec.md",
    )

    #: The EXCLUDED codes a leaf-read document may still name. `C003` must be nameable: the
    #: documents changed what they say about it, and a rule change stated without naming the rule
    #: is not a statement. The others must not be — a leaf-read region naming `S241` was measured
    #: to pass the containment row, and instructing a leaf to satisfy `S241` sends it to fix the
    #: host-rendered runner, which is the exclusion's own recorded ground and issue #110's shape.
    #: Kept here rather than in `lint.py` because it is a rule about DOCUMENTS, not about the gate.
    _NAMEABLE_EXCLUSIONS = ("C003",)

    _CODE_RE = re.compile(r"\b([A-Z]{1,5}[0-9]{3})\b")

    def test_the_code_detector_matches_every_prefix_width_in_use(self) -> None:
        """Narrowing the prefix width is a silent fail-open: the census measured `{4,5}` keeping
        the whole class green while every `C…` / `S…` / `E…` mention stopped being checked, the
        surviving `FORT…` / `PORT…` matches holding the row up. Pinned against the widths the
        declared set actually uses, and against a standard name that must NOT match."""
        for code in ("E000", "C003", "S001", "MOD011", "PORT011", "FORT005"):
            self.assertEqual(self._CODE_RE.findall(f"see {code} here"), [code])
        self.assertEqual(self._CODE_RE.findall("standard-conforming f2008 / F2018"), [])

    def _region(self, path: str, anchor: str, lines: int) -> str:
        text = (REPO_ROOT / path).read_text()
        index = text.find(anchor)
        self.assertNotEqual(
            index, -1,
            f"{path}: the anchor this coupling reads from is gone; re-point it at text that "
            f"PRECEDES the rule statement and is not itself the statement")
        return "\n".join(text[index:].splitlines()[:lines])

    def test_every_rule_code_a_leaf_read_site_names_is_in_the_declared_set(self) -> None:
        """A leaf-read document may name a code the gate RUNS, or one this repository has
        explicitly decided not to run. It may not name a third thing.

        The union is the honest bound: `docs/…/phase_02_generate.md` names `C003` in order to say
        it is no longer checked, and refusing that would force the document to teach a rule
        change without naming the rule. What the check still catches is a code the repository has
        no position on at all — including one the vendor enables by default and nobody reviewed.
        WHAT IT DOES NOT PIN: which side of the union a mention is on. Reading that would mean
        parsing the prose around it.
        """
        known = set(lint.RULE_CODES) | set(self._NAMEABLE_EXCLUSIONS)
        union: set[str] = set()
        for path, anchor, lines, _outside in self._SITES:
            with self.subTest(path=path):
                named = set(self._CODE_RE.findall(self._region(path, anchor, lines)))
                union |= named
                self.assertEqual(
                    named - known, set(),
                    f"{path} instructs a leaf about a rule this repository has no position on; "
                    f"either add it to RULE_CODES or record why it is excluded, in "
                    f"tools/backends/linter/fortitude/lint.py")
        # A single site may legitimately name none — `CHECKS_MODULE_CONTRACT.md` stopped naming
        # any when its allow-directive prose was rewritten. What must not happen is the WHOLE row
        # observing nothing, which is what a per-site emptiness check would have hidden the day
        # every site went quiet.
        self.assertNotEqual(union, set(), "no site named a rule code, so this row observes nothing")
        # The narrowing is real: at least one excluded code must be refusable, or this row is the
        # union rule under another name.
        self.assertTrue(set(lint.EXCLUDED_RULE_CODES) - set(self._NAMEABLE_EXCLUSIONS))
        self.assertEqual(
            set(self._NAMEABLE_EXCLUSIONS) - set(lint.EXCLUDED_RULE_CODES), set(),
            "a code named here is no longer excluded; drop it from the allowance")

    def test_no_leaf_read_site_carries_a_copyable_allow_directive(self) -> None:
        """WHOLE FILE, not a region — because the defect this row exists for lived outside one.

        Round 2 found `docs/workflow/CHECKS_MODULE_CONTRACT.md` still MANDATING
        `! allow(C003)` one line above the region this class watched, in a document every
        agentic `generate` leaf is force-read. Three independent axes reported it; the coupling
        class could not, and a mutant reinstating the mandate INSIDE a region survived too,
        because `C003` is in `EXCLUDED_RULE_CODES` and the containment row deliberately does not
        read which side of that union a mention is on.

        The rule here is mechanical and needs no prose reading: a leaf-read document may DISCUSS
        the directive family (`! allow(...)`), but must not contain a directive naming an actual
        rule code, because that is the spelling a leaf copies. The gate runs with allow comments
        disabled, so every such spelling is either inert or a finding — there is no correct one.
        """
        directive = re.compile(r"allow\(\s*[A-Z]{1,5}[0-9]{3}")
        for path, _anchor, _lines, _outside in self._SITES:
            with self.subTest(path=path):
                text = (REPO_ROOT / path).read_text()
                hits = directive.findall(text)
                self.assertEqual(
                    hits, [],
                    f"{path} carries a copyable allow directive {hits}; the lint gate runs with "
                    f"allow comments disabled, so a leaf following it fails the gate in a way no "
                    f"regeneration can fix while the document still says it")
        # Self-test the detector against the exact shape that shipped, or a rewrite of the regex
        # leaves this row green over the defect it was written for.
        self.assertEqual(
            directive.findall("- Author lint-clean f2008 (the inline `! allow(C003)` directive)"),
            ["allow(C003"])

    def test_the_region_bound_excludes_text_outside_it(self) -> None:
        """The bound is self-tested against real text, not against a probe string.

        Each site names a marker that lives in the same file, after the region. It must be
        present in the file (or the marker itself has rotted and the row observes nothing) and
        absent from the region. Widen a bound and the marker comes inside, which is exactly the
        loosening that would otherwise make the containment and citation rows above pass on
        anything anywhere in the file.
        """
        for path, anchor, lines, outside in self._SITES:
            with self.subTest(path=path):
                whole = (REPO_ROOT / path).read_text()
                self.assertIn(outside, whole,
                              f"{path}: the out-of-region marker is gone; re-point it")
                self.assertNotIn(outside, self._region(path, anchor, lines),
                                 f"{path}: the region reaches past what it is meant to cover")

    def test_every_leaf_read_site_states_that_the_directive_is_disabled(self) -> None:
        """The RULE, not just the absence of a copyable spelling.

        Round 2 hardened these documents against CARRYING a directive a leaf could copy. It did
        not hold them to SAYING what the rule is — measured on the round-3 attack: replacing the
        prohibition with its exact opposite ("an allow comment is the accepted way to clear a
        stubborn style finding") in all three agentic sites passed 1294 tests. The pure path was
        pinned by a token literal in `test_pure_leaf_wiring.py`; the agentic path, including the
        document every `generate` leaf is force-read, was not.

        The literal is DERIVED from `CHECK_FLAGS`, so renaming the flag breaks the code and the
        documents together rather than leaving the prose asserting a flag that is gone.
        """
        flag = next(f for f in lint.CHECK_FLAGS if "allow" in f)
        for path, anchor, lines, _outside in self._SITES:
            with self.subTest(path=path):
                self.assertIn(
                    flag, self._region(path, anchor, lines),
                    f"{path} no longer states that allow directives are disabled; a document "
                    f"that stops saying it is one edit from saying the opposite, which is what "
                    f"sends a leaf to write the line that fails the gate")
        # The detector is not vacuous: the reversal that passed the suite does not contain it.
        self.assertNotIn(
            flag,
            "an `! allow(...)` comment above the offending line is the accepted way to clear a "
            "stubborn style finding")

    def test_every_leaf_read_site_cites_where_the_set_is_defined(self) -> None:
        """A leaf-read contract has to be self-contained, so it repeats part of the rule.

        What it must not do is leave a reader with no way back to the definition — that is how
        four sites drift into four different rule sets.
        """
        for path, anchor, lines, _outside in self._SITES:
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
        # The `"|" not in rest` filter this line used to carry dropped 0 of 42 rows and, measured
        # on the round-3 census, ADMITTED a bogus row whose text contains a pipe with no preceding
        # space. The `^| `CODE` |` anchor is what separates a table row from prose; the filter was
        # an escape hatch, not a discriminator.
        declared_rows = set(re.findall(r"^\| `([A-Z]+[0-9]+)` \|", doc, re.M))
        self.assertEqual(
            declared_rows - set(lint.EXCLUDED_RULE_CODES), set(lint.RULE_CODES),
            "docs/backends/linter/fortitude/RULES.md and RULE_CODES disagree about what the "
            "gate checks; the code is the authority — update the document to match it")
        self.assertEqual(declared_rows & set(lint.EXCLUDED_RULE_CODES),
                         set(lint.EXCLUDED_RULE_CODES))
        self.assertIn(lint.SUPPORTED_VERSION_SPEC, doc)
        self.assertIn("RULE_CODES", doc)
        # Every flag, derived, and read from the ENUMERATION rather than from the file.
        # Round 2 rewrote this document's channel list and the edit never landed, so the commit
        # message said three channels while the document still described two. The first fix
        # asserted the flag literal appeared ANYWHERE in the file — and a reviewer deleted a whole
        # channel bullet with the row still green, because the flag names also occur in the
        # reproduce command and the floor bullet. What is read now is the §Design Policy section,
        # and each flag must OPEN a bullet there, so a deleted or inverted entry fails.
        policy = doc[doc.index("## Design Policy"):doc.index("## Declared set")]
        for flag in lint.CHECK_FLAGS:
            if flag.startswith("--") and flag != "--select":
                self.assertIn(
                    f"- `{flag}`", policy,
                    f"§Design Policy has no bullet opening with {flag}, which the gate runs; a "
                    f"flag mentioned in passing elsewhere is not an enumeration of the channels")

    def test_the_backend_package_is_not_gitignored(self) -> None:
        """`.gitignore` carried a bare, unanchored `fortitude` — the linter binary, when an
        operator installs it into the checkout root.

        Unanchored, it silently swallowed this backend's package and document directories the
        moment that axis value got a directory of its own. `git add -A` skips an ignored path
        with NO warning (an explicit `git add` warns), so the failure mode is a commit that
        looks complete and is missing the module the whole change is about. Nothing observed
        the anchoring until the round-3 census constructed the consequence.
        """
        for rel in ("tools/backends/linter/fortitude/lint.py",
                    "docs/backends/linter/fortitude/RULES.md"):
            with self.subTest(path=rel):
                completed = subprocess.run(
                    ["git", "check-ignore", "-q", rel], cwd=str(REPO_ROOT),
                    capture_output=True, text=True, timeout=60)
                self.assertEqual(completed.returncode, 1,
                                 f"{rel} is gitignored; `git add -A` would skip it silently")

    def test_the_runbook_states_this_range_wherever_an_operator_reads_it(self) -> None:
        """Both sites, not "the range appears somewhere".

        The document states this one at three sites since issue #120 — the host-tool table, the
        install line, and the version-range table §0-1 gained — and a presence check is satisfied
        while one of them drifts. The assertion below is `>= 2` rather than a count, deliberately:
        a count here is a number that rots every time the document grows, which is the class this
        branch spent two rounds correcting. What pins the TABLE is
        `tools/tests/test_host_prerequisites.py`'s set identity over its range column. Measured: editing the first
        occurrence to a different range left an `assertIn` green.

        The set-identity half of this check — that NO range in the document is one nothing
        declares — moved to `tools/tests/test_host_prerequisites.py` when `ruff` and `cppcheck`
        gained ranges of their own (issue #120): it is a property of every linter at once, and
        asserting it from one backend's file would have made this file fail whenever a sibling's
        range changed.
        """
        runbook = (REPO_ROOT / "docs" / "RUNBOOK.md").read_text()
        self.assertGreaterEqual(runbook.count(lint.SUPPORTED_VERSION_SPEC), 2,
                                "the range must reach both the tool table and the install line")
        self.assertIn("unsupported_required_host_tool_versions", runbook)


if __name__ == "__main__":
    unittest.main()
