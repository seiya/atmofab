# Tests: 2D `RK4` update (L0)

## 0. Meta information
- `test_profile_id`: `dynamics_shallow_water_time_update_2d_rk4_l0`
- `test_profile_version`: `0.5.0`
- `status`: `draft`
- `spec_ref.spec_kind`: `component`
- `spec_ref.spec_id`: `dynamics_shallow_water_time_update_2d_rk4`
- `spec_ref.spec_version`: `0.1.1`
- `spec_ref.controlled_spec_path`: `spec/component/dynamics/shallow_water/dynamics_shallow_water_time_update_2d_rk4/controlled_spec.md`

## 1. Test purpose
This suite verifies the published `operation` `dynamics_shallow_water_time_update_2d_rk4__advance` at `L0`: the zero-tendency invariance, the one-step closed form for a linear tendency, the fourth-order global convergence, the stage sequence (the four `rhs` calls per step, their times and their states), the axis-exchange symmetry pair, the smallest valid extent, a large `dt` and a component count other than three, and the input guard for each invalid input of the Controlled Spec §4 at its boundary (`dt=0`, `dt<0`, `ncomp=0`, `nx=0`, `ny=0`), including that the guard makes no `rhs` call. The tendency the tests pass as `rhs` is a procedure the host renders from the expression of §2; the `operation` under test is the only code that calls it.

Of the transformations of `docs/TESTS.md` §Symmetry pairs, the suite carries the **axis exchange** $(h, hu, hv)(x, y) \to (h, hv, hu)(y, x)$ as a pair: the transformed case has `nx` and `ny` exchanged, so every loop of the update runs over the other extent, and a loop bounded by the wrong extent, which on the base case (`nx > ny`) only runs past the array, leaves cells unwritten on the transformed case (`nx < ny`). The linear tendency `dUdt = lam*U` the suite passes acts element by element and treats the components alike, so it commutes with the exchange: the pair judges the `operation`, not the procedure. The two **mirrors** and the **translation** are admitted only trivially and carry no pair: each keeps the base's extents, and the update combines each element of the stage states with the same element of the tendencies by the same weights for every component and every cell, so with the linear tendency a mirrored or translated case is the base's elements reordered within the same loop ranges, with one component negated under a mirror, and its result is the base's result reordered and negated the same way whatever the kernel's loop bounds (measured: both loop-bound defects of `l0_symmetry_pairs_pass` give a mirror pair residual of exactly `0`). The closed-form and stage-state judgments of §6 pin every element of the base case, so a mirror pair would observe nothing they do not.

