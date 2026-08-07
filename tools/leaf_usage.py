#!/usr/bin/env python3
"""The ONE shape a leaf's token usage is recorded in, for every backend.

A leaf's cost arrives in whatever vocabulary its provider speaks — the Claude CLI's result
envelope, an OpenAI-dialect `usage` object, a codex `turn.completed` event — and is recorded
in `agent_runs.jsonl` / `agents/<arid>/dialogs/agent.result.json`, which one audit reads
across all of them. This module owns the conversion and the two "there is no number" states,
so the conductor, the HTTP transport and the audit cannot disagree about what a usage row
means (issue #47).

Deliberately free of any transport, path or orchestration knowledge: stdlib only, pure
functions. The canonical description of the recorded field is `docs/WORKSPACE_LAYOUT.md`
(the `usage` section); the glossary term is `per-leaf usage`.
"""
from __future__ import annotations

from typing import Any


def _nonneg_int_or_none(value: Any) -> int | None:
    """A non-negative `int`, or None for anything else.

    `bool` is excluded although it IS an `int` (`True` would otherwise record as 1 token),
    and so are floats, negatives and strings: a value this module cannot vouch for is
    dropped rather than coerced, so a recorded count is always something the provider
    actually said. (`orchestration_diagnostics` carries its own copy for the transcript
    sums it performs; this module must not depend on that one.)
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


# The four token classes a Claude CLI `usage` object reports, and the vocabulary every other
# provider's dialect is mapped onto. THE definition: `orchestration_diagnostics` derives its
# transcript and pure-attempt sum keys from this one, so a new token class (or a rename)
# cannot land in one aggregation and silently miss the other.
LEAF_TOKEN_CLASS_KEYS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)

# The two `usage` marker states, which a numeric usage dict never carries. They are
# DISTINCT on purpose, and the distinction is the whole point (issue #47):
#   - `not_measured` — this launch has no usage channel at all, so no measurement was
#     ever attempted. A deterministic in-process substep spawns no leaf; a backend may
#     report no usage. Nothing is broken and nothing can be fixed by re-running.
#   - `unavailable`  — a usage channel existed and the measurement FAILED: the envelope
#     was unparseable, or carried no usage object. That is a defect to investigate.
# Conflating them is what made the old marker useless: every row read `unavailable`, so
# an operator could not tell "never instrumented" from "instrumentation broke".
LEAF_USAGE_NOT_MEASURED = "not_measured"
LEAF_USAGE_UNAVAILABLE = "unavailable"

# `usage_source` provenance values — which channel the numbers came from, mirroring the
# `agent_model_provenance` discipline (a recorded number must name where it came from).
LEAF_USAGE_SOURCE_ENVELOPE = "cli_result_envelope"
LEAF_USAGE_SOURCE_HTTP = "http_provider"
LEAF_USAGE_SOURCE_CODEX = "codex_turn_event"
# A row recorded before `usage_source` existed: the numbers are real, the channel that
# produced them was not written down. A distinct value rather than a storage location, so a
# reader cannot mistake "we do not know" for a channel that exists.
LEAF_USAGE_SOURCE_UNRECORDED = "unrecorded"

# Token counts that are a SUBSET of another class and must therefore never be added into
# `total_tokens`: OpenAI's `reasoning_tokens` is part of `completion_tokens`, and its
# `cached_tokens` is part of `prompt_tokens`. They are recorded because they are the term
# that dominates — on `orch_20260807T002410Z_acf2b996` reasoning was 84% of `completion_tokens`
# on two `generate` calls and 99.6% on a `verify` call — and excluded from the sum because
# adding them would double-count the very same tokens.
_SUBSET_USAGE_KEYS: tuple[str, ...] = (
    "reasoning_tokens",
    "cached_tokens",
)


def leaf_usage_not_measured(reason: str) -> dict[str, Any]:
    """The marker for a launch that HAS no usage channel (see `LEAF_USAGE_NOT_MEASURED`)."""
    return {"status": LEAF_USAGE_NOT_MEASURED, "reason": reason}


def leaf_usage_unavailable(reason: str) -> dict[str, Any]:
    """The marker for a usage channel that FAILED (see `LEAF_USAGE_UNAVAILABLE`)."""
    return {"status": LEAF_USAGE_UNAVAILABLE, "reason": reason}


def normalize_leaf_usage(
    raw: Any,
    *,
    source: str,
    cost_usd: Any = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """One leaf's token usage in the single shape every writer and reader agrees on.

    `raw` carries the four CLI token classes under their canonical names
    (`LEAF_TOKEN_CLASS_KEYS`) — the Claude CLI result envelope uses them natively, and the
    HTTP readers map their dialect onto them — plus, optionally, the subset counts in
    `_SUBSET_USAGE_KEYS`. Absent and malformed values are dropped (`_nonneg_int_or_none`),
    never coerced: a recorded 0 must mean the provider said 0.

    `total_tokens` is DERIVED here, and is the sum of the four token classes only. The
    subset keys are deliberately excluded — see `_SUBSET_USAGE_KEYS`. This one rule is
    correct for both wire dialects, so there is no per-dialect total to keep in step:
    an OpenAI-dialect reader never populates the cache classes, and an Anthropic-dialect
    one never populates the subset keys.

    Deriving `total_tokens` is not cosmetic. `audit_orchestration.collect_token_cost_summary`
    accepts a durable usage row only when `total_tokens` is an `int`, and neither the CLI
    envelope nor the HTTP dict carries one — so before this, even a correctly persisted pure
    leaf's usage was discarded by the audit.

    Returns None when nothing measurable is present, so the caller records an explicit
    marker (`leaf_usage_unavailable`) instead of an empty dict that reads as zero cost.
    """
    if not isinstance(raw, dict):
        return None
    usage: dict[str, Any] = {}
    for key in LEAF_TOKEN_CLASS_KEYS:
        value = _nonneg_int_or_none(raw.get(key))
        if value is not None:
            usage[key] = value
    if not usage:
        return None
    for key in _SUBSET_USAGE_KEYS:
        value = _nonneg_int_or_none(raw.get(key))
        if value is not None:
            usage[key] = value
    usage["total_tokens"] = sum(usage.get(key, 0) for key in LEAF_TOKEN_CLASS_KEYS)
    usage["usage_source"] = source
    # The provider's own billed figure when it reports one (the CLI envelope's
    # `total_cost_usd`). A cost audit that has the money number does not have to model
    # per-model pricing to read a trend. `bool` is excluded because it is an `int`.
    if isinstance(cost_usd, (int, float)) and not isinstance(cost_usd, bool) and cost_usd >= 0:
        usage["cost_usd"] = float(cost_usd)
    # The provider's detail objects as the caller supplies them (OpenAI's
    # `completion_tokens_details` / `prompt_tokens_details`; the HTTP reader passes only their
    # int-valued entries, because this object is persisted without going through that module's
    # redaction). A count this normalizer does not model is then still on disk rather than
    # recoverable only from a multi-MB raw SSE capture.
    if isinstance(details, dict) and details:
        usage["provider_details"] = details
    return usage
