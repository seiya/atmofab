# Checks-module ABI — the CUDA C++ binding

> **Audience: the `Generate.generate` / `Generate.verify` leaves of a node whose target profile
> names `toolchain.language: cuda_cpp`, and the maintainer of the binding.** This document binds
> the language-neutral checks-module contract (`docs/workflow/CHECKS_MODULE_CONTRACT.md`) to CUDA
> C++, section by section: its §1-§4 here say how the neutral §1-§4 are spelled in CUDA C++, and
> §5 is the legality and gate-guard rule set every leaf-authored CUDA C++ source of a `Generate`
> node is held to. It is reached through the CUDA C++ language backend's `checks_abi` capability
> (`tools/backends/language/cuda_cpp/checks_abi.py`, which also holds the declaration table
> `CHECKS_ABI_PARAMS` the host renders and the gates compare against). The host inlines the
> neutral §1-§4 followed by §1-§4 of this document into a physics node's `generate.verify`
> reviewer's prompt as `checks_module_contract_document`, and §5 of this document into the
> `harness`-shape producer's prompt as `gate_guards_document`; a physics node's
> `generate.generate` producer is told the same rules by the language's prompt fragments
> (`tools/prompt_templates/backends/language/cuda_cpp/generate_generate.txt`), and the
> `harness`-shape reviewer by its own fragment.

The leaf-authored source files of a node are `<spec_id>_model.cu` (the definitions of its
published operations) and, on a physics node, `<spec_id>_checks.cu` (the checks callbacks and
the bound state) — on a harness, `<spec_id>_runner.cu` (the executable entry) instead. The host
renders `<spec_id>_model.cuh` (the published surface), and on a physics node
`<spec_id>_runner.cu` and `<spec_id>_checks.cuh` (the declarations of this document's §1 and
§1-b) as well as the build control file. How they form one program is
`docs/backends/language/cuda_cpp/BUNDLE_BINDING.md` §1.

## 1. The fixed ABI in CUDA C++

The five callbacks of the neutral §1 are functions returning `void` in `namespace
<spec_id>_checks`, and the host DECLARES them: it renders `<spec_id>_checks.cuh` beside the
sources (`runner.render_checks_header`), and both the host-rendered runner and the leaf's
`<spec_id>_checks.cu` include it. The header reads, for a node whose snapshot schema declares a
scalar `s`, a rank-1 `u` and a rank-2 `a`:

```cpp
// <spec_id>_checks.cuh: the checks ABI of <spec_id>, rendered by the host from the node's IR.
#pragma once
#include <string>
#include <vector>
// ... the guarded definitions of atmofab::View and atmofab::Array (BUNDLE_BINDING.md §1) ...
namespace <spec_id>_checks {
void case_setup(const std::string& case_id, bool& ok);
void case_run(const std::string& case_id, int& steps, int& cells_updated, bool& ok);
void get_time(double& t);
void checks_compute(const std::string& case_id, const std::string& check_id, std::string& status);
void metric_compute(const std::string& case_id, const std::string& name, double& val, bool& is_na, std::string& reason_na, bool& found);
extern double s;
extern std::vector<double> u;
extern atmofab::Array<double, 2> a;
}  // namespace <spec_id>_checks
```

The neutral argument table binds as: an `in` string is `const std::string&`, an `out` string
`std::string&`; `logical` is `bool`, `integer` `int`, `float64` `double`; every `out` argument
is a non-const reference to an object the runner owns and passes freshly initialized.

The leaf's `<spec_id>_checks.cu` begins with `#include "<spec_id>_checks.cuh"` and DEFINES each
callback in `namespace <spec_id>_checks` (a qualified definition `void <spec_id>_checks::f(...)`
counts) with exactly the declared return type and parameter types, in order; the parameter names
are the leaf's. Each is one external definition: not `static`, `inline` or `constexpr`, not
`__device__`, not in an unnamed namespace. The `Generate.gate` static check and the bundle
acceptance gate read the definitions by one reader (`source.checks_module_abi_facts`) and treat a
callback defined any other way as not defined: a definition with another parameter type is an
OVERLOAD the header's declaration never reaches — the runner's call would fail at link — so it
is refused by name before Build. All five are required on every physics node, whatever subset its
runner calls: a node with no metrics still defines `metric_compute`.

### 1-b. The bound state in CUDA C++

