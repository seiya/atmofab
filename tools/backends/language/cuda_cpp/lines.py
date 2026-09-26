"""CUDA C++ source text as the deterministic gates read it (issue #289, R4-b PR-4).

One primitive, `mask`: the source with every comment and the CONTENTS of every string and character
literal replaced by blanks, byte for byte, newlines kept. Everything this backend reads structurally
(`signatures`, `source`) reads the masked text, so a brace, a parenthesis, a `for`, a `;` or a
`#pragma` spelled inside a comment or a literal is not seen — and every offset and line number of
the masked text is the original's. Literal DELIMITERS stay, so a caller can still tell that a
literal was there, and `literals` hands back the original contents where a rule is about them.

`strip_preprocessor` then blanks every preprocessor directive line (with its backslash
continuations), for the readers that must not see `#include` / `#define` text as declarations.

The kernel-launch spelling `name<<<grid, block>>>(args)` needs no special case: the readers here
track `()`, `{}` and `[]` only, and angle brackets only inside a declaration's parameter list, where
a launch cannot stand.

Stdlib only.
"""

from __future__ import annotations

import re

#: A raw string literal's opening: `R"delim(`, optionally prefixed (`u8R`, `LR`, `uR`, `UR`).
_RAW_OPEN = re.compile(r'(?:u8|[uUL])?R"([^()\\\s]{0,16})\(')


def _blank(text: str) -> str:
    """`text` with every character but a newline replaced by a space."""
    return "".join("\n" if ch == "\n" else " " for ch in text)


def mask(text: str) -> str:
    """Comments and literal contents blanked, length and newlines preserved.

    Handles `//` and `/* */` comments, `"..."` and `'...'` literals with backslash escapes, and raw
    string literals `R"d(...)d"`. A line-continuation backslash inside a `//` comment continues the
    comment, as the language says. An unterminated literal or comment is blanked to the end of the
    text: the compiler rejects that source, and a reader here must not see its tail as code."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            j = i
            while j < n:
                if text[j] == "\n":
                    # A backslash immediately before the newline continues the comment.
                    if j > i and text[j - 1] == "\\":
                        j += 1
                        continue
                    break
                j += 1
            out.append(_blank(text[i:j]))
            i = j
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            j = n if end < 0 else end + 2
            out.append(_blank(text[i:j]))
            i = j
            continue
        raw = _RAW_OPEN.match(text, i) if ch in "uULR" else None
        if raw is not None and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_")):
            close = ")" + raw.group(1) + '"'
            end = text.find(close, raw.end())
            j = n if end < 0 else end + len(close)
            body_end = n if end < 0 else end
            out.append(text[i:raw.end()])
            out.append(_blank(text[raw.end():body_end]))
            out.append(text[body_end:j])
            i = j
            continue
        if ch in "\"'":
            # A `'` inside a pp-number is a digit separator (`1'000'000`, `.5'0`, `1e+1'0`), not a
            # literal: walk back over the pp-number (digits, letters, `_`, `.`, `'`, and a sign
            # after an exponent letter) and require it to START like a number — a digit, or a `.`
            # followed by a digit. The `.5'0` start is round 2 of this change's review: read as a
            # literal, it blanked the rest of the line, a `_Pragma` or a literal metric included.
            if ch == "'" and i > 0 and text[i - 1].isalnum() and i + 1 < n and text[i + 1].isalnum():
                prev = i - 1
                while prev >= 0 and (text[prev].isalnum() or text[prev] in "_.'"
                                     or (text[prev] in "+-" and prev > 0
                                         and text[prev - 1] in "eEpP")):
                    prev -= 1
                first = prev + 1
                if text[first].isdigit() or (text[first] == "." and first + 1 < n
                                             and text[first + 1].isdigit()):
                    out.append(ch)
                    i += 1
                    continue
            j = i + 1
            while j < n and text[j] != ch and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            if j < n and text[j] == ch:
                out.append(ch + _blank(text[i + 1:j]) + ch)
                i = j + 1
            else:
                out.append(ch + _blank(text[i + 1:j]))
                i = j
            continue
        out.append(ch)
        i += 1
    masked = "".join(out)
    assert len(masked) == len(text)
    return masked


def literal_spans(text: str) -> list[tuple[int, int, str]]:
    """Every ordinary string literal of `text` as `(offset of its opening quote, offset just past
    its closing quote, contents)`, in source order — the contents between the quotes, escapes left
    as written. Raw literals are included with their body; character literals are not."""
    masked = mask(text)
    found: list[tuple[int, int, str]] = []
    i, n = 0, len(text)
    while i < n:
        if masked[i] == '"':
            j = masked.find('"', i + 1)
            if j < 0:
                break
            found.append((i, j + 1, text[i + 1:j]))
            i = j + 1
            continue
        i += 1
    return found


def literals(text: str) -> list[tuple[int, str]]:
    """Every ordinary string literal of `text` as `(line number, contents)`, in source order."""
    return [(text.count("\n", 0, start) + 1, contents)
            for start, _end, contents in literal_spans(text)]


# Any preprocessing whitespace before the `#` — a form feed and a vertical tab included, which the
# compiler accepts there (round 2 of this change's review: `\f#if 0` was read as code).
_DIRECTIVE_START = re.compile(r"^[^\S\n]*#", re.MULTILINE)


def strip_preprocessor(masked: str) -> str:
    """`masked` with every preprocessor directive line blanked, continuation lines included.
    Expects `mask`'s output, so a `#` inside a literal or comment is not a directive start."""
    lines = masked.split("\n")
    out: list[str] = []
    continuing = False
    for line in lines:
        if continuing or _DIRECTIVE_START.match(line):
            continuing = line.rstrip().endswith("\\")
            out.append(" " * len(line))
        else:
            out.append(line)
    return "\n".join(out)


def line_of(text: str, offset: int) -> int:
    """The 1-based line number of `offset` in `text`."""
    return text.count("\n", 0, offset) + 1
