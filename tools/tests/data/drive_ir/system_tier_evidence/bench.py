#!/usr/bin/env python3
"""System tier PR-S2 bench (unbilled, no LLM): the hand-authored IR of
`problem/shallow_water2d_rotating` (`build_ir.py`), a HAND-WRITTEN composing model
(`models/shallow_water2d_rotating_model.f90`), and the CERTIFIED sources of its closure from the
operator's `workspace/pipelines/` (the harness, the member `shallow_water2d` and its three
components, Coriolis, RK4), each at the version the catalog holds. Run from the repository root:

  PYTHONPATH=. python3 <this file> [--gate] [--mutant <id>]... [--all-mutants] [--keep <dir>]
      [--flags certified|bounds] [--cases <id>,...] [--dump <dir>]

1. (`--gate`) `validate_compile_ir_dir` with the repository as root: Compile.static's gates,
   including the `quantities` / `expected_outcome` / `history` / `distribute` lines against the
   real `tests.md`.
2. Render the Fortran driver, compile the closure and the model, run every case in ONE process
   (so a cache a mutant keeps across cases is exercised), evaluate every predicate with the host
   evaluator, and print each test's status against `expected_outcome`.

`--mutant <id>` applies the patches of `mutants.yaml` entry `<id>` to the model first; the
`--all-mutants` table prints, per mutant, which tests moved, the predicate records that failed
with their value and threshold, or "not caught". The default flags are the certified build's
(`-std=f2008 -O2 -fopenmp`): a bounds-check abort is not a catch.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[4]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))

from tools import primary_evidence as pe  # noqa: E402
from tools import validate_pipeline_semantics as vps  # noqa: E402
from tools import verdict_evaluator  # noqa: E402
from tools.backends.language.fortran import runner as f_runner  # noqa: E402
from tools.tests.test_driver_render import CPU  # noqa: E402

import build_ir  # noqa: E402

FLAGS = {"certified": ["-std=f2008", "-O2", "-fopenmp"],
         "bounds": ["-std=f2008", "-O2", "-fopenmp", "-fcheck=bounds"]}
MODEL = HERE / "models" / f"{build_ir.SID}_model.f90"
MUTANTS = HERE / "mutants.yaml"
#: link order: the harness, the member's components, the member, the composite's components
CLOSURE = (("infrastructure", "harness_fortran_cpu"),
           ("component", "dynamics_shallow_water_boundary_2d_periodic_copy"),
           ("component", "dynamics_shallow_water_flux_2d_rusanov_p0"),
           ("component", "dynamics_shallow_water_time_update_2d_ssprk2"),
           ("problem", "shallow_water2d"),
           ("component", build_ir.CORIOLIS),
           ("component", build_ir.RK4))


def certified_source(kind: str, sid: str) -> Path:
    cat = yaml.safe_load((REPO / "spec/registry/spec_catalog.yaml").read_text())["specs"]
    version = next(e["spec_version"] for e in cat if e["spec_id"] == sid)
    hits = sorted(glob.glob(str(REPO / f"workspace/pipelines/{kind}__{sid}__{version}/fortran_cpu/"
                                f"*/source/src_*/src/{sid}_model.f90")))
    if not hits:
        raise SystemExit(f"no certified fortran_cpu source of {kind}/{sid}@{version}")
    return Path(hits[-1])


def gate(ir: dict[str, Any]) -> list[str]:
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "x_001"
        d.mkdir()
        (d / "spec.ir.yaml").write_text(yaml.safe_dump(ir, sort_keys=False))
        deps = [e["node_key"] for e in ir["dependency"]["direct_deps"]]
        (d / "dependency_graph.json").write_text(json.dumps({
            "node_key": ir["meta"]["node_key"],
            "all_nodes": [{"node_key": k, "topo_level": 0} for k in deps]
            + [{"node_key": ir["meta"]["node_key"], "topo_level": 2}],
            "transitive_deps": [], "direct_deps": deps, "profiles": []}))
        (d / "ir_meta.json").write_text(json.dumps({
            "attempt_count": 1, "verification_status": "pass", "last_fail_reason": None,
            "debug_mode": False, "context_isolated": True}))
        return vps.validate_compile_ir_dir(REPO, d)


def _indent(block: str, n: int) -> str:
    return "".join((" " * n + line) if line.strip() else line
                   for line in block.splitlines(keepends=True))


def apply_patches(text: str, patches: list[dict[str, str]]) -> str:
    """Each patch's `old` must occur exactly once. A YAML block scalar loses the common
    indentation of its lines, so `old` / `new` are re-indented by the one width (0 to 12) at
    which `old` occurs exactly once starting at a line beginning; a fragment without a newline
    is matched as written."""
    for p in patches:
        old, new = p["old"], p["new"]
        if "\n" not in old.rstrip("\n"):
            if text.count(old) != 1:
                raise SystemExit(f"mutation target occurs {text.count(old)} times: {old!r}")
            text = text.replace(old, new)
            continue
        hits = []
        for n in range(13):
            o = _indent(old, n)
            starts = [i for i in range(len(text)) if text.startswith(o, i)
                      and (i == 0 or text[i - 1] == "\n")]
            if len(starts) == 1:
                hits.append((n, starts[0], o))
        if len(hits) != 1:
            raise SystemExit(f"mutation target matches at {len(hits)} indentations: {old!r}")
        n, i, o = hits[0]
        text = text[:i] + _indent(new, n) + text[i + len(o):]
    return text


def build(work: Path, ir: dict[str, Any], model_text: str, flags: list[str]) -> None:
    (work / "ir").mkdir(parents=True)
    (work / "ir" / "spec.ir.yaml").write_text(yaml.safe_dump(ir))
    pe.write_case_inputs(ir, work / "ir")
    for sub in ("initial", "history", "series"):
        (work / "run" / "raw" / "state_snapshots" / sub).mkdir(parents=True)
    (work / "runner.f90").write_text(
        f_runner.render_runner(ir, build_ir.SID, "harness_fortran_cpu", target=CPU))
    objs = []
    texts = [certified_source(k, s).read_text() for k, s in CLOSURE] + [model_text]
    for i, text in enumerate(texts):
        (work / f"s{i}.f90").write_text(text)
        r = subprocess.run(["gfortran", *flags, "-c", f"s{i}.f90", "-o", f"s{i}.o"], cwd=work,
                           capture_output=True, text=True)
        if r.returncode:
            raise SystemExit(f"compile s{i}: {r.stderr[-3000:]}")
        objs.append(f"s{i}.o")
    r = subprocess.run(["gfortran", *flags, "-c", "runner.f90", "-o", "runner.o"], cwd=work,
                       capture_output=True, text=True)
    if r.returncode:
        raise SystemExit("compile runner: " + r.stderr[-3000:])
    subprocess.run(["gfortran", *flags, *objs, "runner.o", "-o", "bin"], cwd=work, check=True,
                   capture_output=True)


def execute(work: Path, ir: dict[str, Any], threads: int = 4) -> subprocess.CompletedProcess:
    ids = sorted(c["case_id"] for c in ir["case"]["test_case_set"])
    return subprocess.run([str(work / "bin"), "--cases", str(work / "ir" / "spec.ir.yaml"), *ids],
                          cwd=work / "run", capture_output=True, text=True, timeout=3000,
                          env={**os.environ, "OMP_NUM_THREADS": str(threads)})


def judge(ir: dict[str, Any], work: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    records = pe.evaluate_primary_predicates(ir, work / "run")
    verdict = verdict_evaluator.evaluate_verdict(ir["io_contract"]["tests"], records)
    return verdict, [dict(r) for r in records]


def one(ir: dict[str, Any], model_text: str, flags: list[str], keep: Path | None
        ) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    work = keep if keep else Path(tempfile.mkdtemp(prefix="swr_bench_"))
    if keep and keep.exists():
        shutil.rmtree(keep)
    try:
        build(work, ir, model_text, flags)
        proc = execute(work, ir)
        note = "" if proc.returncode == 0 else f"run rc={proc.returncode}: {proc.stderr[-400:]}"
        verdict, records = judge(ir, work)
        return verdict, records, note
    finally:
        if not keep:
            shutil.rmtree(work, ignore_errors=True)


def failed_records(ir: dict[str, Any], records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every (predicate, case) that did not hold: its value, operator and threshold, and how
    far outside the threshold it lies (`ratio`: value / threshold for `le`, threshold / value
    for `ge`, so a catch by a factor of 1.01 is visible)."""
    out = []
    for r in records:
        if r.get("satisfied", False):
            continue
        for ev in r.get("evaluated") or [{"case": None, "error": r.get("error")}]:
            if ev.get("satisfied", False):
                continue
            row = {"test_id": r["test_id"], "quantity": r["quantity"], "case": ev.get("case"),
                   "op": r["op"], "value": ev.get("value"), "threshold": ev.get("rhs")}
            if ev.get("error") or ev.get("reason"):
                row["error"] = ev.get("error") or ev.get("reason")
            v, t = row["value"], row["threshold"]
            if isinstance(v, (int, float)) and isinstance(t, (int, float)) and v and t:
                row["ratio"] = abs(v / t) if r["op"] in ("le", "lt") else abs(t / v)
            out.append(row)
    return out


