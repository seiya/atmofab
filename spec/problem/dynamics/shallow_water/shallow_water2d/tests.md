# Tests: 2D shallow water equation (verification input / judgment conditions)

## 0. Meta information
- `status`: `draft`
- `test_profile_id`: `shallow_water2d_baseline`
- `test_profile_version`: `0.4.0`
- `spec_ref.spec_kind`: `problem`
- `spec_ref.spec_id`: `shallow_water2d`
- `spec_ref.spec_version`: `0.4.5`
- `spec_ref.controlled_spec_path`: `spec/problem/dynamics/shallow_water/shallow_water2d/controlled_spec.md`

## 1. Test purpose
This suite verifies, for the discrete implementation of the 2D shallow water equation including bottom topography, conservation, lake-at-rest invariance, stability under topographic forcing, agreement with the theoretical solution — bounded on both sides, since the fully fixed `p0` discretization makes it a property of the scheme — translation equivariance, a rectangular grid, on which an exchange of the two extents, the two domain lengths or the two spacings is not the identity, and the `CFL` guard. The conservation judgment targets mass for all cases, and momentum for the `topography_profile=flat` cases. The judgment targets are `L0` to `L3`, and include an expected failure (`xfail`).

## 2. Input-defaulting rules
### 2-1. Basic constants
- The domain lengths are `L_x=L_y=1.0` (the rectangular case of 2-5 carries `L_y=0.5`, `ny=48`).
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
Only when `topography_profile=flat` and `initial_profile=linear_wave_x`, use the reference solution of the linearized shallow water equation. The reference solution is the following.
$$
h_{ref}(x,t)=H_0+\eta_0\sin\left(2\pi\left(\frac{x}{L_x}-\frac{c_0 t}{L_x}-\mathrm{shift\_x\_fraction}\right)\right)
$$
A theoretical-agreement judgment for variables other than `h` is not required in this suite.

### 2-5. The rectangular case
Every case of §4 but one carries $nx=ny$ and `L_x=L_y`, so an implementation that exchanges the two extents, the two domain lengths or the two spacings — an array declared `(ny, nx)`, a loop bounded by `ny` where `nx` is meant, `dx` where `dy` is meant, an `rhs` that reads its stage state as `(ncomp, ny, nx)` — gives the numbers of a correct one on those cases. `swe2d_flat_ref_n064_sx000_sy000_dts100_rect` (4-3) carries `nx=64`, `ny=48`, `L_x=1.0` and `L_y=0.5`, so $dx=1/64$ and $dy=1/96$, and no exchange within any of the three pairs is the identity on a derived quantity. The §3 procedure gives $\mathrm{n\_step}=223$ and $dt=8.969\times10^{-4}$. The wave of `linear_wave_x` is uniform in `y`, so `ny` and `L_y` enter `errors.analytic_h.l2_rel_tend` through `dt` alone: its reference value is `1.2427e-4`, measured on the reference implementation of 5-5, equal to the value of `swe2d_flat_ref_n064_sx000_sy000_dts100` (`1.2428e-4`) to four digits. Measured on the same implementation with one substitution at a time: `L_x` and `L_y` exchanged gives `1.10e-3`; `dx` and `dy` exchanged in the interface-flux difference gives `1.03e-3`; the cell centres evaluated with the spacings exchanged at setup gives `6.45e-4`; an `rhs` that reads the stage state as `(ncomp, ny, nx)` gives `7.07e-4`; an `x` loop bounded by `ny` gives `9.37e-4` (and `1.3e-4` in `mass_drift_rel`). Each lies outside the band of 5-5. `dx` and `dy` exchanged in the dt rule alone gives the reference value and the §3 step count: the rule of §3 is symmetric in the two spacings when $|u|,|v|\ll c$, so that exchange changes no judged quantity.

## 3. Execution-control rules
- $t_{start}=0.0$ and $t_{end}=0.2$.
- `dt` is decided by the following procedure.
  1. Evaluate $\lambda_0=\max_{i,j}\left((|u_{i,j}|+\sqrt{gh_{i,j}})/dx+(|v_{i,j}|+\sqrt{gh_{i,j}})/dy\right)$ at the initial time.
  2. $\mathrm{dt\_raw}=\mathrm{dt\_scale}\cdot \mathrm{cfl\_target}/\lambda_0$.
  3. $\mathrm{n\_step}=\lceil (t_{end}-t_{start})/\mathrm{dt\_raw}\rceil$.
  4. $dt=(t_{end}-t_{start})/\mathrm{n\_step}$.
- $\mathrm{cfl\_target}=0.45$.
- The stop condition is $n=\mathrm{n\_step}$.

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

