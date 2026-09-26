# Checks-module ABI — the CUDA C++ binding

> **Audience: the `Generate.generate` / `Generate.verify` leaves of a node whose target
> profile names `toolchain.language: cuda_cpp`.** This document binds the language-neutral
> checks-module contract (`docs/workflow/CHECKS_MODULE_CONTRACT.md`) to CUDA C++, section by
> section, and its §5 is the legality and gate-guard rule set every leaf-authored CUDA C++ source
> of a `Generate` node is held to. It is reached through the CUDA C++ language backend's
> `checks_abi` capability (`tools/backends/language/cuda_cpp/checks_abi.py`). The host inlines §5
> of this document into the `harness`-shape producer's prompt as `gate_guards_document`.

The source files of a node are `<spec_id>_model.cu` (the model and its published operations) and
`<spec_id>_runner.cu` (the executable entry). How the two form one program is
`docs/backends/language/cuda_cpp/BUNDLE_BINDING.md` §1.

## 1. The fixed ABI in CUDA C++

Not bound. The checks-module ABI is called by a runner the host renders over the target's
harness, and the CUDA C++ backend renders no runner (it declares no `runner_render`), so no
checks module of this language is called by anything. A node that would need one — a
`component` or `problem` node — is refused at launch
(`target_profile.toolchain_servable_reasons`). This section is written with the runner renderer.

### 1-b. The bound state in CUDA C++

Not bound, for the reason §1 states.

## 2. The semantics, spelled in CUDA C++

Not bound, for the reason §1 states.

## 3. Module-level state in CUDA C++

Not bound, for the reason §1 states.

## 4. Prohibitions in CUDA C++

Not bound, for the reason §1 states.

## 5. CUDA C++ legality and gate guards

This section applies to every leaf-authored CUDA C++ source of any `Generate` node (the model,
and the hand-authored runner of an `infrastructure` node's self-test).

- **One translation unit per program, by inclusion.** The model source `<spec_id>_model.cu`
  opens with `#pragma once` and declares its whole published surface — every module parameter,
  type, prototype and operation §5.1 pins — inside `namespace <spec_id>_model { ... }`. The runner
  reaches it with `#include "<spec_id>_model.cu"` and defines `int main(int argc, char** argv)`.
  The model is never compiled as an object of its own for the same program: a build that compiled
  it separately AND included it would define every published function twice at link.
- **The array view.** A rank-R numeric array argument of a published operation is an
  `atmofab::View<T, R>` (`BUNDLE_BINDING.md` §2): a class template in `namespace atmofab` with
  public members `T* data` and `long extent[R]`, whose element at 0-based indices
  `(i1, ..., iR)` is `data[i1 + extent[0] * (i2 + extent[1] * (... + extent[R-2] * iR))]` —
  column-major, the first index fastest. A harness node DEFINES it, in its model source, before
  its own namespace; every other node receives it through the harness source it includes.
- **Published operations are host functions.** A published operation is callable from the host:
  no `__global__` or `__device__` specifier on it (the §5.1 pin compares the specifiers with the
  return type). Device work sits in kernels the operation launches; a kernel is internal, and its
  name does not start with `<spec_id>__`.
- **Every warning is an error.** The deterministic `Generate.gate` lint check compiles each
  source with the host compiler's `-Wall -Wextra -Werror` and `--Werror all-warnings`
  (`tools/backends/linter/nvcc/lint.py`), under `-std=c++17`. An unused parameter, an unused
  variable, and a comparison between signed and unsigned integers are therefore failures.
- **Intentionally-unused parameters.** When an interface FIXES a parameter the body never reads
  (a name the §5.1 signature pins), the parameter keeps its name and the body opens with
  `(void)<name>;`. A parameter no interface fixes — in a helper the leaf itself declared — is
  deleted from the signature and from every call site instead. An unused local variable is not
  declared at all.
- **No in-source suppression.** `#pragma nv_diag_suppress` (any `nv_diag...` pragma), `#pragma diag_suppress`,
  `#pragma GCC diagnostic`, `#pragma clang diagnostic`, `#pragma warning`, `_Pragma(...)` and
  `__pragma(...)` are refused in every leaf-authored source by the deterministic `Generate.gate`
  static check (`tools/backends/language/cuda_cpp/source.py` `suppression_violations`), because no
  flag of the compiler driver disables them.
- **The syntax stage compiles for the target.** The `Generate.gate` syntax check runs
  `nvcc -std=<toolchain.standard> -arch=<hardware.architecture> -Xcompiler -fsyntax-only -c` over
  every `.cu` of the source directory, each as its own translation unit, so the model must compile
  on its own as well as through the runner's inclusion (`tools/backends/compiler/nvcc/syntax.py`).
