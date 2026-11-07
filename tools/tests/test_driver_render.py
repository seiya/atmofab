"""The host-rendered driver, compiled and run (issue #589 PR-1b).

`data/driver_e2e/` holds what a run links against: the certified harness writers with hand-written
reading operations (not a certified source, `harness_read_case/` says why), the toy operations of
`drv_toy_model.*`, and the certified `shallow_water2d@0.4.7` model cut to one `__step`. Each row
drives the real `render_runner`, `write_case_inputs`, the compiler and `evaluate_primary_predicates`
or the captures, and observes what the program wrote. The Fortran rows run wherever the suite does;
the C++ rows need `nvcc` and no device (the toy model is host code).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from tools import primary_evidence as pe
from tools import runner_ir, verdict_evaluator
from tools.backends.language.cuda_cpp import header as cpp_header
from tools.backends.language.cuda_cpp import runner as cpp_runner
from tools.backends.language.fortran import runner as f_runner
from tools.host_render import RenderError
from tools.tests import skip_probes

ROOT = Path(__file__).resolve().parents[2]
DATA = Path(__file__).parent / "data" / "driver_e2e"
DRIVE_IR = Path(__file__).parent / "data" / "drive_ir"
CPU = {"hardware": {"class": "cpu"}, "execution": {"threads_per_rank": 1}}
GPU = {"hardware": {"class": "gpu"}, "execution": {"threads_per_rank": 1}}
REAL = {"type": "real", "kind": "dp"}


def _arg(name: str, intent: str, spec: dict[str, Any], dims: list[str] | None = None) -> dict:
    out: dict[str, Any] = {"name": name, "intent": intent, "spec": spec}
    if dims:
        out["dims"] = dims
        out["rank"] = len(dims)
    return out


INT = {"type": "integer"}
LOGICAL = {"type": "logical"}
OPS = {
    "drv_toy__init": [
        _arg("nx", "in", INT), _arg("mode", "in", INT), _arg("n_plan", "out", INT),
        _arg("dt_plan", "out", REAL), _arg("u", "out", REAL, ["nx"]),
        _arg("init_ok", "out", LOGICAL)],
    "drv_toy__step": [
        _arg("nx", "in", INT), _arg("mode", "in", INT), _arg("dt", "in", REAL),
        _arg("t", "in", REAL), _arg("stepno", "in", INT), _arg("u", "inout", REAL, ["nx"]),
        _arg("w", "out", REAL, ["nx"]), _arg("ok", "out", LOGICAL)],
    "drv_toy__apply": [
        _arg("nx", "in", INT), _arg("mode", "in", INT), _arg("a", "in", REAL, ["nx"]),
        _arg("b", "out", REAL, ["nx"]), _arg("c", "inout", REAL, ["nx"]), _arg("s", "out", REAL),
        _arg("ok", "out", LOGICAL)],
    "drv_toy__advance": [
        _arg("nx", "in", INT), _arg("t", "in", REAL), _arg("dt", "in", REAL),
        _arg("u_n", "in", REAL, ["nx"]),
        {"name": "rhs", "spec": {"type": "procedure", "interface": "drv_rhs_proto"}},
        _arg("u_np1", "out", REAL, ["nx"]), _arg("ok", "out", LOGICAL)],
}
PROTOTYPE = {"kind": "subroutine", "name": "drv_rhs_proto", "args": [
    _arg("nx", "in", INT), _arg("t", "in", REAL), _arg("u", "in", REAL, ["nx"]),
    _arg("dudt", "out", REAL, ["nx"])]}


def _api() -> dict[str, Any]:
    return {
        "published_operations": [{"operation_id": op} for op in OPS],
        "module_parameters": [{"name": "dp", "value": "float64"}],
        "interfaces": [{"name": "drv_rhs_proto", "signature": PROTOTYPE}],
        "signatures": [{"symbol": op, "signature": {"kind": "subroutine", "name": op,
                                                    "args": args}}
                       for op, args in OPS.items()]}


def _ir(variables: dict[str, str], drive: dict[str, Any], cases: list[dict[str, Any]]) -> dict:
    return {
        "meta": {"spec_id": "drv_toy", "spec_kind": "component"},
        "public_api": _api(),
        "drive": drive,
        "case": {"test_case_set": cases},
        "io_contract": {"raw_requirements": {"required_evidence": [
            {"artifact": "state_snapshots", "required": True,
             "schema": {"time_variable": "t", "variables": [
                 {"name": n, "shape_expr": s} for n, s in variables.items()]}}]}},
    }


def loop_ir(*modes: int, every: int = 2, n_step: int = 4, dt: float = 0.25) -> dict[str, Any]:
    """A loop node: `u` an inout state, `w` a lone out, `ok` the result; one case per mode."""
    drive = {
        "step": {"operation": "drv_toy__step", "result": "ok", "arguments": {
            "nx": "inputs.grid.nx", "mode": "inputs.mode", "dt": "derived.dt", "t": "time",
            "stepno": "step", "u": "state.u", "w": "state.w", "ok": "state.ok"}},
        "loop": {"n_step": "inputs.n_step", "dt": "derived.dt", "t_start": "inputs.t_start",
                 "t_end": "inputs.t_end"},
        "history": {"every": "inputs.every"}}
    cases = [{"case_id": f"m{m}", "derived": {"dt": dt},
              "inputs": {"grid": {"nx": 3}, "mode": m, "n_step": n_step, "t_start": 1.0,
                         "t_end": 1.0 + n_step * dt, "every": every},
              "initial_state": {"u": [1.0, 2.0, 3.0], "w": 0.0}} for m in modes]
    return _ir({"u": "[nx]", "w": "[nx]", "ok": "scalar"}, drive, cases)


def init_ir(*modes: int) -> dict[str, Any]:
    """A loop node whose `init` writes the loop scalars and the state."""
    drive = {
        "init": {"operation": "drv_toy__init", "result": "init_ok", "arguments": {
            "nx": "inputs.grid.nx", "mode": "inputs.mode", "n_plan": "state.n_plan",
            "dt_plan": "state.dt_plan", "u": "state.u", "init_ok": "state.init_ok"}},
        "step": {"operation": "drv_toy__step", "result": "ok", "arguments": {
            "nx": "inputs.grid.nx", "mode": "inputs.mode", "dt": "state.dt_plan", "t": "time",
            "stepno": "step", "u": "state.u", "w": "state.w", "ok": "state.ok"}},
        "loop": {"n_step": "state.n_plan", "dt": "state.dt_plan", "t_start": "inputs.t_start",
                 "t_end": "inputs.t_end"},
        "history": {"every": "inputs.every"}}
    cases = [{"case_id": f"m{m}", "inputs": {"grid": {"nx": 3}, "mode": m, "t_start": 0.0,
                                             "t_end": 0.75, "every": 0},
              "initial_state": {"w": -5.0}} for m in modes]
    return _ir({"u": "[nx]", "w": "[nx]", "n_plan": "scalar", "dt_plan": "scalar",
                "init_ok": "scalar", "ok": "scalar"}, drive, cases)


def call_ir(*modes: int) -> dict[str, Any]:
    """A one-call node: `a` and `c` read, `b` an output with a pre-call literal, `s` its scalar."""
    drive = {"step": {"operation": "drv_toy__apply", "result": "ok", "arguments": {
        "nx": "inputs.grid.nx", "mode": "inputs.mode", "a": "state.a", "b": "state.b",
        "c": "state.c", "s": "state.s", "ok": "state.ok"}}}
    cases = [{"case_id": f"m{m}", "inputs": {"grid": {"nx": 3}, "mode": m},
              "initial_state": {"a": [1.0, 2.0, 3.0], "b": -999.0, "c": [5.0, 5.0, 5.0],
                                "s": -7.0}} for m in modes]
    return _ir({"a": "[nx]", "b": "[nx]", "c": "[nx]", "s": "scalar", "ok": "scalar"},
               drive, cases)


def procedure_ir(expr: str = "inputs.lam * u + sin(t) - 2 ** 2 / max(abs(u), 3.0)",
                 dt: float = 0.5) -> dict[str, Any]:
    """A one-call node over a procedure argument, with the driver's call records."""
    drive = {"step": {"operation": "drv_toy__advance", "result": "ok", "arguments": {
        "nx": "inputs.grid.nx", "t": "inputs.t", "dt": "inputs.dt", "u_n": "state.u_n",
        "rhs": {"procedure": expr, "calls": "state.n_calls",
                "record": {"t": "state.stage_t", "u": ["state.stage_u1", "state.stage_u2"]}},
        "u_np1": "state.u_np1", "ok": "state.ok"}}}
    cases = [{"case_id": "c", "inputs": {"grid": {"nx": 3}, "t": 0.25, "dt": dt, "lam": -2.0},
              "initial_state": {"u_n": [1.0, 2.0, 4.0], "u_np1": -9.0}}]
    return _ir({"stage_t": "[2]", "u_n": "[nx]", "u_np1": "[nx]", "ok": "scalar",
                "n_calls": "scalar", "stage_u1": "[nx]", "stage_u2": "[nx]"}, drive, cases)


