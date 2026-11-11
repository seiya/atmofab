# Tests: 2D rotating shallow water, an assembled model (verification input / judgment conditions)

## 0. Meta information
- `status`: `draft`
- `test_profile_id`: `shallow_water2d_rotating_baseline`
- `test_profile_version`: `0.1.0`
- `spec_ref.spec_kind`: `problem`
- `spec_ref.spec_id`: `shallow_water2d_rotating`
- `spec_ref.spec_version`: `0.1.0`
- `spec_ref.controlled_spec_path`: `spec/problem/dynamics/shallow_water/shallow_water2d_rotating/controlled_spec.md`

## 1. Test purpose
This suite verifies the COMPOSITION of the Controlled Spec §5: that the member `problem` `shallow_water2d` is called once per step with `dt` and its output is the state, that the rotation process fires on the steps and with the time step §5 states, that it rotates with this call's `f` cell by cell, that the order is the member first, that the selector switches the process off entirely, and that a refusal of the member is this step's refusal. The member's own discretization is the member's to certify; this suite judges it only where the composition could alter it (6-4, 6-5), restating the member's own bands on cases equal to the member's input for input.

It judges: the rotation alone against the closed form of the RK4 polynomial, at the final state and at every sample, for an interval of `1` and of `4` and a step count whose remainder by `4` is `3` (6-1); a coupled judgment whose value depends on both the member and the rotation and is not a band on the splitting error, the frequency of an inertia-gravity wave on an `f`-plane (6-2); mass over every sample (6-3); the process off, on the member's cone case and its flat linear-wave case, restating the member's bands, and the independence of the result from `f` and `rotation.every` when off (6-4, 6-5); the splitting order and the per-cell `f`, by two-sided bands calibrated against an independent reference, on a square and a rectangular grid (6-6); the four symmetry pairs of `docs/TESTS.md` with `f` transformed (6-7 to 6-10); the refinement judgment against the restricted reference (6-11); and the guards: the interval at `0` and below, the selector outside `{0, 1}`, the member refusing mid-run with the rotation firing on every step, and the member's `dt` refusal with the rotation on (6-12 to 6-15).

The conditions the composition judgments are built under (the system tier plan, Part 1, §4.1):
- **Rotation on, non-flat, non-uniform `f`.** 6-3, 6-6 to 6-11 run with the rotation on, over the shear-flow state of 2-3, whose momentum is non-zero and varies along both axes, under the `f` of 2-4, which varies along both axes and is periodic on the doubly periodic domain. A flat state at rest makes the member the identity, and a kernel that skips the member whenever the rotation is on then passes; an `f` that varies along one axis only leaves a mutant reading one cell of `f` visible to one symmetry pair only.
- **The interval at `2` with a step count it does not divide.** 6-6 runs with `rotation.every = 2` and `n_step` odd (`183` and `229`). At an interval of `1`, or on a flat state, a member stepped with `k dt`, a member called on rotation steps only and a member called `k` times are each indistinguishable from the stated composition.
- **Cases of one shape with different `f`.** The four transformed cases of 2-11 have the shape of their base and a different `f`; a kernel that retains `f` from an earlier case of the same shape is seen by the pairs. The condition is that these cases share a shape, not that they are adjacent in any order.
- **A rectangular case** (`nx != ny`, `L_x != L_y`) under 6-6: an `f`, or a packed state, indexed under exchanged extents reads the right cells on a square grid.
- **The member refusing on a step where the rotation fires.** 6-14 runs with `rotation = 1` and `rotation.every = 1`; the member refuses at its tenth step of eighteen (measured on the PR-S2 bench, the certified member composed with a hand-written composing model; the refusing step is recorded, not judged), so the rotation has fired on every committed step before it.
- **Margins stated before the judgments were run**, each beside its band in 5-5: the interval pair's closed forms, the two phase shifts of 6-2, and the one-off difference of the two splitting orders of 6-6.

What this suite accepts as beyond a kernel judgment:
- **The guards of the Coriolis operation and of RK4.** Neither is reachable from this `problem`'s inputs for a conforming member: the Coriolis operation refuses `ncomp /= 3`, `nx < 1`, `ny < 1`, and RK4 refuses `dt <= 0` and extents below `1`; `ncomp` is fixed to `3` by the Controlled Spec §5, `rotation.every < 1` is refused by step 1, and the member, called first with the same `nx`, `ny` and `dt`, refuses `nx < 2`, `ny < 2` and `dt <= 0` (its §5 and its 6-15, 6-16). So a kernel that discards the Coriolis guard, or ignores the RK4 guard, passes every test here. The Controlled Spec §5 still requires both; that requirement rests on `Generate.verify`, and 6-15 runs the premise of this argument: the member's `dt < 0` refusal with the rotation on, judged by the flag.
- **The member's discretization.** It is the member's suite's to judge. This suite restates the member's bands only on the member's own cases with the process off (6-4, 6-5), and calibrates every band of the rotating cases on a reference that reproduces the member.
- **The member's private state.** The member publishes `__step` only and keeps no state between calls that its §5 states; how often the member is called per step is judged through the state (6-6, 6-11), not by counting calls.

## 2. Input-defaulting rules
### 2-1. Basic constants
- The domain lengths are `L_x=L_y=1.0`, except the rectangular case of 2-9 (`L_y=0.5`) and the wave case of 2-5 (`L_y=1/32`).
- The representative water level is `H_0=1.0` and the gravitational acceleration is `g=9.81`, the member's.
- The bottom topography is `topography_profile=flat` (`z_b=0`) on every case, except the two cases of 2-7 and the wet-domain refusal case of 2-8, which take the member's `williamson_tc5_cone` with `x_c_fraction=3/4`, `y_c_fraction=2/3`.

### 2-2. Initial-condition profiles
`initial_profile` takes one of the values below (each a closed form over the inputs, evaluated by the host at the cell centres $x_i=(i+1/2)\,dx$, $y_j=(j+1/2)\,dy$):
- `uniform_flow` (2-6): $h=H_0$, $hu=\mathrm{mom\_x0}$, $hv=\mathrm{mom\_y0}$.
- `shear_flow` (2-3).
- `poincare_wave_x` (2-5).
- `linear_wave_x`: the member's profile of the same name (the member's `tests.md` 2-3), with its inputs `eta0`, `wave_sign`, `shift_x_fraction`, `shift_y_fraction`; used by the member's cases of 2-7, 2-8 and 2-10 only.

