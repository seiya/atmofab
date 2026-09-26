"""What a host-rendered runner reads from a physics node's IR — the language-neutral half.

A physics node's runner is rendered by the language backend that declares `runner_render`
(`tools/host_render.py`), and every such renderer asks the IR the same questions: which snapshot
variables to capture and at what rank, which cases to run and which of them are expected to fail,
which check ids and metric addresses to drive, and which `(test_id, case_id)` rows the metrics
basis carries. Those answers — and the refusals of an IR that cannot answer them — are facts about
the IR and the harness's published surface, not about a language, so they are stated here once and
every renderer asks them (issue #289, R4-b PR-6). A renderer adds the refusals its own language
imposes (an identifier grammar, a line width) in the hooks the readers take.

Sharing them is what makes the compile gate's promise hold across targets: every target whose
language renders a runner runs its `ir_content_violations` at Compile
(`validate_pipeline_semantics._compile_render_targets`), so a refusal one renderer makes of a
neutral fact and another does not would fail one target's IR for a reason the other target's
renderer shows is not a defect.

Every refusal is a `host_render.RenderError`, the seam's class, so a renderer raises it through
unchanged and `ir_content_violations` reports it.

Pure; imports only the seam's error class and the neutral case-id token grammar.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tools.host_render import RenderError
from tools.spec_input_gates import CASE_ID_TOKEN_RE

#: Harness-owned snapshot keys a physics snapshot variable must not shadow.
HARNESS_RESERVED_SNAPSHOT_KEYS = frozenset({"t", "case_id", "step"})

#: The harness's `case_id_len` (§5.1 of every harness this repository certifies): the width a
#: parsed case id is stored at, and the bound on a declared case id and a check id.
CASE_ID_LEN = 64


def dget(node: Any, key: str, default: Any = None) -> Any:
    """`node[key]` when `node` is a mapping, else `default` — the readers tolerate a missing or
    mistyped IR node and refuse on the value they then fail to find."""
    return node.get(key, default) if isinstance(node, dict) else default


def rank_of_shape(shape_expr: Any, var: str) -> int:
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


def snapshot_schema(
    ir: dict[str, Any], check_name: Callable[[str], None] | None = None,
) -> tuple[dict[str, str], str]:
    """Return ``({var_name: shape_expr}, time_variable)`` from
    ``io_contract.raw_requirements.required_evidence[state_snapshots].schema``.

    Each variable name is refused when it collides with a harness-reserved key, then handed to
    `check_name` — the renderer's own rules for a name its language binds (an identifier grammar,
    a collision with the checks ABI) — in declaration order, one name at a time, so the first
    defective name is the one reported whichever rule it breaks."""
    io = dget(ir, "io_contract", {})
    rr = dget(io, "raw_requirements", {})
    entry = None
    for e in dget(rr, "required_evidence", []) or []:
        if isinstance(e, dict) and e.get("artifact") == "state_snapshots":
            entry = e
            break
    if entry is None:
        raise RenderError(
            "IR io_contract has no state_snapshots required_evidence entry "
            "(a rendered runner needs the snapshot schema to emit per-case state)")
    schema = dget(entry, "schema", {})
    variables: dict[str, str] = {}
    for v in dget(schema, "variables", []) or []:
        if isinstance(v, dict) and isinstance(v.get("name"), str) and v["name"].strip():
            variables[v["name"].strip()] = v.get("shape_expr")
    if not variables:
        raise RenderError("state_snapshots schema declares no variables")
    time_var = schema.get("time_variable")
    time_var = time_var.strip() if isinstance(time_var, str) and time_var.strip() else "t"
    for name in variables:
        if name in HARNESS_RESERVED_SNAPSHOT_KEYS:
            raise RenderError(
                f"snapshot variable {name!r} collides with a harness-reserved key "
                f"{sorted(HARNESS_RESERVED_SNAPSHOT_KEYS)}")
        if check_name is not None:
            check_name(name)
    return variables, time_var


def require_time_variable_t(time_var: str) -> None:
    """The certified harness writes the per-case snapshot time under the FIXED key `t`
    (harness controlled_spec §2/§3: `__write_snapshot(case_id, values, time)` takes a time
    value, not a name). A physics IR that declares a different `time_variable` cannot be
    honored by the harness — the emitted snapshot would carry `t` while the run contract
    expects the declared name — so fail closed rather than silently render a mismatch."""
    if time_var != "t":
        raise RenderError(
            f"snapshot time_variable is {time_var!r}, but the harness writes the snapshot time "
            "under the fixed key 't' (harness __write_snapshot takes a time value, not a name); "
            "declare time_variable: t for a harness-backed node")


def case_ids(ir: dict[str, Any]) -> list[str]:
    """The declared case ids, in declaration order, refused when empty, unsafe, duplicated or
    longer than the harness's `case_id_len`."""
    case = dget(ir, "case", {})
    out: list[str] = []
    for c in dget(case, "test_case_set", []) or []:
        cid = dget(c, "case_id")
        if isinstance(cid, str) and cid.strip():
            out.append(cid.strip())
    if not out:
        raise RenderError("IR case.test_case_set is empty (no cases to run)")
    # A case_id is concatenated straight into the per-case snapshot PATH by the harness
    # (`raw/state_snapshots/<case_id>.json`), so an id containing `/` or `..` traverses out of
    # the run directory and the (cleanly compiling) runner writes an arbitrary file. A renderer's
    # literal escaping only bars non-printable-ASCII and the length gate only bounds size, so
    # both let `../evil` through. Restrict the id to a filesystem-safe token.
    unsafe = sorted({c for c in out if not CASE_ID_TOKEN_RE.match(c) or ".." in c})
    if unsafe:
        raise RenderError(
            f"case_id(s) {unsafe} are not safe tokens; a case_id is concatenated into the "
            "per-case snapshot path (raw/state_snapshots/<case_id>.json) and reaches the "
            "runner's argv, so it must match [A-Za-z0-9._][A-Za-z0-9._-]* with no '..' "
            "(a leading '-' would be read as an option; anything else escapes the run "
            "directory at runtime)")
    # Duplicate case_ids would render two identical case labels in the runner, a hard compile
    # error the leaf cannot repair (host-rendered runner). Fail closed rather than emit a
    # non-compiling, unrepairable runner.
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


