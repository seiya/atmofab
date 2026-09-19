# Tests: 2D `RK4` update (L0)

## 0. Meta information
- `test_profile_id`: `dynamics_shallow_water_time_update_2d_rk4_l0`
- `test_profile_version`: `0.1.0`
- `status`: `draft`
- `spec_ref.spec_kind`: `component`
- `spec_ref.spec_id`: `dynamics_shallow_water_time_update_2d_rk4`
- `spec_ref.spec_version`: `0.1.0`
- `spec_ref.controlled_spec_path`: `spec/component/dynamics/shallow_water/dynamics_shallow_water_time_update_2d_rk4/controlled_spec.md`

## 1. Test purpose
This suite verifies the published `operation` `dynamics_shallow_water_time_update_2d_rk4__advance` at `L0`: the zero-tendency invariance, the one-step closed form for a linear tendency, the fourth-order global convergence, the stage sequence (the four `rhs` calls per step, their times and their states), and the input guard for an invalid time step (`dt<=0`), including that the guard makes no `rhs` call. The tendency procedures the tests pass as `rhs` are supplied by the checks module; the `operation` under test is the only code that calls them.

## 2. Input-defaulting rules
- The normal cases use `ncomp=3`, `nx=4`, `ny=3`, the state `U_n(c,i,j) = 1 + 0.5*c + 0.1*i - 0.2*j` (every value in `[1.0, 2.7]`), and `t=2.5` (a non-zero start time, so that a stage time computed from `0` instead of `t` is visible).
- The zero-tendency case passes an `rhs` that returns `dUdt=0` at every call, with `dt=0.1`.
- The linear-tendency cases pass an `rhs` that returns `dUdt = lambda*U` with `lambda=-1.0`. The one-step case uses `dt=0.1` and calls the `operation` once. The convergence case integrates from `t=2.5` to `t=3.5` twice, with `n=10` steps of `dt=0.1` and with `n=20` steps of `dt=0.05`, each step's `t` being the running time, so the `operation` is called `30` times.
- Every `rhs` the checks module supplies records, in module-level state of the checks module, the number of calls made since the case was set up and the `t` and `U` it received at each of the first four calls; the checks module also records the `guard_pass` the last call of the `operation` returned. Those records are state variables of the case (§5), so the judgment reads them from the captured state and not from a value the model returns.
- The one-step and stage-sequence judgments read the same case (the linear tendency, `dt=0.1`, one call of the `operation`). The convergence judgment reads its own case (the two integrations), and the zero-tendency judgment its own.
- The abnormal case uses `dt=0.0` (`dt<=0`) with the linear tendency, `t=2.5`, and one call of the `operation`; its state is the normal `U_n` and the records above. The extent clauses `ncomp<1` / `nx<1` / `ny<1` of the Controlled Spec §4 have no case: a state of zero extent has no snapshot the judgment could read, so those clauses are stated by the Controlled Spec and not observed by this suite.

## 3. Execution-control rules
`N/A`: this `component` advances a single step for a given `dt` and does not control the time loop or its cadence. The convergence case's loop of `n` steps is a fixed input of §2 and is driven by the checks module, not by the `component`. Execution control is the responsibility of the `problem` runner.

## 4. Case-expansion rules
`N/A`: the `L0` suite uses fixed inputs and defines no `case` sweep. Case expansion is defined at the `problem` level.

