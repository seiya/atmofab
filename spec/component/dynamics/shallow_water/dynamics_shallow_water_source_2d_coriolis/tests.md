# Tests: 2D shallow water Coriolis source (L0)

## 0. Meta information
- `test_profile_id`: `dynamics_shallow_water_source_2d_coriolis_l0`
- `test_profile_version`: `0.3.0`
- `status`: `draft`
- `spec_ref.spec_kind`: `component`
- `spec_ref.spec_id`: `dynamics_shallow_water_source_2d_coriolis`
- `spec_ref.spec_version`: `0.2.0`
- `spec_ref.controlled_spec_path`: `spec/component/dynamics/shallow_water/dynamics_shallow_water_source_2d_coriolis/controlled_spec.md`

## 1. Test purpose
This suite verifies the published `operation` `dynamics_shallow_water_source_2d_coriolis__apply` at `L0`: the zero source for `f=0`, the pointwise formula with a row-dependent `f` and with an `f` that varies along both axes, the discrete no-work property, the three symmetry pairs the source admits, and the input guard for an invalid component count (`ncomp/=3`, below and above three).

The symmetry pairs (`docs/TESTS.md` §Symmetry pairs) are the **axis exchange** $(h, hu, hv)(x, y) \to (h, hv, hu)(y, x)$ and the two **mirrors** $x \to L_x - x$ with $hu \to -hu$ and $y \to L_y - y$ with $hv \to -hv$. Each reverses the sense of rotation, so each transformed case carries $f \to -f$ at the transformed cell (Controlled Spec §2), and its source is the base's source transformed the same way: $S' = (S_1, S_3, S_2)^T$ under the exchange, $(S_1, -S_2, S_3)$ reversed along `x` and $(S_1, S_2, -S_3)$ reversed along `y` under the mirrors. The translation is not a pair here: the source is pointwise, so a translated state with a translated `f` gives the translated source trivially, and the cases' states are not periodic. The extent clauses `nx<1` / `ny<1` of the Controlled Spec §4 have no case: a state of zero extent has no snapshot the judgment could read, so those two clauses are stated by the Controlled Spec and not observed by this suite.

## 2. Input-defaulting rules
- The normal cases use `ncomp=3`, `nx=4`, `ny=3`. The state is `h(i,j) = 1 + 0.1*i + 0.05*j`, `hu(i,j) = 0.1*i - 0.2*j`, `hv(i,j) = 0.3*j - 0.05*i`. `h` is non-uniform and non-unit at every cell so that the momentum-form source `f*hv` differs from the velocity form `f*hv/h` and from `f*h*hv`, and `hu` and `hv` differ at every cell so that a swap is visible. `hu` and `hv` are of order 1 and `f` of order `1e-4`, so `S` is of order `1e-4` and the absolute tolerance `1e-12` of §6 is a relative tolerance of order `1e-8` on `S`. Every case's Coriolis parameter is affine in the cell indices, `f(i,j) = f_0 + f_j*(j-1) + f_i*(i-1)`, and the case declares its three coefficients `f_0`, `f_j`, `f_i` as inputs; the `nx` × `ny` array the `operation` receives is built from them. The coefficients, not a declared array, are the case's input so that a host-evaluated judgment can rebuild `f` from the grid coordinates at the rank of the state `S` (a declared `nx` × `ny` array has a rank the `ncomp` × `nx` × `ny` source does not, and the predicate grammar does not broadcast across ranks). The row-dependent Coriolis parameter has `f_0 = 1.0e-4`, `f_j = 2.0e-5`, `f_i = 0`; the general one has `f_0 = 1.0e-4`, `f_j = 2.0e-5`, `f_i = 3.0e-6`, whose twelve values are pairwise distinct (from `1.0e-4` to `1.49e-4`), so a value read from another cell, another row or another column is visible.
- The zero-`f` case uses the same state with `f_0 = f_j = f_i = 0`.
- The normal cases are `l0_zero_f` (the zero `f`), `l0_row_f` (the row-dependent `f`) and `l0_general_f` (the general `f`), each on the state above, and three transformed copies of `l0_general_f`, each sorting after it:
  - `l0_general_f_axis`: `nx=3`, `ny=4`, `h'(i,j) = h(j,i)`, `hu'(i,j) = hv(j,i)`, `hv'(i,j) = hu(j,i)`, `f'(i,j) = -f(j,i)`, which is `f_0 = -1.0e-4`, `f_j = -3.0e-6`, `f_i = -2.0e-5` (with `h`, `hu`, `hv`, `f` those of `l0_general_f`).
  - `l0_general_f_xmir`: `nx=4`, `ny=3`, `h'(i,j) = h(5-i,j)`, `hu'(i,j) = -hu(5-i,j)`, `hv'(i,j) = hv(5-i,j)`, `f'(i,j) = -f(5-i,j)`, which is `f_0 = -1.09e-4`, `f_j = -2.0e-5`, `f_i = 3.0e-6`.
  - `l0_general_f_ymir`: `nx=4`, `ny=3`, `h'(i,j) = h(i,4-j)`, `hu'(i,j) = hu(i,4-j)`, `hv'(i,j) = -hv(i,4-j)`, `f'(i,j) = -f(i,4-j)`, which is `f_0 = -1.4e-4`, `f_j = 2.0e-5`, `f_i = -3.0e-6`. Built from these coefficients, each transformed `f` equals the reversed `-f` of `l0_general_f` to `2.7e-20`, the rounding of the coefficient sums.
