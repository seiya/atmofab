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
    registry-free shape is kept; nothing here asks whether the host implements the value."""
    doc = {k: (dict(v) if isinstance(v, dict) else v) for k, v in profile.doc.items()}
    for section, overrides in sections.items():
        doc[section] = {**doc.get(section, {}), **overrides}
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
    `"pure generate.generate"`) composed for `profile`'s language — the text a leaf of that
    target reads, and what a test pinning a template's wording must read since issue #289 moved
    the language's rules into `tools/prompt_templates/backends/language/<id>/`."""
    from tools import orchestration_runtime as ort
    return ort._compose_language_fragments(
        ort._load_launch_prompt_templates()[key], ort._PROMPT_TEMPLATE_FILES[key],
        profile.toolchain["language"])
