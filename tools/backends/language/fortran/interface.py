#!/usr/bin/env python3
"""A certified Fortran source's PUBLISHED interface, as a consumer's leaf is shown it
(issue #289, R4-b PR-3).

Moved unchanged out of `tools/orchestration_runtime.py`, where the dependency facts a consuming
node's `Generate` is oriented with are resolved: the `<prefix>`-named subroutines a source
defines, and one subroutine's dummy-argument interface (argument order, each dummy's type,
intent and rank, and the prototype a procedure-typed dummy references). The runtime reaches it
through the `signatures` capability (`signatures.published_interface` /
`signatures.prefixed_procedures`), asked of the consumer's target language.

Imports nothing from the neutral core.
"""

from __future__ import annotations

import re
from typing import Any

from tools.backends.language.fortran import lines as fortran_lines

# `subroutine` header opener: optional prefixes (pure / elemental / recursive / impure /
# module), then `subroutine <name>` with an OPTIONAL `(` — a zero-argument subroutine is
# legally declared without a parameter list (`subroutine dep__ping`), and the fallback must
# still discover it. `lparen` is captured so the extractor can distinguish the two forms.
# Case-insensitive, name captured for selection. `.match` anchors at the logical-line start,
# so only declaration lines match (a `call`/`end subroutine` line starts with another token).
_FORTRAN_SUBROUTINE_RE = re.compile(
    r"(?:(?:pure|impure|elemental|recursive|module)\s+)*"
    r"subroutine\s+(?P<name>[A-Za-z]\w*)\s*(?P<lparen>\()?",
    re.IGNORECASE,
)



# An `interface` block's span (issue #266). The ABSTRACT opener is mirrored VERBATIM from
# `source._ABSTRACT_INTERFACE_SPAN_OPEN_RE` / `_INTERFACE_SPAN_END_RE` (the validator's
# published-surface scanner, in this package since issue #289's R4-b PR-3; before that the two
# lived in modules that could not import each other); the cross-scanner parity test pins them. The
# prefixed-name scanner skips abstract spans only (a plain interface body declares an external
# the module may re-export, which is an entry point); the prototype reader opens on either
# form. Each opener is the whole statement, so a variable named `interface` opens nothing.
_ABSTRACT_INTERFACE_SPAN_OPEN_RE = re.compile(r"^\s*abstract\s+interface\s*$", re.IGNORECASE)



_INTERFACE_SPAN_OPEN_RE = re.compile(
    r"^\s*(?:abstract\s+)?interface(?:\s*$|\s+[A-Za-z])", re.IGNORECASE)



_INTERFACE_SPAN_END_RE = re.compile(r"^\s*end\s*interface\b", re.IGNORECASE)



def _split_fortran_statements(logical_line: str) -> list[str]:
    """Split one comment-stripped, continuation-joined Fortran logical line into its
    individual statements at top-level ``;`` separators — a ``;`` inside a ``'``/``"`` string
    literal or inside parentheses is NOT a separator. Returns stripped, non-empty statements
    (``[]`` for blank). Fortran permits several statements on one line
    (``contains; subroutine foo()``); scanning at the statement level, not the raw line, keeps
    the declaration discovery robust to that form. NEVER raises. For the common
    one-statement-per-line source this returns the single stripped line unchanged.

    The split itself is ``fortran_lines.split_fortran_statements`` (shared with the validator,
    issue #23); this wrapper is what adds the strip, the empty-drop and the fail-soft envelope
    this module's callers rely on."""
    try:
        if not isinstance(logical_line, str):
            return []
        return [part.strip()
                for part in fortran_lines.split_fortran_statements(logical_line)
                if part.strip()]
    except Exception:
        return []



