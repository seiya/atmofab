#!/usr/bin/env python3
"""Unit tests for tools/spec_input_gates — the node-IDENTITY preconditions checked pre-IR.

These gates run before any phase, on the `spec_ref`s a closure resolves, so they hold no
implementation-language knowledge. The spec_id bound is asked of every declared language
backend (`max_spec_id_len`); each backend's own tests pin how its `SPEC_ID_MAX` derives.
"""

from __future__ import annotations

import unittest

from tools.spec_input_gates import (
    CASE_ID_TOKEN_RE,
    infra_dep_declared_violation,
    max_spec_id_len,
    spec_id_length_violation,
)

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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
