# CodegenBundle — the CUDA C++ binding

> **Audience: the maintainer of the bundle contract and of the CUDA C++ language backend.** This
> document states the CUDA C++ facts the language-neutral `docs/workflow/CODEGEN_BUNDLE_CONTRACT.md`
> applies, and the lowering of the language-neutral §5.1 signature form to CUDA C++. The
> machine-readable copies are `tools/backends/language/cuda_cpp/bundle.py` (`bundle_facts`) and
> `tools/backends/language/cuda_cpp/signatures.py` (`signatures`); where a module and this
> document differ, the module governs.

## 1. Files and translation units

- **Extension.** A CUDA C++ bundle file has the extension `.cu` (`SOURCE_EXTENSIONS`). There is no
  header extension.
- **One translation unit per program.** A source reaches another only by `#include "<file>.cu"`:
  the runner includes the model, and a consumer's model includes each dependency's model, so a
  program is one translation unit and no object of one source is a prerequisite of another's
  (`source.source_module_deps` states no edge). Every model source opens with `#pragma once`, so a
  source included along two paths is read once.
- **`modules`.** A module is a C++ namespace. The model file declares the namespace
  `<spec_id>_model`, which holds its whole published surface; a consumer names a published
  symbol as `<spec_id>_model::<name>`.
- **Host-given names.** The host names a node's model source `<spec_id>_model.cu`, its checks
  source `<spec_id>_checks.cu` and its runner `<spec_id>_runner.cu` (`model_basename` /
  `checks_basename` / `runner_basename`).
- **Identifiers.** An identifier is 1-1024 characters starting with a letter
  (`IDENTIFIER_MAX`, `IDENTIFIER_PATTERN`). C++ identifiers are case-sensitive, and the §5.1 pin
  compares case-sensitively; the neutral vocabulary is compared case-insensitively for another
  reason (`tools/structured_signatures.py`).

## 2. The §5.1 lowering

The neutral form (`tools/structured_signatures.py`) lowers as follows, `K` being the entity's kind
symbol (a module parameter) or the language default (`float` for `real`, `int` for `integer`,
`bool` for `logical`).

| neutral | argument, `intent(in)` | argument, `out` / `inout` | component / result |
|---|---|---|---|
| `real` / `integer` / `logical`, rank 0 | `K name` | `K& name` | `K` |
| `real` / `integer` / `logical`, rank R ≥ 1 | `atmofab::View<const K, R> name` | `atmofab::View<K, R> name` | no lowering |
| `string`, rank 0 | `const std::string& name` | `std::string& name` | `std::string` |
| `derived T`, rank 0 | `const T& name` | `T& name` | `T` |
| `string` / `derived X`, rank 1 | `const std::vector<X>& name` | `std::vector<X>& name` | `std::vector<X>` |
| `procedure` (entry `P` of `interfaces`) | `P name` | — | — |

- A `subroutine` lowers to a function returning `void`; a `function` to one returning its result's
  type.
- A module parameter `n = float64` lowers to `using n = double;` (`float32` to `float`); an
  integer value `n = 64` to `constexpr int n = 64;`.
- An `interfaces` entry `P` lowers to `using P = <return type> (*)(<parameters>);`.
- A `type` lowers to `struct T { <component>; ... };`, components in order.
- **Not part of the C++ type, so not pinned:** an argument's `dims` (a view's extents are run-time
  values) and a string's `len` (a `std::string` carries its own length).
- **No lowering, refused (`SignatureParseError`):** a numeric array component or result, a string
  or derived array of rank above 1, an allocatable numeric array argument, a `logical` with a kind,
  and a name that is a C++ keyword or a CUDA execution-space specifier. The target-free Compile
  gate renders every §5.1 in every language that declares `signatures`, so a §5.1 using one of
  these fails Compile.
- **`atmofab::View<T, R>`** is the array view the target's harness defines (`CHECKS_ABI.md` §5).

## 3. The pin on a generated source

The `Generate.static` gate reads the model source's declarations
(`tools/backends/language/cuda_cpp/declarations.py`) in namespace `<model file stem>` and compares
each against the rendered §5.1 with every whitespace character removed: a type's data members as
an ordered list, a procedure as a set whose first element is the header
`<return type> <name>(<argument names in order>)`, a prototype likewise. Every §5.1 procedure must
also be DEFINED in the file, no function may carry a prototype's name, and every module parameter
must be declared in the namespace exactly once, as rendered.

## 4. Build graph

- **Compiler selectors.** The recognized driver family is `nvcc`
  (`COMPILER_SELECTOR_FAMILIES`); an unrecognized selector is dropped and the build uses `nvcc`
  (`DEFAULT_COMPILER`).
- **Module artifacts.** Compiling a CUDA C++ source leaves none beside its object
  (`source.MODULE_ARTIFACT_SUFFIX` is `None`).
