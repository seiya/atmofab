# Controlled Spec: 2D `MUSCL` reconstruction with the `MC` limiter (component spec)

## 0. Meta information
- `spec_id`: `dynamics_shallow_water_reconstruction_2d_muscl_mc`
- `spec_version`: `0.1.0`
- `status`: `controlled_draft`
- `spec_kind`: `component`
- `domain`: `dynamics`
- `family`: `shallow_water`

## 1. Responsibility and scope
This `component` is responsible only for the piecewise-linear (`MUSCL`, `p1`) reconstruction of one ghost-filled scalar field: from the cell values it produces the left / right states at every `x` interface and the bottom / top states at every `y` interface of the interior, with the slope of each cell limited by the monotonized-central (`MC`) limiter. It reconstructs one field at a time, and the caller applies it to each conserved variable separately (`h`, `hu`, `hv`), so the limiter acts per conserved variable. It does not fill ghost cells, does not compute a flux, and does not integrate in time: the ghost cells of its input are filled by a boundary `component` before the call, and its outputs are the interface states a flux `component` consumes.

## 2. input/output contract
The inputs are the interior extents `nx`, `ny`, the ghost width `ng`, the padded extents `nx_total`, `ny_total`, and the ghost-filled field `U_in` (`nx_total` × `ny_total`). The outputs are the `x`-interface states `U_L`, `U_R` (`nx+1` × `ny` each), the `y`-interface states `U_B`, `U_T` (`nx` × `ny+1` each), and `grid_valid`, which reports whether the supplied grid sizes are valid (§4).

**Index conventions.** The padded index ranges are $i=1..nx_{total}$ and $j=1..ny_{total}$ with $nx_{total}=nx+2ng$ and $ny_{total}=ny+2ng$; the interior is $i=ng+1..ng+nx$, $j=ng+1..ng+ny$. The `x` interfaces are numbered $m=1..nx+1$: interface $m$ lies between the padded cells $c_L=ng+m-1$ and $c_R=ng+m$, so interface $1$ is the west face of the first interior cell (its left cell $ng$ is a ghost cell) and interface $nx+1$ is the east face of the last interior cell (its right cell $ng+nx+1$ is a ghost cell). `U_L(m,j)` and `U_R(m,j)` are the states on the two sides of interface $m$ in the interior row $j=1..ny$, which is the padded row $ng+j$. The `y` interfaces are numbered $n=1..ny+1$ in the same way: interface $n$ lies between the padded rows $c_B=ng+n-1$ and $c_T=ng+n$, and `U_B(i,n)`, `U_T(i,n)` are the states below and above it in the interior column $i=1..nx$, which is the padded column $ng+i$.

**Published signature.** §5.1 pins the operation's full argument list, and this paragraph describes it: a reader must be able to check the two against each other. The published arguments are exactly those eleven, in that order: `nx`, `ny`, `ng`, `nx_total`, `ny_total`, `U_in`, `U_L`, `U_R`, `U_B`, `U_T`, `grid_valid`. The extents `nx`, `ny`, `ng`, `nx_total`, `ny_total` are the vocabulary of `dynamics_shallow_water_boundary_2d_channel_mirror`, whose output is the field this operation reads. `U_in` is an input and the four interface arrays are separate outputs, so the operation never reads a cell it has written. The interface arrays are explicit-shape: `U_L` and `U_R` have the extents `nx + 1` × `ny`, `U_B` and `U_T` the extents `nx` × `ny + 1`.

**Ghost cells are read.** The slope of a cell reads its two neighbours, and the interface states at the edges of the interior read the slope of the adjacent ghost cell, so the operation reads the padded columns $ng-1..ng+nx+2$ and the padded rows $ng-1..ng+ny+2$ of `U_in`; every value it reads is a value the caller's boundary `component` has filled. This is the origin of the bound `ng>=2` of §4. The reconstruction is dimensionless: the slope is a difference of neighbouring cell values, not divided by a cell width, and an interface state is the cell value plus or minus half of that difference, so no grid spacing enters.

**Known property with a mirror ghost.** When the field was filled by a mirror ghost at a `y` wall (the mapping of `dynamics_shallow_water_boundary_2d_channel_mirror`), the two cells adjacent to the wall interface have slopes that mirror each other: for an even reflection the difference across the wall interface is zero, so both slopes are zero; for an odd reflection the two one-sided differences of the ghost cell are those of the interior cell exchanged, so the two slopes are equal. At the wall interface the bottom and top states are therefore equal for an even field (`h`, `hu`) and opposite for an odd field (`hv`). A Rusanov flux evaluated on those states carries an exactly zero mass flux and an exactly zero `hu` flux through the wall, the same property the first-order `p0` reconstruction has, so a `problem` adopting this reconstruction with the mirror ghost conserves mass to round-off. The wall-face `h` of an even field equals the cell-centre `h` of the wall-adjacent cell (its slope is zero), so the wall pressure is evaluated with the cell-centre `h`, as it is under the `p0` reconstruction; the $O(\Delta y)$ wall-pressure property stated by `dynamics_shallow_water_boundary_2d_channel_mirror` §2 holds unchanged.

Units: `U_in` and the four interface arrays carry the unit of the reconstructed field. No other unit enters.

