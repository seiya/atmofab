# Codex: launch mechanics and the case log

SKILL.md holds the operating rules. This file holds how to launch it and what each rule came from.

## Launching

`/codex:review` and `/codex:adversarial-review` are `disable-model-invocation: true` — **I cannot
launch them**; they are for the user to type. Each command file wraps a one-line `node …codex-companion.mjs`
invocation in execution rules (`review.md` is 61 lines around one call), so call the script
directly. Both `.md` files were read to confirm this, and the commands below
were run to confirm a job starts.

```bash
P=~/.claude/plugins/cache/openai-codex/codex/1.0.6   # check the version with ls
# only adversarial-review takes focus text (review is native and accepts no extra instruction)
node "$P/scripts/codex-companion.mjs" adversarial-review --background --base origin/main \
  "<engineering-flavoured focus text; limit the target files explicitly>"
node "$P/scripts/codex-companion.mjs" status <job-id>   # progress (phase / elapsed)
node "$P/scripts/codex-companion.mjs" result <job-id>   # the full text, after completion
```

- The shell backgrounds anything past 120 seconds, so **pass `--background` and keep the job id**
  (measured completions: 11m28s, ~3m, 12m). The `codex:codex-rescue` agent can launch it too, but
  it returns only the job id and you end up calling `status` / `result` anyway
