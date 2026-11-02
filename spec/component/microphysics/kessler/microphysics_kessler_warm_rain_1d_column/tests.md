# Tests: Kessler warm-rain microphysics of a column (L0)

## 0. Meta information
- `test_profile_id`: `microphysics_kessler_warm_rain_1d_column_l0`
- `test_profile_version`: `0.1.0`
- `status`: `draft`
- `spec_ref.spec_kind`: `component`
- `spec_ref.spec_id`: `microphysics_kessler_warm_rain_1d_column`
- `spec_ref.spec_version`: `0.1.0`
- `spec_ref.controlled_spec_path`: `spec/component/microphysics/kessler/microphysics_kessler_warm_rain_1d_column/controlled_spec.md`

## 1. Test purpose
This suite verifies the two published `operation`s at `L0`. For `__adjustment`: the unsaturated no-op, autoconversion on both sides of its threshold, accretion, rain evaporation, the floor `eps` of the saturation ratio, the conservation of total water and of moist internal energy, the three limiters, the saturation step's effect, and the droplet number. For `__terminal_velocity`: the formula, the zero switch, the reference-density scaling and the zero cloud velocity. For each `operation`: the halo contract and the input guard.

The symmetry pairs of `docs/TESTS.md` §Symmetry pairs are not judged. A one-dimensional column has no axis exchange. A reversal or a translation of the column with a correspondingly transformed reference profile is a pointwise identity of each cell's computation. The CFL number is `N/A`: the `operation`s have no transport. The comparison with theory is the closed-form judgments of §6.

## 2. Input-defaulting rules
The block below is the suite's whole input. Every case has `ka=8`, and every case but the two bad-range ones passes `ks=2`, `ke=7`, so the first and the last cell are the halo.

- `constants` are the values of the constant arguments of Controlled Spec §2 in every case. `eps` is $2^{-52}$, the spacing of double-precision numbers at `1`.
- `setup` is each state variable's value at every cell before a case's own rules apply. A case's `columns` entry replaces the interior cells: `[a, b]` gives `a + b*(k-2)` at cell `k`, and `{cells: [...]}` gives the six interior values literally. A variable a case names no rule for keeps its `setup` value at every cell: the halo sentinels of the inputs are positive, so that a host expression over a whole column stays finite, and the outputs `rhoe_t`, `evaporate`, `pcsat`, `vterm_c`, `vterm_r` hold `-999`.
- In a case with a `qv` rule, the interior cells of `cptot` and `cvtot` are the moist-air mixture over the case's `qv`, `qc`, `qr`, with `air` the dry-air constants: $q_d=1-q_v-q_c-q_r$, `cptot` $=q_d\,$`cpdry`$+$`cpvap`$\,q_v+$`cl`$(q_c+q_r)$, `cvtot` $=q_d($`cpdry`$-$`rdry`$)+($`cpvap`$-$`rvap`$)q_v+$`cl`$(q_c+q_r)$.
- `overrides` set one cell after every other rule, and are what force the guard cases.
- `operation` names the published `operation` a case calls and `expected` the outcome §6 states. `refusal` states the flags a refused case's run leaves: `guard_pass = 0`. `nonconvergence_check` marks the case whose cell 4 does not converge.
- `dt` is the case's time step.
- `grid`, on a bad-range case, replaces the `ks`, `ke` passed to the `operation` after the state is built with `ks=2`, `ke=7`.
- The literal `qv` cells of `l0_autoconv_below`, `l0_autoconv_above` and `l0_accretion` are $q_s$ of the dependency's Controlled Spec §3 at the case's `temp` and `dens`, computed once in double precision by that closed form and printed to 17 significant digits. The kernel's own $q_s$ is a second evaluation of the same form, so such a cell is saturated to within a few units in the last place, and the saturation step is a near no-op, not an exact one.
- `parameters.qsat_ratio`, on `l0_nonconverged`, states the multiple of $q_s$ at `temp=277.8`, `dens=1` that the literal `qv` of cell 4 is; it is the condition the case's `xfail_condition` reads.

No case sits on the boundary between the two branches of the saturation step, where a change of the last bit changes the branch: at every interior cell of every `pass` case, $q_\Sigma$ and $q_s$ at the virtual temperature differ by more than `10%` of $q_s$.

