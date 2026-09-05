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
    `test_host_prerequisites.RunbookVersionRangeTests`.

The knot the third bullet ties is new: before this file, `docs/DEVELOPMENT.md` §Fresh-machine setup step 6 spelt
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

#: The name-and-extras head of a requirement. NOT a full PEP 508 parser — these two files are
#: hand-written — but the decomposition below has to admit every shape a legitimate edit would use,
#: because refusing one is a check that makes ordinary work fail. Everything after the head is
#: taken off in STAGES rather than by one regex with optional tails, which is what the first two
#: versions did and what round 3 killed: `--?[A-Za-z][^\s]*` as an "options" tail matches
#: `-sitter==0.26.0`, so `tree-sitter==0.26.0 \` (the continuation form
#: `pip-compile --generate-hashes` emits) parsed as a distribution named `tree` with no specifier.
#: A misparse that names the wrong package is worse than the refusal it replaced.
_REQUIREMENT_HEAD_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)"
    r"(?P<extras>\[[^\]]*\])?"
    r"(?P<spec>.*)$")

#: An option token, at a whitespace boundary: `--word`, `--word=value`, or a single-letter `-X`.
#: The single-letter bound is what stops a hyphenated distribution name being read as an option.
_OPTION_RE = re.compile(r"(?:^|\s)(?:--[A-Za-z][A-Za-z0-9-]*|-[A-Za-z])(?=[\s=]|$)")

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


#: Trailing and leading punctuation a remedy wraps an argument in — backticks, a full stop, a
#: quote. `Install with \`pip install PyYAML\`.` is a real site of this rule and its argument came
#: out of a naive split as ``PyYAML`.`` , which canonicalizes to nothing this file declares.
_ARGUMENT_TRIM = "`'\".,;:)("


def _by_name_installs(text: str, declared: list[str]) -> list[str]:
    """Every `pip install <names>` in `text` that names one of `declared`.

    One predicate for the row that scans the tree and for the probe that witnesses it — the
    alternative is what round 3 found in the sibling boundary check, where a probe re-implemented
    the expression and so could not fail for any reason in the code it claimed to observe.
    """
    found = []
    for match in re.finditer(r"pip install ([^\n\"]*)", text):
        arguments = [a.strip(_ARGUMENT_TRIM) for a in match.group(1).split()]
        if arguments[:1] in (["-r"], ["--requirement"]):
            continue
        quoted = match.group(0).strip().strip(_ARGUMENT_TRIM)
        if any(_canonical(a) in declared for a in arguments if a and not a.startswith("-")):
            found.append(quoted)
        elif "{" in match.group(1):
            # An INTERPOLATED argument list is by-name by construction — the names are the very
            # distributions the caller found missing — and no literal scan can see them. That was
            # one of the five sites, and the one that mattered most: the launch refusal is the
            # only install instruction most operators meet. `-r <path>` is the legitimate
            # interpolation and is skipped above.
            found.append(quoted)
    return found


def _citation_words(text: str) -> list[str]:
    """A heading or a `§` citation, as comparable words.

    Each word is cut at its first character that a heading would not carry, and lowercased, so
    `0-1.` (as `docs/RUNBOOK.md` spells the heading), `0-1's` and `0-1:` (as prose cites it) are
    one word. Round 3's first version compared with `rstrip(".:")` and turned two CORRECT
    citations red for the possessive and the colon.
    """
    words = []
    for raw in text.split():
        word = re.match(r"[A-Za-z0-9_-]*", raw).group(0).lower()
        if word:
            words.append(word)
    return words


#: A section NUMBER, as `docs/RUNBOOK.md` opens its headings ("## 0-1. Host prerequisites").
_SECTION_NUMBER_RE = re.compile(r"^\d+(?:[-.]\d+)*$")


def _citable_names(heading: list[str]) -> list[list[str]]:
    """The word sequences that count as naming `heading`.

    Always the whole heading. And, when it opens with a section NUMBER, that number alone — which
    is how every citation of `docs/RUNBOOK.md` in this repository spells one ("§0-1"), never
    "§0-1. Host prerequisites". Without this alias the rule below has to guess where the name ends
    and the surrounding prose begins, and round 3's first two attempts each turned a set of correct
    citations red doing so.
    """
    names = [heading]
    if heading and _SECTION_NUMBER_RE.match(heading[0]):
        names.append(heading[:1])
    return names


