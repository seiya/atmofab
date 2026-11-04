# Tests: 1D periodic-boundary mapping (L0)

## 0. Meta information
- `test_profile_id`: `dynamics_advection_diffusion_boundary_1d_periodic_copy_l0`
- `test_profile_version`: `0.5.0`
- `status`: `draft`
- `spec_ref.spec_kind`: `component`
- `spec_ref.spec_id`: `dynamics_advection_diffusion_boundary_1d_periodic_copy`
- `spec_ref.spec_version`: `0.2.1`
- `spec_ref.controlled_spec_path`: `spec/component/dynamics/advection_diffusion/dynamics_advection_diffusion_boundary_1d_periodic_copy/controlled_spec.md`

## 1. Test purpose
This suite verifies the published `operation` `dynamics_advection_diffusion_boundary_1d_periodic_copy__apply` at `L0`: the left and right periodic-wrap mapping of the ghost cells at the ghost widths `ng=1` and `ng=2`, the copy of the interior cells, the mirror pair, the smallest valid `nx`, a large `nx`, and the input guard for each invalid input of the Controlled Spec §4 alone at its boundary (`nx<2`, `ng<1`).

The mapping admits three transformations (`docs/TESTS.md` §Symmetry pairs): the mirror, the translation and the sign reversal. The suite carries the mirror as a pair and states why it does not carry the other two. The **mirror** $x \to L - x$ reverses the ghost-extended field (element `k` of `u_in'` is element `nx+2*ng+1-k` of `u_in`); the operation takes no velocity, so no sign is reversed. Under the reversal the left ghost $u'_{-1}$ takes $u'_{nx-1} = u_0$, which is the base's right ghost, so the transformed `u_out` is the base's `u_out` reversed. The base's `u_out` is pinned element by element by the two wrap judgments of §6, each of which also requires the interior copy; the pair carries that to the reversed input, whose left ghost wraps from what was the right interior cell and whose right ghost from what was the left one. The **translation** along the periodic axis is admitted by the mapping but is not a transformation of the ghost-extended array: a cyclic shift of the `nx + 2*ng` elements is not the shift of the periodic interior, whose period is `nx`. The suite carries no translation pair; the `problem` that composes this mapping carries the translation of the whole state. The **sign reversal** $u \to -u$ is admitted because the mapping copies values, and it is not carried: it leaves every position in place, so on the base, whose every element the wrap judgments pin, it separates no kernel from a conforming one. A one-dimensional mapping has no axis exchange.

## 2. Input-defaulting rules
- Every case declares `nx` and `ng` as inputs, and `nx_total` as an input and as the `derived` value `nx_total = nx + 2*ng`, the extent of `u_in` and `u_out`: the state variables' shape is `[nx_total]`, which the `derived` value resolves, because the `operation` has no extent argument of that name.
- The normal cases are `l0_periodic`, its transformed copy `l0_periodic_xmir`, which sorts after it, `l0_periodic_ng2`, `l0_min_nx` and `l0_large_nx`. `l0_periodic` uses `nx=4`, `ng=1`, and `u_in = [-999, 0.3, 1.7, 0.2, 2.9, -999]`: the interior values are pairwise distinct, so that every ghost cell identifies the interior cell it was mapped from, and the ghost cells hold a sentinel value (`-999`) that no interior cell holds, so that a ghost cell of `u_in` read by mistake is visible in the output. With `nx` even, no element of the six is its own image under the reversal. `l0_periodic_xmir` uses `nx=4`, `ng=1`, and `u_in = [-999, 2.9, 0.2, 1.7, 0.3, -999]` (the field of `l0_periodic` reversed). `l0_periodic_ng2` uses `nx=4`, `ng=2`, and `u_in = [-999, -999, 0.3, 1.7, 0.2, 2.9, -999, -999]`: the interior of `l0_periodic` with two ghost cells at each end, so that the ghost width enters every index of the mapping. `l0_min_nx` uses `nx=2`, `ng=1`, and `u_in = [-999, 0.3, 1.7, -999]`, the smallest valid `nx`: a guard written `nx<=2` refuses it. `l0_large_nx` uses `nx=256`, `ng=1`, with `u_in(i) = i` at the interior elements `i` in `2..257` and `-999` at the two ghost cells: an upper limit on `nx` that no clause of the Controlled Spec §4 states refuses it, and the `problem` that composes this mapping runs at such extents.
- The abnormal cases each violate one clause of the Controlled Spec §4 alone, at its boundary. `l0_invalid_nx` uses `nx=1`, `ng=1`, and `u_in = [-999, 0.5, -999]`. `l0_invalid_ng` uses `nx=4`, `ng=0`, and `u_in = [0.3, 1.7, 0.2, 2.9]`; every element of its field is interior.
- Before the call the state holds `u_in` as above and `u_out` holds the literal `-999` at every element, its pre-call content: `u_out` is an operation-only output, so an element the `operation` does not write is captured as `-999` and fails every judgment of §6 over `u_out`.

