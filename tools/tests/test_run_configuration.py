"""`tools/run_configuration.py` (issue #632 P5): a person-written configuration run on a node's
certified binary, outside the workflow.

Each row seeds a certified chain (`orchestration_fixtures.certify_node`) whose IR is a toy node
of `test_driver_render`. Most rows certify that IR's driver, rendered by the real
`render_runner` and built with the toy model, so the selection is the workflow's own resolver
(`DerivationResolver.select`), the evaluation is the host's evaluator, the run is the real
binary, and a continuation is the real driver's restart read under the real start check. The
rows about the process (signals, timeout, a binary that cannot start, the launch environment,
a missing final capture) certify a shell script instead, which they name.
"""
from __future__ import annotations

import atexit
import copy
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock

import yaml

from tools import run_configuration as rc
from tools.backends.language.fortran import runner as f_runner
from tools.tests.orchestration_fixtures import certify_node
from tools.tests.test_driver_render import call_ir, init_ir, loop_ir, run_fortran

NODE = "component/drv_toy@0.1.0"
#: The tail of a script binary that "reaches its end": a final capture with its results true.
FINAL = (b'echo \'{"case_id": "m0", "step": 4.0, "ok": 1.0}\' '
         b'> raw/state_snapshots/m0.json\n')
SPEC_REF = "spec/component/drv_toy"


def _toy_ir(kind: str) -> dict[str, Any]:
    """The loop node with host-evaluated loop scalars (`loop`), or the one whose `init` writes
    `n_step` and `dt` (`init`); each case carries an unread numeric leaf and a string leaf."""
    ir = copy.deepcopy({"loop": lambda: loop_ir(0, 1, every=1, n_step=4),
                        "valid": lambda: loop_ir(0, 1, every=1, n_step=4),
                        "init": lambda: init_ir(0), "call": lambda: call_ir(0)}[kind]())
    if kind == "valid":
        # the step's result dummy `ok` bound to a state of another name
        ir["drive"]["step"]["arguments"]["ok"] = "state.valid"
        schema = ir["io_contract"]["raw_requirements"]["required_evidence"][0]["schema"]
        for var in schema["variables"]:
            if var["name"] == "ok":
                var["name"] = "valid"
    for case in ir["case"]["test_case_set"]:
        case["inputs"]["unused"] = 7
        case["inputs"]["profile"] = "descriptive"
        if kind == "init":
            case["inputs"]["every"] = 1
    return ir


class _Built:
    """One binary per (IR kind, renderer) for the whole module."""

    _dir: Path | None = None
    _cache: ClassVar[dict[tuple[str, bool], tuple[dict[str, Any], bytes]]] = {}

    @classmethod
    def get(cls, kind: str, restart_read: bool = True) -> tuple[dict[str, Any], bytes]:
        key = (kind, restart_read)
        if key not in cls._cache:
            if cls._dir is None:
                cls._dir = Path(tempfile.mkdtemp(prefix="run-configuration-bin-"))
                atexit.register(shutil.rmtree, cls._dir, True)
            tmp = cls._dir / f"{kind}_{int(restart_read)}"
            tmp.mkdir()
            ir = _toy_ir(kind)

            def no_read(a: Any, _plan: Any, _entry: Any, _h: Any, ind: str, *_r: Any) -> None:
                a(f"{ind}step0 = 0")   # the read omitted, the count kept

            if restart_read:
                run = run_fortran(tmp, ir)
            else:
                with mock.patch.object(f_runner, "_render_restart", no_read):
                    run = run_fortran(tmp, ir)
            if run.proc.returncode != 0:
                raise AssertionError(run.proc.stderr[-2000:])
            cls._cache[key] = (ir, (tmp / "bin").read_bytes())
        return cls._cache[key]


def tearDownModule() -> None:
    if _Built._dir is not None:
        shutil.rmtree(_Built._dir, ignore_errors=True)