## 2. Input-defaulting rules
- The normal cases use `ncomp=3`, `nx=4`, `ny=3` and the initial state `U(c,i,j) = 1 + 0.5*c + 0.1*i - 0.2*j` (every value in `[1.0, 2.7]`), with `c`, `i`, `j` counted from `1`; `l0_linear_one_step_axis`, `l0_min_extent` and `l0_ncomp4` state their own extents, and the axis case its own state. The state is written over the cell-centre coordinates of a unit-length cell, `coordinates[]` `kc` on axis `0` with `count` and `length` `ncomp`, `xc` on axis `1` with `count` and `length` `nx`, and `yc` on axis `2` with `count` and `length` `ny`, each with placement `cell_center`, so `c = kc + 1/2`, `i = xc + 1/2`, `j = yc + 1/2`. The inputs of every case are `inputs.grid.ncomp`, `inputs.grid.nx`, `inputs.grid.ny`, `inputs.time.t_start`, `inputs.time.dt`, `inputs.time.n_step`, `inputs.time.t_end`, `inputs.history.every` (§3) and `inputs.constants.lam`. Every case starts at a non-zero `t_start`, so that a stage time computed from `0` instead of `t` is visible, and the two `(t_start, dt)` points whose stage times are judged differ, so that stage times written as constants (the values of one case) are visible in the other. `inputs.time.t_end` is `t_start + n_step*dt` where `dt` is valid: `1.15` on `l0_zero_rhs`; `2.6` on `l0_linear_one_step`, `l0_linear_one_step_axis`, `l0_min_extent`, `l0_ncomp4`, `l0_ncomp0`, `l0_nx0` and `l0_ny0`; `502.5` on `l0_large_dt`; `3.5` on the convergence pair; and on the two `dt` guard cases `2.5` on `l0_invalid_dt_zero` and `2.4` on `l0_invalid_dt_negative`.
- The tendency is one expression for every case: `rhs` returns `dUdt = lam*U` element by element, with the case's `inputs.constants.lam`.
- The zero-tendency case `l0_zero_rhs` has `lam=0.0`, so `rhs` returns `dUdt=0` at every call, with `t_start=0.75`, `dt=0.4` and `n_step=1`.
- The linear-tendency cases have `lam=-1.0` and `t_start=2.5`. The one-step case `l0_linear_one_step` has `dt=0.1` and `n_step=1`. The convergence pair integrates from `t=2.5` to `t=3.5` as two cases, `l0_linear_convergence_n10` with `n_step=10`, `dt=0.1` and `l0_linear_convergence_n20` with `n_step=20`, `dt=0.05`, each step starting at the driver's running time.
- The axis-exchange case `l0_linear_one_step_axis` is the transformed copy of `l0_linear_one_step` and sorts after it: `lam=-1.0`, `t_start=2.5`, `dt=0.1`, `n_step=1`, `ncomp=3`, `nx=3`, `ny=4`, and the state `U'(1,i,j) = 1.5 + 0.1*j - 0.2*i`, `U'(2,i,j) = 2.5 + 0.1*j - 0.2*i`, `U'(3,i,j) = 2.0 + 0.1*j - 0.2*i`, which is `U'(1,i,j) = U(1,j,i)`, `U'(2,i,j) = U(3,j,i)`, `U'(3,i,j) = U(2,j,i)` with `U` that of `l0_linear_one_step`: the spatial axes transposed and the two momentum components exchanged.
- `l0_min_extent` is the smallest valid state, `ncomp=1`, `nx=1`, `ny=1`, with the normal `U` at that one element (`U(1,1,1) = 1.4`), `lam=-1.0`, `t_start=2.5`, `dt=0.1`, `n_step=1`: a guard written `ncomp>1`, `nx>1` or `ny>1` refuses it.
- `l0_large_dt` has `lam=-2.0e-4`, `t_start=2.5`, `dt=500.0`, `n_step=1` and the normal extents and `U`, so `z = lam*dt` is the same `-0.1` and the closed form is the judged one at a large `dt`: a guard with an upper bound on `dt` refuses it. `l0_ncomp4` has `ncomp=4`, `nx=4`, `ny=3`, the `U` formula above for `c` up to `4`, `lam=-1.0`, `t_start=2.5`, `dt=0.1`, `n_step=1`: a guard with an upper bound on `ncomp` refuses it, and `l0_min_extent` is the one below three.
- The abnormal cases `l0_invalid_dt_zero` and `l0_invalid_dt_negative` each violate the `dt<=0` clause of the Controlled Spec §4 on one side of its boundary, `dt=0.0` and `dt=-0.1`, with `lam=-1.0`, `t_start=2.5`, `n_step=1` and the normal `U`. The call runs with that `dt`. Both sides are needed: a guard written as `dt<0` accepts the first and a guard written as `dt/=0` accepts the second.
- The abnormal cases `l0_ncomp0` (`ncomp=0`, `nx=4`, `ny=3`), `l0_nx0` (`ncomp=3`, `nx=0`, `ny=3`) and `l0_ny0` (`ncomp=3`, `nx=4`, `ny=0`) each violate one extent clause of the Controlled Spec §4 alone at its boundary, with `lam=-1.0`, `t_start=2.5`, `dt=0.1`, `n_step=1` and the normal `U` formula; their state has zero cells. An extent guard written `>=0` accepts one, and a guard joining its clauses by `and` instead of `or` accepts each.
- The tests `l0_linear_ode_one_step_pass` and `l0_stage_states_pass` read the same five cases, `l0_linear_one_step`, `l0_linear_one_step_axis`, `l0_min_extent`, `l0_large_dt` and `l0_ncomp4`, each with its own `U`. `l0_fourth_order_pass` reads the convergence pair, and `l0_zero_rhs_invariance_pass` reads `l0_zero_rhs`; `stage_sequence` is judged on `l0_zero_rhs` and those five cases (§5).

