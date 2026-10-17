# Tests: 1D linear advection-diffusion (verification input / judgment conditions)

## 0. Meta information
- `status`: `draft`
- `test_profile_id`: `advdiff1d_linear_baseline`
- `test_profile_version`: `0.5.0`
- `spec_ref.spec_kind`: `problem`
- `spec_ref.spec_id`: `advdiff1d_linear`
- `spec_ref.spec_version`: `0.4.5`
- `spec_ref.controlled_spec_path`: `spec/problem/dynamics/advection_diffusion/advdiff1d_linear/controlled_spec.md`

## 1. Test purpose
This suite verifies, for the discrete implementation of the 1D linear advection-diffusion equation, accuracy in three norms with the order of each, conservation, translation equivariance, the mirror equivariance of the Controlled Spec §5 by a symmetry pair, and the CFL guard (`docs/TESTS.md` §Refinement judgment, §Symmetry pairs). A one-dimensional problem has no axis exchange, so the suite carries no axis-exchange pair. The judgment targets are `L0` to `L3`, and include an expected failure (`xfail`).

## 2. Input-defaulting rules
### 2-1. Input instance
- The domain length `L` is `1.0`.
- The advection velocity is `a=1.0` on every case but the mirror case of 2-2, which carries `a=-1.0`.
- The initial condition is parameterized by `shift_fraction`, and is defined by the following.
  $$
  u(x,0)=\sin\left(2\pi\left(\frac{x}{L}-\text{shift\_fraction}\right)\right)+0.5\sin\left(4\pi\left(\frac{x}{L}-\text{shift\_fraction}\right)\right)
  $$
- The default of `shift_fraction` is `0.0`, and the case value is fixed by the 4-1 table.
- The theoretical solution is defined by the following.
  $$
  u(x,t)=\exp(-\nu k_1^2 t)\sin(k_1(x-a t-\text{shift\_fraction}\cdot L))+0.5\exp(-\nu k_2^2 t)\sin(k_2(x-a t-\text{shift\_fraction}\cdot L))
  $$
- The symbols used in the theoretical solution are $k_1=2\pi/L$ and $k_2=4\pi/L$.
- The initial condition of a case with $\text{shift\_fraction}=s$ is the translation of the $s=0$ initial condition by $+s\cdot L$; this is the property the translation-equivariance judgment of 6-3 evaluates.
- The theoretical solution holds for either sign of `a`.

### 2-2. The mirror pair
The Controlled Spec §5 states the mirror equivariance: the case with `a` → `-a` and the initial state reflected, $u'(x,0)=u(L-x,0)$, has the reflected state at every step. The initial condition of 2-1 at `shift_fraction=0.0` is odd under the reflection, $u(L-x,0)=-u(x,0)$, so the equation's sign reversal (`docs/TESTS.md` §Symmetry pairs) lets the mirror case keep the base's initial condition: `advdiff1d_ref_nx128_shift000_dts100_amir` (4-3) is `advdiff1d_ref_nx128_shift000_dts100` with `a=-1.0` alone, and its state at $t_{end}$ is $u'_i=-u_{nx-1-i}$ of the base's (the array reversed, the grammar's `flip(·, 0)`, with the sign reversed). The pair is judged by `errors.mirror.l2_rel` (5-4) with the bound `1.0e-12`, the suite's own round-off bound (that of `symmetry_l2_rel` and the cross-target judgment): the update for $a<0$ is the update for $a>0$ with the two neighbours exchanged, so a conforming kernel gives round-off. On the reference implementation of the Controlled Spec §5 update it reads `5.6e-16` (`2.0e-16` and `3.4e-16` at `nx=64` and `256`), with the same `n_step`, `328`. A kernel that takes the upwind cell from the left whatever the sign of `a` reads `0.25`, and its mirror case alone reads `0.151` in `errors.analytic.l2_rel_tend`, outside the band of 5-5. The mirror case also judges `mode_scheme_fidelity` with the amplification rate of the $a<0$ update (5-4), which fixes its update to round-off on its own.

## 3. Execution-control rules
- $t_{start}=0.0$ and $t_{end}=0.5$.
- `dt` is decided by the following procedure.
  1. $\text{dt\_raw} = \text{dt\_scale}\cdot\min(\text{cfl\_adv}\cdot dx/|a|,\ \text{cfl\_dif}\cdot dx^2/\nu)$
  2. $\text{n\_step} = \lceil t_{end}/\text{dt\_raw}\rceil$
  3. $dt = t_{end}/\text{n\_step}$
