# Phase 1: Compile

## Overview
The phase that integrates the natural-language specification (`controlled_spec.md` / `tests.md` / `deps.yaml`) into a **single structural IR (`spec.ir.yaml`)**. It is the only phase that reads `controlled_spec.md` as a **generation input**, and the subsequent `Generate` / `Build` / `Validate` use `spec.ir.yaml` as the canonical source (the lone read of `controlled_spec.md` downstream is `Generate.verify`'s requirement-fidelity cross-check, not a generation input).

## I/O contract
- execution input: `controlled_spec.md`, `tests.md`, `deps.yaml`, `spec/registry/spec_catalog.yaml`
- verification input: `controlled_spec.md`, `tests.md`, `deps.yaml`, `spec/registry/spec_catalog.yaml`, the generated `spec.ir.yaml`
- No leaf is seeded the registry, because the two facts a leaf takes from it arrive already resolved by the host: the dependency closure as `<ir_ref>/dependency_graph.json`, and each component dependency's published operation names together with its certified IR's `public_api` `signatures` / `interfaces` / `module_parameters` in the producer's `DEPENDENCIES.md` seed (`<ir_ref>/dependency_surface.json` for the reviewer). Where this document says "read `spec_catalog.yaml`", a leaf reads those seeds instead and must not conclude that an input is missing.
- output: `workspace/ir/<node_key_safe>/<ir_id>/spec.ir.yaml`, `ir_meta.json`

## substep structure
- `Compile.generate`: the LLM substep that generates `spec.ir.yaml`.
- **Both LLM substeps run as `workshop leaf`s** ([issue #534](https://github.com/seiya/atmofab/issues/534) PR-J4-b; `docs/ORCHESTRATION.md` §workshop leaf is canonical for the mechanism, and `docs/GLOSSARY.md` for the term): `Compile.generate` is the PRODUCING kind and `Compile.verify` the READING kind. The host seeds each leaf's `work tree` with a closed context, one `seed` file per document, and with nothing that names an implementation target, because the IR is target-free (issue #284: the target profile the host builds for is chosen per run, after Compile). The producer is seeded this phase's three `spec` documents, the `controlled_spec.md` of each `profile` this node adopts (per the sidecar's `profiles` record), the host-derived `<ir_ref>/dependency_graph.json`, THIS document in full, the two `docs/examples/spec_ir_algorithm*` reference forms, and the host-resolved dependency facts as `DEPENDENCIES.md` when the host resolved any. The reviewer is seeded the three `spec` documents, the IR under review, `<ir_ref>/dependency_surface.json`, and — as the producer is — THIS document in full and the two reference forms; it is NOT seeded the profile spec or the dependency graph, and it takes its severity rubric from §Severity of a finding below rather than from a seed of its own. Neither leaf has repository write authority. The producer is served one host operation, `check_ir`, and writes the WHOLE IR as `spec.ir.yaml` (or a `Compile fail` declaration as `compile_fail.json`, §1-1) at its tree root; the reviewer is served no operation and writes the verify verdict (`verification_status` / `issue_severity` / `last_fail_reason` / `findings`) as `verdict.json`. The HOST snapshots the tree, reads that file, and writes `spec.ir.yaml` and `ir_meta.json` itself. A `check_ir` result is feedback; `Compile.static` is the authority. A configured entry that narrows its `capabilities:` to drop `workshop` is REFUSED at config load (`llm_config_capability_insufficient_for_substep`): there is no other leaf model to fall back to.
- `Compile.static`: a **deterministic conductor in-process substep** (no LLM leaf) run AFTER `Compile.generate` and BEFORE `Compile.verify`. The conductor (`workflow_conductor._compile_static_inproc`) runs the purely-static IR gates — `validate_workspace_root.py` and `validate_pipeline_semantics --stage compile` — and authors `compile_static_meta.json`. A violation is a content failure routed back to `Compile.generate` as a reopen that carries the findings (mirrors the `Generate.gate` static check). So `Compile.verify` is reached only on a deterministically-clean IR.
- `Compile.verify`: an independent LLM substep that self-checks the structural invariants (context-isolated from `Compile.generate`). It launches **no** `validate_pipeline_semantics` gate (that responsibility moved to `Compile.static`); it is a pure semantic pass over the spec-cross-reference invariants of §1-2 — **every one of them**. This sentence deliberately names no subset: an earlier version listed three of them and was read as the scope, which drops V2 — the one family no deterministic gate re-checks (§1-2 V2 says so itself), so a reviewer that skips it is the last thing between a thinly-lowered IR and a `Generate` leaf that cannot read `controlled_spec.md`.

## `spec.ir.yaml` schema

`spec.ir.yaml` is a YAML mapping artifact, and requires holding the following top-level keys.

```yaml
schema_version: "1.0"

meta:
  node_key: "<spec_kind>/<spec_id>@<spec_version>"
  spec_kind: "<problem|component|infrastructure>"   # never `profile`: no IR is authored for one
  spec_id: "<spec_id>"
  spec_version: "<semver>"
  source_refs:
    controlled_spec: "spec/<spec_kind>/<domain>/<family>/<spec_id>/controlled_spec.md"
    tests: "spec/<spec_kind>/<domain>/<family>/<spec_id>/tests.md"
    deps: "spec/<spec_kind>/<domain>/<family>/<spec_id>/deps.yaml"

case:
  # determined values of runtime input (with sweep already expanded). A RUNTIME input is a key or
  # member whose value the node's algorithm takes per case as controlled_spec.md / tests.md define
  # it (grid, time, initial, boundary, constants, a node-specific parameter or sentinel), whether
  # or not a predicate also reads it; each needs a route in the IR (V9). The PROVENANCE inputs
  # (profile_selection, test_profile_id, test_profile_version) point at what the case was authored
  # under and are the host's. Any other key or member (a value only the tests compare against, a
  # label, a note) asks nothing of the code.
  test_case_set:
    - case_id: "<case_id>"
      inputs:
        grid: {...}
        time: {...}
        initial: {...}
        boundary: {...}
        profile_selection: {profile_id: "<id>", profile_version: "<v>"}  # exactly these 2 keys,
        # copied from a dependency_graph.json `profiles[]` entry, when `profiles` is non-empty.
        # When it is empty this gate does not read the field at all, and no case declares one: a
        # node that adopts no profile (`profiles: []`, every harness) carries no
        # `profile_selection`, and its `tests.md` gives the name no other meaning. This comment is
        # the one statement of it; the Compile.generate rule (7) and the GLOSSARY cite it.
        test_profile_id: "<test_profile_id>"
        test_profile_version: "<version>"
      initial_state:   # HOST DATA (issue #589): the content each state variable holds before
        # the first call, which the host evaluates per case (`primary_evidence.evaluate_case_inputs`;
        # `Compile.static` refuses a non-finite value or a shape mismatch). Exactly one entry per
        # schema variable in EVERY case, except a variable `drive.init` writes, a logical state and a
        # `calls` or `record` state of a procedure binding (each starts at 0, the driver's; an entry for
        # one is refused); no key outside the schema.
        # The classes are THE CASE'S: on a list-form `drive.step` they follow from the entries that run
        # this case, so a logical state, an operation-only output or a written state of ANOTHER entry,
        # and a state this case's entries only read or do not bind, is host data here and takes its
        # entry (an array no entry of this case binds is one the driver is rendered to capture
        # unchanged, `drive` below).
        # A state an operation reads: its initial value — a number (filled to the shape), a nested
        # list of exactly the shape, or a predicate-grammar expression over `inputs.*`, the
        # coordinates and the constants (no capture, `derived`, `history`, `series`, `at`, `comparand`),
        # broadcast to the shape after dropping leading extent-1 axes. An OPERATION-ONLY output (written
        # by `step`, read by no `step` dummy, not written by `init`): its PRE-CALL CONTENT, the spec's
        # §2 `setup` value as a scalar NUMBER literal (a `-999` sentinel), never an expression.
        <var>: "<number | nested list | expression>"
      derived:         # optional, HOST DATA: named values `drive` binds (a cell size, `dt`, a step
        # count, a field such as a Coriolis profile), evaluated in mapping order: a number, or an
        # expression over `inputs.*`, the coordinates, the constants, earlier `derived.<n>`, and
        # `initial.<v>` of a variable that has an `initial_state` entry (never of one `init` writes;
        # no `final`, `history`, `series`, `at`, `comparand`). A predicate reads it as `derived.<n>`.
        <name>: "<number | expression>"
  sweeps: {...}        # optional. when the test profile defines a sweep
  refinements: {...}   # optional. when grid refinement is defined

algorithm:
  algorithm_id: "<id>"
  execution_mode: "<sequence|conditional|iterative|columnwise>"   # select per the "algorithm.execution_mode" section below; time marching over n_step is iterative (a loop inside one call; `drive.loop` is the driver's)
  steps:
    - step_id: "<step_id>"
      step_kind: "<boundary_apply|reconstruct|flux_compute|source_term|time_integrate|column_process|pointwise_process|iterative_solve|filter|reduction|diagnostic>"
      operation_ref: "<operation_id>"
      inputs: ["<var1>", "<var2>"]
      outputs: ["<var3>"]
  ordering: [...]              # a sequence of step_id, or an array of before/after dependency objects
  control_condition: ...       # one of a string, a string array, or an object. required non-empty when execution_mode=conditional
  iteration_contract: {...}    # object, the TOP-LEVEL loop only. non-empty when execution_mode=iterative; conversely a loop counter + stop_condition here forbids execution_mode sequence and conditional
  update_semantics: {...}
  temporaries: [...]           # a string array, or an array of name + shape_expr objects
  derived_field_rules: [...]
  invariants: ["<inv1>", ...]  # a non-empty string array
  splitting_policy:
    kind: "<kind>"

io_contract:
  # integrates and holds the IO contract and verification contract: inputs / outputs / semantic_dependency / raw_requirements / tests / predicates
  inputs:
    - name: "<name>"
      shape_expr: "<scalar | [d1,d2,...] | (d1,d2,...)>"
      evidence_ref: "spec.ir.yaml | raw/state_snapshots | ..."  # required non-empty string, same form as outputs[].evidence_ref. A CASE input (nx, dx, g, a selector) is evidenced by the IR itself — `spec.ir.yaml`, its value being case.test_case_set[].inputs — and is NOT a snapshot variable; such an entry named by a step's `inputs` is a runtime input's route (V9); only a STATE input (an initial field the kernel holds) references raw/state_snapshots
  outputs:
    - name: "<name>"
      shape_expr: "<...>"
      evidence_ref: "raw/state_snapshots | ..."
      raw_variables: ["<name1>", ...]  # when evidence_ref=raw/state_snapshots, a non-empty array is required
  raw_requirements:
    required_evidence:
      - artifact: "state_snapshots"  # the captures every node has (optional raw/ prefix, case-insensitive; author the canonical bare form)
        required: true|false
        schema:               # required when artifact=state_snapshots
          variables:          # THE STATE (Z6, issue #255; issue #589): each entry is a variable the driver holds per case — every state array and scalar `drive` binds (§`drive` below), each bound by at least one dummy of `drive` — captured with `t` and `step` before the first call (raw/state_snapshots/initial/<case_id>.json, after `drive.init`) and after the last committed call (raw/state_snapshots/<case_id>.json). A quantity the END STATE defines (an error norm, a mass, a symmetry residual, a per-case or run-wide extremum) is evaluated by the host from the captures and is never a schema variable, and a case input (nx, dx, g, ...) is in case.test_case_set[].inputs or `derived`, never a schema variable. On a node whose OWN runner writes the snapshots (the `infrastructure` harness self-test, the `harness` bundle shape) the variables are that self-test's recorded outputs — the sentinel echoes, the perf fields it re-parsed, the flags its `tests.md` §6 names — and that writer is the trust root (A6), so V3 (iv) does not apply there. Every name is a bindable identifier (`[A-Za-z][A-Za-z0-9_]*`, ≤ 60 chars, pairwise distinct case-insensitively, not `t` / `step` / `case_id`). An entry carries `name` and `shape_expr` only: a `source` key (the retired `operation` / `record` / `reported` / `reference` vocabulary) is a `Compile fail`
            - name: "<name>"
              shape_expr: "<...>"
          time_variable: "<name>"
          time_shape_expr: "scalar"   # MUST be "scalar": the per-snapshot time index is a scalar loop counter the runner always emits as a scalar; "[1]" (or any non-scalar) is rejected at compile and fails post_execute
          coordinates:          # OPTIONAL (Z6, issue #255): the grid axes a predicate expression may name. The STATE's shape is the shape the highest-rank snapshot variables share, and `axis` indexes it: beside `[ncomp, nx, ny]` the `ny` axis is 2. The host derives each coordinate as an array of that shape carrying `count` positions along `axis` (the same value on every other axis), so it pairs only with variables of that rank, never with a lower-rank capture beside them: beside a stacked state write the comparison over the stacked variables, where a coordinate `c` on the component axis, with `count` and `length` both the case's component count (an `inputs.<path>`, so `count` is every case's extent and `c` is i + 1/2), masks a component (on three components `(c - 0.5) * (2.5 - c)` is 1 on component 1, 0 on components 0 and 2) and `roll(<var>, ±1, 0, 0)` brings the neighbouring component into place; `count` must equal the captured extent on that axis, and `count` / `length` are a number or an `inputs.<dotted>` path that resolves to a number in EVERY case; `placement` is `cell_center` ((i + 1/2) * length / count), the one placement the grammar derives
            - {name: x, axis: 0, count: inputs.grid.nx, length: inputs.grid.L_x, placement: cell_center}
      - artifact: "state_history"     # declared iff `drive.loop` is: the full state (every schema variable, `t`, `step`) every `drive.history.every` committed steps, raw/state_snapshots/history/<case_id>__<k>.json (k the committed step); no schema of its own (the state_snapshots one)
      - artifact: "step_series"       # declared iff `drive.loop` is: one entry per committed step, raw/state_snapshots/series/<case_id>.json
        names: ["step", "<v>_min", "<v>_max", ...]   # EXACTLY {step} ∪ {<v>_min, <v>_max for every ARRAY schema variable} (gate (h)); the host-rendered driver folds each extremum over the committed state, a non-finite cell folding to a non-finite entry the host refuses. `t` is not a name of this list and not a grammar name: the harness writer adds the scalar `t`, the time of the last committed state, to the file
  semantic_dependency:
    required_sources: ["<var1>", "<var2>", ...]   # a non-empty string array
  tests:
    # One entry per tests.md test_id (set equality, gated). Validate.execute folds the records of `predicates`
    # into verdict.json in-process: a test's status is its expected_outcome when every predicate of the test
    # that is not cross-target holds, else fail (`tools/verdict_evaluator.py`). Formalize each tests.md §6 / §7 rule.
    - test_id: "<test_id>"                 # exactly the tests.md test_ids, no more/less
      expected_outcome: "pass"             # pass | xfail: its tests.md `expected_outcome:` line (gated); certified if every predicate holds
      target_cases: ["<case_id>", ...]     # non-empty, case.test_case_set case_ids; the cases this test's judgment reads, and every case it reads
      quantities: ["<name>", ...]          # non-empty, distinct (`[a-z][a-z0-9_.]*`): EXACTLY the names of the `quantities:` line of the test's tests.md definition (gated)
  predicates:
    # Z6 (issue #255), issues #582 and #589: the HOST-EVALUATED judgment of each test, and all of it. A
    # predicate is an arithmetic expression over the driver's captures — `initial.<var>` (before the first
    # call, after `drive.init`) and `final.<var>` (after the last committed call), each schema variable plus `t`
    # and `step` (the driver's time and committed step count), and on a `loop` node `history.*` / `series.*` —
    # plus the case's own `inputs.<dotted>` numbers, its `derived.<name>` values and the `coordinates` above,
    # valued by the host (`tools/primary_evidence.py`) with no generated code in the path but the captured values.
    # verdict.json records each one under `per_test[].basis.predicates[]`. The set of `quantity` over a test's
    # predicates that are not cross-target equals its `quantities` (gated, D10), and a quantity may have several
    # predicates. Every entry READS STATE, in `expr` or a `bind` that `expr` reaches: `final.<v>` of a variable a
    # `drive` entry writes (an `out` / `inout` dummy), `initial.<v>` of a
    # variable `drive.init` writes, or `history.<v>` / `series.<v>_min` / `series.<v>_max` of a variable a `drive`
    # entry writes. `final.t`, `final.step`, `initial.t`, `initial.step`, the driver's counters `history.t`,
    # `history.step` and `series.step`, a logical `out` state `<f>` of `step` or `init`
    # (a flag, `result:` or not), `initial.<v>` of a variable the host filled from
    # `initial_state`, `final.<v>` of a variable nothing writes (and its history and series), `derived.*` and
    # `inputs.*` value nothing the kernel produced, and an entry that reads only
    # them is refused — a SYNTACTIC rule, under which a read that sits only inside a term multiplied by a literal
    # zero, raised to a literal zero or subtracted from itself or added to its own negation (spelled the same) is
    # no read (`sum(final.u * 0 + 1)` is constant, issue #483). Exempt, per case over all
    # the test's targets: gate (l)'s closure (below: a `loop` node's `pass` test, the accepted ref, `quantity:
    # run.n_step`), one per test, and a FLAG PREDICATE, one per point and `<f>` per test: `expr` exactly `final.<f>`
    # or `initial.<f>` (`<f>` written in every target case at that point; `initial.` for one `init` writes),
    # `op: eq`, `value` 0 or 1, no `bind`, `quantity: <f>`. It values its cases only on an `xfail` test with `value: 0`.
    # Whether an entry's value depends on the state in any other way, and is the test's own quantity, is V3's judgment. On an `expected_outcome: xfail`
    # test the entries state, in the failure direction, what the guard leaves behind (the state did not advance,
    # `final.guard_pass == 0`), and must hold for the test to certify `xfail`; `expected_outcome`
    # is no predicate key.
    # THE ONE-CALL SCALAR PAIRING (gate (i), re-checked by evaluate_verdict): a ONE-CALL SCALAR is the scalar state a rank-0 real / integer `out` of a `step` entry is
    # bound to on a node without `loop`. Each predicate that reads `final.<s>` of one needs ANOTHER predicate
    # of the same test and quantity that reads `final.<s>` in every case the first reads it, one of the two also
    # reading `final.<a>` of an array state the same operation writes in each of those cases: a predicate never
    # pairs itself (`final.s + 1e-300 * sum(final.F)` alone is refused). The gate is syntactic: it closes that
    # sole-predicate spelling only, and whether the pair's array term carries weight (`final.s` beside
    # `final.s + 1e-300 * sum(final.F)` passes it) is Compile.verify V3's judgment.
    # A judged quantity no captured state variable enters — one tests.md defines over the case inputs, the
    # constants and the time alone (a CFL number over a prescribed speed, a step count the inputs fix) — has no
    # faithful state-reading transcription. For such a quantity tests.md §5 STATES the definition with its state
    # term (`docs/TESTS.md` requirement 9): an expression over the captured state and
    # the declared inputs, with the state fact it rests on. The entry transcribes that statement. Where tests.md states none, the phase cannot be completed from its inputs:
    # `Compile.generate` declares a `Compile fail` (§1-1) whose `compile_fail` names `tests`, §5 and the
    # quantity, and a term the leaf supplies in its place — a bound of its own, or a state read whose
    # only role is to pass the rule above — is not a transcription. "A captured state variable" in this rule is
    # a read the reads-state rule above counts. Four things are NOT such a quantity. A quantity a captured
    # state variable enters is not, however much of it the inputs fix (a CFL number over a wave speed the state
    # carries): transcribe its definition. Nor is `run.n_step`: its entry is gate (l)'s closure, exempt above,
    # and needs no §5 state term. Nor is a flag: its predicate is exempt above. And the `xfail_condition` of a guard test over the inputs (`nx < 2`,
    # `dt <= 0`) is not a judged quantity: the test judges the kernel's refusal, and its entry states the state
    # fact the guard leaves behind, as above, whether or not tests.md spells that fact out. An xfail test that judges such a
    # quantity (a CFL guard whose instability is seeded at round-off only, so the state stays bounded) takes
    # the statement too, which says so.
    # A CROSS-TARGET judgment (tests.md "The cross-target judgment is applied", issue #324) is transcribed as one
    # entry per variable it names: `expr` compares this run's `final.<var>` with
    # `comparand.final.<var>` — the same capture, in the same case, of another target's eligible run of this
    # node, which the host selects — by the tolerance and normaliser the judgment states. Its
    # `quantity` is the name tests.md gives the agreement and it is outside the `quantities` set; the --stage
    # compile gate requires one under every test whose tests.md definition carries that sentence and refuses
    # one under any other test. No verdict evaluates it: the host's cross-target agreement does, and a
    # disagreement stops every target.
    - test_id: "<test_id>"                 # a tests.md test_id (⊆ the tests set)
      quantity: "<name>"                   # the judgment the expression evaluates: a member of its test's `quantities`
      target_cases: ["<case_id>", ...]     # EXACTLY the target_cases of this test's `tests` entry (set equality, gated)
      per_case: true                       # EXACTLY ONE of: per_case: true (evaluate in every target case) / case: "<case_id>" (evaluate in that one case; how a cross-case reduction is read)
      bind:                                # optional named sub-expressions, evaluated in order; each may reference only the ones before it; a bind expr does not reach (directly or through a later bind) enters no value: it adds no read to `cases_read` or to the reads-state rule, while it is still parsed, name-resolved, ranked and evaluated in every case the scope evaluates, and a `comparand.` read in it still makes the entry cross-target
        dA: "(inputs.grid.L_x / inputs.grid.nx) * (inputs.grid.L_y / inputs.grid.ny)"   # spell every inputs.<path> as THIS node's cases declare it (a path that is not a number — or a rectangular numeric list — in every case its scope evaluates is refused)
        M0: "sum(initial.h) * dA"
        M1: "sum(final.h) * dA"
      expr: "abs(M1 - M0) / max(abs(M0), 1e-14)"   # must evaluate to a finite SCALAR; the normaliser is the one tests.md defines
      op: "le"                             # eq | ne | le | ge | lt | gt
      value: 1.0e-10                       # a FINITE number, OR {per_case: {<case_id>: v}} with per_case: true
  # predicate grammar (closed; `tools/primary_evidence.py` GRAMMAR_VERSION 6 — gated at --stage compile
  # by parse, name resolution and operand ranks; what the gate cannot see — the captured arrays' extents, a division by zero —
  # is an evaluation error at Validate.execute, recorded on that predicate, never a crash):
  #   operators   + - * / ** and unary -; numeric constants; `pi`, `e`; a tree at most 64 levels deep
  #   names       initial.<var> / final.<var> (a snapshot schema variable, `t` — the driver's time at that capture,
  #               `t_start + step * dt` on a `loop` node — or `step`, the committed step count; the case's end time as
  #               DECLARED is inputs.time.t_end or whichever input the case carries, and a reference field is evaluated
  #               at the declared value);
  #               history.<var> (the `state_history` samples of a schema variable stacked on a LEADING axis, rank
  #               state + 1; `history.t` / `history.step` rank 1) and series.<name> (a `step_series` name, rank 1 over
  #               the committed steps 1..n): a `loop` node's, each loaded only when a predicate names it, either of
  #               length 0 (`every = 0`, or a first step refused); a coordinate broadcasts against a history.* operand
  #               on its trailing axes;
  #               derived.<name> (the case's `derived` value, host data, a scalar or an array);
  #               inputs.<a>.<b> (a NUMERIC case input: a number, or a rectangular nested list of numbers of rank
  #               1 to 4 read as an array of that shape — a declared sentinel field a capture is compared against;
  #               a string, a boolean, a mapping, an empty or ragged list, or a list holding a non-number is rejected;
  #               resolved in every case the scope evaluates — every target case under per_case, X under case: X — so
  #               an input only some target cases declare is read under `case:` or as at('<case_id>').inputs.<a>.<b>);
  #               <coordinate name> (a `coordinates[]` axis); a `bind` name;
  #               at('<case_id>').initial.<var> / at('<case_id>').final.<var> / at('<case_id>').inputs.<a>.<b> /
  #               at('<case_id>').<coordinate name> — the same values read in another case of THIS predicate's
  #               target_cases (a cross-case reduction: the coarse case's state, inputs and coordinates); the
  #               case_id is written exactly as case.test_case_set declares it;
  #               comparand.initial.<var> / comparand.final.<var> (grammar 3) — the same capture, in the same case,
  #               of the COMPARAND: another target's eligible run of this node, selected by the host. A predicate
  #               that reads one is a CROSS-TARGET predicate: it must read this run's own state too, and the
  #               cross-target agreement evaluates it once per comparand in every case it reads
  #   functions   sum, mean, min, max (one argument reduces; two to eight are elementwise), smax, smin, ssum (reduce
  #               every axis but the leading one, identity on rank 1: per-sample extrema of a history.* operand), abs, sqrt, exp, log,
  #               log2, sin, cos, norm2 (sqrt of the sum of squares), maxabs, ceil, floor,
  #               atan2(y, x) (elementwise, in [-pi, pi]; grammar 4 — a discrete mode's phase),
  #               roll(a, s0[, s1, ...]) (one INTEGER shift per axis, rank ≤ 4; a non-integer shift is an evaluation error),
  #               transpose(a) (axes reversed) / transpose(a, i, j) (axes i and j exchanged: [ncomp, nx, ny] → [ncomp, ny, nx]
  #               by transpose(a, 1, 2)), flip(a, k) (axis k reversed; k an INTEGER, 0 the leading axis),
  #               coarsen(a, f) (block mean over f cells along every axis; f an INTEGER ≥ 1 dividing every extent) — grammar 5
  #               — positional arguments only; the names are exactly the keys of `FUNCTIONS` in that module
  #   refused     a subscript or slice, a comparison, `and` / `or` / `not`, a conditional, a lambda, a
  #               comprehension, a keyword argument, unary +, a string anywhere but at('...'), any other function or
  #               attribute root; a `coordinates[].name` or a `bind` name that is a grammar name (pi, e, sum, initial, ...);
  #               comparand.inputs.<path> and comparand.<coordinate name> (the comparand shares this IR, so both are
  #               this case's own), and `comparand` combined with at('<case_id>') in either order
  #   operands    a scalar pairs with anything; two arrays pair only at equal rank with every extent equal or 1 on
  #               one side (a [nx] array never pairs with a [nx, ny] one, and the 32×32 state of one case never
  #               pairs with the 64×64 state of another — a cross-case comparison reduces each case to a scalar
  #               first, or brings the two states to one shape: roll, transpose or flip the base's (a symmetry pair),
  #               or coarsen the finer of the two by the extent ratio). A coordinate carries the state's shape, so a field
  #               built from one reduces over every cell; a reduction over a field with an extent-1 axis (a
  #               coordinate the host could not expand, because the captured arrays of the state's rank disagree
  #               on shape) is refused until the field is paired with a state array; a captured array's own extents
  #               reduce, one cell included; a state with no cell does not
  #   errors      a non-finite intermediate (a division by zero, a literal `1e400` included), an unpaired operand
  #               extent, a capture file absent / ragged / non-numeric / non-finite / of the wrong rank, a variable the
  #               case's capture does not hold, a flip / transpose axis outside the rank or a coarsen factor that does
  #               not divide (the gate checks ranks, not these values), an `initial.<var>` of a node whose own runner writes no `initial/`
  #               capture (a harness self-test: its predicates read `final.<var>` and `inputs.<path>`), a non-finite
  #               history / series entry, a reduction over an EMPTY history.* or series.* operand — each is a
  #               STRUCTURAL failure of that predicate at Validate.execute (of a cross-target one: an unevaluable
  #               agreement); a comparand variable whose shape differs from this run's is a DISAGREEMENT, not structural
  # worked translations of tests.md prose (the fidelity V3 reads):
  #   positivity            expr: "min(final.h)"                                  op: ge  value: 0.05   (the END-STATE minimum)
  #   run-wide extremum     expr: "min(series.h_min)"                             op: ge  value: 0.05   (the host's fold over EVERY committed state of a `loop` node, beside the end-state predicate above)
  #   sampled CFL           expr: "max(smax(derived.dt * (abs(history.hu / history.h) + sqrt(inputs.constants.g * history.h)) / derived.dx))"  op: le  value: 1.0   (a per-sample index over the `every`-step samples, `smax` per sample, `max` over samples; the same over final.* covers the last state)
  #   zero-step closure     quantity: run.n_step  expr: "final.step - derived.n_step"   op: eq  value: 0      (gate (l) on a `loop` node; without `loop`: `final.guard_pass` eq 1 under `quantity: guard_pass`)
  #   harness sentinel      case: "l0_array_emit_pass"  expr: "maxabs(final.a2 - inputs.initial.a2)"   op: le  value: 1.0e-12   (a self-test's emitted array against the sentinel the case DECLARES, re-parsed by the host: the emitter round-trip judged from the raw file)
  #   lake at rest          expr: "maxabs(final.h - initial.h)"                   op: le  value: 1.0e-12
  #   analytic agreement    bind: {c0: "sqrt(inputs.constants.g * inputs.initial.H_0)", h_ref: "inputs.initial.H_0 + inputs.initial.eta0 * sin(2 * pi * (x - c0 * inputs.time.t_end) / inputs.grid.L_x)"}  (c0 is written before h_ref, which reads it; the reference is evaluated at the DECLARED end time, and every input path is spelled as this node's cases declare it — tests.md §5 is the formula's source, a shift or a second axis included)
  #                         expr: "norm2(final.h - h_ref) / norm2(h_ref)"          op: le  value: {per_case: {...}}
  #   convergence order     case: "<the finer case>"  bind: {xc: "at('<coarse>').x", hc: "<h_ref written over xc and at('<coarse>').inputs...>", e_c: "norm2(at('<coarse>').final.h - hc) / norm2(hc)", e_f: "<the same over final.h, x, inputs...>"}  expr: "log2(e_c / e_f)"  op: ge  value: 0.8
  #   no-state quantity     bind/expr: the tests.md §5 primary-evidence statement (`docs/TESTS.md` requirement 9) written term for term — its input term over
  #                         `inputs.<path>` / `derived.<name>`, the time step as the statement fixes it (the `derived` or input value `drive.loop.dt` binds), and ONLY
  #                         the state term the statement names; op and value are the judgment's (`gt` on an xfail guard). With no
  #                         statement there is nothing to write here: that is a `Compile fail` (the schema block above)
  #   translation pair      case: "<shifted>"  expr: "norm2(final.h - roll(at('<base>').final.h, inputs.initial.shift_x_fraction * inputs.grid.nx, 0)) / norm2(final.h)"
  #   cross-target agreement  quantity: "cross_target_state_agreement"  per_case: true  (one entry per variable the judgment names, over its test's target cases)
  #                         expr: "maxabs(final.u - comparand.final.u) / max(maxabs(final.u), maxabs(comparand.final.u), 1.0)"  op: le  value: 1.0e-12
  #                         (the normaliser is the one tests.md states; this one is symmetric in the two variants, so which of them is the comparand does not decide the agreement)
  # cases_read: the cases a predicate reads are the cases its captured STATE reads
  #   (the state reads the rule above counts: never the driver's time or step count, nor their history.* / series.*) that ENTER ITS VALUE name — in expr, or in a
  #   bind expr reaches (directly or through another bind), outside a term constant by form (the forms the
  #   reads-state rule above prunes): a read of its own case's state reads every case the scope evaluates
  #   (every target case under `per_case: true`, X under `case: X`), and an at('<case>').final.<var> read reads
  #   that case. An at('<case>') in a bind expr never uses, or multiplied by a literal zero,
  #   reads nothing, and neither does an at('<case>').inputs.<path> normaliser, at('<case>').final.<time variable>
  #   or a coordinate of that case (none of them is that case's state), nor a `comparand.` read (another
  #   target's state).
  # predicate scope: `per_case: true` holds in EVERY target case (a {per_case: {<case_id>: v}} value map when the
  #   threshold varies per case covers every target case). `case: <case_id>` evaluates in exactly ONE target case,
  #   how a CROSS-CASE reduction is read (a convergence order at the finer case, a symmetry residual at the
  #   transformed case), reading the others through at('<case_id>').

dependency:
  # LLM-authored: ONLY node_key + direct_deps (the host's directly-required set + semantic `operations`).
  # Do NOT author all_nodes / transitive_deps — the conductor derives them (sidecar, below).
  node_key: "<spec_kind>/<spec_id>@<spec_version>"
  direct_deps:
    - node_key: "<spec_kind>/<spec_id>@<spec_version>"
      kind: "<component|problem>"   # never `profile` (not a node) and never `infrastructure`: the
                                     #   runner harness is the target's, not a dependency of the node
      operations: ["<operation_id>", ...]   # `component/` dep: non-empty (each called `<dep_spec_id>__*` op),
                                             #   pinned by gate _validate_component_dep_operations (V4c-i).

public_api:
  # This FULL shape (signatures + module_parameters; plus `interfaces`, each `{name, signature}`,
  # when §5.1 declares named procedure prototypes — see the authoring bullet) is required on EVERY
  # node: `infrastructure`, `component` (since issue #153) and `problem` (since issue #589, which
  # publishes `<spec_id>__step` and optionally `<spec_id>__init`) (V8).
  # The COMPLETE published surface controlled_spec §5 declares ("operation_ids are exactly: ...") —
  # every operation incl. helper emitters/writers no test exercises directly — so Generate
  # publishes all of them and the runner CALLS them, not reimplements. The --stage compile gate
  # (_validate_published_surface) pins these sets == §5 (V8).
  published_operations:
    - operation_id: "<spec_id>__<name>"   # every §5 operation_id (set equality with §5)
      exercised_by: ["<test_id>", ...]    # optional: tests exercising it ([] for a helper)
  published_types: ["<spec_id>__<type>", ...]   # every §5 published derived type (set equality)
  module_parameters:                      # copy each §5.1 module_parameters entry, value included (V8)
    - {name: dp, base: integer, value: float64}         # {name, value} (base: integer optional); gate
    - {name: case_id_len, base: integer, value: 64}     # pins == §5.1 by value (leaf's only carrier)
  signatures:                             # copy §5.1 structured signatures, one {symbol, signature} each
    - symbol: "<spec_id>__<name>"         # every §5.1 op AND type (set equality with §5.1)
      signature:                          # structured MAPPING, NOT source text: a procedure
        kind: subroutine                  #   {kind, name, args, result?} or a type {name, components};
        name: "<spec_id>__<name>"         #   entity {name, rank?, intent?, spec:{type, kind?, len?,
        args:                             #   name?, alloc?}}. Generate renders it to the target language
          - {name: a, intent: in, spec: {type: real, kind: dp}}   #  (phase_02 §2-1); the gate
          - {name: b, intent: in, spec: {type: string, len: case_id_len}}   # pins == §5.1 (V8).

drive:
  # REQUIRED on a `problem` / `component` node, REFUSED on an `infrastructure` one (issue #589). The kind
  # is the one spec/registry/spec_catalog.yaml records for `meta.spec_id`: an IR the catalog cannot answer
  # for, or whose `meta.spec_kind` disagrees with it, is refused. `drive` is the contract the host-rendered
  # DRIVER of one case is rendered to: every schema variable is a
  # state the driver allocates to the extents its bound dummies' §5.1
  # `dims` give (an array no entry running the case binds: its `shape_expr`, each token resolved through
  # ANY entry's rank-0 dummy of that name against this case's inputs — none, or two that disagree, is
  # refused) and fills from `initial_state` (a logical state, a `calls` or `record` state of a procedure binding, and every variable `init` writes
  # start at 0); it calls `init` once, captures `initial/`, calls `step` once (no `loop`) or up to `loop.n_step`
  # times, and captures the final state. A refusal is a `false` result, never a stop: a false `init`
  # refuses the case (both captures still written); on a `loop` node a step is COMMITTED only on a true
  # result (the state copied back, `step` advanced, `t = t_start + step * dt`, the series entry appended,
  # a sample written every `every` steps), an `out` array starts each call NaN-filled (a committed cell the
  # call leaves unwritten folds to a non-finite series entry, and a predicate that reads it is refused), and a false result ends the loop on the last committed state;
  # on a node without `loop` the one call runs on the states themselves, each `out` starting at its
  # `initial_state` content, and `step` becomes 1 only on a true result. After every call each logical
  # `out` is written into its state as 1 / 0.
  init:                                    # optional; ONE entry, never a list
    operation: "<spec_id>__init"
    result: "<a logical out dummy>"
    arguments: {"<dummy>": "<binding>"}
  step:                                    # one entry, or a LIST of entries each adding `cases: [<case_id>, ...]`
    operation: "<spec_id>__step"           # a problem's `__step`; a component's published operation
    result: "<a logical out dummy>"        # (d) the operation's input-guard logical
    arguments: {"<dummy>": "<binding>"}
  loop:                                    # optional; absent = ONE call per case
    n_step: "<ref>"                        # <ref> = inputs.<path> | derived.<name> | state.<var> (a scalar `init` writes)
    dt: "<ref>"
    t_start: "<ref>"
    t_end: "<ref>"                         # required with `loop` (e)
  history:                                 # REQUIRED with `loop`, refused without it
    every: "inputs.<path>"                 # an inputs reference only; 0 means no samples
  # BINDINGS (an `arguments` value): "inputs.<dotted path>" (the case input, a nested list for an array),
  #   "derived.<name>", "state.<var>" (a schema variable), "time" (the driver's t), "step" (the committed
  #   step count), a number or boolean literal, or a PROCEDURE BINDING for a dummy whose type is `procedure`
  #   and for no other:
  #     {procedure: "<expression>", calls: "state.<scalar>", record: {"<prototype in dummy>": "state.<rank-1>" | ["state.<var>", ...]}}
  #   The host renders a procedure of the prototype the dummy's `interface` names. It assigns the prototype's
  #   one real array `out` elementwise from the expression, which reads only the prototype's rank-0 real and
  #   same-rank real `in` dummies, `inputs.*` and the constants, and calls only abs, sqrt, exp, log, sin, cos,
  #   min, max. `calls` (optional) names a scalar state the driver counts the calls into. `record` (optional)
  #   stores what the operation passed at its first calls: a rank-0 dummy into ONE rank-1 state, one entry per
  #   call; an array dummy into a LIST of states of its rank, one per call. The `calls` and `record` states are
  #   the driver's: they start at 0, take no `initial_state` entry, and count as bound and as written by
  #   the entry.
  # BY DUMMY INTENT, gate (b): `in` takes any binding but a procedure binding, an array dummy an array value; `inout` is
  #   an array bound to "state.<var>" (a rank-0 `inout` is refused); an `out` array binds "state.<var>"; a
  #   logical `out` binds "state.<var>" of a `scalar` schema variable (a LOGICAL STATE, no `initial_state`
  #   entry); a rank-0 real / integer `out` of `init`, or of `step` on a node WITHOUT `loop`, binds
  #   "state.<the dummy's own name>", and of `step` on a `loop` node is refused (a kernel scalar is not a
  #   per-step fact; a run-wide extremum is the host's fold over series.* / history.*). Every dummy of each
  #   bound operation is bound exactly once, by no key that is not a dummy; a state array has at most one
  #   reading (`in` / `inout`) and one writing (`out` / `inout`) dummy per entry, and on a node without
  #   `loop` an `in` / `out` pair binds two DIFFERENT states; on a `loop` node a pair binds ONE state, or
  #   each step starts again from the initial state, with no gate to say so (the host cannot tell
  #   a pair from two unrelated arrays): the reviewer judges it. Every schema variable is bound by a dummy of
  #   `drive`.
  # CLASSES ARE PER CASE: for case `c` they follow from `init` and the `step` entries that run `c` (one
  #   entry on the mapping form; the entry whose `cases` lists `c` on the list form). A predicate reads
  #   kernel state in `c` only through a variable an entry running `c` writes (`final.<v>`, its history and
  #   series), or `init` writes (`initial.<v>`); it must read some in EVERY case its scope evaluates, and every
  #   target case of a test must be valued by a read that is kernel state in THAT case, or on an
  #   `xfail` test by a flag predicate with `value: 0`.
  # GATES (Compile.static, primary_evidence.drive_violations; each a `Compile fail`): (a) a `problem`
  #   publishes `<spec_id>__step` and every `step` entry names it; (b) the intent rules above, and the
  #   procedure binding as stated above; (c) each array
  #   dummy's `dims` (a dims-less one: its state's `shape_expr`) EVALUATE PER CASE under the entry's bindings
  #   — a token naming a rank-0 dummy bound to an input, a `derived` value or a literal, or naming a scalar
  #   `derived` value, resolves, `nx + 1` arithmetic included, any other token is refused; an extent of 0 only on a case only `xfail` tests target
  #   (the flag judges it); an array no entry running the case binds: as the
  #   driver block states — every dummy bound to one state gives the same
  #   extents, a numeric `shape_expr` extent agrees, and an array input or `derived` value has its dummy's
  #   extents; (d) `result` above; (e) `loop` references resolve (`state.<var>` only to a scalar `init`
  #   writes), `t_end` is declared, a rank-0 `step` dummy named `dt` binds `loop.dt`'s reference and one
  #   named `t` binds `time`; (f) `initial_state` as the `case` block states, under each case's classes, a `calls` or `record` state
  #   of a procedure binding taking no entry; (f') a `step` list's
  #   `cases` partition `test_case_set` exactly once, and a `loop` node has one mapping entry; (g) the
  #   `initial_state` / `derived` name rules of the `case` block, checked statically; (h) `step_series` and
  #   `state_history` as their `required_evidence` entries state; (i) the one-call scalar pairing
  #   (`predicates`); (j) the `predicates` rules, unchanged; (k) every variable `init` writes is read by a
  #   predicate over `initial.<v>`, and for a scalar that `loop.n_step` / `loop.dt` references, that predicate also
  #   reads `initial.<a>` of an ARRAY `init` writes; (l) THE ZERO-STEP CLOSURE, matched by its TEXT after
  #   whitespace normalisation: on a `loop` node every `pass` test carries `quantity: run.n_step`,
  #   `expr: "final.step - <ref>"`, `op: eq`, `value: 0`, <ref> the counterpart of
  #   `loop.n_step`'s binding and nothing else: that `derived.<n>` / `inputs.<path>` verbatim, or, for
  #   `n_step: state.<n>` (a scalar `init` writes), `initial.<n>` or `final.<n>` — and then also the `init`
  #   result's flag eq 1 (a refused `__init` planning 0 steps satisfies `0 - 0`); on a node without `loop`
  #   every `pass` test carries the `step` result's flag eq 1 (ONE result its cases' entries share); each
  #   over ALL the test's target cases, under its fixed quantity (`run.n_step`, the result's name), which
  #   the test's tests.md `quantities` line lists; (m) a `loop` node's tests.md carries ONE
  #   `history:` line (docs/TESTS.md §Description format) whose input name is `history.every` without
  #   `inputs.` and whose number each case's input equals; a node without `loop` carries none. The host also
  #   EVALUATES every case (primary_evidence.evaluate_case_inputs) and refuses a non-finite value, a shape
  #   mismatch, a host-data `loop.n_step` that is not an integer >= 1, a host-data `loop.dt` <= 0, an `every`
  #   that is not an integer >= 0, and, when all four `loop` references are host data,
  #   |t_start + n_step * dt - t_end| > 4 ulp(t_end); a case only `xfail` tests target may carry
  #   an invalid dt, the kernel's refusal being its subject (both checks spare that dt alone). No gate parses a threshold.
```

### `<ir_ref>/dependency_graph.json` sidecar (conductor-authored)
The conductor writes this at Compile phase start (`workflow_conductor._write_dependency_graph`)
from `deps.yaml` + `spec_catalog.yaml` — a leaf-non-writable managed path (like
`compile_static_meta.json`; no `operations`):
```yaml
{ "node_key": "<kind>/<id>@<v>",
  "all_nodes":       [{"node_key": "...", "topo_level": <int>}, ...],   # incl. self; topo_level = height
  "transitive_deps": [{"node_key": "...", "via": ["<intermediate>", ...]}, ...],
  "profiles":        [{"node_key": "profile/<id>@<v>", "profile_id": "...", "profile_version": "...",
                       "version_constraint": "...",
                       "components": [{"component_id": "...", "version_constraint": "..."}]}, ...],
  "generated_by": "conductor" }
```
The host directly-required set is `{all_nodes} − {self} − {transitive_deps}`; the `--stage compile`
gate cross-checks it against the IR's `direct_deps`.

`profiles[]` records the `profile` this node ADOPTS. The profile is never in `all_nodes` — the host
resolves it, it is not a certified node (issue #175) — while every `component` it SELECTS is, as an
ordinary member of the directly-required set. `components[].version_constraint` is the profile's
own declaration; the version each resolved to is in `all_nodes` alone. The key is always present
(`[]` when none is adopted); its ABSENCE means a pre-#175 sidecar and is a `Compile fail`.

### `shape_expr` allowed forms
`spec/schema/ir/shape_expr.schema.json` is the canonical source. Limited to the 3 forms `scalar` (case-insensitive) / `[d1, d2, ...]` / `(d1, d2, ...)`. Function-call notation such as `vector(N)` / `matrix(M,N)` / `tensor` is forbidden and is a `Compile fail`. Each dimension token `d_i` is itself limited to an **unsigned integer literal or an identifier**: arithmetic (`[nx + 2*ng]`, `[n-1]`), decimals (`[3.0]`), and signs (`[-3]`) are all a `Compile fail`. For a computed extent, introduce a named dimension symbol (`nx_total`) and write that name as the token.

### `algorithm.steps[].inputs` and `algorithm.steps[].outputs`
A list of non-empty strings (e.g. `["U_L", "U_R"]`); the object form (`[{name: ..., source: ...}]`) is forbidden.

Every token in either list is **traceable** to one of three places: a direct input/output variable of `controlled_spec.md`, an intermediate variable declared in `temporaries`, or a derived quantity of `derived_field_rules`. A name obtained by a slice, an alias, or a component decomposition (`h_L` from `U_L[0]`) is declared in `temporaries` or `derived_field_rules` so that its provenance is explicit. A token that maps to neither a direct name nor a derived one is a `Compile fail` (`undefined binding`), because the dataflow then cannot be traced from `spec.ir.yaml` alone. **`algorithm.state_variables` is NOT one of the provenance sources**: it serves the state contract, not the dataflow trace, so a name carries provenance only through an `io_contract` `inputs` / `outputs` entry, a `state_snapshots` `schema.variables` entry, `temporaries`, or `derived_field_rules`. A prognostic field usually qualifies through the snapshot schema; one that does not — a node that snapshots it under another name, or not at all — is an undefined binding whatever `state_variables` says, and needs a `temporaries` / `derived_field_rules` declaration.

### `algorithm.execution_mode`
Only `sequence` / `conditional` / `iterative` / `columnwise` are allowed.

#### Decision Criteria
Select by the algorithm's **top-level** control structure. Evaluate in this order and take the first match:
1. **`iterative`** — the top level repeats a body that CARRIES STATE FORWARD, advancing a loop counter (time index, iteration index) until a stop condition holds. **Any time marching over `n_step` is `iterative`**, never `sequence`, however few steps the body has — a loop one call of the operation runs; the driver's `drive.loop` is not one (below). Branches inside the body do not change the mode, and neither do the step kinds it contains: **a `column_process` step inside a time loop leaves the node `iterative`**, because the time loop is the top level. Author the loop in `iteration_contract` (loop counter + `stop_condition`) and describe it in `control_condition`.
2. **`columnwise`** — the top-level structure IS an independent per-column sweep: each column is processed without carrying state to the next, so the sweep order does not change the result. `steps[]` holds at least one `column_process` step. Repetition alone does not select this mode — rule 1 already claimed every repetition that carries state, and a `column_process` step is legal under any mode.
3. **`conditional`** — the top level branches or dispatches, and the branch is stated in a non-empty `control_condition`. A dispatch over a STATIC case list (the harness `case_loop` shape) is `conditional`, not `iterative`: its `iteration_contract` describes the dispatch (`kind: case_loop`, `iterate_over`, `termination`) and carries neither a loop-counter key nor `stop_condition`.
4. **`sequence`** — a fixed composition of steps that runs once. A fixed stage composition (SSPRK2's 2 stages, `kind: fixed_stage_composition`) is `sequence`, not `iterative`: the stage count is fixed by the scheme, not decided by a stop condition.

**`algorithm` lowers ONE call of each operation `drive` binds, not the driver** (issue #589). On a node whose `drive` declares `loop`, the time loop is the driver's: `__step` advances the state by ONE committed step, so `algorithm` describes that step — a fixed stage composition is `sequence` with `iteration_contract: {}` — and `n_step`, `dt` and the stop rule are `drive.loop` and `derived`, never an `iteration_contract`. Reference form: `docs/examples/spec_ir_algorithm_2d_problem_contract.example.yaml`.

**Scope: `algorithm.iteration_contract` describes the TOP-LEVEL loop only.** A step that converges internally (`step_kind: iterative_solve`) does not make the algorithm `iterative`, and its convergence criteria (tolerance, iteration cap, divergence policy) are lowered in that step's own fields / `invariants` under the V2 IR-self-sufficiency rule — NOT in `algorithm.iteration_contract`, which states what the WHOLE algorithm does. The deterministic gate recognizes one spelling of this mistake and only under a non-repeating mode — a loop counter together with `stop_condition` under `sequence` / `conditional`, per the couplings below. The same keys under `iterative` or `columnwise` are not flagged at all, and an inner solve's criteria authored here in any other wording (`tolerance` + `max_iterations`, say) are not flagged under any mode. Everything the gate does not reach is a `Compile.verify` V2 finding.

#### Mode ↔ contract couplings
Each of the four rules below is enforced by the `--stage compile` gate and is a `Compile fail` routed to `Compile.generate`:
- `execution_mode=conditional` ⇒ `control_condition` is non-empty.
- `execution_mode=columnwise` ⇒ `steps[]` holds at least one `column_process` step.
- `execution_mode=iterative` ⇒ `iteration_contract` is not an empty object.
- `execution_mode` other than `iterative` / `columnwise` ⇒ `iteration_contract` carries no loop counter (`loop_variable` / `counter` / `index_variable`) together with `stop_condition`. That pair describes a repeated body, which `sequence` and `conditional` deny — and an ABSENT or misspelled `execution_mode` fires this rule too, alongside the enum violation, because the contract is loop-shaped whatever the missing mode was going to say. The remedy is to keep the loop fields and take the mode the Decision Criteria give for that repetition — `iterative`, or `columnwise` for an independent per-column sweep — or, when the algorithm truly runs once, to drop the loop-counter/`stop_condition` keys. `iterative` and `columnwise` both denote a repeated body, so a loop-shaped contract is expected under them and this rule does not apply. A key present with a falsy value (`null`, `""`, `0`, `[]`) declares nothing and does not count toward the pair.

The rule is the KEY PAIR, not the shape of the contract. `{}`, a negative declaration (`{applies: false, rationale: ...}`, `{kind: none, reason: ...}`), a fixed stage composition, and the `case_loop` dispatch shape all pass under `sequence` / `conditional` — because none of them names both a loop counter and a `stop_condition`, not because the shape is exempt. A fixed stage composition that DOES name both (`{kind: fixed_stage_composition, loop_variable: stage, stop_condition: "stage == 2"}`) is a `Compile fail`: state the fixed stage count without a stop condition.

Not a rule, stated to close the direction: a non-empty `control_condition` under `execution_mode=sequence` is **legal**. That field also carries guard narration, and no converse rule is placed on it.

### `algorithm.steps[].step_kind`
Only `boundary_apply` / `reconstruct` / `flux_compute` / `source_term` / `time_integrate` / `column_process` / `pointwise_process` / `iterative_solve` / `filter` / `reduction` / `diagnostic` are allowed.

## `ir_meta.json` required keys
- `attempt_count`, `verification_status`, `last_fail_reason`, `debug_mode`, `context_isolated`
- When `context_isolated=false`, `constraint_reason` is required.
- `artifact_hashes` is the HOST's certification of this phase, never a key you author: a value written inside a leaf window is erased when that window closes, and the host stamps its own at `write-step-result` on a `pass`. Re-authoring `ir_meta.json` without it is correct.
- The host writes this file, and the values are fixed rather than chosen: `ir_id` and `node_key` identify the artifact, `context_isolated` is `true` by construction (the leaf sees only its seeded context), `debug_mode` follows the run's `dev` mode, and `attempt_count` is the number of LAUNCHES the substep that wrote the file made — the producer's for the producer's write, the reviewer's for the reviewer's. `Compile.generate` writes `verification_status: "pending"`: the contract requires a non-empty string, and every dependency-certification reader compares against `"pass"` exactly, so a producer must not leave its own artifact reading as certified. `Compile.verify` then re-authors the file from its verdict.

## `ir_id` format
- Format: `<slug>_<YYYYMMDD>_<seq3>`
- `slug` is a short readable token derived from `spec_id`. Hyphen-separated alphanumeric.
- Regex: `^[a-z0-9]+(?:-[a-z0-9]+)*_[0-9]{8}_[0-9]{3}$`

## substep details

### 1-1. Compile.generate substep
- Read the physics algorithm (A) of the `Controlled Spec`, deterministically expand the input conditions and `sweep` / `refinement` from `tests.md`, and generate the 4 sections `case` / `algorithm` / `io_contract` / `dependency` of `spec.ir.yaml`. The IR names no implementation target: the toolchain, hardware, parallel model and harness are the host's target profile (`spec/targets/<target_id>.yaml`, chosen per run after Compile), and the lowering onto them is decided at Generate (the bundle's `target_lowering_plan`). An `impl_defaults` section — which carried both until issue #284 — is refused by the host. On every node additionally author the `public_api` section — the COMPLETE published surface controlled_spec §5 declares (every `operation_id`, incl. helper emitters/writers no test exercises directly, plus every published derived type) AND `public_api.signatures` (each `{symbol, signature}`) copying the §5.1 structured signatures AND — when §5.1 declares an `interfaces` list of named procedure prototypes (issue #266) — `public_api.interfaces` (each `{name, signature}`, the entry itself; omitted when §5.1 declares none) AND `public_api.module_parameters` (each `{name, base?, value}`) copying the §5.1 module_parameters value-included; the gate pins names == §5, `signatures` == §5.1, `interfaces` == §5.1 by name set and argument set, and `module_parameters` == §5.1 by value (V8). A `problem` publishes `<spec_id>__step` and, when §5.1 declares it, `<spec_id>__init` (issue #589).
- On a `problem` or `component` node author `drive` (schema block) and each case's `initial_state` and `derived`: `drive` binds every dummy of the operations it calls, `initial_state` transcribes `tests.md` §2's initial state (an operation-only output takes the §2 `setup` value as a number literal), `derived` transcribes the values `tests.md` §2-§3 define from the inputs (a cell size, the dt rule, the step count), and every `pass` test carries gate (l)'s fixed form. These are the case's HOST DATA: the host evaluates them and the driver hands them to the operation, so no generated code computes an input, an initial state or a step count. An `infrastructure` node authors none of them.
- Author `dependency.node_key` and `dependency.direct_deps[]` (each with `kind` + `operations`) — the HOST's directly-required set, which is `dependency_graph.json`'s `all_nodes` minus this node minus its `transitive_deps`. Read it there, not from `deps.yaml`: a `profile` entry in `deps.yaml` is not a node, and the `component` it selects are what the direct set holds (issue #175). A `component/` dep's `operations` MUST be non-empty (every `<dep_spec_id>__*` operation this node calls) — pinned by the deterministic gate `_validate_component_dep_operations` (V4c-i, below). No `infrastructure` node is in the set: the runner harness belongs to the target the node is built for, and the host adds it to every pipeline of the node (issue #284). Do NOT author `transitive_deps` / `all_nodes`: the conductor derives that closure/topo graph host-side into `<ir_ref>/dependency_graph.json` (a pure function of `deps.yaml` + `spec_catalog.yaml`).
- When the intent of `controlled_spec.md` does not fit the schema during generation, do not extend the schema; instead treat it as `Compile fail`, declare it with `input` `controlled_spec`, the section in `where` and "IR schema insufficiency" with what does not fit in `what`, and stop. For schema extension, separately update the `spec.ir.yaml` schema design by hand, then retry.
- When `tests.md` defines a judged quantity no captured state variable enters (the `predicates` rule of the schema block above says which) and its §5 carries no definition with a state term for it (`docs/TESTS.md` requirement 9), treat it as `Compile fail`, declare it with `input` `tests`, `where` §5, and the quantity and `docs/TESTS.md` requirement 9 in `what`, and stop. Do not supply a term of your own in its place.
- **How a `Compile fail` is declared.** Since Z4 ([issue #171](https://github.com/seiya/atmofab/issues/171)) no leaf writes `ir_meta.json`. The producer writes `compile_fail.json` at its tree root INSTEAD of `spec.ir.yaml`: a JSON object of exactly three keys, `{"input": "<the input at fault>", "where": "<its section>", "what": "<what is absent, self-contradictory or insufficient>"}` — `input` is one of the inputs its launch prompt lists by name; the host then authors `ir_meta.json` with `verification_status: "fail"`, the reason `<input> <where>: <what>`, and the severity §Severity of a finding below gives a finding whose subject is an INPUT — the host reads that section, you do not choose a value here — writes NO `spec.ir.yaml`, and the conductor stops the run `fail_closed` in both workflow modes (`reason_detail` `compile_declared_fail`, issue #355). It is not retried, so it is not the answer to a section that is merely hard to author. A declaration that names no admissible input, leaves `where` or `what` empty, or stands beside a `spec.ir.yaml` is a document violation, repaired and retried like any producer failure ([issue #526](https://github.com/seiya/atmofab/issues/526)).
- Author a route (V9) for every runtime input each case declares; a provenance input needs none, and a route is not a snapshot echo (V3).
- **The IR carries no comments.** The host re-serializes `spec.ir.yaml`, so a YAML comment is lost to every later reader. Rationale that a later stage or the reviewer needs belongs in a field the schema already has — a `description`, a `notes`, an `invariants` entry of the section it concerns.

### 1-2. Compile.verify substep
`Compile.verify` self-checks **only the structural invariants**. Semantic correctness is delegated to the `Validate` execution result.

**The scope is V1-V4, V8 and V9, and this section is where it is stated.** All six are below. (V5-V7 checked the IR's `impl_defaults`, which the IR no longer carries — issue #284; the numbers are not reused.) Nothing narrower is the scope: no summary of this substep elsewhere in this document, and no sentence in a launch prompt, redefines it. Four of these have no deterministic backstop and are therefore the ones a skipped review actually loses — **V2**'s free-form-math completeness judgment (only its zero-signal presence floor is gated), the parts of V3 that read `tests.md` prose, **V4c-iii** (only a later compile probe catches part of it), and **V9**. A `pass` issued after examining a subset is a wrong verdict, not a partial one.

The required invariant set for the self-check (finalized as a **minimal set**):

#### V1. case coverage
- The `case.test_case_set[].case_id` covers the required cases of all `test_id` required by `tests.md`.
- The `sweep` / `refinement` instructions of `tests.md` are reflected in `case.sweeps` / `case.refinements`.

#### V2. algorithm completeness
- The union of each `step.outputs` set of `algorithm.steps[]` covers the state variables targeted for update by `algorithm.update_semantics` (whose own vocabulary is `target_variables` / `update_order` / …).
- **IR self-sufficiency (the IR is `Generate`'s sole algorithm carrier).** Every `algorithm.steps[].operation_ref` NOT resolved by a `dependency.direct_deps` call — a LOCAL operation the node implements — must be lowered WITH the math a `spec/`-blind leaf needs (defining/update expression + pinning constraints, incl. any form `controlled_spec.md` forbids), in existing fields (`derived_field_rules`, step/operation descriptions, `invariants`); no new schema field. A local op with NO lowering signal (a bare `operation_ref`: no non-structural step field, no `derived_field_rules` entry keyed by a step input/output, no op-name mention in `derived_field_rules`/`invariants`) is a deterministic `Compile.static` presence-floor fail (`_validate_local_operation_lowering` → `Compile.generate`). Above that floor, a thinly-lowered op (name + I/O shape + invariants only, defining math absent) is a `Compile.verify` V2 `fail` remanded to `Compile.generate` — a free-form-math completeness judgment, not deterministically gated (only the zero-signal floor is). Meeting it removed the interim `controlled_spec.md` carve-out on the `Generate.generate` producer (at `pure-10`; phase_02 §2-1).
- `algorithm.ordering` is a valid ordering relation over `algorithm.steps[].step_id` (no cycles, no references to undefined step_id).
- `algorithm.iteration_contract` is not an empty object when `algorithm.execution_mode=iterative`, and conversely carries no loop counter (`loop_variable` / `counter` / `index_variable`) together with `stop_condition` when `algorithm.execution_mode` is `sequence` or `conditional`. Both directions are deterministically gated at `Compile.static` (§`algorithm.execution_mode` **Decision Criteria** is the selection rule they enforce), so only the STRUCTURAL contradiction is caught there. The **semantic** half is a V2 finding this leaf owns: a declared `execution_mode` that contradicts the control structure `controlled_spec.md` describes — a time-marching node declaring `sequence` with an empty `iteration_contract`, which is structurally clean and passes every gate — is a `fail` remanded to `Compile.generate`. On a node whose `drive` declares `loop` the time loop is the driver's, so the converse is the same `fail`: an `algorithm` that lowers `drive.loop` into one call of `__step` (a loop over `n_step` inside the operation).
- **Multi-dimensional `problem` contract.** For a `problem` `node` whose `spec_id` contains `2d` / `3d`, `algorithm` additionally holds the 4 contract keys `state_variables[]` (each with `name` + `shape_expr`), `required_update_paths`, `diagnostics_from_state=true`, and `fallback_policy=fail_closed`, as **direct children of `algorithm`**. `required_update_paths` is a **list of non-empty strings**, each one a `state_variables[].name` (`["h", "hu", "hv"]`); the object form (`[{target: ..., path: [...]}]`) is a `fail`, because `ordering` / `steps[].step_id` already carry the update order.
- **Placement of the multi-dimensional contract (resolution order).** `--stage compile` resolves the contract from the FIRST of these that exists, and validates only that one: (1) `algorithm.state_contract` — **any** mapping wins, even an empty one; (2) `algorithm.update_semantics` — wins if it holds **any** of the 4 contract keys; (3) the direct children of `algorithm`. Author (3) and nothing else: an `algorithm.state_contract` key, or one contract key strayed into `algorithm.update_semantics` (whose own vocabulary is `target_variables` / `update_order` / …), **shadows** the direct children so that they are never read and fail as if absent. Every finding is named `state_contract.<field>` after the RESOLVED contract, not after where the field was authored. Reference form: `docs/examples/spec_ir_algorithm_2d_problem_contract.example.yaml`.

#### V3. io_contract consistency
- Every `io_contract.inputs[]` entry holds a non-empty `evidence_ref` string (same form as `outputs[].evidence_ref`): a CASE input — a grid size, a physical constant, an enumerated selector — is evidenced by the IR itself, `evidence_ref: spec.ir.yaml` (its value is `case.test_case_set[].inputs`, which the host holds), and a STATE input — a schema variable the driver fills from `initial_state` — references `raw/state_snapshots`; an input with a missing or empty `evidence_ref` is a `fail`. An `evidence_ref` (input or output) naming `execution_trace.json` (with or without `raw/`, any case) is a `fail`: no Generate contract produces that file. Route the value by its kind — a per-case value the operation receives is a drive binding — inputs.<path> for a case input, derived.<name> for a value the host computes from the inputs, state.<var> for a state; a value the operation writes is a state_snapshots variable bound to an out or inout dummy, which the driver captures; a case INPUT — an enumerated selector included — is not an evidence artifact at all: it lives in case.test_case_set[].inputs, which the host holds, and is never a snapshot variable. Whether the IR routes a case input to the code is V9.
- Each `io_contract.raw_requirements.required_evidence[].artifact` normalizes (case-insensitively, with an optional `raw/` prefix) to `state_snapshots`, `state_history` or `step_series` (the last two on a `loop` node alone, gate (h)); author the canonical bare form. Any other token, the retired `metrics_basis.json` and `execution_trace.json` among them, is a `fail` whatever its `required` value.
- Each `name` of `io_contract.outputs[]` appears in one of the `outputs` of `algorithm.steps[]`, or is derived by `algorithm.derived_field_rules`.
- When `io_contract.outputs[].evidence_ref=raw/state_snapshots`, `raw_variables` is a non-empty array, and each element references `io_contract.raw_requirements.required_evidence[].schema.variables[].name` or `time_variable`. **The non-snapshot half:** once a `state_snapshots` `required_evidence[]` entry is present and not `required: false`, EVERY `io_contract.outputs[]` entry needs a non-empty `raw_variables` — the trigger is simply that `evidence_ref` does not name `state_snapshots`, so any other entry is a `fail` without it. The element-membership rule above applies to every entry alike; only the single-resolved-shape rule is specific to a snapshot `evidence_ref`. **The single-resolved-shape rule:** for an output whose `evidence_ref` names `raw/state_snapshots`, every element of `raw_variables` must resolve to the SAME shape — `schema.variables[].shape_expr` for a state variable, `schema.time_shape_expr` for the `time_variable` — and `outputs[].shape_expr` must equal that shape (`Compile.static`: `shape_expr must resolve to a single referenced state_snapshots variable/time_variable shape` when the elements resolve to more than one shape; `shape_expr must match referenced state_snapshots schema shape` when they resolve to one and the declared shape differs). Listing a field variable and the `time_variable` together (`raw_variables: ["u", "t"]` with `u` of shape `[nx]` and `t` scalar) resolves to two shapes and is a `fail`; for a non-scalar output, the time it is taken at is recorded in that output's `description`, not as a `raw_variables` element. A scalar output may list the `time_variable` beside a scalar state variable, because both resolve to `scalar`.
- `io_contract.semantic_dependency.required_sources` is a non-empty string array.
- **`io_contract.tests` and `io_contract.predicates` (Z6, [issue #255](https://github.com/seiya/atmofab/issues/255); [issue #582](https://github.com/seiya/atmofab/issues/582); [issue #589](https://github.com/seiya/atmofab/issues/589)) faithfully transcribe each `tests.md` judgment and the DEFINING quantity of it from the captures.** `tests[]` holds the `test_id` of `tests.md` neither more nor less, read through `meta.source_refs.tests` — the IR's only route to `tests.md`, so a ref that does not resolve is itself a `fail`. The `--stage compile` gate (`_validate_tests_and_predicates` → `verdict_evaluator.validate_tests_schema`, `primary_evidence.validate_primary_predicate_schema`) enforces the grammar and the name resolution mechanically (every construct is in the closed grammar, every `initial.<var>` / `final.<var>` / `history.<var>` is a snapshot schema variable, `t` or `step`, every `series.<name>` a `step_series` name, every `derived.<name>` a `derived` entry of every case its scope evaluates, every `inputs.<path>` is a number or a rectangular numeric list in every case its scope evaluates (`at('<case>').inputs.<path>` in that case), every `at('<case>')` is a target case, `coordinates[]` resolves in every case, every operand pair is at equal rank (a coordinate at the state's rank) and `expr` is a scalar, inferred statically, exactly one scope, a numeric `value`). The gate also enforces, by names and cases read only, so what follows is the reviewer's: each test's `quantities` equals its `tests.md` `quantities:` line; the set of `quantity` over its predicates that are not cross-target equals its `quantities`; each predicate's `target_cases` equals its test's; the captured state of every target case of a test enters the value of some predicate of that test, or on an `xfail` test a flag predicate with `value: 0` values it (per test and not per quantity: a refinement order reads the cases of its own pair; `primary_evidence.quantity_violations`, re-checked by `evaluate_verdict`); the one-call scalar pairing of the schema block above; and the `drive` gates (a)-(m), gate (l)'s fixed form and gate (m)'s `history:` line among them. A schema variable carrying `source`, and a `tests.md` carrying a `reported:` line (a retired vocabulary, issue #589), are refused. The SEMANTIC check `Compile.verify` owns: (i) each `quantities` entry names a judgment the test's §6 definition makes, and every judgment that definition makes has one — a judgment with no entry, or an entry no judgment answers to, is a `fail` (a name chosen so that an unrelated expression satisfies the gate is the same `fail`), and so is a predicate no state can fail (a threshold beyond every reachable value, an input the kernel does not write compared with itself, a stability bound (`le`) over only the initial-state speed the case's dt rule was computed from — an `xfail` guard's exceedance over the initial state is not this) — the quantity gate closes omission against the line, and this item closes vacuity and the line against the prose; (ii) each `expr` is a truthful transcription of the `tests.md` §5/§6 definition of that quantity over the captured state and the declared inputs (the conservation integral over the whole domain with the case's cell size, the analytic reference at the case's DECLARED end time — `final.t` is the driver's time after the last COMMITTED step, not a declared input — the convergence order over the two cases it names), with the direction and the FINITE threshold of the prose — where the quantity is one no captured state variable enters, the transcribed definition is the §5 statement with its state term (the schema block above says which quantities these are and which are not). An `expr` that carries a term the `tests.md` definition, or that statement, does not contain — a bound the leaf added, or a state read whose only role is to pass the reads-state rule (`sum(final.u * 0 + 1)` for a cell count — the schema gate refuses that spelling since issue #483 only where it is the entry's sole state read, so this item covers it beside a genuine read and every re-spelling no literal form shows) — is not a transcription and is a `fail`; where `tests.md` defines such a quantity and states no such definition for it, the finding's subject is `tests.md` (§Severity of a finding); (iii) the scope is the one the prose asks for: `per_case` for "in every case", `case:` for a cross-case reduction read at the case that completes it; (iv) each `initial_state` and `derived` entry transcribes the `tests.md` §2 / §3 definition of that case (the closed form, the pre-call content, the dt rule), a `step` dummy carrying the time step or the time under another name binds `loop.dt`'s reference / `time`, a procedure binding's expression, `calls` and `record` transcribe §2 and §5 (the order of the `record` list is the order of the calls), on a `loop` node the two halves of a `step` operation's input / output pair bind one state (bound to two, every step restarts from the initial state and no gate can tell), each `history.every` input is the §3 value, and on a node whose `__init` writes a loop scalar the predicate gate (k) requires transcribes the §3 rule over the `__init`-written arrays; gate (k) is a syntactic floor an inert term satisfies, so this item is the judgment of the transcription. A predicate that evaluates a quantity no test defines, or one whose `expr` is satisfiable by a state the test would reject, is a V3 `fail`. The gate pins the predicate's `target_cases` to its test's; the reviewer does not re-check it. (v) Each `tests.md` cross-target judgment (issue #324) is transcribed as one cross-target predicate per variable it names, under its test and over that test's target cases, comparing this run's `final.<var>` with `comparand.final.<var>` of the SAME variable by the normaliser and threshold the judgment states; no cross-target predicate exists that no judgment asks for. The gate checks only that a cross-target predicate also reads this run's own state and that the tests carrying one are exactly the tests whose `tests.md` definition says "The cross-target judgment is applied", so the rest is the reviewer's: a variable the judgment names with no predicate of its own, or a predicate that compares only `initial.` state, compares this run's variable with a DIFFERENT variable of the comparand (a normaliser that also reads the other variables the `tests.md` definition names, such as a shared momentum scale, is not that), reaches the comparand only through a term that cannot move the value (`0 * maxabs(comparand.final.u)`), or carries a threshold no disagreement of that variable could exceed, is a `fail`. Item (i) does not apply to it: its `quantity` is the one `tests.md` gives the agreement, outside the test's `quantities`. (vi) Each `coordinates[]` entry's `count` names the extent of the state's shape at its `axis`: the `count` value must be, in every case, the extent the `shape_expr` token at that axis takes, whatever key holds it (beside `[ncomp, nx, ny]`, `axis: 1` with `count: inputs.grid.ny` is a `fail`); on a node with `drive` the host checks it when it evaluates the case's state extents (§`drive`), and elsewhere it is the reviewer's.
- **`target_cases` is what the test reads.** A test's `target_cases` must list every case its judgment actually reads — a convergence sweep names all its resolutions, an equivariance test both members of the pair — and no case it does not.

#### V4. dependency consistency
The derived-closure invariants (former V4a `direct∪transitive == all_nodes`, V4b
`expected_node_set == all_nodes`, and topo ordering) are now **correct-by-construction** — the
conductor authors the closure/topo graph into the sidecar and the `--stage compile` gate
cross-checks the IR's `direct_deps` against it (see below). The remaining `operations` invariants:
- **V4c-i (component `operations` non-empty) — deterministic Compile gate.** Every `component/`
  `direct_deps[]` entry authors a non-empty `operations` list (each a non-empty string). Emptiness
  is an *unambiguous structural* fact, so it is pinned by `_validate_component_dep_operations` (a
  `compile_static_violation` routed to `Compile.generate`), NOT left to the LLM: an empty list
  starves the host-resolved dependency facts while the Generate-side gate unconditionally
  requires the import and the call, so a leaf cannot converge (retry budget exhausts). `problem`
  deps are out of scope.
- **V4c-ii (operations ⊆ published) — deterministic Compile gate.** Each `component/` dep's
  non-empty `operations` must be a SUBSET of that dependency's published operation names. Once each
  component IR pins its published op NAMES in `public_api.published_operations` (V8), that surface
  is a deterministic input: the conductor resolves it host-side into the
  `<ir_ref>/dependency_surface.json` sidecar (per component direct dep: `published_operations` + a
  `source` tag of `ir_public_api` / `unresolved`; an `ir_public_api` entry also carries `public_api`,
  the dep IR's `module_parameters` / `interfaces` / `signatures` verbatim, issue #486) at
  Compile-phase start, and
  `_validate_component_dep_operations_membership` checks the authored `operations` against that ONE
  snapshot (the source of the `Compile.generate` leaf's `DEPENDENCIES.md`). A fabricated
  dep op name (the
  2026-07-23 `__apply` drift the facts resolver silently dropped) is now an unambiguous structural
  `Compile fail` to `Compile.generate`, with the full certified catalog + `source` tag embedded in
  the message (the slim-repair lifeline). Inert where the sidecar is absent or the dep entry is
  `unresolved` (a legacy tree / an unresolved graph edge); a malformed sidecar entry is fail-closed.
  Removal of the skip→fail-closed asymmetry after fleet re-auth is tracked in
  `docs/design/deterministic_followups.md`. (Formerly "not deterministically checkable" — a
  component dep IR carried no `public_api`, so the surface had no host data source; the V8 name
  pin supplies it.)
- **V4c-iii (a procedure passed to a dependency matches its prototype) — LLM check.** Where
  `controlled_spec.md` passes a procedure of this node to a dependency argument typed
  `{type: procedure, interface: <name>}` in the sidecar, the step defining that procedure states its
  arguments by the sidecar's `interfaces` entry `<name>`: type, kind, rank, explicit shape and
  `intent`, in order (names are this node's). Inferring them from prose, or declaring a
  `Compile fail` for the ABSENCE of a prototype the sidecar lists, is wrong (a spec that contradicts
  the prototype is still a fail). No Compile gate: the IR has no structural carrier for those
  arguments. The `Generate.gate` syntax probe (closure staged) refuses what the target compiler
  checks of a passed procedure — measured: type, kind, rank, `intent`, constant bounds — not
  argument-expression bounds permuted or same-typed arguments swapped, which only a case with
  unequal extents exposes at `Validate` (issue #486).


#### V8. public_api surface
- **V8 — a node whose `spec_kind` publishes an EXACT surface: names AND signatures.** The kinds are `_EXACT_PUBLISHED_SURFACE_KINDS` — `infrastructure`, `component` (since issue #153, 2026-09-04) and `problem` (since issue #589) — and the gate reads a node's kind from `spec/registry/spec_catalog.yaml`, never from `meta.spec_kind` alone. One gate (`_validate_published_surface`, `--stage compile`) parses §5's published-operation contract and pins `published_operations[].operation_id` == §5 ops and `published_types` == §5 types, AND pins `signatures` + `module_parameters` == the §5.1 block (the two sub-bullets below). A dropped/extra entry, a missing/unresolvable controlled_spec, a §5 parsing to zero ops, an absent `public_api`, or a per-symbol drift is a `Compile fail` to `Compile.generate`. The LLM only transcribes §5 / §5.1 faithfully; the gate enforces equality.
  - On an `infrastructure` node this closes the R1 mode where Compile→IR kept only test-exercised ops, so Generate never published the helper emitters/writers a consuming runner links against.
  - On a `component` node the NAME half is the source-of-truth pin the V4c-ii membership gate and the L2 `dependency_surface.json` sidecar read (the sidecar copies the SIGNATURE half below as well, for a consumer's V4c-iii; issue #486): without it the leaf of that time re-picked a component's public op name on every regeneration (the 2026-07-23 `__compute_flux`/`__advance`/`__apply` drift). Its generation-side mate `_validate_component_generated_surface` (`Generate.gate`) pins the generated `<spec_id>__` definition set == this list — the set its own scanner reads, which is one KIND of definition and not every published symbol, so that gate is the authority on which.
  - On a `problem` node the surface is `<spec_id>__step` and, when §5.1 declares it, `<spec_id>__init` (issue #589): the driver calls them, so the operations `drive` binds are pinned against the controlled spec as a component's are.
  - **The SIGNATURE half was this rule's inverse until issue #153**: `signatures` / `module_parameters` were FORBIDDEN keys on a component, and the argument ABI was left derived from the certified source at Build. The measured consequence: the ABI a component published was whatever the generation emitted, so one `spec_version` republished a different interface on every regeneration — one component's six certified sources carried six different argument lists, two of them from the SAME IR — and a consumer certified against the old one linked against an interface that had moved, discovered only at a downstream node's build. Reversed by the operator decision of 2026-09-04; a component's ABI change now carries a `spec_version` bump.
- **§5.1 canonical interface block + `public_api.signatures`** (same gate): the controlled_spec §5 of a node of ANY kind in `_EXACT_PUBLISHED_SURFACE_KINDS` must carry a `### 5.1` fenced **language-neutral structured** interface block giving every published type + operation *signature* (the machine-readable contract, independent of the implementation language). The gate cross-checks §5.1's symbol set == §5's name lists, and pins the IR's `public_api.signatures` (each `{symbol, signature}`, the structured form) == §5.1 (each language backend renders both and normalizes: comments stripped, `&` joined, whitespace-insensitive, and names compared as that language compares them — Compile renders in every language, a case-sensitive one among them, so copy each §5.1 name's spelling; argument name/order/type/rank/`intent`/`result` and type components must match). Compile.generate transcribes §5.1 into `public_api.signatures` because `Generate.generate` is walled off from `controlled_spec.md` (phase_02 §2-1) — the IR is the leaf's only carrier of the signatures to publish. A missing/duplicate fence, a §5↔§5.1 mismatch, a missing `signatures` block, or a per-symbol drift is a `Compile fail`. The same gate pins §5.1's `interfaces` (named procedure prototypes referenced by a `{type: procedure, interface}` argument, issue #266) == the IR's `public_api.interfaces`: name set equal and each prototype's argument set equal; an IR without the key passes only when §5.1 declares no prototype (every IR certified before the carrier existed), and a prototype is NOT cross-checked against §5's operation list (it is not an operation). The signature bodies are then pinned against the generated model source by the `Generate.gate` static check (`_validate_generated_signatures`), over the same kind set.
- **`public_api.module_parameters`** (same gate, `_validate_ir_module_parameters_against_section51`): pinned == the §5.1 block's `module_parameters` (each `{name, base?, value}`) by name AND value (names compared under each rendering language's case rule — Compile renders in every one, so copy the §5.1 spelling: `DP` for `dp` is a drift in a case-sensitive language; values compared normalized, so `64` == `"64"` and the neutral kind token `float64` == `FLOAT64`; `base` is not compared). Like the signatures, Compile.generate transcribes these because `Generate.generate` is walled off from `controlled_spec.md` — the IR is the leaf's only carrier of the values (`dp` / `case_id_len`). A missing key (when §5.1 declares any), a non-list key, a malformed/duplicate entry, or an omitted/extra/drifted value is a `Compile fail`; the declarations are value-pinned against the generated source by the `Generate.gate` static check — which requires the source to bind each pinned name EXACTLY ONCE, by that declaration, since the stanza comparison pins an argument's type only symbolically (by its kind name) and so routes all precision information through this one check.

An unresolvable adopted `profile` is not a `Compile fail` but a transport `fail_closed` (`compile_dependency_graph_profile_*`): it is decided before any leaf launches, and the repair is to the `spec`.

#### V9. case input routing
- **Presence.** Every runtime input (schema block, `case`) that the node's `tests.md` §2 or `controlled_spec.md` fixes for a case is declared in that case's `inputs`.
- **Route.** Each declared runtime input has a route `Generate` can follow from the IR alone: a `drive` binding — the input bound to a dummy (`inputs.<path>`), or a `derived` entry that reads it bound to one (issue #589); an `io_contract.inputs[]` entry (`evidence_ref: spec.ir.yaml`) that a step's `inputs` or a derived rule a step consumes names, with that step's description or that rule saying what the value fixes (`L`, `dt_rule` → the `dx` / `dt` rules); a step description stating what the value fixes in that step, per case where it differs (a halo step's `boundary.periodic`); a dependency-realized step (a `boundary.kind` realized by the `boundary_apply` dependency step); or a derived rule / step description that fixes a runner-output field from it (`time.steps` → `perf.json` `steps`, the grid → `cells_updated`). A route that says nothing about what the value fixes is none.
- A null member of its kind (`boundary: {kind: none}`) needs no route unless `controlled_spec.md` or `tests.md` gives that aspect content for that case (a boundary scheme, a time-stepping), where the null value is itself a V9 (and V2) `fail`; a harness `tests.md` that asks for the key without giving it content licenses it. A provenance input, and a key or member the algorithm does not take, needs no route; code MAY read a provenance input.
- The only deterministic floor is partial: an `inputs.<path>` a predicate names must resolve in every case its scope evaluates, one a `coordinates[]` entry or a `drive` binding names in every case. The rest is judged to V2's standard.

#### Verification tools
These deterministic gates run in the conductor's `Compile.static` substep (`_compile_static_inproc`), NOT inside the `Compile.verify` leaf:
- Well-formedness of `<ir_ref>/spec.ir.yaml` and `<ir_ref>/ir_meta.json` — `--stage compile` reports an unparsable or non-mapping document as `invalid yaml` / `invalid json` / `must be mapping` / `ir_meta.json: must be json object`; on `fail` it is a `Compile fail` (routed to `Compile.generate`).
- `python3 tools/validate_pipeline_semantics.py --stage compile --ir-ref workspace/ir/<node_key_safe>/<ir_id>/`, with `exit code 0` required. The result is recorded in `compile_static_meta.json`; on `fail` the substep fails before `Compile.verify` runs, so `verification_status=pass` is never reached on a structurally-invalid IR. (The `--stage compile` validator checks the internal-consistency / shape-grammar invariants; the spec-cross-reference invariants are the `Compile.verify` LLM responsibility — see the substep structure note.)
  - **dependency direct_deps consistency** (`_validate_compile_dependency_consistency`): the IR's `dependency.direct_deps` (`(kind, spec_id)` set, version-agnostic) must equal `{all_nodes} − {self} − {transitive}` from the `dependency_graph.json` sidecar. A mismatch is a `Compile fail` routed to `Compile.generate`. Version drift is soft (the Build compile is the backstop). Closes the former V4a/V4b gap.
  - **profile selection pointer** (`_validate_profile_selection`, issue #175): when the sidecar's `profiles` is non-empty, every `inputs.profile_selection` is exactly `{profile_id, profile_version}` naming one of its entries; a missing field, an extra key, or an unresolved id/version is a `Compile fail` to `Compile.generate`. The SIDECAR decides which nodes are gated, never the IR: with `profiles: []` the field is not read at all (no case declares it there — schema block; a stray key is not this gate's to refuse). A sidecar with no `profiles` key is refused.
  - **component dependency operations non-empty** (`_validate_component_dep_operations`, V4c-i): every `component/` `direct_deps[]` entry carries a non-empty `operations` list in which **every** entry is a non-empty string (a bare-string entry, a missing/non-list/empty `operations`, or a list containing **any** non-string/blank entry — even alongside valid ones — all fail); a violation is a `Compile fail` routed to `Compile.generate`. Rationale + the resume-safe host fallback are under V4c-i above. `problem` deps are out of scope.
  - **published surface** (`_validate_published_surface`, every node kind, the kind read from the catalog, V8): `public_api.published_operations`/`published_types` must equal the controlled_spec §5 surface, and `public_api.signatures`/`public_api.module_parameters` must equal the §5.1 canonical interface block (signatures by rendered stanza, module_parameters by value). A missing controlled_spec ref, a §5 parsing to zero ops, an absent `public_api`, or a set/value mismatch is a `Compile fail` to `Compile.generate`. (Whether the target's language has a signature backend is asked at `Generate.static`, where a target exists; the IR names none.) A kind outside the set is a no-op.
  - **component dep operations ⊆ published** (`_validate_component_dep_operations_membership`, V4c-ii): each `component/` dep's non-empty `operations` must be a subset of that dep's published surface, by exact spelling (Compile names no target, and a case-sensitive one resolves the published spelling only), in the conductor-authored `<ir_ref>/dependency_surface.json` sidecar; a fabricated name is a `Compile fail` to `Compile.generate` carrying the full certified catalog + `source` tag. Inert when the sidecar/entry is absent or `unresolved`; a malformed sidecar entry is fail-closed. The sidecar's `public_api` blocks are not read by this gate.
  - **drive and case inputs** (`primary_evidence.drive_violations`, `primary_evidence.evaluate_case_inputs`, issue #589): the gates (a)-(m) and the per-case evaluation the schema block's `drive` comment states, the kind read from `spec/registry/spec_catalog.yaml`; it writes nothing. Each violation is a `Compile fail` routed to `Compile.generate`.
  - **harness render preconditions** (`_validate_harness_render_preconditions`, every non-`infrastructure` node, asked once per declared target profile whose toolchain the host renders a runner for, with that target's language and harness — the IR is target-free, so it must render for each; issue #284): the node's runner is host-rendered from this IR alone, so IR *content* the renderer cannot render is caught here instead of at the render backstop (which terminates the workflow rather than retrying). The gate delegates to `tools/host_render.ir_content_violations`, which invokes the language backend's renderer itself, so it cannot drift. Each violation is a `Compile fail` routed to `Compile.generate`:
    - `raw_requirements.required_evidence` holds a `state_snapshots` entry with a non-empty `schema.variables[]`, and its `schema.time_variable` is `t` (the harness's fixed snapshot-time key); separately, at V3 (`_validate_io_contract_file`, every node kind): every `state_variables[].name` of the resolved state contract is a `state_snapshots` schema variable — a declared primary state the runner never captures is a `Compile fail` (`are not state_snapshots schema variables`), because no host-evaluated predicate could reach it and the bundle binds the schema alone (Z6, [issue #255](https://github.com/seiya/atmofab/issues/255));
    - no snapshot variable is named `t` / `case_id` / `step` (harness-reserved snapshot keys); every snapshot variable is a bindable identifier — on a `problem` / `component` node it is a state the host-rendered DRIVER declares, allocates and passes to the operations `drive` binds it to (§`drive`), never a variable of generated code — so it matches `[A-Za-z][A-Za-z0-9_]*`, the driver's state variable named after it fits the 63-character identifier bound, and no two names fold to one identifier (the bound target languages include a case-insensitive one);
    - every snapshot variable (each is captured for every case) declares `shape_expr` as `scalar` or the bracket form `[d1, ...]` of rank 1–4; the `(d1, ...)` paren form this grammar otherwise permits is rejected, as is rank > 4;
    - `case.test_case_set[]` is non-empty with pairwise-distinct `case_id`s (duplicates render overlapping case labels in the host-rendered runner's case dispatch), each ≤ 64 chars — the harness `case_id_len`, which `__parse_cases` truncates a longer id to, so the runner's case label could never match and every run would stop with an error despite compiling — and each matching `[A-Za-z0-9._][A-Za-z0-9._-]*` with no `..`: a case_id is concatenated into the per-case snapshot path (`raw/state_snapshots/<case_id>.json`), so a `/` or `..` would let the run write outside its directory, and a leading `-` would be read as an option in the runner's argv;
    - every name the renderer embeds in the runner (`case_id`, snapshot variable, the target's hardware class) is printable ASCII — a control character has no literal form in the runner's language, and a non-ASCII character makes the rendered byte length disagree with the code-point length every render bound is measured in — and no rendered line exceeds 100 columns, so an over-long IR-sourced name is rejected rather than emitted as an unlintable runner.
  - Node-**identity** preconditions are excluded from these gates because re-authoring the IR cannot repair them: `spec_id` ≤ 55 characters, the minimum of the declared language backends' `SPEC_ID_MAX` (it keeps the derived `<spec_id>_runner` / `_model` identifiers within 63 characters, the shortest identifier bound an implemented language backend declares), and **no** `infrastructure` dependency declared in `deps.yaml` on any `spec` (issue #284: the runner harness is the attribute of the target profile a node is built for, `spec/targets/<target_id>.yaml` `harness`, and the host adds it to every pipeline of a non-`infrastructure` node; a declaration is refused as `infrastructure_dependency_declared_in_deps`, and inside a `profile` as `profile_declares_infrastructure` at the Compile start of each ADOPTING node). Both are enforced at **spec-input**, before any phase runs — for the target `spec` and for every member of a `--with-deps` closure alike. A `deps.yaml` that is **missing or whose `dependencies:` schema is malformed** is likewise rejected at spec-input on a non-`infrastructure` `spec`, since its dependency declaration cannot be read. A resume re-resolves its node through the same gate, so the remedy is to fix `deps.yaml` (or rename the `spec`) and start a new run.

#### Severity of a finding (`issue_severity`)
`issue_severity` names the repair a finding calls for. You return it in your verdict and the host projects it, with `last_fail_reason`, onto `ir_meta.json`; you do not write that file. (An agentic leaf recorded it there itself until Z4, issue #171.) The conductor acts on the value as `docs/workflow/WORKFLOW_CORE.md` states; the value does not grade the consequence the defect would have at `Generate` or `Validate`, and the size of the correction does not change it. One question selects the value: whether `Compile.generate`, re-run with the finding and the same `controlled_spec.md`, `tests.md`, `deps.yaml` and `spec/registry/spec_catalog.yaml` — this substep's verification inputs, listed in §I/O contract above — can resolve the finding. This is the axis `docs/workflow/phases/phase_02_generate.md` §2-2 §Severity of a finding applies to `Generate.verify`; `spec.ir.yaml` is the artifact under review here and an input there, so a thin lowering takes `minor` here and `major` there.
- `minor`: the defect lies in `spec.ir.yaml`, and `last_fail_reason` names a correction that keeps it faithful to `controlled_spec.md`, `tests.md`, `deps.yaml` and `spec/registry/spec_catalog.yaml`. Every V1–V4, V8 or V9 finding whose subject is the IR takes this value — a `case_id` `tests.md` requires and `case` lacks, a runtime case input the IR routes nowhere, a local operation lowered without the defining math `controlled_spec.md` carries, a declared `execution_mode` that contradicts the control structure `controlled_spec.md` describes, a `predicates` entry that mistranslates its `tests.md` prose, a `quantities` list that omits a judgment — unless the `critical` bullet's condition holds, which wins over this universal.
- `major`: no re-run of `Compile.generate` from the same inputs can resolve the finding. The subject is an input rather than `spec.ir.yaml` — a `spec` document, or what the conductor hands the leaf (a dependency's certified surface, a dependency `ir_meta.json` this substep cannot read); that question decides the value, and these are the cases it usually takes: `controlled_spec.md` states nowhere the defining math of a local operation the algorithm needs; `tests.md` contradicts itself (a `quantities` entry no §5 definition or §6 judgment of `tests.md` defines) or `controlled_spec.md` (a judged quantity over a value `controlled_spec.md` never defines); a dependency's published surface lacks an operation `controlled_spec.md` calls on it; the intent of `controlled_spec.md` does not fit the IR schema; `tests.md` defines a judged quantity no captured state variable enters and its §5 states no primary-evidence statement for it (`docs/TESTS.md` requirement 9). `last_fail_reason` names the input and the contradiction.
- `critical`: `spec.ir.yaml` cannot serve as the base of a repair: it is not this `node`'s, or its `algorithm` lowers a scheme other than the one `controlled_spec.md` describes as a whole rather than diverging in a step.
- When the reviewed documents do not settle whether the subject is `spec.ir.yaml` or an input, the value is `minor`. When they do not settle whether `spec.ir.yaml` can serve as the base of a repair, the value is `minor`: the repair the finding calls for is a producer re-run either way, and only a subject on the input side earns `major`.

## On-failure behavior
- When the input (`controlled_spec.md` / `tests.md` / `deps.yaml`) is insufficient, it is a `Compile fail`, and guessed completion is forbidden. The host-resolved dependency surface (each dependency's published ABI included) is an input too: do not declare absent what it carries.
- A self-check invariant (V1–V4, V8, V9) is `Compile.verify`'s check, not a ground to declare: `Compile.generate` authors an IR that meets it (an input that makes that impossible is the bullet above), and `Compile.verify` returns a `fail` naming the invariant ID in `last_fail_reason`, graded by §Severity of a finding.
- The repair strategy of a retry is the conductor's choice, not the leaf's.

## Acceptance of retry from Validate
The conductor never routes a `Validate.judge` `fail` to `Compile` by itself: the failure escalates to the diagnostician (`judge_semantic_review_fail`), and `Compile` is re-derived only when the diagnostician's directive reopens it. That is a `prod` route; in `dev` the diagnostician answers an upstream defect with `fail_closed`, which revokes nothing, and the operator re-derives `Compile` by hand with a revocation that carries the judge's findings (`docs/RUNBOOK.md` §3-1, the cross-phase re-derivation entry). On a reopen the conductor revokes the certified IR with the judge's findings as the revocation's `last_fail_reason`, one entry per finding (`[<attribution>/<confidence>] <description> (evidence: <refs>)`), and resets `Compile` and every phase after it. A `reuse` directive re-runs the `Compile.generate` attempt this orchestration launched with those findings as the repair findings of its prompt; a `restart`, or a `Compile` this orchestration did not derive, re-runs it from the full launch prompt, and the findings stay on the revoked `ir_meta.json`. A `Generate.verify` finding naming `ir_inconsistency` reaches `Compile` the same way in `prod` (`docs/workflow/phases/phase_02_generate.md` §On-failure behavior).

What a leaf does on such a re-run depends on WHICH substep it is, and the two are opposite — this document is seeded in full into both work trees, so read only your own line:

- **`Compile.generate` (the producer).** Your `compile_fail.json` is not a repair note: it is the terminal "this phase cannot be completed from these inputs" declaration of §1-1, handed to the operator and never retried. Author the IR from the context you were given, using the finding text the prompt carries, and write it as `spec.ir.yaml` with no `compile_fail.json`.
- **`Compile.verify` (the reviewer).** You do not author the IR under any circumstance, and your `last_fail_reason` is the ORDINARY verify-fail channel — the host projects it onto `ir_meta.json` beside `verification_status: "fail"`, and the phase routes it by §Severity of a finding exactly as it routes any other fail. A null `last_fail_reason` is the PASS invariant of your output contract, so writing it is a verdict of pass and nothing else. A re-submission after a Validate finding is a node whose IR was already judged at fault downstream; review it on its merits.

## Design trade-offs
- By having the IR **self-check only the structural invariants**, the bloat of the verification contract is avoided. Semantic correctness is delegated to the `Validate` execution result (the "hybrid verification" principle).
- By keeping the IR **target-free** (issue #284), one Compile serves every target: the target — toolchain, hardware, parallel model, harness — is the host's profile, chosen per run, and the lowering onto it is the `Generate` producer's `target_lowering_plan`, so the Tune optional flow varies the lowering plan rather than the IR. Until then the IR carried both as `impl_defaults` (a fixed layer and a Tune knob layer), which made every IR a statement about one target.
