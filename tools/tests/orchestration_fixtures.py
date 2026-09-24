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

Since issue #250 PR-2 "certified" is decided by the DERIVATION KEY, so the chain also
carries, per phase, the `derivation_key` / `derivation_inputs` / `derivation_transformation`
/ `output_hash` the stamp writes (`stamp=True`, the default). The key is what the production
resolver recomputes (`phase_derivation` over the fixture's own spec files, catalog entry and
upstream outputs) — the fixture cannot invent it, and does not try: it asks the same function
the predicate asks, so a test that then edits one input observes exactly the mismatch a real
edit produces. `ensure_spec_entry` writes the minimal spec directory + catalog entry the key
needs; `certify_node` calls it unless told the caller owns the registry.

Since issue #284 (R4-a PR-2) every Generate / Build / Validate output is a `node_key × target`
fact: `certify_node` writes the pipeline under `workspace/pipelines/<safe>/<target_id>/`, keys
it for `target` (the checked-in profile by default, `target_fixtures.FORTRAN_CPU`), records the
target on the pipeline reservation, installs the profile file into the fixture repository (the
readers that learn the target from a path load it from there) and, when the orchestration
exists, records it as `orchestration_meta.json#invocation.target` — the target the
orchestration-scoped predicates ask for.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from unittest import mock

_PHASE_ORDER = ("compile", "generate", "build", "validate")

#: The canonical empty `deps.yaml` (`_deps_yaml_bytes_are_canonical_empty` recognizes it).
EMPTY_DEPS_YAML = "dependencies:\n  components: []\n  profiles: []\n  infrastructure: []\n"


def spec_ref_of(node_key: str) -> str:
    """The spec directory `ensure_spec_entry` writes for `node_key`: `spec/<kind>/<spec_id>`."""
    kind, rest = node_key.split("/", 1)
    return f"spec/{kind}/{rest.split('@', 1)[0]}"


def ensure_spec_entry(
    repo_root: Path,
    node_key: str,
    *,
    deps_yaml: str | None = None,
    controlled_spec: str | None = None,
    tests_md: str | None = None,
) -> str:
    """Write the minimal spec directory (`controlled_spec.md`, `tests.md`, `deps.yaml`) and
    the `spec_catalog.yaml` entry that resolve `node_key`'s spec_ref and version, keeping
    whatever the catalog already holds. A catalog that already resolves `(kind, spec_id)`
    to a directory keeps that directory (a test that seeded its own registry layout); the
    default layout is `spec/<kind>/<spec_id>`. Existing spec files are left as they are
    unless a body is passed. Returns the spec_ref."""
    import yaml

    kind, rest = node_key.split("/", 1)
    spec_id, version = rest.split("@", 1)
    catalog_path = repo_root / "spec" / "registry" / "spec_catalog.yaml"
    doc: dict[str, Any] = {}
    if catalog_path.is_file():
        loaded = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
        doc = loaded if isinstance(loaded, dict) else {}
    specs = doc.get("specs")
    if not isinstance(specs, list):
        specs = []
    spec_ref = spec_ref_of(node_key)
    for e in specs:
        if not (isinstance(e, dict) and e.get("spec_kind") == kind and e.get("spec_id") == spec_id):
            continue
        ref_source = e.get("deps_path") or e.get("controlled_spec_path")
        if isinstance(ref_source, str) and ref_source.strip():
            spec_ref = str(Path(ref_source.strip()).parent).strip("/")
            break
    spec_dir = repo_root / spec_ref
    spec_dir.mkdir(parents=True, exist_ok=True)
    for name, body, default in (
        ("controlled_spec.md", controlled_spec, f"# {spec_id}\n\nversion {version}\n"),
        ("tests.md", tests_md, f"# tests for {spec_id}\n"),
        ("deps.yaml", deps_yaml, EMPTY_DEPS_YAML),
    ):
        path = spec_dir / name
        if body is not None or not path.is_file():
            path.write_text(body if body is not None else default, encoding="utf-8")
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "spec_kind": kind, "spec_id": spec_id, "spec_version": version,
        "status": "controlled_draft",
        "controlled_spec_path": f"{spec_ref}/controlled_spec.md",
        "tests_path": f"{spec_ref}/tests.md",
        "deps_path": f"{spec_ref}/deps.yaml",
    }
    existing = [e for e in specs if isinstance(e, dict) and e.get("spec_kind") == kind
                and e.get("spec_id") == spec_id and e.get("spec_version") == version]
    if not existing:
        specs.append(entry)
    for e in existing:
        # An entry a test wrote without paths (the older fixtures' shape): give it the
        # paths that resolve it, keeping whatever else it carries.
        if not (e.get("deps_path") or e.get("controlled_spec_path")):
            e.update({k: entry[k] for k in ("controlled_spec_path", "tests_path", "deps_path")})
    doc.setdefault("catalog_version", "0.2.0")
    doc.setdefault("updated_at", "2026-01-01")
    doc["specs"] = specs
    catalog_path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return spec_ref


