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
                              # NOT `/nonexistent`: a marker naming a checkout that does
                              # not exist reaches `refused:unverifiable_owner` through the
                              # STATUS lookup returning None, so the id comparison above
                              # it is never what decided — two paths to one outcome, and
                              # a reviewer's mutant deleting the comparison survived. The
                              # repo_root is filled in below with a REAL checkout whose
                              # metadata is terminal, so only the id mismatch can produce
                              # the verdict.
                              ("wrong_id", {"schema": 1, "orchestration_id": "someone_else",
                                            "repo_root": "<real>"}),
                              ("no_repo", {"schema": 1, "orchestration_id": "orch_no_repo"}),
                              ("blank_repo", {"schema": 1, "orchestration_id": "orch_blank_repo",
                                              "repo_root": "   "})):
            with self.subTest(marker=label):
                oid = f"orch_{label}"
                self._entry(oid, status="pass", marker=None)
                if marker is not None:
                    if marker.get("repo_root") == "<real>":
                        marker = {**marker, "repo_root": str((self.root / f"repo_{oid}").resolve())}
                    (self.homes / oid / WORKFLOW_HOME_OWNER_FILENAME).write_text(
                        json.dumps(marker), encoding="utf-8")
                reports, code = self._prune(orchestration_ids=[oid], delete=True)
                self.assertEqual(reports[0]["verdict"], pwh.REFUSED_UNVERIFIABLE_OWNER)
                self.assertTrue((self.homes / oid).is_dir())
                self.assertEqual(code, 2)
                reports, code = self._prune(orchestration_ids=[oid], delete=True,
                                            allow_unverifiable=True)
                self.assertTrue(reports[0]["deleted"])
                self.assertFalse((self.homes / oid).exists())
                self.assertEqual(code, 0)

    def test_an_entry_owned_by_another_user_is_refused(self) -> None:
        """The first refusal the module docstring names, and it had no witness at all.

        `os.getuid` is patched rather than a second user created, because a test cannot
        make one. Asserted together with the size and backend fields staying empty: the
        uid check returns before the walk, so a directory another user owns is not even
        enumerated.
        """
        self._entry("orch_theirs", status="pass")
        with mock.patch("os.getuid", return_value=os.getuid() + 1):
            reports, code = self._prune(orchestration_ids=["orch_theirs"], delete=True,
                                        allow_unverifiable=True)
        self.assertEqual(reports[0]["verdict"], pwh.REFUSED_FOREIGN_OWNER_UID)
        self.assertFalse(reports[0]["deleted"])
        self.assertEqual(reports[0]["backends"], [])
        self.assertEqual(code, 2)
        self.assertTrue((self.homes / "orch_theirs" / "claude").is_dir())

    def test_the_size_walk_does_not_follow_symlinks_out_of_the_entry(self) -> None:
        """`followlinks=False` is named in the docstring and was unwitnessed.

        A codex leaf's home is bound WRITABLE, so a leaf can put a symlink in it. Two
        consequences the flag prevents: a link to a large tree makes the report overstate
        what deleting the home reclaims, and a link that forms a cycle makes `os.walk`
        recurse until it gives up. Measured as a size comparison, which is the observable
        one.
        """
        self._entry("orch_link", status="pass")
        big = self.root / "big"
        big.mkdir()
        (big / "payload.bin").write_bytes(b"x" * 200_000)
        os.symlink(big, self.homes / "orch_link" / "claude" / "elsewhere")
        os.symlink(self.homes / "orch_link", self.homes / "orch_link" / "claude" / "loop")
        reports, _ = self._prune(orchestration_ids=["orch_link"])
        self.assertLess(reports[0]["size_bytes"], 10_000,
                        "the size walk followed a symlink out of the entry")
        self.assertEqual(reports[0]["verdict"], pwh.VERDICT_DELETABLE)

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

    def test_an_orchestration_id_carrying_a_separator_cannot_reach_a_live_home(self) -> None:
        """The no-override refusal must not be reachable around, only through.

        `--orchestration-id <oid>/<backend>` used to land INSIDE the homes root — so
        the containment assert in `_delete_entry` passed — while `inspect_entry` looked
        for `owner.json` one level below where it lives and answered
        `refused:unverifiable_owner`, which `--allow-unverifiable` releases. The result
        was that `refused:orchestration_not_terminal`, which this tool's docstring and
        `docs/RUNBOOK.md` both describe as having NO override, could be walked around
        with one slash, taking a RUNNING leaf's transcript with it.

        The honest spelling is asserted in the same test as the control: without it,
        "the separator form is refused" would also pass if the tool refused everything.
        """
        self._entry("orch_live", status="running")
        transcript = self.homes / "orch_live" / "claude" / "transcript.jsonl"
        self.assertTrue(transcript.is_file())
        for name in ("orch_live/claude", "orch_live/", "./orch_live", "orch_live/../orch_live"):
            with self.subTest(orchestration_id=name):
                reports, code = self._prune(orchestration_ids=[name], delete=True,
                                            allow_unverifiable=True)
                self.assertEqual(reports[0]["verdict"], pwh.REFUSED_INVALID_ORCHESTRATION_ID)
                self.assertFalse(reports[0]["deleted"])
                self.assertEqual(code, 2)
                self.assertTrue(transcript.is_file(),
                                f"a live leaf's transcript was destroyed via {name!r}")
        # CONTROL — the honest spelling reaches the entry and is refused on its merits.
        reports, code = self._prune(orchestration_ids=["orch_live"], delete=True,
                                    allow_unverifiable=True)
        self.assertEqual(reports[0]["verdict"], pwh.REFUSED_NOT_TERMINAL)
        self.assertTrue(transcript.is_file())

    def test_a_delete_that_fails_is_not_reported_as_work_done(self) -> None:
        """`refused:delete_failed:*` on an explicitly named entry must exit 2.

        The exit code was decided from whether the entry was ALLOWED to be deleted, not
        from whether it WAS, so a delete that raised exited 0 — against the docstring's
        "0 = the requested work was done". A caller scripting the tool would read that
        as success.
        """
        self._entry("orch_a", status="pass")
        with mock.patch.object(pwh, "_delete_entry",
                               side_effect=OSError("Device or resource busy")):
            reports, code = self._prune(orchestration_ids=["orch_a"], delete=True)
        self.assertTrue(reports[0]["verdict"].startswith("refused:delete_failed:"))
        self.assertFalse(reports[0]["deleted"])
        self.assertEqual(code, 2)
        self.assertTrue((self.homes / "orch_a").is_dir())

    def test_a_report_only_run_writes_nothing_into_the_owner_checkout(self) -> None:
        """"Report only" has to mean it, including the lock the status read wanted.

        `_orchestration_meta_exclusive_lock` creates its lock file and `mkdir`s the
        directory tree to hold it, so taking the lock before knowing the metadata was
        there made a plain `--all` write
        `workspace/orchestrations/<oid>/orchestration_meta.json.lock` into whatever path
        the owner marker named — which, after the checkout was moved or deleted, is an
        unrelated project. Asserted as a full before/after tree comparison rather than
        on the lock file's name, so any other write is caught too.
        """
        entry = self._entry("orch_gone", status=None)
        elsewhere = self.root / "repo_orch_gone"
        elsewhere.mkdir()
        (elsewhere / "README.md").write_text("someone else's project\n", encoding="utf-8")
        before = sorted(p.relative_to(elsewhere).as_posix() for p in elsewhere.rglob("*"))
        reports, _ = self._prune(orchestration_ids=["orch_gone"])
        after = sorted(p.relative_to(elsewhere).as_posix() for p in elsewhere.rglob("*"))
        self.assertEqual(reports[0]["verdict"], pwh.REFUSED_UNVERIFIABLE_OWNER)
        self.assertEqual(before, after,
                         "a report-only run created files in the owner checkout")
        self.assertTrue(entry.is_dir())

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

    # ONE definition, read by the census and by its control alike. The first version
    # spelled the patterns twice — once in the census, once copied into the control —
    # so the control could not see a gap in the census or an edit to it, and its copy
    # had already silently dropped one of the four. A test placed outside the thing that
    # defines the set cannot make a claim about the set.
    CALLER_PATTERNS = (
        # `^\s*` and not `^`: this repository's norm is importing `tools.*` INSIDE a
        # function (`workflow_conductor.py` imports both home preparers that way), and
        # anchoring at column 0 missed every such caller — measured against planted ones.
        r"^\s*import prune_workflow_homes",
        r"^\s*from tools\.prune_workflow_homes import",
        r"^\s*from prune_workflow_homes import",
        r"prune_workflow_homes\.(main|prune|inspect_entry)\(",
        # An argv element rather than a sentence: quoted, and inside a list.
        r"[\"'](python3 )?tools/prune_workflow_homes\.py[\"']",
    )

    def test_nothing_is_wired_to_invoke_this_tool_automatically(self) -> None:
        """Retention is manual BY DESIGN, and the design is only real if nothing calls it.

        `TODO.md` records the same decision for `workspace/`: an automatic rule has to
        choose between deleting evidence someone may still audit and keeping everything,
        and only the second cannot silently destroy what the directory exists for.

        What is searched for is a CALLER — an import of the module or an argv naming the
        script — and not a mention. Mentions are the point: the refusal message names the
        remedy, and several documents explain when to run it. An earlier version
        allowlisted mention SITES, and every prose edit in the same change failed it,
        which trains the reader to extend the list rather than to read the diff.

        SAMPLED, not pinned. A caller that assembles the name at runtime — an importlib
        string, a subprocess argv built from parts — is out of reach of any static
        reader. This bounds the growth of the direct spellings, which is the shape a
        later change would actually take.
        """
        import subprocess
        repo_root = Path(__file__).resolve().parents[2]
        offenders: dict[str, list[str]] = {}
        for pattern in self.CALLER_PATTERNS:
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

        Reads `CALLER_PATTERNS` — it does not copy them — so it also fails if a later
        edit narrows the census. Both directions are checked, because the census has
        failed in both: it missed function-local imports (this repository's own style),
        and an earlier version reported every document that merely names the script.
        """
        import re
        must_catch = (
            "import prune_workflow_homes",
            "    import prune_workflow_homes",                       # function-local
            "from tools.prune_workflow_homes import prune",
            "        from tools.prune_workflow_homes import prune",  # function-local
            "        rc = prune_workflow_homes.main([])",
            "    subprocess.run(['python3', 'tools/prune_workflow_homes.py'])",
        )
        must_not_catch = (
            "# remove it with `python3 tools/prune_workflow_homes.py --delete`",
            "run tools/prune_workflow_homes.py to remove one",
            '            "recording it. Inspect it, then remove it with `python3 "',
            "  See tools/prune_workflow_homes.py for the retention policy.",
        )
        for spelling in must_catch:
            with self.subTest(caller=spelling):
                self.assertTrue(
                    any(re.search(p, spelling) for p in self.CALLER_PATTERNS),
                    f"the census patterns would not catch: {spelling}")
        for prose in must_not_catch:
            with self.subTest(prose=prose):
                self.assertFalse(
                    any(re.search(p, prose) for p in self.CALLER_PATTERNS),
                    f"prose must not read as a caller: {prose}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
