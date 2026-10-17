# Tests: 2D shallow water Rusanov flux (L0)

## 0. Meta information
- `test_profile_id`: `dynamics_shallow_water_flux_2d_rusanov_p0_l0`
- `test_profile_version`: `0.3.0`
- `status`: `draft`
- `spec_ref.spec_kind`: `component`
- `spec_ref.spec_id`: `dynamics_shallow_water_flux_2d_rusanov_p0`
- `spec_ref.spec_version`: `0.2.1`
- `spec_ref.controlled_spec_path`: `spec/component/dynamics/shallow_water/dynamics_shallow_water_flux_2d_rusanov_p0/controlled_spec.md`

## 1. Test purpose
This suite verifies the published `operation` `dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux` at `L0`: the consistency of the numerical flux when the left/right states are equal, the non-negativity of the Rusanov wave speeds, the three symmetry pairs the flux admits, and the input guard for a dry state (`h<=0`).

The symmetry pairs (`docs/TESTS.md` §Symmetry pairs) are carried on the distinct-state case `l0_distinct` of §2. Write `P(U) = [U_1, U_3, U_2]` for the exchange of the two momentum components. The **axis exchange** maps the `y` interface to the `x` interface and the reverse, each state with its momentum components exchanged: `U_L' = P(U_B)`, `U_R' = P(U_T)`, `U_B' = P(U_L)`, `U_T' = P(U_R)`; since the physical fluxes satisfy `F(P(U)) = P(G(U))`, the outputs are the base's exchanged: `F_star' = P(G_star)`, `G_star' = P(F_star)`, `a_x' = a_y`, `a_y' = a_x`. The **`x` mirror** reverses `hu` in every state and exchanges the two sides of the `x` interface, whose left state is the mirror image of the base's right state: `U_L' = M_x(U_R)`, `U_R' = M_x(U_L)`, `U_B' = M_x(U_B)`, `U_T' = M_x(U_T)` with `M_x(U) = [U_1, -U_2, U_3]`. The `x` flux is odd under the mirror (`F(M_x U) = -M_x F(U)`): its mass and `hv` components change sign and its `hu` component, quadratic in `hu` plus the pressure, does not; the `y` interface is not reversed, so the `y` flux changes sign in its `hu` component alone: `F_star' = [-F*_1, F*_2, -F*_3]`, `G_star' = [G*_1, -G*_2, G*_3]`, and both wave speeds are unchanged. The **`y` mirror** is the same with the roles of the two interfaces exchanged: `U_L' = M_y(U_L)`, `U_R' = M_y(U_R)`, `U_B' = M_y(U_T)`, `U_T' = M_y(U_B)` with `M_y(U) = [U_1, U_2, -U_3]`, and `F_star' = [F*_1, F*_2, -F*_3]`, `G_star' = [-G*_1, -G*_2, G*_3]`, both wave speeds unchanged. The equal-state case is not the base: its dissipation term is zero, so a pair on it would not see the wave speed enter the flux. `l0_distinct` is not invariant under any of the three transformations: its `x` and `y` interfaces carry different states and different wave speeds, and on each interface the side that fixes the wave speed carries a negative normal velocity, which the mirror of that interface's axis makes positive. A pair establishes the transformed flux only as the checks module captures it: a checks module that writes the transformed base outputs instead of calling the `operation` at the transformed case's arguments captures the same outputs, and the call is the `Generate` reviewer's to hold (`docs/workflow/CHECKS_MODULE_CONTRACT.md` §1). The translation is not a pair: the flux is pointwise per interface and does not depend on its position, so a translated pair of states gives the same flux trivially.

