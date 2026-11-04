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

import ast
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
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
    a keyword) — in declaration order, one name at a time, so the first
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
# and how often. The renderers call these readers through `driver_plan` below. They are stated HERE, not imported from `tools/primary_evidence.py` (which models the
# same block for the verdict and the case-file writer): this module is digested into the render
# tuple and that one into the verdict tuple, and an import across would let a render-time
# behaviour change ride on a digest that does not cover it. The two readings are held equal by
# `tools/tests/test_runner_ir.py` (`DriveAgreementTest`).

_DRIVE_LOOP_KEYS = ("n_step", "dt", "t_start", "t_end")
_IDENT_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


def _ident(text: str) -> bool:
    return bool(text) and not text[0].isdigit() and set(text) <= _IDENT_CHARS


def _state_ref(raw: Any) -> str:
    root, _, rest = str(raw).strip().partition(".")
    if root != "state" or not _ident(rest):
        raise RenderError(f"procedure binding: {raw!r} is not state.<var>")
    return rest


def drive_binding(value: Any) -> tuple[str, Any]:
    """One `arguments` value as ``(kind, ref)``: ``("inputs", "<dotted path>")``,
    ``("derived", name)``, ``("state", var)``, ``("time", None)``, ``("step", None)``,
    ``("literal", value)`` or ``("procedure", ("<expression>", calls, record))``. RenderError
    otherwise."""
    if isinstance(value, bool) or (isinstance(value, (int, float)) and value == value
                                   and value not in (float("inf"), float("-inf"))):
        return ("literal", value)
    if isinstance(value, dict):
        text = value.get("procedure")
        if set(value) <= {"procedure", "calls", "record"} and isinstance(text, str) \
                and text.strip():
            calls = value.get("calls")
            raw = value.get("record") or {}
            record = tuple(
                (str(k), tuple(_state_ref(r) for r in (refs if isinstance(refs, list) else [refs])))
                for k, refs in (raw.items() if isinstance(raw, dict) else ()))
            return ("procedure", (text.strip(), None if calls is None else _state_ref(calls),
                                  record))
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


# ------------------------------------------------------------------------------- driver plan
#
# What both language renderers spell: the host-rendered driver of one IR, as data (issue #589
# PR-1b). A renderer adds names and syntax and decides nothing the plan states.

#: The functions a procedure expression may call (the verdict tuple's
#: `primary_evidence.PROCEDURE_FUNCTIONS`, held equal by `test_runner_ir.py`).
PROCEDURE_FUNCTIONS = frozenset({"abs", "sqrt", "exp", "log", "sin", "cos", "min", "max"})
_PROCEDURE_CONSTANTS = {"pi": math.pi, "e": math.e}


#: The harness's rank-0 reading operation of each driven type.
GETTERS = {"real": "get_real", "integer": "get_int", "logical": "get_bool"}


@dataclass(frozen=True)
class HostValue:
    """One value a case file holds that a call or the loop reads. ``key`` is its key in the
    file, ``local`` the driver's variable. ``kind`` is real / integer / logical (the getter)."""
    key: str
    kind: str
    rank: int
    local: str


@dataclass(frozen=True)
class Actual:
    """How one dummy is passed. ``form``: ``host`` (``ref`` a `HostValue`), ``state`` (the state
    itself), ``work`` (its work copy on a `loop` node), ``logical`` / ``integer`` (a temporary
    the driver writes into the scalar state after the call), ``state_int`` (a rank-0 integer
    `in` read from a scalar state), ``time``, ``step``, ``literal``, ``procedure`` (``ref`` a
    `ProcedurePlan`); ``writes`` — the dummy's intent is not `in`."""
    form: str
    ref: Any = None
    writes: bool = False


@dataclass(frozen=True)
class ProcedurePlan:
    """A host-rendered procedure of a prototype: the local ``name``, the ``prototype`` signature,
    ``out`` the dummy it assigns, ``tree`` the expression, ``calls`` / ``record`` the states the
    driver writes at each call (``record``: prototype dummy -> states, one per call, or one
    rank-1 state for a rank-0 dummy)."""
    name: str
    prototype: dict[str, Any]
    out: str
    tree: ast.expr
    hosts: dict[str, str]
    calls: str | None
    record: tuple[tuple[str, tuple[str, ...]], ...]


