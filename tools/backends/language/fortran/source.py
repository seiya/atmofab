#!/usr/bin/env python3
"""How the deterministic gates READ a Fortran source (issue #289, R4-b PR-3).

The `source_reading` capability. Everything here moved, unchanged in behaviour, out of
`tools/validate_pipeline_semantics.py`, where it had been Fortran knowledge sitting in the neutral
core: the one-statement-per-line view, the declaration and procedure-envelope readers, the call
and assignment readers, the module-dependency map, the checks-module ABI facts, and the gates whose
every rule is a statement of Fortran syntax (the three `problem` model gates, the dependency-
operation presence checks, the runner's JSON-serialization and snapshot-filename scans, the
component's published-procedure list). What stays in the validator is the neutral orchestration:
which files each gate reads, which node kinds it applies to, and how its findings are attributed.
It reaches this module through `registry.capability_module("language", <language>,
"source_reading")`, with the language of the pipeline's target, so a second language is refused
at that lookup rather than read as Fortran.

The contract a reader takes off this module — the names its callers use — is pinned by
`tools/tests/test_backend_boundary.py` (`_LANGUAGE_CAPABILITY_CONTRACT`).

Imports nothing from the neutral core: the validator imports the registry, which loads this module
lazily, so a back-import would be a cycle through a half-initialized module.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from tools.backends.language.fortran import lines as fortran_lines
from tools.backends.language.fortran import structure as fortran_structure
from tools.backends.language.fortran.bundle import IDENTIFIER_MAX

IDENTIFIER_PATTERN = re.compile(r"[a-z_][a-z0-9_]*")



KEYWORDS = {
    "if",
    "then",
    "else",
    "endif",
    "do",
    "enddo",
    "call",
    "subroutine",
    "module",
    "contains",
    "intent",
    "in",
    "out",
    "inout",
    "real",
    "integer",
    "logical",
    "character",
    "type",
    "public",
    "private",
    "use",
    "only",
    "true",
    "false",
}



def joined_masked_view(lowered: str) -> str:
    """``lowered`` as ONE STATEMENT PER LINE, `&` continuations joined and code-lookalikes masked.

    The view every rule in this module that matches Fortran's KEYWORD STRUCTURE over multi-line
    source must read. `fortran_lines.mask_code_lookalikes` alone cannot serve them: it preserves
    line structure by design, so a legally wrapped statement still reaches a `[^\\n]`-bounded or
    `re.MULTILINE`-anchored pattern as fragments. `fortran_lines.fortran_logical_lines` joins, and
    composing the two is what closes that whole class at once — the wrapped `intent(out)` entity
    list, the wrapped `call` actual list, the wrapped `use` / `module` statement.

    One property of the composition is load-bearing and one is merely conventional:

    * **The `;` split is required, not cosmetic.** `fortran_logical_lines` deliberately does not
      split on `;`, so joining ALONE would create a defect that does not exist on unjoined text:
      `real :: tmp; tmp = 0.0` becomes one line, `assignment_records`' `^\\s*` MULTILINE anchor
      stops seeing `tmp = 0.0`, and its `([^\\n!]+)` right-hand side swallows a following
      `; call dep__op(...)` into the identifier set — a phantom producer, fail-open at exactly the
      `isdisjoint` test of the dependency-dataflow gate. Emitting one statement per line restores
      the invariant all three consumers' patterns already assume.
    * **Join first, mask second** — for the reason, not the effect. `fortran_logical_lines` does
      its own comment, quote and continuation tracking over RAW text, and the mask is
      length-preserving, so the two in fact COMMUTE: a reviewer brute-forced 2940 inputs
      (comments, unterminated literals, `;`/`&`/quotes inside comments, labels) and found no
      input where the orders differ. An earlier draft of this note called the ordering
      load-bearing; it is not, and it is kept only because masking last is the order in which the
      result's offsets are obviously comparable.

    Each emitted statement is right-stripped and empty statements are dropped. That is not
    cosmetic: without it the result is NOT a fixed point, and the fixed point is what lets a
    consumer re-apply the view to a fragment of a view — which is what keeps `split_names`
    total with respect to both its raw and its joined callers. Four inputs proved it, and the
    fixed-point test uses all four rather than a well-formed one, which does not distinguish them.
    Only the first is legal Fortran: a trailing `;` (`x = 1;`) emits an empty statement a second
    pass would drop. The other three are robustness against text the `Generate.gate` syntax check
    rejects before this one runs — a lone-`&` line and text ending mid-continuation each leave the
    blank that preceded the consumed marker, and an unterminated literal leaves the blanks the
    mask wrote over its contents.

    INVARIANT, and the price of this view: offsets into the result no longer index the ORIGINAL
    file. Comment-only and blank lines are gone and each `&` has collapsed. Offsets remain
    comparable with EACH OTHER as long as both come from the same view — which is what the
    dependency-dataflow gate's `call_pos` / assignment `pos` comparison relies on, and every gate
    here reports `{model_file}: subroutine {name}` with no line number. Any future rule that wants
    to REPORT a line must take it from `fortran_lines.fortran_logical_lines`' `start_lineno`, not
    from an offset into this string."""
    masked = fortran_lines.mask_code_lookalikes(
        "\n".join(
            _FORTRAN_STATEMENT_LABEL.sub("", statement.lstrip(), count=1)
            for _lineno, line in fortran_lines.fortran_logical_lines(lowered)
            for statement in fortran_lines.split_fortran_statements(line)
        )
    )
    return "\n".join(
        stripped for stripped in (line.rstrip() for line in masked.split("\n")) if stripped
    )



# A declaration whose `::` is optional because it carries no attribute and no initializer, plus
# the `::`-less `enumerator` list. The type-spec's own parenthesised selector (`character(len=3)`,
# `type(t)`) is stepped over by `extract_balanced_parens`, not by a regex, so a comma or `=`
# inside it cannot be read as an entity separator.
# Every `use` that NAMES a local entity — an `only:` list, a rename list, or both, with or
# without the `, non_intrinsic ::` module-nature prefix. An earlier form keyed on the word
# `only` and could not cross the comma that follows `use`, so a bare rename
# (`use m, ncomp => slot`) and the prefixed spelling both went unseen.
_FORTRAN_USE_LOCAL_NAMES = re.compile(
    r"^use\b\s*(?:,\s*(?:non_)?intrinsic\s*)?(?:::)?\s*[a-z_][a-z0-9_]*\s*,\s*"
    r"(?:only\s*:)?"
)



_FORTRAN_ASSOCIATE_OPEN = re.compile(
    r"^(?:[a-z_][a-z0-9_]*\s*:\s*)?(?:associate|select\s*type)\s*\("
)



# A `block` / `associate` / `select type` / `interface` body is a scope of its own. A named
# constant declared inside one is NOT visible to the statements around it, and treating it as if
# it were exempted an actual passed at an earlier call in the enclosing body. Names such a
# construct declares still land in the "other" set, where they can only SUBTRACT — the direction
# that costs a false violation rather than an exemption.
_FORTRAN_CONSTRUCT_OPEN = re.compile(
    r"^(?:[a-z_][a-z0-9_]*\s*:\s*)?(?:block\b|associate\s*\(|select\s*type\s*\(|select\s*case\s*\()"
    r"|^(?:abstract\s+)?interface\b"
)



_FORTRAN_CONSTRUCT_END = re.compile(r"^end\s*(?:block|associate|select|interface)\b")



# Every statement that ATTACHES something to a name. The list of keywords is closed in F2008 and
# short; the SYNTAX behind each of them is neither, so this does not parse them — any statement
# opening with one of these contributes every identifier it mentions to the disqualifying set.
#
# That inversion is the point. Eighteen `::`-less specification statements (`common /blk/ x`,
# `dimension x(3)`, `equivalence (x, y)`, `data x /3/`, `namelist /nl/ x`, `pointer`, `target`,
# `save`, `allocatable`, `external`, `intent`, `volatile`, `asynchronous`, `codimension`,
# `protected`, `value`, `optional`, and the bare `common x`) were each invisible, and each was a
# name made definable while still looking like a pure constant. Enumerating their eighteen
# grammars is the same losing move this rule was adopted to stop making; over-collecting from
# them costs a false violation, which is the direction that may be wrong.
_FORTRAN_ATTRIBUTE_STATEMENT = re.compile(
    r"^(?:common|dimension|equivalence|data|namelist|pointer|target|save|allocatable|external"
    r"|intent|volatile|asynchronous|codimension|contiguous|protected|value|optional|intrinsic"
    r"|bind|sequence|generic|procedure|entry)\b"
)


# `public` and `private` are deliberately absent from that list. They declare nothing — they set
# the accessibility of an entity declared elsewhere — and F2008 R518 makes the `::` optional, so
# leaving them in disqualified `public ncomp` while the `::` branch was skipping
# `public :: ncomp`. The two spellings of one statement must agree, and they agree on the reading
# that matches what the statement does.


_FORTRAN_BARE_DECLARATION = re.compile(
    r"^(integer|real|complex|logical|character|doubleprecision|double\s+precision"
    r"|type|class|enumerator)\b"
)



# Only up to the OPENING paren: the group is delimited by `extract_balanced_parens`, not by a
# greedy `(.*)\)$` — see `parameter_names`.
_FORTRAN_PARAMETER_STATEMENT_PATTERN = re.compile(r"^parameter\s*\(")



# A statement LABEL may precede any statement, including a structural one. Every rule below
# anchors on the keyword, so the label has to come off first — a labelled `10 contains` that went
# unrecognized left the module specification part open across every procedure that followed it.
_FORTRAN_STATEMENT_LABEL = re.compile(r"^\d+\s+")



def _fortran_statement_body(line: str) -> str:
    # The strip is still applied here, not only in the view: this is also reached with RAW
    # fragments (`declared_names` is called on a subroutine body carved out by regex,
    # and on single statements by tests), where no view has run.
    return _FORTRAN_STATEMENT_LABEL.sub("", line.strip().lower(), count=1)



def parameter_names(joined_masked: str) -> set[str]:
    """The names declared as NAMED CONSTANTS in ``joined_masked``.

    ``joined_masked`` must be a `joined_masked_view` — one statement per line. Reading
    physical lines instead would miss a wrapped declaration, which is the defect class this helper
    was written alongside.

    Two forms are recognised:

    * the attribute form, `integer, parameter, public :: ncomp = 3` — the left of the first `::`
      is split on top-level commas and some token must be EXACTLY ``parameter``. An equality test,
      not `\\bparameter\\b`: the word occurs inside `dimension(nparameter)` and inside an
      initializer, and either would otherwise mint a constant that does not exist. The right side
      is split the same way and each item contributes its leading identifier, which drops array
      specs and initializer text.
    * the statement form, `parameter (nlev = 4, mm = 2)` — the parenthesis group is matched by
      `extract_balanced_parens` and NOTHING may follow its close, and every item must carry a
      top-level `=`. A greedy `^parameter\\s*\\((.*)\\)$` was not enough: `parameter` is not a
      reserved word, so `parameter(scratch) = u_in(1)` — an ordinary assignment into an array a
      leaf happened to name `parameter`, which `gfortran -std=f2008` accepts — matched with the
      group `scratch) = u_in(1`, and `scratch` was minted as a constant. That exempted a real
      discarded dependency output. Fail-open, and reachable by naming one array.

    The unparenthesized F77 form (`PARAMETER x = 1`) is deliberately NOT recognised: it is a
    `-std=legacy` gfortran extension, and `Generate.gate`'s syntax check runs `-std=f2008`, so no
    source that reaches these gates can carry one.

    The caller decides SCOPE. This helper reports what the text it is given declares, and a whole
    file is the wrong text to give it: a name that is a constant in one procedure and a live
    variable in another would be reported for both, and the dependency-dataflow gate would then
    exempt a genuinely discarded output — fail-open."""
    return declared_names(joined_masked)[0]



def declared_names(joined_masked: str) -> tuple[set[str], set[str]]:
    """``(named constants, every other declared entity)`` of ``joined_masked``.

    One parser, two answers, because the dependency-dataflow gate needs both and they must agree
    on what a declaration IS. The gate exempts `constants - others` over the WHOLE FILE, so the
    second set carries all of the safety: anything that makes a name definable somewhere must land
    in it. That includes shapes that are not declarations at all — an `associate` / `select type`
    rebinding, and a `use ..., only:` import, since use association overrides host association and
    the imported entity is whatever the other module says it is.

    Constants declared inside a `block` / `associate` / `select type` / `interface` construct go
    to the SECOND set, not the first: such a constant is not visible to the statements around the
    construct, and treating it as if it were exempted a name at a call that preceded it.

    Nothing is ever REMOVED from the second set, and that monotonicity is load-bearing rather than
    tidy. It was briefly broken to pair `integer :: nlev` with a following `parameter (nlev = 4)`
    — one declaration in two statements — and the pairing had no way to be per-declaration in a
    file-wide rule, so it globally re-exempted any name that was a constant in one procedure and
    an ordinary variable in another. That is the very shape this rule exists to refuse, and it
    reappeared within one commit of the redesign. The consequence of not pairing them is that the
    F77 statement form never yields an exemption at all — under the `implicit none` these models
    must declare, `parameter (x = 1)` always follows a type declaration of `x`, which disqualifies
    it. A false violation, and the direction that is allowed to be wrong.

    ``enumerator`` counts as a constant for the same reason ``parameter`` does: an enumerator is
    not definable, so it can never be an output argument. Both of its spellings are read — the
    entity list of an ``enumerator`` statement may omit the ``::``.

    ``import`` is the one ``::`` statement deliberately kept OUT of both sets. It does not declare
    anything: it names an entity of the HOST so an interface body can see it. Counting it as a
    redeclaration made an `import :: ncomp` inside an interface body subtract the very host
    constant it imports, turning a correct silence into a false violation. Subtraction is only
    safe where a statement really does shadow — that is the whole content of the second set."""
    constants: set[str] = set()
    others: set[str] = set()
    construct_depth = 0
    for line in joined_masked.split("\n"):
        statement = _fortran_statement_body(line)
        if not statement:
            continue
        if _FORTRAN_CONSTRUCT_END.match(statement):
            construct_depth = max(0, construct_depth - 1)
            continue
        if _FORTRAN_CONSTRUCT_OPEN.match(statement):
            construct_depth += 1
            # An `associate` header is also a construct opening, and its rebinding is read below
            # before the depth takes effect for the statements inside it.
            if not _FORTRAN_ASSOCIATE_OPEN.match(statement):
                continue
        only_match = _FORTRAN_USE_LOCAL_NAMES.match(statement)
        if only_match is not None:
            # Use association overrides host association, so a name imported here is whatever the
            # other module says it is — not this file's constant. Naming it disqualifies it.
            for item in fortran_lines.split_top_level_commas(statement[only_match.end() :]):
                local, sep, _remote = item.partition("=>")
                match = IDENTIFIER_PATTERN.match(local.strip())
                if match is not None:
                    others.add(match.group(0))
            continue
        if "::" in statement:
            attributes, _, entities = statement.partition("::")
            attribute_tokens = {
                token.strip() for token in fortran_lines.split_top_level_commas(attributes)
            }
            # `import` and a bare accessibility statement (`public :: ncomp`) declare
            # nothing — they name an entity declared elsewhere. Treating them as
            # redeclarations stripped the exemption from any module that lists its
            # constants in a separate `public ::` statement. RE-MEASURED (the earlier
            # figure here, "eight in-tree models, one of them `problem/`-domain", counted
            # distinct lost-name SETS and got the domain count wrong): removing this skip
            # changes the exempt set of 33 of the 365 `*_model.f90` — SEVEN of those FILES
            # under a `problem/` pipeline, the ones this gate reads — falling into eight
            # distinct lost-name sets, three of which include a `problem/` file. (The first
            # correction wrote "eight distinct sets, seven of them problem/-domain", which
            # attaches a file count to sets; per set the figure is three. Re-measured both
            # ways rather than re-worded.) Names lost include `dp`, `ncomp` and
            # `shallow_water2d__g_const`. Skipping a
            # statement KIND is not the forbidden operation: nothing is removed from the
            # disqualifying set, and a name genuinely declared elsewhere still reaches it
            # from its own declaration.
            if "import" in attribute_tokens or attribute_tokens <= {"public", "private"}:
                continue
            target = (
                constants
                if attribute_tokens & {"parameter", "enumerator"} and not construct_depth
                else others
            )
            for item in fortran_lines.split_top_level_commas(entities):
                match = IDENTIFIER_PATTERN.match(item.strip())
                if match is not None:
                    target.add(match.group(0))
            continue
        if _FORTRAN_ATTRIBUTE_STATEMENT.match(statement):
            others.update(extract_identifiers(statement))
            continue
        # `associate (c0 => scratch)` and `select type (c0 => x)` REBIND a name to a definable
        # variable for the length of the construct, so the name is shadowed exactly as a local
        # declaration shadows — and nothing declares it, so only this reaches it.
        associate_match = _FORTRAN_ASSOCIATE_OPEN.match(statement)
        if associate_match is not None:
            inner = extract_balanced_parens(statement, statement.index("(", associate_match.end() - 1))
            for item in fortran_lines.split_top_level_commas(inner):
                name, sep, _target = item.partition("=>")
                if not sep:
                    continue
                match = IDENTIFIER_PATTERN.match(name.strip())
                if match is not None:
                    others.add(match.group(0))
            continue
        # The `::` is optional when a declaration carries no attribute and no initializer, and
        # `integer ncomp` shadows a host constant exactly as `integer :: ncomp` does — missing it
        # left the subtraction one spelling away from the hole it exists to close. `enumerator
        # red, green` is the same omission on the constant side.
        entity_match = _FORTRAN_BARE_DECLARATION.match(statement)
        if entity_match is not None:
            target = (
                constants
                if entity_match.group(1) == "enumerator" and not construct_depth
                else others
            )
            tail = statement[entity_match.end() :].lstrip()
            if tail.startswith("("):
                tail = tail[len(extract_balanced_parens(tail, 0)) + 2 :]
            elif tail.startswith("*"):
                # `character*3 tag` — the obsolescent length selector. `-std=f2008` rejects
                # `integer*4` but accepts this one, and leaving it unparsed meant the declaration
                # did not disqualify the name.
                tail = tail[1:].lstrip()
                length = re.match(r"\(|\d+", tail)
                if length is None:
                    continue
                tail = (
                    tail[len(extract_balanced_parens(tail, 0)) + 2 :]
                    if length.group(0) == "("
                    else tail[length.end() :]
                )
            # `integer function f(x)` IS a declaration — of the function result, which is
            # definable inside the function. Skipping the statement let a file that declares
            # `parameter :: ncomp` in one place and `integer function ncomp(x)` in another exempt
            # a definable name; gfortran accepts that, the two being different scoping units.
            # The result name is taken from the header, and from a `result(...)` clause when one
            # renames it. (The test is `function` followed by a SPACE: `real function_tmp` is an
            # ordinary declaration, and matching by prefix discarded the whole statement — losing
            # every other name on it too.)
            if re.match(r"function\s", tail):
                for pattern in (r"function\s+([a-z_][a-z0-9_]*)", r"result\s*\(\s*([a-z_][a-z0-9_]*)"):
                    header_match = re.search(pattern, tail)
                    if header_match is not None:
                        others.add(header_match.group(1))
                continue
            for item in fortran_lines.split_top_level_commas(tail):
                match = IDENTIFIER_PATTERN.match(item.strip())
                if match is not None:
                    target.add(match.group(0))
            continue
        statement_match = _FORTRAN_PARAMETER_STATEMENT_PATTERN.match(statement)
        if statement_match is None:
            continue
        open_index = statement_match.end() - 1
        inner = extract_balanced_parens(statement, open_index)
        # Nothing may follow the closing paren — which is what rejects `parameter(scratch) =
        # u_in(1)`, an assignment into an array a leaf named `parameter`, since `parameter` is not
        # reserved. A second requirement stood here (every item must carry an `=`); it was removed
        # once a mutation run showed EITHER check alone rejects that case and no legal Fortran
        # exists that only the second catches. Two guards where one suffices is a defence that
        # cannot fail, which is indistinguishable from a defence that does not work.
        if statement[open_index + len(inner) + 2 :].strip():
            continue
        items = fortran_lines.split_top_level_commas(inner)
        if not items:
            continue
        for item in items:
            match = IDENTIFIER_PATTERN.match(item.strip())
            if match is not None:
                constants.add(match.group(0))
    return constants, others



def split_names(raw: str) -> list[str]:
    """The bare identifiers of a comma-separated Fortran list (argument list, `intent(out)`
    entity list, call actuals) — non-identifier items are dropped, not reported.

    Three `Generate.static` gates consume it: `_validate_problem_model_literal_outputs`,
    `_validate_problem_model_dependency_dataflow` and `_validate_problem_metric_only_scalar_kernel`.
    The reproducers below are all at the dataflow gate, which is the one that reads `call`
    actuals and so sees the widest input.

    Unlike this module's other splitter callers, `raw` may arrive as RAW source text: the
    enclosing regexes are `re.DOTALL` over the whole file, so a continued argument list can reach
    here with its `&`, its newlines and its `!` comments intact. It is therefore reduced first by
    `joined_masked_view` — the shared view, not a private copy — and only then split by
    `fortran_lines.split_top_level_commas`. Applying the view here is redundant for the three
    gates below, which now hand over fragments of a view they already built, and the view is a
    fixed point so that costs nothing; it is kept because making this helper's correctness depend
    on the caller having remembered is the exact coupling that produced this defect class. The
    mask half of the view answers three defects:

    * **Comments (pre-existing).** A comma inside a comment manufactured a PHANTOM identifier:
      `call flux__apply(h_in, & ! set a, mid, b` yielded `mid`. The phantom lands in
      `dep_output_candidates`, meets the backward assignment closure, and the `isdisjoint` test
      stops firing — a real "dependency output never reaches intent(out)" violation suppressed
      by the text of a comment. Fail-open.
    * **Character literals (the same phantom by another route).** `call flux__log('recompute a,
      mid, now')` yielded `mid`, back when this function hand-rolled a quote-blind fourth copy
      of the splitter. Fail-open.
    * **Apostrophes in comments.** Once the split became quote-aware, a `! it's` in a continued
      argument list opened a literal that no newline closed, swallowing every later item — the
      dependency-call output lost (fail-open) or a dummy lost out of `arg_names` (fail-closed),
      depending on which list carried the comment. Masking removes the apostrophe with its
      comment before the splitter can see it.

    All three are one root cause: raw multi-line text fed to a single-logical-line helper.

    `&` continuations are the fourth instance of that same root cause, and the view closes them
    too. A mask that blanks in place cannot: the first name after each `&` still carries the
    marker and the newline, is not an identifier, and was dropped — a wrapped argument list lost
    one name per continuation, from EVERY feed at once (a lost dummy wrongly became a
    dependency-output candidate, a lost actual lost a real candidate, a lost `intent(out)` name
    shrank the closure seed). Recovering them is the correct parse, and it was measured on every
    `*_model.f90` in the tree before being taken — dependency ids read from each file's own
    `use <spec_id>_model` lines and `node_key` forced to `problem/`, without which the absolute
    figures are not reproducible: 29 violations before, 27 after. Recovering the names is
    safe only together with the candidate rule's named-constant clause, which had to land in the
    same change: joined WITHOUT that clause the count goes to 31, because the byte-identical
    `shallow_water2d` pair under `workspace_20260706` was passing on two errors CANCELLING — a
    lost `u_np1` made an `intent(out)` dummy its own candidate, so the disjoint test could not
    fire, while a lost `ncomp` hid the constant that now clears it.

    The end-to-end movement, before to after, is elsewhere and `TODO.md` lists it: two
    `workspace_20260319` artifacts lose a FALSE `initialize_state` violation whose only candidate
    is a module `parameter`; one 20260712 model keeps firing with two names recovered from behind
    a `&`; and two `profile/`-domain files move in opposite directions, neither of which any gate
    here reaches."""
    parts = fortran_lines.split_top_level_commas(joined_masked_view(raw))

    names: list[str] = []
    for token in parts:
        part = token.strip().lower()
        if not part:
            continue
        part = re.sub(r"\(.*\)", "", part).strip()
        if IDENTIFIER_PATTERN.fullmatch(part):
            names.append(part)
    return names



def _is_literal_like_expr(expr: str) -> bool:
    lowered = expr.strip().lower()
    if not lowered:
        return False
    if lowered in {".true.", ".false.", "true", "false"}:
        return True
    return bool(re.fullmatch(r"[0-9dDeE\.\+\-\*\/\(\)\s,_]+", lowered))



# The `intent(out)` declarations of one scope. ONE definition: the three `problem` model gates
# each carried their own copy of this pattern, and each recomputed the same set from the same
# text. The set is computed once, in the envelope, and every gate reads `envelope.out_vars`.
_FORTRAN_INTENT_OUT_PATTERN = re.compile(r"intent\s*\(\s*out\s*\)\s*::\s*([^\n!]+)")



class SourceStructureError(Exception):
    """The front end could not resolve the structure of this model source.

    A CONTENT failure, not a transport one: every measured carrier is a legal program written in
    a form the parser lexes differently from the compiler (a variable NAMED `endsubroutine`), and
    a leaf can rewrite it. It is deliberately NOT a fall-back to a looser reading — a structure
    nothing could resolve is exactly the input a silent gate is made of.
    """

    def __init__(self, errors: tuple[Any, ...]) -> None:
        super().__init__("fortran structure could not be resolved")
        self.errors = errors



@dataclass(frozen=True)
class ProcedureEnvelope:
    """One procedure definition, as the three `problem` model gates read it.

    ``body`` is everything between the header and the terminator, CONTAINED PROCEDURES INCLUDED.
    ``out_scope`` is the part of it before the subroutine's own `contains`, and is where
    ``out_vars`` — the `intent(out)` dummies, the definable outputs every gate keys on — is read
    from: a contained procedure's dummies are its own, not its host's.

    Splitting body from out_scope is what lets a gate answer the question it is actually asking.
    An earlier draft CUT the body at `contains` instead, and that cut was wrong in both directions
    at once, each reproduced against origin/main, which flags neither way because its flat span
    ran through the contained procedure:

    * a `call` inside a contained procedure, with the `intent(out)` in the host, landed in an
      envelope with no `intent(out)` at all, so every gate returned at its empty out-set check —
      fail-OPEN;
    * a dependency result propagated to the host's `intent(out)` INSIDE a contained procedure, by
      host association, was invisible to the host's envelope — a false violation.

    Neither is reachable in this tree today (0 of the 365 `*_model.f90` define a contained
    procedure), which is exactly why the tree differential could not see them.

    ``out_vars`` IS THE DEFINABLE-OUTPUT SET, and what belongs in it depends on ``kind``:

    * a `subroutine`'s outputs are its `intent(out)` dummies, and nothing else;
    * a `function`'s outputs are those PLUS its result variable — `result(y)` names it `y`, and
      without a `result(...)` it is the function's own name. That is Fortran's rule and it was
      re-checked against the compiler rather than the standard: with `result(y)` present,
      `gfortran -fsyntax-only -std=f2008` rejects an assignment to the function name with "'f' at
      (1) is not a variable", and accepts one to `y`; with no `result(...)` it accepts the
      assignment to the function name (both executed).

    Every gate keys on this ONE set rather than re-deriving "what counts as an output" three
    times, which is how a function stayed invisible to all three of them for as long as it did.

    ``intent_out_vars`` is the `intent(out)` dummies ALONE — the same set minus the result
    variable. The distinction is not a nicety: `_validate_problem_model_literal_outputs` reads
    ONLY this set, because a result variable in its conjunction is wrong in both directions — as
    a trigger it produced ten false violations against this tree and caught nothing, and as a
    member it EXEMPTED any function whose result happened to be input-dependent, which turned
    rewriting a subroutine as a function into a way to launder a fabricated `intent(out)`.

    HOW FAR THE FUNCTION WIDENING ACTUALLY REACHES IN THIS TREE, measured rather than implied,
    and corrected once by review after a first version overstated it: of the 422 function
    envelopes in the 365 in-tree `*_model.f90`, **0 declare an `intent(out)` dummy**, **none
    carries five outputs**, and **none contains a dependency-operation call** (87 of them live in
    files that have dependencies at all; the 124 procedures that do call one are all subroutines).
    So the corpus COVERAGE added by this widening is zero in all three gates, not two — the +422
    is a visibility number, and the only exercise of the new path is the synthetic reproduction
    and the tests. The widening is prospective: it closes a shape a future model can take, and it
    is the shape this item's own reproduction used. What it is NOT is a claim that anything in the
    tree today is newly checked.
    """

    kind: str
    name: str
    dummy_args: str
    body: str
    out_scope: str
    result_name: str | None
    intent_out_vars: frozenset[str]
    out_vars: frozenset[str]



def _fortran_view_pair(lowered: str) -> tuple[str, str, list[int], list[int]]:
    """The gate view and a LABEL-PRESERVING twin of it, line for line.

    `joined_masked_view` strips a leading statement label, because every rule that reads
    the view anchors on the statement's own keyword and a label sitting in front of it would have
    to be guarded for in each rule. That is right for the RULES and wrong for a PARSER: in F2008's
    obsolescent labelled `DO`, the label IS the loop's terminator, so stripping it leaves
    `do 100 i = 1, 4` with nothing closing it. tree-sitter then reports ERROR and the source is
    refused — a source `gfortran -fsyntax-only -std=f2008 -Wall` accepts with no diagnostic at
    all, and one `origin/main`'s regex walk analysed correctly. Found by review; 0 of the 365
    in-tree `*_model.f90` carry a statement label of any kind, which is why no differential could
    see it.

    Both strings are built from ONE pass over the same statements, so line i of one is line i of
    the other by construction rather than by a length coincidence. Every offset this module hands
    a gate is a LINE START, so the parse offsets are translated by line index and stay exact.
    """
    raw_statements = [
        statement.lstrip()
        for _lineno, line in fortran_lines.fortran_logical_lines(lowered)
        for statement in fortran_lines.split_fortran_statements(line)
    ]
    stripped_lines = [
        _FORTRAN_STATEMENT_LABEL.sub("", statement, count=1) for statement in raw_statements
    ]
    # EVERY label is kept in the twin. WHICH reading to parse is not decided here at all — see
    # `structure_reading`, which asks the parser both ways (it was
    # `procedure_envelopes` until that decision was extracted so a second structural
    # question could share it; this pointer went one frame stale in the same commit). Three versions of a rule
    # that tried to decide it HERE were each defeated by a legal program, in three consecutive
    # review rounds: keep every label (breaks `10 contains` / `20 subroutine helper(v)`, which
    # tree-sitter cannot parse and gfortran accepts); keep any label a `do` names file-wide (a
    # label is unique only within a scoping unit, so `do 100` in one procedure resurrected the
    # label onto a `100 contains` in another); keep the label terminating an OPEN `do`, matched by
    # value (misses a labelled `FORMAT` in the specification part, which needs its label and has
    # no `do` naming it). The set of constructs whose label a parser needs is not closed by
    # enumeration — which is the same argument this module makes for replacing the regex walk, and
    # it applies to the rule standing in front of the parser too.
    labelled_lines = raw_statements

    def finish(lines: list[str], keep: list[bool] | None) -> tuple[str, list[bool]]:
        masked = fortran_lines.mask_code_lookalikes("\n".join(lines)).split("\n")
        rstripped = [line.rstrip() for line in masked]
        if keep is None:
            keep = [bool(line) for line in rstripped]
        return "\n".join(line for line, wanted in zip(rstripped, keep) if wanted), keep

    # The KEEP decision is the stripped view's, applied to both: dropping a line from one and not
    # the other is the only way this pairing can come apart, and a label-only "statement" (which
    # is not legal Fortran anyway) is exactly the input that would do it.
    view, keep = finish(stripped_lines, None)
    labelled_view, _ = finish(labelled_lines, keep)
    return view, labelled_view, _line_starts(view), _line_starts(labelled_view)



def _line_starts(text: str) -> list[int]:
    starts = [0]
    for index, character in enumerate(text):
        if character == "\n":
            starts.append(index + 1)
    return starts



def structure_reading(
    lowered: str,
) -> tuple[str, fortran_structure.StructureTree, Callable[[int], int]]:
    """The ONE structural reading of a source: `(view, tree, to_view)`.

    Extracted from `procedure_envelopes` when `_validate_generated_signatures` needed a
    second structural question answered (`module_level_procedure_names`). It is shared rather
    than copied because the reading below is not a rule anyone should re-derive: a second reading
    that chose differently would answer the two questions about DIFFERENT programs, and this
    module's whole reason for asking a parser is that hand-written agreement about source
    structure does not hold.

    TWO READINGS OF ONE PROGRAM, and the PARSER picks. A statement label is inert to every rule in
    this module — which is why the view strips labels, so each rule can anchor on the statement's
    own keyword — but it is not inert to a parser. Neither reading parses everything, and both
    shapes that defeat one are accepted by the compiler (executed). So the reading is not chosen
    by a rule about labels: three such rules were written and each was defeated by a legal program
    in the next review round. The stripped view is tried first because it is the common case (365
    of the 365 in-tree models carry no label at all, and it needs no offset translation); the
    label-preserving twin is tried only when that fails. A source is refused only when NEITHER
    reading resolves, which is strictly weaker than either rule and needs no enumeration to stay
    true.

    Raises `SourceStructureError` when neither reading resolves. A
    `FortranStructureUnavailableError` from the front end itself propagates untouched — it is the
    operator's failure, and `main` answers it with a dedicated exit code."""
    view, labelled_view, view_starts, labelled_starts = _fortran_view_pair(lowered)
    tree = fortran_structure.parse_view(view)
    translate = False
    if tree.errors:
        labelled_tree = fortran_structure.parse_view(labelled_view)
        if labelled_tree.errors:
            # The STRIPPED reading's errors are reported: it is the canonical view, the one whose
            # line numbers the rest of this module speaks in.
            raise SourceStructureError(tree.errors)
        tree, translate = labelled_tree, True

    def to_view(offset: int) -> int:
        if not translate:
            return offset
        # END OF INPUT IS NOT A LINE START, and it is the one offset that is not: the view never
        # ends in a newline, so `fortran_structure._next_line_start` answers `len(text)` for a
        # span reaching EOF. Mapping that by line index would silently shrink it to the start of
        # the last line. Reachability today is zero — every other offset is a line start, checked
        # over all 365 in-tree models and the hand-built shapes — so this is a guard on the
        # premise, not a fix for an observed defect (review).
        if offset >= len(labelled_view):
            return len(view)
        index = bisect.bisect_right(labelled_starts, offset) - 1
        if index >= len(view_starts):
            return len(view)
        return view_starts[index]

    return view, tree, to_view



