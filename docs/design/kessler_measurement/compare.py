#!/usr/bin/env python3
"""Runs the original SCALE procedures on the `pass` cases of a node's `tests.md` and reads the result.

`screen`: every `pass` case of §2 goes through the driver, its columns become the state snapshots
the workflow's capture would be, and every judgment of the node's predicate file is evaluated by
`tools.primary_evidence.evaluate_primary_predicates`, the function the host itself runs. The
expanded predicates also go through `validate_primary_predicate_schema` against the real test ids.
`compare`: the same driver columns against the snapshots of a certified run.

An `xfail` case does not run through the driver: the original has no guard, and it aborts or runs
on where the Controlled Spec refuses. Its snapshot is the refusal state the Controlled Spec defines,
written by this script, so the evaluation of its judgment shows only that the judgment is expressible
and evaluable, and says nothing of any kernel. What is checked of it is its forcing, with a nonzero
exit on a miss: a case's inputs must meet a refusal clause of Controlled Spec §4 exactly when the case
is `xfail`, and a non-convergence case must abort in the original at cell 4.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
from tools import primary_evidence as pe  # noqa: E402

NODES = {
    "saturation": ("spec/component/microphysics/saturation/microphysics_saturation_liquid_1d_column",
                   "predicates_saturation.yaml"),
    "kessler": ("spec/component/microphysics/kessler/microphysics_kessler_warm_rain_1d_column",
                "predicates_kessler.yaml"),
}
#: What each operation returns; every other variable keeps its setup value.
RETURNS = {
    "dens2qsat_liq": ["qsat"],
    "saturation_adjustment": ["temp", "qv", "qc", "cptot", "cvtot", "rhoe_d"],
    "adjustment": ["temp", "qv", "qc", "qr", "cptot", "cvtot", "rhoe_t", "evaporate", "pcsat"],
    "terminal_velocity": ["vterm_c", "vterm_r"],
}
#: The intended differences from the original, printed beside the maxima. Each node's §1 states them.
DEVIATIONS = [
    "rank-1 column arrays in place of (KA,IA,JA) and the tracer dimension; constants are arguments "
    "(Controlled Spec §2 of both nodes)",
    "`pcsat` is an output; the original writes QC_t_sat to history (Kessler §1)",
    "a non-converged cell returns a finite refusal state and `converged`; the original aborts "
    "(saturation §4)",
    "the Kessler operation refuses the whole column on non-convergence and restores it (Kessler §4)",
    "the ice argument and the cold-rain branch are absent (saturation §1)",
    "a halo cell of an output is written with 0 (§2 of both nodes)",
    "saturation guards: index range, temp, dens, cptot, cvtot; the original has none (saturation §4)",
    "Kessler guards: index range, dt, negative qv / qc / qr, cptot, cvtot; terminal-velocity guards: "
    "index range, dens, refstate_dens; the original has none (Kessler §4)",
]
FLOOR = 1e-15
#: Driver -> the same sources built with `ALT_CONSTANTS=1`, for the cases with `constants: alt`.
ALT_DRIVER: dict[Path, Path] = {}
REL = 1e-12


def load_suite(tests_md: Path) -> dict:
    """The one fenced YAML block of `tests.md` §2."""
    text = tests_md.read_text(encoding="utf-8")
    section = text.split("## 2.", 1)[1].split("\n## 3.", 1)[0]
    fences = re.findall(r"```yaml\n(.*?)```", section, re.S)
    if len(fences) != 1:
        raise SystemExit(f"{tests_md}: §2 holds {len(fences)} YAML fences, not one")
    return yaml.safe_load(fences[0])


def build_initial(suite: dict, case: dict) -> dict[str, list[float]]:
    """The setup state of §2: rule columns, literal cells, the moist heat capacities, overrides."""
    ka, ks, ke = suite["grid"]["ka"], suite["grid"]["ks"], suite["grid"]["ke"]
    state = {name: [float(value)] * ka for name, value in suite["setup"].items()}
    for name, rule in case.get("columns", {}).items():
        cells = state[name][:]
        for n, k in enumerate(range(ks, ke + 1)):
            if isinstance(rule, dict):
                value = rule["cells"][n]
            else:
                value = rule[0] + rule[1] * n
            cells[k - 1] = float(value)
        state[name] = cells
    columns = case.get("columns", {})
    if "qv" in columns:
        c, air = case_inputs(suite, case)["constants"], suite["air"]
        cvvap = c["cpvap"] - c["rvap"]
        for k in range(ks, ke + 1):
            qv, qc = state["qv"][k - 1], state["qc"][k - 1]
            qr = state["qr"][k - 1] if "qr" in columns else 0.0
            qd = 1.0 - qv - qc - qr
            state["cptot"][k - 1] = qd * air["cpdry"] + c["cpvap"] * qv + c["cl"] * (qc + qr)
            state["cvtot"][k - 1] = (qd * (air["cpdry"] - air["rdry"]) + cvvap * qv
                                     + c["cl"] * (qc + qr))
    for row in case.get("overrides", []):
        state[row["var"]][row["k"] - 1] = float(row["value"])
    return state


def case_inputs(suite: dict, case: dict) -> dict:
    """A case's `inputs` as a Compile leaf would carry them."""
    grid = dict(suite["grid"])
    grid.update(case.get("grid", {}))
    constants = dict(suite["constants"])
    if case.get("constants") == "alt":
        constants.update(suite["alt_constants"])
    inputs = {"grid": grid, "constants": constants}
    if "dt" in case:
        inputs["dt"] = case["dt"]
    return inputs