def init_call_ir(*modes: int) -> dict[str, Any]:
    """A one-call node whose `init` writes the state the operation reads."""
    drive = {
        "init": {"operation": "drv_toy__init", "result": "init_ok", "arguments": {
            "nx": "inputs.grid.nx", "mode": "inputs.mode", "n_plan": "state.n_plan",
            "dt_plan": "state.dt_plan", "u": "state.a", "init_ok": "state.init_ok"}},
        "step": {"operation": "drv_toy__apply", "result": "ok", "arguments": {
            "nx": "inputs.grid.nx", "mode": "inputs.apply_mode", "a": "state.a", "b": "state.b",
            "c": "state.c", "s": "state.s", "ok": "state.ok"}}}
    cases = [{"case_id": f"m{m}", "inputs": {"grid": {"nx": 3}, "mode": m, "apply_mode": 0},
              "initial_state": {"b": -999.0, "c": [5.0, 5.0, 5.0], "s": -7.0}} for m in modes]
    return _ir({"a": "[nx]", "b": "[nx]", "c": "[nx]", "s": "scalar", "n_plan": "scalar",
                "dt_plan": "scalar", "init_ok": "scalar", "ok": "scalar"}, drive, cases)


def init_host_loop_ir(*modes: int) -> dict[str, Any]:
    """`init` beside a loop whose count is host data: a refused `init` still takes no step."""
    ir = init_ir(*modes)
    ir["drive"]["loop"]["n_step"] = "inputs.n_step"
    for case in ir["case"]["test_case_set"]:
        case["inputs"]["n_step"] = 3
    return ir


def list_ir(*modes: int) -> dict[str, Any]:
    """The list form: each case to its own entry."""
    ir = call_ir(*modes)
    entry = ir["drive"]["step"]
    ir["drive"]["step"] = [{**entry, "cases": [f"m{m}"]} for m in modes]
    return ir


class Run:
    """One driver built and run in a temporary directory; `snapshot(name)` reads a capture."""

    def __init__(self, directory: Path, proc: subprocess.CompletedProcess) -> None:
        self.dir = directory
        self.proc = proc
        self.snapshots = directory / "run" / "raw" / "state_snapshots"

    def snapshot(self, name: str) -> dict[str, Any]:
        return json.loads((self.snapshots / f"{name}.json").read_text())

    def perf(self) -> dict[str, Any]:
        return json.loads((self.dir / "run" / "perf.json").read_text())


def _prepare(tmp: Path, ir: dict[str, Any], case_ids: list[str] | None) -> list[str]:
    (tmp / "ir").mkdir()
    (tmp / "ir" / "spec.ir.yaml").write_text(yaml.safe_dump(ir))
    pe.write_case_inputs(ir, tmp / "ir")
    for sub in ("initial", "history", "series"):
        (tmp / "run" / "raw" / "state_snapshots" / sub).mkdir(parents=True)
    return case_ids if case_ids is not None else [c["case_id"] for c in ir["case"]["test_case_set"]]


def _exec(tmp: Path, ids: list[str], env: dict[str, str] | None = None) -> Run:
    proc = subprocess.run([str(tmp / "bin"), "--cases", str(tmp / "ir" / "spec.ir.yaml"), *ids],
                          cwd=tmp / "run", capture_output=True, text=True, timeout=300,
                          env={**os.environ, "OMP_NUM_THREADS": "4", **(env or {})})
    return Run(tmp, proc)


