#!/usr/bin/env python3
"""Split leaf cost ACROSS runs into first attempts and retries, at (step, substep) granularity.

This is the instrument for the comparison rule in the SKILL's §"Comparing cost across runs":
a node's total cost cannot say whether an optimization worked while the workload is moving,
so the comparison is fixed at (node, step, substep, attempt-1) before anything is compared.
The rule and the measurement that motivated it are issue #94.

SOURCE: the `usage` rows of every `workspace*/orchestrations/*/agent_runs.jsonl` (issue #47).
A row whose usage is a marker (`not_measured`, `unavailable`) or absent carries no number and
is not counted as a zero; a run recorded before issue #47 therefore contributes nothing here,
and its figures need `analyze_timing.py` and the transcripts instead.

ATTEMPT: every leaf launch is its own `substep` row, so a retry of any kind -- an in-leaf repair
turn, a transport re-launch, a phase re-run after a later verdict or gate refused the result --
is a later row for the same (orchestration, node, step, substep). The earliest such row is the
first attempt; every later one is a retry. A node re-run in a NEW orchestration is a first
attempt there: this counts what one run spent redoing its own work, not what the operator
spent re-running.

CAUSE: a retry is attributed to the most recent non-`pass` substep row of the same node in the
same run that precedes it -- the verdict, gate or leaf failure that sent the run back. The
deterministic substeps (`compile.static`, `generate.gate`, ...) are counted here as causes even
though they carry no usage of their own.

TOKENS: `output_tokens` is the headline, because it is what bills the time (thinking included)
and it is the unit the 2026-07-12 audit quoted; `total_tokens` and the provider-reported
`cost_usd` are printed beside it because a warm-resumed retry re-reads a cached prompt and so
weighs less in them than in output.

Usage:  python3 attempt_cost_report.py [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--json]
"""
import argparse
import collections
import glob
import json
import os
import statistics
import sys

ORCH_GLOB = "workspace*/orchestrations/*/agent_runs.jsonl"


def repo_root(start=None):
    """Walk up to the checkout root, so the glob resolves from any cwd."""
    here = os.path.abspath(start or os.getcwd())
    while True:
        if os.path.isdir(os.path.join(here, ".git")):
            return here
        parent = os.path.dirname(here)
        if parent == here:
            return os.path.abspath(start or os.getcwd())
        here = parent


def measured(row):
    """The row's usage dict when it carries a number, else None (a marker is not a zero)."""
    usage = row.get("usage")
    if isinstance(usage, dict) and isinstance(usage.get("output_tokens"), int):
        return usage
    return None


def load_rows(root, since=None, until=None):
    """Every `substep` row in the window, keyed by (orchestration_id, node_key)."""
    by_node = collections.defaultdict(list)
    for path in sorted(glob.glob(os.path.join(root, ORCH_GLOB))):
        orch = os.path.basename(os.path.dirname(path))
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                started = row.get("started_at") or ""
                if row.get("agent_role") != "substep" or not started:
                    continue
                if (since and started < since) or (until and started >= until):
                    continue
                by_node[(orch, row.get("node_key"))].append(row)
    return by_node


def _bucket():
    return {"n": 0, "output_tokens": 0, "total_tokens": 0, "cost_usd": 0.0}


def _add(bucket, usage):
    bucket["n"] += 1
    bucket["output_tokens"] += usage.get("output_tokens") or 0
    bucket["total_tokens"] += usage.get("total_tokens") or 0
    bucket["cost_usd"] += usage.get("cost_usd") or 0.0


def analyze(by_node):
    totals = {"first": _bucket(), "retry": _bucket()}
    per_substep = collections.defaultdict(lambda: {"first": _bucket(), "retry": _bucket(),
                                                   "first_outputs": []})
    causes = collections.defaultdict(_bucket)
    orchestrations = set()
    for (orch, _node), rows in by_node.items():
        rows.sort(key=lambda r: r["started_at"])
        seen = set()
        for i, row in enumerate(rows):
            label = f"{row.get('step')}.{row.get('substep')}"
            usage = measured(row)
            is_retry = label in seen
            seen.add(label)
            if usage is None:
                continue
            orchestrations.add(orch)
            kind = "retry" if is_retry else "first"
            _add(totals[kind], usage)
            _add(per_substep[label][kind], usage)
            if not is_retry:
                per_substep[label]["first_outputs"].append(usage["output_tokens"])
                continue
            trigger = next((p for p in reversed(rows[:i]) if p.get("status") != "pass"), None)
            origin = (f"{trigger.get('step')}.{trigger.get('substep')} fail"
                      if trigger is not None else "no preceding failure")
            _add(causes[f"{origin} -> {label}"], usage)
    substeps = {}
    for label, entry in sorted(per_substep.items()):
        firsts = entry.pop("first_outputs")
        entry["attempt1_median_output_tokens"] = statistics.median(firsts) if firsts else None
        substeps[label] = entry
    return {"orchestrations": len(orchestrations), "totals": totals,
            "per_substep": substeps, "causes": dict(causes)}


def share(first, retry, key):
    whole = first[key] + retry[key]
    return retry[key] / whole if whole else None


def _pct(value):
    return "n/a" if value is None else f"{100 * value:.1f}%"


def render(report):
    first, retry = report["totals"]["first"], report["totals"]["retry"]
    out = [f"{report['orchestrations']} orchestrations, "
           f"{first['n'] + retry['n']} measured leaf launches ({retry['n']} retries)",
           f"retry share: output_tokens {_pct(share(first, retry, 'output_tokens'))}, "
           f"total_tokens {_pct(share(first, retry, 'total_tokens'))}, "
           f"cost_usd {_pct(share(first, retry, 'cost_usd'))}",
           "",
           f"{'step.substep':<22} {'first':>5} {'retry':>5} {'retry%out':>9} "
           f"{'attempt-1 median out':>21}"]
    for label, entry in report["per_substep"].items():
        median = entry["attempt1_median_output_tokens"]
        out.append(f"{label:<22} {entry['first']['n']:>5} {entry['retry']['n']:>5} "
                   f"{_pct(share(entry['first'], entry['retry'], 'output_tokens')):>9} "
                   f"{'n/a' if median is None else f'{median:,.0f}':>21}")
    out += ["", "retry output_tokens by cause (the failure that sent the run back -> the rerun):"]
    for cause, bucket in sorted(report["causes"].items(),
                                key=lambda kv: -kv[1]["output_tokens"]):
        out.append(f"{bucket['output_tokens']:>12,} {bucket['n']:>4}  {cause}")
    return "\n".join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--since", help="include rows started at or after this ISO date")
    parser.add_argument("--until", help="include rows started before this ISO date")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--root", help="checkout root (default: found from the cwd)")
    args = parser.parse_args(argv)
    report = analyze(load_rows(args.root or repo_root(), args.since, args.until))
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