- The abnormal cases `l0_ncomp2` and `l0_ncomp4` each violate one clause of the Controlled Spec §4, with the row-dependent `f`, `nx=4`, `ny=3`. The below-three case uses `ncomp=2` and a state of two components; the above-three case uses `ncomp=4` and a state of four components (the fourth component `0`).

## 3. Execution-control rules
`N/A`: this `component` evaluates a pointwise source at a supplied state and defines no time-stepping or iteration. Execution control is the responsibility of the `problem` runner.

## 4. Case-expansion rules
`N/A`: the `L0` suite uses fixed inputs and defines no `case` sweep. Case expansion is defined at the `problem` level.

## 5. Diagnostics contract
- Require outputting `checks.zero_f`, `checks.pointwise`, `checks.energy_neutral`, `checks.symmetry_pair`, and `checks.input_guard` in `diagnostics.json`. `checks.symmetry_pair` is computed on the three transformed cases and is `na` on every other case; it reads the source of `l0_general_f`, which the checks module keeps from that case's run (`docs/workflow/CHECKS_MODULE_CONTRACT.md` §2, the accumulator ordering rule).
- `pair_residual` of a transformed case is `norm2(S - T(S_base)) / norm2(S_base)` over every element, where `S_base` is the source of `l0_general_f` captured after its run and `T` the case's transformation of §1 (for the exchange, the spatial axes transposed and the second and third components exchanged; for a mirror, the array reversed along the mirrored axis and the sign of one momentum component reversed). The host evaluates it from the captured states (`at('l0_general_f')` with `transpose`, `flip` and `roll`, `docs/TESTS.md` §Symmetry pairs).
- `cross_target_state_agreement` compares this target's run with every other target's certified variant of this node, case by case (`docs/TESTS.md` §Cross-target judgment): for one output `q` captured after the run and the same output `q_ref` of the same case in the other variant, it is `max|q - q_ref| / max(max|q|, max|q_ref|, 1)` over every element. The host evaluates it from the captured state, against each other variant separately; it is not a `diagnostics.json` field, and on a target with no other certified variant it holds vacuously.

## 6. Test definitions
- `test_id`: `l0_zero_f_gives_zero_source_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_source_2d_coriolis__apply`
  - `expected_outcome`: `pass`
  - `judgment`: on `l0_zero_f` (`f=0` at every cell), every component of `S` is exactly `0` at every cell (component-wise max `|S| == 0`). The cross-target judgment is applied: `cross_target_state_agreement` over `S` is `<= 1e-12`.
- `test_id`: `l0_pointwise_formula_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_source_2d_coriolis__apply`
  - `expected_outcome`: `pass`
  - `judgment`: on `l0_row_f`, `l0_general_f` and the three transformed cases, each with its own `f` and state of §2, satisfy `S(1,i,j) = 0`, `S(2,i,j) = f(i,j)*hv(i,j)`, and `S(3,i,j) = -f(i,j)*hu(i,j)` at every cell within an absolute tolerance of `1e-12` (component-wise max deviation `<= 1e-12`). On `l0_general_f` a kernel that reads `f` from the first column for every column deviates by `6.3e-6`. The cross-target judgment is applied: `cross_target_state_agreement` over `S` is `<= 1e-12`.
- `test_id`: `l0_energy_neutral_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_source_2d_coriolis__apply`
  - `expected_outcome`: `pass`
  - `judgment`: on the cases of `l0_pointwise_formula_pass`, satisfy `hu(i,j)*S(2,i,j) + hv(i,j)*S(3,i,j) = 0` at every cell within an absolute tolerance of `1e-12` (max over cells of the absolute value `<= 1e-12`): the discrete evidence that the source does no work. The cross-target judgment is applied: `cross_target_state_agreement` over `S` is `<= 1e-12`.
- `test_id`: `l0_symmetry_pairs_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_source_2d_coriolis__apply`
  - `expected_outcome`: `pass`
  - `judgment`: on `l0_general_f_axis`, `l0_general_f_xmir` and `l0_general_f_ymir`, `pair_residual` against `l0_general_f` is `<= 1.0e-12` (`checks.symmetry_pair`). Every product of the source is the base's product with both factors' signs reversed or with neither, so a conforming kernel gives the rounding of the transformed `f` alone (measured `1.0e-16`, `7.2e-17` and `1.8e-16` on the axis, `x`-mirror and `y`-mirror cases); the bound is the suite's cross-target bound. The cross-target judgment is applied: `cross_target_state_agreement` over `S` is `<= 1e-12`.
- `test_id`: `l0_invalid_ncomp_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_source_2d_coriolis__apply`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `ncomp<3`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`
- `test_id`: `l0_invalid_ncomp_above_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_source_2d_coriolis__apply`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `ncomp>3`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`

## 7. Pass/fail aggregation rules
- `per_test.pass_rule`: `pass` when the judgment expression is satisfied.
- `per_test.xfail_rule`: `xfail` when `xfail_condition` is true and `pass_when` is satisfied.
- `suite.pass_rule`: `pass` when all `test_id` are `pass` or `xfail`.

## 8. Traceability
- Record `test_profile_id` and `test_profile_version` in `trial_meta.json`.
