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
* **Pinned by hand, in `ALLOWLIST_PATH`, which no command writes**: the allowlist above and the
  instrument's REACH — its globs, its exclusions, its backend roots, and its token-class
  patterns. The allowlist shared a file with the sampled half for four review rounds and
  `--write-baseline` rewrote both, so a bypass added in any commit that also shed a sampled token
  was absorbed without a word. Reach was observable only through the regenerable baseline, whose
  failure message tells the maintainer to regenerate — so narrowing it failed once and passed
  forever after, at three successive levels: a glob, a token class, and then one ALTERNATIVE of a
  token class once the class names alone were pinned. The rules are pinned, not the file list
  they produce: pinning the produced list made adding an ordinary new module a scope failure,
  which is a false rejection on routine work.

WHAT THIS SUITE CANNOT WITNESS ABOUT ITSELF. An assertion cannot observe its own weakening — an
`assertEqual` relaxed to a containment, a necessity check relaxed to a tautology — because the
weakened form is what would run. Five decisions here are of that kind. They are covered by
external mutation runs, not by anything in this file, and are listed as a standing limit rather
than left to read as covered.
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
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import tools.validate_pipeline_semantics as vps  # noqa: E402
from tools import host_render  # noqa: E402
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
    # The committed leaf launch configuration (issue #63). In scope by
    # `docs/BACKEND_BOUNDARY.md` §Scope, which lists it; every other §Scope bullet
    # maps 1:1 to a glob here, and a token added under `leaf_config/` was unmeasured
    # while the document said it was in scope.
    ("leaf_config", "**/*.json"),
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
    # linter. One class per registered linter, because the migration ledger is per (axis, value)
    # and a single `linter-*` class would let a count fall in one backend while it rose in
    # another. Each is a bare name: what these count is the NAME, which the neutral core may use
    # as a preset key — the knowledge is what moved (TODO.md records that the counts do not fall
    # to zero for that reason).
    "linter-fortitude": r"fortitude",
    "linter-cppcheck": r"\bcppcheck\b",
    "linter-ruff": r"\bruff\b",
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


#: Callables that take a module name as a string and import it. `__import__` is here because it
#: is a builtin — no import statement announces it — and a literal argument to it is as static and
#: as executable as an `import` statement. What remains out of reach is an importer obtained
#: INDIRECTLY (`importlib.__dict__["import_module"]("...")`) and any computed name; both are
#: stated as limits rather than implied to be covered.
_IMPORTER_CALLS = frozenset({"import_module", "__import__"})


class UnparseableNeutralModule(Exception):
    """A neutral-core module the import pin could not parse.

    Raised rather than swallowed. Returning an empty set on `SyntaxError` made an unparseable
    module leave the pin silently — and a file that does not parse is precisely where an unread
    import would sit. The direction has to be loud: a module in scope is either read or the
    check fails.
    """


def _imported_modules(source: str, package: str = "", root: Path | None = None) -> set[str]:
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
            names.update(_module_prefix(alias.name, root) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            target = _absolute_import_target(node.module, node.level, package)
            if not target:
                continue
            names.add(_module_prefix(target, root))
            names.update(
                _module_prefix(f"{target}.{alias.name}", root) for alias in node.names)
        elif isinstance(node, ast.Call):
            func = node.func
            attr = func.attr if isinstance(func, ast.Attribute) else (
                func.id if isinstance(func, ast.Name) else None)
            if attr not in _IMPORTER_CALLS:
                continue
            # Positional OR keyword: `import_module(name="...")` and `__import__(name="...")`
            # are the same crossing as the positional spelling, and requiring `node.args` read
            # neither. The keyword is `name` for both callables.
            candidates = [*node.args, *(k.value for k in node.keywords if k.arg == "name")]
            for arg in candidates:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    names.add(_module_prefix(arg.value, root))
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
                path.read_text(encoding="utf-8", errors="replace"), _package_of(rel), root)
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


