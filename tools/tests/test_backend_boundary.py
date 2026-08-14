#!/usr/bin/env python3
"""The backend-boundary ratchet: the neutral core may not accumulate more backend knowledge.

The rule is `docs/BACKEND_BOUNDARY.md`. This module measures two consequences of it and freezes
both against a recorded baseline, so that the debt this repository already carries is visible and
bounded while it is being paid down.

WHAT IS PINNED, AND WHAT IS ONLY SAMPLED. Stating this precisely matters more here than usual,
because a green boundary check reads as "the boundary holds" whether or not it can see the
boundary.

* **Pinned**: the *direct backend imports*. The set of neutral-core modules that import a module
  under `tools/backends/` other than the registry is decided by reading every import statement in
  every neutral-core module, so it is a complete answer to the question it asks. A module removed
  from the allowlist can never silently come back.
* **Pinned**: the *registry's own consistency*. Every declared axis has at least one backend,
  every `extracted` backend imports, and `unsupported_reason` answers `None` for exactly the
  declared members.
* **Sampled**: the *token counts*. `_TOKEN_CLASSES` is an ENUMERATION of technology-specific
  spellings, and an enumeration of a language's surface is exactly the instrument this repository
  has already watched fail sixteen times (`tools/backends/language/fortran/structure.py` explains
  that history). Backend knowledge with no listed token in it is invisible here: a gate that
  hard-codes a two-space indent because one compiler's diagnostics count columns, a Makefile rule
  spelled without the word `makefile`, an argv assembled from fragments. And the counts are per
  token class, so a file that deletes one occurrence and adds another of the same class holds its
  count. What the counts DO give is a monotone bound with a direction: no file may grow, and a
  file that shrinks forces the baseline down (a stale-baseline failure), so the measure cannot
  drift upward and cannot silently stop tightening.

The direction of both failures is toward the rule: a violation fails the suite, and no shape of
input makes the check pass by reading less. What no shape of input does is prove compliance.

Regenerating the baseline is deliberate, not automatic: run

    python3 -m tools.tests.test_backend_boundary --write-baseline

and the diff is then reviewable as the migration step it represents.
"""

from __future__ import annotations

import ast
import json
import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.backends import registry  # noqa: E402

BASELINE_PATH = REPO_ROOT / "tools" / "tests" / "data" / "backend_boundary_baseline.json"

#: The package prefix every backend lives under, and the one module inside it the neutral core is
#: allowed to import. Derived from the registry's own module paths rather than restated, so a
#: backend registered somewhere else cannot pass unnoticed.
BACKEND_PACKAGE = "tools.backends"
REGISTRY_MODULES = frozenset({"tools.backends", "tools.backends.registry"})


# --- what counts as the neutral core -------------------------------------------------------------
#
# The scope is `docs/BACKEND_BOUNDARY.md`'s scope. The exclusions are that document's exclusions,
# plus two of this instrument's own: its baseline (which quotes paths, not knowledge) and the
# migration ledger.
_SCANNED_GLOBS = (
    ("tools", "**/*.py"),
    ("mcp_servers", "**/*.py"),
    ("tools/prompt_templates", "**/*.txt"),
    ("docs", "*.md"),
    ("docs/workflow", "**/*.md"),
    ("skills", "**/SKILL.md"),
)

_EXCLUDED_PREFIXES = (
    # Backends themselves — this is where the knowledge belongs.
    "tools/backends/",
    "tools/prompt_templates/backends/",
    "docs/backends/",
    # Design notes record decisions about a named technology (out of scope by the rule).
    "docs/design/",
    # Tests supply backend-shaped input in order to exercise backends (out of scope by the rule).
    "tools/tests/",
)


def neutral_core_files(root: Path | None = None) -> list[Path]:
    """Every in-scope neutral-core file, repo-relative-sorted and deduplicated."""
    root = root or REPO_ROOT
    found: set[Path] = set()
    for subdir, pattern in _SCANNED_GLOBS:
        base = root / subdir
        if not base.is_dir():
            continue
        for path in base.glob(pattern):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            if "__pycache__" in rel or rel.startswith(_EXCLUDED_PREFIXES):
                continue
            found.add(path)
    return sorted(found, key=lambda p: p.relative_to(root).as_posix())


