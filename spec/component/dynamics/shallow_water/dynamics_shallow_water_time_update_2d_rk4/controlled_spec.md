# Controlled Spec: 2D `RK4` update (component spec)

## 0. Meta information
- `spec_id`: `dynamics_shallow_water_time_update_2d_rk4`
- `spec_version`: `0.1.0`
- `status`: `controlled_draft`
- `spec_kind`: `component`
- `domain`: `dynamics`
- `family`: `shallow_water`

## 1. Responsibility and scope
This `component` is responsible for advancing a conserved-variable field by one time step with the classical four-stage fourth-order Runge–Kutta method (`RK4`). It evaluates the tendency itself: the caller passes the tendency operation as a procedure argument, and this `component` calls it at each of the four stage states. It does not define the tendency, does not apply a boundary condition, and does not choose the time step.

## 2. input/output contract
The inputs are the extents `ncomp`, `nx`, `ny`, the state `U_n` (`ncomp` × `nx` × `ny`, interior cells only), the tendency procedure `rhs`, the time `t` at which `U_n` holds, and the time step `dt`. The outputs are the state `U_np1` (`ncomp` × `nx` × `ny`) at `t + dt` and the input guard `guard_pass` (§4).

**Published signature.** §5.1 pins the operation's full argument list, and this paragraph describes it: a reader must be able to check the two against each other. The published arguments are exactly those nine, in that order: `ncomp`, `nx`, `ny`, `U_n`, `rhs`, `t`, `dt`, `U_np1`, `guard_pass`. `rhs` is a procedure-typed argument whose prototype is the `interfaces` entry `dynamics_shallow_water_time_update_2d_rk4_rhs` of §5.1: a subroutine `(ncomp, nx, ny, t, U, dUdt)` that receives a stage time `t` and a stage state `U` (`ncomp` × `nx` × `ny`, interior cells only) and returns the tendency `dUdt` (`ncomp` × `nx` × `ny`) at that state. A procedure-typed argument carries no `intent`; the prototype is declared by the generated model source and never defined there. The procedure a consumer passes as `rhs` must match the prototype in its argument types, kinds, ranks, explicit shapes and `intent`s; its dummy argument names may differ. `U_n` and `U_np1` are separate arrays, so the operation never reads a cell it has written.

`U`, `U_n`, `U_np1` and `dUdt` hold interior cells only: any ghost region the tendency needs is the caller's, filled inside `rhs` from the interior state it receives, so a consumer's boundary treatment stays outside this `component`. The stage time is passed so that a tendency with an explicit time dependence (a time-dependent forcing) receives the time of the stage it is evaluated at; a tendency without one ignores it.

## 3. Operation definition
The published `operation` is `dynamics_shallow_water_time_update_2d_rk4__advance`. With $R(t, U)$ the tendency `rhs` returns, the update is
$$
k_1 = R\left(t,\ U^n\right),\qquad
k_2 = R\left(t+\tfrac{\Delta t}{2},\ U^n+\tfrac{\Delta t}{2}\,k_1\right),\qquad
k_3 = R\left(t+\tfrac{\Delta t}{2},\ U^n+\tfrac{\Delta t}{2}\,k_2\right),\qquad
k_4 = R\left(t+\Delta t,\ U^n+\Delta t\,k_3\right)
$$
$$
U^{n+1}=U^n+\frac{\Delta t}{6}\left(k_1+2k_2+2k_3+k_4\right)
$$
`rhs` is called exactly four times per step, in this order, each call receiving the stage time and the stage state written above; the four tendencies are the four calls' results, and none is reused for another stage. `guard_pass` true means the update was performed.

## 4. Failure conditions and constraints
Treat `dt<=0`, `ncomp<1`, `nx<1`, and `ny<1` as invalid input and an error: `guard_pass` is false, `rhs` is not called, and `U_np1` is not a valid update.

