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

**This file carries the rules; `references/` carries the episode each rule came from.** A rule
here that does not obviously apply to your case is answered in its reference file, not by
guessing.

- `references/judgment-episodes.md` — what each judgment rule below cost, in full
- `references/input-surfaces.md` — surfaces 5-9, the marker-narrowing version table, the recipes
- `references/source-text-surface.md` — the spelling variation a source-text-reading gate must survive
- `references/dual-read-pairs.md` — the table of facts two layers read
- `references/failure-routing.md` — attribution criteria, the known branches, and remedy wording
- `references/deterministic-substep-wiring.md` — wiring a conductor in-process substep
- `references/verification.md` — the verification commands and the prose grep procedure

## Judgment rules you never drop

These three hold everywhere in the procedure. Finish reading them before you open any
reference file. Episodes for all of them: `references/judgment-episodes.md`.

**1. Before classifying anything as residual or unreachable, run the attack.** Nothing goes into
the residual bucket without a record of an attempt that failed. **Decide by what you ran, not by
who said it** — PR #51's only P1 was accepted for five rounds on an unverified premise, after two
reviewers had found it.

**1-b. Deleting a defense is also a classification.** When you delete a defense you wrote, the
reason is always "this shape cannot occur", and that is the classification itself (PR #53 shipped
a fail-open that way, on the strength of **one** compiler probe).

- **Do not conclude impossibility from a single counterexample** — try at least one more
  **different spelling or different association path** for the same construct
- **"The mutation survives" is not grounds for deletion.** "There is no test" and "the code is
  unnecessary" are different claims. If it survives, keep it and write in the docstring that it
  is not pinned
- This repo pushes the other way too ("delete dead defenses"). **How the tug-of-war settles**:
  you may delete only when there is **an execution record of an attempt to reach it that
  failed**. "No caller exists" (dead code) counts; "the language spec makes it impossible" does not
- **The moment you write "the language spec makes this impossible" is the most dangerous one. If
  you write it, execute one case from that spec and confirm.** What breaks these claims is always
  the language's **special form** (PEP 420 namespace packages, use association, implicit
  association rules). Any "impossible" about package structure, scope, or visibility gets one run

**1-c. Severity is a classification too. Do not decide it from one reproduction.** Rule 1 says
decide "does it happen" by execution; it says **nothing about how far it happens**. The procedure
is: **enumerate every place that reads that fact, then open each layer** (PR #55 got one severity
wrong three times, each time having run a reproduction).

- **Do not use a layer you have not executed in the verdict; say explicitly that you have not
  executed it**
- Finding the counterparts: `references/dual-read-pairs.md`. **When a fact is not in that table,
  that is exactly when you write the enumeration out**
- **Once every reader is open, enumerate what the other layers already cover before writing the
  severity.** The number of readers alone does not justify high — and the correction runs both
  ways (PR #57's fourth correction was toward the safe side: the defense was silently dead, so
  the fix shipped, but **"this is exploitable" was the wrong thing to write**)
- **But never turn "another layer catches it" into a reason not to fix.** The enumeration exists
  to **make the severity description accurate**, not to decide whether to fix

**1-d. The premises of a fix you have not written yet are also a classification.** Rules 1
through 1-c are about things that already exist. If an unwritten fix's premise is false, the plan,
the implementation, the tests and the prose are wasted at once (issue #75: a six-phase plan died
to **one unbilled observation**).

- **The moment your plan says "today this shape is handled like so", execute that one sentence
  before implementing.** Docs, old logs and issue bodies are not sources for a premise
- **Observation costs two orders of magnitude less than implementation**
- **"It was not refused" cannot be shown from refusal logs.** In a layer where refusals leave no
  event, **count the traces on the success side**
- When a premise collapses, **keep the measurements**. The plan dies; the measured facts stay

**2. Do not close an environment-dependent finding with a mock on the test side.** When told
"this test fails on a machine without the compiler", first ask **what happens in production on
that machine** (PR #51: mocking `which` capped a hole where the rule was inert on machines with
no compiler installed).

