#!/usr/bin/env python3
"""The `lint` capability of the `cppcheck` linter backend.

What this module is for: taking the `Generate.gate` lint verdict for `c` / `cpp` out of the
neutral core and stating it here, where the knowledge belongs (issue #120). The argv it declares
is a compiler-family argument list and a check-category selection, which
`docs/BACKEND_BOUNDARY.md` §Design Policy names as knowledge the neutral core may not hold.

WHAT THIS BACKEND DOES NOT ACHIEVE, stated first because the two siblings do achieve it and a
reader will otherwise assume this one does. `fortitude` and `ruff` make a verdict a function of
the source and a DECLARED rule set, because both tools can be handed an explicit set with
`--select`. cppcheck cannot:

* It has no `--select`. `--enable` chooses SEVERITIES, and which checks a severity contains is
  the build's business.
* Its `--errorlist` is not a complete enumeration of what it can report. MEASURED (2026-09-01):
  `uninitvar` and `uninitstring` are absent from `--errorlist` on 2.16.0 and 2.17.1 while
  `uninitvar` still fires on all three supported builds. An earlier version of this work planned
  to refuse, at launch, a build whose `--errorlist` named a check the declaration had no position
  on; that design would have SUPPRESSED `uninitvar` on every build above the floor, on the
  strength of a listing that does not list it.
* Even an exact id set would not give verdict identity, because an id can get BROADER without
  being new. Measured on that same fixture, 2.7 -> 2.16.0 adds `constVariablePointer` (2) and
  `uselessOverride` (1) — both new ids — and takes `passedByValue` from 2 to 3, which is the same
  id firing more often. No argv pins that.

So the property this backend states, and the only one it states, is: **the verdict is a function
of the source, the declared severities, the declared flags, and the BUILD — and the build is
bounded by `MIN_VERSION` / `BELOW_VERSION`, refused at launch before the first leaf.** That is
weaker than its two siblings, it is the strongest the tool admits, and the version range is
therefore load-bearing here in a way it is not for them.

WHAT IS CLOSED, each measured on 2.7 / 2.16.0 / 2.17.1 with the flag present and absent:

* An in-source suppression comment, the channel a leaf can actually write, since a leaf AUTHORS
  the source. `--inline-suppr` is what turns it on, and this argv no longer passes it — the
  removal IS the fix. Measured against the fixture this repository carries (`_DEFECTIVE_SOURCE`
  and `_DEFECTIVE_SOURCE_CPP` in `tools/tests/test_linter_cppcheck.py`):
  `// cppcheck-suppress unusedVariable` above a declaration removes exactly one finding WITH the
  flag and none without it, and the same polarity holds for `// cppcheck-suppress-begin` and
  `// cppcheck-suppress-file`. Dropping the flag closes the whole directive family rather than one
  spelling of it. This repository takes the same position
  for every linter: `fortitude` closes it with `--ignore-allow-comments`, `ruff` with
  `--ignore-noqa`. The previous argv took the opposite position by inheritance, over files the
  leaf writes, with no reason recorded anywhere.
* A REFUSED INVOCATION being read as a verdict about the source. `--error-exitcode=2` separates
  them. Measured identically on all three builds: clean source exits 0, findings exit 2, and a
  directory with no C/C++ source in it, a path that does not exist, and an unknown flag all exit
  1. Under the previous `--error-exitcode=1` those last three were indistinguishable from "there
  are findings", so a conductor that cannot start the linter would have routed the failure to the
  leaf as findings in its own source — issue #110's unwinnable loop.
* The host's own type model. `--platform` defaults to `native`, so `sizeof` and the integer
  widths a check reasons about come from whichever machine runs the gate. `unix64` pins it.
  DECLARED, NOT WITNESSED, and said rather than implied: no fixture measured here reports
  differently under `native`, `unix64` and `unix32`. The flag removes a host input; it does not
  close a demonstrated channel.

CHANNELS MEASURED ABSENT, recorded because a count of closed channels is not a claim that nothing
else decides the verdict, and because a reader coming from the `ruff` backend will expect these:

* cppcheck discovers NO configuration file. Probed on all three builds: `cppcheck-suppressions`,
  `.cppcheck-suppressions`, `suppressions.txt`, `cppcheck.cfg`, `.cppcheck` and `cppcheck.ini`
  beside the sources, and `cppcheck-suppressions` at the repository root — verdict unchanged in
  every case. The probe was live: the same content passed explicitly as
  `--suppressions-list=<file>` does suppress. `compile_commands.json` beside the sources is not
  read either; it needs an explicit `--project=`.
* cppcheck does not read `.gitignore` (measured: `*.c` at the repository root changes nothing),
  and it keeps no cache unless handed `--cppcheck-build-dir`, which this argv does not.

LIMITS, stated rather than implied:

* `--std` is NOT pinned. Its default varies by build, so it is an unpinned input to the verdict.
  The reason it is not pinned is that the standard is a NODE's `toolchain.standard`, and pinning
  one here would invent a policy for a node type the corpus does not contain. The route, the day
  a `c` node exists, is the same one `fortitude`'s `--target-std` note describes and rejects for
  the same reason: it makes the argv node-dependent.
* `--check-level=exhaustive` is rejected by 2.7 (unknown flag, exit 1) and accepted by 2.16.0 and
  2.17.1. It cannot be used while the floor is 2.7.
* The FILE SET is not closed. `cppcheck <dir>` walks for the source extensions it knows; a header
  pulled in by an `#include` is analysed as part of a translation unit but is not itself a walk
  entry.

REACHABILITY. No `spec` node selects `c` / `cpp` today — `_validate_toolchain_backend_supported`
refuses any non-`fortran` language on every non-`infrastructure` node — so this preset is reached
only by `run_linter` in standalone mode. The channel above is closed while that is true, which is
the point: the day a `c` `infrastructure` backend is registered, the registration is the change a
reviewer looks at, not this file.

What this module deliberately does NOT do: decide the verdict, read findings, or know about the
gate. It states the invocation; `mcp_servers/build_runtime_server.py` runs it and
`tools/workflow_conductor.py`'s `_gate_lint_check` reads the result.
"""

