# Tests: 2D `SSPRK2` update (L0)

## 0. Meta information
- `test_profile_id`: `dynamics_shallow_water_time_update_2d_ssprk2_l0`
- `test_profile_version`: `0.7.0`
- `status`: `draft`
- `spec_ref.spec_kind`: `component`
- `spec_ref.spec_id`: `dynamics_shallow_water_time_update_2d_ssprk2`
- `spec_ref.spec_version`: `0.4.1`
- `spec_ref.controlled_spec_path`: `spec/component/dynamics/shallow_water/dynamics_shallow_water_time_update_2d_ssprk2/controlled_spec.md`

## 1. Test purpose
This suite verifies the published `operation` `dynamics_shallow_water_time_update_2d_ssprk2__advance` at `L0`: the zero-`RHS` invariance, the 2-stage weight consistency, the frozen-`RHS` exactness, the `z_b`-invariance, the axis-exchange symmetry pair, a small `dt`, `dx`, `dy`, a component count other than three, and the input guard for each invalid input of the Controlled Spec §4 alone, at its boundary and on its negative side (`dt<=0`, `dx<=0`, `dy<=0`).

Of the transformations of `docs/TESTS.md` §Symmetry pairs, the suite carries the **axis exchange** $(h, hu, hv)(x, y) \to (h, hv, hu)(y, x)$, applied to `U_n`, `L_flux` and `S_b` alike, with `z_b` transposed and `dx` and `dy` exchanged, as a pair: the transformed case has `nx` and `ny` exchanged, so every loop of the update runs over the other extent, and a loop bounded by the wrong extent, which on the base case (`nx > ny`) only runs past the array, leaves cells unwritten on the transformed case (`nx < ny`). The two **mirrors** and the **translation** are admitted only trivially and carry no pair: each keeps the base's extents, and the `L0` update combines each element of `U_n`, `L_flux` and `S_b` with the same elements of the others by the same weights for every component and every cell, linearly, so a mirrored or translated case is the base's elements reordered within the same loop ranges, with one component of every input negated under a mirror, and its result is the base's result reordered and negated the same way whatever the kernel's loop bounds (measured: both loop-bound defects of `l0_symmetry_pairs_pass` give a mirror pair residual of exactly `0`). The closed-form judgment of §6 pins every element of the base case, so a mirror pair would observe nothing it does not.

