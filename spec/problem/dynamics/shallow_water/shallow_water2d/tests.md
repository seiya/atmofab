# Tests: 2D shallow water equation (verification input / judgment conditions)

## 0. Meta information
- `status`: `draft`
- `test_profile_id`: `shallow_water2d_baseline`
- `test_profile_version`: `0.5.0`
- `spec_ref.spec_kind`: `problem`
- `spec_ref.spec_id`: `shallow_water2d`
- `spec_ref.spec_version`: `0.4.6`
- `spec_ref.controlled_spec_path`: `spec/problem/dynamics/shallow_water/shallow_water2d/controlled_spec.md`

## 1. Test purpose
This suite verifies, for the discrete implementation of the 2D shallow water equation including bottom topography, conservation, lake-at-rest invariance, stability under topographic forcing, agreement with the theoretical solution — bounded on both sides, since the fully fixed `p0` discretization makes it a property of the scheme — translation equivariance, a rectangular grid, on which an exchange of the two extents, the two domain lengths or the two spacings is not the identity, a linear wave in `y` on that grid, on which a defect confined to the `y` direction is not the identity, the step count of §3, the topographic response — the transverse momentum a `y`-uniform wave acquires over the cone, the momentum the cone exchanges with the oblique mode, and a time-refinement pair of the forced case — bounded on both sides, and the `CFL` guard. The conservation judgment targets mass for all cases, and momentum for the `topography_profile=flat` cases. The judgment targets are `L0` to `L3`, and include an expected failure (`xfail`).

## 2. Input-defaulting rules
### 2-1. Basic constants
- The domain lengths are `L_x=L_y=1.0` (the rectangular cases of 2-5 and 2-6 carry `L_y=0.5`, `ny=48`).
- The representative water level is `H_0=1.0`.
- For the gravitational acceleration, use the fixed value `g=9.81` of the Controlled Spec.
- The small amplitude is `eta0=1.0e-3`.
- The reference phase speed of a linear gravity wave is $c_0=\sqrt{gH_0}$.

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
  The topography parameters are `x_c=3L_x/4`, `y_c=2L_y/3`, `r_0=min(L_x,L_y)/6`, and `h_s=0.2H_0`.
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
  u(x,y,0)=\frac{\eta_0 c_0}{H_0}\sin\left(2\pi\left(\frac{x}{L_x}-\mathrm{shift\_x\_fraction}\right)\right),\quad v(x,y,0)=0
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
  u(x,y,0)=0,\quad v(x,y,0)=\frac{\eta_0 c_0}{H_0}\sin\left(2\pi\left(\frac{y}{L_y}-\mathrm{shift\_y\_fraction}\right)\right)
  $$
  $$
  hu=h\,u,\quad hv=h\,v
  $$
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
Only when `topography_profile=flat` and `initial_profile` is `linear_wave_x` or `linear_wave_y`, use the reference solution of the linearized shallow water equation. For `linear_wave_x` the reference solution is the following.
$$
h_{ref}(x,t)=H_0+\eta_0\sin\left(2\pi\left(\frac{x}{L_x}-\frac{c_0 t}{L_x}-\mathrm{shift\_x\_fraction}\right)\right)
$$
For `linear_wave_y` the reference solution is the following.
$$
h_{ref}(y,t)=H_0+\eta_0\sin\left(2\pi\left(\frac{y}{L_y}-\frac{c_0 t}{L_y}-\mathrm{shift\_y\_fraction}\right)\right)
$$
A theoretical-agreement judgment for variables other than `h` is not required in this suite.

