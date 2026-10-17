# Tests: 2D `SSPRK2` update (L0)

## 0. Meta information
- `test_profile_id`: `dynamics_shallow_water_time_update_2d_ssprk2_l0`
- `test_profile_version`: `0.5.0`
- `status`: `draft`
- `spec_ref.spec_kind`: `component`
- `spec_ref.spec_id`: `dynamics_shallow_water_time_update_2d_ssprk2`
- `spec_ref.spec_version`: `0.4.1`
- `spec_ref.controlled_spec_path`: `spec/component/dynamics/shallow_water/dynamics_shallow_water_time_update_2d_ssprk2/controlled_spec.md`

## 1. Test purpose
This suite verifies the published `operation` `dynamics_shallow_water_time_update_2d_ssprk2__advance` at `L0`: the zero-`RHS` invariance, the 2-stage weight consistency, the frozen-`RHS` exactness, the `z_b`-invariance, the axis-exchange symmetry pair, and the input guard for an invalid time step (`dt<=0`).

Of the transformations of `docs/TESTS.md` §Symmetry pairs, the suite carries the **axis exchange** $(h, hu, hv)(x, y) \to (h, hv, hu)(y, x)$, applied to `U_n`, `L_flux` and `S_b` alike, with `z_b` transposed and `dx` and `dy` exchanged, as a pair: the transformed case has `nx` and `ny` exchanged, so every loop of the update runs over the other extent, and a loop bounded by the wrong extent, which on the base case (`nx > ny`) only runs past the array, leaves cells unwritten on the transformed case (`nx < ny`). A pair establishes the transformed result only as the checks module captures it: a checks module that calls the `operation` on the base orientation and transforms the result captures the same `U_np1`, and no judgment of this suite separates the two; the call is the `Generate` reviewer's to hold to the case's arguments (`docs/workflow/CHECKS_MODULE_CONTRACT.md` §1). The two **mirrors** and the **translation** are admitted only trivially and carry no pair: each keeps the base's extents, and the `L0` update combines each element of `U_n`, `L_flux` and `S_b` with the same elements of the others by the same weights for every component and every cell, linearly, so a mirrored or translated case is the base's elements reordered within the same loop ranges, with one component of every input negated under a mirror, and its result is the base's result reordered and negated the same way whatever the kernel's loop bounds (measured: both loop-bound defects of `l0_symmetry_pairs_pass` give a mirror pair residual of exactly `0`). The closed-form judgment of §6 pins every element of the base case, so a mirror pair would observe nothing it does not.

## 2. Input-defaulting rules
- The normal case uses `dt>0`, `dx>0`, `dy>0`, and includes both `S_b=0` and `S_b!=0` in the evaluation target.
- Every case uses `ncomp=3`, `nx=4`, `ny=3`, the axis-exchange case excepted: `nx` differs from `ny`, so a loop over the cells bounded by the wrong extent does not cover the array.
- The frozen-`RHS` case `l0_frozen_rhs` uses `U_n(c,i,j) = 1 + 0.5*c + 0.1*i - 0.2*j`, `L_flux(c,i,j) = 0.01*c + 0.02*i - 0.03*j`, `S_b(c,i,j) = 0.02 + 0.005*c - 0.004*i + 0.001*j` (non-zero at every element, from `0.01` to `0.034`), `z_b(i,j) = 0.1*i + 0.05*j`, `dt=0.1`, `dx=0.5`, `dy=0.25`. The second and third components of each field differ at every cell, so an exchange of the momentum components is visible.
- The axis-exchange case `l0_frozen_rhs_axis` is the transformed copy of `l0_frozen_rhs` and sorts after it: `ncomp=3`, `nx=3`, `ny=4`, `dt=0.1`, `dx=0.25`, `dy=0.5`, and, with $c' = 1, 3, 2$ for the components `c = 1, 2, 3`, `U_n'(c,i,j) = 1 + 0.5*c' + 0.1*j - 0.2*i`, `L_flux'(c,i,j) = 0.01*c' + 0.02*j - 0.03*i`, `S_b'(c,i,j) = 0.02 + 0.005*c' - 0.004*j + 0.001*i`, `z_b'(i,j) = 0.1*j + 0.05*i`: each field of `l0_frozen_rhs` with its spatial axes transposed and its second and third components exchanged (`q'(1,i,j) = q(1,j,i)`, `q'(2,i,j) = q(3,j,i)`, `q'(3,i,j) = q(2,j,i)`), `z_b` transposed, and `dx` and `dy` exchanged.
- `L_flux` and `S_b` are supplied as fixed fields; the runner does not recompute them.
- The `z_b`-invariance case holds `U^n`, `L_flux`, and `S_b` fixed and evaluates the update for two distinct `z_b` fields in one case, comparing the two `U^{n+1}` results.
- The abnormal case uses `dt<=0`.

