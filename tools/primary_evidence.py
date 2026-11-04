#!/usr/bin/env python3
"""Host-evaluated predicates (Z6, issue #255; issue #582; `zero_base_architecture.md` §A4).

An `io_contract.predicates[]` entry is an arithmetic expression over the state the host-rendered
runner captured through the certified harness emitters (`raw/state_snapshots/initial/<case_id>.json`
right after `case_setup`, `raw/state_snapshots/<case_id>.json` right after `case_run`), over the
case's declared inputs, and over grid coordinates the host derives from those inputs — evaluated
HERE, by the host, with the generated code contributing nothing to the value but the
captured arrays themselves and the capture's time (`initial.t` / `final.t` is what the
generated `get_time` reported; an expression that needs the case's end time reads the
declared `inputs.time.t_end`). The LLM writes the expression at Compile; no LLM holds the
number. These predicates alone decide a test (issue #582, `tools/verdict_evaluator.py`).

**What a predicate may count as a state read** (grammar 6, issue #589) is derived from the IR's
`drive` block and the published operations' §5.1 signatures (`drive_model`), never from a label the
leaf writes: `final.<v>` of a variable an operation WRITES (an `out` / `inout` dummy of `init` or of a
`step` entry, every logical result included), `initial.<v>` of a variable `init` writes,
`history.*` and `series.*`. `initial.<v>` of a variable the host materialised from the case's
`initial_state`, `final.<v>` of a variable nothing writes, `final.step` / `final.t` (the driver's),
`derived.*` and `inputs.*` are not (`predicate_reads_state`). An IR with no `drive` (an
`infrastructure` node) counts every schema variable at both capture points, as grammar 5 did. One
class of kernel-written scalar remains judged at all — the scalar a rank-0 real / integer `out` of
a one-call operation writes — and a predicate reading it stands beside one that reads it TOGETHER
with an array state the same operation writes (`scalar_pairing_violations`, D7; each record carries
`scalar_pairing`, which `verdict_evaluator.evaluate_verdict` re-checks).

The host also EVALUATES each case's inputs (`evaluate_case_inputs` / `write_case_inputs`, issue
#589 D2): the state extents from the bound dummies' `dims`, every `initial_state` entry and every
`derived` entry, with the predicate grammar, into the case file the host-rendered driver reads.

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
      predicates:
        - test_id: <a tests.md test_id>
          quantity: <name>                       # the quantity the expression evaluates
          target_cases: [<case_id>, ...]
          bind: {M0: "sum(initial.h) * inputs.grid.dx", ...}   # optional named sub-expressions
          expr: "abs(M1 - M0) / max(abs(M0), 1e-14)"
          op: le                                 # eq | ne | le | ge | lt | gt
          value: 1.0e-10                         # OR {per_case: {<case_id>: v}}
          per_case: true                         # exactly one of per_case: true / case: <id>

**The grammar is closed** (`GRAMMAR_VERSION`): a Python expression restricted, by an allowlist
over `ast` node types, to the arithmetic operators `+ - * / **`, unary `-`, finite numeric
constants, the names below, and calls to the functions of `FUNCTIONS` (positional arguments
only, within each function's arity). A subscript, a
slice, a comparison, a boolean operator, a lambda, a keyword argument, a starred argument,
a string outside the one place a string is admitted (`at('<case_id>')`), an attribute of
anything but the roots listed here — each is refused at parse, never evaluated. Adding a
function or a name root is a grammar change and bumps `GRAMMAR_VERSION`.

Names, and where each resolves:

- ``initial.<var>`` / ``final.<var>`` — a snapshot schema variable at the two capture points
  (a scalar `shape_expr` gives a float, an array one a float64 array of that rank);
  ``initial.<time_variable>`` / ``final.<time_variable>`` — the capture's time value;
  ``initial.step`` / ``final.step`` (grammar 6) — the driver's committed step count.
- ``history.<var>`` (grammar 6) — the samples `raw/state_snapshots/history/<case_id>__<k>.json`,
  `k` ascending, stacked on a LEADING axis (rank of the variable + 1); ``history.t`` /
  ``history.step`` rank 1. ``series.<name>`` — a rank-1 entry of
  `raw/state_snapshots/series/<case_id>.json`, a name the `step_series` evidence entry lists. Both
  are loaded only when a predicate names them; zero samples / zero committed steps is admitted,
  a non-finite entry is refused, and a reduction over an empty one is an evaluation error of that
  predicate. ``derived.<name>`` — the case's `derived` value as the host evaluated it
  (`evaluate_case_inputs`): host data, never a state read. None of the three combines with
  `at('<case>')` or `comparand`.
- ``smax(a)`` / ``smin(a)`` / ``ssum(a)`` reduce every axis of ``a`` but the leading one (the
  identity on rank 1); a coordinate NAMED directly as an operand of a binary operator broadcasts
  against an operand of one more rank (a `history.*` one) on its trailing axes.
- ``inputs.<a>.<b>...`` — a NUMERIC value of the case's `case.test_case_set[].inputs`: a
  number, or (grammar 2) a rectangular nested list of numbers of rank 1 to 4, as a float64
  array — a declared sentinel field a capture is compared against. A string, a boolean, a
  mapping, an empty or ragged list, or a list holding a non-number is refused at name
  resolution.
- ``<coordinate name>`` — a `schema.coordinates[]` axis, as a float64 array of the STATE's
  shape carrying the axis's `count` cell-centre positions along `axis` (the same value on
  every other axis), so a field built from it — an analytic reference — has the state's
  shape and reduces (`norm2`, `sum`, `mean`) over the same cells. The state's rank is the
  highest rank among the schema variables, and `axis` indexes it; a coordinate therefore
  pairs only with a variable of that rank (issue #440: beside a stacked `[ncomp, nx, ny]`
  state a rank-2 `[nx, ny]` capture never pairs with one). The state's shape is the
  one shape every captured array of the state's rank has; when those disagree the
  coordinate keeps extent 1 on the other axes, and a reduction over such a field is refused
  until it is paired with a state array.
- ``at('<case_id>').initial.<var>`` / ``at('<case_id>').final.<var>``,
  ``at('<case_id>').inputs.<a>.<b>``, ``at('<case_id>').<coordinate name>`` — the same three
  kinds of value read in another case of the SAME predicate's `target_cases` (a cross-case
  reduction: a convergence order needs the coarse case's state, coordinates and inputs; a
  translation pair needs the base case's state).
- ``comparand.initial.<var>`` / ``comparand.final.<var>`` (grammar 3, issue #324) — the same
  capture read in the same case of ANOTHER TARGET's certified variant of this node: the
  comparand run the host selected (`orchestration_runtime.resolve_comparands`). A predicate
  that names one is a CROSS-TARGET predicate: it is evaluated once per comparand, it must read
  this run's own state too (a comparison of the comparand with a constant values nothing this
  run produced), and it judges no quantity of the test's `quantities` line (`quantity_violations`
  does not count it), because a run's own verdict never evaluates it. Since issue #383 a cross-target predicate is
  not part of any run's verdict: `verdict.json` carries a `no_comparand` placeholder for it,
  and the comparison is the cross-target agreement, evaluated on demand
  (`orchestration_runtime.cross_target_agreement`) wherever "is this node ready" is asked. `comparand.inputs` / `comparand.<coordinate>`
  are not names (the comparand shares this run's IR, so they would be this case's own), and
  `comparand` does not combine with `at('<case>')` in either order.
- a `bind` name — a named sub-expression, evaluated in `bind` order; a bind may reference
  only the binds written before it (acyclic by construction).
- ``pi`` / ``e``.

Every intermediate result must be finite (a division by zero, an overflow, a literal
`1e400`), and the predicate's result must be a finite SCALAR; the operands of a binary
operator or of an elementwise `min` / `max` must be a scalar and anything, or two arrays of
equal rank whose extents agree or are 1 on one side (a coordinate broadcasts against a state
array; a `[nx]` array never silently pairs with a `[nx, ny]` one, and a 32×32 state never
pairs with a 64×64 one). Any of these — and an unallocated, ragged, non-numeric,
non-finite or wrong-rank snapshot array, a `roll` shift that is not an integer, a `flip` or
`transpose` axis outside the array's rank, a `coarsen` factor that does not divide every
extent, a case whose capture file is absent — raises `PrimaryEvidenceError`, which `evaluate_primary_predicates`
records on that predicate as a STRUCTURAL failure (the evidence could not be judged) and
`verdict_evaluator.evaluate_verdict` folds into `structural_violation`. `evaluate` also
converts every arithmetic exception Python or numpy can raise on admitted operands into that
error, so no evaluation escapes the per-predicate record.

The Compile gate infers RANKS statically (`expr_rank`, issue #440) from the declared
`shape_expr`s, the state's rank and each case's inputs, so an operand pair of unequal rank, a
`roll` with the wrong shift count or a non-scalar shift, a `transpose` / `flip` / `coarsen` of
a scalar or with a non-scalar axis or factor, a `transpose` given one axis, and an
array-valued `expr` are refused before any run — each but the last with the message
evaluation would raise, the last naming the rank where `_scalar` names the shape; the gate prefixes the location, names the case when the
refusal is not the same in every case, and adds the coordinate's rank to an operand-pair
refusal of a predicate that names a coordinate. Extents are not declared numerically, so an extent
mismatch stays an evaluation error.

A predicate ranges over EXACTLY the `target_cases` of its `io_contract.tests` entry — the gate
pins set equality and `evaluate_verdict` re-checks it — so a predicate cannot quietly cover the
easiest case of a test alone.

Pure with respect to the conductor: reads the run node directory it is given, and the
comparand run node directories it is handed, and nothing else, like
`tools/raw_evidence_excerpt.py`. `evaluate_verdict` never reads a file; it takes
the list this module returns.
"""

from __future__ import annotations

import argparse
import ast
import copy
import json
import math
import os
import re
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, NamedTuple

import numpy as np

from tools.verdict_evaluator import (_QUANTITY_RE, _apply_op, _is_number, _resolve_value,
                                     scalar_pairs)

#: The version of the expression grammar: the allowed `ast` nodes, the function table, the
#: name roots and the broadcasting rule. Bumped when any of them changes; recorded on every
#: evaluated predicate so a verdict says which grammar valued it. Grammar 4 (issue #327) adds
#: `atan2`: a discrete mode's phase, which a predicate of `G**n_step` needs. Grammar 5 (issue
#: #513) adds `transpose`, `flip` and `coarsen`: a symmetry pair reads its base case's state
#: through the axis exchange or a mirror, and a refinement family judged against its finest
#: level restricts that level to each coarser grid by the block mean. Grammar 6 (issue #589)
#: adds the `history.*`, `series.*` and `derived.*` roots, `step` as a capture name, `smax` /
#: `smin` / `ssum`, and the coordinate broadcast against a `history.*` operand.
GRAMMAR_VERSION = 6

#: `quantity` names: lowercase identifiers with dots.
#: ONE definition, in the verdict evaluator's module.
QUANTITY_RE = _QUANTITY_RE

#: The driver's committed step count, a capture name beside the time variable (grammar 6).
STEP_CAPTURE = "step"
#: Names a snapshot schema variable may not take: the driver's and the harness's own keys.
RESERVED_VARIABLE_NAMES: frozenset[str] = frozenset({"t", STEP_CAPTURE, "case_id"})

#: The comparison operators a predicate admits.
PRIMARY_OPS: frozenset[str] = frozenset({"eq", "ne", "le", "ge", "lt", "gt"})

#: The one coordinate placement the host derives in this grammar version: cell centres of a
#: uniform axis, `(i + 1/2) * length / count`.
COORDINATE_PLACEMENTS: frozenset[str] = frozenset({"cell_center"})

#: name -> (min_args, max_args). `min` / `max` reduce with ONE argument and are elementwise
#: with two or more. `roll` takes the array and one integer shift per axis, leading axes
#: first. `atan2(y, x)` is elementwise, in `[-pi, pi]`. `transpose(a)` reverses the order of
#: the array's axes, and `transpose(a, i, j)` exchanges axes `i` and `j` alone (a stacked
#: `[ncomp, nx, ny]` state is exchanged with `transpose(a, 1, 2)`); `flip(a, k)` reverses axis
#: `k` (an integer scalar, leading axis `0`);
#: `coarsen(a, f)` is the mean over each block of `f` cells along every axis (`f` an integer
#: scalar `>= 1` dividing every extent). This table is the whole function vocabulary.
FUNCTIONS: dict[str, tuple[int, int]] = {
    "sum": (1, 1), "mean": (1, 1), "min": (1, 8), "max": (1, 8), "abs": (1, 1),
    "sqrt": (1, 1), "exp": (1, 1), "log": (1, 1), "log2": (1, 1), "sin": (1, 1),
    "cos": (1, 1), "norm2": (1, 1), "maxabs": (1, 1), "roll": (2, 5), "ceil": (1, 1),
    "floor": (1, 1), "atan2": (2, 2), "transpose": (1, 3), "flip": (2, 2), "coarsen": (2, 2),
    "smax": (1, 1), "smin": (1, 1), "ssum": (1, 1),
}
CONSTANTS: dict[str, float] = {"pi": math.pi, "e": math.e}
#: Name roots an attribute chain may start from. `at` is a call, not a root.
CAPTURE_POINTS: tuple[str, str] = ("initial", "final")
_INPUTS_ROOT = "inputs"
_AT = "at"
#: The root of a cross-target reference (grammar 3): `comparand.initial.<var>` /
#: `comparand.final.<var>`.
_COMPARAND = "comparand"
#: The grammar 6 roots: the sampled state, the per-step series, the case's derived values.
_HISTORY = "history"
_SERIES = "series"
_DERIVED = "derived"
_SAMPLED_ROOTS: tuple[str, ...] = (_HISTORY, _SERIES, _DERIVED)
#: Every identifier the grammar reserves as a root; a coordinate or a bind may not take one.
_ROOTS: tuple[str, ...] = CAPTURE_POINTS + (_INPUTS_ROOT, _AT, _COMPARAND) + _SAMPLED_ROOTS

_BINOPS: dict[type, str] = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/",
                            ast.Pow: "**"}

#: The deepest expression tree `parse_expr` admits. Evaluation recurses once per node, so a
#: tree this deep never approaches the interpreter's recursion limit at any call site (the
#: conductor's own stack included); a 990-deep unary chain used to pass parse and recurse
#: out of `evaluate` (round 3). Far above any expression a test's judgment needs.
MAX_EXPR_DEPTH = 64


class PrimaryEvidenceError(ValueError):
    """A primary predicate could not be parsed, resolved or evaluated."""


# --------------------------------------------------------------------------------- grammar

class NameRef(NamedTuple):
    """One name an expression references, as the resolver sees it.

    ``kind`` is ``capture`` (``initial.<var>`` / ``final.<var>``), ``comparand``
    (``comparand.initial.<var>`` / ``comparand.final.<var>``: the same capture in the
    comparand run), ``inputs`` (``name`` is the dotted path under ``inputs``), ``history`` /
    ``series`` / ``derived`` (grammar 6: ``name`` follows the root), or ``name`` (a
    bare identifier: a coordinate, a bind or a constant). ``case`` is None for the predicate's
    own case, else the ``at('<case>')`` case; under ``at()`` a ``name`` is a coordinate only,
    and a ``comparand`` reference never has one."""
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


def _check_node(node: ast.AST, refs: list[NameRef], depth: int = 0) -> None:
    """Walk one expression node against the allowlist, collecting the names it references.
    Raises `PrimaryEvidenceError` on the first node outside the grammar or deeper than
    `MAX_EXPR_DEPTH`."""
    if depth > MAX_EXPR_DEPTH:
        raise PrimaryEvidenceError(
            f"expr is nested deeper than {MAX_EXPR_DEPTH} levels")
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
        _check_node(node.left, refs, depth + 1)
        _check_node(node.right, refs, depth + 1)
        return
    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, ast.USub):
            raise PrimaryEvidenceError(f"unary {type(node.op).__name__} is not admitted")
        _check_node(node.operand, refs, depth + 1)
        return
    if isinstance(node, ast.Name):
        if node.id in _ROOTS:
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
        if isinstance(root, ast.Name) and root.id == _COMPARAND:
            if len(attrs) != 2 or attrs[0] not in CAPTURE_POINTS:
                raise PrimaryEvidenceError(
                    f"{_COMPARAND}.{'.'.join(attrs)}: a cross-target reference is exactly "
                    f"{_COMPARAND}.initial.<variable> or {_COMPARAND}.final.<variable> (the "
                    "comparand shares this run's IR, so its inputs and coordinates are this "
                    "case's own)")
            refs.append(NameRef("comparand", attrs[1], point=attrs[0]))
            return
        if isinstance(root, ast.Name) and root.id == _INPUTS_ROOT:
            refs.append(NameRef("inputs", ".".join(attrs)))
            return
        if isinstance(root, ast.Name) and root.id in _SAMPLED_ROOTS:
            if len(attrs) != 1:
                raise PrimaryEvidenceError(
                    f"{root.id}.{'.'.join(attrs)}: a {root.id} reference is exactly "
                    f"{root.id}.<name>")
            refs.append(NameRef(root.id, attrs[0]))
            return
        if _is_at_call(root):
            call = root  # type: ignore[assignment]
            assert isinstance(call, ast.Call)
            if (len(call.args) != 1 or call.keywords
                    or not isinstance(call.args[0], ast.Constant)
                    or not isinstance(call.args[0].value, str)
                    or not call.args[0].value.strip()):
                raise PrimaryEvidenceError("at(...) takes exactly one case_id string")
            case_id = call.args[0].value
            if case_id != case_id.strip():
                raise PrimaryEvidenceError(
                    f"at({case_id!r}): a case_id carries no surrounding whitespace")
            if attrs and attrs[0] == _COMPARAND:
                raise PrimaryEvidenceError(
                    f"at({case_id!r}).{'.'.join(attrs)}: a cross-target reference does not "
                    "combine with at('<case_id>'); it reads the comparand in the case being "
                    "evaluated")
            if len(attrs) == 2 and attrs[0] in CAPTURE_POINTS:
                refs.append(NameRef("capture", attrs[1], point=attrs[0], case=case_id))
                return
            if len(attrs) >= 2 and attrs[0] == _INPUTS_ROOT:
                refs.append(NameRef("inputs", ".".join(attrs[1:]), case=case_id))
                return
            if len(attrs) == 1 and attrs[0] not in _ROOTS:
                refs.append(NameRef("name", attrs[0], case=case_id))
                return
            raise PrimaryEvidenceError(
                f"at({case_id!r}).{'.'.join(attrs)}: a cross-case reference is exactly "
                "at('<case_id>').initial.<variable>, at('<case_id>').final.<variable>, "
                "at('<case_id>').inputs.<path> or at('<case_id>').<coordinate> (history.*, "
                "series.* and derived.* are read in the case being evaluated only)")
        raise PrimaryEvidenceError(
            f"attribute chain rooted at {ast.dump(root)} is not admitted (roots: "
            f"{', '.join(CAPTURE_POINTS)}, {_INPUTS_ROOT}, {', '.join(_SAMPLED_ROOTS)}, "
            f"at('<case_id>'), {_COMPARAND})")
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
            _check_node(arg, refs, depth + 1)
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
    except (RecursionError, MemoryError):
        # `ast.parse` itself gives up on a chain of some thousands of operators (measured on
        # 3.10: RecursionError, then MemoryError past ~6000 unary minuses)
        raise PrimaryEvidenceError("expr is nested too deeply to parse") from None
    # `_check_node` recurses once per level and refuses past MAX_EXPR_DEPTH, so it cannot
    # reach the interpreter's limit: no guard around it.
    _check_node(tree.body, [])
    return tree


def expr_names(tree: ast.Expression) -> list[NameRef]:
    """Every name reference in a parsed expression, in source order."""
    refs: list[NameRef] = []
    _check_node(tree.body, refs)
    return refs


def _is_literal_zero(node: ast.AST) -> bool:
    """A numeric literal equal to zero, under any number of unary minuses (`0`, `0.0`, `-0`)."""
    while isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        node = node.operand
    return (isinstance(node, ast.Constant) and not isinstance(node.value, bool)
            and isinstance(node.value, (int, float)) and node.value == 0)


def _negates(node: ast.AST, other: ast.AST) -> bool:
    """`node` is `-X` with `X` structurally identical to `other`."""
    return (isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub)
            and ast.dump(node.operand) == ast.dump(other))


