# `ruff` lint rule set (canonical source)

## Purpose
The `Generate.gate` `static lint` check for a `python` node applies a rule set that is DECLARED
by this repository, not inherited from the linter build installed on the host. This document is
the canonical statement of that set for a reader; the machine-readable definition is
`tools/backends/linter/ruff/lint.py` (`RULE_CODES`), and this file is checked against it by
`tools/tests/test_linter_ruff.py`.

## Scope
- The rule set, the invocation that imposes it, and the linter versions both were measured on.
- **Not a leaf-facing checklist, and that is a decision rather than an omission.** The
  `fortitude` backend has one (`docs/workflow/phases/phase_02_generate.md` §2-1) because a leaf
  that trips a rule burns a regenerate cycle. No leaf can trip these rules today: no `spec` node
  selects `python`, because `_validate_toolchain_backend_supported` refuses any non-`fortran`
  `language` on every non-`infrastructure` node, and an `infrastructure` node still requires an
  extracted backend for its language. The checklist is owed the day that changes, and
  `tools/tests/test_linter_ruff.py` fails when it does — the obligation is tied to the
  reachability gate, not left to memory.
- Not the gate's behaviour on a finding: `docs/workflow/WORKFLOW_CORE.md` is canonical for the
  routing.

## Requirements
- A `Generate.gate` lint verdict is a function of the source and the declared set, not of the
  host's build.
- A rule the vendor enables by default in a future release does not enter a certification gate by
  being released. It enters by being added to `RULE_CODES`, which is a reviewable change. This is
  not hypothetical for this tool: 0.16.0 added **354** rules to its own default set.
- A host whose linter is outside the supported range is refused at launch, before the first leaf
  (`tools/host_prerequisites.py`, reason `unsupported_required_host_tool_versions`;
  `docs/RUNBOOK.md` §0-1).

## Design Policy
- The set is imposed with `--select`, which REPLACES the build's default set rather than adjusting
  it. Suppressing individual rules with `--ignore` was rejected for the reason the `fortitude`
  backend records: it answers one release and leaves the next default addition to enter
  unreviewed.
