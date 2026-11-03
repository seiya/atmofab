"""The bounds-checked second build and its `checked` row (issue #589 PR-0).

Four layers: the seam that resolves what the build is (`tools/checked_build.py`, and the `make`
backend's `checked_build_overrides`), the Build that makes it (`Conductor._build_inproc`, its
deliverables), the post-execute gate that binds the row to it
(`validate_pipeline_semantics._validate_checked_row` / `_validate_execution_shapes`), and a real
`make` reading the composed variables. Validate.execute's run of the row is pinned beside the
other shapes in `test_workflow_conductor.py` and, at a site, `test_execute_at_site.py`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tools.validate_pipeline_semantics as vps
import tools.workflow_conductor as wc
from tools import checked_build
from tools.backends import registry
from tools.backends.build_system.make import execute
from tools.orchestration_runtime import _target_toolchain_identity
from tools.target_profile import load_target_profile
from tools.tests.llm_samples import sample_config_with as _cfg
from tools.tests.target_fixtures import (
    FORTRAN_CPU,
    TARGET_ID,
    passing_shape_rows,
    plant_checked_build,
    profile_with,
)
from tools.tests.test_workflow_conductor import _build_runtime, _TargetedConductor

REPO = Path(__file__).resolve().parents[2]
PROFILES = {t: load_target_profile(REPO, t) for t in ("fortran_cpu", "fortran_cpu_mpi",
                                                      "cpp_gpu")}
MPI = PROFILES["fortran_cpu_mpi"]
GPU = PROFILES["cpp_gpu"]


def _conductor_tc(profile) -> dict[str, str]:
    """`Conductor._read_toolchain`'s shape for `profile`."""
    tc = profile.toolchain
    return {"language": str(tc["language"]), "standard": str(tc["standard"]),
            "build_system": str(tc["build_system"]), "compiler": str(tc.get("compiler") or ""),
            "backend": profile.parallel_backend}


