"""Z4 (issue #171): the PURE leaf is the only leaf model, pinned at the mechanism.

The deletions of PR-1 remove the agentic leaf loop and everything only it reached. What has
to survive them is not any one branch but two properties of the conductor as a whole, and both
are stated here rather than inside the 21k-line `test_workflow_conductor.py`, so that a reader
asking "what still holds after the agentic loop went" has one file to open. (The plan for this
issue placed them in `test_workflow_conductor.py`; they moved because they need the M3c node
fixture that `test_pure_leaf_producer.py` owns, and importing that into the conductor suite
would couple two large files for two tests.)

1. **Every LLM launch is pure.** For every declared provider, every `(phase, substep)` in
   `LLM_LEAF_SUBSTEPS`, and every bundle shape, `run_substep` dispatches into one of the two
   pure loops — and on a node with NO shape the two `generate` pairs fail CLOSED instead of
   falling through to a loop that no longer exists.
2. **Nothing launches outside a read-only sandbox.** `spawn_leaf` refuses a profile that is
   not `readonly`, so "the leaf runs confined with no write authority" is enforced at the
   spawn site rather than inferred from the fact that only pure callers remain.

Both are whole-mechanism pins: reverting the dispatch to a constant, or dropping the readonly
guard, is what they are written to catch.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import tools.llm_config as lc
import tools.workflow_conductor as wc
from tools.tests.test_pure_leaf_producer import _PureFakeConductor, _write_node

# One block per declared provider, in the `capabilities`-free form an operator writes. The set
# is compared against `PROVIDER_CAPABILITIES` in the test, so a provider added to the product
# without a row here is a failure rather than a silently narrower sweep (`no vendor lock-in`,
# `AGENTS.md` §Development premises).
_PROVIDER_BLOCKS = {
    "claude_cli": "        provider: claude_cli\n        model: opus\n",
    "codex_cli": "        provider: codex_cli\n        model: gpt-5.6-sol\n",
    "openai_compatible": (
        "        provider: openai_compatible\n"
        "        base_url: http://localhost:8000/v1\n"
        "        api_key_env: ATMOFAB_TEST_KEY\n        model: local-model\n"),
    "anthropic_api": (
        "        provider: anthropic_api\n"
        "        api_key_env: ATMOFAB_TEST_KEY\n        model: claude-opus-5\n"),
}


def _config_on(provider: str, tmp: Path) -> lc.LlmConfig:
    """A configuration putting `provider` on every LLM leaf."""
    block = _PROVIDER_BLOCKS[provider]
    text = "defaults:\n  provider: claude_cli\n  model: opus\nphases:\n"
    for phase in sorted({p for p, _ in lc.LLM_LEAF_SUBSTEPS}):
        text += f"  {phase}:\n    substeps:\n"
        for p, substep in sorted(lc.LLM_LEAF_SUBSTEPS):
            if p == phase:
                text += f"      {substep}:\n" + block
    path = tmp / f"llm_{provider}.yaml"
    path.write_text(text, encoding="utf-8")
    return lc.load_llm_config(path)


class _DispatchProbe(_PureFakeConductor):
    """Records which loop `run_substep` chose, and refuses to launch anything."""

    def _run_pure_producer_substep(self, refs, phase, substep, *a, **kw):  # type: ignore[override]
        self.__dict__.setdefault("dispatch", []).append(("producer", phase, substep))
        return wc.SubstepOutcome("child-p", "pass", [], 0, None, 1)

    def _run_pure_reviewer_substep(self, refs, phase, substep, *a, **kw):  # type: ignore[override]
        self.__dict__.setdefault("dispatch", []).append(("reviewer", phase, substep))
        return wc.SubstepOutcome("child-r", "pass", [], 0, None, 1)

    def spawn_leaf(self, *a, **kw):  # type: ignore[override]
        raise AssertionError("spawn_leaf reached: an LLM leaf launched outside the pure loops")

    def _run_deterministic_substep(self, refs, phase, substep, child_arid, request):  # type: ignore[override]
        raise AssertionError(
            f"deterministic body reached for the LLM leaf {phase}.{substep}")


class EveryLlmLaunchIsPureTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        self.refs = _write_node(self.repo)

    def _conductor(self, provider: str, shape: str | None) -> _DispatchProbe:
        c = _DispatchProbe(
            repo_root=self.repo, orchestration_id="o", orchestration_agent_run_id="orch",
            env={"ATMOFAB_TEST_KEY": "sk-test"},
            llm_config=_config_on(provider, self.repo))
        # The shape is stubbed rather than built from an IR per shape: what this row is about
        # is the DISPATCH's dependence on the shape, and `_bundle_shape`'s own answer for each
        # node kind is pinned where that function lives.
        c._bundle_shape = lambda refs, _s=shape: _s        # type: ignore[assignment]
        return c

    def test_every_provider_and_pair_dispatches_into_a_pure_loop(self) -> None:
        self.assertEqual(set(_PROVIDER_BLOCKS), set(lc.PROVIDER_CAPABILITIES))
        for provider in sorted(_PROVIDER_BLOCKS):
            for shape in ("m3c", "harness"):
                for phase, substep in sorted(lc.LLM_LEAF_SUBSTEPS):
                    c = self._conductor(provider, shape)
                    outcome = c.run_substep(self.refs, phase, substep)
                    self.assertEqual(outcome.status, "pass",
                                     msg=f"{provider} {shape} {phase}.{substep}")
                    expect = "reviewer" if substep in ("verify", "judge") else "producer"
                    self.assertEqual(c.__dict__.get("dispatch"),
                                     [(expect, phase, substep)],
                                     msg=f"{provider} {shape} {phase}.{substep}")

    def test_a_shapeless_node_fails_the_generate_pairs_closed(self) -> None:
        """With no bundle shape there is no pure path for the two `generate` pairs, and there
        is no longer a second loop to fall through to. The three shape-free pairs are
        unaffected — they carry no shape condition — so this row asserts the split, not just
        the refusal."""
        for provider in sorted(_PROVIDER_BLOCKS):
            for phase, substep in sorted(lc.LLM_LEAF_SUBSTEPS):
                c = self._conductor(provider, None)
                outcome = c.run_substep(self.refs, phase, substep)
                if phase == "generate":
                    self.assertEqual(outcome.status, "fail",
                                     msg=f"{provider} {phase}.{substep}")
                    assert outcome.infra_error is not None
                    self.assertEqual(outcome.infra_error[0], "node_has_no_bundle_shape",
                                     msg=f"{provider} {phase}.{substep}")
                    self.assertEqual(c.__dict__.get("dispatch"), None)
                else:
                    self.assertEqual(outcome.status, "pass",
                                     msg=f"{provider} {phase}.{substep}")

    def test_the_refusal_does_not_name_a_transport_that_no_longer_exists(self) -> None:
        """An operator acts on this text. Until Z4 it told them to "configure an agentic
        provider for this substep", which is now advice no configuration can take."""
        c = self._conductor("claude_cli", None)
        outcome = c.run_substep(self.refs, "generate", "generate")
        assert outcome.infra_error is not None
        detail = outcome.infra_error[1]
        self.assertNotIn("agentic", detail)
        # It still names both inputs the shape predicate reads, so the operator knows where
        # to look (carried from `pure_only_provider_on_agentic_path`, which it replaces).
        self.assertIn("node_key", detail)
        self.assertIn("CodegenBundle shape", detail)


