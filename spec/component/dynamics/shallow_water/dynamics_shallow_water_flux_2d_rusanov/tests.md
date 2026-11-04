# Tests: 2D shallow water Rusanov flux from supplied interface states (L0)

## 0. Meta information
- `test_profile_id`: `dynamics_shallow_water_flux_2d_rusanov_l0`
- `test_profile_version`: `0.5.0`
- `status`: `draft`
- `spec_ref.spec_kind`: `component`
- `spec_ref.spec_id`: `dynamics_shallow_water_flux_2d_rusanov`
- `spec_ref.spec_version`: `0.1.1`
- `spec_ref.controlled_spec_path`: `spec/component/dynamics/shallow_water/dynamics_shallow_water_flux_2d_rusanov/controlled_spec.md`

## 1. Test purpose
This suite verifies the published `operation` `dynamics_shallow_water_flux_2d_rusanov__compute_flux` at `L0`: the consistency of the numerical flux with the physical flux when the two states of an interface are equal, the closed form of the flux and of the wave speeds for three pairs of distinct states, which between them make each of the four states the one that fixes its interface's wave speed and set the larger `|u_n|` against the larger `c` on opposite sides of one interface, the flux of still water, the non-negativity of the wave speeds, the three symmetry pairs the flux admits, and the input guard for a dry state in each of the four supplied states, at `h = 0` and at `h < 0`. The `operation` is pointwise, so every case is one `x` interface and one `y` interface. One fact the Controlled Spec states is not observed by this suite and is stated by it alone: the ordering clause of §4 (the guard is evaluated before any division by `h`), because a division performed before the guard leaves no trace in the state a rejected case captures.

The symmetry pairs (`docs/TESTS.md` §Symmetry pairs) are carried on the first distinct-state case. Write `P(U) = [U_1, U_3, U_2]` for the exchange of the two momentum components. The **axis exchange** maps the `y` interface to the `x` interface and the reverse, each state with its momentum components exchanged: `U_L' = P(U_B)`, `U_R' = P(U_T)`, `U_B' = P(U_L)`, `U_T' = P(U_R)`. Since `F(P(U)) = P(G(U))`, the outputs are the base's exchanged: `F_star' = P(G_star)`, `G_star' = P(F_star)`, `a_x' = a_y`, `a_y' = a_x`. The **`x` mirror** reverses `hu` in every state and exchanges the two sides of the `x` interface, whose left state is the mirror image of the base's right state: `U_L' = M_x(U_R)`, `U_R' = M_x(U_L)`, `U_B' = M_x(U_B)`, `U_T' = M_x(U_T)` with `M_x(U) = [U_1, -U_2, U_3]`. The `x` flux is odd under the mirror (`F(M_x U) = -M_x F(U)`): its mass and `hv` components change sign and its `hu` component, quadratic in `hu` plus the pressure, does not; the `y` interface is not reversed, so the `y` flux changes sign in its `hu` component alone: `F_star' = [-F*_1, F*_2, -F*_3]`, `G_star' = [G*_1, -G*_2, G*_3]`, and both wave speeds are unchanged. The **`y` mirror** is the same with the roles of the two interfaces exchanged: `U_L' = M_y(U_L)`, `U_R' = M_y(U_R)`, `U_B' = M_y(U_T)`, `U_T' = M_y(U_B)` with `M_y(U) = [U_1, U_2, -U_3]`, and `F_star' = [F*_1, F*_2, -F*_3]`, `G_star' = [-G*_1, -G*_2, G*_3]`, both wave speeds unchanged. The first distinct-state case is the base because it is not invariant under any of the three: its `x` and `y` interfaces carry different states and different wave speeds, and on each interface the side that fixes the wave speed carries a negative normal velocity, which the mirror of that interface's axis makes positive; the third distinct-state case is its own axis exchange and would compare a case with itself. The transformed cases are judged against the base by the pair, not by closed forms of their own: the base is pinned by `l0_distinct_state_closed_form_pass`, and the pair pins each transformed case to the base. The translation is not a pair: the flux is pointwise per interface and does not depend on its position, so a translated pair of states gives the same flux trivially.

