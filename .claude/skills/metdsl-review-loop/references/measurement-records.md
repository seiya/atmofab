# Recording a measurement so it cannot rot

Moved out of `SKILL.md` verbatim (2026-08-25). `SKILL.md` round 0 item 2 keeps the four rules;
this file keeps the episodes that produced them.

2. **Do not type measured values by hand; generate them from the artifact you measured.** Keeping
   "a list of every place you wrote a number, re-measured at the end" is not enough — in PR #66 I
   **mistyped it immediately after measuring** (measured suite 4679, wrote 4680). Seven numeric
   errors appeared on that one branch, the last of them in the PR body's disclosure section. What
   worked was **a script that substitutes the numbers in TODO / docs from the measurement
   artifacts**, run and diffed. Leave no path where a human transcribes.

   **"Re-measure at the end" fails outright once a review loop starts, because the end keeps
   moving.** On TODO:269 one paragraph rotted **three times, twice inside a commit whose stated
   purpose was to restate it** — each restatement was correct when written and was falsified by
   the next round's own fix commits, and reviewers reported the same two numbers in three
   consecutive rounds. Note the shape: a fix commit adds a test or a hunk, so the very act of
   answering a review finding invalidates the ledger that describes the branch.

   - **The form is the bug, not the numbers. Write every measurement as a HISTORICAL RECORD that
     names the commit it was taken at** ("at `7c6d187`: 17 hunks, 10 killed …"). A record cannot
     go stale; a claim about the present can, and will, once per round. A reader who needs today's
     figure runs the command.
   - **State the PROPERTY the count stands for, next to the count** ("every behavioural hunk is
     pinned"). That is the part the reader needs and the part that does not move — and it is what
     survives when the number is obsolete anyway.
   - **A count with no unit is not reproducible.** "14 hunks" was reported as 19 and as 16 by two
     reviewers before anyone noticed the figure depends on the diff CONTEXT WIDTH (`-U0` / `-U3` /
     `-U5` gave 21 / 17 / 15 on one range). Name the width, the command, and the exclusions.
   - **Recording that a number "was right when written" does not stop the next rot** — that
     sentence itself was written twice and rotted twice. Only changing the form stopped it.
