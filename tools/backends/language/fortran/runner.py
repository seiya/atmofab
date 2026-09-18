"""The Fortran backend's `runner_render` capability: a physics node's host-authored runner glue.

Pure-function module reached through `registry.capability_module("language", <lang>,
"runner_render")` — the neutral seam is `tools/host_render.py`, which is what the conductor and
the compile gate call. Nothing outside this package imports it by name.

It takes a compiled IR plus the target/harness
spec ids and returns the text of ``<spec_id>_runner.f90`` — the deterministic
"glue" main program that drives the physics node's ``<spec_id>_checks`` callbacks
and emits the standard runner outputs *through the certified
``harness_fortran_cpu`` plumbing*. Because the harness's v3 interface owns the
JSON envelope assembly and the verdict fold (§3 / §5.1 of the harness
controlled_spec), this renderer holds **no serialization knowledge**: it builds
the harness record types and calls the writers — it never formats a JSON token,
folds a verdict, or excludes an xfail itself.

Split of authorship on an M3c node:
- ``<spec_id>_model.f90`` — the physics kernel + ``__apply`` op   (LLM leaf)
- ``<spec_id>_checks.f90`` — the fixed-ABI check/metric callbacks + the
  module-level state storage the snapshot is captured from   (LLM leaf)
- ``<spec_id>_runner.f90`` — this renderer                        (host)
- ``src/Makefile``          — ``workflow_conductor._write_makefile`` (host)

The rendered runner ``use``s two modules: ``harness_fortran_cpu_model`` (the
certified plumbing) and ``<spec_id>_checks`` (the leaf's fixed-ABI callbacks AND
its bound state storage, see ``docs/workflow/CHECKS_MODULE_CONTRACT.md``). Snapshot
capture is the runner's, not the module's (Z6, issue #255): every snapshot variable
is a module-level ``real(dp)`` variable of ``<spec_id>_checks`` named exactly as the IR
declares it, imported as ``sb_<var> => <var>`` and serialized by the certified harness
emitters twice per case — right after ``case_setup`` (``raw/state_snapshots/initial/
<case_id>.json``) and right after ``case_run`` (``raw/state_snapshots/<case_id>.json``)
— before any check or metric callback of that case runs. Generated code contributes
the binding (the declaration and the allocation) and nothing downstream of it: there
is no getter, so no generated procedure can filter, compute or rewrite a captured
value (``zero_base_architecture.md`` §A4). It is authored lint-clean
(``use only:``, a bare ``implicit none`` with NO allow directive, ≤99-column
lines) so the deterministic
Generate.gate lint checker — which lints the whole ``src/`` tree — stays green.

``render_runner`` raises ``RenderError`` (→ transport fail_closed, NOT a Generate
retry) for any IR it cannot faithfully render: an unparseable ``shape_expr``, a
rank>4 snapshot variable, a snapshot variable colliding with a harness-reserved
key, a ``verdict.fields`` outside ``{overall, failed_checks}``, more than one
infrastructure dependency, or an over-long identifier. ``assert_harness_pin``
(the signature pin) is a separate fail-closed guard the conductor runs against
the *certified* harness IR signatures + source before rendering.
"""

from __future__ import annotations

import re
from typing import Any

from tools.backends.language.fortran import bundle
from tools.backends.language.fortran import lines as fortran_lines

# Neutral policy this emitter also enforces at render time.
#
# `RenderError` is the seam's class, not this module's, and importing it is what makes the
# neutral `except RenderError:` clause in the conductor work: a class obtained through
# `registry.load` is a different object to the one an `except` in a neutral module names.
# `CASE_ID_TOKEN_RE` is the safe-token grammar a case_id must obey — it reaches a filesystem
# path and an argv, neither of which is a language question.
from tools.host_render import RenderError
from tools.spec_input_gates import CASE_ID_TOKEN_RE

# The fixed ABI of the leaf-authored `<spec_id>_checks` module (see
# docs/workflow/CHECKS_MODULE_CONTRACT.md). Non-prefixed public names (module
# scope makes them collision-free) so the f2008 63-char identifier limit is not
# exceeded even for a 55-char spec_id. Five procedures: the former snapshot getters
# (`get_scalar` / `get_r1..r4`) are retired — the runner reads the bound module-level
# state directly (`STATE_BINDING_PREFIX`), so a value-returning getter is
# unrepresentable rather than policed (Z6, issue #255).
CHECKS_PUBLIC_NAMES = (
    "case_setup", "case_run", "get_time",
    "checks_compute", "metric_compute",
)

# The dummy arguments of `metric_compute`, in the order the rendered call passes its actuals
# (`_render_metric_calls`), and the one of them whose declaration the compiler cannot check
# against that call. The runner passes `mreason` — `character(len=:), allocatable`, UNALLOCATED
# — for `reason_na`. Fortran lets an allocatable actual associate with a non-allocatable dummy,
# so a `character(len=64)` or `character(len=*)` dummy resolves against the explicit interface
# with no diagnostic from `gfortran -fsyntax-only` (measured, 11.4: rc=0 for both) and faults
# at the first call at run time (the assignment inside writes through a null descriptor; with
# `-fcheck=all` it is "Allocatable actual argument 'mreason' is not allocated"). Only an
# `allocatable` dummy is conforming, and once the dummy IS allocatable the compiler owns the
# rest: a fixed-length allocatable dummy is refused ("must have a deferred length type
# parameter if and only if the dummy has one"), a `pointer` one is refused too. So the one
# fact the compiler cannot see is the attribute, and `checks_abi_dummy_violation` reads
# exactly that one (issue #261: the first bundle of a billed run carried the `len=64` form
# through every deterministic gate and was rejected only by the LLM verify).
METRIC_COMPUTE_DUMMIES = ("case_id", "name", "val", "is_na", "reason_na", "found")
METRIC_COMPUTE_DEFERRED_LENGTH_DUMMY = "reason_na"

# The runner-local alias of a bound snapshot variable: `use <spec_id>_checks, only:
# sb_<var> => <var>`. The prefix keeps the alias clear of every runner local (none starts
# with `sb_`) and of the ABI names, so an IR variable named like a runner local (`i`,
# `ok`, `vals`) still renders. `_snapshot_schema` bounds `sb_<var>` at the identifier limit.
STATE_BINDING_PREFIX = "sb_"
_IDENTIFIER_RE = re.compile(bundle.IDENTIFIER_PATTERN)

# Harness-owned snapshot keys a physics snapshot variable must not shadow.
HARNESS_RESERVED_SNAPSHOT_KEYS = frozenset({"t", "case_id", "step"})

# Fixed character width the checks ABI pins for a check status: assumed-length
# intent(out) is disallowed, so both sides declare this exact width. The check
# *id* is no longer a pinned width — the runner supplies each declared id as a
# literal `intent(in)` actual (per-id ABI), so the module never buffers ids.
CHECK_STATUS_WIDTH = 4