### 2-3. The shear-flow state
$$
h=H_0+\eta_x\sin\left(2\pi\left(\frac{x}{L_x}-p^{\eta}_x\right)\right)+\eta_y\sin\left(2\pi\left(\frac{y}{L_y}-p^{\eta}_y\right)\right)
$$
$$
hu=m_{x0}+m_{x1}\sin\left(2\pi\left(\frac{y}{L_y}-p^{m}_y\right)\right),\qquad
hv=m_{y0}+m_{y1}\sin\left(2\pi\left(\frac{x}{L_x}-p^{m}_x\right)\right)
$$
with the inputs `eta_x` $=\eta_x$, `eta_phase_x` $=p^{\eta}_x$, `eta_y`, `eta_phase_y`, `mom_x0` $=m_{x0}$, `mom_x1`, `mom_phase_y` $=p^m_y$, `mom_y0`, `mom_y1`, `mom_phase_x` $=p^m_x$. The base values are `eta_x=0.01`, `eta_phase_x=0.1`, `eta_y=0.006`, `eta_phase_y=0.3`, `mom_x0=0.05`, `mom_x1=0.04`, `mom_phase_y=0.4`, `mom_y0=-0.03`, `mom_y1=0.05`, `mom_phase_x=0.15`. The momentum is non-zero and varies along both axes, and the depth is non-flat.

### 2-4. The Coriolis parameter
The rotation inputs of every case form the namespace `rotation`: the selector `rotation.on` and the interval `rotation.every`, which the driver passes as the arguments `rotation` and `rotation_every` of `shallow_water2d_rotating__step`, and the parameters of `f` below. On every case,
$$
f=f_0+a_x\sin\left(2\pi\left(\frac{x}{L_x}-p^f_x\right)\right)+a_y\sin\left(2\pi\left(\frac{y}{L_y}-p^f_y\right)\right)
$$
with the inputs `rotation.f0` $=f_0$, `rotation.f_amp_x` $=a_x$, `rotation.f_phase_x` $=p^f_x$, `rotation.f_amp_y`, `rotation.f_phase_y`, evaluated by the host per cell and handed to the driver with the initial state. The shear-flow base takes `f0=20`, `f_amp_x=10`, `f_phase_x=0.2`, `f_amp_y=8`, `f_phase_y=0.05` with `rotation.on=1`, `rotation.every=2`. The field is periodic along both axes, and its form is closed under each transformation of 2-11 with its inputs transformed.

### 2-5. The inertia-gravity wave on an `f`-plane
`initial_profile=poincare_wave_x`, `eta0=1.0e-3`, on `nx=128`, `ny=4`, `L_x=1`, `L_y=1/32` (so $dx=dy$), `f0=20` with the amplitudes and the phases `0` (`f_amp_x=f_amp_y=f_phase_x=f_phase_y=0`), `rotation.on=1`, `rotation.every=1`. With $\kappa=2\pi/L_x$ and $\omega=\sqrt{f_0^2+gH_0\kappa^2}$, the linearised rotating shallow water equations about rest at depth $H_0$ have the solution $h-H_0=\eta_0\cos(\kappa x-\omega t)$, $hu=(\omega/\kappa)\,\eta_0\cos(\kappa x-\omega t)$, $hv=(f_0/\kappa)\,\eta_0\sin(\kappa x-\omega t)$, and the initial state is that solution at $t=0$:
$$
h=H_0+\eta_0\cos(\kappa x),\quad hu=\frac{\omega}{\kappa}\eta_0\cos(\kappa x),\quad hv=\frac{f_0}{\kappa}\eta_0\sin(\kappa x)
$$
The frequency depends on the member (through $gH_0\kappa^2$) and on the rotation (through $f_0^2$): with $f_0=20$ the two terms are about equal ($f_0^2=400$, $gH_0\kappa^2=387.3$), so a kernel that drops either call is off the frequency by a comparable amount (5-5).

### 2-6. The rotation alone
`initial_profile=uniform_flow`, `mom_x0=0.1`, `mom_y0=0.05`, `nx=ny=16`, `f0=50` with the amplitudes and the phases `0` (`f_amp_x=f_amp_y=f_phase_x=f_phase_y=0`), `rotation.on=1`. The member leaves a uniform state on a flat bottom unchanged, so the state is the rotation's alone: after $m$ rotation calls,
$$
hu+\mathrm{i}\,hv=(\mathrm{mom\_x0}+\mathrm{i}\,\mathrm{mom\_y0})\,P(-\mathrm{i}\theta)^m,\qquad \theta=f_0\,k\,\Delta t,\quad P(z)=1+z+\frac{z^2}{2}+\frac{z^3}{6}+\frac{z^4}{24}
$$
with $k$ = `rotation.every` and $m=\lfloor n/k\rfloor$ after $n$ member steps (Controlled Spec §5). Writing $P(-\mathrm{i}\theta)=a+\mathrm{i}b$, $a=1-\theta^2/2+\theta^4/24$, $b=-\theta+\theta^3/6$, $\rho=\sqrt{a^2+b^2}$, $\psi=\mathrm{atan2}(b,a)$:
$$
hu=\rho^m\left(m_{x0}\cos m\psi-m_{y0}\sin m\psi\right),\qquad hv=\rho^m\left(m_{x0}\sin m\psi+m_{y0}\cos m\psi\right)
$$
The three cases: `rotation.every=1` and `4` with `t_end=0.2` ($\mathrm{n\_step}=46$), and `rotation.every=4` with `t_end=0.205` ($\mathrm{n\_step}=47$, remainder `3` by `4`). With remainder `k-1` the firing rule of §5 is the only one of the `k` phases that gives $\lfloor n/k\rfloor$ calls; with remainder `1` three of the four phases give the same count.

### 2-7. The process off on the member's cone case
The member's case `swe2d_ref_n064_sx000_sy000_dts100`, input for input (`nx=ny=64`, `williamson_tc5_cone`, `linear_wave_x`, `eta0=1.0e-3`, `wave_sign=+1`, the shifts `0`, `dt_scale=1.0`, `t_end=0.2`, `history.every=90`), with `rotation.on=0`, `rotation.every=3` and the `f` of the shear-flow base (`f0=20`, `f_amp_x=10`, `f_phase_x=0.2`, `f_amp_y=8`, `f_phase_y=0.05`). Its twin carries `rotation.on=0`, `rotation.every=1` and another `f` (`f0=-35`, `f_amp_x=4`, `f_phase_x=0.7`, `f_amp_y=-6`, `f_phase_y=0.6`); off, neither enters the result. The case has no analytic solution (the member's `tests.md` 2-7), so 6-4 restates the judgments the member holds on it.

