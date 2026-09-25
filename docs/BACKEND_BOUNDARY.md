# Backend boundary (canonical source)

## Purpose
`atmofab` describes a `spec` without naming an implementation technology. A `controlled_spec` is
language-neutral; the `IR` carries the technology choice as data; the workflow renders that choice
into concrete source, a concrete build, and concrete tool invocations. This document is the
canonical source for where the knowledge of a concrete technology is allowed to live, and for the
rule that keeps it out of everything else.

## Scope
- All Python under `tools/` and `mcp_servers/`.
- The leaf prompt templates under `tools/prompt_templates/`.
- The MCP tool declarations under `mcp_servers/` (`*.md`, `*.json`): the `compiler`- and
  `linter`-axis argv is spelled there.
- Every document and script under `skills/`.
- Every document under `docs/`, recursively.
- `README.md`, `AGENTS.md`, `CLAUDE.md`.
- `requirements*.txt`, the dependency declaration. These name the pip distributions of a
  `language` backend's parser and of the `linter` backends' tools, which is the same kind of
  statement as the install line this rule already measures in `docs/RUNBOOK.md`.
- `.github/**`, the CI workflow definitions and anything they run. A workflow's install step and
  its command lines name a `linter`'s distribution, a `compiler`'s executable and a
  `build_system`'s package, so they are in scope for the same reason. Every file under it, not
  only `*.yml`: a `- run:` step is one line away from calling a shell script beside it. Nothing
  matches this today; it is here so the first workflow file lands inside the measured set.
- `.gitignore`, `.mcp.json`, `pytest.ini` and `LICENSE` — the rest of the tracked files at the
  repository root. They are in scope because the ROOT is: `.gitignore` already names a `linter`'s
  cache directory, and a root file is where a declaration lands when nobody has decided where it
  belongs. `TODO.md` is the one tracked root file left out, and
  `tools/tests/test_backend_boundary.py:_UNSCANNED_ROOT_FILES` carries the reason — it records
  backend facts as HISTORY, the same ground on which `docs/design/` is excluded below.

Out of scope, each for a stated reason:
- `spec/`. A `spec` is required to be language-neutral by `docs/CONTROLLED_SPEC.md`, and that
  requirement — not this rule — is what governs it. The requirement is not fully met today: the
  `harness_fortran_cpu` `controlled_spec` names Fortran spellings in the prose around its §5.1
  block. Closing that is a `docs/CONTROLLED_SPEC.md` matter, so `spec/` stays out of this rule's
  scope rather than being measured twice.
- `docs/design/`, which records decisions about a specific technology and is expected to name it.
- `.claude/`, the operator's own interactive development session — its settings and the skills that
  govern reviewing a change to this repository. A workflow leaf loads none of it (measured: zero
  project skills), so nothing there reaches a run; what technology it names, it names ABOUT this
  repository's own instruments rather than inside a generated system, which is the `tools/tests/`
  reason one bullet down. `docs/DEVELOPMENT.md` §"The `.claude/` boundary decision" is canonical
  for the decision and states its cost: that tree is unmeasured, and a technology change must
  sweep it by hand.
- `tools/tests/`, whose fixtures supply backend-shaped input in order to exercise a backend.

## Definitions
- **target-stack axis** (**axis**): one dimension of the technology choice a run makes. The
  declared axes are `language`, `build_system`, `compiler`, `linter`, `parallel`, and `hardware`. Each axis
  and the artifact key its value is read from is declared in `tools/backends/registry.py`, which
  is the source of truth; this sentence is the only place the list is written out, and
  `tools/tests/test_backend_boundary.py` compares it against `registry.AXES` and fails on any
  other markdown line in the repository that quotes four or more of the names in backticks. A
  restatement in plain prose, or one spread over several lines, is not detected — the guard
  catches the spelling this repository actually uses, not every possible one. Other
  documents cite this section rather than repeating the list.
- **backend**: the code that knows one value of one axis — the Fortran `language` backend, the
  `make` `build_system` backend. A backend is identified by `<axis>/<backend_id>`.
- **neutral core**: every module, template, skill, and document in scope that is not a backend.
  The conductor, the runtime, the deterministic gates, the MCP server, and the phase contracts are
  all neutral core.

## Design Policy
- The neutral core may **name** an axis value. It may not **know** what that value implies.
  Naming is carrying, comparing, logging, or passing on a token such as `fortran` or `make`.
  Knowing is anything that would have to change if the value changed: a file extension, a
  keyword, a statement grammar, a compiler argument, a lint rule id, a directive spelling, a
  control-file syntax, a symbol-naming convention, a diagnostic format.