**3. Changing a rule is not done until you have swept the prose that cites that rule as grounds.**
Use the grep procedure in `references/verification.md`. **Right after you write a sentence,
execute it**: numbers, rule ids, compiler diagnostics and "X catches this" are all executable
claims, and **write a range when the number varies**; and **the flip side of rule 3 is that prose you newly write in the same commit is
unverified until you run it** (L128 got four freshly written measurements or citations wrong
inside the fix itself). **Do not write someone else's measurement as your own** — cite the source
explicitly, or re-measure before writing. **Keep a list of every place you wrote a number and
re-measure them together at the end**; a commit message cannot be fixed afterwards, so either
mark the number as measured at a point or rewrite it in the final round.

**3-a. When the sweep keeps losing, COUPLE the documents to the rule with a check.** Rule 3 is a
discipline, and it failed four consecutive rounds on issue #71 **after it had been diagnosed**
(carried, not found: the narrowing round discovered no record defect of its own, so "four rounds
found it" would overstate the history) —
the worst instance told every leaf, in the one document every leaf reads, that a refusal it can
actually receive cannot happen. **The trigger is the count; the audience is the priority**: three
or more statement sites is when discipline has already lost, and a site read by a leaf or an
operator does not lower the count — it decides which site you check first.

**Reach for the pattern this repository already uses three times** (`_SCRATCH_SURFACES`,
`_REDIRECT_RULE_SURFACES`, `_SURFACES` in `tools/tests/test_hooks_cli.py`) — but they are three
DIFFERENT shapes and **they duplicate each other**, so read the one nearest your rule and treat
copying as the starting point. The four traps, each of which cost a round:

- **Anchor on text that PRECEDES the rule and is byte-identical in the wording you are refusing.**
  Anchoring on your own corrected sentence pins that the correction survived, not that the rule is
  stated — witness the check by restoring the old wording and confirming the failure names
  what is missing, not the anchor
- **Bound the reader and self-test the bound**, or a document that mentions the rule's terms
  anywhere passes on the strength of an unrelated sentence
- **Decide what "names the rule" means for THIS rule.** Couple by MEMBERS only when the prose
  names them in full; otherwise couple by POINTER (each site cites where the constant lives) or by
  NUMBER. **The rule is defined once, IN THE CODE, and the documents are checked against it** —
  never the reverse, and never both spelled out independently
- **Pin the members, not the source line.** A legitimate extraction to a named constant must not
  turn a true statement red — resolve a named constant before giving up, and make the failure name
  the repair. This is the easiest one to reintroduce, because pinning the spelling is three lines
  and pinning the members is fifteen

**Before adding a check, ask whether the sites should exist.** The cheaper fix is this
repository's ordinary practice — one canonical statement, everyone else cites it (`AGENTS.md`
§Dedicated rule documents) — and it cannot rot. Coupling is for the sites that must repeat the
rule anyway: a leaf-read contract has to be self-contained, and a refusal message has to say it to
whoever was refused. (This is NOT surface 5's twin, though both count to three: surface 5 changes
the CHANNEL a decision travels on; 3-a keeps many statements of one rule honest.)

**Sites a test cannot reach are real and the check does not cover them**: a commit message, and a
prompt assembled at runtime. For those the only moves are to remove the statement or to make it
derived; say in the commit which sites you could not couple. **Check before assuming a site is out
of reach** — an issue or PR body can be edited (`gh issue edit --body`), and `docs/examples/*.yaml`
is coupled today by `tools/tests/test_llm_config.py`.

## Procedure

### 1. Inventory the surface before fixing

Enumerate, at the argument-name level, every caller-supplied input that reaches **exec / env /
argv / the filesystem / the paths from which the gate reads its evidence**. "Plugged one, the
neighbour was open" is the most frequent recurrence (env allowlisted → argv was open → the
value was open → the auto-discovery path was open: four in a row).

Leave the enumeration in the commit message or TODO.md. The next round's reviewer will come
looking to break it.

