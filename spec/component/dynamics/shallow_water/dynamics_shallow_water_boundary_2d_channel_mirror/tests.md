# Tests: 2D channel-boundary mapping (L0)

## 0. Meta information
- `test_profile_id`: `dynamics_shallow_water_boundary_2d_channel_mirror_l0`
- `test_profile_version`: `0.1.0`
- `status`: `draft`
- `spec_ref.spec_kind`: `component`
- `spec_ref.spec_id`: `dynamics_shallow_water_boundary_2d_channel_mirror`
- `spec_ref.spec_version`: `0.1.0`
- `spec_ref.controlled_spec_path`: `spec/component/dynamics/shallow_water/dynamics_shallow_water_boundary_2d_channel_mirror/controlled_spec.md`

## 1. Test purpose
This suite verifies the published `operation` `dynamics_shallow_water_boundary_2d_channel_mirror__apply` at `L0`: the `x`-direction periodic ghost mapping, the `y`-direction even mirror (`odd_at_wall=false`), the `y`-direction odd mirror (`odd_at_wall=true`), the corner ghost cells, and the input guard for an invalid grid size (`ny<2`) and for a ghost width the interior cannot supply (`ng>ny`, `ng>nx`).

## 2. Input-defaulting rules
- The normal cases use `nx=4`, `ny=3`, `ng=1`, `nx_total=6`, `ny_total=5`. The interior of `U_in` is filled with pairwise-distinct values, `U_in(i,j) = 10*i + j` for the interior indices, so that every ghost cell identifies the interior cell it was mapped from. The ghost cells of `U_in` are filled with a sentinel value (`-999`) that no interior cell holds, so that a ghost cell of `U_in` read by mistake is visible in the output.
- The even-mirror case uses `odd_at_wall=false`; the odd-mirror case and the corner case use `odd_at_wall=true`. The `x`-wrap case uses `odd_at_wall=false`.
- The abnormal cases use `odd_at_wall=false` and the sentinel-filled ghost cells of the normal cases. The invalid-`ny` case uses `ny=1` (`ny<2`) with `ny_total=3`, `nx=4`, `ng=1`, `nx_total=6`. The invalid-`ng`-over-`ny` case uses `ng=3`, `nx=4`, `ny=2`, `nx_total=10`, `ny_total=8` (`ng>ny`, `ng<=nx`). The invalid-`ng`-over-`nx` case uses `ng=3`, `nx=2`, `ny=4`, `nx_total=8`, `ny_total=10` (`ng>nx`, `ng<=ny`).

## 3. Execution-control rules
`N/A`: this `component` applies a boundary mapping to a provided field and defines no time-stepping or iteration. Execution control is the responsibility of the `problem` runner.

## 4. Case-expansion rules
`N/A`: the `L0` suite uses fixed inputs and defines no `case` sweep. Case expansion is defined at the `problem` level.

## 5. Diagnostics contract
- Require outputting `checks.x_wrap`, `checks.y_even_mirror`, `checks.y_odd_mirror`, `checks.corner`, and `checks.input_guard` in `diagnostics.json`.

## 6. Test definitions
- `test_id`: `l0_periodic_x_wrap_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_boundary_2d_channel_mirror__apply`
  - `expected_outcome`: `pass`
  - `judgment`: for every interior row `j`, the `x` ghost cells satisfy `U_out(1,j) = U_in(1+nx,j)` and `U_out(ng+nx+1,j) = U_in(ng+1,j)` exactly (`ng=1`), and the interior cells satisfy `U_out(i,j) = U_in(i,j)` exactly.
- `test_id`: `l0_wall_even_mirror_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_boundary_2d_channel_mirror__apply`
  - `expected_outcome`: `pass`
  - `judgment`: with `odd_at_wall=false`, for every interior column `i` the `y` ghost cells satisfy `U_out(i,ng) = U_in(i,ng+1)` and `U_out(i,ng+ny+1) = U_in(i,ng+ny)` exactly (`ng=1`; the ghost value equals the adjacent interior value of the input), and the interior cells satisfy `U_out(i,j) = U_in(i,j)` exactly.
- `test_id`: `l0_wall_odd_mirror_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_boundary_2d_channel_mirror__apply`
  - `expected_outcome`: `pass`
  - `judgment`: with `odd_at_wall=true`, for every interior column `i` the `y` ghost cells satisfy `U_out(i,ng) = -U_in(i,ng+1)` and `U_out(i,ng+ny+1) = -U_in(i,ng+ny)` exactly (`ng=1`; the ghost value equals the negated adjacent interior value of the input), and the interior cells satisfy `U_out(i,j) = U_in(i,j)` exactly (the sign reversal applies to the ghost cells only).
- `test_id`: `l0_corner_defined_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_boundary_2d_channel_mirror__apply`
  - `expected_outcome`: `pass`
  - `judgment`: with `odd_at_wall=true` and `ng=1`, the four corner ghost cells are the `y` mirror of the `x`-periodic images: `U_out(1,1) = -U_in(1+nx,2)`, `U_out(nx_total,1) = -U_in(2,2)`, `U_out(1,ny_total) = -U_in(1+nx,1+ny)`, and `U_out(nx_total,ny_total) = -U_in(2,1+ny)` exactly; no corner cell holds the `U_in` sentinel value.
- `test_id`: `l0_invalid_ny_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_boundary_2d_channel_mirror__apply`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `ny<2`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`
- `test_id`: `l0_invalid_ng_over_ny_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_boundary_2d_channel_mirror__apply`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `ng>ny`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`
- `test_id`: `l0_invalid_ng_over_nx_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_boundary_2d_channel_mirror__apply`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `ng>nx`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`

## 7. Pass/fail aggregation rules
- `per_test.pass_rule`: `pass` when the judgment expression is satisfied.
- `per_test.xfail_rule`: `xfail` when `xfail_condition` is true and `pass_when` is satisfied.
- `suite.pass_rule`: `pass` when all `test_id` are `pass` or `xfail`.

## 8. Traceability
- Record `test_profile_id` and `test_profile_version` in `trial_meta.json`.
