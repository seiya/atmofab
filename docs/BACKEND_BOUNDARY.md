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
- The `SKILL.md` files under `skills/`.
- The documents under `docs/`.

Out of scope: `spec/` (a `spec` names no technology by construction — the rule that enforces that
is `docs/CONTROLLED_SPEC.md`), and the design notes under `docs/design/`, which record decisions
about a specific technology and are expected to name it.

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
- An axis value this repository has no backend for is **fail-closed**, never defaulted to the one
  backend that exists. The refusal names the axis, the value, the implemented set, and the
  registry — `registry.unsupported_reason` produces that clause, and every gate that refuses on
  this ground carries it verbatim rather than spelling its own.
- A backend that is declared but whose knowledge has not been extracted yet is recorded as
  `extracted=False` in the registry, and its migration is an entry in the ledger in `TODO.md`.
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
  tests. No edit to a neutral gate is required to make the new value accepted — a gate that has to
  be edited is a gate that spelled the implemented set itself, which this rule forbids.
- **Adding an axis** requires an entry in `AXES` in `tools/backends/registry.py` naming where its
  value is read from, an entry in `docs/GLOSSARY.md`, and a row in the placement table above.
- **Changing a rule stated here** requires washing every document that cites it. The citations are
  found with `grep -rn "BACKEND_BOUNDARY" docs skills tools mcp_servers AGENTS.md CLAUDE.md`.

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
- `tools/tests/test_backend_boundary.py` is a **ratchet**: it counts, per neutral file and per
  token class, the occurrences of a fixed list of technology-specific tokens, and fails when a
  count exceeds the frozen baseline in `tools/tests/data/backend_boundary_baseline.json`. It
  bounds growth and it measures migration progress. It is a **sample, not a pin**: a token list is
  an enumeration, backend knowledge with no token in the list is invisible to it, and a file that
  removes one occurrence and adds another of the same class keeps its count. Do not read a passing
  ratchet as an absence of violations.

The current baseline is not zero. The measured debt at the time this rule was written, and the
per-area migration plan that reduces it, are recorded in `TODO.md`.