def _fortran_logical_lines(source_text: str) -> list[str]:
    """Collapse Fortran ``source_text`` into logical STATEMENTS: strip ``!`` comments,
    join ``&`` continuations, split each joined line at top-level ``;`` separators
    (``_split_fortran_statements``), and drop lines that are blank / comment-only.

    Statements come back STRIPPED — ``_FORTRAN_SUBROUTINE_RE`` is applied with ``.match()``,
    which anchors at position 0, and it carries no leading ``\\s*``; the indentation the shared
    scanner preserves would therefore block it, so it is removed here rather than carried.
    Semicolon-packed statements (``contains; subroutine foo()``) are separated so a declaration
    after a ``;`` is still seen. NEVER raises (``[]`` on non-str / error). Shared by
    ``_extract_subroutine_interface`` and ``_list_prefixed_subroutines`` so both see the
    identical statement view. For one-statement-per-line source the result is unchanged from a
    plain strip+join.

    The scanning itself is ``fortran_lines.fortran_logical_lines`` (issue #23) — one
    implementation shared with the validator's two Fortran gates, so a comment, a continuation
    or a character literal can no longer be read one way here and another way there. A line
    that is blank or comment-only is skipped whether or not a continuation is in progress (a
    full-line comment is permitted between continuation lines and must not flush the buffer,
    or a wrapped header ``(...)`` would terminate early). That holds inside an open character
    literal too — F2008 3.3.2.4 resumes a continued character context on the next line that is
    not a comment line."""
    try:
        if not isinstance(source_text, str):
            return []
        logical: list[str] = []
        for _lineno, joined in fortran_lines.fortran_logical_lines(source_text):
            logical.extend(_split_fortran_statements(joined))
        return logical
    except Exception:
        return []



def _list_prefixed_subroutines(source_text: str, prefix: str) -> list[str]:
    """Return, in first-appearance order, the distinct ``subroutine`` names in
    ``source_text`` whose name begins (case-insensitive) with ``prefix``.

    Used as the FALLBACK dependency-interface surface when the IR authors an empty
    ``operations`` list for a component dependency (an authoring wobble): the certified
    dependency source is the authority on which ``<dep_spec_id>__`` entry points exist. The
    ``<dep_spec_id>__`` prefix IS the published-surface convention (this selects by prefix,
    not by a Fortran ``private``/``public`` attribute — same as the pre-existing extractor),
    so surfacing them all lets the pure leaf build its ``call``s against real symbol names /
    argument orders instead of inventing ones ``Generate.gate`` rejects. Surfacing an extra
    interface fact never forces a call, so an over-broad match is orientation-only, not a gate.
    A header inside an ``abstract interface`` block is a PROTOTYPE, not an entry point, and is
    skipped (issue #266); one inside a plain ``interface`` body counts, as before; de-duplicates
    repeat declarations. NEVER raises (``[]`` on any error)."""
    try:
        if not isinstance(prefix, str) or not prefix:
            return []
        pfx = prefix.lower()
        out: list[str] = []
        seen: set[str] = set()
        in_interface = 0
        for line in _fortran_logical_lines(source_text):
            if _INTERFACE_SPAN_END_RE.match(line):
                in_interface = max(0, in_interface - 1)
                continue
            if _ABSTRACT_INTERFACE_SPAN_OPEN_RE.match(line):
                in_interface += 1
                continue
            if in_interface:
                continue
            m = _FORTRAN_SUBROUTINE_RE.match(line)
            if m is None:
                continue
            name = m.group("name")
            if not name.lower().startswith(pfx):
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(name)
        return out
    except Exception:
        return []



