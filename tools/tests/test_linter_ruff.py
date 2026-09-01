"""The declared `ruff` rule set: that it is real, that it is imposed, and that it is stated.

Four kinds of check live here, and they are not interchangeable.

1. **Declaration shape** — pure, no linter needed.
2. **Resolution and closure against the INSTALLED build** — runs the tool. These are the ones
   that matter: a declared set the installed build silently resolves to something else, or a
   channel the flags no longer close, is exactly the failure this work exists to prevent, and no
   amount of asserting a constant against itself would see it. Every channel row carries a
   NEGATIVE CONTROL — the same probe with the flag omitted — because a row that only asserts
   "the findings are still there" passes on a linter that found them for another reason. They do
   NOT skip when the linter is absent (`.claude/skills/metdsl-enforcement-change` judgment
   rule 2: a machine without the tool is a machine that cannot run a workflow, which
   `tools/tests/test_host_prerequisites.py` already asserts).
3. **Prose coupling** — `docs/backends/linter/ruff/RULES.md` is the document a reader is sent to,
   and the code is the authority it is compared against, never the reverse.
4. **The deferred leaf-facing checklist** — this backend has none, deliberately, because no leaf
   can trip these rules. That decision is tied to the reachability gate rather than to memory.

WHAT IS NOT PINNED HERE, stated rather than implied: cross-VERSION identity. Every run below uses
whichever build is installed. That one build resolves the set correctly is what a test can see;
that 0.14.0 / 0.15.20 / 0.16.0 / 0.16.5 agree was MEASURED and is recorded in
`docs/backends/linter/ruff/RULES.md`, and re-measuring it needs several builds installed side by
side, which the suite does not do.
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

from tools.backends.linter.ruff import lint  # noqa: E402

BACKEND_DOC = REPO_ROOT / "docs" / "backends" / "linter" / "ruff" / "RULES.md"

#: A source the declared set passes. It is not empty on purpose: an empty file passes every rule
#: set, so it would witness nothing about which one is running.
_CLEAN_SOURCE = """import os


def probe(value):
    if value is None:
        return os.sep
    return str(value)
"""

#: The fixture the backend document's verdict table is taken from. Five findings under the
#: declared set: two unused imports (`F401`), an ambiguous name (`E741`), an unused variable
#: (`F841`) and an undefined name (`F821`). The nested `with` is deliberate and is NOT one of the
#: five — it is what 0.16.0's default set reports as `SIM117` and the declared set does not, i.e.
#: the drift this backend exists to remove.
_DEFECTIVE_SOURCE = """import os
import sys


def nested(a, b):
    with open(a) as p:
        with open(b) as q:
            return p, q


def ambiguous():
    l = 1
    return undefined_name
