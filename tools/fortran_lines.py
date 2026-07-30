#!/usr/bin/env python3
"""Free-form Fortran logical-line scanning — the one implementation the gates share.

Several deterministic, fail-closed gates have to read generated Fortran the way the compiler
does: a `!` comment is not code, a `&` continuation is one statement across several physical
lines, and a `;` separates statements on one line. Three hand-rolled scanners used to do that
independently (`validate_pipeline_semantics._iter_fortran_logical_lines` and
`._fortran_logical_lines`, `orchestration_runtime._fortran_logical_lines`), and all three
mis-read the SAME four inputs (issue #23). Each defect's harm direction is a false
`Generate fail` / `Compile fail` on a source gfortran and fortitude both accept:

* **`str.splitlines()`** breaks on eight separators Fortran does not treat as line ends
  (`\\f`, `\\v`, `\\x85`, `\\u2028`, …), so a form feed inside a comment ended the comment early
  and re-admitted its tail AS CODE — a commented-out `use harness_…` becoming live, a phantom
  §5.1 stanza. Splitting on `\\n` alone is the language's own rule.
* A **`!` inside a CONTINUED character literal** read as a comment, because quote state was
  tracked per physical line. The rest of the line was dropped and, if what survived ended in
  `&`, the buffer stayed open and swallowed the next statement.
* A continuation line that does **not** start with `&` must join with a **SPACE**: the line
  break is then a token separator (F2008). Concatenating turned `do&` / `i = 1, n` into
  `doi = 1, n`. Only a `&`-led line splits a token, and that one joins tight — joining THAT
  with a space is the mirror error.
* A **lone-`&` continuation line** (`&`, or `&! note`): testing the trailing `&` before
  consuming the leading one read the single ampersand as both markers, so the buffer stayed
  open and glued the next statement on. gfortran terminates the statement there.

The scanner below is recovered from commit `166946a` (`_fortran_code_statements` +
`_strip_fortran_comment_tracking_quotes`, built and verified for the issue #22 OpenMP floor,
then deleted in `5f3ccf6` when that floor moved to anchored line-start patterns). The floor's
answer does not transfer to these callers: a presence check can anchor a pattern at `^\\s*` and
never join anything, while these consumers read the JOINED logical line and compare it against
a declared surface. So the logic returns here, restructured from statement level to
logical-line level, with `;`-splitting and string masking factored out into the caller's hands.

Stdlib only and importing nothing from this package, so every site can depend on it. No
existing module is a home: the validator may not import `orchestration_runtime`
(module-boundary rule); `lang_backend_fortran` imports the validator at module level, so
hosting it there would put the validator in a cycle; and hosting it in the validator would
force `orchestration_runtime` to import that module at module level, which imports PyYAML
unconditionally — the runtime deliberately defers PyYAML so its recovery commands stay usable
without it. Same precedent as `tools/pure_leaf.py`.
"""

from __future__ import annotations


def strip_fortran_comment_tracking_quotes(
    line: str, quote: str | None
) -> tuple[str, str | None]:
    """Drop a trailing ``!`` comment, carrying character-literal state IN and OUT.

    ``quote`` is the open quote character when this physical line begins inside a literal (a
    literal continued from the previous line), or ``None``. Returns the comment-free text plus
    the state at end of line. A per-line stripper cannot know that a ``!`` sits inside a
    continued literal, which is the whole reason this one exists.

    The Fortran doubled-quote escape (``''`` / ``""``) needs no special case: reading it as
    close-then-reopen leaves the inside/outside state identical for any run of quotes, so an
    explicit escape branch was provably unobservable — it survived every mutation because it
    could not change an answer. Dropping it keeps this function to what its tests can pin."""
    out: list[str] = []
    for ch in line:
        if quote is not None:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
        elif ch == "!":
            break
        out.append(ch)
    return "".join(out), quote