def module_level_procedure_names(
    lowered: str, unit_name: str | None = None
) -> frozenset[str]:
    """The names ``lowered`` DEFINES at the top level of the program unit ``unit_name`` — the
    definedness half of the published surface, asked of the language backend.

    The rule this serves is neutral: a published operation must be IMPLEMENTED BY THE UNIT THAT
    PUBLISHES IT, not merely declared, and not implemented by some other unit that happens to
    share the file. Which spellings declare without implementing, which unit counts as the same
    publisher, and why a caller must not decide either itself, belong to the backend —
    `structure.module_level_procedure_names` is canonical for all three.

    ``unit_name`` is the published unit's own name, which this repository fixes by convention as
    the model source's basename with its extension dropped. Measured over the 30 certified
    `component` / `infrastructure` sources in `workspace/pipelines` (2026-09-05): every one
    declares exactly one program unit, and its name is exactly that.
    Passing None asks the unscoped question and is answered by the backend, not here.

    Raises the same two errors as `structure_reading`."""
    _view, tree, _to_view = structure_reading(lowered)
    return fortran_structure.module_level_procedure_names(tree, unit_name)



def module_level_definition_headers(
    lowered: str, unit_name: str
) -> dict[str, tuple[str, ...] | None]:
    """The stanza of each procedure ``lowered`` DEFINES at the top level of ``unit_name``, read
    from the definition the structure reader found — the header the §5.1 comparison must read.

    The reading is this module's (`structure_reading`, which picks the stripped or the
    label-preserving view); what counts as a definition's header and specification part is the
    backend's, and `structure.module_level_definition_stanzas` is canonical for it and for why
    the whole-file stanza splitter's answer is the wrong one. Raises the same two errors as
    `structure_reading`."""
    view, tree, to_view = structure_reading(lowered)
    return fortran_structure.module_level_definition_stanzas(
        tree, unit_name, lambda start, stop: view[to_view(start):to_view(stop)])



