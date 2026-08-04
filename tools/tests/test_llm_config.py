"""Unit tests for the per-substep leaf-LLM configuration (`tools/llm_config.py`, issue #28).

Three things are verified here, in descending order of how expensive it would be to get them
wrong:

1. **The samples load.** `docs/examples/llm_*.example.yaml` are what an operator copies to the
   `./llm.yaml` that `run_workflow.py` reads when nobody passes `--llm-config`, and an untested
   YAML file in this repository has a documented history of drifting silently. Loading them here
   is the condition under which YAML was chosen as the format — and the reason these particular
   `docs/examples` files are not part of that history.
2. **Every named rejection rule fires, exactly once, on its own input.** The rule name is the
   operator's search key, so a rule that silently changed name (or was shadowed by an earlier
   check) is a real regression.
3. **The mirror tables still mirror.** `LLM_LEAF_SUBSTEPS` / `PURE_CAPABLE_SUBSTEPS` /
   `MCP_REQUIRED_LLM_SUBSTEPS` are copies of facts owned by the conductor and the runtime.
   Each guard derives the original BEHAVIORALLY (running the conductor predicate, reading the
   runtime table) rather than re-asserting the same literal, so moving the original reds the
   test instead of leaving two agreeing-but-wrong copies.
"""

from __future__ import annotations

import re
import tempfile
import unittest

import yaml
from pathlib import Path

from tools import llm_config as lc
from tools import orchestration_runtime as ort
from tools import workflow_conductor as wc

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class _Tmp(unittest.TestCase):
    """Base class giving each test a scratch directory and a `write()` helper."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)

    def write(self, text: str, name: str = "llm.yaml") -> Path:
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def assert_rule(self, rule: str, text: str) -> lc.LlmConfigError:
        with self.assertRaises(lc.LlmConfigError) as ctx:
            lc.load_llm_config(self.write(text))
        self.assertEqual(ctx.exception.rule, rule, msg=str(ctx.exception))
        return ctx.exception


SAMPLE_DIR = REPO_ROOT / "docs" / "examples"


def _discovered_samples() -> dict[str, Path]:
    """Every leaf-LLM sample on disk, DISCOVERED rather than listed.

    A sample nobody loads is the drift this file exists to prevent, so a new one must be picked
    up without editing the test — and `test_the_samples_are_the_documented_four` separately
    pins the set, so a deletion or a rename still reds a test."""
    return {p.name: p for p in sorted(SAMPLE_DIR.glob("llm_*.example.yaml"))}


def _sample_body(path: Path) -> dict:
    """The sample's document with its comments stripped — what a reader without the prose sees."""
    text = path.read_text(encoding="utf-8")
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    return yaml.safe_load(body)