class RunConfigurationTest(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # --- fixtures ---------------------------------------------------------------------------

    def certify(self, kind: str = "loop", *, restart_read: bool = True,
                exe_bytes: bytes | None = None, build_site: Any = "local") -> dict[str, str]:
        ir, binary = _Built.get(kind, restart_read)
        refs = certify_node(self.repo, "orch_seed", NODE,
                            ir_text="# certified\n" + yaml.safe_dump(ir),
                            exe_bytes=exe_bytes if exe_bytes is not None else binary,
                            ir_id="drv-toy_20260101_001", pipeline_id="drv-toy_20260101_001")
        (self.repo / refs["exe_ref"]).chmod(0o755)
        meta = self.repo / refs["binary_meta"]
        doc = json.loads(meta.read_text())
        if build_site is not None:
            doc["environment"] = {"build_site": {"site": build_site, "host": None}}
        meta.write_text(json.dumps(doc))
        return refs

    def config(self, doc: Any, name: str = "config.yaml") -> Path:
        path = self.tmp / name
        path.write_text(doc if isinstance(doc, str) else yaml.safe_dump(doc))
        return path

    def run_tool(self, doc: Any, out: str = "out", **kw: Any) -> int:
        return rc.run_configuration(self.repo, SPEC_REF, "fortran_cpu", self.config(doc),
                                    self.tmp / out, **kw)

    def refused(self, doc: Any, out: str = "out", **kw: Any) -> str:
        with self.assertRaises(rc.ConfigurationRefused) as ctx:
            self.run_tool(doc, out, **kw)
        # nothing is written under <out> before a refusal
        self.assertFalse((self.tmp / out).exists() and any((self.tmp / out).iterdir()))
        return str(ctx.exception)

    def snap(self, out: str, name: str) -> dict[str, Any]:
        return json.loads((self.tmp / out / "raw" / "state_snapshots" / f"{name}.json")
                          .read_text())

    def record(self, out: str = "out") -> dict[str, Any]:
        return json.loads((self.tmp / out / rc.RUN_RECORD).read_text())

    # --- the run ----------------------------------------------------------------------------

    def test_an_evaluated_override_changes_the_case_file_and_the_capture(self) -> None:
        refs = self.certify()
        ir_inputs = self.repo / refs["ir_ref"] / "case_inputs"
        before = sorted(ir_inputs.rglob("*")) if ir_inputs.exists() else []
        self.assertEqual(self.run_tool({"base_case": "m0"}, "plain"), 0)
        self.assertEqual(
            self.run_tool({"base_case": "m0", "inputs": {"n_step": 8, "t_end": 3.0}}), 0)
        case = json.loads((self.tmp / "out" / "case_inputs" / "m0.json").read_text())
        plain = json.loads((self.tmp / "plain" / "case_inputs" / "m0.json").read_text())
        self.assertEqual((case["inputs.n_step"], case["inputs.t_end"]), (8, 3.0))
        self.assertEqual((plain["inputs.n_step"], plain["inputs.t_end"]), (4, 2.0))
        self.assertEqual({k: v for k, v in case.items() if k not in ("inputs.n_step",
                                                                     "inputs.t_end")},
                         {k: v for k, v in plain.items() if k not in ("inputs.n_step",
                                                                      "inputs.t_end")})
        self.assertEqual(case["restart.step"], 0)
        self.assertEqual(self.snap("out", "m0")["step"], 8.0)
        self.assertEqual(self.snap("plain", "m0")["step"], 4.0)
        self.assertEqual(self.snap("out", "m0")["t"], 3.0)
        # the tool ran the base's id only, and wrote nothing beside the certified IR
        self.assertEqual(sorted(p.name for p in (self.tmp / "out" / "case_inputs").iterdir()),
                         ["m0.json"])
        self.assertEqual(sorted(ir_inputs.rglob("*")) if ir_inputs.exists() else [], before)
        rec = self.record()
        self.assertEqual(rec["status"], "ok")
        self.assertEqual(rec["exit_status"], 0)
        self.assertEqual(rec["statement"], rc.NOT_A_CERTIFICATION)
        self.assertEqual(rec["node_key"], NODE)
        self.assertEqual(rec["binary_id"], refs["binary_id"])
        self.assertEqual(rec["configuration"]["inputs"], {"n_step": 8, "t_end": 3.0})
        self.assertIn("n_step: 8", rec["configuration"]["text"])
        self.assertEqual(rec["command"][1:], ["--cases",
                                              str((self.tmp / "out").resolve() / "spec.ir.yaml"),
                                              "m0"])
        self.assertIsNone(rec["restart"])

    def test_the_cli_prints_a_refusal_and_exits_two(self) -> None:
        self.certify()
        cfg = self.config({"base_case": "nope"})
        with mock.patch("sys.stderr") as err:
            code = rc.main([SPEC_REF, "--target", "fortran_cpu", "--config", str(cfg),
                            "--out", str(self.tmp / "out"), "--repo-root", str(self.repo)])
        self.assertEqual(code, rc.EXIT_REFUSED)
        self.assertIn("nope", "".join(str(c) for c in err.write.call_args_list))
        code = rc.main([SPEC_REF, "--target", "fortran_cpu", "--config",
                        str(self.config({"base_case": "m0"})), "--out", str(self.tmp / "ok"),
                        "--repo-root", str(self.repo)])
        self.assertEqual(code, 0)

    def test_a_binary_that_exits_non_zero_fails_the_run(self) -> None:
        self.certify(exe_bytes=b"#!/bin/sh\nexit 3\n")
        self.assertEqual(self.run_tool({"base_case": "m0"}), rc.EXIT_RAN_FAILED)
        rec = self.record()
        self.assertEqual((rec["status"], rec["exit_status"]), ("failed", 3))

    def test_the_timeout_kills_the_run(self) -> None:
        self.certify(exe_bytes=b"#!/bin/sh\nsleep 60 &\nwait\n")
        self.assertEqual(self.run_tool({"base_case": "m0"}, timeout_sec=0.5),
                         rc.EXIT_RAN_FAILED)
        rec = self.record()
        self.assertTrue(rec["timed_out"])
        self.assertEqual(rec["status"], "failed")
        self.assertIsNone(rec["exit_status"])

    def test_the_run_takes_the_target_s_launch_environment(self) -> None:
        from tools.host_execution import launch_shape
        from tools.target_profile import load_target_profile
        self.certify(exe_bytes=b"#!/bin/sh\nenv > env.txt\n" + FINAL)
        self.assertEqual(self.run_tool({"base_case": "m0"}), 0)
        want = launch_shape(load_target_profile(self.repo, "fortran_cpu")).env
        self.assertTrue(want)
        got = dict(line.split("=", 1) for line in
                   (self.tmp / "out" / "env.txt").read_text().splitlines() if "=" in line)
        for key, value in want.items():
            self.assertEqual(got.get(key), value, key)
        self.assertEqual(self.record()["launch_env"], dict(sorted(want.items())))

    def test_the_run_loads_from_the_library_directories_its_build_recorded(self) -> None:
        """Issue #644: the run is given the Build's `binary_meta.json#library_dirs` ahead of the
        inherited loader path, and the record carries the directories, never the joined value."""
        refs = self.certify(exe_bytes=b"#!/bin/sh\nenv > env.txt\n" + FINAL)
        meta = self.repo / refs["binary_meta"]
        doc = json.loads(meta.read_text())
        doc["library_dirs"] = ["/opt/zz/lib"]
        meta.write_text(json.dumps(doc))
        with mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": "/inherited"}):
            self.assertEqual(self.run_tool({"base_case": "m0"}), 0)
        got = dict(line.split("=", 1) for line in
                   (self.tmp / "out" / "env.txt").read_text().splitlines() if "=" in line)
        self.assertEqual(got.get("LD_LIBRARY_PATH"), "/opt/zz/lib:/inherited")
        self.assertEqual(self.record()["library_dirs"], ["/opt/zz/lib"])

    # --- refusals ---------------------------------------------------------------------------

    def test_configuration_refusals(self) -> None:
        self.certify()
        rows = [
            ({"base_case": "nope"}, "not a case of the certified IR"),
            ({"base_case": "m0", "inputs": {"grid.ny": 3}}, "not a key"),
            ({"base_case": "m0", "inputs": {"grid": 3}}, "only a numeric leaf"),
            ({"base_case": "m0", "inputs": {"profile": 3}}, "only a numeric leaf"),
            ({"base_case": "m0", "inputs": {"n_step": 4.0}}, "type change"),
            ({"base_case": "m0", "inputs": {"t_start": 1}}, "type change"),
            ({"base_case": "m0", "inputs": {"n_step": True}}, "not an integer or a finite real"),
            ({"base_case": "m0", "inputs": {"t_end": float("nan")}},
             "not an integer or a finite real"),
            ({"base_case": "m0", "inputs": {"t_start": float("inf")}},
             "not an integer or a finite real"),
            ({"base_case": "m0", "inputs": {"n_step": [1]}}, "not an integer or a finite real"),
            ({"base_case": "m0", "inputs": {"n_step": 8}}, "within 4 ulp"),
            ({"base_case": "m0", "inputs": {"unused": 8}}, "changes nothing the binary reads"),
            ({"base_case": "m0", "inputs": {"n_step": 8, "t_end": 3.0, "unused": 1}},
             "inputs.unused"),
            ({"base_case": "m0", "extra": 1}, "a mapping of `base_case`"),
            ({"inputs": {}}, "a mapping of `base_case`"),
            ("[1, 2]", "a mapping of `base_case`"),
            ({"base_case": "m0", "inputs": {"a..b": 1}}, "not a dotted path"),
        ]
        for i, (doc, why) in enumerate(rows):
            with self.subTest(doc=doc):
                self.assertIn(why, self.refused(doc, f"out{i}"))

    def test_a_continuation_case_is_no_base(self) -> None:
        ir, binary = _Built.get("loop")
        ir = json.loads(json.dumps(ir))
        ir["case"]["test_case_set"].append(
            {"case_id": "m0_cont", "inputs": ir["case"]["test_case_set"][0]["inputs"],
             "derived": {"dt": 0.25}, "restart": {"from": "m0", "step": 2}})
        refs = certify_node(self.repo, "orch_seed", NODE, ir_text=yaml.safe_dump(ir),
                            exe_bytes=binary, ir_id="drv-toy_20260101_001",
                            pipeline_id="drv-toy_20260101_001")
        meta = self.repo / refs["binary_meta"]
        doc = json.loads(meta.read_text())
        doc["environment"] = {"build_site": {"site": "local", "host": None}}
        meta.write_text(json.dumps(doc))
        self.assertIn("is a continuation case", self.refused({"base_case": "m0_cont"}))

    def test_out_under_workspace_or_not_empty_is_refused(self) -> None:
        self.certify()
        with self.assertRaises(rc.ConfigurationRefused) as ctx:
            rc.run_configuration(self.repo, SPEC_REF, "fortran_cpu",
                                 self.config({"base_case": "m0"}),
                                 self.repo / "workspace" / "mine")
        self.assertIn("under", str(ctx.exception))
        self.assertFalse((self.repo / "workspace" / "mine").exists())
        (self.tmp / "busy").mkdir()
        (self.tmp / "busy" / "x").write_text("x")
        with self.assertRaises(rc.ConfigurationRefused) as ctx:
            self.run_tool({"base_case": "m0"}, "busy")
        self.assertIn("not an empty directory", str(ctx.exception))
        (self.tmp / "empty").mkdir()
        self.assertEqual(self.run_tool({"base_case": "m0"}, "empty"), 0)

    def test_selection_refusals(self) -> None:
        # nothing certified
        from tools.tests.orchestration_fixtures import ensure_spec_entry
        from tools.tests.target_fixtures import install_target_profile
        ensure_spec_entry(self.repo, NODE)
        install_target_profile(self.repo)
        self.assertIn("no certified Validate output", self.refused({"base_case": "m0"}))
        # built at another site, and built at no recorded site
        for site, shown in (("far", "'far'"), (None, "None")):
            with self.subTest(site=site):
                shutil.rmtree(self.repo / "workspace", ignore_errors=True)
                self.certify(build_site=site)
                why = self.refused({"base_case": "m0"})
                self.assertIn(f"built at site {shown}", why)
        # a launcher target
        from tools.target_profile import load_target_profile
        install_target_profile(self.repo, load_target_profile(rc.REPO_ROOT, "fortran_cpu_mpi"))
        with self.assertRaises(rc.ConfigurationRefused) as ctx:
            rc.run_configuration(self.repo, SPEC_REF, "fortran_cpu_mpi",
                                 self.config({"base_case": "m0"}), self.tmp / "mpi")
        self.assertIn("under a launcher", str(ctx.exception))

    # --- continuation -----------------------------------------------------------------------

    def _continue(self, first: str, k: int, out: str, cfg: dict[str, Any] | None = None,
                  final: bool = False) -> int:
        capture = (self.tmp / first / "raw" / "state_snapshots"
                   / ("m0.json" if final else f"history/m0__{k}.json"))
        return self.run_tool(cfg or {"base_case": "m0"}, out, restart_from=capture)

    def _assert_equal_runs(self, full: str, cont: str, k: int, n_step: int) -> None:
        self.assertEqual(self.snap(cont, "m0"), self.snap(full, "m0"))
        initial = self.snap(cont, "initial/m0")
        self.assertEqual(initial["step"], float(k))
        for j in range(k + 1, n_step + 1):
            self.assertEqual(self.snap(cont, f"history/m0__{j}"),
                             self.snap(full, f"history/m0__{j}"), j)
        series, base = self.snap(cont, "series/m0"), self.snap(full, "series/m0")
        self.assertEqual(series.pop("t"), base.pop("t"))   # the series' `t` is the final time
        self.assertEqual(sorted(series), sorted(base))
        for name, column in series.items():
            self.assertEqual(column, base[name][k:], name)

    def test_a_continuation_from_a_history_sample_equals_the_uninterrupted_run(self) -> None:
        for kind, n_step in (("loop", 4), ("init", 3)):
            with self.subTest(kind=kind):
                shutil.rmtree(self.repo, ignore_errors=True)
                self.repo.mkdir()
                self.certify(kind)
                full, cont, chain = f"{kind}_full", f"{kind}_cont", f"{kind}_chain"
                self.assertEqual(self.run_tool({"base_case": "m0"}, full), 0)
                self.assertEqual(self._continue(full, 1, cont), 0)
                self._assert_equal_runs(full, cont, 1, n_step)
                rec = self.record(cont)
                self.assertEqual(rec["start_check"], "held")
                self.assertEqual(rec["restart"]["step"], 1)
                self.assertEqual(rec["restart"]["source_kind"], "history")
                case = json.loads((self.tmp / cont / "case_inputs" / "m0.json").read_text())
                self.assertEqual(case["restart.step"], 1)
                self.assertEqual(case["restart.u"], self.snap(full, "history/m0__1")["u"])
                # a chain: the continuation's own sample continues again
                self.assertEqual(self._continue(cont, 2, chain), 0)
                self._assert_equal_runs(full, chain, 2, n_step)

    def test_a_continuation_with_an_override_equals_that_configuration_s_run(self) -> None:
        self.certify()
        cfg = {"base_case": "m0", "inputs": {"n_step": 8, "t_end": 3.0}}
        self.assertEqual(self.run_tool(cfg, "full"), 0)
        self.assertEqual(self._continue("full", 5, "cont", cfg), 0)
        self._assert_equal_runs("full", "cont", 5, 8)

    def test_restart_refusals(self) -> None:
        self.certify()
        self.assertEqual(self.run_tool({"base_case": "m0"}, "full"), 0)
        hist = self.tmp / "full" / "raw" / "state_snapshots" / "history"
        # another configuration than the source's
        why = self.refused({"base_case": "m0", "inputs": {"n_step": 8, "t_end": 3.0}},
                           "x1", restart_from=hist / "m0__2.json")
        self.assertIn("differs from this one in ['case_file', 'inputs']", why)
        # a final capture: K = n_step
        with self.assertRaises(rc.ConfigurationRefused) as ctx:
            self._continue("full", 4, "x2", final=True)
        self.assertIn("not below n_step 4", str(ctx.exception))
        # a capture of another case, and one not laid out by a run
        shutil.copy(hist / "m0__2.json", hist / "m1__2.json")
        self.assertIn("not a capture of 'm0'",
                      self.refused({"base_case": "m0"}, "x3", restart_from=hist / "m1__2.json"))
        loose = self.tmp / "m0.json"
        shutil.copy(hist / "m0__2.json", loose)
        self.assertIn("not under <run>/raw/state_snapshots/",
                      self.refused({"base_case": "m0"}, "x4", restart_from=loose))
        # a run directory with no record of this tool
        other = self.tmp / "other" / "raw" / "state_snapshots" / "history"
        other.mkdir(parents=True)
        shutil.copy(hist / "m0__2.json", other)
        self.assertIn("run_record.json is unreadable",
                      self.refused({"base_case": "m0"}, "x5",
                                   restart_from=other / "m0__2.json"))
        # the continuation writer's refusals: a missing restart variable, other extents
        for name, edit, why in (
                ("m0__1.json", lambda d: d.pop("u"), "lacks the restart variable"),
                ("m0__3.json", lambda d: d.update(u=[1.0, 2.0]), "has extents [2]")):
            with self.subTest(name=name):
                path = hist / name
                doc = json.loads(path.read_text())
                edit(doc)
                path.write_text(json.dumps(doc))
                self.assertIn(why, self.refused({"base_case": "m0"}, f"x_{name}",
                                                restart_from=path))
        # a time that is not t_start + K * dt, and a step that is no integer >= 1
        path = hist / "m0__2.json"
        doc = json.loads(path.read_text())
        for edit, why in (({"t": doc["t"] + 0.125}, "within 4 ulp"),
                          ({"step": 0}, "not an integer >= 1")):
            with self.subTest(edit=edit):
                path.write_text(json.dumps({**doc, **edit}))
                self.assertIn(why, self.refused({"base_case": "m0"}, f"x_{next(iter(edit))}",
                                                restart_from=path))

    def test_a_restart_on_a_node_whose_init_writes_n_step_is_bounded_by_the_driver(self) -> None:
        """No host `n_step`: a final capture (K = n_step) passes the tool's checks, and the
        driver refuses `restart.step` outside `0 .. n_step - 1` at run time."""
        self.certify("init")
        self.assertEqual(self.run_tool({"base_case": "m0"}, "full"), 0)
        self.assertEqual(self._continue("full", 3, "cont", final=True), rc.EXIT_RAN_FAILED)
        self.assertNotEqual(self.record("cont")["exit_status"], 0)

    def test_a_driver_without_the_restart_read_fails_the_start_check(self) -> None:
        """The instrument control: the binary re-runs the base from step 0, exits 0, and only
        the host's start check refuses the run."""
        self.certify(restart_read=False)
        self.assertEqual(self.run_tool({"base_case": "m0"}, "full"), 0)
        self.assertEqual(self._continue("full", 2, "cont"), rc.EXIT_RAN_FAILED)
        rec = self.record("cont")
        self.assertEqual(rec["exit_status"], 0)
        self.assertEqual(rec["status"], "failed")
        self.assertIn("did not start at step 2", rec["start_check"])
        self.assertEqual(self.snap("cont", "m0"), self.snap("full", "m0"))


    # --- review round 1 ---------------------------------------------------------------------

    def _certify_ir(self, ir: dict[str, Any], binary: bytes) -> None:
        refs = certify_node(self.repo, "orch_seed", NODE, ir_text=yaml.safe_dump(ir),
                            exe_bytes=binary, ir_id="drv-toy_20260101_001",
                            pipeline_id="drv-toy_20260101_001")
        (self.repo / refs["exe_ref"]).chmod(0o755)
        meta = self.repo / refs["binary_meta"]
        doc = json.loads(meta.read_text())
        doc["environment"] = {"build_site": {"site": "local", "host": None}}
        meta.write_text(json.dumps(doc))

    def _sample(self, run: str, k: int) -> Path:
        return self.tmp / run / "raw" / "state_snapshots" / "history" / f"m0__{k}.json"

    def _refused_at(self, out: Path) -> str:
        with self.assertRaises(rc.ConfigurationRefused) as ctx:
            rc.run_configuration(self.repo, SPEC_REF, "fortran_cpu",
                                 self.config({"base_case": "m0"}), out)
        return str(ctx.exception)

    def test_a_restart_on_an_ir_that_declares_its_own_continuation(self) -> None:
        """The certified restart nodes each carry a continuation case; its own restart point is
        not this run's, at its K or at another."""
        ir, binary = _Built.get("loop")
        ir = copy.deepcopy(ir)
        ir["case"]["test_case_set"].append(
            {"case_id": "m0_cont",
             "inputs": copy.deepcopy(ir["case"]["test_case_set"][0]["inputs"]),
             "derived": {"dt": 0.25}, "restart": {"from": "m0", "step": 2}})
        self._certify_ir(ir, binary)
        self.assertEqual(self.run_tool({"base_case": "m0"}, "full"), 0)
        for k in (1, 2, 3):
            with self.subTest(k=k):
                self.assertEqual(self._continue("full", k, f"cont{k}"), 0)
                self._assert_equal_runs("full", f"cont{k}", k, 4)

    def test_a_restart_source_of_another_node_target_ir_binary_or_case_file(self) -> None:
        self.certify()
        self.assertEqual(self.run_tool({"base_case": "m0"}, "full"), 0)
        rec_path = self.tmp / "full" / rc.RUN_RECORD
        good = rec_path.read_text()
        case_path = self.tmp / "full" / "case_inputs" / "m0.json"
        case = case_path.read_text()
        rows = [("node_key", "component/other@0.1.0"), ("target_id", "fortran_cpu_t2"),
                ("ir_ref", "workspace/ir/x/y"), ("binary_sha256", "0" * 64)]
        for key, value in rows:
            with self.subTest(key=key):
                rec_path.write_text(json.dumps({**json.loads(good), key: value}))
                self.assertIn(f"differs from this one in ['{key}']",
                              self.refused({"base_case": "m0"}, f"r_{key}",
                                           restart_from=self._sample("full", 2)))
        rec_path.write_text(json.dumps({**json.loads(good), "configuration": {
            **json.loads(good)["configuration"], "base_case": "m1"}}))
        self.assertIn("['base_case']", self.refused({"base_case": "m0"}, "r_base",
                                                    restart_from=self._sample("full", 2)))
        # the same configuration under an IR whose base case evaluates otherwise
        rec_path.write_text(good)
        case_path.write_text(json.dumps({**json.loads(case), "inputs.mode": 1}))
        self.assertIn("['case_file']", self.refused({"base_case": "m0"}, "r_case",
                                                    restart_from=self._sample("full", 2)))
        # a chain: the source's own restart.* keys are not compared
        case_path.write_text(json.dumps({**json.loads(case), "restart.step": 2,
                                         "restart.u": [0.0, 0.0, 0.0]}))
        self.assertEqual(self._continue("full", 2, "chain_ok"), 0)

    def test_signals_kill_the_binary_s_process_group_and_the_record_says_so(self) -> None:
        """A `kill` or a closed terminal ends the tool; the binary and a child it started die
        with it, and the record is not left `running`."""
        script = (b"#!/bin/sh\n" b"sleep 120 &\n" b"echo $! > grandchild.pid\n"
                  b"echo $$ > binary.pid\n" b"wait\n")
        self.certify(exe_bytes=script)
        cfg = self.config({"base_case": "m0"})
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            with self.subTest(sig=sig):
                out = self.tmp / f"sig{int(sig)}"
                proc = subprocess.Popen(
                    [sys.executable, str(rc.REPO_ROOT / "tools" / "run_configuration.py"),
                     SPEC_REF, "--target", "fortran_cpu", "--config", str(cfg), "--out",
                     str(out), "--repo-root", str(self.repo)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                self.addCleanup(_reap_proc, proc)
                for name in ("binary.pid", "grandchild.pid"):
                    self.addCleanup(_reap_file, out / name)
                deadline = time.monotonic() + 60
                while not ((out / "grandchild.pid").is_file()
                           and (out / "binary.pid").is_file()) \
                        and time.monotonic() < deadline and proc.poll() is None:
                    time.sleep(0.05)
                pids = [int((out / n).read_text()) for n in ("binary.pid", "grandchild.pid")]
                self.addCleanup(_reap, pids)
                proc.send_signal(sig)
                proc.communicate(timeout=60)
                self.assertEqual(proc.returncode, 128 + int(sig))
                for pid in pids:
                    self.assertFalse(_alive(pid), pid)
                rec = json.loads((out / rc.RUN_RECORD).read_text())
                self.assertEqual(rec["status"], "interrupted")
                self.assertEqual(rec["signal"], int(sig))

    def test_the_timeout_kills_a_child_the_binary_started(self) -> None:
        self.certify(exe_bytes=b"#!/bin/sh\nsleep 120 &\necho $! > grandchild.pid\nwait\n")
        self.assertEqual(self.run_tool({"base_case": "m0"}, timeout_sec=1.0),
                         rc.EXIT_RAN_FAILED)
        self.assertFalse(_alive(int((self.tmp / "out" / "grandchild.pid").read_text())))

    def test_a_binary_that_cannot_start_is_recorded_as_not_started(self) -> None:
        self.certify(exe_bytes=b"\x00\x01not an executable")
        self.assertEqual(self.run_tool({"base_case": "m0"}), rc.EXIT_REFUSED)
        rec = self.record()
        self.assertEqual(rec["status"], "not_started")
        self.assertIn("Exec format error", rec["error"])

    def test_a_kernel_that_refuses_the_configuration_fails_the_run(self) -> None:
        """The toy kernel refuses mode 1 before its last step and exits 0: the run did not reach
        its end, so it is not `ok`."""
        self.certify()
        self.assertEqual(self.run_tool({"base_case": "m0", "inputs": {"mode": 1}}),
                         rc.EXIT_RAN_FAILED)
        rec = self.record()
        self.assertEqual(rec["exit_status"], 0)
        self.assertEqual(rec["status"], "failed")
        self.assertIn("the kernel refused the configuration: ok false", rec["kernel"])
        self.assertLess(rec["final_step"], 4)
        self.assertEqual(self.run_tool({"base_case": "m0"}, "fine"), 0)
        self.assertEqual(self.record("fine")["kernel"], "reached its end")
        self.assertEqual(self.record("fine")["final_step"], 4.0)

    def test_an_override_a_min_reads_is_not_a_no_op(self) -> None:
        """A derived `min()` whose other branch binds still reads both inputs; overriding one or
        both is a configuration, not a no-op."""
        ir, binary = _Built.get("loop")
        ir = copy.deepcopy(ir)
        for case in ir["case"]["test_case_set"]:
            case["inputs"]["cfl"] = {"a": 0.25, "b": 0.5}
            case["derived"]["dt"] = "min(inputs.cfl.a, inputs.cfl.b)"
        self._certify_ir(ir, binary)
        for i, inputs in enumerate(({"cfl.b": 0.75},
                                    {"cfl.a": 0.125, "cfl.b": 0.125, "n_step": 8,
                                     "t_end": 2.0})):
            with self.subTest(inputs=inputs):
                self.assertEqual(self.run_tool({"base_case": "m0", "inputs": inputs},
                                               f"o{i}"), 0)
        self.assertIn("changes nothing the binary reads",
                      self.refused({"base_case": "m0", "inputs": {"unused": 8}}, "o_unused"))

    def test_more_refusals(self) -> None:
        self.certify()
        self.assertIn("1.0e-3", self.refused("base_case: m0\ninputs:\n  t_start: 1e-3\n"))
        self.assertIn("inputs must be a mapping",
                      self.refused({"base_case": "m0", "inputs": [1]}, "o1"))
        (self.tmp / "afile").write_text("x")
        self.assertIn("not an empty directory", self._refused_at(self.tmp / "afile"))
        self.assertEqual((self.tmp / "afile").read_text(), "x")
        self.assertIn("under", self._refused_at(self.repo / "workspace"))
        self.assertEqual(self.run_tool({"base_case": "m0"}, "full"), 0)
        path = self._sample("full", 2)
        path.write_text(json.dumps({**json.loads(path.read_text()), "step": 1.5}))
        self.assertIn("not an integer >= 1",
                      self.refused({"base_case": "m0"}, "o2", restart_from=path))

    def test_a_node_without_drive_or_without_loop(self) -> None:
        self.certify("call")
        self.assertEqual(self.run_tool({"base_case": "m0"}, "full"), 0)
        self.assertIn("declares no loop", self.refused(
            {"base_case": "m0"}, "o1",
            restart_from=self.tmp / "full" / "raw" / "state_snapshots" / "m0.json"))
        shutil.rmtree(self.repo)
        self.repo.mkdir()
        ir, binary = _Built.get("loop")
        ir = copy.deepcopy(ir)
        del ir["drive"]
        self._certify_ir(ir, binary)
        self.assertIn("declares no drive", self.refused({"base_case": "m0"}, "o2"))

    def test_the_site_is_the_one_sites_yaml_maps_the_target_to(self) -> None:
        self.certify(exe_bytes=b"#!/bin/sh\n" + FINAL)
        (self.repo / "sites.yaml").write_text(yaml.safe_dump({
            "sites_version": 1,
            "sites": {"far": {"host": "far.example", "workdir": "/w", "executes": ["cpu"],
                              "scheduler": "none"}},
            "targets": {"fortran_cpu": "far"}}))
        self.assertIn("runs at site far", self.refused({"base_case": "m0"}))
        (self.repo / "sites.yaml").write_text("sites: [")
        self.assertIn("sites.yaml", self.refused({"base_case": "m0"}, "o2"))
        (self.repo / "sites.yaml").unlink()
        self.assertEqual(self.run_tool({"base_case": "m0"}, "o3"), 0)

    def test_a_device_target_at_a_local_site_that_executes_it(self) -> None:
        """The launch shape is the one at the site `sites.yaml` maps the target to: a local
        site that executes the device class runs a device target (the default local site
        does not)."""
        from tools.target_profile import load_target_profile
        gpu = load_target_profile(rc.REPO_ROOT, "cpp_gpu")
        ir, _ = _Built.get("loop")
        refs = certify_node(self.repo, "orch_seed", NODE, ir_text=yaml.safe_dump(ir),
                            exe_bytes=b"#!/bin/sh\n" + FINAL, ir_id="drv-toy_20260101_001",
                            pipeline_id="drv-toy_20260101_001", target=gpu)
        (self.repo / refs["exe_ref"]).chmod(0o755)
        meta = self.repo / refs["binary_meta"]
        doc = json.loads(meta.read_text())
        doc["environment"] = {"build_site": {"site": "local", "host": None}}
        meta.write_text(json.dumps(doc))

        def run(out: str) -> int:
            return rc.run_configuration(self.repo, SPEC_REF, "cpp_gpu",
                                        self.config({"base_case": "m0"}), self.tmp / out)
        with self.assertRaises(rc.ConfigurationRefused) as ctx:
            run("o1")
        self.assertIn("not executed at site local", str(ctx.exception))
        (self.repo / "sites.yaml").write_text(yaml.safe_dump({
            "sites_version": 1, "sites": {"local": {"executes": ["cpu", "gpu"]}}}))
        self.assertEqual(run("o2"), 0)
        self.assertEqual(self.record("o2")["command"][0], str(self.repo / refs["exe_ref"]))

    def test_the_selection_refusal_names_the_resolver_s_reason(self) -> None:
        from tools.tests.orchestration_fixtures import ensure_spec_entry
        from tools.tests.target_fixtures import install_target_profile
        ensure_spec_entry(self.repo, NODE)
        install_target_profile(self.repo)
        why = self.refused({"base_case": "m0"})
        self.assertRegex(why, r"on target fortran_cpu: \S+")

    def test_the_record_carries_the_restart_source_s_hash_and_kind(self) -> None:
        import hashlib
        self.certify("init")
        self.assertEqual(self.run_tool({"base_case": "m0"}, "full"), 0)
        sample = self._sample("full", 1)
        self.assertEqual(self._continue("full", 1, "cont"), 0)
        rec = self.record("cont")["restart"]
        self.assertEqual(rec["sha256"], hashlib.sha256(sample.read_bytes()).hexdigest())
        self.assertEqual((rec["source_kind"], rec["step"]), ("history", 1))
        self.assertEqual(rec["source_run"], str((self.tmp / "full").resolve()))
        ir_files = list((self.repo / "workspace" / "ir" / "component__drv_toy__0.1.0")
                        .rglob("spec.ir.yaml"))
        self.assertEqual(len(ir_files), 1)
        self.assertEqual((self.tmp / "cont" / "spec.ir.yaml").read_bytes(),
                         ir_files[0].read_bytes())

    # --- review round 2 ---------------------------------------------------------------------

    def test_a_result_bound_to_a_state_of_another_name(self) -> None:
        """`result: ok` with `arguments.ok: state.valid`: the flag read is `valid` (Codex, round
        2; the dummy's name was read, absent from the capture, and the refusal passed as ok)."""
        self.certify("valid")
        self.assertEqual(rc.result_states(_Built.get("valid")[0], "m0"), ["valid"])
        self.assertEqual(self.run_tool({"base_case": "m0", "inputs": {"mode": 1}}),
                         rc.EXIT_RAN_FAILED)
        self.assertIn("valid false", self.record()["kernel"])
        self.assertEqual(self.run_tool({"base_case": "m0"}, "fine"), 0)

    def test_a_missing_final_capture_or_result_fails_the_run(self) -> None:
        for name, script, why in (
                ("none", b"#!/bin/sh\nexit 0\n", "no readable final capture"),
                ("noresult", b'#!/bin/sh\necho \'{"step": 4.0}\' > raw/state_snapshots/m0.json\n',
                 "the final capture holds no ok"),
                ("notobject", b"#!/bin/sh\necho '[1]' > raw/state_snapshots/m0.json\n",
                 "not a JSON object")):
            with self.subTest(name=name):
                shutil.rmtree(self.repo)
                self.repo.mkdir()
                self.certify(exe_bytes=script)
                self.assertEqual(self.run_tool({"base_case": "m0"}, name), rc.EXIT_RAN_FAILED)
                rec = self.record(name)
                self.assertEqual((rec["exit_status"], rec["status"]), (0, "failed"))
                self.assertIn(why, rec["kernel"])

    def test_a_signal_while_the_binary_is_being_started(self) -> None:
        """SIGTERM between the fork and the binding of the process: held until the `try` that
        kills the group, so the binary does not outlive the tool."""
        self.certify(exe_bytes=b"#!/bin/sh\necho $$ > binary.pid\nexec tail -f /dev/null\n")
        real = subprocess.Popen

        started: list[int] = []

        class Racing(real):  # type: ignore[misc, valid-type]
            def __init__(self, *a: Any, **kw: Any) -> None:
                super().__init__(*a, **kw)
                started.append(self.pid)
                # to this thread: under xdist a process-directed signal can land on the
                # worker's other thread and not interrupt this one's wait (the tool has one)
                signal.pthread_kill(threading.get_ident(), signal.SIGTERM)

        self.addCleanup(_reap, started)   # the list is filled when the binary starts
        with mock.patch.object(subprocess, "Popen", Racing), \
                self.assertRaises(rc.Terminated):
            self.run_tool({"base_case": "m0"})
        self.assertEqual(len(started), 1)
        self.assertFalse(_alive(started[0]))
        rec = self.record()
        self.assertEqual((rec["status"], rec["signal"]), ("interrupted", int(signal.SIGTERM)))
        self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)

    def test_a_signal_after_the_binary_exited_leaves_no_running_record(self) -> None:
        self.certify()
        real = rc._kernel_outcome

        def stopped(*a: Any) -> Any:
            signal.pthread_kill(threading.get_ident(), signal.SIGTERM)
            return real(*a)

        with mock.patch.object(rc, "_kernel_outcome", stopped), \
                self.assertRaises(rc.Terminated):
            self.run_tool({"base_case": "m0"})
        rec = self.record()
        self.assertEqual((rec["status"], rec["exit_status"]), ("interrupted", 0))

    def test_read_input_paths_names_each_reader(self) -> None:
        ir = {"io_contract": {"raw_requirements": {"required_evidence": [
            {"artifact": "state_snapshots", "schema": {"variables": [], "coordinates": [
                {"name": "x", "axis": 0, "placement": "center", "count": "inputs.g.n",
                 "length": "inputs.g.len"},
                {"name": "y", "axis": 0, "placement": "center", "count": 4, "length": 1.0}]}}]}}}
        case = {"initial_state": {"u": "inputs.a.b * x", "w": [1.0]},
                "derived": {"dt": "min(inputs.c, inputs.d)", "k": 0.5}}
        self.assertEqual(rc.read_input_paths(ir, case, {"inputs.e": 1, "derived.dt": 0.1}),
                         {"e", "a.b", "c", "d", "g.n", "g.len"})

    def test_an_unreadable_source_case_file_is_refused(self) -> None:
        self.certify()
        self.assertEqual(self.run_tool({"base_case": "m0"}, "full"), 0)
        (self.tmp / "full" / "case_inputs" / "m0.json").unlink()
        self.assertIn("['case_file']", self.refused({"base_case": "m0"}, "o1",
                                                    restart_from=self._sample("full", 2)))

    def test_falsy_inputs_that_are_no_mapping_are_refused(self) -> None:
        self.certify()
        for i, text in enumerate(("[]", "false", "0", "''")):
            with self.subTest(text=text):
                self.assertIn("inputs must be a mapping",
                              self.refused(f"base_case: m0\ninputs: {text}\n", f"o{i}"))
        self.assertEqual(self.run_tool("base_case: m0\ninputs:\n", "null_ok"), 0)

    def test_an_xfail_only_base_with_an_invalid_dt_is_a_refusal_not_a_traceback(self) -> None:
        from tools import primary_evidence as pe
        ir = copy.deepcopy(_Built.get("loop")[0])
        case = ir["case"]["test_case_set"][0]
        case["derived"]["dt"] = -0.25
        case["inputs"]["t_end"] = 0.0
        ir["io_contract"]["tests"] = [{"test_id": "t", "expected_outcome": "xfail",
                                       "target_cases": ["m0"]}]
        payload = pe.evaluate_case_inputs(ir)["m0"]
        capture = self.tmp / "cap.json"
        capture.write_text(json.dumps({"step": 1, "t": 0.75, "u": [1.0, 2.0, 3.0],
                                       "w": [0.0, 0.0, 0.0]}))
        stage = self.tmp / "stage"
        stage.mkdir()
        with self.assertRaises(rc.ConfigurationRefused) as ctx:
            rc.continuation_payload(ir, "m0", payload, capture, stage)
        self.assertIn("not > 0", str(ctx.exception))

    # --- review round 3 ---------------------------------------------------------------------

    def test_a_restart_from_a_final_capture_records_its_kind(self) -> None:
        self.certify("init")
        self.assertEqual(self.run_tool({"base_case": "m0"}, "full"), 0)
        self.assertEqual(self._continue("full", 3, "cont", final=True), rc.EXIT_RAN_FAILED)
        self.assertEqual(self.record("cont")["restart"]["source_kind"], "final")

    def test_an_override_equal_to_the_base_value_of_a_read_input_runs(self) -> None:
        """Not a no-op refusal: the input is read by name (the plan's rule)."""
        self.certify()
        self.assertEqual(self.run_tool({"base_case": "m0", "inputs": {"n_step": 4}}), 0)
        self.assertEqual(self.record()["status"], "ok")

    def test_the_record_carries_the_binary_s_process_group_and_the_config_path(self) -> None:
        self.certify()
        self.assertEqual(self.run_tool({"base_case": "m0"}), 0)
        rec = self.record()
        self.assertIsInstance(rec["binary_pgid"], int)
        self.assertEqual(rec["configuration"]["file"], str((self.tmp / "config.yaml").resolve()))

    def test_a_continuation_from_a_run_that_is_still_running_is_refused(self) -> None:
        self.certify()
        self.assertEqual(self.run_tool({"base_case": "m0"}, "full"), 0)
        holder = subprocess.Popen(["tail", "-f", "/dev/null"], start_new_session=True)
        self.addCleanup(_reap_proc, holder)
        rec_path = self.tmp / "full" / rc.RUN_RECORD
        rec = json.loads(rec_path.read_text())
        rec_path.write_text(json.dumps({**rec, "status": "running", "binary_pgid": holder.pid}))
        self.assertIn("is still running", self.refused({"base_case": "m0"}, "o1",
                                                       restart_from=self._sample("full", 2)))
        holder.kill()
        holder.wait()
        self.assertEqual(self._continue("full", 2, "o2"), 0)

    def test_sigint_exits_130_without_a_traceback(self) -> None:
        self.certify(exe_bytes=b"#!/bin/sh\necho $$ > binary.pid\nexec tail -f /dev/null\n")
        out = self.tmp / "int"
        proc = subprocess.Popen(
            [sys.executable, str(rc.REPO_ROOT / "tools" / "run_configuration.py"), SPEC_REF,
             "--target", "fortran_cpu", "--config", str(self.config({"base_case": "m0"})),
             "--out", str(out), "--repo-root", str(self.repo)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        self.addCleanup(_reap_proc, proc)
        self.addCleanup(_reap_file, out / "binary.pid")
        deadline = time.monotonic() + 60
        while not (out / "binary.pid").is_file() and time.monotonic() < deadline \
                and proc.poll() is None:
            time.sleep(0.05)
        proc.send_signal(signal.SIGINT)
        _, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 130)
        self.assertNotIn("Traceback", err)
        self.assertFalse(_alive(int((out / "binary.pid").read_text())))


def _reap_proc(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
        proc.wait()


def _reap_file(path: Path) -> None:
    try:
        _reap([int(path.read_text())])
    except (OSError, ValueError):
        pass


def _reap(pids: list[int]) -> None:
    """Kill what a row started, whether it passed or not."""
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _alive(pid: int) -> bool:
    """Whether ``pid`` names a live process that is no zombie, after a bounded settle (a kill
    is delivered asynchronously)."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except (OSError, IndexError):
            return False
        if state == "Z":
            return False
        time.sleep(0.05)
    return True


if __name__ == "__main__":
    unittest.main()
