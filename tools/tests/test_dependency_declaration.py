"""`requirements.txt` / `requirements-dev.txt`, and that they cannot drift from what runs.

This repository had no dependency declaration until this file's branch, and the reason it is safe
to add one is the same reason `test_host_prerequisites.py` gives for its own probe: the
declaration asserts nothing on its own authority. Every name and every version range below is
compared against a source that already decides the question —

  * `tools/run_workflow.py:REQUIRED_PYTHON_MODULES` — the launch probe that refuses a host
    missing a mid-run import, by distribution name;
  * the package table in `docs/RUNBOOK.md` §0-1 — the operator-facing list, which is WIDER than
    the tuple (`PyYAML` is deliberately absent from the tuple: it is imported at module top and
    already fails legibly at launch, so it carries no reason code);
  * each `linter` backend's `SUPPORTED_VERSION_SPEC` — the range the launch probe accepts, and
    the range `docs/RUNBOOK.md` §0-1's table is checked against by
    `test_host_prerequisites.LinterVersionRangeTests`.

The knot the third bullet ties is new: before this file, `docs/DEVELOPMENT.md` §Setup step 6 spelt
those two linter ranges in an install line that NO test read, so it could tell a developer to
install a build the launch probe refuses. That line now points at `requirements-dev.txt`, and this
file is what holds it.

A fourth authority joined the three above once round 1 measured that the version half of
`requirements.txt` was held by nothing: `MEASURED_PACKAGE_VERSIONS` in
`tools/backends/language/fortran/structure.py`, which is what `MeasuredVersionTests` compares the
two pins and the runbook's table rows against.

What is deliberately NOT checked here, and it is a FORWARD statement rather than a description of
this revision: the `apt-get` line of the CI workflow that PR-3 of GitHub issue #161 will add.
There is no `.github/` in this tree. When it exists, comparing its apt line to a document would
need a hand-written executable-name -> package-name map (`bwrap` -> `bubblewrap`) and a
pip-or-apt column (`fortitude` -> `fortitude-lint`), which is a third copy of a fact two documents
already carry — the thing `docs/DEVELOPMENT.md` §Design Policy forbids. What is meant to witness
that line instead is the suite itself, wherever it runs: `test_host_prerequisites` asserts the
derived executables are on PATH at a supported version, and each linter backend's tests FAIL
rather than skip when their tool is absent.
"""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import run_workflow  # noqa: E402
from tools.backends import registry as backend_registry  # noqa: E402
from tools.backends.language.fortran import structure as fortran_structure  # noqa: E402

#: A requirement line, decomposed. NOT a full PEP 508 parser — these two files are hand-written —
#: but it has to admit every shape a legitimate edit would use, because refusing one is a check
#: that makes ordinary work fail. So `extras` and `marker` are separate groups rather than swept
#: into the version specifier: `ruff>=0.14,<0.17 ; python_version >= "3.10"` and `ruff[x]>=0.14`
#: are correct requirement lines, and an earlier version of this reader put the whole tail into
#: `spec` and then reported the character-for-character range comparison as a drift.
_REQUIREMENT_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)"
    r"(?P<extras>\[[^\]]*\])?"
    r"(?P<spec>[^;\s]*)"
    r"\s*(?P<marker>;[^-]*)?"
    r"\s*(?P<options>(?:--?[A-Za-z][^\s]*(?:[ =][^\s]+)?\s*)*)$")

#: PEP 503 name normalization. `PyYAML`, `pyyaml` and `py_yaml` are one distribution to pip, so
#: they have to be one distribution to every comparison in this file; comparing the spellings
#: refused the normalized form an operator or a tool may legitimately write.
_NAME_SEPARATORS = re.compile(r"[-_.]+")

#: A release number as the documents spell one. Two or three dotted components, so `§0-1` and a
#: bare `3.10` in prose are both out of reach of it by shape.
_VERSION_RE = re.compile(r"\b\d+\.\d+\.\d+\b")

#: ANY version constraint, in any spelling pip accepts. Deliberately wider than the `>=a.b,<c.d`
#: shape `test_host_prerequisites.RunbookVersionRangeTests` reads out of the runbook's linter
#: table, because the two are asking opposite questions. That one EXTRACTS the declared range and
#: compares it, so it must match the declaration's exact spelling; this one is used to REFUSE a
#: version constraint written where none belongs, and a refusal bound to one spelling refuses
#: nothing. Measured on this branch: with the narrow shape, re-adding
#: `pipx install 'ruff>=0.14, <0.17'` (one space), `'ruff<0.17,>=0.14'` (reversed) or
#: `'ruff~=0.14.0'` to the block the rule is about all stayed green.
_VERSION_CONSTRAINT_RE = re.compile(r"(?:===|==|!=|~=|>=|<=|>|<)\s*\d")


def _canonical(name: str) -> str:
    return _NAME_SEPARATORS.sub("-", name).lower()


def _effective_lines(path: Path) -> list[str]:
    """Every line of a requirements file that pip ACTS on, comments and blanks removed.

    One reader for both of the questions below, because the alternative is what the branch's own
    handwritten sweep killed: an `assertIn("-r requirements.txt", path.read_text())` is satisfied
    by a line reading `# -r requirements.txt`, so commenting the include out left the check green.
    A raw-text search cannot tell an instruction from a mention of one.

    A `#` opens a comment only at the start of a line or after whitespace, which is pip's own rule;
    splitting on every `#` truncates a legitimate URL fragment. No line in either file has one
    today, so this is a bound on what a later edit may write rather than a fix to a live defect.
    """
    lines = []
    for raw in path.read_text().splitlines():
        line = re.split(r"(?:^|\s)#", raw, maxsplit=1)[0].strip()
        if line:
            lines.append(line)
    return lines


