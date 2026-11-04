# Controlled Spec: Fortran/CPU runner harness (infrastructure spec)

## 0. Meta information
- `spec_id`: `harness_fortran_cpu`
- `spec_version`: `0.10.0`
- `status`: `controlled_draft`
- `spec_kind`: `infrastructure`
- `domain`: `infra`
- `family`: `harness`

## 1. Responsibility and scope
This `infrastructure` node (R1 harness) is responsible for the shared **runner plumbing** that every Fortran/CPU physics node's runner is built against: argv / `--cases` parsing, the case-set loop driver, the reading of the host-written case file (§3.2), the JSON emission machinery (numeric / integer / boolean / rank-1..4 real-array tokens), and the standard runner-output writers (`raw/state_snapshots/<case_id>.json`, `perf.json`) — the last of which records the **thread count** the run's shared-memory parallel runtime gives the program, which its writer obtains at run time (§3.2). It carries **no physics**: the kernel belongs to the consuming physics node, never here, and the host judges each test from the captures. It targets `(language=fortran, hardware=cpu)`; a different `(language, hardware)` target is a separate harness node.

The node's own generated code is a `harness_fortran_cpu_model.f90` publishing the plumbing operations plus a self-test `harness_fortran_cpu_runner.f90` that exercises them and emits the standard runner outputs (so the harness is verified through the exact same Compile→Generate→Build→Validate path as any node; it is self-hosting — the self-test writes its evidence using its own emitters).

The published surface is a **binding, signature-level contract**: §5.1 gives the canonical language-neutral structured interface block (every public type and every operation signature) in a machine-readable fenced code block, from which the language backend renders the Fortran surface. The generated `harness_fortran_cpu_model.f90` must publish exactly those signatures; a consuming physics node's host-rendered runner glue is written against them and holds no serialization knowledge of its own (the JSON envelope assembly lives only inside these certified operations).

## 2. input/output contract
Input (to the self-test runner): the standard runner argv `--cases <spec.ir.yaml> <case_id>...` — the spec path is taken positionally, `__parse_cases` keeps it, and the spec file itself need not be read; the trailing tokens are the `case_id`s to run, one per `case.test_case_set[]`. The main program marshals the process argv into a token array and calls `harness_fortran_cpu__parse_cases` on it. Each `case_id` selects, by dispatching on the `case_id`, the plumbing aspect that case verifies. Every `case_id` is also the `test_id` of the single-target test that names it, and every test is single-target. A missing `--cases` flag (no cases) makes `__parse_cases` return `ok = false` (the input guard); the guard case verifies this by calling `__parse_cases` on a length-0 token array.

