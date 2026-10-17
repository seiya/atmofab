# Controlled Spec: 2D shallow water channel problem (problem spec)

## 0. Meta information
- `spec_id`: `shallow_water2d_channel`
- `spec_version`: `0.2.5`
- `status`: `controlled_draft`
- `spec_kind`: `problem`
- `domain`: `dynamics`
- `family`: `shallow_water`

## 1. Problem definition
The target is the conservative form of the 2D shallow water equation on a flat bottom with the Coriolis force, on a channel that is periodic along one coordinate axis and bounded by free-slip walls across it. The along-channel axis is the input `channel_axis` (§6), `x` or `y`:
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
The forcing terms are the Coriolis source and the external forcing $Q=[Q_h,\ Q_{hu},\ Q_{hv}]^T$. $Q$ is the manufactured forcing of the forced translating low (§6) for `flow_profile=tc4_translating_low` and is zero for every other `flow_profile`. There is no bottom topography: the bottom is flat and `z_b` is not a variable of this `problem`. The test cases are the steady zonal geostrophic flow (Test Case 2), the steady compact-support jet (Test Case 3) and the forced translating low (Test Case 4) of Williamson et al. (1992), mapped from the sphere to this channel with a `β`-plane Coriolis parameter. The prescribed flow of Test Case 4 is an exact solution of the forced equations, so each of the three test cases of Williamson et al. has an exact reference on the channel: a closed form for Test Cases 2 and 4, and for Test Case 3 the steady state whose depth is the integral of §6, evaluated by its composite Simpson quadrature.