class _ConstantByFormPruner(ast.NodeTransformer):
    """Replace, children first, every term whose value is fixed by its FORM: `X * 0` / `0 * X`
    and `X - X` / `X + -X` / `-X + X` (structurally identical sides, after `-(-X)` is collapsed
    to `X`) by the literal 0, `X ** 0` by the literal 1. Sides equal only after commuting or
    re-associating, and literal arithmetic other than these forms (`0 + 0`), are not folded.
    Each is exact for every finite evaluation, and the evaluator refuses a non-finite value.
    Nothing else is folded — `0 / X`, `0 ** X` and `X / X` are not exact."""

    def visit_UnaryOp(self, node: ast.UnaryOp) -> ast.AST:
        self.generic_visit(node)
        if isinstance(node.op, ast.USub) and isinstance(node.operand, ast.UnaryOp) \
                and isinstance(node.operand.op, ast.USub):
            return node.operand.operand
        return node

    def visit_BinOp(self, node: ast.BinOp) -> ast.AST:
        self.generic_visit(node)
        if isinstance(node.op, ast.Mult) and (_is_literal_zero(node.left)
                                              or _is_literal_zero(node.right)):
            return ast.Constant(0.0)
        if isinstance(node.op, ast.Pow) and _is_literal_zero(node.right):
            return ast.Constant(1.0)
        if isinstance(node.op, ast.Sub) and ast.dump(node.left) == ast.dump(node.right):
            return ast.Constant(0.0)
        if isinstance(node.op, ast.Add) and (_negates(node.left, node.right)
                                             or _negates(node.right, node.left)):
            return ast.Constant(0.0)
        return node


def effective_names(tree: ast.Expression) -> list[NameRef]:
    """The name references of a parsed expression that survive pruning the terms constant by
    form (`_ConstantByFormPruner`): a read that sits only inside such a term does not enter
    the value. `tree` is not modified."""
    return expr_names(_ConstantByFormPruner().visit(copy.deepcopy(tree)))


class StateReads(NamedTuple):
    """Which capture reads value the kernel's state (grammar 6, issue #589): ``final`` — the
    variables whose `final.<v>` counts (those an operation writes, every logical result
    included), ``initial`` — those whose `initial.<v>` counts (those `init` writes). `history.*`
    and `series.*` always count. ``loop`` — the IR declares `drive.loop`, so gate (l)'s
    zero-step closure `final.step - <ref>` is exempt from the reads-a-capture rule
    (`is_zero_step_closure`). Built by `state_reads`; a plain ``set`` of names in its place is
    the pre-drive rule (both points, every listed variable), which an IR with no `drive` keeps."""
    final: frozenset[str]
    initial: frozenset[str]
    loop: bool = False


def is_state_read(ref: NameRef, state: StateReads | set[str] | frozenset[str]) -> bool:
    """Whether one reference reads kernel state under ``state`` (`StateReads`, or a set of
    names counted at both capture points). `history.*` / `series.*` always do; a time or step
    capture, an input, a coordinate, a `derived.*` value and a `comparand.` read never do."""
    if ref.kind in (_HISTORY, _SERIES):
        return True
    if ref.kind != "capture":
        return False
    if isinstance(state, StateReads):
        return ref.name in (state.final if ref.point == "final" else state.initial)
    return ref.name in state


def reads_captured_state(refs: list[NameRef],
                         state_names: StateReads | set[str] | frozenset[str]) -> bool:
    """Whether any reference reads kernel state (`is_state_read`; the time variable and the
    step count excluded)."""
    return any(is_state_read(r, state_names) for r in refs)


#: Gate (l)'s zero-step closure on a `loop` node, matched on the expression TEXT with every
#: whitespace removed: `final.step - <ref>`.
_ZERO_STEP_RE = re.compile(
    r"^final\.step-(?P<ref>(?:derived|initial|final)\.[A-Za-z_][A-Za-z0-9_]*"
    r"|inputs\.[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)$")


def zero_step_closure_ref(pred: Any) -> str | None:
    """The `<ref>` of a predicate in gate (l)'s fixed form on a `loop` node — `expr:
    "final.step - <ref>"` (whitespace aside), `op: eq`, a numeric `value` equal to 0, no
    `bind` — else None. Whether the `<ref>` names an admitted value is gate (l)'s question."""
    if not isinstance(pred, dict) or pred.get("op") != "eq" or pred.get("bind"):
        return None
    value = pred.get("value")
    if not _is_number(value) or value != 0:
        return None
    expr = pred.get("expr")
    match = _ZERO_STEP_RE.match(re.sub(r"\s+", "", expr)) if isinstance(expr, str) else None
    return match.group("ref") if match else None


def is_zero_step_closure(pred: Any, state: StateReads | set[str] | frozenset[str]) -> bool:
    """Whether ``pred`` is gate (l)'s zero-step closure on a node whose `drive` declares a
    `loop` — gate-required, and exempt from the reads-a-capture rule (it reads the driver's
    `final.step`, which no kernel writes)."""
    return isinstance(state, StateReads) and state.loop \
        and zero_step_closure_ref(pred) is not None


def predicate_reads_state(expr_refs: list[NameRef], bind_refs: dict[str, list[NameRef]],
                          state_names: StateReads | set[str] | frozenset[str]) -> bool:
    """Whether a predicate's `expr`, or a `bind` that `expr` reaches (transitively), reads
    kernel state (`is_state_read` under ``state_names``). A predicate that reads none — a
    constant, an input, a `derived.*` value, the time or the step count alone, the host's own
    `initial_state` read back, or a state read only in a bind the expression never uses —
    values nothing the kernel produced and judges nothing; the gate and the evaluator both
    refuse it, gate (l)'s zero-step closure aside (`is_zero_step_closure`).

    SYNTACTIC: the rule sees that the state is read, not that the value depends on it. The
    schema gate first prunes the terms constant by form (`effective_names`: a factor of
    literal zero, an exponent of literal zero, a side subtracted from itself or added to its
    own negation), so
    `sum(final.h) * 0` is refused there (issue #483); the evaluator applies this rule to the
    raw references, and a gated IR never reaches it with such a term. Dependence in any
    other way, and whether the expression is the test's own quantity, is `Compile.verify`
    V3's judgment (`phase_01_compile.md`); the gate over `quantity` names
    (`quantity_violations`) closes omission, not vacuity."""
    if reads_captured_state(expr_refs, state_names):
        return True
    return any(reads_captured_state(bind_refs[name], state_names)
               for name in reached_binds(expr_refs, bind_refs))


def reached_binds(expr_refs: list[NameRef], bind_refs: dict[str, list[NameRef]]) -> set[str]:
    """The names of the binds `expr` reaches: those its references name, and, transitively,
    those a reached bind's references name. A bind outside this set enters no value of the
    predicate. The frontier is built from `expr_refs` as given, so a caller that passes
    `effective_names` references follows no bind named only inside a term constant by form.
    Shared by the reads-state rule (`predicate_reads_state`) and the cases a predicate values
    (`cases_read`, issue #567)."""
    reached: set[str] = set()
    frontier = [r.name for r in expr_refs if r.kind == "name" and r.name in bind_refs]
    while frontier:
        name = frontier.pop()
        if name in reached:
            continue
        reached.add(name)
        frontier.extend(r.name for r in bind_refs[name]
                        if r.kind == "name" and r.name in bind_refs)
    return reached


def predicate_reads_comparand(expr_refs: list[NameRef],
                              bind_refs: dict[str, list[NameRef]]) -> bool:
    """Whether a predicate reads a `comparand.` capture ANYWHERE — in `expr` or in any `bind`,
    reached or not — i.e. whether it is a CROSS-TARGET predicate. Unlike `predicate_reads_state`
    this does not follow reachability: every bind of a predicate is evaluated, so a bind that
    reads the comparand needs one bound whether `expr` uses it or not."""
    return any(r.kind == "comparand" for refs in (expr_refs, *bind_refs.values()) for r in refs)


def _predicate_refs(pred: dict[str, Any]) -> tuple[list[NameRef], dict[str, list[NameRef]]]:
    """`(expr refs, bind name -> refs)` of one predicate; raises `PrimaryEvidenceError` on an
    expression outside the grammar. A `bind` that is not a mapping contributes nothing here:
    the gate refuses it, and `evaluate_binds` records it on the predicate per case."""
    bind = pred.get("bind")
    bind_refs = ({str(k): expr_names(parse_expr(t)) for k, t in bind.items()}
                 if isinstance(bind, dict) else {})
    return expr_names(parse_expr(pred.get("expr"))), bind_refs


def is_cross_target_predicate(pred: Any) -> bool:
    """Whether one `predicates[]` entry is a cross-target predicate. A malformed
    entry (not a mapping, an expression outside the grammar) answers False here: the Compile
    gate refuses it, and `evaluate_primary_predicates` raises on it."""
    if not isinstance(pred, dict):
        return False
    try:
        expr_refs, bind_refs = _predicate_refs(pred)
    except PrimaryEvidenceError:
        return False
    return predicate_reads_comparand(expr_refs, bind_refs)


def cross_target_predicates(ir: dict[str, Any]) -> list[dict[str, Any]]:
    """The cross-target entries of an IR's `io_contract.predicates`, in order. A node
    is asked for its cross-target agreement exactly when this is non-empty (issue #383)."""
    return [p for p in primary_predicates(ir) if is_cross_target_predicate(p)]


class ComparandEvidence(NamedTuple):
    """One comparand of a cross-target agreement: another target's eligible run of the same
    node over the same IR. `run_dir` is the run node directory (absolute) the captures are
    read from; `evidence` is `comparand_evidence_sha256(run_dir)` at selection time — a record
    of which bytes the agreement read, not an input of any key."""
    target_id: str
    pipeline_ref: str
    run_id: str
    run_dir: Path
    evidence: str

    def detail(self) -> dict[str, str]:
        return {"target_id": self.target_id, "pipeline_ref": self.pipeline_ref,
                "run_id": self.run_id, "evidence": self.evidence}


class ComparandResolution(NamedTuple):
    """The answer of `orchestration_runtime.resolve_comparands` for one node on one target
    (issue #345): `comparands`, the other declared targets whose eligible variant is read,
    and `absent`, one `{"target_id", "reason"}` per other declared target that contributed
    nothing, in target-id order. Every other declared target is in exactly one of the two.
    `reason` is the comparand resolver's Build refusal — the first refusal along B's chain up
    to Build (`binary_not_found`, `derivation_key_mismatch:<input>` of the refusing phase —
    `derivation_key_mismatch:ir` from B's Generate when B was generated from an IR other than
    the node's standing certified one —, `revoked`, ...) — `compile_ir_mismatch` (B stands on
    the standing IR and the IR this run validates is not it) or `no_eligible_run`. The record is
    as of this resolution; nothing of it is keyed (issue #383)."""
    comparands: list[ComparandEvidence]
    absent: list[dict[str, str]]


class CrossTargetAgreement(NamedTuple):
    """The answer of `orchestration_runtime.cross_target_agreement` for one node on one target
    (issue #383): whether its certified Validate run agrees with every other target's eligible
    run under the IR's cross-target predicates, evaluated NOW.

    `status` is `not_applicable` (the IR declares no cross-target predicate), `no_comparand`
    (no other target has an eligible run), `agree`, `disagree` or `unevaluable`; only the last
    two make a node not ready (`orchestration_runtime.cross_target_not_ready_reason`).
    `comparands` are the other targets' runs read
    (`ComparandEvidence.detail`), `absent` the other declared targets that contributed none and
    why (`ComparandResolution.absent`), `disagreeing` the target ids a comparison failed
    against (`physics` — a value or a state shape that disagrees), `records` the cross-target
    records `evaluate_primary_predicates(..., cross_target_only=True)` returned,
    `own_run` `{pipeline_ref, run_id, evidence}` of this target's run, and `error` what made the
    comparison `unevaluable` (an exception, or a `structural` record), else None."""
    status: str
    comparands: list[dict[str, str]]
    absent: list[dict[str, str]]
    disagreeing: list[str]
    records: list[dict[str, Any]]
    own_run: dict[str, str] | None
    error: str | None

    def document(self) -> dict[str, Any]:
        return {"status": self.status, "comparands": list(self.comparands),
                "absent": list(self.absent), "disagreeing": list(self.disagreeing),
                "records": list(self.records), "own_run": self.own_run, "error": self.error}


#: The host-authored file under `raw/state_snapshots/` that is not a capture: the snapshot
#: schema the conductor writes from the IR. Excluded from the comparand evidence hash — it is
#: not state the comparand's kernel produced.
SNAPSHOT_SCHEMA_FILENAME = "snapshot_schema.json"


def comparand_evidence_sha256(run_dir: Path) -> str:
    """The identity of a comparand run's captures: sha256 over every `*.json` under
    `run_dir/raw/state_snapshots/` (recursively, `snapshot_schema.json` excluded), in order of
    the path relative to that directory, each contributing `<path>\\0<byte length>\\0<bytes>`.
    Raises `PrimaryEvidenceError` when there is no capture at all."""
    import hashlib
    sdir = Path(run_dir) / "raw" / "state_snapshots"
    files = sorted(
        (p.relative_to(sdir).as_posix(), p) for p in (sdir.rglob("*.json") if sdir.is_dir()
                                                       else ())
        if p.is_file() and p.relative_to(sdir).as_posix() != SNAPSHOT_SCHEMA_FILENAME)
    if not files:
        raise PrimaryEvidenceError(f"{sdir}: no state snapshot capture to bind as a comparand")
    h = hashlib.sha256()
    for rel, path in files:
        data = path.read_bytes()
        h.update(rel.encode("utf-8") + b"\0" + str(len(data)).encode("ascii") + b"\0")
        h.update(data)
    return "sha256:" + h.hexdigest()


# ----------------------------------------------------------------------------- environment

class Lazy:
    """A value computed on first use and kept, error included: a `history/` or `series/`
    loader, or the case's evaluated `derived` values, which only a predicate naming them pays
    for — and whose failure is an evaluation error of THAT predicate alone."""

    def __init__(self, thunk: Callable[[], Any]) -> None:
        self._thunk: Callable[[], Any] | None = thunk
        self._value: Any = None
        self._error: str | None = None

    def get(self) -> Any:
        if self._thunk is not None:
            thunk, self._thunk = self._thunk, None
            try:
                self._value = thunk()
            except PrimaryEvidenceError as exc:
                self._error = str(exc)
        if self._error is not None:
            raise PrimaryEvidenceError(self._error)
        return self._value


def _table(value: Any) -> Any:
    return value.get() if isinstance(value, Lazy) else value


class CaseEnv(NamedTuple):
    """Everything an expression can name inside one case. A capture table holds the declared
    variables the capture file carries; a variable it omits, and every ``initial.<var>`` of a
    case with no ``initial/`` capture (``initial`` None — a harness self-test's own runner
    writes none), is an evaluation error of the predicate that names it. ``history`` /
    ``series`` / ``derived`` (grammar 6) are a mapping or a `Lazy` of one, None when nothing
    can answer them; ``state_rank`` is the state's rank (the coordinates' rank), None when not
    known, and lets a reduction tell a sampled field's leading axis from a state axis."""
    case_id: str
    initial: dict[str, Any] | None   # variable -> float | ndarray (the time variable included)
    final: dict[str, Any]
    inputs: dict[str, Any]      # the case's `inputs` mapping, as authored
    coordinates: dict[str, np.ndarray]
    history: Any = None
    series: Any = None
    derived: Any = None
    state_rank: int | None = None


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


#: The deepest nested list `resolve_input_value` admits: the rank the harness emitters and
#: `roll` cover.
MAX_INPUT_RANK = 4


def resolve_input_value(inputs: Any, dotted: str) -> Any:
    """The value at ``inputs.<dotted>`` as a float or a float64 array: a number, or a
    non-empty rectangular nested list of numbers of rank 1..`MAX_INPUT_RANK`. Raises when
    the key is absent or the value is anything else (a string, a boolean, a mapping, an
    empty / ragged list, a list holding a non-number, a non-finite number)."""
    present, value = _lookup_input(inputs, dotted)
    if not present:
        raise PrimaryEvidenceError(f"inputs.{dotted} is not a key of this case's inputs")
    if _is_number(value):
        try:
            return _finite(float(value), f"inputs.{dotted}")
        except OverflowError:   # an integer literal beyond float64
            raise PrimaryEvidenceError(f"inputs.{dotted} is too large for a float") from None
    if isinstance(value, list):
        if _contains_non_number(value) or not value:
            raise PrimaryEvidenceError(
                f"inputs.{dotted} is a list holding something other than numbers, or an "
                "empty one (only a rectangular numeric list can enter an expression)")
        try:
            arr = np.asarray(value, dtype=np.float64)
        except (TypeError, ValueError, OverflowError):
            arr = None
        if arr is None or arr.size == 0 or arr.ndim > MAX_INPUT_RANK:
            raise PrimaryEvidenceError(
                f"inputs.{dotted} is not a rectangular numeric list of rank 1..{MAX_INPUT_RANK}")
        return _finite(arr, f"inputs.{dotted}")
    raise PrimaryEvidenceError(
        f"inputs.{dotted} is {type(value).__name__}, not a number or a numeric list (only a "
        "numeric case input can enter an expression)")


def resolve_input_number(inputs: Any, dotted: str) -> float:
    """The SCALAR numeric value at ``inputs.<dotted>``; raises when absent, not a number,
    or a list (a coordinate parameter is one number)."""
    value = resolve_input_value(inputs, dotted)
    if not isinstance(value, float):
        raise PrimaryEvidenceError(f"inputs.{dotted} is a list, not one number")
    return value


def _coordinate_param(spec: dict[str, Any], key: str, inputs: Any) -> float:
    raw = spec.get(key)
    if _is_number(raw):
        return float(raw)
    if isinstance(raw, str) and raw.startswith(_INPUTS_ROOT + "."):
        return resolve_input_number(inputs, raw[len(_INPUTS_ROOT) + 1:])
    raise PrimaryEvidenceError(
        f"coordinates[{spec.get('name')!r}].{key} must be a number or inputs.<path> "
        f"(got {raw!r})")


def coordinate_arrays(schema: dict[str, Any], inputs: Any,
                      state_shape: tuple[int, ...] | None = None) -> dict[str, np.ndarray]:
    """The coordinate arrays of one case: the axis's `count` positions along `axis`, expanded
    to ``state_shape`` when one is given (the shape shared by every captured array of the
    state's rank, so a field built from the coordinate reduces over the state's cells), else
    with extent 1 on the other axes."""
    rank = state_rank(schema)
    out: dict[str, np.ndarray] = {}
    specs = schema.get("coordinates")
    for spec in (specs if isinstance(specs, list) else []):
        if not isinstance(spec, dict) or not isinstance(spec.get("name"), str):
            raise PrimaryEvidenceError("coordinates[] entries must be mappings with a name")
        name = spec["name"].strip()
        if not name.isidentifier() or name in CONSTANTS or name in FUNCTIONS \
                or name in _ROOTS:
            raise PrimaryEvidenceError(
                f"coordinates[{name!r}].name must be an identifier that is not a grammar name")
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
        arr = positions.reshape(shape)
        if state_shape is not None:
            if len(state_shape) != rank or state_shape[axis] != n:
                raise PrimaryEvidenceError(
                    f"coordinates[{name!r}]: count {n} on axis {axis} does not match the "
                    f"captured state shape {list(state_shape)}")
            arr = np.ascontiguousarray(np.broadcast_to(arr, state_shape))
        out[name] = arr
    return out


def state_shape_of(captures: dict[str, Any], rank: int) -> tuple[int, ...] | None:
    """The one shape every captured array of ``rank`` has, or None when there is none or
    they disagree."""
    shapes = {np.shape(v) for v in captures.values() if np.ndim(v) == rank}
    return next(iter(shapes)) if len(shapes) == 1 else None


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
    wanted.setdefault(STEP_CAPTURE, [])
    for name, dims in wanted.items():
        if name not in doc:
            # A capture carries the variables its writer holds for this case: the
            # host-rendered runner every declared one (it `error stop`s on an unbound one), a
            # harness self-test's own runner every declared one too, by contract. A predicate naming an absent one
            # fails at `_capture_value`, on that predicate.
            continue
        raw = doc[name]
        try:
            arr = np.asarray(raw, dtype=np.float64)
        except (TypeError, ValueError) as exc:
            raise PrimaryEvidenceError(
                f"{path.name}: variable {name!r} is not a rectangular numeric array "
                f"({exc})") from None
        if _contains_non_number(raw):
            raise PrimaryEvidenceError(f"{path.name}: variable {name!r} holds a non-numeric value")
        if arr.ndim != len(dims):
            raise PrimaryEvidenceError(
                f"{path.name}: variable {name!r} has rank {arr.ndim}, declared "
                f"shape_expr has rank {len(dims)}")
        if arr.size == 0:
            raise PrimaryEvidenceError(f"{path.name}: variable {name!r} has no elements")
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


