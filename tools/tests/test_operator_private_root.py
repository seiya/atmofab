#!/usr/bin/env python3
"""The operator-private root (`~/.atmofab`) resolves in ONE place per subtree.

Issues #132 and #133. Writers create trees under the operator-private root and one guard
forbids a leaf from reading it, and until this file existed nothing tied them together:
`tools/hooks/common.py::operator_secret_root` was the guard's anchor, while the leaf
launcher, `init_orchestration`'s operator-token writer and
`tools/run_workflow.py::_claim_lock_path` each spelled `Path.home() / ".atmofab" / ...`
for themselves. Moving the root was a four-site coordinated edit, and the #127 rename had
already split one of them. Issue #176 deleted the token writer with `dismiss-violation`,
leaving two writers and the guard.

A SEPARATE FILE, and not because `tools/tests/test_orchestration_runtime.py` is 39k lines.
The seam is what has no owner: it crosses `tools.hooks.common`, `tools.orchestration_runtime`
and `tools.run_workflow`, and no existing module imports all three. Keeping it small also
keeps the mutation check's `--test-cmd` down to one file.

What is PINNED here and what is only SAMPLED:

  * PINNED — that the writers and the guard land in one root, by driving the real
    `_claim_lock_path` / `_workflow_homes_root` / `protected_host_read_roots`
    under one patched `$HOME` (`test_the_two_writers_and_the_guard_resolve_one_root`);
  * PINNED — that `".atmofab"` is spelled exactly ONCE across `tools/` and
    `mcp_servers/`, and in `tools/hooks/common.py`
    (`test_the_dot_atmofab_constant_is_spelled_once`). The FILE and the COUNT, not the
    function name: hoisting the literal to a module constant in that file strengthens the
    property and is allowed. A BOUND ON GROWTH,
    not a detector: an f-string, a rename of the constant, or a regex spelling the path
    inside a longer string are all out of its reach, and its own docstring says so;
  * PINNED — that `docs/RUNBOOK.md`'s operator-private-root section names every live
    relocator and opens an inventory row for each default subtree
    (`test_the_runbook_names_every_relocator_and_opens_a_row_for_each_default`), coupled
    by members resolved FROM the code. Rule 3-a, because the relocators are stated in
    more than three places and this is the one an operator reads;
  * SAMPLED — the creation-side refusals. A few overrides that cannot be honoured, not
    the set of them.
"""

from __future__ import annotations