**When the gate reads the source text rather than the meaning of an input (validators and
parsers), the surface is a different one** — it is **the spelling variation the language
permits**, and writing out each grammar is the losing line. Close the family at once by
**inverting the polarity: do not parse statements that start with a keyword; take every identifier
that appears in them to the safe side.** The concrete spellings (keywords are not reserved words,
the optional space in a two-word keyword, omitted `::`, statement labels, the 18 attribute-bearing
forms) and the boundary note that governs them are in `references/source-text-surface.md`.

When a rule derives its safety from an enumeration, **write a test that kills each element of the
enumeration by mutation** (round 0 in `metdsl-review-loop`). A missing element shows up in no
other test.

The five surfaces that are none of exec / env / argv / FS / evidence paths and none of the
spelling variation. Each is one question; the episodes, the version tables and the measurement
recipes are in `references/input-surfaces.md`:

- **Surface 5 — is caller-controlled data mixed into the classification channel the verdict
  reads?** If a marker string, `failure_category`, reason or excerpt is obtained by scanning
  output text, and that text embeds data whose content a caller decides (file names, identifiers,
  paths the leaf chose), **the classification is forgeable**. L174 was broken three rounds running
  and each narrowing was defeated by one byte. **Rule: change the channel rather than narrowing
  the sample** — exit code, exception type, a dedicated field, a sidecar; each is written by the
  side that knows and cannot be written by the caller. This is the classification-channel version
  of `metdsl-review-loop`'s sign "the pin was broken in a different shape every time — move the
  definition to one place". Then **count every site that makes the same
  decision** (there were six, and two further sites scanned neither marker), and at three or more
  change the channel design instead of fixing them individually. Two follow-through traps: **a
  type channel dies silently on a list rebuild** (`[str(v) for v in …]`, a JSON round-trip, a
  `sorted(...)`) so trace the container from emit site to decision and witness it through the
  **real CLI in a real subprocess**; and **counting the sites is not answering them** — a site you
  leave unchanged needs a comment saying it is deliberate, with unreachability proved by call-graph
  closure rather than by reading
- **Surface 6 — if the check tells the reader "do this to fix it", what else does that remedy
  rewrite?** Wherever a ratchet, baseline or allowlist says "on failure, run this to update",
  check that the command does not also rewrite the pin (PR #66: a pin described in three places as
  unremovable **was regenerated by following the instructions**, then recurred in three stages as
  each fix left a wider thing washable). **Rule: never let a pin and the command with authority to
  loosen it live in the same file or the same procedure**, and pin the rule, not the result the
  rule produced. A remedy is also READ — the two rules for that (order the causes by reachability;
  never let it be followable by half) are in §3, because they are about the message a failure hands
  back. Open both when you write one
