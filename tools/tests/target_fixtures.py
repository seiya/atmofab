"""Target-profile fixtures shared by the suite (issue #284, R4-a PR-2).

Since R4-a PR-2 every Generate / Build / Validate artifact is a `node_key × target` fact: it
lives under `workspace/pipelines/<node_key_safe>/<target_id>/<pipeline_id>/`, and every reader
that holds only a path loads the target profile the path names from the repository it is
given. A fixture repository therefore needs the profile file itself, not just the path segment.

`FORTRAN_CPU` is the checked-in profile, loaded from THIS checkout — a test that needs "the"
target reads it from here rather than restating a technology name. `SECOND_TARGET` is the
fixture second target the R4-a plan names (`fortran_cpu_t2`): the same document under another
id, so anything keyed by the target differs and nothing else does.
"""

from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path

from tools.derivation import canonical_json_bytes, sha256_hex
from tools.target_profile import (
    TARGET_PROFILE_SUFFIX,
    TARGETS_DIR,
    TargetProfile,
    load_target_profile,
    pipeline_ref_for,
    pipelines_dir,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The `fortran_cpu` profile, loaded the way a run loads it. Named, not discovered: the checkout
#: declares more than one profile since issue #289 (R4-b PR-5), and every fixture built on this
#: one is a Fortran/CPU fixture.
FORTRAN_CPU: TargetProfile = load_target_profile(REPO_ROOT, "fortran_cpu")
TARGET_ID: str = FORTRAN_CPU.target_id

SECOND_TARGET_ID = "fortran_cpu_t2"


def second_target(profile: TargetProfile = FORTRAN_CPU,
                  target_id: str = SECOND_TARGET_ID) -> TargetProfile:
    """`profile` under another id: identical content, so only the target's identity moves."""
    doc = {**profile.doc, "target_id": target_id}
    return replace(profile, target_id=target_id, doc=doc,
                   sha256=sha256_hex(canonical_json_bytes(doc)))


SECOND_TARGET: TargetProfile = second_target()


def profile_with(profile: TargetProfile = FORTRAN_CPU, **sections: dict) -> TargetProfile:
    """`profile` with some sections' keys overridden — `profile_with(toolchain={"language":
    "c"})` — under the same id. For the tests that used to vary a node's IR toolchain: since
    issue #284 the host reads the TARGET, so the variation belongs on the profile. The loader's
    registry-free shape is kept; nothing here asks whether the host implements the value.

    `execution` is REPLACED, not merged, when given: it is one shape, and a merge would carry
    the base profile's `sweep` into a shape it was not written for. A variant that changes the
    parallel model and states no `execution` runs one thread per rank and no sweep (issue #512):
    the checked-in profile's threads and thread sweep are its own model's, which another model
    may refuse."""
    doc = {k: (dict(v) if isinstance(v, dict) else v) for k, v in profile.doc.items()}
    if "parallel" in sections and "execution" not in sections:
        doc["execution"] = {"threads_per_rank": 1}
    for section, overrides in sections.items():
        doc[section] = (dict(overrides) if section == "execution"
                        else {**doc.get(section, {}), **overrides})
    return replace(profile, doc=doc, sha256=sha256_hex(canonical_json_bytes(doc)))


def install_target_profile(repo_root: Path | str,
                           profile: TargetProfile = FORTRAN_CPU) -> Path:
    """Write `profile` into a fixture repository as `spec/targets/<target_id>.yaml`, so the
    readers that load a target from a pipeline path (the validator, `record_launch`) find it.
    The checked-in profile is copied byte for byte; any other (a variant under the same id
    included) is dumped from its document."""
    import yaml

    dest = Path(repo_root) / TARGETS_DIR / f"{profile.target_id}{TARGET_PROFILE_SUFFIX}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    source = REPO_ROOT / TARGETS_DIR / f"{profile.target_id}{TARGET_PROFILE_SUFFIX}"
    if source.is_file() and load_target_profile(REPO_ROOT, profile.target_id).sha256 == \
            profile.sha256:
        shutil.copyfile(source, dest)
    else:
        dest.write_text(yaml.safe_dump(profile.doc, sort_keys=False), encoding="utf-8")
    return dest


def fixture_target(repo_root: Path | str, explicit: TargetProfile | None = None) -> TargetProfile:
    """The target a test fake answers with: `explicit` when given, else the profile the
    fixture repository itself declares under the fixture id (`install_target_profile` wrote
    it, possibly a `profile_with` variant), else the checked-in one."""
    if explicit is not None:
        return explicit
    try:
        return load_target_profile(Path(repo_root), TARGET_ID)
    except Exception:  # noqa: BLE001 - a repo that declares none, or a non-existent root
        return FORTRAN_CPU


def pipes_dir(node_key_safe: str, target_id: str = TARGET_ID) -> str:
    """`workspace/pipelines/<node_key_safe>/<target_id>` for the fixture target."""
    return pipelines_dir(node_key_safe, target_id)


def pipe_ref(node_key_safe: str, pipeline_id: str, target_id: str = TARGET_ID) -> str:
    """`workspace/pipelines/<node_key_safe>/<target_id>/<pipeline_id>` for the fixture target."""
    return pipeline_ref_for(node_key_safe, target_id, pipeline_id)


def composed_pure_template(key: str, profile: TargetProfile = FORTRAN_CPU) -> str:
    """The pure launch template `key` (an `_PROMPT_TEMPLATE_FILES` key, e.g.
    `"pure generate.generate"`) composed for `profile`'s language and parallel backend — the
    text a leaf of that target reads, and what a test pinning a template's wording must read
    since issue #289 moved the language's rules into
    `tools/prompt_templates/backends/language/<id>/` (and issue #316 the parallel model's into
    `tools/prompt_templates/backends/parallel/<id>/`)."""
    from tools import orchestration_runtime as ort
    return ort._compose_fragments(
        ort._load_launch_prompt_templates()[key], ort._PROMPT_TEMPLATE_FILES[key],
        {"pure_language": profile.toolchain["language"],
         "pure_parallel": profile.parallel_backend})


def passing_shape_rows(profile: TargetProfile = FORTRAN_CPU) -> list[dict]:
    """`quality_check.json#shapes` of a run of `profile` whose every execution shape agreed with
    the serial reference (issue #512): one passing row per `profile.execution_shapes`, in order,
    with the rank count and grid a harness of a launcher target reports, the block size a
    harness of a backend that imposes one reports, and the thread count every harness reports
    (the shape's own: the count it observes at run time). The bounds-checked binary's row comes
    last when the profile's compiler declares checked flags (`checked_build.checked_shape`,
    issue #589 PR-0); `plant_checked_build` writes the records it is bound to."""
    from tools.checked_build import checked_shape

    rows = []
    checked = checked_shape(profile)
    for shape in [*profile.execution_shapes, *([checked] if checked is not None else [])]:
        rows.append({**shape.record(), "command_id": f"cmd_{shape.label}", "run_ok": True,
                     "verdict_available": True,
                     "verdict_match": True, "verdict": "pass",
                     "observed_mpi_ranks": shape.ranks,
                     "observed_process_grid": list(shape.process_grid or (shape.ranks, 1)),
                     "observed_block_size": shape.block_size,
                     "observed_threads_per_rank": shape.threads_per_rank,
                     "state_agreement": {"status": "agree"}, "status": "pass"})
    return rows


def plant_checked_build(pipeline_dir: Path, node_dir: Path, *, binary_id: str, source_id: str,
                        exe: str, profile: TargetProfile = FORTRAN_CPU,
                        command_id: str = "cmd_checked") -> Path:
    """The records the post-execute gate binds a `checked` row to (issue #589 PR-0), as Build and
    Validate.execute write them for `profile`: the checked binary at
    `<pipeline>/binary/<binary_id>/bin_checked/<exe>`, `binary_meta.json#checked_build` (merged
    into a record already there) with the overrides `checked_build_overrides` composes, its
    `compile_project` record in the source's command log, and the row's bare `run_program`
    record (`command_id`) in the run's command log beside `node_dir`'s trial. Returns the
    checked binary's path."""
    import json

    from tools.backends import registry
    from tools.checked_build import checked_flags_for, control_file_rules

    binary_dir = pipeline_dir / "binary" / binary_id
    bin_checked = binary_dir / "bin_checked"
    bin_checked.mkdir(parents=True, exist_ok=True)
    checked_binary = bin_checked / exe
    checked_binary.write_text("checked binary\n", encoding="utf-8")
    obj_dir = str(pipeline_dir / "tmp" / "build_checked")
    flags = list(checked_flags_for(profile))
    # The package's module, not `capability_module`: a row may have patched the record's
    # declaration away to observe the gate refusing it.
    execute = registry.load("build_system", str(profile.toolchain["build_system"])).execute
    overrides = list(execute.checked_build_overrides(
        obj_dir, str(bin_checked), exe, control_file_rules(profile), flags))
    argv = ["make", "-j4", *overrides]
    meta_path = binary_dir / "binary_meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
    meta.update({"source_source_id": source_id,
                 "binary_artifact_ref": f"binary/{binary_id}/bin/{exe}",
                 "checked_build": {"binary_ref": f"binary/{binary_id}/bin_checked/{exe}",
                                   "obj_dir": obj_dir, "bin_dir": str(bin_checked),
                                   "flags": flags, "command_id": "cmd_checked_build",
                                   "argv": argv}})
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    build_log = pipeline_dir / "source" / source_id / "src" / "command_log.jsonl"
    build_log.parent.mkdir(parents=True, exist_ok=True)
    with build_log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"command_id": "cmd_checked_build", "tool_name": "compile_project",
                             "command": argv, "ok": True}) + "\n")
    run_log = node_dir / "command_log.jsonl"
    with run_log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"command_id": command_id, "tool_name": "run_program",
                             "command": [str(checked_binary), "--cases", "spec.ir.yaml", "c1"],
                             "cwd": str(node_dir), "ok": True}) + "\n")
    return checked_binary
