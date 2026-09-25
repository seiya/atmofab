"""Fortran language backend for the language-neutral published-interface representation.

Objective B (``docs`` plan ``swift-mixing-dijkstra``): ``controlled_spec`` §5.1 and the IR
``public_api.signatures`` are moving from a verbatim *Fortran* interface block to a language-neutral
*structured* representation. The Fortran-specific knowledge — how a structured signature renders to a
Fortran stanza, and how a Fortran stanza parses back to the structured form — lives HERE, in one
backend module, instead of being spread across the deterministic gates.

This module exposes two pure functions plus the struct vocabulary, over a §5.1 stanza layer
(``parse_interface_stanzas`` / ``stanza_atoms`` / ``stanza_line_list`` / ``stanza_line_set`` /
``declaration_atoms`` / ``canonicalize_end_line``) that the neutral gates also call directly:
splitting a §5.1 block into per-symbol stanzas and reducing a stanza to its comparison atoms is
Fortran knowledge, and the gates that compare them are neutral.

- ``parse_signatures_from_fortran(block_body)`` — a §5.1 Fortran interface block → the structured
  representation (``{module_parameters, types, interfaces, procedures}``). Built on this module's own stanza
  splitter (``parse_interface_stanzas``) over the shared logical-line scanner in ``lines``, so it
  inherits their comment / continuation / case / whitespace handling.
- ``render_signatures_to_fortran(struct)`` — the inverse: the structured representation → a canonical
  Fortran interface block whose *normalized* lines (comments stripped, ``&`` joined, case-folded,
  whitespace-erased) are token-for-token what the current §5.1 gates compare.

The correctness contract is the round-trip on the REAL harness §5.1 (see
``tools/tests/test_fortran_signatures.py``): for every published symbol,
``normalized_stanza_lines(render(parse(real_block)))`` equals ``normalized_stanza_lines(real_block)``.
As long as that holds, switching §5.1 / the IR to the structured form leaves every downstream
signature comparison byte-for-byte unchanged — the gate renders the structured form back to the exact
Fortran lines it already knows how to compare against a generated ``.f90``.

The struct vocabulary is language-neutral throughout: the neutral ``type`` names (``real`` /
``integer`` / ``logical`` / ``string`` / ``derived`` / ``procedure`` — not ``character`` /
``type(...)`` / ``procedure(...)``), the
string-length tokens (``deferred`` / ``assumed`` — not ``:`` / ``*``), and the module-parameter
kind values (``float64`` / ``float32`` — not ``real64`` / ``real32``). The Fortran spellings are
produced only by the renderer here; the old Fortran tokens fail closed in the neutral form.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from tools.backends.language.fortran import lines as fortran_lines

# The neutral structured form's loader and error class are re-exported: the `signatures`
# capability contract names them, and the neutral gates reach them through this module.
from tools.structured_signatures import (  # noqa: F401  (re-export)
    SignatureParseError,
    load_structured_signatures,
)
from tools.structured_signatures import (
    validate_module_parameter as _validate_module_parameter,
)
from tools.structured_signatures import validate_procedure as _validate_procedure
from tools.structured_signatures import validate_struct as _validate_struct
from tools.structured_signatures import validate_symbol as _validate_symbol

# --- §5.1 canonical interface block: stanza parsing + comparison atoms ---------------------------
#
# §5.1 gives the exact published surface as a fenced Fortran interface block. Two deterministic
# gates consume it: the ``--stage compile`` gate cross-checks its symbol set against §5, and the
# ``Generate.static`` gate pins the generated model source against each signature's interface
# lines. Both compare after a normalization that erases every non-semantic difference — inline
# comments, ``&`` continuations, case, and whitespace — so a signature authored one way in §5.1
# and formatted another way in the generated source still matches (and a genuine argument-name /
# type / rank / intent drift still fails).
#
# This layer used to live in ``tools/validate_pipeline_semantics.py``, and this module imported it
# back out of the neutral core — a backend reaching into the neutral core for its own subject
# matter (`docs/BACKEND_BOUNDARY.md`, the blocking sub-item of TODO.md's validator area). The
# gates that consume it are neutral and reach it here, through the language backend.

_IFACE_PROC_START = re.compile(
    r"^\s*(?:pure\s+|elemental\s+|recursive\s+)*(subroutine|function)\s+([A-Za-z0-9_]+)",
    re.IGNORECASE,
)
# ``end\s*`` (space optional) accepts the legal no-space free-form keywords endsubroutine /
# endfunction / endtype as well as the spaced forms.
_IFACE_PROC_END = re.compile(r"^\s*end\s*(?:subroutine|function)\b", re.IGNORECASE)
# The type DEFINITION header is `_TYPE_HEADER_RE`, defined with the other header patterns below
# and used here rather than restated: moving the stanza layer into this module put a byte-identical
# copy of it beside the original, which is the drift shape this file already carries a warning
# about. The stanza parser and `_parse_type` must agree on what a type header IS — a header the
# splitter accepts and the parser rejects is a stanza with no lowering — so they read one pattern.
_IFACE_TYPE_END = re.compile(r"^\s*end\s*type\b", re.IGNORECASE)
# An `interface` BLOCK (`abstract interface`, a bare `interface`, or a generic `interface <name>`
# / `interface operator(...)` / `interface assignment(=)`). The whole logical line is matched:
# `interface = 3` / `interface(2) = 1` — a VARIABLE named `interface`, which Fortran permits
# because keywords are not reserved — do not open a block. Inside the block a procedure header is
# a PROTOTYPE (a named interface with no body), not a published procedure, and it lands in
# ``iface_stanzas`` rather than ``op_stanzas``.
_IFACE_OPEN_RE = re.compile(
    r"^\s*(?:abstract\s+interface|interface(?:\s+(?:operator\s*\(.*\)|assignment\s*\(\s*=\s*\)"
    r"|[A-Za-z][A-Za-z0-9_]*))?)\s*$",
    re.IGNORECASE,
)
_IFACE_BLOCK_END = re.compile(r"^\s*end\s*interface\b", re.IGNORECASE)
# Lines of a prototype body that carry no ABI: `import [:: names]` brings host entities into the
# interface body's scope and `implicit none` is the mapping rule. Both are legal (and `implicit
# none` is required by the lint gate, C002) in a prototype the leaf writes, and neither belongs
# in the stanza a gate compares, so the splitter drops them. A generic interface's `module
# procedure <name>` / `procedure <name>` listing is likewise not a prototype; it is dropped by
# position (it sits inside the block but outside any prototype), not by this pattern.
_IFACE_BODY_NOISE_RE = re.compile(r"^\s*(?:import\b|implicit\s+none\b)", re.IGNORECASE)

# The `subroutine` / `function` alternatives are REACHED — `source_atoms` runs
# `canonicalize_end_line` over every logical line of real model source, which rewrites
# 1,316 such lines across the in-tree corpus — but no consumer's ANSWER depends on them:
# the only atoms ever looked up in that set are module `parameter` declarations, and a
# procedure stanza excludes its own terminator by construction. Recorded with the right
# predicate: an earlier note said "no consumer reaches", which is measurably false and
# would mislead anyone pruning the pattern.
_END_STMT_RE = re.compile(r"^\s*end\s*(type|subroutine|function)\b", re.IGNORECASE)


def canonicalize_end_line(line: str) -> str:
    """Reduce a closing ``end [type|subroutine|function] [name]`` to just ``end <kind>`` — the
    optional trailing construct name is legal to omit (bare ``end type`` is the common style) and
    is redundant with the header, so a stanza that drops it must still compare equal."""
    m = _END_STMT_RE.match(line)
    # `.lower()` is an INTENT MARKER, not a live guard: a census proved it cannot change an
    # answer. Both consumers are `stanza_atoms`, which normalizes the result to lower case
    # anyway, and a test that passes lower-case input. Kept because the canonical form is
    # defined as lower case; said so rather than left to read as load bearing.
    return f"end {m.group(1).lower()}" if m else line


def parse_interface_stanzas(
    block_body: str,
) -> tuple[dict[str, list[str]], dict[str, list[str]], dict[str, list[str]], list[str]]:
    """Parse a §5.1 canonical Fortran interface block into per-symbol *stanzas*.

    Returns ``(op_stanzas, type_stanzas, iface_stanzas, errors)``. Each stanza value is the
    ordered list of that symbol's interface logical lines (comment-stripped, continuation-joined,
    but NOT yet whitespace-normalized — kept readable for gate messages):
    - a procedure stanza is its ``subroutine``/``function`` header + every dummy-argument /
      ``result`` declaration up to (but excluding) the ``end`` line;
    - a type stanza is its ``type :: name`` header + component declarations + the ``end type``
      line (inclusive, so the closing name is pinned too);
    - an interface stanza is a PROTOTYPE — a procedure header found inside an ``interface`` /
      ``abstract interface`` block — with the same shape as a procedure stanza, minus the
      ``import`` / ``implicit none`` lines a prototype body legally carries (they are scope
      plumbing, not ABI). A generic interface's ``module procedure`` listing is dropped.
    Lines outside any stanza (the public ``parameter`` declarations, comments, blanks) are
    ignored. An unterminated stanza or interface block, OR a duplicate symbol name — across all
    three kinds, so a prototype sharing a defined procedure's name is a duplicate too — is
    reported in ``errors`` (fail-closed at the caller — a duplicate must never silently overwrite,
    which would let a malformed first copy hide behind a correct second)."""
    lines = fortran_lines.fortran_logical_line_texts(block_body)
    op_stanzas: dict[str, list[str]] = {}
    type_stanzas: dict[str, list[str]] = {}
    iface_stanzas: dict[str, list[str]] = {}
    errors: list[str] = []
    seen: set[str] = set()
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        if _IFACE_OPEN_RE.match(line):
            i, closed = _collect_interface_block(lines, i + 1, iface_stanzas, seen, errors)
            if not closed:
                errors.append(f"unterminated interface block opened by '{line}'")
            continue
        m_type = _TYPE_HEADER_RE.match(line)
        m_proc = _IFACE_PROC_START.match(line)
        # The type-before-procedure order is an intent marker, not a live decision: the two
        # patterns are disjoint (one requires a leading `type`, the other a procedure keyword
        # after an optional prefix), so no line reaches both arms. Flipping the order changes no
        # answer; stated so it does not read as a precedence rule someone must preserve.
        if m_type:
            name = m_type.group(1)
            stanza = [line]
            i += 1
            closed = False
            while i < n:
                cur = lines[i].strip()
                # A following stanza header terminates this one so it cannot swallow the next
                # symbol — but a derived type MUST close with `end type` (a bare `end` does NOT
                # close a type in Fortran), so reaching a header first leaves `closed` False and
                # the stanza is reported unterminated (fail-closed). Reprocess the header on the
                # outer loop (do not advance).
                if cur and (_TYPE_HEADER_RE.match(cur) or _IFACE_PROC_START.match(cur)):
                    break
                if cur:
                    stanza.append(cur)
                if cur and _IFACE_TYPE_END.match(cur):
                    closed = True
                    i += 1
                    break
                i += 1
            if not closed:
                errors.append(f"unterminated derived-type definition '{name}'")
            if name in seen:
                errors.append(f"duplicate signature for symbol '{name}'")
            seen.add(name)
            type_stanzas[name] = stanza
        elif m_proc:
            name = m_proc.group(2)
            stanza = [line]
            i += 1
            closed = False
            while i < n:
                cur = lines[i].strip()
                if cur and _IFACE_PROC_END.match(cur):
                    closed = True
                    i += 1
                    break
                # A bare `end` (legal for a module procedure) is not matched above; terminate on the
                # next stanza header so it cannot swallow the following symbol (reprocess it). An
                # `interface` block opening inside a procedure body (an explicit interface for an
                # external the body calls) terminates the stanza the same way, so its prototypes
                # are read as prototypes and not as published procedures.
                if cur and (_IFACE_PROC_START.match(cur) or _TYPE_HEADER_RE.match(cur)
                            or _IFACE_OPEN_RE.match(cur)):
                    closed = True
                    break
                if cur:
                    stanza.append(cur)
                i += 1
            if not closed:
                errors.append(f"unterminated procedure interface '{name}'")
            if name in seen:
                errors.append(f"duplicate signature for symbol '{name}'")
            seen.add(name)
            op_stanzas[name] = stanza
        else:
            i += 1
    return op_stanzas, type_stanzas, iface_stanzas, errors


def _collect_interface_block(
    lines: list[str],
    i: int,
    iface_stanzas: dict[str, list[str]],
    seen: set[str],
    errors: list[str],
) -> tuple[int, bool]:
    """Read one ``interface`` block starting at the line AFTER its opener. Every prototype inside
    it lands in ``iface_stanzas``; returns ``(index after the block, closed)`` where ``closed`` is
    False when the input ended before ``end interface``. A prototype must close with its own
    ``end subroutine`` / ``end function``: reaching ``end interface`` or another prototype header
    first reports it unterminated (that line is then reprocessed, so the block still closes)."""
    n = len(lines)
    while i < n:
        cur = lines[i].strip()
        if not cur:
            i += 1
            continue
        if _IFACE_BLOCK_END.match(cur):
            return i + 1, True
        m_proc = _IFACE_PROC_START.match(cur)
        if not m_proc:
            i += 1  # `module procedure x`, a generic-listing `procedure x`, a comment-only line
            continue
        name = m_proc.group(2)
        stanza = [cur]
        i += 1
        closed = False
        while i < n:
            body = lines[i].strip()
            if body and _IFACE_PROC_END.match(body):
                closed = True
                i += 1
                break
            if body and (_IFACE_PROC_START.match(body) or _IFACE_BLOCK_END.match(body)):
                break
            if body and not _IFACE_BODY_NOISE_RE.match(body):
                stanza.append(body)
            i += 1
        if not closed:
            errors.append(f"unterminated interface prototype '{name}'")
        if name in seen:
            errors.append(f"duplicate signature for symbol '{name}'")
        seen.add(name)
        iface_stanzas[name] = stanza
    return i, False


def declaration_atoms(logical_line: str) -> list[str]:
    """Canonicalize a declaration into one line per declared entity, so a combined declarator
    (``integer, intent(in) :: a, b`` — legal Fortran, ABI-identical, and explicitly permitted by
    §5.1's "formatting may differ") compares equal to the one-per-line form. Non-declarations (a
    ``subroutine``/``function`` header, an ``end`` line — no ``::``) pass through unchanged. The
    shared type-spec + attributes (before ``::``) is prefixed onto each comma-separated entity of
    the entity list (after ``::``, split on top-level commas so an array-spec comma stays intact).
    """
    # Also an intent marker: the empty-entity fallback below subsumes it (a line with no `::`
    # partitions to an empty right-hand side, yields no entities, and returns the same list).
    # Deleting it changes no answer; it states the non-declaration case at the top instead of
    # leaving a reader to derive it from a fallback three lines down.
    if "::" not in logical_line:
        return [logical_line]
    lhs, _sep, rhs = logical_line.partition("::")
    entities = [e.strip() for e in fortran_lines.split_top_level_commas(rhs)]
    entities = [e for e in entities if e]
    if not entities:
        return [logical_line]
    lhs = lhs.strip()
    return [f"{lhs} :: {entity}" for entity in entities]


def stanza_atoms(lines: list[str]) -> tuple[str, ...]:
    """Ordered, normalized, per-entity atoms of a stanza — every declaration split into one atom
    per declared name (``declaration_atoms``) then normalized. This is the canonical comparison
    unit for both gates: it makes combined vs one-per-line declarations, and formatting /
    continuation / comment / case / whitespace differences, all compare equal, while a genuine
    name / type / rank / intent / component drift still differs. A closing ``end [type|subroutine|
    function] [name]`` is canonicalized to drop the optional trailing name (bare ``end type`` — the
    common Fortran style — is byte-for-ABI identical to ``end type <name>``; the type/proc name is
    already pinned by the header)."""
    out: list[str] = []
    for line in lines:
        line = canonicalize_end_line(line)
        for atom in declaration_atoms(line):
            norm = fortran_lines.normalize_fortran_line(atom)
            # `if norm:` FIRES on a reachable input, which is why it is not labelled inert like
            # its neighbours: the scanner and this normalizer disagree about "blank" by design —
            # the scanner uses gfortran's set (space, tab, form feed) so a U+00A0 stays content,
            # while this erases Python's wider `\s` — so a line of such characters survives the
            # scan and normalizes to empty. Without the filter it becomes an empty ATOM and breaks
            # the ordered stanza comparison.
            #
            # What it does NOT prevent, measured rather than asserted: a wrong verdict on a real
            # run. `gfortran -fsyntax-only -std=f2008` rejects every one of those characters
            # (`Error: Invalid character in name`), so no source carrying one can be certified.
            # An earlier version of this comment claimed the harm was over-rejection "on source
            # the compiler accepts", which a reviewer disproved by running the compiler. The
            # guard is kept because an empty atom is meaningless in any case, not because a live
            # input needs it. `test_a_line_of_exotic_blanks_produces_no_atom` is the witness.
            if norm:
                out.append(norm)
    return tuple(out)


def stanza_line_set(lines: list[str]) -> frozenset[str]:
    """Per-entity atom SET of a stanza — used where declaration order is immaterial (a procedure's
    dummy-argument declarations, which Fortran permits in any order and which the header line
    already pins for call order)."""
    return frozenset(stanza_atoms(lines))


def stanza_line_list(lines: list[str]) -> tuple[str, ...]:
    """Per-entity atom LIST of a stanza, order preserved — used where order is part of the contract
    (a derived type's component layout; a verbatim IR transcription)."""
    return stanza_atoms(lines)


def source_atoms(text: str) -> frozenset[str]:
    """Every comparison atom of a whole Fortran text, as a SET — the same per-entity atoms
    ``stanza_atoms`` produces, taken over every logical line rather than one stanza's lines.

    The view a caller needs to ask "does this source declare X anywhere", which is how the §5.1
    module ``parameter`` declarations are pinned: they are part of the published ABI but are not
    inside any stanza, so a stanza-scoped lookup cannot see them. Two callers had the same
    two-level comprehension over `fortran_logical_line_texts` + `stanza_atoms`; stating it once
    here also keeps the caller from needing the line scanner in its own right."""
    return frozenset(
        atom
        for line in fortran_lines.fortran_logical_line_texts(text)
        for atom in stanza_atoms([line])
    )


# --- struct vocabulary -------------------------------------------------------------------------
# The structured form this module renders, and its fail-closed validation, are language-neutral and
# live in `tools/structured_signatures.py` (issue #289, R4-b PR-4), whose header comment documents
# the vocabulary. What stays here is how that form lowers to Fortran and lifts back.

# --- neutral <-> Fortran token maps --------------------------------------------------------------
# The two closed vocabularies the renderer lowers to Fortran and the parser lifts back to neutral.
# A closed grammar (fail-closed on anything else) is what removes the residual Fortran spellings
# from the neutral IR: a stale ``len: ':'`` / ``value: real64`` no longer passes through silently.
#
# ``deferred`` / ``assumed`` (and the value tokens ``float64`` / ``float32``) are RESERVED words of
# this vocabulary, matched case-INSENSITIVELY (Fortran identifiers are case-insensitive, and the
# module-parameter value pin folds case) with lowercase as the canonical form. A consequence of a
# reserved vocabulary: a ``string`` length symbol may NOT be named ``deferred`` / ``assumed`` — such
# a ``len`` is read as the reserved token (rendering ``character(len=:)`` / ``(len=*)``), not a
# symbol reference. No Fortran symbol on the real harness surface collides, but a future spec must
# not name a length parameter ``deferred`` / ``assumed``.
_NEUTRAL_LEN_TO_FORTRAN = {"deferred": ":", "assumed": "*"}   # string length token
_FORTRAN_LEN_TO_NEUTRAL = {v: k for k, v in _NEUTRAL_LEN_TO_FORTRAN.items()}
_NEUTRAL_KIND_VALUES = {"float64": "real64", "float32": "real32"}   # module-parameter kind value
_FORTRAN_KIND_VALUES = {v: k for k, v in _NEUTRAL_KIND_VALUES.items()}

_INTENT_RE = re.compile(r"^intent\(\s*(in|out|inout)\s*\)$", re.IGNORECASE)
_MODULE_PARAM_RE = re.compile(
    r"^\s*integer\s*,\s*parameter\s*::\s*([A-Za-z0-9_]+)\s*=\s*(.+?)\s*$", re.IGNORECASE
)
# A strict extension of the stanza splitter's `_IFACE_PROC_START`: same prefix rule, plus the
# argument list and `result(...)`. It cannot be collapsed into one pattern (the group numbering
# differs), so `test_the_two_procedure_header_patterns_agree` pins that every header one accepts
# the other accepts.
_PROC_HEADER_RE = re.compile(
    r"^\s*(?:pure\s+|elemental\s+|recursive\s+)*(subroutine|function)\s+"
    r"([A-Za-z0-9_]+)\s*(?:\((.*?)\))?\s*(?:result\s*\(\s*([A-Za-z0-9_]+)\s*\))?\s*$",
    re.IGNORECASE,
)
# Also the stanza splitter's type-header test (see the stanza layer above): one pattern, so the
# splitter and the parser cannot disagree about what a type header is.
_TYPE_HEADER_RE = re.compile(
    r"^\s*type\s*(?:,\s*[^:()]*?)?::\s*([A-Za-z0-9_]+)\s*$", re.IGNORECASE
)


# --- parse: Fortran interface block -> structured signatures -------------------------------------

def _split_paren_aware(text: str) -> list[str]:
    """Split ``text`` on top-level commas (commas inside parentheses, brackets or quotes are
    kept). Delegates to `fortran_lines`, the shared splitter, so the handling matches the rest of
    the pipeline."""
    return [p for p in fortran_lines.split_top_level_commas(text)]


def _parse_type_spec(head: str) -> dict[str, Any]:
    """Parse the leading type-spec token of a declaration's left side (before any attribute), e.g.
    ``real(dp)`` / ``character(len=case_id_len)`` / ``type(foo)`` / ``integer`` / ``logical``."""
    head = head.strip()
    low = head.lower()
    if low.startswith("character"):
        m = re.search(r"\(\s*(?:len\s*=\s*)?([^)]*?)\s*\)", head, re.IGNORECASE)
        length = m.group(1).strip() if m else "1"
        # Lift the Fortran length token back to its neutral form (":" -> "deferred",
        # "*" -> "assumed"); a fixed width / symbol passes through unchanged. The parse thus
        # produces a neutral struct, symmetric with what render/validate accept.
        length = _FORTRAN_LEN_TO_NEUTRAL.get(length, length)
        return {"type": "string", "kind": None, "len": length, "name": None, "alloc": False}
    if low.startswith("type") and "(" in head:
        m = re.search(r"\(\s*([A-Za-z0-9_]+)\s*\)", head)
        if not m:
            raise SignatureParseError(f"derived type-spec missing name: {head!r}")
        return {"type": "derived", "kind": None, "len": None, "name": m.group(1), "alloc": False}
    if low.startswith("procedure"):
        # `procedure(<interface-name>)` — a dummy procedure with an explicit interface. A bare
        # `procedure()` / `procedure` (implicit interface) or `procedure(real)` (an intrinsic
        # type-spec standing for an implicit-interface function) has no neutral form: the
        # neutral vocabulary references a NAMED prototype only. Lifted to `interface: <name>`;
        # the reference is resolved against `interfaces[]` by `_validate_struct`.
        m = re.fullmatch(r"procedure\s*\(\s*([A-Za-z][A-Za-z0-9_]*)\s*\)", head, re.IGNORECASE)
        if not m or m.group(1).lower() in ("real", "integer", "logical", "character", "type"):
            raise SignatureParseError(
                f"unsupported procedure type-spec {head!r}; the neutral signature models a dummy "
                "procedure only as `procedure(<interface name>)` referencing a named prototype")
        return {"type": "procedure", "kind": None, "len": None, "name": None, "alloc": False,
                "interface": m.group(1)}
    for base in ("real", "integer", "logical"):
        if low.startswith(base):
            m = re.search(r"\(\s*(?:kind\s*=\s*)?([A-Za-z0-9_]+)\s*\)", head, re.IGNORECASE)
            kind = m.group(1).strip() if m else None
            return {"type": base, "kind": kind, "len": None, "name": None, "alloc": False}
    raise SignatureParseError(f"unrecognized type-spec: {head!r}")


def _parse_entities(rhs: str) -> list[tuple[str, int, list[str] | None]]:
    """Parse the entity list on the right of ``::`` into ``[(name, rank, dims), ...]``. ``rank`` is
    the number of dimensions from a trailing ``(...)`` (``(:)`` -> 1, ``(:,:)`` -> 2). ``dims`` is
    ``None`` for a pure assumed-shape declaration (every dim is ``:``) and the explicit token list
    otherwise (e.g. ``coef(3)`` -> ``['3']``), so a fixed bound round-trips instead of collapsing to
    assumed-shape."""
    out: list[tuple[str, int, list[str] | None]] = []
    for ent in _split_paren_aware(rhs):
        ent = ent.strip()
        if not ent:
            continue
        # Drop any initializer (``= value``) that appears outside parentheses.
        m = re.match(r"^([A-Za-z0-9_]+)\s*(\((.*)\))?", ent)
        if not m:
            raise SignatureParseError(f"unparseable entity: {ent!r}")
        name = m.group(1)
        dims_src = m.group(3)
        if dims_src is None or not dims_src.strip():
            out.append((name, 0, None))
            continue
        toks = [t.strip() for t in _split_paren_aware(dims_src)]
        explicit = None if all(t == ":" for t in toks) else toks
        out.append((name, len(toks), explicit))
    return out


def _parse_decl_line(line: str) -> list[dict[str, Any]]:
    """Parse one declaration logical line into a list of ``entity`` dicts (one per declared name).
    ``intent`` is populated from the attributes; a result/component simply has ``intent=None``."""
    if "::" not in line:
        raise SignatureParseError(f"declaration without '::': {line!r}")
    left, right = line.split("::", 1)
    parts = _split_paren_aware(left)
    if not parts:
        raise SignatureParseError(f"empty type-spec: {line!r}")
    spec = _parse_type_spec(parts[0])
    intent: str | None = None
    for attr in parts[1:]:
        a = attr.strip().lower()
        if a == "allocatable":
            spec["alloc"] = True
        elif a == "parameter":
            pass  # module parameters are handled separately (outside stanzas)
        else:
            m = _INTENT_RE.match(attr.strip())
            if m:
                intent = m.group(1).lower()
            else:
                # Fail closed on an attribute the neutral structure does not model — silently
                # dropping `dimension(:)` / `optional` / `pointer` / `value` would change the ABI
                # (e.g. `real, dimension(:) :: x` parsed as a scalar). The neutral form models only
                # type/kind/len, rank (via the entity's `(:)` dimensions), intent, and allocatable.
                raise SignatureParseError(
                    f"unsupported declaration attribute {attr.strip()!r} in {line!r}; the neutral "
                    "signature models only intent / allocatable (rank comes from the entity's "
                    "dimensions, e.g. `x(:)`, not a `dimension` attribute)")
    entities: list[dict[str, Any]] = []
    for name, rank, dims in _parse_entities(right):
        ent: dict[str, Any] = {"name": name, "rank": rank, "intent": intent, "spec": dict(spec)}
        if dims is not None:
            ent["dims"] = dims
        entities.append(ent)
    return entities


def _parse_procedure(header: str, body_lines: list[str]) -> dict[str, Any]:
    m = _PROC_HEADER_RE.match(header)
    if not m:
        raise SignatureParseError(f"unparseable procedure header: {header!r}")
    kind = m.group(1).lower()
    name = m.group(2)
    arg_names = [a.strip() for a in (m.group(3) or "").split(",") if a.strip()]
    result_name = m.group(4) or (name if kind == "function" else None)
    decls: dict[str, dict[str, Any]] = {}
    for bl in body_lines:
        for ent in _parse_decl_line(bl):
            decls[ent["name"]] = ent
    args: list[dict[str, Any]] = []
    for an in arg_names:
        if an not in decls:
            raise SignatureParseError(f"{name}: argument {an!r} has no declaration")
        args.append(decls[an])
    result_entity: dict[str, Any] | None = None
    if kind == "function":
        if result_name not in decls:
            raise SignatureParseError(f"{name}: result {result_name!r} has no declaration")
        result_entity = decls[result_name]
        result_entity["intent"] = None
    return {"kind": kind, "name": name, "args": args, "result": result_entity}


def _parse_type(header: str, body_lines: list[str]) -> dict[str, Any]:
    m = _TYPE_HEADER_RE.match(header)
    if not m:
        raise SignatureParseError(f"unparseable type header: {header!r}")
    name = m.group(1)
    components: list[dict[str, Any]] = []
    for bl in body_lines:
        low = bl.strip().lower()
        if low.startswith("end type") or low.startswith("type ") or low.startswith("type::"):
            continue
        for ent in _parse_decl_line(bl):
            ent["intent"] = None
            components.append(ent)
    return {"name": name, "components": components}


def parse_signatures_from_fortran(block_body: str) -> dict[str, Any]:
    """Parse a §5.1 canonical Fortran interface block into the structured representation.

    Module ``parameter`` lines (outside every stanza) become ``module_parameters``; procedure and
    derived-type stanzas become ``procedures`` / ``types``. Raises ``SignatureParseError`` on any
    stanza the backend cannot lower (fail-closed — a caller must not silently accept a partial parse).
    """
    op_stanzas, type_stanzas, iface_stanzas, errors = parse_interface_stanzas(block_body)
    if errors:
        raise SignatureParseError("; ".join(errors))

    module_parameters: list[dict[str, Any]] = []
    for logical in fortran_lines.fortran_logical_line_texts(block_body):
        mp = _MODULE_PARAM_RE.match(logical)
        if mp:
            module_parameters.append(
                {"name": mp.group(1), "base": "integer",
                 "value": _fortran_param_value_to_neutral(mp.group(2).strip())}
            )

    procedures = [
        _parse_procedure(lines[0], lines[1:]) for lines in op_stanzas.values()
    ]
    types = [
        _parse_type(lines[0], lines[1:-1]) for lines in type_stanzas.values()
    ]
    interfaces = [
        _parse_procedure(lines[0], lines[1:]) for lines in iface_stanzas.values()
    ]
    struct = {
        "module_parameters": module_parameters,
        "types": types,
        "interfaces": interfaces,
        "procedures": procedures,
    }
    # Parse must not emit a struct that render/validate would reject (the "parse-accepts /
    # validate-rejects asymmetry" — closed at the root here): a Fortran block the backend lifts
    # must lower back cleanly, so validate the neutral struct before returning it.
    _validate_struct(struct)
    return struct


def _fortran_param_value_to_neutral(value: str) -> str:
    """Lift a Fortran module-parameter VALUE to its neutral token: ``real64`` -> ``float64``,
    ``real32`` -> ``float32``, a plain integer literal passes through. Any other Fortran spelling
    (a kind expression such as ``selected_real_kind(15, 307)``, an identifier) has no neutral form —
    the pass-through was removed on purpose — so it fails closed."""
    low = value.strip().lower()
    if low in _FORTRAN_KIND_VALUES:
        return _FORTRAN_KIND_VALUES[low]
    if value.strip().isdigit():
        return value.strip()
    raise SignatureParseError(
        f"module-parameter value {value!r} has no neutral form; the neutral IR admits only a "
        "number or the kind tokens 'float64'/'float32' (the Fortran-expression pass-through, e.g. "
        "selected_real_kind(...), was removed)")


# --- render: structured signatures -> Fortran interface block ------------------------------------

def _map_neutral_len_to_fortran(length: str) -> str:
    """Lower a validated neutral string-length token to its Fortran spelling: ``deferred`` -> ``:``,
    ``assumed`` -> ``*``; a fixed width / symbol passes through unchanged (case preserved)."""
    return _NEUTRAL_LEN_TO_FORTRAN.get(length.strip().lower(), length)


def _map_neutral_param_value_to_fortran(value: Any) -> str:
    """Lower a validated neutral module-parameter value to its Fortran spelling: ``float64`` ->
    ``real64``, ``float32`` -> ``real32``; a number passes through as its decimal string."""
    if isinstance(value, int):  # bool already rejected by validation
        return str(value)
    return _NEUTRAL_KIND_VALUES.get(value.strip().lower(), value.strip())


def render_module_parameter_to_fortran(mp: dict[str, Any]) -> str:
    """Render ONE neutral module parameter to its Fortran declaration line
    (``integer, parameter :: <name> = <value>``), lowering the neutral kind value to its Fortran
    spelling. Fail-closed (``SignatureParseError``) on a malformed parameter. This is the single
    source both ``render_signatures_to_fortran`` and the deterministic Generate.static source pin
    (``generated_source_violations``) render through, so a §5.1
    ``float64`` deterministically demands ``dp = real64`` in the generated source."""
    _validate_module_parameter(mp, "module_parameter")
    return f"integer, parameter :: {mp['name']} = {_map_neutral_param_value_to_fortran(mp['value'])}"


def _render_spec(spec: dict[str, Any]) -> str:
    # Callers render only AFTER validation, so required fields (string `len`, derived `name`) are
    # known present; the accesses below cannot KeyError on a validated struct.
    t = spec["type"]
    if t == "string":
        base = f"character(len={_map_neutral_len_to_fortran(spec['len'])})"
    elif t == "derived":
        base = f"type({spec['name']})"
    elif t in ("real", "integer", "logical"):
        base = t if not spec.get("kind") else f"{t}({spec['kind']})"
    elif t == "procedure":
        base = f"procedure({spec['interface']})"
    else:
        raise SignatureParseError(f"cannot render spec type {t!r}")
    if spec.get("alloc"):
        base += ", allocatable"
    return base


def _render_dims(rank: int, dims: list[str] | None = None) -> str:
    # Explicit `dims` (e.g. ['3'] for a fixed bound) render verbatim; otherwise `rank` assumed-shape
    # colons. This lets a signature express `coef(3)` and not only assumed-shape `(:)`.
    if dims:
        return "(" + ",".join(dims) + ")"
    return "" if not rank else "(" + ",".join([":"] * rank) + ")"


def _render_entity(ent: dict[str, Any]) -> str:
    spec = _render_spec(ent["spec"])
    intent = ent.get("intent")
    attr = f", intent({intent})" if intent else ""
    return f"{spec}{attr} :: {ent['name']}{_render_dims(ent.get('rank', 0), ent.get('dims'))}"


def _render_procedure(proc: dict[str, Any]) -> list[str]:
    name = proc["name"]
    args = proc.get("args", [])  # validation tolerates an omitted args list (a no-arg procedure);
    arg_names = ", ".join(a["name"] for a in args)  # render must too, not KeyError on proc["args"].
    lines: list[str] = []
    if proc["kind"] == "function":
        result = proc["result"]
        # Fortran forbids a `result` name equal to the function name; that form is the IMPLICIT
        # result (the function name IS the result variable), so omit the clause — emitting
        # `function f(...) result(f)` would be invalid source.
        if result["name"] == name:
            lines.append(f"function {name}({arg_names})")
        else:
            lines.append(f"function {name}({arg_names}) result({result['name']})")
    else:
        lines.append(f"subroutine {name}({arg_names})")
    for a in args:
        lines.append(f"  {_render_entity(a)}")
    if proc["kind"] == "function":
        lines.append(f"  {_render_entity(proc['result'])}")
    lines.append(f"end {proc['kind']} {name}")
    return lines


def _render_type(t: dict[str, Any]) -> list[str]:
    lines = [f"type :: {t['name']}"]
    for c in t.get("components", []):  # validation allows an omitted/empty component list (an
        lines.append(f"  {_render_entity(c)}")  # opaque tag type); render must not KeyError.
    lines.append(f"end type {t['name']}")
    return lines


def render_signatures_to_fortran(struct: dict[str, Any]) -> str:
    """Render the structured representation back to a canonical Fortran interface block. The output's
    NORMALIZED lines are what the §5.1 gates compare; exact spacing/comments are irrelevant.
    Fail-closed (``SignatureParseError``) on any malformed struct — never an uncaught index error."""
    _validate_struct(struct)
    blocks: list[str] = []
    for mp in struct.get("module_parameters", []):
        blocks.append(render_module_parameter_to_fortran(mp))
    for t in struct.get("types", []):
        blocks.append("\n".join(_render_type(t)))
    if struct.get("interfaces"):
        blocks.append("\n".join(_render_interface_block(struct["interfaces"])))
    for proc in struct.get("procedures", []):
        blocks.append("\n".join(_render_procedure(proc)))
    return "\n\n".join(blocks) + "\n"


def _render_interface_block(interfaces: list[dict[str, Any]]) -> list[str]:
    """One ``abstract interface`` block holding every prototype. The canonical form carries no
    ``import`` / ``implicit none`` line: it exists to be compared (the stanza splitter drops both
    from a source prototype anyway), not to be compiled. The source a leaf writes needs both
    (the kind symbol is host-associated only through ``import``; the lint gate's C002 wants
    ``implicit none``), which is the Generate template's business, not this renderer's."""
    lines = ["abstract interface"]
    for iface in interfaces:
        lines.extend(f"  {ln}" for ln in _render_procedure(iface))
    lines.append("end interface")
    return lines


def render_interface_to_fortran(iface: dict[str, Any]) -> str:
    """Render ONE ``interfaces[]`` prototype to its own ``abstract interface`` block, so a single
    IR ``public_api.interfaces`` entry can be compared against §5.1 in the same stanza currency
    (``parse_interface_stanzas`` puts it in ``iface_stanzas``). Fail-closed on a malformed entry;
    the struct-level reference rules are not checked here (there is no struct to check against)."""
    _validate_procedure(iface, "interface", allow_procedure_args=False)
    return "\n".join(_render_interface_block([iface])) + "\n"


def render_symbol_to_fortran(sig: dict[str, Any]) -> str:
    """Render ONE published-symbol signature (a procedure or a derived-type struct) to its Fortran
    stanza. Used to compare a single IR ``public_api.signatures`` entry against §5.1 by rendering it
    into the same Fortran currency the existing stanza comparison uses. ``kind`` present ->
    procedure; ``components`` present -> type. Fail-closed on any malformed struct."""
    _validate_symbol(sig)
    if sig.get("kind") in ("subroutine", "function"):
        return "\n".join(_render_procedure(sig)) + "\n"
    return "\n".join(_render_type(sig)) + "\n"




# --- helpers for gate comparison (used by the deterministic gates once §5.1 is structured) --------

def normalized_stanza_index(block_body: str) -> dict[str, frozenset[str]]:
    """Map each published symbol to the frozenset of its NORMALIZED stanza lines. The canonical
    per-symbol comparison key: symbol identity + a whitespace/case/comment-insensitive line set.
    Used to compare a rendered structured block against a generated ``.f90`` (or two structured
    forms) with the exact semantics the current gates use for procedures."""
    op_stanzas, type_stanzas, iface_stanzas, errors = parse_interface_stanzas(block_body)
    if errors:
        raise SignatureParseError("; ".join(errors))
    index: dict[str, frozenset[str]] = {}
    normalize = fortran_lines.normalize_fortran_line
    for name, lines in {**op_stanzas, **type_stanzas, **iface_stanzas}.items():
        index[name] = frozenset(
            normalize(ln) for ln in lines if normalize(ln)
        )
    return index


# --- the capability contract ----------------------------------------------------------------
#
# What the neutral gates take off this module when they reach it through
# `registry.capability_module("language", "fortran", "signatures")` (issue #289, R4-b PR-3). The
# neutral names are aliases of the Fortran-spelled functions above, so a second language states the
# same contract under the same names; `tools/tests/test_backend_boundary.py`
# (`_LANGUAGE_CAPABILITY_CONTRACT`) pins the set.

#: The language's name as the gates' messages spell it.
LANGUAGE_DISPLAY_NAME = "Fortran"
render_signatures = render_signatures_to_fortran
render_symbol = render_symbol_to_fortran
render_interface = render_interface_to_fortran
render_module_parameter = render_module_parameter_to_fortran
validate_module_parameter = _validate_module_parameter


def published_interface(source_text: str, name: str) -> dict[str, Any] | None:
    """The call-site interface of the subroutine `name` a certified source defines
    (`interface.py`), or None."""
    from tools.backends.language.fortran import interface
    return interface._extract_subroutine_interface(source_text, name)


def prefixed_procedures(source_text: str, prefix: str) -> list[str]:
    """The distinct, source-ordered names of the `prefix`-named subroutines a certified source
    publishes (`interface.py`)."""
    from tools.backends.language.fortran import interface
    return interface._list_prefixed_subroutines(source_text, prefix)


def _interface_module():
    from tools.backends.language.fortran import interface
    return interface


def procedure_interface(arg: dict[str, Any]) -> str | None:
    """The prototype a procedure-typed dummy of a resolved fact references (`interface.py`)."""
    return _interface_module().procedure_interface(arg)


def dependency_operations_header(*, detailed: bool, procedure_argument: bool) -> str:
    """The paragraph heading a consumer's published-dependency-operation lines (`interface.py`)."""
    return _interface_module().dependency_operations_header(
        detailed=detailed, procedure_argument=procedure_argument)


def prototype_heading(name: str) -> str:
    """The line above a procedure argument's prototype listing (`interface.py`)."""
    return _interface_module().prototype_heading(name)


def argument_detail_lines(arguments: Any, *,
                          carried_prototypes: frozenset[str] = frozenset()) -> list[str]:
    """The per-dummy-argument lines of one published operation (`interface.py`)."""
    return _interface_module().argument_detail_lines(
        arguments, carried_prototypes=carried_prototypes)


def generated_source_violations(
    *,
    model_files: list[Path],
    target: Path,
    ir_kind: str,
    op_stanzas: dict[str, list[str]],
    type_stanzas: dict[str, list[str]],
    proto_stanzas: dict[str, list[str]],
    module_parameters: list[dict[str, Any]],
    violations: list[str],
) -> None:
    """The source half of the validator's `Generate.static` signature gate
    (`_validate_generated_signatures`): pin the generated model source against the §5.1 canonical
    interface, already rendered to Fortran stanzas (``op_stanzas`` / ``type_stanzas`` /
    ``proto_stanzas``) and already checked against the certified IR by the validator.

    Moved here unchanged from the validator (issue #289, R4-b PR-3): every rule below is a
    statement about how a Fortran source publishes, defines and binds a procedure, a derived type,
    a prototype and a module parameter. ``target`` is the path findings are reported against;
    ``module_parameters`` are §5.1's structured entries (`load_structured_signatures`), rendered
    here."""
    from tools.backends.language.fortran import source as fortran_source
    from tools.backends.language.fortran import structure as fortran_structure

    # Parse the generated source into per-symbol stanzas (a procedure stanza is header + its
    # declarations + body; a type stanza is its full block) so each pinned signature is checked
    # WITHIN its own procedure/type. A GLOBAL source line-set would let a drifted declaration in
    # one procedure be masked by an identical (correct) declaration in another — `intent(in) :: n`
    # is common — so the scoping is load-bearing, not cosmetic.
    combined = "\n".join(
        model_file.read_text(encoding="utf-8", errors="ignore") for model_file in model_files
    )
    # A prototype the source declares inside an `interface` block is neither a published
    # procedure nor a type; the splitter files it separately. It is read for two purposes: a
    # §5.1 procedure the source only prototypes is reported as undefined (the per-symbol loop),
    # and each §5.1 `interfaces` prototype is pinned against the source's prototype of that
    # name (after the loop; issue #266). A prototype that shares a published name with an
    # unprefixed definition is still a `duplicate signature` error from the splitter; the
    # prefixed variant that error never covered is closed in the per-symbol loop, which compares
    # a defined procedure by its own definition's header.
    src_ops, src_types, src_ifaces, src_errors = (
        parse_interface_stanzas(combined))
    # HONOUR the parser's errors. `parse_interface_stanzas`' own docstring says a duplicate symbol
    # name is reported here and must be "fail-closed at the caller — a duplicate must never silently
    # overwrite", and this caller discarded them while the §5.1 side and the IR side both honour
    # theirs. The consequence, measured on a real component §5.1 (issue #153 PR-2 round 1): a model
    # source publishing a DRIFTED 2-argument operation, plus a never-called private helper carrying a
    # second declaration of the SAME published name with the pinned 5-argument shape, produced ZERO
    # violations — the stanza dict is last-wins, so the gate compared §5.1 against the decoy. Moving
    # the decoy earlier in the file restored all 4 violations, which is what identified the
    # mechanism. That is a `leaf shortcut` of the exact class this gate exists to refuse: a
    # `Generate.gate` pass while publishing an ABI §5.1 does not declare.
    if src_errors:
        for err in src_errors:
            violations.append(
                f"{target}: generated model source cannot be compared with controlled_spec §5.1 "
                f"({err}) — a published symbol must be declared exactly once in the model source, "
                "so remove the extra declaration (an `interface` body re-declaring a symbol this "
                "module defines is one) and re-emit")
        return
    src_lists: dict[str, tuple[str, ...]] = {}
    src_proto_lists: dict[str, tuple[str, ...]] = {}  # the prototypes, kept apart (see the loop)
    spec_proto_lists: dict[str, tuple[str, ...]] = {}  # §5.1's prototypes, same currency
    for atom_lists, stanzas in ((src_lists, {**src_ops, **src_types}),
                                (src_proto_lists, src_ifaces),
                                (spec_proto_lists, proto_stanzas)):
        for name, lines in stanzas.items():
            atom_lists[name] = stanza_line_list(lines)

    # PUBLISHING A HEADER IS NOT IMPLEMENTING IT, and everything above this line only compares
    # HEADERS. `parse_interface_stanzas` reads a header wherever it stands, so a model that
    # declares a §5.1 operation as a PROTOTYPE — the pinned header, verbatim, with no
    # implementation anywhere — satisfies every comparison below. Measured (issue #153's CARRIED
    # residue, re-run here before it was fixed): ZERO violations, the syntax stage rc=0, the
    # node's own build rc=0, and the node links as long as nothing calls the operation. The
    # failure lands at a CONSUMER's link, one node and one billed phase away from the leaf that
    # caused it. It is a `leaf shortcut` by the decision criterion: a leaf that takes it is closer
    # to reporting `Generate` done, having skipped the implementation.
    #
    # WHY THE CARRIED PREMISE DOES NOT APPLY HERE, which is the whole reason this could be fixed
    # at last. `checks_module_abi_facts`' docstring names three shapes a bare "is it defined"
    # refusal would fail — a name association, a generic block, an implementation in a separate
    # unit — and each was run against THIS gate before the check below was added. All three are
    # refused TODAY, before it, and for an unrelated reason: none of them puts the pinned §5.1
    # header in the source, so each already fails the `have is None` arm above. So the
    # over-refusal belongs to the checks-module scanner, whose question ("is this name callable")
    # those shapes answer yes to; this gate's question is narrower and they never reach it.
    # `PublishedProcedureDefinednessTests` pins that as a row, so a later widening of the header
    # comparison cannot quietly inherit the over-refusal.
    #
    # A `component` / `infrastructure` source had no structural reader, by deliberate removal: a
    # parse refusal "bought nothing and cost a legal form" where no gate read the file (measured,
    # 0 -> 4 violations). That justification is now spent — this gate reads it, so the refusal
    # buys the definedness check — and a parse the front end cannot resolve is refused rather than
    # skipped. Skipping would put the check behind a switch the LEAF holds: one renamed variable
    # would disable it, which is the silent-gate shape this area exists to remove.
    #
    # THE QUESTION IS SCOPED TO THE PUBLISHING UNIT, and the first version of this check was not.
    # Asked over the whole file it becomes "is this name defined ANYWHERE", which a decoy answers:
    # a round-1 reviewer measured a model whose published operation was a prototype in the
    # published module and an empty stub in a SECOND module in the same file — gate 0 violations,
    # the syntax stage clean, the node's own build clean, and the consumer still failing at LINK
    # with an undefined reference. That is this check's own hole, one unit away, reproduced
    # independently before it was fixed. The duplicate-symbol backstop that should have caught two headers of one name did
    # not, because the decoy's header carried a prefix (`impure elemental`) or a statement label
    # that the stanza reader does not model — so narrowing to that spelling would have closed one
    # decoy and left the family. Scoping removes the family FOR THE DEFINEDNESS ANSWER, and only
    # there: a decoy in the published unit itself — contained in another procedure, or a prototype
    # in another procedure's body — still carried the header the comparison below read, until
    # that comparison took its header from the same definition (`source.module_level_definition_headers`).
    #
    # The unit is named by the source's own basename with its extension dropped, which is the
    # convention this repository already relies on when it resolves the model source at all.
    # (An earlier sentence here described the per-file UNION this replaced — "every model file
    # contributes only the names its own unit defines" — and contradicted the rule two lines
    # above it once the union was gone. Deleted rather than corrected: one statement of a rule.)
    #
    # ONE FILE, NOT A UNION. An earlier version unioned the per-file answers, and a round-2 census
    # showed what that buys: a second model file whose OWN module carries a same-named stub credits
    # the prototype in the published file, which is the unit-granularity hole reopened at file
    # granularity. Its ROUTE was NOT established — `_model_files_in_src_dir` returns more than one
    # file only when `_spec_id_from_node_key` finds no `/`, and `node_key` is read from the
    # host-authored `lineage.json`, which no leaf write root covers — so this is defense in depth
    # rather than a closed exploit, and it is recorded that way. The published surface belongs to
    # ONE module either way, so a set of files this gate cannot resolve to one publisher is
    # fail-closed rather than unioned.
    defined_names: frozenset[str] | None = None
    definition_lists: dict[str, tuple[str, ...] | None] = {}
    unit_absent: str | None = None
    if op_stanzas and len(model_files) != 1:
        # APPENDED DIRECTLY, and NOT via `_fail_closed_if_pinned`, and NOT followed by a
        # `return`. Both were wrong, and a round-3 disclosure reviewer measured the cost:
        # `_fail_closed_if_pinned` appends only when the NODE_KEY's prefix is a pinned kind,
        # while `len(model_files) != 1` can only happen when the node_key has no `/` at all —
        # so in the one configuration this arm can fire, it appended NOTHING and returned,
        # dropping every §5.1 header comparison with it. Against `origin/main`, which reports
        # the drift, that is red-then-GREEN: a check this branch silently removed. Past this
        # point `ir_kind` has already been confirmed to publish an exact surface, so the
        # refusal needs no further condition; and the header comparison does not need the
        # definedness answer, so it continues below with `defined_names` left None.
        violations.append(
            f"{target}: this {ir_kind} node's published surface cannot be pinned to one "
            f"publisher — {len(model_files)} model source files were resolved and exactly one "
            "is expected, so which program unit publishes the controlled_spec §5.1 surface "
            "cannot be decided; the definedness check is skipped for this node and the "
            "signature comparison below still applies")
    if op_stanzas and len(model_files) == 1:
        model_file = model_files[0]
        try:
            source_text = model_file.read_text(encoding="utf-8", errors="ignore").lower()
            if not fortran_structure.publishing_unit_present(
                    fortran_source.structure_reading(source_text)[1], model_file.stem):
                unit_absent = model_file.stem
            defined_names = fortran_source.module_level_procedure_names(source_text, model_file.stem)
            definition_lists = fortran_source.module_level_definition_headers(source_text, model_file.stem)
        # `FortranStructureUnavailableError` is deliberately NOT caught: it is the OPERATOR's
        # failure (an uninstalled package), no edit to this source can clear it, and `main`
        # answers it with a dedicated exit code. Same rule as `source.run_problem_model_gates`.
        except fortran_source.SourceStructureError as exc:
            for structure_error in exc.errors:
                violations.append(
                    f"{target}: the structure front end could not resolve this source at "
                    f"statement {structure_error.line} of its joined view "
                    f"({'missing token' if structure_error.missing else 'parse error'}): "
                    f"{structure_error.snippet!r}. A {ir_kind} node's published operations must be "
                    "shown to be DEFINED and not merely declared, which needs the procedure "
                    f"structure, so an unresolvable source is a Generate failure — "
                    f"{fortran_structure.STRUCTURE_REFUSAL_HINT}.")
            # NO `return`. The header comparison below does not need the parse, and discarding it
            # would hand a leaf whose source has BOTH an unresolvable identifier and a real
            # signature drift only the first of the two — two warm retries where one would do.
            # `defined_names` stays None, so the definedness arm alone is skipped; the violation
            # above already fails the node, so skipping it opens nothing.

    for name in sorted({**op_stanzas, **type_stanzas}):
        spec_lines = op_stanzas.get(name) or type_stanzas.get(name) or []
        is_type = name in type_stanzas
        kind = "derived type" if is_type else "procedure"
        have = src_lists.get(name)
        # A procedure the publishing module DEFINES is compared by ITS header, not by whichever
        # stanza of that name the splitter met (`structure.module_level_definition_stanzas` says
        # why). A definition whose header the splitter cannot read leaves `have` None, and the
        # source is told so.
        if not is_type and name.lower() in definition_lists:
            have = definition_lists[name.lower()]
        # A published procedure the source declares only as a PROTOTYPE inside an `interface`
        # block. The stanza splitter files it under the prototypes, so it is not in `src_lists`;
        # it is still the header the leaf wrote for this name, so it is compared for drift below
        # and reported as undefined by the structural arm (an interface-body header is not a
        # module-level procedure to the structure reader; a round-1 reviewer measured that a
        # clause repeating that verdict here was unobservable). Setting `have` is the whole of
        # it. What the prototype-plus-definition PAIR gets, stated by shape because a round-2
        # reviewer found the previous sentence here claiming a defence that does not exist: an
        # UNPREFIXED pair is the splitter's `duplicate signature` above, whichever comes first;
        # a pair in the module's specification part is refused by the compiler ("already
        # defined") at `Generate.syntax` whatever the prefix; a prototype inside another
        # procedure's BODY plus a module-level definition carrying a prefix the splitter does
        # not model (`impure elemental`) is refused by NEITHER — measured 0 violations here and
        # rc=0 from the syntax check, at origin/main and at this revision — because this arm
        # compared the prototype's atoms while the structural arm credited the prefixed
        # definition. That pair, and its contained-decoy spelling, is closed by the arm above: a
        # name the module DEFINES never falls back to a prototype's header.
        if (not is_type and have is None and name in src_proto_lists
                and name.lower() not in definition_lists):
            have = src_proto_lists[name]
        if have is None and not is_type and name.lower() in definition_lists:
            violations.append(
                f"{target}: generated model source does not publish controlled_spec §5.1 {kind} "
                f"'{name}' in the pinned form — the module defines '{name}', but "
                f"{fortran_structure.UNREAD_DEFINITION_HEADER_REMEDY}")
            continue
        if have is None:
            violations.append(
                f"{target}: generated model source does not publish controlled_spec §5.1 {kind} "
                f"'{name}' (no {kind} of that name/header found — the published surface must match "
                "the pinned §5.1 signature)")
            continue
        if not is_type and defined_names is not None and name.lower() not in defined_names:
            # NOT `continue`. A first version reported this INSTEAD OF the stanza comparison, on
            # the reasoning that "the header is present by construction, so every atom matches" —
            # which is false, and a round-2 reviewer measured it: `have` is keyed on the NAME, not
            # on the header matching §5.1, so a prototype that ALSO drifts has a non-None `have`
            # and its drift atoms were suppressed. A source with both faults was told one per
            # attempt, which is the second warm retry the sibling comment above refuses to pay.
            #
            # `unit_absent` is the other half, and it is why the two messages are not one. Scoping
            # answers the empty set both when the published unit implements nothing and when the
            # source declares no such unit at all, and the second is a DIFFERENT fault with a
            # different repair: telling a leaf to "define it in the module's own `contains`" when
            # the procedure IS defined — in a module under another name — is a remedy whose every
            # clause is false for the source, and nothing else in this stage names the real fault.
            # Measured on a correct model whose module name did not match its file's: three such
            # violations, none of them actionable.
            if unit_absent is not None:
                violations.append(
                    f"{target}: generated model source declares no program unit named "
                    f"'{unit_absent}', so the surface controlled_spec §5.1 pins has no publisher "
                    f"here — procedure '{name}' may well be defined, but not by the module a "
                    f"consumer will `use`. Name the module '{unit_absent}', matching the source "
                    "file, and define the published procedures inside it")
            else:
                violations.append(
                    f"{target}: generated model source declares controlled_spec §5.1 procedure "
                    f"'{name}' but never DEFINES it — "
                    f"{fortran_structure.UNDEFINED_PUBLISHED_PROCEDURE_REMEDY} of '{name}'")
        if is_type:
            # A derived type's WHOLE component layout — names, types, and ORDER, with nothing
            # inserted — is part of the compatibility contract (§5), so the source type block must
            # equal §5.1's atom list EXACTLY. Ordered-subsequence would accept an inserted extra
            # component (widening the published layout); set equality would accept a reorder.
            if have != stanza_line_list(spec_lines):
                violations.append(
                    f"{target}: derived type '{name}' drifts from controlled_spec §5.1 — its "
                    "published component layout (names/types/order, no extras) does not match the "
                    "pinned definition")
            continue
        # A procedure's dummy-argument declarations may be in any order (Fortran-legal, and the
        # header line already pins call order), so membership — not order — is checked here.
        have_set = frozenset(have)
        for orig in spec_lines:
            missing_atoms = [a for a in stanza_atoms([orig]) if a not in have_set]
            if missing_atoms:
                violations.append(
                    f"{target}: procedure '{name}' drifts from controlled_spec §5.1 — missing the "
                    f"pinned interface line `{orig.strip()}` (argument name/type/rank/intent/"
                    "result drift from the published surface, OR a procedure prefix on the header "
                    "that §5.1 does not declare — the header is compared as published, so a prefix "
                    "is a difference even when every argument is right)")

    # The §5.1 PROTOTYPES (issue #266): each `interfaces` entry must appear in the source as a
    # prototype of the same name — inside an `interface` block, never as a definition — with
    # the same atom SET (a prototype has no body, so the comparison is equality both ways,
    # unlike a published procedure's membership check: an extra declaration line in a
    # prototype is a different interface the compiler checks the passed actual against). A
    # prototype the source declares that §5.1 does not is allowed — a module-private
    # interface is not published surface. The "never as a definition" half asks the structure
    # reader, like the definedness arm above, and is skipped on the same conditions (no single
    # publisher, or a source the front end could not resolve, both already refused above).
    # The two scope statements a source prototype must carry (host association of the kind
    # symbol, and the implicit-typing rule the lint gate requires) are not atoms — the
    # splitter drops them — so they cannot drift the comparison either way.
    proto_remedy = (
        "declare it in an abstract interface block of the model module, as a prototype only "
        "(no body), matching the §5.1 `interfaces` entry argument for argument — every "
        "argument name, type, kind, rank, intent and the result — carrying inside the "
        "prototype the two scope statements authoring rule (6a) of the generate template "
        "requires")
    for name in sorted(spec_proto_lists):
        want = frozenset(spec_proto_lists[name])
        have_proto = src_proto_lists.get(name)
        if have_proto is None:
            violations.append(
                f"{target}: generated model source does not declare the controlled_spec §5.1 "
                f"prototype '{name}' (no interface block carries a prototype of that name) — "
                f"{proto_remedy}")
        elif frozenset(have_proto) != want:
            missing = sorted(a for a in want if a not in have_proto)
            extra = sorted(a for a in have_proto if a not in want)
            violations.append(
                f"{target}: prototype '{name}' drifts from controlled_spec §5.1's `interfaces` "
                f"entry — missing {missing}, extra {extra} (compared as normalized "
                f"declaration atoms; the header line is one of them) — {proto_remedy}")
        if defined_names is not None and name.lower() in defined_names:
            violations.append(
                f"{target}: generated model source DEFINES '{name}', which controlled_spec §5.1 "
                "declares as a prototype (an `interfaces` entry) — a prototype is the shape of "
                "the procedure a CALLER passes, and the model must not implement it; remove the "
                f"definition and {proto_remedy}")

    # The §5.1 module-level `parameter` declarations (dp / case_id_len) are part of the published
    # ABI but are not stanzas; pin their exact declaration (name AND value) against the source —
    # a `case_id_len = 32` drift would otherwise be invisible (the symbolic decls still match). Use
    # per-entity atoms so a combined `integer, parameter :: dp = real64, case_id_len = 64` matches.
    all_src_atoms = source_atoms(combined)
    # Defense-in-depth: `_parse_canonical_interface_from_controlled_spec` above already renders the
    # whole §5.1 struct and short-circuits (iface_err → return) on any parameter the backend cannot
    # lower, so a raise here is not reachable in the current gate order. Guard it anyway — this is
    # the lone backend render not already inside an `except SignatureParseError`, so a future reorder
    # or a new caller must fail closed with a clear violation, never crash the gate.
    try:
        param_lines = [render_module_parameter_to_fortran(mp) for mp in module_parameters]
    except SignatureParseError as exc:
        violations.append(
            f"{target}: controlled_spec §5.1 declares a module parameter the language backend "
            f"cannot lower ({exc}) — re-certify the harness so §5.1 carries a neutral parameter "
            "value the generated source can be pinned against")
        param_lines = []
    # The declaration must be PRESENT (below) and it must be the only binding of that name in the
    # model source (here). Presence alone is scope-blind: `source_atoms` is a whole-file atom set, so
    # measured on a real component §5.1 (issue #153 PR-2 round 1) a module-level ALIASING import
    # binding the pinned kind name to a NARROWER kind — making every published argument of that kind
    # single precision — plus a dead private helper carrying the pinned declaration, produced ZERO
    # violations. That is precisely the narrowing the §5.1 prose of all six component specs claims
    # this pin closes, so the claim was false as enforced.
    #
    # Refusing an IMPORT of the pinned name is what closes it for the `only:` forms: an ALIASING
    # import and a plain one both bring a binding this gate cannot see the value of, and a
    # legitimate source has no reason for either — it declares the parameter itself, which is the
    # rule. An earlier version of this comment said it "closes the family rather than the witness",
    # which overstated it in the direction that matters: an UNRESTRICTED `use` supplies the name
    # with nothing here to match on, and no declaration reader can see it either. That construct is
    # refused by the lint rule `C121` (`use-all`) in this same `Generate.gate` substep — executed,
    # not assumed — so it is covered, by another layer and not by this one. This half alone is NOT complete, and an earlier version of this comment
    # claimed it was: it enumerated "imported, or declared only in a dead procedure" and omitted the
    # case where the module declares the name ITSELF with another value — which resolves, compiles,
    # and publishes the wrong precision. The uniqueness check below is what closes that; this one
    # remains because an import is the member uniqueness cannot see (an imported name is bound
    # without a `parameter` declaration to compare).
    param_names = [
        str(mp.get("name") or "").strip()
        for mp in module_parameters if isinstance(mp, dict)
    ]
    for name in [n for n in param_names if n]:
        # Read the import statements out of the atom set the gate ALREADY built (`source_atoms`
        # normalizes each entity and strips whitespace), rather than re-scanning the source through
        # the line module — one fewer neutral-core mention of a backend module name, and one fewer
        # place that has to agree about what a logical line is.
        for atom in sorted(all_src_atoms):
            # A plain import and an intrinsic-module import are both imports, and the second has NO
            # space after the keyword — matching the keyword plus a space alone missed every
            # intrinsic-module import, which is the only form the corpus uses. Atoms carry no
            # whitespace, so match the keyword then any non-name character.
            if not re.match(r"use[,:]|use\w", atom):
                continue
            if "only:" not in atom:
                continue
            imported = atom.split("only:", 1)[1]
            # `a=>b` binds `a`; a bare `b` binds `b`. Either way the pinned name must not appear on
            # the BINDING side of an import.
            bound = [seg.split("=>")[0].strip() for seg in imported.split(",")]
            if name.lower() in bound:
                violations.append(
                    f"{target}: generated model source imports the §5.1 module parameter "
                    f"`{name}` (`{atom}`) instead of declaring it — the published ABI's kind must "
                    f"come from this module's own `parameter` declaration of `{name}`, which is "
                    "what this gate value-pins; an imported binding carries a value the pin "
                    "cannot see")
                break

    # Pair by INDEX over the unfiltered list, not by zipping against a FILTERED name list.
    # `param_lines` maps one-to-one over the same `module_parameters`
    # entries these names come from, so index i is the same parameter on both sides — but only while
    # nothing is dropped from one side. `[n for n in param_names if n]` dropped exactly the
    # empty-named entries, which would have shifted every later pair and checked one parameter's
    # uniqueness against another's pinned line. That is unreachable today for a reason external to
    # this loop (an empty name makes the language backend's parameter renderer raise, so
    # `param_lines` is `[]` and a violation is already recorded) — so the `if not name` skip below is
    # defensive and NOT pinned: deleting it keeps the suite green, which is recorded here rather than
    # taken as grounds to remove it. Relaxing that renderer would turn
    # a coincidence into a silent mispairing. Pairing by index cannot go wrong that way.
    for pline, name in zip(param_lines, param_names):
        if not name:
            continue
        pinned_atoms = stanza_atoms([pline])
        missing_atoms = [a for a in pinned_atoms if a not in all_src_atoms]
        if missing_atoms:
            violations.append(
                f"{target}: generated model source is missing the §5.1 module parameter "
                f"declaration `{pline.strip()}` (a drifted parameter value silently changes the "
                "published ABI)")
            continue
        # UNIQUENESS, not presence. Presence alone asks "does the pinned text occur anywhere in the
        # file", and a declaration is not where it occurs but where the published signatures BIND.
        # Round 1 closed one way to satisfy presence with a non-published binding (an import) and
        # left the family open; round 2 found the next member — a module-level declaration of the
        # SAME name with a NARROWER value, plus the pinned text inside a contained procedure, where
        # a local `parameter` legally shadows the host one. Measured: 0 violations, the source
        # compiles, the linter passes, and every published argument is single precision while §5.1
        # pins double. The stanza comparison pins types only SYMBOLICALLY (`real(dp)`), so all
        # precision information routes through this one check.
        #
        # So the rule is: the pinned name must have EXACTLY ONE binding in the source, and it must
        # be the pinned one.
        #
        # HOW that question is asked is the part round 2 got wrong, and round 3 measured. Round 2
        # answered it with a regex over the atom set, matching an attribute prefix with a character
        # class before the pinned keyword. That is §1's source-text surface asked with a grammar
        # written here, and it lost the way that always loses: the class admitted letters and
        # parens but neither a comma nor an equals sign, so a declaration carrying a SECOND
        # attribute — before or after the pinned keyword — and one whose type specification carries
        # a keyword argument were both invisible, while the same declaration with a bare named type
        # parameter was caught. The boundary tracked the character class, not the language. All
        # three spellings are ordinary style, all compile under the standard the syntax gate
        # enforces, and each publishes the narrower precision against a §5.1 that pins the wider.
        # So the third patch was still a patch.
        #
        # The tree already owns the reader for this question — the declared-names helper this
        # module defines above, which splits the attribute list on top-level commas and tests for
        # the keyword EXACTLY, handles the two-statement declaration form, and reports a restricted
        # import as a binding too, each of those behaviours having its own recorded fail-open.
        # Asking it, per STATEMENT, "does this bind the pinned name" is the same question with no
        # second grammar to keep correct. Statement granularity is what makes it a COUNT rather
        # than a set: that helper's docstring says the caller decides scope, and the scope this
        # rule needs is every binding anywhere in the file, because one local to a published
        # procedure changes THAT procedure's dummy declarations, and so its ABI.
        binding_statements = [
            stmt.strip()
            for stmt in fortran_source.joined_masked_view(combined.lower()).split("\n")
            if stmt.strip() and name.lower() in set().union(*fortran_source.declared_names(stmt))
        ]
        # `split("\n")`, NEVER `str.splitlines()`. That is the language backend's own rule — its
        # line module states it and three other gates already pin it — and this site was the one
        # place on the branch that did not follow it. `splitlines()` breaks on eight separators the
        # target language treats as ordinary content, so a narrowing declaration written with one
        # inside it was torn into fragments that bind nothing and vanished from the count, while
        # PRESENCE (which reads the atom set, built with the correct split) still saw the pinned
        # text in a contained procedure. Zero violations, compiles under the standard the syntax
        # gate enforces, every published argument at the narrower precision: round 2's hole,
        # reopened by round 3's implementation of the fix for it and carried through round 4.
        #
        # A COUNT, and deliberately nothing more. Round 3's first version also subtracted the pinned
        # line as raw text — `set(binding_statements) - {pline.strip().lower()}` — which compares
        # SPELLING in the one check of this gate that had no business doing so: every other
        # comparison here goes through the atom normalizer, and both the §5.1 prose and this
        # function's docstring promise that formatting may differ. Round 4 measured the cost. A
        # source carrying exactly ONE declaration, differing from the rendered form only by
        # interior spacing — a space dropped after the attribute separator, or around the assignment
        # — was told it "binds the name more than once", and the message then printed two strings a
        # reader cannot tell apart. That refusal has NO repair: the leaf can see one declaration, is
        # told there are two, and `Generate.gate` warm-resumes into the same wall. A single
        # declaration binding TWO parameters was refused for both names too — the shape the presence
        # check fifteen lines above explicitly endorses, so the function contradicted itself.
        #
        # The subtraction was never load-bearing. Presence is settled ABOVE: `missing_atoms`
        # `continue`s when the pinned atom is absent, so reaching here means the pinned declaration
        # IS in the source. Given that, "exactly one statement binds this name" already says the
        # single binding is the pinned one, and it says it without asking how anything is spelled.
        if len(binding_statements) > 1:
            listed = ", ".join(f"`{s}`" for s in sorted(binding_statements))
            violations.append(
                f"{target}: generated model source binds the §5.1 module parameter `{name}` "
                f"{len(binding_statements)} times — {listed} — and `{pline.strip()}` is what §5.1 "
                "pins. A published argument's type is pinned only SYMBOLICALLY, so which binding is "
                "in effect is what decides the published precision; keep the pinned declaration and "
                "delete the others, including one local to a contained procedure")
