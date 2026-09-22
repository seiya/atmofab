# Tests: 2D shallow water Rusanov flux from supplied interface states (L0)

## 0. Meta information
- `test_profile_id`: `dynamics_shallow_water_flux_2d_rusanov_l0`
- `test_profile_version`: `0.1.0`
- `status`: `draft`
- `spec_ref.spec_kind`: `component`
- `spec_ref.spec_id`: `dynamics_shallow_water_flux_2d_rusanov`
- `spec_ref.spec_version`: `0.1.0`
- `spec_ref.controlled_spec_path`: `spec/component/dynamics/shallow_water/dynamics_shallow_water_flux_2d_rusanov/controlled_spec.md`

## 1. Test purpose
This suite verifies the published `operation` `dynamics_shallow_water_flux_2d_rusanov__compute_flux` at `L0`: the consistency of the numerical flux with the physical flux when the two states of an interface are equal, the closed form of the flux and of the wave speeds for three pairs of distinct states, which between them make each of the four states the one that fixes its interface's wave speed and set the larger `|u_n|` against the larger `c` on opposite sides of one interface, the flux of still water, the non-negativity of the wave speeds, and the input guard for a dry state in each of the four supplied states, at `h = 0` and at `h < 0`. The `operation` is pointwise, so every case is one `x` interface and one `y` interface. Two facts the Controlled Spec states are not observed by this suite and are stated by it alone: the ordering clause of §4 (the guard is evaluated before any division by `h`), because a division performed before the guard leaves no trace in the state a rejected case captures; and the call itself, because every judged state variable is written by the checks module, so a checks module that fabricates the outputs instead of calling the `operation` is not excluded here.

