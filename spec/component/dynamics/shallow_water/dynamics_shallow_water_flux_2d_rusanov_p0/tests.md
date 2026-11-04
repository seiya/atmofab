# Tests: 2D shallow water Rusanov flux (L0)

## 0. Meta information
- `test_profile_id`: `dynamics_shallow_water_flux_2d_rusanov_p0_l0`
- `test_profile_version`: `0.5.0`
- `status`: `draft`
- `spec_ref.spec_kind`: `component`
- `spec_ref.spec_id`: `dynamics_shallow_water_flux_2d_rusanov_p0`
- `spec_ref.spec_version`: `0.2.1`
- `spec_ref.controlled_spec_path`: `spec/component/dynamics/shallow_water/dynamics_shallow_water_flux_2d_rusanov_p0/controlled_spec.md`

## 1. Test purpose
This suite verifies the published `operation` `dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux` at `L0`: the consistency of the numerical flux when the left/right states are equal, the closed form of the flux and of the wave speeds on the distinct-state case, the non-negativity of the Rusanov wave speeds, the three symmetry pairs the flux admits, and the input guard for a dry state (`h<=0`) in each of the four supplied states, at `h = 0` and at `h < 0`.

The symmetry pairs (`docs/TESTS.md` §Symmetry pairs) are carried on the distinct-state case `l0_distinct` of §2. Write `P(U) = [U_1, U_3, U_2]` for the exchange of the two momentum components. The **axis exchange** maps the `y` interface to the `x` interface and the reverse, each state with its momentum components exchanged: `U_L' = P(U_B)`, `U_R' = P(U_T)`, `U_B' = P(U_L)`, `U_T' = P(U_R)`; since the physical fluxes satisfy `F(P(U)) = P(G(U))`, the outputs are the base's exchanged: `F_star' = P(G_star)`, `G_star' = P(F_star)`, `a_x' = a_y`, `a_y' = a_x`. The **`x` mirror** reverses `hu` in every state and exchanges the two sides of the `x` interface, whose left state is the mirror image of the base's right state: `U_L' = M_x(U_R)`, `U_R' = M_x(U_L)`, `U_B' = M_x(U_B)`, `U_T' = M_x(U_T)` with `M_x(U) = [U_1, -U_2, U_3]`. The `x` flux is odd under the mirror (`F(M_x U) = -M_x F(U)`): its mass and `hv` components change sign and its `hu` component, quadratic in `hu` plus the pressure, does not; the `y` interface is not reversed, so the `y` flux changes sign in its `hu` component alone: `F_star' = [-F*_1, F*_2, -F*_3]`, `G_star' = [G*_1, -G*_2, G*_3]`, and both wave speeds are unchanged. The **`y` mirror** is the same with the roles of the two interfaces exchanged: `U_L' = M_y(U_L)`, `U_R' = M_y(U_R)`, `U_B' = M_y(U_T)`, `U_T' = M_y(U_B)` with `M_y(U) = [U_1, U_2, -U_3]`, and `F_star' = [F*_1, F*_2, -F*_3]`, `G_star' = [-G*_1, -G*_2, G*_3]`, both wave speeds unchanged. The equal-state case is not the base: its dissipation term is zero, so a pair on it would not see the wave speed enter the flux. `l0_distinct` is not invariant under any of the three transformations: its `x` and `y` interfaces carry different states and different wave speeds, and on each interface the side that fixes the wave speed carries a negative normal velocity, which the mirror of that interface's axis makes positive. The translation is not a pair: the flux is pointwise per interface and does not depend on its position, so a translated pair of states gives the same flux trivially.

