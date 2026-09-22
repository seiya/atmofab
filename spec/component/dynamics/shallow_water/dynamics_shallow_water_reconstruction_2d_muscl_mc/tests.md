# Tests: 2D `MUSCL` reconstruction with the `MC` limiter (L0)

## 0. Meta information
- `test_profile_id`: `dynamics_shallow_water_reconstruction_2d_muscl_mc_l0`
- `test_profile_version`: `0.1.0`
- `status`: `draft`
- `spec_ref.spec_kind`: `component`
- `spec_ref.spec_id`: `dynamics_shallow_water_reconstruction_2d_muscl_mc`
- `spec_ref.spec_version`: `0.1.0`
- `spec_ref.controlled_spec_path`: `spec/component/dynamics/shallow_water/dynamics_shallow_water_reconstruction_2d_muscl_mc/controlled_spec.md`

## 1. Test purpose
This suite verifies the published `operation` `dynamics_shallow_water_reconstruction_2d_muscl_mc__reconstruct` at `L0`: the exact reconstruction of a bilinear field (linear along each index, with a slope that depends on the other index), the invariance of a constant field, the zero slope at a local extremum and beside a step, the value of the `MC` slope on a field where each of the three arguments of the minimum is the smallest in some cell, and the input guard for each clause of the Controlled Spec §4 that a captured state can observe: a ghost width below two (`ng<2`) and a padded extent that disagrees with `nx`, `ny`, `ng` (`nx_total/=nx+2*ng`, `ny_total/=ny+2*ng`). The extent clauses `nx<1` / `ny<1` have no case: a state of zero extent has no snapshot the judgment could read, so those two clauses are stated by the Controlled Spec and not observed by this suite.

## 2. Input-defaulting rules
- Every normal case uses `nx=6`, `ny=4`, `ng=2`, `nx_total=10`, `ny_total=8`. The field `U_in` is given on the whole padded array, ghost cells included, by a formula of the padded indices `i=1..10`, `j=1..8`; the ghost cells hold the same formula as the interior, because the operation reads them (Controlled Spec §2) and no boundary `component` runs in this suite. The `x` interfaces are `m=1..7` (`nx+1` values) and the `y` interfaces `n=1..5` (`ny+1` values), numbered as in the Controlled Spec §2: `x` interface `m` lies between the padded columns `m+1` and `m+2`, `y` interface `n` between the padded rows `n+1` and `n+2`; the interior row `j=1..4` is the padded row `j+2` and the interior column `i=1..6` the padded column `i+2`.
- The linear case uses the bilinear field `U_in(i,j) = 1 + 0.3*i + 0.2*j + 0.1*i*j`. Along every padded row `j` the field is linear in `i` with the slope `0.3 + 0.1*j`, and along every padded column `i` it is linear in `j` with the slope `0.2 + 0.1*i`, so the `x` slope of a cell differs from row to row and the `y` slope from column to column; a slope evaluated on a row or column other than the cell's own is visible.
- The constant case uses `U_in(i,j) = 2.5`.
- The extremum case uses `U_in(i,j) = 1` at every cell except `U_in(5,4) = 3` (the interior cell `i=3`, `j=2`).
- The step case uses `U_in(i,j) = 0` for `i<=5` and `U_in(i,j) = 1` for `i>=6`, at every `j` (a step between the interior columns `3` and `4`).
- The cubic case uses `U_in(i,j) = (i-5)^3 + (j-4)^3`. Its one-sided differences in `x` are `a = 3c^2 - 3c + 1` and `b = 3c^2 + 3c + 1` with `c = i-5`, both positive at every cell, and the three arguments of the `MC` minimum are `2a`, `2b` and the central difference `3c^2 + 1`; the `x` slope at the padded columns `2..9` is `28, 13, 2, 1, 2, 13, 28, 49`, the smallest argument being `2b` at column `4`, the central difference at columns `2, 3, 5, 7, 8, 9`, and `2a` at column `6`. The `y` slope at the padded rows `2..7` is, by the same formula in `c = j-4`, `13, 2, 1, 2, 13, 28` (`2b` at row `3`, `2a` at row `5`, the central difference elsewhere). At column `5` and at row `4` the three arguments are `2, 2, 1`, so the central difference is the strict minimum there too.
- The abnormal cases each violate exactly one clause of the Controlled Spec §4, with the bilinear field of the linear case on the padded array their extents define. The invalid-`ng` case uses `ng=1` with `nx=6`, `ny=4`, `nx_total=8`, `ny_total=6`. The invalid-`nx_total` case uses `nx_total=11` with `nx=6`, `ny=4`, `ng=2`, `ny_total=8` (`U_in` allocated `11` × `8`). The invalid-`ny_total` case uses `ny_total=9` with `nx=6`, `ny=4`, `ng=2`, `nx_total=10` (`U_in` allocated `10` × `9`).

