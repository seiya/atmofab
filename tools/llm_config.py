#!/usr/bin/env python3
"""Per-phase / per-substep leaf-LLM configuration (issue #28).

Until now the leaf LLM was ONE run-wide choice (`--llm codex|claude`, plus a run-wide
`--agent-model` / `--llm-command`), and the backend was not a parameter of a leaf launch but
an *identity* the conductor branched on in some twenty-five places. That made two things
impossible: running a cheap substep on a local model while an expensive one stays hosted, and
describing the choice anywhere other than the command line.

This module is the configuration authority for the replacement. It resolves a YAML document
into one `ResolvedLeafEntry` per LLM leaf — the whole of what a launch needs to know about its
model (provider, model id, wrapper command / endpoint, limits) plus the CAPABILITIES that
provider has. Everything downstream keys on the entry, so the conductor asks
`entry.supports("warm_resume")` rather than `backend == "claude"`, and a new provider is a
table row instead of a new branch.

**Capabilities are declared, never inferred.** `PROVIDER_CAPABILITIES` below is the single
source of truth; a config's own `capabilities:` list may only RESTRICT a provider's set (to
model an endpoint that, say, cannot be warm-resumed), never extend it. The user-approved scope
rule — HTTP providers are admissible only on the two Z2 *pure* leaves — is therefore not a
`provider == "openai_compatible"` branch anywhere: it falls out of the HTTP providers holding
`pure` and not `agentic`, checked against what each substep requires
(`llm_config_capability_insufficient_for_substep`).

Stdlib + PyYAML only, and deliberately importing nothing from `tools.orchestration_runtime` /
`tools.workflow_conductor`: both of those import (or will import) this module, and the
mirror-table drift guards in `tools/tests/test_llm_config.py` import all three to compare them.
The tables duplicated here (`LLM_LEAF_SUBSTEPS`, `PURE_CAPABLE_SUBSTEPS`,
`MCP_REQUIRED_LLM_SUBSTEPS`) are guarded copies, not independent opinions: each has a test that
fails if the conductor/runtime original moves.

Rejections are NAMED. `LlmConfigError.rule` carries a stable identifier (`llm_config_*`) that
callers surface verbatim, because a config the operator wrote by hand is exactly the place
where "invalid configuration" without the rule name costs a debugging round-trip.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

import yaml


class _NoDuplicateKeyLoader(yaml.SafeLoader):
    """`yaml.SafeLoader` that refuses a repeated mapping key instead of keeping the last.

    This file decides which model runs each substep, and therefore what a run costs. A
    duplicated `phases.generate` or `substeps.verify` is an editing accident whose silent
    resolution is "run something the operator did not intend and cannot see" — the failure mode
    a named rejection exists for."""


def _no_duplicate_keys(loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False):
    seen: set = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise LlmConfigError(
                "llm_config_duplicate_key",
                f"key {key!r} is defined more than once in the same mapping; YAML keeps only "
                f"the last, which would silently run a provider/model you cannot see in the "
                f"document", where=str(key))
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_NoDuplicateKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    lambda loader, node: _no_duplicate_keys(loader, node))


# An `http://` endpoint sends the API key in cleartext. Loopback is exempt: that is where the
# local servers this provider exists for run, and the traffic never leaves the host. Anything
# else needs an explicit opt-in, because the realistic case is a typo or a LAN address, not a
# deliberate choice.
_INSECURE_BASE_URL_OPT_IN_ENV = "METDSL_ALLOW_INSECURE_LLM_BASE_URL"


def _is_loopback(host: str) -> bool:
    host = (host or "").strip().strip("[]").lower()
    if host in ("localhost", "localhost.localdomain") or host.endswith(".localhost"):
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False

# --- capabilities --------------------------------------------------------------------

# The capability vocabulary. These are the questions the conductor actually asks of a leaf's
# provider; each one replaces a `backend == ...` branch:
#   agentic      the provider can run the shared AGENTIC leaf loop (a tool-holding session
#                driven by a launch prompt, with hooks, MCP grants and a workspace).
#   pure         the provider can run a Z2 host-mediated PURE leaf (one typed document in,
#                one JSON document out; `write_roots: []`, no tools, no hooks, no MCP).
#   warm_resume  a finished leaf session can be reopened for a repair turn carrying the prior
#                context (claude `--resume --fork-session`, codex `exec resume`).
#   mcp_tools    the leaf can be granted build-runtime MCP tools.
#   usage_probe  the provider answers a host-side `/usage` probe, which is how a run waits out
#                a usage-limit reset instead of failing (see docs/RUNBOOK.md).
CAP_AGENTIC = "agentic"
CAP_PURE = "pure"
CAP_WARM_RESUME = "warm_resume"
CAP_MCP_TOOLS = "mcp_tools"
CAP_USAGE_PROBE = "usage_probe"

KNOWN_CAPABILITIES: frozenset[str] = frozenset({
    CAP_AGENTIC, CAP_PURE, CAP_WARM_RESUME, CAP_MCP_TOOLS, CAP_USAGE_PROBE,
})

# THE capability authority. A config may restrict a provider's set; it may never exceed it.
PROVIDER_CAPABILITIES: Mapping[str, frozenset[str]] = {
    "claude_cli": frozenset({CAP_AGENTIC, CAP_PURE, CAP_WARM_RESUME, CAP_MCP_TOOLS,
                             CAP_USAGE_PROBE}),
    "codex_cli": frozenset({CAP_AGENTIC, CAP_PURE, CAP_WARM_RESUME, CAP_MCP_TOOLS}),
    # HTTP providers: one request, one response. No session to reopen, no tools to grant, no
    # `/usage` endpoint in the shape the probe speaks — and, decisively, no agentic loop, which
    # is what confines them to the pure leaves.
    "openai_compatible": frozenset({CAP_PURE}),
    "anthropic_api": frozenset({CAP_PURE}),
}

SUPPORTED_PROVIDERS: frozenset[str] = frozenset(PROVIDER_CAPABILITIES)

# Providers that launch a child process (a CLI), vs. providers the conductor speaks to over
# HTTP from its own process. `probe_all_providers` picks a prober by this split, and
# `test_llm_config` pins that the two partition `SUPPORTED_PROVIDERS`.
CLI_PROVIDERS: frozenset[str] = frozenset({"claude_cli", "codex_cli"})
HTTP_PROVIDERS: frozenset[str] = frozenset({"openai_compatible", "anthropic_api"})

# The `backend` token a provider is recorded and probed under. The two CLI providers keep the
# legacy spellings ("claude" / "codex") so preflight payloads, agent_runs rows and every
# existing consumer of `--backend` stay byte-compatible; the HTTP providers are their own
# tokens (added to the validators' accepted set in Phase 4, NOT to `SUPPORTED_BACKENDS`).
PROVIDER_BACKEND_TOKENS: Mapping[str, str] = {
    "claude_cli": "claude",
    "codex_cli": "codex",
    "openai_compatible": "openai_compatible",
    "anthropic_api": "anthropic_api",
}

# The reverse map, for `llm_config_from_legacy` and for reading a recorded token back.
BACKEND_TOKEN_PROVIDERS: Mapping[str, str] = {v: k for k, v in PROVIDER_BACKEND_TOKENS.items()}


# --- mirror tables (guarded copies) --------------------------------------------------

# The five substeps that run as an LLM leaf. Mirror of
# `workflow_conductor.SUBSTEPS` minus `Conductor._is_deterministic_substep`; guarded by
# `test_llm_leaf_substeps_matches_conductor`.
LLM_LEAF_SUBSTEPS: frozenset[tuple[str, str]] = frozenset({
    ("compile", "generate"),
    ("compile", "verify"),
    ("generate", "generate"),
    ("generate", "verify"),
    ("validate", "judge"),
})

# The subset that CAN run as a Z2 pure leaf. Mirror of the `(phase, substep)` pair test in
# `Conductor._pure_leaf_substep`; guarded by `test_pure_capable_substeps_matches_conductor`.
# NOTE the dispatch there is additionally gated on the node's M3c shape, so a pure-only
# provider on a non-M3c node has no pure path — the conductor fails that closed at run time
# (`pure_only_provider_on_agentic_path`), which config validation cannot see.
PURE_CAPABLE_SUBSTEPS: frozenset[tuple[str, str]] = frozenset({
    ("generate", "generate"),
    ("generate", "verify"),
})

# LLM leaves that hold a build-runtime MCP grant. EMPTY today — every non-empty key of
# `orchestration_runtime._MCP_TOOL_GRANTS_BY_SUBSTEP` is a deterministic in-process body, not
# an LLM leaf. Kept as a table (rather than assumed empty) so that granting an LLM leaf a tool
# automatically starts requiring `mcp_tools` of whatever provider is configured for it;
# guarded by `test_mcp_required_llm_substeps_matches_runtime`.
MCP_REQUIRED_LLM_SUBSTEPS: frozenset[tuple[str, str]] = frozenset()

# Phases that own at least one LLM leaf. `build` is deliberately absent: it is contractually
# deterministic, so naming it in a config is an operator error, not a no-op.
LLM_LEAF_PHASES: frozenset[str] = frozenset(p for p, _ in LLM_LEAF_SUBSTEPS)


def required_capabilities(phase: str, substep: str) -> frozenset[str]:
    """The capabilities an entry MUST have to run `(phase, substep)`.

    A pure-capable substep accepts either an agentic or a pure provider (the conductor picks
    per node shape), so its requirement is the *alternative* {agentic, pure} — expressed here
    as the empty hard requirement plus the alternative handled by the caller. Everything else
    hard-requires `agentic`."""
    caps: set[str] = set()
    if (phase, substep) in MCP_REQUIRED_LLM_SUBSTEPS:
        caps.add(CAP_MCP_TOOLS)
    return frozenset(caps)


# --- errors --------------------------------------------------------------------------

class LlmConfigError(ValueError):
    """A named configuration rejection.

    `rule` is a stable `llm_config_*` identifier; callers print it alongside the message so an
    operator can search for it. `where` is a dotted path into the document
    (`phases.generate.substeps.verify.base_url`) or `"<file>"` for whole-document failures."""

    def __init__(self, rule: str, message: str, *, where: str = "") -> None:
        self.rule = rule
        self.where = where
        super().__init__(f"{rule}: {message}" + (f" (at {where})" if where else ""))


# --- resolved entry ------------------------------------------------------------------

# Entry-level keys accepted anywhere an entry may appear.
_ENTRY_FIELDS: frozenset[str] = frozenset({
    "provider", "model", "command", "base_url", "api_key_env",
    "timeout_s", "max_output_tokens", "capabilities",
})

# Fields that only make sense for some providers. Anything not listed applies to all.
# Fields a provider does not read. Declaring one is an operator error, not a no-op: an ignored
# `max_output_tokens` on a codex entry looks like a budget that was applied.
# Rejected when a level whose own provider is this one declares the field; DROPPED when the
# field merely inherited from a level on another provider. `timeout_s` / `max_output_tokens`
# are transport-neutral budgets that inherit across a provider switch by design, so
# "claude everywhere with a raised ceiling, except one leaf on codex" must load — with the
# ceiling simply not reaching the codex leaf, which has nowhere to apply it.
_FIELDS_NOT_APPLICABLE: Mapping[str, frozenset[str]] = {
    # `timeout_s` bounds an HTTP request; a CLI leaf's wall-clock cap is the conductor's own
    # (`METDSL_LEAF_TIMEOUT_SECONDS`). `max_output_tokens` reaches only the claude transport
    # (`CLAUDE_CODE_MAX_OUTPUT_TOKENS`) and the HTTP request bodies.
    "claude_cli": frozenset({"base_url", "api_key_env", "timeout_s"}),
    "codex_cli": frozenset({"base_url", "api_key_env", "timeout_s", "max_output_tokens"}),
    "openai_compatible": frozenset({"command"}),
    "anthropic_api": frozenset({"command"}),
}

# Fields scoped to the provider that declared them. When a deeper level switches provider,
# these are DROPPED rather than inherited: a `model` or `command` chosen for `claude_cli` is
# meaningless — and usually actively wrong — under `openai_compatible`. `timeout_s` /
# `max_output_tokens` are transport-neutral budgets and do inherit across a switch.
_PROVIDER_SCOPED_FIELDS: frozenset[str] = frozenset({
    "provider", "model", "command", "base_url", "api_key_env", "capabilities",
})

# The Anthropic Messages API has one canonical endpoint, so `base_url` is optional there and
# defaults to it; an OpenAI-compatible server is by definition operator-hosted and has none.
ANTHROPIC_DEFAULT_BASE_URL = "https://api.anthropic.com"


@dataclass(frozen=True)
class ResolvedLeafEntry:
    """Everything one leaf launch needs to know about its model.

    Frozen: an entry is handed to launch code, recorded in provenance, and compared on resume;
    a mutable one would let a late branch rewrite what was already recorded.

    `model` empty is MEANINGFUL, not missing: for `claude_cli` it selects runtime alias
    resolution (the operator deliberately does not pin a version — see
    `orchestration_runtime.default_agent_model_for_backend`). For `codex_cli` it is an operator
    omission, caught at run start by `validate_runnable`, not at load, so that shipping a
    model-less `configs/llm/codex.yaml` and testing that it loads remain compatible."""

    provider: str
    model: str = ""
    command: str = ""
    base_url: str = ""
    api_key_env: str = ""
    timeout_s: float | None = None
    max_output_tokens: int | None = None
    capabilities: frozenset[str] = frozenset()
    # The field names a level on THIS entry's provider actually wrote, as opposed to inherited.
    # `apply_defaults_overrides` needs it: value equality cannot tell an inherited `opus` from
    # a per-substep one deliberately pinned to the same string, and a run-wide `--agent-model`
    # must move the first and leave the second.
    declared: frozenset[str] = frozenset()

    def supports(self, capability: str) -> bool:
        """Fail-closed capability test: an unknown capability name is NOT supported.

        A typo in a call site must read as "this provider cannot do that" and take the
        conservative branch, never as an accidental grant."""
        return capability in self.capabilities

    @property
    def backend_token(self) -> str:
        return PROVIDER_BACKEND_TOKENS.get(self.provider, self.provider)

    @property
    def is_http(self) -> bool:
        return self.provider in HTTP_PROVIDERS

    def provenance(self) -> dict[str, str]:
        """The per-leaf provenance row recorded in the invocation record."""
        return {
            "provider": self.provider,
            "backend": self.backend_token,
            "model": self.model,
        }


# --- parsing helpers -----------------------------------------------------------------

def _require_mapping(value: Any, where: str, rule: str = "llm_config_not_a_mapping") -> dict:
    if not isinstance(value, dict):
        raise LlmConfigError(rule, f"expected a mapping, got {type(value).__name__}", where=where)
    return value


def _clean_str(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise LlmConfigError(
            "llm_config_invalid_field",
            f"expected a string, got {type(value).__name__}", where=where)
    return value.strip()


def _layer_fields(raw: Mapping[str, Any], where: str) -> dict[str, Any]:
    """Validate one document level's entry fields and return them normalized.

    `substeps` / `phases` are structural and stripped by the caller before this runs."""
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in _ENTRY_FIELDS:
            raise LlmConfigError(
                "llm_config_unknown_key",
                f"unknown key {key!r}; accepted: {', '.join(sorted(_ENTRY_FIELDS))}",
                where=f"{where}.{key}")
        loc = f"{where}.{key}"
        if key in ("provider", "model", "command", "base_url", "api_key_env"):
            out[key] = _clean_str(value, loc)
        elif key == "timeout_s":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise LlmConfigError(
                    "llm_config_invalid_field",
                    f"expected a positive number, got {value!r}", where=loc)
            out[key] = float(value)
        elif key == "max_output_tokens":
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise LlmConfigError(
                    "llm_config_invalid_field",
                    f"expected a positive integer, got {value!r}", where=loc)
            out[key] = int(value)
        elif key == "capabilities":
            if not isinstance(value, list) or not all(isinstance(c, str) for c in value):
                raise LlmConfigError(
                    "llm_config_invalid_field",
                    f"expected a list of capability names, got {value!r}", where=loc)
            caps = frozenset(c.strip() for c in value)
            unknown = sorted(caps - KNOWN_CAPABILITIES)
            if unknown:
                raise LlmConfigError(
                    "llm_config_invalid_field",
                    f"unknown capability name(s): {', '.join(unknown)}; accepted: "
                    f"{', '.join(sorted(KNOWN_CAPABILITIES))}", where=loc)
            out[key] = caps
    return out


def _merge_layers(
    layers: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], frozenset[str], frozenset[str]]:
    """Resolve `defaults` -> `phases.<phase>` -> `...substeps.<substep>` into one field map.

    Two kinds of field, resolved differently:

    * **Transport-neutral** budgets (`timeout_s`, `max_output_tokens`) inherit plainly: the
      deepest level that names one wins.
    * **Provider-scoped** fields (`model`, `command`, `base_url`, `api_key_env`,
      `capabilities`) are contributed only by levels whose EFFECTIVE provider is the one this
      entry ends up on. A `model` chosen for `claude_cli` names nothing under
      `openai_compatible`, so a level that switches provider contributes none of the outer
      level's — and, symmetrically, a level that switches BACK re-inherits from the levels that
      share its provider. Resolving against effective providers rather than folding pairwise is
      what makes that second half true: pairwise folding drops a field permanently at the first
      switch, so `defaults(claude) -> generate(http) -> generate.verify(claude)` silently lost
      the operator's `defaults.command` wrapper on that one leaf.

    Returns `(fields, declared_on_this_provider, declared_below_defaults)`. The second tells
    `_finalize_entry` a field the operator wrote for THIS provider from one that merely
    inherited, which is what makes a non-applicable field an error only where it can be acted
    on. The third is narrower — the field was written at a level BELOW `defaults`, i.e. per
    phase or per substep — and is what `apply_defaults_overrides` needs: a run-wide flag
    overrides `defaults` and everything that took its value from `defaults`, but not a
    per-substep value the operator pinned deliberately."""
    effective: list[tuple[str, Mapping[str, Any]]] = []
    provider = ""
    for layer in layers:
        provider = str(layer.get("provider") or "") or provider
        effective.append((provider, layer))
    final_provider = provider

    merged: dict[str, Any] = {}
    declared: set[str] = set()
    declared_local: set[str] = set()
    for index, (layer_provider, layer) in enumerate(effective):
        for key, value in layer.items():
            if key in _PROVIDER_SCOPED_FIELDS and layer_provider != final_provider:
                continue
            merged[key] = value
            if layer_provider == final_provider:
                declared.add(key)
                if index > 0:                    # layer 0 IS `defaults`
                    declared_local.add(key)
    if final_provider:
        merged["provider"] = final_provider
    return merged, frozenset(declared), frozenset(declared_local)


def _finalize_entry(fields: Mapping[str, Any], where: str,
                    declared_here: frozenset[str] = frozenset(),
                    declared_local: frozenset[str] = frozenset()) -> ResolvedLeafEntry:
    """Turn a merged field map into a validated `ResolvedLeafEntry`.

    `declared_here` names the fields written by a level that shares this entry's provider —
    the ones an operator can act on. A field that only INHERITED from a level on another
    provider and does not apply here is dropped silently, because there is no key in the
    document to point at and nothing was misconfigured."""
    provider = str(fields.get("provider") or "")
    if not provider:
        raise LlmConfigError(
            "llm_config_missing_provider",
            "no provider resolved for this entry; set `provider:` here or in `defaults`",
            where=where)
    if provider not in SUPPORTED_PROVIDERS:
        raise LlmConfigError(
            "llm_config_unknown_provider",
            f"unknown provider {provider!r}; accepted: {', '.join(sorted(SUPPORTED_PROVIDERS))}",
            where=f"{where}.provider")

    for field in sorted(_FIELDS_NOT_APPLICABLE.get(provider, frozenset())):
        if not fields.get(field):
            continue
        if field in declared_here:
            raise LlmConfigError(
                "llm_config_field_not_applicable",
                f"field {field!r} does not apply to provider {provider!r}",
                where=f"{where}.{field}")
        fields = {k: v for k, v in fields.items() if k != field}

    allowed = PROVIDER_CAPABILITIES[provider]
    declared = fields.get("capabilities")
    if declared is None:
        capabilities = allowed
    else:
        excess = sorted(frozenset(declared) - allowed)
        if excess:
            raise LlmConfigError(
                "llm_config_capability_exceeds_provider",
                f"provider {provider!r} does not have capability/-ies {', '.join(excess)}; "
                f"`capabilities:` may only restrict {', '.join(sorted(allowed))}",
                where=f"{where}.capabilities")
        capabilities = frozenset(declared)

    base_url = str(fields.get("base_url") or "")
    if provider == "anthropic_api" and not base_url:
        base_url = ANTHROPIC_DEFAULT_BASE_URL

    if provider in HTTP_PROVIDERS:
        if not fields.get("base_url") and provider != "anthropic_api":
            raise LlmConfigError(
                "llm_config_http_requires_base_url",
                f"provider {provider!r} requires `base_url:`", where=where)
        if not fields.get("api_key_env"):
            raise LlmConfigError(
                "llm_config_http_requires_api_key_env",
                f"provider {provider!r} requires `api_key_env:` (the NAME of the environment "
                f"variable holding the key; the key itself is never written to a config)",
                where=where)
        parsed = urlparse(base_url)
        if parsed.scheme == "http" and not _is_loopback(parsed.hostname or ""):
            if os.environ.get(_INSECURE_BASE_URL_OPT_IN_ENV, "").strip().lower() not in (
                    "1", "true", "yes"):
                raise LlmConfigError(
                    "llm_config_insecure_base_url",
                    f"`base_url` {base_url!r} is plain http to a non-loopback host, and the "
                    f"API key named by `api_key_env` would be sent over it in cleartext. Use "
                    f"https, point at loopback (where the local servers this provider exists "
                    f"for run), or set {_INSECURE_BASE_URL_OPT_IN_ENV}=1 to accept the risk "
                    f"deliberately", where=f"{where}.base_url")
        if not fields.get("model"):
            raise LlmConfigError(
                "llm_config_http_requires_model",
                f"provider {provider!r} requires an explicit `model:` (an HTTP endpoint has no "
                f"alias to resolve at runtime)", where=where)

    return ResolvedLeafEntry(
        declared=frozenset(declared_local),
        provider=provider,
        model=str(fields.get("model") or ""),
        command=str(fields.get("command") or ""),
        base_url=base_url,
        api_key_env=str(fields.get("api_key_env") or ""),
        timeout_s=fields.get("timeout_s"),
        max_output_tokens=fields.get("max_output_tokens"),
        capabilities=capabilities,
    )


# --- the config ----------------------------------------------------------------------

@dataclass(frozen=True)
class LlmConfig:
    """A loaded, fully validated leaf-LLM configuration.

    `entries` holds an entry for EVERY LLM leaf (all five pairs), always — resolution is total,
    so no caller has to re-implement the fallback to `defaults`. `defaults` additionally serves
    launches that carry no phase/substep at all (the `escalate` diagnostician), which is why it
    must be agentic."""

    path: str
    sha256: str
    defaults: ResolvedLeafEntry
    entries: Mapping[tuple[str, str], ResolvedLeafEntry]

    def entry_for(self, phase: str | None, substep: str | None) -> ResolvedLeafEntry:
        """The entry for one launch. `(None, None)` — a launch with no phase/substep, i.e.
        `escalate` — resolves to `defaults`, as does any pair that is not an LLM leaf (a
        deterministic substep never launches, but callers may ask defensively)."""
        if phase is None or substep is None:
            return self.defaults
        return self.entries.get((phase, substep), self.defaults)

    def backend_token(self, entry: ResolvedLeafEntry) -> str:
        return entry.backend_token

    @property
    def providers(self) -> frozenset[str]:
        """Every distinct provider this config can launch (defaults included)."""
        return frozenset({self.defaults.provider} | {e.provider for e in self.entries.values()})

    @property
    def is_uniform(self) -> bool:
        return len(self.providers) == 1

    def provenance_map(self) -> dict[str, dict[str, str]]:
        """Per-leaf provider/model, keyed `"<phase>.<substep>"` (plus `"defaults"`).

        Recorded in the invocation record so a mixed closure is legible to cost / A-B audits
        without re-reading the config file (which may since have changed)."""
        out = {"defaults": self.defaults.provenance()}
        for (phase, substep), entry in sorted(self.entries.items()):
            out[f"{phase}.{substep}"] = entry.provenance()
        return out

    def validate_runnable(self) -> None:
        """Run-start checks that are deliberately NOT applied at load.

        `configs/llm/codex.yaml` ships without a model on purpose (the operator must choose
        one), so it must LOAD — and be testable — while still failing before a run that would
        launch `codex exec --model ''`."""
        for label, entry in self._labelled_entries():
            if entry.provider == "codex_cli" and not entry.model:
                raise LlmConfigError(
                    "llm_config_codex_cli_requires_model",
                    "provider 'codex_cli' has no runtime model alias to resolve: set `model:` "
                    "(or pass the deprecated --agent-model)", where=label)

    def _labelled_entries(self) -> list[tuple[str, ResolvedLeafEntry]]:
        out = [("defaults", self.defaults)]
        out += [(f"phases.{p}.substeps.{s}", e) for (p, s), e in sorted(self.entries.items())]
        return out


def _validate_assignment(phase: str, substep: str, entry: ResolvedLeafEntry, where: str) -> None:
    """Reject an entry that cannot run the substep it was assigned to.

    This is where "HTTP providers are pure-only" is enforced — as a capability comparison, not
    a provider name test. A pure-capable substep is satisfied by EITHER `agentic` (the shared
    leaf loop, used on non-M3c nodes) or `pure`; every other LLM leaf hard-requires
    `agentic`."""
    for cap in sorted(required_capabilities(phase, substep)):
        if not entry.supports(cap):
            raise LlmConfigError(
                "llm_config_capability_insufficient_for_substep",
                f"substep {phase}.{substep} requires capability {cap!r}, which provider "
                f"{entry.provider!r} does not have", where=where)
    if (phase, substep) in PURE_CAPABLE_SUBSTEPS:
        if entry.supports(CAP_AGENTIC) or entry.supports(CAP_PURE):
            return
        raise LlmConfigError(
            "llm_config_capability_insufficient_for_substep",
            f"substep {phase}.{substep} requires capability 'agentic' or 'pure', and provider "
            f"{entry.provider!r} has neither", where=where)
    if not entry.supports(CAP_AGENTIC):
        raise LlmConfigError(
            "llm_config_capability_insufficient_for_substep",
            f"substep {phase}.{substep} runs the agentic leaf loop, so it requires capability "
            f"'agentic'; provider {entry.provider!r} has {', '.join(sorted(entry.capabilities))}"
            f" (HTTP providers are admissible only on the pure leaves "
            f"{', '.join(f'{p}.{s}' for p, s in sorted(PURE_CAPABLE_SUBSTEPS))})",
            where=where)


def load_llm_config(path: str | Path) -> LlmConfig:
    """Load and fully validate a leaf-LLM configuration file.

    Every rejection raises `LlmConfigError` with a named `rule`. Run-start-only checks live in
    `LlmConfig.validate_runnable`."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise LlmConfigError(
            "llm_config_unreadable", f"cannot read {p}: {exc}", where=str(p)) from exc
    try:
        doc = yaml.load(text, Loader=_NoDuplicateKeyLoader)
    except yaml.YAMLError as exc:
        raise LlmConfigError(
            "llm_config_unreadable", f"cannot parse {p} as YAML: {exc}", where=str(p)) from exc
    if doc is None:
        doc = {}
    _require_mapping(doc, str(p))

    unknown = sorted(set(doc) - {"defaults", "phases"})
    if unknown:
        raise LlmConfigError(
            "llm_config_unknown_key",
            f"unknown top-level key(s): {', '.join(map(repr, unknown))}; accepted: "
            f"'defaults', 'phases'", where=str(p))

    defaults_raw = _require_mapping(doc.get("defaults") or {}, "defaults")
    default_fields = _layer_fields(defaults_raw, "defaults")
    defaults = _finalize_entry(default_fields, "defaults", frozenset(default_fields))
    if not defaults.supports(CAP_AGENTIC):
        raise LlmConfigError(
            "llm_config_defaults_not_agentic",
            f"`defaults` runs launches that carry no phase/substep (the `escalate` "
            f"diagnostician), which is an agentic session; provider {defaults.provider!r} has "
            f"capabilities {', '.join(sorted(defaults.capabilities)) or '(none)'}",
            where="defaults")

    phases_raw = _require_mapping(doc.get("phases") or {}, "phases")
    resolved: dict[tuple[str, str], ResolvedLeafEntry] = {}
    phase_fields: dict[str, dict[str, Any]] = {}
    for phase, phase_doc in phases_raw.items():
        if not isinstance(phase, str) or phase.strip() not in LLM_LEAF_PHASES:
            detail = ""
            if isinstance(phase, str) and phase.strip() == "build":
                detail = " ('build' is contractually deterministic and launches no LLM leaf)"
            raise LlmConfigError(
                "llm_config_unknown_phase",
                f"unknown phase {phase!r}{detail}; phases with an LLM leaf: "
                f"{', '.join(sorted(LLM_LEAF_PHASES))}", where=f"phases.{phase}")
        phase = phase.strip()
        where = f"phases.{phase}"
        phase_doc = _require_mapping(phase_doc or {}, where)
        substeps_raw = _require_mapping(phase_doc.get("substeps") or {}, f"{where}.substeps")
        phase_fields[phase] = _layer_fields(
            {k: v for k, v in phase_doc.items() if k != "substeps"}, where)
        for substep, substep_doc in substeps_raw.items():
            key = (phase, substep.strip() if isinstance(substep, str) else substep)
            if key not in LLM_LEAF_SUBSTEPS:
                raise LlmConfigError(
                    "llm_config_unknown_substep",
                    f"{phase!r} has no LLM leaf substep {substep!r}; LLM leaves of this phase: "
                    f"{', '.join(sorted(s for p, s in LLM_LEAF_SUBSTEPS if p == phase))}",
                    where=f"{where}.substeps.{substep}")
            sub_where = f"{where}.substeps.{key[1]}"
            merged, declared, declared_local = _merge_layers((
                default_fields, phase_fields[phase],
                _layer_fields(_require_mapping(substep_doc or {}, sub_where), sub_where)))
            entry = _finalize_entry(merged, sub_where, declared, declared_local)
            _validate_assignment(key[0], key[1], entry, sub_where)
            resolved[key] = entry

    # Every LLM leaf gets an entry, so no consumer re-implements the fallback.
    for key in sorted(LLM_LEAF_SUBSTEPS):
        if key in resolved:
            continue
        phase, substep = key
        where = f"phases.{phase}"
        merged, declared, declared_local = _merge_layers(
            (default_fields, phase_fields.get(phase, {})))
        entry = _finalize_entry(merged, where if phase in phase_fields else "defaults",
                                declared, declared_local)
        _validate_assignment(phase, substep, entry,
                             where if phase in phase_fields else "defaults")
        resolved[key] = entry

    return LlmConfig(
        path=str(p),
        sha256=config_sha256(p),
        defaults=defaults,
        entries=dict(resolved),
    )


