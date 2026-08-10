#!/usr/bin/env python3
"""Free-form Fortran logical-line scanning — the one implementation the gates share.

Several deterministic, fail-closed gates have to read generated Fortran the way the compiler
does: a `!` comment is not code, a `&` continuation is one statement across several physical
lines, and a `;` separates statements on one line. Three hand-rolled scanners used to do that
independently (`validate_pipeline_semantics._iter_fortran_logical_lines` and
`._fortran_logical_lines`, `orchestration_runtime._fortran_logical_lines`), and between them
they mis-read six inputs (issue #23) — three that all three get wrong, one where the copies are
wrong in opposite directions, and two that only `_iter_fortran_logical_lines` gets wrong. Each
defect's harm direction is a false `Generate fail` / `Compile fail` on a source gfortran and
fortitude both accept:

* **`str.splitlines()`** breaks on eight separators Fortran does not treat as line ends
  (`\\f`, `\\v`, `\\x85`, `\\u2028`, …), so a form feed inside a comment ended the comment early
  and re-admitted its tail AS CODE — a commented-out `use harness_…` becoming live, a phantom
  §5.1 stanza. Splitting on `\\n` alone is the language's own rule. (All three.)
* **`str.strip()`'s blank set is not Fortran's**, and it is the same character class from the
  other end: gfortran's free-form blank set is exactly three characters (space, tab — a
  `-Wtabs` extension — and form feed) while `strip()` also folds
  `\\v`, `\\x85`, `\\xa0`, `\\u2028`, …. Those are ordinary CONTENT to the compiler, so stripping
  them re-admits literal content as code — a `\\v` before a resuming `&` made the scanner eat an `&`
  gfortran reads as part of the string, and the rest of the literal spilled out as declarations.
  (All three; found in review of the consolidation, in the consolidated scanner itself.)
* A **`!` inside a CONTINUED character literal** read as a comment, because quote state was
  tracked per physical line. The rest of the line was dropped and, if what survived ended in
  `&`, the buffer stayed open and swallowed the next statement; where it did not, the statement
  flushed and simply lost the rest of the literal. Which of the two happens is a property of the
  input, not of the copy. (All three, in both shapes.)
* A continuation line that does **not** start with `&` must join with a **SPACE**: the line
  break is then a token separator (F2008). Concatenating turned `do&` / `i = 1, n` into
  `doi = 1, n`. Only a `&`-led line splits a token, and that one joins tight — joining THAT
  with a space is the mirror error. Both validator copies made the first error and the runtime
  copy made the second, so each was wrong exactly where the other was right, which is why
  comparing them against each other never flagged it. Inside a character context the join is
  always tight: a line break cannot insert a blank into a literal.
* A **lone-`&` continuation line** (`&`, or `&! note`): testing the trailing `&` before
  consuming the leading one read the single ampersand as both markers, so the buffer stayed
  open and glued the next statement on. gfortran terminates the statement there, with only a
  `'&' not allowed by itself` WARNING — so such a source reaches a gate. It still does after
  issue #25: that diagnostic carries no `-W<class>` tag (bare `f951: Warning: ...`), so
  `-Werror=ampersand` does not promote it, unlike the missing-`&` literal resume. (Only
  `_iter_fortran_logical_lines`; the other two consumed the leading `&` first.)
* A **blank or comment line INSIDE a wrap** flushed a truncated logical line, so a legally
  wrapped `subroutine foo(a, &` / `! note` / `     & b)` header arrived at the gates as the
  fragments `subroutine foo(a, ` and `& b)`. (Only `_iter_fortran_logical_lines`; the other two
  skipped such lines, and the runtime copy then space-joined the resume, which is the third
  bullet.)

The scanner below is recovered from commit `166946a` (`_fortran_code_statements` +
`_strip_fortran_comment_tracking_quotes`, built and verified for the issue #22 OpenMP floor,
then deleted in `5f3ccf6` when that floor moved to anchored line-start patterns). The floor's
answer does not transfer to these callers: a presence check can anchor a pattern at `^\\s*` and
never join anything, while these consumers read the JOINED logical line and compare it against
a declared surface. So the logic returns here, restructured from statement level to
logical-line level, with `;`-splitting and string masking factored out into the caller's hands.

`split_top_level_commas`, `literal_crosses_line_break` and `mask_code_lookalikes` joined it
later, out of the same defect class and found the same way. Four copies of the comma splitter
existed — one of them shadowed in-module by a weaker twin, one hidden under the name
`_split_fortran_names` — disagreeing on whether a comma inside a character literal separates;
a fifth copy of the shape was a quote-blind paren extractor (`_iter_fortran_calls`) duplicating
its quote-aware twin in the same module; and above all of them a regex decided which text was a
subroutine body while blind to both comments and literals. Each was found by a different search
— the name, the shape, the other half of the shape, then the rule that selects the input — and
every one of them turned a `Generate.static` verdict wrong for source gfortran accepts.

Stdlib only and importing nothing from this package, so every site can depend on it. No
existing module is a home: the validator may not import `orchestration_runtime`
(module-boundary rule); `lang_backend_fortran` imports the validator at module level, so
hosting it there would put the validator in a cycle; and hosting it in the validator would
force `orchestration_runtime` to import that module at module level, which imports PyYAML
unconditionally — the runtime deliberately defers PyYAML so its recovery commands stay usable
without it. Same precedent as `tools/pure_leaf.py`.
"""

