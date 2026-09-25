# Checks-module contract (fixed ABI)

> **Audience: the leaves of an *M3c physics node* — a non-`infrastructure` node whose target
> the host authors the build control file for and renders the runner over the target's
> harness.** On such a node the `Generate.generate` leaf authors **two** sources: the model (the
> physics kernel and its published operation) and the checks module this contract governs. It
> does **not** author the runner or the build control file — those are host-rendered by the
> conductor (`tools/host_render.py` and the control-file writer). This is not read by
> `Validate.judge`.
>
> This document is **language-neutral**: it names the callbacks, the role and direction of
> each argument, what the bound state means, and what the module must not do. How each of
> those is spelled in a target language — the declarations, the accessibility rules, the
> import form the runner uses, and the language's legality and gate-guard rules — is that
> language backend's binding, `docs/backends/language/<language>/CHECKS_ABI.md`, numbered
> section for section like this one (§5 below).
>
> NO leaf reads this document from disk. It reaches its leaves inlined by the host: the
> `compile.generate` producer receives §1-§4 of it (Compile is target-free, so it is shown no
> binding); the `generate.verify` reviewer receives §1-§4 followed by §1-§4 of the target
> language's binding, as `checks_module_contract_document` (issue #142); the
> `generate.generate` producer is told the five names and the binding convention and shown the
> rendered runner, gated by `m3c_checks_abi_violation`, with the §2/§3 behavioral contract
> distilled into its launch template. All five callbacks bind every node. The language backend
> that renders the runner owns the ABI's names (`host_render.checks_public_names`) — keep it,
> this document, the binding and the distilled paragraph in step. Since Z6
> ([issue #255](https://github.com/seiya/atmofab/issues/255)) the module also carries the node's
> **bound state** (§1-b): there is no snapshot getter, and the runner reads the snapshot
> variables straight from the module's storage.

The rendered runner is glue: it drives this module's callbacks, captures the module's bound
state, and emits the standard runner outputs **through the certified harness plumbing**, which
owns all JSON serialization and the verdict fold. So the checks module holds **no serialization
and no I/O**: it holds the case's state and computes honest per-case checks and metrics; the
runner captures, and the harness folds and writes. Getting this split right is what lets the
runner be host-rendered (deterministic, no leaf regenerate loop), and it is what makes the
snapshot **primary evidence** (`docs/design/zero_base_architecture.md` §A4): the generated code
contributes the binding — the declaration and the allocation of the state variables — and
nothing downstream of it.

## 1. The fixed ABI

The checks module (named `<spec_id>_checks`) publishes **exactly** these five non-prefixed
callbacks, plus the bound state variables of §1-b. Each is a procedure with no return value,
reached by the runner as a call. The argument roles, in order, are fixed:

| Callback | Arguments (name: direction, type) | What it does |
|---|---|---|
| `case_setup` | `case_id`: in, string; `ok`: out, logical | Initialize this case's state from the spec's fixed inputs and constants. `ok` false rejects a guard / xfail input (e.g. an invalid grid size); the case still proceeds, so its snapshot and its input-guard check are produced. |
| `case_run` | `case_id`: in, string; `steps`: out, integer; `cells_updated`: out, integer; `ok`: out, logical | Run the model kernel's time loop for this case and return the perf counters. A non-time-stepping component uses `steps` = 1 and `cells_updated` = the cells touched. |
| `get_time` | `t`: out, float64 | The scalar time of this case at the capture point (0 for an untimed component). Called right AFTER each of the two captures, for the time the snapshot is written with, so no generated procedure runs between `case_setup` / `case_run` returning and the state being serialized. |
| `checks_compute` | `case_id`: in, string; `check_id`: in, string; `status`: out, string of width 4 | The honest per-case result for ONE check. The runner calls it once per (case, check id), supplying `check_id` as a literal from the IR's `diagnostics_contract.checks[].id`; the module authors no id and sets `status` to one of `pass` / `fail` / `na` (width 4, right-padded) on EVERY `check_id` branch, the default branch included. An xfail case's failing guard still reports `fail` (the harness folds). |
| `metric_compute` | `case_id`: in, string; `name`: in, string; `val`: out, float64; `is_na`: out, logical; `reason_na`: out, deferred-length string; `found`: out, logical | One `diagnostics_contract.metrics` leaf (dotted address, e.g. `error.l2`) for this case. `found` false when this case does not produce that metric (it is then omitted); `is_na` / `reason_na` carry an honestly-unavailable value. |

Every `out` argument is assigned on every path of the callback. The five names must be published
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
`raw/state_snapshots/<case_id>.json`), both **before** the first `checks_compute` /
`metric_compute` call of that case. There is no getter: no procedure of this module computes,
filters or returns a snapshot value, and a value the checks compute (a norm, a maximum, a flag,
an echoed input) is a metric, not a snapshot variable.

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
  guard/xfail case whose `case_setup` returned `ok` false must STILL leave every
  bound array allocated to its declared shape and every bound scalar defined (the
  runner always captures the case's state, right after `case_setup` and right after
  `case_run`). Leave a defined placeholder (e.g. zeros of the right shape), never an
  unallocated array. `case_run` updates the state in the bound variables in place —
  a state kept in a private copy is one the capture never sees.
- **`checks_compute` is honest, never judgmental.** The runner calls it once per
  (case, IR check id). Report `fail` when a check fails, even for an xfail case. The
  harness's diagnostics writer computes the per-case verdict (`overall` is `fail` iff any of
  that case's checks is `fail`), the top-level fold, and the **xfail exclusion** (a failing case
  whose `expected_xfail` is true — supplied by the runner from the IR predicates
  — is excluded from the top-level `failed_checks`). Do not pre-adjust for xfail
  here; doing so double-counts and breaks the guard-passes-at-top-level rule.