- $\text{cfl\_adv}=0.6$ and $\text{cfl\_dif}=0.25$.
- The default of `dt_scale` is `1.0`, and the case value is fixed by the 4-1 table. `dt_scale` enters the procedure only at step 1; it does not scale $dt$ or $\text{n\_step}$ directly, so the integration still lands exactly on $t_{end}$.
- The stop condition is $n = \text{n\_step}$.
- The per-case state snapshot is emitted at $t_{end}$, which is the evaluation time of every metric of 5-4.

## 4. Case-expansion rules
### 4-1. family definition
The sweep and fixed values per `family` are defined below.

| family_id | purpose | sweep | fixed |
| --- | --- | --- | --- |
| `advdiff1d_ref` | refinement_for_accuracy | $nx \in \{64,128,256\}$ | $a=1.0,\ \text{shift\_fraction}=0.0,\ \text{dt\_scale}=1.0$ |
| `advdiff1d_sym` | translation_symmetry | $\text{nx}=128,\ \text{shift\_fraction}=0.25$ | $a=1.0,\ \text{dt\_scale}=1.0$ |
| `advdiff1d_guard` | expected_failure_for_cfl | $\text{nx}=64,\ \text{dt\_scale}=1.2$ | $a=1.0,\ \text{shift\_fraction}=0.0$ |

### 4-2. `case_id` generation rule
- The template is `{family_id}_nx{nx:03d}_shift{shift_pct:03d}_dts{dts_pct:03d}`.
- `family_id` is the `family_id` column of the 4-1 table, substituted verbatim; it already carries the `advdiff1d_` prefix, and no further prefix is prepended.
- `shift_pct` is decided by $\text{round}(100\cdot\text{shift\_fraction})$.
- `dts_pct` is decided by $\text{round}(100\cdot\text{dt\_scale})$.
- The expansion order is fixed in the order `family_id`, `nx`, `shift_fraction`, `dt_scale`.

### 4-3. Explicit-override cases
- Add the `case_id` `advdiff1d_ref_nx128_shift000_dts100_tend200`.
- The `base_case_id` references `advdiff1d_ref_nx128_shift000_dts100`, and overrides only `t_end` to $2.0$.
- Add the `case_id` `advdiff1d_ref_nx128_shift000_dts100_amir`.
- The `base_case_id` references `advdiff1d_ref_nx128_shift000_dts100`, and overrides only `a` to $-1.0$ (the mirror case of 2-2; the `case_id` keeps its base's and carries the suffix `_amir`, so it sorts after its base case and reads the base's final state).

## 5. Diagnostics contract
### 5-1. Artifacts
- The diagnostic output file is `diagnostics.json`.
- The judgment output file is `verdict.json`.

### 5-2. Required diagnostic items
`diagnostics.json` requires the following fields.
- `cfl.combined_max`
- `conserved.mass.initial`
- `conserved.mass.final`
- `conserved.mass.abs_initial`
- `metrics.mass_drift_rel`
- `errors.analytic.l1_rel_tend`
- `errors.analytic.l2_rel_tend`
- `errors.analytic.linf_rel_tend`
- `errors.mode_scheme_fidelity`
- `errors.symmetry_l2_rel`
- `errors.mirror.l2_rel`
- `convergence.nx64_to_nx128.l1_order`, `convergence.nx64_to_nx128.l2_order`, `convergence.nx64_to_nx128.linf_order`
- `convergence.nx128_to_nx256.l1_order`, `convergence.nx128_to_nx256.l2_order`, `convergence.nx128_to_nx256.linf_order`

### 5-3. `N/A` rule
- When a diagnostic item is incomputable, make the output value `null` and require `reason_na`.
- `errors.symmetry_l2_rel` is `N/A` for anything other than a translation-pair evaluation, and is emitted on the `shifted` case of the pair.
- `errors.mirror.l2_rel` is `N/A` for anything other than the mirror case of 2-2; it is carried by `advdiff1d_ref_nx128_shift000_dts100_amir`, which sorts after its base case and reads the base case's final state.
- The threshold-definition source is this file, and a per-test threshold override is permitted.

### 5-4. Definition of the metrics
Every judged metric name below is bound to the `diagnostics.json` field of 5-2 that carries its value. A metric that is a derived quantity is emitted by the runner under its own field address, already reduced to a scalar; the definition below fixes how that value is computed. No correspondence other than the ones fixed here is permitted.

