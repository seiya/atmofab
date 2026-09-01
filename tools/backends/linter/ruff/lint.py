#!/usr/bin/env python3
"""The `lint` capability of the `ruff` linter backend.

What this module is for: making a `Generate.gate` lint verdict a function of the SOURCE and a
DECLARED rule set, instead of a function of whichever linter build the host happens to have. It
is the second application of that rule (issue #120); the first was `fortitude` (issues #110 /
#111), and the ground is the same one `docs/BACKEND_BOUNDARY.md` §Design Policy states: a lint
rule id is knowledge the neutral core may not hold, so an argv that DECLARES a rule set cannot be
a row of a table in `mcp_servers/`.

Before it, the gate ran `ruff check .` and inherited that build's compiled-in default set. That
set is not stable: MEASURED on four builds installed side by side (2026-09-01), it is 59 rules on
0.14.0 and on 0.15.20 and 413 rules on 0.16.0 and 0.16.5. The move is 372 codes ADDED and 18
REMOVED, a net 354; the breakdown (47 `PYI`, 42 `UP`, 36 `RUF`, 33 `PLE`, 29 `B`, 21 `SIM`, ...)
belongs to the 372, and an earlier version of this sentence attached it to the net. The REMOVALS
are the sharper half and were not stated at all: all 18 are members of `RULE_CODES` below, so the
vendor default dropped 18 of the exact codes this repository certifies against while adding 372
nobody here has reviewed. On one unchanged fixture that shows up as the inherited default LOSING
`E741` and GAINING `SIM117` at 0.16.0. That is
issue #110's failure mode in a second linter, and it is why the set below is declared rather than
inherited.

The declaration is three facts and nothing else:

* `RULE_CODES` — the rule set the gate applies. This is the ONE definition; the document is
  checked against it (`docs/backends/linter/ruff/RULES.md`).
* `CHECK_FLAGS` — how that set is imposed on the tool.
* `MIN_VERSION` / `BELOW_VERSION` — the versions the set was measured against, which the
  launch-time host probe refuses outside of (`tools/host_prerequisites.py`).

DERIVED, NOT INVENTED. `RULE_CODES` is ruff's OWN default set as of 0.15.20 — the last release
before the expansion — which is also what `--select E4,E7,E9,F` resolves to. Measured: the
declared 59 resolve to exactly these 59 on 0.14.0, 0.15.20, 0.16.0 and 0.16.5, i.e. no redirect
and no silent drop anywhere in the supported range. Widening or narrowing what a certification
means is an edit to this tuple, not a release the vendor makes.

MEASURED, and each fact decided part of the shape below:

* `--select` is validated before a file is read, so an unknown code and a REMOVED code (`E999`:
  ``Rule `E999` was removed and cannot be selected.``) are both exit 2 with nothing checked. Both
  are refusals of the invocation, not verdicts about the source — `unusable_invocation_reason`
  below. The STATUS is what holds across the range; the wording does not, and only the status is
  read: an unknown `ZZZ999` is `Unknown rule selector` on 0.15.20 and later and
  `error: invalid value ... for '--select <RULE_CODE>'` on 0.14.0.
* A REMAPPED code is not an error: `PGH001` prints `has been remapped to 'S307'` and the run
  proceeds. A declared set is therefore checked by RESOLVING it (`--show-settings`), never by
  trusting the spelling.

FIVE CHANNELS decide the verdict from somewhere other than the source and this declaration. Each
flag closes exactly one, each was measured with the flag omitted AND with it present, and all
five behave identically on every supported build. The count is stated because the enumeration is
the thing that goes wrong, and it has now gone wrong twice: `fortitude`'s first version
enumerated the channels it had closed rather than the ones the tool has, and the first version of
THIS module said FOUR and omitted the fifth — the built-in exclude list — which is the quietest
of them all.

* A configuration file DISCOVERED by walking upward — `ruff.toml`, `.ruff.toml` or
  `pyproject.toml` — switches the check off. Measured: `exclude = ["*.py"]` takes a five-finding
  tree to `All checks passed`, exit 0, from beside the sources AND from the repository root two
  directories above them. `--isolated` closes it. Two keys are NOT part of this channel and
  saying so is part of the enumeration: a CLI `--select` overrides a discovered `select` and a
  discovered `ignore`, so neither changes a verdict; what does are `exclude` (silent, exit 0) and
  `per-file-ignores` (all five findings to NONE, measured on all four builds). The upward walk is
  why this matters here — the gate's
  `project_dir` is `source/<source_id>/src/` inside the checkout, so a file at the repository root
  would reach it with nothing written near the sources at all.
* An in-source `# noqa` comment suppresses whatever it names. The leaf authors the source — but
  only in the world this preset becomes reachable in: measured against
  `tools/hooks/common.py`'s `_ALLOWED_BYPRODUCT_EXTENSIONS`, a leaf cannot write a `.py` file
  today at all, so this closure is a future-leaf-side one rather than a reachable `leaf shortcut`.
  Registering a `python` language backend is what makes it reachable, and it would have to widen
  that frozenset to do so.
  Measured: `# noqa: F401` on one import takes five findings to four. That is a `leaf shortcut` in
  the plain sense — the shortest route from a failing gate to a reported-done substep runs through
  one comment — and `--ignore-noqa` closes it. This repository takes the same position for every
  linter: `fortitude` closes the same channel with `--ignore-allow-comments`, and `cppcheck` by
  not passing `--inline-suppr`.
* A `.gitignore` hides files from the walk entirely. Measured: `*.py` at the repository root takes
  a five-finding tree to `All checks passed`, exit 0, with no diagnostic at all — quieter than
  either channel above. `--isolated` does NOT close it; `--no-respect-gitignore` does. The file is
  only honoured inside a git repository, and the gate's `project_dir` is inside the checkout, so
  it is.
* THE TOOL'S OWN BUILT-IN EXCLUDE LIST removes a directory from the walk, and `--isolated`
  RESTORES that list rather than emptying it — which is why the first version of this enumeration
  missed it while believing `--isolated` had closed the configuration question. Measured on all
  four builds: the five-finding fixture placed at `dist/probe.py` reports
  `warning: No Python files found under the given path(s)`, `All checks passed!`, exit 0, while
  the same file one directory up reports its five findings. The list is 25 names, byte-identical
  across the supported range (`.bzr .direnv .eggs .git .git-rewrite .hg .ipynb_checkpoints
  .mypy_cache .nox .pants.d .pyenv .pytest_cache .pytype .ruff_cache .svn .tox .venv .vscode
  __pypackages__ _build buck-out dist node_modules site-packages venv`), so the channel is the
  exclusion itself and not its drift. `--exclude=` — the empty list — closes it. An earlier
  version of this bullet added "and makes the file set a function of the walk root alone", which
  the SYMLINK entry below falsifies: the enumeration has now been wrong three times on this
  backend, twice in a sentence written to fix the previous time. The gate's `project_dir` is
  `source/<source_id>/src/`, which holds generated sources and nothing this list is meant to
  protect, so emptying it costs nothing here.
* A STALE CACHE ENTRY answers instead of the checker. Measured on all four builds: the cache key
  carries neither the file size nor a content hash — under `--isolated --select` alone, a file
  cached clean at 6 bytes and then replaced by the 170-byte five-finding fixture with its
  mtime restored still reports `All checks passed`, exit 0. `--no-cache` closes it, and also stops ruff
  writing `.ruff_cache/` into `project_dir`.

  ONE HONEST QUALIFICATION, because the first version of this bullet was written from a run that
  did not carry the whole argv: on every measured build `--ignore-noqa` ALSO defeats a cache read
  (the stale entry is not served once it is present, though `.ruff_cache/` is still written). So
  `--no-cache` is not, today, the only thing between this gate and a stale verdict. It stays
  because a closure that rests on another flag's undocumented side effect is one vendor change
  from open, and because the directory it stops being written into is the leaf's source tree.
  `ruff check --help` documents `--no-cache` as "Disable cache reads" and says nothing about
  `--ignore-noqa` interacting with the cache at all.

WHAT NO FLAG CLOSES, stated because a count of closed channels is not a claim that nothing else
decides the verdict, and because this enumeration has already been wrong once:

* A WALK READ ERROR degrades to a warning and exit 0. Measured on all four builds: `chmod 000` on
  a subdirectory holding the five-finding fixture gives
  `warning: Encountered error: Permission denied (os error 13)`, `All checks passed!`, exit 0 —
  quieter even than the `.gitignore` channel, since the tool says it could not read something and
  then reports success anyway. No flag changes it; only a caller that refuses a run reporting zero
  files could. `fortitude` behaves identically (`0 files scanned, 1 could not be read`, exit 0)
  and `cppcheck` does not (exit 1, which its `unusable_invocation_reason` classifies as a refusal).
  `TODO.md` carries it; it is recorded here rather than closed because the gain is measured to be
  nothing — a leaf that hides a source from the linter has hidden it from the compiler too, and
  the build control file pins its sources by name.
* A SYMLINKED DIRECTORY whose target lies OUTSIDE the walk root is not entered. Measured on all
  four builds: with `walk/pkg -> ../real` and the five-finding fixture in `real/`, the declared
  invocation over `walk/` reports `warning: No Python files found under the given path(s)`,
  `All checks passed!`, exit 0, while the same directory checked directly reports its five. A DIRECTORY SYMLINK IS NEVER ENTERED, which is the accurate statement: with
  `src/link -> src/real`, the run reports the fixture's five findings and names them under
  `real/probe.py`, never `link/`. So an INWARD target is still found — through its real path — and
  only an OUTWARD one becomes invisible. An earlier version of this bullet said "a target inside
  the walk root is followed", which describes a mechanism the tool does not have. A symlinked FILE
  is followed either way. `fortitude` behaves the same and more quietly (`0 files scanned`, no warning);
  `cppcheck` fails closed (exit 1, classified a refusal).

  THIS ONE DOES NOT SHARE THE READ-ERROR ENTRY'S GROUND, which is why it is listed separately: a
  `chmod 000` directory is hidden from the compiler too, but a symlinked directory COMPILES. What
  bounds it today is write authority — the leaf would need a link target outside `project_dir` —
  not the linter. No flag closes it; a caller refusing a run that reports zero files over a
  directory known to be non-empty would, which is the same caller-side check the read-error entry
  needs. `TODO.md` carries both.
* THE EXTENSIONS the walk reads, and what `__init__.py` semantics imply for a package. Unchanged
  by any flag above.

What this module deliberately does NOT do: decide the verdict, read findings, or know about the
gate. It states the invocation; `mcp_servers/build_runtime_server.py` runs it and
`tools/workflow_conductor.py`'s `_gate_lint_check` reads the result.

REACHABILITY, stated so the closures are not oversold. No `spec` node selects `python` today —
`_validate_toolchain_backend_supported` refuses any non-`fortran` language on every
non-`infrastructure` node — so this preset is reached only by `run_linter` in standalone mode.
The channels above are closed while that is true, which is the point: the day a `python`
`infrastructure` backend is registered, the registration is the change a reviewer looks at, not
this file.
"""