## 3. Operation definition
The published `operation` is `dynamics_shallow_water_reconstruction_2d_muscl_mc__reconstruct`. Write $U_{c,j}$ for `U_in` at the padded cell $(c,j)$. The limited slope of a cell in the `x` direction is built from its two one-sided differences $a=U_{c,j}-U_{c-1,j}$ and $b=U_{c+1,j}-U_{c,j}$:
$$
\sigma^x_{c,j}=\begin{cases}0 & ab\le 0\\ \operatorname{sign}(a)\,\min\!\left(2|a|,\ 2|b|,\ \tfrac12|a+b|\right) & ab>0\end{cases}
$$
and the slope in the `y` direction $\sigma^y_{c,j}$ is the same function of $a=U_{c,j}-U_{c,j-1}$ and $b=U_{c,j+1}-U_{c,j}$. The interface states are
$$
U_L(m,j)=U_{c_L,\,ng+j}+\tfrac12\,\sigma^x_{c_L,\,ng+j},\qquad
U_R(m,j)=U_{c_R,\,ng+j}-\tfrac12\,\sigma^x_{c_R,\,ng+j},\qquad
c_L=ng+m-1,\ c_R=ng+m,\quad m=1..nx+1,\ j=1..ny
$$
$$
U_B(i,n)=U_{ng+i,\,c_B}+\tfrac12\,\sigma^y_{ng+i,\,c_B},\qquad
U_T(i,n)=U_{ng+i,\,c_T}-\tfrac12\,\sigma^y_{ng+i,\,c_T},\qquad
c_B=ng+n-1,\ c_T=ng+n,\quad i=1..nx,\ n=1..ny+1
$$
The `x` slope is needed at the padded columns $c=ng..ng+nx+1$ of every interior row and the `y` slope at the padded rows $c=ng..ng+ny+1$ of every interior column; each cell's slope is one value, used on both of its faces. The three arguments of the minimum are the two one-sided differences doubled and the central difference; the limiter returns the central difference wherever it is the smallest of the three, and a one-sided bound where it is not. A constant field has every slope zero, and a linear field has the slope equal to its exact difference, so the reconstruction of a linear field is exact: both states at every interface equal the value of the linear field at the interface. At a local extremum the two differences have opposite signs and the slope is zero, and next to a step the difference on the flat side is zero, so the slope is zero there too: no interface state lies outside the range of the two cell values it is built from.

## 4. Failure conditions and constraints
Treat `ng<2`, `nx<1`, `ny<1`, `nx_total/=nx+2*ng`, and `ny_total/=ny+2*ng` as invalid input and an error: `grid_valid` is false and the interface arrays are not a valid reconstruction. `ng<2` is invalid because the slope of the ghost cell adjacent to the interior reads the ghost cell beyond it (§2), which a ghost width of one does not hold; the two padded-extent clauses are invalid because the index formulas of §3 address `U_in` through `nx`, `ny` and `ng`, and an extent that disagrees with them addresses cells the array does not hold.

## 5. Public API and compatibility
The only published `operation_id` is `dynamics_shallow_water_reconstruction_2d_muscl_mc__reconstruct`.

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
  name: dynamics_shallow_water_reconstruction_2d_muscl_mc__reconstruct
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
  - name: U_in
    rank: 2
    intent: in
    spec:
      type: real
      kind: dp
    dims:
    - nx_total
    - ny_total
  - name: U_L
    rank: 2
    intent: out
    spec:
      type: real
      kind: dp
    dims:
    - nx + 1
    - ny
  - name: U_R
    rank: 2
    intent: out
    spec:
      type: real
      kind: dp
    dims:
    - nx + 1
    - ny
  - name: U_B
    rank: 2
    intent: out
    spec:
      type: real
      kind: dp
    dims:
    - nx
    - ny + 1
  - name: U_T
    rank: 2
    intent: out
    spec:
      type: real
      kind: dp
    dims:
    - nx
    - ny + 1
  - name: grid_valid
    rank: 0
    intent: out
    spec:
      type: logical
```

## 6. Prohibitions
Forbid replacing the `MC` limiter by another limiter (`minmod`, `van Leer`, `superbee`) or by the unlimited central difference, and forbid omitting the limiter. Forbid using a slope other than the cell's own on either of its faces (a slope taken from a neighbouring cell, or a slope evaluated from different inputs on the two faces of one cell; evaluating the same function of the same three cell values once per face is the same slope). Forbid reading a cell of `U_in` outside the padded columns $ng-1..ng+nx+2$ and the padded rows $ng-1..ng+ny+2$ of §2, and forbid re-deriving or overwriting a ghost value of `U_in` in place of the value the caller filled. Forbid dividing the slope by a grid spacing.

## 7. Traceability
Require recording the adoption result in `component_catalog.yaml` and `case.resolved.yaml`.

## 8. tests reference
The corresponding `tests.md` is `spec/component/dynamics/shallow_water/dynamics_shallow_water_reconstruction_2d_muscl_mc/tests.md`, with `test_profile_version` of `0.1.0`.

## 9. AD preparation information
`ad_readiness.enabled` is `true`. The limiter is a non-differentiable operation and is made explicit as one: the branch on the sign of $ab$, the `min` over the three slope candidates, and `sign` are the discrete operations of §3.
