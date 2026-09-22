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
This suite verifies the published `operation` `dynamics_shallow_water_flux_2d_rusanov__compute_flux` at `L0`: the consistency of the numerical flux with the physical flux when the two states of an interface are equal, the closed form of the flux and of the wave speeds for two distinct states, the flux of still water, the non-negativity of the wave speeds, and the input guard for a dry state (`h<=0`) in each of the four supplied states. The `operation` is pointwise, so every case is one `x` interface and one `y` interface.

## 2. Input-defaulting rules
- Every case uses `g = 9.80616`.
- The equal-state case uses `U_L = U_R = [2.0, 0.6, -0.4]` and `U_B = U_T = [1.5, 0.3, 0.45]`. The physical fluxes are `F(U_L) = [0.6, 19.79232, -0.12]` (`hu`, `hu^2/h + g h^2/2 = 0.18 + 19.61232`, `hu hv/h`) and `G(U_B) = [0.45, 0.09, 11.16693]` (`hv`, `hu hv/h`, `hv^2/h + g h^2/2 = 0.135 + 11.03193`); the wave speeds are `a_x = 0.3 + sqrt(2 g) = 4.728579907826` and `a_y = 0.3 + sqrt(1.5 g) = 4.135262702867`.
- The distinct-state case uses `U_L = [2.0, -0.6, -0.4]`, `U_R = [1.5, -0.3, 0.45]`, `U_B = [1.2, 0.24, -0.36]`, `U_T = [1.8, 0.9, -0.18]`. The two sides of each interface differ in every component, so the dissipation term is non-zero in every component; on each interface the side with the larger `|u_n| + c` carries a NEGATIVE normal velocity (`u_L = -0.3` on the `x` interface, `v_T = -0.1` on the `y` interface), so the absolute value in the wave speed is exercised. The closed form of the Controlled Spec §3 gives `a_x = max(0.3 + sqrt(2 g), 0.2 + sqrt(1.5 g)) = max(4.728579907826, 4.035262702867) = 4.728579907826` (the left side), `a_y = max(0.3 + sqrt(1.2 g), 0.1 + sqrt(1.8 g)) = max(3.730363246072, 4.301319792637) = 4.301319792637` (the top side), `F* = [0.732144976956, 14.732838013826, -1.994646460826]` and `G* = [-1.560395937791, -1.500435531570, 11.149088418663]`.
- The still-water case uses `U_L = U_R = U_B = U_T = [3.0, 0.0, 0.0]`. The closed form gives `F* = [0, 4.5 g, 0] = [0, 44.12772, 0]`, `G* = [0, 0, 44.12772]`, and `a_x = a_y = sqrt(3 g) = 5.423880529658`.
- The abnormal cases each violate the `h<=0` clause of the Controlled Spec §4 in exactly one of the four states, the other three states being those of the distinct-state case, one case per state, the boundary `h = 0` and the strict `h < 0` alternating between the states: `U_L = [0.0, 0.0, 0.0]` (`h = 0`), `U_R = [-0.5, 0.0, 0.0]` (`h < 0`), `U_B = [0.0, 0.0, 0.0]` (`h = 0`), `U_T = [-0.5, 0.0, 0.0]` (`h < 0`). A guard that omits any one state accepts that state's case; a guard written as `h<0` on any state accepts the `h = 0` case of that state.

## 3. Execution-control rules
`N/A`: this `component` exposes a single pointwise `operation` and defines no time-stepping or iteration. Execution control is the responsibility of the time-update `component` and the `problem` runner.

## 4. Case-expansion rules
`N/A`: the `L0` suite uses fixed single-interface states and defines no `case` sweep. Case expansion is defined at the `problem` level.

