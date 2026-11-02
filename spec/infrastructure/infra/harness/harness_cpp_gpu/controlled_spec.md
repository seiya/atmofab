# Controlled Spec: CUDA C++/GPU runner harness (infrastructure spec)

## 0. Meta information
- `spec_id`: `harness_cpp_gpu`
- `spec_version`: `0.3.0`
- `status`: `controlled_draft`
- `spec_kind`: `infrastructure`
- `domain`: `infra`
- `family`: `harness`

## 1. Responsibility and scope
This `infrastructure` node (R1 harness) is responsible for the shared **runner plumbing** that every CUDA C++/GPU physics node's runner is built against: argv / `--cases` parsing, the case-set loop driver, the JSON emission machinery (numeric / integer / boolean / rank-1..4 real-array tokens), the standard runner-output writers (`raw/state_snapshots/<case_id>.json`, `perf.json`), and the **launch configuration** the host imposes on the program's kernels — the threads per block (§3.3), which this node reads and publishes and a consuming node's kernels are launched with. It carries **no physics**: the per-case kernel is driven by the consuming physics node (a `case_run` callback in the physics `*_checks.cu`), never here, and the host judges each test from the captures. It targets `(language=cuda_cpp, hardware=gpu)`; a different `(language, hardware)` target is a separate harness node. The harness is plumbing: it holds no kernel, and every operation it publishes runs on the host — its sources are host code compiled by the target's CUDA compiler driver. Device residency of a consumer's state is the consumer's (its `case_run` copies to and from the device inside the call), so the writers here always read host memory.

The node's own generated code is a `harness_cpp_gpu_model.cu` publishing the plumbing operations plus a self-test `harness_cpp_gpu_runner.cu` that exercises them and emits the standard runner outputs (so the harness is verified through the exact same Compile→Generate→Build→Validate path as any node; it is self-hosting — the self-test writes its evidence using its own emitters).

The published surface is a **binding, signature-level contract**: §5.1 gives the canonical language-neutral structured interface block (every public type and every operation signature) in a machine-readable fenced code block, from which the language backend renders the CUDA C++ surface (the declarations in the host-rendered header `harness_cpp_gpu_model.cuh`). The generated `harness_cpp_gpu_model.cu` must define exactly those signatures; a consuming physics node's host-rendered runner glue is written against them and holds no serialization knowledge of its own (the JSON envelope assembly lives only inside these certified operations).

## 2. input/output contract
Input (to the self-test runner): the standard runner argv `--cases <spec.ir.yaml> <case_id>...` — the spec path is taken positionally and need not be read; the trailing tokens are the `case_id`s to run, one per `case.test_case_set[]`. The main program marshals the process argv into a token array and calls `harness_cpp_gpu__parse_cases` on it. Each `case_id` selects, by dispatching on the `case_id`, the plumbing aspect that case verifies. Every `case_id` is also the `test_id` of the single-target test that names it, and every test is single-target. A missing `--cases` flag (no cases) makes `__parse_cases` return `ok = false` (the input guard); the guard case verifies this by calling `__parse_cases` on a length-0 token array.

