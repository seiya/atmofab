# Tests: liquid-water saturation and saturation adjustment of a column (L0)

## 0. Meta information
- `test_profile_id`: `microphysics_saturation_liquid_1d_column_l0`
- `test_profile_version`: `0.2.0`
- `status`: `draft`
- `spec_ref.spec_kind`: `component`
- `spec_ref.spec_id`: `microphysics_saturation_liquid_1d_column`
- `spec_ref.spec_version`: `0.1.0`
- `spec_ref.controlled_spec_path`: `spec/component/microphysics/saturation/microphysics_saturation_liquid_1d_column/controlled_spec.md`

## 1. Test purpose
This suite verifies the two published `operation`s at `L0`. For `__dens2qsat_liq`: the reference point, the closed form, and the monotonicity in temperature and in density. For `__saturation_adjustment`: the unsaturated no-op, the complete evaporation of cloud water, the conservation of total water and of moist internal energy, the saturated state of the Newton branch and the energy release. For each `operation`: the halo contract and the input guard; for `__saturation_adjustment` also the finite refusal state of a cell that does not converge.

The symmetry pairs of `docs/TESTS.md` §Symmetry pairs are not judged. A one-dimensional column has no axis exchange. A reversal or a translation of the column is a pointwise identity: each `operation` couples no two cells, so the transformed case compares each cell's computation with itself. The CFL number is `N/A`: the `operation`s have no transport and no time step. The comparison with theory is the closed-form judgments of §6.

## 2. Input-defaulting rules
The block below is the suite's whole input. Every case has `ka=8`, and every case without a `grid` entry passes `ks=2`, `ke=7`, so the first and the last cell are the halo.

- `constants` are the values of the constant arguments of Controlled Spec §2 in every case.
- `setup` is each state variable's value at every cell before a case's own rules apply. A case's `columns` entry replaces the interior cells: `[a, b]` gives `a + b*(k-2)` at cell `k`, and `{cells: [...]}` gives the six interior values literally. A variable a case names no rule for keeps its `setup` value at every cell: the halo sentinels of `temp`, `dens`, `qv`, `qc`, `cptot`, `cvtot` are positive, so that a host expression over a whole column stays finite, and `qsat`, `rhoe_d` hold `-999`.
- In a case with a `qv` rule, the interior cells of `cptot` and `cvtot` are the moist-air mixture over the case's `qv` and `qc`, with `air` the dry-air constants: $q_d=1-q_v-q_c$, `cptot` $=q_d\,$`cpdry`$+$`cpvap`$\,q_v+$`cl`$\,q_c$, `cvtot` $=q_d($`cpdry`$-$`rdry`$)+($`cpvap`$-$`rvap`$)q_v+$`cl`$\,q_c$.
- `overrides` set one cell after every other rule, and are what force the guard cases.
- `operation` names the published `operation` a case calls and `expected` the outcome §6 states. `refusal` states the flags a refused case's run leaves: `guard_pass = 0` where it is empty, and otherwise the values it lists. `nonconvergence_check` marks the case whose cell 4 does not converge.
- `grid`, on an index-range case, replaces the `ks`, `ke` passed to the `operation` after the state is built with `ks=2`, `ke=7`.
- `l0_satadj_nonconverged` has at cell 4 the literal `qv` of `0.66357706133254214`, at `temp=277.8`, `dens=1`, `qc=0`. Non-convergence is chaotic in `qv`: at this `temp` and `dens` the multiples of $q_s$ that end the iteration without a finite `temp` include 65, 67, 90, 99 to 121 and 150, and those that converge include 60, 68 to 80, 95, 125 and 130, so no threshold on `qv` states it; none of 2,000 values within `0.1%` of the literal converges. The case is this literal; it stayed non-converged in 20,000 trials with `temp` jittered by `1e-14`, `qv` by `4e-16` and $q_s$ by `1e-15`, all relative.

No case sits on the boundary between the two branches of Controlled Spec §3, where a change of the last bit changes the branch: at every interior cell of every `pass` case, $q_\Sigma$ and $q_s$ at the virtual temperature differ by at least `4.9%` of $q_s$, at cell 2 of `l0_satadj_branch`, and by at least `66%` on the other cases, against a last bit of about `1e-16` relative.

- `l0_satadj_branch` holds two kinds of state on the Newton branch: cells 2 to 4 have $q_s(T^*)<q_\Sigma\le q_s(T_0)$, with $T^*$ the virtual temperature, and cells 5 to 7 have $q_{v0}<q_s(T_0)<q_\Sigma$. Each takes the Newton branch and ends with `qc > 0`.
- `l0_satadj_slow` holds states whose iteration takes 6 iterations (cells 2 to 4), 85 (cell 5), 16 (cell 6) and 32 (cell 7). Cell 7 is not a physical solution: the iteration converges at `temp` near `5425`, with `qv` above `1` and `qc` near `-0.455`. It pins the definition of Controlled Spec §3, which has no protection against such a root and no clip; `qc` of cells 5 and 6 is positive. `l0_satadj_limit` holds at cell 4 a state that reaches the 100th iteration with a finite `temp`, which is not converged. Each count stayed the same in 20,000 trials under the jitter of `l0_satadj_nonconverged`.
- `alt_constants` replaces the values of `constants` it names on a case with `constants: alt`. The values are not physical; each is a few percent from the default.