def _load_history(sdir: Path, case_id: str, variables: dict[str, list[str]],
                  time_variable: str | None, final: dict[str, Any]) -> dict[str, Any]:
    """The `history/` samples of one case, `<case_id>__<k>.json` with `k` the committed step,
    ascending by `k`, every name stacked on a LEADING axis. Each sample is read like a capture
    (`_load_capture`: declared rank, finiteness) and must carry `step` equal to its `k`; a name
    every sample carries is stacked (equal shapes required), one a sample omits is left out (a
    predicate naming it fails on that predicate). No sample at all is admitted (`every = 0`, or
    a first step refused): every name is then empty, an array variable shaped like its final
    capture."""
    hdir = sdir / _HISTORY
    pattern = re.compile(re.escape(case_id) + r"__([0-9]+)\.json")
    found: list[tuple[int, Path]] = []
    if hdir.is_dir():
        for path in hdir.iterdir():
            match = pattern.fullmatch(path.name)
            if match and path.is_file():
                found.append((int(match.group(1)), path))
    found.sort(key=lambda item: item[0])
    samples: list[dict[str, Any]] = []
    for k, path in found:
        sample = _load_capture(path, variables, time_variable)
        if STEP_CAPTURE not in sample:
            raise PrimaryEvidenceError(f"history sample {path.name} carries no {STEP_CAPTURE!r}")
        if sample[STEP_CAPTURE] != float(k):
            raise PrimaryEvidenceError(
                f"history sample {path.name} carries step {sample[STEP_CAPTURE]!r}, not the "
                f"committed step {k} its name says")
        samples.append(sample)
    names = list(variables) + [n for n in (time_variable, STEP_CAPTURE)
                               if n and n not in variables]
    out: dict[str, Any] = {}
    for name in names:
        if not samples:
            shape = np.shape(final[name]) if name in variables and name in final else ()
            out[name] = np.empty((0, *shape), dtype=np.float64)
            continue
        if not all(name in sample for sample in samples):
            continue
        shapes = {np.shape(sample[name]) for sample in samples}
        if len(shapes) != 1:
            raise PrimaryEvidenceError(
                f"history of case {case_id!r}: variable {name!r} changes shape between samples "
                f"({sorted(map(list, shapes))})")
        out[name] = np.stack([np.asarray(sample[name], dtype=np.float64) for sample in samples])
    return out


def _load_series(sdir: Path, case_id: str) -> dict[str, np.ndarray]:
    """`series/<case_id>.json`: one rank-1 numeric list per name, all of one length — zero
    admitted (no committed step) — and every entry finite (the driver's fold carries a
    non-finite committed cell into the entry; it is refused here, never laundered)."""
    path = sdir / _SERIES / f"{case_id}.json"
    if not path.is_file():
        raise PrimaryEvidenceError(f"series file {_SERIES}/{path.name} is absent")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PrimaryEvidenceError(f"series file {path.name} is unreadable: {exc}") from None
    if not isinstance(doc, dict):
        raise PrimaryEvidenceError(f"series file {path.name} is not a JSON object")
    out: dict[str, np.ndarray] = {}
    for name, raw in doc.items():
        if not isinstance(raw, list) or _contains_non_number(raw):
            raise PrimaryEvidenceError(
                f"series file {path.name}: {name!r} is not a list of numbers")
        arr = np.asarray(raw, dtype=np.float64)
        if arr.ndim != 1:
            raise PrimaryEvidenceError(f"series file {path.name}: {name!r} is not rank 1")
        if not np.all(np.isfinite(arr)):
            raise PrimaryEvidenceError(
                f"series file {path.name}: {name!r} holds a non-finite value")
        out[str(name)] = arr
    if len({a.shape[0] for a in out.values()}) > 1:
        raise PrimaryEvidenceError(
            f"series file {path.name}: the entries are not of one length "
            f"({ {n: int(a.shape[0]) for n, a in sorted(out.items())} })")
    return out


def load_case_env(run_dir: Path, case: dict[str, Any], schema: dict[str, Any],
                  derived: Any = None) -> CaseEnv:
    """The environment of one case: both captures under ``run_dir/raw/state_snapshots``, the
    case's inputs and its coordinate arrays; `history/` and `series/` as `Lazy` loaders,
    read only when a predicate names them; ``derived`` (a mapping or a `Lazy` of one) as
    given."""
    case_id = str(case.get("case_id") or "").strip()
    if not case_id:
        raise PrimaryEvidenceError("case has no case_id")
    inputs = case.get("inputs")
    inputs = inputs if isinstance(inputs, dict) else {}
    variables = schema_variables(schema)
    tv = schema.get("time_variable")
    tv = tv.strip() if isinstance(tv, str) and tv.strip() else None
    sdir = Path(run_dir) / "raw" / "state_snapshots"
    initial_path = sdir / "initial" / f"{case_id}.json"
    # A node whose own runner writes the snapshots (a harness self-test) writes no `initial/`
    # capture; the host-rendered runner always does, and the conductor's
    # `_snapshot_deliverable_gap(initial_required=True)` refuses a run without one before any
    # verdict is authored (the `--stage post_execute` validator only shape-checks the file).
    initial = _load_capture(initial_path, variables, tv) if initial_path.is_file() else None
    final = _load_capture(sdir / f"{case_id}.json", variables, tv)
    rank = state_rank(schema)
    shape = state_shape_of(final, rank)
    if shape is not None and initial is not None and state_shape_of(initial, rank) != shape:
        raise PrimaryEvidenceError(
            f"{case_id}: the initial and final captures disagree on the state shape")
    return CaseEnv(case_id=case_id, initial=initial, final=final, inputs=inputs,
                   coordinates=coordinate_arrays(schema, inputs, shape),
                   history=Lazy(lambda: _load_history(sdir, case_id, variables, tv, final)),
                   series=Lazy(lambda: _load_series(sdir, case_id)),
                   derived=derived, state_rank=rank)


# ------------------------------------------------------------------------------ evaluation

def _finite(value: Any, what: str) -> Any:
    arr = np.asarray(value)
    if not np.all(np.isfinite(arr)):
        raise PrimaryEvidenceError(f"{what} produced a non-finite value")
    return value


def _shape_compatible(a: Any, b: Any, what: str) -> None:
    """The one broadcasting rule: a scalar pairs with anything; two arrays pair only at equal
    rank with each extent equal or 1 on one side. Refused here, with the shapes named, rather
    than left to numpy (which right-aligns unequal ranks, and raises a bare ValueError on
    unequal extents)."""
    sa, sb = np.shape(a), np.shape(b)
    if not sa or not sb:
        return
    if len(sa) != len(sb):
        raise PrimaryEvidenceError(
            f"{what}: operands of rank {len(sa)} and {len(sb)} (an operand must be a scalar or "
            "of equal rank; right-aligned broadcasting is not admitted)")
    if any(x != y and x != 1 and y != 1 for x, y in zip(sa, sb)):
        raise PrimaryEvidenceError(
            f"{what}: operands of shape {list(sa)} and {list(sb)} do not pair (extents must be "
            "equal, or 1 on one side)")


def _reducible(name: str, value: Any, srank: int | None = None) -> Any:
    """A reduction over an array with an extent-1 axis (a coordinate the host could not
    expand to the state's shape) would count one cell where the state has a whole row; it is
    refused rather than valued. The leading axis of a field one rank above the state
    (``srank``: a `history.*` field's samples) is not a state axis and may have extent 1. A
    reduction over an EMPTY array — an empty history or series — is refused too: `sum` would
    value it 0 and `max` has no value at all."""
    shape = np.shape(value)
    if 0 in shape:
        raise PrimaryEvidenceError(
            f"{name}(): reduction over an empty array of shape {list(shape)} (an empty "
            "history or series: no sample was written, or no step was committed)")
    state_axes = shape[1:] if srank and len(shape) == srank + 1 else shape
    if len(shape) >= 2 and 1 in state_axes:
        raise PrimaryEvidenceError(
            f"{name}(): reduction over a field of shape {list(shape)} with an extent-1 axis "
            "(a coordinate the host could not expand to the state's shape, or a state with "
            "an axis of extent 1); pair it with a state array first")
    return value


def _sample_reduce(name: str, value: Any) -> Any:
    """`smax` / `smin` / `ssum`: reduce every axis but the leading one (the identity on rank
    1). The trailing axes are a state's, so the extent-1 rule of `_reducible` applies to them
    when there are two or more."""
    arr = np.asarray(value, dtype=np.float64)
    if arr.ndim == 0:
        raise PrimaryEvidenceError(f"{name}(): the argument must be an array")
    if arr.ndim == 1:
        return arr
    if arr.ndim >= 3 and 1 in arr.shape[1:]:
        raise PrimaryEvidenceError(
            f"{name}(): reduction over a field of shape {list(arr.shape)} with an extent-1 "
            "state axis (a coordinate the host could not expand to the state's shape); pair "
            "it with a state array first")
    axes = tuple(range(1, arr.ndim))
    if name == "smax":
        return np.max(arr, axis=axes)
    if name == "smin":
        return np.min(arr, axis=axes)
    return np.sum(arr, axis=axes)


#: The refusal of a `transpose` given one axis: it takes the array alone or with two axes.
_TRANSPOSE_ARITY = "transpose(): takes the array alone, or the array and the two axes it exchanges"


def _integer_scalar(name: str, what: str, value: Any) -> int:
    """`value` as an int; the caller has refused a non-scalar already."""
    fv = float(value)
    if not fv.is_integer():
        raise PrimaryEvidenceError(f"{name}(): {what} {fv!r} is not an integer")
    return int(fv)


def _axis(name: str, arr: np.ndarray, value: Any) -> int:
    k = _integer_scalar(name, "the axis", value)
    if not 0 <= k < arr.ndim:
        raise PrimaryEvidenceError(
            f"{name}(): axis {k} is outside [0, {arr.ndim}) for an array of rank {arr.ndim}")
    return k