### 2-5. The rectangular case
Every case of §4 but two (this one and the `y`-direction case of 2-6) carries $nx=ny$ and `L_x=L_y`, so an implementation that exchanges the two extents, the two domain lengths or the two spacings — an array declared `(ny, nx)`, a loop bounded by `ny` where `nx` is meant, `dx` where `dy` is meant, an `rhs` that reads its stage state as `(ncomp, ny, nx)` — gives the numbers of a correct one on those cases. `swe2d_flat_ref_n064_sx000_sy000_dts100_rect` (4-3) carries `nx=64`, `ny=48`, `L_x=1.0` and `L_y=0.5`, so $dx=1/64$ and $dy=1/96$, and no exchange within any of the three pairs is the identity on a derived quantity. The §3 procedure gives $\mathrm{n\_step}=223$ and $dt=8.969\times10^{-4}$. The wave of `linear_wave_x` is uniform in `y`, so `ny` and `L_y` enter `errors.analytic_h.l2_rel_tend` through `dt` alone: its reference value is `1.2427e-4`, measured on the reference implementation of 5-5, equal to the value of `swe2d_flat_ref_n064_sx000_sy000_dts100` (`1.2428e-4`) to four digits. Measured on the same implementation with one substitution at a time: `L_x` and `L_y` exchanged gives `1.10e-3`; `dx` and `dy` exchanged in the interface-flux difference gives `1.03e-3`; the cell centres evaluated with the spacings exchanged at setup gives `6.45e-4`; an `rhs` that reads the stage state as `(ncomp, ny, nx)` gives `7.07e-4`; an `x` loop bounded by `ny` gives `9.37e-4` (and `1.3e-4` in `mass_drift_rel`). Each lies outside the band of 5-5. `dx` and `dy` exchanged in the dt rule alone gives the reference value and the §3 step count (and does on the `y`-direction case of 2-6): the rule of §3 is symmetric in the two spacings when $|u|,|v|\ll c$, so that exchange changes no judged quantity.

### 2-6. The `y`-direction case
The wave of `linear_wave_x` is uniform in `y`, so the `y`-interface flux difference $G^{*}_{i,j+1/2}-G^{*}_{i,j-1/2}$ vanishes identically on it, and nothing in the `y` flux, the `y` ghost or the spacing that divides the `y` difference reaches `errors.analytic_h.l2_rel_tend`; the pair of 6-6 shares such a defect between its two runs. `swe2d_flat_ref_n064_sx000_sy000_dts100_recty` (4-3) carries the grid of 2-5 and `initial_profile=linear_wave_y`, the wave of 2-3 turned to `y`, with the reference solution of 2-4. The §3 procedure gives $\mathrm{n\_step}=223$ and $dt=8.969\times10^{-4}$, as for the case of 2-5. Its reference value of `errors.analytic_h.l2_rel_tend` is `2.8482e-4`, measured on the reference implementation of 5-5 (the wave has `48` cells per wavelength and travels `1.25` wavelengths in $t_{end}$, against `64` and `0.63` for the case of 2-5). Measured on the same implementation with one substitution at a time: the `y` flux difference divided by `dx` gives `1.17e-3`; the `y` pressure flux omitted gives `2.65e-3`; the `y` flux omitted gives `1.01e-3`, the value of a state left at its initial value; the one-call step of the Controlled Spec §5 gives `0.78` of the reference; half the steps gives `1.15e-3`. Each lies outside the band of 5-5. A `y` boundary that copies the nearest interior cell instead of the periodic image gives `2.76e-4`, inside the band, and `3.7e-7` in `momy_drift_rel`, which fails the momentum judgment. The `y` flux difference divided by `dx` gives the reference value on every other case of §4: each is square or carries the `x` wave. The `y` pressure flux omitted and the `y` flux omitted break the lake-at-rest balance of 6-3 and the topographic response of 2-7 as well (`1.2` and `2.0` in `lake_rest.max_velocity`), and the copied `y` boundary breaks the momentum and translation-equivariance judgments of 6-6 as well. The sign of the transverse momentum flux and the velocity term of the `y` wave speed move the value by less than `0.1%` at the amplitude of 2-1 and are not fixed by this case.

### 2-7. The topographic response
On `williamson_tc5_cone + linear_wave_x` the initial `hv` is zero and the wave is uniform in `y`, so on a flat bottom `hv` stays zero to round-off and without the §5 update it stays zero on any bottom: the `hv` at $t_{end}$ is the momentum the cone generates through the reconstruction of the Controlled Spec §5 step 2 and the source term of its step 4. It is measured by `response.transverse_momentum.l2_rel_tend` (5-4), whose reference values are `1.583e-2`, `1.861e-2` and `1.998e-2` at `nx=32`, `64` and `128` and `1.573e-4` for `swe2d_ref_n064_sx000_sy000_dts100_tend500`, measured on the reference implementation of 5-5, which reproduces the captured state of the certified variant of version `0.4.5` of this node on every case to `3e-15`. The Controlled Spec §5 fixes the reconstruction, and the lake-at-rest invariance of 6-3 does not: three reconstructions that keep exact lake-at-rest balance — the interface elevation taken as the minimum or the average of the two cell elevations instead of their maximum, and the interface state carrying the cell momentum instead of the reconstructed depth times the cell velocity — give `0.59`, `0.79` and `0.83` of the reference at `nx=32` (`0.75`, `0.87` and `0.90` at `nx=64`; `0.86`, `0.93` and `0.95` at `nx=128`) and `1e-15` or less in `lake_rest.max_velocity`. The coarsest case is where a variant of the discretization is visible, and the `nx=32` band of 5-5 excludes all three. Half the steps gives `0.47` to `0.51` of the reference, a state left at its initial value `0`, and a gravitational acceleration halved `0.60` to `0.71`; the source term omitted, its sign flipped, its `y` component omitted, a source from the analytic gradient or from the cell depth, and the reconstruction omitted give `3.9` to `2600` times the reference at `nx=32` to `128` and fail 6-3 as well. The one-call step of the Controlled Spec §5 gives `1.03` to `1.11` of the reference at `nx=32` to `128`, inside those bands; it gives `2.9` on the long case and is excluded by the bands of 6-2 and the pair of 2-8 as well. `dt_scale` from `0.5` to `2.0` moves the value by at most `0.04%` at `nx=64` and `0.6%` on the long case.