## 2. Input-defaulting rules
- The normal case uses `dt>0`, `dx>0`, `dy>0`, and includes both `S_b=0` and `S_b!=0` in the evaluation target.
- Every case uses `ncomp=3`, `nx=4`, `ny=3`, the axis-exchange case and `l0_ncomp4` excepted: `nx` differs from `ny`, so a loop over the cells bounded by the wrong extent does not cover the array.
- The frozen-`RHS` case `l0_frozen_rhs` uses `U_n(c,i,j) = 1 + 0.5*c + 0.1*i - 0.2*j`, `L_flux(c,i,j) = 0.01*c + 0.02*i - 0.03*j`, `S_b(c,i,j) = 0.02 + 0.005*c - 0.004*i + 0.001*j` (non-zero at every element, from `0.01` to `0.034`), `z_b(i,j) = 0.1*i + 0.05*j`, `dt=0.1`, `dx=0.5`, `dy=0.25`. The second and third components of each field differ at every cell, so an exchange of the momentum components is visible.
- The axis-exchange case `l0_frozen_rhs_axis` is the transformed copy of `l0_frozen_rhs` and sorts after it: `ncomp=3`, `nx=3`, `ny=4`, `dt=0.1`, `dx=0.25`, `dy=0.5`, and, with $c' = 1, 3, 2$ for the components `c = 1, 2, 3`, `U_n'(c,i,j) = 1 + 0.5*c' + 0.1*j - 0.2*i`, `L_flux'(c,i,j) = 0.01*c' + 0.02*j - 0.03*i`, `S_b'(c,i,j) = 0.02 + 0.005*c' - 0.004*j + 0.001*i`, `z_b'(i,j) = 0.1*j + 0.05*i`: each field of `l0_frozen_rhs` with its spatial axes transposed and its second and third components exchanged (`q'(1,i,j) = q(1,j,i)`, `q'(2,i,j) = q(3,j,i)`, `q'(3,i,j) = q(2,j,i)`), `z_b` transposed, and `dx` and `dy` exchanged.
- `l0_stage_weight` uses the fields, the extents, `dx` and `dy` of `l0_frozen_rhs` with `dt=0.05`.
- `l0_zero_rhs` uses `U_n`, `z_b`, the extents, `dt`, `dx` and `dy` of `l0_frozen_rhs` with `L_flux = 0` and `S_b = 0` at every element.
- `l0_small_step` uses the fields, the extents and the `z_b` of `l0_frozen_rhs` with `dt=1.0e-3`, `dx=0.015625`, `dy=0.015625`: a guard that bounds `dt`, `dx` or `dy` from below by a positive value refuses it.
- `l0_ncomp4` uses `ncomp=4`, the `nx`, `ny`, `z_b`, `dt`, `dx` and `dy` of `l0_frozen_rhs`, and its formulas for `U_n`, `L_flux` and `S_b` at `c=1..4`: the Controlled Spec §4 has no clause on `ncomp`, so a loop over the components bounded by `3` leaves the fourth component unwritten and a guard on `ncomp` refuses the case.
- `L_flux` and `S_b` are supplied as fixed fields; the `operation` does not recompute them.
- Every case declares `z_b` by three inputs, the coefficients of `z_b(i,j) = a0 + a_i*i + a_j*j` (`a0=0`, `a_i=0.1`, `a_j=0.05` wherever §2 gives `0.1*i + 0.05*j`, and `a0=0`, `a_i=0.05`, `a_j=0.1` on `l0_frozen_rhs_axis`).
- The `z_b`-invariance pair is two cases, `l0_zb_invariance` and `l0_zb_invariance_zb2`, the second sorting after the first. Both hold the extents, `U_n`, `L_flux`, `S_b`, `dt`, `dx` and `dy` of `l0_frozen_rhs`, and they differ in the `z_b` inputs alone: `a0=0`, `a_i=0.1`, `a_j=0.05` on the first and `a0=0.5`, `a_i=-0.2`, `a_j=0.3` on the second.
- The abnormal cases each use the fields, the extents, `dt`, `dx` and `dy` of `l0_frozen_rhs` with one input replaced, so each violates one clause of the Controlled Spec §4 alone: `l0_invalid_dt` has `dt=-0.1`, `l0_dt0` has `dt=0`, `l0_dx0` has `dx=0`, `l0_dx_neg` has `dx=-0.5`, `l0_dy0` has `dy=0`, `l0_dy_neg` has `dy=-0.25`.
- Before the call the state holds the inputs above and `U_np1` holds the literal `-999` at every element, its pre-call content: `U_np1` is an operation-only output, so an element the `operation` does not write is captured as `-999` and fails every judgment of §6 over `U_np1`.

## 3. Execution-control rules
`N/A`: this `component` advances a single step for a given `dt` and does not control the time loop or its cadence. Execution control belongs to the `problem` that composes this `operation`.

distribute: x = grid.nx, y = grid.ny

## 4. Case-expansion rules
`N/A`: the `L0` suite uses fixed inputs and defines no `case` sweep. Case expansion is defined at the `problem` level.

## 5. Judged quantities
A test is judged by the host's predicates over the captures alone (`docs/TESTS.md` requirement 9). Each quantity below is named on the `quantities` line of the tests that judge it, and is evaluated from the state captured after the run in each of the test's target cases.

The host-rendered driver calls the `operation` once per case on the case's state, and the state captured before and after that call consists of `U_n`, `L_flux`, `S_b` (`ncomp` × `nx` × `ny` each) and `z_b` (`nx` × `ny`), as §2 states them; `U_np1` (`ncomp` × `nx` × `ny`, the pre-call content `-999` of §2 before the call), the result of the `operation`; and `guard_pass`. All six are arguments of the `operation`. `guard_pass` is its logical result, which the driver writes into the state after the call as `1` when the call returned true and `0` when it returned false; it starts at `0`, so a result the `operation` never assigned captures `0`. An output array of a rejected input is not defined by the Controlled Spec §4, so the flag is the only state that distinguishes a rejection from an executed update.

