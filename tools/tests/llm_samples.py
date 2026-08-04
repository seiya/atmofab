"""Shared access to the committed leaf-LLM samples, for tests that build a `Conductor`.

A conductor takes its leaf-model authority as a required `llm_config` — there is no run-wide
backend identity it can reconstruct one from — so almost every test that builds one needs a
loaded configuration. These helpers hand out the samples an operator actually copies to
`./llm.yaml`, rather than a hand-written fixture: a fixture would be free to describe a shape
the real documents do not have, which is the failure mode this repository has paid for before.
"""

from __future__ import annotations

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