def run_fortran(tmp: Path, ir: dict[str, Any], sid: str = "drv_toy",
                deps: tuple[str, ...] = (), case_ids: list[str] | None = None,
                model: str | None = None) -> Run:
    ids = _prepare(tmp, ir, case_ids)
    text = f_runner.render_runner(ir, sid, "harness_fortran_cpu", target=CPU)
    (tmp / "runner.f90").write_text(text)
    objs = []
    flags = ["-std=f2008", "-O2", "-fopenmp", "-fcheck=bounds"]
    for stem in ("harness_fortran_cpu", *deps, model or sid):
        obj = f"{stem}.o"
        subprocess.run(["gfortran", *flags, "-c", str(DATA / f"{stem}_model.f90"), "-o", obj],
                       cwd=tmp, check=True, capture_output=True)
        objs.append(obj)
    build = subprocess.run(["gfortran", *flags, "-c", "runner.f90", "-o", "runner.o"], cwd=tmp,
                           capture_output=True, text=True)
    if build.returncode != 0:
        raise AssertionError(build.stderr[:3000] + text[:3000])
    subprocess.run(["gfortran", *flags, *objs, "runner.o", "-o", "bin"], cwd=tmp, check=True,
                   capture_output=True)
    return _exec(tmp, ids)


def _harness_public_api() -> dict[str, Any]:
    spec = (ROOT / "spec/infrastructure/infra/harness/harness_cpp_gpu/controlled_spec.md").read_text()
    block = yaml.safe_load(spec.split("### 5.1", 1)[1].split("```yaml", 1)[1].split("```", 1)[0])
    return {"module_parameters": block["module_parameters"],
            "signatures": [{"symbol": t["name"], "signature": t} for t in block["types"]]
            + [{"symbol": p["name"], "signature": p} for p in block["procedures"]]}


def run_cpp(tmp: Path, ir: dict[str, Any], sid: str = "drv_toy") -> Run:
    ids = _prepare(tmp, ir, None)
    (tmp / "runner.cu").write_text(cpp_runner.render_runner(ir, sid, "harness_cpp_gpu", target=GPU))
    (tmp / f"{sid}_model.cuh").write_text(
        cpp_header.render(sid, ir["public_api"], spec_kind="component"))
    (tmp / "harness_cpp_gpu_model.cuh").write_text(
        cpp_header.render("harness_cpp_gpu", _harness_public_api(), spec_kind="infrastructure"))
    shutil.copy(DATA / "harness_cpp_gpu_model.cu", tmp)
    shutil.copy(DATA / f"{sid}_model.cu", tmp)
    objs = []
    for stem in ("harness_cpp_gpu_model", f"{sid}_model", "runner"):
        build = subprocess.run(["nvcc", "-std=c++17", "-O1", "-c", f"{stem}.cu", "-o", f"{stem}.o"],
                               cwd=tmp, capture_output=True, text=True)
        if build.returncode != 0:
            raise AssertionError(stem + build.stdout[:2000] + build.stderr[:2000])
        objs.append(f"{stem}.o")
    subprocess.run(["nvcc", *objs, "-o", "bin"], cwd=tmp, check=True, capture_output=True)
    return _exec(tmp, ids, {"ATMOFAB_CUDA_BLOCK": "64"})


