#!/usr/bin/env python3
"""Render `bench.py --all-mutants --json <file>` as the mutant table of the PR record: one row per
mutant, its class, caught or not, the tests it moved, its STRONGEST catch and its WEAKEST (the
failed predicates lying furthest and nearest outside their thresholds, `bench.failed_records`'
`outside`: in half-widths for one bound of a two-sided band, else relative to the threshold),
each with its value and threshold, and the number of (predicate, case) records it failed — a
lower bound, and the weakest catch is the weakest among the cases evaluated (`bench.failed_records`).

  python3 <this file> <table.json>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent


def main() -> int:
    table = json.loads(Path(sys.argv[1]).read_text())
    mutants = yaml.safe_load((HERE / "mutants.yaml").read_text())
    print("| mutant | class | caught | tests moved | strongest catch | weakest catch | records failed |")
    print("|---|---|---|---|---|---|---|")

    def cell(r):
        if r is None:
            return "-"
        v, t = r.get("value"), r.get("threshold")
        val = f"{v:.4g}" if isinstance(v, (int, float)) else str(r.get("error", ""))[:60]
        thr = f"{r['op']} {t:.4g}" if isinstance(t, (int, float)) else r["op"]
        out = (f", outside by {r['outside']:.3g} {r['outside_unit']}" if "outside" in r else "")
        return f"`{r['quantity']}` ({r['case']}): {val}, {thr}{out}"

    for mid, m in mutants.items():
        res = table.get(mid, {})
        moved = res.get("moved", [])
        failed = res.get("failed", [])
        # strongest / weakest within one unit: a band's half-widths before a relative excess
        # before an absolute one (the units do not compare with each other)
        order = {"half-widths": 0, "relative": 1, "absolute": 2}
        ranked = sorted((r for r in failed if "outside" in r),
                        key=lambda r: (order[r["outside_unit"]], r["outside"]))
        best = max((r for r in ranked if r["outside_unit"] == ranked[0]["outside_unit"]),
                   key=lambda r: r["outside"]) if ranked else (failed[0] if failed else None)
        worst = ranked[0] if ranked else None
        cls = "must catch" if m["class"] == "must_catch" else "declared unobservable"
        print(f"| {mid}: {m['title']} | {cls} | {'yes' if moved else 'no'} | "
              f"{', '.join(f'`{t}`' for t in moved) or '-'} | {cell(best)} | {cell(worst)} | "
              f"{len(failed)} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
