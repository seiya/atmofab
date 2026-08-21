#!/usr/bin/env python3
"""`docs/DEVELOPMENT.md` is built out of citations, so its citations are what can rot.

The document deliberately restates nothing: its setup table and its scope section point at the
section that owns each procedure. That design moves the failure mode. A restatement goes stale
loudly — two documents disagree and a reader notices — while a citation to a section that was
renumbered or renamed goes stale SILENTLY, and reads as authoritative the whole time.

Scope, stated because a wider version of each check was measured and rejected:

- Only `docs/DEVELOPMENT.md` is scanned for citations. The same sweep over every document in the
  repository is a different change with a different cost; this file pins the document whose
  citations are its entire content.
- Completeness is NOT asserted anywhere here. Requiring `docs/README.md` to index every
  `docs/*.md` would fail on 9 documents that predate this file (measured 2026-08-21), and
  requiring `README.md`'s layout block to list every top-level entry would refuse an ordinary new
  directory until someone documented it. Both are pins on a RESULT rather than on a rule, which
  is the shape this repository has been bitten by. What is asserted is that every path and
  section the document NAMES resolves.
"""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEVELOPMENT_DOC = REPO_ROOT / "docs" / "DEVELOPMENT.md"

#: A citation: a backtick-quoted repository path, then `§`, then the section name. The name runs
#: to the end of a double-quoted span when one follows `§`, else to the first character that
#: cannot start a heading word. Both spellings are in the document ( §0-1 , §"Record placement" ).
_CITATION_RE = re.compile(
    r"`(?P<path>[A-Za-z0-9_./-]+\.md)`\s*§\s*(?:\"(?P<quoted>[^\"]+)\"|(?P<bare>[A-Za-z0-9][A-Za-z0-9 _-]*))"
)

#: A heading line, any depth.
_HEADING_RE = re.compile(r"^#{1,6}\s+(?P<text>.+?)\s*$")

#: A row of the configuration-layer table: the leading cell is a backtick-quoted path, and the
#: last cell is the tracked column. Rows whose leading cell is prose (`the operator's own …`)
#: carry no path and are skipped by the pattern requiring backticks.
_LAYER_ROW_RE = re.compile(
    r"^\|\s*`(?P<path>[^`]+)`\s*\|(?P<middle>[^|]*\|[^|]*)\|\s*(?P<tracked>[^|]+?)\s*\|\s*$"
)


def _headings(path: Path) -> list[str]:
    return [
        m.group("text")
        for m in (_HEADING_RE.match(line) for line in path.read_text(encoding="utf-8").splitlines())
        if m
    ]


def _heading_matches(cited: str, heading: str) -> bool:
    """Whether `heading` is the section `cited` names.

    Prefix rather than equality, deliberately: this repository numbers a runbook section `0-1.`
    and titles it `0-1. Required CLI tools and Python packages`, and a citation names the number
    alone. Requiring equality would force every citation to carry the full title, which is the
    restatement the document exists to avoid.
    """
    normalized = heading.strip().strip("`").casefold()
    wanted = cited.strip().casefold()
    return normalized.startswith(wanted) or normalized.startswith(wanted + ".")