```yaml
grid: {ka: 8, ks: 2, ke: 7}
constants: {rvap: 461.50, cpvap: 1846.0, cl: 4218.0, lhv0: 2.501e+6, psat0: 610.78, tem00: 273.15}
alt_constants: {rvap: 480.0, cpvap: 1900.0, cl: 4100.0, lhv0: 2.4e+6, psat0: 600.0, tem00: 270.0}
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
- {case_id: l0_satadj_nonconverged, operation: saturation_adjustment, expected: xfail, refusal: {guard_pass: 1.0, converged: 0.0}, nonconvergence_check: true, columns: {temp: [277.8, 0.0], dens: [1.0, 0.0], qv: {cells: [0.001, 0.001, 0.66357706133254214, 0.009, 0.001, 0.012]}, qc: {cells: [0.0, 5.0e-4, 0.0, 1.0e-3, 5.0e-4, 1.0e-3]}}}
- {case_id: l0_satadj_branch, operation: saturation_adjustment, expected: pass, columns: {temp: [285.0, 0.0], dens: [1.0, 0.0], qv: {cells: [0.0095, 0.0095, 0.0095, 0.0100, 0.0102, 0.0104]}, qc: {cells: [5.0e-4, 6.0e-4, 7.0e-4, 1.5e-3, 1.5e-3, 1.5e-3]}}}
- {case_id: l0_satadj_slow, operation: saturation_adjustment, expected: pass, columns: {temp: {cells: [243.8, 243.8, 243.8, 294.848, 289.937, 290.097]}, dens: {cells: [0.799, 0.799, 0.799, 1.148, 1.224, 1.039]}, qv: {cells: [0.0185, 0.0185, 0.0185, 0.62658, 0.45295, 0.54852]}, qc: {cells: [0.006, 0.006, 0.006, 0.0, 0.0, 0.0]}}}
- {case_id: l0_satadj_limit, operation: saturation_adjustment, expected: xfail, refusal: {guard_pass: 1.0, converged: 0.0}, nonconvergence_check: true, columns: {temp: {cells: [280.0, 280.0, 279.2327, 280.0, 280.0, 280.0]}, dens: {cells: [1.0, 1.0, 0.88584, 1.0, 1.0, 1.0]}, qv: {cells: [0.001, 0.001, 0.45708, 0.001, 0.001, 0.001]}, qc: [0.0, 0.0]}}
- {case_id: l0_satadj_cptot_nonpositive, operation: saturation_adjustment, expected: xfail, refusal: {}, columns: {temp: [285.0, 3.0], dens: [1.05, -0.05], qv: [0.002, 3.0e-4], qc: [5.0e-4, 1.0e-4]}, overrides: [{var: cptot, k: 4, value: 0.0}]}
- {case_id: l0_ks_below_one, operation: dens2qsat_liq, expected: xfail, refusal: {}, grid: {ks: 0, ke: 7}, columns: {temp: [280.0, 4.0], dens: [1.1, -0.05]}}
- {case_id: l0_ke_above_ka, operation: dens2qsat_liq, expected: xfail, refusal: {}, grid: {ks: 2, ke: 9}, columns: {temp: [280.0, 4.0], dens: [1.1, -0.05]}}
- {case_id: l0_satadj_ks_below_one, operation: saturation_adjustment, expected: xfail, refusal: {}, grid: {ks: 0, ke: 7}, columns: {temp: [285.0, 3.0], dens: [1.05, -0.05], qv: [0.002, 3.0e-4], qc: [5.0e-4, 1.0e-4]}}
- {case_id: l0_satadj_ke_above_ka, operation: saturation_adjustment, expected: xfail, refusal: {}, grid: {ks: 2, ke: 9}, columns: {temp: [285.0, 3.0], dens: [1.05, -0.05], qv: [0.002, 3.0e-4], qc: [5.0e-4, 1.0e-4]}}
- {case_id: l0_qsat_alt, operation: dens2qsat_liq, expected: pass, constants: alt, columns: {temp: [260.0, 8.0], dens: [1.2, -0.1]}}
- {case_id: l0_satadj_alt_subsat, operation: saturation_adjustment, expected: pass, constants: alt, columns: {temp: [285.0, 3.0], dens: [1.05, -0.05], qv: [0.002, 3.0e-4], qc: [5.0e-4, 1.0e-4]}}
- {case_id: l0_satadj_alt_supersat, operation: saturation_adjustment, expected: pass, constants: alt, columns: {temp: [280.0, 3.0], dens: [1.1, -0.05], qv: [0.012, 0.004], qc: [1.0e-3, 2.0e-4]}}
```

## 3. Execution-control rules
`N/A`: each `operation` is a single evaluation of one column and defines no time-stepping. The iteration of `__saturation_adjustment` is part of its definition (Controlled Spec §3), and the suite controls no part of it.

## 4. Case-expansion rules
`N/A`: the `L0` suite uses the fixed cases of §2 and defines no `case` sweep.