## 2. Input-defaulting rules
- Every case but `l0_distinct_g981` uses `g = 9.80616`.
- The equal-state case uses `U_L = U_R = [2.0, 0.6, -0.4]` and `U_B = U_T = [1.5, 0.3, 0.45]`. The physical fluxes are `F(U_L) = [0.6, 19.79232, -0.12]` (`hu`, `hu^2/h + g h^2/2 = 0.18 + 19.61232`, `hu hv/h`) and `G(U_B) = [0.45, 0.09, 11.16693]` (`hv`, `hu hv/h`, `hv^2/h + g h^2/2 = 0.135 + 11.03193`); the wave speeds are `a_x = 0.3 + sqrt(2 g) = 4.728579907826` and `a_y = 0.3 + sqrt(1.5 g) = 4.135262702867`.
- The first distinct-state case `l0_distinct_1` uses `U_L = [2.0, -0.6, -0.4]`, `U_R = [1.5, -0.3, 0.45]`, `U_B = [1.2, 0.24, 0.36]`, `U_T = [1.8, 0.9, -0.18]`. The two sides of each interface differ in every component, so the dissipation term is non-zero in every component; on each interface the side with the larger `|u_n| + c` carries a NEGATIVE normal velocity (`u_L = -0.3` on the `x` interface, `v_T = -0.1` on the `y` interface), so the absolute value in the wave speed is exercised. The normal momentum changes sign across the `y` interface (`v_B = 0.3`, `v_T = -0.1`). The closed form of the Controlled Spec §3 gives `a_x = max(0.3 + sqrt(2 g), 0.2 + sqrt(1.5 g)) = max(4.728579907826, 4.035262702867) = 4.728579907826` (the left side), `a_y = max(0.3 + sqrt(1.2 g), 0.1 + sqrt(1.8 g)) = max(3.730363246072, 4.301319792637) = 4.301319792637` (the top side), `F* = [0.732144976956, 14.732838013826, -1.994646460826]` and `G* = [-1.200395937791, -1.428435531570, 12.697563544012]`.
- The second distinct-state case uses `U_L = [1.2, 0.24, -0.36]`, `U_R = [2.0, -0.6, 0.45]`, `U_B = [1.8, 0.9, -0.18]`, `U_T = [1.2, 0.24, 0.36]`. The side with the larger `|u_n| + c` is the RIGHT state on the `x` interface (`4.728579907826` against `3.630363246072`) and the BOTTOM state on the `y` interface (`4.301319792637` against `3.730363246072`) — the opposite side from the first case on each interface — and that side again carries a negative normal velocity (`u_R = -0.3`, `v_B = -0.1`); the normal momentum changes sign across both interfaces (`u_L = 0.2`, `v_T = 0.3`). With the first case it pins the wave-speed contribution of each of the four states: a state's term is the maximum of its interface in exactly one of the two cases, so an error in any one of the four terms changes `a_x` or `a_y` in one case or the other. The closed form gives `a_x = 4.728579907826`, `a_y = 4.301319792637`, `F* = [-2.071431963130, 15.436381161287, -2.018574862670]` and `G* = [1.380395937791, 1.410435531570, 10.374850855988]`.
- The third distinct-state case uses `U_L = [0.5, 1.5, 0.2]`, `U_R = [3.0, -0.3, 0.1]`, `U_B = [0.5, 0.2, 1.5]`, `U_T = [3.0, 0.1, -0.3]`. On each interface the larger `|u_n|` and the larger `c` come from OPPOSITE sides: `|u_L| = 3.0` against `|u_R| = 0.1`, and `c_L = sqrt(0.5 g) = 2.214289953913` against `c_R = sqrt(3 g) = 5.423880529658`. The Rusanov speed of §3 is the larger of the two per-side sums, `max(5.214289953913, 5.523880529658) = 5.523880529658`; the sum of the two maxima, `max(|u_L|,|u_R|) + max(c_L,c_R) = 8.423880529658`, is a different estimate, which §3 does not define and §6 of the Controlled Spec forbids, and which the first two cases cannot tell apart because there the same side is the larger in both terms. The `y` interface is the same pair with the roles of `hu` and `hv` exchanged. The closed form gives `a_x = a_y = 5.523880529658`, `F* = [-6.304850662072, 29.913237476692, 0.571194026483]` and `G* = [-6.304850662072, 0.571194026483, 29.913237476692]`.
- The case `l0_distinct_g981` uses the states of `l0_distinct_1` with `g = 9.81`, the value a consumer of this `component` may pass, so that a `g` the `operation` does not read from its argument is visible. The closed form gives `a_x = 4.729446918070`, `a_y = 4.302142310774`, `F* = [0.732361729518, 14.738707962289, -1.995014940180]` and `G* = [-1.200642693232, -1.428706962556, 12.702278423909]`, the declared `inputs.expected` of this case.
- Three transformed copies of `l0_distinct_1`, each sorting after it, are the symmetry-pair cases of §1. Each declares, beside its states, the numeric sign vectors `pair_sign_F` and `pair_sign_G` (three numbers each) that the transformation of §1 applies to the base's `F_star` and `G_star` component by component, because the predicate grammar reads numeric inputs only:
  - `l0_distinct_1_axis`: `U_L = [1.2, 0.36, 0.24]`, `U_R = [1.8, -0.18, 0.9]`, `U_B = [2.0, -0.4, -0.6]`, `U_T = [1.5, 0.45, -0.3]`; `pair_sign_F = pair_sign_G = [1, 1, 1]`.
  - `l0_distinct_1_xmir`: `U_L = [1.5, 0.3, 0.45]`, `U_R = [2.0, 0.6, -0.4]`, `U_B = [1.2, -0.24, 0.36]`, `U_T = [1.8, -0.9, -0.18]`; `pair_sign_F = [-1, 1, -1]`, `pair_sign_G = [1, -1, 1]`.
  - `l0_distinct_1_ymir`: `U_L = [2.0, -0.6, 0.4]`, `U_R = [1.5, -0.3, -0.45]`, `U_B = [1.8, 0.9, 0.18]`, `U_T = [1.2, 0.24, -0.36]`; `pair_sign_F = [1, 1, -1]`, `pair_sign_G = [-1, -1, 1]`.
