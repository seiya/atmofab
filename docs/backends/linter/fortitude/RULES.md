# `fortitude` lint rule set (canonical source)

## Purpose
The `Generate.gate` `static lint` check applies a rule set that is DECLARED by this repository,
not inherited from the linter build installed on the host. This document is the canonical
statement of that set for a reader; the machine-readable definition is
`tools/backends/linter/fortitude/lint.py` (`RULE_CODES`), and this file is checked against it by
`tools/tests/test_linter_fortitude.py`.

## Scope
- The rule set, the invocation that imposes it, and the linter versions both were measured on.
- Not the gate's behaviour on a finding: `docs/workflow/phases/phase_02_generate.md` §2-1 is
  canonical for what a leaf does about one, and `docs/workflow/WORKFLOW_CORE.md` for the routing.

## Requirements
- A `Generate.gate` lint verdict is a function of the source and the declared set. Two hosts
  inside the supported version range reach the same verdict on the same source.
- A rule the vendor enables by default in a future release does not enter a certification gate by
  being released. It enters by being added to `RULE_CODES`, which is a reviewable change.
- A host whose linter is outside the supported range is refused at launch, before the first leaf
  (`tools/host_prerequisites.py`, reason `unsupported_required_host_tool_versions`;
  `docs/RUNBOOK.md` §0-1).

## Design Policy
- The set is imposed with `--select`, which REPLACES the build's default set rather than
  adjusting it. Suppressing individual rules with `--ignore` was rejected: it answers one release
  and leaves the next default addition to enter unreviewed.
- The invocation carries `--isolated`, so a configuration file discovered next to the sources
  cannot change the verdict. Measured on 0.8.0 and 0.9.2: a neighbouring `fortitude.toml`
  carrying `[check] ignore = [...]` turns a failing tree green without it.
- `--select ALL` is not used. It is not a superset of the default set on any measured version.

## Declared set
The 40 codes below. The DEFINITION is `RULE_CODES` in
`tools/backends/linter/fortitude/lint.py`; this table is checked against it by
`tools/tests/test_linter_fortitude.py`, in that direction only — the code is the authority and
this document is what is compared to it. Both sides are edited in the same change, which is the
point: widening or narrowing what a certification means is a reviewable edit, not a silent one.

| code | rule |
| --- | --- |
| `C001` | implicit-typing |
| `C002` | interface-implicit-typing |
| `C003` | implicit-external-procedures |
| `C011` | missing-default-case |
| `C051` | trailing-backslash |
| `C061` | missing-intent |
| `C071` | assumed-size |
| `C072` | assumed-size-character-intent |
| `C081` | initialisation-in-declaration |
| `C091` | external-procedure |
| `C092` | procedure-not-in-module |
| `C101` | missing-default-pointer-initalisation |
| `C121` | use-all |
| `C122` | missing-intrinsic |
| `C131` | missing-accessibility-statement |
| `C141` | missing-exit-or-cycle-label |
| `E000` | io-error |
| `E001` | syntax-error |
| `FORT001` | invalid-rule-code-or-name |
| `FORT002` | unused-allow-comment |
| `FORT003` | redirected-allow-comment |
| `FORT004` | duplicated-allow-comment |
| `FORT005` | disabled-allow-comment |
| `MOD011` | old-style-array-literal |
| `MOD021` | deprecated-relational-operator |
| `OB011` | common-block |
| `OB021` | entry-statement |
| `OB031` | specific-name |
| `OB041` | computed-go-to |
| `OB051` | pause-statement |
| `OB061` | deprecated-character-syntax |
| `PORT011` | literal-kind |
| `PORT012` | literal-kind-suffix |
| `PORT021` | star-kind |
| `S001` | line-too-long |
| `S061` | unnamed-end-statement |
| `S071` | missing-double-colon |
| `S081` | superfluous-semicolon |
| `S091` | non-standard-file-extension |
| `S101` | trailing-whitespace |

