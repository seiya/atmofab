# Tests: 2D shallow water Coriolis source (L0)

## 0. Meta information
- `test_profile_id`: `dynamics_shallow_water_source_2d_coriolis_l0`
- `test_profile_version`: `0.1.0`
- `status`: `draft`
- `spec_ref.spec_kind`: `component`
- `spec_ref.spec_id`: `dynamics_shallow_water_source_2d_coriolis`
- `spec_ref.spec_version`: `0.1.0`
- `spec_ref.controlled_spec_path`: `spec/component/dynamics/shallow_water/dynamics_shallow_water_source_2d_coriolis/controlled_spec.md`

## 1. Test purpose
This suite verifies the published `operation` `dynamics_shallow_water_source_2d_coriolis__apply` at `L0`: the zero source for `f=0`, the pointwise formula with a row-dependent `f`, the discrete no-work property, and the input guard for an invalid component count (`ncomp/=3`, below and above three). The extent clauses `nx<1` / `ny<1` of the Controlled Spec §4 have no case: a state of zero extent has no snapshot the judgment could read, so those two clauses are stated by the Controlled Spec and not observed by this suite.

## 2. Input-defaulting rules
- The normal cases use `ncomp=3`, `nx=4`, `ny=3`. The state is `h(i,j) = 1 + 0.1*i + 0.05*j`, `hu(i,j) = 0.1*i - 0.2*j`, `hv(i,j) = 0.3*j - 0.05*i`. `h` is non-uniform and non-unit at every cell so that the momentum-form source `f*hv` differs from the velocity form `f*hv/h` and from `f*h*hv`, and `hu` and `hv` differ at every cell so that a swap is visible. `hu` and `hv` are of order 1 and `f` of order `1e-4`, so `S` is of order `1e-4` and the absolute tolerance `1e-12` of §6 is a relative tolerance of order `1e-8` on `S`. The row-dependent Coriolis parameter is `f(j) = 1.0e-4 + 2.0e-5*(j-1)`.
- The zero-`f` case uses the same state with `f(j) = 0` for every `j`.
- The abnormal cases each violate one clause of the Controlled Spec §4, with the same `f`, `nx=4`, `ny=3`. The below-three case uses `ncomp=2` and a state of two components; the above-three case uses `ncomp=4` and a state of four components (the fourth component `0`).

## 3. Execution-control rules
`N/A`: this `component` evaluates a pointwise source at a supplied state and defines no time-stepping or iteration. Execution control is the responsibility of the `problem` runner.

## 4. Case-expansion rules
`N/A`: the `L0` suite uses fixed inputs and defines no `case` sweep. Case expansion is defined at the `problem` level.

## 5. Diagnostics contract
- Require outputting `checks.zero_f`, `checks.pointwise`, `checks.energy_neutral`, and `checks.input_guard` in `diagnostics.json`.

## 6. Test definitions
- `test_id`: `l0_zero_f_gives_zero_source_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_source_2d_coriolis__apply`
  - `expected_outcome`: `pass`
  - `judgment`: with `f=0` in every row, every component of `S` is exactly `0` at every cell (component-wise max `|S| == 0`).
- `test_id`: `l0_pointwise_formula_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_source_2d_coriolis__apply`
  - `expected_outcome`: `pass`
  - `judgment`: with the row-dependent `f` of §2, satisfy `S(1,i,j) = 0`, `S(2,i,j) = f(j)*hv(i,j)`, and `S(3,i,j) = -f(j)*hu(i,j)` at every cell within an absolute tolerance of `1e-12` (component-wise max deviation `<= 1e-12`).
- `test_id`: `l0_energy_neutral_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_source_2d_coriolis__apply`
  - `expected_outcome`: `pass`
  - `judgment`: with the row-dependent `f` of §2, satisfy `hu(i,j)*S(2,i,j) + hv(i,j)*S(3,i,j) = 0` at every cell within an absolute tolerance of `1e-12` (max over cells of the absolute value `<= 1e-12`): the discrete evidence that the source does no work.
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