## 5. Judged quantities
A test is judged by the host's predicates over the captures alone (`docs/TESTS.md` requirement 9). Each quantity below is named on the `quantities` line of the tests that judge it, and is evaluated from the state captured after the run in each of the test's target cases; a name `initial.<variable>` in an expression of §5 or §6 reads the capture after setup, and `final.<variable>` or a bare variable name the capture after the run.

The state of a case, captured after setup and after the run, consists of `temp`, `dens`, `qv`, `qc`, `cptot`, `cvtot`, `qsat`, `rhoe_d` (each with extent `ka`, at the §2 values at setup, so `qsat` and `rhoe_d` hold the sentinel `-999` and the halo sentinels are those of §2) and the scalars `guard_pass` and `converged`.

On a case of `__dens2qsat_liq`, `temp` and `dens` are input arguments of the `operation` and `qsat` its output argument. On a case of `__saturation_adjustment`, `dens` is an input argument, `temp`, `qv`, `qc`, `cptot` and `cvtot` are arguments updated in place, and `rhoe_d` is an output argument. Every variable an `operation` does not publish keeps its setup value. `guard_pass` and `converged` are each a `record` the checks module writes of the `operation`'s logical output of that name, recorded as a number because a state variable is real-valued: `-1` at setup, before the `operation` is called; `1` when the call returned true; `0` when it returned false. `guard_pass` says whether the input guard accepted the call; `converged` says whether every interior cell's iteration converged. A case of `__dens2qsat_liq` leaves `converged` at `-1`, because that `operation` has no such output.