- **Surface 7 — when you say you closed a configuration surface, what else does that tool read?**
  **A flag that narrows configuration sources (`--setting-sources`, `--config`) governs
  configuration files and nothing else**; auto-injected memory, environment variables,
  cwd-discovered files and startup fetches stay outside it (PR #72: the operator's `MEMORY.md` kept
  reaching the leaf's first user message under a flag documented as closing the surface).
  **Do not conclude from the flag — capture what the process actually received and count it**
  (point `ANTHROPIC_BASE_URL` at a loopback server, save the request body, return 400: unbilled,
  and the same harness reads back the permission layer's real verdicts if you return a synthetic
  `tool_use`; **mix in one control that must be refused**, or "everything passed" cannot be told
  from a dead layer). **When several paths claim closure, observe each path** — one closed for
  another reason structurally hides the hole in the other. **Take a clean `git worktree` as a
  control**, because a development checkout's untracked `settings.local.json` is a confounder, and
  **do not decide from one entry**. **A closure claim names the surface it closed** — not "closed
  the configuration surface" but "closed the configuration-**file** surface"
- **Surface 8 — the place you created for isolation is an input to that tool.** Create a private
  configuration dir to confine a tool, make it writable, and everything the tool reads from that
  dir is a new injection surface (issue #63: one home shared by every leaf meant a `generate` leaf
  could write instructions the later `validate.judge` leaf reads — **a forged certification with
  zero trace in the artifacts**). **Enumerate what the tool READS from that dir separately from
  what it WRITES** — the write enumeration is about availability, the read enumeration about
  security. **Bind the dir ro and allowlist only the writable places**, so unknown names added by
  the tool's next version fall on the inert side, and **build that allowlist by measurement with
  the kind of leaf production runs** (measured with a tool-less leaf it missed 2 of 6).
  **Measure and state the cost of the polarity**, and **decide shared vs per-leaf first** — the
  severity came entirely from sharing
- **Surface 9 — "it moved under something the other side already covers" is a claim about a
  CONFIGURATION**, and it holds only for the configurations you enumerated (PR #86: the covering
  root is relocatable by an environment variable, and with it moved a leaf could read a sibling
  orchestration's transcript, while three documents asserted the closure unconditionally).
  **Enumerate what can MOVE the thing before writing that it is covered**: an environment
  override, a config key, a CLI flag, a symlink, a different `$HOME` — each is one execution.
  **The fix that survives is one resolver, not one sentence**: move the resolution into the module
  that owns the protected-root list so the tree that gets created is the tree that gets guarded.
  And **attribution is not enforcement** — roots are sorted longest-path-first, so a new entry
  beneath an existing one silently takes over the block message; keep a control read that must
  still be attributed to the root above

### 2. Confirm the path production actually takes

**Production does not necessarily pass the argument you wrote the rule on.** In PR #51 the rule
went on `run_syntax_check`'s `sources` argument, and the workflow never passes `sources` (the
auto-discovery side went straight through).

- Read the conductor's in-process calls for real (`_build_inproc` / `_gate_lint_check` /
  `_gate_syntax_check` / `_execute_inproc`)
- Confirm the **position** of the check too. Placed after a skip or an early return, it is inert
  under that condition
- Check whether two places read the same fact, via `references/dual-read-pairs.md`
- **If what you are adding is a conductor in-process (deterministic) substep, work
  `references/deterministic-substep-wiring.md` before writing anything** — it lists the sites that
  special-case one, across `tools/workflow_conductor.py` and `tools/orchestration_runtime.py`, and
  every miss recorded there left the unit suite green while the real flow failed closed

**There are two kinds of position. Look only at control flow inside the function and you miss
one.**

- **Inside the function**: is it after the skip / early return (above)
- **Inside the pipeline**: **does it run before the side that reads the value?** PR #57 put the
  normalization in the "right function", but the caller had already **rendered** the prompt from
  the un-normalized value 15 lines earlier: the symptom did not go away and it newly created an
  **audit-trail mismatch** — worse than before the fix
- **There is one way to find this. Do not poke the validator in a unit test; drive the production
  entry point end to end and assert on the final product** (the rendered prompt, the persisted
  JSON)

### 3. Decide the failure's attribution

Every refusal you add gets routed to someone as their fault. The criteria and the known branches
are in `references/failure-routing.md`. The essentials: **what the leaf can fix is a content
failure; what the conductor, the IR, or the environment caused is a transport fail_closed**. Name
exceptions by type; do not catch at the width of `except ValueError` (that burns the leaf's retry
budget on the conductor's own defects).

**Once attribution is decided, decide when it should surface.** Attribution can be right while the
**moment** is wrong. **Do not fail mid-run on something detectable at launch** — the precedent is
`REQUIRED_CLI_TOOLS` in `tools/run_workflow.py`. Order of decisions: **(a) whose fault is it →
(b) where is the earliest place it can be detected reliably**; if (b) is earlier than the gate,
add the earlier check and **keep the gate's fail-closed as a backstop**. (Eight subagents across
four rounds never raised this; Codex raised it on its first pass — dependencies, preflight and
operations are surfaces subagents structurally do not look at.)

**The cause a refusal message names must match the actual set of causes.** For a fail-closed,
failing correctly is not enough: if the party that failed is a leaf, it is closed only once the
message is **an instruction under which a warm retry converges** (L174 handed back an instruction
that could not converge in principle).

