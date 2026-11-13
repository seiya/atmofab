"""`tools/run_configuration.py` (issue #632 P5): a person-written configuration run on a node's
certified binary, outside the workflow.

Each row seeds a certified chain (`orchestration_fixtures.certify_node`) whose IR is a toy loop
node of `test_driver_render` and whose binary is that IR's driver, rendered by the real
`render_runner` and built with the toy model. So the selection is the workflow's own resolver
(`DerivationResolver.select`), the evaluation is the host's evaluator, the run is the real
binary, and a continuation is the real driver's restart read under the real start check.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock

import yaml

from tools import run_configuration as rc
from tools.backends.language.fortran import runner as f_runner
from tools.tests.orchestration_fixtures import certify_node
from tools.tests.test_driver_render import init_ir, loop_ir, run_fortran

NODE = "component/drv_toy@0.1.0"
SPEC_REF = "spec/component/drv_toy"


def _toy_ir(kind: str) -> dict[str, Any]:
    """The loop node with host-evaluated loop scalars (`loop`), or the one whose `init` writes
    `n_step` and `dt` (`init`); each case carries an unread numeric leaf and a string leaf."""
    ir = loop_ir(0, 1, every=1, n_step=4) if kind == "loop" else init_ir(0)
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
        refs = certify_node(self.repo, "orch_seed", NODE, ir_text=yaml.safe_dump(ir),
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
        self.certify(exe_bytes=b"#!/bin/sh\nenv > env.txt\n")
        self.assertEqual(self.run_tool({"base_case": "m0"}), 0)
        want = launch_shape(load_target_profile(self.repo, "fortran_cpu")).env
        self.assertTrue(want)
        got = dict(line.split("=", 1) for line in
                   (self.tmp / "out" / "env.txt").read_text().splitlines() if "=" in line)
        for key, value in want.items():
            self.assertEqual(got.get(key), value, key)
        self.assertEqual(self.record()["launch_env"], dict(sorted(want.items())))

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
            ({"base_case": "m0", "inputs": {"unused": float("inf")}},
             "not an integer or a finite real"),
            ({"base_case": "m0", "inputs": {"n_step": [1]}}, "not an integer or a finite real"),
            ({"base_case": "m0", "inputs": {"n_step": 8}}, "within 4 ulp"),
            ({"base_case": "m0", "inputs": {"unused": 8}}, "changes nothing the binary reads"),
            ({"base_case": "m0", "inputs": {"n_step": 4}}, "changes nothing the binary reads"),
            ({"base_case": "m0", "inputs": {"n_step": 8, "t_end": 3.0, "unused": 1}},
             "inputs.unused"),
            ({"base_case": "m0", "extra": 1}, "a mapping of `base_case`"),
            ({"inputs": {}}, "a mapping of `base_case`"),
            ("[1, 2]", "a mapping of `base_case`"),
            ({"base_case": "m0", "inputs": {"a..b": 1}}, "not a dotted path"),
        ]
        for doc, why in rows:
            with self.subTest(doc=doc):
                self.assertIn(why, self.refused(doc))

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
        self.assertIn("differs from this one in ['inputs']", why)
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


if __name__ == "__main__":
    unittest.main()
