# Kessler measurement: offline driver and comparison

## Status
A measurement record of [issue #578](https://github.com/seiya/atmofab/issues/578), item 2 of `docs/design/scale_migration.md` §Order. It belongs to no suite and no workflow phase, and nothing in `tools/` reads it.

**Placement exception.** `docs/DEVELOPMENT.md` §Record placement has no row for measurement code. This directory is the exception, placed beside the note it feeds, for as long as the two nodes it measures live in this repository: `TODO.md` carries the removal condition. Direct compiler calls here are a development measurement; `AGENTS.md` §Build-runtime execution rules binds the workflow's `compile`.

## Source
`scale-met/scale-dev` at commit `dc58155` (SCALE 5.5.5). The tree is read only, and nothing is copied from it into this directory. The real files are `scale_precision`, `scale_const`, `scale_atmos_hydrometeor`, `scale_atmos_saturation`, `scale_atmos_phy_mp_common` and `scale_atmos_phy_mp_kessler`, plus `scalelib/include`.

## Contents
- `Makefile`: builds the six real files, the six stubs and the driver. `SCALE_ROOT` is required; `BUILD` defaults to `build/`. `ALT_CONSTANTS=1` builds a copy of `scale_const` holding the suites' `alt_constants`, since the original holds them as compile-time parameters.
- `stubs/`: `scale_io`, `scale_prc`, `scale_prof`, `scale_file_history`, `scale_tracer` and `scale_atmos_grid_cartesC_index`. `scale_file_history` keeps the one field the Kessler adjustment writes, which the driver publishes as `pcsat`. `scale_prc` turns the abort into an error stop.
- `driver.F90`: runs one column through `dens2qsat_liq`, `saturation_adjustment`, `moist_conversion_dens_liq`, the Kessler adjustment or the terminal velocity, with `IA = JA = 1`.
- `compare.py`: the screen and the comparison. It reads the cases from the one fenced YAML block of each node's `tests.md` §2, so no second copy of them exists.
- `predicates_saturation.yaml`, `predicates_kessler.yaml`: the judgments of each `tests.md` §6, transcribed by hand into the primary-predicate grammar.

## Operations
```sh
make SCALE_ROOT=<tree> BUILD=<dir>/O2
make SCALE_ROOT=<tree> BUILD=<dir>/O0 FFLAGS="-O0 -cpp -ffree-line-length-none"
make SCALE_ROOT=<tree> BUILD=<dir>/O2alt ALT_CONSTANTS=1
make SCALE_ROOT=<tree> BUILD=<dir>/O0alt ALT_CONSTANTS=1 FFLAGS="-O0 -cpp -ffree-line-length-none"
python3 compare.py screen saturation --driver <dir>/O2/driver --comparand-driver <dir>/O0/driver --alt-driver <dir>/O2alt/driver --alt-comparand-driver <dir>/O0alt/driver --work <dir>/work
python3 compare.py screen kessler    --driver <dir>/O2/driver --comparand-driver <dir>/O0/driver --alt-driver <dir>/O2alt/driver --alt-comparand-driver <dir>/O0alt/driver --work <dir>/work
python3 compare.py compare kessler   --driver <dir>/O2/driver --alt-driver <dir>/O2alt/driver --run-dir <certified run node directory>
```

**Screen.** Every `pass` case goes through the driver, its columns become state snapshots, and `tools.primary_evidence.evaluate_primary_predicates` evaluates each judgment, the cross-target ones against the `-O0` build; `validate_primary_predicate_schema` reads the same predicates against the real test ids. An `xfail` case does not run: the original has no guard, so its snapshot is the refusal state the Controlled Spec defines, written by the script, and its evaluation shows only that the judgment is expressible and evaluable. Forcing is checked with a nonzero exit: a case's inputs meet a refusal clause of Controlled Spec §4 exactly when it is `xfail`, and a non-convergence case aborts in the original at cell 4. The screen establishes that a `pass` judgment is true of the original and evaluable; it does not establish that the original is correct, and it does not show a judgment separates a wrong kernel.

**Comparison.** The same driver columns against a certified run's `raw/state_snapshots/<case_id>.json`, at interior cells, every returned variable: `1e-12` relative with a `1e-15` absolute floor. `evaporate` is compared as `max(-pcsat, 0)`, because the droplet-number factor amplifies a round-off `pcsat`. A difference beyond the tolerance is a finding on the `spec` or on the generated code, never a tolerance change. The script prints the deviation list beside the maxima.

## Deviations from the original
Listed, not banded, in `DEVIATIONS` of `compare.py`, which the comparison prints, and in §1 of each node's Controlled Spec.

The driver supplies the `0` halos by initialising each output to `0`, since the original writes interior cells only, and supplies `guard_pass = 1` and `converged = 1` on a run that did not abort.