def procedure_envelopes(lowered: str) -> list[ProcedureEnvelope]:
    """Every procedure DEFINITION in ``lowered``, with the body each gate must read.

    The structure comes from `tools/backends/language/fortran/structure.parse_view` (tree-sitter-fortran), NOT from
    a hand-rolled scan of Fortran's keyword structure. The scan this replaces was rewritten four
    times and broken sixteen, always the same way: a spelling the language allows and the rules
    did not enumerate — the one-word `endsubroutine`, a bare `end`, a construct NAMED after a
    keyword, a VARIABLE named after a keyword, an `interface` body's own `end subroutine`. Each
    was fail-OPEN and silenced all three gates for a whole file. That set is not closed by
    enumeration, so the enumeration is gone.

    What is handed to the parser is `joined_masked_view(lowered)` — applied here, not by
    the caller, because the view is a fixed point, so a gate that has already reduced its text
    pays one idempotent pass and a test may hand over raw source (the argument
    `split_names` already makes).

    THE ONE INVARIANT A REWRITE MUST NOT BREAK: `body` is ONE CONTIGUOUS SLICE of a
    length-preserving transform of the view, so a position inside it is a position inside the
    view. `_validate_problem_model_dependency_dataflow` decides "was this actual assigned BEFORE
    the call" by comparing an `assignment_records` position with an `iter_calls`
    position, both taken inside `body`, and `joined_masked_view`'s own contract is that
    offsets are comparable only when both come from the same view. An interface span is therefore
    BLANKED IN PLACE rather than deleted, by `fortran_structure.blank_interface_spans`, after the
    parse — deletion also preserves the ORDER of what remains, but not the offsets.

    A parse carrying an ERROR or MISSING node RAISES `SourceStructureError` instead of
    returning a partial list, and the caller turns that into a content violation. There is no
    looser second reading to fall back to, by design. Measured (2026-08-13, tree-sitter 0.26.0 /
    tree-sitter-fortran 0.6.0): over the views of the 365 in-tree `*_model.f90` the parse carries
    no ERROR node at all and this function's output is BYTE-IDENTICAL to the scan it replaces on
    365/365 files and all 894 envelopes; over the 58 inline test fixtures `gfortran -fsyntax-only
    -std=f2008` accepts, 1 carries an ERROR node and 0 disagree.

    A `function` IS emitted, with its result variable in `out_vars` — 92 of the 365 in-tree
    `*_model.f90` define one, and every gate was blind to all of them for the whole procedure.

    An abbreviated `module procedure solve` is emitted too, and is ALWAYS refused by the caller
    rather than analysed. The reason is not that its body is hard to find — the parser reports it
    exactly — but that F2008 forbids such a body from redeclaring its dummies (`gfortran
    -fsyntax-only -std=f2008`: "is a redefinition of the declaration in the corresponding
    interface"), so no `intent(out)` ever appears in it and every gate would return at its empty
    out-set check. Emitting it and letting it through would be a SILENT gate; refusing it asks
    the leaf for the full form (`module subroutine s(u, v)` / `module function f(x) result(y)`),
    which every gate reads. The alternative — resolving the dummies across the
    interface/implementation boundary via the gfortran dump — was measured and dropped: the three
    dump markers that are stable across compiler versions carry neither the result name nor the
    function/subroutine distinction, so it could only ever have rescued a subroutine-shaped one,
    of which this tree has none. See `TODO.md`.
    """
    # The label reading — two readings of one program, chosen by the PARSER — belongs to
    # `structure_reading` and is stated in its docstring, once. It was duplicated here verbatim
    # when that function was extracted, which is two copies of one rationale that can drift.
    view, tree, to_view = structure_reading(lowered)

    spans = tuple((to_view(start), to_view(end)) for start, end in tree.interface_spans)
    blanked = fortran_structure.blank_interface_spans(view, spans)
    envelopes: list[ProcedureEnvelope] = []
    for procedure in tree.procedures:
        body_end = to_view(procedure.body_end)
        body_start = to_view(procedure.body_start)
        out_end = body_end if procedure.contains_at is None else min(
            to_view(procedure.contains_at), body_end
        )
        out_scope = blanked[body_start : max(out_end, body_start)]
        out_vars: set[str] = set()
        for match in _FORTRAN_INTENT_OUT_PATTERN.finditer(out_scope):
            out_vars.update(split_names(match.group(1)))
        # The result variable is definable and is the whole point of a function, so it joins the
        # out-set — see the envelope's docstring for the compiler check that says WHICH name it is.
        intent_out_vars = frozenset(out_vars)
        if procedure.kind == "function" and procedure.result_name:
            out_vars.add(procedure.result_name)
        envelopes.append(
            ProcedureEnvelope(
                kind=procedure.kind,
                name=procedure.name,
                dummy_args=procedure.dummy_args_text,
                body=blanked[body_start:body_end],
                out_scope=out_scope,
                result_name=procedure.result_name,
                intent_out_vars=intent_out_vars,
                out_vars=frozenset(out_vars),
            )
        )
    return envelopes



# The `problem` model gates match Fortran's KEYWORD STRUCTURE, so the text they read is
# reduced by `joined_masked_view` — inside `structure_reading`, which
# `procedure_envelopes` calls and where every one of them now gets its bodies from. (Two of the three called the view themselves
# as well until review pointed out that the walk had made those calls no-ops, while the comments
# beside them still called them load-bearing. The dependency-dataflow gate's own call stayed: it
# feeds `declared_names` directly.) Without the mask half, body selection stops at the
# first TEXTUAL `end subroutine`: one comment naming it truncates the body, every gate that reads
# the body goes silent, and a legal model passes — fail-open from a comment. The mirror is a
# commented-out procedure minting a phantom match, which fails closed. Without the join half, a
# wrapped `intent(out)` list or `call` actual list is read as fragments.
#
# `call` positions stay comparable with assignment positions because BOTH are offsets into the
# same view, not because the view preserves the file's offsets — it does not (see the view's
# docstring). Nothing here reports a line number; anything that ever does must take it from
# `fortran_lines.fortran_logical_lines`' `start_lineno`.

def _validate_problem_model_literal_outputs(
    node_key: str,
    model_file: Path,
    envelopes: list[ProcedureEnvelope],
    violations: list[str],
) -> None:
    if not node_key.startswith("problem/"):
        return

    for envelope in envelopes:
        sub_name = envelope.name
        arg_names = set(split_names(envelope.dummy_args))
        body = envelope.body

        # THIS GATE READS THE `intent(out)` DUMMIES AND NOTHING ELSE — not `out_vars`, which for
        # a function also holds its result variable. Both halves of that are measurements.
        #
        # It must not FIRE on a result: "every output is a literal and none depends on an input"
        # is evidence of fabrication for a subroutine, but for a function it is the normal shape
        # of a constant accessor (`ghost_width() result(ng); ng = 1`) and of a predicate whose
        # input dependence lives in its BRANCH CONDITIONS rather than in the assigned expression
        # (`res = .false.` … `if (lhs(k) < rhs(k)) res = .true.`), which this gate's
        # flow-insensitive `input_dependent` test cannot see. Executed against all 365 in-tree
        # models: opening the gate on the result alone flags 10 functions, 10 of them legitimate,
        # 0 defects.
        #
        # It must not be EXEMPTED by one either, which is the half review had to point out. The
        # test is a conjunction over the whole output set, so an extra output that IS
        # input-dependent exempts the procedure — and a function's result is an extra output every
        # function has for free. That made rewriting a flagged subroutine as a function a way to
        # launder it: `v = 1.0` with `intent(out) :: v` is flagged as a subroutine, and adding
        # `r = u(1)` to the function form silenced it (both accepted by `gfortran -fsyntax-only
        # -std=f2008`, both executed). A subroutine has to DECLARE an extra `intent(out)` to buy
        # that exemption; a function was getting it by existing.
        out_vars = set(envelope.intent_out_vars)
        if not out_vars:
            continue

        assign_map: dict[str, list[str]] = {}
        for out_var in sorted(out_vars):
            exprs = re.findall(
                rf"\b{re.escape(out_var)}\s*=\s*([^\n!]+)",
                body,
            )
            if exprs:
                assign_map[out_var] = [expr.strip() for expr in exprs]

        if set(assign_map.keys()) != out_vars:
            continue

        all_literal = True
        input_dependent = False
        for out_var, exprs in assign_map.items():
            for expr in exprs:
                if not _is_literal_like_expr(expr):
                    all_literal = False
                    expr_ids = {
                        token
                        for token in IDENTIFIER_PATTERN.findall(expr)
                        if token not in {"d", "e", "true", "false"}
                    }
                    if expr_ids & (arg_names - {out_var}):
                        input_dependent = True

        if all_literal and not input_dependent:
            # The subroutine wording is UNCHANGED, deliberately: it is what the 365-file
            # differential compares against, so every line that moves when functions become
            # visible is a NEW line about a function and attributable as one.
            # One wording for both kinds, and it names `intent(out)` because that is now exactly
            # what was judged. The subroutine text is unchanged byte for byte: it is what the
            # 365-file differential compares against.
            violations.append(
                f"{model_file}: {envelope.kind} {sub_name} has literal-only assignments for "
                f"all intent(out) vars"
            )



