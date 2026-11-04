# Tests: 2D shallow water Coriolis source (L0)

## 0. Meta information
- `test_profile_id`: `dynamics_shallow_water_source_2d_coriolis_l0`
- `test_profile_version`: `0.5.0`
- `status`: `draft`
- `spec_ref.spec_kind`: `component`
- `spec_ref.spec_id`: `dynamics_shallow_water_source_2d_coriolis`
- `spec_ref.spec_version`: `0.2.0`
- `spec_ref.controlled_spec_path`: `spec/component/dynamics/shallow_water/dynamics_shallow_water_source_2d_coriolis/controlled_spec.md`

## 1. Test purpose
This suite verifies the published `operation` `dynamics_shallow_water_source_2d_coriolis__apply` at `L0`: the zero source for `f=0`, the pointwise formula with a row-dependent `f` and with an `f` that varies along both axes, the discrete no-work property, the three symmetry pairs the source admits, the smallest valid extent, and the input guard for each invalid input of the Controlled Spec §4 at its boundary (`ncomp/=3`, below and above three; `nx=0`; `ny=0`).

The symmetry pairs (`docs/TESTS.md` §Symmetry pairs) are the **axis exchange** $(h, hu, hv)(x, y) \to (h, hv, hu)(y, x)$ and the two **mirrors** $x \to L_x - x$ with $hu \to -hu$ and $y \to L_y - y$ with $hv \to -hv$. Each reverses the sense of rotation, so each transformed case carries $f \to -f$ at the transformed cell (Controlled Spec §2), and its source is the base's source transformed the same way: $S' = (S_1, S_3, S_2)^T$ under the exchange, $(S_1, -S_2, S_3)$ reversed along `x` and $(S_1, S_2, -S_3)$ reversed along `y` under the mirrors. The translation is not a pair here: the source is pointwise, so a translated state with a translated `f` gives the translated source trivially, and the cases' states are not periodic.

## 2. Input-defaulting rules
- The normal cases use `ncomp=3`, `nx=4`, `ny=3`. The state is `h(i,j) = 1 + 0.1*i + 0.05*j`, `hu(i,j) = 0.1*i - 0.2*j`, `hv(i,j) = 0.3*j - 0.05*i`. `h` is non-uniform and non-unit at every cell so that the momentum-form source `f*hv` differs from the velocity form `f*hv/h` and from `f*h*hv`, and `hu` and `hv` differ at every cell so that a swap is visible. `hu` and `hv` are of order 1 and `f` of order `1e-4`, so `S` is of order `1e-4` and the absolute tolerance `1e-12` of §6 is a relative tolerance of order `1e-8` on `S`. Every case's Coriolis parameter is affine in the cell indices, `f(i,j) = f_0 + f_j*(j-1) + f_i*(i-1)`, and the case declares its three coefficients `f_0`, `f_j`, `f_i` as inputs; the `nx` × `ny` array the `operation` receives is built from them. The coefficients, not a declared array, are the case's input so that a host-evaluated judgment can rebuild `f` from the grid coordinates at the rank of the state `S` (a declared `nx` × `ny` array has a rank the `ncomp` × `nx` × `ny` source does not, and the predicate grammar does not broadcast across ranks). The row-dependent Coriolis parameter has `f_0 = 1.0e-4`, `f_j = 2.0e-5`, `f_i = 0`; the general one has `f_0 = 1.0e-4`, `f_j = 2.0e-5`, `f_i = 3.0e-6`, whose twelve values are pairwise distinct (from `1.0e-4` to `1.49e-4`), so a value read from another cell, another row or another column is visible.
- The zero-`f` case uses the same state with `f_0 = f_j = f_i = 0`.
- The normal cases are `l0_zero_f` (the zero `f`), `l0_row_f` (the row-dependent `f`) and `l0_general_f` (the general `f`), each on the state above, and three transformed copies of `l0_general_f`, each sorting after it:
  - `l0_general_f_axis`: `nx=3`, `ny=4`, `h'(i,j) = h(j,i)`, `hu'(i,j) = hv(j,i)`, `hv'(i,j) = hu(j,i)`, `f'(i,j) = -f(j,i)`, which is `f_0 = -1.0e-4`, `f_j = -3.0e-6`, `f_i = -2.0e-5` (with `h`, `hu`, `hv`, `f` those of `l0_general_f`).
  - `l0_general_f_xmir`: `nx=4`, `ny=3`, `h'(i,j) = h(5-i,j)`, `hu'(i,j) = -hu(5-i,j)`, `hv'(i,j) = hv(5-i,j)`, `f'(i,j) = -f(5-i,j)`, which is `f_0 = -1.09e-4`, `f_j = -2.0e-5`, `f_i = 3.0e-6`.
  - `l0_general_f_ymir`: `nx=4`, `ny=3`, `h'(i,j) = h(i,4-j)`, `hu'(i,j) = hu(i,4-j)`, `hv'(i,j) = -hv(i,4-j)`, `f'(i,j) = -f(i,4-j)`, which is `f_0 = -1.4e-4`, `f_j = 2.0e-5`, `f_i = -3.0e-6`. Built from these coefficients, each transformed `f` equals the reversed `-f` of `l0_general_f` to `2.7e-20`, the rounding of the coefficient sums.