## 2. Input-defaulting rules
- Every case uses `g = 9.80616`.
- The equal-state case `l0_equal_state` uses `U_L = U_R = [1.0, 0.2, 0.1]` and `U_B = U_T = [1.5, 0.3, 0.45]`.
- The distinct-state case `l0_distinct` uses `g = 9.80616`, `U_L = [2.0, -0.6, -0.4]`, `U_R = [1.5, -0.3, 0.45]`, `U_B = [1.2, 0.24, 0.36]`, `U_T = [1.8, 0.9, -0.18]`. The two sides of each interface differ in every component, so the dissipation term is non-zero in every component, and the side with the larger `|u_n| + c` carries a negative normal velocity on both interfaces (`u_L = -0.3`, `v_T = -0.1`). The closed form of the Controlled Spec §3 gives `a_x = max(0.3 + sqrt(2 g), 0.2 + sqrt(1.5 g)) = 4.728579907826` (the left side), `a_y = max(0.3 + sqrt(1.2 g), 0.1 + sqrt(1.8 g)) = 4.301319792637` (the top side), `F* = [0.732144976956, 14.732838013826, -1.994646460826]` and `G* = [-1.200395937791, -1.428435531570, 12.697563544012]`, the declared inputs `inputs.expected.F_star`, `inputs.expected.G_star`, `inputs.expected.a_x` and `inputs.expected.a_y` of this case.
- Three transformed copies of `l0_distinct`, each sorting after it and each with `g = 9.80616`, are the symmetry-pair cases of §1. Each declares, beside its states, the numeric sign vectors `pair_sign_F` and `pair_sign_G` (three numbers each) that the transformation of §1 applies to the base's `F_star` and `G_star` component by component, because the predicate grammar reads numeric inputs only:
  - `l0_distinct_axis`: `U_L = [1.2, 0.36, 0.24]`, `U_R = [1.8, -0.18, 0.9]`, `U_B = [2.0, -0.4, -0.6]`, `U_T = [1.5, 0.45, -0.3]`; `pair_sign_F = pair_sign_G = [1, 1, 1]`.
  - `l0_distinct_xmir`: `U_L = [1.5, 0.3, 0.45]`, `U_R = [2.0, 0.6, -0.4]`, `U_B = [1.2, -0.24, 0.36]`, `U_T = [1.8, -0.9, -0.18]`; `pair_sign_F = [-1, 1, -1]`, `pair_sign_G = [1, -1, 1]`.
  - `l0_distinct_ymir`: `U_L = [2.0, -0.6, 0.4]`, `U_R = [1.5, -0.3, -0.45]`, `U_B = [1.8, 0.9, 0.18]`, `U_T = [1.2, 0.24, -0.36]`; `pair_sign_F = [1, 1, -1]`, `pair_sign_G = [-1, -1, 1]`.
- The abnormal case `l0_invalid_dry` uses `U_L = [0.0, 0.0, 0.0]` and `U_R = U_B = U_T = [1.0, 0.0, 0.0]`.
- The abnormal cases `l0_invalid_dry_<side>_<form>`, `<side>` one of `l`, `r`, `b`, `t` and `<form>` one of `zero`, `negative`, each violate the `h<=0` clause of the Controlled Spec §4 in exactly one of the four states, the other three states being those of `l0_distinct`. The dry state is `[0.0, 0.0, 0.0]` in a `zero` case (the boundary of the clause) and `[-0.5, 0.0, 0.0]` in a `negative` case (its interior). `l0_invalid_dry` is the `zero` case of `U_L`, so `l0_invalid_dry_l_zero` does not exist. A guard that omits one state accepts both cases of that state; a guard written as `h<0` on one state accepts that state's `h = 0` case; a guard written as `h==0` on one state accepts that state's `h = -0.5` case.
- Before the call `U_L`, `U_R`, `U_B` and `U_T` hold the states above. `F_star`, `G_star`, `a_x` and `a_y` hold the literal `-999` at every element, their pre-call content: each is an operation-only output, so an element the `operation` does not write is captured as `-999` and fails every judgment of §6 over it.

## 3. Execution-control rules
`N/A`: this `component` exposes a single pointwise `operation` and defines no time-stepping or iteration. Execution control belongs to the `problem` that composes this `operation`.

## 4. Case-expansion rules
`N/A`: the `L0` suite uses fixed single-interface states and defines no `case` sweep. Case expansion is defined at the `problem` level.

