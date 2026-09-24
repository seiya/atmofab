#!/usr/bin/env python3
"""Tests for `tools/target_profile.py` — target profiles as host-owned data (issue #284, R4-a)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from tools import target_profile as tp
from tools.orchestration_runtime import (
    _load_spec_catalog,
    admissible_toolchains_document,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "spec" / "schema" / "targets" / "target_profile.schema.json"


def _checkout_doc() -> dict:
    return yaml.safe_load((REPO_ROOT / tp.TARGETS_DIR / "fortran_cpu.yaml").read_text(
        encoding="utf-8"))


class _ScratchRepo:
    """A scratch repo root with a catalog carrying one harness, and profiles written on demand."""

    def __init__(self, tmp: str) -> None:
        self.root = Path(tmp)
        reg = self.root / "spec" / "registry"
        reg.mkdir(parents=True)
        (reg / "spec_catalog.yaml").write_text(
            "catalog_version: 0.2.0\nupdated_at: 2026-09-24\nspecs:\n"
            "  - spec_kind: infrastructure\n    spec_id: harness_x\n"
            "    spec_version: \"0.4.0\"\n    status: controlled_draft\n"
            "    deps_path: spec/infrastructure/harness_x/deps.yaml\n"
            "  - spec_kind: infrastructure\n    spec_id: harness_x\n"
            "    spec_version: \"0.7.0\"\n    status: controlled_draft\n"
            "    deps_path: spec/infrastructure/harness_x/deps.yaml\n"
            "  - spec_kind: component\n    spec_id: not_a_harness\n"
            "    spec_version: \"0.5.0\"\n    status: controlled_draft\n"
            "    deps_path: spec/component/not_a_harness/deps.yaml\n",
            encoding="utf-8")
        _load_spec_catalog.cache_clear()

    def write(self, stem: str, **overrides: object) -> Path:
        doc = _checkout_doc()
        doc["target_id"] = stem
        doc["harness"] = {"infrastructure_id": "harness_x", "version_constraint": ">=0.3.0 <1.0.0"}
        for dotted, value in overrides.items():
            obj = doc
            *parents, leaf = dotted.split("__")
            for part in parents:
                obj = obj[part]
            if value is _DELETE:
                del obj[leaf]
            else:
                obj[leaf] = value
        path = self.root / tp.TARGETS_DIR / f"{stem}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
        return path


_DELETE = object()


class CheckedInProfileTests(unittest.TestCase):
    def test_the_checked_in_profile_loads_gates_clean_and_names_the_catalog_harness(self) -> None:
        self.assertEqual(tp.list_target_ids(REPO_ROOT), ["fortran_cpu"])
        profile = tp.resolve_run_target(REPO_ROOT, None)
        self.assertEqual(profile.target_id, "fortran_cpu")
        self.assertRegex(profile.sha256, r"^sha256:[0-9a-f]{64}$")
        harness = tp.harness_node_key_for_target(REPO_ROOT, profile)
        self.assertTrue(harness.startswith("infrastructure/harness_fortran_cpu@"), harness)
        # The harness is an infrastructure node for this target, and the profile passes the
        # launch gate FOR it too.
        self.assertEqual(tp.target_profile_violations(REPO_ROOT, profile, node_key=harness), [])
        self.assertEqual(profile.record(REPO_ROOT), {
            "target_id": "fortran_cpu", "sha256": profile.sha256, "harness_node_key": harness})

    def test_the_hash_is_over_content_not_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _ScratchRepo(tmp)
            path = repo.write("t1")
            first = tp.load_target_profile(repo.root, "t1").sha256
            path.write_text("# a comment\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
            self.assertEqual(tp.load_target_profile(repo.root, "t1").sha256, first)
            repo.write("t1", hardware__architecture="aarch64")
            self.assertNotEqual(tp.load_target_profile(repo.root, "t1").sha256, first)


class SelectionTests(unittest.TestCase):
    def test_no_profile_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(tp.TargetProfileError) as cm:
                tp.select_target_id(Path(tmp), None)
            self.assertEqual(cm.exception.reason, "no_target_profile")

    def test_one_profile_is_the_default_and_several_require_a_choice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _ScratchRepo(tmp)
            repo.write("alpha")
            self.assertEqual(tp.select_target_id(repo.root, None), "alpha")
            repo.write("beta")
            with self.assertRaises(tp.TargetProfileError) as cm:
                tp.select_target_id(repo.root, None)
            self.assertEqual(cm.exception.reason, "target_required")
            self.assertIn("alpha, beta", cm.exception.detail)
            self.assertEqual(tp.select_target_id(repo.root, "beta"), "beta")

    def test_an_unknown_requested_target_refuses_and_lists_the_declared(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _ScratchRepo(tmp)
            repo.write("alpha")
            with self.assertRaises(tp.TargetProfileError) as cm:
                tp.select_target_id(repo.root, "gamma")
            self.assertEqual(cm.exception.reason, "target_unknown")
            self.assertIn("alpha", cm.exception.detail)

    def test_a_yaml_whose_stem_is_not_a_target_id_is_refused_not_skipped(self) -> None:
        """Skipping it would make the other profile the only one — and so the default."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _ScratchRepo(tmp)
            repo.write("alpha")
            (repo.root / tp.TARGETS_DIR / "Fortran-GPU.yaml").write_text("{}\n", encoding="utf-8")
            with self.assertRaises(tp.TargetProfileError) as cm:
                tp.select_target_id(repo.root, None)
            self.assertEqual(cm.exception.reason, "target_profile_invalid")
            # Nor is the other YAML suffix skipped.
            (repo.root / tp.TARGETS_DIR / "Fortran-GPU.yaml").rename(
                repo.root / tp.TARGETS_DIR / "beta.yml")
            with self.assertRaises(tp.TargetProfileError) as cm:
                tp.select_target_id(repo.root, None)
            self.assertIn("rename it", cm.exception.detail)
            # A file that is not YAML is not a profile at all.
            (repo.root / tp.TARGETS_DIR / "beta.yml").rename(
                repo.root / tp.TARGETS_DIR / "README.md")
            self.assertEqual(tp.select_target_id(repo.root, None), "alpha")


