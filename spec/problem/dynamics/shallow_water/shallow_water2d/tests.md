# Tests: 2D shallow water equation (verification input / judgment conditions)

## 0. Meta information
- `status`: `draft`
- `test_profile_id`: `shallow_water2d_baseline`
- `test_profile_version`: `0.9.0`
- `spec_ref.spec_kind`: `problem`
- `spec_ref.spec_id`: `shallow_water2d`
- `spec_ref.spec_version`: `0.5.1`
- `spec_ref.controlled_spec_path`: `spec/problem/dynamics/shallow_water/shallow_water2d/controlled_spec.md`

## 1. Test purpose
This suite verifies, for the discrete implementation of the 2D shallow water equation including bottom topography, conservation, lake-at-rest invariance, stability under topographic forcing, agreement with the theoretical solution — bounded on both sides, since the fully fixed `p0` discretization makes it a property of the scheme — translation equivariance, a rectangular grid, on which an exchange of the two extents, the two domain lengths or the two spacings is not the identity, a linear wave in `y` on that grid, on which a defect confined to the `y` direction is not the identity, the step count of §3, a lake at rest over the cone on the rectangular grid, the topographic response — the transverse momentum a `y`-uniform wave acquires over the cone, the momentum the cone exchanges with the oblique mode, and a time-refinement pair of the forced case — bounded on both sides, a lake at rest over a cone standing on the domain seam, the `CFL` guard and the refusal of the wet-domain invariant. The error against the theoretical solution is judged in three norms over `h`, `hu` and `hv` with the order of each norm, the cone family against the restricted reference of its finest level in three norms over the free surface, `hu` and `hv`, and the three equivariances of the Controlled Spec §5 by symmetry pairs (`docs/TESTS.md` §Refinement judgment, §Symmetry pairs). The result of the step is judged on the refusal of a `component` (2-17). The conservation judgment targets mass for all cases, and momentum for the `topography_profile=flat` cases. The judgment targets are `L0` to `L3`, and include expected failures (`xfail`).

What this suite accepts as beyond a kernel judgment:
- **The `dt` rule has one author.** The initial state and the §3 procedure are host data: the `Compile` leaf transcribes them into each case's `initial_state` and `derived`, and the host evaluates them. No kernel computes a step count to compare with, so the value of the rule is reviewed by `Compile.verify` alone. The host pins its consistency: $t_{start}+\mathrm{n\_step}\cdot dt$ against $t_{end}$ (the `dt<0` case of 2-17 aside), and `run.n_step`, the committed step count against $\mathrm{n\_step}$. A transcription that departs from §3 and stays consistent is outside what a test here can see, and this is accepted.
- **Equivalent work-array extents.** A work array the step allocates larger than it reads changes no captured value, so no test judges its extent.
- **The form of the wet-domain comparison.** The form of the $h^{*}\le 0$ branch of the Controlled Spec §5 is unobservable: the guard of the flux `component` refuses the same depth on the same step, so the two variants of the comparison named in 2-16 reach the same `false` result and no test separates them.
- **The result of the time-update `component` not read.** A kernel that does not read it continues, on the `dt<0` case of 2-17, from a stage state the Controlled Spec §5 leaves undefined, so its result there is not defined, and no test judges it.

## 2. Input-defaulting rules
### 2-1. Basic constants
- The domain lengths are `L_x=L_y=1.0` (the rectangular cases of 2-5, 2-6 and 2-10 carry `L_y=0.5`, `ny=48`).
- The representative water level is `H_0=1.0`.
- For the gravitational acceleration, use the fixed value `g=9.81` of the Controlled Spec.
- The small amplitude is `eta0=1.0e-3`.
- The reference phase speed of a linear gravity wave is $c_0=\sqrt{gH_0}$.
- The inputs of the symmetry pairs are `wave_sign=+1` (2-3) on every `linear_wave_x` and `linear_wave_y` case, and `x_c_fraction=3/4` and `y_c_fraction=2/3` (2-2) on every `williamson_tc5_cone` case, except the transformed cases of 2-11 and the seam case of 2-15, which override them (4-3).

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
Every case of §4 but the refusal cases of 2-17 and the rectangular ones — this one, the `y`-direction case of 2-6, the rectangular topography case of 2-10 and the transformed cases `_rectt` and `_rectymir` of 2-11 — carries $nx=ny$ and `L_x=L_y`, so an implementation that exchanges the two extents, the two domain lengths or the two spacings — an array declared `(ny, nx)`, a loop bounded by `ny` where `nx` is meant, `dx` where `dy` is meant, an `rhs` that reads its stage state as `(ncomp, ny, nx)` — gives the numbers of a correct one on those cases. `swe2d_flat_ref_n064_sx000_sy000_dts100_rect` (4-3) carries `nx=64`, `ny=48`, `L_x=1.0` and `L_y=0.5`, so $dx=1/64$ and $dy=1/96$, and no exchange within any of the three pairs is the identity on a derived quantity. The §3 procedure gives $\mathrm{n\_step}=223$ and $dt=8.969\times10^{-4}$. The wave of `linear_wave_x` is uniform in `y`, so `ny` and `L_y` enter `errors.analytic_h.l2_rel_tend` through `dt` alone: its reference value is `1.2427e-4`, measured on the reference implementation of 5-5, equal to the value of `swe2d_flat_ref_n064_sx000_sy000_dts100` (`1.2428e-4`) to four digits. Measured on the same implementation with one substitution at a time: `L_x` and `L_y` exchanged gives `1.10e-3`; `dx` and `dy` exchanged in the interface-flux difference gives `1.03e-3`; the cell centres evaluated with the spacings exchanged at setup gives `6.45e-4`; an `rhs` that reads the stage state as `(ncomp, ny, nx)` gives `7.07e-4`; an `x` loop bounded by `ny` gives `9.37e-4` (and `1.3e-4` in `mass_drift_rel`). Each lies outside the band of 5-5. `dx` and `dy` exchanged in the dt rule alone gives the reference value and the §3 step count (and does on the `y`-direction case of 2-6): the rule of §3 is symmetric in the two spacings when $|u|,|v|\ll c$, so that exchange changes no judged quantity. The bottom is flat on this case, so the source term of the Controlled Spec §5 step 4 and the topography of 2-2 do not enter it; their exchanges are judged on the case of 2-10.

### 2-6. The `y`-direction case
The wave of `linear_wave_x` is uniform in `y`, so the `y`-interface flux difference $G^{*}_{i,j+1/2}-G^{*}_{i,j-1/2}$ vanishes identically on it, and nothing in the `y` flux, the `y` ghost or the spacing that divides the `y` difference reaches `errors.analytic_h.l2_rel_tend`; the pair of 6-6 shares such a defect between its two runs. `swe2d_flat_ref_n064_sx000_sy000_dts100_recty` (4-3) carries the grid of 2-5 and `initial_profile=linear_wave_y`, the wave of 2-3 turned to `y`, with the reference solution of 2-4. The §3 procedure gives $\mathrm{n\_step}=223$ and $dt=8.969\times10^{-4}$, as for the case of 2-5. Its reference value of `errors.analytic_h.l2_rel_tend` is `2.8482e-4`, measured on the reference implementation of 5-5 (the wave has `48` cells per wavelength and travels `1.25` wavelengths in $t_{end}$, against `64` and `0.63` for the case of 2-5). Measured on the same implementation with one substitution at a time: the `y` flux difference divided by `dx` gives `1.17e-3`; the `y` pressure flux omitted gives `2.65e-3`; the `y` flux omitted gives `1.01e-3`, the value of a state left at its initial value; the one-call step of the Controlled Spec §5 gives `0.78` of the reference; half the steps gives `1.15e-3`. Each lies outside the band of 5-5. A `y` boundary that copies the nearest interior cell instead of the periodic image gives `2.76e-4`, inside the band, and `3.7e-7` in `momy_drift_rel`, which fails the momentum judgment. The `y` flux difference divided by `dx` gives the reference value on every case of §4 that is square or carries the `x` wave; the rectangular `y` waves of 2-11 see it as well (`swe2d_flat_ref_n064_sx000_sy000_dts100_rectt` reads `1.03e-3` in `errors.analytic_h.l2_rel_tend` and `1.6` in `hv` of its axis-exchange residual), and so does the rectangular lake at rest of 2-10 (`0.20` in `lake_rest.max_velocity`). The `y` pressure flux omitted and the `y` flux omitted break the lake-at-rest balance of 6-3 and the topographic response of 2-7 as well (`1.2` and `2.0` in `lake_rest.max_velocity`), and the copied `y` boundary breaks the momentum and translation-equivariance judgments of 6-6 as well. The sign of the transverse momentum flux and the velocity term of the `y` wave speed move the value by less than `0.1%` at the amplitude of 2-1 and are not fixed by this case.

### 2-7. The topographic response
On `williamson_tc5_cone + linear_wave_x` the initial `hv` is zero and the wave is uniform in `y`, so on a flat bottom `hv` stays zero to round-off and without the §5 update it stays zero on any bottom: the `hv` at $t_{end}$ is the momentum the cone generates through the reconstruction of the Controlled Spec §5 step 2 and the source term of its step 4. It is measured by `response.transverse_momentum.l2_rel_tend` (5-4), whose reference values are `1.583e-2`, `1.861e-2` and `1.998e-2` at `nx=32`, `64` and `128` and `1.573e-4` for `swe2d_ref_n064_sx000_sy000_dts100_tend500`, measured on the reference implementation of 5-5, which reproduces the captured state of the certified variant of version `0.4.5` of this node on every case to `3e-15`. The Controlled Spec §5 fixes the reconstruction, and the lake-at-rest invariance of 6-3 does not: three reconstructions that keep exact lake-at-rest balance — the interface elevation taken as the minimum or the average of the two cell elevations instead of their maximum, and the interface state carrying the cell momentum instead of the reconstructed depth times the cell velocity — give `0.59`, `0.79` and `0.82` of the reference at `nx=32` (`0.75`, `0.87` and `0.90` at `nx=64`; `0.86`, `0.93` and `0.95` at `nx=128`) and `1e-15` or less in `lake_rest.max_velocity`. The coarsest case is where a variant of the discretization is visible, and the `nx=32` band of 5-5 excludes all three. Half the steps gives `0.47` to `0.51` of the reference, a state left at its initial value `0`, and a gravitational acceleration halved `0.60` to `0.71`; the source term omitted, its sign flipped, its `y` component omitted, a source from a centred difference of `z_b` times the cell depth or from the cell depth and the interface elevations, and the reconstruction omitted give `3.9` to `2600` times the reference at `nx=32` to `128` and fail 6-3 as well. The one-call step of the Controlled Spec §5 gives `1.03` to `1.11` of the reference at `nx=32` to `128`, inside those bands; it gives `2.9` on the long case and is excluded by the bands of 6-2 and the pair of 2-8 as well. `dt_scale` from `0.5` to `2.0` moves the value by at most `0.04%` at `nx=64` and `0.6%` on the long case.

