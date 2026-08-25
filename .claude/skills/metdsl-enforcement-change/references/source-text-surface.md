# The source-text surface: spelling variation a Fortran-reading gate must survive

Moved out of `SKILL.md` verbatim (2026-08-25). `SKILL.md` §1 keeps the pointer and the one rule
that closes the whole family (invert the polarity; do not parse statements that start with a
keyword). This checklist is target-language knowledge, so it also carries its own boundary note —
read the second paragraph before quoting any figure from it.

**When the gate reads the source text rather than the meaning of an input (validators and
parsers), the surface is a different one.** The exec/env/argv surface (`SKILL.md` §1) is the MCP
capability gate's and barely applies to the Fortran-reading gates in
`validate_pipeline_semantics.py`. What you inventory there is **the spelling variation the
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
`docs/BACKEND_BOUNDARY.md` keeps out of the neutral core. It is here because the gates it warns
about are in `validate_pipeline_semantics.py`, and it goes when the source-reading area on the
TODO ledger goes. Two things about that are worth stating rather than implying. Nothing measures
this file: `.claude/skills/**` matches none of the scanner's globs, so the ratchet that bounds this
kind of growth elsewhere does not read it at all. And the debt is **new to the repository**, since
until 2026-08-19 these files lived in one operator's home directory. Most of it is not in this
checklist, either: the majority of the sampled tokens under `.claude/skills/` are in episodes and
identifier names, in both skills, and `metdsl-review-loop` carries some while having no checklist
at all. TODO.md's development-documentation entry holds the measurement and the command that
reproduces it — do not quote a figure from here, because every edit to these files moves it.

