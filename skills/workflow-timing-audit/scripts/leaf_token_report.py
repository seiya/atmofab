#!/usr/bin/env python3
"""Report what each LLM leaf of one `orchestration` spent, from that run's own artifacts.

The figures `docs/ORCHESTRATION.md` §"Leaf LLM configuration" records for an HTTP leaf --
reasoning against answer, the output ceiling a request hit, and the throughput it sustained --
are taken with this script rather than transcribed. Three artifacts are needed and all three
are load-bearing:

  launches/<agent_run_id>.http_response.txt   the raw event stream: `usage` and `finish_reason`
  launches/<agent_run_id>.response.json       `started_at`, the record of when the REQUEST began
                                              (an event stream carries no timestamps of its own,
                                              and `agent_runs.jsonl` also has a `started_at` --
                                              that one is the AGENT RUN's, written within
                                              microseconds of its `finished_at`, so it cannot
                                              give an elapsed)
  agent_runs.jsonl                            `step` / `substep` / `finished_at`

DIALECT: this reads the OpenAI dialect (`choices[].delta`, `usage.completion_tokens`), which is
what `openai_compatible` speaks. A Messages-API (`anthropic_api`) stream persists to the same
filename and is reported as a dialect mismatch rather than parsed, because its fields are named
differently and quietly reading zero out of them would be worse than saying so.

ORDER: rows are sorted by the agent run's `finished_at`, so the report reads as the run's own
timeline. Sorting by filename would give lexical `agent_run_id` order, which is not chronological.

Three properties of the source decide the rest of the parse:

* A leaf can fail to produce a `usage` frame in three distinct ways, and they are NOT the same
  finding: the body was never an event stream at all (an HTTP error page -- which for a 504 is
  DEADLINE evidence and belongs to sizing), the stream was severed mid-flight, or the run
  recorded no `finished_at` to measure against. Each is reported as itself; none is reported as
  a zero, which would enter an average as an observation.
* At the ceiling the endpoint's token accounting does not close: `completion_tokens` minus
  `reasoning_tokens` lands either side of zero while not one character of answer was written.
  The answer is therefore counted in CHARACTERS off the `delta.content` frames, which is what
  "a zero-character answer" in that document means.
* An elapsed that is zero or negative is a broken record, not a fast request: a rate is not
  computed from one.

Usage:  python3 leaf_token_report.py <orchestration_dir | orchestration_id> [substep ...]
"""
import datetime
import glob
import json
import os
import sys

WORKSPACE_ROOT = "workspace/orchestrations"


def repo_root(start=None):
    """Walk up to the checkout root, so an id resolves from any cwd as the sibling does."""
    here = os.path.abspath(start or os.getcwd())
    while True:
        if os.path.isdir(os.path.join(here, ".git")):
            return here
        parent = os.path.dirname(here)
        if parent == here:
            return os.path.abspath(start or os.getcwd())
        here = parent


def resolve(target, start=None):
    """Accept a path or a bare `orchestration_id`, as the sibling audit script does."""
    if os.path.isdir(target):
        return target
    tried = []
    for base in (os.getcwd() if start is None else start, repo_root(start)):
        candidate = os.path.join(base, WORKSPACE_ROOT, target)
        tried.append(candidate)
        if os.path.isdir(candidate):
            return candidate
    raise FileNotFoundError(f"no orchestration directory at {target!r}; tried {tried}")


def _stamp(text):
    return datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))