def _headings_agree(citation: list[str], heading: list[str]) -> bool:
    """Does `citation` point at `heading`?

    A citation runs on into prose ("§Design Policy forbids …") and may also be shorter than the
    heading ("§0-1"). So a name agrees when one word list is a PREFIX of the other — which makes
    `§0-9` disagree with every heading of a document whose sections run 0-1 to 0-3, while `§0-1`
    agrees with exactly one.
    """
    if not citation or not heading:
        return False
    for name in _citable_names(heading):
        if citation[:len(name)] == name or name[:len(citation)] == citation:
            return True
    return False


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
    lines: list[str] = []
    pending = ""
    for raw in path.read_text().splitlines():
        line = re.split(r"(?:^|\s)#", raw, maxsplit=1)[0].strip()
        # pip joins a line ending in a backslash with the next one. Not joining them is how a
        # `pip-compile --generate-hashes` block reads as one requirement plus several stray
        # `--hash=` lines.
        if line.endswith("\\"):
            pending += line[:-1].strip() + " "
            continue
        line = (pending + line).strip()
        pending = ""
        if line:
            lines.append(line)
    if pending.strip():
        lines.append(pending.strip())
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


def _decompose(line: str) -> tuple[str, str]:
    """One requirement line -> (distribution name, version specifier).

    Peeled in stages, each of which removes exactly one thing:

      1. options (`--hash=...`, `--no-binary :all:`) — from the first token that IS an option;
      2. the environment marker, at the first `;`;
      3. extras, from the name;
      4. whitespace inside the specifier, so `tree-sitter == 0.26.0` and `tree-sitter==0.26.0`
         compare equal. Both are legal PEP 508 and the spaced form is what a hand edit produces;
         the previous reader refused it outright.

    Returns the specifier with its internal whitespace removed, so a comparison against a backend's
    `SUPPORTED_VERSION_SPEC` answers about the VERSION RANGE and nothing else.
    """
    option = _OPTION_RE.search(line)
    if option is not None:
        line = line[:option.start()]
    line = line.split(";", 1)[0].strip()
    match = _REQUIREMENT_HEAD_RE.match(line)
    if match is None:
        raise AssertionError(f"a line this reader cannot parse: {line!r}")
    return match.group("name"), "".join(match.group("spec").split())


def _parsed(path: Path) -> dict[str, str]:
    """canonical distribution name -> version specifier (possibly empty), for one requirements file.
    """
    found: dict[str, str] = {}
    for line in _requirement_lines(path):
        try:
            name, spec = _decompose(line)
        except AssertionError as exc:
            raise AssertionError(f"{path.name} carries {exc}") from None
        found[_canonical(name)] = spec
    return found


