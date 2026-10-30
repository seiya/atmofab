#!/usr/bin/env python3
"""Tests for tools/orchestration_diagnostics.py (dangling-launch post-mortem)."""

from __future__ import annotations

import ast
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tools import orchestration_diagnostics as diag
from tools.leaf_contract import LEAF_PROMPT_CONTRACT_VERSION

REPO_ROOT = Path(__file__).resolve().parents[2]

ORCH_ID = "orch_test"
CHILD_ARID = "f00d83b5-bfbf-4c0b-8e78-95da6bf6ba5e"


def _orch_root(repo_root: Path) -> Path:
    root = repo_root / "workspace" / "orchestrations" / ORCH_ID
    root.mkdir(parents=True, exist_ok=True)
    return root


def _open_dangling_window(root: Path, *, with_marker: bool = True,
                          request: dict | None = None) -> None:
    """Lay out an open active_child window for CHILD_ARID.

    `request` is MERGED over the launch record's default `{"substep": "verify"}` and written to
    the same `launches/<arid>.request.json` — a record with no `leaf_mode`, which is what keeps
    a caller that passes nothing on the operator-root branch (issue #565)."""
    (root / "active_child_agent_run_id.txt").write_text(CHILD_ARID, encoding="utf-8")
    if with_marker:
        (root / "active_children").mkdir(exist_ok=True)
        (root / "active_children" / f"{CHILD_ARID}.txt").write_text(CHILD_ARID, encoding="utf-8")
    (root / "phase_state_log.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-06-16T12:36:58.834343Z",
                "event": "record_launch",
                "node_key_safe": "component__demo__0.1.0",
                "step": "compile",
                "agent_run_id": CHILD_ARID,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "launches").mkdir(exist_ok=True)
    (root / "launches" / f"{CHILD_ARID}.request.json").write_text(
        json.dumps({"substep": "verify", **(request or {})}), encoding="utf-8"
    )
    (root / "orchestration_meta.json").write_text(
        json.dumps({"orchestration_id": ORCH_ID, "status": "running"}), encoding="utf-8"
    )


LINEAGE_ID = "6c9e51ba-0d1e-4a5b-9c3f-2e7d8a1b4c6f"


_NO_PROVIDER_KEY = object()


def _workshop_request(provider: object = "claude_cli", lineage_id: object = LINEAGE_ID) -> dict:
    """The three launch-record fields `build_launch_request` sets on a workshop launch.

    `provider=_NO_PROVIDER_KEY` OMITS the key: the shape of a workshop record written before
    `026de9d4` (issue #534 step 2 PR-1), which the locator reads as a claude launch."""
    request: dict = {"leaf_mode": "workshop", "workshop_lineage_id": lineage_id}
    if provider is not _NO_PROVIDER_KEY:
        request["workshop_provider"] = provider
    return request


def _home_env(home: Path) -> dict[str, str]:
    """`HOME`, and the homes root at its default place under it.

    The homes root is set explicitly as well because under pytest conftest's autouse fixture
    has already pointed `ATMOFAB_WORKFLOW_HOMES_ROOT` at one directory per TEST, which every
    subtest of a row would share; under plain `unittest` the `HOME` half alone decides it."""
    from tools.operator_private_root import WORKFLOW_HOMES_ROOT_ENV
    return {"HOME": str(home), WORKFLOW_HOMES_ROOT_ENV: str(home / ".atmofab" / "homes")}


def _lineage_projects(orchestration_id: str = ORCH_ID, lineage_id: str = LINEAGE_ID) -> Path:
    """The lineage's `config/projects`, from `workflow_homes_root()` read NOW.

    Call it INSIDE the `HOME` patch: under plain `unittest` nothing else redirects the homes
    root, and a value read outside the patch is the operator's real `~/.atmofab/homes`."""
    from tools.operator_private_root import workflow_homes_root
    return (workflow_homes_root() / orchestration_id / "claude" / lineage_id / "config"
            / "projects")


def _write_transcript(projects: Path, arid: str = CHILD_ARID, slug: str = "-tree-slug") -> Path:
    (projects / slug).mkdir(parents=True, exist_ok=True)
    path = projects / slug / f"{arid}.jsonl"
    path.write_text(json.dumps({"type": "assistant", "timestamp": "2026-06-16T12:38:47.000Z",
                                "message": {"role": "assistant", "content": [
                                    {"type": "tool_use", "name": "Read",
                                     "input": {"file_path": "x"}}]}}) + "\n",
                    encoding="utf-8")
    return path


