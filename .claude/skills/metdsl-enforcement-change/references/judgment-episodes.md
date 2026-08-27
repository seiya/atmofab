# The three judgment rules: the episodes behind them

Moved out of `SKILL.md` verbatim (2026-08-25) so the skill body carries the rules and this file
carries the evidence. `SKILL.md` §"Judgment rules you never drop" keeps every rule and its
criterion; open the matching section here when a rule does not obviously apply, or when you want
to know what it cost.

These three hold everywhere in the procedure. Finish reading them before you open any
reference file.

**1. Before classifying anything as residual or unreachable, run the attack.** Nothing goes
into the residual bucket without a record of an attempt that failed. PR #51's only P1 (a
caller can forge a capability by naming its own `repo_root`) had been found by two reviewers,
yet was accepted for five rounds on the **unverified premise** that exploiting it needed a
primitive the file-tool hook refuses. The actual primitive was one the contract hands over
explicitly: `workspace/tmp/<agent_run_id>` (a bwrap rw bind) plus `Bash(python3
workspace/tmp/*)` (committed in `.claude/settings.json`, the dev layer, and since issue #63 in
`leaf_config/claude/settings.json`, the layer a leaf actually loads — both carry it today). **Decide by what you ran, not by who said
it.**

**1-b. Deleting a defense is also a classification.** Rule 1 is not only about triaging
someone else's finding. **When you delete a defense you wrote**, the reason is always "this
shape cannot occur", and that is the classification itself. In PR #53 I deleted a derived-type
guard I had added in that very round, judging that "F2008 forbids this arrangement", and
shipped a fail-open — the evidence was **one** gfortran probe (a host-associated binding),
while `nopass` plus **use association** makes the compiler accept the same arrangement. One
probe means "I tried one", not "I tried".

- **Do not conclude impossibility from a single counterexample.** Try at least one more
  **different spelling or different association path** for the same construct
- **"The mutation survives" is not grounds for deletion.** "There is no test" and "the code is
  unnecessary" are different claims. If it survives, the default is: keep it, and write in the
  docstring that it is not pinned
- This repo pushes the other way too ("delete dead defenses", e.g.
  `_validate_apply_patch_gate_coverage`), and that collided head-on here. **How the tug-of-war
  settles**: you may delete only when there is **an execution record of an attempt to reach it
  that failed**. "No caller exists" (dead code) counts as such a record; "the language spec
  makes it impossible" does not
- **The moment you write "the language spec makes this impossible" is the most dangerous one.
  If you write it, execute one case from that spec and confirm.** This is the second time in
  this repo. In PR #66 the import reader did not read relative imports, and the docstring
  justified it as "a relative import cannot leave its package, so the neutral core cannot
  reach a backend". In fact `tools/` is a PEP 420 namespace package
  **containing** `tools/backends/`, so a relative import such as
  `from .backends.language.fortran import signatures` **crosses the boundary without leaving the
  package** — confirmed by running it. (An earlier version of this note named a `build_system`
  module that does not exist yet, so the import that proves the point could not be executed.) Same shape as PR #53's `nopass` plus use
  association: what breaks these claims is always the language's **special form** (namespace
  packages, use association, implicit association rules). Any "impossible" about package
  structure, scope, or visibility gets one run the moment you write it

**1-c. Severity is a classification too. Do not decide it from one reproduction.** Rule 1 says
decide "does it happen" by execution; it says **nothing about how far it happens**. The moment
you can reproduce the hole in one layer is the most dangerous one — write the verdict there
and the remaining layers are necessarily inference. The procedure is: **enumerate every place
that reads that fact, then open each layer**. Skip the enumeration and each layer you open
raises your confidence while the unopened ones fall out of view.

- **Do not use a layer you have not executed in the verdict; say explicitly that you have not
  executed it**
- In PR #55 I got the severity of the `agent_role` hole wrong **three times** (medium → high →
  medium → high). Each time "I ran the reproduction", so the letter of rule 1 was satisfied —
  and I missed anyway. Opening only the producer (the manifest) gave high; opening the hook
  gave medium; opening `build_bwrap_profile` and `_validate_actual_write_paths` last showed
  **the answer was neither** — the sandbox rw-binds it, and the audit that
  `docs/AGENT_CONTRACT.md` calls authoritative never runs because of an early return
- In the same round I cited a **function that does not exist** (`_build_capability_payload`;
  the real one is `build_capability_document`). Enumerating the readers first would have kept
  that out of the first draft
- Finding the counterparts: `references/dual-read-pairs.md`. **When a fact is not in that
  table, that is exactly when you write the enumeration out**
- **Once every reader is open, enumerate what the other layers already cover before writing
  the severity.** The number of readers alone does not justify high. In PR #57 I had six
  readers open and still needed a **fourth correction**, this time toward the **safe** side:
  counting every place bwrap rw-binds showed that inside `write_roots` FS-diff containment
  permits the write **for every role**, and every rw-bind outside it was exempted as runtime
  bookkeeping ⇒ **with the sandbox active, no write became newly undetectable under an unknown
  role**. The defense is still silently dead, so the fix ships — but **do not write "this is
  exploitable"**. Conversely, skip this enumeration and the reader count alone reads as high
