#!/usr/bin/env python3
"""Unit tests for tools/spec_input_gates — the node-IDENTITY preconditions checked pre-IR.

These gates run before any phase, on the `spec_ref`s a closure resolves, so they hold no
implementation-language knowledge. `test_fortran_runner` pins `MAX_SPEC_ID_LEN` against the
language backend's own identifier limit, which is where the number comes from.
"""

from __future__ import annotations

import unittest

from tools.spec_input_gates import (
    CASE_ID_TOKEN_RE,
    MAX_SPEC_ID_LEN,
    infra_dep_declared_violation,
    spec_id_length_violation,
)


class SpecIdLengthGateTest(unittest.TestCase):
    """M3d spec-input gate: `spec_id_length_violation` bounds spec_id ≤ MAX_SPEC_ID_LEN.
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

    def test_a_leading_dash_is_refused_because_it_reaches_an_argv(self) -> None:
        # The build-runtime MCP server refuses a leading `-` in a `--cases` value. Accepting one
        # here would pass Compile and Build and fail only at Validate.execute, on an id no gate
        # had objected to.
        self.assertIsNone(CASE_ID_TOKEN_RE.match("-base"))
        self.assertIsNotNone(CASE_ID_TOKEN_RE.match("base-1"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
