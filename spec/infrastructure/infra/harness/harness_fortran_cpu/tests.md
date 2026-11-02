# Tests: Fortran/CPU runner harness (L0 plumbing self-test)

## 0. Meta information
- `test_profile_id`: `harness_fortran_cpu_l0`
- `test_profile_version`: `0.11.0`
- `status`: `draft`
- `spec_ref.spec_kind`: `infrastructure`
- `spec_ref.spec_id`: `harness_fortran_cpu`
- `spec_ref.spec_version`: `0.9.0`
- `spec_ref.controlled_spec_path`: `spec/infrastructure/infra/harness/harness_fortran_cpu/controlled_spec.md`

## 1. Test purpose
This suite verifies the published runner-plumbing operations of `harness_fortran_cpu` at `L0`: numeric / boolean / rank-1..4 array JSON emission round-trips, the literal tokens a non-finite real is emitted as, the case-set fan-out (one `<case_id>.json` snapshot per case), the derived `perf` throughput, and the `--cases` input guard. The self-test runner exercises each operation and writes what the host judges into that case's snapshot; because the writers use the emitters, a faithful emitter is a precondition for a correct output.

## 2. Input-defaulting rules
- Each normal case supplies a small fixed set of sentinel values to the emitter under test. **The sentinel values are DECLARED in the IR as that case's inputs** (`case.test_case_set[].inputs.initial`), and the self-test emits exactly the declared values, so the host can compare what the emitter wrote against what the case declares (§6, the host-evaluated `predicate` of each test):
  - `l0_numeric_roundtrip_pass`: `inputs.initial.x_in = [-1.5, 1.0e-30, 1.0e+30]` (a negative, a `1e-30`-scale and a `1e+30`-scale real).
  - `l0_array_emit_pass`: `inputs.initial.a1 = [1, 2]`, `a2 = [[1, 2], [3, 4]]`, `a3 = [[[1, 2], [3, 4]], [[5, 6], [7, 8]]]`, `a4` the same pattern over `1..16`, each written in the JSON nesting order the emitters produce (the leading array index outermost), and emitted as declared.
  - `l0_boolean_literal_pass`: `inputs.initial.bool_in_true = true`, `bool_in_false = false`.
- The non-finite case (`l0_nonfinite_token_pass`) declares no sentinel input: the IR carries no non-finite number, so the four values it emits — a quiet NaN with the sign bit clear, a quiet NaN with the sign bit set, `+inf` and `-inf` — are produced at run time from the language's IEEE facility, never from a literal division a compiler may refuse to fold. It carries the same `grid` / `time` / `boundary` inputs as every other case; the plumbing aspect it verifies, `harness_fortran_cpu__emit_real` on a non-finite value, is selected by its `case_id`.
- The abnormal case (`l0_missing_cases_xfail`) synthesizes an empty argv-token list (no `--cases`) and drives `harness_fortran_cpu__parse_cases` to exercise the guard.

## 3. Execution-control rules
`N/A`: the harness self-test defines no time-stepping. Each case runs its named plumbing check once (`steps = 1`).

