# Backend boundary (canonical source)

## Purpose
`met-dsl` describes a `spec` without naming an implementation technology. A `controlled_spec` is
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
- `leaf_config/`, the committed configuration a workflow leaf is launched with. It names
  hook events, tool names and permission tokens of the LLM CLI that runs a leaf — an
  `agent` axis concern, not a target-stack one — and no `axis` value appears in it.

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
  declared axes are `language`, `build_system`, `compiler`, `linter`, and `parallel`. Each axis
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
  An **absent** value is a separate case: it takes the default `docs/IMPL_PLAN_SPEC.md` documents,
  which this rule does not change.
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
  `open_vocabulary` (today: `parallel`, whose knob schema states it is not a whitelist). For such
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

## Operations Rules
- **Adding a backend** requires, in this order: create `tools/backends/<axis>/<backend_id>/`; add
  the `Backend` record to `_BACKENDS` in `tools/backends/registry.py`; place the backend's
  documents, prompt fragments, and skill fragments under the paths above; add the backend's own
  tests.
- **What that procedure is sufficient for, TODAY, and what it is not.** Registering a backend
  makes the registry accept the value as a member. It admits nothing further on its own, by
  design: a run reaches the value only where the registry can say the code exists. Measured over
  the gates:
  - `_validate_toolchain_backend_supported` and the `make`-quality-check gates in
    `tools/validate_pipeline_semantics.py`, and `tools/workflow_conductor.py`'s authorship
    predicates, no longer spell a pair of their own — they ask `provides` for the capability they
    need and carry the registry's clause. They widen when the CAPABILITY is declared, which
    asserts that code in this repository already does that job for the value — inlined in the
    neutral core (`core_provides`) or in the record's own package (`backend_provides`); declaring
    one is how a new backend's host-side work is admitted, and the declaration is the thing to
    review.
  - The per-language tables in `tools/codegen_bundle.py` are gone: `LANGUAGES`, the extension
    allowlist, the compiler-driver families and the identifier bound are read from the language
    backend through the registry. The bundle SCHEMA (`spec/schema/generate/`) still carries its
    own `language` enum and pattern, and `tools/tests/test_codegen_bundle.py` fails if the two
    disagree — so a new language backend must widen the schema in the same change.
  - The two infrastructure signature gates take their refusal clause from the registry, but their
    §5.1 helpers import one concrete backend by name and take no `language` argument, so they
    additionally refuse any language those helpers are not wired to
    (`_signature_backend_refusal`). This is the one gate family the procedure above is still not
    sufficient for; it migrates with the `validate_pipeline_semantics.py` source-reading area.
  - `MAKE_QUALITY_CHECK_REQUIRED_LANGUAGES` remains a neutral-core policy set over language
    families (`fortran`, `c`, `cpp`, `mixed`), not an implemented set; it migrates with the
    compiler / linter adapters area, alongside `FORTRAN_C_FAMILY` in `mcp_servers/`.
  - The `static lint` step reaches a registered linter's argv through `capability_module` only
    where that record declares `lint` in `backend_provides` — one value does today, and the rest
    are still rows of a table in `mcp_servers/build_runtime_server.py`. Registering a fifth
    linter therefore widens the evidence gate (which asks the registry) and not the server, which
    keeps its own accepted set. What forced the first of them out is worth stating as the
    criterion: the argv carries the RULE SET the gate applies, and a lint rule id is knowledge
    this document forbids the neutral core, so an argv that selects rules cannot stay there.

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
  moved file leaves the scanned set, which fails the stale-baseline half until the baseline is
  regenerated. Regeneration rewrites the sampled half only — the direct-import allowlist lives in
  its own file and is edited by hand, so a migration cannot absorb a new bypass.
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
    list of technology-specific tokens, and fails both when a count exceeds the frozen baseline in
    `tools/tests/data/backend_boundary_baseline.json` and when it falls below it (a stale
    baseline). It bounds growth and it forces the recorded debt down as areas migrate. It is a
    **sample, not a pin**: a token list is an enumeration, backend knowledge with no token in the
    list is invisible to it, and a file that removes one occurrence and adds another of the same
    class keeps its count. Do not read a passing ratchet as an absence of violations, and do not
    read a falling count as migration — only a count falling because knowledge moved *into a
    backend* is migration.

The current baseline is not zero. The measured debt at the time this rule was written, and the
per-area migration plan that reduces it, are recorded in `TODO.md`.
