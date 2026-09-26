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
`nvcc -std=<toolchain.standard> -arch=<hardware.architecture> -Xcompiler -fsyntax-only -odir <scratch> -c <sources>`
over every `.cu` of the staged directory (all at its top level: a nested one is refused by the static check), each its own translation unit, with the host-rendered
header staged beside them (`STAGED_SUFFIXES`; `tools/backends/compiler/nvcc/syntax.py`). It promotes no warning class: the lint rule set
already makes every warning an error. A failing stage is attributed by re-running the same argv
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
- Neither the model nor the checks source includes or names the harness, and the checks source
  does no file I/O (`CHECKS_ABI.md` §4).

## 4. The parallel presence floor

On a `component` / `problem` node built for `parallel.backend: cuda` on a `gpu`, a model source
with counted `for` loops must define or launch at least one kernel (`__global__` or `<<<`), read
over the code only (`tools/backends/parallel/cuda/directives.py`). The floor does not run on an
`infrastructure` node.

## 5. The `problem` model gates

The Fortran binding's three gates, read over the namespace-scope function definitions of the
model source (`source.run_problem_model_gates`); a source whose brackets do not balance is
refused rather than read in part. A parameter is an OUTPUT when the function can write through
it — a non-const reference, a pointer to non-const, a non-const `atmofab::View` — and a returned
value is one more output.

- **Literal outputs.** A function every one of whose output parameters is assigned whole
  (`out = ...;`) only from literals, none depending on an input, is refused.
- **Dependency dataflow.** What a dependency call writes must reach an output through
  assignments. Its candidates are the names whose storage the call's actuals hand over — at the
  operation's output parameters as the dependency's header `<dep>_model.cuh` beside the model
  declares them, or, without the header, at every position minus `const` / `constexpr` names and
  functions this file defines — minus the enclosing function's parameters and names assigned
  before the call (an inert call's inputs). The closure runs backward from the outputs over
  assignments `lhs = rhs` (a target's base name, `u[i]` and `u.data[i]` included) and over a view
  declared over another name's storage (`View<...> v{u.data(), ...}` makes `u` take `v`).
- **Metric-only scalar kernel.** On a multi-dimensional `problem` node, a function with five or
  more outputs and neither an array parameter (`atmofab::View`, `atmofab::Array`, `std::vector`, a
  pointer) nor a loop (`for`, `while`, a `<<<` launch) is refused.
