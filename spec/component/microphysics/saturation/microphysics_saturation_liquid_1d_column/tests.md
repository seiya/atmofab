# Tests: liquid-water saturation and saturation adjustment of a column (L0)

## 0. Meta information
- `test_profile_id`: `microphysics_saturation_liquid_1d_column_l0`
- `test_profile_version`: `0.1.0`
- `status`: `draft`
- `spec_ref.spec_kind`: `component`
- `spec_ref.spec_id`: `microphysics_saturation_liquid_1d_column`
- `spec_ref.spec_version`: `0.1.0`
- `spec_ref.controlled_spec_path`: `spec/component/microphysics/saturation/microphysics_saturation_liquid_1d_column/controlled_spec.md`

## 1. Test purpose
This suite verifies the two published `operation`s at `L0`. For `__dens2qsat_liq`: the reference point, the closed form, and the monotonicity in temperature and in density. For `__saturation_adjustment`: the unsaturated no-op, the complete evaporation of cloud water, the conservation of total water and of moist internal energy, the saturated state of the Newton branch and the energy release. For each `operation`: the halo contract and the input guard; for `__saturation_adjustment` also the finite refusal state of a cell that does not converge.

The symmetry pairs of `docs/TESTS.md` §Symmetry pairs are not judged. A one-dimensional column has no axis exchange. A reversal or a translation of the column is a pointwise identity: each `operation` couples no two cells, so the transformed case compares each cell's computation with itself. The CFL number is `N/A`: the `operation`s have no transport and no time step. The comparison with theory is the closed-form judgments of §6.

## 2. Input-defaulting rules
The block below is the suite's whole input. Every case has `ka=8`, and every case but the two bad-range ones passes `ks=2`, `ke=7`, so the first and the last cell are the halo.

- `constants` are the values of the constant arguments of Controlled Spec §2 in every case.
- `setup` is each state variable's value at every cell before a case's own rules apply. A case's `columns` entry replaces the interior cells: `[a, b]` gives `a + b*(k-2)` at cell `k`, and `{cells: [...]}` gives the six interior values literally. A variable a case names no rule for keeps its `setup` value at every cell: the halo sentinels of `temp`, `dens`, `qv`, `qc`, `cptot`, `cvtot` are positive, so that a host expression over a whole column stays finite, and `qsat`, `rhoe_d` hold `-999`.
- In a case with a `qv` rule, the interior cells of `cptot` and `cvtot` are the moist-air mixture over the case's `qv` and `qc`, with `air` the dry-air constants: $q_d=1-q_v-q_c$, `cptot` $=q_d\,$`cpdry`$+$`cpvap`$\,q_v+$`cl`$\,q_c$, `cvtot` $=q_d($`cpdry`$-$`rdry`$)+($`cpvap`$-$`rvap`$)q_v+$`cl`$\,q_c$.
- `overrides` set one cell after every other rule, and are what force the guard cases.
- `operation` names the published `operation` a case calls and `expected` the outcome §6 states. `refusal` states the flags a refused case's run leaves: `guard_pass = 0` where it is empty, and otherwise the values it lists. `nonconvergence_check` marks the case whose cell 4 does not converge.
- `grid`, on a bad-range case, replaces the `ks`, `ke` passed to the `operation` after the state is built with `ks=2`, `ke=7`.
- `parameters.qsat_ratio`, on `l0_satadj_nonconverged`, states the multiple of $q_s$ at `temp=277.8`, `dens=1` that the literal `qv` of cell 4 is; it is the condition the case's `xfail_condition` reads. The literal is $q_s(277.8,1)$ of Controlled Spec §3 in double precision, multiplied by `qsat_ratio`.

No case sits on the boundary between the two branches of Controlled Spec §3, where a change of the last bit changes the branch: at every interior cell of every `pass` case, $q_\Sigma$ and $q_s$ at the virtual temperature differ by more than `50%` of $q_s$.