def run_driver(driver: Path, operation: str, grid: dict, dt: float,
               state: dict[str, list[float]]) -> dict[str, list[float]]:
    lines = [f"{operation} {grid['ka']} {grid['ks']} {grid['ke']} {dt!r}"]
    for name in ("temp", "dens", "pres", "qv", "qc", "qr", "cptot", "cvtot",
                 "rhoq_c", "rhoq_r", "refstate_dens"):
        if name in state:
            lines.append(name + " " + " ".join(repr(v) for v in state[name]))
    done = subprocess.run([str(driver)], input="\n".join(lines) + "\n", text=True,
                          capture_output=True, check=False)
    if done.returncode:
        raise RuntimeError(f"{operation}: driver rc={done.returncode}: "
                           f"{done.stdout[-400:]} {done.stderr[-400:]}")
    out = {}
    for line in done.stdout.splitlines():
        parts = line.split()
        out[parts[0]] = [float(p) for p in parts[1:]]
    return out


def refused(case: dict) -> bool:
    return case["expected"] == "xfail"


def snapshot(suite: dict, case: dict, driver: Path) -> tuple[dict, dict]:
    """(initial, final) of one case, each with the scalars the §5 contract records."""
    initial = build_initial(suite, case)
    flags = [n for n in ("guard_pass", "converged") if n in suite["scalars"]]
    start = {**initial, **{n: suite["scalars"][n] for n in flags}}
    final = {k: list(v) if isinstance(v, list) else v for k, v in start.items()}
    if refused(case):
        final["guard_pass"] = case["refusal"].get("guard_pass", 0.0)
        if "converged" in final:
            final["converged"] = case["refusal"].get("converged", 0.0)
        if case.get("nonconvergence_check") and case["operation"] == "saturation_adjustment":
            # The original aborts on the column, so each other cell runs alone; cell 4 keeps its
            # inputs and the halo of the output is 0, as Controlled Spec section 4 and section 2 state.
            ka = suite["grid"]["ka"]
            final["rhoe_d"] = [0.0] * ka
            for k in range(suite["grid"]["ks"], suite["grid"]["ke"] + 1):
                if k == 4:
                    continue
                one = {"ka": ka, "ks": k, "ke": k}
                returned = run_driver(driver, case["operation"], one, 1.0, initial)
                for name, cells in returned.items():
                    final[name][k - 1] = cells[k - 1]
    else:
        grid = case_inputs(suite, case)["grid"]
        chosen = ALT_DRIVER[driver] if case.get("constants") == "alt" else driver
        returned = run_driver(chosen, case["operation"], grid, case.get("dt", 1.0), initial)
        for name, cells in returned.items():
            final[name] = cells
        final["guard_pass"] = 1.0
        if "converged" in final and case["operation"] == "saturation_adjustment":
            final["converged"] = 1.0
    return start, final