## 3. Execution-control rules
`N/A`: this `component` advances a single step for a given `dt` and does not control the time loop or its cadence. Execution control is the responsibility of the `problem` runner.

## 4. Case-expansion rules
`N/A`: the `L0` suite uses fixed inputs and defines no `case` sweep. Case expansion is defined at the `problem` level.

## 5. Diagnostics contract
- Require outputting `checks.zero_rhs_invariance`, `checks.stage_weight_consistency`, `checks.input_guard`, `checks.zb_invariance`, `checks.frozen_rhs_exactness`, and `checks.symmetry_pair` in `diagnostics.json`. `checks.frozen_rhs_exactness` is computed on `l0_frozen_rhs` and `l0_frozen_rhs_axis`; `checks.symmetry_pair` is computed on `l0_frozen_rhs_axis` and is `na` on every other case; it reads the `U_np1` of `l0_frozen_rhs`, which the checks module keeps from that case's run (`docs/workflow/CHECKS_MODULE_CONTRACT.md` §2, the accumulator ordering rule).
- `pair_residual` of `l0_frozen_rhs_axis` is `norm2(U_np1 - T(U_np1_base)) / norm2(U_np1_base)` over every element, where `U_np1_base` is the `U_np1` of `l0_frozen_rhs` captured after its run and `T` the axis exchange: the spatial axes transposed and the second and third components exchanged. The host evaluates it from the captured states (`at('l0_frozen_rhs')` with `roll(flip(transpose(q, 1, 2), 0), 1, 0, 0)`, `docs/TESTS.md` §Symmetry pairs).
- The state of a case, captured after setup and after the run, consists of the inputs `U_n`, `L_flux`, `S_b` (`ncomp` × `nx` × `ny` each) and `z_b` (`nx` × `ny`), written at setup; `U_np1` (`ncomp` × `nx` × `ny`, the sentinel `-999` at setup), the result of the `operation`; on the `z_b`-invariance case also the second `z_b` field and `U_np1_zb2` (the sentinel at setup), the result of the second evaluation; and `guard_pass`, a scalar recorded as a number because a state variable is real-valued: `-1` at setup, before the `operation` is called; `1` when the last call returned true; `0` when it returned false. The setup value `-1` is distinct from both values the call can return, so a case whose `operation` was not called captures `-1`. Every `pass` judgment of §6 requires `guard_pass = 1` on the cases it names, and every `xfail` judgment requires `guard_pass = 0` on its case; these are host-evaluated from the capture after the run (an output array of a rejected input is not defined by the Controlled Spec §4, so the flag is the only state that distinguishes a rejection from an executed update). `checks.input_guard` is `pass` when `guard_pass = 1` and `fail` otherwise.
- `cross_target_state_agreement` compares this target's run with every other target's certified variant of this node, case by case (`docs/TESTS.md` §Cross-target judgment): for one output `q` captured after the run and the same output `q_ref` of the same case in the other variant, it is `max|q - q_ref| / max(max|q|, max|q_ref|, 1)` over every element. The host evaluates it from the captured state, against each other variant separately; it is not a `diagnostics.json` field, and on a target with no other certified variant it holds vacuously.

