#!/usr/bin/env python3
"""Issue #632 (system tier Part 2) P2 bench, unbilled and with no LLM: does the restart test
(`l3_restart_equivalence`, a continuation case `restart: {from: <base>, step: <K>}` judged by
exact equality of its final restart variables with its base's) catch the defects it is declared
to catch, and do the second pass's own checks refuse a broken restart instrument?

Run from the repository root:

  PYTHONPATH=. python3 tools/tests/data/drive_ir/restart_evidence/bench.py [--jobs N]
      [--subjects rot,chan_gen,chan_hand] [--rows <id>,...] [--json <file>] [--all-cases]

Subjects (each over the CERTIFIED sources of its closure, resolved through the lineage of the
pipeline named below — `resolved_dependencies`, recursively for a `problem` member, the source
being `<pipeline_ref>/source/<that pipeline's lineage source_id>/src/<sid>_model.f90`; the run
prints every path and the aggregate verdict each resolved run recorded):

* `rot` — the GENERATED `problem/shallow_water2d_rotating@0.1.0` model of run R-S
  (`workspace/pipelines/problem__shallow_water2d_rotating__0.1.0/fortran_cpu/
  shallow-water2d-rotating_20261112_001`), closure: harness, the member `shallow_water2d@0.5.1`
  (generated) after its three components, Coriolis, RK4. IR: the hand IR
  `../pr3_evidence/ir/shallow_water2d_rotating.yaml` (the generated procedure's argument list is
  that IR's `drive.step` binding set: the pairing builds, and its baseline row moves no test).
  Restart test: base
  `swr_shear_n064_restart` (`history.every` 61, `rotation.every` 2, 183 steps), continuation at
  K = 61.
* `chan_gen` — the GENERATED `problem/shallow_water2d_channel@0.3.1` model
  (`.../problem__shallow_water2d_channel__0.3.1/fortran_cpu/shallow-water2d-channel_20261106_001`)
  under its own certified IR (its lineage `ir_ref`), to which this bench adds in memory the
  continuation `chan_tc4_ref_n032_dts100_cont` (the base's entry with `restart: {from:
  chan_tc4_ref_n032_dts100, step: 272}` and no `initial_state`), and the test
  `l3_restart_equivalence` with its predicates copied from the hand channel IR.
* `chan_hand` — the HAND-WRITTEN 0.4.x model `../pr3_evidence/models/
  shallow_water2d_channel_model.f90` under `../pr3_evidence/ir/shallow_water2d_channel.yaml`
  (which carries the continuation and the test), linked against the closure of the `chan_gen`
  pipeline (the same component versions that IR's `direct_deps` name).

The run of one row is `Validate.execute`'s shape: the host-rendered Fortran driver
(`render_runner`) linked with the closure at the certified flags `-std=f2008 -O2 -fopenmp`;
pass 1 runs the base cases in sorted case-id order in ONE process (`OMP_NUM_THREADS=1`);
the second pass is `tools.tests.test_driver_render.second_pass` (the real continuation writer,
one process per continuation under `run/restart/`, the host start check
`continuation_start_violation`, the conductor's `_merge_continuation_captures`); the judgment is
`evaluate_primary_predicates` + `evaluate_verdict`.

Subset rule (default; `--all-cases` runs every case): pass 1 runs the restart test's base, every
base case whose id sorts before it (pass 1's order is what makes a "cached at the first call of
a process" defect visible, so every case that precedes the base in the suite's process runs),
and the continuation runs in pass 2. Only the tests whose `target_cases` all lie in that set are
judged. Concretely: `rot` runs 23 base cases (`swr_dry_*` .. `swr_shear_n064_restart`; not
`swr_shear_n128/_n256/_shift/_xmir/_ymir`); `chan_gen` / `chan_hand` run every case sorting
before `chan_tc4_ref_n032_dts100` (`chan_dry_*`, `chan_guard_*`, all `chan_tc2_*`, all
`chan_tc3_*`) and the base.

Rows (declared before the bench ran; ids and classes are in the mutant files' headers):

* model mutants (`mutants_<subject>.yaml`, class `must_catch`), the unmutated model
  (`baseline`, must not be caught) and `ctl-dead-counter` (class `control`, must not be caught);
* instrument controls (`controls.yaml`, class `instrument`, each must FAIL the run by the start
  check, a non-zero exit, a missing capture or the restart test): a patch of the rendered runner
  text (`ctl-no-restart-read`, `ctl-t-not-restored`), an edit of the continuation file after the
  real writer wrote it (`ctl-step-zero-file`, `ctl-initial-state-file`; done by wrapping
  `primary_evidence.write_continuation_inputs` in the worker), or the second-pass binary wrapped
  in a script that exits 3 after the binary wrote its captures (`ctl-pass2-nonzero`);
* order controls (`rot` only): the baseline and each row that declares `expect_base_first` /
  `expect_axis_first` (`rot-f-first-call`; `rot-dtrot-once`, base-first only) again with pass 1 running
  ONLY the restart base (`base_first`: the base is the first case of its process, only the
  restart test is judged), where the mutant is declared missed (`expect_base_first: missed`),
  and with pass 1 running `swr_shear_n064_axis` (the same 64 x 64 shape, another f) and then the
  base (`axis_first`, only the restart test judged), where it is expected caught
  (`expect_axis_first: caught`) — the mechanism the declaration names, isolated from the suite's
  order, in which the first firing 64 x 64 case before the base has the base's own f;
* placement control (`rot` only, informational): every `rot` model mutant and the baseline again
  under a variant IR built in memory with the base's (and continuation's) `history.every` 62 and
  K = 62, a MULTIPLE of the call interval `rotation.every` 2, recording caught/missed per row.

A row is CAUGHT when any judged test's status differs from its `expected_outcome`, pass 1 exits
non-zero, or the second pass reports a problem (no restart point, a non-zero exit, a failed start
check, a refused merge). Each row prints which of these caught it — the restart test, other
tests (named), pass 1's exit, the second pass's problem lines — and the failed predicate records
(value / threshold; `system_tier_evidence/bench.py` `failed_records`, a lower bound per
predicate).

Exit 1 when: a `must_catch` row (suite order, K = 61) is not caught; a `control` row or a
baseline is caught (a moved baseline also makes every row of its subject not a measurement); an
`instrument` row is not caught; an order-control row differs from its `expect_*`; or any row is
not measured (a patch that does not apply, a build error, an exception) or did not judge the
restart test. A placement row (K = 62) fails the run only when it is not measured, when its
baseline is caught, or when it did not judge the restart test; whether its mutants are caught is
recorded, not required. Else 0. The
summary also lists every `must_catch` row the RESTART TEST itself did not catch (caught by
another test or not at all): a finding to report, not an exit condition.

Limits: the sources are what the operator's `workspace/` holds (read only); a mutant is the
author's re-expression of the declared defect class on each model's text, and the mutant files
say how each is visible. The order controls run two other orders, not every order. A run at one
thread count says nothing about another.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[4]
sys.path.insert(0, str(REPO))

from tools import primary_evidence as pe  # noqa: E402
from tools import verdict_evaluator  # noqa: E402
from tools.backends.language.fortran import runner as f_runner  # noqa: E402
from tools.tests.test_driver_render import CPU, second_pass  # noqa: E402


def _part1_bench():
    """Part 1's bench module (`system_tier_evidence/bench.py`), loaded by path: this file has
    the same basename."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "system_tier_bench", HERE.parent / "system_tier_evidence" / "bench.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_P1 = _part1_bench()