On `williamson_tc5_cone + oblique_mode` the initial momentum is zero in both directions, so $P^x(t_{end})$ and $P^y(t_{end})$ (5-4) are the time integral of the source term over the run: `momx_drift_rel` and `momy_drift_rel` have the reference values `1.509e-6` and `1.511e-6` on `swe2d_resp_n064_sx000_sy000_dts100`, measured on the same implementation. The interface elevation taken as the minimum gives `0.84` of each; a gravitational acceleration halved `0.63`; a state left at its initial value `0`; the `y` component of the source omitted gives `0` in `momy_drift_rel`, and a source from the cell depth `0.66`. `dt_scale` from `0.5` to `2.0` moves them by at most `0.3%`.

### 2-8. Time-refinement pair
The functionals of 2-7 are end-state functionals and move by less than `2%` (`0.9%` in the transverse momentum at `nx=32`, `1.8%` in the momentum exchange of 6-5) when the source term $S_b$ of the Controlled Spec §5 step 4 is evaluated once per step at $U^n$ instead of at each stage state, the error that makes the topographic forcing first order in time; each stays inside its band. The time integration of the forced case is judged by a pair of `williamson_tc5_cone + linear_wave_x` runs at `nx=64` that differ only in `dt_scale` (`1.0` and `0.5`): the difference of their final `hv`, normalised by the initial `hu` of the base case, is `errors.time_pair.hv_l2_rel` (5-4). Its reference value is `8.781e-6`; the pair at `dt_scale` `0.5` and `0.25` gives `2.178e-6`, a ratio of `4.0`, the second order of the §5 step 5 composition. Measured on the reference implementation of 5-5 with one substitution at a time: $S_b$ evaluated at $U^n$ for the second call gives `1.17e-4`; the one-call step `6.26e-4`; `dt` left at $\mathrm{dt\_raw}$ without step 4 of §3 (the run ends past $t_{end}$) `1.28e-4`; half the steps `1.07e-4`; a state left at its initial value `0`; a gravitational acceleration halved `4.29e-6`; the interface elevation taken as the minimum `7.74e-6`; $\lambda_0$ from one direction `3.56e-5` and `cfl_target=0.5` `1.09e-5`. Each lies outside the band of 5-5. The interface elevation taken as the average (`8.25e-6`) and the interface state carrying the cell momentum (`7.94e-6`) lie inside it and are excluded by the `nx=32` band of 2-7. The pair does not fix the exchange of `dx` and `dy` in the dt rule (2-5).