```yaml
grid: {ka: 8, ks: 2, ke: 7}
constants: {rvap: 461.50, cpvap: 1846.0, cl: 4218.0, lhv0: 2.501e+6, psat0: 610.78, tem00: 273.15}
air: {cpdry: 1004.64, rdry: 287.04}
setup: {temp: 222.0, dens: 0.77, qv: 0.0123, qc: 0.0321, cptot: 1111.0, cvtot: 777.0, qsat: -999.0, rhoe_d: -999.0}
scalars: {guard_pass: -1.0, converged: -1.0}
cases:
- {case_id: l0_qsat_ref, operation: dens2qsat_liq, expected: pass, columns: {temp: [273.15, 0.0], dens: [1.2, -0.1]}}
- {case_id: l0_qsat_temp_ramp, operation: dens2qsat_liq, expected: pass, columns: {temp: [250.0, 10.0], dens: [1.0, 0.0]}}
- {case_id: l0_qsat_dens_ramp, operation: dens2qsat_liq, expected: pass, columns: {temp: [285.0, 0.0], dens: [0.6, 0.1]}}
- {case_id: l0_satadj_dry, operation: saturation_adjustment, expected: pass, columns: {temp: [280.0, 4.0], dens: [1.1, -0.05], qv: [0.002, 2.0e-4], qc: [0.0, 0.0]}}
- {case_id: l0_satadj_subsat, operation: saturation_adjustment, expected: pass, columns: {temp: [285.0, 3.0], dens: [1.05, -0.05], qv: [0.002, 3.0e-4], qc: [5.0e-4, 1.0e-4]}}
- {case_id: l0_satadj_supersat, operation: saturation_adjustment, expected: pass, columns: {temp: [280.0, 3.0], dens: [1.1, -0.05], qv: [0.009, 0.003], qc: [1.0e-3, 2.0e-4]}}
- {case_id: l0_dens_nonpositive, operation: dens2qsat_liq, expected: xfail, refusal: {}, columns: {temp: [280.0, 4.0], dens: [1.1, -0.05]}, overrides: [{var: dens, k: 4, value: 0.0}]}
- {case_id: l0_temp_nonpositive, operation: dens2qsat_liq, expected: xfail, refusal: {}, columns: {temp: [280.0, 4.0], dens: [1.1, -0.05]}, overrides: [{var: temp, k: 4, value: -1.0}]}
- {case_id: l0_bad_range, operation: dens2qsat_liq, expected: xfail, refusal: {}, grid: {ks: 7, ke: 2}, columns: {temp: [280.0, 4.0], dens: [1.1, -0.05]}}
- {case_id: l0_satadj_dens_nonpositive, operation: saturation_adjustment, expected: xfail, refusal: {}, columns: {temp: [285.0, 3.0], dens: [1.05, -0.05], qv: [0.002, 3.0e-4], qc: [5.0e-4, 1.0e-4]}, overrides: [{var: dens, k: 4, value: 0.0}]}
- {case_id: l0_satadj_temp_nonpositive, operation: saturation_adjustment, expected: xfail, refusal: {}, columns: {temp: [285.0, 3.0], dens: [1.05, -0.05], qv: [0.002, 3.0e-4], qc: [5.0e-4, 1.0e-4]}, overrides: [{var: temp, k: 4, value: -1.0}]}
- {case_id: l0_satadj_cvtot_nonpositive, operation: saturation_adjustment, expected: xfail, refusal: {}, columns: {temp: [285.0, 3.0], dens: [1.05, -0.05], qv: [0.002, 3.0e-4], qc: [5.0e-4, 1.0e-4]}, overrides: [{var: cvtot, k: 4, value: 0.0}]}
- {case_id: l0_satadj_bad_range, operation: saturation_adjustment, expected: xfail, refusal: {}, grid: {ks: 7, ke: 2}, columns: {temp: [285.0, 3.0], dens: [1.05, -0.05], qv: [0.002, 3.0e-4], qc: [5.0e-4, 1.0e-4]}}
- {case_id: l0_satadj_nonconverged, operation: saturation_adjustment, expected: xfail, refusal: {guard_pass: 1.0, converged: 0.0}, nonconvergence_check: true, parameters: {qsat_ratio: 100.0}, columns: {temp: [277.8, 0.0], dens: [1.0, 0.0], qv: {cells: [0.001, 0.001, 0.66357706133254214, 0.001, 0.001, 0.001]}, qc: [0.0, 0.0]}}
```