def _array_first(name: str, args: list[Any]) -> Any:
    """`transpose`, `flip` and `coarsen`: the first argument an array, the rest integer
    scalars; the result has the array's rank."""
    arr = np.asarray(args[0])
    if arr.ndim == 0:
        raise PrimaryEvidenceError(f"{name}(): the first argument must be an array")
    for extra in args[1:]:
        if np.ndim(extra) != 0:
            raise PrimaryEvidenceError(f"{name}(): every argument after the first must be a scalar")
    if name == "transpose":
        if len(args) == 1:
            return np.transpose(arr)
        if len(args) != 3:
            raise PrimaryEvidenceError(_TRANSPOSE_ARITY)
        i, j = (_axis(name, arr, a) for a in args[1:])
        return np.swapaxes(arr, i, j)
    if name == "flip":
        return np.flip(arr, axis=_axis(name, arr, args[1]))
    f = _integer_scalar(name, "the factor", args[1])
    if f < 1:
        raise PrimaryEvidenceError(f"coarsen(): the factor {f} is below 1")
    if any(n % f for n in arr.shape):
        raise PrimaryEvidenceError(
            f"coarsen(): the factor {f} does not divide every extent of shape {list(arr.shape)}")
    blocked = np.asarray(arr, dtype=np.float64).reshape(
        tuple(x for n in arr.shape for x in (n // f, f)))
    return blocked.mean(axis=tuple(range(1, 2 * arr.ndim, 2)))


def _call(name: str, args: list[Any], srank: int | None = None) -> Any:
    with np.errstate(all="ignore"):
        if name == "sum":
            return np.sum(_reducible(name, args[0], srank))
        if name == "mean":
            return np.mean(_reducible(name, args[0], srank))
        if name in ("smax", "smin", "ssum"):
            return _sample_reduce(name, args[0])
        if name in ("min", "max"):
            if len(args) == 1:
                _reducible(name, args[0], srank)
                return np.min(args[0]) if name == "min" else np.max(args[0])
            acc = args[0]
            for other in args[1:]:
                _shape_compatible(acc, other, f"{name}()")
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
            return np.sqrt(np.sum(
                np.asarray(_reducible(name, args[0], srank), dtype=np.float64) ** 2))
        if name == "maxabs":
            return np.max(np.abs(_reducible(name, args[0], srank)))
        if name == "ceil":
            return np.ceil(args[0])
        if name == "floor":
            return np.floor(args[0])
        if name == "atan2":
            _shape_compatible(args[0], args[1], "atan2()")
            return np.arctan2(args[0], args[1])
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
        if name in ("transpose", "flip", "coarsen"):
            return _array_first(name, args)
    raise PrimaryEvidenceError(f"function {name!r} is not admitted")  # unreachable past parse


def _eval_node(node: ast.AST, env: CaseEnv, at_envs: dict[str, CaseEnv],
               binds: dict[str, Any], cenv: CaseEnv | None = None) -> Any:
    if isinstance(node, ast.Constant):
        return _finite(float(node.value), f"constant {node.value!r}")
    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left, env, at_envs, binds, cenv)
        right = _eval_node(node.right, env, at_envs, binds, cenv)
        sym = _BINOPS[type(node.op)]
        left, right = _lift_coordinate(node.left, left, right, env, binds)
        right, left = _lift_coordinate(node.right, right, left, env, binds)
        _shape_compatible(left, right, f"operator {sym}")
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
        return -_eval_node(node.operand, env, at_envs, binds, cenv)
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
            return resolve_input_value(env.inputs, ".".join(attrs))
        if isinstance(root, ast.Name) and root.id in _SAMPLED_ROOTS:
            return _sampled_value(env, root.id, attrs[0])
        if isinstance(root, ast.Name) and root.id == _COMPARAND:
            if cenv is None:
                raise PrimaryEvidenceError(
                    f"{_COMPARAND}.{'.'.join(attrs)}: no comparand is bound for this evaluation")
            return _capture_value(cenv, attrs[0], attrs[1], who="the comparand")
        assert _is_at_call(root) and isinstance(root, ast.Call)
        case_id = str(root.args[0].value)  # type: ignore[attr-defined]
        if case_id not in at_envs:
            raise PrimaryEvidenceError(
                f"at({case_id!r}) names a case outside this predicate's target_cases")
        other = at_envs[case_id]
        if len(attrs) == 2 and attrs[0] in CAPTURE_POINTS:
            return _capture_value(other, attrs[0], attrs[1])
        if attrs[0] == _INPUTS_ROOT:
            return resolve_input_value(other.inputs, ".".join(attrs[1:]))
        if attrs[0] in other.coordinates:
            return other.coordinates[attrs[0]]
        raise PrimaryEvidenceError(f"at({case_id!r}).{attrs[0]}: not a coordinate of that case")
    if isinstance(node, ast.Call):
        assert isinstance(node.func, ast.Name)
        args = [_eval_node(a, env, at_envs, binds, cenv) for a in node.args]
        return _finite(_call(node.func.id, args, env.state_rank), f"{node.func.id}()")
    raise PrimaryEvidenceError(f"{type(node).__name__} is not admitted")  # unreachable past parse


def _is_coordinate_node(node: ast.AST, coordinates: Any, binds: Any) -> bool:
    """Whether ``node`` NAMES a coordinate: a bare name that is not a bind, or
    `at('<case>').<coordinate>`. A field built from a coordinate is not one."""
    if isinstance(node, ast.Name):
        return node.id not in binds and node.id in coordinates
    if isinstance(node, ast.Attribute):
        root, attrs = _attr_chain(node)
        return _is_at_call(root) and len(attrs) == 1 and attrs[0] not in _ROOTS
    return False


def _lift_coordinate(node: ast.AST, value: Any, other: Any, env: CaseEnv,
                     binds: dict[str, Any]) -> tuple[Any, Any]:
    """A coordinate named directly as one operand of a binary operator, against an operand
    of one more rank (a `history.*` field: samples on the leading axis), gains that leading
    axis with extent 1, so it broadcasts over the samples on the trailing axes (grammar 6)."""
    if _is_coordinate_node(node, env.coordinates, binds) \
            and np.ndim(other) == np.ndim(value) + 1 and np.ndim(value) >= 1:
        return np.asarray(value)[np.newaxis, ...], other
    return value, other


def _sampled_value(env: CaseEnv, root: str, name: str) -> Any:
    """`history.<name>` / `series.<name>` / `derived.<name>` of the case being evaluated."""
    table = _table(getattr(env, root))
    if table is None:
        raise PrimaryEvidenceError(
            f"{root}.{name}: case {env.case_id!r} has no {root} to read")
    if name not in table:
        what = {_HISTORY: "sampled", _SERIES: "in the series",
                _DERIVED: "a derived entry"}[root]
        raise PrimaryEvidenceError(f"{root}.{name}: not {what} in case {env.case_id!r}")
    return table[name]


def _shape_disagreement(env: CaseEnv, cenv: CaseEnv) -> str | None:
    """A description of the first captured variable whose shape differs between this run's
    case and the comparand's (the same case of the same IR), or None. A comparand variant
    that sized its state differently passes its own gates (a symbolic extent is not pinned to
    the case's inputs) and differs from this one in the state itself."""
    for point in CAPTURE_POINTS:
        mine = env.initial if point == "initial" else env.final
        theirs = cenv.initial if point == "initial" else cenv.final
        for var in sorted(set(mine or {}) & set(theirs or {})):
            a, b = np.shape(mine[var]), np.shape(theirs[var])  # type: ignore[index]
            if a != b:
                return (f"{point}.{var}: shape {list(a)} here, {list(b)} in the comparand "
                        f"(case {env.case_id!r})")
    return None


def _capture_value(env: CaseEnv, point: str, var: str, *, who: str = "this run") -> Any:
    table = env.initial if point == "initial" else env.final
    if table is None:
        raise PrimaryEvidenceError(
            f"{point}.{var}: case {env.case_id!r} of {who} has no initial capture "
            "(raw/state_snapshots/initial/<case_id>.json is absent: a node whose own runner "
            "writes the snapshots writes none)")
    if var not in table:
        raise PrimaryEvidenceError(
            f"{point}.{var}: not captured in case {env.case_id!r} of {who}")
    return table[var]


#: What Python and numpy raise on admitted operands: a Python-float division by zero, an
#: integer literal too large for a float, a numpy operation refused on its operands. Each
#: becomes a PrimaryEvidenceError so the record names the predicate, not the interpreter.
#: `RecursionError` is defence in depth and NOT pinned: `parse_expr` bounds the tree at
#: `MAX_EXPR_DEPTH`, so no admitted expression recurses here, and no test reaches this member.
_ARITHMETIC_ERRORS = (ZeroDivisionError, OverflowError, ValueError, FloatingPointError,
                      TypeError, MemoryError, RecursionError)


def evaluate(tree: ast.Expression, env: CaseEnv, *, at_envs: dict[str, CaseEnv] | None = None,
             binds: dict[str, Any] | None = None,
             comparand_env: CaseEnv | None = None) -> Any:
    """Evaluate a parsed expression in ``env``; a ``comparand.`` name reads
    ``comparand_env`` (the same case in the comparand run), and raises when none is given.
    Returns a float or a float64 array. Raises only `PrimaryEvidenceError`."""
    try:
        return _eval_node(tree.body, env, at_envs or {}, binds or {}, comparand_env)
    except PrimaryEvidenceError:
        raise   # already named by this module (and a ValueError subclass: keep it verbatim)
    except _ARITHMETIC_ERRORS as exc:
        raise PrimaryEvidenceError(
            f"evaluation failed: {type(exc).__name__}: {str(exc)[:200]}") from None


def evaluate_binds(bind: Any, env: CaseEnv, at_envs: dict[str, CaseEnv],
                   comparand_env: CaseEnv | None = None) -> dict[str, Any]:
    """Evaluate a predicate's `bind` mapping in order; each entry sees the earlier ones."""
    out: dict[str, Any] = {}
    if bind is None:
        return out
    if not isinstance(bind, dict):
        raise PrimaryEvidenceError("bind must be a mapping of name -> expression")
    for name, text in bind.items():
        if not isinstance(name, str) or not name.isidentifier():
            raise PrimaryEvidenceError(f"bind name {name!r} is not an identifier")
        if name in CONSTANTS or name in FUNCTIONS or name in _ROOTS or name in env.coordinates:
            raise PrimaryEvidenceError(f"bind name {name!r} shadows a grammar name")
        out[name] = evaluate(parse_expr(text), env, at_envs=at_envs, binds=out,
                             comparand_env=comparand_env)
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
    preds = io.get("predicates") if isinstance(io, dict) else None
    return preds if isinstance(preds, list) else []


def evaluate_primary_predicates(ir: dict[str, Any], run_dir: Path, *,
                                comparands: Sequence[ComparandEvidence] = (),
                                cross_target_only: bool = False,
                                ) -> list[dict[str, Any]]:
    """Evaluate every `io_contract.predicates[]` entry of ``ir`` against the captures
    under ``run_dir``. Returns one record per predicate, in order::

        {test_id, quantity, expr, op, scope, target_cases, cases_read, scalar_pairing,
         grammar_version, comparands, satisfied, kind,
         evaluated: [{case, value, rhs, satisfied} | {case, satisfied: false, reason, error}]}

    ``cases_read`` is the cases the predicate VALUES (`cases_read` under the IR's `state_reads`:
    reached binds, outside a term constant by form; the key keeps its name), and
    ``scalar_pairing`` what D7 asks of it (`record_scalar_pairing`), which
    `verdict_evaluator.evaluate_verdict` re-checks. ``kind`` is ``pass`` / ``physics`` (a
    comparison was false) / ``structural`` (the
    expression could not be evaluated: `PrimaryEvidenceError`) / ``no_comparand``. An empty
    list when the IR declares no primary predicate. A predicate whose own shape is malformed
    (no test_id, no quantity, no expr, an inadmissible op or scope) raises
    `PrimaryEvidenceError` — the Compile gate refuses those, so reaching one here is a defect
    of the certified IR.

    A CROSS-TARGET predicate (`predicate_reads_comparand`, grammar 3) is evaluated once per
    entry of ``comparands`` — the other targets' eligible runs — and per target case, each ``evaluated[]`` element naming its ``comparand`` target id; the
    record's ``comparands`` lists the comparand runs (`ComparandEvidence.detail`). A mismatch
    against one comparand stops that comparand's cases and the rest are still evaluated; a
    captured variable whose SHAPE differs between the two runs is such a mismatch
    (``reason: comparand_shape_mismatch``, ``physics``), and any other evaluation error on any
    comparand makes the record ``structural``. With no comparand at
    all the record is ``kind: no_comparand``, satisfied, with nothing evaluated — the first
    variant of a node has no reference, and the record says so rather than looking like a
    comparison that held. Every other predicate's record carries ``comparands: []``.

    The verdict author passes no comparand, so in a `verdict.json` every cross-target record is
    the ``no_comparand`` placeholder (issue #383). ``cross_target_only`` evaluates the
    cross-target predicates alone, every other one skipped and absent from the result: the
    cross-target agreement (`orchestration_runtime.cross_target_agreement`) asks that way."""
    preds = primary_predicates(ir)
    if not preds:
        return []
    schema = snapshot_schema(ir)
    if schema is None:
        raise PrimaryEvidenceError(
            "predicates declared but the IR has no state_snapshots evidence entry")
    reads = state_reads(ir)
    one_call = drive_model(ir).one_call_scalars if _has_drive(ir) else {}
    cases = _cases_by_id(ir)
    env_cache: dict[tuple[str, str], CaseEnv] = {}
    comparand_list = list(comparands)
    # The case's `derived` values, evaluated once for the IR on first use: a predicate that
    # names none never pays for it, and a failure is a structural record of the predicates
    # that do.
    all_derived = Lazy(lambda: {cid: dict(values.derived)
                                for cid, values in _evaluate_cases(ir).items()})

    def env_for(cid: str, where: Path = Path(run_dir), key: str = "") -> CaseEnv:
        if (key, cid) not in env_cache:
            if cid not in cases:
                raise PrimaryEvidenceError(f"case {cid!r} is not in case.test_case_set")
            derived = Lazy(lambda: all_derived.get().get(cid, {}))
            env_cache[(key, cid)] = load_case_env(where, cases[cid], schema, derived)
        return env_cache[(key, cid)]

    out: list[dict[str, Any]] = []
    closures: set[str] = set()
    for idx, pred in enumerate(preds):
        loc = f"predicates[{idx}]"
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
        for key in ("na_allowed", "expected_outcome"):
            if key in pred:
                raise PrimaryEvidenceError(f"{loc}: {key} is not a primary predicate key")
        tree = parse_expr(pred.get("expr"))
        expr_refs, bind_refs = _predicate_refs(pred)
        closure = is_zero_step_closure(pred, reads) and test_id.strip() not in closures
        if closure:
            closures.add(test_id.strip())   # one exempt closure per test, as the gate admits
        if not predicate_reads_state(expr_refs, bind_refs, reads) and not closure:
            raise PrimaryEvidenceError(
                f"{loc}: reads no captured state the kernel produced (final.<written var>, "
                "initial.<var init writes>, history.*, series.*) in expr or a bind expr "
                "reaches")
        cross = predicate_reads_comparand(expr_refs, bind_refs)
        if cross_target_only and not cross:
            continue
        scope, contexts = _predicate_scope(pred, loc)
        targets = [c.strip() for c in pred["target_cases"]]
        record: dict[str, Any] = {
            "test_id": test_id.strip(), "quantity": quantity, "expr": str(pred["expr"]).strip(),
            "op": op, "scope": scope, "target_cases": list(targets),
            # the cases this record values the state of (`cases_read`), and what D7 asks of it
            # (`record_scalar_pairing`), so the verdict evaluator can re-check both on records
            "cases_read": sorted(cases_read({**pred, "target_cases": targets}, reads) or []),
            "scalar_pairing": record_scalar_pairing({**pred, "target_cases": targets},
                                                    one_call),
            "grammar_version": GRAMMAR_VERSION,
            "comparands": [c.detail() for c in comparand_list] if cross else [],
            "satisfied": True, "kind": "pass", "evaluated": [],
        }
        if cross and not comparand_list:
            record["kind"] = "no_comparand"
            out.append(record)
            continue
        # one pass per comparand for a cross-target predicate; one, with none, otherwise
        passes: list[ComparandEvidence | None] = list(comparand_list) if cross else [None]
        for comp in passes:
            for cid in contexts:
                tag = {"comparand": comp.target_id} if comp is not None else {}
                try:
                    env = env_for(cid)
                    at_envs = {t: env_for(t) for t in targets}
                    cenv = (env_for(cid, comp.run_dir, f"comparand:{comp.target_id}")
                            if comp is not None else None)
                    mismatch = _shape_disagreement(env, cenv) if cenv is not None else None
                    if mismatch is not None:
                        # The two variants disagree on the state itself, not on this run's
                        # evidence: a comparison that failed (`physics`), routed like a value
                        # disagreement, never a structural gap charged to this run (round 2).
                        record["evaluated"].append({**tag, "case": cid, "satisfied": False,
                                                    "reason": "comparand_shape_mismatch",
                                                    "error": mismatch})
                        record["satisfied"] = False
                        if record["kind"] != "structural":
                            record["kind"] = "physics"
                        break
                    binds = evaluate_binds(pred.get("bind"), env, at_envs, cenv)
                    value = _scalar(evaluate(tree, env, at_envs=at_envs, binds=binds,
                                             comparand_env=cenv), f"{loc}.expr")
                    present, rhs = _resolve_value(pred.get("value"), cid)
                    if not present:
                        raise PrimaryEvidenceError(
                            f"{loc}.value.per_case has no entry for {cid!r}")
                    if not _is_number(rhs) or not math.isfinite(rhs):
                        raise PrimaryEvidenceError(
                            f"{loc}.value for {cid!r} is not a finite number")
                    ok = _apply_op(value, op, rhs)
                    record["evaluated"].append({**tag, "case": cid, "value": value, "rhs": rhs,
                                                "satisfied": bool(ok)})
                    if not ok:
                        record["satisfied"] = False
                        if record["kind"] != "structural":
                            record["kind"] = "physics"
                        break
                except PrimaryEvidenceError as exc:
                    record["evaluated"].append({**tag, "case": cid, "satisfied": False,
                                                "reason": "evaluation_error",
                                                "error": str(exc)[:400]})
                    record["satisfied"] = False
                    record["kind"] = "structural"
                    break
        out.append(record)
    return out


# ----------------------------------------------------------------------------- static rank

#: How `expr_rank` ranks each function's result — one entry per key of `FUNCTIONS` (set
#: identity pinned by `StaticRankTest`), so a function added without a rule fails a row.
#: ``reduce``: a scalar; ``reduce_or_pairwise``: a scalar with one argument, the pairwise rank
#: of the arguments with more; ``elementwise``: the argument's rank; ``pairwise``: the two
#: arguments' pairwise rank; ``roll``: the array's rank, with one scalar shift per axis;
#: ``array_first``: the first argument's rank, which must not be 0, every other argument a
#: scalar (an axis range, `coarsen`'s divisibility and every integer check stay evaluation
#: errors: they need values); ``sample_reduce``: rank 1, the argument an array.
_RANK_RULES: dict[str, str] = {
    "sum": "reduce", "mean": "reduce", "norm2": "reduce", "maxabs": "reduce",
    "min": "reduce_or_pairwise", "max": "reduce_or_pairwise",
    "abs": "elementwise", "sqrt": "elementwise", "exp": "elementwise", "log": "elementwise",
    "log2": "elementwise", "sin": "elementwise", "cos": "elementwise",
    "ceil": "elementwise", "floor": "elementwise",
    "atan2": "pairwise", "roll": "roll",
    "transpose": "array_first", "flip": "array_first", "coarsen": "array_first",
    "smax": "sample_reduce", "smin": "sample_reduce", "ssum": "sample_reduce",
}


class RankEnv(NamedTuple):
    """What `expr_rank` knows of a predicate's operands before any run: each capture's rank
    (from its `shape_expr`; the time variable 0, as `_load_capture` reads it), the coordinate
    names and the one rank they all have (`state_rank`), and the rank of an input in a case
    (``input_rank(case_id, dotted path)``)."""
    captures: dict[str, int]
    coordinates: frozenset[str]
    coordinate_rank: int
    input_rank: Callable[[str, str], int]
    case: str | None = None   # the case `inputs.<path>` is read in; None when no ref needs one
    #: ``derived_rank(case_id, name)``: the rank of the case's evaluated `derived.<name>`
    #: (grammar 6); None when no case value is known (the gate then refuses the name first).
    derived_rank: Callable[[str, str], int] | None = None


def _pair_rank(ra: int, rb: int, what: str) -> int:
    """The rank half of `_shape_compatible`: a scalar pairs with anything; two arrays pair
    only at equal rank. The message is `_shape_compatible`'s, byte for byte."""
    if ra == 0 or rb == 0:
        return max(ra, rb)
    if ra != rb:
        raise PrimaryEvidenceError(
            f"{what}: operands of rank {ra} and {rb} (an operand must be a scalar or "
            "of equal rank; right-aligned broadcasting is not admitted)")
    return ra


def expr_rank(node: ast.AST, env: RankEnv, binds: dict[str, int]) -> int:
    """The rank `_eval_node` would return for ``node``, inferred from declarations alone, or
    `PrimaryEvidenceError` with the message evaluation would raise for an operand pair of
    unequal rank, a `roll` with the wrong shift count or a non-scalar shift, a `transpose` /
    `flip` / `coarsen` of a scalar or with a non-scalar axis or factor, a `transpose` given one
    axis. Extents are not inferred: an extent mismatch stays an evaluation error. ``node`` must
    have passed `parse_expr` and name resolution (the gate ranks nothing it refused)."""
    if isinstance(node, ast.Expression):
        return expr_rank(node.body, env, binds)
    if isinstance(node, ast.Constant):
        return 0
    if isinstance(node, ast.BinOp):
        left, right = expr_rank(node.left, env, binds), expr_rank(node.right, env, binds)
        # `_lift_coordinate`: a coordinate named directly beside an operand one rank above it
        for side, rank, other in ((node.left, left, right), (node.right, right, left)):
            if _is_coordinate_node(side, env.coordinates, binds) and rank >= 1 \
                    and other == rank + 1:
                return other
        return _pair_rank(left, right, f"operator {_BINOPS[type(node.op)]}")
    if isinstance(node, ast.UnaryOp):
        return expr_rank(node.operand, env, binds)
    if isinstance(node, ast.Name):
        if node.id in binds:
            return binds[node.id]
        if node.id in env.coordinates:
            return env.coordinate_rank
        return 0   # a constant
    if isinstance(node, ast.Attribute):
        root, attrs = _attr_chain(node)
        if isinstance(root, ast.Name) and root.id in CAPTURE_POINTS:
            return env.captures[attrs[0]]
        if isinstance(root, ast.Name) and root.id == _COMPARAND:
            return env.captures[attrs[1]]
        if isinstance(root, ast.Name) and root.id == _INPUTS_ROOT:
            assert env.case is not None
            return env.input_rank(env.case, ".".join(attrs))
        if isinstance(root, ast.Name) and root.id == _HISTORY:
            return env.captures[attrs[0]] + 1
        if isinstance(root, ast.Name) and root.id == _SERIES:
            return 1
        if isinstance(root, ast.Name) and root.id == _DERIVED:
            assert env.case is not None and env.derived_rank is not None
            return env.derived_rank(env.case, attrs[0])
        assert _is_at_call(root) and isinstance(root, ast.Call)
        case_id = str(root.args[0].value)  # type: ignore[attr-defined]
        if len(attrs) == 2 and attrs[0] in CAPTURE_POINTS:
            return env.captures[attrs[1]]
        if attrs[0] == _INPUTS_ROOT:
            return env.input_rank(case_id, ".".join(attrs[1:]))
        return env.coordinate_rank
    assert isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    name = node.func.id
    ranks = [expr_rank(a, env, binds) for a in node.args]
    rule = _RANK_RULES[name]
    if rule == "reduce" or (rule == "reduce_or_pairwise" and len(ranks) == 1):
        return 0
    if rule == "reduce_or_pairwise":
        acc = ranks[0]
        for r in ranks[1:]:
            acc = _pair_rank(acc, r, f"{name}()")
        return acc
    if rule == "elementwise":
        return ranks[0]
    if rule == "sample_reduce":
        if ranks[0] == 0:
            raise PrimaryEvidenceError(f"{name}(): the argument must be an array")
        return 1
    if rule == "pairwise":
        return _pair_rank(ranks[0], ranks[1], f"{name}()")
    if rule == "array_first":
        if ranks[0] == 0:
            raise PrimaryEvidenceError(f"{name}(): the first argument must be an array")
        if any(r != 0 for r in ranks[1:]):
            raise PrimaryEvidenceError(
                f"{name}(): every argument after the first must be a scalar")
        if name == "transpose" and len(ranks) == 2:
            raise PrimaryEvidenceError(_TRANSPOSE_ARITY)
        return ranks[0]
    # roll
    if len(ranks) - 1 != ranks[0]:
        raise PrimaryEvidenceError(
            f"roll(): {len(ranks) - 1} shift(s) for an array of rank {ranks[0]}")
    if any(r != 0 for r in ranks[1:]):
        raise PrimaryEvidenceError("roll(): a shift must be a scalar")
    return ranks[0]


def _rank_violation(parsed: list[tuple[str, str | None, ast.Expression]],
                    env: RankEnv) -> tuple[str, str] | None:
    """``(location, message)`` of the first rank refusal over one predicate's parsed binds
    (in order — every bind is evaluated at run time, used or not) and `expr`, or None. An
    `expr` of rank above 0 is refused with the scalar rule of `_scalar`."""
    binds: dict[str, int] = {}
    for eloc, bind_name, tree in parsed:
        try:
            rank = expr_rank(tree, env, binds)
        except PrimaryEvidenceError as exc:
            return (eloc, str(exc))
        if bind_name is not None:
            binds[bind_name] = rank
        elif rank != 0:
            return (eloc, (f"evaluates to an array of rank {rank}, not a scalar (reduce it "
                           "with sum/min/max/mean/norm2/maxabs)"))
    return None


# ---------------------------------------------------------------------------------- schema

def _predicate_rank_violation(pred: dict[str, Any], loc: str,
                              parsed: list[tuple[str, str | None, ast.Expression]],
                              targets: list[str], schema: dict[str, Any],
                              variables: dict[str, list[str]], coord_names: set[str],
                              cases: dict[str, dict[str, Any]],
                              case_inputs: dict[str, dict[str, Any]] | None = None,
                              ) -> str | None:
    """The gate's rank violation for one predicate whose names all resolved, or None. A
    predicate that reads `inputs.<path>` of its own case is ranked once per case it is
    evaluated in (an input's rank may differ between cases); the case is named only when the
    refusal is not the same in every one."""
    tv = schema.get("time_variable")
    captures = {n: len(d) for n, d in variables.items()}
    if isinstance(tv, str) and tv.strip():
        captures[tv.strip()] = 0
    captures.setdefault(STEP_CAPTURE, 0)

    def input_rank(cid: str, dotted: str) -> int:
        case = cases.get(cid)
        return int(np.ndim(resolve_input_value(
            case.get("inputs") if isinstance(case, dict) else None, dotted)))

    def derived_rank(cid: str, name: str) -> int:
        return int(np.ndim((case_inputs or {})[cid][f"{_DERIVED}.{name}"]))

    refs = [r for _, _, tree in parsed for r in expr_names(tree)]
    coord_rank = state_rank(schema)
    base = RankEnv(captures=captures, coordinates=frozenset(coord_names),
                   coordinate_rank=coord_rank, input_rank=input_rank, derived_rank=derived_rank)
    contexts: list[str | None] = [None]
    if any(r.kind in ("inputs", _DERIVED) and r.case is None for r in refs):
        try:
            contexts = list(_predicate_scope({**pred, "target_cases": targets}, loc)[1])
        except PrimaryEvidenceError:
            contexts = list(targets)
    found = [(cid, _rank_violation(parsed, base._replace(case=cid))) for cid in contexts]
    failed = [(cid, f) for cid, f in found if f is not None]
    if not failed:
        return None
    cid, (eloc, text) = failed[0]
    if "operands of rank" in text and any(
            r.kind == "name" and r.name in coord_names for r in refs):
        text += (f"; a coordinate has the state's rank {coord_rank}, the highest rank among "
                 "the snapshot variables")
    if cid is None or len(failed) == len(found) and len({f for _, f in failed}) == 1:
        return f"{eloc}: {text}"
    return f"{eloc}: in case {cid!r}: {text}"


def validate_primary_predicate_schema(
    predicates: Any,
    *,
    case_ids: set[str],
    test_ids: list[str],
    schema: dict[str, Any] | None,
    cases: dict[str, dict[str, Any]],
    test_target_cases: dict[str, list[str]] | None = None,
    reads: StateReads | None = None,
    case_inputs: dict[str, dict[str, Any]] | None = None,
    series_names: frozenset[str] | set[str] | None = None,
    history_declared: bool = False,
) -> list[str]:
    """The Compile-stage gate over `io_contract.predicates` (present-or-absent; the per-test
    rules over quantities and the one-call scalar pairing are `quantity_violations` and
    `scalar_pairing_violations`). Returns violation strings (empty == valid).

    Grammar 6 (issue #589) context, which `predicate_schema_context(ir)` builds from the IR:
    ``reads`` — what counts as a state read (`state_reads`; None is the pre-drive rule, every
    schema variable at both capture points, which an IR with no `drive` keeps); ``case_inputs``
    — `evaluate_case_inputs(ir)`, the only source of a `derived.<name>`'s value and so its rank
    (None: every `derived.*` read is refused); ``series_names`` — the `step_series` entry's
    `names` (None: no `step_series` is declared and every `series.*` read is refused);
    ``history_declared`` — a `state_history` entry is declared (else every `history.*` read
    is refused). A schema variable named `t`, `step` or `case_id` is refused.

    Checks each entry's shape (`test_id` ∈ tests.md, `quantity`, `op`, a non-null `value` of
    a number or a per-case map, exactly one scope, `target_cases` ⊆
    cases), parses `expr` and every `bind` under the closed grammar, and resolves every name:
    a capture name is a snapshot schema variable or the time variable; an `inputs.<path>` is a
    number or a rectangular numeric list in EVERY case the scope evaluates the predicate in
    (every target case under `per_case: true`, X under `case: X`; issue #532), and an
    `at('<case>').inputs.<path>` in that case; a bare name is a coordinate, an earlier bind, or a constant;
    an `at('<case>')` case is one of the predicate's own target cases; a
    `comparand.<point>.<var>` (grammar 3) names a snapshot schema variable or the time
    variable, like a capture, and the predicate that reads one must read this run's own state
    as well (the state rule below counts only this run's captures). `coordinates[]` is
    resolved against every declared case, since every case is captured. With
    ``test_target_cases`` (test_id -> the `io_contract.tests` entry's target_cases), a
    predicate's `target_cases` must equal its test's as a set: a predicate over a subset
    of the test's cases would certify the test on its easiest case alone.

    A predicate whose names all resolve is then ranked (`expr_rank`, issue #440): every bind
    in order and `expr`, refusing an operand pair of unequal rank (a coordinate has the
    state's rank), a `roll` shift count other than the array's rank or a non-scalar shift, a
    `transpose` / `flip` / `coarsen` of a scalar or with a non-scalar axis or factor, a
    `transpose` given one axis, and an `expr` that is not a scalar. A name or bind violation
    on the predicate suppresses its rank check, so one defect is reported once.

    Every parsed predicate must read this run's captured state (`predicate_reads_state`),
    and must still read it once the terms constant by form are pruned (`effective_names`,
    issue #483): a read only inside `X * 0`, `X ** 0`, `X - X` or `X + -X` is no read. The
    cross-target classification keeps the raw references."""
    v: list[str] = []
    if predicates is None:
        return v
    if not isinstance(predicates, list):
        return ["io_contract.predicates must be a list"]
    if schema is None:
        return ["io_contract.predicates requires a state_snapshots required_evidence "
                + "entry (the captures it evaluates)"]
    try:
        variables = schema_variables(schema)
    except PrimaryEvidenceError as exc:
        return [f"io_contract.predicates: snapshot schema unreadable ({exc})"]
    tv = schema.get("time_variable")
    capture_names = set(variables) | ({tv.strip()} if isinstance(tv, str) and tv.strip()
                                      else set()) | {STEP_CAPTURE}
    for name in sorted(set(variables) & RESERVED_VARIABLE_NAMES):
        v.append(f"state_snapshots.schema.variables: {name!r} is the driver's or the "
                 f"harness's own key ({sorted(RESERVED_VARIABLE_NAMES)}), not a variable name")
    state: StateReads | set[str] = reads if reads is not None else set(variables)
    coord_names: set[str] = set()
    for cid, case in sorted(cases.items()):
        inputs = case.get("inputs") if isinstance(case.get("inputs"), dict) else {}
        try:
            coord_names |= set(coordinate_arrays(schema, inputs))
        except PrimaryEvidenceError as exc:
            v.append(f"state_snapshots.schema.coordinates does not resolve in case {cid!r}: {exc}")
    md_set = set(test_ids)
    closures: set[str] = set()   # the tests whose zero-step closure has been exempted

    for idx, pred in enumerate(predicates):
        loc = f"predicates[{idx}]"
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
        if targets and test_target_cases is not None and isinstance(test_id, str) \
                and test_id.strip() in test_target_cases \
                and set(targets) != set(test_target_cases[test_id.strip()]):
            v.append(f"{loc}.target_cases {sorted(set(targets))} must equal the target_cases of "
                     f"test {test_id.strip()!r} in io_contract.tests "
                     f"({sorted(set(test_target_cases[test_id.strip()]))}): a predicate "
                     "ranges over every case its test ranges over")
        # The cases an own-case `inputs.<path>` is read in: those the scope evaluates the
        # predicate in (issue #532), or every target case when the scope is malformed, as
        # `_predicate_rank_violation` ranks it.
        scope_cases = list(targets)
        if targets:
            try:
                scope_cases = _predicate_scope({**pred, "target_cases": targets}, loc)[1]
            except PrimaryEvidenceError as exc:
                v.append(str(exc))
        for key in ("na_allowed", "expected_outcome"):
            if key in pred:
                v.append(f"{loc}: {key} is not a predicate key (the test's expected_outcome "
                         "is on its io_contract.tests entry; the host always has the state)")
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
                        if not _is_number(tv_) or not math.isfinite(tv_):
                            v.append(f"{loc}.value.per_case[{cid!r}] must be a finite number")
                    uncovered = sorted(c for c in targets if c not in table)
                    if uncovered:
                        v.append(f"{loc}.value.per_case is missing a threshold for target "
                                 f"case(s) {uncovered}")
        elif not _is_number(value) or not math.isfinite(value):
            v.append(f"{loc}.value must be a finite number (got {value!r})")

        # Names: the binds first, in order, then `expr`; each expression may reference the
        # binds written before it and nothing written after.
        mark = len(v)   # a name violation below suppresses the rank check: no cascades
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
                    if name in CONSTANTS or name in FUNCTIONS or name in _ROOTS \
                            or name in coord_names:
                        v.append(f"{loc}.bind name {name!r} shadows a grammar name")
                    exprs.append((f"{loc}.bind.{name}", name, text))
                    bind_names.append(name)
        exprs.append((f"{loc}.expr", None, pred.get("expr")))
        seen_binds: set[str] = set()
        bind_refs: dict[str, list[NameRef]] = {}
        expr_refs: list[NameRef] = []
        effective_bind_refs: dict[str, list[NameRef]] = {}
        effective_expr_refs: list[NameRef] = []
        parsed: list[tuple[str, str | None, ast.Expression]] = []
        parsed_all = True
        for eloc, bind_name, text in exprs:
            try:
                tree = parse_expr(text)
            except PrimaryEvidenceError as exc:
                v.append(f"{eloc}: {exc}")
                parsed_all = False
                if bind_name is not None:
                    seen_binds.add(bind_name)
                continue
            refs = expr_names(tree)
            parsed.append((eloc, bind_name, tree))
            if bind_name is None:
                expr_refs = refs
                effective_expr_refs = effective_names(tree)
            else:
                bind_refs[bind_name] = refs
                effective_bind_refs[bind_name] = effective_names(tree)
            for ref in refs:
                if ref.case is not None and ref.case not in targets:
                    v.append(f"{eloc}: at({ref.case!r}) is not one of this predicate's "
                             f"target_cases ({sorted(targets)})")
                    continue
                if ref.kind in ("capture", "comparand"):
                    if ref.name not in capture_names:
                        prefix = f"{_COMPARAND}." if ref.kind == "comparand" else ""
                        v.append(f"{eloc}: {prefix}{ref.point}.{ref.name} is not a snapshot "
                                 f"schema variable ({sorted(capture_names)})")
                elif ref.kind == _HISTORY:
                    if not history_declared:
                        v.append(f"{eloc}: history.{ref.name}: the IR declares no "
                                 "state_history required_evidence entry")
                    elif ref.name not in capture_names:
                        v.append(f"{eloc}: history.{ref.name} is not a snapshot schema "
                                 f"variable ({sorted(capture_names)})")
                elif ref.kind == _SERIES:
                    if series_names is None:
                        v.append(f"{eloc}: series.{ref.name}: the IR declares no step_series "
                                 "required_evidence entry")
                    elif ref.name not in series_names:
                        v.append(f"{eloc}: series.{ref.name} is not a name of the step_series "
                                 f"entry ({sorted(series_names)})")
                elif ref.kind == _DERIVED:
                    for cid in scope_cases:
                        if case_inputs is None:
                            # The case evaluation failed, and `drive_violations` (c) refuses
                            # the IR for that already; judge the NAME against what the case
                            # declares, so one evaluation defect is not repeated as a refusal
                            # of every `derived.*` read.
                            case = cases.get(cid)
                            declared = case.get(_DERIVED) if isinstance(case, dict) else None
                            known = isinstance(declared, dict) and ref.name in declared
                        else:
                            known = cid in case_inputs \
                                and f"{_DERIVED}.{ref.name}" in case_inputs[cid]
                        if not known:
                            v.append(f"{eloc}: in case {cid!r}: derived.{ref.name} is not a "
                                     "derived value the host evaluated for that case")
                elif ref.kind == "inputs":
                    for cid in ([ref.case] if ref.case is not None else scope_cases):
                        case = cases.get(cid)
                        inputs = case.get("inputs") if isinstance(case, dict) else None
                        try:
                            resolve_input_value(inputs, ref.name)
                        except PrimaryEvidenceError as exc:
                            v.append(f"{eloc}: in case {cid!r}: {exc}")
                elif ref.case is not None:
                    if ref.name not in coord_names:
                        v.append(f"{eloc}: at({ref.case!r}).{ref.name} is not a coordinate")
                elif ref.name in seen_binds or ref.name in coord_names or ref.name in CONSTANTS:
                    continue
                elif ref.name in bind_names:
                    v.append(f"{eloc}: bind {ref.name!r} is referenced before it is defined")
                else:
                    v.append(f"{eloc}: name {ref.name!r} is not a coordinate, a bind or a "
                             "constant")
            if bind_name is not None:
                seen_binds.add(bind_name)
        reads_derived = any(r.kind == _DERIVED for _, _, tree in parsed
                            for r in expr_names(tree))
        # Without the evaluated case inputs a `derived.*` read has no rank to infer; the IR is
        # refused by `drive_violations` (c) for the evaluation that failed.
        if parsed_all and len(v) == mark and not (reads_derived and case_inputs is None):
            rank_v = _predicate_rank_violation(pred, loc, parsed, targets, schema, variables,
                                               coord_names, cases, case_inputs)
            if rank_v is not None:
                v.append(rank_v)
        if parsed_all and is_zero_step_closure(pred, state) \
                and str(test_id).strip() not in closures:
            # gate (l)'s fixed form: gate-required, exempt from the rule below — ONE per test,
            # so a second one cannot stand for another quantity judged by the step count alone
            closures.add(str(test_id).strip())
            continue
        if parsed_all and not predicate_reads_state(expr_refs, bind_refs, state):
            v.append(f"{loc}: reads no captured state the kernel produced (final.<v> of a "
                     "variable an operation writes, initial.<v> of one init writes, history.*, "
                     "series.*) in expr or a "
                     "bind expr reaches: a predicate values the state the kernel produced (the "
                     "host's own initial_state read back, a derived.* value, final.step / "
                     "final.t, and a comparand.<point>.<var> — another target's state — are not "
                     "this run's kernel state)")
        elif parsed_all and not predicate_reads_state(effective_expr_refs, effective_bind_refs,
                                                      state):
            v.append(f"{loc}: reads no captured state the kernel produced (final.<var> / "
                     "initial.<var> / history.* / series.*) "
                     "outside a term multiplied by a literal zero, raised to a literal zero, or "
                     "subtracted from itself or added to its own negation (the same expression "
                     "spelled the same): the expression is constant in the captured values "
                     "and judges nothing (a cell count is the case's inputs.<grid path>; a "
                     "quantity no captured state enters takes the tests.md §5 primary-evidence "
                     "statement, its state term included)")
    return v


def _entering_refs(pred: dict[str, Any]) -> tuple[set[str], list[NameRef]] | None:
    """``(the cases the scope evaluates the predicate in, the references that ENTER its
    value)``: those in `expr` and in the binds `expr` reaches (`reached_binds`), outside a
    term constant by form (`effective_names`; the frontier of the reach walk is built from the
    PRUNED `expr`, so a bind named only inside such a term is not reached). None when the scope
    or an expression is malformed (the primary schema gate refuses those)."""
    targets = pred.get("target_cases")
    if not isinstance(targets, list) or not all(isinstance(c, str) for c in targets):
        return None
    if pred.get("per_case") is True and "case" not in pred:
        contexts = {c.strip() for c in targets}
    elif "case" not in pred or pred.get("per_case") or not isinstance(pred.get("case"), str) \
            or not pred["case"].strip():
        return None
    else:
        contexts = {pred["case"].strip()}
    bind = pred.get("bind")
    bind_refs: dict[str, list[NameRef]] = {}
    try:
        for name, text in (bind.items() if isinstance(bind, dict) else ()):
            bind_refs[name] = effective_names(parse_expr(text))
        expr_refs = effective_names(parse_expr(pred.get("expr")))
    except PrimaryEvidenceError:
        return None
    entering = list(expr_refs)
    for name in reached_binds(expr_refs, bind_refs):
        entering.extend(bind_refs[name])
    return contexts, entering


def cases_read(pred: dict[str, Any],
               state_names: StateReads | set[str] | frozenset[str]) -> set[str] | None:
    """The cases a primary predicate VALUES the state of: the cases a kernel-state read
    (`is_state_read` under ``state_names``: `final.<written v>`, `initial.<v init writes>`,
    `history.*`, `series.*` — or, given a plain set, `initial.<v>` / `final.<v>` with `<v>` in
    it; the time variable, the step count, an `inputs.<path>`, a `derived.*` value, a
    coordinate and a `comparand.` read are not state) that ENTERS THE VALUE (`_entering_refs`)
    names — an own-case read values every case the scope evaluates the predicate in (every
    target case under `per_case: true`, X under `case: X`), an `at('<case>')` read values that
    case. So a bind `expr` never reaches, an `at('<case>')` only inside a term constant by form,
    and an `at('<case>').inputs.<path>` normaliser or `at('<case>').final.t` beside own-case
    state (issue #567 review round 1) add no case. The record's `cases_read` key keeps its name.
    None when the scope or an expression is malformed (the primary schema gate refuses those;
    they cover nothing here)."""
    found = _entering_refs(pred)
    if found is None:
        return None
    contexts, entering = found
    state_reads_ = [r for r in entering if is_state_read(r, state_names)]
    read = set(contexts) if any(r.case is None for r in state_reads_) else set()
    read |= {r.case for r in state_reads_ if r.case is not None}
    return read


def point_reads(pred: dict[str, Any]) -> dict[tuple[str, str], set[str]]:
    """Per ``(point, name)`` — `initial` / `final` (own-case or `at('<case>')`), `history`,
    `series` — the cases a predicate's reads of it that ENTER the value (`_entering_refs`)
    value. Empty for a malformed predicate."""
    found = _entering_refs(pred)
    if found is None:
        return {}
    contexts, entering = found
    out: dict[tuple[str, str], set[str]] = {}
    for ref in entering:
        if ref.kind == "capture":
            key = (str(ref.point), ref.name)
        elif ref.kind in (_HISTORY, _SERIES):
            key = (ref.kind, ref.name)
        else:
            continue
        out.setdefault(key, set()).update(contexts if ref.case is None else {ref.case})
    return out


def record_scalar_pairing(pred: dict[str, Any],
                          one_call: dict[str, frozenset[str]]) -> dict[str, dict[str, list[str]]]:
    """What D7 asks of one predicate, stamped on its record (`evaluate_primary_predicates`) so
    `verdict_evaluator.evaluate_verdict` re-checks the pairing on the records alone: per one-call
    scalar ``s`` (`DriveModel.one_call_scalars`: ``s`` -> the array states the same operation
    writes) whose `final.<s>` this predicate reads, ``scalar`` — the cases it reads `final.<s>`
    in — and ``paired`` — the cases it reads BOTH `final.<s>` and `final.<a>` of one of those
    arrays in. A scalar it does not read is left out."""
    reads = point_reads(pred)
    out: dict[str, dict[str, list[str]]] = {}
    for scalar, arrays in sorted(one_call.items()):
        scalar_cases = reads.get(("final", scalar), set())
        if not scalar_cases:
            continue
        array_cases: set[str] = set()
        for array in arrays:
            array_cases |= reads.get(("final", array), set())
        out[scalar] = {"scalar": sorted(scalar_cases),
                       "paired": sorted(scalar_cases & array_cases)}
    return out


def quantity_violations(tests: Any, predicates: Any,
                        state_names: StateReads | set[str] | None = None) -> list[str]:
    """The per-test omission gate (issue #582, D10): over each `io_contract.tests[]` entry, the
    set of `quantity` over the test's predicates that are not cross-target must equal the test's
    `quantities`, which `validate_pipeline_semantics` holds to the `tests.md` line. A judgment the
    spec lists therefore cannot go unjudged, and a test always has a predicate its verdict
    evaluates. A cross-target predicate is out of the comparison: no run's verdict evaluates it
    (issue #383), so its `quantity` standing for a listed judgment would pass that judgment on
    every run, and the set of tests that carry one is pinned separately against `tests.md`
    (`CROSS_TARGET_JUDGMENT_PHRASE`). Names only: whether an expression measures that judgment
    is `Compile.verify` V3's. A malformed entry is the schema gates' to report.

    With ``state_names`` (a `StateReads`, or a set of names counted at both capture points),
    every target case of a test must be valued by some predicate of the test
    (`cases_read`): what remains of the coverage gate's reads-every-case rule without a condition
    to compare against. A `case: a` predicate over `[a, b]` as the test's only judgment would
    certify it on case a alone (Z6 PR-3 rounds 1 and 2). Per test and not per quantity: a
    refinement order reads the cases of its own pair, so a quantity may read fewer."""
    v: list[str] = []
    if not isinstance(tests, list):
        return v
    valued: dict[str, set[str]] = {}
    declared: dict[str, set[str]] = {}
    for test in tests:
        if not isinstance(test, dict) or not isinstance(test.get("test_id"), str) \
                or not isinstance(test.get("quantities"), list):
            continue
        declared[test["test_id"].strip()] = {q for q in test["quantities"] if isinstance(q, str)}
    carried: dict[str, set[str]] = {tid: set() for tid in declared}
    for pred in (predicates if isinstance(predicates, list) else []):
        if not isinstance(pred, dict) or not isinstance(pred.get("test_id"), str):
            continue
        tid = pred["test_id"].strip()
        if tid not in carried or not isinstance(pred.get("quantity"), str) \
                or is_cross_target_predicate(pred):
            continue
        carried[tid].add(pred["quantity"])
        if state_names is not None:
            valued.setdefault(tid, set()).update(cases_read(pred, state_names) or ())
    if state_names is not None:
        for test in tests:
            if not isinstance(test, dict) or not isinstance(test.get("test_id"), str) \
                    or not isinstance(test.get("target_cases"), list):
                continue
            tid = test["test_id"].strip()
            unread = sorted({c.strip() for c in test["target_cases"] if isinstance(c, str)}
                            - valued.get(tid, set()))
            if unread and carried.get(tid):
                v.append(f"test {tid!r}: no predicate values the captured state of target "
                         f"case(s) {unread}: a test certified without reading a case says "
                         "nothing about it. Read it with `per_case: true`, or through "
                         "at('<case>').<initial|final>.<var> in the value")
    for tid, listed in sorted(declared.items()):
        missing = sorted(listed - carried[tid])
        extra = sorted(carried[tid] - listed)
        if missing:
            v.append(f"test {tid!r}: no predicate (one that is not cross-target) judges "
                     f"{missing}, which its quantities line lists: add one entry with this "
                     "test_id and quantity")
        if extra:
            v.append(f"test {tid!r}: predicates judge {extra}, which its quantities line does "
                     "not list: tests.md defines what a test judges")
    return v


# ----------------------------------------------------------------------------------- drive
#
# The model of `drive` (issue #589, D1-D8): which operation the host-rendered driver calls, on
# what, and how often — and from it the classes of every schema variable (who writes it, who
# reads it), derived from `drive` and the published operations' §5.1 signatures alone, never
# from a label the leaf writes. A member of the verdict tuple: the classes decide what a
# predicate's read counts for, and the case-file writer below is the host's evaluation of the
# case inputs the driver reads.

#: The roots and words an `arguments` value may take (`parse_binding`).
_STATE_ROOT = "state"
_BINDING_WORDS = ("time", STEP_CAPTURE)
#: The keys of `drive.loop`, every one required with `loop` (gate (e)).
LOOP_KEYS: tuple[str, ...] = ("n_step", "dt", "t_start", "t_end")
#: The spec kinds the catalog answers with (`spec/registry/spec_catalog.yaml`).
DRIVE_REQUIRED_KINDS: frozenset[str] = frozenset({"problem", "component"})
INFRASTRUCTURE_KIND = "infrastructure"
#: The dummy types the driver binds; any other (a string, a derived type) is refused.
_DRIVEN_TYPES: frozenset[str] = frozenset({"real", "integer", "logical", "procedure"})
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class Binding(NamedTuple):
    """One `arguments` value: ``kind`` is ``inputs`` (``ref`` the dotted path), ``derived`` /
    ``state`` (``ref`` the name), ``time`` / ``step`` (``ref`` None), ``literal`` (``ref`` the
    number or bool) or ``procedure`` (``ref`` the expression text, D11)."""
    kind: str
    ref: Any

    def text(self) -> str:
        if self.kind in ("inputs", _DERIVED, _STATE_ROOT):
            return f"{self.kind}.{self.ref}"
        if self.kind == "procedure":
            return f"{{procedure: {self.ref!r}}}"
        return self.kind if self.kind in _BINDING_WORDS else repr(self.ref)


def parse_binding(value: Any) -> Binding:
    """Read one `drive` `arguments` value (SHAPE §2): `"inputs.<path>"`, `"derived.<name>"`,
    `"state.<var>"`, `"time"`, `"step"`, a finite number or a bool, or `{procedure: "<expr>"}`.
    Raises `PrimaryEvidenceError` on anything else."""
    if isinstance(value, bool):
        return Binding("literal", value)
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise PrimaryEvidenceError(f"binding {value!r} is not a finite number")
        return Binding("literal", value)
    if isinstance(value, dict):
        text = value.get("procedure")
        if set(value) == {"procedure"} and isinstance(text, str) and text.strip():
            return Binding("procedure", text.strip())
        raise PrimaryEvidenceError(
            f"binding {value!r}: the one mapping form is {{procedure: \"<expression>\"}}")
    if isinstance(value, str):
        text = value.strip()
        if text in _BINDING_WORDS:
            return Binding(text, None)
        root, _, rest = text.partition(".")
        segments = rest.split(".") if rest else []
        if root == _INPUTS_ROOT and segments and all(_IDENT.fullmatch(p) for p in segments):
            return Binding("inputs", rest)
        if root in (_DERIVED, _STATE_ROOT) and len(segments) == 1 \
                and _IDENT.fullmatch(segments[0]):
            return Binding(root, rest)
    raise PrimaryEvidenceError(
        f"binding {value!r} is not one of inputs.<path>, derived.<name>, state.<var>, time, "
        "step, a number, a bool, or {procedure: \"<expression>\"}")


class DriveEntry(NamedTuple):
    """One operation call of `drive`: ``role`` `init` or `step`, ``loc`` its IR location,
    ``operation`` the published operation, ``result`` its result dummy, ``arguments`` dummy ->
    `Binding`, ``dummies`` the signature's arguments in §5.1 order, ``cases`` the list form's
    `cases` (None in the mapping form and for `init`)."""
    role: str
    loc: str
    operation: str
    result: str
    arguments: dict[str, Binding]
    dummies: tuple[dict[str, Any], ...]
    cases: tuple[str, ...] | None

    def dummy(self, name: str) -> dict[str, Any] | None:
        return next((d for d in self.dummies if d.get("name") == name), None)


class DriveBlock(NamedTuple):
    """The parsed `drive` block: ``init`` (None when absent), the ``step`` entries,
    ``list_form`` (the `step` is a list of entries with `cases`), ``loop`` (key -> `Binding`,
    None when absent: one call per case) and ``history_every`` (the `inputs` path of
    `history.every`, None when absent)."""
    init: DriveEntry | None
    step: tuple[DriveEntry, ...]
    list_form: bool
    loop: dict[str, Binding] | None
    history_every: str | None

    def entries(self) -> list[DriveEntry]:
        return ([self.init] if self.init is not None else []) + list(self.step)

    def entries_for(self, case_id: str) -> list[DriveEntry]:
        """`init`, and the `step` entries that run ``case_id``."""
        return [e for e in self.entries() if e.cases is None or case_id in e.cases]


def _has_drive(ir: Any) -> bool:
    return isinstance(ir, dict) and ir.get("drive") is not None


def _published_operations(ir: dict[str, Any]) -> tuple[set[str], dict[str, dict[str, Any]]]:
    """``(published operation ids, symbol -> §5.1 procedure)`` of `public_api`."""
    api = ir.get("public_api") if isinstance(ir.get("public_api"), dict) else {}
    ops = api.get("published_operations")
    published = {o["operation_id"].strip() for o in (ops if isinstance(ops, list) else [])
                 if isinstance(o, dict) and isinstance(o.get("operation_id"), str)
                 and o["operation_id"].strip()}
    sigs: dict[str, dict[str, Any]] = {}
    entries = api.get("signatures")
    for entry in (entries if isinstance(entries, list) else []):
        if isinstance(entry, dict) and isinstance(entry.get("symbol"), str) \
                and isinstance(entry.get("signature"), dict):
            sigs[entry["symbol"].strip()] = entry["signature"]
    return published, sigs


def _dummy_type(dummy: dict[str, Any]) -> str | None:
    spec = dummy.get("spec")
    return spec.get("type") if isinstance(spec, dict) else None


def _dummy_rank(dummy: dict[str, Any]) -> int:
    rank = dummy.get("rank")
    if isinstance(rank, int) and not isinstance(rank, bool):
        return rank
    dims = dummy.get("dims")
    return len(dims) if isinstance(dims, list) else 0


def _is_logical_out(dummy: dict[str, Any]) -> bool:
    return dummy.get("intent") == "out" and _dummy_type(dummy) == "logical" \
        and _dummy_rank(dummy) == 0


def _parse_entry(raw: Any, role: str, loc: str, published: set[str],
                 sigs: dict[str, dict[str, Any]], list_form: bool,
                 v: list[str]) -> DriveEntry | None:
    """One `init` / `step` entry, its findings appended to ``v``; None when it has any."""
    mark = len(v)
    if not isinstance(raw, dict):
        v.append(f"{loc} must be a mapping {{operation, result, arguments}}")
        return None
    allowed = {"operation", "result", "arguments"} | ({"cases"} if list_form else set())
    for key in sorted(set(raw) - allowed, key=str):
        v.append(f"{loc}.{key} is not a key of a drive entry ({sorted(allowed)})")
    op = raw.get("operation")
    if not isinstance(op, str) or not op.strip():
        v.append(f"{loc}.operation must name a published operation")
        return None
    op = op.strip()
    if op not in published:
        v.append(f"{loc}.operation {op!r} is not a published operation "
                 f"(public_api.published_operations[].operation_id: {sorted(published)})")
    sig = sigs.get(op)
    args = sig.get("args") if isinstance(sig, dict) else None
    if not isinstance(args, list) \
            or not all(isinstance(a, dict) and isinstance(a.get("name"), str) for a in args):
        v.append(f"{loc}.operation {op!r} has no public_api.signatures entry with its args")
        return None
    dummies = tuple(args)
    names = [a["name"] for a in dummies]
    arguments_raw = raw.get("arguments")
    if not isinstance(arguments_raw, dict):
        v.append(f"{loc}.arguments must be a mapping of dummy -> binding")
        return None
    missing = [n for n in names if n not in arguments_raw]
    extra = sorted((str(k) for k in arguments_raw if k not in names))
    if missing:
        v.append(f"{loc}.arguments binds no value to dummy(ies) {missing} of {op}: every dummy "
                 "is bound exactly once")
    if extra:
        v.append(f"{loc}.arguments names {extra}, which are not dummies of {op} ({names})")
    arguments: dict[str, Binding] = {}
    for name in names:
        if name in arguments_raw:
            try:
                arguments[name] = parse_binding(arguments_raw[name])
            except PrimaryEvidenceError as exc:
                v.append(f"{loc}.arguments.{name}: {exc}")
    result = raw.get("result")
    if not isinstance(result, str) or not result.strip():
        v.append(f"{loc}.result must name the logical out dummy that reports the call")
        result = ""
    cases: tuple[str, ...] | None = None
    if list_form:
        raw_cases = raw.get("cases")
        if not isinstance(raw_cases, list) or not raw_cases \
                or not all(isinstance(c, str) and c.strip() for c in raw_cases):
            v.append(f"{loc}.cases must be a non-empty list of case_ids (the list form "
                     "partitions test_case_set)")
        else:
            cases = tuple(c.strip() for c in raw_cases)
    if len(v) != mark:
        return None
    return DriveEntry(role=role, loc=loc, operation=op, result=result.strip(),
                      arguments=arguments, dummies=dummies, cases=cases)


def _parse_drive(ir: dict[str, Any]) -> tuple[DriveBlock | None, list[str]]:
    """The `drive` block's STRUCTURE: every entry well formed, every operation published
    with a signature, every dummy bound once by the vocabulary, `loop` / `history` of the
    admitted keys and forms. ``(block, [])`` when it holds, ``(None, findings)`` otherwise."""
    v: list[str] = []
    drive = ir.get("drive")
    if not isinstance(drive, dict):
        return None, ["drive must be a mapping {init?, step, loop?, history?}"]
    for key in sorted(set(drive) - {"init", "step", "loop", "history"}, key=str):
        v.append(f"drive.{key} is not a key of drive (init, step, loop, history)")
    published, sigs = _published_operations(ir)
    init = None
    if drive.get("init") is not None:
        if isinstance(drive["init"], list):
            v.append("drive.init is one mapping entry, never a list")
        else:
            init = _parse_entry(drive["init"], "init", "drive.init", published, sigs, False, v)
    step_raw = drive.get("step")
    step: list[DriveEntry] = []
    list_form = isinstance(step_raw, list)
    if step_raw is None:
        v.append("drive.step is required: the operation the driver calls per case")
    elif list_form:
        if not step_raw:
            v.append("drive.step is an empty list")
        for i, raw in enumerate(step_raw):
            entry = _parse_entry(raw, "step", f"drive.step[{i}]", published, sigs, True, v)
            if entry is not None:
                step.append(entry)
    else:
        entry = _parse_entry(step_raw, "step", "drive.step", published, sigs, False, v)
        if entry is not None:
            step.append(entry)
    loop: dict[str, Binding] | None = None
    if drive.get("loop") is not None:
        raw_loop = drive["loop"]
        if not isinstance(raw_loop, dict):
            v.append(f"drive.loop must be a mapping of {list(LOOP_KEYS)}")
        else:
            for key in sorted(set(raw_loop) - set(LOOP_KEYS), key=str):
                v.append(f"drive.loop.{key} is not a loop key ({list(LOOP_KEYS)})")
            loop = {}
            for key in LOOP_KEYS:
                if raw_loop.get(key) is None:
                    v.append(f"drive.loop.{key} is required whenever loop is declared"
                             + (" (gate (e): the t_end consistency check needs it)"
                                if key == "t_end" else ""))
                    continue
                try:
                    binding = parse_binding(raw_loop[key])
                except PrimaryEvidenceError as exc:
                    v.append(f"drive.loop.{key}: {exc}")
                    continue
                if binding.kind not in ("inputs", _DERIVED, _STATE_ROOT):
                    v.append(f"drive.loop.{key} must be inputs.<path>, derived.<name> or "
                             f"state.<var> (got {binding.text()})")
                    continue
                loop[key] = binding
    every: str | None = None
    if drive.get("history") is not None:
        raw_history = drive["history"]
        binding = None
        if isinstance(raw_history, dict) and set(raw_history) == {"every"}:
            try:
                binding = parse_binding(raw_history["every"])
            except PrimaryEvidenceError:
                binding = None
        if binding is None or binding.kind != "inputs":
            v.append("drive.history must be {every: inputs.<path>}: the sampling stride is a "
                     "case input (gate (m) compares tests.md's number with it)")
        else:
            every = binding.ref
    if v:
        return None, v
    return DriveBlock(init=init, step=tuple(step), list_form=list_form, loop=loop,
                      history_every=every), []


def drive_block(ir: dict[str, Any]) -> DriveBlock | None:
    """The parsed `drive` block, None when the IR has none; raises `PrimaryEvidenceError`
    naming the first structural finding (`drive_violations` reports them all)."""
    if not _has_drive(ir):
        return None
    block, v = _parse_drive(ir)
    if block is None:
        raise PrimaryEvidenceError(v[0])
    return block


class DriveModel(NamedTuple):
    """The classes of every schema variable (SHAPE §3), derived from `drive` + signatures.

    ``init_written`` — bound to an `out` dummy of `init`; ``written`` — bound to an `out` /
    `inout` dummy of any entry; ``result_states`` — bound to an entry's `result:` dummy, or to
    any logical `out`; ``logical`` — bound to a logical `out` (starts at 0, no `initial_state`);
    ``read`` — bound to an `in` / `inout` dummy of a `step` entry; ``operation_only`` — written
    by `step`, read by no `step` dummy, not init-written, not logical (its `initial_state` is
    a number, the pre-call content); ``one_call_scalars`` — on a node without `loop`, a scalar
    bound to a rank-0 real / integer `out` of a `step` entry -> the array states the entries
    binding it write (D7); ``types`` — the `spec.type` of the dummies bound to each state."""
    block: DriveBlock
    variables: dict[str, list[str]]
    init_written: frozenset[str]
    written: frozenset[str]
    result_states: frozenset[str]
    logical: frozenset[str]
    read: frozenset[str]
    operation_only: frozenset[str]
    one_call_scalars: dict[str, frozenset[str]]
    types: dict[str, str]

    def needs_initial(self) -> frozenset[str]:
        """The variables every case gives an `initial_state` entry (gate (f))."""
        return frozenset(self.variables) - self.init_written - self.logical


def _model_from_block(block: DriveBlock, variables: dict[str, list[str]]) -> DriveModel:
    init_written: set[str] = set()
    written: set[str] = set()
    step_written: set[str] = set()
    result_states: set[str] = set()
    logical: set[str] = set()
    read: set[str] = set()
    one_call: dict[str, set[str]] = {}
    types: dict[str, str] = {}
    for entry in block.entries():
        entry_arrays = {b.ref for name, b in entry.arguments.items()
                        if b.kind == _STATE_ROOT and (d := entry.dummy(name)) is not None
                        and d.get("intent") in ("out", "inout") and _dummy_rank(d) >= 1}
        for dummy in entry.dummies:
            binding = entry.arguments[dummy["name"]]
            if binding.kind != _STATE_ROOT:
                continue
            var, intent = binding.ref, dummy.get("intent")
            if isinstance(_dummy_type(dummy), str):
                types.setdefault(var, _dummy_type(dummy))
            if entry.role == "init" and intent == "out":
                init_written.add(var)
            if intent in ("out", "inout"):
                written.add(var)
                if entry.role == "step":
                    step_written.add(var)
            if entry.role == "step" and intent in ("in", "inout"):
                read.add(var)
            if _is_logical_out(dummy):
                logical.add(var)
                result_states.add(var)
            if dummy["name"] == entry.result:
                result_states.add(var)
            if block.loop is None and entry.role == "step" and intent == "out" \
                    and _dummy_rank(dummy) == 0 and _dummy_type(dummy) in ("real", "integer"):
                one_call.setdefault(var, set()).update(entry_arrays)
    operation_only = step_written - read - init_written - logical
    return DriveModel(block=block, variables=dict(variables),
                      init_written=frozenset(init_written), written=frozenset(written),
                      result_states=frozenset(result_states), logical=frozenset(logical),
                      read=frozenset(read), operation_only=frozenset(operation_only),
                      one_call_scalars={s: frozenset(a) for s, a in one_call.items()},
                      types=types)


def drive_model(ir: dict[str, Any]) -> DriveModel:
    """The `DriveModel` of an IR that carries `drive`; raises `PrimaryEvidenceError` when it
    has none, or when its structure or its snapshot schema is malformed."""
    block = drive_block(ir)
    if block is None:
        raise PrimaryEvidenceError("the IR carries no drive block")
    schema = snapshot_schema(ir)
    if schema is None:
        raise PrimaryEvidenceError("drive needs a state_snapshots required_evidence entry (the "
                                   "schema variables it binds)")
    return _model_from_block(block, schema_variables(schema))


def state_reads(ir: dict[str, Any]) -> StateReads:
    """What counts as a kernel-state read in this IR's predicates (`StateReads`): from
    `drive_model` when the IR carries `drive` (raising on a malformed one); every schema
    variable at both capture points when it carries none (an `infrastructure` node)."""
    if _has_drive(ir):
        model = drive_model(ir)
        return StateReads(final=model.written, initial=model.init_written,
                          loop=model.block.loop is not None)
    schema = snapshot_schema(ir)
    names = frozenset(schema_variables(schema)) if schema is not None else frozenset()
    return StateReads(final=names, initial=names, loop=False)


# ------------------------------------------------------------------------- case evaluation

def _integral(value: float, what: str) -> int:
    if not float(value).is_integer():
        raise PrimaryEvidenceError(f"{what} is {value!r}, not an integer")
    return int(value)


def _extent(token: str, entry: DriveEntry, inputs: Any, derived: dict[str, Any],
            what: str) -> int:
    """One dimension token evaluated under ``entry``'s bindings (gate (c)): an integer
    literal, a rank-0 dummy of the operation bound to `inputs.*`, `derived.*` or a number,
    and `+ - *` / unary minus between them. The extent is an integer >= 1."""
    try:
        tree = ast.parse(token.strip(), mode="eval")
    except SyntaxError:
        raise PrimaryEvidenceError(f"{what}: dims token {token!r} does not parse") from None

    def ev(node: ast.AST) -> int:
        if isinstance(node, ast.Constant) and isinstance(node.value, int) \
                and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.Name):
            dummy = entry.dummy(node.id)
            if dummy is None or _dummy_rank(dummy) != 0:
                raise PrimaryEvidenceError(
                    f"{what}: dims token {token!r} names {node.id!r}, which is not a rank-0 "
                    f"dummy of {entry.operation}")
            binding = entry.arguments[node.id]
            if binding.kind == "inputs":
                value = resolve_input_number(inputs, binding.ref)
            elif binding.kind == _DERIVED and binding.ref in derived \
                    and np.ndim(derived[binding.ref]) == 0:
                value = float(derived[binding.ref])
            elif binding.kind == "literal" and _is_number(binding.ref):
                value = float(binding.ref)
            else:
                raise PrimaryEvidenceError(
                    f"{what}: dims token {token!r} names dummy {node.id!r}, bound to "
                    f"{binding.text()}: an extent resolves only through a dummy bound to "
                    "inputs.<path>, a scalar derived.<name> or a number")
            return _integral(value, f"{what}: {node.id}")
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult)):
            left, right = ev(node.left), ev(node.right)
            return left + right if isinstance(node.op, ast.Add) else (
                left - right if isinstance(node.op, ast.Sub) else left * right)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -ev(node.operand)
        raise PrimaryEvidenceError(
            f"{what}: dims token {token!r} is not an integer, a dummy name, or + - * of them")

    extent = ev(tree.body)
    if extent < 1:
        raise PrimaryEvidenceError(f"{what}: dims token {token!r} evaluates to {extent}, "
                                   "not an extent >= 1")
    return extent