# --- hashing / legacy bridge ---------------------------------------------------------

def config_sha256(path: str | Path) -> str:
    """The config file's SHA-256 as `"sha256:<hex>"`, or `"sha256:missing"`.

    Same primitive and same string form as `orchestration_runtime._compute_sha256` (duplicated
    rather than imported to keep this module free of that dependency; a drift guard in the
    tests compares the two on the same bytes). Hashing FILE BYTES, not the resolved
    configuration, is deliberate: it is what the resume gate compares, and a comment change
    that cannot alter behavior is still a change the operator should re-affirm."""
    p = Path(path)
    if not p.exists():
        return "sha256:missing"
    digest = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def shipped_config_path(backend: str, repo_root: str | Path | None = None) -> Path:
    """The shipped config that reproduces run-wide `--llm <backend>`.

    Prefers the copy inside `repo_root` — a run whose `--repo-root` is its own checkout should
    use that checkout's configs, and recording a root-relative path is what lets a resume find
    the file again. Falls back to the installed copy next to this module when `repo_root` has
    none, so a run against a scratch or partial tree still resolves."""
    name = f"{backend.strip().lower()}.yaml"
    if repo_root is not None:
        candidate = Path(repo_root) / "configs" / "llm" / name
        if candidate.exists():
            return candidate
    return Path(__file__).resolve().parent.parent / "configs" / "llm" / name


