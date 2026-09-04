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

The pin set is deliberately NARROW (a churn magnet if widened): the three template files, the
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
from tools.pure_leaf import PURE_PROMPT_CONTRACT_VERSION, PURE_SYSTEM_PROMPT

_TEMPLATE_FILES = (
    "pure_generate_generate.txt",
    "pure_generate_verify.txt",
    "pure_bundle_repair.txt",
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
    "pure-31": "4e963d99596eae3353a1b3be471b5386b68a492017016c2ee8e038f763f7ac7f",}


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
            "(the three pure_*.txt templates, PURE_REPAIR_STATIC_PARAGRAPH_PREFIXES, the "
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
