#!/usr/bin/env python3
"""The two documents that are MAPS: every place they name has to be real.

`docs/DEVELOPMENT.md` is built out of citations, and `docs/README.md` is an index. Neither
carries content of its own, so what can rot in them is where they point. A restatement goes
stale loudly — two documents disagree and a reader notices — while a citation to a section that
was renumbered or renamed goes stale SILENTLY, and reads as authoritative the whole time.

## What this file will and will not do

The first version of this file parsed prose, and one review round broke it eight ways: a bare
`§Section name` swallowed the sentence that followed it, a citation inside a fenced block shown
as a counter-example was refused, and a backticked `*.md` anywhere in the document had to exist
— which refused `docs/design/<name>.md` in a sentence about where a new decision note goes.
Every one of those was an OVER-REFUSAL of correct writing.

So the question was made weaker and answerable, and the shape is the point:

- **A citation is a declared form, not a thing to be parsed out of prose.** In
  `docs/DEVELOPMENT.md` a section citation is written with the name always quoted, and
  `test_no_citation_uses_the_unquoted_form` enforces that spelling — so narrowing the reader is
  not a silent weakening, and a citation this file cannot read fails loudly.
- **A path is an assertion only inside a TABLE ROW.** The tables are the maps; the prose around
  them is free to name a hypothetical file, a spec artifact, or a path that does not exist yet.
- **Fenced code blocks are stripped before every scan**, in both documents, so a block may show
  any spelling at all — including a wrong one, as a counter-example.

Completeness is NOT asserted anywhere here. Requiring `docs/README.md` to index every
`docs/*.md` would fail on 9 documents that predate this file (measured 2026-08-21), and
requiring `README.md`'s layout block to list every top-level entry would refuse an ordinary new
directory until someone documented it. Both are pins on a RESULT rather than on a rule. What is
asserted is that every place these documents name resolves — the direction with no legitimate
counter-example, since nothing is gained by pointing at something that is not there.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEVELOPMENT_DOC = REPO_ROOT / "docs" / "DEVELOPMENT.md"

#: The declared citation form. The section name is ALWAYS quoted; see the module docstring for
#: why the unquoted form is refused rather than parsed.
_CITATION_RE = re.compile(r"`(?P<path>[A-Za-z0-9_./-]+\.md)`\s*§\s*\"(?P<section>[^\"]+)\"")

#: A section mark not followed by a quoted name. Its own check, so that narrowing the reader
#: above cannot silently exempt a citation instead of refusing it.
_UNQUOTED_CITATION_RE = re.compile("§\\s*(?!\")")

#: A heading line, any depth. Applied only to fence-stripped text.
_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<text>.+?)\s*$")

#: A trailing explicit anchor, which this repository writes on some headings.
_ANCHOR_SUFFIX_RE = re.compile(r"\s*\{#[^}]*\}\s*$")

#: A fenced block delimiter, possibly indented, possibly carrying an info string.
_FENCE_RE = re.compile(r"^\s{0,3}(?P<fence>`{3,}|~{3,})")

#: The header cell naming the column this check reads. Located by NAME, never by position:
#: two earlier versions read a fixed column index, and adding an ordinary `note` column to the
#: table made every row unclassifiable while the message named the wrong thing.
_TRACKED_HEADER = "tracked"


def _strip_fences(text: str) -> str:
    """Blank every fenced code block, preserving the line count.

    Blanking rather than deleting so a heading regex cannot join two lines a block separated,
    and so any future line-numbered message stays honest.
    """
    out: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        match = _FENCE_RE.match(line)
        if fence is None:
            if match:
                fence = match.group("fence")[0]
                out.append("")
                continue
            out.append(line)
            continue
        if match and match.group("fence")[0] == fence:
            fence = None
        out.append("")
    return "\n".join(out)


def _headings(path: Path) -> list[str]:
    return [
        m.group("text")
        for m in (_HEADING_RE.match(line)
                  for line in _strip_fences(path.read_text(encoding="utf-8")).splitlines())
        if m
    ]


def _heading_names(heading: str) -> set[str]:
    """The names a heading answers to: its full text, its number, and its title.

    This repository numbers headings, and a citation legitimately names either half. Both are
    EXACT, never a prefix. Prefix matching was the first version, and it resolved a citation to
    a section renumbered from `0-2` to `0-2a` — silently, which is the one failure mode this
    file exists to catch.
    """
    text = _ANCHOR_SUFFIX_RE.sub("", heading.strip()).strip("`").strip()
    names = {text}
    number, separator, title = text.partition(". ")
    if separator and re.fullmatch(r"[0-9]+(?:-[0-9]+)*", number):
        names.add(number)
        names.add(title.strip())
    return {n.casefold() for n in names if n}


def _table_rows(text: str) -> list[str]:
    """The lines that are table rows. The caller strips fences first."""
    return [line for line in text.splitlines() if line.lstrip().startswith("|")]


def _split_row(line: str) -> list[str]:
    """The cells of one markdown table row, outer pipes dropped.

    An escaped pipe inside a cell is not a separator; it is restored after the split so a cell
    may legitimately contain one.
    """
    body = line.strip().strip("|")
    return [cell.replace("\x00", "|").strip()
            for cell in body.replace("\\|", "\x00").split("|")]


def _layer_rows(text: str) -> list[tuple[str, str]]:
    """`(path, tracked-cell)` for every row of the table that HAS a `tracked` column.

    Both columns are found by content rather than by position — the tracked one by its header
    name, the path by being the row's first backticked cell — so the table may gain, drop or
    reorder columns without this check reading the wrong cell or refusing the edit. A table with no such header
    contributes no rows, which the floor below turns into a loud failure rather than a silent
    pass.
    """
    rows: list[tuple[str, str]] = []
    index: int | None = None
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            # A table BOUNDARY. Without this the header's column index leaked into the next
            # table in the document, whose first cell was then read as a tracked cell. It was
            # invisible only because that table has fewer columns than this one's index — moving
            # the tracked column to the front exposed it.
            index = None
            continue
        cells = _split_row(line)
        if set("".join(cells)) <= set("-: "):
            continue  # the separator row
        headers = [c.strip().casefold() for c in cells]
        if _TRACKED_HEADER in headers:
            index = headers.index(_TRACKED_HEADER)
            continue
        if index is None or index >= len(cells):
            continue
        # The path is the row's first BACKTICKED cell, not its first cell: nothing about this
        # check should depend on where in the row the path sits, and reordering the table's
        # columns is an ordinary edit. A row with no backticked cell is prose and carries none.
        path = next((c.strip("`").strip() for c in cells
                     if c.startswith("`") and c.endswith("`") and len(c) > 2), None)
        if path is None:
            continue
        rows.append((path, cells[index].strip().casefold()))
    return rows


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _require_git_repository(case: unittest.TestCase) -> None:
    """Skip rather than fail where the question cannot be asked.

    A `git archive` snapshot has no repository and `git ls-files` answers 128 there; unpacked
    inside another repository, the OUTER one answers. The first version reported both as "the
    table says X is tracked; git does not track it" — an assertion of something git never said.

    All three conditions — no `git`, no work tree, a work tree whose root is somewhere else —
    are the one already-declared capability `not a git checkout`, spelled exactly so that
    `test_skip_reasons_are_declared` keeps granting it without the ledger growing an entry.
    """
    if shutil.which("git") is None:
        case.skipTest("not a git checkout")
    probe = _git(["rev-parse", "--is-inside-work-tree"], REPO_ROOT)
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        case.skipTest("not a git checkout")
    toplevel = _git(["rev-parse", "--show-toplevel"], REPO_ROOT)
    if toplevel.returncode != 0 or Path(toplevel.stdout.strip()).resolve() != REPO_ROOT:
        case.skipTest("not a git checkout")


class DevelopmentDocCitationTests(unittest.TestCase):
    """Every section `docs/DEVELOPMENT.md` cites exists in the document it names."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _strip_fences(DEVELOPMENT_DOC.read_text(encoding="utf-8"))

    def test_no_citation_uses_the_unquoted_form(self) -> None:
        """The declared form is enforced, so an unreadable citation fails loudly.

        Without this, narrowing the reader to the quoted form would be a silent weakening: a
        writer using the unquoted spelling would get no check at all rather than an error.
        """
        offenders = [line.strip() for line in self.text.splitlines()
                     if _UNQUOTED_CITATION_RE.search(line)]
        self.assertEqual(
            offenders, [],
            "docs/DEVELOPMENT.md writes a section citation without quoting the section name. "
            "The quotes are what let this check tell the name from the sentence around it; "
            "write the section name in double quotes after the section mark.")

    def test_the_document_still_cites(self) -> None:
        """A guard on this file rather than on the document.

        If the citations were rewritten as restatements, or the spelling moved, the checks below
        would pass by finding nothing. The floor sits well under the current count so that
        adding or removing one citation stays ordinary work.
        """
        self.assertGreaterEqual(
            len(_CITATION_RE.findall(self.text)), 4,
            "docs/DEVELOPMENT.md carries fewer section citations than this file was written to "
            "check. Either the document stopped citing and started restating — a design change "
            "to discuss, not a test to relax — or _CITATION_RE no longer reads it.")

    def test_every_cited_section_resolves(self) -> None:
        for match in _CITATION_RE.finditer(self.text):
            cited_path = match.group("path")
            section = match.group("section")
            with self.subTest(citation=f"{cited_path} {section}"):
                target = REPO_ROOT / cited_path
                self.assertTrue(target.is_file(), f"{cited_path} does not exist")
                names: set[str] = set()
                for heading in _headings(target):
                    names |= _heading_names(heading)
                self.assertIn(
                    section.strip().casefold(), names,
                    f"{cited_path} has no heading named {section!r}. A heading answers to its "
                    f"full text, to its number, and to its title — all exactly, so a renumbering "
                    f"breaks the citation instead of hiding behind it. Available: {sorted(names)}")

    def test_every_document_named_in_a_table_row_exists(self) -> None:
        """Inside a TABLE ROW, a backticked `*.md` path is an assertion that the file exists.

        The tables are the map; the prose around them is not. Scoping to rows is what lets the
        document name a not-yet-written note in a sentence about where such a note goes, without
        this check calling that a broken pointer.
        """
        named = sorted({
            path
            for row in _table_rows(self.text)
            for path in re.findall(r"`([A-Za-z0-9_./-]+\.md)`", row)
        })
        self.assertTrue(named, "no table row of docs/DEVELOPMENT.md names a document")
        for path in named:
            with self.subTest(path=path):
                self.assertTrue(
                    (REPO_ROOT / path).is_file(),
                    f"a table row of docs/DEVELOPMENT.md names `{path}`, which is not in the "
                    "tree. A path that is an example rather than a destination belongs in the "
                    "prose, not in the map.")


