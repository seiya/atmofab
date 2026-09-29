#!/usr/bin/env python3
"""Tests for `tools/target_profile.py` — target profiles as host-owned data (issue #284, R4-a)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import yaml

from tools import target_profile as tp
from tools.orchestration_runtime import _load_spec_catalog

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


def _distributed_harness_x() -> Any:
    """The scratch catalog's harness, declared as providing `distributed_state@1`."""
    from tools import codegen_bundle
    return mock.patch.dict(codegen_bundle.HARNESS_CAPABILITY_MANIFESTS, {
        "infrastructure/harness_x@0.7.0": frozenset(
            {"sync_single_case@1", "state_registration@1", "distributed_state@1"})})


class CheckedInProfileTests(unittest.TestCase):
    def test_the_checked_in_profile_loads_gates_clean_and_names_the_catalog_harness(self) -> None:
        self.assertEqual(tp.list_target_ids(REPO_ROOT), ["cpp_gpu", "fortran_cpu", "fortran_cpu_mpi"])
        profile = tp.resolve_run_target(REPO_ROOT, "fortran_cpu")
        self.assertEqual(profile.target_id, "fortran_cpu")
        self.assertRegex(profile.sha256, r"^sha256:[0-9a-f]{64}$")
        harness = tp.harness_node_key_for_target(REPO_ROOT, profile)
        self.assertTrue(harness.startswith("infrastructure/harness_fortran_cpu@"), harness)
        # The harness is an infrastructure node for this target, and the profile passes the
        # launch gate FOR it too.
        self.assertEqual(tp.target_profile_violations(REPO_ROOT, profile, node_key=harness), [])
        self.assertEqual(profile.record(REPO_ROOT), {
            "target_id": "fortran_cpu", "sha256": profile.sha256, "harness_node_key": harness})

    def test_two_checked_in_profiles_make_the_target_a_required_choice(self) -> None:
        """Issue #289 (R4-b PR-5): with a second profile, a run that names none is refused
        rather than defaulted — `--target` is the operator's choice every time."""
        with self.assertRaises(tp.TargetProfileError) as cm:
            tp.resolve_run_target(REPO_ROOT, None)
        self.assertEqual(cm.exception.reason, "target_required")
        self.assertIn("cpp_gpu, fortran_cpu, fortran_cpu_mpi", cm.exception.detail)

    def test_the_checked_in_gpu_profile_builds_its_harness_and_stops_before_validate(self) -> None:
        """Issue #289 (R4-b PR-5): `cpp_gpu` passes the launch gate for its own harness through
        `build`. Since issue #293 the `gpu` class declares `execution`, so the registry half
        passes a run reaching Validate too, and what refuses it is the SITE half: with no
        `sites.yaml` the target runs at the local site, which executes `cpu` only. Its file
        states no `architecture`, so it is loaded with the `gpu` class's default, `all`."""
        from tools.execution_sites import (
            LOCAL_DEFAULT_EXECUTES,
            Site,
            SitesConfig,
            site_violations,
        )
        from tools.host_execution import LOCAL_SITE

        profile = tp.load_target_profile(REPO_ROOT, "cpp_gpu")
        self.assertEqual("all", profile.doc["hardware"]["architecture"])
        harness = tp.harness_node_key_for_target(REPO_ROOT, profile)
        self.assertTrue(harness.startswith("infrastructure/harness_cpp_gpu@"), harness)
        for phase in sorted(tp.NON_EXECUTING_PHASES):
            self.assertEqual(tp.target_profile_violations(
                REPO_ROOT, profile, node_key=harness, until_phase=phase), [], phase)
        self.assertEqual(tp.target_profile_violations(
            REPO_ROOT, profile, node_key=harness, until_phase="validate"), [])
        no_file = SitesConfig(sites={LOCAL_SITE: Site(LOCAL_SITE, LOCAL_DEFAULT_EXECUTES)})
        # Since issue #333 the binary is built at the site that runs it, so the site half is
        # asked of a run that reaches Build as well: with no `sites.yaml`, a run of this target
        # stops at Generate.
        for phase in ("build", "validate"):
            violations = site_violations(no_file, profile, until_phase=phase)
            self.assertEqual(len(violations), 1, (phase, violations))
            self.assertTrue(violations[0].startswith("hardware.class: gpu is not executed"),
                            violations)
        for phase in sorted(tp.NON_BUILDING_PHASES):
            self.assertEqual(site_violations(no_file, profile, until_phase=phase), [], phase)
        self.assertLess(tp.NON_BUILDING_PHASES, tp.NON_EXECUTING_PHASES)
        # A physics node of this language passes too since R4-b PR-6 (the language renders its
        # runner); until then it was refused at every phase for want of `runner_render`.
        for phase in sorted(tp.NON_EXECUTING_PHASES):
            self.assertEqual(tp.target_profile_violations(
                REPO_ROOT, profile, node_key="problem/advdiff1d_linear@0.4.0",
                until_phase=phase), [], phase)

    def test_the_checked_in_mpi_profile_launches_four_ranks_over_its_own_harness(self) -> None:
        """Issue #316 (R4-c PR-3): `fortran_cpu_mpi` names the `mpi` parallel backend with four
        ranks and the distributed-state harness, and passes the launch gate for that harness at
        every phase, Validate included — `mpi` declares the launcher its ranks need. The site half
        admits it at the local site with no `sites.yaml`: a `cpu` class, and a launcher target at
        `local`."""
        from tools.execution_sites import LOCAL_DEFAULT_EXECUTES, Site, SitesConfig, site_violations
        from tools.host_execution import LOCAL_SITE

        profile = tp.load_target_profile(REPO_ROOT, "fortran_cpu_mpi")
        self.assertEqual((profile.hardware_class, profile.parallel_backend, profile.ranks,
                          profile.threads_per_rank), ("cpu", "mpi", 4, 1))
        self.assertEqual(profile.toolchain["language"], "fortran")
        self.assertNotIn("compiler", profile.toolchain)  # a wrapper target pins no compiler
        harness = tp.harness_node_key_for_target(REPO_ROOT, profile)
        self.assertEqual(harness, "infrastructure/harness_fortran_cpu_mpi@0.1.0")
        for phase in sorted(tp.NON_EXECUTING_PHASES) + ["validate"]:
            self.assertEqual(tp.target_profile_violations(
                REPO_ROOT, profile, node_key=harness, until_phase=phase), [], phase)
        no_file = SitesConfig(sites={LOCAL_SITE: Site(LOCAL_SITE, LOCAL_DEFAULT_EXECUTES)})
        self.assertEqual(site_violations(no_file, profile, until_phase="validate"), [])

    def test_the_checked_in_profiles_are_byte_unchanged_by_the_optional_rank_count(self) -> None:
        """Issue #316: `execution.ranks` is optional with a default of 1 so that no existing
        profile's document — and so no sha256, and no Generate / Build / Validate key of its
        target — moves. The digests are the ones at origin/main fcced0b6, before the key
        existed; a profile edited on purpose updates its row here."""
        expected = {
            "cpp_gpu": "sha256:5d7efb15cbc7c40eb63a9c541bce3600e9c418e7f444813a8456ec13a7d4140b",
            "fortran_cpu": "sha256:24e5b2f02d275829459b2d7e93d575370821347366936c6311b1d65262c3bfd4",
        }
        got = {tid: tp.load_target_profile(REPO_ROOT, tid).sha256 for tid in expected}
        self.assertEqual(got, expected)
        for tid in expected:
            profile = tp.load_target_profile(REPO_ROOT, tid)
            self.assertNotIn("ranks", profile.doc["execution"])
            self.assertEqual(profile.ranks, 1)

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
            # Grammar only: WHICH classes exist is the registry's, asked by the launch gate
            # (`LaunchGateTests`), since the `hardware` axis (issue #289).
            "hardware__class": ("CPU", "hardware.class"),
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

    def test_the_rank_count_is_optional_and_a_positive_integer(self) -> None:
        """Issue #316: `execution.ranks` defaults to 1 and, when stated, is an integer >= 1."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _ScratchRepo(tmp)
            repo.write("t1")
            self.assertEqual(tp.load_target_profile(repo.root, "t1").ranks, 1)
            repo.write("t1", execution__ranks=4)
            self.assertEqual(tp.load_target_profile(repo.root, "t1").ranks, 4)
        for bad in (0, -2, True, 2.0, "4"):
            with self.subTest(ranks=bad):
                self.assertIn("execution.ranks: must be an integer >= 1",
                              self._refusal(execution__ranks=bad))

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
        # An open token, like every other axis value: the vocabulary is the registry's
        # (issue #289). A closed enum here would be a second list of hardware classes.
        self.assertEqual(schema["properties"]["hardware"]["properties"]["class"]["$ref"],
                         "#/definitions/token")
        self.assertNotIn("enum", schema["properties"]["hardware"]["properties"]["class"])
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
            "hardware__class": ("tpu", "hardware.class"),
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

    def test_more_ranks_than_one_need_a_launcher_at_every_phase(self) -> None:
        """Issue #316: without a launcher the binary runs as one process whatever the profile
        says. Asked of every run, a Build-only one included: the count is part of the target."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _ScratchRepo(tmp)
            for backend in ("openmp", "none"):
                profile = self._profile(repo, parallel__backend=backend, execution__ranks=2)
                for phase in (None, "build", "validate"):
                    with self.subTest(backend=backend, phase=phase):
                        violations = tp.target_profile_violations(repo.root, profile,
                                                                  until_phase=phase)
                        self.assertIn(f"execution.ranks: 2 ranks need a launcher, and parallel "
                                      f"backend {backend} declares none", violations)
            # One rank needs none; a launcher backend takes any count (over a harness that
            # drives several processes — `test_the_backend_and_the_harness_agree_on_processes`).
            self.assertEqual(tp.target_profile_violations(
                repo.root, self._profile(repo, execution__ranks=1)), [])
            with _distributed_harness_x():
                for ranks in (1, 4):
                    with self.subTest(backend="mpi", ranks=ranks):
                        self.assertEqual(tp.target_profile_violations(repo.root, self._profile(
                            repo, parallel__backend="mpi", execution__ranks=ranks)), [])

    def test_the_backend_and_the_harness_agree_on_processes(self) -> None:
        """Issue #316 (R4-c PR-4): a backend that declares a launcher needs a harness that
        provides `distributed_state`, and such a harness needs a backend with a launcher. Both
        directions are refused, at every phase; the agreeing pairs are accepted."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _ScratchRepo(tmp)
            for phase in (None, "Build", "Validate"):
                with self.subTest(phase=phase, direction="launcher without distributed"):
                    violations = tp.target_profile_violations(
                        repo.root, self._profile(repo, parallel__backend="mpi"),
                        until_phase=phase)
                    self.assertIn(
                        "parallel.backend: mpi starts the program as several processes under "
                        "its launcher, and harness infrastructure/harness_x@0.7.0 provides no "
                        "distributed_state capability to drive them; name a harness that does",
                        violations)
                with self.subTest(phase=phase, direction="distributed without launcher"), \
                        _distributed_harness_x():
                    for backend in ("openmp", "none"):
                        violations = tp.target_profile_violations(
                            repo.root, self._profile(repo, parallel__backend=backend),
                            until_phase=phase)
                        self.assertIn(
                            "harness: infrastructure/harness_x@0.7.0 drives the program as "
                            "several processes (distributed_state), and parallel backend "
                            f"{backend} declares no launcher to start them; use a parallel "
                            "backend that does", violations)
            with _distributed_harness_x():
                self.assertEqual(tp.target_profile_violations(
                    repo.root, self._profile(repo, parallel__backend="mpi")), [])
            self.assertEqual(tp.target_profile_violations(repo.root, self._profile(repo)), [])

    def test_a_compiler_pin_with_a_compiler_wrapper_is_refused(self) -> None:
        """Issue #316: a backend that compiles through its compiler wrapper runs it in the
        compiler's place, so a pinned `toolchain.compiler` names a compiler nothing runs. The
        same pin without a wrapper is accepted."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _ScratchRepo(tmp)
            violations = tp.target_profile_violations(repo.root, self._profile(
                repo, parallel__backend="mpi", toolchain__compiler="gfortran"))
            self.assertTrue(any(v.startswith("toolchain.compiler: gfortran is pinned, and "
                                             "parallel backend mpi compiles through its "
                                             "compiler wrapper") for v in violations),
                            violations)
            self.assertEqual(tp.target_profile_violations(repo.root, self._profile(
                repo, toolchain__compiler="gfortran")), [])

    def test_a_compiler_wrapper_of_another_languages_compiler_is_refused(self) -> None:
        """Issue #316: the wrapper runs one compiler (`WRAPPED_COMPILER`) and the build pins it
        whatever the language, so a target whose language compiles with another is refused —
        at every phase, since the build itself would be wrong. A language the wrapper does
        compile passes."""
        from tools.backends import registry
        wrapped = registry.capability_module("parallel", "mpi", "compiler_wrapper").WRAPPED_COMPILER
        cuda = {"language": "cuda_cpp", "standard": "c++17"}
        cuda_compiler = registry.capability_module(
            "language", "cuda_cpp", "bundle_facts").DEFAULT_COMPILER
        self.assertNotEqual(wrapped, cuda_compiler)
        with tempfile.TemporaryDirectory() as tmp:
            repo = _ScratchRepo(tmp)
            profile = self._profile(repo, parallel__backend="mpi",
                                    toolchain__language="cuda_cpp",
                                    toolchain__standard="c++17")
            self.assertEqual(profile.toolchain["language"], cuda["language"])
            for phase in (None, "build"):
                with self.subTest(phase=phase):
                    self.assertIn(
                        f"parallel.backend: mpi's compiler wrapper runs {wrapped}, and "
                        f"toolchain.language cuda_cpp compiles with {cuda_compiler}",
                        tp.target_profile_violations(repo.root, profile, until_phase=phase))
            fortran = tp.target_profile_violations(
                repo.root, self._profile(repo, parallel__backend="mpi"))
            self.assertFalse([v for v in fortran if "compiler wrapper runs" in v], fortran)

    def test_the_execution_half_is_asked_of_a_run_that_reaches_validate_only(self) -> None:
        """Issue #289: a class this repository cannot launch can be BUILT for, not run.

        Driven by withdrawal since issue #293, when every implemented class came to declare
        `execution` (`gpu` in its package): the `gpu` record without it is the witness —
        refused for a run ending at Validate, and for one whose end is unstated or not a phase
        this module exempts, which is the fail-closed direction — and accepted for a run that
        stops before it. The site half of the same question is `execution_sites.site_violations`
        (`tools/tests/test_execution_sites.py`)."""
        from tools.backends import registry

        record = registry.get("hardware", "gpu")
        self.assertIn("execution", record.backend_provides, "the witness withdraws a declaration")
        withdrawn = mock.patch.dict(registry._BACKENDS, {("hardware", "gpu"): record._replace(
            backend_provides=record.backend_provides - {"execution"})})
        with tempfile.TemporaryDirectory() as tmp, withdrawn:
            repo = _ScratchRepo(tmp)
            gpu = self._profile(repo, hardware__class="gpu", hardware__architecture="sm_90")
            for until in ("Compile", "Generate", "Build", "build", " BUILD "):
                with self.subTest(until_phase=until):
                    self.assertEqual(tp.target_profile_violations(
                        repo.root, gpu, until_phase=until), [])
            for until in ("Validate", "validate", None, "", "Buld"):
                with self.subTest(until_phase=until):
                    violations = tp.target_profile_violations(repo.root, gpu, until_phase=until)
                    self.assertEqual(len(violations), 1, violations)
                    self.assertTrue(violations[0].startswith("hardware.class:"), violations)
                    self.assertIn("'execution'", violations[0])
                    self.assertIn("reaches Validate", violations[0])
            # The negative control: this host's own class runs, at every end.
            cpu = self._profile(repo)
            for until in ("Build", "Validate", None):
                with self.subTest(cpu_until_phase=until):
                    self.assertEqual(tp.target_profile_violations(
                        repo.root, cpu, until_phase=until), [])
            # And `resolve_run_target` carries the phase through to the gate.
            (repo.root / tp.TARGETS_DIR / "t1.yaml").unlink()
            repo.write("t_gpu", hardware__class="gpu", hardware__architecture="sm_90")
            self.assertEqual(tp.resolve_run_target(repo.root, "t_gpu", until_phase="Build")
                             .hardware_class, "gpu")
            with self.assertRaises(tp.TargetProfileError) as ctx:
                tp.resolve_run_target(repo.root, "t_gpu", until_phase="Validate")
            self.assertEqual(ctx.exception.reason, "target_profile_invalid")
            self.assertIn("hardware.class", ctx.exception.detail)

    def test_the_execution_half_asks_the_parallel_backend_for_its_launch_env(self) -> None:
        # Driven by withdrawal: every implemented parallel value declares `execution_env`
        # today, so only a withdrawn one shows the gate asks it — and asks it only of a run that
        # launches the binary.
        from tools.backends import registry

        real = registry.provides

        def provides(axis: str, backend_id: str, capability: str) -> bool:
            return capability != "execution_env" and real(axis, backend_id, capability)

        with tempfile.TemporaryDirectory() as tmp:
            repo = _ScratchRepo(tmp)
            profile = self._profile(repo)
            with mock.patch.object(registry, "provides", provides):
                violations = tp.target_profile_violations(repo.root, profile,
                                                          until_phase="Validate")
                self.assertEqual(len(violations), 1, violations)
                self.assertTrue(violations[0].startswith("parallel.backend:"), violations)
                self.assertIn("'execution_env'", violations[0])
                self.assertEqual(tp.target_profile_violations(
                    repo.root, profile, until_phase="Build"), [])

    def test_the_architecture_is_held_to_the_classs_perf_facts_where_it_states_them(
            self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _ScratchRepo(tmp)
            for arch in ("sm_90", "sm_90a", "sm_75", "sm_100", "sm_120a"):
                with self.subTest(gpu_architecture=arch):
                    self.assertEqual(tp.target_profile_violations(
                        repo.root, self._profile(repo, hardware__class="gpu",
                                                 hardware__architecture=arch),
                        until_phase="Build"), [])
            # A trailing surplus and a non-numeric version too: `match` in place of `fullmatch`,
            # or a widened character class, admits them (round 2).
            for arch in ("x86_64", "sm90", "gfx90a", "sm_", "sm_90ab", "sm_abc", "sm_90_x",
                         "xsm_90"):
                with self.subTest(gpu_architecture=arch):
                    violations = tp.target_profile_violations(
                        repo.root, self._profile(repo, hardware__class="gpu",
                                                 hardware__architecture=arch),
                        until_phase="Build")
                    self.assertEqual(len(violations), 1, violations)
                    self.assertTrue(violations[0].startswith("hardware.architecture:"))
                    self.assertIn(repr(arch), violations[0])
            # A class stating no `perf_facts` keeps its architecture a recorded token.
            self.assertEqual(tp.target_profile_violations(
                repo.root, self._profile(repo, hardware__architecture="sm_90"),
                until_phase="Validate"), [])

    def test_an_absent_architecture_is_the_class_s_default(self) -> None:
        """Issue #289 (R4-b PR-5), the operator's decision: `hardware.architecture` is optional.
        Absent, the loader accepts the profile and gives it the class's `DEFAULT_ARCHITECTURE`
        when the class's `perf_facts` declare one — `all` for `gpu` (R4-b PR-6, the operator's
        decision after a default-architecture binary failed every kernel launch at the site) —
        BEFORE the sha256 is taken, so the default is in the target's identity. A class with no
        `perf_facts` (`cpu`) is left without one. A present one is still held to the grammar
        (the test above), and is never replaced."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _ScratchRepo(tmp)
            for hardware_class, phase, expected in (("gpu", "Build", "all"),
                                                    ("cpu", "Validate", None)):
                with self.subTest(hardware_class=hardware_class):
                    profile = self._profile(repo, hardware__class=hardware_class,
                                            hardware__architecture=_DELETE)
                    self.assertEqual(expected, profile.doc["hardware"].get("architecture"))
                    self.assertEqual(tp.target_profile_violations(
                        repo.root, profile, until_phase=phase), [])
            filled = self._profile(repo, hardware__class="gpu", hardware__architecture=_DELETE)
            stated = self._profile(repo, hardware__class="gpu", hardware__architecture="all")
            self.assertEqual(stated.sha256, filled.sha256)
            pinned = self._profile(repo, hardware__class="gpu", hardware__architecture="sm_89")
            self.assertEqual("sm_89", pinned.doc["hardware"]["architecture"])

    def test_a_capability_the_node_kind_needs_is_asked_by_kind(self) -> None:
        """A non-infrastructure node needs the runner render; an infrastructure node does not.
        Every node needs the control file (both halves; R4-b PR-4 round 3). Driven by
        withdrawing a capability."""
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
            for axis, capability in (("language", "runner_render"),):
                with self.subTest(axis=axis, capability=capability), \
                        mock.patch.object(registry, "provides", without(axis, capability)):
                    self.assertTrue(tp.target_profile_violations(repo.root, profile))
                    self.assertEqual(tp.target_profile_violations(
                        repo.root, profile, node_key="infrastructure/harness_x@0.7.0"), [])
            for axis, capability in (("language", "control_file"),
                                     ("build_system", "control_file")):
                with self.subTest(every_node_axis=axis, capability=capability), \
                        mock.patch.object(registry, "provides", without(axis, capability)):
                    self.assertTrue(tp.target_profile_violations(repo.root, profile))
                    self.assertTrue(tp.target_profile_violations(
                        repo.root, profile, node_key="infrastructure/harness_x@0.7.0"))

            # The build is asked of EVERY kind, the harness included, and so is the build
            # system's half of the control file (R4-b PR-4 round 3: the harness bundle's shape
            # needs the host-authored control file).
            for capability in ("build_execute", "control_file"):
                with self.subTest(build_system=capability), \
                        mock.patch.object(registry, "provides", without("build_system", capability)):
                    self.assertTrue(tp.target_profile_violations(
                        repo.root, profile, node_key="infrastructure/harness_x@0.7.0"))
            # ... and so is every language capability `Generate` reads for any kind (issue
            # #289, R4-b PR-2; the source reader and the signature module since R4-b PR-3): a
            # language missing one is refused at launch, for a harness as for a physics node,
            # naming the capability.
            self.assertEqual(
                tp.LANGUAGE_CAPABILITIES_EVERY_NODE,
                ("bundle_facts", "syntax_promotions", "prompt_fragments", "checks_abi",
                 "source_reading", "signatures", "control_file"))
            for capability in tp.LANGUAGE_CAPABILITIES_EVERY_NODE:
                with self.subTest(every_node=capability), \
                        mock.patch.object(registry, "provides", without("language", capability)):
                    for node_key in (None, "infrastructure/harness_x@0.7.0"):
                        found = tp.target_profile_violations(repo.root, profile, node_key=node_key)
                        self.assertTrue(any(capability in v for v in found), (node_key, found))

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


class HarnessEntriesTests(unittest.TestCase):
    """`target_harness_entries`: the dependency entry a node gains from its target (issue #284,
    R4-a PR-3) — the harness, for every node whose kind is not `infrastructure`."""

    def test_a_non_infrastructure_node_gains_the_targets_harness(self) -> None:
        profile = tp.load_target_profile(REPO_ROOT, "fortran_cpu")
        harness = _checkout_doc()["harness"]
        expected = [("infrastructure", harness["infrastructure_id"],
                     harness["version_constraint"])]
        for kind in ("component", "problem", " component ", None, "Infrastructure"):
            with self.subTest(kind=kind):
                self.assertEqual(tp.target_harness_entries(profile, kind), expected)

    def test_an_infrastructure_node_and_a_run_with_no_target_gain_nothing(self) -> None:
        profile = tp.load_target_profile(REPO_ROOT, "fortran_cpu")
        self.assertEqual(tp.target_harness_entries(profile, "infrastructure"), [])
        self.assertEqual(tp.target_harness_entries(profile, " infrastructure "), [])
        self.assertEqual(tp.target_harness_entries(None, "component"), [])

    def test_the_entry_is_the_profiles_harness_not_a_constant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _ScratchRepo(tmp)
            repo.write("t1", harness={"infrastructure_id": "harness_x",
                                      "version_constraint": ">=0.3.0 <0.5.0"})
            profile = tp.load_target_profile(repo.root, "t1")
            self.assertEqual(tp.target_harness_entries(profile, "component"),
                             [("infrastructure", "harness_x", ">=0.3.0 <0.5.0")])


if __name__ == "__main__":
    unittest.main()
