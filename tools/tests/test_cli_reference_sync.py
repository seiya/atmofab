#!/usr/bin/env python3
"""Sync test for argparse ↔ docs/CLI_REFERENCE.md{,_RARE}.

In the CLI argument information-acquisition policy (the "Information-acquisition
policy" section of `docs/CLI_REFERENCE.md`), the frequent subcommands (Tier-A) are covered in
`docs/CLI_REFERENCE.md`, and the rare subcommands (Tier-B) are kept as an
overview only in `docs/CLI_REFERENCE_RARE.md`. This test:

1. Confirms the completeness of the argparse subcommand set and the Tier-A/Tier-B classification.
2. For a Tier-A subcommand, confirms that the argparse argument set is **included** in the
   doc argument table (additional descriptions on the doc side are allowed, an omission is rejected).
3. For a Tier-B subcommand, confirms only that it appears in the RARE doc's table
   (the detailed arguments are not diffed because the policy is `--help` as canonical).

When a diff is detected, it fails for the purpose of forcing a review of whether it
should be documented in Tier-A or Tier-B.
"""

from __future__ import annotations

import argparse
import io
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tools.orchestration_runtime import main as orchestration_runtime_main


TIER_A_SUBCOMMANDS: frozenset[str] = frozenset({
    "record-launch",
    "record-child-return",
    "deactivate-child",
    "record-reply",
    "record-agent-run",
    "finalize-child",
    "set-status",
    "mark-dependency-readiness",
    "write-step-result",
    "reserve-phase-root",
    "workflow-launch-check",
    "run-gate",
})

TIER_B_SUBCOMMANDS: frozenset[str] = frozenset({
    "init",
    "preflight",
    "preflight-status",
    "record-timeout",
    "check-phase-certified",
    "orchestration-read",
    "revoke-artifact",
    "reset-phase",
})

REPO_ROOT = Path(__file__).resolve().parents[2]
DOC_TIER_A = REPO_ROOT / "docs" / "CLI_REFERENCE.md"
DOC_TIER_B = REPO_ROOT / "docs" / "CLI_REFERENCE_RARE.md"

ARG_FLAG_RE = re.compile(r"--[a-z][a-z0-9-]*")


def _argparse_args_for(subcommand: str) -> set[str]:
    """Invoke `<sub> --help` and extract the argument flag set.

    `-h` / `--help` are excluded (the universal flag argparse auto-adds).
    """
    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            orchestration_runtime_main([subcommand, "--help"])
        except SystemExit:
            pass
    out = buf.getvalue()
    flags = {m.group(0) for m in ARG_FLAG_RE.finditer(out)}
    flags.discard("--help")
    return flags


def _doc_section_args(doc_path: Path, subcommand: str) -> set[str]:
    """Extract `--xxx` arguments from the `## <subcommand>` section in the doc.

    The section runs until the next `## ` heading or EOF. The notation assumes a
    markdown table `| `--name` | yes | ... |`.
    """
    text = doc_path.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"^## {re.escape(subcommand)}\s*$(.*?)(?=^## |\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(text)
    if not match:
        return set()
    section = match.group(1)
    flags = {m.group(0) for m in ARG_FLAG_RE.finditer(section)}
    flags.discard("--help")
    return flags


def _enumerate_argparse_subcommands() -> set[str]:
    """Enumerate the argparse subparser registrations by intercepting them with a monkey-patch.

    Because a regex parse of the `--help` output becomes fragile against changes
    in the argparse output format, capture the `add_subparsers().add_parser(name, ...)`
    calls to obtain the subcommand set.
    """
    captured: set[str] = set()
    real_add_subparsers = argparse.ArgumentParser.add_subparsers

    def patched_add_subparsers(self: argparse.ArgumentParser, **kwargs):  # type: ignore[no-untyped-def]
        action = real_add_subparsers(self, **kwargs)
        real_add_parser = action.add_parser

        def capturing_add_parser(name: str, **parser_kwargs):  # type: ignore[no-untyped-def]
            captured.add(name)
            return real_add_parser(name, **parser_kwargs)

        action.add_parser = capturing_add_parser  # type: ignore[method-assign]
        return action

    with patch.object(argparse.ArgumentParser, "add_subparsers", patched_add_subparsers):
        buf = io.StringIO()
        with redirect_stdout(buf):
            try:
                orchestration_runtime_main(["--help"])
            except SystemExit:
                pass
    return captured


