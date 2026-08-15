#!/usr/bin/env python3
"""Unit tests for tools/backends/language/fortran/lines — the shared free-form Fortran logical-line scanner.

These pin the scanner directly. Most of them are the tests written for the issue #22 OpenMP
floor (commit `166946a`) and deleted with it in `5f3ccf6`, re-targeted from floor verdicts to
scanner output: the inputs are the same compiling sources, but the assertion is now on the
logical lines themselves, which is what the issue #23 consumers read. Consumer-level
reproducers live with their gates in `test_orchestration_runtime` /
`test_validate_pipeline_semantics`.
"""

from __future__ import annotations

import time
import unittest

from tools.backends.language.fortran.lines import (
    _split_top_level,
    fortran_logical_line_texts,
    fortran_logical_lines,
    normalize_fortran_line,
    mask_code_lookalikes,
    split_fortran_statements,
    split_top_level_commas,
    strip_fortran_comment_tracking_quotes,
)


class StripFortranCommentTrackingQuotesTests(unittest.TestCase):
    """The observable contract of the quote-carrying comment stripper.

    Doubled-quote escapes are deliberately NOT a separate case: read as close-then-reopen they
    leave the same state for any run of quotes, so an explicit escape branch was provably
    unobservable (it survived every mutation) and was removed rather than left unpinnable. These
    assertions still cover the escaped forms — they must simply behave, not be special."""

    def test_doubled_quote_escapes_behave(self) -> None:
        self.assertEqual(
            strip_fortran_comment_tracking_quotes("msg = 'a'''", None), ("msg = 'a'''", None))
        self.assertEqual(
            strip_fortran_comment_tracking_quotes('msg = "x"""', None), ('msg = "x"""', None))

    def test_bang_inside_a_literal_is_not_a_comment(self) -> None:
        self.assertEqual(
            strip_fortran_comment_tracking_quotes("msg = 'a'' ! keep'", None),
            ("msg = 'a'' ! keep'", None))

    def test_unterminated_literal_reports_its_open_quote(self) -> None:
        # So the next physical line continues inside the literal.
        self.assertEqual(
            strip_fortran_comment_tracking_quotes("msg = 'a&", None), ("msg = 'a&", "'"))

    def test_entering_mid_literal_the_bang_is_content(self) -> None:
        self.assertEqual(
            strip_fortran_comment_tracking_quotes("      &!b'", "'"), ("      &!b'", None))

    def test_real_trailing_comment_is_dropped(self) -> None:
        # Trailing whitespace survives here — the scanner, not the stripper, rstrips.
        self.assertEqual(
            strip_fortran_comment_tracking_quotes("x = 1  ! note", None), ("x = 1  ", None))

    def test_double_quoted_string_can_hold_a_bang(self) -> None:
        self.assertEqual(
            strip_fortran_comment_tracking_quotes('s = "! not a comment" ! real', None),
            ('s = "! not a comment" ', None))


