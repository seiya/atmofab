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
  count. What the counts DO give (`TokenRatchetTests`) is a monotone bound with a direction:
  no file may grow, and a file that shrinks forces the baseline down (a stale-baseline finding),
  so the measure cannot drift upward and cannot silently stop tightening.

The direction of every failure is toward the rule. What NO shape of input does is prove
compliance — and the reverse claim, that no input makes the check pass by reading less, was made
here and was false three times over: the scope, the class list and the allowlist could each be
narrowed and then blessed by one regeneration. Those three are hand-pinned now; the sampled
counts remain a sample.

The sampled comparison runs in the suite (`TokenRatchetTests`) and on request; regenerating the
baseline is deliberate, not automatic:

    python3 -m tools.tests.test_backend_boundary --check-baseline
    python3 -m tools.tests.test_backend_boundary --write-baseline

Check first, judge each finding by `docs/BACKEND_BOUNDARY.md` §Decision Criteria, then write: the
diff is reviewable as the migration step it represents only once the findings behind it have been
read. Passing both flags at once is refused (exit 2) rather than obeyed, because the judgement
between them is not something an argv can express.
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import tools.validate_pipeline_semantics as vps  # noqa: E402
from tools import host_render  # noqa: E402
from tools.backends import registry  # noqa: E402
from tools.tests.target_fixtures import FORTRAN_CPU as _TARGET_PROFILE

#: The SAMPLED half. Regenerable: `--write-baseline` rewrites it, and `--check-baseline`'s growth
#: finding tells the maintainer to do exactly that when a token appears in a neutral role.
BASELINE_PATH = REPO_ROOT / "tools" / "tests" / "data" / "backend_boundary_baseline.json"

#: The PINNED half, in its own file that no command writes: the direct-import allowlist, the
#: scanned file set, and the token-class list. The allowlist lived in the regenerable file for
#: four review rounds, and `--write-baseline` rewrote both — so the remedy this module prescribes
#: for the sample laundered the pin. A change here is a hand edit, reviewed as the boundary
#: decision it is.
ALLOWLIST_PATH = REPO_ROOT / "tools" / "tests" / "data" / "backend_boundary_allowlist.json"

#: How `--write-baseline`'s summary names the pinned file. Fixed at import rather than derived from
#: `ALLOWLIST_PATH` at print time, because the rows that run `_write_baseline` point both paths at
#: a temporary directory (so that a parallel run never reads a file another row is holding), and a
#: temporary path is not under `REPO_ROOT`.
ALLOWLIST_DISPLAY = ALLOWLIST_PATH.relative_to(REPO_ROOT).as_posix()

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
    # `("leaf_config", "**/*.json")` — the committed leaf launch configuration (issue #63) —
    # was scanned here until Z4 (issue #171) deleted the directory with the agentic leaf. Its
    # §Scope bullet went in the same change, which is what the row below checks: every glob
    # here maps 1:1 to a §Scope bullet, in both directions.
    # The dependency declaration and the CI workflow (issue #161). Same reason as the two root
    # documents above and as `mcp_servers/`'s declaration files: these are where `linter`-,
    # `compiler`- and `language`-axis names are spelled, and an install line is exactly the kind
    # of statement the ratchet already measures in `docs/RUNBOOK.md`. Added because the
    # declaration landed OUTSIDE the scan: the two `pipx install '<linter><range>'` lines deleted
    # from `docs/DEVELOPMENT.md` reappeared in `requirements-dev.txt`, the recorded debt fell by
    # two, and nothing had moved out of the neutral core.
    #
    # Both are GLOBS rather than the filenames that exist today, and that is the whole repair. The
    # first version listed `requirements.txt` and `requirements-dev.txt` literally, which closes
    # two names and not the class: a reviewer dropped a `requirements-ci.txt` carrying two linter
    # ranges and a grammar pin, plus a `.github/workflows/tests.yml` carrying `apt-get install
    # cppcheck gfortran`, a `pipx install 'fortitude-lint…'` and a `gfortran -std=f2008` argv, and
    # the ratchet stayed green at 86 passed. `.github/` has no file in this tree yet — PR-3 of the
    # same issue creates exactly that workflow, and it lands inside the measured set rather than
    # re-creating the shape this entry closed.
    (".", "requirements*.txt"),
    # The remaining TRACKED files at the repository root. Enumerating them rather than globbing
    # `*` is deliberate: an operator's checkout carries untracked root files (`llm.yaml`, a
    # command log) whose tokens would make the recorded debt differ per machine, which is the one
    # thing a ratchet must not do. What keeps the enumeration honest is
    # `RootFileCoverageTests` below: every tracked root file must be scanned or be in
    # `_UNSCANNED_ROOT_FILES` with a reason, so a new one is refused until someone reads it.
    (".", ".gitignore"),
    (".", ".mcp.json"),
    (".", "pytest.ini"),
    (".", "LICENSE"),
    # Every file under `.github`, not only `*.yml`. A workflow's `- run:` step is one line away
    # from `- run: ./.github/workflows/setup.sh`, and round 3 measured that a `.sh` there carrying
    # `apt-get install cppcheck gfortran` and a `gfortran -std=f2008` argv was unscanned while
    # `docs/BACKEND_BOUNDARY.md` said `.github/**` was in scope.
    (".github", "**/*"),
)

#: Tracked root-level files deliberately NOT token-scanned, each with the reason. An EXPLICIT set
#: rather than a pattern, so adding a root file is refused until someone decides which side it is
#: on — which is the failure this branch had (`requirements.txt` and `requirements-dev.txt` landed
#: at the root, outside the scan, with `docs/BACKEND_BOUNDARY.md` §Scope silent about them).
_UNSCANNED_ROOT_FILES: dict[str, str] = {
    "TODO.md": (
        "a work ledger that RECORDS backend facts as history — measured spellings, past "
        "diagnoses, the migration ledger itself. It names technology for the same reason "
        "`docs/design/` does, and §Scope excludes that for the same stated reason. Scanning it "
        "would put its recorded history into the growth bound and make every entry about a "
        "backend a ratchet event. The size of that history moves with the file and is not "
        "part of this reason: 631 token occurrences at `f88ad94`, 205 after issue #181 moved "
        "the finished entries out; re-take it by summing `len(rx.findall(text))` over "
        "`_COMPILED` rather than quoting either figure."),
}

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



def _declare_target_with_language(repo_root: Path, language: str) -> None:
    """Declare, in a fixture repository, the one target profile with `language` and a catalog
    carrying its harness — what the Compile-stage render-precondition gate asks of since R4-a
    PR-3 (issue #284: it renders once per declared target, the IR naming none)."""
    from tools.tests.orchestration_fixtures import ensure_spec_entry
    from tools.tests.target_fixtures import FORTRAN_CPU, install_target_profile, profile_with

    install_target_profile(repo_root, profile_with(toolchain={"language": language}))
    ensure_spec_entry(repo_root,
                      f"infrastructure/{FORTRAN_CPU.harness['infrastructure_id']}@0.7.0")

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


def _load_baseline(path: Path | None = None) -> dict[str, object]:
    return json.loads((path or BASELINE_PATH).read_text(encoding="utf-8"))


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


#: The two command spellings, named once. `_dispatch` matches these and the prose below embeds
#: them, so a rename cannot leave a message naming a flag that no longer exists.
CHECK_FLAG = "--check-baseline"
WRITE_FLAG = "--write-baseline"

#: The condition every finding ends with, and the reason it is one constant. A regeneration
#: that nobody judged is the gate satisfied by editing what judges it — the messages before the
#: ratchet's 2026-09 freeze said "regenerate" unconditionally — so the only instruction to
#: regenerate is this one, and it names the judgement first. `test_the_findings_never_tell_a_
#: reader_to_regenerate_before_saying_when` pins the property rather than the wording: an
#: `assertIn(GROWTH_MESSAGE, out)` compares the constant with itself, so every edit to these
#: strings survived it.
_REMEDY_CLAUSE = (
    "Regenerate the baseline only after judging every entry by docs/BACKEND_BOUNDARY.md "
    "§Decision Criteria: growth that is backend knowledge is moved, never blessed; growth that is "
    "a token in a neutral role, and staleness, are recorded by running "
    f"`python3 -m tools.tests.test_backend_boundary {WRITE_FLAG}` in the same pull request, "
    f"with the {CHECK_FLAG} output and the judgement stated there.")

#: The two findings the sampled comparison can produce. The head of each says WHAT was measured
#: and the tail is the prohibition above, in that order, so that no instruction to regenerate can
#: precede the condition on it.
GROWTH_MESSAGE = (
    "backend knowledge grew in the neutral core (docs/BACKEND_BOUNDARY.md). Move it into "
    "tools/backends/<axis>/<backend_id>/ and reach it through tools/backends/registry.py. "
    "If the growth is a token appearing in a NEUTRAL role (naming an axis value, quoting a "
    "path), say so in the commit message. " + _REMEDY_CLAUSE)

STALE_MESSAGE = (
    "the baseline is looser than the tree, which is what a migration looks like, and it is the "
    "opposite finding from growth: growth is withdrawn, staleness is what the ledger records. " +
    _REMEDY_CLAUSE + " A migration updates the measured debt in TODO.md in the same pull "
    "request, so the ledger and the baseline cannot disagree and the ratchet keeps "
    "tightening.")


def grown_entries(baseline: dict[str, dict[str, int]],
                  measured: dict[str, dict[str, int]]) -> list[str]:
    """`"<rel>: <class> <recorded> -> <measured>"` for every count above its recorded ceiling.

    A file with no recorded entry has a ceiling of zero for every class, so a newly measured file
    is growth rather than something to skip.
    """
    grown: list[str] = []
    for rel, counts in sorted(measured.items()):
        recorded = baseline.get(rel, {})
        for name, n in sorted(counts.items()):
            allowed = recorded.get(name, 0)
            if n > allowed:
                grown.append(f"{rel}: {name} {allowed} -> {n}")
    return grown


def stale_entries(baseline: dict[str, dict[str, int]],
                  measured: dict[str, dict[str, int]],
                  scanned: set[str]) -> list[str]:
    """Every recorded count the tree no longer reaches, and every recorded file with no measurement.

    A ceiling that is never lowered stops being a ratchet: a file that shed backend spelling — or
    left the scanned set entirely — must lower its recorded count in the same commit, so the debt
    figure in TODO.md and the baseline cannot disagree. `scanned` decides which of the two causes
    an absent file gets named (`_absent_cause`).
    """
    stale: list[str] = []
    for rel, counts in sorted(baseline.items()):
        found = measured.get(rel)
        if found is None:
            stale.append(f"{rel}: recorded, now absent ({_absent_cause(rel, scanned)})")
            continue
        for name, allowed in sorted(counts.items()):
            n = found.get(name, 0)
            if n < allowed:
                stale.append(f"{rel}: {name} {allowed} -> {n}")
    return stale


