"""The launch-time host probe: what it asks for, and that it cannot drift from what runs.

The value of this check is entirely in the second half. A probe that held its own list of tool
names would go stale the first time a gate's argv changed, and it would go stale SILENTLY — the
run would start, and fail where it failed before this check existed. So the tests below do not
assert the names; they assert that the names come from the tables that run them.
"""

from __future__ import annotations

import re
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
                    for name in ("version_argv", "unsupported_version_reason",
                                 "self_check_argv", "self_check_reason"):
                        self.assertTrue(callable(getattr(module, name, None)),
                                        f"{axis}/{backend_id} does not answer {name}")
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

    def test_the_probe_reads_the_FIRST_line_and_a_banner_does_not_hide_the_version(self) -> None:
        """The first-line rule is a real decision with a real failure mode, and it had none.

        Every fixture and the real tool print exactly one line, so last-line / whole-text /
        stderr-first readers were all indistinguishable — corpus-dependent, measured on the
        round-3 census. Driven here with a probe that prints a banner first: the reader must
        return the BANNER, which the backend then refuses. Fail-closed is the right polarity —
        an unidentified build must not decide a certification — and this pins that the polarity
        is reached rather than accidentally skipped by a reader that scans for a version.
        """
        probe = (sys.executable, "-c", "print('warning: config ignored'); print('probe 0.9.1')")
        self.assertEqual(hp._tool_version_text(probe), "warning: config ignored")

    def test_a_probe_that_times_out_is_a_refusal_rather_than_a_traceback(self) -> None:
        """`subprocess.TimeoutExpired` is NOT an `OSError`, so it needs its own except arm.

        The census measured the arm as dead — removing it from the tuple kept the suite green —
        which means a hung linter would have tracebacked out of the launch path instead of being
        refused. Driven by raising it, rather than by hanging a real process for the timeout.
        """
        with mock.patch.object(hp.subprocess, "run",
                               side_effect=hp.subprocess.TimeoutExpired("x", 30)):
            self.assertIsNone(hp._tool_version_text(("x", "--version")))

    def test_an_unstartable_probe_is_a_refusal_rather_than_a_traceback(self) -> None:
        self.assertIsNone(hp._tool_version_text(("metdsl-no-such-program-nowhere",)))

    def test_a_build_that_cannot_impose_the_declared_set_is_refused_at_launch(self) -> None:
        """The second launch question, driven through the real arm.

        A version inside the range does not imply the declared set is imposable on that build:
        a code withdrawn in a patch release nobody measured makes the invocation fail without
        judging anything. Left to the gate it arrives as a lint finding attributed to the leaf's
        source — the failure a blank-slate reviewer reproduced. Only the self-check's verdict is
        substituted here; the record, the module and the arm are production.
        """
        with mock.patch.object(hp, "_self_check_reason", lambda module: "synthetic refusal"):
            found = hp.unsupported_host_tool_versions()
        self.assertEqual([item.reason for item in found], ["synthetic refusal"])

    def test_the_self_check_runs_the_backends_own_argv_over_an_empty_directory(self) -> None:
        """No fixture, no parsing: the directory is empty, so a usable build has nothing to find.

        Pinned because the emptiness is what makes the exit status unambiguous — a self-check
        pointed at a directory with sources in it would confuse `there are findings` with
        `the invocation was refused`, which is the distinction this whole arm exists to make.
        """
        seen: dict[str, object] = {}

        class _Probe:
            @staticmethod
            def self_check_argv(empty_dir):
                seen["dir"] = empty_dir
                seen["listing"] = sorted(Path(empty_dir).iterdir())
                return (sys.executable, "-c", "raise SystemExit(0)")

            @staticmethod
            def self_check_reason(returncode, stdout, stderr):
                seen["rc"] = returncode
                return None

        self.assertIsNone(hp._self_check_reason(_Probe))
        self.assertEqual(seen["listing"], [])
        self.assertEqual(seen["rc"], 0)

    def test_a_self_check_that_cannot_be_run_is_a_refusal(self) -> None:
        class _Unrunnable:
            @staticmethod
            def self_check_argv(empty_dir):
                return ("metdsl-no-such-program-nowhere",)

            @staticmethod
            def self_check_reason(returncode, stdout, stderr):  # pragma: no cover - not reached
                return None

        self.assertIsNotNone(hp._self_check_reason(_Unrunnable))

    def test_this_development_host_satisfies_the_version_arm(self) -> None:
        """The companion of `test_this_development_host_satisfies_the_probe`: if this fails, a
        workflow started on this machine would be refused at launch."""
        self.assertEqual(hp.unsupported_host_tool_versions(), ())