Output artifacts (produced by the writers, into the run node dir relative to `cwd=RUNDIR`):
- **`perf.json`** — one object with `case_id`, `target` (`"gpu"`), `walltime_sec`, `steps`, `cells_updated`, `throughput_cells_per_sec` (`= cells_updated / walltime_sec`), a `parallelism` object (`mpi_ranks`, `threads_per_rank`, `gpu_devices`, `parallel_degree_total = mpi_ranks*threads_per_rank*max(gpu_devices,1)`, and `block_size`, the integer threads per block `__block_size` returns, §3.3), and `timestamp_utc` (ISO-8601). The Validate phase compares `block_size` with the block size the host launched the run with.
- **`raw/state_snapshots/<case_id>.json`** — exactly one per case, named at runtime by concatenating `raw/state_snapshots/`, the `case_id` and `.json` (never a hardcoded/sequential literal). Each holds every variable declared in `snapshot_schema.json` plus the declared scalar `time_variable` `t` (value `0.0`; the self-test has a single `steps=1` step). The snapshot state variables, with their `shape_expr`, are listed under the case that computes them:
  - `l0_numeric_roundtrip_pass`: `x_out` (rank-1, `[3]` — the values re-parsed from `__emit_real`'s tokens).
  - `l0_boolean_literal_pass`: `bool_match` (scalar, `1.0` iff a `true` and a `false` boolean emitted the exact literals `true`/`false`).
  - `l0_array_emit_pass`: `a1` (rank-1, `[2]`), `a2` (rank-2, `[2,2]`), `a3` (rank-3, `[2,2,2]`), `a4` (rank-4, `[2,2,2,2]`) — the arrays `__emit_array_r1..r4` emitted, re-parsed.
  - `l0_case_fanout_pass`: `case_index` (scalar — this case's ordinal in the run).
  - `l0_perf_derived_pass`: `throughput_residual` (scalar — the RELATIVE residual `|throughput_cells_per_sec - cells_updated/walltime_sec| / throughput_cells_per_sec` that `tests.md` judges).
  - `l0_nonfinite_token_pass`: `nonfinite_match` (scalar, `1.0` iff the four non-finite reals of §3 emitted through `__emit_real` produced exactly the literals `NaN`, `NaN`, `Infinity`, `-Infinity`).
  - `l0_block_size_pass`: `block_size` (scalar — the value `__block_size()` returns, as a real), `block_valid` (scalar, `1.0` iff that value is one of the six block sizes of §3.3 and a second call returns the same value).
  - `l0_missing_cases_xfail`: `guard_fired` (scalar, `1.0` when `__parse_cases` on a length-0 token array returned `ok = false`).
  A variable is declared once in `snapshot_schema.json` and written by every case: in a case it is not listed under it holds a shape-valid placeholder, `0.0` or an array of zeros of its declared shape, which no predicate of that case reads. `block_size` is the exception: every case captures what `__block_size` returns, and the Validate phase reads it in every snapshot.

Numeric serialization follows the abstract runner-output contract: a finite real as a round-trip-lossless exponential token, a non-finite real as the literal token `NaN` (whatever the sign bit), `Infinity` or `-Infinity` chosen by branching on the value (never handed to a numeric format, never written as `null`, and never a reason for the emitter to stop the program), an integer as its minimal decimal, a boolean as the literal token `true`/`false` — a truncating form (one that may drop a leading digit) and any language-specific boolean token are forbidden. The target-language realization (for CUDA C++: reals via `std::snprintf` with `%.16e`, never the hexadecimal-floating `%a` / `%A` conversion or `std::hexfloat`; integers via `%d` or `std::to_string`; booleans by branching to the literal, never by printing the `bool` itself; non-finite reals by branching on `std::isnan` / `std::isinf`) is canonical in `docs/backends/language/cuda_cpp/RUNNER_OUTPUT.md` §1, the CUDA C++ binding of `docs/workflow/RUNNER_OUTPUT_CONTRACT.md §4`.

## 3. Operation definition
The published operations (all under module `harness_cpp_gpu_model` — a C++ namespace — prefix `harness_cpp_gpu__`) are the plumbing surface a physics-node runner reuses. The canonical machine-readable interface (every signature and public type, verbatim) is §5.1; the prose below states each operation's contract. The module declares two module-level integer parameters the signatures reference: `dp` (the double-precision real kind token; §5.1 value `float64` = IEEE-754 binary64) and `case_id_len = 64` (the longest case id `__parse_cases` accepts, known to the caller). These are the module's published parameters (the IR carries them as `public_api.module_parameters`), declared once in the host-rendered header that the model and any consuming runner include. Their VALUES are pinned (the gate rejects a drifted `case_id_len`), because a consumer that relies on the bound must see the same one. The CUDA C++ binding of these tokens (`using dp = double;`, `inline constexpr int case_id_len = 64;`) is produced by the language backend and declared in the host-rendered header, not authored here.

### 3.1 Published derived types
The module publishes one derived type (named by its fully-qualified `harness_cpp_gpu__<name>`):
- `harness_cpp_gpu__h_named` `{ name: string (dynamic length), json: string (dynamic length) }` — a **boxed named value**: a JSON key `name` and its already-serialized JSON value `json`. It lets heterogeneous-rank / ragged snapshot variables travel through one homogeneous rank-1 dynamic array of `harness_cpp_gpu__h_named` (a homogeneous array argument cannot carry differing ranks — this box, holding the pre-serialized token, is the workaround). The caller `__box`es a value into it before serialization is complete. (Types and lengths use the §5.1 neutral vocabulary; the CUDA C++ binding is the language backend's: a `string` is a `std::string`, a rank-1 dynamic array a `std::vector`, a `boolean` a `bool`.)

### 3.2 Published operations
- `harness_cpp_gpu__parse_cases(tokens, ntokens, case_ids, ncases, ok)` — parse a supplied argv **token list** (NOT the process argv directly, so the guard is exercisable with a synthetic list): find `--cases` in `tokens`, skip the following token (the positional, unread spec path), collect the remaining tokens as `case_ids` (replacing the vector's contents; each case id at most `case_id_len` characters); return `ok = false` when `--cases` is absent from `tokens`, no `case_id` follows it, or a `case_id` is longer than `case_id_len` (the input guard), else `ok = true`. The self-test's main program marshals the real process argv (the `argc` / `argv` of `main`) into `tokens` for the normal cases, and passes a length-0 `tokens` for the guard case.
- `harness_cpp_gpu__emit_real(x) result(s)` — format a `real (kind dp)` scalar as a JSON numeric token: a finite value in round-trip-lossless exponential form, a non-finite value as the literal `NaN` / `Infinity` / `-Infinity` §2 names.
- `harness_cpp_gpu__emit_int(i) result(s)` — format an integer as a JSON numeric token (minimal decimal; §2).
- `harness_cpp_gpu__emit_bool(b) result(s)` — format a `boolean` as the JSON literal `true`/`false`.
- `harness_cpp_gpu__emit_array_r1(a) result(s)` … `__emit_array_r4(a) result(s)` — format a rank-1..4 `real (kind dp)` array view (`atmofab::View<const dp, R>`: a data pointer and R extents, column-major, the first index fastest — the layout `docs/backends/language/cuda_cpp/CHECKS_ABI.md` §5 states) as a nested JSON array (`[ ... ]`, the leading index outermost), reusing `__emit_real` per element.
- `harness_cpp_gpu__box(name, json) result(nv)` — pack a JSON key `name` and an already-serialized JSON value `json` into a `harness_cpp_gpu__h_named`. A consuming runner emits each of a case's snapshot variables (via the matching `__emit_*`), boxes each with `__box`, and hands the resulting `values` array to `__write_snapshot` in one call; in a physics node the host-rendered glue emits this `__box` list mechanically from `snapshot_schema.json` (the harness stays stateless — it holds no snapshot registry, and does no serialization of the boxed value beyond copying the caller's token).
- `harness_cpp_gpu__write_snapshot(case_id, values, time)` — write the per-case `raw/state_snapshots/<case_id>.json` (runtime-built filename) holding the boxed state variables in `values` (a rank-1 array of `harness_cpp_gpu__h_named`) plus the scalar time variable. The emitted object is **flat**: each boxed variable is written as a **top-level key of the snapshot object**, keyed by its `name` with its already-serialized `json` as the value, sibling to the time variable's key. `values` is the dummy-argument name of the boxed array; neither it nor any other wrapper key appears in the emitted JSON.
- `harness_cpp_gpu__write_perf(case_id, target, steps, cells_updated, walltime_sec, mpi_ranks, threads_per_rank, gpu_devices)` — write `perf.json` with all required fields incl. the derived `throughput_cells_per_sec` and the `parallelism` object (`parallel_degree_total = mpi_ranks*threads_per_rank*max(gpu_devices,1)`). It writes `mpi_ranks`, `threads_per_rank` and `gpu_devices` exactly as its caller passes them, and writes `parallelism.block_size` as the value `__block_size()` returns (§3.3), never a constant.

### 3.3 Published launch-configuration operation
**The block size.** Every kernel a program built against this node launches runs in blocks of the same number of threads, which the HOST chooses and the program does not: the host sets the process environment variable `ATMOFAB_CUDA_BLOCK` to a positive decimal integer, and varies it between runs of the same binary (the Validate phase runs the binary once per execution shape of the target and compares the runs). When the variable is unset or empty the block size is `256`. The admitted values are `32`, `64`, `128`, `256`, `512` and `1024`.
- `harness_cpp_gpu__block_size() result(n)` — the threads per block. The first call reads `ATMOFAB_CUDA_BLOCK` as stated above and keeps the value; every later call returns the kept value, so the block size is fixed for the whole run. It launches nothing and calls no CUDA runtime function. The host-rendered runner of a consuming node calls it once, before the first case, and hands the value to the node's sources through the host-rendered header (`docs/backends/language/cuda_cpp/GENERATE_RULES.md` §7); a consuming node's own sources never call it.

The self-test `harness_cpp_gpu_runner.cu` includes the header `harness_cpp_gpu_model.cuh`, calls `__parse_cases`, then for each `case_id` runs (dispatching on the `case_id`) the plumbing check that case names — verifying the emitter round-trips, the case fan-out, the block size, and the input guard — and boxes that case's snapshot variables. The `l0_nonfinite_token_pass` case obtains four non-finite reals AT RUN TIME from the language's IEEE facility (`std::numeric_limits<double>::quiet_NaN()` / `infinity()`, and `std::copysign` for the sign bit) — a quiet NaN with the sign bit clear, a quiet NaN with the sign bit set, `+inf` and `-inf` — never from a literal division a compiler may refuse to fold; it emits each through `__emit_real`, compares the four strings with `NaN`, `NaN`, `Infinity`, `-Infinity`, and emits `nonfinite_match` (`1.0` when all four match, else `0.0`) into its snapshot. The `l0_block_size_pass` case calls `__block_size()` twice, emits the first value as `block_size` and `block_valid` (`1.0` when the value is one of the six of §3.3 and the second call returned the same value, else `0.0`) into its snapshot; it learns the block size from `__block_size` alone and never reads `ATMOFAB_CUDA_BLOCK` itself. It reads its clock before the first case and again after the last, and then calls `__write_perf` once, with that elapsed time as `walltime_sec`, or `1.0e-9` when the elapsed time it reads is not positive (a clock tick longer than the interval), and with `steps` and `cells_updated` both equal to the number of cases its `__parse_cases` call on the program's argv returned: each case runs its one plumbing check as one step (`steps = 1`, `tests.md` §3) that updates one cell, so each value is the sum of the cases' steps, independent of any case's `grid` member and positive whenever a case ran. With both quantities positive, the relative residual of the `tests.md` §6 `l0_perf_derived_pass` judgment is defined. Everything after that call is outside the measured interval. Only then does it compute the `l0_perf_derived_pass` residual, re-parsing the `perf.json` that call wrote. `__write_snapshot` is called once per case, inside the loop or after the `__write_perf` call — the `l0_perf_derived_pass` snapshot only after that residual is computed. Because the writers use the emitters, a correct emitter is necessary for a correct output; the snapshots are the evidence the host judges that plumbing by.

## 4. Failure conditions and constraints
A missing `--cases` flag (or no `case_id` after it) is a hard input error — `__parse_cases` returns `ok = false`, and the self-test's `l0_missing_cases_xfail` case exercises this guard by calling `__parse_cases` on a synthesized empty token list and confirming `ok = false` (recorded as `guard_fired = 1.0`). A JSON emitter whose re-parsed token does not reproduce its input within an absolute tolerance of `1e-12` fails the corresponding test's predicate. `__block_size` stops the program with a non-zero exit status, after a message on the standard error stream, when `ATMOFAB_CUDA_BLOCK` is set, non-empty, and not one of the six decimal values of §3.3 (a sign, a blank or any other character included), so that a run never launches its kernels in a configuration the host did not impose.

## 5. Public API and compatibility
The published `operation_id`s are exactly: `harness_cpp_gpu__parse_cases`, `harness_cpp_gpu__emit_real`, `harness_cpp_gpu__emit_int`, `harness_cpp_gpu__emit_bool`, `harness_cpp_gpu__emit_array_r1`, `harness_cpp_gpu__emit_array_r2`, `harness_cpp_gpu__emit_array_r3`, `harness_cpp_gpu__emit_array_r4`, `harness_cpp_gpu__box`, `harness_cpp_gpu__write_snapshot`, `harness_cpp_gpu__write_perf`, and the launch-configuration operation of §3.3, `harness_cpp_gpu__block_size`. The module also publishes the derived type `harness_cpp_gpu__h_named`.

A change breaking compatibility of any signature (or of a published derived type's component layout) is a **breaking change released under a new `spec_version`**, not a rename. A change to how the published surface is CARRIED — the §5.1 / `IR public_api.signatures` REPRESENTATION — is likewise released under a new `spec_version` even when the ABI is byte-identical; an edit under an unchanged version is still re-derived by the derivation key (`docs/RUNBOOK.md` "Updating a shared dependency spec"). `0.1.0` is the first version: its §5.1 block, published operations and types, and self-test are those of `harness_fortran_cpu@0.7.0` under this node's names, bound to CUDA C++ by the language backend. Within `0.1.0` the self-test gained the case and test `l0_nonfinite_token_pass` (issue #315), which pins how `__emit_real` spells a non-finite real; the published signatures and types are unchanged, so no `spec_version` is released for it, and a certified `IR` that lacks the case is invalidated by its derivation key, which hashes this document and `tests.md`, not by its version. Within `0.1.0` the self-test's `steps` and `cells_updated` were fixed to the number of cases run, and a non-positive measured `walltime_sec` to `1.0e-9` (issue #394), so that the relative residual of `l0_perf_derived_pass` is defined; the published signatures and types are unchanged, so no `spec_version` is released for it, and every certified `IR` is re-derived, because its derivation key hashes this document and `tests.md`. Within `0.1.0` §2 and §3 state that a check status is written without the blanks that pad it to its width, and the self-test's `l0_metric_leaf_pass` case gained the check `status_na`, supplied padded, whose written value the `post_execute` gate reads (issue #437); the published signatures and types — including the width-4 `status` component of `h_check` — are unchanged, so no `spec_version` is released for it, and every certified `IR` is re-derived, because its derivation key hashes this document and `tests.md`. `0.2.0` (issue #512) publishes the block size the host imposes on the program's kernels through `ATMOFAB_CUDA_BLOCK` (`__block_size`, §3.3) and has `__write_perf` record `parallelism.block_size`; the self-test gains `l0_block_size_pass`. The host-rendered runner of `0.2.0`'s consumers calls `__block_size`, which `0.1.0` does not publish, so the release is a new `spec_version`; the signatures `0.1.0` published are unchanged. `0.3.0` (issue #582) removes `__write_diagnostics`, `__write_metrics_basis` and the derived types `h_check`, `h_metric`, `h_case_result` and `h_mb_entry`, the test `l0_metric_leaf_pass` with its case, and the test `l0_multi_case_evidence_pass`: the host judges every test from the captures, so no runner writes `diagnostics.json` or `raw/metrics_basis.json`, and every snapshot holds every declared variable. The remaining signatures and `h_named` are unchanged. Dependent nodes are not migrated by hand and need no content-free version bump of their own. The workflow enforces the skew mechanically at two points:

- **Regeneration** — a node's certified dependency resolution is recorded in its `dependency_graph.json` sidecar; when the catalog moves the harness to a new version, every dependent's recorded resolution stops matching the one `deps.yaml` + `spec_catalog.yaml` derive, so the dependency-freshness readiness check reports it stale and `run_workflow.py --with-deps` re-certifies the closure bottom-up.
- **Skew fail-close** — a consumer that would nonetheless render its runner glue against a drifted interface is stopped before Build by the renderer's signature pin (the language backend's `assert_harness_pin`, reached through `tools/host_render.py`), which compares this §5.1 block against the certified harness IR's `public_api.signatures` and its generated model source.

### 5.1 Canonical interface block
The exact published surface, as a machine-readable **language-neutral** signature block (`module_parameters` / `types` / `procedures`). It describes each published type and operation abstractly — for every argument, result, and derived-type component: its `name`, neutral `type` (`real` / `integer` / `logical` / `string` / `derived`), `rank`, and (for an argument) `intent`; plus the value-pinned module parameters the signatures reference. The vocabulary is neutral throughout: a `string` length is a neutral token — `deferred` (dynamic length) / `assumed` (caller-known length) / a fixed decimal width / a symbol — and a module-parameter kind value is `float64` / `float32`. The target language's binding (here CUDA C++: `double` for `dp`, `std::string` for a string of any length, `bool`, `int`, `atmofab::View<const dp, R>` for an `intent(in)` real array, `std::vector<T>` for a rank-1 array of a derived type or string, a `struct` per type, a function returning `void` per `subroutine`, a `T&` per `intent(out)` scalar, the `<spec_id>__` names inside namespace `harness_cpp_gpu_model`) is produced by the language backend (`tools/backends/language/cuda_cpp/signatures`, stated in `docs/backends/language/cuda_cpp/BUNDLE_BINDING.md` §2), not authored here — so this contract is not tied to one language. The host renders these declarations into `harness_cpp_gpu_model.cuh`; the generated `harness_cpp_gpu_model.cu` includes that header, declares none of them again, and must define every procedure below with the signature this block describes (formatting and comments may differ; names, argument order, types, ranks, `intent`s, and component layout may not). The deterministic gates render this block to the target language and pin it: the `--stage compile` gate cross-checks its symbol set against §5, and the `Generate.static` gate pins the generated model source against these signatures (whitespace-insensitive, case-sensitive).

```yaml
module_parameters:
- name: dp
  value: float64
- name: case_id_len
  value: '64'
types:
- name: harness_cpp_gpu__h_named
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
  name: harness_cpp_gpu__parse_cases
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
  name: harness_cpp_gpu__emit_real
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
  name: harness_cpp_gpu__emit_int
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
  name: harness_cpp_gpu__emit_bool
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
  name: harness_cpp_gpu__emit_array_r1
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
  name: harness_cpp_gpu__emit_array_r2
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
  name: harness_cpp_gpu__emit_array_r3
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
  name: harness_cpp_gpu__emit_array_r4
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
  name: harness_cpp_gpu__box
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
      name: harness_cpp_gpu__h_named
- kind: subroutine
  name: harness_cpp_gpu__write_snapshot
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
      name: harness_cpp_gpu__h_named
  - name: time
    intent: in
    spec:
      type: real
      kind: dp
- kind: subroutine
  name: harness_cpp_gpu__write_perf
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
- kind: function
  name: harness_cpp_gpu__block_size
  result:
    name: n
    spec:
      type: integer
```

## 6. Prohibitions
- No physics: the harness must embed no per-case kernel or per-test judgment logic; the kernel is the consuming physics node's, reached through its `case_run` callback, and the host judges each test.
- `__write_snapshot` emits its `values` by ITERATING the records: a body that does not iterate them is forbidden, whatever it emits, because a consuming physics node supplies arbitrary records. (`__write_perf` receives only scalars and iterates nothing.) This rule governs the record-derived keys; the envelope keys the contract fixes — the snapshot's declared `time_variable` key and `perf.json`'s field set — stay as specified in §2.
- No truncating or language-specific serialization anywhere in the generated source: never a numeric form that may drop a leading digit, never a language-specific boolean token in place of the literal `true`/`false` (branch to the literal instead), never a non-finite real handed to a numeric format or written as `null` (branch to the literal §2 names). The forbidden CUDA C++ realizations (the hexadecimal-floating `%a` / `%A` conversion or `std::hexfloat`; printing a `bool` itself) are enumerated in `docs/backends/language/cuda_cpp/RUNNER_OUTPUT.md` §1.
- Never write `verdict.json`, `aggregate_verdict.json`, `summary.json`, or `trial_meta.json` — not even as a literal filename inside a comment or example string.
- No launch of an external interpreter (`python` / `bash` / `sh` / `node`).
- No kernel and no device call: no `__global__` function, no kernel launch, no CUDA runtime API call. The harness is host plumbing (§1).
- The block size is the host's: `__block_size` returns the value `ATMOFAB_CUDA_BLOCK` names, or `256` when it names none, and no other — never one chosen from the device's properties, an occupancy query, or a preference of this node — and `__write_perf` records that value.
- All output paths are written relatively (so `cd $(RUNDIR)` redirects them); no hardcoded / sequential snapshot filename literal.

## 7. Traceability
Record the harness adoption in `component_catalog.yaml` (as the `(language, hardware)` = `(cuda_cpp, gpu)` runner harness) and the resolved harness version each dependent physics node was certified against.

## 8. tests reference
The corresponding `tests.md` is `spec/infrastructure/infra/harness/harness_cpp_gpu/tests.md`, with `test_profile_version` of `0.7.0`.