### 2-8. The refusal cases
- The interval and the selector: the shear-flow base on `nx=ny=32` with `rotation.every=0`, with `rotation.every=-1`, and with `rotation.on=2` (`rotation.every=1`), each `history.every=0`.
- The member's wet-domain refusal: the member's case `swe2d_dry_n032_sx000_sy000_dts500` input for input (`dt_scale=5.0`, the cone, `history.every=9`), with `rotation.on=1`, `rotation.every=1` and the `f` of the shear-flow base. The bench composition refuses at the member's tenth step of $\mathrm{n\_step}=18$ (nine steps committed).
- The member's `dt` refusal: the member's case `swe2d_flat_ref_n032_sx000_sy000_dts100_dtneg` input for input (`dt_scale=-1.0`, `t_end=-0.2`, `history.every=0`), with `rotation.on=1`, `rotation.every=1` and the `f` of the shear-flow base.

### 2-9. The rectangular case
The shear-flow base with `ny=48` and `L_y=0.5` ($dy=1/96$, $dx=1/64$), `rotation.every=2` ($\mathrm{n\_step}=229$).

### 2-10. The process off on the member's flat linear-wave case
The member's case `swe2d_flat_ref_n064_sx000_sy000_dts100`, input for input, with `rotation.on=0`, `rotation.every=2` and the `f` of the shear-flow base. The member's analytic solution of its 2-4 holds on it, so 6-5 restates the member's analytic bands at `nx=64`.

### 2-11. Symmetry pairs
Each transformed case is the shear-flow base on `nx=ny=64` with the inputs below changed; every other input is the base's. A reflection and the axis exchange reverse the sense of rotation, so `f` becomes the negated, transformed field (Controlled Spec §2); a phase $p$ reflected becomes $-p$ modulo `1`, written in $[0,1)$.
- **Axis exchange** (`swr_shear_n064_axis`): `eta_x` ↔ `eta_y` and `eta_phase_x` ↔ `eta_phase_y`; `mom_x0` ↔ `mom_y0`, `mom_x1` ↔ `mom_y1`, `mom_phase_y` ↔ `mom_phase_x`; `f0=-20`, `f_amp_x=-8`, `f_phase_x=0.05`, `f_amp_y=-10`, `f_phase_y=0.2`. (`nx=ny`, `L_x=L_y`, so the exchange of the extents and lengths leaves them unchanged.)
- **Mirror in `x`** (`swr_shear_n064_xmir`): `eta_x=-0.01`, `eta_phase_x=0.9`; `mom_x0=-0.05`, `mom_x1=-0.04`; `mom_y1=-0.05`, `mom_phase_x=0.85`; `f0=-20`, `f_amp_x=10`, `f_phase_x=0.8`, `f_amp_y=-8`.
- **Mirror in `y`** (`swr_shear_n064_ymir`): `eta_y=-0.006`, `eta_phase_y=0.7`; `mom_y0=0.03`, `mom_y1=-0.05`; `mom_x1=-0.04`, `mom_phase_y=0.6`; `f0=-20`, `f_amp_x=-10`, `f_amp_y=8`, `f_phase_y=0.95`.
- **Translation** (`swr_shear_n064_shift`): every field moved by $L_x/4$ along `x` and $L_y/8$ along `y` (`16` and `8` cells): each `x` phase plus `0.25` and each `y` phase plus `0.125`, modulo `1`: `eta_phase_x=0.35`, `mom_phase_x=0.4`, `f_phase_x=0.45`, `eta_phase_y=0.425`, `mom_phase_y=0.525`, `f_phase_y=0.175`; `f` keeps its sign.

### 2-12. The state before the first call
The host evaluates `h`, `hu`, `hv`, `z_b` and `f` of each case from 2-2 to 2-4 (or the member's closed forms for the cases of 2-7, 2-8 and 2-10) at the cell centres, and hands them to the driver as the state before the first step.

## 3. Execution-control rules
- $t_{start}=0.0$, and $t_{end}=0.2$ on every case but `swr_rot_n016_k4_rem3` (`0.205`) and the `dt` refusal case of 2-8 (`-0.2`).
- `dt` and the step count are `derived` values of the case, which the host evaluates by the member's procedure (the member's `tests.md` §3): `dx` $=L_x/nx$, `dy` $=L_y/ny$, then
  1. $\lambda_0=\max_{i,j}\left((|u_{i,j}|+\sqrt{gh_{i,j}})/dx+(|v_{i,j}|+\sqrt{gh_{i,j}})/dy\right)$ at the initial time;
  2. $\mathrm{dt\_raw}=\mathrm{dt\_scale}\cdot \mathrm{cfl\_target}/\lambda_0$, $\mathrm{cfl\_target}=0.45$;
  3. $\mathrm{n\_step}=\lceil (t_{end}-t_{start})/\mathrm{dt\_raw}\rceil$;
  4. $dt=(t_{end}-t_{start})/\mathrm{n\_step}$.

  `f` does not enter the rule. `dt_scale=1.0` on every case but the two member refusal cases of 2-8.
- The stop condition is $n=\mathrm{n\_step}$. The host's driver calls `shallow_water2d_rotating__step` once per step for $n=1,\dots,\mathrm{n\_step}$, with `step` $=n-1$ (the committed count before the call), `rotation` and `rotation_every` the case inputs `rotation.on` and `rotation.every`, and commits each step whose result `ok` is true. The state the host captures before the first step, after the last and every `every` committed steps consists of `h`, `hu`, `hv`, `z_b`, `f` (`nx` × `ny` each) and the logical result `ok` (`0` before the first step), with the driver's time `t` and committed step count `step`. `every` is the case input `history.every` of the following line: `3` on the cases of 2-6, which with `rotation.every=4` samples between rotation calls and on them; `10` on the wave case (about `0.16` rad of the wave per sample, so no phase error aliases between two samples); `20` on the shear-flow cases; the member's own value on the cases of 2-7, 2-8 and 2-10; `0` (no sample) on the other refusal cases.
- history: every = (history.every) swr_dry_n032 = 9, swr_dtneg_n032 = 0, swr_guard_every0_n032 = 0, swr_guard_everyneg_n032 = 0, swr_guard_sel2_n032 = 0, swr_off_cone_n064 = 90, swr_off_cone_n064_twin = 90, swr_off_flat_n064 = 90, swr_pw_n128 = 10, swr_rot_n016_k1 = 3, swr_rot_n016_k4 = 3, swr_rot_n016_k4_rem3 = 3, swr_shear_n032 = 20, swr_shear_n064 = 20, swr_shear_n064_axis = 20, swr_shear_n064_rect = 20, swr_shear_n064_shift = 20, swr_shear_n064_xmir = 20, swr_shear_n064_ymir = 20, swr_shear_n128 = 20, swr_shear_n256 = 20
- distribute: x = grid.nx (min_block 2), y = grid.ny (min_block 2)

