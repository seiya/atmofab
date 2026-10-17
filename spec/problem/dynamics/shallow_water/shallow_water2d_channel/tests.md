# Tests: 2D shallow water channel (verification input / judgment conditions)

## 0. Meta information
- `status`: `draft`
- `test_profile_id`: `shallow_water2d_channel_baseline`
- `test_profile_version`: `0.5.0`
- `spec_ref.spec_kind`: `problem`
- `spec_ref.spec_id`: `shallow_water2d_channel`
- `spec_ref.spec_version`: `0.2.5`
- `spec_ref.controlled_spec_path`: `spec/problem/dynamics/shallow_water/shallow_water2d_channel/controlled_spec.md`

## 1. Test purpose
This suite verifies, for the discrete implementation of the 2D shallow water equation on a channel with the Coriolis force, the discrete initial state against the analytic profiles, the error against the analytic solution — the steady solution of Williamson et al. (1992) Test Cases 2 and 3 and the forced translating low of Test Case 4, on the channel — in three norms over every state variable, bounded on both sides, since the fully fixed `p1` discretization makes it a property of the scheme, and its second-order decrease with refinement, the error of the unforced `x`-perturbed family against a restricted reference (`docs/TESTS.md` §Refinement judgment), the time integration through a pair of runs that differ only in `dt`, mass conservation, depth positivity, the wall-normal velocity, a long-time integration, the along-channel dynamics through an `x`-perturbed pair (translation equivariance and the decay of the perturbation's along-channel structure, which an update without the along-channel interface flux leaves unchanged), a rectangular grid, on which an exchange of the two extents, the two domain lengths or the two spacings is not the identity, the symmetry pairs of the Controlled Spec §5 — the axis exchange (the channel computed along `y`), the mirror along the channel and the mirror across it (`docs/TESTS.md` §Symmetry pairs) — and the `CFL` guard. The judgment targets are `L0` to `L3`, and include an expected failure (`xfail`). Every transformation `docs/TESTS.md` §Symmetry pairs names is admitted by the equations of this `problem` and carries a pair: the translation along the channel (6-6), the axis exchange (6-14), and the two mirrors (6-15, 6-16); a translation across the channel is not a transformation of a walled domain.

## 2. Input-defaulting rules
### 2-1. Basic constants
The constants are those of the Controlled Spec §6, in SI units: `g=9.80616`, `Omega=7.292e-5`, `a=6.37122e6`, `theta0 = pi/4`, `channel_axis=x` and `zonal_sign=+1` on every case but the symmetry-pair cases of 4-3, which override the inputs 4-3 names; so `f0 = 2*Omega*sin(theta0) = 1.03124...e-4`, `beta = 2*Omega*cos(theta0)/a = 1.61860...e-11` and the orientation sign `sigma = +1` (`-1` for `channel_axis=y`); `gh0=2.94e4` (`h0 = gh0/g = 2998.1155...`) and `u0 = zonal_sign*2*pi*a/(12*86400) = 38.6107...` for `tc2_zonal_uniform`, `tc3_compact_jet` and `tc2_zonal_perturbed`; `gh0=1.0e5` (`h0 = gh0/g = 10197.67...`) and `u_jet=40.0` for `tc4_translating_low`, whose derived parameters are `U_j = zonal_sign*u_jet = 40.0`, `psi0 = -0.03*gh0/f0 = -2.909106...e7`, `R_low = 2*a/12.74244 = 1.0000...e6`, `c_tr = U_j*cos(theta0) = 28.28427...`, `s_c0 = L_s/2` and `r_c = L_r/2` (with $s$, $r$, $L_s$, $L_r$ of the Controlled Spec §2). The ellipses mark derived values: the case inputs carry the constants `g`, `Omega`, `a`, `gh0` and `theta0` (the double-precision value of `pi/4`, or of `-pi/4` where 4-3 overrides it), `channel_axis`, `zonal_sign` and the parameter `u_jet` as values, and `h0`, `u0`, `f0`, `beta`, `sigma`, `U_j`, `psi0`, `R_low`, `c_tr`, `s_c0`, `r_c` are evaluated from them by the expressions above wherever they are used — by the runner and by a judgment that evaluates a reference field alike; no rounded literal of a derived value is an input. The one literal `gh0` that 4-3 sets to a derived value (`chan_tc3_ref_n064_dts100_ymir`) is an input of that case and is written to full double precision beside its closed form.

### 2-2. Coriolis profile
`coriolis_profile=beta_plane` in every case: $f(r)=f_0+\beta\,(r-L_r/2)$ at every interior cell centre, as the per-cell field of the Controlled Spec §6.

### 2-3. Flow profile (initial condition)
The profiles are those of the Controlled Spec §6, written in the along- and cross-channel coordinates $(s, r)$ and the orientation sign $\sigma$; on every case but the axis-exchange cases of 4-3, $(s, r)=(x, y)$ and $\sigma=+1$.
- `tc2_zonal_uniform`: $u_s=u_0$, $u_r=0$, $h(r)=h_0-\sigma\dfrac{u_0}{g}\left[f_0\,(r-L_r/2)+\dfrac{\beta}{2}\,(r-L_r/2)^2\right]$, with `L_x=L_y=6.0e6`.
- `tc3_compact_jet`: $r_b=L_r/8$, $r_e=7L_r/8$, $x_e=0.3$, $\zeta=x_e\,(r-r_b)/(r_e-r_b)$, $b(\zeta)=0$ ($\zeta\le0$), $e^{-1/\zeta}$ ($\zeta>0$), $u_s(r)=u_0\,b(\zeta)\,b(x_e-\zeta)\,e^{4/x_e}$, $u_r=0$, $h(r)=h_0-\dfrac{\sigma}{g}\int_0^r f\,u_s\,dr'$ by the composite Simpson quadrature of the Controlled Spec §6, with `L_x=L_y=8.0e6`. The branch-free form $b(\zeta)=\exp(-1/\max(\zeta,10^{-300}))$ of the Controlled Spec §6 is equal to $b(\zeta)$ at every cell centre of the sweep.
- `tc2_zonal_perturbed`: $h(s,r)=h_{TC2}(r)+\eta_0\sin\left(2\pi\,(s/L_s-\mathrm{shift\_x\_fraction})\right)$ with $h_{TC2}$ the `tc2_zonal_uniform` depth, $u_s=u_0$, $u_r=0$, `eta0=30.0` (`m`), with `L_x=L_y=6.0e6`.
- `tc4_translating_low`: the prescribed flow of the Controlled Spec §6 at $t=0$, $h=\tilde h$, $u=\tilde u$, $v=\tilde v$, with `L_x=L_y=8.0e6` (the rectangular case of 2-10 carries `L_x=6.0e6`, `ny=48`): $\tilde h=\bar h+f\psi/g$, with the background $\bar u=U_j\sin^2(\pi r/L_r)$ along the channel and the closed-form $\bar h$ referred to the channel centre ($\bar h(L_r/2)=h_0$), and $\psi$, $\psi_x$, $\psi_y$ of that section. The background depth ranges from $10882\ \mathrm{m}$ at the wall $r=0$ to $9199\ \mathrm{m}$ at the wall $r=L_r$; the low's depth anomaly is $-306\ \mathrm{m}$ at its centre and its streamfunction at the walls is at most $1.1\times10^{-7}\,|\psi_0|$ in magnitude ($L_r/2=4R_{low}$).

### 2-4. Theoretical solution (with applicability condition)
`tc2_zonal_uniform` and `tc3_compact_jet` are steady solutions of the continuous equations, so the reference at $t_{end}$ is the initial state: for `tc2_zonal_uniform` the closed form of 2-3 evaluated at the cell centres, $h_{ref}=h(r)$, the along-channel momentum $h(r)\,u_0$ and the cross-channel momentum $0$; for `tc3_compact_jet` the discrete initial state itself, $(h, hu, hv)_{ref}=(h, hu, hv)(t=0)$ (its $h$ has no closed form). The theoretical-agreement judgment at $t_{end}$ targets every state variable, `h`, `hu` and `hv`, in three norms (2-12); the wall-normal velocity is in addition judged by its own metric (2-5). `tc4_translating_low` is not steady: its reference at $t_{end}$ is the prescribed flow at $t_{end}$, $h_{ref}=\tilde h(x_i,y_j,t_{end})$, $hu_{ref}=\tilde h\tilde u$, $hv_{ref}=\tilde h\tilde v$ at $t_{end}$, whose low has moved a distance $c_{tr}\,t_{end}$ along the channel ($2444\ \mathrm{km}$ in one day); the theoretical-agreement judgment targets `h`, `hu` and `hv` in three norms, and the velocity error against $(\tilde u,\tilde v)$ at $t_{end}$ is reported. `tc2_zonal_perturbed` has no analytic reference at $t_{end}$; its family is judged against the restricted reference of 2-13, and its `n064` pair by 2-6. The initial state is judged against its closed form for every profile: `h` for `tc2_zonal_uniform`, `tc2_zonal_perturbed` and `tc4_translating_low` (2-3, at the cell centres), both velocity components for all four, and for `tc3_compact_jet` — whose `h` is defined by the quadrature — the depth of the lines before the jet ($r<r_b$), where $u_s=0$ on $[0,r]$ and the integral is exactly zero, so $h=h_0$, and the depth of the lines beyond the jet ($r>r_e$), where the integral is complete and equals the closed-form constant $h_N$ of the Controlled Spec §6. The two pin the constant of integration and the complete integral (the jet's normalisation $e^{4/x_e}$, $x_e$, $r_b$, $r_e$ and the $1/g$ factor). They do not separate quadrature rules: every composite rule is exact to round-off for the complete integral of this smooth, compactly supported integrand, and the lines inside the jet — where a trapezoid rule on the same sub-grid differs from the Simpson rule by up to $1.9\times10^{-7}$ relative at `ny=32` — are judged only against the case's own initial state.

### 2-5. Wall-normal velocity
The exact solution of `tc2_zonal_uniform` and `tc3_compact_jet` has $u_r=0$ everywhere. The mirror ghost yields a non-zero $u_r$ of order $\Delta r$ in the wall-adjacent lines (the boundary `component`'s §2), so a "zero cross-channel momentum at the wall" judgment is not applicable to this discretization; the judged quantity is $\max_{i,j}|u_{r,i,j}(t_{end})|$ over the whole domain, with a per-case threshold decreasing with refinement. The same $O(\Delta r)$ term is the reason the `linf` orders of the cross-channel momentum of `tc2_zonal_uniform` and of the restricted `tc2_zonal_perturbed` family are below one (2-12). For `tc4_translating_low`, whose $u_r$ is the low's by design, the metric is reported and not judged (`13.34`, `21.33`, `23.42` (`m/s`) at `nx=32`, `64`, `128`, `20.15` on the rectangular case).

### 2-6. Along-channel dynamics (translation equivariance and the decay of the along-channel structure)
The profiles uniform along the channel exercise no along-channel interface flux and no periodic mapping (Controlled Spec §5, uniformity along the channel), so they cannot detect an update that omits either. `tc2_zonal_perturbed` does, in two ways. First, `f` depends on $r$ alone and the channel is periodic, so a run whose initial state is shifted by a whole number of cells along the channel (`shift_x_fraction` such that $\mathrm{shift\_x\_fraction}\cdot nx$ is an integer) is the unshifted run shifted by the same number of cells, up to round-off; the pair is judged by `errors.symmetry_h.l2_rel`. Second, the wavenumber-one perturbation is a gravity-wave excitation that disperses as it propagates along the channel and that the `p1` scheme's numerical diffusion damps; its along-channel structure at $t_{end}$ relative to $t=0$ is measured by `xstructure.decay_ratio` (5-4), whose value is a property of the fixed discretization at the case's resolution (reference `0.7418`, `0.7666`, `0.7738`, `0.7762` at `nx=32`, `64`, `128`, `256`), while an update without the along-channel interface flux leaves the structure unchanged (ratio `1.000` to three digits — the lines across the channel evolve independently) and an outflow boundary along the channel breaks mass conservation and the equivariance. The upper bound `0.95` of 5-5 lies between the two, `1.22` to `1.28` times the reference and `0.95` of the ratio without the along-channel interface flux.

### 2-7. The forcing of `tc4_translating_low`
The forcing formula is verified by the forcing `component`'s own `L0` suite, against a central-difference residual of the prescribed flow, in both orientations. This suite judges the forced run as a whole against the prescribed flow at $t_{end}$: the calibration of 5-5 puts a run without the forcing at `2.658e-3` / `2.743e-3` (`nx=64` / `128`) and a run with the forcing frozen at $t=0$ at `2.678e-3` / `2.829e-3`, 2.9 to 20.2 times the upper bound of `errors.mms_h.l2_rel_tend` at those resolutions (at `nx=32` both lie inside the band, `3.117e-3` and `2.722e-3`), and a state left at its initial value at `6.499e-3`. A run that evaluates the forcing at the time of the step's start instead of the stage time differs from the reference implementation by `0.03%` / `0.3%` / `1.6%` in `errors.mms_h.l2_rel_tend` (`nx=32` / `64` / `128`), inside the band; that substitution is detected by the time-refinement pair of 2-8, not by the band. Four substitutions stay inside every band of `errors.mms_h.*`, measured on the reference implementation: the conservation-form products of the forcing `component`'s §3 ($\tilde h F_u+\tilde u F_h$, $\tilde h F_v+\tilde v F_h$) taken with the stage state's depth and velocity instead of the prescribed flow's (`0.10%` to `0.40%` from the reference), `dubar` taken as a difference of the `ubar` array instead of its closed form (within `0.02%`), the `hbar` array replaced by the along-channel mean of the stage depth (`0.06%` to `1.0%`), and a reconstruction of the primitive variables $(h,u,v)$ instead of the conserved ones (`0.6%` to `4.2%`). The time-refinement pair of 2-8 excludes three of the four (`1.903e-8`, `1.724e-8`, `1.850e-8`); the `hbar` substitution lies inside the pair band (`2.479e-8`), so no judgment of this suite excludes it: the Controlled Spec §5 step 6 passes the setup-time background arrays, and §7 forbids a forcing evaluated from the state. Evaluating every term of the forcing's residuals at the stage state (not only the conservation-form products) gives `2.564e-4` / `1.076e-4` / `2.128e-5`, below the lower bound at every resolution. The `ubar` array replaced by the along-channel mean of the stage velocity lies outside the pair band (`1.743e-8`), inside the `nx=32` band (`1.3%` from the reference) and just inside the `nx=64` upper bound (`9.192e-4` against `9.2e-4`), and is excluded by the upper bound of `errors.mms_h.l2_rel_tend` at `nx=128` (`7.758e-4` against `1.4e-4`).

### 2-8. Time-refinement pair
The error against the reference at $t_{end}$ is dominated by the spatial discretization: replacing `RK4` by a second- or third-order Runge–Kutta method, or changing `cfl_target` between `0.2` and `0.6`, changes `errors.steady_h.l2_rel_tend` and `errors.mms_h.l2_rel_tend` by less than `0.05%` at `nx=32` and `64`. The time integration is judged by a pair of `tc4_translating_low` runs at `nx=32` that differ only in `dt_scale` (`1.0` and `0.5`): the relative difference of their final depths is `errors.time_pair.h_l2_rel` (5-4). Its reference value is `2.4847e-8`; the pair at `dt_scale` `0.5` and `0.25` gives `4.876e-9`, a ratio of `5.1`, so the limited scheme is not in the asymptotic regime of `RK4` and the value is not the time error of a fourth-order method. The value is a property of the fixed discretization and the `n_step` of §3: perturbing the state by a relative `1e-15` at every step moves it by `4e-9` of itself. A two-stage second-order method gives `2.45e-6`, `SSPRK3` `8.49e-8`, the three-eighths-rule fourth-order method `1.14e-8`, the forcing at the step's start time or end time for all four stages `2.94e-6` and `2.95e-6`, and at the step's midpoint for all four stages `3.04e-8`; `cfl_target` `0.2`, `0.3`, `0.41`, `0.48` and `0.6` give `4.0e-9`, `8.5e-9`, `2.01e-8`, `3.34e-8` and `4.16e-8`; each lies outside the band of 5-5. The pair does not fix the stage times at which the forcing is read, nor the tableau of a four-stage fourth-order method: the classic `RK4` tableau with the forcing read at $t+\{0,\tfrac13,\tfrac23,1\}\,dt$ gives `2.486e-8`, and the four-stage fourth-order method with $c_2=0.45$, $c_3=0.55$ gives `2.480e-8`, both inside the band; the tableau and the stage times are those of the time-update `component`, which the Controlled Spec §5 calls and which that `component`'s own suite pins. The value does not change monotonically with `cfl_target` (`0.42`, `0.43` and `0.47` give `2.19e-8`, `2.20e-8` and `2.77e-8`, inside the band), so the pair does not fix `cfl_target` to `0.45`; `run.n_step` does.

### 2-9. The `CFL` guard case
`chan_guard_n032_dts240` carries `dt_scale=2.40`, so the §3 procedure gives $\mathrm{n\_step}=184$ and $dt=469.57$ (`s`), and the stability index of the Controlled Spec §5 is `1.077` at the initial state and `1.080` at its maximum over the run (`cfl.max`, 5-4) — above the threshold `1.0` of the `CFL` judgment, which is what 6-7 detects. The case performs all of its §3 steps and stays wet: the depth minimum over the initial state and every committed state is the initial minimum, `1548.9` (`m`), against the positivity threshold `1.0e3` of 5-5, measured on the reference implementation of 5-5. The wet-domain error of the Controlled Spec §5 is not reached by this case. A run of this case whose step loop stops on that error performs fewer steps: its `run.n_step` is the count of steps performed (§3), its judgment against the §3 $\mathrm{n\_step}$ in 6-7 reports `fail`, and 6-7 states what that status decides. The step count and the depth minimum stated here are properties of the fixed discretization at the case's inputs, not conditions of the `pass_when` of 6-7, which reads the `CFL` judgment alone: the `run.n_step` and depth-positivity judgments of the case are applied and their values and statuses reported (6-7).

### 2-10. The rectangular case
Every case of §4 but three carries $nx=ny$ and `L_x=L_y`, so an implementation that exchanges the two extents, the two domain lengths or the two spacings — an array declared `(ny, nx)`, a loop bounded by `ny` where `nx` is meant, `dx` where `dy` is meant, an `rhs` that reads its stage state as `(ncomp, ny, nx)` — gives the numbers of a correct one on those cases. `chan_tc4_ref_n064_dts100_rect` (4-3) carries `nx=64`, `ny=48`, `L_x=6.0e6` and `L_y=8.0e6`, so $dx=93750$ and $dy=166666.\overline{6}$ (`m`), and no exchange within any of the three pairs is the identity on a derived quantity; its two transformed cases (`_rectaxis`, `_rectxmir`, 4-3) carry the same property in the other orientation and under the mirror. The §3 procedure gives $\mathrm{n\_step}=1152$ and $dt=75.00$ (`s`). The reference value of `errors.mms_h.l2_rel_tend` is `9.591e-4` (`errors.mms_h.linf_tend` `74.8`, `errors.mms_vel.l2_rel_tend` `6.50e-2`, `extrema.v_cross.max_abs` `20.1`, `extrema.h.min` `9197.0`), measured on the reference implementation of 5-5; a state left at its initial value gives `7.290e-3`. Measured on the same implementation with one substitution at a time: `dx` and `dy` exchanged in the interface-flux difference gives `3.87e-2`; exchanged in the dt rule alone, the run performs 1114 steps against the 1152 of §3 with the error unchanged, so that substitution is detected by the `run.n_step` judgment and by no band; the cell centres evaluated with the spacings exchanged at setup gives `9.5e-2` in `errors.initial_h.linf_rel` and `6.73e-2`; `L_x` and `L_y` exchanged gives `2.6e-2` in `errors.initial_h.linf_rel` and `1.70e-2`; an `rhs` that reads the stage state as `(ncomp, ny, nx)` through an explicit-shape declaration gives `2.38e-2` and `2.0e-2` in `metrics.mass_drift_rel`; an `x` loop bounded by `ny` gives `2.1e-3` in `metrics.mass_drift_rel` and `5.36e-3`; the forcing omitted or frozen at $t=0$ gives `2.30e-3` and `2.68e-3`. Each lies outside the band of 5-5 or fails the judgment named. With `L_x=L_y=8.0e6` and `ny=48`, the exchange of `L_x` and `L_y` gives the reference value (`9.323e-4` both). The streamfunction of the low at half the along-channel period is `2.6e-2` of $|\psi_0|$ on this channel (`1.5e-3` at `L_x=8.0e6`); the prescribed flow is an exact solution of the forced equations at every `L_x` (the Controlled Spec §6), and the sum of the forcing's depth component over the grid is below $10^{-15}$ of the sum of its magnitude at every time of the run.

### 2-11. Symmetry pairs
The Controlled Spec §5 states three equivariances, and each is judged by pairs of cases (`docs/TESTS.md` §Symmetry pairs): a base case of §4 and its transformed case of 4-3, which differs from the base only in the inputs 4-3 names and carries the metric.
- **Axis exchange** (6-14): the transformed case computes the channel along `y` — `channel_axis=y`, `nx` ↔ `ny`, `L_x` ↔ `L_y`, `Omega=-7.292e-5` — and its final state is $(h,\ hu,\ hv)'=(h^T,\ hv^T,\ hu^T)$ of the base's, with $^T$ the exchange of the two array axes. The rotation reversal is part of the symmetry: the `y`-channel of `tc4_translating_low` at `nx=32` with `Omega` unchanged differs from the transposed base by `1.29e-1` in the `l2` norm of `h`, so the pair pins the sign of the Coriolis term in the exchanged orientation.
- **Mirror along the channel** (6-15): `Omega=-7.292e-5`, `zonal_sign=-1` (and `shift_x_fraction` → $\tfrac12-$`shift_x_fraction` on the perturbed profile); the final state is $(h,\ -hu,\ hv)$ of the base's reflected along `x` (the first array axis, `x`, reversed: the grammar's `flip(·, 0)`).
- **Mirror across the channel** (6-16): `theta0=-pi/4` (so $f_0\to-f_0$ with $\beta$ kept, which reflects $f(r)$ about the centre with its sign reversed); the final state is $(h,\ hu,\ -hv)$ of the base's reflected along `y` (the second array axis, `y`, reversed: the grammar's `flip(·, 1)`). Every profile is admitted: `tc2_zonal_uniform`, `tc2_zonal_perturbed` and `tc4_translating_low` refer their depth to the channel centre, and `tc3_compact_jet`, whose depth is referred to the wall $r=0$, carries `gh0` $=g\,h_N$ of the base, $29400-f_0\,u_0\,e^{4/x_e}\,(r_e-r_b)\,C/x_e=23889.890819821492$ (`m2/s2`, with the constants of 2-1, $L_r=8.0\times10^6$ and $C$ of the Controlled Spec §6), so that the reflected depth is referred to the other wall. Without the shift the `tc3_compact_jet` pair differs by the uniform $h_0-h_N=561.9\ \mathrm{m}$ and its `n_step` by `48`.

Each pair is judged on every state variable by an `l2` residual (5-4), normalised by the `l2` norm of the base's variable (a momentum component by the larger `l2` norm of the base's two momentum components), with the bound `2.0e-11`: the transformed case runs the same operations on transformed data, so the residual of a conforming kernel is round-off, but not zero (the Controlled Spec §5: the forcing's summation order under the exchange, the rounding of the low's centre under a reversed $c_{tr}$, a contracted multiply-add, a vector remainder loop). On the reference implementation every pair of `tc2_zonal_uniform`, `tc2_zonal_perturbed` and `tc3_compact_jet` reads `0` (`5.2e-15` on the `tc3_compact_jet` mirror across the channel, whose `gh0` literal and quadrature are not exactly reflected), and every pair of `tc4_translating_low` reads at most `3.1e-15`; the transformed case performs the same `n_step` as its base (`883`, `886`, `1152`, `567` for the axis exchange; `883`, `886`, `567`, `1087`, `1152` for the mirror along the channel; `883`, `886`, `1087`, `567` for the mirror across it).

