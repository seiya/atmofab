# Tests: 2D shallow water equation (verification input / judgment conditions)

## 0. Meta information
- `status`: `draft`
- `test_profile_id`: `shallow_water2d_baseline`
- `test_profile_version`: `0.6.0`
- `spec_ref.spec_kind`: `problem`
- `spec_ref.spec_id`: `shallow_water2d`
- `spec_ref.spec_version`: `0.4.7`
- `spec_ref.controlled_spec_path`: `spec/problem/dynamics/shallow_water/shallow_water2d/controlled_spec.md`

## 1. Test purpose
This suite verifies, for the discrete implementation of the 2D shallow water equation including bottom topography, conservation, lake-at-rest invariance, stability under topographic forcing, agreement with the theoretical solution — bounded on both sides, since the fully fixed `p0` discretization makes it a property of the scheme — translation equivariance, a rectangular grid, on which an exchange of the two extents, the two domain lengths or the two spacings is not the identity, a linear wave in `y` on that grid, on which a defect confined to the `y` direction is not the identity, the step count of §3, the bottom topography of 2-2 as captured at setup, a lake at rest over the cone on the rectangular grid, the topographic response — the transverse momentum a `y`-uniform wave acquires over the cone, the momentum the cone exchanges with the oblique mode, and a time-refinement pair of the forced case — bounded on both sides, and the `CFL` guard. The error against the theoretical solution is judged in three norms over `h`, `hu` and `hv` with the order of each norm, the cone family against the restricted reference of its finest level in three norms over the free surface, `hu` and `hv`, and the three equivariances of the Controlled Spec §5 by symmetry pairs (`docs/TESTS.md` §Refinement judgment, §Symmetry pairs). The conservation judgment targets mass for all cases, and momentum for the `topography_profile=flat` cases. The judgment targets are `L0` to `L3`, and include an expected failure (`xfail`).

## 2. Input-defaulting rules
### 2-1. Basic constants
- The domain lengths are `L_x=L_y=1.0` (the rectangular cases of 2-5, 2-6 and 2-10 carry `L_y=0.5`, `ny=48`).
- The representative water level is `H_0=1.0`.
- For the gravitational acceleration, use the fixed value `g=9.81` of the Controlled Spec.
- The small amplitude is `eta0=1.0e-3`.
- The reference phase speed of a linear gravity wave is $c_0=\sqrt{gH_0}$.
- The inputs of the symmetry pairs are `wave_sign=+1` (2-3) on every `linear_wave_x` and `linear_wave_y` case, and `x_c_fraction=3/4` and `y_c_fraction=2/3` (2-2) on every `williamson_tc5_cone` case, except the transformed cases of 2-11, which override them (4-3).

### 2-2. Bottom-topography profile
The bottom topography allows the following 2 kinds via `topography_profile`.
- `williamson_tc5_cone`: use the cone topography of the Controlled Spec.
  $$
  d_x(x)=\min\left(|x-x_c|,L_x-|x-x_c|\right),\quad
  d_y(y)=\min\left(|y-y_c|,L_y-|y-y_c|\right)
  $$
  $$
  r(x,y)=\sqrt{d_x(x)^2+d_y(y)^2}
  $$
  $$
  z_b(x,y)=h_s\max\left(0,1-\frac{r(x,y)}{r_0}\right)
  $$
  The topography parameters are $x_c=\mathrm{x\_c\_fraction}\cdot L_x$, $y_c=\mathrm{y\_c\_fraction}\cdot L_y$ (the fractions of 2-1), `r_0=min(L_x,L_y)/6`, and `h_s=0.2H_0`.
- `flat`: `z_b=0` in all cells.

### 2-3. Initial-condition profile
- `linear_wave_x` is defined by the following.
  $$
  \eta(x,y,0)=H_0+\eta_0\sin\left(2\pi\left(\frac{x}{L_x}-\mathrm{shift\_x\_fraction}\right)\right)
  $$
  $$
  h(x,y,0)=\eta(x,y,0)-z_b(x,y)
  $$
  $$
  u(x,y,0)=\mathrm{wave\_sign}\cdot\frac{\eta_0 c_0}{H_0}\sin\left(2\pi\left(\frac{x}{L_x}-\mathrm{shift\_x\_fraction}\right)\right),\quad v(x,y,0)=0
  $$
  $$
  hu=h\,u,\quad hv=h\,v
  $$
- `linear_wave_y` is defined by the following.
  $$
  \eta(x,y,0)=H_0+\eta_0\sin\left(2\pi\left(\frac{y}{L_y}-\mathrm{shift\_y\_fraction}\right)\right)
  $$
  $$
  h(x,y,0)=\eta(x,y,0)-z_b(x,y)
  $$
  $$
  u(x,y,0)=0,\quad v(x,y,0)=\mathrm{wave\_sign}\cdot\frac{\eta_0 c_0}{H_0}\sin\left(2\pi\left(\frac{y}{L_y}-\mathrm{shift\_y\_fraction}\right)\right)
  $$
  $$
  hu=h\,u,\quad hv=h\,v
  $$
- `wave_sign` (`+1` or `-1`) is the direction in which the wave of `linear_wave_x` or `linear_wave_y` travels: `+1` toward increasing `x` or `y`, `-1` the other way (the reflected wave of 2-11).
- `oblique_mode` is defined by the following.
  $$
  \eta(x,y,0)=H_0+\eta_0\sin\left(2\pi\left(\frac{x}{L_x}+\frac{y}{L_y}-\mathrm{shift\_x\_fraction}-\mathrm{shift\_y\_fraction}\right)\right)
  $$
  $$
  h(x,y,0)=\eta(x,y,0)-z_b(x,y),\quad hu=0,\quad hv=0
  $$
- `lake_at_rest` is defined by the following.
  $$
  \eta(x,y,0)=H_0,\quad h(x,y,0)=H_0-z_b(x,y),\quad hu=0,\quad hv=0
  $$

### 2-4. Theoretical solution (with applicability condition)
Only when `topography_profile=flat` and `initial_profile` is `linear_wave_x` or `linear_wave_y`, use the reference solution of the linearized shallow water equation, the wave of 2-3 travelling at $c_0$ in the direction `wave_sign`. For `linear_wave_x` the reference solution is the following.
$$
h_{ref}(x,t)=H_0+\eta_0\sin\left(2\pi\left(\frac{x}{L_x}-\mathrm{wave\_sign}\cdot\frac{c_0 t}{L_x}-\mathrm{shift\_x\_fraction}\right)\right)
$$
$$
hu_{ref}=h_{ref}\,u_{ref},\quad u_{ref}=\mathrm{wave\_sign}\cdot\frac{c_0}{H_0}\left(h_{ref}-H_0\right),\quad hv_{ref}=0
$$
For `linear_wave_y` the reference solution is the following.
$$
h_{ref}(y,t)=H_0+\eta_0\sin\left(2\pi\left(\frac{y}{L_y}-\mathrm{wave\_sign}\cdot\frac{c_0 t}{L_y}-\mathrm{shift\_y\_fraction}\right)\right)
$$
$$
hu_{ref}=0,\quad hv_{ref}=h_{ref}\,v_{ref},\quad v_{ref}=\mathrm{wave\_sign}\cdot\frac{c_0}{H_0}\left(h_{ref}-H_0\right)
$$
At $t=0$ each equals the initial state of 2-3. The three state variables are compared with it in three norms (2-12, 5-4).

### 2-5. The rectangular case
Every case of §4 but the rectangular ones — this one, the `y`-direction case of 2-6, the rectangular topography case of 2-10 and the transformed cases `_rectt` and `_rectymir` of 2-11 — carries $nx=ny$ and `L_x=L_y`, so an implementation that exchanges the two extents, the two domain lengths or the two spacings — an array declared `(ny, nx)`, a loop bounded by `ny` where `nx` is meant, `dx` where `dy` is meant, an `rhs` that reads its stage state as `(ncomp, ny, nx)` — gives the numbers of a correct one on those cases. `swe2d_flat_ref_n064_sx000_sy000_dts100_rect` (4-3) carries `nx=64`, `ny=48`, `L_x=1.0` and `L_y=0.5`, so $dx=1/64$ and $dy=1/96$, and no exchange within any of the three pairs is the identity on a derived quantity. The §3 procedure gives $\mathrm{n\_step}=223$ and $dt=8.969\times10^{-4}$. The wave of `linear_wave_x` is uniform in `y`, so `ny` and `L_y` enter `errors.analytic_h.l2_rel_tend` through `dt` alone: its reference value is `1.2427e-4`, measured on the reference implementation of 5-5, equal to the value of `swe2d_flat_ref_n064_sx000_sy000_dts100` (`1.2428e-4`) to four digits. Measured on the same implementation with one substitution at a time: `L_x` and `L_y` exchanged gives `1.10e-3`; `dx` and `dy` exchanged in the interface-flux difference gives `1.03e-3`; the cell centres evaluated with the spacings exchanged at setup gives `6.45e-4`; an `rhs` that reads the stage state as `(ncomp, ny, nx)` gives `7.07e-4`; an `x` loop bounded by `ny` gives `9.37e-4` (and `1.3e-4` in `mass_drift_rel`). Each lies outside the band of 5-5. `dx` and `dy` exchanged in the dt rule alone gives the reference value and the §3 step count (and does on the `y`-direction case of 2-6): the rule of §3 is symmetric in the two spacings when $|u|,|v|\ll c$, so that exchange changes no judged quantity. The bottom is flat on this case, so the source term of the Controlled Spec §5 step 4 and the topography of 2-2 do not enter it; their exchanges are judged on the case of 2-10.

### 2-6. The `y`-direction case
The wave of `linear_wave_x` is uniform in `y`, so the `y`-interface flux difference $G^{*}_{i,j+1/2}-G^{*}_{i,j-1/2}$ vanishes identically on it, and nothing in the `y` flux, the `y` ghost or the spacing that divides the `y` difference reaches `errors.analytic_h.l2_rel_tend`; the pair of 6-6 shares such a defect between its two runs. `swe2d_flat_ref_n064_sx000_sy000_dts100_recty` (4-3) carries the grid of 2-5 and `initial_profile=linear_wave_y`, the wave of 2-3 turned to `y`, with the reference solution of 2-4. The §3 procedure gives $\mathrm{n\_step}=223$ and $dt=8.969\times10^{-4}$, as for the case of 2-5. Its reference value of `errors.analytic_h.l2_rel_tend` is `2.8482e-4`, measured on the reference implementation of 5-5 (the wave has `48` cells per wavelength and travels `1.25` wavelengths in $t_{end}$, against `64` and `0.63` for the case of 2-5). Measured on the same implementation with one substitution at a time: the `y` flux difference divided by `dx` gives `1.17e-3`; the `y` pressure flux omitted gives `2.65e-3`; the `y` flux omitted gives `1.01e-3`, the value of a state left at its initial value; the one-call step of the Controlled Spec §5 gives `0.78` of the reference; half the steps gives `1.15e-3`. Each lies outside the band of 5-5. A `y` boundary that copies the nearest interior cell instead of the periodic image gives `2.76e-4`, inside the band, and `3.7e-7` in `momy_drift_rel`, which fails the momentum judgment. The `y` flux difference divided by `dx` gives the reference value on every case of §4 that is square or carries the `x` wave; the rectangular `y` waves of 2-11 see it as well (`swe2d_flat_ref_n064_sx000_sy000_dts100_rectt` reads `1.03e-3` in `errors.analytic_h.l2_rel_tend` and `1.6` in `hv` of its axis-exchange residual), and so does the rectangular lake at rest of 2-10 (`0.20` in `lake_rest.max_velocity`). The `y` pressure flux omitted and the `y` flux omitted break the lake-at-rest balance of 6-3 and the topographic response of 2-7 as well (`1.2` and `2.0` in `lake_rest.max_velocity`), and the copied `y` boundary breaks the momentum and translation-equivariance judgments of 6-6 as well. The sign of the transverse momentum flux and the velocity term of the `y` wave speed move the value by less than `0.1%` at the amplitude of 2-1 and are not fixed by this case.