## 2. Input-defaulting rules
- The normal case uses left/right and bottom/top states satisfying `h>0`.
- The distinct-state case `l0_distinct` uses `g = 9.80616`, `U_L = [2.0, -0.6, -0.4]`, `U_R = [1.5, -0.3, 0.45]`, `U_B = [1.2, 0.24, 0.36]`, `U_T = [1.8, 0.9, -0.18]`. The two sides of each interface differ in every component, so the dissipation term is non-zero in every component, and the side with the larger `|u_n| + c` carries a negative normal velocity on both interfaces (`u_L = -0.3`, `v_T = -0.1`).
- Three transformed copies of `l0_distinct`, each sorting after it and each with `g = 9.80616`, are the symmetry-pair cases of §1. Each declares, beside its states, the numeric sign vectors `pair_sign_F` and `pair_sign_G` (three numbers each) that the transformation of §1 applies to the base's `F_star` and `G_star` component by component, because the predicate grammar reads numeric inputs only:
  - `l0_distinct_axis`: `U_L = [1.2, 0.36, 0.24]`, `U_R = [1.8, -0.18, 0.9]`, `U_B = [2.0, -0.4, -0.6]`, `U_T = [1.5, 0.45, -0.3]`; `pair_sign_F = pair_sign_G = [1, 1, 1]`.
  - `l0_distinct_xmir`: `U_L = [1.5, 0.3, 0.45]`, `U_R = [2.0, 0.6, -0.4]`, `U_B = [1.2, -0.24, 0.36]`, `U_T = [1.8, -0.9, -0.18]`; `pair_sign_F = [-1, 1, -1]`, `pair_sign_G = [1, -1, 1]`.
  - `l0_distinct_ymir`: `U_L = [2.0, -0.6, 0.4]`, `U_R = [1.5, -0.3, -0.45]`, `U_B = [1.8, 0.9, 0.18]`, `U_T = [1.2, 0.24, -0.36]`; `pair_sign_F = [1, 1, -1]`, `pair_sign_G = [-1, -1, 1]`.
- The abnormal case uses a state including `h<=0`.

## 3. Execution-control rules
`N/A`: this `component` exposes a single pointwise `operation` and defines no time-stepping or iteration. Execution control is the responsibility of the time-update `component` and the `problem` runner.

## 4. Case-expansion rules
`N/A`: the `L0` suite uses fixed single-interface states and defines no `case` sweep. Case expansion is defined at the `problem` level.

