"""The structural front end the three `problem` model gates read Fortran through.

Replaces a hand-rolled regex walk over Fortran's keyword structure. That walk was rewritten
four times and broken sixteen; every break was the same shape — a spelling the language allows
and the rules did not enumerate (`endsubroutine` as one word, a bare `end`, a construct named
after a keyword, a variable named after a keyword, an `interface` body's own `end subroutine`).
The set of such spellings is not closed by enumeration, which is why this asks a parser instead.

WHAT THIS PARSES IS A VIEW, NOT SOURCE. Callers hand over
`validate_pipeline_semantics._joined_masked_fortran_view` output: lower-cased, `&` continuations
joined, one statement per line, comment and literal CONTENT blanked length-preservingly. Two
properties of that view are load-bearing here:

* it is a FIXED POINT, so a caller that has already reduced its text pays one idempotent pass;
* every offset this module returns is an offset INTO THE VIEW, so a body is one contiguous slice
  of it. `_validate_problem_model_dependency_dataflow` compares an assignment position with a
  call position taken inside that slice, and those two are comparable only within one view.

FAIL-CLOSED, IN TWO DIRECTIONS:

* **the packages are absent** → `FortranStructureUnavailableError`, which the caller reports with
  `FORTRAN_STRUCTURE_UNAVAILABLE_MARKER` so the conductor terminalizes the run
  (`static_frontend_unavailable`) instead of spending a leaf's retry budget on a machine problem
  the leaf cannot fix;
* **the parse carries an ERROR or MISSING node** → `StructureTree.errors` is non-empty and the
  caller raises a CONTENT violation. It does not fall back to a looser reading: a structure the
  parser could not resolve is exactly the input a silent gate is made of. Measured over the
  corpora this repository has (2026-08-13, tree-sitter 0.26.0 / tree-sitter-fortran 0.6.0): of
  the 365 in-tree `*_model.f90`, 0 carry an ERROR node over their view and the procedure set
  agrees with the walk on 365/365.

  WHAT IT REFUSES IS A CLASS, AND THE CLASS IS NOT ENUMERATED HERE — that is the whole point of
  the swap, and two earlier versions of this note got it wrong in the same way, by naming the
  members. The class is: **a legal program in which an identifier is spelled like a keyword the
  parser needs in order to find structure**, so the parser lexes the identifier as the END
  statement or block opener it spells. `endsubroutine`, `interface`, `contains`, `procedure`,
  `associate`, `forall`, `enum`, `import`, `common`, `abstract`, `equivalence` and `type(3)` are
  all members that have been observed; that list is a SAMPLE and review has extended it twice
  (once by 17 rows), which is exactly what an enumeration does. Every member is repairable by a
  rename, which the violation message asks for.

  What IS pinned, as sets, are the two matrices in `test_validate_pipeline_semantics.py` — 48 rows
  naming a variable after a keyword (10 refused) and 35 naming a CONSTRUCT after one (15 refused).
  They bound the refusals over the names they sweep and nothing beyond; treat them as a regression
  guard, not as the definition of the class.

DEPENDENCIES. Two packages this repository does not otherwise declare:

    pip install tree-sitter tree-sitter-fortran

pinned by measurement at **tree-sitter 0.26.0** and **tree-sitter-fortran 0.6.0**. A version bump
re-runs `tools/backends/language/fortran/structure_differential.py` (both halves) before it is accepted — the tree
half proves the corpus still parses the same, the flang half proves it parses it RIGHT. Declaring
these two in a dependency manifest is owned by the MCP-tests-and-CI item of `TODO.md`, which owns
the fact that this repository declares no dependencies at all.
"""

from __future__ import annotations

from dataclasses import dataclass

FORTRAN_STRUCTURE_UNAVAILABLE_MARKER = "[fortran-structure-unavailable]"

#: The node types tree-sitter-fortran gives a procedure DEFINITION, mapped to this module's kind.
#: `module_procedure` is the abbreviated separate module subprogram (`module procedure solve`);
#: the full forms (`module subroutine s(u, v)` / `module function f(x) result(y)`) parse as an
#: ordinary `subroutine` / `function` carrying a `procedure_qualifier`, which is why they need no
#: entry of their own.
_PROCEDURE_KINDS = {
    "subroutine": "subroutine",
    "function": "function",
    "module_procedure": "module_procedure",
}


