#!/usr/bin/env python3
"""`tools/prune_workflow_homes.py` — the ONLY thing that removes a durable home.

Since issue #64 the isolated backend homes are kept indefinitely, so every deletion
goes through this tool and every refusal it makes is the last thing between an
operator's typo and the only record of what a leaf did. The tests are organised around
the three independent refusals rather than around the CLI surface: what the entry IS
(a real directory this user owns), who OWNS it (the marker, and the checkout it names),
and whether the run is OVER (the status, read under the metadata lock).
"""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest import mock

from tools import prune_workflow_homes as pwh
from tools.orchestration_runtime import WORKFLOW_HOME_OWNER_FILENAME


class PruneWorkflowHomesTests(unittest.TestCase):

    def setUp(self) -> None:
        import tempfile
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.homes = self.root / "homes"
        self.homes.mkdir(mode=0o700)

    def _entry(self, oid: str, *, status: str | None = "pass",
               marker: dict | None | str = "auto", backends=("claude",)) -> Path:
        """One `<homes-root>/<oid>/` with its backends, marker, and owner checkout."""
        entry = self.homes / oid
        entry.mkdir(mode=0o700)
        for backend in backends:
            (entry / backend).mkdir(mode=0o700)
            (entry / backend / "transcript.jsonl").write_text("{}\n", encoding="utf-8")
        repo = self.root / f"repo_{oid}"
        if status is not None:
            meta_dir = repo / "workspace" / "orchestrations" / oid
            meta_dir.mkdir(parents=True)
            (meta_dir / "orchestration_meta.json").write_text(
                json.dumps({"orchestration_id": oid, "status": status}), encoding="utf-8")
        if marker == "auto":
            marker = {"schema": 1, "orchestration_id": oid,
                      "repo_root": str(repo.resolve()), "created_at": "2026-08-20T00:00:00Z"}
        if marker is not None:
            (entry / WORKFLOW_HOME_OWNER_FILENAME).write_text(
                json.dumps(marker), encoding="utf-8")
        return entry

    def _prune(self, **kwargs):
        kwargs.setdefault("orchestration_ids", None)
        kwargs.setdefault("delete", False)
        kwargs.setdefault("allow_unverifiable", False)
        return pwh.prune(self.homes, **kwargs)

    def test_the_default_is_a_report_that_deletes_nothing(self) -> None:
        """`--delete` is an explicit choice, and the verdict alone must not act.

        The failure this pins is the one that cannot be undone: an operator running the
        tool to SEE what is there and losing it instead.
        """
        entry = self._entry("orch_a", status="pass")
        reports, code = self._prune()
        self.assertEqual(code, 0)
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["verdict"], pwh.VERDICT_DELETABLE)
        self.assertFalse(reports[0]["deleted"])
        self.assertTrue((entry / "claude" / "transcript.jsonl").is_file())

    def test_a_terminal_orchestration_is_deleted_and_its_neighbours_are_not(self) -> None:
        """Deletion is per entry. One running run in the tree must not stop the sweep,
        and one terminal run must not take the tree with it."""
        gone = self._entry("orch_done", status="pass")
        kept = self._entry("orch_live", status="running")
        reports, code = self._prune(orchestration_ids=None, delete=True)
        self.assertEqual(code, 0)
        by_id = {r["orchestration_id"]: r for r in reports}
        self.assertTrue(by_id["orch_done"]["deleted"])
        self.assertFalse(gone.exists())
        self.assertFalse(by_id["orch_live"]["deleted"])
        self.assertEqual(by_id["orch_live"]["verdict"], pwh.REFUSED_NOT_TERMINAL)
        self.assertTrue((kept / "claude" / "transcript.jsonl").is_file())

    def test_every_terminal_status_the_runtime_knows_is_deletable(self) -> None:
        """Read from `IDEMPOTENT_TERMINAL_STATUSES`, not respelled.

        `fail_closed` is the one that would be missed by a hand-written list, and a
        status added to the runtime later would otherwise become quietly undeletable —
        the operator's only recourse then being `--allow-unverifiable` on a home whose
        owner is in fact perfectly verifiable.
        """
        for status in sorted(pwh.DELETABLE_STATUSES):
            with self.subTest(status=status):
                self._entry(f"orch_{status}", status=status)
        self.assertIn("fail_closed", pwh.DELETABLE_STATUSES)
        reports, _ = self._prune()
        self.assertTrue(reports)
        for report in reports:
            self.assertEqual(report["verdict"], pwh.VERDICT_DELETABLE,
                             msg=report["orchestration_id"])

    def test_a_running_orchestration_cannot_be_forced(self) -> None:
        """`--allow-unverifiable` releases the OWNER refusal only.

        Deleting a home under a live leaf takes that leaf's session with it, so there
        is deliberately no flag for this one. A single override that released both
        would read to an operator as one escape hatch.
        """
        self._entry("orch_live", status="running")
        reports, code = self._prune(orchestration_ids=["orch_live"], delete=True,
                                    allow_unverifiable=True)
        self.assertEqual(reports[0]["verdict"], pwh.REFUSED_NOT_TERMINAL)
        self.assertFalse(reports[0]["deleted"])
        self.assertTrue((self.homes / "orch_live").is_dir())
        self.assertEqual(code, 2, "an explicitly named entry that was refused exits 2")

    def test_a_missing_or_mismatched_marker_is_refused_until_the_flag_says_otherwise(self) -> None:
        """No marker means "unknown owner", which is not the same as "no owner".

        A home with no `owner.json` may belong to a LIVE run in a checkout this
        invocation cannot see — the marker is written under the metadata lock right
        after the directory is created, so the window in which a live home lacks one is
        small but real. Fail closed by default; let the operator say they know better.
        """
        for label, marker in (("missing", None),
                              ("wrong_id", {"schema": 1, "orchestration_id": "someone_else",
                                            "repo_root": "/nonexistent"}),
                              ("no_repo", {"schema": 1, "orchestration_id": "orch_x"})):
            with self.subTest(marker=label):
                oid = f"orch_{label}"
                self._entry(oid, status="pass", marker=marker)
                reports, code = self._prune(orchestration_ids=[oid], delete=True)
                self.assertEqual(reports[0]["verdict"], pwh.REFUSED_UNVERIFIABLE_OWNER)
                self.assertTrue((self.homes / oid).is_dir())
                self.assertEqual(code, 2)
                reports, code = self._prune(orchestration_ids=[oid], delete=True,
                                            allow_unverifiable=True)
                self.assertTrue(reports[0]["deleted"])
                self.assertFalse((self.homes / oid).exists())
                self.assertEqual(code, 0)

    def test_an_owner_checkout_that_no_longer_exists_is_unverifiable_not_terminal(self) -> None:
        """The marker LOCATED nothing, so "did the run finish" is unanswerable.

        Classifying this as not-terminal would be worse than wrong: it is the one
        refusal with no override, so an operator whose checkout was moved could never
        reclaim the space. Classifying it as terminal would delete the home of a run
        that may still be going in a checkout that merely moved.
        """
        self._entry("orch_nocheckout", status=None)
        reports, _ = self._prune(orchestration_ids=["orch_nocheckout"])
        self.assertEqual(reports[0]["verdict"], pwh.REFUSED_UNVERIFIABLE_OWNER)
        self.assertEqual(reports[0]["status"], "")

    def test_a_symlinked_entry_is_refused_and_its_target_survives(self) -> None:
        """`rmtree` through a symlink removes whatever is on the other side."""
        elsewhere = self.root / "precious"
        elsewhere.mkdir()
        (elsewhere / "keep.txt").write_text("keep me", encoding="utf-8")
        os.symlink(elsewhere, self.homes / "orch_link")
        reports, code = self._prune(orchestration_ids=["orch_link"], delete=True,
                                    allow_unverifiable=True)
        self.assertEqual(reports[0]["verdict"], pwh.REFUSED_NOT_A_DIRECTORY)
        self.assertEqual(code, 2)
        self.assertEqual((elsewhere / "keep.txt").read_text(encoding="utf-8"), "keep me")

    def test_a_path_that_escapes_the_homes_root_is_refused_at_the_last_step(self) -> None:
        """The containment assert answers the one question the content checks cannot.

        Every other refusal is about what the directory holds. This one is about where
        the path landed, and it is what a crafted id or a swapped symlink attacks, so
        it runs immediately before `rmtree` rather than at parse time.
        """
        outside = self.root / "outside"
        outside.mkdir()
        with self.assertRaisesRegex(ValueError, "outside the homes root"):
            pwh._delete_entry(outside, self.homes)
        self.assertTrue(outside.is_dir())
        with self.assertRaisesRegex(ValueError, "outside the homes root"):
            pwh._delete_entry(self.homes, self.homes)
        self.assertTrue(self.homes.is_dir())

    def test_the_status_is_read_under_the_orchestration_metadata_lock(self) -> None:
        """A concurrent `--resume` must not be observed mid-transition.

        `update_orchestration_status` writes the terminal status inside this lock, so
        an unlocked read can land between that write and what follows it. Asserted by
        patching the lock rather than by racing it: a timing test would be a flake, and
        what has to hold is that the call happens at all.
        """
        self._entry("orch_a", status="pass")
        seen: list[tuple] = []
        real = pwh._orchestration_meta_exclusive_lock

        def _spy(repo_root, orchestration_id):
            seen.append((Path(repo_root).name, orchestration_id))
            return real(repo_root, orchestration_id)

        with mock.patch.object(pwh, "_orchestration_meta_exclusive_lock", _spy):
            reports, _ = self._prune(orchestration_ids=["orch_a"])
        self.assertEqual(reports[0]["verdict"], pwh.VERDICT_DELETABLE)
        self.assertEqual(seen, [("repo_orch_a", "orch_a")])

    def test_the_report_names_the_owner_the_backends_and_the_size(self) -> None:
        """A report-only run has to be enough to decide on, or `--delete` gets guessed."""
        self._entry("orch_a", status="fail", backends=("claude", "codex"))
        reports, _ = self._prune(orchestration_ids=["orch_a"])
        report = reports[0]
        self.assertEqual(report["backends"], ["claude", "codex"])
        self.assertEqual(report["status"], "fail")
        self.assertEqual(Path(report["owner_repo_root"]).name, "repo_orch_a")
        self.assertGreater(report["size_bytes"], 0)
        text = pwh._render_text(reports, delete=False, homes_root=self.homes)
        self.assertIn("orch_a", text)
        self.assertIn("claude,codex", text)
        self.assertIn("would delete", text)
        self.assertIn("--delete", text)
        # Scaled, not always-MB: this fixture is a few bytes, and "0.0 MB" reads as
        # "empty, deleting it costs nothing" — the opposite of what the report is for.
        self.assertNotIn("0.0 MB", text)
        self.assertRegex(text, r"\d+ B|\d+\.\d KB|\d+\.\d MB")

    def test_no_scope_is_a_usage_error_rather_than_a_default(self) -> None:
        """There is no implicit "everything": the destructive default is the one
        mistake this tool must not make available."""
        with self.assertRaises(SystemExit) as ctx:
            pwh.main(["--homes-root", str(self.homes), "--delete"])
        self.assertEqual(ctx.exception.code, 2)
        with self.assertRaises(SystemExit):
            pwh.main(["--homes-root", str(self.homes), "--all", "--orchestration-id", "x"])

    def test_the_cli_reports_json_and_returns_the_refusal_exit_code(self) -> None:
        import io
        from contextlib import redirect_stdout

        self._entry("orch_live", status="running")
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = pwh.main(["--homes-root", str(self.homes),
                             "--orchestration-id", "orch_live", "--delete", "--json"])
        payload = json.loads(buf.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(payload["homes_root"], str(self.homes))
        self.assertEqual(payload["entries"][0]["verdict"], pwh.REFUSED_NOT_TERMINAL)
        self.assertTrue((self.homes / "orch_live").is_dir())

    def test_nothing_is_wired_to_invoke_this_tool_automatically(self) -> None:
        """Retention is manual BY DESIGN, and the design is only real if nothing calls it.

        `TODO.md` records the same decision for `workspace/`: an automatic rule has to
        choose between deleting evidence someone may still audit and keeping everything,
        and only the second cannot silently destroy what the directory exists for.

        What is searched for is a CALLER — an import of the module or an argv naming the
        script — and not a mention. Mentions are the point: the refusal message names the
        remedy, and four documents explain when to run it. An earlier version of this
        test allowlisted mention SITES, and every prose edit in the same change failed it,
        which trains the reader to extend the list rather than to read the diff.

        SAMPLED, not pinned. A caller that assembles the name at runtime — an importlib
        string, a subprocess argv built from parts — is out of reach of any static
        reader. This bounds the growth of the direct spellings, which is the shape a
        later change would actually take.
        """
        import subprocess
        repo_root = Path(__file__).resolve().parents[2]
        # PYTHON FILES ONLY, and patterns that are Python CODE. Prose naming the script
        # is what the documents are supposed to do, and a search that cannot tell prose
        # from a call reports the documents — which is what the first version of this
        # test did, failing on every prose edit until its allowlist was extended.
        patterns = [
            r"^import prune_workflow_homes",
            r"^from tools\.prune_workflow_homes import",
            r"^from prune_workflow_homes import",
            r"prune_workflow_homes\.(main|prune|inspect_entry)\(",
            # An argv element rather than a sentence: quoted, and inside a list.
            r"[\"'](python3 )?tools/prune_workflow_homes\.py[\"']",
        ]
        offenders: dict[str, list[str]] = {}
        for pattern in patterns:
            out = subprocess.run(
                ["git", "-C", str(repo_root), "grep", "-n", "--untracked", "-E", pattern,
                 "--", "tools/*.py", "tools/**/*.py", "mcp_servers/*.py",
                 "skills/**/*.py", "leaf_config/**/*.py"],
                capture_output=True, text=True, check=False).stdout
            for line in out.splitlines():
                path = line.split(":", 1)[0]
                if path == "tools/tests/test_prune_workflow_homes.py":
                    continue  # this file, and the import at its top
                offenders.setdefault(path, []).append(line.split(":", 2)[-1].strip())
        self.assertEqual(offenders, {},
                         "something now invokes the prune tool; retention has stopped "
                         "being manual")

    def test_the_search_this_census_uses_can_actually_find_a_caller(self) -> None:
        """The control for the test above: a census that finds nothing proves nothing.

        Written as a positive probe against a real caller rather than as a mutation of
        the census, because "the grep returned empty" and "there is no caller" are
        indistinguishable from the passing side.
        """
        import re
        for spelling in ("import prune_workflow_homes",
                         "from tools.prune_workflow_homes import prune",
                         "        rc = prune_workflow_homes.main([])",
                         "    subprocess.run(['python3', 'tools/prune_workflow_homes.py'])"):
            with self.subTest(spelling=spelling):
                self.assertTrue(
                    any(re.search(p, spelling) for p in (
                        r"^import prune_workflow_homes",
                        r"^from tools\.prune_workflow_homes import",
                        r"prune_workflow_homes\.(main|prune|inspect_entry)\(",
                        r"[\"'](python3 )?tools/prune_workflow_homes\.py[\"']")),
                    f"the census patterns would not catch: {spelling}")
        # ...and the CONTROL in the other direction: prose must NOT be caught, which is
        # the failure the first version of this test actually had.
        for prose in ("# remove it with `python3 tools/prune_workflow_homes.py --delete`",
                      "run tools/prune_workflow_homes.py to remove one"):
            with self.subTest(prose=prose):
                self.assertFalse(
                    any(re.search(p, prose) for p in (
                        r"^import prune_workflow_homes",
                        r"^from tools\.prune_workflow_homes import",
                        r"prune_workflow_homes\.(main|prune|inspect_entry)\(")),
                    f"prose must not read as a caller: {prose}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