## 5. Diagnostics contract
- Require outputting `checks.equal_state_consistency`, `checks.distinct_state_closed_form`, `checks.still_water`, `checks.wave_speed_nonnegative`, and `checks.input_guard` in `diagnostics.json`.
- The state of a case, captured after setup and after the run, consists of `U_L`, `U_R`, `U_B`, `U_T` (3 values each, the supplied states), `F_star`, `G_star` (3 values each), `a_x`, `a_y` (scalars), and `guard_ok` (scalar: `-1` at setup, before the `operation` is called; `1` when the `operation` returned true; `0` when it returned false). The four states are set at setup; `F_star`, `G_star`, `a_x`, `a_y` are zero at the capture after setup and are the values the `operation` wrote at the capture after the run; `guard_ok` is set to `-1` at setup and to the value the `operation` returned at the call, recorded as `1` / `0` because a state variable is real-valued. The setup value `-1` is distinct from both values the `operation` can return, so a captured `guard_ok` of `0` is evidence that the `operation` was called and rejected the input; a case whose `operation` was never called captures `-1`, and no judgment of §6 accepts it. No state variable is derived by the checks module after the run: a residual or a deviation over these variables is a `checks.<id>` status, never a state variable. Every judgment of §6 is a statement about these variables and the §2 inputs.
- Each check is computed on the case named here and is `na` on every other case: `checks.equal_state_consistency` on the equal-state case; `checks.distinct_state_closed_form` on the distinct-state case; `checks.still_water` on the still-water case; `checks.wave_speed_nonnegative` on every case the guard accepts; `checks.input_guard` on every case (`pass` when `guard_ok = 1`, `fail` when `guard_ok = 0` and when `guard_ok = -1`).

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
  - `judgment`: with the distinct states of §2, `F_star`, `G_star`, `a_x` and `a_y` equal the closed-form values of §2 within an absolute tolerance of `1e-9` (component-wise max deviation `<= 1e-9`), and `guard_ok = 1` (`checks.distinct_state_closed_form`). A model with the dissipation sign reversed (`+ a (U_R - U_L)/2`) gives `F*_1 = -1.632` in the `h` component (the closed form is `-0.45 - 1.182` instead of `-0.45 + 1.182 = 0.732`); a model without the absolute value in the wave speed (`u + c`) gives `a_x = 4.129` and `a_y = 4.101` instead of `4.729` and `4.301`; a model with the minimum of the two sides gives `a_x = 4.035`; a model that takes the wave speed of the first-named side of every interface (`U_L`, `U_B`) fails the `a_y` comparison (`3.730`), and one that takes the second-named side (`U_R`, `U_T`) fails the `a_x` comparison (`4.035`) — the larger side is the left one on the `x` interface and the top one on the `y` interface, so the two interfaces are told apart from either consistent one-sided model; a model with a per-component wave speed fails the `hu` or `hv` component.
- `test_id`: `l0_still_water_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov__compute_flux`
  - `expected_outcome`: `pass`
  - `judgment`: with the still-water states, the mass flux and the tangential momentum flux are zero and the normal momentum flux is the pressure: `F_star = [0, 44.12772, 0]`, `G_star = [0, 0, 44.12772]`, each component within an absolute tolerance of `1e-9`, `a_x = a_y = sqrt(3 g) = 5.423880529658` within `1e-9`, and `guard_ok = 1` (`checks.still_water`).
- `test_id`: `l0_wave_speed_nonnegative_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov__compute_flux`
  - `expected_outcome`: `pass`
  - `judgment`: on the equal-state, distinct-state and still-water cases, the computed wave speeds satisfy `a_x >= 0` and `a_y >= 0` (`checks.wave_speed_nonnegative`).
- `test_id`: `l0_invalid_dry_state_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov__compute_flux`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `h<=0` in `U_L`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`
  - `judgment`: with `U_L = [0, 0, 0]`, the `operation` is called and returns `guard_ok = 0` (`checks.input_guard` is `fail`, which makes the case's `verdict.overall` `fail`); the captured `guard_ok` is `0`, not the setup value `-1`. A model whose guard on the `x` states is `h<0`, and a checks module that does not call the `operation` on this case, both fail this test.
- `test_id`: `l0_invalid_dry_state_r_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov__compute_flux`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `h<=0` in `U_R`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`
  - `judgment`: with `U_R = [-0.5, 0, 0]`, the same as `l0_invalid_dry_state_xfail`: the `operation` is called and the captured `guard_ok` is `0`. A model whose guard reads `U_L` only fails this test.
- `test_id`: `l0_invalid_dry_state_b_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov__compute_flux`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `h<=0` in `U_B`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`
  - `judgment`: with `U_B = [0, 0, 0]`, the same as `l0_invalid_dry_state_xfail`: the `operation` is called and the captured `guard_ok` is `0`. A model whose guard reads the `x` states only, or whose guard on the `y` states is `h<0`, fails this test.
- `test_id`: `l0_invalid_dry_state_y_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov__compute_flux`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `h<=0` in `U_T`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`
  - `judgment`: with `U_T = [-0.5, 0, 0]`, the same as `l0_invalid_dry_state_xfail`: the `operation` is called and the captured `guard_ok` is `0`. A model whose guard reads `U_B` only among the `y` states fails this test.

## 7. Pass/fail aggregation rules
- `per_test.pass_rule`: `pass` when the judgment expression is satisfied.
- `per_test.xfail_rule`: `xfail` when `xfail_condition` is true and `pass_when` is satisfied.
- `suite.pass_rule`: `pass` when all `test_id` are `pass` or `xfail`.

## 8. Traceability
- Record `test_profile_id` and `test_profile_version` in `trial_meta.json`.