def _include_targets(path: Path) -> list[str]:
    """The files a requirements file pip-includes with `-r` / `--requirement`."""
    targets = []
    for line in _effective_lines(path):
        for flag in ("-r", "--requirement"):
            if line == flag or line.startswith(flag + " "):
                targets.append(line[len(flag):].strip())
            elif flag == "--requirement" and line.startswith("--requirement="):
                targets.append(line.split("=", 1)[1].strip())
    return targets


def _requirement_lines(path: Path) -> list[str]:
    """The lines of a requirements file that state a requirement.

    An option line — pip's `-r` include and anything else beginning with `-` — is not one. That
    case is called out because dropping it silently would make `requirements-dev.txt` look as
    though it declared the runtime set too.
    """
    return [line for line in _effective_lines(path) if not line.startswith("-")]


def _parsed(path: Path) -> dict[str, str]:
    """canonical distribution name -> version specifier (possibly empty), for one requirements file.

    The specifier excludes extras and the environment marker, so a comparison against a backend's
    `SUPPORTED_VERSION_SPEC` answers about the VERSION RANGE and nothing else.
    """
    found: dict[str, str] = {}
    for line in _requirement_lines(path):
        match = _REQUIREMENT_RE.match(line)
        if match is None:  # pragma: no cover - a malformed line is a defect, not a state
            raise AssertionError(f"{path.name} carries a line this reader cannot parse: {line!r}")
        found[_canonical(match.group("name"))] = match.group("spec").strip()
    return found


class RequirementReaderTests(unittest.TestCase):
    """The readers themselves, on synthetic input where the answer is not "nothing".

    Every comparison in the two classes below is only as good as these. They are driven on
    constructed files rather than on the repository's own two, because a rule whose answer on this
    tree is the empty set cannot be observed through the assertion that consumes it — and because
    the shapes that matter here are the ones the repository does NOT contain today: the extras, the
    environment marker and the normalized spelling that a later legitimate edit may write, and
    which an earlier version of this reader refused.
    """

    def _write(self, text: str) -> Path:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        path = Path(holder.name) / "requirements.txt"
        path.write_text(text)
        return path

    def test_the_specifier_excludes_extras_and_the_environment_marker(self) -> None:
        """The over-refusal probe for the character-for-character range comparison. All three of
        these are correct requirement lines for the SAME range, and refusing any of them makes a
        legitimate edit a test failure."""
        for line in (
                'ruff>=0.14,<0.17',
                'ruff[foo]>=0.14,<0.17',
                'ruff>=0.14,<0.17 ; python_version >= "3.10"',
                'ruff[foo]>=0.14,<0.17; sys_platform == "linux"'):
            with self.subTest(line=line):
                self.assertEqual(_parsed(self._write(line + "\n")), {"ruff": ">=0.14,<0.17"})

    def test_names_are_compared_the_way_pip_compares_them(self) -> None:
        """PEP 503 normalization, in both directions.

        Case folds and the three separators collapse to one, so `tree_sitter.fortran` and
        `Tree-Sitter-Fortran` are the same distribution — comparing spellings refused the
        normalized form, which is what pip itself and most tooling emit. What must NOT happen is
        the separator disappearing: `py_yaml` is a different distribution from `PyYAML`, and a
        normalization that merged them would make this file's set comparisons accept the wrong
        package.
        """
        for spelling in ("PyYAML", "pyyaml", "PYYAML"):
            with self.subTest(spelling=spelling):
                self.assertEqual(list(_parsed(self._write(spelling + ">=5.4\n"))), ["pyyaml"])
        for spelling in ("tree_sitter.fortran", "Tree-Sitter-Fortran", "tree.sitter_fortran"):
            with self.subTest(spelling=spelling):
                self.assertEqual(
                    list(_parsed(self._write(spelling + "==0.6.0\n"))), ["tree-sitter-fortran"])
        self.assertEqual(list(_parsed(self._write("py_yaml>=5.4\n"))), ["py-yaml"])

    def test_a_comment_needs_a_boundary_before_it(self) -> None:
        """pip's own rule. Splitting on every `#` truncates a URL fragment, which is a legitimate
        thing for a requirement line to carry."""
        self.assertEqual(
            _effective_lines(self._write("pytest  # the runner\n# whole line\nfoo#bar\n")),
            ["pytest", "foo#bar"])

    def test_the_reader_refuses_a_line_it_cannot_decompose(self) -> None:
        """A malformed line must say so rather than being silently dropped, or every set
        comparison in this file quietly loses a member."""
        with self.assertRaises(AssertionError):
            _parsed(self._write("!!!not a requirement\n"))