class DevelopmentDocCitationTests(unittest.TestCase):
    """Every section `docs/DEVELOPMENT.md` cites exists in the document it names."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = DEVELOPMENT_DOC.read_text(encoding="utf-8")

    def test_the_document_exists_and_is_cited_as_a_citation_document(self) -> None:
        """A guard on this test file rather than on the document.

        If the citations were ever rewritten as restatements the checks below would pass
        vacuously by finding nothing, which is the failure mode a negative assertion has.
        """
        citations = list(_CITATION_RE.finditer(self.text))
        self.assertGreaterEqual(
            len(citations), 5,
            "docs/DEVELOPMENT.md carries fewer section citations than this file was written to "
            "check. Either the document stopped citing and started restating — which is a "
            "design change to discuss, not a test to relax — or the citation spelling moved and "
            "_CITATION_RE no longer reads it.")

    def test_every_cited_section_resolves(self) -> None:
        for match in _CITATION_RE.finditer(self.text):
            cited_path = match.group("path")
            section = match.group("quoted") or match.group("bare")
            with self.subTest(citation=f"{cited_path} §{section}"):
                target = REPO_ROOT / cited_path
                self.assertTrue(target.is_file(), f"{cited_path} does not exist")
                headings = _headings(target)
                self.assertTrue(
                    any(_heading_matches(section, h) for h in headings),
                    f"{cited_path} has no heading for §{section}. Headings: {headings}")

    def test_every_document_cited_without_a_section_exists(self) -> None:
        """In THIS document a backticked `*.md` path is an assertion that the file exists.

        That is a rule about the document rather than a pin on today's file list: it is a map of
        where records go, and a map naming a place that is not there is the defect. Documents
        elsewhere legitimately name a file that does not exist — `docs/BACKEND_BOUNDARY.md`
        names `docs/backends/notes.md` as a counter-example — which is why this sweep is scoped
        to one document. If this document ever needs to name a hypothetical path, write it
        without backticks; the rule is the backticks, not the mention.
        """
        for path in sorted(set(re.findall(r"`((?:docs/)?[A-Za-z0-9_./-]+\.md)`", self.text))):
            with self.subTest(path=path):
                self.assertTrue(
                    (REPO_ROOT / path).is_file(),
                    f"docs/DEVELOPMENT.md names `{path}`, which is not in the tree. A "
                    "deliberately hypothetical path goes without backticks.")


class ConfigurationLayerTableTests(unittest.TestCase):
    """The layer table's paths exist, and its `tracked` column agrees with git.

    This is the branch's central claim in table form — every file that decides what a workflow
    leaf loads is committed — so it is the one row set worth asking git about rather than
    reading.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.rows = [
            (m.group("path").strip(), m.group("tracked").strip().casefold())
            for m in (
                _LAYER_ROW_RE.match(line)
                for line in DEVELOPMENT_DOC.read_text(encoding="utf-8").splitlines()
            )
            if m
        ]

    def test_the_table_was_found(self) -> None:
        self.assertGreaterEqual(
            len(self.rows), 6,
            "the configuration-layer table's rows were not recognized; _LAYER_ROW_RE has drifted "
            "from the table's shape and the checks below would pass on nothing")

    def test_every_in_tree_path_exists(self) -> None:
        """Rows naming a path OUTSIDE the checkout are skipped, and named as skipped.

        The table deliberately carries two of them — the operator's own configuration
        directories and the runtime-state root — because the point of the table is that those
        are the only things left in a home directory. A check that demanded they exist would be
        asserting a property of the machine running the suite.
        """
        checked = 0
        for path, tracked in self.rows:
            if path.startswith("~"):
                continue
            if tracked == "no":
                # An untracked file is per-operator and may legitimately not exist on this
                # machine; `llm.yaml` on a checkout that has not been set up is the case.
                continue
            checked += 1
            with self.subTest(path=path):
                self.assertTrue(
                    (REPO_ROOT / path).exists(),
                    f"the configuration-layer table names {path}, which is not in the tree")
        self.assertGreater(checked, 0, "no in-tree row was checked")

    def test_the_tracked_column_agrees_with_git(self) -> None:
        """`yes` means git tracks it; `no` means the COMMITTED ignore rules cover it.

        The `no` half runs with the operator's global excludes file disabled
        (`core.excludesFile=/dev/null`). That is the whole point: two of these entries were
        ignored only per-machine before this branch, so a check honouring the operator's global
        file would have called the pre-branch state correct.
        """
        checked = 0
        for path, tracked in self.rows:
            if path.startswith("~") or tracked not in {"yes", "no"}:
                continue
            checked += 1
            with self.subTest(path=path, tracked=tracked):
                listed = subprocess.run(
                    ["git", "ls-files", "--error-unmatch", "--", path],
                    cwd=REPO_ROOT, capture_output=True, text=True)
                if tracked == "yes":
                    self.assertEqual(
                        listed.returncode, 0,
                        f"the table says {path} is tracked; git does not track it")
                    continue
                self.assertNotEqual(
                    listed.returncode, 0,
                    f"the table says {path} is untracked; git tracks it")
                ignored = subprocess.run(
                    ["git", "-c", "core.excludesFile=/dev/null", "check-ignore", "-q", "--", path],
                    cwd=REPO_ROOT).returncode
                self.assertEqual(
                    ignored, 0,
                    f"the table says {path} is untracked, but the COMMITTED ignore rules do not "
                    "cover it — a fresh clone would offer it for commit")
        self.assertGreater(checked, 0, "no row's tracked column was checked")


class DevelopmentDocReachabilityTests(unittest.TestCase):
    """TODO:414's completion criterion, as a check rather than as a claim in a closed entry."""

    ENTRY_POINTS = ("AGENTS.md", "CLAUDE.md", "docs/README.md")

    def test_the_document_is_reachable_from_every_entry_point(self) -> None:
        for rel in self.ENTRY_POINTS:
            with self.subTest(entry=rel):
                text = (REPO_ROOT / rel).read_text(encoding="utf-8")
                self.assertIn(
                    "DEVELOPMENT.md", text,
                    f"{rel} no longer points at the development document. TODO:414's completion "
                    "criterion is that it is reachable from the documents an agent always reads.")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
