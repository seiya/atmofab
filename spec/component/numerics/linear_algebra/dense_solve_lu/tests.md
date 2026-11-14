# Tests: solution of a dense linear system by the LU driver of an external library (L0)

## 0. Meta information
- `test_profile_id`: `dense_solve_lu_l0`
- `test_profile_version`: `0.1.0`
- `status`: `draft`
- `spec_ref.spec_kind`: `component`
- `spec_ref.spec_id`: `dense_solve_lu`
- `spec_ref.spec_version`: `0.1.0`
- `spec_ref.controlled_spec_path`: `spec/component/numerics/linear_algebra/dense_solve_lu/controlled_spec.md`

## 1. Test purpose
This suite verifies the published `operation` `dense_solve_lu__solve` at `L0`: the solution of small systems against the solution each case declares, the smallest valid order, a system whose leading diagonal element is zero, a family of well-conditioned systems at three orders against its closed-form solution, the refusal of an invalid order at its boundary, and the refusal of a matrix the factorization finds singular.

The transformations of `docs/TESTS.md` §Symmetry pairs are transformations of a grid, and the system has none: no translation, axis exchange or mirror is defined (Controlled Spec §3), so the suite carries no symmetry pair. The system's own transformations are the exchange of two equations, the exchange of two unknowns and the scaling of an equation. None is judged as a pair: every element of the solution of each small system is pinned by the solution the case declares, so a pair would judge nothing those judgments do not. The equations of `l0_base3` in another order are carried as a declared-solution case of their own, `l0_base3_rowperm`, because that order is the one the factorization cannot start without an interchange.

The comparison with theory is the declared and closed-form solutions of §5. The CFL number is `N/A`: the `operation` has no transport and no time step.

## 2. Input-defaulting rules
- Every case declares the order as the input `system.n`. The state is the matrix `a` (extents `n` by `n`), the right-hand side `b` (extent `n`), the solution `x` (extent `n`) and the flag `solved`. A matrix below is written row by row: the `k`-th inner list is the equation `k`, so its `j`-th number is `a(k, j)`.
- Before the call `x` holds the literal `-999` at every element, its pre-call content: `x` is an operation-only output, so an element the `operation` does not write is captured as `-999` and fails every judgment of §6 over `x`.
- The declared-solution cases each declare the expected solution as the input `expected.x`, a list of `n` numbers; substituting it into the equations gives the right-hand side exactly.
  - `l0_n1`: `n=1`, `a = [[4]]`, `b = [2]`, `expected.x = [0.5]`, the smallest valid order: a guard written `n<=1` refuses it.
  - `l0_base3`: `n=3`, `a = [[4, 1, 2], [0, 5, 3], [2, 1, 6]]`, `b = [8, -1, 18]`, `expected.x = [1, -2, 3]`. The matrix is not symmetric and the three solution elements are pairwise distinct, so a transposed matrix or two exchanged unknowns give another `x`.
  - `l0_base3_rowperm`: the equations of `l0_base3` in the order 2, 3, 1: `n=3`, `a = [[0, 5, 3], [2, 1, 6], [4, 1, 2]]`, `b = [-1, 18, 8]`, `expected.x = [1, -2, 3]`. Its leading diagonal element is `0`, so an elimination without row interchanges divides by zero at its first step.
- The family cases `l0_circulant_n8`, `l0_circulant_n32` and `l0_circulant_n128` use `n=8`, `n=32` and `n=128`. With `m(i, j) = (j - i) mod n`, the residue in `0..n-1`, the matrix is `a(i, j) = n * delta(i, j) + m(i, j) / n`, where `delta(i, j)` is `1` when `i = j` and `0` otherwise, and every element of `b` is `n + (n - 1) / 2`. Every row of the matrix sums to that value, so the solution is `x(i) = 1` for every `i`. Each order is a power of two, so every element of `a` and `b` is represented exactly. The matrix is strictly diagonally dominant: its condition number in the maximum norm is below `3` at every order. Every column of the matrix sums to the same value as every row, so the family does not separate a transposed matrix; the declared-solution cases do.
- The abnormal cases each meet one refusal of the Controlled Spec §4 alone.
  - `l0_n0`: `n=0`, the invalid order at its boundary. Its state has no cell.
  - `l0_singular_n2`: `n=2`, `a = [[1, 2], [2, 4]]`, `b = [1, 2]`. The second equation is twice the first.
  - `l0_singular_n3`: `n=3`, `a = [[1, 0, 1], [0, 1, 1], [1, 1, 2]]`, `b = [1, 1, 2]`. The third equation is the sum of the first two, and the zero pivot arises at the last column.

  Every element of the two singular matrices is a small integer and every multiplier of their elimination is `0`, `1`, `-1` or `1/2`, so every intermediate is represented exactly and the zero pivot is exact in floating point, whichever of two rows of equal magnitude the factorization takes.

## 3. Execution-control rules
`N/A`: this `component` solves one system per call and defines no time-stepping or iteration. Execution control belongs to the caller.

distribute: none

## 4. Case-expansion rules
`N/A`: the `L0` suite uses fixed inputs and defines no `case` sweep.