apply_patches, failed_records = _P1.apply_patches, _P1.failed_records

FLAGS = ["-std=f2008", "-O2", "-fopenmp"]
THREADS = "1"
PR3 = HERE.parent / "pr3_evidence"
PIPES = {
    "rot": REPO / "workspace/pipelines/problem__shallow_water2d_rotating__0.1.0/fortran_cpu/"
                  "shallow-water2d-rotating_20261112_001",
    "chan": REPO / "workspace/pipelines/problem__shallow_water2d_channel__0.3.1/fortran_cpu/"
                   "shallow-water2d-channel_20261106_001",
}
RESTART_TEST = "l3_restart_equivalence"
CHAN_BASE, CHAN_K = "chan_tc4_ref_n032_dts100", 272
ROT_BASE = "swr_shear_n064_restart"


# ---------------------------------------------------------------------------- sources

def _lineage(pipe: Path) -> dict[str, Any]:
    return json.loads((pipe / "lineage.json").read_text())


def _source(pipe: Path) -> Path:
    lin = _lineage(pipe)
    sid = lin["node_key"].split("/", 1)[1].split("@", 1)[0]
    path = pipe / "source" / lin["source_id"] / "src" / f"{sid}_model.f90"
    if not path.is_file():
        raise SystemExit(f"no source {path}")
    return path


