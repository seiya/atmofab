"""The Fortran backend's `runner_render` capability: a physics node's host-authored runner glue.

Pure-function module reached through `registry.capability_module("language", <lang>,
"runner_render")` — the neutral seam is `tools/host_render.py`, which is what the conductor and
the compile gate call. Nothing outside this package imports it by name.

It takes a compiled IR plus the target/harness
spec ids and returns the text of ``<spec_id>_runner.f90`` — the deterministic
"glue" main program that drives the physics node's ``<spec_id>_checks`` callbacks
and emits the standard runner outputs *through the certified harness's
plumbing* (``harness_fortran_cpu``, or ``harness_fortran_cpu_mpi``, over which the
runner is the distributed variant — issue #316: it starts and ends the runtime, gathers
the partitioned state onto rank 0 and writes on rank 0 only). Because the harness's interface owns the
JSON envelope assembly (§3 / §5.1 of the harness controlled_spec), this renderer holds
**no serialization knowledge**: it calls the writers — it never formats a JSON token, and it
judges nothing (the host's predicates over the captures do, issue #582).

Split of authorship on an M3c node:
- ``<spec_id>_model.f90`` — the physics kernel + ``__apply`` op   (LLM leaf)
- ``<spec_id>_checks.f90`` — the fixed-ABI driving callbacks + the
  module-level state storage the snapshot is captured from   (LLM leaf)
- ``<spec_id>_runner.f90`` — this renderer                        (host)
- ``src/Makefile``          — ``workflow_conductor._write_makefile`` (host)

The rendered runner ``use``s two modules: ``<harness_spec_id>_model`` (the
certified plumbing) and ``<spec_id>_checks`` (the leaf's fixed-ABI callbacks AND
its bound state storage, see ``docs/workflow/CHECKS_MODULE_CONTRACT.md``). Snapshot
capture is the runner's, not the module's (Z6, issue #255): every snapshot variable
is a module-level ``real(dp)`` variable of ``<spec_id>_checks`` named exactly as the IR
declares it, imported as ``sb_<var> => <var>`` and serialized by the certified harness
emitters twice per case — right after ``case_setup`` (``raw/state_snapshots/initial/
<case_id>.json``) and right after ``case_run`` (``raw/state_snapshots/<case_id>.json``)
— before any later callback of that case runs. Generated code contributes
the binding (the declaration and the allocation) and nothing downstream of it: there
is no getter, so no generated procedure can filter, compute or rewrite a captured
value (``zero_base_architecture.md`` §A4). It is authored lint-clean
(``use only:``, a bare ``implicit none`` with NO allow directive, ≤99-column
lines) so the deterministic
Generate.gate lint checker — which lints the whole ``src/`` tree — stays green.

``render_runner`` raises ``RenderError`` (→ transport fail_closed, NOT a Generate
retry) for any IR it cannot faithfully render: an unparseable ``shape_expr``, a
rank>4 snapshot variable, a snapshot variable colliding with a harness-reserved
key, more than one
infrastructure dependency, or an over-long identifier. ``assert_harness_pin``
(the signature pin) is a separate fail-closed guard the conductor runs against
the *certified* harness IR signatures + source before rendering.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Neutral policy this emitter also enforces at render time.
#
# `RenderError` is the seam's class, not this module's, and importing it is what makes the
# neutral `except RenderError:` clause in the conductor work: a class obtained through
# `registry.load` is a different object to the one an `except` in a neutral module names.
# `runner_ir` holds the language-neutral IR readers every runner renderer shares (the case-id
# token grammar among them: a case id reaches a filesystem path and an argv, neither of which is a
# language question).
from tools import runner_ir
from tools.backends.language.fortran import bundle
from tools.host_execution import perf_parallelism
from tools.host_render import RenderError

# The fixed ABI of the leaf-authored `<spec_id>_checks` module (see
# docs/workflow/CHECKS_MODULE_CONTRACT.md). Non-prefixed public names (module
# scope makes them collision-free) so the f2008 63-char identifier limit is not
# exceeded even for a 55-char spec_id. Three procedures (the judging half went in issue #582: a test is decided by host predicates over the captures): the former snapshot getters
# (`get_scalar` / `get_r1..r4`) are retired — the runner reads the bound module-level
# state directly (`STATE_BINDING_PREFIX`), so a value-returning getter is
# unrepresentable rather than policed (Z6, issue #255).
CHECKS_PUBLIC_NAMES = (
    "case_setup", "case_run", "get_time",
)

# The runner-local alias of a bound snapshot variable: `use <spec_id>_checks, only:
# sb_<var> => <var>`. The prefix keeps the alias clear of every runner local (none starts
# with `sb_`) and of the ABI names, so an IR variable named like a runner local (`i`,
# `ok`, `vals`) still renders. `_snapshot_schema` bounds `sb_<var>` at the identifier limit.
STATE_BINDING_PREFIX = "sb_"
_IDENTIFIER_RE = re.compile(bundle.IDENTIFIER_PATTERN)

# Harness-owned snapshot keys a physics snapshot variable must not shadow (the neutral IR reader's,
# `tools/runner_ir.py`, re-exported for the tests and callers that read them here).
HARNESS_RESERVED_SNAPSHOT_KEYS = runner_ir.HARNESS_RESERVED_SNAPSHOT_KEYS

# The fixed width of a parsed case id — the harness's `case_id_len`, which the neutral IR reader
# owns (`runner_ir.CASE_ID_LEN`) because it bounds a declared case id for every renderer. The
# rendered runner declares its `case_ids(:)` buffer `character(len=case_id_len)` and passes it to
# `__parse_cases` as an `intent(out)` actual, so this MUST equal the harness's own `case_id_len`
# (harness controlled_spec §3 pins the value; an assumed-length `intent(out)` character dummy is
# disallowed, which is why the width is fixed at all). `assert_harness_pin` enforces that equality
# against the certified harness source, so the two cannot drift.
CASE_ID_LEN = runner_ir.CASE_ID_LEN

# The bound on a spec_id is the language's (`bundle.SPEC_ID_MAX`, where its derivation is
# stated); the render-time check below is the backstop to the neutral spec-input gate.
MAX_SPEC_ID_LEN = bundle.SPEC_ID_MAX


def max_rendered_line() -> int:
    """The deterministic Generate.gate lint checker's column limit for this language — the
    linter's own declaration (`LINE_LENGTH_LIMIT` of the linter `registry.linter_for_language`
    names), read through the registry rather than restated here (issue #424). The rendered
    runner must stay strictly under it because it is host-authored (a leaf cannot edit it to fix
    an overlong line). A backend may import the neutral core; the registry loads the linter
    package lazily, so this module still imports no other backend at import time.

    A failure here is a host defect (two linters declaring this language, or the linter's
    module lacking the attribute), and inside the Compile dry render `ir_content_violations`'
    catch-all reports it as "IR is not renderable" — the `compile_static_violation` routing
    residue TODO.md records for a registry/package disagreement. The first shape raises out of
    the launch host prerequisite probe (`host_prerequisites.resolve_launch_axis_selection` asks
    `linter_for_language`), so it does not reach a run. Nothing on the run path refuses the
    second before the Compile dry render; what catches it is the suite, where most of
    `test_fortran_runner` turns red."""
    from tools.backends import registry

    linter = registry.linter_for_language("fortran")
    return int(registry.capability_module("linter", linter, "lint").LINE_LENGTH_LIMIT)

# The harness symbols this template calls (pinned by assert_harness_pin against
# the certified harness IR). Emitters are added per-rank on demand.
_HARNESS_TYPES = (
    "h_named",
)
_HARNESS_CORE_OPS = (
    "parse_cases", "box", "write_snapshot", "write_perf",
)

# The distributed-state operations the RUNNER calls on a harness that drives the program as
# several ranks (`distributed_state@2`, issues #316 and #512): it starts and ends the runtime, asks its rank
# (only rank 0 writes) and the run's size (the perf record's rank count, observed rather than
# configured), sums the ranks' updated-cell counts, and gathers each distributed bound array onto
# rank 0 before it is serialized (`gather_r<k>`, added per array rank on demand).
_DISTRIBUTED_RUNNER_OPS = ("init", "finalize", "comm_rank", "comm_size", "reduce_sum_int")

#: The distributed-state operations a PHYSICS source (the leaf's model or checks module) may call
#: on such a harness, and the only harness names it may `use` (`source.
#: checks_harness_isolation_violations`). The runner keeps the rest: the start and end of the
#: runtime and the gather are the capture's, and the writers and emitters are the runner's alone
#: (Z6) — a physics source that reached them could write its own evidence.
PHYSICS_CALLABLE_DISTRIBUTED_OPS: tuple[str, ...] = (
    "comm_rank", "comm_size", "grid_dims", "grid_coord", "partition", "exchange_halo_r1",
    "exchange_halo_r2",
    "reduce_sum", "reduce_max", "reduce_min", "reduce_sum_int",
)

#: The per-variable module variables a checks module publishes beside each bound ARRAY on a
#: distributed harness (`docs/backends/language/fortran/CHECKS_ABI.md` §1-c), one group per
#: dimension `<d>` of the process grid (issue #512): the axis of the array partitioned along
#: grid dimension `<d>` (0 = not partitioned along it; 0 in both groups = replicated, rank 0's
#: copy is the global value), the indices, in the array's own bounds, of the cells this rank
#: contributes to the global array along that axis, and the global index of the first of them.
#: Named `sb_<var>_<suffix>` in the checks module itself, and imported under that name.
DISTRIBUTED_BINDING_SUFFIXES: tuple[str, ...] = (
    "axis1", "lo1", "hi1", "glo1", "axis2", "lo2", "hi2", "glo2")


def distributed_binding_names(variable: str) -> list[str]:
    """The metadata names a checks module publishes for the bound array `variable`."""
    return [f"{STATE_BINDING_PREFIX}{variable}_{s}" for s in DISTRIBUTED_BINDING_SUFFIXES]


# --- IR extraction helpers ---------------------------------------------------------------------
#
# The language-neutral readers live in `tools/runner_ir.py` (issue #289, R4-b PR-6), shared with
# every other backend that renders a runner, so the refusals of a neutral IR fact are one set for
# every target. What stays here is what Fortran adds: the snapshot-name rules of its binding, and
# the bound the linter's column limit implies (`max_rendered_line`).

_dget = runner_ir.dget
_rank_of_shape = runner_ir.rank_of_shape
_case_ids = runner_ir.case_ids
_target_class = runner_ir.target_class
_infra_dep_count = runner_ir.infra_dep_count


def _snapshot_schema(ir: dict[str, Any]) -> tuple[dict[str, str], str]:
    """Return ``({var_name: shape_expr}, time_variable)`` from the IR's snapshot schema
    (`runner_ir.snapshot_schema`), with this binding's rules on each variable name.

    Same section `_author_snapshot_schema` (workflow_conductor) reads."""
    seen_folded: dict[str, str] = {}
    abi_folded = {n.casefold() for n in CHECKS_PUBLIC_NAMES}

    def check_name(name: str) -> None:
        # A snapshot variable IS a module-level variable of `<spec_id>_checks` (the binding
        # convention), imported by the runner as `sb_<name> => <name>`: so the name must be a
        # legal identifier, the alias must fit the identifier limit, two names may not fold to
        # one identifier (Fortran is case-insensitive: `U` and `u` would be one variable, and
        # `use ..., only: sb_U => U, sb_u => u` would alias one storage twice), and it may not
        # fold to an ABI procedure name (a module cannot hold a variable and a procedure of one
        # name, so no checks module could be written for such an IR).
        if not _IDENTIFIER_RE.fullmatch(name):
            raise RenderError(
                f"snapshot variable {name!r} is not a bindable identifier: every snapshot "
                "variable is captured from a module-level variable of that name in the "
                f"checks module, so it must match {bundle.IDENTIFIER_PATTERN}")
        alias = f"{STATE_BINDING_PREFIX}{name}"
        if len(alias) > bundle.IDENTIFIER_MAX:
            raise RenderError(
                f"snapshot variable {name!r} is {len(name)} chars; its runner alias "
                f"{alias!r} exceeds the {bundle.IDENTIFIER_MAX}-char identifier limit")
        folded = name.casefold()
        if folded in seen_folded:
            raise RenderError(
                f"snapshot variables {seen_folded[folded]!r} and {name!r} are one identifier "
                "to the checks module (identifiers are case-insensitive), so they cannot both "
                "be bound")
        seen_folded[folded] = name
        if folded in abi_folded:
            raise RenderError(
                f"snapshot variable {name!r} collides with a checks-ABI procedure name "
                f"{list(CHECKS_PUBLIC_NAMES)}; a module cannot hold a variable of that name "
                "beside the procedure")

    return runner_ir.snapshot_schema(ir, check_name)


def _threads(target: dict[str, Any]) -> int:
    """The threads per rank the run executes with — the target profile's `execution`, which
    is what `run_program` is told. Until R4-a PR-2 this read the IR's
    `backend_overrides.openmp.num_threads`, a count the run never used. Only the DISTRIBUTED
    variant's perf record writes it (a launcher target runs one thread per rank, so every
    shape's count is 1); the single-process harness's `__write_perf` takes no thread count and
    writes the one the run's shared-memory runtime gives the program, obtained at run time
    (`harness_fortran_cpu@0.9.0` §3.2, issue #512)."""
    return int(target["execution"]["threads_per_rank"])


#: The target a DRY render is given: `ir_content_violations` renders only to learn whether the
#: IR renders, and discards the text. Both values reach nothing but the perf-record line, which
#: raises for no value, so they decide no violation.
_DRY_RUN_TARGET: dict[str, Any] = {"hardware": {"class": "cpu"},
                                   "execution": {"threads_per_rank": 1}}



# --- code assembly ------------------------------------------------------------


def _hname(harness_spec_id: str, sym: str) -> str:
    return f"{harness_spec_id}__{sym}"


def _flit(value: str) -> str:
    """Escape a string for embedding inside a single-quoted Fortran character literal.

    IR-sourced names (case_ids, snapshot variable names, test_ids)
    are only required to be non-empty by the compile gates, not to be Fortran
    identifiers, so a name containing a `'` would otherwise break the generated literal
    (`case ('a'b')`). Fortran escapes an embedded apostrophe by doubling it.

    Everything embedded must be **printable ASCII**. A control character (newline/tab)
    cannot appear in a literal at all. A non-ASCII character is worse than it looks: the
    Fortran default character kind counts BYTES, while every length this renderer reasons
    about — the ``CASE_ID_LEN`` bound, the linter's column limit — counts Python code
    points. A 64-code-point case_id that is 68 UTF-8 bytes therefore slips past the bound
    and is silently truncated into the harness's ``character(len=64)`` slot, producing a
    runner that compiles and then ``error stop``s on every run. Reject the whole class
    here, where one check covers every embedded name."""
    bad = sorted({ch for ch in value if not (0x20 <= ord(ch) <= 0x7E)})
    if bad:
        raise RenderError(
            f"value {value!r} contains character(s) {bad!r} outside printable ASCII and cannot "
            "be embedded in the generated Fortran source (a control character has no literal "
            "form; a non-ASCII character makes the byte length disagree with the code-point "
            "length every render bound is measured in)")
    return value.replace("'", "''")


def _ranks_used(ir: dict[str, Any]) -> set[int]:
    """The snapshot-variable ranks (0..4) the schema declares — every variable is captured
    for every case — so both the renderer and the signature pin agree on which emitters the
    glue depends on."""
    schema_vars, _ = _snapshot_schema(ir)
    return {_rank_of_shape(shape, v) for v, shape in schema_vars.items()}


def _used_harness_ops(ir: dict[str, Any], *, distributed: bool = False) -> list[str]:
    """Unqualified harness op names the rendered glue calls (deterministic order):
    the core writers/plumbing plus only the emitters for the ranks in use — and, on a
    distributed harness, the runner's distributed-state operations plus only the gathers for
    the array ranks in use."""
    ranks = _ranks_used(ir)
    array_ranks = sorted(r for r in ranks if r >= 1)
    ops = ["parse_cases"]
    if 0 in ranks:
        ops.append("emit_real")
    ops += [f"emit_array_r{r}" for r in array_ranks]
    ops += ["box", "write_snapshot", "write_perf"]
    if distributed:
        ops += [*_DISTRIBUTED_RUNNER_OPS, *(f"gather_r{r}" for r in array_ranks)]
    return ops


def _check_identifier_lengths(spec_id: str, harness_spec_id: str) -> None:
    # These are node-IDENTITY defects (a re-author cannot shorten the spec_id / harness id),
    # so they are `identity=True`: the compile.static mirror excludes them (spec-input concern).
    if len(spec_id) > MAX_SPEC_ID_LEN:
        raise RenderError(
            f"spec_id {spec_id!r} is {len(spec_id)} chars (>{MAX_SPEC_ID_LEN}); "
            "the derived `<spec_id>_checks`/`_runner` identifiers would risk the "
            f"f2008 {bundle.IDENTIFIER_MAX}-char limit", identity=True)
    for derived in (f"{spec_id}_runner", f"{spec_id}_checks", f"{spec_id}_model"):
        if len(derived) > bundle.IDENTIFIER_MAX:
            raise RenderError(
                f"identifier {derived!r} is {len(derived)} chars (>{bundle.IDENTIFIER_MAX})",
                identity=True)
    # The distributed operations are bounded with the plumbing: `exchange_halo_r1` is longer than
    # any plumbing name, and a consumer's model calls it.
    for sym in (*_HARNESS_TYPES, *_HARNESS_CORE_OPS, *_DISTRIBUTED_RUNNER_OPS,
                *PHYSICS_CALLABLE_DISTRIBUTED_OPS, "gather_r4"):
        name = _hname(harness_spec_id, sym)
        if len(name) > bundle.IDENTIFIER_MAX:
            raise RenderError(
                f"harness identifier {name!r} is {len(name)} chars (>{bundle.IDENTIFIER_MAX})",
                identity=True)


def ir_content_violations(ir: dict[str, Any], spec_id: str, harness_spec_id: str) -> list[str]:
    """The Compile-authored render preconditions of an M3c physics node's host-rendered runner,
    as a list of human-readable messages (``[]`` when the IR renders, or when the only defect is
    a node-identity one this deliberately excludes — see below).

    The ``compile.static`` gate (``validate_pipeline_semantics._validate_harness_render_pre
    conditions``) calls this so a defect in an M3c node's IR routes back to ``compile.generate``
    (a cheap warm re-author) instead of surfacing only as ``render_runner``'s Generate-time
    fail_closed — which, running inside the conductor's host render, kills the whole workflow
    rather than retrying (the E2E #3 ``time_variable`` failure class: a Compile-authored value ×
    a renderer fail-close at an unrecoverable position).

    It is an EXACT mirror by construction: it invokes ``render_runner`` itself (a pure,
    side-effect-free, deterministic ~ms render) with the SAME ``(ir, spec_id, harness_spec_id)``
    the conductor's ``_write_runner`` passes, and reports whatever ``RenderError`` the render
    raises. No hand-maintained list of preconditions to drift out of sync — every current and
    future content fail-close (reserved-key collision, rank>4, a control char in
    an IR name, a rendered line over the linter's column limit, …) is caught here the instant the renderer
    rejects it.

    It EXCLUDES only ``RenderError``s flagged ``identity=True`` — the node-identity defects a
    re-author cannot repair (``_check_identifier_lengths``: spec_id/derived-name length; and >1
    infra dep, itself unreachable: since R4-a PR-3, issue #284, an IR declares no infra dep and
    the harness is the target's). Those belong to spec-input validation, NOT a compile.generate retry; they
    remain ``render_runner`` fail-closes as a backstop (see the module docstring)."""
    try:
        render_runner(ir, spec_id, harness_spec_id, target=_DRY_RUN_TARGET)
    except RenderError as exc:
        return [] if exc.identity else [str(exc)]
    except Exception as exc:  # noqa: BLE001
        # `render_runner`'s contract is "bad IR -> RenderError", but a truthy non-iterable IR
        # field (e.g. `test_case_set: 5`, where `_dget(...) or []` yields `5` and `for f in 5`
        # raises TypeError) can still escape as a bare exception. Running INSIDE the compile
        # validator, an uncaught exception would abort `_validate_compile_stage_impl` and discard
        # every violation the sibling gates already collected, replacing the actionable list with
        # a renderer-internals traceback. Convert it to a violation instead: the IR is unrenderable
        # either way, so it routes to compile.generate with an intelligible message.
        return [f"IR is not renderable ({type(exc).__name__}: {exc})"]
    return []


def render_runner(ir: dict[str, Any], spec_id: str, harness_spec_id: str,
                  *, target: dict[str, Any]) -> str:
    """Render ``<spec_id>_runner.f90`` from the IR and the target. Deterministic and pure.

    ``harness_spec_id`` is the certified plumbing module's spec_id (a key of
    ``_HARNESS_PINS``); it selects the variant — the distributed one over a harness that runs the
    program as several ranks. ``target`` is the run's target profile document, read for the perf
    record's hardware class, threads per rank (the distributed variant's alone: the
    single-process harness's writer obtains its own) and device count
    (``host_execution.perf_parallelism``). See module docstring for the
    render-error matrix. The returned text is the complete Fortran source (trailing newline
    included).
    """
    # `spec_id`/`harness_spec_id` empty and IR-not-a-mapping are node-identity/caller defects,
    # not authored content — flag identity so the compile.static mirror excludes them.
    if not isinstance(ir, dict):
        raise RenderError("IR is not a mapping", identity=True)
    spec_id = (spec_id or "").strip()
    harness_spec_id = (harness_spec_id or "").strip()
    if not spec_id:
        raise RenderError("spec_id is empty", identity=True)
    if not harness_spec_id:
        raise RenderError("harness_spec_id is empty", identity=True)
    _check_identifier_lengths(spec_id, harness_spec_id)

    infra = _infra_dep_count(ir)
    if infra > 1:
        raise RenderError(
            f"node declares {infra} infrastructure dependencies; an M3c node depends on "
            "exactly one harness (the runner glue is rendered against a single plumbing "
            "surface)", identity=True)

    # Everything from here down is Compile-authored IR *content*: any RenderError it raises is
    # `identity=False`, so `ir_content_violations` (which invokes this function) surfaces it at
    # compile.static and routes the defect to compile.generate instead of this workflow-killing
    # render. No mirroring to maintain — the gate runs THIS code.
    schema_vars, time_var = _snapshot_schema(ir)
    runner_ir.require_time_variable_t(time_var)
    _case_ids(ir)   # the refusals of a case id (the runner takes the ids from argv)
    target_class = _target_class(target)
    threads = _threads(target)
    # The perf record's device count is the target's (`host_execution.perf_parallelism`). Its
    # rank count is never the configured one, which the post-execute gate compares it with: the
    # single-process variant runs one process and writes 1, and the distributed variant passes
    # the size of the run it observes (`__comm_size`). (Round 3 of the PR's review: writing the
    # profile's count here made that comparison hold by construction.) The thread count is the
    # same kind of fact for the single-process variant, and it passes none: that harness's
    # `__write_perf` obtains the count the shared-memory runtime gives the run itself, because
    # Validate runs one binary at several thread counts and compares each run's record with its
    # own, and the harness's own Validate run then checks the code every consumer records it
    # through (`harness_fortran_cpu@0.9.0` §3.2, issue #512).
    _, _, devices = perf_parallelism(target)
    distributed = _is_distributed_harness(harness_spec_id)

    # ranks the schema declares, so we import only the emitters we call (an unused `use only`
    # name would trip lint). Every snapshot variable is captured for EVERY case (Z6): the
    # snapshot is the full declared state.
    ranks_used = {_rank_of_shape(shape, v) for v, shape in schema_vars.items()}
    has_scalar = 0 in ranks_used
    array_ranks = sorted(r for r in ranks_used if r >= 1)

    H = lambda sym: _hname(harness_spec_id, sym)  # noqa: E731 (local shorthand)

    # ---- module use lists ----
    emit_ops = (["emit_real"] if has_scalar else []) + [f"emit_array_r{r}" for r in array_ranks]
    harness_syms = [
        *[f"{H(t)}" for t in _HARNESS_TYPES],
        H("parse_cases"),
        *[H(op) for op in emit_ops],
        H("box"),
        H("write_snapshot"),
        H("write_perf"),
    ]
    if distributed:
        harness_syms += [H(op) for op in _DISTRIBUTED_RUNNER_OPS]
        harness_syms += [H(f"gather_r{r}") for r in array_ranks]
    checks_syms = ["case_setup", "case_run", "get_time"]
    # The bound state: one `sb_<var> => <var>` rename per snapshot variable (schema declaration
    # order) — the same set the bundle gate requires bound, so a declared state the runner
    # never read cannot exist.
    bound_vars = list(schema_vars)
    limit = max_rendered_line()
    for v in bound_vars:
        one_line = f"    {STATE_BINDING_PREFIX}{v} => {v}, &"
        if len(one_line) < limit:
            checks_syms.append(f"{STATE_BINDING_PREFIX}{v} => {v}")
        else:  # a long name: continue between the alias and the target
            checks_syms.append(f"{STATE_BINDING_PREFIX}{v} => &\n      {v}")
    # On a distributed harness each bound ARRAY brings its partition metadata, imported under
    # the checks module's own names (`distributed_binding_names`). A name that does not fit the
    # identifier limit, or that is one identifier with another bound name, cannot be published
    # beside it — refused here, so the compile gate's dry render reports it for this target.
    distributed_arrays = ([v for v in bound_vars if _rank_of_shape(schema_vars[v], v) >= 1]
                          if distributed else [])
    if distributed:
        taken = {f"{STATE_BINDING_PREFIX}{v}".casefold() for v in bound_vars}
        taken |= {v.casefold() for v in bound_vars}
        for v in distributed_arrays:
            for name in distributed_binding_names(v):
                if len(name) > bundle.IDENTIFIER_MAX:
                    raise RenderError(
                        f"snapshot variable {v!r} is {len(v)} chars; its distributed binding "
                        f"{name!r} exceeds the {bundle.IDENTIFIER_MAX}-char identifier limit")
                if name.casefold() in taken:
                    raise RenderError(
                        f"the distributed binding {name!r} of snapshot variable {v!r} is one "
                        "identifier with another bound name, so the checks module cannot "
                        "publish both")
                taken.add(name.casefold())
                checks_syms.append(name)

    lines: list[str] = []
    a = lines.append

    a("! Deterministic runner glue authored host-side by the conductor (R1/M3c).")
    a("! It drives the physics node's <spec_id>_checks callbacks and emits the standard")
    a(f"! runner outputs THROUGH the certified {harness_spec_id} plumbing, which owns all")
    a("! JSON assembly (harness controlled_spec §3/§5.1). This glue")
    a("! holds no serialization knowledge; it calls the writers.")
    a(f"program {spec_id}_runner")
    a("  use, intrinsic :: iso_fortran_env, only: real64, int64, error_unit")
    a(f"  use {harness_spec_id}_model, only: &")
    for i, sym in enumerate(harness_syms):
        sep = ", &" if i < len(harness_syms) - 1 else ""
        a(f"    {sym}{sep}")
    a(f"  use {spec_id}_checks, only: &")
    for i, sym in enumerate(checks_syms):
        sep = ", &" if i < len(checks_syms) - 1 else ""
        a(f"    {sym}{sep}")
    a("  ! The checks ABI is the same three subroutines for every node.")
    if distributed:
        _render_distributed_abi_comment(a, harness_spec_id)
    # No `! allow(C003)` above it, deliberately, and this is the file where getting it wrong
    # is unrecoverable: the lint gate imposes its rule set with `--ignore-allow-comments`
    # (`tools/backends/linter/fortitude/lint.py`), so a directive here would be reported as
    # FORT005 in a file NO LEAF CAN EDIT — the retry loop would send a leaf to fix a finding
    # it has no write authority over, which is exactly the shape issue #110 recorded. C003
    # is not in the declared set, so a plain `implicit none` is lint-clean.
    a("  implicit none")
    a("")
    a("  integer, parameter :: dp = real64")
    a(f"  integer, parameter :: case_id_len = {CASE_ID_LEN}")
    a("")
    a("  integer :: nargs, i, ci, ln, ncases")
    if distributed:
        a("  integer :: rank")
    a("  logical :: ok, setup_ok, run_ok")
    a("  character(len=512), allocatable :: tokens(:)")
    a("  character(len=case_id_len), allocatable :: case_ids(:)")
    a("")
    a("  integer(int64) :: clock0, clock1, clock_rate")
    a("  real(dp) :: walltime, tval")
    a("  integer :: steps_total, cells_total, steps_c, cells_c")
    a("")
    a(f"  type({H('h_named')}), allocatable :: vals(:)")
    a("")
    # ---- argv marshal + parse ----
    if distributed:
        # Before anything else, as the harness requires; every rank runs the whole program,
        # and only rank 0 writes (the writers are rank-agnostic, harness spec §1).
        a("  ! --- start the message-passing runtime; only rank 0 writes the outputs -------")
        a(f"  call {H('init')}()")
        a(f"  rank = {H('comm_rank')}()")
        a("")
    a("  ! --- read argv and parse the case set (--cases <spec> <case_id>...) --------")
    a("  nargs = command_argument_count()")
    a("  allocate(tokens(max(nargs, 1)))")
    a("  do i = 1, nargs")
    a("    call get_command_argument(i, tokens(i), length=ln)")
    a("  end do")
    a("  allocate(case_ids(max(nargs, 1)))")
    a(f"  call {H('parse_cases')}(tokens, nargs, case_ids, ncases, ok)")
    a("  if (.not. ok) then")
    a("    write(error_unit, '(A)') 'error: --cases <spec> <case_id>... required'")
    a("    error stop 1")
    a("  end if")
    a("")
    a("  steps_total = 0")
    a("  cells_total = 0")
    a("  call system_clock(count=clock0, count_rate=clock_rate)")
    a("")
    # ---- per-case loop ----
    a("  do ci = 1, ncases")
    a("    call case_setup(trim(case_ids(ci)), setup_ok)")
    a("")
    # Z6 capture contract (zero_base_architecture.md §A4): the harness serializes the BOUND
    # state — read straight from the checks module's storage — right after `case_setup`
    # (initial) and right after `case_run` (final), as serialized STRINGS, so nothing a later
    # `get_time` callback writes into that storage can reach a snapshot. There is no getter in
    # between: `get_time` — generated code — is called AFTER the capture, for the time value
    # the snapshot is written with, so no generated procedure runs between `case_setup` /
    # `case_run` returning and the state being serialized.
    a("    ! --- initial state: bound storage serialized right after case_setup, before any")
    a("    ! --- callback of this case runs (raw/state_snapshots/initial/<case_id>.json) ---")
    a("    call capture_state(trim(case_ids(ci)), vals)")
    a("    call get_time(tval)")
    if distributed:
        a("    if (rank == 0) then")
    a(f"    call {H('write_snapshot')}('initial/'//trim(case_ids(ci)), vals, tval)")
    if distributed:
        a("    end if")
    a("    deallocate(vals)")
    a("")
    a("    call case_run(trim(case_ids(ci)), steps_c, cells_c, run_ok)")
    a("    steps_total = steps_total + steps_c")
    a("    cells_total = cells_total + cells_c")
    a("")
    a("    ! --- final state: the same bound storage right after case_run, before any later")
    a("    ! --- callback of this case runs (raw/state_snapshots/<case_id>.json) ---")
    a("    call capture_state(trim(case_ids(ci)), vals)")
    a("    call get_time(tval)")
    if distributed:
        a("    if (rank == 0) then")
    a(f"    call {H('write_snapshot')}(trim(case_ids(ci)), vals, tval)")
    if distributed:
        a("    end if")
    a("    deallocate(vals)")
    a("  end do")
    a("")
    a("  call system_clock(count=clock1)")
    a("  walltime = real(clock1 - clock0, dp) / real(clock_rate, dp)")
    a("  if (walltime <= 0.0_dp) walltime = 1.0e-9_dp")
    a("")
    if distributed:
        # Each rank's `case_run` counts the cells IT updated; the record states the run's.
        a("  cells_total = " + H("reduce_sum_int") + "(cells_total)")
        a("")
        # Everything below reads the values rank 0 captured and writes the outputs, so it is
        # rank 0's alone; the other ranks go straight to the end of the runtime.
        a("  if (rank == 0) then")
    a("  ! --- emit the perf record (harness owns its envelope) ------------------------")
    a(f"  call {H('write_perf')}(trim(case_ids(ncases)), '{_flit(target_class)}', &")
    if distributed:
        a("    steps_total, cells_total, walltime, &")
        a(f"    {H('comm_size')}(), {threads}, {devices})")
        a("  end if")
        a("")
        a(f"  call {H('finalize')}()")
    else:
        a(f"    steps_total, cells_total, walltime, 1, {devices})")
    a("")
    a("contains")
    a("")
    a("  ! Every declared snapshot variable, read from the checks module's bound storage")
    a("  ! (host-associated `sb_<var>`) and serialized by the harness emitters. The same")
    a("  ! set for every case: the snapshot is the full declared state.")
    a("  subroutine capture_state(cid, out)")
    a("    character(len=*), intent(in) :: cid")
    a(f"    type({H('h_named')}), allocatable, intent(out) :: out(:)")
    if distributed:
        # The gather buffers, one per array rank in use (`allocatable, intent(out)` in the
        # gather: allocated on rank 0 only).
        for r in array_ranks:
            a(f"    real(dp), allocatable :: g_r{r}({','.join(':' * r)})")
        if array_ranks:
            a("    integer :: off1, off2")
    a(f"    allocate(out({len(bound_vars)}))")
    for k, v in enumerate(bound_vars, start=1):
        rank = _rank_of_shape(schema_vars[v], v)
        vlit = _flit(v)
        sb = f"{STATE_BINDING_PREFIX}{v}"
        if rank == 0:
            # A scalar is replicated: every rank holds the global value, rank 0's is written.
            if distributed:
                a("    if (rank == 0) then")
            a(f"    out({k}) = {H('box')}('{vlit}', &")
            a(f"      {H('emit_real')}({sb}))")
            if distributed:
                a("    end if")
        elif not distributed:
            # An unallocated bound array is a binding the module never established for this
            # case (its `case_setup` did not allocate it): fail the run loudly rather than
            # pass an unallocated actual to the emitter (undefined behaviour).
            a(f"    call require_bound(allocated({sb}), &")
            a(f"      '{vlit}', cid)")
            a(f"    out({k}) = {H('box')}('{vlit}', &")
            a(f"      {H(f'emit_array_r{rank}')}({sb}))")
        else:
            _render_distributed_capture(a, H, k, v, rank)
    a("  end subroutine capture_state")
    a("")
    if distributed:
        a("  ! Stop the run when a distributed binding names an axis the array does not have.")
        a("  subroutine require_axis(axis, max_axis, name, cid)")
        a("    integer, intent(in) :: axis")
        a("    integer, intent(in) :: max_axis")
        a("    character(len=*), intent(in) :: name")
        a("    character(len=*), intent(in) :: cid")
        a("    if (axis >= 0 .and. axis <= max_axis) return")
        a("    write(error_unit, '(A)') 'error: distributed binding of '//name// &")
        a("      ' names an axis the array does not have, for case '//cid")
        a("    error stop 1")
        a("  end subroutine require_axis")
        a("")
    a("  ! Stop the run when a bound array is not allocated at a capture point.")
    a("  subroutine require_bound(is_bound, name, cid)")
    a("    logical, intent(in) :: is_bound")
    a("    character(len=*), intent(in) :: name")
    a("    character(len=*), intent(in) :: cid")
    a("    if (is_bound) return")
    a("    write(error_unit, '(A)') 'error: bound state '//name// &")
    a("      ' is not allocated at capture for case '//cid")
    a("    error stop 1")
    a("  end subroutine require_bound")
    a("")
    a(f"end program {spec_id}_runner")
    # Safety net: the runner is host-rendered (NOT in the leaf's allowed_output_paths), so a
    # line over the deterministic Generate.gate lint-checker column limit (`max_rendered_line()`) would
    # be an UNREPAIRABLE wedge (no leaf can edit a host file). The hot lines above are wrapped,
    # but an extreme IR-sourced name (a very long case_id / variable) could
    # still overflow a line we did not wrap — fail closed HERE (a clean RenderError → transport
    # fail_closed with an actionable message) rather than let it surface as a lint wedge.
    # A few `lines` entries embed their own `&` continuations,
    # so measure per PHYSICAL line (split on embedded newlines) — measuring the joined entry
    # would false-fail a valid render whose wrapped physical lines are each within the limit.
    # `>=`, not `>`: on some supported linter builds the column rule fires AT the limit (the
    # same reason `_checks` bounds the check-id assignment strictly under it), and a host-authored
    # line of exactly the limit is unrepairable by any leaf.
    limit = max_rendered_line()
    for entry in lines:
        for ln in entry.split("\n"):
            if len(ln) >= limit:
                raise RenderError(
                    f"rendered runner line reaches the linter's {limit}-column limit "
                    f"({len(ln)} columns; {limit - 1} is the widest that lints everywhere): "
                    f"{ln.strip()[:80]!r}… — an IR-sourced name (case_id / "
                    "variable) is too long for the lint column limit; shorten it")
    return "\n".join(lines) + "\n"


def _render_distributed_capture(a: Any, H: Any, k: int, v: str, rank: int) -> None:
    """The capture of the bound array `v` (of rank `rank`, slot `k` of `out`) on a distributed
    harness: replicated (both partitioned-axis slots `0`) → rank 0's copy; partitioned along one
    or both grid dimensions → gathered onto rank 0 by the harness, whose tile check stops the run
    unless the contributed blocks cover the global array exactly. Every rank reaches the gather
    (it is collective); only rank 0 serializes. Each argument sits on its own line, so the
    longest bindable name stays under the lint limit."""
    vlit = _flit(v)
    sb = f"{STATE_BINDING_PREFIX}{v}"
    axis1, lo1, hi1, glo1, axis2, lo2, hi2, glo2 = distributed_binding_names(v)
    emit = H(f"emit_array_r{rank}")
    a(f"    call require_bound(allocated({sb}), &")
    a(f"      '{vlit}', cid)")
    a(f"    call require_axis({axis1}, {rank}, &")
    a(f"      '{vlit}', cid)")
    a(f"    call require_axis({axis2}, {rank}, &")
    a(f"      '{vlit}', cid)")
    a(f"    if ({axis1} == 0 .and. &")
    a(f"      {axis2} == 0) then")
    a("      if (rank == 0) then")
    a(f"        out({k}) = {H('box')}( &")
    a(f"          '{vlit}', &")
    a(f"          {emit}( &")
    a(f"            {sb}))")
    a("      end if")
    a("    else")
    # `lo<d>` / `hi<d>` are indices in the array's OWN bounds along the axis (a leaf declares
    # `u(0:n+1)` as readily as `u(n+2)`); the gather counts positions from 1, so the runner
    # shifts them by that axis's lower bound rather than asking the leaf to. A slot whose axis
    # is 0 is not read by the gather, and its shift stays 0.
    for slot, axis in ((1, axis1), (2, axis2)):
        a(f"      off{slot} = 0")
        a(f"      if ({axis} > 0) then")
        a(f"        off{slot} = lbound({sb}, &")
        a(f"          {axis if rank >= 2 else 1}) - 1")
        a("      end if")
    a(f"      call {H(f'gather_r{rank}')}( &")
    a(f"        {sb}, &")
    a(f"        {axis1}, &")
    a(f"        {lo1} - off1, &")
    a(f"        {hi1} - off1, &")
    a(f"        {glo1}, &")
    a(f"        {axis2}, &")
    a(f"        {lo2} - off2, &")
    a(f"        {hi2} - off2, &")
    a(f"        {glo2}, &")
    a(f"        g_r{rank})")
    a("      if (rank == 0) then")
    a(f"        out({k}) = {H('box')}( &")
    a(f"          '{vlit}', &")
    a(f"          {emit}(g_r{rank}))")
    a("      end if")
    a("    end if")


def _render_distributed_abi_comment(a: Any, harness_spec_id: str) -> None:
    """The distributed half of the checks ABI, stated where the producer leaf reads the ABI
    from (the rendered runner is its source for the ABI's dynamic surface): the partition
    metadata of every bound array, and the harness operations a physics source may call, with
    their interfaces as the pin compares them."""
    for line in (
        "  ! Distributed binding: this harness runs the program as several ranks arranged in",
        "  ! the p1 x p2 process grid the host imposes (grid_dims / grid_coord report it; the",
        "  ! program never chooses it). For every bound ARRAY <var> the checks module also",
        "  ! publishes eight default-integer module variables, which case_setup sets on every",
        "  ! rank for that rank's copy, one group per grid dimension d = 1, 2: sb_<var>_axis<d>,",
        "  ! the axis of <var> partitioned along grid dimension d (0 = not partitioned along d;",
        "  ! 0 in both groups = replicated: every rank holds the global array and rank 0's is",
        "  ! written), sb_<var>_lo<d> / sb_<var>_hi<d>, the indices, in the array's own declared",
        "  ! bounds, of the cells this rank contributes along that axis to the GLOBAL array -",
        "  ! the array as the IR's shape_expr declares it - and sb_<var>_glo<d>, the global",
        "  ! index (from 1, in that shape) of the cell at lo<d>. A halo cell (a copy of a",
        "  ! neighbour's cell) is never contributed; when the declared shape includes the",
        "  ! domain's boundary ghost cells, the first rank along d contributes the leading ones",
        "  ! and the last the trailing ones. hi<d> < lo<d> when a rank contributes nothing, and",
        "  ! the array is allocated all the same. Along a dimension the array is not partitioned",
        "  ! along, every rank of a line holds the same copy and only the coordinate 0 of that",
        "  ! dimension contributes it. Every axis not partitioned holds exactly the extent the",
        "  ! declared shape gives it. capture_state gathers each partitioned array onto rank 0,",
        "  ! and the harness's gather stops the run unless the contributed blocks tile the",
        "  ! global array. The halo exchange along dimension d takes an array whose partitioned",
        "  ! axis holds ng halo cells, the owned cells, then ng halo cells, and fills them at",
        "  ! every position of the other axis; exchanging along one dimension and then the other",
        "  ! fills the corner halo cells too. A scalar is replicated. case_run reports the cells",
        "  ! THIS rank updated (the runner sums them). A checks module, and a problem's",
        "  ! model, may use the harness module for these operations and no other (the direct",
        "  ! use of the message-passing library is refused). A component's model names none:",
        "  ! consumers call its operations on their own rank's block, so the component",
        "  ! distributes its cases in its checks module around the unmodified operations:",
    ):
        a(line)
    ops, _types, _ifaces, _errs = _parse_stanzas(_harness_pin(harness_spec_id).interface)
    for op in PHYSICS_CALLABLE_DISTRIBUTED_OPS:
        header, *body = ops[_hname(harness_spec_id, op)]
        a(f"  !   {header.strip()}")
        for line in body:
            a(f"  !     {line.strip()}")


def render_checks_header(ir: dict[str, Any], spec_id: str) -> None:
    """None: the leaf's `<spec_id>_checks` module carries its own declarations, and the runner's
    `use` of it is checked against them by the compiler, so the host renders no checks header
    for Fortran (`host_render.render_checks_header`)."""
    del ir, spec_id


# --- harness interface signature pin (fail-closed, run before rendering) ------
#
# The harnesses this renderer targets are `_HARNESS_PINS`' keys: harness_fortran_cpu (the R1
# (fortran, cpu) plumbing) and harness_fortran_cpu_mpi (the same plumbing under its own names,
# plus the distributed-state operations, issue #316). The template is written against their
# §5.1 signatures, embedded below verbatim. `assert_harness_pin` checks that the *certified*
# harness the consumer will build against still publishes those exact signatures — in both its
# IR (`public_api.signatures`) and its generated model source — so a harness recert
# that silently changed the interface fails the consumer's render (drift is caught
# at the consumer, not miscompiled at Build).

EXPECTED_HARNESS_SPEC_ID = "harness_fortran_cpu"

#: The harness that drives the program as several ranks (`distributed_state@2`).
DISTRIBUTED_HARNESS_SPEC_ID = "harness_fortran_cpu_mpi"

# The shared harness interface block (the pin compares signatures, not versions). It is the
# eight-argument `__write_perf` form the distributed harness still publishes; the single-process
# harness's pin, `_SINGLE_PROCESS_INTERFACE` below, is derived from it by replacing that one
# stanza (harness_fortran_cpu 0.8.0's writer takes no thread count), so an edit to the
# `write_perf` stanza here must keep that replacement applying. If a harness recert changes §5.1,
# the pin it is compared with and the render template must be updated together (the pin message
# says so).
_HARNESS_V3_INTERFACE = """\
type :: harness_fortran_cpu__h_named
  character(len=:), allocatable :: name
  character(len=:), allocatable :: json
end type harness_fortran_cpu__h_named

subroutine harness_fortran_cpu__parse_cases(tokens, ntokens, case_ids, ncases, ok)
  character(len=*), intent(in) :: tokens(:)
  integer, intent(in) :: ntokens
  character(len=case_id_len), intent(out) :: case_ids(:)
  integer, intent(out) :: ncases
  logical, intent(out) :: ok
end subroutine harness_fortran_cpu__parse_cases

function harness_fortran_cpu__emit_real(x) result(s)
  real(dp), intent(in) :: x
  character(len=:), allocatable :: s
end function harness_fortran_cpu__emit_real

function harness_fortran_cpu__emit_int(i) result(s)
  integer, intent(in) :: i
  character(len=:), allocatable :: s
end function harness_fortran_cpu__emit_int

function harness_fortran_cpu__emit_bool(b) result(s)
  logical, intent(in) :: b
  character(len=:), allocatable :: s
end function harness_fortran_cpu__emit_bool

function harness_fortran_cpu__emit_array_r1(a) result(s)
  real(dp), intent(in) :: a(:)
  character(len=:), allocatable :: s
end function harness_fortran_cpu__emit_array_r1

function harness_fortran_cpu__emit_array_r2(a) result(s)
  real(dp), intent(in) :: a(:,:)
  character(len=:), allocatable :: s
end function harness_fortran_cpu__emit_array_r2

function harness_fortran_cpu__emit_array_r3(a) result(s)
  real(dp), intent(in) :: a(:,:,:)
  character(len=:), allocatable :: s
end function harness_fortran_cpu__emit_array_r3

function harness_fortran_cpu__emit_array_r4(a) result(s)
  real(dp), intent(in) :: a(:,:,:,:)
  character(len=:), allocatable :: s
end function harness_fortran_cpu__emit_array_r4

function harness_fortran_cpu__box(name, json) result(nv)
  character(len=*), intent(in) :: name
  character(len=*), intent(in) :: json
  type(harness_fortran_cpu__h_named) :: nv
end function harness_fortran_cpu__box

subroutine harness_fortran_cpu__write_snapshot(case_id, values, time)
  character(len=*), intent(in) :: case_id
  type(harness_fortran_cpu__h_named), intent(in) :: values(:)
  real(dp), intent(in) :: time
end subroutine harness_fortran_cpu__write_snapshot

subroutine harness_fortran_cpu__write_perf(case_id, target, steps, cells_updated, walltime_sec, mpi_ranks, threads_per_rank, gpu_devices)
  character(len=*), intent(in) :: case_id
  character(len=*), intent(in) :: target
  integer, intent(in) :: steps
  integer, intent(in) :: cells_updated
  real(dp), intent(in) :: walltime_sec
  integer, intent(in) :: mpi_ranks
  integer, intent(in) :: threads_per_rank
  integer, intent(in) :: gpu_devices
end subroutine harness_fortran_cpu__write_perf
"""

# The §5.1 module-level `parameter` declarations. They are part of the published ABI but are not
# stanzas, so `parse_interface_stanzas` never sees them and the signature pin above compares only
# the SYMBOL `case_id_len`, never its value. A harness recert lowering it to 32 would therefore
# leave the interface pin green while this renderer kept emitting a 64-wide `case_ids(:)` actual
# for a 32-wide `intent(out)` dummy — the compiles-then-breaks class the case_id bound exists to
# prevent, reintroduced through harness drift. `dp` likewise fixes the kind of every `real(dp)`
# actual the glue passes. Pin both values against the certified source.
_HARNESS_V3_PARAMETERS: tuple[str, ...] = (
    "integer, parameter :: dp = real64",
    f"integer, parameter :: case_id_len = {CASE_ID_LEN}",
)

# Verbatim copy of `harness_fortran_cpu_mpi@0.3.0` §5.1's distributed-state operations (§3.3),
# as the language backend renders them. That harness's §5.1 is `_HARNESS_V3_INTERFACE` under its
# own `<spec_id>__` names followed by this block (its §5 says so), so its pinned interface is
# composed below rather than copied twice.
_DISTRIBUTED_STATE_INTERFACE = """\
subroutine harness_fortran_cpu_mpi__init()
end subroutine harness_fortran_cpu_mpi__init

subroutine harness_fortran_cpu_mpi__finalize()
end subroutine harness_fortran_cpu_mpi__finalize

function harness_fortran_cpu_mpi__comm_rank() result(r)
  integer :: r
end function harness_fortran_cpu_mpi__comm_rank

function harness_fortran_cpu_mpi__comm_size() result(n)
  integer :: n
end function harness_fortran_cpu_mpi__comm_size

subroutine harness_fortran_cpu_mpi__grid_dims(p1, p2)
  integer, intent(out) :: p1
  integer, intent(out) :: p2
end subroutine harness_fortran_cpu_mpi__grid_dims

subroutine harness_fortran_cpu_mpi__grid_coord(c1, c2)
  integer, intent(out) :: c1
  integer, intent(out) :: c2
end subroutine harness_fortran_cpu_mpi__grid_coord

subroutine harness_fortran_cpu_mpi__partition(n_global, dim, glo, ghi)
  integer, intent(in) :: n_global
  integer, intent(in) :: dim
  integer, intent(out) :: glo
  integer, intent(out) :: ghi
end subroutine harness_fortran_cpu_mpi__partition

subroutine harness_fortran_cpu_mpi__exchange_halo_r1(a, dim, ng, periodic)
  real(dp), intent(inout) :: a(:)
  integer, intent(in) :: dim
  integer, intent(in) :: ng
  logical, intent(in) :: periodic
end subroutine harness_fortran_cpu_mpi__exchange_halo_r1

subroutine harness_fortran_cpu_mpi__exchange_halo_r2(a, axis, dim, ng, periodic)
  real(dp), intent(inout) :: a(:,:)
  integer, intent(in) :: axis
  integer, intent(in) :: dim
  integer, intent(in) :: ng
  logical, intent(in) :: periodic
end subroutine harness_fortran_cpu_mpi__exchange_halo_r2

subroutine harness_fortran_cpu_mpi__gather_r1(a, axis1, lo1, hi1, glo1, axis2, lo2, hi2, glo2, g)
  real(dp), intent(in) :: a(:)
  integer, intent(in) :: axis1
  integer, intent(in) :: lo1
  integer, intent(in) :: hi1
  integer, intent(in) :: glo1
  integer, intent(in) :: axis2
  integer, intent(in) :: lo2
  integer, intent(in) :: hi2
  integer, intent(in) :: glo2
  real(dp), allocatable, intent(out) :: g(:)
end subroutine harness_fortran_cpu_mpi__gather_r1

subroutine harness_fortran_cpu_mpi__gather_r2(a, axis1, lo1, hi1, glo1, axis2, lo2, hi2, glo2, g)
  real(dp), intent(in) :: a(:,:)
  integer, intent(in) :: axis1
  integer, intent(in) :: lo1
  integer, intent(in) :: hi1
  integer, intent(in) :: glo1
  integer, intent(in) :: axis2
  integer, intent(in) :: lo2
  integer, intent(in) :: hi2
  integer, intent(in) :: glo2
  real(dp), allocatable, intent(out) :: g(:,:)
end subroutine harness_fortran_cpu_mpi__gather_r2

subroutine harness_fortran_cpu_mpi__gather_r3(a, axis1, lo1, hi1, glo1, axis2, lo2, hi2, glo2, g)
  real(dp), intent(in) :: a(:,:,:)
  integer, intent(in) :: axis1
  integer, intent(in) :: lo1
  integer, intent(in) :: hi1
  integer, intent(in) :: glo1
  integer, intent(in) :: axis2
  integer, intent(in) :: lo2
  integer, intent(in) :: hi2
  integer, intent(in) :: glo2
  real(dp), allocatable, intent(out) :: g(:,:,:)
end subroutine harness_fortran_cpu_mpi__gather_r3

subroutine harness_fortran_cpu_mpi__gather_r4(a, axis1, lo1, hi1, glo1, axis2, lo2, hi2, glo2, g)
  real(dp), intent(in) :: a(:,:,:,:)
  integer, intent(in) :: axis1
  integer, intent(in) :: lo1
  integer, intent(in) :: hi1
  integer, intent(in) :: glo1
  integer, intent(in) :: axis2
  integer, intent(in) :: lo2
  integer, intent(in) :: hi2
  integer, intent(in) :: glo2
  real(dp), allocatable, intent(out) :: g(:,:,:,:)
end subroutine harness_fortran_cpu_mpi__gather_r4

function harness_fortran_cpu_mpi__reduce_sum(x) result(s)
  real(dp), intent(in) :: x
  real(dp) :: s
end function harness_fortran_cpu_mpi__reduce_sum

function harness_fortran_cpu_mpi__reduce_max(x) result(s)
  real(dp), intent(in) :: x
  real(dp) :: s
end function harness_fortran_cpu_mpi__reduce_max

function harness_fortran_cpu_mpi__reduce_min(x) result(s)
  real(dp), intent(in) :: x
  real(dp) :: s
end function harness_fortran_cpu_mpi__reduce_min

function harness_fortran_cpu_mpi__reduce_sum_int(i) result(s)
  integer, intent(in) :: i
  integer :: s
end function harness_fortran_cpu_mpi__reduce_sum_int
"""


# `harness_fortran_cpu@0.9.0` §5.1's `__write_perf` (§3.2, issue #512), as the language backend
# renders it: the stanza of `_HARNESS_V3_INTERFACE` without the `threads_per_rank` argument,
# because that harness's writer obtains the thread count itself, at run time. Its §5.1 is
# `_HARNESS_V3_INTERFACE` with this stanza in place of that one; the distributed harness keeps
# the eight-argument writer (its runner passes the count, one thread per rank).
_V3_WRITE_PERF_THREADS = """\
subroutine harness_fortran_cpu__write_perf(case_id, target, steps, cells_updated, walltime_sec, mpi_ranks, threads_per_rank, gpu_devices)
  character(len=*), intent(in) :: case_id
  character(len=*), intent(in) :: target
  integer, intent(in) :: steps
  integer, intent(in) :: cells_updated
  real(dp), intent(in) :: walltime_sec
  integer, intent(in) :: mpi_ranks
  integer, intent(in) :: threads_per_rank
  integer, intent(in) :: gpu_devices
end subroutine harness_fortran_cpu__write_perf
"""
_SINGLE_PROCESS_WRITE_PERF = """\
subroutine harness_fortran_cpu__write_perf(case_id, target, steps, cells_updated, walltime_sec, mpi_ranks, gpu_devices)
  character(len=*), intent(in) :: case_id
  character(len=*), intent(in) :: target
  integer, intent(in) :: steps
  integer, intent(in) :: cells_updated
  real(dp), intent(in) :: walltime_sec
  integer, intent(in) :: mpi_ranks
  integer, intent(in) :: gpu_devices
end subroutine harness_fortran_cpu__write_perf
"""
_SINGLE_PROCESS_INTERFACE = _HARNESS_V3_INTERFACE.replace(
    _V3_WRITE_PERF_THREADS, _SINGLE_PROCESS_WRITE_PERF)


@dataclass(frozen=True)
class _HarnessPin:
    """What the renderer expects of one harness it targets."""

    #: The §5.1 block the rendered glue is written against, under the harness's names.
    interface: str
    #: Whether the harness drives the program as several ranks (`distributed_state@2`): the
    #: runner then starts and ends the runtime, gathers the partitioned state onto rank 0 and
    #: writes on rank 0 only.
    distributed: bool


#: The harnesses this renderer targets, by spec_id. Which harness provides which capability is
#: `codegen_bundle.HARNESS_CAPABILITY_MANIFESTS`' fact; `test_fortran_runner` couples the
#: `distributed` column to it, so the two cannot disagree about which harness is distributed.
_HARNESS_PINS: dict[str, _HarnessPin] = {
    EXPECTED_HARNESS_SPEC_ID: _HarnessPin(interface=_SINGLE_PROCESS_INTERFACE, distributed=False),
    DISTRIBUTED_HARNESS_SPEC_ID: _HarnessPin(
        interface=_HARNESS_V3_INTERFACE.replace(
            f"{EXPECTED_HARNESS_SPEC_ID}__", f"{DISTRIBUTED_HARNESS_SPEC_ID}__")
        + "\n" + _DISTRIBUTED_STATE_INTERFACE,
        distributed=True),
}


def _harness_pin(harness_spec_id: str) -> _HarnessPin:
    """The pin of a harness this renderer targets; raises `RenderError` for any other."""
    pin = _HARNESS_PINS.get((harness_spec_id or "").strip())
    if pin is None:
        raise RenderError(
            f"harness_spec_id {harness_spec_id!r} is not one this renderer targets "
            f"({', '.join(sorted(_HARNESS_PINS))})")
    return pin


def _is_distributed_harness(harness_spec_id: str) -> bool:
    """Whether the runner over `harness_spec_id` is the distributed variant. A harness this
    renderer does not target renders the single-process variant (its pin refuses it)."""
    pin = _HARNESS_PINS.get((harness_spec_id or "").strip())
    return pin is not None and pin.distributed


def _parse_stanzas(block: str) -> tuple[Any, Any, Any, Any]:
    from tools.backends.language.fortran.signatures import parse_interface_stanzas
    return parse_interface_stanzas(block)

_PIN_DRIFT_HINT = (
    "the certified harness interface no longer matches the renderer's pinned "
    "expectation — a harness recert changed its published surface; update the "
    "renderer pin (_HARNESS_V3_INTERFACE / _SINGLE_PROCESS_INTERFACE / "
    "_HARNESS_V3_PARAMETERS) AND the "
    "render template together, then re-certify dependent nodes")


def assert_harness_pin(
    ir: dict[str, Any],
    spec_id: str,
    harness_spec_id: str,
    harness_signatures: Any,
    harness_source: str,
) -> None:
    """Fail-closed guard the conductor runs BEFORE rendering: the certified harness
    the consumer will link against must still publish exactly the signatures this
    renderer was written for, in both its IR ``public_api.signatures`` and its
    generated model source. Any drift raises ``RenderError`` (→ transport
    fail_closed with the recert-drift hint), never a Generate content retry.

    ``harness_signatures`` is the certified harness IR's ``public_api.signatures``
    (a list of ``{symbol, interface}``), which the conductor resolves STRUCTURALLY via
    ``_certified_ir_dir`` (the latest passing certified IR for the harness
    ``(kind, id, version)``), not from any leaf-authored source-meta field; ``harness_source``
    is the text of the certified ``<harness_spec_id>_model.f90`` (resolved via
    ``_certified_model_source``). Signatures that resolve to nothing usable (None / empty /
    malformed) fail closed as a missing-artifact error, distinctly from interface drift.
    """
    # The §5.1 stanza layer, from the language backend. It used to be imported out of
    # `validate_pipeline_semantics`, where it sat as five private names — Fortran knowledge in a
    # neutral module, which this module then had to reach back into (docs/BACKEND_BOUNDARY.md).
    from tools.backends.language.fortran import source as fortran_source
    from tools.backends.language.fortran.signatures import (
        parse_interface_stanzas, source_atoms, stanza_atoms, stanza_line_set, stanza_line_list,
        surface_drift, type_layout_drift)

    pin = _harness_pin(harness_spec_id)
    exp_ops, exp_types, exp_ifaces, exp_errs = parse_interface_stanzas(pin.interface)
    if exp_errs or exp_ifaces:  # a renderer bug, not an input problem
        raise RenderError(
            f"embedded harness interface failed to parse: {exp_errs} (the pinned harness "
            f"surface carries no interface prototype; found {sorted(exp_ifaces)})")

    # Module parameter VALUES (see `_HARNESS_V3_PARAMETERS`). Per-entity atoms, so a combined
    # `integer, parameter :: dp = real64, case_id_len = 64` declaration matches — the same
    # normalization `_validate_generated_signatures` uses to pin §5.1 against the
    # source, applied here to pin the RENDERER against the source.
    src_atoms = source_atoms(harness_source or "")
    for pline in _HARNESS_V3_PARAMETERS:
        if any(atom not in src_atoms for atom in stanza_atoms([pline])):
            raise RenderError(
                f"certified harness model source does not declare the pinned module parameter "
                f"`{pline}`, whose VALUE the rendered glue hardcodes (its `case_ids(:)` buffer "
                f"width and its `real(dp)` actuals): {_PIN_DRIFT_HINT}")

    used_symbols = [_hname(harness_spec_id, op)
                    for op in _used_harness_ops(ir, distributed=pin.distributed)]
    if pin.distributed:
        # The operations the rendered comment offers a physics source are pinned too: the
        # producer is told their interfaces from the embedded block, so the certified harness
        # must still publish them as stated.
        used_symbols += [_hname(harness_spec_id, op) for op in PHYSICS_CALLABLE_DISTRIBUTED_OPS
                         if _hname(harness_spec_id, op) not in used_symbols]
    used_symbols += [_hname(harness_spec_id, t) for t in _HARNESS_TYPES]

    # Certified IR signatures, keyed by symbol. Each entry's `signature` is the language-neutral
    # structured form (Objective B); the Fortran backend renders it back to the stanza currency the
    # pin compares in (the same rendering `_validate_ir_signatures_against_section51` uses).
    from tools.backends.language.fortran.signatures import SignatureParseError, render_symbol_to_fortran

    ir_iface: dict[str, str] = {}
    for entry in (harness_signatures if isinstance(harness_signatures, list) else []):
        # A usable entry has a NON-BLANK str symbol and a non-empty mapping signature that renders —
        # the same rule the IR validator (`_validate_ir_signatures_against_section51`) pins, so a
        # blank/unrenderable entry (which that validator rejects) is skipped here rather than seeding
        # a bogus `""` key that would later masquerade as per-symbol interface drift.
        if not (isinstance(entry, dict) and isinstance(entry.get("symbol"), str)
                and entry["symbol"].strip() and isinstance(entry.get("signature"), dict)
                and entry["signature"]):
            continue
        try:
            ir_iface[entry["symbol"].strip()] = render_symbol_to_fortran(entry["signature"])
        except SignatureParseError:
            continue

    # No usable signatures at all (None / [] / non-list / every entry malformed) is an
    # absent-or-incomplete certified-IR artifact — or a caller that failed to resolve it —
    # NOT interface drift. Fail closed here so the per-symbol "omits ... recert drift" message
    # below only ever fires when real signatures ARE present but a specific one is missing.
    if not ir_iface:
        raise RenderError(
            f"certified harness IR carries no usable public_api.signatures "
            f"(got {type(harness_signatures).__name__}) — a missing/incomplete certified-IR "
            "artifact (or a caller that failed to resolve it), NOT interface drift; re-certify "
            "the harness IR (run_workflow.py --with-deps) so its public_api.signatures is present")

    # The procedures and the types are read the way `signatures.generated_source_violations`
    # reads them — from the definitions the harness model module itself carries, compared by what
    # each DECLARES (issue #430) — so the renderer and the gate cannot disagree about a certified
    # source.
    try:
        definitions = fortran_source.module_level_definitions(
            (harness_source or "").lower(), f"{harness_spec_id}_model")
        type_reading = fortran_source.module_level_type_definitions(
            (harness_source or "").lower(), f"{harness_spec_id}_model")
    except fortran_source.SourceStructureError as exc:
        first = exc.errors[0] if exc.errors else None
        where = (f"; the first at statement {first.line} of its joined view: {first.snippet!r}"
                 if first is not None else "")
        # NOT `_PIN_DRIFT_HINT`: nothing says the published surface changed, so editing the pin
        # is the wrong repair. The source does not parse with the installed front end — a
        # front-end version other than `structure.MEASURED_PACKAGE_VERSIONS`, or a harness
        # certified before `Generate.gate` parsed infrastructure sources.
        raise RenderError(
            f"certified harness model source no longer resolves through the Fortran structure "
            f"front end ({len(exc.errors)} parse error(s){where}), so its published procedures "
            "and derived types cannot be compared with the pinned interface. Check that the installed "
            "tree-sitter / tree-sitter-fortran are the versions `requirements.txt` pins; if "
            "they are, re-certify the harness node (`run_workflow.py --with-deps`) so its "
            "source passes the current Generate.gate — do not edit the renderer pin") from exc

    for symbol in used_symbols:
        exp_stanza = exp_ops.get(symbol) or exp_types.get(symbol)
        if exp_stanza is None:  # renderer bug: template depends on an un-embedded symbol
            raise RenderError(
                f"internal: renderer depends on harness symbol {symbol!r} not present in "
                "the embedded pinned interface")
        # A derived type's component layout is ordered (§5 compatibility contract); a
        # procedure's dummy declarations are order-immaterial (the header line, itself an
        # atom, pins call order), matching the two M3c-α gates.
        is_type = symbol in exp_types

        # (1) IR public_api.signatures — an interface-only stanza, so EXACT match.
        ir_text = ir_iface.get(symbol)
        if not ir_text:
            raise RenderError(
                f"certified harness IR public_api.signatures omits {symbol!r}: {_PIN_DRIFT_HINT}")
        ir_ops, ir_types, _ir_ifaces, _ = parse_interface_stanzas(ir_text)
        ir_stanza = ir_ops.get(symbol) or ir_types.get(symbol)
        ir_ok = ir_stanza is not None and (
            stanza_line_list(ir_stanza) == stanza_line_list(exp_stanza) if is_type
            else stanza_line_set(ir_stanza) == stanza_line_set(exp_stanza))
        if not ir_ok:
            raise RenderError(
                f"certified harness IR signature for {symbol!r} differs from the pinned "
                f"interface: {_PIN_DRIFT_HINT}")

        # (2) Generated model source — a procedure is compared by what its definition in the
        # harness model module declares (`surface_drift`), a type by its one module-level
        # definition there (`type_layout_drift`), both as the Generate gate does.
        if is_type:
            type_definition = type_reading.definitions.get(symbol.lower())
            if type_definition is None:
                raise RenderError(
                    f"certified harness model source omits {symbol!r}: {_PIN_DRIFT_HINT}")
            src_ok = not type_layout_drift(
                exp_stanza, definition=type_definition,
                count=type_reading.counts.get(symbol.lower(), 0))
        else:
            definition = definitions.get(symbol.lower())
            if definition is None:
                raise RenderError(
                    f"certified harness model source omits {symbol!r}: {_PIN_DRIFT_HINT}")
            src_ok = not surface_drift(
                exp_stanza, header=definition.header,
                declarations=definition.declarations, exact=False,
                local_types=definition.local_types)
        if not src_ok:
            raise RenderError(
                f"certified harness model source signature for {symbol!r} differs from the "
                f"pinned interface: {_PIN_DRIFT_HINT}")


# ---------------------------------------------------------------------------------------------
# The leaf-facing remedies of the checks ABI (issue #289, R4-b PR-4 precondition). The neutral
# gates decide WHETHER the checks module publishes the ABI and the bound state
# (`codegen_bundle.m3c_checks_abi_violation`, `_m3c_state_binding_mismatch`, the validator's
# `_validate_checks_source_files`); HOW the leaf is told to fix it names this language's
# publication statements, procedure kinds and the runner's import spelling, so the sentence is
# this backend's. Moved verbatim: the messages are byte-identical to the neutral copies they
# replace.
# ---------------------------------------------------------------------------------------------


def checks_abi_publication_violation(spec_id: str, unpublished: list[str],
                                     wrong_kind: list[str]) -> str:
    """The acceptance layer's refusal of a checks module that leaves an ABI name unpublished
    (`unpublished`) or defines one as a callable of the wrong kind (`wrong_kind`)."""
    parts = []
    if unpublished:
        parts.append(
            f"not published by module {spec_id}_checks: {', '.join(unpublished)} (define it "
            f"there and, under a bare `private` default, name it in a `public ::` statement)")
    if wrong_kind:
        parts.append(
            f"defined here as a FUNCTION: {', '.join(wrong_kind)} (every ABI name is a "
            f"subroutine by contract, and the runner reaches the ones it imports with a "
            f"`call`, so a function of that name cannot satisfy it)")
    return (f"module {spec_id}_checks must define and publish the fixed checks ABI as "
            f"subroutines — " + "; ".join(parts)
            + f". The full required set is {', '.join(CHECKS_PUBLIC_NAMES)} for EVERY M3c "
            f"node, whatever subset this node's runner imports.")


def bound_state_publication_violation(spec_id: str, hidden: list[str]) -> str:
    """The acceptance layer's refusal of a checks module that hides bound state variables."""
    return (f"module {spec_id}_checks must publish every bound state variable — the "
            f"host-rendered runner imports each one by name (`{STATE_BINDING_PREFIX}<var> => "
            f"<var>`) and serializes it at the two capture points — but the module hides these "
            f"(a bare `private` default with no `public ::` naming them, or a "
            f"`private ::` naming them): {', '.join(hidden)}. Declare each as a "
            f"module-level `real(dp)` variable (an array of the declared rank, allocated by "
            f"`case_setup`) "
            f"and list it in a `public ::` statement in the specification part.")


def hidden_bound_state_remedy(hidden: list[str]) -> str:
    """The `Generate.gate` static check's statement of the same defect, after the file path."""
    return (f"checks module must publish every bound state variable (the "
            f"host-rendered runner imports each IR snapshot variable as "
            f"`{STATE_BINDING_PREFIX}<var> => <var>` and "
            f"serializes it at the two capture points); hidden by a bare `private` default "
            f"with no `public ::` naming it, or by a `private ::` naming it: {hidden}")


def state_binding_module_reason(module: str) -> str:
    """Why a binding's `module` must be the checks module: how the runner reads the storage."""
    return f"the host-rendered runner reads the bound storage with `use {module}, only: ...`"


def state_binding_storage_reason(variable: str) -> str:
    """Why a binding's `storage_symbol` must equal its variable: the runner's import of it."""
    return (f"the runner imports the module-level variable of THAT name "
            f"(`{STATE_BINDING_PREFIX}{variable} => {variable}`)")


def physics_harness_uses(harness_spec_id: str) -> dict[str, frozenset[str]]:
    """`{module: names}` a physics source may `use` from the harness (`host_render.
    physics_harness_uses`): its distributed-state operations for a physics source
    (`PHYSICS_CALLABLE_DISTRIBUTED_OPS`), on a harness that runs the program as several ranks."""
    if not _is_distributed_harness(harness_spec_id):
        return {}
    sid = harness_spec_id.strip()
    return {f"{sid}_model": frozenset(_hname(sid, op) for op in PHYSICS_CALLABLE_DISTRIBUTED_OPS)}


def distributed_state_names(harness_spec_id: str, arrays: list[str]) -> list[str]:
    """The partition metadata of the bound arrays `arrays` the runner over `harness_spec_id`
    imports from the checks module (`host_render.distributed_state_names`)."""
    if not _is_distributed_harness(harness_spec_id):
        return []
    return [name for v in arrays for name in distributed_binding_names(v)]
