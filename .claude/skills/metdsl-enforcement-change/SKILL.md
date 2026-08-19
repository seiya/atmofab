---
name: metdsl-enforcement-change
description: Use when changing this repository's enforcement machinery — the MCP capability gate, validators, hooks, capability / write_root derivation, and the input validation a gate performs — and whenever you are about to classify a review finding as 「残余」「到達不能」「対象外」 (residual / unreachable / out of scope). Required reading for fixing a fail-open, adding to an allowlist or denylist, touching the gate functions in `mcp_servers/build_runtime_server.py` / `tools/orchestration_runtime.py`, `tools/hooks/`, or the gates in `validate_pipeline_semantics.py`, for fixing an audit finding, and for triaging a subagent's or Codex's review findings.
---

# Changing met-dsl's enforcement machinery

What this skill holds is the **traps specific to the enforcement domain**: dual-read pairs,
failure attribution, verification commands, and the judgment rules you never drop. **How to
run a review** — round structure, exclusion lists, when Codex enters, convergence, mutation
checking — is owned by `metdsl-review-loop`, so start that one too once you reach the review
stage.

Fixing enforcement machinery in this repository carries a high probability that **the fix
itself introduces the next defect**. In PR #51 (two high audit findings), 17 subagent review
rounds plus 3 Codex passes surfaced 15 defects that the fixes had introduced. What follows is
a procedure against those recurring shapes; it does not restate the rules themselves (the
canonical sources are `mcp_servers/README.md` / `docs/HOOKS.md` / `docs/AGENT_SKILLS.md` —
copying them here would add one more twin document, which is itself the defect class this
skill exists to kill).

## Judgment rules you never drop

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

**The flip side of 3: prose you newly write in that same commit is also unverified until you
run it.** Read rule 3 as being about old text and you keep only half of it. In L128 I got
**four newly written measurements or citations wrong inside the fix itself**:

- The suite count three times (the first written blind; the second still off by one after I
  wrote "re-measured")
- A perf ratio twice (off by 3.5x against the raw baseline, then quoting a single point while
  ignoring directory dependence — **write a range when the number varies**)
- A lint rule id (of fortitude's: the one that enforces `implicit none` is C001; **C003 is the
  rule the phase doc has the leaf suppress**, so the citation pointed at a check that never fires)
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

## Procedure

### 1. Inventory the surface before fixing

Enumerate, at the argument-name level, every caller-supplied input that reaches **exec / env /
argv / the filesystem / the paths from which the gate reads its evidence**. "Plugged one, the
neighbour was open" is the most frequent recurrence (env allowlisted → argv was open → the
value was open → the auto-discovery path was open: four in a row).

Leave the enumeration in the commit message or TODO.md. The next round's reviewer will come
looking to break it.

**When the gate reads the source text rather than the meaning of an input (validators and
parsers), the surface is a different one.** The exec/env/argv surface above is the MCP
capability gate's and barely applies to the Fortran-reading gates in
`validate_pipeline_semantics.py`. What you inventory there is **the spelling variation the
language permits**:

- **Keywords are not reserved words.** A variable may be named `module` / `parameter` /
  `contains` / `endmodule`. Every rule that treats "a statement starting with a keyword" as
  structure breaks on this
- **The space in a two-word keyword is sometimes optional** (F2008 Table 3.1). `selecttype` /
  `endsubroutine` / `doubleprecision` are legal as one word. Sweep every `\s+` you wrote (some
  forms such as `abstractinterface` are not legal, so **ask the compiler** to settle each one)
- **`::` may be omitted** (`integer ncomp`, `public ncomp`, `enumerator red`). Check that the
  two spellings of one statement are not treated differently
- **A statement label may precede any statement** (`10 contains`, `100 use m`, `20 subroutine
  f(x)`)
- Attribute-bearing statements have 18 forms without `::` (`common` / `dimension` /
  `equivalence` / `data` / `namelist` / bare `pointer` …). **Writing out each grammar is the
  losing line** — close them all at once by inverting the polarity: do not parse statements
  that start with a keyword; take every identifier that appears in them to the safe side

This checklist names concrete spellings of one target language, which is knowledge
`docs/BACKEND_BOUNDARY.md` keeps out of the neutral core. It is here because the gates it warns
about are in `validate_pipeline_semantics.py`, and it goes when the source-reading area on the
TODO ledger goes. Two things about that are worth stating rather than implying. Nothing measures
this file: `.claude/skills/**` matches none of the scanner's globs, so the ratchet that bounds this
kind of growth elsewhere does not read it at all. And the debt is **new to the repository**, since
until this branch these files lived in one operator's home directory. Most of it is not in this
checklist, either: the majority of the sampled tokens under `.claude/skills/` are in episodes and
identifier names, in both skills, and `metdsl-review-loop` carries some while having no checklist
at all. TODO.md's development-documentation entry holds the measurement and the command that
reproduces it — do not quote a figure from here, because every edit to these files moves it.

