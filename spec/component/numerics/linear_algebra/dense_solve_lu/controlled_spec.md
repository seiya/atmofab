# Controlled Spec: solution of a dense linear system by the LU driver of an external library (component spec)

## 0. Meta information
- `spec_id`: `dense_solve_lu`
- `spec_version`: `0.1.0`
- `status`: `controlled_draft`
- `spec_kind`: `component`
- `domain`: `numerics`
- `family`: `linear_algebra`

## 1. Responsibility and scope
This `component` solves one dense linear system $A x = b$ of order `n` with one right-hand side, by the LU driver `dgesv` of the `external library` `lapack` (`deps.yaml` `dependencies.libraries`, declaration `spec/libraries/lapack.yaml`). The premise of the input is a general real matrix in double precision, stored in full, with no symmetry, band or definiteness assumed.

The factorization and the two triangular solves are the library's. This `component` owns the refusal of an invalid order, the copies that keep its inputs unmodified, the call, and the reading of the library's status.

Out of scope:
- An estimate of the condition number, equilibration and iterative refinement: a matrix that is close to singular is factorized and solved like any other (§4).
- More than one right-hand side, and the reuse of a factorization across calls.
- Banded, symmetric, packed and triangular storage.
- Single precision, which is not specified or measured here.
- Setup and module state: the `operation` keeps nothing between two calls.

## 2. input/output contract
The inputs are the order `n`, the matrix `a` (rank 2, extents `n` by `n`, dimensionless) and the right-hand side `b` (rank 1, extent `n`, dimensionless). `a(i, j)` is the coefficient of the unknown `j` in the equation `i`: the first index is the row. The outputs are the solution `x` (rank 1, extent `n`) and the logical result `solved`. The published arguments are exactly, in this order: `n`, `a`, `b`, `x`, `solved`.

`a` and `b` are inputs only. The library's driver overwrites the matrix and the right-hand side it is handed, so the `operation` hands it copies.

The arrays carry no ghost cell and no boundary: every element of `a` and `b` is read, and every element of `x` is written on a call that returns `solved` true.

**The library procedure.** `dgesv` takes, in this order: the order of the system; the number of right-hand sides; the matrix, stored by columns in an array whose first extent is the next argument; that first extent (the leading dimension, at least the order); an integer array of the order's length that receives the row interchanges; the right-hand sides, one per column, in an array whose first extent is the next argument; that first extent (at least the order); and an integer status it returns. On return with status `0` the right-hand-side array holds the solution. A status `i > 0` states that the factorization met an exactly zero diagonal element of $U$ at position `i`, and no solution is computed. A negative status states an illegal argument, and the library's own error handler may end the program before returning.

## 3. Operation definition
The published `operation` is `dense_solve_lu__solve`. On a valid order (§4) it performs, in this order:

1. Copy `a` and `b` into storage whose first extent is exactly the leading dimension passed to the library.
2. Call `dgesv` on the copies with one right-hand side.
3. Set `solved` true when the returned status is `0`, and false otherwise.
4. When `solved` is true, set `x` to the solution the library left in the copy of the right-hand side.

The driver factorizes the matrix by Gaussian elimination with partial pivoting,
$$
P A = L U,
$$
where $P$ is the permutation of the row interchanges, $L$ is unit lower triangular and $U$ is upper triangular: at each column the row whose element has the largest magnitude at or below the diagonal is brought to the diagonal. It then solves $L y = P b$ and $U x = y$. The result is the solution of the system as supplied: the order of the equations does not change it beyond round-off, and a system whose leading diagonal element is zero is solved when the matrix is not singular.

**Not defined here.** The CFL number is not defined, because the `operation` has no transport and no time step. No spatial symmetry is defined, because the system has no grid.

## 4. Failure conditions and constraints
Treat `n<1` as invalid input: `solved` is false, the library is not called, and `x` is not defined. The refusal precedes the call because the library's error handler may end the program on an illegal argument (§2).

A matrix for which the library returns a positive status (an exactly zero diagonal element of $U$) is singular to the factorization: `solved` is false and `x` is not defined. This is a result of the call, not invalid input.

A matrix that is close to singular without an exactly zero pivot is not refused: `solved` is true and `x` holds what the factorization gives. A caller that needs a bound on the error estimates the condition number itself.

The `operation` never ends the program, and it carries no check on the values of `a` and `b`.

## 5. Public API and compatibility
The only published `operation_id` is `dense_solve_lu__solve`.

A change breaking compatibility of the published signature — the argument names, their order, their types, their ranks, or their `intent`s — is a **breaking change released under a new `spec_version`**, and so is a change to how the surface is carried. An update that keeps the signature and every result is a `minor` update. A change of the library the `operation` reaches, or of the library procedure it calls, is released under a new `spec_version`.

### 5.1 Canonical interface block
The exact published surface as a machine-readable, language-neutral signature block. The rules are those of `docs/CONTROLLED_SPEC.md` §5.1: the deterministic gates render this block to the target language and pin the `IR`'s `public_api` and the generated model source against it.

```yaml
module_parameters:
- name: dp
  value: float64
types: []
procedures:
- kind: subroutine
  name: dense_solve_lu__solve
  args:
  - name: n
    rank: 0
    intent: in
    spec:
      type: integer
  - name: a
    rank: 2
    intent: in
    spec:
      type: real
      kind: dp
    dims:
    - n
    - n
  - name: b
    rank: 1
    intent: in
    spec:
      type: real
      kind: dp
    dims:
    - n
  - name: x
    rank: 1
    intent: out
    spec:
      type: real
      kind: dp
    dims:
    - n
  - name: solved
    rank: 0
    intent: out
    spec:
      type: logical
```

## 6. Prohibitions
Forbid factorizing or solving the system in the `component`'s own source: the factorization and the triangular solves are the library's `dgesv`, and a source that reaches no procedure of the declared library is refused at `Build`. Forbid reaching a library, a module or a library procedure other than the declared `dgesv` of `lapack`. Forbid reordering, scaling or otherwise transforming the equations or the unknowns before the call or the solution after it: the system is handed to the library as supplied, and a system is never solved through a reordered copy mapped back. Forbid calling the library on an invalid order. Forbid reporting `solved` true on a non-zero status, and forbid replacing a refused solve by another method, a regularization or a least-squares solution. Forbid modifying `a` or `b`. Forbid ending the program.

## 7. Traceability
Require recording the adoption result in `component_catalog.yaml` and, by the host, in the adopting `spec`'s `<ir_ref>/dependency_graph.json` (`all_nodes`, at the resolved `version`). The order, the matrix, the right-hand side and the declared solution of each case of the suite fall into the `case` section of `spec.ir.yaml`. The basis of §2 and §3 is the description of the routine `DGESV` in the LAPACK Users' Guide (third edition, SIAM, 1999); the suite is measured against LAPACK 3.10.

## 8. tests reference
The corresponding `tests.md` is `spec/component/numerics/linear_algebra/dense_solve_lu/tests.md`, with `test_profile_version` of `0.1.0`.

## 9. AD preparation information
`ad_readiness.enabled` is `true`. The library call is a gradient-excluded operation: its derivative is not obtained from the library's source, and is defined by the solve with the transposed matrix. The selection of the pivot row and the two refusals of §4 are made explicit as non-differentiable operations.