## 3. Execution-control rules
`N/A`: each `operation` is a single evaluation of one column and defines no time-stepping. The iteration of `__saturation_adjustment` is part of its definition (Controlled Spec §3), and the suite controls no part of it.

## 4. Case-expansion rules
`N/A`: the `L0` suite uses the fixed cases of §2 and defines no `case` sweep.

## 5. Diagnostics contract
- Require outputting `checks.qsat_reference`, `checks.qsat_formula`, `checks.qsat_monotone`, `checks.satadj_dry_noop`, `checks.satadj_evaporates_all`, `checks.satadj_conservation`, `checks.satadj_saturated`, `checks.halo_untouched`, `checks.converged` and `checks.input_guard` in `diagnostics.json`. Each is computed on the cases of the test that names it in §6 and is `na` on every other case; `checks.input_guard` and `checks.converged` are defined in the next item.
- The state of a case, captured after setup and after the run, consists of `temp`, `dens`, `qv`, `qc`, `cptot`, `cvtot`, `qsat`, `rhoe_d` (each with extent `ka`, at the §2 values at setup) and the scalars `guard_pass` and `converged`, each recorded as a number because a state variable is real-valued: `-1` at setup, before the `operation` is called; `1` when the call returned true; `0` when it returned false. A case of `__dens2qsat_liq` leaves `converged` at `-1`, and every variable an `operation` does not publish keeps its setup value. `checks.input_guard` is `pass` when `guard_pass = 1` and `fail` otherwise. `checks.converged` is `na` when `guard_pass` is not `1` and on the cases of `__dens2qsat_liq`; otherwise it is `pass` when `converged = 1` and `fail` otherwise.
- A judgment reads a halo cell only through the masks below. With $i=k-1$ and $c=i+1/2$ the cell-centre coordinate of a unit-length cell, `coordinates[]` axis `0`, `count` and `length` `ka`, placement `cell_center`: `interior` $=\min(1,\max(0,\lfloor c\rfloor-(ks-2)))\cdot\min(1,\max(0,ke-\lfloor c\rfloor))$ and `halo` $=1-$`interior`. `pairs` $=\min(1,\max(0,\lfloor c\rfloor-(ks-2)))\cdot\min(1,\max(0,ke-1-\lfloor c\rfloor))$ marks the cells $k=ks..ke-1$, whose next cell is interior.
- `qs_final` and `dqs_final` are $q_s$ and $dq_s/dT$ of Controlled Spec §3 at the captured `final.temp` and `final.dens`, over every cell (the halo sentinels keep them finite). `L00` and `cvv` are the derived constants of Controlled Spec §2.
- The round-off bounds of §6 are offline calibrations against an independent implementation of the scheme, the original `scale-met/scale-dev` code at commit `dc58155`, and each states what it bounds. `2e-13` on `temp` bounds the rewrite of `temp` by the virtual branch, $(E_0-L_{00}q_v)/C_v\ne T_0$ in the last bits, of order `5.7e-14` at these temperatures. `1e-15` on total water bounds the rounding of a sum of mixing ratios below `0.05`. The Newton-branch bounds are derived rather than calibrated: with $|\delta|<10^{-6}$ (Controlled Spec §3), `|qv - qs_final|` is at most $10^{-6}\,dq_s/dT$ and the energy drift at most $10^{-6}\,dq_s/dT\,(L_{00}+T(c_{vv}-c_l))$ to first order, and the suite's bounds carry a factor of 2 over both, for the second-order term and the difference of $dq_s/dT$ across the last correction.
- `cross_target_state_agreement` compares this target's run with every other target's certified variant of this node, case by case (`docs/TESTS.md` §Cross-target judgment): for one output `q` captured after the run and the same output `q_ref` of the same case in the other variant, it is `max|q - q_ref| / max(max|q|, max|q_ref|, 1)` over every element. The host evaluates it from the captured state; it is not a `diagnostics.json` field, and on a target with no other certified variant it holds vacuously. The saturation step's state is normative to the iterate (Controlled Spec §3), so the bound of `1e-12` holds across correct variants.

