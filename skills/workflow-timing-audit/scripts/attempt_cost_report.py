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

SEGMENT: an orchestration is cut at each `resume_status_reset` event of its
`phase_state_log.jsonl`, i.e. at each operator `--resume`. Attempts are ranked inside one
segment, so the first post-resume run of a substep is a first attempt: this counts what a run
spent redoing its own work, not what the operator spent re-running. A node re-run in a NEW
orchestration is a first attempt for the same reason.

ATTEMPT: every leaf launch is its own `substep` row, so a retry of any kind -- an in-leaf repair
turn, a transport re-launch, a phase re-run after a later verdict or gate refused the result --
is a later row for the same (segment, node, step, substep), ranked by `started_at`. The earliest
such row is the first attempt whatever caused that phase to run; every later one is a retry.
The retry figure is therefore a lower bound on the rework inside a run.

CAUSE: a retry is attributed to the most recent non-`pass` substep row of the same node in the
same segment that precedes it -- the proximate failure that sent the run back, not necessarily
the root. The deterministic substeps (`compile.static`, `generate.gate`, ...) are counted here
as causes even though they carry no usage of their own.

CUMULATIVE ROWS: a claude leaf's recorded usage comes from the result envelope's `modelUsage`
and `total_cost_usd`. On a warm-resumed turn some CLI versions report those as the running
total of the whole resumed session, while the envelope's top-level `usage` stays per turn
(measured on issue #94: three turns of one session recorded 43,861 / 74,526 / 105,418 output
tokens against a per-turn 43,861 / 30,665 / 30,892). A warm-resumed turn names the turn it
resumed in its launch request (`launches/<agent_run_id>.request.json`: `warm_resume` and
`repair_target_agent_run_id`). Its row is replaced by its difference from that turn's recorded
row when, and only when, the difference equals the turn's own envelope top-level `usage`
(`agents/<agent_run_id>/dialogs/leaf.stdout.log`) in all four token classes. A warm-resumed
row whose recorded usage differs from its envelope `usage` and for which the equality does not
hold is left as recorded and counted in `uncorrected_warm_resumes`, so a row this rule could
not decide is visible rather than silently summed.

TOKENS: `output_tokens` is the headline, because it is what bills the time (thinking included).
`total_tokens` (which also counts input and cache reads/writes) and the provider-reported
`cost_usd` are printed beside it; the three shares differ on real runs, so a figure quoted from
this report names which of the three it is.

Usage:  python3 attempt_cost_report.py [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--json]
"""
import argparse
import bisect
import collections
import glob
import json
import os
import statistics
import sys

ORCH_GLOB = "workspace*/orchestrations/*/agent_runs.jsonl"
TOKEN_CLASSES = ("input_tokens", "output_tokens", "cache_read_input_tokens",
                 "cache_creation_input_tokens")


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


def _jsonl(path):
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def resume_points(orch_dir):
    """Sorted timestamps of the operator `--resume`s recorded for one orchestration."""
    return sorted(e["ts"] for e in _jsonl(os.path.join(orch_dir, "phase_state_log.jsonl"))
                  if e.get("event") == "resume_status_reset" and isinstance(e.get("ts"), str))


def envelope(orch_dir, agent_run_id):
    """The leaf's CLI result envelope, or None when there is none to read."""
    path = os.path.join(orch_dir, "agents", str(agent_run_id), "dialogs", "leaf.stdout.log")
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        doc = json.loads(text[text.index("{"):])
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _request(orch_dir, agent_run_id):
    try:
        with open(os.path.join(orch_dir, "launches", f"{agent_run_id}.request.json"),
                  encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def decumulate(rows, orch_dir):
    """Replace each cumulative warm-resumed row's usage by its per-turn difference.

    A warm-resumed row the equality cannot decide keeps its usage and is marked `_uncorrected`.
    """
    # The RECORDED usage of every row, taken before any correction: a cumulative total minus
    # the resumed turn's cumulative total is this turn, whether or not that one was corrected.
    recorded = {r.get("agent_run_id"): dict(r["usage"]) for r in rows if measured(r)}
    for row in rows:
        cur = recorded.get(row.get("agent_run_id"))
        request = _request(orch_dir, row.get("agent_run_id")) if cur else None
        if not request or request.get("warm_resume") is not True:
            continue
        env = envelope(orch_dir, row.get("agent_run_id"))
        turn = env.get("usage") if env and isinstance(env.get("usage"), dict) else None
        if turn is None or all((cur.get(c) or 0) == (turn.get(c) or 0) for c in TOKEN_CLASSES):
            continue
        prev = recorded.get(request.get("repair_target_agent_run_id"))
        diff = {c: (cur.get(c) or 0) - (prev.get(c) or 0) for c in TOKEN_CLASSES} if prev else None
        if diff is None or any(diff[c] != (turn.get(c) or 0) for c in TOKEN_CLASSES):
            row["_uncorrected"] = True
            continue
        fixed = dict(cur, **diff, total_tokens=sum(diff.values()), decumulated=True)
        if isinstance(cur.get("cost_usd"), (int, float)):
            fixed["cost_usd"] = cur["cost_usd"] - (prev.get("cost_usd") or 0.0)
        row["usage"] = fixed


def load_rows(root, since=None, until=None):
    """Every `substep` row in the window, keyed by (orchestration_id, segment, node_key)."""
    by_node = collections.defaultdict(list)
    for path in sorted(glob.glob(os.path.join(root, ORCH_GLOB))):
        orch_dir = os.path.dirname(path)
        orch = os.path.basename(orch_dir)
        resumes = resume_points(orch_dir)
        rows = [r for r in _jsonl(path)
                if r.get("agent_role") == "substep" and r.get("started_at")]
        decumulate(rows, orch_dir)
        for row in rows:
            started = row["started_at"]
            if (since and started < since) or (until and started >= until):
                continue
            segment = bisect.bisect_right(resumes, started)
            by_node[(orch, segment, row.get("node_key"))].append(row)
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
    decumulated = uncorrected = 0
    for (orch, _segment, _node), rows in by_node.items():
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
            decumulated += bool(usage.get("decumulated"))
            uncorrected += bool(row.get("_uncorrected"))
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
            "per_substep": substeps, "causes": dict(causes),
            "decumulated_rows": decumulated, "uncorrected_warm_resumes": uncorrected}


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
           f"cumulative warm-resume rows corrected: {report['decumulated_rows']}; "
           f"warm-resume rows left as recorded: {report['uncorrected_warm_resumes']}",
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
