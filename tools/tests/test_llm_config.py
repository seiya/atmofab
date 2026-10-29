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
3. **The mirror tables still mirror.** `LLM_LEAF_SUBSTEPS` is a copy of a fact owned by the
   conductor. Its guard derives the original BEHAVIORALLY (running the conductor predicate)
   rather than re-asserting the same literal, so moving the original reds the test instead of
   leaving two agreeing-but-wrong copies. `MCP_REQUIRED_LLM_SUBSTEPS` was the second such
   table and went with the MCP capability gate (issue #171 PR-2), along with its guard.
"""

from __future__ import annotations

import re
import shlex
import tempfile
import unittest
from unittest import mock

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


#: The `defaults` most rows start from: a CLI provider and the diagnostician's own bound
#: (`defaults.lineage_timeout_s` is required since issue #534 PR-J4-a and bounds that launch
#: alone).
_DEFAULTS = "defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n"

#: The entry this file writes for an LLM substep whose row is ABOUT something else: the bounds
#: its kind is served and nothing more (issue #534 PR-J4-b — every substep entry is written,
#: states `lineage_timeout_s`, and a producing one `max_wrapper_calls`; no level inherits
#: either). Spelled out here rather than filled in by `llm_samples.workshop_yaml`, because the
#: bound rules are this file's subject: a row about them writes its entry itself.
_BOUNDED_ENTRY: dict[tuple[str, str], str] = {
    ("compile", "generate"): "{max_wrapper_calls: 30, lineage_timeout_s: 2700}",
    ("compile", "verify"): "{lineage_timeout_s: 2700}",
    ("generate", "generate"): "{max_wrapper_calls: 30, lineage_timeout_s: 2700}",
    ("generate", "verify"): "{lineage_timeout_s: 2700}",
    ("validate", "judge"): "{lineage_timeout_s: 2700}",
}


def _doc(defaults: str = _DEFAULTS, entries: dict[tuple[str, str], str] | None = None,
         phase_fields: dict[str, str] | None = None) -> str:
    """A whole configuration document: `defaults`, then all five LLM substeps.

    A pair in `entries` is written EXACTLY as given — a flow mapping (`"{...}"`) or a block
    body (one `key: value` per line) — and nothing is added to it, so a row about an entry's
    own fields states its bounds itself. A pair not in `entries` is written as
    `_BOUNDED_ENTRY`. `phase_fields` are block lines written at the phase level."""
    entries = dict(entries or {})
    phase_fields = dict(phase_fields or {})
    assert set(entries) <= set(_BOUNDED_ENTRY), set(entries) - set(_BOUNDED_ENTRY)
    assert set(phase_fields) <= lc.LLM_LEAF_PHASES, set(phase_fields)
    lines = [defaults.rstrip("\n"), "phases:"]
    for phase in ("compile", "generate", "validate"):
        lines.append(f"  {phase}:")
        lines.extend("    " + line for line in phase_fields.get(phase, "").splitlines())
        lines.append("    substeps:")
        for (p, substep), default_body in _BOUNDED_ENTRY.items():
            if p != phase:
                continue
            body = entries.get((p, substep), default_body)
            if body.startswith("{"):
                lines.append(f"      {substep}: {body}")
            else:
                lines.append(f"      {substep}:")
                lines.extend("        " + line for line in body.splitlines())
    return "\n".join(lines) + "\n"


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

        `effort` is required on every entry whose provider HAS one, which since issue #560 is
        every provider: `anthropic_api` takes `output_config.effort` (it once refused the field
        on a thinking-token-budget premise the current models' API does not have). The
        `inapplicable` branch below stays for a provider added later without one."""
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

    def test_the_claude_sample_states_the_capability_surface_the_code_declares(self) -> None:
        """Issue #168 put the `capabilities:` spelling into the claude sample's header, as a
        COMMENT rather than a key: placing the key would change the leaf set the sample rows
        above read. A comment is exactly the form that rots, so the two facts it states are
        derived from the code here.

        The comment names the provider's whole capability set and the refusal a superset raises.
        Both are enumerations `llm_config` owns, so this asserts the DOCUMENT against the code —
        the set is never spelled in this test. `capabilities:` is required literally because
        that is the operator instruction the comment exists to give; a sample that stops naming
        the key has lost the thing it was added for. (Until Z4, issue #171, the literal was
        `capabilities: [agentic]` — the instruction for putting a leaf back on the agentic
        path, which no longer exists.)

        READ THE ENUMERATION, NOT THE HEADER, and this is the round-1 correction: the first
        version asked `assertIn(cap, header)`, and each name occurs several further times in
        that header outside the enumeration line, so those were satisfied by unrelated prose —
        dropping one from the enumeration left the row green (measured). The parenthetical is
        located by a marker asserted UNIQUE, its contents are parsed into a set, and the
        comparison is set IDENTITY rather than membership: a capability the code drops but the
        comment keeps naming is as wrong for the operator as one it omits."""
        text = (SAMPLE_DIR / "llm_claude.example.yaml").read_text(encoding="utf-8")
        # Anchored at a LINE START, and this is the round-2 correction. `text.split("defaults:")`
        # cut at the first occurrence anywhere, including inside the comment prose — the header
        # already says "would let all five leaves inherit one `defaults` block", one character
        # from tripping it — so a legitimate sentence mentioning the key above the capabilities
        # paragraph truncated the header and made the check report a string that IS in the file
        # as missing, naming a repair that is a no-op. Measured before the fix.
        parts = re.split(r"(?m)^defaults:", text, maxsplit=1)
        self.assertEqual(len(parts), 2,
                         "llm_claude.example.yaml has no top-level `defaults:` key, so the header "
                         "below would be the whole file and every check in this row would be "
                         "answered by the per-leaf blocks instead of by the comment.")
        header = parts[0]
        self.assertIn("`capabilities:` narrows", header)
        self.assertIn("llm_config_capability_exceeds_provider", header)
        marker = "(`claude_cli`: "
        self.assertEqual(
            header.count(marker), 1,
            f"llm_claude.example.yaml: {marker!r} is not a unique marker, so the parse below "
            f"would read the wrong span. It is a hardcoded bound of this check.")
        listed = header.split(marker, 1)[1].split(")", 1)[0]
        # `strip("#")` too: the enumeration is inside a COMMENT, so a legitimate re-wrap puts a
        # `#` at the head of a continuation line and would otherwise glue it to a capability
        # name. Tolerating the comment marker keeps a reformat from reading as a disagreement.
        declared = {name.strip(" `#\n") for name in listed.split(",") if name.strip(" `#\n")}
        self.assertEqual(
            declared, set(lc.PROVIDER_CAPABILITIES["claude_cli"]),
            "llm_claude.example.yaml's header enumerates what `capabilities:` may restrict and "
            "disagrees with `PROVIDER_CAPABILITIES['claude_cli']`. An operator reading a short "
            "list would think naming a missing one is the superset the refusal above rejects; "
            "one reading a long list would think a name the code no longer has is admissible.")

    def test_every_sample_entry_states_the_bounds_of_its_kind_and_no_leaf(self) -> None:
        """Issue #534 PR-J4-b: every LLM leaf is a workshop leaf, so the samples' entries are
        read off the DOCUMENT (not the resolved entry, which could not tell a written bound from
        an inherited one): each substep entry states `lineage_timeout_s`, a producing one also
        `max_wrapper_calls`, a judging one never `max_wrapper_calls`; `defaults` states the
        diagnostician's `lineage_timeout_s` and no `max_wrapper_calls`; no level writes `leaf`,
        which is refused `llm_config_unknown_key` since that PR."""
        for name, path in _discovered_samples().items():
            document = _sample_body(path)
            self.assertIn("lineage_timeout_s", document["defaults"], msg=name)
            self.assertNotIn("max_wrapper_calls", document["defaults"], msg=name)
            self.assertNotIn("leaf", document["defaults"], msg=name)
            for phase, phase_doc in document["phases"].items():
                self.assertFalse({"leaf", "lineage_timeout_s", "max_wrapper_calls"}
                                 & set(phase_doc), msg=f"{name} {phase}")
                for substep, entry in phase_doc["substeps"].items():
                    where = f"{name} {phase}.{substep}"
                    kind = ("reading" if (phase, substep) in lc.WORKSHOP_READING_SUBSTEPS
                            else "producing")
                    self.assertEqual(
                        {f for f in ("max_wrapper_calls", "lineage_timeout_s") if f in entry},
                        set(lc.WORKSHOP_BOUND_FIELDS_BY_KIND[kind]), msg=where)
                    self.assertNotIn("leaf", entry, msg=where)

    def test_the_http_samples_put_the_http_provider_on_every_llm_leaf(self) -> None:
        """Every LLM leaf admits an HTTP provider (each is a workshop leaf, and both HTTP
        providers declare `workshop`); what is checked here is that the HTTP samples USE that
        whole surface rather than demonstrating one leaf and leaving the others on a CLI."""
        for name in ("llm_openai_compatible.example.yaml", "llm_anthropic_api.example.yaml"):
            cfg = lc.load_llm_config(SAMPLE_DIR / name)
            provider = name[len("llm_"):-len(".example.yaml")]
            self.assertIn(provider, lc.HTTP_PROVIDERS, msg=name)
            on_http = {key for key, entry in cfg.entries.items() if entry.is_http}
            self.assertEqual(on_http, set(lc.LLM_LEAF_SUBSTEPS), msg=name)
            for key in on_http:
                self.assertEqual(cfg.entries[key].provider, provider, msg=f"{name} {key}")
            # ...and `defaults` stays on a CLI, which is what runs the `escalate`
            # diagnostician.
            self.assertFalse(cfg.defaults.is_http, msg=name)

    def test_a_codex_config_without_a_model_still_stops_before_launching(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.yaml"
            path.write_text(_doc("defaults:\n  lineage_timeout_s: 2700\n  provider: codex_cli\n"),
                            encoding="utf-8")
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
            path.write_text("defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n", encoding="utf-8")
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
            # ANCHORED to the run's root on BOTH sides. `--repo-root` need not be the shell's
            # working directory, and a relative `cp` pasted from elsewhere copies between two
            # directories the run is not using — leaving the default still missing and the
            # next run failing with this very message again.
            self.assertIn(
                shlex.join(["cp", str(Path(tmp) / "docs" / "examples" / name),
                            str(Path(tmp) / "llm.yaml")]),
                message)
            self.assertTrue((SAMPLE_DIR / name).exists(), msg=name)
        # Every `cp` line must be PASTE-SAFE. The message ends with the list, and
        # `LlmConfigError.__str__` appends " (at <where>)" whenever `where` is set — which
        # would land on the last command and make it fail for anyone who copies it. This is the
        # first message a new operator ever sees.
        for line in message.splitlines():
            if line.strip().startswith("cp "):
                self.assertTrue(line.strip().endswith("/llm.yaml"), msg=line)
        self.assertEqual(ctx.exception.where, "")

    def test_the_copy_commands_survive_a_repo_root_a_shell_would_split(self) -> None:
        """The paths are the OPERATOR's, and this is the one message whose entire job is to be
        pasted into a shell. A root containing a space silently becomes extra arguments: `cp`
        then either fails or, with the wrong argument count, acts on something else."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "my repo (v2)"
            root.mkdir()
            with self.assertRaises(lc.LlmConfigError) as ctx:
                lc.resolve_default_config_path(root)
        for line in str(ctx.exception).splitlines():
            if not line.strip().startswith("cp "):
                continue
            argv = shlex.split(line.strip())
            self.assertEqual(len(argv), 3, msg=line)          # cp SRC DST, not cp + fragments
            self.assertEqual(Path(argv[2]), root / "llm.yaml", msg=line)
            self.assertEqual(Path(argv[1]).parent, root / "docs" / "examples", msg=line)



class ResolutionTests(_Tmp):
    def test_every_llm_leaf_gets_an_entry_from_bare_defaults(self) -> None:
        """Every substep entry is written since issue #534 PR-J4-b (each states its own bound),
        but one that states ONLY its bounds still takes every other field from `defaults`."""
        cfg = lc.load_llm_config(self.write(_doc(
            "defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n  model: d\n")))
        self.assertEqual(set(cfg.entries), set(lc.LLM_LEAF_SUBSTEPS))
        for key, entry in cfg.entries.items():
            self.assertEqual((entry.provider, entry.model), ("claude_cli", "d"), msg=key)

    def test_phase_layer_overrides_defaults_and_substep_overrides_phase(self) -> None:
        cfg = lc.load_llm_config(self.write(_doc(
            "defaults:\n  lineage_timeout_s: 2700\n"
            "  provider: claude_cli\n"
            "  model: default-model\n",
            entries={("generate", "verify"):
                     "model: substep-model\nlineage_timeout_s: 2700"},
            phase_fields={"generate": "model: phase-model"})))
        self.assertEqual(cfg.entry_for("compile", "verify").model, "default-model")
        self.assertEqual(cfg.entry_for("generate", "generate").model, "phase-model")
        self.assertEqual(cfg.entry_for("generate", "verify").model, "substep-model")

    def test_unmentioned_fields_inherit_per_field(self) -> None:
        cfg = lc.load_llm_config(self.write(_doc(
            "defaults:\n  lineage_timeout_s: 2700\n"
            "  provider: claude_cli\n"
            "  model: default-model\n"
            "  max_output_tokens: 4096\n",
            entries={("generate", "verify"):
                     "model: substep-model\nlineage_timeout_s: 2700"})))
        entry = cfg.entry_for("generate", "verify")
        self.assertEqual(entry.model, "substep-model")
        self.assertEqual(entry.max_output_tokens, 4096)

    def test_provider_switch_drops_inherited_provider_scoped_fields(self) -> None:
        """A deeper level that switches provider must not inherit the outer provider's
        model / command: they name things that only exist on the outer transport."""
        cfg = lc.load_llm_config(self.write(_doc(
            "defaults:\n  lineage_timeout_s: 2700\n"
            "  provider: claude_cli\n"
            "  model: claude-model\n"
            "  command: claude --flag\n"
            "  max_output_tokens: 4096\n",
            entries={("generate", "generate"):
                     "provider: openai_compatible\n"
                     "base_url: http://localhost:8000/v1\n"
                     "api_key_env: LOCAL_KEY\n"
                     "model: local-model\n"
                     "max_wrapper_calls: 30\n"
                     "lineage_timeout_s: 2700"})))
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
        cfg = lc.load_llm_config(self.write(_doc(
            "defaults:\n  lineage_timeout_s: 2700\n"
            "  provider: claude_cli\n"
            "  model: claude-model\n"
            "  command: wrapper-claude --sandbox\n",
            entries={("generate", "verify"):
                     "provider: claude_cli\nlineage_timeout_s: 2700"},
            phase_fields={"generate": "provider: openai_compatible\n"
                                      "base_url: http://localhost:8000/v1\n"
                                      "api_key_env: LOCAL_KEY\n"
                                      "model: local-model"})))
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
        cfg = lc.load_llm_config(self.write(_doc(
            "defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n"
            "  max_output_tokens: 32000\n",
            entries={("validate", "judge"):
                     "{provider: codex_cli, model: gpt-5.6-sol, lineage_timeout_s: 2700}"})))
        self.assertEqual(cfg.entry_for("compile", "verify").max_output_tokens, 32000)
        # Dropped where it cannot apply, so nothing downstream reads a budget that is ignored.
        self.assertIsNone(cfg.entry_for("validate", "judge").max_output_tokens)

    def test_declaring_a_non_applicable_field_on_its_own_provider_is_rejected(self) -> None:
        """The other side: a field the operator wrote FOR this provider is an error, because
        there is a key in the document to point at."""
        self.assert_rule("llm_config_field_not_applicable",
                         _doc("defaults:\n  lineage_timeout_s: 2700\n  provider: codex_cli\n"
                              "  model: m\n  max_output_tokens: 32000\n"))
        self.assert_rule("llm_config_field_not_applicable",
                         _doc("defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n"
                              "  timeout_s: 300\n"))

    def test_provider_switch_without_a_replacement_model_is_rejected_not_inherited(self) -> None:
        """The drop is real: the outer `model` cannot satisfy the HTTP model requirement."""
        self.assert_rule("llm_config_http_requires_model", _doc(
            "defaults:\n  lineage_timeout_s: 2700\n"
            "  provider: claude_cli\n"
            "  model: claude-model\n",
            entries={("generate", "generate"):
                     "provider: openai_compatible\n"
                     "base_url: http://localhost:8000/v1\n"
                     "api_key_env: LOCAL_KEY\n"
                     "max_wrapper_calls: 30\n"
                     "lineage_timeout_s: 2700"}))

    def test_entry_for_none_none_is_defaults(self) -> None:
        cfg = lc.load_llm_config(self.write(_doc(
            "defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n  model: d\n",
            phase_fields={"generate": "model: p"})))
        self.assertIs(cfg.entry_for(None, None), cfg.defaults)
        self.assertEqual(cfg.entry_for(None, None).model, "d")

    def test_deterministic_substep_falls_back_to_defaults(self) -> None:
        cfg = lc.load_llm_config(self.write(_doc()))
        self.assertIs(cfg.entry_for("generate", "gate"), cfg.defaults)

    _OPENAI_GENERATE = ("provider: openai_compatible\n"
                        "base_url: http://localhost:8000/v1\n"
                        "api_key_env: LOCAL_KEY\n"
                        "model: local-model\n"
                        "max_wrapper_calls: 30\n"
                        "lineage_timeout_s: 2700")

    def test_anthropic_api_base_url_defaults_to_the_canonical_endpoint(self) -> None:
        cfg = lc.load_llm_config(self.write(_doc(entries={
            ("generate", "generate"):
                "provider: anthropic_api\n"
                "api_key_env: ANTHROPIC_API_KEY\n"
                "model: claude-opus-5\n"
                "max_wrapper_calls: 30\n"
                "lineage_timeout_s: 2700"})))
        self.assertEqual(cfg.entry_for("generate", "generate").base_url,
                         lc.ANTHROPIC_DEFAULT_BASE_URL)

    def test_mixed_config_reports_both_providers(self) -> None:
        cfg = lc.load_llm_config(self.write(_doc(entries={
            ("generate", "generate"): self._OPENAI_GENERATE})))
        self.assertFalse(cfg.is_uniform)
        self.assertEqual(cfg.providers, frozenset({"claude_cli", "openai_compatible"}))
        cfg.validate_runnable()

    def test_provenance_map_is_per_leaf_and_includes_defaults(self) -> None:
        """Each row carries the bounds its entry has (issue #534 PR-J4-b: every entry has
        `lineage_timeout_s`, a producing one `max_wrapper_calls`), never a `"None"`, and no
        `leaf` key — the field that named the leaf model is retired, so a row written now
        never carries it (a row recorded before that PR may)."""
        cfg = lc.load_llm_config(self.write(_doc(entries={
            ("generate", "generate"): self._OPENAI_GENERATE.replace(
                "max_wrapper_calls: 30", "max_wrapper_calls: 12"),
            ("validate", "judge"): "{lineage_timeout_s: 600}"})))
        pm = cfg.provenance_map()
        self.assertEqual(set(pm), {"defaults"} | {f"{p}.{s}" for p, s in lc.LLM_LEAF_SUBSTEPS})
        self.assertEqual(pm["generate.generate"],
                         {"provider": "openai_compatible", "backend": "openai_compatible",
                          "model": "local-model", "command": "", "effort": "",
                          "max_wrapper_calls": "12", "lineage_timeout_s": "2700.0"})
        self.assertEqual(pm["validate.judge"],
                         {"provider": "claude_cli", "backend": "claude", "model": "",
                          "command": "", "effort": "", "lineage_timeout_s": "600.0"})
        self.assertEqual(pm["defaults"],
                         {"provider": "claude_cli", "backend": "claude", "model": "",
                          "command": "", "effort": "", "lineage_timeout_s": "2700.0"})
        for key, row in pm.items():
            self.assertNotIn("leaf", row, msg=key)

    def test_describe_providers_dedups_on_the_probed_surface(self) -> None:
        cfg = lc.load_llm_config(self.write(_doc(
            phase_fields={"generate": "model: another-model"})))
        self.assertEqual([r["backend"] for r in lc.describe_providers(cfg)], ["claude"])


class ProviderCapabilityTableTests(unittest.TestCase):
    """`docs/ORCHESTRATION.md`'s provider table is checked against the code, not read.

    The table is the third statement of `PROVIDER_CAPABILITIES` (the code, `docs/GLOSSARY.md`
    and this table), which is the count at which the skill says discipline has already lost —
    and it lost: Z4 deleted `agentic` and swept the glossary and the example configuration,
    and PR-2 deleted `mcp_tools` and swept them again, while the table kept a whole `mcp_tools`
    column reading `yes | yes | — | —` through both. Nothing had ever read it.

    Coupled by MEMBERS in both directions, because the table names them in full: the capability
    columns must be exactly `KNOWN_CAPABILITIES`, and each cell must agree with that provider's
    set. The code is the authority and the document is compared against it, never the reverse.
    """

    TABLE_DOC = REPO_ROOT / "docs" / "ORCHESTRATION.md"

    def _rows(self) -> list[list[str]]:
        lines = self.TABLE_DOC.read_text(encoding="utf-8").splitlines()
        # Anchor on the header, which names the two non-capability columns. Text that
        # PRECEDES the rule and is byte-identical in the wording being refused: a table that
        # loses a capability column still opens `| provider | backend token |`.
        # Anchored on the first column alone, not on the header's full text: anchoring on
        # `| provider | backend token |` let a second table headed `| provider | backend |`
        # sit in the same document unnoticed, which is how a stale copy comes back.
        starts = [i for i, line in enumerate(lines)
                  if line.strip().replace(" ", "").startswith("|provider|")]
        self.assertEqual(len(starts), 1,
                         f"{self.TABLE_DOC.name}: expected exactly one provider table, "
                         f"found {len(starts)}")
        rows = []
        for line in lines[starts[0]:]:
            stripped = line.strip()
            if not stripped.startswith("|"):
                break
            rows.append([cell.strip() for cell in stripped.strip("|").split("|")])
        self.assertGreater(len(rows), 2, "table has a header but no provider rows")
        # RAGGEDNESS is the escape `zip` leaves open in both directions: a row missing its
        # last cell silently drops that capability's claim, and a row with an extra cell
        # resurrects a column the header no longer has. Checked before any `zip` runs.
        widths = {len(row) for row in rows}
        self.assertEqual(len(widths), 1,
                         f"the provider table is ragged (row widths {sorted(widths)}); a "
                         "short or long row makes the cell comparison read the wrong column")
        return rows

    def test_the_columns_are_exactly_the_known_capabilities(self) -> None:
        header = self._rows()[0]
        self.assertEqual(header[:2], ["provider", "backend token"])
        columns = [cell.strip("`") for cell in header[2:]]
        self.assertEqual(sorted(columns), sorted(lc.KNOWN_CAPABILITIES),
                         "the table's capability columns and KNOWN_CAPABILITIES disagree; "
                         "the code is the authority")

    def test_every_cell_agrees_with_the_capability_authority(self) -> None:
        rows = self._rows()
        columns = [cell.strip("`") for cell in rows[0][2:]]
        # A DUPLICATE ROW is the row-granular twin of the duplicate table this anchor
        # already refuses: a stale `claude_cli` row above the real one is silently
        # overwritten by `documented[provider] = ...`, and the stale copy — which is what
        # a reader's eye lands on first — goes unchecked. Counted before it is built.
        providers = [row[0].strip("`") for row in rows[2:]]
        self.assertEqual(len(providers), len(set(providers)),
                         f"the provider table lists a provider twice: {providers}")
        self.assertEqual(len(providers), len(lc.PROVIDER_CAPABILITIES),
                         "the table and PROVIDER_CAPABILITIES have different row counts")
        documented = {}
        for row in rows[2:]:
            provider = row[0].strip("`")
            documented[provider] = frozenset(
                cap for cap, cell in zip(columns, row[2:]) if cell == "yes")
            for cap, cell in zip(columns, row[2:]):
                with self.subTest(provider=provider, capability=cap):
                    self.assertIn(cell, ("yes", "\u2014"),
                                  "a cell is `yes` or an em dash and nothing else")
        self.assertEqual(documented, dict(lc.PROVIDER_CAPABILITIES))

    def test_the_backend_token_column_is_the_launch_identity(self) -> None:
        # The other half of each row. A provider whose token the table invents would send an
        # operator to `init --backend <token>` with a name the parser refuses.
        for row in self._rows()[2:]:
            provider, token = row[0].strip("`"), row[1].strip("`")
            with self.subTest(provider=provider):
                expected = "claude" if provider == "claude_cli" else (
                    "codex" if provider == "codex_cli" else provider)
                self.assertEqual(token, expected)

class CapabilityTests(_Tmp):
    def test_provider_capability_table_is_the_authority(self) -> None:
        self.assertEqual(set(lc.PROVIDER_CAPABILITIES), set(lc.SUPPORTED_PROVIDERS))
        for provider, caps in lc.PROVIDER_CAPABILITIES.items():
            self.assertLessEqual(caps, lc.KNOWN_CAPABILITIES, msg=provider)
            # Every LLM leaf is a workshop leaf (issue #534 PR-J4-b), so a provider without it
            # could run nothing; every declared provider has it.
            self.assertIn(lc.CAP_WORKSHOP, caps, msg=provider)
        # The user-approved scope rule, as a property of the table rather than a branch: an
        # HTTP provider has no session to reopen, and since issue #534 PR-J3-b runs a workshop
        # leaf through the host loop.
        for provider in lc.HTTP_PROVIDERS:
            self.assertEqual(lc.PROVIDER_CAPABILITIES[provider],
                             frozenset({lc.CAP_WORKSHOP}))

    def test_supports_is_fail_closed_on_an_unknown_capability_name(self) -> None:
        entry = lc.ResolvedLeafEntry(provider="claude_cli",
                                     capabilities=lc.PROVIDER_CAPABILITIES["claude_cli"])
        self.assertTrue(entry.supports(lc.CAP_WARM_RESUME))
        self.assertFalse(entry.supports("warm_resumee"))
        self.assertFalse(entry.supports(""))

    def test_capabilities_may_restrict_a_provider(self) -> None:
        cfg = lc.load_llm_config(self.write(_doc(
            "defaults:\n  lineage_timeout_s: 2700\n"
            "  provider: claude_cli\n"
            "  capabilities: [workshop]\n")))
        entry = cfg.entry_for("validate", "judge")
        self.assertTrue(entry.supports(lc.CAP_WORKSHOP))
        self.assertFalse(entry.supports(lc.CAP_WARM_RESUME))
        self.assertFalse(cfg.defaults.supports(lc.CAP_WARM_RESUME))

    def test_http_provider_on_a_generate_leaf_is_accepted(self) -> None:
        for substep, bounds in (("generate", "max_wrapper_calls: 30\nlineage_timeout_s: 2700"),
                                ("verify", "lineage_timeout_s: 2700")):
            cfg = lc.load_llm_config(self.write(_doc(entries={
                ("generate", substep):
                    "provider: openai_compatible\n"
                    "base_url: http://localhost:8000/v1\n"
                    "api_key_env: LOCAL_KEY\n"
                    "model: local-model\n" + bounds})))
            self.assertEqual(cfg.entry_for("generate", substep).provider, "openai_compatible")

    def test_a_pair_outside_the_llm_leaf_table_requires_nothing(self) -> None:
        """`_validate_assignment` requires `workshop` of the pairs `LLM_LEAF_SUBSTEPS` names,
        and of nothing else: the requirement is derived from the table alone.

        Driven with a synthetic pair, since every real pair is in the table — which is what
        this row is FOR, and also its limit: it pins the derivation, not the wiring. The entry
        states both bounds, because the bound rule (which treats a pair outside the reading set
        as producing) is a different rule; with them, an entry with NO capability passes the
        synthetic pair and is refused on every real one."""
        self.assertNotIn(("future", "leaf"), lc.LLM_LEAF_SUBSTEPS)
        self.assertEqual(lc.required_capabilities("future", "leaf"), frozenset())
        entry = lc.ResolvedLeafEntry(
            provider="anthropic_api", capabilities=frozenset(),
            max_wrapper_calls=3, lineage_timeout_s=10.0)
        # No raise: nothing is required of a pair no LLM leaf runs.
        lc._validate_assignment("future", "leaf", entry, "phases.future.substeps.leaf")
        # ...and every REAL pair does require `workshop`.
        for phase, substep in sorted(lc.LLM_LEAF_SUBSTEPS):
            self.assertEqual(lc.required_capabilities(phase, substep),
                             frozenset({lc.CAP_WORKSHOP}), msg=f"{phase}.{substep}")
            with self.assertRaises(lc.LlmConfigError) as ctx:
                lc._validate_assignment(phase, substep, entry, "x")
            self.assertEqual(ctx.exception.rule,
                             "llm_config_capability_insufficient_for_substep",
                             msg=f"{phase}.{substep}")

    def test_llm_config_refuses_an_entry_without_workshop(self) -> None:
        """Issue #534 PR-J4-b: `workshop` is the ONLY leaf model, so every LLM leaf
        hard-requires it and an entry narrowed away from it is refused by name — on every pair,
        not on the one this test happened to pick. (This is also the whole of what
        `llm_config_workshop_unsupported_on_provider` used to refuse, whose predicate became
        this rule's and which went with that PR.)

        The second half is what makes the first enforceable rather than advisory: neither
        `pure` (retired by that PR) nor `agentic` nor `mcp_tools` is a capability the
        vocabulary knows any more, so a configuration still spelling one is refused at
        `capabilities:` parse time rather than resolving to a transport or a grant that no
        longer exists. Each is asserted, at `defaults` and on a substep entry, because PR-2
        retired `mcp_tools` while pinning only `agentic`, and re-adding `mcp_tools` to
        `KNOWN_CAPABILITIES` was silent."""
        for phase, substep in sorted(lc.LLM_LEAF_SUBSTEPS):
            bounds = _BOUNDED_ENTRY[(phase, substep)][1:-1]
            err = self.assert_rule(
                "llm_config_capability_insufficient_for_substep",
                _doc(entries={(phase, substep): f"{{capabilities: [warm_resume], {bounds}}}"}))
            self.assertIn("'workshop'", str(err), msg=f"{phase}.{substep}")
            self.assertEqual(err.where, f"phases.{phase}.substeps.{substep}")
            # Not "pure OR workshop": there is no second leaf model to fall back to, and a
            # message offering one is the shape this row exists to keep out.
            for retired in ("pure", "agentic"):
                self.assertNotIn(f"'{retired}'", str(err), msg=f"{phase}.{substep}")
        for retired in ("pure", "agentic", "mcp_tools"):
            with self.subTest(capability=retired):
                err = self.assert_rule(
                    "llm_config_invalid_field",
                    _doc("defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n"
                         f"  capabilities: [{retired}, workshop]\n"))
                self.assertIn(retired, str(err))
                self.assertEqual(err.where, "defaults.capabilities")
                err = self.assert_rule(
                    "llm_config_invalid_field",
                    _doc(entries={("validate", "judge"):
                                  f"{{capabilities: [{retired}], lineage_timeout_s: 2700}}"}))
                self.assertEqual(err.where, "phases.validate.substeps.judge.capabilities")
                self.assertNotIn(retired, lc.KNOWN_CAPABILITIES)
                for provider, caps in lc.PROVIDER_CAPABILITIES.items():
                    self.assertNotIn(retired, caps, msg=provider)

    def test_every_declared_provider_validates_for_the_compile_leaves(self) -> None:
        """Issue #168's completion criterion: whichever provider this repository declares can be
        put on either compile leaf and the configuration loads. `no vendor lock-in` is the
        premise (`AGENTS.md` §Development premises), so the subject is every member of
        `PROVIDER_CAPABILITIES`, not the two the operator's machine happens to run."""
        blocks = {
            "claude_cli": "provider: claude_cli\nmodel: opus\n",
            "codex_cli": "provider: codex_cli\nmodel: gpt-5.6-sol\n",
            "openai_compatible": (
                "provider: openai_compatible\n"
                "base_url: http://localhost:8000/v1\n"
                "api_key_env: LOCAL_KEY\nmodel: local-model\n"),
            "anthropic_api": (
                "provider: anthropic_api\n"
                "api_key_env: ANTHROPIC_API_KEY\nmodel: claude-opus-5\n"),
        }
        self.assertEqual(set(blocks), set(lc.PROVIDER_CAPABILITIES))
        for provider, block in sorted(blocks.items()):
            for substep, bounds in (("generate", "max_wrapper_calls: 30\nlineage_timeout_s: 2700"),
                                    ("verify", "lineage_timeout_s: 2700")):
                cfg = lc.load_llm_config(self.write(_doc(
                    "defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n  model: opus\n",
                    entries={("compile", substep): block + bounds})))
                cfg.validate_runnable()
                self.assertEqual(cfg.entry_for("compile", substep).provider, provider)


class RuleTests(_Tmp):
    #: An HTTP `generate.generate` entry, written whole (its bounds included).
    _HTTP_ENTRY = ("provider: openai_compatible\n"
                   "base_url: https://x/v1\napi_key_env: K\nmodel: m\n"
                   "max_wrapper_calls: 30\nlineage_timeout_s: 2700")
    # A producing `anthropic_api` entry (for `generate.generate`) and a reading one.
    _ANTHROPIC_ENTRY = ("provider: anthropic_api\napi_key_env: K\nmodel: claude-opus-5\n"
                        "max_wrapper_calls: 30\nlineage_timeout_s: 2700")
    _ANTHROPIC_READING_ENTRY = ("provider: anthropic_api\napi_key_env: K\n"
                                "model: claude-opus-5\nlineage_timeout_s: 2700")

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
        base = "defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n  model: base\n"
        for body in (base + "phases:\n  generate: []\n",
                     base + "phases:\n  generate:\n    substeps: []\n",
                     base + "phases:\n  generate:\n    substeps:\n      verify: []\n"):
            self.assert_rule("llm_config_not_a_mapping", body)

    def test_an_absent_layer_is_still_legitimately_empty(self) -> None:
        """`generate:` with no value is the spelling that MEANS nothing, and must keep working
        — only `None` reads as empty, never as `llm_config_not_a_mapping`.

        Since issue #534 PR-J4-b no document with an empty layer LOADS: every substep entry is
        written with its bounds, so an empty layer leaves the substeps under it unwritten. So
        "read as empty" is observed as the refusal an empty layer DOES get — the bound rule,
        naming a pair under that layer — which is reached only after the layer was read as an
        empty mapping. An empty `substeps:` layer under `generate` with every other pair
        written is refused for `generate.generate`, the first pair it leaves unwritten."""
        base = "defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n  model: base\n"
        for body in (base, base + "phases:\n", base + "phases:\n  generate:\n",
                     base + "phases:\n  generate:\n    substeps:\n"):
            with self.subTest(body=body):
                self.assert_rule("llm_config_workshop_bound_missing", body)
        written_elsewhere = (
            base + "phases:\n"
            "  compile:\n    substeps:\n"
            "      generate: {max_wrapper_calls: 30, lineage_timeout_s: 2700}\n"
            "      verify: {lineage_timeout_s: 2700}\n"
            "  generate:\n    substeps:\n"
            "  validate:\n    substeps:\n"
            "      judge: {lineage_timeout_s: 2700}\n")
        err = self.assert_rule("llm_config_workshop_bound_missing", written_elsewhere)
        self.assertIn("generate.generate", str(err))
        # ...and the same document with the empty layer filled in loads, inheriting `base`.
        cfg = lc.load_llm_config(self.write(written_elsewhere.replace(
            "  generate:\n    substeps:\n",
            "  generate:\n    substeps:\n"
            "      generate: {max_wrapper_calls: 30, lineage_timeout_s: 2700}\n"
            "      verify: {lineage_timeout_s: 2700}\n"), "absent.yaml"))
        self.assertEqual(cfg.entry_for("generate", "verify").model, "base")

    def test_not_a_mapping_defaults(self) -> None:
        self.assert_rule("llm_config_not_a_mapping", "defaults: claude_cli\n")

    def test_unknown_top_level_keys_of_mixed_types(self) -> None:
        """A malformed document can mix key TYPES, and sorting those against each other raises
        a TypeError that escapes the named-rejection contract as a traceback."""
        self.assert_rule("llm_config_unknown_key",
                         "defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n1: x\nfoo: y\n")

    def test_unknown_top_level_key(self) -> None:
        self.assert_rule("llm_config_unknown_key",
                         "defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\nbackends: {}\n")

    def test_unknown_entry_key(self) -> None:
        self.assert_rule("llm_config_unknown_key",
                         "defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n  temperature: 0.2\n")

    def test_unknown_phase(self) -> None:
        self.assert_rule("llm_config_unknown_phase",
                         "defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\nphases:\n  generat: {}\n")

    def test_unknown_phase_names_build_explicitly(self) -> None:
        err = self.assert_rule(
            "llm_config_unknown_phase",
            "defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\nphases:\n  build:\n    model: x\n")
        self.assertIn("deterministic", str(err))

    def test_unknown_substep_rejects_a_deterministic_one(self) -> None:
        self.assert_rule("llm_config_unknown_substep",
                         "defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n"
                         "phases:\n  generate:\n    substeps:\n      gate:\n        model: x\n")

    def test_unknown_substep_rejects_a_typo(self) -> None:
        self.assert_rule("llm_config_unknown_substep",
                         "defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n"
                         "phases:\n  validate:\n    substeps:\n      judg:\n        model: x\n")

    def test_unknown_provider(self) -> None:
        self.assert_rule("llm_config_unknown_provider", "defaults:\n  lineage_timeout_s: 2700\n  provider: gemini_cli\n")

    def test_missing_provider(self) -> None:
        self.assert_rule("llm_config_missing_provider", "defaults:\n  lineage_timeout_s: 2700\n  model: whatever\n")

    def test_missing_provider_on_an_empty_document(self) -> None:
        self.assert_rule("llm_config_missing_provider", "")

    def test_invalid_field_type(self) -> None:
        self.assert_rule("llm_config_invalid_field",
                         "defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n  model: 12\n")

    def test_invalid_field_non_positive_timeout(self) -> None:
        self.assert_rule("llm_config_invalid_field",
                         "defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n  timeout_s: 0\n")

    def test_invalid_field_effort_level_is_per_provider(self) -> None:
        """The level names are the provider's own, so this is not one enum with different
        spellings: `ultra` exists for codex and not claude, `minimal` for an OpenAI-compatible
        endpoint and for neither CLI nor the Messages API, and `xhigh` / `max` for the Messages
        API and not an OpenAI-compatible endpoint."""
        self.assert_rule("llm_config_invalid_field",
                         "defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n  effort: ultra\n")
        self.assert_rule("llm_config_invalid_field",
                         "defaults:\n  lineage_timeout_s: 2700\n  provider: codex_cli\n  model: m\n  effort: minimal\n")
        self.assert_rule("llm_config_invalid_field",
                         "defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n  effort: enthusiastic\n")
        # `none`/`minimal` are the OpenAI API's words, not the Codex CLI's: the CLI resolves
        # against `supported_reasoning_levels`, which lists neither for any served model.
        self.assert_rule("llm_config_invalid_field",
                         "defaults:\n  lineage_timeout_s: 2700\n  provider: codex_cli\n  model: m\n  effort: none\n")
        # `minimal` is the OpenAI API's word, not the Messages API's; `xhigh` the reverse.
        self.assert_rule("llm_config_invalid_field",
                         _doc(entries={("generate", "generate"): self._ANTHROPIC_ENTRY
                                       + "\neffort: minimal"}))
        self.assert_rule("llm_config_invalid_field",
                         _doc(entries={("generate", "generate"): self._HTTP_ENTRY
                                       + "\neffort: xhigh"}))
        # ...and each accepts its own.
        for body, expected in (
            ("defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n  effort: max\n", "max"),
            ("defaults:\n  lineage_timeout_s: 2700\n  provider: codex_cli\n  model: m\n  effort: ultra\n", "ultra"),
        ):
            cfg = lc.load_llm_config(self.write(_doc(body), "levels.yaml"))
            self.assertEqual(cfg.entry_for("validate", "judge").effort, expected)
        for level in ("xhigh", "max"):
            cfg = lc.load_llm_config(self.write(_doc(entries={
                ("generate", "generate"): self._ANTHROPIC_ENTRY + f"\neffort: {level}"}),
                "levels.yaml"))
            self.assertEqual(cfg.entry_for("generate", "generate").effort, level)

    def test_effort_applies_to_the_anthropic_api_in_its_own_vocabulary(self) -> None:
        """Issue #560: the Messages API takes `output_config.effort` (the provider's effort
        page, fetched 2026-10-30), so the field applies and is checked against the API's own
        levels. It once refused the field on a thinking-token-budget premise the current
        models' API does not have."""
        cfg = lc.load_llm_config(self.write(_doc(entries={
            ("generate", "generate"): self._ANTHROPIC_ENTRY + "\neffort: high"})))
        self.assertEqual(cfg.entry_for("generate", "generate").effort, "high")
        for level in ("minimal", "ultra"):
            with self.subTest(level=level):
                err = self.assert_rule("llm_config_invalid_field", _doc(entries={
                    ("generate", "generate"): self._ANTHROPIC_ENTRY + f"\neffort: {level}"}))
                self.assertIn("accepted: high, low, max, medium, xhigh", str(err))
        # An explicitly empty effort says nothing, as on every provider with one.
        cfg = lc.load_llm_config(self.write(_doc(entries={
            ("generate", "generate"): self._ANTHROPIC_ENTRY + '\neffort: ""'})))
        self.assertEqual(cfg.entry_for("generate", "generate").effort, "")

    def test_an_effort_does_not_inherit_across_a_provider_switch(self) -> None:
        """A level is a word in the outer provider's vocabulary; carrying it into another
        provider would inherit a level it may not have. Both HTTP providers, because
        `anthropic_api` shares every word of `claude_cli`'s set — the one switch where an
        inherited level would LOAD and reach the wire unnoticed."""
        cfg = lc.load_llm_config(self.write(_doc(
            "defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n  effort: max\n",
            entries={("generate", "generate"): self._HTTP_ENTRY,
                     ("generate", "verify"): self._ANTHROPIC_READING_ENTRY,
                     ("validate", "judge"): self._ANTHROPIC_READING_ENTRY
                     + "\neffort: high"})))
        self.assertEqual(cfg.entry_for("generate", "generate").effort, "")
        self.assertEqual(cfg.entry_for("generate", "verify").effort, "")
        self.assertEqual(cfg.entry_for("validate", "judge").effort, "high")
        self.assertEqual(cfg.entry_for("compile", "verify").effort, "max")

    def test_stream_defaults_to_true_on_an_http_entry(self) -> None:
        """The default is the whole fix. A non-streaming completion writes nothing until the
        answer exists, and an intermediary that bounds the interval between upstream reads cuts
        it long before then — measured, three attempts dead after 613.6 s / 612.3 s / 611.8 s
        with `HTTP 504` while the entry's own 2400 s `timeout_s` never fired. An operator who says
        nothing gets SSE."""
        cfg = lc.load_llm_config(self.write(_doc(
            entries={("generate", "generate"): self._HTTP_ENTRY})))
        self.assertIs(cfg.entry_for("generate", "generate").stream, True)

    def test_an_explicit_stream_false_reaches_the_entry(self) -> None:
        """The escape hatch for an endpoint that cannot speak SSE at all."""
        cfg = lc.load_llm_config(self.write(_doc(
            entries={("generate", "generate"): self._HTTP_ENTRY + "\nstream: false"})))
        self.assertIs(cfg.entry_for("generate", "generate").stream, False)

    def test_a_non_boolean_stream_is_rejected(self) -> None:
        """`stream: "false"` is the one that matters: YAML reads it as a STRING, every non-bool
        here is truthy, and a truthiness test would leave streaming ON while the document says
        otherwise — reproducing the 504 the opt-out exists to escape, silently."""
        for value in ('"false"', "0", "[]", "no-thanks"):
            self.assert_rule("llm_config_invalid_field",
                             "defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n"
                             "phases:\n  generate:\n    substeps:\n      generate:\n"
                             "        provider: openai_compatible\n"
                             "        base_url: https://x/v1\n        api_key_env: K\n"
                             f"        model: m\n        stream: {value}\n")

    def test_stream_on_a_cli_entry_is_rejected_as_not_applicable(self) -> None:
        """`stream: false` on a CLI entry, which is the FALSY value — the regression test for a
        `if not fields.get(field)` guard that swallowed exactly the spelling an operator writes.
        A CLI leaf is a process; how its transport frames the answer is not configurable here."""
        self.assert_rule("llm_config_field_not_applicable",
                         "defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n  stream: false\n")
        self.assert_rule("llm_config_field_not_applicable",
                         "defaults:\n  lineage_timeout_s: 2700\n  provider: codex_cli\n  model: m\n  stream: true\n")

    def test_an_explicitly_empty_inapplicable_field_is_rejected(self) -> None:
        """The other half of the presence-not-truthiness guard, pinned deliberately rather than
        left an accident, and pinned for EVERY spelling it changed rather than one of them — the
        collateral of a guard flip is exactly what goes unnoticed. These three previously loaded
        and were silently dropped. `effort: ""` on an `anthropic_api` entry was a fourth until
        issue #560 made `effort` apply there; the sibling row below pins that it now LOADS."""
        http = ("defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n"
                "phases:\n  generate:\n    substeps:\n      generate:\n"
                "        provider: {provider}\n{extra}"
                "        api_key_env: K\n        model: m\n")
        for body in (
            'defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n  base_url: ""\n',
            'defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n  api_key_env: ""\n',
            http.format(provider="openai_compatible",
                        extra='        base_url: https://x/v1\n        command: ""\n'),
        ):
            with self.subTest(body=body):
                self.assert_rule("llm_config_field_not_applicable", body)

    def test_an_explicitly_empty_effort_on_the_anthropic_api_loads(self) -> None:
        """The former fourth member of the row above: since issue #560 `effort` applies to
        `anthropic_api`, so `effort: ""` is the "say nothing" spelling it is everywhere else,
        not an inapplicable field."""
        cfg = lc.load_llm_config(self.write(_doc(entries={
            ("generate", "generate"): self._ANTHROPIC_ENTRY + '\neffort: ""'})))
        self.assertEqual(cfg.entry_for("generate", "generate").effort, "")
        self.assertEqual(cfg.entry_for("generate", "generate").provider, "anthropic_api")

    def test_an_empty_timeout_is_rejected_by_its_own_rule_not_this_one(self) -> None:
        """The near neighbour that is NOT part of the guard flip: an empty `timeout_s` never
        loaded, on any provider, because `_layer_fields` rejects it first and as a different
        rule. Pinned so the comment enumerating the flip's collateral cannot quietly grow it."""
        self.assert_rule("llm_config_invalid_field",
                         "defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n  timeout_s:\n")

    def test_stream_survives_a_switch_between_two_http_providers(self) -> None:
        """Deliberately NOT provider-scoped: it describes the endpoint's transport behaviour, not
        a provider's vocabulary, and two leaves on the same gateway must both keep the opt-out."""
        cfg = lc.load_llm_config(self.write(_doc(
            entries={("generate", "verify"):
                     "provider: anthropic_api\n"
                     "api_key_env: ANTHROPIC_API_KEY\nmodel: claude-opus-5\n"
                     "lineage_timeout_s: 2700"},
            phase_fields={"generate": "provider: openai_compatible\n"
                                      "base_url: https://x/v1\napi_key_env: K\nmodel: m\n"
                                      "stream: false"})))
        self.assertIs(cfg.entry_for("generate", "generate").stream, False)
        self.assertIs(cfg.entry_for("generate", "verify").stream, False)

    def test_stream_inherited_onto_a_cli_substep_is_dropped_not_rejected(self) -> None:
        """Nothing in the document points at that leaf, so there is nothing to correct — the
        same rule `timeout_s` already follows when it inherits onto a CLI leaf."""
        cfg = lc.load_llm_config(self.write(_doc(
            entries={("generate", "verify"):
                     "{provider: claude_cli, model: opus, lineage_timeout_s: 2700}"},
            phase_fields={"generate": "provider: openai_compatible\n"
                                      "base_url: https://x/v1\napi_key_env: K\nmodel: m\n"
                                      "stream: false"})))
        self.assertIs(cfg.entry_for("generate", "generate").stream, False)
        self.assertIs(cfg.entry_for("generate", "verify").stream, True)

    def test_invalid_field_non_finite_timeout(self) -> None:
        """YAML reads `.nan` / `.inf` as floats and both slip past a `<= 0` test, so a config
        advertising a positive wall-clock bound would carry a deadline that never fires."""
        for value in (".nan", ".inf"):
            self.assert_rule("llm_config_invalid_field",
                             "defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n"
                             "phases:\n  generate:\n    substeps:\n      generate:\n"
                             "        provider: openai_compatible\n"
                             "        base_url: https://x/v1\n        api_key_env: K\n"
                             f"        model: m\n        timeout_s: {value}\n")

    def test_invalid_field_unknown_capability_name(self) -> None:
        self.assert_rule("llm_config_invalid_field",
                         "defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n  capabilities: [teleport]\n")

    def test_a_retired_capability_is_refused_not_ignored(self) -> None:
        """`usage_probe` (retired by issue #170, with `agentic` / `mcp_tools` before it, and
        `pure` after it in issue #534 PR-J4-b) is not in `KNOWN_CAPABILITIES`, so a
        configuration still spelling it fails at parse time naming the accepted set — never
        accepted and silently dropped."""
        for retired in ("usage_probe", "agentic", "mcp_tools", "pure"):
            with self.subTest(retired=retired):
                self.assertNotIn(retired, lc.KNOWN_CAPABILITIES)
                self.assert_rule(
                    "llm_config_invalid_field",
                    _doc("defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n"
                         f"  capabilities: [workshop, {retired}]\n"))

    def test_field_not_applicable_http_field_on_a_cli_provider(self) -> None:
        self.assert_rule("llm_config_field_not_applicable",
                         "defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n  base_url: http://x/v1\n")

    def test_field_not_applicable_command_on_an_http_provider(self) -> None:
        self.assert_rule("llm_config_field_not_applicable",
                         "defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n"
                         "phases:\n  generate:\n    substeps:\n      generate:\n"
                         "        provider: openai_compatible\n"
                         "        base_url: http://localhost:8000/v1\n"
                         "        api_key_env: LOCAL_KEY\n"
                         "        model: m\n"
                         "        command: vllm-wrapper\n")

    def test_http_requires_base_url(self) -> None:
        self.assert_rule("llm_config_http_requires_base_url",
                         "defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n"
                         "phases:\n  generate:\n    substeps:\n      generate:\n"
                         "        provider: openai_compatible\n"
                         "        api_key_env: LOCAL_KEY\n"
                         "        model: m\n")

    def test_http_requires_api_key_env(self) -> None:
        self.assert_rule("llm_config_http_requires_api_key_env",
                         "defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n"
                         "phases:\n  generate:\n    substeps:\n      generate:\n"
                         "        provider: openai_compatible\n"
                         "        base_url: http://localhost:8000/v1\n"
                         "        model: m\n")

    def test_http_requires_model(self) -> None:
        self.assert_rule("llm_config_http_requires_model",
                         "defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n"
                         "phases:\n  generate:\n    substeps:\n      generate:\n"
                         "        provider: openai_compatible\n"
                         "        base_url: http://localhost:8000/v1\n"
                         "        api_key_env: LOCAL_KEY\n")

    def _http(self, url: str) -> str:
        return _doc(entries={("generate", "generate"):
                             "provider: openai_compatible\napi_key_env: LOCAL_KEY\n"
                             f"model: m\nbase_url: {url}\n"
                             "max_wrapper_calls: 30\nlineage_timeout_s: 2700"})

    def test_insecure_base_url(self) -> None:
        """Plain http to a non-loopback host sends the API key in cleartext. The realistic case
        is a typo or a LAN address, not a deliberate choice."""
        err = self.assert_rule("llm_config_insecure_base_url",
                               self._http("http://192.168.1.9:8000/v1"))
        self.assertIn("cleartext", str(err))

    def test_loopback_http_is_allowed(self) -> None:
        """Where the local servers this provider exists for run; the traffic never leaves the
        host, and requiring https there would make the headline use case impossible."""
        for url in ("http://localhost:8000/v1", "http://127.0.0.1:8000/v1",
                    "http://[::1]:8000/v1", "http://myhost.localhost:8000/v1"):
            cfg = lc.load_llm_config(self.write(self._http(url), "lb.yaml"))
            self.assertEqual(cfg.entry_for("generate", "generate").base_url, url)

    def test_https_is_allowed_anywhere(self) -> None:
        cfg = lc.load_llm_config(
            self.write(self._http("https://api.example.com/v1")))
        self.assertEqual(cfg.entry_for("generate", "generate").base_url,
                         "https://api.example.com/v1")

    def test_insecure_base_url_has_an_explicit_opt_in(self) -> None:
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {lc._INSECURE_BASE_URL_OPT_IN_ENV: "1"}, clear=False):
            cfg = lc.load_llm_config(
                self.write(self._http("http://192.168.1.9:8000/v1")))
        self.assertEqual(cfg.entry_for("generate", "generate").base_url,
                         "http://192.168.1.9:8000/v1")

    def test_an_unhashable_mapping_key_is_a_named_rejection(self) -> None:
        """`? [a, b]` is legal YAML and produces a LIST key. Testing it for membership raised
        a bare TypeError, which escapes the startup envelope as a traceback rather than the
        named rejection every other malformed document gets."""
        self.assert_rule("llm_config_unknown_key",
                         "defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n? [a, b]\n: x\n")

    def test_content_parses_the_bytes_it_was_given(self) -> None:
        """One snapshot: a caller that must hash, parse and act on the same bytes cannot read
        the file twice, or the two answers can describe different versions."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.yaml"
            path.write_text(_doc("defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n"
                                 "  model: on-disk\n"), encoding="utf-8")
            snapshot = _doc("defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n"
                            "  model: in-hand\n").encode("utf-8")
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
                         "defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n"
                         "phases:\n  generate:\n    model: a\n  generate:\n    model: b\n")

    def test_duplicate_substep_key(self) -> None:
        self.assert_rule("llm_config_duplicate_key",
                         "defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n"
                         "phases:\n  generate:\n    substeps:\n"
                         "      verify:\n        model: a\n      verify:\n        model: b\n")

    def test_duplicate_phase_after_normalization(self) -> None:
        """`generate` and `" generate "` are distinct YAML keys that name the same phase, so
        the duplicate-key loader does not see them — the later one silently replaced the
        earlier provider/model assignment."""
        self.assert_rule("llm_config_duplicate_key",
                         'defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n'
                         'phases:\n  generate:\n    model: a\n  " generate ":\n    model: b\n')

    def test_duplicate_substep_after_normalization(self) -> None:
        self.assert_rule("llm_config_duplicate_key",
                         'defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n'
                         'phases:\n  generate:\n    substeps:\n      verify:\n'
                         '        model: a\n        lineage_timeout_s: 2700\n'
                         '      " verify ":\n        model: b\n        lineage_timeout_s: 2700\n')

    def test_duplicate_field_key(self) -> None:
        self.assert_rule("llm_config_duplicate_key",
                         "defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n  model: a\n  model: b\n")

    def test_capability_exceeds_provider(self) -> None:
        """An HTTP provider declares `workshop` and nothing else, so naming any second
        capability is the superset this rule refuses. `warm_resume` is the probe because it is
        a real capability the CLI providers have and the HTTP ones do not — `agentic`, which
        stood here until Z4 (issue #171), and `pure`, which stood beside `workshop` until issue
        #534 PR-J4-b, are no longer capability names at all and are refused one rule earlier,
        by `llm_config_invalid_field`."""
        err = self.assert_rule(
            "llm_config_capability_exceeds_provider",
            _doc(entries={("generate", "generate"):
                          "provider: openai_compatible\n"
                          "base_url: http://localhost:8000/v1\n"
                          "api_key_env: LOCAL_KEY\n"
                          "model: m\n"
                          "capabilities: [workshop, warm_resume]\n"
                          "max_wrapper_calls: 30\nlineage_timeout_s: 2700"}))
        self.assertIn("warm_resume", str(err))

    def test_capability_insufficient_after_a_restriction(self) -> None:
        """The rule is about the RESOLVED capability set, not the provider name: a claude entry
        restricted until it no longer holds `workshop` is inadmissible on every LLM leaf,
        exactly as a provider declaring no `workshop` would be.

        The restriction has had to change several times. It was `[pure]` on `validate.judge`
        while that was the last agentic-only leaf; Z3 (issue #169) made the judge pure-capable;
        Z4 (issue #171) left one transport, `pure`; and issue #534 PR-J4-b replaced it with
        `workshop`, so `[warm_resume]` is now simply an entry without `workshop`."""
        err = self.assert_rule("llm_config_capability_insufficient_for_substep", _doc(
            entries={("validate", "judge"):
                     "capabilities: [warm_resume]\nlineage_timeout_s: 2700"}))
        self.assertIn("requires capability 'workshop'", str(err))
        self.assertEqual(err.where, "phases.validate.substeps.judge")

    def test_defaults_not_workshop(self) -> None:
        """`defaults` serves the escalate diagnostician, a READING workshop launch (issue #534
        PR-J4-a; a pure one from issue #169 until then), so an entry restricted away from
        `workshop` is inadmissible there. The restriction is the only way to reach this — every
        declared provider supports `workshop` and is a reading provider — which is why the
        subject is `capabilities:` rather than a provider name. It is also the rule that fires
        FIRST for such a document, ahead of the per-substep one, which is why `defaults` is
        where the restriction is written."""
        err = self.assert_rule("llm_config_defaults_not_workshop",
                               _doc("defaults:\n  lineage_timeout_s: 2700\n"
                                    "  provider: claude_cli\n"
                                    "  capabilities: [warm_resume]\n"))
        self.assertIn("escalate", str(err))

    def test_defaults_states_the_diagnosticians_bound(self) -> None:
        """`defaults` is resolved as a reading workshop entry by the loader: `lineage_timeout_s`
        is required there, `max_wrapper_calls` stays refused as substep-scoped, and a written
        `leaf:` is refused as an unknown key (issue #534 PR-J4-b retired the field)."""
        err = self.assert_rule("llm_config_workshop_bound_missing",
                               _doc("defaults:\n  provider: claude_cli\n"))
        self.assertIn("`defaults` must state lineage_timeout_s", str(err))
        self.assertEqual(err.where, "defaults")
        err = self.assert_rule("llm_config_workshop_field_not_substep_scoped",
                               _doc(_DEFAULTS + "  max_wrapper_calls: 3\n"))
        self.assertEqual(err.where, "defaults.max_wrapper_calls")
        for value in ("workshop", "pure"):
            with self.subTest(leaf=value):
                err = self.assert_rule("llm_config_unknown_key",
                                       _doc(_DEFAULTS + f"  leaf: {value}\n"))
                self.assertIn("'leaf'", str(err))
        cfg = lc.load_llm_config(self.write(_doc(
            "defaults:\n  provider: claude_cli\n  lineage_timeout_s: 1234\n")))
        self.assertEqual((cfg.defaults.lineage_timeout_s, cfg.defaults.max_wrapper_calls),
                         (1234.0, None))
        self.assertEqual(cfg.defaults.provenance()["lineage_timeout_s"], "1234.0")
        self.assertNotIn("max_wrapper_calls", cfg.defaults.provenance())
        self.assertNotIn("leaf", cfg.defaults.provenance())
        self.assertFalse(hasattr(cfg.defaults, "leaf"))

    def test_the_defaults_bound_is_not_inherited(self) -> None:
        """`defaults.lineage_timeout_s` bounds the diagnostician alone: a substep entry does
        not receive it. Each entry carries the bound IT states (60 here against `defaults`'
        2700), and an entry that states none is refused `bound_missing` rather than inheriting
        it — written with other fields, or not written at all. A phase-level bound is refused
        as not substep-scoped, so no level between them can carry one down either."""
        cfg = lc.load_llm_config(self.write(_doc(entries={
            ("compile", "generate"): "{max_wrapper_calls: 30, lineage_timeout_s: 60}",
            ("compile", "verify"): "{lineage_timeout_s: 60}",
            ("generate", "generate"): "{max_wrapper_calls: 30, lineage_timeout_s: 60}",
            ("generate", "verify"): "{lineage_timeout_s: 60}",
            ("validate", "judge"): "{lineage_timeout_s: 60}"})))
        self.assertEqual(cfg.defaults.lineage_timeout_s, 2700.0)
        for key, entry in cfg.entries.items():
            with self.subTest(pair=key):
                self.assertEqual(entry.lineage_timeout_s, 60.0)
        err = self.assert_rule("llm_config_workshop_bound_missing",
                               _doc(entries={("validate", "judge"): "{model: opus}"}))
        self.assertIn("missing: lineage_timeout_s", str(err))
        self.assertEqual(err.where, "phases.validate.substeps.judge")
        self.assert_rule("llm_config_workshop_field_not_substep_scoped",
                         _doc(phase_fields={"validate": "lineage_timeout_s: 30"}))

    def test_an_http_defaults_loads_and_runs_beside_a_cli_leaf(self) -> None:
        """`defaults` serves the `escalate` diagnostician, a reading workshop leaf that both
        HTTP providers can run, so an HTTP provider gets past `load_llm_config` AND reaches a
        run.

        The second half is not automatic. `init` and the preflight still take one backend
        token whose parser is `choices={claude, codex}`; measured before `cli_launch_identity`
        existed, this file reached `init` and died on `argument --agent-backend: invalid
        choice: 'anthropic_api'`. Refusing the file instead would have left the
        diagnostician's migration undelivered on two DECLARED providers, and `AGENTS.md`
        §Development premises does not allow that. So the token is resolved from what the
        configuration can LAUNCH, and this row is that shape: an HTTP `defaults` beside a CLI
        `validate.judge`."""
        cfg = lc.load_llm_config(self.write(_doc(
            "defaults:\n  lineage_timeout_s: 2700\n"
            "  provider: anthropic_api\n"
            "  api_key_env: ANTHROPIC_API_KEY\n"
            "  model: claude-opus-5\n",
            entries={("validate", "judge"):
                     "{provider: claude_cli, lineage_timeout_s: 2700}"}), "http_defaults.yaml"))
        self.assertEqual(cfg.defaults.provider, "anthropic_api")
        self.assertTrue(cfg.defaults.is_http)
        cfg.validate_runnable()
        # ...and the single CLI token `init` and the preflight speak comes from what the file
        # can actually LAUNCH, not from `defaults`. Refusing the file instead would leave the
        # diagnostician's migration undelivered on two declared providers, which
        # `AGENTS.md` §Development premises does not allow.
        self.assertEqual(cfg.cli_launch_identity(), ("claude", "claude"))
        # A CLI `defaults` still answers for itself, command included.
        cli = lc.load_llm_config(self.write(_doc(
            "defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n"
            "  command: /opt/claude --flag\n"), "cli_defaults.yaml"))
        cli.validate_runnable()
        self.assertEqual(cli.cli_launch_identity(), ("claude", "/opt/claude --flag"))

    def test_a_configuration_that_launches_nothing_is_refused_by_name(self) -> None:
        """The floor under `cli_launch_identity`. Z3 (issue #169) made an all-HTTP file LOAD,
        so this is reached from a loaded document: an HTTP `defaults` that every substep entry
        inherits its provider from (each writes only its bounds)."""
        cfg = lc.load_llm_config(self.write(_doc(
            "defaults:\n  lineage_timeout_s: 2700\n"
            "  provider: anthropic_api\n  api_key_env: ANTHROPIC_API_KEY\n"
            "  model: claude-opus-5\n"), "all_http.yaml"))
        self.assertEqual(cfg.providers, frozenset({"anthropic_api"}))
        with self.assertRaises(lc.LlmConfigError) as ctx:
            cfg.cli_launch_identity()
        self.assertEqual(ctx.exception.rule, "llm_config_no_cli_backend")
        # ...and RUN START is where it arrives. Calling the resolver directly leaves the hook
        # in `validate_runnable` unpinned: deleting that one line left this file green.
        with self.assertRaises(lc.LlmConfigError) as run_start:
            cfg.validate_runnable()
        self.assertEqual(run_start.exception.rule, "llm_config_no_cli_backend")
        # The remedy names both halves, so it cannot be followed by half.
        self.assertIn("validate.judge", str(ctx.exception).replace("`", ""))
        self.assertIn("defaults", str(ctx.exception))

    def test_codex_requires_model_only_at_run_start(self) -> None:
        cfg = lc.load_llm_config(self.write(_doc(
            "defaults:\n  lineage_timeout_s: 2700\n  provider: codex_cli\n")))
        with self.assertRaises(lc.LlmConfigError) as ctx:
            cfg.validate_runnable()
        self.assertEqual(ctx.exception.rule, "llm_config_codex_cli_requires_model")
        lc.load_llm_config(self.write(_doc(
            "defaults:\n  lineage_timeout_s: 2700\n  provider: codex_cli\n  model: some-slug\n"
        ))).validate_runnable()

    def test_codex_requires_model_rejects_the_generic_alias_too(self) -> None:
        """`codex` is an alias the CLI resolves to whatever it currently prefers, so a leaf
        launched with it records provenance that does not name what ran — as unusable as no
        model at all. Caught HERE, at run start, because the run-wide flag guard that used to
        catch it is gone and everything later has already created an orchestration."""
        for slug in ("codex", "CODEX", "  codex  "):
            cfg = lc.load_llm_config(self.write(_doc(
                "defaults:\n  lineage_timeout_s: 2700\n  provider: codex_cli\n"
                f"  model: {slug!r}\n"), "alias.yaml"))
            with self.assertRaises(lc.LlmConfigError) as ctx:
                cfg.validate_runnable()
            self.assertEqual(ctx.exception.rule, "llm_config_codex_cli_requires_model",
                             msg=slug)
            self.assertIn("explicit slug", str(ctx.exception), msg=slug)
        # ...and a real slug that merely CONTAINS it is fine.
        lc.load_llm_config(self.write(_doc(
            "defaults:\n  lineage_timeout_s: 2700\n  provider: codex_cli\n  model: gpt-5.6-codex\n"),
            "real.yaml")).validate_runnable()

    def test_codex_requires_model_catches_a_per_substep_omission(self) -> None:
        cfg = lc.load_llm_config(self.write(_doc(entries={
            ("validate", "judge"): "{provider: codex_cli, lineage_timeout_s: 2700}"})))
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


def _without_entry(text: str, phase: str, substep: str) -> str:
    """`text` (a `_doc` document) with the one line writing `phases.<phase>.substeps.<substep>`
    removed, so that substep is UNWRITTEN. Asserted to remove exactly one line."""
    lines = text.splitlines()
    head = lines.index(f"  {phase}:")
    tail = next((i for i in range(head + 1, len(lines))
                 if lines[i].startswith("  ") and not lines[i].startswith("   ")), len(lines))
    hits = [i for i in range(head, tail) if lines[i].startswith(f"      {substep}: {{")]
    assert len(hits) == 1, (phase, substep, hits)
    del lines[hits[0]]
    return "\n".join(lines) + "\n"


class WorkshopEntryTests(_Tmp):
    """The two bounds every LLM substep entry states, now that every LLM leaf is a `workshop
    leaf` (issue #534 PR-J4-b), and the `leaf:` field that PR retired."""

    BOUNDS = "max_wrapper_calls: 30, lineage_timeout_s: 2700"
    READING = "lineage_timeout_s: 2700"

    def test_every_entry_resolves_with_the_bounds_it_states(self) -> None:
        """Each kind with the bounds it is served: a producing entry both, a judging entry
        `lineage_timeout_s` alone. The provenance row names the bounds the entry has — never a
        `"None"` — and no `leaf`, and the resolved entry has no `leaf` / `is_workshop`: there
        is one leaf model, so nothing selects or reports one."""
        self.assertEqual(lc.WORKSHOP_LEAF_SUBSTEPS, lc.LLM_LEAF_SUBSTEPS)
        cfg = lc.load_llm_config(self.write(_doc(entries={
            ("compile", "generate"): "{max_wrapper_calls: 11, lineage_timeout_s: 101}",
            ("compile", "verify"): "{lineage_timeout_s: 102}",
            ("generate", "generate"): "{max_wrapper_calls: 13, lineage_timeout_s: 103}",
            ("generate", "verify"): "{lineage_timeout_s: 104}",
            ("validate", "judge"): "{lineage_timeout_s: 105}"})))
        expected = {("compile", "generate"): (11, 101.0), ("compile", "verify"): (None, 102.0),
                    ("generate", "generate"): (13, 103.0), ("generate", "verify"): (None, 104.0),
                    ("validate", "judge"): (None, 105.0)}
        self.assertEqual(set(expected), set(lc.LLM_LEAF_SUBSTEPS))
        for key, (calls, timeout) in expected.items():
            with self.subTest(pair=key):
                entry = cfg.entry_for(*key)
                self.assertEqual((entry.max_wrapper_calls, entry.lineage_timeout_s),
                                 (calls, timeout))
                bounds = {k: v for k, v in entry.provenance().items()
                          if k in ("leaf", "max_wrapper_calls", "lineage_timeout_s")}
                self.assertEqual(bounds, {"lineage_timeout_s": str(timeout)}
                                 | ({} if calls is None else {"max_wrapper_calls": str(calls)}))
                self.assertFalse(hasattr(entry, "leaf"))
                self.assertFalse(hasattr(entry, "is_workshop"))

    def test_a_written_leaf_is_refused_as_an_unknown_key(self) -> None:
        """Issue #534 PR-J4-b retired `leaf:` with the pure leaf: whatever it names — the one
        model there is, the deleted one, or a model that never existed — it is refused
        `llm_config_unknown_key` at every level, never accepted and ignored."""
        for value in ("workshop", "pure", "agentic"):
            for where, text in (
                ("phases.generate.substeps.generate.leaf", _doc(entries={
                    ("generate", "generate"): f"{{leaf: {value}, {self.BOUNDS}}}"})),
                ("phases.validate.substeps.judge.leaf", _doc(entries={
                    ("validate", "judge"): f"{{leaf: {value}, {self.READING}}}"})),
                ("phases.generate.leaf", _doc(phase_fields={"generate": f"leaf: {value}"})),
                ("defaults.leaf", _doc(_DEFAULTS + f"  leaf: {value}\n")),
            ):
                with self.subTest(leaf=value, where=where):
                    err = self.assert_rule("llm_config_unknown_key", text)
                    self.assertEqual(err.where, where)
                    self.assertNotIn("leaf", lc._ENTRY_FIELDS)
                    # The refusal names the migration (a stale document still says to write it).
                    self.assertIn("retired in issue #534 PR-J4-b", str(err))
                    self.assertIn("delete the line", str(err))
        # Another unknown key is not told about `leaf:`.
        err = self.assert_rule("llm_config_unknown_key",
                               _doc(phase_fields={"generate": "flavour: x"}))
        self.assertNotIn("retired", str(err))

    def test_the_bounds_are_typed(self) -> None:
        for bounds in ("max_wrapper_calls: 0, lineage_timeout_s: 10",
                       "max_wrapper_calls: true, lineage_timeout_s: 10",
                       "max_wrapper_calls: 2.5, lineage_timeout_s: 10",
                       "max_wrapper_calls: 3, lineage_timeout_s: 0",
                       "max_wrapper_calls: 3, lineage_timeout_s: .inf",
                       "max_wrapper_calls: 3, lineage_timeout_s: x"):
            with self.subTest(bounds=bounds):
                self.assert_rule("llm_config_invalid_field", _doc(entries={
                    ("generate", "generate"): f"{{{bounds}}}"}))

    def test_workshop_fields_are_substep_scoped(self) -> None:
        """Both bounds are refused at a phase, and `max_wrapper_calls` at `defaults` (which
        states `lineage_timeout_s` for the diagnostician it serves, and nothing else)."""
        for where, body in (
            ("defaults.max_wrapper_calls", _doc(_DEFAULTS + "  max_wrapper_calls: 3\n")),
            ("phases.generate.lineage_timeout_s",
             _doc(phase_fields={"generate": "lineage_timeout_s: 30"})),
            ("phases.compile.max_wrapper_calls",
             _doc(phase_fields={"compile": "max_wrapper_calls: 3"})),
            ("phases.validate.lineage_timeout_s",
             _doc(phase_fields={"validate": "lineage_timeout_s: 30"})),
        ):
            with self.subTest(where=where):
                err = self.assert_rule("llm_config_workshop_field_not_substep_scoped", body)
                self.assertEqual(err.where, where)

    def test_a_codex_workshop_entry_loads(self) -> None:
        """Issue #534 step 2 PR-2: `codex_cli` declares `workshop`, on both producing substeps."""
        for phase in ("generate", "compile"):
            with self.subTest(phase=phase):
                cfg = lc.load_llm_config(self.write(_doc(entries={
                    (phase, "generate"): f"{{provider: codex_cli, model: gpt-x, {self.BOUNDS}}}"})))
                entry = cfg.entry_for(phase, "generate")
                self.assertEqual((entry.provider, entry.max_wrapper_calls,
                                  entry.lineage_timeout_s), ("codex_cli", 30, 2700.0))

    def test_every_substep_entry_states_the_bounds_of_its_kind(self) -> None:
        """Every written substep entry states `lineage_timeout_s`, and a producing one
        `max_wrapper_calls` too; there is no default bound. Driven on every pair."""
        for (phase, substep) in sorted(lc.LLM_LEAF_SUBSTEPS):
            producing = (phase, substep) in lc.WORKSHOP_PRODUCING_SUBSTEPS
            bodies = ["{model: opus}"]
            if producing:
                bodies += ["{max_wrapper_calls: 3}", "{lineage_timeout_s: 30}"]
            for body in bodies:
                with self.subTest(pair=f"{phase}.{substep}", body=body):
                    err = self.assert_rule("llm_config_workshop_bound_missing",
                                           _doc(entries={(phase, substep): body}))
                    self.assertEqual(err.where, f"phases.{phase}.substeps.{substep}")
                    self.assertIn(f"{phase}.{substep}", str(err))

    def test_an_unwritten_substep_is_refused_bound_missing(self) -> None:
        """A substep entry inherited whole — from `defaults` alone, or from a phase that writes
        fields of its own — states no bound, and none is inherited, so it is refused rather
        than run unbounded. Driven by removing each pair in turn from a complete document."""
        for (phase, substep) in sorted(lc.LLM_LEAF_SUBSTEPS):
            with self.subTest(pair=f"{phase}.{substep}", inherits="defaults"):
                err = self.assert_rule("llm_config_workshop_bound_missing",
                                       _without_entry(_doc(), phase, substep))
                self.assertIn(f"{phase}.{substep}", str(err))
                # The location is where the fix is written, not the level the entry was
                # inherited from (`defaults` here).
                self.assertEqual(err.where, f"phases.{phase}.substeps.{substep}")
            with self.subTest(pair=f"{phase}.{substep}", inherits="phase"):
                err = self.assert_rule("llm_config_workshop_bound_missing", _without_entry(
                    _doc(phase_fields={phase: "model: phase-model"}), phase, substep))
                self.assertIn(f"{phase}.{substep}", str(err))
                self.assertEqual(err.where, f"phases.{phase}.substeps.{substep}")
        # ...and the complete document the rows above remove from loads.
        lc.load_llm_config(self.write(_doc(phase_fields={"compile": "model: phase-model"})))

    def test_a_judging_workshop_entry_is_served_no_wrapper_call_budget(self) -> None:
        """A reading lineage is served no operation, so `max_wrapper_calls` there bounds
        nothing and is refused; `lineage_timeout_s` is still required."""
        for (phase, substep) in sorted(lc.WORKSHOP_READING_SUBSTEPS):
            with self.subTest(pair=f"{phase}.{substep}"):
                err = self.assert_rule("llm_config_workshop_bound_not_served",
                                       _doc(entries={(phase, substep): f"{{{self.BOUNDS}}}"}))
                self.assertEqual(err.where, f"phases.{phase}.substeps.{substep}.max_wrapper_calls")
                self.assert_rule("llm_config_workshop_bound_missing",
                                 _doc(entries={(phase, substep): "{max_wrapper_calls: 3}"}))

    def test_a_judging_workshop_entry_runs_on_a_measured_provider_only(self) -> None:
        """Both CLIs' reading argv are measured (codex's in step 3 PR-J2), so a codex judging
        entry loads on each judging substep. The refusal stays for a provider that declares
        `workshop` with no reader row, at load rather than at launch."""
        for (phase, substep) in sorted(lc.WORKSHOP_READING_SUBSTEPS):
            with self.subTest(pair=f"{phase}.{substep}"):
                text = _doc(entries={(phase, substep):
                                     f"{{provider: codex_cli, model: m, {self.READING}}}"})
                entry = lc.load_llm_config(self.write(text)).entry_for(phase, substep)
                self.assertEqual(entry.provider, "codex_cli")
                with mock.patch.object(lc, "WORKSHOP_READING_PROVIDERS",
                                       frozenset({"claude_cli"})):
                    err = self.assert_rule(
                        "llm_config_workshop_reading_unsupported_on_provider", text)
                    self.assertEqual(err.where, f"phases.{phase}.substeps.{substep}.provider")
        # The producing substeps run on codex as before.
        lc.load_llm_config(self.write(_doc(entries={
            ("generate", "generate"): f"{{provider: codex_cli, model: m, {self.BOUNDS}}}"})))

    def test_an_entry_narrowed_away_from_workshop_is_refused(self) -> None:
        """An entry without `workshop` can run no LLM leaf, and is refused
        `llm_config_capability_insufficient_for_substep` — the rule that, since issue #534
        PR-J4-b, covers what `llm_config_workshop_unsupported_on_provider` refused (its
        predicate became this one's). Reached two ways: a provider whose table row lacks it (no
        provider lacks it today, so the table is narrowed for the row), and `capabilities:`
        narrowing it away."""
        http = "provider: anthropic_api, model: m, api_key_env: K"
        with mock.patch.dict(lc.PROVIDER_CAPABILITIES, {"anthropic_api": frozenset()}):
            err = self.assert_rule(
                "llm_config_capability_insufficient_for_substep",
                _doc(entries={("generate", "generate"): f"{{{http}, {self.BOUNDS}}}"}))
            self.assertIn("'workshop'", str(err))
        for (phase, substep), body in (
            (("generate", "generate"), f"{{capabilities: [warm_resume], {self.BOUNDS}}}"),
            (("compile", "verify"), f"{{capabilities: [warm_resume], {self.READING}}}"),
        ):
            with self.subTest(pair=f"{phase}.{substep}"):
                err = self.assert_rule("llm_config_capability_insufficient_for_substep",
                                       _doc(entries={(phase, substep): body}))
                self.assertEqual(err.where, f"phases.{phase}.substeps.{substep}")

    def test_workshop_is_the_capability_every_llm_entry_requires(self) -> None:
        """`required_capabilities` takes no leaf model any more (there is one) and answers
        `{workshop}` for every LLM pair; an entry narrowed to `workshop` alone runs."""
        import inspect
        self.assertEqual(list(inspect.signature(lc.required_capabilities).parameters),
                         ["phase", "substep"])
        for phase, substep in sorted(lc.LLM_LEAF_SUBSTEPS):
            self.assertEqual(lc.required_capabilities(phase, substep),
                             frozenset({lc.CAP_WORKSHOP}), msg=f"{phase}.{substep}")
        cfg = lc.load_llm_config(self.write(_doc(entries={
            ("generate", "generate"): f"{{capabilities: [workshop], {self.BOUNDS}}}"})))
        self.assertEqual(cfg.entry_for("generate", "generate").capabilities,
                         frozenset({lc.CAP_WORKSHOP}))

    def test_a_complete_configuration_passes_its_run_start(self) -> None:
        """The run-start check refuses nothing a complete, bounded configuration states."""
        lc.load_llm_config(self.write(_doc())).validate_runnable()

    @staticmethod
    def _commented_block(sample: str, head: str) -> list[str]:
        lines = (SAMPLE_DIR / sample).read_text(encoding="utf-8").splitlines()
        start = lines.index(f"#       {head}:")
        block = []
        for line in lines[start:]:
            if not line.startswith("#       "):
                break
            block.append(line[len("#       "):].split("#", 1)[0].rstrip())
        return block

    def test_the_http_samples_producing_block_loads_on_each_producing_substep(self) -> None:
        """Issue #534 PR-J3-b: each HTTP sample's commented PRODUCING block, uncommented and
        loaded under both producing substeps, is an HTTP entry with both bounds — and writes no
        `leaf:`, which would be refused."""
        for sample, provider in (("llm_anthropic_api.example.yaml", "anthropic_api"),
                                 ("llm_openai_compatible.example.yaml", "openai_compatible")):
            block = self._commented_block(sample, "generate")
            self.assertFalse([line for line in block if line.strip().startswith("leaf:")])
            for phase in ("compile", "generate"):
                with self.subTest(sample=sample, phase=phase):
                    cfg = lc.load_llm_config(self.write(_doc(entries={
                        (phase, "generate"): "\n".join(line.strip() for line in block[1:])})))
                    cfg.validate_runnable()
                    entry = cfg.entry_for(phase, "generate")
                    self.assertEqual((entry.provider, entry.is_http), (provider, True))
                    self.assertIsNotNone(entry.max_wrapper_calls)
                    self.assertIsNotNone(entry.lineage_timeout_s)

    def test_the_http_samples_reading_block_loads_on_each_judging_substep(self) -> None:
        """Each HTTP sample's commented READING block (issue #534 step 3) is uncommented and
        loaded under each judging substep: a block that stops loading teaches a refused
        configuration. (The CLI samples carry no such block since issue #534 PR-J4-b: their
        live entries ARE the workshop entries, loaded by
        `test_every_sample_loads_resolves_every_leaf_and_is_runnable` and read by
        `test_every_sample_entry_states_the_bounds_of_its_kind_and_no_leaf`.)"""
        for sample, provider in (("llm_anthropic_api.example.yaml", "anthropic_api"),
                                 ("llm_openai_compatible.example.yaml", "openai_compatible")):
            block = self._commented_block(sample, "judge")
            self.assertIn(f"  provider: {provider}", block)
            self.assertNotIn("max_wrapper_calls", "\n".join(block))
            self.assertFalse([line for line in block if line.strip().startswith("leaf:")])
            for phase, substep in sorted(lc.WORKSHOP_READING_SUBSTEPS):
                with self.subTest(sample=sample, pair=f"{phase}.{substep}"):
                    cfg = lc.load_llm_config(self.write(_doc(entries={
                        (phase, substep): "\n".join(line.strip() for line in block[1:])})))
                    cfg.validate_runnable()
                    entry = cfg.entry_for(phase, substep)
                    self.assertEqual(entry.provider, provider)
                    self.assertIsNone(entry.max_wrapper_calls)
                    self.assertIsNotNone(entry.lineage_timeout_s)

    def test_an_http_workshop_entry_loads_on_every_substep(self) -> None:
        """Both HTTP providers run a workshop leaf on every LLM pair, a producer with both
        bounds and a reader with `lineage_timeout_s` alone; a bound at `defaults` or at a
        phase is still refused (substep-scoped)."""
        for provider, extra in (("anthropic_api", "api_key_env: K"),
                                ("openai_compatible",
                                 "base_url: 'http://127.0.0.1:8000/v1', api_key_env: K")):
            for phase, substep in sorted(lc.LLM_LEAF_SUBSTEPS):
                reading = (phase, substep) in lc.WORKSHOP_READING_SUBSTEPS
                bounds = "lineage_timeout_s: 60" if reading else self.BOUNDS
                with self.subTest(provider=provider, pair=f"{phase}.{substep}"):
                    cfg = lc.load_llm_config(self.write(_doc(entries={
                        (phase, substep): f"{{provider: {provider}, model: m, {extra}, {bounds}}}"})))
                    self.assertTrue(cfg.entry_for(phase, substep).is_http)
        for level, body in (
            ("defaults", _doc("defaults:\n  lineage_timeout_s: 2700\n  provider: anthropic_api\n"
                              "  api_key_env: K\n  model: m\n  max_wrapper_calls: 3\n")),
            ("phase", _doc(phase_fields={"generate": "provider: anthropic_api\n"
                                                     "api_key_env: K\nmodel: m\n"
                                                     "lineage_timeout_s: 60"})),
        ):
            with self.subTest(level=level):
                self.assert_rule("llm_config_workshop_field_not_substep_scoped", body)

    def test_the_workshop_pairs_match_the_launch_validator(self) -> None:
        from tools import leaf_contract
        # Every LLM pair is a workshop pair (issue #534 PR-J4-b): the union of the two kinds
        # is `LLM_LEAF_SUBSTEPS`, with no pair left over for a second leaf model.
        self.assertEqual(lc.WORKSHOP_LEAF_SUBSTEPS, lc.LLM_LEAF_SUBSTEPS)
        self.assertFalse(lc.WORKSHOP_PRODUCING_SUBSTEPS & lc.WORKSHOP_READING_SUBSTEPS)
        # The validator's set holds the `escalate` diagnostician's pairs too (issue #534
        # PR-J4-a), a reading launch no operator assigns: `defaults` serves it.
        self.assertEqual(lc.WORKSHOP_LEAF_SUBSTEPS,
                         leaf_contract.WORKSHOP_LAUNCH_PAIRS - leaf_contract.WORKSHOP_DIAGNOSE_PAIRS)
        self.assertEqual(leaf_contract.WORKSHOP_READING_LAUNCH_PAIRS,
                         leaf_contract.WORKSHOP_READING_PAIRS | leaf_contract.WORKSHOP_DIAGNOSE_PAIRS)
        self.assertFalse(lc.WORKSHOP_LEAF_SUBSTEPS & leaf_contract.WORKSHOP_DIAGNOSE_PAIRS)
        self.assertEqual(lc.WORKSHOP_PRODUCING_SUBSTEPS, leaf_contract.WORKSHOP_PRODUCING_PAIRS)
        self.assertEqual(lc.WORKSHOP_READING_SUBSTEPS, leaf_contract.WORKSHOP_READING_PAIRS)
        self.assertEqual(lc.WORKSHOP_READING_PROVIDERS,
                         frozenset(leaf_contract.WORKSHOP_READER_TOOL_SURFACE_TEXT))
        # The retired leaf-model vocabulary is gone from the configuration module.
        for retired in ("LEAF_PURE", "LEAF_WORKSHOP", "LEAF_MODELS", "CAP_PURE",
                        "WORKSHOP_BOUND_FIELDS"):
            self.assertFalse(hasattr(lc, retired), msg=retired)


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
            path.write_text(_doc("defaults:\n  lineage_timeout_s: 2700\n  provider: codex_cli\n"),
                            encoding="utf-8")
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
            path.write_text(_doc(entries={
                ("generate", "generate"): "provider: openai_compatible\n"
                                          "base_url: http://localhost:8000/v1\n"
                                          "api_key_env: LOCAL_KEY\n"
                                          "model: local-model\n"
                                          "max_wrapper_calls: 30\nlineage_timeout_s: 2700"}),
                encoding="utf-8")
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
            path.write_text(_doc(
                "defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n  model: opus\n",
                entries={("validate", "judge"): "{model: opus, lineage_timeout_s: 2700}"}),
                encoding="utf-8")
            cfg = lc.apply_defaults_overrides(lc.load_llm_config(path), model="sonnet")
            self.assertEqual(cfg.entry_for("validate", "judge").model, "opus")
            self.assertEqual(cfg.defaults.model, "sonnet")
            # ...and a leaf that declared nothing DOES follow the default.
            self.assertEqual(cfg.entry_for("compile", "verify").model, "sonnet")

    def test_a_phase_level_pin_equal_to_the_default_survives_too(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "phase.yaml"
            path.write_text(_doc(
                "defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n  command: wrap\n",
                phase_fields={"generate": "command: wrap"}), encoding="utf-8")
            cfg = lc.apply_defaults_overrides(lc.load_llm_config(path), command="other")
            self.assertEqual(cfg.entry_for("generate", "generate").command, "wrap")
            self.assertEqual(cfg.entry_for("compile", "verify").command, "other")

    def test_declared_names_only_what_a_level_below_defaults_wrote(self) -> None:
        """Since issue #534 PR-J4-b every substep entry writes its own bounds, so those are
        in `declared` too; what `defaults` wrote (its `model`) is not."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "d.yaml"
            path.write_text(_doc(
                "defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n  model: opus\n",
                entries={("validate", "judge"): "{model: haiku, lineage_timeout_s: 2700}"}),
                encoding="utf-8")
            cfg = lc.load_llm_config(path)
            self.assertEqual(cfg.entry_for("validate", "judge").declared,
                             frozenset({"model", "lineage_timeout_s"}))
            self.assertEqual(cfg.entry_for("compile", "verify").declared,
                             frozenset({"lineage_timeout_s"}))

    def test_overrides_leave_a_per_substep_model_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pinned.yaml"
            path.write_text(_doc(entries={
                ("validate", "judge"): "{model: judge-model, lineage_timeout_s: 2700}"}),
                encoding="utf-8")
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
            declared.write_text(_doc("defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n"
                                     "  model: opus\n"), encoding="utf-8")
            bare = Path(tmp) / "bare.yaml"
            bare.write_text(_doc(), encoding="utf-8")
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
            path.write_text(_doc(
                "defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n  model: base\n",
                entries={("validate", "judge"): "{model: haiku, lineage_timeout_s: 2700}"}),
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

    def test_llm_leaf_substeps_all_have_a_pure_path_in_the_conductor(self) -> None:
        """Runs `Conductor._leaf_substep` itself (on a stub whose node-shape predicate
        answers a shape), so the pair test in that body is what is being compared — not a copy
        of it. Since Z4 (issue #171) every LLM leaf must answer True on a shaped node: there is
        no second transport for one that does not.

        The shape half is exercised by `..._on_a_shapeless_node` below: with a shape present
        this row cannot tell a pair that is admitted BECAUSE it is a compile pair from one
        admitted because the node's shape happened to allow it."""

        class _Stub:
            # Both spellings of "this launch's model can run the leaf" are supplied (a backend
            # token and a per-launch entry), so the guard does not depend on which one the
            # predicate reads — since Z4 it reads neither (the capability is a load-time rule).
            backend = "claude"
            _leaf_substep = wc.Conductor._leaf_substep

            def _bundle_shape(self, refs):  # noqa: D401 - stub
                return "m3c"

            def entry_for(self, phase, substep):
                return lc.ResolvedLeafEntry(
                    provider="claude_cli",
                    capabilities=lc.PROVIDER_CAPABILITIES["claude_cli"])

        stub = _Stub()
        derived = {
            (phase, substep) for (phase, substep) in lc.LLM_LEAF_SUBSTEPS
            if stub._leaf_substep(None, phase, substep)
        }
        self.assertEqual(derived, set(lc.LLM_LEAF_SUBSTEPS))

    def test_the_conductor_pure_path_on_a_shapeless_node(self) -> None:
        """The same predicate with the node-shape half absent (`_bundle_shape` -> None). Only
        the two COMPILE pairs and the judge stay admissible there — the Compile contract does
        not depend on the node kind, and no IR exists at `compile.generate` time to read a shape
        from — so this row is what separates the pair test from the shape test in
        `_leaf_substep`'s body. Without it, replacing the shape-free arm with the generate
        arm's shape condition stays green. No in-tree node answers None since issue #169; this
        is the hand-crafted-IR fail-safe."""

        class _Stub:
            backend = "claude"
            _leaf_substep = wc.Conductor._leaf_substep

            def _bundle_shape(self, refs):  # noqa: D401 - stub
                return None

            def entry_for(self, phase, substep):
                return lc.ResolvedLeafEntry(
                    provider="claude_cli",
                    capabilities=lc.PROVIDER_CAPABILITIES["claude_cli"])

        stub = _Stub()
        derived = {
            (phase, substep) for (phase, substep) in lc.LLM_LEAF_SUBSTEPS
            if stub._leaf_substep(None, phase, substep)
        }
        self.assertEqual(derived, {("compile", "generate"), ("compile", "verify"),
                                   ("validate", "judge")})

    def test_the_harness_shape_admits_the_generate_pairs_too(self) -> None:
        """Issue #169: the second shape. A node whose `_bundle_shape` is `harness` reaches the
        same five pairs — the predicate asks whether there IS a shape, not which one."""

        class _Stub:
            backend = "claude"
            _leaf_substep = wc.Conductor._leaf_substep

            def _bundle_shape(self, refs):  # noqa: D401 - stub
                return "harness"

            def entry_for(self, phase, substep):
                return lc.ResolvedLeafEntry(
                    provider="claude_cli",
                    capabilities=lc.PROVIDER_CAPABILITIES["claude_cli"])

        stub = _Stub()
        derived = {
            (phase, substep) for (phase, substep) in lc.LLM_LEAF_SUBSTEPS
            if stub._leaf_substep(None, phase, substep)
        }
        self.assertEqual(derived, set(lc.LLM_LEAF_SUBSTEPS))

    def test_the_llm_leaf_set_is_the_size_this_tree_decided(self) -> None:
        """One place where the SIZE of the set is a decision rather than a consequence.

        Every other guard here runs the table (the wiring matrix, the template slots, the audit
        map keys), so growing or shrinking it moves them all together and nothing says a member
        was added or removed. This row does — and it is where a reviewer is told which members
        those are."""
        self.assertEqual(len(lc.LLM_LEAF_SUBSTEPS), 5)
        self.assertEqual(
            sorted(lc.LLM_LEAF_SUBSTEPS),
            [("compile", "generate"), ("compile", "verify"),
             ("generate", "generate"), ("generate", "verify"),
             ("validate", "judge")])

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
            original = _doc("defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n"
                            "  model: original\n")
            path.write_text(original, encoding="utf-8")
            real_read_bytes = Path.read_bytes

            def _swap_after_read(self_path):
                data = real_read_bytes(self_path)
                if self_path == path:            # the file changes underneath us
                    path.write_text(original.replace("model: original", "model: swapped"),
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
            path.write_text("defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n", encoding="utf-8")
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
            path.write_text("defaults:\n  lineage_timeout_s: 2700\n  provider: claude_cli\n", encoding="utf-8")
            os.chmod(path, 0o000)
            try:
                self.assertFalse(os.access(path, os.R_OK), "running as root: file modes do not apply; "
                                 "run the suite as an unprivileged user")
                self.assertEqual(lc.config_sha256(path), "sha256:unreadable")
            finally:
                os.chmod(path, 0o644)


if __name__ == "__main__":
    unittest.main()
