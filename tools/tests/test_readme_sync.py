"""Anti-drift guard for the three mechanically-derivable tables in `README.md`.

The README restates facts that live in code and in the registry: which substeps each phase
runs and which of them are `LLM` leaves, which tools the MCP build/runtime server serves, and
which `spec` this tree holds. Prose copies of machine-owned facts are what this repository
keeps paying for (`TODO.md`'s documentation-drift item, whose own completion criterion asks for
a doc-sync test rather than another proofread), so each table here is compared against the
source that OWNS it, never against a second literal:

* the substep sequences against `workflow_conductor.SUBSTEPS`,
* the `LLM`-leaf column against `llm_config.LLM_LEAF_SUBSTEPS`,
* the MCP tool list against `build_runtime_server.TOOLS`,
* the `spec` table against `spec/registry/spec_catalog.yaml` AND against the
  `controlled_spec.md` files on disk (the registry and the tree can disagree; the README is
  wrong if it matches only one).

Only these three tables are pinned. The prose is deliberately not, and the CLI option table is
not either: `docs/CLI_REFERENCE.md` makes `tools/run_workflow.py --help` canonical for that
surface, and the README says so rather than claiming to be a second source.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
README = REPO_ROOT / "README.md"

sys.path.insert(0, str(REPO_ROOT / "tools"))
sys.path.insert(0, str(REPO_ROOT / "mcp_servers"))

import llm_config  # noqa: E402
import workflow_conductor  # noqa: E402

import build_runtime_server  # noqa: E402


def _read_readme() -> str:
    return README.read_text(encoding="utf-8")


def _table_rows(text: str, heading: str) -> list[list[str]]:
    """The data rows of the first markdown table under `heading`, cells stripped.

    Separator (`|---|`) and header rows are dropped; a table ends at the first line that is
    not a table row, so a section holding two tables yields only the first.
    """
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == heading)
    except StopIteration:  # pragma: no cover - guarded by test_headings_exist
        raise AssertionError(f"README has no heading {heading!r}")
    rows: list[list[str]] = []
    in_table = False
    for line in lines[start + 1:]:
        stripped = line.strip()
        if not stripped.startswith("|"):
            if in_table:
                break
            continue
        in_table = True
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if all(set(cell) <= {"-", ":"} and cell for cell in cells):
            continue
        rows.append(cells)
    assert rows, f"no table found under {heading!r}"
    return rows[1:]  # drop the header row


def _codes(cell: str) -> list[str]:
    """The backtick-quoted tokens of a cell, in order."""
    return re.findall(r"`([^`]+)`", cell)


class ReadmeSubstepTableTest(unittest.TestCase):
    """The substep table is `workflow_conductor.SUBSTEPS` plus `llm_config.LLM_LEAF_SUBSTEPS`."""

    def _rows(self) -> dict[str, list[str]]:
        # The Workflow section holds the phase table first and the substep table second, so this
        # one is located by its own header rather than by the heading. A header that moves or
        # disappears raises here rather than yielding an empty (vacuously passing) mapping.
        text = _read_readme()
        start = text.index("| phase | substeps |")
        block = text[start:]
        out: dict[str, list[str]] = {}
        for line in block[:block.index("\n\n")].splitlines()[2:]:
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            out[cells[0]] = cells[1:]
        assert out, "the substep table has no data rows"
        return out

    def test_substep_sequences_match_the_conductor(self) -> None:
        readme = self._rows()
        self.assertEqual(
            {phase.lower() for phase in readme},
            set(workflow_conductor.SUBSTEPS),
            "README's substep table names a different phase set than workflow_conductor.SUBSTEPS",
        )
        for phase, cells in readme.items():
            owned = workflow_conductor.SUBSTEPS[phase.lower()]
            declared = _codes(cells[0])
            if owned == (None,):
                self.assertEqual(
                    declared, [],
                    f"{phase} has no substep in the conductor; the README names {declared}",
                )
                self.assertIn("none", cells[0].lower())
                continue
            self.assertEqual(
                declared, list(owned),
                f"README's {phase} substeps disagree with workflow_conductor.SUBSTEPS",
            )

    def test_llm_leaf_column_matches_llm_config(self) -> None:
        readme = self._rows()
        declared: set[tuple[str, str]] = set()
        for phase, cells in readme.items():
            for substep in _codes(cells[1]):
                declared.add((phase.lower(), substep))
        self.assertEqual(
            declared, set(llm_config.LLM_LEAF_SUBSTEPS),
            "README's `LLM` substep column disagrees with llm_config.LLM_LEAF_SUBSTEPS",
        )

    def test_build_is_recorded_as_launching_no_leaf(self) -> None:
        """The one claim a reader is most likely to act on: Build spends no tokens."""
        self.assertEqual(workflow_conductor.SUBSTEPS["build"], (None,))
        self.assertEqual(
            [pair for pair in llm_config.LLM_LEAF_SUBSTEPS if pair[0] == "build"], [],
        )
        self.assertIn("none", self._rows()["Build"][1].lower())


class ReadmeMcpToolTableTest(unittest.TestCase):
    def test_tool_list_matches_the_served_registry(self) -> None:
        rows = _table_rows(_read_readme(), "## MCP tools")
        declared = [_codes(row[0])[0] for row in rows]
        self.assertEqual(
            sorted(declared), sorted(build_runtime_server.TOOLS),
            "README's MCP tool table disagrees with build_runtime_server.TOOLS",
        )


class ReadmeSpecTableTest(unittest.TestCase):
    def _declared(self) -> set[tuple[str, str]]:
        rows = _table_rows(_read_readme(), "## In-tree specifications")
        declared: set[tuple[str, str]] = set()
        for row in rows:
            kind = _codes(row[0])[0]
            for spec_id in _codes(row[1]):
                declared.add((kind, spec_id))
        return declared

    def test_matches_the_registry(self) -> None:
        catalog = yaml.safe_load(
            (REPO_ROOT / "spec" / "registry" / "spec_catalog.yaml").read_text(encoding="utf-8")
        )
        registered = {
            (str(entry["spec_kind"]), str(entry["spec_id"])) for entry in catalog["specs"]
        }
        self.assertEqual(
            self._declared(), registered,
            "README's spec table disagrees with spec/registry/spec_catalog.yaml",
        )

    def test_matches_the_tree(self) -> None:
        """Walked, not globbed through the shell: `grep`/`rg` here honours `.gitignore`."""
        on_disk: set[tuple[str, str]] = set()
        for path in (REPO_ROOT / "spec").rglob("controlled_spec.md"):
            rel = path.relative_to(REPO_ROOT / "spec").parts
            on_disk.add((rel[0], rel[-2]))
        self.assertEqual(
            self._declared(), on_disk,
            "README's spec table disagrees with the controlled_spec.md files on disk",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