## 4. Case-expansion rules
### 4-1. family definition
| family_id | purpose | sweep | fixed |
| --- | --- | --- | --- |
| `swr_rot` | the rotation alone against the closed form (2-6) | $nx=ny=16$, `rotation.every` in `{1, 4}` | `uniform_flow`, `flat`, `f0=50`, amplitudes `0` |
| `swr_pw` | the inertia-gravity wave (2-5) | $nx=128$, $ny=4$ | `poincare_wave_x`, `flat`, `f0=20`, amplitudes `0`, `rotation.every=1` |
| `swr_shear` | the composition under the non-uniform `f`, its refinement family (2-3, 2-4) | $nx=ny\in\{32,64,128,256\}$ | `shear_flow` base, `flat`, the `f` base, `rotation.on=1`, `rotation.every=2` |
| `swr_off` | the process off on the member's cases (2-7, 2-10) | $nx=ny=64$ | the member's inputs, `rotation.on=0` |
| `swr_guard` | the refusals of this `spec` (2-8) | $nx=ny=32$ | the shear-flow base, `history.every=0` |

### 4-2. `case_id` generation rule
- `swr_rot_n016_k{k}` for the interval cases, `swr_pw_n128`, `swr_shear_n{nx:03d}`, `swr_off_cone_n064`, `swr_off_flat_n064`, `swr_guard_{what}_n032`, `swr_dry_n032` and `swr_dtneg_n032`; the override cases of 4-3 carry a suffix after their base's `case_id`, so each sorts after its base (`docs/TESTS.md` §Refinement judgment, carrying case).

### 4-3. Explicit-override cases
- `swr_rot_n016_k4_rem3`: base `swr_rot_n016_k4`; overrides only `t_end` to `0.205`.
- `swr_off_cone_n064_twin`: base `swr_off_cone_n064`; overrides only the rotation inputs (2-7).
- `swr_shear_n064_axis`, `swr_shear_n064_xmir`, `swr_shear_n064_ymir`, `swr_shear_n064_shift`: base `swr_shear_n064`; override only the inputs of 2-11.
- `swr_shear_n064_rect`: base `swr_shear_n064`; overrides only `ny` to `48` and `L_y` to `0.5` (2-9).
- `swr_guard_every0_n032`, `swr_guard_everyneg_n032`, `swr_guard_sel2_n032`: the shear-flow base at `nx=ny=32` with the inputs of 2-8.
- `swr_dry_n032`, `swr_dtneg_n032`: the member's cases of 2-8 with the rotation inputs of 2-8.

## 5. Judged quantities
### 5-1. How a quantity is judged
A test is judged by the host's predicates over the captures alone (`docs/TESTS.md` requirement 9). A name below is what a test's `quantities` line lists; a two-sided band is two names, `<name>.lower` and `<name>.upper`, each with its own predicate. The definitions of 5-4 are expressions over the captured state, the captured time and the case inputs; a value measured on the reference implementation is a calibration, not an input.

### 5-2. Judged names
`<v>` stands for each of `h`, `hu`, `hv`; `<k>` for each of `l1`, `l2`, `linf`; `<n>` for each of `n032`, `n064`, `n128`; `<pair>` for each of `n032_to_n064`, `n064_to_n128`; `<t>` for each of `axis_exchange`, `mirror_x`, `mirror_y`, `translation`.
- `rotation.closed_form.final_rel`, `rotation.closed_form.history_rel`
- `wave.phase_error_max`, `wave.phase_error_final`
- `mass_drift_rel`, `mass_drift_rel.history`
- `extrema.h.min`, `cfl.max`
- `response.transverse_momentum.l2_rel_tend`, `off.rotation_inputs_inert`
- `errors.analytic_<v>.<k>_rel_tend` (nine)
- `order.hu_rms`, `order.hu_mean`, `order.hv_rms`, `order.hv_mean`
- `errors.<t>.<v>_l2_rel` (twelve)
- `errors.restricted.<n>.<v>_<k>_rel` (twenty-seven), `convergence.restricted.<pair>.<v>_<k>_order` (eighteen)
- `run.n_step`, `run.state_moved`, `ok`

### 5-3. Cases of each quantity
- A quantity is evaluated on the target cases of the tests that list it; it is not defined elsewhere.
- `off.rotation_inputs_inert` is carried by `swr_off_cone_n064_twin`, which sorts after `swr_off_cone_n064` and reads its final state.
- `errors.<t>.*` is carried by the transformed case of 2-11, which reads the final state of `swr_shear_n064`.
- `errors.restricted.*` and `convergence.restricted.*` are carried by `swr_shear_n256`, which sorts after the three coarser levels and reads their final states.
- Every other quantity is evaluated per case.