def _verdict_of(dep: dict[str, Any]) -> str:
    ref = REPO / dep.get("aggregate_verdict_ref", "")
    return json.loads(ref.read_text()).get("aggregate_verdict") if ref.is_file() else "absent"


def closure(pipe: Path, top: bool = True) -> list[tuple[str, Path]]:
    """Link order: the harness, then each direct dependency after its own closure (a `problem`
    member's components before the member). The pipeline's own run and every resolved run must
    have recorded `pass`."""
    lin = _lineage(pipe)
    harness: list[tuple[str, Path]] = []
    out: list[tuple[str, Path]] = []
    for dep in lin["resolved_dependencies"]:
        dpipe = REPO / dep["pipeline_ref"]
        verdict = _verdict_of(dep)
        dl = _lineage(dpipe)
        note = "" if dl.get("run_id") == dep.get("run_id") else \
            f" (its lineage run_id {dl.get('run_id')} != resolved {dep.get('run_id')})"
        if verdict != "pass":
            raise SystemExit(f"{dep['node_key']}: resolved run verdict {verdict}")
        if dep["node_key"].startswith("infrastructure/"):
            if top:
                harness.append((dep["node_key"], _source(dpipe)))
                print(f"  {dep['node_key']}: {_source(dpipe).relative_to(REPO)} [{verdict}]{note}")
            continue
        if dep["node_key"].startswith("problem/"):
            out += closure(dpipe, top=False)
        out.append((dep["node_key"], _source(dpipe)))
        print(f"  {dep['node_key']}: {_source(dpipe).relative_to(REPO)} [{verdict}]{note}")
    return harness + out


# ---------------------------------------------------------------------------- IRs

def _restart_ids(ir: dict[str, Any]) -> list[str]:
    test = next(t for t in ir["io_contract"]["tests"] if t["test_id"] == RESTART_TEST)
    return list(test["target_cases"])


def chan_gen_ir() -> dict[str, Any]:
    lin = _lineage(PIPES["chan"])
    ir = yaml.safe_load((REPO / lin["ir_ref"] / "spec.ir.yaml").read_text())
    print(f"  certified IR: {lin['ir_ref']}/spec.ir.yaml")
    hand = yaml.safe_load((PR3 / "ir" / "shallow_water2d_channel.yaml").read_text())
    base = next(c for c in ir["case"]["test_case_set"] if c["case_id"] == CHAN_BASE)
    if base["inputs"]["history"]["every"] != CHAN_K:
        raise SystemExit(f"{CHAN_BASE}: history.every is not {CHAN_K}")
    cont = copy.deepcopy(base)
    cont.pop("initial_state", None)
    cont["case_id"] = f"{CHAN_BASE}_cont"
    cont["restart"] = {"from": CHAN_BASE, "step": CHAN_K}
    ir["case"]["test_case_set"].append(cont)
    ir["io_contract"]["tests"].append(copy.deepcopy(
        next(t for t in hand["io_contract"]["tests"] if t["test_id"] == RESTART_TEST)))
    ir["io_contract"]["predicates"] += copy.deepcopy(
        [p for p in hand["io_contract"]["predicates"] if p["test_id"] == RESTART_TEST])
    return ir


