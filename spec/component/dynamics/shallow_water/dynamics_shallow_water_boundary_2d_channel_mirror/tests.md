# Tests: 2D channel-boundary mapping (L0)

## 0. Meta information
- `test_profile_id`: `dynamics_shallow_water_boundary_2d_channel_mirror_l0`
- `test_profile_version`: `0.5.0`
- `status`: `draft`
- `spec_ref.spec_kind`: `component`
- `spec_ref.spec_id`: `dynamics_shallow_water_boundary_2d_channel_mirror`
- `spec_ref.spec_version`: `0.2.0`
- `spec_ref.controlled_spec_path`: `spec/component/dynamics/shallow_water/dynamics_shallow_water_boundary_2d_channel_mirror/controlled_spec.md`

## 1. Test purpose
This suite verifies the published `operation` `dynamics_shallow_water_boundary_2d_channel_mirror__apply` at `L0`: with the walls on the second axis (`wall_axis=2`), the `x`-direction periodic ghost mapping, the `y`-direction even mirror (`odd_at_wall=0`), the `y`-direction odd mirror (`odd_at_wall=1`), the corner ghost cells — each at the two ghost widths `ng=1` and `ng=2`, so that the general-`ng` index formulas of the Controlled Spec §3 are observed and not only their `ng=1` instance; the walls on the first axis (`wall_axis=1`) through the axis-exchange pair; the two mirror pairs; and the input guard for each clause of the Controlled Spec §4: an invalid interior extent (`ny<2`, `nx<2`), a ghost width below one (`ng<1`), a ghost width the interior cannot supply (`ng>ny`, `ng>nx`), and a wall axis that is neither `1` nor `2` (below and above).

The mapping admits four transformations (`docs/TESTS.md` §Symmetry pairs); the suite carries three of them as pairs and states why it does not carry the fourth, the translation. The **axis exchange**: every normal case at `wall_axis=2` has a transposed copy at `wall_axis=1`, whose `U_out` is the transpose of the base's (Controlled Spec §3, the last paragraph). The `wall_axis=1` mapping is judged by that pair alone (its guard by the abnormal `_axis` copies of §2): the base's `U_out` is pinned cell by cell by the four judgments of §6, so a `wall_axis=1` output equal to its transpose is the mapping of §3 in that orientation. The **mirrors**: the padded field reversed along the first array axis, and along the second, maps to the reversed output. The mapping is linear in the field, so the sign reversal of the mirrored velocity component commutes with it and the mirror pairs are judged without it. The **translation** along the periodic axis is admitted by the mapping but is not a transformation of the padded array: a cyclic shift of the padded extent is not the shift of the periodic interior, whose period is `nx`, not `nx_total`. The suite carries no translation pair; the `problem` that composes this mapping carries the translation of the whole state.