## 2. Input-defaulting rules
- Every case uses `g = 9.80616`.
- The equal-state case uses `U_L = U_R = [2.0, 0.6, -0.4]` and `U_B = U_T = [1.5, 0.3, 0.45]`. The physical fluxes are `F(U_L) = [0.6, 19.79232, -0.12]` (`hu`, `hu^2/h + g h^2/2 = 0.18 + 19.61232`, `hu hv/h`) and `G(U_B) = [0.45, 0.09, 11.16693]` (`hv`, `hu hv/h`, `hv^2/h + g h^2/2 = 0.135 + 11.03193`); the wave speeds are `a_x = 0.3 + sqrt(2 g) = 4.728579907826` and `a_y = 0.3 + sqrt(1.5 g) = 4.135262702867`.
- The first distinct-state case uses `U_L = [2.0, -0.6, -0.4]`, `U_R = [1.5, -0.3, 0.45]`, `U_B = [1.2, 0.24, 0.36]`, `U_T = [1.8, 0.9, -0.18]`. The two sides of each interface differ in every component, so the dissipation term is non-zero in every component; on each interface the side with the larger `|u_n| + c` carries a NEGATIVE normal velocity (`u_L = -0.3` on the `x` interface, `v_T = -0.1` on the `y` interface), so the absolute value in the wave speed is exercised. The normal momentum changes sign across the `y` interface (`v_B = 0.3`, `v_T = -0.1`). The closed form of the Controlled Spec §3 gives `a_x = max(0.3 + sqrt(2 g), 0.2 + sqrt(1.5 g)) = max(4.728579907826, 4.035262702867) = 4.728579907826` (the left side), `a_y = max(0.3 + sqrt(1.2 g), 0.1 + sqrt(1.8 g)) = max(3.730363246072, 4.301319792637) = 4.301319792637` (the top side), `F* = [0.732144976956, 14.732838013826, -1.994646460826]` and `G* = [-1.200395937791, -1.428435531570, 12.697563544012]`.
- The second distinct-state case uses `U_L = [1.2, 0.24, -0.36]`, `U_R = [2.0, -0.6, 0.45]`, `U_B = [1.8, 0.9, -0.18]`, `U_T = [1.2, 0.24, 0.36]`. The side with the larger `|u_n| + c` is the RIGHT state on the `x` interface (`4.728579907826` against `3.630363246072`) and the BOTTOM state on the `y` interface (`4.301319792637` against `3.730363246072`) — the opposite side from the first case on each interface — and that side again carries a negative normal velocity (`u_R = -0.3`, `v_B = -0.1`); the normal momentum changes sign across both interfaces (`u_L = 0.2`, `v_T = 0.3`). With the first case it pins the wave-speed contribution of each of the four states: a state's term is the maximum of its interface in exactly one of the two cases, so an error in any one of the four terms changes `a_x` or `a_y` in one case or the other. The closed form gives `a_x = 4.728579907826`, `a_y = 4.301319792637`, `F* = [-2.071431963130, 15.436381161287, -2.018574862670]` and `G* = [1.380395937791, 1.410435531570, 10.374850855988]`.
- The third distinct-state case uses `U_L = [0.5, 1.5, 0.2]`, `U_R = [3.0, -0.3, 0.1]`, `U_B = [0.5, 0.2, 1.5]`, `U_T = [3.0, 0.1, -0.3]`. On each interface the larger `|u_n|` and the larger `c` come from OPPOSITE sides: `|u_L| = 3.0` against `|u_R| = 0.1`, and `c_L = sqrt(0.5 g) = 2.214289953913` against `c_R = sqrt(3 g) = 5.423880529658`. The Rusanov speed of §3 is the larger of the two per-side sums, `max(5.214289953913, 5.523880529658) = 5.523880529658`; the sum of the two maxima, `max(|u_L|,|u_R|) + max(c_L,c_R) = 8.423880529658`, is a different estimate, which §3 does not define and §6 of the Controlled Spec forbids, and which the first two cases cannot tell apart because there the same side is the larger in both terms. The `y` interface is the same pair with the roles of `hu` and `hv` exchanged. The closed form gives `a_x = a_y = 5.523880529658`, `F* = [-6.304850662072, 29.913237476692, 0.571194026483]` and `G* = [-6.304850662072, 0.571194026483, 29.913237476692]`.
- The still-water case uses `U_L = U_R = U_B = U_T = [3.0, 0.0, 0.0]`. The closed form gives `F* = [0, 4.5 g, 0] = [0, 44.12772, 0]`, `G* = [0, 0, 44.12772]`, and `a_x = a_y = sqrt(3 g) = 5.423880529658`.
- The abnormal cases each violate the `h<=0` clause of the Controlled Spec §4 in exactly one of the four states, the other three states being those of the first distinct-state case. There are eight, two per state: one with `h = 0` (the boundary of the clause) and one with `h = -0.5` (its interior), the dry state being `[0.0, 0.0, 0.0]` and `[-0.5, 0.0, 0.0]` respectively. A guard that omits one state accepts both cases of that state; a guard written as `h<0` on one state accepts that state's `h = 0` case; a guard written as `h==0` on one state accepts that state's `h = -0.5` case. Every clause of §4 is therefore pinned on every state and on both sides of its boundary.

## 3. Execution-control rules
`N/A`: this `component` exposes a single pointwise `operation` and defines no time-stepping or iteration. Execution control is the responsibility of the time-update `component` and the `problem` runner.

## 4. Case-expansion rules
`N/A`: the `L0` suite uses fixed single-interface states and defines no `case` sweep. Case expansion is defined at the `problem` level.

