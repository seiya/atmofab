"""Deterministic per-test verdict evaluation.

Pure functions shared by the conductor (``Validate.execute`` authors ``verdict.json`` in-process
from these) and the Compile-stage gate (``validate_pipeline_semantics --stage compile`` validates
``io_contract.tests`` through the same schema check). No filesystem, no conductor dependency.

A test is declared in the IR at ``io_contract.tests``, and the host predicates that judge it at
``io_contract.predicates`` (`tools/primary_evidence.py`, whose records this module folds)::

    io_contract:
      tests:
        - test_id: <str>                 # one per tests.md test_id (set equality gated at Compile)
          expected_outcome: pass|xfail   # the certifying outcome when every predicate holds
          target_cases: [<case_id>, ...]  # the cases its predicates range over
          quantities: [<name>, ...]      # the judgments its tests.md definition lists

A test's ``status`` is its ``expected_outcome`` when every predicate of the test holds, else
``fail``; each record's value was computed by the host from the captures, with no generated code
in the path but the captured arrays and the capture's time (issue #582, which retired the judging
half of the checks module). ``xfail_condition`` is a case-construction fact (verified at Compile),
never an evaluated runtime predicate: a guard test is judged by the predicates its ``tests.md``
definition states in the failure direction.

``evaluate_verdict`` re-checks, on the records it is handed, what the Compile gate states:
a record ranges over its test's ``target_cases``; every test holds at least one record that is not
cross-target, and those records value the state of every target case; and a record reading a ``source: reported`` snapshot variable stands beside a record
of the same quantity that reads ``source: operation`` state, and no reported variable, over the
same cases (`tools.primary_evidence.reported_read_violations`). A kernel's report of its own
run-wide extremum therefore never judges a test alone.
"""

from __future__ import annotations

import re
from typing import Any

_EXPECTED_OUTCOMES: frozenset[str] = frozenset({"pass", "xfail"})
# A `quantity` name (shared with `tools.primary_evidence.QUANTITY_RE`, which imports this
# module and so cannot be imported here): a lowercase dotted identifier.
_QUANTITY_RE = re.compile(r"^[a-z][a-z0-9_.]*$")

# A per-test evaluation result kind, folded into the top-level failure_class.
_KIND_PASS = "pass"
_KIND_PHYSICS = "physics"      # a predicate was valued and its comparison was false
_KIND_STRUCTURAL = "structural"  # a predicate could not be valued
#: A cross-target primary record with no comparand (issue #324): satisfied, nothing evaluated.
#: Since issue #383 it is the only cross-target record a verdict holds — the verdict evaluates
#: no comparison, and the cross-target agreement does, outside it.
_KIND_NO_COMPARAND = "no_comparand"
#: The `sources_read` keys the rules of issue #582 pair (`tools.primary_evidence.SNAPSHOT_SOURCES`).
_SOURCE_OPERATION = "operation"
_SOURCE_REPORTED = "reported"
_SOURCE_REFERENCE = "reference"


def is_cross_target_record(rec: dict[str, Any]) -> bool:
    """Whether a primary record (`tools.primary_evidence.evaluate_primary_predicates`) is a
    CROSS-TARGET one: it lists the comparand runs it was evaluated against, or it is the
    `no_comparand` record of a cross-target predicate with none bound. Every other record
    carries `comparands: []` (or, written before grammar 3, no such key)."""
    return rec.get("kind") == _KIND_NO_COMPARAND or bool(rec.get("comparands"))


class PredicateError(ValueError):
    """A malformed test or record surfaced during evaluation (as opposed to a schema-validation
    pass, which returns violation strings)."""


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _apply_op(lhs: Any, op: str, rhs: Any) -> bool:
    """One comparison of a primary predicate. `eq` / `ne` compare numbers cross-type (1 == 1.0)
    and never a bool with a number; the ordered ops require two numbers."""
    if op == "eq":
        return _values_equal(lhs, rhs)
    if op == "ne":
        return not _values_equal(lhs, rhs)
    if not (_is_number(lhs) and _is_number(rhs)):
        return False
    if op == "le":
        return lhs <= rhs
    if op == "ge":
        return lhs >= rhs
    if op == "lt":
        return lhs < rhs
    if op == "gt":
        return lhs > rhs
    raise PredicateError(f"unknown op: {op}")