Codes deliberately excluded, with the ground:

| code | ground |
| --- | --- |
| `OB001` | default-enabled on every supported version and impossible to select — the tool answers `Rule 'OB001' was removed and cannot be selected`. A removed rule finds nothing, so its absence changes no verdict. |
| `S241` | The rule of the incident (issue #110). Preview-only on 0.8.0, default-enabled from 0.9.0. Every finding it produces on this tree is in the host-rendered runner, which no leaf can edit. Making the renderer satisfy it is separate work and is not a ground for enabling it here. |

## Supported versions
`>=0.8,<0.10`, declared as `MIN_VERSION` / `BELOW_VERSION` in `lint.py` and quoted by
`docs/RUNBOOK.md` §0-1.

- 0.7.5 is below the floor because it has no `--isolated` flag (`error: unexpected argument
  '--isolated' found`), so the declared invocation cannot run on it. It is below the floor even
  though it passes the incident source under its own default set.
- The ceiling states what was measured. A build at or above it is refused rather than trusted;
  widening the range requires re-running the measurement below and recording the result here.

## Measurement (2026-08-27, four builds installed side by side)

Resolved rule sets, read from `fortitude check --isolated --show-settings <file>`:

| version | default set | declared set resolves to |
| --- | --- | --- |
| 0.7.5 | not readable (`--isolated` absent) | invocation unavailable |
| 0.8.0 | 41 codes | the declared 40, exactly |
| 0.9.0 | 59 codes (0.8.0's 41 plus its 18 preview rules) | the declared 40, exactly |
| 0.9.2 | 59 codes | the declared 40, exactly |

Verdicts, over the source of the incident (three files, one of them the host-rendered runner) and
over a control tree carrying one defect per rule family:

| version | `fortitude check .` (inherited default) | declared invocation |
| --- | --- | --- |
| 0.8.0 | incident source: pass; control: 6 findings | incident source: pass; control: the same 6 |
| 0.9.0 | incident source: **58 × S241**; control: 6 findings | incident source: pass; control: the same 6 |
| 0.9.2 | incident source: **58 × S241**; control: 6 findings | incident source: pass; control: the same 6 |

The control's 6 findings are `C003`, `C122`, `C131`, `FORT001`, `PORT011`, `S001` — one per
family the leaf-facing documents instruct a leaf about, plus the unknown-code allow comment. They
are identical across all three versions and both invocations, which is what distinguishes a rule
set that is genuinely applied from one that has been switched off.

The allow-comment machinery survives the explicit selection: `! allow(C003)` on its own line
above a plain `implicit none` suppresses `C003`, and `! allow(ZZZ999)` is reported as `FORT001`
on every supported version.

## Operations Rules
- **Re-measuring** requires the supported versions installed side by side, not the host's one
  build. `python3 -m pip install --target <dir> fortitude-lint==<version>` gives an isolated
  copy whose executable is `<dir>/bin/fortitude`; the host's own install is not modified.
- **Adding a code** to the set: confirm it is selectable on every supported version (an unknown
  code is a hard argument error, exit 2, and nothing is checked), add it to `RULE_CODES`, and
  re-run the resolution check on each version.
- **A code the vendor removes or renames** surfaces as either an argument error or a silent
  redirect (measured: `S051` resolves to `MOD021`). Both are caught by comparing the RESOLVED
  set against `RULE_CODES` rather than by reading the spelling.
- **The leaf-facing documents** state individual rules of this set to the agent that must satisfy
  them (`docs/workflow/phases/phase_02_generate.md` §2-1,
  `skills/workflow-generate-generate/SKILL.md`, `tools/prompt_templates/pure_generate_generate.txt`,
  `docs/workflow/CHECKS_MODULE_CONTRACT.md` §5). Each names a subset and cites this set; a code
  named there that is not in `RULE_CODES` is a test failure, not a documentation nuance.
