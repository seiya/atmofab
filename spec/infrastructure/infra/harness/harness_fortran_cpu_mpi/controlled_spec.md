# Controlled Spec: Fortran/CPU MPI runner harness (infrastructure spec)

## 0. Meta information
- `spec_id`: `harness_fortran_cpu_mpi`
- `spec_version`: `0.1.0`
- `status`: `controlled_draft`
- `spec_kind`: `infrastructure`
- `domain`: `infra`
- `family`: `harness`

## 1. Responsibility and scope
This `infrastructure` node (R1 harness) is responsible for the shared **runner plumbing** that every Fortran/CPU physics node's runner is built against: argv / `--cases` parsing, the case-set loop driver, the JSON emission machinery (numeric / integer / boolean / rank-1..4 real-array tokens), and the standard runner-output writers (`raw/state_snapshots/<case_id>.json`, `raw/metrics_basis.json`, `diagnostics.json`, `perf.json`). It carries **no physics**: the per-case kernel and the per-test check logic are supplied by the consuming physics node (a `case_run` / `checks_compute` callback in the physics `*_checks.f90`), never here. It targets `(language=fortran, hardware=cpu, parallel=mpi)`: a program built against it runs as several processes (ranks) started by the target's launcher, and a different `(language, hardware, parallel)` target is a separate harness node.

Beyond that plumbing, this node owns the program's **message-passing runtime**: it initializes and finalizes the runtime and publishes the distributed-state operations of §3.3 — the rank queries, a block partition of a global index range, the halo exchange of a partitioned array, the gather of a partitioned array onto rank 0, and the global reductions. It is the ONLY node of a program built for this target that calls the message-passing library: a consuming physics node never calls it directly and reaches the runtime only through these operations, and so does this node's own self-test runner. The library is reached from the Fortran model module alone, through its Fortran 2008 binding (the `mpi_f08` module); the program is compiled and linked by the target's compiler wrapper, never by naming the library's paths.

The plumbing of §3.2 is **rank-agnostic**: the writers are pure I/O, they neither ask nor test which rank calls them, and they write their paths relative to the working directory, which every rank shares. Serializing the output is therefore the CALLER's duty: a runner calls the four writers on rank 0 only (a host-rendered physics runner and this node's self-test runner alike), and computes every value it hands them so that rank 0 holds the global value — through the gather and the reductions of §3.3. A program built against this node gives the same diagnostics whatever number of ranks runs it, one included: the target's quality check re-runs the program as a single process, and the run under the launcher must agree with it.

The node's own generated code is a `harness_fortran_cpu_mpi_model.f90` publishing the plumbing operations plus a self-test `harness_fortran_cpu_mpi_runner.f90` that exercises them and emits the standard runner outputs (so the harness is verified through the exact same Compile→Generate→Build→Validate path as any node; it is self-hosting — the self-test writes its evidence using its own emitters).

The published surface is a **binding, signature-level contract**: §5.1 gives the canonical language-neutral structured interface block (every public type and every operation signature) in a machine-readable fenced code block, from which the language backend renders the Fortran surface. The generated `harness_fortran_cpu_mpi_model.f90` must publish exactly those signatures; a consuming physics node's host-rendered runner glue is written against them and holds no serialization knowledge of its own (the JSON envelope assembly and the verdict fold live only inside these certified operations).

## 2. input/output contract
Input (to the self-test runner): the standard runner argv `--cases <spec.ir.yaml> <case_id>...` — the spec path is taken positionally and need not be read; the trailing tokens are the `case_id`s to run, one per `case.test_case_set[]`. The main program marshals the process argv into a token array and calls `harness_fortran_cpu_mpi__parse_cases` on it. Each `case_id` selects, by dispatching on the `case_id`, the plumbing aspect that case verifies. Every `case_id` is also the `test_id` of the single-target test that names it, but the converse does not hold: a **multi-target** test ranges over several existing cases and declares no case of its own (`l0_multi_case_evidence_pass` in `tests.md`). A missing `--cases` flag (no cases) makes `__parse_cases` return `ok = false` (the input guard); the guard case verifies this by calling `__parse_cases` on a length-0 token array.

