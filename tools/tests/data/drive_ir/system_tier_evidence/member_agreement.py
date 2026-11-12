#!/usr/bin/env python3
"""System tier PR-S3 V-b (unbilled, no LLM): bitwise agreement between the CERTIFIED composing
model with its process off and its member, read from the two certified runs of R-S.

  PYTHONPATH=. python3 <this file> [--composite-case swr_off_cone_n064]
      [--member-case swe2d_ref_n064_sx000_sy000_dts100]

The composite's off case is stated in its `tests.md` as the member's case input for input
(`shallow_water2d_rotating/tests.md` §6-4). Each node's run is its LATEST `fortran_cpu`
pipeline under `workspace/pipelines/` at the catalog version, and the case inputs are the ones
that pipeline's `lineage.json` names (`ir_ref`).

1. The two host-written case files (`<ir_ref>/case_inputs/<case>.json`) are compared on the
   keys they share; the run stops if any shared key differs. The two IRs were authored
   independently, so their key names need not match: the shared and unshared key counts are
   printed, and the initial captures (step 2) are what makes the check non-vacuous.
2. Every capture of the two cases is compared field by field, as the TEXT the binary wrote
   (`json` with `parse_float=str`), not as a parsed float: the initial state, every history
   capture, the series, and the final state. A field the composite has and the member does not
   (`f`, the process inputs) is listed and not compared. Any difference in a shared field is a
   finding; there is no tolerance.

Exit 0 when every shared field agrees, 1 otherwise.
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[4]


def latest_run(kind: str, sid: str) -> tuple[Path, Path]:
    """(the run's state_snapshots directory, the IR directory its lineage names)."""
    cat = yaml.safe_load((REPO / "spec/registry/spec_catalog.yaml").read_text())["specs"]
    version = next(e["spec_version"] for e in cat if e["spec_id"] == sid)
    node = f"{kind}__{sid}__{version}"
    pipes = sorted(glob.glob(str(REPO / f"workspace/pipelines/{node}/fortran_cpu/*/")))
    if not pipes:
        raise SystemExit(f"no fortran_cpu pipeline of {node}")
    pipe = Path(pipes[-1])
    lineage = json.loads((pipe / "lineage.json").read_text())
    snaps = pipe / "runs" / lineage["run_id"] / node / "raw" / "state_snapshots"
    print(f"{node}: pipeline {pipe.relative_to(REPO)}, run {lineage['run_id']}, "
          f"ir {lineage['ir_ref']}")
    return snaps, REPO / lineage["ir_ref"]


def load_text(path: Path) -> Any:
    return json.loads(path.read_text(), parse_float=str, parse_int=str)


def compare_inputs(c_ir: Path, c_case: str, m_ir: Path, m_case: str) -> list[str]:
    a = json.loads((c_ir / "case_inputs" / f"{c_case}.json").read_text())
    b = json.loads((m_ir / "case_inputs" / f"{m_case}.json").read_text())
    shared = sorted(set(a) & set(b))
    print(f"case inputs: {len(shared)} shared keys; composite only {sorted(set(a) - set(b))}; "
          f"member only {sorted(set(b) - set(a))}")
    return [f"input {k}: composite {a[k]!r:.80} member {b[k]!r:.80}"
            for k in shared if a[k] != b[k]]


def captures(snaps: Path, case: str) -> dict[str, Path]:
    out = {"final": snaps / f"{case}.json", "initial": snaps / "initial" / f"{case}.json",
           "series": snaps / "series" / f"{case}.json"}
    for p in sorted((snaps / "history").glob(f"{case}__*.json")):
        out["history/" + p.stem.split("__")[-1]] = p
    return {k: v for k, v in out.items() if v.is_file()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--composite-case", default="swr_off_cone_n064")
    ap.add_argument("--member-case", default="swe2d_ref_n064_sx000_sy000_dts100")
    a = ap.parse_args()
    c_snaps, c_ir = latest_run("problem", "shallow_water2d_rotating")
    m_snaps, m_ir = latest_run("problem", "shallow_water2d")
    findings = compare_inputs(c_ir, a.composite_case, m_ir, a.member_case)
    if findings:
        for f in findings:
            print("  DIFFERS", f)
        print("case inputs differ: not compared further")
        return 1
    c_cap, m_cap = captures(c_snaps, a.composite_case), captures(m_snaps, a.member_case)
    if set(c_cap) != set(m_cap):
        findings.append(f"capture sets differ: composite {sorted(c_cap)} member {sorted(m_cap)}")
    for name in sorted(set(c_cap) & set(m_cap)):
        x, y = load_text(c_cap[name]), load_text(m_cap[name])
        shared = sorted(set(x) & set(y))
        same = [k for k in shared if x[k] == y[k]]
        diff = [k for k in shared if x[k] != y[k]]
        print(f"{name}: equal {same}; differ {diff}; composite only {sorted(set(x) - set(y))}; "
              f"member only {sorted(set(y) - set(x))}")
        findings += [f"{name}.{k}" for k in diff]
    print("V-b:", "AGREE (bitwise, every shared field of every capture)" if not findings
          else f"DIFFER {findings}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