class CliReferenceSyncTests(unittest.TestCase):
    """Confirm the sync of argparse and the doc."""

    def test_tier_classification_covers_all_argparse_subcommands(self) -> None:
        """Every argparse subcommand is classified as either Tier-A or Tier-B."""
        argparse_subs = _enumerate_argparse_subcommands()
        self.assertTrue(
            argparse_subs,
            "could not extract the argparse subcommand set (possible parser format change)",
        )
        classified = TIER_A_SUBCOMMANDS | TIER_B_SUBCOMMANDS
        unclassified = argparse_subs - classified
        stale = classified - argparse_subs
        self.assertFalse(
            unclassified,
            f"unclassified subcommand: {sorted(unclassified)}. "
            f"Add it to docs/CLI_REFERENCE.md (Tier-A) or docs/CLI_REFERENCE_RARE.md (Tier-B) "
            f"and also reflect it in this test's TIER_A_SUBCOMMANDS / TIER_B_SUBCOMMANDS.",
        )
        self.assertFalse(
            stale,
            f"subcommand removed on the argparse side: {sorted(stale)}. "
            f"Remove it from this test's TIER_A_SUBCOMMANDS / TIER_B_SUBCOMMANDS and the doc.",
        )

    def test_tier_a_doc_covers_all_argparse_flags(self) -> None:
        """The argparse arguments of a Tier-A subcommand appear in the doc argument table without omission.

        Additional descriptions on the doc side (derived fields, JSON payload, etc.) are allowed.
        A flag present on the argparse side but absent in the doc is failed as an omission.
        """
        # Cross-cutting flags documented once in the "Common conventions" section
        # rather than repeated in every per-subcommand section. `--verbose`
        # toggles off the default terse stdout projection and applies uniformly
        # to all bookkeeping subcommands (see CLI_REFERENCE.md Common conventions).
        global_doc_flags = {"--verbose"}
        missing: dict[str, set[str]] = {}
        for sub in sorted(TIER_A_SUBCOMMANDS):
            cli_flags = _argparse_args_for(sub)
            doc_flags = _doc_section_args(DOC_TIER_A, sub)
            absent = cli_flags - doc_flags - global_doc_flags
            if absent:
                missing[sub] = absent
        self.assertFalse(
            missing,
            "argparse arguments missing from the Tier-A doc: "
            + ", ".join(f"{sub}: {sorted(flags)}" for sub, flags in missing.items())
            + ". Add them to the relevant section of docs/CLI_REFERENCE.md.",
        )

    def test_tier_b_doc_lists_all_rare_subcommands(self) -> None:
        """A Tier-B subcommand appears in the overview table of the RARE doc.

        Because the policy is `--help` as canonical for the detailed arguments, only
        whether the name appears in the doc table is checked.
        """
        text = DOC_TIER_B.read_text(encoding="utf-8")
        missing = [sub for sub in sorted(TIER_B_SUBCOMMANDS) if f"`{sub}`" not in text]
        self.assertFalse(
            missing,
            f"Tier-B subcommand not listed in docs/CLI_REFERENCE_RARE.md: {missing}. "
            f"Add it to the overview table.",
        )

    def test_tier_b_subcommands_absent_from_tier_a_doc(self) -> None:
        """A Tier-B subcommand has no section in the Tier-A doc (maintaining compression)."""
        text = DOC_TIER_A.read_text(encoding="utf-8")
        present = [
            sub
            for sub in sorted(TIER_B_SUBCOMMANDS)
            if re.search(rf"^## {re.escape(sub)}\s*$", text, re.MULTILINE)
        ]
        self.assertFalse(
            present,
            f"treated as Tier-B but a section remains in docs/CLI_REFERENCE.md: {present}. "
            f"Remove the section and keep only the overview in the Tier-B doc.",
        )