#: Node types this module matches on beyond `_PROCEDURE_KINDS`. Listed so `_load_parser` can ask
#: the grammar whether it still defines them — see the check there for why a rename is otherwise
#: silent rather than loud.
_REQUIRED_NODE_TYPES = ("interface", "internal_procedures", "contains_statement")


class FortranStructureUnavailableError(RuntimeError):
    """The front end itself could not be loaded — a machine problem, not a source problem.

    Raised only for an absent/broken `tree_sitter` / `tree_sitter_fortran`. A source this module
    CAN load but cannot parse is not this error; it is `StructureTree.errors`, because the two
    route to different places (transport `fail_closed` vs a content violation the leaf repairs).
    """


@dataclass(frozen=True)
class StructureError:
    """One ERROR or MISSING node, located in the VIEW the caller passed."""

    line: int  # 1-based line number IN THE VIEW
    snippet: str
    missing: bool


@dataclass(frozen=True)
class Procedure:
    """One procedure DEFINITION, with its body located as offsets into the view.

    ``body_start`` is the start of the line after the header statement and ``body_end`` the start
    of the line holding the END statement, which is what makes ``view[body_start:body_end]`` the
    body and nothing else. ``contains_at`` is the start of this procedure's own `contains` line
    (None when it has none): declarations before it are this procedure's, procedures after it are
    its own contained ones whose dummies are NOT its.
    """

    kind: str
    name: str
    dummy_args_text: str
    result_name: str | None
    body_start: int
    body_end: int
    contains_at: int | None


@dataclass(frozen=True)
class StructureTree:
    view: str
    procedures: tuple[Procedure, ...]
    interface_spans: tuple[tuple[int, int], ...]
    errors: tuple[StructureError, ...]


def _load_parser():
    """Import the two packages and build a parser, mapping every import failure to one error.

    Lazy on purpose: `validate_pipeline_semantics` runs stages that never reach a Fortran gate
    (`compile`, `post_build`), and an absent package must not fail those.

    That parenthesis used to name `pre_judge` instead of `post_build`, and it was wrong: an AST
    call closure over the validator puts `--stage pre_judge` and `--stage post_execute` on
    `_validate_impl` -> `_validate_generate_outputs` -> `parse_view`, so both DO reach this
    import and can raise. The two stages that reach neither this module nor the stale-IR gate are
    `compile` and `post_build` (measured the same way, and the conductor's two non-classifying
    gate readers rest on the same fact).
    """
    try:
        import tree_sitter_fortran
        from tree_sitter import Language, Parser
    except Exception as exc:  # ImportError, and the ABI errors a mismatched wheel pair raises
        raise FortranStructureUnavailableError(
            f"{FORTRAN_STRUCTURE_UNAVAILABLE_MARKER} the Fortran structure front end is not "
            f"available on this machine ({exc}). Install it with: "
            f"pip install tree-sitter tree-sitter-fortran"
        ) from exc
    try:
        language = Language(tree_sitter_fortran.language())
        parser = Parser(language)
    except Exception as exc:
        raise FortranStructureUnavailableError(
            f"{FORTRAN_STRUCTURE_UNAVAILABLE_MARKER} the Fortran structure front end failed to "
            f"initialise ({exc}). Check that tree-sitter and tree-sitter-fortran are ABI "
            f"compatible: pip install -U tree-sitter tree-sitter-fortran"
        ) from exc
    # THE GRAMMAR MUST STILL SPEAK THE NODE NAMES THIS MODULE MATCHES ON. Everything below keys
    # on node TYPE STRINGS, so a grammar that renames one reports no procedures and no errors —
    # and every gate then returns at its empty-envelope loop with nothing to say. Silent, which
    # is the one outcome this module exists to prevent, and invisible to a version pin in a
    # docstring that nothing enforces (this repository declares no dependency manifest, so a
    # fresh install resolves whatever is newest). Asking the grammar directly costs one call and
    # converts that whole class into the unavailable error.
    #
    # Reachability is currently zero and was measured, not assumed: tree-sitter-fortran 0.2.0,
    # 0.3.0, 0.4.0, 0.5.1 and 0.6.0 all use these names and all produce byte-identical violations
    # over the 365-model corpus (executed in review). This guards the next version, not any
    # published one.
    unknown = sorted(
        node_type for node_type in (*_PROCEDURE_KINDS, *_REQUIRED_NODE_TYPES)
        if language.id_for_node_kind(node_type, True) is None
    )
    if unknown:
        raise FortranStructureUnavailableError(
            f"{FORTRAN_STRUCTURE_UNAVAILABLE_MARKER} the installed tree-sitter-fortran grammar "
            f"does not define the node types this front end reads ({', '.join(unknown)}), so it "
            f"cannot report procedures at all. This module is written against "
            f"tree-sitter-fortran 0.6.0; pin that version, and re-run "
            f"tools/backends/language/fortran/structure_differential.py (both halves) before accepting a newer one."
        )
    return parser


