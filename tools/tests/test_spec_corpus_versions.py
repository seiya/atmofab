#!/usr/bin/env python3
"""The version fields one spec states in three places agree (issue #324, R4-d PR-2).

A spec's `spec_version` is written in `controlled_spec.md` §0, in `tests.md` §0
(`spec_ref.spec_version`) and in `spec/registry/spec_catalog.yaml`; its `test_profile_version`
in `tests.md` §0 and in the `controlled_spec.md` tests-reference sentence ("with
`test_profile_version` of `X`", `docs/CONTROLLED_SPEC.md`). The Compile leaves are handed both
files, so a bump that reaches one and not the other hands them a contradiction. R4-d PR-2 bumped
fifteen `tests.md` and first left all fifteen tests-reference sentences behind.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
_SPEC_VERSION = re.compile(r"^- `spec_version`: `([^`]+)`", re.M)
_REF_SPEC_VERSION = re.compile(r"^- `spec_ref\.spec_version`: `([^`]+)`", re.M)
_TEST_PROFILE_VERSION = re.compile(r"^- `test_profile_version`: `([^`]+)`", re.M)
_TESTS_REFERENCE = re.compile(r"with `test_profile_version` of `([^`]+)`")


def version_disagreements(entries: list[dict], root: Path) -> list[str]:
    """Every disagreement among the version fields of the catalog `entries`, read under
    `root`. A field a file does not carry is not compared (a `profile` has no tests.md)."""
    out: list[str] = []
    for e in entries:
        sid = e["spec_id"]
        cs = (root / e["controlled_spec_path"]).read_text(encoding="utf-8")
        found = _SPEC_VERSION.findall(cs)
        if found != [e["spec_version"]]:
            out.append(f"{sid}: controlled_spec.md spec_version {found} != catalog "
                       f"{e['spec_version']!r}")
        tp = e.get("tests_path")
        if not tp or not (root / tp).is_file():
            continue
        tests = (root / tp).read_text(encoding="utf-8")
        ref = _REF_SPEC_VERSION.findall(tests)
        if ref != [e["spec_version"]]:
            out.append(f"{sid}: tests.md spec_ref.spec_version {ref} != catalog "
                       f"{e['spec_version']!r}")
        tpv = _TEST_PROFILE_VERSION.findall(tests)
        cited = _TESTS_REFERENCE.findall(cs)
        if cited and cited != tpv:
            out.append(f"{sid}: controlled_spec.md cites test_profile_version {cited}, "
                       f"tests.md declares {tpv}")
    return out


def profile_selection_on_non_adopters(entries: list[dict], root: Path) -> list[str]:
    """Every `tests.md` that names `profile_selection` although its spec adopts no profile.

    The key is declared exactly when the node adopts a profile (the phase_01 schema block,
    issue #358); a node whose `deps.yaml` `dependencies.profiles` is empty declares none, so
    its `tests.md` has no reason to name it — the harness `tests.md` did, and gave the key a
    second, runtime meaning a Compile.verify then graded against."""
    out: list[str] = []
    for e in entries:
        tp, dp = e.get("tests_path"), e.get("deps_path")
        if not tp or not (root / tp).is_file():
            continue
        if "profile_selection" not in (root / tp).read_text(encoding="utf-8"):
            continue
        deps = yaml.safe_load((root / dp).read_text(encoding="utf-8")) if dp else None
        profiles = ((deps or {}).get("dependencies") or {}).get("profiles") or []
        if not profiles:
            out.append(f"{e['spec_id']}: tests.md names profile_selection, but the spec "
                       "adopts no profile")
    return out


class SpecCorpusVersionTest(unittest.TestCase):
    def _entries(self) -> list[dict]:
        doc = yaml.safe_load((REPO / "spec/registry/spec_catalog.yaml").read_text())
        return doc["specs"]

    def test_the_corpus_agrees(self) -> None:
        entries = self._entries()
        # the tests-reference sentence is read for every physics spec (not vacuous)
        cited = [e for e in entries if _TESTS_REFERENCE.search(
            (REPO / e["controlled_spec_path"]).read_text(encoding="utf-8"))]
        self.assertGreaterEqual(len(cited), 15)
        self.assertEqual(version_disagreements(entries, REPO), [])

    def test_each_field_disagreement_is_reported(self) -> None:
        """Drive the rule on a synthetic copy where each field in turn disagrees."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "s").mkdir()
            cs = ("- `spec_version`: `0.1.1`\n\nThe corresponding `tests.md` is `s/tests.md`, "
                  "with `test_profile_version` of `0.2.0`.\n")
            tests = ("- `test_profile_version`: `0.2.0`\n- `spec_ref.spec_version`: `0.1.1`\n")
            entry = {"spec_id": "s", "spec_version": "0.1.1",
                     "controlled_spec_path": "s/controlled_spec.md", "tests_path": "s/tests.md"}

            def run(c: str, t: str, e: dict) -> list[str]:
                (root / "s/controlled_spec.md").write_text(c)
                (root / "s/tests.md").write_text(t)
                return version_disagreements([e], root)

            self.assertEqual(run(cs, tests, entry), [])
            self.assertIn("cites test_profile_version",
                          run(cs, tests.replace("`0.2.0`", "`0.3.0`"), entry)[0])
            self.assertIn("tests.md spec_ref.spec_version",
                          run(cs, tests.replace("`0.1.1`", "`0.1.2`"), entry)[0])
            self.assertIn("controlled_spec.md spec_version",
                          run(cs.replace("`0.1.1`", "`0.1.2`"), tests, entry)[0])
            self.assertEqual(len(run(cs, tests, {**entry, "spec_version": "0.1.2"})), 2)


    def test_no_tests_md_names_profile_selection_on_a_node_adopting_no_profile(self) -> None:
        self.assertEqual(profile_selection_on_non_adopters(self._entries(), REPO), [])

    def test_the_profile_selection_rule_is_driven_both_ways(self) -> None:
        """Synthetic: the corpus answer is empty, so drive a refusal and an acceptance."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "s").mkdir()
            entry = {"spec_id": "s", "tests_path": "s/tests.md", "deps_path": "s/deps.yaml"}
            (root / "s/tests.md").write_text("- each case carries `inputs.profile_selection`\n")
            (root / "s/deps.yaml").write_text("dependencies:\n  profiles: []\n")
            self.assertIn("adopts no profile",
                          profile_selection_on_non_adopters([entry], root)[0])
            (root / "s/deps.yaml").write_text(
                "dependencies:\n  profiles:\n    - profile_id: p\n")
            self.assertEqual(profile_selection_on_non_adopters([entry], root), [])
            (root / "s/deps.yaml").write_text("dependencies:\n  profiles: []\n")
            (root / "s/tests.md").write_text("- selected by its `case_id`\n")
            self.assertEqual(profile_selection_on_non_adopters([entry], root), [])


if __name__ == "__main__":
    unittest.main()