## 3. Execution-control rules
This `component` advances a single step for a given `dt` and does not control a time loop. The host-rendered driver runs the loop of every case: up to `inputs.time.n_step` calls of the `operation`, with `dt` from `inputs.time.dt`, the time starting at `inputs.time.t_start` and `t` passed as the driver's running time `t_start + step*dt`, up to the declared `inputs.time.t_end`. The `operation`'s `U_n` and `U_np1` bind ONE state, `U`, so each step starts from the state the last committed step left. A step commits only on a true `guard_pass`; a false one ends the loop on the last committed state.

history: every = (history.every) l0_zero_rhs = 1, l0_linear_one_step = 1, l0_linear_one_step_axis = 1, l0_min_extent = 1, l0_large_dt = 1, l0_ncomp4 = 1, l0_linear_convergence_n10 = 5, l0_linear_convergence_n20 = 10, l0_invalid_dt_zero = 1, l0_invalid_dt_negative = 1, l0_ncomp0 = 1, l0_nx0 = 1, l0_ny0 = 1

## 4. Case-expansion rules
`N/A`: the `L0` suite uses fixed inputs and defines no `case` sweep. Case expansion is defined at the `problem` level.

## 5. Judged quantities
A test is judged by the host's predicates over the captures alone (`docs/TESTS.md` requirement 9). Each quantity below is named on the `quantities` line of the tests that judge it, and is evaluated from the state captured before the first call and after the last committed call in each of the test's target cases.

The state the host-rendered driver holds for a case consists of `U` (`ncomp` × `nx` × `ny`, the initial state of §2 before the first call, the last committed result after the run), `guard_pass`, `n_calls`, `stage_t` and `stage_U1`, `stage_U2`, `stage_U3`, `stage_U4`. The `operation`'s bindings are `ncomp`, `nx`, `ny` from `inputs.grid.*`, `U_n` and `U_np1` both to `U`, `t` to the driver's time, `dt` to `inputs.time.dt`, and `guard_pass` to `guard_pass`, its result, which the driver writes into the state after each call as `1` when the call returned true and `0` when it returned false; it starts at `0`, so a result the `operation` never assigned captures `0`.

`rhs` binds the procedure the host renders from the expression `inputs.constants.lam * U`, with the driver's state behind it: `n_calls` (scalar) counts its calls over the case's whole loop, `stage_t` (4 values) stores the `t` it received at calls 1–4, and `stage_U1` … `stage_U4` (`ncomp` × `nx` × `ny` each) store the `U` it received at calls 1–4. `n_calls`, `stage_t` and `stage_U1..4` start at `0`, and each element stays `0` for a call not made: `{procedure: "inputs.constants.lam * U", calls: state.n_calls, record: {t: state.stage_t, U: [state.stage_U1, state.stage_U2, state.stage_U3, state.stage_U4]}}`.

A norm, a maximum or a residual over these variables is evaluated by the host, and every judgment of §6 is a statement about these variables and the §2 inputs. The expected stage times of `l0_zero_rhs`, `l0_linear_one_step`, `l0_linear_one_step_axis`, `l0_min_extent`, `l0_large_dt` and `l0_ncomp4` (`t`, `t + dt/2`, `t + dt/2`, `t + dt`, the values §6 states) are the declared input `inputs.expected.stage_t` of the case. `U_np1` starts each call non-finite on the driver's work copy, so a cell the `operation` leaves unwritten fails every judgment that reads it.