class _RunbookReaderMixin:
    """Reading `docs/RUNBOOK.md` §0-1: the section slice, its tables, its install commands.

    One reader shared by the classes below rather than one per class — the sibling
    `test_host_prerequisites.LinterVersionRangeTests` reads the same document for the linter table,
    and a second extractor with different termination semantics is what this file exists to avoid
    inventing.
    """

    #: The §0-1 table this check owns, found by its own header rather than by position — the form
    #: `test_host_prerequisites.LinterVersionRangeTests` uses for the linter table in the same
    #: document, so this file does not invent a second table reader.
    _PACKAGE_TABLE_HEADER = "| package | purpose |"

    def _runbook(self) -> str:
        return (REPO_ROOT / "docs" / "RUNBOOK.md").read_text()

    def _packages_in_table(self, document: str) -> set[str]:
        """The CANONICAL distribution names the §0-1 package table declares, out of `document`.

        Takes the document as an argument so the over-refusal probe below drives THIS function
        rather than re-implementing it with different termination semantics.
        """
        self.assertIn(
            self._PACKAGE_TABLE_HEADER, document,
            "docs/RUNBOOK.md §0-1 no longer carries the Python package table; this check cannot "
            "find what it is supposed to compare")
        found: set[str] = set()
        for line in document.split(self._PACKAGE_TABLE_HEADER, 1)[1].splitlines()[1:]:
            if not line.startswith("|"):
                break
            cell = line.strip().strip("|").split("|")[0].strip()
            if not cell or set(cell) <= set("-:"):
                continue  # the header separator row, or an empty first cell — not a package
            found.add(_canonical(cell.strip("`")))
        return found
    #: The heading of the §0-1 subsection this file is about. Every question below is asked of
    #: THAT slice, not of the whole runbook: an earlier version searched the entire 350-line
    #: document for a `pip install` line and required there to be exactly one, so documenting any
    #: other pip-installable prerequisite anywhere in the operator's runbook turned this file red
    #: with a message about the dependency declaration.
    _SECTION_HEADING = "### Refused at startup — `missing_required_python_modules`"

    def _section(self, document: str) -> str:
        """The §0-1 subsection, up to the next heading of the same or a higher level."""
        self.assertIn(
            self._SECTION_HEADING, document,
            "docs/RUNBOOK.md no longer carries the §0-1 subsection this file is about; the checks "
            "below cannot find what they are supposed to read")
        rest = document.split(self._SECTION_HEADING, 1)[1]
        out = []
        for line in rest.splitlines():
            if line.startswith("## ") or line.startswith("### "):
                break
            out.append(line)
        return "\n".join(out)

    #: Options that take a value, so the value is not a distribution name.
    _PIP_VALUE_OPTIONS = ("-r", "--requirement", "-c", "--constraint")

    #: Every way this repository's documents could spell "run pip's install command". The first
    #: version matched `pip install` alone, and a round-2 reviewer put
    #: `python3 -m pip install PyYAML tree-sitter tree-sitter-fortran` into §0-1 — the unpinned
    #: form the section exists to refuse — with every row still green, because the reader could
    #: not see the command at all.
    _PIP_INSTALL_PREFIXES = (
        ("pip", "install"),
        ("pip3", "install"),
        ("python", "-m", "pip", "install"),
        ("python3", "-m", "pip", "install"),
    )

    @classmethod
    def _pip_install_lines(cls, block: str) -> list[tuple[list[str], list[str]]]:
        """Each pip install command in `block`, as (distribution arguments, `-r` targets).

        A COMMAND reader, not a text search. That distinction is the one this file's own history
        turned on twice: `assertIn("-r requirements.txt", document)` is satisfied by a sentence
        SAYING that earlier revisions told you to run it — measured, a round-1 reviewer deleted the
        install block, appended such a sentence to the end of the document, and the suite stayed
        green. Options are dropped rather than read as distribution names, because
        `pip install --upgrade X` is a legitimate spelling.
        """
        found = []
        for line in block.splitlines():
            words = line.strip().split()
            for prefix in cls._PIP_INSTALL_PREFIXES:
                if tuple(words[:len(prefix)]) == prefix:
                    words = words[len(prefix):]
                    break
            else:
                continue
            arguments: list[str] = []
            includes: list[str] = []
            pending = None
            for word in words:
                if pending is not None:
                    includes.append(word)
                    pending = None
                    continue
                if word.startswith("--requirement="):
                    includes.append(word.split("=", 1)[1])
                    continue
                if word.startswith("-"):
                    pending = word if word in cls._PIP_VALUE_OPTIONS else None
                    continue
                arguments.append(word)
            found.append((arguments, includes))
        return found

    @classmethod
    def _pip_install_arguments(cls, block: str) -> list[list[str]]:
        return [arguments for arguments, _ in cls._pip_install_lines(block)]




