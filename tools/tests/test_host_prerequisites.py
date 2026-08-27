"""The launch-time host probe: what it asks for, and that it cannot drift from what runs.

The value of this check is entirely in the second half. A probe that held its own list of tool
names would go stale the first time a gate's argv changed, and it would go stale SILENTLY — the
run would start, and fail where it failed before this check existed. So the tests below do not
assert the names; they assert that the names come from the tables that run them.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "mcp_servers") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "mcp_servers"))

import build_runtime_server as server  # noqa: E402
from tools import host_prerequisites as hp  # noqa: E402
from tools import workflow_conductor as conductor  # noqa: E402
from tools.backends import registry as backend_registry  # noqa: E402


class LaunchSelectionTests(unittest.TestCase):
    def test_the_selection_is_what_the_conductors_own_readers_answer(self) -> None:
        """Not a constant restated here. `resolve_launch_axis_selection` asks the readers the
        conductor uses on a real node, with an IR that pins nothing — which is the state a cold
        start is actually in, since `Compile` is the phase that authors that file."""
        selection = hp.resolve_launch_axis_selection()
        self.assertEqual(selection["language"], conductor._ir_language({}))
        self.assertEqual(selection["build_system"], conductor._ir_build_system({}))
        self.assertEqual(selection["compiler"], conductor.DEFAULT_COMPILER)

    def test_the_linter_comes_from_the_language_to_preset_mapping_the_gate_uses(self) -> None:
        """The same private name `_gate_lint_check` reads. A second copy would send the probe
        after a linter the gate never runs, which is the whole failure mode this check exists to
        remove."""
        from tools.validate_pipeline_semantics import _LINT_PRESET_FOR_LANGUAGE

        selection = hp.resolve_launch_axis_selection()
        self.assertEqual(
            selection["linter"], _LINT_PRESET_FOR_LANGUAGE[selection["language"]]
        )

    def test_every_selected_axis_value_is_a_registered_implemented_member(self) -> None:
        """The probe asks the registry the question it means (`unimplemented_reason` — is this
        value runnable), so a value nothing implements refuses rather than being probed."""
        selection = hp.resolve_launch_axis_selection()
        for axis in ("language", "build_system", "compiler"):
            self.assertIsNone(
                backend_registry.unimplemented_reason(axis, selection[axis]),
                f"{axis}={selection[axis]!r}",
            )
        for sub in server.lint_preset_sub_presets(selection["linter"]):
            self.assertIsNone(backend_registry.unimplemented_reason("linter", sub), sub)

    def test_an_unregistered_axis_value_refuses_with_the_registrys_own_clause(self) -> None:
        """A build-tooling bug, not a host one — and the remedy is the registry's, not a second
        refusal written in the probe."""
        bogus = "no_such_backend_id"
        reason = backend_registry.unimplemented_reason("build_system", bogus)
        self.assertIsNotNone(reason)
        with self.assertRaises(RuntimeError) as caught:
            hp.required_host_executables(
                {**hp.resolve_launch_axis_selection(), "build_system": bogus}
            )
        self.assertIn(reason, str(caught.exception))


class NoDriftFromWhatActuallyRunsTests(unittest.TestCase):
    """The point of the whole module: the probe reads argv[0] out of the tables that run it."""

    def test_the_linter_executables_are_argv0_of_the_commands_run_linter_runs(self) -> None:
        for preset in list(server._LINT_PRESET_COMMANDS) + list(server._LINT_PRESET_COMPOSITES):
            expected = tuple(
                server._LINT_PRESET_COMMANDS[sub][0]
                for sub in server.lint_preset_sub_presets(preset)
            )
            # order preserved, repeats collapsed
            deduped: list[str] = []
            for exe in expected:
                if exe not in deduped:
                    deduped.append(exe)
            self.assertEqual(server.lint_preset_executables(preset), tuple(deduped), preset)

    def test_a_composite_preset_asks_for_every_linter_it_composes(self) -> None:
        """`mixed` runs two linters in order and appends two command-log records; a probe that
        checked only the first would let the run start and die on the second."""
        for preset, subs in server._LINT_PRESET_COMPOSITES.items():
            self.assertGreater(len(subs), 1, preset)
            self.assertEqual(len(server.lint_preset_executables(preset)), len(set(subs)), preset)

    def test_the_build_executable_is_argv0_of_the_command_compile_project_runs(self) -> None:
        for build_system in ("make",):
            self.assertEqual(
                server.build_system_executable(build_system),
                server._build_command(build_system, None, 1, [])[0],
            )

    def test_the_compiler_executable_is_the_registered_adapters_own_exe(self) -> None:
        """The same `exe` `tool_run_syntax_check` probes before it runs a stage."""
        for compiler, adapter in server._SYNTAX_COMPILER_ADAPTERS.items():
            self.assertEqual(
                server.syntax_compiler_executable(compiler), str(adapter["exe"])
            )

    def test_the_build_compiler_default_equals_the_mandatory_syntax_stage(self) -> None:
        """These are two constants in two modules — the server is standalone-runnable and does
        not import `tools/`, and the conductor reaches the server only lazily. They must be the
        same value: the syntax gate CERTIFIES that stage and the build then RUNS this one, so a
        divergence would certify one compiler and build with another. It is also what lets the
        probe cover the build compiler by probing the mandatory syntax stage."""
        self.assertEqual(conductor.DEFAULT_COMPILER, server.MANDATORY_SYNTAX_COMPILER)
        self.assertIn(conductor.DEFAULT_COMPILER, server._SYNTAX_COMPILER_ADAPTERS)

    def test_the_default_lint_preset_is_a_row_of_the_table_that_runs_it(self) -> None:
        self.assertIn(server.DEFAULT_LINT_PRESET, server._LINT_PRESET_COMMANDS)


class ProbeShapeTests(unittest.TestCase):
    def test_the_probe_covers_all_three_axes_and_repeats_nothing(self) -> None:
        items = hp.required_host_executables()
        self.assertEqual(
            {item.axis for item in items}, {"linter", "build_system", "compiler"}
        )
        executables = [item.executable for item in items]
        self.assertEqual(len(executables), len(set(executables)))

    def test_a_composite_preset_attributes_each_program_to_its_sub_preset(self) -> None:
        """Not to the composite: the sub-preset is the registered `linter` member, so it is what
        the registry can be asked about and what an operator installs."""
        selection = {**hp.resolve_launch_axis_selection(), "linter": "mixed"}
        linter_items = [
            item for item in hp.required_host_executables(selection) if item.axis == "linter"
        ]
        self.assertEqual(
            [item.backend_id for item in linter_items],
            list(server.lint_preset_sub_presets("mixed")),
        )

    def test_missing_is_the_subset_of_required_that_is_not_on_path(self) -> None:
        import shutil

        required = hp.required_host_executables()
        expected = tuple(
            item for item in required if shutil.which(item.executable) is None
        )
        self.assertEqual(hp.missing_host_executables(), expected)

    def test_this_development_host_satisfies_the_probe(self) -> None:
        """A sanity row of the same kind as `test_check_required_cli_tools_returns_empty_when_all_present`:
        if this fails, the machine running the suite could not run a workflow."""
        self.assertEqual(hp.missing_host_executables(), ())


class ToolVersionArmTests(unittest.TestCase):
    """The second launch question: the tool is here, but is it a build we measured against?

    Issue #111. The executables half above answers presence; a tool that is present and of an
    unmeasured version decides a certification with a rule set nobody reviewed, which is the
    failure that produced the incident (#110).
    """

    def test_every_version_gated_capability_carries_the_protocol(self) -> None:
        """MANDATORY, not duck-typed-and-hope — and scoped to what it actually covers.

        The arm reads two names off a capability module. If they were optional, renaming one in a
        backend package would turn the whole launch check off SILENTLY and the next release of
        that vendor's tool would land in a billed run again.

        The first version of this row said "every package-implemented capability" and then
        exempted everything but one of them with a `continue`, so the sentence was false in the
        direction that matters: `runner_render` carries neither name, and the arm would have
        raised `AttributeError` at launch the first time a language-axis executable reached it.
        The obligation now has an explicit membership (`_VERSION_GATED_CAPABILITIES`) and this
        asserts it over exactly that set, with no exemption inside the loop.
        """
        gated = set(hp._VERSION_GATED_CAPABILITIES)
        self.assertNotEqual(gated, set(), "nothing is version-gated, so this row observes nothing")
        seen = 0
        for (axis, backend_id), record in backend_registry._BACKENDS.items():
            for capability in sorted(record.backend_provides & gated):
                with self.subTest(axis=axis, backend_id=backend_id, capability=capability):
                    module = backend_registry.capability_module(axis, backend_id, capability)
                    self.assertTrue(callable(getattr(module, "version_argv", None)))
                    self.assertTrue(
                        callable(getattr(module, "unsupported_version_reason", None)))
                    seen += 1
        self.assertGreater(seen, 0, "no record implements a version-gated capability in its own "
                                    "package, so this row observes nothing")

    def test_a_capability_outside_the_gated_set_is_not_asked_for_a_version(self) -> None:
        """The other direction, and the one that was a latent launch traceback.

        `runner_render` is implemented in a backend package and answers neither name. Driving the
        seam with a language-axis item must yield nothing rather than reaching for `version_argv`.
        """
        module = backend_registry.capability_module("language", "fortran", "runner_render")
        self.assertFalse(hasattr(module, "version_argv"))
        item = hp.HostExecutable("language", "fortran", "gfortran")
        self.assertEqual(list(hp._version_gated_capability_modules(item)), [])

    def test_the_seam_reaches_the_real_backend_module_for_a_gated_capability(self) -> None:
        """The wiring itself, with NO mock — the one thing the arm's other rows cannot see.

        Measured: replacing the body of `_version_gated_capability_modules` with a bare `return`
        left every other test green, because the two behavioural rows substitute this function
        and the host row asserts `== ()`, which an always-empty generator satisfies. That is the
        whole launch gate dead with a green suite.
        """
        from tools.backends.linter.fortitude import lint

        item = hp.HostExecutable("linter", "fortitude", "fortitude")
        self.assertEqual(list(hp._version_gated_capability_modules(item)), [lint])

    def test_an_out_of_range_build_is_refused_through_the_real_seam(self) -> None:
        """The arm end to end with only the PROBE substituted.

        Everything else is production: the real record, the real capability module, the real
        clause. Only the reading of the installed version is replaced, because this host has a
        supported build and the refusal must be observable anyway.
        """
        with mock.patch.object(hp, "_tool_version_text", lambda argv: "fortitude 0.1.0"):
            found = hp.unsupported_host_tool_versions()
        self.assertEqual([item.executable for item in found], ["fortitude"])
        self.assertIn("below the supported floor", found[0].reason)

    def test_the_arm_reports_the_backends_own_clause(self) -> None:
        """Driven with a synthetic capability module, not with the host's environment.

        The refusal path has to be exercised on a machine where every real tool is fine, and
        mocking `shutil.which` would answer a different question (that is the presence half).
        What is substituted here is the DECLARATION the arm reads — the same seam a future
        backend uses — so the arm itself, its message and its record shape all really run.
        """
        class _Refusing:
            @staticmethod
            def version_argv():
                return (sys.executable, "-c", "print('probe 9.9.9')")

            @staticmethod
            def unsupported_version_reason(version_text):
                return f"synthetic refusal for {version_text!r}"

        item = hp.HostExecutable("linter", "fortitude", "fortitude")
        with mock.patch.object(hp, "required_host_executables", lambda selection=None: (item,)), \
             mock.patch.object(hp, "_version_gated_capability_modules",
                               lambda _item: iter((_Refusing,))):
            found = hp.unsupported_host_tool_versions()
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0].axis, found[0].backend_id, found[0].executable),
                         ("linter", "fortitude", "fortitude"))
        self.assertEqual(found[0].version, "probe 9.9.9")
        self.assertIn("synthetic refusal", found[0].reason)

    def test_an_unreadable_probe_is_still_handed_to_the_backend(self) -> None:
        """`None` reaches the clause rather than being swallowed as "fine".

        The polarity is the backend's to decide and it decides refuse; what this pins is that the
        arm ASKS. A version read that raises is the state a missing binary or a hung tool leaves,
        and treating it as success is the fail-open this whole check exists against.
        """
        seen: list[str | None] = []

        class _Unreadable:
            @staticmethod
            def version_argv():
                return ("metdsl-no-such-program-nowhere",)

            @staticmethod
            def unsupported_version_reason(version_text):
                seen.append(version_text)
                return "unreadable"

        item = hp.HostExecutable("linter", "x", "x")
        with mock.patch.object(hp, "required_host_executables", lambda selection=None: (item,)), \
             mock.patch.object(hp, "_version_gated_capability_modules",
                               lambda _item: iter((_Unreadable,))):
            found = hp.unsupported_host_tool_versions()
        self.assertEqual(seen, [None])
        self.assertEqual(found[0].version, None)

    def test_this_development_host_satisfies_the_version_arm(self) -> None:
        """The companion of `test_this_development_host_satisfies_the_probe`: if this fails, a
        workflow started on this machine would be refused at launch."""
        self.assertEqual(hp.unsupported_host_tool_versions(), ())


class NeutralCoreTests(unittest.TestCase):
    def test_the_probe_module_spells_no_technology_token(self) -> None:
        """The acceptance criterion of issue #109, asserted directly rather than left to the
        ratchet's sample: no tool name is added to a `neutral core` file. The ratchet
        (`tools/tests/test_backend_boundary.py`) records the same fact as a count of 0 for this
        file; this row states WHY it must stay 0."""
        from tools.tests.test_backend_boundary import _COMPILED

        source = (REPO_ROOT / "tools" / "host_prerequisites.py").read_text()
        hits = {
            name: rx.findall(source) for name, rx in _COMPILED.items() if rx.search(source)
        }
        self.assertEqual(hits, {})


if __name__ == "__main__":
    unittest.main()