When a rule derives its safety from an enumeration, **write a test that kills each element of
the enumeration by mutation** (round 0 in `metdsl-review-loop`). A missing element shows up in
no other test.

**Surface 5: whether caller-controlled data is mixed into the classification channel the
verdict reads.** There is one surface that is none of exec / env / argv / FS / evidence paths,
and none of the spelling variation. **The values that classify and route a gate failure**
(marker strings, failure_category, reason, excerpt) — **where are they read from**? If they
come from scanning output text, and that text embeds data whose content a caller decides
(**file names, identifiers, or paths the leaf chose**), the classification is forgeable.

L174 was broken three rounds running. The gate scanned the validator's stdout for
`[fortran-structure-unavailable]` and, on finding it, classified the failure as "this machine
has no parser" = terminal. But violations embed **a file name the leaf chose**, in the form
`f"{model_file}: ..."`. Writing a model named `[fortran-structure-unavailable]_model.f90` was
therefore enough to turn a naming slip into "machine failure", and **a failure that should have
been warm-retried killed the whole run**.

Each fix narrowed the scan, and **each was broken by one byte**:

| Version | Test | How it broke |
|---|---|---|
| 1 | marker anywhere in the output | `[marker]_model.f90` |
| 2 | marker at start of line | `x\n[marker]_model.f90` (newline in the path) |
| 3 | `- ` + marker at start of line | `x\n- [marker]_model.f90` |
| 4 | **exit code** | unbreakable (the leaf cannot write it) |

**Rule**: if a classification comes from scanning text, check whether caller-derived data
enters that text. If it does, **change the channel rather than narrowing the sample** — exit
code, exception type, a dedicated field, a sidecar. Each of these is written by the side that
knows and cannot be written by the caller. This is the classification-channel version of
`metdsl-review-loop`'s "when a pin keeps being broken in a new shape, move the definition to
one place".

The same shape usually exists several times in the same repository (in L174 the twin survived
on `[stale-dependency-ir]`, and two further sites — post_execute and pre_judge — **scanned
neither marker**). Once you find one, **count every site that makes the same decision**. At
three or more, change the channel design instead of fixing them individually.

**Surface 6: if the check tells the reader "do this to fix it", what else does that remedy
rewrite?** If surface 5 is the read-side question, this is its write-side twin. Wherever a
ratchet, baseline, or allowlist says "on failure, run this command to update", check that the
command does not also rewrite the pin.

In PR #66 the failure message for a sample (a token-count baseline) instructed the reader to
run `--write-baseline`, and that command wrote **both the sample and the pin** (the bypass
import allowlist). A pin described in three places as "a module removed from the list cannot
quietly come back" **was regenerated by following the instructions**. Regeneration is not rare:
a commit that removes a single token demands it, so normal operation hits it.

It then **recurred in three stages**: separating the allowlist into its own file → now the
**scan range** was washed by the same path (the range was only visible through the baseline) →
pinning the range and the class **name** → now the class's **branches** were washable (dropping
`\bgcc\b` together with its probe removed 13 occurrences and went green after one regenerate).

**Rule**: never let a pin and the command with authority to loosen it **live in the same file
or the same procedure**. And pin the rule, not the result the rule produced (pinning results
makes ordinary work fail and teaches the habit of regenerating without reading the rule).

**Surface 7: when you say you closed a configuration surface, what else does that tool read
besides configuration files?** The six above are about inputs reaching a gate; this one is
about **the side that launches an external tool**. **A flag that narrows configuration sources
(`--setting-sources`, `--config`) governs configuration files and nothing else.** Whatever else
the tool reads — auto-injected memory, environment variables, files auto-discovered from the
cwd, remote configuration fetched at startup — stays outside the flag.

Hit for real in PR #72: I wrote that `claude --setting-sources project` closed the leaf's
configuration surface, but **auto-memory is not a configuration file**, so the operator's
`~/.claude/.../memory/MEMORY.md` **kept being injected into the leaf's first user message**
(past PRs, open issues, standing instructions to the operator). Environment variables
(`ANTHROPIC_BASE_URL` / `ANTHROPIC_MODEL`) passed through just as freely, so a different model
can be run with every flag set. **The doc that said "closed" made the reader read closure over
things that were not closed.**