def _values_equal(lhs: Any, rhs: Any) -> bool:
    if isinstance(lhs, bool) or isinstance(rhs, bool):
        return isinstance(lhs, bool) and isinstance(rhs, bool) and lhs == rhs
    return lhs == rhs


def _resolve_value(value: Any, case_id: str | None) -> tuple[bool, Any]:
    """Resolve a predicate ``value``. A plain literal returns itself; a per-case map
    ``{"per_case": {<case_id>: v}}`` returns the entry for ``case_id`` (``present`` False
    when the case is not in the map)."""
    if isinstance(value, dict) and set(value.keys()) == {"per_case"}:
        table = value["per_case"]
        if not isinstance(table, dict):
            raise PredicateError("value.per_case must be a map of case_id -> value")
        if case_id is None or case_id not in table:
            return (False, None)
        return (True, table[case_id])
    return (True, value)


def _sources_read(rec: dict[str, Any], test_id: str) -> dict[str, set[str]]:
    """A record's `sources_read` (source class -> the cases read through it). A record without
    the map says nothing about what it read, so the pairing rule could not be asked of it."""
    read = rec.get("sources_read")
    if not isinstance(read, dict):
        raise PredicateError(f"primary record for {test_id!r} quantity {rec.get('quantity')!r} "
                             "carries no sources_read map")
    return {str(k): {str(c) for c in v} for k, v in read.items() if isinstance(v, list)}


def _check_reported_pairing(test_id: str, records: list[dict[str, Any]]) -> None:
    """The Compile gate's reported-read rule (`primary_evidence.reported_read_violations`),
    re-checked on the records: a record that reads a `reported` variable needs another record
    of the same quantity, not cross-target, that reads `operation` state and no `reported`
    variable, in every case the report is read in."""
    for rec in records:
        reported = _sources_read(rec, test_id).get(_SOURCE_REPORTED)
        if not reported:
            continue
        paired = False
        for other in records:
            if other is rec or is_cross_target_record(other) \
                    or other.get("quantity") != rec.get("quantity"):
                continue
            read = _sources_read(other, test_id)
            if not read.get(_SOURCE_REPORTED) and reported <= read.get(_SOURCE_OPERATION, set()):
                paired = True
                break
        if not paired:
            raise PredicateError(
                f"primary record for {test_id!r} quantity {rec.get('quantity')!r} reads a "
                f"source: reported variable in {sorted(reported)} and no record of that quantity "
                "reads source: operation state alone in those cases")


def _check_reference_state(test_id: str, records: list[dict[str, Any]]) -> None:
    """The Compile gate's rule (`primary_evidence.reference_read_violations`), re-checked on the
    records: a `reference` read needs `operation` state read in those cases, by records of the same
    quantity, the same one included, none cross-target."""
    for rec in records:
        cases = _sources_read(rec, test_id).get(_SOURCE_REFERENCE)
        if not cases:
            continue
        state: set[str] = set()
        for other in records:
            if not is_cross_target_record(other) and other.get("quantity") == rec.get("quantity"):
                state |= _sources_read(other, test_id).get(_SOURCE_OPERATION, set())
        if not cases <= state:
            raise PredicateError(
                f"primary record for {test_id!r} quantity {rec.get('quantity')!r} reads a "
                f"source: reference variable and no record of that quantity reads source: "
                f"operation state in {sorted(cases - state)}")


