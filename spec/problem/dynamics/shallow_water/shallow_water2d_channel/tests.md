# Tests: 2D shallow water channel (verification input / judgment conditions)

## 0. Meta information
- `status`: `draft`
- `test_profile_id`: `shallow_water2d_channel_baseline`
- `test_profile_version`: `0.1.0`
- `spec_ref.spec_kind`: `problem`
- `spec_ref.spec_id`: `shallow_water2d_channel`
- `spec_ref.spec_version`: `0.1.0`
- `spec_ref.controlled_spec_path`: `spec/problem/dynamics/shallow_water/shallow_water2d_channel/controlled_spec.md`

## 1. Test purpose
This suite verifies, for the discrete implementation of the 2D shallow water equation on a channel with the Coriolis force, the discrete initial state against the analytic profiles, the error against the steady analytic solution — bounded on both sides, since the fully fixed discretization makes it a property of the scheme — and its decrease with refinement (Williamson et al. (1992) Test Cases 2 and 3 on the channel), mass conservation, depth positivity, the wall-normal velocity, a long-time integration, the `x`-direction dynamics through an `x`-perturbed pair (translation equivariance and the decay of the perturbation's `x`-structure, which an update without the `x`-interface flux leaves unchanged), and the `CFL` guard. The judgment targets are `L0` to `L3`, and include an expected failure (`xfail`).

## 2. Input-defaulting rules
### 2-1. Basic constants
The constants are those of the Controlled Spec §6, in SI units: `g=9.80616`, `Omega=7.292e-5`, `a=6.37122e6`, `gh0=2.94e4` (`h0 = gh0/g = 2998.1155...`), `u0 = 2*pi*a/(12*86400) = 38.6107...`, `theta0 = pi/4`, so `f0 = 2*Omega*sin(theta0) = 1.03124...e-4` and `beta = 2*Omega*cos(theta0)/a = 1.61860...e-11`. The ellipses mark derived values: the case inputs carry the constants `g`, `Omega`, `a`, `gh0` and `theta0` (the double-precision value of `pi/4`) as numbers, and `h0`, `u0`, `f0`, `beta` are evaluated from them by the expressions above wherever they are used — by the runner and by a judgment that evaluates a reference field alike; no rounded literal of a derived value is an input.

### 2-2. Coriolis profile
`coriolis_profile=beta_plane` in every case: $f(y)=f_0+\beta\,(y-L_y/2)$ at the cell centres.

### 2-3. Flow profile (initial condition)
- `tc2_zonal_uniform`: $u=u_0$, $v=0$, $h(y)=h_0-\dfrac{u_0}{g}\left[f_0\,(y-L_y/2)+\dfrac{\beta}{2}\,(y-L_y/2)^2\right]$, with `L_x=L_y=6.0e6`.
- `tc3_compact_jet`: $y_b=L_y/8$, $y_e=7L_y/8$, $x_e=0.3$, $s=x_e\,(y-y_b)/(y_e-y_b)$, $b(s)=0$ ($s\le0$), $e^{-1/s}$ ($s>0$), $u(y)=u_0\,b(s)\,b(x_e-s)\,e^{4/x_e}$, $v=0$, $h(y)=h_0-\dfrac1g\int_0^y f\,u\,dy'$ by the composite Simpson quadrature of the Controlled Spec §6, with `L_x=L_y=8.0e6`. The branch-free form $b(s)=\exp(-1/\max(s,10^{-300}))$ of the Controlled Spec §6 is equal to $b(s)$ at every cell centre of the sweep.
- `tc2_zonal_perturbed`: $h(x,y)=h_{TC2}(y)+\eta_0\sin\left(2\pi\,(x/L_x-\mathrm{shift\_x\_fraction})\right)$ with $h_{TC2}$ the `tc2_zonal_uniform` depth, $u=u_0$, $v=0$, `eta0=30.0` (`m`), with `L_x=L_y=6.0e6`.

### 2-4. Theoretical solution (with applicability condition)
`tc2_zonal_uniform` and `tc3_compact_jet` are steady solutions of the continuous equations, so the reference at $t_{end}$ is the initial state: for `tc2_zonal_uniform` the closed form of 2-3 evaluated at the cell centres, $h_{ref}(y)=h(y)$; for `tc3_compact_jet` the discrete initial state itself, $h_{ref}=h(t=0)$ (its $h$ has no closed form). The theoretical-agreement judgment at $t_{end}$ targets `h`; a judgment for `hu` / `hv` against the reference at $t_{end}$ is not required in this suite, and the wall-normal velocity is judged by its own metric (2-5). `tc2_zonal_perturbed` has no steady reference; it is judged by 2-6. The initial state is judged against its closed form for every profile: `h` for `tc2_zonal_uniform` and `tc2_zonal_perturbed` (2-3, at the cell centres), `u` for all three, `v=0` for all three, and for `tc3_compact_jet` — whose `h` is defined by the quadrature — the depth of the rows south of the jet, where $u=0$ on $[0,y]$ and the integral is exactly zero, so $h=h_0$ and it is the maximum of $h$ over the domain.

### 2-5. Wall-normal velocity
The exact solution has $v=0$ everywhere. The mirror ghost yields a non-zero $v$ of order $dy$ in the wall-adjacent rows (the boundary `component`'s §2), so a "zero `hv` at the wall" judgment is not applicable to this discretization; the judged quantity is $\max_{i,j}|v_{i,j}(t_{end})|$ over the whole domain, with a per-case threshold decreasing with refinement.

### 2-6. `x`-direction dynamics (translation equivariance and `x`-structure decay)
The `x`-uniform profiles exercise no `x`-interface flux and no periodic `x` mapping (Controlled Spec §5, zonal uniformity), so they cannot detect an update that omits either. `tc2_zonal_perturbed` does, in two ways. First, `f` depends on `y` alone and `x` is periodic, so a run whose initial state is shifted by a whole number of cells in `x` (`shift_x_fraction` such that $\mathrm{shift\_x\_fraction}\cdot nx$ is an integer) is the unshifted run shifted by the same number of cells, up to round-off; the pair is judged by `errors.symmetry_h.l2_rel`. Second, the wavenumber-one perturbation is a gravity-wave excitation that the `p0` scheme's numerical diffusion damps as it propagates in `x`; its `x`-structure at $t_{end}$ relative to $t=0$ is measured by `xstructure.decay_ratio` (5-4), whose value is a property of the fixed discretization at the case's resolution (reference `0.27` at `nx=64`), while an update without the `x`-interface flux leaves the `x`-structure unchanged (ratio `1.0` to three digits — the columns evolve independently) and an outflow `x` boundary breaks mass conservation and the equivariance.

## 3. Execution-control rules
- $t_{start}=0$ and $t_{end}=86400$ (`s`; one day), unless a case overrides `t_end`.
- `dt` is decided by the following procedure.
  1. Evaluate $\lambda_0=\max_{i,j}\left((|u_{i,j}|+\sqrt{gh_{i,j}})/dx+(|v_{i,j}|+\sqrt{gh_{i,j}})/dy\right)$ at the initial time.
  2. $\mathrm{dt\_raw}=\mathrm{dt\_scale}\cdot \mathrm{cfl\_target}/\lambda_0$.
  3. $\mathrm{n\_step}=\lceil (t_{end}-t_{start})/\mathrm{dt\_raw}\rceil$.
  4. $dt=(t_{end}-t_{start})/\mathrm{n\_step}$.
- $\mathrm{cfl\_target}=0.45$.
- The stop condition is $n=\mathrm{n\_step}$.
- The output times are $0,\ 21600,\ 43200,\ 64800,\ 86400$ (`s`); a case with an overridden `t_end` outputs at the same interval of $21600$ up to its $t_{end}$.

## 4. Case-expansion rules
### 4-1. family definition
The `sweep` and fixed values per `family` are defined below.

| family_id | purpose | sweep | fixed |
| --- | --- | --- | --- |
| `chan_tc2_ref` | refinement_for_steady_geostrophic_flow | $nx=ny\in\{32,64,128\}$ | `flow_profile=tc2_zonal_uniform`, `coriolis_profile=beta_plane`, `L_x=L_y=6.0e6`, `dt_scale=1.0` |
| `chan_tc3_ref` | refinement_for_compact_jet | $nx=ny\in\{32,64,128\}$ | `flow_profile=tc3_compact_jet`, `coriolis_profile=beta_plane`, `L_x=L_y=8.0e6`, `dt_scale=1.0` |
| `chan_tc2_xpert` | x_translation_equivariance_and_wave_decay | $nx=ny=64$ | `flow_profile=tc2_zonal_perturbed`, `coriolis_profile=beta_plane`, `L_x=L_y=6.0e6`, `eta0=30.0`, `shift_x_fraction=0.0`, `dt_scale=1.0` |
| `chan_guard` | expected_failure_for_cfl | $nx=ny=32,\ \mathrm{dt\_scale}=2.40$ | `flow_profile=tc2_zonal_uniform`, `coriolis_profile=beta_plane`, `L_x=L_y=6.0e6` |

### 4-2. `case_id` generation rule
- The template is `{family}_n{nx:03d}_dts{dts_pct:03d}`.
- `dts_pct` is decided by $\mathrm{round}(100\cdot \mathrm{dt\_scale})$.
- The expansion order is fixed in the order `family`, `nx`, `dt_scale`.

### 4-3. Explicit-override cases
- Add the `case_id` `chan_tc2_ref_n128_dts100_tend5d`.
- The `base_case_id` references `chan_tc2_ref_n128_dts100`, and overrides only `t_end` to `432000` (`s`; five days).
- Add the `case_id` `chan_tc2_xpert_n064_dts100_sx025`.
- The `base_case_id` references `chan_tc2_xpert_n064_dts100`, and overrides only `shift_x_fraction` to `0.25` (a shift of 16 cells at `nx=64`).

## 5. Diagnostics contract
### 5-1. Artifacts
- The diagnostic output file is `diagnostics.json`.
- The judgment output file is `verdict.json`.

### 5-2. Required diagnostic items
`diagnostics.json` requires the following fields, each a scalar.
- `cfl.max`
- `conserved.mass.initial`
- `conserved.mass.final`
- `conserved.momentum_x.initial`
- `conserved.momentum_x.final`
- `metrics.mass_drift_rel`
- `extrema.h.min`
- `extrema.v.max_abs`
- `errors.initial_h.linf_rel`
- `errors.initial_h.south_rel`
- `errors.initial_u.linf_rel`
- `errors.initial_v.linf`
- `errors.symmetry_h.l2_rel`
- `xstructure.decay_ratio`
- `errors.steady_h.l2_rel_tend`
- `errors.steady_h.linf_tend`
- `convergence.n032_to_n064.l2_order`
- `convergence.n064_to_n128.l2_order`

### 5-3. `N/A` rule
- When a diagnostic item is incomputable or non-applicable, make the output value `null` and require `reason_na`.
- `errors.initial_h.linf_rel` is `N/A` for `flow_profile=tc3_compact_jet` (its initial `h` is defined by the quadrature and has no independent closed form); it is computed for `tc2_zonal_uniform` and `tc2_zonal_perturbed`.
- `errors.initial_h.south_rel` is `N/A` for anything other than `flow_profile=tc3_compact_jet`.
- `errors.initial_u.linf_rel` and `errors.initial_v.linf` are computed for every profile (`u(y)` is `u0` for the two `tc2_*` profiles and the closed form of 2-3 for `tc3_compact_jet`; `v=0` for all three).
- `errors.symmetry_h.l2_rel` is `N/A` for anything other than the shifted case of a translation pair; it is carried by `chan_tc2_xpert_n064_dts100_sx025`, which sorts after its base case, and reads the base case's final state.
- `xstructure.decay_ratio` is `N/A` for anything other than `flow_profile=tc2_zonal_perturbed`.
- The steady-solution fields `errors.steady_h.*` are `N/A` for `flow_profile=tc2_zonal_perturbed`, which has no steady reference.
- `convergence.n032_to_n064.l2_order` is carried by the `nx=64` case of each refinement family and `convergence.n064_to_n128.l2_order` by its `nx=128` case; the other cases omit the field (5-4).
- `conserved.momentum_x.*` are reported and not judged: the Coriolis source exchanges momentum between `hu` and `hv`, so $P^x$ is not conserved.
- `errors.steady_h.linf_tend` is reported and not judged; the steady-solution judgment is on `errors.steady_h.l2_rel_tend`.

### 5-4. Definition of the metrics
The relative mass-drift value is defined by the following, and emitted as the field `metrics.mass_drift_rel`.
$$
\mathrm{mass\_drift\_rel}=
\frac{|M_{end}-M_0|}{\max(|M_0|,1e{-14})},
\quad
M=\sum_{i,j} h_{i,j}\,dx\,dy
$$
Here $M_0$ is the field `conserved.mass.initial` and $M_{end}$ is `conserved.mass.final`. `conserved.momentum_x.*` is $P^x=\sum_{i,j}(hu)_{i,j}\,dx\,dy$ at the two times.

The initial-state errors are defined by the following, with $h_{ref}$ the closed form of 2-3 at the cell centres ($h_{ref}(y_j)$ for `tc2_zonal_uniform`, $h_{ref}(x_i,y_j)$ for `tc2_zonal_perturbed`), $u(y)$ the closed form of 2-3 ($u_0$ for the two `tc2_*` profiles), $u_{i,j}=(hu)_{i,j}/h_{i,j}$ and $v_{i,j}=(hv)_{i,j}/h_{i,j}$.
$$
\mathrm{initial\_h\_linf\_rel}=\frac{\max_{i,j}|h_{i,j}(0)-h_{ref,i,j}|}{\max_{i,j}|h_{ref,i,j}|},\qquad
\mathrm{initial\_h\_south\_rel}=\frac{\left|\max_{i,j}h_{i,j}(0)-h_0\right|}{h_0}
\quad(\texttt{tc3\_compact\_jet})
$$
$$
\mathrm{initial\_u\_linf\_rel}=\frac{\max_{i,j}|u_{i,j}(0)-u(y_j)|}{u_0},\qquad
\mathrm{initial\_v\_linf}=\max_{i,j}|v_{i,j}(0)|
$$
They are emitted as `errors.initial_h.linf_rel`, `errors.initial_h.south_rel`, `errors.initial_u.linf_rel` and `errors.initial_v.linf` (`m/s`). `initial_h_south_rel` pins the constant of integration of the `tc3_compact_jet` quadrature: $u=0$ on $[0,y_j]$ for every row south of the jet, the integral is exactly zero there, $h=h_0$, and $h$ is non-increasing northward ($f\,u\ge0$), so the maximum of $h(0)$ over the domain is $h_0$.

The steady-solution errors are defined by the following, with $h_{ref}$ as in 2-4.
$$
\mathrm{steady\_h\_l2\_rel}=
\frac{\|h(t_{end})-h_{ref}\|_2}{\|h_{ref}\|_2},\qquad
\mathrm{steady\_h\_linf}=\max_{i,j}\left|h_{i,j}(t_{end})-h_{ref,i,j}\right|
$$
They are emitted as `errors.steady_h.l2_rel_tend` and `errors.steady_h.linf_tend` (`m`).

The wall-normal velocity metric is $\mathrm{v\_max\_abs}=\max_{i,j}|(hv)_{i,j}(t_{end})/h_{i,j}(t_{end})|$ (`m/s`), emitted as `extrema.v.max_abs`.

The translation-equivariance error of the `tc2_zonal_perturbed` pair is defined by the following, with $h_{base}$ the final state of the base case (`shift_x_fraction=0`) and $\mathrm{shift}(\cdot,+\Delta i)$ the cyclic shift by $\Delta i=\mathrm{shift\_x\_fraction}\cdot nx$ cells along `x`; it is emitted as `errors.symmetry_h.l2_rel` on the shifted case.
$$
\mathrm{symmetry\_h\_l2\_rel}=
\frac{\|h_{shifted}(t_{end})-\mathrm{shift}(h_{base}(t_{end}),+\Delta i)\|_2}{\|h_{base}(t_{end})\|_2}
$$

The `x`-structure decay ratio is defined by the following, with $\mathrm{shift}(\cdot, nx/2)$ the cyclic shift by half the domain along `x`, which cancels the `x`-uniform part of $h$ exactly and doubles the odd `x`-harmonics, the wavenumber-one perturbation among them; it is emitted as `xstructure.decay_ratio` on every `tc2_zonal_perturbed` case.
$$
\mathrm{decay\_ratio}=\frac{\|h(t_{end})-\mathrm{shift}(h(t_{end}),nx/2)\|_2}{\|h(0)-\mathrm{shift}(h(0),nx/2)\|_2}
$$

`convergence_order` is a cross-case reduction over `errors.steady_h.l2_rel_tend`, using $p=\log(e_{coarse}/e_{fine})/\log(2)$, and is accumulated only over the target cases of the test that judges it, within one family. It is emitted as a per-case field of the finer case of each pair: `chan_tc2_ref_n064_dts100` and `chan_tc3_ref_n064_dts100` carry `convergence.n032_to_n064.l2_order`, and `chan_tc2_ref_n128_dts100` and `chan_tc3_ref_n128_dts100` carry `convergence.n064_to_n128.l2_order`. The cases preceding the one that completes a reduction omit that field; the override case `chan_tc2_ref_n128_dts100_tend5d` is not a member of a pair and omits both.

### 5-5. Default thresholds
The thresholds are calibrated against an independent reference implementation of the Controlled Spec §5 scheme (issue #265, calibration comment): the per-case error thresholds are about 1.5 times the reference values, the orders are the reference orders minus 0.1 to 0.15. The reference `l2` orders are 0.89 / 0.94 for `tc2_zonal_uniform` and 0.49 / 0.57 for `tc3_compact_jet` (the jet's numerical diffusion dominates under `p0`, so its observed order is below one).
- $\mathrm{cfl.max} \le 1.0$
- `extrema.h.min` is $\ge 1.0e3$ for `tc2_zonal_uniform` and $\ge 2.0e3$ for `tc3_compact_jet` (`m`)
- $\mathrm{mass\_drift\_rel} \le 1.0e{-10}$ ($\le 5.0e{-10}$ for the five-day case)
- `initial_h_linf_rel` $\le 1.0e{-9}$, `initial_h_south_rel` $\le 1.0e{-9}$, `initial_u_linf_rel` $\le 1.0e{-9}$, `initial_v_linf` $\le 1.0e{-12}$ (`m/s`)
- `steady_h_l2_rel` is judged as a two-sided band. The Controlled Spec §5 fixes every term of the discretization (`p0` reconstruction, Rusanov flux, mirror ghost, explicit Coriolis source, `RK4`, the `dt` rule of §3), so the error against the steady solution at $t_{end}$ is a property of that discretization — its numerical diffusion — and not a free quantity: a value below the band is not a better scheme, it is a run that did not perform the §5 update (a state left at its initial value has zero error against a steady solution and passes an upper bound alone). The upper bound is about 1.5 times the reference value and the lower bound about 0.5 times it. The lower bound and the upper bound are two DISTINCT judged quantities of the test — each names its own `quantity` and each requires its own host-evaluated corroborant over the captured state — not two thresholds on one quantity: a corroborant that reads only the upper bound leaves the lower bound to the checks module's own report, and a state left at its initial value with a reported value inside the band would then pass. For `chan_tc2_ref` the band is $[2.6e{-2},\ 8.0e{-2}]$ for `nx=32`, $[1.4e{-2},\ 4.5e{-2}]$ for `nx=64`, and $[7.3e{-3},\ 2.3e{-2}]$ for `nx=128`; for `chan_tc3_ref` it is $[1.7e{-2},\ 5.5e{-2}]$, $[1.2e{-2},\ 4.0e{-2}]$, and $[8.2e{-3},\ 2.6e{-2}]$; for the five-day case it is $[3.2e{-2},\ 9.5e{-2}]$
- `convergence_order` requires $\ge 0.75$ for both pairs of `chan_tc2_ref` and $\ge 0.35$ for both pairs of `chan_tc3_ref`
- `v_max_abs` (`m/s`) for `chan_tc2_ref` is $\le 3.0$ for `nx=32`, $\le 2.0$ for `nx=64`, and $\le 1.3$ for `nx=128`; for `chan_tc3_ref` it is $\le 0.30$, $\le 0.28$, and $\le 0.20$; for the five-day case it is $\le 1.0$
- `symmetry_h_l2_rel` $\le 2.0e{-11}$ (the shift is a whole number of cells and every operation of the scheme is translation-covariant, so the residual is round-off)
- `decay_ratio` is judged as a two-sided band $[0.13,\ 0.55]$ at `nx=64` (reference `0.27`; the bounds are about 0.5 and 2 times it). The lower and the upper bound are two distinct judged quantities, each with its own host-evaluated corroborant. An update without the `x`-interface flux gives `1.0` and fails the upper bound.

## 6. Test definitions
### 6-1. `l0_tc2_initial_state_matches_analytic`
- `level`: `L0`
- `objective`: confirm that the discrete initial state of `tc2_zonal_uniform` is the closed form of 2-3 at the cell centres.
- target cases:
  - `chan_tc2_ref_n064_dts100`
- `expected_outcome`: `pass`
- judgment conditions:
  - The initial-state judgment is applied. The evaluation expressions are `errors.initial_h.linf_rel` with the threshold $\le 1.0e{-9}$, `errors.initial_u.linf_rel` with the threshold $\le 1.0e{-9}$, and `errors.initial_v.linf` with the threshold $\le 1.0e{-12}$.
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$.
  - The depth-positivity judgment is applied. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 1.0e3$.
  - The mass-conservation judgment is not applied. The non-application basis is "because the test judges the initial state and the conservation judgment belongs to 6-3".
  - The theoretical-comparison judgment is not applied. The non-application basis is "because the test judges the initial state and the steady-solution judgment belongs to 6-3".
  - The wall-normal-velocity judgment at $t_{end}$ is not applied. The non-application basis is "because the test judges the initial state; the initial $v$ is judged by `errors.initial_v.linf` above".
  - The translation-equivariance judgment is not applied. The non-application basis is "because the pair case is not run".
  - The `x`-structure judgment is not applied. The non-application basis is "because the initial state is uniform in `x`".

### 6-2. `l0_tc3_initial_jet_matches_analytic`
- `level`: `L0`
- `objective`: confirm that the discrete initial state of `tc3_compact_jet` is the closed form of 2-3 at the cell centres: the velocity, and the depth's constant of integration.
- target cases:
  - `chan_tc3_ref_n064_dts100`
- `expected_outcome`: `pass`
- judgment conditions:
  - The initial-state judgment is applied. The evaluation expressions are `errors.initial_u.linf_rel` with the threshold $\le 1.0e{-9}$, `errors.initial_v.linf` with the threshold $\le 1.0e{-12}$, and `errors.initial_h.south_rel` with the threshold $\le 1.0e{-9}$. `errors.initial_h.linf_rel` is not judged: it is `N/A` for this profile (5-3).
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$.
  - The depth-positivity judgment is applied. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 2.0e3$.
  - The mass-conservation judgment is not applied. The non-application basis is "because the test judges the initial state and the conservation judgment belongs to 6-4".
  - The theoretical-comparison judgment is not applied. The non-application basis is "because the test judges the initial state and the steady-solution judgment belongs to 6-4".
  - The wall-normal-velocity judgment at $t_{end}$ is not applied. The non-application basis is "because the test judges the initial state; the initial $v$ is judged by `errors.initial_v.linf` above".
  - The translation-equivariance judgment is not applied. The non-application basis is "because the pair case is not run".
  - The `x`-structure judgment is not applied. The non-application basis is "because the initial state is uniform in `x`".

### 6-3. `l1_tc2_refinement_steady_geostrophic`
- `level`: `L1`
- `objective`: confirm the steady-solution error decrease with refinement, mass conservation, positivity, and the wall-normal velocity for `tc2_zonal_uniform`.
- target cases:
  - `chan_tc2_ref_n032_dts100`
  - `chan_tc2_ref_n064_dts100`
  - `chan_tc2_ref_n128_dts100`
- `expected_outcome`: `pass`
- judgment conditions:
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$.
  - The depth-positivity judgment is applied. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 1.0e3$.
  - The mass-conservation judgment is applied. The evaluation expression is `metrics.mass_drift_rel`, and the threshold is $\le 1.0e{-10}$.
  - The theoretical-comparison judgment is applied. `errors.steady_h.l2_rel_tend` applies the per-case band of 5-5 as two judged quantities (lower bound $2.6e{-2}$, $1.4e{-2}$, $7.3e{-3}$ and upper bound $8.0e{-2}$, $4.5e{-2}$, $2.3e{-2}$), and `convergence_order` requires $\ge 0.75$ for `convergence.n032_to_n064.l2_order` (carried by `chan_tc2_ref_n064_dts100`) and for `convergence.n064_to_n128.l2_order` (carried by `chan_tc2_ref_n128_dts100`).
  - The wall-normal-velocity judgment is applied. The evaluation expression is `extrema.v.max_abs`, and the per-case threshold is $\le 3.0$, $\le 2.0$, $\le 1.3$.
  - The initial-state judgment is not applied. The non-application basis is "because it belongs to 6-1".
  - The translation-equivariance judgment is not applied. The non-application basis is "because the pair case is not run".
  - The `x`-structure judgment is not applied. The non-application basis is "because the initial state is uniform in `x`".

### 6-4. `l1_tc3_refinement_compact_jet`
- `level`: `L1`
- `objective`: confirm the steady-solution error decrease with refinement, mass conservation, positivity, and the wall-normal velocity for `tc3_compact_jet`.
- target cases:
  - `chan_tc3_ref_n032_dts100`
  - `chan_tc3_ref_n064_dts100`
  - `chan_tc3_ref_n128_dts100`
- `expected_outcome`: `pass`
- judgment conditions:
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$.
  - The depth-positivity judgment is applied. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 2.0e3$.
  - The mass-conservation judgment is applied. The evaluation expression is `metrics.mass_drift_rel`, and the threshold is $\le 1.0e{-10}$.
  - The theoretical-comparison judgment is applied. `errors.steady_h.l2_rel_tend` applies the per-case band of 5-5 as two judged quantities (lower bound $1.7e{-2}$, $1.2e{-2}$, $8.2e{-3}$ and upper bound $5.5e{-2}$, $4.0e{-2}$, $2.6e{-2}$), and `convergence_order` requires $\ge 0.35$ for `convergence.n032_to_n064.l2_order` (carried by `chan_tc3_ref_n064_dts100`) and for `convergence.n064_to_n128.l2_order` (carried by `chan_tc3_ref_n128_dts100`).
  - The wall-normal-velocity judgment is applied. The evaluation expression is `extrema.v.max_abs`, and the per-case threshold is $\le 0.30$, $\le 0.28$, $\le 0.20$.
  - The initial-state judgment is not applied. The non-application basis is "because it belongs to 6-2".
  - The translation-equivariance judgment is not applied. The non-application basis is "because the pair case is not run".
  - The `x`-structure judgment is not applied. The non-application basis is "because the initial state is uniform in `x`".

### 6-5. `l2_tc2_long_run`
- `level`: `L2`
- `objective`: confirm that the steady state is held over five days for `tc2_zonal_uniform` at `nx=128`: bounded error growth, mass conservation, positivity, and the wall-normal velocity.
- target cases:
  - `chan_tc2_ref_n128_dts100_tend5d`
- `expected_outcome`: `pass`
- judgment conditions:
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$.
  - The depth-positivity judgment is applied. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 1.0e3$.
  - The mass-conservation judgment is applied. The evaluation expression is `metrics.mass_drift_rel`, and the threshold is $\le 5.0e{-10}$.
  - The theoretical-comparison judgment is applied. The evaluation expression is `errors.steady_h.l2_rel_tend`, and the band is $[3.2e{-2},\ 9.5e{-2}]$ as two judged quantities. `convergence_order` is not judged: the case is not a member of a refinement pair.
  - The wall-normal-velocity judgment is applied. The evaluation expression is `extrema.v.max_abs`, and the threshold is $\le 1.0$.
  - The initial-state judgment is not applied. The non-application basis is "because it belongs to 6-1".
  - The translation-equivariance judgment is not applied. The non-application basis is "because the pair case is not run".
  - The `x`-structure judgment is not applied. The non-application basis is "because the initial state is uniform in `x`".

### 6-6. `l3_x_translation_equivariance_and_wave_decay`
- `level`: `L3`
- `objective`: confirm that the `x`-direction dynamics are those of the Controlled Spec §5 for `tc2_zonal_perturbed`: the initial state, the translation equivariance of the pair, the decay of the perturbation's `x`-structure, mass conservation and positivity.
- pair cases:
  - `reference` is `chan_tc2_xpert_n064_dts100`
  - `shifted` is `chan_tc2_xpert_n064_dts100_sx025`
- `expected_outcome`: `pass`
- judgment conditions:
  - The initial-state judgment is applied to both cases. The evaluation expressions are `errors.initial_h.linf_rel` with the threshold $\le 1.0e{-9}$ (against the closed form of 2-3 at the cell centres $(x_i,y_j)$, shift included), `errors.initial_u.linf_rel` with the threshold $\le 1.0e{-9}$, and `errors.initial_v.linf` with the threshold $\le 1.0e{-12}$.
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$.
  - The depth-positivity judgment is applied. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 1.0e3$.
  - The mass-conservation judgment is applied. The evaluation expression is `metrics.mass_drift_rel`, and the threshold is $\le 1.0e{-10}$.
  - The translation-equivariance judgment is applied. The evaluation expression is `errors.symmetry_h.l2_rel` (carried by the shifted case), and the threshold is $\le 2.0e{-11}$.
  - The `x`-structure judgment is applied to both cases. The evaluation expression is `xstructure.decay_ratio`, and the band is $[0.13,\ 0.55]$ as two judged quantities.
  - The theoretical-comparison judgment is not applied. The non-application basis is "because `tc2_zonal_perturbed` has no steady reference (2-4)".
  - The wall-normal-velocity judgment at $t_{end}$ is not applied. The non-application basis is "because the perturbation excites $v$ by design; the wall-normal velocity of the steady profiles is judged in 6-3 to 6-5".

### 6-7. `l0_cfl_guard_xfail`
- `level`: `L0`
- `objective`: confirm that a `CFL`-violation case can be detected (expected failure).
- target cases:
  - `chan_guard_n032_dts240`
- `expected_outcome`: `xfail`
- `xfail_condition`: `cfl.max > 1.0`
- `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'cfl'`
- judgment conditions:
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$.
  - The depth-positivity judgment is applied and its real `extrema.h.min` value and status are reported against the threshold $\ge 1.0e3$ (it is never `N/A`; the 5-3 rule does not apply to it). It is non-gating for this test: its status is not a condition of `pass_when`, so a `fail` on it does not change this test's outcome.
  - The mass-conservation judgment is not applied. The non-application basis is "because the purpose of the guard test is only the detection of a stability-condition violation".
  - The theoretical-comparison judgment is not applied. The non-application basis is "because under an unstable condition, the can-continue-execution evaluation is done before the theoretical-agreement judgment".
  - The wall-normal-velocity judgment is not applied. The non-application basis is "because the purpose of the guard test is only the detection of a stability-condition violation".
  - The initial-state judgment is not applied. The non-application basis is "because it belongs to 6-1".
  - The translation-equivariance judgment is not applied. The non-application basis is "because the pair case is not run".
  - The `x`-structure judgment is not applied. The non-application basis is "because the initial state is uniform in `x`".

## 7. Pass/fail aggregation rules
- `per_test.pass_rule`: `pass` when all applicable checks are `pass`.
- `per_test.xfail_rule`: `xfail` when `expected_outcome == xfail` and `xfail_condition` is true and `pass_when` is satisfied.
- `suite.pass_rule`: `pass` when all `test_id` satisfy `pass_rule` or `xfail_rule`.

## 8. Traceability
### 8-1. Output requirements
- `verdict.json` requires each check's `status`, `metric_value`, `threshold`, and `reason_na` when `applicable=false`.
- `summary.json` requires the counts of `pass`, `fail`, `xfail`, and `skipped`.

### 8-2. Traceability records
- The `test_profile_id` and `test_profile_version` of this document must be recorded in `case.resolved.yaml` and `trial_meta.json`.
- The judgment conditions of this document must be mappable to the evaluation basis of `verdict.json`.
