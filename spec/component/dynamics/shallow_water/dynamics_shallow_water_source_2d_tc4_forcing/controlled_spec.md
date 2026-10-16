# Controlled Spec: 2D shallow water TC4 manufactured forcing (component spec)

## 0. Meta information
- `spec_id`: `dynamics_shallow_water_source_2d_tc4_forcing`
- `spec_version`: `0.2.0`
- `status`: `controlled_draft`
- `spec_kind`: `component`
- `domain`: `dynamics`
- `family`: `shallow_water`

## 1. Responsibility and scope
This `component` is responsible only for evaluating the external forcing that makes one prescribed flow — a geostrophic background that varies across the channel and is uniform along it, plus a Gaussian low translating along the channel — an exact solution of the forced 2D shallow water equations in conservation form, at the interior cell centres and at one time. The prescribed flow is the channel form of the forced translating low of Williamson et al. (1992) §4 (test case 4). Which array axis is the along-channel (periodic) axis is an input of the operation (`along_axis`), not a decision this `component` makes: an `x`-channel passes `1`, and a channel whose along-channel axis is `y` passes `2` ([issue #514](https://github.com/seiya/atmofab/issues/514)). The forcing is a function of position and time and does not read the state of the run that applies it. It does not integrate in time, does not apply a boundary condition, does not evaluate the Coriolis source, and does not define the background: the profile of the background across the channel is the vocabulary of the `problem` that evaluates it, and this `component` receives only its values.

## 2. input/output contract
The inputs are the extents `ncomp`, `nx`, `ny`, the orientation `along_axis`, the interior cell-centre coordinates `x` (`nx` values, unit `m`) and `y` (`ny` values, unit `m`), the time `t` (unit `s`), the background given per interior cell (`nx` × `ny` each) — the Coriolis parameter `f` (unit `1/s`), its derivative across the channel `dfdr` (unit `1/(m s)`), the background velocity along the channel `ubar` (unit `m/s`), its derivative across the channel `dubar` (unit `1/s`), and the background depth `hbar` (unit `m`) — the gravitational acceleration `g` (unit `m/s2`), the channel length along the channel `L_along` (unit `m`), and the five parameters of the low: its streamfunction amplitude `psi0` (unit `m2/s`), its radius `R_low` (unit `m`), its translation speed along the channel `c_tr` (unit `m/s`, signed), and its centre at `t=0`, the along-channel coordinate `s_c0` and the cross-channel coordinate `r_c` (unit `m`). The outputs are the forcing field `S` (`ncomp` × `nx` × `ny`, ordered `[h, hu, hv]` along the first extent, interior cells only, no ghost cells; unit of the `h` component `m/s`, of the `hu` and `hv` components `m2/s2`) and the input guard `guard_pass` (§4).

`along_axis` is an integer scalar. With `along_axis = 1` the channel runs along `x`: the along-channel coordinate of the cell $(i,j)$ is $s=x_i$ and its cross-channel coordinate is $r=y_j$, and the background velocity is $(\bar u, 0)$. With `along_axis = 2` the channel runs along `y`: $s=y_j$, $r=x_i$, and the background velocity is $(0, \bar u)$. `ubar` is the background velocity component along the channel in either orientation, and `dfdr` and `dubar` are derivatives with respect to $r$.

**Published signature.** §5.1 pins the operation's full argument list, and this paragraph describes it: a reader must be able to check the two against each other. The published arguments are exactly those twenty-one, in that order: `ncomp`, `nx`, `ny`, `along_axis`, `x`, `y`, `t`, `f`, `dfdr`, `ubar`, `dubar`, `hbar`, `g`, `L_along`, `psi0`, `R_low`, `c_tr`, `s_c0`, `r_c`, `S`, `guard_pass`. The extents `ncomp`, `nx`, `ny` and the per-cell Coriolis parameter `f` are the vocabulary of `dynamics_shallow_water_source_2d_coriolis`, whose source is added to this forcing by the caller. `S` is the only array the operation writes, and it is not among its inputs, so the operation never reads a cell it has written.

The background arguments have the interior extents `nx` × `ny` and no ghost cells, as `x` and `y` have the interior extents: the forcing is evaluated on interior cells only, so a ghost value would be a value nothing reads. They are given per cell rather than per row or per column so that one argument list serves both orientations; the operation reads each cell's own values. The sign convention of `f` is that of `dynamics_shallow_water_source_2d_coriolis` §2 (`f>0` is a counter-clockwise rotation of the right-handed `(x, y)` plane).

**Precondition on the background.** The background is uniform along the channel — each background argument takes one value along every line of cells along the channel — and is in geostrophic balance, $g\,\nabla\bar h=-f\,\hat k\times\bar{\mathbf u}$ at every interior cell: $g\,\partial_y\bar h=-f\,\bar u$ for `along_axis = 1`, and $g\,\partial_x\bar h=+f\,\bar u$ for `along_axis = 2`. The operation evaluates the cross-channel derivative of $\bar h$ through this relation and does not difference `hbar`, so `hbar` enters only as the depth of the background state. A background that does not satisfy the relation is outside this contract: the operation then returns a forcing for the balanced background rather than for the supplied one, and nothing in the returned values reports the disagreement. `dfdr` is the derivative of the same profile `f` is sampled from, and `dubar` the derivative of the profile `ubar` is sampled from; both are supplied because the operation evaluates them at the cell centre and differences no array.

**Known property at a wall.** The low is a Gaussian in the distance from its centre and is not zero anywhere. At a channel wall a distance $L_r/2$ from the centre's cross-channel coordinate `r_c`, where $L_r$ is the channel width, the streamfunction of the low is $\psi_0 e^{-(L_r/2)^2/R_{low}^2}$, which is $1.1\times10^{-7}\,\psi_0$ at $L_r/2=4R_{low}$; the depth and the wall-normal velocity the low contributes there are of that order — the wall-normal velocity is $1.1\times10^{-7}$ of its own maximum — while the along-wall velocity is $1.0\times10^{-6}$ of its own maximum, one order larger, because it carries the factor $2(r-r_c)/R_{low}^2$ that is largest at the wall. The prescribed flow is therefore an exact solution of the forced equations in the interior and is not a solution of a wall condition that sets the normal velocity to zero: the residual of such a wall condition is the wall value of the low, and a `problem` adopting this `component` states its own wall treatment and the size of that residual.

Units: `S` carries the units stated above, which are the units of a source of the conservation-form equations: the `h` component is a depth tendency and the `hu` / `hv` components are momentum tendencies.

## 3. Operation definition
The published `operation` is `dynamics_shallow_water_source_2d_tc4_forcing__apply`. The prescribed flow is built from the streamfunction of the low, at the interior cell $(i,j)$, $i=1..nx$, $j=1..ny$, with the along-channel and cross-channel coordinates $(s, r)$ of the cell (§2), and at the time $t$:
$$
s_c(t)=s_{c0}+c_{tr}\,t,\qquad
\xi=\frac{\pi\,(s-s_c(t))}{L_{along}},\qquad
\rho^2=\left(\frac{L_{along}}{\pi}\right)^2\sin^2\xi+(r-r_c)^2,\qquad
\psi=\psi_0\,e^{-\rho^2/R_{low}^2}
$$
which is periodic along the channel with the period `L_along` and is the Gaussian $\psi_0 e^{-d^2/R_{low}^2}$ of the distance `d` from the centre wherever $|s-s_c(t)|\ll L_{along}$. With $k_\psi=L_{along}/(\pi R_{low}^2)$ the derivatives of $\psi$ along and across the channel are
$$
\psi_s=-k_\psi\sin 2\xi\;\psi,\qquad
\psi_r=-\frac{2(r-r_c)}{R_{low}^2}\;\psi,\qquad
\psi_t=-c_{tr}\,\psi_s
$$
$$
\psi_{ss}=\left(-\frac{2}{R_{low}^2}\cos 2\xi+k_\psi^2\sin^2 2\xi\right)\psi,\qquad
\psi_{sr}=\frac{2(r-r_c)L_{along}}{\pi R_{low}^4}\sin 2\xi\;\psi,\qquad
\psi_{rr}=\left(-\frac{2}{R_{low}^2}+\frac{4(r-r_c)^2}{R_{low}^4}\right)\psi
$$
and its derivatives in `x` and `y` are these, assigned by the orientation:
$$
\texttt{along\_axis}=1:\quad \psi_x=\psi_s,\ \psi_y=\psi_r,\ \psi_{xx}=\psi_{ss},\ \psi_{xy}=\psi_{sr},\ \psi_{yy}=\psi_{rr}
$$
$$
\texttt{along\_axis}=2:\quad \psi_x=\psi_r,\ \psi_y=\psi_s,\ \psi_{xx}=\psi_{rr},\ \psi_{xy}=\psi_{sr},\ \psi_{yy}=\psi_{ss}
$$
The low contributes the streamfunction flow $(-\psi_y,\ \psi_x)$ in either orientation, and the background its velocity along the channel. With `along_axis = 1` the prescribed flow and its derivatives are
$$
\tilde u=\bar u-\psi_y,\qquad \tilde v=\psi_x,\qquad \tilde h=\bar h+\frac{f\,\psi}{g}
$$
$$
\tilde u_x=-\psi_{xy},\quad \tilde u_y=\bar u'-\psi_{yy},\quad \tilde u_t=c_{tr}\,\psi_{xy},\qquad
\tilde v_x=\psi_{xx},\quad \tilde v_y=\psi_{xy},\quad \tilde v_t=-c_{tr}\,\psi_{xx}
$$
$$
\tilde h_x=\frac{f\,\psi_x}{g},\qquad
\tilde h_y=-\frac{f\,\bar u}{g}+\frac{f'\,\psi+f\,\psi_y}{g},\qquad
\tilde h_t=-\frac{c_{tr}\,f\,\psi_x}{g}
$$
and with `along_axis = 2` they are
$$
\tilde u=-\psi_y,\qquad \tilde v=\bar u+\psi_x,\qquad \tilde h=\bar h+\frac{f\,\psi}{g}
$$
$$
\tilde u_x=-\psi_{xy},\quad \tilde u_y=-\psi_{yy},\quad \tilde u_t=c_{tr}\,\psi_{yy},\qquad
\tilde v_x=\bar u'+\psi_{xx},\quad \tilde v_y=\psi_{xy},\quad \tilde v_t=-c_{tr}\,\psi_{xy}
$$
$$
\tilde h_x=\frac{f\,\bar u}{g}+\frac{f'\,\psi+f\,\psi_x}{g},\qquad
\tilde h_y=\frac{f\,\psi_y}{g},\qquad
\tilde h_t=-\frac{c_{tr}\,f\,\psi_y}{g}
$$
where $\bar u$, $\bar u'$, $f$, $f'$ and $\bar h$ are `ubar`, `dubar`, `f`, `dfdr` and `hbar` at the cell $(i,j)$, the time derivatives are $-c_{tr}$ times the along-channel derivatives of the low's terms (the background is steady), and the first term of the cross-channel derivative of $\tilde h$ is the geostrophic relation of §2. The residuals of the advective-form equations are, in either orientation,
$$
F_u=\tilde u_t+\tilde u\,\tilde u_x+\tilde v\,\tilde u_y+g\,\tilde h_x-f\,\tilde v,\qquad
F_v=\tilde v_t+\tilde u\,\tilde v_x+\tilde v\,\tilde v_y+g\,\tilde h_y+f\,\tilde u
$$
$$
F_h=\tilde h_t+\tilde u\,\tilde h_x+\tilde v\,\tilde h_y+\tilde h\,(\tilde u_x+\tilde v_y)
$$
and the published forcing is their conservation form
$$
S_{1,i,j}=F_h,\qquad
S_{2,i,j}=\tilde h\,F_u+\tilde u\,F_h,\qquad
S_{3,i,j}=\tilde h\,F_v+\tilde v\,F_h
$$
Each of the three residuals is written as the full residual of its equation and is not reduced by a cancellation the prescribed flow satisfies: with `along_axis = 1`, $g\tilde h_x-f\tilde v$ is zero and $g\tilde h_y+f\tilde u$ is $f'\psi$ for this flow; with `along_axis = 2`, $g\tilde h_x-f\tilde v$ is $f'\psi$ and $g\tilde h_y+f\tilde u$ is zero; each is evaluated as written. The evaluation is pointwise: $S_{\cdot,i,j}$ depends on $x_i$, $y_j$, $t$, the background at $(i,j)$ and the scalar parameters only, and on no element of `S`. With $\psi_0=0$ the prescribed flow is the geostrophic background, $F_u$, $F_v$ and $F_h$ are each zero, and `S` is zero at every cell. The divergence $\tilde u_x+\tilde v_y$ of the prescribed flow is zero at every cell, because the low contributes a streamfunction flow and the background has no cross-channel component; the term is written because the definition is the residual of the conservation-form equation, and it contributes nothing for this flow.

The two orientations are each other's image under the exchange of the axes, which reverses the sense of rotation and the sign of a streamfunction: the forcing at `along_axis = 2` for the arguments $(nx, ny, x, y, f, f', \bar u, \bar u', \bar h, \psi_0)$ is, cell by cell, the forcing at `along_axis = 1` for $(ny, nx, y, x, -f^T, -f'^T, \bar u^T, \bar u'^T, \bar h^T, -\psi_0)$ (the other scalars unchanged) transposed, with its second and third components exchanged — up to the order of the floating-point operations, since the advective products of a residual are summed in the order §3 writes them. This is not an instruction to compute one orientation from the other: §6 forbids that, and `tests.md` compares the two orientations as a symmetry pair (`docs/TESTS.md` §Symmetry pairs).

## 4. Failure conditions and constraints
Treat `ncomp/=3`, `nx<1`, `ny<1`, an `along_axis` other than `1` or `2`, `R_low<=0`, and `L_along<=0` as invalid input and an error: `guard_pass` is false, and every element of `S` is set to zero. The zero is what the operation publishes on a rejected input — not a valid forcing, and stated so that the rejection has an observable consequence in the output; a caller reads `guard_pass`, not the zeros. No element of the input arrays is read on a rejected input. `R_low<=0` and `L_along<=0` are invalid because the formulas of §3 divide by $R_{low}^2$ and by `L_along`. `h<=0` is not checked here: the state does not enter the forcing, and the dry-state guard belongs to the flux `component` and to the `problem`.

## 5. Public API and compatibility
The only published `operation_id` is `dynamics_shallow_water_source_2d_tc4_forcing__apply`.

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
  name: dynamics_shallow_water_source_2d_tc4_forcing__apply
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
  - name: along_axis
    rank: 0
    intent: in
    spec:
      type: integer
  - name: x
    rank: 1
    intent: in
    spec:
      type: real
      kind: dp
    dims:
    - nx
  - name: y
    rank: 1
    intent: in
    spec:
      type: real
      kind: dp
    dims:
    - ny
  - name: t
    rank: 0
    intent: in
    spec:
      type: real
      kind: dp
  - name: f
    rank: 2
    intent: in
    spec:
      type: real
      kind: dp
    dims:
    - nx
    - ny
  - name: dfdr
    rank: 2
    intent: in
    spec:
      type: real
      kind: dp
    dims:
    - nx
    - ny
  - name: ubar
    rank: 2
    intent: in
    spec:
      type: real
      kind: dp
    dims:
    - nx
    - ny
  - name: dubar
    rank: 2
    intent: in
    spec:
      type: real
      kind: dp
    dims:
    - nx
    - ny
  - name: hbar
    rank: 2
    intent: in
    spec:
      type: real
      kind: dp
    dims:
    - nx
    - ny
  - name: g
    rank: 0
    intent: in
    spec:
      type: real
      kind: dp
  - name: L_along
    rank: 0
    intent: in
    spec:
      type: real
      kind: dp
  - name: psi0
    rank: 0
    intent: in
    spec:
      type: real
      kind: dp
  - name: R_low
    rank: 0
    intent: in
    spec:
      type: real
      kind: dp
  - name: c_tr
    rank: 0
    intent: in
    spec:
      type: real
      kind: dp
  - name: s_c0
    rank: 0
    intent: in
    spec:
      type: real
      kind: dp
  - name: r_c
    rank: 0
    intent: in
    spec:
      type: real
      kind: dp
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
Forbid evaluating the forcing from the state of the run (the forcing is a function of position and time; a term read from `h`, `hu` or `hv` is forbidden). Forbid evaluating it at a time other than the one supplied: the caller passes the time of the stage state the forcing is added to, and the operation uses that value alone. Forbid re-deriving the background inside this `component` — the per-cell values are received, not computed from a profile or its parameters — and forbid differencing a received array in place of the supplied derivative. Forbid deciding the orientation from anything other than `along_axis`, and forbid realising one orientation through the other — exchanging the arguments to the other orientation's form, evaluating there and exchanging the result back: each orientation evaluates §3 with its own assignment of $(s, r)$ and its own prescribed flow, because `tests.md` compares the two orientations as a symmetry pair and an implementation that derives one from the other compares a path with itself (`docs/TESTS.md` §Symmetry pairs). Forbid changing the form of the low (its Gaussian shape, its periodic argument, its translation along the channel at the constant signed speed `c_tr`) and forbid reducing a residual of §3 by a cancellation the prescribed flow satisfies.

## 7. Traceability
Require recording the adoption result in `component_catalog.yaml` and, by the host, in the adopting `spec`'s `<ir_ref>/dependency_graph.json` (`all_nodes`, at the resolved `version`).

## 8. tests reference
The corresponding `tests.md` is `spec/component/dynamics/shallow_water/dynamics_shallow_water_source_2d_tc4_forcing/tests.md`, with `test_profile_version` of `0.3.0`.

## 9. AD preparation information
`ad_readiness.enabled` is `true`. The operation holds no non-differentiable operation: the discrete operations of §3 are `exp`, `sin`, `cos` and arithmetic, each differentiable in the inputs, and `along_axis` selects between two such evaluations. The operation does not depend on the state, so its derivative with respect to the state is zero.