class LoaderTests(unittest.TestCase):
    def _refusal(self, **overrides: object) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _ScratchRepo(tmp)
            repo.write("t1", **overrides)
            with self.assertRaises(tp.TargetProfileError) as cm:
                tp.load_target_profile(repo.root, "t1")
            self.assertEqual(cm.exception.reason, "target_profile_invalid")
            return cm.exception.detail

    def test_the_scratch_baseline_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _ScratchRepo(tmp)
            repo.write("t1")
            self.assertEqual(tp.load_target_profile(repo.root, "t1").target_id, "t1")

    def test_the_target_id_must_be_the_file_stem(self) -> None:
        self.assertIn("is not the file stem", self._refusal(target_id="t2"))
        # A malformed id is named as malformed, not only as a stem disagreement.
        self.assertIn("does not match", self._refusal(target_id="T1"))

    def test_every_required_key_is_required(self) -> None:
        for obj_key, (required, _optional) in tp.PROFILE_SHAPE.items():
            for key in sorted(required):
                with self.subTest(obj=obj_key or "top", key=key):
                    dotted = f"{obj_key}__{key}" if obj_key else key
                    detail = self._refusal(**{dotted: _DELETE})
                    self.assertIn(f"missing required key {key!r}", detail)

    def test_every_object_is_closed(self) -> None:
        for obj_key in tp.PROFILE_SHAPE:
            with self.subTest(obj=obj_key or "top"):
                dotted = f"{obj_key}__surplus" if obj_key else "surplus"
                self.assertIn("unknown key 'surplus'", self._refusal(**{dotted: "x"}))

    def test_the_optional_toolchain_pins_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _ScratchRepo(tmp)
            repo.write("t1", toolchain__compiler="gfortran", toolchain__linker="ld")
            self.assertEqual(tp.load_target_profile(repo.root, "t1").toolchain["linker"], "ld")

    def test_field_grammar(self) -> None:
        cases = {
            "target_profile_version": (2, "target_profile_version"),
            "hardware__class": ("tpu", "hardware.class"),
            "hardware__architecture": ("X86_64", "hardware.architecture"),
            "toolchain__language": ("", "toolchain.language"),
            "toolchain__build_system": (["make"], "toolchain.build_system"),
            "parallel__backend": ("Open MP", "parallel.backend"),
            "execution__threads_per_rank": (0, "execution.threads_per_rank"),
            "harness__version_constraint": ("  ", "harness.version_constraint"),
            "harness__infrastructure_id": (7, "harness.infrastructure_id"),
            "hardware": ("cpu", "hardware: must be a mapping"),
        }
        for dotted, (value, needle) in cases.items():
            with self.subTest(field=dotted):
                self.assertIn(needle, self._refusal(**{dotted: value}))

    def test_more_than_one_thread_per_rank_is_refused_for_now(self) -> None:
        """Validate.execute's run is the single-thread reference of the quality check
        (phase_04_validate.md §4-2): a profile running more threads would leave that
        comparison two parallel runs, a quality certification that tested nothing."""
        for threads in (2, 4):
            with self.subTest(threads=threads):
                reason = self._refusal(execution__threads_per_rank=threads)
                self.assertIn("threads_per_rank", reason)
                self.assertIn("single-thread reference", reason)

    def test_a_bool_is_not_an_integer(self) -> None:
        """`True == 1` in Python, so an `isinstance(int)` check alone accepts it."""
        self.assertIn("target_profile_version", self._refusal(target_profile_version=True))
        self.assertIn("threads_per_rank", self._refusal(execution__threads_per_rank=True))
        # draft-07's `integer` admits 2.0; the loader does not (the schema's description says so).
        self.assertIn("threads_per_rank", self._refusal(execution__threads_per_rank=2.0))

    def test_an_unreadable_or_non_mapping_document_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _ScratchRepo(tmp)
            path = repo.write("t1")
            for text in ("- a list\n", "key: [unclosed\n"):
                with self.subTest(text=text):
                    path.write_text(text, encoding="utf-8")
                    with self.assertRaises(tp.TargetProfileError) as cm:
                        tp.load_target_profile(repo.root, "t1")
                    self.assertEqual(cm.exception.reason, "target_profile_invalid")
            with self.assertRaises(tp.TargetProfileError):
                tp.load_target_profile(repo.root, "absent")
            with self.assertRaises(tp.TargetProfileError):
                tp.load_target_profile(repo.root, "../escape")


