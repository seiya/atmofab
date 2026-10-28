#!/usr/bin/env python3
"""The `generate.verify` reviewer (M-D; a READING workshop leaf since issue #534 PR-J4-b).

Covers the host side of the `generate.verify` channel in `tools/workflow_conductor.py` (the
reviewer loop `_run_pure_reviewer_substep`, verdict validation, the source_meta.json
projection, verdict_meta, the classify_failure verdict route) and the `tools/run_workflow.py`
gate removal. The loop is driven through its workshop seam (`test_workshop_reviewer`'s
`_ReviewerFake`, via `_reviewer_conductor` below): the leaf writes its verdict into the
lineage's tree. Mirrors the producer suite (`test_pure_leaf_producer.py`), whose fixtures it
reuses.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("ATMOFAB_DEP_READINESS_ALLOW_PERSISTED_FALLBACK", "1")

import tools.orchestration_runtime as wc_ort
import tools.workflow_conductor as wc

#: The target language's `bundle_facts`, which names a node's sources for
#: `phase_required_outputs("generate")` since issue #289 (R4-b PR-3).
from tools.backends import registry as _bb_registry  # noqa: E402
from tools.tests.target_fixtures import FORTRAN_CPU as _BB_PROFILE  # noqa: E402
_FORTRAN_BUNDLE_FACTS = _bb_registry.capability_module(
    "language", _BB_PROFILE.toolchain["language"], "bundle_facts")
from tools.pure_leaf import PURE_PROMPT_CONTRACT_VERSION, VERDICT_SEVERITIES
from tools.tests.test_pure_leaf_producer import (
    _NODE, _write_node, _PureFakeConductor, _valid_bundle, _envelope, _conductor,
)
from tools import usage_reset
from tools.tests.llm_samples import sample_config_with as _cfg
from tools.tests.private_root_fixture import (
    refuse_provider_probes_for_module,
    restore_provider_probes_for_module,
)


def setUpModule() -> None:
    # Outside pytest conftest does not load, so this module refuses a real reset probe itself
    # (issue #405; `private_root_fixture.refuse_provider_probes_for_module`).
    refuse_provider_probes_for_module(__name__)


def tearDownModule() -> None:
    restore_provider_probes_for_module(__name__)


def _verdict(status: str = "pass", *, severity: str | None = None,
             reason: str | None = None, findings: list | None = None) -> dict:
    if status == "pass":
        return {"verification_status": "pass", "issue_severity": "none",
                "last_fail_reason": None, "findings": []}
    return {"verification_status": "fail", "issue_severity": severity or "major",
            "last_fail_reason": reason or "generated model diverges from the controlled spec",
            "findings": findings or [{"summary": "mass is not conserved"}]}


# The two real documents the reviewer context slices, copied into each fixture repo. Deliberately
# the REAL documents and not synthetic stand-ins: every reviewer-loop test below renders the live
# slices, so a reorganization either slicer cannot cut turns this whole file red — the early
# warning that a billed run would otherwise be the first to give. `ChecksContractSlicerTests` and
# `SeverityRubricSlicerTests` cover the two helpers' own contracts on synthetic text.
_REAL_CHECKS_CONTRACT = (
    Path(wc.__file__).resolve().parents[1] / "docs" / "workflow" / "CHECKS_MODULE_CONTRACT.md")
_REAL_PHASE_02 = (Path(wc.__file__).resolve().parents[1]
                  / "docs" / "workflow" / "phases" / "phase_02_generate.md")


def _verify_node(repo: Path, *, stage_checks_contract: bool = True,
                 stage_phase_02: bool = True) -> wc.NodeRefs:
    """A pure node with the producer's codegen_bundle.json already on disk (the reviewer input),
    and — unless the corresponding `stage_*` argument is False — the repository's checks-module
    contract and phase_02 contract staged where `_build_pure_verify_context` reads them."""
    refs = _write_node(repo)
    src = repo / refs.source_dir()
    src.mkdir(parents=True, exist_ok=True)
    (src / "codegen_bundle.json").write_text(json.dumps(_valid_bundle()), encoding="utf-8")
    (repo / refs.spec_path).mkdir(parents=True, exist_ok=True)
    (repo / refs.spec_path / "controlled_spec.md").write_text(
        "# §5.1 interface\nconserves mass.\n", encoding="utf-8")
    if stage_checks_contract:
        dest = repo / "docs" / "workflow" / "CHECKS_MODULE_CONTRACT.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_REAL_CHECKS_CONTRACT, dest)
    if stage_phase_02:
        dest = repo / "docs" / "workflow" / "phases" / "phase_02_generate.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_REAL_PHASE_02, dest)
    return refs


# ======================================================================================
# The reviewer loop's workshop seam (issue #534 PR-J4-b: every reviewer is a READING workshop
# leaf, so these rows drive `_run_pure_reviewer_substep` through it)
# ======================================================================================
def _reviewer_fake() -> type:
    """`test_workshop_reviewer._ReviewerFake`: the reviewer loop's workshop seam — the lineage
    home faked, no wrapper, the leaf scripted by `leaf_actions`. Imported late, because that
    module imports this one's `_verdict` / `_verify_node`."""
    from tools.tests.test_workshop_reviewer import _ReviewerFake
    return _ReviewerFake


def _leaf_action(item):
    """One scripted reviewer launch from what the pure rows scripted.

    A launch that exits 0 (a CLI stdout text, or a rc-0 `ProcResult` carrying one) WRITES the
    document its envelope's `result` carries, verbatim, into the lineage tree under the
    reviewer's output name (`verdict.json`; `review.json` for the judge) — on the workshop path
    the tree is the answer, and the envelope is what the CLI prints after such a session, read
    by the host for the model, the usage and the session id only. Any other `ProcResult` (a
    dead or killed leaf) writes nothing and is the launch's result."""
    def action(c, tree, kwargs):
        proc = item if isinstance(item, wc.ProcResult) else wc.ProcResult(0, item, "")
        if proc.returncode == 0 and not proc.timed_out:
            try:
                result = json.loads(proc.stdout).get("result")
            except (ValueError, AttributeError):
                result = None
            if isinstance(result, str):
                request = [cap for sub, cap in c.calls
                           if sub == "record-launch"][-1]["--request-json"]
                name = "review.json" if request["step"] == "validate" else "verdict.json"
                (tree / name).write_text(result, encoding="utf-8")
        return proc
    return action


def _reviewer_conductor(repo: Path, script, *, cls: type | None = None, llm_config=None,
                        **kw):
    """A reviewer fake (`cls`, a subclass of `_reviewer_fake()`, or that class) over `repo`,
    scripted by `script` (`_leaf_action` items; the last repeats). Backoff sleeps are recorded
    on `c.slept`, not slept."""
    (repo / "workspace" / "orchestrations" / "o").mkdir(parents=True, exist_ok=True)
    c = (cls or _reviewer_fake())(
        repo_root=repo, orchestration_id="o", orchestration_agent_run_id="orch",
        llm_config=llm_config if llm_config is not None else _cfg("claude"), env={}, **kw)
    c.calls = []
    c.homes = repo / ".homes"
    c.leaf_actions = [_leaf_action(item) for item in script]
    c.slept = []
    c._sleep_backoff = c.slept.append  # type: ignore[method-assign]
    return c


def _run_verify(c, refs: wc.NodeRefs) -> wc.SubstepOutcome:
    """`generate.verify`'s reviewer loop with the node's own spec — what the deleted seam
    `_run_pure_verify_substep` bound, and what `run_substep` dispatches a judging pair to."""
    return c._run_pure_reviewer_substep(
        refs, "generate", "verify", (),
        c._pure_reviewer_spec("generate", "verify", c._shape_for_spec(refs, "generate")))


def _spawned(c) -> int:
    return len(c.__dict__.get("spawns", []))


def _requests(c) -> list[dict]:
    return [cap["--request-json"] for sub, cap in c.calls if sub == "record-launch"]