def record_orchestration_target(repo_root: Path, orchestration_id: str,
                                target: Any = None) -> None:
    """Record `target` as the orchestration's `invocation.target` (what `run_workflow.py`
    writes at launch), when the orchestration's meta exists; a no-op otherwise."""
    from tools.tests.target_fixtures import fixture_target
    target = fixture_target(repo_root, target)
    meta_path = (repo_root / "workspace" / "orchestrations" / orchestration_id
                 / "orchestration_meta.json")
    if not meta_path.is_file():
        return
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    invocation = meta.get("invocation") if isinstance(meta.get("invocation"), dict) else {}
    invocation["target"] = {"target_id": target.target_id, "sha256": target.sha256}
    meta["invocation"] = invocation
    _write_json(meta_path, meta)


def write_dependency_graph_sidecar(repo_root: Path, node_key: str, ir_ref: str,
                                   *, spec_ref: str | None = None) -> dict[str, Any]:
    """Author `<ir_ref>/dependency_graph.json` the way the conductor does at Compile start
    (`Conductor._write_dependency_graph`: the real builder over deps.yaml + the catalog), so
    the closure the generate / build keys read is the one the registry derives. Raises when
    the closure does not build — a fixture whose dependencies are not in the catalog yet."""
    from tools.dependency_graph import build_dependency_graph

    from tools.orchestration_runtime import DerivationResolver

    if spec_ref is None:
        spec_ref = DerivationResolver(repo_root).spec_ref(node_key) or spec_ref_of(node_key)
    graph, err = build_dependency_graph(
        repo_root, target_spec_ref=spec_ref, target_node_key=node_key)
    if err is not None:
        raise RuntimeError(f"dependency graph of {node_key} does not build: {err}")
    _write_json(repo_root / ir_ref / "dependency_graph.json", graph)
    return graph


def stamp_derivation(
    repo_root: Path, node_key: str, step: str, meta_ref: str, *,
    spec_ref: str | None = None, ir_ref: str | None = None,
    source_ref: str | None = None, binary_ref: str | None = None,
    target: Any = None,
) -> dict[str, Any]:
    """Stamp the derivation record and `output_hash` of ONE phase into its certifying meta at
    `meta_ref` (repo-relative), computed the way the stamp computes them: the key over
    `phase_derivation`'s inputs NOW (for `target`, the checked-in profile by default), the
    output hash over the meta's own `artifact_hashes`. Returns the stamped document."""
    from tools.derivation import output_hash
    from tools.orchestration_runtime import DerivationResolver, phase_derivation
    from tools.tests.target_fixtures import fixture_target

    if spec_ref is None:
        spec_ref = DerivationResolver(repo_root).spec_ref(node_key) or spec_ref_of(node_key)
    record = phase_derivation(
        repo_root, node_key=node_key, step=step,
        spec_ref=spec_ref, ir_ref=ir_ref,
        source_ref=source_ref, binary_ref=binary_ref,
        target=fixture_target(repo_root, target))
    path = repo_root / meta_ref
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["derivation_key"] = record["derivation_key"]
    doc["derivation_inputs"] = record["derivation_inputs"]
    doc["derivation_transformation"] = record["transformation"]
    doc["output_hash"] = output_hash(doc["artifact_hashes"], stage_dir=meta_ref.rsplit("/", 1)[0])
    _write_json(path, doc)
    return doc