The invariants of the operation:
- **zero-tendency invariance:** when `rhs` returns `dUdt=0` at every stage, `U^{n+1}=U^n`.
- **stage sequence:** the four calls receive the stage times $t,\ t+\Delta t/2,\ t+\Delta t/2,\ t+\Delta t$ and the stage states of §3, in that order, and there are exactly four.
- **one-step accuracy:** for the linear tendency $R(t,U)=\lambda U$ the update is $U^{n+1}=\left(1+z+\tfrac{z^2}{2}+\tfrac{z^3}{6}+\tfrac{z^4}{24}\right)U^n$ with $z=\lambda\,\Delta t$, up to round-off.
- **input guard:** when `dt<=0` (or an extent is below 1), `guard_pass` is false and `rhs` is not called.

## 5. Public API and compatibility
The only published `operation_id` is `dynamics_shallow_water_time_update_2d_rk4__advance`. The only published procedure prototype (an `interfaces` entry of §5.1, declared and not defined) is `dynamics_shallow_water_time_update_2d_rk4_rhs`; it is not an operation.

A change breaking compatibility of the published signature — the argument names, their order, their types, their ranks, or their `intent`s — or of the prototype is a **breaking change released under a new `spec_version`**, not a silent regeneration: a consumer is certified against the ABI it linked, and `docs/ORCHESTRATION.md` §13a (derivation-key certification) makes a regenerated dependency source invalidate that consumer's readiness. A change to how the surface is CARRIED (the §5.1 / `IR public_api.signatures` REPRESENTATION) is likewise released under a new `spec_version` even when the ABI is byte-identical, because dependency freshness invalidates a stale certified `IR` only via its version.

### 5.1 Canonical interface block
The exact published surface, as a machine-readable **language-neutral** signature block (`module_parameters` / `types` / `interfaces` / `procedures`). It describes the published operation abstractly — for every argument: its `name`, neutral `type` (`real` / `integer` / `logical` / `procedure`), `rank`, `intent`, and, for an explicit-shape array, its `dims` bound expressions; plus the value-pinned module parameters the signatures reference, and the named procedure prototype the procedure-typed argument `rhs` references (`spec: {type: procedure, interface: <name>}`; rank 0, no `dims`, no `intent`). The `interfaces` entry is a prototype, not an operation: `Compile` transcribes it into the `IR`'s `public_api.interfaces`, the `--stage compile` gate pins that list against this block by name and argument set, and the `Generate.static` gate pins the generated model source's declared prototype of that name against it — the source declares it and must not define it. The vocabulary is neutral throughout: a kind value is `float64`, never the Fortran `real64`. The target language's binding (here Fortran: `real(dp)`; explicit-shape arrays `(ncomp,nx,ny)` where the block declares `dims`, as every array below does, and assumed-shape `(:)` where it does not; `integer, parameter :: dp = real64`; the `<spec_id>__` name; an `abstract interface` for the prototype and `procedure(<prototype>) :: rhs` for the procedure-typed argument) is produced by the language backend (`tools/backends/language/fortran/signatures`), not authored here — so this contract is not tied to Fortran. The generated model source must publish the symbol below with the signature this block describes (formatting, continuations, and comments may differ; the name, argument order, types, ranks, and `intent`s may not). The procedure HEADER is compared as published, so a procedure prefix the block does not declare — marking the operation as side-effect-free, say — is a difference too, and is refused. That is worth stating because it is the one difference a reader of the previous sentence would expect to be tolerated: it changes no argument, and a generator has a natural reason to add it. The deterministic gates render this block to the target language and pin it: the `--stage compile` gate cross-checks its symbol set against §5 and pins the `IR`'s `public_api.signatures` / `public_api.interfaces` / `public_api.module_parameters` == this block, and the `Generate.static` gate pins the generated model source against these signatures (normalized: comments stripped, continuations joined, case-folded, whitespace-insensitive).

