#!/usr/bin/env python3
"""One DEV-ONLY hook policy: an agent session must not wait by sleeping.

Separate from `tools/hooks/operator_safety.py` on purpose. That module holds the policies
applied from BOTH entrypoints (`CLAUDE.md` §hooks: "what they do share is
`tools/hooks/operator_safety.py`, applied deliberately from both"), and this rule belongs to
neither a workflow leaf nor the operator's checkout — it is about how an AGENT SESSION spends
wall-clock and process table. `tools/hooks/dev_cli.py` imports it; nothing on the leaf path does,
and nothing should: a leaf that sleeps wastes its own budget and gets no closer to reporting its
task done, which `AGENTS.md` §Development premises puts out of the defended set.

**The incident.** A review subagent, whose launch prompt said in as many words "create only waits
whose exit condition can be satisfied, and no background polling", polled a background job with
one `Bash` call per poll. Each call left a shell and a `sleep` child behind; after ~36 minutes
there were **144** of them on a shared machine, and the agent had produced no report. The operator
noticed before the session did. This repository's own review skill already records that handing
the rule over in the prompt is not enough — three such accidents happened with the rule in the
prompt — so the rule moves into the layer that can refuse.

**Why refuse the WAIT rather than the loop.** The observed shape was not a loop: it was
`eval 'sleep 1799; true'`, one sleep per tool call, which no loop-detector sees. It is also a
bypass of the harness's own block on foreground `sleep` — the wrapper is what waits, so the
harness's rule was satisfied while its purpose was not. Refusing the sleep itself is the only
form that covers both.

**What to do instead is not "wait differently".** Work the harness tracks re-invokes the session
when it finishes; polling it is pure waste. For a condition the harness cannot see, the `Monitor`
tool exists. Both are in the refusal message, most reachable first.

KNOWN OVER-REFUSAL, stated rather than discovered. The match is over the raw command text, so a
command that merely CONTAINS a sleep invocation is refused too — a `grep "sleep 60"`, a commit
message quoting this rule, a heredoc documenting it. This is the same trade
`operator_safety.py` records for its own rules and for the same reason: the failure direction is a
refusal the operator can rephrase, and narrowing it by parsing shell grammar is the losing line
(`.claude/skills/atmofab-enforcement-change/references/source-text-surface.md`). It is not
hypothetical: this rule refused two commands of the commit that introduced it, both of them
heredocs writing the documentation for it — the same way `operator_safety.py` was refused by
itself on 2026-08-26. What IS narrowed
is the shape: a bare word `sleep` is not matched, so `pkill sleep`, `grep sleep`, `ps | grep -c
"^sleep "` and prose about sleeping all pass. A DURATION has to follow.

THREE OVER-REFUSALS A REVIEWER MEASURED, listed because the paragraph above used to promise the
first two could not happen:

* **A cleanup command SCOPED BY THE DURATION is refused**: `pkill -f "sleep 1799"`, `pgrep -af
  "sleep 1799"`. The duration sits in a quoted argument, which raw text cannot tell from a wait —
  and quoting cannot be the discriminator either, since the incident's own spelling was
  `eval '<wait>; true'`. What keeps this from blocking cleanup is that `atmofab-review-loop`
  ALREADY forbids `pkill -f` for this job and mandates killing by PID after reading `ps`:
  `pkill -P <ppid>` and `kill <pid>` are unaffected, and stopping the agent reaps its children
  anyway. The prescribed route is open; the forbidden one is not. An earlier version of this
  docstring asserted the scoped form still worked, and that was simply false.
* **A search with an UNQUOTED variable is refused**: `grep sleep $FILE`, `grep sleep $(ls)`. The
  `$` is in the duration class because `sleep $N` is a real polling spelling, and in raw text the
  two are the same shape. Quoting the variable passes.
* **On the CODEX dev layer the rule reaches file CONTENT.** `.codex/hooks.json` matches
  `apply_patch`, Codex's only editing route, so a Codex session cannot write a file containing
  the refused literal without splitting the string; the Claude layer's `Write` / `Edit` are not
  matched. The two dev layers therefore differ in REACH. Inherited from what `operator_safety`
  already did to patch text rather than introduced here, and left alone — but it belongs in this
  list rather than in a reader's surprise.

LIMIT, so nobody reads this as a detector: it is a bound on the ordinary spellings, not a barrier.
Measured escapes: `usleep`, `sleep +5`, `python3 -c 'import time; time.sleep(60)'`, a helper
script, and — the ones that matter, because a polling agent reaches for them BY ACCIDENT rather
than to evade — a **busy loop** with no sleep in it, `timeout 300 tail -f`, and `read -t 60`. The
refusal message names the busy loop for exactly that reason. The operator owns the machine and can
always run a wait in another terminal; the point is that the SESSION stops producing them without
thinking.
"""