- **Do not conclude from the flag. Capture what the process actually received and count it.**
  For an LLM CLI, point `ANTHROPIC_BASE_URL` at a local HTTP server, save the request body and
  return 400 (**unbilled**, and it yields both the injected strings and the byte delta).
  "Given what the flag means, that cannot reach it" is the same inference shape as rule 1's
  "impossible"
- **Look only at paths that are already closed and you will never see it.** This hole stayed
  invisible for so long because the pure leaf was closed from the start **by a different flag**
  (`--safe-mode`). **When several paths claim closure, observe each path** — one being closed
  for another reason structurally hides the hole in the other
- **The checkout you observe from is itself a confounder. Until you take a control, you cannot
  say which way it falls.** A development checkout carries an uncommitted
  `.claude/settings.local.json` (untracked; if it is in a global gitignore it does not even show
  in `git status`), so when observing the permission layer **take a clean `git worktree`
  checkout as a control**. Here both gave the same verdict, and the three entries that exist
  only locally (`Bash(patch *)` / `Bash(jq --version)` / `Bash(fortitude --version)`) all three
  came back `This command requires approval` even in the development checkout, which lets me
  write that **`--setting-sources project` does not read `settings.local.json`**. **The
  inference at the start of the observation was the opposite** — I read it as "it was allowed
  because local is in effect". Without the control, that error would have become the
  conclusion. **Do not decide from one entry**: with a single probe you cannot distinguish it
  from "that spelling simply did not match a prefix rule"
- **The same harness measures verdicts, not just injection.** Return a synthetic `tool_use` in
  the first response and read the `tool_result` in the second request body: that is **the
  permission layer's actual verdict**, unbilled. **Mix in one control that must be refused, to
  show first that the layer is alive** — without it, "everything passed" is indistinguishable
  from "the layer was dead"
- **A closure claim names the surface it closed.** Not "closed the configuration surface" but
  "**closed the configuration-file surface**; environment and auto-memory are separate and
  remain open". Drop the surface name and the reader takes the widest meaning

**Surface 8: the place you created for isolation is an input to that tool.** If surface 7 is
"what stays outside the flag", this is the shape where **what you built to make the flag
effective** becomes a new input surface. Create a private configuration dir to confine a tool,
make it writable, and **everything that tool reads from that dir** is a wholly new injection
surface.

Hit in issue #63. To cut the leaf off from the operator's `~/.claude`, I created a
per-orchestration private `CLAUDE_CONFIG_DIR` and bound it rw. But under `--setting-sources
user` that dir's `CLAUDE.md` is injected as "the user's personal global instructions", and its
`agents/*.md` join the subagent roster (both measured). Worse, **one home is shared by every
leaf in the run**. So a `generate` leaf that writes `<home>/CLAUDE.md` has the later
`validate.judge` leaf read it as instructions — **a forged certification with zero trace in the
artifacts**. Under `--setting-sources project` the same file was inert, so **the change made to
close a path opened a new one**.

- **Check**: once you create an isolation target, enumerate what the tool **reads** from that
  dir, separately from what it **writes**. The write enumeration (a state allowlist) is about
  availability; the read enumeration is about security. You need both
- **Polarity**: block the read side with a denylist and any name the tool adds in its next
  version becomes a silent hole. **Bind the dir ro and allowlist only the writable places**, so
  unknown names fall on the inert side. Issue #63's allowlist was built **by measurement** (run
  one agentic leaf and diff the tree). The first version, measured with a tool-less leaf, missed
  2 of 6 — **the allowlist is short unless the kind of leaf you measure matches production too**
- **Measure and state the cost of the polarity**: going ro breaks whatever the tool writes
  atomically (the lock-dir plus temp-file pattern directly under the home). In issue #63 the
  `.claude.json` update fails while transcript, resume, and MCP calls survive — **measured**, then
  accepted. Do not settle for "it should not break"
- **Decide shared vs per-leaf first**: the severity above comes entirely from one home being
  shared by every leaf. Split per leaf and the injection surface closes on itself. If you choose
  sharing, write into the design the sentence that **a write to the shared object is an input to
  the other leaves**

### 2. Confirm the path production actually takes

**Production does not necessarily pass the argument you wrote the rule on.** In PR #51 the rule
went on `run_syntax_check`'s `sources` argument, and the workflow never passes `sources` (the
auto-discovery side went straight through).

- Read the conductor's in-process calls for real (`_build_inproc` / `_gate_lint_check` /
  `_gate_syntax_check` / `_execute_inproc`)
- Confirm the **position** of the check too. Placed after a skip or an early return, it is inert
  under that condition
- Check whether two places read the same fact, via `references/dual-read-pairs.md`

