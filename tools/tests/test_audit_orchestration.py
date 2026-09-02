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
    collect_allow_auto_approve_stats,
    collect_fix_hint_stats,
    collect_policy_block_counts,
    collect_fail_closed_timeline,
    collect_agent_run_summary,
    collect_token_cost_summary,
    collect_pure_leaf_ab_summary,
    detect_suspicious_benign_volume,
    split_substantive_and_benign,
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


class CollectPolicyBlockCountsTests(unittest.TestCase):
    def test_counts_by_policy(self) -> None:
        blocks = [
            _make_block("read_manifest_read_guard"),
            _make_block("read_manifest_read_guard"),
            _make_block("output_manifest_write_guard"),
        ]
        result = collect_policy_block_counts(blocks)
        self.assertEqual(result["read_manifest_read_guard"], 2)
        self.assertEqual(result["output_manifest_write_guard"], 1)

    def test_empty_blocks(self) -> None:
        self.assertEqual(collect_policy_block_counts([]), {})

    def test_unknown_policy_when_no_audit_detail(self) -> None:
        blocks = [{"action": "block", "ts": "2026-05-09T00:00:00Z"}]
        result = collect_policy_block_counts(blocks)
        self.assertIn("unknown", result)

    def test_legacy_policy_id_aggregates_under_current_id(self) -> None:
        # Audit-log continuity: a historical record carrying the pre-rename id
        # (enforce_guarded_apply_patch) must count under the current id so the two
        # do not split into separate buckets in retrospective aggregation.
        blocks = [
            _make_block("enforce_guarded_apply_patch"),
            _make_block("forbid_unauthorized_file_write"),
        ]
        result = collect_policy_block_counts(blocks)
        self.assertEqual(result["forbid_unauthorized_file_write"], 2)
        self.assertNotIn("enforce_guarded_apply_patch", result)


class SplitSubstantiveAndBenignTests(unittest.TestCase):
    def test_auto_read_expected_block_is_benign(self) -> None:
        blocks = [
            _make_block("auto_read_expected_block"),
            _make_block("auto_read_expected_block"),
            _make_block("read_manifest_read_guard"),
        ]
        substantive, benign = split_substantive_and_benign(blocks)
        self.assertEqual(len(benign), 2)
        self.assertEqual(len(substantive), 1)
        self.assertEqual(
            (substantive[0].get("audit_detail") or {}).get("policy"),
            "read_manifest_read_guard",
        )

    def test_audit_separates_benign_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orch_id = "orch_separation"
            orch_root = Path(tmp) / "workspace" / "orchestrations" / orch_id
            (orch_root / "hooks").mkdir(parents=True)
            _write_jsonl(
                orch_root / "hooks" / "native_hook_events.jsonl",
                [
                    _make_block("auto_read_expected_block"),
                    _make_block("auto_read_expected_block"),
                    _make_block("read_manifest_read_guard"),
                    _make_block("output_manifest_write_guard"),
                ],
            )
            result = audit(Path(tmp), orch_id)
        self.assertEqual(result["benign_block_count"], 2)
        self.assertEqual(result["substantive_block_count"], 2)
        # Substantive policies appear in main counts
        self.assertIn("read_manifest_read_guard", result["policy_block_counts"])
        # Benign policies do NOT appear in main counts
        self.assertNotIn("auto_read_expected_block", result["policy_block_counts"])
        # They appear in the dedicated benign bucket
        self.assertEqual(
            result["benign_policy_block_counts"]["auto_read_expected_block"], 2
        )


def _usage_rec(inp: int, out: int, cr: int, cc: int) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "usage": {
                "input_tokens": inp,
                "output_tokens": out,
                "cache_read_input_tokens": cr,
                "cache_creation_input_tokens": cc,
            },
        },
    }


