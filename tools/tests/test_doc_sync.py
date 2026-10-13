#!/usr/bin/env python3
"""Sync tests between repository documents and the code or rules they describe.

Each test guards one class of documentation drift found by the 2026-08-10 audit
(issue #479), so the class fails at PR time instead of waiting for the next audit:

1. The JSON bundle example in `docs/workflow/CODEGEN_BUNDLE_CONTRACT.md` is a bundle
   `validate_bundle` accepts.
2. Every whole-string `pattern` under `spec/schema/` uses the portable anchor that
   `spec/schema/SCHEMA.md` requires.
3. No document a reader or a leaf is shown names a retired artifact.
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from typing import Iterator

from tools import codegen_bundle as cb


REPO_ROOT = Path(__file__).resolve().parents[2]

_JSON_FENCE_RE = re.compile(r"(?ms)^```json[ \t]*\n(.*?)^```[ \t]*$")


def the_json_example(text: str) -> str:
    """The body of the one ```json fence in `text`. Exactly one: a second example would go
    unchecked, and none means the fence spelling changed and the caller would check nothing."""
    fences = _JSON_FENCE_RE.findall(text)
    if len(fences) != 1:
        raise ValueError(f"expected exactly one ```json fence, found {len(fences)}")
    return fences[0]


class BundleExampleTest(unittest.TestCase):
    def test_the_contract_example_is_a_valid_bundle(self) -> None:
        doc_path = REPO_ROOT / "docs" / "workflow" / "CODEGEN_BUNDLE_CONTRACT.md"
        bundle = json.loads(the_json_example(doc_path.read_text(encoding="utf-8")))
        violations = cb.validate_bundle(bundle)
        self.assertEqual(violations, [],
                         "the bundle example in CODEGEN_BUNDLE_CONTRACT.md is rejected by "
                         f"validate_bundle: {violations}")

    def test_the_example_extractor_counts_json_fences_only(self) -> None:
        one = 'x\n```json\n{"a": 1}\n```\n```\nuntagged\n```\n```python\np\n```\n'
        self.assertEqual(the_json_example(one), '{"a": 1}\n')
        for text in ("```\n{}\n```\n", one + '```json\n{"b": 2}\n```\n'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                the_json_example(text)


def _schema_patterns(node: object, path: str = "") -> Iterator[tuple[str, str]]:
    """Every `pattern` string under `node`, with the JSON path of the object that holds it."""
    if isinstance(node, dict):
        if isinstance(node.get("pattern"), str):
            yield path or "/", node["pattern"]
        for key, value in node.items():
            yield from _schema_patterns(value, f"{path}/{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from _schema_patterns(item, f"{path}/{index}")


#: Patterns that are not whole-string patterns, so `spec/schema/SCHEMA.md`'s anchor rule does not
#: govern them. Keyed by (schema path relative to `spec/schema/`, JSON path of the holder).
_ANCHOR_EXEMPT: dict[tuple[str, str], str] = {
    # A contains-a-non-space check (a blank constraint is refused); pinned by test_target_profile.
    ("targets/target_profile.schema.json",
     "/properties/harness/properties/version_constraint"): r"\S",
}

#: The number of `pattern` values the walk must reach (measured 2026-10-14: codegen_bundle 10,
#: harness_capabilities 2, target_profile 3, shape_expr 3). Exact, so a walker that skips a
#: directory or a schema file fails; adding a pattern updates this number.
_EXPECTED_PATTERN_COUNT = 18


def anchor_problem(pattern: str) -> str | None:
    """Why `pattern` breaks the whole-string anchor rule of `spec/schema/SCHEMA.md`, or None."""
    for token in ("$", r"\A", r"\Z"):
        if token in pattern:
            return f"contains {token!r}"
    if not (pattern.startswith("^") and pattern.endswith(r"(?![\s\S])")):
        return "must anchor with '^' … '(?![\\s\\S])'"
    return None


class SchemaAnchorTest(unittest.TestCase):
    def test_the_anchor_rule_on_synthetic_patterns(self) -> None:
        self.assertIsNone(anchor_problem(r"^[a-z]+(?![\s\S])"))
        # The token probes keep both ends right, so only the token clause can refuse them.
        for bad in (r"^[a-z]+$", r"^[a-z]+", r"[a-z]+(?![\s\S])", r"^[a-z]+$(?![\s\S])",
                    r"^\A[a-z]+(?![\s\S])", r"^[a-z]+\Z(?![\s\S])"):
            with self.subTest(pattern=bad):
                self.assertIsNotNone(anchor_problem(bad))

    def test_every_schema_pattern_uses_the_portable_anchor(self) -> None:
        # `^` … `(?![\s\S])`, not `^` … `$`: under Python regex `$` also matches before a
        # trailing newline (the jsonschema library applies the pattern with re.search), while
        # `\A` / `\Z` are invalid in ECMA-262 (Ajv). The rule is `spec/schema/SCHEMA.md`'s.
        schema_root = REPO_ROOT / "spec" / "schema"
        schema_files = sorted(schema_root.rglob("*.json"))
        self.assertTrue(schema_files, f"no schema found under {schema_root}")
        found = 0
        exempt_seen: dict[tuple[str, str], str] = {}
        for schema_file in schema_files:
            rel = schema_file.relative_to(schema_root).as_posix()
            per_file = 0
            for holder, pattern in _schema_patterns(
                    json.loads(schema_file.read_text(encoding="utf-8"))):
                per_file += 1
                if (rel, holder) in _ANCHOR_EXEMPT:
                    exempt_seen[(rel, holder)] = pattern
                    continue
                with self.subTest(schema=rel, holder=holder, pattern=pattern):
                    self.assertIsNone(anchor_problem(pattern),
                                      f"{rel}{holder}: {pattern!r} (spec/schema/SCHEMA.md)")
            self.assertGreaterEqual(per_file, 1, f"the walk reached no pattern in {rel}")
            found += per_file
        self.assertEqual(found, _EXPECTED_PATTERN_COUNT)
        # The exemption names a pattern that still exists, spelled as exempted.
        self.assertEqual(exempt_seen, _ANCHOR_EXEMPT)


#: Artifacts the workflow no longer writes. A document that names one points a reader (or a leaf
#: whose prompt quotes it) at a file that does not exist; where each one's content went is in the
#: failure message of `test_no_document_names_a_retired_artifact`.
RETIRED_ARTIFACT_NAMES: tuple[str, ...] = ("dependency.resolved.yaml", "case.resolved.yaml")

#: The roots read, and the suffixes read under each. `docs/` and `spec/` are what a reader is
#: shown, `tools/prompt_templates/` what a leaf is shown. `TODO.md` is outside on purpose: its job
#: is to name the next open drift.
_RETIRED_NAME_ROOTS: tuple[str, ...] = ("docs", "spec", "tools/prompt_templates")
_RETIRED_NAME_SUFFIXES: tuple[str, ...] = (".md", ".yaml", ".json", ".txt")


def retired_name_occurrences(root: Path) -> list[str]:
    """`rel:lineno: name` for every line under the read roots of `root` that names a retired
    artifact. Taking a `root` is what lets a synthetic tree drive it: with no offender in this
    repository, a guard that only runs here passes whether or not it looks at anything."""
    offenders: list[str] = []
    for top in _RETIRED_NAME_ROOTS:
        base = root / top
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix not in _RETIRED_NAME_SUFFIXES:
                continue
            rel = path.relative_to(root).as_posix()
            for lineno, line in enumerate(
                    path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                for name in RETIRED_ARTIFACT_NAMES:
                    if name in line:
                        offenders.append(f"{rel}:{lineno}: {name}")
    return offenders


class RetiredArtifactNameTest(unittest.TestCase):
    def test_no_document_names_a_retired_artifact(self) -> None:
        offenders = retired_name_occurrences(REPO_ROOT)
        self.assertEqual(
            offenders, [],
            "a document names a retired artifact. `dependency.resolved.yaml`'s node set is the "
            "conductor-authored `<ir_ref>/dependency_graph.json` (`all_nodes`); "
            "`case.resolved.yaml`'s case inputs are each case's `inputs` in `spec.ir.yaml`, "
            "recorded per run in `trial_meta.json`:\n" + "\n".join(offenders))

    def test_the_walker_reads_every_root_and_suffix_it_claims(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            planted: set[str] = set()
            # Spelled out rather than read from the module constants: a family generated from
            # the constant shrinks with it, and dropping a root, a suffix or a name would pass.
            for top in ("docs", "spec", "tools/prompt_templates"):
                for suffix in (".md", ".yaml", ".json", ".txt"):
                    for name in ("dependency.resolved.yaml", "case.resolved.yaml"):
                        rel = f"{top}/nested/{name.split('.')[0]}{suffix}"
                        path = root / rel
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(f"intro\nsee `{name}` here\n", encoding="utf-8")
                        planted.add(f"{rel}:2: {name}")
            # Outside the read roots and suffixes: must NOT be reported.
            (root / "TODO.md").write_text("case.resolved.yaml\n", encoding="utf-8")
            (root / "docs" / "nested" / "x.py").write_text("case.resolved.yaml\n",
                                                           encoding="utf-8")
            self.assertEqual(set(retired_name_occurrences(root)), planted)


if __name__ == "__main__":
    unittest.main()
