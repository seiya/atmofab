# `cppcheck` lint invocation (canonical source)

## Purpose
The `Generate.gate` `static lint` check for a `c` / `cpp` node runs the invocation declared by
`tools/backends/linter/cppcheck/lint.py`. This document is the canonical statement of that
invocation for a reader, of what it closes, and — first, because a reader arriving from the two
sibling documents will otherwise assume otherwise — of **what it cannot close**. This file is
checked against the module by `tools/tests/test_linter_cppcheck.py`.

## Requirements
**This backend does NOT make a verdict a function of the source and a declared rule set.** Its
siblings do: `fortitude` and `ruff` both accept an explicit set with `--select`. cppcheck does
not, and three measurements say the gap cannot be closed from the argv:

- It has no `--select`. `--enable` chooses SEVERITIES, and which checks a severity contains is
  the build's business.
- Its `--errorlist` is **not** a complete enumeration of what it can report. Measured:
  `uninitvar` and `uninitstring` are absent from `--errorlist` on 2.16.0 and 2.17.1, while
  `uninitvar` still fires on all three supported builds. An earlier version of this work planned
  to refuse, at launch, a build whose `--errorlist` named a check the declaration had no position
  on; that design would have SUPPRESSED `uninitvar` on every build above the floor, on the
  strength of a listing that does not list it.
- Even an exact id set would not give verdict identity, because a check can get BROADER without
  being new. Measured on the checked-in fixture, 2.7 to 2.16.0 adds `constVariablePointer` (2) and
  `uselessOverride` (1) — two ids that did not exist — and takes `passedByValue` from 2 to 3,
  which is the same id firing more often. No argv pins that.

So the property this backend states, and the only one, is: **the verdict is a function of the
source, the declared severities, the declared flags, and the BUILD — and the build is bounded by
`MIN_VERSION` / `BELOW_VERSION`, refused at launch before the first leaf.** That is weaker than
what the two sibling backends state. The version range is therefore load-bearing here in a way it
is not for them: it is the only bound on drift that exists.

Issue #120's acceptance asked that a verdict be unchanged by a configuration file and by a
suppression comment "or the exception is recorded with its reason". The configuration and
suppression halves are met (below); this section is that recorded exception for the rule-set
half.

## Scope
- The invocation, what each element of it does, and the linter versions it was measured on.
- **Not a leaf-facing checklist, and that is a decision rather than an omission**, for the reason
  and with the coupling `docs/backends/linter/ruff/RULES.md` §Scope states: no `spec` node
  selects `c` / `cpp` today, and `tools/tests/test_linter_cppcheck.py` fails the day one can.
- Not the gate's behaviour on a finding: `docs/workflow/WORKFLOW_CORE.md` is canonical for the
  routing.

## Design Policy
The declared invocation is

```
cppcheck --error-exitcode=2 --enable=warning,style,performance --platform=unix64 <target>
```

Element by element, each checked against `CHECK_FLAGS` by test:

- `--error-exitcode=2` — separates a REFUSED invocation from a verdict. Measured identically on
  2.7, 2.16.0 and 2.17.1: clean source exits 0, findings exit 2, and a directory holding no
  C/C++ source, a path that does not exist, and an unknown flag all exit 1. Under the previous
  `--error-exitcode=1` those last three were indistinguishable from "there are findings", so a
  conductor that could not start the linter would have routed the failure to the leaf as findings
  in its own source — issue #110's unwinnable loop.
- `--enable=warning,style,performance` — the severities the gate applies, declared as
  `ENABLED_SEVERITIES`. `error` is not listed because cppcheck always reports it.
- `--platform=unix64` — pins the type model. The default is `native`, i.e. whichever machine runs
  the gate. **DECLARED, NOT WITNESSED, and said rather than implied**: no fixture measured here
  reports differently under `native`, `unix64` and `unix32`. The flag removes a host input; it
  does not close a demonstrated channel.
