#!/usr/bin/env python3
"""System tier PR-S3 V-a (unbilled, no LLM): the difference between the HAND-AUTHORED IR of
`problem/shallow_water2d_rotating` (`build_ir.py`, the one PR-S2's bench judged with) and its
CERTIFIED IR (the `ir_ref` of the latest `fortran_cpu` pipeline whose run passed, the one V-a
judges with).

  PYTHONPATH=. python3 <this file> [--at <spec_id>@<version>]...

Printed, each as a count with its members:
- tests and cases present on one side only (by `test_id` / `case_id`);
- predicate keys `(test_id, quantity, case or the SET of target cases)`, and those present
  on one side only; a key can carry several predicates, so each key's predicates are compared
  as a MULTISET on each field below, never one per key;
- of the matched keys: those whose number of predicates differs, whose `op` multiset differs,
  whose `(op, value)` multiset differs as NUMBERS (a threshold that moved), whose values differ
  only in spelling (`0.0` against `0`), whose `target_cases` lists differ (in order or
  content), and the counts of keys whose `expr` / `bind` multisets differ in text. Each field
  is a multiset of its own, so within a key that carries several predicates the pairing of a
  threshold with its expression is not compared: two sides that give the same set of
  tolerances to different components read as unchanged
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


def flat_keys(d: Any, prefix: str = "") -> set[str]:
    if not isinstance(d, dict):
        return {prefix}
    out: set[str] = set()
    for k, v in d.items():
        out |= flat_keys(v, f"{prefix}.{k}" if prefix else k)
    return out


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--at", action="append", default=[],
                    help="<spec_id>@<version>: read that node at that version (bench.AT)")
    for item in ap.parse_args().at:
        sid, _, ver = item.partition("@")
        if not sid or not ver:
            raise SystemExit(f"--at {item!r}: expected <spec_id>@<version>")
        bench.AT[sid] = ver
    hand, cert = build_ir.build(), bench.certified_ir()
    ht = {t["test_id"] for t in hand["io_contract"]["tests"]}
    ct = {t["test_id"] for t in cert["io_contract"]["tests"]}
    print(f"tests: hand {len(ht)}, certified {len(ct)}; hand only {sorted(ht - ct)}; "
          f"certified only {sorted(ct - ht)}")
    hc = {c["case_id"]: c for c in hand["case"]["test_case_set"]}
    cc = {c["case_id"]: c for c in cert["case"]["test_case_set"]}
    print(f"cases: hand {len(hc)}, certified {len(cc)}; hand only {sorted(set(hc) - set(cc))}; "
          f"certified only {sorted(set(cc) - set(hc))}")
    # A key can carry several predicates (22 keys carry 2 or more on each side here), so each
    # side's predicates are grouped per key and compared as multisets, never one per key.
    hp: dict[tuple, list] = {}
    cp: dict[tuple, list] = {}
    for side, ir in ((hp, hand), (cp, cert)):
        for pr in ir["io_contract"]["predicates"]:
            side.setdefault(key(pr), []).append(pr)
    print(f"predicates: hand {len(hand['io_contract']['predicates'])}, certified "
          f"{len(cert['io_contract']['predicates'])}; distinct keys hand {len(hp)}, certified "
          f"{len(cp)}, of which carrying more than one predicate hand "
          f"{sum(len(v) > 1 for v in hp.values())}, certified "
          f"{sum(len(v) > 1 for v in cp.values())}; hand only {sorted(set(hp) - set(cp))}; "
          f"certified only {sorted(set(cp) - set(hp))}")
    both = sorted(set(hp) & set(cp))

    def ms(preds: list, f) -> list:
        return sorted(json.dumps(f(x), sort_keys=True, default=str) for x in preds)

    def num(v: Any) -> Any:
        if isinstance(v, dict):
            return {k: num(x) for k, x in v.items()}
        if isinstance(v, list):
            return [num(x) for x in v]
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
        return v

    count = [k for k in both if len(hp[k]) != len(cp[k])]
    op = [k for k in both if ms(hp[k], lambda x: x.get("op")) != ms(cp[k], lambda x: x.get("op"))]
    moved = [k for k in both if ms(hp[k], lambda x: (x.get("op"), num(x.get("value"))))
             != ms(cp[k], lambda x: (x.get("op"), num(x.get("value"))))]
    spelled = [k for k in both if k not in moved
               and ms(hp[k], lambda x: x.get("value")) != ms(cp[k], lambda x: x.get("value"))]
    order = [k for k in both if ms(hp[k], lambda x: x.get("target_cases"))
             != ms(cp[k], lambda x: x.get("target_cases"))]
    text = Counter()
    for k in both:
        e = ms(hp[k], lambda x: x.get("expr")) != ms(cp[k], lambda x: x.get("expr"))
        b = ms(hp[k], lambda x: x.get("bind")) != ms(cp[k], lambda x: x.get("bind"))
        text["expr"] += e
        text["bind"] += b
        text["expr and bind"] += e and b
    print(f"matched keys {len(both)}: number of predicates differs {count}; op multiset "
          f"differs {op}")
    print(f"  threshold moved ((op, value) multiset differs as numbers): {moved}")
    print(f"  value differs in spelling only: "
          f"{[(k, [x['value'] for x in hp[k]], [x['value'] for x in cp[k]]) for k in spelled]}")
    print(f"  target_cases multiset differs (order included): {order}")
    print(f"  keys whose expr / bind multiset differs in text: {dict(text)}")
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
