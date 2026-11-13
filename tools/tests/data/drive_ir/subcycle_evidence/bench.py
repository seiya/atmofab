#!/usr/bin/env python3
"""Issue #632 P4 bench (unbilled, no LLM): the hand-authored IR of
`problem/shallow_water2d_channel_subcycled` (`build_ir.py`), a HAND-WRITTEN composing model
(`models/shallow_water2d_channel_subcycled_model.f90`) over the HAND-WRITTEN member model
`../pr3_evidence/models/shallow_water2d_channel_model.f90`, and the CERTIFIED sources of the
member's components and of the harness: those the certified `fortran_cpu` pipeline of
`problem/shallow_water2d_channel@0.3.1` resolved (`../restart_evidence/bench.py` `closure`, every
resolved run checked `pass`). The member at `0.4.1` has no certified `fortran_cpu` pipeline, so
the member model is the hand-written one, as in the restart bench's `chan_hand` subject. Run from
the repository root:

  PYTHONPATH=. python3 <this file> [--gate | --gate-only] [--mutant <id>]... [--keep <dir>]
      [--cases <id>,...] [--show-values]
  PYTHONPATH=. python3 <this file> --all-mutants [--jobs N] [--json <file>]

1. (`--gate`) `validate_compile_ir_dir` with the repository as root: Compile.static's gates,
   including the `quantities` / `expected_outcome` / `history` / `restart` / `distribute` lines
   against the real `tests.md`, and the member's published set read from its spec file.
2. The model gates of `Generate.gate` over the composing model (`model_source_gates` with the
   member as an `__init` publisher, and `validate_dependency_operations`): the placement of the
   member's `__init` among them.
3. Render the Fortran driver, compile the closure, the member and the model, run every base case
   in ONE process (a value a mutant caches across cases is exercised), then each continuation in
   a process of its own (`test_driver_render.second_pass`, the host's second pass), evaluate every
   predicate with the host evaluator, and print each test's status against `expected_outcome`.

`mutants.yaml` declares, per row, its class (`must_catch`, `declared_unobservable`, `control`)
and what is expected to catch it (`by`: test ids, `gate` — the model gates of 2 — or `run` — a
non-zero exit of the run); a `gate` row is run through both the gates and the cases. The table
prints, per row, the gate findings, the tests that moved, the failed predicate records and the
run's exit code. Exit 1 when: a `must_catch` row is caught by nothing; a `control` row or the
unmutated model is caught (the baseline moving makes no row a measurement); a
`declared_unobservable` row is caught (a surprise to record); a row's declared catchers did not
all catch it (recorded as `UNEXPECTED CATCHER`); or a row was not measured. The flags are the
certified build's (`-std=f2008 -O2 -fopenmp`): a bounds-check abort is not a catch.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import importlib.util
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

from tools import primary_evidence as pe  # noqa: E402
from tools.backends.language.fortran import runner as f_runner  # noqa: E402
from tools.backends.language.fortran import source as f_source  # noqa: E402
from tools.tests.test_driver_render import CPU, second_pass  # noqa: E402


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


B = _load("chs_build_ir", HERE / "build_ir.py")
_P1 = _load("system_tier_bench", HERE.parent / "system_tier_evidence" / "bench.py")
_RST = _load("restart_bench", HERE.parent / "restart_evidence" / "bench.py")
apply_patches, failed_records, report, judge = (_P1.apply_patches, _P1.failed_records,
                                                _P1.report, _P1.judge)

FLAGS = ["-std=f2008", "-O2", "-fopenmp"]
MODEL = HERE / "models" / f"{B.SID}_model.f90"
MEMBER_MODEL = HERE.parent / "pr3_evidence" / "models" / f"{B.MEMBER}_model.f90"
MUTANTS = HERE / "mutants.yaml"
THREADS = "4"


def sources() -> list[tuple[str, Path]]:
    """Link order: the harness and the member's components (the certified 0.3.1 member's
    resolved closure, its own model left out), then the hand-written member."""
    closure = [s for s in _RST.closure(_RST.PIPES["chan"]) if not s[0].startswith("problem/")]
    print(f"  member (hand-written): {MEMBER_MODEL.relative_to(REPO)}", flush=True)
    return closure + [(f"problem/{B.MEMBER}", MEMBER_MODEL)]


def build_closure(cache: Path, srcs: list[tuple[str, Path]]) -> list[str]:
    return _RST.build_closure(cache, srcs)


def model_gates(text: str) -> list[str]:
    """`Generate.gate`'s model gates over the composing model, the member an `__init` publisher
    (its §5.1 says so; `_init_publishing_member_ids` reads that, which this bench does not
    re-derive)."""
    found: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"{B.SID}_model.f90"
        path.write_text(text)
        f_source.model_source_gates(node_key=B.NODE_KEY, model_file=path, text=text,
                                    dep_spec_ids=[B.MEMBER], violations=found,
                                    multidim_spec_id=None, init_member_ids=(B.MEMBER,))
        f_source.validate_dependency_operations([path], [B.MEMBER], found)
    return found


def build(work: Path, objs_dir: Path, objs: list[str], ir: dict[str, Any], text: str) -> Path:
    (work / "ir").mkdir(parents=True)
    (work / "ir" / "spec.ir.yaml").write_text(yaml.safe_dump(ir))
    pe.write_case_inputs(ir, work / "ir")
    for sub in ("initial", "history", "series"):
        (work / "run" / "raw" / "state_snapshots" / sub).mkdir(parents=True)
    b = work / "b"
    b.mkdir()
    for o in objs:
        shutil.copy(objs_dir / o, b / o)
    for mod in objs_dir.glob("*.mod"):
        shutil.copy(mod, b / mod.name)
    (b / "model.f90").write_text(text)
    (b / "runner.f90").write_text(f_runner.render_runner(ir, B.SID, "harness_fortran_cpu",
                                                         target=CPU))
    for src, obj in (("model.f90", "model.o"), ("runner.f90", "runner.o")):
        r = subprocess.run(["gfortran", *FLAGS, "-c", src, "-o", obj], cwd=b,
                           capture_output=True, text=True)
        if r.returncode:
            raise SystemExit(f"compile {src}: {r.stderr[-3000:]}")
    subprocess.run(["gfortran", *FLAGS, *objs, "model.o", "runner.o", "-o", "bin"], cwd=b,
                   check=True, capture_output=True)
    return b / "bin"


def execute(work: Path, binary: Path, ir: dict[str, Any]) -> tuple[int, str]:
    env = {**os.environ, "OMP_NUM_THREADS": THREADS}
    ids = sorted(c["case_id"] for c in ir["case"]["test_case_set"]
                 if pe.RESTART_CASE_KEY not in c)
    first = subprocess.run([str(binary), "--cases", str(work / "ir" / "spec.ir.yaml"), *ids],
                           cwd=work / "run", capture_output=True, text=True, timeout=3000,
                           env=env)
    if first.returncode:
        return first.returncode, f"pass 1 rc={first.returncode}: {first.stderr[-300:]}"
    _merged, problems = second_pass(ir, work / "run", binary, env)
    if problems:
        return 3, "second pass: " + "; ".join(problems)[:400]
    return 0, ""


def one(job: tuple) -> tuple[str, dict[str, Any]]:
    mid, ir, text, objs_dir, objs, keep = job
    if text is None:
        return mid, {"not_applicable": True}
    res: dict[str, Any] = {"gate": model_gates(text)}
    work = Path(keep) if keep else Path(tempfile.mkdtemp(prefix="chs_bench_"))
    if keep and work.exists():
        shutil.rmtree(work)
    try:
        try:
            binary = build(work, Path(objs_dir), objs, ir, text)
        except SystemExit as exc:
            res["build_error"] = str(exc)[:600]
            return mid, res
        rc, note = execute(work, binary, ir)
        verdict, records = judge(ir, work)
        expected = {t["test_id"]: t["expected_outcome"] for t in ir["io_contract"]["tests"]}
        got = {t["test_id"]: t["status"] for t in verdict["per_test"]}
        res.update({"rc": rc, "note": note, "verdict": verdict, "records": records,
                    "moved": sorted(k for k, v in expected.items() if got.get(k) != v),
                    "failed": failed_records(ir, records)})
        return mid, res
    finally:
        if not keep:
            shutil.rmtree(work, ignore_errors=True)


def catchers(res: dict[str, Any]) -> set[str]:
    out = set(res.get("moved", []))
    if res.get("gate"):
        out.add("gate")
    if res.get("rc"):
        out.add("run")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gate", action="store_true")
    ap.add_argument("--gate-only", action="store_true")
    ap.add_argument("--mutant", action="append", default=[])
    ap.add_argument("--all-mutants", action="store_true")
    ap.add_argument("--keep")
    ap.add_argument("--cases", help="comma-separated case ids to keep (with the tests on them)")
    ap.add_argument("--show-values", action="store_true")
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--json", help="write the mutant table as JSON here")
    a = ap.parse_args()
    ir = yaml.safe_load(B.TRANSCRIPTION.read_text())
    if ir != B.build():
        raise SystemExit(f"{B.TRANSCRIPTION} differs from build_ir.build(): run build_ir.py")
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
        findings = _P1.gate(ir)
        print(f"GATE findings: {len(findings)}")
        for f in findings:
            print("  ", f[:1200])
        rc = 1 if findings else 0
        if a.gate_only:
            return rc
    base = MODEL.read_text()
    root = Path(tempfile.mkdtemp(prefix="chs_closure_"))
    cache = root / "c"
    try:
        objs = build_closure(cache, sources())
        if not a.all_mutants:
            mutants = yaml.safe_load(MUTANTS.read_text()) if a.mutant else {}
            text = base
            for m in a.mutant:
                text = apply_patches(text, mutants[m]["patches"])
            _mid, res = one(("single", ir, text, str(cache), objs, a.keep))
            if "build_error" in res:
                print("BUILD", res["build_error"])
                return 1
            print(f"MODEL GATES: {len(res['gate'])}")
            for f in res["gate"]:
                print("  ", f[:600])
            if res["note"]:
                print(res["note"])
            moved = report(ir, res["verdict"], res["records"], a.show_values)
            return 0 if a.mutant else (1 if moved or rc or res["gate"] or res["rc"] else 0)
        mutants = yaml.safe_load(MUTANTS.read_text())

        def patched(mid: str) -> str | None:
            try:
                return apply_patches(base, mutants[mid]["patches"])
            except SystemExit as exc:
                print(f"   patch of {mid} does not apply: {exc}")
                return None

        jobs = [("__baseline__", ir, base, str(cache), objs, None)]
        jobs += [(mid, ir, patched(mid), str(cache), objs, None) for mid in mutants]
        table: dict[str, Any] = {}
        with cf.ProcessPoolExecutor(a.jobs) as ex:
            for mid, res in ex.map(one, jobs):
                res.pop("verdict", None)
                res.pop("records", None)
                table[mid] = res
                caught = catchers(res)
                if mid == "__baseline__":
                    print(f"== baseline (unmutated model): caught by {sorted(caught) or 'nothing'}"
                          f" {res.get('note', '')}")
                    for g in res.get("gate", []):
                        print("     gate", g[:300])
                    continue
                row = mutants[mid]
                status = ("NOT APPLICABLE" if res.get("not_applicable")
                          else "NOT MEASURED (build error)" if "build_error" in res
                          else "CAUGHT" if caught else "NOT CAUGHT")
                declared = set(row.get("by", []))
                if status == "CAUGHT" and declared and not declared <= caught:
                    status += f" (UNEXPECTED CATCHER: declared {sorted(declared)})"
                print(f"== {mid} [{row['class']}]: {row['title']}  ->  {status} "
                      f"by {sorted(caught)} {res.get('note', '')[:200]}")
                for g in res.get("gate", [])[:4]:
                    print("     gate", g[:300])
                for r in res.get("failed", [])[:10]:
                    print("     rec", json.dumps(r, default=str)[:300])
                if "build_error" in res:
                    print("     BUILD", res["build_error"][:300])
                sys.stdout.flush()
    finally:
        shutil.rmtree(root, ignore_errors=True)
    if a.json:
        Path(a.json).write_text(json.dumps(table, indent=1, default=str))
    baseline = catchers(table.pop("__baseline__"))
    unmeasured = [m for m in mutants if table[m].get("not_applicable")
                  or "build_error" in table[m]]
    measured = [m for m in mutants if m not in unmeasured]
    missed = [m for m in measured if mutants[m]["class"] == "must_catch"
              and not catchers(table[m])]
    surprised = [m for m in measured if mutants[m]["class"] != "must_catch"
                 and catchers(table[m])]
    wrong = [m for m in measured if mutants[m]["class"] == "must_catch" and catchers(table[m])
             and not set(mutants[m].get("by", [])) <= catchers(table[m])]
    print(f"not measured: {unmeasured or 'none'}")
    print(f"must_catch caught by nothing: {missed or 'none'}")
    print(f"control / declared_unobservable caught: {surprised or 'none'}")
    print(f"a declared catcher did not catch: {wrong or 'none'}")
    if baseline:
        print(f"BASELINE CAUGHT by {sorted(baseline)}: no row above is a measurement")
    return 1 if missed or surprised or unmeasured or wrong or baseline else 0


if __name__ == "__main__":
    raise SystemExit(main())