### 2-9. The `CFL` guard case
`swe2d_guard_n032_sx000_sy000_dts240` carries `dt_scale=2.40`, so the §3 procedure gives $\mathrm{n\_step}=38$ and $dt=5.263\times10^{-3}$, and the stability index of the Controlled Spec §5 is `1.056` at the initial state and at its maximum over the run (`cfl.max`, 5-4) — above the threshold `1.0` of the `CFL` judgment, which is what 6-7 detects. The case performs all of its §3 steps and stays wet: the depth minimum over the initial state and every committed state is the initial minimum, `0.999005`, against the positivity threshold `5.0e-2` of 5-5, measured on the reference implementation of 5-5. The wet-domain error of the Controlled Spec §5 is not reached by this case. A run of this case whose step loop stops on that error performs fewer steps: its `run.n_step` is the count of steps performed (§3), its judgment against the §3 $\mathrm{n\_step}$ in 6-7 reports `fail`, and 6-7 states what that status decides. The step count and the depth minimum stated here are properties of the fixed discretization at the case's inputs, not conditions of the `pass_when` of 6-7, which reads the `CFL` judgment alone: the `run.n_step` and depth-positivity judgments of the case are applied and their values and statuses reported (6-7).

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
| `swe2d_ref` | refinement_for_mass_and_positivity_with_topography | $nx=ny\in\{32,64,128\}$ | `topography_profile=williamson_tc5_cone`, `initial_profile=linear_wave_x`, `shift_x_fraction=0.0`, `shift_y_fraction=0.0`, `dt_scale=1.0` |
| `swe2d_lake` | lake_at_rest_invariance_with_topography | $nx=ny=64$ | `topography_profile=williamson_tc5_cone`, `initial_profile=lake_at_rest`, `shift_x_fraction=0.0`, `shift_y_fraction=0.0`, `dt_scale=1.0` |
| `swe2d_resp` | topography_forced_response_stability | $nx=ny=64$ | `topography_profile=williamson_tc5_cone`, `initial_profile=oblique_mode`, `shift_x_fraction=0.0`, `shift_y_fraction=0.0`, `dt_scale=1.0` |
| `swe2d_flat_ref` | refinement_for_linear_wave_analytic | $nx=ny\in\{32,64,128\}$ | `topography_profile=flat`, `initial_profile=linear_wave_x`, `shift_x_fraction=0.0`, `shift_y_fraction=0.0`, `dt_scale=1.0` |
| `swe2d_flat_sym` | translation_equivariance_without_topography | $nx=ny=64$ | `topography_profile=flat`, `initial_profile=oblique_mode`, `shift_x_fraction=0.0`, `shift_y_fraction=0.0`, `dt_scale=1.0` |
| `swe2d_guard` | expected_failure_for_cfl | $nx=ny=32,\ \mathrm{dt\_scale}=2.40$ | `topography_profile=flat`, `initial_profile=linear_wave_x`, `shift_x_fraction=0.0`, `shift_y_fraction=0.0` |

### 4-2. `case_id` generation rule
- The template is `{family}_n{nx:03d}_sx{sx_pct:03d}_sy{sy_pct:03d}_dts{dts_pct:03d}`.
- `sx_pct` is decided by $\mathrm{round}(100\cdot \mathrm{shift\_x\_fraction})$.
- `sy_pct` is decided by $\mathrm{round}(100\cdot \mathrm{shift\_y\_fraction})$.
- `dts_pct` is decided by $\mathrm{round}(100\cdot \mathrm{dt\_scale})$.
- The expansion order is fixed in the order `family`, `nx`, `shift_x_fraction`, `shift_y_fraction`, `dt_scale`.

### 4-3. Explicit-override cases
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

## 5. Diagnostics contract
### 5-1. Artifacts
- The diagnostic output file is `diagnostics.json`.
- The judgment output file is `verdict.json`.

### 5-2. Required diagnostic items
`diagnostics.json` requires the following fields.
- `cfl.max`
- `conserved.mass.initial`
- `conserved.mass.final`
- `conserved.momentum_x.initial`
- `conserved.momentum_x.final`
- `conserved.momentum_y.initial`
- `conserved.momentum_y.final`
- `extrema.h.min`
- `errors.analytic_h.l2_rel_tend`
- `errors.symmetry_h_l2_rel`
- `invariants.lake_rest.max_velocity`
- `invariants.lake_rest.max_surface_deviation`
- `run.n_step`
- `response.transverse_momentum.l2_rel_tend`
- `errors.time_pair.hv_l2_rel`

### 5-3. `N/A` rule
- When a diagnostic item is incomputable or non-applicable, make the output value `null` and require `reason_na`.
- `errors.analytic_h.l2_rel_tend` is `N/A` for anything other than `topography_profile=flat` with `initial_profile=linear_wave_x` or `linear_wave_y`.
- `errors.symmetry_h_l2_rel` is `N/A` for anything other than a translation-pair evaluation.
- `momx_drift_rel` and `momy_drift_rel` are computed for every case: judged against the thresholds of 5-5 for `topography_profile=flat`, as the two-sided band of 5-5 for `swe2d_resp_n064_sx000_sy000_dts100` (6-5), and reported and not judged for the other `williamson_tc5_cone` cases, where the source term exchanges momentum and the response is judged by `response.transverse_momentum.l2_rel_tend` (6-1, 6-4, 6-10) or the lake-at-rest invariance (6-3).
- `response.transverse_momentum.l2_rel_tend` is `N/A` for anything other than `topography_profile=williamson_tc5_cone` with `initial_profile=linear_wave_x`.
- `errors.time_pair.hv_l2_rel` is `N/A` for anything other than the halved-`dt` case of the time-refinement pair; it is carried by `swe2d_ref_n064_sx000_sy000_dts100_half`, which sorts after its base case, and reads the base case's initial and final state.
- `invariants.lake_rest.*` is `N/A` for anything other than `initial_profile=lake_at_rest`.
- `run.n_step` is computed for every case and is never `N/A`.

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

