"""The Fortran backend's `runner_render` capability: a physics node's host-authored runner glue.

Pure-function module reached through `registry.capability_module("language", <lang>,
"runner_render")` — the neutral seam is `tools/host_render.py`, which is what the conductor and
the compile gate call. Nothing outside this package imports it by name.

It takes a compiled IR plus the target/harness spec ids and returns the text of
``<spec_id>_runner.f90`` — the deterministic driver of the physics node (issue #589): per case it
reads the host-written case file through the certified harness (``harness_fortran_cpu``), allocates
the state, calls the operations the node's model publishes, owns the time loop on a ``loop`` node,
and writes every capture THROUGH the harness's plumbing. Because the harness's interface owns the
JSON envelope assembly (§3 / §5.1 of the harness controlled_spec), this renderer holds **no
serialization knowledge**: it calls the writers — it never formats a JSON token, and it judges
nothing (the host's predicates over the captures do, issue #582).

Split of authorship on an M3c node:
- ``<spec_id>_model.f90`` — the physics kernel + its published operations   (LLM leaf)
- ``<spec_id>_runner.f90`` — this renderer                                  (host)
- ``src/Makefile``          — ``workflow_conductor._write_makefile``        (host)

The rendered program ``use``s two modules: ``<harness_spec_id>_model`` (the certified plumbing) and
``<spec_id>_model`` (the node's operations), and on a node with a host-rendered procedure argument
a third of its own, ``<spec_id>_drv``, ahead of the program in the same file. Every snapshot
variable is a ``real(dp)`` variable of the driver, ``st_<var>``, so no generated procedure can
filter, compute or rewrite a captured value on its way to the emitters. The text is authored
lint-clean (``use only:``, a bare ``implicit none`` with NO allow directive, ≤99-column lines) so
the deterministic Generate.gate lint checker — which lints the whole ``src/`` tree — stays green.

A harness that runs the program as several ranks (``harness_fortran_cpu_mpi``) takes the
DISTRIBUTED form of the same driver (issue #610, Plan B §3.3): every rank reads the whole case; the
replication table the host renders from ``runner_ir.distribute_plan`` decides per process grid
whether a case is distributed; on a distributed case each spatial axis of ``drive.distribute`` is
partitioned, the integer extents bound to an axis's cell count become the block's (B4), every
array that carries an axis is copied into a fresh allocatable at its block (B3), the result is
reduced over the ranks (B8), the series folds a written array over the owned cells with a
non-finite flag (B9), and each capture gathers what the driver or the kernel writes onto rank 0,
which writes (B10). A physics source on such a harness is held to its distributed-state operations
(``physics_harness_uses``).

``render_runner`` raises ``RenderError`` (→ transport fail_closed, NOT a Generate
retry) for any IR it cannot faithfully render: an unparseable ``shape_expr``, a
rank>4 snapshot variable, a snapshot variable colliding with a harness-reserved
key, more than one
infrastructure dependency, or an over-long identifier. ``assert_harness_pin``
(the signature pin) is a separate fail-closed guard the conductor runs against
the *certified* harness IR signatures + source before rendering.
"""

from __future__ import annotations

import ast
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
    "parse_cases", "box", "write_snapshot", "write_perf", "read_case", "get_array_r4",
)

#: The width of one command-line token of the rendered driver: the spec path is one, and a longer
#: argument stops the run rather than truncating (the longest real spec path measured is 186).
TOKEN_LEN = 4096

#: The driver's local prefixes (each three characters, so `_snapshot_schema`'s bound on a state's
#: name covers them all): the state, its work copy on a `loop` node, the logical temporary of a
#: logical state, the integer temporary of an integer `init` out.
_ST = "st_"
_WK = "wk_"
_LG = "lg_"
_IV = "iv_"
#: The distributed driver's global copy of a spatial state it reads (issue #610).
_GL = "gl_"

# The distributed-state operations a RUNNER calls on a harness that drives the program as several
# ranks (`distributed_state@2`, issues #316, #512 and #610): it starts and ends the runtime, asks
# its rank (only rank 0 writes), the run's size (the perf record's rank count, observed rather
# than configured) and the process grid (the replication table's key), partitions each spatial
# axis, reduces the result logical, the series extremes and `init`'s scalars over the ranks, and
# gathers each partitioned array it or the kernel writes onto rank 0 before it is serialized
# (`gather_r<k>`, added per array rank on demand, `_distributed_ops`).
_DISTRIBUTED_RUNNER_OPS = ("init", "finalize", "comm_rank", "comm_size", "grid_dims",
                           "partition", "reduce_sum_int", "reduce_min", "reduce_max")

#: The distributed-state operations a PHYSICS source (the leaf's model) may call on such a harness,
#: and the only harness names it may `use` (`source.
#: model_harness_isolation_violations`). The runner keeps the rest: the start and end of the
#: runtime and the gather are the capture's, the writers and emitters are the runner's alone
#: (Z6) — a physics source that reached them could write its own evidence — and the partition is
#: the driver's (issue #610, B13: the driver hands each call its block, so a kernel that
#: partitioned again would distribute an already distributed block).
PHYSICS_CALLABLE_DISTRIBUTED_OPS: tuple[str, ...] = (
    "comm_rank", "comm_size", "grid_dims", "grid_coord", "exchange_halo_r1",
    "exchange_halo_r2",
    "reduce_sum", "reduce_max", "reduce_min", "reduce_sum_int",
)

# --- IR extraction helpers ---------------------------------------------------------------------
#
# The language-neutral readers live in `tools/runner_ir.py` (issue #289, R4-b PR-6), shared with
# every other backend that renders a runner, so the refusals of a neutral IR fact are one set for
# every target. What stays here is what Fortran adds: the rules on the name of a driver variable, and
# the bound the linter's column limit implies (`max_rendered_line`).

_dget = runner_ir.dget
_rank_of_shape = runner_ir.rank_of_shape
_case_ids = runner_ir.case_ids
_target_class = runner_ir.target_class
_infra_dep_count = runner_ir.infra_dep_count


def _snapshot_schema(ir: dict[str, Any]) -> tuple[dict[str, str], str]:
    """Return ``({var_name: shape_expr}, time_variable)`` from the IR's snapshot schema
    (`runner_ir.snapshot_schema`), with this language's rules on each variable name.

    Same section `_author_snapshot_schema` (workflow_conductor) reads."""
    seen_folded: dict[str, str] = {}

    def check_name(name: str) -> None:
        # A snapshot variable IS the driver's state variable `st_<name>`: so the name must be a
        # legal identifier, the prefixed name must fit the identifier limit (every driver prefix
        # is `_ST`'s length), and two names may not fold to one identifier (Fortran is
        # case-insensitive: `U` and `u` would be one variable, declared twice).
        if not _IDENTIFIER_RE.fullmatch(name):
            raise RenderError(
                f"snapshot variable {name!r} is not a bindable identifier: every snapshot "
                "variable is the driver's state variable of that name, so it must match "
                f"{bundle.IDENTIFIER_PATTERN}")
        local = f"{_ST}{name}"
        if len(local) > bundle.IDENTIFIER_MAX:
            raise RenderError(
                f"snapshot variable {name!r} is {len(name)} chars; the driver's state variable "
                f"{local!r} exceeds the {bundle.IDENTIFIER_MAX}-char identifier limit")
        folded = name.casefold()
        if folded in seen_folded:
            raise RenderError(
                f"snapshot variables {seen_folded[folded]!r} and {name!r} are one identifier "
                "to the driver's state variable (identifiers are case-insensitive), so they "
                "cannot both be declared")
        seen_folded[folded] = name

    return runner_ir.snapshot_schema(ir, check_name)