def multiple_variant(ir: dict[str, Any], every: int = 62) -> dict[str, Any]:
    """The placement control's IR: the restart base and its continuation with `history.every`
    `every` and K = `every`."""
    ir = copy.deepcopy(ir)
    base, cont = _restart_ids(ir)
    for c in ir["case"]["test_case_set"]:
        if c["case_id"] in (base, cont):
            c["inputs"]["history"]["every"] = every
        if c["case_id"] == cont:
            c["restart"]["step"] = every
    return ir


def subset(ir: dict[str, Any], order: str, all_cases: bool) -> dict[str, Any]:
    ir = copy.deepcopy(ir)
    base, cont = _restart_ids(ir)
    if order == "base_first":
        keep = {base, cont}
    elif order == "axis_first":
        keep = {"swr_shear_n064_axis", base, cont}
    elif all_cases:
        keep = {c["case_id"] for c in ir["case"]["test_case_set"]}
    else:
        keep = {cid for cid in pe.base_case_ids(ir) if cid <= base} | {cont}
    ir["case"]["test_case_set"] = [c for c in ir["case"]["test_case_set"]
                                   if c["case_id"] in keep]
    ir["io_contract"]["tests"] = [t for t in ir["io_contract"]["tests"]
                                  if set(t["target_cases"]) <= keep]
    tids = {t["test_id"] for t in ir["io_contract"]["tests"]}
    ir["io_contract"]["predicates"] = [p for p in ir["io_contract"]["predicates"]
                                       if p["test_id"] in tids]
    return ir


# ---------------------------------------------------------------------------- build / run

def _gfortran(args: list[str], cwd: Path) -> None:
    r = subprocess.run(["gfortran", *FLAGS, *args], cwd=cwd, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"gfortran {' '.join(args)}: {r.stderr[-2000:]}")


def build_closure(cache: Path, sources: list[tuple[str, Path]]) -> list[str]:
    cache.mkdir(parents=True)
    objs = []
    for i, (_key, src) in enumerate(sources):
        shutil.copy(src, cache / f"c{i}.f90")
        _gfortran(["-c", f"c{i}.f90", "-o", f"c{i}.o"], cache)
        objs.append(f"c{i}.o")
    return objs


def _edit_files(kind: str, shape_dir: Path):
    """`write_continuation_inputs` wrapped so the instrument control edits each file the real
    writer wrote."""
    real = pe.write_continuation_inputs

    def wrapped(ir: dict[str, Any], sdir: Path):
        files, refused = real(ir, sdir)
        for f in files:
            doc = json.loads(f.path.read_text())
            if kind == "step_zero":
                doc["restart.step"] = 0
            elif kind == "initial_state":
                init = json.loads((Path(sdir) / "raw" / "state_snapshots" / "initial"
                                   / f"{f.base}.json").read_text())
                for var in pe.restart_variables(ir):
                    doc[f"restart.{var}"] = init[var]
            else:
                raise ValueError(kind)
            f.path.write_text(json.dumps(doc))
        return files, refused
    return real, wrapped