class FortranLogicalLinesTests(unittest.TestCase):
    """`fortran_logical_lines`: one `(start_lineno, joined_text)` per logical line."""

    def test_lineno_and_whitespace_contract(self) -> None:
        # Leading whitespace of the FIRST physical line survives (callers anchor at `^\s*`);
        # trailing whitespace does not; blank and comment-only lines produce no entry; the
        # lineno is where the logical line STARTED.
        self.assertEqual(
            fortran_logical_lines("\n  ! header\n  x = 1   \n\n  y = 2\n"),
            [(3, "  x = 1"), (5, "  y = 2")])

    def test_semicolons_are_not_split_here(self) -> None:
        # `;`-splitting is the caller's composition step, not the scanner's.
        self.assertEqual(
            fortran_logical_lines("contains; subroutine foo()\n"),
            [(1, "contains; subroutine foo()")])

    def test_text_ending_mid_continuation_still_flushes(self) -> None:
        # The post-loop flush. A file whose last statement is left continued (a truncated write,
        # or an interface stanza sliced out of a larger source) must not lose that statement —
        # dropping it silently shrinks what a gate examines.
        self.assertEqual(fortran_logical_lines("y = 0\nx = 1 &\n"), [(1, "y = 0"), (2, "x = 1 ")])
        self.assertEqual(fortran_logical_lines("x = 1 &"), [(1, "x = 1 ")])

    def test_trailing_newline_yields_no_empty_entry(self) -> None:
        # `split("\n")` produces a trailing "" for every `\n`-terminated file; it must not
        # become a logical line.
        self.assertEqual(fortran_logical_lines("x = 1\n"), [(1, "x = 1")])
        self.assertEqual(fortran_logical_lines(""), [])

    # --- defect A: exotic line separators -------------------------------------------------

    def test_exotic_line_separators_do_not_end_a_comment(self) -> None:
        # `str.splitlines()` breaks on eight separators Fortran does not treat as line ends, so
        # a form feed inside a comment ended the comment early and re-admitted its tail AS CODE.
        for ch, name in (
            ("\x0c", "form feed"), ("\x0b", "vertical tab"), ("\x1c", "FS"), ("\x1d", "GS"),
            ("\x1e", "RS"), ("\x85", "NEL"), ("\u2028", "LINE SEPARATOR"),
            ("\u2029", "PARAGRAPH SEPARATOR"),
        ):
            with self.subTest(separator=name):
                self.assertEqual(
                    fortran_logical_lines(f"    ! prose {ch} use harness_mod\n    x = 1\n"),
                    [(2, "    x = 1")],
                    f"a {name} must not end the comment")

    def test_exotic_separator_before_a_sentinel_leaves_it_inside_the_comment(self) -> None:
        # The mirror direction, and dropping the tail is right: gfortran does not end the line at
        # a form feed either, so that `!$omp` is genuinely inside this statement's comment. The
        # separator line counts once, so the next statement's lineno is 2.
        self.assertEqual(
            fortran_logical_lines("    n = n\x0c!$omp parallel do\n    x = 1\n"),
            [(1, "    n = n"), (2, "    x = 1")])

    # --- defect B: `!` inside a continued literal ------------------------------------------

    def test_bang_inside_a_continued_character_literal(self) -> None:
        # Quote state must cross physical lines. Stripping per line could not know the `!` sat
        # inside a CONTINUED literal, so the rest of the line was dropped and — because what
        # survived ended in `&` — the buffer stayed open and swallowed the next statement.
        self.assertEqual(
            fortran_logical_lines("    msg = 'a&\n      &!b'\n    x = 1\n"),
            [(1, "    msg = 'a!b'"), (3, "    x = 1")])

    def test_a_resuming_line_is_never_mistaken_for_a_comment_line(self) -> None:
        # A `!` on a continuation line inside a literal is content, and the line is NOT a
        # comment line — its first nonblank is the resuming `&`, which is what the skip probe
        # keys on. Getting this wrong drops literal content.
        self.assertEqual(
            fortran_logical_lines("    msg = 'a&\n      &  ! still literal&\n      &b'\n"),
            [(1, "    msg = 'a  ! still literalb'")])

    def test_comment_lines_inside_an_open_literal_are_skipped_too(self) -> None:
        # F2008 3.3.2.4 resumes a continued character context on "the next line that is not a
        # comment line" — and a blank line IS a comment line (3.3.2.3). Verified against
        # gfortran 14.2 with a substring-bounds probe: `'abc&` / blank (or `! note`) /
        # `      &def'` compiles at `-std=f2008` with `s(6:6)` legal and `s(7:7)` out of range,
        # i.e. one string `abcdef`. Terminating the statement at the gap instead spills the
        # rest of the literal out AS CODE, which is how a quoted `open(` became a file-I/O
        # violation and a `;` inside a literal invented a published subroutine.
        for gap, label in (("\n", "blank line"), ("  ! note\n", "comment line")):
            with self.subTest(gap=label):
                self.assertEqual(
                    fortran_logical_lines("s = 'abc&\n" + gap + "      &def'\n"),
                    [(1, "s = 'abcdef'")])

    def test_unterminated_literal_does_not_leak_into_the_next_line(self) -> None:
        # Only reachable from source no compiler accepts, but without the per-line reset a
        # leaked open quote makes the following text read as literal content.
        self.assertEqual(
            fortran_logical_lines("    msg = 'oops\n    x = 1  ! note\n"),
            [(1, "    msg = 'oops"), (2, "    x = 1")])

    # --- defect C: the join separator ------------------------------------------------------

    def test_continuation_without_a_leading_ampersand_joins_with_a_space(self) -> None:
        # The line break is a token separator unless the next line starts with `&`.
        # Concatenating turned `do&` / `i = 1, n` into `doi = 1, n`.
        self.assertEqual(
            fortran_logical_lines("    do&\n      i = 1, n\n"), [(1, "    do i = 1, n")])

    def test_ampersand_led_continuation_joins_tight(self) -> None:
        # The one case that DOES split a token keeps joining without a separator.
        self.assertEqual(
            fortran_logical_lines("    do con&\n      &current (i = 1:n)\n"),
            [(1, "    do concurrent (i = 1:n)")])

    def test_a_literal_always_joins_tight(self) -> None:
        # A line break cannot insert a blank into a character literal, so the space-join rule
        # stops at the quote. The second form omits the resuming `&` — non-conforming, but
        # gfortran ACCEPTS it as an extension (issue #25 promotes `-Werror=ampersand` at the
        # syntax gate, so it no longer reaches one; this scanner still reads it, because it must
        # agree with the compiler on what a source MEANS, including sources the gate rejects).
        # Both compile to `abcdef` (pinned with a compile-time `1/(len(s) - N)` probe against
        # gfortran 14.2 at `-std=f2008`).
        for resume, label in (("      &def'", "conforming `&`-led resume"),
                              ("      def'", "gfortran's missing-`&` extension")):
            with self.subTest(resume=label):
                self.assertEqual(fortran_logical_lines("s = 'abc&\n" + resume + "\n"),
                                 [(1, "s = 'abcdef'")])

    def test_blanks_are_gfortrans_set_not_pythons(self) -> None:
        # `str.strip()` folds far more than free-form Fortran calls a blank. The characters it
        # adds are ordinary CONTENT to the compiler — the same difference `str.splitlines()`
        # made at the other end of the line. Stripping a `\v` here let the `&` behind it be read
        # as a continuation marker, where gfortran reads both as part of the string: `'abc&` /
        # `\v&def'` compiles (`-Wampersand` fires) to `abc<VT>&def`, length 8, pinned with
        # `1/merge(0, 1, (len(sa) == 8) .and. (sa == 'abc'//achar(11)//'&def'))`.
        for ch, name in (("\x0b", "vertical tab"), ("\x1c", "FS"), ("\x85", "NEL"),
                         ("\xa0", "NBSP"), ("\u2028", "LINE SEPARATOR")):
            with self.subTest(blank_candidate=name):
                self.assertEqual(fortran_logical_lines(f"sa = 'abc&\n{ch}&def'\n"),
                                 [(1, f"sa = 'abc{ch}&def'")], f"{name} is not a Fortran blank")
        # The three that ARE gfortran blanks still behave as blanks.
        self.assertEqual(fortran_logical_lines("x = 1 \t\f\n"), [(1, "x = 1")])
        self.assertEqual(fortran_logical_lines("s = 'ab&\n \t\f&cd'\n"), [(1, "s = 'abcd'")])

    def test_the_blank_set_is_the_same_at_all_three_decision_points(self) -> None:
        # The set is used to decide a comment line, a trailing continuation marker, and a
        # resume's column padding. Only the third is reachable on source the compiler accepts
        # (the test above); these two are the same rule at the other two points, and they are
        # pinned so the three cannot drift apart — a set that is correct in one place and
        # Python's default in another is how this class of defect got in.
        # A `\v`-only line is not all-blank, so it is a continuation line, not a comment line:
        # the statement it interrupts ends there rather than joining across it.
        # (The second space is the token-separator join: the resume is not `&`-led.)
        self.assertEqual(fortran_logical_lines("x = 1 + &\n\x0b\n2\n"),
                         [(1, "x = 1 +  \x0b"), (3, "2")])
        # And a `&` is the continuation marker only when it is the LAST non-blank: a `\v` behind
        # it is content, so this line does not continue.
        self.assertEqual(fortran_logical_lines("x = 1 + &\x0b\n2\n"),
                         [(1, "x = 1 + &\x0b"), (2, "2")])

    def test_doubled_quote_escape_split_by_the_wrap(self) -> None:
        # The compiler's character context outlives this scanner's here: in `'ab'&` / `'cd'` the
        # literal looks closed at end of line, but gfortran's lookahead reads the two quotes as
        # one escaped quote. Pinned at `-std=f2008` with `1/merge(0, 1, (len(sa) == 5) .and.
        # (sa == 'ab''cd'))`, which fires — and gfortran emits NO diagnostic, so a source
        # written this way passes the syntax gate. Unlike the missing-`&` resume, no `-Werror`
        # closes this one (issue #25 leaves it open, by construction: there is nothing to
        # promote). A space join would emit `'ab' 'cd'`, which is not Fortran at all.
        for resume, label in (("'cd'", "resume in column 1"),
                              ("     'cd'", "resume indented")):
            with self.subTest(resume=label):
                self.assertEqual(fortran_logical_lines("sa = 'ab'&\n" + resume + "\n"),
                                 [(1, "sa = 'ab''cd'")])
        self.assertEqual(fortran_logical_lines('sa = "ab"&\n"cd"\n'), [(1, 'sa = "ab""cd"')])
        self.assertEqual(fortran_logical_lines("sa = 'ab'&\n'cd'&\n'ef'\n"),
                         [(1, "sa = 'ab''cd''ef'")])
        # And it must not fire when the resume does not start with that same quote: there the
        # literal really did close and the line break is an ordinary token separator.
        self.assertEqual(fortran_logical_lines("call f('ab'&\n, 'cd')\n"),
                         [(1, "call f('ab' , 'cd')")])
        # The lookahead is quote-SPECIFIC, not "whatever character ended the line". Without that
        # restriction `do&` would memo an `o` and tight-join `of x` into `doof x`.
        self.assertEqual(fortran_logical_lines("do&\nof x\n"), [(1, "do of x")])

    def test_escape_lookahead_crosses_a_wrap_line_that_contributes_nothing(self) -> None:
        # The lookahead is over the next CONTRIBUTED character, not the next physical line. A
        # `&&` wrap line contributes none — it is all marker — so it must not clear the memo.
        # gfortran compiles `'ab'&` / `      &&` / `'cd'` to `ab'cd` (length 5, pinned, and with
        # no diagnostic at all). Clearing there resurrects the exact `'ab' 'cd'` the escape rule
        # exists to prevent, one wrap line further out.
        self.assertEqual(fortran_logical_lines("sa = 'ab'&\n      &&\n'cd'\n"),
                         [(1, "sa = 'ab''cd'")])
        self.assertEqual(fortran_logical_lines("sa = 'ab'&\n  &&\n  &&\n'cd'\n"),
                         [(1, "sa = 'ab''cd'")])

    def test_trailing_blanks_after_the_continuation_marker(self) -> None:
        # This, not CRLF, is what makes the `.rstrip()` load-bearing: `do i &   ` is legal and
        # its `&` must still register. (A CRLF source cannot exercise it — `read_text`
        # universal-newline-translates `\r\n` before the scanner ever sees it.)
        self.assertEqual(
            fortran_logical_lines("    do i &   \n      = 1, n\n"), [(1, "    do i  = 1, n")])

    # --- defect D: the lone-`&` continuation line ------------------------------------------

    def test_continuation_line_that_is_only_its_leading_ampersand(self) -> None:
        # The single `&` of a `&`-only line (or `&! note`) was read as BOTH the leading and the
        # trailing marker, so the buffer stayed open and glued the next statement on. gfortran
        # terminates the statement there.
        for wrap, label in (("      &\n", "bare"), ("      &! wrap note\n", "with a comment")):
            with self.subTest(wrap=label):
                self.assertEqual(
                    fortran_logical_lines("    msg = 'x' &\n" + wrap + "    x = 1\n"),
                    [(1, "    msg = 'x' "), (3, "    x = 1")])

    # --- the blank/comment skip inside a wrap ----------------------------------------------

    def test_comment_or_blank_line_inside_a_continuation_is_skipped(self) -> None:
        # Free form permits blank and comment-only lines BETWEEN a `&` line and its
        # continuation; they are ignored, not terminators. The §5.1 `write_perf` header exceeds
        # the 132-column limit and MUST wrap, so this is a live shape, not a curiosity. The
        # start lineno stays at the line the statement opened on.
        self.assertEqual(
            fortran_logical_lines(
                "subroutine hx__wp(a, &\n"
                "  ! a comment inside the wrap\n"
                "\n"
                "    b, c)\n"),
            [(1, "subroutine hx__wp(a,  b, c)")])

    def test_a_continuation_may_repeat_the_ampersand_after_a_skipped_line(self) -> None:
        self.assertEqual(
            fortran_logical_lines("  subroutine f(a, &\n    ! note\n\n       & b, c)\n"),
            [(1, "  subroutine f(a,  b, c)")])