- When the causes grow, **grow the message**. Do not assert "the measured cause is X" in the singular
- For parsers, say that **the reported position drifts**; do not let it read as "the cause is here"
- If the enumeration does not close, **say so and describe the shape to look for**
- **Order the causes by REACHABILITY, most reachable first** — this applies to any remedy a check
  prints, an operator's included, and it is surface 6's question asked of the message instead of
  the command. Issue #71's roster check named a vendor cause when the shortest path was a one-line
  `permissions.deny`, and following the printed remedy **subtracts the tool from the required set
  permanently: the check goes green by having been WIDENED rather than satisfied**
- **A remedy must not be followable by HALF.** A leaf doing only the first half of "re-issue it
  against a granted directory, or drop the leading `/` and pass the directory as `path`" is ALLOWED
  by the hook (rc=0, measured) and the tool then matches nothing. **Silent empty is the worst
  answer a boundary can give** — indistinguishable from a true negative, so the leaf reports
  absence and stops. When a remedy is a conjunction, say so, and say what doing one half produces

### 4. Tests pin properties

- **Do not transcribe measured values.** A test asserting "`language: null` results in dual
  ownership" became **a pin that rejected the correct fix** the moment the implementation moved
  to structural reading. If you use a measured value, compute it from the code inside the test
- **Pin at the handler, not the helper.** Deleting the caller's wiring and staying green
  actually happened (4 of 5 sites)
- **Keep one test that pushes a production payload through the real validator.** The conductor's
  tests mock each tool function, so they never traverse the validation layer
- **Do not call a sample a pin.** A test placed **outside** the place that defines the set cannot
  claim set identity — it can only sample rejections. **Write in the docstring what is pinned and
  what is sampled**, and if the predicate has several branches (`==` / `startswith` / trailing
  slash), **each branch needs its own probe** — PR #55 said "pinned" three rounds running and was
  broken three different ways, the third being "a file and **not a directory**", a branch nobody
  had probed
- **If one string states two rules, a substring pin is necessarily true via the other one.** This
  repo's remedy, hint and contract texts fold two rules into one message, and PR #76 hit this four
  times on one branch. **Remedy: split on the half the rule governs, then read**
  (`WRITE_HINT.split("For temp files")[1]`). **Decision procedure**: before writing a pin, read the
  whole message and count whether the same word is used in explaining another rule — one use and
  the substring pin does not hold. **Confirmation runs to "the mutation makes it fail"**: run it
  against `origin/main`'s wording and see it actually fail. Episode:
  `references/judgment-episodes.md` §Appendix

### 5. Run the mutation check before committing

Revert your fix one hunk at a time and confirm the tests **fail**. In PR #51 the survivors among
**42 mutants**, and the tests passing for the wrong reason, were things I learned only when a later
round pointed them out.

The procedure and the script are **owned by `metdsl-review-loop`**
(`.claude/skills/metdsl-review-loop/scripts/mutation_check.py`).
The same thing runs before review, so read its "Before you hand it over (round 0)".

**When the rule rests on what a vendor TOOL can reach, mutation says nothing — measure the
tool.** `scripts/measure_claude_tool.py` in this skill drives a real Claude Code tool
through a loopback stand-in, unbilled, in a saturated fixture, and exits non-zero when any
row fails its declared expectation. Reach for it before writing "this spelling cannot
reach outside" — that claim was written down wrong four times on issue #71, each time from
a probe that measured something else. `references/verification.md` carries the details.

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
- **You added or changed a conductor in-process (deterministic) substep** →
  `references/deterministic-substep-wiring.md`
- **A new input surface, or a new episode of an existing one** → `references/input-surfaces.md`
- **The mutation check gave a false positive, or missed something** →
  `.claude/skills/metdsl-review-loop/scripts/mutation_check.py`
- **The skill did not fire when it should have, or fired when it should not** → `description`
- **You broke one of the three judgment rules while following the procedure** → SKILL.md itself.
  Distinguish "the rule was missing" from "the rule was there but its trigger point was
  elsewhere"
- **A trap not written here consumed your time** → a candidate for addition. But check that it
  **is not a copy of a canonical source** (rule content belongs to the repo's docs; only the
  procedure and traps written nowhere else belong here), and put the EPISODE in the reference
  file and the RULE here — that split is what keeps this file loadable
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
