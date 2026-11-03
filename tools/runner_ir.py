"""What a host-rendered runner reads from a physics node's IR — the language-neutral half.

A physics node's runner is rendered by the language backend that declares `runner_render`
(`tools/host_render.py`), and every such renderer asks the IR the same questions: which snapshot
variables to capture and at what rank, and which cases to run. Those answers — and the refusals of an IR that cannot answer them — are facts about
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
#: parsed case id is stored at, and the bound on a declared case id.
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
            "select-case label could never match it at runtime. "
            f"Shorten the case_id to ≤{CASE_ID_LEN} chars")
    return out


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


# ------------------------------------------------------------------------------------- drive
#
# The `drive` block (issue #589, PR-1a): what the host-rendered driver calls per case, on what,
# and how often. These readers are what the PR-1b renderers will call; no renderer calls them
# yet. They are stated HERE, not imported from `tools/primary_evidence.py` (which models the
# same block for the verdict and the case-file writer): this module is digested into the render
# tuple and that one into the verdict tuple, and an import across would let a render-time
# behaviour change ride on a digest that does not cover it. The two readings are held equal by
# `tools/tests/test_runner_ir.py` (`DriveAgreementTest`).

_DRIVE_LOOP_KEYS = ("n_step", "dt", "t_start", "t_end")
_IDENT_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


def _ident(text: str) -> bool:
    return bool(text) and not text[0].isdigit() and set(text) <= _IDENT_CHARS


def drive_binding(value: Any) -> tuple[str, Any]:
    """One `arguments` value as ``(kind, ref)``: ``("inputs", "<dotted path>")``,
    ``("derived", name)``, ``("state", var)``, ``("time", None)``, ``("step", None)``,
    ``("literal", value)`` or ``("procedure", "<expression>")``. RenderError otherwise."""
    if isinstance(value, bool) or (isinstance(value, (int, float)) and value == value
                                   and value not in (float("inf"), float("-inf"))):
        return ("literal", value)
    if isinstance(value, dict):
        text = value.get("procedure")
        if set(value) == {"procedure"} and isinstance(text, str) and text.strip():
            return ("procedure", text.strip())
    if isinstance(value, str):
        text = value.strip()
        if text in ("time", "step"):
            return (text, None)
        root, _, rest = text.partition(".")
        parts = rest.split(".") if rest else []
        if root == "inputs" and parts and all(_ident(p) for p in parts):
            return ("inputs", rest)
        if root in ("derived", "state") and len(parts) == 1 and _ident(parts[0]):
            return (root, rest)
    raise RenderError(f"drive binding {value!r} is not inputs.<path>, derived.<name>, "
                      "state.<var>, time, step, a number, a bool or {procedure: <expression>}")


def drive_block(ir: dict[str, Any]) -> dict[str, Any]:
    """The IR's `drive` mapping; RenderError when absent or not a mapping."""
    drive = dget(ir, "drive")
    if not isinstance(drive, dict):
        raise RenderError("IR has no drive block (the operation the driver calls per case)")
    unknown = sorted(set(drive) - {"init", "step", "loop", "history"}, key=str)
    if unknown:
        raise RenderError(f"drive carries unknown key(s) {unknown}")
    return drive


def _signature_args(ir: dict[str, Any], op: str) -> list[dict[str, Any]]:
    for entry in dget(dget(ir, "public_api", {}), "signatures", []) or []:
        if dget(entry, "symbol") == op:
            args = dget(dget(entry, "signature", {}), "args")
            if isinstance(args, list) and all(isinstance(a, dict)
                                              and isinstance(a.get("name"), str) for a in args):
                return args
    raise RenderError(f"drive operation {op!r} has no public_api.signatures entry with its args")


def _drive_call(ir: dict[str, Any], raw: Any, where: str, list_form: bool) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RenderError(f"{where} must be a mapping {{operation, result, arguments}}")
    allowed = {"operation", "result", "arguments"} | ({"cases"} if list_form else set())
    unknown = sorted(set(raw) - allowed, key=str)
    if unknown:
        raise RenderError(f"{where} carries unknown key(s) {unknown}")
    op, result, arguments = raw.get("operation"), raw.get("result"), raw.get("arguments")
    if not isinstance(op, str) or not op.strip():
        raise RenderError(f"{where}.operation must name a published operation")
    if not isinstance(result, str) or not result.strip():
        raise RenderError(f"{where}.result must name the operation's logical out dummy")
    if not isinstance(arguments, dict):
        raise RenderError(f"{where}.arguments must be a mapping of dummy -> binding")
    args = _signature_args(ir, op.strip())
    names = [a["name"] for a in args]
    if set(arguments) != set(names):
        raise RenderError(f"{where}.arguments binds {sorted(map(str, arguments))}; the dummies "
                          f"of {op.strip()} are {names}")
    call = {"operation": op.strip(), "result": result.strip(), "dummies": args,
            "bindings": {n: drive_binding(arguments[n]) for n in names}, "cases": None}
    if list_form:
        cases = raw.get("cases")
        if not isinstance(cases, list) or not cases \
                or not all(isinstance(c, str) and c.strip() for c in cases):
            raise RenderError(f"{where}.cases must be a non-empty list of case_ids")
        call["cases"] = [c.strip() for c in cases]
    return call