- The invocation closes **five** channels, one per flag. Each is a bullet below, the flags are
  checked against `CHECK_FLAGS` by test, and each was measured with the flag omitted AND with it
  present on every supported build. The count has been wrong once: the first version of this
  section said four and omitted `--exclude=`, on the belief that `--isolated` had settled the
  configuration question.
  - `--isolated` — closes a configuration file DISCOVERED by walking upward. Measured on 0.14.0,
    0.15.20, 0.16.0 and 0.16.5: a `ruff.toml`, `.ruff.toml` or `pyproject.toml` carrying
    `exclude = ["*.py"]` takes a five-finding tree to `All checks passed`, exit 0, from beside the
    sources and from the repository root two directories above them. Two config keys are NOT part
    of this channel and saying so is part of the enumeration: a CLI `--select` overrides a
    discovered `select` and a discovered `ignore`, so neither changes a verdict. What does are
    `exclude` (silent, exit 0) and `per-file-ignores` (five findings to one). **Since `--exclude=`
    joined the argv, a CLI `--exclude` also overrides a discovered `exclude`** — so `exclude` is
    no longer the key that isolates this flag, and the witness for it uses `per-file-ignores`
    instead. A control written with `exclude` would show the channel closed with `--isolated`
    REMOVED, and would witness nothing.
  - `--ignore-noqa` — closes an in-source `# noqa` comment, the channel a leaf can actually
    write, since a leaf authors the source. Measured: `# noqa: F401` on one import takes five
    findings to four on every supported build.
  - `--no-respect-gitignore` — closes an ignore file. Measured: a `.gitignore` matching the
    sources takes a five-finding tree to `All checks passed`, exit 0, with no diagnostic at all,
    so it is quieter than either channel above. `--isolated` does NOT close it. The file is only
    honoured inside a git repository, and the gate's `project_dir` is inside the checkout.
  - `--exclude=` — empties the tool's own BUILT-IN exclude list, which `--isolated` restores
    rather than removes. Measured on all four builds: the five-finding fixture at `dist/probe.py`
    reports `warning: No Python files found under the given path(s)`, `All checks passed!`,
    exit 0, while the same file one directory up reports its five findings. The list is 25 names
    and is byte-identical across the supported range (`.bzr`, `.direnv`, `.eggs`, `.git`,
    `.git-rewrite`, `.hg`, `.ipynb_checkpoints`, `.mypy_cache`, `.nox`, `.pants.d`, `.pyenv`,
    `.pytest_cache`, `.pytype`, `.ruff_cache`, `.svn`, `.tox`, `.venv`, `.vscode`,
    `__pypackages__`, `_build`, `buck-out`, `dist`, `node_modules`, `site-packages`, `venv`), so
    what the flag closes is the exclusion itself rather than its drift. Emptying it makes the file
    set a function of the walk root alone, and costs nothing here: the gate's `project_dir` is
    `source/<source_id>/src/`, which holds generated sources and none of the trees that list
    exists to protect.
  - `--no-cache` — closes a stale cache entry answering instead of the checker. Measured on all
    four builds: the cache key carries neither the file size nor a content hash, so under
    `--isolated --select` alone a file cached clean at 6 bytes and then replaced by the 170-byte
    five-finding fixture with its mtime restored still reports `All checks passed`, exit 0. The flag
    also stops ruff writing `.ruff_cache/` into `project_dir`.

    **One qualification, measured and stated rather than left out.** On every supported build
    `--ignore-noqa` also defeats a cache READ, so under the full declared argv the stale entry is
    already not served — `.ruff_cache/` is still written, but the verdict is correct. `--no-cache`
    therefore documents a closure it is not today the sole cause of. It stays because a closure
    resting on another flag's undocumented side effect is one vendor change from open (`ruff check
    --help` documents `--no-cache` as "Disable cache reads" and says nothing about `--ignore-noqa`
    touching the cache), and because the directory it keeps out of the leaf's source tree is worth
    keeping out on its own. The test for this channel drops both flags for its negative control,
    and says so.
- **What no flag closes.** A count of closed channels is not a claim that nothing else decides the
  verdict, and this enumeration has already been wrong once.
  - **A walk READ ERROR degrades to a warning and exit 0.** Measured on all four builds:
    `chmod 000` on a subdirectory holding the five-finding fixture gives
    `warning: Encountered error: Permission denied (os error 13)`, `All checks passed!`, exit 0 —
    quieter than any channel above, because the tool says it could not read something and then
    reports success. No flag changes it; only a caller refusing a run that reports zero files
    could. `fortitude` behaves identically and `cppcheck` does not (exit 1, classified as a
    refusal). It is recorded rather than closed because the gain is measured to be nothing: a leaf
    that hides a source from the linter has hidden it from the compiler too, and the build control
    file pins its sources by name. `TODO.md` carries it.
  - **The extensions the walk reads**, and what `__init__.py` semantics imply for a package.
- `--select ALL` is not used. It is not a spelling of any set anyone reviewed.

## Declared set
The 59 codes below. The DEFINITION is `RULE_CODES` in `tools/backends/linter/ruff/lint.py`; this
table is checked against it by `tools/tests/test_linter_ruff.py`, in that direction only — the
code is the authority and this document is what is compared to it.

DERIVED, NOT INVENTED: it is ruff's OWN default set as of 0.14.0 and 0.15.20, byte-identical on
both, which is also what `--select E4,E7,E9,F` resolves to on every supported build.

| code | rule |
| --- | --- |
| `E401` | multiple-imports-on-one-line |
| `E402` | module-import-not-at-top-of-file |
| `E701` | multiple-statements-on-one-line-colon |
| `E702` | multiple-statements-on-one-line-semicolon |
| `E703` | useless-semicolon |
| `E711` | none-comparison |
| `E712` | true-false-comparison |
| `E713` | not-in-test |
| `E714` | not-is-test |
| `E721` | type-comparison |
| `E722` | bare-except |
| `E731` | lambda-assignment |
| `E741` | ambiguous-variable-name |
| `E742` | ambiguous-class-name |
| `E743` | ambiguous-function-name |
| `E902` | io-error |
| `F401` | unused-import |
| `F402` | import-shadowed-by-loop-var |
| `F403` | undefined-local-with-import-star |
| `F404` | late-future-import |
| `F405` | undefined-local-with-import-star-usage |
| `F406` | undefined-local-with-nested-import-star-usage |
| `F407` | future-feature-not-defined |
| `F501` | percent-format-invalid-format |
| `F502` | percent-format-expected-mapping |
| `F503` | percent-format-expected-sequence |
| `F504` | percent-format-extra-named-arguments |
| `F505` | percent-format-missing-argument |
| `F506` | percent-format-mixed-positional-and-named |
| `F507` | percent-format-positional-count-mismatch |
| `F508` | percent-format-star-requires-sequence |
| `F509` | percent-format-unsupported-format-character |
| `F521` | string-dot-format-invalid-format |
| `F522` | string-dot-format-extra-named-arguments |
| `F523` | string-dot-format-extra-positional-arguments |
| `F524` | string-dot-format-missing-arguments |
| `F525` | string-dot-format-mixing-automatic |
| `F541` | f-string-missing-placeholders |
| `F601` | multi-value-repeated-key-literal |
| `F602` | multi-value-repeated-key-variable |
| `F621` | expressions-in-star-assignment |
| `F622` | multiple-starred-expressions |
| `F631` | assert-tuple |
| `F632` | is-literal |
| `F633` | invalid-print-syntax |
| `F634` | if-tuple |
| `F701` | break-outside-loop |
| `F702` | continue-outside-loop |
| `F704` | yield-outside-function |
| `F706` | return-outside-function |
| `F707` | default-except-not-last |
| `F722` | forward-annotation-syntax-error |
| `F811` | redefined-while-unused |
| `F821` | undefined-name |
| `F822` | undefined-export |
| `F823` | undefined-local |
| `F841` | unused-variable |
| `F842` | unused-annotation |
| `F901` | raise-not-implemented |

Codes deliberately excluded, with the ground:

| code | ground |
| --- | --- |
| `E999` | Impossible to select — the tool answers `Rule 'E999' was removed and cannot be selected.` and exits 2 with nothing checked. A syntax error is reported anyway without being selected, and `E902` (io-error) covers the file-level half. |
| `SIM117` | The rule that made this drift visible (issue #120). Absent from the default set on 0.14.0 and 0.15.20, present from 0.16.0. It is a style preference about nested `with` statements, not a defect class, and admitting it would mean admitting the other 353 rules 0.16.0 turned on with it — none of which anyone has reviewed for a generated source. |
| `I001` | Import sorting. Never in the set this repository declares, and the single largest contributor when this repository's own tree is checked under 0.16.x's default set. No count is written here: such a count is right only at the revision it was taken at, and `TODO.md` carries that rule together with the command to re-take it. A gate that fails a generated source on import ORDER burns a regenerate cycle on a property no certification depends on. |

## Supported versions
`>=0.14,<0.17`, declared as `MIN_VERSION` / `BELOW_VERSION` in `lint.py` and quoted by
`docs/RUNBOOK.md` §0-1.

Both ends state what was MEASURED, not what was found to break, and the difference from the
`fortitude` floor is worth stating: there the floor is forced (0.7.5 has no `--isolated` at all),
here it is not. Spot-checked below the floor, 0.9.0 / 0.12.0 / 0.13.3 all accept the declared
invocation and resolve it to the same 59 codes — but the four channels were not re-measured on
them, so they are outside the range. An unmeasured build is refused at launch rather than allowed
to decide a certification.

## Measurement (2026-09-01, four builds installed side by side)

Reproduce with `python3 -m pip install --target <dir> ruff==<version>`; the executable is
`<dir>/bin/ruff` and the host's own install is not modified.

Resolved rule sets, read from `ruff check --isolated --show-settings` (`linter.rules.enabled`):

| version | default set | declared set resolves to |
| --- | --- | --- |
| 0.14.0 | 59 codes | the declared 59, exactly |
| 0.15.20 | 59 codes | the declared 59, exactly |
| 0.16.0 | 413 codes | the declared 59, exactly |
| 0.16.5 | 413 codes | the declared 59, exactly |

0.16.0 added 354 rules to the default in one minor release: 47 `PYI`, 42 `UP`, 36 `RUF`, 33
`PLE`, 29 `B`, 21 `SIM`, 20 `PLW`, 17 `FURB`, 17 `C`, 13 `PLR`, and the rest.

Verdicts on one fixture (two unused imports, a nested `with`, `l = 1`, an undefined name). The
fixture is `_DEFECTIVE_SOURCE` in `tools/tests/test_linter_ruff.py`, so both columns are
reproducible anywhere the builds are installed:

| version | `ruff check .` (inherited default) | declared invocation |
| --- | --- | --- |
| 0.14.0 | `F401` 2, `E741` 1, `F821` 1, `F841` 1 | `F401` 2, `E741` 1, `F821` 1, `F841` 1 |
| 0.15.20 | the same four | the same four |
| 0.16.0 | `F401` 2, **`SIM117` 1**, `F821` 1, `F841` 1 | the same four |
| 0.16.5 | `F401` 2, **`SIM117` 1**, `F821` 1, `F841` 1 | the same four |

Two readings, both load-bearing:

- **The inherited default is not stable.** On an unchanged source it LOSES `E741` and GAINS
  `SIM117` at 0.16.0. Neither verdict is more correct than the other, which is the problem — the
  same failure mode as issue #110, in a second linter.
- **The declared invocation is identical on all four builds**, which is the requirement.

Argument validation, measured on all four:

| input | result |
| --- | --- |
| an unknown code (`ZZZ999`) | exit 2, nothing checked: `Unknown rule selector 'ZZZ999' in 'select' from the CLI` |
| a removed code (`E999`) | exit 2, nothing checked: `Rule 'E999' was removed and cannot be selected.` |
| a remapped code (`PGH001`) | a warning (`has been remapped to 'S307'`) and the run proceeds |

The first two are why `unusable_invocation_reason` classifies an exit outside `{0, 1}` as a
refusal of the invocation rather than a verdict about the source, and why the launch self-check
runs the declared invocation over an empty directory. The third is why a declared set is checked
by RESOLVING it rather than by trusting the spelling.

## Operations Rules
- **Re-measuring** requires the supported versions installed side by side, not the host's one
  build. `python3 -m pip install --target <dir> ruff==<version>` gives an isolated copy whose
  executable is `<dir>/bin/ruff`; the host's own install is not modified.
- **Adding a code** to the set: confirm it is selectable on every supported version (an unknown
  code is a hard argument error, exit 2, and nothing is checked), add it to `RULE_CODES`, and
  re-run the resolution check on each version.
- **A code the vendor removes or renames** surfaces as either an argument error or a warning with
  a silent redirect. Both are caught by comparing the RESOLVED set against `RULE_CODES` rather
  than by reading the spelling.
- **This document's `ruff` is the PRESET, not this repository's own Python lint.** `TODO.md`
  records `ruff check tools/ mcp_servers/` figures for the repository's own tree; those are a
  development-verification measurement and have nothing to do with the set declared here.
