# When you add a refusal, whose fault is it

Every new refusal gets routed somewhere. **Get the attribution wrong and you either burn retry
budget on a party that cannot fix it, or end the run of a party that can.** PR #51 did one of
each.

The canonical sources are `docs/workflow/phases/phase_02_generate.md` (the gate's failure
classification) and `docs/ORCHESTRATION.md` (how fail_closed is handled). Only the decision
procedure lives here.

## The decision

Decide by **who wrote the input**.

| Cause | Example | Routing |
|---|---|---|
| something the leaf wrote into its own write_root | a file named `src/-o.f90`, the contents of generated source, the contents of the IR | **content failure** (the author fixes it on warm resume) |
| an argument the conductor passes | a non-absolute `project_dir`, an empty capability token, an out-of-range `command_log_path` | **transport fail_closed** (the leaf cannot fix it) |
| environment or infrastructure | a mandatory compiler missing, a broken dependency closure, an unreadable IR | **transport fail_closed** |

When in doubt, ask: **could running this leaf again fix it?** If not, it must not be a content
failure.

## Name exceptions by type

Catching at the width of `except ValueError` gives the same attribution to **other reasons** the
same function raises for. In PR #51 an argument refusal from `tool_run_syntax_check` (a
conductor-side defect) was counted as the leaf's `syntax_error`, which on a setup that symlinked
`workspace/tmp` to another disk burned retry budget at every node.

```python
class SyntaxSourceNameError(ValueError):
    """Only names the leaf wrote. Every other argument refusal stays a plain ValueError."""
```

The catching side uses `except SyntaxSourceNameError`. **Pin both directions** (that exception
becomes content; other ValueErrors keep propagating).

## Side effects of dropping into a content failure

An early return skips the post-processing the normal path always performs. In PR #51 it returned
without writing `write_syntax_evidence` (the host-side certificate), and recorded a stage shape
the certificate reader refuses (`fail` with no command_id).

- Does it return **the same keys** as the normal path's return
- Does it write the artifacts the normal path writes (certificates, logs), or can you explain why
  it does not
- If no compiler or external process **ran**, the stage is `skipped` (there is no command to cite)

## When adding a new raise, read **what the enclosing handler was written for**

Attribution is decided by who wrote the input (the table above), but the actual routing is
overridden by **the meaning the `except` that catches it already carries**. A new exception
**inherits** the existing handler's classification.

In PR #57 I added a `RuntimeError` in `_register_codex_thread` intending to "surface a malformed
launch request". But that `try`'s handler was written for **host-side write failures** (ENOSPC,
a collision with an already-recorded identity) and it recovers the transaction journal and
**converts the result into a transport-dead `ProcResult`**. So the intent "surface it" had
become "kill one billed leaf". Here the raise happens **before any write**, and record-launch's
validator makes it unreachable besides, so it was judged harmless — but **that was the result of
reading and confirming, not something known when it was written**.

- **Always open the `except` outside your new `raise`.** If it has a docstring, read what the
  handler is for
- Does that handler's classification (transport / content / recovery) **match** your
  attribution
- If not: split the exception type and let the handler pass it through, move the raise outside
  the handler, or **decide to ship the mismatch and write down why** (PR #57 took the third; a
  reviewer asked for it to be flagged as a conscious decision)

## A branch a check gains when it starts DECLARING its tool's configuration (2026-08-28, PR #116)

Adding a declared configuration to a tool invocation creates an exit status that did not exist
before: **the invocation was refused**, as distinct from **the source was judged and failed**. The
lint gate's `--select` is validated by the linter's own argument parser, so a rule code the
installed build does not know exits without reading a file. Before the declared set the argv had
no `--select` and the exit was unreachable.

The routing table above sends "what the leaf can fix" to a content failure and everything else to
a transport fail_closed — and the defect was that the gate never asked the question. `ok=false`
became `failure_category="lint_findings"`, so a leaf was warm-resumed with an argv error as its
excerpt and a file it has no write authority over as the thing to fix: the unwinnable loop of
issue #110, in a place the fix for #110 created. A blank-slate reviewer found it in the last round
of the loop.

Three things worth keeping from how it was closed:

- **The discriminator must not be an error string.** The obvious test — "exit 1 and the output
  says `Error:`" — is a classification channel the checked source contributes text to, since file
  names appear in diagnostics. What was used instead is the PRESENCE OF A VERDICT: an exit status
  that cannot mean "there are findings".
