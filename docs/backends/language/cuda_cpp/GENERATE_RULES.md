# Generate rules — the CUDA C++ binding

> **Audience: the maintainer of the CUDA C++ `Generate` prompts and gates.** No leaf reads this
> document from disk; the rules it states reach a pure `generate.generate` leaf through the CUDA
> C++ prompt fragments (`tools/prompt_templates/backends/language/cuda_cpp/`), the static-lint
> rule set the linter backend renders (`tools/backends/linter/nvcc/lint.py`
> `lint_rules_document`), and `docs/backends/language/cuda_cpp/CHECKS_ABI.md` §5. It is the
> canonical checklist behind them (`docs/workflow/phases/phase_02_generate.md` §2-1 states the
> language-neutral rules and points here).

## Scope

The CUDA C++ backend serves the `infrastructure` harness node of a `cpp_gpu`-style target, whose
leaf authors the model and the runner. A `component` or `problem` node of this language is refused
at launch, because the host renders no CUDA C++ runner (`tools/backends/language/cuda_cpp/source.py`
states which gates are refused and why).

## 1. Lint idioms

- The lint check is the CUDA compiler driver with every warning an error
  (`docs/backends/linter/nvcc/RULES.md`), so the idioms are those of a warning-free build under
  `-std=c++17`: no unused parameter (mark an interface-fixed one with `(void)name;`), no unused
  variable, no signed/unsigned comparison, a `return` on every path of a non-`void` function.
- No suppression pragma: every diagnostic-control pragma is refused by the static check
  (`CHECKS_ABI.md` §5).

## 2. The syntax stage

The mandatory syntax stage for `cuda_cpp` is
`nvcc -std=<toolchain.standard> -arch=<hardware.architecture> -Xcompiler -fsyntax-only -odir <scratch> -c <sources>`
over every `.cu` of the staged directory, each its own translation unit
(`tools/backends/compiler/nvcc/syntax.py`). It promotes no warning class: the lint rule set
already makes every warning an error. A failing stage is attributed by re-running the same argv
over a canary translation unit with one kernel; a canary failure is an invocation the driver
refuses (typically a `toolchain.standard` or `hardware.architecture` it does not know) and is a
transport `fail_closed`.

## 3. Model naming and dependency use

- The model source is `<spec_id>_model.cu`, it opens with `#pragma once`, and its published
  surface sits in `namespace <spec_id>_model` (`BUNDLE_BINDING.md` §1).
- The runner `#include`s the model source by that name. Dependency use by a consumer model is a
  physics node's, and is not bound (Scope).

## 4. The parallel presence floor

On a `component` / `problem` node built for `parallel.backend: cuda` on a `gpu`, a model source
with counted `for` loops must define or launch at least one kernel (`__global__` or `<<<`), read
over the code only (`tools/backends/parallel/cuda/directives.py`). The floor does not run on an
`infrastructure` node.

## 5. The `problem` model gates

Not bound (Scope): the gate refuses a `problem` or `component` model source of this language
rather than passing it unread.