## 3. Execution-control rules
`N/A`: this `component` applies a boundary mapping to a provided field and defines no time-stepping or iteration. Execution control belongs to the `problem` that composes this `operation`.

## 4. Case-expansion rules
`N/A`: the `L0` suite uses fixed inputs and defines no `case` sweep. Case expansion is defined at the `problem` level.

## 5. Judged quantities
A test is judged by the host's predicates over the captures alone (`docs/TESTS.md` requirement 9). Each quantity below is named on the `quantities` line of the tests that judge it, and is evaluated from the state captured after the run in each of the test's target cases.

The host-rendered driver calls the `operation` once per case on the case's state, with `nx` and `ng` from the case's inputs and `u_in`, `u_out`, `guard_pass` bound to the states of those names. The state captured before and after that call consists of `u_in` (the ghost-extended input of §2), `u_out` (`nx + 2*ng` values, the pre-call content `-999` of §2 before the call) and `guard_pass`. All three are arguments of the `operation`. `guard_pass` is its logical result, which the driver writes into the state after the call as `1` when the call returned true and `0` when it returned false; it starts at `0`, so a result the `operation` never assigned captures `0`. An output array of a rejected input is not defined by the Controlled Spec §4, so the flag is the only state that distinguishes a rejection from an executed mapping.

- `guard_pass`: the captured flag. It is `== 1` on the cases of a `pass` test and `== 0` on the cases of an `xfail` test.
- `left_wrap`: `max|u_out(k) - u_in(k+nx)|` over `k` in `1..ng`, plus `max|u_out(i) - u_in(i)|` over the interior `i` in `ng+1..ng+nx`: the deviation of the left ghost cells from their periodic images, and of the interior from its copy.
- `right_wrap`: `max|u_out(ng+nx+k) - u_in(ng+k)|` over `k` in `1..ng`, plus `max|u_out(i) - u_in(i)|` over the interior `i` in `ng+1..ng+nx`: the deviation of the right ghost cells from their periodic images, and of the interior from its copy.
- `pair_residual`: on `l0_periodic_xmir`, `norm2(u_out - T(u_out_base)) / norm2(u_out_base)` over every element of the ghost-extended field, where `u_out_base` is the `u_out` of `l0_periodic` captured after its run and `T(u_out_base) = flip(u_out_base, 0)`. The host reads the base through `at('l0_periodic')` and applies `flip` (`docs/workflow/phases/phase_01_compile.md`).
- `cross_target_state_agreement`: this target's run compared with every other target's certified variant of this node, case by case (`docs/TESTS.md` §Cross-target judgment): for one output `q` captured after the run and the same output `q_ref` of the same case in the other variant, `max|q - q_ref| / max(max|q|, max|q_ref|, 1)` over every element. The host evaluates it from the captured state, against each other variant separately; on a target with no other certified variant it holds vacuously. It is outside the `quantities` line.