class TokenCostSummaryTests(unittest.TestCase):
    """The parent-vs-children token breakdown — surfaces the child subagent cost
    that agent_runs.jsonl and the host transcript otherwise hide."""

    def _setup(self, tmp: str) -> tuple[Path, str]:
        repo = Path(tmp) / "repo"
        orch_id = "orch_tokens"
        root = repo / "workspace" / "orchestrations" / orch_id
        root.mkdir(parents=True)
        parent_arid = "0e750000-0000-4000-8000-000000000000"
        child_a = "aaaa1111-1111-4111-8111-111111111111"
        child_b = "bbbb2222-2222-4222-8222-222222222222"
        host_session = "hostsess"
        (root / "orchestration_meta.json").write_text(
            json.dumps(
                {
                    "orchestration_id": orch_id,
                    "orchestration_agent_run_id": parent_arid,
                }
            ),
            encoding="utf-8",
        )
        _write_jsonl(
            root / "agent_runs.jsonl",
            [
                {"agent_run_id": parent_arid, "agent_role": "orchestration", "status": "pass"},
                {"agent_run_id": child_a, "agent_role": "substep", "status": "pass"},
                {"agent_run_id": child_b, "agent_role": "substep", "status": "pass"},
            ],
        )
        home = Path(tmp) / "home"
        slug = str(repo.resolve()).replace("/", "-")
        projects = home / ".claude" / "projects" / slug
        subagents = projects / host_session / "subagents"
        subagents.mkdir(parents=True, exist_ok=True)
        # Parent host transcript: 1 turn. Located by aggregate_parent_usage via the
        # `workspace/tmp/<parent_arid>` marker in its first user (launch) message.
        (projects / f"{host_session}.jsonl").write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "user",
                            "message": {
                                "role": "user",
                                "content": f"Start the workflow workspace/tmp/{parent_arid}",
                            },
                        }
                    ),
                    json.dumps(_usage_rec(100, 50, 2000, 0)),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        def _child(fname: str, arid: str, rec: dict) -> None:
            head = json.dumps(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": (
                            f"capabilities/{arid}.json output_manifests/{arid}.json "
                            f"parent_agent_run_id {parent_arid}"
                        ),
                    },
                }
            )
            (subagents / fname).write_text(head + "\n" + json.dumps(rec) + "\n", encoding="utf-8")

        _child("agent-a.jsonl", child_a, _usage_rec(10, 10, 1000, 0))
        _child("agent-b.jsonl", child_b, _usage_rec(5, 5, 500, 0))
        return repo, orch_id

    def test_collect_token_cost_summary_attributes_parent_and_children(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, orch_id = self._setup(tmp)
            home = Path(tmp) / "home"
            with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False):
                # Transcript reconstruction is OPT-IN since issue #47 — the workflow does not
                # read ~/.claude, so the default audit never touches it. These rows carry no
                # durable `usage`, which is what a pre-issue-#47 run looks like.
                result = audit(repo, orch_id, token_cost_from_transcripts=True)
            tcs = result["token_cost_summary"]
            self.assertTrue(tcs["available"])
            self.assertEqual(tcs["parent_total_tokens"], 100 + 50 + 2000)
            self.assertEqual(tcs["children_total_tokens"], 1020 + 510)
            self.assertEqual(tcs["node_total_tokens"], 2150 + 1530)
            # Parent arid is excluded from the child set (not an unlocatable child).
            self.assertEqual(tcs["children"]["unmatched_arids"], [])
            self.assertEqual(tcs["children"]["matched_count"], 2)
            md = _render_markdown(result)
            self.assertIn("Token cost breakdown", md)
            self.assertIn("child subagents", md)

    def test_available_and_renders_when_only_parent_locatable(self) -> None:
        # Post-cleanup audit: parent session survives, child transcripts are gone.
        # The surviving parent total must still be reported, not discarded.
        from tools.audit_orchestration import collect_token_cost_summary, _render_token_cost

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            home = Path(tmp) / "home"
            slug = str(repo.resolve()).replace("/", "-")
            base = home / ".claude" / "projects" / slug
            base.mkdir(parents=True, exist_ok=True)
            parent_arid = "88c4f71a-efb3-4c89-a706-9d41969cc12e"
            marker = f"workspace/tmp/{parent_arid}"
            (base / "orig.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps({"type": "user", "message": {"role": "user", "content": f"Start the workflow {marker}"}}),
                        json.dumps(_usage_rec(100, 50, 2000, 0)),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            meta = {"orchestration_agent_run_id": parent_arid}
            # No child agent_runs (only the parent): child attribution is
            # unavailable, but the parent total must still be reported.
            runs = [{"agent_run_id": parent_arid, "agent_role": "orchestration", "status": "pass"}]
            with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False):
                tcs = collect_token_cost_summary(repo, meta, runs, from_transcripts=True)
            self.assertEqual(tcs["children"]["matched_count"], 0)  # no children measured
            self.assertTrue(tcs["available"])  # parent rescues availability
            self.assertEqual(tcs["parent_total_tokens"], 2150)
            self.assertEqual(tcs["children_total_tokens"], 0)
            lines: list[str] = []
            _render_token_cost(tcs, lines)
            joined = "\n".join(lines)
            self.assertIn("2,150", joined)
            self.assertIn("partial", joined)
            self.assertIn("child subagents**: unavailable", joined)

    def test_prefers_persisted_usage_over_missing_transcript(self) -> None:
        # finalize_child persists each child's usage into agent_runs.jsonl; a later
        # audit must use it even when the ephemeral transcript is gone.
        from tools.audit_orchestration import collect_token_cost_summary

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            home = Path(tmp) / "home"
            slug = str(repo.resolve()).replace("/", "-")
            (home / ".claude" / "projects" / slug).mkdir(parents=True)  # dir exists, no transcripts
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
            with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False):
                tcs = collect_token_cost_summary(repo, {}, runs)
            self.assertEqual(tcs["children_total_tokens"], 1020)
            self.assertEqual(tcs["children"]["per_child"][child]["source"], "agent_runs.jsonl")
            # The {"status":"unavailable"} marker must NOT count as usage.
            runs2 = [{"agent_run_id": child, "agent_role": "substep", "status": "pass",
                      "usage": {"status": "unavailable", "reason": "x"}}]
            with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False):
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
        reported a figure — the same failure the node total avoids by saying `unavailable`."""
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

    def test_the_default_audit_never_reads_the_claude_transcripts(self) -> None:
        """The change's central policy claim, and the one a default flip would silently undo:
        with no opt-in flag the collector must not call the ~/.claude aggregator AT ALL, even
        for rows that carry no usage — those are reported as unaccounted instead."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            child = "aaaa1111-1111-4111-8111-111111111111"
            runs = [{"agent_run_id": child, "agent_role": "substep", "status": "pass"}]
            with mock.patch.object(
                    ao, "aggregate_child_usage",
                    side_effect=AssertionError("the default audit read ~/.claude")):
                tcs = collect_token_cost_summary(repo, {}, runs)
            self.assertEqual(tcs["children"]["unmatched_arids"], [child])
            # ...and the opt-in really is what reaches it.
            with mock.patch.object(ao, "aggregate_child_usage",
                                   return_value={"available": True, "per_child": {}}) as agg:
                collect_token_cost_summary(repo, {}, runs, from_transcripts=True)
            agg.assert_called_once_with(repo, [child])

    def test_the_cli_flag_reaches_the_collector(self) -> None:
        """The flag is the only way to ask for the legacy path, so an unwired flag would leave
        a run recorded before issue #47 unreadable with no way to say so."""
        seen: dict = {}

        def _audit(repo_root, orchestration_id, *, token_cost_from_transcripts=False):
            seen["from_transcripts"] = token_cost_from_transcripts
            return {}

        # `--format json` so the assertion is about the flag's wiring, not about what the
        # markdown renderer needs to be handed.
        for argv, expected in ((["--orchestration-id", "o", "--format", "json"], False),
                               (["--orchestration-id", "o", "--format", "json",
                                 "--token-cost-from-transcripts"], True)):
            with mock.patch.object(ao, "audit", _audit), \
                    mock.patch.object(sys, "argv", ["audit_orchestration.py", *argv]), \
                    contextlib.redirect_stdout(io.StringIO()):
                ao.main()
            self.assertIs(seen["from_transcripts"], expected, msg=str(argv))

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
            self.assertIn("**node total**: unavailable", joined)
            self.assertNotIn("0 tokens", joined)
            # ...and the JSON consumer is told the same thing as the renderer: something WAS
            # located — every leaf said why it has no numbers.
            self.assertNotIn("reason", tcs["children"])

    def test_unavailable_when_nothing_matched(self) -> None:
        # ~/.claude dir present but holds no transcripts for this orchestration, and
        # no persisted usage / parent: report unavailable, not a 0-token breakdown.
        from tools.audit_orchestration import collect_token_cost_summary, _render_token_cost

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            home = Path(tmp) / "home"
            slug = str(repo.resolve()).replace("/", "-")
            (home / ".claude" / "projects" / slug).mkdir(parents=True)
            runs = [{"agent_run_id": "bbbb2222-2222-4222-8222-222222222222", "agent_role": "substep", "status": "pass"}]
            with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False):
                tcs = collect_token_cost_summary(repo, {}, runs)
            self.assertFalse(tcs["available"])
            lines: list[str] = []
            _render_token_cost(tcs, lines)
            self.assertIn("unavailable", "\n".join(lines))
            self.assertNotIn("0 tokens", "\n".join(lines))

    def test_render_partial_when_only_children_locatable(self) -> None:
        # Children present, parent session not locatable (no orchestration_agent_run_id
        # in meta): the child total must still render, with the parent
        # side marked unavailable and the node total flagged partial.
        from tools.audit_orchestration import collect_token_cost_summary, _render_token_cost

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            home = Path(tmp) / "home"
            slug = str(repo.resolve()).replace("/", "-")
            subagents = home / ".claude" / "projects" / slug / "hostsess" / "subagents"
            subagents.mkdir(parents=True, exist_ok=True)
            child = "aaaa1111-1111-4111-8111-111111111111"
            head = json.dumps(
                {"type": "user", "message": {"role": "user", "content": f"capabilities/{child}.json"}}
            )
            (subagents / "agent-a.jsonl").write_text(
                head + "\n" + json.dumps(_usage_rec(10, 10, 1000, 0)) + "\n", encoding="utf-8"
            )
            meta: dict = {}  # no parent identity → parent unavailable
            runs = [{"agent_run_id": child, "agent_role": "substep", "status": "pass"}]
            with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False):
                tcs = collect_token_cost_summary(repo, meta, runs, from_transcripts=True)
            self.assertTrue(tcs["available"])
            self.assertFalse(tcs["parent"].get("found"))
            self.assertEqual(tcs["children_total_tokens"], 1020)
            lines: list[str] = []
            _render_token_cost(tcs, lines)
            joined = "\n".join(lines)
            self.assertIn("parent orchestration: unavailable", joined)
            self.assertIn("partial — parent usage unavailable", joined)
            self.assertIn("1,020", joined)

    def test_render_handles_unavailable(self) -> None:
        summary = {"available": False, "reason": "claude projects dir missing"}
        from tools.audit_orchestration import _render_token_cost

        lines: list[str] = []
        _render_token_cost(summary, lines)
        joined = "\n".join(lines)
        self.assertIn("unavailable", joined)
        self.assertIn("claude projects dir missing", joined)


