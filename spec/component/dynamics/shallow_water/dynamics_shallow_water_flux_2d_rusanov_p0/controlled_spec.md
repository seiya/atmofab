# Controlled Spec: 2D shallow water Rusanov flux (component spec)

## 0. Meta information
- `spec_id`: `dynamics_shallow_water_flux_2d_rusanov_p0`
- `spec_version`: `0.2.0`
- `status`: `controlled_draft`
- `spec_kind`: `component`
- `domain`: `dynamics`
- `family`: `shallow_water`

## 1. Responsibility and scope
This `component` is responsible for the interface-flux computation of the shallow water equation. Reconstruction is fixed to first-order `p0`.

## 2. input/output contract
The input variables are the left/right states `U_L`, `U_R` across an `x`-interface and the bottom/top states `U_B`, `U_T` across a `y`-interface, each the conserved-variable vector `U=[h, hu, hv]^T`, together with the gravitational acceleration `g`. The output variables are the numerical flux `F*` across the `x`-interface and `G*` across the `y`-interface, each a length-3 vector ordered as `[h, hu, hv]` and aligned with `U`.

Array placement: all inputs and outputs are interface-located values. The caller supplies the reconstructed left/right and bottom/top states at each interface, and receives the flux at the same interface. The operation is pointwise per interface; vectorized application over a 2D grid is the caller's responsibility.

Units: `h` is in `$\mathrm{m}$`, `hu` and `hv` are in `$\mathrm{m^2\,s^{-1}}$`, `g` is in `$\mathrm{m\,s^{-2}}$`, `F*` and `G*` carry the corresponding flux units (`$\mathrm{m^2\,s^{-1}}$` for the `h` component and `$\mathrm{m^3\,s^{-2}}$` for the `hu` / `hv` components).

Dimensions: each state and flux is a 3-component vector. The component is 2D in the sense that it produces the `x`-direction flux `F*` and the `y`-direction flux `G*` from the respective interface states.

Boundary handling: out of scope. This `component` does not apply boundary conditions and assumes the caller provides valid interface states; boundary treatment is the responsibility of the boundary `component`.

## 3. Operation definition
The published `operation` is `dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux`. The Rusanov flux is defined by
$$
F^{*}(U_L,U_R)=\frac{1}{2}\left(F(U_L)+F(U_R)\right)-\frac{1}{2}a_x\left(U_R-U_L\right)
$$
$$
G^{*}(U_B,U_T)=\frac{1}{2}\left(G(U_B)+G(U_T)\right)-\frac{1}{2}a_y\left(U_T-U_B\right)
$$
and the wave speed is
$$
a_x=\max(|u_L|+c_L,|u_R|+c_R),\quad a_y=\max(|v_B|+c_B,|v_T|+c_T),\quad c=\sqrt{gh}
$$

## 4. Failure conditions and constraints
Treat `h<=0` as invalid input and an error.

## 5. Public API and compatibility
The only published `operation_id` is `dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux`.

A change breaking compatibility of the published signature — the argument names, their order, their types, their ranks, or their `intent`s — is a **breaking change released under a new `spec_version`**, not a silent regeneration: a consumer is certified against the ABI it linked, and `docs/ORCHESTRATION.md` §13b makes a regenerated dependency source invalidate that consumer's readiness. A change to how the surface is CARRIED (the §5.1 / `IR public_api.signatures` REPRESENTATION) is likewise released under a new `spec_version` even when the ABI is byte-identical, because dependency freshness invalidates a stale certified `IR` only via its version.

### 5.1 Canonical interface block
The exact published surface, as a machine-readable **language-neutral** signature block (`module_parameters` / `types` / `procedures`). It describes the published operation abstractly — for every argument: its `name`, neutral `type` (`real` / `integer` / `logical`), `rank`, `intent`, and, for an explicit-shape array, its `dims` bound expressions; plus the value-pinned module parameters the signatures reference. The vocabulary is neutral throughout: a kind value is `float64`, never the Fortran `real64`. The target language's binding (here Fortran: `real(dp)`, assumed-shape `(:)` ranks, `integer, parameter :: dp = real64`, the `<spec_id>__` name) is produced by the language backend (`tools/backends/language/fortran/signatures`), not authored here — so this contract is not tied to Fortran. The generated model source must publish the symbol below with the signature this block describes (formatting, continuations, and comments may differ; the name, argument order, types, ranks, and `intent`s may not). The deterministic gates render this block to the target language and pin it: the `--stage compile` gate cross-checks its symbol set against §5 and pins the `IR`'s `public_api.signatures` / `public_api.module_parameters` == this block, and the `Generate.static` gate pins the generated model source against these signatures (normalized: comments stripped, continuations joined, case-folded, whitespace-insensitive).

`module_parameters` pins `dp` itself, not only the arguments that reference it. Without it a signature written `real(dp)` would match a source that obtained `dp` from any kind at all, so narrowing `dp` to `float32` would pass the stanza comparison — a silent ABI change of exactly the class §5.1 exists to prevent. Pinning it means the generated source must carry the parameter DECLARATION (`integer, parameter :: dp = real64`), not a `use`-rename of it.

```yaml
module_parameters:
- name: dp
  value: float64
types: []
procedures:
- kind: subroutine
  name: dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux
  args:
  - name: U_L
    rank: 1
    intent: in
    spec:
      type: real
      kind: dp
    dims:
    - '3'
  - name: U_R
    rank: 1
    intent: in
    spec:
      type: real
      kind: dp
    dims:
    - '3'
  - name: U_B
    rank: 1
    intent: in
    spec:
      type: real
      kind: dp
    dims:
    - '3'
  - name: U_T
    rank: 1
    intent: in
    spec:
      type: real
      kind: dp
    dims:
    - '3'
  - name: g
    rank: 0
    intent: in
    spec:
      type: real
      kind: dp
  - name: F_star
    rank: 1
    intent: out
    spec:
      type: real
      kind: dp
    dims:
    - '3'
  - name: G_star
    rank: 1
    intent: out
    spec:
      type: real
      kind: dp
    dims:
    - '3'
  - name: a_x
    rank: 0
    intent: out
    spec:
      type: real
      kind: dp
  - name: a_y
    rank: 0
    intent: out
    spec:
      type: real
      kind: dp
  - name: guard_ok
    rank: 0
    intent: out
    spec:
      type: logical
```

## 6. Prohibitions
Forbid automatic switching of the reconstruction order and the implicit application of a limiter.

## 7. Traceability
Require recording the adoption result in `component_catalog.yaml` and `case.resolved.yaml`.

## 8. tests reference
The corresponding `tests.md` is `spec/component/dynamics/shallow_water/dynamics_shallow_water_flux_2d_rusanov_p0/tests.md`, with `test_profile_version` of `0.1.0`.

## 9. AD preparation information
`ad_readiness.enabled` is `true`. `max` and `abs` are made explicit as non-differentiable operations.