def extract_identifiers(expr: str) -> set[str]:
    return {
        token
        for token in IDENTIFIER_PATTERN.findall(expr.lower())
        if token not in KEYWORDS
    }



def assignment_records(body: str) -> list[tuple[str, set[str], int]]:
    records: list[tuple[str, set[str], int]] = []
    assign_pattern = re.compile(
        r"^\s*([a-z_][a-z0-9_]*(?:\s*\([^\n=]*\))?)\s*=\s*([^\n!]+)",
        re.MULTILINE,
    )
    for match in assign_pattern.finditer(body):
        lhs_expr = match.group(1)
        lhs_match = IDENTIFIER_PATTERN.search(lhs_expr.lower())
        if lhs_match is None:
            continue
        lhs = lhs_match.group(0)
        rhs_ids = extract_identifiers(match.group(2))
        records.append((lhs, rhs_ids, match.start()))
    return records



def _validate_problem_model_dependency_dataflow(
    node_key: str,
    model_file: Path,
    lowered: str,
    envelopes: list[ProcedureEnvelope],
    dep_spec_ids: list[str],
    violations: list[str],
) -> None:
    """`Generate.static` reachability lint: a `problem` subroutine that calls a dependency
    operation must let that operation's RESULT flow to an `intent(out)` dummy. The check is a
    backward ASSIGNMENT closure from the intent(out) vars: an assignment `lhs = f(rhs...)` makes
    every `rhs` a source of `lhs`, transitively; a dependency-call output not assigned (directly or
    through the chain) into any intent(out) is flagged (the inert-call / discarded-result defect).

    Scope note — this gate does NOT deterministically check the stronger
    ``semantic_dependency.required_sources`` reachability. That property (each intent(out)'s
    expression tree reaches the required physical inputs) is inherently flow-sensitive and
    argument-intent-dependent: whether a value passed to a `call` is read or written, and which of
    several writes to a reused scratch variable reaches a use, cannot be decided from this
    regex-level, flow-insensitive view without the callee interfaces. Every flow-insensitive
    approximation attempted here either false-rejected physically-correct code (a dependency result
    reaching intent(out) through a call chain) or failed open (a required source merely co-passed to
    an unrelated call, or fed to a call whose write is later overwritten). It is therefore left to
    ``Generate.verify`` G5, which reads ``controlled_spec.md`` and IS the semantic authority for
    "each intent(out) reaches the required_sources", backed by the runtime. Check 1 below (a
    dependency RESULT reaching intent(out)) is assignment-only, sound, and kept.

    The candidate rule has four clauses: an actual of a dependency `call` is a candidate OUTPUT
    unless it is a dummy of the enclosing subroutine, OR was assigned earlier in the same view, OR
    is a NAMED CONSTANT, OR is the name of a procedure this file defines (a procedure passed as
    the actual for a procedure-typed dummy — issue #266; a procedure name is not definable
    either). The third clause is not a relaxation of a sound rule — it removes an
    over-approximation the rule never intended. F2008 requires the actual associated with an
    `intent(out)` / `intent(inout)` dummy to be definable, and a named constant is not, so a
    `parameter` can never be a dependency-call output. Verified against the compiler rather than
    inferred from the standard: `gfortran -fsyntax-only -std=f2008` rejects both spellings with
    "Non-variable expression in variable definition context (actual argument to INTENT =
    OUT/INOUT)".

    The clause is deliberately FILE-WIDE AND SCOPE-FREE: a name is exempt only if this file
    declares it as a constant and never declares it as anything else. That is not the precise
    rule — the precise rule is Fortran's own name resolution — and it is not an approximation of
    it either; it is the strictly weaker question that can be answered here. Six rounds of review
    established why: a scoped version was written four times and defeated by every one of these — a
    `parameter` inside a module `function`, a paren-less `subroutine`, an external procedure, a
    submodule separate body, a `block`, a named `associate`, a contained procedure, a second
    module in the same file, a derived type's own `contains`, a statement label, an obsolescent
    `character*3` selector, three spellings of an assignment to a variable named `module`, three
    spellings of `use` association (`only:`, a bare rename, and the `, non_intrinsic ::` prefix),
    and the one-word `selecttype` whose blank F2008 makes optional. Every one was a way for a name
    to be constant SOMEWHERE and definable HERE, and each
    miss produced a SILENT gate, because this set subtracts candidates. The set of mechanisms that
    make a Fortran name visible is not closed at this level of parsing, so a rule whose safety
    depends on enumerating them cannot be made safe by adding cases. Note that three of the
    sixteen were found AFTER the switch to this rule, in the recognition of "declared otherwise" —
    the rule moves the risk from an unbounded set to a bounded one, it does not remove it.

    Asking the file-wide question instead makes the failure direction structural rather than
    hoped-for: a name used both ways anywhere in the file is simply not exempt, which costs a
    false violation. Every one of the thirteen reproductions is still caught, because in each the
    name is also declared as a variable, imported, or `associate`-bound somewhere.

    RESIDUES, reproduced and NOT fixed. (a) A bare `use other_module` can import a VARIABLE whose
    name this file also declares as a constant; nothing in one file can see that. (b) An
    implicitly typed local is not declared at all, so it cannot disqualify a name. An earlier
    version of this note called that unreachable because the lint gate (fortitude C001)
    requires the module-level `implicit none`; a round-2 reviewer of issue #266 PR-2 measured
    that a routine-local `implicit real(dp) (s)` under it compiles under the gate's standard
    and passes the declared lint set, so a local named after a constant or a procedure
    elsewhere in the file can be an undeclared, exempted, discarded output — reachable, zero
    in the corpus, and the same class as (a). (C003 is a different rule and, since issue
    #111, not in the gate's declared rule set.) (c) The candidate
    rule still treats a variable written by an earlier `call` as a candidate, and the consumption
    closure still cannot cross a call; the measured instance is the
    `dynamics_shallow_water_profile_2d_rusanov_p0_ssprk2` model, latent only because of the
    `problem/` guard below. Closing (c) is the flow-sensitive pass recorded in `TODO.md`.

    Residue (c) above is the one with a named instance: the
    `dynamics_shallow_water_profile_2d_rusanov_p0_ssprk2` model yields
    `candidates=['flux_ok', 'u_new', 'z_b']`. The three are NOT the same defect, and an earlier
    draft of this note misattributed all of them to the producer side: `z_b` and `flux_ok` are the
    producer-side residue (written by an earlier `call`, which the candidate rule does not count
    as a producer), while `u_new` is written by the dependency call itself — a correct candidate
    whose flag is consumption-side, because `u = u_new` then crosses a `call` the assignment-only
    closure cannot follow. Whoever builds the flow-sensitive pass needs both halves, so `TODO.md`
    carries the case with that split."""
    if not node_key.startswith("problem/"):
        return
    if not dep_spec_ids:
        return

    dep_prefixes = tuple(f"{spec_id.lower()}__" for spec_id in dep_spec_ids)

    # This gate keeps its own view, and it is the ONLY one of the three that still needs one:
    # `declared_names` below reads `lowered` directly. The envelopes it is handed were
    # built from the view of this same text, and the view is a fixed point, so the two readings
    # agree whether the caller passed raw source or a view.
    lowered = joined_masked_view(lowered)

    # Which names are named constants EVERYWHERE they appear in this file. Deliberately a
    # whole-file question rather than a scoped one: four rounds of review found thirteen ways to
    # get Fortran's scoping wrong at this level — a `parameter` inside a module `function`, a
    # paren-less `subroutine`, an external procedure, a submodule separate body, a `block`, an
    # `associate` rebinding, a second module in the same file, a derived type's own `contains`,
    # a statement label, and three spellings of an assignment to a variable named `module`. Every
    # one of them was a way for a name to be constant SOMEWHERE and definable HERE.
    #
    # This asks the question that has no scope in it: a name is exempt only if the file declares
    # it as a constant and NEVER declares it as anything else. Each of those thirteen
    # reproductions is still caught, because in every one the name is also declared as a variable
    # or bound by an `associate` — that is what made it definable at the call. What it costs is a
    # false violation when one file legitimately uses a name as a constant in one procedure and a
    # variable in another; that is the direction this repository accepts.
    # Read from the view, NOT from the envelope walk's interface-blanked text. Names an interface
    # body declares belong in `file_other_names`, where they can only SUBTRACT an exemption —
    # fail-closed. Feeding this the blanked text would drop them and re-exempt the name, which is
    # fail-OPEN at exactly the candidate rule below. The blanking stays inside the envelopes.
    file_constants, file_other_names = declared_names(lowered)
    parameter_names = file_constants - file_other_names
    # The name of a procedure this file DEFINES — at module level or internal to one —
    # passed as an actual is the procedure itself (a dependency operation taking a
    # procedure-typed dummy, issue #266: the caller hands it the tendency to integrate). Such an
    # actual can carry nothing back, so it is not an output candidate. The envelopes are one
    # per definition, internal ones included (see the envelope class). The SAME file-wide,
    # scope-free rule as the constant clause above, for the same reason: a procedure name is
    # scope-local (an internal procedure of ANOTHER routine, or a module-level procedure a local
    # declaration shadows — both compile), so a name this file also declares as a variable
    # anywhere is NOT exempt. Two round-1 reviewers each built a shape where the plain
    # envelope set hid a discarded result origin/main flagged; subtracting the declared names
    # costs a false violation on a file that uses one name both ways, the direction this
    # gate accepts.
    procedure_names = {envelope.name for envelope in envelopes} - file_other_names

    for envelope in envelopes:
        sub_name = envelope.name
        arg_names = set(split_names(envelope.dummy_args))
        # A function's result variable belongs on the same side as its dummies for the candidate
        # rule: it is this procedure's OWN output face, not a local scratch that a dependency call
        # might have written. Without this, `f = ...` shaped code makes the result name a
        # candidate output of every dependency call it is passed to — a false violation.
        if envelope.kind == "function" and envelope.result_name:
            arg_names.add(envelope.result_name)
        body = envelope.body
        assignments = assignment_records(body)

        # The closure below is SEEDED with this set, so a function's result seeds it too — which
        # is what makes "the dependency result reaches the output" answerable for a function.
        out_vars = set(envelope.out_vars)
        if not out_vars:
            continue

        dep_output_candidates: set[str] = set()
        for callee, args_raw, call_pos in iter_calls(body):
            if not any(callee.startswith(prefix) for prefix in dep_prefixes):
                continue
            call_vars = split_names(args_raw)
            for var in call_vars:
                # A named constant cannot be an output: F2008 requires the actual associated with
                # an `intent(out)`/`intent(inout)` dummy to be definable. See the docstring for
                # the compiler diagnostic this rests on.
                if var in arg_names or var in parameter_names or var in procedure_names:
                    continue
                assigned_before_call = any(
                    lhs == var and pos < call_pos for lhs, _, pos in assignments
                )
                if not assigned_before_call:
                    dep_output_candidates.add(var)

        if not dep_output_candidates:
            continue

        # A dependency RESULT is consumed into output through ASSIGNMENTS (you assign the call's
        # output argument into your state / an intent(out)). Backward-close over assignment RHS
        # only: crossing calls here has no sound flow-insensitive form (see the function
        # docstring) and fails open — `test_discarded_dep_result_flagged_even_when_call_shares_an_input`
        # pins that it must not be done.
        dependency_sources = set(out_vars)
        changed = True
        while changed:
            changed = False
            for lhs, rhs_ids, _ in assignments:
                if lhs not in dependency_sources:
                    continue
                for src in rhs_ids:
                    if src not in dependency_sources:
                        dependency_sources.add(src)
                        changed = True

        if dep_output_candidates.isdisjoint(dependency_sources):
            violations.append(
                f"{model_file}: {envelope.kind} {sub_name} does not propagate dependency "
                f"operation outputs to "
                f"{'intent(out)' if envelope.kind == 'subroutine' else 'definable-output'} "
                f"dataflow (candidates={sorted(dep_output_candidates)})"
            )



def _validate_problem_metric_only_scalar_kernel(
    multidim_spec_id: str | None,
    model_file: Path,
    envelopes: list[ProcedureEnvelope],
    violations: list[str],
) -> None:
    # `multidim_spec_id` is the node's spec_id when it is a multi-dimensional `problem` node and
    # None otherwise: which nodes are multi-dimensional is a fact about the node, not the
    # language, so the validator decides it and hands the answer in.
    if multidim_spec_id is None:
        return
    spec_id = multidim_spec_id

    intent_in_or_inout_array_pattern = re.compile(
        r"intent\s*\(\s*(?:in|inout)\s*\)\s*::\s*[^\n]*\([^)]+\)"
    )
    do_loop_pattern = re.compile(r"^\s*do\s+[a-z_][a-z0-9_]*\s*=", re.MULTILINE)
    forall_pattern = re.compile(r"^\s*forall\s*\(", re.MULTILINE)

    for envelope in envelopes:
        sub_name = envelope.name
        body = envelope.body
        if len(envelope.out_vars) < 5:
            continue

        has_array_inputs = bool(intent_in_or_inout_array_pattern.search(body))
        has_loop = bool(do_loop_pattern.search(body) or forall_pattern.search(body))
        if has_array_inputs or has_loop:
            continue

        violations.append(
            # The subroutine wording is unchanged byte for byte — it is what the 365-file
            # differential compares against, and all four of the corpus's metric-only violations
            # are subroutines. Only a function, whose count can include its result, says so.
            f"{model_file}: {envelope.kind} {sub_name} is metric-only scalar kernel for "
            f"{spec_id}; "
            "2d/3d problem model must not derive many intent(out) metrics without array inputs or update loops"
            if envelope.kind == "subroutine"
            else f"{model_file}: function {sub_name} is metric-only scalar kernel for {spec_id}; "
            "2d/3d problem model must not derive many definable outputs (its intent(out) metrics "
            "and its result) without array inputs or update loops"
        )



def iter_calls(text: str) -> list[tuple[str, str, int]]:
    """Every `call name(...)` in ``text`` as (name, actual-argument text, offset).

    The paren matching is `extract_balanced_parens`, the quote-aware extractor in this module.
    It used to be hand-rolled here and quote-BLIND, which made a paren inside a character-literal
    actual move the depth: `call flux__report('rate )', tmp)` closed the group early and truncated
    the actuals to `'rate `, while `'rate ('` never closed and swallowed the rest of the body.
    Either way `tmp` stopped being a `dep_output_candidates` member and
    `_validate_problem_model_dependency_dataflow` went silent on a real
    "dependency output never reaches intent(out)" violation — fail-open, from the text of a
    diagnostic string. Same defect class as the comma splitter, on the other half of the shape:
    duplicated Fortran text scanning, the copy blind to quotes."""
    calls: list[tuple[str, str, int]] = []
    call_start_pattern = re.compile(r"\bcall\s+([a-z_][a-z0-9_]*)\s*\(")
    for match in call_start_pattern.finditer(text):
        name = match.group(1).lower()
        start = match.start()
        open_pos = match.end() - 1
        calls.append((name, extract_balanced_parens(text, open_pos), start))
    return calls