`module_parameters` pins `dp` itself, not only the arguments that reference it. The stanza comparison pins an argument's type only SYMBOLICALLY — `real(dp)` matches a source that obtained `dp` from any kind at all — so what `dp` MEANS is pinned here and nowhere else, and narrowing it to `float32` would otherwise be a silent ABI change of exactly the class §5.1 exists to prevent. Pinning it means the generated source must BIND this name exactly once, by the parameter DECLARATION the block pins: an aliasing or plain `use` of the name is refused, and so is a second declaration of it anywhere in the file, including one inside a contained procedure (which would change that procedure's own dummy declarations, and so its ABI). Two checks, and they ask different things — the distinction matters to whoever has to satisfy them. PRESENCE: the pinned declaration must appear, compared as a normalized entity, so spacing, continuations, case and a combined `dp = ..., n = ...` declaration are all immaterial, but a differing ATTRIBUTE LIST is not — an extra `public` or `private` on the pinned declaration itself reads as absent, and the refusal quotes the exact text to write. UNIQUENESS: no OTHER statement may bind the name, and that question is asked with the validator's own Fortran declaration reader rather than a pattern of its own, so the attribute form in any attribute order, a `kind=` in the type specification, and the `parameter (...)` statement form are all seen. What it does not see is a name supplied by an unrestricted `use`, which brings a binding no declaration reader can compare; the lint rule `C121` refuses that construct in the same `Generate.gate` substep, measured rather than assumed.

```yaml
module_parameters:
- name: dp
  value: float64
types: []
interfaces:
- kind: subroutine
  name: dynamics_shallow_water_time_update_2d_rk4_rhs
  args:
  - name: ncomp
    rank: 0
    intent: in
    spec:
      type: integer
  - name: nx
    rank: 0
    intent: in
    spec:
      type: integer
  - name: ny
    rank: 0
    intent: in
    spec:
      type: integer
  - name: t
    rank: 0
    intent: in
    spec:
      type: real
      kind: dp
  - name: U
    rank: 3
    intent: in
    spec:
      type: real
      kind: dp
    dims:
    - ncomp
    - nx
    - ny
  - name: dUdt
    rank: 3
    intent: out
    spec:
      type: real
      kind: dp
    dims:
    - ncomp
    - nx
    - ny
procedures:
- kind: subroutine
  name: dynamics_shallow_water_time_update_2d_rk4__advance
  args:
  - name: ncomp
    rank: 0
    intent: in
    spec:
      type: integer
  - name: nx
    rank: 0
    intent: in
    spec:
      type: integer
  - name: ny
    rank: 0
    intent: in
    spec:
      type: integer
  - name: U_n
    rank: 3
    intent: in
    spec:
      type: real
      kind: dp
    dims:
    - ncomp
    - nx
    - ny
  - name: rhs
    rank: 0
    spec:
      type: procedure
      interface: dynamics_shallow_water_time_update_2d_rk4_rhs
  - name: t
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
  - name: U_np1
    rank: 3
    intent: out
    spec:
      type: real
      kind: dp
    dims:
    - ncomp
    - nx
    - ny
  - name: guard_pass
    rank: 0
    intent: out
    spec:
      type: logical
```

## 6. Prohibitions
Forbid automatic switching of the time-integration method, a change of the number of stages or of the stage weights, fewer than four `rhs` calls per step, the reuse of one stage's tendency for another stage, and the evaluation of `rhs` at a state or time other than those of §3. Forbid calling `rhs` when `guard_pass` is false.

## 7. Traceability
Require recording the adoption result in `component_catalog.yaml` and `case.resolved.yaml`.

## 8. tests reference
The corresponding `tests.md` is `spec/component/dynamics/shallow_water/dynamics_shallow_water_time_update_2d_rk4/tests.md`, with `test_profile_version` of `0.1.0`.

## 9. AD preparation information
`ad_readiness.enabled` is `true`. The update is a fixed composition of four tendency evaluations and holds no non-differentiable operation of its own; the differentiability of the step is that of the tendency the caller supplies.