## 6. Test definitions
Every `pass` test also requires `guard_pass = 1` on each of its cases, and each test of `__saturation_adjustment` also `converged = 1`.

- `test_id`: `l0_qsat_reference_point_pass`
  - `level`: `L0`
  - `operation_id`: `microphysics_saturation_liquid_1d_column__dens2qsat_liq`
  - `expected_outcome`: `pass`
  - `judgment`: on `l0_qsat_ref`, where `temp = tem00` so that the saturation pressure is `psat0` exactly, `maxabs(interior*(qsat - psat0/(dens*rvap*tem00))) / maxabs(interior*psat0/(dens*rvap*tem00))` is `<= 1e-12` (`checks.qsat_reference`). The cross-target judgment is applied: `cross_target_state_agreement` over `qsat` is `<= 1e-12`.
- `test_id`: `l0_qsat_clausius_clapeyron_pass`
  - `level`: `L0`
  - `operation_id`: `microphysics_saturation_liquid_1d_column__dens2qsat_liq`
  - `expected_outcome`: `pass`
  - `judgment`: on `l0_qsat_ref`, `l0_qsat_temp_ramp` and `l0_qsat_dens_ramp`, `qsat` equals $q_s$ of Controlled Spec §3 at the case's `temp` and `dens` at every interior cell: `maxabs(interior*(qsat - qs)) / maxabs(interior*qs)` is `<= 1e-12` (`checks.qsat_formula`). The cross-target judgment is applied: `cross_target_state_agreement` over `qsat` is `<= 1e-12`.
- `test_id`: `l0_qsat_monotone_pass`
  - `level`: `L0`
  - `operation_id`: `microphysics_saturation_liquid_1d_column__dens2qsat_liq`
  - `expected_outcome`: `pass`
  - `judgment`: with `d = roll(qsat, -1) - qsat`, on `l0_qsat_temp_ramp` (`temp` increases with `k`, `dens` constant) `min(pairs*d + (1 - pairs))` is `> 0`, and on `l0_qsat_dens_ramp` (`dens` increases with `k`, `temp` constant) `max(pairs*d - (1 - pairs))` is `< 0` (`checks.qsat_monotone`). The cross-target judgment is applied: `cross_target_state_agreement` over `qsat` is `<= 1e-12`.
- `test_id`: `l0_qsat_halo_untouched_pass`
  - `level`: `L0`
  - `operation_id`: `microphysics_saturation_liquid_1d_column__dens2qsat_liq`
  - `expected_outcome`: `pass`
  - `judgment`: on `l0_qsat_ref`, `l0_qsat_temp_ramp` and `l0_qsat_dens_ramp`, `maxabs(halo*qsat)` is `== 0` (`checks.halo_untouched`). The cross-target judgment is applied: `cross_target_state_agreement` over `qsat` is `<= 1e-12`.
- `test_id`: `l0_satadj_dry_noop_pass`
  - `level`: `L0`
  - `operation_id`: `microphysics_saturation_liquid_1d_column__saturation_adjustment`
  - `expected_outcome`: `pass`
  - `judgment`: on `l0_satadj_dry` (`qc = 0`, `qv` below `qsat`), `qv`, `qc`, `cptot` and `cvtot` are unchanged exactly (`maxabs(final - initial)` `== 0` for each) and `maxabs(interior*rhoe_d)` is `== 0`; `maxabs(final.temp - initial.temp)` is `<= 2e-13`, because the virtual branch recomputes `temp` (`checks.satadj_dry_noop`). The cross-target judgment is applied: `cross_target_state_agreement` over each of `temp`, `qv`, `qc`, `cptot`, `cvtot` and `rhoe_d` is `<= 1e-12`.
