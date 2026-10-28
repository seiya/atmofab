"""Shared access to the committed leaf-LLM samples, for tests that build a `Conductor`.

A conductor takes its leaf-model authority as a required `llm_config` — there is no run-wide
backend identity it can reconstruct one from — so almost every test that builds one needs a
loaded configuration. These helpers hand out the samples an operator actually copies to
`./llm.yaml`, rather than a hand-written fixture: a fixture would be free to describe a shape
the real documents do not have, which is the failure mode this repository has paid for before.
"""

from __future__ import annotations

import dataclasses
import functools
from pathlib import Path

from tools import llm_config as lc

SAMPLE_DIR = Path(__file__).resolve().parents[2] / "docs" / "examples"


@functools.lru_cache(maxsize=None)
def sample_config(backend: str = "claude") -> lc.LlmConfig:
    """The committed sample configuration for a CLI backend.

    Cached because ~100 constructions across the suite want one and each load re-reads and
    re-validates a 130-line document. Safe to share: `LlmConfig` is frozen, and the one place a
    conductor rewrites it (`_resolve_claude_model_aliases`) builds a replacement rather than
    mutating this instance."""
    return lc.load_llm_config(SAMPLE_DIR / f"llm_{backend}.example.yaml")


def sample_config_with(backend: str = "claude", agent_model: str = "",
                       llm_command: str = "") -> lc.LlmConfig:
    """A sample configuration with run-wide `model` / `command` overrides applied.

    The overrides are what production's preflight subprocess re-applies
    (`--llm-config-defaults-model/-command`); here they also spell what these tests used to say
    as constructor arguments, before the conductor stopped taking a run-wide backend identity
    at all."""
    return lc.apply_defaults_overrides(
        sample_config(backend or "claude"), model=agent_model, command=llm_command)


#: The bounds `workshop_yaml` gives an LLM substep entry that states none: the samples' values.
SAMPLE_LINEAGE_TIMEOUT_S = 2700
SAMPLE_MAX_WRAPPER_CALLS = 30


def workshop_yaml(text: str) -> str:
    """`text` — a leaf-LLM configuration a test writes by hand — completed into one the loader
    accepts since issue #534 PR-J4-b: every LLM substep is written under
    `phases.<phase>.substeps.<substep>` and states its own bounds (`lineage_timeout_s`; a
    producing one `max_wrapper_calls` as well), and `defaults` states `lineage_timeout_s`.

    What it adds, and nothing else: a missing substep entry (as `{}`, so it inherits what the
    document's `defaults` / phase level say, as the loader resolves it), a missing bound on an
    entry (the samples' values), and the `defaults` bound. A `leaf:` key is DROPPED rather than
    refused, because what these fixtures said with `leaf: workshop` is now what every entry is.
    It never changes a field the text states — a test that writes a bound, a provider or a
    capability gets exactly that — so a test of a CONFIGURATION rule must not use it: it would
    hide the rule under test. Those tests write their document whole."""
    import yaml

    doc = yaml.safe_load(text) or {}
    defaults = doc.setdefault("defaults", {})
    defaults.pop("leaf", None)
    defaults.setdefault("lineage_timeout_s", SAMPLE_LINEAGE_TIMEOUT_S)
    phases = doc.setdefault("phases", {})
    for phase, substep in sorted(lc.LLM_LEAF_SUBSTEPS):
        phase_doc = phases.get(phase)
        if phase_doc is None:
            phase_doc = phases[phase] = {}
        phase_doc.pop("leaf", None)
        substeps = phase_doc.setdefault("substeps", {})
        entry = substeps.get(substep)
        if entry is None:
            entry = substeps[substep] = {}
        entry.pop("leaf", None)
        entry.setdefault("lineage_timeout_s", SAMPLE_LINEAGE_TIMEOUT_S)
        if (phase, substep) in lc.WORKSHOP_PRODUCING_SUBSTEPS:
            entry.setdefault("max_wrapper_calls", SAMPLE_MAX_WRAPPER_CALLS)
    return yaml.safe_dump(doc, sort_keys=False)