class RuntimeRequirementsTests(_RunbookReaderMixin, unittest.TestCase):
    """`requirements.txt` against the two authorities that already decide what a host needs."""

    def test_the_runtime_declaration_is_exactly_the_runbook_package_table(self) -> None:
        """Both directions at once, and the authority is the RUNBOOK table rather than
        `REQUIRED_PYTHON_MODULES`: the tuple deliberately omits `PyYAML` (`tools/run_workflow.py`
        says why), so making the tuple the authority would report the correct file as wrong."""
        self.assertEqual(
            set(_parsed(REPO_ROOT / "requirements.txt")),
            self._packages_in_table(self._runbook()),
            "requirements.txt and the package table in docs/RUNBOOK.md §0-1 disagree about which "
            "distributions the host needs")

    def test_every_module_the_launch_probe_refuses_a_host_for_is_declared(self) -> None:
        """The narrower authority, checked separately because it NAMES the missing member.

        `REQUIRED_PYTHON_MODULES` is a subset of the table; a distribution the probe refuses a
        host for and this file does not install is a machine that passes `pip install -r` and then
        fails at launch.
        """
        declared = set(_parsed(REPO_ROOT / "requirements.txt"))
        for import_name, distribution in run_workflow.REQUIRED_PYTHON_MODULES:
            self.assertIn(
                _canonical(distribution), declared,
                f"tools/run_workflow.py refuses a host missing {import_name!r} (distribution "
                f"{distribution!r}), but requirements.txt does not install it")

    def test_the_runbook_table_covers_every_module_the_launch_probe_names(self) -> None:
        """The knot between the two authorities themselves, which no test tied before this file.

        Without it the pair can agree with `requirements.txt` and with each other's absence: a new
        entry added to `REQUIRED_PYTHON_MODULES` alone leaves the operator-facing install line in
        `docs/RUNBOOK.md` §0-1 short by one, and an operator who follows it gets refused at launch.
        """
        table = self._packages_in_table(self._runbook())
        for import_name, distribution in run_workflow.REQUIRED_PYTHON_MODULES:
            self.assertIn(
                _canonical(distribution), table,
                f"tools/run_workflow.py refuses a host missing {import_name!r} (distribution "
                f"{distribution!r}), but the package table in docs/RUNBOOK.md §0-1 omits it")

    def test_no_install_command_in_the_section_installs_by_NAME(self) -> None:
        """§0-1 installs from the file. A command naming distributions is what it refuses.

        This replaces a check that compared a by-name line's argument set to the table, and the
        replacement is the point: the table IS those three names, so re-adding the very line this
        branch deleted — `pip install PyYAML tree-sitter tree-sitter-fortran` — satisfied it. The
        section's own prose says "Install from the file, not from the names", and the earlier row
        agreed with the names. Measured by a round-2 reviewer: that line, and
        `python3 -m pip install numpy matplotlib` naming packages no authority declares at all,
        were both green.

        The rule is a REFUSAL of by-name installs in this section, not a comparison of them, so
        there is nothing left for a restored line to agree with. The table keeps naming the three
        packages: a table is a description and a command is an instruction, and only the second is
        something an operator runs.
        """
        section = self._section(self._runbook())
        commands = self._pip_install_lines(section)
        self.assertTrue(
            commands,
            "docs/RUNBOOK.md §0-1 no longer carries any pip install command at all; this check "
            "and the pinned-install one below both stop observing anything")
        for arguments, _ in commands:
            self.assertEqual(
                [], arguments,
                "a pip install command in docs/RUNBOOK.md §0-1 names distributions instead of "
                f"installing from requirements.txt (arguments: {arguments}). Two of the three "
                "versions are measured; a by-name install resolves whatever is current.")

    def test_the_runbook_points_the_operator_at_the_pinned_versions(self) -> None:
        """§0-1 has to install from the FILE, because two of the three versions are measured.

        `tools/backends/language/fortran/structure.py` records the Fortran front end as pinned by
        measurement at `tree-sitter` 0.26.0 and `tree-sitter-fortran` 0.6.0; an operator who types
        the three names gets whatever is current, and the `Generate.gate` structure read that
        follows has not been measured on what it is running — a wrong verdict reached part-way
        into a billed run.

        Asked of the COMMAND, and only inside §0-1. Two earlier versions of this row were text
        searches, and a round-1 reviewer killed the second by deleting the install block and
        appending "earlier revisions told you to run `pip install -r requirements.txt`; do NOT do
        that" to the end of the document — green, with the instruction negated.
        """
        section = self._section(self._runbook())
        includes = [target for _, targets in self._pip_install_lines(section) for target in targets]
        self.assertIn(
            "requirements.txt", includes,
            "docs/RUNBOOK.md §0-1 no longer carries a `pip install -r requirements.txt` COMMAND; "
            "an operator following the section installs whatever version is current")

    def test_the_command_reader_does_not_read_a_sentence_about_a_command(self) -> None:
        """The self-test for `_pip_install_lines`, driven on synthetic text.

        This is the reader both rows above rest on, and it is what replaced two successive text
        searches. Both directions: a real command line is decomposed, and prose that quotes the
        same command — including prose that NEGATES it, which is how a reviewer killed the previous
        version — yields nothing.
        """
        self.assertEqual(
            self._pip_install_lines("pip install -r requirements.txt\n"),
            [([], ["requirements.txt"])])
        self.assertEqual(
            self._pip_install_lines("pip install --upgrade PyYAML tree-sitter\n"),
            [(["PyYAML", "tree-sitter"], [])])
        self.assertEqual(
            self._pip_install_lines("pip install --requirement=requirements.txt\n"),
            [([], ["requirements.txt"])])
        for spelling in ("pip3 install", "python -m pip install", "python3 -m pip install"):
            with self.subTest(spelling=spelling):
                self.assertEqual(
                    self._pip_install_lines(f"{spelling} PyYAML tree-sitter\n"),
                    [(["PyYAML", "tree-sitter"], [])])
        for not_a_command in ("pipx install ruff\n", "npm install pip\n", "install pip\n"):
            with self.subTest(not_a_command=not_a_command):
                self.assertEqual(self._pip_install_lines(not_a_command), [])
        for prose in (
                "Historical note: earlier revisions told you to run `pip install -r "
                "requirements.txt`; do NOT do that.\n",
                "Do not `pip install PyYAML` by hand.\n"):
            with self.subTest(prose=prose):
                self.assertEqual(self._pip_install_lines(prose), [])

    def test_the_section_slicer_stops_at_the_next_heading(self) -> None:
        """The bound on the reader, self-tested. Without it every row above is asked of the whole
        document again, which is the over-refusal round 1 reported."""
        document = (
            f"# Runbook\n\n{self._SECTION_HEADING}\n\npip install -r requirements.txt\n\n"
            "### Another subsection\n\npip install matplotlib\n")
        self.assertEqual(
            self._pip_install_lines(self._section(document)), [([], ["requirements.txt"])])
        with self.assertRaises(AssertionError) as caught:
            self._section("# Runbook\n\nno such section\n")
        self.assertIn("no longer carries the §0-1 subsection", str(caught.exception))

    def test_a_table_elsewhere_in_the_document_is_not_this_check_s_business(self) -> None:
        """The over-refusal probe, driving the REAL extractor over a synthetic document.

        `docs/RUNBOOK.md` carries several tables whose first column is a name in backticks. Only
        the one under this header is a package declaration; a `| tool | purpose |` table of CLI
        tools, or a prose paragraph naming a package, may not be read as one.
        """
        document = (
            "# Runbook\n\n| tool | purpose |\n|---|---|\n| `jq` | extracting shell variables |\n"
            f"\n{self._PACKAGE_TABLE_HEADER}\n|---|---|\n"
            "| `zzpkg` | the thing it is for |\n"
            "\nAfterwards install `cmake`, which is not a Python package.\n")
        self.assertEqual(self._packages_in_table(document), {"zzpkg"})

    def test_the_extractor_refuses_a_document_whose_table_is_gone(self) -> None:
        """A renamed or deleted table must say so, not silently compare an empty set."""
        with self.assertRaises(AssertionError) as caught:
            self._packages_in_table("# Runbook\n\nno table here\n")
        self.assertIn("no longer carries the Python package table", str(caught.exception))


class MeasuredVersionTests(_RunbookReaderMixin, unittest.TestCase):
    """The PINS, against the code that measured them.

    `fa1b4d6` declared `tree-sitter==0.26.0` and `tree-sitter-fortran==0.6.0` and checked neither.
    Round 1 measured the consequence: widening either to `>=`, or moving `tree-sitter-fortran` to
    `0.5.0` while three other places said 0.6.0, left the whole file green. The version half of
    `requirements.txt` was the half this branch exists to add, and it was the half nothing held.

    The authority is `tools/backends/language/fortran/structure.MEASURED_PACKAGE_VERSIONS` — the
    backend that depends on the packages, and the only place that can answer what was measured. The
    same fact used to be spelt in that module's docstring, in its refusal message, and in
    `docs/RUNBOOK.md`; the rule below is the coupling rule 3-a of
    `.claude/skills/atmofab-enforcement-change` prescribes at three statement sites.
    """

    def _measured(self) -> dict[str, str]:
        return dict(fortran_structure.MEASURED_PACKAGE_VERSIONS)

    def test_every_measured_version_is_pinned_in_the_runtime_declaration(self) -> None:
        """`==` and the measured value, for each. A floor or a different value is a machine the
        `Generate.gate` structure read was never measured on, reached part-way into a billed run.
        """
        parsed = _parsed(REPO_ROOT / "requirements.txt")
        for distribution, version in sorted(self._measured().items()):
            name = _canonical(distribution)
            with self.subTest(distribution=distribution):
                self.assertIn(
                    name, parsed,
                    f"{distribution} is measured by "
                    f"tools/backends/language/fortran/structure.py but requirements.txt does not "
                    f"install it")
                self.assertEqual(
                    parsed[name], f"=={version}",
                    f"requirements.txt does not pin {distribution} at the measured version "
                    f"{version}; a host installing from this file runs the Fortran structure "
                    f"front end on a release nothing in this repository has measured")

    def test_the_runbook_states_the_measured_version_beside_the_package(self) -> None:
        """The operator-facing half. §0-1 tells a reader what the front end was written against;
        a value there that is not the measured one sends whoever reads it to the wrong release.

        Bounded to the LINES of §0-1 that name the distribution, so a version number belonging to
        anything else in the runbook cannot fire this — and self-tested below, because a bound that
        matches nothing is a green row observing nothing.
        """
        section = self._section(self._runbook())
        checked = 0
        for distribution, version in sorted(self._measured().items()):
            for line in section.splitlines():
                found = set(_VERSION_RE.findall(line))
                if distribution not in line or not found:
                    continue
                # `tree-sitter` is a substring of `tree-sitter-fortran`; a line naming the longer
                # one states the longer one's version, so it is not this row's business.
                if distribution == "tree-sitter" and "tree-sitter-fortran" in line:
                    continue
                checked += 1
                self.assertIn(
                    version, found,
                    f"docs/RUNBOOK.md §0-1 states a version beside {distribution!r} that is not "
                    f"the measured {version}:\n  {line.strip()}")
        self.assertGreaterEqual(
            checked, 2,
            "no line of docs/RUNBOOK.md §0-1 states a version beside a measured package. Either "
            "the statement this check exists for is gone, or a new `###` subsection was inserted "
            "inside §0-1 and the package table now falls outside the slice this check reads — "
            "check which before editing the table")

    def test_the_refusal_message_renders_from_the_same_constant(self) -> None:
        """The third statement site: what a host with the wrong grammar is TOLD to pin.

        A message naming a version the declaration does not pin is a remedy that cannot converge —
        it sends the reader to a release `requirements.txt` refuses.

        WHAT THIS PINS AND WHAT IT DOES NOT. It pins that the raise site REFERENCES
        `MEASURED_PACKAGE_VERSIONS`, read out of the module's own source. It is not a behavioural
        witness: the raise needs an installed grammar missing a node type this front end reads, and
        this repository's own measurement records that no published `tree-sitter-fortran` from
        0.2.0 to 0.6.0 is such a grammar — so the branch is unreachable with anything installable
        today, and it is written to guard the NEXT release. A reader who changes that message must
        keep the reference; nothing here observes the rendered string.
        """
        source = REPO_ROOT / "tools" / "backends" / "language" / "fortran" / "structure.py"
        rendered = source.read_text()
        raise_site = rendered.split("FORTRAN_STRUCTURE_UNAVAILABLE_MARKER} the installed", 1)
        self.assertEqual(
            len(raise_site), 2,
            "the fortran structure module no longer carries the grammar refusal this row is "
            "about; it cannot check what that message tells a host to pin")
        self.assertIn(
            "MEASURED_PACKAGE_VERSIONS['tree-sitter-fortran']", raise_site[1][:800],
            "the grammar refusal in tools/backends/language/fortran/structure.py no longer renders "
            "its version from MEASURED_PACKAGE_VERSIONS, so the version it tells a host to pin can "
            "drift away from the one requirements.txt installs")

    def test_the_runbook_bound_does_not_read_a_version_belonging_to_something_else(self) -> None:
        """The over-refusal probe for the row above, on a synthetic section: an unrelated
        prerequisite's version, and a line naming the longer package, must not be read as the
        shorter package's."""
        checked = []
        section = (
            "Install `cmake` 3.20 first.\n"
            "| `tree-sitter` | the parser runtime, 0.26.0 |\n"
            "| `tree-sitter-fortran` | the grammar (written against 0.6.0) |\n")
        for distribution, version in (("tree-sitter", "0.26.0"), ("tree-sitter-fortran", "0.6.0")):
            for line in section.splitlines():
                found = set(_VERSION_RE.findall(line))
                if distribution not in line or not found:
                    continue
                if distribution == "tree-sitter" and "tree-sitter-fortran" in line:
                    continue
                checked.append((distribution, sorted(found)))
        self.assertEqual(
            checked,
            [("tree-sitter", ["0.26.0"]), ("tree-sitter-fortran", ["0.6.0"])],
            "the line bound either read the cmake version or attributed the grammar's version to "
            "the runtime")