- **But never turn "another layer catches it" into a reason not to fix** (as with the
  `feedback_no_redundant_persistence` family, this repo does not accept leaving something
  because of a second layer). The enumeration exists to **make the severity description
  accurate**, not to decide whether to fix

**1-d. The premises of a fix you have not written yet are also a classification.** Rules 1
through 1-c are about things that **already exist** — someone else's finding, a defense you
are deleting, the severity of a hole — and say nothing about **the facts an unwritten fix
assumes**. That is where it costs most: if the premise is false, the plan, the implementation,
the tests, and the prose are all wasted at once.

- **The moment your plan says "today this shape is handled like so", execute that one
  sentence before implementing.** Docs, old logs, and issue bodies are not sources for a
  premise (each describes one observation moment, and layers change with versions)
- Hit in issue #75: on the premise that "a post-#72 leaf silently loses read commands that do
  not match the committed `permissions.allow`", I wrote a plan complete with six phases, test
  families, and mutation targets. At the start of the work, **one unbilled observation** showed
  that CLI 2.1.234 applies **its own read-only command analysis ahead of the allowlist** and
  every target form passed. No implementation was needed. **Observation costs two orders of
  magnitude less than implementation**
- **"It was not refused" cannot be shown from refusal logs.** In a layer where refusals leave
  no event, **count the traces on the success side** — here the decider was whether each
  `pre_command_execute` had a matching `post_command_execute` (evidence that it ran). The issue
  body itself proposed that cross-check, and nobody had run it
- When a premise collapses, **keep the measurements**. The plan dies; the measured facts, and
  any other hole found along the way (here, a real refusal in the opposite direction), stay

**2. Do not close an environment-dependent finding with a mock on the test side.** When told
"this test fails on a machine without gfortran", first ask **what happens in production on
that machine**. In PR #51, mocking `which` removed the environment dependence and capped a
hole where the validation rule itself was inert on machines with no compiler installed (Codex
later picked it up as a P2).

**3. Changing a rule is not done until you have swept the prose that cites that rule as
grounds.** Three rounds running, I fixed a docstring while **the violation message actually
emitted** 40 lines below stayed stale. Worse, the "measured value" I cited as grounds had been
inverted by an implementation change (the consequence of `language: " fortran"`). Use the grep
procedure in `references/verification.md`.

**3-a. When the sweep keeps losing, COUPLE the documents to the rule with a check.** Rule 3
is a discipline, and on issue #71 it failed **four consecutive rounds after it had been
diagnosed**: round 11 named "the rounds were reliable about code and unreliable about their own
record", and rounds 12 through 15 each CARRIED the same class — carried, not found: round 12 is the
narrowing commit and discovered no record defect, so "four rounds found it" would overstate what
the history shows. The worst instance: the hook
refuses a `Glob` pattern beginning with `/` or with `~`, and **five canonical statements said
"ONLY when it is ABSOLUTE"** — `~` is not absolute, which was that branch's own central
measurement — so `docs/AGENT_CONTRACT.md`, the one document EVERY leaf reads, told a leaf that
a refusal it can actually receive cannot happen.

**Reach for the pattern this repository already uses three times** rather than inventing one.
`tools/tests/test_hooks_cli.py` holds `_SCRATCH_SURFACES`, `_REDIRECT_RULE_SURFACES` and
`_SURFACES` — but they are three DIFFERENT shapes, so read the one nearest your rule before
copying it: `_SURFACES` is `(file, anchor)`; `_SCRATCH_SURFACES` is `(file, anchor, scope)` and
that third column IS the bound; `_REDIRECT_RULE_SURFACES` has no anchor at all and couples by a
phrase regex over a paragraph. **They also duplicate each other** — two near-identical
anchored-window readers with two different window constants live in that one file — so copying
is the starting point and not the goal. The four traps, each of which cost a round:

- **Anchor on text that PRECEDES the rule and is byte-identical in the wording you are
  refusing.** Anchoring on your own corrected sentence pins that the correction survived, not
  that the rule is stated — witness the check by restoring the old wording and confirming the
  failure names what is missing, not the anchor
- **Bound the reader and self-test the bound**, or a document that mentions the rule's terms
  anywhere passes on the strength of an unrelated sentence
- **Decide what "names the rule" means for THIS rule.** Couple by MEMBERS only when the prose
  names them in full — a two-element trigger, yes; `LEAF_ENV_ALLOWLIST`, never, because its
  documents state the policy ("the environment is a declared allowlist") and correctly do not
  enumerate it. Otherwise couple by POINTER (each site must cite where the constant lives) or
  by NUMBER. **The rule is defined once, IN THE CODE, and the documents are checked against it**
  — never the reverse, and never both spelled out independently, which is two spellings of one
  rule and the defect this whole section is about