`cfl_combined_max` is emitted as the field `cfl.combined_max`. Its value is $\max_n (|C|+2D)$ over the integration steps of the case, where $C=a\,dt/dx$ and $D=\nu\,dt/dx^2$.

The value of `cfl_combined_max` is fixed by the case inputs, the constants and the $dt$ of 3 alone; no captured state variable enters it, so its definition is not its primary evidence (`docs/TESTS.md` requirement 9). The primary evidence of the CFL judgment, evaluated by the host, is
$$
\max\left(|C|+2D,\ \frac{\max_i|u_i(t_{end})|}{\max_i|u_i(0)|}\right)
$$
with $C=a\,dt/dx$, $D=\nu\,dt/dx^2$, $dx=L/nx$, and the observed time step $dt=(t_{final}-t_{initial})/n_{step}$. Here $t_{initial}$ and $t_{final}$ are the times the kernel reports at the initial and the final state capture, $n_{step}$ is the step count the procedure of 3 fixes from the case inputs, $a$ is the case's advection velocity and $\nu$ the constant of `controlled_spec.md` §6. The second term is the only state term; it rests on the max-norm bound of `controlled_spec.md` §5, under which it does not exceed 1 (up to round-off) whenever $|C|+2D\le 1$. In a `pass` test both terms are therefore at most 1, and a state whose maximum grew exceeds the threshold. In 6-4 the guard leaves no state fact: the guard case has $|C|+2D>1$, but the discrete modes that $|C|+2D>1$ makes unstable lie around $m=nx/2$, where the rate is largest, $|G_{num}(nx/2)|=|1-2|C|-4D|>1$ ($G_{num}$ as defined for `mode_scheme_fidelity` below, taken at any $m$). They are seeded at round-off only and grow by at most $|G_{num}(nx/2)|^{n_{step}}$ (of order $10^3$ for the guard case of 4-1) within the case, so the second term stays below 1 and the evidence is decided by the first term. Each test applies its CFL threshold to this evidence as it applies it to `cfl_combined_max`: in 6-1 to 6-3 the evidence is at most $1.0$, and in 6-4, whose `xfail_condition` is `cfl.combined_max > 1.0`, the evidence is greater than $1.0$.

The relative mass-drift value is defined by the following.
$$
\mathrm{mass\_drift\_rel}=
\frac{|M_{end}-M_0|}{\max(M^{abs}_0,\ 1e{-14})},
\quad
M=\sum_i u_i\,dx,
\quad
M^{abs}_0=\sum_i |u_i(0)|\,dx
$$
Here $M_0$ is the field `conserved.mass.initial`, $M_{end}$ is `conserved.mass.final`, and $M^{abs}_0$ is `conserved.mass.abs_initial`. The signed initial mass of the initial condition in 2-1 is zero, so the normalizer is $M^{abs}_0$ and not $|M_0|$. The reduced value is emitted as the field `metrics.mass_drift_rel`.

The theoretical-comparison errors are defined in three norms by the following, for $k\in\{1,2,\infty\}$ with $\|e\|_1=\sum_i|e_i|$, $\|e\|_2=(\sum_i e_i^2)^{1/2}$ and $\|e\|_\infty=\max_i|e_i|$ over the cells.
$$
\mathrm{l}k\mathrm{\_rel\_error\_to\_analytic\_tend}=
\frac{\|u_{num}(t_{end})-u_{exact}(t_{end})\|_k}{\|u_{exact}(t_{end})\|_k}
$$
Here $u_{exact}$ is the theoretical solution of 2-1 with the case's `a`, and the values are emitted as the fields `errors.analytic.l1_rel_tend`, `errors.analytic.l2_rel_tend` and `errors.analytic.linf_rel_tend`.

