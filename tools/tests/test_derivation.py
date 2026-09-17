"""`tools/derivation.py` — the pure half of the A1 identity model (issue #250).

What is PINNED here (set identity, by construction): the canonical serialisation, the key's
payload shape (`key_version`, `step`, `transformation`, `inputs` and nothing else), the
transformation tuple of each phase, output-hash order independence and its refusals, the
first-differing-input walk, and selection policy v1. What is SAMPLED: the specific inputs
that move a key (one byte, one closure entry) — the per-phase input SETS are the runtime's
(`test_orchestration_runtime.DerivationInputsTests`), not this module's.
"""
from __future__ import annotations

import hashlib
import json
import unittest
from unittest import mock

from tools import derivation as d
from tools.pure_leaf import PURE_PROMPT_CONTRACT_VERSION
from tools.raw_evidence_excerpt import RAW_EXCERPT_POLICY_VERSION


class CanonicalHashingTests(unittest.TestCase):
    def test_canonical_json_is_sorted_compact_and_utf8(self) -> None:
        # Two spellings of one object serialise identically; non-ASCII is kept as bytes of
        # the character, not escaped (so a hash over a document body is over its text).
        a = d.canonical_json_bytes({"b": [1, {"y": 2, "x": "é"}], "a": None})
        b = d.canonical_json_bytes({"a": None, "b": [1, {"x": "é", "y": 2}]})
        self.assertEqual(a, b)
        self.assertEqual(a, b'{"a":null,"b":[1,{"x":"\xc3\xa9","y":2}]}')

    def test_canonical_json_refuses_a_non_json_value(self) -> None:
        with self.assertRaises(TypeError):
            d.canonical_json_bytes({"p": {1, 2}})

    def test_sha256_hex_has_the_repository_string_form(self) -> None:
        self.assertEqual(d.sha256_hex(b"abc"),
                         "sha256:" + hashlib.sha256(b"abc").hexdigest())