def _threads(target: dict[str, Any]) -> int:
    """The threads per rank the run executes with — the target profile's `execution`, which
    is what `run_program` is told. Until R4-a PR-2 this read the IR's
    `backend_overrides.openmp.num_threads`, a count the run never used. Only the DISTRIBUTED
    variant's perf record writes it (a launcher target runs one thread per rank, so every
    shape's count is 1); the single-process harness's `__write_perf` takes no thread count and
    writes the one the run's shared-memory runtime gives the program, obtained at run time
    (`harness_fortran_cpu@0.10.0` §3.2, issue #512)."""
    return int(target["execution"]["threads_per_rank"])


#: The target a DRY render is given: `ir_content_violations` renders only to learn whether the
#: IR renders, and discards the text. The class and the thread count reach nothing but the
#: perf-record line, which raises for no value; the rank count is the grid list of the
#: distributed form's replication table (issue #610, B6), here `(1, 1)` alone, so the dry render
#: builds that row and a real target's table adds rows of the same width (a case id per line).
#: None of the three decides a violation.
_DRY_RUN_TARGET: dict[str, Any] = {"hardware": {"class": "cpu"},
                                   "execution": {"threads_per_rank": 1, "ranks": 1}}



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


def _used_harness_ops(ir: dict[str, Any], *, distributed: bool = False) -> list[str]:
    """Unqualified harness op names the rendered driver calls (deterministic order): the core
    writers and readers, the emitters and getters of what the IR declares, and the rank-1 emitter
    of the series on a `loop` node. `assert_harness_pin` compares exactly these, so a name left
    out here is an unenforced pin. `distributed`: the distributed form's own operations too
    (`_distributed_ops`), which the program `use`s by the same list."""
    schema, _ = _snapshot_schema(ir)
    plan = runner_ir.driver_plan(ir, schema)
    ops = ["parse_cases", "read_case", *plan.getters, "emit_real"]
    ranks = set(plan.array_ranks) | ({1} if plan.loop else set())
    ops += [f"emit_array_r{r}" for r in sorted(ranks)]
    ops += ["box", "write_snapshot", "write_perf"]
    if distributed:
        dist = _distribution(ir, schema, plan, None, _DRY_RUN_TARGET)
        ops += _distributed_ops(plan, dist)
    return ops


