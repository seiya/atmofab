#!/usr/bin/env python3
"""Render `bench.py --all-mutants --json <file>` as the mutant table of the PR record: one row per
mutant, its class, caught or not, the tests it moved, and its STRONGEST catch (the failed
predicate with the largest value / threshold ratio) with that predicate's metric and threshold,
plus the number of (predicate, case) records it failed. A catch by a small factor is visible.

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
    print("| mutant | class | caught | tests moved | strongest catch: quantity (case) | value | op threshold | ratio | records failed |")
    print("|---|---|---|---|---|---|---|---|---|")
    for mid, m in mutants.items():
        res = table.get(mid, {})
        moved = res.get("moved", [])
        failed = res.get("failed", [])
        best = max((r for r in failed if r.get("ratio")), key=lambda r: r["ratio"], default=None)
        if best is None and failed:
            best = failed[0]
        cls = "must catch" if m["class"] == "must_catch" else "declared unobservable"
        if best:
            v = best.get("value")
            val = f"{v:.4g}" if isinstance(v, (int, float)) else str(best.get("error", ""))[:60]
            thr = f"{best['op']} {best['threshold']:.4g}" if isinstance(best.get("threshold"), (int, float)) else best["op"]
            ratio = f"{best['ratio']:.3g}" if best.get("ratio") else "-"
            q = f"`{best['quantity']}` ({best['case']})"
        else:
            val = thr = ratio = q = "-"
        print(f"| {mid}: {m['title']} | {cls} | {'yes' if moved else 'no'} | "
              f"{', '.join(f'`{t}`' for t in moved) or '-'} | {q} | {val} | {thr} | {ratio} | "
              f"{len(failed)} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