## 2. Input-defaulting rules
- The base normal cases use `wall_axis=2` and `nx=4`, `ny=3` at two ghost widths: `ng=1` (`nx_total=6`, `ny_total=5`) and `ng=2` (`nx_total=8`, `ny_total=7`), and two further normal cases at the inclusive edge of the Controlled Spec §4 bounds, `ng=ny` (`nx=4`, `ny=2`, `ng=2`, `nx_total=8`, `ny_total=6`) and `ng=nx` (`nx=2`, `ny=3`, `ng=2`, `nx_total=6`, `ny_total=7`), which are valid grids and are judged like the other normal cases. Each grid is run with `odd_at_wall=0` and with `odd_at_wall=1`, so the base normal cases are eight: `l0_ng1_even`, `l0_ng1_odd`, `l0_ng2_even`, `l0_ng2_odd`, `l0_ngeqny_even`, `l0_ngeqny_odd`, `l0_ngeqnx_even`, `l0_ngeqnx_odd`. The interior of `U_in` is filled with pairwise-distinct values, `U_in(i,j) = 10*i + j` for the interior indices, so that every ghost cell identifies the interior cell it was mapped from. The ghost cells of `U_in` are filled with a sentinel value (`-999`) that no interior cell holds, so that a ghost cell of `U_in` read by mistake is visible in the output. Every judgment of §6 that targets the base normal cases is evaluated on each of them its `odd_at_wall` admits, with `k` and `m` ranging over `1..ng`.
- Every case declares `odd_at_wall` as the number `1` or `0`, which the driver passes to the `operation`'s logical argument as true or false, and the numeric sign `wall_sign` (`-1` when `odd_at_wall` is `1`, `+1` when it is `0`), which a host-evaluated judgment reads where a judgment depends on the sign.
- The even-mirror judgment uses the base cases with `odd_at_wall=0`; the odd-mirror judgment uses those with `odd_at_wall=1`; the `x`-wrap judgment and the corner judgment use every base normal case (the periodic mapping does not depend on `odd_at_wall`).
- **Axis-exchange cases.** Each base normal case `<base>` has a transposed copy `<base>_axis` at `wall_axis=1`, with `nx` and `ny` exchanged, `nx_total` and `ny_total` exchanged, the same `ng` and `odd_at_wall`, and `U_in` the transpose of the base's padded `U_in` (interior `U_in(i,j) = 10*j + i`, ghost cells the sentinel). The eight are `l0_ng1_even_axis` … `l0_ngeqnx_odd_axis`; each sorts after its base. The transposed `ng=ny` case is a `ng=nx` edge at `wall_axis=1` and the transposed `ng=nx` case a `ng=ny` edge, so each edge of §4 is reached in both orientations.
- **Mirror cases.** `l0_ng2_odd_xmir` and `l0_ng2_odd_ymir` are `l0_ng2_odd` (`wall_axis=2`, `odd_at_wall=1`) with the padded `U_in` reversed along the first array axis (`U_in'(i,j) = U_in(nx_total+1-i, j)`, the reversal along the periodic axis) and along the second (`U_in'(i,j) = U_in(i, ny_total+1-j)`, the reversal across the walls) respectively; the sentinel ghost cells stay ghost cells under either reversal. Each sorts after its base.
- The abnormal cases use `wall_axis=2` (but for the two wall-axis cases and the five `_axis` copies below), `odd_at_wall=0` and the sentinel-filled ghost cells of the normal cases; each violates exactly one clause of the Controlled Spec §4. The invalid-`ny` case uses `ny=1` with `nx=4`, `ng=1`, `nx_total=6`, `ny_total=3`. The invalid-`nx` case uses `nx=1` with `ny=4`, `ng=1`, `nx_total=3`, `ny_total=6`. The invalid-`ng`-below-one case uses `ng=0` with `nx=4`, `ny=3`, `nx_total=4`, `ny_total=3`. The invalid-`ng`-over-`ny` case uses `ng=3`, `nx=4`, `ny=2`, `nx_total=10`, `ny_total=8` (`ng>ny`, `ng<=nx`). The invalid-`ng`-over-`nx` case uses `ng=3`, `nx=2`, `ny=4`, `nx_total=8`, `ny_total=10` (`ng>nx`, `ng<=ny`). The two invalid-wall-axis cases use the grid of `l0_ng1_even` with `wall_axis=0` and `wall_axis=3`, so that a guard written as `wall_axis>2` accepts the first and one written as `wall_axis<1` accepts the second. The abnormal cases are `l0_invalid_ny`, `l0_invalid_nx`, `l0_invalid_ng_zero`, `l0_invalid_ng_over_ny`, `l0_invalid_ng_over_nx`, `l0_invalid_wall_axis_0`, `l0_invalid_wall_axis_3`. Each of the five grid cases has a transposed copy `<case>_axis` at `wall_axis=1` (`nx` and `ny` exchanged, `nx_total` and `ny_total` exchanged, the same `ng`), so that a guard evaluated in one orientation's branch only is visible: §4 rejects each grid in either orientation. The copy violates the transposed clause — `l0_invalid_ny_axis` has `nx=1`, `l0_invalid_nx_axis` has `ny=1`, `l0_invalid_ng_over_ny_axis` has `ng>nx`, `l0_invalid_ng_over_nx_axis` has `ng>ny`, `l0_invalid_ng_zero_axis` has `ng=0` — and is judged by the test of the clause it violates.
- Before the call the state holds `U_in` as above and `U_out` holds the literal `-999` at every element, its pre-call content: `U_out` is an operation-only output, so an element the `operation` does not write is captured as `-999` and fails every judgment of §6 over `U_out`.

## 3. Execution-control rules
`N/A`: this `component` applies a boundary mapping to a provided field and defines no time-stepping or iteration. Execution control belongs to the `problem` that composes this `operation`.