from __future__ import annotations

import re

#: argv[0]. The launch-time host probe reads it out of the argv this module builds, never as a
#: name of its own (`tools/host_prerequisites.py`).
EXECUTABLE = "ruff"

#: The rule set the `Generate.gate` lint check applies, and the only place it is written.
#:
#: Derived, not invented: ruff's own default set on 0.14.0 and 0.15.20, byte-identical on both,
#: and what `--select E4,E7,E9,F` resolves to on every supported build. Measured to RESOLVE to
#: exactly these 59 codes on 0.14.0, 0.15.20, 0.16.0 and 0.16.5.
#:
#: Changing this set changes what a certification means. A new vendor default does NOT enter it
#: by being released — 0.16.0 added 372 and none of them are here; someone adds the code here,
#: and the document that states the set to a reader is checked against this tuple
#: (`tools/tests/test_linter_ruff.py`).
RULE_CODES: tuple[str, ...] = (
    "E401", "E402", "E701", "E702", "E703", "E711", "E712", "E713", "E714",
    "E721", "E722", "E731", "E741", "E742", "E743", "E902",
    "F401", "F402", "F403", "F404", "F405", "F406", "F407", "F501", "F502",
    "F503", "F504", "F505", "F506", "F507", "F508", "F509", "F521", "F522",
    "F523", "F524", "F525", "F541", "F601", "F602", "F621", "F622", "F631",
    "F632", "F633", "F634", "F701", "F702", "F704", "F706", "F707", "F722",
    "F811", "F821", "F822", "F823", "F841", "F842", "F901",
)