class RunbookVersionRangeTests(unittest.TestCase):
    """Every version range the operator's document states is one a backend package declares.

    Set identity, not "the range appears somewhere": a document that states a range nothing
    declares sends an operator to install a build the launch probe will refuse, and a presence
    check is satisfied while a second occurrence drifts (measured on the fortitude range —
    editing the first of its two occurrences left an `assertIn` green).

    This lives here rather than in a backend's own test file because it is a property of ALL of
    them at once: asserted from one backend, a sibling's range change would fail the wrong file.
    Each backend still pins its OWN occurrences where they matter (fortitude's install line).
    """

    def _declared_ranges(self) -> dict[str, str]:
        from tools.backends import registry as backend_registry

        found = {}
        for backend_id in backend_registry.backend_ids("linter"):
            if "lint" not in backend_registry.get("linter", backend_id).backend_provides:
                continue
            module = backend_registry.capability_module("linter", backend_id, "lint")
            found[backend_id] = module.SUPPORTED_VERSION_SPEC
        return found

    #: The §0-1 table this check owns, found by its own header rather than by position.
    _RANGE_TABLE_HEADER = "| linter | supported versions | declaration and measurement |"

    #: Which column of that table holds a range. The third is headed "declaration and
    #: measurement" and is free PROSE: a legitimate note there — say, that a range's interior was
    #: not installable and is unmeasured — carries a `>=x.y,<a.b` that is not a declaration, and
    #: reading the whole row refused it. Reading the whole DOCUMENT refused more still: any
    #: `python3` or `cmake` prerequisite anywhere in the operator's runbook turned the suite red
    #: with a message blaming the document for a linter fact. Both were over-refusals, one scope
    #: level apart, and this constant is the second fix.
    _RANGE_COLUMN = 2

    _RANGE_RE = re.compile(r">=\d+\.\d+(?:\.\d+)?,<\d+\.\d+(?:\.\d+)?")

    def _ranges_in_table(self, document: str) -> set[str]:
        """The ranges the §0-1 linter table DECLARES, out of `document`.

        Takes the document as an argument so the over-refusal probe below can drive THIS
        function rather than re-implementing it. The first version of that probe re-implemented
        the extraction inline, with different termination semantics, and its docstring claimed
        otherwise — measured, replacing this whole body with `return document` left the suite
        green, so the fix it witnesses was pinned by nothing.
        """
        self.assertIn(
            self._RANGE_TABLE_HEADER, document,
            "docs/RUNBOOK.md §0-1 no longer carries the linter version-range table; this check "
            "cannot find what it is supposed to compare")
        found: set[str] = set()
        for line in document.split(self._RANGE_TABLE_HEADER, 1)[1].splitlines()[1:]:
            if not line.startswith("|"):
                break
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) > self._RANGE_COLUMN - 1:
                found.update(self._RANGE_RE.findall(cells[self._RANGE_COLUMN - 1]))
        return found

    def _runbook(self) -> str:
        return (REPO_ROOT / "docs" / "RUNBOOK.md").read_text()

    def test_every_range_the_runbook_table_declares_is_one_a_backend_declares(self) -> None:
        declared = self._declared_ranges()
        self.assertEqual(
            self._ranges_in_table(self._runbook()), set(declared.values()),
            "the linter version-range table in docs/RUNBOOK.md §0-1 declares a range no linter "
            f"backend declares, or omits one that is declared (declared: {declared})")

    def test_every_declared_range_reaches_the_document(self) -> None:
        """The other direction, so a new linter's range cannot land unwritten. It names WHICH
        backend is missing, which the set comparison does not."""
        runbook = self._runbook()
        for backend_id, spec in sorted(self._declared_ranges().items()):
            self.assertIn(spec, runbook,
                          f"the {backend_id} range {spec} is declared but docs/RUNBOOK.md §0-1 "
                          f"does not state it")

    def test_every_range_stated_BESIDE_a_linter_name_is_that_linter_s(self) -> None:
        """The coverage `origin/main` had and this branch deleted, restored without its bug.

        `origin/main` asserted set identity over EVERY range in the document. That caught the
        operator's install line — `pipx install 'fortitude-lint>=0.8,<0.10'` — drifting out of the
        declared range, which is the exact failure the branch's own §Supported-versions reasoning
        is about: a document that tells an operator to install a build the launch probe then
        refuses. It also refused any non-linter prerequisite documented anywhere in the file,
        which is why this branch replaced it with a table-scoped check — and un-pinned the install
        line in the process. Measured: drifting that line to `<0.11` left HEAD green and
        `origin/main` red.

        The property restored here is narrower than a whole-document scan and wider than the
        table: any range on a LINE that names a linter's executable must be that linter's. That
        covers the install line and the host-tool table row, and cannot fire on a `python3` or
        `cmake` prerequisite, because those lines name no linter.
        """
        runbook = self._runbook()
        declared = self._declared_ranges()
        from tools.backends import registry as backend_registry

        checked = 0
        for backend_id, spec in sorted(declared.items()):
            executable = backend_registry.capability_module(
                "linter", backend_id, "lint").EXECUTABLE
            for line in runbook.splitlines():
                if executable not in line:
                    continue
                found = set(self._RANGE_RE.findall(line))
                if not found:
                    continue
                checked += 1
                self.assertEqual(
                    found, {spec},
                    f"docs/RUNBOOK.md states a version range beside {executable!r} that is not "
                    f"the range {backend_id} declares ({spec}); an operator following this line "
                    f"installs a build the launch probe refuses.\n  {line.strip()}")
        self.assertGreaterEqual(
            checked, 2,
            "no line in docs/RUNBOOK.md states a range beside a linter's executable name; this "
            "check has stopped observing the install line it exists for")

    def test_a_range_outside_the_table_s_range_column_is_not_this_check_s_business(self) -> None:
        """The over-refusal probe, driving the REAL extractor over a synthetic document.

        Two legitimate things carry a `>=x.y,<a.b` that is not a linter declaration: a
        prerequisite documented elsewhere in the runbook, and a measurement note in the table's
        own prose column. Both refused the suite at some point in this branch's history; neither
        may again. The row drives `_ranges_in_table` itself — an earlier version re-implemented
        the extraction and so could not fail for any reason in the code it claimed to witness.
        """
        document = (
            "# Runbook\n\nInstall `python3` `>=3.10,<3.14` and `cmake` `>=3.20,<4.0` first.\n\n"
            f"{self._RANGE_TABLE_HEADER}\n|---|---|---|\n"
            "| `zzlint` | `>=9.9,<9.10` | RULES.md — the interior `>=9.2,<9.8` is unmeasured |\n"
            "\nAnd afterwards, `gfortran` `>=11.0,<15.0`.\n"
        )
        self.assertEqual(self._ranges_in_table(document), {">=9.9,<9.10"})

    def test_the_extractor_refuses_a_document_whose_table_is_gone(self) -> None:
        """A renamed or deleted table must say so, not silently compare an empty set."""
        with self.assertRaises(AssertionError) as caught:
            self._ranges_in_table("# Runbook\n\nno table here\n")
        self.assertIn("no longer carries the linter version-range table", str(caught.exception))


