# Controlled Spec: 2D shallow water channel problem (problem spec)

## 0. Meta information
- `spec_id`: `shallow_water2d_channel`
- `spec_version`: `0.2.0`
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
= Q_h
$$
$$
\frac{\partial (hu)}{\partial t}
+ \frac{\partial }{\partial x}\left(hu^2 + \frac{1}{2} g h^2\right)
+ \frac{\partial (huv)}{\partial y}
= f\,hv + Q_{hu}
$$
$$
\frac{\partial (hv)}{\partial t}
+ \frac{\partial (huv)}{\partial x}
+ \frac{\partial }{\partial y}\left(hv^2 + \frac{1}{2} g h^2\right)
= -f\,hu + Q_{hv}
$$
The forcing terms are the Coriolis source and the external forcing $Q=[Q_h,\ Q_{hu},\ Q_{hv}]^T$. $Q$ is the manufactured forcing of the forced translating low (§6) for `flow_profile=tc4_translating_low` and is zero for every other `flow_profile`. There is no bottom topography: the bottom is flat and `z_b` is not a variable of this `problem`. The test cases are the steady zonal geostrophic flow (Test Case 2), the steady compact-support jet (Test Case 3) and the forced translating low (Test Case 4) of Williamson et al. (1992), mapped from the sphere to this channel with a `β`-plane Coriolis parameter. The prescribed flow of Test Case 4 is an exact solution of the forced equations, so each of the three test cases of Williamson et al. has an analytic reference on the channel.

## 2. Definition of variables and coordinates
The coordinate system is 2D Cartesian coordinates, the coordinate names are `x`,`y`, and the unit is `m`. `x` is the along-channel (zonal, periodic) direction and `y` the cross-channel direction; `y` increases toward the north, so `f>0` in the northern-hemisphere configuration of §6.
- `h`: water depth, cell-centered placement, unit `m`
- `hu`: `x`-direction momentum, cell-centered placement, unit `m2/s`
- `hv`: `y`-direction momentum, cell-centered placement, unit `m2/s`
- `f`: Coriolis parameter, one value per interior cell row at the cell centre `y_j`, unit `1/s`, time-invariant, evaluated once at setup from §6
- `Q`: external forcing, cell-centered placement on the interior cells, a function of $(x_i, y_j, t)$ that does not read the state, unit `m/s` for $Q_h$ and `m2/s2` for $Q_{hu}$, $Q_{hv}$; zero unless `flow_profile=tc4_translating_low`

The derived variables are `u=hu/h`, `v=hv/h`, and `c=sqrt(g*h)`. `h<=0` is invalid input.

## 3. Type definition of domain and boundary conditions
The domain is the channel $[0,L_x)\times[0,L_y]$. The grid is a uniform cell-centered finite-volume grid with `nx` × `ny` interior cells, $dx=L_x/nx$, $dy=L_y/ny$, cell centres $x_i=(i-\tfrac12)dx$, $y_j=(j-\tfrac12)dy$ for $i=1..nx$, $j=1..ny$, and two ghost cells on each side (`ng=2`).