# ======================================================================================
# _build_pure_verify_context
# ======================================================================================
class PureVerifyContextTests(unittest.TestCase):
    def test_context_has_the_six_verify_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _verify_node(repo)
            ctx = _conductor(repo)._build_pure_verify_context(refs)
            self.assertEqual(set(ctx),
                             {"controlled_spec_document", "tests_document",
                              "ir_document", "target_profile",
                              "checks_module_contract_document",
                              "severity_rubric_document", "bundle_document"})
            self.assertIn("conserves mass", ctx["controlled_spec_document"])
            # The reviewer judges G6 against the run's target (issue #284): the whole profile,
            # the same document the producer is shown.
            conductor = _conductor(repo)
            self.assertEqual(json.loads(ctx["target_profile"]), conductor.target.doc)
            self.assertIn("bundle_schema_version", ctx["bundle_document"])

    def test_checks_contract_document_is_sections_1_to_4_of_the_real_doc(self) -> None:
        # issue #142: the reviewer receives the ABI half of the contract and nothing else. What is
        # pinned is the SPAN — §1 opens it, §4's content is inside, §5 and the preamble are out.
        # Since issue #289 (R4-b PR-2) the span is TWO documents in order: §1-§4 of the neutral
        # contract, then §1-§4 of the target language's binding of it — and §5 of neither.
        from tools.backends.language.fortran import checks_abi
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _verify_node(repo)
            doc = _conductor(repo)._build_pure_verify_context(
                refs)["checks_module_contract_document"]
            self.assertTrue(doc.startswith("## 1. The fixed ABI\n"), doc[:80])
            neutral_end = doc.index("## 1. The fixed ABI in Fortran")
            for present in ("## 2. Semantics the harness relies on",
                            "## 3. Module-level state is expected",
                            "## 4. Prohibitions",
                            "The runner does not read `ok`",
                            "runner always captures the case's state"):
                self.assertIn(present, doc[:neutral_end])
            for present in ("### 1-b. The bound state in Fortran",
                            "## 2. The semantics, spelled in Fortran",
                            "## 4. Prohibitions in Fortran",
                            "logical, intent(out) :: ok",
                            "character(len=4), intent(out) :: status"):
                self.assertIn(present, doc[neutral_end:])
            # Issue #399: the wording that gave a value of `ok` a meaning is gone from the
            # reviewer's document (the sentence-level rule is
            # `test_checks_module_loops_and_ok_are_not_findings_on_every_target`).
            for refused in ("rejects a guard", "`ok` false", "ok = .false.", "ok=.false.",
                            "returned `ok`", "returning `ok"):
                self.assertNotIn(refused, doc)
            # Every literal here must occur in the REAL documents, or the assertion is true of
            # any slice and pins nothing — an earlier version of this test named a preamble
            # sentence the same commit had rewritten away.
            real = _REAL_CHECKS_CONTRACT.read_text(encoding="utf-8") + checks_abi.document()
            for absent in ("## 5.",
                           "Fortran legality and gate guards",
                           "## 5. Language binding",
                           "# Checks-module contract",
                           "# Checks-module ABI",
                           "NO leaf reads this document from disk"):
                self.assertIn(absent, real, f"{absent!r} no longer occurs in the documents, so "
                                            f"asserting its absence from the slice pins nothing")
                self.assertNotIn(absent, doc)

    def test_missing_checks_contract_raises_the_named_contract(self) -> None:
        # Raises, as the four node artifacts do since issue #467. Measured in issue #169's
        # review: a whitespace-only `pure_context` value never reaches the leaf — the launch
        # validator counts it missing and raises — so degrading only defers the refusal one
        # frame, into `record_launch`, where it escapes the loop's named branch.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _verify_node(repo, stage_checks_contract=False)
            with self.assertRaises(RuntimeError) as cm:
                _conductor(repo)._build_pure_verify_context(refs)
            self.assertIn("pure_checks_contract_document_missing", str(cm.exception))

    def test_each_unreadable_node_artifact_raises_its_named_document(self) -> None:
        """Issue #467: the builder-level rows for the four node artifacts, plus one undecodable
        bundle — `UnicodeError` takes the same named path as a missing file — and one
        whitespace-only IR."""
        rows = (
            (lambda r: f"{r.spec_path}/controlled_spec.md",
             "pure_controlled_spec_document_missing"),
            (lambda r: f"{r.spec_path}/tests.md", "pure_tests_document_missing"),
            (lambda r: f"{r.ir_ref}/spec.ir.yaml", "pure_ir_document_missing"),
            (lambda r: f"{r.source_dir()}/codegen_bundle.json", "pure_bundle_document_missing"),
        )
        for rel_of, marker in rows:
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp)
                refs = _verify_node(repo)
                (repo / rel_of(refs)).unlink()
                with self.assertRaises(RuntimeError) as cm:
                    _conductor(repo)._build_pure_verify_context(refs)
                self.assertIn(marker, str(cm.exception))
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _verify_node(repo)
            (repo / refs.source_dir() / "codegen_bundle.json").write_bytes(b"\xff\xfe")
            with self.assertRaises(RuntimeError) as cm:
                _conductor(repo)._build_pure_verify_context(refs)
            self.assertIn("pure_bundle_document_missing", str(cm.exception))
            self.assertIn("UnicodeDecodeError", str(cm.exception))
        # ...and a whitespace-only one (a truncated write): the launch validator refuses it
        # exactly as it refuses "", one frame too late, so it raises here too.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _verify_node(repo)
            (repo / refs.ir_ref / "spec.ir.yaml").write_text(" \n\t\n", encoding="utf-8")
            with self.assertRaises(RuntimeError) as cm:
                _conductor(repo)._build_pure_verify_context(refs)
            self.assertIn("pure_ir_document_missing", str(cm.exception))
            self.assertTrue(str(cm.exception).endswith(": empty"), str(cm.exception))

    def test_non_utf8_checks_contract_raises_the_named_contract(self) -> None:
        # UnicodeDecodeError is a ValueError, not an OSError: catching OSError alone would let it
        # escape unnamed (the producer's runner read has the same pin).
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _verify_node(repo)
            (repo / "docs" / "workflow" / "CHECKS_MODULE_CONTRACT.md").write_bytes(
                b"## 1. x\n\xff\xfe\n## 5. y\n")
            with self.assertRaises(RuntimeError) as cm:
                _conductor(repo)._build_pure_verify_context(refs)
            self.assertIn("pure_checks_contract_document_missing", str(cm.exception))

    def test_the_language_binding_fails_closed_three_ways(self) -> None:
        """The binding appended to the neutral §1-§4 (issue #289) raises a NAMED refusal when the
        target language declares none, when it cannot be read, and when its section anchors are
        gone — never a shorter document with the header still promising the binding (round 2:
        replacing the unsliceable raise with an empty binding survived every test file)."""
        from unittest import mock
        from tools.backends import registry as backend_registry
        from tools.backends.language.fortran import checks_abi
        body = checks_abi.document()
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _verify_node(repo)
            c = _conductor(repo)
            with mock.patch.object(checks_abi, "document",
                                   lambda: body.replace("## 1. ", "## One ", 1)):
                with self.assertRaises(RuntimeError) as cm:
                    c._build_pure_verify_context(refs)
            self.assertIn("pure_checks_contract_document_unsliceable", str(cm.exception))
            self.assertIn("checks-ABI binding", str(cm.exception))
            with mock.patch.object(checks_abi, "document", side_effect=OSError("gone")):
                with self.assertRaises(RuntimeError) as cm:
                    c._build_pure_verify_context(refs)
            self.assertIn("pure_checks_abi_binding_missing", str(cm.exception))
            record = backend_registry.get("language", "fortran")
            with mock.patch.dict(backend_registry._BACKENDS, {("language", "fortran"): record._replace(
                    backend_provides=record.backend_provides - {"checks_abi"})}):
                with self.assertRaises(RuntimeError) as cm:
                    c._build_pure_verify_context(refs)
            self.assertIn("pure_checks_abi_binding_unavailable", str(cm.exception))

    def test_unsliceable_checks_contract_raises_the_named_contract(self) -> None:
        # A readable document whose anchors moved is a DIFFERENT diagnosis from an absent one:
        # the operator's repair is the document's section numbering, not a missing file.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _verify_node(repo)
            (repo / "docs" / "workflow" / "CHECKS_MODULE_CONTRACT.md").write_text(
                "## 1. The fixed ABI\nbody\n", encoding="utf-8")
            with self.assertRaises(RuntimeError) as cm:
                _conductor(repo)._build_pure_verify_context(refs)
            self.assertIn("pure_checks_contract_document_unsliceable", str(cm.exception))

    def test_severity_rubric_document_is_the_rubric_subsection_of_the_real_phase_02(self) -> None:
        # issue #143: the reviewer receives §2-2's severity rubric and nothing else of phase_02.
        # What is pinned is the SPAN — the subsection heading opens it, its own bullets are
        # inside, and the checklist above it and the retry policy below it are out.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _verify_node(repo)
            doc = _conductor(repo)._build_pure_verify_context(refs)["severity_rubric_document"]
            self.assertTrue(doc.startswith("#### Severity of a finding"), doc[:80])
            self.assertIn("names the repair", doc,
                          "the rubric's lead sentence no longer says what the value names; this "
                          "is the witness that the slot carries the RUBRIC and not some other "
                          "subsection, so a reworded lead sentence means re-reading it here")
            # The rubric restates #142's scope rule as a severity clause, which makes it a THIRD
            # statement of that rule inside phase_02. Pin its polarity POSITIVELY: the sibling
            # `test_phase_02_states_the_scope_rule_with_its_bound` refuses one spelling of the
            # fail-open ("the contract does not state is out of scope"), and a negative phrase
            # pin stops catching the moment the sentence is reworded.
            self.assertIn("is dropped and takes no severity when the checks-module contract "
                          "contradicts it", doc)
            self.assertIn("Where the contract is silent, the finding stands and takes the value "
                          "its subject earns", doc)
            # …and its CLASS, which is what round 1 found wrong: this bullet was strictly wider
            # than the two statements of the same rule it joins ("or the harness", "a value the
            # bundle returns"), so a G5 dataflow finding justified by what the harness does with
            # a MODEL-produced value fell inside the droppable class here and outside it there.
            # The class is taken FROM the template — the pre-existing authority — never spelled
            # again, so the two cannot drift apart. The extraction is self-tested.
            tpl = (Path(wc.__file__).resolve().parents[1] / "tools" / "prompt_templates"
                   / "workshop_generate_verify.txt").read_text(encoding="utf-8")
            _, sep, rest = tpl.partition(
                "can only OVERRULE one kind of claim — a claim about ")
            self.assertTrue(sep, "the template's droppable-class sentence no longer opens with "
                                 "'can only OVERRULE one kind of claim — a claim about '; "
                                 "re-read the TEMPLATE and re-derive this marker")
            claim_class, sep2, _ = rest.partition(" — so drop a finding")
            self.assertTrue(sep2, "the template's droppable-class sentence no longer ends with "
                                  "' — so drop a finding'; the class could not be delimited, so "
                                  "the comparison below would be against the rest of the "
                                  "TEMPLATE, not against a class")
            self.assertTrue(40 < len(claim_class) < 300,
                            f"the extracted class is {len(claim_class)} characters, which is not "
                            f"a claim class; the template's markers moved: {claim_class[:120]!r}")
            # EXCLUSIVITY, not presence. `assertIn` passed on a bullet that stated the template's
            # class AND MORE — appending "or about what the harness does with any value the
            # bundle returns" was green, which is the very widening this coupling exists to
            # refuse. Compare the rubric bullet's own class span for EQUALITY.
            opener, closer = "- A claim about ", " is dropped and takes no severity"
            bullet = [ln for ln in doc.splitlines() if ln.startswith(opener)]
            self.assertEqual(len(bullet), 1,
                             f"the rubric must carry exactly one {opener!r} bullet; found "
                             f"{len(bullet)}")
            rubric_class, sep3, _ = bullet[0][len(opener):].partition(closer)
            self.assertTrue(sep3, f"the rubric's drop bullet no longer contains {closer!r}, so "
                                  f"its class span could not be delimited")
            self.assertEqual(rubric_class, claim_class,
                             "the rubric's droppable class is not the template's, character for "
                             "character; a WIDER class here drops findings the template keeps")
            # The two tie-break sentences decide the ambiguous case, and one direction of each
            # ends the run. A polarity, not prose: pin both, or an inversion ships on a version
            # bump alone.
            self.assertIn("do not settle whether the subject is the sources under review or an "
                          "input, the value is `minor`", doc)
            self.assertIn("do not settle whether the sources under review can serve as the base "
                          "of a repair, the value is `minor`", doc)
            # …and the `minor` bullet's own routing sentence, which round 5 found held by
            # nothing but the drift digest: flipping "Every G1-G7 finding whose subject is the
            # generated code takes this value" to `major` reproduces #142's episode — a
            # contract-conforming bundle terminalizing the run — on a version bump alone.
            self.assertIn("Every G1-G7 finding whose subject is the generated code takes this "
                          "value", doc)
            # `ir_inconsistency` is what routes the operator to `reopen-phase --from-phase
            # compile` (`docs/RUNBOOK.md` §3-1 keys its first arm on that literal), and deleting
            # the obligation to write it turned only the drift digest red (round 5).
            self.assertIn("contains the literal `ir_inconsistency` when the input is "
                          "`spec.ir.yaml`", doc)
            # The rubric grades a FAIL. It must never authorize a pass: a bullet saying an
            # unattributable finding means `pass` is a template-authorized zero-work verdict,
            # and round 5 appended exactly that with only the digest red. Measured: the slice
            # contains neither literal today, so this is emptiness, not an allowlist.
            for authorizing in ("verification_status", "'pass'", '"pass"', " is `pass`"):
                self.assertNotIn(authorizing, doc,
                                 "the rubric names the pass side; it grades a fail, and the "
                                 "pass side is the launch template's output contract")
            # …and the AXIS the tie-breaks tie-break FOR. Round 4 inverted the lead paragraph to
            # "the value grades the consequence … One question selects the value: how severe the
            # consequence of the defect is", kept "names the repair" so the witness above still
            # passed, and turned only the drift digest red — whose remedy line says to bump the
            # version, which is what an intentional doc edit does. The axis is the whole rule.
            self.assertIn("the value does not grade the consequence the defect would have at "
                          "`Validate`, and the size of the correction does not change it", doc)
            self.assertIn("One question selects the value: whether `Generate.generate`, re-run "
                          "with the finding and the same `spec.ir.yaml` and `tests.md`, can "
                          "resolve the finding", doc)
            # Every literal here must occur in the REAL document, or the assertion is true of any
            # slice and pins nothing (the checks-contract test above records why). Note what is
            # NOT usable as an absent literal: `ir_inconsistency` occurs inside the rubric itself.
            real = _REAL_PHASE_02.read_text(encoding="utf-8")
            for absent in ("#### G7. dependency consistency",
                           "See the `verify` `SKILL`.",
                           "## On-failure behavior",
                           "A `Generate fail` retry defaults to an in-phase retry."):
                self.assertIn(absent, real, f"{absent!r} no longer occurs in the document, so "
                                            f"asserting its absence from the slice pins nothing")
                self.assertNotIn(absent, doc)

    def test_severity_rubric_states_every_verdict_severity_at_its_own_bullet(self) -> None:
        """Enumeration coupling (`atmofab-enforcement-change` rule 3-a): the rubric must state
        each severity the CODE accepts, at its own bullet, and state no other.

        PINNED: that every member of `VERDICT_SEVERITIES` except the pass-side `none` opens
        exactly one `- `<value>`:` bullet of the slice, and that the bullet count equals that
        membership — so a value added to the enum, or a bullet deleted from the document, is red
        and NAMES the value. The count comes from the code; the document is checked against it.
        SAMPLED: nothing about what each bullet SAYS — the wording is prose a leaf reads, and
        `test_severity_rubric_document_is_the_rubric_subsection_of_the_real_phase_02` pins only
        the span. `none` has no named constant of its own (the pass side is the bare literal in
        `pure_leaf.validate_verdict`), so its exclusion is self-tested against the tuple rather
        than written out."""
        self.assertIn("none", VERDICT_SEVERITIES,
                      "the pass-side literal left the enum; the exclusion below now removes "
                      "nothing and the count would be off by one")
        expected = set(VERDICT_SEVERITIES) - {"none"}
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _verify_node(repo)
            doc = _conductor(repo)._build_pure_verify_context(refs)["severity_rubric_document"]
        # A VALUE bullet is one whose opener is `<word>`: — the shape the three value bullets
        # use. Summing openers over the ENUM (round 1's fix) restored nothing: it could never see
        # a bullet for a value outside the enum, so `- `fatal`: …` shipped green where the older
        # count caught it. Collect the openers from the DOCUMENT and compare the set.
        found = re.findall(r"^- `([a-z_]+)`:", doc, flags=re.MULTILINE)
        self.assertEqual(sorted(found), sorted(expected),
                         "the rubric's value bullets are not exactly the enum's failing values; "
                         "a bullet naming a value the verdict schema refuses would send the "
                         "reviewer into a schema-repair loop, and a missing one leaves a value "
                         "unexplained. Note the shape this reads: a NON-value bullet must not "
                         "open `- `identifier`:` — write it without the colon.")

    def test_missing_phase_02_raises_the_named_contract(self) -> None:
        # Same disposition as the checks contract, and for the same corrected reason: a
        # whitespace-only value is counted missing and raises, so degrading only defers the
        # refusal one frame into `record_launch` (issue #169).
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _verify_node(repo, stage_phase_02=False)
            with self.assertRaises(RuntimeError) as cm:
                _conductor(repo)._build_pure_verify_context(refs)
            self.assertIn("pure_severity_rubric_document_missing", str(cm.exception))

    def test_non_utf8_phase_02_raises_the_named_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _verify_node(repo)
            (repo / "docs" / "workflow" / "phases" / "phase_02_generate.md").write_bytes(
                b"#### Severity of a finding\n\xff\xfe\n## On-failure behavior\n")
            with self.assertRaises(RuntimeError) as cm:
                _conductor(repo)._build_pure_verify_context(refs)
            self.assertIn("pure_severity_rubric_document_missing", str(cm.exception))

    def test_unsliceable_phase_02_raises_the_named_contract(self) -> None:
        # A readable document whose anchors moved is a DIFFERENT diagnosis from an absent one:
        # the operator's repair is the document's headings, not a missing file. Driven by the
        # REAL document with its terminator heading removed, so the fixture is the failure an
        # operator would actually produce.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _verify_node(repo)
            real = _REAL_PHASE_02.read_text(encoding="utf-8")
            self.assertIn("\n## On-failure behavior\n", real)
            (repo / "docs" / "workflow" / "phases" / "phase_02_generate.md").write_text(
                real.replace("\n## On-failure behavior\n", "\n## Retry policy\n"),
                encoding="utf-8")
            with self.assertRaises(RuntimeError) as cm:
                _conductor(repo)._build_pure_verify_context(refs)
            self.assertIn("pure_severity_rubric_document_unsliceable", str(cm.exception))


# ======================================================================================
# _checks_contract_abi_sections (the slicer's own contract, on synthetic text)
# ======================================================================================
class ChecksContractSlicerTests(unittest.TestCase):
    _DOC = ("# Title\n\npreamble\n\n"
            "## 1. The fixed ABI\nabi body\n\n"
            "## 2. Semantics\nsemantics body\n\n"
            "## 3. State\nstate body\n\n"
            "## 4. Prohibitions\nprohibitions body\n\n"
            "## 5. Legality\nlegality body\n")

    def test_keeps_1_to_4_and_drops_title_preamble_and_5(self) -> None:
        self.assertEqual(
            wc._checks_contract_abi_sections(self._DOC),
            "## 1. The fixed ABI\nabi body\n\n"
            "## 2. Semantics\nsemantics body\n\n"
            "## 3. State\nstate body\n\n"
            "## 4. Prohibitions\nprohibitions body")

    def test_missing_begin_anchor_raises(self) -> None:
        with self.assertRaises(ValueError):
            wc._checks_contract_abi_sections("# Title\n\n## 5. Legality\nbody\n")

    def test_missing_end_anchor_raises(self) -> None:
        # Degrading to "everything after §1" would silently hand the reviewer the deterministic
        # gate section the template tells it not to re-check.
        with self.assertRaises(ValueError):
            wc._checks_contract_abi_sections("## 1. ABI\nbody\n\n## 4. Prohibitions\nb\n")

    def test_decimal_subsection_headings_do_not_anchor(self) -> None:
        # `## 1.5` / `## 5.1` are subsection numbers, not the section anchors.
        with self.assertRaises(ValueError):
            wc._checks_contract_abi_sections("## 1.5 sub\nbody\n\n## 5. Legality\nb\n")
        with self.assertRaises(ValueError):
            wc._checks_contract_abi_sections("## 1. ABI\nbody\n\n## 5.1 sub\nb\n")

    def test_an_end_anchor_before_the_begin_anchor_is_not_counted(self) -> None:
        # The end search starts AFTER the begin index; a §5 back-reference above §1 must not
        # produce an empty (or reversed) slice.
        doc = ("## 5. Legality\nearly\n\n## 1. ABI\nabi body\n\n"
               "## 5. Legality\nlegality body\n")
        self.assertEqual(wc._checks_contract_abi_sections(doc), "## 1. ABI\nabi body")


class NumberedSectionRangeTests(unittest.TestCase):
    """The engine both numbered-section slicers delegate to (issue #169).

    `ChecksContractSlicerTests` above drives it through the `checks` caller; these rows drive
    the properties its docstring states that no caller's document can currently exercise —
    measured: a mutant searching for the end anchor from index 0 rather than from `start + 1`
    survived the whole suite, because both real documents happen to be in heading order.
    """

    def test_a_back_reference_above_the_section_is_not_the_terminator(self) -> None:
        doc = "## 5. Legality\nearly\n\n## 1. ABI\nabi body\n\n## 5. Legality\nlate\n"
        self.assertEqual(wc._numbered_section_range(doc, "1", "5", subject="s"),
                         "## 1. ABI\nabi body")

    def test_a_missing_anchor_raises_and_names_the_subject(self) -> None:
        for begin, end, doc in (("1", "5", "## 5. only\nb\n"),
                                ("1", "5", "## 1. only\nb\n")):
            with self.subTest(doc=doc):
                with self.assertRaises(ValueError) as caught:
                    wc._numbered_section_range(doc, begin, end, subject="the runner contract")
                self.assertIn("the runner contract", str(caught.exception))

    def test_a_decimal_subsection_does_not_anchor(self) -> None:
        with self.assertRaises(ValueError):
            wc._numbered_section_range("## 1.5 sub\nb\n\n## 5. end\nb\n", "1", "5",
                                       subject="s")


