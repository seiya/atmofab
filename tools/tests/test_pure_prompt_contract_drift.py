"""Drift guard for the pure prompt contract (TODO Item 5).

The pure leaf is doc-blind: its behavioral contract is whatever the launch templates + the fixed
ABI constants say, and a change to any of them is a behavior change that MUST be observable through
`PURE_PROMPT_CONTRACT_VERSION` (so `bundle_meta.json` / the launch record stamp the new contract).
Nothing else enforces that coupling — a one-character template edit with no version bump, or a bump
with a stale template, both ship silently.

This test pins a sha256 over the COUPLED contract tuple against the CURRENT version, keeping every
historical pin. The assertions catch three drift directions:
  * an edit to any pinned surface WITHOUT bumping the version -> digest mismatch;
  * a version bump whose PINNED entry was not recomputed -> KeyError / digest mismatch;
  * an EMPTY version bump, or a silent REVERT to a prior contract (a new version whose contract
    tuple equals an earlier version's, so its digest matches) -> duplicate-digest failure. This
    enforces the no-empty-version-bump policy and reverse-drift detection: a genuine, non-reverting
    bump changes the contract tuple and therefore the digest, so every pinned version's digest must
    be unique. History runs from `pure-6` (the pre-guard baseline, seeded below) forward; pins are
    frozen literals (NOT recomputed from the current tuple, which has since moved) and exist only to
    reject a later version that duplicates one of them.

The pin set is deliberately NARROW (a churn magnet if widened): the five template files, the
fixed `PURE_SYSTEM_PROMPT` (the `--system-prompt` string, a documented version-bump trigger in
`pure_leaf.py`), the cold-repair static-paragraph prefix list, the checks-ABI constants the
templates distill verbatim (`CHECKS_PUBLIC_NAMES` and the two character widths), and — since
issue #142 — the §1-4 slice of `docs/workflow/CHECKS_MODULE_CONTRACT.md` and — since issue #143 —
the `#### Severity of a finding` slice of `docs/workflow/phases/phase_02_generate.md`, both of
which the reviewer's prompt inlines verbatim. Those last two are DOCUMENTS, which the bar above
would normally exclude; they qualify because those sections stopped being a document the leaf
reads and became a document the host pastes into the leaf's prompt, which is the same category as
the template bytes. Each is hashed as its SLICE, so the rest of its file stays outside the pin:
editing §5 (the deterministic-gate section) or a G1-G7 checklist item costs no bump. Every member
is a STABLE, behavior-defining input (not a churny one); do NOT grow it beyond that bar. A third
document slice needs the same argument made for it — that the host pastes it into a leaf prompt
and that the leaf's decision depends on it — not this precedent alone.

DELIBERATELY NOT PINNED, and this is a REFUSAL rather than an omission: the documents the Z1
compile pair inlines IN FULL (`docs/workflow/phases/phase_01_compile.md`, the two
`docs/examples/spec_ir_algorithm*` files, `spec/schema/ir/impl_defaults.schema.json`). They meet
the "host pastes it into the leaf's prompt" half of the bar above and fail the "stable" half:
`phase_01_compile.md` has taken 61 commits over 30 distinct days all-time, 43 of them over 16
days in the seven weeks to 2026-09-07 — measured at `04a7b75` with
`git log --oneline -- docs/workflow/phases/phase_01_compile.md | wc -l` and the same log with
`--date=short --format=%ad | sort -u | wc -l`, the second run again with
`--since=2026-07-19 --until=2026-09-08`. (An earlier version of this sentence said 59 commits
over 12 days. That figure was carried in from the planning session rather than re-measured here,
and no counting of this file's history reproduces it; the argument it supports is unaffected,
but a refusal's own justification has to be checkable.) So hashing it would bump
`PURE_PROMPT_CONTRACT_VERSION` on most edits to it — and a bump
has two side effects that reach work this pin has nothing to do with: `_resolve_exemplar_source`
stops offering every sibling exemplar certified at an earlier version, and
`validate_pipeline_semantics._validate_orchestration_hierarchy` refuses to `--resume` an
orchestration across it. Editing a phase document would then break the generate side's exemplar
selection and every in-flight run. Those documents are treated as the leaf's INPUT DATA — the same
category as `controlled_spec.md` and `tests.md`, which are inlined and unpinned for the same
reason — not as its contract. The known cost is stated rather than argued away: an edit to the
phase document DOES change what a compile leaf is told, and this pin will not see it.

The narrower alternative was raised and declined at PR time: `#### Severity of a finding` in
`phase_01_compile.md` sits under the same anchors `_generate_verify_severity_rubric_section`
matches in `phase_02_generate.md`, so that ONE subsection could be sliced and hashed the way the
generate rubric is (issue #143's precedent — the reviewer's `issue_severity` decides routing).
The operator chose the whole-document, unhashed disposition for PR-1 (issue #168). If a review
round reverses that, the reversal costs exactly one version bump.

Every pinned member is either a production constant IMPORTED from its authority
(`CHECKS_PUBLIC_NAMES`, the status width, the prefixes, `PURE_SYSTEM_PROMPT`) or the template file
bytes themselves — never a test-local COPY of a production value, which would drift silently from its
source and pin nothing. In particular the checks-status vocabulary (`'pass'`/`'fail'`/`'na  '`) is
NOT pinned as a separate literal: it has no production enum constant (it lives only as prose in the
templates), and the template prose is already covered by hashing the template bytes above, so a copy
here would be redundant and self-referential.

DELIBERATELY OUT OF SCOPE — host-side acceptance-gate / backstop IMPLEMENTATIONS (e.g.
`validate_pipeline_semantics._validate_diagnostics_contract_output`, the post_execute backstop for
the diagnostics contract that the producer prompt's clause (A) is written against).
`PURE_PROMPT_CONTRACT_VERSION` tracks the pure leaf's INPUT contract (the prompt templates,
`PURE_SYSTEM_PROMPT`, the transport request shape — see `pure_leaf.py`), NOT host-side checks that
run AFTER the leaf returns. Hashing such a gate's source into this tuple would (a) force a spurious
version bump on every transparent gate change — a refactor, a comment, or a false-positive FIX —
making the very churn magnet this pin set is scoped to avoid, and (b) miscategorize a host-side
change as a leaf-input-contract change. The gate's behavior is instead guarded by its own
behavioral tests, and a gate change is captured for A/B comparability by the run's recorded repo
revision (`preflight` / `orchestration_meta.json#invocation`), not by this version. The prose the
gate and prompt share IS pinned — as the template bytes.
"""
from __future__ import annotations