- The still-water case uses `U_L = U_R = U_B = U_T = [3.0, 0.0, 0.0]`. The closed form gives `F* = [0, 4.5 g, 0] = [0, 44.12772, 0]`, `G* = [0, 0, 44.12772]`, and `a_x = a_y = sqrt(3 g) = 5.423880529658`.
- The abnormal cases each violate the `h<=0` clause of the Controlled Spec §4 in exactly one of the four states, the other three states being those of the first distinct-state case. There are eight, two per state: one with `h = 0` (the boundary of the clause) and one with `h = -0.5` (its interior), the dry state being `[0.0, 0.0, 0.0]` and `[-0.5, 0.0, 0.0]` respectively. A guard that omits one state accepts both cases of that state; a guard written as `h<0` on one state accepts that state's `h = 0` case; a guard written as `h==0` on one state accepts that state's `h = -0.5` case. Every clause of §4 is therefore pinned on every state and on both sides of its boundary.
- Before the call `U_L`, `U_R`, `U_B` and `U_T` hold the states above. `F_star`, `G_star`, `a_x` and `a_y` hold the literal `-999` at every element, their pre-call content: each is an operation-only output, so an element the `operation` does not write is captured as `-999` and fails every judgment of §6 over it.

## 3. Execution-control rules
`N/A`: this `component` exposes a single pointwise `operation` and defines no time-stepping or iteration. Execution control belongs to the `problem` that composes this `operation`.

## 4. Case-expansion rules
`N/A`: the `L0` suite uses fixed single-interface states and defines no `case` sweep. Case expansion is defined at the `problem` level.

## 5. Judged quantities
A test is judged by the host's predicates over the captures alone (`docs/TESTS.md` requirement 9). Each quantity below is named on the `quantities` line of the tests that judge it, and is evaluated from the state captured after the run in each of the test's target cases.