class _DriverBehaviour:
    """One language's rows; `build_and_run(tmp, ir)` builds and runs. What each row observes is the captures."""

    build_and_run: Any

    def setUp(self) -> None:  # noqa: N802
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _run(self, ir: dict[str, Any]) -> Run:
        run = type(self).build_and_run(Path(self._tmp.name), ir)
        self.assertEqual(run.proc.returncode, 0, run.proc.stderr[-2000:])
        return run

    def test_block_first_renders_one_on_the_serial_driver(self) -> None:
        """Issue #610 (B5): `block.<axis>.first` is the block's first global index, `1` on a
        serial or device driver. The toy kernel refuses exactly `mode == 1` (`ok = mode /= 1`),
        so `mode` bound to `block.x.first` captures what the literal-1 case captures, and not
        what the mode-0 case does."""
        def run(ir: dict[str, Any], case: str) -> dict[str, Any]:
            with tempfile.TemporaryDirectory() as tmp:
                got = type(self).build_and_run(Path(tmp), ir)
                self.assertEqual(got.proc.returncode, 0, got.proc.stderr[-2000:])
                return got.snapshot(case)

        ir = call_ir(0)
        ir["drive"]["distribute"] = {"axes": [{"name": "x", "cells": "inputs.grid.nx"}]}
        ir["drive"]["step"]["arguments"]["mode"] = "block.x.first"
        bound = run(ir, "m0")
        self.assertEqual(bound["ok"], 0.0)
        self.assertEqual(bound["b"], [2.0, 4.0, 6.0])
        one = run(call_ir(1), "m1")
        self.assertEqual({k: v for k, v in bound.items() if k != "case_id"},
                         {k: v for k, v in one.items() if k != "case_id"})
        self.assertEqual(run(call_ir(0), "m0")["ok"], 1.0)

    def test_a_loop_runs_n_step_committed_steps_with_step_and_t_the_drivers(self) -> None:
        run = self._run(loop_ir(0))
        final = run.snapshot("m0")
        self.assertEqual(final["step"], 4.0)
        self.assertEqual(final["t"], 2.0)
        self.assertEqual(final["u"], [2.0, 3.0, 4.0])
        self.assertEqual(final["ok"], 1.0)
        initial = run.snapshot("initial/m0")
        self.assertEqual(initial["step"], 0.0)
        self.assertEqual(initial["t"], 1.0)
        self.assertEqual(initial["ok"], 0.0)
        self.assertEqual(initial["w"], [0.0, 0.0, 0.0])
        self.assertEqual(run.perf()["steps"], 4)
        self.assertEqual(run.perf()["cells_updated"], 12)

    def test_samples_land_every_every_steps_as_flat_files_with_their_own_step(self) -> None:
        run = self._run(loop_ir(0, every=2))
        names = sorted(p.name for p in (run.snapshots / "history").iterdir())
        self.assertEqual(names, ["m0__2.json", "m0__4.json"])
        sample = run.snapshot("history/m0__2")
        self.assertEqual(sample["step"], 2.0)
        self.assertEqual(sample["t"], 1.5)
        self.assertEqual(sample["u"], [1.5, 2.5, 3.5])
        self.assertEqual(sample["w"], [1.5, 2.5, 3.5])

    def test_every_zero_writes_no_sample(self) -> None:
        run = self._run(loop_ir(0, every=0))
        self.assertEqual(list((run.snapshots / "history").iterdir()), [])

    def test_the_series_holds_one_entry_per_committed_step_of_every_array(self) -> None:
        run = self._run(loop_ir(0))
        env = pe._load_series(run.snapshots, "m0")
        self.assertEqual(sorted(env), sorted(["step", "u_min", "u_max", "w_min", "w_max"]))
        self.assertEqual(env["step"].tolist(), [1.0, 2.0, 3.0, 4.0])
        self.assertEqual(env["u_min"].tolist(), [1.25, 1.5, 1.75, 2.0])
        self.assertEqual(env["u_max"].tolist(), [3.25, 3.5, 3.75, 4.0])

    def test_a_refused_step_commits_nothing_of_what_it_wrote(self) -> None:
        run = self._run(loop_ir(1))
        final = run.snapshot("m1")
        self.assertEqual(final["ok"], 0.0)
        self.assertEqual(final["step"], 2.0)
        self.assertEqual(final["t"], 1.5)
        self.assertEqual(final["u"], [1.5, 2.5, 3.5])
        self.assertEqual(pe._load_series(run.snapshots, "m1")["step"].tolist(), [1.0, 2.0])
        self.assertEqual(run.perf()["steps"], 2)

    def test_a_refusal_at_the_first_step_leaves_the_initial_state_byte_equal(self) -> None:
        run = self._run(loop_ir(3))
        final, initial = run.snapshot("m3"), run.snapshot("initial/m3")
        self.assertEqual(final["u"], initial["u"])
        self.assertEqual(final["w"], initial["w"])
        self.assertEqual(final["step"], 0.0)
        self.assertEqual(final["t"], initial["t"])
        self.assertEqual(pe._load_series(run.snapshots, "m3")["step"].tolist(), [])

    def test_a_never_assigned_result_reads_zero(self) -> None:
        run = self._run(loop_ir(3))
        self.assertEqual(run.snapshot("m3")["ok"], 0.0)

    def test_a_result_is_reset_before_every_call(self) -> None:
        """A true result of the case before must not stand for a kernel that assigns none."""
        run = self._run(call_ir(0, 3))
        self.assertEqual(run.snapshot("m0")["ok"], 1.0)
        self.assertEqual(run.snapshot("m3")["ok"], 0.0)
        self.assertEqual(run.snapshot("m3")["step"], 0.0)

    def test_t_is_t_start_plus_n_dt_not_an_accumulation(self) -> None:
        final = self._run(loop_ir(0, every=0, n_step=10, dt=0.1)).snapshot("m0")
        self.assertEqual(final["t"], 1.0 + 10 * 0.1)
        accumulated = 1.0
        for _ in range(10):
            accumulated += 0.1
        self.assertNotEqual(final["t"], accumulated)

    def test_the_series_carries_the_time_of_the_last_committed_state(self) -> None:
        run = self._run(loop_ir(1))
        doc = json.loads((run.snapshots / "series" / "m1.json").read_text())
        self.assertEqual(doc["t"], 1.5)

    def test_walltime_is_summed_over_the_step_calls(self) -> None:
        perf = self._run(loop_ir(0, every=0, n_step=500)).perf()
        self.assertNotEqual(perf["walltime_sec"], 1.0e-9)
        self.assertGreater(perf["walltime_sec"], 0.0)

    def test_cells_updated_multiply_the_largest_state_array(self) -> None:
        self.assertEqual(self._run(procedure_ir()).perf()["cells_updated"], 3)

    def test_a_refused_init_on_a_one_call_node_takes_no_call(self) -> None:
        run = self._run(init_call_ir(10))
        final = run.snapshot("m10")
        self.assertEqual(final["init_ok"], 0.0)
        self.assertEqual(final["ok"], 0.0)
        self.assertEqual(final["step"], 0.0)
        self.assertEqual(final["b"], [-999.0] * 3)

    def test_an_accepted_init_on_a_one_call_node_feeds_the_call(self) -> None:
        run = self._run(init_call_ir(0))
        self.assertEqual(run.snapshot("initial/m0")["a"], [1.0] * 3)
        self.assertEqual(run.snapshot("m0")["b"], [2.0] * 3)

    def test_a_refused_init_takes_no_step_when_the_count_is_host_data(self) -> None:
        run = self._run(init_host_loop_ir(10, 0))
        self.assertEqual(run.snapshot("m10")["step"], 0.0)
        self.assertEqual(run.snapshot("m0")["step"], 3.0)

    def test_an_init_written_array_is_reallocated_for_each_case(self) -> None:
        run = self._run(init_ir(0, 5))
        self.assertEqual(run.snapshot("initial/m5")["u"], [1.0] * 3)

    def test_an_init_written_array_takes_each_case_s_extents(self) -> None:
        ir = init_ir(0, 5)
        ir["case"]["test_case_set"][1]["inputs"]["grid"]["nx"] = 5
        run = self._run(ir)
        self.assertEqual(len(run.snapshot("m0")["u"]), 3)
        self.assertEqual(len(run.snapshot("m5")["u"]), 5)

    def test_an_integer_input_authored_as_a_whole_float_runs(self) -> None:
        ir = call_ir(0)
        ir["case"]["test_case_set"][0]["inputs"]["grid"]["nx"] = 3.0
        self.assertEqual(self._run(ir).snapshot("m0")["b"], [2.0, 4.0, 6.0])

    def test_the_list_form_runs_each_case_through_its_entry(self) -> None:
        run = self._run(list_ir(0, 1))
        self.assertEqual(run.snapshot("m0")["ok"], 1.0)
        self.assertEqual(run.snapshot("m1")["ok"], 0.0)

    def test_a_case_no_entry_runs_stops_the_program(self) -> None:
        run = self._run(list_ir(0))
        source = run.dir / "ir" / "case_inputs" / "m0.json"
        shutil.copy(source, source.with_name("other.json"))
        second = _exec(run.dir, ["other"])
        self.assertNotEqual(second.proc.returncode, 0)
        self.assertIn("no drive entry", second.proc.stdout + second.proc.stderr)

    def test_more_calls_than_recorded_slots_are_counted_and_not_stored(self) -> None:
        ir = procedure_ir()
        variables = ir["io_contract"]["raw_requirements"]["required_evidence"][0]["schema"][
            "variables"]
        next(v for v in variables if v["name"] == "stage_t")["shape_expr"] = "[1]"
        final = self._run(ir).snapshot("c")
        self.assertEqual(final["n_calls"], 2.0)
        self.assertEqual(final["stage_t"], [0.25])

    def test_an_unassigned_cell_of_a_lone_out_reaches_the_capture_as_nan(self) -> None:
        run = self._run(loop_ir(2))
        doc = json.loads((run.snapshots / "m2.json").read_text())
        self.assertNotEqual(doc["w"][0], doc["w"][0])
        self.assertEqual(doc["w"][1:], [3.0, 4.0])
        with self.assertRaises(pe.PrimaryEvidenceError):
            pe._load_capture(run.snapshots / "m2.json", {"w": ["nx"]}, "t")

    def test_the_fold_carries_a_committed_nan_into_the_series_and_the_loader_refuses_it(self) -> None:
        run = self._run(loop_ir(4))
        doc = json.loads((run.snapshots / "series" / "m4.json").read_text())
        self.assertEqual(doc["u_min"][0], 1.25)
        self.assertNotEqual(doc["u_min"][1], doc["u_min"][1])
        self.assertNotEqual(doc["u_max"][1], doc["u_max"][1])
        with self.assertRaises(pe.PrimaryEvidenceError):
            pe._load_series(run.snapshots, "m4")

    def test_a_refused_init_leaves_finite_captures_and_runs_no_step(self) -> None:
        run = self._run(init_ir(10))
        initial, final = run.snapshot("initial/m10"), run.snapshot("m10")
        self.assertEqual(initial["init_ok"], 0.0)
        self.assertEqual(final["init_ok"], 0.0)
        self.assertEqual(final["step"], 0.0)
        self.assertEqual(final["ok"], 0.0)
        self.assertEqual(final["w"], [-5.0, -5.0, -5.0])
        self.assertEqual(pe._load_series(run.snapshots, "m10")["step"].tolist(), [])

    def test_init_writes_the_loop_scalars_the_driver_runs_by(self) -> None:
        run = self._run(init_ir(0))
        initial, final = run.snapshot("initial/m0"), run.snapshot("m0")
        self.assertEqual(initial["n_plan"], 3.0)
        self.assertEqual(initial["dt_plan"], 0.25)
        self.assertEqual(initial["init_ok"], 1.0)
        self.assertEqual(final["step"], 3.0)
        self.assertEqual(final["t"], 0.75)

    def test_init_planning_no_step_or_no_dt_stops_the_run_before_a_capture(self) -> None:
        for mode in (11, 12):
            with self.subTest(mode=mode):
                tmp = Path(self._tmp.name) / f"m{mode}"
                tmp.mkdir()
                run = type(self).build_and_run(tmp, init_ir(mode))
                self.assertNotEqual(run.proc.returncode, 0)
                self.assertFalse((run.snapshots / "initial" / f"m{mode}.json").exists())

    def test_an_invalid_host_dt_is_the_kernels_to_refuse_not_the_drivers(self) -> None:
        ir = loop_ir(5, n_step=1)
        case = ir["case"]["test_case_set"][0]
        case["derived"]["dt"] = -0.5
        # a case only `xfail` tests target may carry the dt its kernel's refusal is the subject of
        ir["io_contract"]["tests"] = [{"test_id": "t", "expected_outcome": "xfail",
                                       "target_cases": ["m5"]}]
        run = self._run(ir)
        self.assertEqual(run.snapshot("m5")["u"], [-0.5, -0.5, -0.5])

    def test_a_case_with_no_file_stops_the_run_and_writes_nothing(self) -> None:
        tmp = Path(self._tmp.name)
        run = self._run(loop_ir(0))
        for path in run.snapshots.rglob("*.json"):
            path.unlink()
        (run.dir / "run" / "perf.json").unlink()
        second = _exec(tmp, ["m0", "no_such_case"])
        self.assertNotEqual(second.proc.returncode, 0)
        self.assertFalse((run.dir / "run" / "perf.json").exists())
        self.assertFalse((run.snapshots / "no_such_case.json").exists())

    def test_a_name_the_case_does_not_hold_stops_the_run_before_a_capture(self) -> None:
        run = self._run(loop_ir(0))
        for path in run.snapshots.rglob("*.json"):
            path.unlink()
        path = run.dir / "ir" / "case_inputs" / "m0.json"
        doc = json.loads(path.read_text())
        del doc["derived.dt"]
        path.write_text(json.dumps(doc))
        second = _exec(run.dir, ["m0"])
        self.assertNotEqual(second.proc.returncode, 0)
        self.assertFalse((run.snapshots / "initial" / "m0.json").exists())

    def test_one_call_runs_on_the_state_itself(self) -> None:
        run = self._run(call_ir(0))
        final = run.snapshot("m0")
        self.assertEqual(final["b"], [2.0, 4.0, 6.0])
        self.assertEqual(final["c"], [6.0, 6.0, 6.0])
        self.assertEqual(final["s"], 6.0)
        self.assertEqual(final["ok"], 1.0)
        self.assertEqual(final["step"], 1.0)
        self.assertEqual(run.snapshot("initial/m0")["b"], [-999.0] * 3)

    def test_a_one_call_kernel_that_writes_then_refuses_leaves_its_writes_in_the_capture(self) -> None:
        final = self._run(call_ir(1)).snapshot("m1")
        self.assertEqual(final["c"], [6.0, 6.0, 6.0])
        self.assertEqual(final["ok"], 0.0)
        self.assertEqual(final["step"], 0.0)

    def test_what_a_one_call_kernel_never_assigns_reads_its_initial_state_content(self) -> None:
        final = self._run(call_ir(2)).snapshot("m2")
        self.assertEqual(final["b"], [-999.0] * 3)
        self.assertEqual(final["s"], -7.0)
        self.assertEqual(final["ok"], 1.0)

    def test_a_one_call_result_never_assigned_reads_zero(self) -> None:
        final = self._run(call_ir(3)).snapshot("m3")
        self.assertEqual(final["ok"], 0.0)
        self.assertEqual(final["step"], 0.0)

    def test_the_state_of_one_case_does_not_reach_the_next(self) -> None:
        run = self._run(call_ir(0, 2))
        self.assertEqual(run.snapshot("m2")["b"], [-999.0] * 3)
        self.assertEqual(run.snapshot("m2")["c"], [5.0] * 3)


    def _heun(self, expr: str, u: np.ndarray, t: float, dt: float) -> tuple[np.ndarray, ...]:
        """The host's reading of the same expression, through the predicate evaluator."""
        def rhs(tv: float, uv: np.ndarray) -> np.ndarray:
            env = pe.CaseEnv(case_id="c", initial=None, final={}, inputs={"lam": -2.0},
                             coordinates={"u": uv, "t": np.float64(tv)})
            return np.broadcast_to(pe.evaluate(pe.parse_expr(expr), env), uv.shape)
        k1 = rhs(t, u)
        stage = u + dt * k1
        return u + 0.5 * dt * (k1 + rhs(t + dt, stage)), stage

    def test_a_procedure_binding_agrees_with_the_hosts_reading_and_records_its_calls(self) -> None:
        expr = ("inputs.lam * u + sin(t) - 2 ** 2 / max(abs(u), 3.0) + sqrt(exp(log(abs(u) + pi)))"
                " + (-cos(t)) * min(u, e, 2.0)")
        run = self._run(procedure_ir(expr))
        final = run.snapshot("c")
        u = np.array([1.0, 2.0, 4.0])
        expected, stage = self._heun(expr, u, 0.25, 0.5)
        np.testing.assert_allclose(final["u_np1"], expected, rtol=1e-14)
        self.assertEqual(final["n_calls"], 2.0)
        self.assertEqual(final["stage_t"], [0.25, 0.75])
        self.assertEqual(final["stage_u1"], [1.0, 2.0, 4.0])
        np.testing.assert_allclose(final["stage_u2"], stage, rtol=1e-14)
        self.assertEqual(run.snapshot("initial/c")["n_calls"], 0.0)
        self.assertEqual(run.snapshot("initial/c")["stage_t"], [0.0, 0.0])

    def test_a_refusing_operation_leaves_the_records_at_zero(self) -> None:
        final = self._run(procedure_ir("u", dt=-1.0)).snapshot("c")
        self.assertEqual(final["n_calls"], 0.0)
        self.assertEqual(final["stage_t"], [0.0, 0.0])
        self.assertEqual(final["ok"], 0.0)


