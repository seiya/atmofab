# Controlled Spec: 2D `SSPRK2` update (component spec)

## 0. Meta information
- `spec_id`: `dynamics_shallow_water_time_update_2d_ssprk2`
- `spec_version`: `0.4.0`
- `status`: `controlled_draft`
- `spec_kind`: `component`
- `domain`: `dynamics`
- `family`: `shallow_water`

## 1. Responsibility and scope
This `component` is responsible for executing the time integration of the shallow water problem with `SSPRK2`.

## 2. input/output contract
The inputs are `U^n`, the interface-flux difference field `L_flux`, the bottom-topography source term field `S_b`, the bathymetry field `z_b`, `dt`, `dx`, and `dy`. The output is `U^{n+1}`.

**Published signature.** §5.1 pins the operation's full argument list, and this paragraph describes it: a reader must be able to check the two against each other. The published arguments are the extents `ncomp`, `nx`, `ny`; `U_n`, `L_flux`, `S_b` (`ncomp` × `nx` × `ny` each) and `z_b` (`nx` × `ny`) with `dt`, `dx`, `dy` as inputs; `U_np1` (`ncomp` × `nx` × `ny`) and the input guard `guard_pass` (§4) as outputs. The three extents and `guard_pass` are part of the published contract even though the physics above does not name them.

`L_flux` and `S_b` are supplied as **fixed fields** by the caller; this component does **not** recompute them internally. `z_b` is **accepted at the boundary** for interface stability but is **inert at L0**: the output does not depend on it (it is reserved for higher-fidelity source-term coupling).

## 3. Operation definition
The published `operation` is `dynamics_shallow_water_time_update_2d_ssprk2__advance`. In the general continuous form, let $L_{flux}(U)$ be the interface-flux difference and $S_b(U,z_b)$ be the bottom-topography source term, and the update is
$$
U^{(1)}=U^n+\Delta t\left(L_{flux}(U^n)+S_b(U^n,z_b)\right)
$$
$$
U^{n+1}=\frac{1}{2}U^n+\frac{1}{2}\left(U^{(1)}+\Delta t\left(L_{flux}(U^{(1)})+S_b(U^{(1)},z_b)\right)\right)
$$

### 3.1 L0 realization (profile `dynamics_shallow_water_time_update_2d_ssprk2_l0`, this version)
At L0, `L_flux` and `S_b` are provided as fixed input fields and are **not** functions recomputed at the stage state. Each stage right-hand side is computed as
$$
rhs = L_{flux} + S_b
$$
from the supplied fields. The stage-RHS operation (`ssprk2_stage_rhs`) consumes **only** `L_flux` and `S_b` — it does **not** consume `U` or `z_b`. Both stages therefore use the identical RHS, and the two-stage SSPRK2 composition with weights $\tfrac12,\tfrac12$ reduces to the closed form
$$
U^{n+1} = U^n + \Delta t\,(L_{flux} + S_b).
$$
Per-stage re-evaluation of $L_{flux}(U)$ / $S_b(U,z_b)$ and `z_b` source coupling are **out of scope at L0** and deferred to a higher-fidelity profile.

Implementations **MUST NOT** introduce arithmetic no-ops (e.g. `0*U`, `0*z_b`) to reference unused inputs. The accepted-but-inert input (`z_b`) is kept as a live `intent(in)` dummy and referenced through a benign name binding (e.g. an `associate (unused_z_b => z_b); end associate` block) so it neither participates in the computation nor triggers a compiler unused-argument warning. Do **not** add an `! allow(...)` lint pragma for `z_b`, or for anything else: the lint gate imposes its rule set with `--ignore-allow-comments`, so a pragma suppresses nothing whatever it names. The idiom is reserved for nothing — an earlier version of this paragraph reserved it for the `implicit none` / F2008 `C003` conflict, which is no longer a conflict the gate can see, because `C003` is not in the declared rule set.

## 4. Failure conditions and constraints
Treat `dt<=0`, `dx<=0`, and `dy<=0` as invalid input and an error.

### 4.1 Invariants (L0)
- **zero-rhs invariance:** when `L_flux=0` and `S_b=0`, `U^{n+1}` equals `U^n`.
- **stage-weight consistency:** the two-stage composition applies weights `1/2` and `1/2` to `U^n` and the second-stage state.
- **frozen-field exactness:** with `L_flux` and `S_b` supplied as fixed fields, `U^{n+1} = U^n + dt*(L_flux + S_b)` (both stages use the identical RHS).
- **z_b invariance:** with `L_flux`, `S_b`, and `U^n` held fixed, varying `z_b` does not change `U^{n+1}` (`z_b` is inert at L0).
- **input guard:** when `dt<=0` or `dx<=0` or `dy<=0`, `guard_pass` is false and the update is rejected as invalid input.