Output artifacts (produced by the writers, called on rank 0 only (§1), into the run node dir relative to `cwd=RUNDIR`; every value in them is the global one. The diagnostics and every snapshot variable but `owner` and `nranks` are identical whatever number of ranks ran; `owner`, `nranks` and `perf.json`'s `mpi_ranks` / `parallel_degree_total` record how the run was distributed, and `perf.json`'s timing and timestamp fields differ from run to run in any case):
- **`diagnostics.json`** — a JSON object with a top-level `checks` object holding one entry per `io_contract.diagnostics_contract.checks[].id` (each `{ "status": "pass"|"fail" }`), a top-level `verdict` object `{ "overall": "pass"|"fail", "failed_checks": [<check_id>...] }`, and a `per_case` map `{ <case_id>: { "checks": {...}, "verdict": { "overall", "failed_checks" }, "metrics": {...} } }` giving each case's own result. The assembly is done entirely inside `__write_diagnostics` from the caller-supplied per-case result records — the harness performs the fold, the caller supplies only the honest per-case check/metric data and each case's `expected_xfail` flag (§3). Top-level aggregation rule: `verdict.overall == fail` iff some case with `expected_xfail == false` has a failing per-case verdict; a per-case failure of a case with `expected_xfail == true` (the `input_guard` firing on the guard case) is EXCLUDED from the top-level `failed_checks`, so a run where the only failure is the expected guard reports top-level `{ "overall": "pass", "failed_checks": [] }` with `checks.input_guard.status == pass` (the guard behaved as expected). The per-case `input_guard` failure is confined to `per_case.<guard_case>`. The per-case `metrics` object holds one leaf per `h_metric` the caller supplied for that case (dotted-address key ⇒ numeric value; a `is_na` metric is written as `"<address>": null` plus a sibling `"<address>_reason_na": "<reason>"`). The object is produced by iterating that case's supplied array and writing exactly one leaf per record (a record with `is_na = false` emits its single key and no sibling); a `metrics` body that does not iterate the supplied array — one selected by `case_id`, or one emitting a fixed key set — is forbidden (§6) — the consuming physics nodes supply arbitrary records, so a body that reproduces one suite's expected keys is not an implementation of the fold. Exactly one self-test case supplies metrics: `l0_metric_leaf_pass` supplies the two sentinel `h_metric` records of §3, so the fold yields `"metrics": { "selftest.metric_leaf": 0.25, "selftest.metric_na": null, "selftest.metric_na_reason_na": "not_computed" }` for that case (an instance of the fold, not the writer's body; the numeric value is written as the round-trip-lossless token of the serialization rule below, abbreviated here for readability); every other case supplies a length-0 `metrics` array, so the same fold yields `{}`.
- **`perf.json`** — one object with `case_id`, `target` (`"cpu"`), `walltime_sec`, `steps`, `cells_updated`, `throughput_cells_per_sec` (`= cells_updated / walltime_sec`), a `parallelism` object (`mpi_ranks`, `threads_per_rank`, `gpu_devices`, `parallel_degree_total = mpi_ranks*threads_per_rank*max(gpu_devices,1)`), and `timestamp_utc` (ISO-8601). `mpi_ranks` is the number of ranks the run started, as `__comm_size` reports it — never a constant: the Validate phase compares it with the target's `execution.ranks`.
- **`raw/state_snapshots/<case_id>.json`** — exactly one per case, named at runtime as `'raw/state_snapshots/'//trim(case_id)//'.json'` (never a hardcoded/sequential literal). Each holds every variable in *that case's* `io_contract.test_evidence_requirements.required_raw_variables` plus the declared scalar `time_variable` `t` (value `0.0`; the self-test has a single `steps=1` step). The snapshot state variables (declared in `snapshot_schema.json` with their `shape_expr`) are, per case:
  - `l0_numeric_roundtrip_pass`: `x_in` (rank-1, `[3]` — the sentinel reals), `x_out` (rank-1, `[3]` — the values re-parsed from `__emit_real`'s tokens), `max_abs_deviation` (scalar).
  - `l0_boolean_literal_pass`: `bool_match` (scalar, `1.0` iff a `true` and a `false` boolean emitted the exact literals `true`/`false`).
  - `l0_array_emit_pass`: `a1` (rank-1, `[2]`), `a2` (rank-2, `[2,2]`), `a3` (rank-3, `[2,2,2]`), `a4` (rank-4, `[2,2,2,2]`) — the inputs to `__emit_array_r1..r4` — and `max_abs_deviation` (scalar — max component deviation of the re-parsed arrays).
  - `l0_case_fanout_pass`: `case_index` (scalar — this case's ordinal in the run).
  - `l0_perf_derived_pass`: `throughput_residual` (scalar — the RELATIVE residual `|throughput_cells_per_sec - cells_updated/walltime_sec| / throughput_cells_per_sec` that `tests.md` judges).
  - `l0_metric_leaf_pass`: `metric_count` (scalar — the number of `h_metric` records this case supplied to `__write_diagnostics`, `2.0`).
  - `l0_missing_cases_xfail`: `guard_fired` (scalar, `1.0` when `__parse_cases` on a length-0 token array returned `ok = false`). A guard case still emits its snapshot, shape-valid.
  - `l0_partition_tile_pass`: `owned_index` (rank-1, `[14]` — the gathered global index of every cell of a 14-cell partition), `small_index` (rank-1, `[2]` — the same over a 2-cell partition, which leaves every rank above rank 1 empty), `owner` (rank-1, `[14]` — the gathered rank that owns each cell of the 14-cell partition), `nranks` (scalar — `__comm_size()`).
  - `l0_halo_exchange_pass` and `l0_halo_periodic_pass`: `halo_diff_r1` (rank-1, `[12]`), `halo_diff_r2` (rank-2, `[3,12]`, partitioned along axis 2), `halo_diff_r2a1` (rank-2, `[12,3]`, partitioned along axis 1) — the gathered central difference, of the width of each array's ghost region, of a partitioned field quadratic in the global index, taken after a non-periodic (resp. periodic) halo exchange.
  - `l0_gather_roundtrip_pass`: `g1` (rank-1, `[12]`), `g2` (rank-2, `[12,2]`), `g3_a1` … `g3_a3` (rank-3, `[4,4,4]`), `g4_a1` … `g4_a4` (rank-4, `[3,3,3,3]`) — the gathered global arrays, the rank-3 and rank-4 ones gathered along each of their axes in turn.
  - `l0_reduce_pass`: `red_sum`, `red_max`, `red_min` (scalars — the global sum, maximum and minimum of a partitioned field), `red_count` (scalar — the global sum of the ranks' owned cell counts), `reduce_mismatch` (scalar — the global count of reduction results a rank received that differ from the reference, `0.0`).
  A snapshot variable of the five distributed cases is the GLOBAL value rank 0 holds after a gather or a reduction. Every one of them except `owner` and `nranks` is independent of the number of ranks; those two are the record of how the run was distributed, and the host checks the one against the other.
  Each case's `required_raw_variables` is exactly its listed variables above; a variable is declared once in `snapshot_schema.json` and emitted only by the cases that require it.
- **`raw/metrics_basis.json`** — a `{ "per_test": [ <entry>, ... ] }` index holding only primary evidence (never a copy of `diagnostics.json`). Assembled inside `__write_metrics_basis` from the caller-supplied entry records (§3). There is exactly **one entry per (`test_id`, target `case_id`) pair**: a test's primary evidence is the evidence of every case its predicate ranges over, so a single-target test contributes one entry and a multi-target test one entry per targeted case. `(test_id, case_id)` is the unique entry key, and a single-target test carries its `case_id` too — there is no special case. Each entry is a **flat JSON object**: the `test_id` key, the `case_id` key, and every name in that test's `io_contract.test_evidence_requirements.required_raw_variables` as a **direct sibling key of `test_id`**, valued from that entry's own case. Wrapping the required variables under any nested object is forbidden — in particular under the literal key `values`, which is the Fortran component name of `harness_fortran_cpu_mpi__h_mb_entry` (§3.1) and never a JSON key. The `post_execute` gate rejects an entry that nests them under the unrecognized key `values` as `missing required_raw_variables`, and rejects an entry that omits `case_id`. One entry, with its numeric tokens abbreviated for readability (the serialization rule below governs their emitted form):

  ```json
  {
    "test_id": "l0_array_emit_pass",
    "case_id": "l0_array_emit_pass",
    "a1": [1.0, 2.0],
    "a2": [[1.0, 2.0], [3.0, 4.0]],
    "a3": [[[1.0, 2.0], [3.0, 4.0]], [[5.0, 6.0], [7.0, 8.0]]],
    "a4": [[[[1.0, 2.0], [3.0, 4.0]], [[5.0, 6.0], [7.0, 8.0]]], [[[9.0, 10.0], [11.0, 12.0]], [[13.0, 14.0], [15.0, 16.0]]]],
    "max_abs_deviation": 0.0
  }
  ```

  The wrapped form `{ "test_id": "l0_array_emit_pass", "values": { "a1": [1.0, 2.0], … } }` is rejected. The multi-target test `l0_multi_case_evidence_pass` contributes two entries, `{ "test_id": "l0_multi_case_evidence_pass", "case_id": "l0_array_emit_pass", "max_abs_deviation": 0.0 }` and the same with `"case_id": "l0_numeric_roundtrip_pass"`, so the self-test's index holds fourteen entries for its thirteen tests.

Numeric serialization follows the abstract runner-output contract: a real as a round-trip-lossless exponential token, an integer as its minimal decimal, a boolean as the literal token `true`/`false` — a truncating form (one that may drop a leading digit) and any language-specific boolean token are forbidden. The target-language realization (for Fortran: reals via `ES24.16E3` then `trim(adjustl())`, or a bounded `Fw.d`, never `F0`/`F0.d`; integers via `I0`; booleans by branching to the literal, never an `L`-family descriptor) is canonical in `docs/workflow/RUNNER_OUTPUT_CONTRACT.md §4`.

## 3. Operation definition
The published operations (all under module `harness_fortran_cpu_mpi_model`, prefix `harness_fortran_cpu_mpi__`) are the plumbing surface a physics-node runner reuses. The canonical machine-readable interface (every signature and public type, verbatim) is §5.1; the prose below states each operation's contract. The module declares two module-level integer parameters the signatures reference: `dp` (the double-precision real kind token; §5.1 value `float64` = IEEE-754 binary64) and `case_id_len = 64` (the fixed storage width of a parsed case id, known to the caller, because an assumed-length `intent(out)` string dummy is disallowed). These are internal parameters (not part of the public export list); a consuming runner passes matching double-precision-real actuals and declares its own fixed-width (`case_id_len`) case-id buffer. Their VALUES are pinned (the gate rejects a drifted `case_id_len`), because a consumer's hardcoded length must match. The Fortran binding of these tokens (`dp = real64`, `character(len=64)`) is produced by the language backend, not authored here.

### 3.1 Published derived types
The module publishes five derived types (each named by its fully-qualified `harness_fortran_cpu_mpi__<name>`):
- `harness_fortran_cpu_mpi__h_named` `{ name: string (dynamic length), json: string (dynamic length) }` — a **boxed named value**: a JSON key `name` and its already-serialized JSON value `json`. It lets heterogeneous-rank / ragged snapshot and metrics-basis variables travel through one homogeneous rank-1 dynamic array of `harness_fortran_cpu_mpi__h_named` (a homogeneous array argument cannot carry differing ranks — this box, holding the pre-serialized token, is the workaround). The caller `__box`es a value into it before serialization is complete. (Types and lengths use the §5.1 neutral vocabulary; the Fortran binding is the language backend's.)
- `harness_fortran_cpu_mpi__h_check` `{ id: string (dynamic length), status: string (length 4) }` — one per-case check result: its `id` and honest `status` (`'pass'` / `'fail'` / `'na  '`, right-padded to width 4). The caller computes `status`; the harness does not judge.
- `harness_fortran_cpu_mpi__h_metric` `{ name: string (dynamic length), value: real (kind dp), is_na: boolean, reason_na: string (dynamic length) }` — one per-case metric leaf: its dotted-address `name`, numeric `value`, an `is_na` flag, and a `reason_na` string used only when `is_na` is true.
- `harness_fortran_cpu_mpi__h_case_result` `{ case_id: string (dynamic length), expected_xfail: boolean, checks: rank-1 dynamic array of h_check, metrics: rank-1 dynamic array of h_metric }` — one case's complete result: its `case_id`, whether its failure is expected (`expected_xfail`, driving the top-level xfail exclusion), and its `checks` and `metrics` arrays. This is the data-driven record `__write_diagnostics` folds; it embeds no harness-side judgment.
- `harness_fortran_cpu_mpi__h_mb_entry` `{ test_id: string (dynamic length), case_id: string (dynamic length), values: rank-1 dynamic array of h_named }` — one metrics-basis entry: the `test_id` it is evidence for, the `case_id` that evidence was taken from, and the caller-boxed (ragged, any-rank) `values` that form its primary-evidence body. A multi-target test contributes one entry per targeted case, so `(test_id, case_id)` — not `test_id` alone — is unique across the `entries` array handed to `__write_metrics_basis`.

### 3.2 Published operations
- `harness_fortran_cpu_mpi__parse_cases(tokens, ntokens, case_ids, ncases, ok)` — parse a supplied argv **token list** (NOT the process argv directly, so the guard is exercisable with a synthetic list): find `--cases` in `tokens`, skip the following token (the positional, unread spec path), collect the remaining tokens as `case_ids` (each stored in a fixed-width `case_id_len` slot); return `ok = false` when `--cases` is absent from `tokens` or no `case_id` follows it (the input guard), else `ok = true`. The self-test's main program marshals the real process argv (via the language-standard argv accessors) into `tokens` for the normal cases, and passes a length-0 `tokens` for the guard case.
- `harness_fortran_cpu_mpi__emit_real(x) result(s)` — format a `real (kind dp)` scalar as a JSON numeric token (round-trip-lossless exponential form; §2).
- `harness_fortran_cpu_mpi__emit_int(i) result(s)` — format an integer as a JSON numeric token (minimal decimal; §2).
- `harness_fortran_cpu_mpi__emit_bool(b) result(s)` — format a `boolean` as the JSON literal `true`/`false`.
- `harness_fortran_cpu_mpi__emit_array_r1(a) result(s)` … `__emit_array_r4(a) result(s)` — format an assumed-shape rank-1..4 `real (kind dp)` array as a nested JSON array (`[ ... ]`, row-major over the leading index), reusing `__emit_real` per element.
- `harness_fortran_cpu_mpi__box(name, json) result(nv)` — pack a JSON key `name` and an already-serialized JSON value `json` into a `harness_fortran_cpu_mpi__h_named`. A consuming runner emits each of a case's snapshot variables (via the matching `__emit_*`), boxes each with `__box`, and hands the resulting `values` array to `__write_snapshot` in one call; in a physics node the host-rendered glue emits this `__box` list mechanically from `snapshot_schema.json` (the harness stays stateless — it holds no snapshot registry, and does no serialization of the boxed value beyond copying the caller's token).
- `harness_fortran_cpu_mpi__write_snapshot(case_id, values, time)` — write the per-case `raw/state_snapshots/<case_id>.json` (runtime-built filename) holding the boxed state variables in `values` (a rank-1 array of `harness_fortran_cpu_mpi__h_named`) plus the scalar time variable. The emitted object is **flat**: each boxed variable is written as a **top-level key of the snapshot object**, keyed by its `name` with its already-serialized `json` as the value, sibling to the time variable's key. `values` is the dummy-argument name of the boxed array; neither it nor any other wrapper key appears in the emitted JSON.
- `harness_fortran_cpu_mpi__write_metrics_basis(entries, n)` — write `raw/metrics_basis.json` as the `per_test` index from `entries(1:n)` (a rank-1 array of `harness_fortran_cpu_mpi__h_mb_entry`); the harness assembles the `{ "per_test": [ ... ] }` envelope and, per entry, a **flat body**: the entry's `test_id` key, its `case_id` key, followed by one key per element of that entry's `values` array, each written under its `name` with its already-serialized `json` as a **direct sibling key of `test_id`**. `values` is the component name of `harness_fortran_cpu_mpi__h_mb_entry` (§3.1); neither it nor any other wrapper key appears in the emitted JSON (§2 gives the literal entry shape and the rejected shape). The writer emits one JSON entry per supplied record, in the caller's order, and neither deduplicates nor reorders: the caller owns the `(test_id, case_id)` product. **Data-driven plumbing**: the caller supplies the boxed evidence; the harness owns the JSON envelope.
- `harness_fortran_cpu_mpi__write_diagnostics(results, n)` — write `diagnostics.json` from `results(1:n)` (a rank-1 array of `harness_fortran_cpu_mpi__h_case_result`). The harness computes every derived value: each case's per-case verdict (`overall == fail` iff any of that case's `checks` has `status == 'fail'`; `failed_checks` = those check ids), the top-level `checks` object (one entry per distinct check id; `status == fail` iff that id fails in some case with `expected_xfail == false`), and the top-level `verdict` (xfail-excluded fold per §2). It emits the full JSON: top-level `checks` / `verdict` and the `per_case` map (each case's `checks`, `verdict`, and `metrics` leaf object, the latter produced by iterating that case's supplied `metrics` array and writing one leaf per record, with an `is_na` metric encoded as `null` + a `_reason_na` sibling). **Data-driven plumbing, not judgment**: the harness folds and serializes; the per-case check statuses, metric values, and each case's `expected_xfail` are computed by the (self-test or physics) caller and passed in. It embeds no per-test pass/fail decision of its own.
- `harness_fortran_cpu_mpi__write_perf(case_id, target, steps, cells_updated, walltime_sec, mpi_ranks, threads_per_rank, gpu_devices)` — write `perf.json` with all required fields incl. the derived `throughput_cells_per_sec` and the `parallelism` object (`parallel_degree_total = mpi_ranks*threads_per_rank*max(gpu_devices,1)`). It writes `mpi_ranks`, `threads_per_rank` and `gpu_devices` exactly as its caller passes them, and queries nothing to replace them — like every operation of this section it is rank-agnostic (§1); the count of ranks is the caller's to obtain, from `__comm_size`.

### 3.3 Published distributed-state operations
These operations reach the message-passing runtime; every other operation of this node is rank-agnostic (§1). Ranks are numbered from `0` to `size - 1` over all the processes the launcher started, and there is one communicator, the one holding every process. An operation marked **collective** is called by every rank, in the same order relative to every other collective operation, with arguments that agree across ranks where this section says so; a rank that skips a collective call, or calls collectives in a different order, is a program error this node does not diagnose.

- `harness_fortran_cpu_mpi__init()` — initialize the runtime. A program calls it exactly once, before any other operation of this section and before `__parse_cases`. Collective.
- `harness_fortran_cpu_mpi__finalize()` — finalize the runtime. A program calls it exactly once, after its last operation of this section and after the writers have returned; no operation of this section may follow it. Collective.
- `harness_fortran_cpu_mpi__comm_rank() result(r)` — this process's rank, `0 <= r < size`.
- `harness_fortran_cpu_mpi__comm_size() result(n)` — the number of ranks, `n >= 1`; `1` when the program was started as a single process without the launcher.
- `harness_fortran_cpu_mpi__partition(n_global, glo, ghi)` — the block partition of the global index range `1..n_global` (`n_global >= 0`): with `b = n_global / size` (integer division) and `m = mod(n_global, size)`, rank `r` owns `c = b + 1` cells when `r < m` and `c = b` cells otherwise, starting at `glo = r*b + min(r, m) + 1`, and `ghi = glo + c - 1`. The owned ranges are contiguous, in rank order, disjoint, and together cover `1..n_global`; the remainder goes to the lowest ranks. A rank that owns nothing (`n_global < size`) gets `ghi = glo - 1`. It needs no communication.
- `harness_fortran_cpu_mpi__exchange_halo_r1(a, ng, periodic)` and `harness_fortran_cpu_mpi__exchange_halo_r2(a, axis, ng, periodic)` — fill the ghost cells of a partitioned array from its neighbours' owned cells. `a` is this rank's local array: along the partitioned axis (`axis`, `1` or `2`; the rank-1 form has only axis `1`) it holds `ng` ghost cells, then its `nloc` owned cells, then `ng` ghost cells, so its extent there is `nloc + 2*ng`, and its owned cells are positions `ng+1 .. ng+nloc` counting from `1` (`a` is assumed-shape: the positions are those of the dummy, whatever bounds the caller declared). The owned cells are the rank's block as `__partition` assigns it, so the rank below owns the cells just before this rank's first owned cell and the rank above the cells just after its last. On return, the `ng` leading ghost cells hold the last `ng` owned cells of rank `r - 1`, and the `ng` trailing ghost cells hold the first `ng` owned cells of rank `r + 1`, each in its global order, over the whole extent of the other axis. With `periodic = .true.`, rank `0`'s leading neighbour is rank `size - 1` and rank `size - 1`'s trailing neighbour is rank `0`; with a single rank, the leading ghosts then hold the rank's own last `ng` owned cells and the trailing ghosts its own first `ng`. With `periodic = .false.`, rank `0`'s leading ghosts and rank `size - 1`'s trailing ghosts are left as the caller set them (the physical boundary is the caller's), and with a single rank nothing is changed. The owned cells are never changed. `ng = 0` returns without change. Every rank must own at least `ng` cells (`nloc >= ng`), and the extent of the other axis must be the same on every rank. Collective.
- `harness_fortran_cpu_mpi__gather_r1(a, lo, hi, glo, g)` and `harness_fortran_cpu_mpi__gather_r2(a, axis, lo, hi, glo, g)` … `__gather_r4(a, axis, lo, hi, glo, g)` — assemble a partitioned array on rank 0. `a` is this rank's local array (assumed-shape, positions counted from `1`); along `axis` (`1..k` for the rank-`k` form; the rank-1 form has only axis `1`) its owned cells are positions `lo .. hi`, and position `lo` is global index `glo`. A rank that owns nothing passes `hi < lo`, and its `glo` is not read. The owned counts `c = max(hi - lo + 1, 0)` of all ranks define the global extent `n = sum(c)`. **Tile check:** the non-empty ranks' global ranges `glo .. glo + c - 1` must be pairwise disjoint and each lie within `1..n` — so together they cover `1..n` exactly, with neither overlap nor gap — and every rank's extents along the other axes must be equal; otherwise the operation stops the program (§4). On rank 0, `g` is allocated with `a`'s extents except `n` along `axis`, and holds every rank's owned cells at their global positions: position `glo + i` of `g` along `axis` is position `lo + i` of that rank's `a`, for `0 <= i < c`, at every position of the other axes. On every other rank `g` is returned unallocated. With a single rank the result is `a`'s owned slice. Collective.
- `harness_fortran_cpu_mpi__reduce_sum(x) result(s)`, `__reduce_max(x) result(s)`, `__reduce_min(x) result(s)` — the sum, maximum and minimum of the `real (kind dp)` scalar `x` over all ranks, returned on EVERY rank. `__reduce_sum_int(i) result(s)` is the integer sum. Collective.

The operations of this section publish no derived type: their arguments are integers, logicals and `real (kind dp)` scalars and arrays.

The self-test `harness_fortran_cpu_mpi_runner.f90` first calls `__init`, then `__parse_cases`, then for each `case_id` runs (dispatching on the `case_id`) the plumbing check that case names — verifying the emitter round-trips, the case fan-out, the input guard, and the five distributed operations of `tests.md` — and builds that case's `h_case_result` (its `checks`, its `metrics`, and `expected_xfail` from the case's expected outcome). Every rank runs every case, in the same order, so that every collective call of every case is made on every rank; a check's status is decided from values every rank holds (a reduction's result) or from values rank 0 holds (a gathered array), and rank 0's status is the one written. Only `l0_metric_leaf_pass` supplies metrics, so that the metric fold of `__write_diagnostics` is exercised inside this node rather than only by a consuming physics node: that case builds exactly two `harness_fortran_cpu_mpi__h_metric` records — `{ name = 'selftest.metric_leaf', value = 0.25, is_na = false, reason_na = '' }` (`0.25` is exactly representable in binary floating point, so the emitted token round-trips without deviation) and `{ name = 'selftest.metric_na', value = -1.0, is_na = true, reason_na = 'not_computed' }` (`-1.0` is an out-of-band value a correct writer never serializes, because an `is_na` metric is written as `null`) — and no third record, because the `"selftest.metric_na_reason_na"` key is derived by the writer from the second record's `reason_na`. It records its `metric_leaf` check as `pass` when it supplied both records, and emits `metric_count = 2.0` into its snapshot. Every other case supplies a length-0 `metrics` array. It then builds the `h_mb_entry` array over the `(test_id, case_id)` product of §2 (one entry per case each test targets, so a multi-target test yields several), calls the four writers **on rank 0 only** — `__write_snapshot` once per case, and `__write_metrics_basis`, `__write_diagnostics` and `__write_perf` once, passing `__comm_size()` as `mpi_ranks` — and finally calls `__finalize` on every rank. No other rank writes a file. Because the writers use the emitters, a correct emitter is necessary for a correct output; the checks are the harness's own verification that its plumbing is faithful.