What a pair sees that no band of this suite sees, measured on the reference implementation with one defect at a time, written into the kernel and so present in every run that takes the defective code path: the periodic ghost columns of the `y` orientation filled from one column too far reads `1.1e-3` in `h` and `6.7e-3` in `hv` on the axis-exchange pair of the perturbed profile, whose decay ratio (`0.823`) stays inside its band — the uniform profiles do not see it (`0`), which is why the perturbed profile carries a pair; the sign reversal at the wall applied to the along-channel momentum instead of the cross-channel one in the `y` orientation reads `1.6e-3` in `h` and `2.3e-2` in `hv` on the rectangular `tc4_translating_low` pair; the ghost columns beyond $x=L_x$ filled from one column too far reads `4.9e-4` in `h` and `2.2e-2` in `hv` on the `tc4_translating_low` mirror along the channel, whose two `errors.mms_h.l2_rel_tend` (`7.08e-4`, `6.61e-4`) stay inside the `nx=64` band; the ghost rows beyond $y=L_y$ filled from one row too far reads `9.1e-5` in `h` and `8.5e-4` in `hu` on the `tc4_translating_low` mirror across the channel, whose two errors (`6.15e-4`, `6.11e-4`) stay inside the band. Each is at least six orders of magnitude above the bound. A kernel that maps a transformed case onto its base internally and transforms the result back reads `0` on every pair; the Controlled Spec §7 forbids that mapping.

### 2-12. Three norms and orders of the analytic families
The error of every judged level of `chan_tc2_ref`, `chan_tc3_ref` and `chan_tc4_ref` against its analytic reference (2-4) is judged in the `l1`, `l2` and `linf` norms over `h`, `hu` and `hv` (`docs/TESTS.md` §Refinement judgment), and the order of each norm and variable over each pair of adjacent levels. The normalisers (5-4): `h` and the along-channel momentum by the same norm of their own reference; the cross-channel momentum by its own reference's norm for `tc4_translating_low`, whose cross-channel flow is the low's, and by the along-channel momentum reference's norm for the unforced profiles, whose cross-channel reference is zero. The reference values (5-5) are measured on the reference implementation. The expected order is two (the `p1` scheme); the norms below `1.5` for a known reason: on `tc2_zonal_uniform` the `linf` order of `h` is `1.48` / `1.70` (the `MC` limiter at the extremum lines) and the `l2` and `linf` orders of the momentum are `0.65` to `1.45` (the $O(\Delta r)$ cross-channel velocity in the wall-adjacent lines, 2-5, and the wall line's along-channel momentum carrying the same term); `tc3_compact_jet` is at or above `1.49` in every norm; `tc4_translating_low` at or above `1.59`. Over the second pair the order of every norm of every variable of `tc4_translating_low`, and of every norm of `h` of `tc3_compact_jet`, exceeds two (`2.47` to `2.72`), and the bounds take that spread.