### 5-4. Definition of the quantities
Sums, means and extrema run over every cell of the case's grid; a `history` quantity runs over every sample of the case's `state_history` (the samples stacked on a leading axis), and a `final` / `initial` quantity reads the capture after the last committed step / before the first.
- `rotation.closed_form.final_rel`: $\max\left(\max_{i,j}|hu_{i,j}-\widehat{hu}|,\ \max_{i,j}|hv_{i,j}-\widehat{hv}|\right)/\sqrt{m_{x0}^2+m_{y0}^2}$ at the final state, with $\widehat{hu}$, $\widehat{hv}$ the closed form of 2-6 at $m=\lfloor\mathrm{n\_step}/k\rfloor$.
- `rotation.closed_form.history_rel`: the same over every sample, with $m=\lfloor \mathrm{step}/k\rfloor$ of the sample, comparing the largest and the smallest value of `hu` and of `hv` over the cells of each sample with the closed form (the state is uniform).
- `wave.phase_error_max`: the phase of the mode $\cos(\kappa x-\phi)$ of $h-H_0$ in each sample, against $\omega t$ of the sample, wrapped to $(-\pi,\pi]$, its largest magnitude over the samples. The phase is read from two first moments in `x`: with $D=nx\,dx/(2\sin(\kappa\,dx/2))$, $S_1=\sum_{i,j}x_i\,(h_{i,j}-H_0)=-A\,D\,ny\sin\phi$ and, on the field moved by a quarter wavelength ($nx/4$ cells along `x`, `roll`), $S_2=\sum_{i,j}x_i\,(h_{i-nx/4,j}-H_0)=-A\,D\,ny\cos\phi$, exactly on the cell centres, so $\phi=\mathrm{atan2}(-S_1,-S_2)$. The error is $|\mathrm{atan2}(\sin d,\cos d)|$ with $d=\phi-\omega t$, $\omega$ of 2-5. Read sample by sample, a shift that grows past $\pi$ over the run is seen at the sample where it is near $\pi$.
- `wave.phase_error_final`: the same at the final state, against $\omega$ times its captured time.
- `mass_drift_rel`: $\left|\sum h^{final}-\sum h^{initial}\right|/\sum h^{initial}$.
- `mass_drift_rel.history`: $\max_s\left|\sum h^{(s)}-\sum h^{initial}\right|/\sum h^{initial}$ over the samples $s$.
- `extrema.h.min`: the smallest `h` of the initial and the final state (and, on 6-3, of every sample).
- `cfl.max`: $\Delta t\cdot\max_{i,j}\left((|u|+\sqrt{gh})/dx+(|v|+\sqrt{gh})/dy\right)$, at the final state and, as a second predicate under the same name, over every sample; $u=hu/h$, $v=hv/h$.
- `response.transverse_momentum.l2_rel_tend`: the member's quantity of that name, $\lVert hv^{final}\rVert_2/\lVert hu^{initial}\rVert_2$.
- `off.rotation_inputs_inert`: $\max_{v}\max_{i,j}|v^{twin}_{i,j}-v^{base}_{i,j}|\,/\,\max_v\max_{i,j}|v^{base}_{i,j}|$ over `h`, `hu`, `hv` of the final states of `swr_off_cone_n064_twin` and `swr_off_cone_n064`.
- `errors.analytic_<v>.<k>_rel_tend`: the member's quantities of these names on its linear wave (the member's `tests.md` 2-4 and 5-4): the `l1`, `l2` and `linf` norm of the final `h`, `hu` against the reference $h_{ref}=H_0+\eta_0\sin(2\pi(x/L_x-\mathrm{wave\_sign}\,c_0t_{end}/L_x-\mathrm{shift\_x\_fraction}))$, $hu_{ref}=h_{ref}\cdot\mathrm{wave\_sign}\,c_0(h_{ref}-H_0)/H_0$, $c_0=\sqrt{gH_0}$, each normalised by the same norm of its reference; and of `hv` against $0$, normalised by the larger of that norm of $hu_{ref}$ and of $hv_{ref}=0$.
- `order.hu_rms`, `order.hv_rms`: $\sqrt{\mathrm{mean}_{i,j}(hu^2)}$, $\sqrt{\mathrm{mean}_{i,j}(hv^2)}$ at the final state. `order.hu_mean`, `order.hv_mean`: $\mathrm{mean}_{i,j}(hu)$, $\mathrm{mean}_{i,j}(hv)$ at the final state. Each reads one momentum component, not a rotation invariant of the two.
- `errors.<t>.<v>_l2_rel`: the `l2` norm of the transformed case's final `<v>` minus the base's final `<v>` transformed, normalised by the base's `l2` norm of `h` for `h` and by the larger of the base's `l2` norms of `hu` and `hv` for a momentum component. The transformations (cell indices from `1`): axis exchange $h'_{i,j}=h_{j,i}$, $hu'_{i,j}=hv_{j,i}$, $hv'_{i,j}=hu_{j,i}$; mirror in `x` $(h,hu,hv)'_{i,j}=(h,-hu,hv)_{nx+1-i,j}$; mirror in `y` $(h,hu,hv)'_{i,j}=(h,hu,-hv)_{i,ny+1-j}$; translation $(h,hu,hv)'_{i,j}=(h,hu,hv)_{i-16,j-8}$, indices modulo the extents.
- `errors.restricted.<n>.<v>_<k>_rel`: the `<k>` norm of the final `<v>` of the level `<n>` minus the final `<v>` of `swr_shear_n256` restricted to that grid by the conservative block mean over the $(256/n)^2$ cells each coarse cell covers, normalised by the same norm of the restricted reference of the same variable: `h`, `hu` and `hv` are each non-zero and none is a residue of the flow on the shear-flow state, so no momentum component takes the vector's normaliser (`docs/TESTS.md` §Refinement judgment).
- `convergence.restricted.<pair>.<v>_<k>_order`: $\log_2(e_{coarse}/e_{fine})$ of the two levels of the pair.
- `run.n_step`: the captured committed step count minus $\mathrm{n\_step}$ of §3.
- `run.state_moved`: $\max_{i,j}|h^{final}-h^{initial}|$; positive when at least one step was committed.
- `ok`: the captured logical result after the run.

### 5-5. Default thresholds
Every band of a rotating case is calibrated against an independent reference implementation of the Controlled Spec §5 composition (`issue #265`'s procedure): the member's §5 scheme on a flat bottom and the RK4 rotation of §5, written from the Controlled Specs alone, which reproduces the captured state of the certified member composed with a hand-written composing model on every rotating case of this suite to `4e-13` relative (system tier PR-S2, `tools/tests/data/drive_ir/system_tier_evidence/`). Each value moves by round-off alone under a conforming kernel: the member's discretization, the RK4 step and the firing rule are each fixed by the Controlled Specs, and `dt` by §3.

- $\mathrm{cfl.max}\le 1.0$, and `run.n_step` $=0$ on every target case of every `pass` test.
- $\mathrm{mass\_drift\_rel}\le 1.0e{-12}$ and $\mathrm{mass\_drift\_rel.history}\le 1.0e{-12}$ on the flat cases (the reference reads at most `2.2e-16`); on the cone cases of 6-4, $\mathrm{mass\_drift\_rel}\le 1.0e{-10}$, the member's bound, restated.
- $\mathrm{extrema.h.min}\ge 0.5$ on the shear-flow cases (6-3) and $\ge 0.05$ on the cone cases (6-4), the member's bound.
- `rotation.closed_form.final_rel` and `rotation.closed_form.history_rel` $\le 1.0e{-12}$ (the reference reads at most `2.8e-15`). Margins (2-6): the closed forms at `rotation.every=1` and `4` differ by `0.45` of $|w_0|$ on `n_step=46`; `4` RK4 steps of $\Delta t$ in place of one of $4\Delta t$ moves the value by `3.9e-2`, and $\Delta t$ in place of $4\Delta t$ by `0.60`, each more than $10^{10}$ times the bound.
- `wave.phase_error_max` and `wave.phase_error_final` $\le 0.05$ rad. The reference reads `1.01e-2` and `2.0e-3`. A kernel that calls the member alone (the rotation dropped) reads `1.47` and `1.50`; one that calls the rotation alone (the member dropped) reads `3.14` and `0.67`. The bound is under a tenth of the smallest of the four readings (`0.67`).
- `response.transverse_momentum.l2_rel_tend` is judged on 6-4 as the member's band at `nx=64`, $[1.6e{-2},\ 2.1e{-2}]$ (the member's `tests.md` 5-5; reference `1.861e-2`), as two distinct judged quantities.
- $\mathrm{off.rotation\_inputs\_inert}\le 0.0$: off, the composition is the member, and the same binary computes the two cases with the same arithmetic.
- `errors.analytic_<v>.<k>_rel_tend` are judged on 6-5 as the member's bands at `nx=64` (the member's `tests.md` 5-5, the row `swe2d_flat_ref_n064_sx000_sy000_dts100`): for `h`, $[1.0e{-4},\ 1.3e{-4}]$ (`l1`), $[1.1e{-4},\ 1.4e{-4}]$ (`l2`), $[1.5e{-4},\ 2.0e{-4}]$ (`linf`); for `hu`, $[1.5e{-1},\ 2.0e{-1}]$ in each norm; each as two distinct judged quantities; and `hv` $\le 1.0e{-10}$ in each norm.
- `order.*` (6-6) are judged as two-sided bands of half-width `1.0e-6` times the reference value, rounded outward at the seventh significant digit, two distinct judged quantities each. The margin is the one-off difference of the two splitting orders: with the rotation first, the reference moves the four quantities by `1.3e-2`, `1.5e-2`, `1.0e-2` and `3.8e-3` of their value on `swr_shear_n064` and by `9.9e-3`, `1.2e-2`, `2.1e-3` and `2.9e-4` on `swr_shear_n064_rect` (`order.hu_rms`, `order.hu_mean`, `order.hv_rms`, `order.hv_mean`), from `259` to `13700` times the half-width; the two orders converge to the same equations, so no refinement family or band of truncation width separates them, and these bands are not of that kind.