## 4. Failure conditions and constraints
A missing `--cases` flag (or no `case_id` after it) is a hard input error — `__parse_cases` returns `ok = false`, and the self-test's `l0_missing_cases_xfail` case exercises this guard by calling `__parse_cases` on a synthesized empty token list and confirming `ok = false` (recorded as the `input_guard` check firing, with the case's `h_case_result` carrying `expected_xfail = true`). A JSON emitter whose re-parsed token does not reproduce its input within an absolute tolerance of `1e-12` is a failure of the corresponding check.

The operations of §3.3 stop the program with a non-zero exit status (`error stop`), so that the run fails rather than writing evidence from an inconsistent state, when: `__gather_r*`'s tile check fails (§3.3); an `axis` is outside `1..k` for a rank-`k` array; `__exchange_halo_r*` is given `ng < 0`, or `ng` larger than this rank's owned count; a gather's `lo .. hi` of a non-empty rank lies outside `a`'s extent along `axis`; `__partition` is given `n_global < 0`. A failure the message-passing library reports is left to its default handler, which aborts every rank. A stop on one rank ends the run as a failure, and the run's missing or partial outputs are then the Validate phase's to report; no rank attempts recovery.

## 5. Public API and compatibility
The published `operation_id`s are exactly: `harness_fortran_cpu_mpi__parse_cases`, `harness_fortran_cpu_mpi__emit_real`, `harness_fortran_cpu_mpi__emit_int`, `harness_fortran_cpu_mpi__emit_bool`, `harness_fortran_cpu_mpi__emit_array_r1`, `harness_fortran_cpu_mpi__emit_array_r2`, `harness_fortran_cpu_mpi__emit_array_r3`, `harness_fortran_cpu_mpi__emit_array_r4`, `harness_fortran_cpu_mpi__box`, `harness_fortran_cpu_mpi__write_snapshot`, `harness_fortran_cpu_mpi__write_metrics_basis`, `harness_fortran_cpu_mpi__write_diagnostics`, `harness_fortran_cpu_mpi__write_perf`, and the distributed-state operations of §3.3: `harness_fortran_cpu_mpi__init`, `harness_fortran_cpu_mpi__finalize`, `harness_fortran_cpu_mpi__comm_rank`, `harness_fortran_cpu_mpi__comm_size`, `harness_fortran_cpu_mpi__partition`, `harness_fortran_cpu_mpi__exchange_halo_r1`, `harness_fortran_cpu_mpi__exchange_halo_r2`, `harness_fortran_cpu_mpi__gather_r1`, `harness_fortran_cpu_mpi__gather_r2`, `harness_fortran_cpu_mpi__gather_r3`, `harness_fortran_cpu_mpi__gather_r4`, `harness_fortran_cpu_mpi__reduce_sum`, `harness_fortran_cpu_mpi__reduce_max`, `harness_fortran_cpu_mpi__reduce_min`, `harness_fortran_cpu_mpi__reduce_sum_int`. The module also publishes the derived types `harness_fortran_cpu_mpi__h_named`, `harness_fortran_cpu_mpi__h_check`, `harness_fortran_cpu_mpi__h_metric`, `harness_fortran_cpu_mpi__h_case_result`, and `harness_fortran_cpu_mpi__h_mb_entry`.

A change breaking compatibility of any signature (or of a published derived type's component layout) is a **breaking change released under a new `spec_version`**, not a rename. A change to how the published surface is CARRIED — the §5.1 / `IR public_api.signatures` REPRESENTATION — is likewise released under a new `spec_version` even when the ABI is byte-identical, because dependency freshness invalidates a stale certified `IR` only via its version. `0.1.0` is the first version: its §5.1 block is that of `harness_fortran_cpu@0.7.0` under this node's names, followed by the fifteen operations of §3.3; its self-test is that of `harness_fortran_cpu@0.7.0`, run on every rank with rank 0 writing, plus five cases for the distributed operations. Dependent nodes are not migrated by hand and need no content-free version bump of their own. The workflow enforces the skew mechanically at two points:

- **Regeneration** — a node's certified dependency resolution is recorded in its `dependency_graph.json` sidecar; when the catalog moves the harness to a new version, every dependent's recorded resolution stops matching the one `deps.yaml` + `spec_catalog.yaml` derive, so the dependency-freshness readiness check reports it stale and `run_workflow.py --with-deps` re-certifies the closure bottom-up.
- **Skew fail-close** — a consumer that would nonetheless render its runner glue against a drifted interface is stopped before Build by the renderer's signature pin (the language backend's `assert_harness_pin`, reached through `tools/host_render.py`), which compares this §5.1 block against the certified harness IR's `public_api.signatures` and its generated model source — for every operation the rendered runner calls, and for every operation of §3.3 the runner offers a consuming physics node.

### 5.1 Canonical interface block
The exact published surface, as a machine-readable **language-neutral** signature block (`module_parameters` / `types` / `procedures`). It describes each published type and operation abstractly — for every argument, result, and derived-type component: its `name`, neutral `type` (`real` / `integer` / `logical` / `string` / `derived`), `rank`, and (for an argument) `intent`; plus the value-pinned module parameters the signatures reference. The vocabulary is neutral throughout: a `string` length is a neutral token — `deferred` (dynamic length) / `assumed` (caller-known length) / a fixed decimal width / a symbol — never the Fortran `:` / `*`; a module-parameter kind value is `float64` / `float32`, never the Fortran `real64` / `real32`. The target language's binding (here Fortran: `real(dp)`, `character(len=:)` / `character(len=*)`, `type(...)`, assumed-shape `(:)` ranks, `integer, parameter :: dp = real64`, the `<spec_id>__` names) is produced by the language backend (`tools/backends/language/fortran/signatures`), not authored here — so this contract is not tied to Fortran. The generated `harness_fortran_cpu_mpi_model.f90` must publish every symbol below with the signature this block describes (formatting/continuations/comments may differ; names, argument order, types, ranks, `intent`s, `result` names, and component layout may not). The deterministic gates render this block to the target language and pin it: the `--stage compile` gate cross-checks its symbol set against §5, and the `Generate.static` gate pins the generated model source against these signatures (normalized: comments stripped, continuations joined, case-folded, whitespace-insensitive).