## 3. Execution-control rules
`N/A`: this `component` reconstructs interface states from a provided field and defines no time-stepping or iteration. Execution control is the responsibility of the `problem` runner.

## 4. Case-expansion rules
`N/A`: the `L0` suite uses fixed inputs and defines no `case` sweep. Case expansion is defined at the `problem` level.

## 5. Diagnostics contract
- Require outputting `checks.linear_exact`, `checks.constant`, `checks.extremum`, `checks.step_bounded`, `checks.slope_value`, and `checks.input_guard` in `diagnostics.json`.
- The state of a case, captured after setup and after the run, consists of `U_in` (`nx_total` × `ny_total`, the input field, ghost cells included), `U_L` and `U_R` (`nx+1` × `ny` each), `U_B` and `U_T` (`nx` × `ny+1` each), and `grid_valid` (scalar, `1` when the `operation` returned true and `0` when it returned false). `U_in` is set at setup; the four interface arrays are zero at the capture after setup and are the arrays the `operation` wrote at the capture after the run; `grid_valid` is the value the `operation` returned, recorded as `1` / `0` because a state variable is real-valued. No state variable is derived by the checks module after the run: a deviation, a maximum or a bound over these variables is a `checks.<id>` status, never a state variable. Every judgment of §6 is a statement about these variables and the §2 inputs.
- Each check is computed on the case named here and is `na` on every other case: `checks.linear_exact` on the linear case; `checks.constant` on the constant case; `checks.extremum` on the extremum case; `checks.step_bounded` on the step case; `checks.slope_value` on the cubic case; `checks.input_guard` on every case (`pass` when `grid_valid = 1`, `fail` when `grid_valid = 0`).

## 6. Test definitions
- `test_id`: `l0_linear_field_exact_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_reconstruction_2d_muscl_mc__reconstruct`
  - `expected_outcome`: `pass`
  - `judgment`: with the bilinear field, every interface state equals the value of the field at the interface: `U_L(m,j) = U_R(m,j) = 1 + 0.3*(m+1.5) + 0.2*(j+2) + 0.1*(m+1.5)*(j+2)` for `m=1..7`, `j=1..4` (the field at the padded point `(m+1.5, j+2)`), and `U_B(i,n) = U_T(i,n) = 1 + 0.3*(i+2) + 0.2*(n+1.5) + 0.1*(i+2)*(n+1.5)` for `i=1..6`, `n=1..5` (the field at `(i+2, n+1.5)`), each within an absolute tolerance of `1e-12` (component-wise max deviation `<= 1e-12`), and `grid_valid = 1` (`checks.linear_exact`). Along a row the two one-sided differences of every cell are equal, so the `MC` minimum returns the central difference, which is the exact slope `0.3 + 0.1*j` of that padded row (`0.2 + 0.1*i` for the column slope), and the reconstruction is exact. A model with a zero slope (`p0`) gives `U_L(m,j) - U_R(m,j) = -(0.3 + 0.1*(j+2))`, between `-0.6` and `-0.9`, at every `x` interface; a model that evaluates the `x` slope on the padded row above the cell's own gives `U_L` too large by `0.05` at every interface (and a model that evaluates it on a ghost row gives an error of up to `0.25`); a model that evaluates the `y` slope on a neighbouring column errs by `0.05` in `U_B` and `U_T` in the same way.
- `test_id`: `l0_constant_field_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_reconstruction_2d_muscl_mc__reconstruct`
  - `expected_outcome`: `pass`
  - `judgment`: with the constant field, every element of `U_L`, `U_R`, `U_B` and `U_T` equals `2.5` exactly (component-wise max deviation `== 0`), and `grid_valid = 1` (`checks.constant`).