class ValidatePipelineStageSetIsStatedOnceTests(unittest.TestCase):
    """The `--stage` set of `tools/validate_pipeline_semantics.py` is stated in THREE places,
    and only one of them is the definition.

    The definition is the validator's own argparse `choices`. The two restatements are
    `orchestration_runtime._RUN_GATE_ARGS_HELP` (what `run-gate --help` prints) and the
    `validate_pipeline_semantics` row of the `--args-json` schema table in
    `docs/CLI_REFERENCE.md`. Both are checked AGAINST the code here; neither is a source.

    Why this exists: at issue #180's review round 1 the help string was found teaching
    `'stage': 'plan|post_generate|...'`. There is no `plan` stage — `--stage plan` is an
    argparse rc 2 — and `compile`, the stage the conductor runs at `compile.static`, was
    missing. `docs/CLI_REFERENCE.md` had it right, so the two restatements had already
    drifted apart from each other, unnoticed. An operator following the help composes a
    command that cannot run, and `run_gate` persists `status="fail"` with argparse's usage
    text as `violations` — a gate failure that never happened.

    The stage set is read by DRIVING the CLI with a value it must reject, so the enumeration
    comes from argparse itself rather than from a static read of the source. The parse is
    self-tested: an empty or one-element result fails the row rather than passing it.
    """

    _VALIDATOR = REPO_ROOT / "tools" / "validate_pipeline_semantics.py"

    def _declared_stages(self) -> list[str]:
        proc = subprocess.run(
            [sys.executable, str(self._VALIDATOR), "--stage", "__no_such_stage__"],
            cwd=str(REPO_ROOT), text=True, capture_output=True, check=False)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        match = re.search(r"choose from ([^)]+)\)", proc.stderr)
        self.assertIsNotNone(
            match,
            "could not read the stage set out of argparse's refusal; the message shape "
            f"changed and this check is measuring nothing. stderr was: {proc.stderr!r}")
        stages = re.findall(r"'([^']+)'", match.group(1))
        self.assertGreater(
            len(stages), 1,
            f"parsed {stages!r} out of argparse — a set this small means the parse failed, "
            "not that the validator declares one stage")
        return stages

    def _ir_ref_stage(self) -> str:
        """The ONE declared stage that requires `--ir-ref`, asked of the validator itself.

        Driven rather than read: each declared stage is run against an EMPTY repo root and the
        one whose violation says `requires non-empty --ir-ref` is the answer. The empty root is
        what makes it affordable — the argument check fires before any tree is scanned, and
        pointing this at the real checkout costs ~170s because each run walks `workspace/`.

        Self-tested: exactly one stage must match, so a reworded violation or a second such
        stage fails the row instead of silently answering the wrong question.
        """
        declared = self._declared_stages()
        matched = []
        with tempfile.TemporaryDirectory() as empty_root:
            for stage in declared:
                proc = subprocess.run(
                    [sys.executable, str(self._VALIDATOR), "--stage", stage,
                     "--repo-root", empty_root],
                    cwd=str(REPO_ROOT), text=True, capture_output=True, check=False)
                if "requires non-empty --ir-ref" in (proc.stdout + proc.stderr):
                    matched.append(stage)
        self.assertEqual(
            len(matched), 1,
            f"expected exactly one stage to require --ir-ref, got {matched!r} out of "
            f"{declared!r}; the violation wording changed and this check is measuring nothing")
        return matched[0]

    def test_the_run_gate_help_names_exactly_the_declared_stages(self) -> None:
        """Read from the RENDERED `run-gate --help`, which is what an operator sees.

        `_RUN_GATE_ARGS_HELP` is a local of `main()`, not an importable constant, and
        argparse re-wraps it — so the rendered text is both the only reachable form and the
        right one to check. Whitespace is collapsed before matching for that reason.
        """
        declared = self._declared_stages()
        buf = io.StringIO()
        with redirect_stdout(buf):
            try:
                orchestration_runtime_main(["run-gate", "--help"])
            except SystemExit:
                pass
        rendered = re.sub(r"\s+", "", buf.getvalue())
        self.assertIn("validate_pipeline_semantics=>", rendered,
                      "run-gate --help no longer prints a per-gate schema at all")
        match = re.search(r"validate_pipeline_semantics=>\{'stage':'([^']+)'", rendered)
        self.assertIsNotNone(
            match, "the run-gate args help no longer states a stage set in the shape this "
                   "check reads; re-point it rather than deleting the row")
        stated = match.group(1).split("|")
        self.assertEqual(
            sorted(stated), sorted(declared),
            "`_RUN_GATE_ARGS_HELP` states a stage set the validator does not declare. The "
            "validator's argparse `choices` is the definition; fix the help, not the code.")
        # THE SET IS STATED TWICE PER SITE, and coupling only the alternation leaves the other
        # half free — issue #180's round 3 measured both qualifiers changeable to `plan` with
        # this class green, which is the very defect it was written for.
        qualifier = re.search(r"'ir_ref':[^(]*\((\w+)stage\)", rendered)
        self.assertIsNotNone(
            qualifier, "the run-gate args help no longer says which stage takes `ir_ref`")
        self.assertEqual(
            qualifier.group(1), self._ir_ref_stage(),
            "the help names the wrong stage as the one taking `ir_ref`")

    def test_the_cli_reference_row_names_exactly_the_declared_stages(self) -> None:
        declared = self._declared_stages()
        text = DOC_TIER_A.read_text(encoding="utf-8")
        # Anchor on the gate NAME and tolerate table whitespace: a markdown table row is
        # routinely re-aligned, and a check that goes red on a cosmetic space refuses correct
        # work — over-refusal, which is this repository's recorded default error direction.
        # Issue #180's round 2 caught exactly that here: the first version hard-coded one space
        # on each side of the pipe.
        match = re.search(
            r"\|\s*`validate_pipeline_semantics`\s*\|\s*`\{\"stage\":\s*\"([^\"]+)\"", text)
        self.assertIsNotNone(
            match, "the --args-json schema table's validate_pipeline_semantics row no longer "
                   "states a stage set in the shape this check reads. If the row is correct and "
                   "only its FORMATTING changed, re-point this regex; if the row is gone, "
                   "restore it — the stage set must be stated where an operator reads it.")
        stated = match.group(1).split("|")
        self.assertEqual(
            sorted(stated), sorted(declared),
            "docs/CLI_REFERENCE.md states a stage set the validator does not declare. The "
            "validator's argparse `choices` is the definition; fix the document.")
        # Same second statement as the help row above.
        qualifier = re.search(r'"ir_ref":[^(]*\((\w+) stage\)', text)
        self.assertIsNotNone(
            qualifier, "the --args-json schema table no longer says which stage takes `ir_ref`")
        self.assertEqual(
            qualifier.group(1), self._ir_ref_stage(),
            "docs/CLI_REFERENCE.md names the wrong stage as the one taking `ir_ref`")