class DetectDanglingTests(unittest.TestCase):
    def test_open_window_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _open_dangling_window(_orch_root(repo))
            result = diag.detect_dangling_active_child(repo, ORCH_ID)
            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result["agent_run_id"], CHILD_ARID)
            self.assertEqual(result["step"], "compile")
            self.assertEqual(result["substep"], "verify")
            self.assertEqual(result["launch_recorded_at"], "2026-06-16T12:36:58.834343Z")
            self.assertIsInstance(result["elapsed_seconds"], float)

    def test_empty_active_child_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            root = _orch_root(repo)
            (root / "active_child_agent_run_id.txt").write_text("", encoding="utf-8")
            self.assertIsNone(diag.detect_dangling_active_child(repo, ORCH_ID))

    def test_a_leftover_child_return_ack_does_not_close_window(self) -> None:
        """Issue #447 (D2) retired the child-return ack: only the terminal run row (the row
        below) closes the window. An ack a past orchestration left behind is not read, so the
        launch it names is still reported dangling."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            root = _orch_root(repo)
            _open_dangling_window(root)
            (root / "child_returns").mkdir(exist_ok=True)
            (root / "child_returns" / f"{CHILD_ARID}.txt").write_text("ack", encoding="utf-8")
            result = diag.detect_dangling_active_child(repo, ORCH_ID)
            self.assertIsNotNone(result)
            self.assertEqual(result["agent_run_id"], CHILD_ARID)

    def test_terminal_agent_run_closes_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            root = _orch_root(repo)
            _open_dangling_window(root)
            (root / "agent_runs.jsonl").write_text(
                json.dumps(
                    {"agent_run_id": CHILD_ARID, "status": "pass", "finished_at": "2026-06-16T12:48:00Z"}
                )
                + "\n",
                encoding="utf-8",
            )
            self.assertIsNone(diag.detect_dangling_active_child(repo, ORCH_ID))

    def test_claude_pointer_only_window_is_detected(self) -> None:
        # record_launch writes active_child_agent_run_id.txt BEFORE active_children/
        # <arid>.txt, so a crash between the two leaves a pointer-only open window
        # that still blocks the next record-launch. It must be detected.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _open_dangling_window(_orch_root(repo), with_marker=False)
            result = diag.detect_dangling_active_child(repo, ORCH_ID)
            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result["agent_run_id"], CHILD_ARID)

    def test_codex_backend_dangling_detected_without_claude_pointer(self) -> None:
        # codex: record_launch writes active_children/<arid>.txt for ALL
        # backends but NOT active_child_agent_run_id.txt (Claude-only). Detection
        # must key off the backend-neutral marker, else these dangling launches are
        # missed and the orchestration stays `running`.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            root = _orch_root(repo)
            # No active_child_agent_run_id.txt (codex has no pointer).
            markers = root / "active_children"
            markers.mkdir(exist_ok=True)
            (markers / f"{CHILD_ARID}.txt").write_text(CHILD_ARID, encoding="utf-8")
            (root / "phase_state_log.jsonl").write_text(
                json.dumps(
                    {
                        "ts": "2026-06-16T12:36:58Z",
                        "event": "record_launch",
                        "node_key_safe": "component__demo__0.1.0",
                        "step": "compile",
                        "agent_run_id": CHILD_ARID,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            result = diag.detect_dangling_active_child(repo, ORCH_ID)
            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result["agent_run_id"], CHILD_ARID)
            self.assertEqual(result["dangling_child_arids"], [CHILD_ARID])
            self.assertFalse((root / "active_child_agent_run_id.txt").exists())

    def test_invalid_run_attempt_is_not_dangling(self) -> None:
        # A child diverted to agent_runs_invalid.jsonl reached record-agent-run (an
        # invalid terminal attempt). Even with its marker still present, it must NOT
        # be classified as a dangling (launch_incomplete) launch.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            root = _orch_root(repo)
            markers = root / "active_children"
            markers.mkdir(exist_ok=True)
            (markers / f"{CHILD_ARID}.txt").write_text(CHILD_ARID, encoding="utf-8")
            (root / "agent_runs_invalid.jsonl").write_text(
                json.dumps({"agent_run_id": CHILD_ARID, "status": "fail",
                            "fail_reason": "sandbox_enforcement_violation"})
                + "\n",
                encoding="utf-8",
            )
            self.assertIsNone(diag.detect_dangling_active_child(repo, ORCH_ID))

    def test_multiple_parallel_dangling_children_all_listed(self) -> None:
        # Parallel backends can leave several dangling markers.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            root = _orch_root(repo)
            markers = root / "active_children"
            markers.mkdir(exist_ok=True)
            for arid in ("child-a", "child-b"):
                (markers / f"{arid}.txt").write_text(arid, encoding="utf-8")
            # child-a completed (terminal run) → only child-b is dangling.
            (root / "agent_runs.jsonl").write_text(
                json.dumps({"agent_run_id": "child-a", "status": "pass", "finished_at": "x"})
                + "\n",
                encoding="utf-8",
            )
            result = diag.detect_dangling_active_child(repo, ORCH_ID)
            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result["dangling_child_arids"], ["child-b"])
            self.assertEqual(result["agent_run_id"], "child-b")


class ApiErrorFromRecordsTests(unittest.TestCase):
    def test_transient_api_error_529_surfaced(self) -> None:
        """A synthetic 529 assistant record is extracted as a structured, retryable
        api_error so the operator can tell the dangling launch was a transport blip."""
        records = [
            {
                "type": "user",
                "timestamp": "2026-06-17T01:14:13.121Z",
                "message": {"role": "user", "content": "You are a substep agent."},
            },
            {
                "type": "assistant",
                "timestamp": "2026-06-17T01:17:30.724Z",
                "isApiErrorMessage": True,
                "apiErrorStatus": 529,
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "API Error: 529 Overloaded. Temporary."}],
                },
            },
        ]
        api_error = diag.api_error_from_records(records)
        self.assertIsNotNone(api_error)
        assert api_error is not None
        self.assertEqual(api_error["status"], 529)
        self.assertTrue(api_error["retryable"])
        self.assertIn("Overloaded", api_error["message"])

    def test_recovered_api_error_cleared_by_later_activity(self) -> None:
        """A 529 that is FOLLOWED by normal activity was recovered — it must not be
        reported, else a later unrelated hang would be mislabeled safe-to-resume."""
        records = [
            {
                "type": "assistant",
                "timestamp": "2026-06-17T01:17:30.724Z",
                "isApiErrorMessage": True,
                "apiErrorStatus": 529,
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "API Error: 529 Overloaded."}],
                },
            },
            # Normal activity after the error → the error was recovered.
            {
                "type": "assistant",
                "timestamp": "2026-06-17T01:18:05.000Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}],
                },
            },
        ]
        self.assertIsNone(diag.api_error_from_records(records))

    def test_non_retryable_api_error_marked(self) -> None:
        """A 400-class API error is surfaced but flagged non-retryable."""
        records = [
            {
                "type": "assistant",
                "timestamp": "2026-06-17T01:17:30.724Z",
                "isApiErrorMessage": True,
                "apiErrorStatus": 400,
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "API Error: 400 Bad Request."}],
                },
            },
        ]
        api_error = diag.api_error_from_records(records)
        self.assertIsNotNone(api_error)
        assert api_error is not None
        self.assertEqual(api_error["status"], 400)
        self.assertFalse(api_error["retryable"])

    def test_no_api_error_when_clean_tail(self) -> None:
        records = [
            {
                "type": "assistant",
                "timestamp": "2026-06-17T01:17:30.724Z",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
            },
        ]
        self.assertIsNone(diag.api_error_from_records(records))


class TranscriptTailTests(unittest.TestCase):
    def test_dead_air_and_interrupt_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "child.jsonl"
            records = [
                {
                    "type": "assistant",
                    "timestamp": "2026-06-16T12:38:47.000Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "tool_use", "name": "Bash", "input": {"command": "python3 x.py"}}
                        ],
                    },
                },
                {
                    "type": "user",
                    "timestamp": "2026-06-16T12:38:47.421Z",
                    "message": {"role": "user", "content": [{"type": "tool_result", "content": "PASS"}]},
                },
                {
                    "type": "user",
                    "timestamp": "2026-06-16T12:48:47.526Z",
                    "message": {"role": "user", "content": [{"type": "text", "text": "[Request interrupted by user]"}]},
                },
            ]
            path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
            summary = diag.summarize_transcript_tail(path)
            self.assertTrue(summary["found"])
            self.assertEqual(summary["last_activity_ts"], "2026-06-16T12:38:47.421Z")
            self.assertTrue(summary["interrupted"])
            self.assertEqual(summary["interrupt_ts"], "2026-06-16T12:48:47.526Z")
            self.assertAlmostEqual(summary["dead_air_seconds"], 600.105, places=2)
            self.assertEqual(summary["last_tool_use"]["name"], "Bash")

    def test_missing_file_is_not_found(self) -> None:
        summary = diag.summarize_transcript_tail(Path("/nonexistent/x.jsonl"))
        self.assertFalse(summary["found"])


class BuildLaunchIncidentTests(unittest.TestCase):
    def test_incident_correlates_leaf_transcript_by_arid(self) -> None:
        # The conductor pins each leaf's Claude session id to its agent_run_id, so
        # the child transcript is addressable as ~/.claude/projects/<slug>/<arid>.jsonl
        # — no host session needed. build_launch_incident recovers last-activity /
        # dead-air / abort-marker from it.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            home = Path(tmp) / "home"
            _open_dangling_window(_orch_root(repo))
            proj = home / ".claude" / "projects" / "some-slug"
            proj.mkdir(parents=True)
            records = [
                {
                    "type": "assistant",
                    "timestamp": "2026-06-16T12:38:47.000Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "tool_use", "name": "Bash", "input": {"command": "python3 x.py"}}
                        ],
                    },
                },
                {
                    "type": "user",
                    "timestamp": "2026-06-16T12:48:47.000Z",
                    "message": {"role": "user", "content": [{"type": "text", "text": "[Request interrupted by user]"}]},
                },
            ]
            (proj / f"{CHILD_ARID}.jsonl").write_text(
                "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
            )
            with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False):
                incident = diag.build_launch_incident(repo, ORCH_ID)
            self.assertIsNotNone(incident)
            assert incident is not None
            self.assertEqual(incident["dangling_child"]["agent_run_id"], CHILD_ARID)
            child = incident["transcripts"]["child_transcript"]
            self.assertTrue(child["found"])
            self.assertEqual(child["last_tool_use"]["name"], "Bash")
            # The live route must fill the key the audit renderer prints, and say which
            # of the two projects roots answered. Before this was set the renderer put
            # `matched via `None`` on a transcript it had genuinely found.
            self.assertEqual(child["match_method"], "session_id")
            self.assertEqual(child["matched_projects_root"],
                             str(home / ".claude" / "projects"))
            self.assertTrue(child["interrupted"])
            self.assertEqual(child["interrupt_text"], "[Request interrupted by user]")
            self.assertAlmostEqual(child["dead_air_seconds"], 600.0, places=1)

    def test_incident_degrades_when_transcript_ephemeral(self) -> None:
        # ~/.claude cleaned/absent: dangling still detected from in-repo artifacts;
        # child transcript reported not-found (never raises).
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            empty_home = Path(tmp) / "home"
            empty_home.mkdir()
            _open_dangling_window(_orch_root(repo))
            with mock.patch.dict(os.environ, {"HOME": str(empty_home)}, clear=False):
                incident = diag.build_launch_incident(repo, ORCH_ID)
            self.assertIsNotNone(incident)
            assert incident is not None
            self.assertEqual(incident["dangling_child"]["agent_run_id"], CHILD_ARID)
            self.assertFalse(incident["transcripts"]["child_transcript"]["found"])

    def test_a_claude_workshop_incident_correlates_its_lineage_transcript(self) -> None:
        """Pin (d), the hit: end to end, the lineage root is stamped and the tail is read."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            home = Path(tmp) / "home"
            _open_dangling_window(_orch_root(repo), request=_workshop_request())
            with mock.patch.dict(os.environ, _home_env(home), clear=False):
                lineage = _lineage_projects()
                _write_transcript(lineage)
                incident = diag.build_launch_incident(repo, ORCH_ID)
            assert incident is not None
            child = incident["transcripts"]["child_transcript"]
            self.assertTrue(child["found"])
            self.assertEqual(child["match_method"], "session_id")
            self.assertEqual(child["matched_projects_root"], str(lineage))
            self.assertEqual(child["last_tool_use"]["name"], "Read")
            self.assertEqual(child["last_activity_ts"], "2026-06-16T12:38:47.000Z")
            self.assertNotIn("searched_projects_root", child)

    def test_a_workshop_miss_is_typed_and_names_the_place_searched(self) -> None:
        """Pin (d), the misses: one reason per case, each naming a place, and
        `searched_projects_root` set exactly when a root was globbed."""
        cases = {
            "claude_no_file": (_workshop_request(), "lineage",
                               (f"no leaf transcript for {CHILD_ARID} under its lineage's "
                               "config home {lineage} (the launch record's "
                               f"workshop_lineage_id {LINEAGE_ID}; pruned, or the CLI wrote "
                               "none before it died)")),
            "lineage_id_bad": (_workshop_request(lineage_id="../x"), None,
                               ("the launch record names no usable workshop_lineage_id "
                               "('../x'), so no lineage home was searched")),
            "lineage_id_absent": (_workshop_request(lineage_id=None), None,
                                  ("the launch record names no usable workshop_lineage_id "
                                  "(None), so no lineage home was searched")),
            "provider_null": (_workshop_request(provider=None), None,
                                ("the launch record names no usable workshop_provider (None), "
                                "so no transcript root was searched")),
            "codex": (_workshop_request(provider="codex_cli"), None,
                      ("a codex workshop leaf writes no claude transcript; its rollout is under "
                      f"{{homes}}/{ORCH_ID}/codex/{LINEAGE_ID}/config/sessions/, which "
                      "this correlation does not read")),
            "http": (_workshop_request(provider="anthropic_api"), None,
                     ("an HTTP workshop lineage has no CLI transcript; the host loop's turns "
                     f"are workspace/orchestrations/{ORCH_ID}/workshop/{LINEAGE_ID}/"
                     "loop_turns.jsonl")),
            "http_openai": (_workshop_request(provider="openai_compatible"), None,
                            ("an HTTP workshop lineage has no CLI transcript; the host loop's "
                            f"turns are workspace/orchestrations/{ORCH_ID}/workshop/"
                            f"{LINEAGE_ID}/loop_turns.jsonl")),
        }
        for name, (request, searched, reason) in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp) / "repo"
                home = Path(tmp) / "home"
                _open_dangling_window(_orch_root(repo), request=request)
                with mock.patch.dict(os.environ, _home_env(home), clear=False):
                    from tools.operator_private_root import workflow_homes_root
                    lineage = _lineage_projects()
                    homes = workflow_homes_root()
                    # A decoy in BOTH roots a wrong branch could search: a miss that is
                    # typed must not have been answered by either of them.
                    _write_transcript(home / ".claude" / "projects")
                    if name != "claude_no_file":
                        _write_transcript(lineage)
                    incident = diag.build_launch_incident(repo, ORCH_ID)
                assert incident is not None
                child = incident["transcripts"]["child_transcript"]
                self.assertFalse(child["found"])
                self.assertEqual(child["reason"], reason.replace("{lineage}", str(lineage))
                                 .replace("{homes}", str(homes)))
                self.assertEqual(child["searched_projects_root"],
                                 str(lineage) if searched == "lineage" else None)

    def test_an_unexpandable_homes_root_is_a_typed_miss_naming_the_variable(self) -> None:
        """Round 3 of issue #565. The homes-root resolver is not total (the `TODO.md` item on
        `~account` overrides); its `RuntimeError` on the lineage branch is a miss that names
        `ATMOFAB_WORKFLOW_HOMES_ROOT`, not a raise, and nothing is searched. The codex branch
        resolves the same root for its reason and falls back to the placeholder."""
        from tools.operator_private_root import WORKFLOW_HOMES_ROOT_ENV
        for provider, needle in (("claude_cli", WORKFLOW_HOMES_ROOT_ENV),
                                 ("codex_cli", "<homes-root>/")):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp) / "repo"
                home = Path(tmp) / "home"
                _open_dangling_window(_orch_root(repo),
                                      request=_workshop_request(provider=provider))
                env = {**_home_env(home), WORKFLOW_HOMES_ROOT_ENV: "~nosuchacct/x"}
                with mock.patch.dict(os.environ, env, clear=False):
                    incident = diag.build_launch_incident(repo, ORCH_ID)
                assert incident is not None
                child = incident["transcripts"]["child_transcript"]
                self.assertFalse(child["found"])
                self.assertIsNone(child["searched_projects_root"])
                self.assertIn(needle, child["reason"])

    def test_a_workshop_record_without_a_provider_key_is_a_claude_launch(self) -> None:
        """A workshop record written before `026de9d4` carries no `workshop_provider` key and
        is a claude launch by construction (round 1 of issue #565 found such records on the
        operator's machine, every transcript under its lineage home, every one a typed miss;
        the count is in the PR's record). The ABSENT key is searched in the lineage home; a key PRESENT with `null` is the
        typed miss the previous row pins, so the two shapes are driven side by side."""
        for name, provider, found in (("key_absent", _NO_PROVIDER_KEY, True),
                                      ("key_null", None, False)):
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp) / "repo"
                home = Path(tmp) / "home"
                _open_dangling_window(_orch_root(repo),
                                      request=_workshop_request(provider=provider))
                with mock.patch.dict(os.environ, _home_env(home), clear=False):
                    lineage = _lineage_projects()
                    _write_transcript(lineage)
                    _write_transcript(home / ".claude" / "projects")
                    incident = diag.build_launch_incident(repo, ORCH_ID)
                assert incident is not None
                child = incident["transcripts"]["child_transcript"]
                self.assertEqual(child["found"], found)
                if found:
                    self.assertEqual(child["matched_projects_root"], str(lineage))
                else:
                    self.assertIn("no usable workshop_provider (None)", child["reason"])

    def test_an_unreadable_launch_record_is_a_typed_miss_not_the_operator_root(self) -> None:
        """Pin (d): a record that is present but not a JSON object selects no search. The
        operator-root branch would print the pre-Z4 text for what may be a workshop launch."""
        for body, detail in (("{not json", None), ("[1, 2]", "not a JSON object")):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp) / "repo"
                home = Path(tmp) / "home"
                orch = _orch_root(repo)
                _open_dangling_window(orch)
                (orch / "launches" / f"{CHILD_ARID}.request.json").write_text(
                    body, encoding="utf-8")
                _write_transcript(home / ".claude" / "projects")
                with mock.patch.dict(os.environ, _home_env(home), clear=False):
                    incident = diag.build_launch_incident(repo, ORCH_ID)
                assert incident is not None
                child = incident["transcripts"]["child_transcript"]
                self.assertFalse(child["found"])
                self.assertIsNone(child["searched_projects_root"])
                reason = child["reason"]
                self.assertTrue(reason.startswith(
                    f"the launch record launches/{CHILD_ARID}.request.json could not be read ("),
                    reason)
                self.assertTrue(reason.endswith("), so no transcript root was searched"),
                                reason)
                if detail:
                    self.assertIn(detail, reason)

    def test_a_record_without_a_workshop_keeps_the_operator_root_miss(self) -> None:
        """No `leaf_mode` (a deterministic substep) and `"pure"` (a run before PR-J4-b) both
        take the operator root, with the text that names it."""
        for request in ({}, {"leaf_mode": "pure"}):
            with self.subTest(request=request), tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp) / "repo"
                home = Path(tmp) / "home"
                _open_dangling_window(_orch_root(repo), request=request)
                with mock.patch.dict(os.environ, _home_env(home), clear=False):
                    incident = diag.build_launch_incident(repo, ORCH_ID)
                assert incident is not None
                child = incident["transcripts"]["child_transcript"]
                self.assertEqual(child["reason"], diag._OPERATOR_ROOT_MISS)
                self.assertEqual(child["searched_projects_root"],
                                 str(home / ".claude" / "projects"))

    def test_no_incident_when_window_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _orch_root(repo)  # empty orchestration, no active_child file
            self.assertIsNone(diag.build_launch_incident(repo, ORCH_ID))


