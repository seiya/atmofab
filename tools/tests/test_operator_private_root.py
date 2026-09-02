#!/usr/bin/env python3
"""The operator-private root (`~/.atmofab`) resolves in ONE place per subtree.

Issues #132 and #133. Three writers create trees under the operator-private root and one
guard forbids a leaf from reading it, and until this file existed nothing tied the four
together: `tools/hooks/common.py::operator_secret_root` was the guard's anchor, while
`init_orchestration`, `dismiss_violation` and `tools/run_workflow.py::_claim_lock_path`
each spelled `Path.home() / ".atmofab" / ...` for themselves. Moving the root was a
four-site coordinated edit, and the #127 rename had already split one of them.

A SEPARATE FILE, and not because `tools/tests/test_orchestration_runtime.py` is 39k lines.
The seam is what has no owner: it crosses `tools.hooks.common`, `tools.orchestration_runtime`
and `tools.run_workflow`, and no existing module imports all three. Keeping it small also
keeps the mutation check's `--test-cmd` down to one file.

What is PINNED here and what is only SAMPLED:

  * PINNED — that the three writers and the guard land in one root, by driving the real
    `init_orchestration` / `dismiss_violation` / `_claim_lock_path` / `protected_host_read_roots`
    under one patched `$HOME` (`test_the_three_writers_and_the_guard_resolve_one_root`);
  * PINNED — that `".atmofab"` is spelled in exactly one function across `tools/` and
    `mcp_servers/` (`test_the_dot_atmofab_constant_is_spelled_once`). A BOUND ON GROWTH,
    not a detector: an f-string, a rename of the constant, or a regex spelling the path
    inside a longer string are all out of its reach, and its own docstring says so;
  * PINNED — that `docs/RUNBOOK.md`'s operator-private-root section names all three
    relocators and opens an inventory row for each default subtree
    (`test_the_runbook_names_every_relocator_and_opens_a_row_for_each_default`), coupled
    by members resolved FROM the code. Rule 3-a, because the relocators are stated in
    more than three places and this is the one an operator reads;
  * SAMPLED — the creation-side refusals. Two overrides that cannot be honoured, not the
    set of them.
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
from tools.tests.leaf_config_fixture import (
    _private_root_redirects,
    isolated_homes_per_test_suite,
    redirect_isolated_homes_root_for_module,
    restore_isolated_homes_root_for_module,
    seed_claude_leaf_config,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def setUpModule() -> None:
    redirect_isolated_homes_root_for_module(__name__)


def tearDownModule() -> None:
    restore_isolated_homes_root_for_module(__name__)


def load_tests(loader, tests, pattern):  # unittest protocol
    return isolated_homes_per_test_suite(tests)


class OperatorTokenRootOverrideTests(unittest.TestCase):
    """The creation side refuses an override the two sides cannot both honour.

    The resolver stays TOTAL — it feeds `protected_host_read_roots`, and a hook that
    raises while deciding a read is worse than one that guards a path nobody writes to —
    so the refusal has to be here, in the writer. SAMPLED: two overrides that cannot be
    honoured, not the set of them.

    The second assertion in each case is the one that is easy to omit: the refusal must
    arrive before anything is created. `init_orchestration` writes
    `workspace/orchestrations/<oid>/` and a `running` meta long before it reaches the
    token, and a run that looks started with no token is a run whose violations the
    operator cannot dismiss.
    """

    def _init_under(self, override: str, repo: Path, oid: str = "opr_1"):
        env = {k: v for k, v in os.environ.items()
               if k != hooks_common.OPERATOR_TOKENS_ROOT_ENV}
        env[hooks_common.OPERATOR_TOKENS_ROOT_ENV] = override
        with mock.patch.dict(os.environ, env, clear=True):
            ort.init_orchestration(repo, oid, spec_ref="spec/x.yaml")

    def test_a_relative_override_is_refused_before_anything_is_created(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            with self.assertRaises(ValueError) as ctx:
                self._init_under("relative/tokens", repo)
            message = str(ctx.exception)
            self.assertIn(hooks_common.OPERATOR_TOKENS_ROOT_ENV, message)
            self.assertIn("absolute", message)
            self.assertIn("the token store", message)
            self.assertNotIn("creates a home", message)
            self.assertFalse((repo / "workspace" / "orchestrations" / "opr_1").exists())

    def test_an_override_whose_parent_is_missing_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            missing = Path(td) / "no" / "such" / "parent" / "tokens"
            with self.assertRaises(ValueError) as ctx:
                self._init_under(str(missing), repo)
            message = str(ctx.exception)
            self.assertIn("does not exist", message)
            self.assertIn(hooks_common.OPERATOR_TOKENS_ROOT_ENV, message)
            self.assertFalse((repo / "workspace" / "orchestrations" / "opr_1").exists())
            # NAMES ITS OWN TREE. The condition is shared with the homes root; the wording
            # is not, because three trees under `~/.atmofab` are relocatable and a message
            # saying "home" for the TOKEN STORE sends the operator to the wrong one. A
            # round-1 reviewer measured the shared sentence saying "isolated operator
            # token home root" and "the conductor that creates a home".
            self.assertIn("operator token store", message)
            self.assertNotIn("home", message)

    def test_a_token_store_inside_the_checkout_is_refused(self) -> None:
        """The Bash guard is not the only layer, and it was the only one this rule had.

        Found by Codex. `ATMOFAB_OPERATOR_TOKENS_ROOT=<repo>/spec/tokens` was accepted
        and `init_orchestration` wrote the dismiss-violation token there — measured. The
        token's whole design is that no read manifest reaches it
        (`tools/orchestration_runtime.py::dismiss_violation` says so), and
        `_write_read_access_manifest` grants EVERY agentic leaf `docs/` and `spec/`
        unconditionally while the Read tool consults `protected_host_read_roots` not at
        all. A leaf reading the token approves the `unauthorized_write_violation` its own
        substep produced and reports the substep done — the shortcut at full severity.

        `docs/RUNBOOK.md` warned against this configuration and named the WRONG cost
        (recursive in-repo reads failing closed). That is what surface 9 looks like from
        the inside: the closure was asserted for a configuration nobody had enumerated.

        Only the INSIDE direction is refused. A store that CONTAINS the checkout keeps
        its files outside the repository, where a repo-relative manifest cannot name
        them; its cost is the fail-closed recursive read, and that stays documented
        rather than refused. The symlinked spelling is included because the check
        resolves — laundering the path through a link would otherwise be free.
        """
        with tempfile.TemporaryDirectory() as td, \
                tempfile.TemporaryDirectory() as outside:
            repo = Path(td) / "repo"
            (repo / "spec").mkdir(parents=True)
            inside = repo / "spec" / "tokens"
            link = Path(outside) / "link_spec"
            link.symlink_to(repo / "spec", target_is_directory=True)
            for label, override in (("plain", inside), ("symlinked", link / "tokens")):
                with self.subTest(spelling=label):
                    with self.assertRaises(ValueError) as ctx:
                        self._init_under(str(override), repo, oid="opr_inrepo")
                    message = str(ctx.exception)
                    self.assertIn("must not be inside the repository", message)
                    self.assertIn(hooks_common.OPERATOR_TOKENS_ROOT_ENV, message)
                    self.assertFalse(
                        (repo / "workspace" / "orchestrations" / "opr_inrepo").exists())
                    self.assertFalse(inside.exists())

    def test_an_override_naming_an_existing_file_is_refused_before_the_first_write(self) -> None:
        """The half-initialized run the POSITION of the refusal is supposed to prevent.

        Found by Codex, and measured: an override naming an existing regular file passes
        the absolute check and the parent-exists check, so `init_orchestration` ran to
        completion through `workspace/orchestrations/<oid>/` and a `status: running`
        meta — eleven directories and the metadata on disk — before
        `operator_token_path.parent.mkdir(exist_ok=True)` raised `FileExistsError`. That
        is exactly the state commit `ec6cec1` claims this call's position prevents, for
        an input that call did not examine.

        A run that looks started and has no token is a run whose violations the operator
        cannot dismiss, and the failure it arrives as says nothing about the variable.
        """
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            for label, maker in (
                ("regular file", lambda p: p.write_text("not a dir\n", encoding="utf-8")),
                ("broken symlink", lambda p: p.symlink_to(Path(td) / "nothing-here")),
            ):
                target = Path(td) / f"store-{label.replace(' ', '-')}"
                maker(target)
                with self.subTest(kind=label):
                    with self.assertRaises(ValueError) as ctx:
                        self._init_under(str(target), repo, oid="opr_notdir")
                    message = str(ctx.exception)
                    self.assertIn("is not a directory", message)
                    self.assertIn(hooks_common.OPERATOR_TOKENS_ROOT_ENV, message)
                    self.assertFalse(
                        (repo / "workspace" / "orchestrations" / "opr_notdir").exists(),
                        "the refusal arrived after the run was half-created")

    def test_a_tilde_token_store_override_is_expanded(self) -> None:
        """`~` is expanded, and nothing else observes that.

        The twin of `test_a_tilde_homes_root_override_is_expanded`. A quoted
        `ATMOFAB_OPERATOR_TOKENS_ROOT='~/tokens'` is a plausible spelling and the shell
        does not expand inside quotes; without `expanduser` the value would be a literal
        `~` directory under whoever's working directory, the writer and the read guard
        would resolve different trees, and it would slip past the absolute-path refusal
        because `~/...` is not absolute until expanded.
        """
        with mock.patch.dict(
                os.environ,
                {"HOME": "/tmp/fake-home-probe",
                 hooks_common.OPERATOR_TOKENS_ROOT_ENV: "~/big/tokens"},
                clear=False):
            resolved = hooks_common.operator_tokens_root()
        self.assertEqual(resolved, Path("/tmp/fake-home-probe/big/tokens"))
        self.assertNotIn("~", str(resolved))

    def test_dismiss_violation_reads_the_relocated_store(self) -> None:
        """The reader follows the relocator, and does so because it asks the resolver.

        The fake `$HOME` has NO `.atmofab` at all, so a reader that still built the path
        from `Path.home()` finds nothing and this fails on the not-found branch rather
        than on a wrong verdict.
        """
        from tools.orchestration_runtime import (
            _violations_dir,
            _write_unauthorized_write_violation,
            dismiss_violation,
        )
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            home.mkdir()
            store = Path(td) / "elsewhere" / "tokens"
            store.parent.mkdir()
            repo = Path(td) / "repo"
            repo.mkdir()
            seed_claude_leaf_config(repo)
            oid, arid = "opr_dismiss", "arid-1"
            env = {k: v for k, v in os.environ.items()
                   if k != hooks_common.OPERATOR_TOKENS_ROOT_ENV}
            env["HOME"] = str(home)
            env[hooks_common.OPERATOR_TOKENS_ROOT_ENV] = str(store)
            with mock.patch.dict(os.environ, env, clear=True):
                ort.init_orchestration(repo, oid, spec_ref="spec/x.yaml")
                token_file = store / f"{oid}.txt"
                self.assertTrue(token_file.is_file(), f"no token at {token_file}")
                self.assertFalse((home / ".atmofab").exists())
                _violations_dir(repo, oid).mkdir(parents=True, exist_ok=True)
                _write_unauthorized_write_violation(
                    repo, oid, agent_run_id=arid, actor_role="step",
                    actual_changed_paths=["a.txt"], unauthorized_paths=["a.txt"],
                    output_refs=[], write_roots=[],
                )
                dismiss_violation(
                    repo, oid, agent_run_id=arid, dismiss_reason="benign",
                    paths=["a.txt"],
                    operator_token=token_file.read_text(encoding="utf-8").strip(),
                )

    def test_the_not_found_message_names_the_relocator_state(self) -> None:
        """A missing token is most often a shell that lost the export, not a missing init.

        The store is per-ENVIRONMENT: `dismiss-violation` is typed by hand, possibly in a
        different terminal from the one that started the run, and an export present in one
        and absent in the other sends the writer and the reader to two directories. The
        message says which state THIS shell is in, ordered ahead of "re-run init" because
        it is the cause the operator can check in one command — and because following
        "re-run init" first would mint a new token against a live run.
        """
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            store = Path(td) / "tokens"
            store.mkdir()
            # The expected fragment is the STATE SENTENCE, not the path: the store's
            # path is already in the message via `{token_path}`, so asserting on it
            # observed only the unset branch. Found by the round-2 census.
            for override, expected in (
                    (str(store),
                     (f"{hooks_common.OPERATOR_TOKENS_ROOT_ENV}={str(store)!r} "
                      "in this shell")),
                    (None,
                     f"{hooks_common.OPERATOR_TOKENS_ROOT_ENV} is unset in this shell")):
                env = {k: v for k, v in os.environ.items()
                       if k != hooks_common.OPERATOR_TOKENS_ROOT_ENV}
                if override is not None:
                    env[hooks_common.OPERATOR_TOKENS_ROOT_ENV] = override
                else:
                    env["HOME"] = str(Path(td) / "empty-home")
                with mock.patch.dict(os.environ, env, clear=True), \
                        self.assertRaises(ValueError) as ctx:
                    ort.dismiss_violation(
                        repo, "never_inited", agent_run_id="a",
                        dismiss_reason="r", paths=["a.txt"], operator_token="x")
                message = str(ctx.exception)
                self.assertIn(hooks_common.OPERATOR_TOKENS_ROOT_ENV, message)
                self.assertIn(expected, message)
                # And the OTHER state's sentence must be absent, so the two branches
                # cannot both be satisfied by one wording.
                other = ("is unset in this shell" if override is not None
                         else f"{hooks_common.OPERATOR_TOKENS_ROOT_ENV}=")
                self.assertNotIn(other, message)


class DirectCliImportBootstrapTests(unittest.TestCase):
    """The CLI path production actually takes imports the resolver too.

    `tools/orchestration_runtime.py` has TWO import blocks: the ordinary one and a
    fallback taken when the module is run as a script, where `sys.path[0]` is `tools/`
    rather than the repo root. The conductor's init goes through the fallback — it runs
    `python3 tools/orchestration_runtime.py init` with `cwd` set to the target checkout
    (`tools/run_workflow.py::_runtime_command`) — so a name missing from that block is a
    `NameError` in production and in no in-process test.

    Driven as a real subprocess for that reason, from a cwd that is not this checkout.
    """

    def test_init_through_the_script_entry_point_writes_to_the_relocated_store(self) -> None:
        import subprocess
        repo_root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            store = Path(td) / "tokens"
            env = {k: v for k, v in os.environ.items()
                   if k != hooks_common.OPERATOR_TOKENS_ROOT_ENV}
            env[hooks_common.OPERATOR_TOKENS_ROOT_ENV] = str(store)
            env.pop("PYTHONPATH", None)
            completed = subprocess.run(
                ["python3", str(repo_root / "tools" / "orchestration_runtime.py"), "init",
                 "--repo-root", str(repo), "--orchestration-id", "opr_cli",
                 "--spec-ref", "spec/x.yaml"],
                cwd=td, env=env, text=True, capture_output=True, check=False)
            self.assertEqual(completed.returncode, 0,
                             msg=f"{completed.stdout}\n{completed.stderr}")
            self.assertTrue((store / "opr_cli.txt").is_file())

    def test_the_script_entry_point_refuses_an_unusable_relocation(self) -> None:
        """The refusal reaches the operator through the same path, and creates nothing."""
        import subprocess
        repo_root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            env = {k: v for k, v in os.environ.items()
                   if k != hooks_common.OPERATOR_TOKENS_ROOT_ENV}
            env[hooks_common.OPERATOR_TOKENS_ROOT_ENV] = "relative/tokens"
            env.pop("PYTHONPATH", None)
            completed = subprocess.run(
                ["python3", str(repo_root / "tools" / "orchestration_runtime.py"), "init",
                 "--repo-root", str(repo), "--orchestration-id", "opr_cli2",
                 "--spec-ref", "spec/x.yaml"],
                cwd=td, env=env, text=True, capture_output=True, check=False)
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn(hooks_common.OPERATOR_TOKENS_ROOT_ENV,
                          completed.stdout + completed.stderr)
            self.assertFalse((repo / "workspace" / "orchestrations" / "opr_cli2").exists())


class OnePrivateRootTests(unittest.TestCase):
    """The three writers and the guard land in ONE root. This is issue #132 itself."""

    def test_the_three_writers_and_the_guard_resolve_one_root(self) -> None:
        """Move `$HOME` and all four follow, together.

        The four sites are `init_orchestration` (writes the operator token),
        `dismiss_violation` (reads it), `run_workflow._claim_lock_path` (writes a start
        claim) and `protected_host_read_roots` (forbids a leaf from reading any of it).
        Before this branch each of the first three built `Path.home() / ".atmofab" / …`
        for itself, so they agreed by coincidence: the #127 rename moved one and the
        others stayed. RED on `origin/main`.

        Driven through the REAL functions, not through the resolvers — pinning at the
        resolver would leave the wiring free to be deleted, which is the failure this
        repository has already had (4 of 5 sites). `$HOME` is moved by patching
        `_home_dir` in `tools.hooks.common`, the one function `operator_secret_root`
        reads, and the three overrides are cleared so every default branch is taken.

        The `~/.atmofab/homes` assertion is here for completeness of the root, not
        because this change touched it.

        Nothing else ties them.
        """
        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td) / "home"
            fake_home.mkdir()
            repo = Path(td) / "repo"
            repo.mkdir()
            seed_claude_leaf_config(repo)
            oid, arid = "opr_one", "arid-1"
            redirected = {name for name, _sub in _private_root_redirects()}
            env = {k: v for k, v in os.environ.items() if k not in redirected}
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(hooks_common, "_home_dir",
                                      return_value=fake_home):
                atmofab = (fake_home / ".atmofab").resolve()

                # WRITER 1 — the operator token.
                ort.init_orchestration(repo, oid, spec_ref="spec/x.yaml")
                token = atmofab / "operator_tokens" / f"{oid}.txt"
                self.assertTrue(token.is_file(), f"no token at {token}")
                self.assertEqual(oct(token.stat().st_mode & 0o777), "0o600")
                self.assertEqual(oct(token.parent.stat().st_mode & 0o777), "0o700")

                # READER — the same file, resolved independently.
                ort._violations_dir(repo, oid).mkdir(parents=True, exist_ok=True)
                ort._write_unauthorized_write_violation(
                    repo, oid, agent_run_id=arid, actor_role="step",
                    actual_changed_paths=["a.txt"], unauthorized_paths=["a.txt"],
                    output_refs=[], write_roots=[],
                )
                ort.dismiss_violation(
                    repo, oid, agent_run_id=arid, dismiss_reason="benign",
                    paths=["a.txt"],
                    operator_token=token.read_text(encoding="utf-8").strip(),
                )

                # WRITER 2 — the start claim.
                self.assertEqual(
                    run_workflow._claim_lock_path(repo, "spec", "spec/x").parent,
                    atmofab / "start_claims")

                # WRITER 3 — the isolated homes root.
                self.assertEqual(ort._workflow_homes_root(), atmofab / "homes")

                # THE GUARD — both entries, so a Bash read of either fails closed.
                roots = hooks_common.protected_host_read_roots()
                self.assertIn(atmofab, roots)
                self.assertIn(atmofab / "operator_tokens", roots)

    def test_the_dot_atmofab_constant_is_spelled_once(self) -> None:
        """`".atmofab"` appears in exactly one function across `tools/` and `mcp_servers/`.

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
        found = set()
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
                found |= self._atmofab_constants(tree, rel)
        # THE RULE IS "ONCE, IN THE MODULE THAT OWNS THE RESOLVERS", not "inside that
        # one function". The first version pinned the function name, which refused a
        # change that STRENGTHENS the property — hoisting the literal to a module-level
        # constant in the same file — and told the author to reach the location through
        # `operator_secret_root()`, which the code was already doing. Found by the round-2
        # census aiming the over-refusal question at the instrument.
        self.assertEqual(
            {rel for rel, _fn in found}, {"tools/hooks/common.py"},
            "`.atmofab` is spelled outside the module that owns the resolvers. Reach the "
            "location through `operator_secret_root()` / `operator_tokens_root()` / "
            "`workflow_homes_root()` / `run_workflow._start_claims_root()` instead "
            "(issue #132). If a NEW site genuinely has to spell the literal — a migration "
            "tool for a legacy tree is the plausible case — this assertion is where that "
            "decision gets recorded: widen it here, with the reason, rather than leaving "
            f"the rule stated in prose alone. Sites found: {sorted(found)}")
        self.assertEqual(
            len(found), 1,
            "the literal is spelled more than once inside tools/hooks/common.py. One "
            "spelling is the rule; where in the file it lives is not, so a module-level "
            f"constant is fine and a second copy is not. Sites: {sorted(found)}")

    @staticmethod
    def _atmofab_constants(tree: ast.AST, rel: str) -> set[tuple[str, str]]:
        """`(relative path, enclosing function)` for every bare `".atmofab"` constant."""
        out: set[tuple[str, str]] = set()

        def walk(node, enclosing: str) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    walk(child, child.name)
                    continue
                if isinstance(child, ast.Constant) and child.value == ".atmofab":
                    out.add((rel, enclosing))
                walk(child, enclosing)

        walk(tree, "<module>")
        return out

    def test_the_constant_reader_sees_a_synthetic_spelling(self) -> None:
        """Self-test for the bound above: an empty walk must not be able to pass it.

        Both halves — that the reader FINDS a plain `Path.home() / ".atmofab" / "x"` and
        that it reports the enclosing function rather than the module — because a reader
        that returned `<module>` for everything would make the assertion above trivially
        satisfiable by moving the spelling into a function.
        """
        source = (
            "from pathlib import Path\n"
            "def somewhere():\n"
            "    return Path.home() / '.atmofab' / 'x'\n"
            "TOP = Path.home() / '.atmofab'\n"
        )
        self.assertEqual(
            OnePrivateRootTests._atmofab_constants(ast.parse(source), "probe.py"),
            {("probe.py", "somewhere"), ("probe.py", "<module>")})


class RunbookStatesTheRelocatorsTests(unittest.TestCase):
    """Rule 3-a: couple the operator-facing document to the constants in the code.

    The relocators are stated in more than three places — `docs/RUNBOOK.md` at the
    inventory table, the hook-recovery row and the dismiss recipe; `docs/HOOKS.md` twice;
    and the module comments in `orchestration_runtime`, `run_workflow` and
    `hooks/common`. Three or more statement sites is where
    `.claude/skills/atmofab-enforcement-change` rule 3-a says discipline has already
    lost, and the site an OPERATOR reads is the one to check first — a RUNBOOK that
    names two of three relocators sends them to a shell without the export that decides
    where their token is.

    Coupled by MEMBERS, because the RUNBOOK names them in full, and the members are
    resolved FROM THE CODE (`_private_root_redirects`, itself built from the three
    constants) rather than transcribed here — so a rename moves both sides and this test
    is not a second place the rule is spelled.

    Two of rule 3-a's traps are answered explicitly. The ANCHOR is the section heading,
    which precedes every sentence this checks and is byte-identical at `e0bae3d`, so it
    pins that the rule is stated rather than that a correction survived. The READER is
    BOUNDED to that section and the bound is self-tested below, or a document that
    mentions `ATMOFAB_START_CLAIM_ROOT` anywhere — it does, in the cold-start-guard
    bullet 90 lines earlier — would satisfy this on the strength of an unrelated
    sentence.

    NOT coupled: `docs/HOOKS.md`, the module comments, and the `--operator-token` help
    text. Naming the document a leaf never reads and the strings a test cannot reach
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
            "advisory `flock` under `~/.atmofab/start_claims/`" in text,
            "the cold-start bullet this bound is tested against has moved or was "
            "reworded; re-choose a control sentence that names a relocator and sits "
            "OUTSIDE the operator-private-root section")
        self.assertFalse(
            "advisory `flock` under `~/.atmofab/start_claims/`" in section,
            "the section slice reached outside the section — the bound is broken")


if __name__ == "__main__":  # pragma: no cover - manual runs
    unittest.main()
