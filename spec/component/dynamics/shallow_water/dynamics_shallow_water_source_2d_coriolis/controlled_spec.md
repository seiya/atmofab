# Controlled Spec: 2D shallow water Coriolis source (component spec)

## 0. Meta information
- `spec_id`: `dynamics_shallow_water_source_2d_coriolis`
- `spec_version`: `0.1.0`
- `status`: `controlled_draft`
- `spec_kind`: `component`
- `domain`: `dynamics`
- `family`: `shallow_water`

## 1. Responsibility and scope
This `component` is responsible only for evaluating the Coriolis source term of the 2D shallow water equation on the interior cells of a grid, from a conserved-variable field and a Coriolis parameter given per interior row. It does not integrate in time, does not apply a boundary condition, and does not define the Coriolis parameter: the profile of `f` over `y` (an `f`-plane, a `β`-plane, or any other row-wise profile) is the vocabulary of the `problem` that evaluates it, and this `component` receives only its values.

## 2. input/output contract
The inputs are the extents `ncomp`, `nx`, `ny`, the Coriolis parameter `f` (`ny` values, one per interior row, at the cell centre, unit `1/s`), and the conserved-variable field `U` (`ncomp` × `nx` × `ny`, ordered `[h, hu, hv]` along the first extent, interior cells only, no ghost cells). The outputs are the source field `S` (`ncomp` × `nx` × `ny`, aligned with `U`, unit of the `hu` / `hv` components `m2/s2` and of the `h` component `m/s`) and the input guard `guard_pass` (§4).

**Published signature.** §5.1 pins the operation's full argument list, and this paragraph describes it: a reader must be able to check the two against each other. The published arguments are exactly those seven, in that order: `ncomp`, `nx`, `ny`, `f`, `U`, `S`, `guard_pass`. `U` is an input and `S` is a separate output array, so the operation never reads a cell it has written.

`f` has the interior extent `ny` and no ghost cells: the source is evaluated on interior cells only, so a ghost value of `f` would be a value nothing reads. The sign convention is that of a `y` axis pointing north: `f>0` in the northern hemisphere, and the source turns an eastward (`+x`) velocity toward the south (`-y`).

Units: `h` is in `m`, `hu` and `hv` are in `m2/s`, `f` is in `1/s`.

## 3. Operation definition
The published `operation` is `dynamics_shallow_water_source_2d_coriolis__apply`. For every interior cell $(i,j)$, $i=1..nx$, $j=1..ny$, the source is
$$
S_{1,i,j}=0,\qquad
S_{2,i,j}=f_j\,(hv)_{i,j},\qquad
S_{3,i,j}=-f_j\,(hu)_{i,j}
$$
which is the discrete form of the continuous Coriolis source $S=[0,\ f\,hv,\ -f\,hu]^T$ of the momentum equations
$$
\frac{\partial (hu)}{\partial t}+\cdots=f\,hv,\qquad
\frac{\partial (hv)}{\partial t}+\cdots=-f\,hu
$$
The evaluation is pointwise: $S_{\cdot,i,j}$ depends on $U_{\cdot,i,j}$ and $f_j$ only. The source does no work on the flow: $(hu)\,S_2+(hv)\,S_3=0$ at every cell, identically.

## 4. Failure conditions and constraints
Treat `ncomp/=3`, `nx<1`, and `ny<1` as invalid input and an error: `guard_pass` is false and `S` is not a valid source field. `h<=0` is not checked here: `h` does not enter the source, and the dry-state guard belongs to the flux `component` and to the `problem`.

## 5. Public API and compatibility
The only published `operation_id` is `dynamics_shallow_water_source_2d_coriolis__apply`.

A change breaking compatibility of the published signature — the argument names, their order, their types, their ranks, or their `intent`s — is a **breaking change released under a new `spec_version`**, not a silent regeneration: a consumer is certified against the ABI it linked, and `docs/ORCHESTRATION.md` §13b makes a regenerated dependency source invalidate that consumer's readiness. A change to how the surface is CARRIED (the §5.1 / `IR public_api.signatures` REPRESENTATION) is likewise released under a new `spec_version` even when the ABI is byte-identical, because dependency freshness invalidates a stale certified `IR` only via its version.