def _check_identifier_lengths(spec_id: str, harness_spec_id: str) -> None:
    # These are node-IDENTITY defects (a re-author cannot shorten the spec_id / harness id),
    # so they are `identity=True`: the compile.static mirror excludes them (spec-input concern).
    if len(spec_id) > MAX_SPEC_ID_LEN:
        raise RenderError(
            f"spec_id {spec_id!r} is {len(spec_id)} chars (>{MAX_SPEC_ID_LEN}); "
            "the derived `<spec_id>_runner` / `_drv` identifiers would risk the "
            f"f2008 {bundle.IDENTIFIER_MAX}-char limit", identity=True)
    for derived in (f"{spec_id}_runner", f"{spec_id}_drv", f"{spec_id}_model"):
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
    """Render ``<spec_id>_runner.f90``, the driver, from the IR and the target. Deterministic and
    pure.

    ``harness_spec_id`` is the certified plumbing module's spec_id (a key of ``_HARNESS_PINS``);
    a harness that runs the program as several ranks takes the distributed form (module
    docstring). ``target`` is the run's target profile document, read for the perf record's
    hardware class, thread and device counts (``host_execution.perf_parallelism``) and, on the
    distributed form, for the process grids the replication table lists
    (``runner_ir.distribute_grids``). See module docstring for the render-error matrix. The
    returned text is the complete Fortran source (trailing newline included).
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
    _, _, devices = perf_parallelism(target)
    plan = runner_ir.driver_plan(ir, schema_vars)

    H = lambda sym: _hname(harness_spec_id, sym)  # noqa: E731 (local shorthand)
    # A harness that runs the program as several ranks takes the distributed form (issue #610).
    dist = (_distribution(ir, schema_vars, plan, H, target)
            if _is_distributed_harness(harness_spec_id) else None)
    lines: list[str] = []
    a = lines.append
    _render_procedure_module(a, plan, spec_id)
    _render_program(a, plan, spec_id, harness_spec_id, H, target_class, devices, dist)
    # Safety net: the runner is host-rendered, so a line over the deterministic Generate.gate
    # lint-checker column limit (`max_rendered_line()`) would be an UNREPAIRABLE wedge (no leaf
    # can edit a host file). Measured per PHYSICAL line. `>=`, not `>`: on some supported linter
    # builds the column rule fires AT the limit.
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


def _real_literal(value: float) -> str:
    text = format(float(value), ".17g")
    if not any(ch in text for ch in ".e"):
        text += ".0"
    return f"{text}_dp"


def _literal(kind: str, value: Any) -> str:
    if kind == "logical":
        if not isinstance(value, bool):
            raise RenderError(f"literal {value!r} does not bind a logical dummy")
        return ".true." if value else ".false."
    if kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise RenderError(f"literal {value!r} does not bind an integer dummy")
        return str(value)
    return _real_literal(float(value))


def _colons(rank: int) -> str:
    return ",".join(":" * rank)


def _render_procedure_module(a: Any, plan: runner_ir.DriverPlan, spec_id: str) -> None:
    """The module holding each host-rendered procedure of the node (D11), what its expression
    reads and the call records it writes. A module, not a contained procedure: an internal
    procedure passed as an argument needs a trampoline on an executable stack."""
    procedures = [p for entry in plan.entries for p in entry.procedures]
    if not procedures:
        return
    a(f"module {spec_id}_drv")
    a("  use, intrinsic :: iso_fortran_env, only: real64")
    a("  implicit none")
    a("  private")
    a("  integer, parameter :: dp = real64")
    for value in plan.procedure_hosts:
        a(f"  real(dp), public :: {value.local} = 0.0_dp")
    for var in plan.record_states:
        rank = plan.variables[var]
        if rank:
            a(f"  real(dp), allocatable, public :: {_ST}{var}({_colons(rank)})")
        else:
            a(f"  real(dp), public :: {_ST}{var} = 0.0_dp")
    for line in _wrap("public :: " + ", ".join(p.name for p in procedures)):
        a(f"  {line}")
    a("contains")
    for proc in procedures:
        _render_procedure(a, plan, proc)
    a(f"end module {spec_id}_drv")
    a("")


def _render_procedure(a: Any, plan: runner_ir.DriverPlan, proc: runner_ir.ProcedurePlan) -> None:
    from tools.backends.language.fortran.signatures import render_symbol_to_fortran

    stanza = render_symbol_to_fortran({**proc.prototype, "name": proc.name}).strip().splitlines()
    args = {x["name"]: x for x in proc.prototype.get("args") or []}

    def name(token: str) -> str:
        if token in proc.hosts:
            return proc.hosts[token]
        if token not in args:
            raise RenderError(f"procedure expression names {token!r}, not a dummy of its "
                              "prototype")
        return token

    expr = runner_ir.lower_expression(
        proc.tree, name=name, number=_real_literal,
        power=lambda x, y: f"({x} ** {y})",
        function=lambda fn, xs: f"{fn}({', '.join(xs)})")
    for line in _wrap(stanza[0]):
        a(f"  {line}")
    for line in stanza[1:-1]:
        a(f"  {line}")
    if proc.calls or proc.record:
        a("    integer :: call_no")
    # A dummy nothing below reads would be refused as unused in a file no leaf can edit.
    read = _names_of(proc.tree) | {d for d, _refs in proc.record}
    dim_names = {tok for x in args.values() for tok in re.findall(r"[A-Za-z_]\w*", " ".join(
        str(t) for t in (x.get("dims") or [])))}
    unread = [n for n, x in args.items()
              if x.get("intent") == "in" and n not in read and n not in dim_names]
    if unread:
        bindings = ", ".join(f"unused_{n} => {n}" for n in unread)
        a(f"    associate ({bindings})")
        a("    end associate")
    for line in _wrap(f"{proc.out} = {expr}"):
        a(f"    {line}")
    if proc.calls:
        a(f"    {_ST}{proc.calls} = {_ST}{proc.calls} + 1.0_dp")
    if proc.record:
        counter = f"nint({_ST}{proc.calls})" if proc.calls else "1"
        a(f"    call_no = {counter}")
    for dummy, refs in proc.record:
        rank = runner_ir._dummy_rank(args[dummy])
        if rank == 0:
            sv = f"{_ST}{refs[0]}"
            a(f"    if (call_no <= size({sv})) {sv}(call_no) = {dummy}")
            continue
        for k, ref in enumerate(refs, start=1):
            a(f"    if (call_no == {k}) {_ST}{ref} = {dummy}")
    a(f"  {stanza[-1]}")


def _names_of(tree: Any) -> set[str]:
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}


def _wrap(text: str, width: int = 88) -> list[str]:
    """One statement over continuation lines of at most `width` columns, cut at a blank.

    A remainder with no blank before `width` is left whole, past `width`: the column check of
    `render_runner` refuses it, naming the line, once it reaches `max_rendered_line()`. A
    continuation's cut is searched after its own two-blank indent, whose blank at index 1 would
    otherwise be found, cut nothing, and loop forever."""
    out: list[str] = []
    rest = text
    while len(rest) > width:
        cut = rest.rfind(" ", 2 if out else 0, width)
        if cut <= 0:
            break
        out.append(rest[:cut] + " &")
        rest = "  " + rest[cut + 1:]
    out.append(rest)
    return out


def _actual(actual: runner_ir.Actual, block_first: dict[str, str] | None = None) -> str:
    form, ref = actual.form, actual.ref
    if form == "host":
        return ref.local
    if form == "state":
        return f"{_ST}{ref}"
    if form == "work":
        return f"{_WK}{ref}"
    if form == "logical":
        return f"{_LG}{ref}"
    if form == "integer":
        return f"{_IV}{ref}"
    if form == "state_int":
        return f"nint({_ST}{ref})"
    if form == "time":
        return "t_c"
    if form == "step":
        return "step_c"
    if form == "literal":
        return _literal(ref[0], ref[1])
    if form == "block_first":
        # The block's first global index: the distributed driver's offset of that axis, `1` on
        # the serial driver, whose block is the whole case (issue #610, B5).
        return block_first[ref] if block_first is not None else "1"
    return ref.name


def _render_call(a: Any, call: runner_ir.CallPlan, indent: str, *, timed: bool = False,
                 dist: _Dist | None = None) -> None:
    """Every logical `out` `.false.` before the call and written as 1 / 0 after it (D1). The
    scalars an `intent(out)` dummy takes are `volatile`: gfortran clobbers an `intent(out)` scalar
    actual at the call (measured, `-fdump-tree-original`), so at `-O2` a plain variable's reset
    is a dead store and a kernel that never assigns its result reads whatever the register held,
    `.true.` included.

    On the distributed driver (`dist`) the result is the ranks' together (issue #610, B8): the
    count of ranks whose result is true, summed over the ranks, decides it on every rank, so a
    refusal on one rank is the refusal everywhere and the loop exits on every rank at once."""
    for var in call.logical:
        a(f"{indent}{_LG}{var} = .false.")
    for var in call.integer:
        a(f"{indent}{_IV}{var} = 0")
    if timed:
        a(f"{indent}call system_clock(count=clock0)")
    a(f"{indent}call {call.operation}( &")
    actuals = [_actual(x, dist.block_first if dist is not None else None)
               for x in call.actuals]
    for i, text in enumerate(actuals):
        a(f"{indent}  {text}{', &' if i < len(actuals) - 1 else ')'}")
    if timed:
        a(f"{indent}call system_clock(count=clock1)")
        a(f"{indent}walltime = walltime + real(clock1 - clock0, dp) / real(clock_rate, dp)")
    if dist is not None:
        res = f"{_LG}{call.result_state}"
        a(f"{indent}nok = {dist.H('reduce_sum_int')}(merge(1, 0, {res}))")
        a(f"{indent}{res} = nok == nranks")
    for var in call.logical:
        a(f"{indent}{_ST}{var} = merge(1.0_dp, 0.0_dp, {_LG}{var})")
    for var in call.integer:
        a(f"{indent}{_ST}{var} = real({_IV}{var}, dp)")


@dataclass(frozen=True)
class _Dist:
    """What the distributed form of the driver adds to the serial one (issue #610, §3.3), for a
    harness that runs the program as several ranks. ``axes`` are `drive.distribute`'s; on a node
    with none every case is replicated and only the reductions and rank 0's writing remain.

    ``layouts``: the spatial arrays (a state by its name, a bound host array by its key), each
    with one entry per dimension, None where not spatial (`runner_ir.distribute_layouts`).
    ``gathered``: the spatial states the driver or the kernel writes, gathered onto rank 0 at a
    capture (B10); ``global_states``: the spatial states nothing writes, captured and folded from
    the global copy every rank read (B9, B10). ``table``: per process grid, the cases
    `distribute_plan` distributes (B6). ``init_checks``: the rank-0 real / integer states `init`
    writes, which must be finite and agree across the ranks (B8)."""
    H: Any
    axes: tuple[runner_ir.Axis, ...]
    layouts: dict[str, tuple[tuple[int, str] | None, ...]]
    gathered: tuple[str, ...]
    global_states: tuple[str, ...]
    table: tuple[tuple[tuple[int, int], tuple[str, ...]], ...]
    grids: tuple[tuple[int, int], ...]
    threads: int
    init_checks: tuple[str, ...]

    @property
    def block_first(self) -> dict[str, str]:
        return {axis.name: f"dlo{k}" for k, axis in enumerate(self.axes, start=1)}


def _distribution(ir: dict[str, Any], schema: dict[str, str], plan: runner_ir.DriverPlan,
                  H: Any, target: dict[str, Any]) -> _Dist:
    """The distributed facts of ``ir``, or RenderError for an IR they cannot be rendered from:
    the render backstop of gate (n) (`primary_evidence.drive_violations`), which every IR a
    render reaches has passed."""
    layouts, refusals = runner_ir.distribute_layouts(ir, schema)
    if refusals:
        raise RenderError("drive.distribute: " + "; ".join(refusals))
    spatial = {k: v for k, v in layouts.items() if any(d is not None for d in v)}
    gathered = tuple(v for v in plan.arrays if v in spatial and v in plan.written)
    for var in gathered:
        if any(d is not None and d[1] == "ghost" for d in spatial[var]):
            raise RenderError(f"{var}: an array the driver or the kernel writes has a ghost "
                              "layout, and only a cell or face layout is gathered (gate (n))")
    for k, axis in enumerate(plan.axes):
        for entry in plan.entries:
            cells, ghost, extended = entry.axis_hosts[k]
            if cells is None or (extended is not None and ghost is None):
                raise RenderError(
                    f"drive.distribute axis {axis.name}: a drive entry binds no rank-0 integer "
                    f"dummy to inputs.{axis.cells}"
                    + ("" if cells is None else f" with inputs.{axis.ghost}")
                    + " (gate (n))")
            needs_ghost = any(d is not None and d[0] == k and d[1] == "ghost"
                              for lay in spatial.values() for d in lay)
            if needs_ghost and ghost is None:
                raise RenderError(f"drive.distribute axis {axis.name}: an array has a ghost "
                                  f"layout and no rank-0 integer dummy binds inputs.{axis.ghost}")
    init = plan.entries[0].init
    checks = tuple(dict.fromkeys(
        x.ref for x in (init.actuals if init is not None else ())
        if (x.form == "state" and x.writes and plan.variables.get(x.ref) == 0)
        or x.form == "integer"))
    grids = runner_ir.distribute_grids(target)
    return _Dist(
        H=H, axes=plan.axes, layouts=spatial, gathered=gathered,
        global_states=tuple(v for v in plan.arrays if v in spatial and v not in plan.written),
        table=runner_ir.replication_table(ir, grids) if plan.axes else (), grids=grids,
        threads=_threads(target), init_checks=checks)


def _distributed_ops(plan: runner_ir.DriverPlan, dist: _Dist) -> list[str]:
    """The distributed-state operations the distributed form calls, in a fixed order: the start
    and end of the runtime, the rank and size, the grid the table is looked up by, the result's
    reduction (every call), the partition on a node with axes, the extremes' reductions where a
    fold or `init`'s agreement check takes them, and one gather per rank of a gathered array."""
    ops = ["init", "finalize", "comm_rank", "comm_size", "grid_dims", "reduce_sum_int"]
    if dist.axes:
        ops.append("partition")
    if dist.init_checks or (plan.loop and any(v in plan.written for v in plan.arrays)):
        ops += ["reduce_min", "reduce_max"]
    ops += [f"gather_r{r}" for r in sorted({plan.variables[v] for v in dist.gathered})]
    return ops


def _section(layout: tuple[tuple[int, str] | None, ...], ghost: dict[int, str]) -> str:
    """The global array's index ranges of a rank's block: the owned cells of a `cell`
    dimension, the cells and the upper face of a `face` one, the cells and the ghost margin on
    each side of a `ghost` one (the ghost-extended global position of cell `i` is `i + ng`, so
    the block's first position is the global position of its first owned cell)."""
    out = []
    for dim in layout:
        if dim is None:
            out.append(":")
            continue
        k, kind = dim[0] + 1, dim[1]
        upper = {"cell": f"dhi{k}", "face": f"dhi{k} + 1",
                 "ghost": f"dhi{k} + 2 * {ghost.get(dim[0], '')}"}[kind]
        out.append(f"dlo{k}:{upper}")
    return ", ".join(out)


def _owned(layout: tuple[tuple[int, str] | None, ...]) -> str:
    """The positions of a rank's block it contributes to the global array: all of a `cell`
    dimension and, along a `face` one, the cells plus the domain's last face on the last rank
    of the line (`dfe<k>`). Written arrays have no `ghost` dimension (`_distribution`)."""
    return ", ".join(f"1:dfe{d[0] + 1}" if d is not None and d[1] == "face" else ":"
                     for d in layout)


def _global_size(var: str, entry: runner_ir.EntryPlan, dist: _Dist) -> str:
    """The global element count of state ``var`` on the distributed form in a case ``entry``
    runs, for the perf record (B10): a spatial state that entry reads from the case file by its
    global copy; a spatial state it allocates (written by `init` or recorded, never `ghost`-laid)
    from the axes' global counts; any other state by its own size, since every rank holds it
    whole. Per ENTRY (review round 2): on a list-form drive a state read from the file by one
    entry may be recorded by another, whose case leaves the global copy unallocated or another
    case's."""
    layout = dist.layouts.get(var)
    if layout is None:
        return f"size({_ST}{var}, kind=int64)"
    if var in entry.from_file:
        return f"size({_GL}{var}, kind=int64)"
    return " * ".join(
        f"size({_ST}{var}, {i}, kind=int64)" if dim is None
        else f"int(dnc{dim[0] + 1}{' + 1' if dim[1] == 'face' else ''}, int64)"
        for i, dim in enumerate(layout, start=1))


def _render_cells_total(a: Any, sizes: list[str], ind: str) -> None:
    largest = ("max(" + ", ".join(sizes) + ")") if len(sizes) > 1 else (
        sizes[0] if sizes else "1_int64")
    for line in _wrap(f"cells_total = cells_total + int(step_c, int64) * {largest}"):
        a(f"{ind}{line}")


def _from_file_states(plan: runner_ir.DriverPlan) -> set[str]:
    return {v for entry in plan.entries for v in entry.from_file}


def _render_program(a: Any, plan: runner_ir.DriverPlan, spec_id: str, harness_spec_id: str,
                    H: Any, target_class: str, devices: int, dist: _Dist | None = None) -> None:
    from_module = {v.local for v in plan.procedure_hosts}
    records = set(plan.record_states)
    emit_ops = ["emit_real"] + [f"emit_array_r{r}" for r in plan.array_ranks]
    if plan.loop and 1 not in plan.array_ranks:
        emit_ops.insert(1, "emit_array_r1")
    harness_syms = [
        *[H(t) for t in _HARNESS_TYPES], H("parse_cases"), H("read_case"),
        *[H(g) for g in plan.getters], *[H(op) for op in emit_ops],
        H("box"), H("write_snapshot"), H("write_perf")]
    if dist is not None:
        harness_syms += [H(op) for op in _distributed_ops(plan, dist)]
    operations = list(dict.fromkeys(
        c.operation for e in plan.entries for c in ([e.init] if e.init else []) + [e.step]))
    from_file = _from_file_states(plan)
    spatial_hosts = [v for v in plan.hosts
                     if dist is not None and v.rank and v.key in dist.layouts]
    a("! Deterministic driver rendered by the host from the node's IR (issue #589). Per case it")
    a("! reads the host-written case file, allocates the state, calls the published operations,")
    a("! owns the time loop on a loop node, and writes every capture THROUGH the certified")
    a(f"! {harness_spec_id} plumbing, which owns all JSON assembly.")
    if dist is not None:
        a("! The program runs as several ranks (issue #610): every rank reads the whole case; on a")
        a("! case the replication table distributes it calls the operations on its own block, the")
        a("! partitioned state is gathered onto rank 0 at each capture, and rank 0 writes.")
    a(f"program {spec_id}_runner")
    a("  use, intrinsic :: iso_fortran_env, only: real64, int64, error_unit")
    if plan.work_vars or plan.loop:
        a("  use, intrinsic :: ieee_arithmetic, only: ieee_value, ieee_quiet_nan, ieee_is_finite")
    elif dist is not None and dist.init_checks:
        a("  use, intrinsic :: ieee_arithmetic, only: ieee_is_finite")
    a(f"  use {harness_spec_id}_model, only: &")
    for i, sym in enumerate(harness_syms):
        a(f"    {sym}{', &' if i < len(harness_syms) - 1 else ''}")
    a(f"  use {spec_id}_model, only: &")
    for i, op in enumerate(operations):
        a(f"    {op}{', &' if i < len(operations) - 1 else ''}")
    procedures = [p for entry in plan.entries for p in entry.procedures]
    if procedures:
        names = [*(p.name for p in procedures), *sorted(from_module),
                 *(f"{_ST}{v}" for v in plan.record_states)]
        a(f"  use {spec_id}_drv, only: &")
        for i, sym in enumerate(names):
            a(f"    {sym}{', &' if i < len(names) - 1 else ''}")
    # No `! allow(C003)` above it, deliberately: the lint gate imposes its rule set with
    # `--ignore-allow-comments`, so a directive here would be reported in a file no leaf can edit.
    a("  implicit none")
    a("")
    a("  integer, parameter :: dp = real64")
    a(f"  integer, parameter :: case_id_len = {CASE_ID_LEN}")
    a(f"  integer, parameter :: token_len = {TOKEN_LEN}")
    a("")
    a("  integer :: nargs, i, ci, ln, ncases, step_c, steps_total")
    a("  integer(int64) :: cells_total, clock0, clock1, clock_rate")
    a("  real(dp) :: walltime, t_c")
    a("  logical :: ok, case_refused")
    if dist is not None:
        a("  integer :: rank, nranks, p1, p2, nok")
        a("  logical :: grid_known")
        if dist.axes:
            a("  logical :: replicated")
            for k in range(1, len(dist.axes) + 1):
                a(f"  integer :: dnc{k}, dlo{k}, dhi{k}, dnl{k}, dfe{k}")
    a("  character(len=token_len), allocatable :: tokens(:)")
    a("  character(len=case_id_len), allocatable :: case_ids(:)")
    a("  character(len=:), allocatable :: cid")
    a(f"  type({H('h_named')}), allocatable :: vals(:)")
    if plan.list_form:
        a("  integer :: entry_no")
    if plan.loop:
        a("  integer :: n, n_c, every_c, n_done")
        a("  real(dp) :: dt_c, t_start_c")
        a("  real(dp), allocatable :: se(:, :)")
        a("  character(len=24) :: step_text")
    for var, rank in plan.variables.items():
        if var in records:
            continue
        if rank:
            a(f"  real(dp), allocatable :: {_ST}{var}({_colons(rank)})")
        else:
            a(f"  real(dp), volatile :: {_ST}{var}")
    if dist is not None:
        for var in plan.arrays:
            if var in dist.layouts and var in from_file:
                a(f"  real(dp), allocatable :: {_GL}{var}({_colons(plan.variables[var])})")
    for var in plan.work_vars:
        a(f"  real(dp), allocatable :: {_WK}{var}({_colons(plan.variables[var])})")
    for var in plan.logical_states:
        a(f"  logical, volatile :: {_LG}{var}")
    for var in plan.integer_states:
        a(f"  integer, volatile :: {_IV}{var}")
    for value in plan.hosts:
        if value.local in from_module:
            continue
        if value.rank:
            a(f"  real(dp), allocatable :: {value.local}({_colons(value.rank)})")
        else:
            decl = {"real": "real(dp)", "integer": "integer", "logical": "logical"}[value.kind]
            a(f"  {decl} :: {value.local}")
    for value in spatial_hosts:
        a(f"  real(dp), allocatable :: {value.local}g({_colons(value.rank)})")
    a("")
    if dist is not None:
        a("  ! --- the ranks, and the process grid the replication table is looked up by ---")
        a(f"  call {H('init')}()")
        a(f"  rank = {H('comm_rank')}()")
        a(f"  nranks = {H('comm_size')}()")
        a(f"  call {H('grid_dims')}(p1, p2)")
        a("  grid_known = .false.")
        for g1, g2 in dist.grids:
            a(f"  if (p1 == {g1} .and. p2 == {g2}) grid_known = .true.")
        a("  if (.not. grid_known) then")
        a("    write(error_unit, '(A)') 'error: the process grid is not one the replication "
          "table lists'")
        a("    error stop 1")
        a("  end if")
        a("")
    a("  ! --- read argv and parse the case set (--cases <spec> <case_id>...) --------")
    a("  nargs = command_argument_count()")
    a("  allocate(tokens(max(nargs, 1)))")
    a("  do i = 1, nargs")
    a("    call get_command_argument(i, tokens(i), length=ln)")
    a("    if (ln > token_len) then")
    a("      write(error_unit, '(A)') 'error: a command-line argument exceeds the token width'")
    a("      error stop 1")
    a("    end if")
    a("  end do")
    a("  allocate(case_ids(max(nargs, 1)))")
    a(f"  call {H('parse_cases')}(tokens, nargs, case_ids, ncases, ok)")
    a("  if (.not. ok) then")
    a("    write(error_unit, '(A)') 'error: --cases <spec> <case_id>... required'")
    a("    error stop 1")
    a("  end if")
    a("")
    a("  steps_total = 0")
    a("  cells_total = 0_int64")
    a("  walltime = 0.0_dp")
    a("  call system_clock(count_rate=clock_rate)")
    a("")
    a("  do ci = 1, ncases")
    a("    cid = trim(case_ids(ci))")
    a(f"    call {H('read_case')}(cid, ok)")
    a("    if (.not. ok) then")
    a("      write(error_unit, '(A)') 'error: no readable case file for case '//cid")
    a("      error stop 2")
    a("    end if")
    a("    step_c = 0")
    a("    t_c = 0.0_dp")
    a("    case_refused = .false.")
    if plan.list_form:
        a("    entry_no = 0")
        a("    select case (cid)")
        for k, entry in enumerate(plan.entries, start=1):
            for case_id in entry.cases or ():
                a(f"    case ('{_flit(case_id)}')")
                a(f"      entry_no = {k}")
        a("    case default")
        a("      write(error_unit, '(A)') 'error: no drive entry runs case '//cid")
        a("      error stop 2")
        a("    end select")
        a("    select case (entry_no)")
        for k, entry in enumerate(plan.entries, start=1):
            a(f"    case ({k})")
            _render_entry(a, plan, entry, H, "      ", dist)
        a("    case default")
        a("      write(error_unit, '(A)') 'error: no drive entry numbered for case '//cid")
        a("      error stop 2")
        a("    end select")
    else:
        _render_entry(a, plan, plan.entries[0], H, "    ", dist)
    a("    steps_total = steps_total + step_c")
    if dist is None:
        _render_cells_total(a, [f"size({_ST}{v}, kind=int64)" for v in plan.arrays], "    ")
    a("  end do")
    a("")
    a("  if (walltime <= 0.0_dp) walltime = 1.0e-9_dp")
    a("")
    a("  ! --- emit the perf record (harness owns its envelope) ------------------------")
    if dist is None:
        a(f"  call {H('write_perf')}(trim(case_ids(ncases)), '{_flit(target_class)}', &")
        a("    steps_total, int(min(cells_total, int(huge(0), int64))), walltime, 1, "
          f"{devices})")
    else:
        # The rank count is the one the run observes, never the profile's (B10).
        a("  if (rank == 0) then")
        a(f"    call {H('write_perf')}(trim(case_ids(ncases)), '{_flit(target_class)}', &")
        a("      steps_total, int(min(cells_total, int(huge(0), int64))), walltime, nranks, "
          f"{dist.threads}, &")
        a(f"      {devices})")
        a("  end if")
        a(f"  call {H('finalize')}()")
    a("")
    a("contains")
    a("")
    a("  ! Every declared state variable, `t` (the writer's) and `step`: the same set at every")
    a("  ! capture point of every case.")
    a("  subroutine capture_state(out)")
    a(f"    type({H('h_named')}), allocatable, intent(out) :: out(:)")
    if dist is None:
        a(f"    allocate(out({len(plan.variables) + 1}))")
        for k, (var, rank) in enumerate(plan.variables.items(), start=1):
            a(f"    out({k}) = {H('box')}('{_flit(var)}', &")
            a(f"      {H('emit_real') if rank == 0 else H(f'emit_array_r{rank}')}({_ST}{var}))")
        a(f"    out({len(plan.variables) + 1}) = {H('box')}('step', &")
        a(f"      {H('emit_real')}(real(step_c, dp)))")
    else:
        _render_distributed_capture(a, plan, dist, H)
    a("  end subroutine capture_state")
    if plan.loop:
        a("")
        a("  ! `n_done` entries of one series column; the empty list is this glue's own token,")
        a("  ! since no emitter contract covers a zero-length array.")
        a("  function series_json(column) result(s)")
        a("    integer, intent(in) :: column")
        a("    character(len=:), allocatable :: s")
        a("    if (n_done == 0) then")
        a("      s = '[]'")
        a("    else")
        a(f"      s = {H('emit_array_r1')}(se(1:n_done, column))")
        a("    end if")
        a("  end function series_json")
        if dist is None:
            local_ranks, reduced_ranks = plan.array_ranks, ()
        else:
            local_ranks = tuple(sorted({plan.variables[v] for v in plan.arrays
                                        if v not in plan.written}))
            reduced_ranks = tuple(sorted({plan.variables[v] for v in plan.arrays
                                          if v in plan.written}))
        for rank in local_ranks:
            cols = _colons(rank)
            for fn, intrinsic in (("min", "minval"), ("max", "maxval")):
                a("")
                a(f"  ! NaN-propagating: the intrinsic skips a NaN cell, and a committed non-finite")
                a("  ! cell must reach the series, whose loader refuses it.")
                a(f"  function fold_{fn}_r{rank}(arr) result(x)")
                a(f"    real(dp), intent(in) :: arr({cols})")
                a("    real(dp) :: x")
                a("    if (all(ieee_is_finite(arr))) then")
                a(f"      x = {intrinsic}(arr)")
                a("    else")
                a("      x = ieee_value(1.0_dp, ieee_quiet_nan)")
                a("    end if")
                a(f"  end function fold_{fn}_r{rank}")
        for rank in reduced_ranks:
            a("")
            a("  ! The extremes over every rank's owned cells (B9): a non-finite cell on any rank")
            a("  ! is a flag summed over the ranks, so no reduction is handed a NaN, and the entry")
            a("  ! is NaN on every rank. Each rank calls the reductions in the same order.")
            a(f"  subroutine dfold_r{rank}(arr, x_min, x_max)")
            a(f"    real(dp), intent(in) :: arr({_colons(rank)})")
            a("    real(dp), intent(out) :: x_min, x_max")
            a("    integer :: n_bad")
            a(f"    n_bad = {H('reduce_sum_int')}(merge(0, 1, all(ieee_is_finite(arr))))")
            a("    if (n_bad > 0) then")
            a("      x_min = ieee_value(1.0_dp, ieee_quiet_nan)")
            a("      x_max = x_min")
            a("    else")
            a(f"      x_min = {H('reduce_min')}(minval(arr))")
            a(f"      x_max = {H('reduce_max')}(maxval(arr))")
            a("    end if")
            a(f"  end subroutine dfold_r{rank}")
    if dist is not None and dist.init_checks:
        a("")
        a("  ! A scalar `init` wrote must be finite on every rank and equal across the ranks")
        a("  ! (B8): a value taken from a rank's own block is the readable cause here, rather")
        a("  ! than a disagreement many steps later. No reduction is handed a non-finite value.")
        a("  subroutine init_agree(name, x)")
        a("    character(len=*), intent(in) :: name")
        a("    real(dp), intent(in) :: x")
        a("    real(dp) :: x_min, x_max")
        a(f"    if ({H('reduce_sum_int')}(merge(0, 1, ieee_is_finite(x))) > 0) then")
        a("      write(error_unit, '(A)') 'error: init wrote a non-finite '//name// &")
        a("        ' on a rank for case '//cid")
        a("      error stop 2")
        a("    end if")
        a(f"    x_min = {H('reduce_min')}(x)")
        a(f"    x_max = {H('reduce_max')}(x)")
        a("    if (x_min /= x_max) then")
        a("      write(error_unit, '(A)') 'error: init wrote '//name// &")
        a("        ' unequal across the ranks for case '//cid")
        a("      error stop 2")
        a("    end if")
        a("  end subroutine init_agree")
    a("")
    a(f"end program {spec_id}_runner")


def _render_distributed_capture(a: Any, plan: runner_ir.DriverPlan, dist: _Dist, H: Any) -> None:
    """`capture_state` of the distributed form (B10): every rank takes part in each gather of a
    partitioned array the driver or the kernel writes (a collective), and rank 0 boxes. A
    replicated case gathers nothing (a gather of a whole array on every rank does not tile);
    an array nothing writes is boxed from the global copy every rank read; a scalar is rank
    0's."""
    gathered_ranks = sorted({plan.variables[v] for v in dist.gathered})
    for r in gathered_ranks:
        a(f"    real(dp), allocatable :: g_r{r}({_colons(r)})")
    a(f"    allocate(out({len(plan.variables) + 1}))")
    for k, (var, rank) in enumerate(plan.variables.items(), start=1):
        emit = H("emit_real") if rank == 0 else H(f"emit_array_r{rank}")
        box = f"out({k}) = {H('box')}('{_flit(var)}', &"
        if var in dist.gathered:
            layout = dist.layouts[var]
            slots: list[str] = []
            for axis in range(len(dist.axes)):
                dims = [i for i, d in enumerate(layout, start=1) if d is not None and d[0] == axis]
                if not dims:
                    slots += ["0", "1", "1", "1"]
                    continue
                kind = layout[dims[0] - 1][1]
                slots += [str(dims[0]), "1",
                          f"{'dfe' if kind == 'face' else 'dnl'}{axis + 1}", f"dlo{axis + 1}"]
            if len(dist.axes) == 1:
                slots += ["0", "1", "1", "1"]
            a("    if (replicated) then")
            a(f"      if (rank == 0) {box}")
            a(f"        {emit}({_ST}{var}))")
            a("    else")
            a(f"      call {H(f'gather_r{rank}')}({_ST}{var}, &")
            a(f"        {', '.join(slots[:4])}, &")
            a(f"        {', '.join(slots[4:])}, g_r{rank})")
            a(f"      if (rank == 0) {box}")
            a(f"        {emit}(g_r{rank}))")
            a("    end if")
            continue
        source = f"{_GL}{var}" if var in dist.layouts else f"{_ST}{var}"
        a(f"    if (rank == 0) {box}")
        a(f"      {emit}({source}))")
    a(f"    if (rank == 0) out({len(plan.variables) + 1}) = {H('box')}('step', &")
    a(f"      {H('emit_real')}(real(step_c, dp)))")


def _render_partition(a: Any, plan: runner_ir.DriverPlan, entry: runner_ir.EntryPlan,
                      dist: _Dist, ind: str) -> None:
    """The table lookup, the partition, the local extents (B4) and the block copies (B3): every
    spatial array — a state read from the case file and a bound host array alike, whatever the
    dummy's intent — is copied out of the global array into a fresh allocatable, never passed as
    a section. On a replicated case the block is the whole case and no index arithmetic runs (a
    case whose inputs are inconsistent reaches the kernel with its raw inputs, B6)."""
    from_file = [v for v in entry.from_file if v in dist.layouts]
    hosts = [v for v in entry.hosts if v.rank and v.key in dist.layouts]
    ghost = {k: hv[1].local for k, hv in enumerate(entry.axis_hosts) if hv[1] is not None}
    a(f"{ind}! --- the replication table (`distribute_plan` per process grid, issue #610 B6) ---")
    a(f"{ind}replicated = .true.")
    first = True
    for (g1, g2), ids in dist.table:
        ids = [c for c in ids if entry.cases is None or c in entry.cases]
        if not ids:
            continue
        a(f"{ind}{'if' if first else 'else if'} (p1 == {g1} .and. p2 == {g2}) then")
        first = False
        a(f"{ind}  select case (cid)")
        for case_id in ids:
            a(f"{ind}  case ('{_flit(case_id)}')")
            a(f"{ind}    replicated = .false.")
        a(f"{ind}  case default")
        a(f"{ind}    replicated = .true.")
        a(f"{ind}  end select")
    if not first:
        a(f"{ind}end if")
    for k, (cells, _ghost, _extended) in enumerate(entry.axis_hosts, start=1):
        a(f"{ind}dnc{k} = {cells.local}")
    a(f"{ind}if (replicated) then")
    for k in range(1, len(dist.axes) + 1):
        a(f"{ind}  dlo{k} = 1")
        a(f"{ind}  dhi{k} = dnc{k}")
    for var in from_file:
        a(f"{ind}  {_ST}{var} = {_GL}{var}")
    for value in hosts:
        a(f"{ind}  {value.local} = {value.local}g")
    a(f"{ind}else")
    for k in range(1, len(dist.axes) + 1):
        a(f"{ind}  call {dist.H('partition')}(dnc{k}, {k}, dlo{k}, dhi{k})")
    a(f"{ind}  ! the local extents (B4): every rank-0 integer bound to an axis's cell count")
    a(f"{ind}  ! takes the owned count, to its ghost-extended count the owned count + 2*ghost")
    for k, (cells, ghost_host, extended) in enumerate(entry.axis_hosts, start=1):
        a(f"{ind}  {cells.local} = dhi{k} - dlo{k} + 1")
        if extended is not None:
            a(f"{ind}  {extended.local} = {cells.local} + 2 * {ghost_host.local}")
    for local, glob, key in [*((f"{_ST}{v}", f"{_GL}{v}", v) for v in from_file),
                             *((v.local, f"{v.local}g", v.key) for v in hosts)]:
        extents = entry.extents.get(key)
        copy = f"{local} = {glob}({_section(dist.layouts[key], ghost)})"
        if extents is None:
            for line in _wrap(copy):
                a(f"{ind}  {line}")
            continue
        # A copy that holds no element carries no extents (the case file's `[]`), so the block
        # is allocated at its local extents instead: the gather and the call then see its shape.
        a(f"{ind}  if (size({glob}) > 0) then")
        for line in _wrap(copy):
            a(f"{ind}    {line}")
        a(f"{ind}  else")
        a(f"{ind}    if (allocated({local})) deallocate({local})")
        for line in _wrap(f"allocate({local}({', '.join(extents)}))"):
            a(f"{ind}    {line}")
        a(f"{ind}  end if")
    a(f"{ind}end if")
    for k in range(1, len(dist.axes) + 1):
        a(f"{ind}dnl{k} = dhi{k} - dlo{k} + 1")
        a(f"{ind}dfe{k} = dnl{k} + merge(1, 0, dhi{k} == dnc{k})")


def _render_entry(a: Any, plan: runner_ir.DriverPlan, entry: runner_ir.EntryPlan, H: Any,
                  ind: str, dist: _Dist | None = None) -> None:
    spatial_hosts = {v.key for v in entry.hosts
                     if dist is not None and v.rank and v.key in dist.layouts}
    a(f"{ind}! --- the case file, read whole before any capture ---")
    for value in entry.hosts:
        key = _flit(value.key)
        if value.rank:
            a(f"{ind}call {H(f'get_array_r{value.rank}')}( &")
            a(f"{ind}  '{key}', &")
            a(f"{ind}  {value.local}{'g' if value.key in spatial_hosts else ''})")
        else:
            a(f"{ind}{value.local} = {H(runner_ir.GETTERS[value.kind])}( &")
            a(f"{ind}  '{key}')")
    for var in entry.from_file:
        rank = plan.variables[var]
        local = f"{_GL if dist is not None and var in dist.layouts else _ST}{var}"
        if rank:
            a(f"{ind}call {H(f'get_array_r{rank}')}('{_flit(var)}', &")
            a(f"{ind}  {local})")
        else:
            a(f"{ind}{_ST}{var} = {H('get_real')}('{_flit(var)}')")
    if dist is not None and dist.axes:
        _render_partition(a, plan, entry, dist, ind)
    for var in entry.zero_scalars:
        a(f"{ind}{_ST}{var} = 0.0_dp")
    for var, extents in entry.zero_arrays.items():
        a(f"{ind}if (allocated({_ST}{var})) deallocate({_ST}{var})")
        for line in _wrap(f"allocate({_ST}{var}({', '.join(extents)}))"):
            a(f"{ind}{line}")
        a(f"{ind}{_ST}{var} = 0.0_dp")
    for var in plan.work_vars:
        a(f"{ind}if (allocated({_WK}{var})) deallocate({_WK}{var})")
        a(f"{ind}allocate({_WK}{var}, mold={_ST}{var})")
    if plan.loop:
        for key, target in (("t_start", "t_start_c"), ("dt", "dt_c")):
            form, ref = entry.loop_values[key]
            if form == "host":
                a(f"{ind}{target} = {ref.local}")
            else:
                a(f"{ind}{target} = 0.0_dp")
        form, ref = entry.loop_values["n_step"]
        a(f"{ind}n_c = {'nint(' + ref.local + ')' if form == 'host' else '0'}")
        a(f"{ind}every_c = nint({entry.loop_values['every'][1].local})")
        a(f"{ind}t_c = t_start_c")
    if entry.init is not None:
        a(f"{ind}! --- init, on the state itself; a false result refuses the case ---")
        _render_call(a, entry.init, ind, dist=dist)
        a(f"{ind}case_refused = .not. {_LG}{entry.init.result_state}")
        if dist is not None and dist.init_checks:
            a(f"{ind}if (.not. case_refused) then")
            for var in dist.init_checks:
                a(f"{ind}  call init_agree('{_flit(var)}', {_ST}{var})")
            a(f"{ind}end if")
        if plan.loop:
            state_loop = {k: v[1] for k, v in entry.loop_values.items() if v[0] == "state"}
            if state_loop:
                a(f"{ind}if (.not. case_refused) then")
                if "t_start" in state_loop:
                    a(f"{ind}  t_start_c = {_ST}{state_loop['t_start']}")
                    a(f"{ind}  t_c = t_start_c")
                if "dt" in state_loop:
                    a(f"{ind}  dt_c = {_ST}{state_loop['dt']}")
                    a(f"{ind}  if (.not. dt_c > 0.0_dp) then")
                    a(f"{ind}    write(error_unit, '(A)') 'error: init wrote a dt <= 0 for case '//cid")
                    a(f"{ind}    error stop 2")
                    a(f"{ind}  end if")
                if "n_step" in state_loop:
                    a(f"{ind}  n_c = nint({_ST}{state_loop['n_step']})")
                    a(f"{ind}  if (n_c < 1) then")
                    a(f"{ind}    write(error_unit, '(A)') 'error: init wrote n_step < 1 for case '//cid")
                    a(f"{ind}    error stop 2")
                    a(f"{ind}  end if")
                a(f"{ind}end if")
            a(f"{ind}if (case_refused) n_c = 0")
    write = f"{ind}if (rank == 0) " if dist is not None else ind
    a(f"{ind}! --- the initial capture (raw/state_snapshots/initial/<case_id>.json) ---")
    a(f"{ind}call capture_state(vals)")
    a(f"{write}call {H('write_snapshot')}('initial/'//cid, vals, t_c)")
    a(f"{ind}deallocate(vals)")
    if plan.loop:
        _render_loop(a, plan, entry, H, ind, dist)
    else:
        a(f"{ind}if (.not. case_refused) then")
        a(f"{ind}  ! --- one call on the state itself: what it leaves is the captured state ---")
        _render_call(a, entry.step, ind + "  ", timed=True, dist=dist)
        a(f"{ind}  if ({_LG}{entry.step.result_state}) step_c = 1")
        a(f"{ind}end if")
    a(f"{ind}! --- the final capture (raw/state_snapshots/<case_id>.json) ---")
    a(f"{ind}call capture_state(vals)")
    a(f"{write}call {H('write_snapshot')}(cid, vals, t_c)")
    a(f"{ind}deallocate(vals)")
    if plan.loop:
        names = plan.series_names
        inner = ind
        if dist is not None:
            a(f"{ind}if (rank == 0) then")
            inner = ind + "  "
        a(f"{inner}allocate(vals({len(names)}))")
        for col, name in enumerate(names, start=1):
            a(f"{inner}vals({col}) = {H('box')}('{_flit(name)}', series_json({col}))")
        a(f"{inner}call {H('write_snapshot')}('series/'//cid, vals, t_c)")
        a(f"{inner}deallocate(vals)")
        if dist is not None:
            a(f"{ind}end if")
        a(f"{ind}deallocate(se)")
    if dist is not None:
        # The GLOBAL extents (B10), in the entry that ran the case: a sum of the ranks' blocks
        # would count a replicated case once per rank, and a rank's own block is not the case's
        # largest array.
        _render_cells_total(a, [_global_size(v, entry, dist) for v in plan.arrays], ind)


def _render_loop(a: Any, plan: runner_ir.DriverPlan, entry: runner_ir.EntryPlan, H: Any,
                 ind: str, dist: _Dist | None = None) -> None:
    a(f"{ind}n_done = 0")
    a(f"{ind}allocate(se(max(n_c, 0), {len(plan.series_names)}))")
    a(f"{ind}do n = 1, n_c")
    in1 = ind + "  "
    for var in entry.work_copy:
        a(f"{in1}{_WK}{var} = {_ST}{var}")
    for var in entry.work_nan:
        a(f"{in1}{_WK}{var} = ieee_value(1.0_dp, ieee_quiet_nan)")
    _render_call(a, entry.step, in1, timed=True, dist=dist)
    a(f"{in1}if (.not. {_LG}{entry.step.result_state}) exit")
    a(f"{in1}! --- committed: the state, step, t, the series entry, the sample ---")
    for var in (*entry.work_copy, *entry.work_nan):
        a(f"{in1}{_ST}{var} = {_WK}{var}")
    a(f"{in1}step_c = n")
    a(f"{in1}t_c = t_start_c + real(n, dp) * dt_c")
    a(f"{in1}n_done = n")
    a(f"{in1}se(n, 1) = real(n, dp)")
    col = 2
    for var in plan.arrays:
        rank = plan.variables[var]
        if dist is not None and var in plan.written:
            # B9: a written array's extremes over the owned positions, reduced over the ranks.
            owned = _owned(dist.layouts[var]) if var in dist.layouts else _colons(rank)
            a(f"{in1}call dfold_r{rank}({_ST}{var}({owned}), &")
            a(f"{in1}  se(n, {col}), se(n, {col + 1}))")
        else:
            # An array nothing writes: its global copy, the same on every rank and every grid.
            source = f"{_GL}{var}" if dist is not None and var in dist.layouts else f"{_ST}{var}"
            a(f"{in1}se(n, {col}) = fold_min_r{rank}({source})")
            a(f"{in1}se(n, {col + 1}) = fold_max_r{rank}({source})")
        col += 2
    a(f"{in1}if (every_c > 0) then")
    a(f"{in1}  if (mod(n, every_c) == 0) then")
    a(f"{in1}    write(step_text, '(I0)') n")
    a(f"{in1}    call capture_state(vals)")
    if dist is not None:
        a(f"{in1}    if (rank == 0) call {H('write_snapshot')}( &")
    else:
        a(f"{in1}    call {H('write_snapshot')}( &")
    a(f"{in1}      'history/'//cid//'__'//trim(step_text), vals, t_c)")
    a(f"{in1}    deallocate(vals)")
    a(f"{in1}  end if")
    a(f"{in1}end if")
    a(f"{ind}end do")


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

subroutine harness_fortran_cpu__read_case(case_id, ok)
  character(len=*), intent(in) :: case_id
  logical, intent(out) :: ok
end subroutine harness_fortran_cpu__read_case

function harness_fortran_cpu__get_real(name) result(x)
  character(len=*), intent(in) :: name
  real(dp) :: x
end function harness_fortran_cpu__get_real

function harness_fortran_cpu__get_int(name) result(i)
  character(len=*), intent(in) :: name
  integer :: i
end function harness_fortran_cpu__get_int

function harness_fortran_cpu__get_bool(name) result(b)
  character(len=*), intent(in) :: name
  logical :: b
end function harness_fortran_cpu__get_bool

subroutine harness_fortran_cpu__get_array_r1(name, a)
  character(len=*), intent(in) :: name
  real(dp), allocatable, intent(out) :: a(:)
end subroutine harness_fortran_cpu__get_array_r1

subroutine harness_fortran_cpu__get_array_r2(name, a)
  character(len=*), intent(in) :: name
  real(dp), allocatable, intent(out) :: a(:,:)
end subroutine harness_fortran_cpu__get_array_r2

subroutine harness_fortran_cpu__get_array_r3(name, a)
  character(len=*), intent(in) :: name
  real(dp), allocatable, intent(out) :: a(:,:,:)
end subroutine harness_fortran_cpu__get_array_r3

subroutine harness_fortran_cpu__get_array_r4(name, a)
  character(len=*), intent(in) :: name
  real(dp), allocatable, intent(out) :: a(:,:,:,:)
end subroutine harness_fortran_cpu__get_array_r4
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

# Verbatim copy of `harness_fortran_cpu_mpi@0.4.0` §5.1's distributed-state operations (§3.3),
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


# `harness_fortran_cpu@0.10.0` §5.1's `__write_perf` (§3.2, issue #512), as the language backend
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
    #: driver then takes its distributed form (`render_runner`).
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
    """Whether `harness_spec_id` runs the program as several ranks. A harness this renderer does
    not target answers no (its pin refuses it)."""
    pin = _HARNESS_PINS.get((harness_spec_id or "").strip())
    return pin is not None and pin.distributed


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
        # The operations a physics source may call on this harness (`physics_harness_uses`) are
        # pinned too: the certified harness must still publish them as the embedded block states.
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


def physics_harness_uses(harness_spec_id: str) -> dict[str, frozenset[str]]:
    """`{module: names}` a physics source may `use` from the harness (`host_render.
    physics_harness_uses`): its distributed-state operations for a physics source
    (`PHYSICS_CALLABLE_DISTRIBUTED_OPS`), on a harness that runs the program as several ranks."""
    if not _is_distributed_harness(harness_spec_id):
        return {}
    sid = harness_spec_id.strip()
    return {f"{sid}_model": frozenset(_hname(sid, op) for op in PHYSICS_CALLABLE_DISTRIBUTED_OPS)}
