# Tests: 1D periodic-boundary mapping (L0)

## 0. Meta information
- `test_profile_id`: `dynamics_advection_diffusion_boundary_1d_periodic_copy_l0`
- `test_profile_version`: `0.3.0`
- `status`: `draft`
- `spec_ref.spec_kind`: `component`
- `spec_ref.spec_id`: `dynamics_advection_diffusion_boundary_1d_periodic_copy`
- `spec_ref.spec_version`: `0.2.1`
- `spec_ref.controlled_spec_path`: `spec/component/dynamics/advection_diffusion/dynamics_advection_diffusion_boundary_1d_periodic_copy/controlled_spec.md`

## 1. Test purpose
This suite verifies the published `operation` `dynamics_advection_diffusion_boundary_1d_periodic_copy__apply` at `L0`: the left and right periodic-wrap mapping of the ghost cells, the copy of the interior cells, the mirror pair, and the input guard for an invalid grid size (`nx<2`).

The mapping admits three transformations (`docs/TESTS.md` §Symmetry pairs): the mirror, the translation and the sign reversal. The suite carries the mirror as a pair and states why it does not carry the other two. The **mirror** $x \to L - x$ reverses the ghost-extended field (element `k` of `u_in'` is element `nx+2*ng+1-k` of `u_in`); the operation takes no velocity, so no sign is reversed. Under the reversal the left ghost $u'_{-1}$ takes $u'_{nx-1} = u_0$, which is the base's right ghost, so the transformed `u_out` is the base's `u_out` reversed. The base's `u_out` is pinned element by element by the two wrap judgments of §6, each of which also requires the interior copy; the pair carries that to the reversed input, whose left ghost wraps from what was the right interior cell and whose right ghost from what was the left one. The **translation** along the periodic axis is admitted by the mapping but is not a transformation of the ghost-extended array: a cyclic shift of the `nx + 2*ng` elements is not the shift of the periodic interior, whose period is `nx`. The suite carries no translation pair; the `problem` that composes this mapping carries the translation of the whole state. The **sign reversal** $u \to -u$ is admitted because the mapping copies values, and it is not carried: it leaves every position in place, so on the base, whose every element the wrap judgments pin, it separates no kernel from a conforming one. A one-dimensional mapping has no axis exchange.

## 2. Input-defaulting rules
- The normal case uses `nx>=2`, `ng=1`.
- The normal cases are `l0_periodic` and its transformed copy `l0_periodic_xmir`, which sorts after it. `l0_periodic` uses `nx=4`, `ng=1`, and `u_in = [-999, 0.3, 1.7, 0.2, 2.9, -999]`: the interior values are pairwise distinct, so that every ghost cell identifies the interior cell it was mapped from, and the ghost cells hold a sentinel value (`-999`) that no interior cell holds, so that a ghost cell of `u_in` read by mistake is visible in the output. With `nx` even, no element of the six is its own image under the reversal. `l0_periodic_xmir` uses `nx=4`, `ng=1`, and `u_in = [-999, 2.9, 0.2, 1.7, 0.3, -999]` (the field of `l0_periodic` reversed).
- The abnormal case `l0_invalid_nx` uses `nx<2`.

## 3. Execution-control rules
`N/A`: this `component` applies a boundary mapping to a provided field and defines no time-stepping or iteration. Execution control is the responsibility of the `problem` runner.

## 4. Case-expansion rules
`N/A`: the `L0` suite uses fixed inputs and defines no `case` sweep. Case expansion is defined at the `problem` level.