The equations of §1 are invariant under the exchange of the two axes together with $f\to-f$ (the exchange reverses the sense of rotation of the plane), under the reflection $x\to L_x-x$ with $u\to-u$ and $f\to-f$, and under the reflection $y\to L_y-y$ with $v\to-v$ and $f\to-f$. The inputs of §6 — the orientation `channel_axis`, the signed rotation rate `Omega`, the signed reference latitude `theta0` and the direction `zonal_sign` of the flow along the channel — make the transformed configuration of each a case of this `problem`, and §5 states the equivariance of the discretization under each ([issue #514](https://github.com/seiya/atmofab/issues/514)).

## 2. Definition of variables and coordinates
The coordinate system is 2D Cartesian coordinates, the coordinate names are `x`,`y`, and the unit is `m`. The along-channel (periodic) direction is the axis `channel_axis` names, and the other axis is the cross-channel direction. The along-channel coordinate of a point is written $s$ and its cross-channel coordinate $r$: $(s, r)=(x, y)$ for `channel_axis=x` and $(s, r)=(y, x)$ for `channel_axis=y`. $L_s$ and $L_r$ are the domain lengths along and across the channel ($L_s=L_x$, $L_r=L_y$ for `channel_axis=x`; $L_s=L_y$, $L_r=L_x$ for `channel_axis=y`), and $u_s$ and $u_r$ are the velocity components along and across the channel ($u_s=u$, $u_r=v$ for `channel_axis=x`; $u_s=v$, $u_r=u$ for `channel_axis=y`). The sign convention of `f` is that of a right-handed `(x, y)` plane seen from above: `f>0` is a counter-clockwise rotation of the frame (`dynamics_shallow_water_source_2d_coriolis` §2), and the sign of `f` follows from the signed inputs `Omega` and `theta0` (§6).
- `h`: water depth, cell-centered placement, unit `m`
- `hu`: `x`-direction momentum, cell-centered placement, unit `m2/s`
- `hv`: `y`-direction momentum, cell-centered placement, unit `m2/s`
- `f`: Coriolis parameter, one value per interior cell at the cell centre, a function of the cross-channel coordinate $r$ alone, unit `1/s`, time-invariant, evaluated once at setup from §6
- `Q`: external forcing, cell-centered placement on the interior cells, a function of $(x_i, y_j, t)$ that does not read the state, unit `m/s` for $Q_h$ and `m2/s2` for $Q_{hu}$, $Q_{hv}$; zero unless `flow_profile=tc4_translating_low`

The derived variables are `u=hu/h`, `v=hv/h`, and `c=sqrt(g*h)`. `h<=0` is invalid input.

## 3. Type definition of domain and boundary conditions
The domain is the channel $[0,L_x]\times[0,L_y]$, periodic along the channel. The grid is a uniform cell-centered finite-volume grid with `nx` × `ny` interior cells, $dx=L_x/nx$, $dy=L_y/ny$, cell centres $x_i=(i-\tfrac12)dx$, $y_j=(j-\tfrac12)dy$ for $i=1..nx$, $j=1..ny$, and two ghost cells on each side (`ng=2`). The arrays are indexed $(i, j)$ in either orientation: the first array axis is `x` and the second is `y`.

The boundary condition is fixed by `channel_axis`: periodic along the channel, and a free-slip impermeable wall at $r=0$ and $r=L_r$. For `channel_axis=x` the channel is periodic in `x` with walls at $y=0$ and $y=L_y$; for `channel_axis=y` it is periodic in `y` with walls at $x=0$ and $x=L_x$. The wall is realized as a mirror ghost — `h` and the along-channel momentum copied, the cross-channel momentum sign-reversed into the two ghost lines beyond the wall (`hv` into the ghost rows for `channel_axis=x`, `hu` into the ghost columns for `channel_axis=y`) — by the boundary `component` the adopted `profile` selects (§4). The reconstruction `component`'s §2 states the property of its interface states on a mirror-ghost field: at a wall interface the two states of `h` and of the along-wall momentum are equal and those of the wall-normal momentum opposite, so the interface mass flux and the along-wall momentum flux through the wall are exactly zero, and the wall-face `h` equals the cell-centre `h` of the wall-adjacent line, so the $O(\Delta r)$ wall-pressure property that the boundary `component`'s §2 states holds unchanged. The prescribed flow of `tc4_translating_low` has zero background velocity at both walls and a low whose streamfunction at a wall satisfies $|\psi|\le|\psi_0|\,e^{-(L_r/2)^2/R_{low}^2}$ ($1.1\times10^{-7}\,|\psi_0|$ at $L_r=8R_{low}$, the channel of `tests.md`; §6), so the mirror ghost departs from that flow at the walls by that relative amount in `h` and in the wall-normal velocity, and by about $10^{-6}$ of its maximum in the along-wall velocity of the low (the forcing `component`'s §2); this departure is part of the discretization and is not corrected. The default input used for verification is defined in `tests.md`.

## 4. Dependent `component` and adopted `profile`
The adopted `profile` is `dynamics_shallow_water_profile_2d_channel_p1_rk4`, and it selects the following `component`, each of which is therefore a direct dependency of this `spec`. `<ir_ref>/dependency_graph.json` is the only exhaustive statement of that set. The runner harness is not in it: it belongs to the implementation target the node is built for, and the host adds it to every build of the node.
- `dynamics_shallow_water_reconstruction_2d_muscl_mc`
- `dynamics_shallow_water_flux_2d_rusanov`
- `dynamics_shallow_water_boundary_2d_channel_mirror`
- `dynamics_shallow_water_source_2d_coriolis`
- `dynamics_shallow_water_time_update_2d_rk4`

One `component` is declared directly in this `spec`'s own `deps.yaml`, with the compatibility constraint `>=0.2.0 <1.0.0` (the version that takes the orientation as an input), because no adopted `profile` selects it: `dynamics_shallow_water_source_2d_tc4_forcing`, which evaluates the forcing $Q$ of `tc4_translating_low`. A `component` has exactly one source, so a `component` the adopted `profile` selects is not declared again there. §5 calls the boundary, Coriolis and forcing `component`s with the argument lists of their `0.2.0` interfaces (the wall axis, the per-cell Coriolis parameter, the orientation of the forcing).

## 5. Integration algorithm
The spatial discretization is the second-order `p1` finite-volume scheme: the in-cell reconstruction of each conserved variable (`h`, `hu`, `hv`) is piecewise linear (`MUSCL`) with the `MC` limiter, by the reconstruction `component`, and the interface states it returns are supplied to the flux `component`. No hydrostatic reconstruction is applied, because the bottom is flat. The reconstruction order and the limiter are never switched at runtime.

The time integration is the four-stage `RK4` of `dynamics_shallow_water_time_update_2d_rk4__advance`, which evaluates the tendency at each stage state and stage time itself. This `problem` node supplies the tendency as the procedure argument `rhs` of that operation. `rhs` is a procedure of this `problem`'s generated model, written for this purpose, and it is the only procedure passed as `rhs`; a dependency operation is never passed directly as `rhs`. Its arguments match the prototype `dynamics_shallow_water_time_update_2d_rk4_rhs` of that `component`'s §5.1 in type, kind, rank, explicit shape (`U` and `dUdt` are `(ncomp, nx, ny)`) and `intent`; their names are this `problem`'s own. For an evaluation that reports no error, the tendency `dUdt` that `rhs` returns depends on its arguments and on data fixed for the run before the first step alone — this `problem`'s parameters and what setup derives from them, such as `dx`, `dy` and the setup-time arrays of §6. This constraint does not bind the `dUdt` of an evaluation that reports an error, such as the wet-domain error below. The prototype's argument list carries neither the run-fixed data nor a status, so how `rhs` is bound to those data, and how it reports an error to the step, is the choice of the language backend and the target lowering, not of this `spec`.

The tendency `rhs(ncomp, nx, ny, t, U, dUdt)` computes, for a stage state `U` (interior cells) at the stage time `t`, the following in this order.
1. Copy `U` into the ghost-extended work arrays (`nx+4` × `ny+4`, one per component) and fill their ghost cells with `dynamics_shallow_water_boundary_2d_channel_mirror__apply` with `ng=2` and `wall_axis` the array axis across the channel (`2` for `channel_axis=x`, `1` for `channel_axis=y`), called once per component: `h` and the along-channel momentum with `odd_at_wall=false`, and the cross-channel momentum with `odd_at_wall=true` — `hu` `false` and `hv` `true` for `channel_axis=x`, `hu` `true` and `hv` `false` for `channel_axis=y`.
2. Reconstruct the interface states with `dynamics_shallow_water_reconstruction_2d_muscl_mc__reconstruct` with `ng=2`, called once per component on the ghost-filled work array of step 1: it returns, for that component, the `x`-interface states `U_L`, `U_R` (`nx+1` × `ny`) and the `y`-interface states `U_B`, `U_T` (`nx` × `ny+1`), numbered as in that `component`'s §2 — `x` interface $m$ is the west face of interior cell $m$ ($m=1..nx$) or the east face of cell $nx$ ($m=nx+1$), and `y` interface $n$ likewise in `y`.
3. Compute the interface flux with `dynamics_shallow_water_flux_2d_rusanov__compute_flux`, one call per interface, on the three-component states assembled from step 2. At `x` interface $(m,j)$ its `U_L` and `U_R` arguments are the reconstructed left and right states of $(h,hu,hv)$, its `U_B` and `U_T` arguments are the same two states, and its `F_star` is $F^{*}_{m,j}$; at `y` interface $(i,n)$ its `U_B` and `U_T` arguments are the reconstructed bottom and top states, its `U_L` and `U_R` arguments are the same two states, and its `G_star` is $G^{*}_{i,n}$. The output of each call not named here is not used. The flux `component`'s input/output contract is unchanged: it consumes the interface states the caller supplies.
4. Form the interface-flux difference on the interior cells
$$
L_{flux,i,j}=-\frac{F^{*}_{i+1,j}-F^{*}_{i,j}}{dx}
-\frac{G^{*}_{i,j+1}-G^{*}_{i,j}}{dy}
$$
with the interface numbering of step 2.
5. Compute the Coriolis source $S$ on the interior cells with `dynamics_shallow_water_source_2d_coriolis__apply` at the stage state `U` and the setup-time per-cell `f` (`nx` × `ny`).
6. For `flow_profile=tc4_translating_low`, compute the forcing $Q$ on the interior cells with `dynamics_shallow_water_source_2d_tc4_forcing__apply` with `along_axis` the array axis along the channel (`1` for `channel_axis=x`, `2` for `channel_axis=y`), at the stage time `t` received by `rhs`, with the setup-time cell-centre coordinates, the setup-time per-cell background arrays and the parameters of the low of §6. For every other `flow_profile`, $Q=0$ and the forcing `component` is not called.
7. Return
$$
\frac{dU}{dt}=L_{flux}+S+Q,\qquad S=\left[0,\ f_{i,j}\,(hv)_{i,j},\ -f_{i,j}\,(hu)_{i,j}\right]^T
$$

The stage time `t` is the time at which the forcing is evaluated; it enters no other term.

One time step is `dynamics_shallow_water_time_update_2d_rk4__advance(3, nx, ny, U_n, rhs, t, dt, U_np1, guard_pass)`, with `t` $=t_{start}+n\,dt$ at step $n=0..\mathrm{n\_step}-1$ (the time at the start of the step; `tests.md` §3), evaluated from $n$ and not accumulated. The four stage evaluations, their states and times are the time-update `component`'s (its §3); this `problem` does not evaluate a stage itself.

Every operation of steps 1 to 7 is written for the array axes `x` and `y` and acts on each of them by the same rule: the orientation enters only through `wall_axis`, the `odd_at_wall` assignment, `along_axis`, the per-cell `f` and the background arrays. A case with `channel_axis=y` is computed in that orientation.

The discretization holds the following invariants.
- **Wet domain.** $h>0$ in every interior cell at every stage and every step of the run. The domain of validity of this `problem spec` is the non-drying regime; drying and wetting are out of scope. A cell reaching $h\le0$ is a runtime error, and the run stops with an error before the value reaches the flux `component`, whose contract treats `h<=0` as an error; it must not be handled by clipping or flooring the state.
- **Mass conservation.** The mass flux through each wall interface is exactly zero (the two reconstructed states have equal `h` and opposite wall-normal momentum: §3), the along-channel interfaces are periodic, and the Coriolis source has no `h` component, so for every `flow_profile` but `tc4_translating_low` $\sum_{i,j}h_{i,j}$ is conserved up to round-off. For `tc4_translating_low` the change of $\sum_{i,j}h_{i,j}$ over a step is $dt\sum_{i,j}Q_{h,i,j}$ up to round-off; $\int Q_h\,ds$ over one period is zero at every $r$ and `t`, and the discrete $\sum_{i,j}Q_{h,i,j}$ is below $10^{-15}\sum_{i,j}|Q_{h,i,j}|$ on every grid of `tests.md`.
- **Uniformity along the channel.** For `tc2_zonal_uniform` and `tc3_compact_jet`, an initial state uniform along the channel stays uniform along the channel up to round-off: every along-channel interface-flux difference vanishes identically and every remaining operation acts line by line across the channel. It does not apply to `tc4_translating_low`, whose state and forcing depend on $s$.
- **Translation equivariance along the channel.** For every `flow_profile` but `tc4_translating_low`, `f` depends on $r$ alone, $Q=0$ and the channel is periodic along $s$, so shifting the initial state by a whole number of cells along the channel shifts the solution by the same number of cells, up to round-off. It does not apply to `tc4_translating_low`, whose forcing is not shifted with the state.
- **Axis-exchange equivariance.** For every `flow_profile`, the case with `channel_axis` exchanged (`x` ↔ `y`), `nx` ↔ `ny`, `L_x` ↔ `L_y`, `Omega` → `-Omega` and every other input unchanged has, at every step, the state $(h,\ hu,\ hv)'_{i,j}=(h,\ hv,\ hu)_{j,i}$ of the original case, up to round-off. The equations of §1 are invariant under the exchange of the axes with $f\to-f$; `Omega` → `-Omega` reverses $f_0$, $\beta$ and $\psi_0$ (§6), every profile of §6 is written in $(s, r)$ and the signed quantities, and every operation of the scheme acts on the two array axes by the same rule.
- **Mirror equivariance along the channel.** For every `flow_profile`, the case with `Omega` → `-Omega`, `zonal_sign` → `-zonal_sign`, `shift_x_fraction` → $\tfrac12-$`shift_x_fraction` (modulo 1; `tc2_zonal_perturbed` only) and every other input unchanged has, at every step, the state of the original case reflected along the channel ($s\to L_s-s$: the cell $(nx+1-i,\ j)$ for `channel_axis=x`, $(i,\ ny+1-j)$ for `channel_axis=y`) with the along-channel momentum sign-reversed, up to round-off.
- **Mirror equivariance across the channel.** The case with `theta0` → `-theta0` and every other input unchanged has, at every step, the state of the original case reflected across the channel ($r\to L_r-r$) with the cross-channel momentum sign-reversed, up to round-off, for `tc2_zonal_uniform`, `tc2_zonal_perturbed` and `tc4_translating_low`, whose depth is referred to the channel centre (§6). For `tc3_compact_jet`, whose depth is referred to the wall at $r=0$, the same holds with `gh0` → $g\,h_N$ (the depth beyond the jet, §6) in addition: the reflected depth is referred to the other wall.
- **No work by the Coriolis source.** $(hu)\,S_2+(hv)\,S_3=0$ at every cell (the source `component`'s §3).

The round-off of the three equivariances is not zero in general: the forcing of `tc4_translating_low` sums the advective products of its residuals in the order its `component` writes them, which the exchange reverses, and the centre of the low $s_{c0}+c_{tr}\,t$ rounds differently for opposite $c_{tr}$; a contracted multiply-add or a vector remainder loop changes the last bit under a reordering.

The stability index is defined as
$$
\mathrm{cfl}=dt\cdot\max_{i,j}\left(\frac{|u_{i,j}|+c_{i,j}}{dx}+\frac{|v_{i,j}|+c_{i,j}}{dy}\right)
$$
For the threshold, refer to the judgment conditions of `tests.md`.

## 6. Model parameters and the runtime input contract
The physical constants are those of Williamson et al. (1992), in SI units, with the signs that make the transformed configurations of §1 cases of this `problem`:
- `g = 9.80616 m/s2` (gravitational acceleration)
- `Omega` (unit `1/s`, the signed rotation rate; `Omega = 7.292e-5` for the Earth, and a negative value reverses the sense of rotation; `Omega = 0` is an error)
- `a = 6.37122e6 m` (radius)
- `gh0` (unit `m2/s2`), so `h0 = gh0/g` (reference depth): a case input whose default is `gh0 = 2.94e4` for `tc2_zonal_uniform`, `tc3_compact_jet` and `tc2_zonal_perturbed`, and `gh0 = 1.0e5` for `tc4_translating_low`
- `zonal_sign` (`+1` or `-1`; the direction of the flow along the channel, default `+1`; any other value is an error)
- `u0 = zonal_sign*2*pi*a/(12*86400 s)` (reference velocity along the channel of `tc2_zonal_uniform`, `tc3_compact_jet` and `tc2_zonal_perturbed`; one revolution in 12 days)
- `theta0` (the signed reference latitude of the `β`-plane, default `pi/4`; $0<|\theta_0|<\pi/2$, and any other value is an error)

`h0`, `u0`, and the `f_0`, `β`, $\sigma$ below are derived from these inputs by the stated expressions wherever they are used; none of them is an independent input, and no rounded literal of a derived value stands in for the expression.

The orientation is specified by `channel_axis`, and only the values `x` and `y` are allowed (default `x`). It fixes the along- and cross-channel coordinates of §2 and the orientation sign $\sigma=+1$ for `channel_axis=x`, $\sigma=-1$ for `channel_axis=y`. The geostrophic balance of a flow along the channel, $g\,\nabla h=-f\,\hat k\times\mathbf u$, reads $g\,\partial_r h=-\sigma\,f\,u_s$ in either orientation.

The Coriolis profile is specified by `coriolis_profile`, and only the value `beta_plane` is allowed:
$$
f(r)=f_0+\beta\,\left(r-\tfrac{L_r}{2}\right),\qquad
f_0=2\,\Omega\sin\theta_0,\qquad
\beta=\frac{2\,\Omega\cos\theta_0}{a}
$$
evaluated once at setup at every interior cell centre, as the per-cell field $f_{i,j}=f(r_{i,j})$ with $r_{i,j}=y_j$ for `channel_axis=x` and $r_{i,j}=x_i$ for `channel_axis=y`.

The initial condition is specified by `flow_profile`, and only the 4 values `tc2_zonal_uniform`, `tc3_compact_jet`, `tc2_zonal_perturbed` and `tc4_translating_low` are allowed. The first two are steady flows along the channel, uniform in $s$, geostrophically balanced, with $u_r=0$ and $g\,\partial_r h=-\sigma\,f\,u_s$, discretized at the cell centres with the along-channel momentum $h\,u_s$ and the cross-channel momentum $0$; the third is the first with a depth perturbation that depends on $s$, and is not steady.
- `tc2_zonal_uniform` (Test Case 2): $u_s(r)=u_0$ and
$$
h(r)=h_0-\sigma\,\frac{u_0}{g}\left[f_0\left(r-\tfrac{L_r}{2}\right)+\frac{\beta}{2}\left(r-\tfrac{L_r}{2}\right)^2\right]
$$
- `tc3_compact_jet` (Test Case 3): with $r_b=L_r/8$, $r_e=7L_r/8$, $x_e=0.3$, $\zeta(r)=x_e\,(r-r_b)/(r_e-r_b)$ and $b(\zeta)=0$ for $\zeta\le0$, $b(\zeta)=e^{-1/\zeta}$ for $\zeta>0$,
$$
u_s(r)=u_0\,b(\zeta)\,b(x_e-\zeta)\,e^{4/x_e},\qquad
h(r)=h_0-\frac{\sigma}{g}\int_0^{r}f(r')\,u_s(r')\,dr'
$$
- `tc2_zonal_perturbed`: with $h_{TC2}(r)$ the `tc2_zonal_uniform` depth above and the parameters `eta0` (`m`, $\eta_0>0$) and `shift_x_fraction` (dimensionless, in $[0,1)$; the shift is along the channel, along `y` for `channel_axis=y` despite the name),
$$
h(s,r)=h_{TC2}(r)+\eta_0\,\sin\left(2\pi\left(\frac{s}{L_s}-\mathrm{shift\_x\_fraction}\right)\right),\qquad u_s=u_0,\qquad u_r=0
$$
discretized at the cell centres with the along-channel momentum $h\,u_0$ and the cross-channel momentum $0$. The perturbation is a wavenumber-one gravity-wave excitation of the channel: it is the unforced initial condition of this `problem` that is not uniform along the channel, so it is the one that exercises the along-channel interface flux and the periodic mapping without the forcing.

- `tc4_translating_low` (Test Case 4): the prescribed flow is a geostrophic background jet plus a Gaussian low that translates along the channel. With the parameter `u_jet` (`m/s`, $u_{jet}>0$; the jet maximum, the $u_0$ of Test Case 4), $U_j=\mathrm{zonal\_sign}\cdot u_{jet}$, $k=\pi/L_r$ and $h_0=gh_0/g$ with the `gh0` of the case, the background is
$$
\bar u(r)=U_j\sin^2(kr),\qquad
\bar h(r)=h_0-\sigma\,\frac{U_j}{g}\left[F(r)-F\!\left(\tfrac{L_r}{2}\right)\right]
$$
$$
F(r)=f_0\left(\frac r2-\frac{\sin 2kr}{4k}\right)+\beta\left(\frac{r^2}{4}-\frac{L_r\,r}{4}-\frac{(r-L_r/2)\sin 2kr}{4k}-\frac{\cos 2kr-1}{8k^2}\right)
$$
where $F(r)=\int_0^r f(r')\sin^2(kr')\,dr'$ in closed form, so $g\,\partial_r\bar h=-\sigma\,f\,\bar u$ and $\bar h(L_r/2)=h_0$: the background depth is referred to the channel centre, which makes the reflection across the channel (§5) a case of this `problem` without a change of `gh0`. $\bar u$ is the background velocity along the channel; its cross-channel component is zero. The low has the streamfunction
$$
\psi(s,r,t)=\psi_0\,e^{-\rho^2/R_{low}^2},\qquad
\rho^2=\left(\frac{L_s}{\pi}\right)^2\sin^2\xi+(r-r_c)^2,\qquad
\xi=\frac{\pi\,(s-s_{c0}-c_{tr}\,t)}{L_s}
$$
with $\psi_0=-0.03\,gh_0/f_0$, $R_{low}=2a/12.74244$ (so $R_{low}=1.0\times10^6\ \mathrm{m}$ to the digits given), $c_{tr}=U_j\cos\theta_0$, $s_{c0}=L_s/2$ and $r_c=L_r/2$, and its derivatives along and across the channel
$$
\psi_s=-\frac{L_s}{\pi R_{low}^2}\sin 2\xi\;\psi,\qquad
\psi_r=-\frac{2\,(r-r_c)}{R_{low}^2}\;\psi
$$
assigned to the derivatives in `x` and `y` by the orientation: $(\psi_x, \psi_y)=(\psi_s, \psi_r)$ for `channel_axis=x` and $(\psi_x, \psi_y)=(\psi_r, \psi_s)$ for `channel_axis=y` (the forcing `component`'s §3 states the same derivatives and the second derivatives its residuals need). The prescribed flow is, with the streamfunction convention $u=-\psi_y$, $v=\psi_x$ of either orientation,
$$
\texttt{channel\_axis}=x:\quad \tilde u=\bar u-\psi_y,\qquad \tilde v=\psi_x;\qquad\qquad
\texttt{channel\_axis}=y:\quad \tilde u=-\psi_y,\qquad \tilde v=\bar u+\psi_x
$$
$$
\tilde h=\bar h+\frac{f\,\psi}{g}
$$
and it is an exact solution of the equations of §1 with the forcing $Q$ that `dynamics_shallow_water_source_2d_tc4_forcing` §3 defines, with that `component`'s `S` as $Q$, its `along_axis` as in §5 step 6, its per-cell arguments `f`, `dfdr`, `ubar`, `dubar`, `hbar` the values at the cell centre of $f(r)$, $\beta$, $\bar u(r)$, $\bar u'(r)=U_j\,k\sin 2kr$ and $\bar h(r)$, and `x`, `y`, `g`, `L_along` $=L_s$, `psi0`, `R_low`, `c_tr`, `s_c0`, `r_c` the values above. The derivatives `dfdr` and `dubar` are the closed forms stated here and not differences of the arrays. The five per-cell arrays, the cell-centre coordinates and the parameters of the low are evaluated once at setup. The initial state is the prescribed flow at $t=0$ at the cell centres $(x_i, y_j)$: $h=\tilde h$, $hu=\tilde h\,\tilde u$, $hv=\tilde h\,\tilde v$. The reference solution at time $t$ is the prescribed flow at $t$; the centre of the low moves a distance $c_{tr}\,t$ along the channel, so $\tilde h(x,y,t)-\tilde h(x,y,0)=f\,(\psi(x,y,t)-\psi(x,y,0))/g$.

The integral of `tc3_compact_jet` is evaluated by composite Simpson quadrature on a uniform sub-grid of spacing $\Delta r/64$ (with $\Delta r=dy$ for `channel_axis=x`, $dx$ for `channel_axis=y`) from $r'=0$ to the $j$-th cell-centre cross-channel coordinate $r_j=(j-\tfrac12)\Delta r$, which spans $64j-32$ sub-intervals (an even count for every $j$); this quadrature is part of the definition of the discrete initial state and is not an implementation choice. $u_s$ has compact support in $(r_b, r_e)$ and its largest magnitude is $|u_0|$ at $\zeta=x_e/2$. Because $r_b+r_e=L_r$ and $u_s$ is symmetric about $L_r/2$, the $\beta$ term of $f$ integrates to zero over the support, and the depth beyond the jet ($r>r_e$) is the constant
$$
h_N=h_0-\sigma\,\frac{f_0\,u_0\,e^{4/x_e}}{g}\,\frac{r_e-r_b}{x_e}\,C,\qquad C=\int_0^{x_e}e^{-1/\zeta-1/(x_e-\zeta)}\,d\zeta=1.12064479227927\times10^{-7}
$$
which the discrete initial state reproduces at every line beyond $r_e$ to round-off: the integrand is smooth with compact support inside $[0,r_j]$ for those lines, so the composite rule's error on the complete integral is below round-off. The depth before the jet ($r<r_b$) is $h_0$ exactly, since the integral is zero there. Inside the jet the rule's error against the exact partial integral is below $2\times10^{-12}$ relative at every grid of `tests.md`. $C$ has no closed form; its value is defined by the digits given. The branch-free form $b(\zeta)=\exp\left(-1/\max(\zeta,\varepsilon)\right)$ with $\varepsilon=10^{-300}$ is equal to $b(\zeta)$ at every cell centre in double precision (for $\zeta\le0$ the exponent is $-10^{300}$ and the exponential underflows to exactly $0$; no cell centre has $\zeta=0$, because $r_b$ and $r_e$ lie on cell edges when the cross-channel extent is a multiple of 8, and the sweep of `tests.md` uses such extents), and either form may be used.

The runtime input requires the following.
- `L_x`, `L_y`, `nx`, `ny`
- `channel_axis`
- `coriolis_profile`, and the constants `g`, `Omega`, `a`, `theta0`
- `flow_profile`, `zonal_sign`, and for `tc2_zonal_perturbed` its parameters `eta0`, `shift_x_fraction`, and for `tc4_translating_low` its parameter `u_jet`
- `gh0` (default: the value of the case's `flow_profile`, above)
- `t_start`, `t_end`
- `dt_rule`

`channel_axis` other than `x` or `y`, `zonal_sign` other than `+1` or `-1`, `Omega = 0`, and `theta0` outside $0<|\theta_0|<\pi/2$ are errors at setup.

In the initial state, `h>0` in all cells is required. The depth of each profile is extremal at a wall, and the transformations of §5 map the channel onto itself with the depth field reflected or exchanged, so they leave its minimum unchanged; the values below are those of `channel_axis=x` with every sign positive. For `tc2_zonal_uniform` this bounds the channel width: $h$ is smallest at the wall $r=L_r$, $h(L_r)=h_0-\dfrac{u_0}{g}\left[\dfrac{f_0 L_r}{2}+\dfrac{\beta L_r^2}{8}\right]$, which is positive for $L_r<1.046\times10^7\ \mathrm{m}$ with the constants above ($h(L_r)=1493\ \mathrm{m}$ at $L_r=6\times10^6\ \mathrm{m}$); for `tc2_zonal_perturbed` the bound is $h(L_r)>\eta_0$. For `tc4_translating_low`, $\bar h$ is smallest at the wall $r=L_r$, $\bar h(L_r)=h_0-\dfrac{u_{jet}}{g}\left[\dfrac{f_0L_r}{4}+\beta L_r^2\left(\dfrac1{16}-\dfrac1{4\pi^2}\right)\right]$ ($9199\ \mathrm{m}$ at $u_{jet}=40\ \mathrm{m/s}$, $L_r=8\times10^6\ \mathrm{m}$), and the low lowers the depth at its centre by $|f(r_c)\,\psi_0|/g=0.03\,h_0$ ($306\ \mathrm{m}$ at $gh_0=1.0\times10^5$). A channel outside the bound is an error at setup, not a value to clip. An undefined parameter is an error without implicit completion.

## 7. Prohibitions
Forbid a boundary treatment other than the channel of §3 in the orientation `channel_axis` names — a periodic boundary across the channel, a wall along it, or a wall realized by anything but the mirror ghost — the introduction of a bottom topography, the introduction of a forcing term other than the Coriolis source and the forcing $Q$ of `tc4_translating_low`, a non-zero $Q$ for any other `flow_profile`, the evaluation of $Q$ at a time other than the stage time `t` received by `rhs` or from the state, a background or a low other than those of §6, a reconstruction other than the `p1` `MC` reconstruction of §5 or its runtime switching, a Coriolis profile or a flow profile other than the allowed values, automatic switching of `flow_profile` or `coriolis_profile`, and the runtime automatic switching of the discretization scheme. Forbid `clip` / `limiter` / `filter` on the state `h`; the `MC` limiter of the reconstruction `component` acts on the slopes that build the interface states (§5 step 2), not on the state, and is part of the discretization. Forbid passing anything other than the tendency procedure of §5 as the `rhs` argument of the time-update `component`: a dependency operation passed directly is not the tendency of §5.

Forbid realising the `channel_axis=y` channel by transposing the state, the grid or the coordinates to the `x` orientation, computing there and transposing back: the kernel computes each orientation in that orientation, and the `component`s are called with the orientation inputs of §5 steps 1, 5 and 6. Likewise forbid mapping a case onto another sign of `Omega`, `theta0` or `zonal_sign` inside the kernel — computing the reflected case as the original one and reflecting the result: the inputs are evaluated as given. `tests.md` compares the transformed cases with their originals as symmetry pairs (`docs/TESTS.md` §Symmetry pairs), and a kernel that maps one onto the other compares a code path with itself.

## 8. Traceability
The resolution result is recorded by the host: `<ir_ref>/ir_meta.json` carries the node's own `spec_kind` / `spec_id` / `spec_version`, and `<ir_ref>/dependency_graph.json` carries the resolved closure — `all_nodes` as `<spec_kind>/<spec_id>@<version>` for every node of it (this `spec` itself and the `component`; the runner harness is the target's, not a node of this closure), and `profiles[]` as the adopted `profile`, whose entry carries its own `node_key` for identity but is deliberately absent from `all_nodes`, because a `profile` is not a node.

The reference basis is Williamson et al. (1992, JCP 102, 211–224, DOI:10.1016/S0021-9991(05)80016-6) — Test Cases 2 and 3, equations (95) and (101)–(103), and the balance condition (114) reduced to the channel; Test Case 4, equations (116)–(130), mapped to the channel by replacing $\tan^2(d/2a)$ with $\rho^2/R_{low}^2$, the jet $\sin^{14}(2\theta)$ with $\sin^2(\pi r/L_r)$, and the translation rate $u_0/a$ of the centre's longitude with the speed $c_{tr}=U_j\cos\theta_0$ of its position — LeVeque (2002), Toro (2009), and van Leer (1977) for the `MC` limiter. The orientation and sign inputs, the reference of the `tc4_translating_low` depth to the channel centre and the equivariances of §5 are those of [issue #514](https://github.com/seiya/atmofab/issues/514).

## 9. tests reference
The corresponding `tests.md` is `spec/problem/dynamics/shallow_water/shallow_water2d_channel/tests.md`, with `test_profile_version` of `0.5.0`.

## 10. AD preparation information
`ad_readiness.enabled` is `true`. The state update is expressed in the form $U_{next}=F(U_{now}, params)$, and `max`, `abs`, `ceil`, `exp` (in $b(\zeta)$, with its branch at $\zeta=0$), the periodic-index wrap, the sign reversal at the wall, and the limiter of the reconstruction `component` (the branch on the sign of the product of the two one-sided differences, the `min` over the three slope candidates, and `sign`; that `component`'s §9) are made explicit as non-differentiable operations. `sin`, `cos` and `exp` in the prescribed flow of `tc4_translating_low` and its forcing are differentiable. `channel_axis` and the signs of `Omega`, `theta0` and `zonal_sign` are selections fixed at setup; they introduce no non-differentiable operation into the state update.