def source_views(src_files: list[Path]) -> dict[Path, str]:
    """Each source read once and reduced to its `joined_masked_view`.

    The two readers below need the same view of the same files, and the view is the expensive
    part. Sharing it HALVES the cost; it does not remove it. The multiplier over a raw
    read-and-lower is 43x-86x per shared view across six real three-source directories (so
    86x-172x if both readers built their own) — quoted as a RANGE because two earlier drafts of
    this note each gave a single directory's figure and neither reproduced for the other reader.
    The number that does not move is the absolute: 1.9s for the whole tree of 357 directories,
    against 0.02s of raw reads. Large multiplier, small absolute — a note rather than a concern,
    and the multiplier is what to check first if that stops being true."""
    return {
        src_file: joined_masked_view(
            src_file.read_text(encoding="utf-8", errors="ignore").lower()
        )
        for src_file in src_files
    }



def local_module_map(src_views: dict[Path, str]) -> dict[str, str]:
    """Which source stem provides each locally-defined module.

    Reads `joined_masked_view`s, like its consumer below, because a `module &` / `name`
    wrap would otherwise leave the module unregistered — and an unregistered provider silently
    deletes every dependency edge into it. Masking is not what closes that (the pattern is
    `^`-anchored and comments are already gone by then); the reason to take the shared view rather
    than call `fortran_logical_lines` here is that this defect class IS "N slightly different
    Fortran views", and a fourth hand-rolled one is how it comes back."""
    module_map: dict[str, str] = {}
    pattern = re.compile(r"^\s*module\s+(?!procedure\b)([a-z_][a-z0-9_]*)\b", re.MULTILINE)
    for src_file, text in src_views.items():
        stem = src_file.stem.lower()
        for match in pattern.finditer(text):
            module_name = match.group(1)
            module_map.setdefault(module_name, stem)
    return module_map



def source_module_deps(src_files: list[Path]) -> dict[str, set[str]]:
    """The local module-dependency edges between ``src_files``, by source stem.

    The `^\\s*use` pattern is anchored at a LOGICAL line start, so it must read
    `joined_masked_view`: a continuation placed between `use` and the module name
    (`use &` / `dep_model`) matched nothing on physical lines, which emptied the caller's
    `required_object_deps` — and the make backend's `validate_src_dir` returns before it can
    require a Makefile at all when that mapping is empty. One wrapped `use` therefore switched
    off the module-dependency build contract for the whole directory. Fail-open."""
    src_views = source_views(src_files)
    module_map = local_module_map(src_views)
    use_pattern = re.compile(
        r"^\s*use(?:\s*,\s*(?:intrinsic|non_intrinsic)\s*::|\s*::|\s+)?\s*([a-z_][a-z0-9_]*)\b",
        re.MULTILINE,
    )
    deps_by_stem: dict[str, set[str]] = {}
    for src_file, text in src_views.items():
        stem = src_file.stem.lower()
        deps: set[str] = set()
        for match in use_pattern.finditer(text):
            used_module = match.group(1)
            provider_stem = module_map.get(used_module)
            if provider_stem is None or provider_stem == stem:
                continue
            deps.add(provider_stem)
        deps_by_stem[stem] = deps
    return deps_by_stem



# Edit descriptors may carry a leading repeat count (e.g. ``2l1`` / ``3f0.6``);
# the negative lookbehind excludes letters so multi-letter descriptors such as
# ``tl`` (tab-left) are not mistaken for an ``L`` (logical) descriptor.
_RUNNER_FORMAT_LOGICAL_DESC = re.compile(r"(?<![a-z])l\d*(?![a-z])")



# ``f0`` is the F descriptor with width 0; it may be preceded by a repeat count
# (``2f0.6``) or a ``P`` scale factor (``1pf0.6``). The lookbehind excludes every
# letter *except* ``p`` so a ``P`` scale factor is allowed while ``f0`` embedded
# in a word (e.g. ``leaf0``) is not matched.
_RUNNER_FORMAT_F0_DESC = re.compile(r"(?<![a-oq-z])f0(?:\.\d+)?")



# Statement recognizers (operate on lowercased logical lines). ``write`` must be a
# statement keyword followed by ``(`` (not a substring of an identifier such as
# ``write_flag`` / ``rewrite``). A FORMAT statement carries a leading label.
_RUNNER_WRITE_STMT = re.compile(r"(?<![a-z0-9_])write\s*\(")



_RUNNER_FORMAT_STMT = re.compile(r"^\s*(\d+)\s+format\s*\(")



_RUNNER_CHAR_LITERAL = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"")



_RUNNER_KEYWORD_ITEM = re.compile(r"[a-z][a-z0-9_]*\s*=")



_RUNNER_FMT_KEYWORD = re.compile(r"fmt\s*=\s*(.*)", re.DOTALL)



_RUNNER_NAME_TOKEN = re.compile(r"[a-z][a-z0-9_]*\Z")



# Fortran scoping-unit boundaries. Statement labels, FORMAT statements, and local
# variables are scoped to their program unit, so resolution must not cross units.
_FORTRAN_UNIT_END = re.compile(
    r"^\s*end\s*$|^\s*end\s*(?:program|module|submodule|subroutine|function|blockdata)\b"
)



_FORTRAN_UNIT_OPEN = re.compile(
    r"^\s*(?:(?:pure|elemental|impure|recursive)\s+)*(?:program|subroutine|module|submodule)\b"
)



_FORTRAN_FUNCTION_OPEN = re.compile(r"(?<![a-z0-9_])function\s+[a-z][a-z0-9_]*\s*\(")



def iter_logical_lines(text: str) -> list[tuple[int, str]]:
    """Merge free-form continuation lines and split ``;``-separated statements.

    Returns ``(start_lineno, statement)`` pairs, where ``start_lineno`` is the physical line on
    which the statement began. A logical line carrying multiple ``;``-joined statements is
    expanded into one entry per statement, all sharing that line, in source order — the
    reaching-definition index and the scope assignment both walk this list positionally.
    Indentation is left on: what keeps a ``call`` or ``end subroutine`` line from matching is
    that ``_COMPONENT_PUBLISHED_SUB_RE`` and friends anchor at the START of a whole logical
    line, and the ``\\s*`` in that anchor is there to tolerate the indentation this preserves.

    The scanning itself is ``fortran_lines.fortran_logical_lines`` (issue #23) — one
    implementation shared with ``fortran_lines.fortran_logical_line_texts`` (the §5.1 view) and
    with ``orchestration_runtime``, so a comment, a continuation or a character literal can no
    longer be read one way here and another way there. This adapter adds only the ``;`` split.
    """
    logical: list[tuple[int, str]] = []
    for lineno, joined in fortran_lines.fortran_logical_lines(text):
        for statement in fortran_lines.split_fortran_statements(joined):
            if statement.strip():
                logical.append((lineno, statement))
    return logical



def extract_balanced_parens(text: str, open_index: int) -> str:
    """Return the substring inside the parentheses opening at ``open_index``.

    Parentheses appearing inside single/double quoted strings are ignored so a
    format literal such as ``'(a,l1,a)'`` does not prematurely close the group.

    A newline closes an open literal unless the line continues it, per
    `fortran_lines.continuation_state_after_line` (the shared decision — do not re-derive it
    here; it is a forward fold, so the state is threaded rather than searched for).
    This matters only for `iter_calls`, the one caller that hands over multi-line text;
    the others pass a joined logical line. That text is masked before it gets here, so a
    comment-only line inside a continued literal is visible as one and the skip applies. Both errors were observed silencing the
    dependency-dataflow gate: without the reset an apostrophe in a comment (`! it's`) opens a
    literal nothing closes, and with an unconditional reset a legally continued literal
    (`'rate &` / `&more'`) is cut in half so its closing quote reads as an opening one.
    """
    depth = 0
    in_single = False
    in_double = False
    i = open_index
    n = len(text)
    line_start = text.rfind("\n", 0, open_index) + 1
    continues = False
    while i < n:
        ch = text[i]
        if ch == "\n":
            continues = fortran_lines.continuation_state_after_line(
                text[line_start:i], continues)
            line_start = i + 1
            if not continues:
                in_single = False
                in_double = False
        elif in_single:
            if ch == "'":
                in_single = False
        elif in_double:
            if ch == '"':
                in_double = False
        elif ch == "'":
            in_single = True
        elif ch == '"':
            in_double = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[open_index + 1 : i]
        i += 1
    return text[open_index + 1 :]



def _fortran_literal_value(token: str) -> str:
    """Return the character value of a quoted Fortran literal token.

    Strips the outer quotes and collapses the doubled outer quote escape
    (``''`` -> ``'`` / ``""`` -> ``"``) so embedded string descriptors are left
    with single delimiters.
    """
    quote = token[0]
    inner = token[1:-1]
    return inner.replace(quote * 2, quote)



def _strip_format_char_literals(fmt_value: str) -> str:
    """Remove embedded character-string edit descriptors from a format value.

    A Fortran format may embed literal text via ``'...'`` or ``"..."`` (with the
    delimiter doubled to escape). Such text is *data*, not descriptor codes, so
    substrings like ``L1`` or ``F0`` inside it must not be scanned. Returns the
    format with all embedded character literals removed.
    """
    out: list[str] = []
    i = 0
    n = len(fmt_value)
    while i < n:
        ch = fmt_value[i]
        if ch in "'\"":
            quote = ch
            i += 1
            while i < n:
                if fmt_value[i] == quote:
                    if i + 1 < n and fmt_value[i + 1] == quote:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)



def _scan_runner_format_text(
    runner_file: Path, lineno: int, fmt_value: str, violations: list[str]
) -> None:
    # Blanks are insignificant in a Fortran format spec (outside character
    # literals, already stripped), so remove them before matching ``f 0.6`` etc.
    descriptor_stream = re.sub(r"\s+", "", _strip_format_char_literals(fmt_value))
    if _RUNNER_FORMAT_LOGICAL_DESC.search(descriptor_stream):
        violations.append(
            f"{runner_file}:{lineno}: runner write uses Fortran L edit descriptor "
            f"(emits T/F); JSON boolean must be literal true/false"
        )
    if _RUNNER_FORMAT_F0_DESC.search(descriptor_stream):
        violations.append(
            f"{runner_file}:{lineno}: runner write uses Fortran F0/F0.d descriptor in a "
            f"JSON numeric write; forbidden regardless of runtime fixup — use ES/EN "
            f"(e.g. ES24.16E3) or an explicit-width Fw.d with trim(adjustl()) instead"
        )



_RUNNER_UNIT_KEYWORD = re.compile(r"unit\s*=\s*(.*)", re.DOTALL)



# Unit designators that are never a JSON artifact (stdout / stderr); formatted
# writes to them are debug/log output and must not be scanned for JSON safety.
_NON_JSON_WRITE_UNITS = {"*", "output_unit", "error_unit"}



def _runner_write_unit(io_control: str) -> str | None:
    """Return the unit designator of a ``write`` control list (``*`` / name / id)."""
    items = [item.strip() for item in fortran_lines.split_top_level_commas(io_control)]
    for item in items:
        keyword = _RUNNER_UNIT_KEYWORD.match(item)
        if keyword:
            return keyword.group(1).strip()
    if items and not _RUNNER_KEYWORD_ITEM.match(items[0]):
        return items[0].strip()
    return None



def _runner_write_format_token(io_control: str) -> str | None:
    """Return the format spec token referenced by a ``write`` control list.

    Handles ``fmt=`` keyword form and the positional form (unit first, format
    second). Returns the raw token (a quoted literal, an integer label, or a
    name); ``None`` when there is no format (e.g. list-directed ``write(u, *)``
    is returned as ``*`` and filtered by the caller).
    """
    items = [item.strip() for item in fortran_lines.split_top_level_commas(io_control)]
    for item in items:
        keyword = _RUNNER_FMT_KEYWORD.match(item)
        if keyword:
            return keyword.group(1).strip()
    positional = [item for item in items if not _RUNNER_KEYWORD_ITEM.match(item)]
    if len(positional) >= 2:
        return positional[1].strip()
    return None



def _depth0_assignment_rhs(line: str, name: str) -> str | None:
    """Return the RHS of a top-level assignment ``name = rhs`` on ``line``.

    Quote- and paren-aware so an I/O keyword argument (``fmt=`` inside
    ``write(...)``), an equality test (``==``), and other relational operators
    (``/=`` / ``>=`` / ``<=``) are not mistaken for a variable assignment, and an
    array-element target (``a(i) = ...``) does not match a scalar ``name``.
    Returns ``None`` when the line is not such an assignment.
    """
    depth = 0
    in_single = False
    in_double = False
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if in_single:
            if ch == "'":
                in_single = False
        elif in_double:
            if ch == '"':
                in_double = False
        elif ch == "'":
            in_single = True
        elif ch == '"':
            in_double = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "=" and depth == 0:
            if i + 1 < n and line[i + 1] == "=":
                i += 2
                continue
            if i > 0 and line[i - 1] in "<>/=":
                i += 1
                continue
            j = i - 1
            while j >= 0 and line[j] == " ":
                j -= 1
            end = j
            while j >= 0 and (line[j].isalnum() or line[j] == "_"):
                j -= 1
            lhs = line[j + 1 : end + 1]
            if lhs == name and not (j >= 0 and line[j] == ")"):
                # Stop at the next top-level comma so a sibling initializer in a
                # multi-name declaration (``:: a = '(...)', b = '(...)'``) is not
                # folded into this name's RHS.
                return fortran_lines.split_top_level_commas(line[i + 1 :])[0].strip()
        i += 1
    return None



def _split_top_level_concat(expr: str) -> list[str]:
    """Split ``expr`` on the Fortran ``//`` concatenation operator at top level.

    ``//`` inside quotes or parentheses is ignored, so only operands joined at the
    expression's top level are separated.
    """
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    in_single = False
    in_double = False
    i = 0
    n = len(expr)
    while i < n:
        ch = expr[i]
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
        elif ch == "/" and depth == 0 and i + 1 < n and expr[i + 1] == "/":
            parts.append("".join(current))
            current = []
            i += 2
            continue
        else:
            current.append(ch)
        i += 1
    parts.append("".join(current))
    return parts



def _resolve_format_expr(expr: str) -> str | None:
    """Resolve a format-spec expression to its constant value, or ``None``.

    Handles a single character literal and a constant concatenation of character
    literals (``'(a,' // 'l1,a)'``). Any non-literal operand (variable / function
    call) makes the expression unresolvable, returning ``None`` so a partial
    literal is never scanned. The resolved value must look like a Fortran format
    spec (parenthesized).
    """
    pieces: list[str] = []
    for operand in _split_top_level_concat(expr):
        operand = operand.strip()
        literal = _RUNNER_CHAR_LITERAL.fullmatch(operand)
        if not literal:
            return None
        pieces.append(_fortran_literal_value(operand))
    value = "".join(pieces)
    return value if value.lstrip().startswith("(") else None



