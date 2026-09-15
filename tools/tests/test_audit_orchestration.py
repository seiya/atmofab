#!/usr/bin/env python3
"""Tests for tools/audit_orchestration.py."""
from __future__ import annotations

import contextlib
import io
import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import audit_orchestration as ao
from tools import orchestration_diagnostics as diag
from tools.tests.test_orchestration_diagnostics import (
    CHILD_ARID,
    _open_dangling_window,
)
from tools.audit_orchestration import (
    audit,
    collect_agent_run_summary,
    collect_token_cost_summary,
    collect_pure_leaf_ab_summary,
    _render_markdown,
    _render_pure_leaf_ab,
    _render_pure_leaf_row,
    _render_incident_body,
)


def _write_jsonl(path: Path, records: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _make_block(policy: str, command: str = "cmd", fix_hint: dict | None = None) -> dict:
    audit_detail: dict = {"policy": policy}
    if fix_hint is not None:
        audit_detail["fix_hint"] = fix_hint
    return {
        "action": "block",
        "tool_name": "Bash",
        "payload_summary": {"command": command},
        "audit_detail": audit_detail,
        "ts": "2026-05-09T00:00:00Z",
    }


class TokenCostSummaryTests(unittest.TestCase):
    """The per-leaf token cost — read from the durable `usage` rows of agent_runs.jsonl and
    from nothing else (issue #179 deleted the ~/.claude reconstruction)."""

    def test_a_marker_row_is_not_usage(self) -> None:
        # finalize_child persists each leaf's usage into agent_runs.jsonl; that row is the
        # whole source, and a `{"status": "unavailable"}` marker on it must NOT count as usage.
        from tools.audit_orchestration import collect_token_cost_summary

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            child = "aaaa1111-1111-4111-8111-111111111111"
            runs = [
                {
                    "agent_run_id": child,
                    "agent_role": "substep",
                    "status": "pass",
                    "usage": {
                        "input_tokens": 10, "output_tokens": 10,
                        "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 0,
                        "total_tokens": 1020, "assistant_turns": 5, "peak_context_tokens": 1010,
                    },
                }
            ]
            tcs = collect_token_cost_summary(repo, {}, runs)
            self.assertEqual(tcs["children_total_tokens"], 1020)
            self.assertEqual(tcs["children"]["per_child"][child]["source"], "agent_runs.jsonl")
            runs2 = [{"agent_run_id": child, "agent_role": "substep", "status": "pass",
                      "usage": {"status": "unavailable", "reason": "x"}}]
            tcs2 = collect_token_cost_summary(repo, {}, runs2)
            self.assertEqual(tcs2["children"]["matched_count"], 0)

    def test_every_backends_row_shape_is_accepted_by_the_durable_path(self) -> None:
        """The gate is `total_tokens` being an int, and NO provider sends one — so before it
        was derived (`normalize_leaf_usage`), even a correctly persisted pure-claude leaf's
        envelope usage fell through to a ~/.claude lookup that could not match. Both shapes
        the conductor now writes must be read straight off `agent_runs.jsonl`."""
        from tools.audit_orchestration import _render_token_cost

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            envelope_child = "aaaa1111-1111-4111-8111-111111111111"
            http_child = "bbbb2222-2222-4222-8222-222222222222"
            runs = [
                {"agent_run_id": envelope_child, "agent_role": "substep", "status": "pass",
                 "usage": {"input_tokens": 2, "output_tokens": 4,
                           "cache_read_input_tokens": 14278,
                           "cache_creation_input_tokens": 5849, "total_tokens": 20133,
                           "usage_source": "cli_result_envelope", "cost_usd": 0.065739}},
                {"agent_run_id": http_child, "agent_role": "substep", "status": "pass",
                 "usage": {"input_tokens": 33000, "output_tokens": 23538,
                           "reasoning_tokens": 23438, "cached_tokens": 32832,
                           "total_tokens": 56538, "usage_source": "http_provider"}},
            ]
            # No `home` patch and no opt-in flag: the default audit must not need ~/.claude.
            tcs = collect_token_cost_summary(repo, {}, runs)
            self.assertEqual(tcs["children"]["matched_count"], 2)
            self.assertEqual(tcs["children_total_tokens"], 20133 + 56538)
            self.assertEqual(tcs["children"]["unmatched_arids"], [])
            lines: list[str] = []
            _render_token_cost(tcs, lines)
            joined = "\n".join(lines)
            # The term that made `output_tokens` alone misleading, and the cache split.
            self.assertIn("of which reasoning: 23,438", joined)
            self.assertIn("of which prompt-cache hits: 32,832", joined)
            self.assertIn("$0.0657", joined)

    def test_a_run_that_reported_no_cost_does_not_render_a_zero_bill(self) -> None:
        """`$0.0000` reads as "this run was free", where the truth is that no provider
        reported a figure — the same failure the leaf total avoids by saying `unavailable`."""
        from tools.audit_orchestration import collect_token_cost_summary, _render_token_cost

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            runs = [{"agent_run_id": "aaaa1111-1111-4111-8111-111111111111",
                     "agent_role": "substep", "status": "pass",
                     "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}}]
            tcs = collect_token_cost_summary(repo, {}, runs)
            self.assertNotIn("cost_usd", tcs["children"]["children_total"])
            lines: list[str] = []
            _render_token_cost(tcs, lines)
            self.assertNotIn("provider-reported cost", "\n".join(lines))

    def test_a_legacy_row_without_a_total_is_still_read(self) -> None:
        """Rows written before `total_tokens` was derived at finalize time carry only the raw
        pair — which is every HTTP leaf of the run that filed issue #47. Deriving the total
        here as well is what makes those runs readable instead of reporting `available=False`
        for a run that did record numbers."""
        from tools.audit_orchestration import collect_token_cost_summary

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            child = "aaaa1111-1111-4111-8111-111111111111"
            runs = [{"agent_run_id": child, "agent_role": "substep", "status": "pass",
                     "usage": {"input_tokens": 40_080, "output_tokens": 82_210}}]
            tcs = collect_token_cost_summary(repo, {}, runs)
            self.assertEqual(tcs["children_total_tokens"], 40_080 + 82_210)
            self.assertEqual(tcs["children"]["per_child"][child]["source"], "agent_runs.jsonl")
            # The channel that produced those numbers was not written down, and must not be
            # guessed as one that exists — nor as the file they were read out of.
            self.assertEqual(tcs["children"]["per_child"][child]["usage_source"], "unrecorded")

    def test_the_per_child_table_names_the_reasoning_share_and_the_channel(self) -> None:
        """The table is what an operator reads to find the expensive leaf. `reasoning` is the
        term that made `output_tokens` unreadable, and `source` says which channel produced
        the numbers — a row with neither is indistinguishable from one whose provider reported
        no split, so both columns have to render what the row actually holds."""
        from tools.audit_orchestration import collect_token_cost_summary, _render_token_cost

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            runs = [
                {"agent_run_id": "aaaa1111-1111-4111-8111-111111111111",
                 "agent_role": "substep", "status": "pass",
                 "usage": {"input_tokens": 33000, "output_tokens": 23538,
                           "reasoning_tokens": 23438, "total_tokens": 56538,
                           "usage_source": "http_provider"}},
                {"agent_run_id": "bbbb2222-2222-4222-8222-222222222222",
                 "agent_role": "substep", "status": "pass",
                 "usage": {"input_tokens": 2, "output_tokens": 4, "total_tokens": 6,
                           "usage_source": "cli_result_envelope"}},
            ]
            lines: list[str] = []
            _render_token_cost(collect_token_cost_summary(repo, {}, runs), lines)
            joined = "\n".join(lines)
            self.assertIn("| leaf agent_run_id | total | reasoning | source |", joined)
            self.assertIn("| 56,538 | 23,438 | http_provider |", joined)
            # ...and a provider that reported no split says so, rather than rendering a 0.
            self.assertIn("| 6 | n/a | cli_result_envelope |", joined)

    def test_the_markers_are_carried_for_a_json_consumer(self) -> None:
        """The rendered lines only COUNT the markers; `--format json` is where an operator
        (or a script) reads which arid said what, and `reason` is the only thing that
        distinguishes a dead leaf from an envelope that carried no usage."""
        from tools.audit_orchestration import collect_token_cost_summary

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            runs = [{"agent_run_id": "aaaa1111-1111-4111-8111-111111111111",
                     "agent_role": "substep", "status": "fail",
                     "usage": {"status": "unavailable", "reason": "no result envelope"}},
                    {"agent_run_id": "bbbb2222-2222-4222-8222-222222222222",
                     "agent_role": "substep", "status": "pass",
                     "usage": {"status": "not_measured", "reason": "deterministic"}}]
            markers = collect_token_cost_summary(repo, {}, runs)["children"]["markers"]
            self.assertEqual(markers["aaaa1111-1111-4111-8111-111111111111"],
                             {"status": "unavailable", "reason": "no result envelope"})
            self.assertEqual(markers["bbbb2222-2222-4222-8222-222222222222"]["status"],
                             "not_measured")

    def test_not_measured_is_reported_apart_from_a_failed_measurement(self) -> None:
        """A deterministic in-process substep launched no leaf, so `not_measured` is an
        accounted-for row, not a gap; `unavailable` IS a gap and keeps its warning. Reporting
        both as "no locatable transcript" is what made every row look broken."""
        from tools.audit_orchestration import _render_token_cost

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            runs = [
                {"agent_run_id": "aaaa1111-1111-4111-8111-111111111111",
                 "agent_role": "substep", "status": "pass",
                 "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}},
                {"agent_run_id": "bbbb2222-2222-4222-8222-222222222222",
                 "agent_role": "substep", "status": "pass",
                 "usage": {"status": "not_measured", "reason": "deterministic"}},
                {"agent_run_id": "cccc3333-3333-4333-8333-333333333333",
                 "agent_role": "substep", "status": "fail",
                 "usage": {"status": "unavailable", "reason": "no result envelope"}},
            ]
            tcs = collect_token_cost_summary(repo, {}, runs)
            self.assertEqual(tcs["children"]["not_measured"],
                             ["bbbb2222-2222-4222-8222-222222222222"])
            self.assertEqual(tcs["children"]["usage_unavailable"],
                             ["cccc3333-3333-4333-8333-333333333333"])
            # Neither is "unmatched": both said what happened.
            self.assertEqual(tcs["children"]["unmatched_arids"], [])
            lines: list[str] = []
            _render_token_cost(tcs, lines)
            joined = "\n".join(lines)
            self.assertIn("1 run(s) not measured", joined)
            self.assertIn("1 leaf reported no usage", joined)

    def test_a_run_whose_every_leaf_recorded_a_marker_still_renders(self) -> None:
        """The shape of the run that filed issue #47 — and of any run whose leaves all died.
        `available=False` would print "no child usage located", which is the opposite of what
        happened: every leaf said what it had, and what it had was nothing."""
        from tools.audit_orchestration import collect_token_cost_summary, _render_token_cost

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            runs = [{"agent_run_id": f"aaaa{i}111-1111-4111-8111-111111111111",
                     "agent_role": "substep", "status": "fail",
                     "usage": {"status": "unavailable", "reason": "no result envelope"}}
                    for i in range(1, 4)]
            tcs = collect_token_cost_summary(repo, {}, runs)
            self.assertTrue(tcs["available"])
            lines: list[str] = []
            _render_token_cost(tcs, lines)
            joined = "\n".join(lines)
            self.assertIn("3 leaves reported no usage", joined)
            self.assertNotIn("no child usage located", joined)
            # ...and the total must NOT read `0 tokens`, which says the node was free. A
            # marker makes the section renderable; it does not make it a measurement.
            self.assertIn("**leaf total**: unavailable", joined)
            self.assertNotIn("0 tokens", joined)
            # ...and the JSON consumer is told the same thing as the renderer: something WAS
            # located — every leaf said why it has no numbers.
            self.assertNotIn("reason", tcs["children"])

    def test_unavailable_when_nothing_matched(self) -> None:
        # A row with neither numbers nor a marker: report unavailable, not a 0-token
        # breakdown, and name the row as unaccounted.
        from tools.audit_orchestration import collect_token_cost_summary, _render_token_cost

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            child = "bbbb2222-2222-4222-8222-222222222222"
            runs = [{"agent_run_id": child, "agent_role": "substep", "status": "pass"}]
            tcs = collect_token_cost_summary(repo, {}, runs)
            self.assertFalse(tcs["available"])
            self.assertEqual(tcs["children"]["unmatched_arids"], [child])
            lines: list[str] = []
            _render_token_cost(tcs, lines)
            self.assertIn("unavailable", "\n".join(lines))
            self.assertNotIn("0 tokens", "\n".join(lines))

    def test_the_conductors_own_row_is_not_an_unaccounted_leaf(self) -> None:
        # Every real orchestration has one `agent_role: orchestration` row, named by
        # `orchestration_meta.json#orchestration_agent_run_id`, with no `usage` (48 of 48
        # in the corpus). Dropping the exclusion prints "1 leaf arid(s) carry no usage
        # field" on every audit; this is the pin the deleted parent-path fixtures held.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            parent = "0e750000-0000-4000-8000-000000000000"
            child = "aaaa1111-1111-4111-8111-111111111111"
            runs = [{"agent_run_id": parent, "agent_role": "orchestration", "status": "pass"},
                    {"agent_run_id": child, "agent_role": "substep", "status": "pass",
                     "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}]
            tcs = collect_token_cost_summary(
                repo, {"orchestration_agent_run_id": parent}, runs)
            self.assertEqual(tcs["children"]["unmatched_arids"], [])
            self.assertNotIn(parent, tcs["children"]["per_child"])
            lines: list[str] = []
            ao._render_token_cost(tcs, lines)
            self.assertNotIn("carry no usage field", "\n".join(lines))

    def test_an_unaccounted_row_is_named_as_a_leaf(self) -> None:
        # One numeric row makes the section render; the row with no usage field is then
        # counted in the vocabulary the section uses everywhere else — leaf, not child.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            runs = [{"agent_run_id": "aaaa1111-1111-4111-8111-111111111111",
                     "agent_role": "substep", "status": "pass",
                     "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}},
                    {"agent_run_id": "bbbb2222-2222-4222-8222-222222222222",
                     "agent_role": "substep", "status": "pass"}]
            lines: list[str] = []
            ao._render_token_cost(collect_token_cost_summary(repo, {}, runs), lines)
            joined = "\n".join(lines)
            self.assertIn("1 leaf arid(s) carry no usage field at all", joined)
            self.assertNotIn("child", joined)

    def test_the_leaf_total_is_the_whole_section(self) -> None:
        # The durable rows alone produce the total, and no "parent" side exists to be
        # partial against: the conductor is a Python process with no session of its own.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            child = "aaaa1111-1111-4111-8111-111111111111"
            runs = [{"agent_run_id": child, "agent_role": "substep", "status": "pass",
                     "usage": {"input_tokens": 10, "output_tokens": 10,
                               "cache_read_input_tokens": 1000, "total_tokens": 1020}}]
            tcs = collect_token_cost_summary(repo, {}, runs)
            self.assertTrue(tcs["available"])
            self.assertEqual(tcs["children_total_tokens"], 1020)
            for retired in ("parent", "parent_total_tokens", "node_total_tokens",
                            "children_fraction"):
                self.assertNotIn(retired, tcs)
            lines: list[str] = []
            ao._render_token_cost(tcs, lines)
            joined = "\n".join(lines)
            self.assertIn("## Token cost (per leaf)", joined)
            self.assertIn("**leaf total**: 1,020 tokens", joined)
            self.assertNotIn("parent", joined)
            self.assertNotIn("partial", joined)

    def test_render_handles_unavailable(self) -> None:
        summary = {"available": False, "reason": "claude projects dir missing"}
        from tools.audit_orchestration import _render_token_cost

        lines: list[str] = []
        _render_token_cost(summary, lines)
        joined = "\n".join(lines)
        self.assertIn("unavailable", joined)
        self.assertIn("claude projects dir missing", joined)


class CollectAgentRunSummaryTests(unittest.TestCase):
    def test_status_counts(self) -> None:
        runs = [
            {"agent_run_id": "r1", "status": "pass", "finished_at": "2026-05-09T00:00:00Z"},
            {"agent_run_id": "r2", "status": "fail", "finished_at": "2026-05-09T00:01:00Z"},
            {"agent_run_id": "r3", "status": "pass", "finished_at": "2026-05-09T00:02:00Z"},
        ]
        result = collect_agent_run_summary(runs)
        self.assertEqual(result["status_counts"]["pass"], 2)
        self.assertEqual(result["status_counts"]["fail"], 1)
        self.assertEqual(result["missing_finished_at"], [])

    def test_invalid_runs_appear_in_status_counts(self) -> None:
        """Regression: agent_runs_invalid.jsonl entries (terminal-validation
        fallback fail records) must appear in status_counts so operators see
        them in the per-status breakdown — not just in the separate
        invalid_run_count field."""
        from tools.audit_orchestration import collect_agent_run_summary
        result = collect_agent_run_summary(
            [{"agent_run_id": "r1", "status": "pass", "finished_at": "x"}],
            [
                {"agent_run_id": "r2", "status": "fail",
                 "fail_reason": "terminal_payload_validation_error"},
                {"agent_run_id": "r3", "status": "fail"},
            ],
        )
        self.assertEqual(result["status_counts"]["pass"], 1)
        self.assertEqual(result["status_counts"]["fail"], 2)

    def test_missing_finished_at(self) -> None:
        runs = [{"agent_run_id": "r1", "status": "pass"}]
        result = collect_agent_run_summary(runs)
        self.assertIn("r1", result["missing_finished_at"])


class AuditIntegrationTests(unittest.TestCase):
    """audit() end-to-end with a small fixture workspace."""

    def _build_fixture(self, tmp: str, orch_id: str) -> None:
        """A minimal orchestration record. It carried a `hooks/native_hook_events.jsonl`
        with six blocks and three auto-approvals until PR-2 of issue #171: the audit's whole
        hook-event half — per-policy block counts, the benign split, the `fix_hint` report,
        the auto-approve count and the five events before `fail_closed` — is deleted with the
        leaf hook layer that produced the file."""
        root = Path(tmp)
        orch_root = root / "workspace" / "orchestrations" / orch_id
        orch_root.mkdir(parents=True)

        phase_log = [
            {"event": "set_status", "to": "fail_closed", "ts": "2026-05-09T00:10:00Z"},
        ]
        _write_jsonl(orch_root / "phase_state_log.jsonl", phase_log)

        agent_runs = [
            {"agent_run_id": "run1", "status": "pass", "finished_at": "2026-05-09T00:05:00Z"},
            {"agent_run_id": "run2", "status": "fail", "finished_at": "2026-05-09T00:09:00Z"},
        ]
        _write_jsonl(orch_root / "agent_runs.jsonl", agent_runs)

    def test_audit_returns_expected_structure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orch_id = "orch_test_20260509T000000Z_aabbccdd"
            self._build_fixture(tmp, orch_id)
            result = audit(Path(tmp), orch_id)

        self.assertEqual(result["orchestration_id"], orch_id)
        self.assertEqual(result["fail_closed_at"], "2026-05-09T00:10:00Z")
        self.assertEqual(result["agent_run_summary"]["status_counts"]["pass"], 1)
        self.assertEqual(result["agent_run_summary"]["status_counts"]["fail"], 1)
        # The retired keys are GONE, not zeroed: a reader that still asks for one gets a
        # KeyError rather than a clean negative over a file nothing writes.
        for retired in ("total_hook_events", "total_blocks", "policy_block_counts",
                        "fix_hint_stats", "fail_closed_timeline",
                        "allow_auto_approve_stats", "suspicious_benign_volume"):
            self.assertNotIn(retired, result)
        # ...and the parent/node token keys went the same way (issue #179).
        for retired in ("parent", "parent_total_tokens", "node_total_tokens",
                        "children_fraction"):
            self.assertNotIn(retired, result["token_cost_summary"])

    def test_audit_takes_no_transcript_option(self) -> None:
        # The opt-in that used to reach ~/.claude is not a silently accepted no-op.
        with tempfile.TemporaryDirectory() as tmp:
            orch_id = "orch_test_sig"
            self._build_fixture(tmp, orch_id)
            with self.assertRaises(TypeError):
                audit(Path(tmp), orch_id, token_cost_from_transcripts=True)  # type: ignore[call-arg]

    def test_audit_renders_markdown_without_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orch_id = "orch_test_md"
            self._build_fixture(tmp, orch_id)
            result = audit(Path(tmp), orch_id)
        md = _render_markdown(result)
        self.assertIn("fail_closed at:", md)
        self.assertNotIn("Policy block counts", md)
        self.assertNotIn("Auto-approved Write/Edit", md)

    def test_audit_handles_missing_log_files_gracefully(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orch_id = "orch_empty"
            (Path(tmp) / "workspace" / "orchestrations" / orch_id).mkdir(parents=True)
            result = audit(Path(tmp), orch_id)
        self.assertIsNone(result["fail_closed_at"])
        self.assertEqual(result["invalid_run_count"], 0)

    def test_audit_flags_corrupted_jsonl(self) -> None:
        """Regression: malformed JSON lines must be surfaced, not silently dropped."""
        with tempfile.TemporaryDirectory() as tmp:
            orch_id = "orch_corrupt"
            orch_root = Path(tmp) / "workspace" / "orchestrations" / orch_id
            orch_root.mkdir(parents=True)
            (orch_root / "agent_runs.jsonl").write_text(
                '{"agent_run_id":"a","status":"pass"}\n'
                '{this is not valid json\n'
                '{"agent_run_id":"b","status":"fail"}\n',
                encoding="utf-8",
            )
            result = audit(Path(tmp), orch_id)
        self.assertTrue(result["data_integrity_warning"])
        self.assertEqual(result["parse_error_count"], 1)
        self.assertEqual(result["parse_errors"][0]["line_number"], 2)
        # Valid lines still parsed.
        self.assertEqual(result["agent_run_summary"]["status_counts"]["pass"], 1)
        self.assertEqual(result["agent_run_summary"]["status_counts"]["fail"], 1)

    def test_audit_clean_logs_no_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orch_id = "orch_clean"
            orch_root = Path(tmp) / "workspace" / "orchestrations" / orch_id
            orch_root.mkdir(parents=True)
            (orch_root / "agent_runs.jsonl").write_text(
                '{"agent_run_id":"a","status":"pass"}\n', encoding="utf-8",
            )
            result = audit(Path(tmp), orch_id)
        self.assertFalse(result["data_integrity_warning"])
        self.assertEqual(result["parse_error_count"], 0)

    def test_audit_picks_up_invalid_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orch_id = "orch_inv"
            orch_root = Path(tmp) / "workspace" / "orchestrations" / orch_id
            orch_root.mkdir(parents=True)
            _write_jsonl(orch_root / "agent_runs_invalid.jsonl", [
                {"agent_run_id": "run_bad", "status": "fail",
                 "fail_reason": "terminal_payload_validation_error"},
            ])
            result = audit(Path(tmp), orch_id)
        self.assertEqual(result["invalid_run_count"], 1)
        self.assertIn("run_bad", result["invalid_run_ids"])


class LaunchIncidentSnapshotTests(unittest.TestCase):
    def test_audit_surfaces_persisted_snapshot_after_window_cleared(self) -> None:
        # P2: after --resume clears the active-child markers, live detection returns
        # None, but a persisted launch_incident.runtime.*.json must still be surfaced
        # so the documented later-diagnosis path works.
        with tempfile.TemporaryDirectory() as tmp:
            orch_id = "orch_snap"
            orch_root = Path(tmp) / "workspace" / "orchestrations" / orch_id
            orch_root.mkdir(parents=True)
            # No active_child markers (window cleared) → live build returns None.
            (orch_root / "launch_incident.runtime.0123456789ab.json").write_text(
                json.dumps(
                    {
                        "schema": "launch_incident/v1",
                        "orchestration_id": orch_id,
                        "dangling_child": {
                            "agent_run_id": "f00d83b5",
                            "node_key_safe": "component__x__0.1.0",
                            "step": "compile",
                            "substep": "verify",
                            "launch_recorded_at": "2026-06-16T12:36:58Z",
                            "elapsed_seconds": 700.0,
                        },
                        "host_session_id": "b60f2e51",
                        "transcripts": {"child_transcript": {"found": False, "reason": "cleaned"}},
                        "abort_marker": {
                            "interrupted": True,
                            "interrupt_ts": "2026-06-16T12:48:47Z",
                            "interrupt_text": "[Request interrupted by user]",
                            "last_activity_ts": "2026-06-16T12:38:47Z",
                            "dead_air_seconds": 600.0,
                        },
                    }
                ),
                encoding="utf-8",
            )
            result = audit(Path(tmp), orch_id)
            self.assertIsNone(result["launch_incident"])
            self.assertEqual(len(result["launch_incident_snapshots"]), 1)
            md = _render_markdown(result)
        self.assertIn("Captured incident snapshots", md)
        self.assertIn("launch_incident.runtime.0123456789ab.json", md)
        # Decisive evidence from the snapshot's abort_marker is rendered even though
        # the live transcript is gone.
        self.assertIn("[Request interrupted by user]", md)
        self.assertIn("600s", md)

    def test_audit_reports_nothing_when_no_window_and_no_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orch_id = "orch_clean"
            (Path(tmp) / "workspace" / "orchestrations" / orch_id).mkdir(parents=True)
            result = audit(Path(tmp), orch_id)
            self.assertIsNone(result["launch_incident"])
            self.assertEqual(result["launch_incident_snapshots"], [])
            md = _render_markdown(result)
        self.assertIn("no captured incident snapshots", md)


class LiveIncidentMatchMethodTests(unittest.TestCase):
    """The audit renders ``matched via `<match_method>```; the live route must fill it.

    It did not, so an audit run exactly as `docs/RUNBOOK.md` §launch-incomplete-recovery
    instructs printed ``matched via `None``` over a transcript it had genuinely FOUND
    (measured on `orch_20260827T165705Z_af3800b5`, issue #137). This is the end-to-end
    pin of the symptom: window -> located transcript -> rendered markdown.
    """

    ORCH_ID = "orch_test"

    def test_a_live_incident_names_the_method_and_the_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            home = Path(tmp) / "home"
            orch_root = repo / "workspace" / "orchestrations" / self.ORCH_ID
            orch_root.mkdir(parents=True)
            _open_dangling_window(orch_root)
            proj = home / ".claude" / "projects" / "some-slug"
            proj.mkdir(parents=True)
            (proj / f"{CHILD_ARID}.jsonl").write_text(
                json.dumps({"type": "assistant",
                            "timestamp": "2026-06-16T12:38:47.000Z",
                            "message": {"role": "assistant", "content": []}}) + "\n",
                encoding="utf-8")
            with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False):
                result = audit(repo, self.ORCH_ID)
                md = _render_markdown(result)
        self.assertIsNotNone(result["launch_incident"])
        # The WHOLE clause, not `assertIn` on the root alone: the root is a prefix of
        # the transcript path printed two words earlier, so a substring check is true
        # however wrong the reported root is. Pinning the clause is what makes a
        # locator that reports `roots[0]` regardless of where it matched fail HERE too,
        # and not only in the diagnostics suite.
        self.assertIn(
            f"(matched via `session_id` under `{home / '.claude' / 'projects'}`)", md)
        self.assertNotIn("matched via `None`", md)

    def test_a_pure_leafs_hit_in_the_operator_home_names_that_root(self) -> None:
        """The shape the corrected prose is about, and the one that makes the root
        OBSERVABLE here: a private home exists but the transcript is in the operator's.

        A PURE leaf is prepared no private home (`orchestration_runtime` gates that on
        the agentic shape), so it writes to `~/.claude/projects` on a CURRENT run. The
        test above cannot see a locator that reports `roots[0]` regardless of where it
        matched, because with no private home configured `roots[0]` IS the root that
        matched; here the two differ, so the wrong answer is rendered and caught.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            home = Path(tmp) / "home"
            orch_root = repo / "workspace" / "orchestrations" / self.ORCH_ID
            orch_root.mkdir(parents=True)
            _open_dangling_window(orch_root)
            private = home / ".atmofab" / "homes" / self.ORCH_ID / "claude"
            (private / "projects").mkdir(parents=True)
            (orch_root / "orchestration_meta.json").write_text(
                json.dumps({"claude_workflow_home": str(private)}), encoding="utf-8")
            operator_projects = home / ".claude" / "projects"
            (operator_projects / "some-slug").mkdir(parents=True)
            (operator_projects / "some-slug" / f"{CHILD_ARID}.jsonl").write_text(
                json.dumps({"type": "assistant",
                            "timestamp": "2026-06-16T12:38:47.000Z",
                            "message": {"role": "assistant", "content": []}}) + "\n",
                encoding="utf-8")
            with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False):
                md = _render_markdown(audit(repo, self.ORCH_ID))
        self.assertIn(f"(matched via `session_id` under `{operator_projects}`)", md)
        self.assertNotIn(str(private / "projects"), md)


class IncidentMatchMethodRenderTests(unittest.TestCase):
    """The renderer's two shapes, side by side: the clause serves both."""

    def _md(self, ct: dict) -> str:
        lines: list[str] = []
        _render_incident_body({"dangling_child": {}, "transcripts": {"child_transcript": ct}},
                              lines)
        return "\n".join(lines)

    def test_a_legacy_snapshot_keeps_its_method_and_gains_no_root(self) -> None:
        # A persisted `launch_incident.runtime.*.json` from the host-session era carries
        # `tool_use_id` and no root. Rendering it must be untouched by the live fix.
        md = self._md({"found": True, "path": "/x.jsonl", "match_method": "tool_use_id"})
        self.assertIn("(matched via `tool_use_id`)", md)
        self.assertNotIn("under `", md)

    def test_a_missing_method_renders_as_None_rather_than_a_guess(self) -> None:
        """The comment above the clause justifies itself by the ABSENCE of a fallback.

        That justification is the property worth mutating: `ct.get('match_method') or
        'session_id'` reads like a courtesy and would make a producer regression
        invisible, which is the whole defect this branch exists to close. Nothing else
        in the suite observes it — the live pin is green under such a fallback by
        construction, because the live producer fills the key.
        """
        md = self._md({"found": True, "path": "/x.jsonl"})
        self.assertIn("(matched via `None`)", md)

    def test_a_live_incident_appends_the_root_it_matched_under(self) -> None:
        md = self._md({"found": True, "path": "/x.jsonl", "match_method": "session_id",
                       "matched_projects_root": "/home/op/.atmofab/homes/o/claude/projects"})
        self.assertIn(
            "(matched via `session_id` under `/home/op/.atmofab/homes/o/claude/projects`)", md)


class LegacyIncidentApiErrorRenderTests(unittest.TestCase):
    def test_renders_api_error_from_raw_tail_when_structured_field_missing(self) -> None:
        """A legacy snapshot predating the structured api_error field still carries the
        529 marker in raw_tail; audit must surface it from there."""
        incident = {
            "dangling_child": {"agent_run_id": "child-1", "step": "compile",
                               "substep": "generate"},
            "host_session_id": "host-1",
            "transcripts": {
                "child_transcript": {
                    "found": True,
                    "path": "/x.jsonl",
                    "match_method": "tool_use_id",
                    "last_activity_ts": "2026-06-17T01:17:30.724Z",
                    "last_event_type": "assistant",
                    # No structured "api_error" field (legacy snapshot) ...
                    "raw_tail": [
                        {
                            "type": "assistant",
                            "isApiErrorMessage": True,
                            "apiErrorStatus": 529,
                            "message": {"role": "assistant", "content": [
                                {"type": "text", "text": "API Error: 529 Overloaded."}]},
                        }
                    ],
                }
            },
        }
        lines: list[str] = []
        _render_incident_body(incident, lines)
        md = "\n".join(lines)
        self.assertIn("transient API error", md)
        self.assertIn("529", md)
        self.assertIn("safe to", md)


class PureLeafABSummaryTest(unittest.TestCase):
    """collect_pure_leaf_ab_summary + _render_pure_leaf_ab (Z2 M-E)."""

    ORCH = "orch_pure_ab"
    SAFE = "comp__demo__0.1.0"
    PIPELINE_ID = "demo_20260716_001"
    PIPE = f"workspace/pipelines/{SAFE}/{PIPELINE_ID}"
    SRC = f"workspace/pipelines/{SAFE}/{PIPELINE_ID}/source/src_20260716_001"

    def _reserve(self, repo: Path, *, pipeline_id: str | None = None) -> None:
        """Write the pipeline reservation `prepare_node` writes before Compile runs.

        This is what discovery reads. The
        checkpoint only ever carries a non-empty `pipeline_ref` for the `validate`
        step (verified against every real orchestration in-repo), so a fixture that
        hand-builds a compile/generate entry WITH a `pipeline_ref` encodes a shape
        the runtime never produces, and would hide a generate-only run finding nothing.
        """
        res = repo / "workspace" / "orchestrations" / self.ORCH / "reservations" / self.SAFE
        res.mkdir(parents=True, exist_ok=True)
        (res / "generate.json").write_text(
            json.dumps(
                {
                    "node_key": "comp/demo@0.1.0",
                    "step": "generate",
                    # `is not None`, not `or`: an empty-string id is a case under test
                    # and `or` would silently swallow it into the default.
                    "reserved_ir_id": (
                        pipeline_id if pipeline_id is not None else self.PIPELINE_ID
                    ),
                }
            ),
            encoding="utf-8",
        )

    def _lay_out(self, repo: Path, *, executor="pure", with_metas=True) -> None:
        root = repo / "workspace" / "orchestrations" / self.ORCH
        root.mkdir(parents=True, exist_ok=True)
        (root / "orchestration_meta.json").write_text(
            json.dumps({"invocation": {"generate_executor": executor}}), encoding="utf-8"
        )
        (root / "preflight.json").write_text(
            json.dumps({"backend": "claude", "agent_version": "1.2.3 (Claude Code)"}),
            encoding="utf-8",
        )
        self._reserve(repo)
        if with_metas:
            src = repo / self.SRC
            src.mkdir(parents=True, exist_ok=True)
            (src / "bundle_meta.json").write_text(
                json.dumps(
                    {
                        "result": "pass",
                        "failure_category": None,
                        "attempts": 1,
                        "prompt_contract_version": "pure-1",
                        "per_attempt": [
                            {"agent_run_id": "g1", "model": "claude-opus-4-8", "usage": {"input_tokens": 400, "output_tokens": 900}}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (src / "verdict_meta.json").write_text(
                json.dumps(
                    {
                        "result": "pass",
                        "failure_category": None,
                        "attempts": 1,
                        "prompt_contract_version": "pure-1",
                        "per_attempt": [
                            {"agent_run_id": "v1", "model": "claude-opus-4-8", "usage": {"input_tokens": 500, "output_tokens": 30}}
                        ],
                    }
                ),
                encoding="utf-8",
            )

    def test_collect_surfaces_executor_version_and_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._lay_out(repo)
            meta = json.loads(
                (repo / "workspace" / "orchestrations" / self.ORCH / "orchestration_meta.json").read_text()
            )
            out = collect_pure_leaf_ab_summary(repo, self.ORCH, meta)
        self.assertTrue(out["available"])
        self.assertEqual(out["generate_executor"], "pure")
        self.assertEqual(out["agent_cli_version"], "1.2.3 (Claude Code)")
        self.assertEqual(len(out["pure_nodes"]), 1)
        node = out["pure_nodes"][0]
        self.assertEqual(node["source_dir"], self.SRC)  # repo-relative
        self.assertEqual(node["generate"]["usage_total"]["total_tokens"], 1300)
        self.assertEqual(node["verify"]["result"], "pass")

    def test_legacy_run_reports_unavailable_but_keeps_executor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._lay_out(repo, executor="legacy", with_metas=False)
            meta = {"invocation": {"generate_executor": "legacy"}}
            out = collect_pure_leaf_ab_summary(repo, self.ORCH, meta)
        self.assertFalse(out["available"])
        self.assertEqual(out["generate_executor"], "legacy")
        self.assertEqual(out["pure_nodes"], [])

    def test_audit_includes_pure_leaf_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._lay_out(repo)
            result = audit(repo, self.ORCH)
            self.assertIn("pure_leaf_ab_summary", result)
            self.assertTrue(result["pure_leaf_ab_summary"]["available"])
            md = _render_markdown(result)
        self.assertIn("Pure-leaf A/B metrics", md)
        self.assertIn("generate-executor: `pure`", md)
        self.assertIn("claude --version", md)
        self.assertIn(self.SRC, md)

    # -- the Z1 compile half (issue #168) ------------------------------------------
    IR_A = f"workspace/ir/{SAFE}/demo_20260907_001"
    IR_B = f"workspace/ir/{SAFE}/demo_20260907_002"

    def _launch(self, repo: Path, arid: str, *, step: str, ir_ref: str,
                leaf_mode: str | None = "pure") -> None:
        """One persisted launch request — the discovery key for the compile half.

        Deliberately NOT the reservation the generate half uses: `reserved_ir_id` is overwritten
        on every rotation, so it names only the LIVE directory. A launch request is written once
        per attempt and never rewritten, which is why a rotated attempt is discoverable at all.
        """
        d = repo / "workspace" / "orchestrations" / self.ORCH / "launches"
        d.mkdir(parents=True, exist_ok=True)
        row = {"agent_run_id": arid, "step": step, "substep": "generate", "ir_ref": ir_ref}
        if leaf_mode is not None:
            row["leaf_mode"] = leaf_mode
        (d / f"{arid}.request.json").write_text(json.dumps(row), encoding="utf-8")

    def _compile_metas(self, repo: Path, ir_ref: str, *, result: str = "pass") -> None:
        d = repo / ir_ref
        d.mkdir(parents=True, exist_ok=True)
        (d / "compile_generate_meta.json").write_text(json.dumps({
            "result": result, "failure_category": None, "attempts": 1,
            "prompt_contract_version": "pure-32",
            "per_attempt": [{"agent_run_id": "c1", "model": "claude-opus-5",
                             "usage": {"input_tokens": 700, "output_tokens": 300}}],
        }), encoding="utf-8")
        (d / "compile_verify_meta.json").write_text(json.dumps({
            "result": "pass", "failure_category": None, "attempts": 1,
            "prompt_contract_version": "pure-32",
            "per_attempt": [{"agent_run_id": "c2", "model": "claude-sonnet-5",
                             "usage": {"input_tokens": 200, "output_tokens": 20}}],
        }), encoding="utf-8")

    def test_the_compile_half_is_discovered_from_the_launch_requests(self) -> None:
        """Every ir_ref a PURE compile leaf was launched into, including a rotated attempt the
        live reservation no longer names — which is the undercount this discovery exists to
        avoid, in the one place the A/B numbers are the point."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._lay_out(repo, with_metas=False)
            self._launch(repo, "a1", step="compile", ir_ref=self.IR_A)
            self._launch(repo, "a2", step="compile", ir_ref=self.IR_B)
            self._compile_metas(repo, self.IR_A, result="fail")
            self._compile_metas(repo, self.IR_B)
            meta = {"invocation": {"generate_executor": "pure"}}
            out = collect_pure_leaf_ab_summary(repo, self.ORCH, meta)
        self.assertTrue(out["available"])
        self.assertEqual([n["ir_ref"] for n in out["pure_compile_nodes"]],
                         [self.IR_A, self.IR_B])
        self.assertEqual(out["pure_compile_nodes"][0]["generate"]["result"], "fail")
        self.assertEqual(
            out["pure_compile_nodes"][1]["generate"]["usage_total"]["total_tokens"], 1000)
        self.assertEqual(out["pure_compile_nodes"][1]["verify"]["result"], "pass")

    def test_discovery_ignores_a_launch_that_is_not_a_pure_compile_leaf(self) -> None:
        """Three rejections, one row each, because a single combined fixture would let two of
        them stop working unnoticed: another step, an agentic compile launch (no `leaf_mode`),
        and an `ir_ref` that is not a repo-relative workspace path.

        EACH FIXTURE PUTS THE METAS WHERE THE ROW WOULD FIND THEM if the rejection did not fire
        — at `IR_A` for the first two, and at the traversed path for the third. Round 1's first
        version wrote them only at `IR_A`, so the traversal row was green with the guard deleted:
        the escaping path held no metas and `found=False` produced the same empty list. A
        rejection row that would pass with the rejection removed asserts nothing.
        """
        # The escaping ref traverses back INTO the fixture's own tempdir rather than out of it.
        # `../outside/ir` resolves to a sibling of the `TemporaryDirectory`, i.e. a fixed path in
        # the system temp dir: it is never cleaned up, another user's copy of it turns this row
        # into a `PermissionError` instead of a rejection, and the fixture self-test below is
        # then satisfiable by a LEFTOVER from a previous run rather than by this run's write.
        escaping = f"workspace/ir/{self.SAFE}/../../../workspace/ir/{self.SAFE}/traversed"
        landing = f"workspace/ir/{self.SAFE}/traversed"
        for label, kwargs, meta_at in (
            ("another step", {"step": "generate", "ir_ref": self.IR_A}, self.IR_A),
            ("agentic compile",
             {"step": "compile", "ir_ref": self.IR_A, "leaf_mode": None}, self.IR_A),
            ("escaping ir_ref", {"step": "compile", "ir_ref": escaping}, landing),
        ):
            with self.subTest(rejected=label), tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp)
                self._lay_out(repo, with_metas=False)
                self._launch(repo, "a1", **kwargs)
                self._compile_metas(repo, meta_at)
                # Self-test of the fixture, twice over: the metas the row must NOT pick up are
                # readable where the discovery would land, so the empty result below is the
                # rejection and not an absence — and the write stayed INSIDE the tempdir, so it
                # is this run's own and not a leftover.
                landed = repo / meta_at / "compile_generate_meta.json"
                self.assertTrue(landed.is_file(), label)
                self.assertTrue(landed.resolve().is_relative_to(repo.resolve()), label)
                out = collect_pure_leaf_ab_summary(
                    repo, self.ORCH, {"invocation": {"generate_executor": "pure"}})
                self.assertEqual(out["pure_compile_nodes"], [], label)

    def test_attribution_follows_the_leaves_that_ran_not_the_ones_configured(self) -> None:
        """A run stopped at `Compile` launches no `generate` leaf, so a configured generate
        provider must not reach the attribution: naming it labels a compile-only measurement
        with a provider that never executed, and — because it differs from the probed one —
        suppresses the CLI version of the provider that DID run. Both are false provenance in
        the one instrument a billed A/B is read from.

        Both directions in one fixture, because only the pair distinguishes the rule from
        "attribute nothing": the compile leaves ran on the probed backend (so the version is
        reported), and the configured generate leaves are on another (so a rule reading the
        configured map would report `claude/codex` and blank the version).
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._lay_out(repo, with_metas=False)
            root = repo / "workspace" / "orchestrations" / self.ORCH
            meta = {"invocation": {"generate_executor": "pure", "llm_leaf_map": {
                "compile.generate": {"backend": "claude", "model": "opus"},
                "compile.verify": {"backend": "claude", "model": "sonnet"},
                "generate.generate": {"backend": "codex", "model": "gpt-5.6-sol"},
                "generate.verify": {"backend": "codex", "model": "gpt-5.6-terra"},
            }}}
            (root / "orchestration_meta.json").write_text(json.dumps(meta), encoding="utf-8")
            self._launch(repo, "a1", step="compile", ir_ref=self.IR_A)
            self._compile_metas(repo, self.IR_A)
            out = collect_pure_leaf_ab_summary(repo, self.ORCH, meta)
        self.assertFalse(out["pure_leaf_provider_differs"])
        self.assertEqual(out["backend"], "claude")
        self.assertEqual(out["agent_cli_version"], "1.2.3 (Claude Code)")

    def test_attribution_still_names_a_provider_the_leaves_that_ran_are_on(self) -> None:
        """The other polarity, so the row above cannot pass by attributing nothing: when the
        leaves that RAN are on a provider the preflight did not probe, the report names it and
        drops the version rather than borrowing one."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._lay_out(repo, with_metas=False)
            root = repo / "workspace" / "orchestrations" / self.ORCH
            meta = {"invocation": {"generate_executor": "pure", "llm_leaf_map": {
                "compile.generate": {"backend": "codex", "model": "gpt-5.6-sol"},
                "compile.verify": {"backend": "codex", "model": "gpt-5.6-terra"},
                "generate.generate": {"backend": "claude", "model": "opus"},
            }}}
            (root / "orchestration_meta.json").write_text(json.dumps(meta), encoding="utf-8")
            self._launch(repo, "a1", step="compile", ir_ref=self.IR_A)
            self._compile_metas(repo, self.IR_A)
            out = collect_pure_leaf_ab_summary(repo, self.ORCH, meta)
        self.assertTrue(out["pure_leaf_provider_differs"])
        self.assertEqual(out["backend"], "codex")
        self.assertEqual(out["agent_cli_version"], "")

    def test_an_orchestration_with_no_pure_launch_keeps_the_configured_attribution(self) -> None:
        """The fallback, asserted rather than left implicit: with no launch record there is
        nothing to derive from, and reporting the configured set is the older behaviour rather
        than a new claim. This is also what keeps the generate-only fixtures above unchanged."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._lay_out(repo)
            meta = {"invocation": {"generate_executor": "pure", "llm_leaf_map": {
                "generate.generate": {"backend": "codex", "model": "gpt-5.6-sol"},
            }}}
            (repo / "workspace" / "orchestrations" / self.ORCH
             / "orchestration_meta.json").write_text(json.dumps(meta), encoding="utf-8")
            out = collect_pure_leaf_ab_summary(repo, self.ORCH, meta)
        self.assertTrue(out["pure_leaf_provider_differs"])
        self.assertEqual(out["backend"], "codex")

    def test_a_compile_only_run_is_available_and_rendered(self) -> None:
        """`available` must not be decided by the GENERATE half alone: a run stopped at Compile
        writes no source dir at all, and reporting it as "no pure-leaf node located" would hide
        the very arm the A/B compares."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._lay_out(repo, with_metas=False)
            self._launch(repo, "a1", step="compile", ir_ref=self.IR_A)
            self._compile_metas(repo, self.IR_A)
            result = audit(repo, self.ORCH)
            self.assertTrue(result["pure_leaf_ab_summary"]["available"])
            self.assertEqual(result["pure_leaf_ab_summary"]["pure_nodes"], [])
            md = _render_markdown(result)
        self.assertIn(f"### compile `{self.IR_A}`", md)
        self.assertNotIn("no pure-leaf node located", md)

    def test_the_two_phases_are_labelled_apart_in_the_render(self) -> None:
        """Both halves present. The rows of a compile node are keyed `generate` / `verify` too —
        the key names the SUBSTEP — so the heading is the only thing that says which phase a
        block belongs to."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._lay_out(repo)
            self._launch(repo, "a1", step="compile", ir_ref=self.IR_A)
            self._compile_metas(repo, self.IR_A)
            md = _render_markdown(audit(repo, self.ORCH))
        self.assertIn(f"### compile `{self.IR_A}`", md)
        self.assertIn(f"### generate `{self.SRC}`", md)

    def test_discovers_failed_and_rotated_source_dirs_with_no_checkpoint_at_all(self) -> None:
        # A terminally-failed generate is never checkpointed, and a cold restart
        # rotates to a fresh source dir. Discovery must find BOTH from the pipeline
        # reservation alone,
        # which is also the real shape of a generate-only run.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            root = repo / "workspace" / "orchestrations" / self.ORCH
            root.mkdir(parents=True, exist_ok=True)
            (root / "preflight.json").write_text(
                json.dumps({"backend": "claude", "agent_version": "9.9"}), encoding="utf-8"
            )
            self._reserve(repo)
            # Two source dirs under the pipeline: a rotated failed attempt + the retry.
            for sid, result, cat in (("src_001", "fail", "bundle_schema_violation"), ("src_002", "pass", None)):
                sdir = repo / self.PIPE / "source" / sid
                sdir.mkdir(parents=True, exist_ok=True)
                (sdir / "bundle_meta.json").write_text(
                    json.dumps(
                        {
                            "result": result,
                            "failure_category": cat,
                            "attempts": 2,
                            "prompt_contract_version": "pure-1",
                            "per_attempt": [
                                {"agent_run_id": f"{sid}-a", "model": "m", "usage": {"input_tokens": 10, "output_tokens": 1}},
                                {"agent_run_id": f"{sid}-b", "model": "m", "usage": {"input_tokens": 20, "output_tokens": 2}},
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
            out = collect_pure_leaf_ab_summary(
                repo, self.ORCH, {"invocation": {"generate_executor": "pure"}}
            )
        self.assertTrue(out["available"])
        self.assertEqual(len(out["pure_nodes"]), 2)  # failed + retry both measured
        results = {n["source_dir"].split("/")[-1]: n["generate"]["result"] for n in out["pure_nodes"]}
        self.assertEqual(results, {"src_001": "fail", "src_002": "pass"})

    def test_provenance_strings_are_stripped_not_just_validated(self) -> None:
        # _clean_str must clean, not merely validate: these render inline into
        # markdown, so surrounding whitespace would break the line.
        from tools.audit_orchestration import _clean_str

        self.assertEqual(_clean_str("  pure  "), "pure")
        self.assertEqual(_clean_str("1.2.3 (Claude Code)\n"), "1.2.3 (Claude Code)")
        self.assertIsNone(_clean_str("   "))  # whitespace-only is absent
        self.assertIsNone(_clean_str(""))
        self.assertIsNone(_clean_str(None))
        self.assertIsNone(_clean_str(["pure"]))

    def test_wrong_typed_provenance_reported_absent(self) -> None:
        out = collect_pure_leaf_ab_summary(
            Path("/nonexistent"),
            "orch_x",
            {"invocation": {"generate_executor": ["not", "a", "string"]}},
        )
        self.assertIsNone(out["generate_executor"])
        self.assertIsNone(out["agent_cli_version"])

    def test_traversal_reserved_pipeline_id_is_skipped(self) -> None:
        # `reserved_ir_id` is JSON-sourced: a non-segment value would escape the
        # pipeline root (`repo_root / "workspace/pipelines/<safe>" / "../.."`).
        from tools.audit_orchestration import _pure_source_dirs_of

        for bad in ("..", ".", "/abs", "a/b", "../evil", ""):
            with tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp)
                self._reserve(repo, pipeline_id=bad)
                dirs, refs = _pure_source_dirs_of(repo, self.ORCH)
            self.assertEqual(dirs, [], f"{bad!r} must not be globbed")
            # A rejected id must not count as "accepted" either, or the caller would
            # misreport it as a benign not-yet-generated run.
            self.assertEqual(refs, [], f"{bad!r} must not be an accepted ref")

    def test_compile_only_run_is_not_reported_as_a_discovery_failure(self) -> None:
        # A reserved pipeline whose source/ does not exist yet is the NORMAL state of a
        # run stopped at Compile (and of a --with-deps dependency node when the TARGET
        # stops at Compile — `dep_until_phase` follows the target). It must not be
        # reported as a discovery failure.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "workspace" / "orchestrations" / self.ORCH).mkdir(parents=True)
            self._reserve(repo)
            (repo / self.PIPE).mkdir(parents=True)  # pipeline exists, no source/ yet
            out = collect_pure_leaf_ab_summary(
                repo, self.ORCH, {"invocation": {"generate_executor": "pure"}}
            )
        self.assertFalse(out["available"])
        self.assertIn("Generate has not produced one", out["reason"])
        self.assertNotIn("discovery found no node", out["reason"])

    def test_generate_only_run_is_measured_without_any_checkpoint(self) -> None:
        # REGRESSION: discovery previously read completed_steps[].pipeline_ref, which
        # update_checkpoint only populates for the `validate` step — so the natural A/B
        # command (`run_workflow.py <spec> generate --generate-executor pure`) measured
        # NOTHING. This fixture writes no checkpoint at all, which is that run's shape.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._lay_out(repo)  # reservation + metas
            out = collect_pure_leaf_ab_summary(
                repo, self.ORCH, {"invocation": {"generate_executor": "pure"}}
            )
        self.assertTrue(out["available"], "a generate-only pure run must still be measured")
        self.assertEqual(len(out["pure_nodes"]), 1)
        self.assertEqual(out["pure_nodes"][0]["generate"]["result"], "pass")

    def test_codex_backend_version_is_not_labelled_claude(self) -> None:
        # REGRESSION: `preflight.json#agent_version` holds whatever backend was probed
        # (`_probe_codex_backend` runs `codex --version`). Labelling it "claude
        # --version" reported false provenance on every codex orchestration — which
        # this section still renders, since a codex node stays legacy even under
        # --generate-executor pure.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            root = repo / "workspace" / "orchestrations" / self.ORCH
            root.mkdir(parents=True)
            (root / "preflight.json").write_text(
                json.dumps({"backend": "codex", "agent_version": "codex-cli 0.9.1"}),
                encoding="utf-8",
            )
            out = collect_pure_leaf_ab_summary(
                repo, self.ORCH, {"invocation": {"generate_executor": "legacy"}}
            )
        self.assertEqual(out["backend"], "codex")
        self.assertEqual(out["agent_cli_version"], "codex-cli 0.9.1")
        lines: list[str] = []
        _render_pure_leaf_ab(out, lines)
        md = "\n".join(lines)
        self.assertIn("codex --version: `codex-cli 0.9.1`", md)
        self.assertNotIn("claude --version", md)  # the false-provenance label

    def test_unrecorded_backend_does_not_claim_a_cli_name(self) -> None:
        lines: list[str] = []
        _render_pure_leaf_ab(
            {"available": False, "generate_executor": "pure", "backend": None,
             "agent_cli_version": None, "pure_nodes": []},
            lines,
        )
        md = "\n".join(lines)
        self.assertNotIn("claude --version", md)
        self.assertNotIn("codex --version", md)
        self.assertIn("unrecorded", md)

    def test_legacy_source_dir_on_disk_is_filtered_out(self) -> None:
        # The `found` filter must exclude a source dir that EXISTS but carries no
        # pure metas (a legacy node under the same pipeline). The sibling
        # legacy test can't pin this: it creates no source dir at all, so discovery
        # returns nothing regardless of the filter — it would pass even with the
        # filter deleted.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._lay_out(repo, executor="legacy", with_metas=False)
            legacy_src = repo / self.PIPE / "source" / "src_legacy_001"
            legacy_src.mkdir(parents=True)
            (legacy_src / "src").mkdir()  # a real legacy source dir, no bundle/verdict meta
            out = collect_pure_leaf_ab_summary(
                repo, self.ORCH, {"invocation": {"generate_executor": "legacy"}}
            )
        self.assertFalse(out["available"], "a legacy source dir must not become a pure node")
        self.assertEqual(out["pure_nodes"], [])
        self.assertIn("no pure-leaf meta located", out["reason"])

    def test_render_keeps_distinct_models_in_attempt_order(self) -> None:
        # Dedup must be distinct-in-order (dict.fromkeys), not sorted(set(...)):
        # a repair loop that switched models must render in the order it used them.
        lines: list[str] = []
        _render_pure_leaf_row(
            "generate",
            {
                "found": True, "result": "pass", "attempts": 3, "repair_turns": 2,
                "failure_category": None, "prompt_contract_version": "pure-1",
                "usage_total": {"total_tokens": 1},
                "models": ["zeta", "alpha", "zeta"],
            },
            lines,
        )
        md = "\n".join(lines)
        self.assertIn("model(s): zeta, alpha", md)  # first-seen order, not alphabetical
        self.assertNotIn("alpha, zeta", md)

    def test_no_reservation_reports_discovery_reason(self) -> None:
        # No pipeline reservation at all (prepare_node never ran): the one case where
        # discovery genuinely could not proceed. Must be named, not rendered as an
        # indistinguishable legacy-looking zero.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "workspace" / "orchestrations" / self.ORCH).mkdir(parents=True)
            out = collect_pure_leaf_ab_summary(
                repo, self.ORCH, {"invocation": {"generate_executor": "pure"}}
            )
        self.assertFalse(out["available"])
        self.assertIn("no pipeline reservation", out["reason"])
        lines: list[str] = []
        _render_pure_leaf_ab(out, lines)
        md = "\n".join(lines)
        # Must not claim "legacy/agentic run" while the executor line says pure.
        self.assertNotIn("legacy/agentic run", md)
        self.assertIn("no pipeline reservation", md)

    def test_render_collapses_repeated_model_and_shows_contract(self) -> None:
        lines: list[str] = []
        _render_pure_leaf_ab(
            {
                "available": True,
                "generate_executor": "pure",
                "backend": "claude", "agent_cli_version": "1.0",
                "pure_nodes": [
                    {
                        "source_dir": "p/source/s1",
                        "generate": {
                            "found": True, "result": "pass", "attempts": 3, "repair_turns": 2,
                            "failure_category": None, "prompt_contract_version": "pure-1",
                            "usage_total": {"input_tokens": 1, "output_tokens": 2,
                                            "cache_read_input_tokens": 3,
                                            "cache_creation_input_tokens": 4, "total_tokens": 10},
                            "models": ["m-a", "m-a", "m-a"],
                        },
                        "verify": {"found": False},
                    }
                ],
            },
            lines,
        )
        md = "\n".join(lines)
        self.assertIn("model(s): m-a", md)
        self.assertNotIn("m-a, m-a", md)  # repeated alias collapsed
        self.assertIn("contract=`pure-1`", md)
        self.assertIn("cache_creation 4", md)  # reconciles with total 10
        self.assertIn("no verify meta recorded", md)  # not "not a pure leaf"

    def test_unrecognized_executor_is_flagged_not_read_as_legacy(self) -> None:
        # A corrupt/typo'd executor must not be silently classified as legacy: the
        # hint branches on the exact value, so an unknown one gets its own wording
        # plus a warning. The recorded value is still shown verbatim.
        lines: list[str] = []
        _render_pure_leaf_ab(
            {"available": False, "generate_executor": "purre",
             "backend": "claude", "agent_cli_version": "1.0", "pure_nodes": []},
            lines,
        )
        md = "\n".join(lines)
        self.assertIn("generate-executor: `purre`", md)  # verbatim, not corrected
        self.assertIn("unrecognized executor value", md)
        self.assertNotIn("legacy/agentic run", md)

    def test_unrecorded_executor_does_not_claim_legacy(self) -> None:
        lines: list[str] = []
        _render_pure_leaf_ab(
            {"available": False, "generate_executor": None,
             "backend": "claude", "agent_cli_version": None, "pure_nodes": []},
            lines,
        )
        md = "\n".join(lines)
        self.assertIn("generate-executor: `unknown`", md)
        self.assertNotIn("legacy/agentic run", md)
        self.assertNotIn("unrecognized executor value", md)  # absent != invalid

    def test_render_legacy_notes_no_pure_node(self) -> None:
        lines: list[str] = []
        _render_pure_leaf_ab(
            {"available": False, "generate_executor": "legacy", "backend": "claude", "agent_cli_version": None, "pure_nodes": []},
            lines,
        )
        md = "\n".join(lines)
        self.assertIn("no pure-leaf node located", md)
        self.assertIn("unrecorded", md)


class PureLeafProvenanceUnderAMixedConfigTests(unittest.TestCase):
    """`preflight.json#backend` / `#agent_version` describe `defaults` only since issue #28, and
    this section exists to attribute PURE-LEAF metrics — whose leaves are the ones an operator
    is most likely to have moved elsewhere."""

    def test_a_pure_leaf_on_a_different_command_suppresses_the_version(self) -> None:
        """Two leaves can share a backend TOKEN and run different executables, and
        `preflight.json#agent_version` describes only the command it probed. Attributing it
        across that difference names an executable that did not produce the metrics."""
        summary = self._summary({
            "generate.generate": {"backend": "claude", "command": "", "model": "opus"},
            "generate.verify": {"backend": "claude", "command": "/opt/wrap/claude",
                                "model": "opus"},
        })
        self.assertTrue(summary["pure_leaf_provider_differs"])
        self.assertNotIn("2.1.9", self._render(summary))

    def test_the_bare_binary_spellings_are_the_same_surface(self) -> None:
        """`command: claude` and an absent command both mean "launch the bare binary".
        Normalizing only the preflight side reported a difference between two spellings of the
        same executable and suppressed a valid version."""
        for command in ("", "claude"):
            summary = self._summary({
                "generate.generate": {"backend": "claude", "command": command,
                                      "model": "opus"},
                "generate.verify": {"backend": "claude", "command": command, "model": "opus"},
            })
            self.assertFalse(summary["pure_leaf_provider_differs"], msg=repr(command))
            self.assertIn("2.1.9", self._render(summary), msg=repr(command))

    def test_the_same_command_still_reports_the_version(self) -> None:
        summary = self._summary({
            "generate.generate": {"backend": "claude", "command": "", "model": "opus"},
            "generate.verify": {"backend": "claude", "command": "", "model": "opus"},
        })
        self.assertFalse(summary["pure_leaf_provider_differs"])
        self.assertIn("2.1.9", self._render(summary))

    def _summary(self, leaf_map: dict) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = repo_root / "workspace" / "orchestrations" / "o"
            orch.mkdir(parents=True)
            (orch / "orchestration_meta.json").write_text(json.dumps({
                "orchestration_id": "o",
                "invocation": {"generate_executor": "pure", "llm_leaf_map": leaf_map},
            }), encoding="utf-8")
            (orch / "preflight.json").write_text(json.dumps({
                "backend": "claude", "agent_version": "2.1.9 (Claude Code)",
                "probe_command": "claude"}), encoding="utf-8")
            meta = json.loads((orch / "orchestration_meta.json").read_text(encoding="utf-8"))
            return ao.collect_pure_leaf_ab_summary(repo_root, "o", meta)

    def _render(self, summary: dict) -> str:
        lines: list[str] = []
        ao._render_pure_leaf_ab(summary, lines)
        return "\n".join(lines)

    def test_a_uniform_run_still_reports_the_probed_cli_version(self) -> None:
        summary = self._summary({"generate.generate": {"backend": "claude", "model": "opus"}})
        self.assertEqual(summary["backend"], "claude")
        self.assertIn("claude --version: `2.1.9 (Claude Code)`", self._render(summary))

    def test_a_legacy_record_without_a_leaf_map_is_unchanged(self) -> None:
        summary = self._summary({})
        self.assertEqual(summary["backend"], "claude")
        self.assertIn("claude --version:", self._render(summary))

    def test_pure_leaves_on_another_provider_are_named_without_a_borrowed_version(self) -> None:
        # Every LLM leaf is pure-capable since Z3 (issue #169), so the leaf that used to sit
        # OUTSIDE the attributed set here — `validate.judge` on the default backend — is now
        # inside it and would make the surface a mixture. The mixed-config subject is the one
        # this row is about, so the non-default provider is put on every attributed leaf.
        summary = self._summary({
            f"{phase}.{substep}": {"backend": "openai_compatible", "model": "local-coder"}
            for phase, substep in ao._LLM_LEAF_SUBSTEPS
        })
        rendered = self._render(summary)
        self.assertEqual(summary["backend"], "openai_compatible")
        self.assertIn("pure-leaf provider: `openai_compatible`", rendered)
        # The version line is the DEFAULT backend's and would be false provenance here.
        self.assertNotIn("2.1.9 (Claude Code)", rendered)
        self.assertNotIn("--version", rendered)

    def test_the_module_still_runs_as_a_direct_script(self) -> None:
        """`docs/CLI_REFERENCE.md` makes `python3 tools/audit_orchestration.py ...` the
        canonical way to run this. Under it `sys.path[0]` is `tools/`, so an unconditional
        `from tools.x import ...` raises before any existing shim can help.

        `--help` exits inside argparse and so never reaches the function bodies that
        import `tools.hooks.*`; the witness that DOES reach them is
        `ScriptPathDanglingLaunchWitnessTests` below (issue #130)."""
        import os
        import subprocess
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        repo = Path(__file__).resolve().parent.parent.parent
        proc = subprocess.run(
            ["python3", "tools/audit_orchestration.py", "--help"],
            cwd=repo, env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("--orchestration-id", proc.stdout)
        # `docs/CLI_REFERENCE.md` makes `--help` the canonical reference for this tool, so
        # the exit-2 contract is documented only here. One assertion per condition: when
        # this pinned two, a later commit added the third and nothing went red.
        self.assertIn("data_integrity_warning", proc.stdout)
        self.assertIn("diagnostic_failures", proc.stdout)
        self.assertIn("orchestration_found", proc.stdout)
        # The ~/.claude reconstruction flag is gone with its path (issue #179): the tool
        # reads no transcript for cost, and `--help` must not offer one.
        self.assertNotIn("--token-cost-from-transcripts", proc.stdout)
        self.assertNotIn("transcript", proc.stdout)

    def test_the_attributed_substeps_track_the_pure_capable_table(self) -> None:
        import tools.llm_config as lc
        self.assertEqual(
            ao._PURE_LEAF_MAP_KEYS,
            frozenset(f"{p}.{s}" for p, s in lc.LLM_LEAF_SUBSTEPS))
        # A leaf INSIDE that set steers the attribution — `compile.verify` is one since Z1
        # (issue #168), and this row asserted the opposite while it was outside.
        summary = self._summary({"compile.verify": {"backend": "codex", "model": "x"}})
        self.assertEqual(summary["backend"], "codex")
        # A leaf OUTSIDE it does not. `validate.judge` used to be the subject here and is
        # pure-capable since Z3 (issue #169), so the subject is now a DETERMINISTIC substep —
        # one that launches no leaf at all, and therefore can never steer the attribution.
        outside_key = "validate.execute"
        self.assertNotIn(outside_key, ao._PURE_LEAF_MAP_KEYS)
        outside = self._summary({outside_key: {"backend": "codex", "model": "x"}})
        self.assertEqual(outside["backend"], "claude")


class PureJudgeAbRollupTests(unittest.TestCase):
    """Z3 (issue #169): the judge's per-attempt record joins the A/B rollup.

    Its directory is discovered from the persisted launch requests, like the compile pair's,
    because a reservation names only the LIVE run and would drop every repaired attempt."""

    def _rollup(self, *, request: dict, meta: dict | None = None) -> tuple[dict, str]:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = repo_root / "workspace" / "orchestrations" / "o"
            (orch / "launches").mkdir(parents=True)
            (orch / "orchestration_meta.json").write_text(json.dumps({
                "orchestration_id": "o",
                "invocation": {"generate_executor": "pure", "llm_leaf_map": {}},
            }), encoding="utf-8")
            (orch / "preflight.json").write_text(json.dumps({
                "backend": "claude", "agent_version": "2.1.9", "probe_command": "claude"}),
                encoding="utf-8")
            (orch / "launches" / "c1.request.json").write_text(json.dumps(request),
                                                               encoding="utf-8")
            if meta is not None:
                run_node = (repo_root / request["pipeline_ref"] / "runs"
                            / request["run_id"] / "component__spec_x__0.1.0")
                run_node.mkdir(parents=True)
                (run_node / "judge_meta.json").write_text(json.dumps(meta), encoding="utf-8")
            orch_meta = json.loads(
                (orch / "orchestration_meta.json").read_text(encoding="utf-8"))
            summary = ao.collect_pure_leaf_ab_summary(repo_root, "o", orch_meta)
        lines: list[str] = []
        ao._render_pure_leaf_ab(summary, lines)
        return summary, "\n".join(lines)

    _REQUEST = {
        "step": "validate", "substep": "judge", "leaf_mode": "pure",
        "node_key": "component/spec_x@0.1.0",
        "pipeline_ref": "workspace/pipelines/component__spec_x__0.1.0/p_1",
        "run_id": "run_1",
    }
    _META = {"result": "pass", "attempts": 1, "prompt_contract_version": "pure-36",
             "per_attempt": [{"agent_run_id": "c1", "model": "opus",
                              "usage": {"input_tokens": 10, "output_tokens": 20}}]}

    def test_a_pure_judge_run_reports_its_judge_row(self) -> None:
        summary, rendered = self._rollup(request=self._REQUEST, meta=self._META)
        self.assertTrue(summary["available"])
        node = summary["pure_validate_nodes"][0]
        self.assertEqual(node["run_node_dir"],
                         "workspace/pipelines/component__spec_x__0.1.0/p_1/runs/run_1"
                         "/component__spec_x__0.1.0")
        self.assertTrue(node["judge"]["found"])
        self.assertIn("### validate `workspace/pipelines/", rendered)
        self.assertIn("judge", rendered)

    def test_the_safe_node_key_is_read_out_of_the_pipeline_ref(self) -> None:
        """Not recomputed from `node_key`: the tree already has two spellings of that
        transform, and a third here would be the one nothing checks. A request whose
        `pipeline_ref` is not the four-segment shape names no directory at all."""
        request = dict(self._REQUEST, pipeline_ref="workspace/pipelines/component__spec_x__0.1.0")
        summary, _ = self._rollup(request=request, meta=None)
        self.assertEqual(summary["pure_validate_nodes"], [])

    def test_an_agentic_judge_leaves_no_row(self) -> None:
        request = dict(self._REQUEST)
        del request["leaf_mode"]
        summary, _ = self._rollup(request=request, meta=self._META)
        self.assertEqual(summary["pure_validate_nodes"], [])

    def test_a_launched_judge_with_no_record_leaves_no_row(self) -> None:
        summary, _ = self._rollup(request=self._REQUEST, meta=None)
        self.assertEqual(summary["pure_validate_nodes"], [])


class ScriptPathDanglingLaunchWitnessTests(unittest.TestCase):
    """Issue #130: run as a script (`python3 tools/audit_orchestration.py`), the audit
    reported "No dangling active_child window detected" over an OPEN window, because the
    transcript lookup's `from tools.hooks.common import ...` raised `ModuleNotFoundError`
    (repo root not on `sys.path`) inside `audit()`'s best-effort `except Exception`.

    In-process the defect is invisible — pytest puts the root on the path — so the
    witness must be a subprocess with the same path shape the operator gets.
    """

    ORCH_ID = "orch_test"

    def _fixture(self, tmp: str) -> tuple[Path, Path, dict[str, str]]:
        """An open window (no child return, no agent_runs row) plus an isolated HOME.

        HOME is redirected because the transcript lookup reads `$HOME/.claude/projects`;
        the witness must not depend on — or touch — the operator's real home.
        """
        repo_root = Path(tmp) / "repo_root"
        home = Path(tmp) / "home"
        home.mkdir(parents=True)
        root = repo_root / "workspace" / "orchestrations" / self.ORCH_ID
        root.mkdir(parents=True)
        _open_dangling_window(root)
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env["HOME"] = str(home)
        return repo_root, Path(CHILD_ARID), env

    def _run(self, repo_root: Path, env: dict[str, str], *extra: str):
        repo = Path(__file__).resolve().parent.parent.parent
        return subprocess.run(
            ["python3", "tools/audit_orchestration.py",
             "--orchestration-id", self.ORCH_ID, "--repo-root", str(repo_root), *extra],
            cwd=repo, env=env, capture_output=True, text=True, check=False)

    def test_the_witness_env_really_hides_the_tools_package(self) -> None:
        """Guards the two tests below from becoming empty proofs: if a `.pth` file or an
        inherited PYTHONPATH made `tools` importable from anywhere, they would pass without
        the bootstrap. cwd is the tmp dir because `-c` puts cwd on the path, and cwd=repo
        would import `tools` for that reason alone."""
        with tempfile.TemporaryDirectory() as tmp:
            _repo_root, _arid, env = self._fixture(tmp)
            proc = subprocess.run(
                ["python3", "-c", "import tools"],
                cwd=tmp, env=env, capture_output=True, text=True, check=False)
            self.assertNotEqual(proc.returncode, 0, msg="`tools` is importable from cwd=tmp; "
                                                        "the script-path witness proves nothing")

    def test_an_open_window_is_reported_when_run_as_a_script(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root, arid, env = self._fixture(tmp)
            proc = self._run(repo_root, env, "--format", "json")
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            result = json.loads(proc.stdout)
            self.assertEqual(result["diagnostic_failures"], [])
            self.assertEqual(
                result["launch_incident"]["dangling_child"]["agent_run_id"], str(arid))

    def test_the_markdown_does_not_claim_a_clean_negative_when_run_as_a_script(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root, _arid, env = self._fixture(tmp)
            proc = self._run(repo_root, env, "--format", "markdown")
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            self.assertIn("An open active_child window was found", proc.stdout)
            self.assertNotIn("No dangling active_child window detected", proc.stdout)


class InRepoRecordSectionTests(unittest.TestCase):
    """The sections issue #179 moved out of the two workflow-audit SKILLs and into the tool:
    `phase_state_log.jsonl` fail transitions, `violations/`, `failure_analysis.json`, and the
    substeps attempted more than once. Each reads an in-repo record only."""

    ORCH_ID = "orch_inrepo"

    def _root(self, tmp: str, *, status: str | None = None) -> Path:
        root = Path(tmp) / "workspace" / "orchestrations" / self.ORCH_ID
        root.mkdir(parents=True)
        if status is not None:
            (root / "orchestration_meta.json").write_text(
                json.dumps({"orchestration_id": self.ORCH_ID, "status": status}),
                encoding="utf-8")
        return root

    def _rendered(self, tmp: str) -> tuple[dict, str]:
        result = audit(Path(tmp), self.ORCH_ID)
        return result, _render_markdown(result)

    # --- phase state failures -------------------------------------------------------

    def test_fail_and_fail_closed_transitions_are_listed_in_order(self) -> None:
        # The row shape is the corpus's: the orchestration-status writer is the only one
        # recording these states (19 rows over 48 orchestrations, all `set_status`, keys
        # ts/event/to/reason_code/reason_detail/blocking_policy_scope/detected_at — never a
        # node, step or arid).
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            _write_jsonl(root / "phase_state_log.jsonl", [
                {"ts": "2026-09-05T00:00:00Z", "event": "init_orchestration",
                 "from": None, "to": "initialized"},
                {"ts": "2026-09-05T00:01:00Z", "event": "set_status", "to": "fail",
                 "reason_code": "validate_failed", "reason_detail": "judge: fail",
                 "blocking_policy_scope": None, "detected_at": "2026-09-05T00:00:59Z"},
                {"ts": "2026-09-05T00:02:00Z", "event": "child_finished",
                 "node_key_safe": "n1", "step": "compile", "from": "launched",
                 "to": "pass", "agent_run_id": "arid-2"},
                {"ts": "2026-09-05T00:03:00Z", "event": "set_status", "to": "fail_closed",
                 "reason_code": "leaf_transport_error",
                 "reason_detail": "leaf_transport_error: leaf_exit=1"},
            ])
            result, md = self._rendered(tmp)
        failures = result["phase_state_failures"]
        self.assertEqual([f["to"] for f in failures], ["fail", "fail_closed"])
        self.assertEqual(failures[0]["reason_code"], "validate_failed")
        self.assertEqual(failures[1]["reason_code"], "leaf_transport_error")
        self.assertEqual(sorted(failures[0]),
                         ["event", "reason_code", "reason_detail", "to", "ts"])
        self.assertIn("## Phase state failures", md)
        self.assertIn("fail_closed at: `2026-09-05T00:03:00Z`", md)
        self.assertIn("[2026-09-05T00:01:00Z] set_status → `fail` — `validate_failed`: "
                      "judge: fail", md)
        self.assertIn("set_status → `fail_closed` — `leaf_transport_error`: "
                      "leaf_transport_error: leaf_exit=1", md)
        self.assertNotIn("No fail / fail_closed transition recorded", md)

    def test_a_fail_without_fail_closed_is_listed_not_denied(self) -> None:
        # The shape of every `status: fail` run in the corpus (5 of 48): a `fail` row and no
        # `fail_closed`. The section must list it, and must not print the negative sentence
        # beside it.
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            _write_jsonl(root / "phase_state_log.jsonl", [
                {"ts": "2026-09-05T00:01:00Z", "event": "set_status", "to": "fail",
                 "reason_code": "validate_failed", "reason_detail": "judge: fail"}])
            result, md = self._rendered(tmp)
        self.assertIsNone(result["fail_closed_at"])
        self.assertEqual([f["to"] for f in result["phase_state_failures"]], ["fail"])
        section = md.split("## Phase state failures")[1].split("## failure_analysis")[0]
        self.assertIn("set_status → `fail` — `validate_failed`: judge: fail", section)
        self.assertNotIn("fail_closed at:", section)
        self.assertNotIn("No fail / fail_closed transition recorded", section)

    def test_no_failure_renders_one_negative_sentence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            _write_jsonl(root / "phase_state_log.jsonl", [
                {"ts": "2026-09-05T00:00:00Z", "event": "set_status", "to": "pass"}])
            result, md = self._rendered(tmp)
        self.assertEqual(result["phase_state_failures"], [])
        self.assertIsNone(result["fail_closed_at"])
        self.assertIn("No fail / fail_closed transition recorded.", md)
        self.assertNotIn("fail_closed at:", md)

    # --- sandbox violations ---------------------------------------------------------

    def _violation(self, root: Path, arid: str, reason: str) -> None:
        vdir = root / "violations"
        vdir.mkdir(exist_ok=True)
        (vdir / f"{arid}.sandbox_enforcement_violation.json").write_text(json.dumps({
            "kind": "sandbox_enforcement_violation", "agent_run_id": arid,
            "reason": reason, "evaluated_at": f"2026-09-05T00:00:0{arid[-1]}Z"}),
            encoding="utf-8")

    def test_the_three_states_of_the_violations_directory_are_told_apart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._root(tmp)
            result, md = self._rendered(tmp)
            self.assertFalse(result["sandbox_violations"]["directory_present"])
            self.assertIn("`violations/` absent — no sandbox enforcement violation "
                          "was recorded.", md)
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            (root / "violations").mkdir()
            result, md = self._rendered(tmp)
            self.assertTrue(result["sandbox_violations"]["directory_present"])
            self.assertEqual(result["sandbox_violations"]["records"], [])
            self.assertIn("directory present, no record (pre-created by a run before "
                          "issue #171 PR-2)", md)
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            self._violation(root, "arid-1", "sandbox_profile_build_failed")
            self._violation(root, "arid-2", "sandbox_not_enforced")
            self._violation(root, "arid-3", "sandbox_not_enforced")
            result, md = self._rendered(tmp)
            sv = result["sandbox_violations"]
            self.assertEqual(sv["by_reason"], {"sandbox_profile_build_failed": 1,
                                               "sandbox_not_enforced": 2})
            self.assertEqual(sv["by_kind"], {"sandbox_enforcement_violation": 3})
            self.assertEqual([r["agent_run_id"] for r in sv["records"]],
                             ["arid-1", "arid-2", "arid-3"])
            self.assertIn("3 sandbox enforcement record(s):", md)
            self.assertIn("- `sandbox_not_enforced`: 2", md)
            self.assertIn("- `sandbox_profile_build_failed`: 1", md)
            self.assertIn("[2026-09-05T00:00:02Z] `sandbox_not_enforced` arid=`arid-2`", md)
            self.assertNotIn("absent", md.split("## Sandbox")[1].split("## Token")[0])

    def test_a_retired_kind_is_not_a_sandbox_enforcement_finding(self) -> None:
        # The real corpus holds one `*.unauthorized_write_violation.json` (a writer deleted in
        # issue #171 PR-2; `kind` differs, no `reason`, `detected_at` instead of
        # `evaluated_at`). Rendering it under the sandbox heading as reason `unknown` told
        # the operator a confinement finding that never happened.
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            (root / "violations").mkdir()
            (root / "violations" / "arid-7.unauthorized_write_violation.json").write_text(
                json.dumps({"kind": "unauthorized_write_violation", "agent_run_id": "arid-7",
                            "detected_at": "2026-07-24T04:53:10Z", "violations": []}),
                encoding="utf-8")
            result, md = self._rendered(tmp)
            sv = result["sandbox_violations"]
            self.assertEqual(sv["by_kind"], {"unauthorized_write_violation": 1})
            self.assertEqual(sv["by_reason"], {})
            self.assertEqual(sv["records"][0]["reason"], None)
            self.assertEqual(sv["records"][0]["evaluated_at"], "2026-07-24T04:53:10Z")
            section = md.split("## Sandbox")[1].split("## Token")[0]
            self.assertIn("no sandbox enforcement record (the records below are of "
                          "another kind)", section)
            self.assertIn("kind `unauthorized_write_violation` arid=`arid-7`", section)
            self.assertIn("(not a sandbox enforcement finding)", section)
            # The header does not claim the writer is gone: `noncanonical_phase_write_attempt`'s
            # exists uncalled, and the section knows only the kind.
            self.assertNotIn("no longer exists", section)
            self.assertNotIn("`unknown`", section)
            self.assertNotIn("sandbox enforcement record(s):", section)
            # ...and next to a real one, the two are listed apart.
            self._violation(root, "arid-1", "sandbox_not_enforced")
            result, md = self._rendered(tmp)
            section = md.split("## Sandbox")[1].split("## Token")[0]
            self.assertIn("1 sandbox enforcement record(s):", section)
            self.assertIn("- `sandbox_not_enforced`: 1", section)
            self.assertIn("1 record(s) of another kind", section)
            # ...apart: the other-kind arid appears only under its own heading, so the
            # sandbox list above it must not carry `arid-7` (a listing over `records`
            # instead of the sandbox subset survived the round-1 tests).
            sandbox_list = section.split("1 record(s) of another kind")[0]
            self.assertNotIn("arid-7", sandbox_list)
            self.assertIn("arid=`arid-1`", sandbox_list)
            self.assertEqual(result["sandbox_violations"]["by_reason"],
                             {"sandbox_not_enforced": 1})

    def test_an_unreadable_violation_record_is_a_diagnostic_failure(self) -> None:
        # The record a leaf's confinement wrote must not read as "nothing recorded"
        # because it failed to parse.
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            (root / "violations").mkdir()
            (root / "violations" / "x.json").write_text("{not json", encoding="utf-8")
            result, md = self._rendered(tmp)
        self.assertIsNone(result["sandbox_violations"])
        self.assertEqual([f["section"] for f in result["diagnostic_failures"]],
                         ["sandbox_violations"])
        self.assertEqual(result["diagnostic_failures"][0]["error_type"], "JSONDecodeError")
        self.assertIn("`violations/` could not be read", md)
        self.assertIn("UNKNOWN, not empty", md)
        self.assertNotIn("no sandbox enforcement violation was recorded", md)
        self.assertNotIn("directory present, no record", md)

    # --- failure_analysis -----------------------------------------------------------

    def _failure_doc(self, **overrides) -> dict:
        doc = {
            "status": "fail", "orchestration_id": self.ORCH_ID,
            "orchestration_status": "fail_closed",
            "reason_code": "leaf_transport_error",
            "reason_detail": "leaf_transport_error: leaf_exit=1",
            "failed_agent_run": {"agent_run_id": "arid-9", "node_key": "problem/x@0.1.0",
                                 "step": "generate", "substep": "gate", "status": "fail",
                                 "launch_reply_ref": "…"},
            "failed_step_results": [
                {"path": "workspace/orchestrations/o/steps/n/generate/arid-9/step_result.json",
                 "status": "fail", "failed_substeps": ["gate"]}],
            "recommended_retry_decisions": [{"repair_strategy": "restart"}],
            "launch_incident_refs": [],
        }
        doc.update(overrides)
        return doc

    def test_the_canonical_analysis_is_summarized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp, status="fail_closed")
            (root / "failure_analysis.json").write_text(json.dumps(self._failure_doc()),
                                                       encoding="utf-8")
            result, md = self._rendered(tmp)
        fa = result["failure_analysis"]
        self.assertTrue(fa["present"])
        self.assertEqual(fa["canonical"]["reason_code"], "leaf_transport_error")
        self.assertEqual(fa["canonical"]["failed_agent_run"],
                         {"agent_run_id": "arid-9", "node_key": "problem/x@0.1.0",
                          "step": "generate", "substep": "gate", "status": "fail"})
        self.assertEqual(fa["canonical"]["recommended_retry_decision_count"], 1)
        self.assertEqual(fa["sidecars"], [])
        self.assertIn("## failure_analysis", md)
        self.assertIn("`failure_analysis.json` (`orchestration_meta.json#status` = "
                      "`fail_closed`):", md)
        self.assertIn("reason `leaf_transport_error`: leaf_transport_error: leaf_exit=1", md)
        self.assertIn("failed agent run: `arid-9` — `problem/x@0.1.0` generate.gate "
                      "(status `fail`)", md)
        self.assertIn("- failed step results: 1", md)
        self.assertIn("steps/n/generate/arid-9/step_result.json` (status `fail`)", md)
        self.assertIn("- recommended retry decisions: 1", md)

    def test_sidecars_are_summarized_with_their_existing_file_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp, status="fail_closed")
            (root / "failure_analysis.json").write_text(json.dumps(self._failure_doc()),
                                                       encoding="utf-8")
            (root / "failure_analysis.runtime.abc123def456.json").write_text(json.dumps(
                self._failure_doc(reason_code="leaf_timeout", existing_file_status="invalid")),
                encoding="utf-8")
            (root / "failure_analysis.fallback.0123456789ab.json").write_text(json.dumps(
                self._failure_doc(reason_code="emergency")), encoding="utf-8")
            result, md = self._rendered(tmp)
        sidecars = result["failure_analysis"]["sidecars"]
        self.assertEqual([(sc["file"], sc["existing_file_status"], sc["reason_code"])
                          for sc in sidecars],
                         [("failure_analysis.fallback.0123456789ab.json", None, "emergency"),
                          ("failure_analysis.runtime.abc123def456.json", "invalid",
                           "leaf_timeout")])
        self.assertIn("sidecar `failure_analysis.runtime.abc123def456.json` "
                      "(existing_file_status `invalid`):", md)
        self.assertIn("reason `leaf_timeout`", md)
        self.assertIn("sidecar `failure_analysis.fallback.0123456789ab.json`", md)

    def test_a_null_failed_agent_run_and_incident_refs_render(self) -> None:
        # `failed_run = failed_runs[-1] if failed_runs else None` (tools/run_workflow.py):
        # a fail with no failed row in agent_runs.jsonl writes `null`, and a dangling launch
        # writes a snapshot ref. Neither shape is in the corpus's 10 files; both are real.
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp, status="fail")
            (root / "failure_analysis.json").write_text(json.dumps(self._failure_doc(
                failed_agent_run=None, failed_step_results=[],
                launch_incident_refs=["workspace/orchestrations/o/launch_incident.runtime.ab.json"])),
                encoding="utf-8")
            result, md = self._rendered(tmp)
        fa = result["failure_analysis"]["canonical"]
        self.assertIsNone(fa["failed_agent_run"])
        self.assertEqual(fa["launch_incident_refs"],
                         ["workspace/orchestrations/o/launch_incident.runtime.ab.json"])
        self.assertIn("- failed agent run: none recorded", md)
        self.assertIn("- launch incident: `workspace/orchestrations/o/"
                      "launch_incident.runtime.ab.json`", md)
        self.assertNotIn("failed step results", md)

    def test_an_absent_analysis_is_rendered_next_to_the_terminal_status(self) -> None:
        # Absent on a passed run is normal; absent on a failed run is a finding. The
        # renderer does not decide which — it puts the status where the reader can.
        for status in ("pass", "fail"):
            with tempfile.TemporaryDirectory() as tmp:
                self._root(tmp, status=status)
                result, md = self._rendered(tmp)
            self.assertFalse(result["failure_analysis"]["present"])
            self.assertEqual(result["orchestration_status"], status)
            self.assertIn(f"`failure_analysis.json` absent (`orchestration_meta.json#status` "
                          f"= `{status}`).", md)

    def test_a_corrupt_analysis_is_a_diagnostic_failure_not_an_absence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp, status="fail_closed")
            (root / "failure_analysis.json").write_text("{not json", encoding="utf-8")
            result, md = self._rendered(tmp)
        self.assertIsNone(result["failure_analysis"])
        self.assertEqual([f["section"] for f in result["diagnostic_failures"]],
                         ["failure_analysis"])
        self.assertIn("failure_analysis could not be read", md)
        self.assertIn("UNKNOWN, not absent", md)
        self.assertNotIn("`failure_analysis.json` absent", md)

    def test_a_non_object_analysis_or_corrupt_sidecar_is_a_diagnostic_failure(self) -> None:
        # The docstring's two RAISE claims, each with its own witness: a canonical file that
        # is JSON but not an object, and a sidecar that is not JSON (the canonical one intact).
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp, status="fail_closed")
            (root / "failure_analysis.json").write_text("[]", encoding="utf-8")
            result, _md = self._rendered(tmp)
        self.assertEqual([(f["section"], f["error_type"]) for f in result["diagnostic_failures"]],
                         [("failure_analysis", "TypeError")])
        for sidecar_text, error_type in (("{not json", "JSONDecodeError"), ("[]", "TypeError")):
            with tempfile.TemporaryDirectory() as tmp:
                root = self._root(tmp, status="fail_closed")
                (root / "failure_analysis.json").write_text(json.dumps(self._failure_doc()),
                                                           encoding="utf-8")
                (root / "failure_analysis.runtime.abc123def456.json").write_text(
                    sidecar_text, encoding="utf-8")
                result, md = self._rendered(tmp)
            self.assertIsNone(result["failure_analysis"])
            self.assertEqual(
                [(f["section"], f["error_type"]) for f in result["diagnostic_failures"]],
                [("failure_analysis", error_type)])
            self.assertIn("UNKNOWN, not absent", md)

    # --- repeated substeps ----------------------------------------------------------

    def test_a_substep_with_two_rows_is_listed_and_a_single_row_is_not(self) -> None:
        runs = [
            {"agent_run_id": "o", "agent_role": "orchestration", "status": "pass",
             "finished_at": "x"},
            {"agent_run_id": "a1", "node_key": "problem/x@0.1.0", "step": "compile",
             "substep": "verify", "status": "fail", "finished_at": "x"},
            {"agent_run_id": "a2", "node_key": "problem/x@0.1.0", "step": "compile",
             "substep": "verify", "status": "pass", "finished_at": "x"},
            {"agent_run_id": "b1", "node_key": "problem/x@0.1.0", "step": "compile",
             "substep": "generate", "status": "pass", "finished_at": "x"},
        ]
        invalid = [{"agent_run_id": "a3", "node_key": "problem/x@0.1.0", "step": "compile",
                    "substep": "verify", "status": "fail"}]
        summary = collect_agent_run_summary(runs, invalid)
        self.assertEqual(summary["repeated_substeps"], [
            {"node_key": "problem/x@0.1.0", "step": "compile", "substep": "verify",
             "attempts": 3, "statuses": ["fail", "pass", "fail"],
             "agent_run_ids": ["a1", "a2", "a3"]}])
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            _write_jsonl(root / "agent_runs.jsonl", runs)
            _write_jsonl(root / "agent_runs_invalid.jsonl", invalid)
            _result, md = self._rendered(tmp)
        self.assertIn("Repeated substeps (more than one attempt):", md)
        # One line per attempt, status beside the arid: the arid is what Step 4's "what
        # changed between them" is written from (`launches/<arid>.reply.txt`).
        self.assertIn("- `problem/x@0.1.0` compile.verify: 3 attempts\n"
                      "  - `fail` `a1`\n  - `pass` `a2`\n  - `fail` `a3`", md)
        self.assertNotIn("compile.generate", md)

    def test_attempts_are_ordered_by_start_across_the_two_files(self) -> None:
        # A rejected first attempt lives in agent_runs_invalid.jsonl, the retry that recovered
        # in agent_runs.jsonl. Concatenating the files rendered `pass, fail` for that run —
        # the retry read as the failure (Codex, round 2). Both writers stamp `started_at`.
        runs = [{"agent_run_id": "a2", "node_key": "n", "step": "compile", "substep": "verify",
                 "status": "pass", "started_at": "2026-09-05T00:02:00Z", "finished_at": "x"}]
        invalid = [{"agent_run_id": "a1", "node_key": "n", "step": "compile",
                    "substep": "verify", "status": "fail",
                    "started_at": "2026-09-05T00:01:00Z"}]
        summary = collect_agent_run_summary(runs, invalid)
        self.assertEqual(summary["repeated_substeps"][0]["statuses"], ["fail", "pass"])
        self.assertEqual(summary["repeated_substeps"][0]["agent_run_ids"], ["a1", "a2"])
        # A row with no parseable `started_at` keeps its file position, after the dated rows.
        undated = [{"agent_run_id": "a0", "node_key": "n", "step": "compile",
                    "substep": "verify", "status": "fail", "started_at": None}]
        summary = collect_agent_run_summary(undated + runs, invalid)
        self.assertEqual(summary["repeated_substeps"][0]["statuses"], ["fail", "pass", "fail"])

    def test_a_step_row_repeats_without_a_substep_and_two_conductor_rows_do_not(self) -> None:
        # Corpus shapes: `Build` is `agent_role: step`, `substep: None` (31 of 48 runs carry
        # one), and the conductor's own row has neither node nor step (exactly one per run
        # in the corpus, a resume rewriting it in place; two are used here so a dropped
        # guard is visible). The first is an attempt and is keyed with `substep=None`; the
        # second is not an attempt at anything and must not render as
        # `None None.None: 2 attempts`. A single retry — 2 attempts, the commonest repeat
        # in the corpus — is listed.
        runs = [
            {"agent_run_id": "o1", "agent_role": "orchestration", "status": "fail",
             "finished_at": "x"},
            {"agent_run_id": "o2", "agent_role": "orchestration", "status": "pass",
             "finished_at": "x"},
            {"agent_run_id": "b1", "agent_role": "step", "node_key": "problem/x@0.1.0",
             "step": "build", "substep": None, "status": "fail", "finished_at": "x"},
            {"agent_run_id": "b2", "agent_role": "step", "node_key": "problem/x@0.1.0",
             "step": "build", "substep": None, "status": "pass", "finished_at": "x"},
        ]
        summary = collect_agent_run_summary(runs)
        self.assertEqual(summary["repeated_substeps"], [
            {"node_key": "problem/x@0.1.0", "step": "build", "substep": None,
             "attempts": 2, "statuses": ["fail", "pass"], "agent_run_ids": ["b1", "b2"]}])
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            _write_jsonl(root / "agent_runs.jsonl", runs)
            _result, md = self._rendered(tmp)
        self.assertIn("- `problem/x@0.1.0` build: 2 attempts\n  - `fail` `b1`\n  - `pass` `b2`",
                      md)
        self.assertNotIn("None", md.split("Repeated substeps")[1])

    def test_no_repeat_renders_no_heading(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            _write_jsonl(root / "agent_runs.jsonl", [
                {"agent_run_id": "a1", "node_key": "n", "step": "compile",
                 "substep": "verify", "status": "pass", "finished_at": "x"}])
            result, md = self._rendered(tmp)
        self.assertEqual(result["agent_run_summary"]["repeated_substeps"], [])
        self.assertNotIn("Repeated substeps", md)

    def test_the_section_order_puts_the_failure_records_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._root(tmp, status="pass")
            _result, md = self._rendered(tmp)
        headings = [ln for ln in md.splitlines() if ln.startswith("## ")]
        self.assertEqual(headings, [
            "## Phase state failures", "## failure_analysis",
            "## Dangling launch (active_child window)",
            "## Sandbox enforcement violations", "## Token cost (per leaf)",
            "## Pure-leaf A/B metrics", "## agent_runs summary"])


class DiagnosticFailureRecordingTests(unittest.TestCase):
    """The import bootstrap closes the known cause; this closes the CLASS. A best-effort
    section that raises must not leave the audit printing a sentence that reads as a
    measurement ("No dangling ... detected"), and the operator must be able to see that
    something failed (issue #130)."""

    ORCH_ID = "orch_test"

    def _audit(self, tmp: str, target: str, **kwargs) -> dict:
        repo_root = Path(tmp)
        (repo_root / "workspace" / "orchestrations" / self.ORCH_ID).mkdir(parents=True)
        with mock.patch.object(ao, target, side_effect=RuntimeError("boom")):
            return ao.audit(repo_root, self.ORCH_ID, **kwargs)

    def test_a_failed_launch_detection_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = self._audit(tmp, "build_launch_incident")
            self.assertEqual(result["diagnostic_failures"], [
                {"section": "launch_incident", "error_type": "RuntimeError", "error": "boom"}])
            self.assertIsNone(result["launch_incident"])

    def test_a_failed_launch_detection_renders_unknown_not_a_clean_negative(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rendered = ao._render_markdown(self._audit(tmp, "build_launch_incident"))
            self.assertIn("Dangling-launch detection FAILED", rendered)
            self.assertIn("UNKNOWN", rendered)
            self.assertNotIn("No dangling active_child window detected", rendered)
            self.assertIn("## ⚠ diagnostic failures", rendered)
            self.assertIn("`launch_incident` — `RuntimeError: boom`", rendered)

    def test_a_snapshot_still_renders_when_live_detection_failed(self) -> None:
        """The snapshots are read independently of the live window, so a failed detection
        must not suppress the durable evidence that does exist."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            root = repo_root / "workspace" / "orchestrations" / self.ORCH_ID
            root.mkdir(parents=True)
            (root / "launch_incident.runtime.abc.json").write_text(json.dumps(
                {"dangling_child": {"agent_run_id": "arid-x", "step": "compile"}}),
                encoding="utf-8")
            with mock.patch.object(ao, "build_launch_incident",
                                   side_effect=RuntimeError("boom")):
                result = ao.audit(repo_root, self.ORCH_ID)
            rendered = ao._render_markdown(result)
            self.assertIn("Dangling-launch detection FAILED", rendered)
            self.assertIn("`arid-x`", rendered)

    def test_a_failed_token_cost_collection_is_recorded_with_its_cause(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = self._audit(tmp, "collect_token_cost_summary")
            self.assertEqual(result["diagnostic_failures"], [
                {"section": "token_cost_summary", "error_type": "RuntimeError",
                 "error": "boom"}])
            self.assertEqual(
                result["token_cost_summary"]["reason"],
                "token-cost collection failed: RuntimeError: boom")

    def test_a_failed_pure_leaf_ab_collection_is_recorded_with_its_cause(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = self._audit(tmp, "collect_pure_leaf_ab_summary")
            self.assertEqual(result["diagnostic_failures"], [
                {"section": "pure_leaf_ab_summary", "error_type": "RuntimeError",
                 "error": "boom"}])
            self.assertEqual(
                result["pure_leaf_ab_summary"]["reason"],
                "pure-leaf A/B collection failed: RuntimeError: boom")

    def test_a_clean_audit_records_no_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "workspace" / "orchestrations" / self.ORCH_ID).mkdir(parents=True)
            result = ao.audit(repo_root, self.ORCH_ID)
            self.assertEqual(result["diagnostic_failures"], [])
            self.assertNotIn("diagnostic failures", ao._render_markdown(result))

    def test_main_exits_2_when_the_logs_are_corrupt(self) -> None:
        """The oldest exit-2 clause, which predates this branch and had no behavioural
        test at all: `bc212ef` pinned the STRING in `--help`, which cannot see a change
        to `main()`. Dropping the clause left the suite green — measured."""
        result = {"orchestration_id": "o", "diagnostic_failures": [],
                  "orchestration_found": True, "data_integrity_warning": True}
        with mock.patch.object(ao, "audit", lambda *a, **k: result), \
                mock.patch.object(sys, "argv", ["audit_orchestration.py",
                                                "--orchestration-id", "o",
                                                "--format", "json"]), \
                contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaises(SystemExit) as cm:
            ao.main()
        self.assertEqual(cm.exception.code, 2)

    def test_main_exits_2_when_a_diagnostic_section_failed(self) -> None:
        """Same ground as the existing `data_integrity_warning` exit 2: a run that may have
        printed a false negative must be flaggable by CI / a script."""
        for failures, expected in (([], 0), ([{"section": "launch_incident",
                                               "error_type": "RuntimeError",
                                               "error": "boom"}], 2)):
            result = {"orchestration_id": "o", "diagnostic_failures": failures}
            with mock.patch.object(ao, "audit", lambda *a, _r=result, **k: _r), \
                    mock.patch.object(sys, "argv", ["audit_orchestration.py",
                                                    "--orchestration-id", "o",
                                                    "--format", "json"]), \
                    contextlib.redirect_stdout(io.StringIO()):
                if expected:
                    with self.assertRaises(SystemExit) as cm:
                        ao.main()
                    self.assertEqual(cm.exception.code, 2)
                else:
                    ao.main()


class MissingOrchestrationTests(unittest.TestCase):
    """Round 1: the same false negative as issue #130, reached without any exception.

    Every collector reads a missing file as empty, so an orchestration id that names no
    directory produced a full audit of zeroes ending in "No dangling active_child window
    detected" and exit 0. Two ways to get there, both from the RUNBOOK's own procedure:
    a mistyped / stale id, and running its command (which passes no `--repo-root`) from a
    cwd where the default `.` is not this checkout.
    """

    ORCH_ID = "orch_test"

    def test_a_missing_orchestration_is_not_a_clean_negative(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = ao.audit(Path(tmp), "no_such_orch")
            self.assertFalse(result["orchestration_found"])
            rendered = ao._render_markdown(result)
            self.assertIn("## ⚠ orchestration not found", rendered)
            self.assertIn("NOTHING was measured", rendered)
            self.assertIn("UNKNOWN", rendered)
            self.assertNotIn("No dangling active_child window detected", rendered)

    def test_a_real_orchestration_keeps_the_clean_negative(self) -> None:
        """The over-refusal direction: an orchestration that exists and has a closed
        window must still get its plain negative, and exit 0."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "workspace" / "orchestrations" / self.ORCH_ID).mkdir(parents=True)
            result = ao.audit(repo_root, self.ORCH_ID)
            self.assertTrue(result["orchestration_found"])
            rendered = ao._render_markdown(result)
            self.assertIn("No dangling active_child window detected", rendered)
            self.assertNotIn("orchestration not found", rendered)
            # Scoped to the sentence this test is about: asserting "UNKNOWN" is absent
            # from the WHOLE document would refuse any future legitimate use of the word
            # elsewhere in the report.
            self.assertNotIn("NOTHING was measured", rendered)

    def test_the_runbook_command_from_a_foreign_cwd_does_not_report_no_window(self) -> None:
        """Route (b), end to end: the window is OPEN and `--repo-root` defaults to a cwd
        that is not the checkout. Before this the audit printed the clean negative."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo_root"
            home = Path(tmp) / "home"
            home.mkdir(parents=True)
            elsewhere = Path(tmp) / "elsewhere"
            elsewhere.mkdir()
            root = repo_root / "workspace" / "orchestrations" / self.ORCH_ID
            root.mkdir(parents=True)
            _open_dangling_window(root)
            env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
            env["HOME"] = str(home)
            repo = Path(__file__).resolve().parent.parent.parent
            proc = subprocess.run(
                ["python3", str(repo / "tools" / "audit_orchestration.py"),
                 "--orchestration-id", self.ORCH_ID],
                cwd=elsewhere, env=env, capture_output=True, text=True, check=False)
            self.assertEqual(proc.returncode, 2, msg=proc.stdout[-2000:])
            self.assertIn("orchestration not found", proc.stdout)
            self.assertNotIn("No dangling active_child window detected", proc.stdout)

    def test_main_exits_2_for_a_missing_orchestration(self) -> None:
        with mock.patch.object(
            ao, "audit",
            lambda *a, **k: {"orchestration_id": "o", "diagnostic_failures": [],
                             "orchestration_found": False}), \
                mock.patch.object(sys, "argv", ["audit_orchestration.py",
                                                "--orchestration-id", "o",
                                                "--format", "json"]), \
                contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaises(SystemExit) as cm:
            ao.main()
        self.assertEqual(cm.exception.code, 2)

    def test_an_older_result_dict_renders_unchanged(self) -> None:
        """Both readers default to found=True, so a result dict from before this key
        existed must not grow a banner it cannot justify."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "workspace" / "orchestrations" / self.ORCH_ID).mkdir(parents=True)
            legacy = ao.audit(repo_root, self.ORCH_ID)
            del legacy["orchestration_found"]
            rendered = ao._render_markdown(legacy)
            self.assertNotIn("orchestration not found", rendered)
            self.assertIn("No dangling active_child window detected", rendered)


def _deferred_tools_imports(source: str) -> list[str]:
    """The NAMES bound by a `tools.*` import inside a function body of `source`.

    This is the shape of issue #130: an import that only runs when the function does can
    raise into a caller's `except Exception` and be reported as a measurement. At module
    level the same failure is unmissable. Both spellings count — absolute (`tools.x`) and
    relative (`from .x import`), the latter being what a developer inside the package
    writes. Driven on synthetic sources in `DeferredImportScannerTests` — on the real
    file the answer is `[]`, and an assertion against `[]` is green whether the scanner
    works or returns nothing unconditionally.

    Stated limit: it reads `import` statements only, so `importlib.import_module("tools.x")`
    is invisible. Measured over the tree, `import_module` occurs twice, neither in the
    scanned file — `tools/backends/registry.py`, the one module allowed to import
    dynamically, and a test.
    """
    found: list[str] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.ImportFrom):
                # `level > 0` is a relative import — from inside `tools/` that IS a
                # `tools` import, and it is the spelling a developer working in the
                # package reaches for. Keying on the module name alone missed it.
                if inner.level or (inner.module or "").split(".")[0] == "tools":
                    found.extend(a.name for a in inner.names)
            elif isinstance(inner, ast.Import):
                found.extend(a.name for a in inner.names if a.name.split(".")[0] == "tools")
    return found


class DeferredImportScannerTests(unittest.TestCase):
    """Self-test for `_deferred_tools_imports`, both directions: the rule it enforces
    answers "nothing" on today's tree, so the test that consumes it cannot tell a working
    scanner from one that always returns `[]`."""

    def test_it_finds_a_function_body_import(self) -> None:
        self.assertEqual(
            _deferred_tools_imports("def f():\n    from tools.hooks.common import x\n"),
            ["x"])
        self.assertEqual(
            _deferred_tools_imports("def f():\n    import tools.hooks.common\n"),
            ["tools.hooks.common"])

    def test_it_finds_one_nested_inside_a_branch(self) -> None:
        self.assertEqual(
            _deferred_tools_imports(
                "def f():\n    if x:\n        from tools.leaf_usage import K\n"),
            ["K"])

    def test_it_finds_a_relative_import(self) -> None:
        """The spelling that leaked: a developer inside `tools/` writes `from .x import`,
        and keying on the module name alone read that as not-a-`tools`-import."""
        self.assertEqual(
            _deferred_tools_imports("def f():\n    from .hooks.common import x\n"),
            ["x"])
        self.assertEqual(
            _deferred_tools_imports("def f():\n    from . import hooks\n"), ["hooks"])

    def test_it_ignores_module_level_and_foreign_imports(self) -> None:
        self.assertEqual(_deferred_tools_imports("from tools.leaf_usage import K\n"), [])
        self.assertEqual(_deferred_tools_imports("from .leaf_usage import K\n"), [])
        self.assertEqual(_deferred_tools_imports("def f():\n    import json\n"), [])
        self.assertEqual(
            _deferred_tools_imports("def f():\n    from toolsmith import x\n"), [])


class DiagnosticsFailsAtImportTimeTests(unittest.TestCase):
    """Round 1, both reviewers: reverting the module-level hoist in
    `tools/orchestration_diagnostics.py` (leaving the bootstrap) kept the whole suite
    green, so the property the hoist exists FOR had no witness.

    That property is the one that closes issue #130's class: a consumer that cannot
    import `tools` must fail at import, where no caller's `except Exception` can turn it
    into a false negative — not later, inside a function body.
    """

    def test_the_transcript_resolver_is_bound_at_import_time(self) -> None:
        """The direct pin on the hoist. The subprocess test below does NOT pin it: the
        module's sibling `from tools.leaf_usage import ...` makes the import fail loudly
        on its own, so it stays green with the hoist reverted (measured).

        The RULE is that the resolver the transcript path needs is resolved when the
        module loads, so a consumer that cannot import `tools` fails there rather than
        inside a caller's `except`. An earlier version asserted
        `_deferred_tools_imports(source) == []` over the whole module, which pins the
        RESULT instead: it refused a lazy import added elsewhere in the file for cost or
        to break a cycle, neither of which can reintroduce issue #130 once one
        unconditional module-level `tools.*` import exists. Measured — that version
        failed on a legitimate `from tools.backends.registry import ...` inside an
        unrelated helper while the real property still held.
        """
        self.assertTrue(hasattr(diag, "claude_leaf_projects_roots"))
        source = (Path(__file__).resolve().parent.parent
                  / "orchestration_diagnostics.py").read_text(encoding="utf-8")
        self.assertNotIn(
            "claude_leaf_projects_roots",
            _deferred_tools_imports(source),
            msg="the transcript resolver must not be imported inside a function body")

    def test_importing_it_without_the_repo_root_fails_immediately(self) -> None:
        repo = Path(__file__).resolve().parent.parent.parent
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        with tempfile.TemporaryDirectory() as tmp:
            # cwd outside the checkout, path exactly as a direct `tools/` script sees it.
            code = (f"import sys; sys.path[0] = {str(repo / 'tools')!r}; "
                    "import orchestration_diagnostics")
            proc = subprocess.run(
                ["python3", "-c", code],
                cwd=tmp, env=env, capture_output=True, text=True, check=False)
        self.assertNotEqual(proc.returncode, 0, msg=proc.stdout)
        # The traceback ECHOES the `<repo>/tools/orchestration_diagnostics.py` path on
        # any import failure, so `assertIn("tools", stderr)` passed for an unrelated
        # missing module — measured. Assert the message, which names the module.
        self.assertIn("No module named 'tools'", proc.stderr)

    def test_the_script_path_holds_one_module_identity(self) -> None:
        """The other half of dropping the bare-first shims: once the bootstrap puts the
        root on `sys.path`, a surviving bare import would leave `leaf_usage` and
        `tools.leaf_usage` in `sys.modules` as two objects."""
        repo = Path(__file__).resolve().parent.parent.parent
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        with tempfile.TemporaryDirectory() as tmp:
            script = str(repo / "tools" / "audit_orchestration.py")
            code = (
                "import runpy, sys, json\n"
                "sys.argv = ['audit_orchestration.py', '--help']\n"
                f"sys.path[0] = {str(repo / 'tools')!r}\n"
                "try:\n"
                f"    runpy.run_path({script!r}, run_name='__main__')\n"
                "except SystemExit:\n"
                "    pass\n"
                "print(json.dumps(sorted(k for k in sys.modules "
                "if k.split('.')[0] in ('leaf_usage', 'llm_config', "
                "'orchestration_diagnostics', 'hooks'))))\n"
            )
            proc = subprocess.run(
                ["python3", "-c", code],
                cwd=tmp, env=env, capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        bare = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(bare, [], msg=f"bare module identities alongside tools.*: {bare}")


if __name__ == "__main__":
    unittest.main()