class SplitFortranStatementsTests(unittest.TestCase):
    """`split_fortran_statements`: top-level `;` only, parts returned as-is."""

    def test_semicolon_inside_a_string_is_not_a_separator(self) -> None:
        self.assertEqual(
            split_fortran_statements("write(*,*) 'a;b'; x = 1"),
            ["write(*,*) 'a;b'", " x = 1"])

    def test_semicolon_inside_parens_is_not_a_separator(self) -> None:
        # Paren depth matters for the `[1:n; 2]`-shaped text a malformed source can produce; no
        # legal Fortran puts a statement separator inside parentheses.
        self.assertEqual(split_fortran_statements("call f(a; b)"), ["call f(a; b)"])

    def test_unbalanced_close_paren_does_not_silence_later_separators(self) -> None:
        # Depth clamps at zero. Left negative it would stay negative for the rest of the line
        # and no later `;` would split — and the statement a `;` hides is often a declaration,
        # so the harm direction is "published operation reported absent".
        self.assertEqual(split_fortran_statements("end subroutine); x = 1"),
                         ["end subroutine)", " x = 1"])

    def test_parts_are_neither_stripped_nor_filtered(self) -> None:
        # The non-stripping contract: one caller anchors patterns at `^\s*` on these parts, so
        # trimming here would silently break it. Empty parts survive too — callers decide.
        self.assertEqual(split_fortran_statements("  a = 1 ;; b = 2"),
                         ["  a = 1 ", "", " b = 2"])
        self.assertEqual(split_fortran_statements("   "), ["   "])

    def test_single_statement_passes_through(self) -> None:
        self.assertEqual(split_fortran_statements("just one statement"),
                         ["just one statement"])