def _check_baseline(root: Path | None = None, baseline_path: Path | None = None) -> int:
    """The sampled half, on request — the comparison `TokenRatchetTests` runs in the suite.

    Prints every finding under its message and returns 1; with no finding prints one summary line
    and returns 0. Writes nothing. `root` and `baseline_path` are both injectable because a
    witness needs to drive the comparison over a synthetic tree AND a synthetic baseline: given
    only one of the two, the command still reads the real other half.
    """
    root = root or REPO_ROOT
    baseline_path = baseline_path or BASELINE_PATH
    baseline = _load_baseline(baseline_path)["token_counts"]
    measured = token_counts(root)
    scanned = {p.relative_to(root).as_posix() for p in neutral_core_files(root)}
    grown = grown_entries(baseline, measured)
    stale = stale_entries(baseline, measured, scanned)
    for entry in grown:
        print(entry)
    if grown:
        print(GROWTH_MESSAGE)
    for entry in stale:
        print(entry)
    if stale:
        print(STALE_MESSAGE)
    if not grown and not stale:
        total = sum(sum(v.values()) for v in measured.values())
        print(f"ratchet: {len(measured)} files, {total} sampled occurrences, "
              f"matches {baseline_path}")
    return 1 if grown or stale else 0


def _synthetic_tree(tmp: Path, *relatives: str) -> None:
    """Write each relative path under `tmp` with one `fortran-subroutine` occurrence in it."""
    for rel in relatives:
        path = tmp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("subroutine placeholder\n", encoding="utf-8")