def _extract_subroutine_interface(source_text: str, op_name: str) -> dict[str, Any] | None:
    """Extract the published interface of subroutine ``op_name`` from Fortran source.

    Returns ``{"interface": <the header as written, collapsed to one line>, "argument_order":
    [<dummy names in order>]}`` or ``None`` when the subroutine is absent / unparseable.
    The load-bearing datum is ``argument_order`` (Fortran is positional; a consumer that
    swaps args builds against a type/rank mismatch). ``interface`` carries the author's own
    tokens — nothing is re-rendered — but it is not byte-verbatim across a wrap: joining a
    continuation that does not begin with ``&`` contributes one space at the joint. It is
    rendered into the ``<dependency_facts>`` prompt lines, where that is inert.

    Robust to the real shapes Generate emits: free-form ``&`` continuations on the header
    (the generate SKILL forces wrapping at <=100 cols for fortitude S001), ``!`` comments
    (incl. full-line comment between continuations), case-insensitivity, leading
    ``pure``/``impure``/``elemental``/``recursive``/``module`` prefixes, several subroutines
    in one file (selects by name, not the first), and a zero-argument one declared
    WITHOUT a parameter list (``subroutine dep__ping`` -> ``argument_order: []``). NEVER raises.
    """
    try:
        if not isinstance(source_text, str) or not op_name:
            return None
        # 1) Strip `!` comments and join `&` continuations into logical lines, so a wrapped
        #    header `(...)` parses as one unit (see `_fortran_logical_lines` for the
        #    continuation rules; the comment strip carries character-literal state ACROSS
        #    physical lines, so a `!` inside a continued literal stays content).
        logical = _fortran_logical_lines(source_text)
        # 2) Find the `subroutine <op_name>(...)` header among possibly several.
        for idx, line in enumerate(logical):
            m = _FORTRAN_SUBROUTINE_RE.match(line)
            if m is None or m.group("name").lower() != op_name.lower():
                continue
            if m.group("lparen") is None:
                # Zero-argument subroutine declared WITHOUT a parameter list
                # (`subroutine dep__ping`, legal Fortran). No argument order to pin — a
                # `call <name>` / `call <name>()` needs none. Emit the joined header as scanned.
                return {
                    "interface": line[: m.end()].rstrip(),
                    "argument_order": [],
                    "arguments": [],
                }
            open_idx = line.index("(", m.end() - 1)
            depth = 0
            close_idx = None
            for i in range(open_idx, len(line)):
                if line[i] == "(":
                    depth += 1
                elif line[i] == ")":
                    depth -= 1
                    if depth == 0:
                        close_idx = i
                        break
            if close_idx is None:
                return None
            inner = line[open_idx + 1:close_idx]
            args = [a.strip() for a in inner.split(",") if a.strip()]
            header = line[: close_idx + 1].strip()
            # 3) Resolve each dummy's declared type/intent/rank from the body (orientation
            #    for the consumer's call authoring; best-effort, unknown when unparseable).
            decls = _parse_fortran_dummy_declarations(logical, idx, args)
            arguments = [
                {
                    "name": a,
                    **decls.get(
                        a.lower(),
                        {"type": None, "intent": None, "rank": None, "dimension": None},
                    ),
                }
                for a in args
            ]
            out: dict[str, Any] = {
                "interface": header,
                "argument_order": args,
                "arguments": arguments,
            }
            # 4) A procedure-typed dummy (issue #266) names a prototype; the consumer must pass
            #    a procedure of exactly that shape, so the prototype's own lines ride along.
            #    Keyed by prototype name; a name the source does not declare in an interface
            #    block is simply absent (orientation, never a gate).
            prototypes: dict[str, list[str]] = {}
            for arg in arguments:
                proto_name = _procedure_interface_name(arg.get("type"))
                if proto_name:
                    # Stated on the argument, so a renderer of these facts need not parse the
                    # type text (`procedure_interface` below).
                    arg["procedure_interface"] = proto_name
                if proto_name and proto_name not in prototypes:
                    lines = _extract_interface_prototype(logical, proto_name)
                    if lines:
                        prototypes[proto_name] = lines
            if prototypes:
                out["procedure_interfaces"] = prototypes
            return out
        return None
    except Exception:
        return None



