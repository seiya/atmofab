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
- **Nothing touches the filesystem** — neither the path set nor the CONTENT. The first draft
  asked `Path.exists()`, so a gitignored file present in the author's checkout passed here and
  failed in every clone. The first repair fixed only the path set and still `read_text()` its
  way to a `FileNotFoundError` on a tree whose index and worktree disagree; it said in its own
  docstring that it had fixed both, which is the class this branch exists to stop. Paths come
  from `git ls-files` and bodies from `git show :<path>`, so both sides read the same index.
"""
from __future__ import annotations

import functools
import re
import subprocess
import tempfile
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
    # A locator is a line, a line RANGE, or either with an `L` prefix; a possessive `'s` is
    # ordinary prose around a filename. The first version handled only a bare single line, so
    # `cli.py:120-140`, `HOOKS.md:L14` and `cli.py's` were all refused — over-refusal on
    # correct writing, which is what gets a check weakened instead of answered.
    token = re.sub(r"'s$", "", token)
    return re.sub(r":L?\d+(-L?\d+)?$", "", token)


def _pointer_tokens(text: str) -> set[str]:
    """Every token in `text` written as a pointer to a reader: backticked, or a link target.

    Backticks are how these documents point today; `[label](path)` and `![alt](path)` are the
    other spelling Markdown gives, and reading only the first reports a file pointed at by a link
    as unreachable — an over-refusal on correct writing, which is what gets a check deleted rather
    than fixed. A target carrying a scheme (`https:`) is left to the path filter in the caller,
    which admits only first segments this repository tracks.
    """
    return (set(re.findall(r"`([^`\n]+)`", text))
            | set(re.findall(r"\]\(([^)\s]+)\)", text)))


def citation_targets(text: str, source: str) -> list[str]:
    """Every token in `text`, backticked or a Markdown link target, that points at a path here.

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
    for raw in sorted(_pointer_tokens(text)):
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


def document_body(path: str, root: Path = REPO_ROOT) -> str:
    """The document as the INDEX holds it.

    Not `read_text`. A tree whose index and worktree disagree — a deletion not yet staged, a
    partial or sparse checkout, a rebase in flight — makes the filesystem read raise while the
    path set says the file is there, so the check dies with `FileNotFoundError` instead of
    reporting on citations. The two halves have to ask the same source.
    """
    return subprocess.run(["git", "show", f":{path}"], cwd=root,
                          capture_output=True, text=True, check=True).stdout


def skill_names() -> frozenset[str]:
    """The skill directories git tracks a document in."""
    return frozenset(document.split("/")[2] for document in skill_documents())


class SkillCitationTests(unittest.TestCase):

    @staticmethod
    def _corpus_by_pathspec() -> frozenset[str]:
        """The corpus again, asked of git a DIFFERENT way.

        `skill_documents()` filters the full `ls-files` output in Python; this asks git to do
        the matching with a glob pathspec. Two derivations, so a change to either one's filter
        shows up as a disagreement — a floor expressed as a count did not: dropping every
        `references/` document (a third of the corpus, including the file this branch edited)
        still cleared "at least 6 documents, at least 30 citations".
        """
        out = subprocess.run(
            ["git", "ls-files", "-z", "--", f":(glob){SKILL_ROOT}/**/*.md"],
            cwd=REPO_ROOT, capture_output=True, text=True, check=True).stdout
        return frozenset(entry for entry in out.split("\0") if entry)

    def test_every_repository_path_a_skill_cites_is_tracked(self) -> None:
        """A pointer nobody can follow is worse than no pointer.

        These documents are read by subagents that cannot ask a question. On the issue #71
        branch three places asserted that `TODO.md` carried a measurement harness while it
        carried a pointer to a file that had never been in this repository.
        """
        broken = []
        for document in skill_documents():
            text = document_body(document)
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
        # STRUCTURAL, not just a number. A floor of "6 documents / 30 citations" was measured
        # to pass with a THIRD of the corpus dropped — including the reference file this branch
        # edited — because the remainder still cleared it. What must hold is that every skill
        # git tracks a document in is represented, and that each contributes something.
        self.assertEqual(frozenset(documents), self._corpus_by_pathspec())
        for name in sorted(skill_names()):
            docs = [d for d in documents if d.split("/")[2] == name]
            found = sum(len(citation_targets(document_body(d), d)) for d in docs)
            self.assertGreater(found, 0, f"{name} contributed no citations — is the scan live?")