class TokenRatchetTests(unittest.TestCase):
    """The sampled measure over THIS tree: no neutral-core file carries more backend spelling
    than recorded, and none carries less (a stale baseline). Written over the two module
    functions `BaselineComparisonTests` witnesses on synthetic inputs — on a green tree these two
    rows run over empty findings, so they observe the tree's compliance and nothing about the
    comparison, which is why both classes exist."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.baseline = _load_baseline()["token_counts"]
        cls.measured = token_counts()
        cls.scanned = {p.relative_to(REPO_ROOT).as_posix() for p in neutral_core_files()}

    def test_no_file_exceeds_its_recorded_count(self) -> None:
        self.assertEqual(grown_entries(self.baseline, self.measured), [], GROWTH_MESSAGE)

    def test_the_baseline_is_not_stale(self) -> None:
        self.assertEqual(stale_entries(self.baseline, self.measured, self.scanned), [],
                         STALE_MESSAGE)


class BaselineComparisonTests(unittest.TestCase):
    """The sampled comparison, witnessed on synthetic inputs rather than on this tree.

    The comparison over the real baseline (`TokenRatchetTests`) runs over empty findings on a
    green tree, so `return []` in either module function survives it — the assertion observes
    the tree's compliance, not the comparison. These witnesses are what tells a maintainer the
    comparison reports what it claims to.
    """

    def test_the_absent_file_message_names_the_right_cause(self) -> None:
        # Both branches driven directly: in a green tree neither is reachable, so the message a
        # maintainer acts on had no witness and named one cause for two situations.
        self.assertEqual("shed every sampled token", _absent_cause("docs/a.md", {"docs/a.md"}))
        self.assertEqual("left the scanned set", _absent_cause("docs/a.md", set()))

    def test_a_count_above_its_recorded_ceiling_is_reported_as_growth(self) -> None:
        self.assertEqual(
            ["docs/a.md: fortran 2 -> 3", "docs/b.md: c-include 0 -> 1"],
            grown_entries({"docs/a.md": {"fortran": 2}},
                          {"docs/b.md": {"c-include": 1}, "docs/a.md": {"fortran": 3}}))

    def test_a_count_at_or_below_its_ceiling_is_not_growth(self) -> None:
        self.assertEqual([], grown_entries({"docs/a.md": {"fortran": 2}},
                                           {"docs/a.md": {"fortran": 2}}))
        self.assertEqual([], grown_entries({"docs/a.md": {"fortran": 2}},
                                           {"docs/a.md": {"fortran": 1}}))

    def test_a_count_below_its_recorded_ceiling_is_reported_as_stale(self) -> None:
        self.assertEqual(
            ["docs/a.md: fortran 3 -> 1"],
            stale_entries({"docs/a.md": {"fortran": 3}}, {"docs/a.md": {"fortran": 1}},
                          {"docs/a.md"}))

    def test_a_class_that_left_a_recorded_file_counts_as_zero(self) -> None:
        # Not a skip: a class the file no longer carries is the ordinary shape of a migration, and
        # reading `measured.get(name)` as "no opinion" would let the ceiling stay.
        self.assertEqual(
            ["docs/a.md: c-include 2 -> 0"],
            stale_entries({"docs/a.md": {"fortran": 1, "c-include": 2}},
                          {"docs/a.md": {"fortran": 1}}, {"docs/a.md"}))

    def test_a_recorded_file_with_no_measurement_is_stale_and_names_its_cause(self) -> None:
        self.assertEqual(
            ["docs/a.md: recorded, now absent (shed every sampled token)"],
            stale_entries({"docs/a.md": {"fortran": 1}}, {}, {"docs/a.md"}))
        self.assertEqual(
            ["docs/a.md: recorded, now absent (left the scanned set)"],
            stale_entries({"docs/a.md": {"fortran": 1}}, {}, set()))

    def test_check_baseline_reports_growth_and_staleness_from_the_root_it_is_given(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _synthetic_tree(tmp, "docs/kept.md", "docs/new.md")
            # Scanned, and carrying no sampled token: `token_counts` omits it, so it is absent
            # from the measurement while still IN the scanned set. This is the file that observes
            # `_check_baseline` handing the real scanned set to `stale_entries` — with `scanned`
            # degenerate the message names a scope change that did not happen, which is defect
            # (aa) in TODO.md's ledger, and every other row here passes with it broken.
            (tmp / "docs" / "quiet.md").write_text("no sampled spelling here\n", encoding="utf-8")
            baseline_path = tmp / "baseline.json"
            baseline_path.write_text(json.dumps({"token_counts": {
                "docs/kept.md": {"fortran-subroutine": 2},
                "docs/quiet.md": {"fortran-subroutine": 1},
                "docs/gone.md": {"fortran-subroutine": 1},
            }}), encoding="utf-8")
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                rc = _check_baseline(root=tmp, baseline_path=baseline_path)
        out = buffer.getvalue()
        self.assertEqual(1, rc)
        self.assertIn("docs/new.md: fortran-subroutine 0 -> 1", out)
        self.assertIn("docs/kept.md: fortran-subroutine 2 -> 1", out)
        self.assertIn("docs/gone.md: recorded, now absent (left the scanned set)", out)
        self.assertIn("docs/quiet.md: recorded, now absent (shed every sampled token)", out)
        self.assertIn(GROWTH_MESSAGE, out)
        self.assertIn(STALE_MESSAGE, out)

    def test_check_baseline_returns_zero_and_says_so_when_the_tree_matches_its_baseline(
            self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _synthetic_tree(tmp, "docs/kept.md")
            baseline_path = tmp / "baseline.json"
            baseline_path.write_text(
                json.dumps({"token_counts": {"docs/kept.md": {"fortran-subroutine": 1}}}),
                encoding="utf-8")
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                rc = _check_baseline(root=tmp, baseline_path=baseline_path)
        out = buffer.getvalue()
        self.assertEqual(0, rc)
        self.assertIn("ratchet: 1 files, 1 sampled occurrences", out)
        self.assertNotIn(GROWTH_MESSAGE, out)
        self.assertNotIn(STALE_MESSAGE, out)

    def _check_on(self, tree: dict[str, str], baseline: dict[str, dict[str, int]]) -> tuple[int, str]:
        """Drive `_check_baseline` over a synthetic tree and baseline; return `(rc, stdout)`."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            for rel, body in tree.items():
                path = tmp / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body, encoding="utf-8")
            baseline_path = tmp / "baseline.json"
            baseline_path.write_text(json.dumps({"token_counts": baseline}), encoding="utf-8")
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                rc = _check_baseline(root=tmp, baseline_path=baseline_path)
        return rc, buffer.getvalue()

    def test_growth_alone_exits_one(self) -> None:
        # The two directional rows below exist because the both-corner and the neither-corner do
        # not pin `1 if grown or stale else 0`: with the tree carrying BOTH, `1 if grown else 0`
        # and `1 if stale else 0` each still answer 1, and both survived a mutation sweep.
        rc, out = self._check_on({"docs/a.md": "subroutine placeholder\n"}, {})
        self.assertEqual(1, rc)
        self.assertIn("docs/a.md: fortran-subroutine 0 -> 1", out)
        self.assertIn(GROWTH_MESSAGE, out)
        self.assertNotIn(STALE_MESSAGE, out)

    def test_staleness_alone_exits_one(self) -> None:
        # This is the shape a migration produces (docs/BACKEND_BOUNDARY.md §Operations Rules), so
        # it is the corner a ledger pull request actually lands on.
        rc, out = self._check_on({"docs/a.md": "subroutine placeholder\n"},
                                 {"docs/a.md": {"fortran-subroutine": 3}})
        self.assertEqual(1, rc)
        self.assertIn("docs/a.md: fortran-subroutine 3 -> 1", out)
        self.assertIn(STALE_MESSAGE, out)
        self.assertNotIn(GROWTH_MESSAGE, out)

    def _write_on(self, tree: dict[str, str], baseline: object | None) -> tuple[str, Path, Path]:
        """Run `_write_baseline` over a synthetic tree; return `(stdout, root, baseline_path)`.

        The tree and the baseline are both synthetic ON PURPOSE. A row that drove the real
        `_write_baseline` and asserted what its preview printed compared this tree against the
        recorded baseline on every `pytest tools/tests/` — a second copy of
        `TokenRatchetTests`, and one whose assertions are about the preview rather than the tree.
        """
        # `enterContext` is 3.11+; this repository's floor is lower, so the cleanup is
        # registered by hand.
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        tmp = Path(holder.name)
        for rel, body in tree.items():
            path = tmp / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        baseline_path = tmp / "baseline.json"
        if isinstance(baseline, bytes):
            baseline_path.write_bytes(baseline)
        elif baseline is not None:
            baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            _write_baseline(root=tmp, baseline_path=baseline_path)
        return buffer.getvalue(), tmp, baseline_path

    def test_write_baseline_says_so_when_the_regeneration_changes_nothing(self) -> None:
        # The branch an operator hits on nearly every real invocation, and the one with no
        # witness: mutating its text, or dropping its `return`, left the file green.
        printed, _, path = self._write_on(
            {"docs/kept.md": "subroutine placeholder\n"},
            {"token_counts": {"docs/kept.md": {"fortran-subroutine": 1}}})
        self.assertIn("the tree already matches the baseline", printed)
        self.assertNotIn("about to bless", printed)
        self.assertNotIn("could not be read", printed)
        self.assertEqual(measure(path.parent), json.loads(path.read_text(encoding="utf-8")))

    def test_write_baseline_lists_growth_and_staleness_under_separate_headings(self) -> None:
        # Growth and staleness take OPPOSITE answers under §Decision Criteria and print in the
        # same shape, so one flat list said nothing about which answer a row wants.
        printed, _, _ = self._write_on(
            {"docs/kept.md": "subroutine placeholder\n", "docs/new.md": "subroutine here\n"},
            {"token_counts": {"docs/kept.md": {"fortran-subroutine": 2},
                              "docs/gone.md": {"fortran-subroutine": 1}}})
        self.assertIn("docs/new.md: fortran-subroutine 0 -> 1", printed)
        self.assertIn("docs/kept.md: fortran-subroutine 2 -> 1", printed)
        self.assertIn("docs/gone.md: recorded, now absent (left the scanned set)", printed)
        # The counts are DERIVED from the rows printed under each heading, not transcribed: a
        # summary saying "0 grown and 0 stale", or one with the two swapped, printed the entries
        # and survived.
        sections: dict[str, int] = {}
        current = None
        for line in printed.splitlines():
            stripped = line.strip()
            heading = next((h for h in ("GROWN", "STALE") if stripped.startswith(f"{h} (")), None)
            if heading is not None:
                current = heading
                sections[heading] = 0
            elif current and (" -> " in stripped or "recorded, now absent" in stripped):
                sections[current] += 1
        self.assertEqual({"GROWN": 1, "STALE": 2}, sections, printed)
        self.assertIn("about to bless 1 grown and 2 stale", printed)

    def test_write_baseline_still_writes_when_the_recorded_baseline_cannot_be_read(self) -> None:
        """The preview discloses; it must not gate the write.

        A conflict-marked, truncated, binary-corrupted or wrong-shaped baseline is the state whose
        only documented recovery is this command, and for one commit the preview raised out of
        `__main__` before the write — `origin/main` regenerated the same file. Four spellings,
        three of which reached the command AFTER a fix that claimed to close the class:
        unparseable text, a missing key, a non-UTF-8 byte (`UnicodeDecodeError` is a `ValueError`,
        not a `JSONDecodeError`), and a well-formed JSON whose `token_counts` is not a mapping
        (which used to raise inside `grown_entries`, outside the handler).
        """
        tree = {"docs/kept.md": "subroutine placeholder\n"}
        for label, corrupt in (
                ("unparseable", b"<<<<<<< HEAD\n{\n=======\n"),
                ("no token_counts key", b'{"other": {}}\n'),
                ("not utf-8", b'{"token_counts": {"docs/a.md": {"fortran-\xff": 1}}}'),
                ("token_counts is a list", b'{"token_counts": []}\n')):
            with self.subTest(baseline=label):
                printed, root, path = self._write_on(tree, corrupt)
                self.assertIn("the recorded baseline could not be read", printed)
                self.assertIn("starting point rather than a change", printed)
                self.assertEqual(measure(root),
                                 json.loads(path.read_text(encoding="utf-8")))

    def test_the_findings_never_tell_a_reader_to_regenerate_before_saying_when(self) -> None:
        """A SAMPLE over the prescriptive strings, not a set identity — read the second paragraph.

        Every other row here asserts `assertIn(GROWTH_MESSAGE, out)` — the constant compared with
        itself — so restoring an unconditional "regenerate the baseline with --write-baseline"
        anywhere in either message left the whole file green. What is checked here is that the
        only instruction to regenerate in either message is the conditional one: the message
        OUTSIDE `_REMEDY_CLAUSE` names neither the write command nor any of a few spellings of
        blessing the tree.

        WHAT IS SAMPLED: the spellings. A first version looked only at the text BEFORE the clause,
        and appending "In practice: regenerate now with --write-baseline" to the tail — the last
        sentence a reader sees — passed. Widening to the whole message closes that, and does not
        close a differently-worded instruction ("rewrite the recorded ceilings and carry on"),
        which is why the blocklist below carries several spellings and is still a sample. A
        message that means the opposite of the rule in words nobody listed here passes.
        """
        forbidden = (WRITE_FLAG, "regenerate", "rewrite", "bless", "raise the ceiling")
        for name, message in (("GROWTH_MESSAGE", GROWTH_MESSAGE), ("STALE_MESSAGE", STALE_MESSAGE)):
            with self.subTest(message=name):
                self.assertIn(_REMEDY_CLAUSE, message)
                outside = "".join(message.split(_REMEDY_CLAUSE)).lower()
                for spelling in forbidden:
                    self.assertNotIn(spelling.lower(), outside,
                                     f"{name} tells the reader to {spelling!r} outside the one "
                                     f"clause that says when regenerating is allowed")
        # The condition itself: regeneration is conditional, knowledge is never blessed, and the
        # judgement comes before the command.
        self.assertTrue(_REMEDY_CLAUSE.startswith("Regenerate the baseline only after judging"))
        self.assertIn("is moved, never blessed", _REMEDY_CLAUSE)
        self.assertIn("§Decision Criteria", _REMEDY_CLAUSE)
        self.assertLess(_REMEDY_CLAUSE.index("§Decision Criteria"),
                        _REMEDY_CLAUSE.index(WRITE_FLAG),
                        "the clause names the command before the judgement it is conditional on")

    def test_the_refusal_message_names_both_commands_and_what_sits_between_them(self) -> None:
        # Same class as the row above: the exit code is pinned, the sentence that tells the
        # operator WHY the pair is the judgement skipped was not.
        for flag in (CHECK_FLAG, WRITE_FLAG):
            self.assertIn(flag, BOTH_COMMANDS_MESSAGE)
        self.assertIn("judgement", BOTH_COMMANDS_MESSAGE)
        self.assertIn("§Decision Criteria", BOTH_COMMANDS_MESSAGE)
        # A WORDING pin, deliberately, on the one sentence that IS the rule: with only the pins
        # above, replacing the head of the message left the flags and the judgement in the tail
        # and the row green, so nothing said the pair is REFUSED. Rewording this is a deliberate
        # edit that has to read this line.
        self.assertIn("cannot run in one invocation", BOTH_COMMANDS_MESSAGE,
                      "the refusal message no longer says the pair is refused")
        self.assertLess(BOTH_COMMANDS_MESSAGE.index("cannot run in one invocation"),
                        BOTH_COMMANDS_MESSAGE.index("Run "),
                        "the message instructs before it refuses")

    def test_an_unrecognised_argument_beside_a_command_is_refused(self) -> None:
        # `--write-baseline --check-baselin` was a write with no refusal and no comparison.
        with mock.patch(f"{__name__}._write_baseline") as wrote:
            buffer = io.StringIO()
            with contextlib.redirect_stderr(buffer):
                rc = _dispatch(["prog", WRITE_FLAG, CHECK_FLAG[:-1]])
        self.assertEqual(2, rc)
        wrote.assert_not_called()
        self.assertIn(CHECK_FLAG[:-1], buffer.getvalue())
        # Repeating a command is not an unrecognised argument, and no command still means tests:
        # the refusal must not reach `unittest.main`'s own argv, which this module never parsed.
        with mock.patch(f"{__name__}._check_baseline", return_value=0) as checked:
            self.assertEqual(0, _dispatch(["prog", CHECK_FLAG, CHECK_FLAG]))
        checked.assert_called_once_with()
        self.assertIsNone(_dispatch(["prog", "-k", "SomeTest", "--tb=short"]))

    def test_the_two_commands_refuse_to_run_in_one_invocation(self) -> None:
        # Under `elif` this argv wrote the baseline and exited 0 — a regeneration that reads in a
        # pull request as "--check-baseline passed". The judgement between the two is the point.
        with mock.patch(f"{__name__}._write_baseline") as wrote:
            buffer = io.StringIO()
            with contextlib.redirect_stderr(buffer):
                rc = _dispatch(["prog", "--write-baseline", "--check-baseline"])
        self.assertEqual(2, rc)
        wrote.assert_not_called()
        self.assertIn(BOTH_COMMANDS_MESSAGE, buffer.getvalue())

    def test_each_command_alone_still_dispatches_and_no_argument_runs_the_tests(self) -> None:
        # The over-refusal side of the row above: refusing the pair must not refuse either one.
        with mock.patch(f"{__name__}._write_baseline") as wrote:
            self.assertEqual(0, _dispatch(["prog", "--write-baseline"]))
        wrote.assert_called_once_with()
        with mock.patch(f"{__name__}._check_baseline", return_value=7) as checked:
            self.assertEqual(7, _dispatch(["prog", "--check-baseline"]))
        checked.assert_called_once_with()
        self.assertIsNone(_dispatch(["prog"]))
        self.assertIsNone(_dispatch(["prog", "-k", "SomeTest"]))

    def test_the_refused_pair_writes_nothing_through_the_real_command(self) -> None:
        # Through the real CLI in a real subprocess: the in-process row above mocks the writer, so
        # only this one observes that the file on disk is untouched.
        # The one row left that can write the REAL baseline, because a subprocess cannot be
        # patched: it writes only when `_dispatch` is broken, which is this row's failure anyway,
        # so a parallel run is exposed to it only in a run that is already red.
        before = BASELINE_PATH.read_bytes()
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "tools.tests.test_backend_boundary",
                 "--write-baseline", "--check-baseline"],
                cwd=REPO_ROOT, capture_output=True, text=True, check=False)
            after = BASELINE_PATH.read_bytes()
        finally:
            BASELINE_PATH.write_bytes(before)
        self.assertEqual(2, proc.returncode, proc.stdout + proc.stderr)
        self.assertEqual(before, after, "the refused pair wrote the baseline anyway")
        self.assertIn(BOTH_COMMANDS_MESSAGE, proc.stderr)

    def test_check_baseline_is_dispatched_by_the_module_command(self) -> None:
        # The dispatch, not the tree's freshness (`TokenRatchetTests` owns that), so either
        # verdict is a pass here. Without the branch the argument reaches
        # `unittest.main`, which exits 2 having printed its own usage to stderr.
        proc = subprocess.run(
            [sys.executable, "-m", "tools.tests.test_backend_boundary", "--check-baseline"],
            cwd=REPO_ROOT, capture_output=True, text=True, check=False)
        self.assertIn(proc.returncode, (0, 1), proc.stderr)
        self.assertTrue(proc.stdout.strip(), proc.stderr)

    def test_the_module_with_no_command_still_runs_its_tests(self) -> None:
        # `_dispatch` answering None must reach `unittest.main`. Replacing that call with a
        # pass-through leaves every row here green, because nothing else runs the module bare.
        # One named test, so this stays a dispatch witness and not a second suite run.
        proc = subprocess.run(
            [sys.executable, "-m", "tools.tests.test_backend_boundary", "-v",
             "BaselineComparisonTests.test_a_count_at_or_below_its_ceiling_is_not_growth"],
            cwd=REPO_ROOT, capture_output=True, text=True, check=False)
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertIn("Ran 1 test", proc.stderr, proc.stdout + proc.stderr)