import ast
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tools.hooks.common as hooks_common
import tools.orchestration_runtime as ort
from tools import run_workflow
from tools.tests.private_root_fixture import (
    _private_root_redirects,
    isolated_homes_per_test_suite,
    redirect_isolated_homes_root_for_module,
    restore_isolated_homes_root_for_module,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def setUpModule() -> None:
    redirect_isolated_homes_root_for_module(__name__)


def tearDownModule() -> None:
    restore_isolated_homes_root_for_module(__name__)


def load_tests(loader, tests, pattern):  # unittest protocol
    return isolated_homes_per_test_suite(tests)


class WorkflowHomesRootOverrideTests(unittest.TestCase):
    """The creation side refuses an override the two sides cannot both honour.

    The resolver stays TOTAL — it feeds `protected_host_read_roots`, and a hook that
    raises while deciding a read is worse than one that guards a path nobody writes to —
    so the refusal has to be here, in the writer. SAMPLED: a few overrides that cannot be
    honoured, not the set of them.

    The second assertion in each case is the one that is easy to omit: the refusal must
    arrive before anything is created.

    These rows were written for the operator TOKEN STORE, the second tree
    `_require_usable_private_root_override` served. Issue #176 deleted that store with
    `dismiss-violation`, and the ones with a homes-root twin in
    `tools/tests/test_orchestration_runtime.py::DurableWorkflowHomesTests` went with it.
    What is here is the three whose subject has no twin there — an override naming an
    existing non-directory, an override CONTAINING the checkout, and the `.strip()` the
    resolver and the refusal must agree on — ported to the one caller that remains.
    """

    def _prepare_under(self, override: str, repo: Path, oid: str = "opr_1"):
        """Drive the homes preparer with `ATMOFAB_WORKFLOW_HOMES_ROOT` set to `override`."""
        env = {k: v for k, v in os.environ.items()
               if k != hooks_common.WORKFLOW_HOMES_ROOT_ENV}
        env[hooks_common.WORKFLOW_HOMES_ROOT_ENV] = override
        with mock.patch.dict(os.environ, env, clear=True):
            ort._create_workflow_backend_home(repo, oid, "claude", "Claude")

    def test_the_in_repo_refusal_rests_on_a_read_manifest_that_still_grants_spec(self) -> None:
        """The premise the in-repo refusal is FOR, pinned where it can be seen.

        The refusal exists because `_write_read_access_manifest` puts `docs/` and `spec/`
        in every agentic leaf's `allowed_read_roots` unconditionally and the Read tool
        never consults `protected_host_read_roots`. Nothing on this branch pinned that,
        and a round-3 reviewer said so: delete `"spec/"` from the base list and the
        refusal becomes correct-but-unmotivated with every test green.

        Driven through the real manifest writer rather than by reading the literal, so a
        change that keeps the constant but stops it reaching the manifest is caught too.
        Both roots, because either one alone reaches a store an operator would plausibly
        put there.

        This is a PREMISE check, not the rule: the refusal fails closed either way, and
        if this list ever legitimately loses `spec/` the right response is to re-derive
        the refusal's justification, not to delete this assertion.
        """
        policy = ort.build_access_policy_payload(
            agent_run_id="arid-1",
            request_payload={
                "node_key": "n", "step": "generate",
                "ir_ref": "workspace/ir/x",
                "pipeline_ref": "workspace/pipelines/x",
                "orchestration_id": "o",
            })
        roots = policy["allowed_read_roots"]
        for granted in ("docs/", "spec/"):
            with self.subTest(root=granted):
                self.assertIn(
                    granted, roots,
                    f"an agentic leaf no longer reads {granted} unconditionally. The "
                    "in-repo refusal in `_require_usable_private_root_override` is "
                    "justified by exactly this — re-derive its reason before assuming it "
                    "still holds")

    def test_an_override_naming_an_existing_file_is_refused_before_the_first_write(self) -> None:
        """The half-created tree the POSITION of the refusal is supposed to prevent.

        Found by Codex on the token store and measured there: an override naming an
        existing regular file passes the absolute check and the parent-exists check, so
        the writer ran on to `mkdir(exist_ok=True)` and raised a bare `FileExistsError`
        naming nothing. The refusal has to name the variable, and it has to arrive before
        the home directory exists.

        Ported to the homes root by issue #176 with the store it was written for; the
        clause it pins (`is not a directory`) is in the shared helper and had no other
        witness.
        """
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            (repo / "workspace" / "orchestrations" / "opr_notdir").mkdir(parents=True)
            for label, maker in (
                ("regular file", lambda p: p.write_text("not a dir\n", encoding="utf-8")),
                ("broken symlink", lambda p: p.symlink_to(Path(td) / "nothing-here")),
            ):
                target = Path(td) / f"homes-{label.replace(' ', '-')}"
                maker(target)
                with self.subTest(kind=label):
                    with self.assertRaises(ValueError) as ctx:
                        self._prepare_under(str(target), repo, oid="opr_notdir")
                    message = str(ctx.exception)
                    self.assertIn("is not a directory", message)
                    self.assertIn(hooks_common.WORKFLOW_HOMES_ROOT_ENV, message)
                    self.assertFalse(
                        (target / "opr_notdir").exists(),
                        "the refusal arrived after the tree was half-created")

    def test_a_homes_root_containing_the_checkout_is_refused(self) -> None:
        """The other containment direction, refused because no working configuration exists.

        These roots are exempt from the containment DROP so the guard is not lost when
        one of them overlaps the checkout. The consequence for a root ABOVE the checkout
        is that every in-repo path is a path under a protected root — and the guard
        matches the command's tokens, not only its read targets. Measured through the real
        `evaluate_common_policy` with `ATMOFAB_WORKFLOW_HOMES_ROOT` set to the checkout's
        parent: `cat README.md`, `ls`, `python3 tools/x.py` and `echo hi` all BLOCK. It used
        to read "with either root", and that quantifier died with the operator token store
        (issue #176): the relocator left beside this one, `ATMOFAB_START_CLAIM_ROOT`, is in no
        `protected_host_read_roots` entry, so all four of those commands ALLOW under it.

        `docs/RUNBOOK.md` described this as costing "every recursive in-repo read", which
        a round-5 reviewer measured as a wide understatement. Refusing costs nothing —
        there is no configuration here that runs — and turns a total, unexplained failure
        at the first leaf into one refusal naming the variable.

        Ported to the homes root by issue #176 with the token store it was written for.
        It is the ONLY witness of this half of the containment rule: the homes twin
        `test_a_homes_root_inside_the_checkout_is_refused` covers the INSIDE direction
        only.
        """
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "outer" / "repo"
            (repo / "workspace" / "orchestrations" / "opr_outer").mkdir(parents=True)
            with self.assertRaises(ValueError) as ctx:
                self._prepare_under(str(Path(td) / "outer"), repo, oid="opr_outer")
            message = str(ctx.exception)
            self.assertIn("must not contain the repository", message)
            self.assertIn(hooks_common.WORKFLOW_HOMES_ROOT_ENV, message)
            self.assertFalse((Path(td) / "outer" / "opr_outer").exists())

    def test_a_whitespace_only_override_takes_the_default(self) -> None:
        """`.strip()` in the resolver, which nothing observed.

        The resolver strips its override and so does the creation-side refusal. Drop the
        strip in one of them and the two judge `"   /abs/homes"` differently — the
        refusal calls it absolute while the resolver builds a path under the caller's
        working directory. A round-5 reviewer measured the deletion surviving 390 tests.
        Not exploitable at HEAD (the un-stripped path is then caught by the parent-exists
        or in-repo clause), which is why this pins the AGREEMENT rather than a verdict.

        Ported to the homes root by issue #176 with the token store it was written for.
        """
        with mock.patch.dict(
                os.environ,
                {"HOME": "/tmp/fake-home-probe",
                 hooks_common.WORKFLOW_HOMES_ROOT_ENV: "   "},
                clear=False):
            self.assertEqual(hooks_common.workflow_homes_root(),
                             Path("/tmp/fake-home-probe/.atmofab/homes"))


class OnePrivateRootTests(unittest.TestCase):
    """The writers land in ONE root. This is issue #132 itself."""

    def test_the_two_writers_resolve_one_root(self) -> None:
        """Move `$HOME` and both follow, together.

        The two sites are `run_workflow._claim_lock_path` (writes a start claim) and
        `orchestration_runtime._workflow_homes_root` (writes the isolated homes). Before
        issue #132 each writer built `Path.home() / ".atmofab" / …` for itself, so they
        agreed by coincidence: the #127 rename moved one and the others stayed. There
        were four sites; the operator-token writer and its `dismiss_violation` reader
        went with issue #176, and the READ GUARD — `protected_host_read_roots`, which
        refused a leaf's `Bash` read of either root — went with Z4 (issue #171). It
        guarded a leaf-held tool, and a pure leaf holds none: it receives a closed context
        and returns one document, so there is no read for the guard to refuse and nothing
        it could still be coupled to. What keeps the operator's root out of a leaf's reach
        now is the sandbox profile, which binds the repository and nothing else.

        Driven through the REAL functions, not through the resolvers — pinning at the
        resolver would leave the wiring free to be deleted, which is the failure this
        repository has already had (4 of 5 sites). `$HOME` is moved by patching
        `_home_dir` in `tools.hooks.common`, the one function `operator_secret_root`
        reads, and the three overrides are cleared so every default branch is taken.

        Nothing else ties them.
        """
        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td) / "home"
            fake_home.mkdir()
            repo = Path(td) / "repo"
            repo.mkdir()
            oid, arid = "opr_one", "arid-1"
            redirected = {name for name, _sub in _private_root_redirects()}
            env = {k: v for k, v in os.environ.items() if k not in redirected}
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(hooks_common, "_home_dir",
                                      return_value=fake_home):
                atmofab = (fake_home / ".atmofab").resolve()

                # WRITER 1 — the start claim.
                self.assertEqual(
                    run_workflow._claim_lock_path(repo, "spec", "spec/x").parent,
                    atmofab / "start_claims")

                # WRITER 2 — the isolated homes root.
                self.assertEqual(ort._workflow_homes_root(), atmofab / "homes")

                # No third site: the read guard that was the third assertion here is gone
                # with the tool it guarded (see the docstring).

    def test_the_dot_atmofab_constant_is_spelled_once(self) -> None:
        """`".atmofab"` is spelled exactly ONCE across `tools/` and `mcp_servers/`, in
        `tools/hooks/common.py`.

        A BOUND ON GROWTH, not a detector, and the difference matters. What it catches is
        the ordinary way this splits again: someone needs a path under the operator-private
        root, writes `Path.home() / ".atmofab" / "something"` where they are, and the
        guard never learns about it. What it CANNOT catch, stated rather than implied:

          * an f-string (`f"{home}/.atmofab/x"`) — the constant is not a bare `".atmofab"`
            node;
          * the same string inside a longer one — `tools/hooks/common.py` carries several
            marker regexes and argparse help texts that spell the path in prose, and they
            are out of scope by construction;
          * a rename of the constant, or resolution through a variable.

        So a green row here is not evidence that a change respects the rule — it is
        evidence that the rule was not broken in the one shape that has actually broken it
        three times. `tools/tests` is excluded: a test builds fake `~/.atmofab` layouts on
        purpose.

        RED on `origin/main`, where the set has three more members.
        """
        found: list[tuple[str, str]] = []
        for root, dirs, files in os.walk(REPO_ROOT):
            dirs[:] = [d for d in dirs
                       if d not in (".git", "__pycache__", "tests")
                       and not d.startswith("workspace")]
            rel_root = Path(root).relative_to(REPO_ROOT)
            if not rel_root.parts or rel_root.parts[0] not in ("tools", "mcp_servers"):
                continue
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = Path(root) / name
                rel = str(path.relative_to(REPO_ROOT))
                try:
                    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
                except SyntaxError:                  # not this test's subject
                    continue
                found.extend(self._atmofab_constants(tree, rel))
        # THE RULE IS "ONCE, IN THE MODULE THAT OWNS THE RESOLVERS", not "inside that
        # one function". The first version pinned the function name, which refused a
        # change that STRENGTHENS the property — hoisting the literal to a module-level
        # constant in the same file — and told the author to reach the location through
        # `operator_secret_root()`, which the code was already doing. Found by the round-2
        # census aiming the over-refusal question at the instrument.
        self.assertEqual(
            {rel for rel, _fn in found}, {"tools/hooks/common.py"},
            "`.atmofab` is spelled outside the module that owns the resolvers. Reach the "
            "location through `operator_secret_root()` / `workflow_homes_root()` / "
            "`run_workflow._start_claims_root()` instead "
            "(issue #132). If a NEW site genuinely has to spell the literal — a migration "
            "tool for a legacy tree is the plausible case — this assertion is where that "
            "decision gets recorded: widen it here, with the reason, rather than leaving "
            f"the rule stated in prose alone. Sites found: {sorted(found)}")
        self.assertEqual(
            len(found), 1,
            "the literal is spelled more than once inside tools/hooks/common.py. One "
            "spelling is the rule; where in the file it lives is not, so a module-level "
            "constant is fine and a second copy is not — including a second copy in the "
            f"SAME function, which an earlier version of this counter could not see. "
            f"Sites: {sorted(found)}")

    @staticmethod
    def _atmofab_constants(tree: ast.AST, rel: str) -> list[tuple[str, str]]:
        """`(relative path, enclosing function)` for every bare `".atmofab"` constant.

        A LIST, one entry per CONSTANT NODE. It was a set, and both round-3 reviewers
        found the same thing independently: two spellings in one scope collapsed to one
        tuple, so `len(found) == 1` counted scopes rather than spellings and a second
        `".atmofab"` inside `operator_secret_root` itself passed. The commit message that
        introduced the count asserted the stronger property, which the instrument did not
        have.
        """
        out: list[tuple[str, str]] = []

        def walk(node, enclosing: str) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    walk(child, child.name)
                    continue
                if isinstance(child, ast.Constant) and child.value == ".atmofab":
                    out.append((rel, enclosing))
                walk(child, enclosing)

        walk(tree, "<module>")
        return out

    def test_the_constant_reader_sees_a_synthetic_spelling(self) -> None:
        """Self-test for the bound above: an empty walk must not be able to pass it.

        Three halves now. That the reader FINDS a plain `Path.home() / ".atmofab" / "x"`;
        that it reports the enclosing function rather than the module, because a reader
        returning `<module>` for everything would make the assertion above trivially
        satisfiable by moving the spelling into a function; and that it counts NODES
        rather than scopes — two spellings in one function must come back as two, which
        is the defect both round-3 reviewers found in the previous version.
        """
        source = (
            "from pathlib import Path\n"
            "def somewhere():\n"
            "    return Path.home() / '.atmofab' / 'x'\n"
            "TOP = Path.home() / '.atmofab'\n"
        )
        self.assertEqual(
            len(OnePrivateRootTests._atmofab_constants(
                ast.parse("from pathlib import Path\n"
                          "def twice():\n"
                          "    a = Path.home() / '.atmofab' / 'x'\n"
                          "    b = Path.home() / '.atmofab' / 'y'\n"
                          "    return a, b\n"), "probe.py")),
            2, "two spellings in ONE scope must count as two")
        self.assertEqual(
            sorted(OnePrivateRootTests._atmofab_constants(ast.parse(source), "probe.py")),
            [("probe.py", "<module>"), ("probe.py", "somewhere")])