def _dummy_extents(entry: DriveEntry, dummy: dict[str, Any], tokens: list[str] | None,
                   inputs: Any, derived: dict[str, Any], what: str) -> tuple[int, ...]:
    """The extents of an array dummy: its `dims` (a `:` token reads none), else ``tokens``
    (the bound state's `shape_expr`), each evaluated under the entry's bindings."""
    dims = dummy.get("dims")
    if not (isinstance(dims, list) and dims and all(isinstance(t, str) and t.strip() != ":"
                                                    for t in dims)):
        if tokens is None:
            raise PrimaryEvidenceError(
                f"{what}: the array dummy has no dims, and what it is bound to has no "
                "shape_expr to evaluate instead")
        dims = tokens
    return tuple(_extent(str(t), entry, inputs, derived, what) for t in dims)


def _fit(value: Any, shape: tuple[int, ...], what: str) -> Any:
    """An evaluated value made to fill ``shape`` (SHAPE §4.2): a scalar fills any shape; an
    array has its LEADING axes of extent 1 dropped while its rank exceeds the target's, then
    must have the target's rank with every extent equal or 1, and broadcasts. Anything else is
    refused."""
    arr = np.asarray(value, dtype=np.float64)
    if arr.ndim == 0:
        return float(arr) if not shape else np.full(shape, float(arr))
    while arr.ndim > len(shape) and arr.shape[0] == 1:
        arr = arr[0]
    if arr.ndim == 0:
        return float(arr) if not shape else np.full(shape, float(arr))
    if arr.ndim != len(shape) \
            or any(a != t and a != 1 for a, t in zip(arr.shape, shape)):
        raise PrimaryEvidenceError(
            f"{what}: a value of shape {list(np.shape(value))} does not fill the shape "
            f"{list(shape)} (leading extent-1 axes drop, then equal extents or 1 broadcast)")
    return np.ascontiguousarray(np.broadcast_to(arr, shape))