The boundary condition is fixed: periodic in `x`, and a free-slip impermeable wall at $y=0$ and $y=L_y$. The wall is realized as a mirror ghost — `h` and `hu` copied, `hv` sign-reversed into the two ghost rows — by the boundary `component` the adopted `profile` selects (§4). The reconstruction `component`'s §2 states the property of its interface states on a mirror-ghost field: at a wall interface the bottom and top states of `h` and `hu` are equal and those of `hv` opposite, so the interface mass flux and `hu` flux through the wall are exactly zero, and the wall-face `h` equals the cell-centre `h` of the wall-adjacent row, so the $O(dy)$ wall-pressure property that the boundary `component`'s §2 states holds unchanged. The prescribed flow of `tc4_translating_low` has zero background velocity at both walls and a low whose streamfunction at a wall satisfies $|\psi|\le|\psi_0|\,e^{-(L_y/2)^2/R_{low}^2}$ ($1.1\times10^{-7}\,|\psi_0|$ at $L_y=8R_{low}$, the channel of `tests.md`; §6), so the mirror ghost departs from that flow at the walls by that relative amount in `h` and in the wall-normal velocity, and by about $10^{-6}$ of its maximum in the along-wall velocity of the low (the forcing `component`'s §2); this departure is part of the discretization and is not corrected. The default input used for verification is defined in `tests.md`.

## 4. Dependent `component` and adopted `profile`
The adopted `profile` is `dynamics_shallow_water_profile_2d_channel_p1_rk4`, and it selects the following `component`, each of which is therefore a direct dependency of this `spec`. They are not the whole of that set: the runner harness `deps.yaml` declares is one too, and `<ir_ref>/dependency_graph.json` is the only exhaustive statement of it.
- `dynamics_shallow_water_reconstruction_2d_muscl_mc`
- `dynamics_shallow_water_flux_2d_rusanov`
- `dynamics_shallow_water_boundary_2d_channel_mirror`
- `dynamics_shallow_water_source_2d_coriolis`
- `dynamics_shallow_water_time_update_2d_rk4`

One `component` is declared directly in this `spec`'s own `deps.yaml`, with the compatibility constraint `>=0.1.0 <1.0.0`, because no adopted `profile` selects it: `dynamics_shallow_water_source_2d_tc4_forcing`, which evaluates the forcing $Q$ of `tc4_translating_low`. A `component` has exactly one source, so a `component` the adopted `profile` selects is not declared again there.

## 5. Integration algorithm
The spatial discretization is the second-order `p1` finite-volume scheme: the in-cell reconstruction of each conserved variable (`h`, `hu`, `hv`) is piecewise linear (`MUSCL`) with the `MC` limiter, by the reconstruction `component`, and the interface states it returns are supplied to the flux `component`. No hydrostatic reconstruction is applied, because the bottom is flat. The reconstruction order and the limiter are never switched at runtime.

The time integration is the four-stage `RK4` of `dynamics_shallow_water_time_update_2d_rk4__advance`, which evaluates the tendency at each stage state and stage time itself. This `problem` node supplies the tendency as the procedure argument `rhs` of that operation, and `rhs` is an **internal procedure** of the routine of the generated model that performs the step: it reaches the grid extents, `dx`, `dy`, `g`, the setup-time row arrays, the parameters of the low and the work arrays by host association, and it is the only procedure passed as `rhs`. Its dummy arguments match the prototype `dynamics_shallow_water_time_update_2d_rk4_rhs` of that `component`'s §5.1 in type, kind, rank, explicit shape (`U` and `dUdt` are `(ncomp, nx, ny)`) and `intent`; their names are this `problem`'s own. A dependency operation is never passed directly as `rhs`.

The tendency `rhs(ncomp, nx, ny, t, U, dUdt)` computes, for a stage state `U` (interior cells) at the stage time `t`, the following in this order.
1. Copy `U` into the ghost-extended work arrays (`nx+4` × `ny+4`, one per component) and fill their ghost cells with `dynamics_shallow_water_boundary_2d_channel_mirror__apply` with `ng=2`, called once per component: `h` with `odd_at_wall=false`, `hu` with `odd_at_wall=false`, `hv` with `odd_at_wall=true`.
2. Reconstruct the interface states with `dynamics_shallow_water_reconstruction_2d_muscl_mc__reconstruct` with `ng=2`, called once per component on the ghost-filled work array of step 1: it returns, for that component, the `x`-interface states `U_L`, `U_R` (`nx+1` × `ny`) and the `y`-interface states `U_B`, `U_T` (`nx` × `ny+1`), numbered as in that `component`'s §2 — `x` interface $m$ is the west face of interior cell $m$ ($m=1..nx$) or the east face of cell $nx$ ($m=nx+1$), and `y` interface $n$ likewise in `y`.
3. Compute the interface flux with `dynamics_shallow_water_flux_2d_rusanov__compute_flux`, one call per interface, on the three-component states assembled from step 2. At `x` interface $(m,j)$ its `U_L` and `U_R` arguments are the reconstructed left and right states of $(h,hu,hv)$, its `U_B` and `U_T` arguments are the same two states, and its `F_star` is $F^{*}_{m,j}$; at `y` interface $(i,n)$ its `U_B` and `U_T` arguments are the reconstructed bottom and top states, its `U_L` and `U_R` arguments are the same two states, and its `G_star` is $G^{*}_{i,n}$. The output of each call not named here is not used. The flux `component`'s input/output contract is unchanged: it consumes the interface states the caller supplies.
4. Form the interface-flux difference on the interior cells
$$
L_{flux,i,j}=-\frac{F^{*}_{i+1,j}-F^{*}_{i,j}}{dx}
-\frac{G^{*}_{i,j+1}-G^{*}_{i,j}}{dy}
$$
with the interface numbering of step 2.
5. Compute the Coriolis source $S$ on the interior cells with `dynamics_shallow_water_source_2d_coriolis__apply` at the stage state `U` and the setup-time `f`.
6. For `flow_profile=tc4_translating_low`, compute the forcing $Q$ on the interior cells with `dynamics_shallow_water_source_2d_tc4_forcing__apply` at the stage time `t` received by `rhs`, with the setup-time cell-centre coordinates and row arrays and the parameters of the low of §6. For every other `flow_profile`, $Q=0$ and the forcing `component` is not called.
7. Return
$$
\frac{dU}{dt}=L_{flux}+S+Q,\qquad S=\left[0,\ f_j\,(hv)_{i,j},\ -f_j\,(hu)_{i,j}\right]^T
$$

The stage time `t` is the time at which the forcing is evaluated; it enters no other term.

One time step is `dynamics_shallow_water_time_update_2d_rk4__advance(3, nx, ny, U_n, rhs, t, dt, U_np1, guard_pass)`, with `t` $=t_{start}+n\,dt$ at step $n=0..\mathrm{n\_step}-1$ (the time at the start of the step; `tests.md` §3), evaluated from $n$ and not accumulated. The four stage evaluations, their states and times are the time-update `component`'s (its §3); this `problem` does not evaluate a stage itself.

The discretization holds the following invariants.
- **Wet domain.** $h>0$ in every interior cell at every stage and every step of the run. The domain of validity of this `problem spec` is the non-drying regime; drying and wetting are out of scope. A cell reaching $h\le0$ is a runtime error, and the run stops with an error before the value reaches the flux `component`, whose contract treats `h<=0` as an error; it must not be handled by clipping or flooring the state.
- **Mass conservation.** The mass flux through each wall interface is exactly zero (the reconstructed bottom and top states have equal `h` and opposite `hv`: §3), the `x` interfaces are periodic, and the Coriolis source has no `h` component, so for every `flow_profile` but `tc4_translating_low` $\sum_{i,j}h_{i,j}$ is conserved up to round-off. For `tc4_translating_low` the change of $\sum_{i,j}h_{i,j}$ over a step is $dt\sum_{i,j}Q_{h,i,j}$ up to round-off; $\int Q_h\,dx$ over one period is zero at every `y` and `t`, and the discrete $\sum_{i,j}Q_{h,i,j}$ is below $10^{-15}\sum_{i,j}|Q_{h,i,j}|$ on every grid of `tests.md`.
- **Zonal uniformity.** For `tc2_zonal_uniform` and `tc3_compact_jet`, an initial state uniform in `x` stays uniform in `x` up to round-off: every `x`-interface flux difference vanishes identically and every remaining operation acts row-wise. It does not apply to `tc4_translating_low`, whose state and forcing depend on `x`.
- **Translation equivariance in `x`.** For every `flow_profile` but `tc4_translating_low`, `f` depends on `y` alone, $Q=0$ and the `x` boundary is periodic, so shifting the initial state by a whole number of cells in `x` shifts the solution by the same number of cells, up to round-off. It does not apply to `tc4_translating_low`, whose forcing is not shifted with the state.
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
- `gh0` (unit `m2/s2`), so `h0 = gh0/g` (reference depth): `gh0 = 2.94e4` for `tc2_zonal_uniform`, `tc3_compact_jet` and `tc2_zonal_perturbed`, and `gh0 = 1.0e5` for `tc4_translating_low`
- `u0 = 2*pi*a/(12*86400 s)` (reference velocity of `tc2_zonal_uniform`, `tc3_compact_jet` and `tc2_zonal_perturbed`; one revolution in 12 days)
- `theta0 = pi/4` (reference latitude of the `β`-plane)

`gh0` is a case input, with the value above for the case's `flow_profile`. `h0`, `u0`, and the `f_0`, `β` below are derived from these constants by the stated expressions wherever they are used; none of them is an independent input, and no rounded literal of a derived value stands in for the expression.

The Coriolis profile is specified by `coriolis_profile`, and only the value `beta_plane` is allowed:
$$
f(y)=f_0+\beta\,\left(y-\tfrac{L_y}{2}\right),\qquad
f_0=2\,\Omega\sin\theta_0,\qquad
\beta=\frac{2\,\Omega\cos\theta_0}{a}
$$
evaluated once at setup at the cell centres $y_j$.

The initial condition is specified by `flow_profile`, and only the 4 values `tc2_zonal_uniform`, `tc3_compact_jet`, `tc2_zonal_perturbed` and `tc4_translating_low` are allowed. The first two are steady, `x`-uniform, geostrophically balanced states with $v=0$ and $g\,\partial_y h=-f\,u$, discretized at the cell centres $y_j$ with $hu=h\,u$, $hv=0$; the third is the first with an `x`-dependent depth perturbation, and is not steady.
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
discretized at the cell centres $(x_i, y_j)$ with $hu=h\,u_0$, $hv=0$. The perturbation is a wavenumber-one gravity-wave excitation of the channel: it is the unforced initial condition of this `problem` that is not uniform in `x`, so it is the one that exercises the `x`-interface flux and the periodic `x` mapping without the forcing.

- `tc4_translating_low` (Test Case 4): the prescribed flow is a geostrophic background jet plus a Gaussian low that translates east. With the parameter `u_jet` (`m/s`, $u_{jet}>0$; the jet maximum, the $u_0$ of Test Case 4), $k=\pi/L_y$ and $h_0=gh_0/g$ with the `gh0` of this profile, the background is
$$
\bar u(y)=u_{jet}\sin^2(ky),\qquad
\bar h(y)=h_0-\frac{u_{jet}}{g}\left[f_0\left(\frac y2-\frac{\sin 2ky}{4k}\right)+\beta\left(\frac{y^2}{4}-\frac{L_y\,y}{4}-\frac{(y-L_y/2)\sin 2ky}{4k}-\frac{\cos 2ky-1}{8k^2}\right)\right]
$$
which is the closed form of $h_0-\frac1g\int_0^y f\,\bar u\,dy'$, so $g\,\partial_y\bar h=-f\,\bar u$ and $\bar h(0)=h_0$. The low has the streamfunction
$$
\psi(x,y,t)=\psi_0\,e^{-r^2/R_{low}^2},\qquad
r^2=\left(\frac{L_x}{\pi}\right)^2\sin^2\xi+(y-y_c)^2,\qquad
\xi=\frac{\pi\,(x-x_{c0}-c_{tr}\,t)}{L_x}
$$
with $\psi_0=-0.03\,gh_0/f_0$, $R_{low}=2a/12.74244$ (so $R_{low}=1.0\times10^6\ \mathrm{m}$ to the digits given), $c_{tr}=u_{jet}\cos\theta_0$, $x_{c0}=L_x/2$ and $y_c=L_y/2$, and
$$
\psi_x=-\frac{L_x}{\pi R_{low}^2}\sin 2\xi\;\psi,\qquad
\psi_y=-\frac{2\,(y-y_c)}{R_{low}^2}\;\psi
$$
The prescribed flow is
$$
\tilde u=\bar u-\psi_y,\qquad \tilde v=\psi_x,\qquad \tilde h=\bar h+\frac{f\,\psi}{g}
$$
and it is an exact solution of the equations of §1 with the forcing $Q$ that `dynamics_shallow_water_source_2d_tc4_forcing` §3 defines, with that `component`'s `S` as $Q$ and its arguments `f`, `dfdy`, `ubar`, `dubar`, `hbar` the row values of $f(y_j)$, $\beta$, $\bar u(y_j)$, $\bar u'(y_j)=u_{jet}\,k\sin 2ky_j$ and $\bar h(y_j)$, and `x`, `y`, `L_x`, `psi0`, `R_low`, `c_tr`, `x_c0`, `y_c` the values above. The derivatives `dfdy` and `dubar` are the closed forms stated here and not differences of the row arrays. The five row arrays, the cell-centre coordinates and the parameters of the low are evaluated once at setup. The initial state is the prescribed flow at $t=0$ at the cell centres $(x_i, y_j)$: $h=\tilde h$, $hu=\tilde h\,\tilde u$, $hv=\tilde h\,\tilde v$. The reference solution at time $t$ is the prescribed flow at $t$; the centre of the low moves a distance $c_{tr}\,t$ east, so $\tilde h(x,y,t)-\tilde h(x,y,0)=f\,(\psi(x,y,t)-\psi(x,y,0))/g$.

The integral of `tc3_compact_jet` is evaluated by composite Simpson quadrature on a uniform sub-grid of spacing $dy/64$ from $y'=0$ to the cell centre $y_j$, which spans $64j-32$ sub-intervals (an even count for every $j$); this quadrature is part of the definition of the discrete initial state and is not an implementation choice. $u$ has compact support in $(y_b, y_e)$ and its maximum is $u_0$ at $s=x_e/2$. Because $y_b+y_e=L_y$ and $u$ is symmetric about $L_y/2$, the $\beta$ term of $f$ integrates to zero over the support, and the depth north of the jet is the constant
$$
h_N=h_0-\frac{f_0\,u_0\,e^{4/x_e}}{g}\,\frac{y_e-y_b}{x_e}\,C,\qquad C=\int_0^{x_e}e^{-1/s-1/(x_e-s)}\,ds=1.12064479227927\times10^{-7}
$$
which the discrete initial state reproduces at every row north of $y_e$ to round-off: the integrand is smooth with compact support inside $[0,y_j]$ for those rows, so the composite rule's error on the complete integral is below round-off. Inside the jet the rule's error against the exact partial integral is below $2\times10^{-12}$ relative at every `ny` of `tests.md`. $C$ has no closed form; its value is defined by the digits given. The branch-free form $b(s)=\exp\left(-1/\max(s,\varepsilon)\right)$ with $\varepsilon=10^{-300}$ is equal to $b(s)$ at every cell centre in double precision (for $s\le0$ the exponent is $-10^{300}$ and the exponential underflows to exactly $0$; no cell centre has $s=0$, because $y_b$ and $y_e$ lie on cell edges when `ny` is a multiple of 8, and the sweep of `tests.md` uses such `ny`), and either form may be used.

The runtime input requires the following.
- `L_x`, `L_y`, `nx`, `ny`
- `coriolis_profile`
- `flow_profile`, and for `tc2_zonal_perturbed` its parameters `eta0`, `shift_x_fraction`, and for `tc4_translating_low` its parameter `u_jet`
- `gh0` (the value of the case's `flow_profile`, above)
- `t_start`, `t_end`
- `dt_rule`
- `output_schedule`

In the initial state, `h>0` in all cells is required. For `tc2_zonal_uniform` this bounds the channel width: $h$ is smallest at the northern wall, $h(L_y)=h_0-\dfrac{u_0}{g}\left[\dfrac{f_0 L_y}{2}+\dfrac{\beta L_y^2}{8}\right]$, which is positive for $L_y<1.046\times10^7\ \mathrm{m}$ with the constants above ($h(L_y)=1493\ \mathrm{m}$ at $L_y=6\times10^6\ \mathrm{m}$); for `tc2_zonal_perturbed` the bound is $h(L_y)>\eta_0$. For `tc4_translating_low`, $\bar h$ is smallest at the northern wall, $\bar h(L_y)=h_0-u_{jet}f_0L_y/(2g)$ ($8515\ \mathrm{m}$ at $u_{jet}=40\ \mathrm{m/s}$, $L_y=8\times10^6\ \mathrm{m}$), and the low lowers the depth at its centre by $f(y_c)\,|\psi_0|/g=0.03\,h_0$ ($306\ \mathrm{m}$ at $gh_0=1.0\times10^5$). A channel outside the bound is an error at setup, not a value to clip. An undefined parameter is an error without implicit completion.

## 7. Prohibitions
Forbid a boundary treatment other than the channel of §3 — a periodic `y` boundary, a wall in `x`, or a wall realized by anything but the mirror ghost — the introduction of a bottom topography, the introduction of a forcing term other than the Coriolis source and the forcing $Q$ of `tc4_translating_low`, a non-zero $Q$ for any other `flow_profile`, the evaluation of $Q$ at a time other than the stage time `t` received by `rhs` or from the state, a background or a low other than those of §6, a reconstruction other than the `p1` `MC` reconstruction of §5 or its runtime switching, a Coriolis profile or a flow profile other than the allowed values, automatic switching of `flow_profile` or `coriolis_profile`, and the runtime automatic switching of the discretization scheme. Forbid `clip` / `limiter` / `filter` on the state `h`; the `MC` limiter of the reconstruction `component` acts on the slopes that build the interface states (§5 step 2), not on the state, and is part of the discretization. Forbid passing anything other than the internal procedure of §5 as the `rhs` argument of the time-update `component` — a dependency operation passed directly is refused by the generated model's dataflow gate and is not the tendency of §5.

## 8. Traceability
The resolution result is recorded by the host: `<ir_ref>/ir_meta.json` carries the node's own `spec_kind` / `spec_id` / `spec_version`, and `<ir_ref>/dependency_graph.json` carries the resolved closure — `all_nodes` as `<spec_kind>/<spec_id>@<version>` for every node of it (this `spec` itself, the `component`, and the runner harness alike), and `profiles[]` as the adopted `profile`, whose entry carries its own `node_key` for identity but is deliberately absent from `all_nodes`, because a `profile` is not a node.

The reference basis is Williamson et al. (1992, JCP 102, 211–224, DOI:10.1016/S0021-9991(05)80016-6) — Test Cases 2 and 3, equations (95) and (101)–(103), and the balance condition (114) reduced to the channel; Test Case 4, equations (116)–(130), mapped to the channel by replacing $\tan^2(d/2a)$ with $r^2/R_{low}^2$, the jet $\sin^{14}(2\theta)$ with $\sin^2(\pi y/L_y)$, and the translation rate $u_0/a$ of the centre's longitude with the speed $c_{tr}=u_{jet}\cos\theta_0$ of its position — LeVeque (2002), Toro (2009), and van Leer (1977) for the `MC` limiter.

## 9. tests reference
The corresponding `tests.md` is `spec/problem/dynamics/shallow_water/shallow_water2d_channel/tests.md`, with `test_profile_version` of `0.2.0`.

## 10. AD preparation information
`ad_readiness.enabled` is `true`. The state update is expressed in the form $U_{next}=F(U_{now}, params)$, and `max`, `abs`, `ceil`, `exp` (in $b(s)$, with its branch at $s=0$), the periodic-index wrap, the sign reversal at the wall, and the limiter of the reconstruction `component` (the branch on the sign of the product of the two one-sided differences, the `min` over the three slope candidates, and `sign`; that `component`'s §9) are made explicit as non-differentiable operations. `sin`, `cos` and `exp` in the prescribed flow of `tc4_translating_low` and its forcing are differentiable.