"""

_DIAGNOSTIC_RE = re.compile(r"^([A-Z]+[0-9]+)\s", re.M)


def _linter_path() -> str:
    """The installed linter, or a failure that says what to install.

    Deliberately not a skip. A host without it cannot run a workflow at all
    (`docs/RUNBOOK.md` §0-1 refuses the launch), so a green suite on such a machine would be
    reporting that a gate is fine when nothing has run it.
    """
    found = shutil.which(lint.EXECUTABLE)
    if found is None:
        raise AssertionError(
            f"{lint.EXECUTABLE} is not on PATH; the workflow's lint gate cannot run and neither "
            f"can these checks — install {lint.SUPPORTED_VERSION_SPEC} (docs/RUNBOOK.md#0-1)"
        )
    return found


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=str(cwd), text=True, capture_output=True,
                          timeout=180, check=False)


def _reported_codes(completed: subprocess.CompletedProcess) -> list[str]:
    """The rule codes ruff REPORTED, read from the diagnostic lines alone.

    A list rather than a set: two `F401` findings and one are different verdicts, and a channel
    that suppresses one of them would be invisible to a set.
    """
    return sorted(_DIAGNOSTIC_RE.findall(completed.stdout))


class _Tree:
    """A git work tree holding the defective source, since one channel needs a repository.

    `.gitignore` is only honoured inside one, and the gate's `project_dir` is inside this
    repository's checkout — so a fixture that is not a repository would report that channel
    closed when it is open.
    """

    def __init__(self, stack: unittest.TestCase) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="metdsl-ruff-"))
        stack.addCleanup(shutil.rmtree, self.root, True)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True, timeout=60)
        self.src = self.root / "sub" / "src"
        self.src.mkdir(parents=True)
        (self.src / "probe.py").write_text(_DEFECTIVE_SOURCE)

    def run(self, *, drop: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
        """The declared invocation over the source, optionally with some flags omitted."""
        argv = [a for a in lint.check_argv(".") if a not in drop]
        argv[0] = _linter_path()
        return _run(argv, self.src)


class DeclarationTests(unittest.TestCase):
    def test_the_argv_imposes_the_declared_set_and_closes_the_five_channels(self) -> None:
        self.assertEqual(
            lint.check_argv("."),
            (lint.EXECUTABLE, "check", "--isolated", "--ignore-noqa", "--no-respect-gitignore",
             "--no-cache", "--exclude=", "--select", ",".join(lint.RULE_CODES), "."),
        )

    def test_the_default_target_is_the_directory_the_gate_points_at(self) -> None:
        self.assertEqual(lint.check_argv()[-1], ".")
        self.assertEqual(lint.check_argv("src")[-1], "src")

    def test_the_declared_set_is_sorted_and_free_of_duplicates(self) -> None:
        self.assertEqual(list(lint.RULE_CODES), sorted(set(lint.RULE_CODES)))

    def test_no_excluded_code_is_also_declared(self) -> None:
        self.assertEqual(set(lint.EXCLUDED_RULE_CODES) & set(lint.RULE_CODES), set())

    def test_every_exclusion_carries_a_ground(self) -> None:
        for code, ground in lint.EXCLUDED_RULE_CODES.items():
            self.assertGreater(len(ground.strip()), 60, code)

    def test_the_operator_spelling_is_the_declared_range(self) -> None:
        """One range, two spellings, and the second is what `docs/RUNBOOK.md` §0-1 quotes."""
        floor = ".".join(str(p) for p in lint.MIN_VERSION[:2])
        ceiling = ".".join(str(p) for p in lint.BELOW_VERSION[:2])
        self.assertEqual(lint.SUPPORTED_VERSION_SPEC, f">={floor},<{ceiling}")


class VersionGateTests(unittest.TestCase):
    def test_a_build_below_the_floor_is_refused(self) -> None:
        below = (lint.MIN_VERSION[0], lint.MIN_VERSION[1] - 1, 0)
        reason = lint.unsupported_version_reason(f"ruff {below[0]}.{below[1]}.{below[2]}")
        self.assertIsNotNone(reason)
        self.assertIn(lint.SUPPORTED_VERSION_SPEC, reason)

    def test_a_build_at_or_above_the_ceiling_is_refused(self) -> None:
        at = lint.BELOW_VERSION
        self.assertIsNotNone(lint.unsupported_version_reason(f"ruff {at[0]}.{at[1]}.{at[2]}"))

    def test_an_unreadable_version_fails_closed(self) -> None:
        for text in (None, "", "ruff", "not a version"):
            self.assertIsNotNone(lint.unsupported_version_reason(text), repr(text))

    def test_a_build_inside_the_range_is_accepted(self) -> None:
        inside = (lint.MIN_VERSION[0], lint.MIN_VERSION[1], lint.MIN_VERSION[2])
        self.assertIsNone(lint.unsupported_version_reason(f"ruff {inside[0]}.{inside[1]}.{inside[2]}"))

    def test_the_installed_build_is_inside_the_range(self) -> None:
        completed = subprocess.run([_linter_path(), *lint.version_argv()[1:]],
                                   text=True, capture_output=True, timeout=60, check=False)
        first = (completed.stdout or completed.stderr).strip().splitlines()[0]
        self.assertIsNone(lint.unsupported_version_reason(first), first)


class UnusableInvocationTests(unittest.TestCase):
    def test_a_refused_invocation_is_not_a_verdict(self) -> None:
        """Driven through the REAL tool, not a synthetic status.

        The `cppcheck` sibling drove its three refusals through the tool from the start; this one
        was written against a hand-made `(2, "", "Unknown rule selector")`, which asserts the
        function against itself and would survive the tool changing its exit status. It is the
        classifier whose failure recreates issue #110's unwinnable loop, so the status it reads
        has to come from the tool.
        """
        tree = _Tree(self)
        for label, extra in (("an unknown code", "ZZZ999"), ("a removed code", "E999")):
            argv = [a if a != ",".join(lint.RULE_CODES) else a + "," + extra
                    for a in lint.check_argv(".")]
            argv[0] = _linter_path()
            completed = _run(argv, tree.src)
            with self.subTest(refusal=label):
                self.assertEqual(completed.returncode, 2, completed.stderr)
                reason = lint.unusable_invocation_reason(
                    completed.returncode, completed.stdout, completed.stderr)
                self.assertIsNotNone(reason)
                self.assertIn("refused, not the source", reason)

    def test_the_findings_exit_status_is_left_alone(self) -> None:
        """Exit 1 is the ordinary "there are findings" status; classifying it would send a leaf
        away from its own source."""
        self.assertIsNone(lint.unusable_invocation_reason(0, "", ""))
        self.assertIsNone(lint.unusable_invocation_reason(1, "Found 5 errors.", ""))

    def test_the_launch_self_check_accepts_this_host(self) -> None:
        with tempfile.TemporaryDirectory() as empty:
            argv = list(lint.self_check_argv(empty))
            argv[0] = _linter_path()
            completed = _run(argv, Path(empty))
        self.assertIsNone(
            lint.self_check_reason(completed.returncode, completed.stdout, completed.stderr))

    def test_the_self_check_refuses_a_set_this_build_cannot_impose(self) -> None:
        """Driven with a REAL removed code rather than a synthetic exit status.

        `E999` is a code every supported build knows about and refuses to select, which is the
        shape the self-check exists for: a declaration the tool will not accept, caught at launch
        rather than mid-run as a lint failure attributed to a leaf's source.
        """
        with tempfile.TemporaryDirectory() as empty:
            argv = [a if a != ",".join(lint.RULE_CODES) else a + ",E999"
                    for a in lint.self_check_argv(empty)]
            argv[0] = _linter_path()
            completed = _run(argv, Path(empty))
        self.assertNotEqual(completed.returncode, 0)
        reason = lint.self_check_reason(completed.returncode, completed.stdout, completed.stderr)
        self.assertIsNotNone(reason)
        self.assertIn("docs/backends/linter/ruff/RULES.md", reason)


class ResolutionAgainstTheInstalledBuildTests(unittest.TestCase):
    """The declared set is what the installed build actually enables, and the flags still close."""

    #: `--show-settings` is refused alongside `--ignore-noqa` (`the argument '--ignore-noqa'
    #: cannot be used with '--show-settings'`), so the resolution probe drops it. What it asks is
    #: what `--select` RESOLVES to, which no suppression flag participates in; the flags are
    #: covered by the channel rows below, each with its own negative control.
    _SETTINGS_INCOMPATIBLE_FLAGS = ("--ignore-noqa",)

    def _resolved(self, cwd: Path) -> tuple[str, ...]:
        argv = [a for a in lint.check_argv(".")
                if a not in self._SETTINGS_INCOMPATIBLE_FLAGS] + ["--show-settings"]
        argv[0] = _linter_path()
        completed = _run(argv, cwd)
        if completed.returncode not in (0, 1):
            raise AssertionError(
                f"the declared invocation was refused by the installed build "
                f"(rc={completed.returncode}): {(completed.stderr or completed.stdout).strip()}")
        block = re.search(r"linter\.rules\.enabled = \[(.*?)\n\]", completed.stdout, re.S)
        assert block is not None, f"no resolved rule listing in:\n{completed.stdout[:2000]}"
        return tuple(sorted(set(re.findall(r"\(([A-Z]+[0-9]+)\)", block.group(1)))))

    def test_the_declared_set_resolves_to_itself_on_this_build(self) -> None:
        """A code the vendor silently REDIRECTS would pass a spelling check and change the gate.

        Measured on this tool: `PGH001` prints `has been remapped to 'S307'` and the run
        proceeds, so the redirect leaves no non-zero exit behind.
        """
        tree = _Tree(self)
        self.assertEqual(self._resolved(tree.src), tuple(sorted(lint.RULE_CODES)))

    def test_the_declared_invocation_reports_the_documented_findings(self) -> None:
        tree = _Tree(self)
        completed = tree.run()
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(_reported_codes(completed), ["E741", "F401", "F401", "F821", "F841"])

    def test_a_clean_source_passes_the_declared_set(self) -> None:
        tree = _Tree(self)
        (tree.src / "probe.py").write_text(_CLEAN_SOURCE)
        completed = tree.run()
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    #: The discovered-configuration key the negative controls below use, and it is NOT `exclude`.
    #:
    #: `--exclude=` is on the argv for a channel of its own (the tool's built-in list), and a CLI
    #: `--exclude` overrides a discovered `exclude` as well — so a control written with that key
    #: would show the channel closed with `--isolated` REMOVED, and witness nothing about
    #: `--isolated`. `per-file-ignores` is not overridden by anything on the argv, so it is what
    #: isolates the flag under test. Measured: it takes the five findings to one.
    _DISCOVERED_KEY = 'per-file-ignores = {"*.py" = ["F401", "F821", "F841", "E741"]}\n'

    def test_a_configuration_file_beside_the_sources_changes_no_verdict(self) -> None:
        tree = _Tree(self)
        (tree.src / "ruff.toml").write_text("[lint]\n" + self._DISCOVERED_KEY)
        self.assertEqual(_reported_codes(tree.run()),
                         ["E741", "F401", "F401", "F821", "F841"])
        # Negative control: without `--isolated` the same file silences four of the five.
        self.assertEqual(_reported_codes(tree.run(drop=("--isolated",))), [])

    def test_a_configuration_file_at_an_ancestor_changes_no_verdict(self) -> None:
        """The one channel `fortitude` does not have: ruff walks UPWARD for its configuration,
        and the gate's `project_dir` is two directories inside the checkout."""
        tree = _Tree(self)
        (tree.root / "pyproject.toml").write_text("[tool.ruff.lint]\n" + self._DISCOVERED_KEY)
        self.assertEqual(_reported_codes(tree.run()),
                         ["E741", "F401", "F401", "F821", "F841"])
        self.assertEqual(_reported_codes(tree.run(drop=("--isolated",))), [])

    def test_the_builtin_exclude_list_changes_no_verdict(self) -> None:
        """The fifth channel, and the one the first version of this backend missed.

        `--isolated` RESTORES the tool's built-in exclude list rather than emptying it, so a
        source under any of its 25 names is silently not scanned: exit 0, `All checks passed`, and
        a `warning:` line that is not a finding. `--exclude=` empties it.
        """
        tree = _Tree(self)
        excluded = tree.src / "dist"
        excluded.mkdir()
        (tree.src / "probe.py").rename(excluded / "probe.py")
        self.assertEqual(tree.run().returncode, 1)
        control = tree.run(drop=("--exclude=",))
        self.assertEqual(control.returncode, 0)
        self.assertEqual(_reported_codes(control), [])

    def test_an_unreadable_directory_is_a_measured_NON_closure(self) -> None:
        """Recorded because no flag closes it and the documents say so.

        A walk read error degrades to a warning and exit 0 — quieter than any channel the flags
        do close. This row exists so that a future build which starts failing closed, or a flag
        that starts closing it, is noticed rather than assumed. It asserts the MEASURED behaviour,
        not the desirable one, and its docstring is where that distinction is stated.
        """
        import os
        import stat

        tree = _Tree(self)
        hidden = tree.src / "hidden"
        hidden.mkdir()
        (tree.src / "probe.py").rename(hidden / "probe.py")
        os.chmod(hidden, 0)
        self.addCleanup(os.chmod, hidden, stat.S_IRWXU)
        completed = tree.run()
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("Permission denied", completed.stdout + completed.stderr)

    def test_an_in_source_noqa_comment_changes_no_verdict(self) -> None:
        """The channel a leaf can actually write, since a leaf authors the source."""
        tree = _Tree(self)
        (tree.src / "probe.py").write_text(
            _DEFECTIVE_SOURCE.replace("import os\n", "import os  # noqa: F401\n", 1))
        self.assertEqual(_reported_codes(tree.run()),
                         ["E741", "F401", "F401", "F821", "F841"])
        # Negative control: without the flag the directive removes one of the two `F401`s.
        self.assertEqual(_reported_codes(tree.run(drop=("--ignore-noqa",))),
                         ["E741", "F401", "F821", "F841"])

    def test_a_gitignore_changes_no_verdict(self) -> None:
        tree = _Tree(self)
        (tree.root / ".gitignore").write_text("*.py\n")
        self.assertEqual(tree.run().returncode, 1)
        # Negative control, and the quietest of the four: exit 0 with no diagnostic at all.
        control = tree.run(drop=("--no-respect-gitignore",))
        self.assertEqual(control.returncode, 0)
        self.assertEqual(_reported_codes(control), [])

    def test_a_stale_cache_entry_changes_no_verdict(self) -> None:
        """The cache key carries neither the file size nor a content hash, so a run cached clean
        under one mtime answers for a defective file that restores it.

        THE NEGATIVE CONTROL DROPS TWO FLAGS, and that is the finding rather than a shortcut:
        `--ignore-noqa` also defeats a cache read on every measured build, so dropping
        `--no-cache` alone leaves the channel closed and the control would witness nothing. The
        backend document records the same qualification. Dropping both is what exhibits the
        poisoning, and it is exactly the argv the previous preset (`ruff check .`) would have
        used.
        """
        import os

        poisonable = ("--no-cache", "--ignore-noqa")
        tree = _Tree(self)
        clean = tree.src / "probe.py"
        clean.write_text("x = 1\n")
        stamp = (10 ** 9, 10 ** 9)
        os.utime(clean, stamp)
        cached = tree.run(drop=poisonable)
        self.assertEqual(cached.returncode, 0, cached.stdout)
        clean.write_text(_DEFECTIVE_SOURCE)
        os.utime(clean, stamp)
        # Negative control first, so a run order that happened to invalidate the entry cannot
        # make the positive row pass for the wrong reason.
        self.assertEqual(tree.run(drop=poisonable).returncode, 0)
        self.assertEqual(tree.run().returncode, 1)

    def test_the_declared_invocation_writes_no_cache_into_the_source_tree(self) -> None:
        """`.ruff_cache/` in `project_dir` is a byproduct in the leaf's own source directory."""
        tree = _Tree(self)
        tree.run()
        self.assertFalse((tree.src / ".ruff_cache").exists())
        tree.run(drop=("--no-cache",))
        self.assertTrue((tree.src / ".ruff_cache").exists())