### 5.1 Canonical interface block
The exact published surface, as a machine-readable **language-neutral** signature block (`module_parameters` / `types` / `procedures`). It describes the published operation abstractly — for every argument: its `name`, neutral `type` (`real` / `integer` / `logical`), `rank`, `intent`, and, for an explicit-shape array, its `dims` bound expressions; plus the value-pinned module parameters the signatures reference. The vocabulary is neutral throughout: a kind value is `float64`, never the Fortran `real64`. The target language's binding (here Fortran: `real(dp)`, assumed-shape `(:)` ranks, `integer, parameter :: dp = real64`, the `<spec_id>__` name) is produced by the language backend (`tools/backends/language/fortran/signatures`), not authored here — so this contract is not tied to Fortran. The generated model source must publish the symbol below with the signature this block describes (formatting, continuations, and comments may differ; the name, argument order, types, ranks, and `intent`s may not). The procedure HEADER is compared as published, so a procedure prefix the block does not declare — marking the operation as side-effect-free, say — is a difference too, and is refused. That is worth stating because it is the one difference a reader of the previous sentence would expect to be tolerated: it changes no argument, and a generator has a natural reason to add it. The deterministic gates render this block to the target language and pin it: the `--stage compile` gate cross-checks its symbol set against §5 and pins the `IR`'s `public_api.signatures` / `public_api.module_parameters` == this block, and the `Generate.static` gate pins the generated model source against these signatures (normalized: comments stripped, continuations joined, case-folded, whitespace-insensitive).

`module_parameters` pins `dp` itself, not only the arguments that reference it. The stanza comparison pins an argument's type only SYMBOLICALLY — `real(dp)` matches a source that obtained `dp` from any kind at all — so what `dp` MEANS is pinned here and nowhere else, and narrowing it to `float32` would otherwise be a silent ABI change of exactly the class §5.1 exists to prevent. Pinning it means the generated source must BIND this name exactly once, by the parameter DECLARATION the block pins: an aliasing or plain `use` of the name is refused, and so is a second declaration of it anywhere in the file, including one inside a contained procedure (which would change that procedure's own dummy declarations, and so its ABI). Two checks, and they ask different things — the distinction matters to whoever has to satisfy them. PRESENCE: the pinned declaration must appear, compared as a normalized entity, so spacing, continuations, case and a combined `dp = ..., n = ...` declaration are all immaterial, but a differing ATTRIBUTE LIST is not — an extra `public` or `private` on the pinned declaration itself reads as absent, and the refusal quotes the exact text to write. UNIQUENESS: no OTHER statement may bind the name, and that question is asked with the validator's own Fortran declaration reader rather than a pattern of its own, so the attribute form in any attribute order, a `kind=` in the type specification, and the `parameter (...)` statement form are all seen. What it does not see is a name supplied by an unrestricted `use`, which brings a binding no declaration reader can compare; the lint rule `C121` refuses that construct in the same `Generate.gate` substep, measured rather than assumed.

```yaml
module_parameters:
- name: dp
  value: float64
types: []
procedures:
- kind: subroutine
  name: dynamics_shallow_water_source_2d_coriolis__apply
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
  - name: f
    rank: 1
    intent: in
    spec:
      type: real
      kind: dp
    dims:
    - ny
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
  - name: S
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
Forbid evaluating `f` from a profile or parameters inside this `component` (it receives values only). Forbid an implicit or semi-implicit treatment of the Coriolis term (a rotation of the velocity by the time step): the operation returns the explicit source at the supplied state, and the time treatment is the time-update `component`'s. Forbid a source contribution to the `h` component.

## 7. Traceability
Require recording the adoption result in `component_catalog.yaml` and `case.resolved.yaml`.

## 8. tests reference
The corresponding `tests.md` is `spec/component/dynamics/shallow_water/dynamics_shallow_water_source_2d_coriolis/tests.md`, with `test_profile_version` of `0.1.0`.

## 9. AD preparation information
`ad_readiness.enabled` is `true`. The operation is linear in `U` and holds no non-differentiable operation.
