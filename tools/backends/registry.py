#!/usr/bin/env python3
"""The one place that maps a target-stack axis VALUE to the code that knows that technology.

The rule this serves is `docs/BACKEND_BOUNDARY.md`: the neutral core (the conductor, the
runtime, the deterministic gates, the MCP server, the prompt templates) may name an axis value
— `fortran`, `make`, `gfortran` — but may not contain the knowledge that value implies. That
knowledge lives in `tools/backends/<axis>/<backend_id>/` and is reached through this module.

WHAT THIS MODULE IS, STATED PRECISELY, because a registry that overstates itself is worse than
none. It is a DECLARATION plus a loader. It declares, per axis, which backend ids this
repository implements and which of them have actually been extracted into a backend package.
It does NOT enforce that the neutral core goes through it — nothing at import time can tell a
`re.compile(r"subroutine")` inlined in a gate from a neutral one. That enforcement is the
`tools/tests/test_backend_boundary.py` ratchet, and the two work as a pair: this module says
where the knowledge belongs, the ratchet says the neutral core is not accumulating more of it.

`extracted=False` is the honest state of an axis whose knowledge is still inlined in the
neutral core. It is not a stub and not a plan — it is a member whose module is `None`, so
`load()` raises instead of returning something that pretends to work. The migration ledger in
`TODO.md` is what turns those into `True`.

FOUR QUESTIONS, and a caller must ask the one it means. They are deliberately separate
functions rather than one with a flag, because the flag was the bug: a single function answered
membership while its callers meant usability.

    unsupported_reason    declared member?                      naming / message building
    unimplemented_reason  declared AND some code exists          node-acceptance gates: may a
                                                                 run carry this value at all
    provides              the NEUTRAL CORE does <job> for it     host dispatch: may I run MY
                                                                 inlined writer on this node
    unavailable_reason    declared AND extracted                 about to RUN backend code

A record with `module=None` and no capabilities — registered, implemented nowhere — answers
`None` to the first and a refusal to the rest. That is the fail-CLOSED default for a new member.

`provides` is the question every HOST-AUTHORSHIP dispatch has, and getting it wrong is how the
neutral core would hand a node to the wrong writer. The conductor's control-file writer and its
runner renderer each emit ONE build system's and ONE language's text. A predicate guarding them
on "is this value implemented" would answer True the day a second build system is implemented AS
A BACKEND — and route its nodes into the existing writer, which is worse than the hard-coded pair
it replaced.
So a capability says which value the NEUTRAL CORE has an inlined implementation of that job for,
and it is declared per record: a `cmake` backend that does not declare `control_file` gets
`False`, which is the documented leaf-authored path, not a silent misrender. When an area of the
migration ledger lands, its capability moves from the record's declaration to real dispatch
through the backend module, and the capability declaration goes away with it.

Stdlib only, and imports no other module of this package at import time, so every site can
depend on it — including `tools/validate_pipeline_semantics.py`, which may not import
`tools/orchestration_runtime.py` (module-boundary rule), and the recovery paths of
`orchestration_runtime`, which defer PyYAML.
"""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import NamedTuple


class UnsupportedBackend(LookupError):
    """The axis value is not one this repository implements."""


class BackendNotExtracted(NotImplementedError):
    """The backend is implemented, but its code has not moved out of the neutral core yet."""


class Axis(NamedTuple):
    """One dimension of the target stack."""

    name: str
    #: Where the workflow reads this axis' value from, as a dotted path into the artifact that
    #: carries it, or a prose source when no artifact pins it. Kept as text: the readers differ
    #: (the conductor reads a parsed IR, `record-launch` re-parses it), and naming a single
    #: accessor here would be a second owner of a fact those readers already share.
    source: str
    description: str
    #: True when the artifact that carries this axis deliberately does NOT constrain its value,
    #: so `_BACKENDS` lists the members that have code and is not a whitelist. Membership
    #: questions answer permissively for such an axis; extraction questions do not. Declaring a
    #: closed set here for an open knob would refuse values the schema exists to allow —
    #: `spec/schema/ir/impl_defaults.schema.json` says of `parallelization` that "the vocabulary
    #: is deliberately NOT a whitelist, since this is an exploration knob", and the validator
    #: accepts `openmp+simd` / `openmp_tasks` / `cpu_openmp` today.
    open_vocabulary: bool = False