class WiringTests(unittest.TestCase):
    def test_the_registry_reaches_this_module_as_the_lint_capability(self) -> None:
        from tools.backends import registry

        self.assertIs(registry.capability_module("linter", "ruff", "lint"), lint)
        self.assertIn("lint", registry.get("linter", "ruff").backend_provides)
        self.assertEqual(registry.get("linter", "ruff").core_provides, frozenset())

    def test_the_server_runs_the_argv_this_module_declares(self) -> None:
        """Pinned at the TABLE the tool reads, not at `_lint_preset_command`: a wiring that
        computed the right argv and then failed to put it in the table would satisfy the helper."""
        if str(REPO_ROOT / "mcp_servers") not in sys.path:
            sys.path.insert(0, str(REPO_ROOT / "mcp_servers"))
        import build_runtime_server as server

        self.assertEqual(server._LINT_PRESET_COMMANDS["ruff"], lint.check_argv())
        # argv[0] is what the launch probe looks for; a flag ahead of it would send the probe
        # after the wrong program.
        self.assertEqual(server.lint_preset_executables("ruff"), (lint.EXECUTABLE,))

    def test_the_command_log_check_attributes_this_executable_to_this_preset(self) -> None:
        """`_infer_run_linter_preset_from_command` reads argv[0] out of the backend packages.

        Before issue #120 it held its own copy of the three executable names, so a rename here
        would have left it unable to attribute a command log it is supposed to certify.
        """
        from tools.validate_pipeline_semantics import _infer_run_linter_preset_from_command

        self.assertEqual(_infer_run_linter_preset_from_command([lint.EXECUTABLE, "check"]), "ruff")
        self.assertEqual(
            _infer_run_linter_preset_from_command([f"/usr/bin/{lint.EXECUTABLE}"]), "ruff")