- `guard_pass`: the captured flag. It is `== 1` on the cases of a `pass` test and `== 0` on the cases of an `xfail` test.
- `zero_rhs_deviation`: `max|U_np1 - U_n|` over every component and cell.
- `stage_weight_deviation`: `max|U_np1 - U2|` over every component and cell, where, from the captured inputs, `rhs = L_flux + S_b`, `U1 = U_n + dt*rhs` and `U2 = 0.5*U_n + 0.5*(U1 + dt*rhs)`: the 2-stage composition with the weights `1/2,1/2`.
- `frozen_rhs_deviation`: `max|U_np1 - (U_n + dt*(L_flux + S_b))|` over every component and cell, with the case's own fields and `dt`.
- `zb_deviation`: on `l0_zb_invariance_zb2`, `max|U_np1 - U_np1_base|` over every component and cell, where `U_np1_base` is the `U_np1` of `l0_zb_invariance` captured after its run. The host reads the base through `at('l0_zb_invariance')`.
- `pair_residual`: on `l0_frozen_rhs_axis`, `norm2(U_np1 - T(U_np1_base)) / norm2(U_np1_base)` over every element, where `U_np1_base` is the `U_np1` of `l0_frozen_rhs` captured after its run and `T` the axis exchange: the spatial axes transposed and the second and third components exchanged. The host reads the base through `at('l0_frozen_rhs')` and applies `roll(flip(transpose(q, 1, 2), 0), 1, 0, 0)` (`docs/TESTS.md` §Symmetry pairs).
- `cross_target_state_agreement`: this target's run compared with every other target's certified variant of this node, case by case (`docs/TESTS.md` §Cross-target judgment): for one output `q` captured after the run and the same output `q_ref` of the same case in the other variant, `max|q - q_ref| / max(max|q|, max|q_ref|, 1)` over every element. The host evaluates it from the captured state, against each other variant separately; on a target with no other certified variant it holds vacuously. It is outside the `quantities` line.

## 6. Test definitions
- `test_id`: `l0_zero_rhs_invariance_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_time_update_2d_ssprk2__advance`
  - `expected_outcome`: `pass`
  - `quantities`: `guard_pass`, `zero_rhs_deviation`
  - `judgment`: with `L_flux=0` and `S_b=0`, `guard_pass == 1` and `U^{n+1}=U^n` within an absolute tolerance of `1e-12` (`zero_rhs_deviation <= 1e-12`). The cross-target judgment is applied: `cross_target_state_agreement` over `U_np1` is `<= 1e-12`.
- `test_id`: `l0_stage_weight_consistency_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_time_update_2d_ssprk2__advance`
  - `expected_outcome`: `pass`
  - `quantities`: `guard_pass`, `stage_weight_deviation`
  - `judgment`: `guard_pass == 1` and the weights of the 2-stage composition are applied as `1/2,1/2` (`stage_weight_deviation <= 1e-12`). The cross-target judgment is applied: `cross_target_state_agreement` over `U_np1` is `<= 1e-12`.
- `test_id`: `l0_frozen_rhs_exactness_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_time_update_2d_ssprk2__advance`
  - `expected_outcome`: `pass`
  - `quantities`: `frozen_rhs_deviation`, `guard_pass`
  - `judgment`: on `l0_frozen_rhs`, `l0_frozen_rhs_axis`, `l0_small_step` and `l0_ncomp4`, each with its own fields of §2, with fixed nonzero `L_flux` and `S_b`, `guard_pass == 1` and `U^{n+1} = U^n + dt*(L_flux + S_b)` within an absolute tolerance of `1e-12` (`frozen_rhs_deviation <= 1e-12`; frozen-field closed form; both stages use the identical RHS). The cross-target judgment is applied: `cross_target_state_agreement` over `U_np1` is `<= 1e-12`.
- `test_id`: `l0_zb_invariance_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_time_update_2d_ssprk2__advance`
  - `expected_outcome`: `pass`
  - `quantities`: `guard_pass`, `zb_deviation`
  - `judgment`: on `l0_zb_invariance` and `l0_zb_invariance_zb2`, with `U^n`, `L_flux`, and `S_b` held fixed, `guard_pass == 1` on both and the two distinct `z_b` fields yield the identical `U^{n+1}` (`zb_deviation == 0`: `z_b` is inert at L0). The cross-target judgment is applied: `cross_target_state_agreement` over `U_np1` is `<= 1e-12`.