class SchemaAgreementTests(unittest.TestCase):
    """The schema is the declarative copy of `PROFILE_SHAPE` (spec/schema/SCHEMA.md)."""

    def test_each_object_has_the_same_required_and_allowed_keys(self) -> None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        for obj_key, (required, optional) in tp.PROFILE_SHAPE.items():
            with self.subTest(obj=obj_key or "top"):
                node = schema if obj_key == "" else schema["properties"][obj_key]
                self.assertIs(node["additionalProperties"], False)
                self.assertEqual(set(node["required"]), set(required))
                self.assertEqual(set(node["properties"]), set(required | optional))
        self.assertEqual(
            schema["properties"]["execution"]["properties"]["threads_per_rank"],
            {"type": "integer", "minimum": 1, "maximum": 1})
        self.assertEqual(schema["properties"]["target_profile_version"]["enum"],
                         [tp.TARGET_PROFILE_VERSION])
        self.assertEqual(schema["properties"]["hardware"]["properties"]["class"]["enum"],
                         list(tp.HARDWARE_CLASSES))
        self.assertEqual(schema["properties"]["target_id"]["pattern"],
                         f"^{tp.TARGET_ID_PATTERN.pattern}(?![\\s\\S])")
        # A blank constraint is refused by both copies (the loader strips).
        self.assertEqual(
            schema["properties"]["harness"]["properties"]["version_constraint"]["pattern"], r"\S")
        self.assertEqual(schema["definitions"]["token"]["pattern"],
                         f"^{tp.TOKEN_PATTERN.pattern}(?![\\s\\S])")