## 4. Case-expansion rules
`N/A`: the `L0` suite uses fixed inputs and defines no `case` sweep. Case expansion is defined at the `problem` level.

## 5. Judged quantities
A test is judged by the host's predicates over the captures alone (`docs/TESTS.md` requirement 9). Each quantity below is named on the `quantities` line of the tests that judge it, and is evaluated from the state captured after the run in each of the test's target cases.

The host-rendered driver calls the `operation` once per case on the case's state, with `nx`, `ny`, `ng`, `nx_total`, `ny_total`, `wall_axis`, `odd_at_wall` from the case's inputs and `U_in`, `U_out`, `grid_valid` bound to the states of those names. The state captured before and after that call consists of `U_in` (the padded input of §2), `U_out` (`nx_total` × `ny_total`, the pre-call content `-999` of §2 before the call) and `grid_valid`. All three are arguments of the `operation`. `grid_valid` is its logical result, which the driver writes into the state after the call as `1` when the call returned true and `0` when it returned false; it starts at `0`, so a result the `operation` never assigned captures `0`. An output array of a rejected input is not defined by the Controlled Spec §4, so the flag is the only state that distinguishes a rejection from an executed mapping.

- `grid_valid`: the captured flag. It is `== 1` on the cases of a `pass` test and `== 0` on the cases of an `xfail` test.
- `x_wrap`: over the interior rows `j`, the largest `|U_out(i,j) - W(i,j)|`, where `W` is the `x`-periodic image of the case's `U_in`: `W(k,j) = U_in(k+nx,j)` and `W(ng+nx+k,j) = U_in(ng+k,j)` for `k` in `1..ng`, and `W(i,j) = U_in(i,j)` at the interior columns.
- `y_even_mirror`: over the interior columns `i`, the largest of `|U_out(i,ng-k+1) - U_in(i,ng+k)|` and `|U_out(i,ng+ny+k) - U_in(i,ng+ny-k+1)|` for `k` in `1..ng`, and of `|U_out(i,j) - U_in(i,j)|` at the interior rows, with `U_in` that of §2.
- `y_odd_mirror`: over the interior columns `i`, the largest of `|U_out(i,ng-k+1) + U_in(i,ng+k)|` and `|U_out(i,ng+ny+k) + U_in(i,ng+ny-k+1)|` for `k` in `1..ng`, and of `|U_out(i,j) - U_in(i,j)|` at the interior rows, with `U_in` that of §2.
- `corner`: over the corner ghost cells, the largest `|U_out - s*C|`, where `s` is the case's `wall_sign` (§2) and `C` the `y` mirror of the `x`-periodic image of `U_in` that §6 states for each of the four corners.
- `axis_exchange_pair_residual`: on an `_axis` case, `norm2(U_out - T(U_out_base)) / norm2(U_out_base)` over every element of the padded field, where `U_out_base` is the base case's `U_out` captured after its call and `T` the transpose. The host reads `U_out_base` through `at('<base>')` and applies `T` with `transpose` (`docs/workflow/phases/phase_01_compile.md`).
- `mirror_pair_residual`: on `l0_ng2_odd_xmir` and `l0_ng2_odd_ymir`, the same ratio against `l0_ng2_odd`, with `T` the reversal along the first array axis for `_xmir` and along the second for `_ymir`. The host reads the base through `at('l0_ng2_odd')` and applies `T` with `flip` (`docs/workflow/phases/phase_01_compile.md`).
- `cross_target_state_agreement`: this target's run compared with every other target's certified variant of this node, case by case (`docs/TESTS.md` §Cross-target judgment): for one output `q` captured after the run and the same output `q_ref` of the same case in the other variant, `max|q - q_ref| / max(max|q|, max|q_ref|, 1)` over every element. The host evaluates it from the captured state, against each other variant separately; on a target with no other certified variant it holds vacuously. It is outside the `quantities` line.

## 6. Test definitions
- `test_id`: `l0_periodic_x_wrap_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_boundary_2d_channel_mirror__apply`
  - `expected_outcome`: `pass`
  - `quantities`: `grid_valid`, `x_wrap`
  - `judgment`: on every base normal case (`wall_axis=2`), `grid_valid == 1` and, for every interior row `j` and every `k` in `1..ng`, the `x` ghost cells satisfy `U_out(k,j) = U_in(k+nx,j)` and `U_out(ng+nx+k,j) = U_in(ng+k,j)` exactly, and the interior cells satisfy `U_out(i,j) = U_in(i,j)` exactly (`x_wrap == 0`). The cross-target judgment is applied: `cross_target_state_agreement` over `U_out` is `<= 1e-12`.