The theoretical-comparison error (`flat + linear_wave_x` and `flat + linear_wave_y` only) is defined by the following.
$$
\mathrm{analytic\_h\_l2\_rel}=
\frac{\|h_{num}(t_{end})-h_{ref}(t_{end})\|_2}{\|h_{ref}(t_{end})\|_2}
$$

The translation-equivariance error is defined by the following.
$$
\mathrm{symmetry\_h\_l2\_rel}=
\frac{\|h_{shifted}(t_{end})-\mathrm{shift}(h_{ref}(t_{end}),+\Delta x,+\Delta y)\|_2}{\|h_{ref}(t_{end})\|_2}
$$
Here $\Delta x=\mathrm{shift\_x\_fraction}\cdot L_x$ and $\Delta y=\mathrm{shift\_y\_fraction}\cdot L_y$.

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

`cross_target_state_agreement` compares this target's numerical solution with every other target's certified variant of this node, case by case (`docs/TESTS.md` §Cross-target judgment). For each of the state variables `h`, `hu`, and `hv` at $t_{end}$ of a case, with $q$ that variable and $q^{ref}$ the same variable at $t_{end}$ of the same case in the other variant,
$$
\mathrm{cross\_target\_state\_agreement}=\frac{\max_{i,j} |q_{i,j}-q^{ref}_{i,j}|}{N_q}
$$
The normaliser of `h` is $N_h=\max(\max_{i,j}|h_{i,j}|,\ \max_{i,j}|h^{ref}_{i,j}|,\ 1)$. The two momentum components share one normaliser, the momentum scale of the two variants, $N_{hu}=N_{hv}=\max(\max_{i,j}|hu_{i,j}|,\ \max_{i,j}|hv_{i,j}|,\ \max_{i,j}|hu^{ref}_{i,j}|,\ \max_{i,j}|hv^{ref}_{i,j}|,\ 1)$: a component that is a small residue of the flow (the cross-stream `hv` of a zonal jet) carries the rounding of the whole momentum field, so its own maximum is not its rounding scale. It is evaluated by the host from the captured state, against each other variant separately and for each variable separately; it is not a `diagnostics.json` field, and on a target with no other certified variant it holds vacuously.

