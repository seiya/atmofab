#!/usr/bin/env python3
"""What an `io_contract` says about raw evidence, and what a `raw/` directory actually holds.

**The excerpt** — `raw_evidence_excerpt` — is the judge's window onto `raw/`. Issue #169 took the
filesystem away from `validate.judge`'s context, and `raw/` is megabytes of float arrays
(`shallow_water2d`: twelve snapshots of 100 KB - 1.65 MB, all `[nx, ny]`), so it is not seeded
whole. What the host seeds instead is this excerpt: per-array SUMMARIES — shape, extent,
non-finite counts, all-zero — from
which the judge can ask whether the host's records are supported by the state they read. These
are FACTS, not verdicts: an all-zero array is ordinary physics on many nodes (see `_summarize`),
and what makes one a finding is what the tests and the IO contract say that variable should do.

**No numeric array is ever inlined**, at any size. The output is bounded by
(snapshot x variable). That is the policy, and `RAW_EXCERPT_POLICY_VERSION` stamps which version
of it a review was made under; `docs/workflow/phases/phase_04_validate.md` §4-2 is canonical for
what it includes and excludes.

`normalize_raw_evidence_artifact` also lives here: the validator imports it, so the gate and the
excerpt read one definition of an artifact's spelling.

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
#: 2: each snapshot case additionally carries `initial` — the summary of the host-rendered
#: runner's capture before the case's step (`raw/state_snapshots/initial/<case_id>.json`,
#: Z6, issue #255) — so the judge sees both capture points of a case.
#: `schema.min_samples` was dropped without a bump (issue #396): `phase_04_validate.md` §4-2-1,
#: canonical for the contents, never listed it, and it echoed the IR's declaration rather than
#: a fact about `raw/`. The version is in the `validate` transformation tuple, so a bump would
#: re-derive every certified Validate for a display key no gate reads.
#: 3 (issue #582): the `coverage` and `metrics_basis_arrays` sections are gone with
#: `raw/metrics_basis.json`, and each snapshot variable names its `source`.
#: 4 (issue #589 PR-1b): `source` is gone with its vocabulary, `step` is the driver's own column
#: (an ordinary row with no declared shape), each case carries `history_samples` and `series`, and `required_evidence`
#: looks for `state_history` / `step_series` where the driver writes them.
RAW_EXCERPT_POLICY_VERSION = 4

#: Spellings the IR may use for a raw-evidence artifact, mapped to its canonical name.
RAW_EVIDENCE_ALIASES = {
    "state_snapshots": "state_snapshots",
    "raw/state_snapshots": "state_snapshots",
    "raw/state_snapshots/": "state_snapshots",
    # Issue #589: the driver's samples and its per-step series (`_EVIDENCE_DIRS`).
    "state_history": "state_history",
    "step_series": "step_series",
}

#: Where under `raw/` a canonical artifact lives, when that is not its own name.
_EVIDENCE_DIRS = {"state_history": "state_snapshots/history", "step_series": "state_snapshots/series"}

#: How many distinct string values a non-numeric field is summarized by before the summary
#: says "and more" — the excerpt is a summary, and an unbounded one is the thing it exists
#: to avoid.
_MAX_TEXT_CHARS = 200


def normalize_raw_evidence_spelling(token: str) -> str:
    """The one spelling rule every raw-evidence token reader applies before lookup."""
    return token.strip().lower().replace("\\", "/")


def normalize_raw_evidence_artifact(token: str) -> str | None:
    """The canonical artifact name for an IR spelling, or None when it names none."""
    return RAW_EVIDENCE_ALIASES.get(normalize_raw_evidence_spelling(token))


# --------------------------------------------------------------------------------------
# The expected evidence matrix, read from the contract
# --------------------------------------------------------------------------------------


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
    return {
        "policy_version": RAW_EXCERPT_POLICY_VERSION,
        "raw_dir_present": raw_dir.is_dir(),
        "required_evidence": _required_evidence_rows(raw_dir, io_contract),
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


def _state_snapshots(raw_dir: Path, problems: list[str]) -> dict[str, Any]:
    """Each per-case snapshot summarized against `snapshot_schema.json`'s declaration.

    The declared shape travels with the summary so the judge can compare them: a snapshot
    whose array is not the shape the schema declares is evidence about the runner, and it is
    invisible from the numbers alone.

    A case whose runner is host-rendered has TWO captures (Z6, issue #255): the final one at
    `<case_id>.json` and the initial one at `initial/<case_id>.json`, taken before the first
    call. The initial capture is summarized under the case's `initial` key by the same
    summarizer, or `None` when the runner wrote none (a harness self-test's own runner). On a
    `loop` node (issue #589) `history_samples` counts the case's `history/` files and `series` is
    the summary of its `series/` file: each name's length, minimum and maximum, or `None`.
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
        case = _snapshot_case(path, f"raw/state_snapshots/{path.name}", declared,
                              time_variable, problems)
        initial_path = snapshots_dir / "initial" / path.name
        case["initial"] = (
            _snapshot_case(initial_path, f"raw/state_snapshots/initial/{path.name}",
                           declared, time_variable, problems)
            if initial_path.is_file() else None)
        case["history_samples"] = _history_count(snapshots_dir, path.stem)
        case["series"] = _series_summary(snapshots_dir, path.name, problems)
        cases.append(case)
    return {
        "present": True,
        "schema": {
            "time_variable": time_variable,
            "declared_variables": [
                {"name": n, "shape_expr": declared[n]} for n in sorted(declared)],
        },
        "cases": cases,
    }


def _history_count(snapshots_dir: Path, case_id: str) -> int:
    """How many `history/<case_id>__<k>.json` samples the case has (0: none, or no `history/`)."""
    history = snapshots_dir / "history"
    if not history.is_dir():
        return 0
    prefix = f"{case_id}__"
    return sum(1 for p in history.iterdir()
               if p.is_file() and p.name.startswith(prefix) and p.suffix == ".json"
               and p.stem[len(prefix):].isdigit())


def _series_summary(snapshots_dir: Path, name: str, problems: list[str]) -> dict[str, Any] | None:
    """Each series name of the case summarized like a variable (a non-finite entry counted, none
    inlined), or None when the case has no `series/` file."""
    path = snapshots_dir / "series" / name
    if not path.is_file():
        return None
    doc = _read_json(path, problems, f"raw/state_snapshots/series/{name}")
    if not isinstance(doc, dict):
        return None
    return {key: _summarize(doc[key]) for key in sorted(doc)}


def _snapshot_case(path: Path, label: str, declared: dict[str, Any], time_variable: Any,
                   problems: list[str]) -> dict[str, Any]:
    """One snapshot file summarized against the declaration (the per-case body of
    `_state_snapshots`, shared by the final and the initial capture of a case)."""
    doc = _read_json(path, problems, label)
    case: dict[str, Any] = {"case_id": path.stem, "variables": []}
    if doc is None:
        case["read"] = False
        return case
    case["read"] = True
    if not isinstance(doc, dict):
        problems.append(f"{label}: not a JSON object")
        return case
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
    return case


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
        target = raw_dir / _EVIDENCE_DIRS.get(artifact, artifact) if artifact else None
        rows.append({
            "declared_as": spelling,
            "artifact": artifact,
            # `every: 0` is a legal stride, so an absent `history/` is no missing evidence: a
            # predicate over `history.*` is what needs samples, and it answers `structural`.
            "required": item.get("required") is not False and artifact != "state_history",
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
    and a collapsed extent because an array that was never written looks like that, and
    `shape` because an array of the wrong rank is a runner defect no numeric check would
    notice. An empty array reports only the extents its JSON spells: nested lists stop at a zero,
    so `[[],[],[]]` reads `[3, 0]` whatever rank was declared (issue #602).

    `all_zero` IS NOT EVIDENCE OF FABRICATION, and an earlier version of this docstring said it
    was ("the shape an array takes when it was never written", full stop). MEASURED against the
    corpus: on `shallow_water2d`'s `run_20260802_001` — certified `pass` by the agentic judge —
    20 arrays are all-zero, every one of them `hv` (transverse momentum in a setup with none) or
    `z_b` (a flat bed). A physically-zero field is ordinary, and the excerpt cannot tell it from
    an unwritten one: `min` and `max` are `0.0` either way. So this flag is a FACT to be weighed
    against what the tests and the IO contract say the variable should do, and the prompt says so
    in those terms. Reporting it as fabrication made a compliant judge fail sound runs.
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
    """The nested length of an array, and whether its sublists disagree on any level.

    EVERY branch is inspected, not just the first. Descending through `children[0]` alone —
    which this did until a review round — misses raggedness under any later sibling:
    `[[[1,2],[3,4]], [[5],[6,7,8]]]` reported shape `[2,2,2]` and `ragged=False`, and its
    element count matched the rectangular shape too, so nothing in the summary betrayed it. A
    runner emitting malformed evidence would have had it presented to the judge as structurally
    sound, which is the one thing this summary exists to prevent.
    """
    shape: list[int] = []
    ragged = False
    level: list[Any] = [value] if isinstance(value, list) else []
    while level:
        lengths = {len(item) for item in level}
        shape.append(max(lengths))
        if len(lengths) > 1:
            ragged = True
        children: list[Any] = []
        listed = 0
        for item in level:
            for element in item:
                if isinstance(element, list):
                    children.append(element)
                    listed += 1
        total = sum(len(item) for item in level)
        # A level whose members are SOME lists and some scalars is ragged in the other
        # direction — the array changes rank partway across.
        if children and listed != total:
            ragged = True
        level = children
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