# ======================================================================================
# _generate_verify_severity_rubric_section (the slicer's own contract, on synthetic text)
# ======================================================================================
class SeverityRubricSlicerTests(unittest.TestCase):
    _DOC = ("# Title\n\npreamble\n\n"
            "#### G7. dependency consistency\ng7 body\n\n"
            "#### Severity of a finding (`issue_severity`)\nlead sentence\n"
            "- `minor`: m\n\n"
            "## On-failure behavior\nretry body\n")

    def test_keeps_the_subsection_and_drops_the_checklist_and_on_failure(self) -> None:
        self.assertEqual(
            wc._generate_verify_severity_rubric_section(self._DOC),
            "#### Severity of a finding (`issue_severity`)\nlead sentence\n- `minor`: m")

    def test_missing_begin_anchor_raises(self) -> None:
        with self.assertRaises(ValueError):
            wc._generate_verify_severity_rubric_section(
                "#### G7. x\nbody\n\n## On-failure behavior\nb\n")

    def test_missing_end_anchor_raises(self) -> None:
        # Degrading to "everything after the heading" would hand the reviewer the phase's own
        # retry policy as if it were part of the rubric.
        with self.assertRaises(ValueError):
            wc._generate_verify_severity_rubric_section(
                "#### Severity of a finding\n- `minor`: m\n")

    def test_a_subsection_appended_after_the_rubric_raises(self) -> None:
        # Round 2's probe: a new `#### …` subsection between the rubric and `## On-failure
        # behavior` was ABSORBED into the slice and shipped to the reviewer inside a fence
        # labelled "choose `issue_severity` by it" — green everywhere but the drift digest, whose
        # remedy line then tells the maintainer to bump the version. The slice now ends at the
        # first heading and refuses one that is not the expected terminator, by name.
        doc = self._DOC.replace(
            "\n## On-failure behavior",
            "\n#### Recording a finding\n- Each `findings[]` entry names its checklist item.\n"
            "\n## On-failure behavior")
        with self.assertRaises(ValueError) as cm:
            wc._generate_verify_severity_rubric_section(doc)
        self.assertIn("Recording a finding", str(cm.exception))
        # The message must name a repair the editor can actually perform. Round 5's probe added
        # a legitimate `### 2-3. Generate.gate substep`, and the earlier wording ("the rubric
        # must stay the last subsection of §2-2") prescribed something that edit cannot satisfy
        # while the rubric stays in §2-2 — it named neither real repair.
        self.assertIn("Move it above §2-2, or the rubric to the end of §2-2",
                      str(cm.exception))
        # …and it must survive the run log, which keeps `str(exc)[:200]`. A remedy truncated
        # before it is stated is the half-remedy this message exists to avoid.
        self.assertLessEqual(len(str(cm.exception)), 200,
                             "the raise is longer than the 200 characters the run-log detail "
                             "keeps, so the operator sees it cut mid-sentence")

    def test_a_terminator_rename_that_only_APPENDS_words_still_raises(self) -> None:
        # Three documents promise an operator that renaming `## On-failure behavior` fails the
        # substep closed. Under the word-boundary end anchor `## On-failure behavior and retry`
        # still anchored, so the slice silently accepted a renamed section and only the drift
        # digest — remedy: "bump the version" — would have spoken (round 5). The terminator is an
        # exact line now, and this is the case that distinguishes the two.
        doc = self._DOC.replace("## On-failure behavior", "## On-failure behavior and retry")
        with self.assertRaises(ValueError) as cm:
            wc._generate_verify_severity_rubric_section(doc)
        self.assertIn("and retry", str(cm.exception))

    def test_a_renamed_following_section_still_raises_by_name(self) -> None:
        # The other half: ending at the first heading must not turn a RENAMED terminator into a
        # silent success. The named error is what tells an operator which document to repair.
        doc = self._DOC.replace("## On-failure behavior", "## Retry policy")
        with self.assertRaises(ValueError) as cm:
            wc._generate_verify_severity_rubric_section(doc)
        self.assertIn("Retry policy", str(cm.exception))

    def test_a_longer_heading_that_merely_starts_with_the_phrase_does_not_anchor(self) -> None:
        # `(?:\s|$)` is the word boundary: `#### Severity of a findings table` is a different
        # subsection, and anchoring on it would slice a document the rubric is not in.
        with self.assertRaises(ValueError):
            wc._generate_verify_severity_rubric_section(
                "#### Severity of a findingsTable\nbody\n\n## On-failure behavior\nb\n")

    def test_an_end_anchor_before_the_begin_anchor_is_not_counted(self) -> None:
        # The end search starts AFTER the begin index; a back-reference above the heading must
        # not produce an empty (or reversed) slice.
        doc = ("## On-failure behavior\nearly\n\n#### Severity of a finding\n- `minor`: m\n\n"
               "## On-failure behavior\nretry body\n")
        self.assertEqual(wc._generate_verify_severity_rubric_section(doc),
                         "#### Severity of a finding\n- `minor`: m")


