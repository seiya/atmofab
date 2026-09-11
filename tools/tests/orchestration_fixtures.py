#!/usr/bin/env python3
"""Shared orchestration fixtures for the runtime / conductor test suites.

`certify_node` builds, on disk, the artifact chain that makes a node CERTIFIED — the thing
`_phase_certified` (and, from issue #177's PR-2, the completion vouch) actually reads. It
exists because that chain is long enough that hand-rolling it per test produced fixtures
which were subtly not certifiable, and because a test asserting a REFUSAL must start from a
state that would otherwise pass, or it proves nothing about the clause it names.

The hashes are computed here from the bytes written, exactly as `_stamp_certification`
computes them — deliberately not copied from the implementation, so a change to the stamp's
recorded shape shows up as a failing certification rather than as a fixture that agrees with
the code by construction.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

_PHASE_ORDER = ("compile", "generate", "build", "validate")


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def node_safe(node_key: str) -> str:
    kind, rest = node_key.split("/", 1)
    spec_id, version = rest.split("@", 1)
    return f"{kind}__{spec_id}__{version}"


def spec_id_of(node_key: str) -> str:
    return node_key.split("/", 1)[1].split("@", 1)[0]


def certify_node(
    repo_root: Path,
    orchestration_id: str,
    node_key: str = "component/spec_x@0.1.0",
    *,
    through: str = "validate",
    ir_id: str = "spec-x_20260101_001",
    pipeline_id: str = "spec-x_20260101_001",
    source_id: str = "src_20260101_001",
    binary_id: str = "bin_20260101_001",
    run_id: str = "run_20260101_001",
    reserve: bool = True,
) -> dict[str, str]:
    """Write the certified artifact chain for `node_key` up to and including `through`.

    Returns the refs a test needs to reach into what it built (`ir_ref`, `pipeline_ref`,
    the per-stage meta paths and the ids). Every meta carries the `artifact_hashes` of its
    own deliverables and the binding to the stage above it, so the chain satisfies
    `_phase_certified` end to end; a test that wants a refusal mutates exactly one link.
    """
    safe = node_safe(node_key)
    spec_id = spec_id_of(node_key)
    idx = _PHASE_ORDER.index(through)
    orch_root = repo_root / "workspace" / "orchestrations" / orchestration_id
    ir_dir = repo_root / "workspace" / "ir" / safe / ir_id
    pipe_dir = repo_root / "workspace" / "pipelines" / safe / pipeline_id
    ir_ref = f"workspace/ir/{safe}/{ir_id}"
    pipe_ref = f"workspace/pipelines/{safe}/{pipeline_id}"

    if reserve:
        for step, reserved in (("compile", ir_id), ("generate", pipeline_id)):
            _write_json(orch_root / "reservations" / safe / f"{step}.json", {
                "node_key": node_key, "step": step, "reserved_ir_id": reserved,
                "reserved_by_agent_run_id": "orch_run_001", "status": "reserved",
            })

    ir_dir.mkdir(parents=True, exist_ok=True)
    spec_ir = ir_dir / "spec.ir.yaml"
    spec_ir.write_text(f"node_key: {node_key}\n", encoding="utf-8")
    _write_json(ir_dir / "ir_meta.json", {
        "ir_id": ir_id, "node_key": node_key, "attempt_count": 1,
        "verification_status": "pass", "last_fail_reason": None,
        "debug_mode": False, "context_isolated": True,
        "artifact_hashes": {f"{ir_ref}/spec.ir.yaml": _sha256(spec_ir)},
    })
    refs = {"safe": safe, "ir_id": ir_id, "ir_ref": ir_ref,
            "ir_meta": f"{ir_ref}/ir_meta.json"}
    if idx == 0:
        return refs

    src_dir = pipe_dir / "source" / source_id / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    model = src_dir / f"{spec_id}_model.f90"
    model.write_text(f"module {spec_id}_model\nend module\n", encoding="utf-8")
    model_ref = f"{pipe_ref}/source/{source_id}/src/{spec_id}_model.f90"
    _write_json(pipe_dir / "source" / source_id / "source_meta.json", {
        "source_id": source_id, "node_key": node_key, "attempt_count": 1,
        "verification_status": "pass", "last_fail_reason": None,
        "debug_mode": False, "context_isolated": True,
        "source_ir_id": ir_id,
        "artifact_hashes": {model_ref: _sha256(model)},
    })
    _write_json(pipe_dir / "lineage.json", {
        "node_key": node_key, "ir_ref": ir_ref, "pipeline_id": pipeline_id,
        "source_id": source_id,
        "binary_id": binary_id if idx >= 2 else None,
        "run_id": run_id if idx >= 3 else None,
    })
    refs |= {"pipeline_id": pipeline_id, "pipeline_ref": pipe_ref, "source_id": source_id,
             "source_meta": f"{pipe_ref}/source/{source_id}/source_meta.json",
             "model_ref": model_ref}
    if idx == 1:
        return refs

    bin_dir = pipe_dir / "binary" / binary_id / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    exe = bin_dir / f"{spec_id}_runner"
    exe.write_bytes(b"\x7fELF-fixture")
    exe_ref = f"{pipe_ref}/binary/{binary_id}/bin/{spec_id}_runner"
    _write_json(pipe_dir / "binary" / binary_id / "binary_meta.json", {
        "binary_id": binary_id, "node_key": node_key, "pipeline_id": pipeline_id,
        "attempt_count": 1, "verification_status": "pass", "last_fail_reason": None,
        "debug_mode": False, "context_isolated": True,
        "source_source_id": source_id, "source_ir_id": ir_id,
        "binary_artifact_ref": f"binary/{binary_id}/bin/{spec_id}_runner",
        "dependency_check": {"direct_deps": [], "resolved": "match", "closure_bindings": []},
        "artifact_hashes": {exe_ref: _sha256(exe)},
    })
    refs |= {"binary_id": binary_id,
             "binary_meta": f"{pipe_ref}/binary/{binary_id}/binary_meta.json",
             "exe_ref": exe_ref}
    if idx == 2:
        return refs

    run_node = pipe_dir / "runs" / run_id / safe
    _write_json(run_node / "trial_meta.json", {
        "run_id": run_id, "node_key": node_key, "pipeline_id": pipeline_id,
        "source_source_id": source_id, "source_binary_id": binary_id, "status": "pass",
    })
    _write_json(run_node / "aggregate_verdict.json", {
        "node_key": node_key, "run_id": run_id, "aggregate_verdict": "pass",
    })
    # The host record of the `--stage pre_judge` gate's own outcome. Validate is not
    # certified by the verdict alone: the verdict is authored BEFORE that gate runs, so a
    # phase that fail-closed on the gate leaves a passing verdict behind.
    _write_json(run_node / "post_judge_meta.json", {
        "run_id": run_id, "node_key": node_key, "pipeline_id": pipeline_id,
        "status": "pass", "validation_stage": "pre_judge", "failure_category": None,
        "failure_excerpt": None, "violations": [], "disposition": None,
    })
    # The rest of Validate's declared deliverables. `_phase_certified` requires all of them:
    # Validate carries no `artifact_hashes` stamp, so their presence is the only thing standing
    # between "the chain reads as complete" and "the attempt actually finished writing".
    _write_json(run_node / "verdict.json", {
        "node_key": node_key, "run_id": run_id, "self_verdict": "pass", "failure_class": None,
    })
    _write_json(run_node / "summary.json", {
        "node_key": node_key, "run_id": run_id, "total": 0, "pass": 0, "fail": 0,
    })
    _write_json(run_node / "semantic_review.json", {
        "decision": "pass", "findings": [],
    })
    _write_json(run_node / "validate_meta.json", {
        "run_id": run_id, "node_key": node_key, "pipeline_id": pipeline_id,
        "verification_status": "pass", "attempt_count": 1,
    })
    refs |= {"run_id": run_id,
             "aggregate_verdict": f"{pipe_ref}/runs/{run_id}/{safe}/aggregate_verdict.json",
             "post_judge_meta": f"{pipe_ref}/runs/{run_id}/{safe}/post_judge_meta.json",
             "run_node_dir": f"{pipe_ref}/runs/{run_id}/{safe}"}
    return refs