- `run.n_step`: the driver's committed step count against the count its loop runs by, `final.step - inputs.time.n_step`. It is `== 0` on the cases of a `pass` test.
- `guard_pass`: the captured `guard_pass`. It is `== 1` on the cases of a `pass` test and `== 0` on the cases of an `xfail` test.
- `zero_rhs_invariance`: on the zero-tendency case, `max|final.U - initial.U|` over every element.
- `one_step_closed_form`: on `l0_linear_one_step`, `l0_linear_one_step_axis`, `l0_min_extent`, `l0_large_dt` and `l0_ncomp4`, `max|final.U - (1 + z + z^2/2 + z^3/6 + z^4/24)*initial.U|` over every element, with `z = lam*dt` and each case's own `U`.
- `global_order`: over the convergence pair, carried by `l0_linear_convergence_n20`, which sorts last, `log(E(10)/E(20))/log(2)`, with `E(10) = max|at('l0_linear_convergence_n10').final.U - exp(-1.0)*at('l0_linear_convergence_n10').initial.U|` and `E(20) = max|final.U - exp(-1.0)*initial.U|` over every element.
- `stage_sequence`: on the zero-tendency case and on `l0_linear_one_step`, `l0_linear_one_step_axis`, `l0_min_extent`, `l0_large_dt` and `l0_ncomp4`, each against that case's own `dt`, `inputs.expected.stage_t` and `initial.U`, the largest of `max|stage_t - inputs.expected.stage_t|`; `max|stage_U1 - initial.U|`; `max|stage_U2 - (1 + z/2)*initial.U|`; `max|stage_U3 - (1 + z/2 + z^2/4)*initial.U|`; and `max|stage_U4 - (1 + z + z^2/2 + z^3/4)*initial.U|`, with `z = lam*dt`, which is `0` on the zero-tendency case, where every stage state is `initial.U`.
- `call_count`: the captured `n_calls` on a case the guard accepts. It equals four times the case's `n_step`: `4` for the zero-tendency case and the one-step cases (`n_step=1`), `40` for `l0_linear_convergence_n10`, `80` for `l0_linear_convergence_n20`.
- `rhs_not_called_on_guard`: the captured `n_calls` on an abnormal case. It is `== 0`.
- `symmetry_pair`: on `l0_linear_one_step_axis`, the `pair_residual` of each of `U`, `stage_U1`, `stage_U2`, `stage_U3` and `stage_U4`, which is `norm2(q - T(q_base)) / norm2(q_base)` over every element, where `q` is the variable's `final` value, `q_base` the same variable's `final` value in `l0_linear_one_step`, read through `at('l0_linear_one_step')`, and `T` the axis exchange: the spatial axes transposed and the second and third components exchanged, applied as `roll(flip(transpose(q, 1, 2), 0), 1, 0, 0)` (`docs/TESTS.md` §Symmetry pairs).
- `cross_target_state_agreement`: this target's run compared with every other target's certified variant of this node, case by case (`docs/TESTS.md` §Cross-target judgment): for one output `q` captured after the run and the same output `q_ref` of the same case in the other variant, `max|q - q_ref| / max(max|q|, max|q_ref|, 1)` over every element. The host evaluates it from the captured state, against each other variant separately; on a target with no other certified variant it holds vacuously. It is outside the `quantities` line.

## 6. Test definitions
- `test_id`: `l0_zero_rhs_invariance_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_time_update_2d_rk4__advance`
  - `expected_outcome`: `pass`
  - `quantities`: `call_count`, `guard_pass`, `run.n_step`, `stage_sequence`, `zero_rhs_invariance`
  - `judgment`: with the zero tendency, `t_start=0.75`, `dt=0.4`, satisfy `final.U = initial.U` within an absolute tolerance of `1e-12` (`zero_rhs_invariance <= 1e-12`), `guard_pass == 1`, `run.n_step == 0`, `n_calls == 4` (`call_count == 4`), and the stored stage times `stage_t` are `0.75`, `0.95`, `0.95`, `1.15` and the stored stage states `stage_U1..4` are each `initial.U` (the stage states of the Controlled Spec §3 for `R = 0`), each within an absolute tolerance of `1e-12` (`stage_sequence <= 1e-12`). Together with `l0_stage_states_pass` this judges the stage times at the `(t, dt)` points `(0.75, 0.4)`, `(2.5, 0.1)` and `(2.5, 500.0)`, so a model whose stage times are constants rather than `t`, `t + dt/2`, `t + dt/2`, `t + dt` fails one of the two. The cross-target judgment is applied: `cross_target_state_agreement` over `U` is `<= 1e-12`.