class BackendDocumentTests(unittest.TestCase):
    """The document is compared to the code, in that direction only."""

    def setUp(self) -> None:
        self.text = BACKEND_DOC.read_text()

    def _table_codes(self, heading: str) -> list[str]:
        section = self.text.split(heading, 1)[1]
        return re.findall(r"^\| `([A-Z]+[0-9]+)` \|", section, re.M)

    def test_the_document_states_the_declared_set_and_nothing_else(self) -> None:
        """SET IDENTITY over the whole table, not "the declared codes appear in it".

        The first version filtered the table down to rows already in one of the two constants and
        then compared — so a row for a code in NEITHER was dropped before the assertion, and
        adding `| `E501` | line-too-long |` to the Declared-set table passed 33 tests. That is the
        direction that matters: the document is what a reader takes the certified scope from, and
        the day `python` is reachable the leaf-facing checklist §Scope promises is derived from
        this table, so a code listed here and never selected is a rule a leaf is told to satisfy
        that no gate applies.
        """
        section = self.text.split("## Declared set", 1)[1].split("Codes deliberately excluded", 1)[0]
        rows = re.findall(r"^\| `([A-Z]+[0-9]+)` \|", section, re.M)
        self.assertEqual(rows, list(lint.RULE_CODES))

    def test_the_document_states_the_size_of_the_declared_set(self) -> None:
        """`The N codes below` is `len(RULE_CODES)`, so it is derived rather than transcribed.

        It was one of the numbers a reviewer's mutant changed with the suite green. Most rows in
        §Measurement are multi-build measurements a single-build suite genuinely cannot re-take;
        this one is not, and neither is `cppcheck`'s findings exit code.
        """
        self.assertIn(f"The {len(lint.RULE_CODES)} codes below.", self.text)

    def test_the_document_states_every_exclusion(self) -> None:
        stated = set(self._table_codes("Codes deliberately excluded"))
        self.assertEqual(stated, set(lint.EXCLUDED_RULE_CODES))

    def test_the_document_opens_a_bullet_for_every_channel_closing_flag(self) -> None:
        """Element by element AT ITS STATEMENT POSITION, not "the token appears somewhere".

        A flag name also occurs in this document's reproduce commands and in its measurement
        tables, so "is the string present" couples nothing — a whole channel bullet could be
        deleted with the row still green. What is required is that the flag OPEN its own bullet
        in the section that holds the enumeration.
        """
        policy = self.text.split("## Design Policy", 1)[1].split("## Declared set", 1)[0]
        opened = set(re.findall(r"^  - `(--[a-z-]+=?)` — ", policy, re.M))
        expected = {f.split("=", 1)[0] + "=" if f.endswith("=") else f
                    for f in lint.CHECK_FLAGS if f.startswith("--") and f != "--select"}
        self.assertEqual(opened, expected)

    def test_the_document_quotes_the_supported_range(self) -> None:
        self.assertIn(lint.SUPPORTED_VERSION_SPEC, self.text)

    def test_the_package_and_its_document_are_not_gitignored(self) -> None:
        for path in (REPO_ROOT / "tools" / "backends" / "linter" / "ruff" / "lint.py",
                     BACKEND_DOC):
            completed = subprocess.run(["git", "check-ignore", str(path)],
                                       cwd=str(REPO_ROOT), capture_output=True, timeout=60)
            self.assertEqual(completed.returncode, 1, f"{path} is gitignored")