def write_run(run: Path, suite: dict, driver: Path) -> None:
    for case in suite["cases"]:
        start, final = snapshot(suite, case, driver)
        d = run / "raw" / "state_snapshots"
        (d / "initial").mkdir(parents=True, exist_ok=True)
        (d / "initial" / f"{case['case_id']}.json").write_text(json.dumps(start))
        (d / f"{case['case_id']}.json").write_text(json.dumps(final))


def synthetic_ir(suite: dict, predicates: list[dict]) -> dict:
    variables = [{"name": n, "shape_expr": "[ka]"} for n in suite["setup"]]
    variables += [{"name": n, "shape_expr": "scalar"} for n in suite["scalars"]]
    schema = {"variables": variables, "coordinates": [
        {"name": "c", "axis": 0, "count": "inputs.grid.ka", "length": "inputs.grid.ka",
         "placement": "cell_center"}]}
    return {
        "case": {"test_case_set": [{"case_id": c["case_id"], "inputs": case_inputs(suite, c)}
                                   for c in suite["cases"]]},
        "io_contract": {
            "raw_requirements": {"required_evidence": [
                {"artifact": "state_snapshots", "required": True, "schema": schema}]},
            "primary_predicates": predicates,
        },
    }


def load_predicates(here: Path, name: str) -> list[dict]:
    """The predicate file expanded to the shape the Compile gate reads: `target_cases` is the
    test's whole case list, and a row with its own `cases` is one `case:` predicate per case."""
    doc = yaml.safe_load((here / name).read_text(encoding="utf-8"))
    out: list[dict] = []
    for test_id, test in doc["tests"].items():
        targets = test["cases"]
        rows = list(test["judgments"])
        for flag in ("guard", "converged"):
            if flag in test:
                variable = "guard_pass" if flag == "guard" else "converged"
                rows.append({"quantity": variable, "expr": f"final.{variable}", "op": "eq",
                             "value": float(test[flag])})
        for row in rows:
            bind: dict[str, str] = {}
            for name_ in row.get("binds", []):
                bind.update(doc["bind_sets"][name_])
            bind.update(row.get("bind", {}))
            base = {"test_id": test_id, "quantity": row["quantity"], "target_cases": targets,
                    "expr": row["expr"], "op": row["op"], "value": float(row["value"])}
            if bind:
                base["bind"] = bind
            if "cases" in row:
                out.extend({**base, "case": case} for case in row["cases"])
            else:
                out.append({**base, "per_case": True})
        for var in test.get("cross_target", []):
            out.append({
                "test_id": test_id, "quantity": "cross_target_state_agreement",
                "target_cases": targets, "per_case": True,
                "expr": (f"maxabs(final.{var} - comparand.final.{var}) / max(maxabs(final.{var}), "
                         f"maxabs(comparand.final.{var}), 1.0)"),
                "op": "le", "value": 1e-12,
            })
    return out


def gate_violations(tests_md: Path, suite: dict, predicates: list[dict]) -> list[str]:
    """`validate_primary_predicate_schema` against the real test ids of `tests.md`."""
    from tools import validate_pipeline_semantics as vps
    ir = synthetic_ir(suite, predicates)
    targets: dict[str, list[str]] = {}
    for pred in predicates:
        targets[pred["test_id"]] = pred["target_cases"]
    cases = {c["case_id"]: c for c in ir["case"]["test_case_set"]}
    ids = vps._parse_test_ids_from_tests_md(tests_md)
    out = pe.validate_primary_predicate_schema(
        predicates, case_ids=set(cases), test_ids=ids, schema=pe.snapshot_schema(ir), cases=cases,
        test_target_cases=targets)
    out += [f"test {t} has no predicate" for t in ids if t not in targets]
    return out