- `test_id`: `l0_satadj_subsaturated_evaporates_all_pass`
  - `level`: `L0`
  - `operation_id`: `microphysics_saturation_liquid_1d_column__saturation_adjustment`
  - `expected_outcome`: `pass`
  - `judgment`: on `l0_satadj_subsat` (`qc > 0`, every interior cell ending below saturation), with `qsum = initial.qv + initial.qc`, `cvnew = initial.cvtot + initial.qc*(cvv - cl)`, `cpnew = initial.cptot + initial.qc*(cpvap - cl)` and `tstar = (initial.temp*initial.cvtot + initial.qv*L00 - L00*qsum) / cvnew`: `maxabs(final.qc)` over interior cells is `== 0`, and each of `final.qv` against `qsum`, `final.temp` against `tstar`, `final.cptot` against `cpnew` and `final.cvtot` against `cvnew` agrees within `1e-12` relative, `maxabs(interior*(final - reference)) / maxabs(interior*reference)` (`checks.satadj_evaporates_all`). The cross-target judgment is applied: `cross_target_state_agreement` over each of `temp`, `qv`, `qc`, `cptot`, `cvtot` and `rhoe_d` is `<= 1e-12`.
- `test_id`: `l0_satadj_conservation_pass`
  - `level`: `L0`
  - `operation_id`: `microphysics_saturation_liquid_1d_column__saturation_adjustment`
  - `expected_outcome`: `pass`
  - `judgment`: on `l0_satadj_dry`, `l0_satadj_subsat` and `l0_satadj_supersat`, `maxabs(interior*((final.qv + final.qc) - (initial.qv + initial.qc)))` is `<= 1e-15`. On the same cases, with `em = final.temp*final.cvtot + final.qv*L00` and `em0 = initial.temp*initial.cvtot + initial.qv*L00`, `max(interior*abs(em - em0) / (2e-6*dqs_final*(L00 + final.temp*(cvv - cl))))` is `<= 1`, the per-cell drift bound of §5 (`checks.satadj_conservation`). The cross-target judgment is applied: `cross_target_state_agreement` over each of `qv` and `qc` is `<= 1e-12`.
- `test_id`: `l0_satadj_saturated_state_pass`
  - `level`: `L0`
  - `operation_id`: `microphysics_saturation_liquid_1d_column__saturation_adjustment`
  - `expected_outcome`: `pass`
  - `judgment`: on `l0_satadj_supersat`: `max(interior*abs(final.qv - qs_final) / (2e-6*dqs_final))` is `<= 1`; `min(interior*final.qc)` is `>= 0`, and `qc` is positive at every interior cell; with `dqv = final.qv - initial.qv`, each of `rhoe_d` against `-L00*dqv*dens`, `final.cptot - initial.cptot` against `(cpvap - cl)*dqv` and `final.cvtot - initial.cvtot` against `(cvv - cl)*dqv` agrees within `1e-12` relative, `maxabs(interior*(left - right)) / maxabs(interior*right)` (`checks.satadj_saturated`). The cross-target judgment is applied: `cross_target_state_agreement` over each of `temp`, `qv`, `qc`, `cptot`, `cvtot` and `rhoe_d` is `<= 1e-12`.
- `test_id`: `l0_satadj_halo_untouched_pass`
  - `level`: `L0`
  - `operation_id`: `microphysics_saturation_liquid_1d_column__saturation_adjustment`
  - `expected_outcome`: `pass`
  - `judgment`: on `l0_satadj_dry`, `l0_satadj_subsat` and `l0_satadj_supersat`, `maxabs(halo*(final - initial))` is `== 0` for each of `temp`, `qv`, `qc`, `cptot` and `cvtot`, and `maxabs(halo*rhoe_d)` is `== 0` (`checks.halo_untouched`). The cross-target judgment is applied: `cross_target_state_agreement` over each of `temp`, `qv`, `qc`, `cptot`, `cvtot` and `rhoe_d` is `<= 1e-12`.