class LeafTranscriptRootTests(unittest.TestCase):
    """The post-mortem readers find the transcript in the ONE root its launch record selects.

    Issue #63 moved an AGENTIC leaf's transcript out of `~/.claude/projects` into the private
    home the conductor prepared for it, and this class pinned both readers onto that root —
    the code was already correct when it was written, but NOTHING observed it: re-pointing
    either reader at the operator home left the whole suite green.

    Z4 (issue #171) retired the agentic leaf and the private home with it. A pure leaf runs
    with no `CLAUDE_CONFIG_DIR` (`tools/leaf_contract.py`), so its transcript is written where the
    CLI writes every transcript, and `claude_leaf_projects_roots` returns that root for a call
    that names no lineage. A claude `workshop leaf` (issue #534) writes under its lineage's
    `config/projects` instead; issue #565 gave the resolver a `lineage_id` and the locator a
    launch-record branch, and this class pins both halves: the operator home for a record that
    names no workshop, the lineage home ALONE for a claude workshop launch (equal to
    `LineagePaths.config / "projects"`, with both ids checked against the lineage-id grammar).

    ACCEPTED LOSS, recorded rather than discovered later: a transcript written by a pre-Z4
    agentic leaf sat under `<homes-root>/<orchestration_id>/claude/projects` and the
    diagnostician no longer looks there, so `build_launch_incident` answers "no leaf transcript
    located" for a dangling leaf of a pre-Z4 run. The files are not deleted — the run's own
    home is still enumerated by `tools/prune_workflow_homes.py` — and no such leaf can be
    launched again.
    """

    def _repo_and_slug(self, td: str) -> tuple[Path, str]:
        repo = Path(td) / "repo"
        repo.mkdir()
        meta = repo / "workspace" / "orchestrations" / "o"
        meta.mkdir(parents=True)
        (meta / "orchestration_meta.json").write_text(json.dumps({}), encoding="utf-8")
        slug = str(repo.resolve()).replace("/", "-")
        return repo, slug

    def test_a_transcript_in_the_operator_home_is_found(self) -> None:
        """The only root there is. This fixture was the pre-#63 case and is now the live one."""
        with tempfile.TemporaryDirectory() as td:
            repo, slug = self._repo_and_slug(td)
            operator = Path(td) / "operator"
            proj = operator / ".claude" / "projects" / slug
            proj.mkdir(parents=True)
            (proj / "arid-old.jsonl").write_text("{}\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"HOME": str(operator)}, clear=False):
                found = diag._locate_leaf_transcript("arid-old", repo, "o").match
            assert found is not None
            self.assertEqual(found.path, proj / "arid-old.jsonl")
            # The root, not just the file: an incident names where it looked, and
            # `test_a_pure_leafs_hit_in_the_operator_home_names_that_root` drives the same
            # shape end to end.
            self.assertEqual(found.projects_root, operator / ".claude" / "projects")

    def test_with_no_lineage_the_operator_home_is_the_only_root(self) -> None:
        """The COUNT, not just the first entry, for a call that names no lineage.
        `_locate_leaf_transcript` walks every root, so a second root reintroduced ahead of
        this one would send every audit somewhere a pure leaf never writes, and a reader that
        only checked `roots[0]` would not see it."""
        from tools.operator_private_root import claude_leaf_projects_roots
        with tempfile.TemporaryDirectory() as td:
            repo, _slug = self._repo_and_slug(td)
            operator = Path(td) / "operator"
            with mock.patch.dict(os.environ, {"HOME": str(operator)}, clear=False):
                roots = claude_leaf_projects_roots(repo, "o")
            self.assertEqual(roots, (operator / ".claude" / "projects",))

    def test_the_lineage_root_is_the_lineage_paths_config_home(self) -> None:
        """Pin (a) of issue #565: the resolver's plain join and `LineagePaths.config` are two
        spellings of one path — the second is what `Conductor._workshop_session_resumable`
        globs and the CLI is handed as `CLAUDE_CONFIG_DIR` — and this is where they meet."""
        from tools.operator_private_root import (
            claude_leaf_projects_roots,
            workflow_homes_root,
        )
        from tools.workshop_tree import LineagePaths
        with tempfile.TemporaryDirectory() as td:
            repo, _slug = self._repo_and_slug(td)
            with mock.patch.dict(os.environ, _home_env(Path(td) / "operator"),
                                 clear=False):
                homes = workflow_homes_root()
                roots = claude_leaf_projects_roots(repo, "o", "lid")
            lineage = LineagePaths(homes, "o", "lid",
                                   repo / "workspace" / "orchestrations" / "o")
            self.assertEqual(roots, (lineage.config / "projects",))

    def test_the_lineage_branch_refuses_an_id_outside_the_grammar(self) -> None:
        """Pin (b): both ids are checked before they are joined, so neither a `/`-bearing
        lineage id nor a `/`-bearing orchestration id reaches the path."""
        from tools.operator_private_root import claude_leaf_projects_roots
        with tempfile.TemporaryDirectory() as td:
            repo, _slug = self._repo_and_slug(td)
            with mock.patch.dict(os.environ, _home_env(Path(td) / "operator"),
                                 clear=False):
                for oid, lid in (("o", "../x"), ("o", ""), ("o", "a/b"), ("o", 7),
                                 ("../o", "lid"), (None, "lid"), ("", "lid")):
                    with self.subTest(oid=oid, lid=lid), self.assertRaises(ValueError):
                        claude_leaf_projects_roots(repo, oid, lid)  # type: ignore[arg-type]

    def test_a_claude_workshop_launch_is_found_in_its_lineage_home_only(self) -> None:
        """Pin (c): the launch record selects the lineage home, and only it (decision 1)."""
        for planted in ("lineage", "operator"):
            with self.subTest(planted=planted), tempfile.TemporaryDirectory() as td:
                repo = Path(td) / "repo"
                orch = _orch_root(repo)
                _open_dangling_window(orch, request=_workshop_request())
                operator = Path(td) / "operator"
                with mock.patch.dict(os.environ, _home_env(operator), clear=False):
                    lineage = _lineage_projects()
                    target = (lineage if planted == "lineage"
                              else operator / ".claude" / "projects")
                    path = _write_transcript(target)
                    search = diag._locate_leaf_transcript(CHILD_ARID, repo, ORCH_ID)
                self.assertEqual(search.searched_root, lineage)
                if planted == "lineage":
                    assert search.match is not None
                    self.assertEqual(search.match.path, path)
                    self.assertEqual(search.match.projects_root, lineage)
                    self.assertIsNone(search.reason)
                else:
                    self.assertIsNone(search.match)
                    self.assertIn(str(lineage), search.reason or "")

    def test_the_provider_spelling_matches_the_launch_record_vocabulary(self) -> None:
        """The locator spells the four `workshop_provider` values rather than importing
        `leaf_contract`; a fifth provider added there must be classified here too."""
        from tools.leaf_contract import WORKSHOP_LINEAGE_BACKENDS
        self.assertEqual(set(diag._WORKSHOP_PROVIDERS), set(WORKSHOP_LINEAGE_BACKENDS))
        self.assertEqual(WORKSHOP_LINEAGE_BACKENDS[diag._WORKSHOP_PROVIDER_CLAUDE], "claude")
        self.assertEqual(WORKSHOP_LINEAGE_BACKENDS[diag._WORKSHOP_PROVIDER_CODEX], "codex")
        self.assertEqual({WORKSHOP_LINEAGE_BACKENDS[p] for p in diag._WORKSHOP_PROVIDERS_HTTP},
                         {"http"})

    def test_the_lineage_id_grammar_is_one_grammar(self) -> None:
        """The locator, the resolver and `workshop_tree` each spell `[A-Za-z0-9_-]+`; the
        locator checks it first so a bad id is a typed miss, and the three must agree."""
        from tools import operator_private_root, workshop_tree
        self.assertEqual(diag._LINEAGE_ID_RE.pattern, workshop_tree._SAFE_ID_RE.pattern)
        self.assertEqual(operator_private_root._LINEAGE_ID_RE.pattern,
                         workshop_tree._SAFE_ID_RE.pattern)


class SummarizePureLeafMetasTest(unittest.TestCase):
    """tools/orchestration_diagnostics.summarize_pure_leaf_metas (Z2 M-E)."""

    def _bundle_meta(self, **overrides) -> dict:
        meta = {
            "result": "pass",
            "failure_category": None,
            "attempts": 2,
            "prompt_contract_version": "pure-1",
            "per_attempt": [
                {
                    "agent_run_id": "arid-1",
                    "model": "claude-opus-4-8",
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "cache_read_input_tokens": 10,
                        "cache_creation_input_tokens": 5,
                    },
                },
                {
                    "agent_run_id": "arid-2",
                    "model": "claude-opus-4-8",
                    "usage": {"input_tokens": 200, "output_tokens": 80},
                },
            ],
        }
        meta.update(overrides)
        return meta

    def test_reads_both_metas_and_sums_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "source" / "src_x"
            src.mkdir(parents=True)
            (src / "bundle_meta.json").write_text(
                json.dumps(self._bundle_meta()), encoding="utf-8"
            )
            (src / "verdict_meta.json").write_text(
                json.dumps(
                    {
                        "result": "fail",
                        "failure_category": "verdict_schema_violation",
                        "attempts": 1,
                        "prompt_contract_version": "pure-1",
                        "per_attempt": [
                            {"agent_run_id": "v-1", "model": "claude-opus-4-8", "usage": {"input_tokens": 300, "output_tokens": 20}}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            out = diag.summarize_pure_leaf_metas(src, "generate")
        self.assertTrue(out["found"])
        gen = out["generate"]
        self.assertTrue(gen["found"])
        self.assertEqual(gen["result"], "pass")
        self.assertEqual(gen["attempts"], 2)
        self.assertEqual(gen["repair_turns"], 1)
        self.assertEqual(gen["usage_total"]["input_tokens"], 300)
        self.assertEqual(gen["usage_total"]["output_tokens"], 130)
        self.assertEqual(gen["usage_total"]["cache_read_input_tokens"], 10)
        self.assertEqual(gen["usage_total"]["total_tokens"], 300 + 130 + 10 + 5)
        self.assertEqual(gen["models"], ["claude-opus-4-8", "claude-opus-4-8"])
        ver = out["verify"]
        self.assertEqual(ver["result"], "fail")
        self.assertEqual(ver["failure_category"], "verdict_schema_violation")
        self.assertEqual(ver["repair_turns"], 0)

    def test_legacy_node_has_no_metas(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src_legacy"
            src.mkdir(parents=True)
            out = diag.summarize_pure_leaf_metas(src, "generate")
        self.assertFalse(out["found"])
        self.assertFalse(out["generate"]["found"])
        self.assertFalse(out["verify"]["found"])

    def test_malformed_usage_and_missing_attempts_do_not_raise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src_bad"
            src.mkdir(parents=True)
            (src / "bundle_meta.json").write_text(
                json.dumps(
                    {
                        "result": "pass",
                        "per_attempt": [
                            {"agent_run_id": "a", "model": None, "usage": None},
                            {"agent_run_id": "b", "usage": {"input_tokens": "oops"}},
                            "not-a-dict",
                        ],
                    }
                ),
                encoding="utf-8",
            )
            out = diag.summarize_pure_leaf_metas(src, "generate")
        gen = out["generate"]
        self.assertTrue(gen["found"])
        # attempts falls back to the count of structurally-valid (dict) attempts
        # when absent — the "not-a-dict" entry is not counted.
        self.assertEqual(gen["attempts"], 2)
        # non-int usage values ("oops", None) coerce to 0, never raise
        self.assertEqual(gen["usage_total"]["total_tokens"], 0)
        self.assertEqual(gen["models"], [])

    def test_non_pure_dict_and_infinity_usage_do_not_mislead_or_raise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src_edge"
            src.mkdir(parents=True)
            # An empty / unrelated JSON object is not a pure-leaf meta envelope.
            (src / "bundle_meta.json").write_text(json.dumps({}), encoding="utf-8")
            # Infinity is a valid JSON value to Python's decoder; int(inf) would
            # raise OverflowError — the coercion must swallow it as 0.
            (src / "verdict_meta.json").write_text(
                '{"result": "pass", "per_attempt": [{"usage": {"input_tokens": Infinity, "output_tokens": -5}}]}',
                encoding="utf-8",
            )
            out = diag.summarize_pure_leaf_metas(src, "generate")
        self.assertFalse(out["generate"]["found"])  # empty dict → not a pure meta
        ver = out["verify"]
        self.assertTrue(ver["found"])
        self.assertEqual(ver["usage_total"]["total_tokens"], 0)  # inf→0, -5→0

    def test_unparseable_meta_degrades_to_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src_broken"
            src.mkdir(parents=True)
            (src / "bundle_meta.json").write_text("{not json", encoding="utf-8")
            out = diag.summarize_pure_leaf_metas(src, "generate")
        self.assertFalse(out["generate"]["found"])

    def test_non_utf8_meta_degrades_and_does_not_raise(self) -> None:
        # `read_text(encoding="utf-8")` raises UnicodeDecodeError — a ValueError, NOT
        # an OSError — so a narrow `(OSError, JSONDecodeError)` catch would let a
        # corrupt-byte file escape the "degrade, never raise" contract.
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src_badbytes"
            src.mkdir(parents=True)
            (src / "bundle_meta.json").write_bytes(
                b'{"per_attempt": [], "result": "\xff\xfe pass"}'
            )
            out = diag.summarize_pure_leaf_metas(src, "generate")  # must not raise
        self.assertFalse(out["generate"]["found"])
        self.assertFalse(out["found"])

    def test_bools_are_not_counted_as_ints(self) -> None:
        # bool IS an int subclass, so a missing isinstance(value, bool) guard would
        # let attempts=True render verbatim and sum booleans as tokens.
        row = diag._summarize_one_pure_meta(
            {
                "result": "pass",
                "attempts": True,
                "per_attempt": [{"usage": {"input_tokens": True, "output_tokens": 5}}],
            }
        )
        self.assertEqual(row["attempts"], 1)  # True rejected → falls back to 1 valid attempt
        self.assertIsNot(row["attempts"], True)
        self.assertEqual(row["usage_total"]["input_tokens"], 0)  # True is not 1 token
        self.assertEqual(row["usage_total"]["output_tokens"], 5)

    def test_each_compile_part_s_first_launch_is_not_a_repair_turn(self) -> None:
        """`compile_generate_meta.json` concatenates one loop per part (issue #526): a clean
        core + two parts is three launches and no repair turn; one repaired part is one."""
        def meta(n_rows: int, parts: int) -> dict:
            return {"result": "pass", "attempts": n_rows,
                    "per_attempt": [{"usage": {}} for _ in range(n_rows)],
                    "parts": [{"part": f"p{i}"} for i in range(parts)]}
        self.assertEqual(diag._summarize_one_pure_meta(meta(3, 3))["repair_turns"], 0)
        self.assertEqual(diag._summarize_one_pure_meta(meta(4, 3))["repair_turns"], 1)
        # A record with no `parts[]` is one loop, as before.
        self.assertEqual(diag._summarize_one_pure_meta(
            {"result": "pass", "attempts": 2, "per_attempt": [{}, {}]})["repair_turns"], 1)

    def test_non_list_per_attempt_does_not_raise(self) -> None:
        # A corrupt scalar per_attempt must degrade, not TypeError out of the
        # "never raises" contract.
        for bad in (5, "x", {"a": 1}, None):
            row = diag._summarize_one_pure_meta({"result": "pass", "per_attempt": bad})
            self.assertTrue(row["found"])
            self.assertEqual(row["attempts"], 0)
            self.assertEqual(row["usage_total"]["total_tokens"], 0)
            self.assertEqual(row["models"], [])

    def test_models_preserve_attempt_order_and_skip_empty(self) -> None:
        # `models` is distinct-IN-ORDER: a set-based dedup would sort and lose the
        # per-attempt sequence. An empty-string model is not a model.
        _, models = diag._sum_pure_attempt_usage(
            [
                {"model": "zeta"}, {"model": "alpha"}, {"model": "zeta"},
                {"model": ""}, {"model": None},
            ]
        )
        self.assertEqual(models, ["zeta", "alpha", "zeta"])  # order kept, empties dropped

    def test_zero_attempts_does_not_produce_negative_repair_turns(self) -> None:
        # repair_turns is clamped: a corrupt attempts=0 must not render as -1.
        row = diag._summarize_one_pure_meta({"result": "fail", "attempts": 0, "per_attempt": []})
        self.assertEqual(row["attempts"], 0)
        self.assertEqual(row["repair_turns"], 0)

    def test_usage_wait_rows_are_not_counted_as_repair_turns(self) -> None:
        # A non-terminal `pure_transport` row is a re-launch in place — a `--wait-usage-reset`
        # wait or a transient retry — not a repair turn. `attempts` counts every launch, so
        # repair_turns must subtract the re-launches.
        # wait -> pass: 2 launches, 0 repairs.
        row = diag._summarize_one_pure_meta({
            "result": "pass", "attempts": 2,
            "per_attempt": [{"failure_category": "pure_transport"}, {}]})
        self.assertEqual(row["attempts"], 2)
        self.assertEqual(row["repair_turns"], 0)
        # wait -> content-fail -> repair -> pass: 3 launches, 1 repair.
        row = diag._summarize_one_pure_meta({
            "result": "pass", "attempts": 3,
            "per_attempt": [{"failure_category": "pure_transport"},
                            {"failure_category": "bundle_schema_violation"}, {}]})
        self.assertEqual(row["repair_turns"], 1)
        # A genuine TERMINAL transport death (the last row) is NOT a wait — 1 launch, 0 repairs.
        row = diag._summarize_one_pure_meta({
            "result": "fail", "attempts": 1,
            "per_attempt": [{"failure_category": "pure_transport"}]})
        self.assertEqual(row["repair_turns"], 0)
        # ...and the LAST row is excluded only when it is the last: a content repair that then
        # died terminal of transport is 2 launches, 1 repair. Counting every transport row
        # (`per_attempt` instead of `per_attempt[:-1]`) would read it as 0 repairs.
        row = diag._summarize_one_pure_meta({
            "result": "fail", "attempts": 2,
            "per_attempt": [{"failure_category": "bundle_schema_violation"},
                            {"failure_category": "pure_transport"}]})
        self.assertEqual(row["repair_turns"], 1)
        # A pure content repair with no waits is unchanged: 2 launches, 1 repair.
        row = diag._summarize_one_pure_meta({
            "result": "pass", "attempts": 2,
            "per_attempt": [{"failure_category": "bundle_schema_violation"}, {}]})
        self.assertEqual(row["repair_turns"], 1)

    def test_foreign_json_without_payload_key_is_not_a_pure_meta(self) -> None:
        # A stale/hand-edited document carrying only common keys must NOT be
        # reported as a pure-leaf row of all-zero metrics: `per_attempt` (the
        # measurement payload) is the discriminator, not `result`/`attempts`.
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src_foreign"
            src.mkdir(parents=True)
            (src / "bundle_meta.json").write_text(
                json.dumps({"result": "ok", "attempts": 7}), encoding="utf-8"
            )
            out = diag.summarize_pure_leaf_metas(src, "generate")
        self.assertFalse(out["generate"]["found"])
        self.assertFalse(out["found"])

    def test_usage_key_tuple_stays_consistent(self) -> None:
        # The per-attempt sum keys derive from the canonical token classes
        # (`tools/leaf_usage.py`, the shape every backend records against). Pin the relation
        # AND the contents: total_tokens/assistant_turns are derived, not per-attempt CLI
        # fields. (The transcript sum-key tuple that sat next to this one went with the
        # transcript usage aggregators, issue #179.)
        self.assertEqual(diag._PURE_ATTEMPT_USAGE_KEYS, diag.LEAF_TOKEN_CLASS_KEYS)
        self.assertNotIn("total_tokens", diag._PURE_ATTEMPT_USAGE_KEYS)
        self.assertNotIn("assistant_turns", diag._PURE_ATTEMPT_USAGE_KEYS)
        self.assertEqual(
            diag._PURE_ATTEMPT_USAGE_KEYS,
            ("input_tokens", "output_tokens", "cache_read_input_tokens",
             "cache_creation_input_tokens"),
        )

    def test_row_carries_no_source_dir_key(self) -> None:
        # The caller owns the label (the audit rollup attaches a repo-relative
        # path); the callee must not publish a second, absolute notion of it.
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src_x"
            src.mkdir(parents=True)
            out = diag.summarize_pure_leaf_metas(src, "generate")
        self.assertNotIn("source_dir", out)


class PureLeafMetaWriterReaderContractTest(unittest.TestCase):
    """The conductor writes bundle_meta/verdict_meta; this module reads them. The
    two live in different modules with no shared schema constant, so drive the REAL
    writer into the reader — a renamed/dropped field breaks here rather than
    silently reporting zero tokens (both modules' own suites would stay green)."""

    def test_conductor_written_metas_round_trip_through_reader(self) -> None:
        from tools.tests.test_leaf_producer import _conductor, _write_node

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _write_node(repo)
            c = _conductor(repo)
            src_dir = repo / refs.source_dir()
            src_dir.mkdir(parents=True, exist_ok=True)
            per_attempt = [
                {"agent_run_id": "a1", "model": "claude-opus-4-8",
                 "usage": {"input_tokens": 11, "output_tokens": 22,
                           "cache_read_input_tokens": 33, "cache_creation_input_tokens": 44}},
            ]
            c._write_bundle_meta(
                refs, result="fail", failure_category="bundle_schema_violation",
                failure_excerpt="boom", attempts=2, per_attempt=per_attempt,
            )
            c._write_verdict_meta(
                refs, result="pass", failure_category=None, failure_excerpt=None,
                attempts=1, per_attempt=per_attempt,
            )
            out = diag.summarize_pure_leaf_metas(src_dir, "generate")

        self.assertTrue(out["found"])
        gen = out["generate"]
        self.assertTrue(gen["found"], "reader must recognize the conductor's bundle_meta envelope")
        self.assertEqual(gen["result"], "fail")
        self.assertEqual(gen["attempts"], 2)
        self.assertEqual(gen["failure_category"], "bundle_schema_violation")
        # The writer stamps the contract version; the reader must surface it.
        self.assertEqual(gen["prompt_contract_version"], LEAF_PROMPT_CONTRACT_VERSION)
        # Every usage class the writer persists must be summed by the reader.
        self.assertEqual(gen["usage_total"]["total_tokens"], 11 + 22 + 33 + 44)
        self.assertEqual(gen["models"], ["claude-opus-4-8"])
        self.assertTrue(out["verify"]["found"])
        self.assertEqual(out["verify"]["result"], "pass")


_DETECTION_PATH_FUNCS = ("build_launch_incident", "detect_dangling_active_child",
                         "_locate_leaf_transcript")


def _blanket_swallows(source: str, funcs: tuple[str, ...]) -> list[str]:
    """Blanket exception swallows inside ``funcs`` (nested functions included).

    ONE rule in one place, replacing a per-call-site scan that had been widened twice
    and refused correct code each time. What is forbidden is a handler that catches
    everything and does NOT re-raise:

    - `except Exception:` / `except BaseException:` / bare `except:` whose body has no
      `raise`, and
    - `with contextlib.suppress(Exception)` / `suppress(BaseException)`.

    What is ALLOWED, deliberately, because each is correct code the earlier version
    refused: a NARROW handler (`except OSError` — `detect_dangling_active_child` really
    does need one around its pointer read), a `try/finally`, and a broad handler that
    adds context and re-raises. The rule is "nothing on this path may turn a failure
    into a value", not "no error handling on this path".

    Its answer on this tree is `[]`, so it is driven both ways on synthetic sources in
    `SwallowScannerTests` — an assertion against `[]` is green whether the rule works or
    returns nothing unconditionally.
    """
    def _is_broad(node: ast.expr | None) -> bool:
        if node is None:  # bare `except:`
            return True
        names = node.elts if isinstance(node, ast.Tuple) else [node]
        return any(isinstance(n, ast.Name) and n.id in ("Exception", "BaseException")
                   for n in names)

    def _reraises(body: list[ast.stmt]) -> bool:
        return any(isinstance(n, ast.Raise) for stmt in body for n in ast.walk(stmt))

    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in funcs):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Try):
                for h in inner.handlers:
                    if _is_broad(h.type) and not _reraises(h.body):
                        found.append(f"{node.name}:{h.lineno} except")
            elif isinstance(inner, (ast.With, ast.AsyncWith)):
                for item in inner.items:
                    ctx = item.context_expr
                    if not isinstance(ctx, ast.Call):
                        continue
                    fname = ctx.func.attr if isinstance(ctx.func, ast.Attribute) else (
                        ctx.func.id if isinstance(ctx.func, ast.Name) else "")
                    if fname == "suppress" and any(_is_broad(a) for a in ctx.args):
                        found.append(f"{node.name}:{inner.lineno} suppress")
    return found


class SwallowScannerTests(unittest.TestCase):
    """Self-test for `_blanket_swallows`: on the real file the answer is `[]`, and an
    assertion against `[]` is green whether the rule works or always returns one.

    Both directions, because the over-refusing direction is the one this repository gets
    wrong: an earlier version of this rule rejected a context-preserving re-raise and a
    narrow `except OSError`, both of which are correct code."""

    F = ("f",)

    def test_it_finds_a_bare_and_a_broad_handler(self) -> None:
        self.assertEqual(len(_blanket_swallows(
            "def f():\n    try:\n        g()\n    except Exception:\n        return None\n",
            self.F)), 1)
        self.assertEqual(len(_blanket_swallows(
            "def f():\n    try:\n        g()\n    except:\n        return None\n",
            self.F)), 1)
        self.assertEqual(len(_blanket_swallows(
            "def f():\n    try:\n        g()\n    except (ValueError, Exception):\n"
            "        return None\n", self.F)), 1)

    def test_it_finds_a_broad_suppress(self) -> None:
        self.assertEqual(len(_blanket_swallows(
            "def f():\n    with contextlib.suppress(Exception):\n        g()\n",
            self.F)), 1)
        self.assertEqual(len(_blanket_swallows(
            "def f():\n    with suppress(BaseException):\n        g()\n", self.F)), 1)

    def test_it_finds_one_in_a_NESTED_function(self) -> None:
        """`_is_dangling` is nested inside the detector, and round 5 put a swallow there."""
        self.assertEqual(len(_blanket_swallows(
            "def f():\n    def inner():\n        try:\n            g()\n"
            "        except Exception:\n            return False\n", self.F)), 1)

    def test_it_allows_a_narrow_handler(self) -> None:
        """`detect_dangling_active_child` needs `except OSError` around its pointer read.
        Refusing that was a real over-refusal in the previous version of this rule."""
        self.assertEqual(_blanket_swallows(
            "def f():\n    try:\n        g()\n    except OSError:\n        x = ''\n",
            self.F), [])
        self.assertEqual(_blanket_swallows(
            "def f():\n    with contextlib.suppress(OSError):\n        g()\n",
            self.F), [])

    def test_it_allows_a_broad_handler_that_RE_RAISES(self) -> None:
        """Adding context and re-raising does not turn a failure into a value, so the
        rule must not refuse it — the other over-refusal the previous version had."""
        self.assertEqual(_blanket_swallows(
            "def f():\n    try:\n        g()\n    except Exception as e:\n"
            "        raise RuntimeError('ctx') from e\n", self.F), [])
        self.assertEqual(_blanket_swallows(
            "def f():\n    try:\n        g()\n    except Exception:\n"
            "        log()\n        raise\n", self.F), [])

    def test_it_allows_try_finally_and_ignores_other_functions(self) -> None:
        self.assertEqual(_blanket_swallows(
            "def f():\n    try:\n        g()\n    finally:\n        pass\n", self.F), [])
        self.assertEqual(_blanket_swallows(
            "def other():\n    try:\n        g()\n    except Exception:\n        pass\n",
            self.F), [])


def _caused_by(exc: BaseException, wanted: type[BaseException]) -> bool:
    """Is ``wanted`` anywhere in ``exc``'s cause / context chain (or ``exc`` itself)?"""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        if isinstance(cur, wanted):
            return True
        seen.add(id(cur))
        cur = cur.__cause__ or cur.__context__
    return False


class _Injected(Exception):
    """Private to `CollectorsDoNotSwallowTests`: no handler in the tree names it, so
    catching it requires a blanket `except Exception` / bare `except` / `suppress`."""


class CollectorsDoNotSwallowTests(unittest.TestCase):
    """Round 1 of #130's review: a mutant that wrapped `detect_dangling_active_child` in
    `try/except Exception: return None` INSIDE `build_launch_incident` kept all 144 tests
    green — and would restore issue #130's symptom exactly (a clean negative and exit 0),
    because `audit()` records only what IT catches.

    The invariant: the swallow lives at the `audit()` boundary, where it is recorded in
    `diagnostic_failures`. This collector must let a failure out.
    """

    def test_a_detector_failure_of_any_type_reaches_the_boundary(self) -> None:
        """The narrow-but-typed swallow, which the two checks around this one both miss.

        `_blanket_swallows` only flags a handler that catches everything, and the
        primitive injection below is not caught by a narrowly-typed handler either — so
        `with contextlib.suppress(ImportError)` around this one call survived both,
        measured. Nothing on this call is legitimate to suppress at ANY type: whatever
        the detector raises has to reach `audit()`, which is the only place that records
        it. `ImportError` leads the list because it is the class issue #130 raised.

        Chain-based, like the injection test, so a broad handler that re-raises with
        context is not refused.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _open_dangling_window(_orch_root(repo))
            for exc in (ImportError, ModuleNotFoundError, OSError, RuntimeError, ValueError):
                with self.subTest(exc=exc.__name__), \
                        mock.patch.object(diag, "detect_dangling_active_child",
                                          side_effect=exc("boom")), \
                        self.assertRaises(Exception) as cm:
                    diag.build_launch_incident(repo, ORCH_ID)
                self.assertTrue(_caused_by(cm.exception, exc))

    def test_no_blanket_swallow_sits_between_the_disk_and_the_boundary(self) -> None:
        """The invariant asked BEHAVIOURALLY, at the I/O primitives themselves.

        Four review rounds broke this one mechanism in five spellings — `except OSError`
        in `build_launch_incident`, `contextlib.suppress(ImportError)`, a wrapper around
        `detect_dangling_active_child`, and then two more one statement apart INSIDE the
        detector. Each source scan was widened and the next round found another; a scan
        over a named function can only ever produce rejection samples.

        An earlier version of this test injected at `_read_jsonl` and its docstring
        claimed to hold "whatever the code in between looks like". That was FALSE, and
        two round-5 reviewers found it independently: `_read_jsonl`'s first call site is
        partway down the detector, so the injection stopped there and nothing before or
        after it was covered. The claim is now made true rather than narrowed — the
        injection moved DOWN to the `pathlib` primitives every reader on the path
        bottoms out in, one at a time.

        The injected type is private to this test, which is what makes the check ask
        about a BLANKET swallow rather than about handling. A legitimate narrow handler —
        `detect_dangling_active_child` really does catch `OSError` around its pointer
        read (`:155-158`), and `_locate_leaf_transcript` catches `(OSError, ValueError)` —
        does not catch `_Injected` and so is not refused. Only `except Exception`,
        `contextlib.suppress(Exception)`, or a bare `except` is, anywhere on the path.

        And it requires the injection to be the CAUSE, not to be the exception itself:
        a broad handler that adds context and re-raises satisfies the invariant — the
        failure still reaches the boundary and is still recorded — so demanding the exact
        type back would refuse correct code. That over-refusal was real and measured; the
        chain walk is what removes it.
        """
        # `is_file` left this tuple with the child-return ack (issue #447, D2): that check was
        # the path's only `is_file` call, so injecting there reached nothing and the
        # `assertRaises` below went red. A primitive the path does not call cannot be asked
        # this question; one it starts calling belongs back here — and `is_file` is back
        # (issue #565): the locator asks it of the launch record before reading it.
        for prim in ("read_text", "is_dir", "glob", "is_file"):
            with self.subTest(primitive=prim), tempfile.TemporaryDirectory() as tmp:
                    repo = Path(tmp)
                    _open_dangling_window(_orch_root(repo))
                    real = getattr(Path, prim)

                    def _boom(self_path, *a, _real=real, _p=prim, **k):
                        # Only the orchestration's own tree is poisoned, so the
                        # temp-dir machinery around the call keeps working.
                        if ORCH_ID in str(self_path):
                            raise _Injected(_p)
                        return _real(self_path, *a, **k)

                    with mock.patch.object(Path, prim, _boom), \
                            self.assertRaises(Exception) as cm:
                        diag.build_launch_incident(repo, ORCH_ID)
                    self.assertTrue(
                        _caused_by(cm.exception, _Injected),
                        msg=f"{prim}: something escaped, but not from the injection")

    def test_a_lineage_glob_failure_reaches_the_boundary(self) -> None:
        """Pin (g) of issue #565: the lineage branch is on the behavioural swallow check too.

        The window above carries a record with no `leaf_mode`, so it only ever reaches the
        operator-root glob — and the primitives it poisons are first hit by the detector,
        before the locator runs (`is_file` excepted: the locator's own read of the launch
        record is its first caller). Here a claude workshop record is laid out and ONLY the glob
        under the lineage root is poisoned (its path contains `ORCH_ID` and the lineage id),
        so what is observed is the locator's own lineage glob."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            home = Path(tmp) / "home"
            _open_dangling_window(_orch_root(repo), request=_workshop_request())
            real = Path.glob

            def _boom(self_path, *a, **k):
                text = str(self_path)
                if ORCH_ID in text and LINEAGE_ID in text:
                    raise _Injected("glob")
                return real(self_path, *a, **k)

            with mock.patch.dict(os.environ, _home_env(home), clear=False):
                _write_transcript(_lineage_projects())
                with mock.patch.object(Path, "glob", _boom), \
                        self.assertRaises(Exception) as cm:
                    diag.build_launch_incident(repo, ORCH_ID)
            self.assertTrue(_caused_by(cm.exception, _Injected),
                            msg="something escaped, but not from the lineage glob")

    def test_nothing_on_the_detection_path_swallows_broadly(self) -> None:
        """The complement to the injection above, and it is needed: WIDENING the
        legitimate `except OSError` to `except Exception` leaves nothing to propagate, so
        no injection can see it — measured, that mutant survived the injection test. A
        source rule answers it exactly, and this one allows the narrow handler it is
        widening FROM."""
        source = (Path(__file__).resolve().parent.parent
                  / "orchestration_diagnostics.py").read_text(encoding="utf-8")
        self.assertEqual(_blanket_swallows(source, _DETECTION_PATH_FUNCS), [])


class PureLeafMetaPhaseTests(unittest.TestCase):
    """`summarize_pure_leaf_metas`' phase selector, both directions.

    The `phase` argument is REQUIRED (no default), because the two phases' records live in
    different directories under different names and a caller that forgot it would silently read
    the wrong pair and report `found=False` — indistinguishable from an agentic node. The
    unknown-phase guard exists for the same reason in reverse: this module promises
    "best-effort: never raises", and a `KeyError` out of a diagnostics helper breaks an audit
    instead of reporting a gap.
    """

    def _write(self, d, names):
        d.mkdir(parents=True, exist_ok=True)
        for name in names:
            (d / name).write_text(json.dumps({
                "result": "pass", "failure_category": None, "attempts": 1,
                "per_attempt": [{"agent_run_id": "x", "model": "m",
                                 "usage": {"input_tokens": 1, "output_tokens": 2}}],
            }), encoding="utf-8")

    def test_each_phase_reads_its_own_two_files(self) -> None:
        for phase, mine, theirs in (
            ("generate", ("bundle_meta.json", "verdict_meta.json"),
             ("compile_generate_meta.json", "compile_verify_meta.json")),
            ("compile", ("compile_generate_meta.json", "compile_verify_meta.json"),
             ("bundle_meta.json", "verdict_meta.json")),
        ):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                d = Path(tmp) / "artifacts"
                self._write(d, mine)
                out = diag.summarize_pure_leaf_metas(d, phase)
                self.assertTrue(out["found"], phase)
                self.assertEqual(out["generate"]["result"], "pass")
                self.assertEqual(out["verify"]["result"], "pass")
            # ...and the OTHER phase's files are not picked up, which is the half that would
            # silently pass if the table's two rows were swapped or shared.
            with self.subTest(phase=phase, direction="other"), tempfile.TemporaryDirectory() as tmp:
                d = Path(tmp) / "artifacts"
                self._write(d, theirs)
                self.assertFalse(diag.summarize_pure_leaf_metas(d, phase)["found"], phase)

    def test_reviewer_row_carries_the_projected_verdict_beside_the_loop_outcome(self) -> None:
        """`result` is the loop outcome — the conductor writes `pass` whenever a schema-valid
        document was obtained, whatever it decided — so a rejecting reviewer read `result=pass`
        (issue #241). The row now reads the verdict from its projection file and carries it as
        `verdict`; a producer row, which has no verdict, carries no such key."""
        cases = (
            ("compile", "verify", "ir_meta.json", "verification_status", "fail"),
            ("generate", "verify", "source_meta.json", "verification_status", "fail"),
            ("validate", "judge", "semantic_review.json", "decision", "fail"),
        )
        for phase, substep, basename, field, decision in cases:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                d = Path(tmp) / "artifacts"
                self._write(d, tuple(diag.PURE_LEAF_META_FILES[phase].values()))
                (d / basename).write_text(json.dumps({field: decision}), encoding="utf-8")
                out = diag.summarize_pure_leaf_metas(d, phase)
                self.assertEqual(out[substep]["result"], "pass")
                self.assertEqual(out[substep]["verdict"], decision)
                self.assertIsNone(out[substep]["revocation"])
                self.assertIsNone(out[substep]["revoked_by"])
                for other, row in out.items():
                    if other not in (substep, "found"):
                        for key in ("verdict", "revocation", "revoked_by"):
                            self.assertNotIn(key, row, other)

    def test_a_revoked_projection_reads_the_reviewers_own_decision(self) -> None:
        """`revoke-artifact` rewrites `verification_status` to `revoked` and keeps the decision
        in `prior_verification_status`; the row carries THAT as the verdict and the route's
        reason as `revocation`. Round 1 of issue #241: the reference run held a REJECTED
        artifact and an ACCEPTED one under the same `revoked` status, and reading the status
        verbatim rendered both as a rejection."""
        cases = (
            # The two shapes of the audited runs: an accepted source revoked by ANOTHER
            # orchestration's validate failure (round 2: the arid is what places it), and a
            # rejected IR revoked by this run's own verify route.
            ({"verification_status": "revoked", "prior_verification_status": "pass",
              "revocation_reason": "validate_execute_post_execute_violation",
              "revoked_by_agent_run_id": "0df29820-other-run"},
             "pass", "validate_execute_post_execute_violation", "0df29820-other-run"),
            ({"verification_status": "revoked", "prior_verification_status": "fail",
              "revocation_reason": "verify_minor", "revoked_by_agent_run_id": "5895a595"},
             "fail", "verify_minor", "5895a595"),
            # A revocation that recorded no reason / arid, or malformed ones, is still one.
            ({"verification_status": "revoked", "prior_verification_status": "pass"},
             "pass", "", ""),
            ({"verification_status": "revoked", "prior_verification_status": "pass",
              "revocation_reason": 7, "revoked_by_agent_run_id": ["x"]},
             "pass", "", ""),
            # No prior at all: `revoked` is the most that can be said, and it is said.
            ({"verification_status": "revoked"}, "revoked", "", ""),
            ({"verification_status": "revoked", "prior_verification_status": ""},
             "revoked", "", ""),
        )
        for doc, verdict, revocation, by in cases:
            with self.subTest(doc=doc), tempfile.TemporaryDirectory() as tmp:
                d = Path(tmp) / "artifacts"
                self._write(d, ("compile_verify_meta.json",))
                (d / "ir_meta.json").write_text(json.dumps(doc), encoding="utf-8")
                row = diag.summarize_pure_leaf_metas(d, "compile")["verify"]
                self.assertEqual(row["verdict"], verdict)
                self.assertEqual(row["revocation"], revocation)
                self.assertEqual(row["revoked_by"], by)
        # `prior_verification_status` is consulted ONLY under `revoked`: a stale prior beside a
        # live status must not override it.
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "artifacts"
            self._write(d, ("compile_verify_meta.json",))
            (d / "ir_meta.json").write_text(json.dumps(
                {"verification_status": "pass", "prior_verification_status": "fail",
                 "revocation_reason": "stale", "revoked_by_agent_run_id": "stale"}),
                encoding="utf-8")
            row = diag.summarize_pure_leaf_metas(d, "compile")["verify"]
            self.assertEqual(row["verdict"], "pass")
            self.assertIsNone(row["revocation"])
            self.assertIsNone(row["revoked_by"])

    def test_a_pending_compile_projection_reads_pending(self) -> None:
        """`compile.generate` writes `ir_meta.json` at `pending` before the reviewer runs, so an
        exhausted `compile.verify` reads `pending` — passed through as the file says, not mapped
        onto `none` (round 1 of issue #241, F2)."""
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "artifacts"
            self._write(d, ("compile_verify_meta.json",))
            (d / "ir_meta.json").write_text(json.dumps({"verification_status": "pending"}),
                                            encoding="utf-8")
            row = diag.summarize_pure_leaf_metas(d, "compile")["verify"]
        self.assertEqual(row["verdict"], "pending")
        self.assertIsNone(row["revocation"])
        self.assertIsNone(row["revoked_by"])

    def test_reviewer_row_without_a_projection_reads_verdict_none(self) -> None:
        """A budget exhaustion writes no projection; the row says so (`None`, rendered
        `none`) rather than inventing a verdict, and a malformed projection reads the same."""
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "artifacts"
            self._write(d, ("compile_verify_meta.json",))
            out = diag.summarize_pure_leaf_metas(d, "compile")
            self.assertIn("verdict", out["verify"])
            self.assertIsNone(out["verify"]["verdict"])
            self.assertIn("revocation", out["verify"])
            self.assertIsNone(out["verify"]["revocation"])
            self.assertIn("revoked_by", out["verify"])
            self.assertIsNone(out["verify"]["revoked_by"])
            (d / "ir_meta.json").write_text("{not json", encoding="utf-8")
            self.assertIsNone(diag.summarize_pure_leaf_metas(d, "compile")["verify"]["verdict"])
            (d / "ir_meta.json").write_text(json.dumps({"verification_status": ""}),
                                            encoding="utf-8")
            self.assertIsNone(diag.summarize_pure_leaf_metas(d, "compile")["verify"]["verdict"])
            # A non-string value is not a verdict either: the renderer interpolates the field,
            # and a truthy non-string (`true`, `1`, a list) would print as one.
            for value in (True, 1, ["pass"], {"status": "pass"}):
                (d / "ir_meta.json").write_text(json.dumps({"verification_status": value}),
                                                encoding="utf-8")
                self.assertIsNone(
                    diag.summarize_pure_leaf_metas(d, "compile")["verify"]["verdict"], value)
        # A projection with no meta beside it does not conjure a row.
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "artifacts"
            d.mkdir()
            (d / "ir_meta.json").write_text(json.dumps({"verification_status": "pass"}),
                                            encoding="utf-8")
            self.assertEqual(diag.summarize_pure_leaf_metas(d, "compile")["verify"],
                             {"found": False})

    def test_an_unknown_phase_reports_a_gap_rather_than_raising(self) -> None:
        """`found: False` and no substep rows, because an unknown phase HAS no substeps to
        name. The subject used to be `validate`, which is a real phase since Z3 (issue #169);
        every caller labels the row by `found` and reaches its substep rows through `.get`."""
        with tempfile.TemporaryDirectory() as tmp:
            out = diag.summarize_pure_leaf_metas(Path(tmp), "no_such_phase")
            absent = diag.summarize_pure_leaf_metas(Path(tmp), "compile")
        self.assertEqual(out, {"found": False})
        self.assertFalse(absent["found"])
        self.assertEqual(set(absent) - {"found"}, {"generate", "verify"})

    def test_the_validate_phase_reads_its_single_judge_record(self) -> None:
        """Z3: `validate` has ONE pure leaf, and the table is a substep mapping rather than a
        producer/reviewer pair so it does not have to invent a second."""
        with tempfile.TemporaryDirectory() as tmp:
            run_node = Path(tmp)
            (run_node / "judge_meta.json").write_text(json.dumps(
                {"result": "pass", "attempts": 1, "prompt_contract_version": "pure-36",
                 "per_attempt": [{"agent_run_id": "c1", "model": "opus",
                                  "usage": {"input_tokens": 1, "output_tokens": 2}}]}),
                encoding="utf-8")
            out = diag.summarize_pure_leaf_metas(run_node, "validate")
        self.assertTrue(out["found"])
        self.assertTrue(out["judge"]["found"])
        self.assertEqual(set(out) - {"found"}, {"judge"})

    def test_the_file_table_names_exactly_the_phases_that_have_a_pure_pair(self) -> None:
        import tools.llm_config as lc
        self.assertEqual(set(diag.PURE_LEAF_META_FILES),
                         {phase for phase, _ in lc.LLM_LEAF_SUBSTEPS})

    def test_the_file_table_names_exactly_the_pure_capable_substeps(self) -> None:
        """The finer half of the row above: since Z3 the table is keyed by substep too, so a
        pure pair added to `llm_config` without a record here is red."""
        import tools.llm_config as lc
        self.assertEqual(
            {(phase, substep)
             for phase, files in diag.PURE_LEAF_META_FILES.items() for substep in files},
            set(lc.LLM_LEAF_SUBSTEPS))

    def test_the_workspace_layout_document_carries_every_record_in_the_table(self) -> None:
        """`docs/WORKSPACE_LAYOUT.md` is the canonical map of what lands where, and until issue
        #168 it named NONE of these five files — `judge_meta.json` was in its tree and in neither
        table, and the other four were in neither. The two rows above make a pure pair added to
        `llm_config` redden `PURE_LEAF_META_FILES`; this makes it redden the DOCUMENT too, so the
        map cannot silently fall behind the table again.

        Coupled ELEMENT BY ELEMENT AT ITS STATEMENT POSITION rather than by "the name occurs
        somewhere in the file" (`atmofab-enforcement-change` rule 3-a): a basename that appears
        only inside another row's prose — `compile_generate_meta.json` is named in
        `bundle_meta.json`'s own row, as its `compile` twin — must not satisfy the requirement
        for itself. Measured: with that row's own entry deleted the document still holds two
        occurrences of the name and this row is red. So the tree is read as a line whose entry is
        the name, and the table as a row whose FIRST cell ends in it. The expected set is derived from `PURE_LEAF_META_FILES`, never listed
        here, so this asserts the document against the code and not the code against a second
        hand-written list.

        BOTH DIRECTIONS, and the second is round 2's. The per-record requirement below is a
        GROWTH bound: it catches a record the code declares and the document omits. It said
        nothing about SHRINK — retiring a record from `PURE_LEAF_META_FILES` while its row and
        tree entry stay left this row GREEN (measured; the file went red only because the
        neighbouring rows react to the code change, which is not the same thing and disappears
        once the retirement is coordinated). The set-identity assertion over the table's
        ``(`pure` only)`` rows closes it: a document row still claiming to be a pure per-attempt
        record for a name the code no longer declares is an extra, and an extra now fails.

        WHAT THIS PINS AND WHAT IT ONLY SAMPLES, because the difference was measured rather than
        reasoned. PINNED: that each declared record has a tree entry of its own and a table row
        of its own, that the set of ``(`pure` only)`` rows is exactly the declared set, and that
        a rename in `PURE_LEAF_META_FILES` reddens the document. NOT pinned: everything else in
        the row. Two mutants of the document, both GREEN at round 1 --
        rewriting `compile_generate_meta.json`'s row path from `workspace/ir/.../<ir_id>/` to
        `workspace/pipelines/.../WRONG/DIR/`, and rewriting every `conductor
        (`_write_leaf_attempt_meta`)` writer cell to `LEAF (the leaf writes it itself)`, which
        inverts the row's own load-bearing sentence about the leaf holding no write authority.

        Those two are left uncovered deliberately. Coupling the DIRECTORY would need a
        phase-to-`NodeRefs`-accessor mapping written out here, which is the second hand-written
        list this row exists to avoid, and coupling the writer and reader columns would need the
        prose to be generated rather than written. They are verified by reading when the rows
        change; this row is the growth bound, not the whole review."""
        doc = (REPO_ROOT / "docs" / "WORKSPACE_LAYOUT.md").read_text(encoding="utf-8")
        expected = {name for files in diag.PURE_LEAF_META_FILES.values()
                    for name in files.values()}
        # The guard is against a BASENAME COLLISION, which would make the per-position reads
        # below answer for the wrong record. Round 1 caught it hardcoding the cardinality (5)
        # instead: a sixth pure record — the exact growth this coupling exists to catch — failed
        # HERE, with a message naming a collision that does not exist, and before the document
        # requirement could say what to add. The count now comes from the table.
        self.assertEqual(len(expected),
                         sum(len(files) for files in diag.PURE_LEAF_META_FILES.values()),
                         "the derivation collapsed: two phases share a record basename, and the "
                         "per-position reads below would then answer for the wrong one")
        tree_entries, table_first_cells = set(), set()
        for line in doc.splitlines():
            stripped = line.strip()
            for marker in ("\u251c\u2500\u2500 ", "\u2514\u2500\u2500 "):
                if marker in stripped:
                    tree_entries.add(stripped.split(marker, 1)[1].split()[0])
            if stripped.startswith("| `workspace/") and "` |" in stripped:
                table_first_cells.add(stripped.split("` |", 1)[0].removeprefix("| `"))
        # Self-test the two readers: a bound that reads nothing would make every requirement
        # below vacuously... unsatisfiable is the wrong direction, so assert they see the
        # document's OTHER, long-standing entries before requiring these five.
        self.assertIn("ir_meta.json", tree_entries)
        self.assertTrue(any(c.endswith("/compile_static_meta.json") for c in table_first_cells))
        for name in sorted(expected):
            with self.subTest(record=name):
                # `assertTrue`, not `assertIn`: the container is every entry in the document's
                # trees, and dumping it buries the one sentence that says what to do.
                self.assertTrue(
                    name in tree_entries,
                    f"docs/WORKSPACE_LAYOUT.md: no tree entry for `{name}`. It is a record "
                    f"`PURE_LEAF_META_FILES` declares, so the workspace map has to show where "
                    f"it lands.")
                self.assertTrue(
                    any(cell.endswith("/" + name) for cell in table_first_cells),
                    f"docs/WORKSPACE_LAYOUT.md: no phase-artifact row whose path ends in "
                    f"`{name}`. Naming it inside another row's prose does not count -- the "
                    f"table is where its writer and its readers are stated. The reader takes "
                    f"the row's FIRST cell and requires it to end in the basename, so a row "
                    f"written with the `<ir_ref>/` shorthand rather than this table's "
                    f"`workspace/`-rooted convention is refused here too.")

        # SHRINK half, AFTER the per-record loop above and not before it: a declared record
        # the document omits fails both, and the loop's message is the one that says what to
        # add. Ordering it first made growth report a bare set difference instead.
        # The second cell of every per-attempt meta row ends in a marker no other row in the table
        # carries, so it is the document's own statement of which rows claim to be per-attempt
        # records. Compared as a SET against the code. The rows said ``(`pure` only)`` when they
        # were added and ``(`pure` or `workshop`)`` after issue #534 step 1; since PR-J4-b every
        # new record is a `workshop leaf`'s, and only the current marker is accepted, so a row
        # reverted to an older one is red rather than silently re-claiming a pure writer.
        marker = "(`workshop`; `pure` on older records)"
        markers = (marker,)
        claimed = set()
        for line in doc.splitlines():
            stripped = line.strip()
            if not (stripped.startswith("| `workspace/") and any(m in stripped for m in markers)):
                continue
            cells = stripped.split(" | ")
            if len(cells) > 1 and cells[1].rstrip().endswith(markers):
                claimed.add(cells[0].removeprefix("| `").rstrip("`").rsplit("/", 1)[-1])
        self.assertEqual(
            claimed, expected,
            f"docs/WORKSPACE_LAYOUT.md: the rows marked {' or '.join(markers)} are not the records "
            f"`PURE_LEAF_META_FILES` declares. A name here that the code no longer declares is a "
            f"stale row a reader would look for on disk and not find; a declared name missing "
            f"here is covered by the per-record checks ABOVE, which say what to add. If a row's "
            f"phase cell was legitimately reworded away from {marker}, this marker is what "
            f"the check reads and it has to be restored or this comparison rewritten.")


if __name__ == "__main__":
    unittest.main()