def _extract_interface_prototype(logical: list[str], proto_name: str) -> list[str] | None:
    """The statements of the prototype named ``proto_name`` inside an ``interface`` block of
    ``logical`` (comment-stripped, continuation-joined statements): its header and every
    declaration up to its own terminator, verbatim. The header is found by NAME — the
    statement inside a block that names the prototype followed by its argument list (or
    nothing) and carries no ``::`` — so both procedure kinds are read; a prototype body
    carries specification statements only, so the first statement opening with ``end``
    closes it, and its two scope statements (host association, the implicit-typing rule) are
    left out: they belong to an interface body — a consumer's leaf that copied the
    host-association one into the procedure it writes would earn a syntax refusal (the other
    is merely redundant there). ``None`` when no interface block
    declares that name. Orientation only; pure over a list of statement strings (its one
    caller's own envelope catches)."""
    # A HEADER of that name — the existing header pattern for one kind, and `function <name>`
    # for the other — never a statement that merely mentions the name (`external <name>`, a
    # generic block's listing line: a round-2 reviewer measured both being extracted as a
    # one-line "prototype" by a name-only match).
    function_re = re.compile(r"\bfunction\s+" + re.escape(proto_name) + r"\s*(?:\(|$)", re.IGNORECASE)
    in_interface = 0
    for idx, line in enumerate(logical):
        if _INTERFACE_SPAN_END_RE.match(line):
            in_interface = max(0, in_interface - 1)
            continue
        if _INTERFACE_SPAN_OPEN_RE.match(line):
            in_interface += 1
            continue
        if not in_interface:
            continue
        head = line.strip()
        if "::" in head or not head or head.lower().split("(", 1)[0].split()[0].startswith("end"):
            continue
        m = _FORTRAN_SUBROUTINE_RE.match(head)
        if not ((m is not None and m.group("name").lower() == proto_name.lower())
                or function_re.search(head)):
            continue
        out = [head]
        for body in logical[idx + 1:]:
            stripped = body.strip()
            if not stripped:
                continue
            first = stripped.lower().split("(", 1)[0].split()[0]
            if first.startswith("end"):
                break
            # Any spelling of either statement (`import::dp`, `import, only: dp`, a spec list).
            if re.match(r"(?:import|implicit)\b", stripped, re.IGNORECASE):
                continue
            out.append(stripped)
        return out
    return None



# Type keywords that open a Fortran declaration. `double precision` is two words and must be
# matched before `real`-style single tokens. Case-insensitive at the call site.
_FORTRAN_TYPE_KEYWORDS = (
    "double precision",
    "real",
    "integer",
    "logical",
    "complex",
    "character",
    "type",
    "class",
    # A dummy PROCEDURE, `procedure(<prototype>) :: f` (issue #266): resolved with the
    # `procedure(<prototype>)` text as its type, rank 0 and no intent, so the consumer's leaf
    # sees which prototype the procedure it passes must match.
    "procedure",
)



def _rank_of_shape(shape: str) -> int | None:
    """Rank implied by a Fortran array-spec string: depth-aware count of top-level, non-empty
    comma segments. A trailing coarray codimension `[...]` does not add array rank. Returns
    ``None`` for an assumed-rank dummy ``(..)`` — its rank is not statically known, so
    omit-on-doubt (never emit a wrong number).

    `:,:`->2, `nx,ny`->2, `n`->1, `*`->1, `n,*`->2, `size(x),n`->2, empty->0, `..`->None.
    """
    s = shape.strip()
    if s.endswith("]") and "[" in s:
        s = s[: s.rindex("[")].strip()
    if not s:
        return 0
    segs = [seg.strip() for seg in fortran_lines.split_top_level_commas(s) if seg.strip()]
    if any(seg == ".." for seg in segs):
        return None
    return len(segs)



def _paren_inner(text: str) -> str | None:
    """Content of the first balanced `(...)` group at the start of `text` (leading spaces
    allowed), or ``None`` when `text` does not open with a balanced paren group."""
    s = text.lstrip()
    if not s.startswith("("):
        return None
    depth = 0
    for i, ch in enumerate(s):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return s[1:i]
    return None



def _split_first_double_colon(line: str) -> tuple[str | None, str]:
    """Split `line` on the first `::` at paren/bracket depth 0. Returns ``(None, line)`` when
    there is no top-level `::`."""
    depth = 0
    i = 0
    while i < len(line) - 1:
        ch = line[i]
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif ch == ":" and line[i + 1] == ":" and depth == 0:
            return line[:i], line[i + 2:]
        i += 1
    return None, line



def _consume_type_spec(left: str, type_kw: str) -> tuple[str, str]:
    """Given a declaration LEFT side starting with `type_kw`, split off the verbatim type-spec
    (keyword + immediately-adjacent `(kind/len)` or legacy `*N`) from the trailing attributes.

    Returns ``(type_text, rest)``; `rest` still leads with the attribute list (typically a
    leading comma). `character(len=*)` keeps its parens as PART OF THE TYPE, never a shape.
    """
    idx = len(type_kw)
    rest = left[idx:]
    stripped = rest.lstrip()
    lead_ws = rest[: len(rest) - len(stripped)]
    if stripped.startswith("("):
        inner = _paren_inner(stripped)
        if inner is not None:
            plen = len(inner) + 2
            type_text = left[:idx] + lead_ws + stripped[:plen]
            return type_text.strip(), stripped[plen:]
    elif stripped.startswith("*"):
        after_star = stripped[1:].lstrip()
        # legacy `*(*)` / `*(len)` char-length spec — keep the parens as part of the type.
        if after_star.startswith("("):
            inner = _paren_inner(after_star)
            if inner is not None:
                star_ws = stripped[1: len(stripped) - len(after_star)]
                consumed = "*" + star_ws + after_star[: len(inner) + 2]
                type_text = left[:idx] + lead_ws + consumed
                return type_text.strip(), after_star[len(inner) + 2:]
        # legacy `*N` kind/length.
        m = re.match(r"\*\s*\d+", stripped)
        if m:
            type_text = left[:idx] + lead_ws + m.group(0)
            return type_text.strip(), stripped[m.end():]
    return left[:idx].strip(), rest



def _is_type_def_open(low: str) -> bool:
    """True when a lowercased logical line OPENS a derived-type definition block
    (``type :: t`` / ``type, bind(c) :: t`` / obsolescent ``type t``), as opposed to a
    variable declaration ``type(foo) :: x`` or a ``type is (...)`` select-type guard."""
    if not low.startswith("type"):
        return False
    rest = low[4:]
    if not rest:
        return True                      # bare `type` (defensive)
    if rest[0] == "(":
        return False                     # `type(foo) :: x` — a variable declaration
    if rest[0] in ",:":
        return True                      # `type, ... :: t` / `type :: t`
    if rest[0] == " ":
        nxt = rest.strip()
        # `type is (...)` / `type is(...)` is a select-type guard, not a definition.
        if nxt.startswith("is") and (len(nxt) == 2 or not (nxt[2].isalnum() or nxt[2] == "_")):
            return False
        return True                      # `type name` (obsolescent definition form)
    return False



def _is_enum_open(low: str) -> bool:
    """True when a lowercased logical line OPENS an ``enum, bind(c)`` definition block —
    distinct from an ``enumerator`` statement (which also starts with ``enum``)."""
    return low == "enum" or low.startswith("enum,") or low.startswith("enum ")



def _is_block_open(low: str) -> bool:
    """True when a lowercased logical line OPENS an F2008 ``block`` construct (with an optional
    construct label ``name: block``) — distinct from a ``block data`` program unit or an
    identifier that merely starts with ``block``."""
    m = re.match(r"(?:[a-z]\w*\s*:\s*)?block\b", low)
    if m is None:
        return False
    return not (low.startswith("blockdata") or low.startswith("block data"))