class ConfigurationLayerTableTests(unittest.TestCase):
    """The layer table's paths exist, and its `tracked` column agrees with git.

    This is the branch's central claim in table form — every file that decides what a workflow
    leaf loads from this repository is committed — so it is the one row set worth asking git
    about rather than reading.
    """

    UNRECOGNIZED = "unrecognized"

    @classmethod
    def setUpClass(cls) -> None:
        cls.rows = _layer_rows(_strip_fences(DEVELOPMENT_DOC.read_text(encoding="utf-8")))

    @classmethod
    def _tracked_verdict(cls, cell: str) -> object:
        """`True` / `False` for tracked-ness, `None` for out of tree, else UNRECOGNIZED.

        Prefix rather than equality so that a qualified cell stays a CHECKED row. The first
        version required an exact cell and dropped a qualified row silently, which disabled the
        whole claim for that row while leaving the suite green.
        """
        if cell.startswith("out of tree"):
            return None
        if cell.startswith("yes"):
            return True
        if cell.startswith("no"):
            return False
        return cls.UNRECOGNIZED

    def test_the_table_was_found(self) -> None:
        self.assertGreaterEqual(
            len(self.rows), 6,
            "the configuration-layer table's rows were not recognized. Either the table lost "
            f"its {_TRACKED_HEADER!r} header column, or its shape moved — either way the checks "
            "below would pass on nothing.")

    def test_every_tracked_cell_is_recognized(self) -> None:
        """An unclassifiable cell is an error, not a skip.

        Its own test, because deciding this inside the loops below is exactly how a silently
        dropped row happened in the first version.
        """
        for path, cell in self.rows:
            with self.subTest(path=path):
                self.assertNotEqual(
                    self._tracked_verdict(cell), self.UNRECOGNIZED,
                    f"the tracked cell for {path} reads {cell!r}, which this check cannot "
                    "classify. Write yes, no, or out of tree, optionally with a qualifier "
                    "after it.")

    def test_every_in_tree_path_exists(self) -> None:
        """Rows whose tracked cell says out of tree are skipped, keyed on that cell.

        The table deliberately carries such rows — the operator's own configuration directories
        and the runtime-state root — because the point of the table is that those are the only
        things left in a home directory. Keying the skip on the CELL rather than on a leading
        `~` is what lets one be spelled with an environment variable instead.
        """
        checked = 0
        for path, cell in self.rows:
            verdict = self._tracked_verdict(cell)
            if verdict is not True:
                # Out of tree is not ours to assert, and an untracked file is per-operator and
                # may legitimately be absent on a checkout nobody has set up yet.
                continue
            checked += 1
            with self.subTest(path=path):
                self.assertTrue(
                    (REPO_ROOT / path).exists(),
                    f"the configuration-layer table names {path}, which is not in the tree")
        self.assertGreater(checked, 0, "no in-tree row was checked")

    def test_the_tracked_column_agrees_with_git(self) -> None:
        """`yes` means git tracks it; `no` means a COMMITTED `.gitignore` covers it.

        The `no` half reads `check-ignore -v` and requires the SOURCE to be a `.gitignore`.
        Disabling the global excludes file is not enough, and the first version did only that:
        `.git/info/exclude` is per-clone too, it is where this repository was carrying
        `.claude/worktrees/` before this branch, and a check that accepted it would have called
        the pre-branch state correct — the exact thing this row set exists to refuse.
        """
        _require_git_repository(self)
        checked = 0
        for path, cell in self.rows:
            verdict = self._tracked_verdict(cell)
            if verdict not in (True, False):
                continue
            checked += 1
            with self.subTest(path=path, tracked=cell):
                listed = _git(["ls-files", "--error-unmatch", "--", path], REPO_ROOT)
                if verdict is True:
                    self.assertEqual(
                        listed.returncode, 0,
                        f"the table says {path} is tracked; git does not track it")
                    continue
                self.assertNotEqual(
                    listed.returncode, 0,
                    f"the table says {path} is untracked; git tracks it")
                shown = _git(["-c", "core.excludesFile=/dev/null",
                              "check-ignore", "-v", "--", path], REPO_ROOT)
                self.assertEqual(
                    shown.returncode, 0,
                    f"the table says {path} is untracked, but no ignore rule covers it — a "
                    "fresh clone would offer it for commit")
                source = shown.stdout.split(":", 1)[0].strip()
                self.assertEqual(
                    Path(source).name, ".gitignore",
                    f"{path} is ignored by {source!r}, which is not a committed .gitignore. A "
                    "per-clone file such as .git/info/exclude covers it on THIS clone only, "
                    "which is the state this table claims is gone.")
        self.assertGreater(checked, 0, "no row's tracked column was checked")


