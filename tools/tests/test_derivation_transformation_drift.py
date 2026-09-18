"""Drift guards for the derivation TRANSFORMATION versions (issue #250, Z5).

A derivation key (`tools/derivation.py`) hashes a phase's contract inputs together with the
version of the transformation that turns them into an output. The LLM half of that version is
`PURE_PROMPT_CONTRACT_VERSION`, whose coupled surface `test_pure_prompt_contract_drift.py` pins.
This module pins the REST:

* `COMPILE_INLINED_DOCUMENTS_VERSION` — the four documents the compile pair inlines IN FULL
  (`phase_01_compile.md`, the two IR examples, the `impl_defaults` schema). The prompt drift
  test refuses to pin them under `PURE_PROMPT_CONTRACT_VERSION` because a bump of THAT version
  has two side effects (it stops `_resolve_exemplar_source` offering earlier-version exemplars
  and refuses `--resume` across it); a bump of this one has exactly one effect — every node's
  Compile re-derives — and re-pinning without a bump is the maintainer saying "this edit did not
  change what a valid IR is". Either way the decision is made ONCE, here, instead of a typo
  fix invalidating the corpus through a per-node hash.
* `RENDER_VERSION` / `BUILD_VERSION` / `EXECUTE_VERSION` / `VERDICT_VERSION` — the identity of
  the code that implements each deterministic stage, as a digest of the implementing FUNCTIONS'
  source (`inspect.getsource`, for the conductor's methods, which live in a file that changes on
  most branches) and of whole FILES (for the dedicated modules, which change rarely). A digest
  moves on a comment edit too; that is accepted — the alternative, pinning behaviour, is what the
  stages' own tests do, and this pin is only the prompt to ask "did the transformation change".

Each pin here is resolved the same way as the prompt-contract one: on failure, EITHER re-pin
the digest (behaviour-preserving edit) OR bump the constant in `tools/derivation.py` AND add
the new pin (every key of that phase moves). Historical pins are kept so an empty bump or a
silent revert is refused.

The third class of this module is the inlined-document CENSUS: every `pure_context` key the
launch tables declare is classified as node INPUT DATA (hashed per node by the derivation
inputs, or produced by an upstream phase whose output hash is), a CONTRACT document pinned by
the prompt drift test, or a compile-inlined document pinned here. A key added to a table without
a row here fails, so a static document can never be inlined without a version watching it.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import unittest
from pathlib import Path

import tools.derivation as d
import tools.orchestration_runtime as ort
import tools.workflow_conductor as wc

_REPO = Path(wc.__file__).resolve().parents[1]


def _file_digest(rel: str) -> str:
    return hashlib.sha256((_REPO / rel).read_bytes()).hexdigest()


def _source_digest(obj: object) -> str:
    return hashlib.sha256(inspect.getsource(obj).encode("utf-8")).hexdigest()  # type: ignore[arg-type]


def _digest(members: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(members, sort_keys=True).encode("utf-8")).hexdigest()


# The compile-inlined documents, by the KEY the compile builders declare them under
# (`PURE_CONTEXT_REQUIRED_KEYS[("compile", "generate")]`) and the path each reads
# (`Conductor._build_pure_compile_context`).
COMPILE_INLINED_DOCUMENTS: dict[str, str] = {
    "phase_contract_document": ort.WORKFLOW_PHASE_DOC_BY_STEP["compile"],
    "ir_algorithm_example_document": "docs/examples/spec_ir_algorithm_section.example.yaml",
    "ir_algorithm_2d_example_document":
        "docs/examples/spec_ir_algorithm_2d_problem_contract.example.yaml",
    "impl_defaults_schema_document": "spec/schema/ir/impl_defaults.schema.json",
}


def compile_documents_tuple() -> dict[str, str]:
    return {key: _file_digest(rel) for key, rel in COMPILE_INLINED_DOCUMENTS.items()}


def render_tuple() -> dict[str, str]:
    """The host-rendered runner and control file: the language backend's runner renderer, the
    host render module, the IR-shaped and the bundle-derived control-file writers."""
    return {
        "tools/host_render.py": _file_digest("tools/host_render.py"),
        "tools/backends/language/fortran/runner.py":
            _file_digest("tools/backends/language/fortran/runner.py"),
        "Conductor._write_runner": _source_digest(wc.Conductor._write_runner),
        "Conductor._write_makefile": _source_digest(wc.Conductor._write_makefile),
        "Conductor._render_pure_makefile_from_graph":
            _source_digest(wc.Conductor._render_pure_makefile_from_graph),
        "Conductor._write_pure_bundle_artifacts":
            _source_digest(wc.Conductor._write_pure_bundle_artifacts),
    }


def _server():
    return ort._build_runtime_server_module()


def build_tuple() -> dict[str, str]:
    """The in-process build: the server's `compile_project` and its command runner, the
    conductor's build body and its dependency staging, the bundle build-graph derivation."""
    import tools.codegen_bundle as cb
    server = _server()
    return {
        "build_runtime_server.tool_compile_project": _source_digest(server.tool_compile_project),
        "build_runtime_server._run_command": _source_digest(server._run_command),
        "Conductor._build_inproc": _source_digest(wc.Conductor._build_inproc),
        "Conductor._stage_dependency_sources":
            _source_digest(wc.Conductor._stage_dependency_sources),
        "codegen_bundle.derive_build_graph": _source_digest(cb.derive_build_graph),
    }