## 5. Diagnostics contract
- Require outputting `checks.zero_rhs_invariance`, `checks.one_step_closed_form`, `checks.global_order`, `checks.stage_sequence`, `checks.call_count`, `checks.input_guard`, and `checks.rhs_not_called_on_guard` in `diagnostics.json`.
- The state of a case, captured after setup and after the run, consists of `U_n` (`ncomp` × `nx` × `ny`, the input state), `U_np1` (`ncomp` × `nx` × `ny`, the result of the one call of the `operation`; zero in the convergence case), `U_end_n10` and `U_end_n20` (`ncomp` × `nx` × `ny`, the states after the `n=10` and `n=20` integrations; zero outside the convergence case), `guard_pass` (scalar, `1` when the last call of the `operation` returned true and `0` when it returned false), `n_calls` (scalar, the total number of `rhs` calls made during the case's run), `stage_t` (4 values, the `t` received at calls 1–4; zero for a call not made), and `stage_U1`, `stage_U2`, `stage_U3`, `stage_U4` (`ncomp` × `nx` × `ny` each, the `U` received at calls 1–4; zero for a call not made). Every judgment of §6 is a statement about these variables and the §2 inputs.
- Each check is computed on the case named here and is `na` on every other case: `checks.zero_rhs_invariance` on the zero-tendency case; `checks.one_step_closed_form` and `checks.stage_sequence` on the one-step case; `checks.global_order` on the convergence case; `checks.call_count` on every case the guard accepts (`pass` when `n_calls` equals four times the number of calls of the `operation` the case makes: `4` for the zero-tendency and one-step cases, `120` for the convergence case); `checks.input_guard` on every case (`pass` when `guard_pass = 1`, `fail` when `guard_pass = 0`); `checks.rhs_not_called_on_guard` on the abnormal case (`pass` when `n_calls = 0`, `fail` otherwise).

## 6. Test definitions
- `test_id`: `l0_zero_rhs_invariance_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_time_update_2d_rk4__advance`
  - `expected_outcome`: `pass`
  - `judgment`: with the zero tendency, satisfy `U_np1 = U_n` within an absolute tolerance of `1e-12` (component-wise max deviation `<= 1e-12`) and `guard_pass = 1` (`checks.zero_rhs_invariance`), and `n_calls = 4` (`checks.call_count`).
- `test_id`: `l0_linear_ode_one_step_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_time_update_2d_rk4__advance`
  - `expected_outcome`: `pass`
  - `judgment`: with the linear tendency, `lambda=-1.0`, `dt=0.1`, satisfy `U_np1 = (1 + z + z^2/2 + z^3/6 + z^4/24)*U_n` with `z = lambda*dt = -0.1` (the factor is `0.9048375`) within an absolute tolerance of `1e-12` (component-wise max deviation `<= 1e-12`), and `guard_pass = 1` (`checks.one_step_closed_form`). The factor differs from `exp(z) = 0.90483741803...` by `8.2e-8`, from the three-stage third-order factor `1 + z + z^2/2 + z^3/6` by `4.2e-6`, and from the forward-Euler factor `1 + z` by `4.8e-3`, so the tolerance admits the four-stage composition of the Controlled Spec §3 only.
- `test_id`: `l0_fourth_order_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_time_update_2d_rk4__advance`
  - `expected_outcome`: `pass`
  - `judgment`: with the linear tendency, let `E(10)` be the component-wise max of `|U_end_n10 - exp(-1.0)*U_n|` and `E(20)` that of `|U_end_n20 - exp(-1.0)*U_n|`. The observed order `p = log(E(10)/E(20))/log(2)` satisfies `p >= 3.9` (`checks.global_order`; the closed form gives `E(10) = 3.33e-7*max|U_n|`, `E(20) = 2.00e-8*max|U_n|`, `p = 4.06`; a third-order method gives `p = 3.06`), and `n_calls = 120` (`checks.call_count`: four `rhs` calls in each of the `30` steps).
- `test_id`: `l0_stage_states_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_time_update_2d_rk4__advance`
  - `expected_outcome`: `pass`
  - `judgment`: with the linear tendency, `t=2.5`, `dt=0.1`, one call of the `operation` makes exactly `4` calls of `rhs` (`n_calls = 4`, `checks.call_count`), and the recorded stage times `stage_t` are `2.5`, `2.55`, `2.55`, `2.6` and the recorded stage states `stage_U1..4` are `U_n`, `(1 + z/2)*U_n`, `(1 + z/2 + z^2/4)*U_n`, `(1 + z + z^2/2 + z^3/4)*U_n` with `z = lambda*dt = -0.1` — the stage states of the Controlled Spec §3 for `R(t,U) = lambda*U`, written in terms of `U_n` alone — each within an absolute tolerance of `1e-12` (`checks.stage_sequence`). A model that calls `rhs` once and reuses the tendency, that evaluates a stage at `U_n`, or that passes a stage time computed from `0` instead of `t`, fails this judgment.
- `test_id`: `l0_invalid_dt_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_time_update_2d_rk4__advance`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `dt<=0`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard' and checks.rhs_not_called_on_guard.status == pass`
  - `judgment`: with `dt=0.0`, the `operation` returns `guard_pass = 0` (`checks.input_guard` is `fail`, which makes the case's `verdict.overall` `fail`) and makes no `rhs` call (`n_calls = 0`, `checks.rhs_not_called_on_guard` is `pass`). A model that evaluates `rhs` before or despite the guard fails this test.

## 7. Pass/fail aggregation rules
- `per_test.pass_rule`: `pass` when the judgment expression is satisfied.
- `per_test.xfail_rule`: `xfail` when `xfail_condition` is true and `pass_when` is satisfied.
- `suite.pass_rule`: `pass` when all `test_id` are `pass` or `xfail`.

## 8. Traceability
- Record `test_profile_id` and `test_profile_version` in `trial_meta.json`.