- The abnormal cases `l0_ncomp2` and `l0_ncomp4` each violate one clause of the Controlled Spec §4, with the row-dependent `f`, `nx=4`, `ny=3`. The below-three case uses `ncomp=2` and a state of two components; the above-three case uses `ncomp=4` and a state of four components (the fourth component `0`). The extent cases `l0_nx0` (`nx=0`, `ny=3`) and `l0_ny0` (`nx=4`, `ny=0`) each violate one extent clause alone at its boundary, with `ncomp=3` and the general `f`; their state has zero cells.
- `l0_min_extent` is the smallest valid grid, `nx=1`, `ny=1`, with `ncomp=3` and the state and the general `f` above at that one cell (`f = 1.0e-4`): a guard written `nx>1` or `ny>1` refuses it, and an extent guard written `nx>=0` or `ny>=0` accepts `l0_nx0` or `l0_ny0`.
- Before the call the state holds `U` as above and `S` holds the literal `-999` at every element, its pre-call content: `S` is an operation-only output, so an element the `operation` does not write is captured as `-999` and fails every judgment of §6 over `S`.

## 3. Execution-control rules
`N/A`: this `component` evaluates a pointwise source at a supplied state and defines no time-stepping or iteration. Execution control belongs to the `problem` that composes this `operation`.

## 4. Case-expansion rules
`N/A`: the `L0` suite uses fixed inputs and defines no `case` sweep. Case expansion is defined at the `problem` level.

## 5. Judged quantities
A test is judged by the host's predicates over the captures alone (`docs/TESTS.md` requirement 9). Each quantity below is named on the `quantities` line of the tests that judge it, and is evaluated from the state captured after the run in each of the test's target cases.

The host-rendered driver calls the `operation` once per case on the case's state, and the state captured before and after that call consists of `U` (the state of §2), `S` (`ncomp` × `nx` × `ny`, the pre-call content `-999` of §2 before the call) and `guard_pass`. All three are arguments of the `operation`. `guard_pass` is its logical result, which the driver writes into the state after the call as `1` when the call returned true and `0` when it returned false; it starts at `0`, so a result the `operation` never assigned captures `0`. An output array of a rejected input is not defined by the Controlled Spec §4, so the flag is the only state that distinguishes a rejection from an executed mapping.

- `guard_pass`: the captured flag. It is `== 1` on the cases of a `pass` test and `== 0` on the cases of an `xfail` test.
- `max_abs_s`: `max|S|` over every component and cell.
- `pointwise_max_dev`: the largest of `|S(1,i,j)|`, `|S(2,i,j) - f(i,j)*hv(i,j)|` and `|S(3,i,j) + f(i,j)*hu(i,j)|` over every cell, with `f`, `hu`, `hv` those of the case's §2 inputs.
- `energy_max_abs`: `max|hu(i,j)*S(2,i,j) + hv(i,j)*S(3,i,j)|` over every cell.
- `pair_residual`: on a transformed case, `norm2(S - T(S_base)) / norm2(S_base)` over every element, where `S_base` is the source of `l0_general_f` captured after its run and `T` the case's transformation of §1 (for the exchange, the spatial axes transposed and the second and third components exchanged; for a mirror, the array reversed along the mirrored axis and the sign of one momentum component reversed). The host reads `S_base` through `at('l0_general_f')` and applies `T` with `transpose`, `flip` and `roll` (`docs/TESTS.md` §Symmetry pairs).
- `cross_target_state_agreement`: this target's run compared with every other target's certified variant of this node, case by case (`docs/TESTS.md` §Cross-target judgment): for one output `q` captured after the run and the same output `q_ref` of the same case in the other variant, `max|q - q_ref| / max(max|q|, max|q_ref|, 1)` over every element. The host evaluates it from the captured state, against each other variant separately; on a target with no other certified variant it holds vacuously. It is outside the `quantities` line.