def fortran_logical_lines(text: str) -> list[tuple[int, str]]:
    """``text`` reduced to ``(start_lineno, joined_text)`` — one entry per free-form Fortran
    LOGICAL LINE, comments stripped and ``&`` continuations joined.

    ``start_lineno`` is the 1-based physical line on which the logical line began. A line that
    is blank or comment-only produces NO entry. Leading whitespace of the first physical line is
    preserved (callers anchor patterns at ``^\\s*``). Trailing whitespace is stripped from each
    PHYSICAL line before its ``&`` is consumed, so a joined line can still end in the blank that
    preceded a consumed marker (``x = 1 &`` flushes as ``x = 1 ``) — what the rstrip guarantees
    is that a trailing ``&`` is still recognized behind blanks, not a trimmed result. This
    deliberately does NOT split on ``;`` and does NOT mask string contents — see
    ``split_fortran_statements`` and the callers' own maskers, so each gate composes the view
    it needs.

    The four defects this shape exists to close are documented at module level; the load-bearing
    lines below are marked."""
    logical: list[tuple[int, str]] = []
    buffer = ""
    start_lineno = 0
    quote: str | None = None  # set while the buffer ends INSIDE a character literal

    # `text.split("\n")`, never `str.splitlines()`: the latter breaks on eight separators
    # Fortran does not treat as line ends, ending a comment early and re-admitting its tail as
    # code. `\n` alone is the language's rule.
    for lineno, raw_line in enumerate(text.split("\n"), 1):
        # Free form permits blank and comment-only lines BETWEEN a `&`-terminated line and its
        # continuation; they are ignored, not terminators. Flushing on one truncates the
        # statement. Not applicable mid-literal: inside a character context the continuation
        # must be immediate, so the `quote is None` guard is part of the rule, not an
        # optimization — the probe strips from a CLEAN state and would misread literal content.
        if buffer and quote is None:
            probe, _ = strip_fortran_comment_tracking_quotes(raw_line, None)
            if not probe.strip():
                continue
        # Quote state is threaded across physical lines, so a `!` inside a CONTINUED literal
        # stays content.
        code, quote = strip_fortran_comment_tracking_quotes(raw_line, quote)
        # `.rstrip()` matters beyond tidiness: a legal `do i &   ` (trailing blanks after the
        # continuation marker) would otherwise not register as continued. (A CRLF source cannot
        # exercise this — `read_text` universal-newline-translates before the scanner sees it.)
        code = code.rstrip()
        # ORDER MATTERS, in both directions.
        #
        # The continuation's LEADING `&` is consumed BEFORE asking whether this line continues,
        # so the single ampersand of a `&`-only line (or `&! note`) is not read as both markers.
        #
        # And a continuation line that does NOT begin with `&` joins with a SPACE, because the
        # line break is then a token separator. Only a `&`-led line splits a token, and that one
        # joins tight.
        joins_tight = False
        if buffer:
            code = code.lstrip()
            if code.startswith("&"):
                joins_tight = True
                code = code[1:]
        continued = code.endswith("&")
        if continued:
            code = code[:-1]
        if buffer:
            buffer += code if joins_tight else f" {code}"
        else:
            start_lineno = lineno
            buffer = code
        if continued:
            continue
        if buffer.strip():
            logical.append((start_lineno, buffer))
        buffer = ""
        # Reset the literal state with the logical line. Only reachable from source no compiler
        # accepts (an unterminated literal), but without it a leaked open quote makes the NEXT
        # line's text read as literal content.
        quote = None
    if buffer.strip():
        logical.append((start_lineno, buffer))
    return logical


def split_fortran_statements(line: str) -> list[str]:
    """Split one logical line on top-level ``;`` statement separators.

    Semicolons inside quotes or parentheses are ignored, so a line such as
    ``fmt = '(a,l1,a)'; write(u, fmt) x`` becomes two statements. Parts are returned as-is —
    neither stripped nor filtered for emptiness — because the callers differ on both (one keeps
    the ``^\\s*`` its patterns anchor on, another wants stripped statements)."""
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    in_single = False
    in_double = False
    for ch in line:
        if in_single:
            current.append(ch)
            if ch == "'":
                in_single = False
        elif in_double:
            current.append(ch)
            if ch == '"':
                in_double = False
        elif ch == "'":
            in_single = True
            current.append(ch)
        elif ch == '"':
            in_double = True
            current.append(ch)
        elif ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth -= 1
            current.append(ch)
        elif ch == ";" and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts
