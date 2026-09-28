# Tests: Fortran/CPU MPI runner harness (L0 plumbing and distributed-state self-test)

## 0. Meta information
- `test_profile_id`: `harness_fortran_cpu_mpi_l0`
- `test_profile_version`: `0.1.0`
- `status`: `draft`
- `spec_ref.spec_kind`: `infrastructure`
- `spec_ref.spec_id`: `harness_fortran_cpu_mpi`
- `spec_ref.spec_version`: `0.1.0`
- `spec_ref.controlled_spec_path`: `spec/infrastructure/infra/harness/harness_fortran_cpu_mpi/controlled_spec.md`

## 1. Test purpose
This suite verifies the published runner-plumbing operations of `harness_fortran_cpu_mpi` at `L0`: numeric / boolean / rank-1..4 array JSON emission round-trips, the case-set fan-out (one `<case_id>.json` snapshot per case), the derived `perf` throughput, the `--cases` input guard, the per-case metric fold (a supplied numeric leaf and a supplied `N/A` leaf reaching `diagnostics.json` at their declared addresses), the `(test_id, case_id)` shape of the metrics-basis index, and the distributed-state operations — the block partition, the non-periodic and the periodic halo exchange, the gather of rank-1..4 arrays onto rank 0, and the global reductions. The self-test runner exercises each operation and records a per-case check in `diagnostics.json`; because the writers use the emitters, a faithful emitter is a precondition for a correct output.

**Every case passes whatever number of ranks runs it, one included.** The run under the target's launcher starts `execution.ranks` processes, and the target's quality check re-runs the same cases as a single process; the two runs must report the same checks and verdict. So no case's check or verdict may depend on the number of ranks: every distributed case builds its evidence from a global index field that `__partition` distributes, and records the GLOBAL result — an array gathered onto rank 0, or a reduction every rank holds — which is the same for one rank as for many. The one exception is deliberate and touches the snapshot only: `l0_partition_tile_pass` records which rank owns each cell and how many ranks ran, and the host checks the two against each other (§6), because evidence that is the same for one rank as for many cannot show that the run was distributed.

## 2. Input-defaulting rules
- Each normal case supplies a small fixed set of sentinel values to the emitter under test. **The sentinel values are DECLARED in the IR as that case's inputs** (`case.test_case_set[].inputs.initial`), and the self-test emits exactly the declared values, so the host can compare what the emitter wrote against what the case declares (§6, the host-evaluated `primary_predicate` of each test):
  - `l0_numeric_roundtrip_pass`: `inputs.initial.x_in = [-1.5, 1.0e-30, 1.0e+30]` (a negative, a `1e-30`-scale and a `1e+30`-scale real).
  - `l0_array_emit_pass`: `inputs.initial.a1 = [1, 2]`, `a2 = [[1, 2], [3, 4]]`, `a3 = [[[1, 2], [3, 4]], [[5, 6], [7, 8]]]`, `a4` the same pattern over `1..16`, each written in the JSON nesting order the emitters produce (the leading array index outermost), and emitted as declared.
  - `l0_boolean_literal_pass`: `inputs.initial.bool_in_true = true`, `bool_in_false = false`.
- The metric case (`l0_metric_leaf_pass`) supplies exactly two fixed sentinel `h_metric` records and no other sentinel payload. It carries the same `grid` / `time` / `boundary` inputs as every other case, and — like every case — its own `inputs.profile_selection` entry naming the plumbing aspect it verifies, here the diagnostics fold (`harness_fortran_cpu_mpi__write_diagnostics`). The two records, whose field values are the case's input data, are:
  - `{ name = 'selftest.metric_leaf', value = 0.25, is_na = false, reason_na = '' }`
  - `{ name = 'selftest.metric_na', value = -1.0, is_na = true, reason_na = 'not_computed' }`
  There is no third record: `selftest.metric_na_reason_na` is a key the writer derives from the second record's `is_na` / `reason_na` (§5), never a supplied `h_metric`.
