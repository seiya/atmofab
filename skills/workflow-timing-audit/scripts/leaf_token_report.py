#!/usr/bin/env python3
"""Report what each LLM leaf of one `orchestration` spent, from that run's own artifacts.

The figures `docs/ORCHESTRATION.md` §"Leaf LLM configuration" records for an HTTP leaf --
reasoning against answer, the output ceiling a request hit, and the throughput it sustained --
are re-taken with this script rather than transcribed. Three artifacts are needed and all three
are load-bearing:

  launches/<agent_run_id>.http_response.txt   the raw event stream: `usage` and `finish_reason`
  launches/<agent_run_id>.response.json       `started_at`, the ONLY record of when the request
                                              began (an event stream carries no timestamps, so a
                                              report that omits this file cannot compute a rate)
  agent_runs.jsonl                            `step` / `substep` / `finished_at`, and the order

Two properties of the source decide how this reads them:

* A leaf that died in transport has no `usage` frame at all. That is reported as a transport
  death, never as a zero -- a zero would enter an average as a real observation.
* At the ceiling the endpoint's own token accounting does not close: `completion_tokens` minus
  `reasoning_tokens` lands either side of zero while not one character of answer was written.
  The answer is therefore counted in CHARACTERS off the `delta.content` frames, which is what
  "a zero-character answer" in that document means.

Usage:  python3 leaf_token_report.py <orchestration_dir> [substep ...]
"""
import datetime
import glob
import json
import os
import sys


def rows(orch):
    """Yield one dict per leaf launch of `orch` that has a persisted event stream."""
    runs = {r["agent_run_id"]: r for r in map(json.loads, open(f"{orch}/agent_runs.jsonl"))}
    stamp = lambda s: datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    for path in sorted(glob.glob(f"{orch}/launches/*.http_response.txt")):
        arid = os.path.basename(path).split(".")[0]
        run = runs.get(arid, {})
        usage, finish, answer_chars = None, set(), 0
        for line in open(path, encoding="utf-8", errors="replace"):
            if not line.startswith("data: "):
                continue
            body = line[6:].strip()
            if body == "[DONE]":
                continue
            try:
                frame = json.loads(body)
            except ValueError:
                continue
            usage = frame.get("usage") or usage
            for choice in frame.get("choices") or []:
                if choice.get("finish_reason"):
                    finish.add(choice["finish_reason"])
                delta = choice.get("delta") or {}
                if isinstance(delta.get("content"), str):
                    answer_chars += len(delta["content"])
        row = {"agent_run_id": arid, "step": run.get("step"), "substep": run.get("substep"),
               "finish_reason": sorted(finish), "transport_death": usage is None}
        if usage is None:
            yield row
            continue
        started = json.load(open(f"{orch}/launches/{arid}.response.json"))["started_at"]
        elapsed = (stamp(run["finished_at"]) - stamp(started)).total_seconds()
        details = usage.get("completion_tokens_details") or {}
        row.update(prompt_tokens=usage.get("prompt_tokens"),
                   output_tokens=usage["completion_tokens"],
                   reasoning_tokens=details.get("reasoning_tokens"),
                   answer_chars=answer_chars, elapsed_seconds=round(elapsed, 1),
                   tokens_per_second=round(usage["completion_tokens"] / elapsed, 1))
        yield row


def main(argv):
    if len(argv) < 2:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    wanted = set(argv[2:])
    for row in rows(argv[1]):
        if wanted and row.get("substep") not in wanted:
            continue
        if row["transport_death"]:
            print(f"{row['agent_run_id'][:8]} {str(row['substep']):8} TRANSPORT DEATH "
                  f"(no usage frame)")
            continue
        print(f"{row['agent_run_id'][:8]} {str(row['substep']):8} in={row['prompt_tokens']:6} "
              f"reasoning={row['reasoning_tokens']:6} answer={row['answer_chars']:6}ch "
              f"out={row['output_tokens']:6} {row['elapsed_seconds']:7.1f}s "
              f"{row['tokens_per_second']:5.1f} tok/s {row['finish_reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