@dataclass(frozen=True)
class CallPlan:
    operation: str
    result: str
    actuals: tuple[Actual, ...]
    #: state -> the temporary kind, for the scalars written back after the call.
    logical: tuple[str, ...]
    integer: tuple[str, ...]
    result_state: str


@dataclass(frozen=True)
class EntryPlan:
    """What one `drive.step` entry's cases do. ``cases`` None: every case. ``hosts`` is read
    from the case file before any capture; ``from_file`` the states with an `initial_state`
    entry; ``zero_arrays`` state -> extent tokens over the entry's integer locals (allocated and
    zeroed by the driver); ``zero_scalars`` likewise; ``extent_exprs`` of the state arrays in
    schema order are what the perf record multiplies at run time."""
    cases: tuple[str, ...] | None
    hosts: tuple[HostValue, ...]
    from_file: tuple[str, ...]
    zero_arrays: dict[str, tuple[str, ...]]
    zero_scalars: tuple[str, ...]
    init: CallPlan | None
    step: CallPlan
    work_nan: tuple[str, ...]
    work_copy: tuple[str, ...]
    loop_values: dict[str, Any]
    procedures: tuple[ProcedurePlan, ...]


@dataclass(frozen=True)
class DriverPlan:
    variables: dict[str, int]
    arrays: tuple[str, ...]
    scalars: tuple[str, ...]
    entries: tuple[EntryPlan, ...]
    list_form: bool
    loop: bool
    array_ranks: tuple[int, ...]
    getters: tuple[str, ...]
    series_names: tuple[str, ...]
    work_vars: tuple[str, ...]
    logical_states: tuple[str, ...]
    integer_states: tuple[str, ...]
    record_states: tuple[str, ...]
    hosts: tuple[HostValue, ...]
    procedure_hosts: tuple[HostValue, ...]


def _dummy_rank(dummy: dict[str, Any]) -> int:
    rank = dummy.get("rank")
    return rank if isinstance(rank, int) and not isinstance(rank, bool) else len(
        dummy.get("dims") or [])


def _dummy_type(dummy: dict[str, Any]) -> Any:
    return dget(dget(dummy, "spec", {}), "type")


def _interface(ir: dict[str, Any], name: Any) -> dict[str, Any]:
    for item in dget(dget(ir, "public_api", {}), "interfaces", []) or []:
        if dget(item, "name") == name and isinstance(dget(item, "signature"), dict):
            return item["signature"]
    raise RenderError(f"procedure dummy names no public_api.interfaces prototype {name!r}")


def _procedure_tree(text: str) -> ast.expr:
    try:
        return ast.parse(text, mode="eval").body
    except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
        raise RenderError(f"procedure expression {text!r} does not parse: {exc}") from None


def procedure_input_paths(tree: ast.expr) -> list[str]:
    """The `inputs.<path>` an expression reads, each once, in source order."""
    paths: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            parts: list[str] = []
            cur: ast.expr = node
            while isinstance(cur, ast.Attribute):
                parts.append(cur.attr)
                cur = cur.value
            if isinstance(cur, ast.Name) and cur.id == "inputs":
                path = ".".join(reversed(parts))
                if not any(p == path or p.startswith(path + ".") for p in paths):
                    paths = [p for p in paths if not path.startswith(p + ".")] + [path]
    return paths


