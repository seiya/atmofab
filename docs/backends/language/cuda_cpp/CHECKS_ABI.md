# Checks-module ABI — the CUDA C++ binding

> **Audience: the `Generate.generate` / `Generate.verify` leaves of a node whose target
> profile names `toolchain.language: cuda_cpp`.** This document binds the language-neutral
> checks-module contract (`docs/workflow/CHECKS_MODULE_CONTRACT.md`) to CUDA C++, section by
> section, and its §5 is the legality and gate-guard rule set every leaf-authored CUDA C++ source
> of a `Generate` node is held to. It is reached through the CUDA C++ language backend's
> `checks_abi` capability (`tools/backends/language/cuda_cpp/checks_abi.py`). The host inlines §5
> of this document into the `harness`-shape producer's prompt as `gate_guards_document`.

The leaf-authored source files of a node are `<spec_id>_model.cu` (the definitions of its
published operations) and, on a harness, `<spec_id>_runner.cu` (the executable entry); the host
renders `<spec_id>_model.cuh` (the declarations). How they form one program is
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

- **The host declares the published surface; the model DEFINES it.** Before the gates run, the
  host renders `<spec_id>_model.cuh` beside the sources from the IR's `public_api`: inside
  `namespace <spec_id>_model` it declares every module parameter (`using dp = double;`,
  `inline constexpr int case_id_len = 64;`), every published type as a `struct`, every
  `interfaces` entry as a function-pointer alias, and every published operation's declaration;
  it also defines the array view `atmofab::View`. The model source `<spec_id>_model.cu` begins
  with `#include "<spec_id>_model.cuh"` and, inside `namespace <spec_id>_model { ... }`, gives
  each published operation a body with EXACTLY the declared return type, parameter types and
  parameter names, in order. It declares no published type, alias, module parameter or
  operation of its own: a second declaration of a type is a compile error, and a definition
  whose parameter types differ from the declaration is a different overload, which the
  deterministic §5.1 check refuses (and a caller would not link against).
- **The lowering the header uses**, so each definition can be written to match (`K` is the kind
  symbol, e.g. `dp`, or the default `float` / `int` / `bool`):

  | §5.1 argument | `intent(in)` | `intent(out)` / `inout` |
  |---|---|---|
  | `real` / `integer` / `logical`, rank 0 | `K name` | `K& name` |
  | the same, rank R ≥ 1 | `atmofab::View<const K, R> name` | `atmofab::View<K, R> name` |
  | the same, `alloc: true` | `const O& name` | `O& name` |
  | `string`, rank 0 | `const std::string& name` | `std::string& name` |
  | `derived T`, rank 0 | `const T& name` | `T& name` |
  | `string` / `derived X`, rank R ≥ 1 | `const O& name` | `O& name` |
  | `procedure` naming entry `P` | `P name` | — |

  `O` is the OWNING container of the element type: `std::vector<X>` at rank 1, the header's
  `atmofab::Array<X, R>` (members `std::vector<X> data` and `long extent[R]`, column-major) at
  rank R ≥ 2. A `subroutine` returns `void`; a `function` returns its result's type
  (`std::string` for a string, `T` for a derived type, `O` for an array). A component is the
  scalar type or `O`. A string's `len` and an argument's `dims` are not part of the C++ type.
- **The array view.** `atmofab::View<T, R>` has public members `T* data` and `long extent[R]`;
  the element at 0-based indices `(i1, ..., iR)` is
  `data[i1 + extent[0] * (i2 + extent[1] * (... + extent[R-2] * iR))]` — column-major, the first
  index fastest. It does not own its data.
- **Separate compilation.** Every `.cu` is compiled on its own and the objects are linked; the
  runner reaches the model's surface through `#include "<spec_id>_model.cuh"` and defines
  `int main(int argc, char** argv)`. Never `#include` a `.cu` file: the model would then be
  defined in two objects, which fails at link.
- **Published operations are host functions.** A published operation is callable from the host:
  no `__global__`, `__device__` or vendor attribute on it. Device work sits in kernels the
  operation launches; a kernel is internal, and its name does not start with `<spec_id>__`.
- **Every warning is an error.** The deterministic `Generate.gate` lint check compiles each
  source with the host compiler's `-Wall -Wextra -Werror` and `--Werror all-warnings`
  (`tools/backends/linter/nvcc/lint.py`), under `-std=c++17`. An unused parameter, an unused
  variable, and a comparison between signed and unsigned integers are therefore failures, and so
  is a device-assembler error (a kernel over the shared-memory limit).
- **Intentionally-unused parameters.** When an interface FIXES a parameter the body never reads
  (a name the §5.1 signature pins), the parameter keeps its name and the body opens with
  `(void)<name>;`. A parameter no interface fixes — in a helper the leaf itself declared — is
  deleted from the signature and from every call site instead. An unused local variable is not
  declared at all.
- **Only two preprocessor directives.** A leaf-authored source uses `#include "<file>"` /
  `#include <header>` and `#pragma unroll [<argument>]` (in device code only: the host compiler
  reports it unknown in a host function, which fails the lint), and no other directive — no `#define`, no
  `#if` / `#ifdef` / `#endif`, no other `#pragma` (in particular no diagnostic pragma), no line
  marker — and no `_Pragma` / `__pragma` operator, no `##` and no digraph. The deterministic
  `Generate.gate` static check refuses each by presence
  (`tools/backends/language/cuda_cpp/source.py` `preprocessor_violations`): a suppression pragma
  would switch the lint off, and a macro or a conditional would make the §5.1 check read another
  program than the compiler builds. The host-rendered header is not a leaf source and is not
  held to this.
- **The syntax stage compiles for the target.** The `Generate.gate` syntax check runs
  `nvcc -std=<toolchain.standard> -arch=<hardware.architecture> -Xcompiler -fsyntax-only -c` over
  every top-level `.cu` of the source directory, each on its own, with the host-rendered header
  and every `.cu` of a subdirectory staged beside it at the same relative path (a nested source
  is compiled through the file that includes it; `tools/backends/compiler/nvcc/syntax.py`).