## 5. Diagnostics contract
- Require outputting `checks.left_wrap`, `checks.right_wrap`, `checks.mirror_pair`, and `checks.input_guard` in `diagnostics.json`. `checks.mirror_pair` is computed on `l0_periodic_xmir` and is `na` on every other case; it reads the `u_out` of `l0_periodic`, which the checks module keeps from that case's run (`docs/workflow/CHECKS_MODULE_CONTRACT.md` §2, the accumulator ordering rule).
- `pair_residual` of `l0_periodic_xmir` is `norm2(u_out - T(u_out_base)) / norm2(u_out_base)` over every element of the ghost-extended field, where `u_out_base` is the `u_out` of `l0_periodic` captured after its run and `T(u_out_base) = flip(u_out_base, 0)`. The host evaluates it from the captured states (`at('l0_periodic')` with `flip`, `docs/workflow/phases/phase_01_compile.md`).
- The state of a case, captured after setup and after the run, consists of `u_in` (the ghost-extended input of §2, written at setup), `u_out` (`nx + 2*ng` values, the sentinel `-999` at setup), and `guard_pass`, a scalar recorded as a number because a state variable is real-valued: `-1` at setup, before the `operation` is called; `1` when the call returned true; `0` when it returned false. The setup value `-1` is distinct from both values the call can return, so a case whose `operation` was not called captures `-1`. Every `pass` judgment of §6 requires `guard_pass = 1` on the cases it names, and every `xfail` judgment requires `guard_pass = 0` on its cases; these are host-evaluated from the capture after the run (the refusal's observable consequence: an output array of a rejected input is not defined by the Controlled Spec §4, so the flag is the only state that distinguishes a rejection from an executed mapping). `checks.input_guard` is `pass` when `guard_pass = 1` and `fail` otherwise.
- `cross_target_state_agreement` compares this target's run with every other target's certified variant of this node, case by case (`docs/TESTS.md` §Cross-target judgment): for one output `q` captured after the run and the same output `q_ref` of the same case in the other variant, it is `max|q - q_ref| / max(max|q|, max|q_ref|, 1)` over every element. The host evaluates it from the captured state, against each other variant separately; it is not a `diagnostics.json` field, and on a target with no other certified variant it holds vacuously.

## 6. Test definitions
- `test_id`: `l0_periodic_left_wrap_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_advection_diffusion_boundary_1d_periodic_copy__apply`
  - `expected_outcome`: `pass`
  - `judgment`: on `l0_periodic`, after applying, the left ghost cell satisfies `u_out(1) = u_in(nx+1)` ($u_{-1}=u_{nx-1}$) exactly, and the interior cells satisfy `u_out(i) = u_in(i)` for `i` in `2..nx+1` exactly. The cross-target judgment is applied: `cross_target_state_agreement` over `u_out` is `<= 1e-12`.
- `test_id`: `l0_periodic_right_wrap_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_advection_diffusion_boundary_1d_periodic_copy__apply`
  - `expected_outcome`: `pass`
  - `judgment`: on `l0_periodic`, after applying, the right ghost cell satisfies `u_out(nx+2) = u_in(2)` ($u_{nx}=u_0$) exactly, and the interior cells satisfy `u_out(i) = u_in(i)` for `i` in `2..nx+1` exactly. The cross-target judgment is applied: `cross_target_state_agreement` over `u_out` is `<= 1e-12`.
- `test_id`: `l0_mirror_pair_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_advection_diffusion_boundary_1d_periodic_copy__apply`
  - `expected_outcome`: `pass`
  - `judgment`: on `l0_periodic_xmir`, `pair_residual` against `l0_periodic` is `<= 1.0e-12` (`checks.mirror_pair`). The mapping copies values, so a conforming kernel gives exactly `0` (measured `0`); the bound is the suite's cross-target bound, not a measured round-off. Measured with an independent reference of §3, the checks module's sentinel `-999` left in every element the kernel does not write: a kernel that leaves one interior cell unwritten (the second, or the first or the last through a copy loop one cell short at one end) gives `1.42`; one that fills $u_{-1}$ from $u_{nx-2}$ gives `0.895`, and one that fills $u_{nx}$ from $u_1$ gives `0.638`. Each of these also fails a wrap judgment on `l0_periodic`. The cross-target judgment is applied: `cross_target_state_agreement` over `u_out` is `<= 1e-12`.
- `test_id`: `l0_invalid_nx_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_advection_diffusion_boundary_1d_periodic_copy__apply`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `nx<2`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`

## 7. Pass/fail aggregation rules
- `per_test.pass_rule`: `pass` when the judgment expression is satisfied.
- `per_test.xfail_rule`: `xfail` when `xfail_condition` is true and `pass_when` is satisfied.
- `suite.pass_rule`: `pass` when all `test_id` are `pass` or `xfail`.

## 8. Traceability
- Record `test_profile_id` and `test_profile_version` in `trial_meta.json`.