class DetectSuspiciousBenignVolumeTests(unittest.TestCase):
    """Regression: explicit (post-startup) reads of allowlisted paths must NOT
    be silently aggregated into the benign bucket — operators need visibility."""

    def _make_benign(self, agent_id: str) -> dict:
        return {
            "action": "block",
            "agent_run_id": agent_id,
            "audit_detail": {"policy": "auto_read_expected_block"},
        }

    def test_below_budget_not_flagged(self) -> None:
        # Expected platform startup: at most ~6 reads
        blocks = [self._make_benign("agent_a") for _ in range(6)]
        flagged = detect_suspicious_benign_volume(blocks)
        self.assertEqual(flagged, [])

    def test_above_budget_flagged(self) -> None:
        # 50 reads of MEMORY.md from one orchestration agent → suspicious
        blocks = [self._make_benign("agent_a") for _ in range(50)]
        flagged = detect_suspicious_benign_volume(blocks)
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["agent_run_id"], "agent_a")
        self.assertEqual(flagged[0]["policy"], "auto_read_expected_block")
        self.assertEqual(flagged[0]["count"], 50)

    def test_per_agent_threshold(self) -> None:
        # Two agents, only one over budget
        blocks = [self._make_benign("agent_a") for _ in range(50)]
        blocks.extend(self._make_benign("agent_b") for _ in range(3))
        flagged = detect_suspicious_benign_volume(blocks)
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["agent_run_id"], "agent_a")

    def test_reads_agent_run_id_from_audit_detail(self) -> None:
        """Regression: hook's `auto_read_expected_block` puts agent_run_id in
        `audit_detail`, not top-level. The detector must look there so blocks
        are not aggregated under <unknown>."""
        blocks = [
            {
                "action": "block",
                # No top-level agent_run_id — must come from audit_detail
                "audit_detail": {
                    "policy": "auto_read_expected_block",
                    "agent_run_id": "agent_x",
                },
            }
            for _ in range(50)
        ]
        flagged = detect_suspicious_benign_volume(blocks)
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["agent_run_id"], "agent_x")

    def test_audit_surfaces_suspicious_volume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orch_id = "orch_susp"
            orch_root = Path(tmp) / "workspace" / "orchestrations" / orch_id
            (orch_root / "hooks").mkdir(parents=True)
            blocks = [
                {
                    "action": "block",
                    "agent_run_id": "run_orch",
                    "audit_detail": {"policy": "auto_read_expected_block"},
                }
                for _ in range(50)
            ]
            _write_jsonl(orch_root / "hooks" / "native_hook_events.jsonl", blocks)
            result = audit(Path(tmp), orch_id)
        self.assertEqual(len(result["suspicious_benign_volume"]), 1)
        self.assertEqual(result["suspicious_benign_volume"][0]["agent_run_id"], "run_orch")
        # Markdown rendering surfaces the warning
        md = _render_markdown(result)
        self.assertIn("Suspicious benign-block volume", md)