On `williamson_tc5_cone + oblique_mode` the initial momentum is zero in both directions, so $P^x(t_{end})$ and $P^y(t_{end})$ (5-4) are the time integral of the source term over the run: `momx_drift_rel` and `momy_drift_rel` have the reference values `1.509e-6` and `1.511e-6` on `swe2d_resp_n064_sx000_sy000_dts100`, measured on the same implementation. The interface elevation taken as the minimum gives `0.84` of each; a gravitational acceleration halved `0.63`; a state left at its initial value `0`; the `y` component of the source omitted gives `0` in `momy_drift_rel`, and a source from the cell depth and the interface elevations `0.66` in `momy_drift_rel` (`1.0003` in `momx_drift_rel`, inside the band). `dt_scale` from `0.5` to `2.0` moves them by at most `0.32%`.

### 2-8. Time-refinement pair
The functionals of 2-7 are end-state functionals and move by less than `2%` (`0.9%` in the transverse momentum at `nx=32`, `1.8%` in the momentum exchange of 6-5) when the source term $S_b$ of the Controlled Spec §5 step 4 is evaluated once per step at $U^n$ instead of at each stage state, the error that makes the topographic forcing first order in time; each stays inside its band. The time integration of the forced case is judged by a pair of `williamson_tc5_cone + linear_wave_x` runs at `nx=64` that differ only in `dt_scale` (`1.0` and `0.5`): the difference of their final `hv`, normalised by the initial `hu` of the base case, is `errors.time_pair.hv_l2_rel` (5-4). Its reference value is `8.781e-6`; the pair at `dt_scale` `0.5` and `0.25` gives `2.178e-6`, a ratio of `4.0`, the second order of the §5 step 5 composition. Measured on the reference implementation of 5-5 with one substitution at a time: $S_b$ evaluated at $U^n$ for the second call gives `1.17e-4`; the one-call step `6.26e-4`; `dt` left at $\mathrm{dt\_raw}$ without step 4 of §3 (the run ends past $t_{end}$) `1.28e-4`; half the steps `1.07e-4`; a state left at its initial value `0`; a gravitational acceleration halved `4.29e-6`; the interface elevation taken as the minimum `7.74e-6`; $\lambda_0$ from one direction `3.56e-5` and `cfl_target=0.5` `1.09e-5`. Each lies outside the band of 5-5. The interface elevation taken as the average (`8.25e-6`) and the interface state carrying the cell momentum (`7.94e-6`) lie inside it and are excluded by the `nx=32` band of `transverse_momentum_l2_rel` (5-5, 2-7). The pair does not fix the exchange of `dx` and `dy` in the dt rule (2-5).

### 2-9. The `CFL` guard case
`swe2d_guard_n032_sx000_sy000_dts240` carries `dt_scale=2.40`, so the §3 procedure gives $\mathrm{n\_step}=38$ and $dt=5.263\times10^{-3}$, and the stability index of the Controlled Spec §5 is `1.056` at the initial state and at its maximum over the run, and `1.0557` at the final committed state (step `38`, which is also the case's second `state_history` sample, §3) — above the threshold `1.0` of the `CFL` judgment, which is what 6-7 detects over the committed state (5-4). The case performs all of its §3 steps and stays wet: the depth minimum over the initial state and every committed state is the initial minimum, `0.999005`, against the positivity threshold `5.0e-2` of 5-5, measured on the reference implementation of 5-5. The `false` result of the Controlled Spec §5 is not reached by this case (2-16 reaches it). A run of this case whose step returned `false` commits fewer steps: its `final.step` is the count of committed steps (§3), and 6-7 does not judge it. The step count and the depth minimum stated here are properties of the fixed discretization at the case's inputs; 6-7 judges the `CFL` alone.

### 2-10. The rectangular topography case and the initial topography
Every `williamson_tc5_cone` case of §4 but one is square, and on a square grid an exchange of `dx` and `dy` in the source term of the Controlled Spec §5 step 4, or of `L_x` and `L_y` in the topography of 2-2, is the identity. `swe2d_lake_n064_sx000_sy000_dts100_rect` (4-3) carries the grid of 2-5 and `williamson_tc5_cone + lake_at_rest`. The §3 procedure gives $\mathrm{n\_step}=223$ and $dt=8.969\times10^{-4}$; on the reference implementation of 5-5 the case gives `5e-16` in `lake_rest.max_velocity`, `0` in `lake_rest.max_surface_deviation`, `0.4494` in `cfl.max` and `0.8225` in `extrema.h.min`. Measured on the same implementation with one substitution at a time: the `y` component of the source term divided by `dx` instead of `dy` gives `0.14` in `lake_rest.max_velocity`, and the two spacings exchanged in the source term `0.35`; both are the identity on every other case of §4.

The topography of 2-2 is host data: the host evaluates it from the case's inputs as part of the case's initial state (2-14), the kernel reads it, and no test judges it.

### 2-11. Symmetry pairs
The Controlled Spec §5 states three equivariances, and each is judged by pairs of cases (`docs/TESTS.md` §Symmetry pairs): a base case and its transformed case of 4-3, which differs from the base only in the inputs 4-3 names and carries the metric.
- **Axis exchange** (6-11): the transformed case has `nx` ↔ `ny`, `L_x` ↔ `L_y`, `x_c_fraction` ↔ `y_c_fraction` and the initial profile turned to the other axis (`linear_wave_x` ↔ `linear_wave_y`, `shift_x_fraction` ↔ `shift_y_fraction`), and its final state is $(h,\ hu,\ hv)'=(h^T,\ hv^T,\ hu^T)$ of the base's, with $^T$ the exchange of the two array axes. Three pairs: the rectangular wave of 2-5 and its transpose (`48×64`, `L_x=0.5`, `L_y=1.0`, `linear_wave_y`), on which an exchange of the two extents, the two lengths or the two spacings is not the identity; the oblique mode at $(\mathrm{shift\_x\_fraction},\ \mathrm{shift\_y\_fraction})=(0.125,\ 0.25)$ and $(0.25,\ 0.125)$, which loads the `x` and the `y` flux of every cell at once; and the cone case `swe2d_ref_n064_sx000_sy000_dts100` and its transpose (`linear_wave_y`, `x_c_fraction=2/3`, `y_c_fraction=3/4`), which runs the reconstruction and the source term of the Controlled Spec §5 steps 2 and 4 along `y` where the base runs them along `x`. The oblique mode depends on $x/L_x+y/L_y$ alone, so its two cases share an initial state that the exchange maps onto itself, and the pair judges that the solution keeps that symmetry.
- **Mirror in `x`** (6-12): `wave_sign=-1` and `shift_x_fraction` → $\tfrac12-$`shift_x_fraction` (the reflected wave, $\sin(2\pi(1-x/L_x))=\sin(2\pi(x/L_x-\tfrac12))$, travelling toward decreasing `x`), and on the cone case `x_c_fraction=1/4`; the final state is $(h,\ -hu,\ hv)$ of the base's reflected along `x` (the first array axis reversed: the grammar's `flip(·, 0)`). Two pairs: the flat wave `swe2d_flat_ref_n064_sx000_sy000_dts100` and the cone case `swe2d_ref_n064_sx000_sy000_dts100`.
- **Mirror in `y`** (6-13): the same along `y` (the second array axis reversed, `flip(·, 1)`, and `hv` sign-reversed). Two pairs: the rectangular `y`-direction wave of 2-6 with `wave_sign=-1` and `shift_y_fraction=0.5`, and the cone case with `y_c_fraction=1/3` (its wave is uniform in `y`, so only the cone moves).