#: The axes, in the order a run resolves them.
AXES: dict[str, Axis] = {
    "language": Axis(
        name="language",
        source="ir.impl_defaults.toolchain.language",
        description=(
            "The implementation language of the generated source: its syntax, its file "
            "extensions, its symbol spelling, and how a language-neutral signature renders "
            "into it."
        ),
    ),
    "build_system": Axis(
        name="build_system",
        source="ir.impl_defaults.toolchain.build_system",
        description=(
            "The tool that builds the generated source: who authors its control file, what "
            "that file's grammar is, and which targets the workflow requires of it."
        ),
    ),
    "compiler": Axis(
        name="compiler",
        source=(
            "ir.impl_defaults.toolchain.compiler (optional; pins the build compiler, "
            "docs/IMPL_PLAN_SPEC.md) and METDSL_SYNTAX_COMPILERS plus the mandatory "
            "syntax-only stage each language backend names"
        ),
        description=(
            "The compiler front end the syntax-only gate drives: its argv, its diagnostic "
            "format, and which of its warnings the gate promotes to errors."
        ),
    ),
    "linter": Axis(
        name="linter",
        source="the static-lint step's configured linter",
        description=(
            "The static linter the `Generate` lint step runs: its invocation, its rule ids, "
            "and which of them the prompts ask a leaf to satisfy."
        ),
    ),
    "parallel": Axis(
        name="parallel",
        source="ir.impl_defaults.abstract.parallelization",
        description=(
            "The parallel execution model: its directive or construct spelling in the target "
            "language, and the knobs (thread counts, scopes) the host renders for it."
        ),
        open_vocabulary=True,
    ),
}


#: The host-side jobs a value can be implemented FOR, and which axis each is asked of. A
#: capability names a responsibility the NEUTRAL CORE carries today with code specific to one
#: value — the debt the migration ledger in `TODO.md` is paying down, written where the dispatch
#: sites can read it instead of each spelling the value themselves. It is not a feature flag and
#: not a plan: declaring one asserts the inlined code exists NOW.
CAPABILITIES: dict[str, tuple[tuple[str, ...], str]] = {
    "control_file": (
        ("build_system", "language"),
        "The neutral core authors the build control file for this value, and the deterministic "
        "gates parse it. Asked of BOTH axes: the file's syntax is the build system's and its "
        "compile rules are the language's, so the host writes it only where it has both.",
    ),
    "build_execute": (
        ("build_system",),
        "The in-process build / execute path drives this value. Kind-agnostic: it applies to an "
        "infrastructure node exactly as to a physics node.",
    ),
    "runner_render": (
        ("language",),
        "The host renders the runner glue over the certified harness for this value "
        "(`tools/runner_renderer.py`), rather than a leaf authoring it.",
    ),
    "syntax_check": (
        ("compiler",),
        "The syntax-only gate has an argv adapter and a diagnostic reader for this value.",
    ),
    "lint": (
        ("linter",),
        "The static-lint step can run this linter and read its findings.",
    ),
    "parallel_directives": (
        ("parallel",),
        "The host renders this parallel model's directives and knobs into the generated source.",
    ),
}


class Backend(NamedTuple):
    """One declared value of one axis."""

    axis: str
    backend_id: str
    #: Dotted module path of the backend package, or ``None`` while its knowledge is still
    #: inlined in the neutral core (see the migration ledger in ``TODO.md``).
    module: str | None
    #: The `CAPABILITIES` the neutral core carries for this value today. Empty means nothing
    #: inlined does this value's work — which, for a record with `module=None`, is the honest
    #: state of a value that was registered and implemented nowhere. That state must not be
    #: mistaken for a runnable one, which is why it is a declared set rather than something
    #: inferred from `module`.
    core_provides: frozenset[str] = frozenset()

    @property
    def extracted(self) -> bool:
        return self.module is not None

    @property
    def implemented(self) -> bool:
        """The value has code, wherever it lives — an extracted package, or the neutral core."""
        return self.module is not None or bool(self.core_provides)