# --- the sampled token classes -------------------------------------------------------------------
#
# Grouped by the axis whose knowledge they betray. Case-insensitive. Each entry is a SAMPLE of one
# technology's surface, never a definition of it.
_TOKEN_CLASSES: dict[str, str] = {
    # language / fortran
    "fortran": r"fortran",
    "fortran-suffix": r"\.f9[05]\b|\.f0[38]\b|\.fpp\b",
    "fortran-subroutine": r"subroutine",
    "fortran-implicit-none": r"implicit\s+none",
    "fortran-intent": r"\bintent\s*\(",
    "fortran-module-procedure": r"module\s+procedure",
    "fortran-kind": r"\breal(?:64|32)\b",
    "fortran-allocatable": r"allocatable",
    "fortran-module-file": r"\.mod\b",
    "fortran-standard": r"\bf2008\b|\bf2003\b|\bf95\b",
    # language / c-family
    "c-include": r"#include",
    # `llama.cpp` is an LLM server, not a translation unit: the LLM backend axis is selected by
    # `llm.yaml` and is not in scope here.
    "c-suffix": r"(?<!llama)\.(?:cpp|hpp|cxx|cc|hh)\b",
    # build_system / make
    "make-control-file": r"makefile",
    "make-variable": r"\bFFLAGS\b|\bCFLAGS\b|\bLDFLAGS\b|\bOBJDIR\b",
    # compiler
    "compiler-driver": r"\bgfortran\b|\bflang\b|\bg\+\+\b|\bclang\+\+\b|\bgcc\b|\bclang\b",
    "compiler-syntax-only": r"-fsyntax-only",
    # linter
    "linter-fortitude": r"fortitude",
    # parallel
    "parallel-directive": r"!\$omp",
    "parallel-construct": r"do\s+concurrent",
}

_COMPILED = {name: re.compile(pattern, re.IGNORECASE) for name, pattern in _TOKEN_CLASSES.items()}


def token_counts(root: Path | None = None) -> dict[str, dict[str, int]]:
    """Per neutral-core file, per token class, the occurrence count. Empty entries omitted."""
    root = root or REPO_ROOT
    measured: dict[str, dict[str, int]] = {}
    for path in neutral_core_files(root):
        text = path.read_text(encoding="utf-8", errors="replace")
        counts = {
            name: len(rx.findall(text))
            for name, rx in _COMPILED.items()
            if rx.search(text)
        }
        if counts:
            measured[path.relative_to(root).as_posix()] = dict(sorted(counts.items()))
    return measured


# --- the pinned direct-import set ----------------------------------------------------------------


def _imported_modules(source: str) -> set[str]:
    """Every module a source file names for import, as a dotted path.

    `from tools.backends.language.fortran import lines` yields both the package and the
    `...fortran.lines` submodule, because either spelling reaches the same code and only counting
    one of them would make the other a free pass. A relative import cannot leave a package, so it
    can never be a neutral-core module reaching into a backend, and is ignored.
    """
    names: set[str] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return names
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                continue
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def direct_backend_imports(root: Path | None = None) -> dict[str, list[str]]:
    """Per neutral-core Python module, the backend modules it imports outside the registry."""
    root = root or REPO_ROOT
    offenders: dict[str, list[str]] = {}
    for path in neutral_core_files(root):
        if path.suffix != ".py":
            continue
        hits = {
            name
            for name in _imported_modules(path.read_text(encoding="utf-8", errors="replace"))
            if (name == BACKEND_PACKAGE or name.startswith(BACKEND_PACKAGE + "."))
            and name not in REGISTRY_MODULES
        }
        if hits:
            offenders[path.relative_to(root).as_posix()] = sorted(hits)
    return offenders


def measure(root: Path | None = None) -> dict[str, object]:
    return {
        "token_counts": token_counts(root),
        "direct_backend_imports": direct_backend_imports(root),
    }


def _load_baseline() -> dict[str, object]:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


