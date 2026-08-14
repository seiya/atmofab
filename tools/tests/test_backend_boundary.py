#!/usr/bin/env python3
"""The backend-boundary ratchet: the neutral core may not accumulate more backend knowledge.

The rule is `docs/BACKEND_BOUNDARY.md`. This module measures two consequences of it and freezes
both against a recorded baseline, so that the debt this repository already carries is visible and
bounded while it is being paid down.

WHAT IS PINNED, AND WHAT IS ONLY SAMPLED. Stating this precisely matters more here than usual,
because a green boundary check reads as "the boundary holds" whether or not it can see the
boundary.

* **Pinned, over four spellings**: the *direct backend imports*. The set of neutral-core modules
  that reach a module under `tools/backends/` other than the registry is read from every
  `import`, every absolute `from ... import`, every RELATIVE `from . import`, and every
  `importlib.import_module` with a literal argument. Over those four the answer is complete and a
  module removed from the allowlist cannot silently come back. Two things it does not do, both
  once claimed otherwise: a module name COMPUTED at runtime is out of reach of any static reader
  and is not covered; an UNPARSEABLE module is not read as clean but raises
  `UnparseableNeutralModule`. Earlier versions of this sentence said "three spellings" and "a
  complete answer" while relative imports were skipped on the false premise that they cannot
  leave a package — `tools/` is a namespace package that contains `tools/backends/`, so
  `from .backends.build_system.make import RULE` crosses the boundary without leaving it — and
  while a `SyntaxError` returned the empty set.
* **Pinned by hand, in `ALLOWLIST_PATH`, which no command writes**: the allowlist above, the
  scanned file set, and the token-class list. The first shared a file with the sampled half for
  four review rounds and `--write-baseline` rewrote both, so a bypass added in any commit that
  also shed a sampled token was absorbed without a word. The other two were observable only
  through the regenerable baseline, whose failure message tells the maintainer to regenerate —
  so narrowing the scope or deleting a token class failed once and passed forever after.
* **Pinned**: the *registry's own consistency*. Every declared axis has at least one backend,
  every `extracted` backend imports, and `unsupported_reason` answers `None` for exactly the
  declared members of a closed axis — an `open_vocabulary` axis accepts any non-empty token by
  design, and the tests read that flag from the axis rather than naming which axis it is.
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

The direction of every failure is toward the rule. What NO shape of input does is prove
compliance — and the reverse claim, that no input makes the check pass by reading less, was made
here and was false three times over: the scope, the class list and the allowlist could each be
narrowed and then blessed by one regeneration. Those three are hand-pinned now; the sampled
counts remain a sample.

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

import tools.validate_pipeline_semantics as vps  # noqa: E402
from tools.backends import registry  # noqa: E402

#: The SAMPLED half. Regenerable: `--write-baseline` rewrites it, and the growth check tells the
#: maintainer to do exactly that when a token appears in a neutral role.
BASELINE_PATH = REPO_ROOT / "tools" / "tests" / "data" / "backend_boundary_baseline.json"

#: The PINNED half, in its own file that no command writes: the direct-import allowlist, the
#: scanned file set, and the token-class list. The allowlist lived in the regenerable file for
#: four review rounds, and `--write-baseline` rewrote both — so the remedy this module prescribes
#: for the sample laundered the pin. Regeneration is not rare: a commit that merely deletes a
#: sampled token from an in-scope file is asked to regenerate, and roughly one in five recent
#: commits does. (A figure of "14 of the 60 preceding commits" appeared here and did not
#: reproduce — an independent count of the same range gave 10 — so it is stated as an order of
#: magnitude rather than a measurement.) A change here is a hand edit, reviewed as the boundary
#: decision it is.
ALLOWLIST_PATH = REPO_ROOT / "tools" / "tests" / "data" / "backend_boundary_allowlist.json"

#: The package prefix every backend lives under, and the one module inside it the neutral core is
#: allowed to import. Derived from the registry's own module paths rather than restated, so a
#: backend registered somewhere else cannot pass unnoticed.
BACKEND_PACKAGE = "tools.backends"
REGISTRY_MODULES = frozenset({"tools.backends", "tools.backends.registry"})


# --- what counts as the neutral core -------------------------------------------------------------
#
# The scope is `docs/BACKEND_BOUNDARY.md`'s scope, and `_EXCLUDED_PREFIXES` is that document's
# exclusion list — no more. Two files that might be expected here are simply never globbed rather
# than excluded: this instrument's own baseline (under `tools/tests/`) and `TODO.md`, which holds
# the migration ledger and would otherwise count the debt it describes.
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
    ("docs", "**/*.yaml"),
    ("skills", "**/*.md"),
    ("skills", "**/*.py"),
    (".", "README.md"),
    (".", "AGENTS.md"),
    (".", "CLAUDE.md"),
)

#: Out of scope by the rule. The three backend ROOTS are deliberately absent: a path under a
#: backend root is excused by `_is_backend_location`, which requires the placement table's shape,
#: and listing the roots here would short-circuit that check — which is how
#: `tools/backends/scratch.py` and `docs/backends/notes.md` came to be invisible. The consequence
#: is that `tools/backends/registry.py` and the package `__init__.py` files ARE scanned: they are
#: neutral infrastructure that names axis values, and naming is not knowing.
_EXCLUDED_PREFIXES = (
    # Design notes record decisions about a named technology (out of scope by the rule).
    "docs/design/",
    # Tests supply backend-shaped input in order to exercise backends (out of scope by the rule).
    "tools/tests/",
)

#: The fourth backend location from the placement table. It is not a repository-root prefix — the
#: rule puts a skill's backend fragments at `skills/<skill>/backends/<axis>/<id>.md` — so a prefix
#: list cannot express it, and for four review rounds it did not: the ledger's own `skills`
#: migration moved 191 occurrences into a directory the rule names as their home and the check
#: reported it as GROWTH IN THE NEUTRAL CORE, with a message telling the maintainer to move it
#: where it already was. Worse, that area's stated acceptance ("its baseline counts drop") was
#: unachievable, because the occurrences never left the scanned set.
#: The three roots under which a backend directory may sit, and the skill shape, as the
#: placement table spells them. Each requires `<axis>/<backend_id>` BENEATH the root: a prefix
#: test alone excused `tools/backends/scratch.py`, `docs/backends/notes.md` and
#: `skills/x/examples/backends/y.md` — none of which is a backend — so moving debt to a
#: malformed path under a backend root dropped the recorded figure and stayed green. Measured:
#: relocating a 76-token contract to `docs/backends/<flat file>` took the total 2584 -> 2508
#: with no backend created.
_BACKEND_ROOTS = ("tools/backends", "tools/prompt_templates/backends", "docs/backends")
_SKILL_BACKEND_SHAPE = re.compile(r"^skills/[^/]+/backends/([^/]+)/([^/]+)\.md$")


def _is_backend_location(rel: str, axes: frozenset[str] | None = None) -> bool:
    """Whether `rel` sits at one of the placement table's backend locations, in its right SHAPE.

    The axis segment must be a declared axis. The backend id is NOT required to be a registered
    member: a package is created before its `Backend` record in the documented procedure, and a
    check that refused the intermediate state would make the procedure unfollowable. What the
    shape does exclude is a file that is merely *under* a backend root — the excuse that let debt
    vanish into `docs/backends/notes.md`.
    """
    axes = axes if axes is not None else frozenset(registry.AXES)
    for root in _BACKEND_ROOTS:
        if rel.startswith(root + "/"):
            rest = rel[len(root) + 1:].split("/")
            return len(rest) >= 3 and rest[0] in axes
    match = _SKILL_BACKEND_SHAPE.match(rel)
    return bool(match) and match.group(1) in axes


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
            if _is_backend_location(rel):
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
    # Two lessons from probing this class one alternative at a time. No trailing `\b` after
    # `++`: a word boundary needs a word character on one side and `g++ ` has none, so
    # `\bg\+\+\b` could never match. And there is no `\bclang\+\+` alternative, because
    # `\bclang\b` already matches `clang++` — the `\b` between `g` and `+` exists — so it was
    # redundant, and a probe for `clang++ -c` was being killed by its sibling.
    "compiler-driver": r"\bgfortran\b|\bflang\b|\bg\+\+|\bgcc\b|\bclang\b",
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


def _is_module_path(dotted: str, root: Path) -> bool:
    """Whether `dotted` names a module or package file under `root`, decided by the FILESYSTEM.

    Deliberately not `importlib.util.find_spec`, which the first version used: `find_spec`
    IMPORTS every parent package, so one call to `direct_backend_imports()` executed 20
    neutral-core modules at import time, and any module-level failure in any of them surfaced
    here as a boundary-pin error with a message about backend imports. A reader described as
    static must not run the code it reads.
    """
    base = root.joinpath(*dotted.split("."))
    return base.with_suffix(".py").is_file() or (base / "__init__.py").is_file()


def _module_prefix(dotted: str, root: Path | None = None) -> str:
    """The longest prefix of `dotted` that names a real module, never shorter than an axis.

    `from ...fortran.signatures import SignatureParseError` names a SYMBOL, not a module, and
    the first version of this reader recorded the symbol. Two consequences, both demonstrated in
    review: importing one MORE symbol from an already-allowlisted module failed the pin, and
    re-spelling the identical crossing as `import signatures as _sig` failed it too — while the
    failure message said "the set of neutral-core MODULES importing a backend directly changed".
    Neither had crossed a boundary that was not already crossed. Collapsing to the module makes
    the recorded set what its name says it is, so the allowlist counts crossings rather than
    spellings.

    THE FLOOR IS THE POINT. Collapsing by existence alone made the pin blind to exactly the
    crossings the migration ledger is about: `tools.backends.build_system.make` has no directory
    yet, so every prefix down to `tools.backends` was tried and that one EXISTS — and it is in
    `REGISTRY_MODULES`, so the crossing was filtered out as if it were a registry call. Verified:
    a neutral module importing `tools.backends.build_system.make` passed the whole suite. The four
    unextracted axes were the blind set. A name under the backend package therefore never
    collapses below `tools.backends.<axis>`, whether or not that directory exists.
    """
    root = root or REPO_ROOT
    parts = dotted.split(".")
    backend_parts = BACKEND_PACKAGE.split(".")
    floor = 1
    if parts[:len(backend_parts)] == backend_parts and len(parts) > len(backend_parts):
        floor = len(backend_parts) + 1
    for stop in range(len(parts), floor - 1, -1):
        candidate = ".".join(parts[:stop])
        if _is_module_path(candidate, root):
            return candidate
    return ".".join(parts[:floor]) if floor > 1 else dotted


def _absolute_import_target(module: str | None, level: int, package: str) -> str | None:
    """Resolve a `from ... import` target to an absolute dotted name.

    `level` 0 is already absolute. A RELATIVE import is resolved against `package`, the dotted
    package the importing file lives in. The first version skipped relative imports outright,
    on the stated ground that "a relative import cannot leave a package, so it can never be a
    neutral-core module reaching into a backend". That premise is false in this tree: `tools/`
    is a PEP 420 namespace package that CONTAINS `tools/backends/`, so
    `from .backends.build_system.make import RULE` inside `tools/codegen_bundle.py` crosses the
    boundary without leaving the package. It was executed and it works.
    """
    ancestors = package.split(".") if package else []
    if level == 0:
        return module
    if level - 1 > len(ancestors):
        return None
    base = ancestors[: len(ancestors) - (level - 1)]
    return ".".join([*base, module]) if module else ".".join(base)


def _package_of(rel: str) -> str:
    """The dotted package a repo-relative `.py` path lives in (`tools/a/b.py` -> `tools.a`)."""
    parts = Path(rel).parts
    return ".".join(parts[:-1])


class UnparseableNeutralModule(Exception):
    """A neutral-core module the import pin could not parse.

    Raised rather than swallowed. Returning an empty set on `SyntaxError` made an unparseable
    module leave the pin silently — and a file that does not parse is precisely where an unread
    import would sit. The direction has to be loud: a module in scope is either read or the
    check fails.
    """


def _imported_modules(source: str, package: str = "") -> set[str]:
    """Every backend-package module a source reaches, as an absolute dotted module path.

    Four spellings are read: `import a.b`, `from a.b import c`, `from .relative import c`, and
    `importlib.import_module` with a STRING LITERAL argument — the last because it is the
    spelling `registry.load` itself uses, so a neutral-core module can copy it.

    WHAT THIS CANNOT SEE, so that the pin is not read as more than it is: a module name computed
    at runtime (an f-string, a concatenation, a name from a config file) is out of reach of any
    static reader. `docs/BACKEND_BOUNDARY.md` states the criterion as "imports, or names for
    import"; a computed name does neither at parse time. Over the four spellings above the answer
    is complete; beyond them it is silent, and an unparseable module raises instead of reading as
    clean.
    """
    names: set[str] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise UnparseableNeutralModule(str(exc)) from exc
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(_module_prefix(alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            target = _absolute_import_target(node.module, node.level, package)
            if not target:
                continue
            names.add(_module_prefix(target))
            names.update(_module_prefix(f"{target}.{alias.name}") for alias in node.names)
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
    """Per neutral-core Python module, the backend modules it imports outside the registry.

    `root` is honoured end to end — the package context of each file and the filesystem the
    module names resolve against both come from it. It used to be accepted and then ignored:
    `_module_prefix` resolved against `REPO_ROOT` regardless, so a synthetic tree's imports were
    answered by the real repository, and nothing drove it with a root to notice.
    """
    root = root or REPO_ROOT
    offenders: dict[str, list[str]] = {}
    for path in neutral_core_files(root):
        if path.suffix != ".py":
            continue
        rel = path.relative_to(root).as_posix()
        try:
            reached = _imported_modules(
                path.read_text(encoding="utf-8", errors="replace"), _package_of(rel))
        except UnparseableNeutralModule as exc:
            raise UnparseableNeutralModule(f"{rel}: {exc}") from exc
        hits = {
            name
            for name in reached
            if (name == BACKEND_PACKAGE or name.startswith(BACKEND_PACKAGE + "."))
            and name not in REGISTRY_MODULES
        }
        if hits:
            offenders[rel] = sorted(hits)
    return offenders


def measure(root: Path | None = None) -> dict[str, object]:
    """The regenerable half only. `direct_backend_imports` is deliberately absent — see
    `ALLOWLIST_PATH`."""
    return {"token_counts": token_counts(root)}


#: How many of the declared axis names on one line make it a restatement of the list. Four of five
#: rather than all five, so dropping one name is not an escape.
_AXIS_LIST_QUORUM = 4


def axis_list_restatements(root: Path) -> list[str]:
    """Every markdown line under `root` that enumerates the axis list, outside its one owner.

    Separator-agnostic by construction: it counts backticked axis NAMES on a line, not the commas
    or slashes between them. The first version of this guard read two files by name and matched
    one comma spelling — and the same branch then added two slash-separated restatements to
    `README.md` and `docs/README.md` and one to `TODO.md`, none of which it could see. Taking a
    `root` is what lets a synthetic tree drive it: with no violating file in this repository, a
    guard that only ever runs here reports success whether or not it looks at anything.
    """
    canonical = (root / "docs" / "BACKEND_BOUNDARY.md").resolve()
    offenders: list[str] = []
    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(root).as_posix()
        if rel.startswith((".git/", "workspace", "docs/design/")):
            continue
        if path.resolve() == canonical:
            continue
        for lineno, line in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            quoted = set(re.findall(r"`([a-z_]+)`", line)) & set(registry.AXES)
            if len(quoted) >= _AXIS_LIST_QUORUM:
                offenders.append(f"{rel}:{lineno}")
    return offenders


def _load_baseline() -> dict[str, object]:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def _load_pinned() -> dict[str, object]:
    return json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))


def _load_allowlist() -> dict[str, list[str]]:
    return _load_pinned()["direct_backend_imports"]


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


class ScopePinTests(unittest.TestCase):
    """The scanned set and the token-class list, pinned by hand rather than by measurement.

    Both were previously observable only through the regenerable baseline, and both failure
    messages tell the maintainer to regenerate. So narrowing the scope or deleting a token class
    failed once and then passed forever: adding `docs/workflow/` to the exclusions dropped 89
    recorded occurrences, deleting the `skills/**/*.md` glob dropped 13 files, and deleting a
    token class together with its probe cost nothing — each green after one regeneration. Living
    in `ALLOWLIST_PATH`, which no command writes, makes a change to either a reviewed hand edit.
    """

    def test_the_scanned_file_set_matches_the_pinned_list(self) -> None:
        recorded = list(_load_pinned()["scanned_files"])
        measured = [p.relative_to(REPO_ROOT).as_posix() for p in neutral_core_files()]
        self.assertEqual(recorded, measured,
                         "the scanned set changed. Widening it is the migration's job and "
                         "narrowing it is a scope decision; either way, edit "
                         "tools/tests/data/backend_boundary_allowlist.json by hand — "
                         "`--write-baseline` does not touch it.")

    def test_the_token_class_list_matches_the_pinned_list(self) -> None:
        self.assertEqual(sorted(_load_pinned()["token_classes"]), sorted(_TOKEN_CLASSES),
                         "a token class was added or removed. The probe table alone cannot "
                         "catch this — deleting a class WITH its probe is free — and two of the "
                         "four unextracted axes have a single class, so losing one loses an "
                         "axis' whole sample.")


class DirectImportPinTests(unittest.TestCase):
    """The pinned measure: which neutral-core modules bypass the registry, exactly."""

    def test_write_baseline_does_not_touch_the_pinned_file(self) -> None:
        """The write set of `_write_baseline`, observed rather than described.

        The commit that split the two halves is the fix for a laundering path, and its guard
        checked `measure()`'s keys — so appending an `ALLOWLIST_PATH.write_text(...)` to
        `_write_baseline` reinstated the laundering with the whole suite green. This runs the
        command and compares the pinned file's bytes.
        """
        before = ALLOWLIST_PATH.read_bytes()
        baseline_before = BASELINE_PATH.read_bytes()
        # A SENTINEL, not the file's own bytes. Comparing the bytes only catches a write whose
        # content differs, and the write that matters — recomputing the allowlist — produces
        # identical bytes on a clean tree and differing bytes exactly when a bypass has just been
        # added. The sentinel makes ANY write to this path visible, clean tree or not.
        sentinel = before + b"\n"
        try:
            ALLOWLIST_PATH.write_bytes(sentinel)
            _write_baseline()
            after = ALLOWLIST_PATH.read_bytes()
        finally:
            ALLOWLIST_PATH.write_bytes(before)
            BASELINE_PATH.write_bytes(baseline_before)
        self.assertEqual(sentinel, after,
                         "--write-baseline wrote to the hand-edited pin")

    def test_the_regenerable_half_cannot_carry_the_allowlist(self) -> None:
        # `--write-baseline` writes `measure()`. While `measure()` also returned the import set,
        # the command that the sampled check tells maintainers to run rewrote the pin too, and a
        # bypass added in the same commit was absorbed silently. Pinned on `measure()`'s keys
        # rather than on the file's, so re-adding it fails here and not two review rounds later.
        self.assertEqual({"token_counts"}, set(measure()))
        self.assertNotIn("direct_backend_imports", _load_baseline())
        self.assertNotEqual(BASELINE_PATH, ALLOWLIST_PATH)

    def test_direct_backend_imports_match_the_allowlist(self) -> None:
        recorded = _load_allowlist()
        measured = direct_backend_imports()
        # Equality, not containment, in both directions: a new bypass fails, and a bypass that was
        # removed must leave the allowlist in the same commit. This is the half of this module that
        # is a set identity rather than a sample.
        self.assertEqual(
            {k: sorted(v) for k, v in sorted(recorded.items())},
            {k: sorted(v) for k, v in sorted(measured.items())},
            "the set of neutral-core modules importing a backend directly changed. The rule "
            "(docs/BACKEND_BOUNDARY.md) is that the neutral core reaches a backend only through "
            "tools/backends/registry.py; tools/tests/data/backend_boundary_allowlist.json records "
            "the modules that do not yet. Adding to it is a boundary regression, removing from it "
            "is the migration. Edit that file BY HAND: no command writes it, precisely so that "
            "regenerating the sampled baseline cannot absorb a new bypass.")


class ScannedSetTests(unittest.TestCase):
    """The scanned set is driven against a synthetic tree, not against this repository.

    Every glob being RECURSIVE is a fix with no witness in this tree: `docs/` currently has only
    the subdirectories the globs happen to reach, so reverting `docs/**/*.md` to `docs/*.md`
    leaves the suite green. Review found that survivor. A file placed in a directory that does
    not exist here is the only way to observe the property, so `neutral_core_files` takes a
    `root` and these tests build one.
    """

    def _tree(self, tmp: Path, *relatives: str) -> None:
        for rel in relatives:
            path = tmp / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("subroutine placeholder\n", encoding="utf-8")

    def test_a_document_in_an_unforeseen_subdirectory_is_scanned(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self._tree(
                tmp,
                "docs/reference/moved_contract.md",
                "docs/a/b/c/deep.md",
                "docs/examples/pinned.yaml",
                "skills/some-skill/scripts/emit.py",
                "skills/some-skill/SKILL.md",
                "skills/some-skill/backends/language/fortran.md",
                "mcp_servers/tools/run_syntax_check.json",
                "tools/prompt_templates/leaf.txt",
                "README.md",
                "AGENTS.md",
                "CLAUDE.md",
            )
            found = {p.relative_to(tmp).as_posix() for p in neutral_core_files(tmp)}
        # `skills/<skill>/backends/<axis>/<id>.md` is the placement table's fourth backend
        # location and is NOT scanned — the ledger's `skills` migration moves knowledge there,
        # and a check that counted it as neutral core would report a correct migration as growth
        # and leave the area's "counts drop" acceptance unreachable.
        self.assertEqual(
            {"docs/reference/moved_contract.md", "docs/a/b/c/deep.md", "docs/examples/pinned.yaml",
             "skills/some-skill/scripts/emit.py", "skills/some-skill/SKILL.md",
             "mcp_servers/tools/run_syntax_check.json", "tools/prompt_templates/leaf.txt",
             "README.md", "AGENTS.md", "CLAUDE.md"},
            found)

    def test_the_axis_list_detector_flags_a_restatement_whatever_its_separators(self) -> None:
        """A positive control, because this repository contains no violating line.

        A guard with nothing to find reports success whether or not it looks: narrowing it back
        to two named files, or back to matching one comma spelling, both left the suite green.
        The three spellings below are the ones actually written on this branch — commas, slashes,
        and a table cell — plus the near-miss that must NOT be flagged.
        """
        import tempfile
        names = sorted(registry.AXES)
        commas = ", ".join(f"`{n}`" for n in names)
        slashes = " / ".join(f"`{n}`" for n in names)
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            (tmp / "docs").mkdir()
            (tmp / "docs" / "BACKEND_BOUNDARY.md").write_text(
                f"The declared axes are {commas}.\n", encoding="utf-8")
            (tmp / "README.md").write_text(f"| doc | knowledge of {slashes} |\n", encoding="utf-8")
            (tmp / "docs" / "README.md").write_text(
                f"10. BACKEND_BOUNDARY.md (a {slashes} model)\n", encoding="utf-8")
            (tmp / "TODO.md").write_text(f"a registry declaring {commas}\n", encoding="utf-8")
            # Four of the five names — the exact evasion `_AXIS_LIST_QUORUM` is chosen for, and
            # the reason it is 4 rather than 5. Setting the constant to 5 was free before this.
            four = ", ".join(f"`{n}`" for n in names[:4])
            (tmp / "docs" / "four_of_five.md").write_text(f"axes: {four}\n", encoding="utf-8")
            (tmp / "docs" / "innocent.md").write_text(
                f"the `{names[0]}` axis and the `{names[1]}` axis and `{names[2]}`\n",
                encoding="utf-8")
            (tmp / "docs" / "design").mkdir()
            (tmp / "docs" / "design" / "note.md").write_text(f"{commas}\n", encoding="utf-8")
            found = axis_list_restatements(tmp)
        self.assertEqual(
            ["README.md:1", "TODO.md:1", "docs/README.md:1", "docs/four_of_five.md:1"],
            found)

    def test_the_declared_exclusions_are_all_reachable(self) -> None:
        # An exclusion no glob can produce is dead text that reads as a rule. Two were, before
        # the globs became recursive.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self._tree(
                tmp,
                "tools/backends/language/fortran/lines.py",
                "tools/prompt_templates/backends/language/fortran/gen.txt",
                "docs/backends/language/fortran/abi.md",
                "skills/gen/backends/language/fortran.md",
                "docs/design/note.md",
                "tools/tests/test_thing.py",
                "docs/kept.md",
            )
            found = {p.relative_to(tmp).as_posix() for p in neutral_core_files(tmp)}
        self.assertEqual({"docs/kept.md"}, found)


class ImportReaderTests(unittest.TestCase):
    """The spellings `_imported_modules` claims to read, and the one it does not.

    The tree exercises only `import` and `from ... import` today, so the `importlib` branch
    survived a mutation that deleted it — an unexercised branch is a claim with no witness.
    These probes are that witness. They are SAMPLES of each spelling, not a definition of the
    set of ways Python can reach a module; the docstring above states where the reader stops.
    """

    def test_it_reads_every_declared_spelling(self) -> None:
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
                self.assertTrue(_is_module_path(name, REPO_ROOT),
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

    #: Per class: every string it MUST match, and every string it must NOT.
    #:
    #: ONE POSITIVE PER REGEX ALTERNATIVE, not one per class. An independent sweep removed
    #: `f2003`, `CFLAGS`, `LDFLAGS`, `g++` and `clang++` one at a time and the suite stayed
    #: green; their siblings died only because this corpus happens to contain those spellings
    #: today — accident, not coverage. Same for the `\s+` in the two-word patterns, which is the
    #: whole reason those are patterns rather than literals. The negative half is what stops a
    #: class from being widened into a catch-all to keep the positives green; two of the first
    #: negatives written here were wrong, which is the half doing work.
    _PROBES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
        "fortran": (("iso_fortran_env", "GFortran"), ("iso c binding",)),
        "fortran-suffix": ((" model.f90", "a.f95", "b.f03", "c.f08", "d.fpp"),
                           ("model.py", "model.f77")),
        "fortran-subroutine": (("end subroutine foo", "SUBROUTINE bar"), ("end procedure foo",)),
        "fortran-implicit-none": (("implicit none", "implicit    none", "implicit\tnone"),
                                  ("implicitly none", "implicitnone")),
        "fortran-intent": (("intent(in)", "intent (out)"), ("intention of", "intent in")),
        "fortran-module-procedure": (("module procedure add", "module   procedure add"),
                                     ("module parameter add", "moduleprocedure add")),
        "fortran-kind": (("real64", "real32"), ("real 64", "areal64")),
        "fortran-allocatable": (("allocatable :: x",), ("allocated :: x",)),
        "fortran-module-file": (("harness.mod",), ("harness.module",)),
        "fortran-standard": (("-std=f2008", "-std=f2003", "-std=f95"), ("-std=c99", "f2018")),
        "c-include": (("#include <stdio.h>",), ("include stdio",)),
        "c-suffix": (("kernel.cpp", "k.hpp", "k.cxx", "k.cc", "k.hh"), ("llama.cpp", "k.cpp2")),
        "make-control-file": (("src/Makefile", "GNUmakefile"), ("src/BUILD.bazel",)),
        "make-variable": (("FFLAGS += -O2", "CFLAGS += -O2", "LDFLAGS += -s", "OBJDIR := o"),
                          ("FLAGS += -O2", "MYFFLAGS")),
        "compiler-driver": (("gfortran -c", "flang -c", "g++ -c", "clang++ -c", "gcc -c",
                             "clang -c"), ("fortran compiler", "libgcc_s")),
        "compiler-syntax-only": (("-fsyntax-only",), ("--syntax-only",)),
        "linter-fortitude": (("fortitude check",), ("fortifying the gate",)),
        "parallel-directive": (("!$omp parallel do",), ("$omp parallel do",)),
        "parallel-construct": (("do concurrent (i=1:n)", "do  concurrent (i=1:n)"),
                               ("run these concurrently",)),
    }

    def test_every_declared_class_has_a_probe(self) -> None:
        self.assertEqual(sorted(_TOKEN_CLASSES), sorted(self._PROBES),
                         "a token class was added or removed without its probe pair")

    def test_each_class_matches_every_positive_and_rejects_every_negative(self) -> None:
        for name, (positives, negatives) in sorted(self._PROBES.items()):
            rx = _COMPILED[name]
            for probe in positives:
                self.assertTrue(rx.search(probe), f"{name} no longer matches {probe!r}")
            for probe in negatives:
                self.assertIsNone(rx.search(probe), f"{name} now matches {probe!r}")

    @staticmethod
    def _top_level_alternatives(pattern: str) -> list[str]:
        """Split on `|` at depth 0 only.

        A plain `str.split("|")` cut `\\breal(?:64|32)\\b` in half and produced two patterns
        that do not compile — the same class-of-splitter defect this repository fixed for Fortran
        commas (issue #23). Character classes and groups both hold their contents.
        """
        parts: list[str] = []
        buf: list[str] = []
        depth = 0
        in_class = False
        escaped = False
        for ch in pattern:
            if escaped:
                buf.append(ch)
                escaped = False
                continue
            if ch == "\\":
                buf.append(ch)
                escaped = True
                continue
            if in_class:
                buf.append(ch)
                if ch == "]":
                    in_class = False
                continue
            if ch == "[":
                in_class = True
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "|" and depth == 0:
                parts.append("".join(buf))
                buf = []
                continue
            buf.append(ch)
        parts.append("".join(buf))
        return parts

    def test_every_regex_alternative_is_necessary_and_probed(self) -> None:
        r"""Each top-level alternative must be the ONLY thing matching one of its probes.

        Counting probes against alternatives, which this replaced, promised necessity and checked
        arithmetic: `\bclang\+\+` is subsumed by `\bclang\b` — a word boundary does exist
        between `g` and `+` — so it could be deleted for free while the `clang++ -c` probe went
        on passing, killed by its sibling. Removing one alternative and requiring some positive
        to stop matching is the property the name claims.
        """
        for name, pattern in sorted(_TOKEN_CLASSES.items()):
            alternatives = self._top_level_alternatives(pattern)
            if len(alternatives) == 1:
                continue
            positives = self._PROBES[name][0]
            for index in range(len(alternatives)):
                reduced = "|".join(a for i, a in enumerate(alternatives) if i != index)
                rx = re.compile(reduced, re.IGNORECASE)
                self.assertTrue(
                    any(not rx.search(probe) for probe in positives),
                    f"{name}: alternative {alternatives[index]!r} is redundant or unprobed — "
                    "every positive still matches without it")

    def test_every_class_has_at_least_one_negative(self) -> None:
        # "The negative half is what stops a class from being widened into a catch-all" — a claim
        # nothing observed, since an empty negatives tuple passed.
        for name, (positives, negatives) in sorted(self._PROBES.items()):
            self.assertTrue(positives, f"{name}: no positive probe")
            self.assertTrue(negatives, f"{name}: no negative probe")


class RegistryConsistencyTests(unittest.TestCase):
    """The registry's own claims, checked against itself and against the import system."""

    def test_the_canonical_document_lists_exactly_the_declared_axes(self) -> None:
        """One owner for the axis list, compared rather than restated.

        Three documents spelled the five names and nothing checked them: adding a sixth axis to
        `AXES` left all three stale with the full suite green. `AGENTS.md` and `docs/GLOSSARY.md`
        now cite the canonical §Definitions bullet instead of repeating it, and that bullet is
        read here. The parse is deliberately narrow — the backticked names in the sentence that
        begins "The declared axes are" — so a rewrite that drops the list fails rather than
        silently matching nothing.
        """
        doc = (REPO_ROOT / "docs" / "BACKEND_BOUNDARY.md").read_text(encoding="utf-8")
        match = re.search(r"The\s+declared\s+axes\s+are\s+(.+?)\.\s", doc, re.DOTALL)
        self.assertIsNotNone(match, "docs/BACKEND_BOUNDARY.md no longer lists the declared axes")
        listed = set(re.findall(r"`([a-z_]+)`", match.group(1)))
        self.assertEqual(set(registry.AXES), listed)
        # And nowhere else may enumerate it. Checked over EVERY markdown file in the tree, with a
        # separator-agnostic rule (a line quoting four or more axis names), because the first
        # version of this guard read two named files for one comma spelling — and the same branch
        # then added two slash-separated restatements to `README.md` and `docs/README.md`, plus
        # one in `TODO.md`, none of which it could see. A guard that names its subjects is a
        # sample; this one enumerates the corpus.
        self.assertEqual(
            [], axis_list_restatements(REPO_ROOT),
            "these lines enumerate the axis list, which has one owner "
            "(docs/BACKEND_BOUNDARY.md §Definitions); cite that section instead")

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
        # An AST walk, not a substring count: the count was bound to one spelling, and a third
        # membership call written with single quotes (or `axis=`) passed the whole suite.
        membership_calls = [
            node for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and getattr(node.func, "attr", getattr(node.func, "id", None)) == "unsupported_reason"
            and any(isinstance(a, ast.Constant) and a.value == "language"
                    for a in [*node.args, *(k.value for k in node.keywords)])
        ]
        self.assertEqual(
            [], membership_calls,
            "a signature gate guards a Fortran-only renderer on membership alone "
            f"(line(s) {[n.lineno for n in membership_calls]})")
        self.assertEqual(
            2, source.count("unsupported = _signature_backend_refusal(language)"),
            "both infrastructure signature gates must go through the one refusal predicate")

    def test_the_signature_helper_backend_matches_what_the_module_imports(self) -> None:
        """`_SIGNATURE_HELPERS_BACKEND_ID` must name the backend the helpers actually import.

        A SET IDENTITY, not a sample: every `tools.backends.language.<id>` module the validator
        imports is collected from its source, and the set of `<id>`s must be exactly the one the
        constant names. Review reached the same fail-open twice by asking the registry a question
        whose answer did not constrain which module the helpers import — first with a member
        declared `module=None`, then with a member carrying a real module. The constant closes
        that only while it agrees with the imports, which is what this reads.
        """
        source = Path(vps.__file__).read_text(encoding="utf-8")
        prefix = f"{BACKEND_PACKAGE}.language."
        imported_language_backends = {
            name[len(prefix):].split(".")[0]
            for name in _imported_modules(source)
            if name.startswith(prefix)
        }
        self.assertEqual(
            {vps._SIGNATURE_HELPERS_BACKEND_ID}, imported_language_backends,
            "the validator imports a language backend the signature refusal does not name")
        # And the id it names has to be a real, extracted backend — a typo would silently refuse
        # every language including the one that works.
        self.assertIsNone(
            registry.unavailable_reason("language", vps._SIGNATURE_HELPERS_BACKEND_ID))

    def test_each_refusal_ground_is_reached_by_the_input_it_is_for(self) -> None:
        """Both grounds, separately. Only the second one was observed.

        The two grounds are coextensive while one language backend exists, so a mutation that
        deleted the registry ground left the suite green — and made the gate answer a
        NON-MEMBER language with the second ground's sentence, which says that language "has an
        extracted language backend". False, and it sends a reader to thread `language` through
        six helpers instead of to add a backend. The gate tests cannot catch this: they build
        their expected clause by calling this predicate, so they are invariant to which ground
        answered.
        """
        # The non-member is DERIVED, not the literal "c": registering a `c` language backend
        # would have silently converted this probe from the membership ground to the extraction
        # ground while both assertions still passed.
        non_member = next(
            c for c in ("c", "cpp", "rust", "zz_not_a_language")
            if registry.unsupported_reason("language", c) is not None)
        member_gap = vps._signature_backend_refusal(non_member)
        self.assertEqual(registry.unavailable_reason("language", non_member), member_gap)
        self.assertNotIn("still import", member_gap)
        original = vps._SIGNATURE_HELPERS_BACKEND_ID
        try:
            vps._SIGNATURE_HELPERS_BACKEND_ID = "some_other_language"
            dispatch_gap = vps._signature_backend_refusal("fortran")
        finally:
            vps._SIGNATURE_HELPERS_BACKEND_ID = original
        self.assertIn("still import", dispatch_gap)
        self.assertNotEqual(member_gap, dispatch_gap)

    def test_the_refusal_normalizes_the_language_it_is_given(self) -> None:
        # `registry.unavailable_reason` case-folds and strips; the identity comparison against
        # `_SIGNATURE_HELPERS_BACKEND_ID` is exact. Trusting the caller made the two halves
        # disagree about one string, and the direction is a false `Compile fail` on a valid node.
        for spelling in ("fortran", "Fortran", "FORTRAN", "  fortran  ", "\tFortran\n"):
            self.assertIsNone(vps._signature_backend_refusal(spelling), spelling)
        for absent in ("", "   ", None):
            self.assertIsNone(vps._signature_backend_refusal(absent), repr(absent))

    def test_the_registry_and_the_hard_coding_gates_agree_on_the_language_set(self) -> None:
        """Two owners of one fact, the same shape the linter test closes.

        `_validate_toolchain_backend_supported` spells `(make, fortran)` itself and the §5.1
        helpers import one backend by name, so declaring a second `language` member makes the
        registry accept a value those gates refuse — silently, since nothing compared them.
        Failing here is the intended outcome of adding a language backend: it says the gates in
        `docs/BACKEND_BOUNDARY.md` §Operations Rules must migrate in the same change.
        """
        self.assertEqual(
            (vps._SIGNATURE_HELPERS_BACKEND_ID,), registry.backend_ids("language"),
            "the registry declares a language the neutral gates still refuse by hard-coding; "
            "migrate those gates (docs/BACKEND_BOUNDARY.md, TODO.md) in the same change")

    def test_an_extracted_but_undispatched_language_is_still_refused(self) -> None:
        # The behavioural witness for the second ground. Simulated by moving the constant rather
        # than by registering a second backend, because the refusal must hold for ANY language
        # the helpers are not wired to, and that property does not depend on which one is.
        original = vps._SIGNATURE_HELPERS_BACKEND_ID
        try:
            vps._SIGNATURE_HELPERS_BACKEND_ID = "some_other_language"
            reason = vps._signature_backend_refusal("fortran")
            self.assertIsNotNone(
                reason, "a language the helpers do not import was accepted by the gates")
            self.assertIn("still import", reason)
        finally:
            vps._SIGNATURE_HELPERS_BACKEND_ID = original
        self.assertIsNone(vps._signature_backend_refusal("fortran"))
        self.assertIsNone(vps._signature_backend_refusal(""))

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

    def test_every_entry_point_refuses_one_input_the_same_way_with_a_message(self) -> None:
        """One input must not be two kinds of failure, and no refusal may be empty.

        A mutation sweep found this unpinned: `get` built its exception as
        ``unsupported_reason(...) or ""`` and raised the EMPTY STRING for a value an
        `open_vocabulary` axis accepts but has no record for, while `require_available` raised
        `BackendNotExtracted` with a full message for the identical input. An operator reading
        `UnsupportedBackend: ` has nothing to act on.
        """
        cases = [
            # (axis, value, expected exception) — one per axis kind, chosen from the axis flag
            # rather than hard-coded, so a change of which axis is open cannot make this vacuous.
            *[(axis, "no_such_backend",
               registry.BackendNotExtracted if registry.AXES[axis].open_vocabulary
               else registry.UnsupportedBackend)
              for axis in registry.AXES],
        ]
        for axis, value, expected in cases:
            for call in (lambda a=axis, v=value: registry.get(a, v),
                         lambda a=axis, v=value: registry.load(a, v),
                         lambda a=axis, v=value: registry.require_available(a, v)):
                with self.assertRaises(expected) as caught:
                    call()
                message = str(caught.exception)
                self.assertTrue(message.strip(), f"{axis}/{value}: refused with an empty message")
                self.assertIn(value, message)
                self.assertIn("tools/backends/", message)

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
          f"{len(data['token_counts'])} files, {total} sampled occurrences "
          f"(the direct-import allowlist is NOT written by this command; "
          f"{len(_load_allowlist())} modules recorded, edit "
          f"{ALLOWLIST_PATH.relative_to(REPO_ROOT)} by hand)")


if __name__ == "__main__":
    if "--write-baseline" in sys.argv:
        _write_baseline()
    else:
        unittest.main()