- Every backend lives in exactly one package, `tools/backends/<axis>/<backend_id>/`, and is
  registered in `tools/backends/registry.py`. The `<axis>/<backend_id>` SHAPE is load-bearing at
  every backend location in the placement table: a file merely under a backend root — a
  `docs/backends/notes.md`, a `tools/backends/scratch.py` — is neutral core, and moving knowledge
  there is not a migration.
- The neutral core reaches a backend through `tools/backends/registry.py` only. A direct import of
  a backend module from the neutral core is a violation even when the imported name is neutral.
- The dependency direction is one-way: a backend may import the neutral core; the neutral core may
  not import a backend. A helper that a backend needs and the neutral core also uses belongs in
  the neutral core only if it is neutral; if it is not, it belongs in the backend and the neutral
  core's use of it is a violation to be migrated.
- A **present** axis value this repository has no backend for is **fail-closed**, never rendered
  through the one backend that exists. The refusal names the axis, the value, the implemented set,
  and the registry; `registry.unsupported_reason` / `registry.unavailable_reason` produce that
  clause, and a gate that refuses on this ground carries it verbatim rather than spelling its own.
  An **absent** value is a separate case: only an optional field can be absent (a target
  profile's `toolchain.compiler` / `toolchain.linker` pin), and it takes the default
  `docs/IMPL_PLAN_SPEC.md` §3 documents, which this rule does not change.
- **There are four questions about an axis value, and a caller must ask the one it means.** They
  are separate functions in `tools/backends/registry.py` because they were once fewer, and each
  merge was a fail-open.

  | Question | `None` / `True` when | Asked by |
  | --- | --- | --- |
  | `unsupported_reason` | the value is a declared member | naming, and building a refusal message |
  | `unimplemented_reason` | it is declared **and** something implements it, extracted or still inlined | a gate deciding whether a run may carry the value at all |
  | `provides(axis, value, capability)` | **this repository** does that named job for the value, wherever the code lives | a host-authorship dispatch, deciding whether the job is the host's or a leaf's |
  | `unavailable_reason` | it is declared **and** extracted | code about to call into the backend package |

  A member registered with no module and no capability — implemented nowhere — is a declaration,
  not a configuration: membership answers permissively and the other three refuse. Registering a
  backend therefore admits nothing on its own.
- **A capability is a job this repository does for one value**, declared on the `Backend` record
  and listed in `registry.CAPABILITIES`. It exists because a host-authorship dispatch cannot ask
  "is this value implemented?": the control-file writer and the runner renderer each emit one
  build system's and one language's text, so a predicate that widened with implementation would
  route a second backend's nodes into the wrong writer — a worse failure than the hard-coded pair
  it replaced. Declaring a capability asserts the code exists NOW.
- **WHERE that code lives is a second question, and the record answers it separately.**
  `core_provides` is the job still inlined in the neutral core — the debt the ledger is paying
  down. `backend_provides` is the job the record's own package implements, reached only through
  `registry.capability_module`, which refuses a value that has not declared the job and a package
  that does not carry what its record claims. A capability may not appear in both sets: one job,
  one owner. When the ledger area that owns a capability lands, the capability moves from the
  first set to the second and its dispatch site starts routing through `capability_module`.
  `provides` is the union, so the authorship answer — which is not a question about where the
  code sits — does not move with it, and no node changes hands on the migration commit.
- An axis whose carrying artifact deliberately does not constrain its value is declared
  `open_vocabulary` (today: `parallel`, whose carrier on the bundle side,
  `target_lowering_plan.parallelization`, is an open-valued object). For such
  an axis the registry lists the members that have code; membership answers permissively and
  usability still refuses.
- A backend that is declared but whose knowledge has not been extracted yet is recorded with
  `module=None` in the registry, and its migration is an entry in the ledger in `TODO.md`.
  `registry.load` raises for it. This state is a debt record, not a supported configuration.

## Placement rules
| Artifact | Neutral location | Backend location |
| --- | --- | --- |
| Python module | `tools/`, `mcp_servers/` | `tools/backends/<axis>/<backend_id>/` |
| Leaf prompt template | `tools/prompt_templates/` | `tools/prompt_templates/backends/<axis>/<backend_id>/` |
| `SKILL.md` | `skills/<skill>/SKILL.md` | `skills/<skill>/backends/<axis>/<backend_id>.md`, referenced from the neutral `SKILL.md` |
| Document | `docs/` | `docs/backends/<axis>/<backend_id>/` |

A neutral document states the contract in neutral terms and references the backend document for
the binding. A neutral document must not state the binding itself.

A target profile (`spec/targets/<target_id>.yaml`, issue #284) is neutral DATA, not a backend
location: it names one value per axis as an opaque token, and the host asks the registry about
each token (`tools/target_profile.py:target_profile_violations`). Where each axis value is read
from is `registry.AXES[<axis>].source`; for `parallel` it is the profile's `parallel.backend`
together with the bundle's `target_lowering_plan.parallelization`.

## Operations Rules
- **Adding a backend** requires, in this order: create `tools/backends/<axis>/<backend_id>/`; add
  the `Backend` record to `_BACKENDS` in `tools/backends/registry.py`; place the backend's
  documents, prompt fragments, and skill fragments under the paths above; add the backend's own
  tests.
- **What that procedure is sufficient for, TODAY, and what it is not.** Registering a backend
  makes the registry accept the value as a member. It admits nothing further on its own, by
  design: a run reaches the value only where the registry can say the code exists. Measured over
  the gates:
  - The launch gate `tools/target_profile.py:target_profile_violations` (its toolchain half is
    `toolchain_servable_reasons`; until R4-a PR-3, issue #284, the question was asked of the
    IR at `Compile.static`) requires a language to declare every capability `Generate` reads of
    it for any node kind — `bundle_facts`, `syntax_promotions`, `prompt_fragments`, `checks_abi`,
    `source_reading`, `signatures` (`LANGUAGE_CAPABILITIES_EVERY_NODE`, issue #289) — and, for a
    non-`infrastructure` node,
    `control_file` and `runner_render`. It, together with the validator's dispatch into the
    control-file gates (`_validate_control_file`, whose gates are the build system backend's since
    issue #289's R4-b PR-3), and `tools/workflow_conductor.py`'s authorship
    predicates, no longer spell a pair of their own — they ask `provides` for the capability they
    need and carry the registry's clause. They widen when the CAPABILITY is declared, which
    asserts that code in this repository already does that job for the value — inlined in the
    neutral core (`core_provides`) or in the record's own package (`backend_provides`); declaring
    one is how a new backend's host-side work is admitted, and the declaration is the thing to
    review.
  - The same gate's `hardware` half (issue #289) asks a class for `execution` only when the run
    reaches `Validate`, and asks the parallel backend for `execution_env` then too; the launch
    itself goes through `tools/host_execution.py`, which asks the same two. So a registered
    hardware class with no `execution` is admitted for a run that stops at `Build` and refused
    for one that runs the binary — building for a class needs no machine of that class.
  - The per-language tables in `tools/codegen_bundle.py` are gone: `LANGUAGES`, the extension
    allowlist, the compiler-driver families, the identifier grammar and the names the host gives
    a language's files are the language backend's `bundle_facts`, reached through
    `capability_module`. The identifier check is per-file-language: the schema-level pattern is
    the union of the bundle languages' grammars and a cross-field layer holds each identifier to
    its own file's language (issue #289). The bundle SCHEMA (`spec/schema/generate/`) still
    carries its own `language` enum and that union pattern, and `tools/tests/test_codegen_bundle.py`
    fails if the two disagree — so a new language backend must widen the schema in the same change.
  - The `Generate.gate` syntax check reaches its argv through the compiler's `syntax_check` and
    the language's `syntax_promotions` (issue #289): a language that declares none is a transport
    `fail_closed` there and a certification violation, not a pass-through. The generate prompts
    carry a language's rules only as its `prompt_fragments`, composed by the request's
    `pure_language`; the checks-module contract is neutral and its binding is the language's
    `checks_abi`. Which linter a language is linted with is each linter backend's `LANGUAGES`
    (`registry.linter_for_language`).
  - **Every deterministic gate that READS a node's source reads it through the target
    language's backend** (issue #289, R4-b PR-3): the model-source, checks-source, runner-output
    and dependency-use gates through `source_reading`; the §5.1 signature gates through
    `signatures` — at `Generate.static` in the pipeline's target language, and at the
    target-free `Compile.static` in EVERY language that declares it; the dependency facts a
    consumer is shown through the consumer language's `signatures`, which also states how they
    are SHOWN (the call-site guidance, since the R4-b PR-4 preconditions); the file names the gates and
    `phase_required_outputs` read through `bundle_facts`. A language that does not declare the
    capability a gate needs is refused there (`validate_pipeline_semantics._language_module`),
    never read as another language — until that PR these gates imported one language backend by
    name, and a runner under another language's suffix was not SEEN by the runner-output gates
    at all (fail-open). The build control file's gates and renderers are the build system's
    `control_file` (`tools/backends/build_system/make/`), with the language's compile rules as
    its `control_file` half; the Generate presence floor is the parallel backend's
    `parallel_directives`.
  - **A remedy or prompt paragraph a leaf reads that names a language's statements is stated by
    that language's backend**, whichever neutral gate decides the finding (issue #289, the R4-b
    PR-4 preconditions): the checks-ABI and bound-state remedies by the runner backend through
    `host_render`, the component-surface remedies by `source_reading`, the dependency-operation
    guidance by `signatures`, the exemplar's unreferenced-dummy idiom by `prompt_fragments`. A
    target-free message (Compile's) names no language's statements at all.
  - Whether a language's quality check runs through the build system's test target, and whether
    `compile_project` holds it to a dependency-aware build tool, is asked of the language
    backend's `bundle_facts.COMPILED` (`registry.is_compiled_language`); the two token sets that
    answered it in the neutral core (`MAKE_QUALITY_CHECK_REQUIRED_LANGUAGES`, `FORTRAN_C_FAMILY`)
    are gone (issue #289).
  - The `static lint` step reaches a registered linter's argv through `capability_module` only
    where that record declares `lint` in `backend_provides`. Every linter that HAS an argv does
    (issues #111 and #120), so no linter invocation is spelled in
    `mcp_servers/build_runtime_server.py` any more. Registering a fifth linter still widens the
    evidence gate (which asks the registry) and not the server, which keeps its own accepted set
    of preset NAMES. The criterion that forced each move is worth stating: the argv carries the
    RULE SET the gate applies, or the compiler-family arguments it applies it under, and both are
    knowledge this document forbids the neutral core.
    - **One linter record still declares `lint` in `core_provides`, and it is not an exception to
      the rule.** `mixed` is a COMPOSITE: it is defined by the presets it runs in order, holds no
      rule id, no flag and no executable name, and naming an axis value is what the neutral core
      may do. Issue #120's acceptance was written as "`core_provides={"lint"}` appears on no
      `linter` record"; that wording was wrong rather than the row, because the rule this document
      states is about knowing, not about naming.

  Stated this way because the first version of this section claimed the procedure was sufficient,
  and following it produced a backend nothing accepted — and then, after a partial fix, a backend
  that was accepted and silently rendered as Fortran.
- **Adding a capability** requires an entry in `registry.CAPABILITIES` naming which axes it is a
  question of and what job it is, plus at least one record declaring it — a capability nothing
  declares is a question whose answer is always `False`, so a dispatch keyed on it is dead code
  that reads as a live rule. A capability a backend PACKAGE implements additionally requires a
  `registry.CAPABILITY_MODULE_ATTR` row naming the submodule the package re-exports it under: a
  `backend_provides` entry without one is a capability that is declared true and unreachable at
  the same time, and the import-time check refuses it.
- **Moving a capability into a backend** is one commit, not two: create the module under
  `tools/backends/<axis>/<backend_id>/`, re-export it from the package `__init__`, move the
  capability from `core_provides` to `backend_provides`, point the neutral seam at
  `capability_module`, delete the neutral module, and remove its direct-import allowlist entry.
  Splitting it would leave the declaration describing a tree that does not exist — briefly, but
  the declaration is what every dispatch believes. Conversely a record declaring NOTHING — no module, no capability —
  says this repository knows a value nothing can run; the code fails closed on that state, and the
  live declarations must not be in it. `registry` checks its own declarations at import, since a
  typo answering `False` forever would flip a host-authorship dispatch off silently, and
  `tools/tests/test_backend_boundary.py` pins the check, the rules above, and the fact that the
  check is INVOKED at import — deleting that call left the whole suite green.
- **Adding an axis** requires three things: an entry in `AXES` in `tools/backends/registry.py`
  naming where its value is read from and whether its vocabulary is open; **at least one `Backend`
  record for it** (an axis with no members is refused by `tools/tests/test_backend_boundary.py`,
  since an axis nothing implements is a declaration with no subject); and adding its name to the
  §Definitions list above. No row is added to the placement table: that table is indexed by
  artifact kind and parameterised by `<axis>`, so it already covers every axis.
- **Migrating an area** moves knowledge into a backend location from the placement table. Two
  consequences are expected and are not violations: the sampled counts of the *citing* documents
  rise, because a path naming a backend id is naming, not knowing (§Decision Criteria); and the
  moved file leaves the scanned set, which the suite reports as a stale baseline until the
  baseline is regenerated in the same pull request (§Enforcement). Regeneration rewrites the
  sampled half only — the direct-import allowlist lives in its own file and is edited by hand, so
  a migration cannot absorb a new bypass.
- **Changing a rule stated here** requires washing every document that cites it. The citations are
  found with `grep -rn "BACKEND_BOUNDARY" docs skills tools mcp_servers *.md` from the repository
  root — the root `*.md` is load-bearing, since `TODO.md` carries the migration ledger and its
  measured figures, and `README.md` indexes this document.

## Decision Criteria
A fragment of the neutral core is a violation when either test holds.

1. **Substitution test.** Replace the axis value with another declared value of the same axis. If
   the fragment becomes wrong rather than merely unused, it encodes backend knowledge.
2. **Import test.** The fragment imports, or names for import, a module under
   `tools/backends/` without going through `tools/backends/registry.py`.

The following are **not** violations:
- Carrying, comparing, or logging an axis value as an opaque token.
- A backend id appearing in a `spec_id`, an artifact path, or a run record.
- A design note under `docs/design/` naming a technology.
- A test fixture that supplies backend-shaped input in order to exercise a backend.

## Enforcement
Two mechanisms, with different reach. Neither subsumes the other, and neither is a proof of
compliance.

- `tools/backends/registry.py` declares where backend knowledge belongs and refuses an
  unimplemented axis value. It cannot observe knowledge that never asked it anything.
- `tools/tests/test_backend_boundary.py` holds two measures with different reach.
  - The **direct-import pin** is a set identity over the spellings it reads: `import`, an
    absolute `from ... import`, a relative `from . import`, and a literal module name passed to
    `importlib.import_module` or `__import__`, positionally or as `name=`. Within those it is
    complete, and a module that does not parse raises rather than reading as clean. Two things
    are out of reach of any static reader and are NOT covered: a module name computed at runtime,
    and an importer obtained indirectly (`importlib.__dict__["import_module"](...)`). Both are
    pinned as limits by tests, so the boundary of the claim cannot quietly move. The allowlist, the scanned file set and the token-class list live in
    `tools/tests/data/backend_boundary_allowlist.json`, which no command writes: each is changed
    by a reviewed hand edit, so narrowing the instrument is not something a regeneration can
    bless.
  - The **token ratchet** counts, per neutral file and per token class, the occurrences of a fixed
    list of technology-specific tokens, and reports both when a count exceeds the recorded baseline in
    `tools/tests/data/backend_boundary_baseline.json` and when it falls below it (a stale
    baseline). It runs in the suite (`TokenRatchetTests`), so it bounds growth and forces the
    recorded debt down as areas migrate. It is a **sample, not a pin**: a token list is an enumeration, backend knowledge with no token in the
    list is invisible to it, and a file that removes one occurrence and adds another of the same
    class keeps its count. Do not read a passing ratchet as an absence of violations, and do not
    read a falling count as migration — only a count falling because knowledge moved *into a
    backend* is migration.

A pull request the ratchet reports on judges every entry by §Decision Criteria: growth that is
backend knowledge is moved into a backend, never blessed; growth that is a token in a neutral
role, and staleness, are recorded by regenerating the baseline in that pull request, with the
check's output and the judgement stated there. The comparison also runs on request,
`python3 -m tools.tests.test_backend_boundary --check-baseline`, which exits 1 naming every grown
or stale entry and 0 when the tree matches the baseline; `--write-baseline` regenerates it.

The two commands are refused in one invocation (exit 2), because the judgement between them is the step and an invocation asking for
both would report a regeneration as a check that passed; an unrecognised argument beside either
is refused for the same reason, since a mistyped second flag would otherwise run one command
silently. `--write-baseline` prints the grown and stale entries it is about to bless, under
separate headings because the two take opposite answers, before it writes — so a regeneration
that skipped the check still puts the material in front of whoever typed it. That listing is a
disclosure and not a substitute for `--check-baseline`, which is a separate invocation with its
own exit code.

The current baseline is not zero. The per-area migration plan that reduces it is the ledger in
`TODO.md`; the measured debt at the time this rule was written is recorded in
[a comment on the ratchet's issue](https://github.com/seiya/atmofab/issues/182#issuecomment-5559624872).