Every snapshot variable of the neutral §1-b is a namespace-scope variable of `<spec_id>_checks`,
named exactly as the IR names it (C++ identifiers are case-sensitive), whose type is set by the
rank of its `shape_expr`: `double` for a scalar, `std::vector<double>` for rank 1, and
`atmofab::Array<double, R>` for rank R ≥ 2 — the owning column-major array whose `extent[k]` is
the k-th dimension of the `shape_expr` and whose `data` holds their product. The header declares
each `extern`; the leaf's source DEFINES it with that type and external linkage (not `static`,
`const`, `constexpr` or `extern`, not in an unnamed namespace) — a definition of another type is a
compile error against the header (nvcc 13.4: `declaration is incompatible with ...`), and a
missing or internal one is refused by the static check (`source.unpublished_bound_state`). The
runner reads each one as `<spec_id>_checks::<name>` and hands the harness emitters a non-owning
`atmofab::View` over it. At each capture point a `std::vector` must be non-empty and an
`atmofab::Array` must have every `extent[k]` positive with `data.size()` their product; otherwise
the run stops (`bound state <name> is not allocated at capture`). A rejected case (`case_setup`
setting `ok = false`) still leaves every bound array so sized.

## 2. The semantics, spelled in CUDA C++

A rejected guard / xfail case sets `ok = false` in `case_setup`; `status` is `"pass"`, `"fail"`
or `"na  "` — the not-applicable value is written at the contract's width 4, right-padded, and
the certified harness trims the padding before it writes the status, so `diagnostics.json` reads
`"na"` as on every other target; an honestly unavailable metric sets `found = true`,
`is_na = true` and `reason_na` to a short reason, and a metric that does not apply to the case
sets `found = false`; a rejected case's bound arrays are still sized, e.g. filled with `0.0`.

## 3. Module-level state in CUDA C++

The current case's state lives in the bound namespace-scope variables of §1-b, in HOST memory: a
model that computes on the device copies the result back into them before `case_run` returns. A
cross-case accumulator lives in other namespace-scope variables of the checks source, which an
unnamed namespace keeps internal to it.

## 4. Prohibitions in CUDA C++

- **The harness is the runner's alone.** Neither `<spec_id>_checks.cu` nor `<spec_id>_model.cu`
  includes a harness header (`#include "harness_..."`) or names the harness
  (`harness_<x>_model::`, a `harness_<x>__<op>` call); the host-rendered runner is the sole
  caller (`source.checks_harness_isolation_violations`).
- **No file I/O, no command, nothing that runs after `main`, in ANY leaf source** — the checks
  source, the model and every helper: no file stream or stream buffer of any kind
  (`std::ofstream`, `std::ifstream`, `std::fstream`, `std::filebuf`, …), no C stdio or POSIX
  opener (`fopen`, `freopen`, `open`, their `64` / `at` variants), no `std::filesystem`, no
  `rename` / `unlink` / one-path `remove`, no `system` / `popen` / `exec*`, no `atexit` /
  `at_quick_exit`, no `asm`, no `syscall` / `fork` / `posix_spawn`, no `extern "C"` declaration —
  refused by NAME, called or not (`source.checks_harness_isolation_violations`,
  `LEAF_IO_NAMES` / `LEAF_IO_CALL_NAMES`). Emission is the harness's alone, and the host-rendered
  runner makes it so structurally: it writes EVERY output — the snapshots it serialized at the
  capture points included — after the node's last callback has returned, and ends every exit
  with `std::_Exit`, so no code of a leaf source runs after the harness has written anything.

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
  defined in two objects, which fails at link. Every `.cu` sits at the top level of the source
  directory, beside the header; one in a subdirectory is refused by the static check.
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
  marker — and no `_Pragma` / `__pragma` operator, no `##` and no digraph. Three more rules
  close the same door from the other side:
  - `#include <...>` names a C++17 standard library header or `cuda_runtime.h` only
    (`STANDARD_HEADERS`); a bundle or host file is included with `"..."`.
  - No identifier reserved to the implementation — one starting with `_` and a capital letter,
    or with `__` — except the CUDA keywords `__global__`, `__device__`, `__host__`, `__shared__`,
    `__constant__`, `__managed__`, `__restrict__`, `__launch_bounds__`, `__forceinline__`,
    `__noinline__`, `__syncthreads`, `__syncwarp` and `__func__`
    (`RESERVED_IDENTIFIERS_ALLOWED`). The toolchain's headers define macros under reserved names
    that expand to a diagnostic pragma, the runtime header the driver includes into every source
    among them.
  - No backslash at the end of a line, in a comment or a literal included: a continued comment
    is read one way by the compiler and another by the static check.

  The deterministic `Generate.gate` static check refuses each by presence
  (`tools/backends/language/cuda_cpp/source.py` `preprocessor_violations`): a suppression pragma
  would switch the lint off, and a macro or a conditional would make the §5.1 check read another
  program than the compiler builds. The host-rendered header is not a leaf source and is not
  held to this.
- **The syntax stage compiles for the target.** The `Generate.gate` syntax check runs
  `nvcc -std=<toolchain.standard> -arch=<hardware.architecture> -Xcompiler -fsyntax-only -c` over
  every `.cu` of the source directory, each on its own, with the host-rendered header staged
  beside them (`tools/backends/compiler/nvcc/syntax.py`).