_BACKENDS: dict[tuple[str, str], Backend] = {
    (b.axis, b.backend_id): b
    for b in (
        # This record is extracted for signature parse/render AND carries capabilities, because
        # extraction and capability are independent: the neutral core still holds this value's
        # control-file compile rules and its runner render (two open ledger areas).
        Backend(
            "language", "fortran", "tools.backends.language.fortran",
            core_provides=frozenset({"control_file", "runner_render"}),
        ),
        Backend(
            "build_system", "make", None,
            core_provides=frozenset({"control_file", "build_execute"}),
        ),
        Backend("compiler", "gfortran", None, core_provides=frozenset({"syntax_check"})),
        # The linter members ARE the presets the `Generate` lint evidence gate accepts: that gate
        # asks `unimplemented_reason("linter", ...)` and holds no set of its own, so this is the
        # only place the accepted presets are written. Listing only `fortitude` here would
        # narrow the live gate.
        Backend("linter", "fortitude", None, core_provides=frozenset({"lint"})),
        Backend("linter", "cppcheck", None, core_provides=frozenset({"lint"})),
        Backend("linter", "ruff", None, core_provides=frozenset({"lint"})),
        Backend("linter", "mixed", None, core_provides=frozenset({"lint"})),
        Backend("parallel", "openmp", None, core_provides=frozenset({"parallel_directives"})),
        # A node that declares no parallel model. It exists as a member so the axis has a
        # spelling for "serial" alongside its open vocabulary, and it carries the capability
        # because the neutral core does implement it: rendering no directive is what the
        # conductor already does for it, so a node declaring it runs today.
        Backend("parallel", "none", None, core_provides=frozenset({"parallel_directives"})),
    )
}


def _check_declarations() -> None:
    """Fail at import on a declaration this module's own vocabulary does not admit.

    A capability spelled wrong, or declared on an axis it is not a question of, would answer
    `False` forever at a dispatch site that means to answer `True` — a silent authorship flip,
    which is the failure this file exists to prevent. It is cheap to refuse the module instead,
    and unlike a test it cannot be bypassed by importing the module directly.
    """
    for backend in _BACKENDS.values():
        for capability in sorted(backend.core_provides):
            axes_for = CAPABILITIES.get(capability)
            if axes_for is None:
                raise UnsupportedBackend(
                    f"{backend.axis}/{backend.backend_id} declares unknown capability "
                    f"'{capability}' (declared capabilities: {', '.join(sorted(CAPABILITIES))})"
                )
            if backend.axis not in axes_for[0]:
                raise UnsupportedBackend(
                    f"{backend.axis}/{backend.backend_id} declares '{capability}', which is a "
                    f"question of the {', '.join(axes_for[0])} axis only"
                )


_check_declarations()


def _require_axis(axis: str) -> Axis:
    try:
        return AXES[axis]
    except KeyError:
        raise UnsupportedBackend(
            f"unknown target-stack axis '{axis}' (declared axes: "
            f"{', '.join(sorted(AXES))}); see docs/BACKEND_BOUNDARY.md"
        ) from None


def backend_ids(axis: str) -> tuple[str, ...]:
    """The backend ids this repository implements for `axis`, sorted."""
    _require_axis(axis)
    return tuple(sorted(bid for (ax, bid) in _BACKENDS if ax == axis))


def _require_capability(axis: str, capability: str) -> None:
    axes_for = CAPABILITIES.get(capability)
    if axes_for is None:
        raise UnsupportedBackend(
            f"unknown capability '{capability}' (declared: {', '.join(sorted(CAPABILITIES))}); "
            f"see docs/BACKEND_BOUNDARY.md"
        )
    if axis not in axes_for[0]:
        raise UnsupportedBackend(
            f"capability '{capability}' is a question of the {', '.join(axes_for[0])} axis, "
            f"not {axis}"
        )


def provides(axis: str, backend_id: str, capability: str) -> bool:
    """True when the NEUTRAL CORE carries `capability` for this axis value.

    The question a host-authorship dispatch has: may I run my own inlined writer on this node.
    A value with no record — unknown, or an open-vocabulary token — answers False, so the
    dispatch declines rather than writing something for a value it knows nothing about.

    An unknown capability, or one asked of the wrong axis, RAISES. It is a typo in the caller,
    and answering False for it would silently turn a dispatch off — the same authorship flip a
    padded axis value used to cause.
    """
    _require_axis(axis)
    _require_capability(axis, capability)
    backend = _BACKENDS.get((axis, str(backend_id or "").strip().lower()))
    return backend is not None and capability in backend.core_provides