### 2-7. The topographic response
On `williamson_tc5_cone + linear_wave_x` the initial `hv` is zero and the wave is uniform in `y`, so on a flat bottom `hv` stays zero to round-off and without the §5 update it stays zero on any bottom: the `hv` at $t_{end}$ is the momentum the cone generates through the reconstruction of the Controlled Spec §5 step 2 and the source term of its step 4. It is measured by `response.transverse_momentum.l2_rel_tend` (5-4), whose reference values are `1.583e-2`, `1.861e-2` and `1.998e-2` at `nx=32`, `64` and `128` and `1.573e-4` for `swe2d_ref_n064_sx000_sy000_dts100_tend500`, measured on the reference implementation of 5-5, which reproduces the captured state of the certified variant of version `0.4.5` of this node on every case to `3e-15`. The Controlled Spec §5 fixes the reconstruction, and the lake-at-rest invariance of 6-3 does not: three reconstructions that keep exact lake-at-rest balance — the interface elevation taken as the minimum or the average of the two cell elevations instead of their maximum, and the interface state carrying the cell momentum instead of the reconstructed depth times the cell velocity — give `0.59`, `0.79` and `0.82` of the reference at `nx=32` (`0.75`, `0.87` and `0.90` at `nx=64`; `0.86`, `0.93` and `0.95` at `nx=128`) and `1e-15` or less in `lake_rest.max_velocity`. The coarsest case is where a variant of the discretization is visible, and the `nx=32` band of 5-5 excludes all three. Half the steps gives `0.47` to `0.51` of the reference, a state left at its initial value `0`, and a gravitational acceleration halved `0.60` to `0.71`; the source term omitted, its sign flipped, its `y` component omitted, a source from a centred difference of `z_b` times the cell depth or from the cell depth and the interface elevations, and the reconstruction omitted give `3.9` to `2600` times the reference at `nx=32` to `128` and fail 6-3 as well. The one-call step of the Controlled Spec §5 gives `1.03` to `1.11` of the reference at `nx=32` to `128`, inside those bands; it gives `2.9` on the long case and is excluded by the bands of 6-2 and the pair of 2-8 as well. `dt_scale` from `0.5` to `2.0` moves the value by at most `0.04%` at `nx=64` and `0.6%` on the long case.

On `williamson_tc5_cone + oblique_mode` the initial momentum is zero in both directions, so $P^x(t_{end})$ and $P^y(t_{end})$ (5-4) are the time integral of the source term over the run: `momx_drift_rel` and `momy_drift_rel` have the reference values `1.509e-6` and `1.511e-6` on `swe2d_resp_n064_sx000_sy000_dts100`, measured on the same implementation. The interface elevation taken as the minimum gives `0.84` of each; a gravitational acceleration halved `0.63`; a state left at its initial value `0`; the `y` component of the source omitted gives `0` in `momy_drift_rel`, and a source from the cell depth and the interface elevations `0.66` in `momy_drift_rel` (`1.0003` in `momx_drift_rel`, inside the band). `dt_scale` from `0.5` to `2.0` moves them by at most `0.32%`.

### 2-8. Time-refinement pair
The functionals of 2-7 are end-state functionals and move by less than `2%` (`0.9%` in the transverse momentum at `nx=32`, `1.8%` in the momentum exchange of 6-5) when the source term $S_b$ of the Controlled Spec §5 step 4 is evaluated once per step at $U^n$ instead of at each stage state, the error that makes the topographic forcing first order in time; each stays inside its band. The time integration of the forced case is judged by a pair of `williamson_tc5_cone + linear_wave_x` runs at `nx=64` that differ only in `dt_scale` (`1.0` and `0.5`): the difference of their final `hv`, normalised by the initial `hu` of the base case, is `errors.time_pair.hv_l2_rel` (5-4). Its reference value is `8.781e-6`; the pair at `dt_scale` `0.5` and `0.25` gives `2.178e-6`, a ratio of `4.0`, the second order of the §5 step 5 composition. Measured on the reference implementation of 5-5 with one substitution at a time: $S_b$ evaluated at $U^n$ for the second call gives `1.17e-4`; the one-call step `6.26e-4`; `dt` left at $\mathrm{dt\_raw}$ without step 4 of §3 (the run ends past $t_{end}$) `1.28e-4`; half the steps `1.07e-4`; a state left at its initial value `0`; a gravitational acceleration halved `4.29e-6`; the interface elevation taken as the minimum `7.74e-6`; $\lambda_0$ from one direction `3.56e-5` and `cfl_target=0.5` `1.09e-5`. Each lies outside the band of 5-5. The interface elevation taken as the average (`8.25e-6`) and the interface state carrying the cell momentum (`7.94e-6`) lie inside it and are excluded by the `nx=32` band of `transverse_momentum_l2_rel` (5-5, 2-7). The pair does not fix the exchange of `dx` and `dy` in the dt rule (2-5).

### 2-9. The `CFL` guard case
`swe2d_guard_n032_sx000_sy000_dts240` carries `dt_scale=2.40`, so the §3 procedure gives $\mathrm{n\_step}=38$ and $dt=5.263\times10^{-3}$, and the stability index of the Controlled Spec §5 is `1.056` at the initial state and at its maximum over the run (`cfl.max`, 5-2) — above the threshold `1.0` of the `CFL` judgment, which is what 6-7 detects. The case performs all of its §3 steps and stays wet: the depth minimum over the initial state and every committed state is the initial minimum, `0.999005`, against the positivity threshold `5.0e-2` of 5-5, measured on the reference implementation of 5-5. The wet-domain error of the Controlled Spec §5 is not reached by this case. A run of this case whose step loop stops on that error performs fewer steps: its `run.n_step` is the count of steps performed (§3), its judgment against the §3 $\mathrm{n\_step}$ in 6-7 reports `fail`, and 6-7 states what that status decides. The step count and the depth minimum stated here are properties of the fixed discretization at the case's inputs, not conditions of the `pass_when` of 6-7, which reads the `CFL` judgment alone: the `run.n_step` and depth-positivity judgments of the case are applied and their values and statuses reported (6-7).

### 2-10. The rectangular topography case and the initial topography
Every `williamson_tc5_cone` case of §4 but one is square, and on a square grid an exchange of `dx` and `dy` in the source term of the Controlled Spec §5 step 4, or of `L_x` and `L_y` in the topography of 2-2, is the identity. `swe2d_lake_n064_sx000_sy000_dts100_rect` (4-3) carries the grid of 2-5 and `williamson_tc5_cone + lake_at_rest`. The §3 procedure gives $\mathrm{n\_step}=223$ and $dt=8.969\times10^{-4}$; on the reference implementation of 5-5 the case gives `5e-16` in `lake_rest.max_velocity`, `0` in `lake_rest.max_surface_deviation`, `0.4494` in `cfl.max` and `0.8225` in `extrema.h.min`. Measured on the same implementation with one substitution at a time: the `y` component of the source term divided by `dx` instead of `dy` gives `0.14` in `lake_rest.max_velocity`, and the two spacings exchanged in the source term `0.35`; both are the identity on every other case of §4.

The topography itself is judged on every `williamson_tc5_cone` case by `errors.initial_zb.linf_rel` (5-4), the largest difference between the captured initial `z_b` and the closed form of 2-2 at the cell centres. A response of 2-7 does not fix where the cone stands: the wave of `linear_wave_x` is uniform in `y`, so a cone displaced in `y` leaves `response.transverse_momentum.l2_rel_tend` unchanged, and `y_c=L_y/3` leaves the momentum exchange of 6-5 unchanged (measured); a cone displaced by one cell in `x` moves the response by `0.5%` to `1.2%` at `nx=32` to `128`, and a cone displaced by one cell in either direction moves the momentum exchange of 6-5 by `5%`, each inside its band. Measured on the reference implementation of 5-5: the cone centre displaced by one cell in `x` or in `y` gives `0.094` in `errors.initial_zb.linf_rel` at `nx=64` (`0.047` at `nx=128`); `y_c=L_y/3` gives `0.95`; and on the case of this section `r_0=L_x/6` gives `0.49` and `y_c=2L_x/3` gives `0.89`. A conforming implementation differs from the closed form by round-off alone.

### 2-11. Symmetry pairs
The Controlled Spec §5 states three equivariances, and each is judged by pairs of cases (`docs/TESTS.md` §Symmetry pairs): a base case and its transformed case of 4-3, which differs from the base only in the inputs 4-3 names and carries the metric.
- **Axis exchange** (6-11): the transformed case has `nx` ↔ `ny`, `L_x` ↔ `L_y`, `x_c_fraction` ↔ `y_c_fraction` and the initial profile turned to the other axis (`linear_wave_x` ↔ `linear_wave_y`, `shift_x_fraction` ↔ `shift_y_fraction`), and its final state is $(h,\ hu,\ hv)'=(h^T,\ hv^T,\ hu^T)$ of the base's, with $^T$ the exchange of the two array axes. Three pairs: the rectangular wave of 2-5 and its transpose (`48×64`, `L_x=0.5`, `L_y=1.0`, `linear_wave_y`), on which an exchange of the two extents, the two lengths or the two spacings is not the identity; the oblique mode at $(\mathrm{shift\_x\_fraction},\ \mathrm{shift\_y\_fraction})=(0.125,\ 0.25)$ and $(0.25,\ 0.125)$, which loads the `x` and the `y` flux of every cell at once; and the cone case `swe2d_ref_n064_sx000_sy000_dts100` and its transpose (`linear_wave_y`, `x_c_fraction=2/3`, `y_c_fraction=3/4`), which runs the reconstruction and the source term of the Controlled Spec §5 steps 2 and 4 along `y` where the base runs them along `x`. The oblique mode depends on $x/L_x+y/L_y$ alone, so its two cases share an initial state that the exchange maps onto itself, and the pair judges that the solution keeps that symmetry.
- **Mirror in `x`** (6-12): `wave_sign=-1` and `shift_x_fraction` → $\tfrac12-$`shift_x_fraction` (the reflected wave, $\sin(2\pi(1-x/L_x))=\sin(2\pi(x/L_x-\tfrac12))$, travelling toward decreasing `x`), and on the cone case `x_c_fraction=1/4`; the final state is $(h,\ -hu,\ hv)$ of the base's reflected along `x` (the first array axis reversed: the grammar's `flip(·, 0)`). Two pairs: the flat wave `swe2d_flat_ref_n064_sx000_sy000_dts100` and the cone case `swe2d_ref_n064_sx000_sy000_dts100`.
- **Mirror in `y`** (6-13): the same along `y` (the second array axis reversed, `flip(·, 1)`, and `hv` sign-reversed). Two pairs: the rectangular `y`-direction wave of 2-6 with `wave_sign=-1` and `shift_y_fraction=0.5`, and the cone case with `y_c_fraction=1/3` (its wave is uniform in `y`, so only the cone moves).