def execute_tuple() -> dict[str, str]:
    """The in-process execute: the server's two run tools, the conductor's execute body and
    the evidence promotion / quality-check authoring it composes the run record from."""
    server = _server()
    return {
        "build_runtime_server.tool_run_program": _source_digest(server.tool_run_program),
        "build_runtime_server.tool_run_quality_checks":
            _source_digest(server.tool_run_quality_checks),
        "build_runtime_server._run_command": _source_digest(server._run_command),
        "Conductor._execute_inproc": _source_digest(wc.Conductor._execute_inproc),
        "Conductor._promote_run_evidence": _source_digest(wc.Conductor._promote_run_evidence),
        "Conductor._author_quality_check": _source_digest(wc.Conductor._author_quality_check),
        "Conductor._author_snapshot_schema": _source_digest(wc.Conductor._author_snapshot_schema),
    }


def verdict_tuple() -> dict[str, str]:
    """The verdict: the predicate evaluator, the per-test verdict author (`verdict.json`, a
    Validate deliverable — a round-1 census found it in no tuple) and the derived-artifact
    author."""
    return {
        "tools/verdict_evaluator.py": _file_digest("tools/verdict_evaluator.py"),
        # Z6 (issue #255): the host-evaluated primary predicates are half of the verdict.
        "tools/primary_evidence.py": _file_digest("tools/primary_evidence.py"),
        "Conductor._author_execute_verdict": _source_digest(wc.Conductor._author_execute_verdict),
        "Conductor._author_derived_validate_artifacts":
            _source_digest(wc.Conductor._author_derived_validate_artifacts),
    }


# version -> digest, per transformation. KEEP historical entries; ADD the new one on a bump.
PINNED_COMPILE_DOCUMENTS: dict[str, str] = {
    "compile-docs-1": "5f2a2b23cefa67a97a5c3ca493df8fcb37f870de3c2c65076897fd8b544648e1",
    # Z6 PR-1 (issue #255): `phase_01_compile.md` now states what a snapshot variable IS (primary
    # state, bound in the checks module) and the identifier rule its name must satisfy — a
    # compile leaf reading it authors a different IR, so the key moves.
    "compile-docs-2": "1c4e903efb34e2ed8699159a0cac0a994573d4d8269cbab61223d93a82d6263a",
    # Z6 PR-2 (issue #255): `phase_01_compile.md` gains `io_contract.primary_predicates`, the
    # snapshot schema's `coordinates[]`, a condition's `quantity`, and the V3 fidelity rule over
    # them — a compile leaf reading it authors the host-evaluated corroborants, so the key moves.
    # Re-pinned in round 1 before shipping (the version is new on this branch): the grammar
    # block gains the cross-case `at('<case>').inputs` / `.<coordinate>` roots, the operand
    # pairing rule, the arity caps and the coordinate-name rule; V3 gains item (iv).
    "compile-docs-3": "415c072ce9451f76dcac2643c7d0f3ff3addbbcb91b5bc4ce840c41b484777a2",
}
PINNED_RENDER: dict[str, str] = {
    "render-1": "f70621b85c8d456126b20eed138facf20a56d2b2feb257c1a34d1245cd21cf43",
    # Z6 PR-1 (issue #255): the rendered runner reads bound storage (`sb_<var> => <var>`)
    # instead of calling getters, captures twice per case, and the build control file creates
    # `raw/state_snapshots/initial`.
    "render-2": "8409184903c2dc3dca987c853f134fa24ec4cbd38b7623a08457a49ff1b501d3",
}
PINNED_BUILD: dict[str, str] = {
    "build-1": "a4881aad1ed3437091d33f844e7e747f586e4c9b9775026f616618eb500b0343",
}
PINNED_EXECUTE: dict[str, str] = {
    "execute-1": "8bd25306f0ec274b4879be41b33430e0cddf9fe62e19a6d8be4e96dcc4e014be",
    # Z6 PR-1 (issue #255): execute promotes and requires the `initial/<case_id>.json` captures
    # of a host-rendered runner — a new deliverable of the phase.
    "execute-2": "28a3364616ba866133a0e34933a67c0154f0de61409d4cf5ffb95103933eb182",
}
PINNED_VERDICT: dict[str, str] = {
    "verdict-1": "06eb14a32fac4eb5353837261702121c19275f1b3cb61aa9d8dc44a7550a31cb",
    # Z6 PR-2 (issue #255): the verdict conjoins `io_contract.primary_predicates`, valued by
    # `tools/primary_evidence.py` from the captures under the run node directory, with the
    # diagnostics predicates; `basis.primary[]` / `basis.corroboration` are new per-test keys.
    # Re-pinned in round 1 before shipping (the version is new on this branch): every
    # interpreter / numpy exception on admitted operands becomes a per-predicate structural
    # record, the operand rule pairs shapes rather than ranks, `at()` reads a case's inputs
    # and coordinates, and a record's `target_cases` must equal its test's.
    "verdict-2": "2970ea6f3cd7e5f99163485f88ddb4dfac4c86904ccb655fecc77fdff213f042",
}


