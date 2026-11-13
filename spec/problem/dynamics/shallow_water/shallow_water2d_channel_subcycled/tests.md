# Tests: 2D shallow water channel, the member sub-cycled, an assembled model (verification input / judgment conditions)

## 0. Meta information
- `status`: `draft`
- `test_profile_id`: `shallow_water2d_channel_subcycled_baseline`
- `test_profile_version`: `0.1.0`
- `spec_ref.spec_kind`: `problem`
- `spec_ref.spec_id`: `shallow_water2d_channel_subcycled`
- `spec_ref.spec_version`: `0.1.0`
- `spec_ref.controlled_spec_path`: `spec/problem/dynamics/shallow_water/shallow_water2d_channel_subcycled/controlled_spec.md`

## 1. Test purpose
This suite verifies the COMPOSITION of the Controlled Spec §5: that `shallow_water2d_channel_subcycled__init` is the member's `__init` called with this case's inputs, and that one step is `substeps` member steps of length `dt / substeps`, at the times `t + (j-1)·dt/substeps`, each on the state the previous one wrote, ending on the member's first `false` result. The member's discretization is the member's to certify; this suite restates the member's judgments on cases equal to a member case input for input (with `substeps` `1`, and with `substeps` `2` where 2-7 states why the member's bands hold), so that a composition that alters the member's arithmetic, its setup or its time is seen.

It judges: the member's own judgments restated with `substeps` $=1$ on the three steady and forced profiles at `nx=32`, the initial state included (6-1 to 6-3); the sub-cycling pairs, a case with `substeps` $=2$ against the case with `substeps` $=1$ and half the `dt_scale`, equal exactly on `tc2_zonal_uniform` and to round-off on the forced `tc4_translating_low`, whose forcing reads the time of each sub-step, and on `tc4_translating_low` with `substeps` $=4$ against `substeps` $=1$ and a quarter of the `dt_scale` (6-4 to 6-6); the member's time-refinement pair with `substeps` $=1$, whose band is `0.1%` of its value and which pins the time each member step is handed (6-7); the member's refinement judgment of `tc4_translating_low` with `substeps` $=2$, in three norms over three levels with their orders, and on the rectangular grid (6-8, 6-9); the symmetry pairs of the member with `substeps` $=2$: the axis exchange, the two mirrors and the translation along the channel (6-10 to 6-13); the run continued from a state sample in a process of its own against the uninterrupted run (6-14); the independence of a case from the cases run before it (6-15); and the refusals: `substeps` at `0` and below, the member's `__init` refusing the case, and the member's step refusing mid-run with `substeps` $=2$ (6-16 to 6-18).

The conditions the composition judgments are built under:
- **The sub-cycling pairs are exact, not bands.** The member's time error is under `0.05%` of every band of the member's suite (the member's `tests.md` 2-8), so a composition that ignores `substeps` or hands every sub-step the step's start time moves no band of 6-1 to 6-3 or 6-8 (one that does not divide `dt` advances the member by `2 dt` and does move the bands of 6-8 and 6-9). The pairs of 6-4 to 6-6 see each: with the premise $N(s/m)=m\,N(s)$ judged on the case (`pair.n_step_premise`), the two runs of a pair perform the same member steps (Controlled Spec §5), so the residual is `0` on `tc2_zonal_uniform`, which does not read the time, and round-off on `tc4_translating_low`, which does (2-5).
- **A forced profile, and the time of a member step pinned absolutely.** The forcing of `tc4_translating_low` reads the time of each sub-step, so a sub-step time that is not `t + (j-1)·dt/substeps` relative to the step's start is seen by 6-5 and 6-6 and by no judgment over an unforced profile. A pair cannot see a time shifted by the same multiple of the sub-step in both its runs (the start of the sub-step read as its end, its midpoint, one sub-step early): the shift is the same function of $\delta$ on both sides. The member's time-refinement pair, restated with `substeps` $=1$ on the forced profile (6-7), sees it, because there the shift moves the forcing time by a different amount in each of its two runs and its band is `0.1%` of its value; with the pairs, it fixes the time of every sub-step.
- **`substeps` beyond `2`.** 6-6 runs `substeps` $=4$, so a composition that is right for one and two sub-steps only (at most two sub-calls, a time wrong from the third sub-call on) is seen. A composition specialised to the values this suite holds (`-1`, `0`, `1`, `2`, `4`) is not, and that rests on `Generate.verify`.
- **A rectangular case and transformed cases with `substeps` $\ge 2$.** A composition that hands the member exchanged extents or orientation inputs reads the right cells on a square grid and in one orientation (6-9, 6-10).
- **Cases of one profile with other `substeps` in one process.** The pair cases of 6-4 to 6-6 have the inputs of each other but `substeps` and `dt_scale`, and the refusal cases of 6-16 run in the same process after cases with `substeps` $=2$, so a composition that keeps `substeps` from an earlier call is seen.
- **Margins stated before the judgments were run**, each beside its bound in 5-5.

What this suite accepts as beyond a kernel judgment:
- **A `false` result of a sub-step that a later sub-step overwrites.** On the wet-domain case of 6-18 the member refuses at the second sub-step of a step, the last; and a member that refuses a state refuses it again, so a composition that runs the remaining sub-steps after a `false` result and returns the last one's result is not separated from §5 by any case this suite can state. The Controlled Spec §7 forbids it; that requirement rests on `Generate.verify`.
- **A sub-step time accumulated (`t + δ + δ + …`) in place of `t + (j-1)·δ`.** The two differ by the rounding of the time alone, which moves the forcing far below every bound of this suite. The Controlled Spec §5 forbids it; that requirement rests on `Generate.verify`.
- **"Exactly once" of the member's `__init`.** A second call with the same arguments re-establishes what the first did, and no capture separates the two. A call skipped on some cases, or made with other arguments, is judged (6-1 to 6-3, 6-9, 6-10, 6-13, 6-15).
- **The member's discretization and its guards.** They are the member's suite's to judge. This suite restates the member's bands on the member's cases (6-1 to 6-3, 6-7 to 6-9) and the member's wet-domain and `__init` refusals (6-17, 6-18), and does not restate the member's `CFL` guard, its forcing and boundary refusals, its long runs or its restricted family.

## 2. Input-defaulting rules
### 2-1. Basic constants
Every input but `subcycle.substeps` is the member's, with the member's default values (the member's `tests.md` 2-1): `g=9.80616`, `Omega=7.292e-5`, `a=6.37122e6`, `theta0 = pi/4` (the double-precision value `0.7853981633974483`), `channel_axis=x` and `zonal_sign=+1` on every case but the transformed cases of 2-10; `gh0=2.94e4` and $u_0=$ `zonal_sign*2*pi*a/(12*86400)` for `tc2_zonal_uniform`, `tc3_compact_jet` and `tc2_zonal_perturbed`, `gh0=1.0e5` and `u_jet=40.0` for `tc4_translating_low`; `ng=2`; `cfl_target=0.45`. The derived values $h_0=gh_0/g$, $u_0$, $f_0=2\,\Omega\sin\theta_0$, $\beta=2\,\Omega\cos\theta_0/a$, the orientation sign $\sigma$ ($+1$ for `channel_axis=x`, $-1$ for `y`), and the parameters of the low $U_j$, $\psi_0$, $R_{low}$, $c_{tr}$, $s_{c0}$, $r_c$ are evaluated from the inputs by the expressions of the Controlled Spec §6.1 wherever they are used; no rounded literal of a derived value is an input.

The driver passes the inputs of the Controlled Spec §5.1 by number, so every case carries, beside the inputs above: `along_axis` (`1` for `channel_axis=x`, `2` for `y`), `flow_profile_id` (`1` `tc2_zonal_uniform`, `2` `tc3_compact_jet`, `3` `tc2_zonal_perturbed`, `4` `tc4_translating_low`), and all three of `eta0`, `shift_x_fraction` and `u_jet`, each `0.0` on a case whose `flow_profile` does not define it. Every case carries `subcycle.substeps`, an integer, the case input of this `spec` (Controlled Spec §6.2). `nx`, `ny`, `ng`, `along_axis`, `flow_profile_id`, `zonal_sign` and `subcycle.substeps` are integers.

### 2-2. Coriolis profile
`coriolis_profile=beta_plane` in every case: $f(r)=f_0+\beta\,(r-L_r/2)$ at every interior cell centre (Controlled Spec §6.1).

### 2-3. Flow profiles (initial condition)
The profiles are those of the Controlled Spec §6.1, written in $(s, r)$ and $\sigma$; on every case but the axis-exchange case of 2-10, $(s, r)=(x, y)$ and $\sigma=+1$.
- `tc2_zonal_uniform`: $u_s=u_0$, $u_r=0$, $h(r)=h_0-\sigma\dfrac{u_0}{g}\left[f_0\,(r-L_r/2)+\dfrac{\beta}{2}\,(r-L_r/2)^2\right]$, with `L_x=L_y=6.0e6`.
- `tc3_compact_jet`: $r_b=L_r/8$, $r_e=7L_r/8$, $x_e=0.3$, $\zeta=x_e\,(r-r_b)/(r_e-r_b)$, $b(\zeta)=0$ ($\zeta\le0$), $e^{-1/\zeta}$ ($\zeta>0$), $u_s(r)=u_0\,b(\zeta)\,b(x_e-\zeta)\,e^{4/x_e}$, $u_r=0$, $h(r)=h_0-\dfrac{\sigma}{g}\int_0^r f\,u_s\,dr'$ by the composite Simpson quadrature of the Controlled Spec §6.1, with `L_x=L_y=8.0e6`. The depth beyond the jet is the constant $h_N$ of the Controlled Spec §6.1 ($2436.2126\ \mathrm{m}$ with the constants of 2-1 and $L_r=8.0\times10^6$).
- `tc2_zonal_perturbed`: $h(s,r)=h_{TC2}(r)+\eta_0\sin\left(2\pi\,(s/L_s-\mathrm{shift\_x\_fraction})\right)$ with $h_{TC2}$ the `tc2_zonal_uniform` depth, $u_s=u_0$, $u_r=0$, `eta0=30.0` (`m`), with `L_x=L_y=6.0e6`.
- `tc4_translating_low`: the prescribed flow of the Controlled Spec §6.1 at $t=0$, $h=\tilde h$, $hu=\tilde h\,\tilde u$, $hv=\tilde h\,\tilde v$, with `L_x=L_y=8.0e6` (the rectangular case of 2-7 carries `L_x=6.0e6`, `ny=48`, and its transposed case `L_x=8.0e6`, `L_y=6.0e6`, `nx=48`, `ny=64`).

### 2-4. Reference solutions
`tc2_zonal_uniform` and `tc3_compact_jet` are steady: the reference at $t_{end}$ is the initial state — for `tc2_zonal_uniform` the closed form of 2-3 at the cell centres with the along-channel momentum $h(r)\,u_0$ and the cross-channel momentum $0$, for `tc3_compact_jet` the case's own discrete initial state. The reference of `tc4_translating_low` at $t_{end}$ is the prescribed flow of the Controlled Spec §6.1 at $t_{end}$, $h_{ref}=\tilde h(x_i,y_j,t_{end})$, $hu_{ref}=\tilde h\tilde u$, $hv_{ref}=\tilde h\tilde v$. The initial state is judged against its closed form: `h` for `tc2_zonal_uniform`, `tc2_zonal_perturbed` and `tc4_translating_low`, both velocity components for every profile, and for `tc3_compact_jet` the depth of the lines before the jet ($h=h_0$) and beyond it ($h=h_N$).

### 2-5. The sub-cycling pairs and the time-refinement pair
Each sub-cycling pair is a case with `substeps` $=m$ and `dt_scale=1.0` (`_m<m>`) and a case with `substeps=1` and `dt_scale` $=1/m$ (`_dts<100/m>_m1`), every other input equal and the member's case of that profile at `nx=32`: `tc2_zonal_uniform` with $m=2$ (6-4), `tc4_translating_low` with $m=2$ (6-5) and with $m=4$ (6-6). The member's procedure of §3 gives $\mathrm{n\_step}=441$ at `dt_scale` `1.0` and `882` at `0.5` on the first, and `543`, `1086` and `2172` at `1.0`, `0.5` and `0.25` on the second, so $N(1/m)=m\,N(1.0)$ holds on all three and the two runs of a pair perform the same member steps of the same length (Controlled Spec §5, the invariant "sub-cycling is the member at the sub-step"; $\Delta t/2$ and $\Delta t/4$ are exact in binary64); the premise is judged on each pair (`pair.n_step_premise`), so a kernel whose `__init` plans another step count fails the premise rather than the residual. On `tc2_zonal_uniform` no operation reads the time, and the two final states are equal exactly; on `tc4_translating_low` the forcing reads the sub-step time, which the two compute as $t_{start}+n\,\Delta t+(j-1)\,\Delta t/m$ and $t_{start}+(mn+j-1)\,\Delta t/m$, and the residual is round-off.

The time-refinement pair (6-7) is the member's own (the member's `tests.md` 2-8 and 5-5): `chs_tc4_ref_n032_dts100_m1` and `chs_tc4_ref_n032_dts050_m1`, the member's cases `chan_tc4_ref_n032_dts100` and `chan_tc4_ref_n032_dts100_half` input for input with `substeps=1`, judged by the relative difference of their final depths, `errors.time_pair.h_l2_rel`, against the member's band $[2.482e{-8},\ 2.488e{-8}]$ (reference `2.4847e-8`; `0.1%` of its value, because the value moves only by round-off under a conforming kernel). The bench of 8-2 reads `2.4847e-8`.

Measured on the bench of 8-2 (the hand-written composing model over the hand-written member): `pair.h`, `pair.hu`, `pair.hv` read `0` on 6-4, `6.7e-16`, `3.5e-15`, `1.1e-14` on 6-5 and `8.4e-16`, `5.0e-15`, `1.3e-14` on 6-6. A composition that calls the member once with `dt` (the sub-step count ignored) reads `1.5e-4`, `8.8e-3`, `1.4e-2` (absolute) on 6-4 and `4.0e-7`, `8.3e-7`, `4.4e-6` on 6-5; one that hands every sub-step the step's start time `t` reads `1.3e-5`, `1.4e-4`, `1.4e-3` on 6-5, and one that advances the sub-step time by `dt` in place of `dt/2` reads `1.3e-5`, `1.4e-4`, `1.4e-3`; one that does not divide `dt` reads `45.4` (`m`) on 6-4 and `1.4e-2` on 6-5. The bound of 6-5, `1.0e-12`, is `94` times the largest conforming reading and `4.0e5` times below the smallest defect's. On 6-6 a composition that makes at most two sub-calls reads `4.4e-8` in `pair.h`, and one whose sub-step time is wrong from the third sub-call on reads `9.9e-6`. A composition that hands the $j$-th sub-step the time $t+j\,\delta$ (the sub-step's end), $t+(j-\tfrac12)\,\delta$, $t+(j-2)\,\delta$ or $t+(j+9)\,\delta$ reads within round-off on every pair and `5.9e-6`, `2.9e-6`, `5.9e-6`, `5.9e-5` on 6-7, against its upper bound `2.488e-8`.

### 2-6. The member's cases with `substeps` $=1$
`chs_tc2_ref_n032_dts100_m1`, `chs_tc3_ref_n032_dts100_m1` and `chs_tc4_ref_n032_dts100_m1` are the member's cases `chan_tc2_ref_n032_dts100`, `chan_tc3_ref_n032_dts100` and `chan_tc4_ref_n032_dts100` input for input, with `substeps=1`. By the Controlled Spec §5 their runs are the member's runs of those cases, so 6-1 to 6-3 restate the member's judgments on them with the member's bands at `nx=32` (the member's `tests.md` 5-5).

### 2-7. The member's bands under `substeps` $=2$
6-8 and 6-9 restate the member's bands of `tc4_translating_low` at `nx=32`, `64`, `128` and on the rectangular grid on the cases `chs_tc4_ref_n<n>_dts100_m2` and `chs_tc4_ref_n064_dts100_m2_rect`, with `substeps=2`. Those cases advance the member by $\Delta t/2$, and the member's bands were calibrated at $\Delta t$: they hold because the error against the prescribed flow is dominated by the spatial discretization, which `substeps` does not change (the member's `tests.md` 2-8: replacing `RK4` by a second- or third-order method, or `cfl_target` between `0.2` and `0.6`, moves it by less than `0.05%`). The bands are the member's, calibrated against the member's independent reference implementation; the values under `substeps=2` are measured on the bench of 8-2, not on that reference, and each lies inside its band there. The orders are the member's lower bounds. `chs_tc4_ref_n064_dts100_m2_rect` carries `nx=64`, `ny=48`, `L_x=6.0e6`, `L_y=8.0e6`, so no exchange of the two extents, lengths or spacings is the identity.

### 2-8. The restart case
`chs_tc4_ref_n032_dts100_m2_cont` is a `continuation case` (`docs/GLOSSARY.md`) of `chs_tc4_ref_n032_dts100_m2`: its inputs are its base's, and the host runs it in a process of its own from the base's state sample at committed step `272` of the `543` steps the member's `__init` plans (§3, the `restart` line). The driver calls `shallow_water2d_channel_subcycled__init` on the case as for any case, so `dt`, `n_step_plan` and `init_ok` are the member's, overwrites `h`, `hu` and `hv` with the sample, and continues the step count and the time from step `272`. A composition that keeps anything between two calls of `shallow_water2d_channel_subcycled__step` outside the state — a clock or a step counter in place of the `t` argument, a sub-step length or a sub-step count kept from an earlier call, a value cached once per process — ends the continuation in another state than the uninterrupted run, and 6-14 judges the two final states equal exactly. Measured on the bench of 8-2: a private clock restarted by `__init` and advanced by each sub-step reads `97.2` (`m`) in `restart.h`; a private step counter reset by `__init` reads the same. The placement (`docs/TESTS.md`, the `restart` line): the base runs the forced profile with `substeps=2`, so the forcing reads every sub-step's time and a continuation that restarts a clock is seen; the Controlled Spec defines no call interval, so there is no multiple to avoid; `272` and `543 - 272` are both at least `2`; and the base sorts after the refusal cases, the `tc2` and `tc3` cases, `chs_tc4_ref_n032_dts025_m1`, `chs_tc4_ref_n032_dts050_m1` and `chs_tc4_ref_n032_dts100_m1`, whose `substeps`, profile or `dt` differ, so a value a kernel caches once per process is another case's in the base's process and the base's own in the continuation's.

### 2-9. The repeated case
The driver does not reset a kernel's private state between cases, and the cases run in sorted order, so a case can only see what the cases before it left. `chs_zrepeat_n032_dts100_m2` sorts after every other case and repeats `chs_tc2_ref_n032_dts100_m2` exactly, after cases of other profiles: a composition whose `__init` calls the member's `__init` in every case, which establishes the member's private data from the case's inputs, gives the same binary the same state. The residuals `errors.repeat.<v>_l2_rel` (5-4) read `0` on the bench of 8-2, and the bound is `1.0e-12`, the member's. A composition that calls the member's `__init` only at the first `__init` of a process leaves the later cases with the outputs the driver zeroed and the member's data of the first case, and fails here and in every judgment of a later case.

### 2-10. Symmetry pairs
The member's equivariances (the member's Controlled Spec §5), judged with `substeps=2` on `tc4_translating_low` at `nx=64`, the profile whose forcing reads the sub-step time, and on `tc2_zonal_perturbed`, the one unforced profile that is not uniform along the channel (`docs/TESTS.md` §Symmetry pairs). Each transformed case differs from its base only in the inputs named and carries the metric.
- **Axis exchange** (6-10): `chs_tc4_ref_n064_dts100_m2_rectaxis`, base `chs_tc4_ref_n064_dts100_m2_rect`: `channel_axis=y`, `Omega=-7.292e-5`, `nx=48`, `ny=64`, `L_x=8.0e6`, `L_y=6.0e6` (the transpose of the rectangular base, so the exchange of the extents, the lengths and the spacings is not the identity); the final state is $(h,\ hu,\ hv)'=(h^T,\ hv^T,\ hu^T)$ of the base's.
- **Mirror along the channel** (6-11): `chs_tc4_ref_n064_dts100_m2_xmir`, base `chs_tc4_ref_n064_dts100_m2`: `Omega=-7.292e-5`, `zonal_sign=-1`; the final state is $(h,\ -hu,\ hv)$ of the base's reflected along `x` (`flip(·, 0)`).
- **Mirror across the channel** (6-12): `chs_tc4_ref_n064_dts100_m2_ymir`, base `chs_tc4_ref_n064_dts100_m2`: `theta0=-0.7853981633974483`; the final state is $(h,\ hu,\ -hv)$ of the base's reflected along `y` (`flip(·, 1)`).
- **Translation along the channel** (6-13): `chs_tc2_xpert_n064_dts100_m2_sx025`, base `chs_tc2_xpert_n064_dts100_m2` (`shift_x_fraction=0.0`): `shift_x_fraction=0.25`, a shift of `16` cells along `x`; the final state is the base's moved by `16` cells along `x`. The decay of the along-channel structure (`xstructure.decay_ratio`, 5-4) is the member's judgment of that profile, restated.

Each pair is judged on every state variable by an `l2` residual normalised by the `l2` norm of the base's variable (a momentum component by the larger of the base's two momentum norms), with the bound `2.0e-11`, the member's: the transformed case runs the same operations on transformed data, so the residual of a conforming kernel is round-off. The bench of 8-2 reads at most `2.8e-15` on the three pairs of `tc4_translating_low` and `0` on the translation. A composition that hands the member `along_axis` `1` whatever the case's reads `8.0e-2` in `h` on 6-10.

### 2-11. The refusal cases
- `substeps` below `1` (6-16): `chs_guard_n032_dts100_m0` (`substeps=0`) and `chs_guard_n032_dts100_mneg1` (`substeps=-1`), each the member's case `chan_tc2_ref_n032_dts100` with `history.every=0`. Each is refused at the FIRST call by step 1 of the Controlled Spec §5: no step is committed and the state is the one `__init` wrote. A guard that refuses `0` alone runs no sub-step at `-1` and returns `true`: the bench of 8-2 reads `ok` `1` and `441` committed steps on that case.
- The member's `__init` refusing the case (6-17): `chs_tc2_ref_n032_dts100_m2_badprofile`, the member's case `chan_tc2_ref_n032_dts100_badprofile` (`flow_profile_id=0`) with `substeps=2` and `history.every=0`. The member writes `init_ok` `false` with `dt=0` and `n_step_plan=0`, and the driver runs no step. A composition that writes `init_ok` `true` whatever the member returned makes the driver stop on the case's `dt` (the run ends with a non-zero exit, and every later case has no capture).
- The member's step refusing mid-run with `substeps=2` (6-18): `chs_dry_n032_dts1600_m2`, the member's case `chan_dry_n032_dts800` with `substeps=2`, `dt_scale=16.0` and `history.every=0`: `tc2_zonal_uniform` of 2-3 on `nx=ny=32`, `L_x=L_y=6.0e6`, `gh0=2.94e4`, `channel_axis=x`, `zonal_sign=+1`, the constants of 2-1. The §3 procedure gives $\mathrm{n\_step}=28$ and $dt=3085.71$ (`s`), so the sub-step is the member's step at `dt_scale` `8.0`, $1542.86$ (`s`), on which the member's wet-domain error returns `false` at its twelfth step (the member's `tests.md` 2-9-2): the second sub-step of the sixth step, after five committed steps. The last committed state differs from the initial one by `324.7` (`m`) in `h` on the bench of 8-2. A composition that returns `true` whatever the member returned runs to `n_step` with `ok` `1`.

## 3. Execution-control rules
- $t_{start}=0$ and $t_{end}=86400$ (`s`; one day) on every case.
- A case whose `__init` writes `init_ok` as `false` runs no step.
- `dt` is decided by the member's procedure, which the member's `__init` evaluates over the initial state it wrote and writes as `dt` and `n_step_plan`:
  1. $\lambda_0=\max_{i,j}\left((|u_{i,j}|+\sqrt{gh_{i,j}})/dx+(|v_{i,j}|+\sqrt{gh_{i,j}})/dy\right)$ at the initial time;
  2. $\mathrm{dt\_raw}=\mathrm{dt\_scale}\cdot \mathrm{cfl\_target}/\lambda_0$, $\mathrm{cfl\_target}=0.45$;
  3. $\mathrm{n\_step}=\lceil (t_{end}-t_{start})/\mathrm{dt\_raw}\rceil$;
  4. $dt=(t_{end}-t_{start})/\mathrm{n\_step}$.

  `substeps` does not enter the procedure: `dt` is the step of this `problem`, and the member is advanced by `dt / substeps`.
- The state the driver holds per case consists of `h`, `hu`, `hv` (`nx` × `ny` each, the first array axis `x` and the second `y` in either orientation), the scalars `dt`, `n_step_plan` and `init_ok`, and the scalar `ok`. All of them start at `0`. `shallow_water2d_channel_subcycled__init` writes `h`, `hu`, `hv`, `dt`, `n_step_plan` and `init_ok`, so no case carries an `initial_state` entry, and the initial capture is taken after it. `__init` is bound by the name of each of its arguments to the case input of that name (2-1, 2-3, §4), except `nx_g` and `ny_g`, the case's derived values of the same names (each case's `nx` and `ny`), and `i0` and `j0`, the global indices, counted from 1, of the first cell along `x` and `y` of the block the driver hands the operation; its six outputs are the states of the same names, with `init_ok` its result.
- `shallow_water2d_channel_subcycled__step` is bound to `nx`, `ny`, the state `dt`, the driver's time as `t`, the case input `subcycle.substeps` as `substeps`, the states `h`, `hu`, `hv`, each its own `inout` argument, and the state `ok`, its result, which the driver writes as `1` or `0` after each call.
- The stop condition is $n=\mathrm{n\_step}$: the driver's loop runs `n_step_plan` steps with the time step `dt`, both as `__init` wrote them, the time at step $n$ ($n=0..\mathrm{n\_step}-1$) being $t_{start}+n\,dt$, and a `false` result of `__step` ends it on the last committed state. The driver's `step` is the count of committed steps, `0` at the initial capture.
- The driver folds the extrema of `h`, `hu` and `hv` over every committed state into `series.<v>_min` and `series.<v>_max`, and samples the whole state every `history.every` committed steps, about half of each case's step count, so a case takes one or two samples; `0` (no sample) on the refusal cases:

history: every = (history.every) chs_dry_n032_dts1600_m2 = 0, chs_guard_n032_dts100_m0 = 0, chs_guard_n032_dts100_mneg1 = 0, chs_tc2_ref_n032_dts050_m1 = 441, chs_tc2_ref_n032_dts100_m1 = 221, chs_tc2_ref_n032_dts100_m2 = 221, chs_tc2_ref_n032_dts100_m2_badprofile = 0, chs_tc2_xpert_n064_dts100_m2 = 443, chs_tc2_xpert_n064_dts100_m2_sx025 = 443, chs_tc3_ref_n032_dts100_m1 = 142, chs_tc4_ref_n032_dts025_m1 = 1086, chs_tc4_ref_n032_dts050_m1 = 543, chs_tc4_ref_n032_dts100_m1 = 272, chs_tc4_ref_n032_dts100_m2 = 272, chs_tc4_ref_n032_dts100_m2_cont = 272, chs_tc4_ref_n032_dts100_m4 = 272, chs_tc4_ref_n064_dts100_m2 = 544, chs_tc4_ref_n064_dts100_m2_rect = 576, chs_tc4_ref_n064_dts100_m2_rectaxis = 576, chs_tc4_ref_n064_dts100_m2_xmir = 544, chs_tc4_ref_n064_dts100_m2_ymir = 544, chs_tc4_ref_n128_dts100_m2 = 1087, chs_zrepeat_n032_dts100_m2 = 221

restart: chs_tc4_ref_n032_dts100_m2_cont = chs_tc4_ref_n032_dts100_m2 @ 272

distribute: x = grid.nx (min_block 2), y = grid.ny (min_block 2)

## 4. Case-expansion rules
### 4-1. family definition
| family_id | purpose | sweep | fixed |
| --- | --- | --- | --- |
| `chs_tc2_ref` | the member's case restated (2-6), the pair of 6-4, the repeat base (2-9) | $nx=ny=32$; (`substeps`, `dt_scale`) in `(1, 1.0)`, `(2, 1.0)`, `(1, 0.5)` | the member's `chan_tc2_ref_n032_dts100` |
| `chs_tc3_ref` | the member's case restated (2-6) | $nx=ny=32$, `substeps=1` | the member's `chan_tc3_ref_n032_dts100` |
| `chs_tc4_ref` | the member's case restated (2-6), the pairs of 6-5 to 6-7, the refinement family (2-7), the restart base (2-8) | $nx=ny\in\{32,64,128\}$, `substeps=2`; at $nx=32$ also (`substeps`, `dt_scale`) `(1, 1.0)`, `(1, 0.5)`, `(1, 0.25)` and `(4, 1.0)` | the member's `chan_tc4_ref_n<n>_dts100` |
| `chs_tc2_xpert` | the translation pair (2-10) | $nx=ny=64$, `substeps=2` | the member's `chan_tc2_xpert_n064_dts100` |
| `chs_guard` | the refusal of `substeps` (2-11) | $nx=ny=32$, `substeps` in `{0, -1}` | the member's `chan_tc2_ref_n032_dts100`, `history.every=0` |
| `chs_dry` | the member's wet-domain refusal mid-run (2-11) | $nx=ny=32$, `substeps=2`, `dt_scale=16.0` | `flow_profile=tc2_zonal_uniform`, `L_x=L_y=6.0e6`, `gh0=2.94e4`, `history.every=0` (the member's `chan_dry_n032_dts800` but `dt_scale`) |
| `chs_zrepeat` | case independence (2-9) | $nx=ny=32$, `substeps=2` | the inputs of `chs_tc2_ref_n032_dts100_m2`, exactly |

Every case carries the member's inputs of the member case it names, `ng=2`, the constants of 2-1, `subcycle.substeps` and the `history.every` of §3.

### 4-2. `case_id` generation rule
- The template is `{family}_n{nx:03d}_dts{dts_pct:03d}_m{substeps}`, with `dts_pct` $=\mathrm{round}(100\cdot\mathrm{dt\_scale})$ and a negative `substeps` written `neg` and its magnitude; the override cases of 4-3 carry a suffix after their base's `case_id`, so each sorts after its base (`docs/TESTS.md` §Refinement judgment, carrying case).

### 4-3. Explicit-override cases
- `chs_tc4_ref_n064_dts100_m2_rect`: base `chs_tc4_ref_n064_dts100_m2`; overrides only `ny` to `48` and `L_x` to `6.0e6` (2-7).
- `chs_tc4_ref_n064_dts100_m2_rectaxis`: base `chs_tc4_ref_n064_dts100_m2_rect`; overrides only `nx` to `48`, `ny` to `64`, `L_x` to `8.0e6`, `L_y` to `6.0e6`, `channel_axis` to `y` and `Omega` to `-7.292e-5`, and `along_axis` follows `channel_axis` (2-1, 2-10).
- `chs_tc4_ref_n064_dts100_m2_xmir`: base `chs_tc4_ref_n064_dts100_m2`; overrides only `Omega` to `-7.292e-5` and `zonal_sign` to `-1` (2-10).
- `chs_tc4_ref_n064_dts100_m2_ymir`: base `chs_tc4_ref_n064_dts100_m2`; overrides only `theta0` to `-0.7853981633974483` (2-10).
- `chs_tc2_xpert_n064_dts100_m2_sx025`: base `chs_tc2_xpert_n064_dts100_m2`; overrides only `shift_x_fraction` to `0.25` (2-10).
- `chs_tc2_ref_n032_dts100_m2_badprofile`: base `chs_tc2_ref_n032_dts100_m2`; overrides only `flow_profile_id` to `0` and `history.every` to `0` (2-11).
- `chs_tc4_ref_n032_dts100_m2_cont`: the continuation of 2-8: base `chs_tc4_ref_n032_dts100_m2`, nothing overridden; it starts from the base's state sample at committed step `272` (§3, the `restart` line) and sorts after its base.

## 5. Judged quantities
### 5-1. How a quantity is judged
A test is judged by the host's predicates over the captures alone (`docs/TESTS.md` requirement 9). A name below is what a test's `quantities` line lists; a two-sided band is two names, `<name>.lower` and `<name>.upper`, each with its own predicate. The definitions of 5-4 are expressions over the captured state, the captured time and the case inputs; a value measured on a reference or on the bench is a calibration, not an input.

### 5-2. Judged names
`<v>` stands for each of `h`, `hu`, `hv`; `<k>` for each of `l1`, `l2`, `linf`; `<pair>` for each of `n032_to_n064` and `n064_to_n128`.
- `cfl.max`, `metrics.mass_drift_rel`, `extrema.h.min`, `extrema.v_cross.max_abs`
- `errors.initial_h.linf_rel`, `errors.initial_h.below_jet_rel`, `errors.initial_h.above_jet_rel`, `errors.initial_u.linf_rel`, `errors.initial_v.linf_rel`, `errors.initial_vel_cross.linf`
- `run.n_step`, `init_ok`, `init.n_step_plan`, `init.dt`, `ok`, `run.state_moved`, `run.first_call_refused`
- `errors.steady_<v>.<k>_rel_tend` (nine), `errors.mms_<v>.<k>_rel_tend` (nine)
- `convergence.<pair>.<k>_order` (the order of `h`; six), `convergence.<pair>.hu_<k>_order`, `convergence.<pair>.hv_<k>_order` (twelve)
- `pair.n_step_premise`, `pair.<v>` (three), `errors.time_pair.h_l2_rel`
- `errors.axis_exchange.<v>_l2_rel`, `errors.mirror_x.<v>_l2_rel`, `errors.mirror_y.<v>_l2_rel` (three each), `errors.symmetry_h.l2_rel`, `errors.symmetry_hu.l2_rel`, `errors.symmetry_hv.l2_rel`, `xstructure.decay_ratio`
- `errors.repeat.<v>_l2_rel` (three), `restart.<v>` (three)

### 5-3. Cases of each quantity
- A quantity is evaluated on the target cases of the tests that list it; it is not defined elsewhere.
- `pair.*` is carried by the `_m<m>` case of each pair, which sorts after its `_dts<100/m>_m1` case and reads its captures; `errors.time_pair.h_l2_rel` by `chs_tc4_ref_n032_dts100_m1`, which sorts after `chs_tc4_ref_n032_dts050_m1` and reads its final state.
- `convergence.n032_to_n064.*` is carried by `chs_tc4_ref_n064_dts100_m2` and `convergence.n064_to_n128.*` by `chs_tc4_ref_n128_dts100_m2`.
- `errors.axis_exchange.*`, `errors.mirror_x.*`, `errors.mirror_y.*` and `errors.symmetry_*` are carried by the transformed case of 2-10, which reads its base's final state.
- `errors.repeat.*` is carried by `chs_zrepeat_n032_dts100_m2`, and `restart.<v>` by `chs_tc4_ref_n032_dts100_m2_cont`, each reading its base's final state.
- `errors.steady_*` are computed for `tc2_zonal_uniform` and `tc3_compact_jet`, `errors.mms_*` for `tc4_translating_low`, `errors.initial_h.linf_rel` for every profile but `tc3_compact_jet`, `errors.initial_h.below_jet_rel` and `above_jet_rel` for `tc3_compact_jet` only, `errors.initial_vel_cross.linf` for the unforced profiles, `extrema.v_cross.max_abs` for `tc2_zonal_uniform` and `tc3_compact_jet`, `xstructure.decay_ratio` for `tc2_zonal_perturbed`.
- Every other quantity is evaluated per case.

### 5-4. Definition of the quantities
Sums, norms and extrema run over the interior cells of the case's grid; `initial` / `final` read the capture before the first step / after the last committed one, and `history` every sample of the case. $\|e\|_1=\sum|e_{i,j}|$, $\|e\|_2=(\sum e_{i,j}^2)^{1/2}$, $\|e\|_\infty=\max|e_{i,j}|$; $u=hu/h$, $v=hv/h$; $dx=L_x/nx$, $dy=L_y/ny$.

The member's quantities, restated (the member's `tests.md` 5-4):
- `cfl.max`: the stability index of a member step, with the step the member is handed, $\delta=\mathtt{initial.dt}/\mathrm{substeps}$, and $\lambda(\cdot)=\max_{i,j}\left((|u|+\sqrt{gh})/dx+(|v|+\sqrt{gh})/dy\right)$: two predicates under the one name, $\delta\cdot\max(\lambda(\mathtt{initial}),\lambda(\mathtt{final}))$ and $\delta\cdot\max$ over the samples of $\lambda(\mathtt{history})$. With `substeps=1` it is the member's `cfl.max`.
- `metrics.mass_drift_rel`: $|M_{end}-M_0|/\max(|M_0|,10^{-14})$, $M=\sum_{i,j}h_{i,j}\,dx\,dy$ at the initial and the final capture.
- `extrema.h.min`: two predicates under the one name: the smaller of the minima of `h` at the two captures, and the minimum of `series.h_min` over every committed state.
- `extrema.v_cross.max_abs`: $\max_{i,j}|v_{r,i,j}|$ at the final state, the cross-channel velocity ($v$ for `channel_axis=x`).
- `errors.initial_h.linf_rel` $=\max|h(0)-h_{ref}|/\max|h_{ref}|$ with $h_{ref}$ the closed form of 2-3; `errors.initial_h.below_jet_rel` $=\max_{J_b}|h(0)-h_0|/h_0$ and `errors.initial_h.above_jet_rel` $=\max_{J_e}|h(0)-h_N|/h_0$ over the cells whose cross-channel coordinate is below $r_b$ ($J_b$) and above $r_e$ ($J_e$); `errors.initial_u.linf_rel` $=\max|u(0)-u_{ref}|/U_s$ and `errors.initial_v.linf_rel` $=\max|v(0)-v_{ref}|/U_s$ with the closed-form velocity of 2-3 and $U_s=|u_0|$ ($u_{jet}$ for `tc4_translating_low`); `errors.initial_vel_cross.linf` $=\max|u_r(0)|$ (`m/s`).
- `run.n_step` $=$ `final.step - initial.n_step_plan`; `init_ok` $=$ `initial.init_ok`; `init.n_step_plan` $=|\mathtt{initial.n\_step\_plan}-N|$ and `init.dt` $=|\mathtt{initial.dt}-(t_{end}-t_{start})/N|\,/\,((t_{end}-t_{start})/N)$, with $N$ the step count of §3 evaluated over `initial.h`, `initial.hu`, `initial.hv`; `ok` $=$ `final.ok`; `run.state_moved` $=\max|h(t_{final})-h(0)|$.
- The steady-solution and manufactured-solution errors: for $q$ one of `h`, `hu`, `hv` at $t_{end}$ and $q_{ref}$ its reference of 2-4, $\|q-q_{ref}\|_k/N_{q,k}$, with $N_{q,k}=\|q_{ref}\|_k$ for `h` and the along-channel momentum, and for the cross-channel momentum its own reference's norm for `tc4_translating_low` and $\max(\|hu_{ref}\|_k,\|hv_{ref}\|_k)$ for the steady profiles; named `errors.steady_<v>.<k>_rel_tend` (`tc2_zonal_uniform`, `tc3_compact_jet`) and `errors.mms_<v>.<k>_rel_tend` (`tc4_translating_low`).
- `convergence.<pair>.*`: $\log_2(e_{coarse}/e_{fine})$ of the `errors.mms_*` of the same norm and variable over the two levels of the pair, `convergence.<pair>.<k>_order` for `h`, `convergence.<pair>.hu_<k>_order` and `hv_<k>_order` for the momentum.
- The symmetry-pair residuals, with $q'$ the transformed case's final state, $q$ its base's, $N_h=\|h\|_2$ and $N_m=\max(\|hu\|_2,\|hv\|_2)$ of the base, $\cdot^T$ the exchange of the two array axes, $\mathrm{flip}_x$, $\mathrm{flip}_y$ the reversal of the first, second array axis: axis exchange $\|h'-h^T\|_2/N_h$, $\|hu'-hv^T\|_2/N_m$, $\|hv'-hu^T\|_2/N_m$; mirror along the channel $\|h'-\mathrm{flip}_x(h)\|_2/N_h$, $\|hu'+\mathrm{flip}_x(hu)\|_2/N_m$, $\|hv'-\mathrm{flip}_x(hv)\|_2/N_m$; mirror across the channel $\|h'-\mathrm{flip}_y(h)\|_2/N_h$, $\|hu'-\mathrm{flip}_y(hu)\|_2/N_m$, $\|hv'+\mathrm{flip}_y(hv)\|_2/N_m$; named `errors.axis_exchange.<v>_l2_rel`, `errors.mirror_x.<v>_l2_rel`, `errors.mirror_y.<v>_l2_rel`.
- `xstructure.decay_ratio` $=\|h(t_{end})-\mathrm{shift}(h(t_{end}),n_s/2)\|_2/\|h(0)-\mathrm{shift}(h(0),n_s/2)\|_2$, with $\mathrm{shift}(\cdot,n_s/2)$ the cyclic shift by half the domain along the channel.
- `errors.repeat.<v>_l2_rel`: $\|h'-h\|_2/N_h$, $\|hu'-hu\|_2/N_m$, $\|hv'-hv\|_2/N_m$ with $q'$ the final state of `chs_zrepeat_n032_dts100_m2` and $q$ that of `chs_tc2_ref_n032_dts100_m2`.
- `restart.<v>`: $\max_{i,j}|v^{cont}_{i,j}-v^{base}_{i,j}|$ over the final states of `chs_tc4_ref_n032_dts100_m2_cont` and `chs_tc4_ref_n032_dts100_m2`, `maxabs(final.<v> - at('chs_tc4_ref_n032_dts100_m2').final.<v>)` (2-8); exactly `0` under a kernel that keeps nothing between two steps outside the state.

The quantities of this suite:
- `pair.n_step_premise`: $|N_B-m\,N_A|$ with $N_A$ the `initial.n_step_plan` of the `_m<m>` case $A$ of the pair and $N_B$ that of its `_dts<100/m>_m1` case $B$, `abs(at('<B>').initial.n_step_plan - <m> * initial.n_step_plan)` on $A$ (2-5).
- `pair.<v>`: on 6-4, $\max_{i,j}|v^{A}_{i,j}-v^{B}_{i,j}|$ over the final states of $A$ and $B$, `maxabs(final.<v> - at('<B>').final.<v>)`; on 6-5 and 6-6, the same divided by $\max_{i,j}|v^{B}_{i,j}|$.
- `errors.time_pair.h_l2_rel` $=\|h_{half}(t_{end})-h(t_{end})\|_2/\|h(t_{end})\|_2$ with $h$ the final depth of `chs_tc4_ref_n032_dts100_m1` and $h_{half}$ that of `chs_tc4_ref_n032_dts050_m1`, `norm2(at('chs_tc4_ref_n032_dts050_m1').final.h - final.h) / norm2(final.h)` on the former (the member's quantity of that name).
- `errors.symmetry_h.l2_rel` $=\|h'-\mathrm{shift}(h,+\Delta i)\|_2/\|h\|_2$, and `errors.symmetry_hu.l2_rel`, `errors.symmetry_hv.l2_rel` $=\|q'-\mathrm{shift}(q,+\Delta i)\|_2/N_m$ for $q$ each momentum component, with $q'$ the final state of `chs_tc2_xpert_n064_dts100_m2_sx025`, $q$ that of its base, and $\mathrm{shift}(\cdot,+\Delta i)$ the cyclic shift by $\Delta i=\mathrm{shift\_x\_fraction}\cdot nx$ cells along `x` (`roll`).
- `run.first_call_refused`: the captured committed step count plus $\max\left(\max|h^{final}-h^{initial}|,\ \max|hu^{final}-hu^{initial}|,\ \max|hv^{final}-hv^{initial}|\right)$; `0` exactly when the first call refused (no step committed, the state as `__init` wrote it).

`cross_target_state_agreement` compares this target's numerical solution with every other target's certified variant of this node, case by case (`docs/TESTS.md` §Cross-target judgment): for each of `h`, `hu`, `hv` at $t_{end}$ of a case, $\max_{i,j}|q_{i,j}-q^{ref}_{i,j}|/N_q$, with $N_h=\max(\max|h|,\max|h^{ref}|,1)$ and the two momentum components sharing $N_{hu}=N_{hv}=\max(\max|hu|,\max|hv|,\max|hu^{ref}|,\max|hv^{ref}|,1)$. On a target with no other certified variant it holds vacuously.

### 5-5. Default thresholds
The bands of the cases with `substeps=1` and of the cases of 2-7 are the member's (the member's `tests.md` 5-5, `test_profile_version` `0.9.0`), calibrated against the member's independent reference implementation (issue #265's procedure; for the rectangular case issue #490; for the three norms issue #514). The bounds of this suite's own quantities are stated with the margins measured on the bench of 8-2 (2-5, 2-8 to 2-11). Reference values in parentheses are the member's.
- $\mathrm{cfl.max}\le 1.0$, `run.n_step` $=0$, `init_ok` $=1$, `init.n_step_plan` $\le 0.5$ (both operands are integers) and `init.dt` $\le 1.0e{-12}$, on the target cases of each test whose `quantities` line lists them.
- `extrema.h.min` $\ge 1.0e3$ for `tc2_zonal_uniform` and `tc2_zonal_perturbed`, $\ge 2.0e3$ for `tc3_compact_jet`, $\ge 9.0e3$ for `tc4_translating_low` (`m`).
- $\mathrm{mass\_drift\_rel}\le 1.0e{-10}$.
- `errors.initial_h.linf_rel`, `below_jet_rel`, `above_jet_rel`, `errors.initial_u.linf_rel`, `errors.initial_v.linf_rel` $\le 1.0e{-9}$; `errors.initial_vel_cross.linf` $\le 1.0e{-12}$ (`m/s`).
- `extrema.v_cross.max_abs` $\le 4.0$ on `chs_tc2_ref_n032_dts100_m1` and $\le 0.090$ on `chs_tc3_ref_n032_dts100_m1` (`m/s`).
- The steady-solution and manufactured-solution errors as two-sided bands, the lower and the upper bound two distinct judged quantities; with `substeps=1` the run is the member's run of the case, and with `substeps=2` the bands hold on the ground 2-7 states:

| case | variable | `l1` | `l2` | `linf` |
| --- | --- | --- | --- | --- |
| `chs_tc2_ref_n032_dts100_m1` | `h` | $[1.1e{-3},\ 3.4e{-3}]$ (`2.205e-3`) | $[2.4e{-3},\ 7.1e{-3}]$ (`4.739e-3`) | $[8.6e{-3},\ 2.7e{-2}]$ (`1.736e-2`) |
| | `hu` | $[1.3e{-2},\ 4.0e{-2}]$ (`2.665e-2`) | $[2.5e{-2},\ 7.8e{-2}]$ (`5.163e-2`) | $[6.9e{-2},\ 2.1e{-1}]$ (`1.392e-1`) |
| | `hv` | $[1.4e{-3},\ 4.4e{-3}]$ (`2.928e-3`) | $[4.4e{-3},\ 1.4e{-2}]$ (`8.802e-3`) | $[1.3e{-2},\ 4.2e{-2}]$ (`2.799e-2`) |
| `chs_tc3_ref_n032_dts100_m1` | `h` | $[1.6e{-3},\ 5.1e{-3}]$ (`3.395e-3`) | $[2.8e{-3},\ 8.5e{-3}]$ (`5.655e-3`) | $[6.7e{-3},\ 2.1e{-2}]$ (`1.360e-2`) |
| | `hu` | $[1.3e{-1},\ 4.2e{-1}]$ (`2.740e-1`) | $[1.1e{-1},\ 3.6e{-1}]$ (`2.367e-1`) | $[1.2e{-1},\ 3.7e{-1}]$ (`2.448e-1`) |
| | `hv` | $[1.5e{-3},\ 4.8e{-3}]$ (`3.187e-3`) | $[9.7e{-4},\ 3.0e{-3}]$ (`1.952e-3`) | $[7.7e{-4},\ 2.4e{-3}]$ (`1.540e-3`) |
| `chs_tc4_ref_n032_dts100_m1`, `chs_tc4_ref_n032_dts100_m2` | `h` | $[5.1e{-4},\ 1.6e{-3}]$ (`1.028e-3`) | $[1.1e{-3},\ 3.5e{-3}]$ (`2.294e-3`) | $[8.8e{-3},\ 2.7e{-2}]$ (`1.767e-2`) |
| | `hu` | $[2.9e{-2},\ 8.9e{-2}]$ (`5.869e-2`) | $[5.1e{-2},\ 1.6e{-1}]$ (`1.033e-1`) | $[1.3e{-1},\ 4.1e{-1}]$ (`2.685e-1`) |
| | `hv` | $[2.9e{-1},\ 9.0e{-1}]$ (`5.938e-1`) | $[2.7e{-1},\ 8.3e{-1}]$ (`5.521e-1`) | $[3.2e{-1},\ 9.7e{-1}]$ (`6.407e-1`) |
| `chs_tc4_ref_n064_dts100_m2` | `h` | $[1.1e{-4},\ 3.4e{-4}]$ (`2.264e-4`) | $[3.0e{-4},\ 9.2e{-4}]$ (`6.102e-4`) | $[2.6e{-3},\ 8.0e{-3}]$ (`5.292e-3`) |
| | `hu` | $[7.2e{-3},\ 2.2e{-2}]$ (`1.444e-2`) | $[1.4e{-2},\ 4.3e{-2}]$ (`2.857e-2`) | $[3.8e{-2},\ 1.2e{-1}]$ (`7.742e-2`) |
| | `hv` | $[7.8e{-2},\ 2.4e{-1}]$ (`1.569e-1`) | $[7.8e{-2},\ 2.4e{-1}]$ (`1.577e-1`) | $[1.0e{-1},\ 3.2e{-1}]$ (`2.130e-1`) |
| `chs_tc4_ref_n128_dts100_m2` | `h` | $[1.7e{-5},\ 5.3e{-5}]$ (`3.486e-5`) | $[4.6e{-5},\ 1.4e{-4}]$ (`9.311e-5`) | $[4.3e{-4},\ 1.4e{-3}]$ (`8.712e-4`) |
| | `hu` | $[1.2e{-3},\ 3.7e{-3}]$ (`2.401e-3`) | $[2.3e{-3},\ 7.2e{-3}]$ (`4.756e-3`) | $[6.1e{-3},\ 1.9e{-2}]$ (`1.240e-2`) |
| | `hv` | $[1.1e{-2},\ 3.6e{-2}]$ (`2.386e-2`) | $[1.2e{-2},\ 3.7e{-2}]$ (`2.409e-2`) | $[1.6e{-2},\ 5.1e{-2}]$ (`3.393e-2`) |
| `chs_tc4_ref_n064_dts100_m2_rect` | `h` | $[2.2e{-4},\ 6.9e{-4}]$ (`4.586e-4`) | $[4.7e{-4},\ 1.5e{-3}]$ (`9.591e-4`) | $[3.4e{-3},\ 1.1e{-2}]$ (`6.876e-3`) |
| | `hu` | $[1.7e{-2},\ 5.3e{-2}]$ (`3.502e-2`) | $[2.7e{-2},\ 8.2e{-2}]$ (`5.424e-2`) | $[6.1e{-2},\ 1.9e{-1}]$ (`1.239e-1`) |
| | `hv` | $[1.0e{-1},\ 3.2e{-1}]$ (`2.105e-1`) | $[9.2e{-2},\ 2.8e{-1}]$ (`1.853e-1`) | $[1.1e{-1},\ 3.5e{-1}]$ (`2.290e-1`) |

- The orders of 6-8 by the member's lower bounds of `chan_tc4_ref` (reference orders in parentheses; the bench reads each within `0.01` of it under `substeps=2`):

| pair | variable | `l1` | `l2` | `linf` |
| --- | --- | --- | --- | --- |
| `n032_to_n064` | `h` | $\ge 1.8$ (`2.18`) | $\ge 1.6$ (`1.91`) | $\ge 1.4$ (`1.74`) |
| | `hu` | $\ge 1.7$ (`2.02`) | $\ge 1.5$ (`1.85`) | $\ge 1.4$ (`1.79`) |
| | `hv` | $\ge 1.6$ (`1.92`) | $\ge 1.5$ (`1.81`) | $\ge 1.2$ (`1.59`) |
| `n064_to_n128` | `h` | $\ge 2.3$ (`2.6991`) | $\ge 2.4$ (`2.71`) | $\ge 2.3$ (`2.60`) |
| | `hu` | $\ge 2.2$ (`2.59`) | $\ge 2.2$ (`2.59`) | $\ge 2.3$ (`2.64`) |
| | `hv` | $\ge 2.4$ (`2.72`) | $\ge 2.4$ (`2.71`) | $\ge 2.3$ (`2.65`) |

- `xstructure.decay_ratio` as the two-sided band $[0.38,\ 0.95]$ at `nx=64` (the member's reference `0.7666`; the bench reads `0.76662` under `substeps=2`), the lower and the upper bound two distinct judged quantities.
- `pair.n_step_premise` $\le 0.5$ (both operands are integers); `pair.<v>` $\le 0.0$ on 6-4 and $\le 1.0e{-12}$ on 6-5 and 6-6 (2-5).
- `errors.time_pair.h_l2_rel` as the two-sided band $[2.482e{-8},\ 2.488e{-8}]$ (the member's; reference `2.4847e-8`), the lower and the upper bound two distinct judged quantities.
- Every symmetry-pair residual `errors.axis_exchange.*`, `errors.mirror_x.*`, `errors.mirror_y.*` and `errors.symmetry_*` $\le 2.0e{-11}$ (2-10).
- `errors.repeat.*` $\le 1.0e{-12}$ (2-9); `restart.<v>` $\le 0.0$ (2-8).
- `ok` $=0$ on the refusal cases of 6-16 and 6-18; `run.first_call_refused` $=0$ on 6-16; `init_ok` $=0$ on 6-17; `run.state_moved` $>0$ on 6-18.
- $\mathrm{cross\_target\_state\_agreement}\le 1.0e{-10}$ for each of `h`, `hu` and `hv`.

## 6. Test definitions
### 6-1. `l1_tc2_substeps1_restates_member`
- `target_cases`: `chs_tc2_ref_n032_dts100_m1`
- `level`: `L1`
- `judgment`: the member's judgments on its case `chan_tc2_ref_n032_dts100`, with `substeps=1` (2-6): the initial state against the closed form of 2-3, the steady-solution errors of `h`, `hu`, `hv` in three norms as the member's bands at `nx=32`, the wall-normal velocity, mass, the smallest depth, `cfl.max`, `run.n_step`, `init_ok`, `init.n_step_plan`, `init.dt` (5-5). The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, `hv`, and the threshold is $\le 1.0e{-10}$.
- `expected_outcome`: `pass`
- `quantities`: `cfl.max`, `errors.initial_h.linf_rel`, `errors.initial_u.linf_rel`, `errors.initial_v.linf_rel`, `errors.initial_vel_cross.linf`, `errors.steady_h.l1_rel_tend.lower`, `errors.steady_h.l1_rel_tend.upper`, `errors.steady_h.l2_rel_tend.lower`, `errors.steady_h.l2_rel_tend.upper`, `errors.steady_h.linf_rel_tend.lower`, `errors.steady_h.linf_rel_tend.upper`, `errors.steady_hu.l1_rel_tend.lower`, `errors.steady_hu.l1_rel_tend.upper`, `errors.steady_hu.l2_rel_tend.lower`, `errors.steady_hu.l2_rel_tend.upper`, `errors.steady_hu.linf_rel_tend.lower`, `errors.steady_hu.linf_rel_tend.upper`, `errors.steady_hv.l1_rel_tend.lower`, `errors.steady_hv.l1_rel_tend.upper`, `errors.steady_hv.l2_rel_tend.lower`, `errors.steady_hv.l2_rel_tend.upper`, `errors.steady_hv.linf_rel_tend.lower`, `errors.steady_hv.linf_rel_tend.upper`, `extrema.h.min`, `extrema.v_cross.max_abs`, `init.dt`, `init.n_step_plan`, `init_ok`, `metrics.mass_drift_rel`, `run.n_step`

### 6-2. `l1_tc3_substeps1_restates_member`
- `target_cases`: `chs_tc3_ref_n032_dts100_m1`
- `level`: `L1`
- `judgment`: the member's judgments on its case `chan_tc3_ref_n032_dts100`, with `substeps=1` (2-6): the initial depth before and beyond the jet and the initial velocity, the steady-solution errors in three norms as the member's bands at `nx=32`, the wall-normal velocity, mass, the smallest depth, `cfl.max`, `run.n_step`, `init_ok`, `init.n_step_plan`, `init.dt` (5-5). The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, `hv`, and the threshold is $\le 1.0e{-10}$.
- `expected_outcome`: `pass`
- `quantities`: `cfl.max`, `errors.initial_h.above_jet_rel`, `errors.initial_h.below_jet_rel`, `errors.initial_u.linf_rel`, `errors.initial_v.linf_rel`, `errors.initial_vel_cross.linf`, `errors.steady_h.l1_rel_tend.lower`, `errors.steady_h.l1_rel_tend.upper`, `errors.steady_h.l2_rel_tend.lower`, `errors.steady_h.l2_rel_tend.upper`, `errors.steady_h.linf_rel_tend.lower`, `errors.steady_h.linf_rel_tend.upper`, `errors.steady_hu.l1_rel_tend.lower`, `errors.steady_hu.l1_rel_tend.upper`, `errors.steady_hu.l2_rel_tend.lower`, `errors.steady_hu.l2_rel_tend.upper`, `errors.steady_hu.linf_rel_tend.lower`, `errors.steady_hu.linf_rel_tend.upper`, `errors.steady_hv.l1_rel_tend.lower`, `errors.steady_hv.l1_rel_tend.upper`, `errors.steady_hv.l2_rel_tend.lower`, `errors.steady_hv.l2_rel_tend.upper`, `errors.steady_hv.linf_rel_tend.lower`, `errors.steady_hv.linf_rel_tend.upper`, `extrema.h.min`, `extrema.v_cross.max_abs`, `init.dt`, `init.n_step_plan`, `init_ok`, `metrics.mass_drift_rel`, `run.n_step`

### 6-3. `l1_tc4_substeps1_restates_member`
- `target_cases`: `chs_tc4_ref_n032_dts100_m1`
- `level`: `L1`
- `judgment`: the member's judgments on its case `chan_tc4_ref_n032_dts100`, with `substeps=1` (2-6): the initial state against the prescribed flow at $t=0$, the errors against the prescribed flow at $t_{end}$ in three norms as the member's bands at `nx=32`, mass, the smallest depth, `cfl.max`, `run.n_step`, `init_ok`, `init.n_step_plan`, `init.dt` (5-5). The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, `hv`, and the threshold is $\le 1.0e{-10}$.
- `expected_outcome`: `pass`
- `quantities`: `cfl.max`, `errors.initial_h.linf_rel`, `errors.initial_u.linf_rel`, `errors.initial_v.linf_rel`, `errors.mms_h.l1_rel_tend.lower`, `errors.mms_h.l1_rel_tend.upper`, `errors.mms_h.l2_rel_tend.lower`, `errors.mms_h.l2_rel_tend.upper`, `errors.mms_h.linf_rel_tend.lower`, `errors.mms_h.linf_rel_tend.upper`, `errors.mms_hu.l1_rel_tend.lower`, `errors.mms_hu.l1_rel_tend.upper`, `errors.mms_hu.l2_rel_tend.lower`, `errors.mms_hu.l2_rel_tend.upper`, `errors.mms_hu.linf_rel_tend.lower`, `errors.mms_hu.linf_rel_tend.upper`, `errors.mms_hv.l1_rel_tend.lower`, `errors.mms_hv.l1_rel_tend.upper`, `errors.mms_hv.l2_rel_tend.lower`, `errors.mms_hv.l2_rel_tend.upper`, `errors.mms_hv.linf_rel_tend.lower`, `errors.mms_hv.linf_rel_tend.upper`, `extrema.h.min`, `init.dt`, `init.n_step_plan`, `init_ok`, `metrics.mass_drift_rel`, `run.n_step`

### 6-4. `l3_substeps_pair_tc2`
- `target_cases`: `chs_tc2_ref_n032_dts050_m1`, `chs_tc2_ref_n032_dts100_m2`
- `level`: `L3`
- `judgment`: the sub-cycling pair of `tc2_zonal_uniform` (2-5): the premise `pair.n_step_premise` $\le 0.5$ and `pair.h`, `pair.hu`, `pair.hv` $\le 0.0$ (equal exactly), carried by `chs_tc2_ref_n032_dts100_m2`; mass, the smallest depth, `cfl.max`, `run.n_step`, `init_ok`, `init.n_step_plan`, `init.dt` per case. The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, `hv`, and the threshold is $\le 1.0e{-10}$.
- `expected_outcome`: `pass`
- `quantities`: `cfl.max`, `extrema.h.min`, `init.dt`, `init.n_step_plan`, `init_ok`, `metrics.mass_drift_rel`, `pair.h`, `pair.hu`, `pair.hv`, `pair.n_step_premise`, `run.n_step`

### 6-5. `l3_substeps_pair_tc4`
- `target_cases`: `chs_tc4_ref_n032_dts050_m1`, `chs_tc4_ref_n032_dts100_m2`
- `level`: `L3`
- `judgment`: the sub-cycling pair of `tc4_translating_low` (2-5): the premise `pair.n_step_premise` $\le 0.5$ and `pair.h`, `pair.hu`, `pair.hv` $\le 1.0e{-12}$ (relative), carried by `chs_tc4_ref_n032_dts100_m2`; mass, the smallest depth, `cfl.max`, `run.n_step`, `init_ok`, `init.n_step_plan`, `init.dt` per case. The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, `hv`, and the threshold is $\le 1.0e{-10}$.
- `expected_outcome`: `pass`
- `quantities`: `cfl.max`, `extrema.h.min`, `init.dt`, `init.n_step_plan`, `init_ok`, `metrics.mass_drift_rel`, `pair.h`, `pair.hu`, `pair.hv`, `pair.n_step_premise`, `run.n_step`

### 6-6. `l3_substeps_pair_tc4_m4`
- `target_cases`: `chs_tc4_ref_n032_dts025_m1`, `chs_tc4_ref_n032_dts100_m4`
- `level`: `L3`
- `judgment`: the sub-cycling pair of `tc4_translating_low` with `substeps=4` (2-5): the premise `pair.n_step_premise` $\le 0.5$ and `pair.h`, `pair.hu`, `pair.hv` $\le 1.0e{-12}$ (relative), carried by `chs_tc4_ref_n032_dts100_m4`; mass, the smallest depth, `cfl.max`, `run.n_step`, `init_ok`, `init.n_step_plan`, `init.dt` per case. The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, `hv`, and the threshold is $\le 1.0e{-10}$.
- `expected_outcome`: `pass`
- `quantities`: `cfl.max`, `extrema.h.min`, `init.dt`, `init.n_step_plan`, `init_ok`, `metrics.mass_drift_rel`, `pair.h`, `pair.hu`, `pair.hv`, `pair.n_step_premise`, `run.n_step`

### 6-7. `l3_tc4_time_refinement_pair_substeps1`
- `target_cases`: `chs_tc4_ref_n032_dts050_m1`, `chs_tc4_ref_n032_dts100_m1`
- `level`: `L3`
- `judgment`: the member's time-refinement pair with `substeps=1` (2-5): `errors.time_pair.h_l2_rel` in the band $[2.482e{-8},\ 2.488e{-8}]$, its lower and upper bound two distinct judged quantities, carried by `chs_tc4_ref_n032_dts100_m1`; mass, the smallest depth, `cfl.max`, `run.n_step`, `init_ok`, `init.n_step_plan`, `init.dt` per case. The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, `hv`, and the threshold is $\le 1.0e{-10}$.
- `expected_outcome`: `pass`
- `quantities`: `cfl.max`, `errors.time_pair.h_l2_rel.lower`, `errors.time_pair.h_l2_rel.upper`, `extrema.h.min`, `init.dt`, `init.n_step_plan`, `init_ok`, `metrics.mass_drift_rel`, `run.n_step`

### 6-8. `l1_tc4_refinement_substeps2`
- `target_cases`: `chs_tc4_ref_n032_dts100_m2`, `chs_tc4_ref_n064_dts100_m2`, `chs_tc4_ref_n128_dts100_m2`
- `level`: `L1`
- `judgment`: the member's refinement judgment of `tc4_translating_low` with `substeps=2` (2-7): the errors against the prescribed flow at $t_{end}$ of `h`, `hu`, `hv` in three norms per level as the member's bands, and their orders over the two pairs of adjacent levels by the member's lower bounds, carried by the finer case of each pair; the initial state, mass, the smallest depth, `cfl.max`, `run.n_step`, `init_ok`, `init.n_step_plan`, `init.dt` per case. The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, `hv`, and the threshold is $\le 1.0e{-10}$.
- `expected_outcome`: `pass`
- `quantities`: `cfl.max`, `convergence.n032_to_n064.hu_l1_order`, `convergence.n032_to_n064.hu_l2_order`, `convergence.n032_to_n064.hu_linf_order`, `convergence.n032_to_n064.hv_l1_order`, `convergence.n032_to_n064.hv_l2_order`, `convergence.n032_to_n064.hv_linf_order`, `convergence.n032_to_n064.l1_order`, `convergence.n032_to_n064.l2_order`, `convergence.n032_to_n064.linf_order`, `convergence.n064_to_n128.hu_l1_order`, `convergence.n064_to_n128.hu_l2_order`, `convergence.n064_to_n128.hu_linf_order`, `convergence.n064_to_n128.hv_l1_order`, `convergence.n064_to_n128.hv_l2_order`, `convergence.n064_to_n128.hv_linf_order`, `convergence.n064_to_n128.l1_order`, `convergence.n064_to_n128.l2_order`, `convergence.n064_to_n128.linf_order`, `errors.initial_h.linf_rel`, `errors.initial_u.linf_rel`, `errors.initial_v.linf_rel`, `errors.mms_h.l1_rel_tend.lower`, `errors.mms_h.l1_rel_tend.upper`, `errors.mms_h.l2_rel_tend.lower`, `errors.mms_h.l2_rel_tend.upper`, `errors.mms_h.linf_rel_tend.lower`, `errors.mms_h.linf_rel_tend.upper`, `errors.mms_hu.l1_rel_tend.lower`, `errors.mms_hu.l1_rel_tend.upper`, `errors.mms_hu.l2_rel_tend.lower`, `errors.mms_hu.l2_rel_tend.upper`, `errors.mms_hu.linf_rel_tend.lower`, `errors.mms_hu.linf_rel_tend.upper`, `errors.mms_hv.l1_rel_tend.lower`, `errors.mms_hv.l1_rel_tend.upper`, `errors.mms_hv.l2_rel_tend.lower`, `errors.mms_hv.l2_rel_tend.upper`, `errors.mms_hv.linf_rel_tend.lower`, `errors.mms_hv.linf_rel_tend.upper`, `extrema.h.min`, `init.dt`, `init.n_step_plan`, `init_ok`, `metrics.mass_drift_rel`, `run.n_step`

### 6-9. `l1_tc4_rectangular_grid_substeps2`
- `target_cases`: `chs_tc4_ref_n064_dts100_m2_rect`
- `level`: `L1`
- `judgment`: the member's judgments on its rectangular case with `substeps=2` (2-7): the initial state, the errors against the prescribed flow in three norms as the member's band, mass, the smallest depth, `cfl.max`, `run.n_step`, `init_ok`, `init.n_step_plan`, `init.dt`. The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, `hv`, and the threshold is $\le 1.0e{-10}$.
- `expected_outcome`: `pass`
- `quantities`: `cfl.max`, `errors.initial_h.linf_rel`, `errors.initial_u.linf_rel`, `errors.initial_v.linf_rel`, `errors.mms_h.l1_rel_tend.lower`, `errors.mms_h.l1_rel_tend.upper`, `errors.mms_h.l2_rel_tend.lower`, `errors.mms_h.l2_rel_tend.upper`, `errors.mms_h.linf_rel_tend.lower`, `errors.mms_h.linf_rel_tend.upper`, `errors.mms_hu.l1_rel_tend.lower`, `errors.mms_hu.l1_rel_tend.upper`, `errors.mms_hu.l2_rel_tend.lower`, `errors.mms_hu.l2_rel_tend.upper`, `errors.mms_hu.linf_rel_tend.lower`, `errors.mms_hu.linf_rel_tend.upper`, `errors.mms_hv.l1_rel_tend.lower`, `errors.mms_hv.l1_rel_tend.upper`, `errors.mms_hv.l2_rel_tend.lower`, `errors.mms_hv.l2_rel_tend.upper`, `errors.mms_hv.linf_rel_tend.lower`, `errors.mms_hv.linf_rel_tend.upper`, `extrema.h.min`, `init.dt`, `init.n_step_plan`, `init_ok`, `metrics.mass_drift_rel`, `run.n_step`

### 6-10. `l3_axis_exchange_pair`
- `target_cases`: `chs_tc4_ref_n064_dts100_m2_rect`, `chs_tc4_ref_n064_dts100_m2_rectaxis`
- `level`: `L3`
- `judgment`: the axis-exchange residual of 5-4 for `h`, `hu`, `hv` $\le 2.0e{-11}$ (2-10); the initial state, mass, the smallest depth, `cfl.max`, `run.n_step`, `init_ok`, `init.n_step_plan`, `init.dt` on the transformed case (the base's are judged by the test of its family, 6-8 or 6-9). The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, `hv`, and the threshold is $\le 1.0e{-10}$.
- `expected_outcome`: `pass`
- `quantities`: `cfl.max`, `errors.axis_exchange.h_l2_rel`, `errors.axis_exchange.hu_l2_rel`, `errors.axis_exchange.hv_l2_rel`, `errors.initial_h.linf_rel`, `errors.initial_u.linf_rel`, `errors.initial_v.linf_rel`, `extrema.h.min`, `init.dt`, `init.n_step_plan`, `init_ok`, `metrics.mass_drift_rel`, `run.n_step`

### 6-11. `l3_along_channel_mirror_pair`
- `target_cases`: `chs_tc4_ref_n064_dts100_m2`, `chs_tc4_ref_n064_dts100_m2_xmir`
- `level`: `L3`
- `judgment`: the mirror-along-the-channel residual of 5-4 for `h`, `hu`, `hv` $\le 2.0e{-11}$ (2-10); the initial state, mass, the smallest depth, `cfl.max`, `run.n_step`, `init_ok`, `init.n_step_plan`, `init.dt` on the transformed case (the base's are judged by the test of its family, 6-8 or 6-9). The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, `hv`, and the threshold is $\le 1.0e{-10}$.
- `expected_outcome`: `pass`
- `quantities`: `cfl.max`, `errors.initial_h.linf_rel`, `errors.initial_u.linf_rel`, `errors.initial_v.linf_rel`, `errors.mirror_x.h_l2_rel`, `errors.mirror_x.hu_l2_rel`, `errors.mirror_x.hv_l2_rel`, `extrema.h.min`, `init.dt`, `init.n_step_plan`, `init_ok`, `metrics.mass_drift_rel`, `run.n_step`

### 6-12. `l3_cross_channel_mirror_pair`
- `target_cases`: `chs_tc4_ref_n064_dts100_m2`, `chs_tc4_ref_n064_dts100_m2_ymir`
- `level`: `L3`
- `judgment`: the mirror-across-the-channel residual of 5-4 for `h`, `hu`, `hv` $\le 2.0e{-11}$ (2-10); the initial state, mass, the smallest depth, `cfl.max`, `run.n_step`, `init_ok`, `init.n_step_plan`, `init.dt` on the transformed case (the base's are judged by the test of its family, 6-8 or 6-9). The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, `hv`, and the threshold is $\le 1.0e{-10}$.
- `expected_outcome`: `pass`
- `quantities`: `cfl.max`, `errors.initial_h.linf_rel`, `errors.initial_u.linf_rel`, `errors.initial_v.linf_rel`, `errors.mirror_y.h_l2_rel`, `errors.mirror_y.hu_l2_rel`, `errors.mirror_y.hv_l2_rel`, `extrema.h.min`, `init.dt`, `init.n_step_plan`, `init_ok`, `metrics.mass_drift_rel`, `run.n_step`

### 6-13. `l3_x_translation_pair`
- `target_cases`: `chs_tc2_xpert_n064_dts100_m2`, `chs_tc2_xpert_n064_dts100_m2_sx025`
- `level`: `L3`
- `judgment`: the translation residual of 5-4 for `h`, `hu`, `hv` $\le 2.0e{-11}$, carried by the shifted case, and the decay of the along-channel structure as the member's band per case (2-10); the initial state, mass, the smallest depth, `cfl.max`, `run.n_step`, `init_ok`, `init.n_step_plan`, `init.dt` per case. The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, `hv`, and the threshold is $\le 1.0e{-10}$.
- `expected_outcome`: `pass`
- `quantities`: `cfl.max`, `errors.initial_h.linf_rel`, `errors.initial_u.linf_rel`, `errors.initial_v.linf_rel`, `errors.initial_vel_cross.linf`, `errors.symmetry_h.l2_rel`, `errors.symmetry_hu.l2_rel`, `errors.symmetry_hv.l2_rel`, `extrema.h.min`, `init.dt`, `init.n_step_plan`, `init_ok`, `metrics.mass_drift_rel`, `run.n_step`, `xstructure.decay_ratio.lower`, `xstructure.decay_ratio.upper`

### 6-14. `l3_restart_equivalence`
- `target_cases`: `chs_tc4_ref_n032_dts100_m2`, `chs_tc4_ref_n032_dts100_m2_cont`
- `level`: `L3`
- `judgment`: the run continued from the state sample at committed step `272`, in a process of its own, ends in the state of the uninterrupted run (2-8): `restart.h`, `restart.hu`, `restart.hv` $\le 0$ (equal exactly); `run.n_step`, `init_ok`. The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, `hv`, and the threshold is $\le 1.0e{-10}$.
- `expected_outcome`: `pass`
- `quantities`: `init_ok`, `restart.h`, `restart.hu`, `restart.hv`, `run.n_step`

### 6-15. `l3_case_independence_repeat`
- `target_cases`: `chs_tc2_ref_n032_dts100_m2`, `chs_zrepeat_n032_dts100_m2`
- `level`: `L3`
- `judgment`: the repeated case's final state equals its original's (2-9): `errors.repeat.h_l2_rel`, `errors.repeat.hu_l2_rel`, `errors.repeat.hv_l2_rel` $\le 1.0e{-12}$; `run.n_step`, `init_ok`, `init.n_step_plan`, `init.dt` per case. The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, `hv`, and the threshold is $\le 1.0e{-10}$.
- `expected_outcome`: `pass`
- `quantities`: `errors.repeat.h_l2_rel`, `errors.repeat.hu_l2_rel`, `errors.repeat.hv_l2_rel`, `init.dt`, `init.n_step_plan`, `init_ok`, `run.n_step`

### 6-16. `l0_substeps_refusal_xfail`
- `target_cases`: `chs_guard_n032_dts100_m0`, `chs_guard_n032_dts100_mneg1`
- `level`: `L0`
- `xfail_condition`: `substeps < 1` (at `0` and at `-1`)
- `judgment`: the first call refuses: `ok` $=0$ and `run.first_call_refused` $=0$ (2-11). The cross-target judgment is not applied.
- `expected_outcome`: `xfail`
- `quantities`: `ok`, `run.first_call_refused`

### 6-17. `l0_init_refusal_xfail`
- `target_cases`: `chs_tc2_ref_n032_dts100_m2_badprofile`
- `level`: `L0`
- `xfail_condition`: the member's `__init` refuses the case (`flow_profile_id=0`)
- `judgment`: the case is refused at setup: `init_ok` $=0$ (2-11). The cross-target judgment is not applied.
- `expected_outcome`: `xfail`
- `quantities`: `init_ok`

### 6-18. `l0_wet_domain_refusal_xfail`
- `target_cases`: `chs_dry_n032_dts1600_m2`
- `level`: `L0`
- `xfail_condition`: the member's wet-domain invariant fails mid-run at the second sub-step of the sixth step, with `substeps=2` (2-11)
- `judgment`: the run ends on the member's refusal: `ok` $=0$, and `run.state_moved` $>0$ (steps were committed before it). The cross-target judgment is not applied.
- `expected_outcome`: `xfail`
- `quantities`: `ok`, `run.state_moved`

## 7. Pass/fail aggregation rules
`self_verdict` is `pass` when every `pass` test passes and every `xfail` test fails as expected; any other outcome is `fail`. A `pass` test passes when every predicate of every quantity it lists holds on every target case it is evaluated on.

## 8. Traceability
### 8-1. Output requirements
The host records the captures of §3 per case, `test_profile_id` and `test_profile_version` in `trial_meta.json`, and the per-test results in `verdict.json`.

### 8-2. Traceability records
The seeded composition defects the suite was required to catch before any billed run, with what caught each, are reproduced by `tools/tests/data/drive_ir/subcycle_evidence/bench.py` ([issue #632](https://github.com/seiya/spec2science/issues/632) P4): a hand-written composing model over the hand-written member model of `tools/tests/data/drive_ir/pr3_evidence/models/`, built with the certified sources of the member's components. The values this document attributes to "the bench of 8-2" are its readings.

The bands restated from the member hold for the member's scheme and suite as `shallow_water2d_channel` `0.4.1` and its `tests.md` `0.9.0` state them. When the member's new version changes its scheme or its bands, re-take the restated bands from the member's new `tests.md`, re-run the bench, and release this `spec` under a new `spec_version`. A member version that keeps its scheme moves no band.
