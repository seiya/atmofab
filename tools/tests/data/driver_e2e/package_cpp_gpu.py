#!/usr/bin/env python3
"""The unbilled `cpp_gpu` end to end of issue #589 PR-1b: the rendered driver over a converted IR
and a certified cuda model, with no LLM. It writes a directory to copy to a GPU site:

    PYTHONPATH=. python3 tools/tests/data/driver_e2e/package_cpp_gpu.py package <out> swe|cor
    (at the site, with nvcc on PATH) bash <out>/go.sh
    PYTHONPATH=. python3 tools/tests/data/driver_e2e/package_cpp_gpu.py judge <out> swe|cor

`judge` reads `<out>/run` back and prints every test whose status is not its `expected_outcome`.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import yaml

from tools import primary_evidence as pe
from tools import verdict_evaluator
from tools.backends.language.cuda_cpp import header, runner
from tools.tests.test_driver_render import DATA, DRIVE_IR, GPU, _harness_public_api

NODES = {
    "swe": (DRIVE_IR / "shallow_water2d_0.4.7" / "spec.ir.yaml", "shallow_water2d", "problem",
            ("dynamics_shallow_water_boundary_2d_periodic_copy",
             "dynamics_shallow_water_flux_2d_rusanov_p0",
             "dynamics_shallow_water_time_update_2d_ssprk2")),
    "cor": (DRIVE_IR / "coriolis_0.2.0.spec.ir.yaml", "dynamics_shallow_water_source_2d_coriolis",
            "component", ()),
}
CUDA = DATA / "cuda"


def package(out: Path, node: str, *, arch: str = "sm_60") -> list[str]:
    """Write `out`; returns the source stems `go.sh` compiles."""
    ir_path, sid, kind, deps = NODES[node]
    ir = yaml.safe_load(ir_path.read_text())
    (out / "ir").mkdir(parents=True)
    (out / "ir" / "spec.ir.yaml").write_text(yaml.safe_dump(ir))
    pe.write_case_inputs(ir, out / "ir")
    (out / f"{sid}_runner.cu").write_text(
        runner.render_runner(ir, sid, "harness_cpp_gpu", target=GPU))
    (out / f"{sid}_model.cuh").write_text(header.render(sid, ir["public_api"], spec_kind=kind))
    (out / "harness_cpp_gpu_model.cuh").write_text(
        header.render("harness_cpp_gpu", _harness_public_api(), spec_kind="infrastructure"))
    shutil.copy(DATA / "harness_cpp_gpu_model.cu", out)
    shutil.copy(CUDA / f"{sid}_model.cu", out)
    for dep in deps:
        shutil.copy(CUDA / f"{dep}_model.cu", out)
        shutil.copy(CUDA / f"{dep}_model.cuh", out)
    stems = sorted(p.stem for p in out.glob("*.cu"))
    ids = " ".join(sorted(c["case_id"] for c in ir["case"]["test_case_set"]))
    (out / "go.sh").write_text(
        'set -e\ncd "$(dirname "$0")"\n'
        + "".join(f"nvcc -std=c++17 -O2 -rdc=true -arch={arch} -I. -c {s}.cu -o {s}.o\n"
                  for s in stems)
        + f"nvcc -rdc=true -arch={arch} *.o -o bin\n"
        + "mkdir -p " + " ".join(f"run/raw/state_snapshots/{d}"
                                 for d in ("initial", "history", "series")) + "\n"
        + f'cd run\nSPEC2SCIENCE_CUDA_BLOCK=128 ../bin --cases "$PWD/../ir/spec.ir.yaml" {ids}\n')
    return stems


def judge(out: Path, node: str) -> int:
    ir = yaml.safe_load(NODES[node][0].read_text())
    verdict = verdict_evaluator.evaluate_verdict(
        ir["io_contract"]["tests"], pe.evaluate_primary_predicates(ir, out / "run"))
    expected = {t["test_id"]: t["expected_outcome"] for t in ir["io_contract"]["tests"]}
    wrong = {t["test_id"]: t["status"] for t in verdict["per_test"]
             if t["status"] != expected[t["test_id"]]}
    print(json.dumps({"self_verdict": verdict["self_verdict"], "wrong": wrong,
                      "perf": json.loads((out / "run" / "perf.json").read_text())}, indent=1))
    return 1 if wrong else 0


if __name__ == "__main__":
    action, where, name = sys.argv[1:4]
    if action == "package":
        package(Path(where), name)
    else:
        raise SystemExit(judge(Path(where), name))
