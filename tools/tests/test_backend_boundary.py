#!/usr/bin/env python3
"""The backend-boundary ratchet: the neutral core may not accumulate more backend knowledge.

The rule is `docs/BACKEND_BOUNDARY.md`. This module measures two consequences of it and freezes
both against a recorded baseline, so that the debt this repository already carries is visible and
bounded while it is being paid down.

WHAT IS PINNED, AND WHAT IS ONLY SAMPLED. Stating this precisely matters more here than usual,
because a green boundary check reads as "the boundary holds" whether or not it can see the
boundary.

* **Pinned, over three spellings**: the *direct backend imports*. The set of neutral-core modules
  that reach a module under `tools/backends/` other than the registry is decided by reading every
  `import`, every `from ... import`, and every `importlib.import_module` with a literal argument,
  so within those three it is a complete answer and a module removed from the allowlist cannot
  silently come back. A module name COMPUTED at runtime is out of reach of any static reader and
  is not covered — `_imported_modules` says so at the point where it stops looking.
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
import importlib.util
import json
import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import tools.validate_pipeline_semantics as vps  # noqa: E402
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
#
# Every glob is RECURSIVE. The first version scanned `docs/*.md` plus `docs/workflow/**` only,
# which made a move into any new `docs/<subdir>/` indistinguishable from a migration into a
# backend: both lowered the debt figure and both stayed green. Review demonstrated it by moving
# `CHECKS_MODULE_CONTRACT.md` (76 occurrences) into a fresh `docs/reference/` and regenerating.
# The declaration files under `mcp_servers/tools/` and the two root documents are here for the
# same reason: they are where `compiler`- and `linter`-axis argv is actually spelled.
_SCANNED_GLOBS = (
    ("tools", "**/*.py"),
    ("tools/prompt_templates", "**/*.txt"),
    ("mcp_servers", "**/*.py"),
    ("mcp_servers", "**/*.md"),
    ("mcp_servers", "**/*.json"),
    ("docs", "**/*.md"),
    ("skills", "**/*.md"),
    ("skills", "**/*.py"),
    (".", "README.md"),
    (".", "AGENTS.md"),
    (".", "CLAUDE.md"),
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


def _module_prefix(dotted: str) -> str:
    """The longest prefix of `dotted` that names a real module or package, or `dotted` itself.

    `from ...fortran.signatures import SignatureParseError` names a SYMBOL, not a module, and
    the first version of this reader recorded the symbol. Two consequences, both demonstrated in
    review: importing one MORE symbol from an already-allowlisted module failed the pin, and
    re-spelling the identical crossing as `import signatures as _sig` failed it too — while the
    failure message said "the set of neutral-core MODULES importing a backend directly changed".
    Neither had crossed a boundary that was not already crossed. Collapsing to the module makes
    the recorded set what its name says it is, so the allowlist counts crossings rather than
    spellings.
    """
    parts = dotted.split(".")
    for stop in range(len(parts), 0, -1):
        candidate = ".".join(parts[:stop])
        try:
            if importlib.util.find_spec(candidate) is not None:
                return candidate
        except (ImportError, AttributeError, ValueError):
            continue
    return dotted


def _imported_modules(source: str) -> set[str]:
    """Every backend-package module a source file reaches, as a dotted module path.

    Three spellings are read: `import a.b`, `from a.b import c`, and `importlib.import_module`
    with a STRING LITERAL argument — the last because it is the spelling `registry.load` itself
    uses, so a neutral-core module can copy it. A relative import cannot leave a package, so it
    can never be a neutral-core module reaching into a backend, and is ignored.

    WHAT THIS CANNOT SEE, so that the pin is not read as more than it is: a module name computed
    at runtime (an f-string, a concatenation, a name from a config file) is out of reach of any
    static reader. `docs/BACKEND_BOUNDARY.md` states the criterion as "imports, or names for
    import"; a computed name does neither at parse time. The pin is a complete answer over the
    three spellings above and silent beyond them.
    """
    names: set[str] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return names
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(_module_prefix(alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                continue
            names.add(_module_prefix(node.module))
            names.update(_module_prefix(f"{node.module}.{alias.name}") for alias in node.names)
        elif isinstance(node, ast.Call):
            func = node.func
            attr = func.attr if isinstance(func, ast.Attribute) else (
                func.id if isinstance(func, ast.Name) else None)
            if attr != "import_module" or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                names.add(_module_prefix(first.value))
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


class ImportReaderTests(unittest.TestCase):
    """The three spellings `_imported_modules` claims to read, and the one it does not.

    The tree exercises only `import` and `from ... import` today, so the `importlib` branch
    survived a mutation that deleted it — an unexercised branch is a claim with no witness.
    These probes are that witness. They are SAMPLES of each spelling, not a definition of the
    set of ways Python can reach a module; the docstring above states where the reader stops.
    """

    def test_it_reads_all_three_declared_spellings(self) -> None:
        for label, source in (
            ("import", "import tools.backends.language.fortran.lines\n"),
            ("from-import-module",
             "from tools.backends.language import fortran\n"),
            ("from-import-symbol",
             "from tools.backends.language.fortran.signatures import SignatureParseError\n"),
            ("importlib-literal",
             'import importlib\n'
             'm = importlib.import_module("tools.backends.language.fortran.lines")\n'),
            ("importlib-bare-name",
             'from importlib import import_module\n'
             'm = import_module("tools.backends.language.fortran.structure")\n'),
        ):
            found = {n for n in _imported_modules(source) if n.startswith(BACKEND_PACKAGE)}
            self.assertTrue(found, f"{label}: the reader saw no backend module")
            for name in found:
                self.assertIsNotNone(importlib.util.find_spec(name),
                                     f"{label}: {name} is not a module")

    def test_a_computed_module_name_is_out_of_reach_and_that_is_recorded(self) -> None:
        # Not a defect to fix — a static reader cannot resolve this. Pinned so that the
        # docstring's stated limit stays true rather than becoming a guess, and so that anyone
        # who later claims the pin is total has a failing test to argue with.
        source = ('import importlib\n'
                  'axis = "language"\n'
                  'm = importlib.import_module(f"tools.backends.{axis}.fortran.lines")\n')
        self.assertEqual(
            set(), {n for n in _imported_modules(source) if n.startswith(BACKEND_PACKAGE)})

    def test_a_symbol_import_is_recorded_as_the_module_it_crosses_into(self) -> None:
        source = ("from tools.backends.language.fortran.signatures import "
                  "SignatureParseError, render_symbol_to_fortran\n")
        # Both symbols and the `from` target collapse to the one module actually crossed into —
        # that collapse is the whole point, so the expected set is a singleton.
        self.assertEqual(
            {"tools.backends.language.fortran.signatures"},
            {n for n in _imported_modules(source) if n.startswith(BACKEND_PACKAGE)})


class TokenClassReachTests(unittest.TestCase):
    """Every declared token class must be exercised, or it can be deleted for free.

    A class whose baseline is zero everywhere is ratcheting nothing: removing it from
    `_TOKEN_CLASSES` changes no count and no test notices. Review found exactly one such class
    (`c-include`). Rather than delete it — the C family is a declared future language — each
    class is exercised against a synthetic sample here, so the class list is pinned by
    something even when the tree happens not to contain that spelling.
    """

    #: One string per class that the class MUST match, and one that it must NOT. The negative
    #: half is what stops a class from being widened into a catch-all to keep this test green.
    _PROBES: dict[str, tuple[str, str]] = {
        "fortran": ("iso_fortran_env", "iso c binding"),
        "fortran-suffix": ("model.f90", "model.py"),
        "fortran-subroutine": ("end subroutine foo", "end procedure foo"),
        "fortran-implicit-none": ("implicit none", "implicitly none"),
        "fortran-intent": ("intent(in)", "intention of"),
        "fortran-module-procedure": ("module procedure add", "module parameter add"),
        "fortran-kind": ("real64", "real 64"),
        "fortran-allocatable": ("allocatable :: x", "allocated :: x"),
        "fortran-module-file": ("harness.mod", "harness.module"),
        "fortran-standard": ("-std=f2008", "-std=c99"),
        "c-include": ("#include <stdio.h>", "include stdio"),
        "c-suffix": ("kernel.cpp", "llama.cpp"),
        "make-control-file": ("src/Makefile", "src/BUILD.bazel"),
        "make-variable": ("FFLAGS += -O2", "FLAGS += -O2"),
        "compiler-driver": ("gfortran -c", "fortran compiler"),
        "compiler-syntax-only": ("-fsyntax-only", "--syntax-only"),
        "linter-fortitude": ("fortitude check", "fortifying the gate"),
        "parallel-directive": ("!$omp parallel do", "$omp parallel do"),
        "parallel-construct": ("do concurrent (i=1:n)", "run these concurrently"),
    }

    def test_every_declared_class_has_a_probe(self) -> None:
        self.assertEqual(sorted(_TOKEN_CLASSES), sorted(self._PROBES),
                         "a token class was added or removed without its probe pair")

    def test_each_class_matches_its_positive_and_rejects_its_negative(self) -> None:
        for name, (positive, negative) in sorted(self._PROBES.items()):
            rx = _COMPILED[name]
            self.assertTrue(rx.search(positive), f"{name} no longer matches {positive!r}")
            self.assertIsNone(rx.search(negative), f"{name} now matches {negative!r}")


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
            # An open-vocabulary axis accepts an unlisted token by design; the refusal below is
            # about a CLOSED axis. `open_vocabulary` is read from the axis rather than the axis
            # name being special-cased, so declaring another open axis cannot make this vacuous
            # without also making the refusal it skips untrue.
            if registry.AXES[axis].open_vocabulary:
                self.assertIsNone(registry.unsupported_reason(axis, "no_such_backend"))
                continue
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

    def test_membership_and_usability_are_different_questions(self) -> None:
        # The defect this pins: guarding a hard-coded Fortran renderer on MEMBERSHIP meant that
        # declaring a second `language` member with `module=None` — the state 5 of the 8 records
        # are in, and the state the ledger says is normal — silently stopped the signature gates
        # refusing. Asked of the registry rather than of a literal id list, so adding a backend
        # cannot make this test vacuous.
        for axis in registry.AXES:
            for backend_id in registry.backend_ids(axis):
                backend = registry.get(axis, backend_id)
                self.assertIsNone(registry.unsupported_reason(axis, backend_id))
                if backend.extracted:
                    self.assertIsNone(registry.unavailable_reason(axis, backend_id))
                    registry.require_available(axis, backend_id)
                else:
                    reason = registry.unavailable_reason(axis, backend_id)
                    self.assertIsNotNone(
                        reason, f"{axis}/{backend_id} is unextracted but reads as usable")
                    self.assertIn("not extracted", reason)
                    with self.assertRaises(registry.BackendNotExtracted):
                        registry.require_available(axis, backend_id)

    def test_the_signature_gates_ask_for_usability_not_membership(self) -> None:
        # The gate-side half of the pin above: reading the module text, because the failure
        # being prevented is a call to the WRONG registry function, which no fixture can show
        # without a second language backend existing.
        source = Path(vps.__file__).read_text(encoding="utf-8")
        self.assertEqual(
            0, source.count('backend_registry.unsupported_reason("language"'),
            "a signature gate guards a Fortran-only renderer on membership alone")
        self.assertEqual(
            2, source.count('backend_registry.unavailable_reason("language"'),
            "the two signature gates must both ask whether the language backend is usable")

    def test_an_open_vocabulary_axis_accepts_a_value_it_has_no_record_for(self) -> None:
        # `parallel` is an exploration knob whose schema says its vocabulary is deliberately not
        # a whitelist; the validator accepts `openmp+simd` and friends today. Membership must
        # not refuse those, and usability must still refuse them, since no code exists for them.
        self.assertTrue(registry.AXES["parallel"].open_vocabulary)
        for value in ("openmp+simd", "openmp_tasks", "cpu_openmp"):
            self.assertIsNone(registry.unsupported_reason("parallel", value))
            reason = registry.unavailable_reason("parallel", value)
            self.assertIsNotNone(reason)
            self.assertIn("no backend package", reason)
        # An empty token is not a value; it stays refused even on an open axis.
        self.assertIsNotNone(registry.unsupported_reason("parallel", "   "))
        # A closed axis is unaffected.
        self.assertIsNotNone(registry.unsupported_reason("language", "no_such_language"))

    def test_the_linter_members_agree_with_the_gate_that_accepts_presets(self) -> None:
        # Two owners of one fact. The registry listing fewer linters than the live gate accepts
        # is the drift this repository keeps paying for, so it is compared rather than restated.
        self.assertEqual(
            set(registry.backend_ids("linter")), set(vps._LINT_ALLOWED_PRESETS))

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