def report(ir: dict[str, Any], verdict: dict[str, Any], records: list[dict[str, Any]],
           show_values: bool) -> dict[str, tuple]:
    expected = {t["test_id"]: t["expected_outcome"] for t in ir["io_contract"]["tests"]}
    got = {t["test_id"]: t["status"] for t in verdict["per_test"]}
    moved = {k: (got.get(k), v) for k, v in expected.items() if got.get(k) != v}
    print(f"TESTS: {len(expected)}; status != expected: {len(moved)}; "
          f"self_verdict {verdict['self_verdict']}")
    for k, v in moved.items():
        print("  MOVED", k, v)
    for r in records:
        if show_values or not r.get("satisfied", False):
            print("  rec", json.dumps({k: v for k, v in r.items() if k not in ("reads", "expr")},
                                      default=str)[:500])
    return moved


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gate", action="store_true")
    ap.add_argument("--gate-only", action="store_true")
    ap.add_argument("--mutant", action="append", default=[])
    ap.add_argument("--all-mutants", action="store_true")
    ap.add_argument("--keep")
    ap.add_argument("--flags", choices=sorted(FLAGS), default="certified")
    ap.add_argument("--cases", help="comma-separated case ids to keep (with the tests on them)")
    ap.add_argument("--show-values", action="store_true")
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--json", help="write the mutant table as JSON here")
    a = ap.parse_args()
    ir = yaml.safe_load(build_ir.TRANSCRIPTION.read_text())
    if ir != build_ir.build():
        raise SystemExit(f"{build_ir.TRANSCRIPTION} differs from build_ir.build(): run build_ir.py")
    if a.cases:
        keep_ids = set(a.cases.split(","))
        ir["case"]["test_case_set"] = [c for c in ir["case"]["test_case_set"]
                                       if c["case_id"] in keep_ids]
        ir["io_contract"]["tests"] = [t for t in ir["io_contract"]["tests"]
                                      if set(t["target_cases"]) <= keep_ids]
        tids = {t["test_id"] for t in ir["io_contract"]["tests"]}
        ir["io_contract"]["predicates"] = [p for p in ir["io_contract"]["predicates"]
                                           if p["test_id"] in tids]
    rc = 0
    if a.gate or a.gate_only:
        findings = gate(ir)
        print(f"GATE findings: {len(findings)}")
        for f in findings:
            print("  ", f[:1200])
        rc = 1 if findings else 0
        if a.gate_only:
            return rc
    base = MODEL.read_text()
    flags = FLAGS[a.flags]
    if not a.all_mutants:
        mutants = yaml.safe_load(MUTANTS.read_text()) if a.mutant else {}
        text = base
        for m in a.mutant:
            text = apply_patches(text, mutants[m]["patches"])
        verdict, records, note = one(ir, text, flags, Path(a.keep) if a.keep else None)
        if note:
            print(note)
        moved = report(ir, verdict, records, a.show_values)
        return 0 if a.mutant else (1 if moved or rc else 0)
    mutants = yaml.safe_load(MUTANTS.read_text())
    table: dict[str, Any] = {}

    def run_mutant(mid: str) -> tuple[str, dict[str, Any]]:
        text = apply_patches(base, mutants[mid]["patches"])
        try:
            verdict, records, note = one(ir, text, flags, None)
        except SystemExit as exc:
            return mid, {"build_error": str(exc)[:600]}
        expected = {t["test_id"]: t["expected_outcome"] for t in ir["io_contract"]["tests"]}
        got = {t["test_id"]: t["status"] for t in verdict["per_test"]}
        moved = sorted(k for k, v in expected.items() if got.get(k) != v)
        return mid, {"moved": moved, "failed": failed_records(ir, records), "note": note}

    with cf.ThreadPoolExecutor(a.jobs) as ex:
        for mid, res in ex.map(run_mutant, list(mutants)):
            table[mid] = res
            caught = bool(res.get("moved"))
            print(f"== mutant {mid}: {mutants[mid]['title']}  ->  "
                  f"{'CAUGHT' if caught else 'NOT CAUGHT'} {res.get('note', '')[:200]}")
            for t in res.get("moved", []):
                print("     moved", t)
            for r in res.get("failed", [])[:12]:
                print("     rec", json.dumps(r, default=str)[:300])
            if "build_error" in res:
                print("     BUILD", res["build_error"][:300])
    if a.json:
        Path(a.json).write_text(json.dumps(table, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