def cmd_screen(args: argparse.Namespace) -> int:
    here = Path(__file__).resolve().parent
    spec_dir, pred_file = NODES[args.node]
    suite = load_suite(REPO / spec_dir / "tests.md")
    run = Path(args.work) / args.node / "reference"
    other = Path(args.work) / args.node / "comparand"
    ALT_DRIVER[Path(args.driver)] = Path(args.alt_driver)
    ALT_DRIVER[Path(args.comparand_driver)] = Path(args.alt_comparand_driver)
    write_run(run, suite, Path(args.driver))
    write_run(other, suite, Path(args.comparand_driver))
    predicates = load_predicates(here, pred_file)
    ir = synthetic_ir(suite, predicates)
    violations = gate_violations(REPO / spec_dir / "tests.md", suite, predicates)
    for line in violations:
        print(f"GATE {line}")
    comparand = pe.ComparandEvidence("low_optimization", "-", "-", other.resolve(), "-")
    records = pe.evaluate_primary_predicates(ir, run, comparands=[comparand])
    failed = 0
    covered_cases: set[str] = set()
    tests: dict[str, list[bool]] = {}
    for rec in records:
        worst = None
        for row in rec["evaluated"]:
            covered_cases.add(row["case"])
            if "value" in row and (worst is None or abs(row["value"]) > abs(worst)):
                worst = row["value"]
        rhs = rec["evaluated"][0].get("rhs") if rec["evaluated"] else None
        mark = "ok  " if rec["satisfied"] else "FAIL"
        failed += 0 if rec["satisfied"] else 1
        tests.setdefault(rec["test_id"], []).append(rec["satisfied"])
        detail = ""
        if not rec["satisfied"]:
            bad = [r for r in rec["evaluated"] if not r.get("satisfied")]
            detail = " " + json.dumps(bad)[:400]
        print(f"{mark} {rec['test_id']} {rec['quantity']} {rec['op']} {rhs} worst={worst}"
              f" cases={len(rec['evaluated'])}{detail}")
    uncovered = sorted({c["case_id"] for c in suite["cases"]} - covered_cases)
    print(f"predicates {len(records)}, false {failed}; tests {len(tests)}, "
          f"cases {len(suite['cases'])}, cases no predicate reads {uncovered}")
    misses = forcing_misses(suite, Path(args.driver))
    for line in misses:
        print(f"FORCING {line}")
    print(f"forcing misses {len(misses)}")
    print(f"gate violations {len(violations)}")
    return 1 if failed or uncovered or violations or misses else 0


def refusal_clauses(suite: dict, case: dict) -> list[str]:
    """The clauses of Controlled Spec §4 this case's inputs meet."""
    grid = case_inputs(suite, case)["grid"]
    base = suite["grid"]
    state = build_initial(suite, case)
    out = []
    if grid["ks"] < 1:
        out.append("ks < 1")
    if grid["ke"] > grid["ka"]:
        out.append("ke > ka")
    if grid["ks"] > grid["ke"]:
        out.append("ks > ke")
    if case["operation"] == "adjustment" and case["dt"] <= 0:
        out.append("dt <= 0")
    positive = {"dens2qsat_liq": ("temp", "dens"),
                "saturation_adjustment": ("temp", "dens", "cptot", "cvtot"),
                "adjustment": ("temp", "dens", "cptot", "cvtot"),
                "terminal_velocity": ("dens", "refstate_dens")}[case["operation"]]
    signed = ("qv", "qc", "qr") if case["operation"] == "adjustment" else ()
    for k in range(base["ks"] - 1, base["ke"]):
        out += [f"{n}({k + 1}) <= 0" for n in positive if state[n][k] <= 0]
        out += [f"{n}({k + 1}) < 0" for n in signed if state[n][k] < 0]
    return out