- **NA metrics.** When a metric is honestly unavailable set `found` true,
  `is_na` true, and `reason_na` to a short reason; the harness encodes it as
  `"<address>": null` plus a sibling `"<address>_reason_na": "<reason>"`. A
  metric that simply does not apply to a case sets `found` false (omitted).
- **`case_run` perf counters** feed the single `perf.json` (`steps` summed,
  `cells_updated` summed across the run); make them the real work done.
- **Metrics-basis is a test × target-case matrix.** The host-rendered runner records one
  `raw/metrics_basis.json` entry per (`test_id`, target `case_id`) pair, from
  `io_contract.test_predicates[].target_cases`. A multi-target test (a convergence sweep, a
  base/shifted pair) therefore records evidence for *every* case it ranges over; partial
  evidence fails the `post_execute` completeness matrix.
- **Cross-case reductions are per-case metrics.** A quantity that exists only across cases (a
  convergence order, a symmetry residual) is accumulated by the §3 module-level pattern and
  returned by `metric_compute` as a per-case metric of the case where it first becomes
  computable — the `n064` case carries `convergence.order_n032_to_n064`, the `n128` case
  `order_n064_to_n128`. Earlier cases return `found` false for it (a sparse metric is
  normal). The predicate reads it with a `case: <case_id>` condition, so the runner still
  reduces and the predicate still only compares.
- **Accumulator ordering rule.** The runner receives its `case_id`s **sorted**, so an
  accumulator may depend only on cases whose `case_id` sorts **before** the emitting case's.
  Zero-padded resolutions (`n032` < `n064` < `n128`), zero-padded shifts, and suffix-extended
  derivatives satisfy this naturally; the trap is a derived case sorting ahead of its base
  (`..._dts050` before `..._dts100`). `test_case_set` declaration order is NOT the run order.
- **Metrics-basis values must not be uniformly zero.** The runner fills
  `raw/metrics_basis.json` from the final capture of each test's target cases (the
  same serialized values as `raw/state_snapshots/<case_id>.json`, never a re-read of
  the storage), so the bound variables must hold the values the run computed. The
  zeros a rejected guard case leaves are admissible only alongside cases that hold
  real values; a metrics_basis zero-filled across the whole run fails `post_execute`
  (`trivial placeholder detected`). The exact rejection condition is canonical in
  `RUNNER_OUTPUT_CONTRACT.md` §3.
- **A callback cannot reach the snapshot through the state.** Each capture of a case is
  taken straight after `case_setup` / `case_run` returns, before the `get_time` that
  follows it and before every `checks_compute` / `metric_compute` call, and the runner
  keeps the serialized copy — whatever a callback writes into a bound variable afterwards
  is invisible to that capture and to the metrics basis (a write from the initial
  capture's `get_time` reaches the final capture exactly as a write in `case_run` would:
  it is the state). Callbacks compute from the state;
  they do not stage it. (A callback that rewrote the snapshot FILE would be the §4 file-I/O
  prohibition broken; the record of what the gate does and does not see is
  `docs/design/zero_base_architecture.md`, the Z6 item.)

## 3. Module-level state is expected

The runner calls `case_setup`, captures, calls `case_run`, captures again, and then
the check and metric callbacks, for one case at a time. The current case's state
lives in the **bound module-level variables** of §1-b — that is what the captures
read — and any cross-case accumulators a metric needs live in **other** module-level
variables. Key any cross-case accumulation by `case_id`.

A cross-case reduction (§2) uses exactly this: accumulate each case's contribution as it runs;
when `metric_compute` is called for the case that completes the reduction, return the derived
value (`found` true), and `found` false for the earlier cases.

## 4. Prohibitions

- **No reference to the harness module** from EITHER the checks module or the model. The
  physics node never depends on the harness module at the source level — the rendered runner is
  its sole consumer. (The harness's model object is linked via the dependency closure, but the
  physics sources must not name it.)
- **No file I/O in the checks module** — no file is opened or written, and none of
  `verdict.json` / `aggregate_verdict.json` / `summary.json` / `trial_meta.json` appears in it,
  even as a comment or example string. Emission is the harness's exclusive job; the checks
  module only computes.

## 5. Language binding

The spelling of §1-§4 in a target language, and that language's legality and gate-guard rules
for every leaf-authored source of a `Generate` node, are the language backend's:
`docs/backends/language/<language>/CHECKS_ABI.md` (its §1-§4 bind §1-§4 here; its §5 is the
gate-guard rule set). The backend reaches it through its `checks_abi` capability
(`tools/backends/registry.py`).