from __future__ import annotations

# gfortran's blank set for free-form source: space, tab, form feed. NOT Python's `str.strip()`
# set, which also folds `\v`, `\x85`, `\xa0`, `\u2028` and friends. That difference is the same
# one `str.splitlines()` introduced at the other end: those characters are ordinary content to
# the compiler, so stripping them here re-admits literal content as code — a `\v` before a
# resuming `&` made the scanner eat an `&` the compiler reads as part of the string, and the rest
# of the literal spilled out as declarations. Every blank decision in this module goes through
# these three helpers so the set is stated once.
_BLANKS = " \t\f"


def _lstrip_blanks(line: str) -> str:
    return line.lstrip(_BLANKS)


def _rstrip_blanks(line: str) -> str:
    return line.rstrip(_BLANKS)


def _is_all_blank(line: str) -> bool:
    return not line.strip(_BLANKS)


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
    ``split_fortran_statements`` and the validator's ``_mask_fortran_string_contents``, so each
    gate composes the view it needs.

    The defects this shape exists to close are documented at module level; the load-bearing
    lines below are marked."""
    logical: list[tuple[int, str]] = []
    buffer = ""
    start_lineno = 0
    quote: str | None = None  # set while the buffer ends INSIDE a character literal
    pending_quote_escape: str | None = None  # see the join comment below

    # `text.split("\n")`, never `str.splitlines()`: the latter breaks on eight separators
    # Fortran does not treat as line ends, ending a comment early and re-admitting its tail as
    # code. `\n` alone is the language's rule.
    for lineno, raw_line in enumerate(text.split("\n"), 1):
        # Free form permits comment lines BETWEEN a `&`-terminated line and its continuation;
        # they are ignored, not terminators. Flushing on one truncates the statement.
        #
        # This holds INSIDE an open character literal too — F2008 3.3.2.4 resumes a continued
        # character context on "the next line that is not a comment line", and gfortran agrees
        # (`'abc&` / blank or `! note` / `      &def'` compiles to a string of length 6).
        # Guarding this skip on `quote is None` made the statement flush early and spilled the
        # rest of the literal out as code, which is how a quoted `open(` became a file-I/O
        # violation. A comment line is a property of the PHYSICAL line — all blanks, or first
        # nonblank `!` — so the probe deliberately reads from a clean state: that is exactly the
        # predicate, not an approximation of it. (A continuation line inside a literal cannot be
        # mistaken for one: its first nonblank must be the resuming `&`.)
        if buffer:
            probe, _ = strip_fortran_comment_tracking_quotes(raw_line, None)
            if _is_all_blank(probe):
                continue
        # Quote state is threaded across physical lines, so a `!` inside a CONTINUED literal
        # stays content.
        resumes_literal = quote is not None
        code, quote = strip_fortran_comment_tracking_quotes(raw_line, quote)
        # Trailing blanks go, so a legal `do i &   ` still registers as continued. A `\r` is NOT
        # a blank here: every caller reads its Fortran through `read_text`, which universal-
        # newline-translates before the scanner sees it, so a CR does not reach this line from a
        # file. Text that arrives another way (a §5.1 block through PyYAML) could carry one, and
        # it would then be content — which is what gfortran does with it too.
        code = _rstrip_blanks(code)
        # ORDER MATTERS, in both directions.
        #
        # The continuation's LEADING `&` is consumed BEFORE asking whether this line continues,
        # so the single ampersand of a `&`-only line (or `&! note`) is not read as both markers.
        #
        # And a continuation line that does NOT begin with `&` joins with a SPACE, because the
        # line break is then a token separator. Only a `&`-led line splits a token, and that one
        # joins tight.
        #
        # Inside a character context the join is ALWAYS tight: a line break cannot insert a blank
        # into a literal. That covers the conforming `&`-led resume and also gfortran's extension
        # of accepting a resume line with no `&` at all. (Verified: `'abc&` / `      def'` compiles
        # with `len == 6`, i.e. `abcdef`, not `abc def`.) The leading blanks are column padding
        # either way, never content. The extension no longer reaches a gate — issue #25 promotes
        # `-Werror=ampersand` in the `Generate.gate` syntax check — but this is a general Fortran
        # reader, not a gate-shaped one: it must agree with the compiler on what a source MEANS,
        # including sources the gate will go on to reject, and callers outside that gate's reach.
        #
        # `pending_quote_escape` covers the one case where the compiler's notion of "inside a
        # character context" outlives this scanner's: a doubled-quote escape SPLIT BY THE WRAP.
        # In `'ab'&` / `'cd'` the literal looks closed at end of line, but gfortran's lookahead
        # reads the two quotes as one escaped quote and compiles `ab'cd` (`len == 5`, pinned) —
        # with no diagnostic at all, so such a source passes the syntax gate. A space there
        # would produce `'ab' 'cd'`, which is not Fortran at all. The lookahead is over the next
        # CONTRIBUTED character, not the next physical line: a wrap line that contributes nothing
        # (`&&`) must not clear it, which is why the memo is only reassigned when this line
        # contributed something.
        joins_tight = resumes_literal or pending_quote_escape is not None
        if buffer:
            code = _lstrip_blanks(code)
            if pending_quote_escape is not None and not code.startswith(pending_quote_escape):
                joins_tight = resumes_literal
            if code.startswith("&"):
                joins_tight = True
                code = code[1:]
        continued = code.endswith("&")
        if continued:
            code = code[:-1]
        if code:
            # The shape a wrap-split doubled-quote escape takes: a continued line whose last
            # contributed character is a quote. No `quote is None` guard — if the line ended
            # INSIDE a literal the join is already tight via `resumes_literal`, so the guard
            # could not change an answer, and this module does not keep conditions its tests
            # cannot pin.
            pending_quote_escape = code[-1] if continued and code[-1] in ("'", '"') else None
        if buffer:
            buffer += code if joins_tight else f" {code}"
        else:
            start_lineno = lineno
            buffer = code
        if continued:
            continue
        if not _is_all_blank(buffer):
            logical.append((start_lineno, buffer))
        buffer = ""
        # Reset the literal state with the logical line. Only reachable from source no compiler
        # accepts (an unterminated literal), but without it a leaked open quote makes the NEXT
        # line's text read as literal content.
        quote = None
    if not _is_all_blank(buffer):
        logical.append((start_lineno, buffer))
    return logical


def literal_crosses_line_break(text: str, newline_index: int) -> bool:
    """Does a character literal open at ``text[newline_index]``'s line survive the line break?

    Only through a continuation: free-form Fortran lets a literal cross a line break exactly when
    the line ends with ``&`` (F2008 3.3.2.4), and gfortran diagnoses `Unterminated character
    constant` otherwise. So the answer is "the last non-blank character before the newline is
    ``&``". Inside a literal that ``&`` is unambiguous — a `!` there is literal text, so no
    comment can hide it.

    Every scanner that reads RAW multi-line Fortran needs this decision, and getting it wrong is
    a defect in whichever direction it errs: resetting unconditionally cuts a continued literal
    in half, so the closing quote on the resume line reads as an OPENING one and every later
    paren stops counting; never resetting lets an apostrophe in a comment (``! it's``) open a
    literal nothing closes, swallowing the rest of the text. Both were observed silencing
    `Generate.static` — hence one shared implementation rather than a copy per scanner."""
    end = newline_index
    while True:
        line_start = text.rfind("\n", 0, end) + 1
        line = text[line_start:end]
        stripped = _lstrip_blanks(line)
        if stripped and not stripped.startswith("!"):
            return _rstrip_blanks(line).endswith("&")
        # An all-blank or comment-only physical line does not contribute to the statement, so it
        # does not end the continuation either — the resume line's leading `&` still pairs with
        # the `&` above the skipped lines (F2008 3.3.2.4, and what `fortran_logical_lines` does).
        # Without this skip a `! note` between `'x&` and `&y'` closed the literal, and the
        # closing quote on the resume line then read as an OPENING one, leaking the literal's
        # tail as code.
        if line_start == 0:
            return False
        end = line_start - 1


def mask_code_lookalikes(text: str) -> str:
    """Blank out every `!` comment and the CONTENTS of every character literal, preserving the
    length of ``text`` and the position of every surviving character.

    For a rule that matches Fortran's KEYWORD STRUCTURE over RAW multi-line source: neither a
    comment nor the inside of a literal is code, but a regex over raw text cannot tell. Offsets
    are preserved so a caller may match on the mask and slice the ORIGINAL, and so two scans of
    the same source stay comparable by position.

    Quote delimiters survive (a masked literal is still recognisably a literal, so a rule keying
    on `'...'` still matches); the `!` that opens a comment survives for the same reason. Do NOT
    feed a masked text to a rule that inspects literal CONTENT — the forbidden-filename scans
    deliberately catch a quoted `verdict.json`.

    The defect this exists for: `subroutine\\s+(\\w+)\\s*\\((.*?)\\)(.*?)end\\s+subroutine` over raw
    source stops at the first TEXTUAL `end subroutine`, so a comment mentioning one truncates the
    body and the `Generate.static` gates that read that body go silent — fail-open from one
    comment line. The mirror is a commented-out procedure minting a phantom match, which is
    fail-closed. Both were observed."""
    out: list[str] = []
    quote: str | None = None
    in_comment = False
    for index, ch in enumerate(text):
        if ch == "\n":
            in_comment = False
            if quote is not None and not literal_crosses_line_break(text, index):
                quote = None
            out.append(ch)
        elif quote is not None and ch == "!" and _is_all_blank(
                text[text.rfind("\n", 0, index) + 1 : index]):
            # A comment-only line INSIDE a continued literal is a comment, not literal content
            # (`literal_crosses_line_break` skips it for the same reason). Masking it as content
            # would leave its text live if it happened to look like code.
            in_comment = True
            out.append(ch)
        elif in_comment:
            out.append(" ")
        elif quote is not None:
            # `&` survives inside a literal: a trailing one is the continuation marker, and both
            # `literal_crosses_line_break` and `fortran_logical_lines` read it off the text they
            # are given. Blanking it made a masked continued literal look terminated, so its
            # closing quote read as an opening one — the same defect the mask exists to remove.
            out.append(ch if ch in (quote, "&") else " ")
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "!":
            in_comment = True
            out.append(ch)
        else:
            out.append(ch)
    return "".join(out)


def _split_top_level(line: str, separator: str, openers: str, closers: str) -> list[str]:
    """Split ``line`` on ``separator`` occurrences that are outside quotes and outside any
    ``openers``/``closers`` group. Shared body of the two public splitters below; the only
    differences between them are the separator and which bracket kinds nest.

    ``separator`` must be ONE character, and not one the brackets already claim. The scan is
    per-character, so a two-character separator such as Fortran's ``//`` concatenation operator
    would match nothing and return the input unsplit — a silently dead gate, which is the failure
    shape this consolidation exists to remove, so it is checked rather than left to a future
    caller to discover. (``_split_top_level_concat`` in the validator is exactly such a caller
    waiting to happen.) Raised, not asserted, so the guard survives ``python3 -O``, which would
    otherwise elide it and hand back exactly the unsplit input it exists to prevent.

    Character-literal state is tracked with a plain toggle, which is also correct for Fortran's
    doubled-quote escape: ``'it''s'`` leaves the literal at the second quote and re-enters at the
    third, with no character in between, so no separator can be seen outside the literal.

    A newline closes an open literal unless the line continues it (`literal_crosses_line_break`).
    Both public splitters take ONE logical line, where neither case arises — joining has already
    happened. It is defence for a caller handing over raw multi-line text, where an apostrophe in
    a comment (``! it's``) would otherwise open a literal nothing ever closes and swallow every
    later separator to end of text.

    An unbalanced closer clamps the depth at zero rather than driving it negative. Only
    unparseable source gets there, but a stuck-negative depth would silence every later
    separator on the line, and the harm direction of that is a fragment going unseen — for
    ``;``, a declaration after the separator reported absent; for ``,``, an entity of a
    declaration list dropped."""
    if len(separator) != 1 or separator in openers + closers:
        raise ValueError(
            f"separator must be one character the brackets do not claim, got {separator!r}")
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    in_single = False
    in_double = False
    for index, ch in enumerate(line):
        if ch == "\n" and not literal_crosses_line_break(line, index):
            in_single = False
            in_double = False
            current.append(ch)
        elif in_single:
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
        elif ch in openers:
            depth += 1
            current.append(ch)
        elif ch in closers:
            depth = max(0, depth - 1)
            current.append(ch)
        elif ch == separator and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts


def split_fortran_statements(line: str) -> list[str]:
    """Split one logical line on top-level ``;`` statement separators.

    Semicolons inside quotes or parentheses are ignored, so a line such as
    ``fmt = '(a,l1,a)'; write(u, fmt) x`` becomes two statements. Parts are returned as-is —
    neither stripped nor filtered for emptiness — because the callers differ on both (one keeps
    the ``^\\s*`` its patterns anchor on, another wants stripped statements)."""
    return _split_top_level(line, ";", "(", ")")


def split_top_level_commas(text: str) -> list[str]:
    """Split ``text`` on commas that separate top-level items — the one implementation the
    gates share.

    A comma inside quotes (``sep = ','``), inside parentheses (``b(2,2)``) or inside a bracket
    group (an array constructor ``[1,2]``, a coarray codimension ``x[*]``) is not a separator,
    so an entity list ``a(:), b(2,2), c`` splits into ``a(:)`` / ``b(2,2)`` / ``c``. Parts are
    returned as-is — neither stripped nor filtered for emptiness — because the callers differ on
    both.

    Four copies of this used to exist — two in the SAME module ~9,500 lines apart, where the
    later quote-unaware definition shadowed the earlier quote-aware one; one more in
    ``orchestration_runtime``; and ``validate_pipeline_semantics._split_fortran_names`` under a
    different name. Every surviving copy was quote-blind, and both harm directions were reachable
    from valid Fortran:

    * **Fail-open, by phantom item.** ``_split_fortran_names`` made a comma inside a character
      literal manufacture an extra identifier, and that phantom suppressed a real
      `Generate.static` dependency-dataflow violation (see that function).
    * **Fail-open, by truncation.** The runner JSON descriptor gate lost a violation by the
      other route, whenever a format literal carries an UNBALANCED paren — an apostrophe edit
      descriptor emitting ``)`` (``'(a,'')'',l1)'``), or a ``)`` inside a double-quoted piece.
      The quote-blind paren depth skews and the format token is cut short, so nothing is
      scanned. gfortran ``-std=f2008`` accepts both forms.
    * **Fail-closed.** ``_declaration_atoms`` split a BALANCED initializer literal
      (``:: sep = ',', tail = 'z'``) into a truncated atom plus a phantom one — identically on
      both sides of the §5.1 comparison, so it cancelled and reached no gate. An UNBALANCED
      paren (``:: msg = 'a(b', tail = 'z'``) instead suppressed the split on the combined form
      alone, so combined and one-per-line compared UNEQUAL and a legal declaration read as a
      signature mismatch. Probing only the balanced case makes the class look unreachable.

    Hence the shared form is the strictest of the copies — quote-aware from the shadowed one,
    bracket-aware and depth-clamped from the survivors."""
    return _split_top_level(text, ",", "([", ")]")