class CitationTests(unittest.TestCase):
    """The pointers this file's own prose hands a reader, resolved.

    Round 2 measured why this class exists: five docstrings and comments here named a sibling
    class `LinterVersionRangeTests` — written here without its module, because with one it would
    read as a citation and this row would refuse its own explanation — as the checker of the
    runbook's linter version ranges. No such class exists; the real one is
    `RunbookVersionRangeTests`. The behaviour described was accurate; the name a reader would go
    and look up was not, in every place it appeared. Four more citations in these files named a
    `docs/DEVELOPMENT.md` section that does not exist either (`§Setup`; the real heading is
    `## Fresh-machine setup`).

    A wrong pointer is not a style matter. It is what a maintainer follows to decide whether a
    property is already covered, and both of these were written by me in the same commits that
    claimed to be knotting statements to their authorities. `atmofab-enforcement-change` rule 3
    says to execute a sentence right after writing it; a citation is executable, and this is the
    execution.
    """

    def _sources(self) -> dict[str, str]:
        return {
            rel: (REPO_ROOT / rel).read_text()
            for rel in ("tools/tests/test_dependency_declaration.py", "requirements.txt",
                        "requirements-dev.txt")}

    def test_every_sibling_test_class_this_file_names_exists(self) -> None:
        import test_host_prerequisites  # noqa: PLC0415 - imported only to resolve the citations

        cited = set()
        for text in self._sources().values():
            cited.update(re.findall(
                r"test_host_prerequisites\.([A-Z][A-Za-z0-9_]*)\b", text))
        self.assertTrue(cited, "no sibling class is cited any more; this row observes nothing")
        for name in sorted(cited):
            with self.subTest(name=name):
                self.assertTrue(
                    hasattr(test_host_prerequisites, name),
                    f"this file cites test_host_prerequisites.{name}, which does not exist; a "
                    "reader following the pointer to decide whether a property is already covered "
                    "finds nothing")

    @staticmethod
    def _headings(rel: str) -> set[str]:
        """The markdown headings of a document, EMPTY ONES DROPPED.

        The empty one is not a detail. `docs/RUNBOOK.md` contains a line that is a bare `#`, so
        the set held `""`, and the word-prefix rule below compares `words[:0] == []` against it —
        true for every citation ever written. Measured in round 3's follow-up: a deliberately wrong
        `§0-9` citation was accepted on the strength of that member, i.e. the whole row was
        vacuous while its other probes passed. A family that can generate a degenerate member
        answers nothing, and this is that member.
        """
        found = {
            line.lstrip("#").strip()
            for line in (REPO_ROOT / rel).read_text().splitlines()
            if line.startswith("#")}
        return {h for h in found if h}

    #: A `§` pointer into a repository document. The first character class used to be `[A-Za-z]`,
    #: which silently dropped every `§0-1` citation — and those are the MAJORITY form here, since
    #: `docs/RUNBOOK.md`'s sections are numbered. Measured in round 3: a planted `§0-9` citation of
    #: a section that does not exist was green, and `docs/RUNBOOK.md` contributed zero checked
    #: citations while the row's "observes nothing" guard was satisfied by the DEVELOPMENT ones.
    _CITATION_RE = re.compile(r"`(docs/[A-Za-z_/.]+\.md)` §([A-Za-z0-9][^`\n,;]*)")

    def test_the_heading_reader_yields_no_empty_heading(self) -> None:
        """The self-test for the degenerate member, driven on every document this file cites.

        An empty heading makes the word-prefix comparison true for every possible citation, so
        this row is what stands between the one below and being green by construction. Also
        asserts the reader finds a heading at all: an empty SET would make every citation fail,
        which is loud, but a set that is silently one member short is not.
        """
        for rel in sorted({rel for text in self._sources().values()
                           for rel, _ in self._CITATION_RE.findall(text)}):
            with self.subTest(document=rel):
                headings = self._headings(rel)
                self.assertTrue(headings, f"{rel} yielded no headings at all")
                self.assertNotIn("", headings)
                self.assertTrue(all(h.strip() for h in headings))

    def test_the_heading_matcher_straddles_the_shapes_it_has_to_tell_apart(self) -> None:
        """The family for `_headings_agree`, on synthetic headings.

        Both directions in one table, because three successive versions of this rule were each
        correct on one side and wrong on the other: the first missed numbered citations entirely,
        the second refused `§0-1's` and `§0-1:`, the third refused `§0-1` followed by any prose
        that was not the heading's own remainder. A member that could not have come out the other
        way is not evidence, so every ACCEPT row below has a REFUSE row differing in one property.
        """
        runbook = _citation_words("0-1. Host prerequisites")
        design = _citation_words("Design Policy")
        setup = _citation_words("Fresh-machine setup")
        cases = (
            ("0-1", runbook, True),                       # the number alone
            ("0-1's table is checked", runbook, True),     # a possessive, then prose
            ("0-1: the section slice", runbook, True),     # a colon, then prose
            ("0-1 short by one", runbook, True),           # prose that is not the remainder
            ("0-1. Host prerequisites", runbook, True),    # the whole heading
            ("0-9", runbook, False),                       # a section that does not exist
            ("0-9 (\"Refused at startup", runbook, False),  # the same, with prose after it
            ("0", runbook, False),                         # a prefix of the number is not it
            ("Design Policy forbids a second copy", design, True),
            ("Design", design, True),
            ("Design Practice forbids", design, False),    # one word differs
            ("Policy", design, False),                     # the heading's TAIL is not its name
            ("Fresh-machine setup step 6", setup, True),
            ("Fresh machine setup", setup, False),         # the hyphen is part of the word
        )
        for citation, heading, expected in cases:
            with self.subTest(citation=citation, heading=" ".join(heading)):
                self.assertEqual(
                    _headings_agree(_citation_words(citation), heading), expected)
        self.assertFalse(_headings_agree([], runbook), "an empty citation matches nothing")
        self.assertFalse(_headings_agree(runbook, []), "an empty heading matches nothing")

    def test_every_document_heading_this_file_names_exists(self) -> None:
        """Markdown headings are the other pointer kind these files hand a reader.

        Two things this row must NOT do, both measured as defects in its first version:

        * refuse a correct citation of a document it happens not to read. The first version kept a
          three-entry dict and asserted membership, so citing `docs/ORCHESTRATION.md §Purpose` —
          ordinary prose work — was a hard failure naming the citation as broken. Documents are
          read on demand now, and one that does not exist is the only failure;
        * miss a whole citation SHAPE. `§0-1` is the most common pointer in these files and was
          invisible; a heading is matched on a normalized word prefix, so `§0-1` resolves against
          `## 0-1. Host prerequisites` and `§0-9` does not resolve against anything.
        """
        cited: set[tuple[str, str]] = set()
        for text in self._sources().values():
            cited.update(self._CITATION_RE.findall(text))
        self.assertTrue(cited, "no document heading is cited any more; this row observes nothing")
        by_document = {rel for rel, _ in cited}
        self.assertGreaterEqual(
            len(by_document), 2,
            f"every checked citation points at one document ({by_document}); a citation SHAPE has "
            "probably stopped being collected, which is how the numbered-section form went "
            "unchecked before")
        for rel, citation in sorted(cited):
            with self.subTest(citation=f"{rel} §{citation}"):
                path = REPO_ROOT / rel
                self.assertTrue(path.is_file(), f"this file cites {rel}, which does not exist")
                words = _citation_words(citation)
                headings = self._headings(rel)
                self.assertTrue(
                    any(_headings_agree(words, _citation_words(h)) for h in headings),
                    f"this file cites {rel} §{citation!r}, whose leading words are not a heading "
                    f"of that document (its headings: {sorted(headings)})")


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
    `test_host_prerequisites.RunbookVersionRangeTests` reads the same document for the linter table,
    and a second extractor with different termination semantics is what this file exists to avoid
    inventing.
    """

    #: The §0-1 table this check owns, found by its own header rather than by position — the form
    #: `test_host_prerequisites.RunbookVersionRangeTests` uses for the linter table in the same
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

    #: Words that may sit in front of an installer without changing what the line does. `#` is
    #: NOT one: it opens a comment, and treating it as a preamble made the trailing comment on
    #: `pip install -r requirements-dev.txt  # the two linters, plus pytest ...` parse as nine
    #: distribution names.
    _COMMAND_PREAMBLE = ("sudo", "$", ">")

    #: An installer name, as a SHAPE rather than a table. Two tables have now failed here: the
    #: first matched `pip install` alone and missed `python3 -m pip install`; the second listed
    #: four spellings and round 3 walked past it six ways — `sudo pip install`,
    #: `$ pip install` (the shell-prompt convention this repository's own blocks use),
    #: `python3.10 -m pip install` (and `requirements.txt` records 3.10, so spelling the
    #: interpreter version is the natural edit), `pip3.10 install`, `uv pip install` and
    #: `pipenv install`. Enumerating spellings is the losing line: the rule is "some pip-shaped
    #: installer", and that is what these two patterns say.
    _PIP_EXECUTABLE_RE = re.compile(r"^(?:pip|pip\d+(?:\.\d+)?|uv|pipenv)$")
    _PYTHON_EXECUTABLE_RE = re.compile(r"^python(?:\d+(?:\.\d+)?)?$")

    #: Distributions a by-name install in §0-1 may name anyway: bootstrapping the installer itself
    #: is not installing a dependency. `python3 -m pip install --upgrade pip` is the single most
    #: common preamble to installing from a requirements file, PR-3's CI step is written with it,
    #: and the refusal below rejected it as a by-name install. An explicit allowlist rather than a
    #: pattern, so a new exemption is a line someone has to read and add.
    _BOOTSTRAP_DISTRIBUTIONS = frozenset({"pip", "setuptools", "wheel", "uv"})

    @classmethod
    def _installer_arguments(cls, words: list[str]) -> list[str] | None:
        """`words` with any installer prefix removed, or None if it is not an install command."""
        while words and words[0] in cls._COMMAND_PREAMBLE:
            words = words[1:]
        if cls._PYTHON_EXECUTABLE_RE.match(words[0] if words else "") and words[1:2] == ["-m"]:
            words = words[2:]
        if len(words) >= 2 and cls._PIP_EXECUTABLE_RE.match(words[0]):
            rest = words[1:]
            # `uv pip install` / `uv run pip install`: the installer word may be followed by
            # another before `install`.
            while rest and rest[0] != "install" and cls._PIP_EXECUTABLE_RE.match(rest[0]):
                rest = rest[1:]
            if rest[:1] == ["install"]:
                return rest[1:]
        return None

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
            # A shell comment ends the command. Same boundary rule as `_effective_lines`.
            line = re.split(r"(?:^|\s)#", line, maxsplit=1)[0]
            words = cls._installer_arguments(line.strip().split())
            if words is None:
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
            named = [a for a in arguments
                     if _canonical(a) not in self._BOOTSTRAP_DISTRIBUTIONS]
            self.assertEqual(
                [], named,
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
        spellings = (
            "pip3 install", "python -m pip install", "python3 -m pip install",
            # every one of these walked past the previous version's four-entry table
            "sudo pip install", "$ pip install", "python3.10 -m pip install",
            "pip3.10 install", "uv pip install", "pipenv install",
            "sudo python3 -m pip install", "sudo pip3.10 install",
        )
        for spelling in spellings:
            with self.subTest(spelling=spelling):
                self.assertEqual(
                    self._pip_install_lines(f"{spelling} PyYAML tree-sitter\n"),
                    [(["PyYAML", "tree-sitter"], [])])
        for not_a_command in (
                "pipx install ruff\n",        # a different installer, with its own rules
                "npm install pip\n",
                "install pip\n",
                "sudo apt-get install cppcheck\n",
                "pip download PyYAML\n",      # pip, but not install
                "grep 'pip install' docs\n"):  # a mention inside another command
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


class RemedyTests(unittest.TestCase):
    """No remedy this repository PRINTS teaches a by-name install of a declared distribution.

    The rule `docs/RUNBOOK.md` §0-1 states — install from the file, because two of the three
    versions are measured — was stated in a document and enforced in a document, while FIVE places
    in the code told an operator the opposite. The worst of them is
    `tools/run_workflow.py`'s `missing_required_python_modules` detail: it is the ONLY install
    instruction most operators ever meet, because that refusal is what sends them to §0-1 in the
    first place, and it said `pip install tree-sitter tree-sitter-fortran`. It was pinned, too —
    `test_run_workflow.py` asserted the by-name string, and this branch edited that test's
    docstring without touching the assertion.

    Five statement sites of one rule is past the count at which
    `.claude/skills/atmofab-enforcement-change` rule 3-a says discipline has already lost and the
    sites must be COUPLED. This is that coupling, and it is written over the code rather than over
    the documents because the code is where the sites were.

    Bounded to what the runtime SHIPS: `tools/` and `mcp_servers/`, excluding `tools/tests/`. A
    test may quote a by-name install — several here quote the exact line the branch deleted, in
    order to describe it — and refusing that would make the rule unstatable.
    """

    #: Directories whose modules can print a remedy to an operator or a leaf.
    _REMEDY_ROOTS = ("tools", "mcp_servers")

    #: `tools/tests/` quotes by-name installs deliberately, to describe the rule. Nothing else is
    #: excluded — an earlier draft of this row also skipped `tools/backends/`, which excluded the
    #: Fortran front end's own import-failure remedy, i.e. one of the five sites the rule exists
    #: for. Measured: restoring that remedy survived the check that was written to catch it.
    _REMEDY_EXCLUDED = ("tools/tests/",)

    def _sources(self) -> list[tuple[str, str]]:
        found = []
        for root in self._REMEDY_ROOTS:
            for path in sorted((REPO_ROOT / root).rglob("*.py")):
                rel = path.relative_to(REPO_ROOT).as_posix()
                if rel.startswith(self._REMEDY_EXCLUDED) or "__pycache__" in rel:
                    continue
                found.append((rel, path.read_text(encoding="utf-8", errors="replace")))
        return found

    def _declared(self) -> list[str]:
        return sorted(_parsed(REPO_ROOT / "requirements.txt"))

    def test_no_runtime_module_prints_a_by_name_install_of_a_declared_package(self) -> None:
        declared = self._declared()
        self.assertTrue(declared, "requirements.txt declares nothing; this row observes nothing")
        offenders = []
        for rel, text in self._sources():
            for quoted in _by_name_installs(text, declared):
                offenders.append(f"{rel}: {quoted}")
        self.assertEqual(
            [], offenders,
            "a module prints a remedy telling the reader to install a declared distribution BY "
            "NAME. Two of the three carry a version this repository measured, so following it "
            "lands on a release nothing here has driven — and a printed remedy outranks a "
            f"document, because it arrives at the moment of the failure: {offenders}")

    def test_the_remedy_scan_reaches_the_module_it_was_written_for(self) -> None:
        """The self-test. Every needle this row looks for is a common string, and the row's whole
        value is that it reads `tools/run_workflow.py` — so it asserts that it does, and that the
        scan is not silently empty."""
        scanned = {rel for rel, _ in self._sources()}
        self.assertIn("tools/run_workflow.py", scanned)
        self.assertIn("tools/orchestration_runtime.py", scanned)
        self.assertIn("tools/workflow_conductor.py", scanned)
        self.assertNotIn("tools/tests/test_dependency_declaration.py", scanned)
        self.assertGreater(len(scanned), 10, "the remedy scan reads almost nothing")

    def test_the_scan_would_notice_a_by_name_remedy(self) -> None:
        """The other direction, on synthetic text: the rule has to fire on the exact spellings the
        five real sites used, and not on the `-r` form or on a distribution nobody declares."""
        declared = self._declared()
        for line, expected in (
                ("Install it with: pip install tree-sitter tree-sitter-fortran", True),
                ("Install with `pip install PyYAML`.", True),
                ("The fix is `pip install tree-sitter tree-sitter-fortran` on the host", True),
                ("install with: pip install -r requirements.txt", False),
                ("pip install --requirement requirements.txt", False),
                ("pip install pytest", False),      # not a runtime declaration
                ("pip install some-other-thing", False),
                # the interpolated form, both directions
                ("f\"pip install {names}\"", True),
                ("f\"pip install -r {requirements_path}\"", False)):
            with self.subTest(line=line):
                self.assertEqual(bool(_by_name_installs(line, declared)), expected)


class DevelopmentSetupBlockTests(unittest.TestCase):
    """`docs/DEVELOPMENT.md` §Fresh-machine setup step 6's install block states no version range.

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

        And every fence of the SECTION, not only the ones carrying the marker. Round 4 measured the
        gap: `pip install -U tree-sitter` in a SIBLING fence of step 6 was green everywhere, which
        is the same defect `test_the_setup_block_installs_no_package_by_name` exists for, closed
        for one fence and open for the next — and the section's own prose invites a second fence by
        telling a `pipx` user to run individual install commands. The marker still has to appear
        SOMEWHERE in the section, so a document that has lost the block entirely is refused rather
        than passing on an empty list.
        """
        section = document.split("| 6 |", 1)[-1].split("\n## ", 1)[0]
        fences = section.split("```")[1::2]
        self.assertTrue(
            any(self._BLOCK_MARKER in f for f in fences),
            "docs/DEVELOPMENT.md §Fresh-machine setup carries no fenced block containing "
            f"{self._BLOCK_MARKER!r}; this check cannot find the install block it is about")
        return fences

    def _document(self) -> str:
        return (REPO_ROOT / "docs" / "DEVELOPMENT.md").read_text()

    def test_the_setup_block_states_no_version_range(self) -> None:
        found = [c for block in self._setup_blocks(self._document())
                 for c in _VERSION_CONSTRAINT_RE.findall(block)]
        self.assertEqual(
            [], found,
            "docs/DEVELOPMENT.md §Fresh-machine setup step 6 states a version range again. Nothing compares a "
            "range written there to the backend that declares it, so it can tell a developer to "
            "install a build the launch probe refuses (measured on PR #125, on the sibling line "
            "in docs/RUNBOOK.md). State it in requirements-dev.txt, which is checked.")

    def test_the_setup_block_installs_no_package_by_name(self) -> None:
        """The other half of the same rule, in the other document.

        The no-range row above refuses a version CONSTRAINT and says nothing about a by-name
        install — so `pip install -U tree-sitter tree-sitter-fortran` inside the step 6 fence was
        green (measured, round 3). `-U` defeats the pin `requirements-dev.txt` pulls in through its
        include, and the outcome is the one §0-1's identical refusal exists to prevent: a developer
        on an unmeasured grammar. §0-1 refused it and this block did not, which is the shape a
        rule stated in one place and enforced in another always takes.
        """
        for block in self._setup_blocks(self._document()):
            for arguments, _ in RuntimeRequirementsTests._pip_install_lines(block):
                named = [a for a in arguments
                         if _canonical(a) not in
                         RuntimeRequirementsTests._BOOTSTRAP_DISTRIBUTIONS]
                self.assertEqual(
                    [], named,
                    "a pip install command in docs/DEVELOPMENT.md §Fresh-machine setup step 6 "
                    f"names distributions instead of installing from a requirements file "
                    f"(arguments: {arguments}); that resolves whatever version is current, which "
                    "is what installing from the file exists to avoid.")

    def test_the_block_finder_reads_every_fence_of_step_6_and_nothing_else(self) -> None:
        """The over-refusal probe and the self-test, on a synthetic document.

        It has to be synthetic: `docs/DEVELOPMENT.md` at HEAD states no version constraint
        anywhere, so driven on the real file the rows above cannot tell a bounded reader from one
        that found nothing.

        The bound is step 6's subsection — from its row of the setup table to the next `## `
        heading. Two things stay out of reach: anything BEFORE step 6 (the document may state a
        version constraint in prose or in another step's block), and anything after the section.
        A SIBLING fence inside step 6 is deliberately IN reach: round 4 measured
        `pip install -U tree-sitter` in one of those as green everywhere, which is the same defect
        the by-name row exists for, closed for one fence and open for the next.
        """
        document = (
            "# Development\n\nInstall `python3` `>=3.10,<3.14` first.\n\n"
            "```\npipx install 'ruff>=0.1,<0.2'\n```\n\n"        # before step 6: out of reach
            "| 6 | run the suite |\n\n"
            "```\nsudo apt-get install make\nfortitude>=9.9,<9.10\n```\n\n"  # sibling: in reach
            "Do not write `pip install -r requirements-dev.txt` in prose.\n\n"
            f"```\n{self._BLOCK_MARKER}\nsudo apt-get install cppcheck\n```\n\n"
            "## Configuration layers\n\n```\npipx install 'ruff>=0.3,<0.4'\n```\n")
        blocks = self._setup_blocks(document)
        self.assertEqual(
            [self._BLOCK_MARKER in b for b in blocks], [False, True],
            "the reader must return every fence of step 6, in order, and no fence outside it")
        self.assertTrue(_VERSION_CONSTRAINT_RE.findall(blocks[0]),
                        "a constraint in a SIBLING fence of step 6 must be in reach")
        self.assertEqual([], _VERSION_CONSTRAINT_RE.findall(blocks[1]))
        joined = "".join(blocks)
        self.assertNotIn(">=0.1,<0.2", joined, "a fence BEFORE step 6 must be out of reach")
        self.assertNotIn(">=0.3,<0.4", joined, "a fence AFTER the section must be out of reach")
        with self.assertRaises(AssertionError) as caught:
            self._setup_blocks("# Development\n\n| 6 | run |\n\nno block here\n")
        self.assertIn("carries no fenced block", str(caught.exception))
        # A SECOND block carrying the same command is ordinary work (PR-3 quotes this step in a
        # CI workflow); every fence of the section is read, so a constraint in any counts.
        two = document.replace("## Configuration layers",
                               f"```yaml\n  - run: {self._BLOCK_MARKER}\n```\n\n"
                               "## Configuration layers", 1)
        self.assertEqual(3, len(self._setup_blocks(two)))

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

        The sibling `test_host_prerequisites.RunbookVersionRangeTests._declared_ranges` asks the
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

        This is the coverage `docs/DEVELOPMENT.md` §Fresh-machine setup step 6 did not have: its two `pipx
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
