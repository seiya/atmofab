# Tests: 2D shallow water channel (verification input / judgment conditions)

## 0. Meta information
- `status`: `draft`
- `test_profile_id`: `shallow_water2d_channel_baseline`
- `test_profile_version`: `0.2.0`
- `spec_ref.spec_kind`: `problem`
- `spec_ref.spec_id`: `shallow_water2d_channel`
- `spec_ref.spec_version`: `0.2.0`
- `spec_ref.controlled_spec_path`: `spec/problem/dynamics/shallow_water/shallow_water2d_channel/controlled_spec.md`

## 1. Test purpose
This suite verifies, for the discrete implementation of the 2D shallow water equation on a channel with the Coriolis force, the discrete initial state against the analytic profiles, the error against the analytic solution — the steady solution of Williamson et al. (1992) Test Cases 2 and 3 and the forced translating low of Test Case 4, on the channel — bounded on both sides, since the fully fixed `p1` discretization makes it a property of the scheme, and its second-order decrease with refinement, the time integration through a pair of runs that differ only in `dt`, mass conservation, depth positivity, the wall-normal velocity, a long-time integration, the `x`-direction dynamics through an `x`-perturbed pair (translation equivariance and the decay of the perturbation's `x`-structure, which an update without the `x`-interface flux leaves unchanged), and the `CFL` guard. The judgment targets are `L0` to `L3`, and include an expected failure (`xfail`).

## 2. Input-defaulting rules
### 2-1. Basic constants
The constants are those of the Controlled Spec §6, in SI units: `g=9.80616`, `Omega=7.292e-5`, `a=6.37122e6`, `theta0 = pi/4`, so `f0 = 2*Omega*sin(theta0) = 1.03124...e-4` and `beta = 2*Omega*cos(theta0)/a = 1.61860...e-11`; `gh0=2.94e4` (`h0 = gh0/g = 2998.1155...`) and `u0 = 2*pi*a/(12*86400) = 38.6107...` for `tc2_zonal_uniform`, `tc3_compact_jet` and `tc2_zonal_perturbed`; `gh0=1.0e5` (`h0 = gh0/g = 10197.67...`) and `u_jet=40.0` for `tc4_translating_low`, whose derived parameters of the low are `psi0 = -0.03*gh0/f0 = -2.909106...e7`, `R_low = 2*a/12.74244 = 1.0000...e6`, `c_tr = u_jet*cos(theta0) = 28.28427...`, `x_c0 = L_x/2` and `y_c = L_y/2`. The ellipses mark derived values: the case inputs carry the constants `g`, `Omega`, `a`, `gh0` and `theta0` (the double-precision value of `pi/4`) and the parameter `u_jet` as numbers, and `h0`, `u0`, `f0`, `beta`, `psi0`, `R_low`, `c_tr`, `x_c0`, `y_c` are evaluated from them by the expressions above wherever they are used — by the runner and by a judgment that evaluates a reference field alike; no rounded literal of a derived value is an input.

### 2-2. Coriolis profile
`coriolis_profile=beta_plane` in every case: $f(y)=f_0+\beta\,(y-L_y/2)$ at the cell centres.

### 2-3. Flow profile (initial condition)
- `tc2_zonal_uniform`: $u=u_0$, $v=0$, $h(y)=h_0-\dfrac{u_0}{g}\left[f_0\,(y-L_y/2)+\dfrac{\beta}{2}\,(y-L_y/2)^2\right]$, with `L_x=L_y=6.0e6`.
- `tc3_compact_jet`: $y_b=L_y/8$, $y_e=7L_y/8$, $x_e=0.3$, $s=x_e\,(y-y_b)/(y_e-y_b)$, $b(s)=0$ ($s\le0$), $e^{-1/s}$ ($s>0$), $u(y)=u_0\,b(s)\,b(x_e-s)\,e^{4/x_e}$, $v=0$, $h(y)=h_0-\dfrac1g\int_0^y f\,u\,dy'$ by the composite Simpson quadrature of the Controlled Spec §6, with `L_x=L_y=8.0e6`. The branch-free form $b(s)=\exp(-1/\max(s,10^{-300}))$ of the Controlled Spec §6 is equal to $b(s)$ at every cell centre of the sweep.
- `tc2_zonal_perturbed`: $h(x,y)=h_{TC2}(y)+\eta_0\sin\left(2\pi\,(x/L_x-\mathrm{shift\_x\_fraction})\right)$ with $h_{TC2}$ the `tc2_zonal_uniform` depth, $u=u_0$, $v=0$, `eta0=30.0` (`m`), with `L_x=L_y=6.0e6`.
- `tc4_translating_low`: the prescribed flow of the Controlled Spec §6 at $t=0$, $h=\tilde h$, $u=\tilde u$, $v=\tilde v$, with `L_x=L_y=8.0e6`: $\tilde h=\bar h+f\psi/g$, $\tilde u=\bar u-\psi_y$, $\tilde v=\psi_x$, with $\bar u=u_{jet}\sin^2(\pi y/L_y)$, the closed-form $\bar h$, and $\psi$, $\psi_x$, $\psi_y$ of that section. The low's depth anomaly is $-306\ \mathrm{m}$ at its centre and its streamfunction at the walls is at most $1.1\times10^{-7}\,|\psi_0|$ in magnitude ($L_y/2=4R_{low}$).

### 2-4. Theoretical solution (with applicability condition)
`tc2_zonal_uniform` and `tc3_compact_jet` are steady solutions of the continuous equations, so the reference at $t_{end}$ is the initial state: for `tc2_zonal_uniform` the closed form of 2-3 evaluated at the cell centres, $h_{ref}(y)=h(y)$; for `tc3_compact_jet` the discrete initial state itself, $h_{ref}=h(t=0)$ (its $h$ has no closed form). The theoretical-agreement judgment at $t_{end}$ targets `h`; a judgment for `hu` / `hv` against the reference at $t_{end}$ is not required in this suite, and the wall-normal velocity is judged by its own metric (2-5). `tc4_translating_low` is not steady: its reference at $t_{end}$ is the prescribed flow at $t_{end}$, $h_{ref}=\tilde h(x_i,y_j,t_{end})$, whose low has moved a distance $c_{tr}\,t_{end}$ east ($2444\ \mathrm{km}$ in one day); the theoretical-agreement judgment targets `h`, and the velocity error against $(\tilde u,\tilde v)$ at $t_{end}$ is reported. `tc2_zonal_perturbed` has no steady reference; it is judged by 2-6. The initial state is judged against its closed form for every profile: `h` for `tc2_zonal_uniform`, `tc2_zonal_perturbed` and `tc4_translating_low` (2-3, at the cell centres), `u` for all four, `v=0` for the three unforced profiles and $v=\tilde v$ for `tc4_translating_low`, and for `tc3_compact_jet` — whose `h` is defined by the quadrature — the depth of the rows south of the jet, where $u=0$ on $[0,y]$ and the integral is exactly zero, so $h=h_0$ and it is the maximum of $h$ over the domain — and the depth of the rows north of the jet, where the integral is complete and equals the closed-form constant $h_N$ of the Controlled Spec §6, the minimum of $h$ over the domain. The two pin the constant of integration and the complete integral (the jet's normalisation $e^{4/x_e}$, $x_e$, $y_b$, $y_e$ and the $1/g$ factor). They do not separate quadrature rules: every composite rule is exact to round-off for the complete integral of this smooth, compactly supported integrand, and the rows inside the jet — where a trapezoid rule on the same sub-grid differs from the Simpson rule by up to $1.9\times10^{-7}$ relative at `ny=32` — are judged only against the case's own initial state.

### 2-5. Wall-normal velocity
The exact solution of `tc2_zonal_uniform` and `tc3_compact_jet` has $v=0$ everywhere. The mirror ghost yields a non-zero $v$ of order $dy$ in the wall-adjacent rows (the boundary `component`'s §2), so a "zero `hv` at the wall" judgment is not applicable to this discretization; the judged quantity is $\max_{i,j}|v_{i,j}(t_{end})|$ over the whole domain, with a per-case threshold decreasing with refinement. For `tc4_translating_low`, whose $v$ is the low's by design, the metric is reported and not judged.

### 2-6. `x`-direction dynamics (translation equivariance and `x`-structure decay)
The `x`-uniform profiles exercise no `x`-interface flux and no periodic `x` mapping (Controlled Spec §5, zonal uniformity), so they cannot detect an update that omits either. `tc2_zonal_perturbed` does, in two ways. First, `f` depends on `y` alone and `x` is periodic, so a run whose initial state is shifted by a whole number of cells in `x` (`shift_x_fraction` such that $\mathrm{shift\_x\_fraction}\cdot nx$ is an integer) is the unshifted run shifted by the same number of cells, up to round-off; the pair is judged by `errors.symmetry_h.l2_rel`. Second, the wavenumber-one perturbation is a gravity-wave excitation that disperses as it propagates in `x` and that the `p1` scheme's numerical diffusion damps; its `x`-structure at $t_{end}$ relative to $t=0$ is measured by `xstructure.decay_ratio` (5-4), whose value is a property of the fixed discretization at the case's resolution (reference `0.7666` at `nx=64`), while an update without the `x`-interface flux leaves the `x`-structure unchanged (ratio `1.000` to three digits — the columns evolve independently) and an outflow `x` boundary breaks mass conservation and the equivariance. The upper bound `0.95` of 5-5 lies between the two, `1.24` times the reference and `0.95` of the ratio without the `x`-interface flux.

### 2-7. The forcing of `tc4_translating_low`
The forcing formula is verified by the forcing `component`'s own `L0` suite, against a central-difference residual of the prescribed flow. This suite judges the forced run as a whole against the prescribed flow at $t_{end}$: the calibration of 5-5 puts a run without the forcing at `2.886e-3` / `2.994e-3` (`nx=64` / `128`) and a run with the forcing frozen at $t=0$ at `2.880e-3` / `3.037e-3`, 3.0 to 20.9 times the upper bound of `errors.mms_h.l2_rel_tend` at those resolutions, and a state left at its initial value at `6.969e-3`. A run that evaluates the forcing at the time of the step's start instead of the stage time differs from the reference implementation by `0.3%` / `1.6%` in `errors.mms_h.l2_rel_tend` (`6.394e-4` / `9.817e-5`), inside the band; that substitution is detected by the time-refinement pair of 2-8, not by the band. Three substitutions are detected by no judgment of this suite, measured on the reference implementation: a forcing evaluated from the stage state instead of the prescribed flow (`errors.mms_h.l2_rel_tend` within `0.001%` of the reference), `dubar` taken as a difference of the `ubar` row array instead of its closed form (equal to the reference to four digits), and a reconstruction of the primitive variables $(h,u,v)$ instead of the conserved ones (`0.8%` to `11%` from the reference in `errors.steady_h.l2_rel_tend` and `errors.mms_h.l2_rel_tend`, inside every band).

### 2-8. Time-refinement pair
The error against the reference at $t_{end}$ is dominated by the spatial discretization: replacing `RK4` by a second- or third-order Runge–Kutta method, or changing `cfl_target` between `0.2` and `0.6`, changes `errors.steady_h.l2_rel_tend` and `errors.mms_h.l2_rel_tend` by less than `0.05%` at `nx=32` and `64`. The time integration is judged by a pair of `tc4_translating_low` runs at `nx=32` that differ only in `dt_scale` (`1.0` and `0.5`): the relative difference of their final depths, `errors.time_pair.h_l2_rel` (5-4), is the time-discretization error of the `dt_scale=1.0` run to leading order. Its reference value is `1.818e-8`. A two-stage second-order method gives `2.0e-6`, `SSPRK3` `8.7e-8`, the forcing at the step's start time or end time for all four stages `3.4e-6`, `cfl_target=0.3` `7.2e-9` and `cfl_target=0.6` `3.6e-8`, each outside the band of 5-5.

## 3. Execution-control rules
- $t_{start}=0$ and $t_{end}=86400$ (`s`; one day), unless a case overrides `t_end`.
- `dt` is decided by the following procedure.
  1. Evaluate $\lambda_0=\max_{i,j}\left((|u_{i,j}|+\sqrt{gh_{i,j}})/dx+(|v_{i,j}|+\sqrt{gh_{i,j}})/dy\right)$ at the initial time.
  2. $\mathrm{dt\_raw}=\mathrm{dt\_scale}\cdot \mathrm{cfl\_target}/\lambda_0$.
  3. $\mathrm{n\_step}=\lceil (t_{end}-t_{start})/\mathrm{dt\_raw}\rceil$.
  4. $dt=(t_{end}-t_{start})/\mathrm{n\_step}$.
- $\mathrm{cfl\_target}=0.45$.
- The time passed to the time-update `component` at step $n$ ($n=0..\mathrm{n\_step}-1$) is $t_{start}+n\,dt$.
- The stop condition is $n=\mathrm{n\_step}$. The number of steps performed is a state variable of the case: the state the host captures after `case_setup` and after `case_run` consists of `h`, `hu`, `hv` (`nx` × `ny` each) and the scalar `n_step` (`0` after setup, the count of `RK4` steps performed after the run, incremented by the step loop at each step), and it is emitted as the diagnostic `run.n_step`.
- The output times are $0,\ 21600,\ 43200,\ 64800,\ 86400$ (`s`); a case with an overridden `t_end` outputs at the same interval of $21600$ up to its $t_{end}$.

## 4. Case-expansion rules
### 4-1. family definition
The `sweep` and fixed values per `family` are defined below.

| family_id | purpose | sweep | fixed |
| --- | --- | --- | --- |
| `chan_tc2_ref` | refinement_for_steady_geostrophic_flow | $nx=ny\in\{32,64,128\}$ | `flow_profile=tc2_zonal_uniform`, `coriolis_profile=beta_plane`, `L_x=L_y=6.0e6`, `gh0=2.94e4`, `dt_scale=1.0` |
| `chan_tc3_ref` | refinement_for_compact_jet | $nx=ny\in\{32,64,128\}$ | `flow_profile=tc3_compact_jet`, `coriolis_profile=beta_plane`, `L_x=L_y=8.0e6`, `gh0=2.94e4`, `dt_scale=1.0` |
| `chan_tc4_ref` | refinement_for_forced_translating_low | $nx=ny\in\{32,64,128\}$ | `flow_profile=tc4_translating_low`, `coriolis_profile=beta_plane`, `L_x=L_y=8.0e6`, `gh0=1.0e5`, `u_jet=40.0`, `dt_scale=1.0` |
| `chan_tc2_xpert` | x_translation_equivariance_and_wave_decay | $nx=ny=64$ | `flow_profile=tc2_zonal_perturbed`, `coriolis_profile=beta_plane`, `L_x=L_y=6.0e6`, `gh0=2.94e4`, `eta0=30.0`, `shift_x_fraction=0.0`, `dt_scale=1.0` |
| `chan_guard` | expected_failure_for_cfl | $nx=ny=32,\ \mathrm{dt\_scale}=2.40$ | `flow_profile=tc2_zonal_uniform`, `coriolis_profile=beta_plane`, `L_x=L_y=6.0e6`, `gh0=2.94e4` |

Every case carries `ng=2` (the Controlled Spec §3) and the constants `g`, `Omega`, `a`, `theta0` of 2-1.

### 4-2. `case_id` generation rule
- The template is `{family}_n{nx:03d}_dts{dts_pct:03d}`.
- `dts_pct` is decided by $\mathrm{round}(100\cdot \mathrm{dt\_scale})$.
- The expansion order is fixed in the order `family`, `nx`, `dt_scale`.

### 4-3. Explicit-override cases
- Add the `case_id` `chan_tc2_ref_n128_dts100_tend5d`.
- The `base_case_id` references `chan_tc2_ref_n128_dts100`, and overrides only `t_end` to `432000` (`s`; five days).
- Add the `case_id` `chan_tc4_ref_n128_dts100_tend5d`.
- The `base_case_id` references `chan_tc4_ref_n128_dts100`, and overrides only `t_end` to `432000` (`s`; five days).
- Add the `case_id` `chan_tc4_ref_n032_dts100_half`.
- The `base_case_id` references `chan_tc4_ref_n032_dts100`, and overrides only `dt_scale` to `0.5` (the time-refinement pair of 2-8; the `case_id` keeps its base's `dts100` and carries the suffix `_half`, so it sorts after its base case).
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
- `errors.initial_h.north_rel`
- `run.n_step`
- `errors.initial_u.linf_rel`
- `errors.initial_v.linf`
- `errors.initial_v.linf_rel`
- `errors.symmetry_h.l2_rel`
- `xstructure.decay_ratio`
- `errors.steady_h.l2_rel_tend`
- `errors.steady_h.linf_tend`
- `errors.mms_h.l2_rel_tend`
- `errors.mms_h.linf_tend`
- `errors.mms_vel.l2_rel_tend`
- `errors.time_pair.h_l2_rel`
- `convergence.n032_to_n064.l2_order`
- `convergence.n064_to_n128.l2_order`

### 5-3. `N/A` rule
- When a diagnostic item is incomputable or non-applicable, make the output value `null` and require `reason_na`.
- `errors.initial_h.linf_rel` is `N/A` for `flow_profile=tc3_compact_jet` (its initial `h` is defined by the quadrature and has no independent closed form); it is computed for `tc2_zonal_uniform`, `tc2_zonal_perturbed` and `tc4_translating_low`.
- `errors.initial_h.south_rel` and `errors.initial_h.north_rel` are `N/A` for anything other than `flow_profile=tc3_compact_jet`.
- `errors.initial_u.linf_rel` is computed for every profile (`u` is `u0` for the two `tc2_*` profiles and the closed form of 2-3 for `tc3_compact_jet` and `tc4_translating_low`). `errors.initial_v.linf` is computed for the three unforced profiles (`v=0`) and is `N/A` for `tc4_translating_low`; `errors.initial_v.linf_rel` is computed for `tc4_translating_low` only and is `N/A` for every other profile, whose reference `v` is zero.
- `errors.symmetry_h.l2_rel` is `N/A` for anything other than the shifted case of a translation pair; it is carried by `chan_tc2_xpert_n064_dts100_sx025`, which sorts after its base case, and reads the base case's final state.
- `xstructure.decay_ratio` is `N/A` for anything other than `flow_profile=tc2_zonal_perturbed`.
- `errors.time_pair.h_l2_rel` is `N/A` for anything other than the halved-`dt` case of the time-refinement pair; it is carried by `chan_tc4_ref_n032_dts100_half`, which sorts after its base case, and reads the base case's final state.
- `run.n_step` is computed for every case and is never `N/A`.
- The steady-solution fields `errors.steady_h.*` are `N/A` for `flow_profile=tc2_zonal_perturbed`, which has no steady reference, and for `flow_profile=tc4_translating_low`, which is not steady. The manufactured-solution fields `errors.mms_h.*` and `errors.mms_vel.l2_rel_tend` are computed for `flow_profile=tc4_translating_low` only and are `N/A` for every other profile.
- `convergence.n032_to_n064.l2_order` is carried by the `nx=64` case of each of the three refinement families and `convergence.n064_to_n128.l2_order` by its `nx=128` case; the other cases omit the field (5-4).
- `conserved.momentum_x.*` are reported and not judged: the Coriolis source exchanges momentum between `hu` and `hv`, so $P^x$ is not conserved.
- `errors.steady_h.linf_tend` is reported and not judged; the steady-solution judgment is on `errors.steady_h.l2_rel_tend`. Likewise `errors.mms_h.linf_tend` and `errors.mms_vel.l2_rel_tend` are reported and not judged; the manufactured-solution judgment is on `errors.mms_h.l2_rel_tend`.
- `extrema.v.max_abs` is computed for every case, and is judged only for `tc2_zonal_uniform` and `tc3_compact_jet` (2-5).

### 5-4. Definition of the metrics
The relative mass-drift value is defined by the following, and emitted as the field `metrics.mass_drift_rel`.
$$
\mathrm{mass\_drift\_rel}=
\frac{|M_{end}-M_0|}{\max(|M_0|,1e{-14})},
\quad
M=\sum_{i,j} h_{i,j}\,dx\,dy
$$
Here $M_0$ is the field `conserved.mass.initial` and $M_{end}$ is `conserved.mass.final`. `conserved.momentum_x.*` is $P^x=\sum_{i,j}(hu)_{i,j}\,dx\,dy$ at the two times.

The initial-state errors are defined by the following, with $h_{ref}$ the closed form of 2-3 at the cell centres ($h_{ref}(y_j)$ for `tc2_zonal_uniform`, $h_{ref}(x_i,y_j)$ for `tc2_zonal_perturbed`, $\tilde h(x_i,y_j,0)$ for `tc4_translating_low`), $u_{ref}$ the closed form of 2-3 ($u_0$ for the two `tc2_*` profiles, $u(y_j)$ for `tc3_compact_jet`, $\tilde u(x_i,y_j,0)$ for `tc4_translating_low`), $u_s$ the velocity scale ($u_0$, and $u_{jet}$ for `tc4_translating_low`), $v_{ref}=\tilde v(x_i,y_j,0)$ for `tc4_translating_low`, $u_{i,j}=(hu)_{i,j}/h_{i,j}$ and $v_{i,j}=(hv)_{i,j}/h_{i,j}$.
$$
\mathrm{initial\_h\_linf\_rel}=\frac{\max_{i,j}|h_{i,j}(0)-h_{ref,i,j}|}{\max_{i,j}|h_{ref,i,j}|},\qquad
\mathrm{initial\_h\_south\_rel}=\frac{\left|\max_{i,j}h_{i,j}(0)-h_0\right|}{h_0},\qquad
\mathrm{initial\_h\_north\_rel}=\frac{\left|\min_{i,j}h_{i,j}(0)-h_N\right|}{h_0}
\quad(\texttt{tc3\_compact\_jet})
$$
$$
\mathrm{initial\_u\_linf\_rel}=\frac{\max_{i,j}|u_{i,j}(0)-u_{ref,i,j}|}{u_s},\qquad
\mathrm{initial\_v\_linf}=\max_{i,j}|v_{i,j}(0)|,\qquad
\mathrm{initial\_v\_linf\_rel}=\frac{\max_{i,j}|v_{i,j}(0)-v_{ref,i,j}|}{\max_{i,j}|v_{ref,i,j}|}
$$
They are emitted as `errors.initial_h.linf_rel`, `errors.initial_h.south_rel`, `errors.initial_h.north_rel`, `errors.initial_u.linf_rel`, `errors.initial_v.linf` (`m/s`) and `errors.initial_v.linf_rel`. $h_N$ is the closed-form constant of the Controlled Spec §6, $h_N=h_0-f_0\,u_0\,e^{4/x_e}\,(y_e-y_b)\,C/(g\,x_e)$ with $C=1.12064479227927\times10^{-7}$ ($h_N=2436.2126\ \mathrm{m}$ with the constants of 2-1 and $L_y=8.0\times10^6\ \mathrm{m}$). `initial_h_south_rel` pins the constant of integration of the `tc3_compact_jet` quadrature: $u=0$ on $[0,y_j]$ for every row south of the jet, the integral is exactly zero there, $h=h_0$, and $h$ is non-increasing northward ($f\,u\ge0$), so the maximum of $h(0)$ over the domain is $h_0$; `initial_h_north_rel` pins the complete integral, since every row north of $y_e$ carries it and $h_N$ is its closed form; the rule used inside the jet is not judged (2-4).

The steady-solution errors are defined by the following, with $h_{ref}$ as in 2-4.
$$
\mathrm{steady\_h\_l2\_rel}=
\frac{\|h(t_{end})-h_{ref}\|_2}{\|h_{ref}\|_2},\qquad
\mathrm{steady\_h\_linf}=\max_{i,j}\left|h_{i,j}(t_{end})-h_{ref,i,j}\right|
$$
They are emitted as `errors.steady_h.l2_rel_tend` and `errors.steady_h.linf_tend` (`m`).

The manufactured-solution errors of `tc4_translating_low` are defined by the same expressions with the prescribed flow at $t_{end}$ as the reference, $h_{ref,i,j}=\tilde h(x_i,y_j,t_{end})$, $u_{ref,i,j}=\tilde u(x_i,y_j,t_{end})$ and $v_{ref,i,j}=\tilde v(x_i,y_j,t_{end})$ evaluated from the closed forms of the Controlled Spec §6 with the case's inputs:
$$
\mathrm{mms\_h\_l2\_rel}=\frac{\|h(t_{end})-h_{ref}\|_2}{\|h_{ref}\|_2},\qquad
\mathrm{mms\_h\_linf}=\max_{i,j}\left|h_{i,j}(t_{end})-h_{ref,i,j}\right|,\qquad
\mathrm{mms\_vel\_l2\_rel}=\left(\frac{\sum_{i,j}(u_{i,j}-u_{ref,i,j})^2+(v_{i,j}-v_{ref,i,j})^2}{\sum_{i,j}u_{ref,i,j}^2+v_{ref,i,j}^2}\right)^{1/2}
$$
with $u$, $v$ at $t_{end}$. They are emitted as `errors.mms_h.l2_rel_tend`, `errors.mms_h.linf_tend` (`m`) and `errors.mms_vel.l2_rel_tend`.

`run.n_step` is the state variable `n_step` after the run (§3); its judged value is the $\mathrm{n\_step}$ of the §3 procedure evaluated from the case's inputs, $\lceil (t_{end}-t_{start})/\mathrm{dt\_raw}\rceil$ with $\mathrm{dt\_raw}$ from the initial state.

The depth minimum `extrema.h.min` (`m`) is the minimum of $h$ over all interior cells and over the initial state and every committed state of the run. `cfl.max` is the maximum of the stability index of the Controlled Spec §5 over the initial state and every committed state of the run, with the case's `dt`.

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

The time-refinement difference is defined by the following, with $h_{base}$ the final state of `chan_tc4_ref_n032_dts100` and $h_{half}$ that of `chan_tc4_ref_n032_dts100_half`; it is emitted as `errors.time_pair.h_l2_rel` on the latter.
$$
\mathrm{time\_pair\_h\_l2\_rel}=\frac{\|h_{half}(t_{end})-h_{base}(t_{end})\|_2}{\|h_{base}(t_{end})\|_2}
$$

`convergence_order` is a cross-case reduction, using $p=\log(e_{coarse}/e_{fine})/\log(2)$, over `errors.steady_h.l2_rel_tend` for `chan_tc2_ref` and `chan_tc3_ref` and over `errors.mms_h.l2_rel_tend` for `chan_tc4_ref`, and is accumulated only over the target cases of the test that judges it, within one family. It is emitted as a per-case field of the finer case of each pair: `chan_tc2_ref_n064_dts100`, `chan_tc3_ref_n064_dts100` and `chan_tc4_ref_n064_dts100` carry `convergence.n032_to_n064.l2_order`, and `chan_tc2_ref_n128_dts100`, `chan_tc3_ref_n128_dts100` and `chan_tc4_ref_n128_dts100` carry `convergence.n064_to_n128.l2_order`. The cases preceding the one that completes a reduction omit that field; the override cases `chan_tc2_ref_n128_dts100_tend5d`, `chan_tc4_ref_n128_dts100_tend5d` and `chan_tc4_ref_n032_dts100_half` are not members of a refinement pair and omit both.

### 5-5. Default thresholds
The thresholds are calibrated against an independent reference implementation of the Controlled Spec §5 scheme (issue #265, phase-2 calibration comment): the per-case `l2` bands are about 0.5 and 1.5 times the reference values, the `v_max_abs` bounds 1.49 to 1.73 times, and the order bounds 0.31 to 1.12 below the reference orders. The reference `l2` orders are 1.91 / 2.13 for `tc2_zonal_uniform`, 2.32 / 2.58 for `tc3_compact_jet` and 1.93 / 2.72 for `tc4_translating_low`; the order of the second pair exceeds two for all three profiles, and the bounds take that spread.
- $\mathrm{cfl.max} \le 1.0$, and `run.n_step` equals the §3 $\mathrm{n\_step}$ of the case (an integer; judged as $|\mathrm{run.n\_step}-\mathrm{n\_step}| \le 0.5$)
- `extrema.h.min` is $\ge 1.0e3$ for `tc2_zonal_uniform` and `tc2_zonal_perturbed`, $\ge 2.0e3$ for `tc3_compact_jet`, and $\ge 8.0e3$ for `tc4_translating_low` (`m`)
- $\mathrm{mass\_drift\_rel} \le 1.0e{-10}$ ($\le 5.0e{-10}$ for the two five-day cases)
- `initial_h_linf_rel` $\le 1.0e{-9}$, `initial_h_south_rel` $\le 1.0e{-9}$, `initial_h_north_rel` $\le 1.0e{-9}$, `initial_u_linf_rel` $\le 1.0e{-9}$, `initial_v_linf` $\le 1.0e{-12}$ (`m/s`), `initial_v_linf_rel` $\le 1.0e{-9}$
- `steady_h_l2_rel` is judged as a two-sided band. The Controlled Spec §5 fixes every term of the spatial discretization (`p1` `MC` reconstruction, Rusanov flux, mirror ghost, explicit Coriolis source), which dominates the error against the steady solution at $t_{end}$ (the time integration is judged by 2-8), so that error is a property of the discretization — its truncation error and numerical diffusion — and not a free quantity: a value below the band is not a better scheme, it is a run that did not perform the §5 update (a state left at its initial value has zero error against a steady solution and passes an upper bound alone). The upper bound is about 1.5 times the reference value and the lower bound about 0.5 times it. The lower bound and the upper bound are two DISTINCT judged quantities of the test — each names its own `quantity` and each requires its own host-evaluated corroborant over the captured state — not two thresholds on one quantity: a corroborant that reads only the upper bound leaves the lower bound to the checks module's own report, and a state left at its initial value with a reported value inside the band would then pass. For `chan_tc2_ref` the band is $[2.4e{-3},\ 7.1e{-3}]$ for `nx=32`, $[6.3e{-4},\ 1.9e{-3}]$ for `nx=64`, and $[1.4e{-4},\ 4.3e{-4}]$ for `nx=128`; for `chan_tc3_ref` it is $[2.8e{-3},\ 8.5e{-3}]$, $[5.7e{-4},\ 1.7e{-3}]$, and $[9.4e{-5},\ 2.8e{-4}]$; for the five-day case `chan_tc2_ref_n128_dts100_tend5d` it is $[7.2e{-4},\ 2.1e{-3}]$. The first-order `p0` reconstruction gives `5.2e-2` at `chan_tc2_ref` `nx=32` and fails the upper bound.
- `mms_h_l2_rel` is judged as a two-sided band on the same ground, as two distinct judged quantities: a state left at its initial value is excluded by the upper bound (`6.969e-3`), and a state that holds the prescribed flow at $t_{end}$ without the §5 update is excluded by the lower bound. For `chan_tc4_ref` the band is $[1.2e{-3},\ 3.6e{-3}]$ for `nx=32`, $[3.2e{-4},\ 9.6e{-4}]$ for `nx=64`, and $[4.8e{-5},\ 1.45e{-4}]$ for `nx=128`; for the five-day case `chan_tc4_ref_n128_dts100_tend5d` it is $[1.3e{-4},\ 4.0e{-4}]$
- `convergence_order` requires $\ge 1.6$ for both pairs of `chan_tc2_ref`, $\ge 1.8$ for both pairs of `chan_tc3_ref`, and $\ge 1.6$ for both pairs of `chan_tc4_ref`
- `v_max_abs` (`m/s`) for `chan_tc2_ref` is $\le 4.0$ for `nx=32`, $\le 2.6$ for `nx=64`, and $\le 1.5$ for `nx=128`; for `chan_tc3_ref` it is $\le 0.090$, $\le 0.025$, and $\le 0.0080$; for the five-day case `chan_tc2_ref_n128_dts100_tend5d` it is $\le 1.2$
- `time_pair_h_l2_rel` is judged as a two-sided band $[9.0e{-9},\ 2.7e{-8}]$ on `chan_tc4_ref_n032_dts100_half` (reference `1.818e-8`; about 0.5 and 1.5 times it), as two distinct judged quantities, each with its own host-evaluated corroborant.
- `symmetry_h_l2_rel` $\le 2.0e{-11}$ (the shift is a whole number of cells and every operation of the scheme is translation-covariant, so the residual is round-off)
- `decay_ratio` is judged as a two-sided band $[0.38,\ 0.95]$ at `nx=64` (reference `0.7666`; the lower bound is about 0.5 times it, and the upper bound is set below the ratio `1.000` of an update without the `x`-interface flux, 2-6). The lower and the upper bound are two distinct judged quantities, each with its own host-evaluated corroborant.

## 6. Test definitions
### 6-1. `l0_tc2_initial_state_matches_analytic`
- `level`: `L0`
- `objective`: confirm that the discrete initial state of `tc2_zonal_uniform` is the closed form of 2-3 at the cell centres.
- target cases:
  - `chan_tc2_ref_n064_dts100`
- `expected_outcome`: `pass`
- judgment conditions:
  - The initial-state judgment is applied. The evaluation expressions are `errors.initial_h.linf_rel` with the threshold $\le 1.0e{-9}$, `errors.initial_u.linf_rel` with the threshold $\le 1.0e{-9}$, and `errors.initial_v.linf` with the threshold $\le 1.0e{-12}$.
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of the case ($|\mathrm{run.n\_step}-\mathrm{n\_step}| \le 0.5$).
  - The depth-positivity judgment is applied. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 1.0e3$.
  - The mass-conservation judgment is not applied. The non-application basis is "because the test judges the initial state and the conservation judgment belongs to 6-3".
  - The theoretical-comparison judgment is not applied. The non-application basis is "because the test judges the initial state and the steady-solution judgment belongs to 6-3".
  - The wall-normal-velocity judgment at $t_{end}$ is not applied. The non-application basis is "because the test judges the initial state; the initial $v$ is judged by `errors.initial_v.linf` above".
  - The translation-equivariance judgment is not applied. The non-application basis is "because the pair case is not run".
  - The `x`-structure judgment is not applied. The non-application basis is "because the initial state is uniform in `x`".
  - The manufactured-solution judgment is not applied. The non-application basis is "because `errors.mms_*` are `N/A` for a profile other than `tc4_translating_low` (5-3)".

### 6-2. `l0_tc3_initial_jet_matches_analytic`
- `level`: `L0`
- `objective`: confirm that the discrete initial state of `tc3_compact_jet` is the closed form of 2-3 at the cell centres: the velocity, the depth's constant of integration, and the depth north of the jet, which is the quadrature's closed-form value.
- target cases:
  - `chan_tc3_ref_n064_dts100`
- `expected_outcome`: `pass`
- judgment conditions:
  - The initial-state judgment is applied. The evaluation expressions are `errors.initial_u.linf_rel` with the threshold $\le 1.0e{-9}$, `errors.initial_v.linf` with the threshold $\le 1.0e{-12}$, `errors.initial_h.south_rel` with the threshold $\le 1.0e{-9}$, and `errors.initial_h.north_rel` with the threshold $\le 1.0e{-9}$. `errors.initial_h.linf_rel` is not judged: it is `N/A` for this profile (5-3).
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of the case ($|\mathrm{run.n\_step}-\mathrm{n\_step}| \le 0.5$).
  - The depth-positivity judgment is applied. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 2.0e3$.
  - The mass-conservation judgment is not applied. The non-application basis is "because the test judges the initial state and the conservation judgment belongs to 6-4".
  - The theoretical-comparison judgment is not applied. The non-application basis is "because the test judges the initial state and the steady-solution judgment belongs to 6-4".
  - The wall-normal-velocity judgment at $t_{end}$ is not applied. The non-application basis is "because the test judges the initial state; the initial $v$ is judged by `errors.initial_v.linf` above".
  - The translation-equivariance judgment is not applied. The non-application basis is "because the pair case is not run".
  - The `x`-structure judgment is not applied. The non-application basis is "because the initial state is uniform in `x`".
  - The manufactured-solution judgment is not applied. The non-application basis is "because `errors.mms_*` are `N/A` for a profile other than `tc4_translating_low` (5-3)".

### 6-3. `l1_tc2_refinement_steady_geostrophic`
- `level`: `L1`
- `objective`: confirm the steady-solution error decrease with refinement, mass conservation, positivity, and the wall-normal velocity for `tc2_zonal_uniform`.
- target cases:
  - `chan_tc2_ref_n032_dts100`
  - `chan_tc2_ref_n064_dts100`
  - `chan_tc2_ref_n128_dts100`
- `expected_outcome`: `pass`
- judgment conditions:
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of the case ($|\mathrm{run.n\_step}-\mathrm{n\_step}| \le 0.5$).
  - The depth-positivity judgment is applied. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 1.0e3$.
  - The mass-conservation judgment is applied. The evaluation expression is `metrics.mass_drift_rel`, and the threshold is $\le 1.0e{-10}$.
  - The theoretical-comparison judgment is applied. `errors.steady_h.l2_rel_tend` applies the per-case band of 5-5 as two judged quantities (lower bound $2.4e{-3}$, $6.3e{-4}$, $1.4e{-4}$ and upper bound $7.1e{-3}$, $1.9e{-3}$, $4.3e{-4}$), and `convergence_order` requires $\ge 1.6$ for `convergence.n032_to_n064.l2_order` (carried by `chan_tc2_ref_n064_dts100`) and for `convergence.n064_to_n128.l2_order` (carried by `chan_tc2_ref_n128_dts100`).
  - The wall-normal-velocity judgment is applied. The evaluation expression is `extrema.v.max_abs`, and the per-case threshold is $\le 4.0$, $\le 2.6$, $\le 1.5$.
  - The initial-state judgment is not applied. The non-application basis is "because it belongs to 6-1".
  - The translation-equivariance judgment is not applied. The non-application basis is "because the pair case is not run".
  - The `x`-structure judgment is not applied. The non-application basis is "because the initial state is uniform in `x`".
  - The manufactured-solution judgment is not applied. The non-application basis is "because `errors.mms_*` are `N/A` for a profile other than `tc4_translating_low` (5-3)".

### 6-4. `l1_tc3_refinement_compact_jet`
- `level`: `L1`
- `objective`: confirm the steady-solution error decrease with refinement, mass conservation, positivity, and the wall-normal velocity for `tc3_compact_jet`.
- target cases:
  - `chan_tc3_ref_n032_dts100`
  - `chan_tc3_ref_n064_dts100`
  - `chan_tc3_ref_n128_dts100`
- `expected_outcome`: `pass`
- judgment conditions:
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of the case ($|\mathrm{run.n\_step}-\mathrm{n\_step}| \le 0.5$).
  - The depth-positivity judgment is applied. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 2.0e3$.
  - The mass-conservation judgment is applied. The evaluation expression is `metrics.mass_drift_rel`, and the threshold is $\le 1.0e{-10}$.
  - The theoretical-comparison judgment is applied. `errors.steady_h.l2_rel_tend` applies the per-case band of 5-5 as two judged quantities (lower bound $2.8e{-3}$, $5.7e{-4}$, $9.4e{-5}$ and upper bound $8.5e{-3}$, $1.7e{-3}$, $2.8e{-4}$), and `convergence_order` requires $\ge 1.8$ for `convergence.n032_to_n064.l2_order` (carried by `chan_tc3_ref_n064_dts100`) and for `convergence.n064_to_n128.l2_order` (carried by `chan_tc3_ref_n128_dts100`).
  - The wall-normal-velocity judgment is applied. The evaluation expression is `extrema.v.max_abs`, and the per-case threshold is $\le 0.090$, $\le 0.025$, $\le 0.0080$.
  - The initial-state judgment is not applied. The non-application basis is "because it belongs to 6-2".
  - The translation-equivariance judgment is not applied. The non-application basis is "because the pair case is not run".
  - The `x`-structure judgment is not applied. The non-application basis is "because the initial state is uniform in `x`".
  - The manufactured-solution judgment is not applied. The non-application basis is "because `errors.mms_*` are `N/A` for a profile other than `tc4_translating_low` (5-3)".

### 6-5. `l2_tc2_long_run`
- `level`: `L2`
- `objective`: confirm that the steady state is held over five days for `tc2_zonal_uniform` at `nx=128`: bounded error growth, mass conservation, positivity, and the wall-normal velocity.
- target cases:
  - `chan_tc2_ref_n128_dts100_tend5d`
- `expected_outcome`: `pass`
- judgment conditions:
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of the case ($|\mathrm{run.n\_step}-\mathrm{n\_step}| \le 0.5$).
  - The depth-positivity judgment is applied. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 1.0e3$.
  - The mass-conservation judgment is applied. The evaluation expression is `metrics.mass_drift_rel`, and the threshold is $\le 5.0e{-10}$.
  - The theoretical-comparison judgment is applied. The evaluation expression is `errors.steady_h.l2_rel_tend`, and the band is $[7.2e{-4},\ 2.1e{-3}]$ as two judged quantities. `convergence_order` is not judged: the case is not a member of a refinement pair.
  - The wall-normal-velocity judgment is applied. The evaluation expression is `extrema.v.max_abs`, and the threshold is $\le 1.2$.
  - The initial-state judgment is not applied. The non-application basis is "because it belongs to 6-1".
  - The translation-equivariance judgment is not applied. The non-application basis is "because the pair case is not run".
  - The `x`-structure judgment is not applied. The non-application basis is "because the initial state is uniform in `x`".
  - The manufactured-solution judgment is not applied. The non-application basis is "because `errors.mms_*` are `N/A` for a profile other than `tc4_translating_low` (5-3)".

### 6-6. `l3_x_translation_equivariance_and_wave_decay`
- `level`: `L3`
- `objective`: confirm that the `x`-direction dynamics are those of the Controlled Spec §5 for `tc2_zonal_perturbed`: the initial state, the translation equivariance of the pair, the decay of the perturbation's `x`-structure, mass conservation and positivity.
- pair cases:
  - `reference` is `chan_tc2_xpert_n064_dts100`
  - `shifted` is `chan_tc2_xpert_n064_dts100_sx025`
- `expected_outcome`: `pass`
- judgment conditions:
  - The initial-state judgment is applied to both cases. The evaluation expressions are `errors.initial_h.linf_rel` with the threshold $\le 1.0e{-9}$ (against the closed form of 2-3 at the cell centres $(x_i,y_j)$, shift included), `errors.initial_u.linf_rel` with the threshold $\le 1.0e{-9}$, and `errors.initial_v.linf` with the threshold $\le 1.0e{-12}$.
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of the case ($|\mathrm{run.n\_step}-\mathrm{n\_step}| \le 0.5$).
  - The depth-positivity judgment is applied. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 1.0e3$.
  - The mass-conservation judgment is applied. The evaluation expression is `metrics.mass_drift_rel`, and the threshold is $\le 1.0e{-10}$.
  - The translation-equivariance judgment is applied. The evaluation expression is `errors.symmetry_h.l2_rel` (carried by the shifted case), and the threshold is $\le 2.0e{-11}$.
  - The `x`-structure judgment is applied to both cases. The evaluation expression is `xstructure.decay_ratio`, and the band is $[0.38,\ 0.95]$ as two judged quantities.
  - The theoretical-comparison judgment is not applied. The non-application basis is "because `tc2_zonal_perturbed` has no steady reference (2-4)".
  - The wall-normal-velocity judgment at $t_{end}$ is not applied. The non-application basis is "because the perturbation excites $v$ by design; the wall-normal velocity of the steady profiles is judged in 6-3 to 6-5".
  - The manufactured-solution judgment is not applied. The non-application basis is "because `errors.mms_*` are `N/A` for a profile other than `tc4_translating_low` (5-3)".

### 6-7. `l0_cfl_guard_xfail`
- `level`: `L0`
- `objective`: confirm that a `CFL`-violation case can be detected (expected failure).
- target cases:
  - `chan_guard_n032_dts240`
- `expected_outcome`: `xfail`
- `xfail_condition`: `cfl.max > 1.0`
- `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'cfl'`
- judgment conditions:
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of the case ($|\mathrm{run.n\_step}-\mathrm{n\_step}| \le 0.5$).
  - The depth-positivity judgment is applied and its real `extrema.h.min` value and status are reported against the threshold $\ge 1.0e3$ (it is never `N/A`; the 5-3 rule does not apply to it). It is non-gating for this test: its status is not a condition of `pass_when`, so a `fail` on it does not change this test's outcome.
  - The mass-conservation judgment is not applied. The non-application basis is "because the purpose of the guard test is only the detection of a stability-condition violation".
  - The theoretical-comparison judgment is not applied. The non-application basis is "because under an unstable condition, the can-continue-execution evaluation is done before the theoretical-agreement judgment".
  - The wall-normal-velocity judgment is not applied. The non-application basis is "because the purpose of the guard test is only the detection of a stability-condition violation".
  - The initial-state judgment is not applied. The non-application basis is "because it belongs to 6-1".
  - The translation-equivariance judgment is not applied. The non-application basis is "because the pair case is not run".
  - The `x`-structure judgment is not applied. The non-application basis is "because the initial state is uniform in `x`".
  - The manufactured-solution judgment is not applied. The non-application basis is "because `errors.mms_*` are `N/A` for a profile other than `tc4_translating_low` (5-3)".

### 6-8. `l0_tc4_initial_state_matches_analytic`
- `level`: `L0`
- `objective`: confirm that the discrete initial state of `tc4_translating_low` is the prescribed flow of 2-3 at $t=0$ at the cell centres: the depth of the background and the low, and both velocity components.
- target cases:
  - `chan_tc4_ref_n064_dts100`
- `expected_outcome`: `pass`
- judgment conditions:
  - The initial-state judgment is applied. The evaluation expressions are `errors.initial_h.linf_rel` with the threshold $\le 1.0e{-9}$, `errors.initial_u.linf_rel` with the threshold $\le 1.0e{-9}$, and `errors.initial_v.linf_rel` with the threshold $\le 1.0e{-9}$. `errors.initial_v.linf` is not judged: it is `N/A` for this profile (5-3).
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of the case ($|\mathrm{run.n\_step}-\mathrm{n\_step}| \le 0.5$).
  - The depth-positivity judgment is applied. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 8.0e3$.
  - The mass-conservation judgment is not applied. The non-application basis is "because the test judges the initial state and the conservation judgment belongs to 6-9".
  - The manufactured-solution judgment is not applied. The non-application basis is "because the test judges the initial state and the manufactured-solution judgment belongs to 6-9".
  - The theoretical-comparison judgment against a steady solution is not applied. The non-application basis is "because `tc4_translating_low` is not steady (2-4)".
  - The wall-normal-velocity judgment is not applied. The non-application basis is "because the low's $v$ is non-zero by design (2-5)".
  - The translation-equivariance judgment is not applied. The non-application basis is "because the pair case is not run".
  - The `x`-structure judgment is not applied. The non-application basis is "because the `x`-structure of `tc4_translating_low` is the low's, judged through the manufactured solution".

### 6-9. `l1_tc4_refinement_translating_low`
- `level`: `L1`
- `objective`: confirm, for `tc4_translating_low`, the error against the prescribed flow at $t_{end}$ and its second-order decrease with refinement, mass conservation and positivity; the forcing of the Controlled Spec §5 step 6 is exercised, and a run without it or with it frozen at $t=0$ fails the upper bound at `nx=64` and `nx=128` (2-7; at `nx=32` both lie inside the band, `3.244e-3` and `2.898e-3`).
- target cases:
  - `chan_tc4_ref_n032_dts100`
  - `chan_tc4_ref_n064_dts100`
  - `chan_tc4_ref_n128_dts100`
- `expected_outcome`: `pass`
- judgment conditions:
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of the case ($|\mathrm{run.n\_step}-\mathrm{n\_step}| \le 0.5$).
  - The depth-positivity judgment is applied. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 8.0e3$.
  - The mass-conservation judgment is applied. The evaluation expression is `metrics.mass_drift_rel`, and the threshold is $\le 1.0e{-10}$.
  - The manufactured-solution judgment is applied. `errors.mms_h.l2_rel_tend` applies the per-case band of 5-5 as two judged quantities (lower bound $1.2e{-3}$, $3.2e{-4}$, $4.8e{-5}$ and upper bound $3.6e{-3}$, $9.6e{-4}$, $1.45e{-4}$), and `convergence_order` requires $\ge 1.6$ for `convergence.n032_to_n064.l2_order` (carried by `chan_tc4_ref_n064_dts100`) and for `convergence.n064_to_n128.l2_order` (carried by `chan_tc4_ref_n128_dts100`). `errors.mms_h.linf_tend` and `errors.mms_vel.l2_rel_tend` are reported (5-3).
  - The theoretical-comparison judgment against a steady solution is not applied. The non-application basis is "because `tc4_translating_low` is not steady (2-4)".
  - The wall-normal-velocity judgment is not applied. The non-application basis is "because the low's $v$ is non-zero by design (2-5)".
  - The initial-state judgment is not applied. The non-application basis is "because it belongs to 6-8".
  - The translation-equivariance judgment is not applied. The non-application basis is "because the pair case is not run".
  - The `x`-structure judgment is not applied. The non-application basis is "because the `x`-structure of `tc4_translating_low` is the low's, judged through the manufactured solution".

### 6-10. `l2_tc4_long_run`
- `level`: `L2`
- `objective`: confirm, for `tc4_translating_low` at `nx=128` over five days, bounded error growth against the prescribed flow, mass conservation and positivity.
- target cases:
  - `chan_tc4_ref_n128_dts100_tend5d`
- `expected_outcome`: `pass`
- judgment conditions:
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of the case ($|\mathrm{run.n\_step}-\mathrm{n\_step}| \le 0.5$).
  - The depth-positivity judgment is applied. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 8.0e3$.
  - The mass-conservation judgment is applied. The evaluation expression is `metrics.mass_drift_rel`, and the threshold is $\le 5.0e{-10}$.
  - The manufactured-solution judgment is applied. The evaluation expression is `errors.mms_h.l2_rel_tend`, and the band is $[1.3e{-4},\ 4.0e{-4}]$ as two judged quantities. `convergence_order` is not judged: the case is not a member of a refinement pair.
  - The theoretical-comparison judgment against a steady solution is not applied. The non-application basis is "because `tc4_translating_low` is not steady (2-4)".
  - The wall-normal-velocity judgment is not applied. The non-application basis is "because the low's $v$ is non-zero by design (2-5)".
  - The initial-state judgment is not applied. The non-application basis is "because it belongs to 6-8".
  - The translation-equivariance judgment is not applied. The non-application basis is "because the pair case is not run".
  - The `x`-structure judgment is not applied. The non-application basis is "because the `x`-structure of `tc4_translating_low` is the low's, judged through the manufactured solution".

### 6-11. `l3_tc4_time_refinement_pair`
- `level`: `L3`
- `objective`: confirm that the time integration is the `RK4` of the Controlled Spec §5 with the `dt` rule of §3 and the forcing at the stage time, through the pair of 2-8.
- pair cases:
  - `reference` is `chan_tc4_ref_n032_dts100`
  - `halved` is `chan_tc4_ref_n032_dts100_half`
- `expected_outcome`: `pass`
- judgment conditions:
  - The time-refinement judgment is applied. The evaluation expression is `errors.time_pair.h_l2_rel` (carried by the halved case), and the band is $[9.0e{-9},\ 2.7e{-8}]$ as two judged quantities.
  - The `CFL` judgment is applied to both cases. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of each case ($|\mathrm{run.n\_step}-\mathrm{n\_step}| \le 0.5$).
  - The depth-positivity judgment is applied to both cases. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 8.0e3$.
  - The mass-conservation judgment is applied to both cases. The evaluation expression is `metrics.mass_drift_rel`, and the threshold is $\le 1.0e{-10}$.
  - The manufactured-solution judgment is applied to both cases. The evaluation expression is `errors.mms_h.l2_rel_tend`, and the band is the `nx=32` band $[1.2e{-3},\ 3.6e{-3}]$ of 5-5 as two judged quantities (the halved case's reference value is `2.420e-3`, equal to its base's to four digits). `convergence_order` is not judged: the halved case is not a member of a refinement pair.
  - The theoretical-comparison judgment against a steady solution is not applied. The non-application basis is "because `tc4_translating_low` is not steady (2-4)".
  - The wall-normal-velocity judgment is not applied. The non-application basis is "because the low's $v$ is non-zero by design (2-5)".
  - The initial-state judgment is not applied. The non-application basis is "because it belongs to 6-8".
  - The translation-equivariance judgment is not applied. The non-application basis is "because the translation pair is not run".
  - The `x`-structure judgment is not applied. The non-application basis is "because the `x`-structure of `tc4_translating_low` is the low's, judged through the manufactured solution".

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
