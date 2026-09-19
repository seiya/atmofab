# Controlled Spec: 2D channel-boundary mapping (component spec)

## 0. Meta information
- `spec_id`: `dynamics_shallow_water_boundary_2d_channel_mirror`
- `spec_version`: `0.1.0`
- `status`: `controlled_draft`
- `spec_kind`: `component`
- `domain`: `dynamics`
- `family`: `shallow_water`

## 1. Responsibility and scope
This `component` is responsible only for the 2D channel ghost mapping: periodic in the `x` direction and a mirror (free-slip wall) in the `y` direction. It maps one ghost-extended field at a time. Whether a field is mirrored with or without a sign reversal at the wall is an input of the operation, not a decision this `component` makes: the caller passes the sign rule that its variable requires (`h` and the wall-tangential momentum `hu` without reversal, the wall-normal momentum `hv` with reversal).

## 2. input/output contract
The inputs are the interior extents `nx`, `ny`, the ghost width `ng`, the padded extents `nx_total`, `ny_total`, the sign rule `odd_at_wall`, and the ghost-extended field `U_in` (`nx_total` × `ny_total`). The outputs are `U_out`, the same field after the channel ghost mapping, and `grid_valid`, which reports whether the supplied grid sizes are valid (§4).

`odd_at_wall` is a logical scalar. When it is false, the `y`-wall ghost cells receive a copy of the mirrored interior cells (an even reflection, for `h` and `hu`). When it is true, they receive the mirrored interior cells with the sign reversed (an odd reflection, for `hv`). The `x`-direction mapping does not depend on `odd_at_wall`.

**Published signature.** §5.1 pins the operation's full argument list, and this paragraph describes it: a reader must be able to check the two against each other. The published arguments are exactly those nine, in that order: `nx`, `ny`, `ng`, `nx_total`, `ny_total`, `odd_at_wall`, `U_in`, `U_out`, `grid_valid`. Input and output are SEPARATE arrays, not one field mapped in place: that keeps the operation free of the aliasing an `intent(inout)` argument would allow, where a caller passing one array for both would read cells the mapping had already overwritten. This is the convention `dynamics_shallow_water_boundary_2d_periodic_copy` pinned under issue #153, and this `component` follows it; the two specs must not disagree on it.

**Known property of the mirror ghost.** The mirror ghost is consistent with the first-order `p0` reconstruction of the flux `component` this `family` uses: at a `y` wall the flux `component` sees a bottom state and a top state whose `h` and `hu` are equal and whose `hv` are opposite, so the Rusanov mass flux and the `hu` flux through the wall are exactly zero and the `hv` flux through the wall reduces to the pressure term $g h_{wall}^2/2$ evaluated with the wall-adjacent cell value of `h`. That pressure term is the cell value, not the wall value, so the wall pressure carries an $O(\Delta y)$ error. This is a property of the mirror-ghost treatment, stated here so that a `problem` adopting it does not judge the wall by a residual the treatment cannot reach.

## 3. Operation definition
The published `operation` is `dynamics_shallow_water_boundary_2d_channel_mirror__apply`. The padded index ranges are $i=1..nx_{total}$ and $j=1..ny_{total}$ with $nx_{total}=nx+2ng$ and $ny_{total}=ny+2ng$; the interior is $i=ng+1..ng+nx$, $j=ng+1..ng+ny$. Let $s=-1$ when `odd_at_wall` is true and $s=+1$ when it is false. The operation applies two steps in this order.

1. **`x` periodic mapping over the interior rows.** For every interior row $j=ng+1..ng+ny$:
$$
U_{out}(i,j)=U_{in}(i,j)\quad (i=ng+1..ng+nx)
$$
$$
U_{out}(i,j)=U_{in}(i+nx,j)\quad (i=1..ng),\qquad
U_{out}(ng+nx+k,j)=U_{in}(ng+k,j)\quad (k=1..ng)
$$
2. **`y` mirror mapping over every column, read from the result of step 1.** For every column $i=1..nx_{total}$ and $k=1..ng$:
$$
U_{out}(i,\,ng-k+1)=s\,U_{out}(i,\,ng+k),\qquad
U_{out}(i,\,ng+ny+k)=s\,U_{out}(i,\,ng+ny-k+1)
$$

Step 2 reads the rows that step 1 has already completed, so the corner ghost cells are the `y` mirror of the `x`-periodic images, and every cell of `U_out` is defined. The ghost rows and ghost columns of `U_in` are never read.

## 4. Failure conditions and constraints
Treat `nx<2`, `ny<2`, `ng<1`, and `ng>ny` as invalid input and an error: `grid_valid` is false and `U_out` is not a valid mapping. `ng>ny` is invalid because a mirror of width `ng` reads `ng` interior rows, which the interior does not hold.

## 5. Public API and compatibility
The only published `operation_id` is `dynamics_shallow_water_boundary_2d_channel_mirror__apply`.

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
  name: dynamics_shallow_water_boundary_2d_channel_mirror__apply
  args:
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
  - name: ng
    rank: 0
    intent: in
    spec:
      type: integer
  - name: nx_total
    rank: 0
    intent: in
    spec:
      type: integer
  - name: ny_total
    rank: 0
    intent: in
    spec:
      type: integer
  - name: odd_at_wall
    rank: 0
    intent: in
    spec:
      type: logical
  - name: U_in
    rank: 2
    intent: in
    spec:
      type: real
      kind: dp
    dims:
    - nx_total
    - ny_total
  - name: U_out
    rank: 2
    intent: out
    spec:
      type: real
      kind: dp
    dims:
    - nx_total
    - ny_total
  - name: grid_valid
    rank: 0
    intent: out
    spec:
      type: logical
```

## 6. Prohibitions
Forbid automatic fallback to a periodic `y` boundary or to any other wall treatment (a direct wall flux, an extrapolated ghost). Forbid deciding the sign rule from anything other than `odd_at_wall`. Forbid reading a ghost cell of `U_in`.

## 7. Traceability
Require recording the adoption result in `component_catalog.yaml` and `case.resolved.yaml`.

## 8. tests reference
The corresponding `tests.md` is `spec/component/dynamics/shallow_water/dynamics_shallow_water_boundary_2d_channel_mirror/tests.md`, with `test_profile_version` of `0.1.0`.

## 9. AD preparation information
`ad_readiness.enabled` is `true`. The periodic-index wrap and the sign reversal at the wall are made explicit as discrete operations.