def _absent_cause(rel: str, scanned: set[str]) -> str:
    """Why a recorded file has no measured entry.

    `token_counts` omits a file with no hits, so an absent entry means EITHER the file left the
    scanned set OR it is still scanned and shed every sampled token. Naming only the first sent a
    maintainer hunting for a scope change that had not happened. Extracted so both branches can
    be driven directly: neither is reachable from this repository in a green state.
    """
    return "shed every sampled token" if rel in scanned else "left the scanned set"


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
        cls.scanned = {p.relative_to(REPO_ROOT).as_posix() for p in neutral_core_files()}

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

    def test_the_absent_file_message_names_the_right_cause(self) -> None:
        # Both branches driven directly: in a green tree neither is reachable, so the message a
        # maintainer acts on had no witness and named one cause for two situations.
        self.assertEqual("shed every sampled token", _absent_cause("docs/a.md", {"docs/a.md"}))
        self.assertEqual("left the scanned set", _absent_cause("docs/a.md", set()))

    def test_the_baseline_is_not_stale(self) -> None:
        # A ceiling that is never lowered stops being a ratchet. A file that shed backend spelling
        # — or left the scanned set entirely — must lower its recorded count in the same commit,
        # so the debt figure in TODO.md and this baseline cannot disagree.
        stale: list[str] = []
        for rel, counts in sorted(self.baseline.items()):
            measured = self.measured.get(rel)
            if measured is None:
                stale.append(f"{rel}: recorded, now absent "
                             f"({_absent_cause(rel, self.scanned)})")
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
    """The instrument's REACH, pinned by hand rather than observed through its own measurements.

    Reach was previously visible only through the regenerable baseline, whose failure message
    tells the maintainer to regenerate — so narrowing it failed once and passed forever after.
    Measured escapes: adding `docs/workflow/` to the exclusions dropped 89 recorded occurrences;
    deleting the `skills/**/*.md` glob dropped 13 files; deleting a token class with its probe
    cost nothing; and — one level below the first fix for this — deleting ONE ALTERNATIVE of a
    class with its probe dropped 13 more, because only the class NAMES were pinned.

    What is pinned is the RULES, not the file list they produce. Pinning the produced list made
    adding an ordinary new module a scope failure, which is a false rejection on routine work and
    the fastest way to teach people to regenerate without reading. A glob, an exclusion, a
    backend root or a token PATTERN changing is a reviewed hand edit; a new file matching the
    existing rules is free.
    """

    def test_the_scanning_rules_match_the_pinned_ones(self) -> None:
        pinned = _load_pinned()
        self.assertEqual([list(g) for g in pinned["scanned_globs"]],
                         [list(g) for g in _SCANNED_GLOBS], "a scan glob changed")
        self.assertEqual(list(pinned["excluded_prefixes"]), list(_EXCLUDED_PREFIXES),
                         "an exclusion prefix changed")
        self.assertEqual(list(pinned["backend_roots"]), list(_BACKEND_ROOTS),
                         "a backend root changed")
        self.assertEqual(pinned["skill_backend_shape"], _SKILL_BACKEND_SHAPE.pattern,
                         "the skill backend-location shape changed")

    def test_the_token_classes_match_the_pinned_patterns(self) -> None:
        # Patterns, not names. Pinning names alone left the alternative level open: dropping
        # `\bgcc\b` from `compiler-driver` together with its probe shed 13 occurrences, failed
        # once with the regenerate-me message, and passed forever after — while
        # `test_every_regex_alternative_is_necessary_and_probed` stayed green, since it only asks
        # that the SURVIVING alternatives be necessary.
        self.assertEqual(dict(_load_pinned()["token_classes"]), dict(_TOKEN_CLASSES),
                         "a token class or one of its alternatives changed. Edit "
                         "tools/tests/data/backend_boundary_allowlist.json by hand — "
                         "`--write-baseline` does not touch it.")


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
                # MALFORMED backend locations. Each is under a backend root or matches the skill
                # segment, and none is a backend: the axis segment is not a declared axis, or the
                # `<axis>/<backend_id>` depth is missing. They must be SCANNED — excusing them is
                # how debt came to vanish into `docs/backends/notes.md`, and requiring the axis
                # segment to be declared had no control until this list grew these rows.
                "docs/backends/notes.md",
                "docs/backends/not_an_axis/fortran/abi.md",
                "tools/backends/scratch.py",
                "skills/gen/examples/backends/language.md",
                "skills/gen/backends/not_an_axis/fortran.md",
            )
            found = {p.relative_to(tmp).as_posix() for p in neutral_core_files(tmp)}
        self.assertEqual(
            {"docs/kept.md", "docs/backends/notes.md", "docs/backends/not_an_axis/fortran/abi.md",
             "tools/backends/scratch.py", "skills/gen/examples/backends/language.md",
             "skills/gen/backends/not_an_axis/fortran.md"},
            found)


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

    def test_the_floor_keeps_an_unextracted_axis_visible(self) -> None:
        """`tools.backends.<axis>` must survive even when no such directory exists.

        Collapsing by existence alone walked `tools.backends.build_system.make` — an axis with
        no package yet — all the way down to `tools.backends`, which is a REGISTRY module and so
        filtered out. The blind set was the four unextracted axes, i.e. everything the migration
        ledger is about. The guard that fixes this had no test: removing the floor left the whole
        suite green.
        """
        for axis in registry.AXES:
            if any(registry.get(axis, b).extracted for b in registry.backend_ids(axis)):
                continue  # an extracted axis has a directory; the floor is not what saves it
            backend_id = registry.backend_ids(axis)[0]
            dotted = f"{BACKEND_PACKAGE}.{axis}.{backend_id}"
            self.assertEqual(f"{BACKEND_PACKAGE}.{axis}", _module_prefix(dotted))
            reached = _imported_modules(f"import {dotted}\n")
            self.assertTrue(
                reached - REGISTRY_MODULES,
                f"{dotted} collapsed into the registry package and vanished from the pin")

    def test_a_relative_import_into_a_backend_is_read(self) -> None:
        # `tools/` is a PEP 420 namespace package that CONTAINS `tools/backends/`, so a relative
        # import crosses the boundary without leaving the package. Skipping relative imports —
        # the behaviour this replaced — left the whole suite green with a working bypass in place.
        for package, source in (
            ("tools", "from .backends.build_system.make import RULE\n"),
            ("tools", "from .backends import language\n"),
            ("tools.hooks", "from ..backends.language.fortran import structure\n"),
        ):
            reached = {n for n in _imported_modules(source, package)
                       if n.startswith(BACKEND_PACKAGE)}
            self.assertTrue(reached, f"{package}: {source.strip()!r} was not read")
            self.assertTrue(reached - REGISTRY_MODULES, f"{package}: read only as the registry")
        # And the package context is what makes it resolve to the RIGHT name: with no package
        # the same statement yields `backends.…`, which is not under the backend package and so
        # never reaches the pin.
        self.assertEqual(
            set(),
            {n for n in _imported_modules("from .backends import language\n", "")
             if n.startswith(BACKEND_PACKAGE)})

    def test_an_unparseable_module_raises_instead_of_reading_as_clean(self) -> None:
        # A file that does not parse is exactly where an unread import would sit. Returning the
        # empty set let it leave the pin silently, and removing the raise left the suite green.
        with self.assertRaises(UnparseableNeutralModule):
            _imported_modules("import tools.backends.language.fortran.structure\ndef f(:\n")

    def test_the_importer_call_spellings_are_read(self) -> None:
        # `import_module(name=...)` required `node.args` and was unread; `__import__` is a
        # builtin, so no import statement announces it and a literal argument to it is as static
        # and as executable as an `import` statement.
        for source in (
            'import importlib\nimportlib.import_module(name="tools.backends.parallel.openmp")\n',
            '__import__("tools.backends.compiler.gfortran")\n',
            '__import__(name="tools.backends.linter.fortitude")\n',
        ):
            reached = {n for n in _imported_modules(source) if n.startswith(BACKEND_PACKAGE)}
            self.assertTrue(reached - REGISTRY_MODULES, f"unread: {source.strip()!r}")

    def test_an_indirectly_obtained_importer_is_out_of_reach_and_that_is_recorded(self) -> None:
        # Not a defect to fix — resolving this needs the value of an expression, which a static
        # reader does not have. Pinned so the documented limit stays true rather than becoming a
        # guess, and so a later claim that the pin is total has a failing test to argue with.
        source = ('import importlib\n'
                  'importlib.__dict__["import_module"]("tools.backends.parallel.openmp")\n')
        self.assertEqual(set(), {n for n in _imported_modules(source)
                                 if n.startswith(BACKEND_PACKAGE)})

    def test_the_reader_answers_from_the_root_it_is_given(self) -> None:
        """`root` must reach the filesystem lookup, not just the signature.

        It was accepted and ignored: `_module_prefix` resolved against `REPO_ROOT` regardless, so
        a synthetic tree's imports were answered by the real repository — and no test passed a
        root, so nothing noticed. Driven here against a tree that contains a DIFFERENT backend
        layout from this one.
        """
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            pkg = tmp / "tools" / "backends" / "language" / "zz_probe"
            pkg.mkdir(parents=True)
            (pkg / "__init__.py").write_text("", encoding="utf-8")
            dotted = f"{BACKEND_PACKAGE}.language.zz_probe.parts"
            self.assertEqual(f"{BACKEND_PACKAGE}.language.zz_probe",
                             _module_prefix(dotted, tmp))
            # The real repository has no such package, so answering from REPO_ROOT stops at the
            # axis floor — a different answer, which is what makes this a witness.
            self.assertEqual(f"{BACKEND_PACKAGE}.language", _module_prefix(dotted))

    def test_direct_backend_imports_is_driven_by_its_root(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            # A package that exists ONLY in the synthetic tree, so resolving against `REPO_ROOT`
            # gives a different answer (the axis floor) and the assertion discriminates. With a
            # tree whose layout matches this repository's, both readings agree and the test
            # cannot tell whether `root` was used.
            pkg = tmp / "tools" / "backends" / "language" / "zz_probe"
            pkg.mkdir(parents=True)
            (pkg / "__init__.py").write_text("", encoding="utf-8")
            (tmp / "tools" / "probe.py").write_text(
                "from .backends.language.zz_probe import parts\n"
                "from .backends.build_system.make import RULE\n", encoding="utf-8")
            self.assertEqual(
                {"tools/probe.py": [f"{BACKEND_PACKAGE}.build_system",
                                    f"{BACKEND_PACKAGE}.language.zz_probe"]},
                direct_backend_imports(tmp))

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
        "linter-cppcheck": (("cppcheck --enable=warning",), ("cppcheckers", "check cpp")),
        "linter-ruff": (("ruff check .",), ("gruff", "ruffle")),
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
        # declaring a second `language` member with `module=None` — the state most records are
        # in, and the state the ledger says is normal — silently stopped the signature gates
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

    def test_a_language_the_signature_helpers_do_not_serve_is_still_refused_by_them(self) -> None:
        """The one gate family that a registry answer cannot widen, stated as a rule.

        This test used to assert the language id SET was exactly the signature backend's, on the
        grounds that `_validate_toolchain_backend_supported` also spelled `(make, fortran)`
        itself. That gate now dispatches on registry capabilities, so the set equality was
        pinning a RESULT — registering a second language backend would fail it even after the
        gate had been migrated correctly. What survives is the actual constraint: the §5.1
        helpers import one backend by name and take no `language` argument, so any OTHER
        declared language must still be refused by the signature refusal, whatever the registry
        says about it. Failing here means a language was declared without that gate migrating.
        """
        for backend_id in registry.backend_ids("language"):
            refusal = vps._signature_backend_refusal(backend_id)
            if backend_id == vps._SIGNATURE_HELPERS_BACKEND_ID:
                self.assertIsNone(refusal, backend_id)
            else:
                self.assertIsNotNone(
                    refusal,
                    f"language '{backend_id}' is declared but the §5.1 helpers still import "
                    f"'{vps._SIGNATURE_HELPERS_BACKEND_ID}' by name; migrate that gate "
                    "(docs/BACKEND_BOUNDARY.md, TODO.md) in the same change")

    # --- the capability question ------------------------------------------------------------

    def test_the_four_questions_answer_differently_for_the_three_states(self) -> None:
        """Membership / implementation / capability / extraction, driven apart on one axis.

        Three synthetic records, because the live registry cannot show the interesting state:
        every declared member today is implemented, so the row that matters — REGISTERED AND
        IMPLEMENTED NOWHERE — has no witness in the tree. That row is the fail-closed default
        this design exists for: `unsupported_reason` says the value is known, and the other three
        still refuse it.
        """
        axis = "build_system"
        records = {
            (axis, "zz_extracted"): registry.Backend(
                axis, "zz_extracted", "tools.backends.language.fortran",
                core_provides=frozenset({"control_file"})),
            (axis, "zz_inlined"): registry.Backend(
                axis, "zz_inlined", None, core_provides=frozenset({"control_file"})),
            (axis, "zz_declared_only"): registry.Backend(axis, "zz_declared_only", None),
        }
        with mock.patch.dict(registry._BACKENDS, records):
            for backend_id in ("zz_extracted", "zz_inlined", "zz_declared_only"):
                self.assertIsNone(registry.unsupported_reason(axis, backend_id), backend_id)
            # implemented — extracted or with an inlined capability
            self.assertIsNone(registry.unimplemented_reason(axis, "zz_extracted"))
            self.assertIsNone(registry.unimplemented_reason(axis, "zz_inlined"))
            declared_only = registry.unimplemented_reason(axis, "zz_declared_only")
            self.assertIsNotNone(declared_only)
            self.assertIn("nothing implements it", declared_only)
            with self.assertRaises(registry.BackendNotExtracted):
                registry.require_implemented(axis, "zz_declared_only")
            registry.require_implemented(axis, "zz_inlined")
            # capability — independent of where the code lives
            self.assertTrue(registry.provides(axis, "zz_inlined", "control_file"))
            self.assertFalse(registry.provides(axis, "zz_declared_only", "control_file"))
            self.assertFalse(registry.provides(axis, "zz_inlined", "build_execute"))
            self.assertIsNone(
                registry.missing_capability_reason(axis, "zz_inlined", "control_file"))
            missing = registry.missing_capability_reason(
                axis, "zz_declared_only", "control_file")
            self.assertIsNotNone(missing)
            for expected in ("zz_declared_only", "control_file", axis,
                             "tools/backends/registry.py"):
                self.assertIn(expected, missing)
            # extraction — the question with the narrowest yes
            self.assertIsNone(registry.unavailable_reason(axis, "zz_extracted"))
            for unextracted in ("zz_inlined", "zz_declared_only"):
                self.assertIsNotNone(registry.unavailable_reason(axis, unextracted), unextracted)

    def test_every_capability_refuses_every_axis_it_is_not_a_question_of(self) -> None:
        # Exhaustive over `CAPABILITIES × AXES`, replacing three hand-picked pairs. A census
        # measured that widening any capability's axis tuple — so `provides` answers False
        # instead of raising for a mis-asked question — is invisible to the suite for the three
        # pairs nobody happened to pick.
        for capability, (axes, _description) in registry.CAPABILITIES.items():
            for axis in registry.AXES:
                value = registry.backend_ids(axis)[0]
                if axis in axes:
                    self.assertIsInstance(registry.provides(axis, value, capability), bool)
                    continue
                with self.assertRaises(registry.UnsupportedBackend, msg=(axis, capability)):
                    registry.provides(axis, value, capability)

    def test_each_capability_names_the_same_declarers_it_did(self) -> None:
        """The declarer SET per capability, not merely that it is non-empty.

        `test_every_capability_is_declared_by_a_record_and_described` compares the union, so a
        capability declared by two records survives losing one of them — measured for
        `parallel_directives`, which `openmp` and `none` both declare. Asserted as a set, and
        derived from the records rather than written out, so registering a backend that declares
        an existing capability does not fail this: what fails is a declaration silently
        DISAPPEARING from a record that had it.
        """
        # `provided`, the UNION: a capability that has moved into a backend package is still
        # declared for that value, and `provides` — the question this test is about — still
        # answers True for it. Reading `core_provides` alone would report a capability as
        # undeclared on the commit that finishes its migration, which is backwards.
        declarers = {
            capability: {f"{b.axis}/{b.backend_id}" for b in registry._BACKENDS.values()
                         if capability in b.provided}
            for capability in registry.CAPABILITIES
        }
        # Every declared capability has at least one declarer per axis it is a question of,
        # which is the property the union test cannot see once a second declarer exists.
        for capability, (axes, _description) in registry.CAPABILITIES.items():
            for axis in axes:
                self.assertTrue(
                    any(d.startswith(f"{axis}/") for d in declarers[capability]),
                    f"'{capability}' is a question of the {axis} axis and no {axis} record "
                    f"declares it, so `provides` answers False for every value of that axis")

    def test_a_capability_question_is_normalized_and_refused_when_it_is_a_typo(self) -> None:
        # The value normalizes (the gates rely on it); the CAPABILITY does not fall back. A
        # misspelled capability answering False would turn a host-authorship dispatch off
        # silently — the same authorship flip a padded axis value used to cause — so it raises.
        self.assertTrue(registry.provides("build_system", "  MAKE  ", "control_file"))
        for axis, capability in (
            ("build_system", "control-file"),      # wrong spelling
            ("build_system", "runner_render"),     # a real capability, wrong axis
            ("language", "build_execute"),         # likewise
        ):
            with self.assertRaises(registry.UnsupportedBackend):
                registry.provides(axis, "make", capability)
            with self.assertRaises(registry.UnsupportedBackend):
                registry.missing_capability_reason(axis, "make", capability)

    def test_a_none_value_does_not_collide_with_the_backend_named_none(self) -> None:
        """`provides`'s `or ""` guard, which a census showed nothing observed.

        Without it, `str(None).lower()` is the string `"none"` — which is a real backend id on
        the `parallel` axis — so a caller passing `None` (an absent axis value) would get the
        `parallel/none` record's answer instead of a refusal. Measured: deleting the guard
        leaves the suite green, and `provides("parallel", None, "parallel_directives")` flips
        from False to True. The collision is specific to this repository's own id, which is why
        it reads as harmless and is not.
        """
        self.assertIn("none", registry.backend_ids("parallel"), "the collision id is live")
        for absent in (None, "", "   "):
            self.assertFalse(
                registry.provides("parallel", absent, "parallel_directives"), repr(absent))
            self.assertIsNotNone(registry.unimplemented_reason("parallel", absent), repr(absent))

    def test_a_value_with_no_record_provides_nothing(self) -> None:
        # Including on the open-vocabulary axis, where membership answers permissively: an
        # unlisted token is accepted as a value but the host has no code for it, so a dispatch
        # asking `provides` declines instead of rendering something it does not know.
        self.assertIsNone(registry.unsupported_reason("parallel", "openmp_tasks"))
        self.assertFalse(registry.provides("parallel", "openmp_tasks", "parallel_directives"))
        self.assertFalse(registry.provides("build_system", "no_such_backend", "control_file"))

    def test_the_declarations_are_checked_and_the_check_can_fail(self) -> None:
        # `_check_declarations` runs at import, so in a green tree it is invisible: both
        # branches are driven here, or the guard is asserted and never observed.
        registry._check_declarations()  # the live declarations pass
        for bad in (
            registry.Backend("build_system", "zz", None, core_provides=frozenset({"no_such"})),
            registry.Backend("build_system", "zz", None,
                             core_provides=frozenset({"runner_render"})),
        ):
            with mock.patch.dict(registry._BACKENDS, {("build_system", "zz"): bad}):
                with self.assertRaises(registry.UnsupportedBackend):
                    registry._check_declarations()

    def test_the_declaration_check_is_actually_invoked_at_import(self) -> None:
        """The guard was pinned; its INVOCATION was not.

        Review deleted the module-level `_check_declarations()` call and the whole suite stayed
        green — the only test called the function directly, so it proved the guard works and
        nothing proved it runs. Read from the source because that is where the fact lives: a
        behavioural probe would have to re-import the module with a bad declaration, and the
        declarations are built at import from literals, so there is nothing to patch first.
        """
        tree = ast.parse(Path(registry.__file__).read_text(encoding="utf-8"))
        invocations = [
            node.lineno for node in tree.body
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
            and getattr(node.value.func, "id", None) == "_check_declarations"
        ]
        self.assertEqual(
            1, len(invocations),
            "tools/backends/registry.py must call _check_declarations() at module level exactly "
            "once; without it a misspelled capability answers False forever and a host-authorship "
            "dispatch turns off silently (docs/BACKEND_BOUNDARY.md §Operations Rules)")

    def test_each_capability_is_dispatched_on_exactly_where_it_says_it_is(self) -> None:
        """Which capabilities a gate actually asks about, compared against what they claim.

        THE THIRD ATTEMPT at this pin, and the first two are worth stating because both were
        wrong in the same direction — they forbade correct work:

        1. "every live record must be implemented" — refused `Backend("build_system", "cmake",
           None)`, i.e. registering a member before writing its backend, which this registry
           documents as the fail-closed default.
        2. "every axis has at least one implemented backend" — refused the whole documented
           three-step "Adding an axis" procedure (`docs/BACKEND_BOUNDARY.md`), since a brand-new
           axis has nothing implemented yet and there is no capability to declare for it.

        What I was actually reaching for is this: a capability that some gate dispatches on must
        keep having that dispatch, and a capability that nothing asks about must say so rather
        than read as a live rule. `DISPATCHED` below is the claim; the neutral-core source is the
        evidence. Deleting a `provides(...)` call fails here; adding a new capability without a
        caller fails here until it is declared declaration-only; and none of it constrains what
        anyone registers.
        """
        dispatched = {"control_file", "build_execute", "runner_render", "lint"}
        # `lint` joined them when the first linter's argv moved into its package (issue #111):
        # `mcp_servers/build_runtime_server.py`'s `_lint_preset_command` asks `capability_module`
        # for it. Note the asymmetry the instrument's own comment below records — the conductor's
        # `{"lint": ...}` dict key is NOT what makes it dispatched, and never was.
        #
        # `lint` reached every linter that HAS an argv when issue #120 moved `cppcheck` and
        # `ruff` too; `mixed` is the one linter record still answering from `core_provides`, and
        # it has no argv of its own to move (it is a composite).
        #
        # The rest are declaration-only TODAY: they are how their records answer `implemented`,
        # and they gain a dispatch when their ledger area lands (the compiler adapters and the
        # parallel knobs are still inlined in the neutral core).
        declaration_only = set(registry.CAPABILITIES) - dispatched
        asked: set[str] = set()
        registry_path = Path(registry.__file__).resolve()
        for path in neutral_core_files():
            # The registry is where the capabilities are DEFINED and where `provides` itself
            # lives, so scanning it would report every capability as asked — including the ones
            # whose only mention is their own declaration.
            if path.suffix != ".py" or path.resolve() == registry_path:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            calls = [
                node for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and getattr(node.func, "attr", getattr(node.func, "id", None))
                in ("provides", "missing_capability_reason", "capability_module")
            ]
            if not calls:
                continue
            # Two shapes, and only these two — the instrument was wrong twice before settling
            # here, in both directions. Scanning call arguments alone missed `build_execute`,
            # which `_missing_toolchain_capability_clauses` passes through a tuple it loops over;
            # scanning every capability-named string in the file instead picked up an unrelated
            # `{"lint": ...}` dict key in the conductor. So: a direct argument to the call, or an
            # element of a sequence literal that also names an axis — which is the
            # `(axis, value, capability)` row those loops are built from.
            for call in calls:
                asked |= {a.value for a in call.args
                          if isinstance(a, ast.Constant) and a.value in registry.CAPABILITIES}
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Tuple, ast.List)):
                    continue
                literals = {e.value for e in node.elts if isinstance(e, ast.Constant)}
                if literals & set(registry.AXES):
                    asked |= literals & set(registry.CAPABILITIES)
        self.assertEqual(
            dispatched, asked & set(registry.CAPABILITIES),
            "the set of capabilities the neutral core dispatches on changed: a dispatch was "
            "deleted, or a capability gained one without being moved out of the "
            "declaration-only group here")
        self.assertEqual(
            set(), asked & declaration_only,
            "a capability listed as declaration-only is now dispatched on")

    def test_implemented_backend_ids_excludes_a_record_with_no_implementation(self) -> None:
        # The filter is a no-op over today's declarations (every live record is implemented, by
        # the test above), so replacing it with `backend_ids` survives the suite. Driven with a
        # synthetic record, or the narrowing this function exists for has no witness at all.
        record = registry.Backend("linter", "zz_named_only", None)
        with mock.patch.dict(registry._BACKENDS, {("linter", "zz_named_only"): record}):
            self.assertIn("zz_named_only", registry.backend_ids("linter"))
            self.assertNotIn("zz_named_only", registry.implemented_backend_ids("linter"))

    def test_the_membership_refusal_says_declared_and_lists_the_declared_set(self) -> None:
        """Wording, pinned — because this string reaches a leaf verbatim.

        The signature gates carry `unsupported_reason`'s clause into a leaf-facing violation. It
        used to say "is not an implemented {axis} backend (implemented: …)" and list every
        member, including ones nothing implements: once `implemented` became a distinct question
        with its own function, that sentence pointed a leaf at values that cannot run. Both
        halves of the correction — the word and the set — survived the full suite when reverted,
        so neither was witnessed by anything.
        """
        reason = registry.unsupported_reason("build_system", "no_such_backend")
        self.assertIsNotNone(reason)
        self.assertIn("is not a declared build_system backend", reason)
        self.assertNotIn("implemented", reason)
        for declared in registry.backend_ids("build_system"):
            self.assertIn(declared, reason)

    def test_a_refusal_clause_names_the_values_that_do_implement_the_capability(self) -> None:
        # Both reviewers' sweeps deleted the implemented-set half of the clause and no test
        # noticed: the gate tests build their expected string by calling this same function, so
        # they are invariant to what it says. An author who is told only "not implemented" has
        # to go read the registry to find out what is.
        # The refused value must NOT contain an implemented id as a substring, or the assertion
        # holds from the "not '<value>'" half alone. The first version of this test probed
        # `cmake` against an implemented set of exactly `{make}` and was vacuous for that reason
        # — both round-2 reviewers found it independently, and a mutation that deleted the
        # implemented-set half while keeping the surrounding phrase survived the whole suite.
        for axis, refused in (("build_system", "ninja"), ("language", "cpp")):
            able = [b for b in registry.backend_ids(axis)
                    if registry.provides(axis, b, "control_file")]
            self.assertTrue(able, axis)
            for name in able:
                self.assertNotIn(name, refused, "probe value must not contain an implemented id")
            reason = registry.missing_capability_reason(axis, refused, "control_file")
            self.assertIsNotNone(reason)
            for name in able:
                self.assertIn(name, reason)

    def test_every_capability_is_declared_by_a_record_and_described(self) -> None:
        # A capability nothing declares is a question whose answer is always False — a dispatch
        # keyed on it is dead code that reads as a live rule.
        declared = {c for b in registry._BACKENDS.values() for c in b.provided}
        self.assertEqual(set(registry.CAPABILITIES), declared)
        for capability, (axes, description) in registry.CAPABILITIES.items():
            self.assertTrue(axes, capability)
            self.assertTrue(set(axes) <= set(registry.AXES), capability)
            self.assertTrue(description.strip(), capability)

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
            # The refusal says the value has no record and names both routes to giving it one.
            # It used to say only "no backend package", which is the extraction remedy — right
            # for this question and wrong for `unimplemented_reason`, which shares the same
            # message and asks whether the value can run at all.
            self.assertIn("has no record for it", reason)
            self.assertIn("tools/backends/parallel/", reason)
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

    def test_the_lint_gate_keeps_no_preset_list_of_its_own(self) -> None:
        """One owner, checked at the only place a second one could appear.

        This test used to compare the registry's linter ids against `vps._LINT_ALLOWED_PRESETS`,
        which was the second owner: comparing two copies keeps them equal but does not remove
        the copy, and the gate's refusal still spelled its own set instead of carrying the
        registry's clause. The set is gone and the gate asks `unimplemented_reason` per value, so
        what is left to pin is that no new list appears — read from the source, since a list that
        exists but is never consulted would pass any behavioural probe.
        """
        source = Path(vps.__file__).read_text(encoding="utf-8")
        linter_ids = set(registry.backend_ids("linter"))
        # SEQUENCE literals only. Extending this to dict VALUES was tried and reverted: it flags
        # `_LINT_PRESET_FOR_LANGUAGE`, which is a legitimate structure carrying a different fact
        # (which linter a language is linted with), so the guard would have refused correct code
        # and taught the reader to route around it. That mapping's drift risk is real and is
        # closed by the containment test below — the right instrument for a mapping is what its
        # values must satisfy, not whether it exists.
        literals = [
            node.lineno for node in ast.walk(ast.parse(source))
            if isinstance(node, (ast.Set, ast.List, ast.Tuple))
            and {getattr(e, "value", None) for e in node.elts} >= linter_ids
        ]
        self.assertEqual(
            [], literals,
            "a collection in the validator enumerates the linter backends; ask "
            f"registry.unimplemented_reason instead (line(s) {literals})")

    def test_every_implemented_linter_can_be_inferred_from_a_logged_command(self) -> None:
        """The pair this change opened by widening one side of it.

        The lint evidence gate now accepts any IMPLEMENTED linter (it asks the registry), but the
        next check in the same loop infers the preset from the logged command via a hard-coded
        chain of executable names. Registering a fifth linter therefore passes the registry check
        and then fails with "logged command does not match preset (inferred None)", which names
        neither the cause nor the fix. `mixed` is excluded because it is not an executable: the
        branch above this one refuses it and asks for separate entries per real linter.
        """
        for backend_id in registry.implemented_backend_ids("linter"):
            if backend_id == "mixed":
                continue
            self.assertEqual(
                backend_id, vps._infer_run_linter_preset_from_command([backend_id, "check"]),
                f"the lint gate accepts preset '{backend_id}' but cannot infer it from a logged "
                f"command, so the evidence check refuses it for an unrelated-sounding reason")

    def test_the_language_to_linter_mapping_cannot_drift_from_the_registry(self) -> None:
        """The other half of the same fact, which the guard above deliberately allows.

        `_LINT_PRESET_FOR_LANGUAGE` maps a language to the linter it is linted with. That is
        language knowledge, not a copy of the accepted-preset set, so it stays — but its VALUES
        are linter backend ids, and review measured the drift: dropping the `ruff` member from
        the registry leaves this mapping producing `ruff` for `python` while the gate refuses
        it, suite green. Pinned as a containment (the mapping may name fewer linters than exist,
        never one that does not), because equality would fail the day a linter is registered
        before any language uses it.
        """
        implemented = set(registry.implemented_backend_ids("linter"))
        used = set(vps._LINT_PRESET_FOR_LANGUAGE.values())
        self.assertEqual(
            set(), used - implemented,
            "the language->linter mapping names a linter the registry does not implement, so "
            "the lint evidence gate will refuse the preset this mapping produces")

    def test_a_registered_backend_module_lives_under_the_backend_package(self) -> None:
        for axis in registry.AXES:
            for backend_id in registry.backend_ids(axis):
                module = registry.get(axis, backend_id).module
                if module is None:
                    continue
                self.assertEqual(module, f"{BACKEND_PACKAGE}.{axis}.{backend_id}")


