# Controlled Spec: 2D shallow water channel problem (problem spec)

## 0. Meta information
- `spec_id`: `shallow_water2d_channel`
- `spec_version`: `0.1.0`
- `status`: `controlled_draft`
- `spec_kind`: `problem`
- `domain`: `dynamics`
- `family`: `shallow_water`

## 1. Problem definition
The target is the conservative form of the 2D shallow water equation on a flat bottom with the Coriolis force, on a channel that is periodic in `x` and bounded by free-slip walls in `y`:
$$
\frac{\partial h}{\partial t}
+ \frac{\partial (hu)}{\partial x}
+ \frac{\partial (hv)}{\partial y}
= 0
$$
$$
\frac{\partial (hu)}{\partial t}
+ \frac{\partial }{\partial x}\left(hu^2 + \frac{1}{2} g h^2\right)
+ \frac{\partial (huv)}{\partial y}
= f\,hv
$$
$$
\frac{\partial (hv)}{\partial t}
+ \frac{\partial (huv)}{\partial x}
+ \frac{\partial }{\partial y}\left(hv^2 + \frac{1}{2} g h^2\right)
= -f\,hu
$$
The only forcing term is the Coriolis source. There is no bottom topography: the bottom is flat and `z_b` is not a variable of this `problem`. The test cases are the steady zonal geostrophic flow (Test Case 2) and the steady compact-support jet (Test Case 3) of Williamson et al. (1992), mapped from the sphere to this channel with a `β`-plane Coriolis parameter.

## 2. Definition of variables and coordinates
The coordinate system is 2D Cartesian coordinates, the coordinate names are `x`,`y`, and the unit is `m`. `x` is the along-channel (zonal, periodic) direction and `y` the cross-channel direction; `y` increases toward the north, so `f>0` in the northern-hemisphere configuration of §6.
- `h`: water depth, cell-centered placement, unit `m`
- `hu`: `x`-direction momentum, cell-centered placement, unit `m2/s`
- `hv`: `y`-direction momentum, cell-centered placement, unit `m2/s`
- `f`: Coriolis parameter, one value per interior cell row at the cell centre `y_j`, unit `1/s`, time-invariant, evaluated once at setup from §6

The derived variables are `u=hu/h`, `v=hv/h`, and `c=sqrt(g*h)`. `h<=0` is invalid input.

## 3. Type definition of domain and boundary conditions
The domain is the channel $[0,L_x)\times[0,L_y]$. The grid is a uniform cell-centered finite-volume grid with `nx` × `ny` interior cells, $dx=L_x/nx$, $dy=L_y/ny$, cell centres $x_i=(i-\tfrac12)dx$, $y_j=(j-\tfrac12)dy$ for $i=1..nx$, $j=1..ny$, and one ghost cell on each side (`ng=1`).

The boundary condition is fixed: periodic in `x`, and a free-slip impermeable wall at $y=0$ and $y=L_y$. The wall is realized as a mirror ghost — `h` and `hu` copied, `hv` sign-reversed into the ghost row — by the boundary `component` the adopted `profile` selects (§4); the mirror ghost makes the interface mass flux and `hu` flux through the wall exactly zero and carries the $O(dy)$ wall-pressure error that `component`'s §2 states. The default input used for verification is defined in `tests.md`.

## 4. Dependent `component` and adopted `profile`
The adopted `profile` is `dynamics_shallow_water_profile_2d_channel_p0_rk4`, and it selects the following `component`, each of which is therefore a direct dependency of this `spec`. They are not the whole of that set: the runner harness `deps.yaml` declares is one too, and `<ir_ref>/dependency_graph.json` is the only exhaustive statement of it.
- `dynamics_shallow_water_flux_2d_rusanov_p0`
- `dynamics_shallow_water_boundary_2d_channel_mirror`
- `dynamics_shallow_water_source_2d_coriolis`
- `dynamics_shallow_water_time_update_2d_rk4`

No `component` is declared directly here: a `component` has exactly one source, so a `component` an adopted `profile` selects is not declared again in this `spec`'s own `deps.yaml`. A `component` no adopted `profile` selects would be declared there directly, together with its compatibility constraint.

