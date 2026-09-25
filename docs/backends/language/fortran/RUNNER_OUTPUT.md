# Runner output — the Fortran binding

> **Audience: a runner-authoring `Generate` leaf of a node whose target profile names
> `toolchain.language: fortran`** (today the `infrastructure` self-test), and the maintainer of
> the host-rendered runner. This document binds the language-neutral JSON serialization
> requirement of `docs/workflow/RUNNER_OUTPUT_CONTRACT.md` §4 and
> `docs/PERFORMANCE_DIAGNOSTICS.md` §6 to Fortran's formatted output. The host inlines it after
> `RUNNER_OUTPUT_CONTRACT.md` wherever that document is inlined whole (the `harness` shape's two
> `generate` prompts), through the Fortran language backend's `prompt_fragments` capability
> (`tools/backends/language/fortran/prompts.py`).

## 1. JSON serialization in Fortran

**Fortran runner (target profile `toolchain.language=fortran`) descriptor
rules** — enforcement is **descriptor-syntactic**: `post_generate`
(`validate_pipeline_semantics --stage post_generate`) flags the mere *presence*
of a forbidden descriptor in a runner JSON write format spec; it never inspects
runtime output, so a manual leading-zero fixup does **not** pass — the
descriptor must not appear at all.

- Do **not** use the `F0` / `F0.d` numeric descriptor for a JSON numeric token.
- Do **not** use the `L`-family logical descriptor (`L1` etc., which emits
  `T`/`F`) for a JSON boolean. Branch on the logical and write the literal
  `true` / `false`.
- **Canonical safe idiom:** reals via a scientific descriptor `ES24.16E3`
  (always a leading digit; width 24 fits a sign so negatives never overflow to
  `***` — `ES23.16E3` is one column too narrow) then `trim(adjustl(...))`, or a
  bounded explicit-width `Fw.d` (e.g. `F20.6`, never `F0`/`F0.d`) with
  `trim(adjustl(...))`; integers via `I0`; booleans via the `true`/`false`
  literal.

  ```fortran
  function jnum(x) result(s)
    real(8), intent(in) :: x
    character(len=32) :: s
    write(s, '(ES24.16E3)') x      ! leading digit guaranteed; width fits a sign; never F0/F0.d
    s = adjustl(s)                 ! trim(adjustl(s)) at the JSON write site
  end function jnum

  function jbool(b) result(s)
    logical, intent(in) :: b
    character(len=5) :: s
    s = merge('true ', 'false', b) ! literal true/false; never an L descriptor
  end function jbool
  ```

## 2. `perf.json` and the runner's other JSON in Fortran

- A `toolchain.language=fortran` `runner` must not directly embed the `F0` / `F0.d` format into a `JSON` numeric token.
- A `toolchain.language=fortran` `runner`, when outputting a logical value to `JSON`, must not directly embed the `T`/`F` token that the `L`-family edit descriptor (`L1` etc.) generates into a `JSON` boolean token. A `JSON` boolean allows only the literals `true` / `false`, so branch on the logical value and write the string.
- **Enforcement is descriptor-syntactic, not output-based.** The `post_generate` static analysis (`validate_pipeline_semantics --stage post_generate`) flags the mere *presence* of an `F0` / `F0.d` numeric descriptor (and the `L`-family logical descriptor) in any runner `JSON` write format spec. It never inspects the runtime output, so a manual leading-zero fixup (e.g. computing the value, then string-patching a missing `0`) does **not** satisfy the gate — the forbidden descriptor must not appear in the format spec at all.
- **Canonical safe idiom (`fortran`):** emit reals with an explicit scientific descriptor such as `ES24.16E3` (always emits a leading digit; width 24 = sign + `d.dddddddddddddddd` + `E±ddd`, so it never overflows to `****` even for negatives — `ES23.16E3` is one column too narrow and prints `***` for a negative value), then `trim(adjustl(...))`; or, when the magnitude range is known small, a bounded explicit-width `Fw.d` (e.g. `F20.6`, never `F0`/`F0.d`) with `trim(adjustl(...))`. Emit integers with `I0`. Emit booleans by branching on the logical and writing the literal `true` / `false`. Example:

  ```fortran
  function jnum(x) result(s)
    real(8), intent(in) :: x
    character(len=32) :: s
    write(s, '(ES24.16E3)') x      ! leading digit guaranteed; width fits a sign; never F0/F0.d
    s = adjustl(s)                 ! trim(adjustl(s)) at the JSON write site
  end function jnum

  function jbool(b) result(s)
    logical, intent(in) :: b
    character(len=5) :: s
    s = merge('true ', 'false', b) ! literal true/false; never an L descriptor
  end function jbool
  ```