class RootFileCoverageTests(unittest.TestCase):
    """Every TRACKED file at the repository root is scanned, or excused by name with a reason.

    The set-identity half of the scan, and the one this branch needed: `requirements.txt` and
    `requirements-dev.txt` landed at the root OUTSIDE the scan, with `docs/BACKEND_BOUNDARY.md`
    §Scope neither listing them nor excluding them, and the recorded debt fell by two tokens that
    had simply moved into an unmeasured file. Widening the glob afterwards closed those two names;
    round 3 then dropped in a `requirements.in`, a `dev-requirements.txt`, a `constraints.txt`, a
    `pyproject.toml` and a `setup.cfg`, all carrying backend spelling, all green.

    A pattern cannot close that class — the next filename is always one rename away. This can,
    because it asks the opposite question: not "is this name scanned" but "is every name
    accounted for". A new root file is refused until someone puts it on one side or the other,
    which is a review gate rather than a judgment.

    The corpus is `git ls-files`, not a glob: an operator's checkout carries untracked root files
    (`llm.yaml`, a command log) and a rule derived from the filesystem would differ per machine.
    The cost of that choice, stated because it bounds the claim: an UNTRACKED root file is not
    seen here. That is the right side of the line — an untracked file is not part of the
    repository and reaches no other machine — but it does mean this rule answers about what is
    committed, not about what is on disk.

    Measured after it was written, with each file `git add -N`'d one at a time: a
    `requirements.in`, `dev-requirements.txt`, `constraints.txt`, `pyproject.toml`, `setup.cfg`
    and `requirements-ci.txt` carrying linter ranges and a grammar pin are all refused, as are a
    `.github/workflows/tests.sh` and a `.github/workflows/tests.yml` carrying `apt-get install
    cppcheck gfortran` and a `gfortran -std=f2008` argv — eight of eight, where every one of them
    was green before this class and the `.github` widening.
    """

    def _tracked_root_files(self) -> set[str]:
        listed = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files", "-z", "--", ":(top)*"],
            capture_output=True, text=True, check=True).stdout.split("\0")
        return {name for name in listed if name and "/" not in name}

    @staticmethod
    def _scanned_root_files() -> set[str]:
        """`neutral_core_files()` returns ABSOLUTE paths; a root file is one whose repo-relative
        form has no separator. Getting this wrong reported every root file as unaccounted, which
        is at least loud — the quiet direction would have been an empty tracked set."""
        return {
            path.relative_to(REPO_ROOT).as_posix() for path in neutral_core_files()
            if "/" not in path.relative_to(REPO_ROOT).as_posix()}

    def test_every_tracked_root_file_is_scanned_or_excused_by_name(self) -> None:
        tracked = self._tracked_root_files()
        self.assertTrue(tracked, "git listed no root-level file; this check observes nothing")
        scanned = self._scanned_root_files()
        unaccounted = sorted(tracked - scanned - set(_UNSCANNED_ROOT_FILES))
        self.assertEqual(
            [], unaccounted,
            "a tracked file at the repository root is neither token-scanned nor listed in "
            "_UNSCANNED_ROOT_FILES with a reason. Backend spelling placed there would lower no "
            f"counter and trip no check: {unaccounted}")

    def test_the_exclusion_list_has_no_stale_entry(self) -> None:
        """The other direction: an entry for a file that is gone, or for one now scanned, is a
        reason nobody will re-read, and it hides that the set has stopped being exact."""
        tracked = self._tracked_root_files()
        scanned = self._scanned_root_files()
        for name, reason in sorted(_UNSCANNED_ROOT_FILES.items()):
            with self.subTest(name=name):
                self.assertIn(name, tracked, f"{name} is excused but is not a tracked root file")
                self.assertNotIn(name, scanned, f"{name} is excused but IS scanned")
                self.assertGreater(len(reason), 40, f"{name}'s exclusion states no real reason")


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

    #: The heading that ends §Scope's IN-scope list. Everything after it is the "Out of scope,
    #: each for a stated reason" list, and reading that as a declaration is how a glob under
    #: `spec/` — a directory §Scope EXCLUDES — passed this row with no bullet (round 3).
    _OUT_OF_SCOPE_MARKER = "Out of scope"

    def _in_scope_section(self, document: str) -> str:
        self.assertIn("## Scope", document,
                      "docs/BACKEND_BOUNDARY.md no longer has a §Scope section")
        section = document.split("## Scope", 1)[1].split("\n## ", 1)[0]
        self.assertIn(
            self._OUT_OF_SCOPE_MARKER, section,
            "docs/BACKEND_BOUNDARY.md §Scope no longer carries its out-of-scope list; this row "
            "cannot tell a declaration of scope from an exclusion")
        return section.split(self._OUT_OF_SCOPE_MARKER, 1)[0]

    @staticmethod
    def _glob_is_declared(subdir: str, pattern: str, in_scope: str, root: Path) -> bool:
        """Does `in_scope` name the glob `(subdir, pattern)`?

        A module-level predicate called by BOTH the row below and its probe. The first version
        inlined this expression in the row and re-implemented it in the probe, so nothing executed
        the real predicate against a synthetic case — measured in round 3: replacing the row's
        condition with `if False:` and emptying its loop both left the file green, i.e. the probe
        that claims to witness this coupling witnessed nothing. That is the exact sin the sibling
        `test_dependency_declaration` docstring says it avoided.

        For a root-level glob the document may name the PATTERN (`requirements*.txt`) or the files
        it actually matches (`requirements.txt` and `requirements-dev.txt`) — naming the real files
        is what every other bullet does, and requiring the glob string refused that rewording.
        """
        if subdir != ".":
            return subdir in in_scope
        if pattern in in_scope:
            return True
        matched = sorted(p.name for p in (root).glob(pattern) if p.is_file())
        return bool(matched) and all(name in in_scope for name in matched)

    def test_every_scanned_glob_is_named_by_the_document_that_declares_the_scope(self) -> None:
        """`docs/BACKEND_BOUNDARY.md` §Scope and `_SCANNED_GLOBS` say the same thing, checked.

        `_SCANNED_GLOBS`'s own comment has claimed a 1:1 mapping onto §Scope's bullets since the
        `leaf_config/` entry was added, and nothing enforced it — which is how the dependency
        declaration came to be OUTSIDE the scan while §Scope neither listed it nor excluded it
        with a stated reason (issue #161). Two round-2 reviewers had to verify the mapping by
        hand, and deleting the new §Scope bullet left this file green.

        Coupled by POINTER, not by wording: each glob's root path must appear somewhere in the
        §Scope section. That is `atmofab-enforcement-change` rule 3-a's middle option, chosen
        because §Scope is prose that explains each entry rather than a list of globs, and pinning
        the prose would refuse every legitimate rewording. What it catches is the failure that
        actually happened — a glob added with no bullet, or a bullet deleted while the glob stays.
        What it does not catch is a bullet whose PROSE misdescribes its glob; that stays a review
        matter and is said here rather than left to look covered.
        """
        document = (REPO_ROOT / "docs" / "BACKEND_BOUNDARY.md").read_text()
        in_scope = self._in_scope_section(document)
        missing = [
            f"{subdir}/{pattern}" for subdir, pattern in _SCANNED_GLOBS
            if not self._glob_is_declared(subdir, pattern, in_scope, REPO_ROOT)]
        self.assertEqual(
            [], missing,
            "docs/BACKEND_BOUNDARY.md §Scope does not name every scanned glob, so the document "
            "and the instrument disagree about what is measured. Add the bullet, with the reason "
            f"the entry is in scope: {missing}")

    def test_the_scope_coupling_notices_a_glob_with_no_bullet(self) -> None:
        """The probe for the row above, DRIVING THE REAL PREDICATE.

        The row is exactly the kind that goes vacuous silently — every needle it looks for is a
        common word in a document about paths — and its first version could not have failed,
        because this probe re-implemented the expression instead of calling it.

        The `spec/` row is the one round 3 found: §Scope's out-of-scope list names `spec/` as
        EXCLUDED, and a plain substring search over the whole section accepted a `spec` glob on the
        strength of that sentence. The predicate is given only the in-scope half.

        WHAT IS STILL NOT PINNED, measured rather than reasoned: replacing the consuming row's
        condition with `if False` leaves this file green, and so does emptying
        `RootFileCoverageTests`' computation. This probe pins the PREDICATE, not the row that
        calls it — an assertion cannot witness its own weakening, which is the standing limit this
        module's own docstring records for five other decisions here. It is covered by external
        mutation runs and by review, not by anything inside the suite, and saying so is better
        than letting a reader count these rows as self-witnessed.
        """
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        root = Path(holder.name)
        (root / "README.md").write_text("")
        (root / "requirements.txt").write_text("")
        (root / "requirements-dev.txt").write_text("")
        in_scope = "- All Python under `tools/`.\n- `README.md`.\n"
        for subdir, pattern, expected in (
                ("tools", "**/*.py", True),          # a directory the list names
                (".", "README.md", True),            # a root file the list names
                (".", "requirements*.txt", False),   # a root glob it does not
                (".github", "**/*", False),          # a directory it does not
                ("spec", "**/*.yaml", False)):       # named only by the EXCLUSION list
            with self.subTest(glob=f"{subdir}/{pattern}"):
                self.assertEqual(
                    self._glob_is_declared(subdir, pattern, in_scope, root), expected)
        # A root glob may be declared by the FILES it matches rather than by the pattern.
        by_filename = "- `requirements.txt` and `requirements-dev.txt`, the declaration.\n"
        self.assertTrue(
            self._glob_is_declared(".", "requirements*.txt", by_filename, root))
        self.assertFalse(
            self._glob_is_declared(".", "requirements*.txt",
                                   "- `requirements.txt` alone.\n", root),
            "naming only SOME of the files a glob matches must not count as declaring it")

    def test_the_in_scope_slice_stops_at_the_exclusion_list(self) -> None:
        """The bound the row above rests on, self-tested. Without it the predicate is handed the
        exclusion list too, and a bullet saying a directory is OUT of scope reads as a
        declaration that it is in."""
        section = self._in_scope_section(
            "## Scope\n- `tools/`.\n\nOut of scope, each for a stated reason:\n- `spec/`.\n")
        self.assertIn("tools/", section)
        self.assertNotIn("spec/", section)
        with self.assertRaises(AssertionError) as caught:
            self._in_scope_section("## Scope\n- `tools/`.\n")
        self.assertIn("no longer carries its out-of-scope list", str(caught.exception))

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

    @contextlib.contextmanager
    def _data_files_in_a_temporary_directory(self):
        """Point `BASELINE_PATH` and `ALLOWLIST_PATH` at copies in a temporary directory.

        The two rows below run the real `_write_baseline()` with its default arguments, and each
        used to plant a sentinel in the REAL file and restore it in a `finally`. For the length of
        that `try`, any other process reading the file read the sentinel: a parallel suite
        (`pytest -n`) reported `TokenRatchetTests` red, and two mutants were scored killed on that
        alone and survived a serial re-run; `-x` under `-n` then skipped the `finally` and left
        the sentinel in the checkout. Both measured 2026-09-25. Patching the module's two names
        keeps the default-argument path under test, since `_write_baseline` resolves them at call
        time. The caller asserts the real files are byte-identical afterwards, which is what
        still catches a writer that stops honouring the names.
        """
        module = sys.modules[__name__]
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            baseline = tmp / BASELINE_PATH.name
            allowlist = tmp / ALLOWLIST_PATH.name
            baseline.write_bytes(BASELINE_PATH.read_bytes())
            allowlist.write_bytes(ALLOWLIST_PATH.read_bytes())
            with mock.patch.object(module, "BASELINE_PATH", baseline), \
                    mock.patch.object(module, "ALLOWLIST_PATH", allowlist):
                # The sentinel row is only a witness while the writer sees the COPY: unpatched,
                # it plants the sentinel where nothing writes and passes on no evidence.
                self.assertEqual((baseline, allowlist), (module.BASELINE_PATH, module.ALLOWLIST_PATH))
                yield baseline, allowlist

    def _assert_the_real_files_are_untouched(self, baseline: bytes, allowlist: bytes) -> None:
        self.assertEqual(baseline, BASELINE_PATH.read_bytes(),
                         "_write_baseline wrote the real baseline, not the path it was given")
        self.assertEqual(allowlist, ALLOWLIST_PATH.read_bytes(),
                         "_write_baseline wrote the real allowlist")

    def test_write_baseline_does_not_touch_the_pinned_file(self) -> None:
        """The write set of `_write_baseline`, observed rather than described.

        The commit that split the two halves is the fix for a laundering path, and its guard
        checked `measure()`'s keys — so appending an `ALLOWLIST_PATH.write_text(...)` to
        `_write_baseline` reinstated the laundering with the whole suite green. This runs the
        command and compares the pinned file's bytes.
        """
        real_baseline, real_allowlist = BASELINE_PATH.read_bytes(), ALLOWLIST_PATH.read_bytes()
        # A SENTINEL, not the file's own bytes. Comparing the bytes only catches a write whose
        # content differs, and the write that matters — recomputing the allowlist — produces
        # identical bytes on a clean tree and differing bytes exactly when a bypass has just been
        # added. The sentinel makes ANY write to this path visible, clean tree or not.
        sentinel = real_allowlist + b"\n"
        with self._data_files_in_a_temporary_directory() as (_, allowlist):
            allowlist.write_bytes(sentinel)
            with contextlib.redirect_stdout(io.StringIO()):
                _write_baseline()
            after = allowlist.read_bytes()
        self.assertEqual(sentinel, after,
                         "--write-baseline wrote to the hand-edited pin")
        self._assert_the_real_files_are_untouched(real_baseline, real_allowlist)

    def test_write_baseline_actually_writes_the_measurement(self) -> None:
        """The other direction of the sentinel above: that the command writes what it says.

        Dropping `BASELINE_PATH.write_text(...)` from `_write_baseline`, writing to another path,
        or writing an EMPTY or an inflated measurement each left the whole file green — measured
        at `c131639`. `TokenRatchetTests` turns the NEXT run red on a baseline written wrong; this
        row catches it in the run that writes it. The summary line is no witness either —
        `_write_baseline` prints it from a re-read of the file, so on a clean tree it prints the
        right numbers whether or not the write happened.

        Both sides are measured from the SAME tree, so this row says nothing about whether the
        tree matches the recorded baseline — `TokenRatchetTests` does, once.
        """
        real_baseline, real_allowlist = BASELINE_PATH.read_bytes(), ALLOWLIST_PATH.read_bytes()
        with self._data_files_in_a_temporary_directory() as (baseline, _):
            baseline.write_bytes(b'{"token_counts": {"docs/sentinel.md": {"fortran": 1}}}\n')
            with contextlib.redirect_stdout(io.StringIO()):
                _write_baseline()
            written = json.loads(baseline.read_text(encoding="utf-8"))
        self._assert_the_real_files_are_untouched(real_baseline, real_allowlist)
        # Equality against a fresh measurement, not a shape check: an empty write and a write of
        # `measure()` with every count raised are both well-formed, and both are the shape that
        # blesses a tree nobody measured.
        self.assertEqual(measure(), written)
        self.assertNotIn("docs/sentinel.md", written["token_counts"])

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

    #: Lifted to a module function so `BaselineComparisonTests` builds its tree the same way.
    _tree = staticmethod(_synthetic_tree)

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
        # Reading the module text, because the failure being prevented is a call to the WRONG
        # registry function, which no fixture can show without a second language backend.
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
            "a signature gate guards a language backend on membership alone "
            f"(line(s) {[n.lineno for n in membership_calls]})")

    # --- the §5.1 signature gates dispatch (issue #289, R4-b PR-3) ---------------------------

    @staticmethod
    def _patched(record: "registry.Backend"):
        return mock.patch.dict(registry._BACKENDS, {(record.axis, record.backend_id): record})
    #
    # Until that PR the §5.1 helpers imported ONE language backend by name, guarded by
    # `_signature_backend_refusal` and the `_SIGNATURE_HELPERS_BACKEND_ID` constant, whose rows
    # pinned the constant against the imports and the refusal's two grounds. Both are deleted
    # with the mechanism: the gates now reach the `signatures` capability of the pipeline's
    # target language through `_language_module`. The rows below pin the SUCCESSOR's properties
    # — no language backend is imported by name, a language without the capability is refused,
    # a language with it is dispatched to its OWN module, and the helper's two grounds each have
    # their own message.

    def test_the_validator_imports_no_language_backend_by_name(self) -> None:
        """A SET IDENTITY, not a sample: every `tools.backends.language.<id>` module the validator
        imports is collected from its source, and the set must be empty — the registry is the
        only way it reaches a language (`DirectImportPinTests` pins the whole allowlist; this is
        the property the retired `_SIGNATURE_HELPERS_BACKEND_ID` row stood for)."""
        source = Path(vps.__file__).read_text(encoding="utf-8")
        prefix = f"{BACKEND_PACKAGE}.language."
        self.assertEqual(
            set(), {name for name in _imported_modules(source) if name.startswith(prefix)})

    def test_each_language_module_ground_is_reached_by_the_input_it_is_for(self) -> None:
        """`_language_module`'s three answers, each by its own input: no language (nothing
        appended — the unresolved target is reported elsewhere), a language without the
        capability (the registry's reason), and a declared capability whose package cannot be
        loaded (a violation, never an escaping exception)."""
        sink: list[str] = []
        self.assertIsNone(vps._language_module(None, "signatures", "subj", sink))
        self.assertEqual([], sink)

        non_member = next(
            c for c in ("c", "cpp", "rust", "zz_not_a_language")
            if registry.unsupported_reason("language", c) is not None)
        self.assertIsNone(vps._language_module(non_member, "signatures", "subj", sink))
        self.assertEqual(1, len(sink), sink)
        self.assertIn(f"'{non_member}' declares no 'signatures' capability", sink[0])
        self.assertIn(str(registry.missing_capability_reason(
            "language", non_member, "signatures")), sink[0])

        record = registry.Backend(
            "language", "zz_unloadable", "zz_module_that_does_not_exist",
            backend_provides=frozenset({"signatures"}))
        sink = ["a sibling gate already found this"]
        with self._patched(record):
            self.assertIsNone(vps._language_module("zz_unloadable", "signatures", "subj", sink))
        self.assertEqual("a sibling gate already found this", sink[0])
        self.assertTrue(any("could not be loaded" in v for v in sink[1:]), sink)
        # ... and a package that loads but does not carry what its record claims: the registry
        # raises its own typed refusal (not an ImportError), and that lands as a violation too.
        import sys
        import types
        hollow = registry.Backend("language", "zz_hollow_sig", "zz_hollow_sig_pkg",
                                  backend_provides=frozenset({"signatures"}))
        sink = []
        with mock.patch.dict(sys.modules, {"zz_hollow_sig_pkg": types.ModuleType("x")}), \
                self._patched(hollow):
            with self.assertRaises(registry.BackendNotExtracted):
                registry.capability_module("language", "zz_hollow_sig", "signatures")
            self.assertIsNone(vps._language_module("zz_hollow_sig", "signatures", "subj", sink))
        self.assertTrue(any("could not be loaded" in v for v in sink), sink)

        # ... and the live language answers with its own module, appending nothing.
        sink = []
        self.assertIs(registry.capability_module("language", "fortran", "signatures"),
                      vps._language_module("fortran", "signatures", "subj", sink))
        self.assertEqual([], sink)

    def test_the_generate_signature_gate_dispatches_to_the_targets_language(self) -> None:
        """Driven THROUGH `_validate_generated_signatures`, with the pipeline's target naming a
        synthetic language: one that does not declare `signatures` is refused, and one that does
        is rendered by ITS module, observed by a sentinel only that module can produce. With one
        real language backend no other observer can tell dispatch from coincidence (the retired
        rows asserted it by pinning a constant against the imports instead)."""
        import sys
        import tempfile
        import types

        from tools.tests.test_validate_pipeline_semantics import (
            InfrastructureGeneratedSignatureGateTests as Gate,
        )

        def run(language: str) -> list[str]:
            with tempfile.TemporaryDirectory() as t:
                tmp = Path(t)
                execution = Gate()._seed(tmp, source=Gate._GOOD_SOURCE, language=language)
                violations: list[str] = []
                vps._validate_generated_signatures(
                    tmp, execution, [execution.pipeline_dir / "src" / "hx_model.f90"],
                    violations)
                return violations

        refused = run("zz_no_signatures")
        self.assertTrue(any("declares no 'signatures' capability" in v for v in refused),
                        refused)

        own = types.ModuleType("zz_sig_lang")
        own.signatures = types.ModuleType("zz_sig_lang.signatures")
        own.signatures.load_structured_signatures = lambda body: ({}, "ZZ_OWN_RENDERER")
        record = registry.Backend("language", "zz_sig", "zz_sig_lang",
                                  backend_provides=frozenset({"signatures"}))
        with mock.patch.dict(sys.modules, {"zz_sig_lang": own}), self._patched(record):
            dispatched = run("zz_sig")
        self.assertTrue(any("ZZ_OWN_RENDERER" in v for v in dispatched), dispatched)
        # ... and the incumbent still answers for itself (the fixture's source is faithful).
        self.assertEqual([], run("fortran"))

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
        dispatched = {"control_file", "build_execute", "runner_render", "lint", "lint_rules",
                      "execution", "execution_env", "perf_facts", "bundle_facts",
                      "syntax_check", "syntax_promotions", "prompt_fragments",
                      "checks_abi", "source_reading", "signatures", "parallel_directives",
                      "interface_header"}
        # `interface_header` joined them with issue #289 (R4-b PR-4): the conductor asks
        # `provides` / `capability_module` for it when it writes a bundle's files, and when it
        # names the files it authors (`_host_rendered_src_names`).
        # `lint` joined them when the first linter's argv moved into its package (issue #111):
        # `mcp_servers/build_runtime_server.py`'s `_lint_preset_command` asks `capability_module`
        # for it. Note the asymmetry the instrument's own comment below records — the conductor's
        # `{"lint": ...}` dict key is NOT what makes it dispatched, and never was.
        #
        # `lint` reached every linter that HAS an argv when issue #120 moved `cppcheck` and
        # `ruff` too; `mixed` is the one linter record still answering from `core_provides`, and
        # it has no argv of its own to move (it is a composite).
        #
        # `lint_rules` joined them with issue #169: `Conductor._lint_rules_document` asks
        # `capability_module` for it when assembling a pure `harness`-shape producer's context.
        # It is a SECOND capability on the same axis and the same submodule as `lint`, and the
        # split is the point — every registered linter RUNS, so `lint` would have answered for
        # all of them and left the dispatch to a `getattr`, which is what this registry's
        # `capability_module` exists to prevent. Only `fortitude` declares it today.
        #
        # `execution`, `execution_env` and `perf_facts` joined them with issue #289 (R4-b PR-1):
        # the launch gate (`target_profile.hardware_violations`) asks all three, and
        # `tools/host_execution.launch_shape` asks the first two when `Validate.execute` launches
        # the binary.
        #
        # `syntax_check`, `syntax_promotions` and `bundle_facts` joined them with issue #289
        # (R4-b PR-2): the syntax-only adapter moved out of the build-runtime server into the
        # compiler's package and the language's, and the conductor's `Generate.gate`, the
        # server's `run_syntax_check`, the post_generate certification and the default-compiler
        # readers ask the registry for them.
        #
        # `source_reading`, `signatures` and `parallel_directives` joined them with issue #289
        # (R4-b PR-3): the validator's source gates, its §5.1 signature gates and the
        # dependency-fact resolver ask the first two of the target language
        # (`_language_module`, `_resolve_dependency_facts`), and the Generate presence floor
        # asks the third of the target's parallel backend.
        #
        # The rest are declaration-only TODAY: they are how their records answer `implemented`,
        # and they gain a dispatch when their ledger area lands.
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
            # which `_missing_toolchain_capability_clauses` (deleted in R4-a PR-3) passed through a
            # tuple it looped over;
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
        # SEQUENCE literals only. Extending this to dict VALUES was tried and reverted: it
        # flagged the validator's language -> linter table, which was a legitimate structure
        # carrying a different fact (which linter a language is linted with). That fact is the
        # linter backends' own declaration since issue #289 (R4-b PR-2), answered by
        # `registry.linter_for_language`, and its drift is pinned below.
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

    def test_the_language_to_linter_answer_is_the_linters_own_declaration(self) -> None:
        """Which linter a language is linted with is declared by each linter (`LANGUAGES` in its
        `lint` module) and answered by `registry.linter_for_language` (issue #289, R4-b PR-2).

        It was a table in the validator whose VALUES could drift from the registry — review
        measured dropping the `ruff` member leaving the table producing `ruff` for `python`
        while the gate refused it, suite green. Now: every implemented language has an answer,
        every answer is an implemented linter, and no language is declared by two linters."""
        implemented = set(registry.implemented_backend_ids("linter"))
        for language in registry.implemented_backend_ids("language"):
            with self.subTest(language=language):
                linter = registry.linter_for_language(language)
                self.assertIsNotNone(
                    linter, f"implemented language '{language}' has no linter declaring it, so "
                    "its Generate.gate lint check fails closed on every node")
                self.assertIn(linter, implemented)
        declared: dict[str, list[str]] = {}
        for linter in registry.implemented_backend_ids("linter"):
            if "lint" not in registry.get("linter", linter).backend_provides:
                continue
            for language in registry.capability_module("linter", linter, "lint").LANGUAGES:
                declared.setdefault(language, []).append(linter)
        self.assertEqual({}, {k: v for k, v in declared.items() if len(v) > 1})
        self.assertIn(registry.linter_for_language("mixed"), implemented)

    def test_compiled_is_the_backends_declaration(self) -> None:
        """`registry.is_compiled_language` answers the language backend's `COMPILED`, not the
        presence of bundle facts (round 2, issue #289: ignoring the flag survived — no backend
        declared `COMPILED = False`). A value with no bundle facts is not compiled either."""
        from tools.backends.language.fortran import bundle
        self.assertTrue(registry.is_compiled_language("fortran"))
        with mock.patch.object(bundle, "COMPILED", False):
            self.assertFalse(registry.is_compiled_language("fortran"))
        self.assertFalse(registry.is_compiled_language("c"))

    def test_every_syntax_adapter_carries_the_contract_its_readers_use(self) -> None:
        """The `syntax_check` adapter contract is what `run_syntax_check`, the gate and the
        post_generate certification read off the module (round 2, issue #289: it was written
        down nowhere, and an adapter missing `STANDARD_SPELLING_EXAMPLE` would pass every row and
        turn the canary's fail_closed remedy into an AttributeError). Asked of every compiler
        that declares the capability, and the language it names must declare
        `syntax_promotions`."""
        required = ("EXECUTABLE", "LANGUAGE", "VERSION_ARGV", "CANARY_SOURCE",
                    "CANARY_FILENAME", "STANDARD_SPELLING_EXAMPLE", "argv")
        compilers = [c for c in registry.backend_ids("compiler")
                     if registry.provides("compiler", c, "syntax_check")]
        self.assertTrue(compilers)
        for compiler in compilers:
            with self.subTest(compiler=compiler):
                module = registry.capability_module("compiler", compiler, "syntax_check")
                self.assertEqual([a for a in required if not hasattr(module, a)], [])
                self.assertTrue(registry.provides("language", module.LANGUAGE,
                                                  "syntax_promotions"))
                facts = registry.capability_module("language", module.LANGUAGE,
                                                   "syntax_promotions")
                self.assertTrue(module.CANARY_FILENAME.endswith(tuple(facts.SOURCE_SUFFIXES)))

    #: What each LANGUAGE capability's readers take off its module (issue #289, R4-b PR-2;
    #: round 3 found `prompt_fragments.runner_output_document` stated nowhere and checked by
    #: nothing — a second language missing it passes the launch gate, which asks `provides`
    #: only, and dies unnamed at the harness producer's context assembly).
    _LANGUAGE_CAPABILITY_CONTRACT: ClassVar[dict[str, tuple[str, ...]]] = {
        "bundle_facts": ("SOURCE_EXTENSIONS", "COMPILER_SELECTOR_FAMILIES", "IDENTIFIER_MAX",
                         "IDENTIFIER_PATTERN", "DEFAULT_COMPILER", "MANDATORY_SYNTAX_COMPILER",
                         "COMPILED", "model_basename", "checks_basename", "runner_basename"),
        "syntax_promotions": ("SOURCE_SUFFIXES", "PROMOTED_WARNINGS", "compile_order"),
        "prompt_fragments": ("fragments", "runner_output_document",
                             "EXEMPLAR_GATE_DRIFT_NOTE"),
        "checks_abi": ("document",),
        "runner_render": ("render_runner", "assert_harness_pin", "ir_content_violations",
                          "CHECKS_PUBLIC_NAMES", "checks_abi_dummy_violation",
                          # Issue #289, R4-b PR-4: the checks-ABI remedies the bundle layer and
                          # the validator show a leaf, in the runner language's words.
                          "checks_abi_publication_violation",
                          "bound_state_publication_violation", "hidden_bound_state_remedy",
                          "state_binding_module_reason", "state_binding_storage_reason"),
        # Issue #289, R4-b PR-3: what the validator's source gates, the bundle acceptance layer
        # and the make control-file gate take off a language's source reader; what the §5.1
        # gates and the dependency-fact resolver take off its signature module; and what the
        # build system's control-file renderer takes off its control-file rules.
        "source_reading": (
            "model_source_gates", "model_source_not_found_violation",
            "checks_module_declaration_violations", "checks_module_abi_facts",
            "unpublished_bound_state", "checks_harness_isolation_violations",
            "validate_dependency_operations", "validate_runner_json_serialization",
            "validate_runner_snapshot_filenames", "published_subroutines", "counted_loops",
            "source_module_deps", "MODULE_SOURCE_SUFFIXES", "MODULE_ARTIFACT_SUFFIX",
            # R4-b PR-4: the component-surface gate's remedies.
            "published_operation_missing", "published_operation_extra"),
        "signatures": (
            "LANGUAGE_DISPLAY_NAME", "SignatureParseError", "load_structured_signatures",
            "render_signatures", "render_symbol", "render_interface", "render_module_parameter",
            "validate_module_parameter", "parse_interface_stanzas", "stanza_line_list",
            "stanza_line_set", "generated_source_violations", "published_interface",
            "prefixed_procedures",
            # R4-b PR-4: how the dependency-fact renderer shows a consumer those interfaces.
            "procedure_interface", "dependency_operations_header", "prototype_heading",
            "argument_detail_lines"),
        "control_file": ("rules", "READS_ARCHITECTURE"),
    }

    #: The same, for the build-system and parallel capabilities a package carries (issue #289,
    #: R4-b PR-3): the conductor's renderers and failure classifier, the validator's control-file
    #: and quality-check gates, the runtime's in-source placement, and the presence floor.
    _OTHER_CAPABILITY_CONTRACT: ClassVar[dict[tuple[str, str], tuple[str, ...]]] = {
        ("build_system", "control_file"): (
            "CONTROL_FILE_BASENAME", "BUILDS_IN_SOURCE", "QUALITY_CHECK_PRESETS", "targets",
            "render_node", "render_from_graph", "classify_build_failure", "validate_src_dir",
            "validate_test_no_relink", "validate_test_invokes_cases"),
        ("parallel", "parallel_directives"): ("presence_floor", "lowering_plan_declines"),
    }

    def test_every_other_package_capability_carries_the_contract_its_readers_use(self) -> None:
        for (axis, capability), names in self._OTHER_CAPABILITY_CONTRACT.items():
            values = [v for v in registry.backend_ids(axis)
                      if capability in registry.get(axis, v).backend_provides]
            self.assertTrue(values, (axis, capability))
            for value in values:
                with self.subTest(axis=axis, capability=capability, value=value):
                    module = registry.capability_module(axis, value, capability)
                    self.assertEqual([n for n in names if not hasattr(module, n)], [])

    def test_every_language_capability_carries_the_contract_its_readers_use(self) -> None:
        for capability, names in self._LANGUAGE_CAPABILITY_CONTRACT.items():
            languages = [lang for lang in registry.backend_ids("language")
                         if capability in registry.get("language", lang).backend_provides]
            self.assertTrue(languages, capability)
            for language in languages:
                with self.subTest(capability=capability, language=language):
                    module = registry.capability_module("language", language, capability)
                    self.assertEqual([n for n in names if not hasattr(module, n)], [])
        # and every language capability the launch gate requires has a contract row
        from tools.target_profile import LANGUAGE_CAPABILITIES_EVERY_NODE
        self.assertEqual(set(LANGUAGE_CAPABILITIES_EVERY_NODE) - set(
            self._LANGUAGE_CAPABILITY_CONTRACT), set())
        # the documents a language serves are readable and non-empty
        for language in registry.backend_ids("language"):
            if registry.provides("language", language, "checks_abi"):
                self.assertTrue(registry.capability_module(
                    "language", language, "checks_abi").document().strip())
            if registry.provides("language", language, "prompt_fragments"):
                self.assertTrue(registry.capability_module(
                    "language", language, "prompt_fragments").runner_output_document().strip())

    def test_a_language_two_linters_declare_is_refused_not_resolved_by_order(self) -> None:
        import types
        pkg = types.ModuleType("zz_second_fortran_linter")
        pkg.lint = types.ModuleType("zz_second_fortran_linter.lint")
        pkg.lint.LANGUAGES = ("fortran",)
        record = registry.Backend("linter", "zzlint", "zz_second_fortran_linter",
                                  backend_provides=frozenset({"lint"}))
        with mock.patch.dict(sys.modules, {"zz_second_fortran_linter": pkg}), \
                mock.patch.dict(registry._BACKENDS, {("linter", "zzlint"): record}):
            with self.assertRaises(registry.UnsupportedBackend) as ctx:
                registry.linter_for_language("fortran")
        self.assertIn("more than one linter", str(ctx.exception))

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
        # (a) the live tree: the "still inlined" diagnosis, not the "nothing implements it" one.
        # `make`'s `build_execute` is the live instance since issue #289's R4-b PR-3 moved both
        # halves of `control_file` into their packages (it was `language/fortran`'s
        # `control_file` until then).
        with self.assertRaises(registry.BackendNotExtracted) as ctx:
            registry.capability_module("build_system", "make", "build_execute")
        self.assertIn("still carried by the neutral core", str(ctx.exception))

        # (b) a value that declares it in neither set: the other clause
        bare = registry.Backend("language", "zz_bare", "tools.backends.language.fortran")
        with self._patched(bare):
            with self.assertRaises(registry.BackendNotExtracted) as ctx:
                registry.capability_module("language", "zz_bare", "control_file")
        self.assertIn("nothing in this repository implements it", str(ctx.exception))

        # (c) the state a two-axis migration passes through — one axis moved, the other still
        # inlined — which is the state this tree was in until issue #289's R4-b PR-3: the narrow
        # set is what refuses the inlined side, although the capability has a module row.
        inlined_language = registry.Backend(
            "language", "fortran", "tools.backends.language.fortran",
            core_provides=frozenset({"control_file"}),
            backend_provides=registry.get("language", "fortran").backend_provides
            - {"control_file"})
        with self._patched(inlined_language):
            registry._check_declarations()
            self.assertIn("control_file", registry.CAPABILITY_MODULE_ATTR)
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
                         lambda: host_render.render_runner("zz_liar", {}, "bx", "hx", target=_TARGET_PROFILE.doc)):
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
        other_runner.render_runner = lambda ir, spec_id, harness, target: "! rendered by zz_other\n"
        other.runner = other_runner
        record = registry.Backend(
            "language", "zz_other", "zz_other_lang_backend",
            backend_provides=frozenset({"runner_render"}))
        with mock.patch.dict(sys.modules, {"zz_other_lang_backend": other}), \
                self._patched(record):
            self.assertEqual(("zz_only_name",), host_render.checks_public_names("zz_other"))
            self.assertIn("zz_other", host_render.render_runner("zz_other", {}, "bx", "hx", target=_TARGET_PROFILE.doc))
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
        runner.render_runner = lambda ir, spec_id, harness, target: "! zz_second\n"
        runner.ir_content_violations = lambda ir, spec_id, harness: ["zz_second says no"]
        # The checks-ABI remedies are the runner backend's words (issue #289, R4-b PR-4), so a
        # second language states its own, and the gates below must show THOSE.
        runner.checks_abi_publication_violation = (
            lambda spec_id, unpublished, wrong_kind: f"zz_second publishes {unpublished}")
        runner.bound_state_publication_violation = (
            lambda spec_id, hidden: f"zz_second hides {hidden}")
        runner.hidden_bound_state_remedy = lambda hidden: f"zz_second hides {hidden}"
        runner.state_binding_module_reason = lambda module: f"zz_second reads {module}"
        runner.state_binding_storage_reason = lambda variable: f"zz_second imports {variable}"
        runner.checks_abi_dummy_violation = lambda text, spec_id: None
        other.runner = runner
        other.bundle = types.ModuleType("zz_second_lang.bundle")
        other.bundle.SOURCE_EXTENSIONS = (".zz",)
        other.bundle.IDENTIFIER_MAX = 63
        other.bundle.IDENTIFIER_PATTERN = r"^[A-Za-z][A-Za-z0-9_]{0,62}(?![\s\S])"
        # The checks gate names and reads the checks source through the language too (issue
        # #289, R4-b PR-3); this language borrows Fortran's reader, and names its checks source
        # as the fixture below writes it, so the gate reaches the ABI question this row is about.
        from tools.backends.language.fortran import source as fortran_source
        other.bundle.checks_basename = lambda spec_id: f"{spec_id}_checks.f90"
        other.source = fortran_source
        record = registry.Backend(
            "language", "zz_second", "zz_second_lang",
            core_provides=frozenset({"control_file"}),
            backend_provides=frozenset({"runner_render", "bundle_facts", "source_reading"}))
        ir = {
            "meta": {"spec_kind": "component", "spec_id": "bx"},
            "dependency": {"direct_deps": []},
        }
        with mock.patch.dict(sys.modules, {"zz_second_lang": other}), \
                mock.patch.dict(registry._BACKENDS, {("language", "zz_second"): record}):
            # (1) the validator's mirror hands the seam the language the TARGET names
            self.assertEqual("zz_second", vps._m3c_language(ir, "make", "zz_second"))
            self.assertEqual(
                ["zz_second says no"],
                list(host_render.ir_content_violations(
                    vps._m3c_language(ir, "make", "zz_second"), ir, "bx",
                    "harness_fortran_cpu")))
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
            # — the language of a declared target (the Compile is target-free, issue #284)
            with tempfile.TemporaryDirectory() as tmp:
                ir_dir = Path(tmp)
                _declare_target_with_language(ir_dir, "zz_second")
                (ir_dir / "spec.ir.yaml").write_text(json.dumps(ir), encoding="utf-8")
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
            # `language` names the files (the target's); the ABI is still the FILE's language's
            violation = codegen_bundle.m3c_checks_abi_violation(bundle, "bx", language="fortran")
            self.assertIsNotNone(violation)
            self.assertIn("zz_only_abi_name", violation)
            # ...and so is the remedy's wording: the FILE's language states how to publish it.
            self.assertEqual("zz_second publishes ['zz_only_abi_name']", violation)

            # (5) the bound-state remedies are the runner language's words too (issue #289,
            # R4-b PR-4): the bundle layer's, the validator's, and the two binding reasons.
            published_abi = ("module bx_checks\n  private\n  real :: q\n"
                             "  public :: zz_only_abi_name\ncontains\n"
                             "  subroutine zz_only_abi_name()\n  end subroutine\n"
                             "end module bx_checks\n")
            bundle["files"][0]["content"] = published_abi
            bundle["state_bindings"] = [{"state_variable": "q", "storage_symbol": "q"}]
            self.assertEqual("zz_second hides ['q']", codegen_bundle.m3c_checks_abi_violation(
                bundle, "bx", language="fortran"))
            with tempfile.TemporaryDirectory() as tmp:
                src = Path(tmp) / "src"
                src.mkdir()
                (src / "bx_checks.f90").write_text(published_abi, encoding="utf-8")
                violations = []
                vps._validate_checks_source_files(
                    SimpleNamespace(node_key="component/bx@0.1.0"), "zz_second", src, [],
                    violations, bound_state=["q"])
            self.assertIn(f"{src / 'bx_checks.f90'}: zz_second hides ['q']", violations)
            binding = {"state_variable": "q", "capture": "harness_registration",
                       "capability": "state_registration@1"}
            self.assertEqual(
                "state_bindings[0].module must be 'bx_checks' — zz_second reads bx_checks; "
                "got 'elsewhere'",
                codegen_bundle._m3c_state_binding_mismatch(
                    [dict(binding, module="elsewhere", storage_symbol="q")], ["q"], "bx",
                    language="zz_second"))
            self.assertEqual(
                "state_bindings[0].storage_symbol must equal its state_variable 'q' — "
                "zz_second imports q; got 'r'",
                codegen_bundle._m3c_state_binding_mismatch(
                    [dict(binding, module="bx_checks", storage_symbol="r")], ["q"], "bx",
                    language="zz_second"))
            # A runner backend that cannot be LOADED to state the reason still yields the
            # finding, never an exception out of the acceptance layer.
            with mock.patch.object(host_render, "_module", side_effect=ImportError("boom")):
                reason = codegen_bundle._m3c_state_binding_mismatch(
                    [dict(binding, module="elsewhere", storage_symbol="q")], ["q"], "bx",
                    language="zz_second")
            self.assertIn("state_bindings[0].module must be 'bx_checks'", reason)
            self.assertIn("cannot be stated for language 'zz_second'", reason)

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

        from tools.backends.language.fortran import bundle as fortran_bundle
        from tools.backends.language.fortran import source as fortran_source

        empty_pkg = types.ModuleType("zz_pkg_without_runner")  # no `runner` attribute
        # It CAN name and read its sources (since issue #289's R4-b PR-3 the checks gate asks
        # the language for both before anything else), so what this row reaches is the gap it
        # is about: a runner the record claims and the package does not carry.
        empty_pkg.bundle = fortran_bundle
        empty_pkg.source = fortran_source
        record = registry.Backend(
            "language", "zz_hollow", "zz_pkg_without_runner",
            core_provides=frozenset({"control_file"}),
            backend_provides=frozenset({"runner_render", "bundle_facts", "source_reading"}))
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

    def test_the_bundle_abi_gate_refuses_a_file_language_it_cannot_read(self) -> None:
        """`codegen_bundle.m3c_checks_abi_violation` reads the checks file with its LANGUAGE's
        `source_reading` (issue #289, R4-b PR-3): a language that renders a runner but declares
        no source reader is refused with the registry's reason, never read as Fortran and never
        an escaping exception."""
        import sys
        import types

        import tools.codegen_bundle as codegen_bundle

        pkg = types.ModuleType("zz_no_reader_pkg")
        pkg.runner = types.ModuleType("zz_no_reader_pkg.runner")
        pkg.runner.CHECKS_PUBLIC_NAMES = ("case_setup",)
        record = registry.Backend("language", "zz_no_reader", "zz_no_reader_pkg",
                                  backend_provides=frozenset({"runner_render"}))
        bundle = {"files": [{"logical_path": "bx_checks.f90", "role": "checks",
                             "language": "zz_no_reader",
                             "member_node_key": "component/bx@0.1.0",
                             "content": "module bx_checks\nend module bx_checks\n",
                             "modules": ["bx_checks"]}]}
        with mock.patch.dict(sys.modules, {"zz_no_reader_pkg": pkg}), self._patched(record):
            violation = codegen_bundle.m3c_checks_abi_violation(bundle, "bx", language="fortran")
        self.assertIsNotNone(violation)
        self.assertIn("whose sources this repository cannot read", violation)
        self.assertIn(str(registry.missing_capability_reason(
            "language", "zz_no_reader", "source_reading")), violation)
        # ... and a declared reader whose package does not carry it: a refusal, not an exception.
        record = registry.Backend("language", "zz_no_reader", "zz_no_reader_pkg",
                                  backend_provides=frozenset({"runner_render", "source_reading"}))
        with mock.patch.dict(sys.modules, {"zz_no_reader_pkg": pkg}), self._patched(record):
            violation = codegen_bundle.m3c_checks_abi_violation(bundle, "bx", language="fortran")
        self.assertIsNotNone(violation)
        self.assertIn("whose source reader could not be loaded", violation)

    def test_a_backend_that_cannot_be_imported_does_not_empty_the_violation_list(self) -> None:
        """The seam lets a broken import escape as itself — the GATES must not.

        Re-typing an `ImportError` at the seam would tell a leaf to implement what already
        exists, so the seam deliberately does not. But inside `_validate_compile_stage_impl`
        there is no handler, and an escaping exception replaces the sibling gates' actionable
        list with a traceback. Both properties are wanted; the conversion belongs at the gate.
        """
        import tempfile

        import tools.validate_pipeline_semantics as vps

        import sys
        import types

        from tools.backends.language.fortran import bundle as fortran_bundle
        from tools.backends.language.fortran import source as fortran_source

        # (a) a package that does not import at all. Since issue #289's R4-b PR-3 the gate's
        # FIRST reach into the language is its `bundle_facts` / `source_reading`
        # (`_language_module`), so that is the conversion this input meets.
        record = registry.Backend(
            "language", "zz_missing_pkg", "zz_module_that_does_not_exist",
            backend_provides=frozenset({"runner_render", "bundle_facts", "source_reading"}))
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

        # (b) a package that imports and reads its sources, whose runner is broken: the ABI
        # seam raises something other than its typed refusal, and the gate's OWN conversion is
        # what must hold.
        broken_runner = types.ModuleType("zz_broken_runner_pkg.runner")  # no CHECKS_PUBLIC_NAMES
        pkg = types.ModuleType("zz_broken_runner_pkg")
        pkg.runner, pkg.bundle, pkg.source = broken_runner, fortran_bundle, fortran_source
        record = registry.Backend(
            "language", "zz_broken_runner", "zz_broken_runner_pkg",
            backend_provides=frozenset({"runner_render", "bundle_facts", "source_reading"}))
        with mock.patch.dict(sys.modules, {"zz_broken_runner_pkg": pkg}), self._patched(record):
            with self.assertRaises(AttributeError):          # the seam, unchanged
                host_render.checks_public_names("zz_broken_runner")
            with tempfile.TemporaryDirectory() as tmp:       # the gate, converted
                src = Path(tmp) / "src"
                src.mkdir()
                (src / "bx_checks.f90").write_text(
                    "module bx_checks\nend module bx_checks\n", encoding="utf-8")
                violations = ["a sibling gate already found this"]
                vps._validate_checks_source_files(
                    SimpleNamespace(node_key="component/bx@0.1.0"), "zz_broken_runner", src,
                    [], violations)
        self.assertIn("a sibling gate already found this", violations)
        self.assertTrue(any("the backend for language 'zz_broken_runner' could not be loaded"
                            in v for v in violations), violations)

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
            "dependency": {"node_key": "component/bx@0.1.0", "direct_deps": []},
        }
        with self._patched(record):
            with tempfile.TemporaryDirectory() as tmp:
                ir_dir = Path(tmp)
                _declare_target_with_language(ir_dir, "zz_no_pkg")
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
            violation = codegen_bundle.m3c_checks_abi_violation(bundle, "bx", language="fortran")
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
                    lambda: host_render.render_runner("zz_no_renderer", {}, "bx", "hx", target=_TARGET_PROFILE.doc),
                    lambda: host_render.checks_public_names("zz_no_renderer"),
                    lambda: host_render.ir_content_violations(
                        "zz_no_renderer", {}, "bx", "hx"),
                    lambda: host_render.assert_harness_pin(
                        "zz_no_renderer", {}, "bx", "hx", [], "")):
                with self.assertRaises(host_render.RunnerRenderUnavailable) as ctx:
                    call()
                self.assertEqual(expected, str(ctx.exception))


