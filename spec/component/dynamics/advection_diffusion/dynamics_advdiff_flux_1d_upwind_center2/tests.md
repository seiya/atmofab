# Tests: 1D advection-diffusion flux (L0)

## 0. Meta information
- `test_profile_id`: `dynamics_advdiff_flux_1d_upwind_center2_l0`
- `test_profile_version`: `0.2.0`
- `status`: `draft`
- `spec_ref.spec_kind`: `component`
- `spec_ref.spec_id`: `dynamics_advdiff_flux_1d_upwind_center2`
- `spec_ref.spec_version`: `0.3.0`
- `spec_ref.controlled_spec_path`: `spec/component/dynamics/advection_diffusion/dynamics_advdiff_flux_1d_upwind_center2/controlled_spec.md`

## 1. Test purpose
This suite verifies the published `operation` `dynamics_advdiff_flux_1d_upwind_center2__compute_flux` at `L0`: the advective/diffusive flux consistency for a constant field, the uniformity of the diffusive flux for a linear field, the face-by-face agreement with the §3 formulas for a non-constant field, the agreement of the two periodic seam faces, and the input guard for an invalid advection velocity (`a<=0`) and for invalid grid sizes (`ng<1`, `nx<2`).

## 2. Input-defaulting rules
- The normal case uses `nx>=2`, `ng=1` (unless a test states another `ng`), `a>0`, `nu>=0`, `dx>0`, `dt>0`.
- The input field is supplied directly as the ghost-extended array of `nx + 2*ng` values in the layout of `controlled_spec.md` §2; the `L0` suite does not call the boundary `component`.
- The abnormal cases use `a<=0`, `ng=0` (with `nx=4`), or `nx=1` (with `ng=1`), one condition per test with every other input normal.

## 3. Execution-control rules
`N/A`: this `component` exposes a single pointwise `operation` and defines no time-stepping or iteration. Execution control is the responsibility of the time-update `component` and the `problem` runner.

## 4. Case-expansion rules
`N/A`: the `L0` suite uses fixed single-stencil inputs and defines no `case` sweep. Case expansion is defined at the `problem` level.

## 5. Diagnostics contract
- Require outputting `checks.flux_adv_consistency`, `checks.flux_dif_consistency`, `checks.seam_consistency`, and `checks.input_guard` in `diagnostics.json`.
- When `guard_pass` is false, `flux_adv` and `flux_dif` are undefined (`controlled_spec.md` §4): only `checks.input_guard` is evaluated, and no consistency check is computed from those outputs.

## 6. Test definitions
- `test_id`: `l0_constant_state_flux_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_advdiff_flux_1d_upwind_center2__compute_flux`
  - `expected_outcome`: `pass`
  - `judgment`: with a constant-field input (ghost cells included), satisfy `flux_dif=0` and `flux_adv=a*u_const` at all `nx + 1` faces, each within an absolute tolerance of `1e-12` (component-wise max deviation `<= 1e-12`).
- `test_id`: `l0_linear_state_diff_flux_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_advdiff_flux_1d_upwind_center2__compute_flux`
  - `expected_outcome`: `pass`
  - `judgment`: with a linear-field input (ghost cells included, so the field needs no periodicity), `flux_dif` is uniform over all `nx + 1` faces: `max(flux_dif) - min(flux_dif) <= 1e-12`.
- `test_id`: `l0_face_formula_flux_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_advdiff_flux_1d_upwind_center2__compute_flux`
  - `expected_outcome`: `pass`
  - `judgment`: with the fixed inputs `nx=4`, `ng=2`, `a=2.0`, `nu=0.3`, `dx=0.5`, `dt=0.1`, and the ghost-extended field `u = [0.3, 1.7, 0.2, 2.9, 1.1, 0.4, 3.6, 2.2]` (elements `1 … 8`, i.e. $u_{-2}\dots u_{5}$; $u_j$ is element `j + 3`), the outputs equal `flux_adv = [3.4, 0.4, 5.8, 2.2, 0.8]` and `flux_dif = [0.9, -1.62, 1.08, 0.42, -1.92]` (element `j + 2` is face $j+1/2$, $j=-1,\dots,3$, from the §3 formulas) within an absolute tolerance of `1e-12` (component-wise max deviation `<= 1e-12`). On these values every omission of `a`, `nu`, or `1/dx`, a reversed sign, a face shifted by one or two cells, an ignored `ng`, and every pairwise combination of these deviates by at least `0.9` at some face.
- `test_id`: `l0_periodic_seam_flux_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_advdiff_flux_1d_upwind_center2__compute_flux`
  - `expected_outcome`: `pass`
  - `judgment`: with a non-constant interior field and periodic ghost cells $u_{-1}=u_{nx-1}$, $u_{nx}=u_0$, the left seam face $F_{-1/2}$ (element `1`) and the right seam face $F_{nx-1/2}$ (element `nx + 1`) are exactly equal in `flux_adv` and in `flux_dif` (difference `== 0`). The two faces are the same expression applied to the same values, so a nonzero difference means one seam face was produced by a different path, or is missing.
- `test_id`: `l0_invalid_a_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_advdiff_flux_1d_upwind_center2__compute_flux`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `a<=0`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`
- `test_id`: `l0_invalid_ng_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_advdiff_flux_1d_upwind_center2__compute_flux`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `ng=0` with `nx=4` (input size `nx + 2*ng = 4`)
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`
- `test_id`: `l0_invalid_nx_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_advdiff_flux_1d_upwind_center2__compute_flux`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `nx=1` with `ng=1` (input size `3`, output size `2`)
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`

## 7. Pass/fail aggregation rules
- `per_test.pass_rule`: `pass` when the judgment expression is satisfied.
- `per_test.xfail_rule`: `xfail` when `xfail_condition` is true and `pass_when` is satisfied.
- `suite.pass_rule`: `pass` when all `test_id` are `pass` or `xfail`.

## 8. Traceability
- Record `test_profile_id` and `test_profile_version` in `trial_meta.json`.
