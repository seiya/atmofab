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
