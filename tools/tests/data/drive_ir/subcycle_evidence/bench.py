#!/usr/bin/env python3
"""Issue #632 P4 bench (unbilled, no LLM): the hand-authored IR of
`problem/shallow_water2d_channel_subcycled` (`build_ir.py`), a HAND-WRITTEN composing model
(`models/shallow_water2d_channel_subcycled_model.f90`) over the HAND-WRITTEN member model
`../pr3_evidence/models/shallow_water2d_channel_model.f90`, and the CERTIFIED sources of the
member's components and of the harness: those the certified `fortran_cpu` pipeline of
`problem/shallow_water2d_channel@0.3.1` resolved (`../restart_evidence/bench.py` `closure`, every
resolved run checked `pass`). When P4 ran, the member at `0.4.1` had no certified `fortran_cpu`
pipeline, so the member model is the hand-written one, as in the restart bench's `chan_hand`
subject; that default is kept as the pre-R record.

Issue #632 P6 V-a (after the billed run R of 2026-11-13 certified this node and its member):
`--ir certified` reads the IR of R's composite pipeline (`R_PIPE`, its lineage `ir_ref`) in
place of the hand IR; `--model generated` runs R's GENERATED composite over R's generated member
and R's closure (`R_PIPE`'s `resolved_dependencies`), with `--mutants
tools/tests/data/drive_ir/subcycle_evidence/mutants_generated.yaml` (a path from the working
directory; the rows of `mutants.yaml` re-expressed on the generated text); `--ir certified
--model hand` runs the hand composite and the hand member over R's closure. P6 ran both with
`--gate --all-mutants`: the hand pair exits 0; the generated pair exits 1 on one row only, whose
recorded reason heads `mutants_generated.yaml`. `--ir hand --model generated` was not run. Run
from the repository root:

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
non-zero exit of the run); a `gate` row is run through both the gates and the cases. A test
counts as a catcher only when one of its predicates failed BY VALUE: a test that moved only
because a capture is absent (the run stopped, a continuation had no restart point) is printed
apart and caught nothing (`value_moved`). The table prints, per row, the gate findings, the
tests that moved, the failed predicate records and the run's exit code. Exit 1 when: a `must_catch` row is caught by nothing; a `control` row or the
unmutated model is caught (the baseline moving makes no row a measurement); a
`declared_unobservable` row is caught (a surprise to record); a row's declared catchers did not
all catch it (recorded as `UNEXPECTED CATCHER`); a row was not measured; or `--gate` found a
Compile.static violation. The baseline, a control and a declared_unobservable row count as
caught on ANY movement (a test moved by an absent capture, a non-zero exit, a build error:
`moved_any`); a must_catch row is credited only by value (`catchers`). The flags are the
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


#: issue #632 P6 V-a: the composite's pipeline the billed run R certified (2026-11-13); its
#: lineage resolves the generated member `shallow_water2d_channel@0.4.1` of the same run
R_PIPE = (REPO / "workspace/pipelines/problem__shallow_water2d_channel_subcycled__0.1.0/"
          "fortran_cpu/shallow-water2d-channel-subcycled_20261113_001")


def sources(closure_of: str = "p4", member: str = "hand") -> list[tuple[str, Path]]:
    """Link order: the harness and the member's components, then the member. `closure_of`
    `p4` is the certified 0.3.1 member's resolved closure (P4's bench, before R), `r` the
    closure R's composite pipeline resolved; `member` `hand` replaces the member's model in it
    with the hand-written one, `generated` keeps the one that closure resolved."""
    if closure_of == "p4":
        closure = [s for s in _RST.closure(_RST.PIPES["chan"]) if not s[0].startswith("problem/")]
    else:
        closure = _RST.closure(R_PIPE)
        if member == "hand":
            closure = [s for s in closure if not s[0].startswith("problem/")]
    if member == "hand":
        print(f"  member (hand-written): {MEMBER_MODEL.relative_to(REPO)}", flush=True)
        return closure + [(f"problem/{B.MEMBER}", MEMBER_MODEL)]
    return closure


def certified_ir() -> dict[str, Any]:
    lin = json.loads((R_PIPE / "lineage.json").read_text())
    print(f"  certified IR: {lin['ir_ref']}/spec.ir.yaml", flush=True)
    return yaml.safe_load((REPO / lin["ir_ref"] / "spec.ir.yaml").read_text())


def generated_model() -> Path:
    lin = json.loads((R_PIPE / "lineage.json").read_text())
    path = R_PIPE / "source" / lin["source_id"] / "src" / f"{B.SID}_model.f90"
    print(f"  model (generated): {path.relative_to(REPO)}", flush=True)
    return path


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


def value_moved(res: dict[str, Any]) -> set[str]:
    """The moved tests that a predicate failed BY VALUE: some failed record of the test carries a
    number. A test moved only by records with no value (a capture absent because the run stopped,
    a case with no restart point) did not judge the defect; `run` is what caught it."""
    return {r["test_id"] for r in res.get("failed", [])
            if isinstance(r.get("value"), (int, float)) and r["test_id"] in res.get("moved", [])}


def moved_any(res: dict[str, Any]) -> set[str]:
    """Everything that moved, by value or not: the tests that moved (an absent capture
    included), `gate`, `run` and `build`. A row that must NOT be caught — the baseline, a control,
    a declared_unobservable row — is judged by this, the strict reading; a must_catch row is
    credited only by `catchers` (by value)."""
    out = set(res.get("moved", []))
    if res.get("gate"):
        out.add("gate")
    if res.get("rc"):
        out.add("run")
    if "build_error" in res:
        out.add("build")
    return out


def catchers(res: dict[str, Any]) -> set[str]:
    out = value_moved(res)
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
    ap.add_argument("--ir", choices=("hand", "certified"), default="hand",
                    help="certified: the IR of R's composite pipeline (P6 V-a)")
    ap.add_argument("--model", choices=("hand", "generated"), default="hand",
                    help="generated: R's composite and member models, R's closure (P6 V-a)")
    ap.add_argument("--mutants", default=str(MUTANTS))
    a = ap.parse_args()
    mutants_path = Path(a.mutants)
    if a.ir == "hand":
        ir = yaml.safe_load(B.TRANSCRIPTION.read_text())
        if ir != B.build():
            raise SystemExit(f"{B.TRANSCRIPTION} differs from build_ir.build(): run build_ir.py")
    else:
        ir = certified_ir()
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
    base = (generated_model() if a.model == "generated" else MODEL).read_text()
    root = Path(tempfile.mkdtemp(prefix="chs_closure_"))
    cache = root / "c"
    try:
        # the hand IR with the hand models keeps P4's closure (the pre-R record); every other
        # combination links R's closure, with the hand-written member under the hand composite
        objs = build_closure(cache, sources("p4" if a.ir == "hand" and a.model == "hand"
                                            else "r", a.model))
        if not a.all_mutants:
            mutants = yaml.safe_load(mutants_path.read_text()) if a.mutant else {}
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
            if a.mutant:
                return rc   # a --mutant run reports what moved; only a --gate finding fails it
            return 1 if moved or rc or res["gate"] or res["rc"] else 0
        mutants = yaml.safe_load(mutants_path.read_text())

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
                    caught = moved_any(res)
                    print(f"== baseline (unmutated model): caught by {sorted(caught) or 'nothing'}"
                          f" {res.get('note', '')}")
                    for g in res.get("gate", []):
                        print("     gate", g[:300])
                    continue
                row = mutants[mid]
                if row["class"] != "must_catch":
                    caught = moved_any(res)   # the strict reading the summary uses
                status = ("NOT APPLICABLE" if res.get("not_applicable")
                          else "NOT MEASURED (build error)" if "build_error" in res
                          else "CAUGHT" if caught else "NOT CAUGHT")
                declared = set(row.get("by", []))
                if status == "CAUGHT" and declared and not declared <= caught:
                    status += f" (UNEXPECTED CATCHER: declared {sorted(declared)})"
                absent = sorted(set(res.get("moved", [])) - value_moved(res))
                print(f"== {mid} [{row['class']}]: {row['title']}  ->  {status} "
                      f"by {sorted(caught)} {res.get('note', '')[:200]}")
                if absent:
                    print(f"     moved without a value (absent capture): {absent}")
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
    baseline = moved_any(table.pop("__baseline__"))
    unmeasured = [m for m in mutants if table[m].get("not_applicable")
                  or "build_error" in table[m]]
    measured = [m for m in mutants if m not in unmeasured]
    missed = [m for m in measured if mutants[m]["class"] == "must_catch"
              and not catchers(table[m])]
    surprised = [m for m in measured if mutants[m]["class"] != "must_catch"
                 and moved_any(table[m])]
    wrong = [m for m in measured if mutants[m]["class"] == "must_catch" and catchers(table[m])
             and not set(mutants[m].get("by", [])) <= catchers(table[m])]
    print(f"not measured: {unmeasured or 'none'}")
    print(f"must_catch caught by nothing: {missed or 'none'}")
    print(f"control / declared_unobservable caught: {surprised or 'none'}")
    print(f"a declared catcher did not catch: {wrong or 'none'}")
    if baseline:
        print(f"BASELINE CAUGHT by {sorted(baseline)}: no row above is a measurement")
    if rc:
        print("GATE FINDINGS above: the IR is refused, so no row is a measurement of it")
    return 1 if missed or surprised or unmeasured or wrong or baseline or rc else 0


if __name__ == "__main__":
    raise SystemExit(main())
