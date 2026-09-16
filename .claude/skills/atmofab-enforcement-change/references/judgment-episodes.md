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

- **Arithmetic on an inherited count (issue #178, PR #221, 2026-09-15).** `_load_spec_catalog`'s
  comment said "All three production call sites (A, B, C) only invoke this function AFTER
  deps.yaml entries are confirmed non-empty". The branch deleted B and rewrote it to "Both
  production call sites (A, C)". A round-1 reviewer ran the grep: six callers before the branch,
  five after. The "three" had never been true and the "both" inherited it — a decrement is a
  claim about the present made from a number nobody had measured, and it feels safer than
  writing a fresh number because it looks like a smaller edit. The property the sentence was
  protecting ("a leaf orchestration never reaches this raise") held for all five, which is why
  the fix replaced the count with the property and no number. **The tell: a diff whose only
  change to a numeral is `n-1` beside a deletion.** Re-measure, or drop the number.

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

**Issue #175 added four more traps, and how they were found is the point.** The rule fired for
real: one fact — where a node's `direct_deps` comes from — was stated on ELEVEN lines across
SEVEN files (of eight the scan covers; one carries none), six of the seven read by a compile
leaf, and FIVE consecutive review rounds each found a
different copy still stating the fact the change had reversed. Four of those copies were text a
leaf acts on, each costing that leaf a `Compile fail` on every attempt. So a coupling check was
written. **A witness census run against it the same day found four defects in it**, three of them
by PLANTING the very defect the check exists to refuse and watching it pass:

- a statement written across TWO LINES, which is what hard-wrapped Markdown and the prompt
  templates are made of, and the reader was line-local;
- an allowlisted line copied VERBATIM into a second surface, free because the key was the
  digest alone — and the copy is exactly the operation that produced the eleven sites;
- a false statement planted in a `SKILL` after renaming it, because the surface derivation
  guarded with `.is_file()` and silently scanned one file fewer;
- and the set comparison's STALE half, replaced with `[]` and still green, because only the
  unread direction is exercised by an ordinary run.

The lesson under the four is the one this whole section keeps restating: **the check you write to
stop a class of defect is written by the same hand that produced the class**, so it needs the same
treatment — plant the defect, not just the mutant. Every fix above is witnessed by planting.

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

## Rule 1-b: a test class deleted by its name carried five live pins over fourteen mechanisms (issue #170, 2026-09-15)

Issue #170 replaced the `--wait-usage-reset` reset-instant scrape and `/usage` probe (13
functions, 17 constants) with a fixed-schedule wait on the `llm_usage_limit` tag. The plan listed
the test classes to delete as "direct tests of deleted functions", and the first commit deleted
six of them (64 rows) under that label, `UsageLimitTerminalLineStreamTests` among them. That
class was named after `_terminal_usage_limit_line`, a deleted function, and its docstrings were
about the arming pattern, also deleted.

**What the rows actually observed.** Their FIRST assertions drove `_classify_leaf_infra_error`
and `_USAGE_LIMIT_INFRA_PATTERN` — live code the branch kept, and since #170 the ONLY thing
between a quota death and a multi-hour sleep — and only their tails called the deleted arming
function. `test_arming_implies_the_classifier_would_tag` asserted the classifier over a 4 leads ×
6 windows × 5 cues matrix before it checked the implication its name is about;
`test_every_wording_the_classifier_tags_here_also_arms` did the same over 8 × 12. With the class
gone, dropping the `"result":"` envelope prefix, or `weekly` from the window list, or the
`resets 18:00` / `in N hours` / weekday / `tomorrow` cues, or the bare `session limit`
alternative, left every suite green; on origin/main each of those mutants was red in exactly
one deleted row.

**What it cost**: three rounds, and the same shape each time. Round 1's correctness axis found
three rows (mutants P / Q1 / Q2) and the fix commit said "three of its rows pinned live code";
round 2's security axis found ten more mechanisms behind one more row (the matrix) and said the
correction was itself incomplete; round 3's disclosure axis found the relative-time unit cues,
the first half of one more row. Five rows, fourteen mechanisms (3 + 10 + 1), restored in three
commits (031f2093, 3a14ca84, 182c2ca9). Nothing in the loop found them
except reviewers running origin/main's rows against HEAD's code — the sweep mutates HEAD, the
census enumerates HEAD, and the branch's own new rows all read the constant back. The
"deleted TEST" form of rule 1-b was not written anywhere: 1-b speaks of deleting a defense, and
a test class named after a dead mechanism did not read as one.

**What closed it**: classifying the rows by what their bodies call — `ast` over the deleted
functions, cross-referenced against the names live at HEAD — which the round-3 reviewer did in
one pass and which would have taken the author ten minutes before the first commit. The kept
halves live on in `TransportFailureTest` as classifier-only pins, each witnessed by the mutant
that had survived.

**Corollary for the RECORD.** The first correction's count ("three") was written from what one
round had found, not from a classification of the class, so it was wrong in the same way the
original label was — a smaller under-count. A count of restored rows is a claim about the whole
class; take it from the AST diff, not from the round.