The initial states of a pair are not compared with each other: the transformed case's topography and initial state are host data evaluated from its own inputs (2-2, 2-3), and an initial difference would also appear in the final-state residual. Each pair is judged on every state variable by an `l2` residual (5-4), normalised by the `l2` norm of the base's variable (a momentum component by the larger `l2` norm of the base's two momentum components), with the bound `2.0e-11`: the transformed case runs the same operations on transformed data, so the residual of a conforming kernel is round-off, but not zero. On the reference implementation of 5-5 the three axis-exchange pairs read `0`, and the mirror pairs read at most `8.7e-13` (`hv` of the mirror in `y` of the rectangular wave; `1.5e-13` in `hu` of the flat mirror in `x`, `7.4e-14` on the cone pairs): the reflected initial wave equals the reflection of the base's only to the rounding of the argument of its sine (`2.5e-16` in `hu` at $t=0$), and the pressure term $gh^2/2$ of the momentum flux, three orders of magnitude larger than the momentum of the wave, carries that rounding through the run. The transformed case performs the same `n_step` as its base (`223`, `179`, `179` for the axis exchange, in the order of 6-11; `179`, `179` for the mirror in `x`, 6-12; `223`, `179` for the mirror in `y`, 6-13).

What a pair sees that no band of this suite sees, measured on the reference implementation of 5-5 with one defect at a time, written into the kernel and so present in both runs of the pair: the sign of the transverse momentum flux reversed in the flux of one direction (`hv`$\cdot u$ in the `x` flux, or `hu`$\cdot v$ in the `y` flux) reads `2.2e-3` in `hu` and `hv` on the oblique-mode axis pair and `5.2e-5` in `hu` on the cone axis pair, and moves no judged quantity of the other tests by more than `0.2%` (the momentum exchange of 6-5 by `0.13%`, the time pair of 2-8 by `0.1%`, the transverse momentum of 2-7 by `0.03%`, the `y`-direction case of 2-6 not at all); the wave speed of the Rusanov flux taken from the left state alone ($|u_L|+c_L$ instead of the larger of the two sides) along `x` reads `1.9e-5` in `hu` on the flat mirror in `x` and `1.3e-5` in `hv` on the rectangular axis pair, and along `y` `3.2e-5` in `hv` on the mirror in `y` of the rectangular wave; it changes no theoretical-comparison error in its fourth digit and conserves mass and momentum. Each is at least five orders of magnitude above the bound. A kernel that maps a transformed case onto its base internally and transforms the result back reads `0` on every pair; the Controlled Spec §7 forbids that mapping.

### 2-12. Three norms and orders of the linear-wave family
The error of the three cases of the `swe2d_flat_ref` sweep (4-1) and of the rectangular cases of 2-5 and 2-6 against the theoretical solution of 2-4 is judged in the `l1`, `l2` and `linf` norms over `h`, `hu` and `hv` (`docs/TESTS.md` §Refinement judgment), and the order of each norm of `h` and of `hu` over each pair of adjacent levels of `swe2d_flat_ref`. The normalisers (5-4): `h` and the momentum component along which the wave travels (`hu` for `linear_wave_x`, `hv` for `linear_wave_y`) by the same norm of their own reference; the other momentum component, whose reference is zero, by the larger of that norm over the two momentum references. A wave uniform across its direction keeps the cross-wave momentum at zero up to round-off — every interface across the wave sees two equal states, and the transverse momentum flux carries the zero cross-wave velocity — so that component's error is judged by an upper bound alone, `1.0e-10` in each norm (the reference implementation reads `0`). The reference values (5-5) are measured on the reference implementation of 5-5. The expected order is one (the `p0` scheme); every norm of `h` and of the along-wave momentum reads `0.87` over the first pair and `0.93` over the second, approaching one from below. The error of the along-wave momentum is about `1400` times that of `h`: `h` is normalised by its reference, whose mean $H_0$ is a thousand times the amplitude of the wave, and the momentum reference is the wave alone.

### 2-13. The restricted reference of the cone family
`williamson_tc5_cone + linear_wave_x` has no analytic solution at $t_{end}$. Its family `swe2d_ref` (4-1) carries four levels, `nx=ny=32`, `64`, `128`, `256`, and the finest level is the restricted reference (`docs/TESTS.md` §Refinement judgment): its final free surface $\eta=h+z_b$, `hu` and `hv` averaged over the $f\times f$ blocks of cells that cover each coarser cell ($f=256/n$; the grammar function `coarsen`). The free surface is compared instead of `h` because `z_b` is evaluated at each grid's own cell centres (2-2): the block mean of the finest `z_b` differs from a coarser grid's `z_b` by up to `4.0e-3` at `nx=32` (`2.2e-3` at `64`, `1.1e-3` at `128`; $h_s=0.2$), which `h` carries and $\eta$ does not. The three coarser levels are judged against it in three norms over $\eta$, `hu` and `hv` (`hv`, the momentum the cone generates, by the larger of that norm over the two restricted momentum components), and the order of each norm and variable over the two pairs `32→64` and `64→128`; the finest level judges no error of its own. The expected order is one (the `p0` scheme), and against a reference only twice finer the pair $(n, 2n)$ reads about $p+\log_2\frac{1-(n/N)^p}{1-(2n/N)^p}$ (`1.22` and `1.58` for $p=1$, $N=256$): $\eta$ and `hu` read `1.07` to `1.10` and `1.49` to `1.51`. `hv` reads lower, `0.73` to `0.85` and `1.21` to `1.28`, because the coarse levels are not in its asymptotic range: its error at `nx=32` is a third of its own size (`0.34` in the `l2` norm against its own restricted reference), and a smooth Gaussian topography of the same height reads the same (`0.58` to `0.75` and `1.06` to `1.23`), so the slope discontinuities of the cone are not the reason. The finest level also carries the transverse-momentum response of 2-7: `2.074e-2` at `nx=256` (714 steps).

### 2-14. The state before the first call
The driver holds the arrays `h`, `hu`, `hv` and `z_b` (`nx` × `ny` each) and the scalar `ok`. Before the first call `z_b` holds the closed form of 2-2 and `h`, `hu`, `hv` the closed forms of 2-3 over it, at the cell centres $x=(i+\tfrac12)dx$, $y=(j+\tfrac12)dy$, $i=0,\dots,nx-1$, $j=0,\dots,ny-1$; the host evaluates each from the case's inputs. No array is an operation-only output. `ok` is the logical result of `shallow_water2d__step` under its §5.1 name: it starts at `0`, and the driver writes the result into it after each call, `1` for true and `0` for false.

The arguments of `shallow_water2d__step` bind as follows: `nx`, `ny` and `g` to the case inputs, `dx`, `dy` and `dt` to the `derived` values of §3, `t` to the driver's time, and `z_b`, `h`, `hu`, `hv`, `ok` to the state of those names. `h`, `hu` and `hv` are `inout`, each bound to one state.

### 2-15. The seam case
On every other `williamson_tc5_cone` case `z_b` is `0` on the cells along the domain edges, so the periodic image of `z_b` in the ghost layer (Controlled Spec §5) reads `0` whichever cell it copies. `swe2d_lake_n064_sx000_sy000_dts100_edge` (4-3) carries `williamson_tc5_cone + lake_at_rest` with `x_c_fraction=0.01` and `y_c_fraction=0.02`: the cone stands near the corner of the domain, its image crosses the seam in both directions, and it stands off the seam, so the two cells a seam joins hold different `z_b` (the largest `z_b` along the east, west, north and south edges is `0.1782`, `0.1951`, `0.1665` and `0.1851`). A cone centred on the seam holds equal values there, and a ghost copied from the nearest interior cell is then the periodic image. The §3 procedure gives $\mathrm{n\_step}=179$ and $dt=1.117\times10^{-3}$, and 6-3 judges the final state. Measured on the kernel of the certified variant of version `0.4.7` of this node cut to one step: `5.5e-16` in `lake_rest.max_velocity` and `0` in `lake_rest.max_surface_deviation`. Measured on the same kernel with one substitution at a time, each leaving the other two lake cases at their round-off: the east ghost column copied from the nearest interior cell gives `0.049` and `5.9e-3`; the north ghost row the same way gives `0.064` and `7.3e-3`; the east ghost column left at `0`, by a write one column past the halo, gives `0.79` and `0.094`, and a bounds-checked build stops on that write. That kernel reconstructs each cell's east and north faces and never reads its west or south ghost of `z_b`, so the same substitution there changes no captured value on it.

### 2-16. The wet-domain refusal case
Measured at `nx=32` and `dt_scale=1`: `linear_wave_x` over the cone with `eta0` from `0.72` to `0.795`, either `wave_sign` and twelve values of `shift_x_fraction`, completes every step, because the wave fills its own trough, so a stable case does not reach the failure of the invariant. `swe2d_dry_n032_sx000_sy000_dts500` carries `williamson_tc5_cone + linear_wave_x` and `dt_scale=5.00`, so the §3 procedure gives $\mathrm{n\_step}=18$ and $dt=1.111\times10^{-2}$, and the stability index of the initial state is `2.23`. The state satisfies the wet condition of the Controlled Spec §6 at $t=0$; the run is unstable over the cone until a reconstructed depth reaches $h^{*}\le 0$. Measured on the kernel of the certified variant of version `0.4.7` of this node cut to one step: the step returns `false` after `8` committed steps, and the minimum of `h` over the committed states is `0.732`. The step at which the result comes is a property of that build and is not judged; 6-14 judges the result and that the state moved. The driver ends the loop on a `false` result, so `ok == 0` leaves fewer committed steps than $\mathrm{n\_step}$.

Measured on the same kernel: the comparison $h^{*}\le 0$ written $h^{*}<0$, and the four faces joined by `and` instead of `or`, each return `false` on the same step, because $h^{*}$ is `max(0, ·)` and a zero depth then reaches the guard of the flux `component`, which refuses it; the flux guard ignored alone also returns `false` on the same step, through the comparison. With the comparison written $h^{*}<0$ and the flux guard ignored together, the run completes all `18` steps, and so it does with a `false` result of the right-hand side ignored in the step; a kernel that refuses its first step leaves the state at its initial value. 6-14 fails on each of the three.

### 2-17. The refusal cases
The Controlled Spec §5 makes `ok` false when a guard of a dependency `component` is false. A refusal case carries one input that a `component` refuses and is otherwise valid. Only `xfail` tests target it, so the host admits an invalid `dt` on it (`docs/workflow/phases/phase_01_compile.md`, the host's evaluation of a case). The driver ends the loop on the first false result, so a conforming kernel leaves `0` committed steps. The judgment of a refusal case is the flag alone, and its `history.every` is `0`.

| case | input | $\lambda_0$ | $\mathrm{dt\_raw}$ | $\mathrm{n\_step}$ | $dt$ | refused by |
| --- | --- | --- | --- | --- | --- | --- |
| `swe2d_flat_ref_n032_sx000_sy000_dts100_dtneg` | $\mathrm{dt\_scale}=-1.0,\ t_{end}=-0.2$ | $2.0065e{+2}$ | $-2.2427e{-3}$ | $90$ | $-2.2222e{-3}$ | the time-update `component` |
| `swe2d_flat_ref_n032_sx000_sy000_dts100_nx001` | $nx=1$ | $1.0336e{+2}$ | $4.3538e{-3}$ | $46$ | $4.3478e{-3}$ | the boundary `component` |
| `swe2d_flat_ref_n032_sx000_sy000_dts100_ny001` | $ny=1$ | $1.0336e{+2}$ | $4.3538e{-3}$ | $46$ | $4.3478e{-3}$ | the boundary `component` |

The host refuses a reduction over an array with an axis of extent `1`, so on the `nx=1` and `ny=1` cases step 1 of the procedure of §3 is $\lambda_0=\sqrt{g H_0}\,(1/dx+1/dy)$, with no maximum. `t_end` is negative on the `dt<0` case because a negative $\mathrm{dt\_raw}$ and a positive `t_end` give $\mathrm{n\_step}\le 0$, which the host refuses.

Measured on the certified `fortran_cpu` kernel of version `0.5.0` of this node, which returns `false` on all three: the boundary guard's false result returned as true gives `ok` `1` and `46` steps on the `nx=1` and `ny=1` cases, and the time-update guard's false result returned as true gives `ok` `1` and `90` steps on the `dt<0` case. A kernel that does not read the boundary guard continues from ghost cells the Controlled Spec §5 leaves undefined, so its result is not defined, and no test judges it.

## 3. Execution-control rules
- $t_{start}=0.0$, and $t_{end}=0.2$ on every case but those of 4-3 that override `t_end`.
- `dt` and the step count are `derived` values of the case, which the host evaluates in this order: `dx` $=L_x/nx$, `dy` $=L_y/ny$, then `lam0`, `dt_raw`, `n_step` and `dt` by the following procedure.
  1. Evaluate $\lambda_0=\max_{i,j}\left((|u_{i,j}|+\sqrt{gh_{i,j}})/dx+(|v_{i,j}|+\sqrt{gh_{i,j}})/dy\right)$ at the initial time.
  2. $\mathrm{dt\_raw}=\mathrm{dt\_scale}\cdot \mathrm{cfl\_target}/\lambda_0$.
  3. $\mathrm{n\_step}=\lceil (t_{end}-t_{start})/\mathrm{dt\_raw}\rceil$.
  4. $dt=(t_{end}-t_{start})/\mathrm{n\_step}$.
- $\mathrm{cfl\_target}=0.45$.
- The `nx=1` and `ny=1` cases of 2-17 evaluate step 1 of the procedure as that section states it.
- The stop condition is $n=\mathrm{n\_step}$. The host evaluates the initial state (2-14) and $\mathrm{n\_step}$ and $dt$ of this procedure for each case; the host's driver calls `shallow_water2d__step` once per step for $n=1,\dots,\mathrm{n\_step}$ and commits each step whose result `ok` is true. The state the host captures before the first step and after the last consists of `h`, `hu`, `hv`, `z_b` (`nx` × `ny` each) and the logical result `ok` (`0` before the first step), with the driver's time `t` and committed step count `step`; `step` is judged as `run.n_step` (5-4). The driver also writes, per committed step, the minimum and maximum of each state array (`step_series`), and the whole state every `every` committed steps (`state_history`), with `every` the case input `history.every` of the following line: `ceil(n_step / 2)`, so each case is sampled at its mid-run step, and again at its final step where `n_step` is even. A refusal case of 2-17 carries `0` and takes no sample.
- history: every = (history.every) swe2d_ref_n032_sx000_sy000_dts100 = 45, swe2d_ref_n064_sx000_sy000_dts100 = 90, swe2d_ref_n064_sx000_sy000_dts100_axis = 90, swe2d_ref_n064_sx000_sy000_dts100_half = 179, swe2d_ref_n064_sx000_sy000_dts100_tend500 = 2230, swe2d_ref_n064_sx000_sy000_dts100_xmir = 90, swe2d_ref_n064_sx000_sy000_dts100_ymir = 90, swe2d_ref_n128_sx000_sy000_dts100 = 179, swe2d_ref_n256_sx000_sy000_dts100 = 357, swe2d_lake_n064_sx000_sy000_dts100 = 90, swe2d_lake_n064_sx000_sy000_dts100_edge = 90, swe2d_lake_n064_sx000_sy000_dts100_rect = 112, swe2d_resp_n064_sx000_sy000_dts100 = 90, swe2d_flat_ref_n032_sx000_sy000_dts100 = 45, swe2d_flat_ref_n032_sx000_sy000_dts100_dtneg = 0, swe2d_flat_ref_n032_sx000_sy000_dts100_nx001 = 0, swe2d_flat_ref_n032_sx000_sy000_dts100_ny001 = 0, swe2d_flat_ref_n064_sx000_sy000_dts100 = 90, swe2d_flat_ref_n064_sx000_sy000_dts100_rect = 112, swe2d_flat_ref_n064_sx000_sy000_dts100_rectt = 112, swe2d_flat_ref_n064_sx000_sy000_dts100_recty = 112, swe2d_flat_ref_n064_sx000_sy000_dts100_rectymir = 112, swe2d_flat_ref_n064_sx000_sy000_dts100_xmir = 90, swe2d_flat_ref_n128_sx000_sy000_dts100 = 179, swe2d_flat_sym_n064_sx000_sy000_dts100 = 90, swe2d_flat_sym_n064_sx012_sy025_dts100 = 90, swe2d_flat_sym_n064_sx025_sy012_dts100 = 90, swe2d_guard_n032_sx000_sy000_dts240 = 19, swe2d_dry_n032_sx000_sy000_dts500 = 9

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
| `swe2d_dry` | expected_failure_for_the_wet_domain_invariant | $nx=ny=32,\ \mathrm{dt\_scale}=5.00$ | `topography_profile=williamson_tc5_cone`, `x_c_fraction=3/4`, `y_c_fraction=2/3`, `initial_profile=linear_wave_x`, `wave_sign=+1`, `shift_x_fraction=0.0`, `shift_y_fraction=0.0` |

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
- Add the `case_id` `swe2d_lake_n064_sx000_sy000_dts100_edge`.
- The `base_case_id` references `swe2d_lake_n064_sx000_sy000_dts100`, and overrides only `x_c_fraction` to `0.01` and `y_c_fraction` to `0.02` (the seam case of 2-15; the `case_id` keeps its base's and carries the suffix `_edge`, so it sorts after its base case).
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
- Add the `case_id` `swe2d_flat_ref_n032_sx000_sy000_dts100_dtneg`.
- The `base_case_id` references `swe2d_flat_ref_n032_sx000_sy000_dts100`, and overrides only `dt_scale` to `-1.0`, `t_end` to `-0.2` and `history.every` to `0` (a refusal case of 2-17).
- Add the `case_id` `swe2d_flat_ref_n032_sx000_sy000_dts100_nx001`.
- The `base_case_id` references `swe2d_flat_ref_n032_sx000_sy000_dts100`, and overrides only `nx` to `1` and `history.every` to `0` (a refusal case of 2-17, with the `lam0` that section states).
- Add the `case_id` `swe2d_flat_ref_n032_sx000_sy000_dts100_ny001`.
- The `base_case_id` references `swe2d_flat_ref_n032_sx000_sy000_dts100`, and overrides only `ny` to `1` and `history.every` to `0` (a refusal case of 2-17, with the `lam0` that section states).

## 5. Judged quantities
### 5-1. How a quantity is judged
A test is judged by the host's predicates over the captures alone (`docs/TESTS.md` requirement 9). A name below is what a test's `quantities` line lists; a two-sided band is two names, `<name>.lower` and `<name>.upper`, each with its own predicate. The definitions of 5-4 are expressions over the captured state, the captured time and the case inputs, and a value measured on the reference implementation is a calibration, not an input.

### 5-2. Judged names
The names, each a scalar. `<v>` stands for each of `h`, `hu`, `hv`; `<k>` for each of `l1`, `l2`, `linf`; `<pair>` for each of `n032_to_n064` and `n064_to_n128`; `<n>` for each of `n032`, `n064`, `n128`; `<r>` for each of `eta`, `hu`, `hv`.
- `cfl.max`
- `extrema.h.min`
- `mass_drift_rel`, `momx_drift_rel`, `momy_drift_rel`
- `errors.analytic_<v>.<k>_rel_tend` (nine; `errors.analytic_h.l2_rel_tend` among them)
- `convergence.<pair>.<k>_order` (the order of `h`; six)
- `convergence.<pair>.hu_<k>_order` (six)
- `errors.restricted.<n>.<r>_<k>_rel` (twenty-seven)
- `convergence.restricted.<pair>.<r>_<k>_order` (eighteen)
- `errors.axis_exchange.<v>_l2_rel` (three)
- `errors.mirror_x.<v>_l2_rel` (three)
- `errors.mirror_y.<v>_l2_rel` (three)
- `errors.symmetry_h_l2_rel`
- `invariants.lake_rest.max_velocity`
- `invariants.lake_rest.max_surface_deviation`
- `run.n_step`
- `run.state_moved`
- `ok`
- `response.transverse_momentum.l2_rel_tend`
- `errors.time_pair.hv_l2_rel`

### 5-3. Cases of each quantity
- A quantity is evaluated on the target cases of the tests that list it. `N/A` below means it is not defined on that case, so no test judges it there.
- The nine `errors.analytic_*` quantities are `N/A` for anything other than `topography_profile=flat` with `initial_profile=linear_wave_x` or `linear_wave_y`.
- The `convergence.n032_to_n064.*` quantities (the three of `h` and the three of `hu`) are carried by `swe2d_flat_ref_n064_sx000_sy000_dts100` and the `convergence.n064_to_n128.*` quantities by `swe2d_flat_ref_n128_sx000_sy000_dts100`; no other case evaluates them (5-4).
- The `errors.restricted.*` and `convergence.restricted.*` quantities are carried by `swe2d_ref_n256_sx000_sy000_dts100`, which sorts after the three coarser levels of its family and reads their final states; no other case evaluates them (5-4).
- `errors.axis_exchange.*` is carried by each axis-exchange case of 4-3, `errors.mirror_x.*` by each mirror-in-`x` case and `errors.mirror_y.*` by each mirror-in-`y` case, each reading its base's final state; no other case evaluates them.
- `errors.symmetry_h_l2_rel` is `N/A` for anything other than a translation-pair evaluation.
- `momx_drift_rel` and `momy_drift_rel` are computed for every case: judged against the thresholds of 5-5 for `topography_profile=flat` in the tests that apply the momentum-conservation judgment (6-2, 6-6, 6-8, 6-9, and 6-11 to 6-13 on their flat cases), as the two-sided band of 5-5 for `swe2d_resp_n064_sx000_sy000_dts100` (6-5), and not judged for the other `williamson_tc5_cone` cases, where the source term exchanges momentum and the response is judged by `response.transverse_momentum.l2_rel_tend` (6-1, 6-4, 6-10) or the lake-at-rest invariance (6-3).
- `response.transverse_momentum.l2_rel_tend` is `N/A` for anything other than `topography_profile=williamson_tc5_cone` with `initial_profile=linear_wave_x`.
- `errors.time_pair.hv_l2_rel` is `N/A` for anything other than the halved-`dt` case of the time-refinement pair; it is carried by `swe2d_ref_n064_sx000_sy000_dts100_half`, which sorts after its base case, and reads the base case's initial and final state.
- `invariants.lake_rest.*` is `N/A` for anything other than `initial_profile=lake_at_rest`.
- `run.n_step` is judged on every target case of every `pass` test.
- `run.state_moved` is judged on `swe2d_dry_n032_sx000_sy000_dts500` alone (6-14), and `ok` on that case and on the refusal cases of 6-15 and 6-16.

### 5-4. Definition of the quantities
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
with the normaliser $N_{q,k}$: for `h` and for the momentum component along the wave (`hu` for `linear_wave_x`, `hv` for `linear_wave_y`), $\|q_{ref}\|_k$; for the other momentum component, whose reference is zero, $\max(\|hu_{ref}\|_k, \|hv_{ref}\|_k)$. They are named `errors.analytic_<v>.<k>_rel_tend`; the `l2` error of `h`,
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

`convergence_order` is a cross-case reduction, using $p=\log(e_{coarse}/e_{fine})/\log(2)$, over the error of each norm of `h` and of `hu` (`errors.analytic_h.<k>_rel_tend`, `errors.analytic_hu.<k>_rel_tend`) between the cases `swe2d_flat_ref_n032_sx000_sy000_dts100` and `swe2d_flat_ref_n064_sx000_sy000_dts100` (the pair `n032_to_n064`) and between `swe2d_flat_ref_n064_sx000_sy000_dts100` and `swe2d_flat_ref_n128_sx000_sy000_dts100` (the pair `n064_to_n128`). It is evaluated at the finer case of each pair: `convergence.<pair>.<k>_order` for `h` (`convergence.<pair>.l2_order` is the order of `analytic_h_l2_rel`) and `convergence.<pair>.hu_<k>_order` for `hu`. The cases preceding the one that completes a reduction do not evaluate them, and so do the override cases of 4-3, which are not members of a refinement pair. The cross-wave momentum `hv` of the family is zero up to round-off (2-12) and has no order.

The restricted-reference errors of the cone family are defined by the following, with $q^{(n)}$ the final state of `swe2d_ref_n<n>_sx000_sy000_dts100` for $n=32, 64, 128$ and $q^{(256)}$ that of `swe2d_ref_n256_sx000_sy000_dts100`, $q$ one of the free surface $\eta=h+z_b$ (with each level's own captured `z_b`), `hu` and `hv`, and $\mathrm{coarsen}(q^{(256)}, 256/n)$ the mean of $q^{(256)}$ over the $(256/n)\times(256/n)$ blocks of cells that cover each cell of the level $n$ (the cell $(i, j)$ of level $n$ is covered by the cells $((i-1)f+1..if,\ (j-1)f+1..jf)$ of level `256`, $f=256/n$):
$$
\mathrm{restricted}^{(n)}_{q,k}=\frac{\|q^{(n)}-\mathrm{coarsen}(q^{(256)},256/n)\|_k}{N^{(n)}_{q,k}}
$$
with $N^{(n)}_{q,k}=\|\mathrm{coarsen}(q^{(256)},f)\|_k$ for $\eta$ and `hu`, and the maximum of that norm over the two restricted momentum components for `hv` (2-13). They are evaluated on `swe2d_ref_n256_sx000_sy000_dts100` as `errors.restricted.<n>.<r>_<k>_rel` with `<n>` the zero-padded level (`n032`, `n064`, `n128`) and `<r>` one of `eta`, `hu`, `hv` (5-2), and the orders $\log_2(\mathrm{restricted}^{(n)}/\mathrm{restricted}^{(2n)})$ as `convergence.restricted.<pair>.<r>_<k>_order`.

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
They are named `errors.axis_exchange.{h,hu,hv}_l2_rel`, `errors.mirror_x.{h,hu,hv}_l2_rel` and `errors.mirror_y.{h,hu,hv}_l2_rel` on the transformed case.

The velocity metric of lake-at-rest invariance is defined by the following.
$$
\mathrm{lake\_rest\_max\_velocity}=
\max_{i,j}\left(\sqrt{u_{i,j}^2+v_{i,j}^2}\right)
$$
with $u$, $v$ at $t_{end}$; it is named `invariants.lake_rest.max_velocity`.

The surface-deviation metric of lake-at-rest invariance is defined by the following.
$$
\mathrm{lake\_rest\_max\_surface\_deviation}=
\max_{i,j}\left|\eta_{i,j}(t_{end})-\eta_{i,j}(0)\right|,
\quad \eta=h+z_b
$$

`run.n_step` is the driver's committed step count `step` after the run (§3), judged in the fixed form `final.step - derived.n_step == 0` against the $\mathrm{n\_step}$ of the §3 procedure the host evaluates from the case's inputs, $\lceil (t_{end}-t_{start})/\mathrm{dt\_raw}\rceil$ with $\mathrm{dt\_raw}$ from the initial state.

The transverse-momentum response (`williamson_tc5_cone + linear_wave_x` only) is defined by the following, over all cells, and named `response.transverse_momentum.l2_rel_tend`.
$$
\mathrm{transverse\_momentum\_l2\_rel}=\frac{\|hv(t_{end})\|_2}{\|hu(0)\|_2}
$$

The time-refinement difference is defined by the following, with $hv_{base}$ and $hu_{base}(0)$ the final `hv` and the initial `hu` of `swe2d_ref_n064_sx000_sy000_dts100` and $hv_{half}$ the final `hv` of `swe2d_ref_n064_sx000_sy000_dts100_half`; it is named `errors.time_pair.hv_l2_rel` on the latter.
$$
\mathrm{time\_pair\_hv\_l2\_rel}=\frac{\|hv_{half}(t_{end})-hv_{base}(t_{end})\|_2}{\|hu_{base}(0)\|_2}
$$

`ok` is the logical result of `shallow_water2d__step` as the driver captured it (2-14), `final.ok`.

`run.state_moved` is the largest change of `h` over the run, $\max_{i,j}|h_{i,j}(\text{final})-h_{i,j}(0)|$, `maxabs(final.h - initial.h)`: it is above `0` only when at least one step was committed.

`cfl.max` is judged by two predicates under that one name, both the host's over the committed state: the stability index of the Controlled Spec §5 with the case's `dt`, as `derived.dt * max(...)` over the final capture and as `derived.dt * max(smax(...))` over the `state_history` samples (every `every` committed steps, §3). `extrema.h.min` is judged by two predicates as well: the smaller of the minima of `h` at the two captures, and `min(series.h_min)`, the minimum of `h` over every committed state of the run (the driver's `step_series`). The kernel reports no run-wide value. A committed state between two `state_history` samples is not seen by `cfl.max` except at the final capture.

`cross_target_state_agreement` compares this target's numerical solution with every other target's certified variant of this node, case by case (`docs/TESTS.md` §Cross-target judgment). For each of the state variables `h`, `hu`, and `hv` at $t_{end}$ of a case, with $q$ that variable and $q^{ref}$ the same variable at $t_{end}$ of the same case in the other variant,
$$
\mathrm{cross\_target\_state\_agreement}=\frac{\max_{i,j} |q_{i,j}-q^{ref}_{i,j}|}{N_q}
$$
The normaliser of `h` is $N_h=\max(\max_{i,j}|h_{i,j}|,\ \max_{i,j}|h^{ref}_{i,j}|,\ 1)$. The two momentum components share one normaliser, the momentum scale of the two variants, $N_{hu}=N_{hv}=\max(\max_{i,j}|hu_{i,j}|,\ \max_{i,j}|hv_{i,j}|,\ \max_{i,j}|hu^{ref}_{i,j}|,\ \max_{i,j}|hv^{ref}_{i,j}|,\ 1)$: a component that is a small residue of the flow (the cross-stream `hv` of a zonal jet) carries the rounding of the whole momentum field, so its own maximum is not its rounding scale. It is evaluated by the host from the captured state, against each other variant separately and for each variable separately; on a target with no other certified variant it holds vacuously.

### 5-5. Default thresholds
The thresholds of `analytic_h_l2_rel`, `transverse_momentum_l2_rel`, the momentum exchange of 6-5 and `time_pair_hv_l2_rel` are calibrated against independent reference implementations of the Controlled Spec §5 scheme, and every measurement this document attributes to "the reference implementation of 5-5" was taken on one of them: the bands of the three `swe2d_flat_ref` sweep cases and of `swe2d_flat_ref_n064_sx000_sy000_dts100_rect`, and 2-5, on the implementation recorded on issue #491 (calibration comment), which reproduces the values of the certified variants of versions `0.4.1` to `0.4.3` of this node to five digits; 2-6 to 2-10 and the bands of `transverse_momentum_l2_rel`, the momentum exchange, `time_pair_hv_l2_rel` and the `y`-direction case on the implementation recorded on issues #494 and #495 (calibration comments), which reproduces the captured state of the certified variant of version `0.4.5` of this node on every case to `3e-15`; 2-11 to 2-13, the three-norm bands and orders, the restricted-reference bands and orders, the `nx=256` band of `transverse_momentum_l2_rel` and the symmetry pairs on the same implementation with the inputs of 2-1 to 2-3 added, recorded on issue #513 (calibration comment). Every band introduced in this version follows the rule of the `analytic_h_l2_rel` bands below — `0.9` and `1.1` times the reference value, rounded outward to two digits, the value being a property of the fixed discretization that moves by round-off alone — and every order bound is the reference order minus `0.3`, rounded down to one decimal (`0.4` where the reference order is below `0.7`), except the `l2` order of `h`, `0.80`, kept from the previous version.

- $\mathrm{cfl.max} \le 1.0$, and `run.n_step` equals the §3 $\mathrm{n\_step}$ of the case (judged as `final.step - derived.n_step == 0`)
- $h_{min} \ge 5.0e{-2}$
- `ok` $=0$ and `run.state_moved` $>0$ on the case of 2-16 (6-14)
- `ok` $=0$ on the refusal cases of 6-15 and 6-16
- $\mathrm{mass\_drift\_rel} \le 1.0e{-10}$
- $\mathrm{momx\_drift\_rel} \le 1.0e{-10}$ and $\mathrm{momy\_drift\_rel} \le 1.0e{-10}$ for the `topography_profile=flat` cases. For `swe2d_resp_n064_sx000_sy000_dts100` (6-5) each is judged as the two-sided band $[1.3e{-6},\ 1.7e{-6}]$ (references `1.509e-6` and `1.511e-6`, 2-7; `0.9` and `1.1` times them, rounded outward to two digits): the two momenta are zero at $t=0$ and their value at $t_{end}$ is the integral of the source term of the Controlled Spec §5 over the run, a property of the fixed discretization — a value below the band is a run that did not perform the §5 update or a source term weaker than §5's, and a value above it a stronger one. Each bound is a DISTINCT judged quantity of the test with its own host predicate over the captured state, as for `analytic_h_l2_rel`.
- `analytic_h_l2_rel` is judged as a two-sided band (6-2, 6-8, 6-9). The Controlled Spec §5 fixes every term of the discretization (`p0` interface states, the Rusanov flux, the periodic ghost, the two-stage `SSPRK2` composition of its step 5) and §3 fixes `dt`, so the error against the linear-wave solution at $t_{end}$ is a property of the discretization — its truncation error and numerical diffusion — and not a free quantity: a value below the band is not a better scheme, it is a run that did not perform the §5 update (a state that holds the theoretical solution at $t_{end}$ without the update has zero error and passes an upper bound alone), and a value above it is a state left at its initial value (`1.304e-3` on every `linear_wave_x` case of `swe2d_flat_ref`, `1.009e-3` on the `y`-direction case of 2-6), a stopped update (half the steps gives `1.13e-3` at `nx=64`) or a wrong one. The value moves by round-off alone under a conforming implementation — the certified variants of versions `0.4.1` to `0.4.3` of this node agree to `3e-12` relative, and `dt_scale` from `0.5` to `2.0` moves it by at most `0.12%` — so the bounds are `0.9` and `1.1` times the reference value, rounded outward to two digits. The lower bound and the upper bound are two DISTINCT judged quantities of the test — `<name>.lower` and `<name>.upper`, each with its own host predicate over the captured state — not two thresholds on one quantity: a state that holds the transverse momentum at zero falls below the lower bound. For `swe2d_flat_ref` the band is $[2.0e{-4},\ 2.5e{-4}]$ for `nx=32` (reference `2.2671e-4`), $[1.1e{-4},\ 1.4e{-4}]$ for `nx=64` (reference `1.2428e-4`), and $[5.8e{-5},\ 7.2e{-5}]$ for `nx=128` (reference `6.5158e-5`); for the rectangular case `swe2d_flat_ref_n064_sx000_sy000_dts100_rect` it is $[1.1e{-4},\ 1.4e{-4}]$ (reference `1.2427e-4`, 2-5); for the `y`-direction case `swe2d_flat_ref_n064_sx000_sy000_dts100_recty` it is $[2.5e{-4},\ 3.2e{-4}]$ (reference `2.8482e-4`, 2-6). The one-call step $U^{n+1}=U^n+\Delta t\,L(U^n)$ (the forward-Euler step the Controlled Spec §5 step 5 excludes) gives `0.78` to `0.83` of the reference value on every case and fails the lower bound; a gravitational acceleration in error by `5%` moves the value outside the band on `nx=128` in either direction, and one in error by `1%` on no case.
- `transverse_momentum_l2_rel` is judged as a two-sided band (6-1, 6-4, 6-10). The Controlled Spec §5 fixes the reconstruction and the source term, so the transverse momentum the cone generates from a `y`-uniform wave is a property of the discretization (2-7): a value below the band is a run that did not perform the §5 update or a reconstruction weaker than §5's, and a value above it a stronger one. The bounds are `0.9` and `1.1` times the reference value, rounded outward to two digits: $[1.4e{-2},\ 1.8e{-2}]$ for `nx=32` (reference `1.583e-2`), $[1.6e{-2},\ 2.1e{-2}]$ for `nx=64` (reference `1.861e-2`; the halved case of 2-8 carries the same band, its reference `1.861e-2` equal to its base's to four digits), $[1.7e{-2},\ 2.2e{-2}]$ for `nx=128` (reference `1.998e-2`), $[1.8e{-2},\ 2.3e{-2}]$ for `nx=256` (reference `2.074e-2`, 2-13), and $[1.4e{-4},\ 1.8e{-4}]$ for `swe2d_ref_n064_sx000_sy000_dts100_tend500` (reference `1.573e-4`). The lower bound and the upper bound are two DISTINCT judged quantities of the test, each with its own host predicate over the captured state, as for `analytic_h_l2_rel`.
- `time_pair_hv_l2_rel` is judged as a two-sided band $[7.9e{-6},\ 9.7e{-6}]$ on `swe2d_ref_n064_sx000_sy000_dts100_half` (reference `8.781e-6`, 2-8; `0.9` and `1.1` times it, rounded outward to two digits), as two distinct judged quantities, each with its own host predicate over the captured states of the two cases.
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

- `convergence_order` uses $p=\log(e_{coarse}/e_{fine})/\log(2)$ over each error of `h` and `hu` of the three cases of `swe2d_flat_ref` that 6-2 judges, and is evaluated at the finer case of each pair (5-4); the override cases of 4-3 are not members of a refinement pair and do not evaluate them. It requires the following lower bounds (reference orders in parentheses):

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
- `invariants.lake_rest.max_velocity \le 1.0e{-12}`
- `invariants.lake_rest.max_surface_deviation \le 1.0e{-12}`
- `errors.symmetry_h_l2_rel \le 2.0e{-11}`
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
- `quantities`: `cfl.max`, `convergence.restricted.n032_to_n064.eta_l1_order`, `convergence.restricted.n032_to_n064.eta_l2_order`, `convergence.restricted.n032_to_n064.eta_linf_order`, `convergence.restricted.n032_to_n064.hu_l1_order`, `convergence.restricted.n032_to_n064.hu_l2_order`, `convergence.restricted.n032_to_n064.hu_linf_order`, `convergence.restricted.n032_to_n064.hv_l1_order`, `convergence.restricted.n032_to_n064.hv_l2_order`, `convergence.restricted.n032_to_n064.hv_linf_order`, `convergence.restricted.n064_to_n128.eta_l1_order`, `convergence.restricted.n064_to_n128.eta_l2_order`, `convergence.restricted.n064_to_n128.eta_linf_order`, `convergence.restricted.n064_to_n128.hu_l1_order`, `convergence.restricted.n064_to_n128.hu_l2_order`, `convergence.restricted.n064_to_n128.hu_linf_order`, `convergence.restricted.n064_to_n128.hv_l1_order`, `convergence.restricted.n064_to_n128.hv_l2_order`, `convergence.restricted.n064_to_n128.hv_linf_order`, `errors.restricted.n032.eta_l1_rel.lower`, `errors.restricted.n032.eta_l1_rel.upper`, `errors.restricted.n032.eta_l2_rel.lower`, `errors.restricted.n032.eta_l2_rel.upper`, `errors.restricted.n032.eta_linf_rel.lower`, `errors.restricted.n032.eta_linf_rel.upper`, `errors.restricted.n032.hu_l1_rel.lower`, `errors.restricted.n032.hu_l1_rel.upper`, `errors.restricted.n032.hu_l2_rel.lower`, `errors.restricted.n032.hu_l2_rel.upper`, `errors.restricted.n032.hu_linf_rel.lower`, `errors.restricted.n032.hu_linf_rel.upper`, `errors.restricted.n032.hv_l1_rel.lower`, `errors.restricted.n032.hv_l1_rel.upper`, `errors.restricted.n032.hv_l2_rel.lower`, `errors.restricted.n032.hv_l2_rel.upper`, `errors.restricted.n032.hv_linf_rel.lower`, `errors.restricted.n032.hv_linf_rel.upper`, `errors.restricted.n064.eta_l1_rel.lower`, `errors.restricted.n064.eta_l1_rel.upper`, `errors.restricted.n064.eta_l2_rel.lower`, `errors.restricted.n064.eta_l2_rel.upper`, `errors.restricted.n064.eta_linf_rel.lower`, `errors.restricted.n064.eta_linf_rel.upper`, `errors.restricted.n064.hu_l1_rel.lower`, `errors.restricted.n064.hu_l1_rel.upper`, `errors.restricted.n064.hu_l2_rel.lower`, `errors.restricted.n064.hu_l2_rel.upper`, `errors.restricted.n064.hu_linf_rel.lower`, `errors.restricted.n064.hu_linf_rel.upper`, `errors.restricted.n064.hv_l1_rel.lower`, `errors.restricted.n064.hv_l1_rel.upper`, `errors.restricted.n064.hv_l2_rel.lower`, `errors.restricted.n064.hv_l2_rel.upper`, `errors.restricted.n064.hv_linf_rel.lower`, `errors.restricted.n064.hv_linf_rel.upper`, `errors.restricted.n128.eta_l1_rel.lower`, `errors.restricted.n128.eta_l1_rel.upper`, `errors.restricted.n128.eta_l2_rel.lower`, `errors.restricted.n128.eta_l2_rel.upper`, `errors.restricted.n128.eta_linf_rel.lower`, `errors.restricted.n128.eta_linf_rel.upper`, `errors.restricted.n128.hu_l1_rel.lower`, `errors.restricted.n128.hu_l1_rel.upper`, `errors.restricted.n128.hu_l2_rel.lower`, `errors.restricted.n128.hu_l2_rel.upper`, `errors.restricted.n128.hu_linf_rel.lower`, `errors.restricted.n128.hu_linf_rel.upper`, `errors.restricted.n128.hv_l1_rel.lower`, `errors.restricted.n128.hv_l1_rel.upper`, `errors.restricted.n128.hv_l2_rel.lower`, `errors.restricted.n128.hv_l2_rel.upper`, `errors.restricted.n128.hv_linf_rel.lower`, `errors.restricted.n128.hv_linf_rel.upper`, `extrema.h.min`, `mass_drift_rel`, `response.transverse_momentum.l2_rel_tend.lower`, `response.transverse_momentum.l2_rel_tend.upper`, `run.n_step`
- judgment conditions:
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of the case (`final.step - derived.n_step == 0`).
  - The depth-positivity judgment is applied. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 5.0e{-2}$.
  - The mass-conservation judgment is applied. The evaluation expression is `mass_drift_rel`, and the threshold is $\le 1.0e{-10}$.
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
- `quantities`: `cfl.max`, `convergence.n032_to_n064.hu_l1_order`, `convergence.n032_to_n064.hu_l2_order`, `convergence.n032_to_n064.hu_linf_order`, `convergence.n032_to_n064.l1_order`, `convergence.n032_to_n064.l2_order`, `convergence.n032_to_n064.linf_order`, `convergence.n064_to_n128.hu_l1_order`, `convergence.n064_to_n128.hu_l2_order`, `convergence.n064_to_n128.hu_linf_order`, `convergence.n064_to_n128.l1_order`, `convergence.n064_to_n128.l2_order`, `convergence.n064_to_n128.linf_order`, `errors.analytic_h.l1_rel_tend.lower`, `errors.analytic_h.l1_rel_tend.upper`, `errors.analytic_h.l2_rel_tend.lower`, `errors.analytic_h.l2_rel_tend.upper`, `errors.analytic_h.linf_rel_tend.lower`, `errors.analytic_h.linf_rel_tend.upper`, `errors.analytic_hu.l1_rel_tend.lower`, `errors.analytic_hu.l1_rel_tend.upper`, `errors.analytic_hu.l2_rel_tend.lower`, `errors.analytic_hu.l2_rel_tend.upper`, `errors.analytic_hu.linf_rel_tend.lower`, `errors.analytic_hu.linf_rel_tend.upper`, `errors.analytic_hv.l1_rel_tend`, `errors.analytic_hv.l2_rel_tend`, `errors.analytic_hv.linf_rel_tend`, `extrema.h.min`, `mass_drift_rel`, `momx_drift_rel`, `momy_drift_rel`, `run.n_step`
- judgment conditions:
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of the case (`final.step - derived.n_step == 0`).
  - The depth-positivity judgment is applied. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 5.0e{-2}$.
  - The mass-conservation judgment is applied. The evaluation expression is `mass_drift_rel`, and the threshold is $\le 1.0e{-10}$.
  - The momentum-conservation judgment is applied. The evaluation expressions are `momx_drift_rel` and `momy_drift_rel`, and the threshold for both is $\le 1.0e{-10}$.
  - The theoretical-comparison judgment is applied. Each of the six `errors.analytic_h.<k>_rel_tend` and `errors.analytic_hu.<k>_rel_tend` of each case applies its band of the 5-5 table (rows `swe2d_flat_ref_n032_sx000_sy000_dts100`, `_n064_`, `_n128_`) as two judged quantities, each of the three `errors.analytic_hv.<k>_rel_tend` the upper bound $\le 1.0e{-10}$, and each of the six `convergence.n032_to_n064.*_order` quantities (carried by `swe2d_flat_ref_n064_sx000_sy000_dts100`) and the six `convergence.n064_to_n128.*_order` quantities (carried by `swe2d_flat_ref_n128_sx000_sy000_dts100`) its lower bound of the 5-5 order table.
  - The translation-equivariance and symmetry-pair judgments are not applied. The non-application basis is "because the cases are judged here as refinement levels; the pair whose base is `swe2d_flat_ref_n064_sx000_sy000_dts100` is judged in 6-12".
  - The lake-at-rest invariance judgment is not applied. The non-application basis is "because the initial condition is not `lake_at_rest`".
  - The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, and `hv`, and the threshold is $\le 1.0e{-10}$ for each.

### 6-3. `l2_lake_at_rest_invariance`
- `level`: `L2`
- `objective`: confirm that the velocity and free surface are invariant for `williamson_tc5_cone + lake_at_rest`, on the square grid, on the rectangular grid of 2-10 and with the cone on the domain seam (2-15).
- target cases:
  - `swe2d_lake_n064_sx000_sy000_dts100`
  - `swe2d_lake_n064_sx000_sy000_dts100_edge`
  - `swe2d_lake_n064_sx000_sy000_dts100_rect`
- `expected_outcome`: `pass`
- `quantities`: `cfl.max`, `extrema.h.min`, `invariants.lake_rest.max_surface_deviation`, `invariants.lake_rest.max_velocity`, `mass_drift_rel`, `run.n_step`
- judgment conditions:
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of each case (`final.step - derived.n_step == 0`).
  - The depth-positivity judgment is applied. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 5.0e{-2}$.
  - The mass-conservation judgment is applied. The evaluation expression is `mass_drift_rel`, and the threshold is $\le 1.0e{-10}$.
  - The momentum-conservation judgment is not applied. The non-application basis is "because the bottom-topography source term exchanges momentum".
  - The lake-at-rest invariance judgment is applied to every case. It requires `invariants.lake_rest.max_velocity \le 1.0e{-12}` and `invariants.lake_rest.max_surface_deviation \le 1.0e{-12}`.
  - The theoretical-comparison judgment is not applied. The non-application basis is "because it is a topography lake-at-rest case".
  - The translation-equivariance and symmetry-pair judgments are not applied. The non-application basis is "because no translation or symmetry pair is run on these cases".
  - The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, and `hv`, and the threshold is $\le 1.0e{-10}$ for each.

### 6-4. `l2_long_run_mass_conservation`
- `level`: `L2`
- `objective`: confirm that mass and depth positivity are maintained in long-time integration of `williamson_tc5_cone + linear_wave_x`, and the transverse momentum the cone generates over it, bounded on both sides.
- target cases:
  - `swe2d_ref_n064_sx000_sy000_dts100_tend500`
- `expected_outcome`: `pass`
- `quantities`: `cfl.max`, `extrema.h.min`, `mass_drift_rel`, `response.transverse_momentum.l2_rel_tend.lower`, `response.transverse_momentum.l2_rel_tend.upper`, `run.n_step`
- judgment conditions:
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of the case (`final.step - derived.n_step == 0`).
  - The depth-positivity judgment is applied. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 5.0e{-2}$.
  - The mass-conservation judgment is applied. The evaluation expression is `mass_drift_rel`, and the threshold is $\le 5.0e{-10}$.
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
- `quantities`: `cfl.max`, `extrema.h.min`, `mass_drift_rel`, `momx_drift_rel.lower`, `momx_drift_rel.upper`, `momy_drift_rel.lower`, `momy_drift_rel.upper`, `run.n_step`
- judgment conditions:
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of the case (`final.step - derived.n_step == 0`).
  - The depth-positivity judgment is applied. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 5.0e{-2}$.
  - The mass-conservation judgment is applied. The evaluation expression is `mass_drift_rel`, and the threshold is $\le 1.0e{-10}$.
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
- `quantities`: `cfl.max`, `errors.symmetry_h_l2_rel`, `extrema.h.min`, `mass_drift_rel`, `momx_drift_rel`, `momy_drift_rel`, `run.n_step`
- judgment conditions:
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of each case (`final.step - derived.n_step == 0`).
  - The depth-positivity judgment is applied. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 5.0e{-2}$.
  - The mass-conservation judgment is applied. The evaluation expression is `mass_drift_rel`, and the threshold is $\le 1.0e{-10}$.
  - The momentum-conservation judgment is applied. The evaluation expressions are `momx_drift_rel` and `momy_drift_rel`, and the threshold for both is $\le 1.0e{-10}$.
  - The translation-equivariance judgment is applied. The evaluation expression is `errors.symmetry_h_l2_rel`, and the threshold is $\le 2.0e{-11}$.
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
- `quantities`: `cfl.max`
- judgment conditions:
  - The `CFL` judgment is applied in the failure direction: both predicates of `cfl.max` (5-4) read $> 1.0$, the index over the final committed state and the index over the `state_history` samples.
  - The `run.n_step` and depth-positivity judgments are not applied. The non-application basis is "because the purpose of the guard test is only the detection of a stability-condition violation; a run whose step returns the `false` result of the Controlled Spec §5 commits fewer steps (2-9), and the `CFL` judgment alone decides this test".
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
- `quantities`: `cfl.max`, `errors.analytic_h.l1_rel_tend.lower`, `errors.analytic_h.l1_rel_tend.upper`, `errors.analytic_h.l2_rel_tend.lower`, `errors.analytic_h.l2_rel_tend.upper`, `errors.analytic_h.linf_rel_tend.lower`, `errors.analytic_h.linf_rel_tend.upper`, `errors.analytic_hu.l1_rel_tend.lower`, `errors.analytic_hu.l1_rel_tend.upper`, `errors.analytic_hu.l2_rel_tend.lower`, `errors.analytic_hu.l2_rel_tend.upper`, `errors.analytic_hu.linf_rel_tend.lower`, `errors.analytic_hu.linf_rel_tend.upper`, `errors.analytic_hv.l1_rel_tend`, `errors.analytic_hv.l2_rel_tend`, `errors.analytic_hv.linf_rel_tend`, `extrema.h.min`, `mass_drift_rel`, `momx_drift_rel`, `momy_drift_rel`, `run.n_step`
- judgment conditions:
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of the case (`final.step - derived.n_step == 0`).
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
- `quantities`: `cfl.max`, `errors.analytic_h.l1_rel_tend.lower`, `errors.analytic_h.l1_rel_tend.upper`, `errors.analytic_h.l2_rel_tend.lower`, `errors.analytic_h.l2_rel_tend.upper`, `errors.analytic_h.linf_rel_tend.lower`, `errors.analytic_h.linf_rel_tend.upper`, `errors.analytic_hu.l1_rel_tend`, `errors.analytic_hu.l2_rel_tend`, `errors.analytic_hu.linf_rel_tend`, `errors.analytic_hv.l1_rel_tend.lower`, `errors.analytic_hv.l1_rel_tend.upper`, `errors.analytic_hv.l2_rel_tend.lower`, `errors.analytic_hv.l2_rel_tend.upper`, `errors.analytic_hv.linf_rel_tend.lower`, `errors.analytic_hv.linf_rel_tend.upper`, `extrema.h.min`, `mass_drift_rel`, `momx_drift_rel`, `momy_drift_rel`, `run.n_step`
- judgment conditions:
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of the case (`final.step - derived.n_step == 0`).
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
- `quantities`: `cfl.max`, `errors.time_pair.hv_l2_rel.lower`, `errors.time_pair.hv_l2_rel.upper`, `extrema.h.min`, `mass_drift_rel`, `response.transverse_momentum.l2_rel_tend.lower`, `response.transverse_momentum.l2_rel_tend.upper`, `run.n_step`
- judgment conditions:
  - The time-refinement judgment is applied. The evaluation expression is `errors.time_pair.hv_l2_rel` (carried by the halved case), and the band is $[7.9e{-6},\ 9.7e{-6}]$ as two judged quantities.
  - The `CFL` judgment is applied to both cases. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of each case (`final.step - derived.n_step == 0`).
  - The depth-positivity judgment is applied to both cases. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 5.0e{-2}$.
  - The mass-conservation judgment is applied to both cases. The evaluation expression is `mass_drift_rel`, and the threshold is $\le 1.0e{-10}$.
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
- `quantities`: `cfl.max`, `errors.axis_exchange.h_l2_rel`, `errors.axis_exchange.hu_l2_rel`, `errors.axis_exchange.hv_l2_rel`, `extrema.h.min`, `mass_drift_rel`, `momx_drift_rel`, `momy_drift_rel`, `run.n_step`
- judgment conditions:
  - The symmetry-pair judgment is applied to each transformed case. The evaluation expressions are `errors.axis_exchange.h_l2_rel`, `errors.axis_exchange.hu_l2_rel` and `errors.axis_exchange.hv_l2_rel`, each with the threshold $\le 2.0e{-11}$.
  - The `CFL` judgment is applied to each case. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of each case (`final.step - derived.n_step == 0`), which is the base's (2-11).
  - The depth-positivity judgment is applied to each case. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 5.0e{-2}$.
  - The mass-conservation judgment is applied to each case. The evaluation expression is `mass_drift_rel`, and the threshold is $\le 1.0e{-10}$.
  - The momentum-conservation judgment is applied to each `topography_profile=flat` case. The evaluation expressions are `momx_drift_rel` and `momy_drift_rel`, and the threshold for both is $\le 1.0e{-10}$. It is not applied to the `williamson_tc5_cone` cases. The non-application basis is "because the bottom-topography source term exchanges momentum".
  - The theoretical-comparison, restricted-reference, topographic-response, translation-equivariance, lake-at-rest invariance and time-refinement judgments are not applied. The non-application basis is "because each transformed case equals the transformation of its base, whose bands are judged in 6-1, 6-2, 6-8 and 6-9, to the round-off the pair judgment bounds".
  - The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, and `hv`, and the threshold is $\le 1.0e{-10}$ for each.

### 6-12. `l3_x_mirror_pairs`
- `level`: `L3`
- `objective`: confirm the mirror equivariance in `x` of the Controlled Spec §5 on the flat linear wave and the cone (2-11).
- pair cases (base → transformed):
  - `swe2d_flat_ref_n064_sx000_sy000_dts100` → `swe2d_flat_ref_n064_sx000_sy000_dts100_xmir`
  - `swe2d_ref_n064_sx000_sy000_dts100` → `swe2d_ref_n064_sx000_sy000_dts100_xmir`
- `expected_outcome`: `pass`
- `quantities`: `cfl.max`, `errors.mirror_x.h_l2_rel`, `errors.mirror_x.hu_l2_rel`, `errors.mirror_x.hv_l2_rel`, `extrema.h.min`, `mass_drift_rel`, `momx_drift_rel`, `momy_drift_rel`, `run.n_step`
- judgment conditions:
  - The symmetry-pair judgment is applied to each transformed case. The evaluation expressions are `errors.mirror_x.h_l2_rel`, `errors.mirror_x.hu_l2_rel` and `errors.mirror_x.hv_l2_rel`, each with the threshold $\le 2.0e{-11}$.
  - The `CFL` judgment is applied to each case. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of each case (`final.step - derived.n_step == 0`), which is the base's (2-11).
  - The depth-positivity judgment is applied to each case. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 5.0e{-2}$.
  - The mass-conservation judgment is applied to each case. The evaluation expression is `mass_drift_rel`, and the threshold is $\le 1.0e{-10}$.
  - The momentum-conservation judgment is applied to each `topography_profile=flat` case. The evaluation expressions are `momx_drift_rel` and `momy_drift_rel`, and the threshold for both is $\le 1.0e{-10}$. It is not applied to the `williamson_tc5_cone` cases. The non-application basis is "because the bottom-topography source term exchanges momentum".
  - The theoretical-comparison, restricted-reference, topographic-response, translation-equivariance, lake-at-rest invariance and time-refinement judgments are not applied. The non-application basis is "because each transformed case equals the transformation of its base, whose bands are judged in 6-1, 6-2, 6-8 and 6-9, to the round-off the pair judgment bounds".
  - The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, and `hv`, and the threshold is $\le 1.0e{-10}$ for each.

### 6-13. `l3_y_mirror_pairs`
- `level`: `L3`
- `objective`: confirm the mirror equivariance in `y` of the Controlled Spec §5 on the rectangular `y`-direction wave and the cone (2-11).
- pair cases (base → transformed):
  - `swe2d_flat_ref_n064_sx000_sy000_dts100_recty` → `swe2d_flat_ref_n064_sx000_sy000_dts100_rectymir`
  - `swe2d_ref_n064_sx000_sy000_dts100` → `swe2d_ref_n064_sx000_sy000_dts100_ymir`
- `expected_outcome`: `pass`
- `quantities`: `cfl.max`, `errors.mirror_y.h_l2_rel`, `errors.mirror_y.hu_l2_rel`, `errors.mirror_y.hv_l2_rel`, `extrema.h.min`, `mass_drift_rel`, `momx_drift_rel`, `momy_drift_rel`, `run.n_step`
- judgment conditions:
  - The symmetry-pair judgment is applied to each transformed case. The evaluation expressions are `errors.mirror_y.h_l2_rel`, `errors.mirror_y.hu_l2_rel` and `errors.mirror_y.hv_l2_rel`, each with the threshold $\le 2.0e{-11}$.
  - The `CFL` judgment is applied to each case. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of each case (`final.step - derived.n_step == 0`), which is the base's (2-11).
  - The depth-positivity judgment is applied to each case. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 5.0e{-2}$.
  - The mass-conservation judgment is applied to each case. The evaluation expression is `mass_drift_rel`, and the threshold is $\le 1.0e{-10}$.
  - The momentum-conservation judgment is applied to each `topography_profile=flat` case. The evaluation expressions are `momx_drift_rel` and `momy_drift_rel`, and the threshold for both is $\le 1.0e{-10}$. It is not applied to the `williamson_tc5_cone` cases. The non-application basis is "because the bottom-topography source term exchanges momentum".
  - The theoretical-comparison, restricted-reference, topographic-response, translation-equivariance, lake-at-rest invariance and time-refinement judgments are not applied. The non-application basis is "because each transformed case equals the transformation of its base, whose bands are judged in 6-1, 6-2, 6-8 and 6-9, to the round-off the pair judgment bounds".
  - The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, and `hv`, and the threshold is $\le 1.0e{-10}$ for each.

### 6-14. `l0_wet_domain_refusal_xfail`
- `level`: `L0`
- `objective`: confirm that the step returns `false` when a reconstructed depth reaches $h^{*}\le 0$ within the run, after state was committed (expected failure, 2-16).
- target cases:
  - `swe2d_dry_n032_sx000_sy000_dts500`
- `expected_outcome`: `xfail`
- `xfail_condition`: `h* <= 0` within the run
- `quantities`: `ok`, `run.state_moved`
- judgment conditions:
  - The refusal judgment is applied. `ok == 0` over the final state, and `run.state_moved > 0`: a kernel that refuses its first step leaves the state at its initial value, and a kernel that ignores the invariant runs on to `ok == 1`.
  - The `CFL`, `run.n_step` and depth-positivity judgments are not applied. The non-application basis is "because the case is unstable by construction and the step refuses before $\mathrm{n\_step}$".
  - The mass-conservation and momentum-conservation judgments are not applied. The non-application basis is "because the purpose of this test is only the refusal".
  - The theoretical-comparison judgment is not applied. The non-application basis is "because it is a case with topography under an unstable condition".
  - The translation-equivariance and symmetry-pair judgments are not applied. The non-application basis is "because no translation or symmetry pair is run on this case".
  - The lake-at-rest invariance judgment is not applied. The non-application basis is "because the initial condition is not `lake_at_rest`".
  - The cross-target judgment is not applied. The non-application basis is "because the step at which an unstable case refuses is not a result two variants must agree on".

### 6-15. `l0_boundary_refusal_xfail`
- `level`: `L0`
- `objective`: confirm that the refusal of `nx<2` and of `ny<2` by the boundary `component` reaches the result of the step (expected failure, 2-17).
- target cases:
  - `swe2d_flat_ref_n032_sx000_sy000_dts100_nx001`
  - `swe2d_flat_ref_n032_sx000_sy000_dts100_ny001`
- `expected_outcome`: `xfail`
- `xfail_condition`: `nx<2` or `ny<2`
- `quantities`: `ok`
- judgment conditions:
  - The refusal judgment is applied to every case. The evaluation expression is `ok`, the flag `final.ok`, and it is judged $= 0$.
  - Every other judgment is not applied. The non-application basis is "because the case is refused at its first step".
  - The cross-target judgment is not applied. The non-application basis is "because the state a refused case leaves is not a result two variants must agree on".

### 6-16. `l0_dt_refusal_xfail`
- `level`: `L0`
- `objective`: confirm that the refusal of `dt<0` by the time-update `component` reaches the result of the step (expected failure, 2-17).
- target cases:
  - `swe2d_flat_ref_n032_sx000_sy000_dts100_dtneg`
- `expected_outcome`: `xfail`
- `xfail_condition`: `dt<0`
- `quantities`: `ok`
- judgment conditions:
  - The refusal judgment is applied. The evaluation expression is `ok`, the flag `final.ok`, and it is judged $= 0$.
  - Every other judgment is not applied. The non-application basis is "because the case is refused at its first step".
  - The cross-target judgment is not applied. The non-application basis is "because the state a refused case leaves is not a result two variants must agree on".

## 7. Pass/fail aggregation rules
- `per_test.pass_rule`: `pass` when every predicate of the test holds.
- `per_test.xfail_rule`: `xfail` when `expected_outcome` is `xfail`, `xfail_condition` is true and every predicate of the test holds, in the failure direction.
- `suite.pass_rule`: `pass` when all `test_id` satisfy `pass_rule` or `xfail_rule`.

## 8. Traceability
### 8-1. Output requirements
The host writes `verdict.json` from the predicates: each test's `status` and, in `basis.predicates`, each predicate's value and threshold. `summary.json` carries the counts of `pass`, `fail`, `xfail`, and `skipped`.

### 8-2. Traceability records
- The `test_profile_id` and `test_profile_version` of this document must be recorded under each case's `inputs` in `spec.ir.yaml` and in `trial_meta.json`.
- The judgment conditions of this document must be mappable to the predicates of `spec.ir.yaml` through the `quantities` lines.
