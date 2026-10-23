"""Tests for tools/workshop_tree.py and the host-side half of tools/workshop_wrapper.py.

The work tree of a `workshop leaf` lineage (issue #534 PR-1): seeding and the reserved names,
the lineage record and its two bounds, the assembly of `bundle.yaml` into a bundle document
(containment, symlinks, missing / directory / non-UTF-8 targets — each a finding, never a raise),
the snapshot (no seed copied back, no link followed) and the cold-reuse seed. The wrapper's runs
under a real sandbox are `test_bwrap_simulation.WorkshopWrapperSandboxTests`.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from tools import workshop_tree as wt
from tools import workshop_wrapper as ww

_SEEDS = {
    "spec.ir.yaml": "meta: {}\n",
    "tests.md": "# tests\n",
    "CONTRACT.md": "contract\n",
    "x_runner.f90": "program r\nend program r\n",
}
_CONTROL = "Makefile"


class _LineageCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.paths = wt.LineagePaths(
            homes_root=base / "homes", orchestration_id="orch_1", lineage_id="arid-1",
            orchestration_dir=base / "repo" / "workspace" / "orchestrations" / "orch_1")
        self.paths.home.mkdir(parents=True, mode=0o700)
        self.paths.make_work_dirs()
        self.seed_names = wt.seed_tree(self.paths, _SEEDS)
        self.reserved = wt.reserved_tree_names(self.seed_names, _CONTROL)
        wt.write_lineage_record(self.paths, node_key="component/x@0.1.0",
                              started_at_utc="2026-10-23T00:00:00Z")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, rel: str, text: str | bytes) -> Path:
        path = self.paths.tree / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text, encoding="utf-8")
        return path

    def manifest(self, *logical: str, extra: str = "") -> None:
        files = "".join(f"  - logical_path: {p}\n    role: model\n" for p in logical)
        self.write(wt.BUNDLE_MANIFEST_NAME, f"schema_version: 1.2.0\nfiles:\n{files}{extra}")


class LineagePathsTests(unittest.TestCase):
    def test_paths_are_under_the_home_and_the_record_dir(self) -> None:
        p = wt.LineagePaths(Path("/h"), "orch_1", "arid-1", Path("/r/ws/orch_1"))
        self.assertEqual(p.home, Path("/h/orch_1/claude/arid-1"))
        self.assertEqual(
            [p.config, p.tree, p.staged_obj, p.staged_bin, p.syntax, p.snapshot],
            [p.home / n for n in ("config", "tree", "staged/obj", "staged/bin", "syntax",
                                  "snapshot")])
        self.assertEqual(p.record_json, Path("/r/ws/orch_1/workshop/arid-1/workshop_lineage.json"))
        self.assertEqual(p.command_log.parent, p.wrapper_calls.parent)

    def test_an_id_that_is_not_a_plain_token_is_refused(self) -> None:
        for bad in ("../x", "a/b", "", "a b"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    wt.LineagePaths(Path("/h"), "orch_1", bad, Path("/r"))

    def test_work_dirs_need_an_existing_home(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            p = wt.LineagePaths(Path(t), "o", "l", Path(t) / "rec")
            with self.assertRaisesRegex(ValueError, "does not exist"):
                p.make_work_dirs()


class SeedTests(_LineageCase):
    def test_seeds_land_and_their_names_are_reserved(self) -> None:
        self.assertEqual(self.seed_names, tuple(sorted(_SEEDS)))
        self.assertEqual((self.paths.tree / "tests.md").read_text(), "# tests\n")
        self.assertEqual(self.reserved,
                         frozenset(_SEEDS) | {_CONTROL, wt.BUNDLE_MANIFEST_NAME})

    def test_a_tree_that_is_not_empty_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "not empty"):
            wt.seed_tree(self.paths, {"other.md": "x"})

    def test_a_seed_name_that_escapes_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            p = wt.LineagePaths(Path(t), "o", "l", Path(t) / "rec")
            p.home.mkdir(parents=True)
            for bad in ("../x", "/abs", "a//b", "./a"):
                with self.subTest(bad=bad):
                    with self.assertRaises(ValueError):
                        wt.seed_tree(p, {bad: "x"})


class LineageRecordTests(_LineageCase):
    def test_a_lineage_record_is_created_once(self) -> None:
        with self.assertRaisesRegex(ValueError, "already exists"):
            wt.write_lineage_record(self.paths, node_key="n", started_at_utc="t")

    def test_the_counter_stops_at_the_cap_persists_and_ends_the_lineage(self) -> None:
        self.assertTrue(wt.count_wrapper_call(self.paths, 2))
        self.assertTrue(wt.count_wrapper_call(self.paths, 2))
        self.assertEqual(wt.read_lineage_record(self.paths)["wrapper_calls_used"], 2)
        self.assertFalse(wt.count_wrapper_call(self.paths, 2))
        doc = wt.read_lineage_record(self.paths)
        self.assertEqual((doc["wrapper_calls_used"], doc["ended_by"]), (2, "max_wrapper_calls"))
        # An ended lineage refuses every later call, whatever the cap.
        self.assertFalse(wt.count_wrapper_call(self.paths, 100))

    def test_the_counter_is_atomic_across_threads(self) -> None:
        results: list[bool] = []
        lock = threading.Lock()

        def call() -> None:
            ok = wt.count_wrapper_call(self.paths, 25)
            with lock:
                results.append(ok)

        threads = [threading.Thread(target=call) for _ in range(40)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count(True), 25)
        self.assertEqual(wt.read_lineage_record(self.paths)["wrapper_calls_used"], 25)

    def test_the_first_reason_a_lineage_ended_stands(self) -> None:
        self.assertEqual(wt.end_lineage(self.paths, "lineage_timeout"), "lineage_timeout")
        self.assertEqual(wt.end_lineage(self.paths, "leaf_exit"), "lineage_timeout")
        with self.assertRaises(ValueError):
            wt.end_lineage(self.paths, "bored")

    def test_a_bad_cap_is_refused(self) -> None:
        for bad in (0, -1, True, "3"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    wt.count_wrapper_call(self.paths, bad)


class AssemblyTests(_LineageCase):
    def assemble(self):
        return wt.assemble_bundle_document(self.paths.tree, self.reserved)

    def assertFailure(self, result, category: str, *needles: str) -> None:
        self.assertIsInstance(result, wt.AssemblyFailure)
        self.assertEqual(result.category, category, result.findings)
        for needle in needles:
            self.assertIn(needle, result.findings)

    def test_no_manifest_is_workshop_tree_empty(self) -> None:
        self.assertFailure(self.assemble(), wt.WORKSHOP_TREE_EMPTY, "bundle.yaml")

    def test_content_is_read_from_the_named_files(self) -> None:
        self.write("x_model.f90", "module x\nend module x\n")
        self.write("sub/x_helper.f90", "module h\nend module h\n")
        self.manifest("x_model.f90", "sub/x_helper.f90")
        doc = self.assemble()
        self.assertIsInstance(doc, dict)
        self.assertEqual([f["content"] for f in doc["files"]],
                         ["module x\nend module x\n", "module h\nend module h\n"])
        # Assembly does not write into the tree.
        self.assertNotIn("content", (self.paths.tree / "bundle.yaml").read_text())

    def test_every_unreadable_target_is_a_finding_and_nothing_raises(self) -> None:
        outside = Path(self._tmp.name) / "outside.f90"
        outside.write_text("secret\n")
        (self.paths.tree / "link_out.f90").symlink_to(outside)
        self.write("real.f90", "x\n")
        (self.paths.tree / "link_in.f90").symlink_to(self.paths.tree / "real.f90")
        (self.paths.tree / "linkdir").symlink_to(self.paths.tree)
        (self.paths.tree / "adir.f90").mkdir()
        self.write("latin.f90", b"\xff\xfe x\n")
        self.manifest("missing.f90", "adir.f90", "latin.f90", "link_out.f90", "link_in.f90",
                      "linkdir/real.f90", "../escape.f90", "/abs.f90", "tests.md", "Makefile",
                      "real.f90")
        self.assertFailure(
            self.assemble(), wt.BUNDLE_SCHEMA_VIOLATION,
            "files[0]: missing.f90 does not exist",
            "files[1]: adir.f90 is not a regular file",
            "files[2]: latin.f90 is not UTF-8",
            "files[3]: link_out.f90 is reached through a symbolic link",
            "files[4]: link_in.f90 is reached through a symbolic link",
            "files[5]: linkdir/real.f90 is reached through a symbolic link",
            "files[6]: logical_path '../escape.f90'",
            "files[7]: logical_path '/abs.f90'",
            "files[8]: logical_path 'tests.md' names a host-placed tree file",
            "files[9]: logical_path 'Makefile' names a host-placed tree file")
        self.assertNotIn("files[10]", self.assemble().findings)

    def test_a_manifest_carrying_content_is_refused(self) -> None:
        self.write("x_model.f90", "x\n")
        self.write(wt.BUNDLE_MANIFEST_NAME,
                   "files:\n  - logical_path: x_model.f90\n    content: inline\n")
        self.assertFailure(self.assemble(), wt.BUNDLE_SCHEMA_VIOLATION, "must not carry content")

    def test_a_manifest_that_is_not_a_mapping_or_not_yaml_is_refused(self) -> None:
        for text, needle in (("- a\n- b\n", "must be a mapping"),
                             ("files: [\n", "not valid YAML"),
                             ("when: 2026-10-23\n", "not a JSON value")):
            with self.subTest(text=text):
                self.write(wt.BUNDLE_MANIFEST_NAME, text)
                self.assertFailure(self.assemble(), wt.BUNDLE_SCHEMA_VIOLATION, needle)
        self.write(wt.BUNDLE_MANIFEST_NAME, b"\xff\n")
        self.assertFailure(self.assemble(), wt.BUNDLE_SCHEMA_VIOLATION, "not UTF-8")

    def test_a_manifest_no_reader_anticipated_is_a_finding(self) -> None:
        deep = "a: " + "[" * 5000 + "]" * 5000 + "\n"
        for text in ('files:\n  - logical_path: "a\\0b.f90"\n',
                     'files:\n  - logical_path: "\\ud800.f90"\n', deep):
            with self.subTest(text=text[:40]):
                self.write(wt.BUNDLE_MANIFEST_NAME, text)
                self.assertFailure(self.assemble(), wt.BUNDLE_SCHEMA_VIOLATION)

    def test_a_symlinked_manifest_is_refused(self) -> None:
        target = Path(self._tmp.name) / "m.yaml"
        target.write_text("files: []\n")
        (self.paths.tree / wt.BUNDLE_MANIFEST_NAME).symlink_to(target)
        self.assertFailure(self.assemble(), wt.BUNDLE_SCHEMA_VIOLATION, "symbolic link")

    def test_a_document_without_files_goes_on_to_the_contract(self) -> None:
        self.write(wt.BUNDLE_MANIFEST_NAME, "schema_version: 1.2.0\n")
        self.assertEqual(self.assemble(), {"schema_version": "1.2.0"})


class SnapshotTests(_LineageCase):
    def test_the_snapshot_copies_leaf_files_only(self) -> None:
        self.write("x_model.f90", "m\n")
        self.write("notes/CLAUDE.md", "note\n")
        self.manifest("x_model.f90")
        self.write("tests.md", "edited by the leaf\n")  # an edited seed
        self.write(_CONTROL, "derived\n")
        outside = Path(self._tmp.name) / "o.txt"
        outside.write_text("o")
        (self.paths.tree / "ln").symlink_to(outside)
        out = wt.snapshot_tree(self.paths, self.reserved)
        self.assertEqual(out["copied"], ["bundle.yaml", "notes/CLAUDE.md", "x_model.f90"])
        self.assertEqual(out["links_refused"], ["ln"])
        names = sorted(p.relative_to(self.paths.snapshot).as_posix()
                       for p in self.paths.snapshot.rglob("*") if p.is_file())
        self.assertEqual(names, out["copied"])
        # The snapshot is assembled like the tree.
        self.assertIsInstance(
            wt.assemble_bundle_document(self.paths.snapshot, self.reserved), dict)

    def test_a_cold_reuse_seed_takes_the_snapshot_but_not_its_seeds(self) -> None:
        self.write("x_model.f90", "m\n")
        self.manifest("x_model.f90")
        wt.snapshot_tree(self.paths, self.reserved)
        # A seed name planted in the old snapshot by hand is still not copied.
        (self.paths.snapshot / "tests.md").write_text("stale\n")
        new = wt.LineagePaths(self.paths.homes_root, "orch_1", "arid-2",
                              self.paths.orchestration_dir)
        new.home.mkdir(parents=True)
        new.make_work_dirs()
        wt.seed_tree(new, {**_SEEDS, "tests.md": "fresh\n"})
        copied = wt.seed_from_snapshot(new, self.paths, self.reserved)
        self.assertEqual(copied, ["bundle.yaml", "x_model.f90"])
        self.assertEqual((new.tree / "tests.md").read_text(), "fresh\n")

    def test_a_cold_reuse_seed_does_not_overwrite_what_the_new_tree_holds(self) -> None:
        self.write("x_model.f90", "old\n")
        wt.snapshot_tree(self.paths, self.reserved)
        new = wt.LineagePaths(self.paths.homes_root, "orch_1", "arid-3",
                              self.paths.orchestration_dir)
        new.home.mkdir(parents=True)
        new.make_work_dirs()
        wt.seed_tree(new, {"x_model.f90": "new\n"})
        self.assertEqual(wt.seed_from_snapshot(new, self.paths, set()), [])
        self.assertEqual((new.tree / "x_model.f90").read_text(), "new\n")

    def test_the_snapshot_is_emptied_first_and_lists_linked_dirs(self) -> None:
        (self.paths.snapshot / "stale.f90").write_text("from a previous copy\n")
        (self.paths.tree / "dirlink").symlink_to(Path(self._tmp.name))
        self.write("x_model.f90", "m\n")
        out = wt.snapshot_tree(self.paths, self.reserved)
        self.assertFalse((self.paths.snapshot / "stale.f90").exists())
        self.assertEqual(out["links_refused"], ["dirlink"])
        self.assertFalse((self.paths.snapshot / "dirlink").exists())

    def test_an_unreadable_file_is_refused_not_raised(self) -> None:
        locked = self.write("locked.f90", "x\n")
        locked.chmod(0o000)
        try:
            # The row needs a refusal to observe; a process that reads a mode-000 file (root)
            # has none.
            self.assertFalse(os.access(locked, os.R_OK), "run the suite as a non-root user")
            out = wt.snapshot_tree(self.paths, self.reserved)
        finally:
            locked.chmod(0o644)
        self.assertNotIn("locked.f90", out["copied"])
        self.assertEqual([u.split(":")[0] for u in out["unreadable"]], ["locked.f90"])

    def test_a_file_that_became_a_link_is_refused_at_copy_time(self) -> None:
        # The walk saw a regular file; by the copy it is a link out of the tree (a run of the
        # leaf's own program swapping it). The copy reads through no link, so it is refused.
        outside = Path(self._tmp.name) / "secret.txt"
        outside.write_text("outside\n")
        (self.paths.tree / "swapped.f90").symlink_to(outside)
        copied, refused = wt._copy_files(self.paths.tree, self.paths.snapshot,
                                         [wt.PurePosixPath("swapped.f90")])
        self.assertEqual(copied, [])
        self.assertIn("symbolic link", refused[0])
        self.assertFalse((self.paths.snapshot / "swapped.f90").exists())


class HostFileWriteTests(_LineageCase):
    def test_the_control_file_is_written_in_the_tree_never_through_a_link(self) -> None:
        victim = Path(self._tmp.name) / "victim.txt"
        victim.write_text("untouched\n")
        (self.paths.tree / _CONTROL).symlink_to(victim)
        wt.write_host_file(self.paths.tree, _CONTROL, "rendered\n")
        self.assertEqual(victim.read_text(), "untouched\n")
        self.assertFalse((self.paths.tree / _CONTROL).is_symlink())
        self.assertEqual((self.paths.tree / _CONTROL).read_text(), "rendered\n")
        self.assertTrue(wt.is_host_file(self.paths.tree, _CONTROL))

    def test_a_directory_at_the_name_is_replaced(self) -> None:
        (self.paths.tree / _CONTROL).mkdir()
        wt.write_host_file(self.paths.tree, _CONTROL, "rendered\n")
        self.assertEqual((self.paths.tree / _CONTROL).read_text(), "rendered\n")

    def test_a_link_at_the_name_is_not_a_derived_control_file(self) -> None:
        target = self.write("real_control", "all:\n")
        (self.paths.tree / _CONTROL).symlink_to(target)
        self.assertFalse(wt.is_host_file(self.paths.tree, _CONTROL))


def _count_in_child(args) -> int:
    homes, orch_dir, cap, n = args
    paths = wt.LineagePaths(Path(homes), "orch_1", "arid-1", Path(orch_dir))
    return sum(wt.count_wrapper_call(paths, cap) for _ in range(n))


class CrossProcessCounterTests(_LineageCase):
    def test_the_counter_is_exclusive_across_processes(self) -> None:
        # The thread lock is per process; the file lock is what serializes a second process
        # serving the same lineage.
        import multiprocessing

        ctx = multiprocessing.get_context("fork")
        args = (str(self.paths.homes_root), str(self.paths.orchestration_dir), 60, 20)
        with ctx.Pool(6) as pool:
            granted = sum(pool.map(_count_in_child, [args] * 6))
        doc = wt.read_lineage_record(self.paths)
        self.assertEqual((granted, doc["wrapper_calls_used"], doc["ended_by"]),
                         (60, 60, "max_wrapper_calls"))


def _context(paths: wt.LineagePaths, reserved, **over) -> ww.WrapperContext:
    fields = dict(
        paths=paths, repo_root=Path("/nonexistent-checkout"), orchestration_id="orch_1",
        agent_run_id="arid-1", reserved_names=reserved, control_file_basename=_CONTROL,
        violations=lambda doc: None, build_graph=lambda doc: {"compile_units": []},
        render_control_file=lambda *, graph: "all:\n", closure_file_names=(),
        language="fortran", build_system="make", build_overrides=(), lint_preset="fortitude",
        syntax_compiler="gfortran", syntax_std="f2008", syntax_architecture=None,
        syntax_parallel_backend=None, syntax_staged_suffixes=(".f90",),
        launch_argv_prefix=(), launch_env=(), run_unavailable=None,
        base_env=tuple(sorted(ww.wrapper_base_env({"PATH": "/usr/bin:/bin"}).items())),
        toolchain_ro_roots=(), max_wrapper_calls=5, lineage_deadline=time.time() + 600)
    fields.update(over)
    return ww.WrapperContext(**fields)


class WrapperBoundsAndDeriveTests(_LineageCase):
    """The wrapper's bookkeeping and its host-side operation. The sandboxed ones run under real
    bwrap in `test_bwrap_simulation`."""

    def test_every_call_is_counted_and_the_cap_ends_the_lineage(self) -> None:
        ctx = _context(self.paths, self.reserved, max_wrapper_calls=2)
        for _ in range(2):
            self.assertEqual(ww.run_wrapper_op(ctx, "derive")["category"],
                             wt.WORKSHOP_TREE_EMPTY)
        refused = ww.run_wrapper_op(ctx, "derive")
        self.assertEqual((refused["ok"], refused["category"], refused["ended"]),
                         (False, ww.CATEGORY_MAX_WRAPPER_CALLS, True))
        self.assertEqual(wt.read_lineage_record(self.paths)["ended_by"], "max_wrapper_calls")
        # The count persists: a second launch's context over the same record is refused too.
        second = _context(self.paths, self.reserved, agent_run_id="arid-2",
                          max_wrapper_calls=2)
        self.assertEqual(ww.run_wrapper_op(second, "lint")["category"],
                         ww.CATEGORY_MAX_WRAPPER_CALLS)
        other = wt.LineagePaths(self.paths.homes_root, "orch_1", "arid-9",
                                self.paths.orchestration_dir)
        other.home.mkdir(parents=True)
        other.make_work_dirs()
        wt.write_lineage_record(other, node_key="n", started_at_utc="t")
        wt.end_lineage(other, "leaf_exit")
        self.assertEqual(ww.run_wrapper_op(_context(other, self.reserved), "lint")["category"],
                         ww.CATEGORY_LINEAGE_ENDED)

    def test_the_lineage_deadline_ends_the_lineage_without_counting(self) -> None:
        ctx = _context(self.paths, self.reserved, lineage_deadline=time.time() - 1)
        refused = ww.run_wrapper_op(ctx, "compile")
        self.assertEqual((refused["category"], refused["ended"]),
                         (ww.CATEGORY_LINEAGE_TIMEOUT, True))
        doc = wt.read_lineage_record(self.paths)
        self.assertEqual((doc["ended_by"], doc["wrapper_calls_used"]), ("lineage_timeout", 0))

    def test_an_unknown_operation_is_refused_uncounted(self) -> None:
        ctx = _context(self.paths, self.reserved)
        self.assertEqual(ww.run_wrapper_op(ctx, "shell")["category"],
                         ww.CATEGORY_BAD_ARGUMENTS)
        self.assertEqual(wt.read_lineage_record(self.paths)["wrapper_calls_used"], 0)

    def test_derive_writes_the_control_file_and_logs(self) -> None:
        self.write("x_model.f90", "m\n")
        self.manifest("x_model.f90")
        seen: list[dict] = []
        ctx = _context(self.paths, self.reserved,
                       violations=lambda doc: seen.append(doc) or None,
                       render_control_file=lambda *, graph: "rendered\n")
        out = ww.run_wrapper_op(ctx, "derive")
        self.assertEqual(out, {"ok": True, "control_file": _CONTROL, "files": ["x_model.f90"]})
        self.assertEqual(seen[0]["files"][0]["content"], "m\n")
        self.assertEqual((self.paths.tree / _CONTROL).read_text(), "rendered\n")
        entry = json.loads(self.paths.command_log.read_text().splitlines()[-1])
        self.assertEqual((entry["tool_name"], entry["ok"], entry["agent_run_id"]),
                         ("derive", True, "arid-1"))

    def test_derive_reports_the_contract_and_writes_nothing(self) -> None:
        self.write("x_model.f90", "m\n")
        self.manifest("x_model.f90")
        ctx = _context(self.paths, self.reserved,
                       violations=lambda doc: ("bundle_checks_abi_violation", "no checks"))
        out = ww.run_wrapper_op(ctx, "derive")
        self.assertEqual(out, {"ok": False, "category": "bundle_checks_abi_violation",
                               "findings": "no checks"})
        self.assertFalse((self.paths.tree / _CONTROL).exists())

    def test_derive_names_a_missing_staged_closure(self) -> None:
        self.write("x_model.f90", "m\n")
        self.manifest("x_model.f90")
        ctx = _context(self.paths, self.reserved, closure_file_names=("dep_model.f90",))
        self.assertEqual(ww.run_wrapper_op(ctx, "derive")["category"],
                         ww.CATEGORY_STAGED_CLOSURE)

    def test_compile_before_derive_is_refused(self) -> None:
        ctx = _context(self.paths, self.reserved)
        self.assertEqual(ww.run_wrapper_op(ctx, "compile")["category"],
                         ww.CATEGORY_NOT_DERIVED)

    def test_operations_taking_no_arguments_refuse_some(self) -> None:
        ctx = _context(self.paths, self.reserved)
        for op in ("derive", "compile", "lint", "syntax_check"):
            with self.subTest(op=op):
                self.assertEqual(ww.run_wrapper_op(ctx, op, {"x": 1})["category"],
                                 ww.CATEGORY_BAD_ARGUMENTS)

    def test_run_resolves_every_path_itself(self) -> None:
        (self.paths.staged_bin / "x_runner").write_text("#!/bin/sh\n")
        (self.paths.staged_bin / "x_runner").chmod(0o755)
        outside = Path(self._tmp.name) / "cases.yaml"
        outside.write_text("cases: []\n")
        (self.paths.tree / "ln.yaml").symlink_to(outside)
        ctx = _context(self.paths, self.reserved, max_wrapper_calls=100)
        bad = [
            {"binary": "../x_runner"}, {"binary": "/bin/sh"}, {"binary": "absent"},
            {"binary": "x_runner", "cases_file": "../../cases.yaml"},
            {"binary": "x_runner", "cases_file": "/etc/passwd"},
            {"binary": "x_runner", "cases_file": "ln.yaml"},
            {"binary": "x_runner", "case_ids": ["-o"]},
            {"binary": "x_runner", "case_ids": "c1"},
            {"binary": "x_runner", "argv": ["x"]},
        ]
        for args in bad:
            with self.subTest(args=args):
                self.assertEqual(ww.run_wrapper_op(ctx, "run", args)["category"],
                                 ww.CATEGORY_BAD_ARGUMENTS)

    def test_run_on_a_host_that_cannot_run_the_target_says_so(self) -> None:
        ctx = _context(self.paths, self.reserved, run_unavailable="no gpu",
                       launch_argv_prefix=None)
        out = ww.run_wrapper_op(ctx, "run", {"binary": "x"})
        self.assertEqual((out["category"], out["findings"]), (ww.CATEGORY_RUN_UNAVAILABLE,
                                                               "no gpu"))

    def test_a_failure_inside_an_operation_is_feedback(self) -> None:
        def boom(doc):
            raise KeyError("x")
        self.write("x_model.f90", "m\n")
        self.manifest("x_model.f90")
        out = ww.run_wrapper_op(_context(self.paths, self.reserved, violations=boom), "derive")
        self.assertEqual(out["category"], ww.CATEGORY_WRAPPER_ERROR)

    def test_the_base_env_is_the_hosts_path_on_a_private_tmp(self) -> None:
        env = ww.wrapper_base_env({"PATH": "/a:/b", "SECRET": "x"})
        self.assertEqual(env, {"PATH": "/a:/b", "HOME": "/tmp", "TMPDIR": "/tmp",
                               "LANG": "C.UTF-8"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