- **`--inline-suppr` is ABSENT, and its absence is the fix.** It is what turns an in-source
  suppression comment on — the channel a leaf can actually write, since a leaf authors the
  source. Measured on all three builds against the checked-in fixture:
  `// cppcheck-suppress unusedVariable` above a declaration removes exactly one finding WITH the
  flag and none without it. Dropping the flag closes the whole directive FAMILY rather than one
  spelling of it — `// cppcheck-suppress-begin` / `-end` and `// cppcheck-suppress-file` suppress
  the same finding with the flag on 2.16.0 and 2.17.1, and are inert without it.

  **On the FLOOR build those two spellings are inert either way**, and an earlier version of this
  bullet claimed "the same polarity holds" for them, which is false there. Measured on 2.7 — the
  host's build, and the one `docs/RUNBOOK.md` §0-1 tells an operator to install —
  `-begin`/`-end` and `-file` change nothing WITH `--inline-suppr` present. That makes the closure
  no weaker (nothing is suppressed on 2.7 to begin with), but the sentence a reader would rely on
  when widening the range was wrong about the one build actually installed. This repository takes the same
  position for every linter — `fortitude` closes it with `--ignore-allow-comments`, `ruff` with
  `--ignore-noqa` — and the previous argv took the opposite position by inheritance, over files
  the leaf writes, with no reason recorded anywhere.

## Declared set
`ENABLED_SEVERITIES` in `tools/backends/linter/cppcheck/lint.py`: `warning`, `style`,
`performance`, plus `error`, which cppcheck always reports. There is no id-level set; §Requirements
says why.

`SUPPRESSED_RULE_CODES` is **empty**, and empty is a decision. The mechanism works and is
measured — `--suppress=<id>` is accepted, and a build that does not know the id tolerates it
silently, so one declared list would run across the whole range — but this repository has no
ground for suppressing any check today. The two grounds its siblings use do not apply: no check
here is unsatisfiable under this repository's own toolchain (`fortitude`'s `C003`), and there is
no host-rendered C source for a finding to land in that a leaf could not edit (`fortitude`'s
`S241`). Suppressing the checks that differ across the supported range was considered and
rejected: it would not have bought identity anyway, and the listing it would have been derived
from does not enumerate what the tool reports.

## Limits
- **`--std` is not pinned**, so it is an unpinned input to the verdict: its default varies by
  build. It is not pinned because the standard is a NODE's `toolchain.standard`, and pinning one
  here would invent a policy for a node type the corpus does not contain. The route, the day a
  `c` node exists, is the one `docs/backends/linter/fortitude/RULES.md` describes for
  `--target-std` and rejects for the same reason: it makes the argv node-dependent.
- **`--check-level=exhaustive`** is rejected by 2.7 (unknown flag, exit 1) and accepted by 2.16.0
  and 2.17.1. It cannot be used while the floor is 2.7.
- **The FILE SET is not closed.** `cppcheck <dir>` walks for the source extensions it knows; a
  header pulled in by an `#include` is analysed as part of a translation unit but is not itself a
  walk entry.
- **A directory holding no C/C++ source is a REFUSAL, and on a `mixed` node that is reachable
  without anything being wrong.** Measured on 2.7 and 2.17.1: `cppcheck` over a directory holding
  only `main.f90` exits 1 with `could not find or open any of the paths given`, which
  `unusable_invocation_reason` classifies as a refusal — so
  `tools/workflow_conductor.py`'s `_raise_on_unusable_lint_invocation` raises a transport
  `fail_closed`. `preset=mixed` runs `fortitude` and `cppcheck` in order, and
  `_attribute_lint_findings` re-runs each sub-preset over a HOST-AUTHORED copy of the tree, whose
  basenames on such a node are Fortran. The previous argv did not avoid this: the same exit 1 was
  read as "there are findings" and routed to the leaf as defects in its own source, which is
  worse. Neither behaviour is right, and the correct fix belongs to the composite rather than to
  this backend — a sub-preset should be skipped over a tree holding nothing it can analyse.
  Unreachable today: `_validate_toolchain_backend_supported` refuses `language: mixed` on every
  non-`infrastructure` node, and the corpus is `fortran` throughout. `TODO.md` carries it.