from __future__ import annotations

import re

#: argv[0]. The launch-time host probe reads it out of the argv this module builds, never as a
#: name of its own (`tools/host_prerequisites.py`).
EXECUTABLE = "cppcheck"

#: The severities the gate applies, and the only place they are written. `error` is not listed
#: because cppcheck always reports it; these are the ones `--enable` has to ask for.
#:
#: This is the closest thing cppcheck has to a declared rule set, and it is not close: which
#: checks a severity contains is the build's business, which is why the module docstring states
#: the weaker property this backend achieves.
ENABLED_SEVERITIES: tuple[str, ...] = ("warning", "style", "performance")

#: Checks deliberately suppressed, id to the ground for it, imposed as `--suppress=<id>`.
#:
#: EMPTY, deliberately, and empty is a decision rather than an omission. The mechanism works and
#: is measured — `--suppress=<id>` is accepted, and a build that does not know the id tolerates it
#: silently, so one declared list runs across the whole range — but this repository has no ground
#: for suppressing any check today. The two grounds its siblings use do not apply: no check here
#: is unsatisfiable under this repository's own toolchain (`fortitude`'s `C003`), and there is no
#: host-rendered C source for a finding to land in that a leaf could not edit (`fortitude`'s
#: `S241`). Suppressing the checks that differ across the supported range was considered and
#: rejected: it would not have bought identity anyway (`passedByValue` fires more often on a newer
#: build without being a new id), and the list it would have been derived from does not enumerate
#: what the tool reports.
SUPPRESSED_RULE_CODES: dict[str, str] = {}

#: The type model the checks reason under. `native` — the default — is the host's, which is the
#: one input a gate must not take from whichever machine runs it.
PLATFORM = "unix64"

#: The exit status a run with findings ends in. Chosen so that a REFUSED invocation, which exits
#: 1, is distinguishable from a verdict; see `unusable_invocation_reason`.
FINDINGS_EXIT_CODE = 2

#: How the check set is imposed. Each element is answered in the module docstring: what it closes,
#: or that it removes a host input without a measured channel behind it.
CHECK_FLAGS: tuple[str, ...] = (
    f"--error-exitcode={FINDINGS_EXIT_CODE}",
    "--enable=" + ",".join(ENABLED_SEVERITIES),
    f"--platform={PLATFORM}",
    *(f"--suppress={code}" for code in sorted(SUPPRESSED_RULE_CODES)),
)

#: The versions this invocation was measured on. Inclusive floor, exclusive ceiling, compared as
#: tuples of integers.
#:
#: The floor is 2.7 because that is what `apt-get install cppcheck` gives on the platform
#: `docs/RUNBOOK.md` §0-1 documents; the ceiling states what was measured. The INTERIOR of the
#: range is not measured — no installable build between 2.7 and 2.16.0 was available — and that
#: gap matters more here than it would for the two siblings, because for this tool the range is
#: the only thing bounding drift at all. A build outside the range is refused at launch rather
#: than allowed to decide a certification.
MIN_VERSION: tuple[int, int, int] = (2, 7, 0)
BELOW_VERSION: tuple[int, int, int] = (2, 18, 0)

#: The same range in the spelling an operator types (`docs/RUNBOOK.md` §0-1 quotes it).
SUPPORTED_VERSION_SPEC = ">=2.7,<2.18"

#: What the probe runs to learn the installed version. First line is `Cppcheck <x.y[.z]>`.
VERSION_ARGV: tuple[str, ...] = (EXECUTABLE, "--version")

#: Two groups, not three: `Cppcheck 2.7` carries no patch component, so a three-group pattern
#: reads no version at all from the floor build and the launch probe fails it closed.
_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


def check_argv(target: str = ".") -> tuple[str, ...]:
    """The full argv the static-lint step runs over `target`."""
    return (EXECUTABLE, *CHECK_FLAGS, target)