### 5-3. `N/A` rule
- When a diagnostic item is incomputable or non-applicable, make the output value `null` and require `reason_na`.
- `errors.analytic_h.l2_rel_tend` is `N/A` for anything other than `topography_profile=flat` and `initial_profile=linear_wave_x`.
- `errors.symmetry_h_l2_rel` is `N/A` for anything other than a translation-pair evaluation.
- `momx_drift_rel` and `momy_drift_rel` are `N/A` for anything other than `topography_profile=flat`.
- `invariants.lake_rest.*` is `N/A` for anything other than `initial_profile=lake_at_rest`.

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

The theoretical-comparison error (`flat + linear_wave_x` only) is defined by the following.
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

`cross_target_state_agreement` compares this target's numerical solution with every other target's certified variant of this node, case by case (`docs/TESTS.md` §Cross-target judgment). For each of the state variables `h`, `hu`, and `hv` at $t_{end}$ of a case, with $q$ that variable and $q^{ref}$ the same variable at $t_{end}$ of the same case in the other variant,
$$
\mathrm{cross\_target\_state\_agreement}=\frac{\max_{i,j} |q_{i,j}-q^{ref}_{i,j}|}{N_q}
$$
The normaliser of `h` is $N_h=\max(\max_{i,j}|h_{i,j}|,\ \max_{i,j}|h^{ref}_{i,j}|,\ 1)$. The two momentum components share one normaliser, the momentum scale of the two variants, $N_{hu}=N_{hv}=\max(\max_{i,j}|hu_{i,j}|,\ \max_{i,j}|hv_{i,j}|,\ \max_{i,j}|hu^{ref}_{i,j}|,\ \max_{i,j}|hv^{ref}_{i,j}|,\ 1)$: a component that is a small residue of the flow (the cross-stream `hv` of a zonal jet) carries the rounding of the whole momentum field, so its own maximum is not its rounding scale. It is evaluated by the host from the captured state, against each other variant separately and for each variable separately; it is not a `diagnostics.json` field, and on a target with no other certified variant it holds vacuously.