class CitationScannerSelfTests(unittest.TestCase):
    """SELF-TEST. The check above asserts an ABSENCE, so a scanner that found nothing would
    satisfy it. The rule is defined once, above, and driven here from both directions."""

    _SOURCE = f"{SKILL_ROOT}/metdsl-enforcement-change/SKILL.md"

    def test_a_broken_citation_is_found(self) -> None:
        targets = citation_targets("see `tools/no_such_file.py`", self._SOURCE)
        self.assertEqual(targets, ["tools/no_such_file.py"])
        self.assertFalse(is_tracked(targets[0]))

    def test_a_path_untracked_but_present_on_disk_is_not_accepted(self) -> None:
        """The witness for "git, not the filesystem" — and it must hold in EVERY checkout.

        The first version of this row named `workspace/orchestrations`, which is gitignored
        and populated in a working checkout and ABSENT from a clone. So in a clone it passed
        because the path did not exist, not because the predicate asks git: reverting
        `is_tracked` to `Path.exists()` left all nine rows green there. The witness has to name
        something that is present in every checkout and tracked in none. `.git` is exactly
        that — a directory in a main checkout, a file in a linked worktree, never tracked.
        """
        self.assertTrue((REPO_ROOT / ".git").exists(), "no .git — this row cannot witness")
        self.assertFalse(is_tracked(".git"))
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

    def test_the_body_is_read_from_the_index_not_the_worktree(self) -> None:
        """The property the previous round claimed to have fixed and had not.

        Replacing `document_body` with `read_text` passes in any checkout where the index and
        the worktree agree — which is every checkout the suite normally runs in, so the
        mutation survived and the docstring's claim went unwitnessed. A tree where they
        DISAGREE has to be built: this stages a file and then deletes it from disk, which is
        an unstaged deletion, a partial checkout and a rebase in flight all at once as far as
        this predicate is concerned.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = functools.partial(subprocess.run, cwd=root, check=True,
                                    capture_output=True, text=True)
            run(["git", "init", "-q"])
            run(["git", "config", "user.email", "t@example.invalid"])
            run(["git", "config", "user.name", "t"])
            (root / "doc.md").write_text("cites `tools/hooks/cli.py`", encoding="utf-8")
            run(["git", "add", "doc.md"])
            (root / "doc.md").unlink()
            self.assertEqual(document_body("doc.md", root), "cites `tools/hooks/cli.py`")
            with self.assertRaises(FileNotFoundError):
                (root / "doc.md").read_text(encoding="utf-8")

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
                         "tools/tests/", "tools/hooks/cli.py:120-140", "docs/HOOKS.md:L14",
                         "docs/HOOKS.md#L14", "tools/hooks/cli.py's"):
            with self.subTest(spelling):
                targets = citation_targets(f"`{spelling}`", self._SOURCE)
                self.assertEqual(len(targets), 1, spelling)
                self.assertTrue(is_tracked(targets[0]), spelling)


class SkillReachabilityTests(unittest.TestCase):
    """Every file a skill directory holds must be reachable from its `SKILL.md`.

    A skill's `SKILL.md` is loaded in full whenever the skill fires; its `references/` and
    `scripts/` are loaded only when something points a reader at them. That split is what keeps
    the loaded-on-every-invocation cost down, and it holds only while the entry point can reach
    everything: a reference nothing cites is not cheap, it is dead, and the material it holds is
    gone without anyone noticing it left. Measured on the commit that split the two dev skills
    into rule + episode files, which is exactly the edit that can drop a pointer.

    REACHABILITY, not direct citation, and the three widenings each answer a piece of correct
    work an earlier version refused (every one of them measured against a fixture, not imagined):

    - a second-level split — a reference that grows its own sub-file, cited from its parent
    - a file cited from a `scripts/` file the entry point names, so the walk follows any tracked
      file rather than documents alone
    - a reference SHARED between the two skills, cited from the other one's `SKILL.md`, so the
      roots are every skill's entry point at once and only the ownership split is per skill

    What it still does not see is a path that appears only inside a fenced code block, where the
    fence's own backticks hide it from the inline scan. The refusal message says what counts.
    """

    #: What a pointer has to look like for the walk to follow it. Named because the refusal
    #: message quotes it: a rule a reader is told to satisfy must be stated where it is applied.
    _POINTER_FORMS = "a backticked path or a Markdown link target, outside a fenced code block"

    def _reachable(self, root: Path | None = None) -> tuple[dict[str, set[str]], set[str]]:
        """`(files each skill owns, every skill file reachable from SOME skill's SKILL.md)`.

        `root` is resolved at CALL time so the self-test below can point the walk at a tree it
        built; `document_body`'s default binds `REPO_ROOT` at import and cannot be redirected.
        """
        root = root or REPO_ROOT
        files, _directories = _git_paths()
        owned: dict[str, set[str]] = {}
        for entry in files:
            if entry.startswith(SKILL_ROOT + "/"):
                owned.setdefault(entry.split("/")[2], set()).add(entry)
        seen, queue = set(), []
        for skill, entries in sorted(owned.items()):
            entry_point = f"{SKILL_ROOT}/{skill}/SKILL.md"
            self.assertIn(entry_point, entries,
                          f"{skill} has no SKILL.md: a skill directory with no entry point makes "
                          f"every file in it unreachable by construction")
            seen.add(entry_point)
            queue.append(entry_point)
        every_skill_file = {entry for entries in owned.values() for entry in entries}
        while queue:
            document = queue.pop()
            for target in citation_targets(document_body(document, root), document):
                # only a tracked skill file is followed: `document_body` reads the index, so a
                # citation to a path outside it would raise instead of reporting.
                if target in every_skill_file and target not in seen:
                    seen.add(target)
                    queue.append(target)
        return owned, seen

    def _orphan_report(self, owned: dict[str, set[str]], reachable: set[str]) -> list[str]:
        """The finding list, defined once and called from both the row below and its self-test.

        A self-test that rebuilds the reporting inside itself cannot see a reporting bug: with
        this list assembled inline in the row, dropping every append left all rows green, because
        the real tree has no orphan and the self-tests were calling the WALK rather than the row.

        DOCUMENTS only, and the reason is over-refusal rather than tidiness. The walk FOLLOWS a
        script (a reference cited only from `scripts/x.py` is reachable), but a script's own
        dependencies arrive by import, which is not a citation: requiring one would report an
        ordinary helper module beside a cited script as dead and tell the author to delete it.
        Caught by a `.pyc` that a scratch fixture had committed, which the rule then demanded a
        citation for. What the cost argument is about is documents — they are what a reader loads.
        """
        return [f"{entry}: no chain of citations from any skill's SKILL.md reaches it — point at "
                f"it ({self._POINTER_FORMS}) from SKILL.md or from a file SKILL.md reaches, or "
                f"delete it"
                for skill in sorted(owned)
                for entry in sorted(owned[skill] - reachable) if entry.endswith(".md")]

    def test_no_file_in_a_skill_is_unreachable_from_a_skill_md(self) -> None:
        owned, reachable = self._reachable()
        self.assertTrue(owned, f"no skill found under {SKILL_ROOT} — is the scan live?")
        orphans = self._orphan_report(owned, reachable)
        self.assertEqual(orphans, [], "\n".join(orphans))

    def _walk_a_built_tree(self, layout: dict[str, str]) -> tuple[dict[str, set[str]], set[str]]:
        """Run the walk over a git tree built here from `layout` (path -> body).

        `-f` on the add and a neutralised `core.excludesFile`: the fixture builds under
        `.claude/`, and an operator whose global ignore file lists that directory would otherwise
        get "probe has no SKILL.md" — the check blaming the fixture for the environment. Measured
        as a real configuration for this operator (`TODO.md` records `.claude/settings.local.json`
        having been carried through one).
        """
        # A witness for the cleanup below, run every time a fixture is built: if a previous
        # fixture's index had leaked through the cache, this file would not be in it.
        self.assertIn("tools/tests/test_skill_citations.py", _git_paths()[0],
                      "a previous fixture leaked into the path cache")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        run = functools.partial(subprocess.run, cwd=root, check=True,
                                capture_output=True, text=True)
        run(["git", "-c", "core.excludesFile=/dev/null", "init", "-q"])
        for relative, body in layout.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
        run(["git", "add", "-A", "-f"])
        _git_paths.cache_clear()
        self.addCleanup(_git_paths.cache_clear)          # not a `finally`: a failed assertion
        original_root = globals()["REPO_ROOT"]           # below must not leak the temp index
        self.addCleanup(globals().__setitem__, "REPO_ROOT", original_root)
        globals()["REPO_ROOT"] = root                    # `_git_paths` reads it at call time
        return self._reachable(root)

    def test_the_walk_can_actually_see_an_orphan(self) -> None:
        """SELF-TEST of the row above, which is an absence-assertion over a graph walk.

        A walk that marked everything reachable — an `owned`-returning bug, a citation scanner
        that matched every token — would report zero orphans for ever. Drive it against a tree
        built here that HAS one, so the detector is witnessed in both directions in one place.
        """
        probe = f"{SKILL_ROOT}/probe"
        owned, reachable = self._walk_a_built_tree({
            f"{probe}/SKILL.md": "see `references/cited.md` and run `scripts/tool.py`",
            f"{probe}/references/cited.md": "onward to `references/second.md`",
            f"{probe}/references/second.md": "a second-level split",
            f"{probe}/scripts/tool.py": '"""reads `references/from_script.md`."""\n',
            f"{probe}/references/from_script.md": "reached through a script",
            f"{probe}/references/orphan.md": "nobody points here",
        })
        self.assertEqual(sorted(owned["probe"] - reachable),
                         [f"{probe}/references/orphan.md"])
        # drive the REPORTING layer, not only the walk: the row above is what runs in anger
        report = self._orphan_report(owned, reachable)
        self.assertEqual(len(report), 1, report)
        self.assertIn(f"{probe}/references/orphan.md", report[0])
        self.assertIn(self._POINTER_FORMS, report[0])
        # the over-refusal direction: each of these is correct work an earlier version refused
        for reached in ("references/second.md", "scripts/tool.py",
                        "references/from_script.md"):
            self.assertIn(f"{probe}/{reached}", reachable, reached)

    def test_a_reference_shared_between_two_skills_is_not_an_orphan(self) -> None:
        """The roots are every skill's SKILL.md, not one at a time.

        Walking a skill in isolation reports a file the OTHER skill cites as dead and tells the
        author to delete it. The two dev skills overlap in subject, so this is plausible work.
        """
        owned, reachable = self._walk_a_built_tree({
            f"{SKILL_ROOT}/one/SKILL.md":
                "the pair lives in `.claude/skills/two/references/shared.md`",
            f"{SKILL_ROOT}/two/SKILL.md": "nothing of its own",
            f"{SKILL_ROOT}/two/references/shared.md": "cited only from the other skill",
        })
        self.assertEqual(owned["two"] - reachable, set())

    def test_a_link_only_pointer_is_followed(self) -> None:
        """`[label](path)` is a pointer a reader can follow, so the walk must follow it too."""
        owned, reachable = self._walk_a_built_tree({
            f"{SKILL_ROOT}/probe/SKILL.md": "see [the notes](references/linked.md)",
            f"{SKILL_ROOT}/probe/references/linked.md": "reached through a Markdown link",
        })
        self.assertEqual(owned["probe"] - reachable, set())

    def test_a_helper_beside_a_cited_script_is_not_reported(self) -> None:
        """The over-refusal the document-only rule exists to avoid.

        `scripts/tool.py` is cited; `scripts/helper.py` is imported by it. An import is not a
        citation, so a rule over every tracked file reports the helper as dead.
        """
        owned, reachable = self._walk_a_built_tree({
            f"{SKILL_ROOT}/probe/SKILL.md": "run `scripts/tool.py`",
            f"{SKILL_ROOT}/probe/scripts/tool.py": "import helper\n",
            f"{SKILL_ROOT}/probe/scripts/helper.py": "value = 1\n",
        })
        self.assertIn(f"{SKILL_ROOT}/probe/scripts/helper.py", owned["probe"] - reachable)
        self.assertEqual(self._orphan_report(owned, reachable), [])

    def test_a_citation_out_of_the_skill_tree_is_reported_not_raised(self) -> None:
        """The walk follows only tracked SKILL files, and the reason is not tidiness.

        `document_body` reads the index with `check=True`, so following a citation to anything
        else turns a report into `CalledProcessError` — a crash, which reads as a kill while
        asserting nothing about the reachable set. These documents cite `docs/` and `tools/`
        constantly, so the path is ordinary, not hypothetical.
        """
        owned, reachable = self._walk_a_built_tree({
            f"{SKILL_ROOT}/probe/SKILL.md":
                "the rule is in `docs/HOOKS.md` and `references/cited.md` has the episode",
            f"{SKILL_ROOT}/probe/references/cited.md": "the episode",
            "docs/HOOKS.md": "not part of any skill",
        })
        self.assertEqual(owned["probe"] - reachable, set())
        self.assertNotIn("docs/HOOKS.md", reachable)

    def test_a_skill_with_no_entry_point_is_named_as_such(self) -> None:
        """Witness for the `assertIn` guard: without it the walk dies on `git show` instead.

        A directory tracking documents but no `SKILL.md` makes every file in it unreachable by
        construction, and the failure has to say that rather than raise `CalledProcessError`.
        """
        with self.assertRaises(AssertionError) as caught:
            self._walk_a_built_tree({
                f"{SKILL_ROOT}/headless/references/x.md": "no entry point above me",
            })
        self.assertIn("has no SKILL.md", str(caught.exception))

    def test_the_scan_is_live_and_the_root_constant_resolves(self) -> None:
        """The floor `test_the_corpus_is_not_empty` cannot express, because it compares two
        derivations that go empty together.

        Measured: `SKILL_ROOT = ".claude/skillz"` — or `git mv`-ing the directory — left every
        other row in this module green, including the reachability row and the "corpus is not
        empty" row, whose equality then holds between two empty sets and whose per-skill loop
        iterates zero times. What has to be asserted is that the constant names something git
        tracks, and that walking it produces work.
        """
        _files, directories = _git_paths()
        self.assertIn(SKILL_ROOT, directories,
                      f"{SKILL_ROOT} is not a tracked directory — the constant is stale and "
                      f"every absence-assertion in this module is vacuous")
        owned, reachable = self._reachable()
        self.assertTrue(owned, f"no skill directory under {SKILL_ROOT}")
        self.assertGreater(len(reachable), len(owned),
                           "every skill reduced to its SKILL.md alone — the walk followed no "
                           "citation, so the orphan row is observing nothing")


if __name__ == "__main__":
    unittest.main()