class LinterExecutableCollisionTests(unittest.TestCase):
    """A property of EVERY linter at once, so it is asserted from no single backend's file.

    `tools/validate_pipeline_semantics._lint_preset_by_executable` builds an argv[0] -> preset map
    by enumeration, so two backends declaring the same executable would silently let the last row
    win and attribute a logged command — and a certification — to the wrong preset. It fails
    closed instead.

    It lived in `tools/tests/test_linter_cppcheck.py` for one round, where it patched the RUFF
    package to construct the collision: a cross-backend property asserted from one backend's file,
    hardcoding its sibling, which is the placement two other checks in this repository explicitly
    reject. Renaming or removing either backend turned the other's file red.
    """

    def test_two_backends_sharing_an_executable_are_refused(self) -> None:
        from unittest import mock

        from tools import validate_pipeline_semantics as vps
        from tools.backends import registry

        linters = [b for b in registry.backend_ids("linter")
                   if "lint" in registry.get("linter", b).backend_provides]
        self.assertGreaterEqual(len(linters), 2, "a collision needs two package-backed linters")
        first, second = (registry.capability_module("linter", b, "lint") for b in linters[:2])

        vps._lint_preset_by_executable.cache_clear()
        self.addCleanup(vps._lint_preset_by_executable.cache_clear)
        with mock.patch.object(second, "EXECUTABLE", first.EXECUTABLE):
            with self.assertRaises(ValueError) as caught:
                vps._lint_preset_by_executable()
        self.assertIn(first.EXECUTABLE, str(caught.exception))

    def test_the_real_declarations_have_no_collision(self) -> None:
        """The other direction, so the row above cannot pass by refusing everything."""
        from tools import validate_pipeline_semantics as vps

        vps._lint_preset_by_executable.cache_clear()
        self.addCleanup(vps._lint_preset_by_executable.cache_clear)
        self.assertGreaterEqual(len(vps._lint_preset_by_executable()), 3)


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