## 5. Diagnostics contract
- Require outputting `checks.equal_state_consistency`, `checks.wave_speed_nonnegative`, `checks.symmetry_pair`, and `checks.input_guard` in `diagnostics.json`. `checks.symmetry_pair` is computed on the three transformed cases and is `na` on every other case; it reads the outputs of `l0_distinct`, which the checks module keeps from that case's run (`docs/workflow/CHECKS_MODULE_CONTRACT.md` §2, the accumulator ordering rule: each transformed case sorts after its base).
- `pair_residual` of a transformed case and one output `q` (`F_star`, `G_star`, `a_x` or `a_y`) is `norm2(q - T_q(base)) / norm2(T_q(base))` over every element, where `T_q(base)` is the output of `l0_distinct`, captured after its run, that §1 maps to `q`: for the exchange, `F_star` is compared with the base's `G_star` with its last two components exchanged (`roll(flip(G_star, 0), 1)`), `G_star` with the base's `F_star` exchanged the same way, `a_x` with the base's `a_y` and `a_y` with its `a_x`; for a mirror, `F_star` is compared with the base's `F_star` and `G_star` with the base's `G_star`, each multiplied by the case's `pair_sign_F` or `pair_sign_G`, and each wave speed with the base's. The host evaluates it from the captured states (`at('l0_distinct')`, `docs/workflow/phases/phase_01_compile.md`); the transformation preserves the `l2` norm, so the normaliser is the `l2` norm of the base output it reads.
- The state of a case, captured after setup and after the run, consists of `U_L`, `U_R`, `U_B`, `U_T` (3 values each, the supplied states, written at setup), `F_star`, `G_star` (3 values each, the sentinel `-999` at setup), `a_x`, `a_y` (scalars, the sentinel `-999` at setup), and `guard_ok`, a scalar recorded as a number because a state variable is real-valued: `-1` at setup, before the `operation` is called; `1` when the call returned true; `0` when it returned false. The setup value `-1` is distinct from both values the call can return, so a case whose `operation` was not called captures `-1`. Every `pass` judgment of §6 requires `guard_ok = 1` on the cases it names, and every `xfail` judgment requires `guard_ok = 0` on its cases; these are host-evaluated from the capture after the run (the refusal's observable consequence: the outputs of a rejected input are not defined by the Controlled Spec §4, so the flag is the only state that distinguishes a rejection from an executed flux). `checks.input_guard` is `pass` when `guard_ok = 1` and `fail` otherwise.
- `cross_target_state_agreement` compares this target's run with every other target's certified variant of this node, case by case (`docs/TESTS.md` §Cross-target judgment): for one output `q` captured after the run and the same output `q_ref` of the same case in the other variant, it is `max|q - q_ref| / max(max|q|, max|q_ref|, 1)` over every element. The host evaluates it from the captured state, against each other variant separately; it is not a `diagnostics.json` field, and on a target with no other certified variant it holds vacuously.

## 6. Test definitions
- `test_id`: `l0_equal_state_consistency_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux`
  - `expected_outcome`: `pass`
  - `judgment`: with `U_L=U_R`, the dissipation term is analytically zero, so satisfy `$\max_i |F^{*}_i - F(U_L)_i| \le 10^{-12}$` component-wise (and the same for `G*` with `U_B=U_T`). The cross-target judgment is applied: `cross_target_state_agreement` over `F_star`, `G_star`, `a_x`, and `a_y` is `<= 1e-12`.
- `test_id`: `l0_wave_speed_nonnegative_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux`
  - `expected_outcome`: `pass`
  - `judgment`: on every normal case (the equal-state case, `l0_distinct` and its three transformed copies), the computed wave speeds `a_x`,`a_y` are non-negative. The cross-target judgment is applied: `cross_target_state_agreement` over `a_x` and `a_y` is `<= 1e-12`.
- `test_id`: `l0_symmetry_pairs_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux`
  - `expected_outcome`: `pass`
  - `judgment`: on `l0_distinct_axis`, `l0_distinct_xmir` and `l0_distinct_ymir`, `pair_residual` against `l0_distinct` of each of `F_star`, `G_star`, `a_x` and `a_y` is `<= 1.0e-12`, and `guard_ok = 1` (`checks.symmetry_pair`). Every term of a transformed flux is the base's term with its sign reversed or not, and every wave speed is the same maximum of the same two sums, so a conforming kernel gives the rounding of the reordered operations alone (measured `0` on each output of the three cases); the bound is the suite's cross-target bound. This suite judges no closed form, so the pairs are what pins the wave speed and its place in the dissipation term: a kernel without the absolute value in `a_x` (`u + c`) reads `0.145` (`a_x` of `l0_distinct_xmir`, `a_y` of `l0_distinct_axis`), one without it in `a_y` reads `0.127` (`a_y` of `l0_distinct_axis`) and `0.0488` (`a_y` of `l0_distinct_ymir`), and a kernel whose `y` flux is dissipated with `a_x` in place of `a_y` reads `0.0172` (`F_star` of `l0_distinct_axis`); each passes `l0_equal_state_consistency_pass` (the equal states have positive normal velocities and no dissipation) and `l0_wave_speed_nonnegative_pass`. The cross-target judgment is applied: `cross_target_state_agreement` over `F_star`, `G_star`, `a_x`, and `a_y` is `<= 1e-12`.
- `test_id`: `l0_invalid_dry_state_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `h<=0`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`

## 7. Pass/fail aggregation rules
- `per_test.pass_rule`: `pass` when the judgment expression is satisfied.
- `per_test.xfail_rule`: `xfail` when `xfail_condition` is true and `pass_when` is satisfied.
- `suite.pass_rule`: `pass` when all `test_id` are `pass` or `xfail`.

## 8. Traceability
- Record `test_profile_id` and `test_profile_version` in `trial_meta.json`.