class CollectFixHintStatsTests(unittest.TestCase):
    def test_hint_present_counted(self) -> None:
        blocks = [
            _make_block("output_manifest_write_guard", fix_hint={"next_command": "do this"}),
        ]
        stats = collect_fix_hint_stats(blocks)
        self.assertEqual(stats["hint_present"].get("output_manifest_write_guard"), 1)
        self.assertNotIn("output_manifest_write_guard", stats["hint_absent"])

    def test_hint_absent_counted(self) -> None:
        blocks = [_make_block("forbid_python_inline_write")]
        stats = collect_fix_hint_stats(blocks)
        self.assertEqual(stats["hint_absent"].get("forbid_python_inline_write"), 1)

    def test_note_only_hint_counts_as_present(self) -> None:
        """A read block outside allowed_read_roots has no command that works, so
        it carries `note` instead of `next_command` — counting it as "no hint"
        reported a docs gap that does not exist."""
        blocks = [_make_block("read_manifest_read_guard", fix_hint={"note": "re-issue"})]
        stats = collect_fix_hint_stats(blocks)
        self.assertEqual(stats["hint_present"].get("read_manifest_read_guard"), 1)
        self.assertNotIn("read_manifest_read_guard", stats["hint_absent"])

    def test_repeated_search_detected_without_a_command(self) -> None:
        """Grep/Glob blocks carry `path`/`pattern`, not `command`; a search
        retried in a loop is exactly what this aggregation exists to surface."""
        block = {
            "action": "block",
            "tool_name": "Grep",
            "payload_summary": {"session_id": "sess_1", "path": "tools", "pattern": "def foo"},
            "audit_detail": {
                "policy": "read_manifest_read_guard",
                "agent_run_id": "run_1",
                "fix_hint": {"note": "re-issue"},
            },
            "ts": "2026-05-09T00:00:00Z",
        }
        stats = collect_fix_hint_stats([dict(block), dict(block), dict(block)])
        self.assertEqual(
            stats["repeated"]["read_manifest_read_guard"],
            ["run_1::Grep::tools::def foo"] * 2,
        )

    def test_repeat_key_is_scoped_to_the_agent(self) -> None:
        """Two agents blocked once each on the same target is not a retry —
        reporting it accuses an agent of ignoring a hint it never saw."""

        def block(agent_run_id: str, **summary) -> dict:
            return {
                "action": "block",
                "tool_name": "Grep",
                "payload_summary": dict(session_id=f"s-{agent_run_id}", **summary),
                "audit_detail": {
                    "policy": "read_manifest_read_guard",
                    "agent_run_id": agent_run_id,
                    "fix_hint": {"note": "re-issue"},
                },
            }

        two_agents = [block("run_a", path="tools", pattern="x"),
                      block("run_b", path="tools", pattern="x")]
        self.assertEqual(collect_fix_hint_stats(two_agents)["repeated"], {})

        one_agent_twice = [block("run_a", path="tools", pattern="x")] * 2
        self.assertEqual(
            collect_fix_hint_stats([dict(b) for b in one_agent_twice])["repeated"],
            {"read_manifest_read_guard": ["run_a::Grep::tools::x"]},
        )

    def test_repeated_command_detected(self) -> None:
        blocks = [
            _make_block("forbid_tools_direct_read", command="cat tools/foo.py"),
            _make_block("forbid_tools_direct_read", command="cat tools/foo.py"),
        ]
        stats = collect_fix_hint_stats(blocks)
        self.assertIn("forbid_tools_direct_read", stats["repeated"])
        self.assertEqual(len(stats["repeated"]["forbid_tools_direct_read"]), 1)