## Supported versions
`>=2.7,<2.18`, declared as `MIN_VERSION` / `BELOW_VERSION` in `lint.py` and quoted by
`docs/RUNBOOK.md` §0-1.

The floor is 2.7 because that is what `apt-get install cppcheck` gives on the platform §0-1
documents; the ceiling states what was measured. **The INTERIOR of the range is not measured** —
no installable build between 2.7 and 2.16.0 was available — and that gap matters more here than
it would for the two siblings, because for this tool the range is the only thing bounding drift
at all.

## Measurement (2026-09-01, three builds installed side by side)

The host's own build is `cppcheck 2.7` (`apt-get install cppcheck`, Ubuntu jammy). The other two
come from PyPI wheels that bundle the binary: `python3 -m pip install --target <dir>
cppcheck==<wheel-version>`, executable at `<dir>/cppcheck/Cppcheck/cppcheck`. Wheel 1.2.1 / 1.3.0
/ 1.4.0 give Cppcheck 2.16.0 and wheel 1.5.1 gives 2.17.1; wheels 1.0.x and 1.1.1 segfault
(exit 139) and are unusable. The host's own install is not modified.

Verdicts under the declared invocation on the fixture this repository carries —
`_DEFECTIVE_SOURCE` and `_DEFECTIVE_SOURCE_CPP` in `tools/tests/test_linter_cppcheck.py`, both
checked in, so every row below is re-takeable by a reader who installs the builds:

| build | checks reported |
| --- | --- |
| 2.7 | `arrayIndexOutOfBounds` 1, `bufferAccessOutOfBounds` 1, `duplicateExpression` 1, `memleak` 1, `nullPointer` 1, `passedByValue` 2, `selfAssignment` 1, `unassignedVariable` 1, `uninitMemberVar` 1, `uninitvar` 1, `unreadVariable` 1, `unusedVariable` 1, `wrongPrintfScanfArgNum` 1, `zerodiv` 1 |
| 2.16.0 | all of the above, with `passedByValue` 3 rather than 2, plus `constVariablePointer` 2 and `uselessOverride` 1 |
| 2.17.1 | identical to 2.16.0 |

The C++ half of the fixture is there because every difference between the builds needs C++ to
appear at all. The three differences are the three shapes §Requirements names: two ids that did
not exist on 2.7, and one id that did and now fires more often.

`--errorlist` id counts: 2.7 = 310, 2.16.0 = 315, 2.17.1 = 320 — and the listing is incomplete,
per §Requirements.

Exit statuses, identical on all three builds:

| situation | exit |
| --- | --- |
| clean source | 0 |
| findings | 2 |
| the directory holds no C/C++ source | 1 |
| a path that does not exist | 1 |
| an unknown flag | 1 |

Channels measured ABSENT. This section exists because a reader coming from the `ruff` document
will expect these, and a negative result nobody wrote down gets re-measured or, worse, assumed:

| probe | result |
| --- | --- |
| a configuration file beside the sources — `cppcheck-suppressions`, `.cppcheck-suppressions`, `suppressions.txt`, `cppcheck.cfg`, `.cppcheck`, `cppcheck.ini` | verdict unchanged, on all three builds |
| `cppcheck-suppressions` at the repository root | verdict unchanged, on all three builds |
| the same content passed as `--suppressions-list=<file>` | **suppresses** — this is the positive control that says the probes above were live |
| `.gitignore` matching the sources at the repository root | not read; verdict unchanged |
| `compile_commands.json` beside the sources | not read; it needs an explicit `--project=` |
| a cache | none is kept unless `--cppcheck-build-dir=<dir>` is passed, which this argv does not |

## Operations Rules
- **Re-measuring** requires the supported versions installed side by side. Use the wheel recipe
  above rather than upgrading the host's build.
- **Adding a suppression** to `SUPPRESSED_RULE_CODES` requires a ground written in the same edit,
  and re-running the fixture on every supported build: a suppression that removes a real defect
  check is the failure this backend already avoided once (§Requirements).
- **Widening the range** requires re-running the fixture, the exit-status table and the
  channels-absent table on the new build, and recording the result here.
