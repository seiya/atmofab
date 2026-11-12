#!/usr/bin/env python3
"""System tier PR-S3 V-a (unbilled, no LLM): the difference between the HAND-AUTHORED IR of
`problem/shallow_water2d_rotating` (`build_ir.py`, the one PR-S2's bench judged with) and its
CERTIFIED IR (the `ir_ref` of the latest `fortran_cpu` pipeline, the one V-a judges with).

  PYTHONPATH=. python3 <this file>

Printed, each as a count with its members:
- tests and cases present on one side only (by `test_id` / `case_id`);
- predicates matched by `(test_id, quantity, case or the SET of target cases)`, and those present
  on one side only;
- of the matched predicates: those whose `op` differs, those whose `value` differs as a NUMBER
  (a threshold that moved), those whose `value` differs only in spelling (`0.0` against `0`), those
  whose `target_cases` differ only in order, and the counts whose `expr` / `bind` differ in text
  (the two IRs were authored independently, so the expressions are spelled differently; whether
  they compute the same quantity is what the V-a tables measure, not this script);
- of the matched cases: the keys present on one side only.
Exit 0 always: this is a record, not a gate.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[4]))
sys.path.insert(0, str(HERE))

import bench  # noqa: E402
import build_ir  # noqa: E402


def key(p: dict[str, Any]) -> tuple:
    cases = (p["case"],) if p.get("case") else tuple(sorted(p.get("target_cases") or ()))
    return (p["test_id"], p["quantity"], cases)


def numeric_equal(a: Any, b: Any) -> bool:
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(numeric_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(numeric_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) \
            and not isinstance(a, bool) and not isinstance(b, bool):
        return float(a) == float(b)
    return a == b


def flat_keys(d: Any, prefix: str = "") -> set[str]:
    if not isinstance(d, dict):
        return {prefix}
    out: set[str] = set()
    for k, v in d.items():
        out |= flat_keys(v, f"{prefix}.{k}" if prefix else k)
    return out


def main() -> int:
    hand, cert = build_ir.build(), bench.certified_ir()
    ht = {t["test_id"] for t in hand["io_contract"]["tests"]}
    ct = {t["test_id"] for t in cert["io_contract"]["tests"]}
    print(f"tests: hand {len(ht)}, certified {len(ct)}; hand only {sorted(ht - ct)}; "
          f"certified only {sorted(ct - ht)}")
    hc = {c["case_id"]: c for c in hand["case"]["test_case_set"]}
    cc = {c["case_id"]: c for c in cert["case"]["test_case_set"]}
    print(f"cases: hand {len(hc)}, certified {len(cc)}; hand only {sorted(set(hc) - set(cc))}; "
          f"certified only {sorted(set(cc) - set(hc))}")
    hp = {key(p): p for p in hand["io_contract"]["predicates"]}
    cp = {key(p): p for p in cert["io_contract"]["predicates"]}
    print(f"predicates: hand {len(hand['io_contract']['predicates'])}, certified "
          f"{len(cert['io_contract']['predicates'])}; distinct keys hand {len(hp)}, certified "
          f"{len(cp)}; hand only {sorted(set(hp) - set(cp))}; certified only "
          f"{sorted(set(cp) - set(hp))}")
    both = sorted(set(hp) & set(cp))
    op = [k for k in both if hp[k].get("op") != cp[k].get("op")]
    moved = [k for k in both if not numeric_equal(hp[k].get("value"), cp[k].get("value"))]
    spelled = [k for k in both if k not in moved
               and json.dumps(hp[k].get("value")) != json.dumps(cp[k].get("value"))]
    order = [k for k in both if hp[k].get("target_cases") != cp[k].get("target_cases")]
    text = Counter()
    for k in both:
        for f in ("expr", "bind"):
            if hp[k].get(f) != cp[k].get(f):
                text[f] += 1
        if hp[k].get("expr") != cp[k].get("expr") and hp[k].get("bind") != cp[k].get("bind"):
            text["expr and bind"] += 1
    print(f"matched predicates {len(both)}: op differs {op}")
    print(f"  threshold moved (value differs as a number): {moved}")
    print(f"  value differs in spelling only: "
          f"{[(k, hp[k]['value'], cp[k]['value']) for k in spelled]}")
    print(f"  target_cases differ in order only: {order}")
    print(f"  text differs: {dict(text)}")
    extra = Counter()
    for cid in sorted(set(hc) & set(cc)):
        a, b = flat_keys(hc[cid]), flat_keys(cc[cid])
        for k in a - b:
            extra[f"hand only {k}"] += 1
        for k in b - a:
            extra[f"certified only {k}"] += 1
    print(f"case keys on one side only (key: number of cases): {dict(sorted(extra.items()))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
