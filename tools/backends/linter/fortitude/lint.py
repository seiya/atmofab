#!/usr/bin/env python3
"""The `lint` capability of the `fortitude` linter backend.

What this module is for: making a `Generate.gate` lint verdict a function of the SOURCE and a
DECLARED rule set, instead of a function of whichever linter build the host happens to have.
Before it, the gate ran `fortitude check .` and inherited that build's compiled-in default set.
The vendor turned 18 preview rules on by default in 0.9.0 — `S241` among them — and every
finding it produced landed in the host-rendered `*_runner.f90`, a file no leaf can edit. The node
could not pass on any machine with a current linter, and it burned the whole `Generate` retry
budget discovering that (issue #110; the requirement is issue #111).

The declaration is three facts and nothing else:

* `RULE_CODES` — the rule set the gate applies. This is the ONE definition; the documents are
  checked against it (`docs/backends/linter/fortitude/RULES.md`, and the leaf-read sites listed
  there).
* `CHECK_FLAGS` — how that set is imposed on the tool.
* `MIN_VERSION` / `BELOW_VERSION` — the versions the set was measured against, which the
  launch-time host probe refuses outside of (`tools/host_prerequisites.py`).

MEASURED, 2026-08-27, on 0.7.5 / 0.8.0 / 0.9.0 / 0.9.2 installed side by side. The full table is
in the backend document; the four facts that decided the shape of this file:

* `--select` is validated by the argument parser, so a code the installed build does not know is
  a hard error (exit 2) with nothing checked. The declared set therefore has to hold on EVERY
  supported version, not just on the newest.
* `OB001` (`statement-function`) is in 0.8.0's and 0.9.x's default set and CANNOT be selected —
  `Rule 'OB001' was removed and cannot be selected`. It is a phantom row of the default listing:
  a removed rule that fires on nothing. Declaring the default set verbatim makes the gate refuse
  to start; omitting it changes no verdict.
* `--select ALL` is not the default set (it drops `OB001` and adds rules nobody reviewed), so it
  is not a spelling of "what we had before". It is not used.
* An old code is silently redirected to its new name (`S051` -> `MOD021`). A declared set is
  therefore checked by RESOLVING it (`--show-settings`) rather than by trusting the spelling.

THREE CHANNELS decide the verdict from somewhere other than the source and this declaration, and
each flag is load-bearing rather than cosmetic. The count is stated because the first version of
this module said TWO and was wrong: it enumerated the channels it had closed rather than the ones
the tool has, which is how the third stayed open.

* A `fortitude.toml` discovered beside the sources switches rules off — measured on 0.8.0 and
  0.9.2 alike, a neighbouring `[check] ignore=[...]` turns a failing tree green. `--isolated`
  closes it. A leaf cannot write that file today (the output manifest refuses a `.toml` under a
  directory entry — `tools/hooks/common.py`'s `_ALLOWED_BYPRODUCT_EXTENSIONS`), so this half is
  an operator-side and future-leaf-side closure rather than a reachable `leaf shortcut`.
* An in-source `! allow(<codes>)` comment suppresses whatever it names, and the leaf AUTHORS the
  source. Measured: one line reading `! allow(C122, C131, C061, PORT011, C003)` immediately above
  a `module` statement takes the whole module from five findings to `All checks passed`, on 0.8.0
  and 0.9.2 alike, under this very `--select`. That is a `leaf shortcut` in the plain sense — the
  shortest route from a failing gate to a reported-done substep runs through one comment — and
  the first version of this module left it open while closing the channel a leaf cannot reach.
  `--ignore-allow-comments` closes it. HOW LOUD the closure is depends on what the directive
  names, and an earlier version of this docstring overstated it: a code OUTSIDE the declared set
  earns `FORT005` (`disabled-allow-comment`), a declared code on otherwise clean source earns
  `FORT002` (`unused-allow-comment`), and a declared code on source that actually violates it
  earns NOTHING of its own — the suppressed finding simply fires. So the leaf-facing rule is
  "write none", not "you will be told"; the suppressed finding firing is itself the signal in
  the case a leaf would actually write one.
* A `.gitignore` hides files from the walk entirely. Measured on 0.8.0 and 0.9.2: a
  `src/.gitignore` reading `*.f90` takes a five-finding tree to `0 files scanned. All checks
  passed!`, exit 0 — quieter than the allow-comment channel, since there is no diagnostic at all.
  `--no-respect-gitignore` closes it. An ANCESTOR file applies too, and an earlier version of
  this bullet said it did not: what decides is WHAT THE PATTERN MATCHES, not where the file sits.
  A pattern matching the sources (`*.f90` at the work-tree root) hides them; a pattern naming a
  directory ABOVE the walk root does not, because the walk starts below it — which is why this
  repository's own `workspace/` entry never made the gate inert (measured on the real layout: 3
  files scanned without the flag). A leaf cannot write either file today — the manifest admits
  only the exact files it declares — but that is a different layer's accident, not this
  declaration's doing.

`C003` is excluded FOR that second flag. It is the one rule this repository's own toolchain makes
unsatisfiable — it wants the F2018 spec-list `implicit none (type, external)`, which is a compile
error under `-std=f2008` — so four leaf-read documents used to MANDATE an `! allow(C003)`
directive on every module, i.e. the rule set required the very channel that had to be closed.
Selecting a rule that must always be suppressed buys nothing and costs the channel. Measured:
with `C003` out of the set, a plain `implicit none` passes on every supported version.

LIMIT of that exclusion, stated rather than implied: a node targeting f2018 would want `C003`
back, and the route is `--target-std <toolchain.standard>` (measured to stop `C003` firing under
`f2008` without dropping it). It is not taken here because it makes the argv node-dependent —
`run_linter` would gain a caller-supplied input — and because the corpus has no such node:
measured over every `spec.ir.yaml` in the tree, `toolchain.standard` is `f2008` (185 documents,
plus 3 spelling it `2008`) and nothing else. `TODO.md` carries it.

What this module deliberately does NOT do: decide the verdict, read findings, or know about the
gate. It states the invocation; `mcp_servers/build_runtime_server.py` runs it and
`tools/workflow_conductor.py`'s `_gate_lint_check` reads the result.
"""