# The fixed width of a parsed case id. The rendered runner declares its `case_ids(:)` buffer
# `character(len=case_id_len)` and passes it to `__parse_cases` as an `intent(out)` actual, so
# this MUST equal the harness's own `case_id_len` (harness controlled_spec §3 pins the value; an
# assumed-length `intent(out)` character dummy is disallowed, which is why the width is fixed at
# all). `assert_harness_pin` enforces that equality against the certified harness source, so the
# two cannot drift.
#
# It also bounds the declared case ids: a longer one is truncated into the buffer, while the
# `select case` labels and `find_case_index` literals this renderer emits carry the full id — so
# `trim(case_ids(ci))` would never match, and every run would `error stop 1` with "target case
# not run" from a runner that compiled cleanly. The 100-column lint guard does not catch it (a
# bare `case ('<id>')` label only reaches column 100 at ~87 chars), so `_case_ids` bounds it.
CASE_ID_LEN = 64

# The bound on a spec_id, DERIVED rather than restated: the longest name generated from a
# spec_id appends a 7-character role suffix (`_runner` / `_checks`), and `bundle.IDENTIFIER_MAX`
# is this language's identifier limit — the one place that number is spelled for this backend.
# The extra character is margin. `tools/spec_input_gates.MAX_SPEC_ID_LEN` carries the same bound
# as a pre-IR spec-input precondition (it runs before any language is resolved, so it cannot ask
# for this one); `test_fortran_runner` pins the two equal so they cannot drift, and the render
# time check below stays as the backstop.
MAX_SPEC_ID_LEN = bundle.IDENTIFIER_MAX - len("_runner") - 1


# The deterministic Generate.gate lint-checker column limit (fortitude S001). The rendered runner must stay
# within it because it is host-authored (a leaf cannot edit it to fix an overlong line).
MAX_RENDERED_LINE = 100

# The harness symbols this template calls (pinned by assert_harness_pin against
# the certified harness IR). Emitters are added per-rank on demand.
_HARNESS_TYPES = (
    "h_named", "h_check", "h_metric", "h_case_result", "h_mb_entry",
)
_HARNESS_CORE_OPS = (
    "parse_cases", "box", "write_snapshot",
    "write_metrics_basis", "write_diagnostics", "write_perf",
)


# --- IR extraction helpers (all defensive: tolerate missing/mistyped nodes) ---


def _dget(node: Any, key: str, default: Any = None) -> Any:
    return node.get(key, default) if isinstance(node, dict) else default


def _rank_of_shape(shape_expr: Any, var: str) -> int:
    """Rank (0..4) of a snapshot variable's ``shape_expr`` (``"scalar"`` or
    ``"[d1, d2, ...]"``). Raises RenderError for an unparseable form or rank>4."""
    if not isinstance(shape_expr, str) or not shape_expr.strip():
        raise RenderError(f"snapshot variable {var!r} has no shape_expr")
    s = shape_expr.strip()
    if s.lower() == "scalar":
        return 0
    if not (s.startswith("[") and s.endswith("]")):
        raise RenderError(
            f"snapshot variable {var!r} shape_expr {shape_expr!r} is neither "
            "'scalar' nor a '[...]' array shape")
    inner = s[1:-1].strip()
    if not inner:
        raise RenderError(
            f"snapshot variable {var!r} shape_expr {shape_expr!r} has empty dimensions")
    rank = len([d for d in inner.split(",") if d.strip()])
    if rank < 1 or rank > 4:
        raise RenderError(
            f"snapshot variable {var!r} shape_expr {shape_expr!r} has rank {rank} "
            "(the harness emitters cover rank 1..4 only)")
    return rank


def _snapshot_schema(ir: dict[str, Any]) -> tuple[dict[str, str], str]:
    """Return ``({var_name: shape_expr}, time_variable)`` from
    ``io_contract.raw_requirements.required_evidence[state_snapshots].schema``.

    Same section `_author_snapshot_schema` (workflow_conductor) reads."""
    io = _dget(ir, "io_contract", {})
    rr = _dget(io, "raw_requirements", {})
    entry = None
    for e in _dget(rr, "required_evidence", []) or []:
        if isinstance(e, dict) and e.get("artifact") == "state_snapshots":
            entry = e
            break
    if entry is None:
        raise RenderError(
            "IR io_contract has no state_snapshots required_evidence entry "
            "(a rendered runner needs the snapshot schema to emit per-case state)")
    schema = _dget(entry, "schema", {})
    variables: dict[str, str] = {}
    for v in _dget(schema, "variables", []) or []:
        if isinstance(v, dict) and isinstance(v.get("name"), str) and v["name"].strip():
            variables[v["name"].strip()] = v.get("shape_expr")
    if not variables:
        raise RenderError("state_snapshots schema declares no variables")
    time_var = schema.get("time_variable")
    time_var = time_var.strip() if isinstance(time_var, str) and time_var.strip() else "t"
    seen_folded: dict[str, str] = {}
    abi_folded = {n.casefold() for n in CHECKS_PUBLIC_NAMES}
    for name in variables:
        if name in HARNESS_RESERVED_SNAPSHOT_KEYS:
            raise RenderError(
                f"snapshot variable {name!r} collides with a harness-reserved key "
                f"{sorted(HARNESS_RESERVED_SNAPSHOT_KEYS)}")
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
    return variables, time_var


def _case_ids(ir: dict[str, Any]) -> list[str]:
    case = _dget(ir, "case", {})
    out: list[str] = []
    for c in _dget(case, "test_case_set", []) or []:
        cid = _dget(c, "case_id")
        if isinstance(cid, str) and cid.strip():
            out.append(cid.strip())
    if not out:
        raise RenderError("IR case.test_case_set is empty (no cases to run)")
    # A case_id is concatenated straight into the per-case snapshot PATH by the harness
    # (`raw/state_snapshots/'//trim(case_id)//'.json'`), so an id containing `/` or `..`
    # traverses out of the run directory and the (cleanly compiling) runner writes an arbitrary
    # file. `_flit` only bars non-printable-ASCII and the length gate only bounds size, so both
    # let `../evil` through. Restrict the id to a filesystem-and-Fortran-safe token.
    unsafe = sorted({c for c in out if not CASE_ID_TOKEN_RE.match(c) or ".." in c})
    if unsafe:
        raise RenderError(
            f"case_id(s) {unsafe} are not safe tokens; a case_id is concatenated into the "
            "per-case snapshot path (raw/state_snapshots/<case_id>.json) and reaches the "
            "runner's argv, so it must match [A-Za-z0-9._][A-Za-z0-9._-]* with no '..' "
            "(a leading '-' would be read as an option; anything else escapes the run "
            "directory at runtime)")
    # Duplicate case_ids would render two identical `case ('id')` labels in the runner's
    # `select case`, a hard gfortran error the leaf cannot repair (host-rendered runner).
    # Fail closed rather than emit a non-compiling, unrepairable runner.
    dups = sorted({c for c in out if out.count(c) > 1})
    if dups:
        raise RenderError(
            f"IR case.test_case_set has duplicate case_id(s) {dups}; the runner's "
            "select-case would emit overlapping case labels that do not compile")
    # A case_id longer than the harness's `case_id_len` is truncated when `__parse_cases`
    # stores it, so it can never match the full-length literal the runner compares against.
    # The result compiles and then error-stops on every run — fail closed at Compile instead,
    # where a re-author can shorten the id.
    too_long = sorted({c for c in out if len(c) > CASE_ID_LEN})
    if too_long:
        raise RenderError(
            f"case_id(s) {too_long} exceed the harness case_id_len ({CASE_ID_LEN} chars); "
            "`__parse_cases` truncates a parsed case id to that width, so the runner's "
            "select-case label and metrics-basis lookup could never match it at runtime. "
            f"Shorten the case_id to ≤{CASE_ID_LEN} chars")
    return out


