# Controlled Spec: 2D channel-boundary mapping (component spec)

## 0. Meta information
- `spec_id`: `dynamics_shallow_water_boundary_2d_channel_mirror`
- `spec_version`: `0.2.0`
- `status`: `controlled_draft`
- `spec_kind`: `component`
- `domain`: `dynamics`
- `family`: `shallow_water`

## 1. Responsibility and scope
This `component` is responsible only for the 2D channel ghost mapping: periodic along one array axis and a mirror (free-slip wall) along the other. Which array axis carries the walls is an input of the operation (`wall_axis`), not a decision this `component` makes: the `x`-channel of a `problem` (periodic in `x`, walls in `y`) passes the second axis, and a channel whose along-channel axis is `y` passes the first ([issue #514](https://github.com/seiya/atmofab/issues/514)). It maps one ghost-extended field at a time. Whether a field is mirrored with or without a sign reversal at the wall is an input of the operation, not a decision this `component` makes: the caller passes the sign rule that its variable requires (`h` and the wall-tangential momentum without reversal, the wall-normal momentum with reversal: `hu` and `hv` respectively when the walls are on the second axis, `hv` and `hu` when they are on the first).

## 2. input/output contract
The inputs are the interior extents `nx`, `ny`, the ghost width `ng`, the padded extents `nx_total`, `ny_total`, the wall axis `wall_axis`, the sign rule `odd_at_wall`, and the ghost-extended field `U_in` (`nx_total` × `ny_total`). The outputs are `U_out`, the same field after the channel ghost mapping, and `grid_valid`, which reports whether the supplied grid sizes are valid (§4).

`wall_axis` is an integer scalar naming the array axis whose two ends are walls: `2` puts the walls at the first and last interior rows `j` (the walls of an `x`-channel, at `y=0` and `y=L_y`) and makes the first axis periodic; `1` puts them at the first and last interior columns `i` (the walls of a `y`-channel, at `x=0` and `x=L_x`) and makes the second axis periodic. `odd_at_wall` is a logical scalar. When it is false, the wall ghost cells receive a copy of the mirrored interior cells (an even reflection, for `h` and the wall-tangential momentum). When it is true, they receive the mirrored interior cells with the sign reversed (an odd reflection, for the wall-normal momentum). The periodic mapping does not depend on `odd_at_wall`.

**Published signature.** §5.1 pins the operation's full argument list, and this paragraph describes it: a reader must be able to check the two against each other. The published arguments are exactly those ten, in that order: `nx`, `ny`, `ng`, `nx_total`, `ny_total`, `wall_axis`, `odd_at_wall`, `U_in`, `U_out`, `grid_valid`. Input and output are SEPARATE arrays, not one field mapped in place: that keeps the operation free of the aliasing an `intent(inout)` argument would allow, where a caller passing one array for both would read cells the mapping had already overwritten. This is the convention `dynamics_shallow_water_boundary_2d_periodic_copy` pinned under issue #153, and this `component` follows it; the two specs must not disagree on it.

**Known property of the mirror ghost.** The mirror ghost is consistent with the first-order `p0` reconstruction of the flux `component` this `family` uses. It is stated here for walls on the second axis (`wall_axis = 2`, walls in `y`); with walls on the first axis the same holds with `x` and `y`, `u` and `v`, `hu` and `hv` exchanged. At a `y` wall the flux `component` sees a bottom state and a top state whose `h` and `hu` are equal and whose `hv` are opposite, so the Rusanov mass flux and the `hu` flux through the wall are exactly zero, and the `hv` flux through the wall is $h v^2 + g h^2/2 + a_y\, h v_n$ with `h`, `v` and the wave speed $a_y$ taken from the wall-adjacent cell, where $v_n$ is that cell's velocity component directed out of the domain through the wall ($v_n=v$ at the top wall, $v_n=-v$ at the bottom wall; the Rusanov term $-\tfrac12 a_y (U_T-U_B)$ changes sign with the side the ghost is on). The exact wall flux is $[0,\,0,\,g h_{wall}^2/2]^T$ with $v=0$ at the wall, so the treatment evaluates the wall pressure with the cell-centre `h` instead of the wall `h` — an $O(\Delta y)$ error — and its terms in `v` vanish only as the wall-adjacent `v` does. This is a property of the mirror-ghost treatment, stated here so that a `problem` adopting it does not judge the wall by a residual the treatment cannot reach.

## 3. Operation definition
The published `operation` is `dynamics_shallow_water_boundary_2d_channel_mirror__apply`. The padded index ranges are $i=1..nx_{total}$ and $j=1..ny_{total}$ with $nx_{total}=nx+2ng$ and $ny_{total}=ny+2ng$; the interior is $i=ng+1..ng+nx$, $j=ng+1..ng+ny$. Let $s=-1$ when `odd_at_wall` is true and $s=+1$ when it is false. The operation applies two steps in this order: the periodic mapping along the axis that is not `wall_axis`, then the mirror mapping along `wall_axis`.

**`wall_axis = 2`** (periodic in `i`, walls at the ends of `j`):

1. **Periodic mapping along `i` over the interior rows.** For every interior row $j=ng+1..ng+ny$:
$$
U_{out}(i,j)=U_{in}(i,j)\quad (i=ng+1..ng+nx)
$$
$$
U_{out}(i,j)=U_{in}(i+nx,j)\quad (i=1..ng),\qquad
U_{out}(ng+nx+k,j)=U_{in}(ng+k,j)\quad (k=1..ng)
$$
2. **Mirror mapping along `j` over every column, read from the result of step 1.** For every column $i=1..nx_{total}$ and $k=1..ng$:
$$
U_{out}(i,\,ng-k+1)=s\,U_{out}(i,\,ng+k),\qquad
U_{out}(i,\,ng+ny+k)=s\,U_{out}(i,\,ng+ny-k+1)
$$

**`wall_axis = 1`** (periodic in `j`, walls at the ends of `i`):

1. **Periodic mapping along `j` over the interior columns.** For every interior column $i=ng+1..ng+nx$:
$$
U_{out}(i,j)=U_{in}(i,j)\quad (j=ng+1..ng+ny)
$$
$$
U_{out}(i,j)=U_{in}(i,j+ny)\quad (j=1..ng),\qquad
U_{out}(i,ng+ny+k)=U_{in}(i,ng+k)\quad (k=1..ng)
$$
2. **Mirror mapping along `i` over every row, read from the result of step 1.** For every row $j=1..ny_{total}$ and $k=1..ng$:
$$
U_{out}(ng-k+1,\,j)=s\,U_{out}(ng+k,\,j),\qquad
U_{out}(ng+nx+k,\,j)=s\,U_{out}(ng+nx-k+1,\,j)
$$

In either orientation, step 2 reads the lines that step 1 has already completed, so the corner ghost cells are the mirror of the periodic images, and every cell of `U_out` is defined. The ghost rows and ghost columns of `U_in` are never read.

The two orientations are each other's transpose: the mapping at `wall_axis = 1` of a field $V$ with extents $(nx, ny)$ equals the transpose of the mapping at `wall_axis = 2` of $V^T$ with extents $(ny, nx)$, cell by cell. The mapping also commutes with the reversal of either array axis (the padded field read from its last index to its first), because the periodic images and the mirror images of a reversed field are the reversed images. Neither statement is an instruction to compute one orientation from the other: §6 forbids that, and `tests.md` compares the two orientations as a symmetry pair (`docs/TESTS.md` §Symmetry pairs).

## 4. Failure conditions and constraints
Treat `nx<2`, `ny<2`, `ng<1`, `ng>nx`, `ng>ny`, and a `wall_axis` other than `1` or `2` as invalid input and an error: `grid_valid` is false and `U_out` is not a valid mapping. `ng>nx` and `ng>ny` are invalid in either orientation, because a periodic wrap of width `ng` reads `ng` interior lines along the periodic axis and a mirror of width `ng` reads `ng` interior lines along the wall axis, which the interior does not hold; either reads outside the interior — a ghost line of `U_in` for the wrap, a line of `U_out` that step 1 never wrote for the mirror — which §3 never does.

## 5. Public API and compatibility
The only published `operation_id` is `dynamics_shallow_water_boundary_2d_channel_mirror__apply`.

A change breaking compatibility of the published signature — the argument names, their order, their types, their ranks, or their `intent`s — is a **breaking change released under a new `spec_version`**, not a silent regeneration: a consumer is certified against the ABI it linked, and `docs/ORCHESTRATION.md` §13b makes a regenerated dependency source invalidate that consumer's readiness. A change to how the surface is CARRIED (the §5.1 / `IR public_api.signatures` REPRESENTATION) is likewise released under a new `spec_version` even when the ABI is byte-identical, because dependency freshness invalidates a stale certified `IR` only via its version.

### 5.1 Canonical interface block
The exact published surface, as a machine-readable **language-neutral** signature block (`module_parameters` / `types` / `procedures`). It describes the published operation abstractly — for every argument: its `name`, neutral `type` (`real` / `integer` / `logical`), `rank`, `intent`, and, for an explicit-shape array, its `dims` bound expressions; plus the value-pinned module parameters the signatures reference. The vocabulary is neutral throughout: a kind value is `float64`, never the Fortran `real64`. The target language's binding (here Fortran: `real(dp)`, assumed-shape `(:)` ranks, `integer, parameter :: dp = real64`, the `<spec_id>__` name) is produced by the language backend (`tools/backends/language/fortran/signatures`), not authored here — so this contract is not tied to Fortran. The generated model source must publish the symbol below with the signature this block describes (formatting, continuations, and comments may differ; the name, argument order, types, ranks, and `intent`s may not). The procedure HEADER is compared as published, so a procedure prefix the block does not declare — marking the operation as side-effect-free, say — is a difference too, and is refused. That is worth stating because it is the one difference a reader of the previous sentence would expect to be tolerated: it changes no argument, and a generator has a natural reason to add it. The deterministic gates render this block to the target language and pin it: the `--stage compile` gate cross-checks its symbol set against §5 and pins the `IR`'s `public_api.signatures` / `public_api.module_parameters` == this block, and the `Generate.static` gate pins the generated model source against these signatures (normalized: comments stripped, continuations joined, case-folded, whitespace-insensitive).

`module_parameters` pins `dp` itself, not only the arguments that reference it. The stanza comparison pins an argument's type only SYMBOLICALLY — `real(dp)` matches a source that obtained `dp` from any kind at all — so what `dp` MEANS is pinned here and nowhere else, and narrowing it to `float32` would otherwise be a silent ABI change of exactly the class §5.1 exists to prevent. Pinning it means the generated source must BIND this name exactly once, by the parameter DECLARATION the block pins: an aliasing or plain `use` of the name is refused, and so is a second declaration of it anywhere in the file, including one inside a contained procedure (which would change that procedure's own dummy declarations, and so its ABI). Two checks, and they ask different things — the distinction matters to whoever has to satisfy them. PRESENCE: the pinned declaration must appear, compared as a normalized entity, so spacing, continuations, case and a combined `dp = ..., n = ...` declaration are all immaterial, and so is an accessibility attribute (`public` / `private`) on the pinned declaration itself, but any other difference in the ATTRIBUTE LIST is not, and the refusal quotes the exact text to write. UNIQUENESS: no OTHER statement may bind the name, and that question is asked with the validator's own Fortran declaration reader rather than a pattern of its own, so the attribute form in any attribute order, a `kind=` in the type specification, and the `parameter (...)` statement form are all seen. What it does not see is a name supplied by an unrestricted `use`, which brings a binding no declaration reader can compare; the lint rule `C121` refuses that construct in the same `Generate.gate` substep, measured rather than assumed.

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
  - name: wall_axis
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
Forbid automatic fallback to a periodic boundary on the wall axis or to any other wall treatment (a direct wall flux, an extrapolated ghost). Forbid deciding the sign rule from anything other than `odd_at_wall`, and the wall axis from anything other than `wall_axis`. Forbid realising one orientation through the other — transposing the field, mapping it at the other `wall_axis` and transposing the result back: each orientation is mapped along its own axes as §3 states it, because `tests.md` compares the two orientations as a symmetry pair and an implementation that derives one from the other compares a path with itself (`docs/TESTS.md` §Symmetry pairs). Forbid reading a ghost cell of `U_in`.

## 7. Traceability
Require recording the adoption result in `component_catalog.yaml` and, by the host, in the adopting `spec`'s `<ir_ref>/dependency_graph.json` (`all_nodes`, at the resolved `version`).

## 8. tests reference
The corresponding `tests.md` is `spec/component/dynamics/shallow_water/dynamics_shallow_water_boundary_2d_channel_mirror/tests.md`, with `test_profile_version` of `0.3.0`.

## 9. AD preparation information
`ad_readiness.enabled` is `true`. The periodic-index wrap and the sign reversal at the wall are made explicit as discrete operations; the choice of `wall_axis` is a selection between two such mappings, not a non-differentiable operation on the field.