- A judgment reads a halo cell only through the masks below. With $i=k-1$ and $c=i+1/2$ the cell-centre coordinate of a unit-length cell, `coordinates[]` axis `0`, `count` and `length` `ka`, placement `cell_center`: `interior` $=\min(1,\max(0,\lfloor c\rfloor-(ks-2)))\cdot\min(1,\max(0,ke-\lfloor c\rfloor))$ and `halo` $=1-$`interior`. `pairs` $=\min(1,\max(0,\lfloor c\rfloor-(ks-2)))\cdot\min(1,\max(0,ke-1-\lfloor c\rfloor))$ marks the cells $k=ks..ke-1$, whose next cell is interior.
- The names a judgment reads, each from the case's `constants`: `cvv = cpvap - rvap`, `L00 = lhv0 - (cpvap - cl)*tem00`. For a temperature `T` and a density `rho`, `qs(T, rho) = psat0*exp(log(T/tem00)*(cpvap - cl)/rvap + L00/rvap*(1/tem00 - 1/T))/(rho*rvap*T)` and `dqs(T, rho) = qs(T, rho)/T*((lhv0 + (cpvap - cl)*(T - tem00))/(rvap*T) - 1)`. `qs_final = qs(final.temp, final.dens)` and `dqs_final = dqs(final.temp, final.dens)`, over every cell (the halo sentinels keep them finite). `qs = qs(initial.temp, initial.dens)`.
- `idx` is $\lfloor c\rfloor$. Cell masks of `l0_satadj_nonconverged` and `l0_satadj_limit`: `at4 = max(0, 1 - abs(idx - 3))`, `m36 = max(0, 1 - abs(idx - 2)) + max(0, 1 - abs(idx - 5))`, `m57 = max(0, 1 - abs(idx - 4)) + max(0, 1 - abs(idx - 6))`.
- The round-off bounds of §6 are offline calibrations against an independent implementation of the scheme, the original `scale-met/scale-dev` code at commit `dc58155`, and each states what it bounds. `2e-13` on `temp` bounds the rewrite of `temp` by the virtual branch, $(E_0-L_{00}q_v)/C_v\ne T_0$ in the last bits, of order `5.7e-14` at these temperatures. `1e-15` on total water bounds the rounding of a sum of mixing ratios, at most about `0.63` on `l0_satadj_slow`, whose last bit is `1.1e-16`. The Newton-branch bounds are derived rather than calibrated: with $|\delta|<10^{-6}$ (Controlled Spec §3), `|qv - qs_final|` is at most $10^{-6}\,|dq_s/dT|$ and the energy drift at most $10^{-6}\,|dq_s/dT\,(L_{00}+T(c_{vv}-c_l))|$ to first order, in magnitude because $dq_s/dT$ is negative at a `temp` as high as that of cell 7 of `l0_satadj_slow`, and the suite's bounds carry a factor of 2 over both, for the second-order term and the difference of $dq_s/dT$ across the last correction.
- `drift_ratio` compares two consequences of the last Newton correction $\delta$ (Controlled Spec §3 of the saturation node). The state pairs $T=T^{it}-\delta$ with $q_s(T^{it})$, so `final.qv - qs_final` is $\delta\,q_s'$ to first order in $\delta$, and the energy drift is $\delta\,(q_s'(L_{00}+T^{it}(c_{vv}-c_l))+C_v^{prev}-C_v)$. Their quotient differs from 1 by the terms in $\delta$, by $(C_v^{prev}-C_v)/(q_s'(L_{00}+T(c_{vv}-c_l)))$, which is $(c_{vv}-c_l)/(L_{00}+T(c_{vv}-c_l))$, about $1.2\times10^{-3}$ per kelvin, times the correction before the last, and by the rounding of the drift over $\delta\,q_s'(L_{00}+T(c_{vv}-c_l))$.  The bound `1e-3` is a calibration, not a derived limit. The mask `pairs` leaves out cell 7 of the case, whose last correction of `2.3e-10 K` makes the rounding term as large as `2.3e-4`, measured; the cells it keeps have last corrections from `3e-8 K` to `3.3e-7 K` and measure below `1e-5`. Where `final.qv` equals `qs_final` the denominator is zero and the expression is not evaluable, which the host records as a structural failure and so a failed test. The definition does not reach that on this case: its `final.qv - qs_final` is at least `1.8e-11` in magnitude at every cell the mask keeps, against a last bit of about `3e-18`.
- The iterate order of Controlled Spec §3 is read through `drift_ratio` on five cells of one case, and across targets through `cross_target_state_agreement`.
- `input_guard`: the captured `guard_pass`, `== 1` on the cases of a `pass` test and `== 0` on the cases of a refused test whose `xfail_condition` is a guard clause. On a refused case of `__saturation_adjustment` it is also `maxabs(final - initial)` for each of `temp`, `qv`, `qc`, `cptot` and `cvtot`, `== 0`: each input-updated array holds its input at every cell.
- `converged`: the captured `converged`, `== 1` on the cases of a `pass` test of `__saturation_adjustment`. On `l0_satadj_nonconverged` and `l0_satadj_limit` it is `== 0`, with `guard_pass == 1`, and with `maxabs(at4*(final - initial))` for each of `temp`, `qv`, `qc`, `cptot` and `cvtot` and `maxabs(at4*rhoe_d)`, each `== 0`: the cell that did not converge holds its inputs.
- `qsat_reference`: with `ref = psat0/(dens*rvap*tem00)`, `maxabs(interior*(qsat - ref)) / maxabs(interior*ref)`.
- `qsat_formula`: `maxabs(interior*(qsat - qs)) / maxabs(interior*qs)`.
- `qsat_monotone`: with `d = roll(qsat, -1) - qsat`, `min(pairs*d + (1 - pairs))` on a case whose `temp` increases with `k`, and `max(pairs*d - (1 - pairs))` on a case whose `dens` increases with `k`.
- `halo_untouched`: on a case of `__dens2qsat_liq`, `maxabs(halo*qsat)`. On a case of `__saturation_adjustment`, `maxabs(halo*(final - initial))` for each of `temp`, `qv`, `qc`, `cptot` and `cvtot`, and `maxabs(halo*rhoe_d)`.
- `satadj_dry_noop`: `maxabs(final - initial)` for each of `qv`, `qc`, `cptot`, `cvtot` and `temp`, and `maxabs(interior*rhoe_d)`.
- `satadj_evaporates_all`: with `qsum`, `cvnew`, `cpnew` and `tstar` as `l0_satadj_subsaturated_evaporates_all_pass` states them, `maxabs(interior*final.qc)`, and `maxabs(interior*(final - reference)) / maxabs(interior*reference)` for `final.qv` against `qsum`, `final.temp` against `tstar`, `final.cptot` against `cpnew`, `final.cvtot` against `cvnew` and `rhoe_d` against `-L00*(final.qv - initial.qv)*dens`.
- `satadj_conservation`: `maxabs(interior*((final.qv + final.qc) - (initial.qv + initial.qc)))`, and, with `em` and `em0` as `l0_satadj_conservation_pass` states them, the per-cell drift `max(interior*abs(em - em0) / abs(2e-6*dqs_final*(L00 + final.temp*(cvv - cl))))`.
- `satadj_saturated`: `max(interior*abs(final.qv - qs_final) / (2e-6*abs(dqs_final)))`, `max(pairs*abs(drift_ratio - 1))`, `min(interior*final.qc)` and `min(interior*final.qc + (1 - interior))` (the latter `> 0`, the positivity of `qc` at every interior cell), and, with `dqv = final.qv - initial.qv`, `maxabs(interior*(left - right)) / maxabs(interior*right)` for `rhoe_d` against `-L00*dqv*dens`, `final.cptot - initial.cptot` against `(cpvap - cl)*dqv` and `final.cvtot - initial.cvtot` against `(cvv - cl)*dqv`.
- `nonconverged_neighbours`: on `l0_satadj_nonconverged`, `maxabs(m36*final.qc)`, `max(m57*abs(final.qv - qs_final)/(2e-6*abs(dqs_final)))`, `min(m57*final.qc + (1 - m57))`, and `maxabs(interior*(1 - at4)*((final.qv + final.qc) - (initial.qv + initial.qc)))`.
- `cross_target_state_agreement`: this target's run compared with every other target's certified variant of this node, case by case (`docs/TESTS.md` §Cross-target judgment): for one output `q` captured after the run and the same output `q_ref` of the same case in the other variant, `max|q - q_ref| / max(max|q|, max|q_ref|, 1)` over every element. The host evaluates it from the captured state; on a target with no other certified variant it holds vacuously. The saturation step's state is normative to the iterate (Controlled Spec §3), so the bound of `1e-12` holds across correct variants. It is outside the `quantities` line.

## 6. Test definitions
Every `pass` test also requires `guard_pass == 1` (`input_guard`) on each of its cases, and each `pass` test of `__saturation_adjustment` also `converged == 1` (`converged`).

- `test_id`: `l0_qsat_reference_point_pass`
  - `level`: `L0`
  - `operation_id`: `microphysics_saturation_liquid_1d_column__dens2qsat_liq`
  - `expected_outcome`: `pass`
  - `quantities`: `input_guard`, `qsat_reference`
  - `judgment`: on `l0_qsat_ref`, where `temp = tem00` so that the saturation pressure is `psat0` exactly, with `ref = psat0/(dens*rvap*tem00)`, `maxabs(interior*(qsat - ref)) / maxabs(interior*ref)` is `<= 1e-12` (`qsat_reference <= 1e-12`), and `guard_pass == 1` (`input_guard`). The cross-target judgment is applied: `cross_target_state_agreement` over `qsat` is `<= 1e-12`.
- `test_id`: `l0_qsat_clausius_clapeyron_pass`
  - `level`: `L0`
  - `operation_id`: `microphysics_saturation_liquid_1d_column__dens2qsat_liq`
  - `expected_outcome`: `pass`
  - `quantities`: `input_guard`, `qsat_formula`
  - `judgment`: on `l0_qsat_ref`, `l0_qsat_temp_ramp`, `l0_qsat_dens_ramp` and `l0_qsat_alt`, `qsat` equals $q_s$ of Controlled Spec §3 at the case's `temp` and `dens` at every interior cell: `maxabs(interior*(qsat - qs)) / maxabs(interior*qs)` is `<= 1e-12` (`qsat_formula <= 1e-12`), and `guard_pass == 1` (`input_guard`). The cross-target judgment is applied: `cross_target_state_agreement` over `qsat` is `<= 1e-12`.
- `test_id`: `l0_qsat_monotone_pass`
  - `level`: `L0`
  - `operation_id`: `microphysics_saturation_liquid_1d_column__dens2qsat_liq`
  - `expected_outcome`: `pass`
  - `quantities`: `input_guard`, `qsat_monotone`
  - `judgment`: with `d = roll(qsat, -1) - qsat`, on `l0_qsat_temp_ramp` (`temp` increases with `k`, `dens` constant) `min(pairs*d + (1 - pairs))` is `> 0`, and on `l0_qsat_dens_ramp` (`dens` increases with `k`, `temp` constant) `max(pairs*d - (1 - pairs))` is `< 0` (`qsat_monotone`, each with its bound), and `guard_pass == 1` (`input_guard`). The cross-target judgment is applied: `cross_target_state_agreement` over `qsat` is `<= 1e-12`.
- `test_id`: `l0_qsat_halo_untouched_pass`
  - `level`: `L0`
  - `operation_id`: `microphysics_saturation_liquid_1d_column__dens2qsat_liq`
  - `expected_outcome`: `pass`
  - `quantities`: `halo_untouched`, `input_guard`
  - `judgment`: on every `pass` case of `__dens2qsat_liq`, `maxabs(halo*qsat)` is `== 0` (`halo_untouched == 0`), and `guard_pass == 1` (`input_guard`). The cross-target judgment is applied: `cross_target_state_agreement` over `qsat` is `<= 1e-12`.
- `test_id`: `l0_satadj_dry_noop_pass`
  - `level`: `L0`
  - `operation_id`: `microphysics_saturation_liquid_1d_column__saturation_adjustment`
  - `expected_outcome`: `pass`
  - `quantities`: `converged`, `input_guard`, `satadj_dry_noop`
  - `judgment`: on `l0_satadj_dry` (`qc = 0`, `qv` below `qsat`), `qv`, `qc`, `cptot` and `cvtot` are unchanged exactly (`maxabs(final - initial)` `== 0` for each) and `maxabs(interior*rhoe_d)` is `== 0`; `maxabs(final.temp - initial.temp)` is `<= 2e-13`, because the virtual branch recomputes `temp` (`satadj_dry_noop`, each with its bound), and `guard_pass == 1` (`input_guard`) and `converged == 1` (`converged`). The cross-target judgment is applied: `cross_target_state_agreement` over each of `temp`, `qv`, `qc`, `cptot`, `cvtot` and `rhoe_d` is `<= 1e-12`.
- `test_id`: `l0_satadj_subsaturated_evaporates_all_pass`
  - `level`: `L0`
  - `operation_id`: `microphysics_saturation_liquid_1d_column__saturation_adjustment`
  - `expected_outcome`: `pass`
  - `quantities`: `converged`, `input_guard`, `satadj_evaporates_all`
  - `judgment`: on `l0_satadj_subsat` and `l0_satadj_alt_subsat` (`qc > 0`, every interior cell ending below saturation), with `qsum = initial.qv + initial.qc`, `cvnew = initial.cvtot + initial.qc*(cvv - cl)`, `cpnew = initial.cptot + initial.qc*(cpvap - cl)` and `tstar = (initial.temp*initial.cvtot + initial.qv*L00 - L00*qsum) / cvnew`: `maxabs(final.qc)` over interior cells is `== 0`, and each of `final.qv` against `qsum`, `final.temp` against `tstar`, `final.cptot` against `cpnew` and `final.cvtot` against `cvnew` and `rhoe_d` against `-L00*(final.qv - initial.qv)*dens` agrees within `1e-12` relative, `maxabs(interior*(final - reference)) / maxabs(interior*reference)` (`satadj_evaporates_all`, each with its bound), and `guard_pass == 1` (`input_guard`) and `converged == 1` (`converged`). The cross-target judgment is applied: `cross_target_state_agreement` over each of `temp`, `qv`, `qc`, `cptot`, `cvtot` and `rhoe_d` is `<= 1e-12`.
- `test_id`: `l0_satadj_conservation_pass`
  - `level`: `L0`
  - `operation_id`: `microphysics_saturation_liquid_1d_column__saturation_adjustment`
  - `expected_outcome`: `pass`
  - `quantities`: `converged`, `input_guard`, `satadj_conservation`
  - `judgment`: on `l0_satadj_dry`, `l0_satadj_subsat`, `l0_satadj_supersat`, `l0_satadj_branch`, `l0_satadj_slow` and `l0_satadj_alt_supersat`, `maxabs(interior*((final.qv + final.qc) - (initial.qv + initial.qc)))` is `<= 1e-15`. On the same cases, with `em = final.temp*final.cvtot + final.qv*L00` and `em0 = initial.temp*initial.cvtot + initial.qv*L00`, `max(interior*abs(em - em0) / abs(2e-6*dqs_final*(L00 + final.temp*(cvv - cl))))` is `<= 1`, the per-cell drift bound of §5 (`satadj_conservation`, each with its bound), and `guard_pass == 1` (`input_guard`) and `converged == 1` (`converged`). The cross-target judgment is applied: `cross_target_state_agreement` over each of `qv` and `qc` is `<= 1e-12`.
- `test_id`: `l0_satadj_saturated_state_pass`
  - `level`: `L0`
  - `operation_id`: `microphysics_saturation_liquid_1d_column__saturation_adjustment`
  - `expected_outcome`: `pass`
  - `quantities`: `converged`, `input_guard`, `satadj_saturated`
  - `judgment`: on `l0_satadj_supersat`, `l0_satadj_branch`, `l0_satadj_slow` and `l0_satadj_alt_supersat`: `max(interior*abs(final.qv - qs_final) / (2e-6*abs(dqs_final)))` is `<= 1`; with `em` and `em0` as in `l0_satadj_conservation_pass`, `drift_ratio = (em - em0) / ((final.qv - qs_final)*(L00 + final.temp*(cvv - cl)))` and, on `l0_satadj_supersat`, `max(pairs*abs(drift_ratio - 1))` is `<= 1e-3` (§5); on `l0_satadj_supersat` and `l0_satadj_branch`, `min(interior*final.qc)` is `>= 0`, and `qc` is positive at every interior cell; the sign of `qc` is not judged on `l0_satadj_slow` (§2); with `dqv = final.qv - initial.qv`, each of `rhoe_d` against `-L00*dqv*dens`, `final.cptot - initial.cptot` against `(cpvap - cl)*dqv` and `final.cvtot - initial.cvtot` against `(cvv - cl)*dqv` agrees within `1e-12` relative, `maxabs(interior*(left - right)) / maxabs(interior*right)` (`satadj_saturated`, each with its bound), and `guard_pass == 1` (`input_guard`) and `converged == 1` (`converged`). The cross-target judgment is applied: `cross_target_state_agreement` over each of `temp`, `qv`, `qc`, `cptot`, `cvtot` and `rhoe_d` is `<= 1e-12`.
- `test_id`: `l0_satadj_halo_untouched_pass`
  - `level`: `L0`
  - `operation_id`: `microphysics_saturation_liquid_1d_column__saturation_adjustment`
  - `expected_outcome`: `pass`
  - `quantities`: `converged`, `halo_untouched`, `input_guard`
  - `judgment`: on every `pass` case of `__saturation_adjustment`, `maxabs(halo*(final - initial))` is `== 0` for each of `temp`, `qv`, `qc`, `cptot` and `cvtot`, and `maxabs(halo*rhoe_d)` is `== 0` (`halo_untouched == 0`), and `guard_pass == 1` (`input_guard`) and `converged == 1` (`converged`). The cross-target judgment is applied: `cross_target_state_agreement` over each of `temp`, `qv`, `qc`, `cptot`, `cvtot` and `rhoe_d` is `<= 1e-12`.
- `test_id`: `l0_dens_nonpositive_xfail`
  - `level`: `L0`
  - `operation_id`: `microphysics_saturation_liquid_1d_column__dens2qsat_liq`
  - `expected_outcome`: `xfail`
  - `case`: `l0_dens_nonpositive`
  - `xfail_condition`: `dens(4) <= 0`
  - `quantities`: `input_guard`
  - `judgment`: on `l0_dens_nonpositive`, the `operation` refuses the input: `guard_pass == 0` (`input_guard`), in the failure direction. The output is not defined on a refused call (Controlled Spec §4), so the flag alone is judged. The cross-target judgment is not applied.
- `test_id`: `l0_temp_nonpositive_xfail`
  - `level`: `L0`
  - `operation_id`: `microphysics_saturation_liquid_1d_column__dens2qsat_liq`
  - `expected_outcome`: `xfail`
  - `case`: `l0_temp_nonpositive`
  - `xfail_condition`: `temp(4) <= 0`
  - `quantities`: `input_guard`
  - `judgment`: on `l0_temp_nonpositive`, the `operation` refuses the input: `guard_pass == 0` (`input_guard`), in the failure direction. The cross-target judgment is not applied.
- `test_id`: `l0_bad_range_xfail`
  - `level`: `L0`
  - `operation_id`: `microphysics_saturation_liquid_1d_column__dens2qsat_liq`
  - `expected_outcome`: `xfail`
  - `case`: `l0_bad_range`
  - `xfail_condition`: `ks > ke`
  - `quantities`: `input_guard`
  - `judgment`: on `l0_bad_range`, the `operation` refuses the input: `guard_pass == 0` (`input_guard`), in the failure direction. The cross-target judgment is not applied.
- `test_id`: `l0_satadj_dens_nonpositive_xfail`
  - `level`: `L0`
  - `operation_id`: `microphysics_saturation_liquid_1d_column__saturation_adjustment`
  - `expected_outcome`: `xfail`
  - `case`: `l0_satadj_dens_nonpositive`
  - `xfail_condition`: `dens(4) <= 0`
  - `quantities`: `input_guard`
  - `judgment`: on `l0_satadj_dens_nonpositive`, the `operation` refuses the input: `guard_pass == 0` (`input_guard`), in the failure direction. Each input-updated array holds its input at every cell: `maxabs(final - initial)` is `== 0` for each of `temp`, `qv`, `qc`, `cptot` and `cvtot` (`input_guard`). The cross-target judgment is not applied.
- `test_id`: `l0_satadj_temp_nonpositive_xfail`
  - `level`: `L0`
  - `operation_id`: `microphysics_saturation_liquid_1d_column__saturation_adjustment`
  - `expected_outcome`: `xfail`
  - `case`: `l0_satadj_temp_nonpositive`
  - `xfail_condition`: `temp(4) <= 0`
  - `quantities`: `input_guard`
  - `judgment`: on `l0_satadj_temp_nonpositive`, the `operation` refuses the input: `guard_pass == 0` (`input_guard`), in the failure direction. Each input-updated array holds its input at every cell: `maxabs(final - initial)` is `== 0` for each of `temp`, `qv`, `qc`, `cptot` and `cvtot` (`input_guard`). The cross-target judgment is not applied.
- `test_id`: `l0_satadj_cvtot_nonpositive_xfail`
  - `level`: `L0`
  - `operation_id`: `microphysics_saturation_liquid_1d_column__saturation_adjustment`
  - `expected_outcome`: `xfail`
  - `case`: `l0_satadj_cvtot_nonpositive`
  - `xfail_condition`: `cvtot(4) <= 0`
  - `quantities`: `input_guard`
  - `judgment`: on `l0_satadj_cvtot_nonpositive`, the `operation` refuses the input: `guard_pass == 0` (`input_guard`), in the failure direction. Each input-updated array holds its input at every cell: `maxabs(final - initial)` is `== 0` for each of `temp`, `qv`, `qc`, `cptot` and `cvtot` (`input_guard`). The cross-target judgment is not applied.
- `test_id`: `l0_satadj_bad_range_xfail`
  - `level`: `L0`
  - `operation_id`: `microphysics_saturation_liquid_1d_column__saturation_adjustment`
  - `expected_outcome`: `xfail`
  - `case`: `l0_satadj_bad_range`
  - `xfail_condition`: `ks > ke`
  - `quantities`: `input_guard`
  - `judgment`: on `l0_satadj_bad_range`, the `operation` refuses the input: `guard_pass == 0` (`input_guard`), in the failure direction. Each input-updated array holds its input at every cell: `maxabs(final - initial)` is `== 0` for each of `temp`, `qv`, `qc`, `cptot` and `cvtot` (`input_guard`). The cross-target judgment is not applied.
- `test_id`: `l0_satadj_nonconverged_xfail`
  - `level`: `L0`
  - `operation_id`: `microphysics_saturation_liquid_1d_column__saturation_adjustment`
  - `expected_outcome`: `xfail`
  - `case`: `l0_satadj_nonconverged`
  - `xfail_condition`: `qv(4) == 0.66357706133254214`
  - `quantities`: `converged`, `nonconverged_neighbours`
  - `judgment`: on `l0_satadj_nonconverged`, `guard_pass == 1` and, in the failure direction, `converged == 0` (`converged`). Cell 4 does not converge, and it holds its inputs: with `at4` the mask of cell 4, `maxabs(at4*(final - initial))` is `== 0` for each of `temp`, `qv`, `qc`, `cptot` and `cvtot`, and `maxabs(at4*rhoe_d)` is `== 0` (`converged`). The cells on either side of it are still evaluated, so the state changes there (`nonconverged_neighbours`): cells 3 and 6 are subsaturated with `qc > 0` and end with `qc == 0` (`maxabs(m36*final.qc)` is `== 0`, `m36` their mask); cells 5 and 7 are supersaturated and end saturated, `max(m57*abs(final.qv - qs_final)/(2e-6*abs(dqs_final)))` is `<= 1` and `min(m57*final.qc + (1 - m57))` is `> 0`; over every interior cell but cell 4, total water is conserved to `1e-15`. Every published value is finite. The cross-target judgment is not applied.
- `test_id`: `l0_satadj_limit_xfail`
  - `level`: `L0`
  - `operation_id`: `microphysics_saturation_liquid_1d_column__saturation_adjustment`
  - `expected_outcome`: `xfail`
  - `case`: `l0_satadj_limit`
  - `xfail_condition`: `qv(4) == 0.45708 and temp(4) == 279.2327 and dens(4) == 0.88584`
  - `quantities`: `converged`
  - `judgment`: on `l0_satadj_limit`, `guard_pass == 1` and, in the failure direction, `converged == 0` (`converged`). With `at4` the mask of cell 4, `maxabs(at4*(final - initial))` is `== 0` for each of `temp`, `qv`, `qc`, `cptot` and `cvtot`, and `maxabs(at4*rhoe_d)` is `== 0` (`converged`). The cross-target judgment is not applied.
- `test_id`: `l0_satadj_cptot_nonpositive_xfail`
  - `level`: `L0`
  - `operation_id`: `microphysics_saturation_liquid_1d_column__saturation_adjustment`
  - `expected_outcome`: `xfail`
  - `case`: `l0_satadj_cptot_nonpositive`
  - `xfail_condition`: `cptot(4) <= 0`
  - `quantities`: `input_guard`
  - `judgment`: on `l0_satadj_cptot_nonpositive`, the `operation` refuses the input: `guard_pass == 0` (`input_guard`), in the failure direction. Each input-updated array holds its input at every cell: `maxabs(final - initial)` is `== 0` for each of `temp`, `qv`, `qc`, `cptot` and `cvtot` (`input_guard`). The cross-target judgment is not applied.
- `test_id`: `l0_ks_below_one_xfail`
  - `level`: `L0`
  - `operation_id`: `microphysics_saturation_liquid_1d_column__dens2qsat_liq`
  - `expected_outcome`: `xfail`
  - `case`: `l0_ks_below_one`
  - `xfail_condition`: `ks < 1`
  - `quantities`: `input_guard`
  - `judgment`: on `l0_ks_below_one`, the `operation` refuses the input: `guard_pass == 0` (`input_guard`), in the failure direction. The cross-target judgment is not applied.
- `test_id`: `l0_ke_above_ka_xfail`
  - `level`: `L0`
  - `operation_id`: `microphysics_saturation_liquid_1d_column__dens2qsat_liq`
  - `expected_outcome`: `xfail`
  - `case`: `l0_ke_above_ka`
  - `xfail_condition`: `ke > ka`
  - `quantities`: `input_guard`
  - `judgment`: on `l0_ke_above_ka`, the `operation` refuses the input: `guard_pass == 0` (`input_guard`), in the failure direction. The cross-target judgment is not applied.
- `test_id`: `l0_satadj_ks_below_one_xfail`
  - `level`: `L0`
  - `operation_id`: `microphysics_saturation_liquid_1d_column__saturation_adjustment`
  - `expected_outcome`: `xfail`
  - `case`: `l0_satadj_ks_below_one`
  - `xfail_condition`: `ks < 1`
  - `quantities`: `input_guard`
  - `judgment`: on `l0_satadj_ks_below_one`, the `operation` refuses the input: `guard_pass == 0` (`input_guard`), in the failure direction. Each input-updated array holds its input at every cell: `maxabs(final - initial)` is `== 0` for each of `temp`, `qv`, `qc`, `cptot` and `cvtot` (`input_guard`). The cross-target judgment is not applied.
- `test_id`: `l0_satadj_ke_above_ka_xfail`
  - `level`: `L0`
  - `operation_id`: `microphysics_saturation_liquid_1d_column__saturation_adjustment`
  - `expected_outcome`: `xfail`
  - `case`: `l0_satadj_ke_above_ka`
  - `xfail_condition`: `ke > ka`
  - `quantities`: `input_guard`
  - `judgment`: on `l0_satadj_ke_above_ka`, the `operation` refuses the input: `guard_pass == 0` (`input_guard`), in the failure direction. Each input-updated array holds its input at every cell: `maxabs(final - initial)` is `== 0` for each of `temp`, `qv`, `qc`, `cptot` and `cvtot` (`input_guard`). The cross-target judgment is not applied.

## 7. Pass/fail aggregation rules
- `per_test.pass_rule`: `pass` when every predicate of the test holds.
- `per_test.xfail_rule`: `xfail` when `xfail_condition` is true and every predicate of the test holds.
- `suite.pass_rule`: `pass` when all `test_id` are `pass` or `xfail`.

## 8. Traceability
- Record `test_profile_id` and `test_profile_version` in `trial_meta.json`.