def _test_predicates(ir: dict[str, Any]) -> list[dict[str, Any]]:
    io = _dget(ir, "io_contract", {})
    return [p for p in (_dget(io, "test_predicates", []) or []) if isinstance(p, dict)]


def _xfail_cases(ir: dict[str, Any]) -> set[str]:
    """Case ids whose failure is expected (targeted by an ``xfail`` predicate)."""
    xfail: set[str] = set()
    for p in _test_predicates(ir):
        if str(p.get("expected_outcome") or "").strip().lower() == "xfail":
            for tc in p.get("target_cases") or []:
                if isinstance(tc, str) and tc.strip():
                    xfail.add(tc.strip())
    return xfail


def _target_cases(ir: dict[str, Any], test_id: str) -> list[str]:
    """All distinct case ids targeted by the predicate(s) for ``test_id``, in
    declaration order. This is the metrics-basis row set of that test: the runner
    records one ``h_mb_entry`` per ``(test_id, case_id)`` pair, and the post_execute
    completeness matrix (``_validate_metrics_basis_per_test``) is anchored on the
    same field."""
    seen: list[str] = []
    for p in _test_predicates(ir):
        if str(p.get("test_id") or "").strip() == test_id:
            for tc in p.get("target_cases") or []:
                if isinstance(tc, str) and tc.strip() and tc.strip() not in seen:
                    seen.append(tc.strip())
    return seen


def _per_case_vars(ir: dict[str, Any], schema_vars: dict[str, str]) -> dict[str, list[str]]:
    """Map each case_id to the union of ``required_raw_variables`` over the tests targeting
    that case, ordered by the snapshot schema declaration order. Since Z6 this is a
    VALIDATION (each entry must be a schema variable) and the metrics-basis pick set — the
    runner captures every schema variable for every case, not this per-case subset."""
    io = _dget(ir, "io_contract", {})
    req_by_test: dict[str, list[str]] = {}
    for r in _dget(io, "test_evidence_requirements", []) or []:
        if not isinstance(r, dict):
            continue
        tid = str(r.get("test_id") or "").strip()
        if tid:
            req_by_test[tid] = [
                v.strip() for v in (r.get("required_raw_variables") or [])
                if isinstance(v, str) and v.strip()
            ]
    schema_order = list(schema_vars)
    per_case: dict[str, set[str]] = {}
    for p in _test_predicates(ir):
        tid = str(p.get("test_id") or "").strip()
        needed = set(req_by_test.get(tid, []))
        for tc in p.get("target_cases") or []:
            if isinstance(tc, str) and tc.strip():
                per_case.setdefault(tc.strip(), set()).update(needed)
    out: dict[str, list[str]] = {}
    for cid in _case_ids(ir):
        want = per_case.get(cid, set())
        missing = [v for v in want if v not in schema_vars]
        if missing:
            raise RenderError(
                f"case {cid!r} requires raw variables {sorted(missing)} absent from the "
                "state_snapshots schema (required_raw_variables must be snapshot variables)")
        out[cid] = [v for v in schema_order if v in want]
    return out


def _checks(ir: dict[str, Any]) -> list[str]:
    io = _dget(ir, "io_contract", {})
    dc = _dget(io, "diagnostics_contract", {})
    ids: list[str] = []
    for c in _dget(dc, "checks", []) or []:
        cid = _dget(c, "id")
        if isinstance(cid, str) and cid.strip():
            sid = cid.strip()
            # The old buffered ABI implied a de-facto 32-char id ceiling (chk_ids width). With that
            # gone, bound the raw id at CASE_ID_LEN (symmetric with the case-id cap) as a first
            # gate. This alone is NOT sufficient — see the escaped-width check below.
            if len(sid) > CASE_ID_LEN:
                raise RenderError(
                    f"check id {sid!r} is {len(sid)} chars (>{CASE_ID_LEN}); the per-id "
                    "checks_compute call would breach the 100-column runner lint guard")
            ids.append(sid)
    if not ids:
        raise RenderError("IR diagnostics_contract declares no checks")
    # The width-binding rendered line per id is the assignment `    case_checks(<k>)%id = '<lit>'`,
    # whose columns are 25 + digits(k) + len(_flit(id)) — `_flit` DOUBLES embedded apostrophes, so a
    # raw-<=64 id can still expand past the limit. A line of EXACTLY MAX_RENDERED_LINE slips past
    # `render_runner`'s `> 100` backstop yet fails the S001 lint (which fires AT 100) on a line the
    # host authors and the Generate leaf cannot repair — an unrepairable fail_closed. Fail closed
    # HERE instead, keeping the widest such line strictly under the limit. (The `checks_compute`
    # continuation `      '<lit>', cstatus)` is 18 + len(_flit(id)) — always narrower than the
    # assignment, so bounding the assignment bounds both.)
    for k, sid in enumerate(ids, start=1):
        width = 25 + len(str(k)) + len(_flit(sid))
        if width >= MAX_RENDERED_LINE:
            raise RenderError(
                f"check id {sid!r} renders a {width}-column `case_checks(...)%id` assignment "
                f"(>= the {MAX_RENDERED_LINE}-column S001 lint limit) after Fortran apostrophe "
                "escaping; the host-authored runner line would fail Generate.gate lint check unrepairably. "
                "Declare a shorter check id (or one with fewer apostrophes).")
    return ids


def _metrics(ir: dict[str, Any]) -> list[str]:
    """Dotted metric addresses from ``diagnostics_contract.metrics`` (may be empty)."""
    io = _dget(ir, "io_contract", {})
    dc = _dget(io, "diagnostics_contract", {})
    out: list[str] = []
    for m in _dget(dc, "metrics", []) or []:
        if isinstance(m, str) and m.strip():
            out.append(m.strip())
        elif isinstance(m, dict):
            addr = m.get("address") or m.get("name") or m.get("id")
            if isinstance(addr, str) and addr.strip():
                out.append(addr.strip())
    return out


def _verify_verdict_fields(ir: dict[str, Any]) -> None:
    io = _dget(ir, "io_contract", {})
    dc = _dget(io, "diagnostics_contract", {})
    verdict = _dget(dc, "verdict", {})
    fields = _dget(verdict, "fields", []) or []
    allowed = {"overall", "failed_checks"}
    extra = {str(f).strip() for f in fields} - allowed
    if extra:
        raise RenderError(
            f"diagnostics_contract.verdict.fields {sorted(extra)} outside the harness "
            f"fold surface {sorted(allowed)} — the rendered glue only builds "
            "overall/failed_checks records")