def _print_what_is_about_to_be_blessed(root: Path | None = None,
                                       baseline_path: Path | None = None) -> None:
    """What `--write-baseline` is about to absorb, printed before it absorbs it.

    PERMANENT: it outlived the ratchet's 2026-09 freeze, which is why it was written with no
    issue marker. It exists because "check first, judge, then write" is prose, and prose is
    followable by half: `--write-baseline` alone succeeded with no diff, no entries, and no word
    about having skipped the check, so an unjudged regeneration and a judged one printed the same
    thing. This does not move the judgement into the argv — the pair is still refused — it puts
    the material in front of whoever typed the command.

    `root` and `baseline_path` exist so a witness can drive this on a SYNTHETIC tree, and that is
    not a convenience: a row that drives it on the REAL tree and asserts what it prints is a
    second tree-versus-baseline comparison, one whose assertions are about the preview. One was
    written here during the freeze and shipped for one commit, and turned `pytest tools/tests/`
    red on any pull request that moved a sampled token.
    """
    root = root or REPO_ROOT
    try:
        previous = _load_baseline(baseline_path)["token_counts"]
        if not isinstance(previous, dict) or not all(
                isinstance(v, dict) for v in previous.values()):
            # A well-formed JSON of the wrong SHAPE reached `grown_entries` and raised there,
            # outside this handler: `{"token_counts": []}` took the command down the same way the
            # narrow `except` did. The shape check brings it inside.
            raise TypeError(f"token_counts is {type(previous).__name__}, not a mapping of mappings")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # NOT a gate. The preview exists to disclose, and a preview that refuses to run must not
        # take the write down with it: an unreadable baseline — a conflict-marked merge of two
        # regenerations, a truncated write — is exactly the state whose only documented recovery
        # IS this command, and raising here made the recovery print a traceback while
        # `origin/main` regenerated the file. The narrow `except FileNotFoundError` that did that
        # was written on this branch and shipped for one commit.
        print(f"the recorded baseline could not be read ({type(exc).__name__}: {exc}), so this "
              f"write is a starting point rather than a change, and nothing below is a comparison")
        return
    measured = token_counts(root)
    scanned = {p.relative_to(root).as_posix() for p in neutral_core_files(root)}
    grown = grown_entries(previous, measured)
    stale = stale_entries(previous, measured, scanned)
    if not grown and not stale:
        print("the tree already matches the baseline: this regeneration changes nothing")
        return
    # Growth and staleness take OPPOSITE answers under §Decision Criteria — growth is withdrawn,
    # staleness is what a migration looks like — and the two entry lines are the same shape, so
    # one flat list said nothing about which answer each row wants. Separate headings, and no claim
    # that this output IS the check: `--check-baseline` is a separate invocation with its own
    # exit code, and the ledger procedure records that one.
    print(f"{WRITE_FLAG} is about to bless {len(grown)} grown and {len(stale)} stale entries. "
          f"Each is a judgement by docs/BACKEND_BOUNDARY.md §Decision Criteria; this listing is "
          f"a disclosure, not a substitute for {CHECK_FLAG} and its exit code:")
    if grown:
        print(f"  GROWN ({len(grown)}) — backend knowledge the neutral core did not carry before:")
        for entry in grown:
            print(f"    {entry}")
    if stale:
        print(f"  STALE ({len(stale)}) — counts the tree no longer reaches, the migration shape:")
        for entry in stale:
            print(f"    {entry}")