class CollectFailClosedTimelineTests(unittest.TestCase):
    def test_no_fail_closed(self) -> None:
        result = collect_fail_closed_timeline([], [])
        self.assertIsNone(result["fail_closed_at"])
        self.assertEqual(result["last_events"], [])

    def test_returns_last_5_events(self) -> None:
        phase_log = [{"event": "set_status", "to": "fail_closed", "ts": "2026-05-09T01:00:00Z"}]
        hook_events = [
            {"action": "block", "ts": f"2026-05-09T00:0{i}:00Z", "audit_detail": {"policy": f"p{i}"}}
            for i in range(8)
        ]
        result = collect_fail_closed_timeline(hook_events, phase_log, n=5)
        self.assertEqual(result["fail_closed_at"], "2026-05-09T01:00:00Z")
        self.assertEqual(len(result["last_events"]), 5)

    def test_events_are_ordered_before_fail_ts(self) -> None:
        phase_log = [{"to": "fail_closed", "ts": "2026-05-09T00:05:00Z"}]
        hook_events = [
            {"action": "block", "ts": "2026-05-09T00:03:00Z", "audit_detail": {}},
            {"action": "allow", "ts": "2026-05-09T00:06:00Z", "audit_detail": {}},
        ]
        result = collect_fail_closed_timeline(hook_events, phase_log, n=5)
        # Only event before or at fail_ts
        self.assertEqual(len(result["last_events"]), 1)

    def test_orders_by_parsed_timestamp_not_file_order(self) -> None:
        """Regression: hook events appended out-of-order (multiple hook
        processes) must still be sliced by parsed timestamp before the
        fail_closed cutoff. The previous file-order logic could surface the
        wrong commands as the events leading up to fail_closed."""
        phase_log = [{"to": "fail_closed", "ts": "2026-05-09T00:05:00Z"}]
        # File order is (late, early1, early2, after) but chronological order
        # before fail_closed is early1 < early2 < late.
        hook_events = [
            {"action": "block", "ts": "2026-05-09T00:04:30Z", "audit_detail": {"policy": "late"}},
            {"action": "block", "ts": "2026-05-09T00:00:30Z", "audit_detail": {"policy": "early1"}},
            {"action": "block", "ts": "2026-05-09T00:01:00Z", "audit_detail": {"policy": "early2"}},
            {"action": "allow", "ts": "2026-05-09T00:06:00Z", "audit_detail": {"policy": "after"}},
        ]
        result = collect_fail_closed_timeline(hook_events, phase_log, n=2)
        # Last 2 by time, not by file position
        policies = [e["policy"] for e in result["last_events"]]
        self.assertEqual(policies, ["early2", "late"])

    def test_unparseable_timestamps_surfaced_not_dropped(self) -> None:
        """Regression: events with malformed timestamps must NOT be silently
        dropped from `last_events`. They should appear in the timeline and
        be counted via `unparseable_timestamp_count`."""
        phase_log = [{"to": "fail_closed", "ts": "2026-05-09T00:05:00Z"}]
        hook_events = [
            {"action": "block", "ts": "2026-05-09T00:04:30Z", "audit_detail": {"policy": "p1"}},
            {"action": "block", "ts": "BAD-TIMESTAMP", "audit_detail": {"policy": "malformed"}},
        ]
        result = collect_fail_closed_timeline(hook_events, phase_log, n=5)
        self.assertEqual(result["unparseable_timestamp_count"], 1)
        policies = [e["policy"] for e in result["last_events"]]
        # Both events appear (parseable first, unparseable appended at end)
        self.assertIn("p1", policies)
        self.assertIn("malformed", policies)

    def test_unparseable_timestamps_trigger_data_integrity_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orch_id = "orch_bad_ts"
            orch_root = Path(tmp) / "workspace" / "orchestrations" / orch_id
            (orch_root / "hooks").mkdir(parents=True)
            _write_jsonl(orch_root / "phase_state_log.jsonl", [
                {"to": "fail_closed", "ts": "2026-05-09T00:05:00Z"},
            ])
            _write_jsonl(orch_root / "hooks" / "native_hook_events.jsonl", [
                {"action": "block", "ts": "BAD-TS", "audit_detail": {"policy": "x"}},
            ])
            result = audit(Path(tmp), orch_id)
        self.assertTrue(result["data_integrity_warning"])
        self.assertEqual(result["unparseable_timestamp_count"], 1)

    def test_handles_z_and_offset_timestamps(self) -> None:
        """Both `Z` (UTC) and explicit offset timestamps must parse correctly."""
        phase_log = [{"to": "fail_closed", "ts": "2026-05-09T00:05:00+00:00"}]
        hook_events = [
            {"action": "block", "ts": "2026-05-09T00:04:30Z", "audit_detail": {"policy": "p1"}},
        ]
        result = collect_fail_closed_timeline(hook_events, phase_log, n=5)
        self.assertEqual(len(result["last_events"]), 1)
        self.assertEqual(result["last_events"][0]["policy"], "p1")

    def test_picks_latest_fail_closed_when_multiple(self) -> None:
        # Regression: multiple fail_closed transitions (reopen + re-fail) should
        # use the LATEST timestamp, not the first.
        phase_log = [
            {"to": "fail_closed", "ts": "2026-05-09T00:01:00Z"},
            {"to": "running", "ts": "2026-05-09T00:02:00Z"},
            {"to": "fail_closed", "ts": "2026-05-09T00:05:00Z"},
        ]
        hook_events = [
            {"action": "block", "ts": "2026-05-09T00:00:30Z", "audit_detail": {"policy": "early"}},
            {"action": "block", "ts": "2026-05-09T00:04:30Z", "audit_detail": {"policy": "late"}},
            {"action": "allow", "ts": "2026-05-09T00:06:00Z", "audit_detail": {}},
        ]
        result = collect_fail_closed_timeline(hook_events, phase_log, n=5)
        self.assertEqual(result["fail_closed_at"], "2026-05-09T00:05:00Z")
        # Both pre-fail blocks should be included (under the latest fail_ts cutoff)
        policies = [e.get("policy") for e in result["last_events"]]
        self.assertIn("early", policies)
        self.assertIn("late", policies)


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


