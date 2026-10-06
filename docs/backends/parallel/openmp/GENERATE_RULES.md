# Generate rules — `openmp`

> **Audience: the maintainer of the `Generate` gates, and the operator reading a refusal.**
> No leaf reads this document: a producer is told these rules by the `openmp` prompt fragments
> (`tools/prompt_templates/backends/parallel/openmp/`), and a model source is held to them by the
> code named below. Moved from `docs/workflow/phases/phase_02_generate.md` (issue #424), which
> states the presence floor and the target-lowering obligation neutrally and points here.

## 1. The default lowering

On a target whose parallel backend is `openmp`, a bundle's `target_lowering_plan` that names no
parallel model gets OpenMP: the target's backend is the default model. For a `cpu` `fortran`
target the default is `!$omp parallel do` on the parallelizable loops, declared as
`"parallelization": {"model": "openmp", …}`.

- **A default, not a rule.** Beyond the presence floor (§2), which loops carry a directive, the
  schedule / chunk / collapse, and every other lowering choice are the producer's, stated in its
  plan; `Generate.verify` G6 holds the source to the plan, and to this default only where the
  plan is silent (`docs/workflow/phases/phase_02_generate.md` §2-2, G6).
- **Never force a directive onto a loop that is not parallelizable** (a carried dependence, a
  reduction that cannot be expressed correctly). Declare `"model": "none"` and say why in the
  plan; the reviewer holds that declaration to the loops, and a `none` over plainly
  parallelizable loops is itself a finding.
- **What already counts as parallel** is the construct of the node's language that IS the
  parallelism. In a Fortran source a `do concurrent` loop needs no directive beside it
  ([GENERATE_RULES.md](../../language/fortran/GENERATE_RULES.md) §4).
- **Directives are compiled, not ignored.** On an `openmp` target the `Generate.gate` syntax
  check passes the compiler's OpenMP flag (for `fortran`, `-fopenmp`:
  [GENERATE_RULES.md](../../language/fortran/GENERATE_RULES.md) §2), so a malformed clause is a
  syntax error. A Fortran directive too long for the linter's column limit continues by repeating
  the sentinel on the next line (`!$omp parallel do &`, then `!$omp& schedule(static)`); a bare
  `&` continuation of a directive is a syntax error.

## 2. The presence floor

The `Generate.gate` static check refuses a model source on an `openmp` target when ALL of the
following hold. The pieces are this backend's `parallel_directives`
(`tools/backends/parallel/openmp/directives.py`), the language backend's
`source_reading.counted_loops`, and the validator that composes them
(`validate_pipeline_semantics._validate_parallel_presence_floor`).

| condition | owner |
|---|---|
| the node is a `component` or `problem` node (an `infrastructure` node is host measurement code and is never asked) | the validator |
| the target's (language, hardware class) pair has a floor; the one pair stated is (`fortran`, `cpu`), and any other pair has none | `directives.presence_floor` |
| the bundle's `target_lowering_plan` does not explicitly decline OpenMP | `directives.lowering_plan_declines` |
| the model source counts at least one loop | the language's `counted_loops` |
| the model source holds not one directive | `directives.presence_floor(...).directive` |

- **A directive** is the `!$omp` sentinel at the start of a physical line, after blanks only
  (space, tab, form feed), compared case-insensitively. A mention inside a comment or a string,
  and a commented-out `!!$omp`, is not one. Why a line-start anchor is enough for a Fortran source
  is [GENERATE_RULES.md](../../language/fortran/GENERATE_RULES.md) §4.
- **Declining.** The plan declines OpenMP when a model-bearing member of its `parallelization`
  object (`model`; `method`, `scheme` and `kind` are read too, the spellings the former
  Compile-authored knob layer used) names no parallelism (`none`, `off`, `serial`, `sequential`,
  `false`, `disabled`) or another model, and no model-bearing member names OpenMP (a substring
  match, so `openmp+simd` names it). A plan with no `parallelization` object, no model-bearing
  member, or a value that is not a non-empty string declines nothing, and the floor applies: the
  plan is the reopened producer's own, so reading silence as an exemption would let it switch its
  own floor off (issue #284).
- **Sources out of the floor's reach** count no loop and pass: a whole-array source, and for
  `fortran` a source holding a `do concurrent` or a `do` header that wraps before it can be
  classified ([GENERATE_RULES.md](../../language/fortran/GENERATE_RULES.md) §4, which also lists
  the `do` shapes that are not counted).
- **The finding** names the model file and its counted loops, and tells the producer to add
  `!$omp parallel do` to the parallelizable loops, or — only for loops that cannot be
  parallelized — to declare `"model": "none"` with the reason.

## 3. What no gate sees

The floor is a PRESENCE floor. A directive on some loops only, a schedule other than the plan's,
an incorrect parallelization, and the honesty of a declared `none` all pass it; they are
`Generate.verify` G6's judgment, told in the `openmp` reviewer fragment. Where the floor does not
run — an `infrastructure` node, a source out of its reach (§2), a plan that declines — whether the
source reflects the plan at all is G6's too. The floor reads the model source only: how a checks
module's loops are lowered is never a finding (issue #400).

## 4. The launch environment

A binary built for `openmp` is launched with `OMP_NUM_THREADS` and `OMP_THREAD_LIMIT` both set to
the target profile's `execution.threads_per_rank` (`tools/backends/parallel/openmp/execution.py`, the
`execution_env` capability, read by `tools/host_execution.py`). The limit caps a nested or
`num_threads(...)` region at the width the profile states.