class SplitTopLevelCommasTests(unittest.TestCase):
    """`split_top_level_commas`: the consolidation of four copies that disagreed.

    `validate_pipeline_semantics` defined this twice ~9,500 lines apart — the later
    quote-UNAWARE definition shadowed the earlier quote-aware one — plus a third time under the
    name `_split_fortran_names`; `orchestration_runtime` held a fourth. The first test is the
    reproducer at splitter level; the consumer-level ones live with their gates in
    `test_validate_pipeline_semantics`."""

    def test_comma_inside_a_character_literal_is_not_a_separator(self) -> None:
        # The reproduced defect: every surviving copy split inside the literal. Both harm
        # directions were reachable — a phantom identifier suppressing a Generate.static
        # dependency-dataflow violation (fail-open), and a truncated + phantom §5.1 declaration
        # atom (fail-closed). The consumer-level reproducers live with their gates in
        # `test_validate_pipeline_semantics`.
        self.assertEqual(split_top_level_commas("sep = ','"), ["sep = ','"])
        self.assertEqual(split_top_level_commas("a, 'x,y', b"), ["a", " 'x,y'", " b"])
        self.assertEqual(split_top_level_commas('a, "x,y", b'), ["a", ' "x,y"', " b"])

    def test_doubled_quote_escape_keeps_the_literal_closed(self) -> None:
        # Fortran escapes a quote by doubling it. The plain toggle leaves the literal at the
        # second quote and re-enters at the third with no character in between, so a comma
        # after the escape is still inside the literal.
        self.assertEqual(split_top_level_commas("s = 'it''s, fine', t"),
                         ["s = 'it''s, fine'", " t"])

    def test_comma_inside_parens_or_brackets_is_not_a_separator(self) -> None:
        # An entity list: the array-spec comma and the array-constructor / coarray-codimension
        # comma are part of one entity, not separators between entities.
        self.assertEqual(split_top_level_commas("a(:), b(2,2), c"),
                         ["a(:)", " b(2,2)", " c"])
        self.assertEqual(split_top_level_commas("x = [1,2], y"), ["x = [1,2]", " y"])

    def test_unbalanced_close_does_not_silence_later_separators(self) -> None:
        # Depth clamps at zero, as in `split_fortran_statements`. Left negative it would stay
        # negative for the rest of the text and drop every later entity of the list.
        self.assertEqual(split_top_level_commas("a), b"), ["a)", " b"])
        self.assertEqual(split_top_level_commas("a], b"), ["a]", " b"])

    def test_parts_are_neither_stripped_nor_filtered(self) -> None:
        self.assertEqual(split_top_level_commas("  a ,, b "), ["  a ", "", " b "])
        self.assertEqual(split_top_level_commas(""), [""])

    def test_single_item_passes_through(self) -> None:
        self.assertEqual(split_top_level_commas("just one"), ["just one"])

    def test_the_continuation_rule_follows_whether_the_text_is_masked(self) -> None:
        # On RAW text this splitter cannot see comments, so for it a comment-only line HAS opened
        # a literal; skipping past it to the `&` above would carry that bogus literal onward and
        # swallow every later separator (`c` here).
        self.assertEqual(split_top_level_commas("a, &\n! it's here\nb, c"),
                         ["a", " &\n! it's here\nb", " c"])
        # On MASKED text the same line is visibly a comment and must be skipped, as
        # `fortran_logical_lines` skips it — otherwise the literal closes at the wrong newline,
        # the closing quote on the resume line reads as an opening one, and `b` is swallowed
        # instead. Both errors lose a list item, and a lost item is a lost violation.
        masked = mask_code_lookalikes("a, 'x&\n! note\n&y', b")
        self.assertEqual(len(split_top_level_commas(masked)), 3, masked)

    def test_a_newline_closes_an_open_literal_unless_continued(self) -> None:
        # Defence for a caller handing over raw text. An apostrophe in a comment must not open a
        # literal that swallows every later separator...
        self.assertEqual(split_top_level_commas("a, & ! it's\n b, c"),
                         ["a", " & ! it's\n b", " c"])
        # ...but a LEGALLY continued literal must survive the break, or its closing quote reads
        # as an opening one and the rest of the text is swallowed instead. Both errors were
        # observed silencing Generate.static; the decision is `continuation_state_after_line`.
        self.assertEqual(split_top_level_commas("a, 'x, &\n     &y', b"),
                         ["a", " 'x, &\n     &y'", " b"])

    def test_mask_preserves_length_and_blanks_code_lookalikes(self) -> None:
        for text in ("  banner = 'x&\n! progress note\n      &end subroutine y'\n",
                     "  banner = 'x&\n\n      &end subroutine y'\n",
                     "  banner = 'x&\n      &end subroutine y'\n",
                     "  a = 1 ! end subroutine\n  b = 'end subroutine'\n",
                     "  s = 'abc\n  t = 1\n"):
            masked = mask_code_lookalikes(text)
            self.assertEqual(len(masked), len(text), text)
            # The whole point: no `end subroutine` survives from a comment or a literal, in any
            # of these shapes. A comment or blank line BETWEEN a `&` and its resume does not end
            # the continuation, so the literal's tail must stay masked across it — masking it as
            # terminated left the tail live, which is what truncates a subroutine envelope.
            self.assertNotIn("end subroutine", masked, text)

    def test_mask_is_linear_in_a_run_of_skipped_continuation_lines(self) -> None:
        # The continuation rule is a FORWARD fold. Stated as a backward search from each newline
        # it rescanned the whole run of skipped lines every time, so a legal continued literal
        # spanning a comment block was quadratic: 8,000 comment lines took 4.6 s to mask, and a
        # deterministic gate that slow on generated source is a defect of its own. Doubling the
        # run must roughly double the work, not quadruple it.
        def _mask(n: int) -> float:
            text = "  banner = 'x&\n" + "! note\n" * n + "      &end'\n"
            start = time.perf_counter()
            masked = mask_code_lookalikes(text)
            self.assertNotIn("end'", masked[masked.index("!"):])
            return time.perf_counter() - start

        _mask(2000)  # warm the interpreter so the first call is not the slow one
        small = min(_mask(2000) for _ in range(3))
        large = min(_mask(8000) for _ in range(3))
        # 4x the input. Linear predicts ~4x; the quadratic form was ~16x. 8x leaves room for a
        # noisy machine while still failing the shape this pins.
        self.assertLess(large, small * 8, f"{small=} {large=} — looks superlinear")

    def test_mask_keeps_delimiters_and_the_continuation_marker(self) -> None:
        self.assertEqual(mask_code_lookalikes("x = 'rate &\n&more' ! note & tail"),
                         "x = '     &\n&    ' !            ")

    def test_separator_must_be_one_character_the_brackets_do_not_claim(self) -> None:
        # Raised, not asserted, so `python3 -O` cannot elide it: a two-character separator
        # (Fortran's `//`) matches nothing per-character and would return the input unsplit —
        # a silently dead gate, the shape this consolidation exists to remove.
        with self.assertRaises(ValueError):
            _split_top_level("a//b", "//", "(", ")")
        with self.assertRaises(ValueError):
            _split_top_level("a(b", "(", "([", ")]")