def _host_expr_violation(refs: list[NameRef], coords: Any, *, derived_before: Any = None,
                         initial_vars: Any = None) -> str | None:
    """Gate (g): what an `initial_state` expression (``derived_before`` None) or a `derived`
    expression may name. Both: `inputs.*`, a coordinate, a constant. A `derived` expression
    also: an EARLIER `derived.<name>`, and `initial.<v>` of a variable that has an
    `initial_state` entry (host-materialised). Everything else — `final.*`, `history.*`,
    `series.*`, `at(...)`, `comparand.*`, an `initial.<v>` `init` writes — is refused."""
    for ref in refs:
        if ref.case is not None:
            return "at('<case>') is not admitted in a host-evaluated expression"
        if ref.kind == "inputs":
            continue
        if ref.kind == "name":
            if ref.name in coords or ref.name in CONSTANTS:
                continue
            return f"name {ref.name!r} is not a coordinate or a constant"
        if derived_before is not None and ref.kind == _DERIVED:
            if ref.name in derived_before:
                continue
            return f"derived.{ref.name} is not a derived entry written before this one"
        if derived_before is not None and ref.kind == "capture" and ref.point == "initial" \
                and ref.name in initial_vars:
            continue
        what = (f"{ref.point}.{ref.name}" if ref.kind in ("capture", "comparand")
                else f"{ref.kind}.{ref.name}")
        if ref.kind == "comparand":
            what = f"{_COMPARAND}.{what}"
        return (f"{what} is not admitted here: "
                + ("an initial_state expression reads inputs.*, coordinates and constants only"
                   if derived_before is None else
                   "a derived expression reads inputs.*, coordinates, constants, earlier "
                   "derived.<name> and initial.<v> of a variable with an initial_state entry"))
    return None


class CaseValues(NamedTuple):
    """One case as the host evaluated it: ``shapes`` (state -> extents, `()` for a scalar),
    ``initial`` (each `initial_state` entry: a float or a float64 array), ``derived`` (each
    `derived` entry, fitted to the array dummy it is bound to), ``inputs`` (each `inputs.*`
    path the drive block references -> its authored value)."""
    shapes: dict[str, tuple[int, ...]]
    initial: dict[str, Any]
    derived: dict[str, Any]
    inputs: dict[str, Any]
    derived_ints: frozenset[str]


def _input_paths(block: DriveBlock, entries: list[DriveEntry]) -> list[str]:
    paths: list[str] = []
    for entry in entries:
        paths.extend(b.ref for b in entry.arguments.values() if b.kind == "inputs")
    paths.extend(b.ref for b in (block.loop or {}).values() if b.kind == "inputs")
    if block.history_every is not None:
        paths.append(block.history_every)
    return sorted(set(paths))