def _test_evidence(ir: dict[str, Any]) -> list[tuple[str, list[str]]]:
    io = _dget(ir, "io_contract", {})
    out: list[tuple[str, list[str]]] = []
    for r in _dget(io, "test_evidence_requirements", []) or []:
        if not isinstance(r, dict):
            continue
        tid = str(r.get("test_id") or "").strip()
        if not tid:
            continue
        vs = [v.strip() for v in (r.get("required_raw_variables") or [])
              if isinstance(v, str) and v.strip()]
        out.append((tid, vs))
    if not out:
        raise RenderError("IR io_contract.test_evidence_requirements is empty")
    return out


def _target_class(ir: dict[str, Any]) -> str:
    impl = _dget(ir, "impl_defaults", {})
    target = _dget(impl, "target", {})
    cls = target.get("class") if isinstance(target, dict) else None
    return cls.strip() if isinstance(cls, str) and cls.strip() else "cpu"


def _threads(ir: dict[str, Any]) -> int:
    impl = _dget(ir, "impl_defaults", {})
    ov = _dget(impl, "backend_overrides", {})
    omp = _dget(ov, "openmp", {})
    n = omp.get("num_threads") if isinstance(omp, dict) else None
    try:
        return max(1, int(n))
    except (TypeError, ValueError):
        return 1


def _infra_dep_count(ir: dict[str, Any]) -> int:
    dep = _dget(ir, "dependency", {})
    count = 0
    for d in _dget(dep, "direct_deps", []) or []:
        nk = _dget(d, "node_key") if isinstance(d, dict) else (d if isinstance(d, str) else None)
        if isinstance(nk, str) and nk.split("/", 1)[0].strip() == "infrastructure":
            count += 1
    return count


# --- code assembly ------------------------------------------------------------


def _hname(harness_spec_id: str, sym: str) -> str:
    return f"{harness_spec_id}__{sym}"


def _flit(value: str) -> str:
    """Escape a string for embedding inside a single-quoted Fortran character literal.

    IR-sourced names (case_ids, snapshot variable names, metric addresses, test_ids)
    are only required to be non-empty by the compile gates, not to be Fortran
    identifiers, so a name containing a `'` would otherwise break the generated literal
    (`case ('a'b')`). Fortran escapes an embedded apostrophe by doubling it.

    Everything embedded must be **printable ASCII**. A control character (newline/tab)
    cannot appear in a literal at all. A non-ASCII character is worse than it looks: the
    Fortran default character kind counts BYTES, while every length this renderer reasons
    about — the ``CASE_ID_LEN`` bound, the 100-column lint limit — counts Python code
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


def _used_harness_ops(ir: dict[str, Any]) -> list[str]:
    """Unqualified harness op names the rendered glue calls (deterministic order):
    the core writers/plumbing plus only the emitters for the ranks in use."""
    ranks = _ranks_used(ir)
    ops = ["parse_cases"]
    if 0 in ranks:
        ops.append("emit_real")
    ops += [f"emit_array_r{r}" for r in sorted(r for r in ranks if r >= 1)]
    ops += ["box", "write_snapshot", "write_metrics_basis",
            "write_diagnostics", "write_perf"]
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
    for sym in (*_HARNESS_TYPES, *_HARNESS_CORE_OPS):
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
    future content fail-close (reserved-key collision, rank>4, verdict.fields, a control char in
    an IR name, an over-100-column rendered line, …) is caught here the instant the renderer
    rejects it.

    It EXCLUDES only ``RenderError``s flagged ``identity=True`` — the node-identity defects a
    re-author cannot repair (``_check_identifier_lengths``: spec_id/derived-name length; and >1
    infra dep, itself unreachable for an M3c node, which has exactly one infra dep by
    construction). Those belong to spec-input validation, NOT a compile.generate retry; they
    remain ``render_runner`` fail-closes as a backstop (see the module docstring)."""
    try:
        render_runner(ir, spec_id, harness_spec_id)
    except RenderError as exc:
        return [] if exc.identity else [str(exc)]
    except Exception as exc:  # noqa: BLE001
        # `render_runner`'s contract is "bad IR -> RenderError", but a truthy non-iterable IR
        # field (e.g. `verdict.fields: 5`, where `_dget(...) or []` yields `5` and `for f in 5`
        # raises TypeError) can still escape as a bare exception. Running INSIDE the compile
        # validator, an uncaught exception would abort `_validate_compile_stage_impl` and discard
        # every violation the sibling gates already collected, replacing the actionable list with
        # a renderer-internals traceback. Convert it to a violation instead: the IR is unrenderable
        # either way, so it routes to compile.generate with an intelligible message.
        return [f"IR is not renderable ({type(exc).__name__}: {exc})"]
    return []