class FortranDriverTest(_DriverBehaviour, unittest.TestCase):
    build_and_run = staticmethod(run_fortran)


@skip_probes.skip_unless_installed("cuda_compiler_driver")
class CppDriverTest(_DriverBehaviour, unittest.TestCase):
    build_and_run = staticmethod(run_cpp)


class ResultResetOptimisationTest(unittest.TestCase):
    """gfortran clobbers a plain `intent(out)` scalar actual at the call, so the driver's reset is a
    dead store unless the actual is `volatile`: a kernel that never assigns its result would read
    `true`. Measured to depend on the flags, so each flag set a build can use is run here."""

    def _ok_after_true_case(self, flags: list[str]) -> float:
        ir = call_ir(0, 3)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ids = _prepare(root, ir, None)
            (root / "runner.f90").write_text(
                f_runner.render_runner(ir, "drv_toy", "harness_fortran_cpu", target=CPU))
            objs = []
            for stem in ("harness_fortran_cpu", "drv_toy"):
                subprocess.run(["gfortran", *flags, "-c", str(DATA / f"{stem}_model.f90"), "-o",
                                f"{stem}.o"], cwd=root, check=True, capture_output=True)
                objs.append(f"{stem}.o")
            subprocess.run(["gfortran", *flags, "-c", "runner.f90", "-o", "r.o"], cwd=root,
                           check=True, capture_output=True)
            subprocess.run(["gfortran", *flags, *objs, "r.o", "-o", "bin"], cwd=root, check=True,
                           capture_output=True)
            run = _exec(root, ids)
            self.assertEqual(run.proc.returncode, 0, run.proc.stderr[-1000:])
            return run.snapshot("m3")["ok"]

    def test_a_never_assigned_result_reads_zero_under_every_flag_set(self) -> None:
        for flags in (["-std=f2008", "-O0"], ["-std=f2008", "-O2"], ["-std=f2008", "-O3"],
                      ["-std=f2008", "-O2", "-fcheck=bounds"],
                      ["-std=f2008", "-O2", "-fopenmp"]):
            with self.subTest(flags=flags):
                self.assertEqual(self._ok_after_true_case(flags), 0.0)

    def test_the_scalars_an_out_dummy_takes_are_volatile(self) -> None:
        text = f_runner.render_runner(call_ir(0), "drv_toy", "harness_fortran_cpu", target=CPU)
        self.assertIn("logical, volatile :: lg_ok", text)
        self.assertIn("real(dp), volatile :: st_s", text)