def lower_expression(node: ast.expr, *, name: Callable[[str], str],
                     number: Callable[[float], str],
                     power: Callable[[str, str], str],
                     function: Callable[[str, list[str]], str]) -> str:
    """A procedure expression as source text, spelled by the renderer's four callbacks. Every
    number is lowered as a real (a `1 / 2` is a half), every operand parenthesised. Anything but
    `+ - * / **`, unary `+ -`, a number, a name, an `inputs.*` path and `PROCEDURE_FUNCTIONS` is
    refused."""
    def go(n: ast.expr) -> str:
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) \
                and not isinstance(n.value, bool):
            return number(float(n.value))
        if isinstance(n, ast.Name):
            if n.id in _PROCEDURE_CONSTANTS:
                return number(_PROCEDURE_CONSTANTS[n.id])
            return name(n.id)
        if isinstance(n, ast.Attribute):
            parts: list[str] = []
            cur: ast.expr = n
            while isinstance(cur, ast.Attribute):
                parts.append(cur.attr)
                cur = cur.value
            if isinstance(cur, ast.Name) and cur.id == "inputs":
                return name("inputs." + ".".join(reversed(parts)))
            raise RenderError("a procedure expression's dotted name is inputs.<path>")
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd)):
            return f"({'-' if isinstance(n.op, ast.USub) else '+'}{go(n.operand)})"
        if isinstance(n, ast.BinOp):
            if isinstance(n.op, ast.Pow):
                return power(go(n.left), go(n.right))
            ops = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/"}
            if type(n.op) in ops:
                return f"({go(n.left)} {ops[type(n.op)]} {go(n.right)})"
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                and n.func.id in PROCEDURE_FUNCTIONS and not n.keywords:
            return function(n.func.id, [go(a) for a in n.args])
        raise RenderError(f"a procedure expression cannot lower {ast.dump(n)[:60]}")
    return go(node)


def _extent_tokens(dummy: dict[str, Any], fallback: list[str] | None, dummy_local: Any) -> tuple[
        str, ...]:
    dims = dummy.get("dims")
    tokens = (dims if isinstance(dims, list) and dims
              and all(isinstance(t, str) and t.strip() != ":" for t in dims) else fallback)
    if tokens is None:
        raise RenderError(f"array dummy {dummy.get('name')!r} has no dims and no shape_expr")
    return tuple(_IDENT_RE.sub(lambda m: dummy_local(m.group(0)), str(t)) for t in tokens)


_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _shape_tokens(shape_expr: Any) -> list[str]:
    text = str(shape_expr).strip()
    if text.lower() == "scalar":
        return []
    return [t.strip() for t in text[1:-1].split(",") if t.strip()]


