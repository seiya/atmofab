#!/usr/bin/env python3
"""Every repository path the dev skills cite must be one git tracks.

`.claude/skills/` is DEV-ONLY — no workflow leaf reads it (`CLAUDE.md` is canonical) — but it
is what an operator and every review subagent are handed, and it is dense with pointers into
the tree. Nothing measured them: those files match none of the backend-boundary scanner's
globs, and the suite reached into that directory only for the one script it owns.

**Scope, stated because the first version of this file claimed more than it did.** This checks
one thing: a backticked token shaped like a repository path names something git tracks. It does
NOT check bare identifiers (`some_function`), and it does not verify that a `path::symbol`
citation's symbol still exists — a first draft did the latter by whole-file substring, which
resolved `tools/hooks/cli.py::TODO` and the truncated prefix of a renamed symbol, and this
repository deliberately keeps superseded names in prose, so that check was unsound in the
direction that matters. Bare-identifier citations were swept by hand at the time this landed
(108 of them; the three absent were all deliberate negative examples) and are left uncovered
rather than covered badly.

Two properties the first draft lacked, both found by review:

- **The corpus is discovered from git and its size is asserted.** Moving `references/` out from
  under a skill made the scan find nothing and pass — an empty corpus satisfies an
  absence-assertion for ever.
- **Nothing touches the filesystem.** The first draft asked `Path.exists()`, so a gitignored
  file present in the author's checkout passed here and failed in every clone; the repair
  after that still `read_text()` its way to a `FileNotFoundError` on a tree whose index and
  worktree disagree. Git is asked once, and it is the only thing asked.
"""
from __future__ import annotations

import re
import subprocess
import unittest
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = ".claude/skills"

# Citations to paths this repository deliberately does NOT track. Named one by one, with the
# reason: an untracked path is exactly what a stale pointer looks like, so an exemption has to
# be a decision rather than a shape.
_UNTRACKED_ON_PURPOSE = {
    # The operator's machine-local settings layer. The skill cites it to say what it is NOT —
    # not read by a leaf, not consulted by the permission gate — and it cannot be tracked
    # without becoming the thing it is being contrasted with.
    ".claude/settings.local.json",
}

# A backticked token carrying one of these is prose, not a pointer: a glob taught as an
# example, a placeholder, or a sentence fragment.
_NOT_A_PATH = "*?<>| "

# Skill-relative pointers (`references/verification.md`, `scripts/mutation_check.py`) resolve
# against the skill that contains them. These are the pointers a reader follows most often and
# the first draft could not see any of them.
_SKILL_RELATIVE_ROOTS = ("references/", "scripts/")


@lru_cache(maxsize=1)
def _git_paths() -> tuple[frozenset[str], frozenset[str]]:
    """`(tracked files, every directory prefix of one)`, straight from the index."""
    out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO_ROOT,
                         capture_output=True, text=True, check=True).stdout
    files = {entry for entry in out.split("\0") if entry}
    directories = set()
    for entry in files:
        parts = entry.split("/")
        for i in range(1, len(parts)):
            directories.add("/".join(parts[:i]))
    return frozenset(files), frozenset(directories)


def normalize(token: str) -> str:
    """Strip what prose adds to a path: punctuation, a trailing slash, an anchor, a locator.

    `docs/HOOKS.md#the-rule`, `tools/hooks/cli.py:120` and `tools/hooks/cli.py::_fn` all name
    the same file; refusing them was an over-refusal on correct writing in the first draft.
    """
    token = token.rstrip(".,;:)").rstrip("/")
    token = token.split("#", 1)[0].split("::", 1)[0]
    return re.sub(r":\d+$", "", token)


def citation_targets(text: str, source: str) -> list[str]:
    """Every backticked token in `text` that points at a path in this repository.

    A token is a pointer when its first segment is a top-level entry git tracks — derived, not
    a hand-kept prefix list, which is why `TODO.md`, `AGENTS.md`, `CLAUDE.md` and
    `leaf_config/` are in scope now and were invisible before — or when it is skill-relative.
    Returns repository-relative paths, so the caller has one kind of thing to check.
    """
    files, _directories = _git_paths()
    top_level = {entry.split("/")[0] for entry in files}
    skill_dir = Path(source).parent
    if skill_dir.parent.as_posix() != SKILL_ROOT:      # a file under references/ or scripts/
        skill_dir = skill_dir.parent
    targets = []
    for raw in sorted(set(re.findall(r"`([^`\n]+)`", text))):
        if any(char in raw for char in _NOT_A_PATH):
            continue
        token = normalize(raw)
        if not token:
            continue
        if token.startswith(_SKILL_RELATIVE_ROOTS):
            targets.append((skill_dir / token).as_posix())
        elif token.split("/")[0] in top_level:
            targets.append(token)
    return targets


def is_tracked(path: str) -> bool:
    files, directories = _git_paths()
    return path in _UNTRACKED_ON_PURPOSE or path in files or path in directories