import hashlib
import json
import re
import unittest
from pathlib import Path

import tools.codegen_bundle as cb
import tools.orchestration_runtime as ort
import tools.workflow_conductor as wc
import tools.backends.language.fortran.runner as rr
import tools.backends.linter.fortitude.lint as _fortitude_lint
from tools.pure_leaf import PURE_PROMPT_CONTRACT_VERSION, PURE_SYSTEM_PROMPT

_TEMPLATE_FILES = (
    "pure_compile_generate.txt",
    "pure_compile_verify.txt",
    "pure_generate_generate.txt",
    "pure_generate_verify.txt",
    "pure_generate_generate_harness.txt",
    "pure_generate_verify_harness.txt",
    "pure_bundle_repair.txt",
    "pure_escalate_diagnose.txt",
    "pure_validate_judge.txt",
)

# sha256 of the canonical serialization of the coupled tuple, keyed by contract version. When an
# INTENTIONAL contract change bumps `PURE_PROMPT_CONTRACT_VERSION`, KEEP the existing entries and
# ADD the new (version -> digest) entry with the value this test prints on failure. A bump with no
# matching entry, an edit with no bump, or a new version whose digest duplicates an earlier one (an
# empty bump OR a silent revert to a prior contract) all fail here. Never delete or edit a
# historical entry — they backstop those checks.
#
# `pure-6` is the pre-guard BASELINE: it predates this guard (the guard was introduced at pure-7), so
# it never had a live pin; its digest is the pure-6 template bytes (origin/main) hashed under the
# pure-6/pure-7 tuple schema. Only pure_generate_generate.txt differs between pure-6 and pure-7;
# every other pinned member is identical across the two.
#
# NOTE — the contract-tuple SCHEMA changed at pure-8: `check_id_width` was dropped from
# `_contract_tuple` when the runner-driven per-id checks ABI removed the pinned check-id width
# (`CHECK_ID_WIDTH` no longer exists). The pure-6 / pure-7 digests below are FROZEN
# literals computed under the OLD (wider) schema; they are NOT recomputed from the current tuple and
# serve ONLY the uniqueness / no-empty-bump check (`test_no_empty_version_bump`). Because they were
# hashed under a schema the current `_digest()` no longer produces, they can never collide with a
# current-schema digest — so they do NOT detect a future REVERT of the template to the pre-pure-8
# (pure-6/pure-7) contract. Revert detection holds only among versions sharing the CURRENT schema
# (pure-8 onward); the empty-bump guard across all pinned versions is unaffected. Only the CURRENT
# version's pin (whichever `PURE_PROMPT_CONTRACT_VERSION` names, below) is a live equality
# target for `_digest()` — naming a specific version here just goes stale on every bump.
PINNED: dict[str, str] = {
    "pure-6": "b614072bcaad7ffe61f48d54256305b89982457d2ef6c3b5126e09598e5e7067",
    "pure-7": "14c7db85579eeb5f0dd21af2a7321edfcc9bcd647bcb735f511e0d3f80aa2eda",
    "pure-8": "1b1a9575930504226c6d6acebf7cf3ee4b64247e4146f978ee84bbe505b1e4c2",
    "pure-9": "273f38bdbf82569ed5f7ebb7a4ce9896c6b386297f1e25ccbd74923b4f38c70a",
    "pure-10": "ba2da518724e26df35bae96bd69a462f96bfb9509b8785b6355ebf51e7e8cc4b",
    "pure-11": "cec79c570b3de442677ab90d18f2064bf32e8e113d9200b862dbf6a89254b8f8",
    # pure-12: generate lint+syntax+static merged into the single `Generate.gate` substep — the
    # pure templates renamed the checker-gate anchors (Generate.lint/syntax/static ->
    # Generate.gate's lint/syntax/static check). Checker terminology preserved; substep name only.
    "pure-12": "35239881cab2c79d1f59a52985d21c8cdc330c05c1b2a1f7106ac436b107d11c",
    # pure-13: rule (6) split into (6a) infrastructure neutral-token lowering (trigger corrected
    # — a component public_api no longer implies infrastructure) + (6b) component published-surface
    # pin (operation entrypoints / model subroutines == IR public_api.published_operations).
    "pure-13": "d656ccd45489006020ad09c52abb0b42ed826cd149230fd4c13804cab6344dc5",
    # pure-14: pure prompts and their line-0 sentinel now describe the Codex read-only,
    # structured-output approximation accurately instead of promising tool-free isolation.
    "pure-14": "f15dc94b346844b2523bd38ed6a374abc8073114c103a080db2c6f90e8a6bb87",
    # pure-15: rule (1) states the C131 pair symmetrically — a `public ::` list without the bare
    # `private` fails the lint gate, just as the bare `private` without the list fails the syntax
    # gate. Only the private-alone direction was stated before (issue #12 item 5).
    "pure-15": "2b1c56474c820653bd59681e23a631c2104aea9547569e530f2705b9699a217d",
    # pure-16: new rule (7) states the impl_defaults reflection obligation the producer was
    # punished by but never told (issue #22) — the `abstract` / `backend_overrides` knobs bind, read
    # by MEANING because their spelling varies per node, with the deterministic zero-`!$omp` floor
    # named (scoped to its real trigger) plus the `-fopenmp` and directive-continuation traps. The
    # two headers are reworded to match (rules are no longer all deterministic gates; the Target
    # profile is an obligation, not data). The VERIFY template moves in the same bump: it is the
    # only text the pure reviewer reads, so leaving its G6 unamended would have reproduced issue
    # #22's asymmetry on the reviewer side — it now scopes the floor's guarantee instead of
    # assuming a directive always exists.
    "pure-16": "ad11bb930dad02cf74d16e01e79e37804e05683b7e7281281eec41964cc5f018",
    # pure-17: rule (3) states the THIRD promoted syntax-gate class, `-Werror=ampersand` (issue
    # #25) — a continued character literal must resume with a leading `&`. gfortran accepts a
    # resume line without one as an extension, and that shape put a counted-`do` spelling written
    # inside a string at a physical line start, where the fail_closed OpenMP presence floor
    # counted it and falsely rejected the node. The producer is now told the rule it is judged
    # by, which is the same asymmetry correction pure-16 made for impl_defaults.
    "pure-17": "67de1716fdaa1c5a461015b869bf63e168a31136ea28ce246935678f74a176ec",
    # pure-18: rule (1) names the source of the lint rule set the `Generate.gate` applies. The
    # gate no longer inherits the installed linter's default set — it imposes the set declared in
    # `tools/backends/linter/fortitude/lint.py` (issue #111) — so a producer told "the linter's
    # defaults" was being pointed at something that is no longer what judges it.
    "pure-18": "95ba494864fe7d3042d30434e84fe2adcda80b5f76a190f9a2f08ef50e864e84",
    # pure-19: the `! allow(C003)` workaround is GONE from rule (2) — the lint gate now runs
    # with allow comments disabled and C003 is not in its declared set, so the directive the
    # template used to mandate on every module is itself a finding (FORT005). A producer
    # following pure-18 would now fail the gate it was written to pass.
    "pure-19": "4feebc64731231031adf911097f23e1e3870b1a35e77ccc98f0a1cd810c09079",
    # pure-20: rule (2) no longer promises the producer that an allow directive will be
    # REPORTED. Measured: `FORT005` fires only for a code outside the declared set; a declared
    # code earns `FORT002` on clean source and nothing at all on source that violates it — the
    # case a producer would actually be in. Promising a diagnostic that does not arrive is the
    # oscillation this rule exists to prevent, so the text states the finding fires anyway.
    "pure-20": "0e9b74e2daebbbf24cb14d936bf1b6b356a15e92a1de6a4909fad61900a0a8e5",
    # pure-21: rule (2) names the FLAG (`--ignore-allow-comments`) rather than describing it.
    # The flag literal is what couples the four leaf-read statements of this rule to the code
    # that imposes it: a rename now breaks both together. Measured before it: reversing the
    # prohibition into its opposite in the three agentic sites passed 1294 tests.
    "pure-21": "ce130490f66843a4adae34584710e4de5dba079706f5f5d5e6f0a7789f5cb272",
    # pure-22: rule (1) stated the S001 boundary as a version-independent fact ("fires at exactly
    # 100"). Measured: the comparison is `>=` on 0.8.x and `>` on 0.9.x, both inside the supported
    # range, so the sentence was false on half of it. The instruction ("under 100") was already
    # correct everywhere; what changed is that a producer checking the claim against its own host
    # no longer finds the checklist wrong.
    "pure-22": "771d0659d2341bb372d1dde2c7afd71b2a25d4383ce900ed94c20c828826e94d",
    # pure-23: the project rename (issue #127; `docs/GLOSSARY.md` §13 is canonical for the
    # name and for what it was called before) reached the opening sentence of both pure
    # templates ("the `generate.generate` producer of the atmofab workflow"). No rule, gate, or ABI constant moved. The bump is here because
    # `orchestration_runtime._resolve_exemplar_source` gates prior-art exemplars on this
    # version, and a bundle produced under the old text must not be silently treated as
    # having been produced under the new one; re-pinning in place would have been the
    # reverse-drift hole this file exists to close.
    "pure-23": "6c9d1ced2855e79f709e06122856885c93d9b50670973e8a81d5230ed1a9ec2d",
    # pure-24: the verify template gained a FIFTH data-fenced document,
    # `<checks_module_contract_document>` (§1-4 of docs/workflow/CHECKS_MODULE_CONTRACT.md,
    # sliced host-side), and its scope paragraph now makes that document the authority for what
    # the runner does with each checks-module callback's result (issue #142). A verdict issued
    # under pure-23 was reached WITHOUT the document that defines the ABI it was judging — the
    # observed failure was a `major` against a `case_setup(case_id, ok)` written exactly as the
    # contract specifies — so the two vintages must stay distinguishable. Known side effect (as
    # for every bump), TWO of them: `_resolve_exemplar_source` gates prior-art exemplars on this
    # version, so every sibling exemplar certified at pure-23 or earlier stops being offered
    # (advisory only — the producer takes the ABI from the rendered runner, not from an exemplar);
    # and `validate_pipeline_semantics._validate_orchestration_hierarchy` hard-fails a persisted
    # pure launch row whose `prompt_contract_version` is not the current one, so an orchestration
    # whose `generate` ran under pure-23 cannot be `--resume`d across this bump. Both are inherent
    # to bumping and neither is new; they are named because a bump is where an operator meets them.
    "pure-24": "4ae194a27f650d2edc45ed1d7fc3a77cf1a15a7f5481b058963d13ed2745c751",
    # pure-25: the verify template gained a SIXTH data-fenced document,
    # `<severity_rubric_document>` (the `#### Severity of a finding` subsection of
    # docs/workflow/phases/phase_02_generate.md §2-2, sliced host-side), and its checklist now
    # hands the choice of `issue_severity` to that rubric instead of to the leaf ("fail with the
    # severity the defect warrants" — the wording it replaces) (issue #143). A verdict issued
    # under pure-24 chose the value with no rule to choose it by, and in `dev` the two upper
    # values terminalize the run, so the two vintages must stay distinguishable. The same two
    # known side effects as pure-24 apply, unchanged: `_resolve_exemplar_source` stops offering
    # exemplars certified at pure-24 or earlier, and an orchestration whose `generate` ran under
    # pure-24 cannot be `--resume`d across this bump
    # (`validate_pipeline_semantics._validate_orchestration_hierarchy`).
    "pure-25": "b929666c3119e3e18cd2500e7d1b5691457990f41fa181f0beead402cd364d56",
    # pure-26: the rubric's drop bullet was strictly WIDER than the two statements of the same
    # rule it joins — "the host-rendered runner **or the harness** … with **a value the bundle
    # returns**", against "the runner … with the RESULT a checks-module callback returns" in both
    # the verify template and phase_02 §Generate-executor. A G5 dataflow finding justified by
    # what the harness does with a model-produced value was therefore droppable under the rubric
    # and not under the template, on a document (`CHECKS_MODULE_CONTRACT.md` §2) titled
    # "Semantics the harness relies on". The bullet now names the template's class in the
    # template's words, and a check derives that class FROM the template. The same edit
    # generalizes the `minor` / `critical` subjects from "the bundle" to "the sources under
    # review", because the AGENTIC verify leaf reviews a node whose runner it authored and no
    # bullet's subject named it. A verdict issued under pure-25 was reached against a wider drop
    # class, so the vintages stay distinguishable. Side effects as for every bump, unchanged.
    "pure-26": "f1e92b91e76df84c71b224e873c91246beec63efc894f75f85248db4afabd569",
    # pure-27: the checklist and the rubric disagreed about what the PURE reviewer may fault.
    # The checklist said "verify only the code-vs-IR semantics below" and G1-G7 are all
    # code-subject, while the rubric's `major` requires the subject to be an INPUT — and nothing
    # else in the 217-line prompt mentioned `ir_inconsistency` or told the reviewer it may
    # attribute a defect to the IR at all. The only escape was the rubric's own tie-break, which
    # steers an unsettled subject to `minor`, so `major` was close to unreachable for this
    # persona: an IR that cannot satisfy a checklist item looped the producer to
    # `MAX_ATTEMPTS_PER_PHASE` instead of stopping with a name the operator can act on. (The
    # AGENTIC reviewer never had this: `phase_02` §On-failure behavior states
    # `ir_inconsistency` directly and its SKILL tells it to catch a requirement the
    # `spec.ir.yaml` translation dropped or distorted (SKILL.md:18) — the literal itself
    # occurs 0 times in that SKILL, on this branch and on `origin/main`, and an earlier
    # version of this note said it was there — the issue #22 asymmetry again, roles swapped.) The checklist now says the
    # input-side finding is the reviewer's to make; the `major` bullet's enumeration becomes the
    # cases the question usually takes rather than a closed list, which left a `tests.md`-only
    # defect the IR faithfully reproduces with no value at all.
    "pure-27": "14b882c6af0edf1e8d0e525f43c3bd29dbcb079616f7d73e2809114d892d2ecf",
    # pure-28: `pure-27`'s checklist clause was NARROWER than the rubric bullet it routes to. It
    # licensed one input-side finding — "the IR itself omits or distorts what `controlled_spec.md`
    # or `tests.md` requires" — while the rubric's `major` also covers a faithful IR reproducing a
    # `controlled_spec.md`/`tests.md` CONTRADICTION, which is not an omission or a distortion.
    # Read as an exhaustive permission ("One kind of finding …"), it left the reviewer with a
    # template-authorized route to `pass` on a spec-level contradiction — the zero-work verdict.
    # `pure-27` opened the rubric side of exactly this case and did not open the checklist side.
    "pure-28": "779836ad22d646196b037be001c90ad0a724e2db24ed90814f998de5b4cfe742",
    # pure-29: the SECOND tie-break ordered a verdict the rubric's own `major` bullet forbids.
    # It fires when the unsettled question is about the SOURCES (the `minor`/`critical`
    # boundary), and `pure-27` had made `major` mean "the subject is an INPUT", requiring
    # `last_fail_reason` to name one — so the rubric ordered `major` in exactly the case where
    # the reviewer has no input to name, and `docs/RUNBOOK.md` §3-1 (added by this branch) then
    # tells the operator that such a verdict is a leaf defect. Both sentences were pinned
    # literally, so the contradiction was pinned rather than caught. The tie-break now reads
    # `minor`, which is the same side the first tie-break takes and the side the cost asymmetry
    # argues for: an under-grade spends one repair round, an over-grade ends the `dev` run.
    "pure-29": "fe832b26c532f3aef26bb485e012dcd889e9ec9b45fa25b3be3bd031c0d887e4",
    # pure-30: the checklist stopped RE-ENUMERATING the rubric's `major` cases and defers to it.
    # `pure-27` named one of the four, `pure-28` two — each written to fix the previous one, each
    # read as the exhaustive permission its wording implies ("One kind of finding …"), and each
    # leaving the reviewer a template-authorized `pass` on the cases it omitted. `pure-28`'s
    # blind spot was the sharpest: the same commit added a §3-1 arm BECAUSE the rubric's third
    # case is "`spec.ir.yaml` contradicts the checks-module contract", and did not add that case
    # to the clause it was widening for that reason. Keeping two lists in step is the twin the
    # rubric exists to avoid (`docs/DEVELOPMENT.md` §Design Policy), and the rubric is inlined
    # three paragraphs below the checklist, so the clause now grants the permission and points.
    "pure-30": "7039408f93b3be3e3fa7d075081cea0eae8c95740fdc9f25e55448a359376c82",
    # pure-31 (issue #153 PR-2): a `component` node's IR now carries `public_api.signatures` and
    # `public_api.module_parameters`, so rule (6a)'s neutral-token lowering applies to it and rule
    # (6b) says the NAMES are not the whole surface — the signatures pin each argument's name,
    # order, type, rank and `intent`, compared against the emitted source by the `Generate.gate`
    # static check. Rule (1) consequently splits the `dp` binding by IR shape: a node whose
    # `module_parameters` declares the name must bind it with the parameter DECLARATION
    # (`integer, parameter :: dp = real64`), because that is what the gate value-pins, and the
    # `use`-rename that every physics node used until now declares no parameter and FAILS; a node
    # with no module parameters keeps the rename form. The reason a component gained the keys at
    # all is that an ABI derived post-hoc republished six different argument lists for one
    # `spec_version`.
    # `pure-31` is still the same contract change and has not shipped; round 1 added the `dims`
    # lowering rule to it, which is part of the same "a component's IR now carries signatures"
    # change rather than a second one. Round 1 measured what its absence cost: a leaf following rule
    # 6b's enumerated fields exactly, and rendering rank as assumed-shape (which is what
    # `docs/CONTROLLED_SPEC.md` named as the Fortran binding), earns 3 refusals on the flux
    # component — `dims` is load-bearing in 5 of the 6 new §5.1 blocks and was documented nowhere a
    # leaf or a spec author reads.
    # Round 2 folds in one more sentence for the same reason, and it is the same rule again: rule
    # (1) now says the pinned name must be declared EXACTLY ONCE, a second declaration inside a
    # contained procedure included. That closed a measured fail-open — a module-level binding to a
    # narrower kind with the pinned text shadowed in a procedure passed a presence check while
    # publishing single precision — and a leaf judged by uniqueness but never told about it would
    # have spent an attempt on a refusal its instructions did not state.
    # Round 4 folds in one more, same contract change, same rule: the published HEADER is compared
    # as emitted, so a procedure prefix the signature does not carry is a drift. Measured as an
    # over-refusal with a MISROUTING message — it blamed argument drift for a source whose arguments
    # were all correct — and the construct occurs in the corpus today, so a generator writes it
    # unprompted. The six specs already said so; a leaf does not read specs.
    "pure-31": "404fed5ddc0077954b8b38e5294a7fd3a56d22064f87430824b2ad4db27dc37f",
    # pure-32 (issue #168, Z1): the pure transport gained TWO templates — `pure_compile_generate`
    # (the IR producer: one JSON document `{ir, last_fail_reason}`) and `pure_compile_verify`
    # (the IR reviewer, returning the same verdict document the generate reviewer returns) — so
    # `compile.generate` and `compile.verify` now run as pure leaves instead of agentic CLI
    # sessions. Nothing in the four previously pinned members moved; the digest changes because
    # `_TEMPLATE_FILES` grew by two. The usual two side effects apply and are not new: exemplars
    # certified at pure-31 or earlier stop being offered, and an orchestration whose `generate`
    # ran under pure-31 cannot be `--resume`d across this bump.
    "pure-32": "a8872df07ad2c2e9fed855f335e17d99e143c01f668b2d515cda40d739c72441",
    # pure-33 (issue #168, round 3): the IR producer's output contract now STATES two host
    # refusals it did not. Round 3's disclosure axis rendered the real prompt and compared it
    # against `_pure_ir_document_violations`: the `ir.meta.spec_kind`-equals-the-node's-kind
    # clause and the serialize/parse round trip were both enforced and undisclosed, so a leaf
    # met them as a repair turn out of a budget of two, and the round-trip finding named no
    # path to act on. A refusal the leaf is never told about is a turn it cannot converge on.
    "pure-33": "1bc1346d5919215eb7abfc28aa25534645ed876101716da15f8e1c15396915ab",
    # pure-34 (issue #168, round 5): the reviewer template said the phase contract's verify
    # section "states them as a numbered list, and that list is the whole of your scope". The
    # inlined document states that scope FOUR ways — the verify section (V1-V5, V8), a
    # §substep-structure summary naming three of them, a §fixed/knob sentence assigning V6/V7,
    # and this template — so a sentence meant to bound the reviewer instead made the document's
    # own disagreement load-bearing, and the NARROWEST reading drops V2. Measured by the round-5
    # security axis: stripping every `algorithm.steps[].description` from a real certified IR
    # still PASSES `--stage compile`, so V2 at `compile.verify` is the only thing between a
    # de-mathed IR and a `Generate` leaf that cannot read `controlled_spec.md`. The template now
    # refuses every narrower reading BY NAME, including its own sentence.
    "pure-34": "6625f0468c2ac988c31d3cfab38d1a1565933f213d9d7427bb4024cb29a9dd79",
    # pure-35 (issue #169, PR-1): the escalate diagnostician moved onto the pure transport, so
    # `pure_escalate_diagnose.txt` joins the coupled tuple. Its persona, its directive schema
    # and its decision criteria used to live half in `workflow_conductor` constants and half in
    # `skills/workflow-escalate/SKILL.md` (read host-side, hashed by nothing); the template is
    # now the single source and this guard is what makes an edit to it an observable event.
    # The digest below is what three review rounds settled on, after corrections to text a leaf
    # ACTS on: the null-`target_phase` rule (`_parse_directive` REFUSES a null target under
    # `action="reopen"` and discards the whole directive, where the template had said only
    # that null means the current phase), and the untrusted-data instruction its sibling
    # `pure_bundle_repair.txt` carries — the inlined `diagnosis_document` quotes artifact
    # content VERBATIM, some of it written by the agents whose work failed, one of which has an
    # interest in the rollback the diagnostician picks. The provenance is MIXED, not
    # agent-only: `_gather_failure_context` also inlines host-written gate metas, and an
    # earlier version of this comment (and of the template) said the artifacts were all
    # agent-written, which would tell the leaf its firmest evidence is adversarial. pure-35 was
    # introduced by this branch and has never been recorded by a run, so re-pinning it is not
    # editing a historical entry.
    "pure-35": "fa5612cce3f15740f8c5f37a182e2fecd435863ec727e58a6111825dd0fb8a49",
    # pure-36 (issue #169, PR-2): `validate.judge` moved onto the pure transport, so
    # `pure_validate_judge.txt` joins the coupled tuple — and with it the §1 + §3 slice of
    # `RUNNER_OUTPUT_CONTRACT.md`, which is inlined into that prompt verbatim. The judge is the
    # leaf that used to hold tools, and what it used them for was to walk `raw/` with scripts it
    # wrote per run; its template now tells it that the host-computed excerpt is its whole view
    # of that evidence, which is a contract change in the strongest sense — the same leaf, asked
    # the same question, reasoning from a different input. pure-36 was introduced by this branch
    # and has never been recorded by a run — which is also why its digest is RE-PINNED rather
    # than superseded by a `pure-37`: a review round measured the `notes` cap this template
    # states to the leaf (4,000 chars) against the population it has to serve and found it
    # already below a recorded review's real `notes` field (4,272; the corpus runs to 9,773),
    # so the cap moved to 12,000 in the template and in the validator. Re-pinning an entry no
    # run has ever recorded is not editing a historical entry; `pure-37` stays free for PR-3.
    # Re-pinned a second time, in review round 2: the template told the judge that an
    # `all_zero` array is "the shape an unwritten one takes" and therefore a fabrication to
    # fail on. Measured against the corpus, that is false and live — `run_20260802_001`, which
    # the agentic judge certified `pass`, carries 20 all-zero arrays, every one a flat bed or a
    # zero transverse momentum. A compliant judge following that sentence would have failed
    # sound runs, which the billed A/B would have surfaced as a spurious regression.
    # Re-pinned a third time, in review round 3, and the reason is the mirror of round 2's.
    # Round 2 removed the clause calling an all-zero array a fabrication; the DISCLOSURE axis
    # rendered the prompt and read it as the judge, and found the clause beside it doing the
    # same damage: "a metric the excerpt cannot support" is a fail, while round 1 had measured
    # that 16 of 17 metrics on the reference node cannot be supported at any value. A compliant
    # judge had to fail every run in the corpus. The checklist now distinguishes CONTRADICTED
    # (a finding) from not-corroborated (expected, and never a finding), states the asymmetry
    # and the 1-of-17 figure so the leaf knows its window is deliberately narrow, and restores
    # `nan_count` / `inf_count` / `ragged` / `shape` as defects on their own — round 2's own
    # correction had swept them into the all-zero carve-out, where nothing else in the workflow
    # would have caught them. The inlined `RUNNER_OUTPUT_CONTRACT.md` slice moved too: §3
    # promised the judge a "per-test recomputation" it no longer performs.
    # Re-pinned a fourth time, in review round 4, which rendered the prompt on two real nodes
    # and read it as the judge. Three defects, all of them in text the leaf acts on:
    #   * the slot-name instruction was FALSE — the labels read "**Tests (…):**", so ten of the
    #     eleven `<document_key>` names appeared ZERO times in the rendered prompt and the leaf
    #     had to guess them from one example. `evidence_refs` exists only inside `findings`, so
    #     the guess is only ever made on a FAIL: the cost fell entirely on the correct-but-
    #     expensive verdict. Every label now opens with its literal key.
    #   * checklist (c) said "a missing declared variable is a fail". The excerpt's benign
    #     `declared_variables_absent` (per-snapshot; non-empty on THREE cases of a
    #     `pass`-certified node) matches that prose better than the intended
    #     `coverage.missing_required_variables` does. Both are now named, and the neighbouring
    #     keys that are not findings are named as not-findings.
    #   * (b) ordered a `shape` vs `shape_expr` comparison that is not evaluable: `nx_face`
    #     occurs nine times in the prompt and never with a value. Scoped to RANK, with an
    #     explicit instruction not to infer a binding.
    # The inlined `RUNNER_OUTPUT_CONTRACT.md` §3 clause moved again: round 3's fix had removed
    # the only sentence telling the runner-authoring leaf that the VALUES are consumed, while
    # `post_execute` checks key presence only — a shortcut this branch opened and now closes.
    # Re-pinned a fifth time, by round 4's blank-slate axis: checklist (c) told the judge that
    # a non-empty `coverage.missing` / `missing_required_variables` is its own `attribution=code`
    # finding, while `--stage post_execute` refuses ALL THREE coverage conditions before a judge
    # is launched — through the very function this branch moved into the excerpt module. The
    # clause claimed a role the gate owns, in the same paragraph where (f) tells the leaf the
    # gates are the authority for what they check. It now says these are gate-owned, that a
    # non-empty one means the run reached the judge in a state the gate should have refused, and
    # that it is an `attribution=evidence` integrity signal rather than the judge's contribution.
    "pure-36": "b90892edf00f9fcef3320d8b17ed29f053bcd64b00606fa774f07c77842137dc",
    # pure-37 (issue #169, PR-3): the second GENERATE bundle SHAPE. `_TEMPLATE_FILES` grew by two
    # — `pure_generate_generate_harness.txt` / `pure_generate_verify_harness.txt`, the producer
    # and reviewer an `infrastructure` self-test now runs as a pure leaf, where before it was the
    # last live fall-through to the agentic loop. The two side effects are the usual ones and are
    # not new: an exemplar certified at pure-36 or earlier stops being offered, and an
    # orchestration whose `generate` ran under pure-36 cannot be `--resume`d across this bump.
    # The coupled tuple also gained a member: the WHOLE `RUNNER_OUTPUT_CONTRACT.md`, which those
    # two templates inline (the §1+§3 slice the judge sees stays its own member). Re-pinned once
    # inside the same version, by round 1: the producer template was missing the model MODULE
    # name obligation (only the FILE name was stated, and the host only checked the file name),
    # and its `state_bindings` line invited a binding this shape can never satisfy. Re-pinned a
    # THIRD time, by round 2: the reviewer template's checklist preamble sat in separate `\n\n`
    # blocks, so `PURE_REPAIR_STATIC_PARAGRAPH_PREFIXES`' lift carried the header and none of
    # H1-H10 into a cold repair; the paragraphs are folded into one and two producer-side
    # prefixes were added beside them. And a FOURTH time, by round 3, which rendered the prompt
    # and looked for the deterministic gate's rule set: making this leaf pure had cut the only
    # carrier it had (a force-read `CHECKS_MODULE_CONTRACT.md` §5, which a pure leaf does not
    # read), so §5 is now inlined and joins the tuple as its own member. A FIFTH time, by round
    # 4, which measured what that inline actually covers: §5 states three of the seven rule codes
    # the leaf lost, so the rule that pointed at it claimed a completeness it does not have, and
    # the escape hatch beside it did not cover a §5 clause naming procedures of the §1-§4 ABI
    # that this shape has no file for. A SIXTH and last time, closing that gap at the operator's
    # decision: the linter backend now renders its declared rule set for a leaf
    # (`lint.lint_rules_document`), the host inlines it, and it joins the tuple — so adding a
    # code to `RULE_CODES` is a leaf-contract change rather than a silent widening. A SEVENTH
    # and final time, by round 5: with two inlined rule sets the precedence sentence's "this
    # list" no longer had one antecedent, and the reading that made the prose section govern the
    # lint set would have dropped exactly the four codes the previous re-pin delivered. The
    # precedence is now stated by NAME in all three directions. The m3c template's own version
    # string moved with it — it still said 1.0.0, so the two producer templates were telling
    # their leaves two versions of one contract.
    "pure-37": "5ad32d360a1522cae8abedab657d9e15977d9dca357bb029f210b48f7ea88c9f",
    # issue #175: compile.generate rule 3 now sources `direct_deps` from the graph
    # document (a `profile` entry in deps.yaml is not a node), rule 9 pins
    # `profile_selection`, and the profile-document heading says the selected component
    # set is already resolved.
    "pure-38": "3d0d113a217a555a254a9dfaea45053209c2b5d12933fbf8ecc012addf62417d",
    # issue #175 Part B: the generate pair loses every `profile` clause that named a
    # `profile` NODE (the cardinality ladder rung, the module-parameter and `public_api`
    # carve-outs, the OpenMP floor's exemption) — a `profile` is host-resolved at Compile
    # and can be neither a dependency node nor an optimization-unit member. Two rules are
    # UNCHANGED from `pure-38` and must not be read out of this entry as deletions. The
    # inert dependency-call rule: an earlier revision of this branch deleted it, and review
    # measured that three deterministic gates force a leaf into exactly the shape it
    # describes, so it was restored. G2/H2's runtime-input minimum, including the
    # `profile`/`component` selection result: three successive revisions tried to except a
    # two-key `inputs.profile_selection` from it, each on a premise measurement then
    # falsified — the last of them by a certified `pass` bundle
    # (`shallow_water2d_checks.f90`) that reads exactly those two keys as a per-case guard.
    # The exception was abandoned rather than rewritten a fourth time; a `profile_selection`
    # a runner ignores is a `fail` here, as it was before issue #175.
    "pure-39": "6a73aa8bc9f2bc56c18d08d4160e335cecc38f2401e696cb11f343256cdb2a26",}