- The five distributed cases each build a partitioned field from GLOBAL cell indices, run the operation under test on it, and record the global result. Their declared inputs are the problem sizes and the reference results, and every case carries the same `grid` / `time` / `boundary` inputs as every other case plus its own `inputs.profile_selection` naming the operation it verifies. In every one, `glo .. ghi` is this rank's range from `harness_fortran_cpu_mpi__partition(n, glo, ghi)` for the size `n` named, `nloc = ghi - glo + 1`, and "gather" means the matching `harness_fortran_cpu_mpi__gather_r<k>`, whose result rank 0 records.
  - `l0_partition_tile_pass`: `inputs.initial.n_global = 12`, `n_small = 2`, `index_ref = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]`, `small_ref = [1, 2]`. Each rank partitions `n_global`, fills a length-`nloc` array with its owned global indices `glo, glo+1, …, ghi` (as reals) and gathers it (`lo = 1`, `hi = nloc`) into `owned_index`. It does the same for `n_small` into `small_index`; with more than two ranks every rank above rank 1 owns nothing and passes a length-0 array with `lo = 1`, `hi = 0`. Each rank also fills a length-`nloc` array of its partition of `n_global` with its own rank, `__comm_rank()` (as a real), and gathers it into `owner`; `nranks` is `__comm_size()` (as a real). `owner` is the one piece of evidence in the suite that DEPENDS on the number of ranks, and deliberately so: it is what shows that the run was distributed at all. The host checks it against the block rule for the `nranks` the run reports (§6), so a harness whose collective operations never leave the calling process — each rank computing the whole range by itself — records an `owner` of all zeros and fails at any run of more than one rank.
  - `l0_halo_exchange_pass`: `inputs.initial.n_global = 12`, `ng = 2`, `diff_ref_r1 = [3, 4, 4, 4, 4, 4, 4, 4, 4, 4, -9, -10]`, `diff_ref_r2 = [[103, 104, 4, 4, 4, 4, 4, 4, 4, 4, -109, -110], [203, 204, 4, 4, 4, 4, 4, 4, 4, 4, -209, -210], [303, 304, 4, 4, 4, 4, 4, 4, 4, 4, -309, -310]]`. Rank-1: a length-`nloc + 2*ng` array is set to `0.0`, its owned positions `ng+1 .. ng+nloc` get the global index of the cell, and `harness_fortran_cpu_mpi__exchange_halo_r1(f, ng, .false.)` fills the ghosts; then for each owned position `p` the case computes the width-2 central difference `f(p+2) - f(p-2)` into a length-`nloc` array, which it gathers into `halo_diff_r1`. Rank-2: a `3 × (nloc + 2*ng)` array partitioned along axis 2 is set to `0.0`, its owned cell `(k, ng+i)` gets `100*k` plus the global index of the cell, `harness_fortran_cpu_mpi__exchange_halo_r2(h, 2, ng, .false.)` fills the ghosts, and the same difference along axis 2 (`h(k, p+2) - h(k, p-2)`) is gathered along axis 2 into `halo_diff_r2`. The references follow from the global field alone: `4` wherever both stencil ends are inside `1..12`, and the untouched `0.0` ghost beyond each physical end.
  - `l0_halo_periodic_pass`: the same construction with `periodic = .true.` in both exchanges; `inputs.initial.n_global = 12`, `ng = 2`, `diff_ref_r1 = [-8, -8, 4, 4, 4, 4, 4, 4, 4, 4, -8, -8]`, `diff_ref_r2` three rows each equal to `diff_ref_r1`. With periodic ghosts the stencil at the first two and the last two cells reads the opposite end of the global field.
  - `l0_gather_roundtrip_pass`: every element's value is built from its GLOBAL indices `(i1, i2, i3, i4)` as `i1 + 100*i2 + 10000*i3 + 1000000*i4` (absent indices omitted). `g1`: `n = 12`, a length-`nloc` array holding `glo, …, ghi`, gathered with `lo = 1`, `hi = nloc`. `g2`: `n = 12` along axis 1 of a `nloc × 2` array, gathered with `axis = 1`. `g3`: `n = 8` along axis 3 of a `2 × 2 × nloc` array, gathered with `axis = 3`. `g4`: `n = 8` along axis 2 of a `2 × (nloc + 2) × 2 × 2` array whose owned cells are positions `2 .. nloc+1` of axis 2 — one extra cell on each side, set to `-1.0`, which the gather must not carry — gathered with `axis = 2`, `lo = 2`, `hi = nloc + 1`. The declared references are `inputs.initial.g1_ref = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]`, `g2_ref = [[101, 201], [102, 202], [103, 203], [104, 204], [105, 205], [106, 206], [107, 207], [108, 208], [109, 209], [110, 210], [111, 211], [112, 212]]`, `g3_ref = [[[10101, 20101, 30101, 40101, 50101, 60101, 70101, 80101], [10201, 20201, 30201, 40201, 50201, 60201, 70201, 80201]], [[10102, 20102, 30102, 40102, 50102, 60102, 70102, 80102], [10202, 20202, 30202, 40202, 50202, 60202, 70202, 80202]]]`, `g4_ref = [[[[1010101, 2010101], [1020101, 2020101]], [[1010201, 2010201], [1020201, 2020201]], [[1010301, 2010301], [1020301, 2020301]], [[1010401, 2010401], [1020401, 2020401]], [[1010501, 2010501], [1020501, 2020501]], [[1010601, 2010601], [1020601, 2020601]], [[1010701, 2010701], [1020701, 2020701]], [[1010801, 2010801], [1020801, 2020801]]], [[[1010102, 2010102], [1020102, 2020102]], [[1010202, 2010202], [1020202, 2020202]], [[1010302, 2010302], [1020302, 2020302]], [[1010402, 2010402], [1020402, 2020402]], [[1010502, 2010502], [1020502, 2020502]], [[1010602, 2010602], [1020602, 2020602]], [[1010702, 2010702], [1020702, 2020702]], [[1010802, 2010802], [1020802, 2020802]]]]`, each written in the JSON nesting order the emitters produce (the leading array index outermost).
  - `l0_reduce_pass`: `inputs.initial.n_global = 12`, `sum_ref = 39.0`, `max_ref = 6.0`, `min_ref = 0.5`. Each rank holds `0.5 * j` for its owned global indices `j`, and takes the local sum, maximum and minimum over them; `red_sum`, `red_max` and `red_min` are `__reduce_sum`, `__reduce_max` and `__reduce_min` of those, and `red_count` is `__reduce_sum_int(nloc)` as a real.