def test_predicates(ir: dict[str, Any]) -> list[dict[str, Any]]:
    io = dget(ir, "io_contract", {})
    return [p for p in (dget(io, "test_predicates", []) or []) if isinstance(p, dict)]


def xfail_cases(ir: dict[str, Any]) -> set[str]:
    """Case ids whose failure is expected (targeted by an ``xfail`` predicate)."""
    xfail: set[str] = set()
    for p in test_predicates(ir):
        if str(p.get("expected_outcome") or "").strip().lower() == "xfail":
            for tc in p.get("target_cases") or []:
                if isinstance(tc, str) and tc.strip():
                    xfail.add(tc.strip())
    return xfail


def target_cases(ir: dict[str, Any], test_id: str) -> list[str]:
    """All distinct case ids targeted by the predicate(s) for ``test_id``, in
    declaration order. This is the metrics-basis row set of that test: the runner
    records one ``h_mb_entry`` per ``(test_id, case_id)`` pair, and the post_execute
    completeness matrix (``_validate_metrics_basis_per_test``) is anchored on the
    same field."""
    seen: list[str] = []
    for p in test_predicates(ir):
        if str(p.get("test_id") or "").strip() == test_id:
            for tc in p.get("target_cases") or []:
                if isinstance(tc, str) and tc.strip() and tc.strip() not in seen:
                    seen.append(tc.strip())
    return seen


def per_case_vars(ir: dict[str, Any], schema_vars: dict[str, str]) -> dict[str, list[str]]:
    """Map each case_id to the union of ``required_raw_variables`` over the tests targeting
    that case, ordered by the snapshot schema declaration order. Since Z6 this is a
    VALIDATION (each entry must be a schema variable) and the metrics-basis pick set — the
    runner captures every schema variable for every case, not this per-case subset."""
    io = dget(ir, "io_contract", {})
    req_by_test: dict[str, list[str]] = {}
    for r in dget(io, "test_evidence_requirements", []) or []:
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
    for p in test_predicates(ir):
        tid = str(p.get("test_id") or "").strip()
        needed = set(req_by_test.get(tid, []))
        for tc in p.get("target_cases") or []:
            if isinstance(tc, str) and tc.strip():
                per_case.setdefault(tc.strip(), set()).update(needed)
    out: dict[str, list[str]] = {}
    for cid in case_ids(ir):
        want = per_case.get(cid, set())
        missing = [v for v in want if v not in schema_vars]
        if missing:
            raise RenderError(
                f"case {cid!r} requires raw variables {sorted(missing)} absent from the "
                "state_snapshots schema (required_raw_variables must be snapshot variables)")
        out[cid] = [v for v in schema_order if v in want]
    return out