## 5. Integration algorithm
The spatial discretization is the first-order (`p0`) finite-volume scheme: the in-cell reconstruction is piecewise constant, the interface states supplied to the flux `component` are the adjacent cell values, and no hydrostatic reconstruction is applied, because the bottom is flat. The reconstruction order is never switched at runtime.

The time integration is the four-stage `RK4` of `dynamics_shallow_water_time_update_2d_rk4__advance`, which evaluates the tendency at each stage state itself. This `problem` node supplies the tendency as the procedure argument `rhs` of that operation, and `rhs` is an **internal procedure** of the routine of the generated model that performs the step: it reaches the grid extents, `dx`, `dy`, `g`, the `f` array and the halo work arrays by host association, and it is the only procedure passed as `rhs`. Its dummy arguments match the prototype `dynamics_shallow_water_time_update_2d_rk4_rhs` of that `component`'s §5.1 in type, kind, rank, explicit shape (`U` and `dUdt` are `(ncomp, nx, ny)`) and `intent`; their names are this `problem`'s own. A dependency operation is never passed directly as `rhs`.

The tendency `rhs(ncomp, nx, ny, t, U, dUdt)` computes, for a stage state `U` (interior cells) at the stage time `t`, the following in this order.
1. Copy `U` into the ghost-extended work arrays (`nx+2` × `ny+2`, one per component) and fill their ghost cells with `dynamics_shallow_water_boundary_2d_channel_mirror__apply`, called once per component: `h` with `odd_at_wall=false`, `hu` with `odd_at_wall=false`, `hv` with `odd_at_wall=true`.
2. Compute the interface flux with `dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux`, passing the adjacent cell values as the interface states: at the `x`-interface $(i+\tfrac12,j)$, $U_L=U_{i,j}$ and $U_R=U_{i+1,j}$ for $i=0..nx$; at the `y`-interface $(i,j+\tfrac12)$, $U_B=U_{i,j}$ and $U_T=U_{i,j+1}$ for $j=0..ny$, where an index `0` or `nx+1` / `ny+1` addresses a ghost cell of step 1. The flux `component`'s input/output contract is unchanged: it consumes the interface states the caller supplies.
3. Form the interface-flux difference on the interior cells
$$
L_{flux,i,j}=-\frac{F^{*}_{i+1/2,j}-F^{*}_{i-1/2,j}}{dx}
-\frac{G^{*}_{i,j+1/2}-G^{*}_{i,j-1/2}}{dy}
$$
4. Compute the Coriolis source on the interior cells with `dynamics_shallow_water_source_2d_coriolis__apply` at the stage state `U` and the setup-time `f`, and return
$$
\frac{dU}{dt}=L_{flux}+S,\qquad S=\left[0,\ f_j\,(hv)_{i,j},\ -f_j\,(hu)_{i,j}\right]^T
$$
The stage time `t` is received and not used: no term of §1 depends on time explicitly.

One time step is `dynamics_shallow_water_time_update_2d_rk4__advance(3, nx, ny, U_n, rhs, t, dt, U_np1, guard_pass)`, after which $t \leftarrow t+dt$. The four stage evaluations, their states and times are the time-update `component`'s (its §3); this `problem` does not evaluate a stage itself.