def _read_runs(orch):
    runs = {}
    with open(f"{orch}/agent_runs.jsonl", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:                      # a trailing blank line is not a record
                continue
            record = json.loads(line)
            arid = record.get("agent_run_id")
            if arid in runs:                  # never silently keep the last one
                raise ValueError(f"duplicate agent_run_id in agent_runs.jsonl: {arid}")
            runs[arid] = record
    return runs


def _parse_stream(path):
    """Return (usage, finish_reasons, answer_chars, frames_seen, head)."""
    usage, finish, answer_chars, frames, head = None, set(), 0, 0, ""
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if len(head) < 90:
                # Whitespace-squashed so an HTML error page shows its TITLE rather than its
                # first tag: a `504 Gateway Time-out` is deadline evidence and has to be
                # readable from the report that sizes deadlines.
                head = (head + " " + " ".join(line.split())).strip()[:90]
            if not line.startswith("data: "):
                continue
            body = line[6:].strip()
            if body == "[DONE]":
                continue
            try:
                frame = json.loads(body)
            except ValueError:
                continue
            frames += 1
            if frame.get("usage") is not None:
                # LAST usage frame wins: the OpenAI dialect sends the cumulative one at the end.
                usage = frame["usage"]
            for choice in frame.get("choices") or []:
                if choice.get("finish_reason"):
                    finish.add(choice["finish_reason"])
                delta = choice.get("delta") or {}
                if isinstance(delta.get("content"), str):
                    answer_chars += len(delta["content"])
    return usage, finish, answer_chars, frames, head


def rows(orch):
    """Yield one dict per persisted leaf stream of `orch`, in run order."""
    runs = _read_runs(orch)
    out = []
    for path in glob.glob(f"{orch}/launches/*.http_response.txt"):
        arid = os.path.basename(path).split(".")[0]
        run = runs.get(arid)
        usage, finish, answer_chars, frames, head = _parse_stream(path)
        row = {"agent_run_id": arid, "step": run.get("step") if run else None,
               "substep": run.get("substep") if run else None,
               "finish_reason": sorted(finish), "note": None,
               "finished_at": (run or {}).get("finished_at")}
        if run is None:
            row["note"] = "no agent_runs.jsonl row for this launch"
        elif usage is None and frames == 0:
            # NOT spelled "not an event stream": `docs/ORCHESTRATION.md` defines
            # `response_not_an_event_stream` as a 200 body that did not OPEN as one, which fails
            # closed on the first attempt. What is seen here is only that no frame parsed -- an
            # HTTP error body reaches this branch too, and those ARE classified and retried.
            row["note"] = f"no event-stream frames parsed; body starts: {head!r}"
        elif usage is None:
            row["note"] = f"stream severed after {frames} frames (no usage frame)"
        elif run.get("finished_at") is None:
            row["note"] = "agent run records no finished_at"
        if row["note"] is None:
            with open(f"{orch}/launches/{arid}.response.json", encoding="utf-8") as handle:
                started = json.load(handle)["started_at"]
            elapsed = (_stamp(run["finished_at"]) - _stamp(started)).total_seconds()
            details = usage.get("completion_tokens_details") or {}
            row.update(prompt_tokens=usage.get("prompt_tokens"),
                       output_tokens=usage.get("completion_tokens"),
                       reasoning_tokens=details.get("reasoning_tokens"),
                       answer_chars=answer_chars, elapsed_seconds=round(elapsed, 1))
            if usage.get("completion_tokens") is None:
                row["note"] = ("dialect mismatch: no `completion_tokens` in usage "
                               f"({sorted(usage)}) -- this reads the OpenAI dialect only")
            elif elapsed <= 0:
                row["note"] = f"elapsed is not positive ({elapsed:.1f}s); no rate computed"
            else:
                row["tokens_per_second"] = round(usage["completion_tokens"] / elapsed, 1)
        out.append(row)
    # Chronological, and a row with no `finished_at` sorts last rather than crashing the sort.
    out.sort(key=lambda r: (r["finished_at"] is None, r["finished_at"] or "", r["agent_run_id"]))
    return out


def main(argv):
    if len(argv) < 2:
        print("usage: leaf_token_report.py <orchestration_dir | orchestration_id> [substep ...]",
              file=sys.stderr)
        return 2
    orch = resolve(argv[1])
    wanted = set(argv[2:])
    printed = 0
    for row in rows(orch):
        if wanted and row.get("substep") not in wanted:
            continue
        printed += 1
        if row.get("tokens_per_second") is None:
            print(f"{row['agent_run_id'][:8]} {str(row['substep']):8} -- {row['note']}")
            continue
        print(f"{row['agent_run_id'][:8]} {str(row['substep']):8} in={row['prompt_tokens']:6} "
              f"reasoning={row['reasoning_tokens']:6} answer={row['answer_chars']:6}ch "
              f"out={row['output_tokens']:6} {row['elapsed_seconds']:7.1f}s "
              f"{row['tokens_per_second']:5.1f} tok/s {row['finish_reason']}")
    if not printed:
        target = f" for substep(s) {sorted(wanted)}" if wanted else ""
        print(f"no persisted leaf streams in {orch}{target} "
              f"(an all-CLI run persists none)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
