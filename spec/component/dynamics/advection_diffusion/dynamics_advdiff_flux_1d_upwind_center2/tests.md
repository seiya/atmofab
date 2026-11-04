# Tests: 1D advection-diffusion flux (L0)

## 0. Meta information
- `test_profile_id`: `dynamics_advdiff_flux_1d_upwind_center2_l0`
- `test_profile_version`: `0.6.0`
- `status`: `draft`
- `spec_ref.spec_kind`: `component`
- `spec_ref.spec_id`: `dynamics_advdiff_flux_1d_upwind_center2`
- `spec_ref.spec_version`: `0.4.0`
- `spec_ref.controlled_spec_path`: `spec/component/dynamics/advection_diffusion/dynamics_advdiff_flux_1d_upwind_center2/controlled_spec.md`

## 1. Test purpose
This suite verifies the published `operation` `dynamics_advdiff_flux_1d_upwind_center2__compute_flux` at `L0`: the advective/diffusive flux consistency for a constant field, the uniformity of the diffusive flux for a linear field, the face-by-face agreement with the §3 formulas for a non-constant field at each sign of `a`, the agreement of the two periodic seam faces, the mirror pair, the smallest valid extent, and the input guard for each invalid input of the Controlled Spec §4 alone, at its boundary and inside it (`a=0`; `dx=0` and `dx<0`; `dt=0` and `dt<0`; `nx=1`; `ng=0`).