### 5-5. Default thresholds
The thresholds of `analytic_h_l2_rel`, `transverse_momentum_l2_rel`, the momentum exchange of 6-5 and `time_pair_hv_l2_rel` are calibrated against an independent reference implementation of the Controlled Spec §5 scheme (issue #491, calibration comment; issues #494 and #495, calibration comments), which reproduces the values of the certified variants of versions `0.4.1` to `0.4.3` of this node to five digits and the captured state of the certified variant of version `0.4.5` on every case to `3e-15`.

- $\mathrm{cfl.max} \le 1.0$, and `run.n_step` equals the §3 $\mathrm{n\_step}$ of the case (an integer; judged as $|\mathrm{run.n\_step}-\mathrm{n\_step}| \le 0.5$)
- $h_{min} \ge 5.0e{-2}$
- $\mathrm{mass\_drift\_rel} \le 1.0e{-10}$
- $\mathrm{momx\_drift\_rel} \le 1.0e{-10}$ and $\mathrm{momy\_drift\_rel} \le 1.0e{-10}$ for the `topography_profile=flat` cases. For `swe2d_resp_n064_sx000_sy000_dts100` (6-5) each is judged as the two-sided band $[1.3e{-6},\ 1.7e{-6}]$ (references `1.509e-6` and `1.511e-6`, 2-7; `0.9` and `1.1` times them, rounded outward to two digits): the two momenta are zero at $t=0$ and their value at $t_{end}$ is the integral of the source term of the Controlled Spec §5 over the run, a property of the fixed discretization — a value below the band is a run that did not perform the §5 update or a source term weaker than §5's, and a value above it a stronger one. Each bound is a DISTINCT judged quantity of the test with its own host-evaluated corroborant over the captured state, as for `analytic_h_l2_rel`.
- `analytic_h_l2_rel` is judged as a two-sided band (6-2, 6-8, 6-9). The Controlled Spec §5 fixes every term of the discretization (`p0` interface states, the Rusanov flux, the periodic ghost, the two-stage `SSPRK2` composition of its step 5) and §3 fixes `dt`, so the error against the linear-wave solution at $t_{end}$ is a property of the discretization — its truncation error and numerical diffusion — and not a free quantity: a value below the band is not a better scheme, it is a run that did not perform the §5 update (a state that holds the theoretical solution at $t_{end}$ without the update has zero error and passes an upper bound alone), and a value above it is a state left at its initial value (`1.304e-3` on every case of `swe2d_flat_ref`), a stopped update (half the steps gives `1.13e-3` at `nx=64`) or a wrong one. The value moves by round-off alone under a conforming implementation — the certified variants of versions `0.4.1` to `0.4.3` of this node agree to `3e-12` relative, and `dt_scale` from `0.5` to `2.0` moves it by at most `0.12%` — so the bounds are `0.9` and `1.1` times the reference value, rounded outward to two digits. The lower bound and the upper bound are two DISTINCT judged quantities of the test — each names its own `quantity` and each requires its own host-evaluated corroborant over the captured state — not two thresholds on one quantity: a corroborant that reads only the upper bound leaves the lower bound to the checks module's own report, and a state that holds the theoretical solution with a reported value inside the band would then pass. For `swe2d_flat_ref` the band is $[2.0e{-4},\ 2.5e{-4}]$ for `nx=32` (reference `2.2671e-4`), $[1.1e{-4},\ 1.4e{-4}]$ for `nx=64` (reference `1.2428e-4`), and $[5.8e{-5},\ 7.2e{-5}]$ for `nx=128` (reference `6.5158e-5`); for the rectangular case `swe2d_flat_ref_n064_sx000_sy000_dts100_rect` it is $[1.1e{-4},\ 1.4e{-4}]$ (reference `1.2427e-4`, 2-5); for the `y`-direction case `swe2d_flat_ref_n064_sx000_sy000_dts100_recty` it is $[2.5e{-4},\ 3.2e{-4}]$ (reference `2.8482e-4`, 2-6). The one-call step $U^{n+1}=U^n+\Delta t\,L(U^n)$ (the forward-Euler step the Controlled Spec §5 step 5 excludes) gives `0.78` to `0.83` of the reference value on every case and fails the lower bound; a gravitational acceleration in error by `5%` moves the value outside the band on `nx=128` in either direction, and one in error by `1%` on no case.
- `transverse_momentum_l2_rel` is judged as a two-sided band (6-1, 6-4, 6-10). The Controlled Spec §5 fixes the reconstruction and the source term, so the transverse momentum the cone generates from a `y`-uniform wave is a property of the discretization (2-7): a value below the band is a run that did not perform the §5 update or a reconstruction weaker than §5's, and a value above it a stronger one. The bounds are `0.9` and `1.1` times the reference value, rounded outward to two digits: $[1.4e{-2},\ 1.8e{-2}]$ for `nx=32` (reference `1.583e-2`), $[1.6e{-2},\ 2.1e{-2}]$ for `nx=64` (reference `1.861e-2`; the halved case of 2-8 carries the same band, its reference `1.861e-2` equal to its base's to four digits), $[1.7e{-2},\ 2.2e{-2}]$ for `nx=128` (reference `1.998e-2`), and $[1.4e{-4},\ 1.8e{-4}]$ for `swe2d_ref_n064_sx000_sy000_dts100_tend500` (reference `1.573e-4`). The lower bound and the upper bound are two DISTINCT judged quantities of the test, each with its own host-evaluated corroborant over the captured state, as for `analytic_h_l2_rel`.
- `time_pair_hv_l2_rel` is judged as a two-sided band $[7.9e{-6},\ 9.7e{-6}]$ on `swe2d_ref_n064_sx000_sy000_dts100_half` (reference `8.781e-6`, 2-8; `0.9` and `1.1` times it, rounded outward to two digits), as two distinct judged quantities, each with its own host-evaluated corroborant over the captured states of the two cases.
- `convergence_order` uses $p=\log(e_{coarse}/e_{fine})/\log(2)$ over `analytic_h_l2_rel`, and requires $\ge 0.80$ for both `n32_to_n64` and `n64_to_n128`. It is accumulated over the three cases of `swe2d_flat_ref` that 6-2 judges and emitted as a per-case field of the finer case of each pair; the override cases `swe2d_flat_ref_n064_sx000_sy000_dts100_rect` and `swe2d_flat_ref_n064_sx000_sy000_dts100_recty` are not members of a refinement pair and omit the field.
- `lake_rest.max_velocity \le 1.0e{-12}`
- `lake_rest.max_surface_deviation \le 1.0e{-12}`
- `symmetry_h_l2_rel \le 2.0e{-11}`
- $\mathrm{cross\_target\_state\_agreement} \le 1.0e{-10}$ for each of `h`, `hu`, and `hv`

