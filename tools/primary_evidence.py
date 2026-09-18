#!/usr/bin/env python3
"""Host-evaluated primary predicates (Z6, issue #255; `zero_base_architecture.md` §A4).

A `test_predicates[]` condition compares a value the GENERATED checks module computed and
wrote into `diagnostics.json` — secondary evidence, produced by the same leaf turn that
produced the kernel under test. A `primary_predicates[]` entry is the corroborant: an
arithmetic expression over the PRIMARY state the host-rendered runner captured through the
certified harness emitters (`raw/state_snapshots/initial/<case_id>.json` right after
`case_setup`, `raw/state_snapshots/<case_id>.json` right after `case_run`), over the case's
declared inputs, and over grid coordinates the host derives from those inputs — evaluated
HERE, by the host, with the generated code contributing nothing to the value. The LLM
writes the expression at Compile; no LLM holds the number.

The IR shape (`docs/workflow/phases/phase_01_compile.md` §`spec.ir.yaml` schema)::

    io_contract:
      raw_requirements:
        required_evidence:
          - artifact: state_snapshots
            schema:
              variables: [{name: h, shape_expr: "[nx, ny]"}, ...]
              coordinates:                       # optional; each axis the expressions need
                - {name: x, axis: 0, count: inputs.grid.nx, length: inputs.grid.L_x,
                   placement: cell_center}
      primary_predicates:
        - test_id: <a tests.md test_id>
          quantity: <name>                       # the quantity the expression evaluates
          target_cases: [<case_id>, ...]
          bind: {M0: "sum(initial.h) * inputs.grid.dx", ...}   # optional named sub-expressions
          expr: "abs(M1 - M0) / max(abs(M0), 1e-14)"
          op: le                                 # eq | ne | le | ge | lt | gt
          value: 1.0e-10                         # OR {per_case: {<case_id>: v}}
          per_case: true                         # exactly one of per_case: true / case: <id>

**The grammar is closed** (`GRAMMAR_VERSION`): a Python expression restricted, by an allowlist
over `ast` node types, to the arithmetic operators `+ - * / **`, unary `-`, numeric
constants, the names below, and calls to the functions of `FUNCTIONS`. A subscript, a
slice, a comparison, a boolean operator, a lambda, a keyword argument, a starred argument,
a string outside the one place a string is admitted (`at('<case_id>')`), an attribute of
anything but the roots listed here — each is refused at parse, never evaluated. Adding a
function or a name root is a grammar change and bumps `GRAMMAR_VERSION`.

Names, and where each resolves:

- ``initial.<var>`` / ``final.<var>`` — a snapshot schema variable at the two capture points
  (a scalar `shape_expr` gives a float, an array one a float64 array of that rank);
  ``initial.<time_variable>`` / ``final.<time_variable>`` — the capture's time value.
- ``inputs.<a>.<b>...`` — a NUMERIC value of the case's `case.test_case_set[].inputs`
  (a string, a boolean, a list or a mapping at that path is refused at name resolution).
- ``<coordinate name>`` — a `schema.coordinates[]` axis, as a float64 array carrying the
  axis's `count` cell-centre positions along `axis` and extent 1 on every other axis of the
  state's rank, so it broadcasts against a state array without a subscript.
- ``at('<case_id>').initial.<var>`` / ``at('<case_id>').final.<var>`` — another case of the
  SAME predicate's `target_cases` (a cross-case reduction: a convergence order, a
  translation pair).
- a `bind` name — a named sub-expression, evaluated in `bind` order; a bind may reference
  only the binds written before it (acyclic by construction).
- ``pi`` / ``e``.

Every intermediate result must be finite, and the predicate's result must be a finite
SCALAR; a binary operator's operands must have equal rank or one must be a scalar (numpy's
right-aligned broadcasting of unequal ranks is refused, so a `[nx]` array never silently
pairs with a `[nx, ny]` one). Any of these — and an unallocated, ragged, non-numeric,
non-finite or wrong-rank snapshot array, a `roll` shift that is not an integer, a case whose
capture file is absent — raises `PrimaryEvidenceError`, which `evaluate_primary_predicates`
records on that predicate as a STRUCTURAL failure (the evidence could not be judged) and
`verdict_evaluator.evaluate_verdict` folds into `structural_violation`.

Pure with respect to the conductor: reads the run node directory it is given and nothing
else, like `tools/raw_evidence_excerpt.py`. `evaluate_verdict` never reads a file; it takes
the list this module returns.
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import sys
from pathlib import Path
from typing import Any, NamedTuple

import numpy as np

from tools.verdict_evaluator import _QUANTITY_RE, _apply_op, _is_number, _resolve_value

#: The version of the expression grammar: the allowed `ast` nodes, the function table, the
#: name roots and the broadcasting rule. Bumped when any of them changes; recorded on every
#: evaluated predicate so a verdict says which grammar valued it.
GRAMMAR_VERSION = 1

#: `quantity` names: lowercase identifiers with dots, the same shape as a metric address.
#: ONE definition, in the evaluator that reads it on the secondary side too.
QUANTITY_RE = _QUANTITY_RE

#: The comparison operators a primary predicate admits. `includes` is a set-membership test on
#: a diagnostics list and has no meaning for a scalar the host computed.
PRIMARY_OPS: frozenset[str] = frozenset({"eq", "ne", "le", "ge", "lt", "gt"})

#: The one coordinate placement the host derives in this grammar version: cell centres of a
#: uniform axis, `(i + 1/2) * length / count`.
COORDINATE_PLACEMENTS: frozenset[str] = frozenset({"cell_center"})

#: name -> (min_args, max_args). `min` / `max` reduce with ONE argument and are elementwise
#: with two or more. `roll` takes the array and one integer shift per axis, leading axes
#: first. This table is the whole function vocabulary.
FUNCTIONS: dict[str, tuple[int, int]] = {
    "sum": (1, 1), "mean": (1, 1), "min": (1, 8), "max": (1, 8), "abs": (1, 1),
    "sqrt": (1, 1), "exp": (1, 1), "log": (1, 1), "log2": (1, 1), "sin": (1, 1),
    "cos": (1, 1), "norm2": (1, 1), "maxabs": (1, 1), "roll": (2, 5), "ceil": (1, 1),
    "floor": (1, 1),
}
CONSTANTS: dict[str, float] = {"pi": math.pi, "e": math.e}
#: Name roots an attribute chain may start from. `at` is a call, not a root.
CAPTURE_POINTS: tuple[str, str] = ("initial", "final")
_INPUTS_ROOT = "inputs"
_AT = "at"

_BINOPS: dict[type, str] = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/",
                            ast.Pow: "**"}


class PrimaryEvidenceError(ValueError):
    """A primary predicate could not be parsed, resolved or evaluated."""


# --------------------------------------------------------------------------------- grammar

class NameRef(NamedTuple):
    """One name an expression references, as the resolver sees it.

    ``kind`` is ``capture`` (``initial.<var>`` / ``final.<var>``; ``case`` is None for the
    predicate's own case, else the ``at('<case>')`` case), ``inputs`` (``name`` is the dotted
    path under ``inputs``), or ``name`` (a bare identifier: a coordinate, a bind or a
    constant)."""
    kind: str
    name: str
    point: str | None = None
    case: str | None = None


def _attr_chain(node: ast.AST) -> tuple[ast.AST, list[str]]:
    """Split an attribute chain into its root node and the attribute names, outermost last."""
    attrs: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        attrs.append(cur.attr)
        cur = cur.value
    attrs.reverse()
    return cur, attrs


def _is_at_call(node: ast.AST) -> bool:
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == _AT)


def _check_node(node: ast.AST, refs: list[NameRef]) -> None:
    """Walk one expression node against the allowlist, collecting the names it references.
    Raises `PrimaryEvidenceError` on the first node outside the grammar."""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise PrimaryEvidenceError(
                f"constant {node.value!r} is not a number (only numeric constants are admitted; "
                "a string is admitted only as the argument of at('<case_id>'))")
        return
    if isinstance(node, ast.BinOp):
        if type(node.op) not in _BINOPS:
            raise PrimaryEvidenceError(f"operator {type(node.op).__name__} is not admitted "
                                       f"(admitted: {' '.join(_BINOPS.values())})")
        _check_node(node.left, refs)
        _check_node(node.right, refs)
        return
    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, (ast.USub, ast.UAdd)):
            raise PrimaryEvidenceError(f"unary {type(node.op).__name__} is not admitted")
        _check_node(node.operand, refs)
        return
    if isinstance(node, ast.Name):
        if node.id in CAPTURE_POINTS or node.id in (_INPUTS_ROOT, _AT):
            raise PrimaryEvidenceError(
                f"{node.id!r} is a name root, not a value (write {node.id}.<name>)"
                if node.id != _AT else "at('<case_id>') must be followed by .initial.<variable>"
                " or .final.<variable>")
        refs.append(NameRef("name", node.id))
        return
    if isinstance(node, ast.Attribute):
        root, attrs = _attr_chain(node)
        if isinstance(root, ast.Name) and root.id in CAPTURE_POINTS:
            if len(attrs) != 1:
                raise PrimaryEvidenceError(
                    f"{root.id}.{'.'.join(attrs)}: a capture reference is exactly "
                    f"{root.id}.<variable>")
            refs.append(NameRef("capture", attrs[0], point=root.id))
            return
        if isinstance(root, ast.Name) and root.id == _INPUTS_ROOT:
            refs.append(NameRef("inputs", ".".join(attrs)))
            return
        if _is_at_call(root):
            call = root  # type: ignore[assignment]
            assert isinstance(call, ast.Call)
            if (len(call.args) != 1 or call.keywords
                    or not isinstance(call.args[0], ast.Constant)
                    or not isinstance(call.args[0].value, str)
                    or not call.args[0].value.strip()):
                raise PrimaryEvidenceError("at(...) takes exactly one case_id string")
            if len(attrs) != 2 or attrs[0] not in CAPTURE_POINTS:
                raise PrimaryEvidenceError(
                    f"at({call.args[0].value!r}).{'.'.join(attrs)}: a cross-case reference is "
                    "exactly at('<case_id>').initial.<variable> or "
                    "at('<case_id>').final.<variable>")
            refs.append(NameRef("capture", attrs[1], point=attrs[0],
                                case=call.args[0].value.strip()))
            return
        raise PrimaryEvidenceError(
            f"attribute chain rooted at {ast.dump(root)} is not admitted (roots: "
            f"{', '.join(CAPTURE_POINTS)}, {_INPUTS_ROOT}, at('<case_id>'))")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise PrimaryEvidenceError("only a named function may be called")
        name = node.func.id
        if name == _AT:
            raise PrimaryEvidenceError(
                "at('<case_id>') must be followed by .initial.<variable> or .final.<variable>")
        if name not in FUNCTIONS:
            raise PrimaryEvidenceError(
                f"function {name!r} is not admitted (admitted: {', '.join(sorted(FUNCTIONS))})")
        if node.keywords:
            raise PrimaryEvidenceError(f"{name}(): keyword arguments are not admitted")
        lo, hi = FUNCTIONS[name]
        if not (lo <= len(node.args) <= hi):
            raise PrimaryEvidenceError(
                f"{name}() takes {lo} to {hi} arguments, got {len(node.args)}")
        for arg in node.args:
            if isinstance(arg, ast.Starred):
                raise PrimaryEvidenceError(f"{name}(): a starred argument is not admitted")
            _check_node(arg, refs)
        return
    raise PrimaryEvidenceError(
        f"{type(node).__name__} is not admitted in a primary predicate expression")


def parse_expr(text: Any) -> ast.Expression:
    """Parse ``text`` as one expression of the closed grammar. Raises `PrimaryEvidenceError`
    for a non-string, an unparsable string, or any construct outside the allowlist."""
    if not isinstance(text, str) or not text.strip():
        raise PrimaryEvidenceError("expr must be a non-empty string")
    try:
        tree = ast.parse(text.strip(), mode="eval")
    except (SyntaxError, ValueError) as exc:
        raise PrimaryEvidenceError(f"expr does not parse: {exc}") from None
    _check_node(tree.body, [])
    return tree


def expr_names(tree: ast.Expression) -> list[NameRef]:
    """Every name reference in a parsed expression, in source order."""
    refs: list[NameRef] = []
    _check_node(tree.body, refs)
    return refs


# ----------------------------------------------------------------------------- environment

class CaseEnv(NamedTuple):
    """Everything an expression can name inside one case."""
    case_id: str
    initial: dict[str, Any]     # variable -> float | ndarray (the time variable included)
    final: dict[str, Any]
    inputs: dict[str, Any]      # the case's `inputs` mapping, as authored
    coordinates: dict[str, np.ndarray]


def _shape_dims(shape_expr: Any) -> list[str]:
    """The dimension tokens of a `shape_expr` (`[]` for `scalar`). The full grammar is
    `spec/schema/ir/shape_expr.schema.json`, gated at Compile; this reads the accepted forms."""
    token = str(shape_expr or "").strip()
    if token.lower() == "scalar":
        return []
    if len(token) >= 2 and token[0] in "[(" and token[-1] in "])":
        return [d.strip() for d in token[1:-1].split(",")]
    raise PrimaryEvidenceError(f"shape_expr {shape_expr!r} is not a recognised form")


def snapshot_schema(ir: dict[str, Any]) -> dict[str, Any] | None:
    """The `state_snapshots` schema of an IR, or None when it declares none."""
    io = ir.get("io_contract") if isinstance(ir, dict) else None
    rr = io.get("raw_requirements") if isinstance(io, dict) else None
    entries = rr.get("required_evidence") if isinstance(rr, dict) else None
    for entry in (entries if isinstance(entries, list) else []):
        if isinstance(entry, dict) and entry.get("artifact") == "state_snapshots":
            schema = entry.get("schema")
            return schema if isinstance(schema, dict) else {}
    return None


def schema_variables(schema: dict[str, Any]) -> dict[str, list[str]]:
    """variable name -> shape dims, over `schema.variables[]`."""
    out: dict[str, list[str]] = {}
    for entry in (schema.get("variables") if isinstance(schema.get("variables"), list) else []):
        if isinstance(entry, dict) and isinstance(entry.get("name"), str):
            out[entry["name"].strip()] = _shape_dims(entry.get("shape_expr"))
    return out


def state_rank(schema: dict[str, Any]) -> int:
    """The rank a coordinate array is shaped to: the highest rank among the schema variables."""
    dims = schema_variables(schema).values()
    return max((len(d) for d in dims), default=0)


def _lookup_input(inputs: Any, dotted: str) -> tuple[bool, Any]:
    cur = inputs
    for seg in dotted.split("."):
        if not isinstance(cur, dict) or seg not in cur:
            return (False, None)
        cur = cur[seg]
    return (True, cur)


def resolve_input_number(inputs: Any, dotted: str) -> float:
    """The numeric value at ``inputs.<dotted>``; raises when absent or not a number."""
    present, value = _lookup_input(inputs, dotted)
    if not present:
        raise PrimaryEvidenceError(f"inputs.{dotted} is not a key of this case's inputs")
    if not _is_number(value):
        raise PrimaryEvidenceError(
            f"inputs.{dotted} is {type(value).__name__}, not a number (only a numeric case "
            "input can enter an expression)")
    return float(value)


def _coordinate_param(spec: dict[str, Any], key: str, inputs: Any) -> float:
    raw = spec.get(key)
    if _is_number(raw):
        return float(raw)
    if isinstance(raw, str) and raw.startswith(_INPUTS_ROOT + "."):
        return resolve_input_number(inputs, raw[len(_INPUTS_ROOT) + 1:])
    raise PrimaryEvidenceError(
        f"coordinates[{spec.get('name')!r}].{key} must be a number or inputs.<path> "
        f"(got {raw!r})")


def coordinate_arrays(schema: dict[str, Any], inputs: Any) -> dict[str, np.ndarray]:
    """The coordinate arrays of one case, shaped to the state rank with the axis's `count`
    positions along `axis` and extent 1 elsewhere."""
    rank = state_rank(schema)
    out: dict[str, np.ndarray] = {}
    specs = schema.get("coordinates")
    for spec in (specs if isinstance(specs, list) else []):
        if not isinstance(spec, dict) or not isinstance(spec.get("name"), str):
            raise PrimaryEvidenceError("coordinates[] entries must be mappings with a name")
        name = spec["name"].strip()
        axis = spec.get("axis")
        if not isinstance(axis, int) or isinstance(axis, bool) or not (0 <= axis < rank):
            raise PrimaryEvidenceError(
                f"coordinates[{name!r}].axis must be an integer in [0, {rank}) "
                f"(the state rank); got {axis!r}")
        placement = spec.get("placement")
        if placement not in COORDINATE_PLACEMENTS:
            raise PrimaryEvidenceError(
                f"coordinates[{name!r}].placement must be one of "
                f"{sorted(COORDINATE_PLACEMENTS)}; got {placement!r}")
        count = _coordinate_param(spec, "count", inputs)
        length = _coordinate_param(spec, "length", inputs)
        if not (count.is_integer() and count >= 1):
            raise PrimaryEvidenceError(f"coordinates[{name!r}].count resolved to {count!r}, "
                                       "not a positive integer")
        if not (math.isfinite(length) and length > 0):
            raise PrimaryEvidenceError(f"coordinates[{name!r}].length resolved to {length!r}, "
                                       "not a positive number")
        n = int(count)
        positions = (np.arange(n, dtype=np.float64) + 0.5) * (length / n)
        shape = [1] * rank
        shape[axis] = n
        out[name] = positions.reshape(shape)
    return out


def _load_capture(path: Path, variables: dict[str, list[str]],
                  time_variable: str | None) -> dict[str, Any]:
    """One capture file as `variable -> float | float64 ndarray`, each checked against its
    declared rank (and equal extents for a repeated dimension token) and for finiteness."""
    if not path.is_file():
        raise PrimaryEvidenceError(f"capture file {path.name} is absent")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PrimaryEvidenceError(f"capture file {path.name} is unreadable: {exc}") from None
    if not isinstance(doc, dict):
        raise PrimaryEvidenceError(f"capture file {path.name} is not a JSON object")
    out: dict[str, Any] = {}
    wanted = dict(variables)
    if time_variable:
        wanted[time_variable] = []
    for name, dims in wanted.items():
        if name not in doc:
            raise PrimaryEvidenceError(f"{path.name}: variable {name!r} is not captured")
        raw = doc[name]
        try:
            arr = np.asarray(raw, dtype=np.float64)
        except (TypeError, ValueError) as exc:
            raise PrimaryEvidenceError(
                f"{path.name}: variable {name!r} is not a rectangular numeric array "
                f"({exc})") from None
        if isinstance(raw, bool) or (isinstance(raw, list) and _contains_non_number(raw)):
            raise PrimaryEvidenceError(f"{path.name}: variable {name!r} holds a non-numeric value")
        if arr.ndim != len(dims):
            raise PrimaryEvidenceError(
                f"{path.name}: variable {name!r} has rank {arr.ndim}, declared "
                f"shape_expr has rank {len(dims)}")
        bound: dict[str, int] = {}
        for token, extent in zip(dims, arr.shape):
            if token.isdigit():
                if int(token) != extent:
                    raise PrimaryEvidenceError(
                        f"{path.name}: variable {name!r} extent {extent} != declared {token}")
            elif bound.setdefault(token, extent) != extent:
                raise PrimaryEvidenceError(
                    f"{path.name}: variable {name!r} repeats dimension {token!r} with "
                    f"unequal extents")
        if not np.all(np.isfinite(arr)):
            raise PrimaryEvidenceError(f"{path.name}: variable {name!r} holds a non-finite value")
        out[name] = float(arr) if arr.ndim == 0 else arr
    return out


def _contains_non_number(value: Any) -> bool:
    if isinstance(value, list):
        return any(_contains_non_number(v) for v in value)
    return not _is_number(value)


def load_case_env(run_dir: Path, case: dict[str, Any], schema: dict[str, Any]) -> CaseEnv:
    """The environment of one case: both captures under ``run_dir/raw/state_snapshots``, the
    case's inputs and its coordinate arrays."""
    case_id = str(case.get("case_id") or "").strip()
    if not case_id:
        raise PrimaryEvidenceError("case has no case_id")
    inputs = case.get("inputs")
    inputs = inputs if isinstance(inputs, dict) else {}
    variables = schema_variables(schema)
    tv = schema.get("time_variable")
    tv = tv.strip() if isinstance(tv, str) and tv.strip() else None
    sdir = Path(run_dir) / "raw" / "state_snapshots"
    initial = _load_capture(sdir / "initial" / f"{case_id}.json", variables, tv)
    final = _load_capture(sdir / f"{case_id}.json", variables, tv)
    return CaseEnv(case_id=case_id, initial=initial, final=final, inputs=inputs,
                   coordinates=coordinate_arrays(schema, inputs))


