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
import subprocess
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

    def _report(self, *extra):
        """The script's whole `--json` report over the synthetic orchestration."""
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
        return json.loads(out.getvalue())

    def _run(self, *extra):
        """Per run id: the `output_tokens` of the transcript the row was read from, or None."""
        return {leaf["run_id"]: (leaf["transcript"]["out_tokens"] if leaf["transcript"]
                                 else None) for leaf in self._report(*extra)["leaves"]}

    def _record(self, rid, body):
        (self.orch / "launches" / f"{rid}.request.json").write_text(body, encoding="utf-8")

    def test_i_a_claude_workshop_row_is_read_from_its_lineage_home(self):
        _transcript(self._lineage() / "-any-tree-slug" / f"{CLAUDE_RID}.jsonl", CLAUDE_RID, 101)
        report = self._report()
        got = {leaf["run_id"]: (leaf["transcript"]["out_tokens"] if leaf["transcript"]
                                else None) for leaf in report["leaves"]}
        self.assertEqual(got[CLAUDE_RID], 101)
        # The report says which lineage homes its figures were read from: the pair first,
        # then every lineage root a workshop row answered from (round 0 of issue #565: this
        # output line was the one unpinned hunk of the branch).
        self.assertEqual(report["project_dirs"],
                         [str(self.operator), str(self._lineage())])

    def test_a_workshop_row_without_a_provider_key_is_read_as_claude(self):
        """A record written before `026de9d4` (issue #534 step 2 PR-1) has no
        `workshop_provider` key and is a claude launch by construction; round 1 of issue #565
        found 9 such records on the operator's machine, every one reported as non-LLM with
        its transcript on disk. A key PRESENT with another value is driven beside it."""
        self._record(CLAUDE_RID, json.dumps({"leaf_mode": "workshop",
                                             "workshop_lineage_id": LINEAGE}))
        _transcript(self._lineage() / "-slug" / f"{CLAUDE_RID}.jsonl", CLAUDE_RID, 111)
        _transcript(self.operator / f"{CLAUDE_RID}.jsonl", CLAUDE_RID, 999)
        self.assertEqual(self._run()[CLAUDE_RID], 111)
        self._record(CLAUDE_RID, json.dumps({"leaf_mode": "workshop",
                                             "workshop_provider": None,
                                             "workshop_lineage_id": LINEAGE}))
        self.assertIsNone(self._run()[CLAUDE_RID])

    def test_a_claude_workshop_row_with_no_usable_lineage_id_is_read_from_nowhere(self):
        """The resolver answers the OPERATOR root for a `None` lineage id, so the script must
        not ask it (round 1 of issue #565, `route not established`): with the id absent or
        outside the grammar the row is None, whatever the operator home holds."""
        _transcript(self.operator / f"{CLAUDE_RID}.jsonl", CLAUDE_RID, 999)
        _transcript(self._lineage() / "-slug" / f"{CLAUDE_RID}.jsonl", CLAUDE_RID, 998)
        for lineage_id in (None, "../x", ""):
            with self.subTest(lineage_id=lineage_id):
                self._record(CLAUDE_RID, json.dumps({"leaf_mode": "workshop",
                                                     "workshop_provider": "claude_cli",
                                                     "workshop_lineage_id": lineage_id}))
                self.assertIsNone(self._run()[CLAUDE_RID])

    def test_an_unreadable_record_is_read_from_nowhere(self):
        """As `tools/orchestration_diagnostics.py` treats it: a record that exists but is not
        a JSON object selects no root, because the pair would answer with a pure leaf's file
        for what may be a workshop launch. Decoys in both places a wrong branch would look."""
        _transcript(self.operator / f"{CLAUDE_RID}.jsonl", CLAUDE_RID, 999)
        _transcript(self._lineage() / "-slug" / f"{CLAUDE_RID}.jsonl", CLAUDE_RID, 998)
        for body in ("{not json", "[1, 2]"):
            with self.subTest(body=body):
                self._record(CLAUDE_RID, body)
                self.assertIsNone(self._run()[CLAUDE_RID])

    def test_the_pair_is_the_private_home_first_then_the_operator_home(self):
        """The two members of the pre-#565 pair, and their order, which the branch rewrote
        into a per-run-id lookup: a run resumed across the issue-#63 migration has leaves in
        both, so BOTH must be searched (round 1 of issue #565: `project_dirs[:1]` survived),
        and the private home (`orchestration_meta.json#claude_workflow_home`) answers first."""
        private = self.homes / OID / "claude"
        (self.orch / "orchestration_meta.json").write_text(
            json.dumps({"claude_workflow_home": str(private)}), encoding="utf-8")
        private_projects = private / "projects" / self.slug
        private_projects.mkdir(parents=True)
        _transcript(private_projects / f"{PURE_RID}.jsonl", PURE_RID, 121)
        _transcript(self.operator / f"{PURE_RID}.jsonl", PURE_RID, 122)
        self.assertEqual(self._run()[PURE_RID], 121)
        (private_projects / f"{PURE_RID}.jsonl").unlink()
        self.assertEqual(self._run()[PURE_RID], 122)

    def test_the_script_runs_through_a_symlink(self):
        """The checkout root is taken from the script's REAL path, so an operator's symlink to
        it still imports `tools/` (round 1 of issue #565: `abspath` kept the symlink's own
        directory and the import failed). Driven as a subprocess from outside the checkout,
        where the only failure the script may report is the orchestration lookup."""
        link = self.repo.parent / "analyze_timing_link.py"
        link.symlink_to(_SCRIPT)
        proc = subprocess.run([sys.executable, str(link), "orch_nonexistent"],
                              cwd=str(self.repo.parent), capture_output=True, text=True,
                              timeout=60, env={**os.environ, "HOME": str(self.home)})
        self.assertNotIn("ModuleNotFoundError", proc.stderr)
        self.assertNotEqual(proc.returncode, 0)

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