## 6. Test definitions
- `test_id`: `l0_periodic_left_wrap_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_advection_diffusion_boundary_1d_periodic_copy__apply`
  - `expected_outcome`: `pass`
  - `quantities`: `guard_pass`, `left_wrap`
  - `judgment`: on `l0_periodic`, `l0_periodic_ng2`, `l0_min_nx` and `l0_large_nx`, after applying, `guard_pass == 1`, for every `k` in `1..ng` the left ghost cell satisfies `u_out(k) = u_in(k+nx)` exactly (at `ng=1` this is $u_{-1}=u_{nx-1}$), and the interior cells satisfy `u_out(i) = u_in(i)` for `i` in `ng+1..ng+nx` exactly (`left_wrap == 0`). The cross-target judgment is applied: `cross_target_state_agreement` over `u_out` is `<= 1e-12`.
- `test_id`: `l0_periodic_right_wrap_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_advection_diffusion_boundary_1d_periodic_copy__apply`
  - `expected_outcome`: `pass`
  - `quantities`: `guard_pass`, `right_wrap`
  - `judgment`: on `l0_periodic`, `l0_periodic_ng2`, `l0_min_nx` and `l0_large_nx`, after applying, `guard_pass == 1`, for every `k` in `1..ng` the right ghost cell satisfies `u_out(ng+nx+k) = u_in(ng+k)` exactly (at `ng=1` this is $u_{nx}=u_0$), and the interior cells satisfy `u_out(i) = u_in(i)` for `i` in `ng+1..ng+nx` exactly (`right_wrap == 0`). The cross-target judgment is applied: `cross_target_state_agreement` over `u_out` is `<= 1e-12`.
- `test_id`: `l0_mirror_pair_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_advection_diffusion_boundary_1d_periodic_copy__apply`
  - `expected_outcome`: `pass`
  - `quantities`: `guard_pass`, `pair_residual`
  - `judgment`: `guard_pass == 1` on `l0_periodic` and `l0_periodic_xmir`, and on `l0_periodic_xmir` `pair_residual` against `l0_periodic` is `<= 1.0e-12`. The mapping copies values, so a conforming kernel gives exactly `0` (measured `0`); the bound is the suite's cross-target bound, not a measured round-off. Measured with an independent reference of §3, the pre-call content `-999` left in every element the kernel does not write: a kernel that leaves one interior cell unwritten (the second, or the first or the last through a copy loop one cell short at one end) gives `1.42`; one that fills $u_{-1}$ from $u_{nx-2}$ gives `0.895`, and one that fills $u_{nx}$ from $u_1$ gives `0.638`. Each of these also fails a wrap judgment on `l0_periodic`. The cross-target judgment is applied: `cross_target_state_agreement` over `u_out` is `<= 1e-12`.
- `test_id`: `l0_invalid_nx_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_advection_diffusion_boundary_1d_periodic_copy__apply`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `nx<2`
  - `quantities`: `guard_pass`
  - `judgment`: on `l0_invalid_nx`, the `operation` refuses the input: `guard_pass == 0`, in the failure direction. The cross-target judgment is not applied.
- `test_id`: `l0_invalid_ng_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_advection_diffusion_boundary_1d_periodic_copy__apply`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `ng<1`
  - `quantities`: `guard_pass`
  - `judgment`: on `l0_invalid_ng`, the `operation` refuses the input: `guard_pass == 0`, in the failure direction. The cross-target judgment is not applied.

## 7. Pass/fail aggregation rules
- `per_test.pass_rule`: `pass` when every predicate of the test holds.
- `per_test.xfail_rule`: `xfail` when `xfail_condition` is true and every predicate of the test holds.
- `suite.pass_rule`: `pass` when all `test_id` are `pass` or `xfail`.

## 8. Traceability
- Record `test_profile_id` and `test_profile_version` in `trial_meta.json`.
