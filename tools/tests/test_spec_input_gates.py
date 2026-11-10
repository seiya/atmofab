#!/usr/bin/env python3
"""Unit tests for tools/spec_input_gates — the node-IDENTITY preconditions checked pre-IR.

These gates run before any phase, on the `spec_ref`s a closure resolves, so they hold no
implementation-language knowledge. The spec_id bound is asked of every declared language
backend (`max_spec_id_len`); each backend's own tests pin how its `SPEC_ID_MAX` derives.
"""

from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from tools.spec_input_gates import (
    CASE_ID_TOKEN_RE,
    infra_dep_declared_violation,
    max_spec_id_len,
    problem_dep_on_non_problem_violation,
    problem_dependency_surface_violation,
    spec_id_length_violation,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

MAX_SPEC_ID_LEN = max_spec_id_len()


class MaxSpecIdLenTest(unittest.TestCase):
    """`max_spec_id_len` is the minimum of the declared languages' `SPEC_ID_MAX`, read per call."""

    @staticmethod
    def _bundles() -> dict[str, object]:
        from tools.backends import registry
        return {lang: registry.capability_module("language", lang, "bundle_facts")
                for lang in registry.backend_ids("language")
                if registry.provides("language", lang, "bundle_facts")}

    def test_the_bound_is_the_minimum_over_declared_languages(self) -> None:
        bundles = self._bundles()
        self.assertGreaterEqual(len(bundles), 2, bundles)
        self.assertEqual(min(b.SPEC_ID_MAX for b in bundles.values()), max_spec_id_len())

    def test_the_bound_follows_each_language(self) -> None:
        """Moving ONE language's value below the others moves the gate, for every language —
        a literal, a fixed language, or a `max` in place of the `min` each fails a row."""
        from unittest import mock
        bundles = self._bundles()
        floor = min(b.SPEC_ID_MAX for b in bundles.values())
        for lang, bundle in bundles.items():
            with self.subTest(language=lang), mock.patch.object(bundle, "SPEC_ID_MAX", floor - 9):
                self.assertEqual(floor - 9, max_spec_id_len())
                msg = spec_id_length_violation("q" * (floor - 8))
                self.assertIsNotNone(msg)
                self.assertIn(f">{floor - 9}", msg)
        self.assertEqual(floor, max_spec_id_len())

    def test_a_language_without_bundle_facts_is_not_asked(self) -> None:
        """A declared language that does not provide `bundle_facts` states no bound and is
        skipped, rather than raising `capability_module`'s refusal for every launch."""
        from unittest import mock

        from tools.backends import registry
        real_provides = registry.provides
        langs = registry.backend_ids("language")
        loosest = max(langs, key=lambda lang: registry.capability_module(
            "language", lang, "bundle_facts").SPEC_ID_MAX)
        others = [lang for lang in langs if lang != loosest]
        floor_without = self._bundles()[loosest].SPEC_ID_MAX
        # Withdraw bundle_facts from every language but the loosest: only it may answer.
        with mock.patch.object(
                registry, "provides",
                side_effect=lambda axis, bid, cap: False
                if (axis, cap) == ("language", "bundle_facts") and bid in others
                else real_provides(axis, bid, cap)):
            self.assertEqual(floor_without, max_spec_id_len())

    def test_no_language_is_a_refusal_not_an_unbounded_gate(self) -> None:
        from unittest import mock

        from tools.backends import registry
        with mock.patch.object(registry, "backend_ids", return_value=()):
            with self.assertRaises(RuntimeError):
                max_spec_id_len()


class SpecIdLengthGateTest(unittest.TestCase):
    """M3d spec-input gate: `spec_id_length_violation` bounds spec_id ≤ max_spec_id_len().
    This is the canonical capture point for the one node-IDENTITY render precondition the
    compile.static hoist excludes (a re-author cannot shorten a spec_id); the conductor's
    resolve_node calls it so a too-long spec_id fails before any phase, not at render-kill."""

    def test_ok_at_or_below_limit(self) -> None:
        self.assertIsNone(spec_id_length_violation("shallow_water2d"))
        self.assertIsNone(spec_id_length_violation("x" * MAX_SPEC_ID_LEN))
        # 54-char real catalog id (the longest currently-safe one) passes.
        self.assertIsNone(
            spec_id_length_violation("dynamics_advection_diffusion_boundary_1d_periodic_copy"))

    def test_violation_above_limit(self) -> None:
        # The 61-char id the catalog carried until it was renamed to fit the bound.
        offender = "dynamics_advection_diffusion_profile_1d_upwind_center2_euler1"
        msg = spec_id_length_violation(offender)
        self.assertIsNotNone(msg)
        self.assertIn(str(len(offender)), msg)
        self.assertIn(str(MAX_SPEC_ID_LEN), msg)
        # One char over the limit is already a violation; the limit itself is not.
        self.assertIsNotNone(spec_id_length_violation("y" * (MAX_SPEC_ID_LEN + 1)))
        self.assertIsNone(spec_id_length_violation("y" * MAX_SPEC_ID_LEN))

    def test_non_string_and_whitespace(self) -> None:
        # The isinstance guard, driven on the input that needs it: a NON-string whose `str()`
        # would exceed the bound. Without the guard this gate would reject a value it cannot
        # measure, and the gate runs before any phase — the rejection would be unappealable.
        self.assertIsNone(spec_id_length_violation(None))
        self.assertIsNone(spec_id_length_violation(123))
        self.assertIsNone(spec_id_length_violation(10 ** (MAX_SPEC_ID_LEN + 5)))
        self.assertIsNone(spec_id_length_violation(["x" * (MAX_SPEC_ID_LEN + 5)]))
        # Surrounding whitespace is stripped before measuring.
        self.assertIsNone(spec_id_length_violation("  short  "))
        self.assertIsNotNone(spec_id_length_violation("  " + "z" * (MAX_SPEC_ID_LEN + 1) + "  "))


class InfraDepDeclaredGateTest(unittest.TestCase):
    """Spec-input gate: `infra_dep_declared_violation` refuses ANY `infrastructure` entry in a
    `deps.yaml`, whatever the spec's kind (issue #284, R4-a PR-3): the runner harness is the
    target profile's attribute, never a spec dependency. Until PR-3 this was the opposite rule
    (`infra_dep_count_violation`: exactly one on every spec that builds)."""

    def test_zero_passes(self) -> None:
        self.assertIsNone(infra_dep_declared_violation(0))

    def test_any_declaration_is_refused(self) -> None:
        for count in (1, 2, 5):
            msg = infra_dep_declared_violation(count)
            self.assertIsNotNone(msg, count)
            assert msg is not None
            self.assertTrue(msg.startswith("infrastructure_dependency_declared_in_deps: "), msg)
            self.assertIn(f"declares {count} `infrastructure`", msg)

    def test_message_names_the_target_profile_as_the_harness_owner_and_the_remedy(self) -> None:
        msg = infra_dep_declared_violation(1)
        assert msg is not None
        self.assertIn("spec/targets/<target_id>.yaml `harness`", msg)
        self.assertIn("Remove the `infrastructure:` section", msg)
        self.assertIn("dependency;", msg)
        self.assertIn("dependencies;", infra_dep_declared_violation(2) or "")


class CaseIdTokenGrammarTest(unittest.TestCase):
    """The one owner of the case_id safe-token grammar.

    A case_id is concatenated into `raw/state_snapshots/<case_id>.json` and reaches the runner's
    argv, so the grammar is a path/argv safety question, not a language one. It was private to
    the Fortran emitter and imported through the underscore by three other modules; it is public
    here so those readers share one spelling."""

    def test_accepts_the_shapes_a_real_case_id_takes(self) -> None:
        for ok in ("base", "case_1", "c.2", "a-b", "A1", "0", "x" * 64, "._-"[0] + "y"):
            self.assertIsNotNone(CASE_ID_TOKEN_RE.match(ok), ok)

    def test_refuses_traversal_separators_and_a_leading_dash(self) -> None:
        # `..` itself matches the character class, so every reader pairs the regex with an
        # explicit `".." not in token` check; the separators and the option-looking id are what
        # the grammar alone must refuse.
        for bad in ("../evil", "a/b", "a\\b", "-c", "", " x", "a b", "caf\u00e9"):
            self.assertIsNone(CASE_ID_TOKEN_RE.match(bad), bad)

    def test_refuses_every_character_a_shell_acts_on(self) -> None:
        # A case id also reaches the quality check's `CASES`, which the build control file
        # interpolates unquoted into a recipe line; since issue #457 deleted the build-runtime
        # library's value check, this grammar is what keeps a shell-active character out of it.
        # Each character is tried mid-token, where only the grammar can refuse it.
        for ch in "\t\n\r;&|$`'\"\\<>()*?[]{}~#!":
            with self.subTest(ch=ch):
                self.assertIsNone(CASE_ID_TOKEN_RE.match(f"a{ch}b"))

    def test_a_leading_dash_is_refused_because_it_reaches_an_argv(self) -> None:
        # A case id reaches the runner's argv, where a word opening with `-` is an option's
        # spelling, and nothing downstream refuses one (`spec_input_gates` says why); this is
        # the one place it is refused.
        self.assertIsNone(CASE_ID_TOKEN_RE.match("-base"))
        self.assertIsNotNone(CASE_ID_TOKEN_RE.match("base-1"))


class ProblemDependencyOnNonProblemTest(unittest.TestCase):
    """`problem_dep_on_non_problem_violation`: a `problems:` entry is admitted on a `problem`
    only, by the kind the caller read from the CATALOG; a caller with no catalog answer passes
    `None`, which refuses."""

    def test_a_non_problem_declaring_a_problem_dependency_is_refused(self) -> None:
        for kind in ("component", "infrastructure", "profile", " component ", None, "", 3):
            with self.subTest(kind=kind):
                violation = problem_dep_on_non_problem_violation(kind, 1)
                self.assertIsNotNone(violation)
                self.assertTrue(violation.startswith("problem_dependency_on_non_problem: "))
        # The count is what decides whether the kind is asked at all: no entry, no question.
        for kind in ("component", None):
            self.assertIsNone(problem_dep_on_non_problem_violation(kind, 0))
        self.assertIsNone(problem_dep_on_non_problem_violation("problem", 2))
        self.assertIsNone(problem_dep_on_non_problem_violation(" problem ", 1))


def _write_member_spec(repo: Path, member_id: str, section_51_yaml: str | None, *,
                       fence: bool = True) -> str:
    """A member `problem` spec under `spec/problem/<member_id>/` whose §5.1 fence holds
    `section_51_yaml` (`None`: no §5.1 subsection at all). Returns the spec_ref."""
    ref = f"spec/problem/{member_id}"
    d = repo / ref
    d.mkdir(parents=True, exist_ok=True)
    parts = ["# Controlled Spec", "", "## 5. Published interface", "",
             f"`{member_id}__step` advances the state.", ""]
    if section_51_yaml is not None:
        parts += ["### 5.1 Canonical interface", ""]
        if fence:
            parts += ["```yaml", textwrap.dedent(section_51_yaml).strip("\n"), "```", ""]
        else:
            parts += [textwrap.dedent(section_51_yaml).strip("\n"), ""]
    parts += ["## 6. Tests", "", "none", ""]
    (d / "controlled_spec.md").write_text("\n".join(parts), encoding="utf-8")
    return ref


_STEP = """
    procedures:
      - name: {name}
        args:
          - {{name: dt, type: real, kind: dp, intent: in}}
"""


class ProblemDependencySurfaceTest(unittest.TestCase):
    """`problem_dependency_surface_violation` (D11 of the system tier's Part 1): an ALLOWLIST —
    the §5.1 procedure names of the member's own spec must be exactly `{<member>__step}`.
    Every row asserts the reason token, so a violation of another rule cannot stand in."""

    REASON = "problem_dependency_surface_not_step_only: "

    def _check(self, section_51_yaml: str | None, *, member_id: str = "m",
               fence: bool = True) -> str | None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            ref = _write_member_spec(repo, member_id, section_51_yaml, fence=fence)
            return problem_dependency_surface_violation(repo, ref)

    def _assert_refused(self, violation: str | None) -> None:
        self.assertIsNotNone(violation)
        self.assertTrue(violation.startswith(self.REASON), violation)

    def test_a_step_only_problem_is_admitted_as_a_member(self) -> None:
        self.assertIsNone(self._check(_STEP.format(name="m__step")))
        # The same name twice is still the set {m__step}.
        self.assertIsNone(self._check(
            _STEP.format(name="m__step") + "      - name: m__step\n        args: []\n"))

    def test_a_member_publishing_init_is_refused(self) -> None:
        violation = self._check(_STEP.format(name="m__step")
                                + "      - name: m__init\n        args: []\n")
        self._assert_refused(violation)
        self.assertIn("m__init", violation)

    def test_a_member_publishing_a_third_operation_is_refused(self) -> None:
        for extra in ("m__reset", "other__step", "M__step", "m__step "):
            with self.subTest(extra=extra):
                self._assert_refused(self._check(
                    _STEP.format(name="m__step")
                    + f"      - name: \"{extra}\"\n        args: []\n"))
        # A lone name that is not exactly `<member>__step` is refused too: the comparison is
        # exact, not a case-folded or stripped one, and the member id is the spec directory's.
        for only in ("M__step", "m__Step", "m_step", "other__step"):
            with self.subTest(only=only):
                self._assert_refused(self._check(_STEP.format(name=f"\"{only}\"")))

    def test_a_member_whose_section_5_1_is_missing_or_unreadable_is_refused(self) -> None:
        self._assert_refused(self._check(None))  # no §5.1 subsection
        self._assert_refused(self._check(_STEP.format(name="m__step"), fence=False))  # no fence
        self._assert_refused(self._check("procedures: [unclosed"))  # not YAML
        self._assert_refused(self._check("- a list, not a mapping"))
        self._assert_refused(self._check("procedures: []"))  # empty list
        self._assert_refused(self._check("module_parameters: []"))  # no procedures key
        self._assert_refused(self._check("procedures: null"))  # present but null
        self._assert_refused(self._check("unknown_key: []"))  # load_structured_signatures refuses
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            # A spec directory with no controlled_spec.md, and an unresolved member.
            (repo / "spec" / "problem" / "gone").mkdir(parents=True)
            self._assert_refused(problem_dependency_surface_violation(repo, "spec/problem/gone"))
            self._assert_refused(problem_dependency_surface_violation(repo, None))
            self._assert_refused(problem_dependency_surface_violation(repo, ""))
            self._assert_refused(problem_dependency_surface_violation(repo, "  "))

    def test_a_member_whose_surface_read_raises_is_refused(self) -> None:
        import tools.structured_signatures as ss
        import tools.validate_pipeline_semantics as vps
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            ref = _write_member_spec(repo, "m", _STEP.format(name="m__step"))
            self.assertIsNone(problem_dependency_surface_violation(repo, ref))
            for module, name in ((vps, "_extract_controlled_spec_section"),
                                 (vps, "_extract_subsection_51"),
                                 (vps, "_section51_fence_body"),
                                 (ss, "load_structured_signatures")):
                with self.subTest(helper=name):
                    with mock.patch.object(module, name, side_effect=RuntimeError("boom")):
                        violation = problem_dependency_surface_violation(repo, ref)
                    self._assert_refused(violation)
                    self.assertIn("RuntimeError", violation)

    def test_a_member_with_a_non_string_or_missing_procedure_name_is_refused(self) -> None:
        good = "      - name: m__step\n        args: []\n"
        for bad in ("      - args: []\n",                 # no name
                    "      - name: null\n        args: []\n",
                    "      - name: 7\n        args: []\n",
                    "      - name: [m__step]\n        args: []\n",
                    "      - m__step\n",                  # entry not a mapping
                    "      - null\n"):
            with self.subTest(bad=bad):
                # The bad entry BESIDE a good one: a reader that filters a missing name out
                # (`.get("name")` with None dropped) would see exactly {m__step} and admit it.
                self._assert_refused(self._check("    procedures:\n" + good + bad))

    def test_module_parameters_types_and_interfaces_are_not_counted(self) -> None:
        block = """
            module_parameters:
              - {name: dp, base: real, value: float64}
            types:
              - name: m_state
                components:
                  - {name: h, type: real, kind: dp}
            interfaces:
              - name: m_rhs
                args:
                  - {name: x, type: real, kind: dp, intent: in}
            procedures:
              - name: m__step
                args:
                  - {name: dt, type: real, kind: dp, intent: in}
        """
        self.assertIsNone(self._check(block))
        # A name that only a NON-procedure entry carries is no member operation either way:
        # an interface named `m__init` does not publish `m__init`.
        self.assertIsNone(self._check(block.replace("name: m_rhs", "name: m__init")))

    def test_the_corpus_admits_shallow_water2d_and_refuses_the_channel_as_members(self) -> None:
        """Over the real `spec/`: the two step-only problems are admitted, the channel (which
        publishes `__init`) is refused, and so is a member that does not exist."""
        from tools.orchestration_runtime import resolve_spec_ref_for
        for sid in ("shallow_water2d", "advdiff1d_linear"):
            with self.subTest(sid=sid):
                ref = resolve_spec_ref_for(_REPO_ROOT, "problem", sid)
                self.assertTrue(ref)
                self.assertIsNone(problem_dependency_surface_violation(_REPO_ROOT, ref))
        ref = resolve_spec_ref_for(_REPO_ROOT, "problem", "shallow_water2d_channel")
        self.assertTrue(ref)
        violation = problem_dependency_surface_violation(_REPO_ROOT, ref)
        self.assertIsNotNone(violation)
        self.assertTrue(violation.startswith(self.REASON))
        self.assertIn("shallow_water2d_channel__init", violation)
        self._assert_refused(problem_dependency_surface_violation(
            _REPO_ROOT, "spec/problem/dynamics/shallow_water/does_not_exist"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