def render_runner(ir: dict[str, Any], spec_id: str, harness_spec_id: str) -> str:
    """Render ``<spec_id>_runner.f90`` from the IR alone. Deterministic and pure.

    ``harness_spec_id`` is the certified plumbing module's spec_id
    (``harness_fortran_cpu``). See module docstring for the render-error matrix.
    The returned text is the complete Fortran source (trailing newline included).
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
    # The certified harness writes the per-case snapshot time under the FIXED key `t`
    # (harness controlled_spec §2/§3: `__write_snapshot(case_id, values, time)` takes a time
    # value, not a name). A physics IR that declares a different `time_variable` cannot be
    # honored by the harness — the emitted snapshot would carry `t` while the run contract
    # expects the declared name — so fail closed rather than silently render a mismatch.
    if time_var != "t":
        raise RenderError(
            f"snapshot time_variable is {time_var!r}, but the harness writes the snapshot time "
            "under the fixed key 't' (harness __write_snapshot takes a time value, not a name); "
            "declare time_variable: t for a harness-backed node")
    _verify_verdict_fields(ir)
    case_ids = _case_ids(ir)
    per_case = _per_case_vars(ir, schema_vars)
    xfail = _xfail_cases(ir)
    checks = _checks(ir)
    metrics = _metrics(ir)
    evidence = _test_evidence(ir)
    target_class = _target_class(ir)
    threads = _threads(ir)

    # ranks the schema declares, so we import only the emitters we call (an unused `use only`
    # name would trip lint). Every snapshot variable is captured for EVERY case (Z6): the
    # snapshot is the full declared state, not the subset a case's tests happen to require —
    # `per_case` above validates `required_raw_variables` ⊆ schema and nothing else here.
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
        H("write_metrics_basis"),
        H("write_diagnostics"),
        H("write_perf"),
    ]
    checks_syms = ["case_setup", "case_run", "get_time", "checks_compute"]
    if metrics:  # metric_compute is only called when the node declares metrics
        checks_syms.append("metric_compute")
    # The bound state: one `sb_<var> => <var>` rename per snapshot variable (schema declaration
    # order) — the same set the bundle gate requires bound, so a declared state the runner
    # never read cannot exist (a round-2 reviewer measured that gap when the read set was the
    # per-case union of `required_raw_variables`).
    bound_vars = list(schema_vars)
    for v in bound_vars:
        one_line = f"    {STATE_BINDING_PREFIX}{v} => {v}, &"
        if len(one_line) < MAX_RENDERED_LINE:
            checks_syms.append(f"{STATE_BINDING_PREFIX}{v} => {v}")
        else:  # a long name: continue between the alias and the target
            checks_syms.append(f"{STATE_BINDING_PREFIX}{v} => &\n      {v}")

    lines: list[str] = []
    a = lines.append

    a("! Deterministic runner glue authored host-side by the conductor (R1/M3c).")
    a("! It drives the physics node's <spec_id>_checks callbacks and emits the standard")
    a("! runner outputs THROUGH the certified harness_fortran_cpu plumbing, which owns all")
    a("! JSON assembly and the verdict fold (harness controlled_spec §3/§5.1). This glue")
    a("! holds no serialization knowledge; it builds harness records and calls the writers.")
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
    # The one dummy declaration this program's calls cannot make the compiler check, stated
    # where the producer leaf is told to read the ABI from (`METRIC_COMPUTE_DUMMIES`). Rendered
    # on every node, metrics or not: the stub a no-metrics node still has to define is where
    # the fixed-length form has been observed (issue #261).
    a("  ! The checks ABI is the same five subroutines for every node. metric_compute's")
    a(f"  ! `{METRIC_COMPUTE_DEFERRED_LENGTH_DUMMY}` dummy MUST be declared")
    a("  ! `character(len=:), allocatable, intent(out)`: this program passes an UNALLOCATED")
    a("  ! deferred-length allocatable for it, which a fixed-length or assumed-length dummy")
    a("  ! accepts at compile time and faults on at run time. A no-metrics stub included.")
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
    a("  logical :: ok, setup_ok, run_ok")
    a("  character(len=512), allocatable :: tokens(:)")
    a("  character(len=case_id_len), allocatable :: case_ids(:)")
    a("")
    a("  integer(int64) :: clock0, clock1, clock_rate")
    a("  real(dp) :: walltime, tval")
    a("  integer :: steps_total, cells_total, steps_c, cells_c")
    a("")
    a(f"  type({H('h_case_result')}), allocatable :: results(:)")
    a(f"  type({H('h_mb_entry')}), allocatable :: mb_entries(:)")
    a(f"  type({H('h_mb_entry')}), allocatable :: snap_cache(:)")
    a(f"  type({H('h_named')}), allocatable :: vals(:), sel(:)")
    a(f"  type({H('h_check')}), allocatable :: case_checks(:)")
    if metrics:  # case_metrics is only referenced under the per-case metric block
        a(f"  type({H('h_metric')}), allocatable :: case_metrics(:)")
    a("")
    a(f"  character(len={CHECK_STATUS_WIDTH}) :: cstatus")
    if metrics:
        a("  integer :: mcount, tci")
        a("  real(dp) :: mval")
        a("  logical :: mis_na, mfound")
        a("  character(len=:), allocatable :: mreason")
    else:
        a("  integer :: tci")
    a("")
    # ---- argv marshal + parse ----
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
    a("  allocate(results(ncases))")
    a("  allocate(snap_cache(ncases))")
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
    # (initial) and right after `case_run` (final), and `snap_cache` holds the serialized
    # STRINGS, so nothing a later `checks_compute` / `metric_compute` / `get_time` callback
    # writes into that storage can reach a snapshot or the metrics basis. There is no getter in
    # between: `get_time` — generated code — is called AFTER the capture, for the time value
    # the snapshot is written with, so no generated procedure runs between `case_setup` /
    # `case_run` returning and the state being serialized.
    a("    ! --- initial state: bound storage serialized right after case_setup, before any")
    a("    ! --- callback of this case runs (raw/state_snapshots/initial/<case_id>.json) ---")
    a("    call capture_state(trim(case_ids(ci)), vals)")
    a("    call get_time(tval)")
    a(f"    call {H('write_snapshot')}('initial/'//trim(case_ids(ci)), vals, tval)")
    a("    deallocate(vals)")
    a("")
    a("    call case_run(trim(case_ids(ci)), steps_c, cells_c, run_ok)")
    a("    steps_total = steps_total + steps_c")
    a("    cells_total = cells_total + cells_c")
    a("")
    a("    ! --- final state: the same bound storage right after case_run, before any check")
    a("    ! --- or metric callback of this case runs (raw/state_snapshots/<case_id>.json) ---")
    a("    call capture_state(trim(case_ids(ci)), vals)")
    a("    call get_time(tval)")
    a(f"    call {H('write_snapshot')}(trim(case_ids(ci)), vals, tval)")
    a("    snap_cache(ci)%case_id = trim(case_ids(ci))")
    a("    snap_cache(ci)%values = vals")
    a("    deallocate(vals)")
    a("")
    a("    ! --- honest per-case checks (runner-driven ids; xfail fold is the harness's) ---")
    a(f"    allocate(case_checks({len(checks)}))")
    for k, cid in enumerate(checks, start=1):
        clit = _flit(cid)
        # Per-id ABI (metric_compute's twin): the runner passes each IR-declared check id as a
        # literal `intent(in)` actual, so the module authors only the status — a dropped/renamed
        # id is structurally impossible. The width-binding line is the `case_checks(k)%id = '<id>'`
        # assignment (~90-91 cols for a 64-char id, 2-digit index); the wrapped `checks_compute`
        # call keeps its id on the continuation line (~82 cols). `_checks()` bounds the id at
        # CASE_ID_LEN (64) and a global >100-col backstop fail-closes any pathological escaped id.
        a("    call checks_compute(trim(case_ids(ci)), &")
        a(f"      '{clit}', cstatus)")
        a(f"    case_checks({k})%id = '{clit}'")
        a(f"    case_checks({k})%status = cstatus")
    a("")
    if metrics:
        a("    ! --- per-case metric leaves (dotted addresses; NA carried honestly) ---")
        a(f"    allocate(case_metrics({len(metrics)}))")
        a("    mcount = 0")
        for m in metrics:
            mlit = _flit(m)
            # Wrapped: a dotted metric address makes the single-line form exceed the 100-col
            # lint limit at ~23 chars, so the address sits on the header and the out-args wrap.
            a(f"    call metric_compute(trim(case_ids(ci)), '{mlit}', &")
            a("      mval, mis_na, mreason, mfound)")
            a("    if (mfound) then")
            a("      mcount = mcount + 1")
            a(f"      case_metrics(mcount)%name = '{mlit}'")
            a("      case_metrics(mcount)%value = mval")
            a("      case_metrics(mcount)%is_na = mis_na")
            a("      if (mis_na) then")
            a("        case_metrics(mcount)%reason_na = trim(mreason)")
            a("      else")
            a("        case_metrics(mcount)%reason_na = ''")
            a("      end if")
            a("    end if")
        a("")
    a("    results(ci)%case_id = trim(case_ids(ci))")
    a(f"    results(ci)%expected_xfail = {_xfail_expr(case_ids, xfail)}")
    a("    results(ci)%checks = case_checks")
    a("    deallocate(case_checks)")
    if metrics:
        a("    results(ci)%metrics = case_metrics(1:mcount)")
        a("    deallocate(case_metrics)")
    else:
        a("    allocate(results(ci)%metrics(0))")
    a("  end do")
    a("")
    a("  call system_clock(count=clock1)")
    a("  walltime = real(clock1 - clock0, dp) / real(clock_rate, dp)")
    a("  if (walltime <= 0.0_dp) walltime = 1.0e-9_dp")
    a("")
    # ---- metrics-basis: ONE entry per (test_id, target case_id) pair (R3-core).
    # A test's primary evidence is the evidence of EVERY case its predicate ranges over: a
    # single-target test contributes one row, a convergence sweep (nx = 32/64/128) three, a
    # base/shifted equivariance pair two. `(test_id, case_id)` is the entry's unique key, and
    # the post_execute completeness matrix (`_validate_metrics_basis_per_test`) pins the entry
    # set against exactly this product, so partial evidence cannot pass.
    #
    # Every target case's snapshot holds its test's `required_raw_variables` BY CONSTRUCTION:
    # the runner captures EVERY schema variable for every case, and `_per_case_vars` already
    # fail-closes when a required variable is absent from the schema. So each `pick` below
    # resolves — there is no additional precondition to check here.
    mb_rows: list[tuple[str, str, list[str]]] = []
    for tid, req_vars in evidence:
        tcases = _target_cases(ir, tid)
        if not tcases:
            raise RenderError(
                f"test {tid!r} in test_evidence_requirements has no target case in "
                "any test predicate (cannot resolve its metrics-basis source case)")
        for rv in req_vars:
            if rv not in schema_vars:
                raise RenderError(
                    f"test {tid!r} required_raw_variable {rv!r} is not a snapshot variable")
        for tcase in tcases:
            mb_rows.append((tid, tcase, req_vars))
    a("  ! --- metrics-basis entries: one per (test_id, target case_id) --------------")
    a(f"  allocate(mb_entries({len(mb_rows)}))")
    for k, (tid, tcase, req_vars) in enumerate(mb_rows, start=1):
        # Wrap the case-id-bearing lines so a long case_id cannot exceed the 100-col lint limit.
        a("  tci = find_case_index(case_ids, ncases, &")
        a(f"    '{_flit(tcase)}')")
        a("  if (tci < 1) then")
        a("    write(error_unit, '(A)') 'error: target case not run: ' // &")
        a(f"      '{_flit(tcase)}'")
        a("    error stop 1")
        a("  end if")
        a(f"  mb_entries({k})%test_id = '{_flit(tid)}'")
        a(f"  mb_entries({k})%case_id = &")
        a(f"    '{_flit(tcase)}'")
        a(f"  allocate(sel({len(req_vars)}))")
        for j, rv in enumerate(req_vars, start=1):
            a(f"  sel({j}) = pick(snap_cache(tci)%values, '{_flit(rv)}')")
        a(f"  mb_entries({k})%values = sel")
        a("  deallocate(sel)")
    a("")
    a("  ! --- emit the run outputs (harness owns every envelope + the fold) ----------")
    a(f"  call {H('write_metrics_basis')}(mb_entries, {len(mb_rows)})")
    a(f"  call {H('write_diagnostics')}(results, ncases)")
    a(f"  call {H('write_perf')}(trim(case_ids(ncases)), '{_flit(target_class)}', &")
    a(f"    steps_total, cells_total, walltime, 1, {threads}, 0)")
    a("")
    a("contains")
    a("")
    a("  ! Every declared snapshot variable, read from the checks module's bound storage")
    a("  ! (host-associated `sb_<var>`) and serialized by the harness emitters. The same")
    a("  ! set for every case: the snapshot is the full declared state.")
    a("  subroutine capture_state(cid, out)")
    a("    character(len=*), intent(in) :: cid")
    a(f"    type({H('h_named')}), allocatable, intent(out) :: out(:)")
    a(f"    allocate(out({len(bound_vars)}))")
    for k, v in enumerate(bound_vars, start=1):
        rank = _rank_of_shape(schema_vars[v], v)
        vlit = _flit(v)
        sb = f"{STATE_BINDING_PREFIX}{v}"
        if rank == 0:
            a(f"    out({k}) = {H('box')}('{vlit}', &")
            a(f"      {H('emit_real')}({sb}))")
        else:
            # An unallocated bound array is a binding the module never established for this
            # case (its `case_setup` did not allocate it): fail the run loudly rather than
            # pass an unallocated actual to the emitter (undefined behaviour).
            a(f"    call require_bound(allocated({sb}), &")
            a(f"      '{vlit}', cid)")
            a(f"    out({k}) = {H('box')}('{vlit}', &")
            a(f"      {H(f'emit_array_r{rank}')}({sb}))")
    a("  end subroutine capture_state")
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
    a("  ! Index of `target` in the parsed case list, or -1 when absent.")
    a("  function find_case_index(ids, n, target) result(idx)")
    a("    character(len=*), intent(in) :: ids(:)")
    a("    integer, intent(in) :: n")
    a("    character(len=*), intent(in) :: target")
    a("    integer :: idx, k")
    a("    idx = -1")
    a("    do k = 1, n")
    a("      if (trim(ids(k)) == target) then")
    a("        idx = k")
    a("        return")
    a("      end if")
    a("    end do")
    a("  end function find_case_index")
    a("")
    a("  ! The boxed value named `name` from a case's cached snapshot values.")
    a(f"  function pick(vals_in, name) result(nv)")
    a(f"    type({H('h_named')}), intent(in) :: vals_in(:)")
    a("    character(len=*), intent(in) :: name")
    a(f"    type({H('h_named')}) :: nv")
    a("    integer :: k")
    a("    do k = 1, size(vals_in)")
    a("      if (trim(vals_in(k)%name) == name) then")
    a("        nv = vals_in(k)")
    a("        return")
    a("      end if")
    a("    end do")
    a("    write(error_unit, '(A)') 'error: raw variable '//trim(name)//' absent from snapshot'")
    a("    error stop 1")
    a("  end function pick")
    a("")
    a(f"end program {spec_id}_runner")
    # Safety net: the runner is host-rendered (NOT in the leaf's allowed_output_paths), so a
    # line over the deterministic Generate.gate lint-checker column limit (fortitude S001, 100 cols) would
    # be an UNREPAIRABLE wedge (no leaf can edit a host file). The hot lines above are wrapped,
    # but an extreme IR-sourced name (a very long metric address / case_id / variable) could
    # still overflow a line we did not wrap — fail closed HERE (a clean RenderError → transport
    # fail_closed with an actionable message) rather than let it surface as a lint wedge.
    # A few `lines` entries embed their own `&` continuations (the multi-line `_xfail_expr`),
    # so measure per PHYSICAL line (split on embedded newlines) — measuring the joined entry
    # would false-fail a valid render whose wrapped physical lines are each within the limit.
    # `>=`, not `>`: on some supported linter builds S001 fires AT 100 columns (the same reason
    # `_checks` bounds the check-id assignment strictly under the limit), and a host-authored
    # line of exactly 100 columns is unrepairable by any leaf.
    for entry in lines:
        for ln in entry.split("\n"):
            if len(ln) >= MAX_RENDERED_LINE:
                raise RenderError(
                    f"rendered runner line reaches the {MAX_RENDERED_LINE}-column lint limit "
                    f"({len(ln)} columns; 99 is the widest that lints everywhere): "
                    f"{ln.strip()[:80]!r}… — an IR-sourced name (case_id / metric address / "
                    "variable) is too long for the lint column limit; shorten it")
    return "\n".join(lines) + "\n"


# --- checks ABI: the dummy declaration the compiler cannot check ----------------

_DECL_TYPE_RE = re.compile(
    r"^(?:integer|real|logical|complex|character|double\s*precision|type\s*\(|class\s*\()")
_UNIT_HEADER_RE = re.compile(r"^module\s+([a-z]\w*)$")
# A procedure definition header over a string-masked statement (the type-spec prefix of a
# function is greedy, exactly as the neutral ABI scan matches it).
_PROC_HEADER_RE = re.compile(
    r"^(?:(?:module|pure|impure|elemental|recursive|non_recursive)\s+)*"
    r"(?:(?:integer|real|double\s+precision|complex|logical|character|type|class)\b[^!]*\s+)?"
    r"(subroutine|function)\s+([a-z]\w*)\s*(?:\((.*?)\))?")


def _statements(text: str) -> list[str]:
    """Free-form source as one statement per entry: comments stripped, continuations joined,
    `;`-joined statements split, each stripped and lower-cased (Fortran is case-insensitive)."""
    return [stmt.strip().lower()
            for line in fortran_lines.fortran_logical_line_texts(text)
            for stmt in fortran_lines.split_fortran_statements(line)
            if stmt.strip()]


def _split_declaration(stmt: str) -> tuple[str, str] | None:
    """A type declaration statement as `(attribute_list, entity_list)`, or None when `stmt` is
    not one. With `::` the split is at the first `::` outside parentheses and literals; without
    it the language allows no attributes, so the entity list is what follows the type-spec
    (the keyword, then a `*len` / `(...)` selector with balanced parentheses)."""
    if not _DECL_TYPE_RE.match(stmt):
        return None
    depth = 0
    quote = ""
    for i, c in enumerate(stmt):
        if quote:
            if c == quote:
                quote = ""
        elif c in "'\"":
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == ":" and depth == 0 and stmt.startswith("::", i):
            return stmt[:i], stmt[i + 2:]
    m = re.match(r"^(?:double\s*precision|integer|real|logical|complex|character|type|class)",
                 stmt)
    i = m.end()
    n = len(stmt)
    while i < n:
        c = stmt[i]
        if c in " \t":
            i += 1
        elif c == "*":
            i += 1
            while i < n and (stmt[i] in " \t" or stmt[i].isdigit()):
                i += 1
        elif c == "(":
            depth = 0
            while i < n:
                if stmt[i] == "(":
                    depth += 1
                elif stmt[i] == ")":
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                i += 1
        else:
            break
    return "", stmt[i:]


def _entity_names(entity_list: str) -> set[str]:
    """The names an entity list declares: the leading identifier of each top-level
    comma-separated item (`x`, `x(:)`, `x*8`, `x = 0`)."""
    names: set[str] = set()
    for item in fortran_lines.split_top_level_commas(entity_list):
        m = re.match(r"^\s*([a-z]\w*)", item)
        if m:
            names.add(m.group(1))
    return names


def _declared_allocatable(spec_part: list[str], name: str) -> bool | None:
    """Whether `name` carries the `allocatable` attribute over the statements of one
    specification part — in its type declaration's attribute list or in a separate
    `allocatable [::] <names>` attribute statement; None when no type declaration statement
    declares `name` at all."""
    declared = False
    allocatable = False
    for stmt in spec_part:
        am = re.match(r"^allocatable\b\s*(?:::)?(.*)$", stmt)
        if am:
            if name in _entity_names(am.group(1)):
                allocatable = True
            continue
        split = _split_declaration(stmt)
        if split is None or name not in _entity_names(split[1]):
            continue
        declared = True
        if any(a.strip() == "allocatable"
               for a in fortran_lines.split_top_level_commas(split[0])):
            allocatable = True
    return allocatable if declared else None


def checks_abi_dummy_violation(text: str, spec_id: str) -> str | None:
    """The one dummy-declaration constraint on `<spec_id>_checks` the compiler cannot state, or
    None: a `metric_compute` DEFINED at module level inside `module <spec_id>_checks` must
    declare `METRIC_COMPUTE_DEFERRED_LENGTH_DUMMY` (found by POSITION in its dummy list, so a
    renamed dummy is still the one the runner's unallocated actual reaches) with the
    `allocatable` attribute — see `METRIC_COMPUTE_DUMMIES` for why that attribute alone.

    Positive evidence only, like `checks_module_abi_facts`: a module that publishes the name
    without defining it here is not judged (the syntax gate resolves that `use`), and a
    definition inside an `interface` block, an internal procedure, or another module is not
    the runner's callee. The required set is the FULL fixed ABI, so this runs whether or not
    the node's runner imports `metric_compute` (a node with no metrics stubs it and the runner
    never calls it — the declaration is still the pinned one, and the check is uniform rather
    than conditioned on the IR).

    Read over statements (`fortran_lines`), never lines, with the same tolerance for spelling as
    the ABI scan: a declaration with `::` or without, the attribute inline or in a separate
    `allocatable ::` statement, `end subroutine` / `endsubroutine` / bare `end`. The
    specification part is read up to the first `contains` or the procedure's own end. A
    `metric_compute` with fewer dummies than the position is a violation here (fail-closed);
    the syntax gate would refuse the call too. The two fail-open shapes a walk like this can
    take are both closed on the safe side: a bare `end` is never read as closing the target
    module (a later `metric_compute` would otherwise be skipped), and a body whose end is not
    found is judged over what was read."""
    target = f"{spec_id}_checks".lower()
    position = METRIC_COMPUTE_DUMMIES.index(METRIC_COMPUTE_DEFERRED_LENGTH_DUMMY)
    stmts = _statements(text)
    in_target = False
    in_interface = False
    proc_depth = 0
    for i, s in enumerate(stmts):
        um = _UNIT_HEADER_RE.match(s)
        if um:
            in_target = um.group(1) == target
            proc_depth, in_interface = 0, False
            continue
        if re.match(r"^end\s*module\b", s):
            in_target = False
            proc_depth, in_interface = 0, False
            continue
        if not in_interface and re.match(r"^(?:abstract\s+)?interface\b", s):
            in_interface = True
            continue
        if in_interface:
            if re.match(r"^end\s*interface\b", s):
                in_interface = False
            continue
        if re.match(r"^end\s*(?:subroutine|function|procedure)\b", s) or s == "end":
            proc_depth = max(proc_depth - 1, 0)
            continue
        hm = _PROC_HEADER_RE.match(fortran_lines.mask_code_lookalikes(s))
        if hm is None:
            continue
        proc_depth += 1
        if not (in_target and proc_depth == 1
                and hm.group(1) == "subroutine" and hm.group(2) == "metric_compute"):
            continue
        # The header is matched on the masked text; the dummy list is read from the original.
        dummies = [d.strip() for d in fortran_lines.split_top_level_commas(
            s[hm.start(3):hm.end(3)] if hm.group(3) is not None else "") if d.strip()]
        pinned = ", ".join(METRIC_COMPUTE_DUMMIES)
        if len(dummies) <= position:
            return (f"metric_compute declares {len(dummies)} dummy argument(s) where the "
                    f"runner's call passes {len(METRIC_COMPUTE_DUMMIES)}: declare it as "
                    f"metric_compute({pinned})")
        name = dummies[position]
        spec_part: list[str] = []
        for s2 in stmts[i + 1:]:
            if (re.match(r"^end\s*(?:subroutine|function|procedure)?\b", s2) or s2 == "end"
                    or s2 == "contains"
                    or _PROC_HEADER_RE.match(fortran_lines.mask_code_lookalikes(s2))):
                break
            spec_part.append(s2)
        verdict = _declared_allocatable(spec_part, name)
        if verdict is None:
            return (f"metric_compute's dummy argument {name!r} (position {position + 1}, the "
                    f"one the host-rendered runner passes its unallocated deferred-length "
                    f"actual for) has no type declaration statement in metric_compute; declare "
                    f"it exactly as the checks-module contract pins it: "
                    f"`character(len=:), allocatable, intent(out) :: {name}`")
        if not verdict:
            return (f"metric_compute's dummy argument {name!r} (position {position + 1}) is "
                    f"declared without the `allocatable` attribute. The host-rendered runner "
                    f"passes an UNALLOCATED `character(len=:), allocatable` actual for it, "
                    f"which a non-allocatable dummy accepts at compile time (no diagnostic from "
                    f"the syntax check or the build) and faults on at run time (the first "
                    f"assignment to it writes through a null descriptor). Declare it exactly as "
                    f"the checks-module contract pins it: `character(len=:), allocatable, "
                    f"intent(out) :: {name}` — an assignment `{name} = '<short reason>'` "
                    f"then allocates it")
        return None
    return None


def _xfail_expr(case_ids: list[str], xfail: set[str]) -> str:
    """Fortran boolean expression selecting the xfail cases at runtime.

    Kept inline (per-case, in the loop) rather than a table so the runner needs
    no extra module state; short-circuits to ``.false.`` when there are none."""
    xf = [c for c in case_ids if c in xfail]
    if not xf:
        return ".false."
    terms = " .or. &\n      ".join(f"trim(case_ids(ci)) == '{_flit(c)}'" for c in xf)
    return terms


# --- harness interface signature pin (fail-closed, run before rendering) ------
#
# The only harness this renderer targets is harness_fortran_cpu (the R1 (fortran,
# cpu) plumbing). The template is written against its v3 §5.1 signatures, embedded
# below verbatim. `assert_harness_pin` checks that the *certified* harness the
# consumer will build against still publishes those exact signatures — in both its
# IR (`public_api.signatures`) and its generated model source — so a harness recert
# that silently changed the interface fails the consumer's render (drift is caught
# at the consumer, not miscompiled at Build).

EXPECTED_HARNESS_SPEC_ID = "harness_fortran_cpu"

# Verbatim copy of the harness controlled_spec §5.1 canonical interface block (v3, harness
# spec_version 0.3.0: `h_mb_entry` gained the `case_id` component so metrics-basis evidence is
# keyed by (test_id, case_id) and a multi-target test records every targeted case — the pin
# compares signatures, not versions). If a harness recert changes §5.1, THIS block and the
# render template must be updated together (the pin message says so).
_HARNESS_V3_INTERFACE = """\
type :: harness_fortran_cpu__h_named
  character(len=:), allocatable :: name
  character(len=:), allocatable :: json