def evaluate_verdict(tests: Any, records: Any, *,
                     run_id: str | None = None,
                     node_key: str | None = None) -> dict[str, Any]:
    """Author the deterministic ``verdict.json`` body from the IR's ``io_contract.tests`` and the
    list ``tools.primary_evidence.evaluate_primary_predicates`` returned for this run. Returns a
    dict with ``per_test`` (one ``{test_id, status, basis}`` per test, in order, ``basis`` being
    ``{predicates: [records]}``), the reduced ``self_verdict``, and the ``failure_class``
    (``pass`` / ``physics_fail`` / ``structural_violation``).

    A test with no record, or with only cross-target records, raises ``PredicateError``: nothing
    would judge it. So does a record of a test no entry carries, one over other cases than its
    test's, a target case no record values, and a reported read with no state read beside it
    (`_check_reported_pairing`).

    A CROSS-TARGET record (`is_cross_target_record`, issue #324) takes no part in the status.
    Since issue #383 the verdict evaluates no comparison: every cross-target record it is handed
    must be the ``no_comparand`` placeholder — satisfied, with no comparand and nothing evaluated
    — and one that is not raises ``PredicateError`` (dropping it would leave an evaluated
    comparison silently ignored). The comparison is the cross-target agreement
    (`orchestration_runtime.cross_target_agreement`), asked where "is this node ready" is
    asked, so a disagreement between two variants that each pass their own tests stops both
    (plan decision 6 of issue #324; `docs/RUNBOOK.md` §3-0, cross-target predicates, says how
    the operator resolves it).
    """
    by_test: dict[str, list[dict[str, Any]]] = {}
    for rec in (records if isinstance(records, list) else []):
        if not isinstance(rec, dict) or not isinstance(rec.get("test_id"), str):
            raise PredicateError("each primary record must be a mapping with a test_id")
        by_test.setdefault(rec["test_id"].strip(), []).append(rec)
    doc: dict[str, Any] = {}
    if node_key is not None:
        doc["node_key"] = node_key
    if run_id is not None:
        doc["run_id"] = run_id
    # An empty test set is never a legitimate PASS: a node with no evaluable test cannot certify.
    # Fail structurally rather than reduce an empty per_test to `pass` (Compile forbids it, and
    # the conductor guards it; this is for any direct caller).
    if not isinstance(tests, list) or not tests:
        doc["self_verdict"] = "fail"
        doc["failure_class"] = "structural_violation"
        doc["per_test"] = []
        return doc
    per_test: list[dict[str, Any]] = []
    saw_structural = False
    saw_physics = False
    for test in tests:
        if not isinstance(test, dict):
            raise PredicateError("each tests entry must be a mapping")
        test_id = test.get("test_id")
        if not isinstance(test_id, str) or not test_id.strip():
            raise PredicateError("tests entry missing a non-empty test_id")
        test_id = test_id.strip()
        expected = str(test.get("expected_outcome") or "").strip().lower()
        if expected not in _EXPECTED_OUTCOMES:
            raise PredicateError(f"expected_outcome must be one of {sorted(_EXPECTED_OUTCOMES)}")
        recs = by_test.pop(test_id, None) or []
        own = [r for r in recs if not is_cross_target_record(r)]
        if not own:
            raise PredicateError(f"test {test_id!r} has no predicate record that is not "
                                 "cross-target: nothing judges it")
        targets = {str(c) for c in (test.get("target_cases") or [])}
        for rec in recs:
            if is_cross_target_record(rec) and not (
                    rec.get("kind") == _KIND_NO_COMPARAND and rec.get("satisfied") is True
                    and not rec.get("comparands") and not rec.get("evaluated")):
                # Issue #383: the verdict is evaluated with no comparand, so a cross-target
                # record is the placeholder. Anything else is a comparison this verdict would
                # otherwise ignore — refused, never dropped.
                raise PredicateError(
                    f"primary record for {test_id!r} quantity "
                    f"{rec.get('quantity')!r} is an evaluated cross-target comparison; the "
                    "verdict evaluates none (the cross-target agreement does)")
            # The Compile gate pins this; re-checked here so a record over a subset of the
            # test's cases never reads as a judgment of the whole test.
            if "target_cases" in rec and set(map(str, rec["target_cases"])) != targets:
                raise PredicateError(
                    f"primary record for {test_id!r} ranges over "
                    f"{sorted(map(str, rec['target_cases']))}, not the test's target_cases")
        unread = sorted(targets - {str(c) for r in own for c in (r.get("cases_read") or [])})
        if unread:
            raise PredicateError(f"no record of test {test_id!r} values the captured state of "
                                 f"target case(s) {unread}")
        _check_reported_pairing(test_id, recs)
        _check_reference_state(test_id, recs)
        satisfied = all(bool(r.get("satisfied")) for r in own)
        structural = any(r.get("kind") == _KIND_STRUCTURAL for r in own)
        if not satisfied:
            if structural:
                saw_structural = True
            else:
                saw_physics = True
        per_test.append({"test_id": test_id, "status": expected if satisfied else "fail",
                         "basis": {"predicates": recs}})
    if by_test:
        # A record for a test no entry carries would silently judge nothing; Compile pins
        # predicate test_ids == tests[] test_ids, so this is an IR defect.
        raise PredicateError(
            f"predicates name test_id(s) with no io_contract.tests entry: {sorted(by_test)}")

    doc["self_verdict"] = _reduce_statuses([item["status"] for item in per_test])
    if saw_structural:
        doc["failure_class"] = "structural_violation"
    elif saw_physics:
        doc["failure_class"] = "physics_fail"
    else:
        doc["failure_class"] = "pass"
    doc["per_test"] = per_test
    return doc