class CollectAllowAutoApproveStatsTests(unittest.TestCase):
    """Aggregation for visualizing `action=allow_auto_approve` events."""

    def _make_allow_auto(self, tool_name: str) -> dict:
        return {
            "action": "allow_auto_approve",
            "tool_name": tool_name,
            "audit_detail": {"policy": "output_manifest_write_allow", "tool_name": tool_name},
            "ts": "2026-05-09T00:00:00Z",
        }

    def test_empty_events_returns_zero_total(self) -> None:
        result = collect_allow_auto_approve_stats([])
        self.assertEqual(result, {"total": 0, "by_tool": {}})

    def test_ignores_non_allow_auto_approve_actions(self) -> None:
        events = [
            {"action": "allow", "tool_name": "Read"},
            {"action": "block", "tool_name": "Write"},
            self._make_allow_auto("Write"),
        ]
        result = collect_allow_auto_approve_stats(events)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["by_tool"], {"Write": 1})

    def test_aggregates_by_tool_name_and_sorts_by_count(self) -> None:
        events = [
            self._make_allow_auto("Write"),
            self._make_allow_auto("Write"),
            self._make_allow_auto("Write"),
            self._make_allow_auto("Edit"),
        ]
        result = collect_allow_auto_approve_stats(events)
        self.assertEqual(result["total"], 4)
        self.assertEqual(list(result["by_tool"].keys()), ["Write", "Edit"])
        self.assertEqual(result["by_tool"]["Write"], 3)
        self.assertEqual(result["by_tool"]["Edit"], 1)

    def test_falls_back_to_audit_detail_tool_name_when_top_level_missing(self) -> None:
        events = [
            {
                "action": "allow_auto_approve",
                "audit_detail": {"tool_name": "Write"},
            }
        ]
        result = collect_allow_auto_approve_stats(events)
        self.assertEqual(result["by_tool"], {"Write": 1})

    def test_unknown_tool_when_no_tool_name_anywhere(self) -> None:
        events = [{"action": "allow_auto_approve"}]
        result = collect_allow_auto_approve_stats(events)
        self.assertEqual(result["by_tool"], {"unknown": 1})