from __future__ import annotations

import re

#: argv[0]. The launch-time host probe reads it out of the argv this module builds, never as a
#: name of its own (`tools/host_prerequisites.py`).
EXECUTABLE = "fortitude"

#: The rule set the `Generate.gate` lint check applies, and the only place it is written.
#:
#: Derived, not invented: it is 0.8.0's default set (41 codes) minus `OB001` and `C003`, both of
#: which `EXCLUDED_RULE_CODES` below states a ground for. Measured to RESOLVE to exactly these 39
#: codes on 0.8.0, 0.9.0 and 0.9.2 — i.e. no redirect and no silent drop anywhere in the
#: supported range.
#:
#: Changing this set changes what a certification means. A new vendor default does NOT enter it
#: by being released; someone adds the code here, and the documents that state the rule to a leaf
#: are checked against this tuple (`tools/tests/test_linter_fortitude.py`).
RULE_CODES: tuple[str, ...] = (
    "C001", "C002", "C011", "C051", "C061", "C071", "C072", "C081", "C091",
    "C092", "C101", "C121", "C122", "C131", "C141",
    "E000", "E001",
    "FORT001", "FORT002", "FORT003", "FORT004", "FORT005",
    "MOD011", "MOD021",
    "OB011", "OB021", "OB031", "OB041", "OB051", "OB061",
    "PORT011", "PORT012", "PORT021",
    "S001", "S061", "S071", "S081", "S091", "S101",
)

#: Codes deliberately left OUT of `RULE_CODES`, with the reason, so a reader asking "why is this
#: not checked" gets an answer here instead of re-deriving it. Not machine-consulted; the set
#: above is what runs.
EXCLUDED_RULE_CODES: dict[str, str] = {
    "C003": (
        "unsatisfiable under this repository's own toolchain — it wants the F2018 spec-list "
        "`implicit none (type, external)`, which is a compile error under `-std=f2008`, the "
        "standard every node in the corpus declares. Selecting it forced an `! allow(C003)` "
        "directive onto every module, which is to say it forced the suppression channel "
        "`--ignore-allow-comments` exists to close. See the module docstring for the route back "
        "(`--target-std`) if an f2018 node ever appears."
    ),
    "OB001": (
        "default-enabled on every supported version and impossible to select — the tool answers "
        "`Rule 'OB001' was removed and cannot be selected`. A removed rule finds nothing, so its "
        "absence changes no verdict."
    ),
    "S241": (
        "the incident rule (issue #110). Preview-only on 0.8.0, default-on from 0.9.0, and every "
        "finding it produces on this tree is in the host-rendered runner, which no leaf can edit. "
        "Making the renderer satisfy it is separate work and is not a reason to enable it here."
    ),
}

#: How the set is imposed. Each flag closes one channel that would otherwise decide the verdict
#: from somewhere other than the source and this declaration; the module docstring enumerates
#: them and what each was measured to do.
CHECK_FLAGS: tuple[str, ...] = (
    "--isolated",
    "--ignore-allow-comments",
    "--no-respect-gitignore",
    "--select", ",".join(RULE_CODES),
)

#: The versions `RULE_CODES` was measured to resolve identically on. Inclusive floor, exclusive
#: ceiling, compared as tuples of integers.
#:
#: The floor is 0.8.0 rather than 0.7.5, which also passed the incident source: 0.7.5 has no
#: `--isolated` flag at all (`error: unexpected argument '--isolated' found`), so the declared
#: invocation cannot run there. The ceiling is a statement about what was measured, not a claim
#: that 0.10.0 breaks — an unmeasured build is refused at launch rather than allowed to decide a
#: certification.
MIN_VERSION: tuple[int, int, int] = (0, 8, 0)
BELOW_VERSION: tuple[int, int, int] = (0, 10, 0)

#: The same range in the spelling an operator types (`docs/RUNBOOK.md` §0-1 quotes it).
SUPPORTED_VERSION_SPEC = ">=0.8,<0.10"

#: What the probe runs to learn the installed version. First line is `fortitude <x.y.z>`.
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
    resolves the way it was measured to, and the whole point of the declaration is that the
    verdict is a function of a KNOWN build. The caller is the launch probe, so the cost of a
    false refusal is an operator message before the first billed leaf, not a dead run.
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
            f"(install {SUPPORTED_VERSION_SPEC}); the declared rule set cannot be imposed on it"
        )
    if version >= BELOW_VERSION:
        return (
            f"{EXECUTABLE} {_spell(version)} is at or above {_spell(BELOW_VERSION)}, which the "
            f"declared rule set has not been measured against (supported: "
            f"{SUPPORTED_VERSION_SPEC}); re-measure and widen the range in "
            f"tools/backends/linter/fortitude/lint.py rather than running unmeasured"
        )
    return None