class OutputHashTests(unittest.TestCase):
    _H = "sha256:" + "a" * 64
    _B = "sha256:" + "b" * 64
    _STAGE = "workspace/pipelines/n/p_20260101_001/source/src_20260101_001"

    def test_order_independent_and_path_sensitive(self) -> None:
        one = d.output_hash({f"{self._STAGE}/src/a.f90": self._H,
                             f"{self._STAGE}/src/b.f90": self._B}, stage_dir=self._STAGE)
        two = d.output_hash({f"{self._STAGE}/src/b.f90": self._B,
                             f"{self._STAGE}/src/a.f90": self._H}, stage_dir=self._STAGE)
        self.assertEqual(one, two)
        # The same bytes under swapped names is a different output.
        swapped = d.output_hash({f"{self._STAGE}/src/a.f90": self._B,
                                 f"{self._STAGE}/src/b.f90": self._H}, stage_dir=self._STAGE)
        self.assertNotEqual(one, swapped)
        # And it is the canonical hash of the RELATIVE map, so a reader can recompute it.
        self.assertEqual(one, d.sha256_hex(d.canonical_json_bytes(
            {"src/a.f90": self._H, "src/b.f90": self._B})))

    def test_a_fresh_stage_id_with_identical_bytes_is_the_same_output(self) -> None:
        """The property every downstream key rests on: a re-derivation whose deliverables
        come out byte-identical under a new stage directory binds identically."""
        other = "workspace/pipelines/n/p_20260101_001/source/src_20260102_003"
        self.assertEqual(
            d.output_hash({f"{self._STAGE}/src/a.f90": self._H}, stage_dir=self._STAGE),
            d.output_hash({f"{other}/src/a.f90": self._H}, stage_dir=other))
        # A leading or trailing slash on either side does not change the answer.
        self.assertEqual(
            d.output_hash({f"/{self._STAGE}/src/a.f90": self._H}, stage_dir=self._STAGE + "/"),
            d.output_hash({f"{self._STAGE}/src/a.f90": self._H}, stage_dir=self._STAGE))

    def test_refuses_empty_malformed_and_foreign_paths(self) -> None:
        for bad in ({}, None, [], {"": self._H}, {f"{self._STAGE}/p": "abc"},
                    {f"{self._STAGE}/p": "sha256:"}, {f"{self._STAGE}/p": 5}, {3: self._H},
                    # outside the stage directory, the stage directory itself, a sibling
                    {"workspace/pipelines/n/other/src/a.f90": self._H},
                    {self._STAGE: self._H},
                    {self._STAGE + "_2/src/a.f90": self._H}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                d.output_hash(bad, stage_dir=self._STAGE)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            d.output_hash({f"{self._STAGE}/p": self._H}, stage_dir="")


class DerivationKeyTests(unittest.TestCase):
    def test_transformation_versions_per_phase(self) -> None:
        """The version tuple of each phase, member by member — a member dropped from one
        tuple would silently stop that phase's key from moving with it."""
        self.assertEqual(d.transformation_versions(), {
            "compile": ("pure", PURE_PROMPT_CONTRACT_VERSION,
                        d.COMPILE_INLINED_DOCUMENTS_VERSION),
            "generate": ("pure", PURE_PROMPT_CONTRACT_VERSION, d.RENDER_VERSION),
            "build": (d.BUILD_VERSION,),
            "validate": (d.EXECUTE_VERSION, d.VERDICT_VERSION, "pure",
                         PURE_PROMPT_CONTRACT_VERSION,
                         f"raw-excerpt-{RAW_EXCERPT_POLICY_VERSION}"),
        })
        self.assertEqual(set(d.transformation_versions()), set(d.DERIVATION_STEPS))

    def test_key_is_the_canonical_hash_of_exactly_four_fields(self) -> None:
        inputs = {"spec": {"tests": "sha256:" + "1" * 64}, "closure": []}
        expected = d.sha256_hex(d.canonical_json_bytes({
            "key_version": d.DERIVATION_KEY_VERSION,
            "step": "compile",
            "transformation": list(d.transformation_versions()["compile"]),
            "inputs": inputs,
        }))
        self.assertEqual(d.derivation_key("compile", inputs), expected)
        self.assertEqual(d.derivation_key(" Compile ", inputs), expected)

    def test_key_is_deterministic_and_moves_with_one_byte(self) -> None:
        base = {"spec": {"controlled_spec": "sha256:" + "1" * 64, "tests": "sha256:" + "2" * 64},
                "closure": [{"node_key": "component/a@0.1.0", "ir": "sha256:" + "3" * 64}]}
        self.assertEqual(d.derivation_key("generate", base), d.derivation_key("generate", dict(base)))
        moved = json.loads(json.dumps(base))
        moved["spec"]["controlled_spec"] = "sha256:" + "1" * 63 + "0"
        self.assertNotEqual(d.derivation_key("generate", base), d.derivation_key("generate", moved))
        closure_moved = json.loads(json.dumps(base))
        closure_moved["closure"][0]["ir"] = "sha256:" + "4" * 64
        self.assertNotEqual(d.derivation_key("generate", base),
                            d.derivation_key("generate", closure_moved))

    def test_key_moves_with_each_version_axis_and_the_step(self) -> None:
        inputs = {"x": "sha256:" + "1" * 64}
        base = d.derivation_key("build", inputs)
        self.assertNotEqual(base, d.derivation_key("validate", inputs))
        with mock.patch.object(d, "BUILD_VERSION", "build-2"):
            self.assertNotEqual(base, d.derivation_key("build", inputs))
        with mock.patch.object(d, "DERIVATION_KEY_VERSION", d.DERIVATION_KEY_VERSION + 1):
            self.assertNotEqual(base, d.derivation_key("build", inputs))
        with mock.patch.object(d, "COMPILE_INLINED_DOCUMENTS_VERSION", "compile-docs-2"):
            # The compile-docs version is COMPILE's axis only.
            self.assertEqual(base, d.derivation_key("build", inputs))
            self.assertNotEqual(d.derivation_key("compile", inputs),
                                d.sha256_hex(d.canonical_json_bytes({
                                    "key_version": d.DERIVATION_KEY_VERSION, "step": "compile",
                                    "transformation": ["pure", PURE_PROMPT_CONTRACT_VERSION,
                                                       "compile-docs-1"],
                                    "inputs": inputs})))

    def test_refuses_an_unknown_step_and_non_mapping_inputs(self) -> None:
        with self.assertRaises(ValueError):
            d.derivation_key("assemble", {})
        with self.assertRaises(TypeError):
            d.derivation_key("compile", ["not", "a", "mapping"])  # type: ignore[arg-type]


class FirstDifferingInputTests(unittest.TestCase):
    def test_walks_nested_mappings_and_lists_in_sorted_order(self) -> None:
        recorded = {"spec": {"tests": "t1", "controlled_spec": "c1"},
                    "closure": [{"node_key": "a", "ir": "1"}, {"node_key": "b", "ir": "2"}]}
        self.assertIsNone(d.first_differing_input(recorded, json.loads(json.dumps(recorded))))
        moved = json.loads(json.dumps(recorded)); moved["spec"]["tests"] = "t2"
        self.assertEqual(d.first_differing_input(recorded, moved), "spec.tests")
        moved = json.loads(json.dumps(recorded)); moved["closure"][1]["ir"] = "9"
        self.assertEqual(d.first_differing_input(recorded, moved), "closure[1].ir")
        # A length difference is reported at the list; a one-sided key at the key.
        shorter = json.loads(json.dumps(recorded)); shorter["closure"].pop()
        self.assertEqual(d.first_differing_input(recorded, shorter), "closure")
        extra = json.loads(json.dumps(recorded)); extra["profiles"] = []
        self.assertEqual(d.first_differing_input(recorded, extra), "profiles")
        # Sorted-key order decides which of two differences is FIRST.
        both = json.loads(json.dumps(recorded))
        both["spec"]["tests"] = "t2"; both["closure"][0]["ir"] = "8"
        self.assertEqual(d.first_differing_input(recorded, both), "closure[0].ir")

    def test_scalar_roots(self) -> None:
        self.assertEqual(d.first_differing_input({"a": 1}, {"a": 2}), "a")
        self.assertEqual(d.first_differing_input({"a": [1]}, {"a": [1, 2]}), "a")


class SelectEligibleTests(unittest.TestCase):
    def _cand(self, attempt: str, order: tuple, ref: str = "r") -> d.Candidate:
        return d.Candidate(attempt_id=attempt, order=order, output_hash="sha256:" + "0" * 64,
                           ref=ref)

    def test_policy_v1_takes_the_latest_order_then_the_attempt_id(self) -> None:
        older = self._cand("z-attempt", ("20260101", 1))
        newer = self._cand("a-attempt", ("20260102", 1))
        self.assertIs(d.select_eligible([older, newer]), newer)
        self.assertIs(d.select_eligible([newer, older]), newer)
        tie_a = self._cand("a", ("20260102", 1)); tie_b = self._cand("b", ("20260102", 1))
        self.assertIs(d.select_eligible([tie_a, tie_b]), tie_b)
        self.assertIs(d.select_eligible([tie_b, tie_a]), tie_b)

    def test_none_when_nothing_is_eligible_and_unknown_policy_refused(self) -> None:
        self.assertIsNone(d.select_eligible([]))
        with self.assertRaises(ValueError):
            d.select_eligible([self._cand("a", (1,))], policy_version=d.SELECTION_POLICY_VERSION + 1)


if __name__ == "__main__":
    unittest.main()