## 5. Judged quantities
A test is judged by the host's predicates over the captures alone (`docs/TESTS.md` requirement 9). Each quantity below is named on the `quantities` line of the tests that judge it, and is evaluated from the state captured after the run in each of the test's target cases.

The host-rendered driver calls the `operation` once per case on the case's state, and the state captured before and after that call consists of `U_L`, `U_R`, `U_B`, `U_T` (3 values each, the supplied states of §2), `F_star`, `G_star` (3 values each), `a_x`, `a_y` (scalars), each of these four holding the pre-call content `-999` of §2 before the call, and `guard_ok`. All are arguments of the `operation`. `guard_ok` is its logical result, which the driver writes into the state after the call as `1` when the call returned true and `0` when it returned false; it starts at `0`, so a result the `operation` never assigned captures `0`. The outputs of a rejected input are not defined by the Controlled Spec §4, so the flag is the only state that distinguishes a rejection from an executed flux. A wave speed is judged beside the flux its call wrote: each quantity that reads `a_x` or `a_y` also holds a term that reads the speed together with the flux of its interface, `F_star` with `a_x` and `G_star` with `a_y`.

- `guard_ok`: the captured flag. It is `== 1` on the cases of a `pass` test and `== 0` on the cases of an `xfail` test.
- `equal_state_consistency`: on the equal-state case, evaluated for each interface: `max|F_star_i - F(U_L)_i|` over the three components, with `F(U) = [hu, hu*hu/h + g*h*h/2, hu*hv/h]` of the captured `U_L`, and `max|G_star_i - G(U_B)_i|`, with `G(U) = [hv, hu*hv/h, hv*hv/h + g*h*h/2]` of the captured `U_B`, `g` that of the case's inputs.
- `distinct_state_closed_form`: on `l0_distinct`, against the closed form of §2: the deviation of each of `a_x` and `a_y`, and the deviation of each flux (the largest over the components) added to the deviation of its interface's speed, `max|F_star - F*| + |a_x - a_x^*|` and `max|G_star - G*| + |a_y - a_y^*|`.
- `wave_speed_nonnegative`: `min(a_x, a_y)`, the smaller of the two captured wave speeds, and beside it the residual of each flux against the speed its call reports, `max|F_star - (0.5 (F(U_L) + F(U_R)) - 0.5 a_x (U_R - U_L))|` and `max|G_star - (0.5 (G(U_B) + G(U_T)) - 0.5 a_y (U_T - U_B))|` over the three components, with `F(U) = [hu, hu*hu/h + g*h*h/2, hu*hv/h]` and `G(U) = [hv, hu*hv/h, hv*hv/h + g*h*h/2]` of the captured states, `g` that of the case's inputs, and the captured `a_x` and `a_y`. The dissipation term of an interface with equal states is zero, so on the equal-state case the residual holds whatever the speed.
- `pair_residual`: on a transformed case, the residual of `a_x` and of `a_y`, and the residual of each flux added to that of its interface's speed: `r(a_x)`, `r(a_y)`, `r(F_star) + r(a_x)` and `r(G_star) + r(a_y)`. For one output `q`, `r(q)` is `norm2(q - T_q(base)) / norm2(T_q(base))` over every element, where `T_q(base)` is the output of `l0_distinct`, captured after its run, that §1 maps to `q`: for the exchange, `F_star` is compared with the base's `G_star` with its last two components exchanged (`roll(flip(G_star, 0), 1)`), `G_star` with the base's `F_star` exchanged the same way, `a_x` with the base's `a_y` and `a_y` with its `a_x`; for a mirror, `F_star` is compared with the base's `F_star` and `G_star` with the base's `G_star`, each multiplied by the case's `pair_sign_F` or `pair_sign_G`, and each wave speed with the base's. The host reads the base through `at('l0_distinct')` (`docs/workflow/phases/phase_01_compile.md`); the transformation preserves the `l2` norm, so the normaliser is the `l2` norm of the base output it reads.
- `cross_target_state_agreement`: this target's run compared with every other target's certified variant of this node, case by case (`docs/TESTS.md` §Cross-target judgment): for one output `q` captured after the run and the same output `q_ref` of the same case in the other variant, `max|q - q_ref| / max(max|q|, max|q_ref|, 1)` over every element. The host evaluates it from the captured state, against each other variant separately; on a target with no other certified variant it holds vacuously. It is outside the `quantities` line.