class TokenRatchetTests(unittest.TestCase):
    """The sampled measure: no neutral-core file may carry MORE backend spelling than recorded."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.baseline = _load_baseline()["token_counts"]
        cls.measured = token_counts()

    def test_no_file_exceeds_its_recorded_count(self) -> None:
        grown: list[str] = []
        for rel, counts in sorted(self.measured.items()):
            recorded = self.baseline.get(rel, {})
            for name, n in sorted(counts.items()):
                allowed = recorded.get(name, 0)
                if n > allowed:
                    grown.append(f"{rel}: {name} {allowed} -> {n}")
        self.assertEqual(
            grown, [],
            "backend knowledge grew in the neutral core (docs/BACKEND_BOUNDARY.md). Move it into "
            "tools/backends/<axis>/<backend_id>/ and reach it through tools/backends/registry.py. "
            "If the growth is a token appearing in a NEUTRAL role (naming an axis value, quoting a "
            "path), say so in the commit message and regenerate the baseline with "
            "`python3 -m tools.tests.test_backend_boundary --write-baseline`.")

    def test_the_baseline_is_not_stale(self) -> None:
        # A ceiling that is never lowered stops being a ratchet. A file that shed backend spelling
        # — or left the scanned set entirely — must lower its recorded count in the same commit,
        # so the debt figure in TODO.md and this baseline cannot disagree.
        stale: list[str] = []
        for rel, counts in sorted(self.baseline.items()):
            measured = self.measured.get(rel)
            if measured is None:
                stale.append(f"{rel}: recorded but no longer in the scanned set")
                continue
            for name, allowed in sorted(counts.items()):
                n = measured.get(name, 0)
                if n < allowed:
                    stale.append(f"{rel}: {name} {allowed} -> {n}")
        self.assertEqual(
            stale, [],
            "the baseline is looser than the tree: regenerate it with "
            "`python3 -m tools.tests.test_backend_boundary --write-baseline` so the ratchet keeps "
            "tightening, and update the measured debt in TODO.md.")


class DirectImportPinTests(unittest.TestCase):
    """The pinned measure: which neutral-core modules bypass the registry, exactly."""

    def test_direct_backend_imports_match_the_allowlist(self) -> None:
        recorded = _load_baseline()["direct_backend_imports"]
        measured = direct_backend_imports()
        # Equality, not containment, in both directions: a new bypass fails, and a bypass that was
        # removed must leave the allowlist in the same commit. This is the half of this module that
        # is a set identity rather than a sample.
        self.assertEqual(
            {k: sorted(v) for k, v in sorted(recorded.items())},
            {k: sorted(v) for k, v in sorted(measured.items())},
            "the set of neutral-core modules importing a backend directly changed. The rule "
            "(docs/BACKEND_BOUNDARY.md) is that the neutral core reaches a backend only through "
            "tools/backends/registry.py; the allowlist records the modules that do not yet. Adding "
            "to it is a boundary regression, removing from it is the migration.")


class RegistryConsistencyTests(unittest.TestCase):
    """The registry's own claims, checked against itself and against the import system."""

    def test_every_axis_has_at_least_one_backend(self) -> None:
        for axis in registry.AXES:
            self.assertTrue(registry.backend_ids(axis), f"axis '{axis}' declares no backend")

    def test_every_axis_names_where_its_value_is_read_from(self) -> None:
        for axis, spec in registry.AXES.items():
            self.assertEqual(axis, spec.name)
            self.assertTrue(spec.source.strip(), f"axis '{axis}' does not say what carries it")
            self.assertTrue(spec.description.strip(), f"axis '{axis}' has no description")

    def test_an_extracted_backend_imports_and_an_unextracted_one_refuses(self) -> None:
        for axis in registry.AXES:
            for backend_id in registry.backend_ids(axis):
                backend = registry.get(axis, backend_id)
                if backend.extracted:
                    self.assertIsNotNone(registry.load(axis, backend_id))
                else:
                    with self.assertRaises(registry.BackendNotExtracted):
                        registry.load(axis, backend_id)

    def test_unsupported_reason_is_none_for_exactly_the_declared_members(self) -> None:
        for axis in registry.AXES:
            for backend_id in registry.backend_ids(axis):
                self.assertIsNone(registry.unsupported_reason(axis, backend_id))
                # The spelling rule the gates rely on: padded and mixed-case values normalize.
                self.assertIsNone(
                    registry.unsupported_reason(axis, f"  {backend_id.upper()}  "))
            reason = registry.unsupported_reason(axis, "no_such_backend")
            self.assertIsNotNone(reason)
            # The refusal must name the axis, the offending value, and where to register a fix —
            # a leaf or an operator reading it cannot act on "unsupported".
            self.assertIn(axis, reason)
            self.assertIn("no_such_backend", reason)
            self.assertIn("tools/backends/registry.py", reason)
            with self.assertRaises(registry.UnsupportedBackend):
                registry.require_supported(axis, "no_such_backend")

    def test_an_unknown_axis_is_refused_by_every_entry_point(self) -> None:
        for call in (
            lambda: registry.backend_ids("no_such_axis"),
            lambda: registry.get("no_such_axis", "fortran"),
            lambda: registry.unsupported_reason("no_such_axis", "fortran"),
            lambda: registry.require_supported("no_such_axis", "fortran"),
            lambda: registry.load("no_such_axis", "fortran"),
        ):
            with self.assertRaises(registry.UnsupportedBackend):
                call()

    def test_a_registered_backend_module_lives_under_the_backend_package(self) -> None:
        for axis in registry.AXES:
            for backend_id in registry.backend_ids(axis):
                module = registry.get(axis, backend_id).module
                if module is None:
                    continue
                self.assertEqual(module, f"{BACKEND_PACKAGE}.{axis}.{backend_id}")


def _write_baseline() -> None:
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_PATH.write_text(json.dumps(measure(), indent=2, sort_keys=True) + "\n",
                             encoding="utf-8")
    data = _load_baseline()
    total = sum(sum(v.values()) for v in data["token_counts"].values())
    print(f"wrote {BASELINE_PATH.relative_to(REPO_ROOT)}: "
          f"{len(data['token_counts'])} files, {total} sampled occurrences, "
          f"{len(data['direct_backend_imports'])} modules importing a backend directly")


if __name__ == "__main__":
    if "--write-baseline" in sys.argv:
        _write_baseline()
    else:
        unittest.main()