def _contract_tuple() -> dict[str, object]:
    tpl_dir = Path(ort.__file__).resolve().parent / "prompt_templates"
    return {
        "templates": {
            name: (tpl_dir / name).read_text(encoding="utf-8") for name in _TEMPLATE_FILES
        },
        "system_prompt": PURE_SYSTEM_PROMPT,
        "repair_static_prefixes": list(ort.PURE_REPAIR_STATIC_PARAGRAPH_PREFIXES),
        "checks_public_names": list(rr.CHECKS_PUBLIC_NAMES),
        "check_status_width": rr.CHECK_STATUS_WIDTH,
        # The §1-4 SLICE of the checks-module contract, not the file: since issue #142 those
        # sections are inlined verbatim into the reviewer's prompt, which puts them under this
        # pin's own stated bar (a stable, behavior-defining leaf INPUT) exactly as the template
        # bytes are. Hashing the slice rather than the whole document keeps §5 — the deterministic
        # gate section the reviewer is told not to re-check — out of the tuple, so an edit there
        # is not a spurious bump; and it makes the slicer's OWN behaviour part of the contract,
        # closing the gap `_checks_contract_abi_sections`' docstring used to record: moving an
        # anchor, or a document renumbering that silently re-slices, changes the digest.
        "checks_contract_abi_sections": wc._checks_contract_abi_sections(
            (Path(wc.__file__).resolve().parents[1]
             / "docs" / "workflow" / "CHECKS_MODULE_CONTRACT.md").read_text(encoding="utf-8")),
        # The severity-rubric SLICE of phase_02_generate.md, on the same ground and with the same
        # scoping (issue #143): since `pure-25` the `#### Severity of a finding` subsection is
        # inlined verbatim into the reviewer's prompt and is the sole rule for the
        # `issue_severity` a verdict carries, so it is a leaf INPUT, not a document the leaf
        # reads. Hashing the slice keeps G1-G7 and the phase's retry policy out of the tuple —
        # editing a checklist item is not a spurious bump — and makes
        # `_generate_verify_severity_rubric_section`'s own anchors part of the contract.
        "generate_verify_severity_rubric_section": wc._generate_verify_severity_rubric_section(
            (Path(wc.__file__).resolve().parents[1]
             / "docs" / "workflow" / "phases" / "phase_02_generate.md").read_text(
                encoding="utf-8")),
        # The §1 + §3 SLICE of the runner-output contract, on the same ground as the two above
        # (issue #169): since `pure-36` those sections are inlined verbatim into the pure
        # `validate.judge` prompt, so they are a leaf INPUT rather than a document the leaf
        # reads. Hashing the slice keeps §2, §4 and §5 out of the tuple — the judge is not shown
        # them — and makes `_runner_output_contract_sections`' own anchors part of the contract,
        # which matters more here than for the other two because this slicer takes TWO ranges
        # and a silent widening would swallow §2 between them.
        "runner_output_contract_sections": wc._runner_output_contract_sections(
            (Path(wc.__file__).resolve().parents[1]
             / "docs" / "workflow" / "RUNNER_OUTPUT_CONTRACT.md").read_text(encoding="utf-8")),
        # ...and the WHOLE of the same document, because since `pure-37` the `harness` bundle
        # shape's two `generate` prompts inline all of it (issue #169): a leaf that AUTHORS the
        # program reads every section, where the judge that only reads its output is shown two.
        # The slice above stays a separate member deliberately — it is what the judge sees, and
        # a widening of `_runner_output_contract_sections` must still be visible as a change to
        # THAT member rather than be absorbed by the whole-document one.
        #
        # This is the one document treated as a leaf CONTRACT rather than as leaf INPUT DATA
        # (the disposition `phase_01_compile.md` and `controlled_spec.md` take above), and the
        # churn measurement is why: 11 commits over 8 distinct days all-time, 3 since
        # 2026-07-19 — measured at `ed36c77` with
        # `git log --oneline -- docs/workflow/RUNNER_OUTPUT_CONTRACT.md | wc -l`, the same log
        # with `--date=short --format=%ad | sort -u | wc -l`, and again with `--since=2026-07-19`.
        # A document that stable does not make the bump a churn magnet, and it additionally
        # carries a size ceiling (`test_orchestration_runtime.ChildContextDocSizeTests`), so it
        # is edited deliberately. The known cost is the usual pair: a bump stops
        # `_resolve_exemplar_source` offering earlier-version exemplars and refuses `--resume`
        # across it.
        "runner_output_contract_document": (
            Path(wc.__file__).resolve().parents[1]
            / "docs" / "workflow" / "RUNNER_OUTPUT_CONTRACT.md").read_text(encoding="utf-8"),
        # §5 of the checks-module contract, on the same ground as the §1-§4 slice beside it: it
        # is inlined verbatim into the `harness` producer's prompt, so it is a leaf INPUT rather
        # than a document the leaf reads. Hashing the SLICE keeps §1-§4 out of this member (they
        # are already their own) and makes the section's CONTENT part of the contract.
        # WHAT THIS DOES NOT PIN, corrected by round 4 after the first version of this comment
        # claimed it did: the slicer's REFUSALS. A digest over the slice of today's document —
        # which carries no `## 6.` — cannot see a guard against one, and deleting that guard left
        # the whole suite green. `test_pure_leaf_producer.PureHarnessShapeTests` drives both
        # refusals on synthetic text, and pins the slice's identity against
        # `_checks_contract_abi_sections` so swapping the two is red.
        # The static lint rule set as the harness producer is shown it. A leaf INPUT like the
        # slices beside it, and the one member that comes from a BACKEND rather than a document:
        # adding a code to `RULE_CODES` changes what the leaf is told, which is a contract change
        # and has to bump the version rather than ship silently.
        "lint_rules_document": _fortitude_lint.lint_rules_document(),
        "checks_contract_gate_guards_section": wc._checks_contract_gate_guards_section(
            (Path(wc.__file__).resolve().parents[1]
             / "docs" / "workflow" / "CHECKS_MODULE_CONTRACT.md").read_text(encoding="utf-8")),
    }