## Rule 3-a: one rule, two forms, one cell — and the count wrong for the third time (issue #180, 2026-09-10)

Issue #180 retired a gate and, in doing so, hand-edited the row of `docs/CLI_REFERENCE.md` and
the `run-gate` help string that both restate `validate_pipeline_semantics`'s `--stage` set. A
review round found the help teaching `plan`, a stage argparse rejects, while the document had it
right — two restatements that had drifted apart from each other with nothing comparing them.
Three statement sites is rule 3-a's trigger, so the fix coupled both restatements to the
validator's argparse `choices`, read by driving the CLI with a value it must reject.

**The coupling covered half the rule.** Each restatement says the set TWICE:

    'stage': 'compile|post_generate|post_build|post_execute|pre_judge|full'   <- the alternation
    'ir_ref': 'workspace/ir/...'(compile stage)                              <- the qualifier

The check read the alternation. A later round set BOTH qualifiers to `(plan stage)` — the exact
defect the class exists for, `plan` and all — and the file stayed at `6 passed`. The second
statement was never coupled, and it does not read as a second SITE: it sits in the same cell,
one clause along.

**What closed it**: finding the code that answers the second form, rather than widening the
regex. The validator itself says which stage takes `--ir-ref` — run each declared stage bare and
exactly one violation reads `requires non-empty --ir-ref` — so the qualifier is compared against
a derived value, with the derivation self-tested (exactly one stage must match, or the row fails
naming the reworded violation as the cause). Witnessed with the round's own attack: both
qualifiers set to `plan` redden both rows here and leave the old form green on the same tree.

**Cost**: one round, on a check written to prevent exactly this, three commits earlier.

**And the same round broke the rule a second way, in the sentence that counts these traps.**
Adding the bullet made eleven; the prose still said "ten", exactly as it had said "four" over
five bullets before issue #143. That sentence has now been wrong twice, both times by ADDITION,
both times caught by a reviewer rather than by the author who added the bullet — which is why it
now carries an instruction to re-count, and why the honest description of rule 3-a's own
enumeration trap is that it has fired ON ITSELF three times. Nothing compares the number to the
list; a check that did would be four lines, and the reason there is not one is that this file is
prose a person reads, not a surface a leaf acts on.

**The generalisable tell**: a restatement that reads as a SENTENCE rather than a list — a
qualifier, a "(defaults to …)", a "…, which is what the gate runs" — sitting beside the
machine-shaped form you coupled. The machine-shaped form is the one you notice; the sentence is
the one that stays free.

## Rule 1-d: a premise executed on the accepting layer only (issue #235 / PR #236, 2026-09-16)

`execution_trace.json` sat in the `required_evidence[].artifact` enum with no Generate contract
producing it, so an IR that chose it passed `Compile.static` and failed closed at
`Validate.execute` after a full Compile/Generate/Build. The plan retired the token and, following
rule 1-d, EXECUTED its premise before implementing: "an enumerated string input needs no evidence
form of its own, because a JSON string is `scalar` to the snapshot shape gate" —
`_infer_json_shape("flat") == []` and `_shape_matches_expr("scalar", [])`, both true, both run in
the planning session and written into the plan as verified.

**What the run did not touch.** The premise is about a VALUE moving between two layers, and the
run drove the layer that ACCEPTS it (the post_execute shape gate) and not the one that PRODUCES it:
`docs/workflow/CHECKS_MODULE_CONTRACT.md`'s snapshot getters return numbers, the host-rendered
runner boxes a rank-0 variable through the harness's real emitter, and the harness publishes no
string emitter. So the remedy the fix appended to the refusal — leaf-read, inlined whole into both
compile prompts — told the compile leaf to route an enumerated input as a string-valued scalar
snapshot variable, a shape the validator would accept and no runner could emit. That is the class
#235 exists to close (a compile-side contract naming a capability with no producer), reintroduced
by its own fix, in the sentence written for exactly that case. The issue body carried the same
premise, and rule 1-d already says an issue body is not a source for one; what it did not say is
that ONE execution is not enough when the premise names two layers.

**How it was found**: round 1's correctness axis, by driving `render_runner` on the hand-routed IR
and reading the emitted getter — not by any instrument in the loop (the sweep, the census and the
blank-slate reviewer all compare HEAD against itself and were green). Cost: one round and a
Medium, plus a second round to un-narrow the corrected sentence (it had then said every per-case
value IS scalar).

**The rule**: a premise about a value names the layer that produces it and the layer that
accepts it; running one is not running the premise. Rule 1-c's "enumerate every reader, then open
each layer" is the same instruction one rule over, and it did not fire because 1-c is written for
SEVERITY (things that already exist) while this was a plan (1-d) — which is why the bullet now sits
under 1-d. The producer/consumer pair for the raw-evidence vocabulary is now a row in
`references/dual-read-pairs.md`.

