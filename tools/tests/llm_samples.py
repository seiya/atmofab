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


@functools.lru_cache(maxsize=None)
def agentic_only_config(backend: str = "claude") -> lc.LlmConfig:
    """The sample configuration with `pure` REMOVED from every leaf, so every LLM leaf runs the
    agentic loop.

    A test whose subject is the shared agentic leaf loop needs a leaf that runs it. Four of the
    five LLM leaves now dispatch to a pure loop instead whenever their provider holds `pure`
    (`Conductor._pure_leaf_substep`), so a test that reaches the agentic loop by naming
    `compile.verify` reached it by accident of what had been migrated — and stopped reaching it
    when Z1 migrated the two compile leaves (issue #168).

    The restriction is the config file's own `capabilities:` key, which may only narrow a
    provider's declared set (`llm_config.PROVIDER_CAPABILITIES` is the single source), so this
    is the SAME mechanism an operator uses to keep a leaf on the agentic path — and the one the
    A/B baseline arm of issue #168 uses. It is not a test-only back door.
    """
    def narrowed(entry: lc.ResolvedLeafEntry) -> lc.ResolvedLeafEntry:
        # Only `pure` is dropped. Narrowing all the way to `{agentic}` would also drop
        # `warm_resume`, which is a DIFFERENT capability and is what the agentic loop's own
        # slim-repair turn is gated on — so a fixture written to reach the agentic loop would
        # silently stop exercising its warm resume.
        return dataclasses.replace(
            entry, capabilities=frozenset(entry.capabilities) - {lc.CAP_PURE})

    base = sample_config(backend or "claude")
    return dataclasses.replace(
        base,
        defaults=narrowed(base.defaults),
        entries={key: narrowed(entry) for key, entry in base.entries.items()},
    )