def _parse_one_declaration(
    line: str,
) -> tuple[str, str | None, list[tuple[str, int | None, str | None]]] | None:
    """Parse a Fortran type-declaration statement into ``(type_text, intent, entities)`` where
    `entities` is ``[(name, rank, dimension), ...]``. Returns ``None`` when `line` is not a
    confidently-parseable type declaration (no top-level `::`, or the leading token is not a
    recognized type keyword). Never raises for a parse miss — the caller treats it as unknown.
    """
    if "::" not in line:
        return None
    left, right = _split_first_double_colon(line)
    if left is None:
        return None
    low_left = left.strip().lower()
    type_kw = None
    for kw in _FORTRAN_TYPE_KEYWORDS:
        if (
            low_left == kw
            or low_left.startswith(kw + " ")
            or low_left.startswith(kw + "(")
            or low_left.startswith(kw + "*")
            or low_left.startswith(kw + ",")
        ):
            type_kw = kw
            break
    if type_kw is None:
        return None
    type_text, rest = _consume_type_spec(left.strip(), type_kw)
    intent: str | None = None
    shared_shape: str | None = None
    for attr in fortran_lines.split_top_level_commas(rest):
        al = attr.strip().lower()
        if al.startswith("intent"):
            m = re.search(r"intent\s*\(\s*(in\s*out|inout|in|out)\s*\)", al)
            if m:
                intent = m.group(1).replace(" ", "")
        elif al.startswith("dimension"):
            inner = _paren_inner(attr.strip()[len("dimension"):])
            if inner is not None:
                shared_shape = inner
    # `has_dim_attr` distinguishes "no dimension attribute" (a bare entity is a scalar, rank 0)
    # from "dimension attribute present but its rank is unknown" (e.g. `dimension(..)` -> None).
    has_dim_attr = shared_shape is not None
    shared_rank = _rank_of_shape(shared_shape) if has_dim_attr else None
    entities: list[tuple[str, int | None, str | None]] = []
    for ent in fortran_lines.split_top_level_commas(right):
        ent = ent.strip()
        if not ent:
            continue
        mname = re.match(r"([A-Za-z]\w*)", ent)
        if not mname:
            continue
        name = mname.group(1)
        after = ent[mname.end():].lstrip()
        if after.startswith("("):
            shape = _paren_inner(after)
            if shape is not None:
                entities.append((name, _rank_of_shape(shape), shape))
                continue
        if has_dim_attr:
            entities.append((name, shared_rank, shared_shape))
        else:
            entities.append((name, 0, None))
    if not entities:
        return None
    return type_text, intent, entities



def _parse_fortran_dummy_declarations(
    logical: list[str], header_idx: int, argument_order: list[str]
) -> dict[str, dict[str, Any]]:
    """Resolve each dummy argument's declared ``{type, intent, rank, dimension}`` from the
    subroutine-body statements in `logical` following `header_idx`.

    Best-effort / never raises. Omit-on-doubt: a dummy whose declaration cannot be confidently
    parsed, or that is declared twice with a CONFLICTING rank, is left out of the returned map
    (the caller renders it as unknown). Only names in `argument_order` are recorded. A nested
    ``interface`` block and a derived-type definition (``type :: t`` … ``end type``) in the
    specification part are skipped — both declare names that are NOT this routine's dummies (an
    inner name colliding with a dummy must not poison it, and an ``end type`` must not be
    mistaken for the routine terminator). Scanning stops at the routine's ``contains`` / ``end``
    boundary.
    """
    wanted = {a.lower() for a in argument_order}
    resolved: dict[str, dict[str, Any]] = {}
    conflicted: set[str] = set()
    try:
        in_interface = 0
        in_type_def = 0
        in_block = 0
        for line in logical[header_idx + 1:]:
            low = line.strip().lower()
            if not low:
                continue
            # `abstract interface` opens an interface block too (its procedures' dummies are
            # NOT this routine's) — strip the `abstract` prefix before the opener test.
            tok = low[len("abstract "):].lstrip() if low.startswith("abstract ") else low
            if tok.startswith("interface") and not tok.startswith("interface("):
                in_interface += 1
                continue
            if low.startswith("end interface") or low.startswith("endinterface"):
                in_interface = max(0, in_interface - 1)
                continue
            if in_interface:
                continue
            # A derived-type / enum DEFINITION block declares components/enumerators, not this
            # routine's dummies. Skip its interior and, crucially, its `end type` / `end enum`
            # (else the terminator break below would stop the scan before later dummies / would
            # record an inner name's rank for a same-named dummy).
            if (
                low.startswith("end type") or low.startswith("endtype")
                or low.startswith("end enum") or low.startswith("endenum")
            ):
                in_type_def = max(0, in_type_def - 1)
                continue
            if _is_type_def_open(low) or _is_enum_open(low):
                in_type_def += 1
                continue
            if in_type_def:
                continue
            # An F2008 `block` construct's local declarations are NOT dummies (a dummy can only
            # be declared in the specification part). Skip its interior and its `end block` so a
            # block-local name never shadows a dummy's rank and `end block` is not mistaken for
            # the routine terminator. Handles an optional construct label `name: block`.
            if low.startswith("end block") or low.startswith("endblock"):
                in_block = max(0, in_block - 1)
                continue
            if _is_block_open(low):
                in_block += 1
                continue
            if in_block:
                continue
            head_tok = low.split("(", 1)[0].split()[0] if low.split() else ""
            if head_tok in ("end", "endsubroutine", "endfunction", "contains"):
                break
            parsed = _parse_one_declaration(line)
            if parsed is None:
                continue
            decl_type, intent, entities = parsed
            for name, rank, dim in entities:
                lname = name.lower()
                if lname not in wanted or lname in conflicted:
                    continue
                prev = resolved.get(lname)
                if prev is not None and prev.get("rank") != rank:
                    del resolved[lname]
                    conflicted.add(lname)
                    continue
                resolved[lname] = {
                    "type": decl_type,
                    "intent": intent,
                    "rank": rank,
                    "dimension": dim,
                }
    except Exception:
        return resolved
    return resolved


