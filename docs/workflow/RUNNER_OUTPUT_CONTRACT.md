# Runner output contract (perf.json / raw/)

> **Audience: `Validate.judge` and a runner-authoring `Generate` leaf.** The single
> canonical source for *what the `runner` must emit* and *how it serializes it* — §3
> (`raw/` evidence) is what the judge holds the run's evidence against. There is no §1:
> the diagnostics document it specified was retired by
> [issue #582](https://github.com/seiya/spec2science/issues/582), and the other sections keep
> their numbers so that a citation holds. It supersedes the runner-output rules once duplicated in
> `phase_02_generate.md` §2-1, `phase_04_validate.md` §4-1, and
> `PERFORMANCE_DIAGNOSTICS.md` §2/§6. The deterministic `Build` / `Validate.execute`
> steps take their contract from `phase_03_build.md` / `phase_04_validate.md`.

> **Scope note (R1/M3c-β → M3d).** On an *M3c node* (a physics node of a target the host
> authors the build control file and renders the runner for, over that target's harness) the
> `runner` is **host-rendered**, the **driver** — the leaf authors the model alone, the harness
> owns the JSON assembly, and the host authors the verdict. A rendered runner calls no writer but
> the snapshot and performance ones. What it does per case is §3, and what it reads is §6.
> Since M3d this doc is **not for a physics `Generate` leaf** (it authors no runner). It
> governs the runner-authoring one — the `infrastructure` self-test — and
> `Validate.judge`. Both get it (issue #169) as the seed `runner_output_contract.md`: whole,
> followed by the target language's runner-output binding (issue #289), in both
> `harness`-shape `generate` trees, §3 in the judge's. The *authoring* rules below survive as
> **deterministic backstops** (name / forbidden-output / JSON-descriptor /
> snapshot-filename gates in `validate_pipeline_semantics.py`).

The `runner` emits **only** `perf.json` and the `raw/state_snapshots/` captures
(plus `stdout.log` / `stderr.log`). It must **never** write
`verdict.json`, `aggregate_verdict.json`, `summary.json`, or `trial_meta.json`
— the host authors those at `Validate`. The check is a substring match
against the **whole runner source including comments**: even an identical
string inside a comment or example is a fail.

## 2. `perf.json` (required fields)

One `perf.json` per run: one `write_perf` call, after the last case, never
rewritten. Required fields (a custom minimal
schema such as `{case_count, wall_seconds}` is rejected by the `post_execute`
gate, which mandatorily verifies `walltime_sec` / `throughput_cells_per_sec` /
`parallelism`):

- `case_id`: string
- `target`: `cpu|gpu|...`
- `walltime_sec`: elapsed wall-clock seconds, final before `write_perf` is called.
  - On a host-rendered driver it is the sum over the calls of the `step` operation alone: reading a
    case, allocating, `init`, the captures, the series fold and the samples are outside it. A run
    whose calls measure no positive time writes `1.0e-9`.
  - On a hand-authored runner it is the case executions: from before the first case to after the
    last, the work `steps` / `cells_updated` count. The run-wide writers and a self-test's re-parse
    of its own `perf.json` are outside it.
- `steps`: number of execution steps. On a host-rendered driver, the sum over the cases of the
  committed steps this process ran.
- `cells_updated`: total updated cells (`nx*ny*nz*steps` etc.). On a host-rendered driver, the sum
  over the cases of those steps times the cell count of the case's largest state array,
  saturating at the default integer's largest value. No gate reads `steps` or `cells_updated`.
- `throughput_cells_per_sec`: `cells_updated / walltime_sec`
- `parallelism` (required object): `mpi_ranks` / `threads_per_rank` /
  `gpu_devices` / `parallel_degree_total`
  (`= mpi_ranks * threads_per_rank * max(gpu_devices,1)`); a process-grid harness
  adds `process_grid` (`[p1, p2]`), a device harness `block_size` (threads
  per block; #512)
- `timestamp_utc`: ISO8601 (recommended)

A `perf.json` missing `parallelism` is invalid/unjudgeable.

## 3. `raw/` primary evidence

The required composition is IR-driven —
`spec.ir.yaml.io_contract.raw_requirements.required_evidence` is the canonical
source; do not uniformly require a fixed minimal composition.

- **`raw/state_snapshots/<case_id>.json`** — when `state_snapshots` is required,
  the runner writes **exactly one snapshot per case**, named from the `case_id`
  it receives on argv (`--cases <spec.ir.yaml> <case_id>...`, one positional per
  `case.test_case_set[]`). Build the path from that `case_id`
  (`trim(case_id)//'.json'`), never a hardcoded/sequential literal
  (`snapshot_0001.json`) or a single combined file: a string-literal name is
  flagged by `post_generate`; a wrong runtime-built name fails
  `Validate.execute`'s per-`<case_id>.json` deliverable gate. Each snapshot must
  hold **every** declared state variable, in every case and on every node (a harness
  self-test as a host-rendered runner), **plus the declared `time_variable`**, each
  shape-matching its
  `snapshot_schema.json` declaration (state variables their `shape_expr`, the
  `time_variable` its `time_shape_expr`). A case that rejects its input or
  produces no meaningful values (e.g. a `*_xfail` length-guard case) **still
  emits every declared variable, shape-valid, and must not drop the key**: a value
  whose JSON shape matches its `shape_expr`, `0.0` or zeros of the declared extents
  (the host refuses an empty array in any capture). It may additionally record a guard flag.
  - Correct (a guard case, `x` declared `[3]`): `{"case_id":"l0_invalid_length_xfail","step":0,"x":[0.0,0.0,0.0],"invalid_rejected":true}`.
  - Wrong: `{"case_id":"l0_invalid_length_xfail","step":0,"invalid_rejected":true}` — drops the declared `x` → `post_execute` fails with "declared state_variables missing"; and `"x":[]` — the host refuses an empty capture.
  - **No placeholder text.** `post_execute` scans every file under
    `raw/state_snapshots/` (`snapshot_schema.json` included), spaces and newlines
    removed, for `"dummy"` / `"placeholder"` / `"sample": "state_recorded"`; a hit
    fails (`placeholder content detected`). Each pattern carries its own quotes, so
    the fail condition is a key or string value exactly `dummy` or `placeholder`, or
    a `sample` field valued `state_recorded` — anywhere in the file.

  When `required_evidence` does not declare `state_snapshots` as required,
  `raw/state_snapshots/` must not be required.
- **`raw/state_snapshots/initial/<case_id>.json`** (host-rendered runners only) — the
  driver the host renders for an M3c physics node writes a SECOND capture of each case,
  before the first call of `step`, after `init`, under `initial/`, of exactly the per-case
  shape above (Z6, issue #255). It is a deliverable of that runner and is
  shape-checked like the final snapshot. A hand-authored runner (a harness self-test)
  does not write it, and nothing requires it to. The host pre-creates
  `raw/state_snapshots/` in the run directory of every runner, and
  `raw/state_snapshots/initial/`, `history/` and `series/` only for a host-rendered one, so a
  hand-authored runner's run directory `raw/state_snapshots/` holds exactly the files it writes
  ([issue #368](https://github.com/seiya/spec2science/issues/368)).
- **`raw/state_snapshots/history/<case_id>__<k>.json`** (a host-rendered runner on a node whose
  `drive` declares `loop`, when the IR requires `state_history`) — a sample of the full state at
  committed step `<k>`, every `every` committed steps (`every` = 0: none), of the per-case shape
  above.
- **`raw/state_snapshots/series/<case_id>.json`** (the same node, when the IR requires
  `step_series`) — exactly the names the IR's `step_series` entry declares, `step`, `<v>_min` and
  `<v>_max` of every array state, each a list with one entry per committed step, all of one length,
  and the writer's scalar `t`, the time of the last committed state. `t` is not a series name. The
  driver folds the extrema over the committed state and carries a non-finite committed cell into
  the entry as `NaN`, and the host's series reader refuses such an entry.

**What the driver captures.** A capture of a host-rendered driver holds every state variable, `step`
and the writer's scalar `t`. Per case the driver reads the case through `__read_case` and the getters
(§6), allocates each state and fills it from the case's `initial_state`, and calls `init` if `drive`
declares one on the state itself; a false `init` result leaves the case with both captures written
and no step. It then writes the initial capture. On a node whose `drive` declares `loop` it calls
`step` up to `n_step` times through work copies: an `inout` dummy receives a copy of the state and every `out`
array a NaN-filled work array. A true result commits the call: the state is copied back, `step`
advances, `t = t_start + step * dt`, a series entry is appended and a sample is written every `every`
steps. A false result ends the loop on the last committed state. After `init` a loop node's driver
reads `restart.step`; at `K` > 0 (a continuation case, `phase_04_validate.md` §4-1) it
restores each `restart.<v>` and starts `step` at `K`. Without `loop` it makes ONE call on the
state itself, each `out` starting at its `initial_state` content, and `step` becomes 1 only on a true
result. After every call each logical `out` is written into its state as 1 or 0. It then writes the
final capture and, on a `loop` node, the series file; after the last case, `__write_perf`.

## 4. JSON serialization (UTF-8)

`perf.json` and every `.json` under `raw/` must be a UTF-8
JSON object restorable by a standard JSON parser. Every finite numeric token follows
RFC 8259 (`.123` / `-.123` with a missing leading zero are forbidden).

**A non-finite real** is written as the literal token `NaN`, `Infinity` or `-Infinity`
(`NaN` whatever the sign bit), chosen by branching on the value, never by handing the
value to a numeric format. These three tokens are the one departure from RFC 8259 this
contract takes: they are exactly the non-finite tokens the host's reader accepts, and
any other spelling a numeric format may print for the value (`nan`, `-nan`, `inf`;
a processor-dependent `NaN(...)`) makes the document unreadable. A non-finite value is never written as
`null`, and serializing one never stops the program: the token is
written and the other cases' evidence is still owed. A writer may still stop on an input outside a
range it states for its own arguments (a non-positive `walltime_sec`, which `-Infinity` also
is); that is a refusal of the input, not a spelling of the value, and a value's being
non-finite is never such a range. What the host does with the value is decided elsewhere —
a non-finite value in a declared state variable or the time variable of a capture a
primary predicate reads is a `structural_violation` naming the variable
(`phase_04_validate.md`).

The writer rules a runner of a given language follows to satisfy this — which of its
format descriptors are forbidden for a JSON token and the canonical safe idiom — are that
language backend's: `docs/backends/language/<language>/RUNNER_OUTPUT.md`. The host appends it
to this document wherever this document is seeded whole.

## 5. Other runner constraints

- When the target profile's `toolchain.language` is a compiled language (its language
  backend declares `COMPILED`), the `runner` must not launch an external interpreter
  (`python` / `bash` / `sh` / `node`).
- The `runner` writes its output paths **relatively** so a `cd $(RUNDIR)` in the
  `make test`/`check` target redirects them under the run dir (see the build
  system's control-file document, `docs/backends/build_system/<id>/CONTROL_FILE.md` §2).
- The `runner` is **always** invoked with `--cases <spec.ir.yaml> <case_id>...`:
  by `run_program` directly and by the `make test`/`check` target (which forwards
  the same argv, so the re-run writes the same captures as `run_program`'s). It
  may treat a missing `--cases` as a hard error (no no-argv mode needed); it takes
  the spec path positionally and need not read the spec file. The directory of that
  path is where the case files are (§6).

## 6. The case file

- The host evaluates each case's inputs and writes the result to
  `case_inputs/<case_id>.json` beside `spec.ir.yaml`, before the program runs, for every
  case of the IR that is no continuation: at every `Validate.execute`, and in a `workshop leaf`'s
  `run` operation beside the IR the host seeded in its tree (a continuation's: §3). The `runner` opens this file through the certified harness's `__read_case`
  and reads each value through the typed getters; it parses neither the file nor
  `spec.ir.yaml` itself.
- The file is one flat JSON object on one line of any length, with sorted keys. A key is
  a name: `inputs.<path>` for an input, `derived.<name>` for a host-evaluated derived value,
  a state variable's name for its initial content, and on a `loop` node `restart.step`, with a continuation's `restart.<v>` (§3). On an
  `infrastructure` node it holds every numeric `inputs.*` leaf of the case, a boolean written as
  `1` or `0`.
- Each value is an RFC 8259 number or a rectangular nested array of numbers of depth 1 to 4
  (the leading array index outermost, as the emitters write it), an empty array admitted. This
  grammar is not §4's: a number is spelled as the host's serializer spells it (`3`, `0.1`,
  `1e-30`), and no non-finite token occurs, because the host refuses a non-finite input.
- The certified harness's reading operations are the only reader:
  - `__parse_cases` keeps the spec path when it returns `ok = true`, and a call that returns
    `ok = false` leaves the kept path as it is. The token array holds that path whole, so its width
    is not `case_id_len`.
  - `__read_case(case_id, ok)` reads the file of `case_id` from `case_inputs/` in the directory of
    the kept path (no directory part: the current directory), the whole line whatever its length.
    `ok = true` replaces the loaded case. `ok = false` loads none, so no value of an earlier case
    remains readable. It is `false` when the file is absent, when it holds anything but the grammar above (a nested
    object, a string, `true` / `false` / `null`, a non-finite token, a ragged array, an array deeper
    than 4, a `\` in a key, a number that does not convert to a finite binary64 value), and when
    no `__parse_cases` has succeeded. It never stops the program.
  - `__get_real(name)` reads any number token; `__get_int(name)` an integer token (no fraction, no
    exponent) that fits the default integer; `__get_bool(name)` exactly the token `1` (true) or `0` (false);
    `__get_array_r1..r4(name, a)` allocates `a` to the array's extents, the leading array index the
    outermost JSON level. An empty array gives extent 0 at its level and every level below, so `[]`
    reads at every rank and `[[],[]]` at rank 2 and above.
  - A getter holds no default and takes no `ok`. With no case loaded, a name the case does not hold,
    a scalar asked as an array or an array as a scalar, an array of another nesting depth, or a
    token its type does not admit, it stops the program with a non-zero exit status. Do not call a
    getter inside an output statement: a runtime can hold that unit's lock while the stop runs,
    and the program then hangs instead of stopping.
