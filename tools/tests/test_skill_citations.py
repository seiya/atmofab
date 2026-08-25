#!/usr/bin/env python3
"""The dev skills under `.claude/skills/` cite this repository; the citations must resolve.

These files are DEV-ONLY — no workflow leaf reads them (`CLAUDE.md` is canonical for that) —
but they are what an operator and every review subagent are handed, and they are dense with
pointers into the tree: file paths, `path::symbol` references, and quotations of code. Nothing
measured them. `.claude/skills/**` matches none of the backend-boundary scanner's globs, and
the suite reached into that directory only for the one script it owns.

Two checks, both narrow on purpose. **A check that parses prose to decide what a claim IS will
refuse correct writing** — that failure took two rebuilds and a scope declaration on the
TODO:414 branch — so nothing here reads a sentence. The first check keys on the SHAPE of a
token (a backticked string starting with a repository directory), the second on a spelling the
code owns.

The second is this repository's own rule applied to the file that states it:
`metdsl-enforcement-change` rule 3-a says that past three statement sites, a rule's documents
must be COUPLED to the rule by reading its constant out of the code. That rule is stated in a
document which quotes the constant. So it is coupled here.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS = REPO_ROOT / ".claude" / "skills"

# A backticked token is treated as a repository citation when it starts with one of these and
# carries no glob metacharacter or space. That excludes the illustrative paths these documents
# are full of — `~/.bashrc`, `/etc/hostname`, `docs/**/*.md`, `<repo>/spec/*` — without
# reading what any sentence is claiming.
_CITATION_PREFIXES = ("docs/", "tools/", "mcp_servers/", "skills/", ".claude/", "spec/")
_NOT_A_CITATION = "*?<>| "


def repository_citations(text: str) -> list[str]:
    """Every backticked token in `text` that names a path in this repository."""
    found = []
    for token in sorted(set(re.findall(r"`([^`\n]+)`", text))):
        if not token.startswith(_CITATION_PREFIXES):
            continue
        if any(char in token for char in _NOT_A_CITATION):
            continue
        found.append(token.rstrip(".,;:)"))
    return found


def unresolved(token: str, root: Path) -> str:
    """"" if the citation resolves, else why it does not."""
    path, _, symbol = token.partition("::")
    target = root / path
    if not target.exists():
        return "file does not exist"
    if symbol and symbol.split("::")[0] not in target.read_text(encoding="utf-8"):
        return "file exists but does not contain the symbol"
    return ""


class SkillCitationTests(unittest.TestCase):

    def test_every_repository_path_a_skill_cites_resolves(self) -> None:
        """A pointer nobody can follow is worse than no pointer.

        These documents are read by subagents that cannot ask a question. On the issue #71
        branch, three places asserted that `TODO.md` carried a measurement harness while it
        carried a pointer to a file that had never been in this repository, and four sites
        named a production caller no callable answers to.
        """
        broken = []
        for path in sorted(SKILLS.rglob("*.md")):
            text = path.read_text(encoding="utf-8")
            for token in repository_citations(text):
                why = unresolved(token, REPO_ROOT)
                if why:
                    broken.append(f"{path.relative_to(REPO_ROOT)}: `{token}` — {why}")
        self.assertEqual(broken, [], "\n".join(broken))

    def test_the_glob_trigger_quoted_by_the_skill_is_the_one_the_hook_uses(self) -> None:
        """Rule 3-a, applied to the document that states it.

        The rule says: past three statement sites, read the rule's constant out of the code
        and require every statement to name it. `SKILL.md` quotes the `Glob` pattern trigger
        as the worked example. If the hook's trigger changes, that example becomes one more
        document stating a rule wrongly — which is the exact failure the rule exists to stop,
        occurring inside its own explanation.
        """
        hook = (REPO_ROOT / "tools" / "hooks" / "cli.py").read_text(encoding="utf-8")
        match = re.search(r"pattern\.startswith\(\(([^)]*)\)\)", hook)
        self.assertIsNotNone(match, "the pattern trigger is no longer a startswith tuple")
        assert match is not None
        spelling = f"pattern.startswith(({match.group(1)}))"
        skill = (SKILLS / "metdsl-enforcement-change" / "SKILL.md").read_text(encoding="utf-8")
        # `assertTrue`, not `assertIn`: the failure message of `assertIn` prints the whole
        # haystack, and the haystack here is a 700-line document. A check whose failure has
        # to be scrolled past is a check that gets weakened rather than answered.
        self.assertTrue(
            spelling in skill,
            f"the skill quotes a trigger the hook no longer uses; the hook's is {spelling!r}")


class CitationScannerSelfTests(unittest.TestCase):
    """SELF-TEST. Both checks above assert an ABSENCE of findings, so a scanner that found
    nothing at all would pass them for ever. The rule is defined once, above, and driven here
    from both sides — one text that must produce a finding and one that must not."""

    def test_a_broken_citation_is_found(self) -> None:
        text = "see `tools/this_file_does_not_exist.py` for the procedure"
        tokens = repository_citations(text)
        self.assertEqual(tokens, ["tools/this_file_does_not_exist.py"])
        self.assertTrue(unresolved(tokens[0], REPO_ROOT))

    def test_a_missing_symbol_is_found(self) -> None:
        self.assertTrue(
            unresolved("tools/hooks/cli.py::_no_such_symbol_here", REPO_ROOT))
        self.assertFalse(
            unresolved("tools/hooks/cli.py::_evaluate_grep_glob_read_policy", REPO_ROOT))

    def test_illustrative_paths_are_not_treated_as_citations(self) -> None:
        """The over-refusal direction, which is the one that makes a check get deleted.

        These documents teach by quoting patterns and host paths. Reading `~/.bashrc` or
        `docs/**/*.md` as a citation would fail the suite for correct writing, and the fix
        would be to weaken the check rather than the prose.
        """
        text = ("`~/.bashrc` and `/etc/hostname` read, while `docs/**/*.md`, `<repo>/spec/*` "
                "and `tools/hooks/cli.py` are different shapes")
        self.assertEqual(repository_citations(text), ["tools/hooks/cli.py"])


if __name__ == "__main__":
    unittest.main()