def _evaluate_case(model: DriveModel, schema: dict[str, Any],
                   case: dict[str, Any]) -> CaseValues:
    """SHAPE §4 steps 1-4 for one case; raises `PrimaryEvidenceError` naming the entry."""
    block = model.block
    cid = str(case.get("case_id")).strip()
    inputs = case.get("inputs") if isinstance(case.get("inputs"), dict) else {}
    derived_raw = case.get("derived", {})
    initial_raw = case.get("initial_state", {})
    if derived_raw is None:
        derived_raw = {}
    if initial_raw is None:
        initial_raw = {}
    if not isinstance(derived_raw, dict):
        raise PrimaryEvidenceError("derived must be a mapping of name -> number | expression")
    if not isinstance(initial_raw, dict):
        raise PrimaryEvidenceError("initial_state must be a mapping of variable -> value")
    coords = coordinate_arrays(schema, inputs, None)
    entries = block.entries_for(cid)

    # The derived values a dims token may need: every entry that reads no `initial.*`,
    # evaluated in order (the full pass below re-evaluates each and reports its error).
    pre: dict[str, Any] = {}
    pre_env = CaseEnv(case_id=cid, initial=None, final={}, inputs=inputs, coordinates=coords,
                      derived=pre)
    for name, text in derived_raw.items():
        try:
            pre[str(name)] = (float(text) if _is_number(text)
                              else evaluate(parse_expr(text), pre_env))
        except (PrimaryEvidenceError, TypeError, ValueError):
            continue

    # 1. shapes
    shapes: dict[str, tuple[int, ...]] = {}
    for var, dims in model.variables.items():
        if not dims:
            shapes[var] = ()
    for entry in entries:
        for dummy in entry.dummies:
            binding = entry.arguments[dummy["name"]]
            if binding.kind != _STATE_ROOT or _dummy_rank(dummy) == 0:
                continue
            var = binding.ref
            what = f"{entry.loc}.arguments.{dummy['name']} (state.{var})"
            ext = _dummy_extents(entry, dummy, model.variables.get(var), inputs, pre, what)
            if var in shapes and shapes[var] != ext:
                raise PrimaryEvidenceError(
                    f"{what}: extents {list(ext)}, while another dummy bound to state.{var} "
                    f"gives {list(shapes[var])}")
            shapes[var] = ext
    for var, dims in model.variables.items():
        if var not in shapes:
            raise PrimaryEvidenceError(
                f"state.{var} is an array no operation entry that runs this case binds, so its "
                "extents are unknown")
        for axis, token in enumerate(dims):
            if token.isdigit() and int(token) != shapes[var][axis]:
                raise PrimaryEvidenceError(
                    f"state.{var}: shape_expr extent {token} on axis {axis} disagrees with the "
                    f"bound dummies' {shapes[var][axis]}")
    rank = state_rank(schema)
    largest = {shapes[v] for v, d in model.variables.items() if len(d) == rank and rank > 0}
    for spec in (schema.get("coordinates") or []):
        if not isinstance(spec, dict) or not isinstance(spec.get("axis"), int):
            continue
        n = int(_coordinate_param(spec, "count", inputs))
        if largest and all(shape[spec["axis"]] != n for shape in largest):
            raise PrimaryEvidenceError(
                f"coordinates[{spec.get('name')!r}].count {n} is not the extent of axis "
                f"{spec['axis']} of the state ({sorted(map(list, largest))})")

    # 2. initial_state
    for key in initial_raw:
        if key not in model.variables:
            raise PrimaryEvidenceError(f"initial_state.{key} is not a snapshot schema variable")
        if key in model.init_written or key in model.logical:
            raise PrimaryEvidenceError(
                f"initial_state.{key}: a variable init writes, or a logical result the driver "
                "writes, takes no initial_state entry (the driver starts it at 0)")
    missing = sorted(model.needs_initial() - set(initial_raw))
    if missing:
        raise PrimaryEvidenceError(f"initial_state has no entry for {missing}")
    initial_env = CaseEnv(case_id=cid, initial=None, final={}, inputs=inputs,
                          coordinates=coords)
    initial: dict[str, Any] = {}
    for var, raw in initial_raw.items():
        what = f"initial_state.{var}"
        shape = shapes[var]
        if var in model.operation_only and not _is_number(raw):
            raise PrimaryEvidenceError(
                f"{what}: an output the operation only writes takes a number literal, its "
                "pre-call content broadcast to the shape")
        if _is_number(raw):
            value = _fit(_finite(float(raw), what), shape, what)
        elif isinstance(raw, list):
            if _contains_non_number(raw):
                raise PrimaryEvidenceError(f"{what}: a list holding something other than numbers")
            try:
                arr = np.asarray(raw, dtype=np.float64)
            except (TypeError, ValueError):
                raise PrimaryEvidenceError(f"{what}: not a rectangular numeric list") from None
            if arr.shape != shape:
                raise PrimaryEvidenceError(
                    f"{what}: a literal of shape {list(arr.shape)}, the state's is {list(shape)}")
            value = _finite(arr, what)
        elif isinstance(raw, str):
            tree = parse_expr(raw)
            bad = _host_expr_violation(expr_names(tree), coords)
            if bad is not None:
                raise PrimaryEvidenceError(f"{what}: {bad}")
            try:
                value = _fit(evaluate(tree, initial_env), shape, what)
            except PrimaryEvidenceError as exc:
                raise PrimaryEvidenceError(
                    str(exc) if str(exc).startswith(what) else f"{what}: {exc}") from None
        else:
            raise PrimaryEvidenceError(f"{what}: a number, a nested list or an expression")
        _finite(value, what)
        if model.types.get(var) == "integer":
            _integral_array(value, what)
        initial[var] = value

    # 3. derived, in mapping order
    derived: dict[str, Any] = {}
    ints: set[str] = set()
    derived_env = CaseEnv(case_id=cid, initial=initial, final={}, inputs=inputs,
                          coordinates=coords, derived=derived)
    for name, raw in derived_raw.items():
        what = f"derived.{name}"
        if not isinstance(name, str) or not _IDENT.fullmatch(name):
            raise PrimaryEvidenceError(f"{what}: a derived name is an identifier")
        if _is_number(raw):
            derived[name] = _finite(float(raw), what)
            if isinstance(raw, int):
                ints.add(name)
            continue
        if not isinstance(raw, str):
            raise PrimaryEvidenceError(f"{what}: a number or an expression")
        tree = parse_expr(raw)
        bad = _host_expr_violation(expr_names(tree), coords, derived_before=set(derived),
                                   initial_vars=set(initial))
        if bad is not None:
            raise PrimaryEvidenceError(f"{what}: {bad}")
        try:
            derived[name] = _finite(evaluate(tree, derived_env), what)
        except PrimaryEvidenceError as exc:
            raise PrimaryEvidenceError(
                str(exc) if str(exc).startswith(what) else f"{what}: {exc}") from None

    # bound host data: shapes, types
    fitted: dict[str, tuple[int, ...]] = {}
    for entry in entries:
        for dummy in entry.dummies:
            binding = entry.arguments[dummy["name"]]
            what = f"{entry.loc}.arguments.{dummy['name']} ({binding.text()})"
            if binding.kind == "inputs":
                value = resolve_input_value(inputs, binding.ref)
            elif binding.kind == _DERIVED:
                if binding.ref not in derived:
                    raise PrimaryEvidenceError(f"{what}: the case has no derived.{binding.ref}")
                value = derived[binding.ref]
            elif binding.kind == "literal":
                value = binding.ref
            else:
                continue
            rank_ = _dummy_rank(dummy)
            if rank_ == 0:
                if np.ndim(value) != 0:
                    raise PrimaryEvidenceError(f"{what}: an array bound to a rank-0 dummy")
            else:
                dims = _dummy_extents(entry, dummy, None, inputs, pre, what)
                if binding.kind == "inputs":
                    if tuple(np.shape(value)) != dims:
                        raise PrimaryEvidenceError(
                            f"{what}: an input of shape {list(np.shape(value))}, the dummy's "
                            f"dims evaluate to {list(dims)}")
                else:
                    value = _fit(value, dims, what)
                    if binding.ref in fitted and fitted[binding.ref] != dims:
                        raise PrimaryEvidenceError(
                            f"{what}: derived.{binding.ref} is bound to dummies of extents "
                            f"{list(fitted[binding.ref])} and {list(dims)}")
                    fitted[binding.ref] = dims
                    derived[binding.ref] = value
            if _dummy_type(dummy) == "integer" and binding.kind != "literal":
                _integral_array(value, what)
                if binding.kind == _DERIVED:
                    ints.add(binding.ref)

    # 4. the loop and history refusals
    host: dict[str, float] = {}
    for key, binding in (block.loop or {}).items():
        what = f"drive.loop.{key} ({binding.text()})"
        if binding.kind == _STATE_ROOT:
            continue
        value = (resolve_input_value(inputs, binding.ref) if binding.kind == "inputs"
                 else derived.get(binding.ref))
        if value is None or np.ndim(value) != 0:
            raise PrimaryEvidenceError(f"{what}: not a scalar of this case")
        host[key] = float(value)
        if key == "n_step":
            if not (float(value).is_integer() and value >= 1):
                raise PrimaryEvidenceError(f"{what} is {value!r}, not an integer >= 1")
            if binding.kind == _DERIVED:
                ints.add(binding.ref)
        if key == "dt" and not value > 0:
            raise PrimaryEvidenceError(f"{what} is {value!r}, not > 0")
    if len(host) == len(LOOP_KEYS):
        drift = abs(host["t_start"] + host["n_step"] * host["dt"] - host["t_end"])
        bound = 4 * float(np.spacing(abs(host["t_end"])))
        if not drift <= bound:
            raise PrimaryEvidenceError(
                f"drive.loop: t_start + n_step * dt = "
                f"{host['t_start'] + host['n_step'] * host['dt']!r} is not t_end "
                f"{host['t_end']!r} within 4 ulp ({bound!r}): the loop does not end at t_end")
    if block.history_every is not None:
        every = resolve_input_value(inputs, block.history_every)
        if np.ndim(every) != 0 or not (float(every).is_integer() and every >= 0):
            raise PrimaryEvidenceError(
                f"drive.history.every (inputs.{block.history_every}) is {every!r}, not an "
                "integer >= 0")

    used = {}
    for path in _input_paths(block, entries):
        resolve_input_value(inputs, path)   # refuses an absent, non-numeric or non-finite one
        used[path] = _lookup_input(inputs, path)[1]
    return CaseValues(shapes=shapes, initial=initial, derived=derived, inputs=used,
                      derived_ints=frozenset(ints))


def _integral_array(value: Any, what: str) -> None:
    arr = np.asarray(value, dtype=np.float64)
    if not np.all(arr == np.floor(arr)):
        raise PrimaryEvidenceError(f"{what}: an integer is bound, and the value is not integral")


def _evaluate_cases(ir: dict[str, Any]) -> dict[str, CaseValues]:
    """`_evaluate_case` over every case of an IR that carries `drive` (each error prefixed
    with the case); an IR with none has no derived value and evaluates nothing here."""
    cases = _cases_by_id(ir)
    if not _has_drive(ir):
        return {cid: CaseValues({}, {}, {}, {}, frozenset()) for cid in cases}
    model = drive_model(ir)
    schema = snapshot_schema(ir) or {}
    out: dict[str, CaseValues] = {}
    for cid, case in sorted(cases.items()):
        try:
            out[cid] = _evaluate_case(model, schema, case)
        except PrimaryEvidenceError as exc:
            raise PrimaryEvidenceError(f"case {cid!r}: {exc}") from None
    return out


def _json_number(value: Any, as_int: bool) -> Any:
    if isinstance(value, np.ndarray):
        return value.astype(np.int64).tolist() if as_int else value.astype(np.float64).tolist()
    return int(value) if as_int else float(value)


def _authored_json(value: Any, what: str) -> Any:
    """An authored input value as the case file carries it: an int stays an int, a float a
    float, a list keeps ints when every leaf is one and is floats otherwise."""
    if isinstance(value, bool):
        raise PrimaryEvidenceError(f"{what} is a bool, not a number")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return _finite(value, what)
    if isinstance(value, list):
        arr = np.asarray(value, dtype=np.float64)
        _finite(arr, what)
        leaves = arr.size == 0 or _all_ints(value)
        return np.asarray(value).astype(np.int64).tolist() if leaves else arr.tolist()
    raise PrimaryEvidenceError(f"{what} is not a number or a numeric list")


def _all_ints(value: Any) -> bool:
    if isinstance(value, list):
        return all(_all_ints(v) for v in value)
    return isinstance(value, int) and not isinstance(value, bool)


def _infrastructure_payload(inputs: Any) -> dict[str, Any]:
    """An IR with no `drive` (D10): every NUMERIC `inputs.*` leaf — a number or a bool (as
    1 / 0), or a rectangular list of them nested at most `MAX_INPUT_RANK` deep, empty admitted
    — under `inputs.<path>`; strings and every other leaf are skipped, a non-finite number is
    refused."""
    out: dict[str, Any] = {}

    def walk(node: Any, prefix: str) -> None:
        for key, value in node.items():
            path = f"{prefix}.{key}"
            if isinstance(value, dict):
                walk(value, path)
            elif isinstance(value, bool):
                out[path] = int(value)
            elif isinstance(value, (int, float)):
                out[path] = value if isinstance(value, int) else _finite(float(value), path)
            elif isinstance(value, list) and not _contains_non_number(value):
                try:
                    arr = np.asarray(value, dtype=np.float64)
                except (TypeError, ValueError):
                    continue   # ragged: not an array, skipped
                if arr.ndim > MAX_INPUT_RANK or (arr.size and arr.dtype == object):
                    continue
                _finite(arr, path)
                out[path] = (np.asarray(value).astype(np.int64).tolist()
                             if arr.size and _all_ints_or_bools(value) else arr.tolist())

    if isinstance(inputs, dict):
        walk(inputs, _INPUTS_ROOT)
    return out


def _all_ints_or_bools(value: Any) -> bool:
    if isinstance(value, list):
        return all(_all_ints_or_bools(v) for v in value)
    return isinstance(value, int)