def run_row(job: dict[str, Any]) -> dict[str, Any]:
    t0 = time.time()
    work = Path(tempfile.mkdtemp(prefix=f"rst_{job['subject']}_"))
    try:
        ir = job["ir"]
        for o in job["objs"]:
            shutil.copy(Path(job["cache"]) / o, work / o)
        for mod in Path(job["cache"]).glob("*.mod"):
            shutil.copy(mod, work / mod.name)
        (work / "model.f90").write_text(job["model"])
        runner = f_runner.render_runner(ir, job["sid"], "harness_fortran_cpu", target=CPU)
        if job.get("runner_patches"):
            runner = apply_patches(runner, job["runner_patches"])
        (work / "runner.f90").write_text(runner)
        try:
            _gfortran(["-c", "model.f90", "-o", "model.o"], work)
            _gfortran(["-c", "runner.f90", "-o", "runner.o"], work)
            _gfortran([*job["objs"], "model.o", "runner.o", "-o", "bin"], work)
        except RuntimeError as exc:
            return {"build_error": str(exc)[:800]}
        (work / "ir").mkdir()
        (work / "ir" / "spec.ir.yaml").write_text(yaml.safe_dump(ir))
        pe.write_case_inputs(ir, work / "ir")
        run = work / "run"
        for sub in ("initial", "history", "series"):
            (run / "raw" / "state_snapshots" / sub).mkdir(parents=True)
        env = {**os.environ, "OMP_NUM_THREADS": THREADS}
        ids = pe.base_case_ids(ir)
        p1 = subprocess.run([str(work / "bin"), "--cases", str(work / "ir" / "spec.ir.yaml"),
                             *ids], cwd=run, capture_output=True, text=True, timeout=6000,
                            env=env)
        binary = work / "bin"
        if job.get("wrapper"):
            binary = work / "bin_wrapped"
            binary.write_text(f"#!/bin/sh\n\"{work / 'bin'}\" \"$@\"\nexit 3\n")
            binary.chmod(0o755)
        real = None
        if job.get("file_edit"):
            real, wrapped = _edit_files(job["file_edit"], run)
            pe.write_continuation_inputs = wrapped
        try:
            merged, problems = second_pass(ir, run, binary, env=env)
        finally:
            if real is not None:
                pe.write_continuation_inputs = real
        records = pe.evaluate_primary_predicates(ir, run)
        verdict = verdict_evaluator.evaluate_verdict(ir["io_contract"]["tests"], records)
        expected = {t["test_id"]: t["expected_outcome"] for t in ir["io_contract"]["tests"]}
        got = {t["test_id"]: t["status"] for t in verdict["per_test"]}
        moved = sorted(k for k, v in expected.items() if got.get(k) != v)
        return {"moved": moved, "statuses": {k: got.get(k) for k in moved},
                "failed": failed_records(ir, [dict(r) for r in records]),
                "pass1_rc": p1.returncode,
                "pass1_stderr": p1.stderr[-300:] if p1.returncode else "",
                "merged": merged, "problems": problems, "pass1_cases": len(ids),
                "tests_judged": len(expected), "restart_judged": RESTART_TEST in expected,
                "seconds": round(time.time() - t0, 1)}
    except Exception as exc:  # noqa: BLE001 - a row that raised is not measured
        return {"error": f"{type(exc).__name__}: {exc}"[:800]}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def caught(res: dict[str, Any]) -> bool:
    return bool(res.get("moved") or res.get("pass1_rc") or res.get("problems"))


def unmeasured(res: dict[str, Any]) -> bool:
    return bool(res.get("not_applicable") or res.get("build_error") or res.get("error"))


# ---------------------------------------------------------------------------- main