class _DriftCase:
    """One (constant, tuple, pins) triple; `TransformationDriftTests` drives each."""

    def __init__(self, name: str, version: str, tuple_fn, pinned: dict[str, str]) -> None:
        self.name, self.version, self.tuple_fn, self.pinned = name, version, tuple_fn, pinned


def _cases() -> list[_DriftCase]:
    return [
        _DriftCase("COMPILE_INLINED_DOCUMENTS_VERSION", d.COMPILE_INLINED_DOCUMENTS_VERSION,
                   compile_documents_tuple, PINNED_COMPILE_DOCUMENTS),
        _DriftCase("RENDER_VERSION", d.RENDER_VERSION, render_tuple, PINNED_RENDER),
        _DriftCase("BUILD_VERSION", d.BUILD_VERSION, build_tuple, PINNED_BUILD),
        _DriftCase("EXECUTE_VERSION", d.EXECUTE_VERSION, execute_tuple, PINNED_EXECUTE),
        _DriftCase("VERDICT_VERSION", d.VERDICT_VERSION, verdict_tuple, PINNED_VERDICT),
    ]


class TransformationDriftTests(unittest.TestCase):
    def test_each_current_version_is_pinned_and_matches(self) -> None:
        for case in _cases():
            with self.subTest(constant=case.name):
                members = case.tuple_fn()
                computed = _digest(members)
                self.assertIn(
                    case.version, case.pinned,
                    f"{case.name} = {case.version!r} has no pin. Add "
                    f"PINNED[...][{case.version!r}] = {computed!r} (its members: "
                    f"{sorted(members)}).")
                self.assertEqual(
                    case.pinned[case.version], computed,
                    f"\n\nThe {case.name} surface moved (recomputed digest: {computed}).\n"
                    "Resolve ONE of two ways:\n"
                    f"  (1) the edit changed what the transformation PRODUCES: bump "
                    f"{case.name} in tools/derivation.py, keep the existing pins, add the new "
                    "one — every derivation key of that phase moves, and every certified "
                    "output of it re-derives on the next run;\n"
                    "  (2) the edit is behaviour-preserving (a comment, a refactor, a message): "
                    f"re-pin PINNED[{case.version!r}] = {computed!r}.\n"
                    f"Members: {json.dumps(members, indent=2, sort_keys=True)}")

    def test_no_empty_bump_or_silent_revert(self) -> None:
        for case in _cases():
            with self.subTest(constant=case.name):
                digests = list(case.pinned.values())
                self.assertEqual(len(digests), len(set(digests)),
                                 f"{case.name}: two pinned versions share a digest")

    def test_the_compile_documents_are_the_ones_the_builder_inlines(self) -> None:
        """The census half for the compile pair: the four documents pinned here are exactly
        the static keys of the compile producer's context that the prompt drift test does NOT
        pin, and each is read from the path this table names (a renamed file would otherwise
        keep an old pin green over a document nobody inlines any more)."""
        declared = set(ort.PURE_CONTEXT_REQUIRED_KEYS[("compile", "generate")])
        self.assertTrue(set(COMPILE_INLINED_DOCUMENTS) <= declared)
        builder = inspect.getsource(wc.Conductor._build_pure_compile_context)
        for key, rel in COMPILE_INLINED_DOCUMENTS.items():
            with self.subTest(key=key):
                self.assertTrue((_REPO / rel).is_file(), rel)
                self.assertIn(f'"{key}"', builder)
                # The phase document is read through the runtime's table; the other three
                # by their literal path.
                self.assertIn(rel if key != "phase_contract_document"
                              else 'WORKFLOW_PHASE_DOC_BY_STEP["compile"]', builder)


# --- the inlined-document census ------------------------------------------------------

