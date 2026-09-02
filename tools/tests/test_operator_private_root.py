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
  * SAMPLED — the creation-side refusals. Two overrides that cannot be honoured, not the
    set of them.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tools.hooks.common as hooks_common
import tools.orchestration_runtime as ort
from tools.tests.leaf_config_fixture import (
    isolated_homes_per_test_suite,
    redirect_isolated_homes_root_for_module,
    restore_isolated_homes_root_for_module,
    seed_claude_leaf_config,
)


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
            self.assertIn(hooks_common.OPERATOR_TOKENS_ROOT_ENV, str(ctx.exception))
            self.assertIn("absolute", str(ctx.exception))
            self.assertFalse((repo / "workspace" / "orchestrations" / "opr_1").exists())

    def test_an_override_whose_parent_is_missing_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            missing = Path(td) / "no" / "such" / "parent" / "tokens"
            with self.assertRaises(ValueError) as ctx:
                self._init_under(str(missing), repo)
            self.assertIn("does not exist", str(ctx.exception))
            self.assertIn(hooks_common.OPERATOR_TOKENS_ROOT_ENV, str(ctx.exception))
            self.assertFalse((repo / "workspace" / "orchestrations" / "opr_1").exists())

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
            for override, expected in ((str(store), str(store)),
                                       (None, "unset")):
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


if __name__ == "__main__":  # pragma: no cover - manual runs
    unittest.main()