```yaml
module_parameters:
- name: dp
  value: float64
- name: case_id_len
  value: '64'
types:
- name: harness_fortran_cpu_mpi__h_named
  components:
  - name: name
    spec:
      type: string
      len: deferred
      alloc: true
  - name: json
    spec:
      type: string
      len: deferred
      alloc: true
- name: harness_fortran_cpu_mpi__h_check
  components:
  - name: id
    spec:
      type: string
      len: deferred
      alloc: true
  - name: status
    spec:
      type: string
      len: '4'
- name: harness_fortran_cpu_mpi__h_metric
  components:
  - name: name
    spec:
      type: string
      len: deferred
      alloc: true
  - name: value
    spec:
      type: real
      kind: dp
  - name: is_na
    spec:
      type: logical
  - name: reason_na
    spec:
      type: string
      len: deferred
      alloc: true
- name: harness_fortran_cpu_mpi__h_case_result
  components:
  - name: case_id
    spec:
      type: string
      len: deferred
      alloc: true
  - name: expected_xfail
    spec:
      type: logical
  - name: checks
    rank: 1
    spec:
      type: derived
      name: harness_fortran_cpu_mpi__h_check
      alloc: true
  - name: metrics
    rank: 1
    spec:
      type: derived
      name: harness_fortran_cpu_mpi__h_metric
      alloc: true
- name: harness_fortran_cpu_mpi__h_mb_entry
  components:
  - name: test_id
    spec:
      type: string
      len: deferred
      alloc: true
  - name: case_id
    spec:
      type: string
      len: deferred
      alloc: true
  - name: values
    rank: 1
    spec:
      type: derived
      name: harness_fortran_cpu_mpi__h_named
      alloc: true
procedures:
- kind: subroutine
  name: harness_fortran_cpu_mpi__parse_cases
  args:
  - name: tokens
    rank: 1
    intent: in
    spec:
      type: string
      len: assumed
  - name: ntokens
    intent: in
    spec:
      type: integer
  - name: case_ids
    rank: 1
    intent: out
    spec:
      type: string
      len: case_id_len
  - name: ncases
    intent: out
    spec:
      type: integer
  - name: ok
    intent: out
    spec:
      type: logical
- kind: function
  name: harness_fortran_cpu_mpi__emit_real
  args:
  - name: x
    intent: in
    spec:
      type: real
      kind: dp
  result:
    name: s
    spec:
      type: string
      len: deferred
      alloc: true
- kind: function
  name: harness_fortran_cpu_mpi__emit_int
  args:
  - name: i
    intent: in
    spec:
      type: integer
  result:
    name: s
    spec:
      type: string
      len: deferred
      alloc: true
- kind: function
  name: harness_fortran_cpu_mpi__emit_bool
  args:
  - name: b
    intent: in
    spec:
      type: logical
  result:
    name: s
    spec:
      type: string
      len: deferred
      alloc: true
- kind: function
  name: harness_fortran_cpu_mpi__emit_array_r1
  args:
  - name: a
    rank: 1
    intent: in
    spec:
      type: real
      kind: dp
  result:
    name: s
    spec:
      type: string
      len: deferred
      alloc: true
- kind: function
  name: harness_fortran_cpu_mpi__emit_array_r2
  args:
  - name: a
    rank: 2
    intent: in
    spec:
      type: real
      kind: dp
  result:
    name: s
    spec:
      type: string
      len: deferred
      alloc: true
- kind: function
  name: harness_fortran_cpu_mpi__emit_array_r3
  args:
  - name: a
    rank: 3
    intent: in
    spec:
      type: real
      kind: dp
  result:
    name: s
    spec:
      type: string
      len: deferred
      alloc: true
- kind: function
  name: harness_fortran_cpu_mpi__emit_array_r4
  args:
  - name: a
    rank: 4
    intent: in
    spec:
      type: real
      kind: dp
  result:
    name: s
    spec:
      type: string
      len: deferred
      alloc: true
- kind: function
  name: harness_fortran_cpu_mpi__box
  args:
  - name: name
    intent: in
    spec:
      type: string
      len: assumed
  - name: json
    intent: in
    spec:
      type: string
      len: assumed
  result:
    name: nv
    spec:
      type: derived
      name: harness_fortran_cpu_mpi__h_named
- kind: subroutine
  name: harness_fortran_cpu_mpi__write_snapshot
  args:
  - name: case_id
    intent: in
    spec:
      type: string
      len: assumed
  - name: values
    rank: 1
    intent: in
    spec:
      type: derived
      name: harness_fortran_cpu_mpi__h_named
  - name: time
    intent: in
    spec:
      type: real
      kind: dp
- kind: subroutine
  name: harness_fortran_cpu_mpi__write_metrics_basis
  args:
  - name: entries
    rank: 1
    intent: in
    spec:
      type: derived
      name: harness_fortran_cpu_mpi__h_mb_entry
  - name: n
    intent: in
    spec:
      type: integer
- kind: subroutine
  name: harness_fortran_cpu_mpi__write_diagnostics
  args:
  - name: results
    rank: 1
    intent: in
    spec:
      type: derived
      name: harness_fortran_cpu_mpi__h_case_result
  - name: n
    intent: in
    spec:
      type: integer
- kind: subroutine
  name: harness_fortran_cpu_mpi__write_perf
  args:
  - name: case_id
    intent: in
    spec:
      type: string
      len: assumed
  - name: target
    intent: in
    spec:
      type: string
      len: assumed
  - name: steps
    intent: in
    spec:
      type: integer
  - name: cells_updated
    intent: in
    spec:
      type: integer
  - name: walltime_sec
    intent: in
    spec:
      type: real
      kind: dp
  - name: mpi_ranks
    intent: in
    spec:
      type: integer
  - name: threads_per_rank
    intent: in
    spec:
      type: integer
  - name: gpu_devices
    intent: in
    spec:
      type: integer
- kind: subroutine
  name: harness_fortran_cpu_mpi__init
- kind: subroutine
  name: harness_fortran_cpu_mpi__finalize
- kind: function
  name: harness_fortran_cpu_mpi__comm_rank
  result:
    name: r
    spec:
      type: integer
- kind: function
  name: harness_fortran_cpu_mpi__comm_size
  result:
    name: n
    spec:
      type: integer
- kind: subroutine
  name: harness_fortran_cpu_mpi__partition
  args:
  - name: n_global
    intent: in
    spec:
      type: integer
  - name: glo
    intent: out
    spec:
      type: integer
  - name: ghi
    intent: out
    spec:
      type: integer
- kind: subroutine
  name: harness_fortran_cpu_mpi__exchange_halo_r1
  args:
  - name: a
    rank: 1
    intent: inout
    spec:
      type: real
      kind: dp
  - name: ng
    intent: in
    spec:
      type: integer
  - name: periodic
    intent: in
    spec:
      type: logical
- kind: subroutine
  name: harness_fortran_cpu_mpi__exchange_halo_r2
  args:
  - name: a
    rank: 2
    intent: inout
    spec:
      type: real
      kind: dp
  - name: axis
    intent: in
    spec:
      type: integer
  - name: ng
    intent: in
    spec:
      type: integer
  - name: periodic
    intent: in
    spec:
      type: logical
- kind: subroutine
  name: harness_fortran_cpu_mpi__gather_r1
  args:
  - name: a
    rank: 1
    intent: in
    spec:
      type: real
      kind: dp
  - name: lo
    intent: in
    spec:
      type: integer
  - name: hi
    intent: in
    spec:
      type: integer
  - name: glo
    intent: in
    spec:
      type: integer
  - name: g
    rank: 1
    intent: out
    spec:
      type: real
      kind: dp
      alloc: true
- kind: subroutine
  name: harness_fortran_cpu_mpi__gather_r2
  args:
  - name: a
    rank: 2
    intent: in
    spec:
      type: real
      kind: dp
  - name: axis
    intent: in
    spec:
      type: integer
  - name: lo
    intent: in
    spec:
      type: integer
  - name: hi
    intent: in
    spec:
      type: integer
  - name: glo
    intent: in
    spec:
      type: integer
  - name: g
    rank: 2
    intent: out
    spec:
      type: real
      kind: dp
      alloc: true
- kind: subroutine
  name: harness_fortran_cpu_mpi__gather_r3
  args:
  - name: a
    rank: 3
    intent: in
    spec:
      type: real
      kind: dp
  - name: axis
    intent: in
    spec:
      type: integer
  - name: lo
    intent: in
    spec:
      type: integer
  - name: hi
    intent: in
    spec:
      type: integer
  - name: glo
    intent: in
    spec:
      type: integer
  - name: g
    rank: 3
    intent: out
    spec:
      type: real
      kind: dp
      alloc: true
- kind: subroutine
  name: harness_fortran_cpu_mpi__gather_r4
  args:
  - name: a
    rank: 4
    intent: in
    spec:
      type: real
      kind: dp
  - name: axis
    intent: in
    spec:
      type: integer
  - name: lo
    intent: in
    spec:
      type: integer
  - name: hi
    intent: in
    spec:
      type: integer
  - name: glo
    intent: in
    spec:
      type: integer
  - name: g
    rank: 4
    intent: out
    spec:
      type: real
      kind: dp
      alloc: true
- kind: function
  name: harness_fortran_cpu_mpi__reduce_sum
  args:
  - name: x
    intent: in
    spec:
      type: real
      kind: dp
  result:
    name: s
    spec:
      type: real
      kind: dp
- kind: function
  name: harness_fortran_cpu_mpi__reduce_max
  args:
  - name: x
    intent: in
    spec:
      type: real
      kind: dp
  result:
    name: s
    spec:
      type: real
      kind: dp
- kind: function
  name: harness_fortran_cpu_mpi__reduce_min
  args:
  - name: x
    intent: in
    spec:
      type: real
      kind: dp
  result:
    name: s
    spec:
      type: real
      kind: dp
- kind: function
  name: harness_fortran_cpu_mpi__reduce_sum_int
  args:
  - name: i
    intent: in
    spec:
      type: integer
  result:
    name: s
    spec:
      type: integer
```