def _procedure_interface_name(type_text: Any) -> str | None:
    """The prototype name a resolved dummy type ``procedure(<name>)`` references, else None."""
    if not isinstance(type_text, str):
        return None
    m = re.fullmatch(r"procedure\s*\(\s*([A-Za-z]\w*)\s*\)", type_text.strip(), re.IGNORECASE)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------------------------
# How a consumer's leaf is SHOWN the interface read above (issue #289, R4-b PR-4 precondition).
# The neutral renderer (`orchestration_runtime._published_operations_lines`) walks the resolved
# dependency facts and states what is language-free — the dependency, the operation's header,
# the unresolved-name and signature-drift warnings. The call-site guidance around them names
# this language's argument passing, intents, array ranks and slices, and how a procedure
# argument is written; that is this module's, and it reaches the leaf only when the consumer's
# language is Fortran. Moved verbatim: the rendered block is byte-identical.
# ---------------------------------------------------------------------------------------------


def procedure_interface(arg: dict[str, Any]) -> str | None:
    """The prototype a procedure-typed dummy references, as `_extract_subroutine_interface`
    stated it on the argument (`procedure_interface`), else None."""
    name = arg.get("procedure_interface")
    return name.strip() if isinstance(name, str) and name.strip() else None


def dependency_operations_header(*, detailed: bool, procedure_argument: bool) -> str:
    """The paragraph that heads the published-operation lines. `detailed` when per-argument
    lines follow (the rank/shape guidance promises them); `procedure_argument` when one of
    those arguments takes a procedure."""
    if not detailed:
        return (
            "**Published dependency operations (conductor-resolved from each dependency's "
            "CERTIFIED source — the exact source Build will compile/link):** Fortran arguments "
            "are positional; call each operation with EXACTLY this argument order. A wrong "
            "order builds against a type/rank mismatch and fails the build (routed back to "
            "Generate). For generate.generate this is authoring-binding; for verify/validate "
            "it is the authoritative order to check the emitted `call` against."
        )
    header = (
        "**Published dependency operations (conductor-resolved from each dependency's "
        "CERTIFIED source — the exact source Build will compile/link):** Fortran arguments "
        "are positional; call each operation with EXACTLY this argument order. Each dummy "
        "argument's declared type, intent, and rank/shape is listed under its header — the "
        "actual argument you pass must MATCH the dummy's rank and shape. When a dummy is "
        "lower-rank than your full state array (e.g. a rank-2 `(:,:)` dummy vs. your rank-3 "
        "`(ncomp,:,:)` state), LOOP over the extra component/dimension and pass lower-rank "
        "slices; do NOT pass the whole higher-rank array. A wrong order OR a rank/shape "
        "mismatch builds against a type/rank mismatch and fails the build (routed back to "
        "Generate). For generate.generate this is authoring-binding; for verify/validate "
        "it is the authoritative order to check the emitted `call` against."
    )
    if procedure_argument:
        header += (
            " An argument marked as a PROCEDURE argument takes a procedure, not data: "
            "write one (an internal procedure of the calling routine, or a module "
            "procedure) with exactly the prototype the argument line names and pass "
            "its NAME as the actual; the compiler checks the shape, and a mismatch fails "
            "the build the same way."
        )
    return header