class LaunchGateTests(unittest.TestCase):
    def _profile(self, repo: _ScratchRepo, **overrides: object) -> tp.TargetProfile:
        repo.write("t1", **overrides)
        return tp.load_target_profile(repo.root, "t1")

    def test_axis_values_the_host_does_not_implement_are_refused(self) -> None:
        cases = {
            "toolchain__language": ("cobol", "toolchain:"),
            "toolchain__build_system": ("bazel", "toolchain:"),
            "parallel__backend": ("cuda_streams", "parallel.backend"),
            "toolchain__compiler": ("icx", "toolchain.compiler"),
        }
        with tempfile.TemporaryDirectory() as tmp:
            repo = _ScratchRepo(tmp)
            self.assertEqual(tp.target_profile_violations(repo.root, self._profile(repo)), [])
            for dotted, (value, needle) in cases.items():
                with self.subTest(field=dotted):
                    violations = tp.target_profile_violations(
                        repo.root, self._profile(repo, **{dotted: value}))
                    self.assertTrue(violations)
                    self.assertTrue(violations[0].startswith(needle), violations)
                    # The registry's own membership reason, not a capability clause about a
                    # value it has never heard of (`parallel` is an open vocabulary, whose
                    # reason is worded differently).
                    if dotted != "parallel__backend":
                        self.assertIn("is not a declared", violations[0])

    def test_a_capability_the_node_kind_needs_is_asked_by_kind(self) -> None:
        """A non-infrastructure node needs the control file and the runner render; an
        infrastructure node only the build. Driven by withdrawing a capability."""
        from tools.backends import registry

        real = registry.provides

        def without(withdrawn_axis: str, withdrawn: str):
            def provides(axis: str, backend_id: str, capability: str) -> bool:
                return (not (axis == withdrawn_axis and capability == withdrawn)
                        and real(axis, backend_id, capability))
            return provides

        with tempfile.TemporaryDirectory() as tmp:
            repo = _ScratchRepo(tmp)
            profile = self._profile(repo)
            # One row per (axis, capability) a non-infrastructure node needs: the registry
            # today gives its one language and one build system every capability, so only a
            # withdrawal can tell the four requirements apart (round 1: dropping the
            # language's `control_file` survived).
            for axis, capability in (("language", "runner_render"), ("language", "control_file"),
                                     ("build_system", "control_file")):
                with self.subTest(axis=axis, capability=capability), \
                        mock.patch.object(registry, "provides", without(axis, capability)):
                    self.assertTrue(tp.target_profile_violations(repo.root, profile))
                    self.assertEqual(tp.target_profile_violations(
                        repo.root, profile, node_key="infrastructure/harness_x@0.7.0"), [])

            # The build is asked of EVERY kind, the harness included.
            with mock.patch.object(registry, "provides", without("build_system", "build_execute")):
                self.assertTrue(tp.target_profile_violations(
                    repo.root, profile, node_key="infrastructure/harness_x@0.7.0"))

    def test_the_harness_resolves_to_the_highest_matching_infrastructure_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _ScratchRepo(tmp)
            self.assertEqual(tp.harness_node_key_for_target(repo.root, self._profile(repo)),
                             "infrastructure/harness_x@0.7.0")
            narrowed = self._profile(repo, harness__version_constraint=">=0.3.0 <0.5.0")
            self.assertEqual(tp.harness_node_key_for_target(repo.root, narrowed),
                             "infrastructure/harness_x@0.4.0")

    def test_a_harness_the_catalog_does_not_carry_as_infrastructure_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _ScratchRepo(tmp)
            for harness in ({"infrastructure_id": "absent", "version_constraint": ">=0.1.0"},
                            {"infrastructure_id": "not_a_harness", "version_constraint": ">=0.1.0"},
                            {"infrastructure_id": "harness_x", "version_constraint": ">=2.0.0"}):
                with self.subTest(harness=harness):
                    profile = self._profile(repo, harness=harness)
                    with self.assertRaises(tp.TargetProfileError):
                        tp.harness_node_key_for_target(repo.root, profile)
                    violations = tp.target_profile_violations(repo.root, profile)
                    self.assertEqual(len(violations), 1, violations)
                    with self.assertRaises(tp.TargetProfileError) as cm:
                        tp.resolve_run_target(repo.root, "t1")
                    self.assertEqual(cm.exception.detail.count("target t1"), 1,
                                     cm.exception.detail)
            (repo.root / "spec" / "registry" / "spec_catalog.yaml").write_text(
                "", encoding="utf-8")
            with self.assertRaises(tp.TargetProfileError):
                tp.harness_node_key_for_target(repo.root, self._profile(repo))
            # The corruption branch names the target once too (round 2).
            with self.assertRaises(tp.TargetProfileError) as cm:
                tp.resolve_run_target(repo.root, "t1")
            self.assertEqual(cm.exception.detail.count("target t1"), 1, cm.exception.detail)

    def test_an_infrastructure_node_must_be_its_targets_harness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _ScratchRepo(tmp)
            repo.write("t1")
            self.assertEqual(
                tp.resolve_run_target(repo.root, "t1",
                                      node_key="infrastructure/harness_x@0.7.0").target_id, "t1")
            for other in ("infrastructure/harness_x@0.4.0", "infrastructure/harness_y@0.7.0"):
                with self.subTest(node_key=other):
                    with self.assertRaises(tp.TargetProfileError) as cm:
                        tp.resolve_run_target(repo.root, "t1", node_key=other)
                    self.assertEqual(cm.exception.reason, "target_harness_mismatch")
            # A non-infrastructure node is not a harness, whatever its name.
            self.assertEqual(tp.resolve_run_target(
                repo.root, "t1", node_key="component/harness_y@0.7.0").target_id, "t1")

    def test_the_admissible_toolchain_document_asks_the_same_question(self) -> None:
        """ONE capability question: the compile producer's admissible set is exactly the set of
        (language, build_system) pairs a profile could name and pass the launch gate with."""
        from tools.backends import registry

        for node_key in ("component/x@0.1.0", "infrastructure/x@0.1.0"):
            with self.subTest(node_key=node_key):
                admissible = {
                    (p["language"], p["build_system"])
                    for p in json.loads(admissible_toolchains_document(node_key))[
                        "admissible_toolchains"]}
                servable = {
                    (lang, b)
                    for lang in registry.backend_ids("language")
                    for b in registry.backend_ids("build_system")
                    if not tp.toolchain_servable_reasons(
                        lang, b, infrastructure=node_key.startswith("infrastructure/"))}
                self.assertEqual(admissible, servable)
                self.assertTrue(admissible)