- `test_id`: `l0_dens_nonpositive_xfail`
  - `level`: `L0`
  - `operation_id`: `microphysics_saturation_liquid_1d_column__dens2qsat_liq`
  - `expected_outcome`: `xfail`
  - `case`: `l0_dens_nonpositive`
  - `xfail_condition`: `dens(4) <= 0`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`
  - `judgment`: `guard_pass` is `0`. The output is not defined on a refused call (Controlled Spec §4), so the flag alone is judged.
- `test_id`: `l0_temp_nonpositive_xfail`
  - `level`: `L0`
  - `operation_id`: `microphysics_saturation_liquid_1d_column__dens2qsat_liq`
  - `expected_outcome`: `xfail`
  - `case`: `l0_temp_nonpositive`
  - `xfail_condition`: `temp(4) <= 0`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`
  - `judgment`: `guard_pass` is `0`.
- `test_id`: `l0_bad_range_xfail`
  - `level`: `L0`
  - `operation_id`: `microphysics_saturation_liquid_1d_column__dens2qsat_liq`
  - `expected_outcome`: `xfail`
  - `case`: `l0_bad_range`
  - `xfail_condition`: `ks > ke`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`
  - `judgment`: `guard_pass` is `0`.
- `test_id`: `l0_satadj_dens_nonpositive_xfail`
  - `level`: `L0`
  - `operation_id`: `microphysics_saturation_liquid_1d_column__saturation_adjustment`
  - `expected_outcome`: `xfail`
  - `case`: `l0_satadj_dens_nonpositive`
  - `xfail_condition`: `dens(4) <= 0`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`
  - `judgment`: `guard_pass` is `0`, and each input-updated array holds its input at every cell: `maxabs(final - initial)` is `== 0` for each of `temp`, `qv`, `qc`, `cptot` and `cvtot`.
- `test_id`: `l0_satadj_temp_nonpositive_xfail`
  - `level`: `L0`
  - `operation_id`: `microphysics_saturation_liquid_1d_column__saturation_adjustment`
  - `expected_outcome`: `xfail`
  - `case`: `l0_satadj_temp_nonpositive`
  - `xfail_condition`: `temp(4) <= 0`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`
  - `judgment`: as `l0_satadj_dens_nonpositive_xfail`.
- `test_id`: `l0_satadj_cvtot_nonpositive_xfail`
  - `level`: `L0`
  - `operation_id`: `microphysics_saturation_liquid_1d_column__saturation_adjustment`
  - `expected_outcome`: `xfail`
  - `case`: `l0_satadj_cvtot_nonpositive`
  - `xfail_condition`: `cvtot(4) <= 0`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`
  - `judgment`: as `l0_satadj_dens_nonpositive_xfail`.
- `test_id`: `l0_satadj_bad_range_xfail`
  - `level`: `L0`
  - `operation_id`: `microphysics_saturation_liquid_1d_column__saturation_adjustment`
  - `expected_outcome`: `xfail`
  - `case`: `l0_satadj_bad_range`
  - `xfail_condition`: `ks > ke`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`
  - `judgment`: as `l0_satadj_dens_nonpositive_xfail`.
- `test_id`: `l0_satadj_nonconverged_xfail`
  - `level`: `L0`
  - `operation_id`: `microphysics_saturation_liquid_1d_column__saturation_adjustment`
  - `expected_outcome`: `xfail`
  - `case`: `l0_satadj_nonconverged`
  - `xfail_condition`: `qsat_ratio >= 100`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'converged'`
  - `judgment`: `guard_pass` is `1` and `converged` is `0`. Cell 4 does not converge; the other five cells are unsaturated and take the virtual branch. Over every cell: `qv`, `qc`, `cptot` and `cvtot` hold their inputs exactly, `maxabs(interior*rhoe_d)` is `== 0`, and `maxabs(final.temp - initial.temp)` is `<= 2e-13`, so every published value is finite and the column's other cells were still evaluated.

## 7. Pass/fail aggregation rules
- `per_test.pass_rule`: `pass` when the judgment expression is satisfied.
- `per_test.xfail_rule`: `xfail` when `xfail_condition` is true and `pass_when` is satisfied.
- `suite.pass_rule`: `pass` when all `test_id` are `pass` or `xfail`.

## 8. Traceability
- Record `test_profile_id` and `test_profile_version` in `trial_meta.json`.