class DevelopmentSetupBlockTests(unittest.TestCase):
    """`docs/DEVELOPMENT.md` §Setup step 6's install block states no version range.

    The property this branch bought and nothing was holding. Before it, that block spelt
    `pipx install \'fortitude-lint>=0.8,<0.10\'` and `pipx install \'ruff>=0.14,<0.17\'` and NO test
    read them — measured on `origin/main` (738fca4) by the correctness axis and re-measured here:
    drifting the fortitude range to `<0.11` there leaves `test_host_prerequisites`,
    `test_development_doc_sync`, `test_readme_sync` and `test_backend_boundary` all green. That is
    the exact defect PR #125 measured on `docs/RUNBOOK.md`'s own install line, sitting in a second
    document.

    Deleting the ranges does not stop them coming back, so the deletion is turned into a rule: the
    block installs from `requirements-dev.txt`, which IS checked, and states no range of its own.
    Two documents may state these ranges — `requirements-dev.txt` and `docs/RUNBOOK.md` §0-1's
    table — and both are compared to the backends\' declarations.
    """

    _BLOCK_MARKER = "pip install -r requirements-dev.txt"

    def _setup_blocks(self, document: str) -> list[str]:
        """EVERY fenced block of `document` that carries the dev install command.

        Bounded to the blocks rather than to the document: `docs/DEVELOPMENT.md` may legitimately
        state a version constraint in prose about something else, and refusing that would be the
        same over-refusal `test_host_prerequisites` records having made once at document scope.
        Takes the document as an argument so the probe below drives THIS reader — the sibling file
        records a version of the same check that re-implemented its extractor inline and so could
        not fail for any reason in the code it claimed to witness.

        ALL of them, not "the one": the first version required exactly one such block and failed
        the whole file when a second appeared. PR-3 of this issue adds a CI workflow whose own step
        is this command, and a document quoting that step in a second fence is ordinary work. The
        rule applies to each block equally, so there is no reason to insist there is one.
        """
        holding = [f for f in document.split("```")[1::2] if self._BLOCK_MARKER in f]
        self.assertTrue(
            holding,
            "docs/DEVELOPMENT.md carries no fenced block containing "
            f"{self._BLOCK_MARKER!r}; this check cannot find the install block it is about")
        return holding

    def _document(self) -> str:
        return (REPO_ROOT / "docs" / "DEVELOPMENT.md").read_text()

    def test_the_setup_block_states_no_version_range(self) -> None:
        found = [c for block in self._setup_blocks(self._document())
                 for c in _VERSION_CONSTRAINT_RE.findall(block)]
        self.assertEqual(
            [], found,
            "docs/DEVELOPMENT.md §Setup step 6 states a version range again. Nothing compares a "
            "range written there to the backend that declares it, so it can tell a developer to "
            "install a build the launch probe refuses (measured on PR #125, on the sibling line "
            "in docs/RUNBOOK.md). State it in requirements-dev.txt, which is checked.")

    def test_the_block_finder_is_bounded_to_the_block(self) -> None:
        """The over-refusal probe and the self-test, on a synthetic document.

        It has to be synthetic: `docs/DEVELOPMENT.md` at HEAD states no version range anywhere, so
        driven on the real file the row above cannot tell a bounded reader from one that found
        nothing. Three things must be out of the reader\'s reach — a range in prose, a range in a
        DIFFERENT fenced block, and the marker appearing in prose rather than in a fence.
        """
        document = (
            "# Development\n\nInstall `python3` `>=3.10,<3.14` first.\n\n"
            "```\nsudo apt-get install make\nfortitude>=9.9,<9.10\n```\n\n"
            "Do not write `pip install -r requirements-dev.txt` in prose.\n\n"
            f"```\n{self._BLOCK_MARKER}\nsudo apt-get install cppcheck\n```\n\n"
            "And afterwards `gfortran` `>=11.0,<15.0`.\n")
        blocks = self._setup_blocks(document)
        self.assertEqual(1, len(blocks))
        self.assertIn(self._BLOCK_MARKER, blocks[0])
        self.assertEqual([], _VERSION_CONSTRAINT_RE.findall(blocks[0]))
        with self.assertRaises(AssertionError) as caught:
            self._setup_blocks("# Development\n\nno block here\n")
        self.assertIn("carries no fenced block", str(caught.exception))
        # A SECOND block carrying the same command is ordinary work (PR-3 quotes this step in a
        # CI workflow); both are read, and a constraint in either is caught.
        two = document + f"\n```yaml\n  - run: {self._BLOCK_MARKER}\n```\n"
        self.assertEqual(2, len(self._setup_blocks(two)))

    def test_the_reader_sees_a_constraint_in_every_spelling_pip_accepts(self) -> None:
        """The other direction, and the one that decides whether the row above can ever fail.

        A family that straddles the SPELLING, not one shape repeated. The first version of this
        rule matched only `>=a.b,<c.d` with no space and in that order, and a round-2 reviewer
        walked straight past it with a space, with the operands reversed, and with `~=` — three
        ordinary ways to write the same instruction, all green. More members of one shape would not
        have closed that; a family whose members differ in the property the rule is about does.
        """
        cases = {
            "pipx install 'fortitude-lint>=0.8,<0.10'": True,   # what origin/main carried
            "pipx install 'ruff>=0.14, <0.17'": True,           # a space
            "pipx install 'ruff<0.17,>=0.14'": True,            # reversed
            "pipx install 'ruff~=0.14.0'": True,                # compatible-release
            "pipx install 'ruff==0.16.5'": True,                # an exact pin
            "pipx install 'ruff!=0.15.0'": True,                # an exclusion
            "pip install -r requirements-dev.txt": False,       # the line that belongs there
            "sudo apt-get install cppcheck": False,             # and its neighbour
            "# see docs/RUNBOOK.md 0-1 for the ranges": False,  # a pointer, not a constraint
        }
        for line, expected in cases.items():
            document = f"# Development\n\n```\n{self._BLOCK_MARKER}\n{line}\n```\n"
            with self.subTest(line=line):
                found = _VERSION_CONSTRAINT_RE.findall(self._setup_blocks(document)[0])
                self.assertEqual(bool(found), expected)