class DeferredLeafChecklistTests(unittest.TestCase):
    def test_no_language_this_preset_lints_is_reachable_by_a_node(self) -> None:
        """The obligation this backend defers, tied to the gate that makes it deferrable.

        `docs/backends/linter/ruff/RULES.md` §Scope says the leaf-facing rule checklist is owed
        the day a leaf can trip these rules, and states that it is not owed today because no
        `spec` node can select the language. That second half is a claim about the registry, and
        this is where it is checked: the day a `language` backend for one of them is registered,
        this fails, and the failure names the document that has to grow.
        """
        from tools.backends import registry
        from tools.validate_pipeline_semantics import _LINT_PRESET_FOR_LANGUAGE

        ours = sorted(lang for lang, preset in _LINT_PRESET_FOR_LANGUAGE.items()
                      if preset == "ruff")
        self.assertEqual(ours, ["python"])
        for language in ours:
            self.assertIsNotNone(
                registry.unimplemented_reason("language", language),
                f"`{language}` is now an implemented language backend, so a leaf can trip the "
                f"rules this backend declares. The leaf-facing checklist deferred in "
                f"docs/backends/linter/ruff/RULES.md §Scope is now owed: add it to "
                f"docs/workflow/phases/phase_02_generate.md §2-1 beside the fortitude one, and "
                f"drop the deferral from §Scope.")


if __name__ == "__main__":
    unittest.main()