class CapabilityOwnershipTests(unittest.TestCase):
    """The two capability sets, and the dispatch that reaches the second one.

    `core_provides` says a job is still inlined in the neutral core; `backend_provides` says the
    record's own package does it. Every guard below was reverted and measured: without it the
    suite stays green while the registry can describe a state that cannot exist, or hand a seam a
    backend that never claimed the work.
    """

    def _patched(self, record: "registry.Backend"):
        return mock.patch.dict(registry._BACKENDS, {(record.axis, record.backend_id): record})

    def test_a_package_capability_requires_a_package(self) -> None:
        # W1. `module=None` with `backend_provides` is "the package implementation in the package
        # that does not exist". Nothing else refuses it: `provides` would answer True and
        # `capability_module` would then raise on a value the registry called implemented.
        record = registry.Backend(
            "language", "zz_no_pkg", None, backend_provides=frozenset({"runner_render"}))
        with self._patched(record):
            with self.assertRaises(registry.UnsupportedBackend) as ctx:
                registry._check_declarations()
        self.assertIn("no backend package", str(ctx.exception))

    def test_a_capability_may_not_be_owned_twice(self) -> None:
        # W2. Both sets naming one capability leaves no answer to WHICH implementation runs —
        # the ambiguity the migration removes, reintroduced by a declaration.
        record = registry.Backend(
            "language", "zz_both", "tools.backends.language.fortran",
            core_provides=frozenset({"runner_render"}),
            backend_provides=frozenset({"runner_render"}))
        with self._patched(record):
            with self.assertRaises(registry.UnsupportedBackend) as ctx:
                registry._check_declarations()
        self.assertIn("BOTH", str(ctx.exception))

    def test_a_package_capability_needs_a_place_to_be_reached(self) -> None:
        # W1b. A `backend_provides` entry with no `CAPABILITY_MODULE_ATTR` row is a capability
        # that is declared true and unreachable at the same time.
        # The capability is SYNTHESISED — declared, of this axis, and deliberately absent from
        # `CAPABILITY_MODULE_ATTR` — rather than borrowed from the live table. Every live
        # capability without a row is one the ledger intends to migrate, and migrating it would
        # turn this probe into a false failure whose message ("UnsupportedBackend not raised")
        # names nothing for the author who tripped it.
        record = registry.Backend(
            "language", "zz_unreachable", "tools.backends.language.fortran",
            backend_provides=frozenset({"zz_rowless"}))
        with mock.patch.dict(
                registry.CAPABILITIES, {"zz_rowless": (("language",), "a synthetic job")}), \
                self._patched(record):
            with self.assertRaises(registry.UnsupportedBackend) as ctx:
                registry._check_declarations()
        self.assertIn("CAPABILITY_MODULE_ATTR", str(ctx.exception))

    def test_provides_is_the_union_and_not_extraction(self) -> None:
        # W3. Two ways to get `provides` wrong, and each needs its own record. Simplifying it to
        # "the record is extracted" would answer True for a package that does not do this job —
        # the authorship flip the predicate exists to prevent.
        pkg_only = registry.Backend(
            "language", "zz_pkg_only", "tools.backends.language.fortran",
            backend_provides=frozenset({"runner_render"}))
        with self._patched(pkg_only):
            self.assertTrue(registry.provides("language", "zz_pkg_only", "runner_render"))
        extracted_mute = registry.Backend(
            "language", "zz_mute", "tools.backends.language.fortran")
        with self._patched(extracted_mute):
            self.assertIsNone(registry.unavailable_reason("language", "zz_mute"))
            self.assertFalse(registry.provides("language", "zz_mute", "runner_render"))

    def test_capability_module_refuses_a_backend_that_never_claimed_the_job(self) -> None:
        # W5. Extraction is not a claim. `load` would hand this package straight back — and it
        # HAS a `runner` module, so the seam would render Fortran for a value whose record says
        # nothing about rendering.
        record = registry.Backend("language", "zz_mute", "tools.backends.language.fortran")
        with self._patched(record):
            with self.assertRaises(registry.BackendNotExtracted) as ctx:
                registry.capability_module("language", "zz_mute", "runner_render")
            self.assertIsNotNone(registry.load("language", "zz_mute"))  # `load` does not refuse
        self.assertIn("does not implement", str(ctx.exception))

    def test_capability_module_refuses_a_package_that_does_not_carry_it(self) -> None:
        # W5b. The declaration and the tree disagreeing the other way: the record claims the job,
        # the package has no such module. Returning the package anyway would defer the failure to
        # a missing attribute inside a seam, where it reads as a render bug.
        record = registry.Backend(
            "language", "zz_liar", "tools.backends", backend_provides=frozenset({"runner_render"}))
        with self._patched(record):
            with self.assertRaises(registry.BackendNotExtracted) as ctx:
                registry.capability_module("language", "zz_liar", "runner_render")
        self.assertIn("re-exports no", str(ctx.exception))

    def test_a_package_capability_is_name_and_axis_checked_like_a_core_one(self) -> None:
        # W1c. `_check_declarations` walked `core_provides` before the two sets existed, and
        # reverting it to that survives the whole suite: a `backend_provides` entry naming a
        # capability that does not exist, or one belonging to another axis, reached NO check —
        # the CAPABILITY_MODULE_ATTR guard below it fires on a different ground and with a
        # different message, so it LOOKS like coverage. Both grounds are asserted by message.
        unknown = registry.Backend(
            "language", "zz_unknown_cap", "tools.backends.language.fortran",
            backend_provides=frozenset({"zz_not_a_capability"}))
        with self._patched(unknown):
            with self.assertRaises(registry.UnsupportedBackend) as ctx:
                registry._check_declarations()
        self.assertIn("unknown capability", str(ctx.exception))
        # `lint` IS a capability — of the `linter` axis. Declared on a `language` record it must
        # be refused as a wrong-axis question, not as a missing reach convention.
        wrong_axis = registry.Backend(
            "language", "zz_wrong_axis", "tools.backends.language.fortran",
            backend_provides=frozenset({"lint"}))
        with self._patched(wrong_axis):
            with self.assertRaises(registry.UnsupportedBackend) as ctx:
                registry._check_declarations()
        self.assertIn("axis only", str(ctx.exception))

    def test_the_refusal_names_the_value_that_does_implement_the_capability(self) -> None:
        # W8b. `missing_capability_reason` builds its "this repository implements it for X" list
        # from the records, and reverting that read to `core_provides` survives: the clause then
        # says "no value of this axis" for a capability the Fortran backend demonstrably has.
        # This string is carried VERBATIM into a leaf-facing violation, so a clause that names no
        # implementer tells an author to go implement something that already exists.
        # `zz_no_renderer`, not a real candidate value: `cpp` is a language this repository
        # names elsewhere as a plausible next member, and registering it — the documented
        # "Adding a backend" procedure — would turn this probe into a false failure.
        no_renderer = registry.Backend("language", "zz_no_renderer", None)
        with self._patched(no_renderer):
            reason = registry.missing_capability_reason(
                "language", "zz_no_renderer", "runner_render")
        self.assertIsNotNone(reason)
        self.assertIn("fortran", reason)
        self.assertNotIn("no value of this axis", reason)
        # And the negative half, so the assertion above cannot pass by naming everything: an
        # axis where nothing declares the capability really does say so.
        with mock.patch.dict(
                registry._BACKENDS,
                {k: v._replace(core_provides=frozenset(), backend_provides=frozenset())
                 for k, v in registry._BACKENDS.items() if k[0] == "language"}):
            bare = registry.missing_capability_reason("language", "zz_probe", "runner_render")
        self.assertIn("no value of this axis", bare)

    def test_capability_module_classifies_a_caller_typo_as_a_caller_typo(self) -> None:
        # W5c. `capability_module` validates the capability BEFORE asking about the record, and
        # deleting that survives — because without it every bad capability still gets refused,
        # just as `BackendNotExtracted` ("this backend does not implement it") instead of
        # `UnsupportedBackend` ("there is no such capability"). The registry's own contract is
        # that one input must not be one kind of failure at one entry point and another kind at
        # the next, and the second message sends a reader to declare a capability that does not
        # exist. The class is the assertion; the message would pass either way.
        for capability, ground in (("lint", "axis"), ("zz_not_a_capability", "unknown")):
            with self.assertRaises(registry.UnsupportedBackend, msg=capability) as ctx:
                registry.capability_module("language", "fortran", capability)
            self.assertIn(ground, str(ctx.exception))

    def test_the_package_reexport_is_what_the_dispatch_actually_reaches(self) -> None:
        """W7b. Driven in a FRESH interpreter, because in this one the question is already
        answered by accident.

        `capability_module` reads the capability off the package as an attribute, and importing
        a submodule anywhere sets that attribute on its parent. Several test modules import
        `...fortran.runner` directly, so by the time W7 runs the attribute exists whether or not
        `__init__` re-exports it — measured: deleting the re-export leaves the entire suite
        green, while a real run (where nothing imports the submodule by name) fail-closes on
        every M3c node. A subprocess that imports only the registry is the one observer that
        sees the line.
        """
        import subprocess
        import sys
        probe = (
            "from tools.backends import registry as r\n"
            "m = r.capability_module('language', 'fortran', 'runner_render')\n"
            "assert 'runner' in m.__name__, m.__name__\n"
            "assert hasattr(m, 'render_runner')\n"
            "print('ok')\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", probe], cwd=str(REPO_ROOT),
            capture_output=True, text=True)
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("ok", proc.stdout)

    def test_the_fortran_package_carries_what_its_record_claims(self) -> None:
        # W7. The declaration is a claim about the tree; this is the tree. Without it the record
        # could name a capability whose implementation had been renamed or deleted, and only a
        # live workflow would find out.
        module = registry.capability_module("language", "fortran", "runner_render")
        for name in ("render_runner", "assert_harness_pin", "ir_content_violations",
                     "CHECKS_PUBLIC_NAMES"):
            self.assertTrue(hasattr(module, name), name)

    def test_the_declaration_rules_do_not_block_a_single_value_migrating(self) -> None:
        """The rule that used to live here, and why there is no rule here now.

        Twice a declaration rule was written to forbid a record claiming a capability the
        neutral core no longer implements — first keyed by capability, then by axis — and both
        refused legitimate work. The axis-keyed form was the worse of the two: `linter` has four
        values whose lint adapters are genuinely separate inlined implementations, so migrating
        one made the other three illegal and `import tools.backends.registry` raise, for a module
        the whole tree imports. `parallel` had the same shape, and the `none` record's own
        comment predicted the state the rule forbade.

        "Does the neutral core still implement this job for THIS value" is a fact about the
        tree, per (axis, value), and no other record carries it. It is not checkable at
        declaration time — the registry must not import a backend there — so it is caught where
        it is knowable: the reachability loop below for the live tree, the seam's refusal, and
        the gates that report that refusal as a violation.
        """
        for axis, backend_id, capability in (("linter", "fortitude", "lint"),
                                             ("parallel", "openmp", "parallel_directives"),
                                             ("build_system", "make", "control_file")):
            migrated = registry.Backend(
                axis, backend_id, "tools.backends.language.fortran",
                backend_provides=frozenset({capability}))
            with mock.patch.dict(
                    registry.CAPABILITY_MODULE_ATTR, {capability: "runner"}), \
                    self._patched(migrated):
                registry._check_declarations()
                # the siblings that still carry it inlined keep answering for authorship
                for other in registry.backend_ids(axis):
                    if other != backend_id:
                        self.assertTrue(
                            registry.provides(axis, other, capability)
                            or capability not in registry._BACKENDS[(axis, other)].provided,
                            (axis, other, capability))

    def test_capability_module_refuses_a_capability_the_neutral_core_still_owns(self) -> None:
        """Both halves of a branch a comment here wrongly called unreachable, and a check a
        comment wrongly called moot. One record separates them.

        `control_file` is a question of two axes. When it migrates on `build_system` it gains a
        `CAPABILITY_MODULE_ATTR` row while `language/fortran` legitimately still carries it in
        `core_provides`. In that state, widening `capability_module`'s test from
        `backend_provides` to `provided` returns the package's module for a job the record only
        claims to do inlined: the wrong-module dispatch the function exists to prevent.

        And on the UNMODIFIED tree the same call takes the refusal's FIRST clause — the one
        saying the capability is still carried by the neutral core, rather than that nothing
        implements it. A comment called that clause unreachable; it is one line away. Both are
        asserted, because sending a reader to the wrong declaration is all this message does.
        """
        # (a) the live tree: the "still inlined" diagnosis, not the "nothing implements it" one
        with self.assertRaises(registry.BackendNotExtracted) as ctx:
            registry.capability_module("language", "fortran", "control_file")
        self.assertIn("still carried by the neutral core", str(ctx.exception))

        # (b) a value that declares it in neither set: the other clause
        bare = registry.Backend("language", "zz_bare", "tools.backends.language.fortran")
        with self._patched(bare):
            with self.assertRaises(registry.BackendNotExtracted) as ctx:
                registry.capability_module("language", "zz_bare", "control_file")
        self.assertIn("nothing in this repository implements it", str(ctx.exception))

        # (c) the state the ledger's next area creates: the narrow set is what refuses
        migrated_make = registry.Backend(
            "build_system", "make", "tools.backends.language.fortran",
            core_provides=frozenset({"build_execute"}),
            backend_provides=frozenset({"control_file"}))
        with mock.patch.dict(registry.CAPABILITY_MODULE_ATTR, {"control_file": "runner"}), \
                self._patched(migrated_make):
            registry._check_declarations()
            self.assertTrue(registry.provides("language", "fortran", "control_file"))
            with self.assertRaises(registry.BackendNotExtracted):
                registry.capability_module("language", "fortran", "control_file")

    def test_the_seam_refuses_a_package_that_does_not_carry_the_capability(self) -> None:
        """The seam's own use of `capability_module`, at the seam.

        Everything else about it is checked at the registry. What is only visible here is that
        the seam does not reach the backend the cheap way: `load(...)` plus
        `getattr(..., "runner")` is a live idiom elsewhere in the neutral core, so it is what a
        future migration copies, and under it this record yields `None` and then an
        `AttributeError` from inside a deterministic gate — the raise-instead-of-violation the
        seam exists to prevent. Through `capability_module` it is a typed refusal.
        """
        record = registry.Backend(
            "language", "zz_liar", "tools.backends",
            backend_provides=frozenset({"runner_render"}))
        with self._patched(record):
            self.assertIsNotNone(host_render.runner_render_refusal("zz_liar"))
            for call in (lambda: host_render.checks_public_names("zz_liar"),
                         lambda: host_render.render_runner("zz_liar", {}, "bx", "hx")):
                with self.assertRaises(host_render.RunnerRenderUnavailable):
                    call()

    def test_the_seam_lets_a_broken_backend_import_escape_as_itself(self) -> None:
        """`_module` re-types the REGISTRY's two refusals and nothing else.

        Widening that `except` to `except Exception` survives the suite, and it converts any
        failure inside the backend package — an ImportError, a syntax error, a broken module
        constant — into a violation string saying this repository does not implement
        `runner_render` for the value. That is false evidence handed to a leaf: it tells an
        author to implement what already exists, and it hides a host bug as a node defect.
        """
        record = registry.Backend(
            "language", "zz_broken", "tools.backends.zz_does_not_exist",
            backend_provides=frozenset({"runner_render"}))
        with self._patched(record):
            with self.assertRaises(ModuleNotFoundError):
                host_render.checks_public_names("zz_broken")

    def test_the_seam_dispatches_to_the_backend_of_the_LANGUAGE_it_is_given(self) -> None:
        """That the seam is asked the NODE's language, semantically rather than by spelling.

        Measured before this existed: hard-coding `"fortran"` at the seam's callers survived the
        suite, and in the naive spelling was killed only by the token ratchet — which
        `docs/BACKEND_BOUNDARY.md` §Enforcement states is a bound on growth and not a detector.
        A second language backend is the only observer that can tell dispatch from coincidence,
        so one is synthesised here: with two records declaring `runner_render`, asking for one
        must not return the other's answer.
        """
        import sys
        import types

        other = types.ModuleType("zz_other_lang_backend")
        other_runner = types.ModuleType("zz_other_lang_backend.runner")
        other_runner.CHECKS_PUBLIC_NAMES = ("zz_only_name",)
        other_runner.render_runner = lambda ir, spec_id, harness: "! rendered by zz_other\n"
        other.runner = other_runner
        record = registry.Backend(
            "language", "zz_other", "zz_other_lang_backend",
            backend_provides=frozenset({"runner_render"}))
        with mock.patch.dict(sys.modules, {"zz_other_lang_backend": other}), \
                self._patched(record):
            self.assertEqual(("zz_only_name",), host_render.checks_public_names("zz_other"))
            self.assertIn("zz_other", host_render.render_runner("zz_other", {}, "bx", "hx"))
            # ...and the incumbent still answers for itself, so the assertion above cannot pass
            # by the seam having been broken for everyone.
            self.assertIn("case_setup", host_render.checks_public_names("fortran"))

    def test_every_caller_of_the_seam_passes_the_NODE_S_language(self) -> None:
        """The three call sites, not just the seam — measured, all three were unobserved.

        `test_the_seam_dispatches_to_the_backend_of_the_LANGUAGE_it_is_given` witnesses
        `host_render`. Its callers each read the language off an artifact and hand it over, and
        hard-coding the value at any of them was killed ONLY by the token ratchet — which
        `docs/BACKEND_BOUNDARY.md` §Enforcement states is a bound on growth and not a detector,
        so in a spelling the ratchet cannot see (`"for" "tran"`) all three survived the suite.
        This is the "hands a node to the wrong writer" class, and one language backend is not
        enough to observe it: a second one is synthesised so the answers are distinguishable.
        """
        import json
        import sys
        import types

        import tools.codegen_bundle as codegen_bundle
        import tools.validate_pipeline_semantics as vps

        other = types.ModuleType("zz_second_lang")
        runner = types.ModuleType("zz_second_lang.runner")
        runner.CHECKS_PUBLIC_NAMES = ("zz_only_abi_name",)
        runner.render_runner = lambda ir, spec_id, harness: "! zz_second\n"
        runner.ir_content_violations = lambda ir, spec_id, harness: ["zz_second says no"]
        other.runner = runner
        other.bundle = types.ModuleType("zz_second_lang.bundle")
        other.bundle.SOURCE_EXTENSIONS = (".zz",)
        other.bundle.IDENTIFIER_MAX = 63
        other.bundle.IDENTIFIER_PATTERN = r"^[A-Za-z][A-Za-z0-9_]{0,62}(?![\s\S])"
        record = registry.Backend(
            "language", "zz_second", "zz_second_lang",
            core_provides=frozenset({"control_file"}),
            backend_provides=frozenset({"runner_render"}))
        ir = {
            "meta": {"spec_kind": "component", "spec_id": "bx"},
            "impl_defaults": {"toolchain": {"language": "zz_second", "build_system": "make"}},
            "dependency": {"direct_deps": [
                {"node_key": "infrastructure/harness_fortran_cpu@0.7.0"}]},
        }
        with mock.patch.dict(sys.modules, {"zz_second_lang": other}), \
                mock.patch.dict(registry._BACKENDS, {("language", "zz_second"): record}):
            # (1) the validator's mirror hands the seam the language it read from the IR
            self.assertEqual("zz_second", vps._ir_m3c_language(ir))
            self.assertEqual(
                ["zz_second says no"],
                list(host_render.ir_content_violations(
                    vps._ir_m3c_language(ir), ir, "bx", "harness_fortran_cpu")))
            # (2) the checks-source gate holds the leaf to THIS language's ABI. Driven through
            # the gate body, not through the seam: hard-coding the language inside the gate
            # survived the whole suite, because the seam's own witness never enters it.
            import tempfile
            with tempfile.TemporaryDirectory() as tmp:
                src = Path(tmp) / "src"
                src.mkdir()
                (src / "bx_checks.f90").write_text(
                    "module bx_checks\nend module bx_checks\n", encoding="utf-8")
                violations: list[str] = []
                vps._validate_checks_source_files(
                    SimpleNamespace(node_key="component/bx@0.1.0"), "zz_second", src, [],
                    violations)
            self.assertTrue(
                any("zz_only_abi_name" in v for v in violations),
                f"the gate demanded some other language's ABI: {violations}")

            # (3) the render-precondition gate asks THIS language's renderer for its objections
            with tempfile.TemporaryDirectory() as tmp:
                ir_dir = Path(tmp)
                (ir_dir / "spec.ir.yaml").write_text(json.dumps({
                    **ir, "dependency": {"direct_deps": [
                        {"node_key": "infrastructure/harness_fortran_cpu@0.7.0"}]}}),
                    encoding="utf-8")
                pre: list[str] = []
                vps._validate_harness_render_preconditions(ir_dir, ir_dir, pre)
            self.assertTrue(
                any("zz_second says no" in v for v in pre),
                f"the gate consulted some other language's renderer: {pre}")

            # (4) the bundle ABI gate asks for the FILE's language, not a fixed one
            bundle = {
                "files": [{"logical_path": "bx_checks.f90", "role": "checks",
                           "language": "zz_second", "member_node_key": "component/bx@0.1.0",
                           "content": "module bx_checks\nend module bx_checks\n",
                           "modules": ["bx_checks"]}],
            }
            violation = codegen_bundle.m3c_checks_abi_violation(bundle, "bx")
            self.assertIsNotNone(violation)
            self.assertIn("zz_only_abi_name", violation)

    @staticmethod
    def _unreachable_package_capabilities(
            backends: "dict[tuple[str, str], registry.Backend]") -> list[str]:
        """Every `backend_provides` declaration in `backends` that cannot actually be reached.

        COMPUTED over the records rather than written out, because a hand-listed check answers
        only for the records someone remembered — and the one tree check that existed was
        hard-coded to a single (axis, value, capability) triple, so a second backend declaring a
        capability its package does not carry would have been accepted by everything.
        """
        unreachable = []
        for (axis, backend_id), record in backends.items():
            for capability in sorted(record.backend_provides):
                try:
                    registry.capability_module(axis, backend_id, capability)
                except (registry.UnsupportedBackend, registry.BackendNotExtracted) as exc:
                    unreachable.append(f"{axis}/{backend_id}:{capability} ({exc!r})")
                except Exception as exc:  # noqa: BLE001
                    # Reported separately: a package whose `__init__` fails for an environment
                    # reason (a missing optional dependency) is not a record that overstates
                    # itself, and calling it one sends a reader to the declaration.
                    unreachable.append(
                        f"{axis}/{backend_id}:{capability} FAILED TO LOAD ({exc!r})")
        return unreachable

    def test_every_declared_package_capability_is_reachable_in_this_tree(self) -> None:
        # The declaration/tree gap cannot be closed at declaration time — the registry must not
        # import a backend there — so this is where it is closed for THIS tree: one loop over
        # every record, asking the question the seam will ask at run time.
        self.assertEqual(
            [], self._unreachable_package_capabilities(dict(registry._BACKENDS)),
            "a record declares a capability in backend_provides that its package does not "
            "carry; the seam would refuse a node the authorship predicates approved")

    def test_the_reachability_loop_is_observed_against_a_known_bad_tree(self) -> None:
        """The loop itself, driven — because over the live tree it can only ever pass.

        A check that iterates the real records answers "today's tree is fine" and says nothing
        about whether the iteration works. Deleting its body, or narrowing it back to one
        hard-coded triple, leaves it green. So it runs against a synthetic mapping with a known
        shape instead, the way this file's scanned-set tests do.
        """
        import sys
        import types

        hollow = types.ModuleType("zz_hollow_pkg")  # declares the job, carries nothing
        good = types.ModuleType("zz_good_pkg")
        good.runner = types.ModuleType("zz_good_pkg.runner")
        synthetic = {
            ("language", "zz_good"): registry.Backend(
                "language", "zz_good", "zz_good_pkg",
                backend_provides=frozenset({"runner_render"})),
            ("language", "zz_hollow"): registry.Backend(
                "language", "zz_hollow", "zz_hollow_pkg",
                backend_provides=frozenset({"runner_render"})),
            ("language", "zz_inlined"): registry.Backend(
                "language", "zz_inlined", None, core_provides=frozenset({"control_file"})),
        }
        with mock.patch.dict(sys.modules, {"zz_hollow_pkg": hollow, "zz_good_pkg": good}), \
                mock.patch.dict(registry._BACKENDS, synthetic):
            found = self._unreachable_package_capabilities(synthetic)
        # Exactly the hollow one: the good record is reached, and the record that declares
        # nothing in `backend_provides` is not visited at all.
        self.assertEqual(1, len(found), found)
        self.assertIn("zz_hollow:runner_render", found[0])

    def test_a_declaration_that_outruns_its_package_lands_in_the_gates_as_a_violation(self):
        """The gap that cannot be closed, checked where it lands.

        A record declares `runner_render` in `backend_provides`; its package does not carry the
        module. `_check_declarations` accepts it — the registry must not import a backend at
        declaration time — so `provides` answers True and both authorship predicates approve the
        node, while the seam refuses. Three review rounds were spent claiming a rule had made
        this impossible; it cannot be made impossible, so what matters is that the gate reports
        it as a VIOLATION and never as an exception, since an uncaught raise inside
        `_validate_compile_stage_impl` discards every violation its sibling gates collected.

        Driven THROUGH the checks gate, not through the seam: the seam's own refusal has its own
        witness, and dispatching correctly inside a gate is a different fact from dispatching
        correctly when called directly. The render-precondition gate shares this branch and is
        driven for the broken-import input by its own test.
        """
        import sys
        import tempfile
        import types

        import tools.validate_pipeline_semantics as vps

        empty_pkg = types.ModuleType("zz_pkg_without_runner")  # no `runner` attribute
        record = registry.Backend(
            "language", "zz_hollow", "zz_pkg_without_runner",
            core_provides=frozenset({"control_file"}),
            backend_provides=frozenset({"runner_render"}))
        with mock.patch.dict(sys.modules, {"zz_pkg_without_runner": empty_pkg}), \
                self._patched(record):
            registry._check_declarations()  # accepted: nothing here can see inside the package
            self.assertTrue(registry.provides("language", "zz_hollow", "runner_render"))
            self.assertIsNotNone(host_render.runner_render_refusal("zz_hollow"))

            with tempfile.TemporaryDirectory() as tmp:
                src = Path(tmp) / "src"
                src.mkdir()
                (src / "bx_checks.f90").write_text(
                    "module bx_checks\nend module bx_checks\n", encoding="utf-8")
                violations: list[str] = []
                vps._validate_checks_source_files(
                    SimpleNamespace(node_key="component/bx@0.1.0"), "zz_hollow", src, [],
                    violations)
                self.assertTrue(
                    any("cannot be stated for language 'zz_hollow'" in v for v in violations),
                    violations)

    def test_a_backend_that_cannot_be_imported_does_not_empty_the_violation_list(self) -> None:
        """The seam lets a broken import escape as itself — the GATES must not.

        Re-typing an `ImportError` at the seam would tell a leaf to implement what already
        exists, so the seam deliberately does not. But inside `_validate_compile_stage_impl`
        there is no handler, and an escaping exception replaces the sibling gates' actionable
        list with a traceback. Both properties are wanted; the conversion belongs at the gate.
        """
        import tempfile

        import tools.validate_pipeline_semantics as vps

        record = registry.Backend(
            "language", "zz_missing_pkg", "zz_module_that_does_not_exist",
            backend_provides=frozenset({"runner_render"}))
        with self._patched(record):
            with self.assertRaises(ModuleNotFoundError):     # the seam, unchanged
                host_render.checks_public_names("zz_missing_pkg")
            with tempfile.TemporaryDirectory() as tmp:       # the gate, converted
                src = Path(tmp) / "src"
                src.mkdir()
                (src / "bx_checks.f90").write_text(
                    "module bx_checks\nend module bx_checks\n", encoding="utf-8")
                violations = ["a sibling gate already found this"]
                vps._validate_checks_source_files(
                    SimpleNamespace(node_key="component/bx@0.1.0"), "zz_missing_pkg", src, [],
                    violations)
        self.assertIn("a sibling gate already found this", violations)
        self.assertTrue(any("could not be loaded" in v for v in violations), violations)

    def test_neither_gate_empties_its_violation_list_on_a_broken_backend(self) -> None:
        """BOTH gates, because the conversion is written twice and only one copy was driven.

        A backend package that cannot be imported is a host fault. The seam lets it escape as
        itself — re-typing it would tell a leaf to implement what already exists — so each gate
        converts it, and `_validate_compile_stage_impl` has no handler of its own: an escape
        replaces the sibling gates' actionable list with a traceback. The copy in
        `_validate_harness_render_preconditions` had no witness; its twin did.
        """
        import json
        import tempfile

        import tools.validate_pipeline_semantics as vps

        record = registry.Backend(
            "language", "zz_no_pkg", "zz_module_that_does_not_exist",
            core_provides=frozenset({"control_file"}),
            backend_provides=frozenset({"runner_render"}))
        ir = {
            "meta": {"spec_kind": "component", "spec_id": "bx"},
            "impl_defaults": {"toolchain": {"language": "zz_no_pkg", "build_system": "make"}},
            "dependency": {"node_key": "component/bx@0.1.0", "direct_deps": [
                {"node_key": "infrastructure/harness_fortran_cpu@0.7.0"}]},
        }
        with self._patched(record):
            with tempfile.TemporaryDirectory() as tmp:
                ir_dir = Path(tmp)
                (ir_dir / "spec.ir.yaml").write_text(json.dumps(ir), encoding="utf-8")
                violations = ["a sibling gate already found this"]
                vps._validate_harness_render_preconditions(ir_dir, ir_dir, violations)
        self.assertIn("a sibling gate already found this", violations)
        self.assertTrue(any("could not be loaded" in v for v in violations), violations)

    def test_implemented_counts_a_package_only_record(self) -> None:
        # `Backend.implemented` reads `provided`, and reverting it to `module is not None`
        # survives — every live record with a capability also has a module or lacks both. The
        # separating shape is a record with a capability and NO module, which is the honest
        # state of a value whose code is still inlined in the neutral core.
        record = registry.Backend(
            "linter", "zz_inlined_only", None, core_provides=frozenset({"lint"}))
        with self._patched(record):
            self.assertTrue(record.implemented)
            self.assertIn("zz_inlined_only", registry.implemented_backend_ids("linter"))
            self.assertIsNone(registry.unimplemented_reason("linter", "zz_inlined_only"))

    def test_capability_module_normalizes_the_value_like_every_other_entry_point(self) -> None:
        # `capability_module` lower-cases and strips before the lookup, and dropping that
        # survives: no test passes it a padded or upper-cased value. Every other entry point
        # normalizes, and a seam handed `" Fortran "` by a caller that did not would get a
        # refusal for a value this repository implements.
        for spelling in (" fortran", "FORTRAN", "Fortran ", " FoRtRaN "):
            module = registry.capability_module("language", spelling, "runner_render")
            self.assertTrue(hasattr(module, "render_runner"), spelling)

    def test_the_bundle_gate_is_the_third_caller_and_converts_a_broken_import_too(self) -> None:
        # `m3c_checks_abi_violation` was enumerated as one of the seam's three callers and was
        # the one left without the conversion its two siblings got. It escapes into
        # `_pure_bundle_violations`, which has no handler at that call, so the acceptance layer
        # crashes rather than rejecting the bundle.
        import tools.codegen_bundle as codegen_bundle

        record = registry.Backend(
            "language", "zz_broken_bundle", "zz_module_that_does_not_exist",
            backend_provides=frozenset({"runner_render"}))
        bundle = {"files": [{
            "logical_path": "bx_checks.f90", "role": "checks", "language": "zz_broken_bundle",
            "member_node_key": "component/bx@0.1.0",
            "content": "module bx_checks\nend module bx_checks\n", "modules": ["bx_checks"]}]}
        with self._patched(record):
            violation = codegen_bundle.m3c_checks_abi_violation(bundle, "bx")
        self.assertIsNotNone(violation)
        self.assertIn("could not be loaded", violation)

    def test_capability_module_refuses_a_non_module_under_the_convention_name(self) -> None:
        # The `isinstance(module, ModuleType)` narrowing survives being weakened to `is None`,
        # because every other witness uses a package with NO attribute at all. The refusal's own
        # comment describes this input — "a seam holding some other object and failing later on
        # a missing function" — and nobody supplied it.
        import sys
        import types

        impostor = types.ModuleType("zz_impostor_pkg")
        impostor.runner = "not a module"          # the convention name, wrong kind
        record = registry.Backend(
            "language", "zz_impostor", "zz_impostor_pkg",
            backend_provides=frozenset({"runner_render"}))
        with mock.patch.dict(sys.modules, {"zz_impostor_pkg": impostor}), self._patched(record):
            with self.assertRaises(registry.BackendNotExtracted) as ctx:
                registry.capability_module("language", "zz_impostor", "runner_render")
        self.assertIn("re-exports no", str(ctx.exception))

    def test_a_capability_may_migrate_one_axis_at_a_time(self) -> None:
        """The rule above must not block the ledger's own next area.

        `control_file` is a question of BOTH `build_system` and `language`. Keyed by capability
        alone, the migrated-out-of-the-core rule refused the language half — still legitimately
        inlined — the moment the build-system half moved, and the remedy its message named led
        straight to the no-package refusal. That is the next area in `TODO.md`, blocked by a
        rule written three commits earlier. It is per AXIS for that reason.
        """
        migrated_make = registry.Backend(
            "build_system", "make", "tools.backends.language.fortran",
            core_provides=frozenset({"build_execute"}),
            backend_provides=frozenset({"control_file"}))
        with mock.patch.dict(registry.CAPABILITY_MODULE_ATTR, {"control_file": "runner"}), \
                self._patched(migrated_make):
            registry._check_declarations()
            # the language half is untouched and still answers for authorship
            self.assertTrue(registry.provides("language", "fortran", "control_file"))
            self.assertTrue(registry.provides("build_system", "make", "control_file"))

    def test_capability_module_refuses_a_value_with_no_record_by_class(self) -> None:
        """`require_available` inside `capability_module`, driven on the input that needs it.

        The first version of this test used a DECLARED-but-unextracted record and asserted the
        class — and it passed with `require_available` deleted, because the record is in
        `_BACKENDS`, the lookup succeeds, and the `backend_provides` check below raises the same
        class. It was a test that could not fail for its stated reason.

        The input that reaches the guard is an OPEN-VOCABULARY axis value with no record at all:
        the membership question answers permissively for it, so the lookup on the next line is
        what fails, and without the guard it fails as a bare `KeyError`. The registry's contract
        is that one input is not two different kinds of failure at two entry points.
        """
        self.assertTrue(registry.AXES["parallel"].open_vocabulary)
        self.assertIsNone(registry.unsupported_reason("parallel", "zz_unregistered"))
        with self.assertRaises(registry.BackendNotExtracted):
            registry.capability_module(
                "parallel", "zz_unregistered", "parallel_directives")

    def test_the_seam_refuses_a_language_that_declares_no_renderer(self) -> None:
        # W8. The seam must not fall through to whichever backend happens to be extracted, and
        # the refusal must be the REGISTRY's sentence — a second wording here is a second
        # authority for what a leaf is told to implement.
        from tools import host_render
        # A SYNTHETIC value, not `cpp`: this repository names `cpp` elsewhere as a plausible
        # next language member, and registering one must not make this probe fail for a reason
        # that has nothing to do with what it checks.
        probe = registry.Backend("language", "zz_no_renderer", None)
        with self._patched(probe):
            expected = registry.missing_capability_reason(
                "language", "zz_no_renderer", "runner_render")
            self.assertIsNotNone(expected)
            self.assertEqual(expected, host_render.runner_render_refusal("zz_no_renderer"))
            for call in (
                    lambda: host_render.render_runner("zz_no_renderer", {}, "bx", "hx"),
                    lambda: host_render.checks_public_names("zz_no_renderer"),
                    lambda: host_render.ir_content_violations(
                        "zz_no_renderer", {}, "bx", "hx"),
                    lambda: host_render.assert_harness_pin(
                        "zz_no_renderer", {}, "bx", "hx", [], "")):
                with self.assertRaises(host_render.RunnerRenderUnavailable) as ctx:
                    call()
                self.assertEqual(expected, str(ctx.exception))


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