class FortranTokenWidthTest(unittest.TestCase):
    def test_an_argument_past_the_token_width_stops_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = run_fortran(Path(tmp), call_ir(0))
            self.assertEqual(run.proc.returncode, 0, run.proc.stderr[-500:])
            long = "x" * (f_runner.TOKEN_LEN + 1)
            second = _exec(Path(tmp), [long])
            self.assertNotEqual(second.proc.returncode, 0)
            self.assertIn("exceeds the token width", second.proc.stderr)


class CertifiedModelEndToEndTest(unittest.TestCase):
    """The driver over a certified kernel: every test's status is the certified run's, which
    is every test's `expected_outcome` on these two nodes (issue #589 PR-1b's end to end; the
    swe2d model is the certified one cut to `__step`, `data/driver_e2e/`)."""

    def _verdict(self, ir_path: Path, sid: str, deps: tuple[str, ...]) -> dict[str, Any]:
        ir = yaml.safe_load(ir_path.read_text())
        with tempfile.TemporaryDirectory() as tmp:
            run = run_fortran(Path(tmp), ir, sid=sid, deps=deps)
            self.assertEqual(run.proc.returncode, 0, run.proc.stderr[-2000:])
            records = pe.evaluate_primary_predicates(ir, Path(tmp) / "run")
            verdict = verdict_evaluator.evaluate_verdict(ir["io_contract"]["tests"], records)
            expected = {t["test_id"]: t["expected_outcome"] for t in ir["io_contract"]["tests"]}
            self.assertEqual({t["test_id"]: t["status"] for t in verdict["per_test"]}, expected)
            self.assertEqual(verdict["self_verdict"], "pass")
            perf = run.perf()
        return perf

    def test_coriolis_one_call(self) -> None:
        perf = self._verdict(DRIVE_IR / "coriolis_0.2.0.spec.ir.yaml",
                             "dynamics_shallow_water_source_2d_coriolis", ())
        self.assertEqual(perf["steps"], 7)

    def test_shallow_water2d_loop(self) -> None:
        perf = self._verdict(DRIVE_IR / "shallow_water2d_0.4.7" / "spec.ir.yaml",
                             "shallow_water2d",
                             ("dynamics_shallow_water_boundary_2d_periodic_copy",
                              "dynamics_shallow_water_flux_2d_rusanov_p0",
                              "dynamics_shallow_water_time_update_2d_ssprk2"))
        # the figures the certified run recorded (2026-11-03)
        self.assertEqual((perf["steps"], perf["cells_updated"]), (9546, 89930752))