## 6. Test definitions
### 6-1. `l1_refinement_mass_and_positivity`
- `level`: `L1`
- `objective`: confirm mass conservation and positivity against refinement for `williamson_tc5_cone + linear_wave_x`, and the transverse momentum the cone generates, bounded on both sides.
- target cases:
  - `swe2d_ref_n032_sx000_sy000_dts100`
  - `swe2d_ref_n064_sx000_sy000_dts100`
  - `swe2d_ref_n128_sx000_sy000_dts100`
- `expected_outcome`: `pass`
- judgment conditions:
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of the case ($|\mathrm{run.n\_step}-\mathrm{n\_step}| \le 0.5$).
  - The depth-positivity judgment is applied. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 5.0e{-2}$.
  - The mass-conservation judgment is applied. The evaluation expression is `mass_drift_rel`, and the threshold is $\le 1.0e{-10}$.
  - The topographic-response judgment is applied. `transverse_momentum_l2_rel` is judged per case as the two-sided band of 5-5, as two judged quantities.
  - The momentum-conservation judgment is not applied. The non-application basis is "because the bottom-topography source term exchanges momentum; the response is judged by `transverse_momentum_l2_rel`".
  - The theoretical-comparison judgment is not applied. The non-application basis is "because it is a case with topography".
  - The translation-equivariance judgment is not applied. The non-application basis is "because the pair case is not run".
  - The lake-at-rest invariance judgment is not applied. The non-application basis is "because the initial condition is not `lake_at_rest`".
  - The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, and `hv`, and the threshold is $\le 1.0e{-10}$ for each.

### 6-2. `l1_refinement_linear_wave`
- `level`: `L1`
- `objective`: confirm the theoretical-solution error decrease with refinement for `flat + linear_wave_x`.
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
  - The theoretical-comparison judgment is applied. `analytic_h_l2_rel` is judged per case as the two-sided band of 5-5, as two judged quantities, and `convergence_order` requires $\ge 0.80$ for both.
  - The translation-equivariance judgment is not applied. The non-application basis is "because the pair case is not run".
  - The lake-at-rest invariance judgment is not applied. The non-application basis is "because the initial condition is not `lake_at_rest`".
  - The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, and `hv`, and the threshold is $\le 1.0e{-10}$ for each.

### 6-3. `l2_lake_at_rest_invariance`
- `level`: `L2`
- `objective`: confirm that the velocity and free surface are invariant for `williamson_tc5_cone + lake_at_rest`.
- target cases:
  - `swe2d_lake_n064_sx000_sy000_dts100`
- `expected_outcome`: `pass`
- judgment conditions:
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of the case ($|\mathrm{run.n\_step}-\mathrm{n\_step}| \le 0.5$).
  - The depth-positivity judgment is applied. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 5.0e{-2}$.
  - The mass-conservation judgment is applied. The evaluation expression is `mass_drift_rel`, and the threshold is $\le 1.0e{-10}$.
  - The momentum-conservation judgment is not applied. The non-application basis is "because the bottom-topography source term exchanges momentum".
  - The lake-at-rest invariance judgment is applied. It requires `lake_rest.max_velocity \le 1.0e{-12}` and `lake_rest.max_surface_deviation \le 1.0e{-12}`.
  - The theoretical-comparison judgment is not applied. The non-application basis is "because it is a topography lake-at-rest case".
  - The translation-equivariance judgment is not applied. The non-application basis is "because it is a single-case verification".
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
  - The topographic-response judgment is applied. `transverse_momentum_l2_rel` is judged as the two-sided band $[1.4e{-4},\ 1.8e{-4}]$ of 5-5, as two judged quantities.
  - The momentum-conservation judgment is not applied. The non-application basis is "because the bottom-topography source term exchanges momentum; the response is judged by `transverse_momentum_l2_rel`".
  - The theoretical-comparison judgment is not applied. The non-application basis is "because it is a topography long-time case".
  - The translation-equivariance judgment is not applied. The non-application basis is "because the pair case is not run".
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
  - The momentum-exchange judgment is applied. The evaluation expressions are `momx_drift_rel` and `momy_drift_rel`, each judged as the two-sided band $[1.3e{-6},\ 1.7e{-6}]$ of 5-5, as two judged quantities each.
  - The theoretical-comparison judgment is not applied. The non-application basis is "because the theoretical solution is not used in a case with topography".
  - The translation-equivariance judgment is not applied. The non-application basis is "because equivariance is not required in this test for a case with topography".
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
  - The translation-equivariance judgment is not applied. The non-application basis is "because the pair case is not run".
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
  - The theoretical-comparison judgment is applied. `analytic_h_l2_rel` is judged as the two-sided band $[1.1e{-4},\ 1.4e{-4}]$ of 5-5, as two judged quantities. `convergence_order` is not judged: the case is not a member of a refinement pair.
  - The translation-equivariance judgment is not applied. The non-application basis is "because the pair case is not run".
  - The lake-at-rest invariance judgment is not applied. The non-application basis is "because the initial condition is not `lake_at_rest`".
  - The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, and `hv`, and the threshold is $\le 1.0e{-10}$ for each.

