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
This suite verifies the published `operation` `dynamics_shallow_water_boundary_2d_channel_mirror__apply` at `L0`: the `x`-direction periodic ghost mapping, the `y`-direction even mirror (`odd_at_wall=false`), the `y`-direction odd mirror (`odd_at_wall=true`), the corner ghost cells — each at the two ghost widths `ng=1` and `ng=2`, so that the general-`ng` index formulas of the Controlled Spec §3 are observed and not only their `ng=1` instance — and the input guard for each clause of the Controlled Spec §4: an invalid interior extent (`ny<2`, `nx<2`), a ghost width below one (`ng<1`), and a ghost width the interior cannot supply (`ng>ny`, `ng>nx`).

## 2. Input-defaulting rules
- The normal cases use `nx=4`, `ny=3` at two ghost widths: `ng=1` (`nx_total=6`, `ny_total=5`) and `ng=2` (`nx_total=8`, `ny_total=7`). The interior of `U_in` is filled with pairwise-distinct values, `U_in(i,j) = 10*i + j` for the interior indices, so that every ghost cell identifies the interior cell it was mapped from. The ghost cells of `U_in` are filled with a sentinel value (`-999`) that no interior cell holds, so that a ghost cell of `U_in` read by mistake is visible in the output. Every judgment of §6 that targets a normal case is evaluated at both ghost widths, with `k` and `m` ranging over `1..ng`.
- The even-mirror judgment uses `odd_at_wall=false`; the odd-mirror judgment and the corner judgment use `odd_at_wall=true`; the `x`-wrap judgment uses `odd_at_wall=false`.
- The abnormal cases use `odd_at_wall=false` and the sentinel-filled ghost cells of the normal cases; each violates exactly one clause of the Controlled Spec §4. The invalid-`ny` case uses `ny=1` with `nx=4`, `ng=1`, `nx_total=6`, `ny_total=3`. The invalid-`nx` case uses `nx=1` with `ny=4`, `ng=1`, `nx_total=3`, `ny_total=6`. The invalid-`ng`-below-one case uses `ng=0` with `nx=4`, `ny=3`, `nx_total=4`, `ny_total=3`. The invalid-`ng`-over-`ny` case uses `ng=3`, `nx=4`, `ny=2`, `nx_total=10`, `ny_total=8` (`ng>ny`, `ng<=nx`). The invalid-`ng`-over-`nx` case uses `ng=3`, `nx=2`, `ny=4`, `nx_total=8`, `ny_total=10` (`ng>nx`, `ng<=ny`).

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
  - `judgment`: at each ghost width, for every interior row `j` and every `k` in `1..ng`, the `x` ghost cells satisfy `U_out(k,j) = U_in(k+nx,j)` and `U_out(ng+nx+k,j) = U_in(ng+k,j)` exactly, and the interior cells satisfy `U_out(i,j) = U_in(i,j)` exactly.
- `test_id`: `l0_wall_even_mirror_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_boundary_2d_channel_mirror__apply`
  - `expected_outcome`: `pass`
  - `judgment`: with `odd_at_wall=false`, at each ghost width, for every interior column `i` and every `k` in `1..ng`, the `y` ghost cells satisfy `U_out(i,ng-k+1) = U_in(i,ng+k)` and `U_out(i,ng+ny+k) = U_in(i,ng+ny-k+1)` exactly (the ghost value equals the mirrored interior value of the input), and the interior cells satisfy `U_out(i,j) = U_in(i,j)` exactly.
- `test_id`: `l0_wall_odd_mirror_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_boundary_2d_channel_mirror__apply`
  - `expected_outcome`: `pass`
  - `judgment`: with `odd_at_wall=true`, at each ghost width, for every interior column `i` and every `k` in `1..ng`, the `y` ghost cells satisfy `U_out(i,ng-k+1) = -U_in(i,ng+k)` and `U_out(i,ng+ny+k) = -U_in(i,ng+ny-k+1)` exactly (the ghost value equals the negated mirrored interior value of the input), and the interior cells satisfy `U_out(i,j) = U_in(i,j)` exactly (the sign reversal applies to the ghost cells only).
- `test_id`: `l0_corner_defined_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_boundary_2d_channel_mirror__apply`
  - `expected_outcome`: `pass`
  - `judgment`: with `odd_at_wall=true`, at each ghost width, the corner ghost cells are the `y` mirror of the `x`-periodic images: for every `k` and `m` in `1..ng`, `U_out(k,ng-m+1) = -U_in(k+nx,ng+m)`, `U_out(ng+nx+k,ng-m+1) = -U_in(ng+k,ng+m)`, `U_out(k,ng+ny+m) = -U_in(k+nx,ng+ny-m+1)`, and `U_out(ng+nx+k,ng+ny+m) = -U_in(ng+k,ng+ny-m+1)` exactly (at `ng=1` these are `U_out(1,1) = -U_in(1+nx,2)`, `U_out(nx_total,1) = -U_in(2,2)`, `U_out(1,ny_total) = -U_in(1+nx,1+ny)`, and `U_out(nx_total,ny_total) = -U_in(2,1+ny)`); no corner cell holds the `U_in` sentinel value.
- `test_id`: `l0_invalid_ny_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_boundary_2d_channel_mirror__apply`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `ny<2`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`
- `test_id`: `l0_invalid_nx_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_boundary_2d_channel_mirror__apply`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `nx<2`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`
- `test_id`: `l0_invalid_ng_zero_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_boundary_2d_channel_mirror__apply`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `ng<1`
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