def llm_config_from_legacy(
    backend: str,
    agent_model: str = "",
    llm_command: str = "",
    *,
    repo_root: str | Path | None = None,
) -> LlmConfig:
    """Build the config that the deprecated `--llm/--agent-model/--llm-command` trio denotes.

    Loads the SHIPPED file for the backend (so the legacy path and the config path resolve
    through exactly the same code — that equivalence is acceptance criterion 1) and then
    applies the two run-wide overrides onto `defaults`, which propagate to every leaf because
    the shipped configs declare nothing per phase."""
    path = shipped_config_path(backend, repo_root)
    if not path.exists():
        raise LlmConfigError(
            "llm_config_unknown_provider",
            f"no shipped configuration for backend {backend!r} (expected {path})",
            where=str(path))
    cfg = load_llm_config(path)
    return apply_defaults_overrides(cfg, model=agent_model, command=llm_command)


def apply_defaults_overrides(
    cfg: LlmConfig, *, model: str = "", command: str = ""
) -> LlmConfig:
    """Return `cfg` with run-wide `model` / `command` overrides applied.

    The override reaches every entry that inherited the corresponding field from `defaults`;
    an entry that set its own is left alone, and an entry on a DIFFERENT provider than
    `defaults` is never touched (the legacy flags describe one backend, and forcing e.g. a
    claude model alias onto an HTTP entry would be exactly the provider-scope leak that
    `_merge_layer` exists to prevent). `sha256` still describes the file on disk; the override
    literals are recorded separately in the invocation record."""
    model = (model or "").strip()
    command = (command or "").strip()
    if not model and not command:
        return cfg

    def _override(entry: ResolvedLeafEntry, inherited: ResolvedLeafEntry,
                  *, is_defaults: bool = False) -> ResolvedLeafEntry:
        if entry.provider != inherited.provider:
            return entry

        def _inherited(field: str) -> bool:
            """The entry took this field from `defaults` rather than declaring its own.

            DECLARATION, not value equality: a `validate.judge.model: opus` written next to a
            `defaults.model: opus` is a deliberate pin that happens to agree, and a run-wide
            `--agent-model sonnet` must not move it. `defaults` itself is always overridable —
            that is what the flag overrides."""
            return is_defaults or field not in entry.declared

        changes: dict[str, Any] = {}
        if model and entry.model == inherited.model and _inherited("model"):
            changes["model"] = model
        if command and entry.command == inherited.command and _inherited("command"):
            # Reachable only for a CLI provider: the override is applied to entries sharing
            # `defaults`' provider, and `defaults` must be agentic (`llm_config_defaults_not_agentic`),
            # which no HTTP provider is.
            changes["command"] = command
        return ResolvedLeafEntry(**{**entry.__dict__, **changes}) if changes else entry

    new_defaults = _override(cfg.defaults, cfg.defaults, is_defaults=True)
    entries = {k: _override(e, cfg.defaults) for k, e in cfg.entries.items()}
    return LlmConfig(path=cfg.path, sha256=cfg.sha256,
                     defaults=new_defaults, entries=entries)


def describe_providers(cfg: LlmConfig) -> list[dict[str, str]]:
    """One row per DISTINCT provider the config can launch, for preflight (Phase 4).

    Deduplicated on the launch surface that a probe actually exercises — the backend token plus
    the command / endpoint — so two entries differing only in model are probed once."""
    seen: dict[tuple[str, str], dict[str, str]] = {}
    for _, entry in [("defaults", cfg.defaults)] + sorted(
            ((f"{p}.{s}", e) for (p, s), e in cfg.entries.items())):
        # The api_key_env is part of the probed surface: two entries sharing a base_url but
        # naming different key variables are two things that can independently be unset, and
        # collapsing them would leave the second one unprobed.
        key = (entry.backend_token, entry.command, entry.base_url, entry.api_key_env)
        if key in seen:
            continue
        seen[key] = {
            "backend": entry.backend_token,
            "provider": entry.provider,
            "command": entry.command,
            "base_url": entry.base_url,
            "api_key_env": entry.api_key_env,
        }
    return [seen[k] for k in sorted(seen)]