- `test_id`: `l0_linear_ode_one_step_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_time_update_2d_rk4__advance`
  - `expected_outcome`: `pass`
  - `quantities`: `guard_pass`, `one_step_closed_form`, `run.n_step`
  - `judgment`: on `l0_linear_one_step`, `l0_linear_one_step_axis`, `l0_min_extent`, `l0_ncomp4` (`lam=-1.0`, `dt=0.1`) and `l0_large_dt` (`lam=-2.0e-4`, `dt=500.0`), each with its own `U`, satisfy `final.U = (1 + z + z^2/2 + z^3/6 + z^4/24)*initial.U` with `z = lam*dt = -0.1` (the factor is `0.9048375`) within an absolute tolerance of `1e-12` (`one_step_closed_form <= 1e-12`), `guard_pass == 1` and `run.n_step == 0`. The factor differs from `exp(z) = 0.90483741803...` by `8.2e-8`, from the three-stage third-order factor `1 + z + z^2/2 + z^3/6` by `4.2e-6`, and from the forward-Euler factor `1 + z` by `4.8e-3`, so the tolerance excludes `exp(z)`, a third-order and a first-order composition. It does not by itself single out the classical stage points: every explicit four-stage fourth-order method has the same one-step factor for a linear tendency (the 3/8-rule, with the stage state `(1 + z/3)*initial.U` at its second call, gives `0.9048375` too), so the stage points of the Controlled Spec §3 are pinned by `l0_stage_states_pass`, not by this test. The cross-target judgment is applied: `cross_target_state_agreement` over `U` is `<= 1e-12`.
- `test_id`: `l0_fourth_order_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_time_update_2d_rk4__advance`
  - `expected_outcome`: `pass`
  - `quantities`: `call_count`, `global_order`, `guard_pass`, `run.n_step`
  - `judgment`: with `lam=-1.0`, let `E(10)` be the component-wise max of `|final.U - exp(-1.0)*initial.U|` on `l0_linear_convergence_n10` and `E(20)` the same on `l0_linear_convergence_n20`. The observed order `p = log(E(10)/E(20))/log(2)` satisfies `p >= 3.9` (`global_order >= 3.9`; the closed form gives `E(10) = 3.33e-7*max|initial.U|`, `E(20) = 2.00e-8*max|initial.U|`, `p = 4.06`; a third-order method gives `p = 3.06`), `n_calls == 40` on `l0_linear_convergence_n10` and `n_calls == 80` on `l0_linear_convergence_n20` (`call_count`: four `rhs` calls in each step), `guard_pass == 1` and `run.n_step == 0` on both. The cross-target judgment is applied: `cross_target_state_agreement` over `U` is `<= 1e-12`.
- `test_id`: `l0_stage_states_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_time_update_2d_rk4__advance`
  - `expected_outcome`: `pass`
  - `quantities`: `call_count`, `guard_pass`, `run.n_step`, `stage_sequence`
  - `judgment`: on `l0_linear_one_step`, `l0_linear_one_step_axis`, `l0_min_extent`, `l0_ncomp4` (`lam=-1.0`, `dt=0.1`) and `l0_large_dt` (`lam=-2.0e-4`, `dt=500.0`), each with its own `U` and `t_start=2.5`, one call of the `operation` makes exactly `4` calls of `rhs` (`n_calls == 4`, `call_count == 4`), `guard_pass == 1`, `run.n_step == 0`, and the stored stage times `stage_t` are `2.5`, `2.55`, `2.55`, `2.6` (on `l0_large_dt` `2.5`, `252.5`, `252.5`, `502.5`) and the stored stage states `stage_U1..4` are `initial.U`, `(1 + z/2)*initial.U`, `(1 + z/2 + z^2/4)*initial.U`, `(1 + z + z^2/2 + z^3/4)*initial.U` with `z = lam*dt = -0.1` — the stage states of the Controlled Spec §3 for `R(t,U) = lam*U`, written in terms of `initial.U` alone — each within an absolute tolerance of `1e-12` (`stage_sequence <= 1e-12`). A model that calls `rhs` once and reuses the tendency, that evaluates a later stage at the step's input state, or that passes a stage time computed from `0` instead of `t`, fails this judgment. The cross-target judgment is applied: `cross_target_state_agreement` over `stage_U1`, `stage_U2`, `stage_U3`, and `stage_U4` is `<= 1e-12`.
