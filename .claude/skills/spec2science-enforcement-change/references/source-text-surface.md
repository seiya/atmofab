# The source-text surface: spelling variation a Fortran-reading gate must survive

Moved out of `SKILL.md` verbatim (2026-08-25). `SKILL.md` §1 keeps the pointer and the one rule
that closes the whole family (invert the polarity; do not parse statements that start with a
keyword). This checklist is target-language knowledge, so it also carries its own boundary note —
read the second paragraph before quoting any figure from it.

**When the gate reads the source text rather than the meaning of an input (validators and
parsers), the surface is a different one.** The exec/env/argv surface (`SKILL.md` §1) is where a
caller's values reach a command — since issue #457 the build-runtime library checks only their
TYPES, so that inventory is of the code that composes them — and it barely applies to the Fortran-reading gates (the `fortran` backend's `source_reading`,
`tools/backends/language/fortran/`, which `validate_pipeline_semantics.py` reaches through the
registry). What you inventory there is **the spelling variation the
language permits**:

- **Keywords are not reserved words.** A variable may be named `module` / `parameter` /
  `contains` / `endmodule`. Every rule that treats "a statement starting with a keyword" as
  structure breaks on this
- **The space in a two-word keyword is sometimes optional** (F2008 Table 3.1). `selecttype` /
  `endsubroutine` / `doubleprecision` are legal as one word. Sweep every `\s+` you wrote (some
  forms such as `abstractinterface` are not legal, so **ask the compiler** to settle each one)
- **`::` may be omitted** (`integer ncomp`, `public ncomp`, `enumerator red`). Check that the
  two spellings of one statement are not treated differently
- **A statement label may precede any statement** (`10 contains`, `100 use m`, `20 subroutine
  f(x)`)
- Attribute-bearing statements have 18 forms without `::` (`common` / `dimension` /
  `equivalence` / `data` / `namelist` / bare `pointer` …). **Writing out each grammar is the
  losing line** — close them all at once by inverting the polarity: do not parse statements
  that start with a keyword; take every identifier that appears in them to the safe side

This checklist names concrete spellings of one target language, which is knowledge
`docs/BACKEND_BOUNDARY.md` keeps out of the neutral core. It was written while the gates it warns
about sat in `validate_pipeline_semantics.py`, with the promise that it would go when the
source-reading area of the `TODO.md` migration ledger closed. That area closed (issue #289) and the
whole migration with [issue #469](https://github.com/seiya/spec2science/issues/469), and the checklist
stays: it is a review checklist for whoever changes the `fortran` reader, not code the neutral
core runs, and its natural home if it is ever moved is `docs/backends/language/fortran/`. Two things about that are worth stating rather than implying. Nothing measures
this file: `.claude/skills/**` matches none of the scanner's globs, so the ratchet that bounds this
kind of growth elsewhere does not read it at all. And the debt is **new to the repository**, since
until 2026-08-19 these files lived in one operator's home directory. Most of it is not in this
checklist, either: the majority of the sampled tokens under `.claude/skills/` are in episodes and
identifier names, in both skills, and `spec2science-review-loop` carries some while having no checklist
at all. The development-documentation entry, now [a comment on PR #90](https://github.com/seiya/spec2science/pull/90#issuecomment-5559633234), holds the measurement and the command that
reproduces it — do not quote a figure from here, because every edit to these files moves it.

## What the reader drops and the build compiles (issue #589 PR-1b)

A gate that reads a comment-free view is judging a different text from the one the compiler reads
whenever the two disagree about what a comment is. Fortran's `!$` is that case: a comment to the
statement reader, a statement under `-fopenmp`, which is how `fortran_cpu` builds. A blank-slate
reviewer put `!$ call execute_command_line("sed ...")` in a coriolis model with no `ncomp` guard:
it rewrote the guard captures and went through `Generate.gate`, Build and Validate to `self_verdict:
pass`, both guard tests `xfail`. Every scan of the isolation gate (`open`, `use`, the file names,
and the two the previous round had just added) was one sentinel away.

**Rule: list what your reader discards (comments, masked strings, dead code, preprocessor
branches), then ask of each what the compiler does with it UNDER THE FLAGS THE HOST REALLY USES.**
Read the build's flags, not the language's defaults. `model_harness_isolation_violations` now blanks
the sentinel of a non-directive `!$` line before reading.

**A scan by NAME also refuses less than it looks.** `call execute_command_line` and `call system`
were refused by name, and `bind(c, name='system')` under a local name passed, with an `atexit`
handler rewriting a capture after the driver wrote it (round 1, measured `guard_pass` 0 to 1, every
gate green). Refuse the MECHANISM a name is reached through (`bind(c)`, as the C++ scan refuses
`extern "C"`). The plan had classified this route as "no working route found", without running one.