def driver_plan(ir: dict[str, Any], variables: dict[str, str]) -> DriverPlan:
    """The driver of ``ir`` (``variables``: the snapshot schema, name -> shape_expr), or
    RenderError for a shape the renderers have no spelling for: an `init` or `step` dummy that is
    neither a real, an integer, a logical nor a procedure, a logical `in` bound to a state, an
    integer array, a real `in` scalar bound to `step`."""
    ranks = {v: rank_of_shape(shape, v) for v, shape in variables.items()}
    init = drive_init(ir)
    steps = drive_steps(ir)
    loop = drive_loop(ir)
    every = drive_history_every(ir)
    if (loop is None) != (every is None):
        raise RenderError("drive.history is declared exactly when drive.loop is")
    if loop is not None and len(steps) != 1:
        raise RenderError("a loop node's drive.step is one mapping entry")
    hosts: dict[tuple[str, str, int], HostValue] = {}

    def host(key: str, kind: str, rank: int) -> HostValue:
        found = hosts.get((key, kind, rank))
        if found is None:
            found = HostValue(key, kind, rank, f"h{len(hosts) + 1}")
            hosts[(key, kind, rank)] = found
        return found

    logical_states: set[str] = set()
    integer_states: set[str] = set()
    record_states: set[str] = set()
    procedure_hosts: dict[str, HostValue] = {}
    work_vars: set[str] = set()

    def call_plan(call: dict[str, Any], role: str, used: list[HostValue],
                  procedures: list[ProcedurePlan]) -> tuple[CallPlan, dict[str, str]]:
        """The call, and dummy -> extent term for the rank-0 dummies of an extent."""
        actuals: list[Actual] = []
        logical: list[str] = []
        integer: list[str] = []
        locals_: dict[str, str] = {}
        for dummy in call["dummies"]:
            name = dummy["name"]
            kind, ref = call["bindings"][name]
            dtype, rank, intent = _dummy_type(dummy), _dummy_rank(dummy), dummy.get("intent")
            if dtype == "procedure":
                if kind != "procedure":
                    raise RenderError(f"procedure dummy {name!r} takes {{procedure: <expr>}}")
                proto = _interface(ir, dget(dummy["spec"], "interface"))
                plan = _procedure_plan(call, name, proto, ref, len(procedures) + 1, host,
                                       procedure_hosts, used)
                procedures.append(plan)
                actuals.append(Actual("procedure", plan))
                record_states.update([plan.calls] if plan.calls else [])
                record_states.update(r for _d, refs in plan.record for r in refs)
                continue
            if dtype not in ("real", "integer", "logical"):
                raise RenderError(f"dummy {name!r} of type {dtype!r} cannot be driven")
            if rank >= 1 and dtype != "real":
                raise RenderError(f"dummy {name!r}: a non-real array has no driver storage")
            if kind in ("inputs", "derived"):
                value = host(f"{kind}.{ref}", dtype, rank)
                used.append(value)
                actuals.append(Actual("host", value))
                if rank == 0:
                    locals_[name] = value.local
            elif kind == "literal":
                actuals.append(Actual("literal", (dtype, ref)))
                if rank == 0:
                    locals_[name] = str(ref)
            elif kind == "time":
                actuals.append(Actual("time"))
            elif kind == "step":
                actuals.append(Actual("step"))
            elif kind == "state":
                if rank == 0 and intent == "out":
                    if dtype == "logical":
                        logical.append(ref)
                        logical_states.add(ref)
                        actuals.append(Actual("logical", ref))
                    elif dtype == "integer":
                        integer.append(ref)
                        integer_states.add(ref)
                        actuals.append(Actual("integer", ref))
                    else:
                        actuals.append(Actual("state", ref, True))
                elif rank == 0 and dtype == "logical":
                    raise RenderError(f"logical in dummy {name!r} bound to a state")
                elif rank == 0 and dtype == "integer":
                    actuals.append(Actual("state_int", ref))
                elif role == "step" and loop is not None and intent in ("out", "inout"):
                    work_vars.add(ref)
                    actuals.append(Actual("work", ref, True))
                else:
                    actuals.append(Actual("state", ref, intent != "in"))
            else:
                raise RenderError(f"dummy {name!r} is bound to {kind!r}")
        result = call["bindings"].get(call["result"])
        if result is None or result[0] != "state" or result[1] not in logical:
            raise RenderError(f"{call['operation']}: result {call['result']!r} is not a logical "
                              "out bound to a scalar state")
        return (CallPlan(call["operation"], call["result"], tuple(actuals), tuple(logical),
                         tuple(integer), result[1]), locals_)

    entries: list[EntryPlan] = []
    for step in steps:
        used: list[HostValue] = []
        procedures: list[ProcedurePlan] = []
        init_plan, init_locals = (call_plan(init, "init", used, procedures)
                                  if init is not None else (None, {}))
        step_plan, step_locals = call_plan(step, "step", used, procedures)
        locals_ = {**step_locals, **init_locals}
        classes = _case_classes(init, step)
        written_by_init = classes["init_written"]
        driver_written = written_by_init | frozenset(step_plan.logical) \
            | frozenset(init_plan.logical if init_plan else ()) | frozenset(record_states)
        from_file = tuple(v for v in variables if v not in driver_written)
        zero_scalars = tuple(v for v in variables if v in driver_written and ranks[v] == 0)
        zero_arrays: dict[str, tuple[str, ...]] = {}

        def dummy_local(token: str) -> str:
            if token not in locals_:
                raise RenderError(f"extent token {token!r} names no rank-0 dummy bound to an "
                                  "input, a derived value or a literal")
            return locals_[token]

        for var in variables:
            if var in driver_written and ranks[var] >= 1:
                bound = _dummy_bound_to(init, step, var)
                zero_arrays[var] = _extent_tokens(
                    bound if bound is not None else {}, _shape_tokens(variables[var]),
                    dummy_local)
        loop_values: dict[str, Any] = {}
        if loop is not None:
            for key in ("n_step", "dt", "t_start"):
                kind, ref = loop[key]
                if kind == "state":
                    loop_values[key] = ("state", ref)
                else:
                    value = host(f"{kind}.{ref}", "real", 0)
                    used.append(value)
                    loop_values[key] = ("host", value)
            value = host(f"inputs.{every}", "real", 0)
            used.append(value)
            loop_values["every"] = ("host", value)
        work_nan = tuple(a.ref for a, d in zip(step_plan.actuals, step["dummies"])
                         if a.form == "work" and d.get("intent") == "out")
        work_copy = tuple(a.ref for a, d in zip(step_plan.actuals, step["dummies"])
                          if a.form == "work" and d.get("intent") == "inout")
        entries.append(EntryPlan(
            cases=tuple(step["cases"]) if step["cases"] is not None else None,
            hosts=tuple(dict.fromkeys(used)), from_file=from_file, zero_arrays=zero_arrays,
            zero_scalars=zero_scalars, init=init_plan, step=step_plan, work_nan=work_nan,
            work_copy=work_copy, loop_values=loop_values, procedures=tuple(procedures)))
    arrays = tuple(v for v in variables if ranks[v] >= 1)
    scalars = tuple(v for v in variables if ranks[v] == 0)
    getters: set[str] = set()
    for value in hosts.values():
        getters.add(f"get_array_r{value.rank}" if value.rank else GETTERS[value.kind])
    for entry in entries:
        for var in entry.from_file:
            getters.add(f"get_array_r{ranks[var]}" if ranks[var] else "get_real")
    for var in record_states:
        if var not in variables:
            raise RenderError(f"record state {var!r} is not a snapshot variable")
    return DriverPlan(
        variables=ranks, arrays=arrays, scalars=scalars, entries=tuple(entries),
        list_form=steps[0]["cases"] is not None, loop=loop is not None,
        array_ranks=tuple(sorted({ranks[v] for v in arrays})),
        getters=tuple(sorted(getters)),
        series_names=("step", *(f"{v}_{m}" for v in arrays for m in ("min", "max")))
        if loop is not None else (),
        work_vars=tuple(v for v in variables if v in work_vars),
        logical_states=tuple(v for v in variables if v in logical_states),
        integer_states=tuple(v for v in variables if v in integer_states),
        record_states=tuple(v for v in variables if v in record_states),
        hosts=tuple(hosts.values()), procedure_hosts=tuple(procedure_hosts.values()))