## 6. Test definitions
- `test_id`: `l0_equal_state_consistency_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux`
  - `expected_outcome`: `pass`
  - `quantities`: `guard_ok`, `equal_state_consistency`
  - `judgment`: with `U_L=U_R`, the dissipation term is analytically zero, so `guard_ok == 1` and `$\max_i |F^{*}_i - F(U_L)_i| \le 10^{-12}$` component-wise, and the same for `G*` with `U_B=U_T` (`equal_state_consistency <= 1e-12` at both interfaces). The cross-target judgment is applied: `cross_target_state_agreement` over `F_star`, `G_star`, `a_x`, and `a_y` is `<= 1e-12`.
- `test_id`: `l0_distinct_state_closed_form_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux`
  - `expected_outcome`: `pass`
  - `quantities`: `guard_ok`, `distinct_state_closed_form`
  - `judgment`: on `l0_distinct`, `guard_ok == 1` and `F_star`, `G_star`, `a_x` and `a_y` equal the closed form of §2 within an absolute tolerance of `1e-9` (`distinct_state_closed_form`: each term of §5 `<= 1e-9`). A kernel taking the minimum of the two sides in the wave speed (`a = min(|u_a| + c_a, |u_b| + c_b)`) reads `0.693` on `a_x`, `0.571` on `a_y`, `0.988` on the `x` flux term and `0.759` on the `y` flux term, and passes every other test of this suite: the equal-state test is blind to the wave speed, the speed stays non-negative and consistent with its flux, and a symmetry pair compares against a base that carries the same defect. The cross-target judgment is applied: `cross_target_state_agreement` over `F_star`, `G_star`, `a_x`, and `a_y` is `<= 1e-12`.
- `test_id`: `l0_wave_speed_nonnegative_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux`
  - `expected_outcome`: `pass`
  - `quantities`: `guard_ok`, `wave_speed_nonnegative`
  - `judgment`: on every normal case (the equal-state case, `l0_distinct` and its three transformed copies), `guard_ok == 1`, the computed wave speeds `a_x`,`a_y` are non-negative (`min(a_x, a_y) >= 0`), and the residual of each flux against its speed is `<= 1e-12` (`wave_speed_nonnegative`). A kernel that dissipates one flux with a speed other than the one it reports fails the residual on a case whose two sides of that interface differ. A conforming kernel reads `0` for each residual (measured on every case). The cross-target judgment is applied: `cross_target_state_agreement` over `a_x` and `a_y` is `<= 1e-12`.
- `test_id`: `l0_symmetry_pairs_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux`
  - `expected_outcome`: `pass`
  - `quantities`: `guard_ok`, `pair_residual`
  - `judgment`: on `l0_distinct_axis`, `l0_distinct_xmir` and `l0_distinct_ymir`, each term of `pair_residual` against `l0_distinct` is `<= 1.0e-12`, and `guard_ok == 1` on those cases and on `l0_distinct`. Every term of a transformed flux is the base's term with its sign reversed or not, and every wave speed is the same maximum of the same two sums, so a conforming kernel gives the rounding of the reordered operations alone (measured `0` on each output of the three cases); the bound is the suite's cross-target bound. A kernel without the absolute value in `a_x` (`u + c`) reads `0.145` (`a_x` of `l0_distinct_xmir`, `a_y` of `l0_distinct_axis`), one without it in `a_y` reads `0.127` (`a_y` of `l0_distinct_axis`) and `0.0488` (`a_y` of `l0_distinct_ymir`), and a kernel whose `y` flux is dissipated with `a_x` in place of `a_y` reads `0.0172` (`F_star` of `l0_distinct_axis`); each also deviates from the closed form of `l0_distinct`, so on this suite the pairs establish the transformed orientations, not a defect the closed form leaves open, and each passes `l0_equal_state_consistency_pass` (the equal states have positive normal velocities and no dissipation). The cross-target judgment is applied: `cross_target_state_agreement` over `F_star`, `G_star`, `a_x`, and `a_y` is `<= 1e-12`.