class SeamTests(unittest.TestCase):
    """What the build is, resolved once for its three readers."""

    def test_each_checked_in_profile_resolves_its_compiler_and_flags(self) -> None:
        """The compiler is the compiler AXIS value — under the MPI backend's wrapper the
        compiler the wrapper runs, not the wrapper program the control file names — and the
        same one the build key records (`_target_toolchain_identity`)."""
        expected = {"fortran_cpu": ("gfortran", ("-fcheck=bounds",)),
                    "fortran_cpu_mpi": ("gfortran", ("-fcheck=bounds",)),
                    "cpp_gpu": ("nvcc", ())}
        for target_id, profile in PROFILES.items():
            with self.subTest(target=target_id):
                self.assertEqual((checked_build.target_compiler(profile),
                                  checked_build.checked_flags_for(profile)),
                                 expected[target_id])
                self.assertEqual(checked_build.target_compiler(profile),
                                 _target_toolchain_identity(profile, probe=False)["compiler"])
        # The program the MPI control file names is the wrapper, which no compiler record is.
        self.assertNotEqual(wc.build_compiler(_conductor_tc(MPI)),
                            checked_build.target_compiler(MPI))

    def test_a_pin_that_is_not_the_language_s_default_decides(self) -> None:
        """The profile's `toolchain.compiler` pin, not the language's default, names the
        compiler whose flags apply: every checked-in profile pins its language's default, so
        none of them can tell the two apart (round 0: ignoring the pin survived)."""
        pinned = profile_with(toolchain={**FORTRAN_CPU.toolchain, "compiler": "nvcc"})
        self.assertNotEqual(pinned.toolchain["compiler"],
                            checked_build.target_compiler(FORTRAN_CPU))
        self.assertEqual(checked_build.target_compiler(pinned), "nvcc")
        self.assertEqual(checked_build.checked_flags_for(pinned), ())
        self.assertIsNone(checked_build.checked_shape(pinned))

    def test_the_flag_rules_are_the_control_file_s(self) -> None:
        """`control_file_rules` hands the language's `rules` what the conductor's control-file
        writer hands it, so the gate's expected flag string is the rendered one."""
        for target_id, profile in PROFILES.items():
            with self.subTest(target=target_id):
                arch = (profile.doc.get("hardware") or {}).get("architecture")
                self.assertEqual(
                    checked_build.control_file_rules(profile),
                    wc.Conductor._control_file_rules(_conductor_tc(profile),
                                                     str(arch) if arch else None))

    def test_a_compiler_that_does_not_declare_the_capability_is_refused(self) -> None:
        """An absent declaration is not an empty one: the build and the gate refuse rather than
        skip the row."""
        record = registry._BACKENDS[("compiler", "gfortran")]
        stripped = record._replace(backend_provides=record.backend_provides - {"checked_flags"})
        with mock.patch.dict(registry._BACKENDS, {("compiler", "gfortran"): stripped}):
            with self.assertRaises(checked_build.CheckedBuildUnavailable) as caught:
                checked_build.checked_flags_for(FORTRAN_CPU)
            self.assertIn("checked_flags", str(caught.exception))
            with self.assertRaises(checked_build.CheckedBuildUnavailable):
                checked_build.checked_shape(FORTRAN_CPU)

    def test_the_checked_shape(self) -> None:
        """One process, one thread, no imposed grid, the certified block size; None when the
        compiler declares no flags."""
        shape = checked_build.checked_shape(MPI)
        self.assertEqual((shape.label, shape.threads_per_rank, shape.ranks, shape.process_grid,
                          shape.block_size), ("checked", 1, 1, None, None))
        gfortran_on_a_block = profile_with(GPU, toolchain={**GPU.toolchain,
                                                           "language": "fortran",
                                                           "compiler": "gfortran"})
        self.assertEqual(checked_build.checked_shape(gfortran_on_a_block).block_size,
                         GPU.block_size)
        self.assertIsNone(checked_build.checked_shape(GPU))
        self.assertNotIn(checked_build.CHECKED_SHAPE_LABEL,
                         {s.label for p in PROFILES.values() for s in p.execution_shapes})

    def test_the_overrides_append_the_flags_to_the_control_file_s_string_verbatim(self) -> None:
        """The flag string is `rules['flags']` byte for byte — the module-directory pair with
        the object-directory reference unexpanded — followed by the flags; the directories
        are the checked build's own."""
        rules = checked_build.control_file_rules(FORTRAN_CPU)
        out = execute.checked_build_overrides("/o", "/b", "x_runner", rules, ("-fA", "-fB"))
        self.assertEqual(out[:3], execute.build_overrides("/o", "/b", "x_runner"))
        self.assertEqual(out[3], f"{rules['flags_variable']}={rules['flags']} -fA -fB")
        self.assertEqual(len(out), 4)
        # The variable and the string are the mapping's, not a spelling of this function.
        self.assertEqual(
            execute.checked_build_overrides("/o", "/b", "x", {"flags_variable": "ZZ",
                                                              "flags": "q r"}, ("-s",))[3],
            "ZZ=q r -s")
        for empty in ((), ("",), (" ",)):
            with self.subTest(empty=empty), self.assertRaises(ValueError):
                execute.checked_build_overrides("/o", "/b", "x", rules, empty)


def _refs(**over) -> wc.NodeRefs:
    return wc.NodeRefs(target_id=TARGET_ID, node_key="component/spec_x@0.1.0",
                       spec_path="spec/component/spec_x", ir_id="x_1", pipeline_id="x_1",
                       source_id="src_1", binary_id="bin_1", **over)