SUBJECTS = {
    "rot": {"pipe": "rot", "mutants": "mutants_rot.yaml"},
    "chan_gen": {"pipe": "chan", "mutants": "mutants_chan_gen.yaml"},
    "chan_hand": {"pipe": "chan", "mutants": "mutants_chan_hand.yaml"},
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=24)
    ap.add_argument("--subjects", default="rot,chan_gen,chan_hand")
    ap.add_argument("--rows", help="comma-separated row ids to run (baseline always runs)")
    ap.add_argument("--json")
    ap.add_argument("--all-cases", action="store_true")
    a = ap.parse_args()
    t_start = time.time()
    controls = yaml.safe_load((HERE / "controls.yaml").read_text())
    jobs: list[dict[str, Any]] = []
    meta: dict[str, dict[str, Any]] = {}
    cache_root = Path(tempfile.mkdtemp(prefix="rst_cache_"))
    try:
        for subj in a.subjects.split(","):
            spec = SUBJECTS[subj]
            pipe = PIPES[spec["pipe"]]
            print(f"== subject {subj}: pipeline {pipe.relative_to(REPO)}")
            sources = closure(pipe)
            if subj == "rot":
                ir_full = yaml.safe_load((PR3 / "ir" / "shallow_water2d_rotating.yaml").read_text())
                model = _source(pipe)
                print(f"  IR: {(PR3 / 'ir' / 'shallow_water2d_rotating.yaml').relative_to(REPO)}")
            elif subj == "chan_gen":
                ir_full = chan_gen_ir()
                model = _source(pipe)
            else:
                ir_full = yaml.safe_load((PR3 / "ir" / "shallow_water2d_channel.yaml").read_text())
                model = PR3 / "models" / "shallow_water2d_channel_model.f90"
                print(f"  IR: {(PR3 / 'ir' / 'shallow_water2d_channel.yaml').relative_to(REPO)}")
            print(f"  model: {model.relative_to(REPO)}")
            sid = ir_full["meta"]["spec_id"]
            cache = cache_root / subj
            objs = build_closure(cache, sources)
            base_text = model.read_text()
            mutants = yaml.safe_load((HERE / spec["mutants"]).read_text())
            rows: list[tuple[str, dict[str, Any]]] = [
                ("baseline", {"title": "the unmutated model", "class": "control"})]
            rows += list(mutants.items()) + list(controls.items())
            if a.rows:
                want = set(a.rows.split(","))
                rows = [r for r in rows if r[0] == "baseline" or r[0] in want]
            variants = [("suite", "cut")]
            for rid, row in rows:
                kinds = [("suite", "cut")]
                if subj == "rot" and rid == "baseline":
                    kinds += [("base_first", "cut"), ("axis_first", "cut")]
                elif subj == "rot":
                    kinds += [(o, "cut") for o in ("base_first", "axis_first")
                              if f"expect_{o}" in row]
                if subj == "rot" and (rid == "baseline" or row.get("class") == "must_catch"):
                    kinds.append(("suite", "multiple"))
                for order, variant in kinds:
                    ir = multiple_variant(ir_full) if variant == "multiple" else ir_full
                    job = {"subject": subj, "row": rid, "order": order, "variant": variant,
                           "class": row.get("class"), "title": row.get("title", ""),
                           "expect": row.get(f"expect_{order}"),
                           "ir": subset(ir, order, a.all_cases), "sid": sid,
                           "cache": str(cache), "objs": objs, "model": base_text}
                    try:
                        if row.get("patches"):
                            job["model"] = apply_patches(base_text, row["patches"])
                        if row.get("runner_patches"):
                            job["runner_patches"] = row["runner_patches"]
                    except SystemExit as exc:
                        job["not_applicable"] = str(exc)[:400]
                    job["file_edit"] = row.get("file_edit")
                    job["wrapper"] = row.get("wrapper", False)
                    jobs.append(job)
            meta[subj] = {"variants": variants}
        print(f"rows: {len(jobs)}; jobs {a.jobs}; OMP_NUM_THREADS {THREADS}", flush=True)
        results: list[tuple[dict[str, Any], dict[str, Any]]] = []
        with cf.ProcessPoolExecutor(a.jobs) as ex:
            futs = {}
            for job in jobs:
                if job.get("not_applicable"):
                    results.append((job, {"not_applicable": job["not_applicable"]}))
                else:
                    futs[ex.submit(run_row, job)] = job
            for fut in cf.as_completed(futs):
                job = futs[fut]
                res = fut.result()
                results.append((job, res))
                print(f"   done {job['subject']}/{job['row']}/{job['order']}/{job['variant']} "
                      f"{res.get('seconds', '')}s", flush=True)
    finally:
        shutil.rmtree(cache_root, ignore_errors=True)
    return report(results, a.json, time.time() - t_start)