def missing_capability_reason(axis: str, backend_id: str, capability: str) -> str | None:
    """`None` when `provides` holds; otherwise the clause a gate refusing on this ground carries.

    Names the axis, the value, what the neutral core would have had to implement, and the values
    it does implement it for — so a gate does not spell its own set (docs/BACKEND_BOUNDARY.md).
    """
    if provides(axis, backend_id, capability):
        return None
    able = ", ".join(
        bid for bid in backend_ids(axis) if capability in _BACKENDS[(axis, bid)].core_provides
    ) or "no value of this axis"
    # The capability's PROSE is not repeated here. A violation string is read by an author
    # deciding what to re-write, and `CAPABILITIES` is where the job is described; two clauses
    # each carrying a paragraph made the message longer than the artifact it is about.
    return (
        f"this repository implements '{capability}' for {axis} {able}, not '{backend_id}' "
        f"(add it under tools/backends/{axis}/ and register it in tools/backends/registry.py "
        f"— see docs/BACKEND_BOUNDARY.md)"
    )


def implemented_backend_ids(axis: str) -> tuple[str, ...]:
    """The backend ids of `axis` this repository implements today, sorted.

    `backend_ids` is the DECLARED set; this is the subset something can actually run — the
    set-shaped form of `unimplemented_reason`, for the gates that need the whole set rather than
    a verdict on one value. Registering a member with no code does not widen it.
    """
    return tuple(bid for bid in backend_ids(axis) if _BACKENDS[(axis, bid)].implemented)


def get(axis: str, backend_id: str) -> Backend:
    """The `Backend` record, or raise naming why there is none.

    The two raises are classified the same way `require_available` classifies them, because the
    same input reaching two entry points must not be two different kinds of failure: a value
    that is not a member at all is `UnsupportedBackend`, and a value an `open_vocabulary` axis
    accepts but has no record for is `BackendNotExtracted`. The first version built its message
    as ``unsupported_reason(...) or ""`` and raised the EMPTY STRING for the second case, while
    its own docstring promised a message naming what is implemented.
    """
    _require_axis(axis)
    normalized = str(backend_id or "").strip().lower()
    backend = _BACKENDS.get((axis, normalized))
    if backend is not None:
        return backend
    reason = unsupported_reason(axis, backend_id)
    if reason is not None:
        raise UnsupportedBackend(reason)
    raise BackendNotExtracted(_no_record_reason(axis, backend_id))


def _no_record_reason(axis: str, backend_id: str) -> str:
    return (
        f"'{backend_id}' is an accepted {axis} value but has no backend package; "
        f"extract one under tools/backends/{axis}/ and register it in "
        f"tools/backends/registry.py — see docs/BACKEND_BOUNDARY.md"
    )


def unsupported_reason(axis: str, backend_id: str) -> str | None:
    """`None` when `backend_id` is a declared member of `axis`; otherwise the reason.

    MEMBERSHIP ONLY. A member declared with `module=None` answers `None` here, because it IS
    declared — the axis value is one this repository knows. A caller deciding whether a node may
    run must ask `unimplemented_reason`, and one about to RUN backend code
    `unavailable_reason`; the two were one function for one
    review round, and in that round registering a second `language` member with `module=None`
    silently stopped the signature gates refusing while the renderer under them was still
    Fortran. Membership and usability are different questions and each caller wants exactly one
    of them.

    Returned rather than raised because the callers that need it most are the deterministic
    gates, which do not raise on a content failure — they append a violation string whose
    prefix (the artifact path) is theirs to choose and whose routing depends on the list it
    lands in. A gate that had to catch an exception to build that string would be spelling the
    reason a second time, which is the drift this repository keeps paying for.

    An `open_vocabulary` axis answers `None` for any non-empty token: `_BACKENDS` lists the
    members that have code, and the artifact carrying that axis deliberately does not constrain
    its value, so a membership test there would refuse values the schema exists to allow.
    """
    spec = _require_axis(axis)
    normalized = str(backend_id or "").strip().lower()
    if (axis, normalized) in _BACKENDS:
        return None
    if spec.open_vocabulary and normalized:
        return None
    implemented = ", ".join(backend_ids(axis))
    return (
        f"'{backend_id}' is not an implemented {axis} backend (implemented: {implemented}); "
        f"add one under tools/backends/{axis}/ and register it in tools/backends/registry.py "
        f"— see docs/BACKEND_BOUNDARY.md"
    )


