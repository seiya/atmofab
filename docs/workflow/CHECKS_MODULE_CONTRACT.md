# Checks-module contract (fixed ABI)

> **Audience: the leaves of an *M3c physics node* — a non-`infrastructure` node whose target
> the host authors the build control file for and renders the runner over the target's
> harness.** On such a node the `Generate.generate` leaf authors **two** sources: the model (the
> physics kernel and its published operation) and the checks module this contract governs. It
> does **not** author the runner or the build control file — those are host-rendered by the
> conductor (`tools/host_render.py` and the control-file writer). This is not read by
> `Validate.judge`.
>
> This document is **language-neutral**: it names the procedures, the role and direction of
> each argument, what the bound state means, and what the module must not do. How each of
> those is spelled in a target language — the declarations, the accessibility rules, the
> import form the runner uses, and the language's legality and gate-guard rules — is that
> language backend's binding, `docs/backends/language/<language>/CHECKS_ABI.md`, numbered
> section for section like this one (§5 below).
>
> NO leaf reads this document from disk at its repository path. The host seeds it into a leaf's
> work tree as `CHECKS_MODULE_CONTRACT.md`: the `compile.generate` producer is seeded §1-§4 of it
> (Compile is target-free, so it is seeded no binding); the `generate.verify` reviewer §1-§4
> followed by §1-§4 of the target language's binding (document key
> `checks_module_contract_document`, issue #142); the `generate.generate` producer is told the
> three names and the binding convention and seeded the rendered runner, gated by
> `m3c_checks_abi_violation`, with the §2/§3 behavioral contract distilled into its launch
> template. All three procedures bind every node. The language backend
> that renders the runner owns the ABI's names (`host_render.checks_public_names`) — keep it,
> this document, the binding and the distilled paragraph in step. Since Z6
> ([issue #255](https://github.com/seiya/atmofab/issues/255)) the module also carries the node's
> **bound state** (§1-b): there is no snapshot getter, and the runner reads the snapshot
> variables straight from the module's storage.

The rendered runner is glue: it drives this module's procedures, captures the module's bound
state, and emits the standard runner outputs **through the certified harness plumbing**, which
owns all JSON serialization. So the checks module holds **no serialization, no I/O and no
judgment**: it drives the published operation and holds the case's state; the runner captures,
the harness writes, and the host alone judges, by the IR's predicates over the captures
([issue #582](https://github.com/seiya/atmofab/issues/582)). Getting this split right is what lets the
runner be host-rendered (deterministic, no leaf regenerate loop), and it is what makes the
snapshot **primary evidence** (`docs/design/zero_base_architecture.md` §A4): the generated code
contributes the binding — the declaration and the allocation of the state variables — and
nothing downstream of it.

## 1. The fixed ABI

The checks module (named `<spec_id>_checks`) publishes **exactly** these three non-prefixed
procedures, plus the bound state variables of §1-b. Each is a procedure with no return value,
reached by the runner as a call. The argument roles, in order, are fixed:

| Procedure | Arguments (name: direction, type) | What it does |
|---|---|---|
| `case_setup` | `case_id`: in, string; `ok`: out, logical | Initialize this case's state from the spec's fixed inputs and constants. The runner does not read `ok`, here or from `case_run`: every case proceeds, so its snapshot is produced, and what the captures hold (a `record` of the operation's logical result, §1-b, or the state it left) is the guard's evidence — the value of `ok` is never a finding. |
| `case_run` | `case_id`: in, string; `steps`: out, integer; `cells_updated`: out, integer; `ok`: out, logical | Run the model kernel's time loop for this case — by calling the model's published operation (`<spec_id>__<op>`), from `case_run` or from a procedure of the checks module that `case_run` reaches; the checks module computes no state update of its own, and the `Generate.gate` static check refuses a `case_run` from which no published operation is reachable; it checks a reach, not a use, so whether the state IS the operation's result stays the reviewer's to judge — and return the perf counters. A non-time-stepping component uses `steps` = 1 and `cells_updated` = the cells touched. |
| `get_time` | `t`: out, float64 | The scalar time of this case at the capture point (0 for an untimed component). Called right AFTER each of the two captures, for the time the snapshot is written with, so no generated procedure runs between `case_setup` / `case_run` returning and the state being serialized. |

Every `out` argument is assigned on every path of the procedure. The three names must be published
from the checks module **itself**; which declarations publish a name is the binding's rule, and
the `Generate.gate` static check (`_validate_checks_source_files`) applies it.

### 1-b. The bound state

Every variable the IR declares under
`io_contract.raw_requirements.required_evidence[artifact: state_snapshots].schema.variables[]`
is a **module-level float64 variable of the checks module, named exactly as the IR names it** —
of rank N for a rank-N `shape_expr`, a scalar for `shape_expr: scalar` — and is published. The
host-rendered runner reads each one directly from the module and serializes it through the
harness emitters at two capture points per case: right after `case_setup` (written to
`raw/state_snapshots/initial/<case_id>.json`) and right after `case_run` (written to
`raw/state_snapshots/<case_id>.json`). There is no getter: no procedure of this module returns
a snapshot value.

The IR's `source` field of a variable says who writes it, and the module holds nothing else as
bound state:

| `source` | What the bound variable holds |
|---|---|
| `operation` (default) | An argument of the published operation: state in or out, a repeated call's result. `case_setup` fills the inputs and the operation writes the outputs. |
| `record` | A value the module records of the operation's observable behaviour without computing a judgment: its logical result as `-1 / 0 / 1`, a count of callback invocations, a state copied at a callback or after an intermediate call. |
| `reported` | A scalar: a run-wide reduction over the integration path, which the operation returns as an output argument and the module passes through. On an execution shape that distributes the state it is the whole-domain reduction, made before the operation returns (the target's parallel rules say how), because one rank's copy of a bound scalar is what the capture writes. |
| `reference` | A field the module computes from the spec's closed forms as a comparison reference, only where `tests.md` §2 defines it and names it as leaf-computed. |

A quantity the end state defines (an error norm, a mass, a symmetry residual, a per-case extremum)
is evaluated by the host from the state and is never a bound variable, and neither is an input the
case declares.

The bundle declares this convention as its `state_bindings[]` — one entry per snapshot
variable, `storage_symbol == state_variable`, `module == <spec_id>_checks`,
`capture: harness_registration`, `capability: state_registration@1`
(`docs/workflow/CODEGEN_BUNDLE_CONTRACT.md` §State bindings) — and the host checks the
declaration against the IR, then requires each bound variable **published** under the same rule
as the ABI names. An unallocated bound array at a capture point stops the run (`bound state
<name> is not allocated at capture`), so `case_setup` allocates every array to its declared
shape on every path, the rejected-input path included.

## 2. Semantics the harness relies on

- **Bound state is shape-valid at both capture points, even for a rejected case.** A
  guard/xfail case must STILL leave every
  bound array allocated to its declared shape and every bound scalar defined (the
  runner always captures the case's state, right after `case_setup` and right after
  `case_run`). Leave a defined placeholder (e.g. zeros of the right shape), never an
  unallocated array. `case_run` updates the state in the bound variables in place —
  a state kept in a private copy is one the capture never sees.
- **`case_run` perf counters** feed the single `perf.json` (`steps` summed,
  `cells_updated` summed across the run); make them the real work done.
- **A procedure cannot reach a capture already taken.** Each capture of a case is
  taken straight after `case_setup` / `case_run` returns, before the `get_time` that
  follows it, and the runner keeps the serialized copy — whatever `get_time` writes into a
  bound variable afterwards is invisible to that capture (a write from the initial
  capture's `get_time` reaches the final capture exactly as a write in `case_run` would:
  it is the state). (A procedure that rewrote the snapshot FILE would be the §4 file-I/O
  prohibition broken; the record of what the gate does and does not see is
  `docs/design/zero_base_architecture.md`, the Z6 item.)

## 3. Module-level state is expected

The runner calls `case_setup`, captures, calls `case_run` and captures again, for one case at a
time. The current case's state lives in the **bound module-level variables** of §1-b — that is
what the captures read. A quantity that exists only across cases (a convergence order, a symmetry
residual) needs nothing here: the host's predicate reads the other case's capture
(`at('<case_id>')`, `phase_01_compile.md`). The runner receives its `case_id`s **sorted**, not in
`test_case_set` declaration order.

## 4. Prohibitions

- **No reference to the harness module** from EITHER the checks module or the model, except to
  the operations the harness offers a physics source: a harness that runs the program as several
  ranks offers its distributed-state operations (the partition, the halo exchange, the rank
  queries, the reductions; issue #316), which are how a distributed kernel reaches the runtime,
  and the language binding says how they are named. Everything else the harness publishes — the
  emitters and the writers — is the rendered runner's alone. (The harness's model object is
  linked via the dependency closure.)
- **No file I/O in the checks module** — no file is opened or written, and none of
  `verdict.json` / `aggregate_verdict.json` / `summary.json` / `trial_meta.json` appears in it,
  even as a comment or example string. Emission is the harness's exclusive job.
- **No local entity named as a published operation** — the checks module calls the model's
  operation (`<spec_id>__*`) and gives that name to nothing of its own: no procedure,
  procedure pointer, variable, dummy or other declaration of that name — a call to such a name
  would run the checks module's own code. Where the name may appear is the binding's §4.

## 5. Language binding

The spelling of §1-§4 in a target language, and that language's legality and gate-guard rules
for every leaf-authored source of a `Generate` node, are the language backend's:
`docs/backends/language/<language>/CHECKS_ABI.md` (its §1-§4 bind §1-§4 here; its §5 is the
gate-guard rule set). The backend reaches it through its `checks_abi` capability
(`tools/backends/registry.py`).