## 6. Test definitions
- `test_id`: `l0_zero_f_gives_zero_source_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_source_2d_coriolis__apply`
  - `expected_outcome`: `pass`
  - `quantities`: `guard_pass`, `max_abs_s`
  - `judgment`: on `l0_zero_f` (`f=0` at every cell), `guard_pass == 1` and every component of `S` is exactly `0` at every cell (`max_abs_s == 0`). The cross-target judgment is applied: `cross_target_state_agreement` over `S` is `<= 1e-12`.
- `test_id`: `l0_pointwise_formula_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_source_2d_coriolis__apply`
  - `expected_outcome`: `pass`
  - `quantities`: `guard_pass`, `pointwise_max_dev`
  - `judgment`: on `l0_row_f`, `l0_general_f`, the three transformed cases and `l0_min_extent`, each with its own `f` and state of §2, `guard_pass == 1` and `S(1,i,j) = 0`, `S(2,i,j) = f(i,j)*hv(i,j)`, and `S(3,i,j) = -f(i,j)*hu(i,j)` at every cell within an absolute tolerance of `1e-12` (`pointwise_max_dev <= 1e-12`). On `l0_general_f` a kernel that reads `f` from the first column for every column deviates by `6.3e-6`. The cross-target judgment is applied: `cross_target_state_agreement` over `S` is `<= 1e-12`.
- `test_id`: `l0_energy_neutral_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_source_2d_coriolis__apply`
  - `expected_outcome`: `pass`
  - `quantities`: `energy_max_abs`, `guard_pass`
  - `judgment`: on the cases of `l0_pointwise_formula_pass`, `guard_pass == 1` and `hu(i,j)*S(2,i,j) + hv(i,j)*S(3,i,j) = 0` at every cell within an absolute tolerance of `1e-12` (`energy_max_abs <= 1e-12`): the discrete evidence that the source does no work. The cross-target judgment is applied: `cross_target_state_agreement` over `S` is `<= 1e-12`.
- `test_id`: `l0_symmetry_pairs_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_source_2d_coriolis__apply`
  - `expected_outcome`: `pass`
  - `quantities`: `guard_pass`, `pair_residual`
  - `judgment`: on `l0_general_f_axis`, `l0_general_f_xmir` and `l0_general_f_ymir`, `guard_pass == 1` and `pair_residual` against `l0_general_f` is `<= 1.0e-12`. Every product of the source is the base's product with both factors' signs reversed or with neither, so a conforming kernel gives the rounding of the transformed `f` alone (measured `1.0e-16`, `7.2e-17` and `1.8e-16` on the axis, `x`-mirror and `y`-mirror cases); the bound is the suite's cross-target bound. The cross-target judgment is applied: `cross_target_state_agreement` over `S` is `<= 1e-12`.
- `test_id`: `l0_invalid_ncomp_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_source_2d_coriolis__apply`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `ncomp<3`
  - `quantities`: `guard_pass`
  - `judgment`: on `l0_ncomp2`, the `operation` refuses the input: `guard_pass == 0`, in the failure direction. The cross-target judgment is not applied.
- `test_id`: `l0_invalid_ncomp_above_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_source_2d_coriolis__apply`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `ncomp>3`
  - `quantities`: `guard_pass`
  - `judgment`: on `l0_ncomp4`, the `operation` refuses the input: `guard_pass == 0`, in the failure direction. The cross-target judgment is not applied.
- `test_id`: `l0_invalid_nx_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_source_2d_coriolis__apply`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `nx<1`
  - `quantities`: `guard_pass`
  - `judgment`: on `l0_nx0`, the `operation` refuses the input: `guard_pass == 0`, in the failure direction. The state has no cell, so the flag is all there is to judge. The cross-target judgment is not applied.
- `test_id`: `l0_invalid_ny_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_source_2d_coriolis__apply`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `ny<1`
  - `quantities`: `guard_pass`
  - `judgment`: on `l0_ny0`, the `operation` refuses the input: `guard_pass == 0`, in the failure direction. The state has no cell, so the flag is all there is to judge. The cross-target judgment is not applied.

## 7. Pass/fail aggregation rules
- `per_test.pass_rule`: `pass` when every predicate of the test holds.
- `per_test.xfail_rule`: `xfail` when `xfail_condition` is true and every predicate of the test holds.
- `suite.pass_rule`: `pass` when all `test_id` are `pass` or `xfail`.

## 8. Traceability
- Record `test_profile_id` and `test_profile_version` in `trial_meta.json`.
