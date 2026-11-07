# Tests: 2D periodic-boundary mapping (L0)

## 0. Meta information
- `test_profile_id`: `dynamics_shallow_water_boundary_2d_periodic_copy_l0`
- `test_profile_version`: `0.5.0`
- `status`: `draft`
- `spec_ref.spec_kind`: `component`
- `spec_ref.spec_id`: `dynamics_shallow_water_boundary_2d_periodic_copy`
- `spec_ref.spec_version`: `0.2.1`
- `spec_ref.controlled_spec_path`: `spec/component/dynamics/shallow_water/dynamics_shallow_water_boundary_2d_periodic_copy/controlled_spec.md`

## 1. Test purpose
This suite verifies the published `operation` `dynamics_shallow_water_boundary_2d_periodic_copy__apply` at `L0`: the `x`-direction and `y`-direction periodic ghost-cell mapping (the `y` ghost rows including the corner ghost cells) at the ghost widths `ng=1` and `ng=2`, the three symmetry pairs the mapping admits, the smallest valid grid, a large grid, and the input guard for each invalid input of the Controlled Spec §4 alone at its boundary (`ny<2`, `nx<2`, `ng<1`).

The mapping admits four transformations (`docs/TESTS.md` §Symmetry pairs); the suite carries three of them as pairs and states why it does not carry the fourth, the translation. The **axis exchange**: the padded field transposed, with `nx` and `ny` and `nx_total` and `ny_total` exchanged, maps to the transposed output; the transposed case runs the `x` wrap over what were the base's columns and the `y` wrap over what were its rows, and the exchanged extents bound each loop of the mapping by the other extent. The **mirrors**: the padded field reversed along the first array axis, and along the second, maps to the reversed output; the reversed case fills each left ghost column from what was the base's right interior edge and each bottom ghost row from what was its top interior edge. The mapping is linear in the field and is applied to one field at a time, so the sign reversal of the mirrored momentum component commutes with it and the mirror pairs are judged without it. The **translation** of the periodic interior is admitted by the mapping but is not a transformation of the padded array: a cyclic shift of the padded extent is not the shift of the periodic interior, whose period is `nx` (or `ny`), not `nx_total` (or `ny_total`). The suite carries no translation pair; the `problem` that composes this mapping carries the translation of the whole state.

## 2. Input-defaulting rules
- The base normal case `l0_ng1` uses `nx=4`, `ny=3`, `ng=1`, `nx_total=6`, `ny_total=5`: `nx` differs from `ny`, so the axis exchange is not the identity. The padded index ranges are `i=1..nx_total`, `j=1..ny_total`, and the interior is `i=ng+1..ng+nx`, `j=ng+1..ng+ny`. The interior of `U_in` is filled with pairwise-distinct values, `U_in(i,j) = 10*i + j` for the interior indices (from `22` to `54`), so that every ghost cell identifies the interior cell it was mapped from. The ghost cells of `U_in` are filled with a sentinel value (`-999`) that no interior cell holds, so that a ghost cell of `U_in` read by mistake, or a ghost cell of `U_out` left unwritten, is visible in the output.
- Three transformed copies of `l0_ng1`, each sorting after it, with `ng=1`:
  - `l0_ng1_axis`: `nx=3`, `ny=4`, `nx_total=5`, `ny_total=6`, `U_in'(i,j) = U_in(j,i)` with `U_in` that of `l0_ng1` (interior `U_in'(i,j) = 10*j + i`, ghost cells the sentinel).
  - `l0_ng1_xmir`: `nx=4`, `ny=3`, `nx_total=6`, `ny_total=5`, `U_in'(i,j) = U_in(7-i,j)` (interior `U_in'(i,j) = 10*(7-i) + j`, ghost cells the sentinel).
  - `l0_ng1_ymir`: `nx=4`, `ny=3`, `nx_total=6`, `ny_total=5`, `U_in'(i,j) = U_in(i,6-j)` (interior `U_in'(i,j) = 10*i + 6 - j`, ghost cells the sentinel).
  Under either reversal and under the transpose the sentinel ghost cells stay ghost cells.
- `l0_ng2` is the grid of `l0_ng1` at the next ghost width: `nx=4`, `ny=3`, `ng=2`, `nx_total=8`, `ny_total=7`, with the interior `U_in(i,j) = 10*i + j` and the ghost cells the sentinel, so that the ghost width enters every index of the mapping.
- `l0_min_grid` is the smallest valid grid: `nx=2`, `ny=2`, `ng=1`, `nx_total=4`, `ny_total=4`, with the interior `U_in(i,j) = 10*i + j` and the ghost cells the sentinel. A guard written `nx>2` or `ny>2` refuses it.
- `l0_large_grid` is a large grid: `nx=256`, `ny=256`, `ng=1`, `nx_total=258`, `ny_total=258`, with the interior `U_in(i,j) = 1000*i + j`, pairwise distinct at these extents, and the ghost cells the sentinel. An upper limit on `nx` or `ny` that no clause of the Controlled Spec §4 states refuses it, and the `problem`s that compose this mapping run at such extents.
- The abnormal cases use the interior and sentinel filling of the normal cases, and each violates one clause of the Controlled Spec §4 alone, at its boundary. `l0_invalid_ny` uses `ny=1` with `nx=4`, `ng=1`, `nx_total=6`, `ny_total=3`. `l0_invalid_nx` uses `nx=1` with `ny=3`, `ng=1`, `nx_total=3`, `ny_total=5`. `l0_invalid_ng` uses `ng=0` with `nx=4`, `ny=3`, `nx_total=4`, `ny_total=3`; every cell of its field is interior.
- Before the call the state holds `U_in` as above and `U_out` holds the literal `-999` at every element, its pre-call content: `U_out` is an operation-only output, so an element the `operation` does not write is captured as `-999` and fails every judgment of §6 over `U_out`.

## 3. Execution-control rules
`N/A`: this `component` applies a boundary mapping to a provided field and defines no time-stepping or iteration. Execution control belongs to the `problem` that composes this `operation`.

distribute: none

## 4. Case-expansion rules
`N/A`: the `L0` suite uses fixed inputs and defines no `case` sweep. Case expansion is defined at the `problem` level.

## 5. Judged quantities
A test is judged by the host's predicates over the captures alone (`docs/TESTS.md` requirement 9). Each quantity below is named on the `quantities` line of the tests that judge it, and is evaluated from the state captured after the run in each of the test's target cases.

The host-rendered driver calls the `operation` once per case on the case's state, with `nx`, `ny`, `ng`, `nx_total`, `ny_total` from the case's inputs and `U_in`, `U_out`, `grid_valid` bound to the states of those names. The state captured before and after that call consists of `U_in` (the padded input of §2), `U_out` (`nx_total` × `ny_total`, the pre-call content `-999` of §2 before the call) and `grid_valid`. All three are arguments of the `operation`. `grid_valid` is its logical result, which the driver writes into the state after the call as `1` when the call returned true and `0` when it returned false; it starts at `0`, so a result the `operation` never assigned captures `0`. An output array of a rejected input is not defined by the Controlled Spec §4, so the flag is the only state that distinguishes a rejection from an executed mapping.

- `grid_valid`: the captured flag. It is `== 1` on the cases of a `pass` test and `== 0` on the cases of an `xfail` test.
- `x_wrap_residual`: over every interior row `j`, the largest of `|U_out(k,j) - U_in(k+nx,j)|` and `|U_out(ng+nx+k,j) - U_in(ng+k,j)|` for `k` in `1..ng`, and `|U_out(i,j) - U_in(i,j)|` for the interior columns `i`, with each case's own extents.
- `y_wrap_residual`: over every column `i` in `1..nx_total`, the largest of `|U_out(i,k) - U_in(p(i),k+ny)|` and `|U_out(i,ng+ny+k) - U_in(p(i),ng+k)|` for `k` in `1..ng`, with `p` the column map of `l0_periodic_y_wrap_pass` and each case's own extents.
- `pair_residual`: on a transformed case, `norm2(U_out - T(U_out_base)) / norm2(U_out_base)` over every element of the padded field, where `U_out_base` is the `U_out` of `l0_ng1` captured after its call and `T` the case's transformation: the transpose for `l0_ng1_axis`, the reversal along the first array axis for `l0_ng1_xmir`, along the second for `l0_ng1_ymir`. The host reads the base through `at('l0_ng1')` and applies `transpose` / `flip` (`docs/workflow/phases/phase_01_compile.md`).
- `cross_target_state_agreement`: this target's run compared with every other target's certified variant of this node, case by case (`docs/TESTS.md` §Cross-target judgment): for one output `q` captured after the run and the same output `q_ref` of the same case in the other variant, `max|q - q_ref| / max(max|q|, max|q_ref|, 1)` over every element. The host evaluates it from the captured state, against each other variant separately; on a target with no other certified variant it holds vacuously. It is outside the `quantities` line.

## 6. Test definitions
- `test_id`: `l0_periodic_x_wrap_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_boundary_2d_periodic_copy__apply`
  - `expected_outcome`: `pass`
  - `quantities`: `grid_valid`, `x_wrap_residual`
  - `judgment`: on `l0_ng1`, its three transformed cases, `l0_ng2`, `l0_min_grid` and `l0_large_grid`, `grid_valid == 1` and, for every interior row `j` and every `k` in `1..ng`, the `x` ghost cells satisfy `U_out(k,j) = U_in(k+nx,j)` and `U_out(ng+nx+k,j) = U_in(ng+k,j)` exactly, and the interior cells satisfy `U_out(i,j) = U_in(i,j)` exactly (`x_wrap_residual == 0`). The cross-target judgment is applied: `cross_target_state_agreement` over `U_out` is `<= 1e-12`.
- `test_id`: `l0_periodic_y_wrap_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_boundary_2d_periodic_copy__apply`
  - `expected_outcome`: `pass`
  - `quantities`: `grid_valid`, `y_wrap_residual`
  - `judgment`: on `l0_ng1`, its three transformed cases, `l0_ng2`, `l0_min_grid` and `l0_large_grid`, `grid_valid == 1` and, for every column `i` in `1..nx_total` (the `x` ghost columns included) and every `k` in `1..ng`, the `y` ghost cells satisfy `U_out(i,k) = U_in(p(i),k+ny)` and `U_out(i,ng+ny+k) = U_in(p(i),ng+k)` exactly (`y_wrap_residual == 0`), where `p(i) = i` for an interior column, `p(k) = k+nx` and `p(ng+nx+k) = ng+k` for the `x` ghost columns: the `y` mapping is applied after the `x` mapping (Controlled Spec §3), so a corner ghost cell holds the periodic image of the interior along both axes and no cell of `U_out` holds the sentinel. The cross-target judgment is applied: `cross_target_state_agreement` over `U_out` is `<= 1e-12`.
- `test_id`: `l0_symmetry_pairs_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_boundary_2d_periodic_copy__apply`
  - `expected_outcome`: `pass`
  - `quantities`: `grid_valid`, `pair_residual`
  - `judgment`: `grid_valid == 1` on `l0_ng1` and its three transformed cases, and on `l0_ng1_axis`, `l0_ng1_xmir` and `l0_ng1_ymir` `pair_residual` against `l0_ng1` is `<= 1.0e-12`. The mapping copies values, so a conforming kernel gives exactly `0` (measured on the three pairs with an independent reference of §3); the bound is the suite's cross-target bound, not a measured round-off. Measured with that reference: a kernel whose `x` wrap loops over the rows `j = ng+1..ng+nx` (the `x` extent bounding the row loop) passes both judgments above on `l0_ng1`, where `nx > ny` makes the loop reach the top ghost row that the `y` wrap then overwrites, and gives `pair_residual = 9.6` on `l0_ng1_axis`, where `nx < ny` leaves the last interior row's two `x` ghost cells, and the two bottom corner cells the `y` wrap copies from them, holding the sentinel (the `x`-wrap judgment on `l0_ng1_axis` fails as well); a kernel whose right `x` ghost column reads one column too far (`U_in(ng+k+1,j)`) gives `0.098` on the axis pair and `0.14` on the `x`-mirror pair, and one whose top `y` ghost row reads one row too far gives `0.10` on the axis pair and `0.016` on the `y`-mirror pair, both also failing the wrap judgments on `l0_ng1`. The cross-target judgment is applied: `cross_target_state_agreement` over `U_out` is `<= 1e-12`.
- `test_id`: `l0_invalid_ny_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_boundary_2d_periodic_copy__apply`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `ny<2`
  - `quantities`: `grid_valid`
  - `judgment`: on `l0_invalid_ny`, the `operation` refuses the input: `grid_valid == 0`, in the failure direction. The cross-target judgment is not applied.
- `test_id`: `l0_invalid_nx_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_boundary_2d_periodic_copy__apply`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `nx<2`
  - `quantities`: `grid_valid`
  - `judgment`: on `l0_invalid_nx`, the `operation` refuses the input: `grid_valid == 0`, in the failure direction. The cross-target judgment is not applied.
- `test_id`: `l0_invalid_ng_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_boundary_2d_periodic_copy__apply`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `ng<1`
  - `quantities`: `grid_valid`
  - `judgment`: on `l0_invalid_ng`, the `operation` refuses the input: `grid_valid == 0`, in the failure direction. The cross-target judgment is not applied.

## 7. Pass/fail aggregation rules
- `per_test.pass_rule`: `pass` when every predicate of the test holds.
- `per_test.xfail_rule`: `xfail` when `xfail_condition` is true and every predicate of the test holds.
- `suite.pass_rule`: `pass` when all `test_id` are `pass` or `xfail`.

## 8. Traceability
- Record `test_profile_id` and `test_profile_version` in `trial_meta.json`.