The discretization holds the following invariants.
- **Wet domain.** $h>0$ in every interior cell at every stage and every step of the run. The domain of validity of this `problem spec` is the non-drying regime; drying and wetting are out of scope. A cell reaching $h\le0$ is a runtime error, and the run stops with an error before the value reaches the flux `component`, whose contract treats `h<=0` as an error; it must not be handled by clipping or flooring the state.
- **Mass conservation.** The mirror ghost makes the mass flux through each wall interface exactly zero (the bottom and top states have equal `h` and opposite `hv`), the `x` interfaces are periodic, and the Coriolis source has no `h` component, so $\sum_{i,j}h_{i,j}$ is conserved up to round-off.
- **Zonal uniformity.** An initial state uniform in `x` stays uniform in `x` up to round-off: every `x`-interface flux difference vanishes identically and every remaining operation acts row-wise.
- **Translation equivariance in `x`.** `f` depends on `y` alone and the `x` boundary is periodic, so shifting the initial state by a whole number of cells in `x` shifts the solution by the same number of cells, up to round-off.
- **No work by the Coriolis source.** $(hu)\,S_2+(hv)\,S_3=0$ at every cell (the source `component`'s §3).

The stability index is defined as
$$
\mathrm{cfl}=dt\cdot\max_{i,j}\left(\frac{|u_{i,j}|+c_{i,j}}{dx}+\frac{|v_{i,j}|+c_{i,j}}{dy}\right)
$$
For the threshold, refer to the judgment conditions of `tests.md`.

## 6. Model parameters and the runtime input contract
The physical constants are those of Williamson et al. (1992), in SI units:
- `g = 9.80616 m/s2` (gravitational acceleration)
- `Omega = 7.292e-5 1/s` (rotation rate)
- `a = 6.37122e6 m` (radius)
- `gh0 = 2.94e4 m2/s2`, so `h0 = gh0/g` (reference depth)
- `u0 = 2*pi*a/(12*86400 s)` (reference velocity; one revolution in 12 days)
- `theta0 = pi/4` (reference latitude of the `β`-plane)

`h0`, `u0`, and the `f_0`, `β` below are derived from these constants by the stated expressions wherever they are used; none of them is an independent input, and no rounded literal of a derived value stands in for the expression.

The Coriolis profile is specified by `coriolis_profile`, and only the value `beta_plane` is allowed:
$$
f(y)=f_0+\beta\,\left(y-\tfrac{L_y}{2}\right),\qquad
f_0=2\,\Omega\sin\theta_0,\qquad
\beta=\frac{2\,\Omega\cos\theta_0}{a}
$$
evaluated once at setup at the cell centres $y_j$.

The initial condition is specified by `flow_profile`, and only the 3 values `tc2_zonal_uniform`, `tc3_compact_jet` and `tc2_zonal_perturbed` are allowed. The first two are steady, `x`-uniform, geostrophically balanced states with $v=0$ and $g\,\partial_y h=-f\,u$, discretized at the cell centres $y_j$ with $hu=h\,u$, $hv=0$; the third is the first with an `x`-dependent depth perturbation, and is not steady.
- `tc2_zonal_uniform` (Test Case 2): $u(y)=u_0$ and
$$
h(y)=h_0-\frac{u_0}{g}\left[f_0\left(y-\tfrac{L_y}{2}\right)+\frac{\beta}{2}\left(y-\tfrac{L_y}{2}\right)^2\right]
$$
- `tc3_compact_jet` (Test Case 3): with $y_b=L_y/8$, $y_e=7L_y/8$, $x_e=0.3$, $s(y)=x_e\,(y-y_b)/(y_e-y_b)$ and $b(s)=0$ for $s\le0$, $b(s)=e^{-1/s}$ for $s>0$,
$$
u(y)=u_0\,b(s)\,b(x_e-s)\,e^{4/x_e},\qquad
h(y)=h_0-\frac{1}{g}\int_0^{y}f(y')\,u(y')\,dy'
$$
- `tc2_zonal_perturbed`: with $h_{TC2}(y)$ the `tc2_zonal_uniform` depth above and the parameters `eta0` (`m`, $\eta_0>0$) and `shift_x_fraction` (dimensionless, in $[0,1)$),
$$
h(x,y)=h_{TC2}(y)+\eta_0\,\sin\left(2\pi\left(\frac{x}{L_x}-\mathrm{shift\_x\_fraction}\right)\right),\qquad u=u_0,\qquad v=0
$$
discretized at the cell centres $(x_i, y_j)$ with $hu=h\,u_0$, $hv=0$. The perturbation is a wavenumber-one gravity-wave excitation of the channel: it is the only initial condition of this `problem` that is not uniform in `x`, so it is the one that exercises the `x`-interface flux and the periodic `x` mapping.

The integral is evaluated by composite Simpson quadrature on a uniform sub-grid of spacing $dy/64$ from $y'=0$ to the cell centre $y_j$, which spans $64j-32$ sub-intervals (an even count for every $j$); this quadrature is part of the definition of the discrete initial state and is not an implementation choice. $u$ has compact support in $(y_b, y_e)$ and its maximum is $u_0$ at $s=x_e/2$. Because $y_b+y_e=L_y$ and $u$ is symmetric about $L_y/2$, the $\beta$ term of $f$ integrates to zero over the support, and the depth north of the jet is the constant
$$
h_N=h_0-\frac{f_0\,u_0\,e^{4/x_e}}{g}\,\frac{y_e-y_b}{x_e}\,C,\qquad C=\int_0^{x_e}e^{-1/s-1/(x_e-s)}\,ds=1.12064479227927\times10^{-7}
$$
which the discrete initial state reproduces at every row north of $y_e$ to round-off: the integrand is smooth with compact support inside $[0,y_j]$ for those rows, so the composite rule's error on the complete integral is below round-off. Inside the jet the rule's error against the exact partial integral is below $2\times10^{-12}$ relative at every `ny` of `tests.md`. $C$ has no closed form; its value is defined by the digits given. The branch-free form $b(s)=\exp\left(-1/\max(s,\varepsilon)\right)$ with $\varepsilon=10^{-300}$ is equal to $b(s)$ at every cell centre in double precision (for $s\le0$ the exponent is $-10^{300}$ and the exponential underflows to exactly $0$; no cell centre has $s=0$, because $y_b$ and $y_e$ lie on cell edges when `ny` is a multiple of 8, and the sweep of `tests.md` uses such `ny`), and either form may be used.

The runtime input requires the following.
- `L_x`, `L_y`, `nx`, `ny`
- `coriolis_profile`
- `flow_profile`, and for `tc2_zonal_perturbed` its parameters `eta0`, `shift_x_fraction`
- `t_start`, `t_end`
- `dt_rule`
- `output_schedule`

In the initial state, `h>0` in all cells is required. For `tc2_zonal_uniform` this bounds the channel width: $h$ is smallest at the northern wall, $h(L_y)=h_0-\dfrac{u_0}{g}\left[\dfrac{f_0 L_y}{2}+\dfrac{\beta L_y^2}{8}\right]$, which is positive for $L_y<1.046\times10^7\ \mathrm{m}$ with the constants above ($h(L_y)=1493\ \mathrm{m}$ at $L_y=6\times10^6\ \mathrm{m}$); for `tc2_zonal_perturbed` the bound is $h(L_y)>\eta_0$. A channel outside the bound is an error at setup, not a value to clip. An undefined parameter is an error without implicit completion.

## 7. Prohibitions
Forbid a boundary treatment other than the channel of §3 — a periodic `y` boundary, a wall in `x`, or a wall realized by anything but the mirror ghost — the introduction of a bottom topography, the introduction of a forcing term other than the Coriolis source, a Coriolis profile or a flow profile other than the allowed values, automatic switching of `flow_profile` or `coriolis_profile`, and the runtime automatic switching of the discretization scheme. Forbid `clip` / `limiter` / `filter` on `h`. Forbid passing anything other than the internal procedure of §5 as the `rhs` argument of the time-update `component` — a dependency operation passed directly is refused by the generated model's dataflow gate and is not the tendency of §5.

## 8. Traceability
The resolution result is recorded by the host: `<ir_ref>/ir_meta.json` carries the node's own `spec_kind` / `spec_id` / `spec_version`, and `<ir_ref>/dependency_graph.json` carries the resolved closure — `all_nodes` as `<spec_kind>/<spec_id>@<version>` for every node of it (this `spec` itself, the `component`, and the runner harness alike), and `profiles[]` as the adopted `profile`, whose entry carries its own `node_key` for identity but is deliberately absent from `all_nodes`, because a `profile` is not a node.

The reference basis is Williamson et al. (1992, JCP 102, 211–224, DOI:10.1016/S0021-9991(05)80016-6) — Test Cases 2 and 3, equations (95) and (101)–(103), and the balance condition (114) reduced to the channel — LeVeque (2002), and Toro (2009).

## 9. tests reference
The corresponding `tests.md` is `spec/problem/dynamics/shallow_water/shallow_water2d_channel/tests.md`, with `test_profile_version` of `0.1.0`.

## 10. AD preparation information
`ad_readiness.enabled` is `true`. The state update is expressed in the form $U_{next}=F(U_{now}, params)$, and `max`, `abs`, `ceil`, `exp` (in $b(s)$, with its branch at $s=0$), the periodic-index wrap and the sign reversal at the wall are made explicit as non-differentiable operations.