def _assign_fortran_scopes(
    logical_lines: list[tuple[int, str]],
) -> tuple[list[int], dict[int, int | None]]:
    """Return a scope id per logical line plus a ``scope -> parent scope`` map.

    A ``program`` / ``subroutine`` / ``function`` / ``(sub)module`` header opens a
    new scope nested in the current one; the matching ``end`` (or ``end <kind>``)
    closes it. Construct ends (``end do`` / ``end if`` / ...) are not unit ends and
    do not close a scope. The parent map enables host-association lookups for
    names declared in an enclosing unit.
    """
    scopes: list[int] = []
    parents: dict[int, int | None] = {0: None}
    stack = [0]
    next_id = 1
    for _lineno, line in logical_lines:
        lowered = line.lower()
        if _FORTRAN_UNIT_END.match(lowered):
            scopes.append(stack[-1])
            if len(stack) > 1:
                stack.pop()
            continue
        opens_unit = bool(_FORTRAN_UNIT_OPEN.match(lowered)) and not re.match(
            r"^\s*module\s+procedure\b", lowered
        )
        if not opens_unit and _FORTRAN_FUNCTION_OPEN.search(lowered):
            opens_unit = True
        if opens_unit:
            next_id += 1
            parents[next_id] = stack[-1]
            stack.append(next_id)
            scopes.append(next_id)
        else:
            scopes.append(stack[-1])
    return scopes, parents



def validate_runner_json_serialization(
    runner_file: Path,
    text: str,
    violations: list[str],
) -> None:
    """Flag JSON-incompatible Fortran edit descriptors in runner write statements.

    The ``runner`` emits only JSON artifacts (``diagnostics.json`` / ``perf.json``
    / ``raw/metrics_basis.json`` / ``raw/state_snapshots/*.json``). Two descriptor
    classes in a ``write`` format spec break standard JSON parsing and must never
    reach a JSON token:

      * ``L`` / ``L<n>`` logical edit descriptor emits the bare tokens ``T`` / ``F``
        instead of JSON ``true`` / ``false`` (booleans must be literal strings).
      * ``F0`` / ``F0.d`` numeric edit descriptor can emit leading-zero-less floats
        like ``.5`` that violate RFC 8259.

    The source is scanned as *logical* lines (free-form ``&`` continuations are
    merged, inline comments stripped). Only genuine ``write(...)`` statements are
    inspected — the keyword must be followed by ``(`` so an identifier such as
    ``write_flag`` on a ``read`` line is not mistaken for output. For each write
    the format spec it actually references is resolved and scanned, whether it is
    an inline literal (``write(u, '(...)')`` / ``write(u, fmt='(...)')``), an
    integer label bound to a ``FORMAT`` statement (``write(u, 100)`` +
    ``100 format(...)``), or a named character constant/variable
    (``write(u, fmt)`` + ``fmt = '(...)'``). Statement labels and local variables
    are resolved within the write's own Fortran scoping unit, so a label/name
    reused in another program unit is not confused. For a named format the most
    recent literal assignment at or before the write (its reaching definition) is
    scanned; a non-literal reassignment (``fmt=fmt`` / computed) does not clear a
    prior unsafe literal. ``read`` statements and read-only format definitions are
    never scanned, since logical/numeric input parsing legitimately uses these
    descriptors.
    """
    logical_lines = iter_logical_lines(text)
    scopes, scope_parents = _assign_fortran_scopes(logical_lines)

    def scope_chain(scope: int) -> list[int]:
        chain: list[int] = []
        current: int | None = scope
        while current is not None:
            chain.append(current)
            current = scope_parents.get(current)
        return chain

    # First pass: collect per-scope label-bound formats and every write statement
    # with the format token it references; record which names are used so only
    # their assignments need tracking.
    # ``order`` is the logical-statement sequence index (monotonic across the
    # whole file), used for reaching analysis instead of the physical line number
    # because ``;``-separated statements share one physical line.
    label_formats: dict[tuple[int, str], str] = {}
    writes: list[tuple[int, int, int, str]] = []  # (scope, order, lineno, token)
    used_names: set[str] = set()
    for order, (scope, (lineno, line)) in enumerate(zip(scopes, logical_lines)):
        lowered = line.lower()
        label_match = _RUNNER_FORMAT_STMT.match(lowered)
        if label_match:
            label_formats[(scope, label_match.group(1))] = extract_balanced_parens(
                lowered, label_match.end() - 1
            )
        for write_match in _RUNNER_WRITE_STMT.finditer(lowered):
            io_control = extract_balanced_parens(lowered, write_match.end() - 1)
            unit = _runner_write_unit(io_control)
            if unit is not None and unit in _NON_JSON_WRITE_UNITS:
                continue
            token = _runner_write_format_token(io_control)
            if not token:
                continue
            writes.append((scope, order, lineno, token))
            if (
                token[0] not in "'\""
                and not token.isdigit()
                and _RUNNER_NAME_TOKEN.match(token)
            ):
                used_names.add(token)

    # Collect literal assignments per (scope, name) keyed by statement order. Only
    # resolvable format literals are recorded; a non-literal assignment is
    # intentionally not recorded so it neither resolves nor erases a prior literal.
    name_assignments: dict[tuple[int, str], list[tuple[int, str]]] = {}
    if used_names:
        for order, (scope, (lineno, line)) in enumerate(zip(scopes, logical_lines)):
            lowered = line.lower()
            for name in used_names:
                rhs = _depth0_assignment_rhs(lowered, name)
                if rhs is None:
                    continue
                value = _resolve_format_expr(rhs)
                if value is not None:
                    name_assignments.setdefault((scope, name), []).append(
                        (order, value)
                    )

    # Second pass: scan each write's resolved format spec. Labels are strictly
    # local to their unit; named formats follow host association (the write's
    # scope and its enclosing scopes) and use the latest assignment strictly
    # before the write in statement order.
    for scope, order, lineno, token in writes:
        if token[0] in "'\"":
            value = _resolve_format_expr(token)
            if value is not None:
                _scan_runner_format_text(runner_file, lineno, value, violations)
        elif token.isdigit():
            content = label_formats.get((scope, token))
            if content is not None:
                _scan_runner_format_text(runner_file, lineno, content, violations)
        elif _RUNNER_NAME_TOKEN.match(token):
            candidates: list[tuple[int, str]] = []
            for ancestor in scope_chain(scope):
                candidates.extend(name_assignments.get((ancestor, token), ()))
            reaching: str | None = None
            for assign_order, value in sorted(candidates):
                if assign_order < order:
                    reaching = value
                else:
                    break
            if reaching is not None:
                _scan_runner_format_text(runner_file, lineno, reaching, violations)



# A whole-path snapshot data filename embedded in a single string literal:
# ``state_snapshots/<name>.json``. The per-case contract requires the runner to
# BUILD the name from the case_id it receives on argv (e.g.
# ``'raw/state_snapshots/'//trim(case_id)//'.json'``), which keeps ``.json`` in a
# SEPARATE literal so this pattern does not match. A fixed/sequential literal
# (``snapshot_0001.json``, a combined file) does match. The character class
# excludes quotes so a match never crosses a string-literal boundary.
_RUNNER_SNAPSHOT_LITERAL = re.compile(r"state_snapshots/([^/'\"]+)\.json")



def validate_runner_snapshot_filenames(
    runner_file: Path,
    text: str,
    violations: list[str],
    known_case_ids: set[str] | None = None,
) -> None:
    """Flag a hardcoded ``raw/state_snapshots/<name>.json`` filename in the runner.

    ``Validate.execute``'s deliverable gate requires exactly one
    ``raw/state_snapshots/<case_id>.json`` per ``case.test_case_set[].case_id``
    (``workflow_conductor.build_launch_request``), so the runner must build the
    snapshot path from the ``case_id`` it receives on argv (``--cases <spec>
    <case_id>...``) rather than emitting a fixed/sequential name. This best-effort
    static check catches the common failure: a whole-path string literal carrying
    ``state_snapshots/<name>.json`` with no per-case concatenation — e.g.
    ``snapshot_0001.json``. A correctly-built name (``trim(case_id)//'.json'``)
    keeps ``.json`` in a separate literal and is not flagged; the runtime
    deliverable gate is the deterministic backstop for constructions this static
    parse cannot resolve. ``snapshot_schema.json`` (conductor-authored metadata)
    is exempt. When ``known_case_ids`` is given, a hardcoded literal whose stem IS
    a declared case_id is NOT flagged — it satisfies the deliverable gate, so
    flagging it would be a false positive (case-insensitive: ``text`` is the
    already-lowercased runner source and case_ids are lowercase by convention).
    """
    case_id_stems = (
        {cid.lower() for cid in known_case_ids} if known_case_ids else set()
    )
    for lineno, line in iter_logical_lines(text):
        if "state_snapshots/" not in line:
            continue
        # Scope to file-opening statements so a snapshot path written as JSON
        # *content* (not an output target) is never mistaken for a filename.
        if "file=" not in line and not re.search(r"\bopen\s*\(", line):
            continue
        for match in _RUNNER_SNAPSHOT_LITERAL.finditer(line):
            name = match.group(1)
            if name == "snapshot_schema" or name in case_id_stems:
                continue
            violations.append(
                f"{runner_file}:{lineno}: hardcoded snapshot filename "
                f"'state_snapshots/{name}.json' — write one "
                "raw/state_snapshots/<case_id>.json per case, building the name "
                "from the case_id received on argv (e.g. trim(case_id)//'.json'); "
                "a fixed/sequential name fails Validate.execute's per-case "
                "deliverable gate (phase_02_generate.md / phase_04_validate.md §43)"
            )



def statements(text: str) -> list[str]:
    """Fortran source as one STATEMENT per entry: comments stripped, `&` continuations joined,
    and `;`-joined statements split apart.

    Every rule written against "a line" is really written against a statement, so this is what
    such a rule must iterate. `fortran_logical_line_texts` alone does only the first two steps, and
    the omission is invisible until someone writes `use a; use b` — at which point an anchored
    `^\\s*use\\b` rule silently sees one statement and misses the other. Both halves of the M3c
    checks gate go through here so they cannot drift apart on that."""
    return [stmt for line in fortran_lines.fortran_logical_line_texts(text)
            for stmt in fortran_lines.split_fortran_statements(line)]



def checks_module_abi_facts(text: str, spec_id: str) -> tuple[set[str], set[str], set[str]]:
    """`(published, defined_subroutines, defined_procs)` for `module <spec_id>_checks` in `text`,
    lowercased. The 3-tuple projection of `checks_module_accessibility_scan` (below), kept as
    the two ABI gates' entry point.

    THE single parser for the checks-module ABI surface, shared by the deterministic
    `Generate.static` gate (`_validate_checks_source_files`, which reads the staged file) and the
    Z2 bundle acceptance gate (`codegen_bundle.m3c_checks_abi_violation`, which reads the
    producer's in-memory bundle before anything is written). They MUST agree: a second
    implementation is how the bundle gate came to accept output that `Generate.static` then
    rejected, reopening the phase — the drift this function exists to make impossible.

    `published` is what `use <spec_id>_checks, only:` can resolve: Fortran's module default is
    PUBLIC, so a name is published iff it is defined and not `private ::`'d, unless a bare
    `private` statement flips the default, in which case it must be `public ::`'d.

    `defined_procs` are the module-level procedure definitions written HERE, and
    `defined_subroutines` the subset of those spelled `subroutine`. Callers must read them as
    positive evidence only, never as "everything callable": a name can be published and callable
    without appearing in either — `use`-associated from another module, declared through an
    `interface` / generic block, or implemented in a submodule. So `n in defined_procs and n not
    in defined_subroutines` proves n is a FUNCTION here, while `n not in defined_procs` proves
    nothing at all (and rejecting on it fails a legal module).

    Iterates `statements`, not raw lines: left unsplit, `public :: a; public :: b` read
    as a single `public` statement whose list was `a; public :: b`, losing `a` (whose token was
    `a;`) and inventing a name `public` — legal Fortran (gfortran rc=0) reported unpublished by
    BOTH gates."""
    return checks_module_accessibility_scan(text, spec_id)[:3]



def unpublished_bound_state(text: str, spec_id: str, bound: Iterable[str]) -> list[str]:
    """The `bound` module-level variable names `use <spec_id>_checks, only: <name>` cannot
    resolve, by the same scan and the same notion of "published" the ABI gates use (Z6, issue
    #255): under a bare module-level `private` a variable is published iff a `public ::`
    statement names it; under the language's default-public accessibility it is published unless a
    `private ::` statement names it. A variable is never DEFINED in the sense a procedure is
    (the scan reads no declarations — that is the source-text surface the gates refuse to
    parse), so the default-public branch cannot tell an undeclared name from a declared one and
    accepts both; the `Generate.gate` syntax check then owns the undeclared case (`Symbol not
    found in module`), exactly as it owns a `use`-associated ABI procedure. Case-insensitive."""
    _, _, _, public_ids, private_ids, default_private = \
        checks_module_accessibility_scan(text, spec_id)
    out: list[str] = []
    for name in bound:
        key = name.casefold()
        if key in private_ids or (default_private and key not in public_ids):
            out.append(name)
    return out