## 5. Judged quantities
A test is judged by the host's predicates over the captures alone (`docs/TESTS.md` requirement 9). Each quantity below is named on the `quantities` line of the tests that judge it, and is evaluated from the state captured after the run in each of the test's target cases.

The host-rendered driver calls the `operation` once per case on the case's state, with `n` from the case's inputs and `a`, `b`, `x`, `solved` bound to the states of those names. The state captured before and after that call consists of `a`, `b`, `x` (the pre-call content `-999` of §2 before the call) and `solved`. All four are arguments of the `operation`. `solved` is its logical result, which the driver writes into the state after the call as `1` when the call returned true and `0` when it returned false; it starts at `0`, so a result the `operation` never assigned captures `0`. The solution of a refused call is not defined by the Controlled Spec §4, so the flag is the only state that distinguishes a refusal from a solve.

- `solved`: the captured flag. It is `== 1` on the cases of a `pass` test and `== 0` on the cases of an `xfail` test.
- `solution_error`: `max|x(i) - x_ref(i)| / max|x_ref(i)|` over `i` in `1..n`, where `x` is the solution captured after the run and `x_ref` is the case's declared expected solution `expected.x`.
- `family_error`: `max|x(i) - 1|` over `i` in `1..n`: the deviation of the captured solution from the closed-form solution of the family of §2.
- `cross_target_state_agreement`: this target's run compared with every other target's certified variant of this node, case by case (`docs/TESTS.md` §Cross-target judgment): for the solution `q` captured after the run and the same solution `q_ref` of the same case in the other variant, `max|q - q_ref| / max(max|q|, max|q_ref|, 1)` over every element. The host evaluates it from the captured state, against each other variant separately; on a target with no other certified variant it holds vacuously. It is outside the `quantities` line.

The predicate grammar holds no product of a matrix and a vector, so the residual `a x - b` of a captured solution is not a judged quantity: every `pass` case is judged against a solution its inputs fix.

## 6. Test definitions
- `test_id`: `l0_declared_solution_pass`
  - `level`: `L0`
  - `operation_id`: `dense_solve_lu__solve`
  - `expected_outcome`: `pass`
  - `quantities`: `solved`, `solution_error`
  - `judgment`: on `l0_n1`, `l0_base3` and `l0_base3_rowperm`, after the call, `solved == 1` and `solution_error <= 1.0e-12`. The bound is a round-off bound: the three matrices have condition numbers below `4` in the maximum norm, and a conforming kernel gives a `solution_error` of at most a few units of round-off (measured `0` on the three cases against LAPACK 3.10, with a hand-written kernel of the Controlled Spec §3). Measured with that kernel on `l0_base3`: a kernel that hands the library the transposed matrix gives `0.35`; one that keeps the copy of the matrix in storage with one row more than the leading dimension it passes gives `3.2`. `l0_n1` separates neither: a matrix of one element is its own transpose, and one element is addressed alike at any leading dimension. The cross-target judgment is applied: `cross_target_state_agreement` over `x` is `<= 1e-12`.
- `test_id`: `l0_circulant_family_pass`
  - `level`: `L0`
  - `operation_id`: `dense_solve_lu__solve`
  - `expected_outcome`: `pass`
  - `quantities`: `solved`, `family_error`
  - `judgment`: on `l0_circulant_n8`, `l0_circulant_n32` and `l0_circulant_n128`, after the call, `solved == 1` and `family_error <= 1.0e-12`. The bound is the order times the condition number times the unit round-off with a margin: at `n=128` that product is below `5.0e-14` (measured `3.7e-15` against LAPACK 3.10). It is an upper bound only, so that another build of the library, whose round-off differs, stays inside it. A kernel whose storage has one row more than the leading dimension it passes gives `91` at `n=8`. The cross-target judgment is applied: `cross_target_state_agreement` over `x` is `<= 1e-12`.
- `test_id`: `l0_invalid_n_xfail`
  - `level`: `L0`
  - `operation_id`: `dense_solve_lu__solve`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `n<1`
  - `quantities`: `solved`
  - `judgment`: on `l0_n0`, the `operation` refuses the input: `solved == 0`, in the failure direction. The state has no cell, so the flag is all there is to judge. The cross-target judgment is not applied.
- `test_id`: `l0_singular_xfail`
  - `level`: `L0`
  - `operation_id`: `dense_solve_lu__solve`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: the factorization meets an exactly zero pivot
  - `quantities`: `solved`
  - `judgment`: on `l0_singular_n2` and `l0_singular_n3`, the `operation` reports the library's positive status: `solved == 0`, in the failure direction. A kernel that does not read the status reports `solved == 1` on both. The cross-target judgment is not applied.

## 7. Pass/fail aggregation rules
- `per_test.pass_rule`: `pass` when every predicate of the test holds.
- `per_test.xfail_rule`: `xfail` when `xfail_condition` is true and every predicate of the test holds.
- `suite.pass_rule`: `pass` when all `test_id` are `pass` or `xfail`.

## 8. Traceability
- Record `test_profile_id` and `test_profile_version` in `trial_meta.json`.