class DevRequirementsTests(unittest.TestCase):
    """`requirements-dev.txt` against the `linter` backends' own declarations."""

    def _declared_ranges(self) -> dict[str, str]:
        """backend id -> `SUPPORTED_VERSION_SPEC`, for every implemented linter that lints.

        The sibling `test_host_prerequisites.LinterVersionRangeTests._declared_ranges` asks the
        same question with `backend_ids`; this one asks `implemented_backend_ids`, so a linter that
        is DECLARED but whose package has not been extracted is outside this file rather than
        inside it with no module to import. The two answers are identical on this tree
        (`cppcheck`, `fortitude`, `mixed`, `ruff` are all implemented) and are not the same walk —
        an earlier version of this docstring said they were, which would have sent a later reader
        past the difference. The walk is repeated rather than imported because importing a sibling
        test's helper couples the two files' fixtures; what must not be duplicated is the
        CONSTANT, and it is not — both ask the registry.
        """
        found: dict[str, str] = {}
        for backend_id in backend_registry.implemented_backend_ids("linter"):
            if "lint" not in backend_registry.get("linter", backend_id).backend_provides:
                continue
            module = backend_registry.capability_module("linter", backend_id, "lint")
            found[backend_id] = module.SUPPORTED_VERSION_SPEC
        return found

    def _pip_installable_ranges(self) -> dict[str, str]:
        """The subset of the above whose tool pip can install, keyed by DISTRIBUTION name.

        `cppcheck` is a system package, so its range is documented and not declared here; the map
        below is the one place this file states a distribution name that a module does not, and it
        is asserted against the registry's own backend ids so a new linter cannot land unnoticed.
        """
        distributions = {"fortitude": "fortitude-lint", "ruff": "ruff"}
        apt_only = {"cppcheck"}
        declared = self._declared_ranges()
        self.assertEqual(
            set(declared), set(distributions) | apt_only,
            "a linter backend was added or removed; requirements-dev.txt and this map have to say "
            "whether pip installs it")
        return {_canonical(distributions[b]): spec
                for b, spec in declared.items() if b in distributions}

    def test_every_pip_installable_linter_range_is_the_range_its_backend_declares(self) -> None:
        """Character for character, in both directions.

        This is the coverage `docs/DEVELOPMENT.md` §Setup step 6 did not have: its two `pipx
        install '<name><range>'` lines spelt these ranges and no test read them, so they could
        drift out of the declared range exactly the way `docs/RUNBOOK.md`'s install line did
        (measured on PR #125). Step 6 now points at this file instead.
        """
        expected = self._pip_installable_ranges()
        parsed = _parsed(REPO_ROOT / "requirements-dev.txt")
        self.assertEqual(
            {name: parsed.get(name) for name in expected}, expected,
            "requirements-dev.txt states a linter version range that is not the one its backend "
            "declares; a developer following this file installs a build the launch probe refuses")

    def test_no_line_installs_a_linter_backend_under_the_wrong_distribution_name(self) -> None:
        """The failure this direction exists for: a line spelling a linter's BACKEND ID where its
        distribution name belongs — `fortitude` rather than `fortitude-lint`.

        Written as an identity against the registry's backend ids, deliberately NOT as a substring
        search. The first version refused any requirement whose name merely contained `lint` or
        `ruff`, which makes `pylint`, `yamllint`, `sqlfluff` and every `flake8-*` plugin — all
        ordinary developer tools with no bearing on a `linter` backend — a test failure. Refusing
        legitimate work is the error direction this repository's review loop records as its
        default, and a substring over an open namespace is that error in its cheapest form.
        """
        installable = set(self._pip_installable_ranges())
        declared_ids = {_canonical(b) for b in self._declared_ranges()}
        wrong = {
            name for name in _parsed(REPO_ROOT / "requirements-dev.txt")
            if name in declared_ids and name not in installable}
        self.assertEqual(
            set(), wrong,
            "requirements-dev.txt names a linter by its BACKEND ID; pip installs it under a "
            f"different distribution name (offending: {sorted(wrong)})")

    def test_the_dev_file_installs_the_runner_the_suite_is_started_with(self) -> None:
        """The one line that makes this file's stated purpose true, and nothing read it.

        Round 1 measured it: deleting `pytest` from `requirements-dev.txt` left every row green.
        The authority is not a name written here — it is `pytest.ini` at the repository root, which
        is a pytest configuration file and is what decides that the suite is started with pytest
        (`testpaths`, `addopts`, the `slow` marker). While that file exists, a dev requirements
        file that does not install pytest describes a machine that cannot run the suite.
        """
        self.assertTrue(
            (REPO_ROOT / "pytest.ini").is_file(),
            "there is no pytest.ini at the repository root; this row's authority for requiring "
            "pytest is gone and the row has to be re-decided rather than left passing")
        self.assertIn(
            "pytest", _parsed(REPO_ROOT / "requirements-dev.txt"),
            "pytest.ini configures this repository's suite, but requirements-dev.txt does not "
            "install pytest — a machine built from this file cannot run the suite it is for")

    def test_the_dev_file_includes_the_runtime_file(self) -> None:
        """`pip install -r requirements-dev.txt` has to be sufficient to run the suite, which
        imports the runtime dependencies. Without the include it installs a set that cannot.

        Asked of the lines pip ACTS on, not of the file's text: the first version of this row
        searched the raw text, and the branch's handwritten sweep killed it by commenting the
        include out — `# -r requirements.txt` contains the string the assertion looked for.
        """
        self.assertIn(
            "requirements.txt", _include_targets(REPO_ROOT / "requirements-dev.txt"),
            "requirements-dev.txt no longer includes requirements.txt, so installing it alone "
            "leaves the suite without the runtime dependencies it imports")

    def test_the_include_reader_does_not_read_a_commented_out_include(self) -> None:
        """The self-test for `_include_targets`, which is the only thing standing between the row
        above and the false green it used to give. Both directions: a live include is found, and
        the same line commented out is not — so a later change that stops stripping comments turns
        this red rather than turning the row above vacuous."""
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        scratch = Path(holder.name)
        live = scratch / "live.txt"
        live.write_text("-r requirements.txt\npytest\n")
        self.assertEqual(_include_targets(live), ["requirements.txt"])
        dead = scratch / "dead.txt"
        dead.write_text("# -r requirements.txt\npytest\n")
        self.assertEqual(_include_targets(dead), [])
        long_form = scratch / "long.txt"
        long_form.write_text("--requirement requirements.txt\n")
        self.assertEqual(_include_targets(long_form), ["requirements.txt"])

    def test_the_reader_does_not_mistake_the_include_for_a_requirement(self) -> None:
        """The self-test for `_requirement_lines`: `-r requirements.txt` must not come back as a
        distribution named `-r`, and a comment-only line must not come back at all. Both are what
        every count in this file rests on."""
        self.assertNotIn("-r", _parsed(REPO_ROOT / "requirements-dev.txt"))
        self.assertNotIn("requirements.txt", _parsed(REPO_ROOT / "requirements-dev.txt"))
        self.assertTrue(_parsed(REPO_ROOT / "requirements-dev.txt"),
                        "the reader returned nothing at all, so every comparison above is vacuous")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