The host-rendered driver calls the `operation` once per case on the case's state, and the state captured before and after that call consists of `U_L`, `U_R`, `U_B`, `U_T` (3 values each, the supplied states of §2), `F_star`, `G_star` (3 values each), `a_x`, `a_y` (scalars), each of these four holding the pre-call content `-999` of §2 before the call, and `guard_ok`. All are arguments of the `operation`. `guard_ok` is its logical result, which the driver writes into the state after the call as `1` when the call returned true and `0` when it returned false; it starts at `0`, so a result the `operation` never assigned captures `0`. `-999` is not a value the `operation` produces on any case of this suite, so a captured `0` in these variables is a zero the `operation` wrote, and a variable the `operation` left untouched captures `-999` and fails. A residual or a deviation over these variables is evaluated by the host and is never a state variable. Every judgment of §6 is a statement about these variables and the §2 inputs. The expected values of the equal-state, distinct-state, `l0_distinct_g981` and still-water cases, those §2 and §6 state, are declared inputs of the case (`inputs.expected.F_star`, `inputs.expected.G_star`, `inputs.expected.a_x`, `inputs.expected.a_y`). A wave speed is judged beside the flux its call wrote: each quantity that reads `a_x` or `a_y` also holds a term that reads the speed together with the flux of its interface, `F_star` with `a_x` and `G_star` with `a_y`.

- `guard_ok`: the captured flag. It is `== 1` on the cases of a `pass` test and `== 0` on the cases of an `xfail` test.
- `equal_state_consistency`: on the equal-state case, `max|F_star - F(U_L)|` and `max|G_star - G(U_B)|` over the components, with `F(U_L)` and `G(U_B)` the declared expected values of §2.
- `distinct_state_closed_form`: on each of the three distinct-state cases and on `l0_distinct_g981`, against that case's own closed form of §2: the deviation of each of `a_x` and `a_y`, and the deviation of each flux (the largest over the components) added to the deviation of its interface's speed, `max|F_star - F*| + |a_x - a_x^*|` and `max|G_star - G*| + |a_y - a_y^*|`.
- `still_water`: on the still-water case, against the closed form of §2: the deviation of each of `a_x` and `a_y`, and the deviation of each flux (the largest over the components) added to the deviation of its interface's speed, as in `distinct_state_closed_form`.
- `wave_speed_nonnegative`: on the equal-state, the three distinct-state, the three transformed and the still-water cases, the captured `a_x` and `a_y`, and beside them the residual of each flux against the speed its call reports, `max|F_star - (0.5 (F(U_L) + F(U_R)) - 0.5 a_x (U_R - U_L))|` and `max|G_star - (0.5 (G(U_B) + G(U_T)) - 0.5 a_y (U_T - U_B))|` over the three components, with `F(U) = [hu, hu*hu/h + g*h*h/2, hu*hv/h]` and `G(U) = [hv, hu*hv/h, hv*hv/h + g*h*h/2]` of the captured states, `g` that of the case's inputs, and the captured `a_x` and `a_y`. The dissipation term of an interface with equal states is zero, so on the equal-state and still-water cases the residual holds whatever the speed.
- `symmetry_pair`: on the three transformed cases, the `pair_residual` of `a_x` and of `a_y`, and the `pair_residual` of each flux added to that of its interface's speed: `pair_residual(F_star) + pair_residual(a_x)` and `pair_residual(G_star) + pair_residual(a_y)`. `pair_residual` of a transformed case and one output `q` (`F_star`, `G_star`, `a_x` or `a_y`) is `norm2(q - T_q(base)) / norm2(T_q(base))` over every element, where `T_q(base)` is the output of `l0_distinct_1`, captured after its run, that §1 maps to `q`: for the exchange, `F_star` is compared with the base's `G_star` with its last two components exchanged (`roll(flip(G_star, 0), 1)`), `G_star` with the base's `F_star` exchanged the same way, `a_x` with the base's `a_y` and `a_y` with its `a_x`; for a mirror, `F_star` is compared with the base's `F_star` and `G_star` with the base's `G_star`, each multiplied by the case's `pair_sign_F` or `pair_sign_G`, and each wave speed with the base's. The host reads the base through `at('l0_distinct_1')` (`docs/workflow/phases/phase_01_compile.md`); the transformation preserves the `l2` norm, so the normaliser is the `l2` norm of the base output it reads.
- `rejected_outputs_zero`: on every abnormal case, `max|F_star| + max|G_star| + |a_x| + |a_y|`, and beside it `|a_x| + |a_y|`. A captured `-999` does not satisfy `== 0`.
- `cross_target_state_agreement`: this target's run compared with every other target's certified variant of this node, case by case (`docs/TESTS.md` §Cross-target judgment): for one output `q` captured after the run and the same output `q_ref` of the same case in the other variant, `max|q - q_ref| / max(max|q|, max|q_ref|, 1)` over every element. The host evaluates it from the captured state, against each other variant separately; on a target with no other certified variant it holds vacuously. It is outside the `quantities` line.