```yaml
grid: {ka: 8, ks: 2, ke: 7}
constants: {rvap: 461.50, cpvap: 1846.0, cl: 4218.0, lhv0: 2.501e+6, psat0: 610.78, tem00: 273.15, eps: 2.220446049250313e-16, dwatr: 1000.0}
air: {cpdry: 1004.64, rdry: 287.04}
setup: {temp: 222.0, dens: 0.77, pres: 12345.0, qv: 0.0123, qc: 0.0321, qr: 0.0456, cptot: 1111.0, cvtot: 777.0, rhoq_c: 0.00011, rhoq_r: 0.00022, refstate_dens: 0.66, rhoe_t: -999.0, evaporate: -999.0, pcsat: -999.0, vterm_c: -999.0, vterm_r: -999.0}
scalars: {guard_pass: -1.0}
cases:
- {case_id: l0_dry_column, operation: adjustment, expected: pass, dt: 10.0, columns: {temp: [280.0, 4.0], dens: [1.1, -0.05], pres: [95000.0, -3000.0], qv: [0.002, 2.0e-4], qc: [0.0, 0.0], qr: [0.0, 0.0]}}
- {case_id: l0_autoconv_below, operation: adjustment, expected: pass, dt: 10.0, columns: {temp: [280.0, 3.0], dens: [1.1, -0.05], pres: [95000.0, -3000.0], qv: {cells: [0.0069707099479050273, 0.0088562934797248565, 0.011224291781499925, 0.014196002954967301, 0.017924837548416488, 0.022606104294801654]}, qc: [3.0e-4, 1.0e-4], qr: [0.0, 0.0]}}
- {case_id: l0_autoconv_above, operation: adjustment, expected: pass, dt: 10.0, columns: {temp: [280.0, 3.0], dens: [1.1, -0.05], pres: [95000.0, -3000.0], qv: {cells: [0.0069707099479050273, 0.0088562934797248565, 0.011224291781499925, 0.014196002954967301, 0.017924837548416488, 0.022606104294801654]}, qc: [2.0e-3, 4.0e-4], qr: [0.0, 0.0]}}
- {case_id: l0_accretion, operation: adjustment, expected: pass, dt: 5.0, columns: {temp: [285.0, 2.0], dens: [1.05, -0.05], pres: [95000.0, -3000.0], qv: {cells: [0.010045104325954974, 0.011938599971313749, 0.014196002954967301, 0.016893863866331377, 0.020127891289273229, 0.024018985813226756]}, qc: [5.0e-4, 1.0e-4], qr: [2.0e-4, 1.0e-4]}}
- {case_id: l0_evaporation, operation: adjustment, expected: pass, dt: 10.0, columns: {temp: [290.0, -2.0], dens: [1.0, -0.04], pres: [90000.0, -2000.0], qv: [0.003, 2.0e-4], qc: [0.0, 0.0], qr: [1.0e-3, 2.0e-4]}}
- {case_id: l0_cloud_evaporates, operation: adjustment, expected: pass, dt: 10.0, columns: {temp: [285.0, 0.5], dens: [1.0, 0.0], pres: [100000.0, 0.0], qv: [0.0095, 0.0], qc: [3.0e-3, 2.0e-4], qr: [0.0, 0.0]}}
- {case_id: l0_supersaturated, operation: adjustment, expected: pass, dt: 10.0, columns: {temp: [280.0, 3.0], dens: [1.1, -0.05], pres: [95000.0, -3000.0], qv: [0.009, 0.003], qc: [1.0e-3, 2.0e-4], qr: [0.0, 0.0]}}
- {case_id: l0_eps_floor, operation: adjustment, expected: pass, dt: 100.0, columns: {temp: [108.0, 0.2], dens: [1.0, 0.0], pres: [100000.0, 0.0], qv: [1.0e-18, 0.0], qc: [0.0, 0.0], qr: [1.0e-3, 0.0]}}
- {case_id: l0_limiter_qr, operation: adjustment, expected: pass, dt: 3600.0, columns: {temp: [290.0, 1.0], dens: [1.0, -0.02], pres: [95000.0, -1000.0], qv: [0.001, 1.0e-4], qc: [0.0, 0.0], qr: [1.0e-5, 2.0e-6]}}
- {case_id: l0_limiter_qc, operation: adjustment, expected: pass, dt: 100.0, columns: {temp: [290.0, 1.0], dens: [1.0, -0.02], pres: [95000.0, -1000.0], qv: [0.002, 1.0e-4], qc: [1.0e-5, 2.0e-6], qr: [0.01, 1.0e-3]}}
- {case_id: l0_limiter_qv, operation: adjustment, expected: pass, dt: 100.0, columns: {temp: [290.0, 1.0], dens: [1.0, -0.02], pres: [95000.0, -1000.0], qv: [1.0e-6, 2.0e-7], qc: [1.0e-3, 1.0e-4], qr: [0.01, 1.0e-3]}}
- {case_id: l0_tv_formula, operation: terminal_velocity, expected: pass, columns: {dens: [1.1, -0.05], rhoq_c: [1.0e-4, 1.0e-5], rhoq_r: [1.0e-3, 2.0e-4], refstate_dens: [1.2, -0.08]}}
- {case_id: l0_tv_refstate, operation: terminal_velocity, expected: pass, columns: {dens: [1.0, 0.0], rhoq_c: [1.0e-4, 0.0], rhoq_r: [1.0e-3, 0.0], refstate_dens: [1.2, -0.1]}}
- {case_id: l0_tv_zerosw, operation: terminal_velocity, expected: pass, columns: {dens: [1.1, -0.05], rhoq_c: [1.0e-4, 1.0e-5], rhoq_r: [1.0e-13, 1.0e-14], refstate_dens: [1.2, -0.08]}}
- {case_id: l0_dt_nonpositive, operation: adjustment, expected: xfail, refusal: {}, dt: 0.0, columns: {temp: [285.0, 3.0], dens: [1.05, -0.05], pres: [95000.0, -3000.0], qv: [0.002, 3.0e-4], qc: [5.0e-4, 1.0e-4], qr: [1.0e-4, 1.0e-5]}}
- {case_id: l0_negative_moisture, operation: adjustment, expected: xfail, refusal: {}, dt: 10.0, columns: {temp: [285.0, 3.0], dens: [1.05, -0.05], pres: [95000.0, -3000.0], qv: [0.002, 3.0e-4], qc: [5.0e-4, 1.0e-4], qr: [1.0e-4, 1.0e-5]}, overrides: [{var: qr, k: 4, value: -1.0e-5}]}
- {case_id: l0_cvtot_nonpositive, operation: adjustment, expected: xfail, refusal: {}, dt: 10.0, columns: {temp: [285.0, 3.0], dens: [1.05, -0.05], pres: [95000.0, -3000.0], qv: [0.002, 3.0e-4], qc: [5.0e-4, 1.0e-4], qr: [1.0e-4, 1.0e-5]}, overrides: [{var: cvtot, k: 4, value: 0.0}]}
- {case_id: l0_dens_nonpositive, operation: adjustment, expected: xfail, refusal: {}, dt: 10.0, columns: {temp: [285.0, 3.0], dens: [1.05, -0.05], pres: [95000.0, -3000.0], qv: [0.002, 3.0e-4], qc: [5.0e-4, 1.0e-4], qr: [1.0e-4, 1.0e-5]}, overrides: [{var: dens, k: 4, value: 0.0}]}
- {case_id: l0_bad_range, operation: adjustment, expected: xfail, refusal: {}, dt: 10.0, grid: {ks: 7, ke: 2}, columns: {temp: [285.0, 3.0], dens: [1.05, -0.05], pres: [95000.0, -3000.0], qv: [0.002, 3.0e-4], qc: [5.0e-4, 1.0e-4], qr: [1.0e-4, 1.0e-5]}}
- {case_id: l0_nonconverged, operation: adjustment, expected: xfail, refusal: {}, nonconvergence_check: true, dt: 10.0, parameters: {qsat_ratio: 100.0}, columns: {temp: [277.8, 0.0], dens: [1.0, 0.0], pres: [100000.0, 0.0], qv: {cells: [0.001, 0.001, 0.66357706133254214, 0.001, 0.001, 0.001]}, qc: {cells: [5.0e-4, 5.0e-4, 0.0, 5.0e-4, 5.0e-4, 5.0e-4]}, qr: {cells: [1.0e-4, 1.0e-4, 0.0, 1.0e-4, 1.0e-4, 1.0e-4]}}}
- {case_id: l0_tv_bad_range, operation: terminal_velocity, expected: xfail, refusal: {}, grid: {ks: 7, ke: 2}, columns: {dens: [1.1, -0.05], rhoq_c: [1.0e-4, 1.0e-5], rhoq_r: [1.0e-3, 2.0e-4], refstate_dens: [1.2, -0.08]}}
- {case_id: l0_tv_dens_nonpositive, operation: terminal_velocity, expected: xfail, refusal: {}, columns: {dens: [1.1, -0.05], rhoq_c: [1.0e-4, 1.0e-5], rhoq_r: [1.0e-3, 2.0e-4], refstate_dens: [1.2, -0.08]}, overrides: [{var: dens, k: 4, value: 0.0}]}
- {case_id: l0_tv_refstate_nonpositive, operation: terminal_velocity, expected: xfail, refusal: {}, columns: {dens: [1.1, -0.05], rhoq_c: [1.0e-4, 1.0e-5], rhoq_r: [1.0e-3, 2.0e-4], refstate_dens: [1.2, -0.08]}, overrides: [{var: refstate_dens, k: 4, value: 0.0}]}
```

