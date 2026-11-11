"""The host-rendered driver, compiled and run (issue #589 PR-1b).

`data/driver_e2e/` holds what a run links against: the certified harness writers with hand-written
reading operations (not a certified source, `harness_read_case/` says why), the toy operations of
`drv_toy_model.*`, and the certified `shallow_water2d@0.4.7` model cut to one `__step`. Each row
drives the real `render_runner`, `write_case_inputs`, the compiler and `evaluate_primary_predicates`
or the captures, and observes what the program wrote. The Fortran rows run wherever the suite does;
the C++ rows need `nvcc` and no device (the toy model is host code).
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
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


def with_dummy(ir: dict[str, Any], dummy: str) -> dict[str, Any]:
    """`ir` with its procedure dummy `rhs` renamed `dummy`, in the signature and the bindings."""
    ir = copy.deepcopy(ir)
    for sig in ir["public_api"]["signatures"]:
        sig["signature"]["args"] = [{**arg, "name": dummy} if arg["name"] == "rhs" else arg
                                    for arg in sig["signature"]["args"]]
    steps = ir["drive"]["step"]
    for entry in steps if isinstance(steps, list) else [steps]:
        entry["arguments"][dummy] = entry["arguments"].pop("rhs")
    return ir


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


def list_record_ir(record_first: bool) -> dict[str, Any]:
    """The list form over a procedure record (issue #613): case `c`'s entry is `procedure_ir`'s
    call, recording `n_calls`, `stage_t`, `stage_u1` and `stage_u2`; case `p`'s entry binds no
    procedure, so in case `p` all four are read from the case file — an array record, the rank-1
    record of a rank-0 dummy and the scalar `calls` state. ``record_first`` lists the recording
    entry first."""
    record = {"operation": "drv_toy__advance", "result": "ok", "cases": ["c"], "arguments": {
        "nx": "inputs.grid.nx", "t": "inputs.t", "dt": "inputs.dt", "u_n": "state.u_n",
        "rhs": {"procedure": "inputs.lam * u", "calls": "state.n_calls",
                "record": {"t": "state.stage_t", "u": ["state.stage_u1", "state.stage_u2"]}},
        "u_np1": "state.u_np1", "ok": "state.ok"}}
    plain = {"operation": "drv_toy__apply", "result": "ok", "cases": ["p"], "arguments": {
        "nx": "inputs.grid.nx", "mode": "inputs.mode", "a": "state.stage_u1", "b": "state.u_np1",
        "c": "state.u_n", "s": "state.s", "ok": "state.ok"}}
    cases = [
        {"case_id": "p", "inputs": {"grid": {"nx": 3}, "mode": 0},
         "initial_state": {"stage_u1": [1.0, 2.0, 4.0], "stage_u2": [7.0, 8.0, 9.0],
                           "stage_t": [0.5, 0.25], "n_calls": 9.0, "u_n": [5.0, 5.0, 5.0],
                           "u_np1": -9.0, "s": -7.0}},
        {"case_id": "c", "inputs": {"grid": {"nx": 3}, "t": 0.25, "dt": 0.5, "lam": -2.0},
         "initial_state": {"u_n": [1.0, 2.0, 4.0], "u_np1": -9.0, "s": -7.0}}]
    return _ir({"stage_t": "[2]", "u_n": "[nx]", "u_np1": "[nx]", "ok": "scalar",
                "n_calls": "scalar", "stage_u1": "[nx]", "stage_u2": "[nx]", "s": "scalar"},
               {"step": [record, plain] if record_first else [plain, record]}, cases)


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
        obj, src = f"{stem}.o", DATA / f"{stem}_model.f90"
        # each compile reads the `.mod` files the ones before it left
        _compile_cached(["gfortran", *flags, "-c", str(src), "-o", obj], tmp,
                        [src, *sorted(tmp.glob("*.mod"))])
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


_OBJECT_CACHE: list[Path] = []


def _compile_cached(argv: list[str], cwd: Path, inputs: list[Path]) -> None:
    """Run the compile ``argv`` in ``cwd``, or copy in what an identical compile left before.

    The key is the command line and the bytes of every input it reads, so a row that edits a
    source (a seeded defect, a rendered header) misses and compiles. What the compile leaves is
    every file new in ``cwd`` afterwards (the object, and a Fortran compile's `.mod` files).
    For the sources every row of a class repeats: the harness is 3.2 s of a 7 s `nvcc` build and
    1.35 s of an MPI one (measured 2026-11-11). Per process, so an xdist worker builds each
    entry once and no two workers share a directory."""
    import atexit
    import hashlib
    if not _OBJECT_CACHE:
        _OBJECT_CACHE.append(Path(tempfile.mkdtemp(prefix="spec2science-objcache-")))
        atexit.register(shutil.rmtree, _OBJECT_CACHE[0], True)
    digest = hashlib.sha256("\0".join(argv).encode())
    for path in inputs:
        digest.update(b"\0" + path.name.encode() + b"\0" + path.read_bytes())
    entry = _OBJECT_CACHE[0] / digest.hexdigest()
    if not entry.is_dir():
        before = set(os.listdir(cwd))
        build = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
        if build.returncode != 0:
            raise AssertionError(" ".join(argv) + build.stdout[:2000] + build.stderr[:3000])
        staging = Path(tempfile.mkdtemp(dir=_OBJECT_CACHE[0]))
        for name in set(os.listdir(cwd)) - before:
            shutil.copy2(cwd / name, staging / name)
        staging.rename(entry)
        return
    for item in entry.iterdir():
        shutil.copy2(item, cwd / item.name)


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
    headers = sorted(tmp.glob("*.cuh"))
    for stem in ("harness_cpp_gpu_model", f"{sid}_model"):
        _compile_cached(["nvcc", "-std=c++17", "-O1", "-c", f"{stem}.cu", "-o", f"{stem}.o"],
                        tmp, [tmp / f"{stem}.cu", *headers])
        objs.append(f"{stem}.o")
    build = subprocess.run(["nvcc", "-std=c++17", "-O1", "-c", "runner.cu", "-o", "runner.o"],
                           cwd=tmp, capture_output=True, text=True)
    if build.returncode != 0:
        raise AssertionError("runner" + build.stdout[:2000] + build.stderr[:2000])
    objs.append("runner.o")
    subprocess.run(["nvcc", *objs, "-o", "bin"], cwd=tmp, check=True, capture_output=True)
    return _exec(tmp, ids, {"SPEC2SCIENCE_CUDA_BLOCK": "64"})


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

    def test_a_procedure_dummy_of_every_legal_length_builds_and_runs(self) -> None:
        """A procedure dummy of 55 to 63 characters made the Fortran driver's procedure name,
        `driver_<dummy>_1`, longer than the language's 63, and the runner did not compile. 54 is
        the longest whose name fits, 55 the shortest that does not, 63 the longest legal dummy."""
        for width in (54, 55, 63):
            with self.subTest(width=width), tempfile.TemporaryDirectory() as tmp:
                run = type(self).build_and_run(
                    Path(tmp), with_dummy(procedure_ir("inputs.lam * u"), "r" * width))
                self.assertEqual(run.proc.returncode, 0, run.proc.stderr[-2000:])
                final = run.snapshot("c")
                self.assertEqual(final["n_calls"], 2.0)
                self.assertEqual(final["stage_t"], [0.25, 0.75])

    def test_a_refusing_operation_leaves_the_records_at_zero(self) -> None:
        final = self._run(procedure_ir("u", dt=-1.0)).snapshot("c")
        self.assertEqual(final["n_calls"], 0.0)
        self.assertEqual(final["stage_t"], [0.0, 0.0])
        self.assertEqual(final["ok"], 0.0)

    def test_a_state_another_entry_records_is_read_from_the_file_in_either_entry_order(
            self) -> None:
        """Issue #613: a state entry `c` records is read from the case file in entry `p`, which
        records nothing — whichever entry is listed first and whichever case runs first. Before
        the fix a recording entry listed first zeroed the four records in `p` as well."""
        for record_first in (False, True):
            ir = list_record_ir(record_first)
            self.assertEqual(pe.drive_violations(
                {**ir, "drive": {**ir["drive"], "distribute": {"axes": []}}},
                kind="component"), [])
            for reversed_cases in (False, True):
                with self.subTest(record_first=record_first, reversed_cases=reversed_cases):
                    tmp = Path(self._tmp.name) / f"r{int(record_first)}c{int(reversed_cases)}"
                    tmp.mkdir()
                    built = copy.deepcopy(ir)
                    if reversed_cases:
                        built["case"]["test_case_set"].reverse()
                    run = type(self).build_and_run(tmp, built)
                    self.assertEqual(run.proc.returncode, 0, run.proc.stderr[-2000:])
                    initial_p, final_p = run.snapshot("initial/p"), run.snapshot("p")
                    self.assertEqual(initial_p["stage_u1"], [1.0, 2.0, 4.0])
                    self.assertEqual(initial_p["stage_u2"], [7.0, 8.0, 9.0])
                    self.assertEqual(initial_p["stage_t"], [0.5, 0.25])
                    self.assertEqual(initial_p["n_calls"], 9.0)
                    self.assertEqual(final_p["u_np1"], [2.0, 4.0, 8.0])
                    self.assertEqual(final_p["n_calls"], 9.0)
                    initial_c, final_c = run.snapshot("initial/c"), run.snapshot("c")
                    self.assertEqual(initial_c["n_calls"], 0.0)
                    self.assertEqual(initial_c["stage_t"], [0.0, 0.0])
                    self.assertEqual(initial_c["stage_u1"], [0.0, 0.0, 0.0])
                    self.assertEqual(final_c["n_calls"], 2.0)
                    self.assertEqual(final_c["stage_u1"], [1.0, 2.0, 4.0])
                    self.assertEqual(final_c["stage_t"], [0.25, 0.75])

    def test_two_entries_that_each_bind_a_procedure_render_two_procedures(self) -> None:
        """Issue #613: a host-rendered procedure is numbered over the node, so two entries that
        each bind `rhs` render `driver_rhs_1` and `driver_rhs_2`, where both took `driver_rhs_1`
        and the driver did not compile. Each case's entry zeroes and fills the records itself."""
        ir = procedure_ir("inputs.lam * u")
        entry = ir["drive"]["step"]
        ir["drive"]["step"] = [{**copy.deepcopy(entry), "cases": ["c"]},
                               {**copy.deepcopy(entry), "cases": ["p"]}]
        ir["case"]["test_case_set"].append(
            {"case_id": "p", "inputs": {"grid": {"nx": 3}, "t": 1.0, "dt": 0.25, "lam": 3.0},
             "initial_state": {"u_n": [2.0, 2.0, 2.0], "u_np1": -9.0}})
        self.assertEqual(pe.drive_violations(
            {**ir, "drive": {**ir["drive"], "distribute": {"axes": []}}}, kind="component"), [])
        run = self._run(ir)
        final_p, final_c = run.snapshot("p"), run.snapshot("c")
        self.assertEqual(final_p["n_calls"], 2.0)
        self.assertEqual(final_p["stage_t"], [1.0, 1.25])
        self.assertEqual(final_p["stage_u1"], [2.0, 2.0, 2.0])
        self.assertEqual(final_p["stage_u2"], [3.5, 3.5, 3.5])
        self.assertEqual(final_c["n_calls"], 2.0)
        self.assertEqual(final_c["stage_t"], [0.25, 0.75])
        self.assertEqual(final_c["stage_u1"], [1.0, 2.0, 4.0])
        self.assertEqual(final_c["stage_u2"], [0.0, 0.0, 0.0])
        for cid in ("p", "c"):
            initial = run.snapshot(f"initial/{cid}")
            self.assertEqual(initial["n_calls"], 0.0, cid)
            self.assertEqual(initial["stage_t"], [0.0, 0.0], cid)
            self.assertEqual(initial["stage_u1"], [0.0] * 3, cid)
            self.assertEqual(initial["stage_u2"], [0.0] * 3, cid)


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


class FortranProcedureNameTest(unittest.TestCase):
    def test_a_procedure_name_past_the_identifier_limit_drops_the_dummy(self) -> None:
        """The plan's `driver_<dummy>_<n>` where it fits, `driver_<n>` where it does not, and no
        rendered identifier past the limit either way: the second of two procedures checks that
        the number is the plan's, not a constant."""
        from tools.backends.language.fortran import bundle as f_bundle

        limit = f_bundle.IDENTIFIER_MAX
        base = procedure_ir("inputs.lam * u")
        entry = base["drive"]["step"]
        base["drive"]["step"] = [{**copy.deepcopy(entry), "cases": ["c"]},
                                 {**copy.deepcopy(entry), "cases": ["p"]}]
        base["case"]["test_case_set"].append(copy.deepcopy(base["case"]["test_case_set"][0]))
        base["case"]["test_case_set"][1]["case_id"] = "p"
        for width, names in ((limit - 9, ["driver_" + "r" * (limit - 9) + "_1",
                                          "driver_" + "r" * (limit - 9) + "_2"]),
                             (limit - 8, ["driver_1", "driver_2"]),
                             (limit, ["driver_1", "driver_2"])):
            with self.subTest(width=width):
                ir = with_dummy(base, "r" * width)
                self.assertEqual(f_runner.ir_content_violations(
                    ir, "drv_toy", "harness_fortran_cpu"), [])
                text = f_runner.render_runner(ir, "drv_toy", "harness_fortran_cpu", target=CPU)
                code = re.sub(r"'[^']*'|\"[^\"]*\"|!.*", "", text)
                self.assertLessEqual(
                    max(len(w) for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", code)), limit)
                for name in names:
                    self.assertIn(f"  subroutine {name}(", text)


class FortranTokenWidthTest(unittest.TestCase):
    def test_an_argument_past_the_token_width_stops_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = run_fortran(Path(tmp), call_ir(0))
            self.assertEqual(run.proc.returncode, 0, run.proc.stderr[-500:])
            long = "x" * (f_runner.TOKEN_LEN + 1)
            second = _exec(Path(tmp), [long])
            self.assertNotEqual(second.proc.returncode, 0)
            self.assertIn("exceeds the token width", second.proc.stderr)


_WRAP_PROBE = """
import json, sys
from tools.tests.test_driver_render import CPU, procedure_ir
from tools.backends.language.fortran import runner as f_runner
from tools.host_render import RenderError
out = {"wrap": f_runner._wrap("y " * 40 + "z" * 95), "render": {}}
for k in (17, 18, 30):
    ir = procedure_ir("abs(" * k + "u" + ")" * k)
    try:
        text = f_runner.render_runner(ir, "drv_toy", "harness_fortran_cpu", target=CPU)
        out["render"][k] = max(len(line) for line in text.splitlines())
    except RenderError as exc:
        out["render"][k] = str(exc)
json.dump(out, sys.stdout)
"""


class FortranWrapTest(unittest.TestCase):
    """A continuation whose remainder holds no blank before the width (TODO item, found by issue
    #613's review round 3): `_wrap` found its own indent's blank, cut nothing and never returned,
    so the render — and `Compile.static`'s dry render in the conductor's process — hung, its
    memory growing. Run in a child under a timeout and an address-space cap, so a regression
    fails the row rather than hanging the suite or exhausting the runner's memory."""

    def test_a_blank_free_remainder_longer_than_the_width_returns(self) -> None:
        import resource

        def cap() -> None:
            # 4 GiB of address space. The probe's VmPeak was 0.1 GB at one BLAS thread and 1.7 GB
            # at 48 (numpy reserves per thread); the old loop grew about 180 MB/s.
            resource.setrlimit(resource.RLIMIT_AS, (4 << 30, 4 << 30))
        proc = subprocess.run([sys.executable, "-c", _WRAP_PROBE], cwd=ROOT, text=True,
                              capture_output=True, timeout=60, preexec_fn=cap)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        out = json.loads(proc.stdout)
        self.assertEqual(out["wrap"], ["y " * 39 + "y &", "  " + "z" * 95])
        render = out["render"]
        self.assertLess(render["17"], f_runner.max_rendered_line())
        self.assertLess(render["18"], f_runner.max_rendered_line())
        self.assertIsInstance(render["30"], str)
        self.assertIn("column limit", render["30"])


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


# --- A `problem` over a `problem` (the system tier, Part 1, PR-S1) ---------------------------
#
# The composite `shallow_water2d_rotating` (`data/driver_e2e/shallow_water2d_rotating_model.f90`,
# hand-written) calls the member `shallow_water2d__step` and then, every `rotation_every` steps,
# one RK4 step of the Coriolis source. Its drive IR is the member's 0.4.7 IR with the composite's
# arguments added: `step`, the case inputs `rotation` / `rotation_every`, and `f` a snapshot
# schema variable bound as `state.f`, as `z_b` is.

SW2D_COMPONENTS = ("dynamics_shallow_water_boundary_2d_periodic_copy",
                   "dynamics_shallow_water_flux_2d_rusanov_p0",
                   "dynamics_shallow_water_time_update_2d_ssprk2")
CORIOLIS = "dynamics_shallow_water_source_2d_coriolis"
RK4 = "dynamics_shallow_water_time_update_2d_rk4"
ROTATING = "shallow_water2d_rotating"
ROTATING_NODE_KEY = f"problem/{ROTATING}@0.1.0"
#: The composite's direct dependencies, as its IR's `dependency.direct_deps` names them.
ROTATING_DIRECT_DEPS = (("problem", "shallow_water2d", "0.5.1", "shallow_water2d__step"),
                        ("component", CORIOLIS, "0.2.0", f"{CORIOLIS}__apply"),
                        ("component", RK4, "0.1.1", f"{RK4}__advance"))
#: What `run_fortran` links before the composite (the harness is implicit): the member's
#: components, the member, then the composite's own components.
ROTATING_LINK = (*SW2D_COMPONENTS, "shallow_water2d", CORIOLIS, RK4)


def shallow_water2d_ir() -> dict[str, Any]:
    return yaml.safe_load((DRIVE_IR / "shallow_water2d_0.4.7" / "spec.ir.yaml").read_text())


def shallow_water2d_rotating_ir(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """The composite's drive IR over `cases`, derived from the member's 0.4.7 IR."""
    ir = shallow_water2d_ir()
    op = f"{ROTATING}__step"
    ir["meta"].update({"node_key": ROTATING_NODE_KEY, "spec_id": ROTATING,
                       "spec_version": "0.1.0"})
    spec_dir = f"spec/problem/dynamics/shallow_water/{ROTATING}"
    ir["meta"]["source_refs"] = {"controlled_spec": f"{spec_dir}/controlled_spec.md",
                                 "tests": f"{spec_dir}/tests.md",
                                 "deps": f"{spec_dir}/deps.yaml"}
    ir["dependency"] = {
        "node_key": ROTATING_NODE_KEY,
        "direct_deps": [{"kind": kind, "node_key": f"{kind}/{sid}@{ver}", "operations": [o]}
                        for kind, sid, ver, o in ROTATING_DIRECT_DEPS]}
    ir["case"]["test_case_set"] = cases
    step = ir["drive"]["step"]
    step["operation"] = op
    step["arguments"].update({"step": "step", "rotation": "inputs.rotation.on",
                              "rotation_every": "inputs.rotation.every", "f": "state.f"})
    entry = ir["public_api"]["signatures"][0]
    args: list[dict[str, Any]] = []
    for arg in entry["signature"]["args"]:
        if arg["name"] == "z_b":
            args += [{"intent": "in", "name": n, "rank": 0, "spec": {"type": "integer"}}
                     for n in ("step", "rotation", "rotation_every")]
            args.append({"dims": ["nx", "ny"], "intent": "in", "name": "f", "rank": 2,
                         "spec": dict(REAL)})
        args.append(arg)
    entry["signature"]["args"] = args
    entry["signature"]["name"] = entry["symbol"] = op
    ir["public_api"]["published_operations"][0]["operation_id"] = op
    variables = ir["io_contract"]["raw_requirements"]["required_evidence"][0]["schema"]["variables"]
    variables.insert([v["name"] for v in variables].index("z_b") + 1,
                     {"name": "f", "shape_expr": "[nx, ny]"})
    return ir


def with_rotation(case: dict[str, Any], *, on: int, every: int, f0: float) -> dict[str, Any]:
    """`case` with the composite's inputs: the selector, the interval and a uniform `f = f0`."""
    out = copy.deepcopy(case)
    out["inputs"]["rotation"] = {"on": on, "every": every, "f0": f0}
    out["initial_state"]["f"] = "inputs.rotation.f0"
    return out


def rotating_section_5_1(ir: dict[str, Any]) -> str:
    """A §5.1 canonical interface block equal to `ir`'s `public_api` (for a fixture
    `controlled_spec.md`), so the signature pin reads the surface the IR declares."""
    block = {"module_parameters": ir["public_api"]["module_parameters"], "types": [],
             "procedures": [e["signature"] for e in ir["public_api"]["signatures"]]}
    body = yaml.safe_dump(block, sort_keys=False)
    return f"### 5.1 Canonical interface block\n\n```yaml\n{body}```\n"


def rotating_closure_graph(repo: Path) -> dict[str, Any]:
    """The two-level dependency graph of the composite, built by `build_dependency_graph` over a
    fixture repository written into `repo`: composite -> member problem -> its three
    components, and composite -> Coriolis, RK4."""
    from tools.dependency_graph import build_dependency_graph
    from tools.tests.orchestration_fixtures import ensure_spec_entry

    def deps(components: tuple[str, ...], problems: tuple[str, ...] = ()) -> str:
        text = "dependencies:\n  components:\n" + "".join(
            f"    - component_id: {c}\n      version_constraint: \">=0.1.0\"\n"
            for c in components)
        text += "  profiles: []\n  infrastructure: []\n"
        if problems:
            text += "  problems:\n" + "".join(
                f"    - problem_id: {p}\n      version_constraint: \">=0.1.0\"\n"
                for p in problems)
        return text

    for sid, version in zip(SW2D_COMPONENTS, ("0.2.1", "0.2.1", "0.4.1")):
        ensure_spec_entry(repo, f"component/{sid}@{version}")
    ensure_spec_entry(repo, f"component/{CORIOLIS}@0.2.0")
    ensure_spec_entry(repo, f"component/{RK4}@0.1.1")
    ensure_spec_entry(repo, "problem/shallow_water2d@0.5.1", deps_yaml=deps(SW2D_COMPONENTS))
    ensure_spec_entry(repo, ROTATING_NODE_KEY,
                      deps_yaml=deps((CORIOLIS, RK4), ("shallow_water2d",)))
    graph, error = build_dependency_graph(
        repo, target_spec_ref=f"spec/problem/{ROTATING}", target_node_key=ROTATING_NODE_KEY)
    if error is not None:
        raise AssertionError(error)
    return graph


def rotating_closure(graph: dict[str, Any]) -> list[str]:
    """The composite's closure spec_ids in compile order, as `_closure_nodes_from_graph` decides
    it. `all_nodes` is handed over REVERSED: the builder already lists it deepest first (and
    `component/` sorts before `problem/`), so only a reversed input observes the order the
    function imposes."""
    from tools.orchestration_runtime import _closure_nodes_from_graph
    shuffled = {**graph, "all_nodes": list(reversed(graph["all_nodes"]))}
    return [nk.split("/", 1)[1].split("@", 1)[0]
            for nk in _closure_nodes_from_graph(shuffled, ROTATING_NODE_KEY)]


def render_rotating_makefile(closure: list[str]) -> str:
    """The make control file the conductor renders for the composite (`render_node`), over
    `closure` (spec_ids, deepest first), for the fortran_cpu toolchain."""
    from tools.backends import registry
    from tools.backends.build_system.make import control_file
    rules = registry.capability_module("language", "fortran", "control_file").rules(
        standard="f2008", parallel_backend="openmp")
    return control_file.render_node(
        rules=rules, compiler="gfortran", bin_name=f"{ROTATING}_runner", cases_default="c",
        model_stem=f"{ROTATING}_model", runner_stem=f"{ROTATING}_runner", closure=closure,
        initial_capture_dir=True)


class ProblemOverProblemEndToEndTest(unittest.TestCase):
    """The composing fixture built and run by the host-rendered driver over the member fixture,
    the certified Coriolis source and the certified RK4 source (`data/driver_e2e/`)."""

    def test_a_problem_over_a_problem_dependency_builds_and_runs(self) -> None:
        member_ir = shallow_water2d_ir()
        base = next(c for c in member_ir["case"]["test_case_set"]
                    if c["case_id"] == "swe2d_ref_n032_sx000_sy000_dts100")
        cid = base["case_id"]

        # (a) Rotation off: the composite's final state is the member's own run of the same
        # case, bitwise. The member is built here from its 0.4.7 IR cut to the case.
        member_ir["case"]["test_case_set"] = [base]
        off = shallow_water2d_rotating_ir([with_rotation(base, on=0, every=1, f0=5.0)])
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            member = run_fortran(Path(a), member_ir, sid="shallow_water2d", deps=SW2D_COMPONENTS)
            composite = run_fortran(Path(b), off, sid=ROTATING, deps=ROTATING_LINK)
            for run in (member, composite):
                self.assertEqual(run.proc.returncode, 0, run.proc.stderr[-2000:])
            m, c = member.snapshot(cid), composite.snapshot(cid)
        self.assertEqual((c["ok"], c["step"]), (1.0, m["step"]))
        self.assertGreater(m["step"], 0)
        for name in ("h", "hu", "hv"):
            diff = float(np.max(np.abs(np.asarray(c[name]) - np.asarray(m[name]))))
            self.assertEqual(diff, 0.0, name)

        # (b) Rotation on over a flat state at rest in `h`, uniform velocity, uniform `f`: the
        # member is the identity there, so each rotation call is one RK4 step of
        # dz/dt = -i f z on z = hu + i hv (the Coriolis spec's source [0, f hv, -f hu]), and the
        # final z is z0 * R(-i f k dt)^n, R the RK4 polynomial, n the steps s in 0..n_step-1 with
        # mod(s + 1, k) == 0 (the driver's `step` is the committed count s during call s + 1).
        flat = next(c for c in shallow_water2d_ir()["case"]["test_case_set"]
                    if c["case_id"] == "swe2d_flat_ref_n032_sx000_sy000_dts100")
        f0, hu0, hv0, n_step = 5.0, 0.3, 0.1, 40
        ks = (1, 3, 4)
        cases = []
        for k in ks:
            case = with_rotation(flat, on=1, every=k, f0=f0)
            case["case_id"] = f"rot_flat_k{k}"
            case["initial_state"].update({"h": "inputs.constants.H_0", "hu": str(hu0),
                                          "hv": str(hv0)})
            case["derived"]["n_step"] = str(n_step)
            case["inputs"]["history"]["every"] = 0
            cases.append(case)
        closed: dict[int, complex] = {}
        with tempfile.TemporaryDirectory() as t:
            run = run_fortran(Path(t), shallow_water2d_rotating_ir(cases), sid=ROTATING,
                              deps=ROTATING_LINK)
            self.assertEqual(run.proc.returncode, 0, run.proc.stderr[-2000:])
            for k in ks:
                cid = f"rot_flat_k{k}"
                inputs = json.loads((Path(t) / "ir" / "case_inputs" / f"{cid}.json").read_text())
                np.testing.assert_array_equal(np.asarray(inputs["f"]), f0)
                np.testing.assert_array_equal(np.asarray(inputs["z_b"]), 0.0)
                dt = inputs["derived.dt"]
                w = -1j * f0 * k * dt
                calls = sum(1 for s in range(n_step) if (s + 1) % k == 0)
                closed[k] = complex(hu0, hv0) * (1 + w + w**2 / 2 + w**3 / 6 + w**4 / 24) ** calls
                final = run.snapshot(cid)
                self.assertEqual((final["ok"], final["step"]), (1.0, n_step))
                err = max(float(np.max(np.abs(np.asarray(final["hu"]) - closed[k].real))),
                          float(np.max(np.abs(np.asarray(final["hv"]) - closed[k].imag))))
                h0 = np.asarray(inputs["h"])
                drift = float(np.max(np.abs(np.asarray(final["h"]) - h0)))
                with self.subTest(k=k):
                    # Rounding over 40 linear RK4 steps is a few 1e-16 on |z| ~ 0.3; 1e-12
                    # leaves three orders above it and is far below what separates the
                    # intervals (asserted below).
                    self.assertLess(err, 1e-12)
                    self.assertEqual(drift, 0.0)
        # The tolerance discriminates: each interval's closed form is 1e3 tolerances from the
        # others' and from no rotation at all, and k = 3 (40 = 13 * 3 + 1) is the interval
        # whose call count depends on the firing phase `mod(step + 1, k) == 0`.
        for k1, k2 in ((1, 3), (1, 4), (3, 4)):
            self.assertGreater(abs(closed[k1] - closed[k2]), 1e3 * 1e-12)
        for k in ks:
            self.assertGreater(abs(closed[k] - complex(hu0, hv0)), 1e-2)

    def test_the_control_file_orders_a_two_level_closure_deepest_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            closure = rotating_closure(rotating_closure_graph(Path(tmp)))
        self.assertEqual(sorted(closure), sorted(ROTATING_LINK))
        member = closure.index("shallow_water2d")
        self.assertTrue(all(closure.index(c) < member for c in SW2D_COMPONENTS), closure)
        makefile = render_rotating_makefile(closure)
        dep_objs = re.search(r"^DEP_OBJS = (.*)$", makefile, re.MULTILINE).group(1).split()
        self.assertEqual(dep_objs, [f"$(OBJDIR)/{d}_model.o" for d in closure])
        member_rule = re.search(r"^\$\(OBJDIR\)/shallow_water2d_model\.o: (.*)$", makefile,
                                re.MULTILINE).group(1).split()
        for component in SW2D_COMPONENTS:
            self.assertIn(f"$(OBJDIR)/{component}_model.o", member_rule)
        self.assertNotIn(f"$(OBJDIR)/{ROTATING}_model.o", member_rule)
        model_rule = re.search(r"^\$\(MODEL_OBJ\): (.*)$", makefile, re.MULTILINE).group(1)
        self.assertIn("$(DEP_OBJS)", model_rule)

        if shutil.which("make") is None or shutil.which("gfortran") is None:
            self.skipTest("make or gfortran is not on PATH: the rendered file is not run")
        # The rendered file, run over the staged fixtures as the conductor stages them (the
        # harness first, as `pipeline_closure_nodes` puts it): it builds the composite.
        flat = next(c for c in shallow_water2d_ir()["case"]["test_case_set"]
                    if c["case_id"] == "swe2d_flat_ref_n032_sx000_sy000_dts100")
        ir = shallow_water2d_rotating_ir([with_rotation(flat, on=1, every=2, f0=5.0)])
        with tempfile.TemporaryDirectory() as t:
            out = Path(t)
            ids = _prepare(out, ir, None)
            staged = ["harness_fortran_cpu", *closure, ROTATING]
            for stem in staged:
                shutil.copy(DATA / f"{stem}_model.f90", out)
            (out / f"{ROTATING}_runner.f90").write_text(
                f_runner.render_runner(ir, ROTATING, "harness_fortran_cpu", target=CPU))
            (out / "Makefile").write_text(render_rotating_makefile(staged[:-1]))
            build = subprocess.run(["make", "all", "BIN=bin"], cwd=out, capture_output=True,
                                   text=True, timeout=600)
            self.assertEqual(build.returncode, 0, build.stdout[-2000:] + build.stderr[-2000:])
            order = [line.split()[-1] for line in build.stdout.splitlines() if " -o " in line]
            self.assertLess(order.index("./shallow_water2d_model.o"),
                            order.index(f"./{ROTATING}_model.o"))
            run = _exec(out, ids)
            self.assertEqual(run.proc.returncode, 0, run.proc.stderr[-2000:])
            self.assertEqual(run.snapshot(ids[0])["ok"], 1.0)


class ComposingModelGateTest(unittest.TestCase):
    """The composing fixture passes the `problem` model gates with the member among its
    dependencies, and each gate refuses a mutation of it in its own reach."""

    DEPS = ["shallow_water2d", CORIOLIS, RK4]
    CALL = "    call shallow_water2d__step(nx, ny, dx, dy, g, dt, t, z_b, h, hu, hv, member_ok)\n"

    def _text(self) -> str:
        return (DATA / f"{ROTATING}_model.f90").read_text()

    def _gates(self, text: str) -> list[str]:
        from tools.backends.language.fortran import source
        found: list[str] = []
        source.model_source_gates(
            node_key=ROTATING_NODE_KEY, model_file=Path("m.f90"), text=text,
            dep_spec_ids=self.DEPS, violations=found, multidim_spec_id=ROTATING)
        return found

    def _calls(self, text: str) -> list[str]:
        from tools.backends.language.fortran import source
        found: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f"{ROTATING}_model.f90"
            path.write_text(text)
            source.validate_dependency_operations([path], self.DEPS, found)
        return found

    def test_the_composing_fixture_passes_the_model_source_gates(self) -> None:
        self.assertEqual(self._gates(self._text()), [])
        self.assertEqual(self._calls(self._text()), [])

    def test_the_composing_fixture_without_its_member_call_is_refused(self) -> None:
        """The dependency-call gate refuses the fixture with the member's call removed. The
        dataflow gate does not: it reads the calls a model MAKES, and the RK4 call's result
        still reaches the state."""
        cut = self._text().replace(self.CALL, "    member_ok = .true.\n")
        self.assertNotEqual(cut, self._text())
        self.assertTrue(any("missing dependency operation call (shallow_water2d__*)" in v
                            for v in self._calls(cut)), self._calls(cut))
        self.assertEqual(self._gates(cut), [])

    def test_a_tendency_that_discards_the_coriolis_result_is_refused(self) -> None:
        """The dataflow gate reads the composite's tendency: one that drops the Coriolis
        operation's output is refused, so the passing row is not vacuous."""
        cut = self._text().replace("    dUdt = s\n", "    dUdt = 1.0_dp\n")
        self.assertNotEqual(cut, self._text())
        self.assertTrue(any("coriolis_tendency does not propagate" in v
                            for v in self._gates(cut)), self._gates(cut))


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
    def test_a_distributed_harness_takes_the_distributed_form(self) -> None:
        """Issue #610: what was refused here as `driver_distributed_pending` until PR-B2 renders
        the distributed form; an axis-less node keeps the serial body under the distributed
        harness's names, with rank 0 writing and the result reduced, and no partition."""
        text = f_runner.render_runner(loop_ir(0), "drv_toy", "harness_fortran_cpu_mpi",
                                      target=CPU)
        self.assertIn("  call harness_fortran_cpu_mpi__init()", text)
        self.assertIn("lg_ok = nok == nranks", text)
        self.assertIn("    if (rank == 0) call harness_fortran_cpu_mpi__write_snapshot(cid, vals, t_c)",
                      text)
        self.assertNotIn("__partition", text)
        self.assertNotIn("nranks", f_runner.render_runner(loop_ir(0), "drv_toy",
                                                          "harness_fortran_cpu", target=CPU))

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


# --- the distributed driver, compiled and run under the launcher (issue #610, PR-B2) -------------
#
# `data/driver_e2e/mpi/` holds the certified `harness_fortran_cpu_mpi@0.2.0` model with three
# blocks of the certified `harness_fortran_cpu@0.10.0` model inserted under its names (the
# variables of the reading state, `__parse_cases` in the form that keeps the spec path, and
# `__read_case` with its getters): `0.4.0`'s surface by hand, held to the real `0.4.0` §5.1 by
# `assert_harness_pin` below. Five hand-written models, each with the IR its builder returns and the
# `tests.md` line gate (o) holds that IR to.

MPI_DATA = DATA / "mpi"
MPI_HARNESS = "harness_fortran_cpu_mpi"
#: The grids of `fortran_cpu_mpi` (the certified 4-rank grid and its sweep, 2 to 4 ranks), uneven partitions
#: kept, and the singleton the quality check runs.
MPI_GRIDS = ((2, 2), (4, 1), (1, 4), (3, 1), (1, 3), (2, 1), (1, 2))
SINGLETON = (1, 1)


def _mpi_target() -> dict[str, Any]:
    return yaml.safe_load((ROOT / "spec/targets/fortran_cpu_mpi.yaml").read_text())


def _mpi_flag_sets() -> dict[str, list[str]]:
    """The flag sets the host builds a binary of this target with: the release build's
    (`control_file.flags`, without its module-directory options) and the `checked` row's (the
    compiler's `CHECKED_FLAGS` appended)."""
    from tools.backends.compiler.gfortran import checked_flags
    from tools.backends.language.fortran import control_file
    release = control_file.flags("f2008", "mpi").split(" -J")[0].split()
    return {"release": release, "checked": [*release, *checked_flags.CHECKED_FLAGS]}


def _mpi_ir(sid: str, kind: str, ops: dict[str, list[dict[str, Any]]], drive: dict[str, Any],
            cases: list[dict[str, Any]], variables: dict[str, str], tests: list[dict[str, Any]],
            predicates: list[dict[str, Any]], interfaces: tuple[dict[str, Any], ...] = ()
            ) -> dict[str, Any]:
    ir: dict[str, Any] = {
        "schema_version": 1,
        "meta": {"spec_id": sid, "spec_kind": kind, "spec_version": "0.1.0",
                 "node_key": f"{kind}/{sid}@0.1.0"},
        "public_api": {
            "published_operations": [{"operation_id": op} for op in ops],
            "module_parameters": [{"name": "dp", "value": "float64"}],
            "interfaces": [{"name": p["name"], "signature": p} for p in interfaces],
            "signatures": [{"symbol": op, "signature": {"kind": "subroutine", "name": op,
                                                        "args": args}}
                           for op, args in ops.items()]},
        "drive": drive,
        "case": {"test_case_set": cases},
        "dependency": {"node_key": f"{kind}/{sid}@0.1.0", "direct_deps": []},
        "io_contract": {
            "raw_requirements": {"required_evidence": [
                {"artifact": "state_snapshots", "required": True,
                 "schema": {"time_variable": "t", "variables": [
                     {"name": n, "shape_expr": s} for n, s in variables.items()]}}]},
            "tests": tests, "predicates": predicates}}
    if "loop" in drive:
        names = ["step", *(f"{v}_{m}" for v, s in variables.items() if s != "scalar"
                           for m in ("min", "max"))]
        ir["io_contract"]["raw_requirements"]["required_evidence"] += [
            {"artifact": "state_history"}, {"artifact": "step_series", "names": names}]
    return ir


def _one_test_per_case(cases: list[dict[str, Any]], checks: list[tuple[str, str, str, Any]],
                       xfail: dict[str, list[tuple[str, str, str, Any]]] | None = None
                       ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """One test per case, carrying the predicates ``checks`` (quantity, expr, op, value); a case
    of ``xfail`` gets an `xfail` test carrying that entry's predicates instead (each holds on the
    refusal it states)."""
    tests, predicates = [], []
    for case in cases:
        tid, ids = f"t_{case['case_id']}", [case["case_id"]]
        own = (xfail or {}).get(ids[0], checks)
        tests.append({"test_id": tid,
                      "expected_outcome": "xfail" if ids[0] in (xfail or {}) else "pass",
                      "target_cases": ids, "quantities": sorted({q for q, *_ in own})})
        predicates += [{"test_id": tid, "quantity": q, "target_cases": ids, "per_case": True,
                        "expr": expr, "op": op, "value": value} for q, expr, op, value in own]
    return tests, predicates


def recon_ir() -> dict[str, Any]:
    """Model (1), muscl's shape: a ghosted `in` (`u_in`, its domain ghosts a sentinel) and two
    face `out`s. `c6x4` under `(1, 4)` is the one-cell-block case (`min_block` 1, B6); `cbad`'s
    `nx_total` is inconsistent, so it is replicated with its raw inputs and refused (`xfail`)."""
    sid = "fx_recon"
    ops = {f"{sid}__apply": [
        _arg("nx", "in", INT), _arg("ny", "in", INT), _arg("ng", "in", INT),
        _arg("nx_total", "in", INT), _arg("ny_total", "in", INT),
        _arg("u_in", "in", REAL, ["nx_total", "ny_total"]),
        _arg("u_l", "out", REAL, ["nx + 1", "ny"]), _arg("u_r", "out", REAL, ["nx", "ny + 1"]),
        _arg("ok", "out", LOGICAL)]}
    drive = {
        "distribute": {"axes": [
            {"name": "x", "cells": "inputs.grid.nx", "ghost": "inputs.grid.ng",
             "extended": "inputs.grid.nx_total"},
            {"name": "y", "cells": "inputs.grid.ny", "ghost": "inputs.grid.ng",
             "extended": "inputs.grid.ny_total"}]},
        "step": {"operation": f"{sid}__apply", "result": "ok", "arguments": {
            "nx": "inputs.grid.nx", "ny": "inputs.grid.ny", "ng": "inputs.grid.ng",
            "nx_total": "inputs.grid.nx_total", "ny_total": "inputs.grid.ny_total",
            "u_in": "state.u_in", "u_l": "state.u_l", "u_r": "state.u_r", "ok": "state.ok"}}}

    def case(cid: str, nx: int, ny: int, ng: int, nx_total: int | None = None) -> dict:
        nxt, nyt = (nx + 2 * ng if nx_total is None else nx_total), ny + 2 * ng
        u = [[-999.0 if not (ng <= i < nx + ng and ng <= j < ny + ng) else
              1.0 + 0.1 * (i - ng + 1) + 0.37 * (j - ng + 1) + 0.013 * (i - ng + 1) ** 2
              * (j - ng + 1) for j in range(nyt)] for i in range(nxt)]
        return {"case_id": cid, "inputs": {"grid": {"nx": nx, "ny": ny, "ng": ng,
                                                    "nx_total": nxt, "ny_total": nyt}},
                "initial_state": {"u_in": u, "u_l": -999.0, "u_r": -999.0}}
    cases = [case("c6x4", 6, 4, 2), case("c12x8", 12, 8, 2), case("c1x1", 1, 1, 2),
             case("cbad", 6, 4, 2, nx_total=9)]
    tests, predicates = _one_test_per_case(
        cases, [("ok", "final.ok", "eq", 1),
                ("faces", "maxabs(final.u_l) + maxabs(final.u_r)", "gt", 0.0)],
        xfail={"cbad": [("ok", "final.ok", "eq", 0),
                        ("faces", "maxabs(final.u_l) + maxabs(final.u_r)", "gt", 0.0)]})
    return _mpi_ir(sid, "component", ops, drive, cases,
                   {"u_in": "[nx_total, ny_total]", "u_l": "[nx + 1, ny]",
                    "u_r": "[nx, ny + 1]", "ok": "scalar"}, tests, predicates)


def prob_ir() -> dict[str, Any]:
    """Model (2), a stencil `problem`: periodic along x, walls along y, `__init` from the block's
    offsets (`block.<axis>.first`) and the global counts (`derived` values), a mid-step exchange,
    the walls restored by global index, a rank-local refusal at step `refuse_at` (`ny8r`), and a
    block-local `init` refusal on the block holding the global last cell (`ny8i`, channel's
    `__init` depth refusal in shape): the result's reduction (B8) refuses the case on every rank,
    and without it the other ranks go on alone into a collective (a hang).
    `min_block 2`: `ny4` replicates on `(1, 4)` and `(1, 3)` and distributes on `(1, 2)`, `(2, 2)`."""
    sid = "fx_prob"
    ops = {
        f"{sid}__init": [
            _arg("nx", "in", INT), _arg("ny", "in", INT), _arg("nx_g", "in", INT),
            _arg("ny_g", "in", INT), _arg("i0", "in", INT), _arg("j0", "in", INT),
            _arg("refuse_init", "in", INT),
            _arg("h", "out", REAL, ["nx", "ny"]), _arg("dt", "out", REAL),
            _arg("n_cells", "out", INT), _arg("ok", "out", LOGICAL)],
        f"{sid}__step": [
            _arg("nx", "in", INT), _arg("ny", "in", INT), _arg("nx_g", "in", INT),
            _arg("ny_g", "in", INT), _arg("i0", "in", INT), _arg("j0", "in", INT),
            _arg("refuse_at", "in", INT), _arg("stepno", "in", INT), _arg("dt", "in", REAL),
            _arg("h", "inout", REAL, ["nx", "ny"]), _arg("ok", "out", LOGICAL)]}
    common = {"nx": "inputs.nx", "ny": "inputs.ny", "nx_g": "derived.nx_g",
              "ny_g": "derived.ny_g", "i0": "block.x.first", "j0": "block.y.first"}
    drive = {
        "distribute": {"axes": [{"name": "x", "cells": "inputs.nx", "min_block": 2},
                                {"name": "y", "cells": "inputs.ny", "min_block": 2}]},
        "init": {"operation": f"{sid}__init", "result": "ok", "arguments": {
            **common, "refuse_init": "inputs.refuse_init", "h": "state.h", "dt": "state.dt",
            "n_cells": "state.n_cells", "ok": "state.init_ok"}},
        "step": {"operation": f"{sid}__step", "result": "ok", "arguments": {
            **common, "refuse_at": "inputs.refuse_at", "stepno": "step", "dt": "state.dt",
            "h": "state.h", "ok": "state.step_ok"}},
        "loop": {"n_step": "inputs.n_step", "dt": "state.dt", "t_start": "inputs.t_start",
                 "t_end": "inputs.t_end"},
        "history": {"every": "inputs.every"}}
    cases = [{"case_id": cid, "derived": {"nx_g": "inputs.nx", "ny_g": "inputs.ny"},
              "inputs": {"nx": 8, "ny": ny, "refuse_at": refuse_at, "refuse_init": refuse_init,
                         "n_step": 6, "t_start": 0.0, "t_end": 1.0, "every": 2}}
             for cid, ny, refuse_at, refuse_init in (("ny4", 4, -1, 0), ("ny8", 8, -1, 0),
                                                     ("ny8r", 8, 3, 0), ("ny8i", 8, -1, 1))]
    tests, predicates = _one_test_per_case(
        cases, [("init", "min(initial.h) + initial.dt + initial.init_ok", "gt", 1.0),
                ("n_cells", "initial.n_cells - inputs.nx * inputs.ny", "eq", 0),
                ("run.n_step", "final.step - inputs.n_step", "eq", 0),
                ("h", "maxabs(final.h)", "gt", 0.0)],
        xfail={"ny8r": [("step_ok", "final.step_ok", "eq", 0),
                        ("h", "maxabs(final.h)", "gt", 0.0)],
               "ny8i": [("init_ok", "final.init_ok", "eq", 0),
                        ("h", "maxabs(final.h)", "gt", 0.0)]})
    return _mpi_ir(sid, "problem", ops, drive, cases,
                   {"h": "[nx, ny]", "dt": "scalar", "n_cells": "scalar", "init_ok": "scalar",
                    "step_ok": "scalar"},
                   tests, predicates)


def wit_ir() -> dict[str, Any]:
    """Model (3), the witness: `w` holds the `nx` the call was handed, `wb` the `nx_b` of a
    second dummy bound to the same input, `wg` the `nx_g` bound to a `derived` value."""
    sid = "fx_wit"
    ops = {f"{sid}__apply": [
        _arg("nx", "in", INT), _arg("nx_b", "in", INT), _arg("nx_g", "in", INT),
        _arg("w", "out", REAL, ["nx"]), _arg("wb", "out", REAL, ["nx"]),
        _arg("wg", "out", REAL, ["nx"]), _arg("ok", "out", LOGICAL)]}
    drive = {"distribute": {"axes": [{"name": "x", "cells": "inputs.nx"}]},
             "step": {"operation": f"{sid}__apply", "result": "ok", "arguments": {
                 "nx": "inputs.nx", "nx_b": "inputs.nx", "nx_g": "derived.nx_g",
                 "w": "state.w", "wb": "state.wb", "wg": "state.wg", "ok": "state.ok"}}}
    cases = [{"case_id": "c6", "derived": {"nx_g": "inputs.nx"}, "inputs": {"nx": 6},
              "initial_state": {"w": -999.0, "wb": -999.0, "wg": -999.0}}]
    tests, predicates = _one_test_per_case(cases, [("ok", "final.ok", "eq", 1),
                                                   ("w", "maxabs(final.w)", "gt", 0.0)])
    return _mpi_ir(sid, "component", ops, drive, cases,
                   {"w": "[nx]", "wb": "[nx]", "wg": "[nx]", "ok": "scalar"}, tests, predicates)


def loop_mpi_ir() -> dict[str, Any]:
    """Model (4), a `loop` component in rk4's and tc4's shape: `u [ncomp, nx, ny]` from the
    case file, the host arrays `derived.f`, `derived.x`, `derived.y`, a procedure binding whose
    `record` fills two states from the prototype's `q` (named like no state) and a `[4]` stamp,
    a ghosted `in` state `ug [nx + 2*ng]` (domain ghosts a sentinel) and a cell `in` state
    `zb [nx, ny]`."""
    sid = "fx_loop"
    proto = {"kind": "subroutine", "name": "fx_rhs_proto", "args": [
        _arg("ncomp", "in", INT), _arg("nx", "in", INT), _arg("ny", "in", INT),
        _arg("t", "in", REAL), _arg("q", "in", REAL, ["ncomp", "nx", "ny"]),
        _arg("dqdt", "out", REAL, ["ncomp", "nx", "ny"])]}
    ops = {f"{sid}__advance": [
        _arg("ncomp", "in", INT), _arg("nx", "in", INT), _arg("ny", "in", INT),
        _arg("ng", "in", INT), _arg("u", "in", REAL, ["ncomp", "nx", "ny"]),
        _arg("ug", "in", REAL, ["nx + 2*ng"]), _arg("zb", "in", REAL, ["nx", "ny"]),
        _arg("f", "in", REAL, ["nx", "ny"]), _arg("x", "in", REAL, ["nx"]),
        _arg("y", "in", REAL, ["ny"]),
        {"name": "rhs", "spec": {"type": "procedure", "interface": "fx_rhs_proto"}},
        _arg("t", "in", REAL), _arg("dt", "in", REAL),
        _arg("u_np1", "out", REAL, ["ncomp", "nx", "ny"]), _arg("ok", "out", LOGICAL)]}
    drive = {
        "distribute": {"axes": [
            {"name": "x", "cells": "inputs.grid.nx", "ghost": "inputs.grid.ng"},
            {"name": "y", "cells": "inputs.grid.ny"}]},
        "step": {"operation": f"{sid}__advance", "result": "ok", "arguments": {
            "ncomp": "inputs.grid.ncomp", "nx": "inputs.grid.nx", "ny": "inputs.grid.ny",
            "ng": "inputs.grid.ng", "u": "state.u", "ug": "state.ug", "zb": "state.zb",
            "f": "derived.f", "x": "derived.x", "y": "derived.y",
            "rhs": {"procedure": "inputs.lam * q", "calls": "state.n_calls",
                    "record": {"t": "state.stage_t",
                               "q": ["state.stage_q1", "state.stage_q2"]}},
            "t": "time", "dt": "inputs.time.dt", "u_np1": "state.u", "ok": "state.ok"}},
        "loop": {"n_step": "inputs.time.n_step", "dt": "inputs.time.dt",
                 "t_start": "inputs.time.t_start", "t_end": "inputs.time.t_end"},
        "history": {"every": "inputs.history.every"}}

    def case(cid: str, nx: int, ny: int, ng: int) -> dict[str, Any]:
        u = [[[(k + 1) * (i + 1) + 10 * (j + 1) + 0.01 * i * j for j in range(ny)]
              for i in range(nx)] for k in range(2)]
        ug = [-999.0 if (i < ng or i >= nx + ng) else (i - ng + 1) * 0.5
              for i in range(nx + 2 * ng)]
        return {"case_id": cid,
                "derived": {"f": "inputs.fld.f", "x": "inputs.fld.x", "y": "inputs.fld.y"},
                "inputs": {"grid": {"ncomp": 2, "nx": nx, "ny": ny, "ng": ng}, "lam": 0.5,
                           "fld": {"f": [[1.0 + 0.1 * i - 0.05 * j for j in range(ny)]
                                         for i in range(nx)],
                                   "x": [0.5 + i for i in range(nx)],
                                   "y": [100.0 + 2 * j for j in range(ny)]},
                           "time": {"dt": 0.1, "n_step": 3, "t_start": 0.0, "t_end": 0.3},
                           "history": {"every": 1}},
                "initial_state": {"u": u, "ug": ug, "zb": [[0.2 * i + 0.03 * j for j in range(ny)]
                                                           for i in range(nx)]}}
    cases = [case("big", 12, 8, 2), case("nearly", 9, 5, 1), case("tiny", 3, 1, 2)]
    tests, predicates = _one_test_per_case(
        cases, [("ok", "final.ok", "eq", 1),
                ("run.n_step", "final.step - inputs.time.n_step", "eq", 0),
                ("u", "maxabs(final.u)", "gt", 0.0)])
    return _mpi_ir(sid, "component", ops, drive, cases,
                   {"u": "[ncomp, nx, ny]", "ok": "scalar", "n_calls": "scalar",
                    "stage_t": "[4]", "stage_q1": "[ncomp, nx, ny]",
                    "stage_q2": "[ncomp, nx, ny]", "ug": "[nx + 2*ng]", "zb": "[nx, ny]"},
                   tests, predicates, interfaces=(proto,))


def lst_ir(record_first: bool = False) -> dict[str, Any]:
    """Model (5), a list-form `component` with axis `x` (review round 2 of PR-B2): case `a`'s
    entry reads `q [nx]` from the case file, case `b`'s entry has `q` recorded by a procedure
    binding (the prototype's `in` dummy `q`), so `q` is file-backed in `a`'s entry and
    driver-written in `b`'s. ``record_first`` lists `b`'s entry first (issue #613: the entry
    order carries no meaning, so the captures are the same). `a` has 8 cells and `b` 6, so with
    `min_block` 2 both distribute on `(2, 1)` and `(2, 2)` and `b` alone replicates on
    `(4, 1)`."""
    sid = "fx_lst"
    proto = {"kind": "subroutine", "name": "fx_lst_proto", "args": [
        _arg("nx", "in", INT), _arg("q", "in", REAL, ["nx"]), _arg("dqdt", "out", REAL, ["nx"])]}
    ops = {f"{sid}__plain": [_arg("nx", "in", INT), _arg("q", "in", REAL, ["nx"]),
                              _arg("w", "out", REAL, ["nx"]), _arg("ok", "out", LOGICAL)],
           f"{sid}__proc": [_arg("nx", "in", INT),
                             {"name": "rhs", "spec": {"type": "procedure",
                                                      "interface": "fx_lst_proto"}},
                             _arg("w", "out", REAL, ["nx"]), _arg("ok", "out", LOGICAL)]}
    drive = {"distribute": {"axes": [{"name": "x", "cells": "inputs.nx", "min_block": 2}]},
             "step": [
                 {"operation": f"{sid}__plain", "result": "ok", "cases": ["a"],
                  "arguments": {"nx": "inputs.nx", "q": "state.q", "w": "state.w",
                                "ok": "state.ok"}},
                 {"operation": f"{sid}__proc", "result": "ok", "cases": ["b"],
                  "arguments": {"nx": "inputs.nx",
                                "rhs": {"procedure": "inputs.lam * q",
                                        "record": {"q": "state.q"}},
                                "w": "state.w", "ok": "state.ok"}}]}
    cases = [{"case_id": "a", "inputs": {"nx": 8, "lam": 0.5},
              "initial_state": {"q": [1.0 + i for i in range(8)], "w": -999.0}},
             {"case_id": "b", "inputs": {"nx": 6, "lam": 0.5},
              "initial_state": {"w": -999.0}}]
    if record_first:
        drive["step"].reverse()
    tests, predicates = _one_test_per_case(cases, [("ok", "final.ok", "eq", 1),
                                                   ("w", "maxabs(final.w)", "gt", 0.0)])
    return _mpi_ir(sid, "component", ops, drive, cases,
                   {"q": "[nx]", "w": "[nx]", "ok": "scalar"}, tests, predicates,
                   interfaces=(proto,))


def _lst_entry(ir: dict[str, Any], operation: str) -> dict[str, Any]:
    """The `drive.step` entry of `lst_ir`'s IR that runs ``operation``, in either entry order."""
    (entry,) = [e for e in ir["drive"]["step"] if e["operation"] == operation]
    return entry


def lst_init_procedure_ir(record_first: bool = False) -> dict[str, Any]:
    """`lst_ir` whose `drive.init` is the `__proc` call recording `r` (issue #613): `init` is
    planned once per entry, so each entry renders its own copy of `init`'s procedure, and `r`
    is driver-written in every case. Gate (k) requires a predicate on each `init` output."""
    ir = lst_ir(record_first)
    init = copy.deepcopy(_lst_entry(ir, "fx_lst__proc"))
    init.pop("cases")
    init["arguments"] = {"nx": "inputs.nx",
                         "rhs": {"procedure": "inputs.lam * q", "record": {"q": "state.r"}},
                         "w": "state.wi", "ok": "state.oki"}
    ir["drive"]["init"] = init
    ir["io_contract"]["raw_requirements"]["required_evidence"][0]["schema"]["variables"] += [
        {"name": "r", "shape_expr": "[nx]"}, {"name": "wi", "shape_expr": "[nx]"},
        {"name": "oki", "shape_expr": "scalar"}]
    ir["io_contract"]["tests"], ir["io_contract"]["predicates"] = _one_test_per_case(
        ir["case"]["test_case_set"], [
            ("ok", "final.ok", "eq", 1), ("w", "maxabs(final.w)", "gt", 0.0),
            ("oki", "initial.oki", "eq", 1), ("wi", "maxabs(initial.wi)", "ge", 0.0)])
    return ir


def many_procedures_ir(n: int) -> dict[str, Any]:
    """``n`` list entries, each a copy of `lst_ir`'s `__proc` entry for its own case `b<i>`:
    ``n`` host-rendered procedures in one module (issue #613)."""
    ir = lst_ir()
    proc = _lst_entry(ir, "fx_lst__proc")
    ir["drive"]["step"] = [{**copy.deepcopy(proc), "cases": [f"b{i}"]} for i in range(n)]
    cases = [{"case_id": f"b{i}", "inputs": {"nx": 6, "lam": 0.5},
              "initial_state": {"w": -999.0}} for i in range(n)]
    ir["case"]["test_case_set"] = cases
    ir["io_contract"]["tests"], ir["io_contract"]["predicates"] = _one_test_per_case(
        cases, [("ok", "final.ok", "eq", 1), ("w", "maxabs(final.w)", "gt", 0.0)])
    return ir


MPI_FIXTURES = {"fx_recon": recon_ir, "fx_prob": prob_ir, "fx_wit": wit_ir,
                "fx_loop": loop_mpi_ir, "fx_lst": lst_ir}


class MpiRun:
    """One run of a distributed binary: ``rc`` None when it was killed at the timeout."""

    def __init__(self, directory: Path, rc: int | None, stderr: str) -> None:
        self.dir, self.rc, self.stderr = directory, rc, stderr

    def snapshots(self) -> dict[str, Any]:
        root = self.dir / "raw" / "state_snapshots"
        return {str(p.relative_to(root)): json.loads(p.read_text())
                for p in sorted(root.rglob("*.json"))}

    def perf(self) -> dict[str, Any]:
        return json.loads((self.dir / "perf.json").read_text())


class MpiBuild:
    """A fixture model's distributed driver, rendered for `fortran_cpu_mpi` and built with the MPI
    pair `test_parallel_mpi` resolves (a missing pair fails, #498), in a temporary directory."""

    def __init__(self, tmp: Path, ir: dict[str, Any], *, flags: list[str],
                 model: str | None = None, runner: str | None = None,
                 harness: str = MPI_HARNESS) -> None:
        from tools.backends.parallel.mpi import launcher, wrapper
        from tools.tests.test_parallel_mpi import _require_mpi_pair
        pair = Path(_require_mpi_pair())
        self.launcher = str(pair / launcher.EXECUTABLE)
        self.tmp, self.ir = tmp, ir
        sid = ir["meta"]["spec_id"]
        (tmp / "ir").mkdir(parents=True)
        (tmp / "ir" / "spec.ir.yaml").write_text(yaml.safe_dump(ir))
        pe.write_case_inputs(ir, tmp / "ir")
        self.runner = runner if runner is not None else f_runner.render_runner(
            ir, sid, harness, target=_mpi_target())
        (tmp / "runner.f90").write_text(self.runner)
        (tmp / "model.f90").write_text(model if model is not None
                                       else (MPI_DATA / f"{sid}_model.f90").read_text())
        harness_src = (MPI_DATA / f"{MPI_HARNESS}_model.f90" if harness == MPI_HARNESS
                       else DATA / "harness_fortran_cpu_model.f90")
        compiler = str(pair / wrapper.COMPILER_WRAPPER) if harness == MPI_HARNESS else "gfortran"
        _compile_cached([compiler, *flags, "-c", str(harness_src), "-o", "harness.o"], tmp,
                        [harness_src])
        objs = ["harness.o"]
        for stem, src in (("model", tmp / "model.f90"), ("runner", tmp / "runner.f90")):
            build = subprocess.run([compiler, *flags, "-c", str(src), "-o", f"{stem}.o"],
                                   cwd=tmp, capture_output=True, text=True)
            if build.returncode != 0:
                raise AssertionError(f"{stem}: {build.stderr[:3000]}")
            objs.append(f"{stem}.o")
        subprocess.run([compiler, *flags, *objs, "-o", "bin"], cwd=tmp, check=True,
                       capture_output=True)

    def run(self, grid: tuple[int, int], *, cases: list[str] | None = None,
            timeout: float = 120) -> MpiRun:
        """Run under ``grid``: the singleton bare, as the quality check runs it, and a grid of
        several ranks under the launcher with `--oversubscribe` and the grid variable. A run past
        ``timeout`` is killed with its whole process group (the ranks with the launcher)."""
        import signal
        from tools.backends.parallel.mpi import execution
        d = self.tmp / f"run_{grid[0]}x{grid[1]}"
        shutil.rmtree(d, ignore_errors=True)
        for sub in ("initial", "history", "series"):   # what `_run_raw_dirs` creates
            (d / "raw" / "state_snapshots" / sub).mkdir(parents=True)
        env = {k: v for k, v in os.environ.items() if k != execution.PROCESS_GRID_VARIABLE}
        argv = [str(self.tmp / "bin")]
        if grid != SINGLETON:
            env.update(execution.environment(1, process_grid=grid))
            argv = [self.launcher, "--oversubscribe", "-n", str(grid[0] * grid[1]), *argv]
        ids = cases if cases is not None else [c["case_id"] for c in
                                               self.ir["case"]["test_case_set"]]
        # Two kills, because a seeded kernel defect HANGS on purpose (a rank alone in a
        # collective) and its own session outlives this process. The `finally` covers any way
        # out of `communicate` other than the timeout (an interrupt, a failure in this frame).
        # A SIGKILLed worker runs no `finally`, so the run also sits under coreutils `timeout`,
        # a minute past the deadline here. TERM first, KILL ten seconds later: the launcher
        # puts each rank in a process group of its own, so no group signal reaches a rank, and
        # it is the launcher that forwards TERM to them (measured 2026-11-11 with a rank that is
        # not an MPI program: under a KILL-only backstop the four ranks stayed under init, under
        # TERM none did). The same day the `ny8r` hang of a killed run had held four cores for
        # 2.5 h. `timeout` passes an exit status through; a child killed by a signal reads 128+N
        # instead of -N, still non-zero, which is all a row asks of `rc`.
        argv = ["timeout", "-k", "10", str(int(timeout) + 60), *argv]
        proc = subprocess.Popen([*argv, "--cases", str(self.tmp / "ir" / "spec.ir.yaml"), *ids],
                                cwd=d, env=env, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, text=True, start_new_session=True,
                                stdin=subprocess.DEVNULL)
        try:
            _out, err = proc.communicate(timeout=timeout)
            return MpiRun(d, proc.returncode, err)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            return MpiRun(d, None, "timeout")
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()

    def verdict(self, run: MpiRun) -> dict[str, str]:
        records = pe.evaluate_primary_predicates(self.ir, run.dir)
        verdict = verdict_evaluator.evaluate_verdict(self.ir["io_contract"]["tests"], records)
        return {t["test_id"]: t["status"] for t in verdict["per_test"]}


def _witness_expectation(ir: dict[str, Any], grid: tuple[int, int]) -> dict[str, list[float]]:
    """What model (3)'s capture holds under ``grid``: on a case `distribute_plan` distributes,
    each cell of `w` and `wb` the count of the block holding it (the harness's partition: the
    first `nx mod p` ranks of a line one cell more), on a replicated one the case's `nx`; `wg` the
    case's `nx` everywhere."""
    case = ir["case"]["test_case_set"][0]
    nx = case["inputs"]["nx"]
    if runner_ir.distribute_plan(ir, case, grid):
        p = grid[0]
        blocks = [nx // p + (1 if c < nx % p else 0) for c in range(p)]
        local = [float(b) for b in blocks for _ in range(b)]
    else:
        local = [float(nx)] * nx
    return {"w": local, "wb": local, "wg": [float(nx)] * nx}


class DistributedDriverEndToEndTest(unittest.TestCase):
    """The distributed form of the host-rendered driver (issue #610, plan B §3.3), compiled with
    the fixture harness and run with the real launcher on every grid of `fortran_cpu_mpi`
    and bare as the singleton: each shape's captures and verdict equal the singleton's. The
    seeded defects below show what each comparison detects; the documented-limit row shows the
    one thing the capture is silent on (B10)."""

    @classmethod
    def setUpClass(cls) -> None:
        from tools.tests.test_parallel_mpi import _require_mpi_pair
        _require_mpi_pair()

    def setUp(self) -> None:  # noqa: N802
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._n = 0

    def _build(self, ir: dict[str, Any], *, flags: str = "release", **kw: Any) -> MpiBuild:
        self._n += 1
        return MpiBuild(Path(self._tmp.name) / f"b{self._n}", ir,
                        flags=_mpi_flag_sets()[flags], **kw)

    def _first_disagreement(self, build: MpiBuild, reference: dict[str, Any],
                            grids: tuple[tuple[int, int], ...] = MPI_GRIDS,
                            **kw: Any) -> tuple[tuple[int, int], str] | None:
        """The first grid whose run fails or whose captures differ from ``reference``."""
        for grid in grids:
            run = build.run(grid, **kw)
            if run.rc != 0:
                return grid, f"rc {run.rc}: {run.stderr[-300:]}"
            got = run.snapshots()
            if got != reference:
                bad = sorted(k for k in reference if got.get(k) != reference[k])
                return grid, f"captures differ: {bad[:3]}"
        return None

    def _agree_on_every_grid(self, sid: str, flags: str) -> None:
        ir = MPI_FIXTURES[sid]()
        build = self._build(ir, flags=flags)
        single = build.run(SINGLETON)
        self.assertEqual(single.rc, 0, single.stderr[-2000:])
        reference, verdict = single.snapshots(), build.verdict(single)
        self.assertEqual(verdict, {t["test_id"]: t["expected_outcome"]
                                   for t in ir["io_contract"]["tests"]})
        serial_cells = single.perf()["cells_updated"]
        for grid in MPI_GRIDS:
            # A hang (a rank alone in a collective) is killed at the timeout and ends the row:
            # one bounded wait, not one per grid.
            run = build.run(grid, timeout=60)
            if run.rc is None:
                self.fail(f"{sid} {flags} {grid}: killed at the timeout (a hang)")
            with self.subTest(sid=sid, flags=flags, grid=grid):
                self.assertEqual(run.rc, 0, run.stderr[-2000:])
                got = run.snapshots()
                if sid == "fx_wit":
                    final = got["c6.json"]
                    self.assertEqual({k: final[k] for k in ("w", "wb", "wg")},
                                     _witness_expectation(ir, grid))
                else:
                    self.assertEqual(sorted(got), sorted(reference))
                    for name in reference:
                        self.assertEqual(got[name], reference[name], name)
                self.assertEqual(build.verdict(run), verdict)
                perf = run.perf()
                self.assertEqual(perf["parallelism"]["mpi_ranks"], grid[0] * grid[1])
                self.assertEqual(perf["parallelism"]["process_grid"], list(grid))
                self.assertEqual(perf["cells_updated"], serial_cells)

    def test_the_fixture_irs_pass_gates_n_and_o_and_the_harness_pin(self) -> None:
        """Each fixture IR is one `Compile.static` admits (gates (a)-(n), and gate (o) against its
        `tests.md` line), and the fixture harness publishes what the renderer pins against the
        real `harness_fortran_cpu_mpi@0.4.0` §5.1 (plan B §8 (k))."""
        from tools.validate_pipeline_semantics import _distribute_line_violations
        spec = (ROOT / "spec/infrastructure/infra/harness/harness_fortran_cpu_mpi/"
                       "controlled_spec.md").read_text()
        block = yaml.safe_load(spec.split("### 5.1", 1)[1].split("```yaml", 1)[1].split("```", 1)[0])
        signatures = [{"symbol": t["name"], "signature": t} for t in block["types"]] + [
            {"symbol": p["name"], "signature": p} for p in block["procedures"]]
        source = (MPI_DATA / f"{MPI_HARNESS}_model.f90").read_text()
        for sid, builder in MPI_FIXTURES.items():
            with self.subTest(sid=sid):
                ir = builder()
                kind = ir["meta"]["spec_kind"]
                self.assertEqual(pe.drive_violations(ir, kind=kind), [])
                self.assertEqual(_distribute_line_violations(
                    MPI_DATA / f"{sid}.tests.md", ir, kind=kind), [])
                f_runner.assert_harness_pin(ir, sid, MPI_HARNESS, signatures, source)

    def test_the_singleton_is_the_serial_driver(self) -> None:
        """On `(1, 1)` every case is replicated (B6), so the distributed binary bare writes what
        the serial driver over the single-process harness writes, `cells_updated` included."""
        for sid in ("fx_recon", "fx_wit", "fx_loop", "fx_lst"):
            with self.subTest(sid=sid):
                ir = MPI_FIXTURES[sid]()
                single = self._build(ir).run(SINGLETON)
                self.assertEqual(single.rc, 0, single.stderr[-2000:])
                serial = self._build(ir, harness="harness_fortran_cpu",
                                     flags="release").run(SINGLETON)
                self.assertEqual(serial.rc, 0, serial.stderr[-2000:])
                self.assertEqual(single.snapshots(), serial.snapshots())
                self.assertEqual(single.perf()["cells_updated"], serial.perf()["cells_updated"])
                self.assertEqual(single.perf()["steps"], serial.perf()["steps"])

    def test_the_witness_tells_a_replicated_case_from_a_distributed_one(self) -> None:
        """With the real `distribute_plan` the `(2, 1)` capture holds the block counts and the
        singleton the case's; with `distribute_plan` patched to replicate the case, the `(2, 1)`
        capture holds the case's: the row that fails if the driver distributes a replicated
        case or replicates a distributed one. A second dummy bound to the same cell-count input
        takes the block count too; the count a `derived` entry carries does not."""
        ir = wit_ir()
        self.assertTrue(runner_ir.distribute_plan(ir, ir["case"]["test_case_set"][0], (2, 1)))
        real = self._build(ir).run((2, 1))
        self.assertEqual(real.rc, 0, real.stderr[-1000:])
        final = real.snapshots()["c6.json"]
        self.assertEqual(final["w"], [3.0] * 6)
        self.assertEqual(final["wb"], [3.0] * 6)
        self.assertEqual(final["wg"], [6.0] * 6)
        with mock.patch.object(runner_ir, "distribute_plan", return_value=False):
            replicated = self._build(ir).run((2, 1))
        self.assertEqual(replicated.rc, 0, replicated.stderr[-1000:])
        self.assertEqual(replicated.snapshots()["c6.json"]["w"], [6.0] * 6)

    def test_a_one_cell_block_under_a_two_cell_ghost_distributes_and_agrees(self) -> None:
        """B6's threshold has no `ghost` term: model (1)'s 6x4 case with `ng = 2` is distributed
        under `(1, 4)`, one cell per block along y, and agrees with the singleton (the margin is
        sliced from the global array)."""
        ir = recon_ir()
        case = next(c for c in ir["case"]["test_case_set"] if c["case_id"] == "c6x4")
        self.assertTrue(runner_ir.distribute_plan(ir, case, (1, 4)))
        build = self._build(ir, flags="checked")
        reference = build.run(SINGLETON, cases=["c6x4"]).snapshots()
        self.assertIsNone(self._first_disagreement(build, reference, ((1, 4),), cases=["c6x4"]))

    def test_the_rendered_table_lists_each_grid_once_and_no_wider_than_the_dry_render(self) -> None:
        target = _mpi_target()
        target["execution"]["sweep"]["grids"] = [[2, 2], [4, 1], [1, 1], [4, 1]]
        self.assertEqual(runner_ir.distribute_grids(target), ((2, 2), (4, 1), (1, 1)))
        text = f_runner.render_runner(recon_ir(), "fx_recon", MPI_HARNESS, target=target)
        self.assertEqual(text.count("if (p1 == 4 .and. p2 == 1) grid_known = .true."), 1)
        dry = f_runner.render_runner(recon_ir(), "fx_recon", MPI_HARNESS,
                                     target=f_runner._DRY_RUN_TARGET)
        self.assertIn("if (p1 == 1 .and. p2 == 1) grid_known = .true.", dry)
        self.assertNotIn("p1 == 2", dry)
        width = max(len(line) for line in dry.splitlines())
        self.assertLessEqual(max(len(line) for line in _mpi_runner_text("fx_recon")
                                 .splitlines()), width)

    def _driver_defect_fails_a_grid(self, sid: str, label: str, old: str, new: str) -> None:
        """Plan B §5's driver defects (`_DRIVER_DEFECTS`, one row each), each seeded into the
        rendered text of the model whose captures show it: every one fails — a run failure, a hang
        killed at the timeout, or captures that differ from the singleton's (on the witness, from
        what `distribute_plan` implies) — on at least one grid."""
        ir = MPI_FIXTURES[sid]()
        text = _mpi_runner_text(sid)
        self.assertEqual(text.count(old), 1, (label, old))
        build = self._build(ir, runner=text.replace(old, new))
        if sid == "fx_wit":
            run = build.run((2, 1))
            self.assertNotEqual(
                {k: run.snapshots()["c6.json"][k] for k in ("w", "wb", "wg")}
                if run.rc == 0 else None, _witness_expectation(ir, (2, 1)), label)
            return
        reference = self._build(ir).run(SINGLETON).snapshots()
        self.assertIsNotNone(self._first_disagreement(build, reference, timeout=30), label)

    def _kernel_defect_fails_a_grid(self, sid: str, label: str,
                                    edits: tuple[tuple[str, str], ...], cases: list[str],
                                    grids: tuple[tuple[int, int], ...]) -> None:
        """Plan B §6's kernel defects (`_KERNEL_DEFECTS`, one row each): on the singleton each
        writes what the correct model writes (every case replicated, a grid of one rank), and
        each fails a grid of `MPI_GRIDS` — a hang among them, which the timeout kills with the ranks."""
        ir = MPI_FIXTURES[sid]()
        model = (MPI_DATA / f"{sid}_model.f90").read_text()
        for old, new in edits:
            self.assertEqual(model.count(old), 1, (label, old))
            model = model.replace(old, new)
        reference = self._build(ir).run(SINGLETON, cases=cases).snapshots()
        build = self._build(ir, model=model)
        single = build.run(SINGLETON, cases=cases)
        self.assertEqual(single.rc, 0, single.stderr[-1000:])
        self.assertEqual(single.snapshots(), reference)
        self.assertIsNotNone(self._first_disagreement(
            build, reference, grids, cases=cases, timeout=30), label)

    def test_an_init_scalar_unequal_across_the_ranks_stops_the_run(self) -> None:
        """B8: after an accepted `init`, a rank-0 real or integer it wrote must agree across the
        ranks. Model (2) taking its step size from its block's counts gives each rank its own `dt`
        under `(3, 1)` (blocks of 3, 3 and 2 cells), and the run stops naming it; a `(2, 2)` run,
        whose blocks are equal, agrees across the ranks and differs from the singleton instead."""
        ir = prob_ir()
        model = (MPI_DATA / "fx_prob_model.f90").read_text()
        old = "    dt = 0.8_dp / real(nx_g + ny_g, dp)"
        self.assertEqual(model.count(old), 1)
        build = self._build(ir, model=model.replace(old, "    dt = 0.8_dp / real(nx + ny, dp)"))
        run = build.run((3, 1), cases=["ny8"])
        self.assertNotEqual(run.rc, 0)
        self.assertIn("error: init wrote dt unequal across the ranks for case ny8", run.stderr)
        self.assertEqual(build.run(SINGLETON, cases=["ny8"]).rc, 0)
        nan = model.replace(old, "    dt = merge(ieee_value(1.0_dp, ieee_quiet_nan), 0.1_dp, "
                                 "i0 > 1)").replace(
            "  use harness_fortran_cpu_mpi_model,",
            "  use, intrinsic :: ieee_arithmetic, only: ieee_value, ieee_quiet_nan\n"
            "  use harness_fortran_cpu_mpi_model,")
        run = self._build(ir, model=nan).run((2, 1), cases=["ny8"])
        self.assertNotEqual(run.rc, 0)
        self.assertIn("error: init wrote a non-finite dt on a rank for case ny8", run.stderr)
        # An integer `out` is checked the same way (channel's `n_step_plan`): the count of the
        # block's cells instead of the field's differs per rank under `(3, 1)`.
        old = "    n_cells = nx_g * ny_g"
        self.assertEqual(model.count(old), 1)
        build = self._build(ir, model=model.replace(old, "    n_cells = nx * ny"))
        run = build.run((3, 1), cases=["ny8"])
        self.assertNotEqual(run.rc, 0)
        self.assertIn("error: init wrote n_cells unequal across the ranks for case ny8",
                      run.stderr)
        self.assertEqual(build.run(SINGLETON, cases=["ny8"]).rc, 0)

    def test_the_perf_size_of_a_state_comes_from_the_entry_that_ran_the_case(self) -> None:
        """Model (5): `q` is read from the file in case `a`'s entry and recorded in case `b`'s,
        so `cells_updated` is 8 for `a` and 6 for `b` whatever the order and the grid — not the
        size of `a`'s global copy of `q` (16 for `a b`), nor of one never allocated (`b` alone,
        or first). Under the `checked` flags, on the singleton, on grids that distribute both
        cases, and on `(4, 1)`, which replicates `b` alone (6 cells under 4 ranks of
        `min_block` 2)."""
        for record_first in (False, True):     # the entry order too (issue #613)
            ir = lst_ir(record_first)
            cases = {c["case_id"]: c for c in ir["case"]["test_case_set"]}
            self.assertFalse(runner_ir.distribute_plan(ir, cases["b"], (4, 1)))
            self.assertTrue(runner_ir.distribute_plan(ir, cases["a"], (4, 1)))
            build = self._build(ir, flags="checked")
            for ids, cells in ((["a", "b"], 14), (["b"], 6), (["b", "a"], 14), (["a"], 8)):
                for grid in (SINGLETON, (2, 1), (4, 1), (2, 2)):
                    with self.subTest(record_first=record_first, cases=ids, grid=grid):
                        run = build.run(grid, cases=ids, timeout=60)
                        self.assertEqual(run.rc, 0, run.stderr[-1000:])
                        self.assertEqual(run.perf()["cells_updated"], cells)

    def test_a_recording_entry_listed_first_leaves_the_other_entry_s_state_file_backed(
            self) -> None:
        """Issue #613: model (5) in either entry order reads `q` from the case file in case
        `a` (whose entry records nothing) and zeroes it in case `b` (whose procedure records
        it), under both flag sets, on the singleton, on every grid and on the serial driver.
        Before the fix the recording entry listed first zeroed `q` in case `a` too."""
        for record_first in (False, True):
            ir = lst_ir(record_first)
            for flags in ("checked", "release"):
                with self.subTest(record_first=record_first, flags=flags):
                    build = self._build(ir, flags=flags)
                    single = build.run(SINGLETON)
                    self.assertEqual(single.rc, 0, single.stderr[-2000:])
                    self._assert_lst_captures(single.snapshots())
                    self.assertEqual(build.verdict(single), {"t_a": "pass", "t_b": "pass"})
                    self.assertIsNone(self._first_disagreement(build, single.snapshots()))
                    serial = self._build(ir, flags=flags, harness="harness_fortran_cpu")
                    run = serial.run(SINGLETON)
                    self.assertEqual(run.rc, 0, run.stderr[-2000:])
                    self._assert_lst_captures(run.snapshots())

    def _assert_lst_captures(self, snaps: dict[str, Any]) -> None:
        self.assertEqual(snaps["initial/a.json"]["q"], [1.0 + i for i in range(8)])
        self.assertEqual(snaps["a.json"]["w"], [2.0 * (1.0 + i) for i in range(8)])
        self.assertEqual(snaps["initial/b.json"]["q"], [0.0] * 6)
        self.assertEqual(snaps["b.json"]["q"], [3.0] * 6)

    def test_a_wrong_kernel_fails_a_relative_predicate_in_either_entry_order(self) -> None:
        """Issue #613, the certification consequence: case `a`'s predicate judges `final.w`
        against `initial.q`, an IR the gates admit. A model computing `w = 3q` instead of `2q`
        fails it in either entry order, on the serial driver and on `(2, 2)`. Before the fix the
        recording entry listed first zeroed `q` in case `a`, so `initial.q` and `final.w` were
        both zero and the wrong kernel passed."""
        model = (MPI_DATA / "fx_lst_model.f90").read_text()
        self.assertEqual(model.count("w = 2.0_dp * q"), 1)
        wrong = model.replace("w = 2.0_dp * q", "w = 3.0_dp * q")
        for record_first in (False, True):
            ir = lst_ir(record_first)
            for pred in ir["io_contract"]["predicates"]:
                if pred["test_id"] == "t_a" and pred["quantity"] == "w":
                    pred["expr"], pred["op"], pred["value"] = (
                        "maxabs(final.w - 2.0 * initial.q)", "le", 1e-12)
            tests, preds = ir["io_contract"]["tests"], ir["io_contract"]["predicates"]
            cases = {c["case_id"]: c for c in ir["case"]["test_case_set"]}
            self.assertEqual(pe.drive_violations(ir, kind="component"), [])
            self.assertEqual(pe.quantity_violations(tests, preds), [])
            self.assertEqual(list(pe.validate_primary_predicate_schema(
                preds, case_ids=set(cases), test_ids=[t["test_id"] for t in tests],
                schema=pe.snapshot_schema(ir), cases=cases,
                test_target_cases={t["test_id"]: t["target_cases"] for t in tests},
                **pe.predicate_schema_context(ir))), [])
            for harness, grid in (("harness_fortran_cpu", SINGLETON), (MPI_HARNESS, (2, 2))):
                for kernel, text, outcome in (("correct", None, "pass"), ("wrong", wrong, "fail")):
                    with self.subTest(record_first=record_first, harness=harness, kernel=kernel):
                        build = self._build(ir, flags="checked", harness=harness, model=text)
                        run = build.run(grid, timeout=60)
                        self.assertEqual(run.rc, 0, run.stderr[-2000:])
                        # the verdict first: before the fix the reversed wrong kernel PASSED
                        self.assertEqual(build.verdict(run)["t_a"], outcome)
                        self.assertEqual(run.snapshots()["initial/a.json"]["q"],
                                         [1.0 + i for i in range(8)])

    def test_a_state_bound_to_no_dummy_of_its_entry_is_read_from_the_file_on_every_grid(
            self) -> None:
        """Issue #613: `rhs` records `q` over two calls (`q`, then `d`) and counts them in `nc`,
        so in case `a`, whose entry binds no procedure, `d` and `nc` are file-backed and bound to
        no dummy of the entry. `d` is a spatial `cell` array, so the distributed driver
        block-copies it at the extents derived from the schema token `nx` through the entry's
        `nx` dummy. Either entry order, both flag sets, the singleton and every grid."""
        model = (MPI_DATA / "fx_lst_model.f90").read_text()
        one = "    call rhs(nx, z, w)\n"
        self.assertEqual(model.count(one), 1)
        model = model.replace(one, one + "    z = w\n" + one)
        for record_first in (False, True):
            ir = lst_ir(record_first)
            rhs = _lst_entry(ir, "fx_lst__proc")["arguments"]["rhs"]
            rhs["record"], rhs["calls"] = {"q": ["state.q", "state.d"]}, "state.nc"
            ir["io_contract"]["raw_requirements"]["required_evidence"][0]["schema"][
                "variables"] += [{"name": "d", "shape_expr": "[nx]"},
                                 {"name": "nc", "shape_expr": "scalar"}]
            case_a = next(c for c in ir["case"]["test_case_set"] if c["case_id"] == "a")
            case_a["initial_state"].update({"d": [10.0 + i for i in range(8)], "nc": 5.0})
            self.assertEqual(pe.drive_violations(ir, kind="component"), [])
            variables = {v["name"]: v["shape_expr"] for v in pe.snapshot_schema(ir)["variables"]}
            with self.subTest(record_first=record_first, plan=True):
                self.assertEqual(runner_ir.distribute_layouts(ir, variables)[0]["d"],
                                 ((0, "cell"),))
                entry_a = next(e for e in runner_ir.driver_plan(ir, variables).entries
                               if e.cases == ("a",))
                self.assertIn("d", entry_a.from_file)
                self.assertEqual(entry_a.extents["d"], ("h1",))
            for flags in ("checked", "release"):
                build = self._build(ir, flags=flags, model=model)
                reference = None
                for grid in (SINGLETON, *MPI_GRIDS):
                    with self.subTest(record_first=record_first, flags=flags, grid=grid):
                        run = build.run(grid, timeout=60)
                        self.assertEqual(run.rc, 0, run.stderr[-2000:])
                        snaps = run.snapshots()
                        reference = snaps if reference is None else reference
                        self.assertEqual(snaps["initial/a.json"]["d"], [10.0 + i for i in range(8)])
                        self.assertEqual(snaps["a.json"]["d"], [10.0 + i for i in range(8)])
                        self.assertEqual(snaps["initial/a.json"]["nc"], 5.0)
                        self.assertEqual(snaps["initial/b.json"]["d"], [0.0] * 6)
                        self.assertEqual(snaps["b.json"]["d"], [1.5] * 6)
                        self.assertEqual(snaps["b.json"]["nc"], 2.0)
                        self.assertEqual(snaps, reference)
                        self.assertEqual(build.verdict(run), {"t_a": "pass", "t_b": "pass"})

    def test_an_init_procedure_builds_in_either_entry_order(self) -> None:
        """Issue #613: an `init` binding a procedure is planned in each entry, so each entry
        renders its own copy under its own name; before the fix the two copies were both
        `driver_rhs_1` and the runner did not compile. `r` (`init`'s record) is driver-written in
        every case and `q` stays file-backed in case `a`: the serial driver, the singleton and
        three grids, `checked` flags."""
        for record_first in (False, True):
            ir = lst_init_procedure_ir(record_first)
            self.assertEqual(pe.drive_violations(ir, kind="component"), [])
            builds = ((self._build(ir, flags="checked", harness="harness_fortran_cpu"),
                       (SINGLETON,)),
                      (self._build(ir, flags="checked"), (SINGLETON, (2, 1), (4, 1), (2, 2))))
            for build, grids in builds:
                for grid in grids:
                    with self.subTest(record_first=record_first, grid=grid):
                        run = build.run(grid, timeout=60)
                        self.assertEqual(run.rc, 0, run.stderr[-2000:])
                        snaps = run.snapshots()
                        self.assertEqual(snaps["initial/a.json"]["q"], [1.0 + i for i in range(8)])
                        self.assertEqual(snaps["initial/a.json"]["r"], [3.0] * 8)
                        self.assertEqual(snaps["b.json"]["q"], [3.0] * 6)
                        self.assertEqual(set(build.verdict(run).values()), {"pass"})

    def test_seven_procedures_render_a_wrapped_public_statement(self) -> None:
        """Issue #613: the procedure module's `public` statement is continued over lines, so
        seven host-rendered procedures render under the linter's column limit (one line of 108
        columns was refused before), build on the serial driver and pass."""
        ir = many_procedures_ir(7)
        self.assertEqual(pe.drive_violations(ir, kind="component"), [])
        text = f_runner.render_runner(ir, "fx_lst", "harness_fortran_cpu", target=CPU)
        self.assertLess(max(len(line) for line in text.splitlines()), 100)
        build = self._build(ir, flags="checked", harness="harness_fortran_cpu")
        run = build.run(SINGLETON, timeout=60)
        self.assertEqual(run.rc, 0, run.stderr[-2000:])
        self.assertEqual(set(build.verdict(run).values()), {"pass"})
        self.assertEqual(len(build.verdict(run)), 7)

    def test_the_public_statement_wraps_past_88_characters_and_not_at_88(self) -> None:
        """Issue #613 review round 2: the renderer continues the procedure module's `public ::`
        statement once it is longer than 88 characters (the whole statement, not the export
        list). The two probes straddle that width: `n` procedures whose dummy is `width` long
        make a statement of `n * (width + 11) + 8` characters, 88 for two procedures of dummy
        width 29 (one line) and 89 for three of width 16 (continued). A width other than 88
        moves one of them."""
        for n, width, expected in ((2, 29, 88), (3, 16, 89)):
            with self.subTest(statement=expected):
                ir = many_procedures_ir(n)
                dummy = "r" * width
                for sig in ir["public_api"]["signatures"]:
                    sig["signature"]["args"] = [{**arg, "name": dummy} if arg["name"] == "rhs"
                                                else arg for arg in sig["signature"]["args"]]
                for entry in ir["drive"]["step"]:
                    entry["arguments"][dummy] = entry["arguments"].pop("rhs")
                self.assertEqual(pe.drive_violations(ir, kind="component"), [])
                lines = f_runner.render_runner(
                    ir, "fx_lst", "harness_fortran_cpu", target=CPU).splitlines()
                first = next(i for i, line in enumerate(lines)
                             if line.startswith("  public :: driver_"))
                end = first
                while lines[end].endswith(" &"):
                    end += 1
                statement = " ".join(line.strip().removesuffix(" &")
                                     for line in lines[first:end + 1])
                self.assertEqual(len(statement), expected, statement)
                self.assertEqual(end - first + 1, 1 if expected <= 88 else 2,
                                 lines[first:end + 1])

    def test_a_whole_field_mean_divides_by_the_reduced_count_of_the_cells_it_summed(self) -> None:
        """Every cell is held by the same number of ranks on every shape the driver runs (a
        replicated case: all of them; a node with fewer axes than the grid has dimensions: all
        along the other dimension; a distributed one: one), so model (2)'s `__init` taking `dt`
        from the field's mean — `__reduce_sum` of its block's sum over `__reduce_sum_int` of its
        block's cell count — or from a total, that mean times the global count from `derived`,
        writes the singleton's `dt` on every grid, `ny4` replicated on `(1, 4)` and `(1, 3)`
        included, and so does the one-axis variant (axis `x` alone, `block.y.first` the literal
        1). Dividing the reduced sum by the global count instead — the un-normalised sum — writes
        it `p` times too large where `p` ranks hold each cell, and agrees on the singleton and
        on the grids that distribute both axes. The scalar agreement check (B8) is silent on it:
        a reduced value is equal across the ranks."""
        H = MPI_HARNESS
        base = (MPI_DATA / "fx_prob_model.f90").read_text()
        edits = (
            (f"  use {H}_model, only: {H}__exchange_halo_r2\n",
             f"  use {H}_model, only: {H}__exchange_halo_r2, &\n"
             f"    {H}__reduce_sum, {H}__reduce_sum_int\n"),
            # The collectives are reached on every rank: no return before them.
            ("    n_cells = 0\n    if (.not. ok) return\n", "    n_cells = 0\n"))
        for old, _new in edits:
            self.assertEqual(base.count(old), 1, old)
        mean = (f"({H}__reduce_sum(sum(h)) &\n"
                f"      / real({H}__reduce_sum_int(nx * ny), dp))")
        dt_lines = {
            "mean": f"    dt = 1.0e-3_dp * {mean}\n",
            "total": f"    dt = 1.0e-5_dp * real(nx_g * ny_g, dp) * {mean}\n",
            "unnormalised": f"    dt = 1.0e-3_dp * {H}__reduce_sum(sum(h)) &\n"
                            "      / real(nx_g * ny_g, dp)\n"}
        dt_old = "    dt = 0.8_dp / real(nx_g + ny_g, dp)\n"
        self.assertEqual(base.count(dt_old), 1)
        one_axis = prob_ir()
        one_axis["drive"]["distribute"]["axes"] = one_axis["drive"]["distribute"]["axes"][:1]
        for call in ("init", "step"):
            one_axis["drive"][call]["arguments"]["j0"] = 1
        exchange_y = f"    call {H}__exchange_halo_r2(he, 2, 2, 1, .false.)\n"
        self.assertEqual(base.count(exchange_y), 1)
        cases = ["ny4", "ny8"]
        # (variant, axes, the expected dt ratio to the singleton per grid and case).
        unit = {g: (1.0, 1.0) for g in MPI_GRIDS}
        rows = (
            ("mean", 2, unit), ("total", 2, unit), ("mean", 1, unit),
            ("unnormalised", 2, {(1, 4): (4.0, 1.0), (1, 3): (3.0, 1.0), (2, 2): (1.0, 1.0)}),
            ("unnormalised", 1, {(1, 4): (4.0, 4.0), (2, 2): (2.0, 2.0), (4, 1): (1.0, 1.0)}))
        for variant, n_axes, expected in rows:
            with self.subTest(variant=variant, axes=n_axes):
                model = base
                for old, new in (*edits, (dt_old, dt_lines[variant])):
                    model = model.replace(old, new)
                ir = prob_ir() if n_axes == 2 else one_axis
                if n_axes == 1:
                    model = model.replace(exchange_y, "")
                build = self._build(ir, model=model)
                single = build.run(SINGLETON, cases=cases)
                self.assertEqual(single.rc, 0, single.stderr[-1000:])
                ref = single.snapshots()
                for grid, ratios in expected.items():
                    run = build.run(grid, cases=cases, timeout=60)
                    self.assertEqual(run.rc, 0, (grid, run.stderr[-1000:]))
                    got = run.snapshots()
                    for cid, ratio in zip(cases, ratios):
                        dt = got[f"initial/{cid}.json"]["dt"]
                        self.assertAlmostEqual(dt / ref[f"initial/{cid}.json"]["dt"], ratio,
                                               places=12, msg=(grid, cid))
                        if ratio == 1.0:
                            np.testing.assert_allclose(
                                np.asarray(got[f"{cid}.json"]["h"], float),
                                np.asarray(ref[f"{cid}.json"]["h"], float), rtol=1e-12)

    def test_a_committed_nan_on_another_rank_reaches_the_series_on_every_grid(self) -> None:
        """B9: model (2) committing a NaN at the global last cell after its first step, and
        clearing it at the next, writes it into no history sample (`every` 2) and into the
        series' first entry: on the singleton, whose block is the whole field, and on every grid
        of the sweep, where a distributed case holds the cell on the last rank's block and only
        the non-finite flag summed over the ranks carries it to rank 0's entry (`minval` skips a
        NaN). The loader refuses the series on each, as it does the serial driver's."""
        ir = prob_ir()
        model = (MPI_DATA / "fx_prob_model.f90").read_text()
        for old, new in (
                ("  use harness_fortran_cpu_mpi_model,",
                 "  use, intrinsic :: ieee_arithmetic, only: ieee_value, ieee_quiet_nan, "
                 "ieee_is_nan\n  use harness_fortran_cpu_mpi_model,"),
                ("    he = 0.0_dp\n    he(1:nx, 1:ny) = h\n",
                 "    he = 0.0_dp\n    if (ieee_is_nan(h(nx, ny))) h(nx, ny) = 10.0_dp\n"
                 "    he(1:nx, 1:ny) = h\n"),
                ("          + 0.5_dp * he(i, j + 1) - 4.5_dp * he(i, j))\n      end do\n"
                 "    end do\n",
                 "          + 0.5_dp * he(i, j + 1) - 4.5_dp * he(i, j))\n      end do\n"
                 "    end do\n    if (stepno == 0 .and. i0 + nx - 1 == nx_g .and. "
                 "j0 + ny - 1 == ny_g) h(nx, ny) = ieee_value(1.0_dp, ieee_quiet_nan)\n")):
            self.assertEqual(model.count(old), 1, old)
            model = model.replace(old, new)
        build = self._build(ir, model=model)
        for grid in (SINGLETON, *MPI_GRIDS):
            with self.subTest(grid=grid):
                run = build.run(grid, cases=["ny4", "ny8"], timeout=60)
                self.assertEqual(run.rc, 0, run.stderr[-1000:])
                snaps = run.snapshots()
                for cid in ("ny4", "ny8"):
                    series = snaps[f"series/{cid}.json"]
                    for name in ("h_min", "h_max"):
                        column = np.asarray(series[name], float)
                        self.assertTrue(np.isnan(column[0]), (cid, name))
                        self.assertTrue(np.isfinite(column[1:]).all(), (cid, name))
                    for key in (f"initial/{cid}.json", f"{cid}.json",
                                *(f"history/{cid}__{n}.json" for n in (2, 4, 6))):
                        self.assertTrue(np.isfinite(np.asarray(snaps[key]["h"], float)).all(),
                                        key)
                    with self.assertRaises(pe.PrimaryEvidenceError):
                        pe._load_series(run.dir / "raw" / "state_snapshots", cid)

    def test_the_interior_upper_face_is_in_no_record(self) -> None:
        """B10's documented limit: each shared face is taken from the right neighbour's first
        position, so an interior block's upper face is in no record. Model (1) wrong at its LAST
        local face (a one-sided formula there, which the domain's last face takes on the
        singleton too) agrees with the singleton on every grid; the same defect at the FIRST
        local face disagrees. A gather that closed the limit would change this pinned fact."""
        ir = recon_ir()
        model = (MPI_DATA / "fx_recon_model.f90").read_text()
        correct = ("        u_l(i, j) = u_in(i + ng - 1, j + ng) &\n"
                   "          + 0.3_dp * (u_in(i + ng - 1, j + ng) - u_in(i + ng - 2, j + ng))\n")
        self.assertEqual(model.count(correct), 1)
        for face, agrees in (("nx + 1", True), ("1", False)):
            with self.subTest(face=face):
                wrong = correct + f"        if (i == {face}) u_l(i, j) = u_in(i + ng - 1, j + ng)\n"
                build = self._build(ir, model=model.replace(correct, wrong))
                single = build.run(SINGLETON)
                self.assertEqual(single.rc, 0, single.stderr[-1000:])
                found = self._first_disagreement(build, single.snapshots())
                self.assertEqual(found is None, agrees, found)


class CompileCacheTest(unittest.TestCase):
    """`_compile_cached` hands back an earlier compile's outputs only for the same command line
    over the same input bytes. The "compiler" concatenates its inputs into its output and counts
    its runs in the parent directory, so a stale hit shows as the old bytes and a hit as no run."""

    CMD = ["sh", "-c", "cat a.src a.h > a.o; echo x >> ../runs"]

    def _compile(self, root: Path, name: str, text: str, header: str) -> tuple[str, int]:
        cwd = root / name
        cwd.mkdir()
        (cwd / "a.src").write_text(text)
        (cwd / "a.h").write_text(header)
        _compile_cached(self.CMD, cwd, [cwd / "a.src", cwd / "a.h"])
        return (cwd / "a.o").read_text(), len((root / "runs").read_text().split())

    def test_a_changed_input_compiles_and_the_same_input_reuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(self._compile(root, "a", "one\n", "h\n"), ("one\nh\n", 1))
            # an edited source and an edited header each miss and compile
            self.assertEqual(self._compile(root, "b", "two\n", "h\n"), ("two\nh\n", 2))
            self.assertEqual(self._compile(root, "c", "one\n", "g\n"), ("one\ng\n", 3))
            # the first one's bytes again, in a fresh directory: copied in, nothing run
            self.assertEqual(self._compile(root, "d", "one\n", "h\n"), ("one\nh\n", 3))

    def test_the_same_inputs_under_another_command_compile(self) -> None:
        """The MPI harness is one source under the release flags and the `checked` ones: a key
        without the command line would hand a `checked` row the release object."""
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            (cwd / "a.src").write_text("one\n")
            for flag in ("release", "checked"):
                _compile_cached(["sh", "-c", f"cat a.src > a.o; echo {flag} >> a.o"], cwd,
                                [cwd / "a.src"])
                self.assertEqual((cwd / "a.o").read_text(), f"one\n{flag}\n")

    def test_a_failing_compile_raises_and_is_not_cached(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            (cwd / "a.src").write_text("x")
            for _ in range(2):   # the second call runs (and fails) again rather than hitting
                with self.assertRaises(AssertionError):
                    _compile_cached(["sh", "-c", "touch a.o; exit 1"], cwd, [cwd / "a.src"])


def _mpi_runner_text(sid: str) -> str:
    return f_runner.render_runner(MPI_FIXTURES[sid](), sid, MPI_HARNESS, target=_mpi_target())


#: Plan B §5's seeded driver defects: (fixture, label, rendered text, its replacement).
_DRIVER_DEFECTS: tuple[tuple[str, str, str, str], ...] = (
    ("fx_prob", "the result is not reduced over the ranks",
     "      lg_step_ok = nok == nranks\n", ""),
    ("fx_recon", "the face gather leaves out the shared face",
     "        1, 1, dfe1, dlo1, &\n        2, 1, dnl2, dlo2, g_r2)",
     "        1, 1, dnl1, dlo1, &\n        2, 1, dnl2, dlo2, g_r2)"),
    ("fx_loop", "the fold of a written cell array is not reduced",
     "      x_min = harness_fortran_cpu_mpi__reduce_min(minval(arr))\n"
     "      x_max = harness_fortran_cpu_mpi__reduce_max(maxval(arr))\n"
     "    end if\n  end subroutine dfold_r3",
     "      x_min = minval(arr)\n      x_max = maxval(arr)\n"
     "    end if\n  end subroutine dfold_r3"),
    ("fx_loop", "one dummy's local extent is not rewritten",
     "      h3 = dhi2 - dlo2 + 1\n", ""),
    ("fx_loop", "the ghost slice is off by ng",
     "      st_ug = gl_ug(dlo1:dhi1 + 2 * h4)", "      st_ug = gl_ug(dlo1 + h4:dhi1 + 3 * h4)"),
    ("fx_prob", "block.<axis>.first is off by one",
     "    call fx_prob__init( &\n      h1, &\n      h2, &\n      h3, &\n      h4, &\n      dlo1, &",
     "    call fx_prob__init( &\n      h1, &\n      h2, &\n      h3, &\n      h4, &\n"
     "      dlo1 + 1, &"),
    ("fx_wit", "a table row is flipped",
     "    else if (p1 == 2 .and. p2 == 1) then\n      select case (cid)\n      case ('c6')\n"
     "        replicated = .false.",
     "    else if (p1 == 2 .and. p2 == 1) then\n      select case (cid)\n      case ('c6')\n"
     "        replicated = .true."),
    ("fx_loop", "the null axis's offset is wrong in a gather",
     "      call harness_fortran_cpu_mpi__gather_r3(st_u, &\n        2, 1, dnl1, dlo1, &",
     "      call harness_fortran_cpu_mpi__gather_r3(st_u, &\n        1, 1, dnl1, dlo1, &"),
    ("fx_loop", "a derived slice has its axes swapped",
     "h5 = h5g(dlo1:dhi1, dlo2:dhi2)", "h5 = h5g(dlo2:dhi2, dlo1:dhi1)"),
    ("fx_loop", "the record gather is dropped",
     "    if (replicated) then\n      if (rank == 0) out(5) =",
     "    if (.true.) then\n      if (rank == 0) out(5) ="),
)

#: The seeded kernel defects: (fixture, label, the model edits, cases, grids). Each fails a
#: shape of `MPI_GRIDS` and passes the singleton.
_KERNEL_DEFECTS: tuple[tuple[str, str, tuple[tuple[str, str], ...], list[str],
                             tuple[tuple[int, int], ...]], ...] = (
    ("fx_prob", "a problem that copies its own edges instead of exchanging", (
        ("    call harness_fortran_cpu_mpi__exchange_halo_r2(he, 1, 1, 1, .true.)\n"
         "    call harness_fortran_cpu_mpi__exchange_halo_r2(he, 2, 2, 1, .false.)\n",
         "    he(0, :) = he(nx, :)\n    he(nx + 1, :) = he(1, :)\n"),), ["ny8"], ((2, 2),)),
    ("fx_prob", "a refusal before the exchange on one rank (a hang)", (
        ("    he = 0.0_dp\n",
         "    he = 0.0_dp\n    if (stepno == refuse_at .and. i0 + nx - 1 == nx_g .and. "
         "j0 + ny - 1 == ny_g) then\n      ok = .false.\n      return\n    end if\n"),),
     ["ny8r"], ((2, 2),)),
    ("fx_prob", "a wall decided by the grid coordinate", (
        ("  use harness_fortran_cpu_mpi_model, only: harness_fortran_cpu_mpi__exchange_halo_r2\n",
         "  use harness_fortran_cpu_mpi_model, only: harness_fortran_cpu_mpi__exchange_halo_r2, &\n"
         "    harness_fortran_cpu_mpi__grid_coord, harness_fortran_cpu_mpi__grid_dims\n"),
        ("    integer :: i, j\n    he = 0.0_dp\n",
         "    integer :: i, j, c1, c2, p1, p2\n    he = 0.0_dp\n"),
        ("    if (j0 == 1) he(:, 0) = he(:, 1)\n"
         "    if (j0 + ny - 1 == ny_g) he(:, ny + 1) = he(:, ny)\n",
         "    call harness_fortran_cpu_mpi__grid_coord(c1, c2)\n"
         "    call harness_fortran_cpu_mpi__grid_dims(p1, p2)\n"
         "    if (c2 == 0) he(:, 0) = he(:, 1)\n"
         "    if (c2 == p2 - 1) he(:, ny + 1) = he(:, ny)\n")), ["ny4"], ((1, 4),)),
    ("fx_recon", "a component guard against a global count passed in by hand", (
        ("    ok = nx >= 1 .and. ny >= 1", "    ok = nx >= 6 .and. ny >= 1"),),
     ["c6x4"], ((2, 2),)),
)



def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")


def _add_defect_rows() -> None:
    """One test method per fixture and flag set, and per seeded defect, so the suite's workers run
    them side by side: `test_agree_<fixture>_<flags>` runs the fixture on every grid under the
    release flags and under the `checked` row's (issue #589 PR-1b: what the rendered driver reads
    across an `intent(out)` call depends on them)."""
    for sid in MPI_FIXTURES:
        for flags in ("release", "checked"):
            setattr(DistributedDriverEndToEndTest, f"test_agree_{sid}_{flags}",
                    lambda self, a=(sid, flags): self._agree_on_every_grid(*a))
    for sid, label, old, new in _DRIVER_DEFECTS:
        setattr(DistributedDriverEndToEndTest, f"test_driver_defect_{_slug(label)}",
                lambda self, a=(sid, label, old, new): self._driver_defect_fails_a_grid(*a))
    for sid, label, edits, cases, grids in _KERNEL_DEFECTS:
        setattr(DistributedDriverEndToEndTest, f"test_kernel_defect_{_slug(label)}",
                lambda self, a=(sid, label, edits, cases, grids):
                self._kernel_defect_fails_a_grid(*a))


_add_defect_rows()

if __name__ == "__main__":
    unittest.main()