- **Pin the members, not the source line.** A legitimate extraction to a named constant must
  not turn a true statement red — the exemplar `_trigger_prefixes` FAILED this when written — it read
  `pattern.startswith((…))` with the tuple inline, so extracting it to a named constant, a
  refactor that changes nothing, raised its assertion and named no repair. It resolves a named
  constant today (`tools/tests/test_hooks_cli.py`, its own docstring records the episode), so copy
  the current version. Resolve a named constant before giving up, and make the failure name the
  repair. This is the trap that is easiest to reintroduce, because pinning the spelling is
  three lines and pinning the members is fifteen

**Before adding a check, ask whether the sites should exist.** The cheaper fix is this
repository's ordinary practice — one canonical statement, everyone else cites it (`AGENTS.md`
§Dedicated rule documents) — and it cannot rot. Coupling is for the sites that must repeat the
rule anyway: a leaf-read contract has to be self-contained, and a refusal message has to say it
to whoever was refused. Note this is NOT surface 5's twin, though both count to three: surface
5 changes the CHANNEL a decision travels on so the decision stops being forgeable, and 3-a adds
machinery so that many statements of one rule stay honest. Different question, same threshold.

**The trigger is the count; the audience is the priority.** Three or more statement sites is
when discipline has already lost. That one of them is read by a leaf or an operator does not
lower the count — it decides how soon you do it, and which site you check first.

**Sites a test cannot reach are real and the check does not cover them**: a commit message,
which cannot be edited once pushed, and a prompt assembled at runtime. For those the only moves
are to remove the statement or to make it derived; say in the commit which sites you could not
couple. **Check before assuming a site is out of reach** — an issue or PR body can be edited
(`gh issue edit --body`), and `docs/examples/*.yaml` was untested and permanently drifting until
`tools/tests/test_llm_config.py` closed it, so citing that as a live example of the unreachable
sends a reader past a site that is already coupled.

**The flip side of 3: prose you newly write in that same commit is also unverified until you
run it.** Read rule 3 as being about old text and you keep only half of it. In L128 I got
**six newly written measurements or citations wrong inside the fix itself**:

- The suite count three times (the first written blind; the second still off by one after I
  wrote "re-measured")
- A perf ratio twice (off by 3.5x against the raw baseline, then quoting a single point while
  ignoring directory dependence — **write a range when the number varies**)
- A lint rule id (of fortitude's: the one that enforces `implicit none` is C001; **C003 fires on
  nothing the gate reads** — when this episode happened the phase doc had the leaf suppress it,
  and since issue #111 it is out of the declared rule set — so the citation pointed at a check
  that never fires)
- **Numbers that were right rot when the branch moves.** In PR #53 I got the suite count wrong
  twice; the second time it was **correct when written** and was obsoleted by a later round
  adding tests. **Keep a list of every place you wrote a number and re-measure them together at
  the end** (a commit message cannot be fixed, so either mark the number in TODO.md or the
  docstring as "measured at this point" or rewrite it in the final round)
- Attribution of the residue (I put all three candidates on the producer side; one was on the
  consumption side, which would have sent the next person at half the problem)
- **Do not write someone else's measurement as your own.** This flip side is about "prose I
  wrote is unverified until run", but the path most often missed is **a number sourced from a
  reviewer**. In PR #55, commit `0d444c2` reproduced a reviewer's "30 commits, 26 of them DONE"
  verbatim; measuring it myself gave **71 / 42 / 6** — all wrong. Cite the source explicitly,
  or re-measure before writing

**Right after you write a sentence, execute it.** Numbers, rule ids, compiler diagnostics, and
"X catches this" are all executable claims. If you have not run it, do not write it.


## Appendix: a pin can be true via the OTHER rule in the same message (PR #76)

Moved from `SKILL.md` §4 "Tests pin properties", which keeps the rule and the decision procedure.


- **If one string states two rules, a substring pin is necessarily true via the other one.**
  This repo's remedy, hint, and contract texts fold **two rules into one message** ("write
  artifacts like this, write scratch like that"). Aim `assertIn("Write tool", reason)` at that
  and rule A's sentence satisfies rule B's pin. PR #76 hit this **four times on one branch**:
  `WRITE_HINT` (the artifact sentence also contains "Edit/Write tool", so emptying the temp
  sentence stayed green) / the positive half of the document-inspection test (**699 characters
  later on the same line**, the artifact sentence) / the managed-artifact refusal (green against
  `origin/main`'s "Bash may only write scratch" wording) / one more. All four times I wrote
  "fixed" and it recurred in a different shape the next round
  - **The remedy is the same every time: split on the half the rule governs, then read**
    (`WRITE_HINT.split("For temp files")[1]` / `reason.split("allowed_tmp_root")[1]`)
  - **Decision procedure**: before writing a pin, read the whole message and count whether the
    same word is used in the explanation of another rule. One use and the substring pin does not
    hold
  - **Confirmation runs to "the mutation makes it fail".** Here too, run it against
    `origin/main`'s wording and see it actually fail — "I asserted the new wording, so it is
    pinned" is inference