- The abnormal case (`l0_missing_cases_xfail`) synthesizes an empty argv-token list (no `--cases`) and drives `harness_fortran_cpu_mpi__parse_cases` to exercise the guard.

## 3. Execution-control rules
`N/A`: the harness self-test defines no time-stepping. Each case runs its named plumbing check once (`steps = 1`).

## 4. Case-expansion rules
The suite defines twelve `case`s and thirteen `test_id`s (no sweep). The self-test's own runner writes each case's snapshot once, after the case ran; it writes no `raw/state_snapshots/initial/<case_id>.json` (that capture is the host-rendered runner's), so every host-evaluated `primary_predicate` of §6 reads `final.<var>` and the case's declared `inputs` only. Twelve tests are **single-target**: each names exactly one case, and that case's `case_id` equals the `test_id` — the twelve `case.test_case_set[].case_id` are exactly those twelve `test_id`s. The thirteenth, `l0_multi_case_evidence_pass`, is **multi-target**: it declares no case of its own and ranges over the two existing cases `l0_array_emit_pass` and `l0_numeric_roundtrip_pass`, both of which already emit `max_abs_deviation`. Each `case_id` selects the plumbing aspect that case verifies. The multi-case run itself is the `case_fanout` evidence: each case writes exactly one `raw/state_snapshots/<case_id>.json`. Each snapshot holds that case's `required_raw_variables` (the union over the tests targeting it, listed per test in §6) plus the scalar time variable `t` (value `0.0`; a single `steps=1` step).

Because `raw/metrics_basis.json` carries one entry per (`test_id`, target `case_id`) pair, the thirteen tests produce **fourteen** entries: one for each single-target test plus two for `l0_multi_case_evidence_pass`.

The run needs at least one rank and at most six: the halo cases partition 12 cells and exchange `ng = 2` ghost cells, and a rank owning fewer than `ng` cells stops the program (spec §4). Within that range every case's snapshot, check status and verdict are the same whatever number of ranks runs it (§1).

## 5. Diagnostics contract
- Require outputting, in `diagnostics.json`, a top-level `checks` object holding `checks.numeric_roundtrip`, `checks.boolean_literal`, `checks.array_emit`, `checks.case_fanout`, `checks.perf_derived`, `checks.metric_leaf`, `checks.input_guard`, `checks.partition_tile`, `checks.halo_exchange`, `checks.halo_periodic`, `checks.gather_roundtrip`, and `checks.reduce` (each `{ "status": "pass"|"fail" }`), a top-level `verdict` object with `overall` and `failed_checks`, and a `per_case` map giving each `case_id` its own `{ checks, verdict, metrics }`. The whole `diagnostics.json` is assembled inside `__write_diagnostics` from the caller-supplied `h_case_result` records (each carrying its `expected_xfail` flag); the caller supplies only honest per-case data, and the harness performs every fold.
- The per-case `metrics` object is a **fold over that case's supplied `h_metric` array**: `harness_fortran_cpu_mpi__write_diagnostics` iterates the array and writes exactly one leaf per record — a record with `is_na = false` as the single key `"<name>": <value>` (no sibling), a record with `is_na = true` as `"<name>": null` plus the one sibling `"<name>_reason_na": "<reason_na>"`. The object is decided by the supplied records alone: a body that does not iterate the supplied array — one selected by branching on `case_id`, or one emitting a fixed key set — is forbidden even when it reproduces the expected keys for this suite, because the consuming physics nodes supply different records. A length-0 array legitimately yields the empty object `{}`.
- Exactly one case supplies metrics: `l0_metric_leaf_pass` supplies the two records of §2, so its `metrics` object carries the three addresses below and every other case supplies a length-0 array and therefore gets the empty object `{}`. The required per-case metric addresses — declared in the IR as `io_contract.diagnostics_contract.metrics` and each emitted verbatim as a key of `per_case.l0_metric_leaf_pass.metrics` — are:
  - `selftest.metric_leaf` — the numeric leaf of the first supplied record, value `0.25`. `0.25` is exactly representable in binary floating point, so the round-trip through the emitted JSON token introduces no deviation.
  - `selftest.metric_na` — the honest-`N/A` leaf of the second supplied record, written as `null`, never as a real number. Its supplied `value = -1.0` is out-of-band and a correct writer never serializes it (nor any other number), because an `is_na` record is written as `null`. The §6 predicate asserts this by requiring the address to be N/A rather than any numeric value.
  - `selftest.metric_na_reason_na` — the `N/A` sibling the writer derives from the second record's `reason_na`, value `"not_computed"`. It is declared here so the predicate `ref` of §6 resolves; it is not a supplied `h_metric`, and no case computes it.
- The invalid-input case (`l0_missing_cases_xfail`) is reported as a **failing** guard at the PER-CASE level only: `per_case.l0_missing_cases_xfail.verdict.overall == fail` with `input_guard` in its `failed_checks` (the guard correctly firing on a missing `--cases`). This expected `xfail` failure is EXCLUDED from the top-level aggregation: the top-level `verdict.overall` stays `pass`, top-level `failed_checks` is `[]`, and top-level `checks.input_guard.status == pass` (the guard behaved as expected). Only a NON-`xfail` case failure would set the top-level `overall` to `fail`.

## 6. Test definitions
- `test_id`: `l0_numeric_roundtrip_pass`
  - `level`: `L0`
  - `operation_id`: `harness_fortran_cpu_mpi__emit_real`
  - `expected_outcome`: `pass`
  - `required_raw_variables`: `x_in` (rank-1, `[3]`), `x_out` (rank-1, `[3]`), `max_abs_deviation` (scalar)
  - `judgment`: the sentinel reals `x_in` (the three values §2 declares) emitted via `harness_fortran_cpu_mpi__emit_real` and re-parsed into `x_out` reproduce the inputs within an absolute tolerance of `1e-12` (`max_abs_deviation = max|x_out - x_in| <= 1e-12`), and `checks.numeric_roundtrip.status == pass`.
  - `quantity`: `numeric_roundtrip` on both `pass_when` conditions (`checks.numeric_roundtrip.status == pass`; `per_case.<case_id>.verdict.overall == pass`).
  - `primary_predicate` (host-evaluated, quantity `numeric_roundtrip`, `per_case: true`): `maxabs(final.x_out - inputs.initial.x_in) <= 1.0e-12` — the re-parsed values, as the runner emitted them into the snapshot, against the declared sentinel.
- `test_id`: `l0_boolean_literal_pass`
  - `level`: `L0`
  - `operation_id`: `harness_fortran_cpu_mpi__emit_bool`
  - `expected_outcome`: `pass`
  - `required_raw_variables`: `bool_match` (scalar)
  - `judgment`: a `true` and a `false` boolean emit the exact JSON literals `true` / `false` (no language-specific boolean token), so `bool_match == 1.0`, and `checks.boolean_literal.status == pass`.
  - `quantity`: `boolean_literal` on both `pass_when` conditions.
  - `primary_predicate` (quantity `boolean_literal`, `per_case: true`): `final.bool_match == 1.0` (op `eq`).
- `test_id`: `l0_array_emit_pass`
  - `level`: `L0`
  - `operation_id`: `harness_fortran_cpu_mpi__emit_array_r2` (representative; the case exercises `__emit_array_r1..r4`)
  - `expected_outcome`: `pass`
  - `required_raw_variables`: `a1` (rank-1, `[2]`), `a2` (rank-2, `[2,2]`), `a3` (rank-3, `[2,2,2]`), `a4` (rank-4, `[2,2,2,2]`), `max_abs_deviation` (scalar)
  - `judgment`: the rank-1..4 real arrays `a1..a4` emitted via `harness_fortran_cpu_mpi__emit_array_r1..r4` produce well-formed nested JSON arrays whose parsed shape and element values match the inputs within `1e-12` (`max_abs_deviation <= 1e-12`), and `checks.array_emit.status == pass`.
  - `quantity`: `array_emit` on both `pass_when` conditions.
  - `primary_predicate` (quantity `array_emit`, `per_case: true`): `max(maxabs(final.a1 - inputs.initial.a1), maxabs(final.a2 - inputs.initial.a2), maxabs(final.a3 - inputs.initial.a3), maxabs(final.a4 - inputs.initial.a4)) <= 1.0e-12` — the emitted arrays, re-parsed by the host from the snapshot file, against the declared sentinels (shape and values: a wrong nesting order or extent is a shape-pairing error, which fails the predicate).
- `test_id`: `l0_case_fanout_pass`
  - `level`: `L0`
  - `operation_id`: `harness_fortran_cpu_mpi__write_snapshot`
  - `expected_outcome`: `pass`
  - `required_raw_variables`: `case_index` (scalar)
  - `judgment`: the case loop writes exactly one `raw/state_snapshots/<case_id>.json` per case (runtime-built name), each carrying its `required_raw_variables`, and `checks.case_fanout.status == pass`; `case_index` is this case's 1-based position in the case loop, so `1 <= case_index <= 12`.
  - `quantity`: `case_fanout` on both `pass_when` conditions.
  - `primary_predicate` (quantity `case_fanout`, `per_case: true`, two entries): `final.case_index >= 1.0` and `final.case_index <= 12.0`.
- `test_id`: `l0_perf_derived_pass`
  - `level`: `L0`
  - `operation_id`: `harness_fortran_cpu_mpi__write_perf`
  - `expected_outcome`: `pass`
  - `required_raw_variables`: `throughput_residual` (scalar)
  - `judgment`: `perf.json` carries all required fields and `throughput_cells_per_sec == cells_updated / walltime_sec` within a relative tolerance of `1e-9`: `throughput_residual = |throughput_cells_per_sec - cells_updated / walltime_sec| / throughput_cells_per_sec <= 1e-9` (the RELATIVE residual, so the snapshot value is judged against the tolerance alone), and `checks.perf_derived.status == pass`. `perf.json`'s `parallelism.mpi_ranks` is the `__comm_size()` of the run that wrote it (the Validate phase compares it with the target's `execution.ranks`); `threads_per_rank` is `1` and `gpu_devices` is `0`.
  - `quantity`: `perf_derived` on both `pass_when` conditions.
  - `primary_predicate` (quantity `perf_derived`, `per_case: true`): `final.throughput_residual <= 1.0e-9`.
