#!/usr/bin/env python3
"""What an `io_contract` says about raw evidence, and what a `raw/` directory actually holds.

Two things live here, and they are one subject seen from both ends.

**The contract readers** — `contract_test_evidence_requirements`, `test_id_to_case_ids`,
`expected_metrics_basis_keys`, `normalize_raw_evidence_artifact` — derive the expected
(test_id, case_id) evidence MATRIX from the IR's `io_contract`. They were
`validate_pipeline_semantics`'s private helpers and moved here so that the `--stage
post_execute` gate and the judge's excerpt read one definition of the matrix rather than
two; the validator imports them back under their old private names.

**The excerpt** — `raw_evidence_excerpt` — is the judge's window onto `raw/`. Issue #169
puts `validate.judge` on the pure leaf, and a pure leaf gets documents, not a filesystem:
`raw/` is 13 MB on `shallow_water2d` (a 6.5 MB `metrics_basis.json` and twelve snapshots
of 100 KB - 1.65 MB, all `[nx, ny]` float arrays), so it cannot be inlined and
the leaf cannot go and read it. What the host inlines instead is this excerpt: the
COVERAGE of that matrix, and per-array SUMMARIES — shape, extent, non-finite counts,
all-zero — from which the judge can ask whether `diagnostics.json`'s metrics are supported
by the evidence they claim to come from, and whether an array is the degenerate shape a
fabricated one takes.

**No numeric array is ever inlined**, at any size. The output is bounded by
(test x case x variable) plus (snapshot x variable). MEASURED on `shallow_water2d`'s
recorded run `run_20260802_001`: 13,019,745 B of `raw/` becomes 52,001 B of excerpt — 250x
— in 0.71 s, as 48 metrics-basis rows and 12 snapshot cases, with no spurious coverage
finding against that run's own IR. That is the policy, and `RAW_EXCERPT_POLICY_VERSION`
stamps which version of it a review was made under;
`docs/workflow/phases/phase_04_validate.md` §4-2 is canonical for what it includes and
excludes.

**Nothing here raises for a defect in the evidence.** An absent `raw/`, an unparseable
file, a ragged array: each is a FACT about the run, recorded in the excerpt and handed to
the judge, because the alternative — an exception during context assembly — means no judge
runs at all on exactly the runs that most need one. What the host raises for is its own
inability to resolve the run node directory, which is the caller's business and not this
module's.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, NamedTuple

#: Bumped when what the excerpt CONTAINS changes, so a recorded `semantic_review.json` says
#: which window its judge was looking through. Stamped by the host into that file.
RAW_EXCERPT_POLICY_VERSION = 1

#: Spellings the IR may use for a raw-evidence artifact, mapped to its canonical name.
RAW_EVIDENCE_ALIASES = {
    "metrics_basis.json": "metrics_basis.json",
    "raw/metrics_basis.json": "metrics_basis.json",
    "execution_trace.json": "execution_trace.json",
    "raw/execution_trace.json": "execution_trace.json",
    "state_snapshots": "state_snapshots",
    "raw/state_snapshots": "state_snapshots",
    "raw/state_snapshots/": "state_snapshots",
}

#: How many distinct string values a non-numeric field is summarized by before the summary
#: says "and more" — the excerpt is a summary, and an unbounded one is the thing it exists
#: to avoid.
_MAX_TEXT_CHARS = 200


def normalize_raw_evidence_artifact(token: str) -> str | None:
    """The canonical artifact name for an IR spelling, or None when it names none."""
    normalized = token.strip().lower().replace("\\", "/")
    return RAW_EVIDENCE_ALIASES.get(normalized)


# --------------------------------------------------------------------------------------
# The expected evidence matrix, read from the contract
# --------------------------------------------------------------------------------------


def contract_test_evidence_requirements(contract: dict[str, Any]) -> dict[str, set[str]]:
    """Map each test_id to the raw variables it declares, from
    ``io_contract.test_evidence_requirements``.

    A test declaring an EMPTY `required_raw_variables` is dropped, which is why
    `_validate_test_evidence_requirements` refuses one outright — see the note in
    `expected_metrics_basis_keys`.
    """
    raw_reqs = contract.get("test_evidence_requirements")
    if not isinstance(raw_reqs, list):
        return {}

    result: dict[str, set[str]] = {}
    for item in raw_reqs:
        if not isinstance(item, dict):
            continue
        raw_test_id = item.get("test_id")
        raw_variables = item.get("required_raw_variables")
        if (
            not isinstance(raw_test_id, str)
            or not raw_test_id.strip()
            or not isinstance(raw_variables, list)
        ):
            continue
        variables = {
            token.strip()
            for token in raw_variables
            if isinstance(token, str) and token.strip()
        }
        if variables:
            result[raw_test_id.strip()] = variables
    return result


def test_id_to_case_ids(contract: dict[str, Any]) -> dict[str, list[str]]:
    """Map each test_id to every case_id its predicate ranges over, from
    ``io_contract.test_predicates[].target_cases``.

    This is the row set of a test's metrics-basis evidence: the host-rendered runner emits one
    entry per ``(test_id, case_id)`` over exactly this product (``host_render.render_runner``),
    so the post_execute completeness matrix mirrors the renderer rather than guessing. Empty
    dict when the IR declares no predicates.
    """
    predicates = contract.get("test_predicates")
    if not isinstance(predicates, list):
        # `_io_contract_for_execution` hoists the key out of the nested `io_contract`
        # section; an un-flattened doc still nests it.
        nested = contract.get("io_contract")
        predicates = nested.get("test_predicates") if isinstance(nested, dict) else None
    if not isinstance(predicates, list):
        return {}
    mapping: dict[str, list[str]] = {}
    for item in predicates:
        if not isinstance(item, dict):
            continue
        test_id = item.get("test_id")
        if not (isinstance(test_id, str) and test_id.strip()):
            continue
        bucket = mapping.setdefault(test_id.strip(), [])
        for case_id in item.get("target_cases") or []:
            if isinstance(case_id, str) and case_id.strip() and case_id.strip() not in bucket:
                bucket.append(case_id.strip())
    return mapping


class ExpectedMetricsBasisKeys(NamedTuple):
    """The expected evidence matrix, plus the two shapes of contract that cannot express one.

    `expected` is the (test_id, case_id) product. `untargeted_tests` declare raw variables but
    no `target_cases`, so their rows cannot be derived at all — the gate refuses the run rather
    than calling the underivable rows "unknown", and they contribute nothing to `expected`.
    `multi_target_tests` own several rows, which matters only to the deprecated `tests` object
    form of `metrics_basis.json`, keyed by test_id and unable to hold them.
    """

    expected: set[tuple[str, str]]
    untargeted_tests: list[str]
    multi_target_tests: list[str]
    test_requirements: dict[str, set[str]]


def expected_metrics_basis_keys(contract: dict[str, Any]) -> ExpectedMetricsBasisKeys:
    """Derive the expected metrics-basis evidence matrix from an `io_contract`.

    The expected evidence is the test x target_case MATRIX: one entry per case each test's
    predicate ranges over. `test_predicates[].target_cases` is the anchor the host-rendered
    runner emits from (the language backend runner's `_target_cases`), so both sides read one
    field.

    The row SET is `test_requirements`, whose source (`contract_test_evidence_requirements`)
    drops a test declaring an EMPTY `required_raw_variables` while the renderer's
    `_test_evidence` keeps it — so such a test would render a row this matrix calls "unknown".
    That IR never reaches the gate: `_validate_test_evidence_requirements` rejects an empty
    `required_raw_variables` outright. Should that ever be relaxed, the two filters must be
    reconciled rather than left to disagree.
    """
    test_requirements = contract_test_evidence_requirements(contract)
    test_to_cases = test_id_to_case_ids(contract)
    expected: set[tuple[str, str]] = set()
    untargeted: list[str] = []
    multi_target: list[str] = []
    for test_id in test_requirements:
        target_cases = test_to_cases.get(test_id) or []
        if not target_cases:
            untargeted.append(test_id)
            continue
        if len(target_cases) > 1:
            multi_target.append(test_id)
        for case_id in target_cases:
            expected.add((test_id, case_id))
    return ExpectedMetricsBasisKeys(
        expected, sorted(untargeted), sorted(multi_target), test_requirements)


#: An entry may nest its variables one level down under one of these; the gate honours the
#: first one present, so the excerpt must read the same key or the judge and the gate would
#: disagree about which variables an entry holds.
METRICS_BASIS_NESTED_VARIABLE_FIELDS = ("raw_variables", "variables", "evidence")

#: Keys of a flat entry that are bookkeeping rather than evidence.
METRICS_BASIS_BOOKKEEPING_KEYS = frozenset(
    {
        "test_id",
        "case_id",
        "case_ids",
        "cases",
        "status",
        "summary",
        "notes",
        "meta",
        "artifacts",
    }
)


def metrics_basis_variable_keys(entry: dict[str, Any]) -> set[str]:
    """The variable names one metrics-basis entry holds.

    Nesting fields are matched by exact `get()`, so a padded `" variables "` is a wrapper
    rather than a nesting field — `_metrics_basis_unrecognized_wrapper` mirrors that, and the
    contract requires each variable as a DIRECT sibling of `test_id` in the first place.
    """
    for field_name in METRICS_BASIS_NESTED_VARIABLE_FIELDS:
        raw_value = entry.get(field_name)
        if isinstance(raw_value, dict):
            return {
                key.strip()
                for key in raw_value
                if isinstance(key, str) and key.strip()
            }

    return {
        key.strip()
        for key in entry
        if isinstance(key, str)
        and key.strip()
        and key not in METRICS_BASIS_BOOKKEEPING_KEYS
    }


# --------------------------------------------------------------------------------------
# The excerpt
# --------------------------------------------------------------------------------------


def raw_evidence_excerpt(raw_dir: Path, io_contract: dict[str, Any]) -> dict[str, Any]:
    """Summarize a run's `raw/` directory against its `io_contract`, inlining no array.

    Total by construction: every failure to read or parse becomes a `problems` entry, never an
    exception (see the module docstring). The result is JSON-serializable and deterministic —
    every list is ordered — so the same run excerpts to the same bytes.
    """
    problems: list[str] = []
    present_keys, entry_by_key = _read_metrics_basis(raw_dir, problems)
    keys = expected_metrics_basis_keys(io_contract if isinstance(io_contract, dict) else {})

    return {
        "policy_version": RAW_EXCERPT_POLICY_VERSION,
        "raw_dir_present": raw_dir.is_dir(),
        "required_evidence": _required_evidence_rows(raw_dir, io_contract),
        "coverage": _coverage(keys, present_keys, entry_by_key),
        "metrics_basis_arrays": _metrics_basis_arrays(entry_by_key),
        "state_snapshots": _state_snapshots(raw_dir, problems),
        "problems": problems,
    }


def _read_json(path: Path, problems: list[str], label: str) -> Any:
    """Read one JSON document, recording — never raising — whatever went wrong."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        problems.append(f"{label}: absent")
    except json.JSONDecodeError as exc:
        problems.append(f"{label}: not parseable as JSON ({exc.msg} at line {exc.lineno})")
    except (OSError, UnicodeError) as exc:
        problems.append(f"{label}: unreadable ({type(exc).__name__})")
    return None


def _read_metrics_basis(
    raw_dir: Path, problems: list[str]
) -> tuple[set[tuple[str, str]], dict[tuple[str, str], dict[str, Any]]]:
    """Index `raw/metrics_basis.json` by its (test_id, case_id) key.

    Accepts both container forms the contract allows — the `per_test` LIST and the deprecated
    `tests` OBJECT — because the excerpt reports on what the run produced, including the form
    that cannot hold a multi-target test's rows; naming that form is what lets the judge read
    a "missing" row as a container defect rather than as absent evidence.
    """
    doc = _read_json(raw_dir / "metrics_basis.json", problems, "raw/metrics_basis.json")
    entries: dict[tuple[str, str], dict[str, Any]] = {}
    if doc is None:
        return set(), entries
    if not isinstance(doc, dict):
        problems.append("raw/metrics_basis.json: not a JSON object")
        return set(), entries

    raw_entries = doc.get("per_test")
    if raw_entries is None:
        raw_entries = doc.get("tests")
    if isinstance(raw_entries, list):
        items = list(enumerate(raw_entries))
    elif isinstance(raw_entries, dict):
        items = list(raw_entries.items())
    else:
        problems.append(
            "raw/metrics_basis.json: neither a `per_test` list nor a `tests` object")
        return set(), entries

    for where, item in items:
        if not isinstance(item, dict):
            problems.append(f"raw/metrics_basis.json: entry {where!r} is not an object")
            continue
        test_id = item.get("test_id")
        if not isinstance(test_id, str) or not test_id.strip():
            # The `tests` object form keys entries by test_id, so the key is the id there.
            test_id = where if isinstance(where, str) else None
        if not isinstance(test_id, str) or not test_id.strip():
            problems.append(f"raw/metrics_basis.json: entry {where!r} names no test_id")
            continue
        case_id = item.get("case_id")
        if not isinstance(case_id, str) or not case_id.strip():
            problems.append(
                f"raw/metrics_basis.json: entry for test_id {test_id.strip()!r} carries no "
                "case_id — evidence is keyed by (test_id, case_id)")
            continue
        key = (test_id.strip(), case_id.strip())
        if key in entries:
            problems.append(f"raw/metrics_basis.json: duplicated entry for {list(key)}")
            continue
        entries[key] = item
    return set(entries), entries


def _coverage(
    keys: ExpectedMetricsBasisKeys,
    present: set[tuple[str, str]],
    entry_by_key: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    """The expected matrix against the produced one, in both directions.

    `missing` and `unexpected` are the two directions `post_execute` already pins; they appear
    here because the judge is asked whether the evidence SUPPORTS the verdict, and a row that
    was never produced supports nothing. `missing_required_variables` is the finer question the
    gate does not ask of `metrics_basis.json`: a row that exists but omits a variable its test
    declared.
    """
    missing_vars: list[dict[str, Any]] = []
    for key in sorted(present & keys.expected):
        required = keys.test_requirements.get(key[0]) or set()
        held = metrics_basis_variable_keys(entry_by_key[key])
        absent = sorted(v for v in required if v not in held)
        if absent:
            missing_vars.append(
                {"test_id": key[0], "case_id": key[1], "missing_variables": absent})
    return {
        "expected": [list(k) for k in sorted(keys.expected)],
        "present": [list(k) for k in sorted(present)],
        "missing": [list(k) for k in sorted(keys.expected - present)],
        "unexpected": [list(k) for k in sorted(present - keys.expected)],
        "missing_required_variables": missing_vars,
        "tests_without_target_cases": list(keys.untargeted_tests),
        "tests_targeting_several_cases": list(keys.multi_target_tests),
    }


def _metrics_basis_arrays(
    entry_by_key: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    """One summary row per (entry, variable). Bounded by the matrix; never an array."""
    rows: list[dict[str, Any]] = []
    for (test_id, case_id) in sorted(entry_by_key):
        variables = _entry_variables(entry_by_key[(test_id, case_id)])
        for name in sorted(variables):
            row = {"test_id": test_id, "case_id": case_id, "variable": name}
            row.update(_summarize(variables[name]))
            rows.append(row)
    return rows


def _entry_variables(entry: dict[str, Any]) -> dict[str, Any]:
    """The variables of one entry with their VALUES, read through the same nesting rule as
    `metrics_basis_variable_keys` — which returns names only, and is what the gate calls."""
    for field_name in METRICS_BASIS_NESTED_VARIABLE_FIELDS:
        nested = entry.get(field_name)
        if isinstance(nested, dict):
            return {k.strip(): v for k, v in nested.items()
                    if isinstance(k, str) and k.strip()}
    return {k.strip(): v for k, v in entry.items()
            if isinstance(k, str) and k.strip()
            and k not in METRICS_BASIS_BOOKKEEPING_KEYS}


def _state_snapshots(raw_dir: Path, problems: list[str]) -> dict[str, Any]:
    """Each per-case snapshot summarized against `snapshot_schema.json`'s declaration.

    The declared shape travels with the summary so the judge can compare them: a snapshot
    whose array is not the shape the schema declares is evidence about the runner, and it is
    invisible from the numbers alone.
    """
    snapshots_dir = raw_dir / "state_snapshots"
    if not snapshots_dir.is_dir():
        return {"present": False, "schema": None, "cases": []}

    schema = _read_json(
        snapshots_dir / "snapshot_schema.json", problems,
        "raw/state_snapshots/snapshot_schema.json")
    declared: dict[str, Any] = {}
    time_variable = None
    if isinstance(schema, dict):
        time_variable = schema.get("time_variable")
        for item in schema.get("variables") or []:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                declared[item["name"]] = item.get("shape_expr")

    cases: list[dict[str, Any]] = []
    try:
        files = sorted(p for p in snapshots_dir.iterdir()
                       if p.is_file() and p.name != "snapshot_schema.json")
    except OSError as exc:
        problems.append(f"raw/state_snapshots: unreadable ({type(exc).__name__})")
        files = []
    for path in files:
        label = f"raw/state_snapshots/{path.name}"
        doc = _read_json(path, problems, label)
        case: dict[str, Any] = {"case_id": path.stem, "variables": []}
        if doc is None:
            case["read"] = False
            cases.append(case)
            continue
        case["read"] = True
        if not isinstance(doc, dict):
            problems.append(f"{label}: not a JSON object")
            cases.append(case)
            continue
        if isinstance(time_variable, str):
            case["time_variable"] = time_variable
            case["time_value"] = _summarize(doc.get(time_variable)) if (
                time_variable in doc) else None
        for name in sorted(k for k in doc if k != time_variable):
            variable = {"name": name, "declared_shape_expr": declared.get(name)}
            variable.update(_summarize(doc[name]))
            case["variables"].append(variable)
        case["declared_variables_absent"] = sorted(
            n for n in declared if n not in doc and n != time_variable)
        cases.append(case)
    return {
        "present": True,
        "schema": {
            "time_variable": time_variable,
            "declared_variables": [
                {"name": n, "shape_expr": declared[n]} for n in sorted(declared)],
            "min_samples": schema.get("min_samples") if isinstance(schema, dict) else None,
        },
        "cases": cases,
    }


def _required_evidence_rows(raw_dir: Path, io_contract: Any) -> list[dict[str, Any]]:
    """Each declared `raw_requirements.required_evidence` artifact against what is on disk.

    The declared spelling is kept beside the canonical name: an artifact the aliases do not
    recognise is a contract the gate silently ignores, and saying so is more useful to the
    judge than dropping the row.
    """
    raw_requirements = (io_contract or {}).get("raw_requirements") \
        if isinstance(io_contract, dict) else None
    declared = raw_requirements.get("required_evidence") \
        if isinstance(raw_requirements, dict) else None
    if not isinstance(declared, list):
        return []

    rows: list[dict[str, Any]] = []
    for item in declared:
        if not isinstance(item, dict):
            continue
        spelling = item.get("artifact")
        if not isinstance(spelling, str):
            continue
        artifact = normalize_raw_evidence_artifact(spelling)
        target = raw_dir / artifact if artifact else None
        rows.append({
            "declared_as": spelling,
            "artifact": artifact,
            "required": item.get("required") is not False,
            "present": bool(target and (target.is_file() or target.is_dir())),
        })
    return rows


# --------------------------------------------------------------------------------------
# Summarizing one value without inlining it
# --------------------------------------------------------------------------------------


def _summarize(value: Any) -> dict[str, Any]:
    """Summarize one raw value: a scalar by its value, an array by its statistics.

    The statistics are chosen for what a judge is actually asked: `nan_count` / `inf_count`
    because a non-finite entry invalidates whatever metric was computed from it, `all_zero`
    and a collapsed extent because those are the shapes an array takes when it was never
    written, and `shape` because an array of the wrong rank is a runner defect no numeric
    check would notice.
    """
    if isinstance(value, bool) or value is None:
        return {"kind": "scalar", "value": value}
    if isinstance(value, (int, float)):
        return {"kind": "scalar", "value": _json_number(value)}
    if isinstance(value, str):
        text = value if len(value) <= _MAX_TEXT_CHARS else value[:_MAX_TEXT_CHARS] + "..."
        return {"kind": "text", "value": text, "length": len(value)}
    if isinstance(value, dict):
        return {"kind": "object", "keys": sorted(str(k) for k in value)}
    if not isinstance(value, list):
        return {"kind": "unknown", "type": type(value).__name__}

    shape, ragged = _shape(value)
    stats = _ArrayStats()
    _walk(value, stats)
    summary: dict[str, Any] = {
        "kind": "array",
        "shape": shape,
        "ragged": ragged,
        "count": stats.count,
        "numeric_count": stats.numeric,
        "nan_count": stats.nan,
        "inf_count": stats.inf,
        "non_numeric_count": stats.non_numeric,
    }
    if stats.numeric and stats.finite:
        summary["min"] = _json_number(stats.minimum)
        summary["max"] = _json_number(stats.maximum)
        summary["all_zero"] = stats.all_zero
    else:
        summary["min"] = None
        summary["max"] = None
        summary["all_zero"] = False
    return summary


def _shape(value: Any) -> tuple[list[int], bool]:
    """The nested length of an array, and whether its sublists disagree on any level."""
    shape: list[int] = []
    ragged = False
    level: Any = value
    while isinstance(level, list):
        shape.append(len(level))
        children = [item for item in level if isinstance(item, list)]
        if children and len(children) != len(level):
            ragged = True
        if not children:
            break
        lengths = {len(item) for item in children}
        if len(lengths) > 1:
            ragged = True
        level = children[0]
    return shape, ragged


class _ArrayStats:
    """Running statistics over the numeric leaves of a nested array."""

    def __init__(self) -> None:
        self.count = 0
        self.numeric = 0
        self.non_numeric = 0
        self.finite = 0
        self.nan = 0
        self.inf = 0
        self.minimum = math.inf
        self.maximum = -math.inf
        self.all_zero = True


def _walk(value: Any, stats: _ArrayStats) -> None:
    """Fold every leaf of a nested list into `stats`. Iterative, so a deeply nested document
    cannot exhaust the interpreter's stack — the input is leaf-authored evidence, and the
    excerpt must not be the thing that crashes on it."""
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, list):
            pending.extend(item)
            continue
        stats.count += 1
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            stats.non_numeric += 1
            stats.all_zero = False
            continue
        stats.numeric += 1
        number = float(item)
        if math.isnan(number):
            stats.nan += 1
            stats.all_zero = False
            continue
        if math.isinf(number):
            stats.inf += 1
            stats.all_zero = False
            continue
        stats.finite += 1
        stats.minimum = min(stats.minimum, number)
        stats.maximum = max(stats.maximum, number)
        if number != 0.0:
            stats.all_zero = False


def _json_number(value: float) -> Any:
    """A number JSON can carry. `nan` / `inf` are not JSON, and the counts already say they
    were there, so the value itself becomes a name rather than a token `json.dumps` emits and
    no strict parser accepts."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    number = float(value)
    if math.isnan(number):
        return "nan"
    if math.isinf(number):
        return "inf" if number > 0 else "-inf"
    return number