## 5. Diagnostics contract
- Require outputting `checks.equal_state_consistency`, `checks.distinct_state_closed_form`, `checks.still_water`, `checks.wave_speed_nonnegative`, `checks.rejected_outputs_zero`, and `checks.input_guard` in `diagnostics.json`.
- The state of a case, captured after setup and after the run, consists of `U_L`, `U_R`, `U_B`, `U_T` (3 values each, the supplied states), `F_star`, `G_star` (3 values each), `a_x`, `a_y` (scalars), and `guard_ok` (scalar: `-1` at setup, before the `operation` is called; `1` when the `operation` returned true; `0` when it returned false). The four states are set at setup; `F_star`, `G_star`, `a_x` and `a_y` are set to the sentinel value `-999` at setup, before the `operation` is called, and are the values the `operation` wrote at the capture after the run — the sentinel is not a value the `operation` produces on any case of this suite, so a captured `0` in these variables is a zero the `operation` wrote, which is what the abnormal cases judge, and a variable the `operation` left untouched captures `-999` and fails; `guard_ok` is set to `-1` at setup and to the value the `operation` returned at the call, recorded as `1` / `0` because a state variable is real-valued. The setup value `-1` is distinct from both values the `operation` can return, so a case whose `operation` was not called, and whose flag was therefore not written after setup, captures `-1`, which no judgment of §6 accepts. This does not establish that the `operation` was called: the flag is a state variable the checks module owns, so a checks module that writes `0` into it without calling the `operation` captures the same value as a rejected input. What the setup value excludes is the omission of the call, not the fabrication of its result; the fabrication of the whole judged state is outside what this suite can observe and is stated in §1. No state variable is derived by the checks module after the run: a residual or a deviation over these variables is a `checks.<id>` status, never a state variable. Every judgment of §6 is a statement about these variables and the §2 inputs.
- Each check is computed on the case named here and is `na` on every other case: `checks.equal_state_consistency` on the equal-state case; `checks.distinct_state_closed_form` on each of the three distinct-state cases, against that case's own closed form; `checks.still_water` on the still-water case; `checks.wave_speed_nonnegative` on every case the guard accepts; `checks.rejected_outputs_zero` on every abnormal case; `checks.input_guard` on every case (`pass` when `guard_ok = 1`, `fail` when `guard_ok = 0` and when `guard_ok = -1`).

## 6. Test definitions
- `test_id`: `l0_equal_state_consistency_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov__compute_flux`
  - `expected_outcome`: `pass`
  - `judgment`: with `U_L = U_R` and `U_B = U_T`, the dissipation term is analytically zero, so `F_star = F(U_L) = [0.6, 19.79232, -0.12]` and `G_star = G(U_B) = [0.45, 0.09, 11.16693]` component-wise within an absolute tolerance of `1e-12` (`max_i |F*_i - F(U_L)_i| <= 1e-12`, and the same for `G*`), and `guard_ok = 1` (`checks.equal_state_consistency`). A model with the momentum flux `hu^2/h` and the pressure `g h^2/2` exchanged between `F` and `G`, or with the pressure `g h^2` or `g h`, fails.
- `test_id`: `l0_distinct_state_closed_form_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov__compute_flux`
  - `expected_outcome`: `pass`
  - `judgment`: on each of the three distinct-state cases of §2, `F_star`, `G_star`, `a_x` and `a_y` equal that case's closed-form values of §2 within an absolute tolerance of `1e-9` (component-wise max deviation `<= 1e-9`), and `guard_ok = 1` (`checks.distinct_state_closed_form`). A model with the dissipation sign reversed (`+ a (U_R - U_L)/2`) gives `F*_1 = -1.632` in the `h` component (the closed form is `-0.45 - 1.182` instead of `-0.45 + 1.182 = 0.732`); a model without the absolute value in the wave speed (`u + c`) gives `a_x = 4.129` and `a_y = 4.101` instead of `4.729` and `4.301`; a model with the minimum of the two sides gives `a_x = 4.035`; a model that drops one of the four states from its wave speed, or evaluates that state's term from another state's `h` or momentum, fails the case in which that state is the maximum of its interface (the first case for `U_L` and `U_T`, the second for `U_R` and `U_B`), so no single state's term is left unpinned; a model that adds the two maxima (`max(|u_L|,|u_R|) + max(c_L,c_R)`) instead of taking the maximum of the two per-side sums gives `8.423880529658` for both wave speeds of the third case, against `5.523880529658`; a model with a per-component wave speed fails the `hu` or `hv` component.