def _line_start(view: str, position: int) -> int:
    newline = view.rfind("\n", 0, position)
    return 0 if newline < 0 else newline + 1


def _next_line_start(view: str, position: int) -> int:
    newline = view.find("\n", position)
    return len(view) if newline < 0 else newline + 1


def _byte_to_char_mapper(view: str, encoded: bytes):
    """Map a tree-sitter BYTE offset to an offset into ``view``.

    Every offset this module returns indexes the caller's `str`, because that is what the gates
    slice. The two coincide for ASCII — which the view is, wherever it is code — so the identity
    is used when the lengths match, and a table is built only when they do not (a non-ASCII
    character surviving in the view, e.g. inside a character literal whose delimiters the mask
    keeps).
    """
    if len(encoded) == len(view):
        return lambda byte_offset: byte_offset
    table: dict[int, int] = {}
    byte_offset = 0
    for char_offset, character in enumerate(view):
        table[byte_offset] = char_offset
        byte_offset += len(character.encode("utf-8"))
    table[byte_offset] = len(view)
    return lambda offset: table.get(offset, len(view))


def _text(encoded: bytes, node) -> str:
    return encoded[node.start_byte : node.end_byte].decode("utf-8", "replace")


def parse_view(view: str) -> StructureTree:
    """Parse ``view`` (a `_joined_masked_fortran_view`) into procedures + interface spans.

    Procedures are returned in body order. A procedure DECLARED inside an `interface` block is not
    a definition and is not returned — its span is returned separately so the caller can blank it
    IN PLACE, keeping every body a slice of one length-preserving transform of the view.
    """
    parser = _load_parser()
    encoded = view.encode("utf-8")
    tree = parser.parse(encoded)
    to_char = _byte_to_char_mapper(view, encoded)

    procedures: list[Procedure] = []
    interface_spans: list[tuple[int, int]] = []
    errors: list[StructureError] = []

    def record_error(node) -> None:
        line = view.count("\n", 0, to_char(node.start_byte)) + 1
        snippet = _text(encoded, node).strip().splitlines()
        errors.append(
            StructureError(
                line=line,
                snippet=(snippet[0][:200] if snippet else ""),
                missing=bool(node.is_missing),
            )
        )

    # AN EXPLICIT STACK, not recursion. A recursive walk costs one Python frame per tree node, so
    # a source with deeply nested constructs raised `RecursionError` — at ~1000 nested `if` blocks
    # here, and the exact depth moves with whatever stack the caller already spent, which is why a
    # depth limit would be the wrong shape of fix. `RecursionError` is a `RuntimeError`, so
    # `validate_pipeline_semantics.main`'s handler caught it, printed `schema_load_failed`, and
    # DISCARDED every other violation of that invocation — a legal source (gfortran accepts it)
    # taking down the whole gate run and reporting a cause that has nothing to do with it. The
    # regex walk this module replaced had no recursion, so this was a surface the swap introduced.
    # Found by review.
    def walk(root) -> None:
        stack: list[tuple[object, bool]] = [(root, False)]
        while stack:
            node, inside_interface = stack.pop()
            visit(node, inside_interface, stack)

    def visit(node, inside_interface: bool, stack: list) -> None:
        if node.type == "ERROR" or node.is_missing:
            record_error(node)
        if node.is_named and node.type == "interface":
            # `- 1` for the same reason as `body_start` below, and it is NOT cosmetic here: a
            # node's span may include its terminating newline, and `_next_line_start` applied to
            # that newline's successor answers one line TOO FAR. Blanking one line too many
            # deleted the first statement after `end interface` from the body — which, when that
            # statement was the only assignment to the `intent(out)` dummy, silenced all three
            # gates. Fail-OPEN, found by the terminator x interface acceptance matrix and NOT by
            # the 365-file differential, which cannot see it: 0 of the 365 models declare an
            # interface inside a body.
            interface_spans.append(
                (
                    _line_start(view, to_char(node.start_byte)),
                    _next_line_start(view, max(to_char(node.end_byte) - 1, 0)),
                )
            )
            inside_interface = True
        kind = _PROCEDURE_KINDS.get(node.type) if node.is_named else None
        if kind and node.children and not inside_interface:
            procedure = _procedure(view, encoded, node, kind, to_char)
            if procedure is not None:
                procedures.append(procedure)
        # Reversed so the stack pops children left to right: `procedures` is sorted by
        # `body_start` afterwards, but `errors` is reported in the order found and a reader
        # follows it top to bottom.
        for child in reversed(node.children):
            stack.append((child, inside_interface))

    walk(tree.root_node)
    procedures.sort(key=lambda item: item.body_start)
    return StructureTree(
        view=view,
        procedures=tuple(procedures),
        interface_spans=tuple(sorted(interface_spans)),
        errors=tuple(sorted(errors, key=lambda item: (item.line, item.snippet))),
    )