| quantity | `swr_shear_n064` | `swr_shear_n064_rect` |
| --- | --- | --- |
| `order.hu_rms` | $[2.591682e{-2},\ 2.591688e{-2}]$ | $[3.586580e{-2},\ 3.586588e{-2}]$ |
| `order.hu_mean` | $[-8.321736e{-3},\ -8.321718e{-3}]$ | $[-7.526936e{-3},\ -7.526920e{-3}]$ |
| `order.hv_rms` | $[4.048892e{-2},\ 4.048901e{-2}]$ | $[4.485663e{-2},\ 4.485673e{-2}]$ |
| `order.hv_mean` | $[3.040306e{-2},\ 3.040313e{-2}]$ | $[4.095345e{-2},\ 4.095354e{-2}]$ |

- Every symmetry-pair residual `errors.<t>.<v>_l2_rel` is $\le 2.0e{-11}$ (the member's bound; the reference reads at most `4.2e-15`).
- The restricted-reference errors of 6-11 are judged as two-sided bands, `0.9` and `1.1` times the reference value rounded outward to two digits, two distinct judged quantities each, and their orders by lower bounds, the reference order minus `0.3` rounded down to one decimal (an order against a restricted reference includes the reference's own error, `docs/TESTS.md`):

| case | variable | `l1` | `l2` | `linf` |
| --- | --- | --- | --- | --- |
| `n032` | `h` | $[2.8e{-3},\ 3.5e{-3}]$ | $[3.5e{-3},\ 4.4e{-3}]$ | $[1.0e{-2},\ 1.4e{-2}]$ |
|  | `hu` | $[2.8e{-1},\ 3.5e{-1}]$ | $[2.6e{-1},\ 3.2e{-1}]$ | $[2.4e{-1},\ 3.1e{-1}]$ |
|  | `hv` | $[2.0e{-1},\ 2.6e{-1}]$ | $[2.1e{-1},\ 2.7e{-1}]$ | $[2.2e{-1},\ 2.8e{-1}]$ |
| `n064` | `h` | $[1.3e{-3},\ 1.7e{-3}]$ | $[1.7e{-3},\ 2.1e{-3}]$ | $[5.5e{-3},\ 6.8e{-3}]$ |
|  | `hu` | $[1.4e{-1},\ 1.9e{-1}]$ | $[1.3e{-1},\ 1.7e{-1}]$ | $[1.3e{-1},\ 1.6e{-1}]$ |
|  | `hv` | $[1.0e{-1},\ 1.3e{-1}]$ | $[1.0e{-1},\ 1.3e{-1}]$ | $[1.0e{-1},\ 1.4e{-1}]$ |
| `n128` | `h` | $[4.7e{-4},\ 5.8e{-4}]$ | $[6.1e{-4},\ 7.5e{-4}]$ | $[2.0e{-3},\ 2.6e{-3}]$ |
|  | `hu` | $[4.8e{-2},\ 5.9e{-2}]$ | $[4.4e{-2},\ 5.5e{-2}]$ | $[4.4e{-2},\ 5.5e{-2}]$ |
|  | `hv` | $[3.8e{-2},\ 4.8e{-2}]$ | $[4.0e{-2},\ 5.0e{-2}]$ | $[4.3e{-2},\ 5.4e{-2}]$ |
| `n032_to_n064` order | `h` | $\ge 0.7$ | $\ge 0.7$ | $\ge 0.6$ |
|  | `hu` | $\ge 0.6$ | $\ge 0.6$ | $\ge 0.6$ |
|  | `hv` | $\ge 0.7$ | $\ge 0.7$ | $\ge 0.7$ |
| `n064_to_n128` order | `h` | $\ge 1.2$ | $\ge 1.1$ | $\ge 1.1$ |
|  | `hu` | $\ge 1.3$ | $\ge 1.3$ | $\ge 1.2$ |
|  | `hv` | $\ge 1.0$ | $\ge 1.0$ | $\ge 0.9$ |

- `ok` $=0$ on the refusal cases (6-12 to 6-15), and `run.state_moved` $>0$ on `swr_dry_n032` (6-14).
- $\mathrm{cross\_target\_state\_agreement}\le 1.0e{-10}$ for each of `h`, `hu` and `hv`.

## 6. Test definitions
### 6-1. `l1_rotation_closed_form`
- `target_cases`: `swr_rot_n016_k1`, `swr_rot_n016_k4`, `swr_rot_n016_k4_rem3`
- `level`: `L1`
- `judgment`: the rotation alone against the closed form of the RK4 polynomial at the final state and over every sample (2-6, 5-5); `cfl.max`, `run.n_step`. The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, `hv`, and the threshold is $\le 1.0e{-10}$.
- `expected_outcome`: `pass`
- `quantities`: `rotation.closed_form.final_rel`, `rotation.closed_form.history_rel`, `cfl.max`, `run.n_step`

### 6-2. `l2_inertia_gravity_wave_phase`
- `target_cases`: `swr_pw_n128`
- `level`: `L2`
- `judgment`: the phase of the inertia-gravity wave against $\omega t$ over every sample and at the final state (2-5, 5-4, 5-5); mass; `cfl.max`, `run.n_step`. The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, `hv`, and the threshold is $\le 1.0e{-10}$.
- `expected_outcome`: `pass`
- `quantities`: `wave.phase_error_max`, `wave.phase_error_final`, `mass_drift_rel`, `cfl.max`, `run.n_step`

### 6-3. `l2_mass_conservation`
- `target_cases`: every `swr_shear` case, the override cases of 4-3 included
- `level`: `L2`
- `judgment`: mass over every sample and at the final state; the smallest depth; `cfl.max`, `run.n_step`. The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, `hv`, and the threshold is $\le 1.0e{-10}$.
- `expected_outcome`: `pass`
- `quantities`: `mass_drift_rel.history`, `mass_drift_rel`, `extrema.h.min`, `cfl.max`, `run.n_step`

### 6-4. `l2_process_off_member_cone`
- `target_cases`: `swr_off_cone_n064`, `swr_off_cone_n064_twin`
- `level`: `L2`
- `judgment`: the process off on the member's cone case (2-7): the member's transverse-momentum band at `nx=64`, mass, the smallest depth, `cfl.max`, `run.n_step`, and the twin's final state equal to the base's. The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, `hv`, and the threshold is $\le 1.0e{-10}$.
- `expected_outcome`: `pass`
- `quantities`: `response.transverse_momentum.l2_rel_tend.lower`, `response.transverse_momentum.l2_rel_tend.upper`, `off.rotation_inputs_inert`, `mass_drift_rel`, `extrema.h.min`, `cfl.max`, `run.n_step`

### 6-5. `l1_process_off_member_flat`
- `target_cases`: `swr_off_flat_n064`
- `level`: `L1`
- `judgment`: the process off on the member's flat linear-wave case (2-10): the member's analytic bands at `nx=64`, mass, `cfl.max`, `run.n_step`. The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, `hv`, and the threshold is $\le 1.0e{-10}$.
- `expected_outcome`: `pass`
- `quantities`: `errors.analytic_h.l1_rel_tend.lower`, `errors.analytic_h.l1_rel_tend.upper`, `errors.analytic_h.l2_rel_tend.lower`, `errors.analytic_h.l2_rel_tend.upper`, `errors.analytic_h.linf_rel_tend.lower`, `errors.analytic_h.linf_rel_tend.upper`, `errors.analytic_hu.l1_rel_tend.lower`, `errors.analytic_hu.l1_rel_tend.upper`, `errors.analytic_hu.l2_rel_tend.lower`, `errors.analytic_hu.l2_rel_tend.upper`, `errors.analytic_hu.linf_rel_tend.lower`, `errors.analytic_hu.linf_rel_tend.upper`, `errors.analytic_hv.l1_rel_tend`, `errors.analytic_hv.l2_rel_tend`, `errors.analytic_hv.linf_rel_tend`, `mass_drift_rel`, `cfl.max`, `run.n_step`

### 6-6. `l3_splitting_order_nonuniform_f`
- `target_cases`: `swr_shear_n064`, `swr_shear_n064_rect`
- `level`: `L3`
- `judgment`: the four `order.*` bands of 5-5 on the square and the rectangular grid, mass, `cfl.max`, `run.n_step`. The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, `hv`, and the threshold is $\le 1.0e{-10}$.
- `expected_outcome`: `pass`
- `quantities`: `order.hu_rms.lower`, `order.hu_rms.upper`, `order.hu_mean.lower`, `order.hu_mean.upper`, `order.hv_rms.lower`, `order.hv_rms.upper`, `order.hv_mean.lower`, `order.hv_mean.upper`, `mass_drift_rel`, `cfl.max`, `run.n_step`

### 6-7. `l3_axis_exchange_pair`
- `target_cases`: `swr_shear_n064`, `swr_shear_n064_axis`
- `level`: `L3`
- `judgment`: the axis-exchange residual of 5-4 for `h`, `hu`, `hv`, mass, `cfl.max`, `run.n_step`. The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, `hv`, and the threshold is $\le 1.0e{-10}$.
- `expected_outcome`: `pass`
- `quantities`: `errors.axis_exchange.h_l2_rel`, `errors.axis_exchange.hu_l2_rel`, `errors.axis_exchange.hv_l2_rel`, `mass_drift_rel`, `cfl.max`, `run.n_step`

### 6-8. `l3_mirror_x_pair`
- `target_cases`: `swr_shear_n064`, `swr_shear_n064_xmir`
- `level`: `L3`
- `judgment`: the mirror-in-`x` residual of 5-4 for `h`, `hu`, `hv`, mass, `cfl.max`, `run.n_step`. The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, `hv`, and the threshold is $\le 1.0e{-10}$.
- `expected_outcome`: `pass`
- `quantities`: `errors.mirror_x.h_l2_rel`, `errors.mirror_x.hu_l2_rel`, `errors.mirror_x.hv_l2_rel`, `mass_drift_rel`, `cfl.max`, `run.n_step`

### 6-9. `l3_mirror_y_pair`
- `target_cases`: `swr_shear_n064`, `swr_shear_n064_ymir`
- `level`: `L3`
- `judgment`: the mirror-in-`y` residual of 5-4 for `h`, `hu`, `hv`, mass, `cfl.max`, `run.n_step`. The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, `hv`, and the threshold is $\le 1.0e{-10}$.
- `expected_outcome`: `pass`
- `quantities`: `errors.mirror_y.h_l2_rel`, `errors.mirror_y.hu_l2_rel`, `errors.mirror_y.hv_l2_rel`, `mass_drift_rel`, `cfl.max`, `run.n_step`

### 6-10. `l3_translation_pair`
- `target_cases`: `swr_shear_n064`, `swr_shear_n064_shift`
- `level`: `L3`
- `judgment`: the translation residual of 5-4 for `h`, `hu`, `hv`, mass, `cfl.max`, `run.n_step`. The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, `hv`, and the threshold is $\le 1.0e{-10}$.
- `expected_outcome`: `pass`
- `quantities`: `errors.translation.h_l2_rel`, `errors.translation.hu_l2_rel`, `errors.translation.hv_l2_rel`, `mass_drift_rel`, `cfl.max`, `run.n_step`

### 6-11. `l1_refinement_restricted`
- `target_cases`: `swr_shear_n032`, `swr_shear_n064`, `swr_shear_n128`, `swr_shear_n256`
- `level`: `L1`
- `judgment`: the restricted-reference errors of `h`, `hu`, `hv` at the three coarser levels in three norms, as two-sided bands, and their orders, carried by `swr_shear_n256` (5-3, 5-5); mass, `cfl.max`, `run.n_step` per case. The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `h`, `hu`, `hv`, and the threshold is $\le 1.0e{-10}$.
- `expected_outcome`: `pass`
- `quantities`: `errors.restricted.n032.h_l1_rel.lower`, `errors.restricted.n032.h_l1_rel.upper`, `errors.restricted.n032.h_l2_rel.lower`, `errors.restricted.n032.h_l2_rel.upper`, `errors.restricted.n032.h_linf_rel.lower`, `errors.restricted.n032.h_linf_rel.upper`, `errors.restricted.n032.hu_l1_rel.lower`, `errors.restricted.n032.hu_l1_rel.upper`, `errors.restricted.n032.hu_l2_rel.lower`, `errors.restricted.n032.hu_l2_rel.upper`, `errors.restricted.n032.hu_linf_rel.lower`, `errors.restricted.n032.hu_linf_rel.upper`, `errors.restricted.n032.hv_l1_rel.lower`, `errors.restricted.n032.hv_l1_rel.upper`, `errors.restricted.n032.hv_l2_rel.lower`, `errors.restricted.n032.hv_l2_rel.upper`, `errors.restricted.n032.hv_linf_rel.lower`, `errors.restricted.n032.hv_linf_rel.upper`, `errors.restricted.n064.h_l1_rel.lower`, `errors.restricted.n064.h_l1_rel.upper`, `errors.restricted.n064.h_l2_rel.lower`, `errors.restricted.n064.h_l2_rel.upper`, `errors.restricted.n064.h_linf_rel.lower`, `errors.restricted.n064.h_linf_rel.upper`, `errors.restricted.n064.hu_l1_rel.lower`, `errors.restricted.n064.hu_l1_rel.upper`, `errors.restricted.n064.hu_l2_rel.lower`, `errors.restricted.n064.hu_l2_rel.upper`, `errors.restricted.n064.hu_linf_rel.lower`, `errors.restricted.n064.hu_linf_rel.upper`, `errors.restricted.n064.hv_l1_rel.lower`, `errors.restricted.n064.hv_l1_rel.upper`, `errors.restricted.n064.hv_l2_rel.lower`, `errors.restricted.n064.hv_l2_rel.upper`, `errors.restricted.n064.hv_linf_rel.lower`, `errors.restricted.n064.hv_linf_rel.upper`, `errors.restricted.n128.h_l1_rel.lower`, `errors.restricted.n128.h_l1_rel.upper`, `errors.restricted.n128.h_l2_rel.lower`, `errors.restricted.n128.h_l2_rel.upper`, `errors.restricted.n128.h_linf_rel.lower`, `errors.restricted.n128.h_linf_rel.upper`, `errors.restricted.n128.hu_l1_rel.lower`, `errors.restricted.n128.hu_l1_rel.upper`, `errors.restricted.n128.hu_l2_rel.lower`, `errors.restricted.n128.hu_l2_rel.upper`, `errors.restricted.n128.hu_linf_rel.lower`, `errors.restricted.n128.hu_linf_rel.upper`, `errors.restricted.n128.hv_l1_rel.lower`, `errors.restricted.n128.hv_l1_rel.upper`, `errors.restricted.n128.hv_l2_rel.lower`, `errors.restricted.n128.hv_l2_rel.upper`, `errors.restricted.n128.hv_linf_rel.lower`, `errors.restricted.n128.hv_linf_rel.upper`, `convergence.restricted.n032_to_n064.h_l1_order`, `convergence.restricted.n032_to_n064.h_l2_order`, `convergence.restricted.n032_to_n064.h_linf_order`, `convergence.restricted.n032_to_n064.hu_l1_order`, `convergence.restricted.n032_to_n064.hu_l2_order`, `convergence.restricted.n032_to_n064.hu_linf_order`, `convergence.restricted.n032_to_n064.hv_l1_order`, `convergence.restricted.n032_to_n064.hv_l2_order`, `convergence.restricted.n032_to_n064.hv_linf_order`, `convergence.restricted.n064_to_n128.h_l1_order`, `convergence.restricted.n064_to_n128.h_l2_order`, `convergence.restricted.n064_to_n128.h_linf_order`, `convergence.restricted.n064_to_n128.hu_l1_order`, `convergence.restricted.n064_to_n128.hu_l2_order`, `convergence.restricted.n064_to_n128.hu_linf_order`, `convergence.restricted.n064_to_n128.hv_l1_order`, `convergence.restricted.n064_to_n128.hv_l2_order`, `convergence.restricted.n064_to_n128.hv_linf_order`, `mass_drift_rel`, `cfl.max`, `run.n_step`

### 6-12. `l0_rotation_every_refusal_xfail`
- `target_cases`: `swr_guard_every0_n032`, `swr_guard_everyneg_n032`
- `level`: `L0`
- `xfail_condition`: `rotation.every < 1` (at the boundary `0`, and at `-1`)
- `judgment`: the step refuses: `ok` $=0$. The cross-target judgment is not applied.
- `expected_outcome`: `xfail`
- `quantities`: `ok`

### 6-13. `l0_selector_refusal_xfail`
- `target_cases`: `swr_guard_sel2_n032`
- `level`: `L0`
- `xfail_condition`: `rotation.on` not in `{0, 1}`
- `judgment`: the step refuses: `ok` $=0$. The cross-target judgment is not applied.
- `expected_outcome`: `xfail`
- `quantities`: `ok`

### 6-14. `l0_member_refusal_xfail`
- `target_cases`: `swr_dry_n032`
- `level`: `L0`
- `xfail_condition`: the member's wet-domain invariant fails mid-run, with the rotation firing on every step (2-8)
- `judgment`: the run ends on the member's refusal: `ok` $=0$, and `run.state_moved` $>0$ (steps were committed before it). A kernel that ignores the member's result, or lets the RK4 result overwrite it, continues to `n_step` with `ok` $=1$. The cross-target judgment is not applied.
- `expected_outcome`: `xfail`
- `quantities`: `ok`, `run.state_moved`

### 6-15. `l0_member_dt_refusal_xfail`
- `target_cases`: `swr_dtneg_n032`
- `level`: `L0`
- `xfail_condition`: `dt < 0` with `rotation.on=1`, `rotation.every=1` (2-8): the member refuses before the rotation process is reached
- `judgment`: `ok` $=0$, the flag alone. The cross-target judgment is not applied.
- `expected_outcome`: `xfail`
- `quantities`: `ok`

## 7. Pass/fail aggregation rules
`self_verdict` is `pass` when every `pass` test passes and every `xfail` test fails as expected; any other outcome is `fail`. A `pass` test passes when every predicate of every quantity it lists holds on every target case it is evaluated on.

## 8. Traceability
### 8-1. Output requirements
The host records the captures of §3 per case, `test_profile_id` and `test_profile_version` in `trial_meta.json`, and the per-test results in `verdict.json`.

### 8-2. Traceability records
The calibration of 5-5 is reproduced by `tools/tests/data/drive_ir/system_tier_evidence/calibrate.py` from the reference `reference.py` beside it, and the seeded composition defects the suite was required to catch before any billed run, with the predicate that caught each, by `bench.py` beside it (system tier PR-S2). The member's bands restated in 6-4 and 6-5 are the member's `tests.md` 5-5 at `test_profile_version` `0.9.0`.