- `test_id`: `l0_symmetry_pairs_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_time_update_2d_ssprk2__advance`
  - `expected_outcome`: `pass`
  - `quantities`: `guard_pass`, `pair_residual`
  - `judgment`: on `l0_frozen_rhs_axis`, `pair_residual` against `l0_frozen_rhs` is `<= 1.0e-12`, and `guard_pass == 1` on that case and on `l0_frozen_rhs`. Every element of the transformed case is computed by the same operations as the element of the base it is the image of, so a conforming kernel gives round-off at most (measured `0` with an independent reference of the Controlled Spec §3.1, which also meets the closed form exactly on either case); the bound is the suite's cross-target bound. Measured with that reference: a kernel whose loop over `j` in the second stage is bounded by `nx` writes past the array on `l0_frozen_rhs`, where no judgment reads those writes and the run may abort on them, and gives `pair_residual = 262` on the axis case, whose last row keeps its pre-call content `-999` (closed-form deviation `1001.71` there); one whose loop over `j` in the first stage is bounded by `nx` leaves the last row of its stage state unset, and with that stage state zeroed gives `0.27` (closed-form deviation `1.35` there; an unset local array gives another value, never the closed form). On the axis case the frozen-`RHS` judgment fails with the pair: the transformed case is the evidence, and the pair states it as an equivariance of the base. The cross-target judgment is applied: `cross_target_state_agreement` over `U_np1` is `<= 1e-12`.
- `test_id`: `l0_invalid_dt_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_time_update_2d_ssprk2__advance`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `dt<0`
  - `quantities`: `guard_pass`
  - `judgment`: on the abnormal case of §2 (`l0_invalid_dt`), the `operation` refuses the input: `guard_pass == 0`, in the failure direction. The cross-target judgment is not applied.
- `test_id`: `l0_invalid_dt_zero_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_time_update_2d_ssprk2__advance`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `dt=0`
  - `quantities`: `guard_pass`
  - `judgment`: on `l0_dt0`, with `dt=0` and every other input valid, the `operation` refuses the input: `guard_pass == 0`, in the failure direction. The cross-target judgment is not applied.
- `test_id`: `l0_invalid_dx_zero_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_time_update_2d_ssprk2__advance`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `dx=0`
  - `quantities`: `guard_pass`
  - `judgment`: on `l0_dx0`, with `dx=0` and every other input valid, the `operation` refuses the input: `guard_pass == 0`, in the failure direction. The cross-target judgment is not applied.
- `test_id`: `l0_invalid_dx_negative_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_time_update_2d_ssprk2__advance`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `dx<0`
  - `quantities`: `guard_pass`
  - `judgment`: on `l0_dx_neg`, with `dx=-0.5` and every other input valid, the `operation` refuses the input: `guard_pass == 0`, in the failure direction. The cross-target judgment is not applied.
- `test_id`: `l0_invalid_dy_zero_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_time_update_2d_ssprk2__advance`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `dy=0`
  - `quantities`: `guard_pass`
  - `judgment`: on `l0_dy0`, with `dy=0` and every other input valid, the `operation` refuses the input: `guard_pass == 0`, in the failure direction. The cross-target judgment is not applied.
- `test_id`: `l0_invalid_dy_negative_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_time_update_2d_ssprk2__advance`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `dy<0`
  - `quantities`: `guard_pass`
  - `judgment`: on `l0_dy_neg`, with `dy=-0.25` and every other input valid, the `operation` refuses the input: `guard_pass == 0`, in the failure direction. The cross-target judgment is not applied.

## 7. Pass/fail aggregation rules
- `per_test.pass_rule`: `pass` when every predicate of the test holds.
- `per_test.xfail_rule`: `xfail` when `xfail_condition` is true and every predicate of the test holds.
- `suite.pass_rule`: `pass` when all `test_id` are `pass` or `xfail`.

## 8. Traceability
- Record `test_profile_id` and `test_profile_version` in `trial_meta.json`.