def _procedure(view: str, encoded: bytes, node, kind: str, to_char) -> Procedure | None:
    header = node.children[0]
    if not header.is_named:
        return None
    name_node = header.child_by_field_name("name")
    name = _text(encoded, name_node) if name_node is not None else ""

    parameters = header.child_by_field_name("parameters")
    dummy_args_text = ""
    # An EMPTY list (`subroutine s()`) puts the bare `(` token in this field rather than a named
    # `parameters` node, so the field's presence does not mean there are dummies. Requiring the
    # named node answers "" for `s()` exactly as it does for the paren-less `s`, which is what the
    # walk's balanced-paren extraction answered for both.
    if parameters is not None and parameters.is_named and parameters.type == "parameters":
        raw = _text(encoded, parameters)
        # The node spans the parenthesised list; the walk this replaces carried the text BETWEEN
        # the parens, and `_split_fortran_names` is written against that.
        if raw.startswith("(") and raw.endswith(")"):
            raw = raw[1:-1]
        dummy_args_text = raw

    result_name = None
    if kind == "function":
        result_name = name or None
        for child in header.children:
            if child.is_named and child.type == "function_result":
                for grandchild in child.children:
                    if grandchild.is_named:
                        result_name = _text(encoded, grandchild)
                        break

    # `max(..., 0)` and the `- 1`: a statement node's span may or may not include the newline that
    # terminates it (tree-sitter-fortran includes it), and stepping back onto the statement's last
    # character makes `_next_line_start` answer "the line after the header" under both.
    body_start = _next_line_start(view, max(to_char(header.end_byte) - 1, 0))
    end_node = node.children[-1]
    if end_node.is_named and end_node.type.startswith("end_"):
        body_end = _line_start(view, to_char(end_node.start_byte))
    else:
        body_end = _next_line_start(view, to_char(node.end_byte))
    body_end = max(body_end, body_start)

    contains_at = None
    for child in node.children:
        if child.is_named and child.type == "internal_procedures":
            for grandchild in child.children:
                if grandchild.is_named and grandchild.type == "contains_statement":
                    contains_at = _line_start(view, to_char(grandchild.start_byte))
                    break
            break

    return Procedure(
        kind=kind,
        name=name,
        dummy_args_text=dummy_args_text,
        result_name=result_name,
        body_start=body_start,
        body_end=body_end,
        contains_at=contains_at,
    )


def blank_interface_spans(view: str, spans: tuple[tuple[int, int], ...]) -> str:
    """``view`` with every interface span replaced by blanks, IN PLACE.

    In place, not deleted, so a body stays ONE `start:end` slice of a string the same length as
    the view — the invariant the dependency-dataflow gate's before/after position comparison
    rests on. (Deletion would preserve ORDER too; what it would not preserve is the offsets.)
    """
    if not spans:
        return view
    characters = list(view)
    for start, end in spans:
        for index in range(start, min(end, len(characters))):
            if characters[index] != "\n":
                characters[index] = " "
    return "".join(characters)