The initial states of a pair are not compared with each other: the transformed case's topography is judged against its own closed form (`errors.initial_zb.linf_rel`, 5-4), and an initial difference would also appear in the final-state residual. Each pair is judged on every state variable by an `l2` residual (5-4), normalised by the `l2` norm of the base's variable (a momentum component by the larger `l2` norm of the base's two momentum components), with the bound `2.0e-11`: the transformed case runs the same operations on transformed data, so the residual of a conforming kernel is round-off, but not zero. On the reference implementation of 5-5 the three axis-exchange pairs read `0`, and the mirror pairs read at most `8.7e-13` (`hv` of the mirror in `y` of the rectangular wave; `1.5e-13` in `hu` of the flat mirror in `x`, `7.4e-14` on the cone pairs): the reflected initial wave equals the reflection of the base's only to the rounding of the argument of its sine (`2.5e-16` in `hu` at $t=0$), and the pressure term $gh^2/2$ of the momentum flux, three orders of magnitude larger than the momentum of the wave, carries that rounding through the run. The transformed case performs the same `n_step` as its base (`223`, `179`, `179` for the axis exchange, in the order of 6-11; `179`, `179` for the mirror in `x`, 6-12; `223`, `179` for the mirror in `y`, 6-13).

What a pair sees that no band of this suite sees, measured on the reference implementation of 5-5 with one defect at a time, written into the kernel and so present in both runs of the pair: the sign of the transverse momentum flux reversed in the flux of one direction (`hv`$\cdot u$ in the `x` flux, or `hu`$\cdot v$ in the `y` flux) reads `2.2e-3` in `hu` and `hv` on the oblique-mode axis pair and `5.2e-5` in `hu` on the cone axis pair, and moves no judged quantity of the other tests by more than `0.2%` (the momentum exchange of 6-5 by `0.13%`, the time pair of 2-8 by `0.1%`, the transverse momentum of 2-7 by `0.03%`, the `y`-direction case of 2-6 not at all); the wave speed of the Rusanov flux taken from the left state alone ($|u_L|+c_L$ instead of the larger of the two sides) along `x` reads `1.9e-5` in `hu` on the flat mirror in `x` and `1.3e-5` in `hv` on the rectangular axis pair, and along `y` `3.2e-5` in `hv` on the mirror in `y` of the rectangular wave; it changes no theoretical-comparison error in its fourth digit and conserves mass and momentum. Each is at least five orders of magnitude above the bound. A kernel that maps a transformed case onto its base internally and transforms the result back reads `0` on every pair; the Controlled Spec §7 forbids that mapping.

### 2-12. Three norms and orders of the linear-wave family
The error of the three cases of the `swe2d_flat_ref` sweep (4-1) and of the rectangular cases of 2-5 and 2-6 against the theoretical solution of 2-4 is judged in the `l1`, `l2` and `linf` norms over `h`, `hu` and `hv` (`docs/TESTS.md` §Refinement judgment), and the order of each norm of `h` and of `hu` over each pair of adjacent levels of `swe2d_flat_ref`. The normalisers (5-4): `h` and the momentum component along which the wave travels (`hu` for `linear_wave_x`, `hv` for `linear_wave_y`) by the same norm of their own reference; the other momentum component, whose reference is zero, by the larger of that norm over the two momentum references. A wave uniform across its direction keeps the cross-wave momentum at zero up to round-off — every interface across the wave sees two equal states, and the transverse momentum flux carries the zero cross-wave velocity — so that component's error is judged by an upper bound alone, `1.0e-10` in each norm (the reference implementation reads `0`). The reference values (5-5) are measured on the reference implementation of 5-5. The expected order is one (the `p0` scheme); every norm of `h` and of the along-wave momentum reads `0.87` over the first pair and `0.93` over the second, approaching one from below. The error of the along-wave momentum is about `1400` times that of `h`: `h` is normalised by its reference, whose mean $H_0$ is a thousand times the amplitude of the wave, and the momentum reference is the wave alone.

### 2-13. The restricted reference of the cone family
`williamson_tc5_cone + linear_wave_x` has no analytic solution at $t_{end}$. Its family `swe2d_ref` (4-1) carries four levels, `nx=ny=32`, `64`, `128`, `256`, and the finest level is the restricted reference (`docs/TESTS.md` §Refinement judgment): its final free surface $\eta=h+z_b$, `hu` and `hv` averaged over the $f\times f$ blocks of cells that cover each coarser cell ($f=256/n$; the grammar function `coarsen`). The free surface is compared instead of `h` because `z_b` is evaluated at each grid's own cell centres (2-2): the block mean of the finest `z_b` differs from a coarser grid's `z_b` by up to `4.0e-3` at `nx=32` (`2.2e-3` at `64`, `1.1e-3` at `128`; $h_s=0.2$), which `h` carries and $\eta$ does not. The three coarser levels are judged against it in three norms over $\eta$, `hu` and `hv` (`hv`, the momentum the cone generates, by the larger of that norm over the two restricted momentum components), and the order of each norm and variable over the two pairs `32→64` and `64→128`; the finest level judges no error of its own. The expected order is one (the `p0` scheme), and against a reference only twice finer the pair $(n, 2n)$ reads about $p+\log_2\frac{1-(n/N)^p}{1-(2n/N)^p}$ (`1.22` and `1.58` for $p=1$, $N=256$): $\eta$ and `hu` read `1.07` to `1.10` and `1.49` to `1.51`. `hv` reads lower, `0.73` to `0.85` and `1.21` to `1.28`, because the coarse levels are not in its asymptotic range: its error at `nx=32` is a third of its own size (`0.34` in the `l2` norm against its own restricted reference), and a smooth Gaussian topography of the same height reads the same (`0.58` to `0.75` and `1.06` to `1.23`), so the slope discontinuities of the cone are not the reason. The finest level also carries the transverse-momentum response of 2-7: `2.074e-2` at `nx=256` (714 steps).

## 3. Execution-control rules
- $t_{start}=0.0$ and $t_{end}=0.2$.
- `dt` is decided by the following procedure.
  1. Evaluate $\lambda_0=\max_{i,j}\left((|u_{i,j}|+\sqrt{gh_{i,j}})/dx+(|v_{i,j}|+\sqrt{gh_{i,j}})/dy\right)$ at the initial time.
  2. $\mathrm{dt\_raw}=\mathrm{dt\_scale}\cdot \mathrm{cfl\_target}/\lambda_0$.
  3. $\mathrm{n\_step}=\lceil (t_{end}-t_{start})/\mathrm{dt\_raw}\rceil$.
  4. $dt=(t_{end}-t_{start})/\mathrm{n\_step}$.
- $\mathrm{cfl\_target}=0.45$.
- The stop condition is $n=\mathrm{n\_step}$. The number of steps performed is a state variable of the case: the state the host captures after `case_setup` and after `case_run` consists of `h`, `hu`, `hv`, `z_b` (`nx` × `ny` each) and the scalar `n_step` (`0` after setup, the count of `SSPRK2` steps of the Controlled Spec §5 step 5 performed after the run, incremented by the step loop at each step), and it is emitted as the diagnostic `run.n_step`.

## 4. Case-expansion rules
### 4-1. family definition
The `sweep` and fixed values per `family` are defined below.

| family_id | purpose | sweep | fixed |
| --- | --- | --- | --- |
| `swe2d_ref` | refinement_for_mass_and_positivity_with_topography_and_restricted_reference | $nx=ny\in\{32,64,128,256\}$ | `topography_profile=williamson_tc5_cone`, `x_c_fraction=3/4`, `y_c_fraction=2/3`, `initial_profile=linear_wave_x`, `wave_sign=+1`, `shift_x_fraction=0.0`, `shift_y_fraction=0.0`, `dt_scale=1.0` |
| `swe2d_lake` | lake_at_rest_invariance_with_topography | $nx=ny=64$ | `topography_profile=williamson_tc5_cone`, `x_c_fraction=3/4`, `y_c_fraction=2/3`, `initial_profile=lake_at_rest`, `shift_x_fraction=0.0`, `shift_y_fraction=0.0`, `dt_scale=1.0` |
| `swe2d_resp` | topography_forced_response_stability | $nx=ny=64$ | `topography_profile=williamson_tc5_cone`, `x_c_fraction=3/4`, `y_c_fraction=2/3`, `initial_profile=oblique_mode`, `shift_x_fraction=0.0`, `shift_y_fraction=0.0`, `dt_scale=1.0` |
| `swe2d_flat_ref` | refinement_for_linear_wave_analytic | $nx=ny\in\{32,64,128\}$ | `topography_profile=flat`, `initial_profile=linear_wave_x`, `wave_sign=+1`, `shift_x_fraction=0.0`, `shift_y_fraction=0.0`, `dt_scale=1.0` |
| `swe2d_flat_sym` | translation_equivariance_without_topography | $nx=ny=64$ | `topography_profile=flat`, `initial_profile=oblique_mode`, `shift_x_fraction=0.0`, `shift_y_fraction=0.0`, `dt_scale=1.0` |
| `swe2d_guard` | expected_failure_for_cfl | $nx=ny=32,\ \mathrm{dt\_scale}=2.40$ | `topography_profile=flat`, `initial_profile=linear_wave_x`, `wave_sign=+1`, `shift_x_fraction=0.0`, `shift_y_fraction=0.0` |

### 4-2. `case_id` generation rule
- The template is `{family}_n{nx:03d}_sx{sx_pct:03d}_sy{sy_pct:03d}_dts{dts_pct:03d}`.
- `sx_pct` is decided by $\mathrm{round}(100\cdot \mathrm{shift\_x\_fraction})$.
- `sy_pct` is decided by $\mathrm{round}(100\cdot \mathrm{shift\_y\_fraction})$.
- `dts_pct` is decided by $\mathrm{round}(100\cdot \mathrm{dt\_scale})$.
- The expansion order is fixed in the order `family`, `nx`, `shift_x_fraction`, `shift_y_fraction`, `dt_scale`.

### 4-3. Explicit-override cases
Each override case references a `base_case_id` and overrides only the inputs listed; its `case_id` keeps its base's and carries a suffix, so it sorts after its base case (`docs/TESTS.md` §Refinement judgment, carrying case), except the two `swe2d_flat_sym` override cases, whose `case_id` follows the template of 4-2 with the overridden shifts.
- Add the `case_id` `swe2d_ref_n064_sx000_sy000_dts100_tend500`.
- The `base_case_id` references `swe2d_ref_n064_sx000_sy000_dts100`, and overrides only `t_end` to `5.0`.
- Add the `case_id` `swe2d_flat_sym_n064_sx025_sy012_dts100`.
- The `base_case_id` references `swe2d_flat_sym_n064_sx000_sy000_dts100`, and overrides only `shift_x_fraction=0.25` and `shift_y_fraction=0.125`.
- Add the `case_id` `swe2d_flat_ref_n064_sx000_sy000_dts100_rect`.
- The `base_case_id` references `swe2d_flat_ref_n064_sx000_sy000_dts100`, and overrides only `ny` to `48` and `L_y` to `0.5` (the rectangular case of 2-5; the `case_id` keeps its base's `n064` and carries the suffix `_rect`, so it sorts after its base case and before `swe2d_flat_ref_n128_sx000_sy000_dts100`).
- Add the `case_id` `swe2d_flat_ref_n064_sx000_sy000_dts100_recty`.
- The `base_case_id` references `swe2d_flat_ref_n064_sx000_sy000_dts100`, and overrides only `ny` to `48`, `L_y` to `0.5` and `initial_profile` to `linear_wave_y` (the `y`-direction case of 2-6 on the grid of 2-5; the `case_id` keeps its base's `n064` and carries the suffix `_recty`, so it sorts after `swe2d_flat_ref_n064_sx000_sy000_dts100_rect` and before `swe2d_flat_ref_n128_sx000_sy000_dts100`).
- Add the `case_id` `swe2d_ref_n064_sx000_sy000_dts100_half`.
- The `base_case_id` references `swe2d_ref_n064_sx000_sy000_dts100`, and overrides only `dt_scale` to `0.5` (the time-refinement pair of 2-8; the `case_id` keeps its base's `dts100` and carries the suffix `_half`, so it sorts after its base case).
- Add the `case_id` `swe2d_lake_n064_sx000_sy000_dts100_rect`.
- The `base_case_id` references `swe2d_lake_n064_sx000_sy000_dts100`, and overrides only `ny` to `48` and `L_y` to `0.5` (the rectangular topography case of 2-10; the `case_id` keeps its base's `n064` and carries the suffix `_rect`, so it sorts after its base case).

The symmetry-pair cases of 2-11. The `base_case_id` of each is the base whose final state it reads (2-11, 5-4), and the overrides are relative to that base, except the oblique pair: both of its cases take their `base_case_id` from the family case `swe2d_flat_sym_n064_sx000_sy000_dts100` and override only the shifts, and the base the axis-exchange residual of `swe2d_flat_sym_n064_sx025_sy012_dts100` reads is `swe2d_flat_sym_n064_sx012_sy025_dts100`, named here and in 6-11.
- Axis exchange:
  - `swe2d_flat_ref_n064_sx000_sy000_dts100_rectt`: the `base_case_id` references `swe2d_flat_ref_n064_sx000_sy000_dts100_rect`; overrides only `nx` to `48`, `ny` to `64`, `L_x` to `0.5`, `L_y` to `1.0` and `initial_profile` to `linear_wave_y` (the transpose of `_rect`; it sorts after `_rect` and before `_recty`).
  - `swe2d_flat_sym_n064_sx025_sy012_dts100` (the translated case of 6-6, also the transformed case of this pair): its base is `swe2d_flat_sym_n064_sx012_sy025_dts100`, whose `base_case_id` references `swe2d_flat_sym_n064_sx000_sy000_dts100` and which overrides only `shift_x_fraction` to `0.125` and `shift_y_fraction` to `0.25` (a shift of `8` and `16` cells; its `case_id` follows the template of 4-2, `round(12.5) = 12`, and sorts before the translated case).
  - `swe2d_ref_n064_sx000_sy000_dts100_axis`: the `base_case_id` references `swe2d_ref_n064_sx000_sy000_dts100`; overrides only `initial_profile` to `linear_wave_y`, `x_c_fraction` to `2/3` and `y_c_fraction` to `3/4` (`nx=ny`, `L_x=L_y`, so the exchange of the extents and lengths leaves them unchanged).
- Mirror in `x`:
  - `swe2d_flat_ref_n064_sx000_sy000_dts100_xmir`: the `base_case_id` references `swe2d_flat_ref_n064_sx000_sy000_dts100`; overrides only `wave_sign` to `-1` and `shift_x_fraction` to `0.5` (the `case_id` keeps its base's `sx000`).
  - `swe2d_ref_n064_sx000_sy000_dts100_xmir`: the `base_case_id` references `swe2d_ref_n064_sx000_sy000_dts100`; overrides only `wave_sign` to `-1`, `shift_x_fraction` to `0.5` and `x_c_fraction` to `1/4`.
- Mirror in `y`:
  - `swe2d_flat_ref_n064_sx000_sy000_dts100_rectymir`: the `base_case_id` references `swe2d_flat_ref_n064_sx000_sy000_dts100_recty`; overrides only `wave_sign` to `-1` and `shift_y_fraction` to `0.5`.
  - `swe2d_ref_n064_sx000_sy000_dts100_ymir`: the `base_case_id` references `swe2d_ref_n064_sx000_sy000_dts100`; overrides only `y_c_fraction` to `1/3`.

## 5. Diagnostics contract
### 5-1. Artifacts
- The diagnostic output file is `diagnostics.json`.
- The judgment output file is `verdict.json`.

### 5-2. Required diagnostic items
`diagnostics.json` requires the following fields, each a scalar. `<v>` stands for each of `h`, `hu`, `hv`; `<k>` for each of `l1`, `l2`, `linf`; `<pair>` for each of `n032_to_n064` and `n064_to_n128`; `<n>` for each of `n032`, `n064`, `n128`; `<r>` for each of `eta`, `hu`, `hv`.
- `cfl.max`
- `conserved.mass.initial`
- `conserved.mass.final`
- `conserved.momentum_x.initial`
- `conserved.momentum_x.final`
- `conserved.momentum_y.initial`
- `conserved.momentum_y.final`
- `extrema.h.min`
- `errors.analytic_<v>.<k>_rel_tend` (nine fields; `errors.analytic_h.l2_rel_tend` among them)
- `convergence.<pair>.<k>_order` (the order of `h`; six fields)
- `convergence.<pair>.hu_<k>_order` (six fields)
- `errors.restricted.<n>.<r>_<k>_rel` (twenty-seven fields)
- `convergence.restricted.<pair>.<r>_<k>_order` (eighteen fields)
- `errors.axis_exchange.<v>_l2_rel` (three fields)
- `errors.mirror_x.<v>_l2_rel` (three fields)
- `errors.mirror_y.<v>_l2_rel` (three fields)
- `errors.symmetry_h_l2_rel`
- `invariants.lake_rest.max_velocity`
- `invariants.lake_rest.max_surface_deviation`
- `run.n_step`
- `response.transverse_momentum.l2_rel_tend`
- `errors.time_pair.hv_l2_rel`
- `errors.initial_zb.linf_rel`

### 5-3. `N/A` rule
- When a diagnostic item is incomputable or non-applicable, make the output value `null` and require `reason_na`.
- The nine `errors.analytic_*` fields are `N/A` for anything other than `topography_profile=flat` with `initial_profile=linear_wave_x` or `linear_wave_y`.
- The `convergence.n032_to_n064.*` fields (the three of `h` and the three of `hu`) are carried by `swe2d_flat_ref_n064_sx000_sy000_dts100` and the `convergence.n064_to_n128.*` fields by `swe2d_flat_ref_n128_sx000_sy000_dts100`; every other case omits them (5-4).
- The `errors.restricted.*` and `convergence.restricted.*` fields are carried by `swe2d_ref_n256_sx000_sy000_dts100`, which sorts after the three coarser levels of its family and reads their final states; every other case omits them (5-4).
- `errors.axis_exchange.*` is carried by each axis-exchange case of 4-3, `errors.mirror_x.*` by each mirror-in-`x` case and `errors.mirror_y.*` by each mirror-in-`y` case, each reading its base's final state; every other case omits them.
- `errors.symmetry_h_l2_rel` is `N/A` for anything other than a translation-pair evaluation.
- `momx_drift_rel` and `momy_drift_rel` are computed for every case: judged against the thresholds of 5-5 for `topography_profile=flat` in the tests that apply the momentum-conservation judgment (6-2, 6-6, 6-8, 6-9), as the two-sided band of 5-5 for `swe2d_resp_n064_sx000_sy000_dts100` (6-5), and reported and not judged for the other `williamson_tc5_cone` cases, where the source term exchanges momentum and the response is judged by `response.transverse_momentum.l2_rel_tend` (6-1, 6-4, 6-10) or the lake-at-rest invariance (6-3).
- `response.transverse_momentum.l2_rel_tend` is `N/A` for anything other than `topography_profile=williamson_tc5_cone` with `initial_profile=linear_wave_x`.
- `errors.time_pair.hv_l2_rel` is `N/A` for anything other than the halved-`dt` case of the time-refinement pair; it is carried by `swe2d_ref_n064_sx000_sy000_dts100_half`, which sorts after its base case, and reads the base case's initial and final state.
- `invariants.lake_rest.*` is `N/A` for anything other than `initial_profile=lake_at_rest`.
- `run.n_step` is computed for every case and is never `N/A`.
- `errors.initial_zb.linf_rel` is `N/A` for anything other than `topography_profile=williamson_tc5_cone`.

### 5-4. Definition of the metrics
The relative mass-drift value is defined by the following.
$$
\mathrm{mass\_drift\_rel}=
\frac{|M_{end}-M_0|}{\max(|M_0|,1e{-14})},
\quad
M=\sum_{i,j} h_{i,j}\,dx\,dy
$$

The relative momentum-drift value is defined by the following.
$$
\mathrm{momx\_drift\_rel}=
\frac{|P^x_{end}-P^x_0|}{\max(M_0\,c_0,1e{-14})},
\quad
P^x=\sum_{i,j} (hu)_{i,j}\,dx\,dy
$$
$$
\mathrm{momy\_drift\_rel}=
\frac{|P^y_{end}-P^y_0|}{\max(M_0\,c_0,1e{-14})},
\quad
P^y=\sum_{i,j} (hv)_{i,j}\,dx\,dy
$$
Here $M_0$ is the initial mass.

The theoretical-comparison errors (`flat + linear_wave_x` and `flat + linear_wave_y` only) are defined in three norms over each state variable. With $q$ one of `h`, `hu`, `hv` at $t_{end}$, $q_{ref}$ its reference of 2-4 at $t_{end}$ evaluated at the cell centres, the sums and the maximum over all cells, and
$$
\|e\|_1=\sum_{i,j}|e_{i,j}|,\qquad \|e\|_2=\Big(\sum_{i,j}e_{i,j}^2\Big)^{1/2},\qquad \|e\|_\infty=\max_{i,j}|e_{i,j}|,
$$
the error of $q$ in the norm $k\in\{1, 2, \infty\}$ is
$$
\frac{\|q-q_{ref}\|_k}{N_{q,k}}
$$
with the normaliser $N_{q,k}$: for `h` and for the momentum component along the wave (`hu` for `linear_wave_x`, `hv` for `linear_wave_y`), $\|q_{ref}\|_k$; for the other momentum component, whose reference is zero, $\max(\|hu_{ref}\|_k, \|hv_{ref}\|_k)$. They are emitted as `errors.analytic_<v>.<k>_rel_tend`; the `l2` error of `h`,
$$
\mathrm{analytic\_h\_l2\_rel}=
\frac{\|h_{num}(t_{end})-h_{ref}(t_{end})\|_2}{\|h_{ref}(t_{end})\|_2},
$$
is `errors.analytic_h.l2_rel_tend`, as in the previous version.

The translation-equivariance error is defined by the following.
$$
\mathrm{symmetry\_h\_l2\_rel}=
\frac{\|h_{shifted}(t_{end})-\mathrm{shift}(h_{ref}(t_{end}),+\Delta x,+\Delta y)\|_2}{\|h_{ref}(t_{end})\|_2}
$$
Here $\Delta x=\mathrm{shift\_x\_fraction}\cdot L_x$ and $\Delta y=\mathrm{shift\_y\_fraction}\cdot L_y$.

`convergence_order` is a cross-case reduction, using $p=\log(e_{coarse}/e_{fine})/\log(2)$, over the error of each norm of `h` and of `hu` (`errors.analytic_h.<k>_rel_tend`, `errors.analytic_hu.<k>_rel_tend`) between the cases `swe2d_flat_ref_n032_sx000_sy000_dts100` and `swe2d_flat_ref_n064_sx000_sy000_dts100` (the pair `n032_to_n064`) and between `swe2d_flat_ref_n064_sx000_sy000_dts100` and `swe2d_flat_ref_n128_sx000_sy000_dts100` (the pair `n064_to_n128`). It is emitted as a per-case field of the finer case of each pair: `convergence.<pair>.<k>_order` for `h` (`convergence.<pair>.l2_order` is the order of `analytic_h_l2_rel`) and `convergence.<pair>.hu_<k>_order` for `hu`. The cases preceding the one that completes a reduction omit those fields, and so do the override cases of 4-3, which are not members of a refinement pair. The cross-wave momentum `hv` of the family is zero up to round-off (2-12) and has no order.

The restricted-reference errors of the cone family are defined by the following, with $q^{(n)}$ the final state of `swe2d_ref_n<n>_sx000_sy000_dts100` for $n=32, 64, 128$ and $q^{(256)}$ that of `swe2d_ref_n256_sx000_sy000_dts100`, $q$ one of the free surface $\eta=h+z_b$ (with each level's own captured `z_b`), `hu` and `hv`, and $\mathrm{coarsen}(q^{(256)}, 256/n)$ the mean of $q^{(256)}$ over the $(256/n)\times(256/n)$ blocks of cells that cover each cell of the level $n$ (the cell $(i, j)$ of level $n$ is covered by the cells $((i-1)f+1..if,\ (j-1)f+1..jf)$ of level `256`, $f=256/n$):
$$
\mathrm{restricted}^{(n)}_{q,k}=\frac{\|q^{(n)}-\mathrm{coarsen}(q^{(256)},256/n)\|_k}{N^{(n)}_{q,k}}
$$
with $N^{(n)}_{q,k}=\|\mathrm{coarsen}(q^{(256)},f)\|_k$ for $\eta$ and `hu`, and the maximum of that norm over the two restricted momentum components for `hv` (2-13). They are emitted on `swe2d_ref_n256_sx000_sy000_dts100` as `errors.restricted.<n>.<r>_<k>_rel` with `<n>` the zero-padded level (`n032`, `n064`, `n128`) and `<r>` one of `eta`, `hu`, `hv` (5-2), and the orders $\log_2(\mathrm{restricted}^{(n)}/\mathrm{restricted}^{(2n)})$ as `convergence.restricted.<pair>.<r>_<k>_order`.

The symmetry-pair residuals are defined by the following, with $q'$ the final state of the transformed case and $q$ that of its base (4-3), every norm the `l2` norm over all cells, $N_h=\|h\|_2$ and $N_m=\max(\|hu\|_2,\|hv\|_2)$ the base's norms, $\cdot^T$ the exchange of the two array axes, $\mathrm{flip}_x$ the reversal of the first array axis, `x` (the cell $(i, j)$ taken from $(nx+1-i, j)$; the grammar's `flip(·, 0)`, whose axes count from `0`), and $\mathrm{flip}_y$ the reversal of the second array axis, `y` (the cell $(i, j)$ taken from $(i, ny+1-j)$; `flip(·, 1)`):
$$
\mathrm{axis\_exchange}:\quad \frac{\|h'-h^T\|}{N_h},\quad \frac{\|hu'-hv^T\|}{N_m},\quad \frac{\|hv'-hu^T\|}{N_m}
$$
$$
\mathrm{mirror\_x}:\quad \frac{\|h'-\mathrm{flip}_x(h)\|}{N_h},\quad \frac{\|hu'+\mathrm{flip}_x(hu)\|}{N_m},\quad \frac{\|hv'-\mathrm{flip}_x(hv)\|}{N_m}
$$
$$
\mathrm{mirror\_y}:\quad \frac{\|h'-\mathrm{flip}_y(h)\|}{N_h},\quad \frac{\|hu'-\mathrm{flip}_y(hu)\|}{N_m},\quad \frac{\|hv'+\mathrm{flip}_y(hv)\|}{N_m}
$$
They are emitted as `errors.axis_exchange.{h,hu,hv}_l2_rel`, `errors.mirror_x.{h,hu,hv}_l2_rel` and `errors.mirror_y.{h,hu,hv}_l2_rel` on the transformed case.

The velocity metric of lake-at-rest invariance is defined by the following.
$$
\mathrm{lake\_rest\_max\_velocity}=
\max_{i,j}\left(\sqrt{u_{i,j}^2+v_{i,j}^2}\right)
$$

The surface-deviation metric of lake-at-rest invariance is defined by the following.
$$
\mathrm{lake\_rest\_max\_surface\_deviation}=
\max_{i,j}\left|\eta_{i,j}(t_{end})-\eta_{i,j}(0)\right|,
\quad \eta=h+z_b
$$

`run.n_step` is the state variable `n_step` after the run (§3); its judged value is the $\mathrm{n\_step}$ of the §3 procedure evaluated from the case's inputs, $\lceil (t_{end}-t_{start})/\mathrm{dt\_raw}\rceil$ with $\mathrm{dt\_raw}$ from the initial state.

The transverse-momentum response (`williamson_tc5_cone + linear_wave_x` only) is defined by the following, over all cells, and emitted as `response.transverse_momentum.l2_rel_tend`.
$$
\mathrm{transverse\_momentum\_l2\_rel}=\frac{\|hv(t_{end})\|_2}{\|hu(0)\|_2}
$$

The time-refinement difference is defined by the following, with $hv_{base}$ and $hu_{base}(0)$ the final `hv` and the initial `hu` of `swe2d_ref_n064_sx000_sy000_dts100` and $hv_{half}$ the final `hv` of `swe2d_ref_n064_sx000_sy000_dts100_half`; it is emitted as `errors.time_pair.hv_l2_rel` on the latter.
$$
\mathrm{time\_pair\_hv\_l2\_rel}=\frac{\|hv_{half}(t_{end})-hv_{base}(t_{end})\|_2}{\|hu_{base}(0)\|_2}
$$

The initial-topography error (`williamson_tc5_cone` only) is defined by the following, with $z_b^{ref}$ the closed form of 2-2 evaluated at the cell centres $(x_i,y_j)=((i+\tfrac12)\,dx,\ (j+\tfrac12)\,dy)$ and $z_b(0)$ the `z_b` captured after `case_setup` (§3); it is emitted as `errors.initial_zb.linf_rel`.
$$
\mathrm{initial\_zb\_linf\_rel}=\frac{\max_{i,j}\left|z_{b,i,j}(0)-z_b^{ref}(x_i,y_j)\right|}{h_s}
$$

`cross_target_state_agreement` compares this target's numerical solution with every other target's certified variant of this node, case by case (`docs/TESTS.md` §Cross-target judgment). For each of the state variables `h`, `hu`, and `hv` at $t_{end}$ of a case, with $q$ that variable and $q^{ref}$ the same variable at $t_{end}$ of the same case in the other variant,
$$
\mathrm{cross\_target\_state\_agreement}=\frac{\max_{i,j} |q_{i,j}-q^{ref}_{i,j}|}{N_q}
$$
The normaliser of `h` is $N_h=\max(\max_{i,j}|h_{i,j}|,\ \max_{i,j}|h^{ref}_{i,j}|,\ 1)$. The two momentum components share one normaliser, the momentum scale of the two variants, $N_{hu}=N_{hv}=\max(\max_{i,j}|hu_{i,j}|,\ \max_{i,j}|hv_{i,j}|,\ \max_{i,j}|hu^{ref}_{i,j}|,\ \max_{i,j}|hv^{ref}_{i,j}|,\ 1)$: a component that is a small residue of the flow (the cross-stream `hv` of a zonal jet) carries the rounding of the whole momentum field, so its own maximum is not its rounding scale. It is evaluated by the host from the captured state, against each other variant separately and for each variable separately; it is not a `diagnostics.json` field, and on a target with no other certified variant it holds vacuously.

### 5-5. Default thresholds
The thresholds of `analytic_h_l2_rel`, `transverse_momentum_l2_rel`, the momentum exchange of 6-5 and `time_pair_hv_l2_rel` are calibrated against independent reference implementations of the Controlled Spec §5 scheme, and every measurement this document attributes to "the reference implementation of 5-5" was taken on one of them: the bands of the three `swe2d_flat_ref` sweep cases and of `swe2d_flat_ref_n064_sx000_sy000_dts100_rect`, and 2-5, on the implementation recorded on issue #491 (calibration comment), which reproduces the values of the certified variants of versions `0.4.1` to `0.4.3` of this node to five digits; 2-6 to 2-10 and the bands of `transverse_momentum_l2_rel`, the momentum exchange, `time_pair_hv_l2_rel` and the `y`-direction case on the implementation recorded on issues #494 and #495 (calibration comments), which reproduces the captured state of the certified variant of version `0.4.5` of this node on every case to `3e-15`; 2-11 to 2-13, the three-norm bands and orders, the restricted-reference bands and orders, the `nx=256` band of `transverse_momentum_l2_rel` and the symmetry pairs on the same implementation with the inputs of 2-1 to 2-3 added, recorded on issue #513 (calibration comment). Every band introduced in this version follows the rule of the `analytic_h_l2_rel` bands below — `0.9` and `1.1` times the reference value, rounded outward to two digits, the value being a property of the fixed discretization that moves by round-off alone — and every order bound is the reference order minus `0.3`, rounded down to one decimal (`0.4` where the reference order is below `0.7`), except the `l2` order of `h`, `0.80`, kept from the previous version.

- $\mathrm{cfl.max} \le 1.0$, and `run.n_step` equals the §3 $\mathrm{n\_step}$ of the case (an integer; judged as $|\mathrm{run.n\_step}-\mathrm{n\_step}| \le 0.5$)
- $h_{min} \ge 5.0e{-2}$
- $\mathrm{mass\_drift\_rel} \le 1.0e{-10}$
- $\mathrm{momx\_drift\_rel} \le 1.0e{-10}$ and $\mathrm{momy\_drift\_rel} \le 1.0e{-10}$ for the `topography_profile=flat` cases. For `swe2d_resp_n064_sx000_sy000_dts100` (6-5) each is judged as the two-sided band $[1.3e{-6},\ 1.7e{-6}]$ (references `1.509e-6` and `1.511e-6`, 2-7; `0.9` and `1.1` times them, rounded outward to two digits): the two momenta are zero at $t=0$ and their value at $t_{end}$ is the integral of the source term of the Controlled Spec §5 over the run, a property of the fixed discretization — a value below the band is a run that did not perform the §5 update or a source term weaker than §5's, and a value above it a stronger one. Each bound is a DISTINCT judged quantity of the test with its own host-evaluated corroborant over the captured state, as for `analytic_h_l2_rel`.
- `analytic_h_l2_rel` is judged as a two-sided band (6-2, 6-8, 6-9). The Controlled Spec §5 fixes every term of the discretization (`p0` interface states, the Rusanov flux, the periodic ghost, the two-stage `SSPRK2` composition of its step 5) and §3 fixes `dt`, so the error against the linear-wave solution at $t_{end}$ is a property of the discretization — its truncation error and numerical diffusion — and not a free quantity: a value below the band is not a better scheme, it is a run that did not perform the §5 update (a state that holds the theoretical solution at $t_{end}$ without the update has zero error and passes an upper bound alone), and a value above it is a state left at its initial value (`1.304e-3` on every `linear_wave_x` case of `swe2d_flat_ref`, `1.009e-3` on the `y`-direction case of 2-6), a stopped update (half the steps gives `1.13e-3` at `nx=64`) or a wrong one. The value moves by round-off alone under a conforming implementation — the certified variants of versions `0.4.1` to `0.4.3` of this node agree to `3e-12` relative, and `dt_scale` from `0.5` to `2.0` moves it by at most `0.12%` — so the bounds are `0.9` and `1.1` times the reference value, rounded outward to two digits. The lower bound and the upper bound are two DISTINCT judged quantities of the test — each names its own `quantity` and each requires its own host-evaluated corroborant over the captured state — not two thresholds on one quantity: a corroborant that reads only the upper bound leaves the lower bound to the checks module's own report, and a state that holds the theoretical solution with a reported value inside the band would then pass. For `swe2d_flat_ref` the band is $[2.0e{-4},\ 2.5e{-4}]$ for `nx=32` (reference `2.2671e-4`), $[1.1e{-4},\ 1.4e{-4}]$ for `nx=64` (reference `1.2428e-4`), and $[5.8e{-5},\ 7.2e{-5}]$ for `nx=128` (reference `6.5158e-5`); for the rectangular case `swe2d_flat_ref_n064_sx000_sy000_dts100_rect` it is $[1.1e{-4},\ 1.4e{-4}]$ (reference `1.2427e-4`, 2-5); for the `y`-direction case `swe2d_flat_ref_n064_sx000_sy000_dts100_recty` it is $[2.5e{-4},\ 3.2e{-4}]$ (reference `2.8482e-4`, 2-6). The one-call step $U^{n+1}=U^n+\Delta t\,L(U^n)$ (the forward-Euler step the Controlled Spec §5 step 5 excludes) gives `0.78` to `0.83` of the reference value on every case and fails the lower bound; a gravitational acceleration in error by `5%` moves the value outside the band on `nx=128` in either direction, and one in error by `1%` on no case.
- `transverse_momentum_l2_rel` is judged as a two-sided band (6-1, 6-4, 6-10). The Controlled Spec §5 fixes the reconstruction and the source term, so the transverse momentum the cone generates from a `y`-uniform wave is a property of the discretization (2-7): a value below the band is a run that did not perform the §5 update or a reconstruction weaker than §5's, and a value above it a stronger one. The bounds are `0.9` and `1.1` times the reference value, rounded outward to two digits: $[1.4e{-2},\ 1.8e{-2}]$ for `nx=32` (reference `1.583e-2`), $[1.6e{-2},\ 2.1e{-2}]$ for `nx=64` (reference `1.861e-2`; the halved case of 2-8 carries the same band, its reference `1.861e-2` equal to its base's to four digits), $[1.7e{-2},\ 2.2e{-2}]$ for `nx=128` (reference `1.998e-2`), $[1.8e{-2},\ 2.3e{-2}]$ for `nx=256` (reference `2.074e-2`, 2-13), and $[1.4e{-4},\ 1.8e{-4}]$ for `swe2d_ref_n064_sx000_sy000_dts100_tend500` (reference `1.573e-4`). The lower bound and the upper bound are two DISTINCT judged quantities of the test, each with its own host-evaluated corroborant over the captured state, as for `analytic_h_l2_rel`.
- `time_pair_hv_l2_rel` is judged as a two-sided band $[7.9e{-6},\ 9.7e{-6}]$ on `swe2d_ref_n064_sx000_sy000_dts100_half` (reference `8.781e-6`, 2-8; `0.9` and `1.1` times it, rounded outward to two digits), as two distinct judged quantities, each with its own host-evaluated corroborant over the captured states of the two cases.
- The other theoretical-comparison errors of 2-12 are judged as two-sided bands on the same ground, two distinct judged quantities each; the rows of `h` in the `l2` norm are the bands above. The cross-wave momentum (`hv` of a `linear_wave_x` case, `hu` of the `y`-direction case) is judged by the upper bound $\le 1.0e{-10}$ in each norm (2-12). The bands are, per case, variable and norm (reference values in parentheses):

| case | variable | `l1` | `l2` | `linf` |
| --- | --- | --- | --- | --- |
| `swe2d_flat_ref_n032_sx000_sy000_dts100` | `h` | $[1.8e{-4},\ 2.3e{-4}]$ (`2.041e-4`) | $[2.0e{-4},\ 2.5e{-4}]$ (`2.267e-4`) | $[2.8e{-4},\ 3.6e{-4}]$ (`3.203e-4`) |
| | `hu` | $[2.8e{-1},\ 3.6e{-1}]$ (`3.200e-1`) | $[2.8e{-1},\ 3.6e{-1}]$ (`3.206e-1`) | $[2.8e{-1},\ 3.6e{-1}]$ (`3.215e-1`) |
| `swe2d_flat_ref_n064_sx000_sy000_dts100` | `h` | $[1.0e{-4},\ 1.3e{-4}]$ (`1.118e-4`) | $[1.1e{-4},\ 1.4e{-4}]$ (`1.243e-4`) | $[1.5e{-4},\ 2.0e{-4}]$ (`1.757e-4`) |
| | `hu` | $[1.5e{-1},\ 2.0e{-1}]$ (`1.756e-1`) | $[1.5e{-1},\ 2.0e{-1}]$ (`1.758e-1`) | $[1.5e{-1},\ 2.0e{-1}]$ (`1.759e-1`) |
| `swe2d_flat_ref_n128_sx000_sy000_dts100` | `h` | $[5.2e{-5},\ 6.5e{-5}]$ (`5.862e-5`) | $[5.8e{-5},\ 7.2e{-5}]$ (`6.516e-5`) | $[8.3e{-5},\ 1.1e{-4}]$ (`9.228e-5`) |
| | `hu` | $[8.2e{-2},\ 1.1e{-1}]$ (`9.208e-2`) | $[8.2e{-2},\ 1.1e{-1}]$ (`9.215e-2`) | $[8.3e{-2},\ 1.1e{-1}]$ (`9.236e-2`) |
| `swe2d_flat_ref_n064_sx000_sy000_dts100_rect` | `h` | $[1.0e{-4},\ 1.3e{-4}]$ (`1.118e-4`) | $[1.1e{-4},\ 1.4e{-4}]$ (`1.243e-4`) | $[1.5e{-4},\ 2.0e{-4}]$ (`1.757e-4`) |
| | `hu` | $[1.5e{-1},\ 2.0e{-1}]$ (`1.756e-1`) | $[1.5e{-1},\ 2.0e{-1}]$ (`1.757e-1`) | $[1.5e{-1},\ 2.0e{-1}]$ (`1.759e-1`) |
| `swe2d_flat_ref_n064_sx000_sy000_dts100_recty` | `h` | $[2.3e{-4},\ 2.9e{-4}]$ (`2.563e-4`) | $[2.5e{-4},\ 3.2e{-4}]$ (`2.848e-4`) | $[3.6e{-4},\ 4.5e{-4}]$ (`4.025e-4`) |
| | `hv` | $[3.6e{-1},\ 4.5e{-1}]$ (`4.024e-1`) | $[3.6e{-1},\ 4.5e{-1}]$ (`4.028e-1`) | $[3.6e{-1},\ 4.5e{-1}]$ (`4.030e-1`) |

- `convergence_order` uses $p=\log(e_{coarse}/e_{fine})/\log(2)$ over each error of `h` and `hu` of the three cases of `swe2d_flat_ref` that 6-2 judges, and is emitted as a per-case field of the finer case of each pair (5-4); the override cases of 4-3 are not members of a refinement pair and omit the fields. It requires the following lower bounds (reference orders in parentheses):

| pair | variable | `l1` | `l2` | `linf` |
| --- | --- | --- | --- | --- |
| `n032_to_n064` | `h` | $\ge 0.5$ (`0.87`) | $\ge 0.80$ (`0.87`) | $\ge 0.5$ (`0.87`) |
| | `hu` | $\ge 0.5$ (`0.87`) | $\ge 0.5$ (`0.87`) | $\ge 0.5$ (`0.87`) |
| `n064_to_n128` | `h` | $\ge 0.6$ (`0.93`) | $\ge 0.80$ (`0.93`) | $\ge 0.6$ (`0.93`) |
| | `hu` | $\ge 0.6$ (`0.93`) | $\ge 0.6$ (`0.93`) | $\ge 0.6$ (`0.93`) |

- The restricted-reference errors of the cone family (2-13), carried by `swe2d_ref_n256_sx000_sy000_dts100`, are judged as two-sided bands on the same ground, two distinct judged quantities each, and their orders by lower bounds (the reasons for the orders of `hv` are stated in 2-13):

| level | variable | `l1` | `l2` | `linf` |
| --- | --- | --- | --- | --- |
| `n032` | `eta` | $[1.5e{-4},\ 1.9e{-4}]$ (`1.716e-4`) | $[1.7e{-4},\ 2.1e{-4}]$ (`1.904e-4`) | $[2.6e{-4},\ 3.3e{-4}]$ (`2.927e-4`) |
| | `hu` | $[2.5e{-1},\ 3.2e{-1}]$ (`2.850e-1`) | $[2.5e{-1},\ 3.2e{-1}]$ (`2.853e-1`) | $[2.5e{-1},\ 3.2e{-1}]$ (`2.853e-1`) |
| | `hv` | $[5.4e{-3},\ 6.8e{-3}]$ (`6.110e-3`) | $[6.5e{-3},\ 8.0e{-3}]$ (`7.241e-3`) | $[1.0e{-2},\ 1.4e{-2}]$ (`1.209e-2`) |
| `n064` | `eta` | $[7.3e{-5},\ 9.0e{-5}]$ (`8.116e-5`) | $[8.0e{-5},\ 9.9e{-5}]$ (`8.995e-5`) | $[1.2e{-4},\ 1.6e{-4}]$ (`1.391e-4`) |
| | `hu` | $[1.2e{-1},\ 1.5e{-1}]$ (`1.345e-1`) | $[1.2e{-1},\ 1.5e{-1}]$ (`1.343e-1`) | $[1.2e{-1},\ 1.5e{-1}]$ (`1.334e-1`) |
| | `hv` | $[3.0e{-3},\ 3.8e{-3}]$ (`3.400e-3`) | $[3.6e{-3},\ 4.6e{-3}]$ (`4.107e-3`) | $[6.5e{-3},\ 8.1e{-3}]$ (`7.289e-3`) |
| `n128` | `eta` | $[2.5e{-5},\ 3.2e{-5}]$ (`2.848e-5`) | $[2.8e{-5},\ 3.5e{-5}]$ (`3.153e-5`) | $[4.4e{-5},\ 5.5e{-5}]$ (`4.942e-5`) |
| | `hu` | $[4.2e{-2},\ 5.2e{-2}]$ (`4.715e-2`) | $[4.2e{-2},\ 5.2e{-2}]$ (`4.701e-2`) | $[4.2e{-2},\ 5.2e{-2}]$ (`4.687e-2`) |
| | `hv` | $[1.2e{-3},\ 1.6e{-3}]$ (`1.399e-3`) | $[1.5e{-3},\ 1.9e{-3}]$ (`1.704e-3`) | $[2.8e{-3},\ 3.5e{-3}]$ (`3.153e-3`) |
| `n032_to_n064` order | `eta` | $\ge 0.7$ (`1.08`) | $\ge 0.7$ (`1.08`) | $\ge 0.7$ (`1.07`) |
| | `hu` | $\ge 0.7$ (`1.08`) | $\ge 0.7$ (`1.09`) | $\ge 0.7$ (`1.097`) |
| | `hv` | $\ge 0.5$ (`0.85`) | $\ge 0.5$ (`0.82`) | $\ge 0.4$ (`0.73`) |
| `n064_to_n128` order | `eta` | $\ge 1.2$ (`1.51`) | $\ge 1.2$ (`1.51`) | $\ge 1.1$ (`1.49`) |
| | `hu` | $\ge 1.2$ (`1.51`) | $\ge 1.2$ (`1.51`) | $\ge 1.2$ (`1.51`) |
| | `hv` | $\ge 0.9$ (`1.28`) | $\ge 0.9$ (`1.27`) | $\ge 0.9$ (`1.21`) |

- Every symmetry-pair residual `errors.axis_exchange.*`, `errors.mirror_x.*` and `errors.mirror_y.*` is $\le 2.0e{-11}$ for each of `h`, `hu` and `hv` (2-11; the reference implementation reads at most `8.7e-13`)
- `initial_zb_linf_rel` $\le 1.0e{-12}$ (6-1, 6-3, 6-4, 6-5, 6-10): the topography is a closed form of the case's inputs evaluated once at setup, so a conforming value differs from it by round-off alone.
- `lake_rest.max_velocity \le 1.0e{-12}`
- `lake_rest.max_surface_deviation \le 1.0e{-12}`
- `symmetry_h_l2_rel \le 2.0e{-11}`
- $\mathrm{cross\_target\_state\_agreement} \le 1.0e{-10}$ for each of `h`, `hu`, and `hv`

## 6. Test definitions
### 6-1. `l1_refinement_mass_and_positivity`
- `level`: `L1`
- `objective`: confirm mass conservation and positivity against refinement for `williamson_tc5_cone + linear_wave_x`, the transverse momentum the cone generates, bounded on both sides, and the error of the free surface, `hu` and `hv` of the levels `32`, `64` and `128` against the restricted reference of the level `256` in three norms and its decrease with refinement (2-13).
- target cases:
  - `swe2d_ref_n032_sx000_sy000_dts100`
  - `swe2d_ref_n064_sx000_sy000_dts100`
  - `swe2d_ref_n128_sx000_sy000_dts100`
  - `swe2d_ref_n256_sx000_sy000_dts100`
- `expected_outcome`: `pass`
- judgment conditions:
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of the case ($|\mathrm{run.n\_step}-\mathrm{n\_step}| \le 0.5$).
  - The depth-positivity judgment is applied. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 5.0e{-2}$.
  - The mass-conservation judgment is applied. The evaluation expression is `mass_drift_rel`, and the threshold is $\le 1.0e{-10}$.
  - The initial-topography judgment is applied. The evaluation expression is `errors.initial_zb.linf_rel`, and the threshold is $\le 1.0e{-12}$.
  - The topographic-response judgment is applied. `transverse_momentum_l2_rel` is judged per case as the two-sided band of 5-5, as two judged quantities.
  - The restricted-reference judgment is applied. Each of the twenty-seven `errors.restricted.<n>.<r>_<k>_rel` (carried by `swe2d_ref_n256_sx000_sy000_dts100`) applies its band of the 5-5 restricted table as two judged quantities, and each of the eighteen `convergence.restricted.<pair>.<r>_<k>_order` (carried by the same case) applies its lower bound of that table.
  - The momentum-conservation judgment is not applied. The non-application basis is "because the bottom-topography source term exchanges momentum; the response is judged by `transverse_momentum_l2_rel`".
  - The theoretical-comparison judgment is not applied. The non-application basis is "because it is a case with topography; its error is judged against the restricted reference above".
  - The translation-equivariance and symmetry-pair judgments are not applied. The non-application basis is "because the cases are judged here as refinement levels; the pairs whose base is `swe2d_ref_n064_sx000_sy000_dts100` are judged in 6-11 to 6-13".
  - The lake-at-rest invariance judgment is not applied. The non-application basis is "because the initial condition is not `lake_at_rest`".
  - The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, and `hv`, and the threshold is $\le 1.0e{-10}$ for each.

### 6-2. `l1_refinement_linear_wave`
- `level`: `L1`
- `objective`: confirm the theoretical-solution error of `h`, `hu` and `hv` in three norms and its decrease with refinement for `flat + linear_wave_x` (2-12).
- target cases:
  - `swe2d_flat_ref_n032_sx000_sy000_dts100`
  - `swe2d_flat_ref_n064_sx000_sy000_dts100`
  - `swe2d_flat_ref_n128_sx000_sy000_dts100`
- `expected_outcome`: `pass`
- judgment conditions:
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of the case ($|\mathrm{run.n\_step}-\mathrm{n\_step}| \le 0.5$).
  - The depth-positivity judgment is applied. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 5.0e{-2}$.
  - The mass-conservation judgment is applied. The evaluation expression is `mass_drift_rel`, and the threshold is $\le 1.0e{-10}$.
  - The momentum-conservation judgment is applied. The evaluation expressions are `momx_drift_rel` and `momy_drift_rel`, and the threshold for both is $\le 1.0e{-10}$.
  - The theoretical-comparison judgment is applied. Each of the six `errors.analytic_h.<k>_rel_tend` and `errors.analytic_hu.<k>_rel_tend` of each case applies its band of the 5-5 table (rows `swe2d_flat_ref_n032_sx000_sy000_dts100`, `_n064_`, `_n128_`) as two judged quantities, each of the three `errors.analytic_hv.<k>_rel_tend` the upper bound $\le 1.0e{-10}$, and each of the six `convergence.n032_to_n064.*_order` fields (carried by `swe2d_flat_ref_n064_sx000_sy000_dts100`) and the six `convergence.n064_to_n128.*_order` fields (carried by `swe2d_flat_ref_n128_sx000_sy000_dts100`) its lower bound of the 5-5 order table.
  - The translation-equivariance and symmetry-pair judgments are not applied. The non-application basis is "because the cases are judged here as refinement levels; the pair whose base is `swe2d_flat_ref_n064_sx000_sy000_dts100` is judged in 6-12".
  - The lake-at-rest invariance judgment is not applied. The non-application basis is "because the initial condition is not `lake_at_rest`".
  - The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, and `hv`, and the threshold is $\le 1.0e{-10}$ for each.

### 6-3. `l2_lake_at_rest_invariance`
- `level`: `L2`
- `objective`: confirm that the velocity and free surface are invariant for `williamson_tc5_cone + lake_at_rest`, on the square grid and on the rectangular grid of 2-10, and that the captured topography is that of 2-2.
- target cases:
  - `swe2d_lake_n064_sx000_sy000_dts100`
  - `swe2d_lake_n064_sx000_sy000_dts100_rect`
- `expected_outcome`: `pass`
- judgment conditions:
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of each case ($|\mathrm{run.n\_step}-\mathrm{n\_step}| \le 0.5$).
  - The depth-positivity judgment is applied. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 5.0e{-2}$.
  - The mass-conservation judgment is applied. The evaluation expression is `mass_drift_rel`, and the threshold is $\le 1.0e{-10}$.
  - The momentum-conservation judgment is not applied. The non-application basis is "because the bottom-topography source term exchanges momentum".
  - The initial-topography judgment is applied. The evaluation expression is `errors.initial_zb.linf_rel`, and the threshold is $\le 1.0e{-12}$.
  - The lake-at-rest invariance judgment is applied to both cases. It requires `lake_rest.max_velocity \le 1.0e{-12}` and `lake_rest.max_surface_deviation \le 1.0e{-12}`.
  - The theoretical-comparison judgment is not applied. The non-application basis is "because it is a topography lake-at-rest case".
  - The translation-equivariance and symmetry-pair judgments are not applied. The non-application basis is "because no translation or symmetry pair is run on these cases".
  - The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, and `hv`, and the threshold is $\le 1.0e{-10}$ for each.

### 6-4. `l2_long_run_mass_conservation`
- `level`: `L2`
- `objective`: confirm that mass and depth positivity are maintained in long-time integration of `williamson_tc5_cone + linear_wave_x`, and the transverse momentum the cone generates over it, bounded on both sides.
- target cases:
  - `swe2d_ref_n064_sx000_sy000_dts100_tend500`
- `expected_outcome`: `pass`
- judgment conditions:
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of the case ($|\mathrm{run.n\_step}-\mathrm{n\_step}| \le 0.5$).
  - The depth-positivity judgment is applied. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 5.0e{-2}$.
  - The mass-conservation judgment is applied. The evaluation expression is `mass_drift_rel`, and the threshold is $\le 5.0e{-10}$.
  - The initial-topography judgment is applied. The evaluation expression is `errors.initial_zb.linf_rel`, and the threshold is $\le 1.0e{-12}$.
  - The topographic-response judgment is applied. `transverse_momentum_l2_rel` is judged as the two-sided band $[1.4e{-4},\ 1.8e{-4}]$ of 5-5, as two judged quantities.
  - The momentum-conservation judgment is not applied. The non-application basis is "because the bottom-topography source term exchanges momentum; the response is judged by `transverse_momentum_l2_rel`".
  - The theoretical-comparison judgment is not applied. The non-application basis is "because it is a topography long-time case".
  - The translation-equivariance and symmetry-pair judgments are not applied. The non-application basis is "because no translation or symmetry pair is run on this case".
  - The lake-at-rest invariance judgment is not applied. The non-application basis is "because the initial condition is not `lake_at_rest`".
  - The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, and `hv`, and the threshold is $\le 1.0e{-10}$ for each.

### 6-5. `l3_topography_forced_response_stability`
- `level`: `L3`
- `objective`: confirm the numerical stability under topographic forcing for `williamson_tc5_cone + oblique_mode`, and the momentum the forcing exchanges over the run, bounded on both sides.
- target cases:
  - `swe2d_resp_n064_sx000_sy000_dts100`
- `expected_outcome`: `pass`
- judgment conditions:
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of the case ($|\mathrm{run.n\_step}-\mathrm{n\_step}| \le 0.5$).
  - The depth-positivity judgment is applied. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 5.0e{-2}$.
  - The mass-conservation judgment is applied. The evaluation expression is `mass_drift_rel`, and the threshold is $\le 1.0e{-10}$.
  - The initial-topography judgment is applied. The evaluation expression is `errors.initial_zb.linf_rel`, and the threshold is $\le 1.0e{-12}$.
  - The momentum-exchange judgment is applied. The evaluation expressions are `momx_drift_rel` and `momy_drift_rel`, each judged as the two-sided band $[1.3e{-6},\ 1.7e{-6}]$ of 5-5, as two judged quantities each.
  - The theoretical-comparison judgment is not applied. The non-application basis is "because the theoretical solution is not used in a case with topography".
  - The translation-equivariance and symmetry-pair judgments are not applied. The non-application basis is "because no translation or symmetry pair is run on this case; the equivariances over the topography are judged on the cone pairs of 6-11 to 6-13".
  - The lake-at-rest invariance judgment is not applied. The non-application basis is "because the initial condition is not `lake_at_rest`".
  - The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, and `hv`, and the threshold is $\le 1.0e{-10}$ for each.

### 6-6. `l3_translation_equivariance`
- `level`: `L3`
- `objective`: confirm that the numerical solution is equivariant to the translation of the `flat + oblique_mode` initial condition.
- pair cases:
  - `reference` is `swe2d_flat_sym_n064_sx000_sy000_dts100`
  - `shifted` is `swe2d_flat_sym_n064_sx025_sy012_dts100`
- `expected_outcome`: `pass`
- judgment conditions:
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of each case ($|\mathrm{run.n\_step}-\mathrm{n\_step}| \le 0.5$).
  - The depth-positivity judgment is applied. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 5.0e{-2}$.
  - The mass-conservation judgment is applied. The evaluation expression is `mass_drift_rel`, and the threshold is $\le 1.0e{-10}$.
  - The momentum-conservation judgment is applied. The evaluation expressions are `momx_drift_rel` and `momy_drift_rel`, and the threshold for both is $\le 1.0e{-10}$.
  - The translation-equivariance judgment is applied. The evaluation expression is `symmetry_h_l2_rel`, and the threshold is $\le 2.0e{-11}$.
  - The symmetry-pair judgment is not applied. The non-application basis is "because the shifted case's axis-exchange pair is judged in 6-11".
  - The theoretical-comparison judgment is not applied. The non-application basis is "because `oblique_mode` is outside the target of the theoretical-agreement judgment".
  - The lake-at-rest invariance judgment is not applied. The non-application basis is "because the initial condition is not `lake_at_rest`".
  - The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, and `hv`, and the threshold is $\le 1.0e{-10}$ for each.

### 6-7. `l0_cfl_guard_xfail`
- `level`: `L0`
- `objective`: confirm that a `CFL`-violation case can be detected (expected failure).
- target cases:
  - `swe2d_guard_n032_sx000_sy000_dts240`
- `expected_outcome`: `xfail`
- `xfail_condition`: `cfl.max > 1.0`
- `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'cfl'`
- judgment conditions:
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$. `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of the case ($|\mathrm{run.n\_step}-\mathrm{n\_step}| \le 0.5$), and its real value and status are reported: the case performs all of its steps (2-9), and a run whose step loop stops on the wet-domain error of the Controlled Spec §5 reports the count performed and a `fail` (2-9). The `run.n_step` judgment is non-gating for this test: its status is not a condition of `pass_when`, so a `fail` on it does not change this test's outcome, which the `CFL` judgment decides.
  - The depth-positivity judgment is applied and its real `extrema.h.min` value and status are reported against the threshold $\ge 5.0e{-2}$ (it is never `N/A`; the §5-3 rule does not apply to it). It is non-gating for this test: its status is not a condition of `pass_when`, so a `fail` on it does not change this test's outcome.
  - The mass-conservation judgment is not applied. The non-application basis is "because the purpose of the guard test is only the detection of a stability-condition violation".
  - The momentum-conservation judgment is not applied. The non-application basis is "because the purpose of the guard test is only the detection of a stability-condition violation".
  - The theoretical-comparison judgment is not applied. The non-application basis is "because under an unstable condition, the can-continue-execution evaluation is done before the theoretical-agreement judgment".
  - The translation-equivariance and symmetry-pair judgments are not applied. The non-application basis is "because the purpose of the guard test is only the detection of a stability-condition violation".
  - The lake-at-rest invariance judgment is not applied. The non-application basis is "because the initial condition is not `lake_at_rest`".
  - The cross-target judgment is not applied. The non-application basis is "because the state an unstable case leaves at $t_{end}$ is not a result two variants must agree on".

### 6-8. `l1_linear_wave_rectangular_grid`
- `level`: `L1`
- `objective`: confirm, for `flat + linear_wave_x` on the rectangular grid of 2-5, conservation, depth positivity and the error against the theoretical solution at $t_{end}$; each exchange of the two extents, the two domain lengths or the two spacings measured in 2-5 to change `errors.analytic_h.l2_rel_tend` fails a judgment of this test.
- target cases:
  - `swe2d_flat_ref_n064_sx000_sy000_dts100_rect`
- `expected_outcome`: `pass`
- judgment conditions:
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of the case ($|\mathrm{run.n\_step}-\mathrm{n\_step}| \le 0.5$).
  - The depth-positivity judgment is applied. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 5.0e{-2}$.
  - The mass-conservation judgment is applied. The evaluation expression is `mass_drift_rel`, and the threshold is $\le 1.0e{-10}$.
  - The momentum-conservation judgment is applied. The evaluation expressions are `momx_drift_rel` and `momy_drift_rel`, and the threshold for both is $\le 1.0e{-10}$.
  - The theoretical-comparison judgment is applied. Each of the six `errors.analytic_h.<k>_rel_tend` and `errors.analytic_hu.<k>_rel_tend` applies its band of the 5-5 table (rows `swe2d_flat_ref_n064_sx000_sy000_dts100_rect`; the `l2` band of `h` is $[1.1e{-4},\ 1.4e{-4}]$) as two judged quantities, and each of the three `errors.analytic_hv.<k>_rel_tend` the upper bound $\le 1.0e{-10}$. `convergence_order` is not judged: the case is not a member of a refinement pair.
  - The translation-equivariance and symmetry-pair judgments are not applied. The non-application basis is "because the case is judged here as a single case; its transformed case is judged in 6-11".
  - The lake-at-rest invariance judgment is not applied. The non-application basis is "because the initial condition is not `lake_at_rest`".
  - The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, and `hv`, and the threshold is $\le 1.0e{-10}$ for each.

### 6-9. `l1_linear_wave_y_rectangular_grid`
- `level`: `L1`
- `objective`: confirm, for `flat + linear_wave_y` on the rectangular grid of 2-5, conservation, depth positivity and the error against the theoretical solution at $t_{end}$; each substitution confined to the `y` direction that 2-6 states lies outside the band or fails the momentum judgment fails a judgment of this test.
- target cases:
  - `swe2d_flat_ref_n064_sx000_sy000_dts100_recty`
- `expected_outcome`: `pass`
- judgment conditions:
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of the case ($|\mathrm{run.n\_step}-\mathrm{n\_step}| \le 0.5$).
  - The depth-positivity judgment is applied. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 5.0e{-2}$.
  - The mass-conservation judgment is applied. The evaluation expression is `mass_drift_rel`, and the threshold is $\le 1.0e{-10}$.
  - The momentum-conservation judgment is applied. The evaluation expressions are `momx_drift_rel` and `momy_drift_rel`, and the threshold for both is $\le 1.0e{-10}$.
  - The theoretical-comparison judgment is applied. Each of the six `errors.analytic_h.<k>_rel_tend` and `errors.analytic_hv.<k>_rel_tend` applies its band of the 5-5 table (rows `swe2d_flat_ref_n064_sx000_sy000_dts100_recty`; the `l2` band of `h` is $[2.5e{-4},\ 3.2e{-4}]$) as two judged quantities, and each of the three `errors.analytic_hu.<k>_rel_tend` the upper bound $\le 1.0e{-10}$. `convergence_order` is not judged: the case is not a member of a refinement pair.
  - The translation-equivariance and symmetry-pair judgments are not applied. The non-application basis is "because the case is judged here as a single case; its transformed case is judged in 6-13".
  - The lake-at-rest invariance judgment is not applied. The non-application basis is "because the initial condition is not `lake_at_rest`".
  - The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, and `hv`, and the threshold is $\le 1.0e{-10}$ for each.

### 6-10. `l3_topography_time_refinement_pair`
- `level`: `L3`
- `objective`: exclude, through the pair of 2-8, a source term evaluated once per step instead of at each stage state of the Controlled Spec §5 step 5, the one-call step, and a `dt` left at $\mathrm{dt\_raw}$ without step 4 of §3 (the other step-count substitutions of 2-8 are excluded by the `run.n_step` judgment of this test as well); the two reconstructions 2-8 states lie inside the pair's band are judged by 6-1, and the exchange of `dx` and `dy` in the dt rule is fixed by no test (2-5).
- pair cases:
  - `reference` is `swe2d_ref_n064_sx000_sy000_dts100`
  - `halved` is `swe2d_ref_n064_sx000_sy000_dts100_half`
- `expected_outcome`: `pass`
- judgment conditions:
  - The time-refinement judgment is applied. The evaluation expression is `errors.time_pair.hv_l2_rel` (carried by the halved case), and the band is $[7.9e{-6},\ 9.7e{-6}]$ as two judged quantities.
  - The `CFL` judgment is applied to both cases. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of each case ($|\mathrm{run.n\_step}-\mathrm{n\_step}| \le 0.5$).
  - The depth-positivity judgment is applied to both cases. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 5.0e{-2}$.
  - The mass-conservation judgment is applied to both cases. The evaluation expression is `mass_drift_rel`, and the threshold is $\le 1.0e{-10}$.
  - The initial-topography judgment is applied to both cases. The evaluation expression is `errors.initial_zb.linf_rel`, and the threshold is $\le 1.0e{-12}$.
  - The topographic-response judgment is applied to both cases. `transverse_momentum_l2_rel` is judged as the `nx=64` band $[1.6e{-2},\ 2.1e{-2}]$ of 5-5, as two judged quantities.
  - The momentum-conservation judgment is not applied. The non-application basis is "because the bottom-topography source term exchanges momentum; the response is judged by `transverse_momentum_l2_rel`".
  - The theoretical-comparison judgment is not applied. The non-application basis is "because it is a case with topography".
  - The translation-equivariance, restricted-reference and symmetry-pair judgments are not applied. The non-application basis is "because the cases are judged here as a time-refinement pair; the level `nx=64` is judged against the restricted reference in 6-1, and the pairs whose base is `swe2d_ref_n064_sx000_sy000_dts100` in 6-11 to 6-13".
  - The lake-at-rest invariance judgment is not applied. The non-application basis is "because the initial condition is not `lake_at_rest`".
  - The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, and `hv`, and the threshold is $\le 1.0e{-10}$ for each.

### 6-11. `l3_axis_exchange_pairs`
- `level`: `L3`
- `objective`: confirm the axis-exchange equivariance of the Controlled Spec §5 on the rectangular linear wave, the oblique mode and the cone (2-11).
- pair cases (base → transformed):
  - `swe2d_flat_ref_n064_sx000_sy000_dts100_rect` → `swe2d_flat_ref_n064_sx000_sy000_dts100_rectt`
  - `swe2d_flat_sym_n064_sx012_sy025_dts100` → `swe2d_flat_sym_n064_sx025_sy012_dts100`
  - `swe2d_ref_n064_sx000_sy000_dts100` → `swe2d_ref_n064_sx000_sy000_dts100_axis`
- `expected_outcome`: `pass`
- judgment conditions:
  - The symmetry-pair judgment is applied to each transformed case. The evaluation expressions are `errors.axis_exchange.h_l2_rel`, `errors.axis_exchange.hu_l2_rel` and `errors.axis_exchange.hv_l2_rel`, each with the threshold $\le 2.0e{-11}$.
  - The `CFL` judgment is applied to each case. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of each case ($|\mathrm{run.n\_step}-\mathrm{n\_step}| \le 0.5$), which is the base's (2-11).
  - The depth-positivity judgment is applied to each case. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 5.0e{-2}$.
  - The mass-conservation judgment is applied to each case. The evaluation expression is `mass_drift_rel`, and the threshold is $\le 1.0e{-10}$.
  - The momentum-conservation judgment is applied to each `topography_profile=flat` case. The evaluation expressions are `momx_drift_rel` and `momy_drift_rel`, and the threshold for both is $\le 1.0e{-10}$. It is not applied to the `williamson_tc5_cone` cases. The non-application basis is "because the bottom-topography source term exchanges momentum".
  - The initial-topography judgment is applied to each `williamson_tc5_cone` case. The evaluation expression is `errors.initial_zb.linf_rel` against the closed form of 2-2 with the case's own `x_c_fraction` and `y_c_fraction`, and the threshold is $\le 1.0e{-12}$.
  - The theoretical-comparison, restricted-reference, topographic-response, translation-equivariance, lake-at-rest invariance and time-refinement judgments are not applied. The non-application basis is "because each transformed case equals the transformation of its base, whose bands are judged in 6-1, 6-2, 6-8 and 6-9, to the round-off the pair judgment bounds".
  - The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, and `hv`, and the threshold is $\le 1.0e{-10}$ for each.

### 6-12. `l3_x_mirror_pairs`
- `level`: `L3`
- `objective`: confirm the mirror equivariance in `x` of the Controlled Spec §5 on the flat linear wave and the cone (2-11).
- pair cases (base → transformed):
  - `swe2d_flat_ref_n064_sx000_sy000_dts100` → `swe2d_flat_ref_n064_sx000_sy000_dts100_xmir`
  - `swe2d_ref_n064_sx000_sy000_dts100` → `swe2d_ref_n064_sx000_sy000_dts100_xmir`
- `expected_outcome`: `pass`
- judgment conditions:
  - The symmetry-pair judgment is applied to each transformed case. The evaluation expressions are `errors.mirror_x.h_l2_rel`, `errors.mirror_x.hu_l2_rel` and `errors.mirror_x.hv_l2_rel`, each with the threshold $\le 2.0e{-11}$.
  - The `CFL` judgment is applied to each case. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of each case ($|\mathrm{run.n\_step}-\mathrm{n\_step}| \le 0.5$), which is the base's (2-11).
  - The depth-positivity judgment is applied to each case. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 5.0e{-2}$.
  - The mass-conservation judgment is applied to each case. The evaluation expression is `mass_drift_rel`, and the threshold is $\le 1.0e{-10}$.
  - The momentum-conservation judgment is applied to each `topography_profile=flat` case. The evaluation expressions are `momx_drift_rel` and `momy_drift_rel`, and the threshold for both is $\le 1.0e{-10}$. It is not applied to the `williamson_tc5_cone` cases. The non-application basis is "because the bottom-topography source term exchanges momentum".
  - The initial-topography judgment is applied to each `williamson_tc5_cone` case. The evaluation expression is `errors.initial_zb.linf_rel` against the closed form of 2-2 with the case's own `x_c_fraction` and `y_c_fraction`, and the threshold is $\le 1.0e{-12}$.
  - The theoretical-comparison, restricted-reference, topographic-response, translation-equivariance, lake-at-rest invariance and time-refinement judgments are not applied. The non-application basis is "because each transformed case equals the transformation of its base, whose bands are judged in 6-1, 6-2, 6-8 and 6-9, to the round-off the pair judgment bounds".
  - The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, and `hv`, and the threshold is $\le 1.0e{-10}$ for each.

### 6-13. `l3_y_mirror_pairs`
- `level`: `L3`
- `objective`: confirm the mirror equivariance in `y` of the Controlled Spec §5 on the rectangular `y`-direction wave and the cone (2-11).
- pair cases (base → transformed):
  - `swe2d_flat_ref_n064_sx000_sy000_dts100_recty` → `swe2d_flat_ref_n064_sx000_sy000_dts100_rectymir`
  - `swe2d_ref_n064_sx000_sy000_dts100` → `swe2d_ref_n064_sx000_sy000_dts100_ymir`
- `expected_outcome`: `pass`
- judgment conditions:
  - The symmetry-pair judgment is applied to each transformed case. The evaluation expressions are `errors.mirror_y.h_l2_rel`, `errors.mirror_y.hu_l2_rel` and `errors.mirror_y.hv_l2_rel`, each with the threshold $\le 2.0e{-11}$.
  - The `CFL` judgment is applied to each case. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of each case ($|\mathrm{run.n\_step}-\mathrm{n\_step}| \le 0.5$), which is the base's (2-11).
  - The depth-positivity judgment is applied to each case. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 5.0e{-2}$.
  - The mass-conservation judgment is applied to each case. The evaluation expression is `mass_drift_rel`, and the threshold is $\le 1.0e{-10}$.
  - The momentum-conservation judgment is applied to each `topography_profile=flat` case. The evaluation expressions are `momx_drift_rel` and `momy_drift_rel`, and the threshold for both is $\le 1.0e{-10}$. It is not applied to the `williamson_tc5_cone` cases. The non-application basis is "because the bottom-topography source term exchanges momentum".
  - The initial-topography judgment is applied to each `williamson_tc5_cone` case. The evaluation expression is `errors.initial_zb.linf_rel` against the closed form of 2-2 with the case's own `x_c_fraction` and `y_c_fraction`, and the threshold is $\le 1.0e{-12}$.
  - The theoretical-comparison, restricted-reference, topographic-response, translation-equivariance, lake-at-rest invariance and time-refinement judgments are not applied. The non-application basis is "because each transformed case equals the transformation of its base, whose bands are judged in 6-1, 6-2, 6-8 and 6-9, to the round-off the pair judgment bounds".
  - The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, and `hv`, and the threshold is $\le 1.0e{-10}$ for each.

## 7. Pass/fail aggregation rules
- `per_test.pass_rule`: `pass` when all applicable checks are `pass`.
- `per_test.xfail_rule`: `xfail` when `expected_outcome == xfail` and `xfail_condition` is true and `pass_when` is satisfied.
- `suite.pass_rule`: `pass` when all `test_id` satisfy `pass_rule` or `xfail_rule`.

## 8. Traceability
### 8-1. Output requirements
- `verdict.json` requires each check's `status`, `metric_value`, `threshold`, and `reason_na` when `applicable=false`.
- `summary.json` requires the counts of `pass`, `fail`, `xfail`, and `skipped`.

### 8-2. Traceability records
- The `test_profile_id` and `test_profile_version` of this document must be recorded under each case's `inputs` in `spec.ir.yaml` and in `trial_meta.json`.
- The judgment conditions of this document must be mappable to the evaluation basis of `verdict.json`.