@skip_probes.skip_unless_installed("cuda_compiler_driver")
class CppGpuPackageCompilesTest(unittest.TestCase):
    """The package the `cpp_gpu` end to end runs at a site (`data/driver_e2e/package_cpp_gpu.py`)
    compiles here. Compiled for the device with no device: the run itself is the site's, recorded
    on the pull request."""

    def test_each_node_compiles(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("package_cpp_gpu",
                                                      DATA / "package_cpp_gpu.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for node in module.NODES:
            with self.subTest(node=node), tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp)
                stems = module.package(out, node)
                build = subprocess.run(
                    ["nvcc", "-std=c++17", "-O1", "-rdc=true", "-I.", "-c", f"{stems[-1]}.cu", "-o", "r.o"],
                    cwd=out, capture_output=True, text=True)
                self.assertEqual(build.returncode, 0, build.stdout[-1500:] + build.stderr[-1500:])
                for stem in stems[:-1]:
                    built = subprocess.run(
                        ["nvcc", "-std=c++17", "-O1", "-rdc=true", "-I.", "-c", f"{stem}.cu", "-o",
                         f"{stem}.o"], cwd=out, capture_output=True, text=True)
                    self.assertEqual(built.returncode, 0, stem + built.stderr[-1500:])


class ProblemModelGateTest(unittest.TestCase):
    """The canonical `__step` shape passes the `problem` dataflow gates: `intent(inout)` state and
    one logical `intent(out)` result set from literals (issue #589 D3)."""

    DEPS = ["dynamics_shallow_water_boundary_2d_periodic_copy",
            "dynamics_shallow_water_flux_2d_rusanov_p0",
            "dynamics_shallow_water_time_update_2d_ssprk2"]

    def _violations(self, text: str) -> list[str]:
        from tools.backends.language.fortran import source
        found: list[str] = []
        source.model_source_gates(
            node_key="problem/shallow_water2d@0.4.7", model_file=Path("m.f90"), text=text,
            dep_spec_ids=self.DEPS, violations=found, multidim_spec_id="shallow_water2d")
        return found

    def test_the_step_fixture_passes(self) -> None:
        self.assertEqual(self._violations((DATA / "shallow_water2d_model.f90").read_text()), [])

    def test_a_step_that_discards_every_dependency_result_is_refused(self) -> None:
        text = (DATA / "shallow_water2d_model.f90").read_text()
        cut = text.replace("h(i, j) = 0.5_dp * u_n(1, i, j) + 0.5_dp * u_2(1, i, j)", "h(i, j) = g")
        cut = cut.replace("hu(i, j) = 0.5_dp * u_n(2, i, j) + 0.5_dp * u_2(2, i, j)", "hu(i, j) = g")
        cut = cut.replace("hv(i, j) = 0.5_dp * u_n(3, i, j) + 0.5_dp * u_2(3, i, j)", "hv(i, j) = g")
        self.assertNotEqual(cut, text)
        self.assertTrue(any("does not propagate" in v for v in self._violations(cut)))


class RenderRefusalTest(unittest.TestCase):
    def test_a_distributed_harness_has_no_driver_yet(self) -> None:
        with self.assertRaises(RenderError) as caught:
            f_runner.render_runner(loop_ir(0), "drv_toy", "harness_fortran_cpu_mpi", target=CPU)
        self.assertIn(f_runner.DRIVER_DISTRIBUTED_PENDING, str(caught.exception))
        self.assertTrue(caught.exception.identity)

    def test_an_ir_with_no_drive_is_refused_as_content(self) -> None:
        ir = loop_ir(0)
        del ir["drive"]
        for module, harness, target in ((f_runner, "harness_fortran_cpu", CPU),
                                        (cpp_runner, "harness_cpp_gpu", GPU)):
            with self.assertRaises(RenderError) as caught:
                module.render_runner(ir, "drv_toy", harness, target=target)
            self.assertFalse(caught.exception.identity)

    def test_the_shapes_the_renderers_have_no_spelling_for_are_refused(self) -> None:
        def integer_array(ir: dict) -> None:
            ir["public_api"]["signatures"][0]["signature"]["args"][2]["spec"] = INT

        def logical_in_state(ir: dict) -> None:
            args = ir["public_api"]["signatures"][0]["signature"]["args"]
            args[2]["spec"] = LOGICAL
            args[2].pop("dims"), args[2].pop("rank")

        def string_dummy(ir: dict) -> None:
            ir["public_api"]["signatures"][0]["signature"]["args"][1]["spec"] = {
                "type": "string", "len": "assumed"}

        def result_not_logical(ir: dict) -> None:
            ir["drive"]["step"]["result"] = "s"

        def unknown_extent(ir: dict) -> None:
            ir["public_api"]["signatures"][0]["signature"]["args"][2]["dims"] = ["nope"]

        def history_without_loop(ir: dict) -> None:
            ir["drive"]["history"] = {"every": "inputs.mode"}

        for mutate, ir_of in ((integer_array, lambda: call_ir(0)),
                              (logical_in_state, lambda: call_ir(0)),
                              (string_dummy, lambda: call_ir(0)),
                              (result_not_logical, lambda: call_ir(0)),
                              (history_without_loop, lambda: call_ir(0))):
            with self.subTest(mutate=mutate.__name__):
                ir = ir_of()
                ir["public_api"]["signatures"] = [
                    s for s in ir["public_api"]["signatures"] if s["symbol"] == "drv_toy__apply"]
                ir["public_api"]["signatures"] = json.loads(
                    json.dumps(ir["public_api"]["signatures"]))
                mutate(ir)
                with self.assertRaises(RenderError):
                    f_runner.render_runner(ir, "drv_toy", "harness_fortran_cpu", target=CPU)
        ir = init_ir(0)
        ir["public_api"] = json.loads(json.dumps(ir["public_api"]))
        init_sig = next(s for s in ir["public_api"]["signatures"]
                        if s["symbol"] == "drv_toy__init")["signature"]
        next(a for a in init_sig["args"] if a["name"] == "u")["dims"] = ["nope"]
        with self.assertRaisesRegex(RenderError, "extent token 'nope'"):
            f_runner.render_runner(ir, "drv_toy", "harness_fortran_cpu", target=CPU)

    def test_a_host_rendered_procedure_binds_what_it_does_not_read(self) -> None:
        """The promoted unused-dummy warning would refuse a file no leaf can edit."""
        ir = procedure_ir("inputs.lam * u")
        rhs = ir["drive"]["step"]["arguments"]["rhs"]
        del rhs["record"], rhs["calls"]
        schema = ir["io_contract"]["raw_requirements"]["required_evidence"][0]["schema"]
        schema["variables"] = [v for v in schema["variables"]
                               if v["name"] in ("u_n", "u_np1", "ok")]
        text = f_runner.render_runner(ir, "drv_toy", "harness_fortran_cpu", target=CPU)
        self.assertIn("unused_t => t", text)
        with tempfile.TemporaryDirectory() as tmp:
            run = run_fortran(Path(tmp), ir)
            self.assertEqual(run.proc.returncode, 0, run.proc.stderr[-500:])
            subprocess.run(
                ["gfortran", "-std=f2008", "-c", "-Wall", "-Werror=unused-dummy-argument",
                 "-Wno-unused-variable", "runner.f90", "-o", "w.o"], cwd=tmp, check=True,
                capture_output=True)

    def test_the_two_readers_of_a_procedure_binding_agree(self) -> None:
        self.assertEqual(runner_ir.PROCEDURE_FUNCTIONS, pe.PROCEDURE_FUNCTIONS)
        raw = {"procedure": " inputs.lam * u ", "calls": "state.n",
               "record": {"t": "state.st", "u": ["state.a", "state.b"]}}
        kind, (text, calls, record) = runner_ir.drive_binding(raw)
        got = pe.parse_binding(raw)
        self.assertEqual((kind, text, calls, record), (got.kind, got.ref, got.calls, got.record))
        tree = runner_ir._procedure_tree("inputs.a.b + inputs.c * u - inputs.a.b")
        names = [r.name for r in pe.expr_names(pe.parse_expr("inputs.a.b + inputs.c * u"))
                 if r.kind == "inputs"]
        self.assertEqual(sorted(runner_ir.procedure_input_paths(tree)), sorted(names))


class ProcedureGateTest(unittest.TestCase):
    """Gate (b) of `{procedure: expr}` and the case file's half of it (amendment 1)."""

    def _violations(self, ir: dict[str, Any]) -> list[str]:
        block, v = pe._parse_drive(ir)
        self.assertIsNotNone(block, v)
        schema = pe.snapshot_schema(ir)
        variables = pe.schema_variables(schema)
        out: list[str] = []
        pe._binding_violations(ir, pe._model_from_block(block, variables), variables, out)
        return out

    def _binding(self, ir: dict[str, Any]) -> dict[str, Any]:
        return ir["drive"]["step"]["arguments"]["rhs"]

    def test_the_inputs_the_expression_reads_are_in_the_case_file(self) -> None:
        payload = pe.evaluate_case_inputs(procedure_ir("inputs.lam * u"))["c"]
        self.assertEqual(payload["inputs.lam"], -2.0)

    def test_a_clean_binding_passes_and_its_records_need_no_initial_state(self) -> None:
        ir = procedure_ir()
        self.assertEqual(self._violations(ir), [])
        model = pe.drive_model(ir)
        self.assertEqual(model.records, {"n_calls", "stage_t", "stage_u1", "stage_u2"})
        self.assertFalse(model.needs_initial() & model.records)
        self.assertTrue(model.records <= model.written)

    def test_refusals(self) -> None:
        for mutate, needle in (
            (lambda b: b.update(procedure="final.u * 2"), "reads"),
            (lambda b: b.update(procedure="roll(u, 1)"), "not elementwise"),
            (lambda b: b.update(procedure="nx * u"), "reads 'nx'"),
            (lambda b: b.update(calls="state.u_n"), "not a scalar"),
            (lambda b: b.update(record={"nope": "state.stage_t"}), "not an in dummy"),
            (lambda b: b.update(record={"t": "state.ok"}), "one rank-1 state"),
            (lambda b: b.update(record={"u": ["state.stage_t", "state.ok"]}), "has rank"),
        ):
            with self.subTest(needle=needle):
                ir = procedure_ir()
                mutate(self._binding(ir))
                found = self._violations(ir)
                self.assertTrue(any(needle in x for x in found), found)

    def test_an_array_input_in_an_expression_is_refused(self) -> None:
        ir = procedure_ir("inputs.lam * u")
        ir["case"]["test_case_set"][0]["inputs"]["lam"] = [1.0, 2.0, 3.0]
        with self.assertRaisesRegex(pe.PrimaryEvidenceError, "scalar inputs"):
            pe.evaluate_case_inputs(ir)

    def test_a_state_listed_as_a_record_takes_no_initial_state(self) -> None:
        ir = procedure_ir()
        ir["case"]["test_case_set"][0]["initial_state"]["n_calls"] = 0.0
        with self.assertRaisesRegex(pe.PrimaryEvidenceError, "call record"):
            pe.evaluate_case_inputs(ir)


if __name__ == "__main__":
    unittest.main()
