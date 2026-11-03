# Conversion record: `problem/shallow_water2d@0.4.7` into the issue #589 shape (PR-1a fixture)

Source documents:
- certified IR `workspace/ir/problem__shallow_water2d__0.4.7/shallow-water2d_20261103_001/spec.ir.yaml`;
- certified run `workspace/pipelines/problem__shallow_water2d__0.4.7/fortran_cpu/shallow-water2d_20261103_001/` (model, checks module, runner under `source/src_20261103_001/src`; captures under `runs/run_20261103_001/problem__shallow_water2d__0.4.7/raw/state_snapshots/`; `verdict.json`: 12 `pass`, 1 `xfail`);
- `spec/problem/dynamics/shallow_water/shallow_water2d/{controlled_spec.md,tests.md}` at `spec_version` `0.4.7`, `test_profile_version` `0.7.0` (both kept, so the catalog lookup agrees with `meta`).

Files: `spec.ir.yaml` (the converted IR), `controlled_spec.md` and `tests.md` (copies changed only where the converted IR requires).

## 1. Surface and drive

- `public_api`: one published operation, `shallow_water2d__step(nx, ny, dx, dy, g, dt, t, z_b, h, hu, hv, ok)`. The arguments are the ones the certified model's step body reads (`shallow_water2d__run`'s loop body and `eval_rhs` read `nx`, `ny`, `dx`, `dy`, `g`, `dt`, `z_b`, `h`, `hu`, `hv`), plus `t` (plan §3.4 lists it; the certified scheme does not read it — it is an `in` scalar the driver binds to `time`) and the result `ok`. `h`, `hu`, `hv` are `inout` (rank 2, `dims: [nx, ny]`), `z_b` is `in`, `ok` is the one logical `out`. There is no rank-0 real or integer `out` (the certified `n_step`, `cfl_run_max`, `h_min_run` dummies are gone). `nx`, `ny` integer; every real `real(dp)` with `module_parameters: dp = float64`. The certified `n_step_target` argument is gone: the loop is the driver's.
- `drive.step`: `{operation: shallow_water2d__step, result: ok}` with `nx: inputs.grid.nx`, `ny: inputs.grid.ny`, `dx: derived.dx`, `dy: derived.dy`, `g: inputs.constants.g`, `dt: derived.dt`, `t: time`, `z_b/h/hu/hv/ok: state.*`.
- `drive.loop`: `n_step: derived.n_step`, `dt: derived.dt`, `t_start: inputs.time.t_start`, `t_end: inputs.time.t_end`.
- `drive.history.every: inputs.history.every`; every case's `inputs` gains `history: {every: <ceil(n_step / 2)>}` (placed after `time`).
- `controlled_spec.md` §5 keeps its title (Integration algorithm); a paragraph naming `shallow_water2d__step` and its contract, and `### 5.1 Canonical interface block` with the fence (the form of `dynamics_shallow_water_source_2d_coriolis/controlled_spec.md` §5.1), are appended at the end of §5. The §5 sentence "the run stops with an error" becomes "the step returns `ok = false` … not committed" (D3: a failure is a `false` result). Checked: `_parse_public_api_from_controlled_spec` reads exactly `{shallow_water2d__step}` from §5; the fence's `procedures` equal `public_api.signatures[].signature` and its `module_parameters` equal `public_api.module_parameters`; the Fortran `signatures` backend renders it without error.

### The history stride: per case, `every = ceil(n_step / 2)`

- Revised 2026-11-04 at the operator's request to cut the sample size; the first conversion used one stride `every = 38` (the guard's `n_step`), `9,011,200` state numbers, about 200 MB of JSON per run.
- A reduction over an empty history is `structural` for that predicate (SHAPE.md §5), so every case needs at least one sample: `ceil(n_step / 2) <= n_step` gives one sample at mid-run, and a second at the final step where `n_step` is even. Per case: 45 (n032, 90 steps), 90 (179), 112 (223), 179 (357), 357 (n256, 714), 2230 (`_tend500`, 4459), 19 (guard, 38).
- The guard's detection over history (`l0_cfl_guard_xfail`, `> 1.0`) holds: its samples are committed steps 19 and 38, and step 38 is the final state, whose index is `1.055733964672891` (measured, §4).
- Size: `954,368` state numbers over the 24 cases (four arrays per sample), about 21 MB of JSON text per run, 1/9.4 of the single-stride form.
- Pass tests: every committed state's index is at most the certified `cfl_run_max` of its case (at most `0.44999792`, `_tend500`), so the history `cfl.max` predicate holds on any sample. The trade: a committed state between samples is seen by `cfl.max` only at the final capture.
- `tests.md` §3 carries the per-case form of gate (m): `- history: every = (history.every) <case_id> = <M>, ...`, every case listed once.

## 2. Per-case `initial_state` and `derived`

`initial_state` transcribes `shallow_water2d_checks.f90` `setup_state` (steps 01-02) per case, as expressions over `inputs.*`, the coordinates `x`, `y` and `pi`, keeping the Fortran parenthesization:
- `z_b`: the cone `0.2 * H_0 * max(0, 1 - sqrt(ddx * ddx + ddy * ddy) / (min(L_x, L_y) / 6))` with `ddx = min(abs(x - x_c_fraction * L_x), L_x - abs(x - x_c_fraction * L_x))` (and `ddy` likewise) on the `williamson_tc5_cone` cases; the literal `0.0` on the `flat` cases.
- `h`: `H_0 + eta0 * s - (z_b)` (`H_0 + eta0 * s` on flat; `H_0 - (z_b)` on the lake), `s` = `sin(2 * pi * (x / L_x - shift_x_fraction))` (`linear_wave_x`), the same in `y` (`linear_wave_y`), `sin(2 * pi * (x / L_x + y / L_y - shift_x_fraction - shift_y_fraction))` (`oblique_mode`).
- `hu` / `hv`: `(h) * (wave_sign * (eta0 * sqrt(g * H_0) / H_0) * s)` on the along-wave component; the literal `0.0` on the other one, on `oblique_mode` and on the lake (the certified `h * 0.0` and `0.0` are both `+0.0`).
- The shifts, mirrors and axis exchange need no transformed formula (unlike coriolis): they are carried by the case's own inputs (`wave_sign`, `shift_*_fraction`, `x_c_fraction`, `y_c_fraction`, `initial_profile`), exactly as the certified checks module read them.
- Two forms are forced by the evaluator: `initial_state` admits no `bind` and no `initial.*`, so `h`, `hu`, `hv` inline the whole `z_b` expression (long, duplicated); and the squares are written `ddx * ddx`, not `ddx ** 2`, because the grammar's `**` is `np.power`, which is not correctly rounded for exponent 2 (measured: 27,946 of 100,000 random doubles differ from `a * a`); with `** 2` the cone cases differed from the certified capture by 1 ulp in up to 234 cells.

`derived`, in order, transcribes the checks module's `case_run` dt rule (IR step 04, `tests.md` §3):
`dx = inputs.grid.L_x / inputs.grid.nx`, `dy = …`, `lam0 = max((abs(initial.hu / initial.h) + sqrt(g * initial.h)) / derived.dx + (abs(initial.hv / initial.h) + sqrt(g * initial.h)) / derived.dy)`, `dt_raw = dt_scale * cfl_target / derived.lam0`, `n_step = ceil((t_end - t_start) / derived.dt_raw)`, `dt = (t_end - t_start) / derived.n_step`. `lam0` and `dt_raw` are payload entries no binding reads. `derived.n_step` is a float with an integral value (`ceil`); whether the host evaluator's `loop.n_step` check accepts an integral float is the evaluator's to confirm.

## 3. Schema and evidence

- `schema.variables`: `h`, `hu`, `hv`, `z_b` (`[nx, ny]`), `ok` (`scalar`, the logical result state; no `initial_state` entry). Every `source` key is removed; `n_step`, `cfl_run_max`, `h_min_run` are removed (their capture is gone).
- `required_evidence`: `state_snapshots` (unchanged otherwise), `{artifact: state_history}`, `{artifact: step_series, names: [t, step, h_min, h_max, hu_min, hu_max, hv_min, hv_max, z_b_min, z_b_max]}` (gate (h): every array state, `z_b` included).
- `io_contract.outputs`: `n_step`, `cfl_run_max`, `h_min_run` removed, `ok` added; `io_contract.inputs` gains `every`; `semantic_dependency.required_sources` loses `n_step`.
- `algorithm` (structured parts reconciled, prose kept): the description's sentence on the published operation is rewritten for `__step`; steps `step_03_initial_validation`, `step_05_run_diagnostics_init`, `step_22_commit_diagnostics` deleted (from `steps`, `ordering`, `iteration_contract.body_steps`); steps 01, 02, 04 marked host-evaluated, step 02's outputs `h, hu, hv`, step 04's `n_step_target` renamed `n_step`; `control_condition`, `iteration_contract.stop_condition` / `step_count_rule`, `update_semantics.target_variables` (`h, hu, hv, ok`), `temporaries` (minus `setup_ok`, `lam0`, `dt_raw`, `n_step_target`, `dt`), and the `t` / `cells_updated` rules rewritten for the driver. The remaining prose of steps 06-21 is the certified text.

## 4. Predicates (310 → 299)

Old → new, per quantity; every predicate not listed is byte-identical to the certified IR (the cross-target, convergence, error, band, symmetry, lake, mass and momentum predicates).

| quantity | certified predicate | converted |
|---|---|---|
| `cfl.max` (13 tests, ×2) | `dt * max(lam0, lam1)` with binds `gg, dx, dy, lam0, lam1, dt_raw, nst, dt` (lam0 over `initial.*`, lam1 over `final.*`) | `derived.dt * max((abs(final.hu / final.h) + sqrt(inputs.constants.g * final.h)) / derived.dx + (abs(final.hv / final.h) + sqrt(inputs.constants.g * final.h)) / derived.dy)`, same `op` / `value` |
| | `final.cfl_run_max` | `derived.dt * max(smax(<the same index over history.h, history.hu, history.hv>))`, same `op` / `value` |
| `extrema.h.min` (12 pass tests) | `final.h_min_run` `ge 0.05` | `min(series.h_min)` `ge 0.05` |
| | `min(min(initial.h), min(final.h))` | unchanged |
| `run.n_step` (12 pass tests) | `abs(final.n_step - nst) le 0.5` with the dt-rule binds | gate (l)'s fixed form `final.step - derived.n_step`, `op: eq`, `value: 0`, `per_case: true`, all target cases of the test (asserted for every `pass` test) |
| `errors.initial_zb.linf_rel` (8 tests, 11 predicates) | `maxabs(initial.z_b - zref) / hs le 1e-12` | deleted (§5) |