## 3. Execution-control rules
`N/A`: each `operation` is a single evaluation of one column. `dt` is a case input of §2 and the suite performs one step. Stepping is the responsibility of a `problem` runner.

## 4. Case-expansion rules
`N/A`: the `L0` suite uses the fixed cases of §2 and defines no `case` sweep.

## 5. Diagnostics contract
- Require outputting `checks.dry_noop`, `checks.autoconversion`, `checks.accretion`, `checks.evaporation`, `checks.eps_floor`, `checks.water_energy`, `checks.limiter`, `checks.saturated`, `checks.evaporate_number`, `checks.tv_formula`, `checks.tv_zerosw`, `checks.tv_refstate`, `checks.tv_cloud_zero`, `checks.halo_untouched` and `checks.input_guard` in `diagnostics.json`. Each is computed on the cases of the test that names it in §6 and is `na` on every other case; `checks.input_guard` is defined in the next item.
- The state of a case, captured after setup and after the run, consists of `temp`, `qv`, `qc`, `qr`, `cptot`, `cvtot`, `dens`, `pres`, `rhoe_t`, `evaporate`, `pcsat`, `vterm_c`, `vterm_r`, `rhoq_c`, `rhoq_r`, `refstate_dens` (each with extent `ka`, at the §2 values at setup) and the scalar `guard_pass`, recorded as a number because a state variable is real-valued: `-1` at setup, before the `operation` is called; `1` when the call returned true; `0` when it returned false. A variable an `operation` does not publish keeps its setup value. `checks.input_guard` is `pass` when `guard_pass = 1` and `fail` otherwise.
- A judgment reads a halo cell only through the masks below. With $i=k-1$ and $c=i+1/2$ the cell-centre coordinate of a unit-length cell, `coordinates[]` axis `0`, `count` and `length` `ka`, placement `cell_center`: `interior` $=\min(1,\max(0,\lfloor c\rfloor-(ks-2)))\cdot\min(1,\max(0,ke-\lfloor c\rfloor))$, `halo` $=1-$`interior`, `first` $=\max(0,1-|\lfloor c\rfloor-(ks-1)|)$ marks the cell $k=ks$, and `pairs` $=\min(1,\max(0,\lfloor c\rfloor-(ks-2)))\cdot\min(1,\max(0,ke-1-\lfloor c\rfloor))$ marks the cells $k=ks..ke-1$.
- `qs` is $q_s$ of the dependency's Controlled Spec §3 at `initial.temp`, `initial.dens`; `qs_final` and `dqs_final` are $q_s$ and $dq_s/dT$ at `final.temp`, `final.dens`, over every cell (the halo sentinels keep them finite). `L00` and `cvv` are the derived constants of Controlled Spec §2. `dq_auto`, `dq_accr` and `dq_evap` are $d_{auto}$, $d_{accr}$, $d_{evap}$ of Controlled Spec §3 step 1 over the `initial` state and `qs`. `dv`, `dc` and `dr` are `final - initial` of `qv`, `qc`, `qr`.
- The round-off bounds of §6 are offline calibrations against an independent implementation of the scheme, the original `scale-met/scale-dev` code at commit `dc58155`, and each states what it bounds. `2e-13` on `temp` bounds $(TC_v)/C_v\ne T$ and the rewrite of `temp` by the saturation step's virtual branch, in the last bits. `1e-15` on total water bounds the rounding of a sum of mixing ratios. `3e-16` bounds the change of a mixing ratio across a near no-op saturation step: it is `dqs/dT`, at most `1.5e-3 /K` at these temperatures, times the bound `2e-13` on the rounding of `temp`. `1e-17` bounds $q+(-q/\mathtt{dt})\mathtt{dt}$ on a limited variable below `0.01`, which is nonzero and can be negative. The denominator floor `10` of `rhoe_t` is $\rho L_{00}$ times the rounding of a `qv` of order `0.02`, relative to `1e-12`. The Newton-branch bounds are those of the dependency's `tests.md` §5.
- `drift_ratio` compares two consequences of the last Newton correction $\delta$ (Controlled Spec §3 of the saturation node). The state pairs $T=T^{it}-\delta$ with $q_s(T^{it})$, so `final.qv - qs_final` is $\delta\,q_s'$ to first order in $\delta$, and the energy drift is $\delta\,(q_s'(L_{00}+T^{it}(c_{vv}-c_l))+C_v^{prev}-C_v)$. Their quotient differs from 1 by the terms in $\delta$, by $(C_v^{prev}-C_v)/(q_s'(L_{00}+T(c_{vv}-c_l)))$, which is $(c_{vv}-c_l)/(L_{00}+T(c_{vv}-c_l))$, about $1.2\times10^{-3}$ per kelvin, times the correction before the last, and by the rounding of the drift over $\delta\,q_s'(L_{00}+T(c_{vv}-c_l))$. A slope, a first guess or a threshold other than Controlled Spec §3 changes the correction before the last or the pairing itself. The bound `1e-3` is a calibration against the original and independent kernels, not a derived limit. The mask `pairs` leaves out cell 7 of the case, whose last correction of `2.3e-10 K` makes the rounding term as large as `2.3e-4`, measured; the cells it keeps have last corrections from `3e-8 K` to `3.3e-7 K` and measure at most `2.9e-6`, over gfortran and ifort or ifx builds from `-O0` to `-Ofast`, with and without fused multiply-add, and four arrangements of the arithmetic. Where a kernel returns `final.qv` equal to `qs_final` the denominator is zero and the expression is not evaluable, which the host records as a structural failure and so a failed test. A correct kernel does not reach that on this case: its `final.qv - qs_final` is at least `3e-13` at every interior cell, against a last bit of about `3e-18`.
- `cross_target_state_agreement` compares this target's run with every other target's certified variant of this node, case by case (`docs/TESTS.md` §Cross-target judgment): for one output `q` captured after the run and the same output `q_ref` of the same case in the other variant, it is `max|q - q_ref| / max(max|q|, max|q_ref|, 1)` over every element. The host evaluates it from the captured state; it is not a `diagnostics.json` field, and on a target with no other certified variant it holds vacuously. Where the saturation step is a near no-op or `qc` is limited, `pcsat` is round-off and the droplet-number factor of about `4.6e11 m-3 per kg/kg` amplifies it, so `pcsat`, `evaporate` and `rhoe_t` are not compared there, and each judgment names what it compares.

## 6. Test definitions
Every `pass` test also requires `guard_pass = 1` on each of its cases.

- `test_id`: `l0_dry_column_noop_pass`
  - `level`: `L0`
  - `operation_id`: `microphysics_kessler_warm_rain_1d_column__adjustment`
  - `expected_outcome`: `pass`
  - `judgment`: on `l0_dry_column` (`qc = qr = 0`, `qv` below `qs`), `qv`, `qc`, `qr`, `cptot`, `cvtot` are unchanged exactly (`maxabs(final - initial)` `== 0` for each), `maxabs(interior*X)` is `== 0` for each of `rhoe_t`, `evaporate` and `pcsat`, and `maxabs(final.temp - initial.temp)` is `<= 2e-13` (`checks.dry_noop`). The cross-target judgment is applied: `cross_target_state_agreement` over each of `temp`, `qv`, `qc`, `qr`, `cptot`, `cvtot`, `rhoe_t`, `evaporate` and `pcsat` is `<= 1e-12`.
- `test_id`: `l0_autoconversion_threshold_pass`
  - `level`: `L0`
  - `operation_id`: `microphysics_kessler_warm_rain_1d_column__adjustment`
  - `expected_outcome`: `pass`
  - `judgment`: on `l0_autoconv_below` (`qc <= 1e-3` at every interior cell, `qr = 0`, `qv` at `qs`): `maxabs(interior*dc)` is `<= 3e-16` and `maxabs(final.qr - initial.qr)` is `== 0`. On `l0_autoconv_above` (`qc > 1e-3`, `qr = 0`, `qv` at `qs`), with `ref = 1e-3*(initial.qc - 1e-3)*dt`: `maxabs(interior*(dc + ref)) / maxabs(interior*ref)` and `maxabs(interior*(dr - ref)) / maxabs(interior*ref)` are `<= 1e-10`. On both, `maxabs(interior*dv)` is `<= 3e-16` and `maxabs(final.temp - initial.temp)` is `<= 2e-13`. `evaporate` is not judged here: `pcsat` is round-off (`checks.autoconversion`). The denominator is the column maximum of `ref`, `3e-5` here and `1.9e-5` on `l0_accretion`, so the bound `3e-16` of §5 is at most `1.6e-11` of it, and `1e-10` carries a factor of 6 over that. The cross-target judgment is applied: `cross_target_state_agreement` over each of `temp`, `qv`, `qc`, `qr`, `cptot` and `cvtot` is `<= 1e-12`.
- `test_id`: `l0_accretion_pass`
  - `level`: `L0`
  - `operation_id`: `microphysics_kessler_warm_rain_1d_column__adjustment`
  - `expected_outcome`: `pass`
  - `judgment`: on `l0_accretion` (`qc, qr > 0`, `qv` at `qs`, so that `dq_evap` is at most round-off), with `ref = (dq_auto + dq_accr)*dt`: `maxabs(interior*(dc + ref)) / maxabs(interior*ref)` and `maxabs(interior*(dr - ref)) / maxabs(interior*ref)` are `<= 1e-10` (`checks.accretion`). The cross-target judgment is applied: `cross_target_state_agreement` over each of `temp`, `qv`, `qc`, `qr`, `cptot` and `cvtot` is `<= 1e-12`.
- `test_id`: `l0_evaporation_pass`
  - `level`: `L0`
  - `operation_id`: `microphysics_kessler_warm_rain_1d_column__adjustment`
  - `expected_outcome`: `pass`
  - `judgment`: on `l0_evaporation` (`qc = 0`, `qr > 0`, subsaturated), with `ref = dq_evap*dt`: `maxabs(interior*(dr + ref)) / maxabs(interior*ref)` and `maxabs(interior*(dv - ref)) / maxabs(interior*ref)` are `<= 1e-12`; `maxabs(interior*final.qc)` is `== 0`; and with `tref = -dv*(L00 + initial.temp*(cvv - cl))/final.cvtot`, `maxabs(interior*(final.temp - initial.temp - tref))` is `<= 2e-13` (`checks.evaporation`). The cross-target judgment is applied: `cross_target_state_agreement` over each of `temp`, `qv`, `qc`, `qr`, `cptot`, `cvtot` and `rhoe_t` is `<= 1e-12`.
- `test_id`: `l0_eps_floor_pass`
  - `level`: `L0`
  - `operation_id`: `microphysics_kessler_warm_rain_1d_column__adjustment`
  - `expected_outcome`: `pass`
  - `judgment`: on `l0_eps_floor`, where `qs` is below `eps` at every interior cell (`temp` near `108`, `qs` near `3e-17`) so that the floor decides the saturation ratio, with `ref = dq_evap*dt` and `dq_evap` over `max(qs, eps)`: `maxabs(interior*(dv - ref)) / maxabs(interior*ref)` is `<= 1e-12` (`checks.eps_floor`). The saturation ratio is at most `0.0045` with the floor and up to `0.03` without it, so a kernel that omits the floor deviates by about `2e-2`. The cross-target judgment is applied: `cross_target_state_agreement` over each of `temp`, `qv` and `qr` is `<= 1e-12`.
- `test_id`: `l0_total_water_and_energy_pass`
  - `level`: `L0`
  - `operation_id`: `microphysics_kessler_warm_rain_1d_column__adjustment`
  - `expected_outcome`: `pass`
  - `judgment`: on `l0_dry_column`, `l0_autoconv_below`, `l0_autoconv_above`, `l0_accretion`, `l0_evaporation`, `l0_cloud_evaporates`, `l0_supersaturated`, where no limiter binds, and on `l0_limiter_qr`, `l0_limiter_qc`, where the `qv` limiter does not: `maxabs(interior*(dv + dc + dr))` is `<= 1e-15`; with `em = final.temp*final.cvtot + final.qv*L00` and `em0 = initial.temp*initial.cvtot + initial.qv*L00`, `max(interior*abs(em - em0)/(2e-6*dqs_final*(L00 + final.temp*(cvv - cl))))` is `<= 1`; and `maxabs(interior*(rhoe_t*dt + dens*L00*dv)) / max(maxabs(interior*dens*L00*dv), 10)` is `<= 1e-12` (`checks.water_energy`). The cross-target judgment is applied: `cross_target_state_agreement` over each of `qv`, `qc` and `qr` is `<= 1e-12`.
- `test_id`: `l0_limiter_positivity_pass`
  - `level`: `L0`
  - `operation_id`: `microphysics_kessler_warm_rain_1d_column__adjustment`
  - `expected_outcome`: `pass`
  - `judgment`: on `l0_limiter_qr` the `qr` limiter binds at every interior cell: `maxabs(interior*final.qr)` is `<= 1e-17`. On `l0_limiter_qc` the `qc` limiter binds at every interior cell and each cell ends subsaturated through the virtual branch: `maxabs(interior*final.qc)` is `== 0`. On `l0_limiter_qv` the `qv` limiter binds at every interior cell: `maxabs(interior*final.qv)` is `<= 1e-17`. On all three, `min(interior*X)` is `>= -1e-17` for each of `qv`, `qc`, `qr` (`checks.limiter`). The cross-target judgment is applied: `cross_target_state_agreement` over each of `temp`, `qv`, `qc`, `qr`, `cptot` and `cvtot` is `<= 1e-12`.
- `test_id`: `l0_supersaturated_saturates_pass`
  - `level`: `L0`
  - `operation_id`: `microphysics_kessler_warm_rain_1d_column__adjustment`
  - `expected_outcome`: `pass`
  - `judgment`: on `l0_supersaturated`: `max(interior*abs(final.qv - qs_final)/(2e-6*dqs_final))` is `<= 1`, the bound of the dependency's `tests.md` §5; with `em`, `em0` as in `l0_total_water_and_energy_pass`, `drift_ratio = (em - em0)/((final.qv - qs_final)*(L00 + final.temp*(cvv - cl)))` and `max(pairs*abs(drift_ratio - 1))` is `<= 1e-3`, the bound of the dependency's section with the same mask; and `min(interior*final.qc + halo)` is `> 0` (`checks.saturated`). The cross-target judgment is applied: `cross_target_state_agreement` over each of `temp`, `qv`, `qc`, `cptot`, `cvtot`, `rhoe_t` and `pcsat` is `<= 1e-12`.
- `test_id`: `l0_evaporate_number_pass`
  - `level`: `L0`
  - `operation_id`: `microphysics_kessler_warm_rain_1d_column__adjustment`
  - `expected_outcome`: `pass`
  - `judgment`: on `l0_cloud_evaporates` (subsaturated with cloud) and `l0_supersaturated` (condensing), with `ref = max(-pcsat, 0)*dens/(4/3*pi*dwatr*(8e-6)**3)`: `maxabs(interior*(evaporate - ref)) / max(maxabs(interior*ref), 1)` is `<= 1e-12`. `pcsat` itself equals the saturation step's change of `qc`: with `star = initial.qc - 1e-3*max(initial.qc - 1e-3, 0)*dt`, the value of `qc` before that step on these two cases, where `qr = 0`, `maxabs(interior*(pcsat - (final.qc - star)/dt)) / maxabs(interior*pcsat)` is `<= 1e-12`. On `l0_cloud_evaporates`, `min(interior*evaporate + halo)` is `> 0`; on `l0_supersaturated`, `maxabs(interior*evaporate)` is `== 0` and `min(interior*pcsat + halo)` is `> 0` (`checks.evaporate_number`). The cross-target judgment is applied: `cross_target_state_agreement` over each of `evaporate` and `pcsat` is `<= 1e-12`.
- `test_id`: `l0_adjustment_halo_untouched_pass`
  - `level`: `L0`
  - `operation_id`: `microphysics_kessler_warm_rain_1d_column__adjustment`
  - `expected_outcome`: `pass`
  - `judgment`: on every `pass` case of `__adjustment`, `maxabs(halo*(final - initial))` is `== 0` for each of `temp`, `qv`, `qc`, `qr`, `cptot` and `cvtot`, and `maxabs(halo*X)` is `== 0` for each of `rhoe_t`, `evaporate` and `pcsat` (`checks.halo_untouched`). The cross-target judgment is applied: `cross_target_state_agreement` over each of `temp`, `qv`, `qc`, `qr`, `cptot` and `cvtot` is `<= 1e-12`.
- `test_id`: `l0_terminal_velocity_formula_pass`
  - `level`: `L0`
  - `operation_id`: `microphysics_kessler_warm_rain_1d_column__terminal_velocity`
  - `expected_outcome`: `pass`
  - `judgment`: on `l0_tv_formula` (every interior `qr >= 1e-12`), with `ref = -36.34*(dens*(rhoq_r/dens))**0.1364*sum(first*refstate_dens)/refstate_dens`: `maxabs(interior*(vterm_r - ref)) / maxabs(interior*ref)` is `<= 1e-12`, and `max(interior*vterm_r - halo)` is `< 0` (`checks.tv_formula`). The cross-target judgment is applied: `cross_target_state_agreement` over `vterm_r` is `<= 1e-12`.
- `test_id`: `l0_terminal_velocity_zerosw_pass`
  - `level`: `L0`
  - `operation_id`: `microphysics_kessler_warm_rain_1d_column__terminal_velocity`
  - `expected_outcome`: `pass`
  - `judgment`: on `l0_tv_zerosw` (every interior `qr < 1e-12`), `maxabs(final.vterm_r)` is `== 0` (`checks.tv_zerosw`). The cross-target judgment is applied: `cross_target_state_agreement` over `vterm_r` is `<= 1e-12`.
- `test_id`: `l0_terminal_velocity_refstate_scaling_pass`
  - `level`: `L0`
  - `operation_id`: `microphysics_kessler_warm_rain_1d_column__terminal_velocity`
  - `expected_outcome`: `pass`
  - `judgment`: on `l0_tv_refstate` (`dens` and `rhoq_r` constant, `refstate_dens` varying), with `vks = sum(first*vterm_r)` and `rks = sum(first*refstate_dens)`: `maxabs(interior*(vterm_r/vks - rks/refstate_dens))` is `<= 1e-12` (`checks.tv_refstate`). The cross-target judgment is applied: `cross_target_state_agreement` over `vterm_r` is `<= 1e-12`.
- `test_id`: `l0_terminal_velocity_cloud_zero_pass`
  - `level`: `L0`
  - `operation_id`: `microphysics_kessler_warm_rain_1d_column__terminal_velocity`
  - `expected_outcome`: `pass`
  - `judgment`: on `l0_tv_formula`, `l0_tv_refstate` and `l0_tv_zerosw`, `maxabs(final.vterm_c)` is `== 0` (`checks.tv_cloud_zero`). The cross-target judgment is applied: `cross_target_state_agreement` over `vterm_c` is `<= 1e-12`.
- `test_id`: `l0_tv_halo_untouched_pass`
  - `level`: `L0`
  - `operation_id`: `microphysics_kessler_warm_rain_1d_column__terminal_velocity`
  - `expected_outcome`: `pass`
  - `judgment`: on `l0_tv_formula`, `l0_tv_refstate` and `l0_tv_zerosw`, `maxabs(halo*vterm_r)` is `== 0` (`checks.halo_untouched`). The cross-target judgment is applied: `cross_target_state_agreement` over `vterm_r` is `<= 1e-12`.
- `test_id`: `l0_dt_nonpositive_xfail`
  - `level`: `L0`
  - `operation_id`: `microphysics_kessler_warm_rain_1d_column__adjustment`
  - `expected_outcome`: `xfail`
  - `case`: `l0_dt_nonpositive`
  - `xfail_condition`: `dt <= 0`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`
  - `judgment`: `guard_pass` is `0`, and each input-updated array holds its input at every cell: `maxabs(final - initial)` is `== 0` for each of `temp`, `qv`, `qc`, `qr`, `cptot` and `cvtot`.
- `test_id`: `l0_negative_moisture_xfail`
  - `level`: `L0`
  - `operation_id`: `microphysics_kessler_warm_rain_1d_column__adjustment`
  - `expected_outcome`: `xfail`
  - `case`: `l0_negative_moisture`
  - `xfail_condition`: `qr(4) < 0`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`
  - `judgment`: `guard_pass` is `0`, and each input-updated array holds its input at every cell: `maxabs(final - initial)` is `== 0` for each of `temp`, `qv`, `qc`, `qr`, `cptot` and `cvtot`.
- `test_id`: `l0_cvtot_nonpositive_xfail`
  - `level`: `L0`
  - `operation_id`: `microphysics_kessler_warm_rain_1d_column__adjustment`
  - `expected_outcome`: `xfail`
  - `case`: `l0_cvtot_nonpositive`
  - `xfail_condition`: `cvtot(4) <= 0`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`
  - `judgment`: `guard_pass` is `0`, and each input-updated array holds its input at every cell: `maxabs(final - initial)` is `== 0` for each of `temp`, `qv`, `qc`, `qr`, `cptot` and `cvtot`.
- `test_id`: `l0_dens_nonpositive_xfail`
  - `level`: `L0`
  - `operation_id`: `microphysics_kessler_warm_rain_1d_column__adjustment`
  - `expected_outcome`: `xfail`
  - `case`: `l0_dens_nonpositive`
  - `xfail_condition`: `dens(4) <= 0`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`
  - `judgment`: `guard_pass` is `0`, and each input-updated array holds its input at every cell: `maxabs(final - initial)` is `== 0` for each of `temp`, `qv`, `qc`, `qr`, `cptot` and `cvtot`.
- `test_id`: `l0_bad_range_xfail`
  - `level`: `L0`
  - `operation_id`: `microphysics_kessler_warm_rain_1d_column__adjustment`
  - `expected_outcome`: `xfail`
  - `case`: `l0_bad_range`
  - `xfail_condition`: `ks > ke`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`
  - `judgment`: `guard_pass` is `0`, and each input-updated array holds its input at every cell: `maxabs(final - initial)` is `== 0` for each of `temp`, `qv`, `qc`, `qr`, `cptot` and `cvtot`.
- `test_id`: `l0_nonconverged_xfail`
  - `level`: `L0`
  - `operation_id`: `microphysics_kessler_warm_rain_1d_column__adjustment`
  - `expected_outcome`: `xfail`
  - `case`: `l0_nonconverged`
  - `xfail_condition`: `qsat_ratio >= 100`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`
  - `judgment`: `guard_pass` is `0`, and each input-updated array holds its input at every cell, as `l0_dt_nonpositive_xfail`, although the rates and the saturation step of the column's other cells, which hold `qc > 0` and `qr > 0`, have changed them before the refusal. Cell 4, with `qc = qr = 0`, does not converge.
- `test_id`: `l0_tv_bad_range_xfail`
  - `level`: `L0`
  - `operation_id`: `microphysics_kessler_warm_rain_1d_column__terminal_velocity`
  - `expected_outcome`: `xfail`
  - `case`: `l0_tv_bad_range`
  - `xfail_condition`: `ks > ke`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`
  - `judgment`: `guard_pass` is `0`. The outputs are not defined on a refused call (Controlled Spec §4), so the flag alone is judged.
- `test_id`: `l0_tv_dens_nonpositive_xfail`
  - `level`: `L0`
  - `operation_id`: `microphysics_kessler_warm_rain_1d_column__terminal_velocity`
  - `expected_outcome`: `xfail`
  - `case`: `l0_tv_dens_nonpositive`
  - `xfail_condition`: `dens(4) <= 0`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`
  - `judgment`: `guard_pass` is `0`.
- `test_id`: `l0_tv_refstate_nonpositive_xfail`
  - `level`: `L0`
  - `operation_id`: `microphysics_kessler_warm_rain_1d_column__terminal_velocity`
  - `expected_outcome`: `xfail`
  - `case`: `l0_tv_refstate_nonpositive`
  - `xfail_condition`: `refstate_dens(4) <= 0`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`
  - `judgment`: `guard_pass` is `0`.

## 7. Pass/fail aggregation rules
- `per_test.pass_rule`: `pass` when the judgment expression is satisfied.
- `per_test.xfail_rule`: `xfail` when `xfail_condition` is true and `pass_when` is satisfied.
- `suite.pass_rule`: `pass` when all `test_id` are `pass` or `xfail`.

## 8. Traceability
- Record `test_profile_id` and `test_profile_version` in `trial_meta.json`.