- **The first version over-refused, immediately.** It also refused an exit 1 that printed no
  diagnostic line, and that false-refused a legitimate content-failure fixture whose output shape
  it had not been measured against — one test run to surface, and the default error direction this
  repository keeps recording.
- **The half that could not be classified safely moved to LAUNCH.** A withdrawn code (the tool
  knows it and refuses to select it) exits 1 like an ordinary findings run. Rather than parse
  output, the declared invocation is run once over an EMPTY directory before the first leaf, where
  a usable build reports `0 files scanned` and exits 0 — the whole answer is a bare exit status.
  That is §3's "decide when it should surface" applied: whose fault, then where is the earliest
  place it can be detected reliably, then keep the gate's refusal as the backstop.

## Attribution detail moved from SKILL.md (2026-08-25)

The fuller statement of two rules `SKILL.md` §3 now carries compressed — when a correct
attribution surfaces at the wrong moment, and what a refusal message must say to be followable —
together with their episodes. Both sides state a rule, which is one site over the pair; if a third
appears, rule 3-a applies to this pair as much as to anything else.

The surfaces named in the last bullet — dependencies, preflight, execution policy, consistency
with repository conventions — are the list `metforge-review-loop` §"When to bring in Codex" carries.

**Once attribution is decided, decide when it should surface.** Attribution can be right while
the **moment** is wrong, and the attribution discussion alone never surfaces that. In L174 a new
parser dependency (two Python packages) missing from a machine makes the gate fail closed and
go terminal — attribution "operator" is correct. But it surfaces **at the first Generate node's
gate, after lint and syntax passed, in the middle of a billed run**. **The right failure at the
wrong moment.**

- **Do not fail mid-run on something detectable at launch.** The precedent exists
  (`REQUIRED_CLI_TOOLS` in `tools/run_workflow.py`, whose comment even states the reason:
  "Missing any one fails the run before init, so agents never hit a partial-failure state")
- Order of decisions: **(a) whose fault is it → (b) where is the earliest place it can be
  detected reliably**. If (b) is earlier than the gate, **keep the gate's fail-closed as a
  backstop** and add the earlier check as well
- Eight subagents across four rounds never raised this; **Codex raised it on its first pass**.
  Dependencies, preflight, and operations are surfaces subagents structurally do not look at (see
  the Codex section of `metforge-review-loop`)

**The cause a refusal message names must match the actual set of causes.** For a fail-closed,
failing correctly is not enough. If the party that failed is a leaf, it is closed only once the
message is **an instruction under which a warm retry converges**. L174's refusal asserted **a
single cause** ("the cause is a variable named with a keyword; rename it"), and once a
label-induced refusal was added, that input had no rename target at all = **an instruction that
cannot converge in principle**, handed back to the leaf (flagged two rounds running).

- When the causes grow, **grow the message**. Do not assert "the measured cause is X" in the
  singular
- For parsers, say that **the reported position drifts** (error recovery reports the head of the
  program unit rather than the actual cause). Do not let it read as "the cause is here"
- If the enumeration does not close, **say so and describe the shape to look for** ("an
  identifier or label sits where the parser expects structure")
- **Order the causes by REACHABILITY, most reachable first** — this one is not only about a
  leaf: it applies to any remedy a check prints, an operator's included, and it is surface 6's
  question asked of the message instead of the command. Issue #71's roster check offered
  a vendor cause ("the CLI stopped offering this tool, record it in the absent-on-CLI seam")
  when the shortest path to that failure was a one-line `permissions.deny` in the leaf
  configuration the probe itself seeds. Following the printed remedy subtracts the tool from
  the required set permanently: **the check goes green by having been WIDENED rather than
  satisfied**, which is surface 6's regenerate command delivered as prose
- **A remedy must not be followable by HALF.** The same branch's `Glob` refusal ended
  "Re-issue it against a granted directory, or drop the leading `/` and pass the directory as
  `path`" — read as two options. A leaf doing only the first half is ALLOWED by the hook (rc=0,
  measured) and the tool then matches nothing, because a relative pattern is anchored at the
  repository root while the search is confined to `path`. No refusal, no log line, nothing
  below that could produce one. **Silent empty is the worst answer a boundary can give**: it is
  indistinguishable from a true negative, so the leaf reports absence and stops. When a remedy
  is a conjunction, say so, and say what doing one half produces

