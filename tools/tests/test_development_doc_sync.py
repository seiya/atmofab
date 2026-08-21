#!/usr/bin/env python3
"""The structural claims of the two documents that are MAPS.

`docs/DEVELOPMENT.md` says where each kind of development record belongs and which configuration
layer governs which session; `docs/README.md` indexes the document set. Neither carries content
of its own, so what can rot in them is where they point.

## Scope, and the half that was DELIBERATELY GIVEN UP

Two earlier versions of this file also resolved `docs/DEVELOPMENT.md`'s inline section citations
against the headings of the documents they name. Both were broken the same way in successive
review rounds — thirteen over-refusals of correct writing between them, every one a consequence
of deciding, from raw markdown, what is a citation and what is a heading: an indented code
block, an HTML comment, a fence nested inside a longer fence, a heading that both opens and
closes with a code span, a section numbered `5.1` where the reader modelled `5-1`, the section
mark used in a sentence about section marks.

Breaking the same way twice is the sign that the question is at the wrong level, so the third
version does not ask it. **Citation resolution is out of scope here, and the residue is named:
a cited section that is renumbered or renamed goes stale silently, and only review catches it.**
Closing it needs a real markdown parser; this repository has none, and adding one to keep a
documentation map honest is out of proportion to what the map is worth. `TODO.md` carries the
residue and what would close it.

What is left needs no prose parsing:

- **A table is located by its HEADER ROW** and read by column name. The tables are the map.
- **Inside a table row, a backticked `*.md` path asserts that the file exists.** The prose around
  the tables is free to name a hypothetical file, a spec artifact, or a path not yet written.
- **`docs/README.md`'s index entries resolve**, in one direction only.
- **The development document is reachable** from the documents an agent always reads.

Completeness is NOT asserted anywhere. Requiring `docs/README.md` to index every `docs/*.md`
would fail on 8 documents that predate this file (measured 2026-08-21, counting
`docs/*.md` other than the index itself), and requiring
`README.md`'s layout block to list every top-level entry would refuse an ordinary new directory
until someone documented it. Both pin a RESULT rather than a rule.

The readers below are unit-tested against synthetic input by `TableReaderTests`. A reader
witnessed only by today's documents is one whose decisions are mostly unobserved: of the
previous version's 40 decisions, a review measured 4 as killed by the tree as it stood.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEVELOPMENT_DOC = REPO_ROOT / "docs" / "DEVELOPMENT.md"

#: A `*.md` path as this repository backticks one.
_MD_PATH_RE = re.compile(r"`([A-Za-z0-9_./-]+\.md)`")

#: A markdown heading of any depth.
_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<text>.+?)\s*$")

#: The header cells that identify the configuration-layer table. BOTH are required: keying on
#: `tracked` alone absorbed any other table carrying that column, and then ordered its author to
#: write `yes` / `no` / `out of tree` in a table about something else.
_LAYER_TABLE_HEADERS = ("file", "tracked")

#: The verdict for a `tracked` cell this reader cannot classify. A value rather than a skip, so
#: the caller can make it an error.
UNRECOGNIZED = "unrecognized"


def _split_row(line: str) -> list[str]:
    """The cells of one markdown table row, outer pipes dropped.

    An escaped pipe inside a cell is not a separator; it is restored after the split so a cell
    may legitimately contain one.
    """
    body = line.strip().strip("|")
    return [cell.replace("\x00", "|").strip()
            for cell in body.replace("\\|", "\x00").split("|")]


def _is_separator_row(cells: list[str]) -> bool:
    """The `|---|:---:|` row, in any alignment spelling."""
    joined = "".join(cells)
    return bool(joined) and set(joined) <= set("-: ")


def read_table(text: str, headers: tuple[str, ...]) -> list[dict[str, str]]:
    """Rows of every table whose header row carries ALL of `headers`, as name-to-cell maps.

    Columns are read by NAME, so a table may gain, drop or reorder them without this reader
    taking the wrong cell or refusing the edit. The header binding is dropped at a table
    BOUNDARY — any line that is not a table row — because it once leaked into the next table in
    the document, invisible only while that table had fewer columns than the leaked index.
    """
    rows: list[dict[str, str]] = []
    names: list[str] | None = None
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            names = None
            continue
        cells = _split_row(line)
        if _is_separator_row(cells):
            continue
        lowered = [c.casefold() for c in cells]
        if all(h in lowered for h in headers):
            names = lowered
            continue
        if names is None:
            continue
        rows.append({name: cells[i].strip()
                     for i, name in enumerate(names) if i < len(cells)})
    return rows


def indexed_names(text: str, sections: tuple[str, ...]) -> list[str]:
    """Every backticked `*.md` inside one of `sections`, INCLUDING their subsections.

    The depth rule is what makes the second index section readable: it holds all of its entries
    under subheadings, and a version that dropped the section on every heading scanned none of
    them — indexing a nonexistent document there passed. No entry in today's index is
    subsection-ONLY, so that rule has no witness in the corpus and is witnessed synthetically by
    `IndexReaderTests` instead.
    """
    depth: int | None = None
    names: set[str] = set()
    for line in text.splitlines():
        heading = _HEADING_RE.match(line)
        if heading:
            level, title = len(heading.group("hashes")), heading.group("text").strip()
            if any(title.startswith(s) for s in sections):
                depth = level
            elif depth is not None and level <= depth:
                depth = None
            continue
        if depth is None:
            continue
        names.update(_MD_PATH_RE.findall(line))
    return sorted(names)


def tracked_verdict(cell: str) -> bool | None | str:
    """`True` / `False` for tracked-ness, `None` for out of tree, else `UNRECOGNIZED`.

    Prefix rather than equality so a qualified cell stays a CHECKED row: an earlier version
    required an exact cell and dropped a qualified row silently, disabling the claim for that
    row while the suite stayed green.
    """
    lowered = cell.strip().casefold()
    if lowered.startswith("out of tree"):
        return None
    if lowered.startswith("yes"):
        return True
    if lowered.startswith("no"):
        return False
    return UNRECOGNIZED


def _git(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True)


def _require_git_repository(case: unittest.TestCase) -> None:
    """Skip rather than fail where the question cannot be asked.

    A `git archive` snapshot has no repository and `git ls-files` answers 128 there; unpacked
    inside another repository, the OUTER one answers. An earlier version reported both as "the
    table says X is tracked; git does not track it" — an assertion of something git never said.
    """
    if shutil.which("git") is None:
        case.skipTest("no git work tree to ask about tracking")
    probe = _git(["rev-parse", "--is-inside-work-tree"])
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        case.skipTest("no git work tree to ask about tracking")
    toplevel = _git(["rev-parse", "--show-toplevel"])
    if toplevel.returncode != 0 or Path(toplevel.stdout.strip()).resolve() != REPO_ROOT:
        case.skipTest("no git work tree to ask about tracking")


class TableReaderTests(unittest.TestCase):
    """The readers, against synthetic input — one case per decision.

    Their only other witness is today's documents, which exercise a handful of these decisions
    and leave the rest unobserved.
    """

    def test_cells_split_on_unescaped_pipes_only(self) -> None:
        self.assertEqual(_split_row("| a | b | c |"), ["a", "b", "c"])
        self.assertEqual(_split_row("|a|b\\|c|"), ["a", "b|c"])

    def test_separator_rows_are_recognized_in_every_alignment_spelling(self) -> None:
        for row in ("|---|---|", "|:---|---:|", "| :---: | --- |"):
            with self.subTest(row=row):
                self.assertTrue(_is_separator_row(_split_row(row)))
        self.assertFalse(_is_separator_row(_split_row("| a | b |")))

    def test_a_table_is_found_by_its_header_and_read_by_column_name(self) -> None:
        text = "| file | layer | tracked |\n|---|---|---|\n| `a.json` | LEAF | yes |\n"
        self.assertEqual(read_table(text, _LAYER_TABLE_HEADERS),
                         [{"file": "`a.json`", "layer": "LEAF", "tracked": "yes"}])

    def test_reordered_and_added_columns_do_not_move_the_answer(self) -> None:
        text = "| tracked | note | file |\n|---|---|---|\n| yes | x | `a.json` |\n"
        rows = read_table(text, _LAYER_TABLE_HEADERS)
        self.assertEqual([(r["file"], r["tracked"]) for r in rows], [("`a.json`", "yes")])

    def test_a_table_missing_one_required_header_is_not_read(self) -> None:
        """Keying on `tracked` alone absorbed unrelated tables, so both names are required."""
        text = "| artifact | tracked |\n|---|---|\n| `spec/x.yaml` | not by this table |\n"
        self.assertEqual(read_table(text, _LAYER_TABLE_HEADERS), [])

    def test_the_header_binding_stops_at_a_table_boundary(self) -> None:
        text = ("| file | tracked |\n|---|---|\n| `a.json` | yes |\n"
                "\nsome prose\n\n| record | home |\n|---|---|\n| a thing | `docs/` |\n")
        self.assertEqual([r["file"] for r in read_table(text, _LAYER_TABLE_HEADERS)],
                         ["`a.json`"])

    def test_a_row_shorter_than_its_header_contributes_only_the_cells_it_has(self) -> None:
        """A ragged row is read, not crashed on. The bound is the only thing stopping that."""
        text = "| file | layer | tracked |\n|---|---|---|\n| `a.json` | LEAF |\n"
        self.assertEqual(read_table(text, _LAYER_TABLE_HEADERS),
                         [{"file": "`a.json`", "layer": "LEAF"}])

    def test_tracked_verdicts_including_qualified_and_unrecognized_cells(self) -> None:
        cases = {
            "yes": True, "Yes (since #63)": True,
            "no": False, "no (per-operator)": False,
            "out of tree": None, "out of tree, by design": None,
            "committed": UNRECOGNIZED, "": UNRECOGNIZED,
        }
        for cell, expected in cases.items():
            with self.subTest(cell=cell):
                self.assertEqual(tracked_verdict(cell), expected)


class IndexReaderTests(unittest.TestCase):
    """`indexed_names`, against synthetic input.

    The depth rule cannot be witnessed by today's index — no entry there sits only under a
    subheading — so it is witnessed here. Without this, dropping the rule leaves the corpus
    green, which is exactly how it went unnoticed for a round.
    """

    SECTIONS = ("Wanted",)

    def test_an_entry_under_a_subheading_is_reached(self) -> None:
        text = "## Wanted\n### Deeper\n- `a.md`\n"
        self.assertEqual(indexed_names(text, self.SECTIONS), ["a.md"])

    def test_a_sibling_section_ends_the_scan(self) -> None:
        text = "## Wanted\n- `a.md`\n## Other\n- `b.md`\n"
        self.assertEqual(indexed_names(text, self.SECTIONS), ["a.md"])

    def test_a_shallower_heading_ends_the_scan(self) -> None:
        text = "## Wanted\n### Deeper\n- `a.md`\n# Top\n- `b.md`\n"
        self.assertEqual(indexed_names(text, self.SECTIONS), ["a.md"])

    def test_content_before_any_wanted_section_is_ignored(self) -> None:
        text = "## Other\n- `b.md`\n## Wanted\n- `a.md`\n"
        self.assertEqual(indexed_names(text, self.SECTIONS), ["a.md"])

    def test_a_section_that_is_absent_yields_nothing(self) -> None:
        self.assertEqual(indexed_names("## Other\n- `b.md`\n", self.SECTIONS), [])


class DevelopmentDocTableTests(unittest.TestCase):
    """Every document a table row of `docs/DEVELOPMENT.md` names exists."""

    def test_every_document_named_in_a_table_row_exists(self) -> None:
        rows = [line for line in DEVELOPMENT_DOC.read_text(encoding="utf-8").splitlines()
                if line.lstrip().startswith("|")]
        named = sorted({p for row in rows for p in _MD_PATH_RE.findall(row)})
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

    @classmethod
    def setUpClass(cls) -> None:
        rows = read_table(DEVELOPMENT_DOC.read_text(encoding="utf-8"), _LAYER_TABLE_HEADERS)
        cls.entries = [
            (row["file"].strip("`").strip(), row.get("tracked", ""))
            for row in rows
            if len(row.get("file", "")) > 2
            and row["file"].startswith("`") and row["file"].endswith("`")
        ]

    def test_the_table_was_found(self) -> None:
        self.assertGreaterEqual(
            len(self.entries), 6,
            "the configuration-layer table was not recognized. Either it lost one of the "
            f"{_LAYER_TABLE_HEADERS} header columns or its shape moved — either way the checks "
            "below would pass on nothing.")

    def test_every_tracked_cell_is_recognized(self) -> None:
        """An unclassifiable cell is an error, not a skip.

        Its own test, because deciding this inside the loops below is how a silently dropped row
        happened once already.
        """
        for path, cell in self.entries:
            with self.subTest(path=path):
                self.assertNotEqual(
                    tracked_verdict(cell), UNRECOGNIZED,
                    f"the tracked cell for {path} reads {cell!r}, which this check cannot "
                    "classify. Write yes, no, or out of tree, optionally with a qualifier.")

    def test_every_in_tree_path_exists(self) -> None:
        """Rows reading out of tree are skipped, keyed on that cell rather than on a leading `~`.

        The table deliberately carries such rows — the operator's own configuration directories
        and the runtime-state root — because the point of the table is that those are the only
        things left in a home directory.
        """
        checked = 0
        for path, cell in self.entries:
            if tracked_verdict(cell) is not True:
                continue
            checked += 1
            with self.subTest(path=path):
                self.assertTrue(
                    (REPO_ROOT / path).exists(),
                    f"the configuration-layer table names {path}, which is not in the tree")
        self.assertGreater(checked, 0, "no in-tree row was checked")

    def test_the_tracked_column_agrees_with_git(self) -> None:
        """`yes` means git tracks it; `no` means a COMMITTED `.gitignore` covers it.

        The `no` half reads `check-ignore -v` and requires the deciding source to be a
        `.gitignore` **that git tracks**. Two weaker versions were measured to accept the state
        this row set exists to refuse: disabling the global excludes file leaves
        `.git/info/exclude`, and checking the basename alone leaves an UNTRACKED
        `.claude/.gitignore` — each per-clone in exactly the way the other is.
        """
        _require_git_repository(self)
        checked = 0
        for path, cell in self.entries:
            verdict = tracked_verdict(cell)
            if verdict not in (True, False):
                continue
            checked += 1
            with self.subTest(path=path, tracked=cell):
                listed = _git(["ls-files", "--error-unmatch", "--", path])
                if verdict is True:
                    self.assertEqual(
                        listed.returncode, 0,
                        f"the table says {path} is tracked; git does not track it")
                    continue
                self.assertNotEqual(
                    listed.returncode, 0,
                    f"the table says {path} is untracked; git tracks it")
                shown = _git(["check-ignore", "-v", "--", path])
                self.assertEqual(
                    shown.returncode, 0,
                    f"the table says {path} is untracked, but no ignore rule covers it — a "
                    "fresh clone would offer it for commit")
                source = shown.stdout.split(":", 1)[0].strip()
                self.assertEqual(
                    Path(source).name, ".gitignore",
                    f"{path} is ignored by {source!r}, which is not a .gitignore")
                self.assertEqual(
                    _git(["ls-files", "--error-unmatch", "--", source]).returncode, 0,
                    f"{path} is ignored by {source!r}, which git does not track. A per-clone or "
                    "per-machine file covers it on THIS checkout only, which is the state this "
                    "table claims is gone.")
        self.assertGreater(checked, 0, "no row's tracked column was checked")


class GitGuardTests(unittest.TestCase):
    """The guard does not skip in this repository.

    Without this, making `_require_git_repository` skip unconditionally lets four separate
    defect corpora through — a table lie, a per-clone ignore file, no ignore rule at all, and a
    global excludes file — with nothing anywhere asserting that the git half ever ran.
    """

    def test_the_guard_does_not_skip_where_a_repository_is_present(self) -> None:
        """Whether the repository is present is decided WITHOUT the guard being tested.

        Asking the guard would make this circular — a guard that always skips would make its own
        witness skip too. `.git` is the independent question: present as a directory in a normal
        checkout and as a file in a `git worktree`, absent in a `git archive` snapshot, which is
        the extraction this repository's own skills prescribe for mutation runs.
        """
        if not (REPO_ROOT / ".git").exists():
            self.skipTest("no git work tree to ask about tracking")
        try:
            _require_git_repository(self)
        except unittest.SkipTest as exc:  # pragma: no cover - the failure path is the point
            self.fail(f"the git guard skipped in a checkout that has a .git: {exc}. Every "
                      "assertion in ConfigurationLayerTableTests."
                      "test_the_tracked_column_agrees_with_git is then unreachable.")


class DocsIndexTests(unittest.TestCase):
    """Every document `docs/README.md` indexes exists.

    One direction only. The reverse — every `docs/*.md` appears in the index — was measured and
    is false for 8 documents that predate this check, and closing that is a decision about where
    each of them belongs rather than a test.
    """

    INDEX = REPO_ROOT / "docs" / "README.md"

    #: The two sections that ARE the index, matched WITH their subsections. Scoped by section
    #: rather than by file, because the rest of the document is prose that legitimately names
    #: artifacts which are not repository files, and a whole-file sweep refused one.
    INDEX_SECTIONS = ("Shortest reading order", "Role-based Structure")

    def _indexed_names(self, sections: tuple[str, ...] | None = None) -> list[str]:
        return indexed_names(self.INDEX.read_text(encoding="utf-8"),
                             sections if sections is not None else self.INDEX_SECTIONS)

    def test_the_scan_reads_both_index_sections(self) -> None:
        """Both headings exist, so neither section is silently unscanned.

        Weaker than it looks and deliberately so: today no entry is subsection-only, so the
        corpus cannot witness the depth rule itself. `IndexReaderTests` does that on synthetic
        input.
        """
        headings = [m.group("text").strip()
                    for m in (_HEADING_RE.match(line)
                              for line in self.INDEX.read_text(encoding="utf-8").splitlines())
                    if m]
        for section in self.INDEX_SECTIONS:
            with self.subTest(section=section):
                self.assertTrue(
                    any(h.startswith(section) for h in headings),
                    f"docs/README.md has no {section!r} heading, so the scan reads nothing "
                    "from it")

    def test_every_indexed_document_resolves(self) -> None:
        names = self._indexed_names()
        self.assertGreaterEqual(
            len(names), 10, "the index sections were not read; a heading or spelling has moved")
        for name in names:
            with self.subTest(document=name):
                self.assertTrue(
                    (self.INDEX.parent / name).is_file() or (REPO_ROOT / name).is_file(),
                    f"docs/README.md indexes {name}, which is neither under docs/ nor at the "
                    "repository root")


class DevelopmentDocReachabilityTests(unittest.TestCase):
    """TODO:414's completion criterion, as a check rather than as a claim in a closed entry."""

    ENTRY_POINTS = ("AGENTS.md", "CLAUDE.md", "docs/README.md")

    def test_the_entry_point_list_is_the_one_the_criterion_names(self) -> None:
        """Pinned as a SET: dropping an entry point from the tuple below is otherwise free."""
        self.assertEqual(
            set(self.ENTRY_POINTS), {"AGENTS.md", "CLAUDE.md", "docs/README.md"},
            "TODO:414's criterion names the documents an agent always reads; narrowing this "
            "tuple narrows the criterion without saying so")

    def test_the_document_is_reachable_from_every_entry_point(self) -> None:
        for rel in self.ENTRY_POINTS:
            with self.subTest(entry=rel):
                text = (REPO_ROOT / rel).read_text(encoding="utf-8")
                # The full spelling rather than the bare stem: this proves a POINTER, where the
                # word DEVELOPMENT alone would be satisfied by ordinary prose. The index names
                # it docs-relative, so it is the one entry point spelt without the directory.
                wanted = "DEVELOPMENT.md" if rel == "docs/README.md" else "docs/DEVELOPMENT.md"
                self.assertIn(
                    wanted, text,
                    f"{rel} no longer points at the development document. TODO:414's completion "
                    "criterion is that it is reachable from the documents an agent always reads.")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