- `test_id`: `l0_extremum_zero_slope_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_reconstruction_2d_muscl_mc__reconstruct`
  - `expected_outcome`: `pass`
  - `judgment`: with the extremum field, every slope is zero (the two one-sided differences of the peak cell are `2` and `-2`, of opposite sign; those of its four neighbours have one zero factor; every other cell has both differences zero), so every interface state equals the cell value on its side: `U_L(m,j) = U_in(m+1, j+2)` and `U_R(m,j) = U_in(m+2, j+2)`, `U_B(i,n) = U_in(i+2, n+1)` and `U_T(i,n) = U_in(i+2, n+2)`. Numerically, `U_L(4,2) = 3`, `U_R(3,2) = 3`, `U_B(3,3) = 3`, `U_T(3,2) = 3`, and every other element of the four arrays is `1`, exactly (component-wise max deviation `== 0`), and `grid_valid = 1` (`checks.extremum`). A model with the unlimited central difference gives `U_L(3,2) = U_R(4,2) = 1.5` (slope `1` at the two neighbours of the peak) and fails.
- `test_id`: `l0_step_bounded_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_reconstruction_2d_muscl_mc__reconstruct`
  - `expected_outcome`: `pass`
  - `judgment`: with the step field, every slope is zero (the difference on the flat side of the step is zero at the two cells adjacent to it, and both differences are zero elsewhere), so `U_L(m,j) = 0` for `m<=4` and `1` for `m>=5`, `U_R(m,j) = 0` for `m<=3` and `1` for `m>=4`, at every `j`, and `U_B(i,n) = U_T(i,n) = 0` for `i<=3` and `1` for `i>=4`, at every `n`, exactly (component-wise max deviation `== 0`); in particular every element of the four arrays lies in `[0, 1]`, the range of the two cell values each is built from; and `grid_valid = 1` (`checks.step_bounded`). A model with the unlimited central difference gives `U_L(4,j) = 0.25` and `U_R(4,j) = 0.75` and fails.
- `test_id`: `l0_mc_slope_value_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_reconstruction_2d_muscl_mc__reconstruct`
  - `expected_outcome`: `pass`
  - `judgment`: with the cubic field, the interface states are the cell values plus or minus half the `MC` slopes of §2: `U_L(m,j) = P_L(m) + (j-2)^3` with `P_L(1..7) = -13, -1.5, 0, 0.5, 2, 14.5, 41`; `U_R(m,j) = P_R(m) + (j-2)^3` with `P_R(1..7) = -14.5, -2, -0.5, 0, 1.5, 13, 39.5`; `U_B(i,n) = (i-3)^3 + Q_B(n)` with `Q_B(1..5) = -1.5, 0, 0.5, 2, 14.5`; `U_T(i,n) = (i-3)^3 + Q_T(n)` with `Q_T(1..5) = -2, -0.5, 0, 1.5, 13`; each within an absolute tolerance of `1e-12` (component-wise max deviation `<= 1e-12`), and `grid_valid = 1` (`checks.slope_value`). The `x` tables are `P_L(m) = (m-4)^3 + s(m+1)/2` and `P_R(m) = (m-3)^3 - s(m+2)/2` with `s(2..9) = 28, 13, 2, 1, 2, 13, 28, 49` the `x` slope of §2; the `y` tables are `Q_B(n) = (n-3)^3 + r(n+1)/2` and `Q_T(n) = (n-2)^3 - r(n+2)/2` with `r(2..7) = 13, 2, 1, 2, 13, 28`. A model with the unlimited central difference gives `P_L(3) = 1` (slope `4` instead of `2` at column `4`) and `P_L(5) = 3` (slope `4` at column `6`); a model with the `minmod` limiter gives `P_L(2) = -4.5` (slope `7` instead of `13` at column `3`); a model with the `van Leer` limiter gives `P_L(2) = -2.885` (slope `2ab/(a+b) = 10.23` with `a=19`, `b=7`); each fails.
- `test_id`: `l0_invalid_ng_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_reconstruction_2d_muscl_mc__reconstruct`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `ng<2`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`
- `test_id`: `l0_invalid_nx_total_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_reconstruction_2d_muscl_mc__reconstruct`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `nx_total/=nx+2*ng`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`
- `test_id`: `l0_invalid_ny_total_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_reconstruction_2d_muscl_mc__reconstruct`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `ny_total/=ny+2*ng`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`

## 7. Pass/fail aggregation rules
- `per_test.pass_rule`: `pass` when the judgment expression is satisfied.
- `per_test.xfail_rule`: `xfail` when `xfail_condition` is true and `pass_when` is satisfied.
- `suite.pass_rule`: `pass` when all `test_id` are `pass` or `xfail`.

## 8. Traceability
- Record `test_profile_id` and `test_profile_version` in `trial_meta.json`.