class RunbookStatesTheRelocatorsTests(unittest.TestCase):
    """Rule 3-a: couple the operator-facing document to the constants in the code.

    The relocators are stated in more than three places — `docs/RUNBOOK.md` at the
    inventory table and the hook-recovery row; `docs/HOOKS.md` twice;
    and the module comments in `orchestration_runtime`, `run_workflow` and
    `hooks/common`. Three or more statement sites is where
    `.claude/skills/atmofab-enforcement-change` rule 3-a says discipline has already
    lost, and the site an OPERATOR reads is the one to check first — a RUNBOOK that
    names one of two relocators sends them to a shell without the export that decides
    where their homes are.

    Coupled by MEMBERS, because the RUNBOOK names them in full, and the members are
    resolved FROM THE CODE (`_private_root_redirects`, itself built from the
    constants) rather than transcribed here — so a rename moves both sides and this test
    is not a second place the rule is spelled.

    Two of rule 3-a's traps are answered explicitly. The ANCHOR is the section heading,
    which precedes every sentence this checks and is byte-identical at `e0bae3d`, so it
    pins that the rule is stated rather than that a correction survived. The READER is
    BOUNDED to that section and the bound is self-tested below, or a document that
    mentions `ATMOFAB_START_CLAIM_ROOT` anywhere — it does, in the cold-start-guard
    bullet 90 lines earlier — would satisfy this on the strength of an unrelated
    sentence.

    NOT coupled: `docs/HOOKS.md` and the module comments. Naming the document a leaf
    never reads and the strings a test cannot reach
    would be coupling for its own sake; what this buys is that the operator-facing
    statement cannot fall behind the code.
    """

    ANCHOR = "## The operator-private root ("

    def _section(self) -> str:
        text = (REPO_ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
        # A missing anchor is a legitimate edit (someone reworded the heading), and it
        # used to arrive as an uncaught `ValueError: substring not found` naming nothing.
        # A check that refuses ordinary work has to say what to do about it — this one
        # names the anchor and where it is spelled.
        self.assertTrue(
            self.ANCHOR in text,
            f"docs/RUNBOOK.md has no section opening {self.ANCHOR!r}. If the heading was "
            "reworded, update `ANCHOR` here to the new one — it is deliberately text that "
            "PRECEDES the rule, so it pins that the rule is stated rather than that some "
            "correction survived.")
        start = text.index(self.ANCHOR)
        rest = text[start + len(self.ANCHOR):]
        end = rest.find("\n## ")
        section = rest if end < 0 else rest[:end]
        self.assertLess(len(section), len(text) / 2,
                        "the section slice is most of the document — the bound is broken, "
                        "and every assertion below would pass on an unrelated sentence")
        return section

    def test_the_runbook_names_every_relocator_and_opens_a_row_for_each_default(self) -> None:
        section = self._section()
        for env_name, subdir in _private_root_redirects():
            # `assertTrue`, not `assertIn`: the failure message has to name the repair,
            # and `assertIn` prints the whole section beside it.
            self.assertTrue(
                f"`{env_name}`" in section,
                f"docs/RUNBOOK.md §{self.ANCHOR.strip('# (')} does not name "
                f"{env_name}; an operator reading it would not know the tree can move")
            self.assertTrue(
                f"| `~/.atmofab/{subdir}" in section,
                f"the inventory table has no row opening on ~/.atmofab/{subdir}")

    def test_the_bound_excludes_text_on_both_sides_of_the_section(self) -> None:
        """The self-test for the bound, in the shapes that would actually defeat it.

        BOTH directions, because the first version only had one. Backward:
        `ATMOFAB_START_CLAIM_ROOT` appears in the cold-start-guard bullet of §Failure
        modes, far above this section — if the slice reached that far, deleting the
        relocator from the inventory would leave the row above green. Forward: the round-2
        census measured that mutating `rest.find("\n## ")` to `-1` left all twelve tests
        green, because the RUNBOOK's 44 KB tail happens to mention no relocator today.
        That is corpus-dependence, not a bound, so the forward end is asserted against the
        NEXT heading rather than against what the tail happens to contain.
        """
        text = (REPO_ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
        section = self._section()
        next_heading = "## Repair cheat sheet on a hook block"
        self.assertTrue(
            next_heading in text,
            f"the heading this bound stops at ({next_heading!r}) has moved; re-choose "
            "one that immediately follows the operator-private-root section")
        self.assertFalse(
            next_heading in section,
            "the section slice runs past its own section into the next one — the forward "
            "end of the bound is broken, and every assertion in this class would then be "
            "satisfiable by a sentence somewhere else in the document")
        # `assertTrue`, not `assertIn`: the haystack here is the whole 163 KB document.
        self.assertTrue(
            "(relocatable with `ATMOFAB_START_CLAIM_ROOT`) and a cold start takes one" in text,
            "the claim bullet this bound is tested against has moved or was reworded; "
            "re-choose a control sentence that names a relocator and sits OUTSIDE the "
            "operator-private-root section. (It has moved once already: issue #177 "
            "rewrote §3-1's concurrency bullet when the driver-liveness probe was "
            "deleted, and this control had pinned that sentence's exact wording.)")
        self.assertFalse(
            "(relocatable with `ATMOFAB_START_CLAIM_ROOT`) and a cold start takes one" in section,
            "the section slice reached outside the section — the bound is broken")


if __name__ == "__main__":  # pragma: no cover - manual runs
    unittest.main()
