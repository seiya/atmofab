# Tests: 2D shallow water TC4 manufactured forcing (L0)

## 0. Meta information
- `test_profile_id`: `dynamics_shallow_water_source_2d_tc4_forcing_l0`
- `test_profile_version`: `0.1.0`
- `status`: `draft`
- `spec_ref.spec_kind`: `component`
- `spec_ref.spec_id`: `dynamics_shallow_water_source_2d_tc4_forcing`
- `spec_ref.spec_version`: `0.1.0`
- `spec_ref.controlled_spec_path`: `spec/component/dynamics/shallow_water/dynamics_shallow_water_source_2d_tc4_forcing/controlled_spec.md`

## 1. Test purpose
This suite verifies the published `operation` `dynamics_shallow_water_source_2d_tc4_forcing__apply` at `L0`: that the published forcing equals externally evaluated reference values on a fixed grid; that it is the residual of the forced 2D shallow water equations for the prescribed flow of the Controlled Spec §3, at two times; that a zero low leaves the geostrophic background unforced; that the forcing translates with the low; and the input guard for each clause of the Controlled Spec §4 that a captured state can observe — an invalid component count (`ncomp/=3`, below and above three), a non-positive radius (`R_low<=0`, at zero and below), and a non-positive channel length (`L_x<=0`, at zero and below) — together with the zero-filled `S` §4 requires on a rejected input.

Three clauses of the Controlled Spec are stated there and not observed here. The extent clauses `nx<1` / `ny<1` have no case: a state of zero extent has no snapshot the judgment could read. The clause that no element of the input arrays is read on a rejected input has none either: a read leaves no trace in the captured state. And the divergence term $\tilde h\,(\tilde u_x+\tilde v_y)$ of $F_h$ is zero at every cell of every case, because the prescribed flow of the Controlled Spec §3 has zero divergence, so its presence in the operation is not observable by any case of this suite — a model that omits the term produces the same `S` on every case here.

Two further properties of the Controlled Spec are stated there and not observed here. The ORDER of the guard is one: a model that evaluates the forcing on a rejected input and then overwrites `S` with zeros satisfies every abnormal case of this suite, because the overwritten values are all the captured state holds and the compiler flags the host passes (`-std=<standard> -O2`, no floating-point trapping and no `-fcheck`) let the intermediate division by `R_low^2` or by `L_x` produce a non-finite value without a fault. What the abnormal cases judge is the published output and the flag, not when the guard ran. The identical cancellations of the Controlled Spec §3 are the other: $g\tilde h_x-f\tilde v$ is zero and $g\tilde h_y+f\tilde u$ is $f'\psi$ for the prescribed flow, so a model that writes either residual in its reduced form produces the same `S` on every case of this suite, bit for bit, as one that evaluates it as written.

The §2 precondition that the background is geostrophic is a precondition on the caller's input and not a judgment of this suite: every case of this suite supplies a balanced background, and no case supplies an unbalanced one, so what the operation does with an unbalanced background is not observed.

The two instruments answer different questions, and neither answers the other's. The finite-difference case compares the published forcing against a residual the checks module builds from the same closed form of $\psi$ that the Controlled Spec §3 states, so a reading of $\psi$ that is wrong in the same way on both sides is invisible to it: with `R_low` a tenth of its value, `psi0` halved or sign-reversed, `y_c` or `x_c0` displaced, or the Gaussian written as $e^{-r^2/(2R_{low}^2)}$, the residual stays at `5.6e-9` to `6.6e-8`, under the threshold. The reference-values case is what pins the shape of $\psi$ and its four parameters: its expected array is a declared input of the case, evaluated outside this suite, and each of those six misreadings deviates from it by at least `3.8`.

The call itself is not established either: `S` is a state variable the checks module owns, and the finite-difference reference `S_fd` is computed by the checks module, so a checks module that fabricates both instead of calling the `operation` is not excluded here. The setup sentinel of §5 excludes the cheaper route — omitting the call and leaving the sentinel — on every case.