@lru_cache(maxsize=1)
def skill_documents() -> tuple[str, ...]:
    files, _directories = _git_paths()
    return tuple(sorted(f for f in files
                        if f.startswith(SKILL_ROOT + "/") and f.endswith(".md")))


class SkillCitationTests(unittest.TestCase):

    def test_every_repository_path_a_skill_cites_is_tracked(self) -> None:
        """A pointer nobody can follow is worse than no pointer.

        These documents are read by subagents that cannot ask a question. On the issue #71
        branch three places asserted that `TODO.md` carried a measurement harness while it
        carried a pointer to a file that had never been in this repository.
        """
        broken = []
        for document in skill_documents():
            text = (REPO_ROOT / document).read_text(encoding="utf-8")
            for target in citation_targets(text, document):
                if not is_tracked(target):
                    broken.append(f"{document}: `{target}` is not a tracked path")
        self.assertEqual(broken, [], "\n".join(broken))

    def test_the_corpus_is_not_empty(self) -> None:
        """An absence-assertion over nothing passes for ever.

        Measured: moving `references/` out from under a skill made the check above find zero
        citations and report success with a deliberately broken pointer in place. The floor is
        well under today's figures so ordinary editing does not trip it, and it is a floor
        rather than an equality because these documents are edited constantly.
        """
        documents = skill_documents()
        self.assertGreaterEqual(len(documents), 6, documents)
        total = sum(len(citation_targets((REPO_ROOT / d).read_text(encoding="utf-8"), d))
                    for d in documents)
        self.assertGreaterEqual(total, 30, f"only {total} citations found — is the scan live?")


class CitationScannerSelfTests(unittest.TestCase):
    """SELF-TEST. The check above asserts an ABSENCE, so a scanner that found nothing would
    satisfy it. The rule is defined once, above, and driven here from both directions."""

    _SOURCE = f"{SKILL_ROOT}/metdsl-enforcement-change/SKILL.md"

    def test_a_broken_citation_is_found(self) -> None:
        targets = citation_targets("see `tools/no_such_file.py`", self._SOURCE)
        self.assertEqual(targets, ["tools/no_such_file.py"])
        self.assertFalse(is_tracked(targets[0]))

    def test_a_path_untracked_but_present_locally_is_not_accepted(self) -> None:
        """The environment-dependence a round-0 worktree run exposed: `workspace/` is
        gitignored and populated in a working checkout, absent from a clone."""
        self.assertFalse(is_tracked("workspace/orchestrations"))
        self.assertTrue(is_tracked("tools/hooks"))

    def test_a_deliberately_untracked_citation_is_exempt_by_name(self) -> None:
        self.assertTrue(is_tracked(".claude/settings.local.json"))

    def test_top_level_files_and_skill_relative_pointers_are_in_scope(self) -> None:
        """The three shapes the first draft's hand-kept prefix list could not see."""
        targets = citation_targets(
            "`TODO.md`, `AGENTS.md` and `references/verification.md`", self._SOURCE)
        # Order-insensitive: the question is scope, and coupling it to the sort order of raw
        # tokens would make an unrelated rename of one of them fail this row.
        self.assertCountEqual(
            targets,
            ["AGENTS.md", "TODO.md",
             f"{SKILL_ROOT}/metdsl-enforcement-change/references/verification.md"])

    def test_a_skill_relative_pointer_resolves_from_a_reference_file_too(self) -> None:
        source = f"{SKILL_ROOT}/metdsl-enforcement-change/references/verification.md"
        self.assertEqual(
            citation_targets("`scripts/measure_claude_tool.py`", source),
            [f"{SKILL_ROOT}/metdsl-enforcement-change/scripts/measure_claude_tool.py"])

    def test_prose_that_only_looks_like_a_path_is_not_a_citation(self) -> None:
        """The over-refusal direction, which is what gets a check deleted rather than fixed.

        These documents teach by quoting globs, host paths and placeholders. Reading them as
        pointers would fail the suite for correct writing.
        """
        text = ("`~/.bashrc` and `/etc/hostname` read, while `docs/**/*.md`, `<repo>/spec/*`, "
                "`skills/workflow-<step>/SKILL.md` and `tools/hooks/cli.py` differ")
        self.assertEqual(citation_targets(text, self._SOURCE), ["tools/hooks/cli.py"])

    def test_an_anchor_or_a_line_number_does_not_break_a_citation(self) -> None:
        for spelling in ("docs/HOOKS.md#the-rule", "docs/HOOKS.md:14", "docs/HOOKS.md::absolute",
                         "tools/tests/"):
            with self.subTest(spelling):
                targets = citation_targets(f"`{spelling}`", self._SOURCE)
                self.assertEqual(len(targets), 1, spelling)
                self.assertTrue(is_tracked(targets[0]), spelling)


if __name__ == "__main__":
    unittest.main()