- `test_id`: `l0_wall_even_mirror_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_boundary_2d_channel_mirror__apply`
  - `expected_outcome`: `pass`
  - `quantities`: `grid_valid`, `y_even_mirror`
  - `judgment`: with `odd_at_wall=0`, on every base normal case (`wall_axis=2`), `grid_valid == 1` and, for every interior column `i` and every `k` in `1..ng`, the `y` ghost cells satisfy `U_out(i,ng-k+1) = U_in(i,ng+k)` and `U_out(i,ng+ny+k) = U_in(i,ng+ny-k+1)` exactly (the ghost value equals the mirrored interior value of the input), and the interior cells satisfy `U_out(i,j) = U_in(i,j)` exactly (`y_even_mirror == 0`). The cross-target judgment is applied: `cross_target_state_agreement` over `U_out` is `<= 1e-12`.
- `test_id`: `l0_wall_odd_mirror_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_boundary_2d_channel_mirror__apply`
  - `expected_outcome`: `pass`
  - `quantities`: `grid_valid`, `y_odd_mirror`
  - `judgment`: with `odd_at_wall=1`, on every base normal case (`wall_axis=2`), `grid_valid == 1` and, for every interior column `i` and every `k` in `1..ng`, the `y` ghost cells satisfy `U_out(i,ng-k+1) = -U_in(i,ng+k)` and `U_out(i,ng+ny+k) = -U_in(i,ng+ny-k+1)` exactly (the ghost value equals the negated mirrored interior value of the input), and the interior cells satisfy `U_out(i,j) = U_in(i,j)` exactly (the sign reversal applies to the ghost cells only; `y_odd_mirror == 0`). The cross-target judgment is applied: `cross_target_state_agreement` over `U_out` is `<= 1e-12`.
- `test_id`: `l0_corner_defined_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_boundary_2d_channel_mirror__apply`
  - `expected_outcome`: `pass`
  - `quantities`: `corner`, `grid_valid`
  - `judgment`: on every base normal case (`wall_axis=2`), with $s$ the case's `wall_sign` (§2), `grid_valid == 1` and the corner ghost cells are the `y` mirror of the `x`-periodic images: for every `k` and `m` in `1..ng`, `U_out(k,ng-m+1) = s*U_in(k+nx,ng+m)`, `U_out(ng+nx+k,ng-m+1) = s*U_in(ng+k,ng+m)`, `U_out(k,ng+ny+m) = s*U_in(k+nx,ng+ny-m+1)`, and `U_out(ng+nx+k,ng+ny+m) = s*U_in(ng+k,ng+ny-m+1)` exactly (at `ng=1` and `odd_at_wall=1` these are `U_out(1,1) = -U_in(1+nx,2)`, `U_out(nx_total,1) = -U_in(2,2)`, `U_out(1,ny_total) = -U_in(1+nx,1+ny)`, and `U_out(nx_total,ny_total) = -U_in(2,1+ny)`); no corner cell holds the `U_in` sentinel value (`corner == 0`). The cross-target judgment is applied: `cross_target_state_agreement` over `U_out` is `<= 1e-12`.
- `test_id`: `l0_axis_exchange_pair_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_boundary_2d_channel_mirror__apply`
  - `expected_outcome`: `pass`
  - `quantities`: `axis_exchange_pair_residual`, `grid_valid`
  - `judgment`: on each of the eight `_axis` cases (`wall_axis=1`), `axis_exchange_pair_residual` against its base is `<= 1.0e-12`, and `grid_valid == 1` on each of the eight and on its base. The mapping copies values and reverses signs, so a conforming kernel gives exactly `0`; the bound is the suite's cross-target bound, not a measured round-off. Measured on the eight pairs with an independent reference of §3: a kernel that ignores `wall_axis` and maps every field as at `wall_axis=2` gives `0.21` to `1.43`; one whose `wall_axis=1` mirror reads the ghost ring of `U_in` instead of the result of step 1 (corners holding the sentinel) gives `9.5` to `16.3`; one whose `wall_axis=1` wrap reads one line short (`U_in(i,j+ny-1)`) gives `0.10` to `10.7`. The cross-target judgment is applied: `cross_target_state_agreement` over `U_out` is `<= 1e-12`.