### 6-9. `l1_linear_wave_y_rectangular_grid`
- `level`: `L1`
- `objective`: confirm, for `flat + linear_wave_y` on the rectangular grid of 2-5, conservation, depth positivity and the error against the theoretical solution at $t_{end}$; each substitution confined to the `y` direction measured in 2-6 to change `errors.analytic_h.l2_rel_tend` or `momy_drift_rel` fails a judgment of this test.
- target cases:
  - `swe2d_flat_ref_n064_sx000_sy000_dts100_recty`
- `expected_outcome`: `pass`
- judgment conditions:
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of the case ($|\mathrm{run.n\_step}-\mathrm{n\_step}| \le 0.5$).
  - The depth-positivity judgment is applied. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 5.0e{-2}$.
  - The mass-conservation judgment is applied. The evaluation expression is `mass_drift_rel`, and the threshold is $\le 1.0e{-10}$.
  - The momentum-conservation judgment is applied. The evaluation expressions are `momx_drift_rel` and `momy_drift_rel`, and the threshold for both is $\le 1.0e{-10}$.
  - The theoretical-comparison judgment is applied. `analytic_h_l2_rel` is judged as the two-sided band $[2.5e{-4},\ 3.2e{-4}]$ of 5-5, as two judged quantities. `convergence_order` is not judged: the case is not a member of a refinement pair.
  - The translation-equivariance judgment is not applied. The non-application basis is "because the pair case is not run".
  - The lake-at-rest invariance judgment is not applied. The non-application basis is "because the initial condition is not `lake_at_rest`".
  - The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, and `hv`, and the threshold is $\le 1.0e{-10}$ for each.

### 6-10. `l3_topography_time_refinement_pair`
- `level`: `L3`
- `objective`: exclude, through the pair of 2-8, a source term evaluated once per step instead of at each stage state of the Controlled Spec §5 step 5, the one-call step, and a `dt` that is not the §3 one; the substitutions 2-8 states the pair does not exclude are judged by 6-1.
- pair cases:
  - `reference` is `swe2d_ref_n064_sx000_sy000_dts100`
  - `halved` is `swe2d_ref_n064_sx000_sy000_dts100_half`
- `expected_outcome`: `pass`
- judgment conditions:
  - The time-refinement judgment is applied. The evaluation expression is `errors.time_pair.hv_l2_rel` (carried by the halved case), and the band is $[7.9e{-6},\ 9.7e{-6}]$ as two judged quantities.
  - The `CFL` judgment is applied to both cases. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of each case ($|\mathrm{run.n\_step}-\mathrm{n\_step}| \le 0.5$).
  - The depth-positivity judgment is applied to both cases. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 5.0e{-2}$.
  - The mass-conservation judgment is applied to both cases. The evaluation expression is `mass_drift_rel`, and the threshold is $\le 1.0e{-10}$.
  - The topographic-response judgment is applied to both cases. `transverse_momentum_l2_rel` is judged as the `nx=64` band $[1.6e{-2},\ 2.1e{-2}]$ of 5-5, as two judged quantities.
  - The momentum-conservation judgment is not applied. The non-application basis is "because the bottom-topography source term exchanges momentum; the response is judged by `transverse_momentum_l2_rel`".
  - The theoretical-comparison judgment is not applied. The non-application basis is "because it is a case with topography".
  - The translation-equivariance judgment is not applied. The non-application basis is "because the translation pair is not run".
  - The lake-at-rest invariance judgment is not applied. The non-application basis is "because the initial condition is not `lake_at_rest`".
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