#: Codes deliberately left OUT of `RULE_CODES`, with the reason, so a reader asking "why is this
#: not checked" gets an answer here instead of re-deriving it. Not machine-consulted; the set
#: above is what runs.
EXCLUDED_RULE_CODES: dict[str, str] = {
    "E999": (
        "impossible to select — the tool answers `Rule 'E999' was removed and cannot be "
        "selected.` and exits 2 with nothing checked. A syntax error is reported anyway, without "
        "being selected, and `E902` (io-error) covers the file-level half."
    ),
    "SIM117": (
        "the rule that made this drift visible (issue #120). Absent from the default set on "
        "0.14.0 and 0.15.20, present from 0.16.0. It is a style preference about nested `with` "
        "statements, not a defect class, and admitting it would mean admitting the other 371 "
        "rules 0.16.0 turned on with it — none of which anyone has reviewed for a generated "
        "source. Enabling any of them is a separate, reviewable edit to `RULE_CODES`."
    ),
    "I001": (
        "import sorting. Never in the set this repository declares, and the single largest "
        "contributor when this repository's own tree is checked under 0.16.x's default set — no "
        "count is written here, because such a count is right only at the revision it was taken "
        "at (TODO.md carries that rule and the command to re-take it). A gate that fails a "
        "generated source on import ORDER burns a regenerate cycle on a property no "
        "certification depends on."
    ),
}