## 6. Test definitions
- `test_id`: `l0_equal_state_consistency_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov__compute_flux`
  - `expected_outcome`: `pass`
  - `quantities`: `guard_ok`, `equal_state_consistency`
  - `judgment`: with `U_L = U_R` and `U_B = U_T`, the dissipation term is analytically zero, so `F_star = F(U_L) = [0.6, 19.79232, -0.12]` and `G_star = G(U_B) = [0.45, 0.09, 11.16693]` component-wise within an absolute tolerance of `1e-12` (`equal_state_consistency`: `max_i |F*_i - F(U_L)_i| <= 1e-12`, and the same for `G*`), and `guard_ok == 1`. A model with the momentum flux `hu^2/h` and the pressure `g h^2/2` exchanged between `F` and `G`, or with the pressure `g h^2` or `g h`, fails. The cross-target judgment is applied: `cross_target_state_agreement` over `F_star`, `G_star`, `a_x`, and `a_y` is `<= 1e-12`.
- `test_id`: `l0_distinct_state_closed_form_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov__compute_flux`
  - `expected_outcome`: `pass`
  - `quantities`: `guard_ok`, `distinct_state_closed_form`
  - `judgment`: on each of the three distinct-state cases of §2 and on `l0_distinct_g981`, `F_star`, `G_star`, `a_x` and `a_y` equal that case's closed-form values of §2 within an absolute tolerance of `1e-9` (`distinct_state_closed_form`: each term of §5 `<= 1e-9`), and `guard_ok == 1`. A model with the dissipation sign reversed (`+ a (U_R - U_L)/2`) gives `F*_1 = -1.632` in the `h` component (the closed form is `-0.45 - 1.182` instead of `-0.45 + 1.182 = 0.732`); a model without the absolute value in the wave speed (`u + c`) gives `a_x = 4.129` and `a_y = 4.101` instead of `4.729` and `4.301`; a model with the minimum of the two sides gives `a_x = 4.035`; a model that drops one of the four states from its wave speed, or evaluates that state's term from another state's `h` or momentum, fails the case in which that state is the maximum of its interface (the first case for `U_L` and `U_T`, the second for `U_R` and `U_B`), so no single state's term is left unpinned; a model that adds the two maxima (`max(|u_L|,|u_R|) + max(c_L,c_R)`) instead of taking the maximum of the two per-side sums gives `8.423880529658` for both wave speeds of the third case, against `5.523880529658`; a model with a per-component wave speed fails the `hu` or `hv` component; a model that holds `g` as a literal `9.80616`, or that refuses `g >= 9.81`, fails `l0_distinct_g981`. The cross-target judgment is applied: `cross_target_state_agreement` over `F_star`, `G_star`, `a_x`, and `a_y` is `<= 1e-12`.
- `test_id`: `l0_still_water_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov__compute_flux`
  - `expected_outcome`: `pass`
  - `quantities`: `guard_ok`, `still_water`
  - `judgment`: with the still-water states, the mass flux and the tangential momentum flux are zero and the normal momentum flux is the pressure: `F_star = [0, 44.12772, 0]`, `G_star = [0, 0, 44.12772]`, each component within an absolute tolerance of `1e-9`, `a_x = a_y = sqrt(3 g) = 5.423880529658` within `1e-9` (`still_water`: each term of §5 `<= 1e-9`), and `guard_ok == 1`. The cross-target judgment is applied: `cross_target_state_agreement` over `F_star`, `G_star`, `a_x`, and `a_y` is `<= 1e-12`.
- `test_id`: `l0_wave_speed_nonnegative_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov__compute_flux`
  - `expected_outcome`: `pass`
  - `quantities`: `guard_ok`, `wave_speed_nonnegative`
  - `judgment`: on the equal-state, the three distinct-state, the three transformed and the still-water cases, `guard_ok == 1` and the computed wave speeds satisfy `a_x >= 0` and `a_y >= 0`, and the residual of each flux against its speed is `<= 1e-12` (`wave_speed_nonnegative`). A kernel that dissipates one flux with a speed other than the one it reports fails the residual on a case whose two sides of that interface differ. A conforming kernel reads `0` for each residual (measured on every case). The cross-target judgment is applied: `cross_target_state_agreement` over `a_x` and `a_y` is `<= 1e-12`.
