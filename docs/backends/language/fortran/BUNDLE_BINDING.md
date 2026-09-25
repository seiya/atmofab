# CodegenBundle — the Fortran binding

> **Audience: the maintainer of the bundle contract and of the Fortran language backend.** This
> document states the Fortran facts the language-neutral `docs/workflow/CODEGEN_BUNDLE_CONTRACT.md`
> applies. The machine-readable copy of each is `tools/backends/language/fortran/bundle.py` (the
> `bundle_facts` capability); where the two differ, the module governs.

## 1. Files

- **Extension.** A Fortran bundle file is free-form f2008 with the extension `.f90`
  (`SOURCE_EXTENSIONS`). It is an allowlist, not a recognizer: `.f` / `.f95` are Fortran too, but
  the build rules the host authors match `.f90` only.
- **`modules`.** A module is a Fortran `module`. One compiled `.mod` file is written per module
  name, and Fortran names are case-insensitive, so the contract's case-folded module-name
  uniqueness is exactly the language's own rule.
- **Privacy.** A `helper` / `internal_module` role is private by declaration; a Fortran
  `private` statement does not make a file private in the contract's sense.
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
  sb_<name> => <name>` — `docs/backends/language/fortran/CHECKS_ABI.md` §2).

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