class BridgeTests(unittest.TestCase):
    """The R4-a PR-1 bridge (PR-3 deletes it with `impl_defaults`)."""

    def _ir(self) -> dict:
        return {"impl_defaults": {
            "target": {"class": "cpu", "backend": "openmp", "architecture": "x86_64"},
            "toolchain": {"language": "fortran", "standard": "f2008", "build_system": "make"}}}

    def test_the_bridge_compares_exactly_these_fields(self) -> None:
        """A literal, so dropping a field from `BRIDGE_FIELDS` is red: the row below iterates
        the constant and could not notice one missing. `target.backend` is deliberately absent
        (see the constant's comment)."""
        self.assertEqual(dict(tp.BRIDGE_FIELDS), {
            "target.class": "hardware.class",
            "toolchain.language": "toolchain.language",
            "toolchain.standard": "toolchain.standard",
            "toolchain.build_system": "toolchain.build_system",
        })

    def test_a_matching_ir_passes_and_each_bridge_field_is_compared(self) -> None:
        profile = tp.load_target_profile(REPO_ROOT, "fortran_cpu")
        self.assertEqual(tp.ir_profile_mismatches(self._ir(), profile), [])
        for ir_path, _profile_path in tp.BRIDGE_FIELDS:
            section, key = ir_path.split(".")
            with self.subTest(field=ir_path):
                changed = self._ir()
                changed["impl_defaults"][section][key] = "other"
                found = tp.ir_profile_mismatches(changed, profile)
                self.assertEqual(len(found), 1, found)
                self.assertEqual(
                    found[0], f"{ir_path}='other' expected {tp._dotted(profile.doc, _profile_path)!r}")
                absent = self._ir()
                del absent["impl_defaults"][section][key]
                self.assertEqual(len(tp.ir_profile_mismatches(absent, profile)), 1)

    def test_case_is_normalized_and_the_parallel_backend_is_not_compared(self) -> None:
        """A node that parallelizes nothing (the certified harness IR declares a serial backend)
        still runs on the target: that is a lowering choice, not a target attribute."""
        profile = tp.load_target_profile(REPO_ROOT, "fortran_cpu")
        ir = self._ir()
        ir["impl_defaults"]["toolchain"]["language"] = "Fortran"
        ir["impl_defaults"]["target"]["backend"] = "serial"
        self.assertEqual(tp.ir_profile_mismatches(ir, profile), [])

    def test_an_ir_without_impl_defaults_mismatches_every_field(self) -> None:
        profile = tp.load_target_profile(REPO_ROOT, "fortran_cpu")
        for ir in ({}, None, {"impl_defaults": "x"}):
            with self.subTest(ir=ir):
                self.assertEqual(len(tp.ir_profile_mismatches(ir, profile)),
                                 len(tp.BRIDGE_FIELDS))


if __name__ == "__main__":
    unittest.main()
