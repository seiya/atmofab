# Tests: 1D forward Euler update (L0)

## 0. Meta information
- `test_profile_id`: `dynamics_advection_diffusion_time_update_1d_euler1_l0`
- `test_profile_version`: `0.5.0`
- `status`: `draft`
- `spec_ref.spec_kind`: `component`
- `spec_ref.spec_id`: `dynamics_advection_diffusion_time_update_1d_euler1`
- `spec_ref.spec_version`: `0.3.1`
- `spec_ref.controlled_spec_path`: `spec/component/dynamics/advection_diffusion/dynamics_advection_diffusion_time_update_1d_euler1/controlled_spec.md`

## 1. Test purpose
This suite verifies the published `operation` `dynamics_advection_diffusion_time_update_1d_euler1__advance` at `L0`: the zero-tendency invariance, the single-step update-formula consistency, and the input guard for an invalid time step (`dt<=0`).

The suite carries no symmetry pair (`docs/TESTS.md` §Symmetry pairs). The update admits the **mirror** (`u_n` and `L_flux` reversed together; a tendency is not a component along $x$, so no sign is reversed), the **translation** (both arrays shifted together), and, being linear, the **sign reversal** ($u^n \to -u^n$, $L \to -L$). Each cell's update reads only that cell's own values (Controlled Spec §3), so each of these maps the output by the same transformation for every conforming kernel, and `l0_single_step_formula_pass` pins every cell of the output on its fixed inputs: a kernel that a pair over those inputs separates from a conforming one deviates in that judgment too (measured with an independent reference of §3: a loop one cell short, leaving the setup sentinel in the last cell, deviates there by `1001.68` and a tendency read one cell shifted by `0.58`). The transformations judge nothing beyond the formula case, and a one-dimensional update has no axis exchange.

## 2. Input-defaulting rules
- The normal case uses `nx>=1`, `dt>0`, and a finite `L_flux`.
- The abnormal case uses `dt<=0`.

## 3. Execution-control rules
`N/A`: this `component` advances a single step for a given `dt` and does not control the time loop or its cadence. Execution control is the responsibility of the `problem` runner.

## 4. Case-expansion rules
`N/A`: the `L0` suite uses fixed inputs and defines no `case` sweep. Case expansion is defined at the `problem` level.

## 5. Judged quantities
A test is judged by the host's predicates over the captures alone (`docs/TESTS.md` requirement 9). Each quantity below is named on the `quantities` line of the tests that judge it, and is evaluated from the state captured after the run in each of the test's target cases.

The state of a case, captured after setup and after the run, consists of `u_n` and `L_flux` (the inputs of §2, written at setup), `u_np1` (`nx` values, the sentinel `-999` at setup) and `guard_pass`. `u_n`, `L_flux` and `u_np1` are arguments of the `operation`. `guard_pass` is a `record` the checks module writes of the `operation`'s logical result, a scalar recorded as a number because a state variable is real-valued: `-1` at setup, before the `operation` is called; `1` when the call returned true; `0` when it returned false. The setup value `-1` is distinct from both values the call can return, so a case whose `operation` was not called captures `-1`. An output array of a rejected input is not defined by the Controlled Spec §4, so the flag is the only state that distinguishes a rejection from an executed update.

- `guard_pass`: the captured flag. It is `== 1` on the cases of a `pass` test and `== 0` on the cases of an `xfail` test.
- `zero_tendency_difference`: `max|u_np1 - u_n|` over every cell, with `u_n` the captured input.
- `formula_max_deviation`: `max|u_np1 - u_np1_expected|` over every cell, where `u_np1_expected` is the output the case's judgment of §6 states (`u_n + dt*L_flux` on the case's inputs).
- `cross_target_state_agreement`: this target's run compared with every other target's certified variant of this node, case by case (`docs/TESTS.md` §Cross-target judgment): for one output `q` captured after the run and the same output `q_ref` of the same case in the other variant, `max|q - q_ref| / max(max|q|, max|q_ref|, 1)` over every element. The host evaluates it from the captured state, against each other variant separately; on a target with no other certified variant it holds vacuously. It is outside the `quantities` line.

## 6. Test definitions
- `test_id`: `l0_zero_tendency_invariance_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_advection_diffusion_time_update_1d_euler1__advance`
  - `expected_outcome`: `pass`
  - `quantities`: `guard_pass`, `zero_tendency_difference`
  - `judgment`: with `L_flux = 0` and a non-uniform `u_n`, `guard_pass == 1` and `u^{n+1}=u^n` exactly (`zero_tendency_difference == 0`): $u+\Delta t\cdot 0$ is $u$ itself. The cross-target judgment is applied: `cross_target_state_agreement` over `u_np1` is `<= 1e-12`.
- `test_id`: `l0_single_step_formula_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_advection_diffusion_time_update_1d_euler1__advance`
  - `expected_outcome`: `pass`
  - `quantities`: `formula_max_deviation`, `guard_pass`
  - `judgment`: with the fixed inputs `nx=4`, `u_n = [0.3, 1.7, 0.2, 2.9]`, `L_flux = [1.1, -0.4, 3.6, -2.2]`, `dt=0.1`, `guard_pass == 1` and the output equals `u_np1 = [0.41, 1.66, 0.56, 2.68]` (`u_n + dt*L_flux`) within an absolute tolerance of `1e-12` (`formula_max_deviation <= 1e-12`). On these values an update that drops `dt`, reverses the sign of `L_flux`, uses `dt^2`, ignores `L_flux`, or reads `L_flux` or `u_n` one cell shifted deviates by at least `0.3` at some cell. The cross-target judgment is applied: `cross_target_state_agreement` over `u_np1` is `<= 1e-12`.
- `test_id`: `l0_invalid_dt_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_advection_diffusion_time_update_1d_euler1__advance`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `dt<=0`
  - `quantities`: `guard_pass`
  - `judgment`: on the abnormal case of §2 (`l0_invalid_dt`), the `operation` refuses the input: `guard_pass == 0`, in the failure direction. The cross-target judgment is not applied.

## 7. Pass/fail aggregation rules
- `per_test.pass_rule`: `pass` when every predicate of the test holds.
- `per_test.xfail_rule`: `xfail` when `xfail_condition` is true and every predicate of the test holds.
- `suite.pass_rule`: `pass` when all `test_id` are `pass` or `xfail`.

## 8. Traceability
- Record `test_profile_id` and `test_profile_version` in `trial_meta.json`.