Input (to `__read_case`): the **case file** `case_inputs/<case_id>.json`, which the host writes beside `<spec.ir.yaml>` for every case before the program runs (`docs/workflow/RUNNER_OUTPUT_CONTRACT.md` §6), one flat JSON object on one line of any length. Its keys are names (`inputs.initial.x_in`, `derived.dt`, a state variable's name) and hold no `\` escape; each value is a number or a rectangular nested array of numbers of depth 1 to 4, an empty array admitted at any level. A number is an RFC 8259 token, so `3`, `-1.5` and `1e-30` are all numbers, and it is not the emitters' spelling. JSON whitespace between tokens is admitted. A document holding anything else — a nested object, a string, `true` / `false` / `null`, a `NaN` / `Infinity` / `-Infinity` token, a ragged array, an array deeper than 4, a number that does not convert to a finite binary64 value — is unreadable.

Output artifacts (produced by the writers, into the run node dir relative to `cwd=RUNDIR`):
- **`perf.json`** — one object with `case_id`, `target` (`"cpu"`), `walltime_sec`, `steps`, `cells_updated`, `throughput_cells_per_sec` (`= cells_updated / walltime_sec`), a `parallelism` object (`mpi_ranks`, `threads_per_rank`, `gpu_devices`, `parallel_degree_total = mpi_ranks*threads_per_rank*max(gpu_devices,1)`), and `timestamp_utc` (ISO-8601). `threads_per_rank` is the number of threads the run was given, which `__write_perf` obtains from the run's shared-memory parallel runtime when it is called (§3.2) — never a constant and never a value its caller passes: the Validate phase runs the same binary at several thread counts and compares it with the count each run was launched with.
- **`raw/state_snapshots/<case_id>.json`** — exactly one per case, named at runtime as `'raw/state_snapshots/'//trim(case_id)//'.json'` (never a hardcoded/sequential literal). Each holds every variable declared in `snapshot_schema.json` plus the declared scalar `time_variable` `t` (value `0.0`; the self-test has a single `steps=1` step). The snapshot state variables, with their `shape_expr`, are listed under the case that computes them:
  - `l0_numeric_roundtrip_pass`: `x_out` (rank-1, `[3]` — the values re-parsed from `__emit_real`'s tokens).
  - `l0_boolean_literal_pass`: `bool_match` (scalar, `1.0` iff a `true` and a `false` boolean emitted the exact literals `true`/`false`).
  - `l0_array_emit_pass`: `a1` (rank-1, `[2]`), `a2` (rank-2, `[2,2]`), `a3` (rank-3, `[2,2,2]`), `a4` (rank-4, `[2,2,2,2]`) — the arrays `__emit_array_r1..r4` emitted, re-parsed.
  - `l0_case_fanout_pass`: `case_index` (scalar — this case's ordinal in the run).
  - `l0_perf_derived_pass`: `throughput_cells_per_sec`, `cells_updated`, `walltime_sec` (scalars — the three fields as the case re-parsed them from the `perf.json` the writer wrote), from which the host evaluates the RELATIVE residual `tests.md` judges.
  - `l0_nonfinite_token_pass`: `nonfinite_match` (scalar, `1.0` iff the four non-finite reals of §3 emitted through `__emit_real` produced exactly the literals `NaN`, `NaN`, `Infinity`, `-Infinity`).
  - `l0_read_case_roundtrip_pass`: `rc_real`, `rc_whole`, `rc_int`, `rc_true`, `rc_false`, `rc_empty_read`, `rc_e2`, `rc_e3`, `rc_e4` (scalars) and `rc_r1` (`[3]`), `rc_r2` (`[2,3]`), `rc_r3` (`[2,2,3]`), `rc_r4` (`[2,2,2,3]`) — what the getters returned for the case's own case file, written back through the emitters (§3): `rc_true` / `rc_false` are `1.0` iff `__get_bool` returned true / false for them, `rc_empty_read` is `1.0` iff the array `__get_array_r1` returned for the empty sentinel has size 0, and `rc_e2` / `rc_e3` / `rc_e4` are one element of `rc_r2` / `rc_r3` / `rc_r4` obtained by indexing the array the getter returned: every index at its first value but the last, which is at its second.
  - `l0_read_case_second_pass`: `rc_real` and `rc_r1` of the same shapes, read from that case's own case file.
  - `l0_missing_case_file_xfail`: `read_refused` (scalar, `1.0` when `__read_case` returned `ok = false` for a `case_id` no case file exists for).
  - `l0_missing_cases_xfail`: `guard_fired` (scalar, `1.0` when `__parse_cases` on a length-0 token array returned `ok = false`).
  A variable is declared once in `snapshot_schema.json` and written by every case: in a case it is not listed under it holds a shape-valid placeholder, `0.0` or an array of zeros of its declared shape, which no predicate of that case reads.

Numeric serialization follows the abstract runner-output contract: a finite real as a round-trip-lossless exponential token, a non-finite real as the literal token `NaN` (whatever the sign bit), `Infinity` or `-Infinity` chosen by branching on the value (never handed to a numeric format, never written as `null`, and never a reason for the emitter to stop the program), an integer as its minimal decimal, a boolean as the literal token `true`/`false` — a truncating form (one that may drop a leading digit) and any language-specific boolean token are forbidden. The target-language realization (for Fortran: reals via `ES24.16E3` then `trim(adjustl())`, or a bounded `Fw.d`, never `F0`/`F0.d`; integers via `I0`; booleans by branching to the literal, never an `L`-family descriptor; non-finite reals by branching on `ieee_is_nan` / `ieee_is_finite` from the intrinsic module `ieee_arithmetic`, imported with an `only:` list) is canonical in `docs/backends/language/fortran/RUNNER_OUTPUT.md` §1, the Fortran binding of `docs/workflow/RUNNER_OUTPUT_CONTRACT.md §4`.

## 3. Operation definition
The published operations (all under module `harness_fortran_cpu_model`, prefix `harness_fortran_cpu__`) are the plumbing surface a physics-node runner reuses. The canonical machine-readable interface (every signature and public type, verbatim) is §5.1; the prose below states each operation's contract. The module declares two module-level integer parameters the signatures reference: `dp` (the double-precision real kind token; §5.1 value `float64` = IEEE-754 binary64) and `case_id_len = 64` (the fixed storage width of a parsed case id, known to the caller, because an assumed-length `intent(out)` string dummy is disallowed). Their accessibility is not part of this contract — §5.1 pins their VALUES, not whether the module exports them: the module may export them or keep them private, and a consuming runner imports them where the module exports them or declares its own matching values (a double-precision-real kind for the actuals it passes and a fixed-width (`case_id_len`) case-id buffer). Each of these shapes is correct. Their VALUES are pinned (the gate rejects a drifted `case_id_len`), because a consumer's hardcoded length must match. The Fortran binding of these tokens (`dp = real64`, `character(len=64)`) is produced by the language backend, not authored here.

### 3.1 Published derived types
The module publishes one derived type (named by its fully-qualified `harness_fortran_cpu__<name>`):
- `harness_fortran_cpu__h_named` `{ name: string (dynamic length), json: string (dynamic length) }` — a **boxed named value**: a JSON key `name` and its already-serialized JSON value `json`. It lets heterogeneous-rank / ragged snapshot variables travel through one homogeneous rank-1 dynamic array of `harness_fortran_cpu__h_named` (a homogeneous array argument cannot carry differing ranks — this box, holding the pre-serialized token, is the workaround). The caller `__box`es a value into it before serialization is complete. (Types and lengths use the §5.1 neutral vocabulary; the Fortran binding is the language backend's.)

### 3.2 Published operations
- `harness_fortran_cpu__parse_cases(tokens, ntokens, case_ids, ncases, ok)` — parse a supplied argv **token list** (NOT the process argv directly, so the guard is exercisable with a synthetic list): find `--cases` in `tokens`, keep the following token as the spec path (the positional token, which `__read_case` finds the case files beside and which the call replaces only when it returns `ok = true`), collect the remaining tokens as `case_ids` (each stored in a fixed-width `case_id_len` slot); return `ok = false` when `--cases` is absent from `tokens` or no `case_id` follows it (the input guard), else `ok = true`. The self-test's main program marshals the real process argv (via the language-standard argv accessors) into `tokens` for the normal cases, and passes a length-0 `tokens` for the guard case.
- `harness_fortran_cpu__emit_real(x) result(s)` — format a `real (kind dp)` scalar as a JSON numeric token: a finite value in round-trip-lossless exponential form, a non-finite value as the literal `NaN` / `Infinity` / `-Infinity` §2 names.
- `harness_fortran_cpu__emit_int(i) result(s)` — format an integer as a JSON numeric token (minimal decimal; §2).
- `harness_fortran_cpu__emit_bool(b) result(s)` — format a `boolean` as the JSON literal `true`/`false`.
- `harness_fortran_cpu__emit_array_r1(a) result(s)` … `__emit_array_r4(a) result(s)` — format an assumed-shape rank-1..4 `real (kind dp)` array as a nested JSON array (`[ ... ]`, row-major over the leading index), reusing `__emit_real` per element.
- `harness_fortran_cpu__box(name, json) result(nv)` — pack a JSON key `name` and an already-serialized JSON value `json` into a `harness_fortran_cpu__h_named`. A consuming runner emits each of a case's snapshot variables (via the matching `__emit_*`), boxes each with `__box`, and hands the resulting `values` array to `__write_snapshot` in one call; in a physics node the host-rendered glue emits this `__box` list mechanically from `snapshot_schema.json` (the harness holds no snapshot registry, and does no serialization of the boxed value beyond copying the caller's token; the only state its operations of this section keep is the spec path and the loaded case of `__read_case`).
- `harness_fortran_cpu__write_snapshot(case_id, values, time)` — write the per-case `raw/state_snapshots/<case_id>.json` (runtime-built filename) holding the boxed state variables in `values` (a rank-1 array of `harness_fortran_cpu__h_named`) plus the scalar time variable. The emitted object is **flat**: each boxed variable is written as a **top-level key of the snapshot object**, keyed by its `name` with its already-serialized `json` as the value, sibling to the time variable's key. `values` is the dummy-argument name of the boxed array; neither it nor any other wrapper key appears in the emitted JSON.
- `harness_fortran_cpu__write_perf(case_id, target, steps, cells_updated, walltime_sec, mpi_ranks, gpu_devices)` — write `perf.json` with all required fields incl. the derived `throughput_cells_per_sec` and the `parallelism` object (`parallel_degree_total = mpi_ranks*threads_per_rank*max(gpu_devices,1)`). It writes `mpi_ranks` and `gpu_devices` exactly as its caller passes them. It takes no thread count: it writes as `threads_per_rank` the number of threads the run's shared-memory parallel runtime gives the program, and uses that number in `parallel_degree_total`. **The thread count.** The HOST chooses how many threads the program runs with, and varies it between runs of the same binary (the Validate phase runs the binary once per execution shape of the target and compares each run's `perf.json` with the count it was launched with): it sets the count in the run's launch environment for the target's shared-memory parallel runtime, and the program learns it from that runtime alone. `__write_perf` obtains it AT RUN TIME on every call, as the maximum team size that runtime reports — never a constant and never read from an environment variable by this node. The query is reached through conditional compilation, so that a build without the runtime enabled compiles it away and the count written is `1` (for Fortran with OpenMP: `omp_get_max_threads()` from the intrinsic module `omp_lib`, both the import and the call written on `!$` conditional-compilation lines, after an unconditional assignment of `1`; `docs/backends/parallel/openmp/GENERATE_RULES.md` §5). It starts no parallel region. Because the count is obtained inside this writer, the self-test and the host-rendered runner of every consuming node record it through the same code, and this node's own Validate run, which runs the self-test at several thread counts, is what checks it.
- `harness_fortran_cpu__read_case(case_id, ok)` — read the case file of `case_id`: `<dir>/case_inputs/<case_id>.json`, where `<dir>` is the directory part of the spec path the last successful `__parse_cases` kept (a spec path with no directory part: the current directory). `ok = true` when the file exists and is readable under §2: the case it holds replaces the one loaded before. Otherwise `ok = false` and NO case is loaded, so a value of the previous case cannot be read after a refusal; the same holds when no `__parse_cases` has succeeded. This operation does not stop the program.
- `harness_fortran_cpu__get_real(name) result(x)` — the value of `name` in the loaded case: any number token, the nearest binary64 value.
- `harness_fortran_cpu__get_int(name) result(i)` — the value of `name`, an integer token (no fraction and no exponent) that fits the default integer.
- `harness_fortran_cpu__get_bool(name) result(b)` — the value of `name`, exactly the token `1` (true) or `0` (false): the host writes a boolean input that way.
- `harness_fortran_cpu__get_array_r1(name, a)` … `__get_array_r4(name, a)` — allocate `a` to the extents of `name`'s array and fill it, the inverse of `__emit_array_r1..r4`: the leading array index is the outermost JSON level. An empty array at a level gives extent 0 there and at every level below it, so `[]` reads at every rank and `[[],[]]` at rank 2 and above; any other value whose nesting depth differs from the rank is a mismatch. Number tokens read as in `__get_real`.
- Each getter holds no default and takes no `ok`: with no case loaded, a name the case does not hold, a scalar asked as an array or an array as a scalar, or a token its type does not admit, it stops the program with a non-zero exit status (`error stop`).

The self-test `harness_fortran_cpu_runner.f90` calls `__parse_cases`, then for each `case_id` runs (dispatching on the `case_id`) the plumbing check that case names — verifying the emitter round-trips, the case fan-out, and the input guard — and boxes that case's snapshot variables. The `l0_nonfinite_token_pass` case obtains four non-finite reals AT RUN TIME from the language's IEEE facility (`ieee_value` with `ieee_quiet_nan` / `ieee_positive_inf` / `ieee_negative_inf`, and `ieee_copy_sign` for the sign bit) — a quiet NaN with the sign bit clear, a quiet NaN with the sign bit set, `+inf` and `-inf` — never from a literal division a compiler may refuse to fold; it emits each through `__emit_real`, compares the four strings with `NaN`, `NaN`, `Infinity`, `-Infinity`, and emits `nonfinite_match` (`1.0` when all four match, else `0.0`) into its snapshot. It reads its clock before the first case and again after the last, and then calls `__write_perf` once, with that elapsed time as `walltime_sec`, or `1.0e-9` when the elapsed time it reads is not positive (a clock tick longer than the interval), with `mpi_ranks = 1` and `gpu_devices = 0` (the thread count is the writer's own, above), and with `steps` and `cells_updated` both equal to the number of cases its `__parse_cases` call on the program's argv returned: each case runs its one plumbing check as one step (`steps = 1`, `tests.md` §3) that updates one cell, so each value is the sum of the cases' steps, independent of any case's `grid` member and positive whenever a case ran. With both quantities positive, the relative residual of the `tests.md` §6 `l0_perf_derived_pass` judgment is defined. Everything after that call is outside the measured interval. Only then does it fill the `l0_perf_derived_pass` variables, re-parsing the `perf.json` that call wrote. `__write_snapshot` is called once per case, inside the loop or after the `__write_perf` call — the `l0_perf_derived_pass` snapshot only after those variables are filled. Because the writers use the emitters, a correct emitter is necessary for a correct output; the snapshots are the evidence the host judges that plumbing by.

The `l0_read_case_roundtrip_pass` case calls `__read_case` with its own `case_id`, reads every sentinel of `tests.md` §2 through the getter of its type under its key `inputs.initial.<name>`, and emits what the getter returned into its snapshot; it holds no copy of a sentinel, and the program stops as §3.2 states when a getter refuses. The `l0_read_case_second_pass` case does the same for the two sentinels it declares, so a reader that kept an earlier case fails one of the two. The `l0_missing_case_file_xfail` case calls `__read_case` with the literal id `l0_no_such_case_file`, which names no case of the IR, so no case file exists for it, and emits `read_refused` (`1.0` when `ok = false`, else `0.0`). No other case calls `__read_case`.

## 4. Failure conditions and constraints
A missing `--cases` flag (or no `case_id` after it) is a hard input error — `__parse_cases` returns `ok = false`, and the self-test's `l0_missing_cases_xfail` case exercises this guard by calling `__parse_cases` on a synthesized empty token list and confirming `ok = false` (recorded as `guard_fired = 1.0`). A JSON emitter whose re-parsed token does not reproduce its input within an absolute tolerance of `1e-12` fails the corresponding test's predicate.

A case file that is absent or unreadable under §2 makes `__read_case` return `ok = false`, which the self-test's `l0_missing_case_file_xfail` case exercises. A getter's refusal (§3.2) stops the program with a non-zero exit status (`error stop`), so that no evidence is written from a value the case does not hold.

## 5. Public API and compatibility
The published `operation_id`s are exactly: `harness_fortran_cpu__parse_cases`, `harness_fortran_cpu__emit_real`, `harness_fortran_cpu__emit_int`, `harness_fortran_cpu__emit_bool`, `harness_fortran_cpu__emit_array_r1`, `harness_fortran_cpu__emit_array_r2`, `harness_fortran_cpu__emit_array_r3`, `harness_fortran_cpu__emit_array_r4`, `harness_fortran_cpu__box`, `harness_fortran_cpu__write_snapshot`, `harness_fortran_cpu__write_perf`, `harness_fortran_cpu__read_case`, `harness_fortran_cpu__get_real`, `harness_fortran_cpu__get_int`, `harness_fortran_cpu__get_bool`, `harness_fortran_cpu__get_array_r1`, `harness_fortran_cpu__get_array_r2`, `harness_fortran_cpu__get_array_r3`, `harness_fortran_cpu__get_array_r4`. The module also publishes the derived type `harness_fortran_cpu__h_named`.

A change breaking compatibility of any signature (or of a published derived type's component layout) is a **breaking change released under a new `spec_version`**, not a rename: `0.2.1` → `0.3.0` added the `case_id` component to `h_mb_entry`. A change to how the published surface is CARRIED — the §5.1 / `IR public_api.signatures` REPRESENTATION — is likewise released under a new `spec_version` even when the ABI is byte-identical; an edit under an unchanged version is still re-derived by the derivation key (`docs/RUNBOOK.md` "Updating a shared dependency spec"): `0.3.0` → `0.4.0` moved §5.1 and `public_api.signatures` from a Fortran interface block to the language-neutral structured form (`{symbol, signature}`); the published operations, argument types/ranks/`intent`s, and component layouts are unchanged. `0.4.0` → `0.5.0` began transcribing §5.1's value-pinned `module_parameters` (the `dp` / `case_id_len` values) into the `IR`'s `public_api.module_parameters` (a new carrier the `--stage compile` gate pins == §5.1 by value); the §5.1 block, the published operations/types, and the generated ABI are byte-identical — only the IR representation gained the field, so freshness must re-certify a stale `0.4.0` IR that lacks it. `0.5.0` → `0.6.0` made the §5.1 / `IR` leaf vocabulary fully language-neutral: a string length is `deferred` / `assumed` (not the Fortran `:` / `*`) and a kind value is `float64` / `float32` (not `real64` / `real32`); the language backend lowers these tokens to their Fortran spelling, so the published operations, argument types/ranks/`intent`s, component layouts, and the generated ABI are byte-identical — only the leaf-facing representation changed, so freshness must re-certify a stale `0.5.0` IR carrying the old tokens. `0.6.0` → `0.7.0` extends the self-test to exercise the per-case metric fold: the new case `l0_metric_leaf_pass` supplies two sentinel `h_metric` records and the new test of the same name asserts their serialized addresses, so a `__write_diagnostics` that drops the caller-supplied metrics is rejected inside this node instead of only in a consuming physics node. The §5.1 block, the published operations and types, the component layouts, and the generated ABI are unchanged; the self-test behavior and the test profile changed, so freshness must re-certify a stale `0.6.0` IR whose predicates and diagnostics contract lack the new case. Within `0.7.0` the self-test gained the case and test `l0_nonfinite_token_pass` (issue #315), which pins how `__emit_real` spells a non-finite real; the published signatures and types are unchanged, so no `spec_version` is released for it, and a certified `IR` that lacks the case is invalidated by its derivation key, which hashes this document and `tests.md`, not by its version. Within `0.7.0` the self-test's `steps` and `cells_updated` were fixed to the number of cases run, and a non-positive measured `walltime_sec` to `1.0e-9` (issue #394), so that the relative residual of `l0_perf_derived_pass` is defined; the published signatures and types are unchanged, so no `spec_version` is released for it, and every certified `IR` is re-derived, because its derivation key hashes this document and `tests.md`. Within `0.7.0` §2 and §3 state that a check status is written without the blanks that pad it to its width, and the self-test's `l0_metric_leaf_pass` case gained the check `status_na`, supplied padded, whose written value the `post_execute` gate reads (issue #437); the published signatures and types — including the width-4 `status` component of `h_check` — are unchanged, so no `spec_version` is released for it, and every certified `IR` is re-derived, because its derivation key hashes this document and `tests.md`. `0.8.0` (issue #512) changes the signature of `__write_perf`: it drops the `threads_per_rank` argument and writes the thread count the run's shared-memory parallel runtime gives the program, which it obtains itself at run time (§3.2), so that `perf.json` records the count a run was launched with rather than a value its caller chose, and the code that obtains the count is the code this node's own Validate run checks. The host-rendered runner of `0.8.0`'s consumers calls the seven-argument `__write_perf`, which `0.7.0` does not publish, so the release is a new `spec_version`; every other signature and every published type is unchanged. `0.9.0` (issue #582) removes `__write_diagnostics`, `__write_metrics_basis` and the derived types `h_check`, `h_metric`, `h_case_result` and `h_mb_entry`, the test `l0_metric_leaf_pass` with its case, and the test `l0_multi_case_evidence_pass`: the host judges every test from the captures, so no runner writes `diagnostics.json` or `raw/metrics_basis.json`, and every snapshot holds every declared variable. The remaining signatures and `h_named` are unchanged. `0.10.0` (issue #589) adds `__read_case`, `__get_real`, `__get_int`, `__get_bool` and `__get_array_r1..r4`, which read the host-written case file (§2, §3.2), and has `__parse_cases` keep the spec path it used to skip; the self-test gains `l0_read_case_roundtrip_pass`, `l0_read_case_second_pass` and `l0_missing_case_file_xfail`. No signature of `0.9.0` changes, and the next version of the host-rendered runner calls the new operations, which a harness of `0.9.0` does not publish, so the release is a new `spec_version`. Dependent nodes are not migrated by hand and need no content-free version bump of their own. The workflow enforces the skew mechanically at two points:

- **Regeneration** — a node's certified dependency resolution is recorded in its `dependency_graph.json` sidecar; when the catalog moves the harness to a new version, every dependent's recorded resolution stops matching the one `deps.yaml` + `spec_catalog.yaml` derive, so the dependency-freshness readiness check reports it stale and `run_workflow.py --with-deps` re-certifies the closure bottom-up.
- **Skew fail-close** — a consumer that would nonetheless render its runner glue against a drifted interface is stopped before Build by the renderer's signature pin (the language backend's `assert_harness_pin`, reached through `tools/host_render.py`), which compares this §5.1 block against the certified harness IR's `public_api.signatures` and its generated model source.

### 5.1 Canonical interface block
The exact published surface, as a machine-readable **language-neutral** signature block (`module_parameters` / `types` / `procedures`). It describes each published type and operation abstractly — for every argument, result, and derived-type component: its `name`, neutral `type` (`real` / `integer` / `logical` / `string` / `derived`), `rank`, and (for an argument) `intent`; plus the value-pinned module parameters the signatures reference. The vocabulary is neutral throughout: a `string` length is a neutral token — `deferred` (dynamic length) / `assumed` (caller-known length) / a fixed decimal width / a symbol — never the Fortran `:` / `*`; a module-parameter kind value is `float64` / `float32`, never the Fortran `real64` / `real32`. The target language's binding (here Fortran: `real(dp)`, `character(len=:)` / `character(len=*)`, `type(...)`, assumed-shape `(:)` ranks, `integer, parameter :: dp = real64`, the `<spec_id>__` names) is produced by the language backend (`tools/backends/language/fortran/signatures`), not authored here — so this contract is not tied to Fortran. The generated `harness_fortran_cpu_model.f90` must publish every type and procedure below with the signature this block describes, and declare every module parameter below with the value this block pins (whether it exports a module parameter is free, §3) (formatting/continuations/comments may differ; names, argument order, types, ranks, `intent`s, `result` names, and component layout may not). The deterministic gates render this block to the target language and pin it: the `--stage compile` gate cross-checks its symbol set against §5, and the `Generate.static` gate pins the generated model source against these signatures (normalized: comments stripped, continuations joined, case-folded, whitespace-insensitive).

```yaml
module_parameters:
- name: dp
  value: float64
- name: case_id_len
  value: '64'
types:
- name: harness_fortran_cpu__h_named
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
procedures:
- kind: subroutine
  name: harness_fortran_cpu__parse_cases
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
  name: harness_fortran_cpu__emit_real
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
  name: harness_fortran_cpu__emit_int
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
  name: harness_fortran_cpu__emit_bool
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
  name: harness_fortran_cpu__emit_array_r1
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
  name: harness_fortran_cpu__emit_array_r2
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
  name: harness_fortran_cpu__emit_array_r3
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
  name: harness_fortran_cpu__emit_array_r4
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
  name: harness_fortran_cpu__box
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
      name: harness_fortran_cpu__h_named
- kind: subroutine
  name: harness_fortran_cpu__write_snapshot
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
      name: harness_fortran_cpu__h_named
  - name: time
    intent: in
    spec:
      type: real
      kind: dp
- kind: subroutine
  name: harness_fortran_cpu__write_perf
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
  - name: gpu_devices
    intent: in
    spec:
      type: integer
- kind: subroutine
  name: harness_fortran_cpu__read_case
  args:
  - name: case_id
    intent: in
    spec:
      type: string
      len: assumed
  - name: ok
    intent: out
    spec:
      type: logical
- kind: function
  name: harness_fortran_cpu__get_real
  args:
  - name: name
    intent: in
    spec:
      type: string
      len: assumed
  result:
    name: x
    spec:
      type: real
      kind: dp
- kind: function
  name: harness_fortran_cpu__get_int
  args:
  - name: name
    intent: in
    spec:
      type: string
      len: assumed
  result:
    name: i
    spec:
      type: integer
- kind: function
  name: harness_fortran_cpu__get_bool
  args:
  - name: name
    intent: in
    spec:
      type: string
      len: assumed
  result:
    name: b
    spec:
      type: logical
- kind: subroutine
  name: harness_fortran_cpu__get_array_r1
  args:
  - name: name
    intent: in
    spec:
      type: string
      len: assumed
  - name: a
    rank: 1
    intent: out
    spec:
      type: real
      kind: dp
      alloc: true
- kind: subroutine
  name: harness_fortran_cpu__get_array_r2
  args:
  - name: name
    intent: in
    spec:
      type: string
      len: assumed
  - name: a
    rank: 2
    intent: out
    spec:
      type: real
      kind: dp
      alloc: true
- kind: subroutine
  name: harness_fortran_cpu__get_array_r3
  args:
  - name: name
    intent: in
    spec:
      type: string
      len: assumed
  - name: a
    rank: 3
    intent: out
    spec:
      type: real
      kind: dp
      alloc: true
- kind: subroutine
  name: harness_fortran_cpu__get_array_r4
  args:
  - name: name
    intent: in
    spec:
      type: string
      len: assumed
  - name: a
    rank: 4
    intent: out
    spec:
      type: real
      kind: dp
      alloc: true
```

## 6. Prohibitions
- No physics: the harness must embed no per-case kernel or per-test judgment logic; the kernel is the consuming physics node's, and the host judges each test.
- `__write_snapshot` emits its `values` by ITERATING the records: a body that does not iterate them is forbidden, whatever it emits, because a consuming physics node supplies arbitrary records. (`__write_perf` receives only scalars and iterates nothing.) This rule governs the record-derived keys; the envelope keys the contract fixes — the snapshot's declared `time_variable` key and `perf.json`'s field set — stay as specified in §2.
- No truncating or language-specific serialization anywhere in the generated source: never a numeric form that may drop a leading digit, never a language-specific boolean token in place of the literal `true`/`false` (branch to the literal instead), never a non-finite real handed to a numeric format or written as `null` (branch to the literal §2 names). The forbidden Fortran realizations (`F0` / `F0.d` numeric, an `L`-family logical descriptor) are enumerated in `docs/backends/language/fortran/RUNNER_OUTPUT.md` §1.
- Never write `verdict.json`, `aggregate_verdict.json`, `summary.json`, or `trial_meta.json` — not even as a literal filename inside a comment or example string.
- A getter returns the value the case file holds or stops the program (§3.2): a default, a value left from an earlier case and a value this node computes are forbidden, and the only file the reading operations open is the case file `__read_case` names.
- No launch of an external interpreter (`python` / `bash` / `sh` / `node`).
- The thread count is the runtime's: the `threads_per_rank` `__write_perf` writes to `perf.json` is the maximum team size the shared-memory parallel runtime reports when the program runs, or `1` in a build without that runtime enabled, and no other — never a constant of this node, a count read from an environment variable or the host's configuration, or one chosen by a preference of this node.
- All output paths are written relatively (so `cd $(RUNDIR)` redirects them); no hardcoded / sequential snapshot filename literal.

## 7. Traceability
Record the harness adoption in `component_catalog.yaml` (as the `(language, hardware)` = `(fortran, cpu)` runner harness) and the resolved harness version each dependent physics node was certified against.

## 8. tests reference
The corresponding `tests.md` is `spec/infrastructure/infra/harness/harness_fortran_cpu/tests.md`, with `test_profile_version` of `0.12.0`.