def checks_module_accessibility_scan(
    text: str, spec_id: str,
) -> tuple[set[str], set[str], set[str], set[str], set[str], bool]:
    """The three sets of `checks_module_abi_facts` followed by `(public_ids, private_ids,
    module_default_private)` for `module <spec_id>_checks` in `text`, lowercased — the one scan
    behind `checks_module_abi_facts` (its first three) and `unpublished_bound_state` (its last
    three). See the former's docstring for what each set does and does not prove."""
    logical = statements(text)
    public_ids: set[str] = set()
    private_ids: set[str] = set()
    defined_procs: set[str] = set()
    defined_subroutines: set[str] = set()
    module_default_private = False  # a bare module-level `private` flips the default
    type_depth = 0  # a bare `private` inside a derived-type def is a component attr, not the module default
    in_interface = False  # a subroutine/function header inside an interface block is a proto, not a def
    proc_depth = 0  # nesting of procedure defs; only depth-0 (module-level) procs are published
    in_target_module = False  # only defs INSIDE `module <spec_id>_checks` are its published ABI
    target_module = f"{spec_id}_checks".lower()
    # A subroutine/function definition header. `function` may carry a type-spec prefix; the
    # `end <proc>` case is handled before this so the leading-token alternation can't match it.
    # The type-spec `[^!]*` is greedy, so it is matched against a STRING-MASKED copy of the line
    # (below): unmasked it ran from a declaration's type keyword into a string literal —
    # `character(len=*), parameter :: note = 'run subroutine case_setup first'` registered a
    # phantom definition and suppressed every later `public ::`. Masking rather than excluding
    # quotes fixes that WITHOUT rejecting a legal quote inside the type-spec itself
    # (`character(kind=kind('a')) function metric_compute()`), which the exclusion turned into a
    # published-but-undefined function the gate then accepted — a fail-open both gate authors
    # missed until a Codex review, since the runner `call`s it and Generate.syntax fails later.
    proc_start = re.compile(
        r"(?i)^\s*(?:(?:module|pure|impure|elemental|recursive|non_recursive)\s+)*"
        r"(?:(?:integer|real|double\s+precision|complex|logical|character|type|class)\b"
        r"[^!]*\s+)?"
        r"(subroutine|function)\s+([A-Za-z]\w*)")
    for ln in logical:
        s = ln.strip()
        # Enter/leave the TARGET checks module. A `module <name>` statement (not `module
        # subroutine/function/procedure`, which carry more tokens) opens a module; only
        # definitions/accessibility INSIDE `module <spec_id>_checks` are importable via
        # `use <spec_id>_checks` — procs after `end module` or in a second module are not.
        mo = re.match(r"(?i)^\s*module\s+([A-Za-z]\w*)\s*$", s)
        if mo:
            in_target_module = mo.group(1).lower() == target_module
            proc_depth, type_depth, in_interface = 0, 0, False
            continue
        if re.match(r"(?i)^\s*end\s*module\b", s):
            in_target_module = False
            proc_depth, type_depth, in_interface = 0, 0, False
            continue
        # An `interface` / `abstract interface` block declares procedure PROTOTYPES, not
        # definitions — its subroutine/function headers must not count as `defined_procs`
        # (else a module could publish an ABI name it only prototypes but never defines).
        if not in_interface and re.match(r"(?i)^\s*(?:abstract\s+)?interface\b", s):
            in_interface = True
            continue
        if in_interface:
            if re.match(r"(?i)^\s*end\s*interface\b", s):
                in_interface = False
            continue
        # Track derived-type nesting so a component-level bare `private` isn't mistaken
        # for the module default. `type :: name` / `type name` / `type, attrs :: name`
        # open a def; `type(...)` decls and `type is (...)` guards do not.
        if type_depth == 0 and re.match(r"(?i)^\s*type\b", s) \
                and not re.match(r"(?i)^\s*type\s*\(", s) \
                and not re.match(r"(?i)^\s*type\s+is\b", s):
            type_depth = 1
            continue
        if type_depth > 0:
            if re.match(r"(?i)^\s*end\s*type\b", s):
                type_depth = 0
            continue
        # `end subroutine/function/procedure` closes a proc scope. A bare `end` closes the
        # innermost program unit: a procedure when we are inside one, else the module itself.
        # (`end module` is handled above; construct ends `end do`/`end if`/… fall through.)
        if re.match(r"(?i)^\s*end\s*(?:subroutine|function|procedure)\b", s):
            if proc_depth > 0:
                proc_depth -= 1
            continue
        if re.match(r"(?i)^\s*end\s*$", s):
            if proc_depth > 0:
                proc_depth -= 1
            else:
                in_target_module = False  # bare `end` at unit level closes the (target) module
            continue
        # A subroutine/function definition. Only a MODULE-LEVEL (depth-0) definition INSIDE the
        # target checks module is a published entity the runner can `use ... only:`; a nested
        # internal procedure, or one in another module / after `end module`, is not. Matched on a
        # string-masked copy so the greedy type-spec neither crosses INTO a string literal nor is
        # blocked by a legal quote WITHIN the type-spec.
        pm2 = proc_start.match(mask_string_contents(s))
        if pm2:
            if in_target_module and proc_depth == 0:
                defined_procs.add(pm2.group(2).lower())
                if pm2.group(1).lower() == "subroutine":
                    defined_subroutines.add(pm2.group(2).lower())
            proc_depth += 1
            continue
        # `public` / `private` statements only publish/hide the target module's own entities,
        # and only in its specification part (depth 0, not inside a procedure body).
        if proc_depth > 0 or not in_target_module:
            continue
        m = re.match(r"(?i)^\s*public\b\s*(::)?\s*(.*)$", s)
        if m:
            for tok in re.split(r"[,\s]+", m.group(2).strip()):
                if re.fullmatch(r"[A-Za-z]\w*", tok):
                    public_ids.add(tok.lower())
            continue
        pm = re.match(r"(?i)^\s*private\b\s*(::)?\s*(.*)$", s)
        if pm:
            body = pm.group(2).strip()
            if not body:  # a bare module-level `private` makes the default accessibility private
                module_default_private = True
            else:
                for tok in re.split(r"[,\s]+", body):
                    if re.fullmatch(r"[A-Za-z]\w*", tok):
                        private_ids.add(tok.lower())
            continue
    # Fortran module default accessibility is PUBLIC unless a bare `private` statement flips
    # it. Under the (default) public module, a name is published iff it is DEFINED and not
    # explicitly `private ::`'d; under a bare-`private` module, iff it is `public ::`'d.
    if module_default_private:
        published = public_ids - private_ids
    else:
        published = (public_ids | defined_procs) - private_ids
    return (published, defined_subroutines, defined_procs,
            public_ids, private_ids, module_default_private)



# `subroutine` declaration opener mirroring orchestration_runtime._FORTRAN_SUBROUTINE_RE (the
# published-surface scanner the resolver uses): optional pure/impure/elemental/recursive/module
# prefixes, then `subroutine <name>`. `^\s*` anchors at the (comment-stripped, continuation-
# joined) logical-line start, so `end subroutine` / `call` lines never match. Kept in lock-step
# with the runtime regex by the cross-scanner parity test (ComponentGeneratedSurfaceGateTests).
_COMPONENT_PUBLISHED_SUB_RE = re.compile(
    r"^\s*(?:(?:pure|impure|elemental|recursive|module)\s+)*"
    r"subroutine\s+(?P<name>[A-Za-z]\w*)",
    re.IGNORECASE,
)



# An ABSTRACT interface block's span. A procedure header inside one is a PROTOTYPE (issue
# #266: the shape of a procedure a caller passes), not a published operation, whatever its
# name begins with — so the scan below skips the span. A NON-abstract interface body is
# different: it declares an external procedure the module can re-export, which is a callable
# a consumer links, so the scan counts it exactly as it did before this rule (a round-2
# reviewer measured the wider skip letting a module publish an extra `<spec_id>__` external
# through a sibling file). The opener is the whole statement, so a variable named
# `interface` opens nothing. Mirrored VERBATIM in the runtime's prefixed-name scanner, which
# the parity test pins.
_ABSTRACT_INTERFACE_SPAN_OPEN_RE = re.compile(r"^\s*abstract\s+interface\s*$", re.IGNORECASE)



_INTERFACE_SPAN_END_RE = re.compile(r"^\s*end\s*interface\b", re.IGNORECASE)



def published_subroutines(text: str, spec_id: str) -> list[str]:
    """Distinct, first-appearance-ordered ``subroutine`` names in ``text`` whose name begins
    (case-insensitive) with ``<spec_id>__`` — the component's published operation surface. A
    header inside an ``abstract interface`` block is a prototype and is not counted (issue
    #266); one inside a plain ``interface`` body is an external the module may re-export and
    counts, as before. The
    validator may NOT import ``orchestration_runtime`` (module-boundary rule), so this is a
    separate mirror of ``interface._list_prefixed_subroutines`` (the dependency-fact reader); the cross-scanner parity
    test pins the two implementations to the same result.

    The two used to be pinned only over the domain a code generator emits, because their
    hand-rolled scanners joined continuations with different whitespace and so resolved a
    pathological split THROUGH the ``subroutine`` keyword or through the name identifier
    (``pure&`` / ``dep__fo&``/``&o``) differently. That divergence is gone: both now scan with
    ``fortran_lines.fortran_logical_lines`` (issue #23), the single shared implementation, so the
    parity test runs over the FULL domain — mid-token splits, exotic line separators, a ``!``
    inside a continued literal, a lone-``&`` wrap line. What remains local to each side is only
    the composition (this one keeps leading whitespace for the ``^\\s*`` anchor above; the runtime
    strips). NEVER raises."""
    try:
        prefix = f"{spec_id}__".lower()
        out: list[str] = []
        seen: set[str] = set()
        in_interface = 0
        for _lineno, stmt in iter_logical_lines(text):
            if _INTERFACE_SPAN_END_RE.match(stmt):
                in_interface = max(0, in_interface - 1)
                continue
            if _ABSTRACT_INTERFACE_SPAN_OPEN_RE.match(stmt):
                in_interface += 1
                continue
            if in_interface:
                continue
            m = _COMPONENT_PUBLISHED_SUB_RE.match(stmt)
            if m is None:
                continue
            name = m.group("name")
            if not name.lower().startswith(prefix):
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(name)
        return out
    except Exception:
        return []



def mask_string_contents(line: str) -> str:
    """Replace the CONTENTS of every quoted string with spaces, keeping the quote delimiters and
    every character's position.

    For matching a statement's KEYWORD structure only: a string literal can hold text that looks
    like Fortran (`'run subroutine x first'`), and a real type-spec can hold a quote inside its
    parens (`character(kind=kind('a')) function f()`). Masking the contents removes the phantom
    keyword without disturbing the parens/`::`/`,` a header match keys on. Do NOT feed a masked
    line to a rule that inspects string CONTENT (the forbidden-filename scan deliberately catches
    a quoted `verdict.json`)."""
    out: list[str] = []
    quote: str | None = None
    for ch in line:
        if quote is not None:
            out.append(ch if ch == quote else " ")
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            out.append(ch)
        else:
            out.append(ch)
    return "".join(out)



def validate_dependency_operations(
    model_files: list[Path],
    dep_spec_ids: list[str],
    violations: list[str],
) -> None:
    for model_file in model_files:
        text = model_file.read_text(encoding="utf-8", errors="ignore")
        # All three checks below ask whether a KEYWORD appears in code, so neither a comment nor
        # the inside of a literal may answer. Unmasked, each was satisfiable from prose: a
        # commented-out `! use dep_model` or `! call dep__op(...)` silenced the two presence
        # requirements (fail-open — and the `use` one especially, since a model that host-
        # associates instead of using the module still compiles, which is exactly what this
        # check exists to catch), while a `write(*,*) 'calls subroutine dep__op'` or a
        # `! subroutine dep__op is external` raised a redefinition violation against a model that
        # defines nothing (fail-closed).
        lowered = fortran_lines.mask_code_lookalikes(text.lower())

        for spec_id in dep_spec_ids:
            spec_id_l = spec_id.lower()
            op_prefix = re.escape(spec_id_l + "__")
            module_name = re.escape(spec_id_l + "_model")

            if not re.search(rf"\buse\s+{module_name}\b", lowered):
                violations.append(
                    f"{model_file}: missing dependency module use ({spec_id}_model)"
                )

            if re.search(rf"\bsubroutine\s+{op_prefix}[a-z0-9_]*\b", lowered):
                violations.append(
                    f"{model_file}: dependency operation redefinition detected ({spec_id}__*)"
                )

            if not re.search(rf"\bcall\s+{op_prefix}[a-z0-9_]*\b", lowered):
                violations.append(
                    f"{model_file}: missing dependency operation call ({spec_id}__*)"
                )


def run_problem_model_gates(
    node_key: str,
    model_file: Path,
    lowered: str,
    dep_spec_ids: list[str],
    violations: list[str],
    *,
    multidim_spec_id: str | None,
) -> None:
    """Read one model source once and run the three `problem` model gates over it.

    ONE parse per file, for all three. They each used to call `procedure_envelopes`
    themselves, which parsed the same text three times and gave a refusal three chances to be
    reported — or, worse, to be reported differently. Both failure directions are answered here,
    once, and both stop the gates for this file rather than letting them run on a structure
    nothing resolved.

    A FUNCTION, not a block inside the caller's loop, so that every early return below stops the
    GATES and nothing else. Written as a `continue` first, which would have skipped any check a
    later change appends after the gates in that loop — the silent-skip shape this file keeps
    having to remove.

    Scoped to `problem/`, which is where each of the three gates returns early anyway. Hoisting
    the parse out of the gates quietly widened who it applies to: a `component` or
    `infrastructure` node has no gate reading its Fortran, so refusing its source bought nothing
    and cost a legal F2008 form (measured against origin/main: a component model carrying
    `real :: endsubroutine` / `endsubroutine = 1.0` went from 0 violations to 4). The refusal's
    own justification — that every gate would otherwise pass the file in silence — is vacuous
    where no gate runs. Found in review; 0 of the 365 in-tree models are affected either way.
    """
    if not node_key.startswith("problem/"):
        return

    try:
        envelopes = procedure_envelopes(lowered)
    # `FortranStructureUnavailableError` is deliberately NOT caught here. It is the OPERATOR's
    # failure — an uninstalled package — and no edit to this source can clear it, so it propagates
    # to `main`, which answers with a dedicated EXIT CODE. It used to be turned into a violation
    # string carrying a marker for the conductor to scan for, and a leaf-chosen filename defeated
    # that scan three times running; see the handler in `main`.
    except SourceStructureError as exc:
        for structure_error in exc.errors:
            violations.append(
                f"{model_file}: the Fortran structure front end could not resolve this "
                f"source at statement {structure_error.line} of its joined view "
                f"({'missing token' if structure_error.missing else 'parse error'}): "
                f"{structure_error.snippet!r}. The `problem` model gates cannot read a "
                f"source whose procedure structure is ambiguous, so this is a Generate "
                f"failure. Two causes have been observed, and the reported statement may be "
                f"the module header rather than either of them, because that is where the "
                f"parser gives up. (1) A VARIABLE or construct NAMED after a keyword — "
                f"`endsubroutine`, `interface`, `contains` and friends are legal names and "
                f"are read here as the statements they spell; rename it (e.g. "
                f"`end_subroutine_flag`). (2) STATEMENT LABELS that are needed in one place "
                f"and in the way in another — a labelled `DO` or a `FORMAT` alongside a "
                f"labelled `contains` or procedure header; give the loop an `end do` and "
                f"drop the label from the specification statement. Neither list is closed: "
                f"the shape to look for is an identifier or a label sitting where this "
                f"parser expects structure."
            )
        return

    # An abbreviated separate module subprogram is REFUSED, always, and this is the only
    # place that decides it. F2008 forbids such a body from redeclaring its dummies —
    # `gfortran -fsyntax-only -std=f2008` answers "is a redefinition of the declaration in the
    # corresponding interface" — so no `intent(out)` can appear in it and all three gates
    # would return at their empty out-set check while looking like they had run. That is a
    # silent gate, which is the exact defect class this whole area exists to remove, so the
    # leaf is asked for the full form instead. 0 of the 365 in-tree models use the short form
    # today, so this refuses nothing that exists; it closes the shape before it appears.
    abbreviated = [e.name for e in envelopes if e.kind == "module_procedure"]
    if abbreviated:
        for name in abbreviated:
            violations.append(
                f"{model_file}: `module procedure {name}` (the abbreviated separate module "
                f"subprogram) cannot be checked: F2008 forbids its body from redeclaring its "
                f"dummies, so the intent(out) declarations the `problem` model gates read "
                f"never appear in it and every gate would pass it in silence. Write the full "
                f"form instead — `module subroutine {name}(...)` with its "
                f"`intent(in)`/`intent(out)` declarations, or `module function {name}(...) "
                f"result(...)` — which is checked normally."
            )
    # ... and then the gates run ANYWAY, on the procedures in this file that ARE readable.
    # An earlier version stopped here, on the parse-error case's symmetry; the two are not
    # symmetric. A parse error leaves no usable envelope at all, while an abbreviated
    # `module procedure` leaves every OTHER procedure in the file perfectly readable, so
    # stopping here silenced their real violations and handed the leaf one instruction where
    # it needed two. The abbreviated envelope itself carries an empty out-set, so every gate
    # skips it — which is the silence being refused on its behalf above, not a second chance
    # for it to pass.

    _validate_problem_model_literal_outputs(
        node_key=node_key,
        model_file=model_file,
        envelopes=envelopes,
        violations=violations,
    )

    _validate_problem_model_dependency_dataflow(
        node_key=node_key,
        model_file=model_file,
        lowered=lowered,
        envelopes=envelopes,
        dep_spec_ids=dep_spec_ids,
        violations=violations,
    )
    _validate_problem_metric_only_scalar_kernel(
        multidim_spec_id=multidim_spec_id,
        model_file=model_file,
        envelopes=envelopes,
        violations=violations,
    )


