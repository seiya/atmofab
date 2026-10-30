"""Witness `skills/workflow-timing-audit/scripts/analyze_timing.py`'s transcript resolution.

The script reports a leaf as LLM only when it finds that leaf's transcript, so WHERE it looks
decides the run's LLM time and token totals. Until issue #565 it searched the orchestration's
pre-Z4 private home and the operator's `~/.claude/projects/<repo-slug>` alone, and a claude
`workshop leaf` writes under neither: every workshop leaf of a current run read as non-LLM
(measured on `orch_20261029T172930Z_fe1f4c26`: 0 of its 5 claude workshop launches). The rows
below drive `main()` with `--json` over a synthetic orchestration and read its per-leaf list:
a claude workshop row is read from its lineage's config home and from nowhere else, a codex or
HTTP workshop row from nowhere, a row whose record names no workshop from the operator pair, and
`--project-dir` overrides every row.
"""
import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "skills" / "workflow-timing-audit" / "scripts" / "analyze_timing.py"
sys.path.insert(0, str(_SCRIPT.parent))
import analyze_timing  # noqa: E402

from tools.operator_private_root import WORKFLOW_HOMES_ROOT_ENV  # noqa: E402

OID = "orch_20261030T000000Z_test0001"
CLAUDE_RID = "11111111-0000-4000-8000-000000000001"
PURE_RID = "22222222-0000-4000-8000-000000000002"
CODEX_RID = "33333333-0000-4000-8000-000000000003"
HTTP_RID = "44444444-0000-4000-8000-000000000004"
LINEAGE = "aaaaaaaa-0000-4000-8000-00000000000a"


def _workshop(provider):
    return {"leaf_mode": "workshop", "workshop_provider": provider,
            "workshop_lineage_id": LINEAGE}


def _transcript(path, rid, out_tokens):
    """One model response whose `output_tokens` says which file it was read from."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = {"type": "assistant", "timestamp": "2026-10-30T00:00:01.000Z",
            "message": {"id": f"msg-{rid}-{out_tokens}", "role": "assistant",
                        "content": [{"type": "text", "text": "x"}],
                        "usage": {"output_tokens": out_tokens}}}
    path.write_text(json.dumps(line) + "\n", encoding="utf-8")


class TranscriptResolutionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = pathlib.Path(os.path.realpath(tmp.name))
        self.repo = base / "checkout"
        (self.repo / ".git").mkdir(parents=True)
        self.orch = self.repo / "workspace" / "orchestrations" / OID
        (self.orch / "launches").mkdir(parents=True)
        self.home = base / "home"
        self.homes = base / "homes"
        self.slug = str(self.repo).replace("/", "-")
        self.operator = self.home / ".claude" / "projects" / self.slug
        self.records = {CLAUDE_RID: _workshop("claude_cli"), PURE_RID: {"substep": "static"},
                        CODEX_RID: _workshop("codex_cli"), HTTP_RID: _workshop("anthropic_api")}
        log = []
        for i, rid in enumerate(self.records):
            log.append({"event": "child_launched", "agent_run_id": rid, "step": "compile",
                        "ts": f"2026-10-30T00:0{i}:00.000000Z"})
            log.append({"event": "record_agent_run_terminal", "agent_run_id": rid,
                        "ts": f"2026-10-30T00:0{i}:30.000000Z"})
        (self.orch / "phase_state_log.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in log), encoding="utf-8")
        for rid, record in self.records.items():
            (self.orch / "launches" / f"{rid}.request.json").write_text(
                json.dumps(record), encoding="utf-8")

    def _lineage(self, backend="claude"):
        return self.homes / OID / backend / LINEAGE / "config" / "projects"

    def _run(self, *extra):
        out = io.StringIO()
        cwd = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, cwd)
        env = {"HOME": str(self.home), WORKFLOW_HOMES_ROOT_ENV: str(self.homes)}
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(sys, "argv", ["analyze_timing.py", OID, "--json", *extra]), \
                contextlib.redirect_stdout(out):
            analyze_timing.main()
        os.chdir(cwd)
        leaves = json.loads(out.getvalue())["leaves"]
        return {leaf["run_id"]: (leaf["transcript"]["out_tokens"] if leaf["transcript"]
                                 else None) for leaf in leaves}

    def test_i_a_claude_workshop_row_is_read_from_its_lineage_home(self):
        _transcript(self._lineage() / "-any-tree-slug" / f"{CLAUDE_RID}.jsonl", CLAUDE_RID, 101)
        self.assertEqual(self._run()[CLAUDE_RID], 101)

    def test_ii_a_claude_workshop_row_is_not_read_from_the_operator_home(self):
        """One root, on this reader too (operator decision D1)."""
        _transcript(self.operator / f"{CLAUDE_RID}.jsonl", CLAUDE_RID, 102)
        self.assertIsNone(self._run()[CLAUDE_RID])

    def test_iii_a_row_without_a_workshop_is_read_from_the_operator_home(self):
        _transcript(self.operator / f"{PURE_RID}.jsonl", PURE_RID, 103)
        # A decoy in a lineage home: a row whose record names no workshop never goes there.
        _transcript(self._lineage() / "-slug" / f"{PURE_RID}.jsonl", PURE_RID, 999)
        self.assertEqual(self._run()[PURE_RID], 103)

    def test_iv_codex_and_http_workshop_rows_have_no_transcript(self):
        """Decoys in every place a wrong branch could look: the claude lineage home (whose
        path the record's lineage id would spell), the codex lineage home, and the operator
        pair. None of them may answer."""
        for rid in (CODEX_RID, HTTP_RID):
            _transcript(self._lineage() / "-slug" / f"{rid}.jsonl", rid, 201)
            _transcript(self._lineage("codex") / "-slug" / f"{rid}.jsonl", rid, 202)
            _transcript(self.operator / f"{rid}.jsonl", rid, 203)
        got = self._run()
        self.assertIsNone(got[CODEX_RID])
        self.assertIsNone(got[HTTP_RID])

    def test_v_project_dir_overrides_every_row(self):
        explicit = self.repo.parent / "explicit"
        for n, rid in enumerate(self.records):
            _transcript(explicit / f"{rid}.jsonl", rid, 300 + n)
            # And a competing file where the default resolution would look.
            _transcript(self._lineage() / "-slug" / f"{rid}.jsonl", rid, 400 + n)
            _transcript(self.operator / f"{rid}.jsonl", rid, 500 + n)
        got = self._run("--project-dir", str(explicit))
        self.assertEqual(got, {rid: 300 + n for n, rid in enumerate(self.records)})


if __name__ == "__main__":
    unittest.main()
