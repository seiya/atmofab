"""The declared `cppcheck` invocation: that it is imposed, that it closes what it claims, and
that what it CANNOT close is stated.

Four kinds of check live here, and they are not interchangeable.

1. **Declaration shape** — pure, no linter needed.
2. **Behaviour against the INSTALLED build** — runs the tool. Every channel row carries a
   NEGATIVE CONTROL, because a row that only asserts "the findings are still there" passes on a
   linter that found them for another reason. They do NOT skip when the linter is absent
   (`.claude/skills/metdsl-enforcement-change` judgment rule 2).
3. **Prose coupling** — `docs/backends/linter/cppcheck/RULES.md` is compared against the code,
   never the reverse.
4. **The deferred leaf-facing checklist**, tied to the reachability gate rather than to memory.

WHAT IS NOT PINNED HERE, stated rather than implied. Cross-VERSION identity — which this backend
does not achieve at all, for the reasons its document's §Requirements states. Every run below uses
whichever build is installed; the three-build measurement is recorded in the document and
re-taking it needs the builds side by side, which the suite does not do. What that leaves this
file able to see is that THIS build imposes the declared invocation and that the channels named
as closed are closed on it.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.backends.linter.cppcheck import lint  # noqa: E402

BACKEND_DOC = REPO_ROOT / "docs" / "backends" / "linter" / "cppcheck" / "RULES.md"

#: A source the declared severities pass. Not empty on purpose: an empty file passes every
#: invocation, so it would witness nothing about which one is running.
_CLEAN_SOURCE = """int metdsl_probe(int n)
{
    int total = 0;
    for (int i = 0; i < n; i++) {
        total += i;
    }
    return total;
}
"""

#: The fixture the backend document's verdict table is taken from. One defect per family the
#: declared severities cover.
_DEFECTIVE_SOURCE = """#include <stdlib.h>
#include <string.h>
#include <stdio.h>

