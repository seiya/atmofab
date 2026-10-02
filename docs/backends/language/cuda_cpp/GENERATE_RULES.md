# Generate rules — the CUDA C++ binding

> **Audience: the maintainer of the CUDA C++ `Generate` prompts and gates.** No leaf reads this
> document from disk; the rules it states reach a pure `generate.generate` leaf through the CUDA
> C++ prompt fragments (`tools/prompt_templates/backends/language/cuda_cpp/`), the static-lint
> rule set the linter backend renders (`tools/backends/linter/nvcc/lint.py`
> `lint_rules_document`), and `docs/backends/language/cuda_cpp/CHECKS_ABI.md` §5. It is the
> canonical checklist behind them (`docs/workflow/phases/phase_02_generate.md` §2-1 states the
> language-neutral rules and points here).

## Scope

The CUDA C++ backend serves every node of a `cpp_gpu`-style target: the `infrastructure` harness,
whose leaf authors the model and the runner, and the `component` / `problem` nodes, whose leaf
authors the model and the checks source while the host renders the runner and the checks header
over the certified harness (`tools/backends/language/cuda_cpp/runner.py`, issue #289 R4-b PR-6).

## 1. Lint idioms

- The lint check is the CUDA compiler driver with every warning an error
  (`docs/backends/linter/nvcc/RULES.md`), so the idioms are those of a warning-free build under
  `-std=c++17`: no unused parameter (mark an interface-fixed one with `(void)name;`), no unused
  variable, no signed/unsigned comparison, a `return` on every path of a non-`void` function.
- Only `#include` and `#pragma unroll`: every other directive, `_Pragma`, `##` and every digraph
  is refused by the static check, and so are a `<...>` include of anything but a standard library
  header or `cuda_runtime.h`, a reserved identifier other than the CUDA keywords, and a
  backslash at the end of a line (`CHECKS_ABI.md` §5).

## 2. The syntax stage

The mandatory syntax stage for `cuda_cpp` is
`nvcc -std=<toolchain.standard> -arch=<hardware.architecture> -Xcompiler -fsyntax-only -odir <scratch> -rdc=true -c <sources>`
over every `.cu` of the staged directory (all at its top level: a nested one is refused by the static check), each its own translation unit, with the host-rendered
header staged beside them (`STAGED_SUFFIXES`; `tools/backends/compiler/nvcc/syntax.py`). It promotes no warning class: the lint rule set
already makes every warning an error. `-rdc=true` is the build's relocatable device code
(`BUNDLE_BINDING.md` §4): without it a kernel's call to a `__host__ __device__` operation of
another file fails this stage although the build links it. A failing stage is attributed by re-running the same argv
over a canary translation unit with one kernel; a canary failure is an invocation the driver
refuses (typically a `toolchain.standard` or `hardware.architecture` it does not know) and is a
transport `fail_closed`.

## 3. Model naming and dependency use

- The model source is `<spec_id>_model.cu`: it includes the host-rendered
  `<spec_id>_model.cuh` and defines the published operations in `namespace <spec_id>_model`
  (`BUNDLE_BINDING.md` §1).
- The runner includes the same header; on a physics node it is host-rendered and includes the
  harness's header and `<spec_id>_checks.cuh` (`CHECKS_ABI.md` §1).
- A consumer model uses each direct dependency the way the build links it
  (`source.validate_dependency_operations`): it includes the dependency's host-rendered
  `"<dep>_model.cuh"` (the host copies it into `src/` at Generate start and stages it into the
  object directory at Build), defines no `<dep>__*` function, and calls at least one
  `<dep>__<op>` operation — qualified `<dep>_model::` or not — from a function body.
- A dependency operation its header declares `__host__ __device__` (a pointwise operation of a
  `component`, `BUNDLE_BINDING.md` §2) may be called from inside a kernel the consumer defines, one
  element at a time, on views into device storage: that is a dependency call like any other, and
  the dataflow gate follows it through the kernel's launch (§5).
- A `problem` node's header declares nothing of its operation (its IR has no signatures), so its
  checks source declares the operation itself, in namespace `<spec_id>_model`, with exactly the
  model's definition types; another spelling is refused (`checks_harness_isolation_violations`)
  before it would be a link error at Build.
- Neither the model nor the checks source includes or names the harness, and the checks source
  does no file I/O; no leaf source — the model and every helper included — names a file stream
  or stream buffer, a file opener, renamer or deleter, a command runner, an exit handler
  registration or `asm`, called or not (`CHECKS_ABI.md` §4); the runner ends with `std::_Exit`.

## 4. The parallel presence floor

On a `component` / `problem` node built for `parallel.backend: cuda` on a `gpu`, a model source
with counted `for` loops must define or launch at least one kernel (`__global__` or `<<<`), read
over the code only (`tools/backends/parallel/cuda/directives.py`). The floor does not run on an
`infrastructure` node. A pointwise operation (`BUNDLE_BINDING.md` §2) defines no kernel and
launches nothing, while its body may hold a counted loop over its fixed extent (the three
components of a face): its plan declares `"model": "none"`, with the reason that its consumers'
kernels call it once per element, which exempts it from the floor and which `generate.verify`
holds to the binding (the producer and the reviewer are both told). The device trace
(`DEVICE_TRACE.md`) asks nothing of it; its device path is exercised by the traced runs of the
consumers whose kernels call it.

A failed CUDA call is a failure of the operation: it frees what it allocated and returns without
the result (a checks callback still assigns its `out` arguments). It is not reported through the
IR's input guard, whose formula is the IR's, over the inputs, and the runner does not read
`case_run`'s `ok` (`runner.py`) — so whether a case's checks notice the missing result depends on
what its outputs held, and the device trace below is what holds the rule. A host fallback — a path that recomputes a kernel's
result on the host when an allocation, copy, launch or synchronize fails — is forbidden: it makes a
run whose kernels never executed produce correct-looking state. The producer is told (rule
`target_lowering_floor`), `generate.verify` holds it (checklist G6), and `Validate.execute` reads
for it: the run is traced on the device, and a kernel the node's sources define that no case executed
fails the run (issue #307). Measured on R4-b PR-6's billed run: a flux component with such a
fallback passed Validate at a site where every kernel launch failed.

Neither the floor nor the trace sees whether a kernel does its loop's work: the floor asks that a
kernel exist, and the trace that each defined kernel ran at least once. A loop the plan puts on
the device that the source computes on the host — beside a stub kernel, or behind a branch that
sends every case the run covers to the host (a size threshold above them) — is `generate.verify`'s
to fail (G6). A loop the plan keeps on the host is the plan's claim, held like a declared `none`:
its reason must be about the loop itself (a carried dependence, a reduction, a copy that only
reshapes host state, a check over the host state a checks callback receives), not about a gate or a run in which a kernel did not execute. The
producer is told the same, and told that a kernel the trace never saw is launched where the cases
reach it with a launch configuration the device accepts, and removed only when its work is not
needed, never moved to a host loop (issue #307 PR-3).

## 5. The `problem` model gates

The Fortran binding's three gates, read over the namespace-scope function definitions of the
model source (`source.run_problem_model_gates`); a source whose brackets do not balance is
refused rather than read in part. A parameter is an OUTPUT when the function can write through
it — a non-const reference, a pointer to non-const, a non-const `atmofab::View` — and a returned
value is one more output, whether or not its `return` names anything.

- **Literal outputs.** A function every one of whose output parameters is assigned whole
  (`out = ...;`) only from literals, none depending on an input, is refused; a compound
  `out += ...;` reads the output's previous value and so depends on an input.
- **Dependency dataflow.** What a dependency call writes must reach an output through
  assignments. Its candidates are the names whose storage the call's actuals hand over — at the
  operation's output parameters as the dependency's header `<dep>_model.cuh` beside the model
  declares them, or, without the header, at every position minus `const` / `constexpr` names and
  functions this file defines — minus the enclosing function's parameters and names assigned
  before the call by an assignment statement — a declaration's initializer is not one, as in the
  Fortran binding (an inert call's inputs). An actual's names are its storage (`u`, `&u`,
  `u[i]`, `u.data()`), else every plain name it mentions (a pointer, `as_view(u)`, `w.flux`).
  The closure runs backward from the outputs over assignments `lhs = rhs` (a target's base name,
  `u[i]`, `u.data[i]` and `v.data` included), over a view or
  pointer made to point into another name's storage (`View<...> v{u.data(), ...}`,
  `double* p = u.data();` make `u` take `v` / `p`), and — past the Fortran binding, which follows
  no call — over calls whose parameter directions the types state: a function or kernel the model
  source defines, a dependency operation, `cudaMemcpy` / `cudaMemcpyAsync` (each output actual
  takes the input actuals its callee's BODY lets reach it — a function the model source defines
  is summarized from its body, to a fixed point; a dependency operation from its declaration,
  every input; a copy from its source argument alone). A view built in place carries its first
  element only (`View<...>{p, {n}}` carries `p`, not the extent `n`). A call to anything else —
  a template, which the declaration reader does not read, included — is not followed. The check
  is PER CALL: at least one of the names each dependency call writes must reach an output, and
  when the operation's header gives it an ARRAY output (a non-const view, an owning array or
  vector reference, a pointer), one of its array outputs — a guard flag reaching `ok` does not
  stand for a discarded flux. The Fortran binding pools the candidates of every call; neither
  checks every written name, and `Generate.verify` G5 is the authority on the rest. A call to a
  function or kernel the model source defines that calls a dependency operation — directly or
  through another such function — is a dependency call of its caller too, whose candidates are
  the actuals at those of the callee's output parameters that the dependency's result reaches
  through the callee's body (issue #380): inside a kernel, the dependency's result is written
  into the kernel's own output pointer, which the kernel's check exempts as an output, so what
  must be shown is that the buffer the launch hands it reaches the caller's outputs. A guard flag
  the kernel also writes (`int* bad`) does not stand for the flux, and an integer index
  (`View<double, 1>{f + 3 * i, {3}}` hands over `f`, not `i`) is not a result.
- **Metric-only scalar kernel.** On a multi-dimensional `problem` node, a function with five or
  more outputs and neither an array parameter (`atmofab::View`, `atmofab::Array`, `std::vector`, a
  pointer) nor a loop (`for`, `while`, a `<<<` launch) is refused.
- **Checks reach (every M3c node, not `problem` only; issue #314).** The `Generate.gate` static
  check refuses a `<spec_id>_checks.cu` whose `case_run` reaches no published operation of the
  model (`source.checks_model_reach_violations`; the rule is
  `docs/workflow/CHECKS_MODULE_CONTRACT.md` §1). The published set is every `<spec_id>__*` the
  model DEFINES in `namespace <spec_id>_model`; an empty set is refused. From the `case_run` of
  `namespace <spec_id>_checks`, every identifier of a defined function's body (comments,
  literals and directives blanked, `using` declarations dropped) that names a function the
  checks source defines is followed, and a published operation QUALIFIED by
  `<spec_id>_model::` (or a `namespace md = <spec_id>_model;` alias) is a reach — a
  `<<<...>>>` launch, a call split over lines, a call inside a lambda in a body, an operation
  taken by address. In the checks source and every other leaf source but the model, the
  operation's name appears only that way, or as the checks source's declaration in
  `namespace <spec_id>_model`: a local lambda, functor, variable, member or template of that
  name, and an unqualified call after a `using`, are refused. NOT followed, so an operation reached only
  through one is refused — call it from a namespace-scope function instead: a namespace-scope
  lambda variable, a struct member function, a template function, a macro. A call only from
  `case_setup` does not count (its capture is the INITIAL state). Also refused: a checks-side
  definition of a function named as a published operation, in any namespace (on a `problem`
  node the checks source DECLARES it in `namespace <spec_id>_model` and never defines it). Same-named
  functions merge (a call to either follows both). The
  refusal reads `case_run reaches no published operation of the model`. It is a reach claim: a
  dead or guarded call passes it and is `Generate.verify`'s.