## 2. Input-defaulting rules
- Every case uses `ncomp=3`, `nx=8`, `ny=6`, the channel `L_x=8.0e6` and a channel width `L_y=8.0e6` (`L_y` is not an argument of the `operation`: it fixes the checks module's own grid and background), the interior cell-centre coordinates `x(i) = (i-0.5)*L_x/nx` (`i=1..8`, so `dx=1.0e6` and `x = 5.0e5, 1.5e6, ..., 7.5e6`) and `y(j) = (j-0.5)*L_y/ny` (`j=1..6`, so `dy = 4.0e6/3` and `y = 2.0e6/3, 2.0e6, 1.0e7/3, 1.4e7/3, 6.0e6, 2.2e7/3`), and `g=9.80616`.
- The background is a `β`-plane channel background in geostrophic balance, carrying the constants of the Williamson (1992) test case 4 and its jet in a channel form — the paper's zonal jet `u0*sin(2*theta)^14` has no channel form and is replaced by `u0*sin(k_y*y)^2`, which is what makes `hbar` a closed form. The constants are `Omega=7.292e-5`, `a=6.37122e6`, `theta0=pi/4`, `u0=40.0`, `gh0=1.0e5`, `k_y=pi/L_y`, `f0=2*Omega*sin(theta0)`, `beta=2*Omega*cos(theta0)/a` and `h0=gh0/g`: `f(j) = f0 + beta*(y(j)-L_y/2)`, `dfdy(j) = beta`, `ubar(j) = u0*sin(k_y*y(j))^2`, `dubar(j) = u0*k_y*sin(2*k_y*y(j))`, and
  `hbar(j) = h0 - (u0/g)*( f0*(y/2 - sin(2*k_y*y)/(4*k_y)) + beta*( y^2/4 - L_y*y/4 - (y-L_y/2)*sin(2*k_y*y)/(4*k_y) - (cos(2*k_y*y)-1)/(8*k_y^2) ) )` at `y=y(j)`.
  Numerically `f0 = 1.0312445296824608e-4`, `beta = 1.618598211461009e-11`, `h0 = 10197.671667604853`, and, over `j=1..6`, `f = 4.91711793e-5, 7.07524887e-5, 9.23337982e-5, 1.13915108e-4, 1.35496417e-4, 1.57077727e-4`, `ubar = 2.67949192, 20.0, 37.32050808, 37.32050808, 20.0, 2.67949192`, `hbar = 10194.82445119, 10105.30549663, 9773.36760160, 9225.13791297, 8728.41163610, 8524.85682947`. The background derivative satisfies `g*dhbar/dy = -f*ubar` exactly by construction, which is the §2 precondition. `hbar` differs from row to row and `f` is strictly increasing, so a row index taken from the wrong row is visible; `ubar` alone does not show it, because `u0*sin(k_y*y)^2` is symmetric about the channel centre and takes only three distinct values over these six rows (rows `1` and `6`, `2` and `5`, `3` and `4` are pairwise equal).
- The low uses `psi0 = -0.03*gh0/f0 = -2.9091063405919403e7` (the paper's amplitude) and `R_low = 2*a/sqrt(sigma) = 1.0e6` with `sigma = (12.74244)^2`, which is the small-distance form of the paper's `tan(d/(2*a))^2` argument; the translation speed is `c_tr = u0*cos(theta0) = 28.284271247461902`, the zonal speed at `theta0` of the paper's angular translation `u0/a`, and the centre at `t=0` is `x_c0 = 4.0e6`, `y_c = 4.0e6`. `x_c0` is not the coordinate of any cell centre and `y_c` is not the coordinate of any row centre, so the low is resolved by the grid rather than sampled at its peak.
- The reference-values case uses its own grid: `ncomp=3`, `nx=3`, `ny=3`, `t=0.0`, the coordinates `x = 5.0e5, 3.5e6, 5.5e6` and `y = 2.0e6, 3.5e6, 5.5e6` (no two of which are at equal distances from `x_c0` or `y_c`, so a reflected or displaced centre is visible), and the same `g`, `L_x` and low as the other cases. Its background is the §2 background evaluated at those three rows and stated as the case's input to twelve significant digits: `f = 7.0752488739e-05, 9.50314619109e-05, 0.00012740342614`, `dfdy = 1.61859821146e-11, 1.61859821146e-11, 1.61859821146e-11`, `ubar = 20, 38.4775906502, 27.6536686473`, `dubar = 1.57079632679e-05, 6.01117729884e-06, -1.45122657607e-05`, `hbar = 10105.3054966, 9712.97798835, 8888.17989701`.
- The expected `S` of the reference-values case is a declared input of that case, stated here in full as an array, evaluated outside this suite from the Controlled Spec §3 formulas at the inputs above, to twelve significant digits (`S(c,i,j)`, the three rows of each block being `i=1,2,3` and the three values of a row `j=1..3`):
  `S(1,i,j)`: `2.07076849205e-07, 1.18296981454e-05, 2.14582504133e-06` / `8.30660437744e-05, 0.00523216536385, 0.000867094839494` / `-3.45885341571e-05, -0.00201068048463, -0.000358873632824`.
  `S(2,i,j)`: `-0.000496688715314, 0.00224538485829, 0.000911289200366` / `-0.194966484055, 6.80898501816, 0.455993894103` / `0.0826581315973, -0.802271006614, -0.158692603742`.
  `S(3,i,j)`: `7.34530782137e-05, -0.0192583286369, -0.000766326727261` / `-0.0864540543592, 4.24630440729, -0.639232235064` / `0.0171255378033, -1.866172301, -0.052353315734`.
  Stating the values to twelve significant digits places them within `6.8e-12` of the values the formulas give in double precision, and evaluating them from the twelve-digit inputs above rather than from the unrounded background shifts them by at most `3.1e-12`; both are far below the threshold of §6.
- The two finite-difference cases use this background and low at `t=0.0` and at `t=3.0e4`. The low's centre is at `x=4.0e6` in the first and at `x=4.848528137e6` in the second, so the two cases sample it at different positions relative to the grid, and a time-dependent term evaluated at a fixed time is visible in one of the two.
- The zero-low case uses the same inputs with `psi0=0.0` at `t=0.0`.
- The translation case uses the same inputs and calls the `operation` twice: once at `t=0.0` and once at `t=106066.017177982`, which is `3*dx/c_tr` to fifteen significant digits — the time the low needs to translate exactly three cells. The same time rounded to ten significant digits (`106066.0172`) moves the judged residual of §6 to `1.7e-9`, above its threshold, so the value is stated to fifteen.
- The abnormal cases each violate exactly one clause of the Controlled Spec §4, with the background, low and `t=0.0` of the first finite-difference case. The two `ncomp` cases use `ncomp=2` (with `S` allocated with two components) and `ncomp=4` (four components). The two `R_low` cases use `R_low=0.0` and `R_low=-1.0e6`. The two `L_x` cases use `L_x=0.0` and `L_x=-8.0e6`. Each clause is violated on both sides of its boundary so that a guard written as `R_low<0` accepts the zero case, and one written as `R_low/=0` accepts the negative case; the same for `L_x`, and `ncomp` is violated below and above three.
- The finite-difference reference `S_fd` is built by the checks module at setup, before the `operation` is called, from the prescribed flow of the Controlled Spec §3 evaluated by the checks module's own closed forms: with `h(x,y,t) = hbar(y) + f(y)*psi(x,y,t)/g`, `u(x,y,t) = ubar(y) - psi_y(x,y,t)` and `v(x,y,t) = psi_x(x,y,t)`, and the conservation-form fluxes `Fx = [h*u, h*u*u + g*h*h/2, h*u*v]` and `Fy = [h*v, h*u*v, h*v*v + g*h*h/2]`, the reference at the cell `(i,j)` is
  `F_fd = (U(t+dt) - U(t-dt))/(2*dt) + (Fx(x+d) - Fx(x-d))/(2*d) + (Fy(y+d) - Fy(y-d))/(2*d) - S_cor`, with `U = [h, h*u, h*v]`, the Coriolis source `S_cor = [0, f*h*v, -f*h*u]` at the cell, `d=50.0` m and `dt=1.0` s. The differences are evaluated at the shifted positions from the closed forms, not from an array, so no grid boundary enters. The truncation error of that reference over the cells of these cases is `2.2e-10` in the `h` component, `4.7e-8` in the `hu` component and `4.4e-8` in the `hv` component at `t=0` (`2.3e-10`, `4.8e-8`, `5.4e-8` at `t=3.0e4`), which is what the threshold of §6 is set above.

## 3. Execution-control rules
`N/A`: this `component` evaluates a forcing at a supplied time and defines no time-stepping or iteration. The translation case's two calls are a fixed input of §2 and are driven by the checks module, not by the `component`. Execution control is the responsibility of the `problem` runner.

## 4. Case-expansion rules
`N/A`: the `L0` suite uses fixed inputs and defines no `case` sweep. Case expansion is defined at the `problem` level.

## 5. Diagnostics contract
- Require outputting `checks.reference_values`, `checks.fd_residual`, `checks.zero_low`, `checks.translation`, `checks.rejected_outputs_zero`, and `checks.input_guard` in `diagnostics.json`.
- The state of a case, captured after setup and after the run, consists of `S` (`ncomp` × `nx` × `ny`, the array the `operation` wrote at the call of a case that makes one), `S_fd` (`3` × `nx` × `ny`, the finite-difference reference of §2, written at setup and not written again), `S_t0` and `S_tau` (`3` × `nx` × `ny` each, the arrays the `operation` wrote at the two calls of the translation case), and `guard_pass` (scalar: `-1` at setup, before the `operation` is called; `1` when the last call returned true; `0` when it returned false). Every element of `S`, `S_t0` and `S_tau` is set to the sentinel value `-999` at setup, and `S_fd` is set to the sentinel on every case but the two finite-difference cases, which are the only cases that build it. The sentinel is not a value the `operation` produces on any case of this suite, so a captured `0` in `S` is a zero the `operation` wrote, which is what the abnormal cases judge, and an array the `operation` left untouched captures `-999` and fails every judgment of §6 that reads it. `S` keeps the sentinel on the translation case, which writes `S_t0` and `S_tau` instead, and `S_t0` / `S_tau` keep it on every other case; no judgment reads an array on a case that does not write it.
- `guard_pass` is set to `-1` at setup and to the value the `operation` returned at the call, recorded as `1` / `0` because a state variable is real-valued. The setup value `-1` is distinct from both values the `operation` can return, so a case whose `operation` was not called captures `-1`, which no judgment of §6 accepts. This does not establish that the `operation` was called: the flag is a state variable the checks module owns, so a checks module that writes `0` into it without calling the `operation` captures the same value as a rejected input. What the setup value excludes is the omission of the call, not the fabrication of its result; the fabrication of the whole judged state is outside what this suite can observe and is stated in §1.
- No state variable is derived by the checks module after the run: a deviation, a maximum or a norm over these variables is a `checks.<id>` status, never a state variable. `S_fd` is derived before the run, from the §2 closed forms and the §2 inputs alone, and no element of it is read from an output of the `operation`. The expected array of the reference-values case is not a state variable at all: it is a declared input of the case, stated in §2, and the judgment compares the captured `S` to it element by element rather than through a sum, a minimum or a maximum, which a permutation of the elements would satisfy. Every judgment of §6 is a statement about these variables and the §2 inputs.
- Each check is computed on the case named here and is `na` on every other case: `checks.reference_values` on the reference-values case; `checks.fd_residual` on each of the two finite-difference cases, against that case's own time; `checks.zero_low` on the zero-low case; `checks.translation` on the translation case; `checks.rejected_outputs_zero` on every abnormal case (`pass` when every element of `S` equals `0` exactly, `fail` otherwise, the setup sentinel included); `checks.input_guard` on every case (`pass` when `guard_pass = 1`, `fail` when `guard_pass = 0` and when `guard_pass = -1`).

## 6. Test definitions
- `test_id`: `l0_reference_values_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_source_2d_tc4_forcing__apply`
  - `expected_outcome`: `pass`
  - `judgment`: on the reference-values case, the captured `S` equals the expected array of §2 element by element: the maximum over the components and cells of `|S - S_expected|` is `<= 1.0e-8`, and `guard_pass = 1` (`checks.reference_values`). A correct implementation deviates by at most `6.8e-12`, the rounding of the stated values. Each of the six misreadings of the low named in §1 deviates by at least `3.8`: `R_low` a tenth of its value by `6.8`, `psi0` halved by `4.9`, `psi0` sign-reversed by `3.9`, `y_c` moved to `2.0e6` by `7.5`, `x_c0` moved to `1.0e6` by `6.8`, and the Gaussian written as `exp(-r^2/(2*R_low^2))` by `4.4`; a stationary low (`c_tr=0`) deviates by `4.4`. None of those is excluded by the finite-difference cases, which read the same closed form of the low that the model does.
- `test_id`: `l0_forcing_matches_fd_residual_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_source_2d_tc4_forcing__apply`
  - `expected_outcome`: `pass`
  - `judgment`: at `t=0.0`, the published forcing equals the finite-difference residual of §2: the maximum over the components and cells of `|S - S_fd|` is `<= 1.0e-6`, and `guard_pass = 1` (`checks.fd_residual`). The measured value is `4.7e-8`, the truncation error of the reference. The threshold is `2.0e-4` of the maximum of `|F_h|` over the cells (`5.0e-3`), which is the smallest of the three components, so each component is pinned to at least that relative accuracy; the `hu` and `hv` components have maxima of `4.8` and `5.9`. A model that omits the time derivative of the prescribed velocity leaves a deviation of `5.2`, one that omits the time derivative of `v` leaves `3.6`, one that omits the time derivative of `h` leaves `0.21`, one that drops the `f'*psi` term of `h_y` leaves `2.3`, one that drops `ubar'` from `u_y` leaves `1.1`, one that writes `F_2` as `h*F_u` without the `u*F_h` term leaves `0.23`, and one that exchanges the two momentum components leaves `9.2`; each fails this judgment.
- `test_id`: `l0_forcing_matches_fd_residual_late_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_source_2d_tc4_forcing__apply`
  - `expected_outcome`: `pass`
  - `judgment`: at `t=3.0e4`, the same as `l0_forcing_matches_fd_residual_pass`: the maximum over the components and cells of `|S - S_fd|` is `<= 1.0e-6`, and `guard_pass = 1` (`checks.fd_residual`). The measured value is `5.4e-8`. The low's centre has moved `8.485e5` m east of `x_c0`, so a model that evaluates the low at the fixed time `0` deviates by `9.1` here while agreeing with the reference to `4.7e-8` at `t=0`; the deviations of the two time-derivative mutants named in `l0_forcing_matches_fd_residual_pass` are `5.6` and `6.5` here. Together with the case at `t=0` this judges the forcing at two positions of the low relative to the grid.
- `test_id`: `l0_zero_low_gives_zero_forcing_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_source_2d_tc4_forcing__apply`
  - `expected_outcome`: `pass`
  - `judgment`: with `psi0=0.0`, the maximum over the components and cells of `|S|` is `<= 1.0e-8`, and `guard_pass = 1` (`checks.zero_low`). The prescribed flow is then the geostrophic background, which the forced equations hold as a steady solution, and the closed form of §3 cancels to zero exactly; the threshold is the rounding of that cancellation, whose two terms have a maximum of `4.3e-3` over the rows. A model whose `h_y` carries the geostrophic term with the wrong sign leaves `8.5e-3` in `F_v` and `78` in `S_3`, and a model that drops the term leaves half of each (`4.3e-3` and `39`); each fails this judgment. This judgment does not read `S_fd`, which is the sentinel on this case.
- `test_id`: `l0_translation_covariance_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_source_2d_tc4_forcing__apply`
  - `expected_outcome`: `pass`
  - `judgment`: the forcing at `t=106066.017177982` is the forcing at `t=0.0` shifted three cells east: the maximum over the components and cells of `|S_tau - roll(S_t0, 3)|`, where `roll` shifts the `x` index by `+3` cyclically, divided by the maximum of `|S_t0|`, is `<= 1.0e-9`, and `guard_pass = 1` (`checks.translation`). The measured value is `1.1e-14`, at the fifteen-digit time stated above; at the exact `3*dx/c_tr` it is `2.1e-17`, and the difference is the rounding of the time. A model that translates the low westward (`c_tr` replaced by `-c_tr` wherever it appears in the Controlled Spec §3) gives `1.33` for the same expression, one that leaves the low stationary (`c_tr` replaced by `0`) gives `1.02`, and one whose `x` dependence is the Gaussian of `x-x_c(t)` rather than the periodic argument of §3 gives `0.25`; each fails this judgment. The denominator is `5.874` on this case, so the ratio is not formed on a vanishing maximum. This judgment does not read `S` or `S_fd`, which are the sentinel on this case.
- `test_id`: `l0_invalid_ncomp_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_source_2d_tc4_forcing__apply`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `ncomp<3`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard' and checks.rejected_outputs_zero.status == pass`
  - `judgment`: with `ncomp=2`, the `operation` returns `guard_pass = 0` (`checks.input_guard` is `fail`, which makes the case's `verdict.overall` `fail`) and writes exactly `0` into every element of `S` (`checks.rejected_outputs_zero`: every element of `S` equals `0` exactly, so no element is the setup sentinel `-999` and none is a value the operation computed from the rejected input). The captured `guard_pass` is `0`, not the setup value `-1`. A model without this guard clause fails this test. A checks module that does not call the `operation` on this case and leaves the flag at its setup value fails this test.
- `test_id`: `l0_invalid_ncomp_above_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_source_2d_tc4_forcing__apply`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `ncomp>3`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard' and checks.rejected_outputs_zero.status == pass`
  - `judgment`: with `ncomp=4`, the same as `l0_invalid_ncomp_xfail`: `guard_pass = 0` and every element of `S` is exactly `0`, not the setup sentinel. A model whose clause is `ncomp<3` fails this test.
- `test_id`: `l0_invalid_r_low_zero_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_source_2d_tc4_forcing__apply`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `R_low==0`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard' and checks.rejected_outputs_zero.status == pass`
  - `judgment`: with `R_low=0.0`, the `operation` returns `guard_pass = 0` (`checks.input_guard` is `fail`, which makes the case's `verdict.overall` `fail`) and writes exactly `0` into every element of `S` (`checks.rejected_outputs_zero`: every element of `S` equals `0` exactly, so no element is the setup sentinel `-999` and none is a value the operation computed from the rejected input). The captured `guard_pass` is `0`, not the setup value `-1`. A model whose clause is `R_low<0` fails this test.
- `test_id`: `l0_invalid_r_low_negative_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_source_2d_tc4_forcing__apply`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `R_low<0`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard' and checks.rejected_outputs_zero.status == pass`
  - `judgment`: with `R_low=-1.0e6`, the same as `l0_invalid_r_low_zero_xfail`: `guard_pass = 0` and every element of `S` is exactly `0`, not the setup sentinel. A model whose clause is `R_low==0` fails this test: a negative radius gives the same `R_low^2` as its positive counterpart, so nothing else in the outputs distinguishes it.
- `test_id`: `l0_invalid_l_x_zero_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_source_2d_tc4_forcing__apply`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `L_x==0`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard' and checks.rejected_outputs_zero.status == pass`
  - `judgment`: with `L_x=0.0`, the `operation` returns `guard_pass = 0` (`checks.input_guard` is `fail`, which makes the case's `verdict.overall` `fail`) and writes exactly `0` into every element of `S` (`checks.rejected_outputs_zero`: every element of `S` equals `0` exactly, so no element is the setup sentinel `-999` and none is a value the operation computed from the rejected input). The captured `guard_pass` is `0`, not the setup value `-1`. A model whose clause is `L_x<0` fails this test.
- `test_id`: `l0_invalid_l_x_negative_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_source_2d_tc4_forcing__apply`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `L_x<0`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard' and checks.rejected_outputs_zero.status == pass`
  - `judgment`: with `L_x=-8.0e6`, the same as `l0_invalid_l_x_zero_xfail`: `guard_pass = 0` and every element of `S` is exactly `0`, not the setup sentinel. A model whose clause is `L_x==0` fails this test.

## 7. Pass/fail aggregation rules
- `per_test.pass_rule`: `pass` when the judgment expression is satisfied.
- `per_test.xfail_rule`: `xfail` when `xfail_condition` is true and `pass_when` is satisfied.
- `suite.pass_rule`: `pass` when all `test_id` are `pass` or `xfail`.

## 8. Traceability
- Record `test_profile_id` and `test_profile_version` in `trial_meta.json`.