The operation admits three transformations (`docs/TESTS.md` §Symmetry pairs): the mirror, the sign reversal and the translation. The suite carries the mirror as a pair and states why it does not carry the other two. The **mirror** $x \to L - x$ reverses the advection velocity, $a \to -a$, and the ghost-extended field, $u'_j = u_{nx-1-j}$ (element `k` of `u'` is element `nx+2*ng+1-k` of `u`). Face $j+1/2$ lies at $x=(j+1)\,dx$, which the mirror maps to $(nx-1-j)\,dx$, face $(nx-2-j)+1/2$: element `j + 2` of a flux array maps to element `nx - j`, so the `nx + 1` faces are reversed. A face flux is a component along $x$ and changes sign under the mirror: at the image face $j' = nx-2-j$, §3 gives $a'\,u'_{j'+1} = -a\,u_j$ (the transformed `a<0` path against the base's `a>0` path), $a'\,u'_{j'} = -a\,u_{j+1}$ (the converse), and $u'_{j'+1}-u'_{j'} = -(u_{j+1}-u_j)$, so the transformed case's outputs are `flux_adv' = -flip(flux_adv)` and `flux_dif' = -flip(flux_dif)` of the base. Each base of the pair is a fixed-value case of one sign of `a`, so each pair runs the path of the other sign on the reversed field. The **sign reversal** $u \to -u$ is admitted because §3 is linear in `u`, and it is not carried: the one class of defect it separates from the formula cases — a kernel that chooses the upwind side from the sign of `u` (or of `a*u`) instead of the sign of `a` — is seen by `l0_face_formula_aneg`, whose field changes sign (§6). The **translation** is not a pair here: the flux at each face reads the two cells beside it, so a translated field gives the translated fluxes by §3 alone, and the cases' fields are not periodic. A one-dimensional operation has no axis exchange.

## 2. Input-defaulting rules
- Every case declares `nx`, `ng`, `a`, `nu`, `dx`, `dt` as inputs, and the `operation` receives each as the argument of that name.
- The normal cases use `nx>=2`, `ng=1` (unless a case states another `ng`), `a/=0` (`a>0` unless a case states another `a`), `nu>=0`, `dx>0`, `dt>0`:
  - `l0_constant_state`: `nx=4`, `ng=1`, `a=1.5`, `nu=0.1`, `dx=0.25`, `dt=0.1`, `u = u_const = 2.0` at every element, ghost cells included; the case declares `u_const` as an input.
  - `l0_linear_state`: `nx=4`, `ng=1`, `a=1.0`, `nu=0.1`, `dx=0.25`, `dt=0.1`, `u = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0]`.
  - `l0_periodic_seam`: `nx=4`, `ng=1`, `a=1.0`, `nu=0.1`, `dx=0.25`, `dt=0.1`, `u = [0.5, 1.0, 3.0, 2.0, 0.5, 1.0]`. The case declares the selectors `seam.left_select = [1, 0, 0, 0, 0]` and `seam.right_select = [0, 0, 0, 0, 1]` as inputs, the two end faces of §5.
  - `l0_min_extent`: the smallest valid grid, `nx=2`, `ng=1`, with `a=2.0`, `nu=0.3`, `dx=0.5`, `dt=0.1`, `u = [0.3, 1.7, 0.2, 2.9]`: a guard written `nx<3` or `ng<2` refuses it.
- The input field is supplied directly as the ghost-extended array of `nx + 2*ng` values in the layout of `controlled_spec.md` §2; the `L0` suite does not call the boundary `component`.
- Every case derives the extent of `u` as the scalar `n_u = nx + 2*ng`, which sizes the state `u`: the Controlled Spec §5.1 gives `u` no `dims`.
- The fixed-value cases are `l0_face_formula` (`a>0`) and `l0_face_formula_aneg` (`a<0`), each with the inputs its judgment of §6 states, and one transformed copy of each, sorting after its base:
  - `l0_face_formula_amir`: `nx=4`, `ng=2`, `a=-2.0`, `nu=0.3`, `dx=0.5`, `dt=0.1`, `u = [2.2, 3.6, 0.4, 1.1, 2.9, 0.2, 1.7, 0.3]` (the field of `l0_face_formula` reversed, and `a` reversed).
  - `l0_face_formula_aneg_amir`: `nx=5`, `ng=2`, `a=1.5`, `nu=0.2`, `dx=0.25`, `dt=0.1`, `u = [-2.2, 1.8, 3.1, -0.7, 0.9, 2.4, -1.3, 0.6, 2.7]` (the field of `l0_face_formula_aneg` reversed, and `a` reversed).
- The abnormal cases each violate one clause of the Controlled Spec §4 alone. They use `nx=4`, `ng=1`, `a=1.0`, `nu=0.1`, `dx=0.25`, `dt=0.1`, `u = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]` but for one change:
  - `l0_invalid_a`: `a=0`.
  - `l0_invalid_dx_zero`: `dx=0`, the clause's boundary. `l0_invalid_dx_negative`: `dx=-0.25`.
  - `l0_invalid_dt_zero`: `dt=0`, the clause's boundary. `l0_invalid_dt_negative`: `dt=-0.1`.
  - `l0_invalid_nx`: `nx=1`, the clause's boundary, with `u = [1.0, 2.0, 3.0]`.
  - `l0_invalid_ng`: `ng=0`, the clause's boundary, with `u = [1.0, 2.0, 3.0, 4.0]`.
- Before the call the state holds `u` as above, and `flux_adv` and `flux_dif` hold the literal `-999` at every element, their pre-call content: both are operation-only outputs, so an element the `operation` does not write is captured as `-999` and fails every judgment of §6 over that output.

## 3. Execution-control rules
`N/A`: this `component` exposes a single pointwise `operation` and defines no time-stepping or iteration. Execution control is the responsibility of the time-update `component` and the `problem` runner.

## 4. Case-expansion rules
`N/A`: the `L0` suite uses fixed single-stencil inputs and defines no `case` sweep. Case expansion is defined at the `problem` level.

## 5. Judged quantities
A test is judged by the host's predicates over the captures alone (`docs/TESTS.md` requirement 9). Each quantity below is named on the `quantities` line of the tests that judge it, and is evaluated from the state captured after the run in each of the test's target cases.

The host-rendered driver calls the `operation` once per case on the case's state, and the state captured before and after that call consists of `u` (`nx + 2*ng` values, the ghost-extended input of §2), `flux_adv` and `flux_dif` (`nx + 1` values each, the pre-call content `-999` of §2 before the call) and `guard_pass`. All four are arguments of the `operation`. `guard_pass` is its logical result, which the driver writes into the state after the call as `1` when the call returned true and `0` when it returned false; it starts at `0`, so a result the `operation` never assigned captures `0`. An output array of a rejected input is not defined by the Controlled Spec §4, so the flag is the only state that distinguishes a rejection from an executed operation.

- `guard_pass`: the captured flag. It is `== 1` on the cases of a `pass` test and `== 0` on the cases of an `xfail` test.
- `flux_adv_maxdev`: `max|flux_adv - flux_adv_expected|` over the `nx + 1` faces, where `flux_adv_expected` is the value the case's judgment of §6 states: `a*u_const` at every face on `l0_constant_state`, the listed values on `l0_face_formula`, `l0_face_formula_aneg` and `l0_min_extent`.
- `flux_dif_maxdev`: `max|flux_dif - flux_dif_expected|` over the `nx + 1` faces, where `flux_dif_expected` is the value the case's judgment of §6 states: `0` at every face on `l0_constant_state`, the listed values on `l0_face_formula`, `l0_face_formula_aneg` and `l0_min_extent`.
- `flux_dif_spread`: `max(flux_dif) - min(flux_dif)` over the `nx + 1` faces.
- `seam_adv_diff`: `|flux_adv(1) - flux_adv(nx+1)|`, the difference of the left and right seam faces, which the host evaluates as `|sum(seam.left_select * flux_adv) - sum(seam.right_select * flux_adv)|` with the selectors of §2.
- `seam_dif_diff`: `|flux_dif(1) - flux_dif(nx+1)|`, the difference of the left and right seam faces, evaluated the same way over `flux_dif`.
- `pair_residual_adv`: on a transformed case, `norm2(flux_adv - T(flux_adv_base)) / norm2(flux_adv_base)` over the `nx + 1` faces, where `flux_adv_base` is the base case's `flux_adv` captured after its run and `T(q) = -flip(q, 0)` (§1: the faces reversed and the sign reversed). The host reads the base through `at('<base>')` and applies `flip` (`docs/workflow/phases/phase_01_compile.md`).
- `pair_residual_dif`: the same expression over `flux_dif`: `norm2(flux_dif - T(flux_dif_base)) / norm2(flux_dif_base)`.
- `cross_target_state_agreement`: this target's run compared with every other target's certified variant of this node, case by case (`docs/TESTS.md` §Cross-target judgment): for one output `q` captured after the run and the same output `q_ref` of the same case in the other variant, `max|q - q_ref| / max(max|q|, max|q_ref|, 1)` over every element. The host evaluates it from the captured state, against each other variant separately; on a target with no other certified variant it holds vacuously. It is outside the `quantities` line.

## 6. Test definitions
- `test_id`: `l0_constant_state_flux_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_advdiff_flux_1d_upwind_center2__compute_flux`
  - `expected_outcome`: `pass`
  - `quantities`: `flux_adv_maxdev`, `flux_dif_maxdev`, `guard_pass`
  - `judgment`: on `l0_constant_state`, a constant-field input (ghost cells included), `guard_pass == 1` and `flux_dif=0` and `flux_adv=a*u_const` at all `nx + 1` faces, each within an absolute tolerance of `1e-12` (`flux_adv_maxdev <= 1e-12` and `flux_dif_maxdev <= 1e-12`). The cross-target judgment is applied: `cross_target_state_agreement` over `flux_adv` and `flux_dif` is `<= 1e-12`.
- `test_id`: `l0_linear_state_diff_flux_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_advdiff_flux_1d_upwind_center2__compute_flux`
  - `expected_outcome`: `pass`
  - `quantities`: `flux_dif_spread`, `guard_pass`
  - `judgment`: on `l0_linear_state`, a linear-field input (ghost cells included, so the field needs no periodicity), `guard_pass == 1` and `flux_dif` is uniform over all `nx + 1` faces: `max(flux_dif) - min(flux_dif) <= 1e-12` (`flux_dif_spread <= 1e-12`). The cross-target judgment is applied: `cross_target_state_agreement` over `flux_adv` and `flux_dif` is `<= 1e-12`.
- `test_id`: `l0_face_formula_flux_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_advdiff_flux_1d_upwind_center2__compute_flux`
  - `expected_outcome`: `pass`
  - `quantities`: `flux_adv_maxdev`, `flux_dif_maxdev`, `guard_pass`
  - `judgment`: on `l0_face_formula`, with the fixed inputs `nx=4`, `ng=2`, `a=2.0`, `nu=0.3`, `dx=0.5`, `dt=0.1`, and the ghost-extended field `u = [0.3, 1.7, 0.2, 2.9, 1.1, 0.4, 3.6, 2.2]` (elements `1 … 8`, i.e. $u_{-2}\dots u_{5}$; $u_j$ is element `j + 3`), `guard_pass == 1` and the outputs equal `flux_adv = [3.4, 0.4, 5.8, 2.2, 0.8]` and `flux_dif = [0.9, -1.62, 1.08, 0.42, -1.92]` (element `j + 2` is face $j+1/2$, $j=-1,\dots,3$, from the §3 formulas) within an absolute tolerance of `1e-12` (`flux_adv_maxdev <= 1e-12` and `flux_dif_maxdev <= 1e-12`). On these values every omission of `a`, `nu`, or `1/dx`, a reversed sign, a face shifted by one or two cells, an ignored `ng`, and every pairwise combination of these deviates by at least `0.9` at some face. The cross-target judgment is applied: `cross_target_state_agreement` over `flux_adv` and `flux_dif` is `<= 1e-12`.
- `test_id`: `l0_face_formula_negative_a_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_advdiff_flux_1d_upwind_center2__compute_flux`
  - `expected_outcome`: `pass`
  - `quantities`: `flux_adv_maxdev`, `flux_dif_maxdev`, `guard_pass`
  - `judgment`: on `l0_face_formula_aneg`, with the fixed inputs `nx=5`, `ng=2`, `a=-1.5`, `nu=0.2`, `dx=0.25`, `dt=0.1`, and the ghost-extended field `u = [2.7, 0.6, -1.3, 2.4, 0.9, -0.7, 3.1, 1.8, -2.2]` (elements `1 … 9`, i.e. $u_{-2}\dots u_{6}$; $u_j$ is element `j + 3`; the outer ghost cells $u_{-2}$ and $u_6$ are read by no face, so an `a<0` path that ignores `ng` reads a shifted cell), `guard_pass == 1` and the outputs equal `flux_adv = [1.95, -3.6, -1.35, 1.05, -4.65, -2.7]` ($a\,u_{j+1}$) and `flux_dif = [1.52, -2.96, 1.2, 1.28, -3.04, 1.04]` (element `j + 2` is face $j+1/2$, $j=-1,\dots,4$, from the §3 formulas) within an absolute tolerance of `1e-12` (`flux_adv_maxdev <= 1e-12` and `flux_dif_maxdev <= 1e-12`). On these values (measured) an advection flux that reads the `a>0` side $u_j$, chooses the side from the sign of `u_{j+1}` or of `a*u_j`, addresses $u_{j+1}$ at its `ng=1` position (ignoring `ng`), or reads the cell one face back deviates by `5.7` in `flux_adv`, and one that reads the cell one face forward by `6.0`; one that uses `|a|` or reverses the sign deviates by `9.3`, and one that omits `a` by `7.75`. A diffusion flux that omits `1/dx` deviates by `2.28` in `flux_dif`, one that omits `nu` by `12.16`, one with a reversed sign by `6.08`, and one shifted by one face by `4.48`. The cross-target judgment is applied: `cross_target_state_agreement` over `flux_adv` and `flux_dif` is `<= 1e-12`.
- `test_id`: `l0_min_extent_flux_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_advdiff_flux_1d_upwind_center2__compute_flux`
  - `expected_outcome`: `pass`
  - `quantities`: `flux_adv_maxdev`, `flux_dif_maxdev`, `guard_pass`
  - `judgment`: on `l0_min_extent` (`nx=2`, `ng=1`, the inputs of §2), `guard_pass == 1` and the outputs equal `flux_adv = [0.6, 3.4, 0.4]` and `flux_dif = [-0.84, 0.9, -1.62]` (element `j + 2` is face $j+1/2$, $j=-1,\dots,1$, from the §3 formulas) within an absolute tolerance of `1e-12` (`flux_adv_maxdev <= 1e-12` and `flux_dif_maxdev <= 1e-12`). The cross-target judgment is applied: `cross_target_state_agreement` over `flux_adv` and `flux_dif` is `<= 1e-12`.
- `test_id`: `l0_periodic_seam_flux_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_advdiff_flux_1d_upwind_center2__compute_flux`
  - `expected_outcome`: `pass`
  - `quantities`: `guard_pass`, `seam_adv_diff`, `seam_dif_diff`
  - `judgment`: on `l0_periodic_seam`, a non-constant interior field with periodic ghost cells $u_{-1}=u_{nx-1}$, $u_{nx}=u_0$, `guard_pass == 1` and the left seam face $F_{-1/2}$ (element `1`) and the right seam face $F_{nx-1/2}$ (element `nx + 1`) are exactly equal in `flux_adv` and in `flux_dif` (`seam_adv_diff == 0` and `seam_dif_diff == 0`). The two faces are the same expression applied to the same values, so a nonzero difference means one seam face was produced by a different path, or is missing. The cross-target judgment is applied: `cross_target_state_agreement` over `flux_adv` and `flux_dif` is `<= 1e-12`.
- `test_id`: `l0_mirror_pair_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_advdiff_flux_1d_upwind_center2__compute_flux`
  - `expected_outcome`: `pass`
  - `quantities`: `guard_pass`, `pair_residual_adv`, `pair_residual_dif`
  - `judgment`: `guard_pass == 1` on `l0_face_formula_amir`, `l0_face_formula_aneg_amir` and their bases, and on the two transformed cases the residual against `l0_face_formula` and `l0_face_formula_aneg` respectively is `<= 1.0e-12` for `flux_adv` and for `flux_dif` (`pair_residual_adv <= 1.0e-12` and `pair_residual_dif <= 1.0e-12`). Every product of the transformed case is the base's product with the sign of one factor reversed, and every difference the base's difference negated, so a conforming kernel gives exactly `0` (measured `0` on both pairs and both arrays); the bound is the suite's cross-target bound, not a measured round-off. Measured with an independent reference of §3: a kernel whose `a<0` path addresses $u_{j+1}$ at its `ng=1` position (ignoring `ng`) gives `1.36` in `flux_adv` on `l0_face_formula_amir` and `1.43` on `l0_face_formula_aneg_amir` (and fails `l0_face_formula_aneg`, `5.7`); a kernel that reads $u_j$ for either sign gives `1.36` and `1.43` in `flux_adv` on the two pairs; one that uses `|a|` gives `2.0` on both; one that chooses the side from the sign of `a*u_j` gives `2.02` on `l0_face_formula_aneg_amir`. These are advection defects, and the `flux_dif` residual of each is `0`. The cross-target judgment is applied: `cross_target_state_agreement` over `flux_adv` and `flux_dif` is `<= 1e-12`.
- `test_id`: `l0_invalid_a_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_advdiff_flux_1d_upwind_center2__compute_flux`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `a==0`
  - `quantities`: `guard_pass`
  - `judgment`: on `l0_invalid_a` (`a=0`), the `operation` refuses the input: `guard_pass == 0`, in the failure direction. The cross-target judgment is not applied.
- `test_id`: `l0_invalid_dx_zero_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_advdiff_flux_1d_upwind_center2__compute_flux`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `dx=0`
  - `quantities`: `guard_pass`
  - `judgment`: on `l0_invalid_dx_zero` (`dx=0`, every other input normal), the `operation` refuses the input: `guard_pass == 0`, in the failure direction. The cross-target judgment is not applied.
- `test_id`: `l0_invalid_dx_negative_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_advdiff_flux_1d_upwind_center2__compute_flux`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `dx<0`
  - `quantities`: `guard_pass`
  - `judgment`: on `l0_invalid_dx_negative` (`dx=-0.25`, every other input normal), the `operation` refuses the input: `guard_pass == 0`, in the failure direction. The cross-target judgment is not applied.
- `test_id`: `l0_invalid_dt_zero_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_advdiff_flux_1d_upwind_center2__compute_flux`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `dt=0`
  - `quantities`: `guard_pass`
  - `judgment`: on `l0_invalid_dt_zero` (`dt=0`, every other input normal), the `operation` refuses the input: `guard_pass == 0`, in the failure direction. The cross-target judgment is not applied.
- `test_id`: `l0_invalid_dt_negative_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_advdiff_flux_1d_upwind_center2__compute_flux`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `dt<0`
  - `quantities`: `guard_pass`
  - `judgment`: on `l0_invalid_dt_negative` (`dt=-0.1`, every other input normal), the `operation` refuses the input: `guard_pass == 0`, in the failure direction. The cross-target judgment is not applied.
- `test_id`: `l0_invalid_ng_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_advdiff_flux_1d_upwind_center2__compute_flux`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `ng=0` with `nx=4` (input size `nx + 2*ng = 4`)
  - `quantities`: `guard_pass`
  - `judgment`: on `l0_invalid_ng` (`ng=0`, `nx=4`), the `operation` refuses the input: `guard_pass == 0`, in the failure direction. The cross-target judgment is not applied.
- `test_id`: `l0_invalid_nx_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_advdiff_flux_1d_upwind_center2__compute_flux`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `nx=1` with `ng=1` (input size `3`, output size `2`)
  - `quantities`: `guard_pass`
  - `judgment`: on `l0_invalid_nx` (`nx=1`, `ng=1`), the `operation` refuses the input: `guard_pass == 0`, in the failure direction. The cross-target judgment is not applied.

## 7. Pass/fail aggregation rules
- `per_test.pass_rule`: `pass` when every predicate of the test holds.
- `per_test.xfail_rule`: `xfail` when `xfail_condition` is true and every predicate of the test holds.
- `suite.pass_rule`: `pass` when all `test_id` are `pass` or `xfail`.

## 8. Traceability
- Record `test_profile_id` and `test_profile_version` in `trial_meta.json`.