**There are two kinds of position. Look only at control flow inside the function and you miss
one.**

- **Inside the function**: is it after the skip / early return (above)
- **Inside the pipeline**: **does it run before the side that reads the value?** PR #57 put the
  normalization in the "right function", but `record_launch` called
  `prepare_launch_request_payload` (which **renders** the prompt, and the rendering reads
  `agent_role`) **first**. The normalization ran 15 lines later, **the symptom did not go away**,
  and it newly created an **audit-trail mismatch**: the persisted request normalized, the prompt
  next to it rendered un-normalized. Worse than before the fix
- **There is one way to find this. Do not poke the validator in a unit test; drive the
  production entry point end to end and assert on the final product** (the rendered prompt, the
  persisted JSON). PR #57's first test called `_build_task_card` itself, so it went green while
  nobody looked at the prompt that actually ships

### 3. Decide the failure's attribution

Every refusal you add gets routed to someone as their fault. The criteria and the known
branches are in `references/failure-routing.md`. The essentials: **what the leaf can fix is a
content failure; what the conductor, the IR, or the environment caused is a transport
fail_closed**. Name exceptions by type; do not catch at the width of `except ValueError` (that
burns the leaf's retry budget on the conductor's own defects).

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
  the Codex section of `metdsl-review-loop`)

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

### 4. Tests pin properties

- **Do not transcribe measured values.** A test asserting "`language: null` results in dual
  ownership" became **a pin that rejected the correct fix** the moment the implementation moved
  to structural reading. If you use a measured value, compute it from the code inside the test
- **Pin at the handler, not the helper.** Deleting the caller's wiring and staying green
  actually happened (4 of 5 sites)
- **Keep one test that pushes a production payload through the real validator.** The conductor's
  tests mock each tool function, so they never traverse the validation layer
- **Do not call a sample a pin.** A test placed **outside** the place that defines the set
  cannot claim set identity — it can only sample rejections. **Write in the docstring what is
  pinned and what is sampled.** In PR #55 all three rounds said "pinned" and all three were
  broken (a three-name denylist → a substring → "a file, not a directory"). If the predicate has
  several branches (`==` / `startswith` / trailing slash), **each branch needs its own probe**

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

### 5. Run the mutation check before committing

Revert your fix one hunk at a time and confirm the tests **fail**. In PR #51 the survivors among
**42 mutants**, and the tests passing for the wrong reason, were things I learned only when a later
round pointed them out.

The procedure and the script are **owned by `metdsl-review-loop`** (`scripts/mutation_check.py`).
The same thing runs before review, so read its "Before you hand it over (round 0)".

### 6. Verify and record

Run the procedures in `references/verification.md` (suite baseline, ruff diff against
origin/main, doc size ceilings, end-to-end through a real server process, the prose grep) and
write the measured values into the commit or TODO.md. **Do not write an assertion you have not
measured.**

### 7. Decide whether this skill itself needs updating

At the end of the work, judge whether **this session made this skill's content stale, or exposed
a gap it should fill**. Do not stop at the judgment: when you decide something is needed, **tell
the user** (do not rewrite silently — what changes and why is material for their decision).

Signs to look for:

- **You created a new dual-read pair, or resolved an existing one** → the table in
  `references/dual-read-pairs.md`
- **You hesitated over failure attribution, or it fit no existing category** →
  `references/failure-routing.md`
- **A verification step was missing, or a command had gone stale** → `references/verification.md`
- **The mutation check gave a false positive, or missed something** →
  `.claude/skills/metdsl-review-loop/scripts/mutation_check.py`
- **The skill did not fire when it should have, or fired when it should not** → `description`
- **You broke one of the three judgment rules while following the procedure** → SKILL.md itself.
  Distinguish "the rule was missing" from "the rule was there but its trigger point was
  elsewhere"
- **A trap not written here consumed your time** → a candidate for addition. But check that it
  **is not a copy of a canonical source** (rule content belongs to the repo's docs; only the
  procedure and traps written nowhere else belong here)
- **Memory** (`feedback_enforcement_change_skill.md`) is a pointer only. Wanting to add content
  there means the skill is what needs updating

If you judge that no update is needed, say so in one line. The judgment itself is information.

## When triaging review findings

Use this skill even when you have not touched code. Judgment rule 1 is what matters at this
moment.

- Before sorting a finding into real / false positive / residual, run the reproduction
- Treat "the implementation is right but the test is weak" as real (PR #51's surviving mutants
  came from here)
- When a finding lands inside the previous round's fix, that is not a coincidence. **Look
  hardest at the files you just fixed in the next round**
