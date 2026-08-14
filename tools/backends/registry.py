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


class Backend(NamedTuple):
    """One implemented value of one axis."""

    axis: str
    backend_id: str
    #: Dotted module path of the backend package, or ``None`` while its knowledge is still
    #: inlined in the neutral core (see the migration ledger in ``TODO.md``).
    module: str | None

    @property
    def extracted(self) -> bool:
        return self.module is not None


_BACKENDS: dict[tuple[str, str], Backend] = {
    (b.axis, b.backend_id): b
    for b in (
        Backend("language", "fortran", "tools.backends.language.fortran"),
        Backend("build_system", "make", None),
        Backend("compiler", "gfortran", None),
        # The linter members are the presets `validate_pipeline_semantics._LINT_ALLOWED_PRESETS`
        # already accepts. Listing only `fortitude` here would have made this registry disagree
        # with the live gate about which linters exist.
        Backend("linter", "fortitude", None),
        Backend("linter", "cppcheck", None),
        Backend("linter", "ruff", None),
        Backend("linter", "mixed", None),
        Backend("parallel", "openmp", None),
        # A node that declares no parallel model. It has no code of its own — it exists as a
        # member so the axis has a spelling for "serial" alongside its open vocabulary.
        Backend("parallel", "none", None),
    )
}


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


def get(axis: str, backend_id: str) -> Backend:
    """The `Backend` record, or raise `UnsupportedBackend` naming what IS implemented."""
    _require_axis(axis)
    key = (axis, str(backend_id or "").strip().lower())
    try:
        return _BACKENDS[key]
    except KeyError:
        raise UnsupportedBackend(unsupported_reason(axis, backend_id) or "") from None


def unsupported_reason(axis: str, backend_id: str) -> str | None:
    """`None` when `backend_id` is a declared member of `axis`; otherwise the reason.

    MEMBERSHIP ONLY. A member declared with `module=None` answers `None` here, because it IS
    declared — the axis value is one this repository knows. A caller that is about to RUN
    backend code must ask `unavailable_reason` instead; the two were one function for one
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


def unavailable_reason(axis: str, backend_id: str) -> str | None:
    """`None` when `backend_id` is declared for `axis` AND its code has been extracted.

    This is the question a caller that is about to run backend code has — a signature renderer,
    a source scanner, a control-file writer. `unsupported_reason` is not that question: it
    answers `None` for a declared-but-unextracted member, whose code by definition still sits
    in the neutral core behind a hard-coded import of some OTHER backend. A gate that guarded a
    Fortran-only renderer on membership alone would let a `cpp` node through and pin its
    signatures by rendering them as Fortran.
    """
    reason = unsupported_reason(axis, backend_id)
    if reason is not None:
        return reason
    normalized = str(backend_id or "").strip().lower()
    backend = _BACKENDS.get((axis, normalized))
    if backend is None or backend.extracted:
        # `None` here means an open-vocabulary axis accepted a token with no record. Such an
        # axis has no extracted code to offer either, so say so rather than implying it does.
        if backend is None:
            return (
                f"'{backend_id}' is an accepted {axis} value but has no backend package; "
                f"extract one under tools/backends/{axis}/ and register it in "
                f"tools/backends/registry.py — see docs/BACKEND_BOUNDARY.md"
            )
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

    `UnsupportedBackend` when the axis value is not implemented; `BackendNotExtracted` when it
    is implemented but its knowledge still lives in the neutral core.
    """
    backend = get(axis, backend_id)
    if backend.module is None:
        raise BackendNotExtracted(
            f"the {axis} backend '{backend.backend_id}' is implemented but not extracted: its "
            "knowledge is still inlined in the neutral core, so there is no module to load "
            "(migration ledger: TODO.md, rule: docs/BACKEND_BOUNDARY.md)"
        )
    return importlib.import_module(backend.module)