- `test_id`: `l0_invalid_dry_state_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `h<=0`
  - `quantities`: `guard_ok`
  - `judgment`: on the abnormal case of §2 (`l0_invalid_dry`), the `operation` refuses the input: `guard_ok == 0`, in the failure direction. The cross-target judgment is not applied.
- `test_id`: `l0_invalid_dry_state_l_negative_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `h<0` in `U_L`
  - `quantities`: `guard_ok`
  - `judgment`: on `l0_invalid_dry_l_negative` (`U_L = [-0.5, 0, 0]`, the other three states those of `l0_distinct`), the `operation` refuses the input: `guard_ok == 0`, in the failure direction. The cross-target judgment is not applied.
- `test_id`: `l0_invalid_dry_state_r_zero_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `h==0` in `U_R`
  - `quantities`: `guard_ok`
  - `judgment`: on `l0_invalid_dry_r_zero` (`U_R = [0, 0, 0]`, the other three states those of `l0_distinct`), the `operation` refuses the input: `guard_ok == 0`, in the failure direction. The cross-target judgment is not applied.
- `test_id`: `l0_invalid_dry_state_r_negative_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `h<0` in `U_R`
  - `quantities`: `guard_ok`
  - `judgment`: on `l0_invalid_dry_r_negative` (`U_R = [-0.5, 0, 0]`, the other three states those of `l0_distinct`), the `operation` refuses the input: `guard_ok == 0`, in the failure direction. The cross-target judgment is not applied.
- `test_id`: `l0_invalid_dry_state_b_zero_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `h==0` in `U_B`
  - `quantities`: `guard_ok`
  - `judgment`: on `l0_invalid_dry_b_zero` (`U_B = [0, 0, 0]`, the other three states those of `l0_distinct`), the `operation` refuses the input: `guard_ok == 0`, in the failure direction. The cross-target judgment is not applied.
- `test_id`: `l0_invalid_dry_state_b_negative_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `h<0` in `U_B`
  - `quantities`: `guard_ok`
  - `judgment`: on `l0_invalid_dry_b_negative` (`U_B = [-0.5, 0, 0]`, the other three states those of `l0_distinct`), the `operation` refuses the input: `guard_ok == 0`, in the failure direction. The cross-target judgment is not applied.
- `test_id`: `l0_invalid_dry_state_t_zero_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `h==0` in `U_T`
  - `quantities`: `guard_ok`
  - `judgment`: on `l0_invalid_dry_t_zero` (`U_T = [0, 0, 0]`, the other three states those of `l0_distinct`), the `operation` refuses the input: `guard_ok == 0`, in the failure direction. The cross-target judgment is not applied.
- `test_id`: `l0_invalid_dry_state_t_negative_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `h<0` in `U_T`
  - `quantities`: `guard_ok`
  - `judgment`: on `l0_invalid_dry_t_negative` (`U_T = [-0.5, 0, 0]`, the other three states those of `l0_distinct`), the `operation` refuses the input: `guard_ok == 0`, in the failure direction. The cross-target judgment is not applied.

## 7. Pass/fail aggregation rules
- `per_test.pass_rule`: `pass` when every predicate of the test holds.
- `per_test.xfail_rule`: `xfail` when `xfail_condition` is true and every predicate of the test holds.
- `suite.pass_rule`: `pass` when all `test_id` are `pass` or `xfail`.

## 8. Traceability
- Record `test_profile_id` and `test_profile_version` in `trial_meta.json`.