## 6. Test definitions
- `test_id`: `l0_zero_rhs_invariance_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_time_update_2d_ssprk2__advance`
  - `expected_outcome`: `pass`
  - `judgment`: with `L_flux=0` and `S_b=0`, satisfy `U^{n+1}=U^n` within an absolute tolerance of `1e-12` (component-wise max deviation `<= 1e-12`). The cross-target judgment is applied: `cross_target_state_agreement` over `U_np1` is `<= 1e-12`.
- `test_id`: `l0_stage_weight_consistency_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_time_update_2d_ssprk2__advance`
  - `expected_outcome`: `pass`
  - `judgment`: the weights of the 2-stage composition are applied as `1/2,1/2`. The cross-target judgment is applied: `cross_target_state_agreement` over `U_np1` is `<= 1e-12`.
- `test_id`: `l0_frozen_rhs_exactness_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_time_update_2d_ssprk2__advance`
  - `expected_outcome`: `pass`
  - `judgment`: on `l0_frozen_rhs` and `l0_frozen_rhs_axis`, each with its own fields of §2, with fixed nonzero `L_flux` and `S_b`, satisfy `U^{n+1} = U^n + dt*(L_flux + S_b)` within an absolute tolerance of `1e-12` (component-wise max deviation `<= 1e-12`; frozen-field closed form; both stages use the identical RHS). The cross-target judgment is applied: `cross_target_state_agreement` over `U_np1` is `<= 1e-12`.
- `test_id`: `l0_zb_invariance_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_time_update_2d_ssprk2__advance`
  - `expected_outcome`: `pass`
  - `judgment`: with `U^n`, `L_flux`, and `S_b` held fixed, the update evaluated for two distinct `z_b` fields yields the identical `U^{n+1}` (`U_np1_zb2 = U_np1`, `z_b` is inert at L0). The cross-target judgment is applied: `cross_target_state_agreement` over `U_np1` and over `U_np1_zb2` is `<= 1e-12`.
- `test_id`: `l0_symmetry_pairs_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_time_update_2d_ssprk2__advance`
  - `expected_outcome`: `pass`
  - `judgment`: on `l0_frozen_rhs_axis`, `pair_residual` against `l0_frozen_rhs` is `<= 1.0e-12` (`checks.symmetry_pair`), and `guard_pass = 1`. Every element of the transformed case is computed by the same operations as the element of the base it is the image of, so a conforming kernel gives round-off at most (measured `0` with an independent reference of the Controlled Spec §3.1, which also meets the closed form exactly on either case); the bound is the suite's cross-target bound. Measured with that reference: a kernel whose loop over `j` in the second stage is bounded by `nx` passes the frozen-`RHS` judgment on `l0_frozen_rhs` (its out-of-range iterations write past the array, which no judgment reads) and gives `pair_residual = 0.54` on the axis case, whose last row is left unwritten (closed-form deviation `2.71` there); one whose loop over `j` in the first stage is bounded by `nx` gives `0.27` (closed-form deviation `1.35` there). On the axis case the frozen-`RHS` judgment fails with the pair: the transformed case is the evidence, and the pair states it as an equivariance of the base. The cross-target judgment is applied: `cross_target_state_agreement` over `U_np1` is `<= 1e-12`.
- `test_id`: `l0_invalid_dt_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_time_update_2d_ssprk2__advance`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `dt<=0`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`
  - `judgment`: on the abnormal case, `guard_pass = 0` and `checks.input_guard` fails.

## 7. Pass/fail aggregation rules
- `per_test.pass_rule`: `pass` when the judgment expression is satisfied.
- `per_test.xfail_rule`: `xfail` when `xfail_condition` is true and `pass_when` is satisfied.
- `suite.pass_rule`: `pass` when all `test_id` are `pass` or `xfail`.

## 8. Traceability
- Record `test_profile_id` and `test_profile_version` in `trial_meta.json`.
