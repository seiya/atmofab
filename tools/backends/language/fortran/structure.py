"""The structural front end the three `problem` model gates, and the §5.1 signature pin, read
Fortran through.

Replaces a hand-rolled regex walk over Fortran's keyword structure. That walk was rewritten
four times and broken sixteen; every break was the same shape — a spelling the language allows
and the rules did not enumerate (`endsubroutine` as one word, a bare `end`, a construct named
after a keyword, a variable named after a keyword, an `interface` body's own `end subroutine`).
The set of such spellings is not closed by enumeration, which is why this asks a parser instead.

WHAT THIS PARSES IS A VIEW, NOT SOURCE. Callers hand over
`source.joined_masked_view` output (the view moved there from the validator in issue #289's
R4-b PR-3): lower-cased, `&` continuations
joined, one statement per line, comment and literal CONTENT blanked length-preservingly. Two
properties of that view are load-bearing here:

* it is a FIXED POINT, so a caller that has already reduced its text pays one idempotent pass;
* every offset this module returns is an offset INTO THE VIEW, so a body is one contiguous slice
  of it. `_validate_problem_model_dependency_dataflow` compares an assignment position with a
  call position taken inside that slice, and those two are comparable only within one view.

FAIL-CLOSED, IN TWO DIRECTIONS:

* **the packages are absent** → `FortranStructureUnavailableError`, which propagates to the
  validator's `main`, where it becomes a DEDICATED EXIT CODE; the conductor terminalizes the run
  on that code (`static_frontend_unavailable`) instead of spending a leaf's retry budget on a
  machine problem the leaf cannot fix. The caller also puts `FORTRAN_STRUCTURE_UNAVAILABLE_MARKER`
  in the message, but that is for a human reader and carries no decision — an earlier version of
  this line said the marker is what makes the conductor terminalize, which was the text-scan
  design the exit code replaced;
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

DEPENDENCIES. Two packages, pinned by measurement — the versions are `MEASURED_PACKAGE_VERSIONS`
below, which is the single definition this module, its refusal message, `requirements.txt` and
`docs/RUNBOOK.md` §0-1 are all checked against (`tools/tests/test_dependency_declaration.py`). A
version bump re-runs `tools/backends/language/fortran/structure_differential.py` (both halves)
before it is accepted — the tree half proves the corpus still parses the same, the flang half
proves it parses it RIGHT.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from tools.backends import registry as backend_registry

#: The versions this front end was MEASURED on, by pip distribution name. Written here, in the
#: backend that depends on them, because the value is a property of THIS code: the module drives a
#: `Language(tree_sitter_fortran.language())` / `Parser(language)` spelling that has changed across
#: py-tree-sitter releases, and the grammar's node-type names are what every gate below matches on.
#:
#: It is a CONSTANT rather than six prose spellings because the same fact used to be written out in
#: this module's docstring, in its refusal message, and in `docs/RUNBOOK.md` — three statements a
#: sweep has to keep in step by discipline. `tools/tests/test_dependency_declaration.py` now checks
#: `requirements.txt` and the runbook against this dict, and the refusal message renders from it.
#: A bump is accepted only after re-running `structure_differential.py` (both halves).
MEASURED_PACKAGE_VERSIONS: dict[str, str] = {
    "tree-sitter": "0.26.0",
    "tree-sitter-fortran": "0.6.0",
}

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
_REQUIRED_NODE_TYPES = (
    "interface", "internal_procedures", "contains_statement",
    # The program-unit types `module_level_procedure_names` scopes by. Listed here for the same
    # reason as the three above, but NOT for the reason a first version of this comment gave: it
    # said a grammar rename "would otherwise make the scope silently universal again, which is
    # the exact fail-open that scoping was added to close". Measured — with these entries gone
    # and the collection predicate pointed at names no grammar defines, `units` is empty, the
    # scope is empty, and `module_level_procedure_names` returns the empty set, so a faithful
    # model gets one "never DEFINES it" per published operation. The failure is total
    # OVER-REFUSAL, fail-CLOSED, which is why the guard is still worth having: it converts an
    # unrepairable warm-retry loop into an operator-facing unavailable error. Two round-2
    # reviewers found the direction stated backwards, independently.
    "module", "submodule", "module_statement", "submodule_statement",
    # The two declaration statement types a procedure's own specification part is read from by
    # the §5.1 signature pin (`Procedure.declarations`). A rename leaves every pinned dummy
    # "not declared", which is total over-refusal; the guard turns it into the unavailable error.
    "variable_declaration", "variable_modification",
    # A derived type a procedure defines in its own specification part (`Procedure.local_types`):
    # a renamed node would make every such shadow of a published type invisible again.
    "derived_type_definition", "derived_type_statement", "type_name",
    # The parts of a derived type definition the §5.1 type comparison reads (`DerivedType`): the
    # one header attribute it accepts and the end statement it skips. A renamed `access_specifier`
    # would make `type, public :: t` an unaccepted header (over-refusal); a renamed
    # `end_type_statement` would read as a non-component statement (over-refusal too).
    "access_specifier", "end_type_statement",
)


class FortranStructureUnavailableError(backend_registry.BackendFrontendUnavailable):
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


#: The statement types `Procedure.declarations` collects: a type declaration (with or without
#: `::`, `procedure(iface) :: cb` included) and an attribute statement (`optional :: n`,
#: `dimension n(:)`, `intent(in) :: x`, `value`, `pointer`, `target`, `allocatable`, ...).
_DECLARATION_TYPES = ("variable_declaration", "variable_modification")


@dataclass(frozen=True)
class Declaration:
    """One declaration statement, as offsets into the view: ``start`` is the start of its line and
    ``end`` the start of the line after it (the view holds one statement per line)."""

    kind: str
    start: int
    end: int


@dataclass(frozen=True)
class Procedure:
    """One procedure DEFINITION, with its body located as offsets into the view.

    ``header_start`` is the start of the line holding the header statement, ``body_start`` the
    start of the line after it, and ``body_end`` the start of the line holding the END statement,
    which is what makes ``view[body_start:body_end]`` the body and nothing else. ``contains_at`` is the start of this procedure's own `contains` line
    (None when it has none): declarations before it are this procedure's, procedures after it are
    its own contained ones whose dummies are NOT its.

    ``declarations`` are the procedure's OWN declaration statements: the DIRECT children of its
    node of a `_DECLARATION_TYPES` type. A declaration inside a `BLOCK`, an `interface` body or a
    contained procedure is a child of that construct, not of this node, so it is not one — which
    is what the §5.1 signature pin needs: a `BLOCK`'s or a contained procedure's declarations are
    another scope's, and an interface body's declare the INTERFACE's dummies. (An interface block
    can declare a dummy PROCEDURE of this one; that dummy is then not declared by any statement
    read here, and the pin refuses it, since §5.1 writes a dummy procedure
    `procedure(<interface>) :: <name>`.)
    Empty for the abbreviated `module procedure` form.

    ``local_types`` are the lowercased names of the derived types the procedure DEFINES among
    those same direct children. One named like a type a dummy is declared with shadows it: the
    dummy's `type(t)` then names the local type, which no caller can pass (issue #430 round 1).
    """

    kind: str
    name: str
    dummy_args_text: str
    result_name: str | None
    header_start: int
    body_start: int
    body_end: int
    contains_at: int | None
    declarations: tuple[Declaration, ...] = ()
    local_types: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProgramUnit:
    """One `module` / `submodule` program unit, located as offsets into the view.

    ``parent`` is the ancestor module a submodule extends (``None`` for a module). Both names are
    lowercased. A unit whose own name the parser does not report is not emitted at all, so a
    caller scoping by name can never match one by accident.
    """

    kind: str
    name: str
    parent: str | None
    start: int
    end: int


@dataclass(frozen=True)
class DerivedType:
    """One derived type DEFINITION, wherever it stands in the file, as the §5.1 type comparison
    reads it (issue #430).

    ``name`` is the lowercased `type_name` of its opening statement. ``header_extras`` are the
    lowercased texts of that statement's other named children — an `access_specifier`
    (``public``), ``extends(...)``, ``abstract``, ``bind(c)``, a type-parameter list.
    ``components`` are the definition's DIRECT `variable_declaration` children, in source order;
    ``other_children`` the node types of every other direct named child except the opener and the
    `end type` statement (``private_statement``, ``sequence_statement``,
    ``derived_type_procedures``, a preprocessor conditional, ...).

    The scope is recorded as the walk found it: ``unit`` / ``unit_kind`` are the innermost
    `module` / `submodule` the definition stands in (None outside one), ``in_procedure`` is True
    inside any procedure node (a definition, a `BLOCK` in one, or a prototype in an interface
    body), and ``in_interface`` True inside any `interface` block."""

    name: str
    start: int
    header_extras: tuple[str, ...]
    components: tuple[Declaration, ...]
    other_children: tuple[str, ...]
    unit: str | None
    unit_kind: str | None
    in_procedure: bool
    in_interface: bool


@dataclass(frozen=True)
class StructureTree:
    view: str
    procedures: tuple[Procedure, ...]
    interface_spans: tuple[tuple[int, int], ...]
    units: tuple[ProgramUnit, ...]
    errors: tuple[StructureError, ...]
    types: tuple[DerivedType, ...] = ()


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
            f"pip install -r requirements.txt"
        ) from exc
    try:
        language = Language(tree_sitter_fortran.language())
        parser = Parser(language)
    except Exception as exc:
        raise FortranStructureUnavailableError(
            f"{FORTRAN_STRUCTURE_UNAVAILABLE_MARKER} the Fortran structure front end failed to "
            f"initialise ({exc}). The installed tree-sitter and tree-sitter-fortran are not ABI "
            f"compatible; install the measured pair with: pip install -r requirements.txt"
        ) from exc
    # THE GRAMMAR MUST STILL SPEAK THE NODE NAMES THIS MODULE MATCHES ON. Everything below keys
    # on node TYPE STRINGS, so a grammar that renames one reports no procedures and no errors —
    # and every gate then returns at its empty-envelope loop with nothing to say. Silent, which
    # is the one outcome this module exists to prevent, and invisible to a version DECLARATION,
    # which bounds what a fresh install resolves and says nothing about what the resolved grammar
    # names its nodes. `requirements.txt` now pins both packages at `MEASURED_PACKAGE_VERSIONS`,
    # and that changes the reachability of the class below without closing it: an operator can
    # install by hand, and a pin is a claim about a release number rather than about node types.
    # Asking the grammar directly costs one call and converts that whole class into the
    # unavailable error.
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
            f"tree-sitter-fortran {MEASURED_PACKAGE_VERSIONS['tree-sitter-fortran']}; pin that "
            f"version, and re-run "
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
    units: list[ProgramUnit] = []
    types: list[DerivedType] = []
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
    # Each stack entry carries the scope the walk is in: inside an `interface` block, inside a
    # procedure node, and the innermost `module` / `submodule` as `(kind, name)`. Only the first
    # decides what is collected as a procedure; the other two place a derived type definition.
    def walk(root) -> None:
        stack: list[tuple[object, bool, bool, tuple[str, str] | None]] = [
            (root, False, False, None)]
        while stack:
            node, inside_interface, inside_procedure, unit = stack.pop()
            visit(node, inside_interface, inside_procedure, unit, stack)

    def visit(node, inside_interface: bool, inside_procedure: bool,
              unit: tuple[str, str] | None, stack: list) -> None:
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
        if node.is_named and node.type in ("module", "submodule"):
            program_unit = _program_unit(encoded, node, to_char)
            # An unnamed unit still opens a scope: a definition inside it is not at module level
            # of any NAMED unit, so it must not inherit the enclosing one.
            unit = (node.type, program_unit.name if program_unit is not None else "")
            if program_unit is not None:
                units.append(program_unit)
        if node.is_named and node.type == "derived_type_definition":
            derived = _derived_type(view, encoded, node, to_char, unit,
                                    inside_procedure, inside_interface)
            if derived is not None:
                types.append(derived)
        kind = _PROCEDURE_KINDS.get(node.type) if node.is_named else None
        if kind and node.children and not inside_interface:
            procedure = _procedure(view, encoded, node, kind, to_char)
            if procedure is not None:
                procedures.append(procedure)
        if kind:
            inside_procedure = True
        # Reversed so the stack pops children left to right: `procedures` is sorted by
        # `body_start` afterwards, but `errors` is reported in the order found and a reader
        # follows it top to bottom.
        for child in reversed(node.children):
            stack.append((child, inside_interface, inside_procedure, unit))

    walk(tree.root_node)
    procedures.sort(key=lambda item: item.body_start)
    return StructureTree(
        view=view,
        procedures=tuple(procedures),
        interface_spans=tuple(sorted(interface_spans)),
        units=tuple(sorted(units, key=lambda item: item.start)),
        errors=tuple(sorted(errors, key=lambda item: (item.line, item.snippet))),
        types=tuple(sorted(types, key=lambda item: item.start)),
    )


def _derived_type(view: str, encoded: bytes, node, to_char, unit: tuple[str, str] | None,
                  in_procedure: bool, in_interface: bool) -> DerivedType | None:
    """The `DerivedType` at ``node``, or None when its opener reports no `type_name`."""
    opener = next((child for child in node.children
                   if child.is_named and child.type == "derived_type_statement"), None)
    if opener is None:
        return None
    name = None
    extras: list[str] = []
    for part in opener.children:
        if not part.is_named:
            continue
        if part.type == "type_name" and name is None:
            name = _text(encoded, part).strip().lower()
        else:
            extras.append(_text(encoded, part).strip().lower())
    if not name:
        return None
    components: list[Declaration] = []
    others: list[str] = []
    for child in node.children:
        if not child.is_named or child is opener or child.type == "end_type_statement":
            continue
        if child.type == "variable_declaration":
            components.append(Declaration(
                kind=child.type,
                start=_line_start(view, to_char(child.start_byte)),
                end=_next_line_start(view, max(to_char(child.end_byte) - 1, 0)),
            ))
        else:
            others.append(child.type)
    return DerivedType(
        name=name,
        start=to_char(node.start_byte),
        header_extras=tuple(extras),
        components=tuple(components),
        other_children=tuple(others),
        unit=unit[1] if unit is not None else None,
        unit_kind=unit[0] if unit is not None else None,
        in_procedure=in_procedure,
        in_interface=in_interface,
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

    declarations = tuple(
        Declaration(
            kind=child.type,
            start=_line_start(view, to_char(child.start_byte)),
            end=_next_line_start(view, max(to_char(child.end_byte) - 1, 0)),
        )
        for child in node.children
        if kind != "module_procedure" and child.is_named and child.type in _DECLARATION_TYPES
    )

    local_types = tuple(
        _text(encoded, name_node).strip().lower()
        for child in node.children
        if kind != "module_procedure" and child.is_named
        and child.type == "derived_type_definition"
        for opener in child.children
        if opener.is_named and opener.type == "derived_type_statement"
        for name_node in opener.children
        if name_node.is_named and name_node.type == "type_name"
    )

    return Procedure(
        kind=kind,
        name=name,
        dummy_args_text=dummy_args_text,
        result_name=result_name,
        header_start=_line_start(view, to_char(header.start_byte)),
        body_start=body_start,
        body_end=body_end,
        contains_at=contains_at,
        declarations=declarations,
        local_types=local_types,
    )


def _program_unit(encoded: bytes, node, to_char) -> ProgramUnit | None:
    """The `module` / `submodule` unit at ``node``, or None when the parser reports no name.

    The unit's own name and (for a submodule) its ancestor module come from the OPENING statement
    only. The `end` statement may repeat the name and may omit it; reading either would make the
    answer depend on which spelling the source used.
    """
    opener = f"{node.type}_statement"
    for child in node.children:
        if not (child.is_named and child.type == opener):
            continue
        name = None
        parent = None
        for part in child.children:
            if not part.is_named:
                continue
            if part.type == "name" and name is None:
                name = _text(encoded, part).strip().lower()
            elif part.type == "module_name" and parent is None:
                parent = _text(encoded, part).strip().lower()
        if not name:
            return None
        return ProgramUnit(
            kind=node.type,
            name=name,
            parent=parent,
            start=to_char(node.start_byte),
            end=to_char(node.end_byte),
        )
    return None


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


#: What a leaf is told when a §5.1-published operation is declared but never defined. It lives
#: here, not at the gate that raises it, because every noun in it is this language's: the block a
#: prototype sits in, and the section a definition belongs to. The neutral gate interpolates it.
UNDEFINED_PUBLISHED_PROCEDURE_REMEDY = (
    "the pinned header appears only as a prototype — inside an `interface` block, or as a "
    "procedure contained within another procedure — so the module publishes a name with no "
    "implementation, and a consumer that calls it fails at LINK with `undefined reference`. "
    "Define it in the module's own `contains` section with the published header, and remove "
    "the prototype"
)

#: What a leaf is told when the publishing module DEFINES a §5.1 operation in the one form whose
#: header and declarations the §5.1 comparison cannot read. Here for the same reason as the
#: constant above: every form it names is this language's. A prefix, a type before `function` and a
#: second header inside the body used to be listed here too; each is now a header difference the
#: comparison reports itself.
UNREAD_DEFINITION_HEADER_REMEDY = (
    "it is an abbreviated `module procedure`, which repeats no header and declares no dummy, so "
    "there is nothing to compare with §5.1. Write the full header exactly as §5.1 pins it, with "
    "each dummy and the result declared in the procedure's own specification part"
)

#: What a leaf is told when this front end cannot resolve a source. Same reason for living here:
#: the shapes it names are spellings of THIS language. The class is not closed — see the module
#: docstring, which is canonical for why an enumeration is the wrong instrument.
STRUCTURE_REFUSAL_HINT = (
    "the shape to look for is an identifier or a statement label sitting where the parser "
    "expects structure — a VARIABLE or construct named after a keyword (`endsubroutine`, "
    "`interface`, `contains` are legal names and are read as the statements they spell; rename "
    "it), or a labelled `DO` / `FORMAT` alongside a labelled `contains` or procedure header "
    "(give the loop an `end do` and drop the label from the specification statement). Neither "
    "list is closed"
)


def publishing_unit_present(tree: StructureTree, unit_name: str) -> bool:
    """Does ``tree`` declare the program unit ``unit_name``, or a submodule descended from it?

    Separate from `module_level_procedure_names` because the two answer questions a caller must
    not conflate. That function returns the empty set both for "the unit is here and implements
    nothing" and for "the unit is not here at all", and a caller that cannot tell them apart tells
    a leaf to define procedures its source already defines — measured on a correct model whose
    module name did not match its file's: three violations, every clause of the remedy false for
    the source, and no violation anywhere in the stage naming the actual fault."""
    wanted = unit_name.strip().lower()
    return any(
        unit.name == wanted or (unit.parent is not None and unit.parent == wanted)
        for unit in tree.units
    )


@dataclass(frozen=True)
class Definition:
    """A module-level procedure definition as the §5.1 signature pin reads it: the text of its
    header statement and of each of its own declaration statements (`Procedure.declarations`), as
    ``(statement type, text)`` pairs in source order, and the derived types it defines itself
    (`Procedure.local_types`)."""

    header: str
    declarations: tuple[tuple[str, str], ...]
    local_types: tuple[str, ...] = ()


def module_level_definitions(
    tree: StructureTree,
    unit_name: str,
    text_between: Callable[[int, int], str],
) -> dict[str, Definition | None]:
    """What each procedure ``tree`` DEFINES at the top level of ``unit_name`` declares, keyed by
    lowercased name. ``text_between(start, stop)`` returns the caller's view text between two of
    ``tree``'s offsets (the caller owns the translation when it parsed a label-preserving twin).

    This is what the §5.1 signature pin compares, and the whole-file stanza splitter is not. The
    splitter reads a header wherever it stands and keys it by NAME, while the definedness answer is
    about ONE procedure, so a source could satisfy each with a different one (PR #279: a contained
    procedure, a prototype in another body, or a DTIO prototype in a `BLOCK` of the definition
    itself, each carrying the pinned header beside a drifted definition). Reading the header and
    the declarations from the definition the structure reader found leaves no second procedure to
    read them from; reading only the definition's OWN type declarations and attribute statements
    leaves no `BLOCK`, interface body or contained procedure to supply a dummy's characteristics
    (issue #430; `Procedure.declarations` says what an interface block can still declare).

    None for an abbreviated separate module subprogram (`module procedure <name>`), which repeats
    no header and may not redeclare its dummies, so there is nothing of it to compare."""
    definitions: dict[str, Definition | None] = {}
    for procedure in module_level_procedures(tree, unit_name):
        name = procedure.name.strip().lower()
        if procedure.kind not in ("subroutine", "function"):
            definitions[name] = None
            continue
        definitions[name] = Definition(
            header=text_between(procedure.header_start, procedure.body_start).strip(),
            declarations=tuple(
                (declaration.kind, text_between(declaration.start, declaration.end).strip())
                for declaration in procedure.declarations),
            local_types=procedure.local_types,
        )
    return definitions


@dataclass(frozen=True)
class TypeDefinition:
    """A derived type definition as the §5.1 type comparison reads it: ``header_extras`` and
    ``other_children`` as `DerivedType` records them, and the text of each component declaration
    in source order."""

    header_extras: tuple[str, ...]
    components: tuple[str, ...]
    other_children: tuple[str, ...]


@dataclass(frozen=True)
class TypeReading:
    """`module_level_type_definitions`' answer. ``definitions`` are the types defined at module
    level of the publishing module, keyed by lowercased name; ``counts`` is how many
    `derived_type_definition`s of each name the whole file carries, in any scope."""

    definitions: dict[str, TypeDefinition]
    counts: dict[str, int]


def module_level_type_definitions(
    tree: StructureTree,
    unit_name: str,
    text_between: Callable[[int, int], str],
) -> TypeReading:
    """The derived types ``tree`` defines at module level of the MODULE ``unit_name`` — not in a
    submodule (a consumer's `use` reaches only the module's own specification part), not inside a
    procedure (a definition, a `BLOCK` in one) and not inside an `interface` block — plus how many
    times each type name is defined anywhere in the file.

    This is what the §5.1 type comparison reads, and the whole-file stanza splitter is not
    (issue #430). The splitter keyed a type by its name AS WRITTEN and read only the `type ::`
    header form, so a module-level `type t` (no `::`) or `TYPE :: T` was invisible to it, and a
    private helper's local `type :: t` carrying the pinned components stood in for a drifted
    published one. Reading the definition the module's own specification part carries leaves no
    other definition to read; counting every definition of the name, any scope, refuses the second
    one a reader of the name could be handed instead (a local type of the same name in a published
    procedure makes that procedure's `type(t)` dummies the local type).

    The ``in_interface`` term is implied by ``in_procedure`` for every definition the grammar can
    place there (an interface block holds procedure bodies and `module procedure` statements, so a
    type in one stands in a procedure body), and is not pinned: dropping it survives the suite. It
    states the scope rather than deciding it."""
    wanted = unit_name.strip().lower()
    definitions: dict[str, TypeDefinition] = {}
    counts: dict[str, int] = {}
    for derived in tree.types:
        counts[derived.name] = counts.get(derived.name, 0) + 1
        if (derived.unit_kind == "module" and derived.unit == wanted
                and not derived.in_procedure and not derived.in_interface):
            definitions.setdefault(derived.name, TypeDefinition(
                header_extras=derived.header_extras,
                components=tuple(text_between(c.start, c.end).strip()
                                 for c in derived.components),
                other_children=derived.other_children,
            ))
    return TypeReading(definitions, counts)


def module_level_procedure_names(
    tree: StructureTree, unit_name: str | None = None
) -> frozenset[str]:
    """The names of `module_level_procedures`, lowercased."""
    return frozenset(
        procedure.name.strip().lower()
        for procedure in module_level_procedures(tree, unit_name))


def module_level_procedures(
    tree: StructureTree, unit_name: str | None = None
) -> tuple[Procedure, ...]:
    """The procedures ``tree`` DEFINES at module level.

    THREE exclusions, and they answer one question from three sides: does this name have an
    implementation that the module publishing it actually carries?

    A procedure DECLARED inside an `interface` block is not in ``tree.procedures`` at all
    (`parse_view` does not descend into an interface span), which is the property this function is
    built on. A caller that tracked `interface` / `end interface` itself would fail in two
    directions and only one of them is safe: miss an opener and a prototype passes as a
    definition, miss a closer and a real definition reads as a prototype. `interface` is a legal
    variable name, so neither miss is hypothetical — the module docstring's whole subject.

    A CONTAINED procedure is excluded. It carries the name but does not publish it: a consumer's
    `use` cannot reach it, so accepting one leaves exactly the undefined reference at the
    consumer's link that the prototype-only shape leaves. Containment is decided by the body spans
    the parser reports — P is contained when another procedure's body encloses P's start — not by
    counting `contains` statements. The comparison stays inside ONE tree, so it needs no view
    translation: both offsets come from the same parse.

    A definition in ANOTHER PROGRAM UNIT is excluded when ``unit_name`` is given, and that
    exclusion is the whole reason this parameter exists. Without it the answer is "is this name
    defined anywhere in the parse", which a decoy satisfies: a review round measured a model whose
    published operation was a prototype in the published module and an empty stub in a second
    module in the same file — gate 0 violations, compiler rc=0, and the consumer still failed at
    LINK with `undefined reference`, i.e. the exact fail-open the definedness check exists to
    close, reopened one unit away. The duplicate-symbol backstop that should have caught the two
    headers did not, because the decoy's header carried a prefix the stanza reader does not model.
    Scoping removes the class rather than that spelling.

    A SUBMODULE descended from ``unit_name`` counts as the same publisher: a separate module
    subprogram is the module's own implementation and a consumer links against it exactly as if it
    were written inline. The whole chain counts, and the reason is a property of the LANGUAGE
    rather than a depth limit: `submodule (ancestor : parent) name` names the ANCESTOR MODULE
    first, and that is what the parser reports as `parent`, so `submodule (m:mid) leaf` carries
    `parent == "m"` and is reached by scoping to `m`. Measured — an earlier version of this
    paragraph said the opposite in both halves ("only one level is followed … a submodule of a
    submodule does not count"), and also implied that naming the INTERMEDIATE submodule would
    reach the descendant, which it does not: scoping to `mid` returns the empty set.

    ``unit_name`` matching is by the unit's own declared name, lowercased. A source declaring no
    unit of that name yields none, which fails every published procedure — fail-closed,
    and the right answer: the module the node is contracted to publish is not there.

    An abbreviated separate module subprogram (`module procedure solve`, in a submodule) IS an
    implementation and IS returned. `_validate_problem_model_*` refuses that form for a different
    reason — F2008 forbids it from redeclaring its dummies, so those gates would read an empty
    out-set — and reading the two rules as one would fail a legal submodule node."""
    scope: list[tuple[int, int]] = []
    if unit_name is not None:
        wanted = unit_name.strip().lower()
        scope = [
            (unit.start, unit.end)
            for unit in tree.units
            if unit.name == wanted or (unit.parent is not None and unit.parent == wanted)
        ]
        if not scope:
            return ()
    bodies = [(p.body_start, p.body_end) for p in tree.procedures]
    found: list[Procedure] = []
    for index, procedure in enumerate(tree.procedures):
        if scope and not any(
            start <= procedure.body_start < end for start, end in scope
        ):
            continue
        nested = any(
            start <= procedure.body_start < end
            for other, (start, end) in enumerate(bodies)
            if other != index
        )
        if not nested:
            found.append(procedure)
    return tuple(found)