def _write_baseline(root: Path | None = None, baseline_path: Path | None = None) -> None:
    root = root or REPO_ROOT
    baseline_path = baseline_path or BASELINE_PATH
    _print_what_is_about_to_be_blessed(root=root, baseline_path=baseline_path)
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(json.dumps(measure(root), indent=2, sort_keys=True) + "\n",
                             encoding="utf-8")
    data = _load_baseline(baseline_path)
    total = sum(sum(v.values()) for v in data["token_counts"].values())
    print(f"wrote {baseline_path}: "
          f"{len(data['token_counts'])} files, {total} sampled occurrences "
          f"(the direct-import allowlist is NOT written by this command; "
          f"{len(_load_allowlist())} modules recorded, edit "
          f"{ALLOWLIST_DISPLAY} by hand)")


#: What the two commands may not be composed into. `--check-baseline` reports; `--write-baseline`
#: blesses whatever the tree currently says. The documented order (this module's docstring,
#: `docs/BACKEND_BOUNDARY.md` §Enforcement) puts a HUMAN JUDGEMENT between them, so a single
#: invocation asking for both is not an order — it is the judgement skipped. Under `elif` the pair
#: wrote and exited 0, which reads in a pull request as a check that passed.
BOTH_COMMANDS_MESSAGE = (
    f"{CHECK_FLAG} and {WRITE_FLAG} cannot run in one invocation: the first reports what "
    "the tree has grown or shed, the second blesses it, and the step between them is a judgement "
    "by docs/BACKEND_BOUNDARY.md §Decision Criteria that no argv can express. Run "
    f"`{CHECK_FLAG}`, read every entry, then run `{WRITE_FLAG}` if the judgement says to.")