def forcing_misses(suite: dict, driver: Path) -> list[str]:
    """Every case whose inputs do not force what its `expected` states."""
    misses = []
    for case in suite["cases"]:
        clauses = refusal_clauses(suite, case)
        if case.get("nonconvergence_check"):
            if clauses:
                misses.append(f"{case['case_id']}: also meets {clauses}")
            try:
                run_driver(driver, case["operation"], suite["grid"], case.get("dt", 1.0),
                           build_initial(suite, case))
                misses.append(f"{case['case_id']}: the original did not abort")
            except RuntimeError as exc:
                if not re.search(r"not converged!\s+4\s", str(exc)):
                    misses.append(f"{case['case_id']}: aborted elsewhere: {str(exc)[:160]}")
        elif refused(case) and not clauses:
            misses.append(f"{case['case_id']}: xfail, and no refusal clause holds")
        elif not refused(case) and clauses:
            misses.append(f"{case['case_id']}: pass, and {clauses} holds")
    return misses


def cmd_compare(args: argparse.Namespace) -> int:
    spec_dir, _ = NODES[args.node]
    suite = load_suite(REPO / spec_dir / "tests.md")
    snaps = Path(args.run_dir) / "raw" / "state_snapshots"
    ALT_DRIVER[Path(args.driver)] = Path(args.alt_driver)
    print("deviations, not banded:")
    for line in DEVIATIONS:
        print(f"  {line}")
    bad = 0
    grid = suite["grid"]
    print("variable case max_rel max_abs")
    for case in suite["cases"]:
        if refused(case):
            continue
        _, ref = snapshot(suite, case, Path(args.driver))
        got = json.loads((snaps / f"{case['case_id']}.json").read_text(encoding="utf-8"))
        for var in RETURNS[case["operation"]]:
            r, g = ref[var], got[var]
            if var == "evaporate":
                # `rNc` amplifies round-off `pcsat`, so the comparison reads what the scheme defines.
                r = [max(-x, 0.0) for x in ref["pcsat"]]
                g = [max(-x, 0.0) for x in got["pcsat"]]
            if var == "rhoe_t":
                # At a near no-op it is round-off in `qv` times `dens * L00 / dt`, so it is read as
                # the mixing ratio it stands for, under the same floor as `pcsat`.
                c = case_inputs(suite, case)["constants"]
                l00 = c["lhv0"] - (c["cpvap"] - c["cl"]) * c["tem00"]
                scale = [case["dt"] / (d * l00) for d in ref["dens"]]
                r = [x * s for x, s in zip(ref[var], scale)]
                g = [x * s for x, s in zip(got[var], scale)]
            max_rel = max_abs = 0.0
            for k in range(grid["ks"] - 1, grid["ke"]):
                diff = abs(r[k] - g[k])
                max_abs = max(max_abs, diff)
                if diff > FLOOR:
                    max_rel = max(max_rel, diff / max(abs(r[k]), abs(g[k])))
            print(f"{var} {case['case_id']} {max_rel:.3e} {max_abs:.3e}")
            bad += 1 if max_rel > REL else 0
    print(f"variables beyond {REL} relative with a {FLOOR} absolute floor: {bad}")
    return 1 if bad else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    screen = sub.add_parser("screen")
    screen.add_argument("node", choices=sorted(NODES))
    screen.add_argument("--driver", required=True)
    screen.add_argument("--comparand-driver", required=True,
                        help="the same sources built at another optimisation level")
    screen.add_argument("--alt-driver", required=True, help="--driver built with ALT_CONSTANTS=1")
    screen.add_argument("--alt-comparand-driver", required=True)
    screen.add_argument("--work", required=True)
    compare = sub.add_parser("compare")
    compare.add_argument("node", choices=sorted(NODES))
    compare.add_argument("--driver", required=True)
    compare.add_argument("--alt-driver", required=True, help="--driver built with ALT_CONSTANTS=1")
    compare.add_argument("--run-dir", required=True)
    args = parser.parse_args()
    return cmd_screen(args) if args.command == "screen" else cmd_compare(args)


if __name__ == "__main__":
    sys.exit(main())