## 5. Public API and compatibility
The only published `operation_id` is `dynamics_shallow_water_time_update_2d_ssprk2__advance`.

A change breaking compatibility of the published signature — the argument names, their order, their types, their ranks, or their `intent`s — is a **breaking change released under a new `spec_version`**, not a silent regeneration: a consumer is certified against the ABI it linked, and `docs/ORCHESTRATION.md` §13b makes a regenerated dependency source invalidate that consumer's readiness. A change to how the surface is CARRIED (the §5.1 / `IR public_api.signatures` REPRESENTATION) is likewise released under a new `spec_version` even when the ABI is byte-identical, because dependency freshness invalidates a stale certified `IR` only via its version.

### 5.1 Canonical interface block
The exact published surface, as a machine-readable **language-neutral** signature block (`module_parameters` / `types` / `procedures`). It describes the published operation abstractly — for every argument: its `name`, neutral `type` (`real` / `integer` / `logical`), `rank`, `intent`, and, for an explicit-shape array, its `dims` bound expressions; plus the value-pinned module parameters the signatures reference. The vocabulary is neutral throughout: a kind value is `float64`, never the Fortran `real64`. The target language's binding (here Fortran: `real(dp)`, assumed-shape `(:)` ranks, `integer, parameter :: dp = real64`, the `<spec_id>__` name) is produced by the language backend (`tools/backends/language/fortran/signatures`), not authored here — so this contract is not tied to Fortran. The generated model source must publish the symbol below with the signature this block describes (formatting, continuations, and comments may differ; the name, argument order, types, ranks, and `intent`s may not). The procedure HEADER is compared as published, so a procedure prefix the block does not declare — marking the operation as side-effect-free, say — is a difference too, and is refused. That is worth stating because it is the one difference a reader of the previous sentence would expect to be tolerated: it changes no argument, and a generator has a natural reason to add it. The deterministic gates render this block to the target language and pin it: the `--stage compile` gate cross-checks its symbol set against §5 and pins the `IR`'s `public_api.signatures` / `public_api.module_parameters` == this block, and the `Generate.static` gate pins the generated model source against these signatures (normalized: comments stripped, continuations joined, case-folded, whitespace-insensitive).

`module_parameters` pins `dp` itself, not only the arguments that reference it. The stanza comparison pins an argument's type only SYMBOLICALLY — `real(dp)` matches a source that obtained `dp` from any kind at all — so what `dp` MEANS is pinned here and nowhere else, and narrowing it to `float32` would otherwise be a silent ABI change of exactly the class §5.1 exists to prevent. Pinning it means the generated source must BIND this name exactly once, by the parameter DECLARATION the block pins: an aliasing or plain `use` of the name is refused, and so is a second declaration of it anywhere in the file, including one inside a contained procedure (which would change that procedure's own dummy declarations, and so its ABI). The gate asks "does this statement bind the name" with the validator's own Fortran declaration reader rather than a pattern of its own, so the attribute form in any attribute order, a `kind=` in the type specification, and the `parameter (...)` statement form are all one question. What it does not see is a name supplied by an unrestricted `use`, which brings a binding no declaration reader can compare; the lint rule `C121` refuses that construct in the same `Generate.gate` substep, measured rather than assumed. An earlier version of this paragraph claimed the rule refused every further spelling "by construction"; it did not — three ordinary spellings were accepted until issue #153 round 3 measured them.

```yaml
module_parameters:
- name: dp
  value: float64
types: []
procedures:
- kind: subroutine
  name: dynamics_shallow_water_time_update_2d_ssprk2__advance
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
  - name: L_flux
    rank: 3
    intent: in
    spec:
      type: real
      kind: dp
    dims:
    - ncomp
    - nx
    - ny
  - name: S_b
    rank: 3
    intent: in
    spec:
      type: real
      kind: dp
    dims:
    - ncomp
    - nx
    - ny
  - name: z_b
    rank: 2
    intent: in
    spec:
      type: real
      kind: dp
    dims:
    - nx
    - ny
  - name: dt
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
  - name: dy
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
Forbid automatic switching of the time-integration method.

## 7. Traceability
Require recording the adoption result in `component_catalog.yaml` and `case.resolved.yaml`.

## 8. tests reference
The corresponding `tests.md` is `spec/component/dynamics/shallow_water/dynamics_shallow_water_time_update_2d_ssprk2/tests.md`, with `test_profile_version` of `0.3.0`.

## 9. AD preparation information
`ad_readiness.enabled` is `true`. `ceil` (when used in the `dt` rule) is made explicit as a non-differentiable operation.