## 4. Case-expansion rules
The suite defines seven `case`s and seven `test_id`s (no sweep). The self-test's own runner writes each case's snapshot once, after the case ran; it writes no `raw/state_snapshots/initial/<case_id>.json` (that capture is the host-rendered runner's), so every host-evaluated `predicate` of §6 reads `final.<var>` and the case's declared `inputs` only. Every test is **single-target**: it names exactly one case, and that case's `case_id` equals the `test_id` — the seven `case.test_case_set[].case_id` are exactly those seven `test_id`s. Each `case_id` selects the plumbing aspect that case verifies. The multi-case run itself is the fan-out evidence: each case writes exactly one `raw/state_snapshots/<case_id>.json`.

**Every snapshot holds every variable `controlled_spec.md` §2 declares**, plus the scalar time variable `t` (value `0.0`; a single `steps=1` step). A variable §2 lists under the case holds what the case computed; any other variable holds a shape-valid placeholder, `0.0` or an array of zeros of its declared shape, which no predicate of that case reads; a test's own predicates fail on a placeholder, so a case that does not fill its variables fails its test.

## 5. Judged quantities
Each test of §6 judges one quantity, named by its `quantities` line and defined by its `predicate` lines: an expression over the case's `final` capture and the inputs the case declares. The host evaluates them, and they alone decide the test (`docs/TESTS.md` requirement 9); the self-test writes no verdict of its own. Every snapshot variable is written by this node's self-test, which is the trust root for it (A6), and is `source: operation` by default. This suite has no `reported` and no `reference` variable, so no test carries a `reported` line.

## 6. Test definitions
- `test_id`: `l0_numeric_roundtrip_pass`
  - `level`: `L0`
  - `operation_id`: `harness_fortran_cpu__emit_real`
  - `expected_outcome`: `pass`
  - `judgment`: the sentinel reals `x_in` (the three values §2 declares) emitted via `harness_fortran_cpu__emit_real` and re-parsed into `x_out` reproduce the inputs within an absolute tolerance of `1e-12` (`max|x_out - x_in| <= 1e-12`).
  - `quantities`: `numeric_roundtrip`
  - `predicate` (host-evaluated, quantity `numeric_roundtrip`, `per_case: true`): `maxabs(final.x_out - inputs.initial.x_in) <= 1.0e-12` — the re-parsed values, as the runner emitted them into the snapshot, against the declared sentinel.
- `test_id`: `l0_boolean_literal_pass`
  - `level`: `L0`
  - `operation_id`: `harness_fortran_cpu__emit_bool`
  - `expected_outcome`: `pass`
  - `judgment`: a `true` and a `false` boolean emit the exact JSON literals `true` / `false` (no language-specific boolean token), so `bool_match == 1.0`.
  - `quantities`: `boolean_literal`
  - `predicate` (quantity `boolean_literal`, `per_case: true`): `final.bool_match == 1.0` (op `eq`).
- `test_id`: `l0_array_emit_pass`
  - `level`: `L0`
  - `operation_id`: `harness_fortran_cpu__emit_array_r2` (representative; the case exercises `__emit_array_r1..r4`)
  - `expected_outcome`: `pass`
  - `judgment`: the rank-1..4 real arrays `a1..a4` emitted via `harness_fortran_cpu__emit_array_r1..r4` produce well-formed nested JSON arrays whose parsed shape and element values match the inputs within `1e-12`.
  - `quantities`: `array_emit`
  - `predicate` (quantity `array_emit`, `per_case: true`): `max(maxabs(final.a1 - inputs.initial.a1), maxabs(final.a2 - inputs.initial.a2), maxabs(final.a3 - inputs.initial.a3), maxabs(final.a4 - inputs.initial.a4)) <= 1.0e-12` — the emitted arrays, re-parsed by the host from the snapshot file, against the declared sentinels (shape and values: a wrong nesting order or extent is a shape-pairing error, which fails the predicate).
- `test_id`: `l0_case_fanout_pass`
  - `level`: `L0`
  - `operation_id`: `harness_fortran_cpu__write_snapshot`
  - `expected_outcome`: `pass`
  - `judgment`: the case loop writes exactly one `raw/state_snapshots/<case_id>.json` per case (runtime-built name), each carrying every declared variable; `case_index` is this case's 1-based position in the case loop, so `1 <= case_index <= 7`.
  - `quantities`: `case_fanout`
  - `predicate` (quantity `case_fanout`, `per_case: true`, two entries): `final.case_index >= 1.0` and `final.case_index <= 7.0`.
- `test_id`: `l0_perf_derived_pass`
  - `level`: `L0`
  - `operation_id`: `harness_fortran_cpu__write_perf`
  - `expected_outcome`: `pass`
  - `judgment`: `throughput_cells_per_sec == cells_updated / walltime_sec` within a relative tolerance of `1e-9`: `|throughput_cells_per_sec - cells_updated / walltime_sec| / throughput_cells_per_sec <= 1e-9` (the RELATIVE residual). The `steps`, `cells_updated` and `walltime_sec` the self-test passes to `__write_perf` are the run totals `controlled_spec.md` §3 fixes for it (not the per-case `steps = 1` of §3 of this document), all positive, so the throughput the residual divides by is positive and the residual is defined. The parallelism it passes is `mpi_ranks = 1` and `gpu_devices = 0`; `harness_fortran_cpu__write_perf` takes no thread count (`controlled_spec.md` §3.2). The writer itself writes as `parallelism.threads_per_rank`, and uses in `parallel_degree_total`, the count the run's shared-memory parallel runtime gives the program, which it obtains when it is called: the maximum team size that runtime reports, never a constant and never read from an environment variable. In Fortran with OpenMP it imports `omp_get_max_threads` from the intrinsic module `omp_lib` and calls it, both on `!$` conditional-compilation lines, after an unconditional assignment of `1` to the count, so a build without OpenMP enabled reads both lines as comments and writes `1` (`docs/backends/parallel/openmp/GENERATE_RULES.md` §5). The Validate phase runs the self-test at several thread counts and compares `perf.json#parallelism.threads_per_rank` with the count each run was launched with, so a writer that records a constant fails on the first run whose count differs, and every consuming node's runner records its count through the same writer.
  - `quantities`: `perf_derived`
  - `predicate` (quantity `perf_derived`, `per_case: true`): `abs(final.throughput_cells_per_sec - final.cells_updated / final.walltime_sec) / final.throughput_cells_per_sec <= 1.0e-9` — evaluated by the host from the three fields the case re-parsed from `perf.json`; a placeholder `0.0` there divides by zero and fails the test.
- `test_id`: `l0_nonfinite_token_pass`
  - `level`: `L0`
  - `operation_id`: `harness_fortran_cpu__emit_real`
  - `expected_outcome`: `pass`
  - `judgment`: the four non-finite reals of §2 — a quiet NaN with the sign bit clear, a quiet NaN with the sign bit set, `+inf` and `-inf`, in that order — emitted via `harness_fortran_cpu__emit_real` produce exactly the tokens `NaN`, `NaN`, `Infinity`, `-Infinity` (the literals `docs/workflow/RUNNER_OUTPUT_CONTRACT.md` §4 names; never `null`, never a numeric format's own spelling, and the emitter does not stop the program), so `nonfinite_match == 1.0`. The four strings are compared in memory and no file carries them, because a non-finite value in a snapshot is a `structural_violation`; this node's own runner is the trust root for `nonfinite_match` (A6), and the host's reading of the tokens is outside this test.
  - `quantities`: `nonfinite_token`
  - `predicate` (quantity `nonfinite_token`, `per_case: true`): `final.nonfinite_match == 1.0` (op `eq`).
- `test_id`: `l0_missing_cases_xfail`
  - `level`: `L0`
  - `operation_id`: `harness_fortran_cpu__parse_cases`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: a `--cases` flag is absent from the token list passed to `__parse_cases`
  - `judgment`: calling `__parse_cases` on a length-0 token array returns `ok = false`, so `guard_fired == 1.0`.
  - `quantities`: `input_guard`
  - `predicate` (quantity `input_guard`, `per_case: true`): `final.guard_fired == 1.0` (op `eq`) — the state fact the guard leaves behind, in the failure direction; the test certifies `xfail` when it holds.

## 7. Pass/fail aggregation rules
- `per_test.pass_rule`: `pass` when every predicate of the test holds.
- `per_test.xfail_rule`: `xfail` when `xfail_condition` is true and every predicate of the test holds (the input guard fires on a missing `--cases`).
- `suite.pass_rule`: `pass` when all `test_id` are `pass` or `xfail`.

## 8. Traceability
- Record `test_profile_id` and `test_profile_version` in `trial_meta.json`.