def unimplemented_reason(axis: str, backend_id: str) -> str | None:
    """`None` when `backend_id` is declared for `axis` AND this repository implements it.

    Implemented means the code exists — in the backend package (`extracted`) or still in the
    neutral core, which is what a declared `core_provides` capability asserts. This is the
    question a NODE-ACCEPTANCE gate has: may a run carry
    this axis value at all. `unavailable_reason` is not that question — it refuses every value
    whose backend has not been extracted yet, which today is most of them, so a gate asking it
    would reject every node this repository can actually build.

    `unsupported_reason` is not that question either, and the difference is the whole point of
    this function. Membership answers `None` for a value that was added to `_BACKENDS` and has
    no code anywhere; a gate guarding on membership alone would accept such a node and hand it
    to whichever backend the surrounding module hard-codes. Registering a member is therefore
    inert here until its `module` or its `core_provides` says where the code is.

    Returned rather than raised for `unsupported_reason`'s reason: the callers are deterministic
    gates that append a violation string rather than raising.
    """
    reason = unsupported_reason(axis, backend_id)
    if reason is not None:
        return reason
    normalized = str(backend_id or "").strip().lower()
    backend = _BACKENDS.get((axis, normalized))
    if backend is None:
        # An open-vocabulary axis accepted a token with no record: nothing implements it.
        return _no_record_reason(axis, backend_id)
    if backend.implemented:
        return None
    return (
        f"the {axis} backend '{backend.backend_id}' is declared but nothing implements it: "
        f"it has no backend package and no code in the neutral core, so a node naming it "
        f"cannot run (rule: docs/BACKEND_BOUNDARY.md)"
    )


def unavailable_reason(axis: str, backend_id: str) -> str | None:
    """`None` when `backend_id` is declared for `axis` AND its code has been extracted.

    This is the question a caller that is about to run backend code has — a signature renderer,
    a source scanner, a control-file writer. Neither other question is that one: `unsupported_reason`
    answers `None` for a declared-but-unextracted member, whose code by definition still sits
    in the neutral core behind a hard-coded import of some OTHER backend, and
    `unimplemented_reason` answers `None` for it too — it says the value RUNS, not that it runs
    through a backend package. A gate that guarded a
    Fortran-only renderer on either of them would let a `cpp` node through and pin its
    signatures by rendering them as Fortran.
    """
    reason = unsupported_reason(axis, backend_id)
    if reason is not None:
        return reason
    normalized = str(backend_id or "").strip().lower()
    backend = _BACKENDS.get((axis, normalized))
    if backend is None:
        # An open-vocabulary axis accepted a token with no record. Such an axis has no extracted
        # code to offer either, so say so rather than implying it does.
        return _no_record_reason(axis, backend_id)
    if backend.extracted:
        return None
    return (
        f"the {axis} backend '{backend.backend_id}' is declared but not extracted: its "
        f"knowledge still sits in the neutral core, so nothing can run it (migration ledger: "
        f"TODO.md, rule: docs/BACKEND_BOUNDARY.md)"
    )


def require_supported(axis: str, backend_id: str) -> None:
    """Raise `UnsupportedBackend` unless `backend_id` is a declared member of `axis`."""
    reason = unsupported_reason(axis, backend_id)
    if reason is not None:
        raise UnsupportedBackend(reason)


def require_implemented(axis: str, backend_id: str) -> None:
    """Raise unless `backend_id` is declared for `axis` AND something implements it.

    Classified exactly as `require_available` classifies the same inputs — a non-member is
    `UnsupportedBackend`, a member with no code is `BackendNotExtracted` — so an input cannot be
    one kind of failure at one entry point and another kind at the next.
    """
    reason = unimplemented_reason(axis, backend_id)
    if reason is None:
        return
    if unsupported_reason(axis, backend_id) is not None:
        raise UnsupportedBackend(reason)
    raise BackendNotExtracted(reason)


def require_available(axis: str, backend_id: str) -> None:
    """Raise unless `backend_id` is declared for `axis` AND extracted."""
    reason = unavailable_reason(axis, backend_id)
    if reason is None:
        return
    if unsupported_reason(axis, backend_id) is not None:
        raise UnsupportedBackend(reason)
    raise BackendNotExtracted(reason)


def load(axis: str, backend_id: str) -> ModuleType:
    """Import and return the backend package.

    `UnsupportedBackend` when the axis value is not a member; `BackendNotExtracted` when it is a
    member (or an open-vocabulary value) whose knowledge still lives in the neutral core. The
    classification is `require_available`'s rather than this function's own, so the same input
    cannot be one kind of failure here and another kind there — it was, for one review round.
    """
    require_available(axis, backend_id)
    module = _BACKENDS[(axis, str(backend_id or "").strip().lower())].module
    assert module is not None  # `require_available` returned, so the record is extracted
    return importlib.import_module(module)