def version_argv() -> tuple[str, ...]:
    """The argv that reports the installed version."""
    return VERSION_ARGV


def parse_version(text: str | None) -> tuple[int, int, int] | None:
    """The (major, minor, patch) of a `--version` line, or `None` when it does not carry one.

    A missing patch component reads as 0, which is what `Cppcheck 2.7` means and what the floor
    comparison needs. The line's shape is the vendor's, so reading it lives here rather than in
    the neutral probe.
    """
    if not text:
        return None
    match = _VERSION_RE.search(text)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3) or 0))


def _spell(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def unsupported_version_reason(version_text: str | None) -> str | None:
    """Why the installed build must not decide a certification, or `None` when it may.

    Fails CLOSED on an unreadable version. That polarity is the same as its siblings', but the
    stake is higher here: for this tool the version range is the ONLY bound on what the gate
    applies, so "we could not tell which build" is "we could not tell what the gate checks".
    """
    version = parse_version(version_text)
    if version is None:
        return (
            f"{EXECUTABLE}: could not read a version from {version_text!r} "
            f"(ran {' '.join(VERSION_ARGV)}); this linter's check set is the build's own, so an "
            f"unidentified build is refused rather than trusted (supported: "
            f"{SUPPORTED_VERSION_SPEC})"
        )
    if version < MIN_VERSION:
        return (
            f"{EXECUTABLE} {_spell(version)} is below the supported floor {_spell(MIN_VERSION)} "
            f"(install {SUPPORTED_VERSION_SPEC}); the declared invocation has not been measured "
            f"against it"
        )
    if version >= BELOW_VERSION:
        return (
            f"{EXECUTABLE} {_spell(version)} is at or above {_spell(BELOW_VERSION)}, which the "
            f"declared invocation has not been measured against (supported: "
            f"{SUPPORTED_VERSION_SPEC}); re-measure and widen the range in "
            f"tools/backends/linter/cppcheck/lint.py rather than running unmeasured"
        )
    return None


def unusable_invocation_reason(returncode: int, stdout: str, stderr: str) -> str | None:
    """Why this run judged nothing, or `None` when its exit status is a verdict.

    cppcheck exits 1 for every way it can fail to run — a directory holding no C/C++ source, a
    path that does not exist, an unknown flag — and that is the SAME status it would use for
    findings under the argv this backend replaced. `--error-exitcode=2` moves findings out of the
    way so this function can classify by exit status alone. Measured on 2.7 / 2.16.0 / 2.17.1:
    clean 0, findings 2, each of the three refusals 1.

    ONLY THE EXIT STATUS IS READ. The alternative — matching `cppcheck: error:` in the output —
    reads a channel the caller's own file names are mixed into, which is the shape
    `.claude/skills/metdsl-enforcement-change` surface 5 exists for.
    """
    if returncode in (0, FINDINGS_EXIT_CODE):
        return None
    return (
        f"{EXECUTABLE} exited {returncode} without judging anything — the invocation was refused, "
        f"not the source. A run that judged the source exits 0 (clean) or "
        f"{FINDINGS_EXIT_CODE} (findings), so this is a defect in "
        f"tools/backends/linter/cppcheck/lint.py, in what was handed to it as a target, or a "
        f"build outside the supported range — never in the source under it: "
        f"{(stderr or stdout).strip()[:400]}"
    )


def self_check_argv(empty_dir: str) -> tuple[str, ...]:
    """The declared invocation, pointed at a directory holding no source.

    UNLIKE its siblings, this answers only "does this build accept the flags this repository
    hands it". It cannot answer "can this build impose the declared set", because there is no
    declared set to impose — the module docstring says why. A build that rejects a flag exits 1
    with nothing analysed, and so does an empty directory, so the two are told apart by
    `self_check_reason` reading which of them the output says.
    """
    return check_argv(empty_dir)


#: What every supported build prints when the argv parsed and the walk simply found nothing. It
#: is the ONE string this module matches, it comes from the tool rather than from any caller —
#: the directory is created by the launch probe, not named by a leaf — and the alternative
#: (treating exit 1 as fatal) would refuse every host, since an empty directory is the one thing
#: the probe can guarantee.
_EMPTY_TARGET_MARKER = "could not find or open any of the paths given"


def self_check_reason(returncode: int, stdout: str, stderr: str) -> str | None:
    """`None` when this build accepts the declared invocation; the refusal clause otherwise."""
    if returncode == 0:
        return None
    if returncode == 1 and _EMPTY_TARGET_MARKER in (stdout + stderr):
        return None
    return (
        f"{EXECUTABLE} does not accept the invocation this repository declares: it exited "
        f"{returncode} over a directory with no source in it, where a usable build reports "
        f"`{_EMPTY_TARGET_MARKER}`. A flag this build does not know is the measured cause — "
        f"re-measure the invocation against this build and record it in "
        f"docs/backends/linter/cppcheck/RULES.md: {(stderr or stdout).strip()[:400]}"
    )