### 5-5. Default thresholds
The thresholds of `analytic_h_l2_rel` are calibrated against an independent reference implementation of the Controlled Spec §5 scheme (issue #491, calibration comment), which reproduces the values of the certified variants of this node to five digits.

- $\mathrm{cfl.max} \le 1.0$
- $h_{min} \ge 5.0e{-2}$
- $\mathrm{mass\_drift\_rel} \le 1.0e{-10}$
- $\mathrm{momx\_drift\_rel} \le 1.0e{-10}$
- $\mathrm{momy\_drift\_rel} \le 1.0e{-10}$
- `analytic_h_l2_rel` is judged as a two-sided band (6-2, 6-8). The Controlled Spec §5 fixes every term of the discretization (`p0` interface states, the Rusanov flux, the periodic ghost, the two-stage `SSPRK2` composition of its step 5) and §3 fixes `dt`, so the error against the linear-wave solution at $t_{end}$ is a property of the discretization — its truncation error and numerical diffusion — and not a free quantity: a value below the band is not a better scheme, it is a run that did not perform the §5 update (a state that holds the theoretical solution at $t_{end}$ without the update has zero error and passes an upper bound alone), and a value above it is a state left at its initial value (`1.304e-3` on every case of `swe2d_flat_ref`), a stopped update (half the steps gives `1.13e-3` at `nx=64`) or a wrong one. The value moves by round-off alone under a conforming implementation — the certified variants of this node agree to `3e-12` relative, and `dt_scale` from `0.5` to `2.0` moves it by at most `0.12%` — so the bounds are `0.9` and `1.1` times the reference value, rounded outward to two digits. The lower bound and the upper bound are two DISTINCT judged quantities of the test — each names its own `quantity` and each requires its own host-evaluated corroborant over the captured state — not two thresholds on one quantity: a corroborant that reads only the upper bound leaves the lower bound to the checks module's own report, and a state that holds the theoretical solution with a reported value inside the band would then pass. For `swe2d_flat_ref` the band is $[2.0e{-4},\ 2.5e{-4}]$ for `nx=32` (reference `2.2671e-4`), $[1.1e{-4},\ 1.4e{-4}]$ for `nx=64` (reference `1.2428e-4`), and $[5.8e{-5},\ 7.2e{-5}]$ for `nx=128` (reference `6.5158e-5`); for the rectangular case `swe2d_flat_ref_n064_sx000_sy000_dts100_rect` it is $[1.1e{-4},\ 1.4e{-4}]$ (reference `1.2427e-4`, 2-5). The one-call step $U^{n+1}=U^n+\Delta t\,L(U^n)$ (the forward-Euler step the Controlled Spec §5 step 5 excludes) gives `0.78` to `0.83` of the reference value on every case and fails the lower bound; a gravitational acceleration in error by `5%` moves the value outside the band on `nx=128` in either direction, and one in error by `1%` on no case.
- `convergence_order` uses $p=\log(e_{coarse}/e_{fine})/\log(2)$ over `analytic_h_l2_rel`, and requires $\ge 0.80$ for both `n32_to_n64` and `n64_to_n128`. It is accumulated over the three cases of `swe2d_flat_ref` that 6-2 judges and emitted as a per-case field of the finer case of each pair; the override case `swe2d_flat_ref_n064_sx000_sy000_dts100_rect` is not a member of a refinement pair and omits the field.
- `lake_rest.max_velocity \le 1.0e{-12}`
- `lake_rest.max_surface_deviation \le 1.0e{-12}`
- `symmetry_h_l2_rel \le 2.0e{-11}`
- $\mathrm{cross\_target\_state\_agreement} \le 1.0e{-10}$ for each of `h`, `hu`, and `hv`

## 6. Test definitions
### 6-1. `l1_refinement_mass_and_positivity`
- `level`: `L1`
- `objective`: confirm mass conservation and positivity against refinement for `williamson_tc5_cone + linear_wave_x`.
- target cases:
  - `swe2d_ref_n032_sx000_sy000_dts100`
  - `swe2d_ref_n064_sx000_sy000_dts100`
  - `swe2d_ref_n128_sx000_sy000_dts100`
- `expected_outcome`: `pass`
- judgment conditions:
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$.
  - The depth-positivity judgment is applied. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 5.0e{-2}$.
  - The mass-conservation judgment is applied. The evaluation expression is `mass_drift_rel`, and the threshold is $\le 1.0e{-10}$.
  - The momentum-conservation judgment is not applied. The non-application basis is "because the bottom-topography source term exchanges momentum".
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
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$.
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
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$.
  - The depth-positivity judgment is applied. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 5.0e{-2}$.
  - The mass-conservation judgment is applied. The evaluation expression is `mass_drift_rel`, and the threshold is $\le 1.0e{-10}$.
  - The momentum-conservation judgment is not applied. The non-application basis is "because the bottom-topography source term exchanges momentum".
  - The lake-at-rest invariance judgment is applied. It requires `lake_rest.max_velocity \le 1.0e{-12}` and `lake_rest.max_surface_deviation \le 1.0e{-12}`.
  - The theoretical-comparison judgment is not applied. The non-application basis is "because it is a topography lake-at-rest case".
  - The translation-equivariance judgment is not applied. The non-application basis is "because it is a single-case verification".
  - The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, and `hv`, and the threshold is $\le 1.0e{-10}$ for each.

### 6-4. `l2_long_run_mass_conservation`
- `level`: `L2`
- `objective`: confirm that mass and depth positivity are maintained in long-time integration of `williamson_tc5_cone + linear_wave_x`.
- target cases:
  - `swe2d_ref_n064_sx000_sy000_dts100_tend500`
- `expected_outcome`: `pass`
- judgment conditions:
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$.
  - The depth-positivity judgment is applied. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 5.0e{-2}$.
  - The mass-conservation judgment is applied. The evaluation expression is `mass_drift_rel`, and the threshold is $\le 5.0e{-10}$.
  - The momentum-conservation judgment is not applied. The non-application basis is "because the bottom-topography source term exchanges momentum".
  - The theoretical-comparison judgment is not applied. The non-application basis is "because it is a topography long-time case".
  - The translation-equivariance judgment is not applied. The non-application basis is "because the pair case is not run".
  - The lake-at-rest invariance judgment is not applied. The non-application basis is "because the initial condition is not `lake_at_rest`".
  - The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, and `hv`, and the threshold is $\le 1.0e{-10}$ for each.

### 6-5. `l3_topography_forced_response_stability`
- `level`: `L3`
- `objective`: confirm the numerical stability under topographic forcing for `williamson_tc5_cone + oblique_mode`.
- target cases:
  - `swe2d_resp_n064_sx000_sy000_dts100`
- `expected_outcome`: `pass`
- judgment conditions:
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$.
  - The depth-positivity judgment is applied. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 5.0e{-2}$.
  - The mass-conservation judgment is applied. The evaluation expression is `mass_drift_rel`, and the threshold is $\le 1.0e{-10}$.
  - The momentum-conservation judgment is not applied. The non-application basis is "because the bottom-topography source term exchanges momentum".
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
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$.
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
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$.
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
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$.
  - The depth-positivity judgment is applied. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 5.0e{-2}$.
  - The mass-conservation judgment is applied. The evaluation expression is `mass_drift_rel`, and the threshold is $\le 1.0e{-10}$.
  - The momentum-conservation judgment is applied. The evaluation expressions are `momx_drift_rel` and `momy_drift_rel`, and the threshold for both is $\le 1.0e{-10}$.
  - The theoretical-comparison judgment is applied. `analytic_h_l2_rel` is judged as the two-sided band $[1.1e{-4},\ 1.4e{-4}]$ of 5-5, as two judged quantities. `convergence_order` is not judged: the case is not a member of a refinement pair.
  - The translation-equivariance judgment is not applied. The non-application basis is "because the pair case is not run".
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