#: How the set is imposed. Each flag closes one channel that would otherwise decide the verdict
#: from somewhere other than the source and this declaration; the module docstring enumerates
#: them and what each was measured to do.
CHECK_FLAGS: tuple[str, ...] = (
    "--isolated",
    "--ignore-noqa",
    "--no-respect-gitignore",
    "--no-cache",
    "--exclude=",
    "--select", ",".join(RULE_CODES),
)

#: The versions `RULE_CODES` was measured to resolve identically on. Inclusive floor, exclusive
#: ceiling, compared as tuples of integers.
#:
#: Both ends state what was MEASURED, not what was found to break, and the difference from
#: `fortitude`'s floor is worth stating: there the floor was forced (0.7.5 has no `--isolated` at
#: all), here it is not. Spot-checked outside the range, 0.9.0 / 0.12.0 / 0.13.3 all accept the
#: declared invocation and resolve it to the same 59 codes — but neither the five closed channels
#: nor the two recorded non-closures were
#: re-measured on them, so they are below the floor. An unmeasured build is refused at launch
#: rather than allowed to decide a certification.
MIN_VERSION: tuple[int, int, int] = (0, 14, 0)
BELOW_VERSION: tuple[int, int, int] = (0, 17, 0)

#: The same range in the spelling an operator types (`docs/RUNBOOK.md` §0-1 quotes it).
SUPPORTED_VERSION_SPEC = ">=0.14,<0.17"

#: What the probe runs to learn the installed version. First line is `ruff <x.y.z>`.
VERSION_ARGV: tuple[str, ...] = (EXECUTABLE, "--version")

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def check_argv(target: str = ".") -> tuple[str, ...]:
    """The full argv the static-lint step runs over `target`."""
    return (EXECUTABLE, "check", *CHECK_FLAGS, target)


def version_argv() -> tuple[str, ...]:
    """The argv that reports the installed version."""
    return VERSION_ARGV


def parse_version(text: str | None) -> tuple[int, int, int] | None:
    """The (major, minor, patch) of a `--version` line, or `None` when it does not carry one.

    The line's shape is the vendor's, so reading it lives here rather than in the neutral probe.
    """
    if not text:
        return None
    match = _VERSION_RE.search(text)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _spell(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def unsupported_version_reason(version_text: str | None) -> str | None:
    """Why the installed build must not decide a certification, or `None` when it may.

    Fails CLOSED on an unreadable version: "we could not tell" is not evidence that the rule set
    resolves the way it was measured to. The caller is the launch probe, so the cost of a false
    refusal is an operator message before the first billed leaf, not a dead run.
    """
    version = parse_version(version_text)
    if version is None:
        return (
            f"{EXECUTABLE}: could not read a version from {version_text!r} "
            f"(ran {' '.join(VERSION_ARGV)}); the declared lint rule set is only measured on "
            f"{SUPPORTED_VERSION_SPEC}, so an unidentified build is refused rather than trusted"
        )
    if version < MIN_VERSION:
        return (
            f"{EXECUTABLE} {_spell(version)} is below the supported floor {_spell(MIN_VERSION)} "
            f"(install {SUPPORTED_VERSION_SPEC}); the declared rule set has not been measured "
            f"against it"
        )
    if version >= BELOW_VERSION:
        return (
            f"{EXECUTABLE} {_spell(version)} is at or above {_spell(BELOW_VERSION)}, which the "
            f"declared rule set has not been measured against (supported: "
            f"{SUPPORTED_VERSION_SPEC}); re-measure and widen the range in "
            f"tools/backends/linter/ruff/lint.py rather than running unmeasured"
        )
    return None


def unusable_invocation_reason(returncode: int, stdout: str, stderr: str) -> str | None:
    """Why this run judged nothing, or `None` when its exit status is a verdict.

    Declaring the rule set made `--select` part of the argv, and the tool validates it before it
    reads a file: a code the installed build does not know, and one it has REMOVED, are both
    exit 2 with nothing checked. Before the declared set the argv carried no `--select` and that
    exit was unreachable. Left unclassified it arrives at the gate as `ok=false` and routes to
    the leaf as lint findings, sending it to fix a file it has no write authority over: the
    unwinnable loop of issue #110 in a new place.

    ONLY THE EXIT STATUS IS READ, and only where it is unambiguous. Exit 1 is the ordinary "there
    are findings" status and is left alone — the same line `fortitude`'s equivalent draws, after
    an earlier version of that one false-refused a legitimate content failure by reading text.
    """
    if returncode in (0, 1):
        return None
    return (
        f"{EXECUTABLE} exited {returncode} without checking anything — the invocation was "
        f"refused, not the source. The declared rule set is imposed with `--select`, which the "
        f"tool validates before it reads a file, so this is a defect in "
        f"tools/backends/linter/ruff/lint.py (or a build outside the supported range), never in "
        f"the source under it: {(stderr or stdout).strip()[:400]}"
    )


def self_check_argv(empty_dir: str) -> tuple[str, ...]:
    """The declared invocation, pointed at a directory holding no source.

    A valid argv over an empty directory exits 0 (`All checks passed`). A code this build does
    not know, or one it has removed, exits 2 — which an empty directory cannot otherwise produce,
    since there is nothing to find. So one run answers "is this build able to impose the declared
    set" with a bare exit status: no output parsing, no fixture, and no dependence on how findings
    are formatted. Run at launch, before any leaf, by `tools/host_prerequisites.py`.
    """
    return check_argv(empty_dir)


def self_check_reason(returncode: int, stdout: str, stderr: str) -> str | None:
    """`None` when the declared set is imposable on this build; the refusal clause otherwise."""
    if returncode == 0:
        return None
    return (
        f"{EXECUTABLE} cannot impose the declared rule set on this host: the invocation exited "
        f"{returncode} over a directory with no source in it, where a usable build reports "
        f"`All checks passed` and exits 0. A code this build does not know, or one it has "
        f"removed, is the measured cause — re-measure the set against this build and record it "
        f"in docs/backends/linter/ruff/RULES.md: {(stderr or stdout).strip()[:400]}"
    )