from __future__ import annotations

import re
from typing import Any

#: A `sleep` in command position, with a DURATION after it. The duration is what separates a wait
#: from every other appearance of the word, and it is why `grep sleep` / `pkill -f sleep` /
#: `ps | grep "^sleep "` are not refused.
#:
#: TWO prefixes, and only two, because that is what measurement left. A wrapper word —
#: `command`, `env`, `builtin`, `exec`, `nohup`, `time`, the shapes that defeat a shell function
#: or alias, and `command` is what got past the harness's own block in the incident — needs NO
#: alternative of its own: it is followed by whitespace, and whitespace is already a separator, so
#: the wait is matched by the separator branch. The first version of this pattern spelled the
#: wrapper words out anyway; the mutation sweep deleted that group and the suite stayed green, and
#: driving the pattern with the group removed showed it misses nothing (`command`, `env`, `time`
#: and a `do`-loop body all still match). What the sweep DID show to be load-bearing is the
#: backslash (`\sleep` is preceded by `\`, not a separator) and the absolute path (`/bin/sleep` is
#: preceded by `/`), so those two stay and the dead alternative is gone rather than kept as
#: reassurance.
_SLEEP_INVOCATION = re.compile(
    r"""
    (?:^|[\s;&|(`'"]|\$\()          # start, or after a separator / quote / substitution
    (?:\\)?                         # a leading backslash defeats an alias
    (?:/(?:usr/)?bin/)?             # an absolute path is the same program
    sleep\s+
    [0-9$.]                         # a duration: a literal, a leading-dot fraction, or a
                                    # variable holding one. The `.` is there because `sleep .5`
                                    # — a sub-second pause, the natural spelling INSIDE a fast
                                    # poll loop, which is this rule's own subject — was missed
                                    # while `sleep 0.5` was caught. Found by a reviewer, not by
                                    # the author's own case list.
    """,
    re.VERBOSE,
)

#: What the refusal tells the reader to do, most reachable cause first. Ordered because a remedy
#: is read in order and the first line is the one that gets followed
#: (`.claude/skills/atmofab-enforcement-change/references/failure-routing.md`).
_REMEDY = (
    "Do not wait by sleeping in an agent session. In order of what is most likely true here: "
    "(1) if you are waiting on work this harness started, DO NOT POLL — it re-invokes the "
    "session when the work finishes, so the wait is pure waste; "
    "(2) if you are waiting on a condition the harness cannot see, use the Monitor tool, which "
    "watches without leaving a process per check behind; "
    "(3) if you genuinely need a pause, run it in another terminal — this refusal is about what "
    "the SESSION spawns, not about what the machine may do. "
    "WHAT IS NOT THE ANSWER: a busy loop that spins instead of sleeping is worse for a shared "
    "machine, not better, and so is any other spelling of the same wait — this rule catches the "
    "one shape it can see, and going around it defeats the point rather than the check. "
    "Each poll leaves a shell and a sleep child behind: a subagent that was told not to poll "
    "produced 144 of them in 36 minutes on this machine and returned no result, which is why "
    "this is a hook and not a paragraph in a prompt."
)


def polling_wait_violation(command: str) -> tuple[str, dict[str, Any]] | None:
    """Return `(reason, audit_detail)` for a command that waits by sleeping, or None.

    A pure function of its argument, like `operator_safety_violation`, so a test drives it
    without a process and the DEV entrypoint stays stdlib-only.
    """
    # NOT PINNED, and kept anyway: an empty command finds no match either, so a mechanism
    # sweep cannot distinguish this guard from its absence. It mirrors
    # `operator_safety_violation`'s first line, and "the mutation survives" is not grounds for
    # deleting a defense in this repository — what it is grounds for is saying so here.
    if not command:
        return None
    match = _SLEEP_INVOCATION.search(command)
    if match is None:
        return None
    return (
        "blocked by dev session hygiene: a sleep-based wait is refused in an agent session. "
        + _REMEDY,
        {
            "policy": "forbid_sleep_wait_in_agent_session",
            "command": command,
            "matched_at": match.start(),
        },
    )