end type harness_fortran_cpu__h_named

type :: harness_fortran_cpu__h_check
  character(len=:), allocatable :: id
  character(len=4) :: status
end type harness_fortran_cpu__h_check

type :: harness_fortran_cpu__h_metric
  character(len=:), allocatable :: name
  real(dp) :: value
  logical :: is_na
  character(len=:), allocatable :: reason_na
end type harness_fortran_cpu__h_metric

type :: harness_fortran_cpu__h_case_result
  character(len=:), allocatable :: case_id
  logical :: expected_xfail
  type(harness_fortran_cpu__h_check), allocatable :: checks(:)
  type(harness_fortran_cpu__h_metric), allocatable :: metrics(:)
end type harness_fortran_cpu__h_case_result

type :: harness_fortran_cpu__h_mb_entry
  character(len=:), allocatable :: test_id
  character(len=:), allocatable :: case_id
  type(harness_fortran_cpu__h_named), allocatable :: values(:)
end type harness_fortran_cpu__h_mb_entry

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

subroutine harness_fortran_cpu__write_metrics_basis(entries, n)
  type(harness_fortran_cpu__h_mb_entry), intent(in) :: entries(:)
  integer, intent(in) :: n
end subroutine harness_fortran_cpu__write_metrics_basis

subroutine harness_fortran_cpu__write_diagnostics(results, n)
  type(harness_fortran_cpu__h_case_result), intent(in) :: results(:)
  integer, intent(in) :: n
