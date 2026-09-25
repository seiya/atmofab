# Controlled Spec: 1D advection-diffusion flux (component spec)

## 0. Meta information
- `spec_id`: `dynamics_advdiff_flux_1d_upwind_center2`
- `spec_version`: `0.3.0`
- `status`: `controlled_draft`
- `spec_kind`: `component`
- `domain`: `dynamics`
- `family`: `advection_diffusion`

## 1. Responsibility and scope
This `component` is responsible for computing the interface flux of the 1D advection-diffusion problem at every one of the `nx + 1` faces of the domain, the two end faces included. It does not handle the state update itself, and it does not fill the ghost cells: filling them is the responsibility of the boundary `component`, and this `component` receives a field whose ghost cells are already filled.

## 2. input/output contract
The inputs are `nx`, `ng`, the ghost-extended field `u` (rank 1, holding `nx + 2*ng` cell-centered values — the interior `nx` cells plus `ng` ghost cells at each end, the same layout as the boundary `component`'s `u_out`), `a`, `nu`, `dx`, and `dt`. The outputs are `flux_adv(j+1/2)` and `flux_dif(j+1/2)` at each of the `nx + 1` faces, and `guard_pass`, which reports whether the inputs are valid (§4).

**Index origin.** §3 and `tests.md` state the field in its own index convention `u_{-ng} … u_{nx-1+ng}`, where `u_0 … u_{nx-1}` are the interior cells. The published arrays carry no lower bound of their own — §5.1 pins `u` as rank-1 with no `dims`, so the callee sees position `1` first — and the correspondence is positional: `u_j` is element `j + ng + 1`. The faces are $j+1/2$ for $j=-1,\dots,nx-1$, and $F_{j+1/2}$ is element `j + 2` of `flux_adv` and of `flux_dif`. So the left end face $F_{-1/2}$ is element `1`, and the right end face $F_{nx-1/2}$ is element `nx + 1`.

**Published signature.** §5.1 pins the operation's full argument list, and this paragraph describes it: a reader must be able to check the two against each other. The published arguments are the cell count `nx`, the ghost width `ng`, and the field `u` (`nx + 2*ng` values) with `a`, `nu`, `dx`, `dt` as inputs; `flux_adv` and `flux_dif` (`nx + 1` face values each) and the input guard `guard_pass` (§4) as outputs. `nx`, `ng` and `guard_pass` are part of the published contract even though the physics above does not name them.

## 3. Operation definition
The published `operation` is `dynamics_advdiff_flux_1d_upwind_center2__compute_flux`. The advection flux is defined by first-order upwind, and the diffusion flux by second-order central, at every face $j+1/2$, $j=-1,\dots,nx-1$:
$$
F^{adv}_{j+1/2}=a\,u_j\quad(a>0)
$$
$$
F^{dif}_{j+1/2}=-\nu\frac{u_{j+1}-u_j}{dx}
$$
The cells read are $u_{-1}\dots u_{nx}$, so `ng>=1` suffices.

## 4. Failure conditions and constraints
Treat `a<=0`, `dx<=0`, `dt<=0`, `nx<2`, and `ng<1` as invalid input and an error.

## 5. Public API and compatibility
The only published `operation_id` is `dynamics_advdiff_flux_1d_upwind_center2__compute_flux`. On a `major` compatibility break, separate the `spec_id`.

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
  name: dynamics_advdiff_flux_1d_upwind_center2__compute_flux
  args:
  - name: nx
    rank: 0
    intent: in
    spec:
      type: integer
  - name: ng
    rank: 0
    intent: in
    spec:
      type: integer
  - name: u
    rank: 1
    intent: in
    spec:
      type: real
      kind: dp
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
  - name: flux_adv
    rank: 1
    intent: out
    spec:
      type: real
      kind: dp
    dims:
    - nx + 1
  - name: flux_dif
    rank: 1
    intent: out
    spec:
      type: real
      kind: dp
    dims:
    - nx + 1
  - name: guard_pass
    rank: 0
    intent: out
    spec:
      type: logical
```

## 6. Prohibitions
The discretization order must not be changed automatically. Forbid implicit completion of undefined input.

## 7. Traceability
This `operation_id` requires registration in `component_catalog.yaml`. `case.resolved.yaml` requires recording the adopted `component_id@version`.

## 8. tests reference
The corresponding `tests.md` is `spec/component/dynamics/advection_diffusion/dynamics_advdiff_flux_1d_upwind_center2/tests.md`, with `test_profile_version` of `0.2.0`.

## 9. AD preparation information
`ad_readiness.enabled` is `true`. It includes no non-differentiable operations.
