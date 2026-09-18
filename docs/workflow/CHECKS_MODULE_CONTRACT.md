# Checks-module contract (`<spec_id>_checks.f90`, fixed ABI)

> **Audience: the `Generate.generate` / `Generate.verify` leaves of an *M3c
> physics node* — a `build_system=make`, `language=fortran` node with exactly
> one `infrastructure` (runner-harness) dependency.** On such a node the leaf
> authors **two** Fortran sources: `<spec_id>_model.f90` (the physics kernel +
> the published `__apply` operation) and `<spec_id>_checks.f90` (this contract).
> It does **not** author `<spec_id>_runner.f90` or `src/Makefile` — those are
> host-rendered by the conductor (`tools/host_render.py` / `_write_makefile`) and are outside the leaf's `allowed_output_paths`. This is
> not read by `Validate.judge`.
>
> NO leaf reads this document from disk — none has held a tool to read one with since Z4
> ([issue #171](https://github.com/seiya/atmofab/issues/171)), and the force-read set that used to
> deliver it is deleted. It reaches its leaves two ways, both inlined by the host: the `generate.generate` producer is told the five names and the binding
> convention and shown the rendered runner, gated by `m3c_checks_abi_violation`, with the §2/§3
> behavioral contract distilled into
> `pure_generate_generate.txt`; the `generate.verify` reviewer receives §1-4 of this document
> inlined as `checks_module_contract_document` (issue #142). (pure-8's runner-driven per-id `checks_compute`
> takes each id as a literal actual, so a dropped id is impossible — the former
> `m3c_checks_ids_violation` gate is gone.) All five bind every node. The language backend that
> renders the runner owns the ABI (`host_render.checks_public_names`) — keep it, this doc, and
> the distilled paragraph in step. Since Z6 ([issue #255](https://github.com/seiya/atmofab/issues/255))
> the module also carries the node's **bound state** (§1-b): the snapshot getters
> (`get_scalar` / `get_r1..r4`) are retired, and the runner reads the snapshot variables straight
> from module-level storage.
>
> **§5 (Fortran legality and gate guards) is the one section with a wider
> scope**: it binds **every** `Generate` leaf that authors Fortran — including the
> non-M3c node kind (an `infrastructure` harness self-test) that hand-authors its
> own `<spec_id>_runner.f90`. Sections
> 1-4 (the checks-module ABI) apply to an M3c node only.

The rendered runner is glue: it drives this module's callbacks, captures the
module's bound state, and emits the standard runner outputs **through the certified
`harness_fortran_cpu` plumbing**, which owns all JSON serialization and the verdict
fold. So the checks module holds **no serialization and no I/O**: it holds the
case's state and computes honest per-case checks and metrics; the runner captures,
and the harness folds and writes. Getting this split right is what lets the runner
be host-rendered (deterministic, no leaf regenerate loop), and it is what makes the
snapshot **primary evidence** (`docs/design/zero_base_architecture.md` §A4): the
generated code contributes the binding — the declaration and the allocation of the
state variables — and nothing downstream of it.

## 1. The fixed ABI

`module <spec_id>_checks` publishes **exactly** these five non-prefixed procedure
names (module scope makes them collision-free — the harness's symbols are all
`harness_fortran_cpu__*` and the model's are `<spec_id>__*`, so a bare
`case_run` cannot clash; non-prefixed names keep every identifier under the
f2008 63-character limit, which a `<spec_id>__checks_compute` would exceed for a
long `spec_id`) plus the bound state variables of §1-b. Author them verbatim:

```fortran
public :: case_setup, case_run, get_time
public :: checks_compute, metric_compute

! Initialize this case's state from the spec's fixed inputs/constants. ok=.false.
! rejects a guard / xfail input (e.g. an invalid grid size) — the case still
! proceeds so its snapshot + input_guard check are produced.
subroutine case_setup(case_id, ok)
  character(len=*), intent(in) :: case_id
  logical, intent(out) :: ok
end subroutine case_setup

! Run the model kernel's time loop for this case and return the perf counters.
! A non-time-stepping component uses steps=1 and cells_updated = cells touched.
subroutine case_run(case_id, steps, cells_updated, ok)
  character(len=*), intent(in) :: case_id
  integer, intent(out) :: steps, cells_updated
  logical, intent(out) :: ok
end subroutine case_run

! The scalar time value of this case at the capture point (real(dp), 0.0 for an
! untimed component). Called right AFTER each of the two captures, for the time the
! snapshot is written with — so no generated procedure runs between case_setup /
! case_run returning and the state being serialized.
subroutine get_time(t)
  real(dp), intent(out) :: t
end subroutine get_time

! The honest per-case result for ONE check. The runner calls this once per
! (case, check id), supplying `check_id` (a literal from the IR's
! diagnostics_contract.checks[].id); you author no id and set `status` (one of
! 'pass'/'fail'/'na  ', width 4) on EVERY `check_id` branch, `case default`
! included. An xfail case's failing guard still reports 'fail' (the harness folds).
subroutine checks_compute(case_id, check_id, status)
  character(len=*), intent(in) :: case_id
  character(len=*), intent(in) :: check_id
  character(len=4), intent(out) :: status
end subroutine checks_compute

! One diagnostics_contract.metrics leaf (dotted address, e.g. 'error.l2') for
! this case. `found` is .false. when this case does not produce that metric (it
! is then omitted). `is_na`/`reason_na` carry an honestly-unavailable value.
subroutine metric_compute(case_id, name, val, is_na, reason_na, found)
  character(len=*), intent(in) :: case_id
  character(len=*), intent(in) :: name
  real(dp), intent(out) :: val
  logical, intent(out) :: is_na
  character(len=:), allocatable, intent(out) :: reason_na
  logical, intent(out) :: found
end subroutine metric_compute
```

Pinned width: `status` is `character(len=4)` (the rendered runner declares a
matching actual). The check id is a runner-supplied `intent(in)` actual, so no
width is pinned for it. `reason_na` is a deferred-length allocatable.

The five names must be published from `module <spec_id>_checks` **itself**. The
`Generate.gate` static check (`_validate_checks_source_files`) resolves the published set
from that module alone: the names its `public` statements list, plus — only while
the module keeps Fortran's default public accessibility — the procedures the module
**defines at module level**, minus the names any `private`
statement hides. Authoring the two `public ::` lines above verbatim, in the
specification part (never inside a procedure body), satisfies the gate under either
accessibility default; a bare module-level `private` without them publishes nothing
and fails. Where the fallback applies, a name that is only prototyped in an
`interface` block, defined as an internal procedure of another procedure, or defined
in a submodule / a second module / after `end module` does not count as defined.

### 1-b. The bound state

Every variable the IR declares under
`io_contract.raw_requirements.required_evidence[artifact: state_snapshots].schema.variables[]`
is a **module-level `real(dp)` variable of `<spec_id>_checks`, named exactly as the IR
names it** — a rank-N `shape_expr` as `real(dp), allocatable :: <name>(:, ...)` with N
colons, a `shape_expr: scalar` as `real(dp) :: <name>` — and is listed in a `public ::`
statement of the specification part. The host-rendered runner imports each one directly
(`use <spec_id>_checks, only: sb_<name> => <name>`) and serializes it through the harness
emitters at two capture points per case: right after `case_setup` (written to
`raw/state_snapshots/initial/<case_id>.json`) and right after `case_run` (written to
`raw/state_snapshots/<case_id>.json`), both **before** the first `checks_compute` /
`metric_compute` call of that case. There is no getter: no procedure of this module
computes, filters or returns a snapshot value, and a value the checks compute (a norm,
a maximum, a flag, an echoed input) is a metric, not a snapshot variable.

The bundle declares this convention as its `state_bindings[]` — one entry per snapshot
variable, `storage_symbol == state_variable`, `module == <spec_id>_checks`,
`capture: harness_registration`, `capability: state_registration@1`
(`docs/workflow/CODEGEN_BUNDLE_CONTRACT.md` §State bindings) — and the host checks the
declaration against the IR, then requires each bound variable **published** under the
same scan as the ABI names: under a bare `private` a `public ::` statement must name it;
under the default-public accessibility it is published unless a `private ::` names it.
An unallocated bound array at a capture point stops the run (`bound state <name> is not
allocated at capture`), so `case_setup` allocates every array to its declared shape on
every path, the rejected-input path included.

## 2. Semantics the harness relies on

- **Bound state is shape-valid at both capture points, even for a rejected case.** A
  guard/xfail case whose `case_setup` returned `ok=.false.` must STILL leave every
  bound array allocated to its declared shape and every bound scalar defined (the
  runner always captures the case's state, right after `case_setup` and right after
  `case_run`). Leave a defined placeholder (e.g. zeros of the right shape), never an
  unallocated array. `case_run` updates the state in the bound variables in place —
  a state kept in a private copy is one the capture never sees.
- **`checks_compute` is honest, never judgmental.** The runner calls it once per
  (case, IR check id). Report `'fail'` when a check fails, even for an xfail case. The harness `__write_diagnostics` computes the
  per-case verdict (`overall == 'fail'` iff any of that case's checks is
  `'fail'`), the top-level fold, and the **xfail exclusion** (a failing case
  whose `expected_xfail` is true — supplied by the runner from the IR predicates
  — is excluded from the top-level `failed_checks`). Do not pre-adjust for xfail
  here; doing so double-counts and breaks the guard-passes-at-top-level rule.
- **NA metrics.** When a metric is honestly unavailable set `found=.true.`,
  `is_na=.true.`, and `reason_na` to a short reason; the harness encodes it as
  `"<address>": null` plus a sibling `"<address>_reason_na": "<reason>"`. A
  metric that simply does not apply to a case sets `found=.false.` (omitted).
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
  `order_n064_to_n128`. Earlier cases return `found=.false.` for it (a sparse metric is
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
value (`found=.true.`), and `found=.false.` for the earlier cases.

## 4. Prohibitions

- **No `use harness_*`** in EITHER `<spec_id>_checks.f90` or
  `<spec_id>_model.f90`. The physics node never depends on the harness module at
  the source level — the rendered runner is the sole `use harness_fortran_cpu_model`
  site. (The harness's `<spec_id>_model.o` is linked via the closure, but the
  physics sources must not name it.)
- **No file I/O in the checks module** — no `open` / `write(unit=...)` to a file,
  no `verdict.json` / `aggregate_verdict.json` / `summary.json` / `trial_meta.json`
  even as a comment or example string. Emission is the harness's exclusive job;
  the checks module only computes.

## 5. Fortran legality and gate guards

This section applies to every leaf-authored Fortran source of any `Generate` node
(the model, this checks module, and the hand-authored runner of an `infrastructure`
node's self-test).

- **`intent(out)` character dummies** must be fixed-length (`character(len=4)`)
  or a deferred-length allocatable (`character(len=:), allocatable`) — an
  assumed-length `intent(out)` (`character(len=*)`) is illegal, matching the
  harness's own rule.
- **`metric_compute`'s `reason_na` keeps its §1 declaration on every node.** The
  runner passes an UNALLOCATED deferred-length actual for it; a fixed-length or
  assumed-length dummy passes the syntax check and Build and faults at the first call.
  The bundle acceptance gate refuses it (`m3c_checks_abi_violation`, issue #261) — a
  no-metrics stub included; the rendered runner states the declaration in a comment.
- **`spec_id` ≤ 55 characters** so the derived `<spec_id>_checks` / `_runner` /
  `_model` identifiers stay within the f2008 63-character limit (on an M3c node the
  renderer fails closed above this).
- Author lint-clean f2008 (`use ..., only:`, a plain `implicit none` with NO allow
  directive above it, lines UNDER 100 columns — 99 is the longest that passes on every
  supported linter build) — the deterministic `Generate.gate` lint check lints the whole
  `src/` tree, every leaf-authored source included.
- **Intentionally-unused dummy arguments.** The deterministic `Generate.gate` syntax check
  compiles the whole staged source set with
  `-Werror=unused-dummy-argument -Werror=unused-variable -Werror=ampersand`, so an
  unreferenced dummy argument in any leaf-authored source is a compile failure. When an
  **interface fixes** the dummy — an inert input the algorithm defines as unused, an
  ABI-fixed argument such as `name` / `case_id` — it stays a live `intent(in)` dummy and is
  bound immediately after its declarations with
  `associate (unused_<name> => <name>); end associate`. An arithmetic no-op (`0*<name>` and
  equivalents) is forbidden as the binding. `! allow(...)`
  does not suppress this class, and no allow directive is legitimate anywhere: the lint
  gate runs `--ignore-allow-comments`, so one is inert rather than honoured
  (`tools/backends/linter/fortitude/lint.py`) — the finding it names fires regardless.
- **A dummy argument no interface fixes is deleted, not bound.** The `associate` binding
  exists only to keep a signature no leaf owns intact. In a private helper the leaf itself
  declared, an unused dummy is removed from the signature and from every call site; binding
  it would freeze dead surface. An unused local variable is likewise not declared at all.
- **`intent(out)` dummies the body never sets** are the same promoted class
  (`-Werror=unused-dummy-argument` also rejects *"Dummy argument … was declared INTENT(OUT)
  but was not set"*). Every `intent(out)` dummy of every leaf-authored procedure is assigned
  on every path, including the degenerate one: a `metric_compute` with no metric still
  assigns `val` / `is_na` / `reason_na` and `found = .false.`.
- **A continued character literal resumes with a leading `&`** — the third promoted class
  (`-Werror=ampersand`). gfortran accepts a resume line without one as an extension, which
  put a counted-`do` spelling written inside a string at a physical line start, where the
  line-anchored `!$omp` presence floor counted it; issue #25 promotes the class so it cannot.
  Write `'a message that is &` / `      &continued'`; the same wrap with the resume `&`
  omitted is a compile failure.
