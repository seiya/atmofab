# Controlled Spec: 1D forward Euler update (component spec)

## 0. Meta information
- `spec_id`: `dynamics_advection_diffusion_time_update_1d_euler1`
- `spec_version`: `0.2.0`
- `status`: `controlled_draft`
- `spec_kind`: `component`
- `domain`: `dynamics`
- `family`: `advection_diffusion`

## 1. Responsibility and scope
This `component` is responsible for executing the time update of the 1D advection-diffusion problem.

## 2. input/output contract
The input is `u^n(i)`, `a`, `nu`, `dx`, `dt`, and the boundary-applied neighboring cell values. The output is `u^{n+1}(i)`.

**Published signature.** §5.1 pins the operation's full argument list, and this paragraph describes it: a reader must be able to check the two against each other. The published arguments are the cell count `nx` and `u_n` (`nx` values) with `a`, `nu`, `dx`, `dt` as inputs; `u_np1` (`nx` values) and the input guard `guard_pass` (§4) as outputs. `nx` and `guard_pass` are part of the published contract even though the physics above does not name them.

## 3. Operation definition
The published `operation` is `dynamics_advection_diffusion_time_update_1d_euler1__advance`. The update expression is
$$
u_i^{n+1}
= u_i^n
- C\left(u_i^n-u_{i-1}^n\right)
+ D\left(u_{i+1}^n-2u_i^n+u_{i-1}^n\right)
$$
$$
C=a\frac{\Delta t}{\Delta x},\quad D=\nu\frac{\Delta t}{\Delta x^2}
$$

## 4. Failure conditions and constraints
Treat `dx<=0` and `dt<=0` as invalid input and an error.

## 5. Public API and compatibility
The only published `operation_id` is `dynamics_advection_diffusion_time_update_1d_euler1__advance`.

A change breaking compatibility of the published signature — the argument names, their order, their types, their ranks, or their `intent`s — is a **breaking change released under a new `spec_version`**, not a silent regeneration: a consumer is certified against the ABI it linked, and `docs/ORCHESTRATION.md` §13b makes a regenerated dependency source invalidate that consumer's readiness. A change to how the surface is CARRIED (the §5.1 / `IR public_api.signatures` REPRESENTATION) is likewise released under a new `spec_version` even when the ABI is byte-identical, because dependency freshness invalidates a stale certified `IR` only via its version.

### 5.1 Canonical interface block
The exact published surface, as a machine-readable **language-neutral** signature block (`module_parameters` / `types` / `procedures`). It describes the published operation abstractly — for every argument: its `name`, neutral `type` (`real` / `integer` / `logical`), `rank`, `intent`, and, for an explicit-shape array, its `dims` bound expressions; plus the value-pinned module parameters the signatures reference. The vocabulary is neutral throughout: a kind value is `float64`, never the Fortran `real64`. The target language's binding (here Fortran: `real(dp)`, assumed-shape `(:)` ranks, `integer, parameter :: dp = real64`, the `<spec_id>__` name) is produced by the language backend (`tools/backends/language/fortran/signatures`), not authored here — so this contract is not tied to Fortran. The generated model source must publish the symbol below with the signature this block describes (formatting, continuations, and comments may differ; the name, argument order, types, ranks, and `intent`s may not). The procedure HEADER is compared as published, so a procedure prefix the block does not declare — marking the operation as side-effect-free, say — is a difference too, and is refused. That is worth stating because it is the one difference a reader of the previous sentence would expect to be tolerated: it changes no argument, and a generator has a natural reason to add it. The deterministic gates render this block to the target language and pin it: the `--stage compile` gate cross-checks its symbol set against §5 and pins the `IR`'s `public_api.signatures` / `public_api.module_parameters` == this block, and the `Generate.static` gate pins the generated model source against these signatures (normalized: comments stripped, continuations joined, case-folded, whitespace-insensitive).

`module_parameters` pins `dp` itself, not only the arguments that reference it. The stanza comparison pins an argument's type only SYMBOLICALLY — `real(dp)` matches a source that obtained `dp` from any kind at all — so what `dp` MEANS is pinned here and nowhere else, and narrowing it to `float32` would otherwise be a silent ABI change of exactly the class §5.1 exists to prevent. Pinning it means the generated source must BIND this name exactly once, by the parameter DECLARATION the block pins: an aliasing or plain `use` of the name is refused, and so is a second declaration of it anywhere in the file, including one inside a contained procedure (which would change that procedure's own dummy declarations, and so its ABI). Two checks, and they ask different things — the distinction matters to whoever has to satisfy them. PRESENCE: the pinned declaration must appear, compared as a normalized entity, so spacing, continuations, case and a combined `dp = ..., n = ...` declaration are all immaterial, but a differing ATTRIBUTE LIST is not — an extra `public` or `private` on the pinned declaration itself reads as absent, and the refusal quotes the exact text to write. UNIQUENESS: no OTHER statement may bind the name, and that question is asked with the validator's own Fortran declaration reader rather than a pattern of its own, so the attribute form in any attribute order, a `kind=` in the type specification, and the `parameter (...)` statement form are all seen. An earlier version of this paragraph described the second as if it governed the first. What it does not see is a name supplied by an unrestricted `use`, which brings a binding no declaration reader can compare; the lint rule `C121` refuses that construct in the same `Generate.gate` substep, measured rather than assumed. An earlier version of this paragraph claimed the rule refused every further spelling "by construction"; it did not — three ordinary spellings were accepted until issue #153 round 3 measured them.

```yaml
module_parameters:
- name: dp
  value: float64
types: []
procedures:
- kind: subroutine
  name: dynamics_advection_diffusion_time_update_1d_euler1__advance
  args:
  - name: nx
    rank: 0
    intent: in
    spec:
      type: integer
  - name: u_n
    rank: 1
    intent: in
    spec:
      type: real
      kind: dp
    dims:
    - nx
  - name: a
    rank: 0
    intent: in
    spec:
      type: real
      kind: dp
  - name: nu
    rank: 0
    intent: in
    spec:
      type: real
      kind: dp
  - name: dx
    rank: 0
    intent: in
    spec:
      type: real
      kind: dp
  - name: dt
    rank: 0
    intent: in
    spec:
      type: real
      kind: dp
  - name: u_np1
    rank: 1
    intent: out
    spec:
      type: real
      kind: dp
    dims:
    - nx
  - name: guard_pass
    rank: 0
    intent: out
    spec:
      type: logical
```

## 6. Prohibitions
Forbid automatic switching of the time-integration method.

## 7. Traceability
Require recording the adoption result in `component_catalog.yaml` and `case.resolved.yaml`.

## 8. tests reference
The corresponding `tests.md` is `spec/component/dynamics/advection_diffusion/dynamics_advection_diffusion_time_update_1d_euler1/tests.md`, with `test_profile_version` of `0.1.0`.

## 9. AD preparation information
`ad_readiness.enabled` is `true`. `ceil` (when used in the `dt` rule) is made explicit as a non-differentiable operation.