# ------------------------------------------------------------------------------ evaluation

def _finite(value: Any, what: str) -> Any:
    arr = np.asarray(value)
    if not np.all(np.isfinite(arr)):
        raise PrimaryEvidenceError(f"{what} produced a non-finite value")
    return value


def _rank_compatible(a: Any, b: Any, what: str) -> None:
    ra, rb = np.ndim(a), np.ndim(b)
    if ra != rb and ra != 0 and rb != 0:
        raise PrimaryEvidenceError(
            f"{what}: operands of rank {ra} and {rb} (an operand must be a scalar or of equal "
            "rank; right-aligned broadcasting is not admitted)")


def _call(name: str, args: list[Any]) -> Any:
    with np.errstate(all="ignore"):
        if name == "sum":
            return np.sum(args[0])
        if name == "mean":
            return np.mean(args[0])
        if name in ("min", "max"):
            if len(args) == 1:
                return np.min(args[0]) if name == "min" else np.max(args[0])
            acc = args[0]
            for other in args[1:]:
                _rank_compatible(acc, other, f"{name}()")
                acc = np.minimum(acc, other) if name == "min" else np.maximum(acc, other)
            return acc
        if name == "abs":
            return np.abs(args[0])
        if name == "sqrt":
            return np.sqrt(args[0])
        if name == "exp":
            return np.exp(args[0])
        if name == "log":
            return np.log(args[0])
        if name == "log2":
            return np.log2(args[0])
        if name == "sin":
            return np.sin(args[0])
        if name == "cos":
            return np.cos(args[0])
        if name == "norm2":
            return np.sqrt(np.sum(np.asarray(args[0], dtype=np.float64) ** 2))
        if name == "maxabs":
            return np.max(np.abs(args[0]))
        if name == "ceil":
            return np.ceil(args[0])
        if name == "floor":
            return np.floor(args[0])
        if name == "roll":
            arr = np.asarray(args[0])
            shifts = args[1:]
            if len(shifts) != arr.ndim:
                raise PrimaryEvidenceError(
                    f"roll(): {len(shifts)} shift(s) for an array of rank {arr.ndim}")
            ints: list[int] = []
            for s in shifts:
                if np.ndim(s) != 0:
                    raise PrimaryEvidenceError("roll(): a shift must be a scalar")
                fs = float(s)
                if not fs.is_integer():
                    raise PrimaryEvidenceError(f"roll(): shift {fs!r} is not an integer")
                ints.append(int(fs))
            return np.roll(arr, tuple(ints), axis=tuple(range(arr.ndim)))
    raise PrimaryEvidenceError(f"function {name!r} is not admitted")  # unreachable past parse