def _reduce_statuses(statuses: list[str]) -> str:
    """self_verdict reduce — identical rule to the post_judge derivation
    (_author_derived_validate_artifacts): fail if any fail/blocked; else xfail iff every
    non-skipped entry is xfail; else pass."""
    counts = {"pass": 0, "fail": 0, "xfail": 0, "skipped": 0, "blocked": 0}
    for st in statuses:
        if st in counts:
            counts[st] += 1
    if counts["fail"] > 0 or counts["blocked"] > 0:
        return "fail"
    if counts["xfail"] > 0 and counts["pass"] == 0:
        return "xfail"
    return "pass"


# --------------------------------------------------------------------------- schema

def validate_tests_schema(tests: Any, *, case_ids: set[str], test_ids: list[str]) -> list[str]:
    """Validate ``io_contract.tests`` at Compile. Returns human-readable violation strings (empty
    == valid), each prefixed with a stable token so the conductor's post-gate classifier can
    route it.

    Checks: the structure and the outcome enum; the ``test_id`` set equals the ``tests.md`` set
    (``test_ids``); ``target_cases`` is non-empty, each entry a declared case; ``quantities`` is a
    non-empty list of distinct names of the ``quantity`` form."""
    v: list[str] = []
    if not isinstance(tests, list) or not tests:
        return ["io_contract.tests must be a non-empty list"]
    seen_ids: list[str] = []
    for idx, test in enumerate(tests):
        loc = f"tests[{idx}]"
        if not isinstance(test, dict):
            v.append(f"{loc} must be a mapping")
            continue
        test_id = test.get("test_id")
        if not isinstance(test_id, str) or not test_id.strip():
            v.append(f"{loc}.test_id must be a non-empty string")
        else:
            seen_ids.append(test_id.strip())
        outcome = test.get("expected_outcome")
        if not isinstance(outcome, str) or outcome.strip().lower() not in _EXPECTED_OUTCOMES:
            v.append(f"{loc}.expected_outcome must be one of {sorted(_EXPECTED_OUTCOMES)}")
        targets = test.get("target_cases")
        if not isinstance(targets, list) or not targets:
            v.append(f"{loc}.target_cases must be a non-empty list")
            targets = []
        for cid in targets:
            if not isinstance(cid, str) or cid not in case_ids:
                v.append(f"{loc}.target_cases references unknown case_id ({cid!r})")
        quantities = test.get("quantities")
        if not isinstance(quantities, list) or not quantities:
            v.append(f"{loc}.quantities must be a non-empty list: the judgments the test's "
                     "tests.md `quantities:` line lists")
            continue
        for q in quantities:
            if not isinstance(q, str) or not _QUANTITY_RE.match(q):
                v.append(f"{loc}.quantities entry {q!r} must match {_QUANTITY_RE.pattern}")
        named = [q for q in quantities if isinstance(q, str)]
        if len(named) != len(set(named)):
            v.append(f"{loc}.quantities has a duplicated entry ({sorted(named)})")
    if len(seen_ids) != len(set(seen_ids)):
        dups = sorted({t for t in seen_ids if seen_ids.count(t) > 1})
        v.append(f"io_contract.tests has duplicated test_id ({dups})")
    missing = sorted(set(test_ids) - set(seen_ids))
    extra = sorted(set(seen_ids) - set(test_ids))
    if missing:
        v.append(f"io_contract.tests missing tests from tests.md ({missing})")
    if extra:
        v.append(f"io_contract.tests has unknown test_id not in tests.md ({extra})")
    return v