## 6. Prohibitions
- No physics: the harness must embed no per-case kernel or per-test judgment logic; those are the consuming physics node's `case_run` / `checks_compute` callbacks. `__write_diagnostics` folds caller-supplied statuses and each case's `expected_xfail`; it never decides pass/fail itself.
- Every writer that receives a record array (`__write_snapshot`, `__write_metrics_basis`, `__write_diagnostics`) emits that array's part of its output by ITERATING the records: a body that does not iterate them is forbidden, whatever it emits. (`__write_perf` receives only scalars and iterates nothing.) In particular a `metrics` body that is a literal (`{}` or any fixed key set), or that is selected by branching on `case_id`, is forbidden even when its output happens to match this node's own self-test, because a consuming physics node supplies arbitrary records the same body would drop. Only a length-0 supplied array may yield `{}`, and only as the outcome of the iteration. This rule governs the record-derived keys; the envelope keys the contract fixes — the snapshot's declared `time_variable` key, `perf.json`'s field set, and the `test_id` / `case_id` keys of a metrics-basis entry — stay as specified in §2.
- No truncating or language-specific serialization anywhere in the generated source: never a numeric form that may drop a leading digit, never a language-specific boolean token in place of the literal `true`/`false` (branch to the literal instead). The forbidden Fortran realizations (`F0` / `F0.d` numeric, an `L`-family logical descriptor) are enumerated in `docs/workflow/RUNNER_OUTPUT_CONTRACT.md §4`.
- Never write `verdict.json`, `aggregate_verdict.json`, `summary.json`, or `trial_meta.json` — not even as a literal filename inside a comment or example string.
- No launch of an external interpreter (`python` / `bash` / `sh` / `node`).
- All output paths are written relatively (so `cd $(RUNDIR)` redirects them); no hardcoded / sequential snapshot filename literal.
- The message-passing library is used by the model module `harness_fortran_cpu_mpi_model` alone, through its Fortran 2008 binding. The self-test runner calls none of the library's procedures and imports none of its modules: it reaches the runtime through §3.3 only, as a consuming physics node's runner does.
- No file is written by a rank other than rank 0, and no writer of §3.2 is called by one: the output is serialized by the caller's rank-0 guard alone (§1).
- No operation of §3.2 calls the message-passing library, and none of §3.3 writes a file.

## 7. Traceability
Record the harness adoption in `component_catalog.yaml` (as the `(language, hardware, parallel)` = `(fortran, cpu, mpi)` runner harness) and the resolved harness version each dependent physics node was certified against.

## 8. tests reference
The corresponding `tests.md` is `spec/infrastructure/infra/harness/harness_fortran_cpu_mpi/tests.md`, with `test_profile_version` of `0.1.0`.