def _eval_node(node: ast.AST, env: CaseEnv, at_envs: dict[str, CaseEnv],
               binds: dict[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return float(node.value)
    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left, env, at_envs, binds)
        right = _eval_node(node.right, env, at_envs, binds)
        sym = _BINOPS[type(node.op)]
        _rank_compatible(left, right, f"operator {sym}")
        with np.errstate(all="ignore"):
            if sym == "+":
                out = left + right
            elif sym == "-":
                out = left - right
            elif sym == "*":
                out = left * right
            elif sym == "/":
                out = left / right
            else:
                out = np.power(left, right)
        return _finite(out, f"operator {sym}")
    if isinstance(node, ast.UnaryOp):
        val = _eval_node(node.operand, env, at_envs, binds)
        return -val if isinstance(node.op, ast.USub) else +val
    if isinstance(node, ast.Name):
        if node.id in binds:
            return binds[node.id]
        if node.id in env.coordinates:
            return env.coordinates[node.id]
        if node.id in CONSTANTS:
            return CONSTANTS[node.id]
        raise PrimaryEvidenceError(f"name {node.id!r} is not a bind, a coordinate or a constant")
    if isinstance(node, ast.Attribute):
        root, attrs = _attr_chain(node)
        if isinstance(root, ast.Name) and root.id in CAPTURE_POINTS:
            return _capture_value(env, root.id, attrs[0])
        if isinstance(root, ast.Name) and root.id == _INPUTS_ROOT:
            return resolve_input_number(env.inputs, ".".join(attrs))
        assert _is_at_call(root) and isinstance(root, ast.Call)
        case_id = str(root.args[0].value).strip()  # type: ignore[attr-defined]
        if case_id not in at_envs:
            raise PrimaryEvidenceError(
                f"at({case_id!r}) names a case outside this predicate's target_cases")
        return _capture_value(at_envs[case_id], attrs[0], attrs[1])
    if isinstance(node, ast.Call):
        assert isinstance(node.func, ast.Name)
        args = [_eval_node(a, env, at_envs, binds) for a in node.args]
        return _finite(_call(node.func.id, args), f"{node.func.id}()")
    raise PrimaryEvidenceError(f"{type(node).__name__} is not admitted")  # unreachable past parse


def _capture_value(env: CaseEnv, point: str, var: str) -> Any:
    table = env.initial if point == "initial" else env.final
    if var not in table:
        raise PrimaryEvidenceError(f"{point}.{var}: not a snapshot schema variable")
    return table[var]


def evaluate(tree: ast.Expression, env: CaseEnv, *, at_envs: dict[str, CaseEnv] | None = None,
             binds: dict[str, Any] | None = None) -> Any:
    """Evaluate a parsed expression in ``env``. Returns a float or a float64 array."""
    return _eval_node(tree.body, env, at_envs or {}, binds or {})


def evaluate_binds(bind: Any, env: CaseEnv, at_envs: dict[str, CaseEnv]) -> dict[str, Any]:
    """Evaluate a predicate's `bind` mapping in order; each entry sees the earlier ones."""
    out: dict[str, Any] = {}
    if bind is None:
        return out
    if not isinstance(bind, dict):
        raise PrimaryEvidenceError("bind must be a mapping of name -> expression")
    for name, text in bind.items():
        if not isinstance(name, str) or not name.isidentifier():
            raise PrimaryEvidenceError(f"bind name {name!r} is not an identifier")
        if name in CONSTANTS or name in FUNCTIONS or name in CAPTURE_POINTS \
                or name in (_INPUTS_ROOT, _AT) or name in env.coordinates:
            raise PrimaryEvidenceError(f"bind name {name!r} shadows a grammar name")
        out[name] = evaluate(parse_expr(text), env, at_envs=at_envs, binds=out)
    return out


def _scalar(value: Any, what: str) -> float:
    if np.ndim(value) != 0:
        raise PrimaryEvidenceError(
            f"{what} evaluated to an array of shape {list(np.shape(value))}, not a scalar "
            "(reduce it with sum/min/max/mean/norm2/maxabs)")
    out = float(value)
    if not math.isfinite(out):
        raise PrimaryEvidenceError(f"{what} evaluated to a non-finite value")
    return out


def _predicate_scope(pred: dict[str, Any], loc: str) -> tuple[str, list[str]]:
    """``("per_case", target_cases)`` or ``("case", [the one case])``."""
    targets = pred.get("target_cases")
    if not isinstance(targets, list) or not targets \
            or not all(isinstance(c, str) and c.strip() for c in targets):
        raise PrimaryEvidenceError(f"{loc}.target_cases must be a non-empty list of case_ids")
    targets = [c.strip() for c in targets]
    per_case = pred.get("per_case")
    has_case = "case" in pred
    if per_case is True and not has_case:
        return ("per_case", targets)
    if has_case and not per_case:
        sel = pred.get("case")
        if not isinstance(sel, str) or not sel.strip():
            raise PrimaryEvidenceError(f"{loc}.case must be a non-empty case_id")
        if sel.strip() not in targets:
            raise PrimaryEvidenceError(
                f"{loc}.case {sel!r} is not one of its target_cases ({sorted(targets)})")
        return ("case", [sel.strip()])
    raise PrimaryEvidenceError(
        f"{loc}: exactly one of `per_case: true` or `case: <case_id>` names the scope")


def _cases_by_id(ir: dict[str, Any]) -> dict[str, dict[str, Any]]:
    case = ir.get("case") if isinstance(ir, dict) else None
    tcs = case.get("test_case_set") if isinstance(case, dict) else None
    out: dict[str, dict[str, Any]] = {}
    for c in (tcs if isinstance(tcs, list) else []):
        if isinstance(c, dict) and isinstance(c.get("case_id"), str) and c["case_id"].strip():
            out[c["case_id"].strip()] = c
    return out


def primary_predicates(ir: dict[str, Any]) -> list[Any]:
    io = ir.get("io_contract") if isinstance(ir, dict) else None
    preds = io.get("primary_predicates") if isinstance(io, dict) else None
    return preds if isinstance(preds, list) else []


def evaluate_primary_predicates(ir: dict[str, Any], run_dir: Path) -> list[dict[str, Any]]:
    """Evaluate every `io_contract.primary_predicates[]` entry of ``ir`` against the captures
    under ``run_dir``. Returns one record per predicate, in order::

        {test_id, quantity, expr, op, grammar_version, satisfied, kind, evaluated: [
            {case, value, rhs, satisfied} | {case, satisfied: false, reason, error}]}

    ``kind`` is ``pass`` / ``physics`` (a comparison was false) / ``structural`` (the
    expression could not be evaluated: `PrimaryEvidenceError`). An empty list when the IR
    declares no primary predicate. A predicate whose own shape is malformed (no test_id, no
    quantity, no expr, an inadmissible op or scope) raises `PrimaryEvidenceError` — the Compile
    gate refuses those, so reaching one here is a defect of the certified IR."""
    preds = primary_predicates(ir)
    if not preds:
        return []
    schema = snapshot_schema(ir)
    if schema is None:
        raise PrimaryEvidenceError(
            "primary_predicates declared but the IR has no state_snapshots evidence entry")
    cases = _cases_by_id(ir)
    env_cache: dict[str, CaseEnv] = {}

    def env_for(cid: str) -> CaseEnv:
        if cid not in env_cache:
            if cid not in cases:
                raise PrimaryEvidenceError(f"case {cid!r} is not in case.test_case_set")
            env_cache[cid] = load_case_env(run_dir, cases[cid], schema)
        return env_cache[cid]

    out: list[dict[str, Any]] = []
    for idx, pred in enumerate(preds):
        loc = f"primary_predicates[{idx}]"
        if not isinstance(pred, dict):
            raise PrimaryEvidenceError(f"{loc} must be a mapping")
        test_id = pred.get("test_id")
        quantity = pred.get("quantity")
        op = pred.get("op")
        if not isinstance(test_id, str) or not test_id.strip():
            raise PrimaryEvidenceError(f"{loc}.test_id must be a non-empty string")
        if not isinstance(quantity, str) or not QUANTITY_RE.match(quantity):
            raise PrimaryEvidenceError(f"{loc}.quantity must match {QUANTITY_RE.pattern}")
        if not isinstance(op, str) or op not in PRIMARY_OPS:
            raise PrimaryEvidenceError(f"{loc}.op must be one of {sorted(PRIMARY_OPS)}")
        if "value" not in pred or pred.get("value") is None:
            raise PrimaryEvidenceError(f"{loc} must have a non-null value")
        tree = parse_expr(pred.get("expr"))
        scope, contexts = _predicate_scope(pred, loc)
        targets = [c.strip() for c in pred["target_cases"]]
        record: dict[str, Any] = {
            "test_id": test_id.strip(), "quantity": quantity, "expr": str(pred["expr"]).strip(),
            "op": op, "scope": scope, "grammar_version": GRAMMAR_VERSION,
            "satisfied": True, "kind": "pass", "evaluated": [],
        }
        for cid in contexts:
            try:
                env = env_for(cid)
                at_envs = {t: env_for(t) for t in targets}
                binds = evaluate_binds(pred.get("bind"), env, at_envs)
                value = _scalar(evaluate(tree, env, at_envs=at_envs, binds=binds),
                                f"{loc}.expr")
                present, rhs = _resolve_value(pred.get("value"), cid)
                if not present:
                    raise PrimaryEvidenceError(f"{loc}.value.per_case has no entry for {cid!r}")
                if not _is_number(rhs):
                    raise PrimaryEvidenceError(f"{loc}.value for {cid!r} is not a number")
                ok = _apply_op(value, op, rhs)
                record["evaluated"].append({"case": cid, "value": value, "rhs": rhs,
                                            "satisfied": bool(ok)})
                if not ok:
                    record["satisfied"] = False
                    record["kind"] = "physics"
                    break
            except PrimaryEvidenceError as exc:
                record["evaluated"].append({"case": cid, "satisfied": False,
                                            "reason": "evaluation_error",
                                            "error": str(exc)[:400]})
                record["satisfied"] = False
                record["kind"] = "structural"
                break
        out.append(record)
    return out


# ---------------------------------------------------------------------------------- schema

def validate_primary_predicate_schema(
    predicates: Any,
    *,
    case_ids: set[str],
    test_ids: list[str],
    schema: dict[str, Any] | None,
    cases: dict[str, dict[str, Any]],
) -> list[str]:
    """The Compile-stage gate over `io_contract.primary_predicates` (present-or-absent; the
    per-test coverage rule is a separate gate). Returns violation strings (empty == valid).

    Checks each entry's shape (`test_id` ∈ tests.md, `quantity`, `op`, a non-null `value` of
    the same forms as a `test_predicates` condition, exactly one scope, `target_cases` ⊆
    cases), parses `expr` and every `bind` under the closed grammar, and resolves every name:
    a capture name is a snapshot schema variable or the time variable; an `inputs.<path>` is a
    number in EVERY target case; a bare name is a coordinate, an earlier bind, or a constant;
    an `at('<case>')` case is one of the predicate's own target cases. `coordinates[]` is
    resolved against every declared case, since every case is captured."""
    v: list[str] = []
    if predicates is None:
        return v
    if not isinstance(predicates, list):
        return ["io_contract.primary_predicates must be a list"]
    if schema is None:
        return ["io_contract.primary_predicates requires a state_snapshots required_evidence "
                + "entry (the captures it evaluates)"]
    try:
        variables = schema_variables(schema)
    except PrimaryEvidenceError as exc:
        return [f"io_contract.primary_predicates: snapshot schema unreadable ({exc})"]
    tv = schema.get("time_variable")
    capture_names = set(variables) | ({tv.strip()} if isinstance(tv, str) and tv.strip()
                                      else set())
    coord_names: set[str] = set()
    for cid, case in sorted(cases.items()):
        inputs = case.get("inputs") if isinstance(case.get("inputs"), dict) else {}
        try:
            coord_names |= set(coordinate_arrays(schema, inputs))
        except PrimaryEvidenceError as exc:
            v.append(f"state_snapshots.schema.coordinates does not resolve in case {cid!r}: {exc}")
    md_set = set(test_ids)

    for idx, pred in enumerate(predicates):
        loc = f"primary_predicates[{idx}]"
        if not isinstance(pred, dict):
            v.append(f"{loc} must be a mapping")
            continue
        test_id = pred.get("test_id")
        if not isinstance(test_id, str) or not test_id.strip():
            v.append(f"{loc}.test_id must be a non-empty string")
        elif test_id.strip() not in md_set:
            v.append(f"{loc}.test_id {test_id!r} is not a tests.md test_id")
        quantity = pred.get("quantity")
        if not isinstance(quantity, str) or not QUANTITY_RE.match(quantity):
            v.append(f"{loc}.quantity must match {QUANTITY_RE.pattern}")
        op = pred.get("op")
        if not isinstance(op, str) or op not in PRIMARY_OPS:
            v.append(f"{loc}.op must be one of {sorted(PRIMARY_OPS)}")
        targets_raw = pred.get("target_cases")
        targets = [c.strip() for c in targets_raw
                   if isinstance(c, str) and c.strip()] if isinstance(targets_raw, list) else []
        if not isinstance(targets_raw, list) or not targets_raw or len(targets) != len(targets_raw):
            v.append(f"{loc}.target_cases must be a non-empty list of case_id strings")
        for cid in targets:
            if cid not in case_ids:
                v.append(f"{loc}.target_cases references unknown case_id ({cid!r})")
        if targets:
            try:
                _predicate_scope({**pred, "target_cases": targets}, loc)
            except PrimaryEvidenceError as exc:
                v.append(str(exc))
        if "na_allowed" in pred:
            v.append(f"{loc}: na_allowed has no meaning for a host-evaluated predicate")
        value = pred.get("value")
        if value is None:
            v.append(f"{loc} must have a non-null `value`")
        elif isinstance(value, dict):
            if set(value.keys()) != {"per_case"}:
                v.append(f"{loc}.value must be a number or a {{per_case: {{case_id: value}}}} map")
            elif pred.get("per_case") is not True:
                v.append(f"{loc} has a per_case value map but per_case is not true")
            else:
                table = value["per_case"]
                if not isinstance(table, dict) or not table:
                    v.append(f"{loc}.value.per_case must be a non-empty map")
                else:
                    for cid, tv_ in table.items():
                        if cid not in case_ids:
                            v.append(f"{loc}.value.per_case references unknown case_id ({cid!r})")
                        if not _is_number(tv_):
                            v.append(f"{loc}.value.per_case[{cid!r}] must be a number")
                    uncovered = sorted(c for c in targets if c not in table)
                    if uncovered:
                        v.append(f"{loc}.value.per_case is missing a threshold for target "
                                 f"case(s) {uncovered}")
        elif not _is_number(value):
            v.append(f"{loc}.value must be a number (got {value!r})")

        # Names: the binds first, in order, then `expr`; each expression may reference the
        # binds written before it and nothing written after.
        bind = pred.get("bind")
        bind_names: list[str] = []
        exprs: list[tuple[str, str | None, Any]] = []   # (location, bind name | None, text)
        if bind is not None:
            if not isinstance(bind, dict):
                v.append(f"{loc}.bind must be a mapping of name -> expression")
            else:
                for name, text in bind.items():
                    if not isinstance(name, str) or not name.isidentifier():
                        v.append(f"{loc}.bind name {name!r} is not an identifier")
                        continue
                    if name in CONSTANTS or name in FUNCTIONS or name in CAPTURE_POINTS \
                            or name in (_INPUTS_ROOT, _AT) or name in coord_names:
                        v.append(f"{loc}.bind name {name!r} shadows a grammar name")
                    exprs.append((f"{loc}.bind.{name}", name, text))
                    bind_names.append(name)
        exprs.append((f"{loc}.expr", None, pred.get("expr")))
        seen_binds: set[str] = set()
        for eloc, bind_name, text in exprs:
            try:
                tree = parse_expr(text)
            except PrimaryEvidenceError as exc:
                v.append(f"{eloc}: {exc}")
                if bind_name is not None:
                    seen_binds.add(bind_name)
                continue
            for ref in expr_names(tree):
                if ref.kind == "capture":
                    if ref.name not in capture_names:
                        v.append(f"{eloc}: {ref.point}.{ref.name} is not a snapshot schema "
                                 f"variable ({sorted(capture_names)})")
                    if ref.case is not None and ref.case not in targets:
                        v.append(f"{eloc}: at({ref.case!r}) is not one of this predicate's "
                                 f"target_cases ({sorted(targets)})")
                elif ref.kind == "inputs":
                    for cid in targets:
                        case = cases.get(cid)
                        inputs = case.get("inputs") if isinstance(case, dict) else None
                        try:
                            resolve_input_number(inputs, ref.name)
                        except PrimaryEvidenceError as exc:
                            v.append(f"{eloc}: in case {cid!r}: {exc}")
                elif ref.name in seen_binds or ref.name in coord_names or ref.name in CONSTANTS:
                    continue
                elif ref.name in bind_names:
                    v.append(f"{eloc}: bind {ref.name!r} is referenced before it is defined")
                else:
                    v.append(f"{eloc}: name {ref.name!r} is not a coordinate, a bind or a "
                             "constant")
            if bind_name is not None:
                seen_binds.add(bind_name)
    return v


# ------------------------------------------------------------------------------------- CLI

def _read_ir(path: Path) -> dict[str, Any]:
    import yaml  # the host reader; PyYAML is a runtime dependency of the conductor
    target = path / "spec.ir.yaml" if path.is_dir() else path
    doc = yaml.safe_load(target.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise SystemExit(f"{target}: not a mapping")
    return doc


def main(argv: list[str] | None = None) -> int:
    """Read-only: evaluate an IR's primary predicates against a recorded run node directory
    and print the records as JSON. The offline measurement the plan of issue #255 names."""
    parser = argparse.ArgumentParser(
        prog="python3 -m tools.primary_evidence",
        description="Evaluate io_contract.primary_predicates against a run's captures.")
    parser.add_argument("--ir", required=True, type=Path,
                        help="the IR directory (holding spec.ir.yaml) or the file itself")
    parser.add_argument("--run", required=True, type=Path,
                        help="the run node directory holding raw/state_snapshots/")
    args = parser.parse_args(argv)
    ir = _read_ir(args.ir)
    try:
        records = evaluate_primary_predicates(ir, args.run)
    except PrimaryEvidenceError as exc:
        print(json.dumps({"error": str(exc)}, indent=2))
        return 2
    print(json.dumps(records, indent=2, ensure_ascii=False))
    return 0 if all(r["satisfied"] for r in records) else 1


if __name__ == "__main__":
    sys.exit(main())
