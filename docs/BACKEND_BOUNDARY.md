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

Out of scope, each for a stated reason:
- `spec/`. A `spec` is required to be language-neutral by `docs/CONTROLLED_SPEC.md`, and that
  requirement — not this rule — is what governs it. The requirement is not fully met today: the
  `harness_fortran_cpu` `controlled_spec` names Fortran spellings in the prose around its §5.1
  block. Closing that is a `docs/CONTROLLED_SPEC.md` matter, so `spec/` stays out of this rule's
  scope rather than being measured twice.
- `docs/design/`, which records decisions about a specific technology and is expected to name it.
- `tools/tests/`, whose fixtures supply backend-shaped input in order to exercise a backend.

## Definitions
- **target-stack axis** (**axis**): one dimension of the technology choice a run makes. The
  declared axes are `language`, `build_system`, `compiler`, `linter`, and `parallel`. Each axis
  and the artifact key its value is read from is declared in `tools/backends/registry.py`.
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
  registered in `tools/backends/registry.py`.
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
- **Membership and usability are different questions, and a caller must ask the one it means.**
  `unsupported_reason` asks whether the value is a declared member. `unavailable_reason` asks
  whether it is declared *and* extracted. Code that is about to run backend behaviour — a
  renderer, a scanner, a control-file writer — asks the second: a declared-but-unextracted member
  has no code of its own, so guarding on membership alone lets it fall through to whichever
  backend the surrounding module hard-codes.
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
- **Which gates that procedure is sufficient for, TODAY.** It is sufficient for the two
  infrastructure signature gates in `tools/validate_pipeline_semantics.py`
  (`_validate_infrastructure_public_api`, `_validate_infrastructure_generated_signatures`), which
  take their refusal from the registry. It is **not** sufficient for the gates that still spell
  their own set: `_validate_toolchain_backend_supported` and the `make`-quality-check gates in the
  same module, `tools/workflow_conductor.py`'s `make ∧ fortran` authorship conjunction, and the
  per-language tables in `tools/codegen_bundle.py`. Those refuse a new backend regardless of the
  registry, and migrating them is an area in the ledger in `TODO.md`. The target state is that no
  gate spells the set; this document does not claim the target state has been reached.
- **Adding an axis** requires an entry in `AXES` in `tools/backends/registry.py` naming where its
  value is read from and whether its vocabulary is open, an entry in `docs/GLOSSARY.md`, and a row
  in the placement table above.
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
  - The **direct-import pin** is a set identity over three spellings — `import`, `from ... import`,
    and `importlib.import_module` with a literal argument. Within those it is complete; a module
    name computed at runtime is out of reach of any static reader and is not covered.
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