int uninit_use(void) { int x; return x + 1; }
int oob(void) { int a[4]; a[4] = 1; return a[0]; }
int leak(int n) { int *p = malloc(sizeof(int) * n); if (p == p) return 1; return 0; }
int nullp(void) { int *p = NULL; return *p; }
void unusedv(void) { int q; (void)0; }
int dupbranch(int c) { if (c) return 1; else return 1; }
void selfassign(int a) { a = a; }
int divzero(void) { int z = 0; return 5 / z; }
void strcpy_ovf(void) { char b[4]; strcpy(b, "0123456789"); }
void printf_bad(void) { printf("%d %d\\n", 1); }
"""

#: The C++ half of the same fixture. It is here because the backend document's verdict table
#: reports the DRIFT between supported builds, and every check that differs across them
#: (`constVariablePointer`, `uselessOverride`, and `passedByValue` firing more often) needs C++ to
#: appear at all. A table taken from a fixture the repository does not carry is not reproducible
#: by a reader — the failure `docs/backends/linter/fortitude/RULES.md` records having made once.
_DEFECTIVE_SOURCE_CPP = """#include <vector>
#include <string>
struct S { int a; S() {} virtual ~S() {} virtual void f() {} };
struct T : S { void f() override {} };
int sum(std::vector<int> v) { int s = 0; for (size_t i = 0; i < v.size(); i++) s += v[i]; return s; }
std::string cat(std::string a, std::string b) { return a + b; }
"""

#: The checks this fixture produces on EVERY supported build. It is a subset, not the whole
#: verdict: 2.16.0 and 2.17.1 add `constVariablePointer` and `uselessOverride` and report
#: `passedByValue` more often, which is the drift the document records and no argv pins. Asserting
#: the whole histogram here would make the suite fail on a legitimately supported build.
_CHECKS_EVERY_BUILD_REPORTS = frozenset({
    "arrayIndexOutOfBounds", "bufferAccessOutOfBounds", "duplicateExpression", "memleak",
    "nullPointer", "passedByValue", "selfAssignment", "unassignedVariable", "uninitMemberVar",
    "uninitvar", "unreadVariable", "unusedVariable", "wrongPrintfScanfArgNum", "zerodiv",
})

_DIAGNOSTIC_RE = re.compile(r"\[([A-Za-z][A-Za-z0-9]*)\]\s*$", re.M)


def _linter_path() -> str:
    """The installed linter, or a failure that says what to install."""
    found = shutil.which(lint.EXECUTABLE)
    if found is None:
        raise AssertionError(
            f"{lint.EXECUTABLE} is not on PATH; the workflow's lint gate cannot run and neither "
            f"can these checks — install {lint.SUPPORTED_VERSION_SPEC} (docs/RUNBOOK.md#0-1)"
        )
    return found


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=str(cwd), text=True, capture_output=True,
                          timeout=300, check=False)


def _reported_checks(completed: subprocess.CompletedProcess) -> set[str]:
    """The check ids cppcheck REPORTED, read from the trailing `[id]` of a diagnostic line.

    cppcheck writes diagnostics to stderr and echoes the offending source line under each, so
    reading the whole output for an id would match the echo rather than the finding.
    """
    return set(_DIAGNOSTIC_RE.findall(completed.stderr)) | set(_DIAGNOSTIC_RE.findall(
        completed.stdout))


class _Tree:
    def __init__(self, stack: unittest.TestCase, source: str | None = None) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="metdsl-cppcheck-"))
        stack.addCleanup(shutil.rmtree, self.root, True)
        self.src = self.root / "sub" / "src"
        self.src.mkdir(parents=True)
        if source is None:
            (self.src / "probe.c").write_text(_DEFECTIVE_SOURCE)
            (self.src / "probe.cpp").write_text(_DEFECTIVE_SOURCE_CPP)
        else:
            (self.src / "probe.c").write_text(source)

    def run(self, *extra: str) -> subprocess.CompletedProcess:
        argv = list(lint.check_argv("."))
        argv[0] = _linter_path()
        return _run([argv[0], *extra, *argv[1:]], self.src)


class DeclarationTests(unittest.TestCase):
    def test_the_argv_is_the_declared_invocation(self) -> None:
        self.assertEqual(
            lint.check_argv("."),
            (lint.EXECUTABLE, "--error-exitcode=2",
             "--enable=warning,style,performance", "--platform=unix64", "."),
        )

    def test_the_suppression_flag_is_absent(self) -> None:
        """The removal IS the fix; a reinstated `--inline-suppr` reopens the one channel a leaf
        can write."""
        self.assertNotIn("--inline-suppr", lint.check_argv("."))

    def test_the_findings_exit_code_is_not_the_refusal_one(self) -> None:
        """cppcheck exits 1 for every way it can fail to RUN, so findings must not also be 1."""
        self.assertNotEqual(lint.FINDINGS_EXIT_CODE, 1)
        self.assertIn(f"--error-exitcode={lint.FINDINGS_EXIT_CODE}", lint.check_argv("."))

    def test_the_default_target_is_the_directory_the_gate_points_at(self) -> None:
        self.assertEqual(lint.check_argv()[-1], ".")
        self.assertEqual(lint.check_argv("src")[-1], "src")

    def test_every_suppression_carries_a_ground(self) -> None:
        """Empty today, and the row exists so that adding one without a reason fails."""
        for code, ground in lint.SUPPRESSED_RULE_CODES.items():
            self.assertGreater(len(ground.strip()), 60, code)

    def test_every_declared_suppression_reaches_the_argv(self) -> None:
        argv = lint.check_argv(".")
        for code in lint.SUPPRESSED_RULE_CODES:
            self.assertIn(f"--suppress={code}", argv)
        self.assertEqual(
            sum(1 for a in argv if a.startswith("--suppress=")),
            len(lint.SUPPRESSED_RULE_CODES))

    def test_the_operator_spelling_is_the_declared_range(self) -> None:
        floor = ".".join(str(p) for p in lint.MIN_VERSION[:2])
        ceiling = ".".join(str(p) for p in lint.BELOW_VERSION[:2])
        self.assertEqual(lint.SUPPORTED_VERSION_SPEC, f">={floor},<{ceiling}")


class VersionGateTests(unittest.TestCase):
    def test_a_two_component_version_line_is_read(self) -> None:
        """`Cppcheck 2.7` carries no patch component, and the floor build is spelled that way.

        A three-group pattern reads no version from it at all, which the fail-closed arm would
        then turn into a refusal of the very build `docs/RUNBOOK.md` §0-1 tells an operator to
        install.
        """
        self.assertEqual(lint.parse_version("Cppcheck 2.7"), (2, 7, 0))
        self.assertEqual(lint.parse_version("Cppcheck 2.16.0"), (2, 16, 0))
        self.assertIsNone(lint.unsupported_version_reason("Cppcheck 2.7"))

    def test_a_build_below_the_floor_is_refused(self) -> None:
        reason = lint.unsupported_version_reason("Cppcheck 2.6")
        self.assertIsNotNone(reason)
        self.assertIn(lint.SUPPORTED_VERSION_SPEC, reason)

    def test_a_build_at_or_above_the_ceiling_is_refused(self) -> None:
        at = lint.BELOW_VERSION
        self.assertIsNotNone(lint.unsupported_version_reason(f"Cppcheck {at[0]}.{at[1]}"))

    def test_an_unreadable_version_fails_closed(self) -> None:
        for text in (None, "", "Cppcheck", "not a version"):
            self.assertIsNotNone(lint.unsupported_version_reason(text), repr(text))

    def test_the_installed_build_is_inside_the_range(self) -> None:
        completed = subprocess.run([_linter_path(), *lint.version_argv()[1:]],
                                   text=True, capture_output=True, timeout=60, check=False)
        first = (completed.stdout or completed.stderr).strip().splitlines()[0]
        self.assertIsNone(lint.unsupported_version_reason(first), first)


class UnusableInvocationTests(unittest.TestCase):
    def test_the_two_verdict_statuses_are_verdicts(self) -> None:
        self.assertIsNone(lint.unusable_invocation_reason(0, "", ""))
        self.assertIsNone(lint.unusable_invocation_reason(lint.FINDINGS_EXIT_CODE, "", ""))

    def test_a_refusal_is_classified_by_driving_the_real_tool(self) -> None:
        """Not a synthetic exit status: the three ways cppcheck refuses to run, executed.

        Each must be told apart from findings, which is the whole reason `--error-exitcode` is 2.
        """
        tree = _Tree(self)
        argv = list(lint.check_argv("."))
        argv[0] = _linter_path()
        cases = {
            "an empty directory": [*argv[:-1], str(tree.root / "empty")],
            "a path that does not exist": [*argv[:-1], str(tree.root / "nope")],
            "an unknown flag": [argv[0], "--metdsl-not-a-flag", *argv[1:]],
        }
        (tree.root / "empty").mkdir()
        for label, case in cases.items():
            completed = _run(case, tree.src)
            self.assertEqual(completed.returncode, 1, f"{label}: {completed.stderr}")
            reason = lint.unusable_invocation_reason(
                completed.returncode, completed.stdout, completed.stderr)
            self.assertIsNotNone(reason, label)
            self.assertIn("refused, not the source", reason)

    def test_findings_are_a_verdict_through_the_real_tool(self) -> None:
        tree = _Tree(self)
        completed = tree.run()
        self.assertEqual(completed.returncode, lint.FINDINGS_EXIT_CODE, completed.stderr)
        self.assertIsNone(lint.unusable_invocation_reason(
            completed.returncode, completed.stdout, completed.stderr))

    def test_the_launch_self_check_accepts_this_host(self) -> None:
        with tempfile.TemporaryDirectory() as empty:
            argv = list(lint.self_check_argv(empty))
            argv[0] = _linter_path()
            completed = _run(argv, Path(empty))
        self.assertIsNone(
            lint.self_check_reason(completed.returncode, completed.stdout, completed.stderr))

    def test_the_self_check_refuses_a_flag_this_build_does_not_accept(self) -> None:
        """Driven with a real unknown flag rather than a synthetic status, and it is the arm that
        matters: an empty directory also exits 1, so a self-check reading the status alone would
        refuse every host."""
        with tempfile.TemporaryDirectory() as empty:
            argv = [_linter_path(), "--metdsl-not-a-flag", *lint.CHECK_FLAGS, empty]
            completed = _run(argv, Path(empty))
        reason = lint.self_check_reason(completed.returncode, completed.stdout, completed.stderr)
        self.assertIsNotNone(reason)
        self.assertIn("docs/backends/linter/cppcheck/RULES.md", reason)


class BehaviourAgainstTheInstalledBuildTests(unittest.TestCase):
    def test_the_declared_invocation_reports_the_documented_checks(self) -> None:
        tree = _Tree(self)
        reported = _reported_checks(tree.run())
        self.assertTrue(_CHECKS_EVERY_BUILD_REPORTS <= reported,
                        sorted(_CHECKS_EVERY_BUILD_REPORTS - reported))

    def test_a_clean_source_passes(self) -> None:
        tree = _Tree(self, _CLEAN_SOURCE)
        completed = tree.run()
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_an_in_source_suppression_comment_changes_no_verdict(self) -> None:
        """The one channel a leaf can write, and the one this change closes."""
        tree = _Tree(self)
        (tree.src / "probe.c").write_text(_DEFECTIVE_SOURCE.replace(
            "void unusedv(void) { int q;",
            "// cppcheck-suppress unusedVariable\nvoid unusedv(void) { int q;", 1))
        self.assertIn("unusedVariable", _reported_checks(tree.run()))
        # Negative control: the flag this argv no longer passes is what made it work.
        self.assertNotIn("unusedVariable", _reported_checks(tree.run("--inline-suppr")))

    def test_a_configuration_file_beside_the_sources_changes_no_verdict(self) -> None:
        """A NEGATIVE result, with a live positive control.

        cppcheck discovers no configuration file, so unlike its two siblings this backend closes
        nothing here. Recorded as a test rather than as a sentence because a negative nobody
        re-measures becomes an assumption; the control is what keeps the row honest.
        """
        tree = _Tree(self)
        content = "unusedVariable\nmemleak\narrayIndexOutOfBounds\n"
        for name in ("cppcheck-suppressions", ".cppcheck-suppressions", "suppressions.txt",
                     "cppcheck.cfg", ".cppcheck", "cppcheck.ini"):
            probe = tree.src / name
            probe.write_text(content)
            self.assertIn("unusedVariable", _reported_checks(tree.run()), name)
            probe.unlink()
        (tree.root / "cppcheck-suppressions").write_text(content)
        self.assertIn("unusedVariable", _reported_checks(tree.run()))
        # The positive control: the same content, named explicitly, DOES suppress.
        listed = tree.root / "explicit.txt"
        listed.write_text(content)
        self.assertNotIn("unusedVariable",
                         _reported_checks(tree.run(f"--suppressions-list={listed}")))

    def test_a_gitignore_changes_no_verdict(self) -> None:
        tree = _Tree(self)
        (tree.root / ".gitignore").write_text("*.c\n")
        self.assertIn("unusedVariable", _reported_checks(tree.run()))


class WiringTests(unittest.TestCase):
    def test_the_registry_reaches_this_module_as_the_lint_capability(self) -> None:
        from tools.backends import registry

        self.assertIs(registry.capability_module("linter", "cppcheck", "lint"), lint)
        self.assertIn("lint", registry.get("linter", "cppcheck").backend_provides)
        self.assertEqual(registry.get("linter", "cppcheck").core_provides, frozenset())

    def test_the_server_runs_the_argv_this_module_declares(self) -> None:
        if str(REPO_ROOT / "mcp_servers") not in sys.path:
            sys.path.insert(0, str(REPO_ROOT / "mcp_servers"))
        import build_runtime_server as server

        self.assertEqual(server._LINT_PRESET_COMMANDS["cppcheck"], lint.check_argv())
        self.assertEqual(server.lint_preset_executables("cppcheck"), (lint.EXECUTABLE,))

    def test_the_composite_preset_runs_this_argv_too(self) -> None:
        """`mixed` composes `fortitude` and `cppcheck`, and it is the reason `mixed` keeps `lint`
        in `core_provides`: it has no invocation of its own to move."""
        if str(REPO_ROOT / "mcp_servers") not in sys.path:
            sys.path.insert(0, str(REPO_ROOT / "mcp_servers"))
        import build_runtime_server as server

        from tools.backends import registry

        self.assertIn("cppcheck", server.lint_preset_sub_presets("mixed"))
        self.assertIn(lint.EXECUTABLE, server.lint_preset_executables("mixed"))
        self.assertEqual(registry.get("linter", "mixed").backend_provides, frozenset())

    def test_the_command_log_check_attributes_this_executable_to_this_preset(self) -> None:
        from tools.validate_pipeline_semantics import _infer_run_linter_preset_from_command

        self.assertEqual(
            _infer_run_linter_preset_from_command([lint.EXECUTABLE]), "cppcheck")
        self.assertEqual(
            _infer_run_linter_preset_from_command([f"/usr/bin/{lint.EXECUTABLE}"]), "cppcheck")

    def test_two_backends_sharing_an_executable_are_refused(self) -> None:
        """The mapping is built by enumeration, so a collision would silently let the last row
        win and attribute a logged command — and a certification — to the wrong preset.

        It cannot happen on today's declarations, so it is driven by making one: the `ruff`
        package is patched to claim this executable, and the builder must raise rather than
        return an answer.
        """
        from unittest import mock

        from tools.backends import registry
        from tools import validate_pipeline_semantics as vps

        other = registry.capability_module("linter", "ruff", "lint")
        vps._lint_preset_by_executable.cache_clear()
        self.addCleanup(vps._lint_preset_by_executable.cache_clear)
        with mock.patch.object(other, "EXECUTABLE", lint.EXECUTABLE):
            with self.assertRaises(ValueError) as caught:
                vps._lint_preset_by_executable()
        self.assertIn(lint.EXECUTABLE, str(caught.exception))


class BackendDocumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = BACKEND_DOC.read_text()

    def test_the_document_states_the_declared_invocation(self) -> None:
        """The argv itself, in the fenced block a reader copies, derived from the code."""
        self.assertIn(" ".join(lint.check_argv("<target>")), self.text)

    def test_the_document_opens_a_bullet_for_every_element_of_the_argv(self) -> None:
        """Element by element AT ITS STATEMENT POSITION. A flag name also appears in this
        document's measurement tables and in its limits, so "the token is present" couples
        nothing."""
        policy = self.text.split("## Design Policy", 1)[1].split("## Declared set", 1)[0]
        opened = set(re.findall(r"^- `(--[a-z-]+(?:=[^`]*)?)` — ", policy, re.M))
        expected = {f for f in lint.CHECK_FLAGS}
        self.assertEqual(opened, expected)

    def test_the_document_states_the_absent_suppression_flag(self) -> None:
        """The removal is the fix, so the document has to say the flag is gone and why."""
        policy = self.text.split("## Design Policy", 1)[1].split("## Declared set", 1)[0]
        self.assertRegex(policy, re.compile(r"^- \*\*`--inline-suppr` is ABSENT", re.M))

    def test_the_document_states_every_suppression(self) -> None:
        stated = set(re.findall(r"^\| `([A-Za-z][A-Za-z0-9]*)` \|", self.text, re.M))
        self.assertEqual(stated & set(lint.SUPPRESSED_RULE_CODES),
                         set(lint.SUPPRESSED_RULE_CODES))

    def test_the_document_states_the_severities_the_gate_enables(self) -> None:
        for severity in lint.ENABLED_SEVERITIES:
            self.assertIn(severity, self.text)

    def test_the_document_quotes_the_supported_range(self) -> None:
        self.assertIn(lint.SUPPORTED_VERSION_SPEC, self.text)

    def test_the_document_states_the_weaker_property_before_anything_else(self) -> None:
        """The one claim this document must not let a reader assume. It is checked at its
        STATEMENT POSITION — the opening of §Requirements — because the same words appear later
        in the design bullets, where they would satisfy a substring check without the section
        that owns them existing."""
        requirements = self.text.split("## Requirements", 1)[1].split("## Scope", 1)[0]
        self.assertRegex(
            requirements.strip(),
            r"^\*\*This backend does NOT make a verdict a function of the source and a declared "
            r"rule set\.\*\*")

    def test_the_package_and_its_document_are_not_gitignored(self) -> None:
        for path in (REPO_ROOT / "tools" / "backends" / "linter" / "cppcheck" / "lint.py",
                     BACKEND_DOC):
            completed = subprocess.run(["git", "check-ignore", str(path)],
                                       cwd=str(REPO_ROOT), capture_output=True, timeout=60)
            self.assertEqual(completed.returncode, 1, f"{path} is gitignored")


class DeferredLeafChecklistTests(unittest.TestCase):
    def test_no_language_this_preset_lints_is_reachable_by_a_node(self) -> None:
        """The obligation this backend defers, tied to the gate that makes it deferrable."""
        from tools.backends import registry
        from tools.validate_pipeline_semantics import _LINT_PRESET_FOR_LANGUAGE

        ours = sorted(lang for lang, preset in _LINT_PRESET_FOR_LANGUAGE.items()
                      if preset == "cppcheck")
        self.assertEqual(ours, ["c", "c++", "cpp", "cuda_c"])
        for language in ours:
            self.assertIsNotNone(
                registry.unimplemented_reason("language", language),
                f"`{language}` is now an implemented language backend, so a leaf can trip the "
                f"checks this backend runs. The leaf-facing checklist deferred in "
                f"docs/backends/linter/cppcheck/RULES.md §Scope is now owed: add it to "
                f"docs/workflow/phases/phase_02_generate.md §2-1 beside the fortitude one, and "
                f"drop the deferral from §Scope.")


if __name__ == "__main__":
    unittest.main()