end subroutine harness_fortran_cpu__write_diagnostics

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

_PIN_DRIFT_HINT = (
    "the certified harness interface no longer matches the renderer's pinned "
    "expectation — a harness recert changed its published surface; update the "
    "renderer pin (_HARNESS_V3_INTERFACE / _HARNESS_V3_PARAMETERS) AND the "
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
    from tools.backends.language.fortran.signatures import (
        parse_interface_stanzas, source_atoms, stanza_atoms, stanza_line_set, stanza_line_list)

    if (harness_spec_id or "").strip() != EXPECTED_HARNESS_SPEC_ID:
        raise RenderError(
            f"harness_spec_id {harness_spec_id!r} is not the pinned "
            f"{EXPECTED_HARNESS_SPEC_ID!r}; the renderer only targets that harness")

    exp_ops, exp_types, exp_errs = parse_interface_stanzas(_HARNESS_V3_INTERFACE)
    if exp_errs:  # a renderer bug, not an input problem
        raise RenderError(f"embedded harness interface failed to parse: {exp_errs}")

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

    used_symbols = [_hname(harness_spec_id, op) for op in _used_harness_ops(ir)]
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

    src_ops, src_types, _src_errs = parse_interface_stanzas(harness_source or "")

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
        ir_ops, ir_types, _ = parse_interface_stanzas(ir_text)
        ir_stanza = ir_ops.get(symbol) or ir_types.get(symbol)
        ir_ok = ir_stanza is not None and (
            stanza_line_list(ir_stanza) == stanza_line_list(exp_stanza) if is_type
            else stanza_line_set(ir_stanza) == stanza_line_set(exp_stanza))
        if not ir_ok:
            raise RenderError(
                f"certified harness IR signature for {symbol!r} differs from the pinned "
                f"interface: {_PIN_DRIFT_HINT}")

        # (2) Generated model source — a procedure stanza carries its body, so the pinned
        # interface atoms must be a SUBSET of the source stanza's atoms (a type block has no
        # body, so it is compared exactly, matching _validate_generated_signatures).
        src_stanza = src_ops.get(symbol) or src_types.get(symbol)
        if src_stanza is None:
            raise RenderError(
                f"certified harness model source omits {symbol!r}: {_PIN_DRIFT_HINT}")
        if is_type:
            src_ok = stanza_line_list(src_stanza) == stanza_line_list(exp_stanza)
        else:
            have = frozenset(stanza_atoms(src_stanza))
            src_ok = stanza_line_set(exp_stanza).issubset(have)
        if not src_ok:
            raise RenderError(
                f"certified harness model source signature for {symbol!r} differs from the "
                f"pinned interface: {_PIN_DRIFT_HINT}")