class SpawnLeafRequiresAReadonlyProfileTests(unittest.TestCase):
    """`spawn_leaf` launches nothing outside a read-only sandbox.

    Pinned at the spawn site rather than at the callers: after Z4 the only CLI callers are the
    two pure loops, and a guard that rests on that fact would go quiet the moment a sixth
    caller appears. The HTTP providers return before this — they launch no process at all —
    which is asserted here too so that "every entry is refused" cannot be read into it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)

    def _conductor(self) -> wc.Conductor:
        tmp = self.repo
        return _PureFakeConductor(
            repo_root=tmp, orchestration_id="o", orchestration_agent_run_id="orch",
            env={"ATMOFAB_TEST_KEY": "sk-test"},
            llm_config=_config_on("claude_cli", tmp))

    def _write_profile(self, child_arid: str, readonly: bool) -> None:
        d = self.repo / "workspace" / "orchestrations" / "o" / "sandbox_profiles"
        d.mkdir(parents=True, exist_ok=True)
        import json
        (d / f"{child_arid}.json").write_text(
            json.dumps({"orchestration_id": "o", "agent_run_id": child_arid,
                        "sandbox_runtime": "bwrap", "repo_root": str(self.repo),
                        "tmp_dir": str(self.repo / "tmp"), "readonly": readonly}),
            encoding="utf-8")

    def test_a_writable_profile_is_refused(self) -> None:
        c = self._conductor()
        self._write_profile("child-rw", readonly=False)
        with self.assertRaises(wc.SandboxEnforcementError) as ctx:
            wc.Conductor.spawn_leaf(c, "PROMPT", {}, c.entry_for("generate", "generate"),
                                    child_arid="child-rw")
        self.assertIn("read-only", str(ctx.exception))

    def test_a_missing_profile_is_refused(self) -> None:
        c = self._conductor()
        with self.assertRaises(wc.SandboxEnforcementError):
            wc.Conductor.spawn_leaf(c, "PROMPT", {}, c.entry_for("generate", "generate"),
                                    child_arid="child-none")

    def test_an_http_entry_launches_no_process_and_is_not_gated_on_a_profile(self) -> None:
        c = _PureFakeConductor(
            repo_root=self.repo, orchestration_id="o", orchestration_agent_run_id="orch",
            env={"ATMOFAB_TEST_KEY": "sk-test"},
            llm_config=_config_on("anthropic_api", self.repo))
        entry = c.entry_for("generate", "generate")
        self.assertTrue(entry.is_http)
        seen: list[str] = []
        c._run_http_leaf = (                                # type: ignore[assignment]
            lambda *a, **kw: seen.append(kw["child_arid"]) or wc.ProcResult(0, "{}", ""))
        wc.Conductor.spawn_leaf(c, "PROMPT", {}, entry, child_arid="child-http")
        self.assertEqual(seen, ["child-http"])


class CodexLeafHasNoHookLayerTests(unittest.TestCase):
    """Z4: the codex leaf's confinement is the sandbox, not a hook.

    Until Z4 a codex leaf carried an in-sandbox `PreToolUse`/`PostToolUse` hook layer, and
    with it three preflight checks, a host-certified feature cache, a hooks file copied into
    the private CODEX_HOME, and `--dangerously-bypass-hook-trust` on both `exec` subcommands.
    What confines the pure codex leaf now is the read-only bwrap profile plus `--sandbox
    read-only` / `sandbox_mode="read-only"` and `--output-schema`; the private home's
    `config.toml` untrusted marker STAYS, because it is what keeps this checkout's dev-layer
    `.codex/hooks.json` from joining the leaf's hook set at all."""

    def _entry(self) -> lc.ResolvedLeafEntry:
        return lc.ResolvedLeafEntry(
            provider="codex_cli", model="gpt-5.6-sol",
            capabilities=lc.PROVIDER_CAPABILITIES["codex_cli"])

    def test_neither_codex_subcommand_carries_a_hook_trust_bypass(self) -> None:
        import tempfile as _tf
        with _tf.TemporaryDirectory() as td:
            repo = Path(td)
            c = _PureFakeConductor(
                repo_root=repo, orchestration_id="o", orchestration_agent_run_id="orch",
                env={}, llm_config=_config_on("codex_cli", repo))
            entry = c.entry_for("generate", "generate")
            cold = c.leaf_command(entry, session_id="child-1", pure=True)
            warm = c.leaf_command(entry, session_id="child-1",
                                  resume_session_id="thread-1", pure=True)
        for argv in (cold, warm):
            self.assertNotIn("--dangerously-bypass-hook-trust", argv)
        # The read-only policy is what replaced it, on BOTH subcommands — `exec resume`
        # takes no `--sandbox`, so it is re-pinned through the shared `--config` override.
        self.assertIn("read-only", cold)
        self.assertIn('sandbox_mode="read-only"', warm)

    def test_the_preflight_declares_no_codex_hook_checks(self) -> None:
        from tools.orchestration_runtime import (
            CODEX_EXEC_RESUME_REQUIRED_FLAGS,
            CODEX_REQUIRED_LAUNCH_CHECKS,
        )
        for name in ("hooks_enabled", "codex_project_hooks_validated",
                     "codex_project_hook_trust_bypass"):
            self.assertNotIn(name, CODEX_REQUIRED_LAUNCH_CHECKS)
        self.assertNotIn("--dangerously-bypass-hook-trust",
                         CODEX_EXEC_RESUME_REQUIRED_FLAGS)

    def test_the_private_codex_home_carries_the_marker_and_no_hooks(self) -> None:
        """The home keeps `config.toml` (the untrusted marker) and `auth.json`, and no longer
        carries `hooks.json` — nor a `hooks_sha256` for a file that does not exist."""
        import os
        import tempfile as _tf

        from tools.orchestration_runtime import (
            _prepare_codex_workflow_home,
            init_orchestration,
        )
        with _tf.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            codex_home = Path(td) / "codex_home"
            codex_home.mkdir()
            (codex_home / "auth.json").write_text("{}", encoding="utf-8")
            init_orchestration(
                repo_root=repo, orchestration_id="orch_z4",
                spec_ref="spec/problem/shallow_water2d/controlled_spec.md",
                source_dependency_ref="spec/problem/shallow_water2d/deps.yaml",
            )
            prev = os.environ.get("CODEX_HOME")
            os.environ["CODEX_HOME"] = str(codex_home)
            try:
                iso = _prepare_codex_workflow_home(repo, "orch_z4")
            finally:
                if prev is None:
                    os.environ.pop("CODEX_HOME", None)
                else:
                    os.environ["CODEX_HOME"] = prev
            home = Path(iso["home"])
            self.addCleanup(__import__("shutil").rmtree, home, True)
            self.assertTrue((home / "config.toml").is_file())
            self.assertIn("untrusted", (home / "config.toml").read_text(encoding="utf-8"))
            self.assertFalse((home / "hooks.json").exists())
            self.assertNotIn("hooks", iso)
            self.assertNotIn("hooks_sha256", iso)