def prototype_heading(name: str) -> str:
    """The line above a procedure argument's prototype, listed verbatim from the source."""
    return (
        f"    prototype `{name}` — the procedure you pass for the "
        "argument above must declare EXACTLY these dummies (names may differ; "
        "type, kind, rank and intent may not; write it as an ordinary "
        "procedure of yours, with these declarations and nothing an interface "
        "body needs):")


def argument_detail_lines(
    arguments: Any, *, carried_prototypes: frozenset[str] = frozenset()
) -> list[str]:
    """Render indented per-dummy-argument ``type / intent / rank-N (shape)`` lines for one
    published operation, or ``[]`` when `arguments` is absent or every entry is unresolved
    (fully backward-compatible header-only fallback). Orientation-only: an unresolved rank is
    marked explicitly and NEVER implied as a number. A procedure-typed dummy names its
    prototype; ``carried_prototypes`` says which prototypes the caller renders under the
    operation, so the line promises a listing only when one follows.
    """
    if not isinstance(arguments, list) or not arguments:
        return []
    # A resolved rank is a plain int (bool excluded); anything else is treated as unknown so a
    # malformed/hand-edited entry can never render a garbage `rank-<x>` line.
    def _known_rank(a: Any) -> bool:
        return isinstance(a, dict) and isinstance(a.get("rank"), int) and not isinstance(
            a.get("rank"), bool)

    if not any(_known_rank(a) for a in arguments):
        return []
    lines: list[str] = []
    for arg in arguments:
        if not isinstance(arg, dict):
            continue
        name = str(arg.get("name", "")).strip()
        if not name:
            continue
        proto_name = procedure_interface(arg)
        if proto_name:
            where = ("listed under this operation" if proto_name in carried_prototypes else
                     "the dependency declares (it could not be read host-side, so match the "
                     "argument's role and let Build verify the shape)")
            lines.append(
                f"    {name}: {str(arg.get('type')).strip()} — a PROCEDURE argument: pass a "
                f"procedure whose interface is EXACTLY the prototype `{proto_name}` {where} "
                "(your own module or internal procedure; it may reach your grid, parameters "
                "and fields by host association)")
            continue
        if not _known_rank(arg):
            # Rank could not be resolved host-side. Do NOT tell the leaf to read the
            # dependency source — a pure leaf reads nothing but its launch prompt, and the
            # dependency's source is not inlined there. Give an actionable fallback instead:
            # pass the argument per the operation's role and let Build's compiler verify the
            # final rank.
            lines.append(
                f"    {name}: (rank/shape not resolved — pass this argument per the "
                "operation's role; Build verifies the final rank)"
            )
            continue
        rank = arg.get("rank")
        parts: list[str] = []
        atype = str(arg.get("type") or "").strip()
        if atype:
            parts.append(atype)
        intent = arg.get("intent")
        if intent:
            parts.append(f"intent({intent})")
        if rank == 0:
            parts.append("rank-0 (scalar)")
        else:
            dim = str(arg.get("dimension") or "").strip()
            parts.append(f"rank-{rank} ({dim})" if dim else f"rank-{rank}")
        lines.append(f"    {name}: " + ", ".join(parts))
    return lines
