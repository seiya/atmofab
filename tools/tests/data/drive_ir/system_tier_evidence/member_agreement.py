#!/usr/bin/env python3
"""System tier PR-S3 V-b (unbilled, no LLM): bitwise agreement between the CERTIFIED composing
model with its process off and its member, read from the two certified runs of R-S.

  PYTHONPATH=. python3 <this file> [--composite-case swr_off_cone_n064]
      [--member-case swe2d_ref_n064_sx000_sy000_dts100] [--at <spec_id>@<version>]...

The composite's off case is stated in its `tests.md` as the member's case input for input
(`shallow_water2d_rotating/tests.md` §6-4). The composite's run is its LATEST `fortran_cpu`
pipeline under `workspace/pipelines/` at the catalog version whose run passed; the member's is the pipeline and
run that pipeline's `lineage.json` RESOLVED (`resolved_dependencies[]`), not the member's
latest. Each side's case inputs are the ones its own lineage names (`ir_ref`).

1. The two host-written case files (`<ir_ref>/case_inputs/<case>.json`) are compared on the
   keys they share; the run stops if any shared key differs. The two IRs were authored
   independently, so their key names need not match: the shared and unshared key counts are
   printed, and the initial captures (step 2) are what makes the check non-vacuous.
2. Every capture of the two cases is compared field by field, as the TEXT the binary wrote
   (`json` with `parse_float=str`), not as a parsed float: the initial state, every history
   capture, the series, and the final state. A field the composite has and the member does not
   (`f`, the process inputs) is listed and not compared. Each of the following is a finding;
   there is no tolerance: a shared field that differs; a field the member has and the composite
   lacks; a state capture (all but the series) in which `h`, `hu`, `hv` or `z_b` is not shared;
   a side lacking `initial`, `final`, `series` or every `history/<step>`; the two sides holding
   different sets of captures.

Issue #632 P6 V-b reuses this script for `problem/shallow_water2d_channel_subcycled` over its
member `problem/shallow_water2d_channel` (`--composite`, `--member`, `--state-fields`, and one
`--pair <composite case>=<member case>` per pair, each compared as above):

  PYTHONPATH=. python3 <this file> --composite shallow_water2d_channel_subcycled
      --member shallow_water2d_channel --state-fields h,hu,hv
      --pair chs_tc2_ref_n032_dts100_m1=chan_tc2_ref_n032_dts100 ...

Exit 0 when nothing is found, 1 otherwise.
"""
from __future__ import annotations

import argparse
import glob
import itertools
import json
import sys
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[4]
REQUIRED_CAPTURES = ("initial", "final", "series")
STATE_FIELDS: tuple[str, ...] = ("h", "hu", "hv", "z_b")


#: `--at <spec_id>@<version>`: read that node at that version in place of the catalog's (issue
#: #632 P2 moved both nodes before either was certified there; `bench.py`'s `AT` says the same).
AT: dict[str, str] = {}


def node_dir(kind: str, sid: str) -> str:
    version = AT.get(sid)
    if version is None:
        cat = yaml.safe_load((REPO / "spec/registry/spec_catalog.yaml").read_text())["specs"]
        version = next(e["spec_version"] for e in cat if e["spec_id"] == sid)
    return f"{kind}__{sid}__{version}"


def latest_pipeline(node: str) -> Path:
    """The latest `fortran_cpu` pipeline of `node` whose recorded run's aggregate verdict is
    `pass` (a pipeline's `lineage.json` is written before its run)."""
    for pipe in reversed(sorted(glob.glob(str(REPO / f"workspace/pipelines/{node}/fortran_cpu/*/")))):
        pipe = Path(pipe)
        lineage = pipe / "lineage.json"
        if not lineage.is_file():
            continue
        run_id = json.loads(lineage.read_text()).get("run_id")
        agg = pipe / "runs" / str(run_id) / node / "aggregate_verdict.json"
        if agg.is_file() and json.loads(agg.read_text()).get("aggregate_verdict") == "pass":
            return pipe
    raise SystemExit(f"no fortran_cpu pipeline of {node} with a passing run (a node the catalog moved before it was certified there: pass --at <spec_id>@<version> with a certified version)")


def run_of(pipe: Path, node: str, run_id: str | None = None) -> tuple[Path, Path]:
    """(the run's state_snapshots directory, the IR directory the pipeline's lineage names). The
    run is `run_id` when given (the run a consumer resolved, which a later run of the same
    pipeline may have followed), else the lineage's own run; it must exist."""
    lineage = json.loads((pipe / "lineage.json").read_text())
    run = run_id if run_id is not None else lineage["run_id"]
    run_dir = pipe / "runs" / str(run)
    if not run_dir.is_dir():
        raise SystemExit(f"{node}: run {run} is absent under {pipe.relative_to(REPO)}")
    snaps = run_dir / node / "raw" / "state_snapshots"
    print(f"{node}: pipeline {pipe.relative_to(REPO)}, run {run}, ir {lineage['ir_ref']}")
    return snaps, REPO / lineage["ir_ref"]


def load_text(path: Path) -> Any:
    return json.loads(path.read_text(), parse_float=str, parse_int=str)


def compare_inputs(c_ir: Path, c_case: str, m_ir: Path, m_case: str) -> list[str]:
    a = json.loads((c_ir / "case_inputs" / f"{c_case}.json").read_text())
    b = json.loads((m_ir / "case_inputs" / f"{m_case}.json").read_text())
    shared = sorted(set(a) & set(b))
    print(f"case inputs: {len(shared)} shared keys; composite only {sorted(set(a) - set(b))}; "
          f"member only {sorted(set(b) - set(a))}")
    out = []
    for k in shared:
        if a[k] == b[k]:
            continue
        if _broadcast_equal(a[k], b[k]):
            print(f"  input {k}: equal values, other shape (a reference one IR keeps as a "
                  f"profile and the other as a field); not a difference")
            continue
        out.append(f"input {k}: composite {a[k]!r:.80} member {b[k]!r:.80}")
    return out