def _procedure_plan(call: dict[str, Any], dummy: str, proto: dict[str, Any], binding: Any,
                    index: int, host: Callable[[str, str, int], HostValue],
                    procedure_hosts: dict[str, HostValue], used: list[HostValue],
                    ) -> ProcedurePlan:
    text, calls, record = binding
    args = proto.get("args") or []
    outs = [a for a in args if a.get("intent") == "out"]
    if len(outs) != 1 or _dummy_rank(outs[0]) < 1:
        raise RenderError(f"procedure {dummy!r}: its prototype has no one array out")
    tree = _procedure_tree(text)
    hosts: dict[str, str] = {}
    for path in procedure_input_paths(tree):
        value = host(f"inputs.{path}", "real", 0)
        used.append(value)
        procedure_hosts[path] = value
        hosts[f"inputs.{path}"] = value.local
    return ProcedurePlan(name=f"driver_{dummy}_{index}", prototype=proto, out=outs[0]["name"],
                         tree=tree, hosts=hosts, calls=calls, record=record)


def _dummy_bound_to(init: dict[str, Any] | None, step: dict[str, Any], var: str
                    ) -> dict[str, Any] | None:
    for call in ([init] if init is not None else []) + [step]:
        for dummy in call["dummies"]:
            kind, ref = call["bindings"][dummy["name"]]
            if kind == "state" and ref == var:
                return dummy
    return None


def _case_classes(init: dict[str, Any] | None, step: dict[str, Any]) -> dict[str, frozenset[str]]:
    written: set[str] = set()
    for dummy in (init["dummies"] if init is not None else ()):
        kind, ref = init["bindings"][dummy["name"]]
        if kind == "state" and dummy.get("intent") == "out":
            written.add(ref)
    del step
    return {"init_written": frozenset(written)}
