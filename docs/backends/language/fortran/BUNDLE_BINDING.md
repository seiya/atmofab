# CodegenBundle — the Fortran binding

> **Audience: the maintainer of the bundle contract and of the Fortran language backend.** This
> document states the Fortran facts the language-neutral `docs/workflow/CODEGEN_BUNDLE_CONTRACT.md`
> applies. The machine-readable copy of each is `tools/backends/language/fortran/bundle.py` (the
> `bundle_facts` capability); where the two differ, the module governs.

## 1. Files

- **Extension.** A Fortran bundle file is free-form f2008 with the extension `.f90`
  (`SOURCE_EXTENSIONS`). It is an allowlist, not a recognizer: `.f` / `.f95` are Fortran too, but
  the build rules the host authors match `.f90` only.
- **Placement.** Every bundle file sits at the top level of the source directory. A `.f90` in a
  subdirectory is refused by the `Generate.gate` syntax stage, which compiles the top level only
  and walks at any depth so that a nested source is refused rather than skipped: the derived
  Makefile would compile and link it as an object the stage never checked (issue #420).
- **`modules`.** A module is a Fortran `module`. One compiled `.mod` file is written per module
  name, and Fortran names are case-insensitive, so the contract's case-folded module-name
  uniqueness is exactly the language's own rule.
- **Privacy.** A `helper` / `internal_module` role is private by declaration; a Fortran
  `private` statement does not make a file private in the contract's sense, and a private file
  is not reachable from the model: a staged dependency is `<spec_id>_model.f90` alone
  (§Host-given names).
- **Host-given names.** The host names a node's model source `<spec_id>_model.f90`, its checks
  source `<spec_id>_checks.f90` and its runner `<spec_id>_runner.f90` (`model_basename` /
  `checks_basename` / `runner_basename`); a staged dependency is `<spec_id>_model.f90`.

## 2. Entrypoints and state bindings

- **Identifiers.** An identifier is 1-63 characters, starting with a letter (`IDENTIFIER_MAX`,
  `IDENTIFIER_PATTERN`, the f2008/f2018 limit), so a symbol over the bound cannot pass the
  mandatory `Generate.gate` syntax check.
- **Case.** Fortran is case-insensitive in module and symbol names, which is why the contract
  compares both case-folded.
- **Imports.** The host renders an entrypoint's or a binding's import as
  `use <module>, only: <symbol>` (for a bound state variable, `use <spec_id>_checks, only:
  sb_<name> => <name>` — `docs/backends/language/fortran/CHECKS_ABI.md` §1-b).
- **The retired signature-shape count.** Before `entrypoints` and `state_bindings` were declared
  fields, a node's published update path was recovered from the generated source by counting
  the `intent(out)` dummy arguments of a `subroutine`; the contract's "counting the output
  arguments of a generated procedure" is that count, and no gate reads it now
  (`docs/design/deterministic_followups.md`, "Problem state-array usage").

## 3. Build graph

- **Compiler selectors.** The recognized driver families are `COMPILER_SELECTOR_FAMILIES`
  (`gfortran`, `flang`, `ifx`, `nvfortran`, `frt`, …). With a target-triple prefix and a version
  suffix, `gfortran`, `x86_64-linux-gnu-gfortran-12`, `frt` and `frtpx` are kept for a Fortran
  bundle; a driver for another language (`gcc`, `g++`, `clang`) is dropped — the Makefile would
  pin it as `FC` and it would fail on `.f90` — and so is `payload-gfortran` / `sh-gfortran`. An
  unrecognized selector is dropped and the build uses `gfortran` (`DEFAULT_COMPILER`).
- **Module collisions.** A bundle module named like a staged dependency's `<spec_id>_model`
  would overwrite that dependency's `.mod`, which is the Fortran form of the contract's
  cross-origin module-name collision.
- **Objects.** `core/util.f90` flattens to `core__util.o`; a flat `<name>.f90` keeps `<name>.o`.

## 4. Signature lowering

How the language-neutral §5.1 / `public_api.signatures` form (`docs/CONTROLLED_SPEC.md`, "5.1 Canonical interface block")
lowers to Fortran. The renderer is `tools/backends/language/fortran/signatures.py`
(`render_signatures_to_fortran`), and its inverse parses a Fortran interface block back to the
neutral form; where this section and the module differ, the module governs.

- **Types.** `real` / `integer` / `logical` with a `kind` render as `real(<kind>)` (e.g.
  `real(dp)`), without one as the bare type; `string` renders as `character(len=<len>)`, the
  neutral length token `deferred` as `character(len=:)` and `assumed` as `character(len=*)`;
  `derived` as `type(<name>)`; `procedure` as `procedure(<interface>)`.
- **Ranks.** An argument with `dims` renders them verbatim (`coef(3)`); one without renders
  `rank` assumed-shape colons (`(:)`, `(:,:)`).
- **Module parameters.** Each renders as `integer, parameter :: <name> = <value>`, the neutral
  kind value lowered (`float64` → `real64`, `float32` → `real32`); a number passes through. The
  old Fortran tokens (`real64`, `:`, `*`) are refused in the neutral form.
- **Names.** A published operation keeps its `<spec_id>__<op>` name; a `function` whose result
  is named like the function renders with no `result(...)` clause, since Fortran forbids that
  name.
