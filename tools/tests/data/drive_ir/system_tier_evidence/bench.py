#!/usr/bin/env python3
"""System tier PR-S2 bench (unbilled, no LLM): the hand-authored IR of
`problem/shallow_water2d_rotating` (`build_ir.py`), a HAND-WRITTEN composing model
(`models/shallow_water2d_rotating_model.f90`), and the CERTIFIED sources of its closure from the
operator's `workspace/pipelines/` (the harness, the member `shallow_water2d` and its three
components, Coriolis, RK4), each at the version the catalog holds. Run from the repository root:

  PYTHONPATH=. python3 <this file> [--gate | --gate-only] [--mutant <id>]... [--keep <dir>]
      [--flags certified|bounds] [--cases <id>,...] [--show-values]
  PYTHONPATH=. python3 <this file> --all-mutants [--jobs N] [--json <file>]
  (PR-S3 V-a) add [--ir certified] [--model generated] [--mutants <file>]
  (issue #632 P2) add [--at <spec_id>@<version>]... to read a node at another version (see AT)

`--ir certified` judges with the CERTIFIED IR of the node (the `ir_ref` of its latest
`fortran_cpu` pipeline whose run's aggregate verdict is `pass`) in place of the hand-authored one; `--model generated` builds the
certified generated model of that pipeline in place of the hand-written one; `--mutants` names
the patch file (default `mutants.yaml`, written against the hand-written model;
`mutants_generated.yaml` re-expresses the same defects on the generated text). A mutant whose
patch does not apply is reported `NOT APPLICABLE` rather than stopping the table; it, and a
mutant that does not build, is NOT MEASURED, and the run exits 1 when any mutant is.

1. (`--gate`) `validate_compile_ir_dir` with the repository as root: Compile.static's gates,
   including the `quantities` / `expected_outcome` / `history` / `distribute` lines against the
   real `tests.md`.
2. Render the Fortran driver, compile the closure and the model, run every case in ONE process
   (so a cache a mutant keeps across cases is exercised), evaluate every predicate with the host
   evaluator, and print each test's status against `expected_outcome`.

`--mutant <id>` applies the patches of `mutants.yaml` entry `<id>` to the model first; the
`--all-mutants` table prints, per mutant, which tests moved, the predicate records that failed
with their value and threshold, or "not caught"; it exits 1 when a `must_catch` mutant is not
caught, a `declared_unobservable` one is (a surprise to record), or any mutant was not measured,
else 0. "Caught" means some test's status differs from its `expected_outcome`; a run that ends
with a non-zero code is caught the same way and its code is printed beside it. The unmutated
model runs in the same table first; if it moves any test, the run exits 1 and no row is a
measurement. A single `--mutant <id>` run prints the moved tests and exits 0 whatever they are. The default flags
are the certified build's (`-std=f2008 -O2 -fopenmp`): a bounds-check abort is not a catch.

The closure's sources are each node's LATEST `fortran_cpu` attempt at the catalog version under
`workspace/pipelines/` (the lexicographically last attempt directory); the run prints the paths
it used. That they are the certified ones is the operator's workspace state, not checked here,
and they are not read from the composite's lineage (`member_agreement.py` reads the member's run
that way): after a member or component is re-derived on its own, compare the printed paths with
the `lineage.json` `resolved_dependencies` of the composite pipeline (its direct dependencies and
the harness) and of the member pipeline it resolves (the member's components) before reading a
V-a table.
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
from tools.tests.test_driver_render import CPU, second_pass  # noqa: E402

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


#: `--at <spec_id>@<version>`: read that node at that version in place of the catalog's. Issue
#: #632 P2 moved the member to `0.5.2` and this node to `0.1.1` (a restart case each, no scheme
#: change) before any pipeline exists at either, so until they are certified this bench runs with
#: `--at shallow_water2d@0.5.1 --at shallow_water2d_rotating@0.1.0`, and prints what it read.
AT: dict[str, str] = {}


def catalog_version(sid: str) -> str:
    if sid in AT:
        return AT[sid]
    cat = yaml.safe_load((REPO / "spec/registry/spec_catalog.yaml").read_text())["specs"]
    return next(e["spec_version"] for e in cat if e["spec_id"] == sid)


def certified_source(kind: str, sid: str) -> Path:
    version = catalog_version(sid)
    hits = sorted(glob.glob(str(REPO / f"workspace/pipelines/{kind}__{sid}__{version}/fortran_cpu/"
                                f"*/source/src_*/src/{sid}_model.f90")))
    if not hits:
        raise SystemExit(f"no certified fortran_cpu source of {kind}/{sid}@{version} (a node the catalog moved before it was certified there: pass --at <spec_id>@<version> with a certified version)")
    print(f"source {kind}/{sid}@{version}: {Path(hits[-1]).relative_to(REPO)}", flush=True)
    return Path(hits[-1])


def passed_pipeline(node: str) -> Path:
    """The latest `fortran_cpu` pipeline of `node` whose recorded run's aggregate verdict is
    `pass`: a pipeline's `lineage.json` is written before its run, so the latest directory alone
    can be an attempt that failed later."""
    for lineage in reversed(sorted(glob.glob(str(REPO / f"workspace/pipelines/{node}/"
                                                 f"fortran_cpu/*/lineage.json")))):
        pipe = Path(lineage).parent
        run_id = json.loads(Path(lineage).read_text()).get("run_id")
        agg = pipe / "runs" / str(run_id) / node / "aggregate_verdict.json"
        if agg.is_file() and json.loads(agg.read_text()).get("aggregate_verdict") == "pass":
            return pipe
    raise SystemExit(f"no fortran_cpu pipeline of {node} with a passing run (a node the catalog moved before it was certified there: pass --at <spec_id>@<version> with a certified version)")


def certified_pipeline() -> Path:
    version = catalog_version(build_ir.SID)
    return passed_pipeline(f"problem__{build_ir.SID}__{version}")


def certified_ir() -> dict[str, Any]:
    lineage = json.loads((certified_pipeline() / "lineage.json").read_text())
    path = REPO / lineage["ir_ref"] / "spec.ir.yaml"
    print(f"certified IR: {path.relative_to(REPO)}", flush=True)
    return yaml.safe_load(path.read_text())


def generated_model() -> Path:
    pipe = certified_pipeline()
    lineage = json.loads((pipe / "lineage.json").read_text())
    path = pipe / "source" / lineage["source_id"] / "src" / f"{build_ir.SID}_model.f90"
    print(f"generated model: {path.relative_to(REPO)}", flush=True)
    return path


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
    """Pass 1 over the base cases in ONE process, then `Validate.execute`'s second pass (issue
    #632, `test_driver_render.second_pass`): each continuation case in a process of its own. A
    continuation not merged returns a non-zero code naming it; its captures are then absent and
    its tests fail."""
    env = {**os.environ, "OMP_NUM_THREADS": str(threads)}
    ids = sorted(c["case_id"] for c in ir["case"]["test_case_set"]
                 if pe.RESTART_CASE_KEY not in c)
    first = subprocess.run([str(work / "bin"), "--cases", str(work / "ir" / "spec.ir.yaml"), *ids],
                           cwd=work / "run", capture_output=True, text=True, timeout=3000, env=env)
    if first.returncode != 0:
        return first
    _merged, problems = second_pass(ir, work / "run", work / "bin", env)
    if problems:
        return subprocess.CompletedProcess(first.args, 3, first.stdout,
                                           first.stderr + "\n".join(problems))
    return first


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
    """The (predicate, case) pairs found not to hold: its value, operator and threshold, and how far
    outside it lies (`outside`, with `outside_unit`): for one bound of a two-sided band, in
    half-widths of that band; otherwise relative to the threshold (or absolute when the
    threshold is 0). A catch by a small margin is visible either way. A LOWER BOUND on the failed
    pairs: the evaluator stops a per-case record at its first failing case, so a later case of
    the same predicate that would also fail is not listed (and not ranked by `table.py`)."""
    # The thresholds come from the IR's predicates, not from the records: a per-case record
    # need not list every case, so the partner bound of a band is looked up where it is stated.
    rhs: dict[tuple, Any] = {}
    for p in ir["io_contract"]["predicates"]:
        value = p["value"]
        for cid in ([p["case"]] if p.get("case") else p["target_cases"]):
            v = value["per_case"].get(cid) if isinstance(value, dict) else value
            rhs[(p["test_id"], p["quantity"], cid)] = v
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
            if isinstance(v, (int, float)) and isinstance(t, (int, float)):
                q = r["quantity"]
                other = None
                for a, b in ((".lower", ".upper"), (".upper", ".lower")):
                    if q.endswith(a):
                        other = rhs.get((r["test_id"], q[:-len(a)] + b, ev.get("case")))
                if isinstance(other, (int, float)) and other != t:
                    row["outside"], row["outside_unit"] = abs(v - t) / (abs(other - t) / 2), "half-widths"
                elif t:
                    row["outside"], row["outside_unit"] = abs(v - t) / abs(t), "relative"
                else:
                    row["outside"], row["outside_unit"] = abs(v - t), "absolute"
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


def _run_mutant(job: tuple) -> tuple[str, dict[str, Any]]:
    """One mutant in a worker process: build, run, judge; the tests it moved and the records it
    failed."""
    mid, ir, text, flags = job
    if text is None:
        return mid, {"not_applicable": True}
    try:
        verdict, records, note = one(ir, text, flags, None)
    except SystemExit as exc:
        return mid, {"build_error": str(exc)[:600]}
    expected = {t["test_id"]: t["expected_outcome"] for t in ir["io_contract"]["tests"]}
    got = {t["test_id"]: t["status"] for t in verdict["per_test"]}
    moved = sorted(k for k, v in expected.items() if got.get(k) != v)
    return mid, {"moved": moved, "failed": failed_records(ir, records), "note": note}


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
    ap.add_argument("--ir", choices=("hand", "certified"), default="hand")
    ap.add_argument("--model", choices=("hand", "generated"), default="hand")
    ap.add_argument("--mutants", default=str(MUTANTS))
    ap.add_argument("--at", action="append", default=[],
                    help="<spec_id>@<version>: read that node at that version (see AT)")
    a = ap.parse_args()
    for item in a.at:
        sid, _, ver = item.partition("@")
        if not sid or not ver:
            raise SystemExit(f"--at {item!r}: expected <spec_id>@<version>")
        AT[sid] = ver
    if a.ir == "certified":
        ir = certified_ir()
    else:
        ir = yaml.safe_load(build_ir.TRANSCRIPTION.read_text())
        if ir != build_ir.build():
            raise SystemExit(f"{build_ir.TRANSCRIPTION} differs from build_ir.build(): "
                             "run build_ir.py")
    # build_ir's placeholder for a band bands.yaml lacks is +-1e300, which judges nothing
    vacuous = [(p["test_id"], p["quantity"]) for p in ir["io_contract"]["predicates"]
               if "1e+300" in repr(p["value"])]
    if vacuous:
        raise SystemExit(f"bands.yaml lacks the band of {vacuous}: run calibrate.py --write")
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
    base = (generated_model() if a.model == "generated" else MODEL).read_text()
    mutants_path = Path(a.mutants)
    flags = FLAGS[a.flags]
    if not a.all_mutants:
        mutants = yaml.safe_load(mutants_path.read_text()) if a.mutant else {}
        text = base
        for m in a.mutant:
            text = apply_patches(text, mutants[m]["patches"])
        verdict, records, note = one(ir, text, flags, Path(a.keep) if a.keep else None)
        if note:
            print(note)
        moved = report(ir, verdict, records, a.show_values)
        return 0 if a.mutant else (1 if moved or rc else 0)
    mutants = yaml.safe_load(mutants_path.read_text())
    table: dict[str, Any] = {}

    def patched(mid: str) -> str | None:
        if mutants[mid].get("not_applicable"):
            return None
        try:
            return apply_patches(base, mutants[mid]["patches"])
        except SystemExit as exc:
            print(f"   patch of {mid} does not apply: {exc}")
            return None

    # processes, not threads: the judging is Python under the GIL and dominates a run
    # The unmutated model is run in the same table: a catch means a test moved AGAINST a baseline
    # that moved none, so a closure source that is not the certified one (a later failed attempt
    # read as the latest) shows here instead of turning every mutant into a catch.
    baseline_id = "__baseline__"
    baseline_moved: list[str] | None = None
    with cf.ProcessPoolExecutor(a.jobs) as ex:
        jobs = [(baseline_id, ir, base, flags)] + [(mid, ir, patched(mid), flags) for mid in mutants]
        for mid, res in ex.map(_run_mutant, jobs):
            if mid == baseline_id:
                baseline_moved = res.get("moved") if "build_error" not in res else ["(build error)"]
                print(f"== baseline (unmutated model): moved {baseline_moved or 'none'} "
                      f"{res.get('note', '')[:200]}")
                if "build_error" in res:
                    print("     BUILD", res["build_error"][:300])
                continue
            table[mid] = res
            caught = bool(res.get("moved"))
            status = ("NOT APPLICABLE" if res.get("not_applicable")
                      else "NOT MEASURED (build error)" if "build_error" in res
                      else "CAUGHT" if caught else "NOT CAUGHT")
            print(f"== mutant {mid}: {mutants[mid]['title']}  ->  "
                  f"{status} {res.get('note', '')[:200]}")
            for t in res.get("moved", []):
                print("     moved", t)
            for r in res.get("failed", [])[:12]:
                print("     rec", json.dumps(r, default=str)[:300])
            if "build_error" in res:
                print("     BUILD", res["build_error"][:300])
            sys.stdout.flush()
    if a.json:
        Path(a.json).write_text(json.dumps(table, indent=1, default=str))
    # A mutant that was not measured (its patch does not apply, or it does not build) is neither
    # caught nor uncaught: it fails the run whatever its class. A must-catch mutant passes only
    # by being run and caught, and a declared-unobservable one is shown only by being run.
    unmeasured = [m for m in mutants if table[m].get("not_applicable")
                  or "build_error" in table[m]]
    missed = [m for m in mutants if mutants[m]["class"] == "must_catch"
              and m not in unmeasured and not table[m].get("moved")]
    surprised = [m for m in mutants if mutants[m]["class"] != "must_catch"
                 and table[m].get("moved")]
    print(f"not measured (not applicable or build error): {unmeasured or 'none'}")
    print(f"must_catch not caught: {missed or 'none'}; declared unobservable caught: "
          f"{surprised or 'none'}")
    if baseline_moved:
        print(f"BASELINE MOVED {baseline_moved}: no mutant row above is a measurement")
    return 1 if missed or surprised or unmeasured or baseline_moved else 0


if __name__ == "__main__":
    raise SystemExit(main())