- **Never wrap it in `timeout`. `--background` does not detach.** The launcher stays attached
  streaming progress, so **killing the launcher kills the codex child**, and the job record stays
  `running`, indistinguishable from a stall. Hit for real on the issue #63 PR with `timeout 110 node
  ...codex-companion.mjs review --background`: the job ran investigation commands every five seconds
  from t+0 to t+103 and **stopped dead at t+103** (the launcher starts about 7 seconds before the
  job's first log line, so job t+103 ≈ launcher 110s). The same branch without `timeout` **finished
  in about 12 minutes and returned one P1 and one P2**
- **Let the harness's background execution do the waiting** (`run_in_background`); do not add your
  own timeout or polling. Wrapping it came from **guessing** that `--background` means "returns
  immediately"; one observation before guessing would have shown it
- **Get the base wrong and you review an empty diff.** On a merged branch `origin/main` points at
  your own merge commit. Check `git diff --shortstat <base>...HEAD` before launching
- Job logs: `~/.claude/plugins/data/codex-openai-codex/state/<repo>-<hash>/jobs/<job-id>.log`

## Stalls

**Before suspecting a stall, check whether you killed it.** The one stall judged on the issue #63
PR was the `timeout` suicide above. The tell is the timestamp of the log's last line: investigation
commands at even intervals that cut off at one instant means an external kill. A true stall shows
`phase` not advancing **and no commands accumulating**.

Real stalls happen and are a different failure from crashing. PR #57 launched twice and **got no
report either time** — `phase: running` for 46 minutes, then `phase: verifying` for 28 minutes with
a narrowed focus, both far outside the 11m / 3m measurements. PR #72 stalled twice on native
`review` (`running` for 20 and 61 minutes, **zero partial output**, `result` returning `No job
found`).

- **Stalls are intermittent. Do not conclude "Codex cannot be used on this branch".** PR #72's
  **third run finished in about 2 minutes, and its single finding was the one defect that four
  subagent rounds — census and convergence judgment included — had all missed.** Stopping at two
  would have lost it
- So **the two-launch cap is a budget, not evidence of quality**. Whether to stop or draw a third
  is decided by how large the remaining doubt is. Evidence: a stall dies within the first few
  commands, so look at whether investigation commands are accumulating in the log (a completed run
  reaches `Review output captured.`)
- **Treat the same phase for more than 15 minutes as a stall and cancel.** Waiting collapses the
  "do not edit until both are in" rule with it
- `result <job-id>` returns **`No job found` before completion** (a different channel from status).
  Take in-flight information from status's `Progress:` and the launch command's stdout
- **A partial verdict sometimes appears in the launch stdout.** PR #57's second run emitted
  `Assistant message captured: {"verdict":"needs-attention", ...}` minutes before stalling, and its
  gist matched a defect a subagent found independently. Read the output up to the stall
- **Do not count a stall as clean** — same as a filter drop

## Native vs adversarial

**Prefer native `review`; `adversarial-review` stalls more often** (measured). In L174
`adversarial-review` stalled on the first run (22 minutes with no phase change and no report),
while the **native `/codex:review` the user typed came back in about 3 minutes with a P1 that
nobody across 4 rounds × 2 subagents had produced**. Only adversarial takes focus text, but the
probability of getting an answer back outweighs that. Even when narrowing the focus, run native
once first.

## Where Codex sees what subagents do not

Issue #63 is the data point on the other side of "clean is not a stopping condition": both of its
completed runs returned real defects, both in subagent blind spots.

In PR #51 it caught in one pass what 17 subagent rounds had walked past. Not capability — **it did
not share the premises I had handed over**. Hence: do not over-brief Codex on history either.

**PR #72, the clearest case.** Codex's single finding: "if the operator writes the same flag in the
`command:` prefix, the conductor appends its own, two appear in argv, and `argv.index()` reads the
operator's". **Four subagent rounds took argv as something the conductor builds, and nobody looked
at the interaction with the prefix.** The harm was not only a mis-record but a **fail-open in the
very fail-closed check that PR added** (config from the prefix was hashed, passing `.mcp.json`
through). Interaction with elements that come from anywhere other than "the input I built" is a
blind spot for a subagent handed the diff and the threat model.

**L174's split**, which is where "different axis" came from:

- **Codex**: an undeclared new dependency that, with no launch-time check, **surfaces only mid-way
  through a billed run** (P1) / a development harness calling the compiler without going through
  MCP (P2, judged not applicable)
- **8 subagents**: all **internal logic of the diff** (fail-opens, offsets, pins spinning in
  neutral, prose truth)

**But do not generalize from one case.** PR #66's two Codex runs were both internal logic, and one
**fully duplicated** a blank-slate subagent's finding. Allocate the single launch believing it is a
net on another axis and you will miss. Expect no split; use it as one independent pass.

## Where Codex is not a convergence signal

**For changes that add checking machinery.** Against a new static analyser or parser, Codex almost
always finds "one more construct". PR #58 launched three times and got **`needs-attention` all
three**, two of them pointing inside the previous round's fix (not a content filter — it simply
found something every time). The findings were useful and became regression tests, but **waiting
for "Codex is clean" never ends the loop**. Drop it from the superior condition there and judge by
"does it exist in the real corpus". For the same reason the second and third launches waste the
budget: replace them with blank-slate subagents.

**And when it IS clean, the round is not.** Clean has now come back twice, and both times the
subagent sharing that round was not clean. Issue #153: the round-2 launch (native `review`,
`--base origin/main --scope branch`, no stall, exit 0) returned "The changes consistently record and
validate dependency source bindings across both readiness evaluators and the closure driver. No
actionable correctness regressions were identified in the changed code." The blank-slate subagent in
the same round returned four findings — including a guard whose test family could not fail, and a
corrections bullet that had committed the error it condemned. **Two readings, and the second is the
useful one**: (a) Codex was right about what it was asked, which was correctness of the changed code,
and every one of the four findings was about a WITNESS or a RECORD rather than about production
behaviour; (b) that is exactly the class Codex does not look for, so a clean pass bounds a narrower
question than the round does. Note also that this branch ADDS checking machinery, where the paragraph
above predicts Codex almost always finds one more construct — it did not, so the prediction is a
tendency and not a rule. Cheap to record, and it stops "Codex was clean" from being quoted as
evidence about the round.

## Content filter drops

L128 launched four times and **three were interrupted** (security-flavoured vocabulary plus
wandering into code outside the review target; one died right after producing a verified
counterexample).

- **Do not count a filter drop as clean.** Zero output is not "no findings"
- What gets through: **engineering phrasing** ("evaluate this static analyser's parsing soundness",
  not "find the fail-opens"), **explicitly limited target files** so it does not wander, and asking
  for **counterexample construction** rather than attack
- **One rewrite-and-retry at most** (it consumes the second launch). If that fails too, say
  explicitly that the judgment is made without Codex and move on