# ======================================================================================
# The generate.verify reviewer loop: happy path, fail verdict, repair, exhaustion, transport
# ======================================================================================
class PureVerifySubstepTests(unittest.TestCase):
    def _run(self, envelopes, *, cls=None, stage_checks_contract=True,
             stage_phase_02=True, llm_config=None, unlink=None):
        self._tmp = tempfile.TemporaryDirectory()
        repo = Path(self._tmp.name)
        refs = _verify_node(repo, stage_checks_contract=stage_checks_contract,
                            stage_phase_02=stage_phase_02)
        if unlink is not None:
            # A repo-relative path, computed from the node's refs, removed after staging.
            (repo / unlink(refs)).unlink()
        c = _reviewer_conductor(repo, envelopes, cls=cls, llm_config=llm_config)
        # Record the run-log rows without suppressing them: the real `emit` still runs, so what a
        # test reads back is what the operator's event stream carries.
        c.events = []  # type: ignore[attr-defined]
        _real_emit = c.emit
        def _emit(event, **fields):  # type: ignore[no-untyped-def]
            c.events.append({"event": event, **fields})  # type: ignore[attr-defined]
            _real_emit(event, **fields)
        c.emit = _emit  # type: ignore[assignment,method-assign]
        oc = _run_verify(c, refs)
        return c, refs, oc

    def tearDown(self) -> None:
        if hasattr(self, "_tmp"):
            self._tmp.cleanup()

    def test_context_assembly_failure_is_a_recorded_fail_closed_outcome(self) -> None:
        """The HANDLER pin for issue #142's fail-closed disposition, not the builder's.

        `_build_pure_verify_context` raising is only half the contract: `run_substep`'s callers
        must never see an exception, so this loop has to convert it into the same recorded
        `pure_context_assembly_failed` transport outcome the producer produces. Deleting the
        try/except around the call would leave every builder-side test above green.

        A `pass` envelope is queued deliberately: if the recovery were missing in the other
        direction — the failure swallowed and the launch continued — the reviewer would return
        `pass` and the assertions on spawn count and artifacts would catch it.
        """
        c, refs, oc = self._run([_envelope(_verdict("pass"))], stage_checks_contract=False)
        self.assertEqual(oc.status, "fail")
        self.assertEqual(oc.leaf_returncode, 1)      # transport => run_phase's fail_closed route
        self.assertIsNotNone(oc.infra_error)
        self.assertEqual(oc.infra_error[0], "pure_context_assembly_failed")
        self.assertIn("pure_checks_contract_document_missing", oc.infra_error[1])
        self.assertEqual(oc.output_refs, [])
        self.assertEqual(_spawned(c), 0)  # no leaf was billed
        base = c.repo_root / refs.source_dir()
        self.assertFalse((base / "source_meta.json").exists())
        self.assertFalse((base / "verdict_meta.json").exists())
        # The run-log row is the operator's only trace of WHICH document was missing: the
        # fail_closed reason downstream carries the tag but not the path. Deleting the emit left
        # this file green before this assertion existed.
        events = [e for e in c.events if e["event"] == "pure_context_assembly_failed"]
        self.assertEqual(len(events), 1, c.events)
        self.assertEqual(events[0]["node_key"], refs.node_key)
        self.assertIn("pure_checks_contract_document_missing", events[0]["detail"])

    def test_a_missing_severity_rubric_is_the_same_recorded_fail_closed_outcome(self) -> None:
        """Twin of the test above for the SECOND sliced document (issue #143).

        The builder reads the checks contract first, so the assertion above would still pass with
        the rubric read deleted — the contract's own name reaches the outcome either way. This
        drives the second read on its own: contract staged, phase_02 absent.
        """
        c, refs, oc = self._run([_envelope(_verdict("pass"))], stage_phase_02=False)
        self.assertEqual(oc.status, "fail")
        self.assertEqual(oc.infra_error[0], "pure_context_assembly_failed")
        self.assertIn("pure_severity_rubric_document_missing", oc.infra_error[1])
        self.assertEqual(_spawned(c), 0)  # no leaf was billed
        base = c.repo_root / refs.source_dir()
        self.assertFalse((base / "source_meta.json").exists())
        events = [e for e in c.events if e["event"] == "pure_context_assembly_failed"]
        self.assertEqual(len(events), 1, c.events)
        self.assertIn("pure_severity_rubric_document_missing", events[0]["detail"])

    def test_a_missing_node_artifact_is_the_same_recorded_fail_closed_outcome(self) -> None:
        """Issue #467: the four node artifacts degraded to `""`, and the blank was refused one
        frame later inside `record_launch`, escaping `run_substep` and aborting the conductor
        with no outcome row. Each is now the same recorded outcome as a missing contract."""
        rows = (
            (lambda r: f"{r.source_dir()}/codegen_bundle.json", "pure_bundle_document_missing"),
            (lambda r: f"{r.spec_path}/controlled_spec.md",
             "pure_controlled_spec_document_missing"),
            (lambda r: f"{r.ir_ref}/spec.ir.yaml", "pure_ir_document_missing"),
            (lambda r: f"{r.spec_path}/tests.md", "pure_tests_document_missing"),
        )
        for rel_of, marker in rows:
            with self.subTest(marker=marker):
                c, refs, oc = self._run([_envelope(_verdict("pass"))], unlink=rel_of)
                try:
                    self.assertEqual(oc.status, "fail")
                    self.assertEqual(oc.leaf_returncode, 1)
                    self.assertEqual(oc.infra_error[0], "pure_context_assembly_failed")
                    self.assertIn(marker, oc.infra_error[1])
                    self.assertEqual(oc.output_refs, [])
                    self.assertEqual(_spawned(c), 0)  # no leaf was billed
                    base = c.repo_root / refs.source_dir()
                    self.assertFalse((base / "source_meta.json").exists())
                    self.assertFalse((base / "verdict_meta.json").exists())
                    events = [e for e in c.events
                              if e["event"] == "pure_context_assembly_failed"]
                    self.assertEqual(len(events), 1, c.events)
                    self.assertIn(marker, events[0]["detail"])
                finally:
                    self._tmp.cleanup()

    def test_the_reviewer_loop_takes_its_launch_instant_from_the_filesystem(self) -> None:
        """Mirror of the producer's #113 pin — one witness per OCCURRENCE, not per rule.

        The three sites that take a launch instant read identically, and round 0's hunk
        mutation reported this one killed in one run and surviving in another (the kill came
        from a neighbouring failure under `-x`, not from an assertion about this line). An
        identity against the probe `Conductor._launch_instant` writes for this attempt's arid
        settles it: a `time.time()` reading is a different number, the two clocks differing by
        up to one timer tick.

        What this pins is PROVENANCE, not a decision. `SubstepOutcome.launched_at` was deleted
        by issue #176 with the mini-loop that was its only production consumer, so what the
        resolver leaves on this path is the PROBE ITSELF — an operator's record of when this
        attempt started, under the arid of the row it belongs to. This is that probe's only
        witness on the reviewer loop: without it, a mutation dropping the `_launch_instant`
        call from `_spawn_workshop_turn` is silent.
        """
        c, _refs, oc = self._run([_envelope(_verdict("pass"))])
        self.assertEqual(oc.status, "pass")
        probe = (c.repo_root / "workspace" / "orchestrations" / c.orchestration_id
                 / "agents" / oc.agent_run_id / wc.LAUNCH_INSTANT_PROBE_BASENAME)
        self.assertTrue(probe.exists(), probe)
        self.assertEqual(json.loads(probe.read_text())["agent_run_id"], oc.agent_run_id)

    def test_pure_pass_finalize_payload_satisfies_the_real_summary_validator(self) -> None:
        """Mirror of the producer's regression pin (billed E2E, 2026-07-16).

        A pure row's `output_refs` is empty by contract, so `_validate_agent_summary_text`
        requires `result_summary` to explain the row. The reviewer, like the producer,
        passed None on pass — which `finalize-child` rejects. These tests stub
        `runtime()`, so the only way to catch it is to run the conductor's ACTUAL
        captured payload through the REAL validators.
        """
        from tools.orchestration_runtime import (
            _extract_agent_summary_text, _validate_agent_summary_text,
        )

        c, refs, oc = self._run([_envelope(_verdict("pass"))])
        self.assertEqual(oc.status, "pass")
        payloads = [cap["--agent-run-json"] for sub, cap in c.calls
                    if sub == "finalize-child" and "--agent-run-json" in cap]
        self.assertTrue(payloads, "finalize-child must have been called with a payload")
        for payload in payloads:
            self.assertEqual(payload["output_refs"], [])  # the pure contract
            _validate_agent_summary_text(payload, _extract_agent_summary_text(payload))

    def test_pass_verdict_writes_source_meta_and_empty_output_refs(self) -> None:
        c, refs, oc = self._run([_envelope(_verdict("pass"))])
        self.assertEqual(oc.status, "pass")
        self.assertEqual(oc.output_refs, [])
        base = c.repo_root / refs.source_dir()
        meta = json.loads((base / "source_meta.json").read_text())
        self.assertEqual(meta["verification_status"], "pass")
        self.assertEqual(meta["issue_severity"], "none")
        self.assertIsNone(meta["last_fail_reason"])
        # meta_contracts-required keys are all present with correct types.
        for k in ("attempt_count", "verification_status", "last_fail_reason",
                  "debug_mode", "context_isolated"):
            self.assertIn(k, meta)
        vmeta = json.loads((base / "verdict_meta.json").read_text())
        self.assertEqual(vmeta["result"], "pass")
        self.assertIsNone(vmeta["failure_category"])
        self.assertEqual(vmeta["prompt_contract_version"], PURE_PROMPT_CONTRACT_VERSION)
        self.assertEqual(vmeta["per_attempt"][0]["model"], "claude-opus-4-8")
        self.assertEqual(vmeta["per_attempt"][0]["model_provenance"], "result_envelope")

    def test_a_codex_reviewer_attempt_records_the_host_pinned_model(self) -> None:
        """Issue #348, the reviewer loop's twin of the producer row: a codex reviewer's
        `per_attempt[].model` records the host-pinned slug (not the streamed value, absent or
        forged) and equals the agent-run row of the same launch."""
        from tools.tests.test_workshop_producer import _codex_proc
        for label, streamed in (("no streamed model", None), ("forged stream", "forged-model")):
            def _writes_pass(c, tree, kwargs, streamed=streamed):
                (tree / "verdict.json").write_text(json.dumps(_verdict("pass")),
                                                   encoding="utf-8")
                proc = _codex_proc()
                proc.model = streamed
                return proc
            with self.subTest(stream=label), tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp)
                refs = _verify_node(repo)
                c = _reviewer_conductor(repo, [], llm_config=_cfg(
                    "codex", agent_model="gpt-5.6-sol"))
                # The homes OUTSIDE /tmp, as production's are: a codex reading lineage whose
                # home a codex shell can write is refused (`_codex_shell_writable_root`).
                homes = tempfile.TemporaryDirectory(dir="/var/tmp")
                self.addCleanup(homes.cleanup)
                c.homes = Path(homes.name).resolve() / "homes"
                # The thread the launch emitted (what the fake recorded), as the session index
                # would answer it.
                c._session_id_for_child = (  # type: ignore[assignment]
                    lambda arid, entry=None, c=c: c.__dict__.get("threads", {}).get(
                        arid, f"thread-{arid}"))
                c.leaf_actions = [_writes_pass]
                oc = _run_verify(c, refs)
                self.assertEqual(oc.status, "pass", label)
                vmeta = json.loads(
                    (c.repo_root / refs.source_dir() / "verdict_meta.json").read_text())
                row = [cap["--agent-run-json"] for sub, cap in c.calls
                       if sub == "finalize-child" and "--agent-run-json" in cap][-1]
                self.assertEqual(vmeta["per_attempt"][0]["model"], "gpt-5.6-sol", label)
                self.assertEqual(vmeta["per_attempt"][0]["model_provenance"],
                                 "codex_launch_pinned", label)
                self.assertEqual(vmeta["per_attempt"][0]["model"], row["agent_model"], label)

    def test_fail_verdict_is_substep_fail_with_projected_source_meta(self) -> None:
        c, refs, oc = self._run([_envelope(_verdict("fail", severity="minor",
                                                    reason="flux sign is wrong"))])
        self.assertEqual(oc.status, "fail")
        self.assertEqual(oc.leaf_returncode, 0)      # a content fail, NOT transport
        self.assertIsNone(oc.infra_error)
        base = c.repo_root / refs.source_dir()
        meta = json.loads((base / "source_meta.json").read_text())
        self.assertEqual(meta["verification_status"], "fail")
        self.assertEqual(meta["issue_severity"], "minor")
        # The reason, then every finding's summary (`verify_repair_text`, issue #347).
        self.assertEqual(meta["last_fail_reason"], "flux sign is wrong\n1. mass is not conserved")
        # A valid fail verdict is NOT a routed category — verdict_meta stays result=pass.
        vmeta = json.loads((base / "verdict_meta.json").read_text())
        self.assertEqual(vmeta["result"], "pass")
        self.assertIsNone(vmeta["failure_category"])

    _LONG_SUMMARIES = ("`quoted` escape is missing " + "q" * 2500,
                       "the block count does not use ceil " + "c" * 2500,
                       "rank 0 writes the halo twice " + "h" * 2500)

    def _producer_repair_request_after_a_minor_verdict(self, *, resumable: bool) -> dict:
        """Drive a `minor` verdict with `_LONG_SUMMARIES` through the real routing and read the
        launch request the producer's repair turn RECORDS — via `run_substep`, so the producer
        loop's own seed of `repair_findings` is on the path, not only `build_launch_request`.
        The producer is a workshop leaf (`test_pure_leaf_producer._WorkshopLeafFake`), whose
        lineage `child-1` the repair targets: `resumable=True` reopens it warm, `resumable=False`
        is the #209 cold first turn (a provider without warm resume, a GC'd transcript), a new
        lineage carrying the findings."""
        from tools.tests.test_pure_leaf_producer import _workshop_conductor
        self._tmp = tempfile.TemporaryDirectory()
        repo = Path(self._tmp.name)
        refs = _verify_node(repo)
        producer = _workshop_conductor(repo)
        producer.envelopes = [_envelope(_valid_bundle())]
        self.assertEqual(producer.run_substep(refs, "generate", "generate").status, "pass")
        (target,) = [r["agent_run_id"] for r in _requests(producer)]
        reviewer = _reviewer_conductor(repo, [_envelope(_verdict(
            "fail", severity="minor", reason="escape handling is incomplete",
            findings=[{"summary": s} for s in self._LONG_SUMMARIES]))])
        reviewer.homes = repo / ".reviewer_homes"
        reviewer._n = 100  # its arids (and lineage ids) apart from the producer's
        oc = _run_verify(reviewer, refs)
        self.assertEqual(oc.status, "fail")
        passed = wc.SubstepOutcome("a", "pass", [])
        decision = reviewer.classify_failure(refs, "generate", [passed, passed, oc])
        self.assertEqual((decision.reason, decision.repair_strategy), ("verify_minor", "reuse"))
        findings = reviewer._read_repair_findings(refs, decision.reason, "generate")
        repair = reviewer._repair_payload(decision, target, findings=findings)
        # What `run_phase` does before a reopened producer attempt
        # (`_ensure_fresh_producer_id`): a new source directory holding only what the host
        # places in it.
        runner = producer._runner_basename(refs)
        text = (repo / refs.source_dir() / "src" / runner).read_text(encoding="utf-8")
        refs = wc.NodeRefs(target_id=refs.target_id, node_key=refs.node_key,
                           spec_path=refs.spec_path, ir_id=refs.ir_id,
                           pipeline_id=refs.pipeline_id, source_id=refs.source_id + "_reopen")
        (repo / refs.source_dir() / "src").mkdir(parents=True)
        (repo / refs.source_dir() / "src" / runner).write_text(text, encoding="utf-8")
        # The producer repair turn, on the producer's fake: one valid bundle answers it.
        producer.calls = []
        producer.resumable = resumable
        evidence = producer.__dict__.setdefault("evidence", {})
        with mock.patch.object(
                wc_ort, "_read_launch_request_payload",
                lambda repo_root, oid, *, agent_run_id: evidence.get(
                    f"{agent_run_id}.request.input.json")):
            produced = producer.run_substep(refs, "generate", "generate", repair=repair)
        self.assertEqual(produced.status, "pass")
        launches = _requests(producer)
        self.assertEqual(len(launches), 1)
        return launches[0]

    def _expected_fold(self) -> str:
        expected = ("escape handling is incomplete\n"
                    + "\n".join(f"{i}. {s}"
                                 for i, s in enumerate(self._LONG_SUMMARIES, start=1)))
        # Longer than every clip a reader of it could apply (the diagnostician's 6000 budget
        # is the largest in the tree).
        self.assertGreater(len(expected), 6000)
        return expected

    def test_every_finding_of_a_minor_verdict_reaches_the_producer_repair_request(self) -> None:
        """Issue #347's defect, end to end on the host: the reviewer's one-line reason named one
        defect and `findings[]` named more, and the warm repair's `repair_findings` carried only
        the reason (orch_20260929T204405Z_c269a003, launch 608eef82). Every summary must now be
        in the warm repair turn's recorded request."""
        req = self._producer_repair_request_after_a_minor_verdict(resumable=True)
        self.assertEqual(req["repair_strategy"], "reuse")
        self.assertTrue(req.get("warm_resume"))
        self.assertEqual(req["repair_findings"], self._expected_fold())

    def test_every_finding_reaches_a_cold_first_repair_turn_too(self) -> None:
        """The cold half of the same path: with no session to resume, the reopen's first turn
        is a cold repair (issue #209) and must carry the same whole fold."""
        req = self._producer_repair_request_after_a_minor_verdict(resumable=False)
        self.assertEqual(req["repair_strategy"], "reuse")
        self.assertNotIn("warm_resume", req)
        self.assertEqual(req["repair_findings"], self._expected_fold())

    def test_bounded_repair_recovers_on_second_turn(self) -> None:
        bad = {"verification_status": "pass"}  # schema violation (missing keys)
        c, refs, oc = self._run([_envelope(bad), _envelope(_verdict("pass"))])
        self.assertEqual(oc.status, "pass")
        self.assertEqual(oc.attempts, 2)
        self.assertTrue((c.repo_root / refs.source_dir() / "source_meta.json").exists())

    def test_exhausted_schema_repair_no_source_meta_proof_of_work(self) -> None:
        bad = {"verification_status": "pass"}  # persistently schema-invalid
        c, refs, oc = self._run([_envelope(bad)])  # same bad envelope every turn
        self.assertEqual(oc.status, "fail")
        base = c.repo_root / refs.source_dir()
        # Proof-of-work: no schema-valid verdict => source_meta.json is NOT written.
        self.assertFalse((base / "source_meta.json").exists())
        vmeta = json.loads((base / "verdict_meta.json").read_text())
        self.assertEqual(vmeta["result"], "fail")
        self.assertEqual(vmeta["failure_category"], "verdict_schema_violation")
        self.assertTrue(vmeta.get("failure_excerpt"))

    def test_exhaustion_spends_exactly_the_repair_budget(self) -> None:
        """The reviewer loop's copy of the budget boundary, `attempt < MAX_BUNDLE_REPAIR_TURNS`,
        which had no witness either (issue #508): widened to `<=`, every exhaustion bills one
        launch more than the budget names, and the row above stays green because the fake
        repeats its last envelope for as many launches as the loop asks. The two loops are
        separate copies, so the producer's row does not cover this one. Pinned on the launch
        count, the outcome's `attempts`, and the `verdict_meta` the outer route reads."""
        from tools.pure_leaf import MAX_BUNDLE_REPAIR_TURNS
        c, refs, oc = self._run([_envelope({"verification_status": "pass"})])
        self.assertEqual(oc.status, "fail")
        self.assertEqual(_spawned(c), MAX_BUNDLE_REPAIR_TURNS + 1)
        self.assertEqual(oc.attempts, MAX_BUNDLE_REPAIR_TURNS + 1)
        vmeta = json.loads((c.repo_root / refs.source_dir() / "verdict_meta.json").read_text())
        self.assertEqual(vmeta["attempts"], MAX_BUNDLE_REPAIR_TURNS + 1)
        self.assertEqual(len(vmeta["per_attempt"]), MAX_BUNDLE_REPAIR_TURNS + 1)

    def test_an_unparseable_verdict_document_is_the_schema_category(self) -> None:
        """A `verdict.json` that is not JSON at all is a schema violation of the reviewer's
        document, repaired and exhausted under `verdict_schema_violation` (the pure leaf's
        `pure_response_unparseable` was the category of a reply text that held no document,
        which the workshop path does not read), and no projection is written."""
        c, refs, oc = self._run([_envelope("not a verdict at all")])
        self.assertEqual((oc.status, oc.leaf_returncode), ("fail", 0))
        self.assertFalse((c.repo_root / refs.source_dir() / "source_meta.json").exists())
        vmeta = json.loads((c.repo_root / refs.source_dir() / "verdict_meta.json").read_text())
        self.assertEqual(vmeta["failure_category"], wc.GENERATE_VERDICT_SCHEMA_VIOLATION)

    def test_transport_error_routes_fail_closed(self) -> None:
        c, refs, oc = self._run([wc.ProcResult(1, "", "usage limit reached")])
        self.assertEqual(oc.status, "fail")
        self.assertNotEqual(oc.leaf_returncode, 0)   # forces run_phase's fail_closed branch
        # A transport failure has no fixable verdict — not repaired (one attempt only).
        self.assertEqual(oc.attempts, 1)
        self.assertFalse((c.repo_root / refs.source_dir() / "source_meta.json").exists())

    def test_a_timed_out_reviewer_is_ended_by_its_bound_and_never_waits_out_a_quota(self) -> None:
        """Where the field hang happened, the loop classifies the dead leaf itself. A leaf the
        conductor KILLED must be read from `ProcResult.timed_out` — if this site classified the
        captured text instead, the throttle notice a wedged leaf had printed before it stopped
        answering would read as `llm_usage_limit`, and with `--wait-usage-reset` on the
        conductor would sleep for hours and re-launch a leaf it had killed itself: the
        multi-hour silent block the cap exists to remove.

        On the reviewer's workshop lineage (issue #534 PR-J4-b) a killed launch is ENDED by its
        bound (`ended_by: lineage_timeout`, its own exit on `leaf_rc`), and with no schema-valid
        `fail` in its tree it is an unfinished review, run again from the seeds until the
        repair budget is spent — never a transport death, so never a quota wait. (The pure loop
        ended at once with `infra_error: leaf_timeout`; the workshop rule is pinned whole by
        `test_workshop_reviewer`'s `test_a_pass_never_finished_exhausts_into_the_schema_category`.)"""
        from tools.pure_leaf import MAX_BUNDLE_REPAIR_TURNS
        marker = wc._leaf_timeout_marker(7200, 7203.0)
        self._tmp = tempfile.TemporaryDirectory()
        repo = Path(self._tmp.name)
        refs = _verify_node(repo)
        # wait_usage_reset ON: the opt-in that would otherwise park the run for hours.
        c = _reviewer_conductor(repo, [wc.ProcResult(
            -9, "", "You've hit your session limit · resets 3pm (Asia/Tokyo)\n" + marker,
            timed_out=True)], wait_usage_reset=True)
        events: list = []
        c.emit = lambda event, **f: events.append(event)  # type: ignore[assignment]
        oc = _run_verify(c, refs)
        spawn_kwargs = c.spawns
        self.assertEqual((oc.status, oc.leaf_returncode, oc.infra_error), ("fail", 0, None))
        self.assertEqual(c.slept, [])                # ...never parked for a reset
        self.assertEqual([e for e in events if e.startswith("leaf_usage_limit")], [])
        vmeta = json.loads((c.repo_root / refs.source_dir() / "verdict_meta.json").read_text())
        self.assertEqual(vmeta["failure_category"], wc.GENERATE_VERDICT_SCHEMA_VIOLATION)
        self.assertEqual([(a["ended_by"], a["leaf_rc"]) for a in vmeta["per_attempt"]],
                         [("lineage_timeout", -9)] * (MAX_BUNDLE_REPAIR_TURNS + 1))
        # The event has to NAME the wedged leaf; only the caller knows node/step/substep.
        self.assertEqual(spawn_kwargs[0]["timeout_context"],
                         {"node_key": refs.node_key, "step": "generate", "substep": "verify",
                          "agent_run_id": spawn_kwargs[0]["child_arid"],
                          "leaf_mode": "workshop"})

    def _waiting(self, *procs: wc.ProcResult, flag: bool = True,
                 record: bool = False) -> tuple:
        """A reviewer-loop driver (`_run_verify`): `procs` are the scripted leaf results (the
        last one repeats; the fake refuses a launch far past the script, so a wait that never
        counts its budget is a red row rather than a hang), a passing verdict is appended when
        only one is given. Every launch request is on `_requests(c)` and every spawn's kwargs
        on `c.spawns` (`record` is kept for the callers that say they read them); events land
        on `c.events` either way (so a flag-off run can assert it emitted nothing)."""
        self._tmp = tempfile.TemporaryDirectory()
        repo = Path(self._tmp.name)
        refs = _verify_node(repo)
        c = _reviewer_conductor(
            repo, list(procs) + ([wc.ProcResult(0, _envelope(_verdict("pass")), "")]
                                 if len(procs) == 1 else []), wait_usage_reset=flag)
        c.events = []
        c.emit = lambda event, **f: c.events.append((event, f))  # type: ignore[assignment]
        return c, refs

    _QUOTA = wc.ProcResult(1, "", "Claude AI usage limit reached")
    _FLAKE = wc.ProcResult(1, "", "API Error: Connection closed mid-response.")

    def test_a_missing_lineage_home_on_a_warm_repair_falls_back_to_a_cold_repair_of_the_same_verdict(self) -> None:
        """The reviewer twin of the producer's missing-home row (issue #508).
        `_spawn_workshop_turn` returns None when a warm resume has no lineage home to run in
        (issue #245; `workshop_lineage_home_missing`); the loop keeps its repair target, so the
        NEXT turn is still a `reuse` repair of the reviewer's own schema-violating verdict —
        findings and full context — rather than a fresh review that forgets what it was
        repairing. The two loops are separate copies of that line, and the producer's row left
        the reviewer's unwitnessed: with it dropped, the fallback launch here renders with
        `repair_strategy: none`. (The pure loop's fallback also re-sent the prior verdict as
        `prior_document`; with the home gone a workshop lineage has no snapshot to seed it from,
        so the fallback is seeded from the seeds alone.)

        The missing home is faked where production detects it (`record_launch`'s
        `workshop_lineage_home_missing`, which the runtime's record-launch returns before it
        writes any launch state and so before any leaf is spawned; the fake runs the real
        record first and then adds the flag, so the dead warm turn's request stays
        observable), on the claude fake, where a session id is the child's arid.
        """
        c, refs = self._waiting(wc.ProcResult(0, _envelope({"verification_status": "pass"}), ""),
                                wc.ProcResult(0, _envelope(_verdict("pass")), ""), record=True)
        real_record = c.record_launch
        vanished: list[bool] = []

        def _vanish_once(child_arid, request, entry=None, **kw):
            rec = real_record(child_arid, request, entry, **kw)
            if request.get("warm_resume") and not vanished:
                vanished.append(True)
                return {**rec, "workshop_lineage_home_missing": True}
            return rec

        c.record_launch = _vanish_once  # type: ignore[assignment]
        oc = _run_verify(c, refs)
        self.assertEqual(oc.status, "pass")
        self.assertEqual(vanished, [True])
        # Three requests built: cold, the warm repair that died to the missing home, the cold
        # repair that replaced it. Two spawned: the dead warm turn never reached the leaf.
        requests = _requests(c)
        self.assertEqual([r["repair_strategy"] for r in requests], ["none", "reuse", "reuse"])
        self.assertTrue(requests[1].get("warm_resume"))
        self.assertFalse(requests[2].get("warm_resume"))
        self.assertEqual(requests[2]["repair_target_agent_run_id"], "child-1")
        self.assertTrue(requests[2].get("repair_findings"))
        self.assertTrue(requests[2].get("pure_context"))
        self.assertNotEqual(requests[2]["workshop_lineage_id"], "child-1")
        self.assertEqual([k.get("resume_session_id") for k in c.spawns], [None, None])
        self.assertEqual(oc.attempts, 2)
        self.assertTrue(any(ev == "resume_session_unavailable"
                            and f.get("reason") == "workshop_lineage_home_missing"
                            for ev, f in c.events), c.events)

    def test_a_repair_whose_session_is_not_resumable_is_built_and_spawned_cold(self) -> None:
        """The reviewer loop decides `warm` in its own lineage machine
        (`_WorkshopLineageMachine.next_launch`, asking `_workshop_session_resumable`), and the
        producer's row does not reach this copy (issue #508, round 1: dropping the resumability
        half left the suite green). A schema-violating verdict whose session is gone must be
        repaired COLD: the request carries the findings and the full context, the new lineage's
        tree carries the prior verdict (seeded from the old lineage's snapshot — what the pure
        loop's `prior_document` carried), and the spawn names no session to resume. Warm
        instead, the repair would be a `--resume` of a transcript that no longer exists."""
        c, refs = self._waiting(wc.ProcResult(0, _envelope({"verification_status": "pass"}), ""),
                                wc.ProcResult(0, _envelope(_verdict("pass")), ""), record=True)
        c.resumable = False
        carried: list = []
        write_first, write_second = c.leaf_actions

        def _reads_the_carried_verdict(conductor, tree, kwargs):
            carried.append(json.loads((tree / "verdict.json").read_text(encoding="utf-8")))
            return write_second(conductor, tree, kwargs)

        c.leaf_actions = [write_first, _reads_the_carried_verdict]
        oc = _run_verify(c, refs)
        self.assertEqual(oc.status, "pass")
        requests = _requests(c)
        self.assertEqual([r["repair_strategy"] for r in requests], ["none", "reuse"])
        self.assertFalse(requests[1].get("warm_resume"))
        self.assertEqual(requests[1]["repair_target_agent_run_id"], "child-1")
        self.assertTrue(requests[1].get("repair_findings"))
        self.assertEqual(carried, [{"verification_status": "pass"}])
        self.assertTrue(requests[1].get("pure_context"))
        self.assertEqual([k.get("resume_session_id") for k in c.spawns], [None, None])

    def test_every_reviewer_launch_names_the_target_language(self) -> None:
        """The generate reviewer's template carries `{{language:<name>}}` markers, and the
        request must name the language they are composed for (issue #289): a launch without
        `pure_language` is refused at render. Driven through the real reviewer loop, cold launch
        and repair turn both — the render in the unit tests builds its own requests."""
        c, refs = self._waiting(wc.ProcResult(0, _envelope("nope"), ""),
                                wc.ProcResult(0, _envelope(_verdict("pass")), ""), record=True)
        oc = _run_verify(c, refs)
        self.assertEqual(oc.status, "pass")
        self.assertGreaterEqual(len(_requests(c)), 2)
        import tools.orchestration_runtime as ort
        for request in _requests(c):
            self.assertEqual(request.get("pure_language"), c.target.toolchain["language"])
            # ...and the parallel backend its `{{parallel:<name>}}` markers are (issue #316).
            self.assertEqual(request.get("pure_parallel"), c.target.parallel_backend)
            ort._pure_launch_template(request)  # composes, i.e. does not raise

    def test_wait_usage_reset_recovers_a_transport_usage_limit(self) -> None:
        """--wait-usage-reset (opt-in) mirrors the producer: a reviewer transport death the
        classifier tags `llm_usage_limit` is waited out in place on the fixed schedule and
        re-launched, rather than fail-closing. The wait is not a repair turn (attempts stays the
        reviewer's repair count)."""
        c, refs = self._waiting(wc.ProcResult(1, "", "usage limit reached"))
        oc = _run_verify(c, refs)
        self.assertEqual(oc.status, "pass")
        self.assertEqual(_spawned(c), 2)
        self.assertEqual(oc.attempts, 2)             # launch count (the wait launch is counted)
        self.assertEqual(c.slept, [wc.USAGE_LIMIT_WAIT_SCHEDULE_SECONDS[0]])
        self.assertTrue((c.repo_root / refs.source_dir() / "source_meta.json").exists())

    def test_the_reviewer_wait_asks_the_provider_of_its_own_entry(self) -> None:
        """Issue #405, the reviewer half: the reset probe is asked with the reviewer loop's OWN
        `entry` (a per-substep override is the provider whose window is read). The substep's
        entry is made distinguishable by a sentinel model."""
        import dataclasses
        c, refs = self._waiting(wc.ProcResult(1, "", "usage limit reached"))
        orig_entry_for = c.entry_for

        def _entry_for(phase, substep):
            entry = orig_entry_for(phase, substep)
            if (phase, substep) == ("generate", "verify"):
                return dataclasses.replace(entry, model="per-substep-sentinel")
            return entry
        c.entry_for = _entry_for  # type: ignore[assignment]
        asked: list = []
        c._read_usage_reset = (  # type: ignore[assignment]
            lambda entry, arid: asked.append(entry) or usage_reset.ResetReading(
                None, None, usage_reset.FAILURE_PROBE_FAILED, "stub"))
        oc = _run_verify(c, refs)
        self.assertEqual(oc.status, "pass")
        self.assertEqual([e.model for e in asked], ["per-substep-sentinel"])

    def test_wait_usage_reset_recovers_a_real_cli_abort_in_either_shape(self) -> None:
        """REGRESSION, production shapes: the CLI reports a usage limit on STDOUT with a 0-byte
        stderr, in one of two recorded shapes — the bare message (5 of the 6 incidents on record)
        or the `--output-format json` envelope carrying it in `result` (the 1 that struck a pure
        leaf, verbatim below from `orch_20260719T021249Z_419ebdf9`). The wait is decided from the
        classifier's tag, so BOTH shapes wait the same first schedule entry on this loop; this
        pins that the tag reaches the wait from the reviewer loop as well as from the producer's.
        It does NOT pin the classifier's `"result":"` prefix — the recorded message names the
        `session` window, which the bare `session limit` fallback tags too (measured: green with
        the prefix alternative deleted); that prefix is pinned by
        `test_workflow_conductor.py::test_an_enveloped_abort_is_tagged_whatever_the_cli_key_order`."""
        bare = "You've hit your session limit · resets 5:50pm (Asia/Tokyo)\n"
        enveloped = (
            '{"type":"result","subtype":"success","is_error":true,"api_error_status":429,"duration_ms":64'
            '6,"duration_api_ms":0,"num_turns":1,"result":"You\'ve hit your session limit · resets 12:30pm'
            ' (Asia/Tokyo)","stop_reason":"stop_sequence","session_id":"160cb9c9-b595-4a87-aa72-fc1cf86c4'
            '878","total_cost_usd":0,"usage":{"input_tokens":0,"cache_creation_input_tokens":0,"cache_rea'
            'd_input_tokens":0,"output_tokens":0,"server_tool_use":{"web_search_requests":0,"web_fetch_re'
            'quests":0},"service_tier":"standard","cache_creation":{"ephemeral_1h_input_tokens":0,"epheme'
            'ral_5m_input_tokens":0},"inference_geo":"","iterations":[],"speed":"standard"},"modelUsage":'
            '{},"permission_denials":[],"terminal_reason":"api_error","fast_mode_state":"off","uuid":"ab0'
            '9298a-847a-415f-87d6-88205cc51fb4"}')
        for shape, stdout in (("bare", bare), ("enveloped", enveloped)):
            with self.subTest(shape=shape):
                c, refs = self._waiting(wc.ProcResult(1, stdout, ""))
                oc = _run_verify(c, refs)
                self.assertEqual(oc.status, "pass")
                self.assertEqual(_spawned(c), 2)                # waited, then relaunched
                self.assertEqual(c.slept, [wc.USAGE_LIMIT_WAIT_SCHEDULE_SECONDS[0]])

    def test_the_reviewer_wait_budget_is_the_schedule_then_fails_closed(self) -> None:
        """The reviewer loop's OWN `usage_waits` bookkeeping, driven through the real loop (the
        conductor-file rows drive a fake loop with its own copy of the guard and cannot see this
        call site): four usage-limit deaths sleep exactly the schedule, the fourth is DECLINED
        `budget_spent` with the death's own tag, and the substep fails with the honest launch
        count. Without this row `usage_waits += 1` could be dropped at this site with the suite
        green — a lasting quota would then park `generate.verify` 900 s forever."""
        c, refs = self._waiting(*([self._QUOTA] * (wc.MAX_USAGE_LIMIT_WAITS + 1)))
        oc = _run_verify(c, refs)
        self.assertEqual(oc.status, "fail")
        self.assertEqual(oc.infra_error[0], "llm_usage_limit")
        self.assertEqual(_spawned(c), wc.MAX_USAGE_LIMIT_WAITS + 1)
        self.assertEqual(oc.attempts, wc.MAX_USAGE_LIMIT_WAITS + 1)
        self.assertEqual(c.slept, list(wc.USAGE_LIMIT_WAIT_SCHEDULE_SECONDS))
        waits = [f for e, f in c.events if e == "leaf_usage_limit_wait"]
        self.assertEqual([w["wait_attempt"] for w in waits], [1, 2, 3])
        declined = [f for e, f in c.events if e == "leaf_usage_limit_wait_declined"]
        self.assertEqual([(d["reason"], d["tag"], d["wait_attempt"]) for d in declined],
                         [("budget_spent", "llm_usage_limit", wc.MAX_USAGE_LIMIT_WAITS + 1)])
        meta = json.loads((c.repo_root / refs.source_dir() / "verdict_meta.json").read_text())
        self.assertEqual(meta["attempts"], wc.MAX_USAGE_LIMIT_WAITS + 1)
        self.assertEqual([a["failure_category"] for a in meta["per_attempt"]],
                         ["pure_transport"] * (wc.MAX_USAGE_LIMIT_WAITS + 1))

    def test_a_flag_off_usage_limit_emits_no_wait_event_at_all(self) -> None:
        """`docs/ORCHESTRATION.md`: "with the flag off nothing is emitted and the death is
        terminal as before", and `docs/RUNBOOK.md` tells the operator to read a
        `leaf_usage_limit_wait_declined` as "an opted-in run still fail_closed". A decline
        emitted on the flag-off path would make that reading false for a run that never opted
        in; the flag check is the FIRST thing `_usage_limit_wait` does, and this pins it."""
        c, refs = self._waiting(self._QUOTA, flag=False)
        oc = _run_verify(c, refs)
        self.assertEqual(oc.status, "fail")
        self.assertEqual(oc.infra_error[0], "llm_usage_limit")
        self.assertEqual(c.slept, [])
        self.assertEqual([e for e, _ in c.events if e.startswith("leaf_usage_limit")], [])

    def test_the_reviewer_waits_only_a_usage_limit_not_any_transport_death(self) -> None:
        """The reviewer call site's tag guard, driven for real. A transient death takes the
        transient retry (2 s), never the usage schedule; a 4xx is terminal at once. Widening the
        guard to "any non-timeout transport death" would park a 429 for 15 minutes AND emit a
        `leaf_usage_limit_wait` whose `tag` is what the death carried — which is why the tag is
        taken from `infra_error` rather than spelled inside `_usage_limit_wait`."""
        c, refs = self._waiting(self._FLAKE)
        oc = _run_verify(c, refs)
        self.assertEqual(oc.status, "pass")
        self.assertEqual(c.slept, [2.0])                                  # transient backoff
        self.assertEqual([e for e, _ in c.events if e.startswith("leaf_usage_limit")], [])
        c, refs = self._waiting(wc.ProcResult(1, "", "API Error: 400 invalid_request_error"))
        oc = _run_verify(c, refs)
        self.assertEqual(oc.status, "fail")
        self.assertEqual(oc.infra_error[0], "llm_client_error")
        self.assertEqual(c.slept, [])
        self.assertEqual([e for e, _ in c.events if e.startswith("leaf_usage_limit")], [])

    def test_the_reviewer_wait_is_not_a_repair_turn(self) -> None:
        """`attempt` and `resume_session_id` are UNCHANGED across a wait, in the reviewer loop:
        (1) a wait on a cold first attempt re-launches COLD — `repair_strategy: none`, no
        `resume_session_id` on the spawn — and the following schema violation still gets its
        repair turn (a wait that spent one would refuse it and fail closed); (2) a wait that
        interrupts a REPAIR turn re-runs that repair — still a `reuse` with the same findings —
        but, on the workshop lineage, COLD in a new lineage seeded from the seeds alone: a dead
        session is never resumed, since it would continue a half-written turn (issue #534 step
        3; `test_workshop_reviewer`'s `test_a_dead_warm_repair_is_retried_cold_never_resumed`).
        Asserted on the launch request and the spawn kwargs, not on the absence of a carrier a
        transport death never sets."""
        # (1) quota on the cold attempt, then EVERY repair turn the budget allows is needed:
        # `MAX_BUNDLE_REPAIR_TURNS` malformed verdicts before the valid one. A wait that spent
        # one `attempt` would refuse the last repair and fail closed (the fixture straddles the
        # budget on purpose — one malformed verdict would pass under that regression).
        from tools.pure_leaf import MAX_BUNDLE_REPAIR_TURNS
        malformed = [wc.ProcResult(0, _envelope("nope"), "")] * MAX_BUNDLE_REPAIR_TURNS
        c, refs = self._waiting(self._QUOTA, *malformed,
                                wc.ProcResult(0, _envelope(_verdict("pass")), ""), record=True)
        oc = _run_verify(c, refs)
        self.assertEqual(oc.status, "pass")
        self.assertEqual(_spawned(c), MAX_BUNDLE_REPAIR_TURNS + 2)
        self.assertEqual(c.slept, [wc.USAGE_LIMIT_WAIT_SCHEDULE_SECONDS[0]])
        self.assertEqual([r["repair_strategy"] for r in _requests(c)],
                         ["none", "none"] + ["reuse"] * MAX_BUNDLE_REPAIR_TURNS)
        # each repair resumes the attempt before it, starting from the cold RE-launch
        # (`child-2`): the dead attempt (`child-1`) is never a resume target
        self.assertEqual([k.get("resume_session_id") for k in c.spawns],
                         [None, None] + [f"child-{n + 2}" for n in range(MAX_BUNDLE_REPAIR_TURNS)])
        # (2) malformed verdict, quota on the repair turn, then the repair passes.
        c, refs = self._waiting(wc.ProcResult(0, _envelope("nope"), ""), self._QUOTA,
                                wc.ProcResult(0, _envelope(_verdict("pass")), ""), record=True)
        oc = _run_verify(c, refs)
        self.assertEqual(oc.status, "pass")
        self.assertEqual(_spawned(c), 3)
        self.assertEqual([r["repair_strategy"] for r in _requests(c)],
                         ["none", "reuse", "reuse"])
        # The repair resumes the reviewer's own session; its re-run after the wait resumes none.
        self.assertEqual([k.get("resume_session_id") for k in c.spawns],
                         [None, "child-1", None])
        self.assertEqual([r["workshop_lineage_id"] for r in _requests(c)],
                         ["child-1", "child-1", "child-3"])
        # ...and the same FINDINGS: the transport death neither overwrote the carriers (the
        # guard above the wait) nor did the wait clear them — a leaf's 160-char evidence line
        # must never become the next repair prompt's findings.
        findings = [str(r.get("repair_findings", "")) for r in _requests(c)[1:]]
        self.assertEqual(findings[0], findings[1])
        self.assertTrue(findings[1])
        self.assertNotIn("usage limit", findings[1].lower())

    def test_a_transient_retry_is_not_a_repair_turn_and_the_record_counts_it(self) -> None:
        """The reviewer twin of the producer row of the same name (issue #508, rounds 1-2).
        (1) One dropped connection on the cold attempt, then a schema-violating verdict on every
        turn: the retry must not spend the repair budget, and the launch count, the outcome's
        `attempts` and `verdict_meta` must all count the flake, `MAX_BUNDLE_REPAIR_TURNS + 2`.
        (2) The connection drops on the first repair turn instead: the retry re-runs that repair
        with the same findings, and still spends no budget — cold, in a new lineage seeded from
        the seeds alone, never resuming the dead session (the workshop lineage's rule)."""
        from tools.pure_leaf import MAX_BUNDLE_REPAIR_TURNS
        malformed = wc.ProcResult(0, _envelope({"verification_status": "pass"}), "")
        c, refs = self._waiting(self._FLAKE, *([malformed] * (MAX_BUNDLE_REPAIR_TURNS + 1)),
                                record=True)
        oc = _run_verify(c, refs)
        self.assertEqual(oc.status, "fail")
        self.assertEqual(c.slept, [2.0])
        self.assertEqual(_spawned(c), MAX_BUNDLE_REPAIR_TURNS + 2)
        self.assertEqual([r["repair_strategy"] for r in _requests(c)],
                         ["none", "none"] + ["reuse"] * MAX_BUNDLE_REPAIR_TURNS)
        self.assertEqual(oc.attempts, MAX_BUNDLE_REPAIR_TURNS + 2)
        vmeta = json.loads((c.repo_root / refs.source_dir() / "verdict_meta.json").read_text())
        self.assertEqual(vmeta["failure_category"], "verdict_schema_violation")
        self.assertEqual(vmeta["attempts"], MAX_BUNDLE_REPAIR_TURNS + 2)
        self.assertEqual(len(vmeta["per_attempt"]), MAX_BUNDLE_REPAIR_TURNS + 2)
        c, refs = self._waiting(malformed, self._FLAKE, *([malformed] * MAX_BUNDLE_REPAIR_TURNS),
                                record=True)
        oc = _run_verify(c, refs)
        self.assertEqual(oc.status, "fail")
        self.assertEqual(_spawned(c), MAX_BUNDLE_REPAIR_TURNS + 2)
        self.assertEqual([r["repair_strategy"] for r in _requests(c)],
                         ["none"] + ["reuse"] * (MAX_BUNDLE_REPAIR_TURNS + 1))
        # the flaked repair (child-2) resumes the reviewer's session; its retry (child-3)
        # resumes none, in a lineage of its own
        self.assertEqual([k.get("resume_session_id") for k in c.spawns][:3],
                         [None, "child-1", None])
        self.assertEqual([r["workshop_lineage_id"] for r in _requests(c)][:3],
                         ["child-1", "child-1", "child-3"])
        findings = [str(r.get("repair_findings", "")) for r in _requests(c)[1:3]]
        self.assertEqual(findings[0], findings[1])
        self.assertTrue(findings[1])

    def test_a_warm_repair_that_never_ran_is_not_a_repair_turn(self) -> None:
        """The reviewer twin of the producer row of the same name (issue #508, round 1): a
        schema-violating verdict on every turn, the lineage home vanishing on the first warm
        repair. The dead turn must not spend the repair budget: every repair the budget allows
        still reaches a leaf (`MAX_BUNDLE_REPAIR_TURNS + 1` spawns), and `verdict_meta` counts
        the launches that ran, not the request that died."""
        from tools.pure_leaf import MAX_BUNDLE_REPAIR_TURNS
        malformed = wc.ProcResult(0, _envelope({"verification_status": "pass"}), "")
        c, refs = self._waiting(malformed, malformed, record=True)
        recorded = c.record_launch
        vanished: list[bool] = []

        def _vanish_once(child_arid, request, entry=None, **kw):
            rec = recorded(child_arid, request, entry, **kw)
            if request.get("warm_resume") and not vanished:
                vanished.append(True)
                return {**(rec or {}), "workshop_lineage_home_missing": True}
            return rec

        c.record_launch = _vanish_once  # type: ignore[assignment]
        oc = _run_verify(c, refs)
        self.assertEqual(oc.status, "fail")
        self.assertEqual(vanished, [True])
        self.assertEqual(_spawned(c), MAX_BUNDLE_REPAIR_TURNS + 1)
        self.assertEqual(len(_requests(c)), MAX_BUNDLE_REPAIR_TURNS + 2)
        self.assertEqual(oc.attempts, MAX_BUNDLE_REPAIR_TURNS + 1)
        vmeta = json.loads((c.repo_root / refs.source_dir() / "verdict_meta.json").read_text())
        self.assertEqual(vmeta["attempts"], MAX_BUNDLE_REPAIR_TURNS + 1)
        self.assertEqual(len(vmeta["per_attempt"]), MAX_BUNDLE_REPAIR_TURNS + 1)

    def test_a_verdict_accepted_after_a_transient_retry_records_every_launch(self) -> None:
        """The reviewer's PASS path counts its launches at three sites — `verdict_meta`, the
        verdict's project meta (`source_meta.json#attempt_count` here) and the terminal row's
        `result_summary` — and only the outcome's `attempts` was pinned (issue #508, round 2;
        the producer twin is `test_a_bundle_accepted_after_a_transient_retry_records_every_launch`).
        One dropped connection, then a passing verdict: two launches, and every record says 2.
        Only a re-launch in the history separates `len(per_attempt)` from `attempt + 1`."""
        c, refs = self._waiting(self._FLAKE)
        oc = _run_verify(c, refs)
        self.assertEqual(oc.status, "pass")
        self.assertEqual(_spawned(c), 2)
        self.assertEqual(oc.attempts, 2)
        base = c.repo_root / refs.source_dir()
        vmeta = json.loads((base / "verdict_meta.json").read_text())
        self.assertEqual(vmeta["result"], "pass")
        self.assertEqual(vmeta["attempts"], 2)
        self.assertEqual(len(vmeta["per_attempt"]), 2)
        self.assertEqual(vmeta["per_attempt"][0]["failure_category"], "pure_transport")
        self.assertEqual(json.loads((base / "source_meta.json").read_text())["attempt_count"], 2)
        payloads = [cap["--agent-run-json"] for sub, cap in c.calls
                    if sub == "finalize-child" and "--agent-run-json" in cap]
        self.assertIn("attempts=2)", json.dumps(payloads[-1]))

    def test_transport_terminal_after_a_schema_repair_records_the_transport_excerpt(self) -> None:
        """The reviewer twin of the producer's
        `test_transport_terminal_after_a_content_repair_records_the_transport_excerpt` (issue
        #508, round 2): a schema-violating verdict, then the repair turn dies of a usage limit
        with the wait flag OFF, which is terminal. `verdict_meta` must pair
        `failure_category: pure_transport` with the TRANSPORT excerpt, never the schema finding
        still held in the repair carrier."""
        c, refs = self._waiting(wc.ProcResult(0, _envelope({"verification_status": "pass"}), ""),
                                self._QUOTA, flag=False, record=True)
        oc = _run_verify(c, refs)
        self.assertEqual(oc.status, "fail")
        self.assertEqual(oc.leaf_returncode, 1)
        stale = _requests(c)[1]["repair_findings"]      # the schema finding the carrier holds
        self.assertTrue(stale)
        vmeta = json.loads((c.repo_root / refs.source_dir() / "verdict_meta.json").read_text())
        self.assertEqual(vmeta["failure_category"], "pure_transport")
        excerpt = vmeta.get("failure_excerpt") or ""
        self.assertIn("usage limit", excerpt.lower())
        self.assertNotIn(stale, excerpt)
        self.assertEqual(vmeta["attempts"], 2)

    def test_unencodable_valid_verdict_is_schema_violation_not_transport(self) -> None:
        # Codex review (defect 1): a schema-SOUND verdict whose last_fail_reason holds a lone
        # surrogate is not UTF-8 persistable. It must be caught as a schema violation (repairable,
        # routable via the verdict table) — NOT accepted and then mis-routed as a host-write /
        # transport failure. A schema-valid verdict must always reach its routing.
        bad = {"verification_status": "fail", "issue_severity": "major",
               "last_fail_reason": "\ud800", "findings": [{"summary": "x"}]}
        c, refs, oc = self._run([_envelope(bad)])  # persistently unencodable -> exhaustion
        self.assertEqual(oc.status, "fail")
        self.assertEqual(oc.leaf_returncode, 0)          # routable, NOT a transport fail_closed
        base = c.repo_root / refs.source_dir()
        self.assertFalse((base / "source_meta.json").exists())
        vmeta = json.loads((base / "verdict_meta.json").read_text())
        self.assertEqual(vmeta["failure_category"], "verdict_schema_violation")

    def test_verdict_meta_write_failure_on_exhaustion_recovers(self) -> None:
        # Codex review (defect 2): the exhaustion-path verdict_meta write must be guarded like the
        # accepted path — a host-write failure recovers as a fail_closed transport outcome, never
        # an uncaught exception escaping run_substep.
        class _C(_reviewer_fake()):  # type: ignore[misc]
            def _write_verdict_meta(self, *a, **k):  # type: ignore[override]
                raise OSError(28, "No space left on device")
        bad = {"verification_status": "pass"}  # persistently schema-invalid -> exhaustion
        c, refs, oc = self._run([_envelope(bad)], cls=_C)
        self.assertEqual(oc.status, "fail")
        self.assertNotEqual(oc.leaf_returncode, 0)
        self.assertEqual(oc.infra_error[0], "pure_verify_host_write_failed")

    def test_finalize_before_source_meta_write_ordering(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        repo = Path(self._tmp.name)
        refs = _verify_node(repo)
        observed: dict[str, bool] = {}

        class _C(_reviewer_fake()):  # type: ignore[misc]
            def finalize_child(self, child_arid, reply_text, agent_run_json):  # type: ignore[override]
                base = self.repo_root / refs.source_dir()
                observed["meta_exists_at_finalize"] = (base / "source_meta.json").exists()
                return super().finalize_child(child_arid, reply_text, agent_run_json)

        c = _reviewer_conductor(repo, [_envelope(_verdict("pass"))], cls=_C)
        oc = _run_verify(c, refs)
        self.assertEqual(oc.status, "pass")
        # The window was closed BEFORE source_meta.json was authored.
        self.assertFalse(observed["meta_exists_at_finalize"])
        self.assertTrue((c.repo_root / refs.source_dir() / "source_meta.json").exists())

    def test_host_write_failure_after_finalize_recovers(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        repo = Path(self._tmp.name)
        refs = _verify_node(repo)
        finalized: dict[str, bool] = {}

        class _C(_reviewer_fake()):  # type: ignore[misc]
            def finalize_child(self, child_arid, reply_text, agent_run_json):  # type: ignore[override]
                finalized["did"] = True
                return super().finalize_child(child_arid, reply_text, agent_run_json)

            def _write_verify_source_meta(self, refs, verdict, *, attempts):  # type: ignore[override]
                raise OSError(28, "No space left on device")

        c = _reviewer_conductor(repo, [_envelope(_verdict("pass"))], cls=_C)
        oc = _run_verify(c, refs)
        self.assertTrue(finalized.get("did"))
        self.assertEqual(oc.status, "fail")
        self.assertNotEqual(oc.leaf_returncode, 0)
        self.assertEqual(oc.infra_error[0], "pure_verify_host_write_failed")

    def test_repair_resumes_only_reviewers_own_session(self) -> None:
        # Persona separation: on the repair turn the resumed session is the reviewer's OWN prior
        # attempt arid (a "child-N" minted in this loop), never an external/producer arid.
        resumes: list = []

        class _C(_reviewer_fake()):  # type: ignore[misc]
            def spawn_leaf(self, prompt_text, child_env, entry=None, **kwargs):  # type: ignore[override]
                resumes.append(kwargs.get("resume_session_id"))
                return super().spawn_leaf(prompt_text, child_env, entry, **kwargs)
        bad = {"verification_status": "pass"}
        c, refs, oc = self._run([_envelope(bad), _envelope(_verdict("pass"))], cls=_C)
        self.assertEqual(oc.status, "pass")
        self.assertIsNone(resumes[0])                      # first turn is a cold launch
        self.assertEqual(resumes[1], "child-1")            # repair resumes the reviewer's arid
        self.assertTrue(all(r is None or str(r).startswith("child-") for r in resumes))


# ======================================================================================
# classify_failure verdict routing (M-D)
# ======================================================================================
class PureVerifyRoutingTests(unittest.TestCase):
    def _outcomes(self, status: str) -> list:
        # SUBSTEPS["generate"] == ("generate","gate","verify"); a verify failure is index 2.
        oc_pass = wc.SubstepOutcome("a", "pass", [])
        oc_last = wc.SubstepOutcome("v", status, [])
        return [oc_pass, oc_pass, oc_last]

    def test_schema_exhaustion_routes_cold_generate_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _verify_node(repo)
            c = _conductor(repo)
            c._write_verdict_meta(refs, result="fail",
                                  failure_category="verdict_schema_violation",
                                  failure_excerpt="issue_severity must be one of ...",
                                  attempts=3, per_attempt=[])
            dec = c.classify_failure(refs, "generate", self._outcomes("fail"))
            self.assertEqual(dec.action, "retry")
            self.assertEqual(dec.target_phase, "generate")
            self.assertEqual(dec.repair_strategy, "restart")
            self.assertEqual(dec.reason, "generate_verdict_verdict_schema_violation")

    def test_cut_read_exhaustion_routes_cold_generate_restart(self) -> None:
        """Issue #547: a codex reading lineage that read past the budget on every turn is
        routed as a schema exhaustion is, not to the severity gate's unclassified end."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _verify_node(repo)
            c = _conductor(repo)
            c._write_verdict_meta(refs, result="fail", failure_category=wc.WORKSHOP_CUT_READ,
                                  failure_excerpt="`cat x` printed 600,000 bytes",
                                  attempts=3, per_attempt=[])
            dec = c.classify_failure(refs, "generate", self._outcomes("fail"))
            self.assertEqual((dec.action, dec.target_phase, dec.repair_strategy, dec.reason),
                             ("retry", "generate", "restart",
                              "generate_verdict_workshop_cut_read"))

    def test_valid_fail_verdict_falls_through_to_severity_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _verify_node(repo)
            c = _conductor(repo)
            # A valid fail verdict wrote source_meta (minor) + a category-less verdict_meta.
            c._write_verify_source_meta(refs, _verdict("fail", severity="minor",
                                                       reason="flux sign"), attempts=1)
            c._write_verdict_meta(refs, result="pass", failure_category=None,
                                  failure_excerpt=None, attempts=1, per_attempt=[])
            dec = c.classify_failure(refs, "generate", self._outcomes("fail"))
            # Fell through to the verify-severity gate (NOT a verdict route).
            self.assertFalse((dec.reason or "").startswith("generate_verdict_"))
            self.assertEqual(dec.reason, "verify_minor")
            self.assertEqual(dec.repair_strategy, "reuse")


# ======================================================================================
# Routing-category drift guard + cold-fallback surrogate safety (parallel_review)
# ======================================================================================
class PureVerifyRoutingDriftTests(unittest.TestCase):
    def test_routing_categories_match_pure_leaf_constants(self) -> None:
        # parallel_review (glm): the routing table must key on the SAME category strings the loop
        # assigns, or a constant rename would silently mis-route. Pin the identity. (The pure
        # reply extractor's two categories, `pure_leaf.RESPONSE_*`, went with it in issue #534
        # PR-J4-b; the reviewer loop assigns the two below.)
        self.assertIn(wc.GENERATE_VERDICT_SCHEMA_VIOLATION, wc.GENERATE_VERDICT_FAILURE_ROUTING)
        self.assertEqual(wc.GENERATE_VERDICT_FAILURE_ROUTING[wc.WORKSHOP_CUT_READ],
                         ("generate", "restart"))
        # Not repairable and not routed: an unreadable rollout ends fail_closed by its rc.
        self.assertNotIn(wc.WORKSHOP_ROLLOUT_UNREADABLE, wc.GENERATE_VERDICT_FAILURE_ROUTING)
        self.assertNotIn(wc.WORKSHOP_ROLLOUT_UNREADABLE, wc.COMPILE_VERDICT_FAILURE_ROUTING)


class PureVerifyColdFallbackSurrogateTests(unittest.TestCase):
    def test_cold_fallback_repair_with_surrogate_does_not_crash(self) -> None:
        # parallel_review (glm §5.3): a verdict carrying an unpaired surrogate goes to the
        # verdict_schema_violation repair path; on a COLD fallback (session not resumable) the
        # repair request that record_launch writes as UTF-8 carries what the violation says of
        # it (the pure loop also echoed the verdict itself as `prior_document`; a workshop
        # lineage carries it as a file in the new tree). It must be normalized so the write
        # does not raise UnicodeEncodeError mid-repair.
        class _C(_reviewer_fake()):  # type: ignore[misc]
            resumable = False  # force the cold-fallback repair branch on every turn

            def record_launch(self, child_arid, request, entry=None, **kwargs):  # type: ignore[override]
                # Emulate the real record_launch's UTF-8 prompt persistence to surface any
                # non-encodable request field as the real path would.
                json.dumps(request, ensure_ascii=False).encode("utf-8")
                return super().record_launch(child_arid, request, entry, **kwargs)
        bad = {"verification_status": "fail", "issue_severity": "major",
               "last_fail_reason": "\ud800", "findings": [{"summary": "x"}]}
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _verify_node(repo)
            c = _reviewer_conductor(repo, [_envelope(bad)], cls=_C)
            oc = _run_verify(c, refs)  # must not raise
            self.assertEqual(oc.status, "fail")

    def test_surrogate_in_findings_does_not_crash_repair_or_meta(self) -> None:
        # parallel_review round 2 (kimi §2.A): `last_excerpt` (findings) flows into both the repair
        # turn's `repair_findings` and verdict_meta's failure_excerpt, both persisted as UTF-8. Even
        # if a violation message ever carried a lone surrogate, capture-time normalization must keep
        # every downstream write from raising. Force it by making the validator emit one.
        from unittest import mock
        import tools.pure_leaf as pl

        class _C(_reviewer_fake()):  # type: ignore[misc]
            def record_launch(self, child_arid, request, entry=None, **kwargs):  # type: ignore[override]
                json.dumps(request, ensure_ascii=False).encode("utf-8")  # emulate prompt persist
                return super().record_launch(child_arid, request, entry, **kwargs)
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _verify_node(repo)
            # schema-invalid -> repair
            c = _reviewer_conductor(repo, [_envelope({"verification_status": "pass"})], cls=_C)
            with mock.patch.object(pl, "verify_verdict_violations",
                                   return_value=["bad field value \ud800 here"]):
                oc = _run_verify(c, refs)  # must not raise
            self.assertEqual(oc.status, "fail")
            # verdict_meta persisted cleanly (excerpt normalized), and its write did not fail_close.
            vmeta = json.loads((c.repo_root / refs.source_dir() / "verdict_meta.json").read_text())
            self.assertEqual(vmeta["failure_category"], "verdict_schema_violation")
            self.assertEqual(oc.leaf_returncode, 0)   # NOT a host-write fail_close


# ======================================================================================
# build_launch_request pure verify variant
# ======================================================================================
class PureVerifyLaunchRequestTests(unittest.TestCase):
    def test_pure_verify_request_shape(self) -> None:
        """The verify request `build_launch_request` builds for a reviewer leaf: a workshop
        request (issue #534 PR-J4-b) stamped with the one prompt contract version, no leaf
        write set, no skill, the context present. A host-rendered request that names no
        lineage or provider is refused at the builder."""
        with tempfile.TemporaryDirectory() as tmp:
            refs = _verify_node(Path(tmp))
            kwargs = dict(
                step="generate", substep="verify", orchestration_id="o",
                orchestration_agent_run_id="orch", child_agent_run_id="c",
                agent_model="opus", workflow_mode="dev",
                runner_host_authored=True,
                pure_leaf=True,
                pure_context={"controlled_spec_document": "cs", "tests_document": "t",
                              "ir_document": "ir",
                              "checks_module_contract_document": "## 1. The fixed ABI",
                              "bundle_document": "b"})
            req = wc.build_launch_request(refs, workshop_lineage_id="c",
                                          workshop_provider="claude_cli", **kwargs)
            self.assertEqual(req["leaf_mode"], "workshop")
            self.assertEqual(req["substep"], "verify")
            self.assertEqual(req["prompt_contract_version"], PURE_PROMPT_CONTRACT_VERSION)
            self.assertNotIn("workshop_prompt_contract_version", req)
            self.assertEqual(req["allowed_output_paths"], [])
            self.assertEqual(req["skill_name"], "")
            self.assertIn("pure_context", req)
            for missing in ("workshop_lineage_id", "workshop_provider"):
                with self.subTest(missing=missing), self.assertRaises(ValueError):
                    wc.build_launch_request(refs, **kwargs, **{
                        k: v for k, v in (("workshop_lineage_id", "c"),
                                          ("workshop_provider", "claude_cli"))
                        if k != missing})


# ======================================================================================
# verify output-contract paragraph (read by the cold repair)
# ======================================================================================
class PureVerifyOutputContractTests(unittest.TestCase):
    # Issue #391: a failing verdict lists every defect, one entry each, and its severity is the
    # value that comes first in the order `major`, `critical`, `minor`. The rule is in the
    # output-contract paragraph, which a cold repair re-states with the rest of the launch
    # body, so it is read from the composed template, not from the template file. Each sentence is pinned WHOLE: a key-phrase
    # probe let a weakened ("… you choose to report"), negated or reversed rule stay green
    # (round 1), and only the drift and allowlist hashes, which every legitimate edit re-pins,
    # noticed. Rewording a sentence therefore means rewording it here too.
    _EVERY_DEFECT_PHRASES = (
        ("A `fail` lists EVERY defect your review found, whether its subject is the artifact "
         "under review or an input, one `findings` entry per defect — not the first one met, and "
         "not several folded into one entry: the "
         "host hands the producer the whole list at once, and a defect this verdict leaves out "
         "is found only by a later verdict, at the cost of one more attempt from a finite "
         "budget."),
        ("`last_fail_reason` states the corrections as a whole; it does not repeat the first "
         "finding."),
        ("When the findings earn different values under the severity rubric, `issue_severity` "
         "is the one that comes first in the order `major`, `critical`, `minor`: a `major` names "
         "an input that no re-run of the producer repairs, which stays true whatever else the "
         "verdict carries, and a `critical` calls for a cold re-run of the producer, which "
         "discards the artifact under review that every `minor` is about."),
    )
    _VERIFY_REQUESTS = (
        {"step": "generate", "substep": "verify"},
        {"step": "generate", "substep": "verify", "pure_shape": "harness"},
        {"step": "compile", "substep": "verify"},
    )

    def _lifted_verify_paragraphs(self) -> list[str]:
        import tools.orchestration_runtime as ort
        texts = []
        for extra in self._VERIFY_REQUESTS:
            req = {"leaf_mode": "workshop", "workshop_provider": "claude_cli",
                   "pure_language": "fortran", "pure_parallel": "openmp",
                   "prompt_contract_version": PURE_PROMPT_CONTRACT_VERSION, **extra}
            # The `Output contract` paragraph of each verify launch template, read where the
            # cold repair reads it: the composed template itself (issue #510).
            texts.append(next(b.strip() for b in ort._pure_launch_template(req).split("\n\n")
                              if b.lstrip().startswith("Output contract")))
        return texts

    def _missing_every_defect_phrases(self, text: str) -> list[str]:
        return [p for p in self._EVERY_DEFECT_PHRASES if p not in text]

    def test_every_verify_template_asks_a_fail_to_list_every_defect(self) -> None:
        for extra, text in zip(self._VERIFY_REQUESTS, self._lifted_verify_paragraphs()):
            with self.subTest(request=extra):
                self.assertTrue(text.startswith("Output contract (verify verdict)"))
                self.assertEqual(self._missing_every_defect_phrases(text), [])
                # The insertion did not split the paragraph: its closing clause is still lifted.
                self.assertIn("more than one document", text)

    def test_the_three_verify_output_contracts_are_one_text(self) -> None:
        texts = self._lifted_verify_paragraphs()
        self.assertTrue(texts[0])
        self.assertEqual(texts[1], texts[0])
        self.assertEqual(texts[2], texts[0])

    def test_a_cold_verify_repair_carries_the_rule_and_a_warm_one_does_not(self) -> None:
        """The composed template is the source; the cold repair TURN is the delivery. A round-2 review
        mutation that dropped the output contract from the cold repair of the verdict pairs
        alone left every suite green, so the rendered repair prompt is read here for each
        verify request."""
        import tools.orchestration_runtime as ort
        for extra in self._VERIFY_REQUESTS:
            req = {"leaf_mode": "workshop", "workshop_provider": "claude_cli",
                   "pure_language": "fortran", "pure_parallel": "openmp",
                   "prompt_contract_version": PURE_PROMPT_CONTRACT_VERSION,
                   "repair_findings": "verdict JSON did not parse", **extra}
            with self.subTest(request=extra):
                cold = ort._render_pure_repair_prompt(req)
                for phrase in self._EVERY_DEFECT_PHRASES:
                    self.assertEqual(cold.count(phrase), 1, phrase)
                warm = ort._render_pure_repair_prompt({**req, "warm_resume": True})
                # Self-test: the warm render is a real repair prompt, so its lacking the rule
                # is the warm/cold split and not an empty render.
                self.assertIn("verdict JSON did not parse", warm)
                self.assertEqual(self._missing_every_defect_phrases(warm),
                                 list(self._EVERY_DEFECT_PHRASES))

    def test_the_phrase_probe_fails_when_the_rule_is_removed(self) -> None:
        """Self-test: with the #391 sentences cut out of the lifted paragraph, the probe above
        reports every phrase missing, so a deleted rule is red rather than vacuously green."""
        text = self._lifted_verify_paragraphs()[0]
        start = text.index("A `fail` lists EVERY defect")
        end = text.index("Use the exact lowercase enum literals.")
        self.assertEqual(self._missing_every_defect_phrases(text[:start] + text[end:]),
                         list(self._EVERY_DEFECT_PHRASES))


# ======================================================================================
# Full pure generate phase through the real step_result validator (subagent review round 1)
# ======================================================================================
class PureStepResultValidationTests(unittest.TestCase):
    """A passing generate phase whose two leaves were PURE must clear
    `_validate_step_result_payload` — the records a pure leaf wrote before issue #534 PR-J4-b
    retired it are still read (a record reader keeps reading old rows).

    The pure producer AND the pure verify reviewer both finalized with output_refs==[] (the
    host authors model/checks/source_meta after the child windows close). Without the
    host-rendered carve-out the per-step-result validator raises — `substep must publish
    non-empty output_refs` and `required_outputs must be satisfied by substep output_refs` — so
    such a node could never certify. This exercises the real validator end-to-end (the unit
    loops stub the runtime, so they miss this)."""

    def _setup(self, tmp: str):
        import tools.orchestration_runtime as ort
        repo = Path(tmp)
        refs = _verify_node(repo)
        oid = "o"
        orch = repo / "workspace" / "orchestrations" / oid
        (orch / "launches").mkdir(parents=True, exist_ok=True)
        gen_arid, ver_arid = "gen-pure-1", "ver-pure-1"
        # agent_runs.jsonl: both substeps pure, pass, empty output_refs.
        rows = [
            {"agent_run_id": gen_arid, "agent_role": "substep", "node_key": _NODE,
             "step": "generate", "substep": "generate", "status": "pass", "output_refs": []},
            {"agent_run_id": ver_arid, "agent_role": "substep", "node_key": _NODE,
             "step": "generate", "substep": "verify", "status": "pass", "output_refs": []},
        ]
        (orch / "agent_runs.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        for arid, substep in ((gen_arid, "generate"), (ver_arid, "verify")):
            (orch / "launches" / f"{arid}.request.json").write_text(
                json.dumps({"leaf_mode": "pure", "step": "generate", "substep": substep}),
                encoding="utf-8")
        # Host-authored deliverables on disk (what the pure producer + verify write).
        required = wc.phase_required_outputs(refs, "generate", runner_host_authored=True,
                                          bundle_facts=_FORTRAN_BUNDLE_FACTS, control_file_basename="Makefile")
        for ref in required:
            p = repo / ref
            p.parent.mkdir(parents=True, exist_ok=True)
            if ref.endswith("source_meta.json"):
                p.write_text(json.dumps({
                    "source_id": refs.source_id, "node_key": _NODE, "attempt_count": 1,
                    "verification_status": "pass", "issue_severity": "none",
                    "last_fail_reason": None, "debug_mode": True, "context_isolated": True,
                }), encoding="utf-8")
            else:
                p.write_text("module m\nend module\n", encoding="utf-8")
        payload = {
            "status": "pass", "validation_stage": "post_generate",
            "substep_agent_run_ids": [gen_arid, ver_arid], "failed_substeps": [],
            "retry_decisions": None, "required_outputs": required,
        }
        return ort, repo, oid, refs, payload, required

    def test_passing_pure_generate_step_result_validates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ort, repo, oid, refs, payload, _ = self._setup(tmp)
            # Must NOT raise — the pure carve-out vouches host-authored deliverables by existence.
            ort._validate_step_result_payload(
                repo, oid, node_key=_NODE, step="generate", agent_run_id="orch", payload=payload)

    def test_missing_host_authored_deliverable_still_fails(self) -> None:
        # Anti-mock-green: the carve-out vouches by ON-DISK existence, so a declared-but-unauthored
        # deliverable must still fail (not be waved through).
        with tempfile.TemporaryDirectory() as tmp:
            ort, repo, oid, refs, payload, required = self._setup(tmp)
            model_ref = next(r for r in required if r.endswith("_model.f90"))
            (repo / model_ref).unlink()
            with self.assertRaises(ValueError):
                ort._validate_step_result_payload(
                    repo, oid, node_key=_NODE, step="generate", agent_run_id="orch", payload=payload)


class WorkshopStepResultValidationTests(PureStepResultValidationTests):
    """The same phase with both leaves workshop leaves (issue #534; every leaf since PR-J4-b):
    their terminal rows also carry output_refs == [], and the step-result validator vouches the
    host-written deliverables by existence exactly as it did for the pure leaves'."""

    def _setup(self, tmp: str):
        ort, repo, oid, refs, payload, required = super()._setup(tmp)
        for arid, substep in (("gen-pure-1", "generate"), ("ver-pure-1", "verify")):
            path = (repo / "workspace" / "orchestrations" / oid / "launches"
                    / f"{arid}.request.json")
            path.write_text(json.dumps({"leaf_mode": "workshop", "step": "generate",
                                        "substep": substep}), encoding="utf-8")
        return ort, repo, oid, refs, payload, required


class PureVerifyTransientWallClockBudgetScopeTest(unittest.TestCase):
    """The reviewer loop's copy of the transient wall-clock budget.

    The rule has two implementations (the producer loop's and the reviewer loop's), and this
    repository's own history says a rule with two
    implementations drifts at the untested one: the producer loop's copy was pinned first and the
    reviewer's was not, so the budget could be removed here outright — or the accumulator hoisted
    back above the transport branch, which is the exact defect the producer side was fixed for —
    with the whole suite still green."""

    @staticmethod
    def _clocked() -> type:
        """The reviewer fake whose every launch advances the fixture's monotonic clock by its
        scripted duration (`seconds`, the last repeating)."""
        class _C(_reviewer_fake()):  # type: ignore[misc]
            def spawn_leaf(self, prompt_text, child_env, entry=None, **kwargs):  # type: ignore[override]
                index = len(self.__dict__.get("spawns", []))
                self.clock[0] += self.seconds[min(index, len(self.seconds) - 1)]
                return super().spawn_leaf(prompt_text, child_env, entry, **kwargs)
        return _C

    def _run(self, procs: list, seconds: "list[float]", **kw):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        repo = Path(self._tmp.name)
        refs = _verify_node(repo)
        c = _reviewer_conductor(repo, procs, cls=self._clocked(), **kw)
        c.seconds = seconds
        c.clock = [1_752_200_000.0]
        events: list = []
        c.emit = lambda event, **f: events.append({"event": event, **f})  # type: ignore
        # Only `monotonic` is driven by the fixture: the budget measures a DURATION, and a
        # fixture that moved both clocks together could not tell the two readings apart — which
        # is how the producer-side pin missed that the pure loops were reading a wall clock.
        with mock.patch.object(wc.time, "monotonic", lambda: c.clock[0]):
            oc = _run_verify(c, refs)
        return c, oc, events

    _FLAKE = wc.ProcResult(1, "", "API Error: Connection closed mid-response.")

    def test_a_usage_limit_wait_does_not_regrant_the_transient_count_budget(self) -> None:
        """The transient COUNT budget (`MAX_LEAF_TRANSIENT_RETRIES`) survives a wait unchanged,
        as the wall-clock one does (the reviewer twin of the producer's row): two flakes spend
        it, a quota is waited, and a THIRD flake is terminal — a wait that reset
        `transient_retries` would grant it a retry and hide a persistent fault behind up to
        three extra launches per wait. Reviewer loop, real."""
        c, oc, events = self._run(
            [self._FLAKE, self._FLAKE, wc.ProcResult(1, "", "Claude AI usage limit reached"),
             self._FLAKE, wc.ProcResult(0, _envelope(_verdict("pass")), "")],
            [2.0], wait_usage_reset=True)
        self.assertEqual(oc.status, "fail")
        self.assertEqual(oc.infra_error[0], "llm_transport_flake")
        self.assertEqual(_spawned(c), 4)                       # flake, flake, quota, flake — done
        self.assertEqual(c.slept, [2.0, 10.0, wc.USAGE_LIMIT_WAIT_SCHEDULE_SECONDS[0]])
        self.assertEqual([e["event"] for e in events].count("leaf_transient_retry"), 2)

    def test_a_transient_attempt_that_burned_the_clock_is_refused(self) -> None:
        """The incident's own shape on the reviewer leaf: one 613 s death, no second launch."""
        c, oc, events = self._run(
            [self._FLAKE, wc.ProcResult(0, _envelope(_verdict("pass")), "")], [613.0])
        self.assertEqual(oc.status, "fail")
        self.assertEqual(_spawned(c), 1)
        declined = [e for e in events if e["event"] == "leaf_transient_retry_declined"]
        self.assertEqual(len(declined), 1)
        self.assertEqual(declined[0]["reason"], "wall_clock_budget")

    def test_the_budget_accumulates_across_transient_attempts(self) -> None:
        """The reviewer loop's copy of the ARITHMETIC, as distinct from the placement. Two 350 s
        deaths: neither crosses the budget alone, together they do."""
        c, oc, events = self._run(
            [self._FLAKE, self._FLAKE, wc.ProcResult(0, _envelope(_verdict("pass")), "")],
            [350.0, 350.0, 5.0])
        self.assertEqual(oc.status, "fail")
        self.assertEqual(_spawned(c), 2)
        declined = [e for e in events if e["event"] == "leaf_transient_retry_declined"]
        self.assertEqual(len(declined), 1)
        self.assertAlmostEqual(declined[0]["spent_seconds"], 350.0)

    def test_a_cheap_transient_failure_is_still_retried(self) -> None:
        """The control: the budget must not touch the flake the count budget exists for."""
        c, oc, events = self._run(
            [self._FLAKE, wc.ProcResult(0, _envelope(_verdict("pass")), "")], [2.0, 3.0])
        self.assertEqual(oc.status, "pass")
        self.assertEqual(_spawned(c), 2)
        self.assertEqual([e["event"] for e in events].count("leaf_transient_retry"), 1)

    def test_a_slow_verdict_repair_turn_does_not_spend_the_transient_budget(self) -> None:
        """A reviewer whose verdict fails schema validation after 700 s is repaired, not
        retried — and the two-second network flake that follows must still get its retry."""
        c, oc, events = self._run(
            [wc.ProcResult(0, _envelope('{"verdict_schema_version": "9.9.9"}'), ""),
             self._FLAKE,
             wc.ProcResult(0, _envelope(_verdict("pass")), "")],
            [700.0, 2.0, 5.0])
        self.assertEqual(oc.status, "pass")
        self.assertEqual([e["event"] for e in events].count("leaf_transient_retry"), 1)
        self.assertEqual(
            [e["event"] for e in events].count("leaf_transient_retry_declined"), 0)


# ======================================================================================
# The `harness` bundle shape's reviewer (issue #169) — driven through the loop
# ======================================================================================
class PureHarnessVerifyWiringTests(unittest.TestCase):
    """The reviewer half of the second shape, driven through the reviewer loop and
    `run_substep` rather than by calling `_pure_reviewer_spec` directly.

    Choosing the spec and WIRING the choice into the two call sites are different things: a
    round-0 mutation sweep reported both call sites as unpinned while the spec's own rows were
    green, which is exactly the "the predicate has a test, the call site does not" shape.
    """

    #: The reviewer context reads two of these; the third is here because this class also drives
    #: the PRODUCER through `run_substep`, and that context reads the checks-module contract.
    _REPO_DOCS = ("docs/workflow/RUNNER_OUTPUT_CONTRACT.md",
                  "docs/workflow/CHECKS_MODULE_CONTRACT.md",
                  "docs/workflow/phases/phase_02_generate.md")

    def setUp(self) -> None:
        from tools.tests.test_pure_leaf_producer import (
            _write_harness_node, _harness_bundle, _HARNESS_SPEC_ID)
        self._harness_bundle = _harness_bundle
        self._spec_id = _HARNESS_SPEC_ID
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        real_root = Path(wc.__file__).resolve().parents[1]
        for rel in self._REPO_DOCS:
            dest = self.repo / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(real_root / rel, dest)
        self.refs = _write_harness_node(self.repo)
        src = self.repo / self.refs.source_dir()
        src.mkdir(parents=True, exist_ok=True)
        (src / "codegen_bundle.json").write_text(
            json.dumps(_harness_bundle()), encoding="utf-8")
        self.c = _reviewer_conductor(self.repo, [_envelope(_verdict("pass"))])

    def _last_request(self) -> dict:
        return [cap["--request-json"] for sub, cap in self.c.calls
                if sub == "record-launch"][-1]

    def test_every_repository_document_of_this_context_fails_CLOSED(self) -> None:
        """One row per document the harness reviewer's context reads — repository document or
        node artifact — each REMOVING that document and requiring the failure to name it.

        A round-3 census found the severity rubric's two raises here vacuous while the same
        mutation on the m3c twin was killed — a fail-closed guard copied into the new shape with
        its witness left behind on the old one, and the mutation direction is fail-OPEN (the
        reviewer would judge with an empty rubric and choose `issue_severity` by how heavy the
        defect looks, which is what terminalized a run before issue #143).

        The unsliceable case is driven for the rubric too, because a document present but
        re-organized is the failure the slicer exists for and reads nothing like a missing file.
        Set identity against the context builder's own output is asserted last, so a document
        added later is either given a row or shows up here."""
        cases = {
            "docs/workflow/RUNNER_OUTPUT_CONTRACT.md": "pure_runner_output_contract_document_missing",
            "docs/workflow/phases/phase_02_generate.md": "pure_severity_rubric_document_missing",
            # The node artifacts raise too since issue #467 (they degraded to "" before).
            f"{self.refs.spec_path}/controlled_spec.md": "pure_controlled_spec_document_missing",
            f"{self.refs.spec_path}/tests.md": "pure_tests_document_missing",
            f"{self.refs.ir_ref}/spec.ir.yaml": "pure_ir_document_missing",
            f"{self.refs.source_dir()}/codegen_bundle.json": "pure_bundle_document_missing",
        }
        for rel, reason in cases.items():
            with self.subTest(document=rel):
                target = self.repo / rel
                body = target.read_text(encoding="utf-8")
                target.unlink()
                try:
                    with self.assertRaises(RuntimeError) as caught:
                        self.c._build_pure_harness_verify_context(self.refs)
                    self.assertIn(reason, str(caught.exception))
                finally:
                    target.write_text(body, encoding="utf-8")
        # ...and a rubric whose anchors moved is a DIFFERENT named failure, not a blank slot.
        phase_doc = self.repo / "docs/workflow/phases/phase_02_generate.md"
        body = phase_doc.read_text(encoding="utf-8")
        phase_doc.write_text(body.replace("#### Severity of a finding", "#### Grading"),
                             encoding="utf-8")
        try:
            with self.assertRaises(RuntimeError) as caught:
                self.c._build_pure_harness_verify_context(self.refs)
            self.assertIn("pure_severity_rubric_document_unsliceable", str(caught.exception))
        finally:
            phase_doc.write_text(body, encoding="utf-8")
        # The rows above cover every document of the context; none degrades to "" (issue #467).
        # Set identity against the builder's own output, so a document added later is either
        # given a row above or shows up here. (An earlier form subtracted the reason STRINGS
        # from a set of context KEYS — a no-op term a round-4 reviewer spotted; the sets are
        # named explicitly now.)
        ctx = self.c._build_pure_harness_verify_context(self.refs)
        raising = {"runner_output_contract_document", "severity_rubric_document",
                   "controlled_spec_document", "tests_document", "ir_document",
                   "bundle_document"}
        # Host data, read off the run's own loaded profile (issue #284): nothing to be missing.
        host = {"target_profile"}
        self.assertEqual(set(ctx), raising | host)
        self.assertEqual(json.loads(ctx["target_profile"]), self.c.target.doc)
        self.assertEqual(len(cases), len(raising))

    def test_a_missing_node_artifact_is_a_recorded_fail_closed_outcome(self) -> None:
        """Issue #467, driven through the production loop for this shape too: the bundle under
        review gone, a recorded `pure_context_assembly_failed`, no `record-launch`."""
        (self.repo / self.refs.source_dir() / "codegen_bundle.json").unlink()
        oc = _run_verify(self.c, self.refs)
        self.assertEqual(oc.status, "fail")
        self.assertEqual(oc.infra_error[0], "pure_context_assembly_failed")
        self.assertIn("pure_bundle_document_missing", oc.infra_error[1])
        self.assertEqual(_spawned(self.c), 0)
        self.assertEqual([sub for sub, _ in getattr(self.c, "calls", [])
                          if sub == "record-launch"], [])

    def test_the_reviewer_loop_wires_the_harness_shape_through(self) -> None:
        """The harness shape's CONTEXT reaches the reviewer's request. Driven through
        `run_substep`, the one call site left since the `_run_pure_verify_substep` seam went in
        issue #534 PR-J4-b."""
        oc = self.c.run_substep(self.refs, "generate", "verify")
        self.assertEqual(oc.status, "pass", getattr(oc, "failure_excerpt", None))
        request = self._last_request()
        self.assertEqual(request["pure_shape"], "harness")
        self.assertEqual(
            sorted(request["pure_context"]),
            sorted(wc_ort.PURE_CONTEXT_REQUIRED_KEYS_BY_SHAPE[
                ("generate", "verify", "harness")]))
        # ...and the runner-output contract really is what the fifth slot carries, not the
        # checks-module ABI the m3c reviewer receives.
        self.assertNotIn("checks_module_contract_document", request["pure_context"])
        self.assertIn("runner_output_contract_document", request["pure_context"])

    def test_run_substep_dispatches_the_reviewer_with_the_shape(self) -> None:
        """`run_substep`'s reviewer dispatch resolves the shape itself."""
        oc = self.c.run_substep(self.refs, "generate", "verify")
        self.assertEqual(oc.status, "pass", getattr(oc, "failure_excerpt", None))
        self.assertEqual(self._last_request()["pure_shape"], "harness")

    def test_run_substep_dispatches_the_producer_with_the_shape(self) -> None:
        from tools.tests.test_pure_leaf_producer import _harness_bundle, _workshop_conductor
        producer = _workshop_conductor(self.repo)
        producer.envelopes = [_envelope(_harness_bundle())]
        oc = producer.run_substep(self.refs, "generate", "generate")
        self.assertEqual(oc.status, "pass", getattr(oc, "failure_excerpt", None))
        request = _requests(producer)[-1]
        self.assertEqual(request["pure_shape"], "harness")
        self.assertIn("runner_output_contract_document", request["pure_context"])

    def test_walltime_scope_is_stated_once_in_the_inlined_contract(self) -> None:
        """Issue #365: two verifiers given byte-identical prompts demanded opposite perf-write
        orders, because the inlined contract said `walltime_sec` was "the whole execution" while
        the IR wrote perf before the self-test re-parsed it. The contract's §2 now says the one
        write and the interval once; this row keeps the removed phrasings out of the inlined
        contract and the two harness templates, and out of `docs/PERFORMANCE_DIAGNOSTICS.md`,
        which cites §2 (the harness `controlled_spec.md` order sentences are not read here). It
        is a literal guard on
        the removed phrasings, not a reader of meaning: a rephrased reversal in the contract or
        a template is caught by the drift pin's digest and reaches a leaf only through a
        deliberate version bump, while a `controlled_spec.md` edit moves only that node's
        derivation key, which no test reads for meaning."""
        ctx = self.c._build_pure_harness_verify_context(self.refs)
        contract = ctx["runner_output_contract_document"]
        self.assertIn("final before `write_perf`", contract)
        self.assertNotIn("whole execution", contract)
        self.assertNotIn("per single case execution", contract)
        real_root = Path(wc.__file__).resolve().parents[1]
        perf_doc = (real_root / "docs" / "PERFORMANCE_DIAGNOSTICS.md").read_text(encoding="utf-8")
        self.assertNotIn("whole execution", perf_doc)
        self.assertNotIn("per single case execution", perf_doc)
        self.assertIn("RUNNER_OUTPUT_CONTRACT.md` §2", perf_doc)
        for name in ("workshop_generate_generate_harness.txt",
                     "workshop_generate_verify_harness.txt"):
            with self.subTest(template=name):
                text = (real_root / "tools" / "prompt_templates" / name).read_text(
                    encoding="utf-8")
                self.assertNotIn("whole execution", text)
                sentences = [s for s in re.split(r"(?<=\.)\s", text)
                             if "The performance document" in s]
                self.assertTrue(sentences, name)
                for s in sentences:
                    self.assertNotIn("per case", s)
                    self.assertIn("once per run", s)

    def test_the_m3c_node_still_carries_no_shape_through_the_same_sites(self) -> None:
        """The negative half: `pure_shape` is ABSENT on the default shape, so a mutation that
        stamped one unconditionally is red here rather than green on the harness rows above."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _verify_node(repo)
            c = _reviewer_conductor(repo, [_envelope(_verdict("pass"))])
            c.run_substep(refs, "generate", "verify")
            request = [cap["--request-json"] for sub, cap in c.calls
                       if sub == "record-launch"][-1]
        self.assertNotIn("pure_shape", request)
        self.assertIn("checks_module_contract_document", request["pure_context"])


if __name__ == "__main__":
    unittest.main()