- `test_id`: `l0_symmetry_pairs_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_time_update_2d_rk4__advance`
  - `expected_outcome`: `pass`
  - `quantities`: `guard_pass`, `run.n_step`, `symmetry_pair`
  - `judgment`: on `l0_linear_one_step_axis`, `pair_residual` against `l0_linear_one_step` is `<= 1.0e-12` for each of `U`, `stage_U1`, `stage_U2`, `stage_U3` and `stage_U4` (`symmetry_pair <= 1.0e-12`), and `guard_pass == 1` and `run.n_step == 0` on both cases. Every element of the transformed case is computed by the same operations as the element of the base it is the image of, so a conforming kernel gives round-off at most (measured `0` with an independent reference of the Controlled Spec §3, from the closed form a deviation of `4.4e-16` on either case); the bound is the suite's cross-target bound. Measured on the certified kernel with one loop bound changed: a loop over `j` bounded by `nx` runs past its array on `l0_linear_one_step`, and the run stops with no judgment evaluated; a loop bounded by `min(nx, ny)` passes on `l0_linear_one_step` and on the axis case leaves the last row unwritten. In the final combination that row of `U` is non-finite; in the loop building a stage state the row of the temporary is unwritten; the closed-form and pair judgments fail in both. The transformed case is the evidence, and the pair states it as an equivariance of the base. The cross-target judgment is applied: `cross_target_state_agreement` over `U`, `stage_U1`, `stage_U2`, `stage_U3`, and `stage_U4` is `<= 1e-12`.
- `test_id`: `l0_invalid_dt_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_time_update_2d_rk4__advance`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `dt<=0`
  - `quantities`: `guard_pass`, `rhs_not_called_on_guard`
  - `judgment`: on `l0_invalid_dt_zero` (`dt=0.0`), the `operation` refuses the input: `guard_pass == 0`, in the failure direction, and it makes no `rhs` call (`n_calls == 0`, `rhs_not_called_on_guard == 0`). A model that evaluates `rhs` before or despite the guard, or whose guard is `dt<0`, fails this test. The cross-target judgment is not applied.
- `test_id`: `l0_negative_dt_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_time_update_2d_rk4__advance`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `dt<0`
  - `quantities`: `guard_pass`, `rhs_not_called_on_guard`
  - `judgment`: on `l0_invalid_dt_negative` (`dt=-0.1`), the same as `l0_invalid_dt_xfail`: `guard_pass == 0`, in the failure direction, and `n_calls == 0` (`rhs_not_called_on_guard == 0`). A model whose guard is `dt/=0` fails this test. The cross-target judgment is not applied.
- `test_id`: `l0_invalid_ncomp_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_time_update_2d_rk4__advance`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `ncomp<1`
  - `quantities`: `guard_pass`, `rhs_not_called_on_guard`
  - `judgment`: on `l0_ncomp0`, the `operation` refuses the input: `guard_pass == 0`, in the failure direction, and `n_calls == 0` (`rhs_not_called_on_guard == 0`). The state has no cell, so the flag and the call count are all there is to judge. The cross-target judgment is not applied.
- `test_id`: `l0_invalid_nx_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_time_update_2d_rk4__advance`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `nx<1`
  - `quantities`: `guard_pass`, `rhs_not_called_on_guard`
  - `judgment`: on `l0_nx0`, the `operation` refuses the input: `guard_pass == 0`, in the failure direction, and `n_calls == 0` (`rhs_not_called_on_guard == 0`). The state has no cell, so the flag and the call count are all there is to judge. The cross-target judgment is not applied.
- `test_id`: `l0_invalid_ny_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_time_update_2d_rk4__advance`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `ny<1`
  - `quantities`: `guard_pass`, `rhs_not_called_on_guard`
  - `judgment`: on `l0_ny0`, the `operation` refuses the input: `guard_pass == 0`, in the failure direction, and `n_calls == 0` (`rhs_not_called_on_guard == 0`). The state has no cell, so the flag and the call count are all there is to judge. The cross-target judgment is not applied.

## 7. Pass/fail aggregation rules
- `per_test.pass_rule`: `pass` when every predicate of the test holds.
- `per_test.xfail_rule`: `xfail` when `xfail_condition` is true and every predicate of the test holds.
- `suite.pass_rule`: `pass` when all `test_id` are `pass` or `xfail`.

## 8. Traceability
- Record `test_profile_id` and `test_profile_version` in `trial_meta.json`.