#: Every `pure_context` key the launch tables declare, classified. `input`: node input data,
#: hashed per node by the derivation inputs (`phase_derivation_inputs`) or an artifact of an
#: upstream phase whose OUTPUT HASH is; `contract`: a static document or slice pinned by
#: `test_pure_prompt_contract_drift._contract_tuple` under `PURE_PROMPT_CONTRACT_VERSION`;
#: `compile-docs`: pinned above under `COMPILE_INLINED_DOCUMENTS_VERSION`; `derived`: a
#: host-computed value that is a function of `input` members and the transformation version
#: (not hashed on its own); `same-phase`: an artifact an EARLIER substep of the same phase
#: produced, which the phase's reviewer or judge reads — not a key input of that phase (the
#: key was taken at phase start, before it existed), covered by the phase's own stamp and
#: output hash; `diagnosis`: the escalate diagnostician's, which certifies nothing.
INLINED_DOCUMENT_CLASS: dict[str, str] = {
    "controlled_spec_document": "input",
    "tests_document": "input",
    "deps_document": "input",
    "profile_spec_document": "input",
    "dependency_graph_document": "input",      # the derived closure signature is hashed
    "dependency_surface_document": "derived",  # from the closure members' certified IRs
    "ir_document": "input",                    # the compile output hash
    "bundle_document": "same-phase",           # the producer's bundle (the reviewer's subject)
    "harness_capabilities": "input",           # `harness.manifest`
    "target_profile": "input",                 # inside the IR
    "runner_document": "derived",              # RENDER_VERSION over the IR + harness outputs
    "toolchain_document": "input",
    "io_contract_document": "input",           # inside the IR
    "diagnostics_document": "same-phase",      # execute output, under the validate stamp
    "verdict_document": "same-phase",
    "perf_document": "same-phase",
    "trial_meta_document": "same-phase",
    "quality_check_document": "same-phase",
    "binary_meta_document": "input",           # the build output hash
    "source_meta_document": "input",           # the generate output hash
    "raw_evidence_excerpt_document": "derived",  # RAW_EXCERPT_POLICY_VERSION over raw/
    "phase_contract_document": "compile-docs",
    "ir_algorithm_example_document": "compile-docs",
    "ir_algorithm_2d_example_document": "compile-docs",
    "impl_defaults_schema_document": "compile-docs",
    "checks_module_contract_document": "contract",
    "severity_rubric_document": "contract",
    "runner_output_contract_document": "contract",
    "gate_guards_document": "contract",
    "lint_rules_document": "contract",
    "diagnosis_document": "diagnosis",
}


class InlinedDocumentCensusTests(unittest.TestCase):
    def test_every_declared_context_key_is_classified(self) -> None:
        declared: set[str] = set()
        for keys in ort.PURE_CONTEXT_REQUIRED_KEYS.values():
            declared |= set(keys)
        for keys in ort.PURE_CONTEXT_REQUIRED_KEYS_BY_SHAPE.values():
            declared |= set(keys)
        self.assertEqual(
            set(INLINED_DOCUMENT_CLASS), declared,
            "a pure_context key was added to (or dropped from) the launch tables without a "
            "row in INLINED_DOCUMENT_CLASS — decide whether it is node input data, a pinned "
            "contract document, a compile-inlined document, or derived, before a leaf sees it")
        self.assertEqual(set(INLINED_DOCUMENT_CLASS.values()),
                         {"input", "contract", "compile-docs", "derived", "same-phase",
                          "diagnosis"})

    def test_the_contract_rows_are_members_of_the_prompt_drift_tuple(self) -> None:
        """A key classified `contract` must be pinned by the prompt drift test's tuple — read
        off that test's `_contract_tuple` source, so a row here cannot claim a pin that
        module does not take."""
        from tools.tests import test_pure_prompt_contract_drift as drift
        tuple_source = inspect.getsource(drift._contract_tuple)
        expected_member = {
            "checks_module_contract_document": "checks_contract_abi_sections",
            "severity_rubric_document": "generate_verify_severity_rubric_section",
            "runner_output_contract_document": "runner_output_contract_document",
            "gate_guards_document": "checks_contract_gate_guards_section",
            "lint_rules_document": "lint_rules_document",
        }
        contract_keys = {k for k, v in INLINED_DOCUMENT_CLASS.items() if v == "contract"}
        self.assertEqual(contract_keys, set(expected_member))
        for key, member in expected_member.items():
            with self.subTest(key=key):
                self.assertIn(f'"{member}":', tuple_source)

    def test_the_compile_docs_rows_are_the_pinned_table(self) -> None:
        self.assertEqual({k for k, v in INLINED_DOCUMENT_CLASS.items() if v == "compile-docs"},
                         set(COMPILE_INLINED_DOCUMENTS))


if __name__ == "__main__":
    unittest.main()