def evaluate_case_inputs(ir: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The host's evaluation of every case's inputs (issue #589 D2, SHAPE §4): case_id -> the
    one flat JSON object the driver reads (`write_case_inputs`). For an IR with `drive`: the
    state extents from the bound dummies' `dims` (gate (c)), every `initial_state` and
    `derived` entry evaluated with the predicate grammar (gate (g)'s names), the bound host
    data's shapes, the loop scalars that are host data (`n_step` an integer >= 1, `dt` > 0, the
    `t_end` check when all four are host data) and `history.every` (an integer >= 0); the
    object holds `inputs.<path>` for every input the block references, `derived.<name>` for
    every derived entry and `<var>` for every `initial_state` entry. For an IR with none:
    every numeric `inputs.*` leaf. Raises `PrimaryEvidenceError` naming the case and the entry.
    `Compile.static` calls it as a gate (and writes nothing)."""
    cases = _cases_by_id(ir)
    if not _has_drive(ir):
        return {cid: _infrastructure_payload(case.get("inputs")) for cid, case in cases.items()}
    model = drive_model(ir)
    out: dict[str, dict[str, Any]] = {}
    for cid, values in _evaluate_cases(ir).items():
        payload: dict[str, Any] = {}
        for path, value in values.inputs.items():
            payload[f"inputs.{path}"] = _authored_json(value, f"case {cid!r}: inputs.{path}")
        for name, value in values.derived.items():
            payload[f"{_DERIVED}.{name}"] = _json_number(value, name in values.derived_ints)
        for var, value in values.initial.items():
            payload[var] = _json_number(value, model.types.get(var) == "integer")
        out[cid] = payload
    return out


#: The directory beside `spec.ir.yaml` the case files are written to.
CASE_INPUTS_DIRNAME = "case_inputs"


def write_case_inputs(ir: dict[str, Any], ir_dir: Path) -> list[Path]:
    """Write `<ir_dir>/case_inputs/<case_id>.json` for every case (`evaluate_case_inputs`),
    each through a temporary name of its own beside it (`tempfile.mkstemp`) and `os.replace`,
    so a reader never sees a truncated file and two writers never share a temporary name. The
    directory is created when absent and never removed, and no other file in it is touched.
    Bytes are deterministic: sorted keys, finite numbers only (`allow_nan=False`). Returns the
    paths written, in case order."""
    from tools.spec_input_gates import CASE_ID_TOKEN_RE   # function-level: render-free import
    payloads = evaluate_case_inputs(ir)
    for cid in payloads:
        if not CASE_ID_TOKEN_RE.match(cid) or ".." in cid:
            raise PrimaryEvidenceError(f"case_id {cid!r} is not a safe file name token")
    out_dir = Path(ir_dir) / CASE_INPUTS_DIRNAME
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for cid in sorted(payloads):
        data = (json.dumps(payloads[cid], sort_keys=True, allow_nan=False,
                           separators=(",", ":")) + "\n").encode("utf-8")
        fd, tmp = tempfile.mkstemp(dir=out_dir, prefix=f".{cid}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(tmp, out_dir / f"{cid}.json")
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        written.append(out_dir / f"{cid}.json")
    return written


# ----------------------------------------------------------------------------- drive gates

def _evidence_entries(ir: dict[str, Any]) -> dict[str, dict[str, Any]]:
    io = ir.get("io_contract") if isinstance(ir.get("io_contract"), dict) else {}
    rr = io.get("raw_requirements") if isinstance(io.get("raw_requirements"), dict) else {}
    entries = rr.get("required_evidence")
    out: dict[str, dict[str, Any]] = {}
    for entry in (entries if isinstance(entries, list) else []):
        if isinstance(entry, dict) and isinstance(entry.get("artifact"), str):
            out.setdefault(entry["artifact"], entry)
    return out


def step_series_names(ir: dict[str, Any]) -> frozenset[str] | None:
    """The `names` of the `step_series` evidence entry, None when none is declared."""
    entry = _evidence_entries(ir).get("step_series")
    if entry is None:
        return None
    names = entry.get("names")
    return frozenset(n for n in (names if isinstance(names, list) else []) if isinstance(n, str))


def predicate_schema_context(ir: dict[str, Any]) -> dict[str, Any]:
    """The grammar 6 keyword arguments of `validate_primary_predicate_schema` for ``ir``:
    `reads` (`state_reads`; the pre-drive rule when the `drive` block is malformed, which
    `drive_violations` refuses on its own), `case_inputs` (`evaluate_case_inputs`, None when it
    raises — `Compile.static` reports that itself through `drive_violations` (c) — and a
    `derived.*` read is then judged against the names the case declares),
    `series_names` and `history_declared`."""
    try:
        reads: StateReads | None = state_reads(ir)
    except PrimaryEvidenceError:
        reads = None
    try:
        case_inputs: dict[str, dict[str, Any]] | None = evaluate_case_inputs(ir)
    except PrimaryEvidenceError:
        case_inputs = None
    return {"reads": reads, "case_inputs": case_inputs,
            "series_names": step_series_names(ir),
            "history_declared": "state_history" in _evidence_entries(ir)}


def _test_entries(ir: dict[str, Any]) -> list[dict[str, Any]]:
    io = ir.get("io_contract") if isinstance(ir.get("io_contract"), dict) else {}
    tests = io.get("tests")
    return [t for t in (tests if isinstance(tests, list) else [])
            if isinstance(t, dict) and isinstance(t.get("test_id"), str)]


def _own_predicates(ir: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    """``(index, predicate)`` of every well-shaped predicate that is not cross-target."""
    return [(i, p) for i, p in enumerate(primary_predicates(ir))
            if isinstance(p, dict) and isinstance(p.get("test_id"), str)
            and isinstance(p.get("quantity"), str) and not is_cross_target_predicate(p)]


def _norm(text: Any) -> str:
    return re.sub(r"\s+", "", text) if isinstance(text, str) else ""


def _covers_test(pred: dict[str, Any], test: dict[str, Any]) -> bool:
    targets = pred.get("target_cases")
    return pred.get("per_case") is True and "case" not in pred and isinstance(targets, list) \
        and {str(c).strip() for c in targets} \
        == {str(c).strip() for c in (test.get("target_cases") or [])}


def scalar_pairing_violations(ir: dict[str, Any]) -> list[str]:
    """D7 (gate (i)): a predicate that reads `final.<s>`, `s` a one-call scalar
    (`DriveModel.one_call_scalars`: the scalar a rank-0 real / integer `out` of a `step` entry
    writes on a node without `loop`), needs ANOTHER predicate of the SAME test and quantity, not
    cross-target, that reads `final.<s>` in every case it reads `s` in, and in each of those
    cases one of the two reads `final.<a>` of an array state the same operation writes
    (`scalar_pairs`; reads that enter the value, `point_reads`). A scalar a kernel reports is
    judged beside the state its call wrote, by a predicate other than the one reading it: a
    sole predicate reading both — `final.s + 1e-300 * sum(final.F)` — is judged by `s` alone,
    the spelling the retired `reported_read_violations` refused (review round 1, R1-4). An IR
    with no `drive`, or a malformed one (`drive_violations` reports it), gives none here."""
    if not _has_drive(ir):
        return []
    try:
        model = drive_model(ir)
    except PrimaryEvidenceError:
        return []
    if not model.one_call_scalars:
        return []
    own = [(i, p, record_scalar_pairing(p, model.one_call_scalars))
           for i, p in _own_predicates(ir)]
    v: list[str] = []
    for idx, pred, pairing in own:
        tid, quantity = pred["test_id"].strip(), pred["quantity"]
        for scalar, facts in pairing.items():
            cases = set(facts["scalar"])
            paired = any(
                j != idx and p["test_id"].strip() == tid and p["quantity"] == quantity
                and scalar in other and scalar_pairs(cases, facts, other[scalar])
                for j, p, other in own)
            if not paired:
                v.append(
                    f"predicates[{idx}] (test {tid!r}, quantity {quantity!r}) reads final."
                    f"{scalar} — the scalar a rank-0 out of a one-call operation writes — in "
                    f"{sorted(cases)}, and no OTHER predicate of that test and quantity reads "
                    f"final.{scalar} in those cases with final.<a> of an array state the same "
                    f"operation writes ({sorted(model.one_call_scalars[scalar])}) read beside "
                    "it by one of the two (D7: a kernel-reported scalar is judged beside the "
                    "state its call wrote, by a predicate other than the one reading it)")
    return v


def _binding_violations(model: DriveModel, schema_vars: dict[str, list[str]],
                        v: list[str]) -> None:
    """Gate (b): each binding admitted for its dummy's intent, rank and type (SHAPE §2)."""
    block = model.block
    bound: set[str] = set()
    for entry in block.entries():
        reading: dict[str, list[str]] = {}
        writing: dict[str, list[str]] = {}
        for dummy in entry.dummies:
            name = dummy["name"]
            binding = entry.arguments[name]
            loc = f"{entry.loc}.arguments.{name}"
            dtype, rank, intent = _dummy_type(dummy), _dummy_rank(dummy), dummy.get("intent")
            if dtype not in _DRIVEN_TYPES:
                v.append(f"{loc}: a dummy of type {dtype!r} cannot be driven (the driver binds "
                         "real, integer, logical and procedure dummies)")
                continue
            if dtype == "procedure" or binding.kind == "procedure":
                if dtype != "procedure" or binding.kind != "procedure":
                    v.append(f"{loc}: {{procedure: ...}} binds a procedure-typed dummy, and a "
                             "procedure-typed dummy takes nothing else (D11)")
                continue
            if binding.kind == _STATE_ROOT:
                var = binding.ref
                if var not in schema_vars:
                    v.append(f"{loc}: state.{var} is not a snapshot schema variable")
                    continue
                bound.add(var)
                if len(schema_vars[var]) != rank:
                    v.append(f"{loc}: a rank-{rank} dummy bound to state.{var}, whose "
                             f"shape_expr has rank {len(schema_vars[var])}")
                    continue
                if model.types.get(var) != dtype:
                    v.append(f"{loc}: state.{var} is bound to dummies of types "
                             f"{model.types.get(var)!r} and {dtype!r}")
                if intent in ("in", "inout"):
                    reading.setdefault(var, []).append(name)
                if intent in ("out", "inout"):
                    writing.setdefault(var, []).append(name)
            if intent == "inout":
                if rank == 0:
                    v.append(f"{loc}: a rank-0 inout dummy is refused (an inout is a state "
                             "array the call updates)")
                elif binding.kind != _STATE_ROOT:
                    v.append(f"{loc}: an inout dummy binds state.<var> only "
                             f"(got {binding.text()})")
            elif intent == "out":
                if binding.kind != _STATE_ROOT:
                    v.append(f"{loc}: an out dummy binds state.<var> only (got {binding.text()})")
                elif rank == 0 and dtype in ("real", "integer"):
                    if entry.role == "step" and block.loop is not None:
                        v.append(f"{loc}: a rank-0 {dtype} out of the step operation on a loop "
                                 "node is refused (D3: a kernel scalar is not judged on a loop "
                                 "node)")
                    elif binding.ref != name:
                        v.append(f"{loc}: a rank-0 {dtype} out binds the state named after "
                                 f"the dummy, state.{name} (got {binding.text()})")
            elif intent == "in":
                if rank >= 1 and binding.kind not in ("inputs", _DERIVED, _STATE_ROOT):
                    v.append(f"{loc}: an array in dummy binds inputs.<path>, derived.<name> or "
                             f"state.<var> (got {binding.text()})")
                if binding.kind == "time" and not (rank == 0 and dtype == "real"):
                    v.append(f"{loc}: time binds a rank-0 real dummy")
                if binding.kind == STEP_CAPTURE and not (rank == 0 and dtype == "integer"):
                    v.append(f"{loc}: step binds a rank-0 integer dummy")
                if binding.kind == "literal":
                    lit = binding.ref
                    ok = (isinstance(lit, bool) if dtype == "logical" else
                          (isinstance(lit, int) and not isinstance(lit, bool))
                          if dtype == "integer" else _is_number(lit))
                    if not ok:
                        v.append(f"{loc}: the literal {lit!r} does not bind a {dtype} dummy")
            else:
                v.append(f"{loc}: intent {intent!r} is not in / out / inout")
        for var, names in sorted(reading.items()):
            if len(names) > 1 and len(schema_vars.get(var, [])) >= 1:
                v.append(f"{entry.loc}: state.{var} is read by dummies {names}: a state array "
                         "has at most one reading dummy per entry")
        for var, names in sorted(writing.items()):
            if len(names) > 1 and len(schema_vars.get(var, [])) >= 1:
                v.append(f"{entry.loc}: state.{var} is written by dummies {names}: a state "
                         "array has at most one writing dummy per entry")
        if block.loop is None:
            for var in sorted(set(reading) & set(writing)):
                ins = [n for n in reading[var] if (entry.dummy(n) or {}).get("intent") == "in"]
                outs = [n for n in writing[var] if (entry.dummy(n) or {}).get("intent") == "out"]
                if ins and outs:
                    v.append(f"{entry.loc}: the in/out pair {ins + outs} binds one state, "
                             f"state.{var}: on a node without loop the pair binds two different "
                             "states (the state itself is the actual)")
    for var in sorted(set(schema_vars) - bound):
        v.append(f"state_snapshots.schema.variables: {var!r} is bound by no dummy of drive "
                 "(a variable no operation touches is host data echoed into a capture)")


def _zero_step_refs(n_step: Binding | None) -> list[str]:
    """The `<ref>` gate (l)'s zero-step closure `final.step - <ref>` accepts on a `loop` node:
    the closure counterpart of `drive.loop.n_step`'s binding — `derived.<n>` / `inputs.<path>`
    verbatim, and for `state.<n>` (a scalar `init` writes, gate (e)) `initial.<n>` or
    `final.<n>`. Any other ref names a count the loop does not run by, so the closure would
    compare the committed steps with an unrelated number. Empty when `n_step` is undeclared
    (`_parse_drive` reports that)."""
    if n_step is None:
        return []
    if n_step.kind == _STATE_ROOT:
        return [f"initial.{n_step.ref}", f"final.{n_step.ref}"]
    return [n_step.text()]


def _init_result_state(block: DriveBlock) -> str | None:
    """The state `init`'s `result:` dummy binds, when `drive.loop.n_step` is a `state.<n>` (a
    scalar `init` writes): gate (l) then also requires `initial.<that state> eq 1` on every
    `pass` test, since a refused `init` that plans 0 steps leaves the planned count and the
    committed steps both 0 and `final.step - initial.<n>` would hold. None otherwise (or when `init` is malformed,
    which the structural gates report)."""
    n_step = (block.loop or {}).get("n_step")
    init = block.init
    if n_step is None or n_step.kind != _STATE_ROOT or init is None:
        return None
    binding = init.arguments.get(init.result)
    return binding.ref if binding is not None and binding.kind == _STATE_ROOT else None


def _or_list(refs: list[str]) -> str:
    return " or ".join(repr(r) for r in refs) if refs else "none (drive.loop.n_step is absent)"


def drive_violations(ir: dict[str, Any], *, kind: str) -> list[str]:
    """The Compile gate over `drive` (issue #589, SHAPE §6 gates (a)-(l)); ``kind`` is the
    node's `spec_kind` as the catalog answers it (never the IR's own `meta.spec_kind`). Gate
    (m) reads `tests.md` and is the validator's. An `infrastructure` IR carries no `drive`; a
    `problem` or `component` IR must. Structural findings stop the semantic gates (no
    cascade). Returns violation strings (empty == valid)."""
    if kind == INFRASTRUCTURE_KIND:
        return (["drive: an infrastructure node carries no drive block (its own runner runs "
                 "its cases)"] if _has_drive(ir) else [])
    if kind not in DRIVE_REQUIRED_KINDS:
        return [f"drive: spec kind {kind!r} is not one of "
                f"{sorted(DRIVE_REQUIRED_KINDS | {INFRASTRUCTURE_KIND})}"]
    if not _has_drive(ir):
        return [f"drive is required on a {kind} node: the operation the host-rendered driver "
                "calls per case, and on what"]
    block, v = _parse_drive(ir)
    if block is None:
        return v
    schema = snapshot_schema(ir)
    if schema is None:
        return ["drive needs a state_snapshots required_evidence entry (the schema variables "
                "it binds)"]
    try:
        schema_vars = schema_variables(schema)
    except PrimaryEvidenceError as exc:
        return [f"drive: the snapshot schema is unreadable ({exc})"]
    model = _model_from_block(block, schema_vars)
    entries = _evidence_entries(ir)
    for idx, entry in enumerate(schema.get("variables") or []):
        if isinstance(entry, dict) and "source" in entry:
            v.append(f"state_snapshots.schema.variables[{idx}].source: the source vocabulary is "
                     "retired (D6): what a variable is follows from drive")

    # (a) a problem publishes <spec_id>__step, and every step entry calls it
    if kind == "problem":
        meta = ir.get("meta") if isinstance(ir.get("meta"), dict) else {}
        spec_id = str(meta.get("spec_id") or "").strip()
        published, _ = _published_operations(ir)
        step_op = f"{spec_id}__step"
        if not spec_id:
            v.append("(a) meta.spec_id is required to name the problem's step operation")
        else:
            if step_op not in published:
                v.append(f"(a) a problem publishes {step_op} in public_api.published_operations "
                         "(D3)")
            for entry in block.step:
                if entry.operation != step_op:
                    v.append(f"(a) {entry.loc}.operation is {entry.operation!r}; a problem's "
                             f"drive.step calls {step_op}")
            if block.init is not None and block.init.operation != f"{spec_id}__init":
                v.append(f"(a) drive.init.operation is {block.init.operation!r}; a problem's "
                         f"init is {spec_id}__init")

    # (b) the bindings
    _binding_violations(model, schema_vars, v)

    # (d) the result names a logical out of the operation
    for entry in block.entries():
        dummy = entry.dummy(entry.result)
        if dummy is None or not _is_logical_out(dummy):
            v.append(f"(d) {entry.loc}.result {entry.result!r} is not a logical rank-0 out "
                     f"dummy of {entry.operation}")

    cases = _cases_by_id(ir)
    # (e) the loop references; history with loop
    if block.loop is not None:
        for key, binding in block.loop.items():
            loc = f"(e) drive.loop.{key} ({binding.text()})"
            if binding.kind == _STATE_ROOT:
                if binding.ref not in model.init_written or schema_vars.get(binding.ref) != []:
                    v.append(f"{loc}: a state.<var> loop reference names a scalar init writes")
            for cid, case in sorted(cases.items()):
                if binding.kind == "inputs":
                    try:
                        resolve_input_number(case.get("inputs"), binding.ref)
                    except PrimaryEvidenceError as exc:
                        v.append(f"{loc}: in case {cid!r}: {exc}")
                elif binding.kind == _DERIVED:
                    derived = case.get("derived")
                    if not isinstance(derived, dict) or binding.ref not in derived:
                        v.append(f"{loc}: case {cid!r} has no derived.{binding.ref}")
        if block.history_every is None:
            v.append("(e) drive.history is required with loop: {every: inputs.<path>}")
    elif block.history_every is not None:
        v.append("(e) drive.history is refused without loop (a one-call node has no samples)")

    # (f) initial_state per case
    needs = model.needs_initial()
    for cid, case in sorted(cases.items()):
        initial = case.get("initial_state")
        initial = initial if isinstance(initial, dict) else {}
        if case.get("initial_state") is not None and not isinstance(case["initial_state"], dict):
            v.append(f"(f) case {cid!r}: initial_state must be a mapping")
        for key in sorted(map(str, initial)):
            if key not in schema_vars:
                v.append(f"(f) case {cid!r}: initial_state.{key} is not a snapshot schema "
                         "variable")
            elif key in model.init_written or key in model.logical:
                v.append(f"(f) case {cid!r}: initial_state.{key} — a variable init writes, or a "
                         "logical result the driver writes, takes no initial_state entry")
            elif key in model.operation_only and not _is_number(initial[key]):
                v.append(f"(f) case {cid!r}: initial_state.{key} — an output the operation only "
                         "writes takes a number literal (its pre-call content)")
        missing = sorted(needs - set(initial))
        if missing:
            v.append(f"(f) case {cid!r}: initial_state has no entry for {missing}")

    # (f') the list form partitions the cases; a loop node has one mapping entry
    if block.list_form:
        if block.loop is not None:
            v.append("(f') a loop node's drive.step is one mapping entry, not a list")
        seen: dict[str, int] = {}
        for entry in block.step:
            for cid in entry.cases or ():
                seen[cid] = seen.get(cid, 0) + 1
        unknown = sorted(set(seen) - set(cases))
        twice = sorted(c for c, n in seen.items() if n > 1)
        absent = sorted(set(cases) - set(seen))
        if unknown:
            v.append(f"(f') drive.step[].cases names unknown case_id(s) {unknown}")
        if twice:
            v.append(f"(f') drive.step[].cases lists {twice} more than once")
        if absent:
            v.append(f"(f') drive.step[].cases leaves {absent} to no entry")

    # (g) what a host-evaluated expression may name, statically, in every case
    for cid, case in sorted(cases.items()):
        inputs = case.get("inputs") if isinstance(case.get("inputs"), dict) else {}
        try:
            coords = set(coordinate_arrays(schema, inputs, None))
        except PrimaryEvidenceError:
            coords = set()
        initial = case.get("initial_state") if isinstance(case.get("initial_state"), dict) else {}
        for var, raw in initial.items():
            if isinstance(raw, str):
                try:
                    bad = _host_expr_violation(expr_names(parse_expr(raw)), coords)
                except PrimaryEvidenceError as exc:
                    bad = str(exc)
                if bad is not None:
                    v.append(f"(g) case {cid!r}: initial_state.{var}: {bad}")
        derived = case.get("derived") if isinstance(case.get("derived"), dict) else {}
        before: set[str] = set()
        for name, raw in derived.items():
            if isinstance(raw, str):
                try:
                    bad = _host_expr_violation(expr_names(parse_expr(raw)), coords,
                                               derived_before=before,
                                               initial_vars=set(map(str, initial)))
                except PrimaryEvidenceError as exc:
                    bad = str(exc)
                if bad is not None:
                    v.append(f"(g) case {cid!r}: derived.{name}: {bad}")
            elif not _is_number(raw):
                v.append(f"(g) case {cid!r}: derived.{name} is a number or an expression")
            before.add(str(name))

    # (c) the per-case shapes (only once the bindings and names hold: no cascade)
    if not v:
        for cid, case in sorted(cases.items()):
            try:
                _evaluate_case(model, schema, case)
            except PrimaryEvidenceError as exc:
                v.append(f"(c) case {cid!r}: {exc}")

    # (h) the history artifacts
    arrays = sorted(n for n, d in schema_vars.items() if d)
    expected = {"t", STEP_CAPTURE} | {f"{a}_{m}" for a in arrays for m in ("min", "max")}
    series = entries.get("step_series")
    if (series is not None) != (block.loop is not None):
        v.append("(h) the step_series evidence entry is declared iff drive.loop is")
    if series is not None:
        names = series.get("names")
        listed = [n for n in (names if isinstance(names, list) else []) if isinstance(n, str)]
        if not isinstance(names, list) or len(listed) != len(names) \
                or len(set(listed)) != len(listed) or set(listed) != expected:
            v.append(f"(h) step_series.names must be exactly {sorted(expected)} (t, step and the "
                     f"min / max of every array state); got {names!r}")
    if ("state_history" in entries) != (block.history_every is not None):
        v.append("(h) the state_history evidence entry is declared iff drive.history is")

    # (i) D7
    v.extend(f"(i) {msg}" for msg in scalar_pairing_violations(ir))

    own = _own_predicates(ir)
    # (k) every init-written variable read over initial.*; a loop scalar with an array
    loop_scalars = {b.ref for k, b in (block.loop or {}).items()
                    if k in ("n_step", "dt") and b.kind == _STATE_ROOT}
    init_arrays = {a for a in model.init_written if schema_vars.get(a)}
    reads = [point_reads(p) for _i, p in own]
    for var in sorted(model.init_written):
        readers = [r for r in reads if r.get(("initial", var))]
        if not readers:
            v.append(f"(k) {var!r}, which init writes, is read by no predicate over "
                     f"initial.{var}")
        elif var in loop_scalars and not any(
                any(r.get(("initial", a)) for a in init_arrays) for r in readers):
            v.append(f"(k) {var!r} sets the loop (drive.loop) and init writes it: a predicate "
                     f"over initial.{var} also reads initial.<a> of an array init writes "
                     f"({sorted(init_arrays)})")

    # (l) the zero-step closure, in its fixed form — not asked while (e) refuses
    # `loop.n_step` itself (a `state.<var>` that is no scalar `init` writes): the accepted ref
    # follows from that binding, so the finding would only restate (e)'s
    n_step = (block.loop or {}).get("n_step")
    n_step_refused = n_step is not None and n_step.kind == _STATE_ROOT and (
        n_step.ref not in model.init_written or schema_vars.get(n_step.ref) != [])
    for test in _test_entries(ir):
        if n_step_refused:
            break
        if str(test.get("expected_outcome") or "").strip().lower() != "pass":
            continue
        tid = test["test_id"].strip()
        mine = [p for _i, p in own if p["test_id"].strip() == tid and _covers_test(p, test)]
        targets = [str(c).strip() for c in (test.get("target_cases") or [])]
        if block.loop is not None:
            accepted = _zero_step_refs(block.loop.get("n_step"))
            if not any(zero_step_closure_ref(pred) in accepted for pred in mine):
                v.append(f"(l) test {tid!r} (pass) carries no predicate `expr: \"final.step - "
                         "<ref>\"`, `op: eq`, `value: 0`, `per_case: true` over all its target "
                         "cases whose <ref> is the closure counterpart of drive.loop.n_step: "
                         f"for this IR {_or_list(accepted)} (the zero-step closure)")
            init_result = _init_result_state(block)
            if init_result is not None and not any(
                    _norm(p.get("expr")) == f"initial.{init_result}" and p.get("op") == "eq"
                    and _is_number(p.get("value")) and p.get("value") == 1
                    and not p.get("bind") for p in mine):
                v.append(f"(l) test {tid!r} (pass) carries no predicate `expr: \"initial."
                         f"{init_result}\"`, `op: eq`, `value: 1`, `per_case: true` over all its "
                         "target cases: drive.loop.n_step is a scalar init writes, so the "
                         "closure counts the steps against init's own plan, which a refused "
                         "init (result 0) planning 0 steps satisfies with 0 - 0; the init "
                         "result closes it")
        else:
            covering = [e for e in block.step
                        if e.cases is None or set(e.cases) & set(targets)]
            results = sorted({e.arguments[e.result].ref for e in covering
                              if e.result in e.arguments
                              and e.arguments[e.result].kind == _STATE_ROOT})
            for state in results:
                if not any(_norm(p.get("expr")) == f"final.{state}" and p.get("op") == "eq"
                           and _is_number(p.get("value")) and p.get("value") == 1
                           and not p.get("bind") for p in mine):
                    v.append(f"(l) test {tid!r} (pass) carries no predicate `expr: "
                             f"\"final.{state}\"`, `op: eq`, `value: 1`, `per_case: true` over "
                             "all its target cases (the call's result, the zero-step closure of "
                             "a one-call node)")
    return v

def _read_ir(path: Path) -> dict[str, Any]:
    import yaml  # the host reader; PyYAML is a runtime dependency of the conductor
    target = path / "spec.ir.yaml" if path.is_dir() else path
    try:
        doc = yaml.safe_load(target.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise PrimaryEvidenceError(f"{target}: unreadable IR ({exc})") from None
    if not isinstance(doc, dict):
        raise PrimaryEvidenceError(f"{target}: not a mapping")
    return doc


def main(argv: list[str] | None = None) -> int:
    """Read-only: evaluate an IR's primary predicates against a recorded run node directory
    and print the records as JSON. The offline measurement the plan of issue #255 names."""
    parser = argparse.ArgumentParser(
        prog="python3 -m tools.primary_evidence",
        description="Evaluate io_contract.predicates against a run's captures and "
                    "print the records as JSON. Exit 0: every predicate satisfied (or none "
                    "declared); 1: a predicate unsatisfied (a physics or structural record); "
                    "2: the IR or the run directory could not be read, or a predicate is "
                    "malformed.")
    parser.add_argument("--ir", required=True, type=Path,
                        help="the IR directory (holding spec.ir.yaml) or the file itself")
    parser.add_argument("--run", required=True, type=Path,
                        help="the run node directory holding raw/state_snapshots/")
    parser.add_argument("--comparand", action="append", default=[], metavar="TARGET_ID=RUN_DIR",
                        help="bind another target's run node directory as a comparand of the "
                             "cross-target predicates (repeatable; the host's own selection is "
                             "orchestration_runtime.resolve_comparands, read by "
                             "orchestration_runtime.cross_target_agreement)")
    args = parser.parse_args(argv)
    try:
        ir = _read_ir(args.ir)
        if not (Path(args.run) / "raw" / "state_snapshots").is_dir():
            raise PrimaryEvidenceError(f"{args.run}: no raw/state_snapshots directory")
        comparands: list[ComparandEvidence] = []
        for item in args.comparand:
            target_id, sep, run = str(item).partition("=")
            if not sep or not target_id.strip() or not run.strip():
                raise PrimaryEvidenceError(f"--comparand {item!r}: expected TARGET_ID=RUN_DIR")
            run_path = Path(run.strip())
            comparands.append(ComparandEvidence(
                target_id=target_id.strip(), pipeline_ref="", run_id=run_path.parent.name,
                run_dir=run_path, evidence=comparand_evidence_sha256(run_path)))
        records = evaluate_primary_predicates(ir, args.run, comparands=comparands)
    except PrimaryEvidenceError as exc:
        print(json.dumps({"error": str(exc)}, indent=2))
        return 2
    print(json.dumps(records, indent=2, ensure_ascii=False))
    return 0 if all(r["satisfied"] for r in records) else 1


if __name__ == "__main__":
    sys.exit(main())