- The `lam0` half of the old `cfl.max` end-point predicate is dropped: the index of the initial state is host data (`initial_state`), not kernel state, and the dt rule bounds it by `dt_scale * cfl_target`.
- The guard (`l0_cfl_guard_xfail`) keeps `cfl.max` alone, in the failure direction (`gt 1.0`) over `final.*` and over `history.*` (plan §3.3 "The guard"); no `run.n_step` and no step-count predicate (6-7 does not judge the step count; `xfail` is outside gate (l)).
- `min(series.h_min)` covers the committed states 1..n; the initial state, covered by the certified `h_min_run`, is host data and remains covered by the unchanged `min(min(initial.h), min(final.h))`.

## 5. Deleted, and what does not survive

- **Decision 7**: the initial-state judgment of this node is the quantity `errors.initial_zb.linf_rel` (no whole test of this node judges only the initial state): 11 predicates deleted, and the quantity removed from the `quantities` of the 8 tests that listed it (`l1_refinement_mass_and_positivity`, `l2_lake_at_rest_invariance`, `l2_long_run_mass_conservation`, `l3_topography_forced_response_stability`, `l3_topography_time_refinement_pair`, `l3_axis_exchange_pairs`, `l3_x_mirror_pairs`, `l3_y_mirror_pairs`). No test is deleted; all 13 survive. It was the only predicate set that read no `final.*` / `comparand.*`.
- **Decision 8** (one-author dt rule): the certified `run.n_step` compared Compile's transcription with the KERNEL's count; now both `final.step` and `derived.n_step` are host facts, and the rule's value is reviewed by `Compile.verify` V3 (iv) alone. Pinned for consistency by the host's `t_end` check and gate (l).
- **Decision 15**: `cfl_run_max` and `h_min_run` deleted. A committed state between two history samples is no longer seen by `cfl.max` (the certified `cfl_run_max` saw every step); at `every = ceil(n_step / 2)` a pass case is sampled once or twice plus its final capture.
- **The setup refusal** (checks module `setup_state`, IR step 03): the wet-start check (`h > 0`, `eta > max` of the face neighbours' `z_b`), the `x_c_fraction` / `y_c_fraction` range check, the "cone with no `z_b > 0` cell" check and the unknown-profile refusal were the checks module's; no host gate re-checks them. On every case of this set they hold (the certified run set up every case). An unknown profile or topography cannot be expressed at all (the expression is chosen per case).
- `tests.md`: every `reported:` line deleted (13); 2-9, 2-10 (second paragraph replaced), 2-11, §1, §3 (the captured state, the `history:` line), 5-2, 5-3, 5-4 (`run.n_step`, the `cfl.max` / `extrema.h.min` paragraph, the initial-topography definition removed), 5-5 and §6 (quantities, `reported`, the initial-topography bullets, the `run.n_step` parenthetical, 6-7's `CFL` bullet) edited. `controlled_spec.md`: §5 only.

## 6. Verification (numpy + `tools/primary_evidence.py`'s grammar-5 evaluator on the IR's own strings)

Each `initial_state` string evaluated with `coordinate_arrays(schema, inputs, None)` (unexpanded), the leading-1 drop and `broadcast_to [nx, ny]`, against `raw/state_snapshots/initial/<case>.json`; `derived` evaluated in order with the materialized arrays as `initial.*`; the dt the model used recomputed from the certified initial capture by the checks module's `case_run` arithmetic.

| case | max \|Δ\| h, hu, hv, z_b | `derived.n_step` = certified `n_step` | `derived.dt` = model dt | final CFL (converted) |
|---|---|---|---|---|
| all 24 cases | `0` (bit-identical, every cell) | yes (90, 179, 223, 357, 714, 4459, guard 38) | yes (bitwise) | ≤ `0.44978` on the 23 pass cases |
| `swe2d_guard_n032_sx000_sy000_dts240` | `0` | 38 = 38 | yes | `1.055733964672891` (plan §12 J's figure) |

- `t_start + derived.n_step * derived.dt` equals the certified final `t` bitwise on every case (guard `0.19999999999999998`); the `t_end` check `|t_start + n_step·dt − t_end| <= 4·spacing(t_end)` holds on every case (largest residual `2.8e-17`, guard).
- `yaml.safe_load` loads the IR; the `quantities` lines of `tests.md` §6 equal the IR's `tests[].quantities` for all 13 tests; every predicate quantity (minus `cross_target_state_agreement`) is listed.
- Not run: the history, series and `final.step` predicates (no capture of the new shape exists before PR-1b's driver).