def _digest() -> str:
    payload = json.dumps(_contract_tuple(), sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class PurePromptContractDriftTests(unittest.TestCase):
    def test_current_version_is_pinned_and_matches(self) -> None:
        computed = _digest()
        resolution = (
            f"\n\nRecomputed digest: {computed}\n"
            "Resolve ONE of two ways:\n"
            f"  (1) INTENTIONAL contract change: bump PURE_PROMPT_CONTRACT_VERSION (tools/pure_leaf.py), "
            "KEEP the existing PINNED entries, and add PINNED['<new-version>'] = '" + computed
            + "' here (its digest must differ from every existing pin — else it is an empty bump).\n"
            "  (2) UNINTENTIONAL drift: revert the edit to the pinned surface "
            f"({len(_TEMPLATE_FILES)} pure_*.txt templates, "
            "PURE_REPAIR_STATIC_PARAGRAPH_PREFIXES, the "
            "language backend runner's checks-ABI constants, or an inlined document slice — "
            "CHECKS_MODULE_CONTRACT.md §1-4, phase_02_generate.md's severity rubric)."
        )
        self.assertIn(
            PURE_PROMPT_CONTRACT_VERSION, PINNED,
            f"PURE_PROMPT_CONTRACT_VERSION={PURE_PROMPT_CONTRACT_VERSION!r} has no PINNED entry."
            + resolution,
        )
        self.assertEqual(
            computed, PINNED[PURE_PROMPT_CONTRACT_VERSION],
            f"pure prompt contract digest for {PURE_PROMPT_CONTRACT_VERSION!r} does not match the pin."
            + resolution,
        )

    def test_no_empty_version_bump(self) -> None:
        # Every pinned version's digest must be UNIQUE. A genuine contract bump changes the tuple and
        # therefore the digest; a new version whose digest equals an earlier one is an EMPTY version
        # bump (unchanged contract), which the no-empty-version-bump policy rejects. Without this,
        # bumping the version literal and copying the failure message's recomputed (unchanged) digest
        # into a new PINNED entry would pass — the reverse-drift hole.
        by_digest: dict[str, list[str]] = {}
        for version, digest in PINNED.items():
            by_digest.setdefault(digest, []).append(version)
        collisions = {d: vs for d, vs in by_digest.items() if len(vs) > 1}
        self.assertEqual(
            collisions, {},
            "empty version bump detected — these versions share an identical contract digest, so "
            f"their contract tuple is unchanged: {collisions}. A version bump must change the "
            "contract (templates / PURE_SYSTEM_PROMPT / coupled ABI constants); do not add a new "
            "version whose digest duplicates an earlier one.",
        )


class TemplateGateParityTests(unittest.TestCase):
    """The `pure_generate_generate.txt` sentences S1-S3 distil constants that live in
    `codegen_bundle.py`. If a constant moves and the prompt does not, the leaf is told to
    emit a value the gate no longer accepts — the exact E2E#7 failure mode. These assert the
    template's distilled surface still agrees with the gate's live constants.

    The pin members are IMPORTED production constants and the template file bytes only
    (per the drift-guard scoping above); this class adds no test-local copy of a gate value.
    """

    @staticmethod
    def _generate_template_bytes() -> str:
        tpl_dir = Path(ort.__file__).resolve().parent / "prompt_templates"
        return (tpl_dir / "pure_generate_generate.txt").read_text(encoding="utf-8")

    def test_template_names_every_state_residency(self) -> None:
        template = self._generate_template_bytes()
        for residency in cb.STATE_RESIDENCIES:
            self.assertIn(
                residency, template,
                f"state_residency {residency!r} (cb.STATE_RESIDENCIES) is not named in "
                "pure_generate_generate.txt — S2 has drifted from the gate enum.")

    def test_template_capability_tokens_are_all_manifest_provided(self) -> None:
        # Every `<name>@<version>` token the prompt shows as an example must be one the
        # harness manifests actually provide; otherwise the prompt points the leaf at a
        # capability the gate rejects as unavailable.
        template = self._generate_template_bytes()
        provided = set().union(*cb.HARNESS_CAPABILITY_MANIFESTS.values())
        tokens = set(re.findall(r"[a-z][a-z0-9_]*@[0-9]+", template))
        self.assertEqual(
            tokens - provided, set(),
            "pure_generate_generate.txt names capability tokens the harness manifests do not "
            f"provide: {sorted(tokens - provided)} (provided: {sorted(provided)}).")


if __name__ == "__main__":
    unittest.main()