class DocsIndexTests(unittest.TestCase):
    """Every document `docs/README.md` indexes exists.

    One direction only. The reverse — every `docs/*.md` appears in the index — was measured and
    is false for 9 documents that predate this check, and closing that is a decision about where
    each of them belongs rather than a test.
    """

    INDEX = REPO_ROOT / "docs" / "README.md"

    #: The two sections that ARE the index, matched with their subsections. Scoped by section
    #: rather than by file, because the rest of the document is prose that legitimately names
    #: artifacts which are not repository files, and a whole-file sweep refused one.
    INDEX_SECTIONS = ("Shortest reading order", "Role-based Structure")

    def _indexed_names(self) -> list[str]:
        """Names inside an index section, INCLUDING its subsections.

        The depth rule is the fix for a measured vacuity: the second section holds all of its
        entries under subheadings, and a version that reset on every heading scanned none of
        them — indexing a nonexistent document there passed.
        """
        depth: int | None = None
        names: set[str] = set()
        for line in _strip_fences(self.INDEX.read_text(encoding="utf-8")).splitlines():
            heading = _HEADING_RE.match(line)
            if heading:
                level = len(heading.group("hashes"))
                text = heading.group("text").strip()
                if any(text.startswith(s) for s in self.INDEX_SECTIONS):
                    depth = level
                elif depth is not None and level <= depth:
                    depth = None
                continue
            if depth is None:
                continue
            names.update(re.findall(r"`([A-Za-z0-9_./-]+\.md)`", line))
        return sorted(names)

    def test_both_index_sections_are_scanned(self) -> None:
        """Its own test, because one of the two was silently unscanned for a whole round."""
        headings = [line.strip() for line in
                    _strip_fences(self.INDEX.read_text(encoding="utf-8")).splitlines()
                    if _HEADING_RE.match(line)]
        for section in self.INDEX_SECTIONS:
            with self.subTest(section=section):
                self.assertTrue(
                    any(h.lstrip("# ").startswith(section) for h in headings),
                    f"docs/README.md has no {section!r} heading; the scan below reads nothing "
                    "from it")
        # An entry that sits under a SUBHEADING must be reachable — what the depth rule buys.
        self.assertIn(
            "DEVELOPMENT.md", self._indexed_names(),
            "the index scan does not reach the entries under the second section's subheadings")

    def test_every_indexed_document_resolves(self) -> None:
        names = self._indexed_names()
        self.assertGreaterEqual(
            len(names), 10, "the index sections were not read; a heading or spelling has moved")
        for name in names:
            with self.subTest(document=name):
                # Docs-relative only. A repository-relative fallback was measured to be needed by
                # zero entries while admitting `TODO.md` as though `docs/TODO.md` existed.
                self.assertTrue(
                    (self.INDEX.parent / name).is_file(),
                    f"docs/README.md indexes {name}, which is not under docs/")


class DevelopmentDocReachabilityTests(unittest.TestCase):
    """TODO:414's completion criterion, as a check rather than as a claim in a closed entry."""

    ENTRY_POINTS = ("AGENTS.md", "CLAUDE.md", "docs/README.md")

    def test_the_document_is_reachable_from_every_entry_point(self) -> None:
        for rel in self.ENTRY_POINTS:
            with self.subTest(entry=rel):
                text = (REPO_ROOT / rel).read_text(encoding="utf-8")
                # The full spelling rather than the bare stem: this proves a POINTER, where the
                # word `DEVELOPMENT` alone would be satisfied by ordinary prose. The index names
                # it docs-relative, so it is the one entry point spelt without the directory.
                wanted = "DEVELOPMENT.md" if rel == "docs/README.md" else "docs/DEVELOPMENT.md"
                self.assertIn(
                    wanted, text,
                    f"{rel} no longer points at the development document. TODO:414's completion "
                    "criterion is that it is reachable from the documents an agent always reads.")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
