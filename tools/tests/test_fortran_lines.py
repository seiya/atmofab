#!/usr/bin/env python3
"""Unit tests for tools/fortran_lines — the shared free-form Fortran logical-line scanner.

These pin the scanner directly. Most of them are the tests written for the issue #22 OpenMP
floor (commit `166946a`) and deleted with it in `5f3ccf6`, re-targeted from floor verdicts to
scanner output: the inputs are the same compiling sources, but the assertion is now on the
logical lines themselves, which is what the issue #23 consumers read. Consumer-level
reproducers live with their gates in `test_orchestration_runtime` /
`test_validate_pipeline_semantics`.
"""

from __future__ import annotations

import unittest

from tools.fortran_lines import (
    fortran_logical_lines,
    split_fortran_statements,
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
            ("\x1e", "RS"), ("\x85", "NEL"), (" ", "LINE SEPARATOR"),
            (" ", "PARAGRAPH SEPARATOR"),
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

    def test_mid_literal_wrap_requires_an_immediate_continuation(self) -> None:
        # Inside an open literal there is no blank/comment skip: those lines ARE literal
        # content, so the join must take them verbatim rather than probing them as comments.
        self.assertEqual(
            fortran_logical_lines("    msg = 'a&\n      &  ! still literal&\n      &b'\n"),
            [(1, "    msg = 'a  ! still literalb'")])

    def test_blank_line_inside_an_open_literal_is_not_skipped(self) -> None:
        # What the `quote is None` guard on the blank/comment skip buys. Mid-literal a blank line
        # is not a wrap gap — the standard requires the continuation to be immediate (its first
        # nonblank must be `&`), so this source is illegal either way. The guard decides WHICH
        # illegal reading: terminate here (and let the next line be its own statement), or skip
        # and let the open literal swallow `x = 1` as literal content. Swallowing following code
        # is the harmful one — it is how a live statement disappears from a gate's view.
        self.assertEqual(
            fortran_logical_lines("    msg = 'a&\n\n    x = 1\n"),
            [(1, "    msg = 'a "), (3, "    x = 1")])

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

    def test_parts_are_neither_stripped_nor_filtered(self) -> None:
        # The non-stripping contract: one caller anchors patterns at `^\s*` on these parts, so
        # trimming here would silently break it. Empty parts survive too — callers decide.
        self.assertEqual(split_fortran_statements("  a = 1 ;; b = 2"),
                         ["  a = 1 ", "", " b = 2"])
        self.assertEqual(split_fortran_statements("   "), ["   "])

    def test_single_statement_passes_through(self) -> None:
        self.assertEqual(split_fortran_statements("just one statement"),
                         ["just one statement"])


if __name__ == "__main__":  # pragma: no cover - manual invocation
    unittest.main()
