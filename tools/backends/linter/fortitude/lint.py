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

`--isolated` is load-bearing and not cosmetic: without it the tool discovers a `fortitude.toml`
next to the sources it is checking, and that file can switch rules off. Measured on 0.8.0 and
0.9.2 alike, a neighbouring `[check] ignore=[...]` turns a failing tree green. A leaf cannot
write one today (the output manifest refuses a `.toml` under a directory entry —
`tools/hooks/common.py`'s `_ALLOWED_BYPRODUCT_EXTENSIONS`), so this closes an operator-side and
future-leaf-side channel rather than a reachable `leaf shortcut`.

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
#: Derived, not invented: it is 0.8.0's default set minus `OB001` (see the module docstring).
#: Measured to RESOLVE to exactly these 40 codes on 0.8.0, 0.9.0 and 0.9.2 — i.e. no redirect
#: and no silent drop anywhere in the supported range.
#:
#: Changing this set changes what a certification means. A new vendor default does NOT enter it
#: by being released; someone adds the code here, and the documents that state the rule to a leaf
#: are checked against this tuple (`tools/tests/test_linter_fortitude.py`).
RULE_CODES: tuple[str, ...] = (
    "C001", "C002", "C003", "C011", "C051", "C061", "C071", "C072", "C081", "C091",
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

#: How the set is imposed. `--isolated` first: it is what makes the verdict independent of a
#: config file that happens to sit next to the sources (module docstring).
CHECK_FLAGS: tuple[str, ...] = ("--isolated", "--select", ",".join(RULE_CODES))

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