class AuditIntegrationTests(unittest.TestCase):
    """audit() end-to-end with a small fixture workspace."""

    def _build_fixture(self, tmp: str, orch_id: str) -> None:
        root = Path(tmp)
        orch_root = root / "workspace" / "orchestrations" / orch_id
        hooks_dir = orch_root / "hooks"
        hooks_dir.mkdir(parents=True)

        hook_events = [
            _make_block("read_manifest_read_guard", "cat tools/x.py"),
            _make_block("read_manifest_read_guard", "cat tools/y.py"),
            _make_block("read_manifest_read_guard", "cat tools/z.py"),
            _make_block("read_manifest_read_guard", "cat tools/z.py"),
            _make_block("read_manifest_read_guard", "cat tools/z.py"),
            _make_block("output_manifest_write_guard", fix_hint={"next_command": "guarded-apply-patch ..."}),
            {"action": "allow", "tool_name": "Read", "ts": "2026-05-09T00:10:00Z"},
            {
                "action": "allow_auto_approve",
                "tool_name": "Write",
                "audit_detail": {"policy": "output_manifest_write_allow", "tool_name": "Write"},
                "ts": "2026-05-09T00:06:00Z",
            },
            {
                "action": "allow_auto_approve",
                "tool_name": "Write",
                "audit_detail": {"policy": "output_manifest_write_allow", "tool_name": "Write"},
                "ts": "2026-05-09T00:07:00Z",
            },
            {
                "action": "allow_auto_approve",
                "tool_name": "Edit",
                "audit_detail": {"policy": "output_manifest_write_allow", "tool_name": "Edit"},
                "ts": "2026-05-09T00:08:00Z",
            },
        ]
        _write_jsonl(hooks_dir / "native_hook_events.jsonl", hook_events)

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
        self.assertEqual(result["total_blocks"], 6)
        self.assertEqual(result["policy_block_counts"]["read_manifest_read_guard"], 5)
        self.assertEqual(result["policy_block_counts"]["output_manifest_write_guard"], 1)
        self.assertEqual(result["fix_hint_stats"]["hint_present"]["output_manifest_write_guard"], 1)
        self.assertEqual(result["fix_hint_stats"]["hint_absent"]["read_manifest_read_guard"], 5)
        self.assertIsNotNone(result["fail_closed_timeline"]["fail_closed_at"])
        self.assertEqual(result["agent_run_summary"]["status_counts"]["pass"], 1)
        self.assertEqual(result["agent_run_summary"]["status_counts"]["fail"], 1)
        aa = result["allow_auto_approve_stats"]
        self.assertEqual(aa["total"], 3)
        self.assertEqual(aa["by_tool"], {"Write": 2, "Edit": 1})

    def test_audit_renders_markdown_without_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orch_id = "orch_test_md"
            self._build_fixture(tmp, orch_id)
            result = audit(Path(tmp), orch_id)
        md = _render_markdown(result)
        self.assertIn("REPEATED ERROR PATTERN", md)
        self.assertIn("read_manifest_read_guard", md)
        self.assertIn("fail_closed", md)
        self.assertIn("Auto-approved Write/Edit", md)
        self.assertIn("Total: 3", md)

    def test_audit_markdown_omits_auto_approve_section_when_zero(self) -> None:
        """Section is suppressed when no allow_auto_approve events fired."""
        with tempfile.TemporaryDirectory() as tmp:
            orch_id = "orch_no_auto_approve"
            orch_root = Path(tmp) / "workspace" / "orchestrations" / orch_id
            (orch_root / "hooks").mkdir(parents=True)
            _write_jsonl(
                orch_root / "hooks" / "native_hook_events.jsonl",
                [_make_block("read_manifest_read_guard")],
            )
            result = audit(Path(tmp), orch_id)
        self.assertEqual(result["allow_auto_approve_stats"]["total"], 0)
        md = _render_markdown(result)
        self.assertNotIn("Auto-approved Write/Edit", md)

    def test_audit_handles_missing_log_files_gracefully(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orch_id = "orch_empty"
            (Path(tmp) / "workspace" / "orchestrations" / orch_id).mkdir(parents=True)
            result = audit(Path(tmp), orch_id)
        self.assertEqual(result["total_blocks"], 0)
        self.assertIsNone(result["fail_closed_timeline"]["fail_closed_at"])
        self.assertEqual(result["invalid_run_count"], 0)

    def test_audit_flags_corrupted_jsonl(self) -> None:
        """Regression: malformed JSON lines must be surfaced, not silently dropped."""
        with tempfile.TemporaryDirectory() as tmp:
            orch_id = "orch_corrupt"
            orch_root = Path(tmp) / "workspace" / "orchestrations" / orch_id
            (orch_root / "hooks").mkdir(parents=True)
            (orch_root / "hooks" / "native_hook_events.jsonl").write_text(
                '{"action":"block"}\n'
                '{this is not valid json\n'
                '{"action":"allow"}\n',
                encoding="utf-8",
            )
            result = audit(Path(tmp), orch_id)
        self.assertTrue(result["data_integrity_warning"])
        self.assertEqual(result["parse_error_count"], 1)
        self.assertEqual(result["parse_errors"][0]["line_number"], 2)
        # Valid lines still parsed
        self.assertEqual(result["total_hook_events"], 2)
        self.assertEqual(result["total_blocks"], 1)

    def test_audit_clean_logs_no_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orch_id = "orch_clean"
            orch_root = Path(tmp) / "workspace" / "orchestrations" / orch_id
            (orch_root / "hooks").mkdir(parents=True)
            (orch_root / "hooks" / "native_hook_events.jsonl").write_text(
                '{"action":"block"}\n', encoding="utf-8",
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

        This — NOT `orchestration_checkpoint.json` — is what discovery reads. The
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
        self.assertIn("Pure-leaf A/B metrics (Z2)", md)
        self.assertIn("generate-executor: `pure`", md)
        self.assertIn("claude --version", md)
        self.assertIn(self.SRC, md)

    def test_discovers_failed_and_rotated_source_dirs_with_no_checkpoint_at_all(self) -> None:
        # A terminally-failed generate is never checkpointed, and a cold restart
        # rotates to a fresh source dir. Discovery must find BOTH from the pipeline
        # reservation alone — this fixture writes NO orchestration_checkpoint.json,
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
            self._lay_out(repo)  # reservation + metas, no orchestration_checkpoint.json
            ck = repo / "workspace" / "orchestrations" / self.ORCH / "orchestration_checkpoint.json"
            self.assertFalse(ck.exists(), "fixture must have no checkpoint")
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
        summary = self._summary({
            "generate.generate": {"backend": "openai_compatible", "model": "local-coder"},
            "generate.verify": {"backend": "openai_compatible", "model": "local-coder"},
            "validate.judge": {"backend": "claude", "model": "opus"},
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

    def test_the_attributed_substeps_track_the_pure_capable_table(self) -> None:
        import tools.llm_config as lc
        self.assertEqual(
            ao._PURE_LEAF_MAP_KEYS,
            frozenset(f"{p}.{s}" for p, s in lc.PURE_CAPABLE_SUBSTEPS))
        # A leaf outside that set must not steer the attribution.
        summary = self._summary({"compile.verify": {"backend": "codex", "model": "x"}})
        self.assertEqual(summary["backend"], "claude")


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

    def test_token_cost_from_transcripts_no_longer_always_fails(self) -> None:
        """Consequence 2 of the issue: the same swallowed import made the opt-in
        transcript path report `token-cost collection failed` on every script run."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root, _arid, env = self._fixture(tmp)
            proc = self._run(repo_root, env, "--format", "json",
                             "--token-cost-from-transcripts")
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            result = json.loads(proc.stdout)
            self.assertEqual(result["diagnostic_failures"], [])
            reason = str(result["token_cost_summary"].get("reason") or "")
            self.assertFalse(reason.startswith("token-cost collection failed"), msg=reason)


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
            self.assertNotIn("UNKNOWN", rendered)

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
    """Names imported from `tools.*` inside a function body of `source`.

    This is the shape of issue #130: an import that only runs when the function does can
    raise into a caller's `except Exception` and be reported as a measurement. At module
    level the same failure is unmissable. Driven on synthetic sources in
    `DeferredImportScannerTests` — on the real file the answer is `[]`, and an assertion
    against `[]` is green whether the scanner works or returns nothing unconditionally.
    """
    found: list[str] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.ImportFrom) and (inner.module or "").split(".")[0] == "tools":
                found.extend(f"{inner.module}.{a.name}" for a in inner.names)
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
            ["tools.hooks.common.x"])
        self.assertEqual(
            _deferred_tools_imports("def f():\n    import tools.hooks.common\n"),
            ["tools.hooks.common"])

    def test_it_finds_one_nested_inside_a_branch(self) -> None:
        self.assertEqual(
            _deferred_tools_imports(
                "def f():\n    if x:\n        from tools.leaf_usage import K\n"),
            ["tools.leaf_usage.K"])

    def test_it_ignores_module_level_and_foreign_imports(self) -> None:
        self.assertEqual(_deferred_tools_imports("from tools.leaf_usage import K\n"), [])
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

    def test_the_diagnostics_module_defers_no_tools_import(self) -> None:
        """The direct pin on the hoist. The subprocess test below does NOT pin it: the
        module's sibling `from tools.leaf_usage import ...` makes the import fail loudly
        on its own, so it stays green with the hoist reverted (measured)."""
        source = (Path(__file__).resolve().parent.parent
                  / "orchestration_diagnostics.py").read_text(encoding="utf-8")
        self.assertEqual(_deferred_tools_imports(source), [])
        self.assertTrue(hasattr(diag, "claude_leaf_projects_roots"))

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
        self.assertIn("ModuleNotFoundError", proc.stderr)
        self.assertIn("tools", proc.stderr)

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
