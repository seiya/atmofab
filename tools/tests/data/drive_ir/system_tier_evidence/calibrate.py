#!/usr/bin/env python3
"""Calibration of `shallow_water2d_rotating`'s bands against the independent reference
(`reference.py`, issue #265's procedure): every case of the hand IR is solved by the reference,
written in the driver's capture layout (final, initial and history snapshots), and judged by the
SAME host predicates the IR carries. The reference's values set the bands (`bands.yaml`), and
three variants of the reference measure the margins `tests.md` states beside them:

- `swapped`: the rotation before the member (mutant 1), judgment 6's one-off order difference;
- `no_rotation`: the member alone (f dropped), and `no_member`: the rotation alone, judgment 3's
  two phase shifts;
- the closed forms of the interval pair, `k = 1` against `k = 4`.

  PYTHONPATH=. python3 <this file> [--write] [--work <dir>] [--against <bench --keep dir>]

`--against` compares the reference's captures with a bench run's (`bench.py --keep <dir>`): per
case and variable, the largest absolute difference over the largest absolute reference value,
at the final state and at every history sample — the agreement tests.md 5-5 states.

The J numbers below are the plan's judgments; in tests.md §6, J1/J2 are 6-1, J3 6-2, J4 6-3,
J5 6-4 / 6-5, J6 6-6, J7 6-7..6-10, J8 6-11, J9 6-12..6-15.

The case files (initial arrays, `dt`, `n_step`) are the host's own, written by
`primary_evidence.write_case_inputs` from the IR; the reference reads them and nothing else of
the repository. `--write` rewrites `bands.yaml`.
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[4]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))

from tools import primary_evidence as pe  # noqa: E402

import build_ir  # noqa: E402
import reference as R  # noqa: E402

#: J6's half-width, relative to the reference value (tests.md 5-5).
ORDER_REL = 1.0e-6


def solve(work: Path, ir: dict[str, Any], variant: str = "stated") -> Path:
    """Solve every flat case the reference covers; write snapshots under `<work>/<variant>/run`."""
    run = work / variant / "run" / "raw" / "state_snapshots"
    for sub in ("initial", "history", "series"):
        (run / sub).mkdir(parents=True, exist_ok=True)
    cdir = work / "ir" / "case_inputs"
    for case in ir["case"]["test_case_set"]:
        cid = case["case_id"]
        ci = json.loads((cdir / f"{cid}.json").read_text())
        A = lambda k: np.array(ci[k], dtype=float)  # noqa: E731
        h, hu, hv, f, zb = A("h"), A("hu"), A("hv"), A("f"), A("z_b")
        if zb.any() or ci["derived.dt"] <= 0 or ci["inputs.rotation.every"] < 1 \
                or ci["inputs.rotation.on"] not in (0, 1):
            continue  # the cone cases (the member's) and the guard cases: not the reference's
        rotation = ci["inputs.rotation.on"]
        member = True
        order = "stated"
        if variant == "swapped":
            order = "swapped"
        elif variant == "no_rotation":
            rotation = 0
        elif variant == "no_member":
            member = False
        every_h = int(ci["inputs.history.every"])
        U, hist = R.run(h, hu, hv, f, dx=ci["derived.dx"], dy=ci["derived.dy"],
                        dt=ci["derived.dt"], n_step=int(ci["derived.n_step"]), rotation=rotation,
                        every=int(ci["inputs.rotation.every"]), order=order,
                        history_every=every_h, member=member)
        t0, dt, n = ci["inputs.time.t_start"], ci["derived.dt"], int(ci["derived.n_step"])

        def snap(path: Path, state, step: int, t: float, ok: float) -> None:
            doc = {"h": state[0].tolist(), "hu": state[1].tolist(), "hv": state[2].tolist(),
                   "z_b": zb.tolist(), "f": f.tolist(), "ok": ok, "step": float(step), "t": t}
            path.write_text(json.dumps(doc))

        snap(run / "initial" / f"{cid}.json", (h, hu, hv), 0, t0, 0.0)
        snap(run / f"{cid}.json", U, n, t0 + n * dt, 1.0)
        for step, *state in hist:
            snap(run / "history" / f"{cid}__{step}.json", state, step, t0 + step * dt, 1.0)
    return work / variant / "run"


def values(ir: dict[str, Any], run: Path, cases: set[str]) -> dict[tuple[str, str], float]:
    """(quantity, case) -> value, over the predicates whose cases the reference solved."""
    keep = dict(ir)
    keep["io_contract"] = dict(ir["io_contract"])
    # A per-case predicate is split into one predicate per case: the evaluator stops a per-case
    # record at its first failing case, so a variant that fails one case (the `swapped` run)
    # would otherwise leave the later cases unevaluated.
    split: list[dict[str, Any]] = []
    for p in ir["io_contract"]["predicates"]:
        if not set(p["target_cases"]) <= cases or "comparand" in p["expr"] \
                or p["quantity"] in ("run.n_step", "ok"):
            continue   # the closure and the flag are admitted over every target case only
        if not p.get("per_case"):
            split.append(p)
            continue
        for cid in p["target_cases"]:
            q = dict(p)
            q["target_cases"] = [cid]
            if isinstance(p["value"], dict):
                q["value"] = {"per_case": {cid: p["value"]["per_case"][cid]}}
            split.append(q)
    keep["io_contract"]["predicates"] = split
    out: dict[tuple[str, str], float] = {}
    for rec in pe.evaluate_primary_predicates(keep, run):
        rec = dict(rec)
        for ev in rec.get("evaluated") or []:
            if "value" in ev:
                out[(rec["quantity"], ev["case"])] = ev["value"]
            elif ev.get("error"):
                raise SystemExit(f"{rec['quantity']} {ev['case']}: {ev['error']}")
    return out


def out2(x: float, up: bool) -> float:
    """Round outward to two significant digits."""
    if x == 0:
        return 0.0
    e = math.floor(math.log10(abs(x))) - 1
    m = x / 10 ** e
    m = math.ceil(m) if up else math.floor(m)
    return float(f"{m * 10 ** e:.2e}")


def out_rel(x: float, rel: float, up: bool) -> float:
    """x moved outward by rel*|x|, rounded outward at the 7th significant digit."""
    y = x + (rel if up else -rel) * abs(x)
    e = math.floor(math.log10(abs(y))) - 6
    m = y / 10 ** e
    m = math.ceil(m) if up else math.floor(m)
    return float(f"{m * 10 ** e:.6e}")


def closed_form(f0: float, k: int, dt: float, n_step: int, u0: float, v0: float):
    th = f0 * k * dt
    a, b = 1 - th * th / 2 + th ** 4 / 24, -th + th ** 3 / 6
    m = n_step // k
    rho, psi = math.hypot(a, b), math.atan2(b, a)
    return (rho ** m * (u0 * math.cos(m * psi) - v0 * math.sin(m * psi)),
            rho ** m * (u0 * math.sin(m * psi) + v0 * math.cos(m * psi)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--work")
    ap.add_argument("--against")
    a = ap.parse_args()
    work = Path(a.work) if a.work else Path(tempfile.mkdtemp(prefix="swr_cal_"))
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    ir = build_ir.build()
    (work / "ir").mkdir()
    pe.write_case_inputs(ir, work / "ir")
    runs = {v: solve(work, ir, v) for v in ("stated", "swapped", "no_rotation", "no_member")}
    solved = {p.stem for p in (runs["stated"] / "raw" / "state_snapshots").glob("*.json")}
    ref = values(ir, runs["stated"], solved)
    var = {v: values(ir, runs[v], solved) for v in ("swapped", "no_rotation", "no_member")}
    bands: dict[str, Any] = {}
    report: list[str] = []

    # judgment 6: the order quantities, ref +- ORDER_REL, against the swap's difference
    for q in ("order.hu_rms", "order.hu_mean", "order.hv_rms", "order.hv_mean"):
        lo, hi = {}, {}
        for cid in build_ir.ORDER_CASES:
            r = ref[(f"{q}.lower", cid)]
            s = var["swapped"][(f"{q}.lower", cid)]
            lo[cid], hi[cid] = out_rel(r, ORDER_REL, False), out_rel(r, ORDER_REL, True)
            report.append(f"J6 {q:14s} {cid:22s} ref {r: .9e} band [{lo[cid]: .6e}, "
                          f"{hi[cid]: .6e}]  swap {s: .6e}  |swap-ref|/half-width "
                          f"{abs(s - r) / (hi[cid] - r):.3g}")
        bands[q] = {"lower": lo, "upper": hi}

    # judgment 3: the phase error, and the two shifts
    for q in ("wave.phase_error_max", "wave.phase_error_final"):
        r = ref[(q, "swr_pw_n128")]
        report.append(f"J3 {q:22s} ref {r:.4e}  member alone (f dropped) "
                      f"{var['no_rotation'][(q, 'swr_pw_n128')]:.4f}  rotation alone "
                      f"{var['no_member'][(q, 'swr_pw_n128')]:.4f} rad")
    bands["wave.phase_error"] = {"upper": 0.05}

    # judgment 8: the restricted-reference errors (0.9 / 1.1, outward to two digits) and the
    # orders (reference minus 0.3, down to one decimal)
    for (q, cid), r in sorted(ref.items()):
        if q.startswith("errors.restricted.") and q.endswith(".lower"):
            base = q[:-len(".lower")]
            bands[base] = {"lower": out2(0.9 * r, False), "upper": out2(1.1 * r, True)}
            report.append(f"J8 {base:44s} ref {r:.4e} band [{bands[base]['lower']:.2e}, "
                          f"{bands[base]['upper']:.2e}]")
        if q.startswith("convergence.restricted."):
            bound = math.floor((r - 0.3) * 10) / 10
            bands[q] = {"lower": bound if r >= 0.7 else 0.4}
            report.append(f"J8 {q:44s} ref {r:.3f} bound >= {bands[q]['lower']}")

    # judgments 1, 2: the interval pair's closed forms and the mutants' effect on them
    cdir = work / "ir" / "case_inputs"
    for cid in build_ir.ROT_CASES:
        ci = json.loads((cdir / f"{cid}.json").read_text())
        n, dt, f0 = int(ci["derived.n_step"]), ci["derived.dt"], 50.0
        u0, v0 = 0.1, 0.05
        k = int(ci["inputs.rotation.every"])
        e = closed_form(f0, k, dt, n, u0, v0)
        e1 = closed_form(f0, 1, dt, n, u0, v0)
        sub = closed_form(f0, 1, dt, (n // k) * k, u0, v0)  # k RK4 steps of dt (mutant 3)
        dtk = closed_form(f0 / k, k, dt, n, u0, v0)  # dt in place of k*dt (mutant 2)
        w0 = math.hypot(u0, v0)
        d = lambda p, q: max(abs(p[0] - q[0]), abs(p[1] - q[1])) / w0  # noqa: E731
        report.append(f"J2 {cid:22s} n_step {n} k {k}: vs k=1 {d(e, e1):.3e}; k steps of dt "
                      f"{d(e, sub):.3e}; dt for k*dt {d(e, dtk):.3e}; reference value "
                      f"{ref[('rotation.closed_form.final_rel', cid)]:.2e}")
    # the round-off quantities the reference reads (tests.md 5-5)
    for q in ("mass_drift_rel", "mass_drift_rel.history", "rotation.closed_form.final_rel",
              "rotation.closed_form.history_rel"):
        report.append(f"round-off {q:34s} reference max {max(v for (k, _c), v in ref.items() if k == q):.2e}")
    pairs = [v for (k, _c), v in ref.items() if k.startswith(("errors.axis_exchange.",
             "errors.mirror_x.", "errors.mirror_y.", "errors.translation."))]
    report.append(f"round-off {'symmetry residuals':34s} reference max {max(pairs):.2e}")
    if a.against:
        bench = Path(a.against) / "run" / "raw" / "state_snapshots"
        mine = runs["stated"] / "raw" / "state_snapshots"
        worst: dict[str, float] = {}
        for path in sorted(mine.rglob("*.json")):
            rel = path.relative_to(mine)
            if rel.parts[0] == "initial":
                continue
            r_doc, b_doc = json.loads(path.read_text()), json.loads((bench / rel).read_text())
            cid = rel.name.split("__")[0].removesuffix(".json")
            for v in ("h", "hu", "hv"):
                r_arr, b_arr = np.array(r_doc[v]), np.array(b_doc[v])
                d = float(np.max(np.abs(b_arr - r_arr)) / max(np.max(np.abs(r_arr)), 1e-300))
                worst[cid] = max(worst.get(cid, 0.0), d)
        rotating = {c["case_id"] for c in ir["case"]["test_case_set"]
                    if c["inputs"]["rotation"]["on"] == 1}
        for cid, d in sorted(worst.items()):
            report.append(f"agreement {cid:28s} {'rotating' if cid in rotating else 'off':8s} {d:.2e}")
        report.append(f"agreement max over rotating cases {max(d for c, d in worst.items() if c in rotating):.2e}")
    print("\n".join(report))
    if a.write:
        (HERE / "bands.yaml").write_text(
            "# Written by calibrate.py --write from the independent reference (reference.py).\n"
            + yaml.safe_dump(bands, sort_keys=True))
        print("wrote", HERE / "bands.yaml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