class Section51NormalizationTests(unittest.TestCase):
    """The §5.1 view of the scanner: `fortran_logical_line_texts` + `normalize_fortran_line`.

    These moved here with the two functions, which used to be private helpers of
    `validate_pipeline_semantics` that the language backend had to import back out of the
    neutral core (docs/BACKEND_BOUNDARY.md). The gates that consume them stay neutral, so
    their own tests stayed in `test_validate_pipeline_semantics`."""

    def test_continuation_join_skips_interleaved_comment_and_blank(self) -> None:
        # Free-form Fortran allows blank / full-comment lines inside a `&` continuation; the join
        # must span them (the §5.1 write_perf header is >132 cols and MUST wrap).
        joined = fortran_logical_line_texts(
            "subroutine hx__wp(a, &\n"
            "  ! a comment inside the wrap\n"
            "\n"
            "    b, c)\n")
        self.assertEqual(len(joined), 1)
        self.assertEqual(normalize_fortran_line(joined[0]), "subroutinehx__wp(a,b,c)")

    def test_the_section51_view_does_not_split_on_semicolons(self) -> None:
        # The docstring of `fortran_logical_line_texts` asserts this as its distinguishing
        # property against the validator's `;`-splitting `_iter_fortran_logical_lines`: the
        # interface-stanza parser wants each header AS WRITTEN. Adding a `;` split changed the
        # atoms of 79 real corpus files and left the whole suite green — the property the
        # adapter's own docstring claims had no observer.
        text = "associate(unused_z_b=>z_b); end associate\n"
        self.assertEqual(fortran_logical_line_texts(text), ["associate(unused_z_b=>z_b); end associate"])
        # ... while the statement splitter, which is a different view over the same scan, does.
        self.assertEqual(split_fortran_statements(fortran_logical_line_texts(text)[0]),
                         ["associate(unused_z_b=>z_b)", " end associate"])

    def test_normalization_joins_continuations_and_folds_case(self) -> None:
        # A continuation-split, differently-cased, comment-bearing header normalizes to the
        # same canonical line as its single-line form.
        joined = fortran_logical_line_texts(
            "SUBROUTINE Hx__Foo(a, &  ! keep going\n     b)  ! done\n")
        self.assertEqual(len(joined), 1)
        self.assertEqual(
            normalize_fortran_line(joined[0]), "subroutinehx__foo(a,b)")

    def test_comment_strip_honors_strings(self) -> None:
        # The stripper is now the shared one (issue #23); `None` is the "this physical line does
        # not start inside a character literal" state, and the returned state is discarded here.
        line = strip_fortran_comment_tracking_quotes(
            "s = '! not a comment' ! real comment", None)[0]
        self.assertEqual(normalize_fortran_line(line), "s='!notacomment'")


if __name__ == "__main__":  # pragma: no cover - manual invocation
    unittest.main()