- `test_id`: `l0_mirror_pairs_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_boundary_2d_channel_mirror__apply`
  - `expected_outcome`: `pass`
  - `quantities`: `grid_valid`, `mirror_pair_residual`
  - `judgment`: on `l0_ng2_odd_xmir` and `l0_ng2_odd_ymir`, `mirror_pair_residual` against `l0_ng2_odd` is `<= 1.0e-12`, and `grid_valid == 1` on the two and on `l0_ng2_odd`; a conforming kernel gives exactly `0`. The base output is pinned cell by cell by the four judgments above; the two pairs carry that to the reversed inputs, whose left ghost columns wrap from what was the right interior and whose bottom ghost rows mirror what was the top wall. The cross-target judgment is applied: `cross_target_state_agreement` over `U_out` is `<= 1e-12`.
- `test_id`: `l0_invalid_ny_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_boundary_2d_channel_mirror__apply`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `ny<2`
  - `quantities`: `grid_valid`
  - `judgment`: on `l0_invalid_ny` at `wall_axis=2` and on `l0_invalid_nx_axis` at `wall_axis=1`, the `operation` refuses the input: `grid_valid == 0`, in the failure direction. The cross-target judgment is not applied.
- `test_id`: `l0_invalid_nx_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_boundary_2d_channel_mirror__apply`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `nx<2`
  - `quantities`: `grid_valid`
  - `judgment`: on `l0_invalid_nx` at `wall_axis=2` and on `l0_invalid_ny_axis` at `wall_axis=1`, the `operation` refuses the input: `grid_valid == 0`, in the failure direction. The cross-target judgment is not applied.
- `test_id`: `l0_invalid_ng_zero_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_boundary_2d_channel_mirror__apply`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `ng<1`
  - `quantities`: `grid_valid`
  - `judgment`: on `l0_invalid_ng_zero` at `wall_axis=2` and on `l0_invalid_ng_zero_axis` at `wall_axis=1`, the `operation` refuses the input: `grid_valid == 0`, in the failure direction. The cross-target judgment is not applied.
- `test_id`: `l0_invalid_ng_over_ny_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_boundary_2d_channel_mirror__apply`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `ng>ny`
  - `quantities`: `grid_valid`
  - `judgment`: on `l0_invalid_ng_over_ny` at `wall_axis=2` and on `l0_invalid_ng_over_nx_axis` at `wall_axis=1`, the `operation` refuses the input: `grid_valid == 0`, in the failure direction. The cross-target judgment is not applied.
- `test_id`: `l0_invalid_ng_over_nx_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_boundary_2d_channel_mirror__apply`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `ng>nx`
  - `quantities`: `grid_valid`
  - `judgment`: on `l0_invalid_ng_over_nx` at `wall_axis=2` and on `l0_invalid_ng_over_ny_axis` at `wall_axis=1`, the `operation` refuses the input: `grid_valid == 0`, in the failure direction. The cross-target judgment is not applied.
- `test_id`: `l0_invalid_wall_axis_zero_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_boundary_2d_channel_mirror__apply`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `wall_axis<1`
  - `quantities`: `grid_valid`
  - `judgment`: on `l0_invalid_wall_axis_0`, the `operation` refuses the input: `grid_valid == 0`, in the failure direction. The cross-target judgment is not applied.
- `test_id`: `l0_invalid_wall_axis_three_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_boundary_2d_channel_mirror__apply`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `wall_axis>2`
  - `quantities`: `grid_valid`
  - `judgment`: on `l0_invalid_wall_axis_3`, the `operation` refuses the input: `grid_valid == 0`, in the failure direction. The cross-target judgment is not applied.

## 7. Pass/fail aggregation rules
- `per_test.pass_rule`: `pass` when every predicate of the test holds.
- `per_test.xfail_rule`: `xfail` when `xfail_condition` is true and every predicate of the test holds.
- `suite.pass_rule`: `pass` when all `test_id` are `pass` or `xfail`.

## 8. Traceability
- Record `test_profile_id` and `test_profile_version` in `trial_meta.json`.