def report(results: list[tuple[dict[str, Any], dict[str, Any]]], json_path: str | None,
           wall: float) -> int:
    order_key = {"suite/cut": 0, "base_first/cut": 1, "axis_first/cut": 2, "suite/multiple": 3}
    results.sort(key=lambda jr: (jr[0]["subject"], order_key[f"{jr[0]['order']}/"
                                                             f"{jr[0]['variant']}"],
                                 jr[0]["row"] != "baseline", jr[0]["row"]))
    failures: list[str] = []
    moved_baseline: set[tuple[str, str, str]] = set()
    restart_missed: list[str] = []
    table = []
    for job, res in results:
        where = f"{job['subject']}/{job['order']}/{job['variant']}"
        is_caught = caught(res)
        if unmeasured(res):
            status = "NOT MEASURED"
            failures.append(f"{where} {job['row']}: not measured")
        else:
            status = "CAUGHT" if is_caught else "NOT CAUGHT"
            if job["variant"] == "multiple":
                if job["row"] == "baseline" and is_caught:
                    moved_baseline.add((job["subject"], job["order"], job["variant"]))
                    failures.append(f"{where} baseline caught")
            elif job["order"] in ("base_first", "axis_first"):
                if job["row"] == "baseline":
                    if is_caught:
                        moved_baseline.add((job["subject"], job["order"], job["variant"]))
                        failures.append(f"{where} baseline caught")
                elif job.get("expect") not in ("missed", "caught") \
                        or (job["expect"] == "missed") == is_caught:
                    failures.append(f"{where} {job['row']}: expected {job.get('expect')}, "
                                    f"{status}")
            elif job["class"] == "must_catch" and not is_caught:
                failures.append(f"{where} {job['row']}: must_catch NOT CAUGHT")
            elif job["class"] == "control" and is_caught:
                if job["row"] == "baseline":
                    moved_baseline.add((job["subject"], job["order"], job["variant"]))
                failures.append(f"{where} {job['row']}: control CAUGHT")
            elif job["class"] == "instrument" and not is_caught:
                failures.append(f"{where} {job['row']}: instrument control NOT CAUGHT")
        by = []
        if (not unmeasured(res) and job["class"] == "must_catch" and job["order"] == "suite"
                and RESTART_TEST not in res.get("moved", [])):
            restart_missed.append(f"{where} {job['row']} ({status})")
        if RESTART_TEST in res.get("moved", []):
            by.append("restart test")
        others = [t for t in res.get("moved", []) if t != RESTART_TEST]
        if others:
            by.append("other tests " + ",".join(others))
        if res.get("pass1_rc"):
            by.append(f"pass-1 rc={res['pass1_rc']}")
        if res.get("problems"):
            by.append("second pass")
        print(f"== [{where}] {job['row']} ({job['class']}) -> {status}"
              f"{' by ' + '; '.join(by) if by else ''}  [{res.get('seconds', '-')}s; pass 1 "
              f"{res.get('pass1_cases', '-')} cases, merged {res.get('merged', '-')}, "
              f"{res.get('tests_judged', '-')} tests judged]")
        if not unmeasured(res) and not res.get("restart_judged"):
            failures.append(f"{where} {job['row']}: the restart test was not judged")
        for line in res.get("problems", []):
            print(f"     second pass: {line[:400]}")
        if res.get("pass1_stderr"):
            print(f"     pass-1 stderr: {res['pass1_stderr'][-200:]}")
        for r in res.get("failed", [])[:10]:
            print("     rec", json.dumps(r, default=str)[:300])
        for k in ("not_applicable", "build_error", "error"):
            if res.get(k):
                print(f"     {k}: {res[k][:600]}")
        table.append({"subject": job["subject"], "order": job["order"],
                      "variant": job["variant"], "row": job["row"], "class": job["class"],
                      "status": status, "by": by,
                      "restart_test": RESTART_TEST in res.get("moved", []), **{k: v for k, v in res.items()}})
    for subj, order, variant in sorted(moved_baseline):
        print(f"BASELINE CAUGHT on {subj}/{order}/{variant}: no row there is a measurement")
    print("must_catch rows the restart test itself did not catch (suite order; `cut` is the "
          "suite's K, `multiple` the placement control's):"
          + ("" if restart_missed else " none"))
    for line in restart_missed:
        print("  ", line)
    print(f"wall: {wall:.0f} s")
    print("exit-1 reasons:" if failures else "exit-1 reasons: none")
    for f in failures:
        print("  ", f)
    if json_path:
        Path(json_path).write_text(json.dumps(table, indent=1, default=str))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