def drive_init(ir: dict[str, Any]) -> dict[str, Any] | None:
    """`drive.init` as ``{operation, result, dummies, bindings, cases: None}``, or None."""
    raw = drive_block(ir).get("init")
    if raw is None:
        return None
    if isinstance(raw, list):
        raise RenderError("drive.init is one mapping entry, never a list")
    return _drive_call(ir, raw, "drive.init", False)


def drive_steps(ir: dict[str, Any]) -> list[dict[str, Any]]:
    """The `drive.step` entries, each ``{operation, result, dummies, bindings, cases}``:
    one entry with ``cases`` None in the mapping form, else the list with each entry's cases."""
    raw = drive_block(ir).get("step")
    if isinstance(raw, list):
        if not raw:
            raise RenderError("drive.step is an empty list")
        return [_drive_call(ir, r, f"drive.step[{i}]", True) for i, r in enumerate(raw)]
    if raw is None:
        raise RenderError("drive.step is required")
    return [_drive_call(ir, raw, "drive.step", False)]


def drive_loop(ir: dict[str, Any]) -> dict[str, tuple[str, Any]] | None:
    """`drive.loop` as key -> binding over `n_step`, `dt`, `t_start`, `t_end` (each an
    inputs / derived / state reference), or None (one call per case)."""
    raw = drive_block(ir).get("loop")
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != set(_DRIVE_LOOP_KEYS):
        raise RenderError(f"drive.loop must carry exactly {list(_DRIVE_LOOP_KEYS)}")
    out = {k: drive_binding(raw[k]) for k in _DRIVE_LOOP_KEYS}
    for key, (kind, _ref) in out.items():
        if kind not in ("inputs", "derived", "state"):
            raise RenderError(f"drive.loop.{key} must be inputs.<path>, derived.<name> or "
                              "state.<var>")
    return out


def drive_history_every(ir: dict[str, Any]) -> str | None:
    """The `inputs` path `drive.history.every` names, or None when no history is declared."""
    raw = drive_block(ir).get("history")
    if raw is None:
        return None
    if isinstance(raw, dict) and set(raw) == {"every"}:
        kind, ref = drive_binding(raw["every"])
        if kind == "inputs":
            return ref
    raise RenderError("drive.history must be {every: inputs.<path>}")


def drive_state_classes(ir: dict[str, Any]) -> dict[str, frozenset[str]]:
    """The classes of the state variables (issue #589 SHAPE §3), from `drive` + signatures:
    ``init_written`` (an `out` of init), ``written`` (an `out` / `inout` of any entry),
    ``read`` (an `in` / `inout` of a step entry), ``logical`` (a logical rank-0 `out`),
    ``result_states`` (an entry's `result:` dummy, or any logical `out`) and
    ``operation_only`` (written by step, read by no step dummy, not init-written, not
    logical)."""
    init, steps = drive_init(ir), drive_steps(ir)
    calls = ([init] if init is not None else []) + steps
    roles = ["init"] * (len(calls) - len(steps)) + ["step"] * len(steps)
    classes: dict[str, set[str]] = {k: set() for k in (
        "init_written", "written", "read", "logical", "result_states", "step_written")}
    for role, call in zip(roles, calls):
        for dummy in call["dummies"]:
            kind, var = call["bindings"][dummy["name"]]
            if kind != "state":
                continue
            intent = dummy.get("intent")
            spec = dummy.get("spec") if isinstance(dummy.get("spec"), dict) else {}
            rank = dummy.get("rank", len(dummy.get("dims") or []))
            if role == "init" and intent == "out":
                classes["init_written"].add(var)
            if intent in ("out", "inout"):
                classes["written"].add(var)
                if role == "step":
                    classes["step_written"].add(var)
            if role == "step" and intent in ("in", "inout"):
                classes["read"].add(var)
            if intent == "out" and spec.get("type") == "logical" and rank == 0:
                classes["logical"].add(var)
                classes["result_states"].add(var)
            if dummy["name"] == call["result"]:
                classes["result_states"].add(var)
    step_written = classes.pop("step_written")
    classes["operation_only"] = (step_written - classes["read"] - classes["init_written"]
                                 - classes["logical"])
    return {k: frozenset(v) for k, v in classes.items()}
