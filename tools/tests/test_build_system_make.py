"""The `make` build system's `build_execute` declarations (issue #424 PR-2).

What the neutral core reads off `tools/backends/build_system/make/execute.py` and
`control_file.object_name`, pinned by value: these moved unchanged out of the conductor, the
server, the validator and `codegen_bundle`, and a value that changes here changes what a Build
runs, what a quality check re-runs and which record the gate accepts. The readers' wiring is
pinned where the readers are (`test_workflow_conductor.py`, `test_build_runtime_server.py`,
`test_validate_pipeline_semantics.py`, `test_codegen_bundle.py`).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.backends import registry
from tools.backends.build_system.make import control_file, execute


class BuildExecuteDeclarationTests(unittest.TestCase):
    def test_the_registry_dispatches_build_execute_to_this_module(self) -> None:
        self.assertIs(registry.capability_module("build_system", "make", "build_execute"),
                      execute)

    def test_the_quality_check_presets_are_the_control_files(self) -> None:
        # The argv table and the control-file target table name ONE preset set: a preset the
        # server runs whose target the gate does not require (or the reverse) would pass a
        # record the control file cannot have produced.
        self.assertEqual(set(execute.QUALITY_CHECK_COMMANDS),
                         set(control_file.QUALITY_CHECK_PRESETS))
        for preset, (executable, target) in execute.QUALITY_CHECK_COMMANDS.items():
            with self.subTest(preset=preset):
                self.assertEqual(executable, "make")
                self.assertEqual(target, control_file.QUALITY_CHECK_PRESETS[preset])
        self.assertIn(execute.QUALITY_CHECK_PRESET, execute.QUALITY_CHECK_COMMANDS)
        self.assertEqual(execute.QUALITY_CHECK_PRESET, "make_test")

    def test_build_argv_and_overrides(self) -> None:
        self.assertEqual(execute.build_argv(None, 4, []), ["make", "-j4"])
        self.assertEqual(execute.build_argv("all", 2, ["A=1", "B=2"]),
                         ["make", "-j2", "all", "A=1", "B=2"])
        self.assertEqual(execute.build_overrides("/o", "/b", "x_runner"),
                         ("OBJDIR=/o", "BINDIR=/b", "BIN=x_runner"))

    def test_quality_check_env(self) -> None:
        self.assertEqual(
            execute.quality_check_env("/o", "/b", "/r", "x_runner", "/s.yaml", ["c1", "c2"]),
            {"OBJDIR": "/o", "BINDIR": "/b", "RUNDIR": "/r", "BIN": "x_runner",
             "SPEC": "/s.yaml", "CASES": "c1 c2"})

    def test_quality_check_preset_reads_the_recorded_argv(self) -> None:
        cases = {
            ("make", "test"): "make_test",
            ("/usr/bin/make", "-j4", "test"): "make_test",
            ("MAKE", "TEST"): "make_test",
            ("make", "check"): "make_check",
            ("make", "", "check"): "make_check",
            ("make",): None,
            ("make", "all"): None,
            ("make", "tests"): None,
            ("ctest",): None,
            ("pytest", "-q"): None,
            ("bash", "-c", "make test"): None,
            ("test", "make"): None,
            (): None,
        }
        for argv, expected in cases.items():
            with self.subTest(argv=argv):
                self.assertEqual(execute.quality_check_preset(list(argv)), expected)

    def test_build_artifacts_and_binary_missing(self) -> None:
        self.assertEqual(execute.BUILD_ARTIFACT_SUFFIXES, (".o", ".a", ".so"))
        self.assertEqual(execute.BINARY_MISSING,
                         ("make_error", "the Makefile build rule must produce $(BINDIR)/$(BIN)"))

    def test_object_name(self) -> None:
        self.assertEqual(control_file.object_name("adv1d_model.f90"), "adv1d_model.o")
        self.assertEqual(control_file.object_name("core/util.f90"), "core__util.o")
        self.assertEqual(control_file.object_name("a/b/c.cu"), "a__b__c.o")
        self.assertEqual(control_file.object_name("x_model.cuh"), "x_model.o")


if __name__ == "__main__":
    unittest.main()