The mirror-pair residual is defined by the following, with $u'$ the state at $t_{end}$ of `advdiff1d_ref_nx128_shift000_dts100_amir` and $u$ that of `advdiff1d_ref_nx128_shift000_dts100`, and $\mathrm{flip}(u)_i=u_{nx-1-i}$ (the grammar's `flip(·, 0)`); it is emitted as the field `errors.mirror.l2_rel` on the former.
$$
\mathrm{mirror\_l2\_rel}=\frac{\|u'(t_{end})+\mathrm{flip}(u(t_{end}))\|_2}{\|u(t_{end})\|_2}
$$

The translation-equivariance error is defined by the following.
$$
\mathrm{symmetry\_l2\_rel}=
\frac{\|u_{shifted}(t_{end})-\mathrm{shift}(u_{ref}(t_{end}),+\delta_s)\|_2}{\|u_{ref}(t_{end})\|_2}
$$
Here $u_{ref}$ is the numerical solution of the `reference` case of the pair, $u_{shifted}$ that of the `shifted` case, and $\delta_s=\text{shift\_fraction}\cdot L$ is the shift distance of the `shifted` case. $\delta_s$ is a distance and is not the grid spacing $dx$. The value is emitted as the field `errors.symmetry_l2_rel`.

`convergence_order` is a cross-case reduction over each of `errors.analytic.l1_rel_tend`, `errors.analytic.l2_rel_tend` and `errors.analytic.linf_rel_tend`, using $p=\log(e_{coarse}/e_{fine})/\log(2)$, and is accumulated only over the target cases of the test that judges it. It is emitted as a per-case field of the finer case of each pair: `advdiff1d_ref_nx128_shift000_dts100` carries `convergence.nx64_to_nx128.<k>_order`, and `advdiff1d_ref_nx256_shift000_dts100` carries `convergence.nx128_to_nx256.<k>_order`, for `<k>` each of `l1`, `l2`, `linf`. The cases preceding the one that completes a reduction omit that field.

`mode_scheme_fidelity` is emitted as the field `errors.mode_scheme_fidelity`. It measures whether the kernel advances each discrete Fourier mode by the amplification rate of the discrete update of `controlled_spec.md` §5. That update is linear and periodic, so it advances the mode $m$ by exactly $G_{num}(m)$ per step, and by $G_{num}(m)^{n_{step}}$ over the case, up to round-off.

Let the discrete mode number be $m \in \{1,2\}$, $\theta_m=2\pi m/nx$, and $k_m=2\pi m/L$. The cell centres are $x_i=(i+1/2)\,dx$ for $i=0,\dots,nx-1$.
The dimensionless numbers are $C=a\,dt/dx$ and $D=\nu\,dt/dx^2$, with the `dt` and `n_step` that 3 decides for the case.
Then the 1-step amplification rate is defined by the following.
$$
G_{num}(m)=1-C\left(1-e^{-i\theta_m}\right)+D\left(e^{i\theta_m}-2+e^{-i\theta_m}\right)\quad(a>0)
$$
$$
G_{num}(m)=1-C\left(e^{i\theta_m}-1\right)+D\left(e^{i\theta_m}-2+e^{-i\theta_m}\right)\quad(a<0)
$$
The discrete Fourier coefficient of the state is defined by the following.
$$
\hat u_m(t)=\sum_i u_i(t)\,e^{-ik_m x_i}
$$
`mode_scheme_fidelity` is defined by the following.
$$
\max_{m\in\{1,2\}}\frac{\left|\hat u_m(t_{end})-G_{num}(m)^{n_{step}}\,\hat u_m(0)\right|}{\left|\hat u_m(0)\right|}
$$
Here $\hat u_m(0)$ is the coefficient of the initial state and $\hat u_m(t_{end})$ that of the state at $t_{end}$. $G_{num}(m)^{n_{step}}$ is the complex power: its modulus is $|G_{num}(m)|^{n_{step}}$ and its argument is $n_{step}\arg G_{num}(m)$.

`cross_target_state_agreement` compares this target's numerical solution with every other target's certified variant of this node, case by case (`docs/TESTS.md` §Cross-target judgment). For the state variable `u` at $t_{end}$ of a case,
$$
\mathrm{cross\_target\_state\_agreement}=\frac{\max_i |u_i-u^{ref}_i|}{\max(\max_i|u_i|,\ \max_i|u^{ref}_i|,\ 1)}
$$
Here $u^{ref}$ is `u` at $t_{end}$ of the same case in the other variant. It is evaluated by the host from the captured state, against each other variant separately; it is not a `diagnostics.json` field, and on a target with no other certified variant it holds vacuously.

### 5-5. Default thresholds
- $\text{cfl\_combined\_max} \le 1.0$
- $\text{mass\_drift\_rel} \le 1.0e{-12}$
- The theoretical-comparison errors of the three cases of `advdiff1d_ref` are judged as two-sided bands, `0.9` and `1.1` times the reference value rounded outward to two digits, two distinct judged quantities each (an upper and a lower bound). The Controlled Spec §5 fixes the update, so the error is a property of the discretization: a value below the band is a run that did not perform the §5 update (a state that holds the theoretical solution passes an upper bound alone), and a value above it a wrong or stopped one. The reference values are measured on an independent implementation of the Controlled Spec §5 update, recorded on issue #513 (calibration comment):

| case | `l1` | `l2` | `linf` |
| --- | --- | --- | --- |
| `advdiff1d_ref_nx064_shift000_dts100` | $[1.0e{-1},\ 1.3e{-1}]$ (`1.1118e-1`) | $[1.0e{-1},\ 1.4e{-1}]$ (`1.2157e-1`) | $[1.2e{-1},\ 1.6e{-1}]$ (`1.4044e-1`) |
| `advdiff1d_ref_nx128_shift000_dts100` | $[6.8e{-2},\ 8.4e{-2}]$ (`7.5869e-2`) | $[7.4e{-2},\ 9.1e{-2}]$ (`8.2631e-2`) | $[8.5e{-2},\ 1.1e{-1}]$ (`9.5449e-2`) |
| `advdiff1d_ref_nx256_shift000_dts100` | $[3.9e{-2},\ 4.9e{-2}]$ (`4.4094e-2`) | $[4.3e{-2},\ 5.3e{-2}]$ (`4.7834e-2`) | $[4.9e{-2},\ 6.1e{-2}]$ (`5.5291e-2`) |

- $\text{convergence\_order} \ge 0.50$ in each norm, the bound of the previous version (reference orders `0.551`, `0.557`, `0.557` over `nx64_to_nx128` and `0.783`, `0.789`, `0.788` over `nx128_to_nx256` in `l1`, `l2`, `linf`; the first pair is below one because the diffusion-limited `dt` makes the advective Courant number fall from `0.39` to `0.20`, and the first-order upwind error is not yet in its asymptotic range)
- $\text{mirror\_l2\_rel} \le 1.0e{-12}$ (2-2)
- $\text{mode\_scheme\_fidelity} \le 1.0e{-10}$
- $\text{symmetry\_l2\_rel} \le 1.0e{-12}$
- $\text{cross\_target\_state\_agreement} \le 1.0e{-12}$

## 6. Test definitions
### 6-1. `l1_refinement_against_analytic`
- `level`: `L1`
- `objective`: confirm that the error decreases with refinement and the agreement with the theoretical solution is within tolerance.
- target cases:
  - `advdiff1d_ref_nx064_shift000_dts100`
  - `advdiff1d_ref_nx128_shift000_dts100`
  - `advdiff1d_ref_nx256_shift000_dts100`
- `expected_outcome`: `pass`
- judgment conditions:
  - The CFL judgment is applied. The evaluation expression is `cfl_combined_max`, and the threshold is $\le 1.0$.
  - The mass-conservation judgment is applied. The evaluation expression is `mass_drift_rel`, and the threshold is $\le 1.0e{-12}$.
  - The symmetry judgment is not applied. The non-application basis is "because the translated pair case is not run in this test; the translation pair is judged in 6-3 and the mirror pair in 6-5".
  - The theoretical-comparison judgment is applied.
    - Each of `l1_rel_error_to_analytic_tend`, `l2_rel_error_to_analytic_tend` and `linf_rel_error_to_analytic_tend` applies its per-case band of the 5-5 table as two judged quantities.
    - `convergence_order` uses $p=\log(e_{coarse}/e_{fine})/\log(2)$, and requires $\ge 0.50$ in each norm for both `nx64_to_nx128` and `nx128_to_nx256`.
    - `mode_scheme_fidelity` uses the discrete-mode evaluation expression defined in 5-4, and requires $\le 1.0e{-10}$.
  - The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `u`, and the threshold is $\le 1.0e{-12}$.

### 6-2. `l2_mass_conservation_long_run`
- `level`: `L2`
- `objective`: confirm discrete mass conservation in long-time integration.
- target cases:
  - `advdiff1d_ref_nx128_shift000_dts100_tend200`
- `expected_outcome`: `pass`
- judgment conditions:
  - The CFL judgment is applied. The evaluation expression is `cfl_combined_max`, and the threshold is $\le 1.0$.
  - The mass-conservation judgment is applied. The evaluation expression is `mass_drift_rel`, and the threshold is $\le 3.0e{-12}$.
  - The symmetry judgment is not applied. The non-application basis is "because this test aims at the long-time conservation evaluation of a single initial condition".
  - The theoretical-comparison judgment is applied. It requires $\text{l2\_rel\_error\_to\_analytic\_tend} \le 2.3e{-1}$.
  - The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `u`, and the threshold is $\le 1.0e{-12}$.

### 6-3. `l3_translation_equivariance`
- `level`: `L3`
- `objective`: confirm that the numerical solution translates by the same amount in response to the translation of the initial condition.
- pair cases:
  - `reference` is `advdiff1d_ref_nx128_shift000_dts100`
  - `shifted` is `advdiff1d_sym_nx128_shift025_dts100`
- `expected_outcome`: `pass`
- judgment conditions:
  - The CFL judgment is applied. The evaluation expression is `cfl_combined_max`, and the threshold is $\le 1.0$.
  - The mass-conservation judgment is applied. The evaluation expression is `mass_drift_rel`, and the threshold is $\le 1.0e{-12}$.
  - The symmetry judgment is applied. The evaluation expression is `symmetry_l2_rel`, and the threshold is $\le 1.0e{-12}$.
  - The theoretical-comparison judgment is applied. It requires $\text{l2\_rel\_error\_to\_analytic\_tend} \le 1.1e{-1}$.
  - The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `u`, and the threshold is $\le 1.0e{-12}$.

### 6-4. `l0_cfl_guard_xfail`
- `level`: `L0`
- `objective`: confirm that a CFL-violation case can be detected (expected failure).
- target cases:
  - `advdiff1d_guard_nx064_shift000_dts120`
- `expected_outcome`: `xfail`
- `xfail_condition`: `cfl.combined_max > 1.0`
- `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'cfl'`
- judgment conditions:
  - The CFL judgment is applied. The evaluation expression is `cfl_combined_max`, and the threshold is $\le 1.0$.
  - The mass-conservation judgment is applied. The evaluation expression is `mass_drift_rel`, and the threshold is `informational_only`.
  - The symmetry judgment is not applied. The non-application basis is "because the purpose of the guard test is only the detection of a stability-condition violation".
  - The theoretical-comparison judgment is not applied. The non-application basis is "because under an unstable condition, the cannot-continue-execution judgment is prioritized over the theoretical-agreement judgment".
  - The cross-target judgment is not applied. The non-application basis is "because the state an unstable case leaves at $t_{end}$ is not a result two variants must agree on".

### 6-5. `l3_mirror_pair`
- `level`: `L3`
- `objective`: confirm the mirror equivariance of the Controlled Spec §5: with `a` → `-a` from the same odd initial condition, the numerical solution is the reflected base solution with its sign reversed (2-2), and the $a<0$ update advances each discrete mode by its own amplification rate.
- pair cases:
  - `reference` is `advdiff1d_ref_nx128_shift000_dts100`
  - `mirrored` is `advdiff1d_ref_nx128_shift000_dts100_amir`
- `expected_outcome`: `pass`
- judgment conditions:
  - The CFL judgment is applied. The evaluation expression is `cfl_combined_max`, and the threshold is $\le 1.0$.
  - The mass-conservation judgment is applied. The evaluation expression is `mass_drift_rel`, and the threshold is $\le 1.0e{-12}$.
  - The symmetry judgment is applied. The evaluation expression is `mirror_l2_rel` (carried by the mirrored case), and the threshold is $\le 1.0e{-12}$.
  - The theoretical-comparison judgment is applied to the mirrored case. `mode_scheme_fidelity` uses the discrete-mode evaluation expression of 5-4 with the $a<0$ amplification rate, and requires $\le 1.0e{-10}$. The bands of the errors are not judged on it: it equals the reflection of its base, whose bands are judged in 6-1, to the round-off the pair judgment bounds.
  - The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `u`, and the threshold is $\le 1.0e{-12}$.

## 7. Pass/fail aggregation rules
- `per_test.pass_rule`: pass when all applicable checks pass.
- `per_test.xfail_rule`: xfail when `expected_outcome == xfail` and `xfail_condition` is true and `pass_when` is satisfied.
- `suite.pass_rule`: pass when all `test_id` satisfy `pass_rule` or `xfail_rule`.

## 8. Traceability
### 8-1. Output requirements
- `verdict.json` requires each check's `status`, `metric_value`, `threshold`, and `reason_na` when `applicable=false`.
- `summary.json` requires the counts of `pass`, `fail`, `xfail`, and `skipped`.

### 8-2. Traceability records
- The `test_profile_id` and `test_profile_version` of this document must be recorded under each case's `inputs` in `spec.ir.yaml` and in `trial_meta.json`.
- The judgment conditions of this document must be mappable to the evaluation basis of `verdict.json`.