- `test_id`: `l0_symmetry_pairs_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov__compute_flux`
  - `expected_outcome`: `pass`
  - `quantities`: `guard_ok`, `symmetry_pair`
  - `judgment`: on `l0_distinct_1_axis`, `l0_distinct_1_xmir` and `l0_distinct_1_ymir`, `pair_residual` against `l0_distinct_1` of each of `F_star`, `G_star`, `a_x` and `a_y` is `<= 1.0e-12` (`symmetry_pair`: each term of §5 `<= 1.0e-12`), and `guard_ok == 1`. Every term of a transformed flux is the base's term with its sign reversed or not, and every wave speed is the same maximum of the same two sums, so a conforming kernel gives the rounding of the reordered operations alone (measured `0` on each output of the three cases); the bound is the suite's cross-target bound. A kernel without the absolute value in `a_x` (`u + c`) reads `0.145` (`a_x` of `l0_distinct_1_xmir`, `a_y` of `l0_distinct_1_axis`), and one without it in `a_y` reads `0.127` (`a_y` of `l0_distinct_1_axis`) and `0.0488` (`a_y` of `l0_distinct_1_ymir`); a kernel whose `y` flux is dissipated with `a_x` in place of `a_y` reads `0.0172` (`F_star` of `l0_distinct_1_axis`); a kernel whose `y` flux carries `hu^2/h` in its `hu` component in place of `hu hv/h` reads `0.0389` (`G_star` of both mirror cases) and `0.0201` (`F_star` of `l0_distinct_1_axis`). Each of these kernels also deviates from the closed form of `l0_distinct_1` (by `0.6`, `0.2`, `0.141` and `0.258`), so on this suite the pairs establish the transformed orientations, not a defect the closed forms leave open. The cross-target judgment is applied: `cross_target_state_agreement` over `F_star`, `G_star`, `a_x`, and `a_y` is `<= 1e-12`.
- `test_id`: `l0_invalid_dry_state_l_zero_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov__compute_flux`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `h==0` in `U_L`
  - `quantities`: `guard_ok`, `rejected_outputs_zero`
  - `judgment`: with `U_L = [0, 0, 0]` and the other three states those of the first distinct-state case, the `operation` refuses the input, in the failure direction: `guard_ok == 0`, and it writes `0` into `F_star`, `G_star`, `a_x` and `a_y` (`rejected_outputs_zero == 0`: every component of `F_star` and `G_star` and both wave speeds are exactly `0`, none of them the pre-call content `-999`). A model whose guard on `U_L` is `h<0` fails this test. A model that computes a flux from the rejected input fails this test. The cross-target judgment is not applied.
- `test_id`: `l0_invalid_dry_state_l_negative_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov__compute_flux`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `h<0` in `U_L`
  - `quantities`: `guard_ok`, `rejected_outputs_zero`
  - `judgment`: with `U_L = [-0.5, 0, 0]` and the other three states those of the first distinct-state case, the `operation` refuses the input, in the failure direction: `guard_ok == 0`, and it writes `0` into `F_star`, `G_star`, `a_x` and `a_y` (`rejected_outputs_zero == 0`: every component of `F_star` and `G_star` and both wave speeds are exactly `0`, none of them the pre-call content `-999`). A model whose guard on `U_L` is `h==0` fails this test. A model that computes a flux from the rejected input fails this test. The cross-target judgment is not applied.
- `test_id`: `l0_invalid_dry_state_r_zero_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov__compute_flux`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `h==0` in `U_R`
  - `quantities`: `guard_ok`, `rejected_outputs_zero`
  - `judgment`: with `U_R = [0, 0, 0]` and the other three states those of the first distinct-state case, the `operation` refuses the input, in the failure direction: `guard_ok == 0`, and it writes `0` into `F_star`, `G_star`, `a_x` and `a_y` (`rejected_outputs_zero == 0`: every component of `F_star` and `G_star` and both wave speeds are exactly `0`, none of them the pre-call content `-999`). A model whose guard omits `U_R`, or whose guard on `U_R` is `h<0`, fails this test. A model that computes a flux from the rejected input fails this test. The cross-target judgment is not applied.