class RunGateGateSetIsStatedOnceTests(unittest.TestCase):
    """`run-gate --gate`'s accepted set, coupled to the constant that defines it.

    Same shape as `ValidatePipelineStageSetIsStatedOnceTests`, one table row up. The definition
    is `orchestration_runtime.DEFAULT_ALLOWED_GATE_SERVICES`; the argparse `choices` and the
    `--gate` help are already DERIVED from it (issue #180), so the only restatement left is the
    `--gate` row of `docs/CLI_REFERENCE.md`, and nothing compared them.

    Issue #180 shrank that set by one and hand-edited that row. Round 3 pointed out that
    `b101142`'s own rationale — three statement sites is where discipline has already lost —
    applies to this row unchanged, so it gets the same treatment: the set is read from the
    RENDERED `run-gate --help`, which is where argparse prints what it will actually accept.
    """

    def _declared_gates(self) -> list[str]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            try:
                orchestration_runtime_main(["run-gate", "--help"])
            except SystemExit:
                pass
        rendered = re.sub(r"\s+", "", buf.getvalue())
        match = re.search(r"--gate\{([^}]+)\}", rendered)
        self.assertIsNotNone(
            match, "run-gate --help no longer prints the --gate choices; argparse's rendering "
                   f"changed and this check is measuring nothing. Rendered: {rendered[:400]!r}")
        gates = [g for g in match.group(1).split(",") if g]
        self.assertGreater(
            len(gates), 1,
            f"parsed {gates!r} out of argparse — a set this small means the parse failed")
        from tools.orchestration_runtime import DEFAULT_ALLOWED_GATE_SERVICES
        self.assertEqual(
            sorted(gates), sorted(DEFAULT_ALLOWED_GATE_SERVICES),
            "argparse's --gate choices and DEFAULT_ALLOWED_GATE_SERVICES have diverged; the "
            "constant is the definition and `choices` must be derived from it")
        return gates

    def test_the_cli_reference_row_names_exactly_the_declared_gates(self) -> None:
        declared = self._declared_gates()
        text = DOC_TIER_A.read_text(encoding="utf-8")
        match = re.search(r"\|\s*`--gate`\s*\|\s*yes\s*\|([^|]+)\|", text)
        self.assertIsNotNone(
            match, "the run-gate argument table no longer has a `--gate` row in the shape this "
                   "check reads. If the row is correct and only its FORMATTING changed, "
                   "re-point this regex; if it is gone, restore it.")
        stated = re.findall(r"`([a-z_]+)`", match.group(1))
        # The cell also cites the constant by name; that citation is not a gate.
        stated = [g for g in stated if g != "DEFAULT_ALLOWED_GATE_SERVICES"]
        self.assertEqual(
            sorted(set(stated)), sorted(declared),
            "docs/CLI_REFERENCE.md's `--gate` row names a set the code does not declare. "
            "`DEFAULT_ALLOWED_GATE_SERVICES` is the definition; fix the document.")

if __name__ == "__main__":
    unittest.main()
