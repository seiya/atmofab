# Checks-module ABI — the Fortran binding

> **Audience: the `Generate.generate` / `Generate.verify` leaves of a node whose target
> profile names `toolchain.language: fortran`.** This document binds the language-neutral
> checks-module contract (`docs/workflow/CHECKS_MODULE_CONTRACT.md`) to Fortran, section by
> section: its §1-§4 here say how the neutral §1-§4 are spelled in Fortran, and §5 is the
> legality and gate-guard rule set every leaf-authored Fortran source of a `Generate` node is
> held to. It is reached through the Fortran language backend's `checks_abi` capability
> (`tools/backends/language/fortran/checks_abi.py`). The host seeds the neutral §1-§4
> followed by §1-§4 of this document into the `generate.verify` reviewer's work tree as
> `CHECKS_MODULE_CONTRACT.md`, and §5 of this document into the `harness`-shape producer's
> work tree as `gate_guards.md`. Compile, which is target-free, is seeded the neutral §1-§4
> only.

The source files are `<spec_id>_model.f90` (the physics kernel and the published operation)
and `<spec_id>_checks.f90` (the checks module, `module <spec_id>_checks`). The runner,
`<spec_id>_runner.f90`, and `src/Makefile` are host-rendered.

## 1. The fixed ABI in Fortran

`module <spec_id>_checks` publishes **exactly** the five non-prefixed procedure names of the
neutral §1 (module scope makes them collision-free — the harness's symbols are all
`harness_fortran_cpu__*` and the model's are `<spec_id>__*`, so a bare `case_run` cannot clash;
non-prefixed names keep every identifier under the f2008 63-character limit, which a
`<spec_id>__checks_compute` would exceed for a long `spec_id`) plus the bound state variables of
§1-b. Each is a SUBROUTINE, because the runner reaches it with a `call`; a FUNCTION of any of these
names cannot satisfy the ABI. Author them verbatim:

```fortran
public :: case_setup, case_run, get_time
public :: checks_compute, metric_compute

! Initialize this case's state from the spec's fixed inputs/constants.
subroutine case_setup(case_id, ok)
  character(len=*), intent(in) :: case_id
  logical, intent(out) :: ok
end subroutine case_setup

! Run the model kernel's time loop for this case by `call <spec_id>__<op>(...)`
! (from here or from a procedure of this module called from here) and return the perf
! counters.
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

### 1-b. The bound state in Fortran

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
published unless a `private ::` names it. A rejected guard / xfail case still leaves every bound array allocated to its declared shape.

### 1-c. The distributed binding in Fortran

On a target whose harness provides `distributed_state@2` (issue #512) every rank holds its
own copy of the module's storage, the ranks form the process grid the host imposes, and the
runner gathers each partitioned array onto
rank 0 before it serializes it. Beside every bound ARRAY `<name>` the module publishes (`public
::`, imported under the same names) eight default-integer variables, which `case_setup` sets on
every rank for every case, a rejected one included — four per grid dimension `<d>` (`1`, `2`):
`sb_<name>_axis<d>`, the axis of `<name>` partitioned along dimension `<d>` (`0` = not
partitioned along it; `0` in both = replicated, rank 0's copy is written); `sb_<name>_lo<d>` /
`sb_<name>_hi<d>`, the indices along that axis, in the array's own declared bounds, of the
cells this rank contributes to the GLOBAL array — the array as the IR's `shape_expr` declares
it — and `sb_<name>_glo<d>`, the global index (from `1`, in that shape) of the cell at
`lo<d>`. A halo cell is never contributed; when the declared shape includes the domain's
boundary ghost cells, the first rank along `<d>` contributes the leading ones and the last the
trailing ones. `hi<d> < lo<d>` when a rank contributes nothing (the array still allocated);
along a dimension the array is not partitioned along, only the coordinate `0` contributes its
copy; an axis not partitioned holds its declared extent. A scalar is replicated. The
harness's gather stops the run unless the contributed blocks tile the global array. `case_run` reports the
cells THIS rank updated; `checks_compute` / `metric_compute` run on every rank and rank 0's
result is written, so a whole-field value is a harness reduction. A physics source `use`s the
harness only with an `only:` list of its distributed-state operations
(`docs/backends/parallel/mpi/GENERATE_RULES.md`).

## 2. The semantics, spelled in Fortran

The neutral §2 is procedure semantics; what it leaves to the language is the spelling of its
values. `status` is
`'pass'`, `'fail'` or `'na  '` (width 4, right-padded); an honestly unavailable metric sets
`found = .true.`, `is_na = .true.` and `reason_na` (deferred-length, allocated by the
assignment), and a metric that does not apply to the case sets `found = .false.`; a rejected
case's bound arrays are still allocated, e.g. filled with `0.0_dp`.

## 3. Module-level state in Fortran

The current case's state lives in the bound module-level variables of §1-b, and a cross-case
accumulator in other module-level variables of `<spec_id>_checks`. Nothing else is
language-specific here.

## 4. Prohibitions in Fortran

- **No `use harness_*`** in EITHER `<spec_id>_checks.f90` or `<spec_id>_model.f90`, in any
  spelling (`use harness_...`, `use :: harness_...`, `use, non_intrinsic :: harness_...`),
  except §1-c's `use <harness>_model, only: <distributed-state operations>` on a harness that
  offers them. The rendered runner is the sole site that uses the harness's plumbing. (The
  harness's `<spec_id>_model.o` is linked via the closure.)
- **No file I/O in the checks module** — no `open` / `write(unit=...)` to a file.
- **The model's operation is named in two places only.** In the checks source and every other
  leaf `.f90` but the model, a `<spec_id>__<op>` the model defines appears only in a
  `use <spec_id>_model` statement — the one in the module's specification part is what makes
  `case_run`'s call the model's — and right after `call`; never as a declaration, procedure
  pointer, interface body, dummy, construct name, generic name, actual argument or definition
  (`source.checks_model_reach_violations`).

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
- **A §5.1 module parameter (`dp`, `case_id_len`, …) has ONE binding in the model file**,
  the model module's own `parameter`. An import counts as a binding, so a further module in
  that file spells the kind or length under a name of its own (`real64`, a literal length)
  rather than importing it. Any other file imports it from `<spec_id>_model` normally.
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
- **No derived type that contains itself through allocatable components.** A component
  `type(t), allocatable :: children(:)`, or a scalar `type(t), allocatable :: child`, inside
  `type :: t` is conforming f2008 that the `Generate.gate` syntax check, the lint check and
  Build all accept, and the GNU Fortran release the `fortran` targets build with mishandles
  it: copying or appending a node whose own children are populated (an array constructor
  and `allocate` + `move_alloc` alike) frees the same memory twice, and the program aborts
  (`double free detected`), faults (SIGSEGV) or hangs. The ban covers a cycle through another
  type as well (`node` holds `holder`, `holder` holds an allocatable `node`): on that form the
  compiler itself never finishes, allocating memory without bound. No gate reads for either
  form; the direct one surfaces at `Validate.execute`, where the run restarts `Generate`
  without the cause. Represent a tree — a parsed document, a nested structure — as a flat
  array of nodes indexed by integers, each node holding the indices of its children rather
  than the children, or consume the input without building a tree. A type that no chain of
  components leads back to (a deferred-length character, a `real(dp)` array, an integer
  index array, a component of another non-recursive type) is unaffected, and so is a
  `pointer` component of the type's own type.
- **A structure constructor is not given a deferred-length character component.** Inside
  `type :: t` with `character(len=:), allocatable :: id`, the constructor `t(src(j)%id, 'pass')`
  — its value for `id` being a deferred-length character component of any object: a scalar of
  this type or another, an array element, or the object being assigned (`x = t(x%id, 'fail')`)
  — is conforming f2008 that the `Generate.gate` syntax check, the lint check and Build all
  accept, and the GNU Fortran release the `fortran` targets build with gives `id` an EMPTY value
  (length 0) in the new object, at every optimization level. The same constructor given a
  literal, a local deferred-length variable the component was assigned to, or an expression
  (`trim(src(j)%id)`, `src(j)%id // ''`) is correct, and so is appending the element itself
  (`[acc, src(j)]`); the array constructor is not the trigger (a scalar
  `one = t(src(j)%id, 'pass')` is empty too). No gate reads for it: the defect surfaces where
  the runner's output is checked at `Validate.execute` (every folded id is `""`, so the
  output's checks reduce to one `""` entry, which the post-execute check refuses). Copy such a
  component through a local DEFERRED-LENGTH variable or an expression, or assign the new
  element's components one by one with the subscript an integer variable or arithmetic on one
  (`k = size(acc)`, then `acc(k)%id = src(j)%id`), rather than through the constructor. A
  fixed-length local is not a substitute: `t(trim(key), 'pass')` with `character(len=32) :: key`
  gives `id` length 32 at `-O2`, the trimmed text padded with NUL bytes; `trim(key) // ''` and
  `key(1:len_trim(key))` are correct. On the same release, assigning a deferred-length
  component through a subscript that calls `size`, `min` or `max` (the forms measured) faults
  at run time (SIGSEGV) or leaves the component unallocated or empty, depending on the program.