- `test_id`: `l0_metric_leaf_pass`
  - `level`: `L0`
  - `operation_id`: `harness_fortran_cpu_mpi__write_diagnostics`
  - `expected_outcome`: `pass`
  - `required_raw_variables`: `metric_count` (scalar)
  - `judgment`: the case supplies the two `h_metric` records of §2 to `harness_fortran_cpu_mpi__write_diagnostics` and records `checks.metric_leaf.status == pass` when it supplied both, and the writer folds them into that case's `metrics` object: `selftest.metric_leaf` is present as a number equal to `0.25` within `1e-10`, `selftest.metric_na_reason_na` is present with the value `"not_computed"`, and `selftest.metric_na` carries the honest-`N/A` encoding — it is **not a real number**. A writer that drops the supplied metrics leaves `metrics` empty, so the `selftest.metric_leaf` and `selftest.metric_na_reason_na` addresses are absent and those conditions fail structurally. The `selftest.metric_na` condition asserts the N/A encoding by comparing (`eq`) against a non-numeric token: `na_allowed` accepts the honest `null` (and, because the predicate DSL resolves a present-`null` identically to an absent leaf, an absent leaf too), while any PRESENT numeric value — the supplied out-of-band `-1.0`, or any placeholder such as `0.0` — never equals the token and fails as a physics mismatch, so a writer that serializes a number where `null` is required is caught regardless of its sign. **Residual (not detectable by this or any predicate):** a writer that emits the `selftest.metric_na_reason_na` sibling but OMITS the `"selftest.metric_na": null` key is accepted, because the DSL cannot distinguish an absent leaf from a present `null` (the honest encoding the `na_allowed` clause must accept), and `value: null` is not an authorable comparison. This omission is benign for a consuming node, which reads an absent metric and a `null` metric identically through the same `na_allowed` mechanism. The evidence variable `metric_count` (`= 2.0`, the number of records supplied) is snapshot evidence of the supply, not a `diagnostics.json` address: it is carried in `raw/state_snapshots/l0_metric_leaf_pass.json` and `raw/metrics_basis.json`, and no `pass_when` condition references it.
  - `predicate_conditions` (the verbatim `pass_when.all` conditions, transcribed into the IR as this test's `io_contract.test_predicates[].pass_when.all`; the metric conditions are scoped `per_case: true` over this test's single target case, because a metric address exists only inside a `per_case` slice; every condition carries the one quantity `metric_leaf_fold` — the fold of the two supplied records into this case's `metrics` object, which is what all five assert a facet of):
    - `ref`: `selftest.metric_leaf`, `op`: `ge`, `value`: `0.2499999999`, `per_case`: `true`, `quantity`: `metric_leaf_fold`
    - `ref`: `selftest.metric_leaf`, `op`: `le`, `value`: `0.2500000001`, `per_case`: `true`, `quantity`: `metric_leaf_fold`
    - `ref`: `selftest.metric_na_reason_na`, `op`: `eq`, `value`: `not_computed`, `per_case`: `true`, `quantity`: `metric_leaf_fold`
    - `ref`: `selftest.metric_na`, `op`: `eq`, `value`: `expected_na` (a non-numeric token), `per_case`: `true`, `na_allowed`: `true`, `quantity`: `metric_leaf_fold`
    - `ref`: `checks.metric_leaf.status`, `op`: `eq`, `value`: `pass`, `quantity`: `metric_leaf_fold`
  - `primary_predicate` (quantity `metric_leaf_fold`, `per_case: true`): `final.metric_count == 2.0` (op `eq`) — the supply side of the fold, the one fact about it the snapshot carries. The fold's output lives in `diagnostics.json` alone, and this node's own writer is the trust root for both files (A6), so the corroboration here is that the supply the snapshot records and the fold the diagnostics report are the same two records; a stronger host check of the fold would need the records in the snapshot, which is not asked.
- `test_id`: `l0_missing_cases_xfail`
  - `level`: `L0`
  - `operation_id`: `harness_fortran_cpu_mpi__parse_cases`
  - `expected_outcome`: `xfail`
  - `required_raw_variables`: `guard_fired` (scalar)
  - `xfail_condition`: a `--cases` flag is absent from the token list passed to `__parse_cases`
  - `pass_when`: `per_case.l0_missing_cases_xfail.verdict.overall == fail` and `per_case.l0_missing_cases_xfail.verdict.failed_checks includes 'input_guard'` (calling `__parse_cases` on a length-0 token array returns `ok = false`, so `guard_fired == 1.0` and the guard fires).
  - `quantity`: `input_guard` on both `pass_when` conditions.
  - `primary_predicate` (quantity `input_guard`, `per_case: true`): `final.guard_fired == 1.0` (op `eq`) — the state fact the guard leaves behind; it must hold for the test to certify `xfail`.
- `test_id`: `l0_partition_tile_pass`
  - `level`: `L0`
  - `operation_id`: `harness_fortran_cpu_mpi__partition`
  - `expected_outcome`: `pass`
  - `required_raw_variables`: `owned_index` (rank-1, `[12]`), `small_index` (rank-1, `[2]`), `owner` (rank-1, `[12]`), `nranks` (scalar)
  - `judgment`: the owned ranges tile each global range exactly — gathered by global index, `owned_index` is `1..12` and `small_index` is `1..2`, including when ranks own nothing — within `1e-12`; every cell of the 12-cell range is owned by the rank the block rule of the spec's §3.3 assigns it for the `nranks` ranks of the run (`owner`); and `checks.partition_tile.status == pass`. The case's check compares `owner` with the block rule too, recomputed by the case.
  - `quantity`: `partition_tile` on both `pass_when` conditions.
  - `primary_predicate` (quantity `partition_tile`, `per_case: true`, two entries):
    - `max(maxabs(final.owned_index - inputs.initial.index_ref), maxabs(final.small_index - inputs.initial.small_ref)) <= 1.0e-12`.
    - the block rule, evaluated by the host for the rank count the run reports: `bind` `nb: "floor(inputs.initial.n_global / final.nranks)"`, `nm: "inputs.initial.n_global - nb * final.nranks"`, `owner_ref: "max(floor((inputs.initial.index_ref - 1) / (nb + 1)), floor((inputs.initial.index_ref - 1 - nm) / nb))"`; `expr` `maxabs(final.owner - owner_ref)`, `op` `le`, `value` `1.0e-12`. (`owner_ref` is rank `r` for the `nb + 1` cells of each of the first `nm` ranks and for the `nb` cells of each later rank, which is §3.3's rule; `nb >= 1` because the suite runs at most six ranks.) A single-process run reports `nranks = 1` and an `owner` of zeros, and passes; a run of four reports `nranks = 4` and must record owners `0,0,0,1,1,1,2,2,2,3,3,3`.
- `test_id`: `l0_halo_exchange_pass`
  - `level`: `L0`
  - `operation_id`: `harness_fortran_cpu_mpi__exchange_halo_r1` (representative; the case exercises `__exchange_halo_r1` and `__exchange_halo_r2`)
  - `expected_outcome`: `pass`
  - `required_raw_variables`: `halo_diff_r1` (rank-1, `[12]`), `halo_diff_r2` (rank-2, `[3,12]`)
  - `judgment`: after a NON-periodic exchange of `ng = 2` ghost cells, the width-2 central difference of the global index field, gathered, equals the declared reference within `1e-12` for both ranks of array: `4` at every interior cell — so every ghost cell next to a rank boundary holds the neighbour's owned value — and the untouched `0.0` beyond each physical end, and `checks.halo_exchange.status == pass`.
  - `quantity`: `halo_exchange` on both `pass_when` conditions.
  - `primary_predicate` (quantity `halo_exchange`, `per_case: true`): `max(maxabs(final.halo_diff_r1 - inputs.initial.diff_ref_r1), maxabs(final.halo_diff_r2 - inputs.initial.diff_ref_r2)) <= 1.0e-12`.
- `test_id`: `l0_halo_periodic_pass`
  - `level`: `L0`
  - `operation_id`: `harness_fortran_cpu_mpi__exchange_halo_r2` (representative; the case exercises `__exchange_halo_r1` and `__exchange_halo_r2`)
  - `expected_outcome`: `pass`
  - `required_raw_variables`: `halo_diff_r1` (rank-1, `[12]`), `halo_diff_r2` (rank-2, `[3,12]`)
  - `judgment`: after a PERIODIC exchange of `ng = 2` ghost cells, the same gathered difference equals the declared periodic reference within `1e-12` for both ranks of array — the first two and last two cells read the opposite end of the global field, with one rank as with several — and `checks.halo_periodic.status == pass`.
  - `quantity`: `halo_periodic` on both `pass_when` conditions.
  - `primary_predicate` (quantity `halo_periodic`, `per_case: true`): `max(maxabs(final.halo_diff_r1 - inputs.initial.diff_ref_r1), maxabs(final.halo_diff_r2 - inputs.initial.diff_ref_r2)) <= 1.0e-12`.
- `test_id`: `l0_gather_roundtrip_pass`
  - `level`: `L0`
  - `operation_id`: `harness_fortran_cpu_mpi__gather_r2` (representative; the case exercises `__gather_r1..r4`)
  - `expected_outcome`: `pass`
  - `required_raw_variables`: `g1` (rank-1, `[12]`), `g2` (rank-2, `[12,2]`), `g3` (rank-3, `[2,2,8]`), `g4` (rank-4, `[2,8,2,2]`)
  - `judgment`: gathering arrays partitioned along axis 1 (`g1`, `g2`), axis 3 (`g3`) and axis 2 with owned cells offset from the array's start (`g4`) assembles on rank 0 the global arrays of §2 — shape and values, with no `-1.0` from outside an owned range — within `1e-12`, and `checks.gather_roundtrip.status == pass`.
  - `quantity`: `gather_roundtrip` on both `pass_when` conditions.
  - `primary_predicate` (quantity `gather_roundtrip`, `per_case: true`): `max(maxabs(final.g1 - inputs.initial.g1_ref), maxabs(final.g2 - inputs.initial.g2_ref), maxabs(final.g3 - inputs.initial.g3_ref), maxabs(final.g4 - inputs.initial.g4_ref)) <= 1.0e-12` — the gathered arrays, re-parsed by the host from the snapshot file, against the declared references (a wrong placement or extent fails the predicate).
- `test_id`: `l0_reduce_pass`
  - `level`: `L0`
  - `operation_id`: `harness_fortran_cpu_mpi__reduce_sum` (representative; the case exercises `__reduce_sum`, `__reduce_max`, `__reduce_min` and `__reduce_sum_int`)
  - `expected_outcome`: `pass`
  - `required_raw_variables`: `red_sum`, `red_max`, `red_min`, `red_count` (scalars)
  - `judgment`: the global reductions of the partitioned field `0.5 * j`, `j = 1..12`, are its sum `39.0`, maximum `6.0` and minimum `0.5` within `1e-12`, and the global sum of the owned counts is `12`, and `checks.reduce.status == pass`.
  - `quantity`: `reduce` on both `pass_when` conditions.
  - `primary_predicate` (quantity `reduce`, `per_case: true`): `max(abs(final.red_sum - inputs.initial.sum_ref), abs(final.red_max - inputs.initial.max_ref), abs(final.red_min - inputs.initial.min_ref), abs(final.red_count - inputs.initial.n_global)) <= 1.0e-12`.
- `test_id`: `l0_multi_case_evidence_pass`
  - `level`: `L0`
  - `operation_id`: `harness_fortran_cpu_mpi__write_metrics_basis`
  - `expected_outcome`: `pass`
  - `target_cases`: `l0_array_emit_pass`, `l0_numeric_roundtrip_pass` (this test declares no case of its own — it is the suite's multi-target test)
  - `required_raw_variables`: `max_abs_deviation` (scalar)
  - `judgment`: `__write_metrics_basis` records this test's primary evidence for **every** case it targets — `raw/metrics_basis.json` carries one `per_test` entry per (`test_id`, target `case_id`) pair, so this test contributes two entries, each keyed by its own `case_id` and holding that case's `max_abs_deviation` — and both target cases round-trip within the `1e-12` tolerance, i.e. `per_case.l0_array_emit_pass.verdict.overall == pass` and `per_case.l0_numeric_roundtrip_pass.verdict.overall == pass`.
  - `quantity`: `max_abs_deviation` on every `pass_when` condition (the per-case `verdict.overall`, `checks.array_emit.status`, `checks.numeric_roundtrip.status`).
  - `primary_predicate` (quantity `max_abs_deviation`, `per_case: true` over both target cases): `final.max_abs_deviation <= 1.0e-12`.

## 7. Pass/fail aggregation rules
- `per_test.pass_rule`: `pass` when the judgment expression is satisfied. For the multi-target test the judgment must hold in EVERY target case.
- `per_test.xfail_rule`: `xfail` when `xfail_condition` is true and `pass_when` is satisfied (the input guard fires on a missing `--cases`).
- `per_test.evidence_rule`: `raw/metrics_basis.json` holds exactly one entry per (`test_id`, target `case_id`) pair — fourteen entries for the thirteen tests.
- `suite.pass_rule`: `pass` when all `test_id` are `pass` or `xfail`.

## 8. Traceability
- Record `test_profile_id` and `test_profile_version` in `trial_meta.json`.