def _shape(v: Any) -> tuple[int, ...] | None:
    """The array's shape, or None when it is ragged (rows of different shapes)."""
    if not (isinstance(v, list) and v):
        return ()
    inner = [_shape(x) for x in v]
    if any(i is None or i != inner[0] for i in inner):
        return None
    return (len(v), *inner[0])


def _broadcast_equal(x: Any, y: Any) -> bool:
    """`x` and `y` hold the same numbers once the smaller is broadcast to the larger's shape
    (a scalar, or a `[1, n]` row, against an `[m, n]` array), compared exactly."""
    def flat_at(v: Any, idx: tuple[int, ...], shape: tuple[int, ...]) -> Any:
        own = _shape(v)
        idx = idx[len(idx) - len(own):] if own else ()
        for i, n in zip(idx, own):
            v = v[0 if n == 1 else i]
        return v
    sx, sy = _shape(x), _shape(y)
    if sx is None or sy is None:
        return False
    rank = max(len(sx), len(sy))
    px, py = (1,) * (rank - len(sx)) + sx, (1,) * (rank - len(sy)) + sy
    if any(p != q and 1 not in (p, q) for p, q in zip(px, py)):
        return False
    big = tuple(max(p, q) for p, q in zip(px, py))
    for idx in itertools.product(*(range(n) for n in big)):
        u, w = flat_at(x, idx, big), flat_at(y, idx, big)
        if isinstance(u, list) or isinstance(w, list) or u != w:
            return False
    return True


def captures(snaps: Path, case: str) -> dict[str, Path]:
    out = {"final": snaps / f"{case}.json", "initial": snaps / "initial" / f"{case}.json",
           "series": snaps / "series" / f"{case}.json"}
    for p in sorted((snaps / "history").glob(f"{case}__*.json")):
        out["history/" + p.stem.split("__")[-1]] = p
    return {k: v for k, v in out.items() if v.is_file()}


def compare_pair(c_snaps: Path, c_ir: Path, c_case: str, m_snaps: Path, m_ir: Path,
                 m_case: str) -> list[str]:
    print(f"-- composite {c_case} / member {m_case}")
    findings = compare_inputs(c_ir, c_case, m_ir, m_case)
    if findings:
        for f in findings:
            print("  DIFFERS", f)
        print("case inputs differ: not compared further")
        return [f"{c_case}: {f}" for f in findings]
    c_cap, m_cap = captures(c_snaps, c_case), captures(m_snaps, m_case)
    for side, cap in (("composite", c_cap), ("member", m_cap)):
        missing = [k for k in REQUIRED_CAPTURES if k not in cap]
        if not any(k.startswith("history/") for k in cap):
            missing.append("history/<step>")
        if missing:
            findings.append(f"{side} lacks captures {missing}")
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
        findings += [f"{name}.{k} (member only)" for k in sorted(set(y) - set(x))]
        if name != "series":
            findings += [f"{name}.{k} (not shared)" for k in STATE_FIELDS if k not in shared]
    return [f"{c_case}: {f}" for f in findings]


def main() -> int:
    global STATE_FIELDS
    ap = argparse.ArgumentParser()
    ap.add_argument("--composite", default="shallow_water2d_rotating")
    ap.add_argument("--member", default="shallow_water2d")
    ap.add_argument("--composite-case", default="swr_off_cone_n064")
    ap.add_argument("--member-case", default="swe2d_ref_n064_sx000_sy000_dts100")
    ap.add_argument("--pair", action="append", default=[],
                    help="<composite case>=<member case>; replaces --composite-case/--member-case")
    ap.add_argument("--state-fields", default=",".join(STATE_FIELDS),
                    help="the fields every state capture of both sides must share")
    ap.add_argument("--at", action="append", default=[],
                    help="<spec_id>@<version>: read that node at that version (see AT)")
    a = ap.parse_args()
    for item in a.at:
        sid, _, ver = item.partition("@")
        if not sid or not ver:
            raise SystemExit(f"--at {item!r}: expected <spec_id>@<version>")
        AT[sid] = ver
    STATE_FIELDS = tuple(f for f in a.state_fields.split(",") if f)
    pairs = []
    for item in a.pair:
        c, _, m = item.partition("=")
        if not c or not m:
            raise SystemExit(f"--pair {item!r}: expected <composite case>=<member case>")
        pairs.append((c, m))
    if not pairs:
        pairs = [(a.composite_case, a.member_case)]
    c_node = node_dir("problem", a.composite)
    m_node = node_dir("problem", a.member)
    c_pipe = latest_pipeline(c_node)
    c_snaps, c_ir = run_of(c_pipe, c_node)
    resolved = [d for d in json.loads((c_pipe / "lineage.json").read_text())["resolved_dependencies"]
                if d["node_key"].replace("/", "__").replace("@", "__") == m_node]
    if len(resolved) != 1:
        raise SystemExit(f"the composite's lineage resolves {len(resolved)} pipelines of {m_node}")
    m_snaps, m_ir = run_of(REPO / resolved[0]["pipeline_ref"], m_node, resolved[0]["run_id"])
    findings: list[str] = []
    for c_case, m_case in pairs:
        findings += compare_pair(c_snaps, c_ir, c_case, m_snaps, m_ir, m_case)
    print("V-b:", f"AGREE (bitwise, every shared field of every capture; {len(pairs)} pair(s))"
          if not findings else f"DIFFER {findings}")
    return 1 if findings else 0

if __name__ == "__main__":
    sys.exit(main())
