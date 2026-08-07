#!/usr/bin/env python3
"""Tests for tools/leaf_usage.py — the ONE shape a leaf's token usage is recorded in."""

from __future__ import annotations

import unittest

from tools import leaf_usage as lu


class NormalizeLeafUsageTests(unittest.TestCase):
    """The ONE leaf-usage shape every backend records (issue #47).

    Before this, an agentic `claude` leaf recorded `{"status": "unavailable"}` and an HTTP leaf
    recorded a bare input/output pair — so a full billed run produced zero token numbers for 10
    of its 13 leaves and partial numbers for the other 3.
    """

    def test_the_four_token_classes_are_summed_into_total_tokens(self) -> None:
        """`total_tokens` is DERIVED here because no provider sends one, and
        `audit_orchestration.collect_token_cost_summary` accepts a durable row only when it is
        an int — so without this even a correctly persisted pure leaf was discarded by the
        audit. The cache classes are additional prompt classes, not subsets, so they count."""
        usage = lu.normalize_leaf_usage(
            {"input_tokens": 2, "output_tokens": 4,
             "cache_read_input_tokens": 14278, "cache_creation_input_tokens": 5849},
            source=lu.LEAF_USAGE_SOURCE_ENVELOPE)
        self.assertEqual(usage["total_tokens"], 2 + 4 + 14278 + 5849)
        self.assertEqual(usage["usage_source"], "cli_result_envelope")

    def test_subset_counts_are_recorded_but_never_added_into_the_total(self) -> None:
        """`reasoning_tokens` is part of `completion_tokens` and `cached_tokens` is part of
        `prompt_tokens`. Adding either would count the same tokens twice — and they are large:
        on `orch_20260807T002410Z_acf2b996` reasoning was 84-99.6% of the output tokens, and two
        otherwise identical `generate` calls reported 64 vs 32,832 cached prompt tokens."""
        usage = lu.normalize_leaf_usage(
            {"input_tokens": 33_000, "output_tokens": 23_538,
             "reasoning_tokens": 23_438, "cached_tokens": 32_832},
            source=lu.LEAF_USAGE_SOURCE_HTTP)
        self.assertEqual(usage["total_tokens"], 33_000 + 23_538)
        # ...and they ARE recorded: dropping them is what made `output_tokens` unreadable.
        self.assertEqual(usage["reasoning_tokens"], 23_438)
        self.assertEqual(usage["cached_tokens"], 32_832)

    def test_malformed_and_absent_counts_are_dropped_not_coerced(self) -> None:
        """A recorded 0 must mean the provider said 0. `True` is an `int` in Python, so the
        bool exclusion is load-bearing rather than pedantic."""
        usage = lu.normalize_leaf_usage(
            {"input_tokens": 11, "output_tokens": None, "cache_read_input_tokens": "12",
             "cache_creation_input_tokens": -1, "reasoning_tokens": True},
            source=lu.LEAF_USAGE_SOURCE_HTTP)
        self.assertEqual(usage, {"input_tokens": 11, "total_tokens": 11,
                                 "usage_source": "http_provider"})

    def test_nothing_measurable_returns_none_so_the_caller_records_a_marker(self) -> None:
        """An empty dict would read as zero cost; None makes the caller say why instead."""
        for raw in ({}, None, "usage", {"reasoning_tokens": 5}, {"total_tokens": 9}):
            self.assertIsNone(
                lu.normalize_leaf_usage(raw, source=lu.LEAF_USAGE_SOURCE_HTTP), msg=repr(raw))

    def test_the_provider_cost_and_detail_objects_are_carried(self) -> None:
        """The CLI envelope's own billed figure, and the detail objects the caller supplies —
        so a count this normalizer does not model is still on disk rather than recoverable only
        from a multi-MB raw SSE capture."""
        details = {"completion_tokens_details": {"reasoning_tokens": 40, "audio_tokens": 0}}
        usage = lu.normalize_leaf_usage(
            {"input_tokens": 1, "output_tokens": 2}, source=lu.LEAF_USAGE_SOURCE_ENVELOPE,
            cost_usd=0.065739, details=details)
        self.assertEqual(usage["cost_usd"], 0.065739)
        self.assertEqual(usage["provider_details"], details)
        # A bool is an int; a cost of `True` is not a cost. Nor is a negative one — and an
        # empty details object must not leave an empty `provider_details` key behind, which
        # would read as "the provider sent an object" when it sent nothing.
        for bad_cost in (True, -0.5, "0.5"):
            self.assertNotIn("cost_usd", lu.normalize_leaf_usage(
                {"input_tokens": 1}, source=lu.LEAF_USAGE_SOURCE_HTTP, cost_usd=bad_cost),
                msg=repr(bad_cost))
        self.assertNotIn("provider_details", lu.normalize_leaf_usage(
            {"input_tokens": 1}, source=lu.LEAF_USAGE_SOURCE_HTTP, details={}))

    def test_the_two_marker_states_are_distinguishable(self) -> None:
        """`not_measured` (no leaf launched — nothing to measure) is not a defect;
        `unavailable` (a usage channel that failed) is. Conflating them is what made the old
        marker useless: every row read `unavailable`, whatever had happened."""
        self.assertEqual(lu.leaf_usage_not_measured("no leaf"),
                         {"status": "not_measured", "reason": "no leaf"})
        self.assertEqual(lu.leaf_usage_unavailable("broke"),
                         {"status": "unavailable", "reason": "broke"})
        self.assertNotEqual(lu.LEAF_USAGE_NOT_MEASURED, lu.LEAF_USAGE_UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