- `test_id`: `l0_still_water_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov__compute_flux`
  - `expected_outcome`: `pass`
  - `judgment`: with the still-water states, the mass flux and the tangential momentum flux are zero and the normal momentum flux is the pressure: `F_star = [0, 44.12772, 0]`, `G_star = [0, 0, 44.12772]`, each component within an absolute tolerance of `1e-9`, `a_x = a_y = sqrt(3 g) = 5.423880529658` within `1e-9`, and `guard_ok = 1` (`checks.still_water`).
- `test_id`: `l0_wave_speed_nonnegative_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov__compute_flux`
  - `expected_outcome`: `pass`
  - `judgment`: on the equal-state, the three distinct-state and the still-water cases, the computed wave speeds satisfy `a_x >= 0` and `a_y >= 0` (`checks.wave_speed_nonnegative`).
- `test_id`: `l0_invalid_dry_state_l_zero_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov__compute_flux`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `h==0` in `U_L`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`
  - `judgment`: with `U_L = [0, 0, 0]` and the other three states those of the first distinct-state case, the `operation` returns `guard_ok = 0` (`checks.input_guard` is `fail`, which makes the case's `verdict.overall` `fail`) and writes `0` into `F_star`, `G_star`, `a_x` and `a_y` (`checks.rejected_outputs_zero`: every component of `F_star` and `G_star` and both wave speeds are exactly `0`, none of them the setup sentinel `-999`). The captured `guard_ok` is `0`, not the setup value `-1`. A model whose guard on `U_L` is `h<0` fails this test. A model that computes a flux from the rejected input, and a checks module that does not call the `operation` on this case and leaves the flag at its setup value, both fail this test.
- `test_id`: `l0_invalid_dry_state_l_negative_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov__compute_flux`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `h<0` in `U_L`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`
  - `judgment`: with `U_L = [-0.5, 0, 0]` and the other three states those of the first distinct-state case, the `operation` returns `guard_ok = 0` (`checks.input_guard` is `fail`, which makes the case's `verdict.overall` `fail`) and writes `0` into `F_star`, `G_star`, `a_x` and `a_y` (`checks.rejected_outputs_zero`: every component of `F_star` and `G_star` and both wave speeds are exactly `0`, none of them the setup sentinel `-999`). The captured `guard_ok` is `0`, not the setup value `-1`. A model whose guard on `U_L` is `h==0` fails this test. A model that computes a flux from the rejected input, and a checks module that does not call the `operation` on this case and leaves the flag at its setup value, both fail this test.
- `test_id`: `l0_invalid_dry_state_r_zero_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov__compute_flux`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `h==0` in `U_R`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`
  - `judgment`: with `U_R = [0, 0, 0]` and the other three states those of the first distinct-state case, the `operation` returns `guard_ok = 0` (`checks.input_guard` is `fail`, which makes the case's `verdict.overall` `fail`) and writes `0` into `F_star`, `G_star`, `a_x` and `a_y` (`checks.rejected_outputs_zero`: every component of `F_star` and `G_star` and both wave speeds are exactly `0`, none of them the setup sentinel `-999`). The captured `guard_ok` is `0`, not the setup value `-1`. A model whose guard omits `U_R`, or whose guard on `U_R` is `h<0`, fails this test. A model that computes a flux from the rejected input, and a checks module that does not call the `operation` on this case and leaves the flag at its setup value, both fail this test.
- `test_id`: `l0_invalid_dry_state_r_negative_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov__compute_flux`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `h<0` in `U_R`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`
  - `judgment`: with `U_R = [-0.5, 0, 0]` and the other three states those of the first distinct-state case, the `operation` returns `guard_ok = 0` (`checks.input_guard` is `fail`, which makes the case's `verdict.overall` `fail`) and writes `0` into `F_star`, `G_star`, `a_x` and `a_y` (`checks.rejected_outputs_zero`: every component of `F_star` and `G_star` and both wave speeds are exactly `0`, none of them the setup sentinel `-999`). The captured `guard_ok` is `0`, not the setup value `-1`. A model whose guard omits `U_R` fails this test. A model that computes a flux from the rejected input, and a checks module that does not call the `operation` on this case and leaves the flag at its setup value, both fail this test.
- `test_id`: `l0_invalid_dry_state_b_zero_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov__compute_flux`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `h==0` in `U_B`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`
  - `judgment`: with `U_B = [0, 0, 0]` and the other three states those of the first distinct-state case, the `operation` returns `guard_ok = 0` (`checks.input_guard` is `fail`, which makes the case's `verdict.overall` `fail`) and writes `0` into `F_star`, `G_star`, `a_x` and `a_y` (`checks.rejected_outputs_zero`: every component of `F_star` and `G_star` and both wave speeds are exactly `0`, none of them the setup sentinel `-999`). The captured `guard_ok` is `0`, not the setup value `-1`. A model whose guard omits `U_B`, or whose guard on `U_B` is `h<0`, fails this test. A model that computes a flux from the rejected input, and a checks module that does not call the `operation` on this case and leaves the flag at its setup value, both fail this test.
- `test_id`: `l0_invalid_dry_state_b_negative_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov__compute_flux`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `h<0` in `U_B`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`
  - `judgment`: with `U_B = [-0.5, 0, 0]` and the other three states those of the first distinct-state case, the `operation` returns `guard_ok = 0` (`checks.input_guard` is `fail`, which makes the case's `verdict.overall` `fail`) and writes `0` into `F_star`, `G_star`, `a_x` and `a_y` (`checks.rejected_outputs_zero`: every component of `F_star` and `G_star` and both wave speeds are exactly `0`, none of them the setup sentinel `-999`). The captured `guard_ok` is `0`, not the setup value `-1`. A model whose guard omits `U_B` fails this test. A model that computes a flux from the rejected input, and a checks module that does not call the `operation` on this case and leaves the flag at its setup value, both fail this test.
- `test_id`: `l0_invalid_dry_state_t_zero_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov__compute_flux`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `h==0` in `U_T`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`
  - `judgment`: with `U_T = [0, 0, 0]` and the other three states those of the first distinct-state case, the `operation` returns `guard_ok = 0` (`checks.input_guard` is `fail`, which makes the case's `verdict.overall` `fail`) and writes `0` into `F_star`, `G_star`, `a_x` and `a_y` (`checks.rejected_outputs_zero`: every component of `F_star` and `G_star` and both wave speeds are exactly `0`, none of them the setup sentinel `-999`). The captured `guard_ok` is `0`, not the setup value `-1`. A model whose guard omits `U_T`, or whose guard on `U_T` is `h<0`, fails this test. A model that computes a flux from the rejected input, and a checks module that does not call the `operation` on this case and leaves the flag at its setup value, both fail this test.
- `test_id`: `l0_invalid_dry_state_t_negative_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov__compute_flux`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `h<0` in `U_T`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`
  - `judgment`: with `U_T = [-0.5, 0, 0]` and the other three states those of the first distinct-state case, the `operation` returns `guard_ok = 0` (`checks.input_guard` is `fail`, which makes the case's `verdict.overall` `fail`) and writes `0` into `F_star`, `G_star`, `a_x` and `a_y` (`checks.rejected_outputs_zero`: every component of `F_star` and `G_star` and both wave speeds are exactly `0`, none of them the setup sentinel `-999`). The captured `guard_ok` is `0`, not the setup value `-1`. A model whose guard omits `U_T` fails this test. A model that computes a flux from the rejected input, and a checks module that does not call the `operation` on this case and leaves the flag at its setup value, both fail this test.

## 7. Pass/fail aggregation rules
- `per_test.pass_rule`: `pass` when the judgment expression is satisfied.
- `per_test.xfail_rule`: `xfail` when `xfail_condition` is true and `pass_when` is satisfied.
- `suite.pass_rule`: `pass` when all `test_id` are `pass` or `xfail`.

## 8. Traceability
- Record `test_profile_id` and `test_profile_version` in `trial_meta.json`.