### 2-13. The restricted reference of the perturbed family
`tc2_zonal_perturbed` has no analytic reference at $t_{end}$. Its family `chan_tc2_xpert` (4-1) carries four levels, `nx=ny=32`, `64`, `128`, `256`, and the finest level is the restricted reference (`docs/TESTS.md` §Refinement judgment): its final `h`, `hu` and `hv` averaged over the $f\times f$ blocks of cells that cover each coarser cell ($f=256/n$; the grammar function `coarsen`). The three coarser levels are judged against it in three norms over `h`, `hu` and `hv` with the normalisers of 2-12 (the cross-channel momentum, a residue of the flow, by the along-channel momentum reference's norm), and the order of each norm and variable over the two pairs `32→64` and `64→128`; the finest level judges no error of its own. The order of a pair against a reference only twice finer includes the reference's own error: for a scheme of order $p$ the pair $(n, 2n)$ against the level $N$ reads about $p+\log_2\frac{1-(n/N)^p}{1-(2n/N)^p}$ (`2.07` and `2.32` for $p=2$, $N=256$), and the bounds are the calibrated values. The restricted `l1` error of `h` lies within `2.5%` of the `tc2_zonal_uniform` error against its exact reference at every level, so the steady part of the flow dominates it; the perturbation's own error (the `l2` norm of the perturbation part of $h$ against its restricted reference, over the cells, normalised by `eta0`) is `0.48`, `0.13`, `0.025` at `32`, `64`, `128`.

## 3. Execution-control rules
- $t_{start}=0$ and $t_{end}=86400$ (`s`; one day), unless a case overrides `t_end`.
- `dt` is decided by the following procedure.
  1. Evaluate $\lambda_0=\max_{i,j}\left((|u_{i,j}|+\sqrt{gh_{i,j}})/dx+(|v_{i,j}|+\sqrt{gh_{i,j}})/dy\right)$ at the initial time.
  2. $\mathrm{dt\_raw}=\mathrm{dt\_scale}\cdot \mathrm{cfl\_target}/\lambda_0$.
  3. $\mathrm{n\_step}=\lceil (t_{end}-t_{start})/\mathrm{dt\_raw}\rceil$.
  4. $dt=(t_{end}-t_{start})/\mathrm{n\_step}$.
- $\mathrm{cfl\_target}=0.45$.
- The time passed to the time-update `component` at step $n$ ($n=0..\mathrm{n\_step}-1$) is $t_{start}+n\,dt$.
- The stop condition is $n=\mathrm{n\_step}$. The number of steps performed is a state variable of the case: the state the host captures after `case_setup` and after `case_run` consists of `h`, `hu`, `hv` (`nx` × `ny` each, the first array axis `x` and the second `y` in either orientation) and the scalar `n_step` (`0` after setup, the count of `RK4` steps performed after the run, incremented by the step loop at each step), and it is emitted as the diagnostic `run.n_step`.

## 4. Case-expansion rules
### 4-1. family definition
The `sweep` and fixed values per `family` are defined below.

| family_id | purpose | sweep | fixed |
| --- | --- | --- | --- |
| `chan_tc2_ref` | refinement_for_steady_geostrophic_flow | $nx=ny\in\{32,64,128\}$ | `flow_profile=tc2_zonal_uniform`, `coriolis_profile=beta_plane`, `channel_axis=x`, `zonal_sign=+1`, `L_x=L_y=6.0e6`, `gh0=2.94e4`, `dt_scale=1.0` |
| `chan_tc3_ref` | refinement_for_compact_jet | $nx=ny\in\{32,64,128\}$ | `flow_profile=tc3_compact_jet`, `coriolis_profile=beta_plane`, `channel_axis=x`, `zonal_sign=+1`, `L_x=L_y=8.0e6`, `gh0=2.94e4`, `dt_scale=1.0` |
| `chan_tc4_ref` | refinement_for_forced_translating_low | $nx=ny\in\{32,64,128\}$ | `flow_profile=tc4_translating_low`, `coriolis_profile=beta_plane`, `channel_axis=x`, `zonal_sign=+1`, `L_x=L_y=8.0e6`, `gh0=1.0e5`, `u_jet=40.0`, `dt_scale=1.0` |
| `chan_tc2_xpert` | x_translation_equivariance_wave_decay_and_restricted_refinement | $nx=ny\in\{32,64,128,256\}$ | `flow_profile=tc2_zonal_perturbed`, `coriolis_profile=beta_plane`, `channel_axis=x`, `zonal_sign=+1`, `L_x=L_y=6.0e6`, `gh0=2.94e4`, `eta0=30.0`, `shift_x_fraction=0.0`, `dt_scale=1.0` |
| `chan_guard` | expected_failure_for_cfl | $nx=ny=32,\ \mathrm{dt\_scale}=2.40$ | `flow_profile=tc2_zonal_uniform`, `coriolis_profile=beta_plane`, `channel_axis=x`, `zonal_sign=+1`, `L_x=L_y=6.0e6`, `gh0=2.94e4` |

Every case carries `ng=2` (the Controlled Spec §3) and the constants `g`, `Omega`, `a`, `theta0` of 2-1.

### 4-2. `case_id` generation rule
- The template is `{family}_n{nx:03d}_dts{dts_pct:03d}`.
- `dts_pct` is decided by $\mathrm{round}(100\cdot \mathrm{dt\_scale})$.
- The expansion order is fixed in the order `family`, `nx`, `dt_scale`.

### 4-3. Explicit-override cases
Each override case references a `base_case_id` and overrides only the inputs listed; its `case_id` keeps its base's and carries a suffix, so it sorts after its base case (`docs/TESTS.md` §Refinement judgment, carrying case).
- Add the `case_id` `chan_tc2_ref_n128_dts100_tend5d`.
- The `base_case_id` references `chan_tc2_ref_n128_dts100`, and overrides only `t_end` to `432000` (`s`; five days).
- Add the `case_id` `chan_tc4_ref_n128_dts100_tend5d`.
- The `base_case_id` references `chan_tc4_ref_n128_dts100`, and overrides only `t_end` to `432000` (`s`; five days).
- Add the `case_id` `chan_tc4_ref_n032_dts100_half`.
- The `base_case_id` references `chan_tc4_ref_n032_dts100`, and overrides only `dt_scale` to `0.5` (the time-refinement pair of 2-8; the `case_id` keeps its base's `dts100`).
- Add the `case_id` `chan_tc2_xpert_n064_dts100_sx025`.
- The `base_case_id` references `chan_tc2_xpert_n064_dts100`, and overrides only `shift_x_fraction` to `0.25` (a shift of 16 cells at `nx=64`).
- Add the `case_id` `chan_tc4_ref_n064_dts100_rect`.
- The `base_case_id` references `chan_tc4_ref_n064_dts100`, and overrides only `ny` to `48` and `L_x` to `6.0e6` (the rectangular case of 2-10).

The symmetry-pair cases of 2-11. The `base_case_id` of each is the base whose final state it reads (2-11, 5-4), and the overrides are relative to that base; `-7.292e-5` is `-Omega` and `-0.7853981633974483` is the double-precision value of `-pi/4`.
- Axis exchange (`channel_axis=y`, `Omega=-7.292e-5`, the extents and lengths exchanged):
  - `chan_tc2_ref_n064_dts100_axis`: the `base_case_id` references `chan_tc2_ref_n064_dts100`; overrides only `channel_axis` to `y` and `Omega` to `-7.292e-5` (`nx=ny`, `L_x=L_y`, so the exchange of the extents and lengths leaves them unchanged).
  - `chan_tc3_ref_n064_dts100_axis`: the `base_case_id` references `chan_tc3_ref_n064_dts100`; overrides only `channel_axis` to `y` and `Omega` to `-7.292e-5`.
  - `chan_tc2_xpert_n064_dts100_sx025_axis`: the `base_case_id` references `chan_tc2_xpert_n064_dts100_sx025`; overrides only `channel_axis` to `y` and `Omega` to `-7.292e-5`.
  - `chan_tc4_ref_n064_dts100_rectaxis`: the `base_case_id` references `chan_tc4_ref_n064_dts100_rect`; overrides only `nx` to `48`, `ny` to `64`, `L_x` to `8.0e6`, `L_y` to `6.0e6`, `channel_axis` to `y` and `Omega` to `-7.292e-5` (the transpose of `_rect`).
- Mirror along the channel (`Omega=-7.292e-5`, `zonal_sign=-1`):
  - `chan_tc2_ref_n064_dts100_xmir`: the `base_case_id` references `chan_tc2_ref_n064_dts100`; overrides only `Omega` to `-7.292e-5` and `zonal_sign` to `-1`.
  - `chan_tc3_ref_n064_dts100_xmir`: the `base_case_id` references `chan_tc3_ref_n064_dts100`; the same two overrides.
  - `chan_tc4_ref_n064_dts100_xmir`: the `base_case_id` references `chan_tc4_ref_n064_dts100`; the same two overrides.
  - `chan_tc2_xpert_n064_dts100_xmir`: the `base_case_id` references `chan_tc2_xpert_n064_dts100`; the same two overrides and `shift_x_fraction` to `0.5`.
  - `chan_tc4_ref_n064_dts100_rectxmir`: the `base_case_id` references `chan_tc4_ref_n064_dts100_rect`; the same two overrides.
- Mirror across the channel (`theta0=-pi/4`):
  - `chan_tc2_ref_n064_dts100_ymir`: the `base_case_id` references `chan_tc2_ref_n064_dts100`; overrides only `theta0` to `-0.7853981633974483`.
  - `chan_tc3_ref_n064_dts100_ymir`: the `base_case_id` references `chan_tc3_ref_n064_dts100`; overrides only `theta0` to `-0.7853981633974483` and `gh0` to `23889.890819821492` ($=g\,h_N$ of the base, 2-11).
  - `chan_tc4_ref_n064_dts100_ymir`: the `base_case_id` references `chan_tc4_ref_n064_dts100`; overrides only `theta0` to `-0.7853981633974483`.
  - `chan_tc2_xpert_n064_dts100_ymir`: the `base_case_id` references `chan_tc2_xpert_n064_dts100`; overrides only `theta0` to `-0.7853981633974483`.

## 5. Diagnostics contract
### 5-1. Artifacts
- The diagnostic output file is `diagnostics.json`.
- The judgment output file is `verdict.json`.

### 5-2. Required diagnostic items
`diagnostics.json` requires the following fields, each a scalar. `<v>` stands for each of `h`, `hu`, `hv`; `<k>` for each of `l1`, `l2`, `linf`; `<pair>` for each of `n032_to_n064` and `n064_to_n128`; `<n>` for each of `n032`, `n064`, `n128`.
- `cfl.max`
- `conserved.mass.initial`
- `conserved.mass.final`
- `conserved.momentum_x.initial`
- `conserved.momentum_x.final`
- `metrics.mass_drift_rel`
- `extrema.h.min`
- `extrema.v_cross.max_abs`
- `errors.initial_h.linf_rel`
- `errors.initial_h.below_jet_rel`
- `errors.initial_h.above_jet_rel`
- `run.n_step`
- `errors.initial_u.linf_rel`
- `errors.initial_v.linf_rel`
- `errors.initial_vel_cross.linf`
- `errors.symmetry_h.l2_rel`
- `xstructure.decay_ratio`
- `errors.steady_<v>.<k>_rel_tend` (nine fields)
- `errors.steady_h.linf_tend`
- `errors.mms_<v>.<k>_rel_tend` (nine fields)
- `errors.mms_h.linf_tend`
- `errors.mms_vel.l2_rel_tend`
- `errors.time_pair.h_l2_rel`
- `convergence.<pair>.<k>_order` (the order of `h`; six fields)
- `convergence.<pair>.hu_<k>_order`, `convergence.<pair>.hv_<k>_order` (twelve fields)
- `errors.restricted.<n>.<v>_<k>_rel` (twenty-seven fields)
- `convergence.restricted.<pair>.<v>_<k>_order` (eighteen fields)
- `errors.axis_exchange.<v>_l2_rel` (three fields)
- `errors.mirror_x.<v>_l2_rel` (three fields)
- `errors.mirror_y.<v>_l2_rel` (three fields)

### 5-3. `N/A` rule
- When a diagnostic item is incomputable or non-applicable, make the output value `null` and require `reason_na`.
- `errors.initial_h.linf_rel` is `N/A` for `flow_profile=tc3_compact_jet` (its initial `h` is defined by the quadrature and has no independent closed form); it is computed for `tc2_zonal_uniform`, `tc2_zonal_perturbed` and `tc4_translating_low`.
- `errors.initial_h.below_jet_rel` and `errors.initial_h.above_jet_rel` are `N/A` for anything other than `flow_profile=tc3_compact_jet`.
- `errors.initial_u.linf_rel` and `errors.initial_v.linf_rel` are computed for every case. `errors.initial_vel_cross.linf` is computed for the three unforced profiles, whose cross-channel velocity is zero by definition, and is `N/A` for `tc4_translating_low`.
- `errors.symmetry_h.l2_rel` is `N/A` for anything other than the shifted case of the translation pair; it is carried by `chan_tc2_xpert_n064_dts100_sx025`, which sorts after its base case, and reads the base case's final state.
- `xstructure.decay_ratio` is `N/A` for anything other than `flow_profile=tc2_zonal_perturbed`.
- `errors.time_pair.h_l2_rel` is `N/A` for anything other than the halved-`dt` case of the time-refinement pair; it is carried by `chan_tc4_ref_n032_dts100_half`, which sorts after its base case, and reads the base case's final state.
- `run.n_step` is computed for every case and is never `N/A`.
- The steady-solution fields `errors.steady_*` are computed for `flow_profile=tc2_zonal_uniform` and `tc3_compact_jet` and are `N/A` for `tc2_zonal_perturbed`, which has no steady reference, and for `tc4_translating_low`, which is not steady. The manufactured-solution fields `errors.mms_*` are computed for `flow_profile=tc4_translating_low` only and are `N/A` for every other profile.
- The `convergence.n032_to_n064.*` fields (the three of `h`, the three of `hu`, the three of `hv`) are carried by `chan_tc2_ref_n064_dts100`, `chan_tc3_ref_n064_dts100` and `chan_tc4_ref_n064_dts100`, and the `convergence.n064_to_n128.*` fields by `chan_tc2_ref_n128_dts100`, `chan_tc3_ref_n128_dts100` and `chan_tc4_ref_n128_dts100`; every other case omits them (5-4).
- The `errors.restricted.*` and `convergence.restricted.*` fields are carried by `chan_tc2_xpert_n256_dts100`, which sorts after the three coarser levels of its family and reads their final states; every other case omits them (5-4).
- `errors.axis_exchange.*` is carried by each axis-exchange case of 4-3, `errors.mirror_x.*` by each mirror-along-the-channel case, and `errors.mirror_y.*` by each mirror-across-the-channel case, each reading its base's final state; every other case omits them.
- `conserved.momentum_x.*` are reported and not judged: the Coriolis source exchanges momentum between `hu` and `hv`, so $P^x$ is not conserved.
- `errors.steady_h.linf_tend`, `errors.mms_h.linf_tend` and `errors.mms_vel.l2_rel_tend` are reported and not judged; the judgment is on the relative norms.
- `extrema.v_cross.max_abs` is computed for every case, and is judged only for the `chan_tc2_ref` and `chan_tc3_ref` cases of 6-3 to 6-5 (2-5).

### 5-4. Definition of the metrics
The relative mass-drift value is defined by the following, and emitted as the field `metrics.mass_drift_rel`.
$$
\mathrm{mass\_drift\_rel}=
\frac{|M_{end}-M_0|}{\max(|M_0|,1e{-14})},
\quad
M=\sum_{i,j} h_{i,j}\,dx\,dy
$$
Here $M_0$ is the field `conserved.mass.initial` and $M_{end}$ is `conserved.mass.final`. `conserved.momentum_x.*` is $P^x=\sum_{i,j}(hu)_{i,j}\,dx\,dy$ at the two times.

The initial-state errors are defined by the following, with $h_{ref}$ the closed form of 2-3 at the cell centres (a function of $r$ for `tc2_zonal_uniform`, of $(s, r)$ for `tc2_zonal_perturbed`, $\tilde h(x_i,y_j,0)$ for `tc4_translating_low`), $(u_{ref}, v_{ref})$ the closed-form velocity of 2-3 in `x` and `y` components (the along-channel velocity $u_s$ of the profile in the component `channel_axis` names and $0$ in the other for the three unforced profiles, $(\tilde u, \tilde v)(x_i,y_j,0)$ for `tc4_translating_low`), $U_s$ the velocity scale ($|u_0|$, and $u_{jet}$ for `tc4_translating_low`), $u_{i,j}=(hu)_{i,j}/h_{i,j}$ and $v_{i,j}=(hv)_{i,j}/h_{i,j}$, and, for `tc3_compact_jet`, $J_b$ the cells whose cross-channel coordinate is below $r_b$ and $J_e$ those above $r_e$, $h_0=gh_0/g$ with the case's `gh0`, and $h_N$ the closed-form constant of the Controlled Spec §6 with the case's inputs.
$$
\mathrm{initial\_h\_linf\_rel}=\frac{\max_{i,j}|h_{i,j}(0)-h_{ref,i,j}|}{\max_{i,j}|h_{ref,i,j}|},\qquad
\mathrm{initial\_h\_below\_jet\_rel}=\frac{\max_{(i,j)\in J_b}\left|h_{i,j}(0)-h_0\right|}{h_0},\qquad
\mathrm{initial\_h\_above\_jet\_rel}=\frac{\max_{(i,j)\in J_e}\left|h_{i,j}(0)-h_N\right|}{h_0}
$$
$$
\mathrm{initial\_u\_linf\_rel}=\frac{\max_{i,j}|u_{i,j}(0)-u_{ref,i,j}|}{U_s},\qquad
\mathrm{initial\_v\_linf\_rel}=\frac{\max_{i,j}|v_{i,j}(0)-v_{ref,i,j}|}{U_s}
$$
They are emitted as `errors.initial_h.linf_rel`, `errors.initial_h.below_jet_rel`, `errors.initial_h.above_jet_rel`, `errors.initial_u.linf_rel` and `errors.initial_v.linf_rel`. For the three unforced profiles the cross-channel velocity of the initial state is in addition judged absolutely, $\mathrm{initial\_vel\_cross\_linf}=\max_{i,j}|u_{r,i,j}(0)|$ (`m/s`; $u_r=v$ for `channel_axis=x`, $u$ for `channel_axis=y`), emitted as `errors.initial_vel_cross.linf`: its reference is exactly zero and the discrete initial state sets the cross-channel momentum to zero, so a conforming kernel reads `0`, and the relative error normalised by $U_s$ alone would admit $3.9\times10^{-8}\ \mathrm{m/s}$. With the constants of 2-1 and $L_r=8.0\times10^6\ \mathrm{m}$, $h_N=2436.2126\ \mathrm{m}$. `initial_h_below_jet_rel` pins the constant of integration of the `tc3_compact_jet` quadrature ($u_s=0$ on $[0,r]$ for every cell of $J_b$, so the integral is exactly zero and $h=h_0$); `initial_h_above_jet_rel` pins the complete integral, since every cell of $J_e$ carries it and $h_N$ is its closed form; the rule used inside the jet is not judged (2-4).

The steady-solution and manufactured-solution errors are defined in three norms over each state variable. With $q$ one of `h`, `hu`, `hv` at $t_{end}$, $q_{ref}$ its reference at $t_{end}$ (2-4: the initial state for `tc2_zonal_uniform` and `tc3_compact_jet`; the prescribed flow at $t_{end}$, $\tilde h$, $\tilde h\tilde u$, $\tilde h\tilde v$ evaluated from the closed forms of the Controlled Spec §6 with the case's inputs, for `tc4_translating_low`), the sums and the maximum over the interior cells, and
$$
\|e\|_1=\sum_{i,j}|e_{i,j}|,\qquad \|e\|_2=\Big(\sum_{i,j}e_{i,j}^2\Big)^{1/2},\qquad \|e\|_\infty=\max_{i,j}|e_{i,j}|,
$$
the error of $q$ in the norm $k\in\{1, 2, \infty\}$ is
$$
\frac{\|q-q_{ref}\|_k}{N_{q,k}}
$$
with the normaliser $N_{q,k}$: for `h` and for the along-channel momentum (`hu` on every case judged in three norms), $\|q_{ref}\|_k$; for the cross-channel momentum (`hv`), $\|hv_{ref}\|_k$ for `tc4_translating_low` and $\max(\|hu_{ref}\|_k, \|hv_{ref}\|_k)$ for `tc2_zonal_uniform` and `tc3_compact_jet` (their cross-channel reference is zero). They are emitted as `errors.steady_<v>.<k>_rel_tend` (`tc2_zonal_uniform`, `tc3_compact_jet`) and `errors.mms_<v>.<k>_rel_tend` (`tc4_translating_low`) for `<v>` in `h`, `hu`, `hv` and `<k>` in `l1`, `l2`, `linf`. In addition $\max_{i,j}|h_{i,j}(t_{end})-h_{ref,i,j}|$ (`m`) is emitted as `errors.steady_h.linf_tend` or `errors.mms_h.linf_tend`, and for `tc4_translating_low`
$$
\mathrm{mms\_vel\_l2\_rel}=\left(\frac{\sum_{i,j}(u_{i,j}-u_{ref,i,j})^2+(v_{i,j}-v_{ref,i,j})^2}{\sum_{i,j}u_{ref,i,j}^2+v_{ref,i,j}^2}\right)^{1/2}
$$
with $u$, $v$ at $t_{end}$ and $u_{ref}=\tilde u$, $v_{ref}=\tilde v$ at $t_{end}$, as `errors.mms_vel.l2_rel_tend`.

`run.n_step` is the state variable `n_step` after the run (§3); its judged value is the $\mathrm{n\_step}$ of the §3 procedure evaluated from the case's inputs, $\lceil (t_{end}-t_{start})/\mathrm{dt\_raw}\rceil$ with $\mathrm{dt\_raw}$ from the initial state.

The depth minimum `extrema.h.min` (`m`) is the minimum of $h$ over all interior cells and over the initial state and every committed state of the run. `cfl.max` is the maximum of the stability index of the Controlled Spec §5 over the initial state and every committed state of the run, with the case's `dt`.

The wall-normal velocity metric is the largest magnitude of the cross-channel velocity at $t_{end}$, $\max_{i,j}|(hv)_{i,j}/h_{i,j}|$ for `channel_axis=x` and $\max_{i,j}|(hu)_{i,j}/h_{i,j}|$ for `channel_axis=y` (`m/s`), emitted as `extrema.v_cross.max_abs`.

The translation-equivariance error of the `tc2_zonal_perturbed` pair is defined by the following, with $h_{base}$ the final state of the base case (`shift_x_fraction=0`) and $\mathrm{shift}(\cdot,+\Delta i)$ the cyclic shift by $\Delta i=\mathrm{shift\_x\_fraction}\cdot nx$ cells along `x`; it is emitted as `errors.symmetry_h.l2_rel` on the shifted case.
$$
\mathrm{symmetry\_h\_l2\_rel}=
\frac{\|h_{shifted}(t_{end})-\mathrm{shift}(h_{base}(t_{end}),+\Delta i)\|_2}{\|h_{base}(t_{end})\|_2}
$$

The decay ratio of the along-channel structure is defined by the following, with $\mathrm{shift}(\cdot, n_s/2)$ the cyclic shift by half the domain along the channel ($n_s$ the extent along the channel: `nx` for `channel_axis=x`, `ny` for `channel_axis=y`), which cancels the part of $h$ uniform along the channel exactly and doubles the odd along-channel harmonics, the wavenumber-one perturbation among them; it is emitted as `xstructure.decay_ratio` on every `tc2_zonal_perturbed` case.
$$
\mathrm{decay\_ratio}=\frac{\|h(t_{end})-\mathrm{shift}(h(t_{end}),n_s/2)\|_2}{\|h(0)-\mathrm{shift}(h(0),n_s/2)\|_2}
$$

The time-refinement difference is defined by the following, with $h_{base}$ the final state of `chan_tc4_ref_n032_dts100` and $h_{half}$ that of `chan_tc4_ref_n032_dts100_half`; it is emitted as `errors.time_pair.h_l2_rel` on the latter.
$$
\mathrm{time\_pair\_h\_l2\_rel}=\frac{\|h_{half}(t_{end})-h_{base}(t_{end})\|_2}{\|h_{base}(t_{end})\|_2}
$$

`convergence_order` is a cross-case reduction, using $p=\log(e_{coarse}/e_{fine})/\log(2)$, over the error of each norm and variable — `errors.steady_<v>.<k>_rel_tend` for `chan_tc2_ref` and `chan_tc3_ref` and `errors.mms_<v>.<k>_rel_tend` for `chan_tc4_ref` — between the cases `<family>_n032_dts100` and `<family>_n064_dts100` (the pair `n032_to_n064`) and between `<family>_n064_dts100` and `<family>_n128_dts100` (the pair `n064_to_n128`), within one family. It is emitted as a per-case field of the finer case of each pair: `convergence.<pair>.<k>_order` for `h` (`convergence.<pair>.l2_order` is the `l2` order of `h`) and `convergence.<pair>.hu_<k>_order`, `convergence.<pair>.hv_<k>_order` for the momentum components. The cases preceding the one that completes a reduction omit those fields, and so do the override cases of 4-3, which are not members of a refinement pair.

The restricted-reference errors of the perturbed family are defined by the following, with $q^{(n)}$ the final state of `chan_tc2_xpert_n<n>_dts100` for $n=32, 64, 128$, $q^{(256)}$ that of `chan_tc2_xpert_n256_dts100`, and $\mathrm{coarsen}(q^{(256)}, 256/n)$ the mean of $q^{(256)}$ over the $(256/n)\times(256/n)$ blocks of cells that cover each cell of the level $n$ (the cell $(i, j)$ of level $n$ is covered by the cells $((i-1)f+1..if,\ (j-1)f+1..jf)$ of level `256`, $f=256/n$):
$$
\mathrm{restricted}^{(n)}_{q,k}=\frac{\|q^{(n)}-\mathrm{coarsen}(q^{(256)},256/n)\|_k}{N^{(n)}_{q,k}}
$$
with $N^{(n)}_{q,k}$ the normaliser of the steady-solution errors applied to the restricted reference ($\|\mathrm{coarsen}(q^{(256)},f)\|_k$ for `h` and `hu`; $\max$ of that norm over the two restricted momentum components for `hv`). They are emitted on `chan_tc2_xpert_n256_dts100` as `errors.restricted.n<n>.<v>_<k>_rel`, and the orders $\log_2(\mathrm{restricted}^{(n)}/\mathrm{restricted}^{(2n)})$ as `convergence.restricted.<pair>.<v>_<k>_order`.

The symmetry-pair residuals are defined by the following, with $q'$ the final state of the transformed case and $q$ that of its base (4-3), every norm the `l2` norm over the interior cells, $N_h=\|h\|_2$ and $N_m=\max(\|hu\|_2,\|hv\|_2)$ the base's norms, $\cdot^T$ the exchange of the two array axes, $\mathrm{flip}_x$ the reversal of the first array axis, `x` (the cell $(i, j)$ taken from $(nx+1-i, j)$; the grammar's `flip(·, 0)`, whose axes count from `0`), and $\mathrm{flip}_y$ the reversal of the second array axis, `y` (the cell $(i, j)$ taken from $(i, ny+1-j)$; `flip(·, 1)`):
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

`cross_target_state_agreement` compares this target's numerical solution with every other target's certified variant of this node, case by case (`docs/TESTS.md` §Cross-target judgment). For each of the state variables `h`, `hu`, and `hv` at $t_{end}$ of a case, with $q$ that variable and $q^{ref}$ the same variable at $t_{end}$ of the same case in the other variant,
$$
\mathrm{cross\_target\_state\_agreement}=\frac{\max_{i,j} |q_{i,j}-q^{ref}_{i,j}|}{N_q}
$$
The normaliser of `h` is $N_h=\max(\max_{i,j}|h_{i,j}|,\ \max_{i,j}|h^{ref}_{i,j}|,\ 1)$. The two momentum components share one normaliser, the momentum scale of the two variants, $N_{hu}=N_{hv}=\max(\max_{i,j}|hu_{i,j}|,\ \max_{i,j}|hv_{i,j}|,\ \max_{i,j}|hu^{ref}_{i,j}|,\ \max_{i,j}|hv^{ref}_{i,j}|,\ 1)$: a component that is a small residue of the flow (the cross-stream `hv` of a zonal jet) carries the rounding of the whole momentum field, so its own maximum is not its rounding scale. It is evaluated by the host from the captured state, against each other variant separately and for each variable separately; it is not a `diagnostics.json` field, and on a target with no other certified variant it holds vacuously.

### 5-5. Default thresholds
The thresholds are calibrated against an independent reference implementation of the Controlled Spec §5 scheme (issue #265, phase-2 calibration comment; for the rectangular case of 2-10, issue #490, calibration comment; for the three norms, the restricted family, the symmetry pairs and the `tc4_translating_low` depth referred to the channel centre, issue #514, calibration comment). An error is judged by a two-sided band; the bands introduced in this version are $[\lfloor 0.5\,e\rfloor,\ \lceil 1.5\,e\rceil]$ of the reference value $e$, rounded outward to two significant digits, and the `l2` bands of `h` kept from the previous version for `tc2_zonal_uniform` and `tc3_compact_jet` are about 0.5 and 1.5 times the reference. An order is judged by a lower bound, the reference order minus `0.3` rounded down to one decimal (`0.4` where the reference order is below `0.7`), except the `l2` orders of `h` kept from the previous version. The `v_cross_max_abs` bounds are 1.49 to 1.73 times the reference values.
- $\mathrm{cfl.max} \le 1.0$, and `run.n_step` equals the §3 $\mathrm{n\_step}$ of the case (an integer; judged as $|\mathrm{run.n\_step}-\mathrm{n\_step}| \le 0.5$)
- `extrema.h.min` is $\ge 1.0e3$ for `tc2_zonal_uniform` and `tc2_zonal_perturbed`, $\ge 2.0e3$ for `tc3_compact_jet`, and $\ge 9.0e3$ for `tc4_translating_low` (`m`; the reference minimum over every `tc4_translating_low` case is `9193.9`)
- $\mathrm{mass\_drift\_rel} \le 1.0e{-10}$ ($\le 5.0e{-10}$ for the two five-day cases)
- `initial_h_linf_rel` $\le 1.0e{-9}$, `initial_h_below_jet_rel` $\le 1.0e{-9}$, `initial_h_above_jet_rel` $\le 1.0e{-9}$, `initial_u_linf_rel` $\le 1.0e{-9}$, `initial_v_linf_rel` $\le 1.0e{-9}$, `initial_vel_cross_linf` $\le 1.0e{-12}$ (`m/s`)
- The steady-solution and manufactured-solution errors are judged as two-sided bands. The Controlled Spec §5 fixes every term of the spatial discretization (`p1` `MC` reconstruction, Rusanov flux, mirror ghost, explicit Coriolis source), which dominates the error against the reference at $t_{end}$ (the time integration is judged by 2-8), so that error is a property of the discretization — its truncation error and numerical diffusion — and not a free quantity: a value below the band is not a better scheme, it is a run that did not perform the §5 update (a state left at its initial value has zero error against a steady solution and passes an upper bound alone). The lower bound and the upper bound of every band are two DISTINCT judged quantities of the test — each names its own `quantity` and each requires its own host-evaluated corroborant over the captured state — not two thresholds on one quantity: a corroborant that reads only the upper bound leaves the lower bound to the checks module's own report, and a state left at its initial value with a reported value inside the band would then pass. The bands are, per family, level, variable and norm (reference values in parentheses):

| case | variable | `l1` | `l2` | `linf` |
| --- | --- | --- | --- | --- |
| `chan_tc2_ref_n032_dts100` | `h` | $[1.1e{-3},\ 3.4e{-3}]$ (`2.205e-3`) | $[2.4e{-3},\ 7.1e{-3}]$ (`4.739e-3`) | $[8.6e{-3},\ 2.7e{-2}]$ (`1.736e-2`) |
| | `hu` | $[1.3e{-2},\ 4.0e{-2}]$ (`2.665e-2`) | $[2.5e{-2},\ 7.8e{-2}]$ (`5.163e-2`) | $[6.9e{-2},\ 2.1e{-1}]$ (`1.392e-1`) |
| | `hv` | $[1.4e{-3},\ 4.4e{-3}]$ (`2.928e-3`) | $[4.4e{-3},\ 1.4e{-2}]$ (`8.802e-3`) | $[1.3e{-2},\ 4.2e{-2}]$ (`2.799e-2`) |
| `chan_tc2_ref_n064_dts100` | `h` | $[2.3e{-4},\ 7.1e{-4}]$ (`4.668e-4`) | $[6.3e{-4},\ 1.9e{-3}]$ (`1.260e-3`) | $[3.1e{-3},\ 9.4e{-3}]$ (`6.227e-3`) |
| | `hu` | $[3.8e{-3},\ 1.2e{-2}]$ (`7.681e-3`) | $[1.0e{-2},\ 3.1e{-2}]$ (`2.057e-2`) | $[3.7e{-2},\ 1.2e{-1}]$ (`7.566e-2`) |
| | `hv` | $[4.3e{-4},\ 1.4e{-3}]$ (`8.737e-4`) | $[1.8e{-3},\ 5.5e{-3}]$ (`3.618e-3`) | $[8.8e{-3},\ 2.7e{-2}]$ (`1.778e-2`) |
| `chan_tc2_ref_n128_dts100` | `h` | $[4.3e{-5},\ 1.3e{-4}]$ (`8.651e-5`) | $[1.4e{-4},\ 4.3e{-4}]$ (`2.880e-4`) | $[9.5e{-4},\ 2.9e{-3}]$ (`1.915e-3`) |
| | `hu` | $[1.0e{-3},\ 3.2e{-3}]$ (`2.067e-3`) | $[3.7e{-3},\ 1.2e{-2}]$ (`7.520e-3`) | $[1.8e{-2},\ 5.6e{-2}]$ (`3.705e-2`) |
| | `hv` | $[1.1e{-4},\ 3.5e{-4}]$ (`2.298e-4`) | $[6.9e{-4},\ 2.1e{-3}]$ (`1.392e-3`) | $[5.0e{-3},\ 1.6e{-2}]$ (`1.005e-2`) |
| `chan_tc3_ref_n032_dts100` | `h` | $[1.6e{-3},\ 5.1e{-3}]$ (`3.395e-3`) | $[2.8e{-3},\ 8.5e{-3}]$ (`5.655e-3`) | $[6.7e{-3},\ 2.1e{-2}]$ (`1.360e-2`) |
| | `hu` | $[1.3e{-1},\ 4.2e{-1}]$ (`2.740e-1`) | $[1.1e{-1},\ 3.6e{-1}]$ (`2.367e-1`) | $[1.2e{-1},\ 3.7e{-1}]$ (`2.448e-1`) |
| | `hv` | $[1.5e{-3},\ 4.8e{-3}]$ (`3.187e-3`) | $[9.7e{-4},\ 3.0e{-3}]$ (`1.952e-3`) | $[7.7e{-4},\ 2.4e{-3}]$ (`1.540e-3`) |
| `chan_tc3_ref_n064_dts100` | `h` | $[3.0e{-4},\ 9.3e{-4}]$ (`6.200e-4`) | $[5.7e{-4},\ 1.7e{-3}]$ (`1.132e-3`) | $[1.4e{-3},\ 4.5e{-3}]$ (`2.964e-3`) |
| | `hu` | $[3.2e{-2},\ 9.7e{-2}]$ (`6.441e-2`) | $[2.9e{-2},\ 8.9e{-2}]$ (`5.897e-2`) | $[3.1e{-2},\ 9.5e{-2}]$ (`6.325e-2`) |
| | `hv` | $[5.6e{-4},\ 1.8e{-3}]$ (`1.137e-3`) | $[3.1e{-4},\ 9.6e{-4}]$ (`6.376e-4`) | $[2.1e{-4},\ 6.5e{-4}]$ (`4.318e-4`) |
| `chan_tc3_ref_n128_dts100` | `h` | $[5.6e{-5},\ 1.7e{-4}]$ (`1.122e-4`) | $[9.4e{-5},\ 2.8e{-4}]$ (`1.889e-4`) | $[2.2e{-4},\ 6.9e{-4}]$ (`4.559e-4`) |
| | `hu` | $[6.1e{-3},\ 1.9e{-2}]$ (`1.233e-2`) | $[5.4e{-3},\ 1.7e{-2}]$ (`1.089e-2`) | $[7.2e{-3},\ 2.2e{-2}]$ (`1.441e-2`) |
| | `hv` | $[1.3e{-4},\ 4.2e{-4}]$ (`2.773e-4`) | $[8.5e{-5},\ 2.6e{-4}]$ (`1.709e-4`) | $[6.5e{-5},\ 2.0e{-4}]$ (`1.306e-4`) |
| `chan_tc4_ref_n032_dts100` | `h` | $[5.1e{-4},\ 1.6e{-3}]$ (`1.028e-3`) | $[1.1e{-3},\ 3.5e{-3}]$ (`2.294e-3`) | $[8.8e{-3},\ 2.7e{-2}]$ (`1.767e-2`) |
| | `hu` | $[2.9e{-2},\ 8.9e{-2}]$ (`5.869e-2`) | $[5.1e{-2},\ 1.6e{-1}]$ (`1.033e-1`) | $[1.3e{-1},\ 4.1e{-1}]$ (`2.685e-1`) |
| | `hv` | $[2.9e{-1},\ 9.0e{-1}]$ (`5.938e-1`) | $[2.7e{-1},\ 8.3e{-1}]$ (`5.521e-1`) | $[3.2e{-1},\ 9.7e{-1}]$ (`6.407e-1`) |
| `chan_tc4_ref_n064_dts100` | `h` | $[1.1e{-4},\ 3.4e{-4}]$ (`2.264e-4`) | $[3.0e{-4},\ 9.2e{-4}]$ (`6.102e-4`) | $[2.6e{-3},\ 8.0e{-3}]$ (`5.292e-3`) |
| | `hu` | $[7.2e{-3},\ 2.2e{-2}]$ (`1.444e-2`) | $[1.4e{-2},\ 4.3e{-2}]$ (`2.857e-2`) | $[3.8e{-2},\ 1.2e{-1}]$ (`7.742e-2`) |
| | `hv` | $[7.8e{-2},\ 2.4e{-1}]$ (`1.569e-1`) | $[7.8e{-2},\ 2.4e{-1}]$ (`1.577e-1`) | $[1.0e{-1},\ 3.2e{-1}]$ (`2.130e-1`) |
| `chan_tc4_ref_n128_dts100` | `h` | $[1.7e{-5},\ 5.3e{-5}]$ (`3.486e-5`) | $[4.6e{-5},\ 1.4e{-4}]$ (`9.311e-5`) | $[4.3e{-4},\ 1.4e{-3}]$ (`8.712e-4`) |
| | `hu` | $[1.2e{-3},\ 3.7e{-3}]$ (`2.401e-3`) | $[2.3e{-3},\ 7.2e{-3}]$ (`4.756e-3`) | $[6.1e{-3},\ 1.9e{-2}]$ (`1.240e-2`) |
| | `hv` | $[1.1e{-2},\ 3.6e{-2}]$ (`2.386e-2`) | $[1.2e{-2},\ 3.7e{-2}]$ (`2.409e-2`) | $[1.6e{-2},\ 5.1e{-2}]$ (`3.393e-2`) |
| `chan_tc4_ref_n064_dts100_rect` | `h` | $[2.2e{-4},\ 6.9e{-4}]$ (`4.586e-4`) | $[4.7e{-4},\ 1.5e{-3}]$ (`9.591e-4`) | $[3.4e{-3},\ 1.1e{-2}]$ (`6.876e-3`) |
| | `hu` | $[1.7e{-2},\ 5.3e{-2}]$ (`3.502e-2`) | $[2.7e{-2},\ 8.2e{-2}]$ (`5.424e-2`) | $[6.1e{-2},\ 1.9e{-1}]$ (`1.239e-1`) |
| | `hv` | $[1.0e{-1},\ 3.2e{-1}]$ (`2.105e-1`) | $[9.2e{-2},\ 2.8e{-1}]$ (`1.853e-1`) | $[1.1e{-1},\ 3.5e{-1}]$ (`2.290e-1`) |

  The five-day cases and the halved-`dt` case are judged in the `l2` norm of `h`: $[7.2e{-4},\ 2.1e{-3}]$ for `chan_tc2_ref_n128_dts100_tend5d` (reference `1.430e-3`), $[1.2e{-4},\ 3.9e{-4}]$ for `chan_tc4_ref_n128_dts100_tend5d` (reference `2.550e-4`), and the `nx=32` band $[1.1e{-3},\ 3.5e{-3}]$ for `chan_tc4_ref_n032_dts100_half` (reference `2.294e-3`, equal to its base's to five digits). The first-order `p0` reconstruction gives `5.2e-2` in the `l2` norm of `h` at `chan_tc2_ref` `nx=32` and fails the upper bound.
- `convergence_order` requires the following lower bounds (reference orders in parentheses; the reasons for the orders below `1.5` are stated in 2-12):

| family, pair | variable | `l1` | `l2` | `linf` |
| --- | --- | --- | --- | --- |
| `chan_tc2_ref`, `n032_to_n064` | `h` | $\ge 1.9$ (`2.24`) | $\ge 1.6$ (`1.91`) | $\ge 1.1$ (`1.48`) |
| | `hu` | $\ge 1.4$ (`1.79`) | $\ge 1.0$ (`1.33`) | $\ge 0.5$ (`0.88`) |
| | `hv` | $\ge 1.4$ (`1.74`) | $\ge 0.9$ (`1.28`) | $\ge 0.4$ (`0.65`) |
| `chan_tc2_ref`, `n064_to_n128` | `h` | $\ge 2.1$ (`2.43`) | $\ge 1.6$ (`2.13`) | $\ge 1.4$ (`1.70`) |
| | `hu` | $\ge 1.5$ (`1.89`) | $\ge 1.1$ (`1.45`) | $\ge 0.7$ (`1.03`) |
| | `hv` | $\ge 1.6$ (`1.93`) | $\ge 1.0$ (`1.38`) | $\ge 0.5$ (`0.82`) |
| `chan_tc3_ref`, `n032_to_n064` | `h` | $\ge 2.1$ (`2.45`) | $\ge 1.8$ (`2.32`) | $\ge 1.8$ (`2.20`) |
| | `hu` | $\ge 1.7$ (`2.09`) | $\ge 1.7$ (`2.00`) | $\ge 1.6$ (`1.95`) |
| | `hv` | $\ge 1.1$ (`1.49`) | $\ge 1.3$ (`1.61`) | $\ge 1.5$ (`1.84`) |
| `chan_tc3_ref`, `n064_to_n128` | `h` | $\ge 2.1$ (`2.47`) | $\ge 1.8$ (`2.58`) | $\ge 2.4$ (`2.70`) |
| | `hu` | $\ge 2.0$ (`2.38`) | $\ge 2.1$ (`2.44`) | $\ge 1.8$ (`2.13`) |
| | `hv` | $\ge 1.7$ (`2.04`) | $\ge 1.5$ (`1.90`) | $\ge 1.4$ (`1.73`) |
| `chan_tc4_ref`, `n032_to_n064` | `h` | $\ge 1.8$ (`2.18`) | $\ge 1.6$ (`1.91`) | $\ge 1.4$ (`1.74`) |
| | `hu` | $\ge 1.7$ (`2.02`) | $\ge 1.5$ (`1.85`) | $\ge 1.4$ (`1.79`) |
| | `hv` | $\ge 1.6$ (`1.92`) | $\ge 1.5$ (`1.81`) | $\ge 1.2$ (`1.59`) |
| `chan_tc4_ref`, `n064_to_n128` | `h` | $\ge 2.3$ (`2.70`) | $\ge 2.4$ (`2.71`) | $\ge 2.3$ (`2.60`) |
| | `hu` | $\ge 2.2$ (`2.59`) | $\ge 2.2$ (`2.59`) | $\ge 2.3$ (`2.64`) |
| | `hv` | $\ge 2.4$ (`2.72`) | $\ge 2.4$ (`2.71`) | $\ge 2.3$ (`2.65`) |

- The restricted-reference errors of the perturbed family (2-13), carried by `chan_tc2_xpert_n256_dts100`, are judged as two-sided bands on the same ground and as two distinct judged quantities each, and their orders by lower bounds:

| level | variable | `l1` | `l2` | `linf` |
| --- | --- | --- | --- | --- |
| `n032` | `h` | $[1.1e{-3},\ 3.4e{-3}]$ (`2.231e-3`) | $[2.4e{-3},\ 7.3e{-3}]$ (`4.838e-3`) | $[1.0e{-2},\ 3.3e{-2}]$ (`2.171e-2`) |
| | `hu` | $[1.3e{-2},\ 4.0e{-2}]$ (`2.643e-2`) | $[2.5e{-2},\ 7.6e{-2}]$ (`5.059e-2`) | $[7.1e{-2},\ 2.2e{-1}]$ (`1.426e-1`) |
| | `hv` | $[1.8e{-3},\ 5.5e{-3}]$ (`3.604e-3`) | $[4.4e{-3},\ 1.4e{-2}]$ (`8.829e-3`) | $[1.4e{-2},\ 4.5e{-2}]$ (`2.944e-2`) |
| `n064` | `h` | $[2.3e{-4},\ 7.1e{-4}]$ (`4.728e-4`) | $[6.3e{-4},\ 2.0e{-3}]$ (`1.280e-3`) | $[3.8e{-3},\ 1.2e{-2}]$ (`7.723e-3`) |
| | `hu` | $[3.6e{-3},\ 1.1e{-2}]$ (`7.321e-3`) | $[9.4e{-3},\ 2.9e{-2}]$ (`1.899e-2`) | $[3.6e{-2},\ 1.1e{-1}]$ (`7.235e-2`) |
| | `hv` | $[5.4e{-4},\ 1.7e{-3}]$ (`1.090e-3`) | $[1.7e{-3},\ 5.2e{-3}]$ (`3.443e-3`) | $[8.8e{-3},\ 2.7e{-2}]$ (`1.762e-2`) |
| `n128` | `h` | $[4.2e{-5},\ 1.3e{-4}]$ (`8.434e-5`) | $[1.2e{-4},\ 3.9e{-4}]$ (`2.545e-4`) | $[9.6e{-4},\ 2.9e{-3}]$ (`1.924e-3`) |
| | `hu` | $[8.2e{-4},\ 2.5e{-3}]$ (`1.645e-3`) | $[2.6e{-3},\ 8.1e{-3}]$ (`5.354e-3`) | $[1.2e{-2},\ 3.9e{-2}]$ (`2.539e-2`) |
| | `hv` | $[1.3e{-4},\ 4.0e{-4}]$ (`2.603e-4`) | $[5.2e{-4},\ 1.6e{-3}]$ (`1.050e-3`) | $[3.9e{-3},\ 1.2e{-2}]$ (`7.832e-3`) |
| `n032_to_n064` order | `h` | $\ge 1.9$ (`2.24`) | $\ge 1.6$ (`1.92`) | $\ge 1.1$ (`1.49`) |
| | `hu` | $\ge 1.5$ (`1.85`) | $\ge 1.1$ (`1.41`) | $\ge 0.6$ (`0.98`) |
| | `hv` | $\ge 1.4$ (`1.73`) | $\ge 1.0$ (`1.36`) | $\ge 0.4$ (`0.74`) |
| `n064_to_n128` order | `h` | $\ge 2.1$ (`2.49`) | $\ge 2.0$ (`2.33`) | $\ge 1.7$ (`2.01`) |
| | `hu` | $\ge 1.8$ (`2.15`) | $\ge 1.5$ (`1.83`) | $\ge 1.2$ (`1.51`) |
| | `hv` | $\ge 1.7$ (`2.07`) | $\ge 1.4$ (`1.71`) | $\ge 0.8$ (`1.17`) |

- `v_cross_max_abs` (`m/s`) for `chan_tc2_ref` is $\le 4.0$ for `nx=32`, $\le 2.6$ for `nx=64`, and $\le 1.5$ for `nx=128`; for `chan_tc3_ref` it is $\le 0.090$, $\le 0.025$, and $\le 0.0080$; for the five-day case `chan_tc2_ref_n128_dts100_tend5d` it is $\le 1.2$
- `time_pair_h_l2_rel` is judged as a two-sided band $[2.18e{-8},\ 2.81e{-8}]$ on `chan_tc4_ref_n032_dts100_half` (reference `2.4847e-8`; 0.88 and 1.13 times it, rounded outward at three digits — narrower than the other bands, because the value moves only by round-off under a conforming implementation, 2-8), as two distinct judged quantities, each with its own host-evaluated corroborant.
- `symmetry_h_l2_rel` $\le 2.0e{-11}$ (the shift is a whole number of cells and every operation of the scheme is translation-covariant, so the residual is round-off)
- Every symmetry-pair residual `errors.axis_exchange.*`, `errors.mirror_x.*` and `errors.mirror_y.*` is $\le 2.0e{-11}$ for each of `h`, `hu` and `hv` (2-11; the reference implementation reads at most `5.2e-15`)
- `decay_ratio` is judged as a two-sided band $[0.38,\ 0.95]$ at `nx=64`, `128` and `256` (references `0.7666`, `0.7738`, `0.7762`) and $[0.37,\ 0.95]$ at `nx=32` (reference `0.7418`); the lower bound is about 0.5 times the reference, and the upper bound is set below the ratio `1.000` of an update without the along-channel interface flux (2-6). The lower and the upper bound are two distinct judged quantities, each with its own host-evaluated corroborant.
- $\mathrm{cross\_target\_state\_agreement} \le 1.0e{-10}$ for each of `h`, `hu`, and `hv`

## 6. Test definitions
In every test, a judgment this section does not name as applied is not applied, with the basis stated beside it; the judgments are those of 5-5: initial state, `CFL` (with `run.n_step`), depth positivity, mass conservation, theoretical comparison (the steady-solution errors), manufactured solution (the errors against the prescribed flow of `tc4_translating_low`), restricted reference, wall-normal velocity, translation equivariance, along-channel structure (`decay_ratio`), time refinement, symmetry pair, and cross-target.

### 6-1. `l0_tc2_initial_state_matches_analytic`
- `level`: `L0`
- `objective`: confirm that the discrete initial state of `tc2_zonal_uniform` is the closed form of 2-3 at the cell centres.
- target cases:
  - `chan_tc2_ref_n064_dts100`
- `expected_outcome`: `pass`
- judgment conditions:
  - The initial-state judgment is applied. The evaluation expressions are `errors.initial_h.linf_rel`, `errors.initial_u.linf_rel` and `errors.initial_v.linf_rel`, each with the threshold $\le 1.0e{-9}$, and `errors.initial_vel_cross.linf` with the threshold $\le 1.0e{-12}$ (`m/s`).
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of the case ($|\mathrm{run.n\_step}-\mathrm{n\_step}| \le 0.5$).
  - The depth-positivity judgment is applied. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 1.0e3$.
  - The mass-conservation judgment is not applied. The non-application basis is "because the test judges the initial state and the conservation judgment belongs to 6-3".
  - The theoretical-comparison judgment is not applied. The non-application basis is "because the test judges the initial state and the steady-solution judgment belongs to 6-3".
  - The wall-normal-velocity judgment at $t_{end}$ is not applied. The non-application basis is "because the test judges the initial state; the initial cross-channel velocity is judged by `errors.initial_vel_cross.linf` above".
  - The translation-equivariance, along-channel-structure, restricted-reference, time-refinement and symmetry-pair judgments are not applied. The non-application basis is "because the case is not a member of a pair or of the perturbed family".
  - The manufactured-solution judgment is not applied. The non-application basis is "because `errors.mms_*` are `N/A` for a profile other than `tc4_translating_low` (5-3)".
  - The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, and `hv`, and the threshold is $\le 1.0e{-10}$ for each.

### 6-2. `l0_tc3_initial_jet_matches_analytic`
- `level`: `L0`
- `objective`: confirm that the discrete initial state of `tc3_compact_jet` is the closed form of 2-3 at the cell centres: the velocity, the depth's constant of integration, and the depth beyond the jet, which is the quadrature's closed-form value.
- target cases:
  - `chan_tc3_ref_n064_dts100`
- `expected_outcome`: `pass`
- judgment conditions:
  - The initial-state judgment is applied. The evaluation expressions are `errors.initial_u.linf_rel`, `errors.initial_v.linf_rel`, `errors.initial_h.below_jet_rel` and `errors.initial_h.above_jet_rel`, each with the threshold $\le 1.0e{-9}$, and `errors.initial_vel_cross.linf` with the threshold $\le 1.0e{-12}$ (`m/s`). `errors.initial_h.linf_rel` is not judged: it is `N/A` for this profile (5-3).
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of the case ($|\mathrm{run.n\_step}-\mathrm{n\_step}| \le 0.5$).
  - The depth-positivity judgment is applied. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 2.0e3$.
  - The mass-conservation judgment is not applied. The non-application basis is "because the test judges the initial state and the conservation judgment belongs to 6-4".
  - The theoretical-comparison judgment is not applied. The non-application basis is "because the test judges the initial state and the steady-solution judgment belongs to 6-4".
  - The wall-normal-velocity judgment at $t_{end}$ is not applied. The non-application basis is "because the test judges the initial state; the initial cross-channel velocity is judged by `errors.initial_vel_cross.linf` above".
  - The translation-equivariance, along-channel-structure, restricted-reference, time-refinement and symmetry-pair judgments are not applied. The non-application basis is "because the case is not a member of a pair or of the perturbed family".
  - The manufactured-solution judgment is not applied. The non-application basis is "because `errors.mms_*` are `N/A` for a profile other than `tc4_translating_low` (5-3)".
  - The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, and `hv`, and the threshold is $\le 1.0e{-10}$ for each.

### 6-3. `l1_tc2_refinement_steady_geostrophic`
- `level`: `L1`
- `objective`: confirm, for `tc2_zonal_uniform`, the steady-solution error of `h`, `hu` and `hv` in three norms and its decrease with refinement, mass conservation, positivity, and the wall-normal velocity.
- target cases:
  - `chan_tc2_ref_n032_dts100`
  - `chan_tc2_ref_n064_dts100`
  - `chan_tc2_ref_n128_dts100`
- `expected_outcome`: `pass`
- judgment conditions:
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of the case ($|\mathrm{run.n\_step}-\mathrm{n\_step}| \le 0.5$).
  - The depth-positivity judgment is applied. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 1.0e3$.
  - The mass-conservation judgment is applied. The evaluation expression is `metrics.mass_drift_rel`, and the threshold is $\le 1.0e{-10}$.
  - The theoretical-comparison judgment is applied. Each of the nine `errors.steady_<v>.<k>_rel_tend` of each case applies its band of the 5-5 table (rows `chan_tc2_ref_n032_dts100`, `_n064_dts100`, `_n128_dts100`) as two judged quantities, and each of the nine `convergence.n032_to_n064.*_order` fields (carried by `chan_tc2_ref_n064_dts100`) and the nine `convergence.n064_to_n128.*_order` fields (carried by `chan_tc2_ref_n128_dts100`) applies its lower bound of the 5-5 order table (rows `chan_tc2_ref`).
  - The wall-normal-velocity judgment is applied. The evaluation expression is `extrema.v_cross.max_abs`, and the per-case threshold is $\le 4.0$, $\le 2.6$, $\le 1.5$.
  - The initial-state judgment is not applied. The non-application basis is "because it belongs to 6-1".
  - The translation-equivariance, along-channel-structure, restricted-reference, time-refinement and symmetry-pair judgments are not applied. The non-application basis is "because the cases are not members of a pair or of the perturbed family; the pairs whose base is `chan_tc2_ref_n064_dts100` are judged in 6-14 to 6-16".
  - The manufactured-solution judgment is not applied. The non-application basis is "because `errors.mms_*` are `N/A` for a profile other than `tc4_translating_low` (5-3)".
  - The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, and `hv`, and the threshold is $\le 1.0e{-10}$ for each.

### 6-4. `l1_tc3_refinement_compact_jet`
- `level`: `L1`
- `objective`: confirm, for `tc3_compact_jet`, the steady-solution error of `h`, `hu` and `hv` in three norms and its decrease with refinement, mass conservation, positivity, and the wall-normal velocity.
- target cases:
  - `chan_tc3_ref_n032_dts100`
  - `chan_tc3_ref_n064_dts100`
  - `chan_tc3_ref_n128_dts100`
- `expected_outcome`: `pass`
- judgment conditions:
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of the case ($|\mathrm{run.n\_step}-\mathrm{n\_step}| \le 0.5$).
  - The depth-positivity judgment is applied. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 2.0e3$.
  - The mass-conservation judgment is applied. The evaluation expression is `metrics.mass_drift_rel`, and the threshold is $\le 1.0e{-10}$.
  - The theoretical-comparison judgment is applied. Each of the nine `errors.steady_<v>.<k>_rel_tend` of each case applies its band of the 5-5 table (rows `chan_tc3_ref_n032_dts100`, `_n064_dts100`, `_n128_dts100`) as two judged quantities, and each of the nine `convergence.n032_to_n064.*_order` fields (carried by `chan_tc3_ref_n064_dts100`) and the nine `convergence.n064_to_n128.*_order` fields (carried by `chan_tc3_ref_n128_dts100`) applies its lower bound of the 5-5 order table (rows `chan_tc3_ref`).
  - The wall-normal-velocity judgment is applied. The evaluation expression is `extrema.v_cross.max_abs`, and the per-case threshold is $\le 0.090$, $\le 0.025$, $\le 0.0080$.
  - The initial-state judgment is not applied. The non-application basis is "because it belongs to 6-2".
  - The translation-equivariance, along-channel-structure, restricted-reference, time-refinement and symmetry-pair judgments are not applied. The non-application basis is "because the cases are not members of a pair or of the perturbed family; the pairs whose base is `chan_tc3_ref_n064_dts100` are judged in 6-14 to 6-16".
  - The manufactured-solution judgment is not applied. The non-application basis is "because `errors.mms_*` are `N/A` for a profile other than `tc4_translating_low` (5-3)".
  - The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, and `hv`, and the threshold is $\le 1.0e{-10}$ for each.

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
  - The theoretical-comparison judgment is applied. The evaluation expression is `errors.steady_h.l2_rel_tend`, and the band is $[7.2e{-4},\ 2.1e{-3}]$ as two judged quantities. The other steady-solution fields of the case are reported, and `convergence_order` is not judged: the case is not a member of a refinement pair.
  - The wall-normal-velocity judgment is applied. The evaluation expression is `extrema.v_cross.max_abs`, and the threshold is $\le 1.2$.
  - The initial-state judgment is not applied. The non-application basis is "because it belongs to 6-1".
  - The translation-equivariance, along-channel-structure, restricted-reference, time-refinement and symmetry-pair judgments are not applied. The non-application basis is "because the case is not a member of a pair or of the perturbed family".
  - The manufactured-solution judgment is not applied. The non-application basis is "because `errors.mms_*` are `N/A` for a profile other than `tc4_translating_low` (5-3)".
  - The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, and `hv`, and the threshold is $\le 1.0e{-10}$ for each.

### 6-6. `l3_x_translation_equivariance_and_wave_decay`
- `level`: `L3`
- `objective`: confirm that the along-channel dynamics are those of the Controlled Spec §5 for `tc2_zonal_perturbed`: the initial state, the translation equivariance of the pair, the decay of the perturbation's along-channel structure, mass conservation and positivity.
- pair cases:
  - `reference` is `chan_tc2_xpert_n064_dts100`
  - `shifted` is `chan_tc2_xpert_n064_dts100_sx025`
- `expected_outcome`: `pass`
- judgment conditions:
  - The initial-state judgment is applied to both cases. The evaluation expressions are `errors.initial_h.linf_rel` (against the closed form of 2-3 at the cell centres, shift included), `errors.initial_u.linf_rel` and `errors.initial_v.linf_rel`, each with the threshold $\le 1.0e{-9}$, and `errors.initial_vel_cross.linf` with the threshold $\le 1.0e{-12}$ (`m/s`).
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of the case ($|\mathrm{run.n\_step}-\mathrm{n\_step}| \le 0.5$).
  - The depth-positivity judgment is applied. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 1.0e3$.
  - The mass-conservation judgment is applied. The evaluation expression is `metrics.mass_drift_rel`, and the threshold is $\le 1.0e{-10}$.
  - The translation-equivariance judgment is applied. The evaluation expression is `errors.symmetry_h.l2_rel` (carried by the shifted case), and the threshold is $\le 2.0e{-11}$.
  - The along-channel-structure judgment is applied to both cases. The evaluation expression is `xstructure.decay_ratio`, and the band is $[0.38,\ 0.95]$ as two judged quantities.
  - The theoretical-comparison judgment is not applied. The non-application basis is "because `tc2_zonal_perturbed` has no steady reference (2-4); the family's restricted-reference judgment belongs to 6-13".
  - The wall-normal-velocity judgment at $t_{end}$ is not applied. The non-application basis is "because the perturbation excites the cross-channel velocity by design; the wall-normal velocity of the steady profiles is judged in 6-3 to 6-5".
  - The manufactured-solution, restricted-reference, time-refinement and symmetry-pair judgments are not applied. The non-application basis is "because `errors.mms_*` are `N/A` for this profile (5-3), the restricted reference belongs to 6-13, and the transformed cases of these two cases are judged in 6-14 to 6-16".
  - The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, and `hv`, and the threshold is $\le 1.0e{-10}$ for each.

### 6-7. `l0_cfl_guard_xfail`
- `level`: `L0`
- `objective`: confirm that a `CFL`-violation case can be detected (expected failure).
- target cases:
  - `chan_guard_n032_dts240`
- `expected_outcome`: `xfail`
- `xfail_condition`: `cfl.max > 1.0`
- `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'cfl'`
- judgment conditions:
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$. `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of the case ($|\mathrm{run.n\_step}-\mathrm{n\_step}| \le 0.5$), and its real value and status are reported: the case performs all of its steps (2-9), and a run whose step loop stops on the wet-domain error of the Controlled Spec §5 reports the count performed and a `fail` (2-9). The `run.n_step` judgment is non-gating for this test: its status is not a condition of `pass_when`, so a `fail` on it does not change this test's outcome, which the `CFL` judgment decides.
  - The depth-positivity judgment is applied and its real `extrema.h.min` value and status are reported against the threshold $\ge 1.0e3$ (it is never `N/A`; the 5-3 rule does not apply to it). It is non-gating for this test: its status is not a condition of `pass_when`, so a `fail` on it does not change this test's outcome.
  - The mass-conservation judgment is not applied. The non-application basis is "because the purpose of the guard test is only the detection of a stability-condition violation".
  - The theoretical-comparison judgment is not applied. The non-application basis is "because the purpose of the guard test is only the detection of a stability-condition violation; the state of the guard case at $t_{end}$ is not judged against the steady solution".
  - The wall-normal-velocity judgment is not applied. The non-application basis is "because the purpose of the guard test is only the detection of a stability-condition violation".
  - The initial-state judgment is not applied. The non-application basis is "because it belongs to 6-1".
  - The translation-equivariance, along-channel-structure, restricted-reference, time-refinement and symmetry-pair judgments are not applied. The non-application basis is "because the case is not a member of a pair or of the perturbed family".
  - The manufactured-solution judgment is not applied. The non-application basis is "because `errors.mms_*` are `N/A` for a profile other than `tc4_translating_low` (5-3)".
  - The cross-target judgment is not applied. The non-application basis is "because the state an unstable case leaves at $t_{end}$ is not a result two variants must agree on".

### 6-8. `l0_tc4_initial_state_matches_analytic`
- `level`: `L0`
- `objective`: confirm that the discrete initial state of `tc4_translating_low` is the prescribed flow of 2-3 at $t=0$ at the cell centres: the depth of the background referred to the channel centre and the low, and both velocity components.
- target cases:
  - `chan_tc4_ref_n064_dts100`
- `expected_outcome`: `pass`
- judgment conditions:
  - The initial-state judgment is applied. The evaluation expressions are `errors.initial_h.linf_rel`, `errors.initial_u.linf_rel` and `errors.initial_v.linf_rel`, each with the threshold $\le 1.0e{-9}$.
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of the case ($|\mathrm{run.n\_step}-\mathrm{n\_step}| \le 0.5$).
  - The depth-positivity judgment is applied. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 9.0e3$.
  - The mass-conservation judgment is not applied. The non-application basis is "because the test judges the initial state and the conservation judgment belongs to 6-9".
  - The manufactured-solution judgment is not applied. The non-application basis is "because the test judges the initial state and the manufactured-solution judgment belongs to 6-9".
  - The theoretical-comparison judgment against a steady solution is not applied. The non-application basis is "because `tc4_translating_low` is not steady (2-4)".
  - The wall-normal-velocity judgment is not applied. The non-application basis is "because the low's cross-channel velocity is non-zero by design (2-5)".
  - The translation-equivariance, along-channel-structure, restricted-reference, time-refinement and symmetry-pair judgments are not applied. The non-application basis is "because the case is judged here as a single case; its pairs are judged in 6-14 to 6-16".
  - The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, and `hv`, and the threshold is $\le 1.0e{-10}$ for each.

### 6-9. `l1_tc4_refinement_translating_low`
- `level`: `L1`
- `objective`: confirm, for `tc4_translating_low`, the error of `h`, `hu` and `hv` against the prescribed flow at $t_{end}$ in three norms and its second-order decrease with refinement, mass conservation and positivity; the forcing of the Controlled Spec §5 step 6 is exercised, and a run without it or with it frozen at $t=0$ fails the upper bound of `errors.mms_h.l2_rel_tend` at `nx=64` and `nx=128` (2-7; at `nx=32` both lie inside the band, `3.117e-3` and `2.722e-3`).
- target cases:
  - `chan_tc4_ref_n032_dts100`
  - `chan_tc4_ref_n064_dts100`
  - `chan_tc4_ref_n128_dts100`
- `expected_outcome`: `pass`
- judgment conditions:
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of the case ($|\mathrm{run.n\_step}-\mathrm{n\_step}| \le 0.5$).
  - The depth-positivity judgment is applied. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 9.0e3$.
  - The mass-conservation judgment is applied. The evaluation expression is `metrics.mass_drift_rel`, and the threshold is $\le 1.0e{-10}$.
  - The manufactured-solution judgment is applied. Each of the nine `errors.mms_<v>.<k>_rel_tend` of each case applies its band of the 5-5 table (rows `chan_tc4_ref_n032_dts100`, `_n064_dts100`, `_n128_dts100`) as two judged quantities, and each of the nine `convergence.n032_to_n064.*_order` fields (carried by `chan_tc4_ref_n064_dts100`) and the nine `convergence.n064_to_n128.*_order` fields (carried by `chan_tc4_ref_n128_dts100`) applies its lower bound of the 5-5 order table (rows `chan_tc4_ref`). `errors.mms_h.linf_tend` and `errors.mms_vel.l2_rel_tend` are reported (5-3).
  - The theoretical-comparison judgment against a steady solution is not applied. The non-application basis is "because `tc4_translating_low` is not steady (2-4)".
  - The wall-normal-velocity judgment is not applied. The non-application basis is "because the low's cross-channel velocity is non-zero by design (2-5)".
  - The initial-state judgment is not applied. The non-application basis is "because it belongs to 6-8".
  - The translation-equivariance, along-channel-structure, restricted-reference, time-refinement and symmetry-pair judgments are not applied. The non-application basis is "because the cases are judged here as refinement levels; the time pair is judged in 6-11 and the pairs whose base is `chan_tc4_ref_n064_dts100` in 6-15 and 6-16".
  - The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, and `hv`, and the threshold is $\le 1.0e{-10}$ for each.

### 6-10. `l2_tc4_long_run`
- `level`: `L2`
- `objective`: confirm, for `tc4_translating_low` at `nx=128` over five days, bounded error growth against the prescribed flow, mass conservation and positivity.
- target cases:
  - `chan_tc4_ref_n128_dts100_tend5d`
- `expected_outcome`: `pass`
- judgment conditions:
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of the case ($|\mathrm{run.n\_step}-\mathrm{n\_step}| \le 0.5$).
  - The depth-positivity judgment is applied. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 9.0e3$.
  - The mass-conservation judgment is applied. The evaluation expression is `metrics.mass_drift_rel`, and the threshold is $\le 5.0e{-10}$.
  - The manufactured-solution judgment is applied. The evaluation expression is `errors.mms_h.l2_rel_tend`, and the band is $[1.2e{-4},\ 3.9e{-4}]$ as two judged quantities. The other manufactured-solution fields of the case are reported, and `convergence_order` is not judged: the case is not a member of a refinement pair.
  - The theoretical-comparison judgment against a steady solution is not applied. The non-application basis is "because `tc4_translating_low` is not steady (2-4)".
  - The wall-normal-velocity judgment is not applied. The non-application basis is "because the low's cross-channel velocity is non-zero by design (2-5)".
  - The initial-state judgment is not applied. The non-application basis is "because it belongs to 6-8".
  - The translation-equivariance, along-channel-structure, restricted-reference, time-refinement and symmetry-pair judgments are not applied. The non-application basis is "because the case is not a member of a pair or of the perturbed family".
  - The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, and `hv`, and the threshold is $\le 1.0e{-10}$ for each.

### 6-11. `l3_tc4_time_refinement_pair`
- `level`: `L3`
- `objective`: exclude, through the pair of 2-8, the substitutions of the Controlled Spec §5 that 2-8 and 2-7 list as outside the pair band: the second- and third-order Runge–Kutta methods, the three-eighths-rule fourth-order method, the forcing at the step's start, end or midpoint time for all four stages, the conservation-form products of the forcing taken with the stage state, `dubar` differenced, the `ubar` array taken from the stage state, a reconstruction of the primitive variables, and the `cfl_target` values 2-8 lists as outside the band. The substitutions 2-7 and 2-8 state the pair does not exclude are not judged here.
- pair cases:
  - `reference` is `chan_tc4_ref_n032_dts100`
  - `halved` is `chan_tc4_ref_n032_dts100_half`
- `expected_outcome`: `pass`
- judgment conditions:
  - The time-refinement judgment is applied. The evaluation expression is `errors.time_pair.h_l2_rel` (carried by the halved case), and the band is $[2.18e{-8},\ 2.81e{-8}]$ as two judged quantities.
  - The `CFL` judgment is applied to both cases. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of each case ($|\mathrm{run.n\_step}-\mathrm{n\_step}| \le 0.5$).
  - The depth-positivity judgment is applied to both cases. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 9.0e3$.
  - The mass-conservation judgment is applied to both cases. The evaluation expression is `metrics.mass_drift_rel`, and the threshold is $\le 1.0e{-10}$.
  - The manufactured-solution judgment is applied to the halved case. The evaluation expression is `errors.mms_h.l2_rel_tend`, and the band is the `nx=32` band $[1.1e{-3},\ 3.5e{-3}]$ of 5-5 as two judged quantities (the halved case's reference value is `2.294e-3`, equal to its base's to five digits); the base case's nine errors and its bands belong to 6-9. `convergence_order` is not judged: the halved case is not a member of a refinement pair.
  - The theoretical-comparison judgment against a steady solution is not applied. The non-application basis is "because `tc4_translating_low` is not steady (2-4)".
  - The wall-normal-velocity judgment is not applied. The non-application basis is "because the low's cross-channel velocity is non-zero by design (2-5)".
  - The initial-state judgment is not applied. The non-application basis is "because it belongs to 6-8".
  - The translation-equivariance, along-channel-structure, restricted-reference and symmetry-pair judgments are not applied. The non-application basis is "because the cases are not members of a translation or symmetry pair or of the perturbed family".
  - The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, and `hv`, and the threshold is $\le 1.0e{-10}$ for each.

### 6-12. `l1_tc4_rectangular_grid`
- `level`: `L1`
- `objective`: confirm, for `tc4_translating_low` on the rectangular grid of 2-10, the initial state at the cell centres, the error of `h`, `hu` and `hv` against the prescribed flow at $t_{end}$ in three norms, mass conservation and positivity; each exchange of the two extents, the two domain lengths or the two spacings measured in 2-10 fails a judgment of this test.
- target cases:
  - `chan_tc4_ref_n064_dts100_rect`
- `expected_outcome`: `pass`
- judgment conditions:
  - The initial-state judgment is applied. The evaluation expressions are `errors.initial_h.linf_rel`, `errors.initial_u.linf_rel` and `errors.initial_v.linf_rel`, each with the threshold $\le 1.0e{-9}$.
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of the case ($|\mathrm{run.n\_step}-\mathrm{n\_step}| \le 0.5$).
  - The depth-positivity judgment is applied. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 9.0e3$.
  - The mass-conservation judgment is applied. The evaluation expression is `metrics.mass_drift_rel`, and the threshold is $\le 1.0e{-10}$.
  - The manufactured-solution judgment is applied. Each of the nine `errors.mms_<v>.<k>_rel_tend` applies its band of the 5-5 table (rows `chan_tc4_ref_n064_dts100_rect`) as two judged quantities. `errors.mms_h.linf_tend` and `errors.mms_vel.l2_rel_tend` are reported (5-3). `convergence_order` is not judged: the case is not a member of a refinement pair.
  - The theoretical-comparison judgment against a steady solution is not applied. The non-application basis is "because `tc4_translating_low` is not steady (2-4)".
  - The wall-normal-velocity judgment is not applied. The non-application basis is "because the low's cross-channel velocity is non-zero by design (2-5)".
  - The translation-equivariance, along-channel-structure, restricted-reference, time-refinement and symmetry-pair judgments are not applied. The non-application basis is "because the case is judged here as a single case; its two transformed cases are judged in 6-14 and 6-15".
  - The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, and `hv`, and the threshold is $\le 1.0e{-10}$ for each.

### 6-13. `l1_tc2_perturbed_refinement_restricted`
- `level`: `L1`
- `objective`: confirm, for `tc2_zonal_perturbed`, the error of `h`, `hu` and `hv` of the levels `32`, `64` and `128` against the restricted reference of the level `256` in three norms and its decrease with refinement (2-13), the initial state, the decay of the along-channel structure at every level, mass conservation and positivity.
- target cases:
  - `chan_tc2_xpert_n032_dts100`
  - `chan_tc2_xpert_n064_dts100`
  - `chan_tc2_xpert_n128_dts100`
  - `chan_tc2_xpert_n256_dts100`
- `expected_outcome`: `pass`
- judgment conditions:
  - The initial-state judgment is applied to every case. The evaluation expressions are `errors.initial_h.linf_rel`, `errors.initial_u.linf_rel` and `errors.initial_v.linf_rel`, each with the threshold $\le 1.0e{-9}$, and `errors.initial_vel_cross.linf` with the threshold $\le 1.0e{-12}$ (`m/s`).
  - The `CFL` judgment is applied. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of the case ($|\mathrm{run.n\_step}-\mathrm{n\_step}| \le 0.5$).
  - The depth-positivity judgment is applied. The evaluation expression is `extrema.h.min`, and the threshold is $\ge 1.0e3$.
  - The mass-conservation judgment is applied. The evaluation expression is `metrics.mass_drift_rel`, and the threshold is $\le 1.0e{-10}$.
  - The restricted-reference judgment is applied. Each of the twenty-seven `errors.restricted.<n>.<v>_<k>_rel` (carried by `chan_tc2_xpert_n256_dts100`) applies its band of the 5-5 restricted table as two judged quantities, and each of the eighteen `convergence.restricted.<pair>.<v>_<k>_order` (carried by the same case) applies its lower bound of that table.
  - The along-channel-structure judgment is applied to every case. The evaluation expression is `xstructure.decay_ratio`, and the band is $[0.37,\ 0.95]$ for `chan_tc2_xpert_n032_dts100` and $[0.38,\ 0.95]$ for the other three cases, as two judged quantities.
  - The theoretical-comparison judgment is not applied. The non-application basis is "because `tc2_zonal_perturbed` has no steady reference (2-4); its error is judged against the restricted reference above".
  - The wall-normal-velocity judgment at $t_{end}$ is not applied. The non-application basis is "because the perturbation excites the cross-channel velocity by design".
  - The manufactured-solution, translation-equivariance, time-refinement and symmetry-pair judgments are not applied. The non-application basis is "because `errors.mms_*` are `N/A` for this profile (5-3) and the cases are judged here as refinement levels; the translation pair belongs to 6-6".
  - The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, and `hv`, and the threshold is $\le 1.0e{-10}$ for each.

### 6-14. `l3_axis_exchange_pairs`
- `level`: `L3`
- `objective`: confirm the axis-exchange equivariance of the Controlled Spec §5 (2-11): each case computed with the channel along `y` is the transpose of its base computed along `x`, with the two momentum components exchanged, to round-off, on every unforced profile and on the rectangular forced case.
- pair cases (base → transformed; each transformed case reads its base's final state):
  - `chan_tc2_ref_n064_dts100` → `chan_tc2_ref_n064_dts100_axis`
  - `chan_tc3_ref_n064_dts100` → `chan_tc3_ref_n064_dts100_axis`
  - `chan_tc2_xpert_n064_dts100_sx025` → `chan_tc2_xpert_n064_dts100_sx025_axis`
  - `chan_tc4_ref_n064_dts100_rect` → `chan_tc4_ref_n064_dts100_rectaxis`
- `expected_outcome`: `pass`
- judgment conditions:
  - The symmetry-pair judgment is applied to each transformed case. The evaluation expressions are `errors.axis_exchange.h_l2_rel`, `errors.axis_exchange.hu_l2_rel` and `errors.axis_exchange.hv_l2_rel`, each with the threshold $\le 2.0e{-11}$.
  - The initial-state judgment is applied to each transformed case, against the closed form of 2-3 in the `y` orientation. The evaluation expressions are `errors.initial_u.linf_rel` and `errors.initial_v.linf_rel`, and `errors.initial_h.linf_rel` for the three cases whose profile has a closed-form depth, or `errors.initial_h.below_jet_rel` and `errors.initial_h.above_jet_rel` for `chan_tc3_ref_n064_dts100_axis`, each with the threshold $\le 1.0e{-9}$. On every transformed case whose profile is unforced, `errors.initial_vel_cross.linf` is judged in addition, with the threshold $\le 1.0e{-12}$ (`m/s`).
  - The `CFL` judgment is applied to each transformed case. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of the case ($|\mathrm{run.n\_step}-\mathrm{n\_step}| \le 0.5$; `883`, `567`, `886`, `1152`, the base's).
  - The depth-positivity judgment is applied to each transformed case. The evaluation expression is `extrema.h.min`, and the threshold is that of its profile in 5-5 ($\ge 1.0e3$, $\ge 2.0e3$, $\ge 1.0e3$, $\ge 9.0e3$).
  - The mass-conservation judgment is applied to each transformed case. The evaluation expression is `metrics.mass_drift_rel`, and the threshold is $\le 1.0e{-10}$.
  - The theoretical-comparison, manufactured-solution, restricted-reference, wall-normal-velocity, translation-equivariance, along-channel-structure and time-refinement judgments are not applied. The non-application basis is "because each transformed case equals its base, whose bands are judged in 6-3, 6-4, 6-6 and 6-12, to the round-off the pair judgment bounds".
  - The cross-target judgment is applied to each transformed case. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, and `hv`, and the threshold is $\le 1.0e{-10}$ for each.

### 6-15. `l3_along_channel_mirror_pairs`
- `level`: `L3`
- `objective`: confirm the mirror equivariance along the channel of the Controlled Spec §5 (2-11): each case with the rotation and the flow reversed is the reflection of its base along `x`, with `hu` sign-reversed, to round-off, on every profile and on the rectangular forced case.
- pair cases (base → transformed; each transformed case reads its base's final state):
  - `chan_tc2_ref_n064_dts100` → `chan_tc2_ref_n064_dts100_xmir`
  - `chan_tc3_ref_n064_dts100` → `chan_tc3_ref_n064_dts100_xmir`
  - `chan_tc4_ref_n064_dts100` → `chan_tc4_ref_n064_dts100_xmir`
  - `chan_tc2_xpert_n064_dts100` → `chan_tc2_xpert_n064_dts100_xmir`
  - `chan_tc4_ref_n064_dts100_rect` → `chan_tc4_ref_n064_dts100_rectxmir`
- `expected_outcome`: `pass`
- judgment conditions:
  - The symmetry-pair judgment is applied to each transformed case. The evaluation expressions are `errors.mirror_x.h_l2_rel`, `errors.mirror_x.hu_l2_rel` and `errors.mirror_x.hv_l2_rel`, each with the threshold $\le 2.0e{-11}$.
  - The initial-state judgment is applied to each transformed case, against the closed form of 2-3 with the case's signs. The evaluation expressions are `errors.initial_u.linf_rel` and `errors.initial_v.linf_rel`, and `errors.initial_h.linf_rel` for the four cases whose profile has a closed-form depth, or `errors.initial_h.below_jet_rel` and `errors.initial_h.above_jet_rel` for `chan_tc3_ref_n064_dts100_xmir`, each with the threshold $\le 1.0e{-9}$. On every transformed case whose profile is unforced, `errors.initial_vel_cross.linf` is judged in addition, with the threshold $\le 1.0e{-12}$ (`m/s`).
  - The `CFL` judgment is applied to each transformed case. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of the case ($|\mathrm{run.n\_step}-\mathrm{n\_step}| \le 0.5$; `883`, `567`, `1087`, `886`, `1152`, the base's).
  - The depth-positivity judgment is applied to each transformed case. The evaluation expression is `extrema.h.min`, and the threshold is that of its profile in 5-5 ($\ge 1.0e3$, $\ge 2.0e3$, $\ge 9.0e3$, $\ge 1.0e3$, $\ge 9.0e3$).
  - The mass-conservation judgment is applied to each transformed case. The evaluation expression is `metrics.mass_drift_rel`, and the threshold is $\le 1.0e{-10}$.
  - The theoretical-comparison, manufactured-solution, restricted-reference, wall-normal-velocity, translation-equivariance, along-channel-structure and time-refinement judgments are not applied. The non-application basis is "because each transformed case equals the reflection of its base, whose bands are judged in 6-3, 6-4, 6-6, 6-9 and 6-12, to the round-off the pair judgment bounds".
  - The cross-target judgment is applied to each transformed case. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, and `hv`, and the threshold is $\le 1.0e{-10}$ for each.

### 6-16. `l3_cross_channel_mirror_pairs`
- `level`: `L3`
- `objective`: confirm the mirror equivariance across the channel of the Controlled Spec §5 (2-11): each case with the reference latitude reversed is the reflection of its base along `y`, with `hv` sign-reversed, to round-off, on every profile — `tc4_translating_low` through its depth referred to the channel centre, and `tc3_compact_jet` through the shifted `gh0`.
- pair cases (base → transformed; each transformed case reads its base's final state):
  - `chan_tc2_ref_n064_dts100` → `chan_tc2_ref_n064_dts100_ymir`
  - `chan_tc3_ref_n064_dts100` → `chan_tc3_ref_n064_dts100_ymir`
  - `chan_tc4_ref_n064_dts100` → `chan_tc4_ref_n064_dts100_ymir`
  - `chan_tc2_xpert_n064_dts100` → `chan_tc2_xpert_n064_dts100_ymir`
- `expected_outcome`: `pass`
- judgment conditions:
  - The symmetry-pair judgment is applied to each transformed case. The evaluation expressions are `errors.mirror_y.h_l2_rel`, `errors.mirror_y.hu_l2_rel` and `errors.mirror_y.hv_l2_rel`, each with the threshold $\le 2.0e{-11}$.
  - The initial-state judgment is applied to each transformed case, against the closed form of 2-3 with the case's signs and `gh0`. The evaluation expressions are `errors.initial_u.linf_rel` and `errors.initial_v.linf_rel`, and `errors.initial_h.linf_rel` for the three cases whose profile has a closed-form depth, or `errors.initial_h.below_jet_rel` and `errors.initial_h.above_jet_rel` for `chan_tc3_ref_n064_dts100_ymir` (whose depth before the jet is its own $h_0=23889.890819821492/g$ and beyond it its own $h_N$, the base's $h_0$), each with the threshold $\le 1.0e{-9}$. On every transformed case whose profile is unforced, `errors.initial_vel_cross.linf` is judged in addition, with the threshold $\le 1.0e{-12}$ (`m/s`).
  - The `CFL` judgment is applied to each transformed case. The evaluation expression is `cfl.max`, and the threshold is $\le 1.0$; `run.n_step` is judged equal to the §3 $\mathrm{n\_step}$ of the case ($|\mathrm{run.n\_step}-\mathrm{n\_step}| \le 0.5$; `883`, `567`, `1087`, `886`, the base's).
  - The depth-positivity judgment is applied to each transformed case. The evaluation expression is `extrema.h.min`, and the threshold is that of its profile in 5-5 ($\ge 1.0e3$, $\ge 2.0e3$, $\ge 9.0e3$, $\ge 1.0e3$).
  - The mass-conservation judgment is applied to each transformed case. The evaluation expression is `metrics.mass_drift_rel`, and the threshold is $\le 1.0e{-10}$.
  - The theoretical-comparison, manufactured-solution, restricted-reference, wall-normal-velocity, translation-equivariance, along-channel-structure and time-refinement judgments are not applied. The non-application basis is "because each transformed case equals the reflection of its base, whose bands are judged in 6-3, 6-4, 6-6 and 6-9, to the round-off the pair judgment bounds".
  - The cross-target judgment is applied to each transformed case. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, and `hv`, and the threshold is $\le 1.0e{-10}$ for each.

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