def model_source_gates(
    *,
    node_key: str,
    model_file: Path,
    text: str,
    dep_spec_ids: list[str],
    violations: list[str],
    multidim_spec_id: str | None,
) -> None:
    """Every structural gate over one model source, in the order the validator ran them: the
    `index(case_id)` / literal-metric floors, then the three `problem` model gates
    (`run_problem_model_gates`). `text` is the file's raw content."""
    lowered = text.lower()
    # The metric scans below are the same shape as the `problem` model gates — a regex over
    # multi-line source — and need the same view. Their `([^\n!]+)` right-hand side stopped at
    # the physical newline, so a wrapped `metrics(1) = &` / `1.0` captured only the `&`, which
    # carries no digit and so never counted as literal-like: the literal-metric floor was
    # escapable by wrapping the assignments. The gates below re-derive this view themselves
    # (it is a fixed point) and are left reading `lowered` so their own contract stays whole.
    joined = joined_masked_view(lowered)

    if re.search(r"index\s*\(\s*case_id", joined) and re.search(
        r"metrics\s*\(\s*\d+\s*\)", joined
    ):
        violations.append(
            f"{model_file}: hardcoded case_id -> metrics assignment pattern detected"
        )

    assignments = re.findall(
        r"metrics\s*\(\s*\d+\s*\)\s*=\s*([^\n!]+)",
        joined,
        flags=re.MULTILINE,
    )
    literal_like = 0
    for rhs in assignments:
        if re.search(r"[-+]?\d+(?:\.\d+)?(?:d|e)?[-+]?\d*", rhs):
            literal_like += 1
    if len(assignments) >= 6 and literal_like >= 6:
        violations.append(
            f"{model_file}: many literal metric assignments detected ({literal_like}/{len(assignments)})"
        )

    run_problem_model_gates(
        node_key=node_key,
        model_file=model_file,
        lowered=lowered,
        dep_spec_ids=dep_spec_ids,
        violations=violations,
        multidim_spec_id=multidim_spec_id,
    )



def checks_module_declaration_violations(checks_path: Path, text: str, spec_id: str) -> list[str]:
    """The checks source must declare the fixed ABI module `<spec_id>_checks`."""
    logical = fortran_lines.fortran_logical_line_texts(text)
    if not any(re.match(rf"(?i)^\s*module\s+{re.escape(spec_id)}_checks\b", ln)
               for ln in logical):
        return [f"{checks_path}: must declare `module {spec_id}_checks` (the fixed ABI module)"]
    return []


def checks_harness_isolation_violations(
    checks_path: Path, text: str, model_files: list[Path]
) -> list[str]:
    """The isolation half of the M3c checks-source gate: neither physics source `use`s the
    harness, and the checks module does no file I/O. `text` is the checks source's raw content;
    each model source is read here."""
    violations: list[str] = []
    logical = fortran_lines.fortran_logical_line_texts(text)

    # Neither the checks nor the model source may `use` the harness module. Tolerate the
    # optional `, <attr>` (e.g. `, intrinsic`) and `::` forms — `use harness_x`,
    # `use :: harness_x`, and `use, non_intrinsic :: harness_x` must all be caught. The scan is
    # per STATEMENT, not per line: the regex is anchored, so a harness `use` written as the second
    # statement of a `;`-joined line (`use, intrinsic :: iso_fortran_env, only: dp => real64;
    # use harness_fortran_cpu_model, only: ...`, legal and rc=0) would otherwise be invisible —
    # a fail-OPEN on the isolation invariant that nothing downstream catches, since the harness is
    # staged for `Generate.syntax` and the bundle contract has no isolation layer.
    use_harness_re = re.compile(r"(?i)^\s*use\b\s*(?:,\s*\w+\s*)?(?:::\s*)?harness_")
    for f in [checks_path, *model_files]:
        ftext = f.read_text(encoding="utf-8", errors="ignore")
        if any(use_harness_re.match(stmt.strip())
               for stmt in statements(ftext)):
            violations.append(
                f"{f}: a physics source must not `use` the harness module — the physics "
                "node never depends on the harness at the source level (the host-rendered "
                "runner is the sole `use harness_*` site)")

    # The checks module does no file I/O (emission is the harness/runner's exclusive job). Scan
    # string-masked statements: an `open(` call is code, so a quoted `'open('` in a message string
    # is not one — matching it would fail-close a legal module. (The forbidden-filename scan the
    # validator runs after this is deliberately the opposite: it inspects string CONTENT, so it
    # runs on the raw text.)
    if any(re.search(r"(?i)\bopen\s*\(", mask_string_contents(ln)) for ln in logical):
        violations.append(
            f"{checks_path}: checks module must not do file I/O (`open(`) — emission is the "
            "harness's job; the checks module only computes state/checks/metrics")
    return violations





def model_source_not_found_violation(
    src_dir: Path, expected_model_name: str | None, model_glob: str
) -> str:
    """Build the violation message for an absent node model source.

    When ``expected_model_name`` is None the spec_id could not be derived, so the
    name is unknown and the message stays generic. Otherwise, distinguish the
    real causes: (a) the required literal module name ``<spec_id>_model`` exceeds
    the f2008 identifier limit, so no valid literal name exists and renaming
    cannot fix it; (b) no ``*_model.f90`` was emitted at all; (c) a model source
    exists but under a non-literal (abbreviated/derived) name. Case (c) is the
    common Generate mistake — the literal ``<spec_id>_model.f90`` is required (a
    depending node resolves it via ``use <spec_id>_model``) — so the message names
    the offending file and instructs a rename rather than the misleading
    "not found", which reads as if no file was written.
    """
    if expected_model_name is None:
        return f"{src_dir}: model source not found"
    # The module identifier is the expected name without the ``.f90`` suffix. Fortran 2008
    # limits a name to `bundle.IDENTIFIER_MAX` (63) characters, so an over-limit
    # `<spec_id>_model` fails here as a spec-level problem (the spec_id is too long). General
    # over-limit identifiers in the generated source are caught by the real compiler front-end
    # in the deterministic `generate.gate` substep.
    expected_module = expected_model_name[: -len(".f90")]
    if len(expected_module) > IDENTIFIER_MAX:
        # No legal literal name exists: <spec_id>_model is itself over the f2008
        # limit. Renaming the abbreviated file would only trade one violation for
        # another, so this is a spec-level problem (the spec_id is too long) and
        # must stop there rather than be "fixed" at Generate's discretion.
        return (
            f"{src_dir}: required model module name {expected_module} "
            f"({len(expected_module)} chars) exceeds the f2008 "
            f"{IDENTIFIER_MAX}-char identifier limit; the spec_id is too "
            "long for a literal <spec_id>_model name — stop as a spec-level "
            "problem (do not abbreviate at Generate's discretion)"
        )
    present = sorted(
        p.name for p in src_dir.glob(model_glob) if p.is_file()
    )
    if present:
        return (
            f"{src_dir}: model source {', '.join(present)} present but must be "
            f"named {expected_model_name} (literal spec_id prefix required; "
            "abbreviated/derived prefix rejected) — rename to match"
        )
    return f"{src_dir}: node model source not found ({expected_model_name})"


#: The sources a build control file must order by module dependency, and the artifact a compiled
#: module leaves beside its object — what the build system's prerequisite gate
#: (`tools/backends/build_system/make/gates.validate_src_dir`) asks of this language.
MODULE_SOURCE_SUFFIXES: tuple[str, ...] = (".f90",)
MODULE_ARTIFACT_SUFFIX = ".mod"


# The presence floor's whole Fortran surface: four anchored patterns over PHYSICAL lines, no state
# — the three loop patterns below (`counted_loops`) and the OpenMP sentinel, which is the parallel
# backend's (`tools/backends/parallel/openmp/directives.py`).
#
# Rounds of review found six defects in a previous, cleverer scanner that joined `&` continuations,
# tracked character-literal state across lines, and split statements on `;`. Each fix introduced the
# next defect, and four of the six were FALSE POSITIVES on a `fail_closed` gate. Measured over every
# `.f90` file in the tree, that machinery produced verdicts identical to these four patterns — it was
# priced entirely for inputs that have never occurred (the corpus contains no continued `do` header,
# no `do concurrent`, no labelled or named `do`, and no `do` after a `;`).
#
# The insight that makes the state unnecessary: anchoring at a PHYSICAL line start puts comments and
# almost all string literals out of reach. A comment line begins with `!`, so an `!$omp` inside one
# is never at a line start; and a continued character literal resumes with `&`, so its content is not
# either. The earlier comment-mention and literal-mention evasions are closed by the anchor rather
# than by parsing.
#
# That second half needed a qualifier — "a CONFORMING literal" — until issue #25 removed it. gfortran
# ALSO accepts a literal resumed with no `&` at all, and `'start&` / `do i = 1, n suffix'` then does
# put a counted-`do` spelling at a physical line start, inside a string; the floor would have counted
# it, a false REJECT on a source the syntax gate passed. Issue #23 measured that and accepted it;
# issue #25 closed it at the root instead, by promoting `-Werror=ampersand` in the `Generate.gate`
# syntax check (`syntax.PROMOTED_WARNINGS`, applied by the compiler's syntax adapter). Such a source
# is now rejected before any source reaches this gate, so the anchor holds over every literal that
# gets here, with no state and no qualifier.
#
# That joining scanner does live in the tree again, as `lines` in this package (issue #23) — but for
# consumers this floor is not: they read the JOINED logical line and compare it against a declared
# surface, so they cannot anchor their way out of the state. A presence check can, so this one still
# must not take the dependency. The two answers are not in conflict; the question differs.
#
# `[ \t\f]` is gfortran's blank set: a form feed is a blank it accepts, both before a `do` and as a
# token separator (verified against the compiler, and the reason the previous scanner leaked).
_BLANK = r"[ \t\f]"
# A `do` opener at a line start, with the spellings a generator plausibly emits: a statement label
# before it (`10 do i = 1, n`), a named construct (`loop_i: do ...`).
_DO_OPENER = rf"^{_BLANK}*(?:\d+{_BLANK}+)?(?:[a-z_]\w*{_BLANK}*:{_BLANK}*)?do"
# The separator between `do` and its loop-control. F2008 permits a LEADING COMMA there
# (`do , i = 1, n`), which gfortran accepts under `-std=f2008`, so a comma form must classify
# like the plain one — otherwise `do , concurrent (...)` went unrecognized and a genuinely
# parallel file could be flagged.
_DO_SEP = rf"(?:{_BLANK}+|{_BLANK}*,{_BLANK}*)"

# A COUNTED loop: `do <var> =`, also accepting an obsolescent branch-target label (`do 10 i = 1, n`).
# The `=` tail is what makes it counted, so `do concurrent (...)` and `do while (...)` are excluded by
# construction, and `do_it = 1` cannot match because `do` must be followed by a blank.
_COUNTED_DO_RE = re.compile(
    _DO_OPENER + rf"{_DO_SEP}(?:\d+{_BLANK}+)?[a-z_]\w*{_BLANK}*=", re.IGNORECASE | re.MULTILINE
)

# `do concurrent` anywhere in the file is a declaration of parallel intent and takes the file out of
# the floor's reach even when counted loops sit beside it — without this, an accumulator reset beside
# `do concurrent` work was reported as unparallelized, contradicting the floor's own message.
_DO_CONCURRENT_RE = re.compile(
    _DO_OPENER + rf"{_DO_SEP}concurrent\b", re.IGNORECASE | re.MULTILINE
)

# A `do` header that wraps before it can be classified (`do &`, `do i &`, `do&`). Such a header might
# be a `do concurrent`, so its presence fails the WHOLE FILE open rather than risk the false positive.
#
# Two guards keep it from swallowing a file it has no business in — this pattern takes the WHOLE FILE
# out of scope, so an over-match here does not miss a loop, it silently disables the gate.
#
# The lookahead belongs to THIS pattern alone: the two above are separated from their operand by
# `{_BLANK}+`, but this one may see `do&` with nothing between, so without a boundary it matched any
# wrapped line whose first token merely STARTS with `do` — `domain, &`, `double &`,
# `dot_product(a,b) &`. A continued argument list is this repo's idiomatic style.
#
# And the wrapped token excludes `=`, because `\S*` is one blank-free token: a header whose BOUNDS
# wrap is classifiable and must fall through to `_COUNTED_DO_RE`, but `do i=1, &` (no blanks around
# the `=`, which is ordinary spacing) put the whole `i=1,` into that token and matched here. Only the
# spaced spelling escaped, so the guarantee rested on a space.
#
# A trailing comment after the marker is allowed (`do & ! parallel loop`), which free form permits
# and gfortran accepts: requiring only blanks after the `&` left such a header unrecognized, so a
# counted loop beside it fired on a source whose wrapped header may well have been a `do concurrent`.
#
# The lookahead and the optional comma admit `_DO_SEP`'s comma form, so a `do , … &` header is
# recognized as wrapped. Missing it was the false-positive direction: the file would stay in scope
# and a counted loop beside it could fire, even though the wrapped header might be a `do concurrent`.
# A 13k-combination sweep over {blanks, labels, construct names, `do`-prefixed identifiers,
# separators, tokens, trailing blanks} now reports zero spurious matches and zero missed headers.
_WRAPPED_DO_RE = re.compile(
    _DO_OPENER + rf"(?={_BLANK}|&|,){_BLANK}*,?{_BLANK}*[^=\s]*{_BLANK}*&{_BLANK}*(?:!.*)?$",
    re.IGNORECASE | re.MULTILINE,
)


def counted_loops(text: str) -> int:
    """The number of counted `do` loops a parallel backend's presence floor may demand a directive
    for, or 0 when the source is out of the floor's reach: it holds a `do concurrent` (already
    parallel by construct, whatever else the file contains) or a `do` header that wraps before it
    can be classified (it might be a `do concurrent` — fail open). A source with only whole-array
    syntax answers 0 too."""
    if _DO_CONCURRENT_RE.search(text):
        return 0
    if _WRAPPED_DO_RE.search(text):
        return 0
    return len(_COUNTED_DO_RE.findall(text))
