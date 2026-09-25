# Checks-module ABI — the Fortran binding

> **Audience: the `Generate.generate` / `Generate.verify` leaves of a node whose target
> profile names `toolchain.language: fortran`.** This document binds the language-neutral
> checks-module contract (`docs/workflow/CHECKS_MODULE_CONTRACT.md`) to Fortran, section by
> section: its §1-§4 here say how the neutral §1-§4 are spelled in Fortran, and §5 is the
> legality and gate-guard rule set every leaf-authored Fortran source of a `Generate` node is
> held to. It is reached through the Fortran language backend's `checks_abi` capability
> (`tools/backends/language/fortran/checks_abi.py`). The host inlines the neutral §1-§4
> followed by §1-§4 of this document into the `generate.verify` reviewer's prompt as
> `checks_module_contract_document`, and §5 of this document into the `harness`-shape
> producer's prompt as `gate_guards_document`. Compile, which is target-free, is shown the
> neutral §1-§4 only.

The source files are `<spec_id>_model.f90` (the physics kernel and the published operation)
and `<spec_id>_checks.f90` (the checks module, `module <spec_id>_checks`). The runner,
`<spec_id>_runner.f90`, and `src/Makefile` are host-rendered.

## 1. The fixed ABI in Fortran

`module <spec_id>_checks` publishes **exactly** the five non-prefixed procedure names of the
neutral §1 (module scope makes them collision-free — the harness's symbols are all
`harness_fortran_cpu__*` and the model's are `<spec_id>__*`, so a bare `case_run` cannot clash;
non-prefixed names keep every identifier under the f2008 63-character limit, which a
`<spec_id>__checks_compute` would exceed for a long `spec_id`) plus the bound state variables of
§2. Each is a SUBROUTINE, because the runner reaches it with a `call`; a FUNCTION of any of these
names cannot satisfy the ABI. Author them verbatim:

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

Pinned width: `status` is `character(len=4)` (the rendered runner declares a matching actual),
and its three values are `'pass'`, `'fail'` and `'na  '` (right-padded). The check id is a
runner-supplied `intent(in)` actual, so no width is pinned for it. `reason_na` is a
deferred-length allocatable. `real(dp)` is `real64` from `iso_fortran_env`.

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

## 2. The bound state in Fortran

Every snapshot variable of the neutral §1-b is a **module-level `real(dp)` variable of
`<spec_id>_checks`, named exactly as the IR names it** (Fortran identifiers are
case-insensitive, so keep the IR's spelling) — a rank-N `shape_expr` as
`real(dp), allocatable :: <name>(:, ...)` with N colons, a `shape_expr: scalar` as
`real(dp) :: <name>` — and is listed in a `public ::` statement of the specification part.
The host-rendered runner imports each one directly
(`use <spec_id>_checks, only: sb_<name> => <name>`), tests `allocated(sb_<name>)` before it
reads an array, and serializes it through the harness emitters at the two capture points.

Publication is checked under the same scan as the ABI names: under a bare `private` a
`public ::` statement must name the variable; under the default-public accessibility it is
published unless a `private ::` names it. A rejected case (`case_setup` returning
`ok = .false.`) still leaves every bound array allocated to its declared shape.

## 3. Module-level state in Fortran

The current case's state lives in the bound module-level variables of §2, and a cross-case
accumulator in other module-level variables of `<spec_id>_checks`. Nothing else is
language-specific here.

## 4. Prohibitions in Fortran

- **No `use harness_*`** in EITHER `<spec_id>_checks.f90` or `<spec_id>_model.f90`, in any
  spelling (`use harness_...`, `use :: harness_...`, `use, non_intrinsic :: harness_...`).
  The rendered runner is the sole `use harness_fortran_cpu_model` site. (The harness's
  `<spec_id>_model.o` is linked via the closure, but the physics sources must not name it.)
- **No file I/O in the checks module** — no `open` / `write(unit=...)` to a file.

## 5. Fortran legality and gate guards

This section applies to every leaf-authored Fortran source of any `Generate` node
(the model, this checks module, and the hand-authored runner of an `infrastructure`
node's self-test).

- **`intent(out)` character dummies** must be fixed-length (`character(len=4)`)
  or a deferred-length allocatable (`character(len=:), allocatable`) — an
  assumed-length `intent(out)` (`character(len=*)`) is illegal, matching the
  harness's own rule.
- **`metric_compute`'s `reason_na` keeps its §1 declaration on every node.** The
  runner passes an UNALLOCATED deferred-length actual for it, and a fixed-length dummy
  passes the lint gate, the syntax check and Build, then faults at the first call (the
  assumed-length form is the lint gate's, per the first bullet above). The bundle
  acceptance gate refuses a dummy without the attribute (`m3c_checks_abi_violation`,
  issue #261) — a no-metrics stub included; the rendered runner states the declaration
  in a comment.
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