class BuildTests(unittest.TestCase):
    """`Conductor._build_inproc` makes the checked binary as a second deliverable."""

    def _build(self, repo: Path, *, target=None, checked_ok: bool = True,
               checked_writes: bool = True, certified_writes: bool = True):
        build_runtime = _build_runtime()
        c = _TargetedConductor(repo_root=repo, orchestration_id="t",
                               orchestration_agent_run_id="x", llm_config=_cfg("claude"), env={},
                               target_profile=target)
        refs = _refs()
        (repo / refs.ir_ref).mkdir(parents=True, exist_ok=True)
        (repo / refs.source_dir() / "src").mkdir(parents=True, exist_ok=True)
        calls: list[dict] = []
        staged: list[Path] = []
        real_stage = wc.Conductor._stage_dependency_sources

        def spy_stage(self_, refs_, obj_dir, *, phase):
            staged.append(Path(obj_dir))
            return real_stage(self_, refs_, obj_dir, phase=phase)

        def fake_compile(args):
            calls.append(args)
            overrides = dict(a.split("=", 1) for a in args["extra_args"])
            checked = len(calls) == 2
            if (checked_writes if checked else certified_writes):
                Path(overrides["BINDIR"]).mkdir(parents=True, exist_ok=True)
                (Path(overrides["BINDIR"]) / overrides["BIN"]).write_text("bin\n")
            ok = checked_ok if checked else True
            return {"ok": ok, "return_code": 0 if ok else 2, "command_id": f"cid{len(calls)}",
                    "command": ["make", "-j1", *args["extra_args"]],
                    "stderr": "" if ok else "checked: Error: boom"}

        with mock.patch.object(build_runtime, "tool_compile_project", fake_compile), \
                mock.patch.object(wc.Conductor, "_stage_dependency_sources", spy_stage), \
                mock.patch.object(subprocess, "run",
                                  return_value=subprocess.CompletedProcess([], 0, "", "")):
            out = c._build_inproc(refs, "child-1")
        meta = json.loads((repo / refs.binary_dir() / "binary_meta.json").read_text())
        return c, refs, out, meta, calls, staged

    def test_the_second_build_is_the_control_file_s_flags_plus_the_compiler_s(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            c, refs, out, meta, calls, staged = self._build(repo)
            self.assertEqual(out["returncode"], 0)
            self.assertEqual(meta["verification_status"], "pass", meta)
            self.assertEqual(len(calls), 2)
            obj = repo / "workspace" / "tmp" / "child-1" / "build"
            obj_checked = obj.parent / "build_checked"
            bin_checked = repo / refs.binary_dir() / "bin_checked"
            target = c.target
            expected = list(execute.checked_build_overrides(
                str(obj_checked), str(bin_checked), "spec_x_runner",
                checked_build.control_file_rules(target),
                checked_build.checked_flags_for(target)))
            self.assertEqual(calls[1]["extra_args"], expected)
            self.assertEqual(calls[0]["extra_args"], list(execute.build_overrides(
                str(obj), str(repo / refs.binary_dir() / "bin"), "spec_x_runner")))
            # The closure is staged into each object directory, the certified one first.
            self.assertEqual(staged, [obj, obj_checked])
            self.assertTrue(any(obj_checked.iterdir()), "nothing staged into build_checked")
            self.assertEqual(sorted(p.name for p in obj_checked.iterdir()),
                             sorted(p.name for p in obj.iterdir()))
            self.assertEqual(meta["checked_build"], {
                "binary_ref": "binary/bin_1/bin_checked/spec_x_runner",
                "obj_dir": str(obj_checked), "bin_dir": str(bin_checked),
                "flags": list(checked_build.checked_flags_for(target)),
                "command_id": "cid2", "argv": ["make", "-j1", *expected]})
            self.assertEqual(meta["binary_artifact_ref"], "binary/bin_1/bin/spec_x_runner")
            self.assertEqual(meta["command_id"], "cid1")

    def test_a_failed_checked_build_fails_the_build(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _c, _r, out, meta, calls, _s = self._build(Path(td), checked_ok=False,
                                                       checked_writes=False)
            self.assertEqual(out["returncode"], 0)  # a content failure, routed to Generate
            self.assertEqual(len(calls), 2)
            self.assertEqual(meta["verification_status"], "fail")
            self.assertIn("checked: Error: boom", meta["failure_excerpt"])
            self.assertIn("[checked build]", meta["failure_excerpt"])

    def test_a_checked_build_that_left_no_binary_fails_the_build(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _c, _r, _o, meta, calls, _s = self._build(Path(td), checked_writes=False)
            self.assertEqual(len(calls), 2)
            self.assertEqual(meta["verification_status"], "fail")
            self.assertEqual(meta["failure_category"], execute.BINARY_MISSING[0])
            self.assertIn("bin_checked/spec_x_runner", meta["failure_excerpt"])

    def test_no_checked_build_after_a_failed_build(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _c, _r, _o, meta, calls, _s = self._build(Path(td), certified_writes=False)
            self.assertEqual(len(calls), 1)
            self.assertEqual(meta["verification_status"], "fail")
            self.assertIn("bin/spec_x_runner", meta["failure_excerpt"])
            self.assertNotIn("bin_checked", meta["failure_excerpt"])

    def test_a_compiler_that_declares_no_flags_builds_once_and_records_null(self) -> None:
        none_declared = profile_with(toolchain={**FORTRAN_CPU.toolchain})
        record = registry._BACKENDS[("compiler", "gfortran")]
        module = registry.capability_module("compiler", "gfortran", "checked_flags")
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(module, "CHECKED_FLAGS", ()):
            c, _r, _o, meta, calls, staged = self._build(Path(td), target=none_declared)
            self.assertIn("checked_flags", record.backend_provides)
            self.assertEqual(len(calls), 1)
            self.assertEqual(len(staged), 1)
            self.assertIsNone(meta["checked_build"])
            self.assertEqual(meta["verification_status"], "pass")
            self.assertFalse(c._builds_checked())

    def test_the_checked_binary_is_a_deliverable_when_the_compiler_declares_flags(self) -> None:
        refs = _refs()
        common = {"step": "build", "substep": None, "orchestration_id": "o",
                  "orchestration_agent_run_id": "a", "child_agent_run_id": "c",
                  "agent_model": "m", "workflow_mode": "dev", "exe_name": "spec_x_runner"}
        on = wc.build_launch_request(refs, checked_build=True, **common)["allowed_output_paths"]
        off = wc.build_launch_request(refs, **common)["allowed_output_paths"]
        bdir = refs.binary_dir()
        self.assertEqual(on, [f"{bdir}/bin/spec_x_runner", f"{bdir}/bin_checked/spec_x_runner",
                              f"{bdir}/binary_meta.json", f"{bdir}/command_log.jsonl"])
        self.assertEqual(off, [p for p in on if "/bin_checked/" not in p])
        with tempfile.TemporaryDirectory() as td:
            c = _TargetedConductor(repo_root=Path(td), orchestration_id="t",
                                   orchestration_agent_run_id="x", llm_config=_cfg("claude"),
                                   env={})
            self.assertTrue(c._builds_checked())
            gpu = _TargetedConductor(repo_root=Path(td), orchestration_id="t",
                                     orchestration_agent_run_id="x", llm_config=_cfg("claude"),
                                     env={}, target_profile=GPU)
            self.assertFalse(gpu._builds_checked())

    def test_the_runtime_admits_the_checked_deliverable_and_the_stamp_pins_it(self) -> None:
        """Round 1 (the blocker): the launch `run_phase` records for a declaring compiler's Build
        passes the runtime's own validation — `_validate_launch_request_payload` and the
        phase-contract matcher `_allowed_output_paths_for_launch`, which `record-launch` runs
        before the in-process body — and the step result's `required_outputs` carry the same
        path, so `_certifiable_artifact_refs` (the set `artifact_hashes` is taken over) pins it.
        Driven over a recorded real build request. A path one level deeper, or beside it under
        another name, stays refused."""
        import glob

        from tools.orchestration_runtime import (
            _allowed_output_paths_for_launch,
            _certifiable_artifact_refs,
            _validate_launch_request_payload,
        )
        from tools.tests.test_workflow_conductor import _FIXTURE_DIR, _refs_from_request
        requests = [json.loads(Path(f).read_text()) for f in
                    sorted(glob.glob(str(_FIXTURE_DIR / "*.request.json")))]
        req = next(r for r in requests if isinstance(r, dict) and r.get("step") == "build"
                   and "allowed_output_paths" in r)
        refs = _refs_from_request(req)
        common = {"step": "build", "substep": req.get("substep"),
                  "orchestration_id": req["orchestration_id"],
                  "orchestration_agent_run_id": req["parent_agent_run_id"],
                  "child_agent_run_id": req["agent_run_id"], "agent_model": req["agent_model"],
                  "workflow_mode": req["workflow_mode"], "exe_name": f"{refs.spec_id}_runner"}
        built = wc.build_launch_request(refs, checked_build=True, **common)
        checked = f"{refs.binary_dir()}/bin_checked/{refs.spec_id}_runner"
        self.assertIn(checked, built["allowed_output_paths"])
        _validate_launch_request_payload(built)
        self.assertIn(checked, _allowed_output_paths_for_launch(request_payload=built))
        required = wc.phase_required_outputs(refs, "build", exe_name=f"{refs.spec_id}_runner",
                                             checked_build=True)
        def binaries(paths):
            return [p for p in paths if "/bin/" in p or "/bin_checked/" in p]

        self.assertEqual(binaries(required), binaries(built["allowed_output_paths"]))
        self.assertEqual(len(binaries(required)), 2)
        self.assertIn(checked, _certifiable_artifact_refs(
            required, f"{refs.binary_dir()}/binary_meta.json"))
        self.assertNotIn(checked, wc.phase_required_outputs(
            refs, "build", exe_name=f"{refs.spec_id}_runner"))
        for wrong in (f"{refs.binary_dir()}/bin_checked/sub/{refs.spec_id}_runner",
                      f"{refs.binary_dir()}/bin_checkedX/{refs.spec_id}_runner"):
            with self.subTest(wrong=wrong):
                bad = dict(built, allowed_output_paths=[*built["allowed_output_paths"], wrong])
                with self.assertRaises(ValueError):
                    _allowed_output_paths_for_launch(request_payload=bad)


class GateTests(unittest.TestCase):
    """The post-execute gate binds the `checked` row to the checked binary, REQUIRED."""

    def _tree(self, tmp: str, target=FORTRAN_CPU):
        root = Path(tmp)
        pipeline = root / "pipe"
        node = pipeline / "runs" / "run_1" / "node"
        node.mkdir(parents=True)
        (pipeline / "binary" / "bin_1" / "bin").mkdir(parents=True)
        (pipeline / "binary" / "bin_1" / "bin" / "x_runner").write_text("bin\n")
        (node / "trial_meta.json").write_text(json.dumps({"source_binary_id": "bin_1"}))
        (node / "quality_check.json").write_text(json.dumps(
            {"status": "pass", "shapes": passing_shape_rows(target)}))
        plant_checked_build(pipeline, node, binary_id="bin_1", source_id="src_1",
                            exe="x_runner", profile=target)
        execution = vps.NodeExecution(node_key="component/x@0.1.0", node_dir=node,
                                      exec_dir=node, pipeline_dir=pipeline)
        return root, pipeline, node, execution

    def _violations(self, root: Path, execution, target=FORTRAN_CPU) -> list[str]:
        violations: list[str] = []
        with mock.patch.object(vps, "_pipeline_target", return_value=target):
            vps._validate_execution_shapes(root, execution, violations)
            vps._validate_checked_row(root, execution, violations)
        return violations

    @staticmethod
    def _edit_json(path: Path, fn) -> None:
        doc = json.loads(path.read_text())
        fn(doc)
        path.write_text(json.dumps(doc))

    @staticmethod
    def _edit_log(path: Path, command_id: str, fn) -> None:
        recs = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        for rec in recs:
            if rec.get("command_id") == command_id:
                fn(rec)
        path.write_text("".join(json.dumps(r) + "\n" for r in recs))

    def test_the_planted_records_pass_on_each_declaring_profile(self) -> None:
        for target in (FORTRAN_CPU, MPI):
            with self.subTest(target=target.target_id), tempfile.TemporaryDirectory() as tmp:
                root, _p, _n, execution = self._tree(tmp, target)
                self.assertEqual(self._violations(root, execution, target), [])

    def test_a_compiler_that_declares_no_flags_needs_no_row_and_no_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            node = root / "node"
            node.mkdir()
            rows = passing_shape_rows(GPU)
            self.assertNotIn("checked", [r["label"] for r in rows])
            (node / "quality_check.json").write_text(json.dumps({"status": "pass",
                                                                 "shapes": rows}))
            execution = vps.NodeExecution(node_key="component/x@0.1.0", node_dir=node,
                                          exec_dir=node, pipeline_dir=root)
            self.assertEqual(self._violations(root, execution, GPU), [])

    def test_a_declaring_compiler_s_run_without_the_row_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _p, node, execution = self._tree(tmp)
            self._edit_json(node / "quality_check.json",
                            lambda d: d.__setitem__("shapes", d["shapes"][:-1]))
            violations = self._violations(root, execution)
            self.assertTrue(any("then the checked binary's" in v for v in violations),
                            violations)

    def test_a_compiler_that_does_not_declare_the_capability_is_a_violation(self) -> None:
        record = registry._BACKENDS[("compiler", "gfortran")]
        stripped = record._replace(backend_provides=record.backend_provides - {"checked_flags"})
        with tempfile.TemporaryDirectory() as tmp:
            root, _p, _n, execution = self._tree(tmp)
            with mock.patch.dict(registry._BACKENDS, {("compiler", "gfortran"): stripped}):
                violations = self._violations(root, execution)
            self.assertTrue(any("checked_flags" in v for v in violations), violations)

    def test_each_refusal_of_the_binding(self) -> None:
        """Every condition the walked `source_command_ref` form skips on is a refusal here."""
        meta = ("pipe", "binary", "bin_1", "binary_meta.json")

        def rows(fn):
            return "qc", lambda d: fn(d["shapes"][-1])

        def checked(fn):
            return meta, lambda d: fn(d["checked_build"])

        cases: dict[str, tuple] = {
            # the row's command_id
            "null command_id": (*rows(lambda r: r.__setitem__("command_id", None)),
                                "must name exactly one record"),
            "empty command_id": (*rows(lambda r: r.__setitem__("command_id", "")),
                                 "must name exactly one record"),
            "unmatched command_id": (*rows(lambda r: r.__setitem__("command_id", "nope")),
                                     "must name exactly one record"),
            # the record's binary and form
            "ran bin/<exe>": ("log", lambda r: r.__setitem__(
                "command", [r["command"][0].replace("/bin_checked/", "/bin/"),
                            *r["command"][1:]]), "runs binary/bin_1/bin_checked/x_runner bare"),
            "launcher prefix": ("log", lambda r: r.__setitem__(
                "command", ["mpirun", "-n", "1", *r["command"]]), "bare"),
            "relative executable outside": ("log", lambda r: r.update(
                {"command": ["./x_runner", *r["command"][1:]]}), "bare"),
            "not run_program": ("log", lambda r: r.__setitem__("tool_name", "compile_project"),
                                "run_program of the checked binary"),
            "record failed": ("log", lambda r: r.__setitem__("ok", False),
                              "run_program of the checked binary"),
            "command not a list": ("log", lambda r: r.__setitem__("command", "x_runner"),
                                   "run_program of the checked binary"),
            # binary_meta.json#checked_build
            "checked_build null": (meta, lambda d: d.__setitem__("checked_build", None),
                                   "checked_build is None"),
            "binary_ref": (*checked(lambda b: b.__setitem__(
                "binary_ref", "binary/bin_1/bin/x_runner")), "checked_build.binary_ref"),
            "flags": (*checked(lambda b: b.__setitem__("flags", [])), "checked_build.flags"),
            "bin_dir": (*checked(lambda b: b.__setitem__("bin_dir", "/elsewhere")),
                        "checked_build.bin_dir"),
            "bin_dir is bin/": (*checked(lambda b: b.__setitem__(
                "bin_dir", b["bin_dir"][:-len("bin_checked")] + "bin")),
                                "checked_build.bin_dir"),
            "obj_dir missing": (*checked(lambda b: b.__setitem__("obj_dir", "")),
                                "must be non-empty strings"),
            "argv without the flags": (*checked(lambda b: b.__setitem__("argv", b["argv"][:-1])),
                                       "checked_build.argv must end with"),
            "argv recomposed": (*checked(lambda b: b["argv"].__setitem__(
                -1, b["argv"][-1].replace("$(OBJDIR)", b["obj_dir"]))),
                                "checked_build.argv must end with"),
            "build command_id": (*checked(lambda b: b.__setitem__("command_id", "nope")),
                                 "must name one compile_project record"),
            # clause (ii): the compile record itself
            "compile record failed": ("build_log", lambda r: r.__setitem__("ok", False),
                                      "must name one compile_project record"),
            "compile record other argv": ("build_log", lambda r: r.__setitem__(
                "command", r["command"][:-1]), "must name one compile_project record"),
        }
        for name, (where, fn, needle) in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                root, pipeline, node, execution = self._tree(tmp)
                if where == "log":
                    self._edit_log(node / "command_log.jsonl", "cmd_checked", fn)
                elif where == "qc":
                    self._edit_json(node / "quality_check.json", fn)
                elif where == "build_log":
                    self._edit_log(pipeline / "source" / "src_1" / "src" / "command_log.jsonl",
                                   "cmd_checked_build", fn)
                else:
                    self._edit_json(root.joinpath(*where), fn)
                violations = self._violations(root, execution)
                self.assertTrue(any(needle in v for v in violations), (needle, violations))

    def test_a_resume_after_the_checkout_moved_is_admitted(self) -> None:
        """Round 1 (record axis F1): Build records absolute directories, and a resume from a
        moved checkout validates the same certified Build — its run records name the new
        paths, `binary_meta.json` the old. The gate admits it (it refused it, measured)."""
        with tempfile.TemporaryDirectory() as tmp:
            old_root = Path(tmp) / "old"
            old_root.mkdir()
            _r, _p, _n, _e = self._tree(str(old_root))
            new_root = Path(tmp) / "new"
            shutil.move(str(old_root), str(new_root))
            node = new_root / "pipe" / "runs" / "run_1" / "node"
            moved = str(new_root / "pipe" / "binary" / "bin_1" / "bin_checked" / "x_runner")
            self._edit_log(node / "command_log.jsonl", "cmd_checked",
                           lambda r: r.update({"command": [moved, *r["command"][1:]],
                                               "cwd": str(node)}))
            execution = vps.NodeExecution(node_key="component/x@0.1.0", node_dir=node,
                                          exec_dir=node, pipeline_dir=new_root / "pipe")
            meta = json.loads((new_root / "pipe/binary/bin_1/binary_meta.json").read_text())
            self.assertTrue(meta["checked_build"]["bin_dir"].startswith(str(old_root)))
            self.assertEqual(self._violations(new_root, execution), [])

    def test_a_record_written_twice_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _p, node, execution = self._tree(tmp)
            log = node / "command_log.jsonl"
            log.write_text(log.read_text() * 2)
            violations = self._violations(root, execution)
            self.assertTrue(any("found 2" in v for v in violations), violations)

    def test_the_mpi_row_is_bound_bare_and_refused_under_the_certified_prefix(self) -> None:
        """On a launcher target the certified run's prefix is not the row's: the row runs
        bare, so a record under the launcher is refused and the bare one admitted."""
        from tools.host_execution import launch_argv_prefix
        prefix = list(launch_argv_prefix(MPI.parallel_backend, MPI.ranks))
        self.assertTrue(prefix)
        with tempfile.TemporaryDirectory() as tmp:
            root, _p, node, execution = self._tree(tmp, MPI)
            self.assertEqual(self._violations(root, execution, MPI), [])
            self._edit_log(node / "command_log.jsonl", "cmd_checked",
                           lambda r: r.__setitem__("command", [*prefix, *r["command"]]))
            violations = self._violations(root, execution, MPI)
            self.assertTrue(any("bare, with no launch prefix" in v for v in violations),
                            violations)


@unittest.skipUnless(shutil.which("make"), "make is not installed")
class RealMakeTests(unittest.TestCase):
    """The composed variables as `make` reads them: the command-line flag string replaces the
    control file's `?=` value whole, and its unexpanded object-directory reference is expanded
    by make at use, to the checked build's own directory."""

    def test_the_flag_string_reaches_make_with_its_reference_expanded(self) -> None:
        build_runtime = _build_runtime()
        rules = checked_build.control_file_rules(FORTRAN_CPU)
        var = rules["flags_variable"]
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src"
            src.mkdir()
            (src / "Makefile").write_text(
                f"OBJDIR ?= .\nBINDIR ?= .\nBIN ?= x\n{var} ?= {rules['flags']}\n"
                f"all:\n\tmkdir -p $(OBJDIR) $(BINDIR)\n"
                f"\tprintf '%s\\n' '$({var})' > $(OBJDIR)/flags.txt\n"
                f"\ttouch $(BINDIR)/$(BIN)\n")
            obj, bin_ = Path(td) / "obj_checked", Path(td) / "bin_checked"
            result = build_runtime.tool_compile_project({
                "project_dir": str(src), "language": "fortran", "build_system": "make",
                "extra_args": list(execute.checked_build_overrides(
                    str(obj), str(bin_), "x", rules,
                    checked_build.checked_flags_for(FORTRAN_CPU))),
                "command_log_path": str(Path(td) / "log.jsonl")})
            self.assertTrue(result["ok"], result)
            flags = (obj / "flags.txt").read_text().strip()
            self.assertEqual(flags, f"{rules['flags']} -fcheck=bounds".replace(
                "$(OBJDIR)", str(obj)))
            self.assertEqual(flags.count(f"-J{obj}"), 1)
            self.assertTrue((bin_ / "x").is_file())


if __name__ == "__main__":
    unittest.main()