def _dispatch(argv: list[str]) -> int | None:
    """The module's command line. `None` means "not a command" — the caller runs the tests.

    Separated from `__main__` so the refusal above and the exit codes are witnessable without a
    subprocess; the subprocess witnesses remain, because a function returning the right number
    says nothing about what `__main__` does with it.
    """
    write = WRITE_FLAG in argv
    check = CHECK_FLAG in argv
    if write and check:
        print(BOTH_COMMANDS_MESSAGE, file=sys.stderr)
        return 2
    if not write and not check:
        return None
    # In command mode every other argument is meaningless, and silence over one is how
    # `--write-baseline --check-baselin` became a write with no refusal and no comparison. Only
    # here: with no command this returns None and `unittest.main` reads argv as it always has.
    extra = [a for a in argv[1:] if a not in (WRITE_FLAG, CHECK_FLAG)]
    if extra:
        print(f"unrecognised argument(s) {extra} beside a command; this module's commands take no "
              f"other argument, and a mistyped one — {WRITE_FLAG} {CHECK_FLAG[:-1]} — would "
              f"otherwise run one command silently while you asked for two", file=sys.stderr)
        return 2
    if write:
        _write_baseline()
        return 0
    return _check_baseline()


if __name__ == "__main__":
    _rc = _dispatch(sys.argv)
    if _rc is None:
        unittest.main()
    sys.exit(_rc)