def accept_any_certified_ir() -> mock._patch:
    """A patch that makes the compile clause's validator re-check (issue #238) accept the IR.

    The stub `spec.ir.yaml` `certify_node` writes is not a document the compile-stage
    validator passes, so every test that builds a certified chain here and then asks
    `_phase_certified` / `_certified_ir_candidate` / the completion vouch would otherwise be
    refused with `ir_rejected_by_current_validator:…` and prove nothing about the clause it
    names. Spelled once so the seam name lives in one place; the rows that pin the validator
    clause itself do NOT use this and patch the seam with a verdict of their own.
    """
    from tools import orchestration_runtime
    return mock.patch.object(orchestration_runtime, "_certified_ir_violations", return_value=[])


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
    stamp: bool = True,
    spec_entry: bool = True,
    model_text: str | None = None,
    ir_text: str | None = None,
    exe_bytes: bytes | None = None,
    target: Any = None,
) -> dict[str, str]:
    """Write the certified artifact chain for `node_key` up to and including `through`.

    Returns the refs a test needs to reach into what it built (`ir_ref`, `pipeline_ref`,
    the per-stage meta paths and the ids). Every meta carries the `artifact_hashes` of its
    own deliverables, the binding to the stage above it and (`stamp`) the derivation record
    the key lookup selects by, so the chain satisfies `_phase_certified` end to end; a test
    that wants a refusal mutates exactly one link. `spec_entry` writes the spec directory
    and catalog entry the key resolves (`ensure_spec_entry`); pass `False` when the test
    owns the registry and has already written an entry for this node.
    """
    from tools.tests.target_fixtures import fixture_target, install_target_profile
    from tools.tests.target_fixtures import pipe_ref as _pr
    # The repository's own declared profile when it has one (a test that declared a variant
    # certifies FOR it), else the checked-in one — the rule the conductor fakes read by.
    target = fixture_target(repo_root, target)
    safe = node_safe(node_key)
    spec_id = spec_id_of(node_key)
    idx = _PHASE_ORDER.index(through)
    if spec_entry:
        ensure_spec_entry(repo_root, node_key)
    install_target_profile(repo_root, target)
    record_orchestration_target(repo_root, orchestration_id, target)

    def _stamp(step: str, meta_ref: str, **refs: str | None) -> None:
        if stamp:
            stamp_derivation(repo_root, node_key, step, meta_ref, target=target, **refs)
    orch_root = repo_root / "workspace" / "orchestrations" / orchestration_id
    ir_dir = repo_root / "workspace" / "ir" / safe / ir_id
    pipe_ref = _pr(safe, pipeline_id, target.target_id)
    pipe_dir = repo_root / pipe_ref
    ir_ref = f"workspace/ir/{safe}/{ir_id}"

    if reserve:
        for step, reserved in (("compile", ir_id), ("generate", pipeline_id)):
            _write_json(orch_root / "reservations" / safe / f"{step}.json", {
                "node_key": node_key, "step": step, "reserved_ir_id": reserved,
                "reserved_by_agent_run_id": "orch_run_001", "status": "reserved",
                **({"target_id": target.target_id} if step == "generate" else {}),
            })

    ir_dir.mkdir(parents=True, exist_ok=True)
    spec_ir = ir_dir / "spec.ir.yaml"
    spec_ir.write_text(ir_text if ir_text is not None else f"node_key: {node_key}\n",
                       encoding="utf-8")
    _write_json(ir_dir / "ir_meta.json", {
        "ir_id": ir_id, "node_key": node_key, "attempt_count": 1,
        "verification_status": "pass", "last_fail_reason": None,
        "debug_mode": False, "context_isolated": True,
        "artifact_hashes": {f"{ir_ref}/spec.ir.yaml": _sha256(spec_ir)},
    })
    refs = {"safe": safe, "ir_id": ir_id, "ir_ref": ir_ref,
            "ir_meta": f"{ir_ref}/ir_meta.json"}
    if stamp:
        write_dependency_graph_sidecar(repo_root, node_key, ir_ref)
    _stamp("compile", refs["ir_meta"])
    if idx == 0:
        return refs

    src_dir = pipe_dir / "source" / source_id / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    model = src_dir / f"{spec_id}_model.f90"
    model.write_text(model_text if model_text is not None
                     else f"module {spec_id}_model\nend module\n", encoding="utf-8")
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
    _stamp("generate", refs["source_meta"], ir_ref=ir_ref)
    if idx == 1:
        return refs

    bin_dir = pipe_dir / "binary" / binary_id / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    exe = bin_dir / f"{spec_id}_runner"
    exe.write_bytes(exe_bytes if exe_bytes is not None else b"\x7fELF-fixture")
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
    _stamp("build", refs["binary_meta"], ir_ref=ir_ref,
           source_ref=f"{pipe_ref}/source/{source_id}")
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
    # The rest of Validate's declared deliverables, hashed into `validate_meta.json`'s
    # `artifact_hashes` the way `write_step_result` stamps them (Validate certifies its meta
    # since issue #250 PR-1, and the key lookup reads it since PR-2).
    _write_json(run_node / "verdict.json", {
        "node_key": node_key, "run_id": run_id, "self_verdict": "pass", "failure_class": None,
    })
    _write_json(run_node / "summary.json", {
        "node_key": node_key, "run_id": run_id, "total": 0, "pass": 0, "fail": 0,
    })
    _write_json(run_node / "semantic_review.json", {
        "decision": "pass", "findings": [],
    })
    run_ref = f"{pipe_ref}/runs/{run_id}/{safe}"
    _write_json(run_node / "validate_meta.json", {
        "run_id": run_id, "node_key": node_key, "pipeline_id": pipeline_id,
        "verification_status": "pass", "attempt_count": 1,
        # Exactly the declared deliverables minus the meta itself
        # (`phase_required_outputs(..., "validate")`); `trial_meta.json` and
        # `post_judge_meta.json` are host records outside the declared set.
        "artifact_hashes": {
            f"{run_ref}/{name}": _sha256(run_node / name)
            for name in ("aggregate_verdict.json", "verdict.json", "summary.json",
                         "semantic_review.json")},
    })
    refs |= {"run_id": run_id,
             "aggregate_verdict": f"{run_ref}/aggregate_verdict.json",
             "post_judge_meta": f"{run_ref}/post_judge_meta.json",
             "validate_meta": f"{run_ref}/validate_meta.json",
             "run_node_dir": run_ref}
    _stamp("validate", refs["validate_meta"], ir_ref=ir_ref,
           binary_ref=f"{pipe_ref}/binary/{binary_id}")
    return refs