def check_ids(ir: dict[str, Any]) -> list[str]:
    """The declared `diagnostics_contract.checks[].id`s, in order; refused when none is declared.
    A renderer whose language bounds the rendered line bounds the id itself."""
    io = dget(ir, "io_contract", {})
    dc = dget(io, "diagnostics_contract", {})
    ids: list[str] = []
    for c in dget(dc, "checks", []) or []:
        cid = dget(c, "id")
        if isinstance(cid, str) and cid.strip():
            ids.append(cid.strip())
    if not ids:
        raise RenderError("IR diagnostics_contract declares no checks")
    return ids


def metrics(ir: dict[str, Any]) -> list[str]:
    """Dotted metric addresses from ``diagnostics_contract.metrics`` (may be empty)."""
    io = dget(ir, "io_contract", {})
    dc = dget(io, "diagnostics_contract", {})
    out: list[str] = []
    for m in dget(dc, "metrics", []) or []:
        if isinstance(m, str) and m.strip():
            out.append(m.strip())
        elif isinstance(m, dict):
            addr = m.get("address") or m.get("name") or m.get("id")
            if isinstance(addr, str) and addr.strip():
                out.append(addr.strip())
    return out


def verify_verdict_fields(ir: dict[str, Any]) -> None:
    io = dget(ir, "io_contract", {})
    dc = dget(io, "diagnostics_contract", {})
    verdict = dget(dc, "verdict", {})
    fields = dget(verdict, "fields", []) or []
    allowed = {"overall", "failed_checks"}
    extra = {str(f).strip() for f in fields} - allowed
    if extra:
        raise RenderError(
            f"diagnostics_contract.verdict.fields {sorted(extra)} outside the harness "
            f"fold surface {sorted(allowed)} — the rendered glue only builds "
            "overall/failed_checks records")


def test_evidence(ir: dict[str, Any]) -> list[tuple[str, list[str]]]:
    io = dget(ir, "io_contract", {})
    out: list[tuple[str, list[str]]] = []
    for r in dget(io, "test_evidence_requirements", []) or []:
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


def metrics_basis_rows(
    ir: dict[str, Any], evidence: list[tuple[str, list[str]]], schema_vars: dict[str, str],
) -> list[tuple[str, str, list[str]]]:
    """ONE metrics-basis entry per (test_id, target case_id) pair (R3-core), as
    `(test_id, case_id, required_raw_variables)`.

    A test's primary evidence is the evidence of EVERY case its predicate ranges over: a
    single-target test contributes one row, a convergence sweep (nx = 32/64/128) three, a
    base/shifted equivariance pair two. `(test_id, case_id)` is the entry's unique key, and
    the post_execute completeness matrix (`_validate_metrics_basis_per_test`) pins the entry
    set against exactly this product, so partial evidence cannot pass.

    Every target case's snapshot holds its test's `required_raw_variables` BY CONSTRUCTION:
    the runner captures EVERY schema variable for every case, and `per_case_vars` already
    fail-closes when a required variable is absent from the schema."""
    rows: list[tuple[str, str, list[str]]] = []
    for tid, req_vars in evidence:
        tcases = target_cases(ir, tid)
        if not tcases:
            raise RenderError(
                f"test {tid!r} in test_evidence_requirements has no target case in "
                "any test predicate (cannot resolve its metrics-basis source case)")
        for rv in req_vars:
            if rv not in schema_vars:
                raise RenderError(
                    f"test {tid!r} required_raw_variable {rv!r} is not a snapshot variable")
        for tcase in tcases:
            rows.append((tid, tcase, req_vars))
    return rows


def target_class(target: dict[str, Any]) -> str:
    """The hardware class the run executes on, off the target profile document (issue #284)."""
    return str(target["hardware"]["class"])


def infra_dep_count(ir: dict[str, Any]) -> int:
    dep = dget(ir, "dependency", {})
    count = 0
    for d in dget(dep, "direct_deps", []) or []:
        nk = dget(d, "node_key") if isinstance(d, dict) else (d if isinstance(d, str) else None)
        if isinstance(nk, str) and nk.split("/", 1)[0].strip() == "infrastructure":
            count += 1
    return count