- `test_id`: `l0_invalid_dry_state_r_negative_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov__compute_flux`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `h<0` in `U_R`
  - `quantities`: `guard_ok`, `rejected_outputs_zero`
  - `judgment`: with `U_R = [-0.5, 0, 0]` and the other three states those of the first distinct-state case, the `operation` refuses the input, in the failure direction: `guard_ok == 0`, and it writes `0` into `F_star`, `G_star`, `a_x` and `a_y` (`rejected_outputs_zero == 0`: every component of `F_star` and `G_star` and both wave speeds are exactly `0`, none of them the pre-call content `-999`). A model whose guard omits `U_R` fails this test. A model that computes a flux from the rejected input fails this test. The cross-target judgment is not applied.
- `test_id`: `l0_invalid_dry_state_b_zero_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov__compute_flux`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `h==0` in `U_B`
  - `quantities`: `guard_ok`, `rejected_outputs_zero`
  - `judgment`: with `U_B = [0, 0, 0]` and the other three states those of the first distinct-state case, the `operation` refuses the input, in the failure direction: `guard_ok == 0`, and it writes `0` into `F_star`, `G_star`, `a_x` and `a_y` (`rejected_outputs_zero == 0`: every component of `F_star` and `G_star` and both wave speeds are exactly `0`, none of them the pre-call content `-999`). A model whose guard omits `U_B`, or whose guard on `U_B` is `h<0`, fails this test. A model that computes a flux from the rejected input fails this test. The cross-target judgment is not applied.
- `test_id`: `l0_invalid_dry_state_b_negative_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov__compute_flux`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `h<0` in `U_B`
  - `quantities`: `guard_ok`, `rejected_outputs_zero`
  - `judgment`: with `U_B = [-0.5, 0, 0]` and the other three states those of the first distinct-state case, the `operation` refuses the input, in the failure direction: `guard_ok == 0`, and it writes `0` into `F_star`, `G_star`, `a_x` and `a_y` (`rejected_outputs_zero == 0`: every component of `F_star` and `G_star` and both wave speeds are exactly `0`, none of them the pre-call content `-999`). A model whose guard omits `U_B` fails this test. A model that computes a flux from the rejected input fails this test. The cross-target judgment is not applied.
- `test_id`: `l0_invalid_dry_state_t_zero_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov__compute_flux`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `h==0` in `U_T`
  - `quantities`: `guard_ok`, `rejected_outputs_zero`
  - `judgment`: with `U_T = [0, 0, 0]` and the other three states those of the first distinct-state case, the `operation` refuses the input, in the failure direction: `guard_ok == 0`, and it writes `0` into `F_star`, `G_star`, `a_x` and `a_y` (`rejected_outputs_zero == 0`: every component of `F_star` and `G_star` and both wave speeds are exactly `0`, none of them the pre-call content `-999`). A model whose guard omits `U_T`, or whose guard on `U_T` is `h<0`, fails this test. A model that computes a flux from the rejected input fails this test. The cross-target judgment is not applied.
- `test_id`: `l0_invalid_dry_state_t_negative_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov__compute_flux`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `h<0` in `U_T`
  - `quantities`: `guard_ok`, `rejected_outputs_zero`
  - `judgment`: with `U_T = [-0.5, 0, 0]` and the other three states those of the first distinct-state case, the `operation` refuses the input, in the failure direction: `guard_ok == 0`, and it writes `0` into `F_star`, `G_star`, `a_x` and `a_y` (`rejected_outputs_zero == 0`: every component of `F_star` and `G_star` and both wave speeds are exactly `0`, none of them the pre-call content `-999`). A model whose guard omits `U_T` fails this test. A model that computes a flux from the rejected input fails this test. The cross-target judgment is not applied.

## 7. Pass/fail aggregation rules
- `per_test.pass_rule`: `pass` when every predicate of the test holds.
- `per_test.xfail_rule`: `xfail` when `xfail_condition` is true and every predicate of the test holds.
- `suite.pass_rule`: `pass` when all `test_id` are `pass` or `xfail`.

## 8. Traceability
- Record `test_profile_id` and `test_profile_version` in `trial_meta.json`.
