# CodegenBundle — the CUDA C++ binding

> **Audience: the maintainer of the bundle contract and of the CUDA C++ language backend.** This
> document states the CUDA C++ facts the language-neutral `docs/workflow/CODEGEN_BUNDLE_CONTRACT.md`
> applies, and the lowering of the language-neutral §5.1 signature form to CUDA C++. The
> machine-readable copies are `tools/backends/language/cuda_cpp/bundle.py` (`bundle_facts`) and
> `tools/backends/language/cuda_cpp/signatures.py` (`signatures`); where a module and this
> document differ, the module governs.

## 1. Files and translation units

- **Extension.** A CUDA C++ bundle file has the extension `.cu` (`SOURCE_EXTENSIONS`); a leaf
  ships no header. Every bundle file sits at the top level of the source directory: a `.cu` in a
  subdirectory is refused by the `Generate.gate` static check (`source.model_source_gates`), since
  Build would compile it as an object the syntax stage never compiled.
- **Separate compilation against a host-rendered header.** Every `.cu` is compiled on its own and
  the objects are linked — the build graph's ordinary shape (`codegen_bundle.derive_build_graph`).
  The declarations a source needs of a node's published surface are in `<spec_id>_model.cuh`,
  which the HOST renders from the node's IR `public_api` (`header.render`, the `interface_header`
  capability) and writes beside the bundle's files; the model source includes it and defines the
  published operations, and the runner and a consumer include it and call them. A consumer's
  `src/` also holds, host-written at Generate start, each closure member's header COPIED from the
  member's certified source directory — never re-rendered, so it is the declaration the member's
  own Build compiled — sha-checked against the consumer's generate key
  (`workflow_conductor._write_dependency_headers`); Build stages the same bytes beside each
  member's source in the object directory (`_stage_dependency_sources`), which is on the include
  path. On a physics node the host also renders `<spec_id>_checks.cuh`, the declarations of the
  checks ABI and the bound state the leaf's `<spec_id>_checks.cu` defines (`CHECKS_ABI.md` §1).
  No object of one source is a prerequisite of another's compile (`source.source_module_deps`
  states no edge). The rendered control file does not name the header as a prerequisite either:
  Build compiles into a fresh object directory, and the header is rewritten with every accepted
  bundle.
- **`modules`.** A module is a C++ namespace. The model file declares the namespace
  `<spec_id>_model`, which holds its whole published surface; a consumer names a published
  symbol as `<spec_id>_model::<name>`.
- **Host-given names.** The host names a node's model source `<spec_id>_model.cu`, its header
  `<spec_id>_model.cuh`, its checks source `<spec_id>_checks.cu` and its runner
  `<spec_id>_runner.cu` (`model_basename` / `header.basename` / `checks_basename` /
  `runner_basename`).
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
| `real` / `integer` / `logical`, rank R ≥ 1 | `atmofab::View<const K, R> name` | `atmofab::View<K, R> name` | `O` |
| the same, `alloc: true` | `const O& name` | `O& name` | — |
| `string`, rank 0 | `const std::string& name` | `std::string& name` | `std::string` |
| `derived T`, rank 0 | `const T& name` | `T& name` | `T` |
| `string` / `derived X`, rank R ≥ 1 | `const O& name` | `O& name` | `O` |
| `procedure` (entry `P` of `interfaces`) | `P name` | — | — |

- A `subroutine` lowers to a function returning `void`; a `function` to one returning its result's
  type.
- A module parameter `n = float64` lowers to `using n = double;` (`float32` to `float`); an
  integer value `n = 64` to `inline constexpr int n = 64;` (in a header, an unreferenced
  `inline constexpr` draws no unused-variable diagnostic).
- `O` is the owning container: `std::vector<X>` at rank 1, `atmofab::Array<X, R>` at rank
  R ≥ 2 (defined by every rendered header: `std::vector<X> data` and `long extent[R]`,
  column-major).
- A `kind` naming a module parameter with an INTEGER value is refused: only a `float64` /
  `float32` parameter lowers to a C++ type. A kind naming no parameter of the block is rendered as
  the name.
- An `interfaces` entry `P` lowers to `using P = <return type> (*)(<parameters>);`.
- A `type` lowers to `struct T { <component>; ... };`, components in order.
- **Not part of the C++ type, so not pinned:** an argument's `dims` (a view's extents are run-time
  values) and a string's `len` (a `std::string` carries its own length).
- **No lowering, refused (`SignatureParseError`):** a `logical` with a kind, a kind naming an
  integer-valued module parameter, and a name that is a C++ keyword or a CUDA execution-space
  specifier. The target-free Compile
  gate renders every §5.1 in every language that declares `signatures`, so a §5.1 using one of
  these fails Compile.
- **`atmofab::View<T, R>`** is the array view every rendered header defines, once per
  translation unit (`header.VIEW_DEFINITION`; its layout is `CHECKS_ABI.md` §5).

## 3. The pin on a generated source

The `Generate.static` gate reads the host-rendered header and the model source TOGETHER
(`tools/backends/language/cuda_cpp/declarations.py`), in namespace `<model file stem>`, and
compares each declaration against the rendered §5.1 with every whitespace character removed: a
type's data members as an ordered list, a procedure as a set whose first element is the header
`<return type> <name>(<argument names in order>)`, a prototype likewise. Every §5.1 procedure must
be DEFINED in the model source with the declared parameter types (a definition whose types differ
is another overload, refused; a top-level `const` on a by-value parameter is not part of the type
and is ignored), no function may carry a prototype's name, and every module parameter must be
declared once. Every leaf source is also held to the preprocessor allowlist of `CHECKS_ABI.md` §5,
so the text the pin reads is the program the compiler builds.

## 4. Build graph

- **Compiler selectors.** The recognized driver family is `nvcc`
  (`COMPILER_SELECTOR_FAMILIES`); an unrecognized selector is dropped and the build uses `nvcc`
  (`DEFAULT_COMPILER`).
- **Module artifacts.** Compiling a CUDA C++ source leaves none beside its object
  (`source.MODULE_ARTIFACT_SUFFIX` is `None`).
- **Control file.** The host authors every node's `src/Makefile` (the language half of
  `control_file`, `tools/backends/language/cuda_cpp/control_file.py`): `NVCC` pinned, `NVCCFLAGS`
  `-std=<toolchain.standard> -O2 -arch=<hardware.architecture> -I$(OBJDIR)`, one object per `.cu`,
  one link.
