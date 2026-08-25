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
import subprocess
import unittest
from functools import lru_cache
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
        # A trailing slash marks a directory in prose (`tools/tests/`); git tracks the
        # directory under its bare name, so both spellings must reach the same lookup.
        found.append(token.rstrip(".,;:)").rstrip("/"))
    return found


# Citations to paths this repository deliberately does NOT track. Each is named, with the
# reason, rather than matched by a pattern: an untracked path is exactly what a stale pointer
# looks like, so the exemption must be a decision and not a shape.
_UNTRACKED_ON_PURPOSE = {
    # The operator's machine-local settings layer. The skill names it to say what it is NOT
    # (not read by a leaf, not consulted by the permission gate), which is why it is cited at
    # all — and it cannot be tracked without becoming the thing it is contrasted with.
    ".claude/settings.local.json",
}


@lru_cache(maxsize=1)
def tracked_paths() -> frozenset[str]:
    """Every path git tracks, plus every directory prefix of one.

    Judged against GIT, not against the filesystem. The first version of this check asked
    `Path.exists()` and passed in the author's checkout while failing in a fresh worktree,
    because a gitignored machine-local file happened to be present — the "a test passes
    because of its own environment" shape this repository has recorded twice. It was caught
    by the round-0 mutation run, which builds worktrees, and not by the suite.
    """
    out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO_ROOT,
                         capture_output=True, text=True, check=True).stdout
    paths = set()
    for entry in out.split("\0"):
        if not entry:
            continue
        paths.add(entry)
        parts = entry.split("/")
        for i in range(1, len(parts)):
            paths.add("/".join(parts[:i]))
    return frozenset(paths)


def unresolved(token: str, root: Path) -> str:
    """"" if the citation resolves, else why it does not."""
    path, _, symbol = token.partition("::")
    if path in _UNTRACKED_ON_PURPOSE:
        return ""
    if path not in tracked_paths():
        return "not a tracked path"
    target = root / path
    if symbol and symbol.split("::")[0] not in target.read_text(encoding="utf-8"):
        return "file is tracked but does not contain the symbol"
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

    def test_an_untracked_file_that_exists_locally_does_not_resolve(self) -> None:
        """The environment-dependence that the round-0 worktree run exposed.

        `.gitignore` itself is tracked, so it cannot serve as the fixture; a path under a
        gitignored directory is present in the author's checkout and absent from a clone.
        Judging by `Path.exists()` made this check pass here and fail everywhere else.
        """
        self.assertTrue(unresolved("workspace/orchestrations", REPO_ROOT))
        self.assertFalse(unresolved("tools/hooks", REPO_ROOT))

    def test_a_deliberately_untracked_citation_is_exempt_by_name(self) -> None:
        self.assertFalse(unresolved(".claude/settings.local.json", REPO_ROOT))

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