class SampleConfigTests(unittest.TestCase):
    """`docs/examples/llm_*.example.yaml` are the files an operator copies to `./llm.yaml`.

    An untested YAML file in this repository has a documented history of drifting silently
    (`docs/examples/*.yaml`), and these are the documents that decide which model runs each
    leaf. Loading them here is the condition under which YAML was chosen as the format."""

    def test_the_samples_are_the_documented_four(self) -> None:
        self.assertEqual(set(_discovered_samples()), set(lc.SAMPLE_CONFIG_NAMES))
        self.assertEqual(lc.SAMPLE_CONFIG_DIR, "docs/examples")

    def test_every_sample_loads_resolves_every_leaf_and_is_runnable(self) -> None:
        for name, path in _discovered_samples().items():
            cfg = lc.load_llm_config(path)
            self.assertEqual(sorted(cfg.entries), sorted(lc.LLM_LEAF_SUBSTEPS), msg=name)
            cfg.validate_runnable()

    def test_every_sample_spells_out_every_leaf(self) -> None:
        """The samples answer "what runs where, and how hard does it think" by being READ.
        Inheritance is legal and right once the copy is the operator's own; it is the wrong
        choice for the sample, where a reader would have to re-derive the answer.

        `effort` is required only where the entry's provider HAS one: `anthropic_api` expresses
        the same idea as a thinking token budget, and writing a level there is rejected."""
        for name, path in _discovered_samples().items():
            document = _sample_body(path)
            declared = {
                (phase, substep)
                for phase, phase_doc in (document.get("phases") or {}).items()
                for substep in ((phase_doc or {}).get("substeps") or {})
            }
            self.assertEqual(declared, set(lc.LLM_LEAF_SUBSTEPS), msg=name)
            for phase, phase_doc in document["phases"].items():
                for substep, entry in phase_doc["substeps"].items():
                    where = f"{name} {phase}.{substep}"
                    self.assertIn("provider", entry, msg=where)
                    inapplicable = lc._FIELDS_NOT_APPLICABLE.get(entry["provider"], frozenset())
                    for field in ("model", "effort"):
                        if field in inapplicable:
                            self.assertNotIn(field, entry, msg=where)
                            continue
                        self.assertIn(field, entry, msg=where)

    def test_the_claude_sample_is_uniform_and_names_unpinned_aliases(self) -> None:
        cfg = lc.load_llm_config(SAMPLE_DIR / "llm_claude.example.yaml")
        self.assertEqual(cfg.providers, frozenset({"claude_cli"}))
        self.assertTrue(cfg.is_uniform)
        for (phase, substep), entry in cfg.entries.items():
            self.assertEqual(entry.backend_token, "claude")
            # An unpinned ALIAS, never a version — versions move, and the exact one that ran is
            # recovered from the transcript afterwards.
            self.assertNotRegex(entry.model, r"-\d+-\d+$")
            self.assertIn(entry.model, ("opus", "sonnet"), msg=f"{phase}.{substep}")

    def test_the_codex_sample_carries_an_explicit_slug(self) -> None:
        """Codex has no alias to resolve, so every launch must carry a slug. The sample names
        one rather than failing at run start; blanking it still trips the rule."""
        cfg = lc.load_llm_config(SAMPLE_DIR / "llm_codex.example.yaml")
        self.assertEqual(cfg.providers, frozenset({"codex_cli"}))
        for entry in cfg.entries.values():
            self.assertTrue(entry.model)
            self.assertNotEqual(entry.model.lower(), "codex")

    def test_the_cli_samples_economise_only_the_leaf_the_measurements_allow(self) -> None:
        """Two separate claims, and the difference between them is the point.

        EFFORT is uniform at the current generation's own default. Both CLIs default to
        `medium`, and Codex lowered it from the `xhigh` its predecessor gpt-5.5 shipped, so the
        vendor moved the default down as the model improved. Overriding that needs evidence
        about the model that will run, and the repository's re-run measurements are not it: they
        were recorded almost entirely on Opus 4.8, and a re-run rate belongs to the
        (model, effort, task) triple.

        MODEL is where the one economy goes, because the argument for it is structural rather
        than generational: re-runs were ~20% of every token spent and concentrated in the leaves
        that WRITE, and `generate.generate` was seen at 7 attempts against a
        `MAX_ATTEMPTS_PER_PHASE` of 3 — where crossing the cap fails the phase rather than
        costing one more attempt. So the writing leaves are the last to economise on any model,
        and `compile.verify` is the safe one: smallest, re-ran in 3 of 93 nodes, reads an IR the
        deterministic `compile.static` gate already proved clean, and re-read downstream by two
        other leaves. `validate.judge` is NOT economised despite never being re-run once in 69
        nodes, because nothing follows it to catch what it misses.

        The mixed HTTP samples are out of scope: their `generate` leaves run a different
        provider's model entirely, so "same model as generate.generate" says nothing there."""
        for name in ("llm_claude.example.yaml", "llm_codex.example.yaml"):
            cfg = lc.load_llm_config(SAMPLE_DIR / name)
            self.assertLessEqual(cfg.providers, lc.CLI_PROVIDERS, msg=name)
            top = cfg.entry_for("generate", "generate")
            self.assertEqual(top.effort, "medium", msg=name)
            for (phase, substep), entry in cfg.entries.items():
                where = f"{name} {phase}.{substep}"
                self.assertEqual(entry.effort, top.effort, msg=where)
                if (phase, substep) == ("compile", "verify"):
                    self.assertNotEqual(entry.model, top.model,
                                        msg=f"{where} is the leaf meant to be economised")
                    continue
                self.assertEqual(entry.model, top.model, msg=where)

    def test_the_http_samples_put_the_http_provider_on_exactly_the_two_pure_leaves(self) -> None:
        """The scope rule the HTTP samples exist to demonstrate. An HTTP provider anywhere else
        does not load at all (`llm_config_capability_insufficient_for_substep`, covered by
        `CapabilityTests`); what is checked here is that the samples USE the whole admissible
        surface rather than demonstrating one leaf and leaving the other agentic."""
        for name in ("llm_openai_compatible.example.yaml", "llm_anthropic_api.example.yaml"):
            cfg = lc.load_llm_config(SAMPLE_DIR / name)
            provider = name[len("llm_"):-len(".example.yaml")]
            self.assertIn(provider, lc.HTTP_PROVIDERS, msg=name)
            on_http = {key for key, entry in cfg.entries.items() if entry.is_http}
            self.assertEqual(on_http, set(lc.PURE_CAPABLE_SUBSTEPS), msg=name)
            for key in on_http:
                self.assertEqual(cfg.entries[key].provider, provider, msg=f"{name} {key}")
            # ...and `defaults` stays agentic, which is what runs the `escalate` diagnostician.
            self.assertFalse(cfg.defaults.is_http, msg=name)

    def test_a_codex_config_without_a_model_still_stops_before_launching(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.yaml"
            path.write_text("defaults:\n  provider: codex_cli\n", encoding="utf-8")
            with self.assertRaises(lc.LlmConfigError) as ctx:
                lc.load_llm_config(path).validate_runnable()
        self.assertEqual(ctx.exception.rule, "llm_config_codex_cli_requires_model")


class DefaultConfigPathTests(unittest.TestCase):
    def test_the_default_is_repo_root_slash_llm_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(lc.default_config_path(tmp), Path(tmp) / "llm.yaml")
            self.assertEqual(lc.DEFAULT_CONFIG_FILENAME, "llm.yaml")

    def test_resolving_returns_the_file_when_it_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "llm.yaml"
            path.write_text("defaults:\n  provider: claude_cli\n", encoding="utf-8")
            self.assertEqual(lc.resolve_default_config_path(tmp), path)

    def test_a_missing_default_names_the_path_and_the_command_that_creates_it(self) -> None:
        """There is deliberately no fallback: a run resolved from a file nobody chose is a run
        nobody can reproduce. The refusal has to carry the fix, because the operator's next
        question is "then where do I get one"."""
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(lc.LlmConfigError) as ctx:
                lc.resolve_default_config_path(tmp)
        self.assertEqual(ctx.exception.rule, "llm_config_default_missing")
        message = str(ctx.exception)
        self.assertIn(str(Path(tmp) / "llm.yaml"), message)
        for name in lc.SAMPLE_CONFIG_NAMES:
            self.assertIn(f"cp docs/examples/{name} llm.yaml", message)
            self.assertTrue((SAMPLE_DIR / name).exists(), msg=name)



class ResolutionTests(_Tmp):
    def test_every_llm_leaf_gets_an_entry_from_bare_defaults(self) -> None:
        cfg = lc.load_llm_config(self.write("defaults:\n  provider: claude_cli\n"))
        self.assertEqual(set(cfg.entries), set(lc.LLM_LEAF_SUBSTEPS))

    def test_phase_layer_overrides_defaults_and_substep_overrides_phase(self) -> None:
        cfg = lc.load_llm_config(self.write(
            "defaults:\n"
            "  provider: claude_cli\n"
            "  model: default-model\n"
            "phases:\n"
            "  generate:\n"
            "    model: phase-model\n"
            "    substeps:\n"
            "      verify:\n"
            "        model: substep-model\n"
        ))
        self.assertEqual(cfg.entry_for("compile", "verify").model, "default-model")
        self.assertEqual(cfg.entry_for("generate", "generate").model, "phase-model")
        self.assertEqual(cfg.entry_for("generate", "verify").model, "substep-model")

    def test_unmentioned_fields_inherit_per_field(self) -> None:
        cfg = lc.load_llm_config(self.write(
            "defaults:\n"
            "  provider: claude_cli\n"
            "  model: default-model\n"
            "  max_output_tokens: 4096\n"
            "phases:\n"
            "  generate:\n"
            "    substeps:\n"
            "      verify:\n"
            "        model: substep-model\n"
        ))
        entry = cfg.entry_for("generate", "verify")
        self.assertEqual(entry.model, "substep-model")
        self.assertEqual(entry.max_output_tokens, 4096)

    def test_provider_switch_drops_inherited_provider_scoped_fields(self) -> None:
        """A deeper level that switches provider must not inherit the outer provider's
        model / command: they name things that only exist on the outer transport."""
        cfg = lc.load_llm_config(self.write(
            "defaults:\n"
            "  provider: claude_cli\n"
            "  model: claude-model\n"
            "  command: claude --flag\n"
            "  max_output_tokens: 4096\n"
            "phases:\n"
            "  generate:\n"
            "    substeps:\n"
            "      generate:\n"
            "        provider: openai_compatible\n"
            "        base_url: http://localhost:8000/v1\n"
            "        api_key_env: LOCAL_KEY\n"
            "        model: local-model\n"
        ))
        entry = cfg.entry_for("generate", "generate")
        self.assertEqual(entry.provider, "openai_compatible")
        self.assertEqual(entry.model, "local-model")
        self.assertEqual(entry.command, "")           # dropped, not inherited
        # Transport-neutral budgets DO inherit across a provider switch.
        self.assertEqual(entry.max_output_tokens, 4096)
        self.assertEqual(cfg.entry_for("generate", "verify").command, "claude --flag")

    def test_switching_back_to_the_defaults_provider_re_inherits_from_it(self) -> None:
        """The other half of the drop rule, and the half a pairwise fold gets wrong: once a
        phase switches provider, a substep switching BACK must re-inherit from the levels that
        share its provider — not start from an empty provider surface. Folding pairwise loses
        the operator's `defaults.command` wrapper on exactly that one leaf, silently."""
        cfg = lc.load_llm_config(self.write(
            "defaults:\n"
            "  provider: claude_cli\n"
            "  model: claude-model\n"
            "  command: wrapper-claude --sandbox\n"
            "phases:\n"
            "  generate:\n"
            "    provider: openai_compatible\n"
            "    base_url: http://localhost:8000/v1\n"
            "    api_key_env: LOCAL_KEY\n"
            "    model: local-model\n"
            "    substeps:\n"
            "      verify:\n"
            "        provider: claude_cli\n"
        ))
        back = cfg.entry_for("generate", "verify")
        self.assertEqual(back.provider, "claude_cli")
        self.assertEqual(back.model, "claude-model")
        self.assertEqual(back.command, "wrapper-claude --sandbox")
        # ...and the level that switched away still contributes nothing provider-scoped.
        self.assertEqual(back.base_url, "")
        self.assertEqual(back.api_key_env, "")
        self.assertEqual(cfg.entry_for("generate", "generate").command, "")

    def test_a_transport_neutral_field_inherits_into_a_provider_that_ignores_it(self) -> None:
        """`max_output_tokens` reaches only the claude transport and `timeout_s` only the HTTP
        one, but both inherit across a provider switch by design. Rejecting an INHERITED one
        made "claude everywhere with a raised ceiling, except one leaf on codex" — a core use
        case — unloadable, and pointed the operator at a key their document does not contain."""
        cfg = lc.load_llm_config(self.write(
            "defaults:\n  provider: claude_cli\n  max_output_tokens: 32000\n"
            "phases:\n  validate:\n    substeps:\n      judge:\n"
            "        provider: codex_cli\n        model: gpt-5.6-sol\n"))
        self.assertEqual(cfg.entry_for("compile", "verify").max_output_tokens, 32000)
        # Dropped where it cannot apply, so nothing downstream reads a budget that is ignored.
        self.assertIsNone(cfg.entry_for("validate", "judge").max_output_tokens)

    def test_declaring_a_non_applicable_field_on_its_own_provider_is_rejected(self) -> None:
        """The other side: a field the operator wrote FOR this provider is an error, because
        there is a key in the document to point at."""
        self.assert_rule("llm_config_field_not_applicable",
                         "defaults:\n  provider: codex_cli\n  model: m\n"
                         "  max_output_tokens: 32000\n")
        self.assert_rule("llm_config_field_not_applicable",
                         "defaults:\n  provider: claude_cli\n  timeout_s: 300\n")

    def test_provider_switch_without_a_replacement_model_is_rejected_not_inherited(self) -> None:
        """The drop is real: the outer `model` cannot satisfy the HTTP model requirement."""
        self.assert_rule("llm_config_http_requires_model",
                         "defaults:\n"
                         "  provider: claude_cli\n"
                         "  model: claude-model\n"
                         "phases:\n"
                         "  generate:\n"
                         "    substeps:\n"
                         "      generate:\n"
                         "        provider: openai_compatible\n"
                         "        base_url: http://localhost:8000/v1\n"
                         "        api_key_env: LOCAL_KEY\n")

    def test_entry_for_none_none_is_defaults(self) -> None:
        cfg = lc.load_llm_config(self.write(
            "defaults:\n  provider: claude_cli\n  model: d\n"
            "phases:\n  generate:\n    model: p\n"))
        self.assertIs(cfg.entry_for(None, None), cfg.defaults)
        self.assertEqual(cfg.entry_for(None, None).model, "d")

    def test_deterministic_substep_falls_back_to_defaults(self) -> None:
        cfg = lc.load_llm_config(self.write("defaults:\n  provider: claude_cli\n"))
        self.assertIs(cfg.entry_for("generate", "gate"), cfg.defaults)

    def test_anthropic_api_base_url_defaults_to_the_canonical_endpoint(self) -> None:
        cfg = lc.load_llm_config(self.write(
            "defaults:\n  provider: claude_cli\n"
            "phases:\n  generate:\n    substeps:\n      generate:\n"
            "        provider: anthropic_api\n"
            "        api_key_env: ANTHROPIC_API_KEY\n"
            "        model: claude-opus-5\n"))
        self.assertEqual(cfg.entry_for("generate", "generate").base_url,
                         lc.ANTHROPIC_DEFAULT_BASE_URL)

    def test_mixed_config_reports_both_providers(self) -> None:
        cfg = lc.load_llm_config(self.write(
            "defaults:\n  provider: claude_cli\n"
            "phases:\n  generate:\n    substeps:\n      generate:\n"
            "        provider: openai_compatible\n"
            "        base_url: http://localhost:8000/v1\n"
            "        api_key_env: LOCAL_KEY\n"
            "        model: local-model\n"))
        self.assertFalse(cfg.is_uniform)
        self.assertEqual(cfg.providers, frozenset({"claude_cli", "openai_compatible"}))
        cfg.validate_runnable()

    def test_provenance_map_is_per_leaf_and_includes_defaults(self) -> None:
        cfg = lc.load_llm_config(self.write(
            "defaults:\n  provider: claude_cli\n"
            "phases:\n  generate:\n    substeps:\n      generate:\n"
            "        provider: openai_compatible\n"
            "        base_url: http://localhost:8000/v1\n"
            "        api_key_env: LOCAL_KEY\n"
            "        model: local-model\n"))
        pm = cfg.provenance_map()
        self.assertEqual(set(pm), {"defaults"} | {f"{p}.{s}" for p, s in lc.LLM_LEAF_SUBSTEPS})
        self.assertEqual(pm["generate.generate"],
                         {"provider": "openai_compatible", "backend": "openai_compatible",
                          "model": "local-model", "command": "", "effort": ""})
        self.assertEqual(pm["validate.judge"]["backend"], "claude")

    def test_describe_providers_dedups_on_the_probed_surface(self) -> None:
        cfg = lc.load_llm_config(self.write(
            "defaults:\n  provider: claude_cli\n"
            "phases:\n  generate:\n    model: another-model\n"))
        self.assertEqual([r["backend"] for r in lc.describe_providers(cfg)], ["claude"])


class CapabilityTests(_Tmp):
    def test_provider_capability_table_is_the_authority(self) -> None:
        self.assertEqual(set(lc.PROVIDER_CAPABILITIES), set(lc.SUPPORTED_PROVIDERS))
        for provider, caps in lc.PROVIDER_CAPABILITIES.items():
            self.assertLessEqual(caps, lc.KNOWN_CAPABILITIES, msg=provider)
        # The user-approved scope rule, as a property of the table rather than a branch.
        for provider in lc.HTTP_PROVIDERS:
            self.assertEqual(lc.PROVIDER_CAPABILITIES[provider], frozenset({lc.CAP_PURE}))

    def test_supports_is_fail_closed_on_an_unknown_capability_name(self) -> None:
        entry = lc.ResolvedLeafEntry(provider="claude_cli",
                                     capabilities=lc.PROVIDER_CAPABILITIES["claude_cli"])
        self.assertTrue(entry.supports(lc.CAP_WARM_RESUME))
        self.assertFalse(entry.supports("warm_resumee"))
        self.assertFalse(entry.supports(""))

    def test_capabilities_may_restrict_a_provider(self) -> None:
        cfg = lc.load_llm_config(self.write(
            "defaults:\n"
            "  provider: claude_cli\n"
            "  capabilities: [agentic, pure, mcp_tools]\n"))
        entry = cfg.entry_for("validate", "judge")
        self.assertTrue(entry.supports(lc.CAP_AGENTIC))
        self.assertFalse(entry.supports(lc.CAP_WARM_RESUME))
        self.assertFalse(entry.supports(lc.CAP_USAGE_PROBE))

    def test_http_provider_on_a_pure_leaf_is_accepted(self) -> None:
        for substep in ("generate", "verify"):
            cfg = lc.load_llm_config(self.write(
                "defaults:\n  provider: claude_cli\n"
                "phases:\n  generate:\n    substeps:\n"
                f"      {substep}:\n"
                "        provider: openai_compatible\n"
                "        base_url: http://localhost:8000/v1\n"
                "        api_key_env: LOCAL_KEY\n"
                "        model: local-model\n"))
            self.assertEqual(cfg.entry_for("generate", substep).provider, "openai_compatible")

    def test_http_provider_on_each_agentic_leaf_is_rejected(self) -> None:
        agentic = sorted(lc.LLM_LEAF_SUBSTEPS - lc.PURE_CAPABLE_SUBSTEPS)
        self.assertTrue(agentic)
        for phase, substep in agentic:
            err = self.assert_rule(
                "llm_config_capability_insufficient_for_substep",
                "defaults:\n  provider: claude_cli\n"
                f"phases:\n  {phase}:\n    substeps:\n      {substep}:\n"
                "        provider: anthropic_api\n"
                "        api_key_env: ANTHROPIC_API_KEY\n"
                "        model: claude-opus-5\n")
            self.assertIn(f"{phase}.{substep}", str(err))


class RuleTests(_Tmp):
    def test_unreadable_missing_file(self) -> None:
        with self.assertRaises(lc.LlmConfigError) as ctx:
            lc.load_llm_config(self.root / "nope.yaml")
        self.assertEqual(ctx.exception.rule, "llm_config_unreadable")

    def test_unreadable_malformed_yaml(self) -> None:
        self.assert_rule("llm_config_unreadable", "defaults: [unclosed\n")

    def test_not_a_mapping_top_level(self) -> None:
        self.assert_rule("llm_config_not_a_mapping", "- provider: claude_cli\n")

    def test_not_a_mapping_empty_sequence_layers(self) -> None:
        """An empty sequence is FALSY, so `or {}` coerced it to an empty mapping and the layer
        read as "overrides nothing" — silently discarding what the operator wrote and running
        the inherited provider and model."""
        base = "defaults:\n  provider: claude_cli\n  model: base\n"
        for body in (base + "phases:\n  generate: []\n",
                     base + "phases:\n  generate:\n    substeps: []\n",
                     base + "phases:\n  generate:\n    substeps:\n      verify: []\n"):
            self.assert_rule("llm_config_not_a_mapping", body)

    def test_an_absent_layer_is_still_legitimately_empty(self) -> None:
        """`generate:` with no value is the spelling that MEANS nothing, and must keep working
        — only `None` reads as empty."""
        base = "defaults:\n  provider: claude_cli\n  model: base\n"
        for body in (base, base + "phases:\n", base + "phases:\n  generate:\n",
                     base + "phases:\n  generate:\n    substeps:\n"):
            cfg = lc.load_llm_config(self.write(body, "absent.yaml"))
            self.assertEqual(cfg.entry_for("generate", "verify").model, "base")

    def test_not_a_mapping_defaults(self) -> None:
        self.assert_rule("llm_config_not_a_mapping", "defaults: claude_cli\n")

    def test_unknown_top_level_keys_of_mixed_types(self) -> None:
        """A malformed document can mix key TYPES, and sorting those against each other raises
        a TypeError that escapes the named-rejection contract as a traceback."""
        self.assert_rule("llm_config_unknown_key",
                         "defaults:\n  provider: claude_cli\n1: x\nfoo: y\n")

    def test_unknown_top_level_key(self) -> None:
        self.assert_rule("llm_config_unknown_key",
                         "defaults:\n  provider: claude_cli\nbackends: {}\n")

    def test_unknown_entry_key(self) -> None:
        self.assert_rule("llm_config_unknown_key",
                         "defaults:\n  provider: claude_cli\n  temperature: 0.2\n")

    def test_unknown_phase(self) -> None:
        self.assert_rule("llm_config_unknown_phase",
                         "defaults:\n  provider: claude_cli\nphases:\n  generat: {}\n")

    def test_unknown_phase_names_build_explicitly(self) -> None:
        err = self.assert_rule(
            "llm_config_unknown_phase",
            "defaults:\n  provider: claude_cli\nphases:\n  build:\n    model: x\n")
        self.assertIn("deterministic", str(err))

    def test_unknown_substep_rejects_a_deterministic_one(self) -> None:
        self.assert_rule("llm_config_unknown_substep",
                         "defaults:\n  provider: claude_cli\n"
                         "phases:\n  generate:\n    substeps:\n      gate:\n        model: x\n")

    def test_unknown_substep_rejects_a_typo(self) -> None:
        self.assert_rule("llm_config_unknown_substep",
                         "defaults:\n  provider: claude_cli\n"
                         "phases:\n  validate:\n    substeps:\n      judg:\n        model: x\n")

    def test_unknown_provider(self) -> None:
        self.assert_rule("llm_config_unknown_provider", "defaults:\n  provider: gemini_cli\n")

    def test_missing_provider(self) -> None:
        self.assert_rule("llm_config_missing_provider", "defaults:\n  model: whatever\n")

    def test_missing_provider_on_an_empty_document(self) -> None:
        self.assert_rule("llm_config_missing_provider", "")

    def test_invalid_field_type(self) -> None:
        self.assert_rule("llm_config_invalid_field",
                         "defaults:\n  provider: claude_cli\n  model: 12\n")

    def test_invalid_field_non_positive_timeout(self) -> None:
        self.assert_rule("llm_config_invalid_field",
                         "defaults:\n  provider: claude_cli\n  timeout_s: 0\n")

    def test_invalid_field_effort_level_is_per_provider(self) -> None:
        """The level names are the provider's own, so this is not one enum with different
        spellings: `ultra` exists for codex and not claude, `minimal` for an OpenAI-compatible
        endpoint and for neither CLI."""
        self.assert_rule("llm_config_invalid_field",
                         "defaults:\n  provider: claude_cli\n  effort: ultra\n")
        self.assert_rule("llm_config_invalid_field",
                         "defaults:\n  provider: codex_cli\n  model: m\n  effort: minimal\n")
        self.assert_rule("llm_config_invalid_field",
                         "defaults:\n  provider: claude_cli\n  effort: enthusiastic\n")
        # `none`/`minimal` are the OpenAI API's words, not the Codex CLI's: the CLI resolves
        # against `supported_reasoning_levels`, which lists neither for any served model.
        self.assert_rule("llm_config_invalid_field",
                         "defaults:\n  provider: codex_cli\n  model: m\n  effort: none\n")
        # ...and each accepts its own.
        for body, expected in (
            ("defaults:\n  provider: claude_cli\n  effort: max\n", "max"),
            ("defaults:\n  provider: codex_cli\n  model: m\n  effort: ultra\n", "ultra"),
        ):
            cfg = lc.load_llm_config(self.write(body, "levels.yaml"))
            self.assertEqual(cfg.entry_for("validate", "judge").effort, expected)

    def test_effort_does_not_apply_to_the_anthropic_api(self) -> None:
        """Its Messages API expresses the same idea as a thinking token budget, not a level."""
        self.assert_rule("llm_config_field_not_applicable",
                         "defaults:\n  provider: claude_cli\n"
                         "phases:\n  generate:\n    substeps:\n      generate:\n"
                         "        provider: anthropic_api\n"
                         "        api_key_env: ANTHROPIC_API_KEY\n"
                         "        model: claude-opus-5\n        effort: high\n")

    def test_an_effort_does_not_inherit_across_a_provider_switch(self) -> None:
        """A level is a word in the outer provider's vocabulary; carrying it into another
        provider would inherit a level it may not have."""
        cfg = lc.load_llm_config(self.write(
            "defaults:\n  provider: claude_cli\n  effort: max\n"
            "phases:\n  generate:\n    substeps:\n      generate:\n"
            "        provider: openai_compatible\n"
            "        base_url: https://x/v1\n        api_key_env: K\n        model: m\n"))
        self.assertEqual(cfg.entry_for("generate", "generate").effort, "")
        self.assertEqual(cfg.entry_for("validate", "judge").effort, "max")

    def test_invalid_field_non_finite_timeout(self) -> None:
        """YAML reads `.nan` / `.inf` as floats and both slip past a `<= 0` test, so a config
        advertising a positive wall-clock bound would carry a deadline that never fires."""
        for value in (".nan", ".inf"):
            self.assert_rule("llm_config_invalid_field",
                             "defaults:\n  provider: claude_cli\n"
                             "phases:\n  generate:\n    substeps:\n      generate:\n"
                             "        provider: openai_compatible\n"
                             "        base_url: https://x/v1\n        api_key_env: K\n"
                             f"        model: m\n        timeout_s: {value}\n")

    def test_invalid_field_unknown_capability_name(self) -> None:
        self.assert_rule("llm_config_invalid_field",
                         "defaults:\n  provider: claude_cli\n  capabilities: [teleport]\n")

    def test_field_not_applicable_http_field_on_a_cli_provider(self) -> None:
        self.assert_rule("llm_config_field_not_applicable",
                         "defaults:\n  provider: claude_cli\n  base_url: http://x/v1\n")

    def test_field_not_applicable_command_on_an_http_provider(self) -> None:
        self.assert_rule("llm_config_field_not_applicable",
                         "defaults:\n  provider: claude_cli\n"
                         "phases:\n  generate:\n    substeps:\n      generate:\n"
                         "        provider: openai_compatible\n"
                         "        base_url: http://localhost:8000/v1\n"
                         "        api_key_env: LOCAL_KEY\n"
                         "        model: m\n"
                         "        command: vllm-wrapper\n")

    def test_http_requires_base_url(self) -> None:
        self.assert_rule("llm_config_http_requires_base_url",
                         "defaults:\n  provider: claude_cli\n"
                         "phases:\n  generate:\n    substeps:\n      generate:\n"
                         "        provider: openai_compatible\n"
                         "        api_key_env: LOCAL_KEY\n"
                         "        model: m\n")

    def test_http_requires_api_key_env(self) -> None:
        self.assert_rule("llm_config_http_requires_api_key_env",
                         "defaults:\n  provider: claude_cli\n"
                         "phases:\n  generate:\n    substeps:\n      generate:\n"
                         "        provider: openai_compatible\n"
                         "        base_url: http://localhost:8000/v1\n"
                         "        model: m\n")

    def test_http_requires_model(self) -> None:
        self.assert_rule("llm_config_http_requires_model",
                         "defaults:\n  provider: claude_cli\n"
                         "phases:\n  generate:\n    substeps:\n      generate:\n"
                         "        provider: openai_compatible\n"
                         "        base_url: http://localhost:8000/v1\n"
                         "        api_key_env: LOCAL_KEY\n")

    _HTTP = ("defaults:\n  provider: claude_cli\n"
             "phases:\n  generate:\n    substeps:\n      generate:\n"
             "        provider: openai_compatible\n        api_key_env: LOCAL_KEY\n"
             "        model: m\n        base_url: {url}\n")

    def test_insecure_base_url(self) -> None:
        """Plain http to a non-loopback host sends the API key in cleartext. The realistic case
        is a typo or a LAN address, not a deliberate choice."""
        err = self.assert_rule("llm_config_insecure_base_url",
                               self._HTTP.format(url="http://192.168.1.9:8000/v1"))
        self.assertIn("cleartext", str(err))

    def test_loopback_http_is_allowed(self) -> None:
        """Where the local servers this provider exists for run; the traffic never leaves the
        host, and requiring https there would make the headline use case impossible."""
        for url in ("http://localhost:8000/v1", "http://127.0.0.1:8000/v1",
                    "http://[::1]:8000/v1", "http://myhost.localhost:8000/v1"):
            cfg = lc.load_llm_config(self.write(self._HTTP.format(url=url), "lb.yaml"))
            self.assertEqual(cfg.entry_for("generate", "generate").base_url, url)

    def test_https_is_allowed_anywhere(self) -> None:
        cfg = lc.load_llm_config(
            self.write(self._HTTP.format(url="https://api.example.com/v1")))
        self.assertEqual(cfg.entry_for("generate", "generate").base_url,
                         "https://api.example.com/v1")

    def test_insecure_base_url_has_an_explicit_opt_in(self) -> None:
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {lc._INSECURE_BASE_URL_OPT_IN_ENV: "1"}, clear=False):
            cfg = lc.load_llm_config(
                self.write(self._HTTP.format(url="http://192.168.1.9:8000/v1")))
        self.assertEqual(cfg.entry_for("generate", "generate").base_url,
                         "http://192.168.1.9:8000/v1")

    def test_an_unhashable_mapping_key_is_a_named_rejection(self) -> None:
        """`? [a, b]` is legal YAML and produces a LIST key. Testing it for membership raised
        a bare TypeError, which escapes the startup envelope as a traceback rather than the
        named rejection every other malformed document gets."""
        self.assert_rule("llm_config_unknown_key",
                         "defaults:\n  provider: claude_cli\n? [a, b]\n: x\n")

    def test_content_parses_the_bytes_it_was_given(self) -> None:
        """One snapshot: a caller that must hash, parse and act on the same bytes cannot read
        the file twice, or the two answers can describe different versions."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.yaml"
            path.write_text("defaults:\n  provider: claude_cli\n  model: on-disk\n",
                            encoding="utf-8")
            snapshot = b"defaults:\n  provider: claude_cli\n  model: in-hand\n"
            cfg = lc.load_llm_config(path, content=snapshot)
            self.assertEqual(cfg.defaults.model, "in-hand")
            self.assertEqual(cfg.sha256, lc._sha256_bytes(snapshot))
            self.assertEqual(cfg.path, str(path))
            self.assertNotEqual(cfg.sha256, lc.config_sha256(path))

    def test_duplicate_key(self) -> None:
        """YAML keeps the last of a repeated key. This file decides which model runs each
        substep and therefore what a run costs, so a silent resolution is the failure mode a
        named rejection exists for."""
        self.assert_rule("llm_config_duplicate_key",
                         "defaults:\n  provider: claude_cli\n"
                         "phases:\n  generate:\n    model: a\n  generate:\n    model: b\n")

    def test_duplicate_substep_key(self) -> None:
        self.assert_rule("llm_config_duplicate_key",
                         "defaults:\n  provider: claude_cli\n"
                         "phases:\n  generate:\n    substeps:\n"
                         "      verify:\n        model: a\n      verify:\n        model: b\n")

    def test_duplicate_phase_after_normalization(self) -> None:
        """`generate` and `" generate "` are distinct YAML keys that name the same phase, so
        the duplicate-key loader does not see them — the later one silently replaced the
        earlier provider/model assignment."""
        self.assert_rule("llm_config_duplicate_key",
                         'defaults:\n  provider: claude_cli\n'
                         'phases:\n  generate:\n    model: a\n  " generate ":\n    model: b\n')

    def test_duplicate_substep_after_normalization(self) -> None:
        self.assert_rule("llm_config_duplicate_key",
                         'defaults:\n  provider: claude_cli\n'
                         'phases:\n  generate:\n    substeps:\n      verify:\n'
                         '        model: a\n      " verify ":\n        model: b\n')

    def test_duplicate_field_key(self) -> None:
        self.assert_rule("llm_config_duplicate_key",
                         "defaults:\n  provider: claude_cli\n  model: a\n  model: b\n")

    def test_capability_exceeds_provider(self) -> None:
        err = self.assert_rule(
            "llm_config_capability_exceeds_provider",
            "defaults:\n  provider: claude_cli\n"
            "phases:\n  generate:\n    substeps:\n      generate:\n"
            "        provider: openai_compatible\n"
            "        base_url: http://localhost:8000/v1\n"
            "        api_key_env: LOCAL_KEY\n"
            "        model: m\n"
            "        capabilities: [pure, agentic]\n")
        self.assertIn("agentic", str(err))

    def test_capability_insufficient_after_a_restriction(self) -> None:
        """The rule is about the RESOLVED capability set, not the provider name: a claude
        entry restricted to `pure` is as inadmissible on an agentic leaf as an HTTP one."""
        self.assert_rule("llm_config_capability_insufficient_for_substep",
                         "defaults:\n  provider: claude_cli\n"
                         "phases:\n  compile:\n    substeps:\n      verify:\n"
                         "        capabilities: [pure]\n")

    def test_defaults_not_agentic(self) -> None:
        err = self.assert_rule("llm_config_defaults_not_agentic",
                               "defaults:\n"
                               "  provider: anthropic_api\n"
                               "  api_key_env: ANTHROPIC_API_KEY\n"
                               "  model: claude-opus-5\n")
        self.assertIn("escalate", str(err))

    def test_defaults_not_agentic_by_restriction(self) -> None:
        self.assert_rule("llm_config_defaults_not_agentic",
                         "defaults:\n  provider: claude_cli\n  capabilities: [pure]\n")

    def test_codex_requires_model_only_at_run_start(self) -> None:
        cfg = lc.load_llm_config(self.write("defaults:\n  provider: codex_cli\n"))
        with self.assertRaises(lc.LlmConfigError) as ctx:
            cfg.validate_runnable()
        self.assertEqual(ctx.exception.rule, "llm_config_codex_cli_requires_model")
        lc.load_llm_config(self.write(
            "defaults:\n  provider: codex_cli\n  model: some-slug\n")).validate_runnable()

    def test_codex_requires_model_rejects_the_generic_alias_too(self) -> None:
        """`codex` is an alias the CLI resolves to whatever it currently prefers, so a leaf
        launched with it records provenance that does not name what ran — as unusable as no
        model at all. Caught HERE, at run start, because the run-wide flag guard that used to
        catch it is gone and everything later has already created an orchestration."""
        for slug in ("codex", "CODEX", "  codex  "):
            cfg = lc.load_llm_config(self.write(
                f"defaults:\n  provider: codex_cli\n  model: {slug!r}\n", "alias.yaml"))
            with self.assertRaises(lc.LlmConfigError) as ctx:
                cfg.validate_runnable()
            self.assertEqual(ctx.exception.rule, "llm_config_codex_cli_requires_model",
                             msg=slug)
            self.assertIn("explicit slug", str(ctx.exception), msg=slug)
        # ...and a real slug that merely CONTAINS it is fine.
        lc.load_llm_config(self.write(
            "defaults:\n  provider: codex_cli\n  model: gpt-5.6-codex\n",
            "real.yaml")).validate_runnable()

    def test_codex_requires_model_catches_a_per_substep_omission(self) -> None:
        cfg = lc.load_llm_config(self.write(
            "defaults:\n  provider: claude_cli\n"
            "phases:\n  validate:\n    substeps:\n      judge:\n"
            "        provider: codex_cli\n"))
        with self.assertRaises(lc.LlmConfigError) as ctx:
            cfg.validate_runnable()
        self.assertEqual(ctx.exception.rule, "llm_config_codex_cli_requires_model")
        self.assertIn("validate", ctx.exception.where)
        self.assertIn("judge", ctx.exception.where)

    def test_every_named_rule_has_a_test(self) -> None:
        """Guard against a rule being added to the module without a test here — the
        untested-rule failure mode is silent, because an unreachable rule looks exactly like a
        rule nobody triggered."""
        quoted = re.compile(r'"(llm_config_[a-z0-9_]+)"')
        declared = set(quoted.findall(
            (REPO_ROOT / "tools" / "llm_config.py").read_text(encoding="utf-8")))
        self.assertIn("llm_config_unreadable", declared)
        tested = set(quoted.findall(Path(__file__).read_text(encoding="utf-8")))
        self.assertEqual(declared - tested, set())


class DefaultsOverrideTests(unittest.TestCase):
    """`apply_defaults_overrides` applies a run-wide model / command onto a loaded config.

    Its one production caller is the preflight subprocess, which re-applies the values `main`
    already resolved so the probe describes the launch surface the run will actually use. The
    rules below are what make that safe: an override moves `defaults` and everything that took
    its value from `defaults`, and never a value a level below `defaults` declared for itself
    or an entry the config moved to another provider."""

    def test_an_override_does_not_displace_a_per_leaf_declaration(self) -> None:
        """The samples declare a model for every leaf, so a run-wide value reaches `defaults`
        and stops there. To change a declared leaf, edit the file."""
        cfg = lc.apply_defaults_overrides(
            lc.load_llm_config(SAMPLE_DIR / "llm_codex.example.yaml"), model="gpt-5-codex")
        self.assertEqual(cfg.defaults.model, "gpt-5-codex")
        for entry in cfg.entries.values():
            self.assertNotEqual(entry.model, "gpt-5-codex")
            self.assertTrue(entry.model_declared)
        cfg.validate_runnable()

    def test_an_override_still_reaches_a_file_that_declares_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bare.yaml"
            path.write_text("defaults:\n  provider: codex_cli\n", encoding="utf-8")
            cfg = lc.apply_defaults_overrides(lc.load_llm_config(path), model="gpt-5-codex")
        for entry in cfg.entries.values():
            self.assertEqual(entry.model, "gpt-5-codex")
        cfg.validate_runnable()

    def test_a_command_override_becomes_defaults_command_everywhere(self) -> None:
        """No sample declares a `command:`, so a run-wide one reaches every leaf."""
        cfg = lc.apply_defaults_overrides(
            lc.load_llm_config(SAMPLE_DIR / "llm_claude.example.yaml"),
            command="wrapper claude --x")
        self.assertEqual(cfg.defaults.command, "wrapper claude --x")
        for entry in cfg.entries.values():
            self.assertEqual(entry.command, "wrapper claude --x")

    def test_overrides_do_not_cross_a_provider_boundary(self) -> None:
        """A run-wide override describes ONE backend; an entry the config moved to another
        provider keeps its own model."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mixed.yaml"
            path.write_text(
                "defaults:\n  provider: claude_cli\n"
                "phases:\n  generate:\n    substeps:\n      generate:\n"
                "        provider: openai_compatible\n"
                "        base_url: http://localhost:8000/v1\n"
                "        api_key_env: LOCAL_KEY\n"
                "        model: local-model\n", encoding="utf-8")
            cfg = lc.apply_defaults_overrides(lc.load_llm_config(path), model="claude-pinned")
            self.assertEqual(cfg.entry_for("generate", "generate").model, "local-model")
            self.assertEqual(cfg.entry_for("generate", "verify").model, "claude-pinned")
            self.assertEqual(cfg.defaults.model, "claude-pinned")

    def test_an_explicit_pin_that_equals_the_default_still_survives_an_override(self) -> None:
        """DECLARATION decides, not value equality. A `validate.judge.model: opus` written next
        to a `defaults.model: opus` is a deliberate pin that happens to agree, and a run-wide
        override must move the default and everything that inherited it — not that."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "same.yaml"
            path.write_text(
                "defaults:\n  provider: claude_cli\n  model: opus\n"
                "phases:\n  validate:\n    substeps:\n      judge:\n        model: opus\n",
                encoding="utf-8")
            cfg = lc.apply_defaults_overrides(lc.load_llm_config(path), model="sonnet")
            self.assertEqual(cfg.entry_for("validate", "judge").model, "opus")
            self.assertEqual(cfg.defaults.model, "sonnet")
            # ...and a leaf that declared nothing DOES follow the default.
            self.assertEqual(cfg.entry_for("compile", "verify").model, "sonnet")

    def test_a_phase_level_pin_equal_to_the_default_survives_too(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "phase.yaml"
            path.write_text(
                "defaults:\n  provider: claude_cli\n  command: wrap\n"
                "phases:\n  generate:\n    command: wrap\n", encoding="utf-8")
            cfg = lc.apply_defaults_overrides(lc.load_llm_config(path), command="other")
            self.assertEqual(cfg.entry_for("generate", "generate").command, "wrap")
            self.assertEqual(cfg.entry_for("compile", "verify").command, "other")

    def test_declared_names_only_what_a_level_below_defaults_wrote(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "d.yaml"
            path.write_text(
                "defaults:\n  provider: claude_cli\n  model: opus\n"
                "phases:\n  validate:\n    substeps:\n      judge:\n        model: haiku\n",
                encoding="utf-8")
            cfg = lc.load_llm_config(path)
            self.assertEqual(cfg.entry_for("validate", "judge").declared, frozenset({"model"}))
            self.assertEqual(cfg.entry_for("compile", "verify").declared, frozenset())

    def test_overrides_leave_a_per_substep_model_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pinned.yaml"
            path.write_text(
                "defaults:\n  provider: claude_cli\n"
                "phases:\n  validate:\n    substeps:\n      judge:\n"
                "        model: judge-model\n", encoding="utf-8")
            cfg = lc.apply_defaults_overrides(lc.load_llm_config(path), model="run-wide")
            self.assertEqual(cfg.entry_for("validate", "judge").model, "judge-model")
            self.assertEqual(cfg.entry_for("compile", "verify").model, "run-wide")

    def test_the_override_unpins_the_model_it_replaces(self) -> None:
        """A run-wide override is an ALIAS, whose contract is that it leaves the model
        UNPINNED. Keeping `model_declared` when it replaced a declared value made the same
        value behave two ways depending on whether the file happened to declare one of its
        own."""
        with tempfile.TemporaryDirectory() as tmp:
            declared = Path(tmp) / "declared.yaml"
            declared.write_text("defaults:\n  provider: claude_cli\n  model: opus\n",
                                encoding="utf-8")
            bare = Path(tmp) / "bare.yaml"
            bare.write_text("defaults:\n  provider: claude_cli\n", encoding="utf-8")
            for path in (declared, bare):
                cfg = lc.apply_defaults_overrides(lc.load_llm_config(path), model="sonnet")
                entry = cfg.entry_for("validate", "judge")
                self.assertEqual(entry.model, "sonnet", msg=path.name)
                self.assertFalse(entry.model_declared, msg=path.name)
            # Without the override, a declared model IS pinned.
            self.assertTrue(
                lc.load_llm_config(declared).entry_for("validate", "judge").model_declared)

    def test_a_substep_pin_survives_the_override_and_stays_pinned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pin.yaml"
            path.write_text(
                "defaults:\n  provider: claude_cli\n  model: base\n"
                "phases:\n  validate:\n    substeps:\n      judge:\n        model: haiku\n",
                encoding="utf-8")
            cfg = lc.apply_defaults_overrides(lc.load_llm_config(path), model="sonnet")
            judge = cfg.entry_for("validate", "judge")
            self.assertEqual(judge.model, "haiku")
            self.assertTrue(judge.model_declared)
            other = cfg.entry_for("compile", "verify")
            self.assertEqual(other.model, "sonnet")
            self.assertFalse(other.model_declared)

    def test_no_override_returns_the_same_config(self) -> None:
        cfg = lc.load_llm_config(SAMPLE_DIR / "llm_claude.example.yaml")
        self.assertIs(lc.apply_defaults_overrides(cfg), cfg)


class MirrorTableDriftTests(unittest.TestCase):
    """Each guard derives the original from the owning module, not from a second literal."""

    def test_llm_leaf_substeps_matches_conductor(self) -> None:
        derived = {
            (phase, substep)
            for phase, substeps in wc.SUBSTEPS.items()
            for substep in substeps
            if substep is not None
            and not wc.Conductor._is_deterministic_substep(phase, substep)
        }
        self.assertEqual(derived, set(lc.LLM_LEAF_SUBSTEPS))

    def test_llm_leaf_phases_excludes_build(self) -> None:
        self.assertNotIn("build", lc.LLM_LEAF_PHASES)
        self.assertEqual(lc.LLM_LEAF_PHASES, frozenset({"compile", "generate", "validate"}))

    def test_pure_capable_substeps_matches_conductor(self) -> None:
        """Runs `Conductor._pure_leaf_substep` itself (on a stub whose node-shape predicates
        are both True and whose entry is pure-capable), so the pair test in that body is what
        is being compared — not a copy of it."""

        class _Stub:
            # Both spellings of "this launch's model is pure-capable" are supplied, so the
            # guard keeps working across the Phase-2 conversion of the backend identity into a
            # per-launch entry.
            backend = "claude"
            _pure_leaf_substep = wc.Conductor._pure_leaf_substep

            def _conductor_authors_makefile(self, refs):  # noqa: D401 - stub
                return True

            def _conductor_authors_runner(self, refs):  # noqa: D401 - stub
                return True

            def entry_for(self, phase, substep):
                return lc.ResolvedLeafEntry(
                    provider="claude_cli",
                    capabilities=lc.PROVIDER_CAPABILITIES["claude_cli"])

        stub = _Stub()
        derived = {
            (phase, substep) for (phase, substep) in lc.LLM_LEAF_SUBSTEPS
            if stub._pure_leaf_substep(None, phase, substep)
        }
        self.assertEqual(derived, set(lc.PURE_CAPABLE_SUBSTEPS))
        self.assertLessEqual(lc.PURE_CAPABLE_SUBSTEPS, lc.LLM_LEAF_SUBSTEPS)

    def test_mcp_required_llm_substeps_matches_runtime(self) -> None:
        granted = {key for key, tools in ort._MCP_TOOL_GRANTS_BY_SUBSTEP.items() if tools}
        self.assertEqual(granted & set(lc.LLM_LEAF_SUBSTEPS),
                         set(lc.MCP_REQUIRED_LLM_SUBSTEPS))

    def test_backend_tokens_cover_the_legacy_supported_backends(self) -> None:
        cli_tokens = {lc.PROVIDER_BACKEND_TOKENS[p] for p in lc.CLI_PROVIDERS}
        self.assertEqual(cli_tokens, set(ort.SUPPORTED_BACKENDS))
        self.assertEqual(set(lc.PROVIDER_BACKEND_TOKENS), set(lc.SUPPORTED_PROVIDERS))
        self.assertEqual(len(set(lc.PROVIDER_BACKEND_TOKENS.values())),
                         len(lc.SUPPORTED_PROVIDERS))
        self.assertEqual(lc.CLI_PROVIDERS | lc.HTTP_PROVIDERS, lc.SUPPORTED_PROVIDERS)
        self.assertEqual(lc.CLI_PROVIDERS & lc.HTTP_PROVIDERS, frozenset())

    def test_the_recorded_hash_describes_the_bytes_that_were_parsed(self) -> None:
        """Hashing via a SECOND read lets a replacement between the two resolve the entries
        from the old bytes while recording a pin describing the new ones — an invocation
        claiming a configuration the run did not use, and a resume that accepts it."""
        import unittest.mock
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.yaml"
            original = "defaults:\n  provider: claude_cli\n  model: original\n"
            path.write_text(original, encoding="utf-8")
            real_read_bytes = Path.read_bytes

            def _swap_after_read(self_path):
                data = real_read_bytes(self_path)
                if self_path == path:            # the file changes underneath us
                    path.write_text("defaults:\n  provider: claude_cli\n  model: swapped\n",
                                    encoding="utf-8")
                return data

            with unittest.mock.patch.object(Path, "read_bytes", _swap_after_read):
                cfg = lc.load_llm_config(path)
            self.assertEqual(cfg.defaults.model, "original")
            self.assertEqual(cfg.sha256, lc._sha256_bytes(original.encode("utf-8")))
            self.assertNotEqual(cfg.sha256, lc.config_sha256(path))

    def test_config_sha256_agrees_with_the_runtime_hash_primitive(self) -> None:
        """Duplicated primitive, same bytes, same string form — including the missing-file
        sentinel, which callers compare against."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f.yaml"
            path.write_text("defaults:\n  provider: claude_cli\n", encoding="utf-8")
            self.assertEqual(lc.config_sha256(path), ort._compute_sha256(path))
            missing = Path(tmp) / "gone.yaml"
            self.assertEqual(lc.config_sha256(missing), ort._compute_sha256(missing))
            self.assertEqual(lc.config_sha256(missing), "sha256:missing")

    def test_an_unreadable_file_hashes_to_a_value_no_record_can_match(self) -> None:
        """A file that exists but cannot be read is not an exception the callers are shaped
        for — the resume gate would exit with a traceback instead of its structured refusal."""
        import os
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f.yaml"
            path.write_text("defaults:\n  provider: claude_cli\n", encoding="utf-8")
            os.chmod(path, 0o000)
            try:
                if os.access(path, os.R_OK):     # running as root: the mode does not apply
                    self.skipTest("cannot make a file unreadable as this user")
                self.assertEqual(lc.config_sha256(path), "sha256:unreadable")
            finally:
                os.chmod(path, 0o644)


if __name__ == "__main__":
    unittest.main()
