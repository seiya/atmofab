#!/usr/bin/env python3
"""Issue #632 P6 V-c (unbilled, no LLM): `tools/run_configuration.py` on the CERTIFIED binaries
of R (2026-11-13), the first runs of the tool on a binary the workflow certified.

  PYTHONPATH=. python3 <this file> [--subjects rot,chan] [--keep <dir>]

Per subject, every run goes through the tool's command line (`python3 tools/run_configuration.py
<spec_ref> --target fortran_cpu ...`), with `--out` under a fresh temporary directory outside
`workspace/`:

1. `control`: the base case with no override. Every capture it writes (initial, each history
   sample, final, series) must equal the CERTIFIED run's capture of that case, field by field, as
   the text the binary wrote (`json` with `parse_float=str`). The certified run is the one the
   pipeline's `lineage.json` names, the pipeline the one the tool's own `run_record.json` says it
   selected.
2. `override`: one input changed (`OVERRIDE` below). The run must be `ok` and its final state must
   differ from the control's in at least one of the state fields: the override reached the run.
3. the chain: `U`, the configuration `CHAIN` run uninterrupted; `A`, the same configuration
   continued (`--restart-from`) from U's history sample at `K1`; `B`, continued from A's OWN
   history sample at `K2 > K1`. Each of A and B must be `ok` with the tool's start check held,
   and every capture it writes must equal U's: the final capture; each history sample (A and B
   write the samples after their start; the sample at the start step is the restored state,
   which the start check compares); and the series, which covers only the continuation's own
   steps, against the tail of U's series (every per-step field; the scalar `t` field of a
   series, the time of its last entry, whole).

Exit 0 when every comparison holds and every run's exit status is the expected one; 1 otherwise.
A refusal of the tool (exit 2) is a finding, not routed around: the tool selects the certified
output with the workflow's own resolver and this script does not choose a pipeline for it.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[4]

STATE_FIELDS = ("h", "hu", "hv")

#: subject -> spec_ref, base case, an override, the chain's configuration and its two cuts.
SUBJECTS: dict[str, dict[str, Any]] = {
    "rot": {
        "spec_ref": "spec/problem/dynamics/shallow_water/shallow_water2d_rotating",
        "base": "swr_shear_n064_restart",
        # f0 20 -> 25: the Coriolis parameter of the non-uniform f the case evaluates
        "override": {"rotation.f0": 25.0},
        # the certified case as it is: history.every 61, n_step 183 -> samples 61, 122
        "chain": {},
        "cuts": (61, 122),
    },
    "chan": {
        "spec_ref": "spec/problem/dynamics/shallow_water/shallow_water2d_channel",
        "base": "chan_tc4_ref_n032_dts100",
        # the jet speed of the TC4 initial flow, 40 -> 30
        "override": {"initial.u_jet": 30.0},
        # n_step 543; the certified history.every 272 gives one sample, so the chain's
        # configuration sets 181 -> samples 181, 362
        "chain": {"history.every": 181},
        "cuts": (181, 362),
    },
}


def tool(spec_ref: str, config: Path, out: Path, restart: Path | None = None) -> int:
    cmd = [sys.executable, str(REPO / "tools" / "run_configuration.py"), spec_ref,
           "--target", "fortran_cpu", "--config", str(config), "--out", str(out)]
    if restart is not None:
        cmd += ["--restart-from", str(restart)]
    r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
    print(f"    $ run_configuration.py ... --out {out.name}"
          + (f" --restart-from {restart.relative_to(restart.parents[3])}" if restart else "")
          + f" -> exit {r.returncode}")
    if r.returncode:
        print("      stdout:", r.stdout[-1500:])
        print("      stderr:", r.stderr[-1500:])
    return r.returncode


def write_config(path: Path, base: str, inputs: dict[str, Any]) -> Path:
    path.write_text(yaml.safe_dump({"base_case": base, "inputs": inputs}))
    return path


def load_text(path: Path) -> Any:
    return json.loads(path.read_text(), parse_float=str, parse_int=str)


def captures(snaps: Path, case: str) -> dict[str, Path]:
    out = {"final": snaps / f"{case}.json", "initial": snaps / "initial" / f"{case}.json",
           "series": snaps / "series" / f"{case}.json"}
    for p in sorted((snaps / "history").glob(f"{case}__*.json")):
        out["history/" + p.stem.split("__")[-1]] = p
    return {k: v for k, v in out.items() if v.is_file()}


def compare(name: str, x: dict[str, Any], y: dict[str, Any]) -> list[str]:
    """Every field of `y` (the reference) must be in `x` with the same text."""
    diff = [k for k in y if k in x and x[k] != y[k]]
    missing = [k for k in y if k not in x]
    extra = [k for k in x if k not in y]
    print(f"      {name}: {len(y) - len(diff) - len(missing)}/{len(y)} fields equal"
          + (f"; DIFFER {diff}" if diff else "") + (f"; MISSING {missing}" if missing else "")
          + (f"; extra {extra}" if extra else ""))
    return [f"{name}.{k}" for k in diff + missing]


def record(out: Path) -> dict[str, Any]:
    return json.loads((out / "run_record.json").read_text())


def certified_snaps(rec: dict[str, Any]) -> Path:
    pipe = REPO / rec["pipeline_ref"]
    run_id = rec["validate_run_id"]
    node = rec["node_key"].replace("/", "__").replace("@", "__")
    print(f"    certified (the tool's selection): {rec['pipeline_ref']} run {run_id}, "
          f"ir {rec['ir_ref']}")
    return pipe / "runs" / run_id / node / "raw" / "state_snapshots"


def run_subject(key: str, cfg: dict[str, Any], root: Path) -> list[str]:
    findings: list[str] = []
    base, spec_ref = cfg["base"], cfg["spec_ref"]
    d = root / key
    d.mkdir()
    print(f"== {key}: {spec_ref} base {base}")

    # 1. control
    print("  control (no override) against the certified capture")
    out = d / "control"
    if tool(spec_ref, write_config(d / "control.yaml", base, {}), out):
        return [f"{key}: control run failed"]
    rec = record(out)
    print(f"    status {rec.get('status')}, final step {rec.get('final_step')}")
    cert = captures(certified_snaps(rec), base)
    mine = captures(out / "raw" / "state_snapshots", base)
    if set(cert) != set(mine):
        findings.append(f"{key} control: capture sets differ: tool {sorted(mine)} "
                        f"certified {sorted(cert)}")
    for name in sorted(set(cert) & set(mine)):
        findings += [f"{key} control {f}" for f in
                     compare(name, load_text(mine[name]), load_text(cert[name]))]
    control_final = load_text(mine["final"])

    # 2. override
    print(f"  override {cfg['override']}")
    out = d / "override"
    if tool(spec_ref, write_config(d / "override.yaml", base, cfg["override"]), out):
        findings.append(f"{key}: override run failed")
    else:
        fin = load_text(out / "raw" / "state_snapshots" / f"{base}.json")
        moved = [k for k in STATE_FIELDS if fin.get(k) != control_final.get(k)]
        print(f"    status {record(out).get('status')}; state fields that moved: {moved}")
        if not moved:
            findings.append(f"{key}: override moved no state field")

    # 3. chain
    k1, k2 = cfg["cuts"]
    print(f"  chain {cfg['chain'] or '(the certified case)'}: U; A from U@{k1}; B from A@{k2}")
    conf = write_config(d / "chain.yaml", base, cfg["chain"])
    u, a, b = d / "U", d / "A", d / "B"
    if tool(spec_ref, conf, u):
        return findings + [f"{key}: U failed"]
    hist = u / "raw" / "state_snapshots" / "history"
    if tool(spec_ref, conf, a, hist / f"{base}__{k1}.json"):
        return findings + [f"{key}: A failed"]
    if tool(spec_ref, conf, b, a / "raw" / "state_snapshots" / "history" / f"{base}__{k2}.json"):
        return findings + [f"{key}: B failed"]
    ucap = captures(u / "raw" / "state_snapshots", base)
    for label, seg, start in (("A", a, k1), ("B", b, k2)):
        rec = record(seg)
        print(f"    {label}: status {rec.get('status')}, start check "
              f"{rec.get('start_check')}, restart step {(rec.get('restart') or {}).get('step')}")
        scap = captures(seg / "raw" / "state_snapshots", base)
        expect = {n for n in ucap if n in ("final", "series")
                  or (n.startswith("history/") and int(n.split("/")[1]) > start)}
        got = {n for n in scap if n != "initial"
               and not (n.startswith("history/") and int(n.split("/")[1]) <= start)}
        if expect != got:
            findings.append(f"{key} {label}: capture set {sorted(got)} != U's after {start} "
                            f"{sorted(expect)}")
        for name in sorted(expect & got):
            x, y = load_text(scap[name]), load_text(ucap[name])
            if name == "series":
                n = len(x["step"])
                y = {k: (v[-n:] if isinstance(v, list) else v) for k, v in y.items()}
                if float(x["step"][0]) != start + 1:
                    findings.append(f"{key} {label}: series starts at step {x['step'][0]}")
            findings += [f"{key} {label} {f}" for f in compare(f"{label} {name}", x, y)]
    return findings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--subjects", default="rot,chan")
    ap.add_argument("--keep", help="a new directory outside workspace/ to keep the runs in")
    a = ap.parse_args()
    if a.keep:
        root = Path(a.keep).resolve()
        root.mkdir(parents=True)
    else:
        root = Path(tempfile.mkdtemp(prefix="vc_"))
    findings: list[str] = []
    try:
        for key in a.subjects.split(","):
            findings += run_subject(key, SUBJECTS[key], root)
    finally:
        if not a.keep:
            shutil.rmtree(root, ignore_errors=True)
    print("V-c:", "HOLDS (every capture equal; every run's exit as expected)" if not findings
          else f"FINDINGS {findings}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
