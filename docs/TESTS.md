# Requirements and format of tests (canonical source)

## Purpose
`tests.md` is the canonical source for the verification input and judgment conditions of a `spec`. It is commonly used for the `spec_kind` `problem` / `component` / `infrastructure`. A `profile` has none: it is a compile-time selection policy the host resolves, nothing is generated or executed for it, and there is therefore nothing for a test to be about (`docs/SPEC.md` requirement 5, issue #175).
The evaluation result of `tests.md` is mapped to the relevant `node`'s `self_verdict` in `verdict.json`, and the aggregated judgment including dependencies is handled in `aggregate_verdict.json`.

## Scope
- `spec/problem/<domain>/<family>/<spec_id>/tests.md`
- `spec/component/<domain>/<family>/<spec_id>/tests.md`
- `spec/infrastructure/<domain>/<family>/<spec_id>/tests.md`

## Requirements
1. The canonical source format is `Markdown`.
2. State `test_profile_id`, `test_profile_version`, `status`, and `spec_ref` at the top of the document as required. The two `test_profile_*` values are `provenance input`s (`docs/GLOSSARY.md`): a `tests.md` sentence asking that they be recorded in `trial_meta.json` is met by the host at `Validate.execute`, not by generated code.
3. `spec_ref` requires `spec_kind`, `spec_id`, `spec_version`, and `controlled_spec_path`.
4. Each `spec` defines at least 1 `L0` test.
5. Define `L1` / `L2` / `L3` according to the verification purpose. They are not forbidden by `spec_kind`.
6. No fixed lower bound on the number of tests is set. Sufficiency is judged by the requirement-coverage rule.
7. Treat an undefined item as an error without completion.
8. The judgment condition must be evaluable per `node_key`. It must not implicitly reference the state of a dependency `node`.
9. A judged quantity that no captured state variable enters (a captured state variable is any variable of the state snapshot except one that only echoes a value the inputs fix, such as a time step) — the §5 definition fixes it from the case inputs, the constants and the time alone (a CFL number over a prescribed speed, a step count the inputs fix) — carries, in §5, its **primary-evidence statement** ([issue #340](https://github.com/seiya/atmofab/issues/340)): the expression the host evaluates over the captured state and the declared inputs for that judgment, and the state fact it rests on (a bound an invariant of the `Controlled Spec` gives). For each `xfail` test that judges the quantity, the statement says whether the guard leaves a state fact at all. A quantity that a captured state variable enters needs none, however much of it the inputs fix (a CFL number over a wave speed the state carries): its definition is its primary evidence. Neither does the `xfail_condition` of a guard test over the inputs (`nx < 2`, `dt <= 0`): what that test judges is the kernel's refusal, not a quantity. Without the statement, the Compile contract has no faithful expression to transcribe and `Compile` fails (`docs/workflow/phases/phase_01_compile.md`, the `primary_predicates` rule of the schema block and V3). The two Compile leaves apply this rule; no deterministic gate does, so a certified IR is not evidence that its spec carried a statement.

## Coverage rules per `spec_kind`
- `problem`
  - Define execution control, case expansion, judgment expressions, and pass/fail aggregation rules.
  - When there is a non-applicable item in the validity judgment, define `N/A` and `reason_na`.

- `component`
  - For each published `operation`, define at least one normal case and one guard case (`fail` or `xfail`) each.
  - Add `L1`-and-above accuracy / conservation / equivalence tests as needed.

- `infrastructure` (R1 harness)
  - For each published harness operation, define at least one normal case and one guard case (`fail` / `xfail`) each — e.g. numeric round-trip (negative / min / max), boolean-literal emission, case fan-out → per-case snapshot naming, a missing-`--cases` guard (`xfail`), and per-test index completeness.
  - The harness's own runner (a self-test driver) exercises these; the existing `post_execute` gate group provides additional oracles.

## Description format
0. Meta information
1. Test purpose
2. Input-defaulting rules
3. Execution-control rules
4. Case-expansion rules
5. Diagnostics contract
6. Test definitions
7. Pass/fail aggregation rules
8. Traceability

When there is an unnecessary section depending on the `spec_kind`, state `N/A` and the reason rather than omitting it.

## Cross-target judgment
A `problem` or `component` node generated for more than one target is also judged variant against variant ([issue #324](https://github.com/seiya/atmofab/issues/324)): in each target case, a state variable this target's run captured after the run is compared with the same variable, in the same case, of every other target's certified variant of the node (the `comparand`, `docs/GLOSSARY.md`). A test opts in with a judgment that contains the exact words "The cross-target judgment is applied" inside its own definition (a `problem` writes "The cross-target judgment is applied. The evaluation expression is `cross_target_state_agreement` over `<variables>`, and the threshold is $\le v$"; a `component` appends "The cross-target judgment is applied: `cross_target_state_agreement` over `<variables>` is `<= v`" to its `judgment` line), naming each state variable it compares; §5 of the same `tests.md` defines the quantity. The Compile gate keys on those words (`validate_pipeline_semantics.CROSS_TARGET_JUDGMENT_PHRASE`): every test whose definition contains them must carry a cross-target predicate and no other test may, so a judgment that is not applied says "The cross-target judgment is not applied". The default definition is, for one variable $q$ and one other variant $q^{ref}$,
$$
\mathrm{cross\_target\_state\_agreement}=\frac{\max_i |q_i-q^{ref}_i|}{\max(\max_i|q_i|,\ \max_i|q^{ref}_i|,\ 1)}
$$
over every element $i$ of the variable. The denominator is symmetric in the two variants, so which of them is the comparand does not decide the verdict, and its floor of `1` makes the bound an absolute one for a variable of magnitude below `1`. A `tests.md` states another normaliser in its §5 when a variable's own maximum is not the scale of its rounding — a velocity or momentum component that is a small residue of the flow is normalised by the largest maximum over all the components of that vector in both variants — and keeps it symmetric in the two variants.
- The host evaluates it from the captured primary state, against each other variant separately. It has no `diagnostics.json` field and no secondary condition: the generated code of one target never sees another target's state.
- On a target with no other certified variant it holds vacuously; with one, a disagreement fails the test on every variant whose Validate reads the other, because it does not say which of the two is wrong.
- Name only variables whose value after the run the `Controlled Spec` defines in every target case of the test. Do not apply it to an `xfail` guard test: the state a refused or unstable case leaves is not a result two variants must agree on.
- The threshold states how far two correct variants may drift apart (a contracted multiply-add, another summation order, a device math library); it is not an accuracy bound.
- An `infrastructure` harness node does not carry it: each target has its own harness `spec`.

## Operations Rules
- When a `Controlled Spec` change affects the judgment conditions, update `tests.md` in the same change.
- For `xfail`, define `xfail_condition` and `pass_when` simultaneously.
- When changing a threshold, state the affected `test_id`.
- Judge pass/fail including dependencies not in `tests.md` but in `dependency.resolved.yaml` and `aggregate_verdict.json`.

## Decision Criteria
- The test input and pass/fail judgment can be restored from the document alone.
- The correspondence between `spec_ref` and `controlled_spec.md` is unique.
- An `L0` test exists.
- The requirement-coverage rule is satisfied.
- The judgment result can be reproduced as a per-`node_key` `self_verdict`.
